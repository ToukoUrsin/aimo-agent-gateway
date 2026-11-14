"""SQLite store: checkouts, orders, stock holds and a hash-chained audit ledger."""
import hashlib
import json
import sqlite3
import threading
import time
from datetime import datetime

from .config import DATA, TZ
from .signing import jcs

DB = DATA / "gateway.db"
SCHEMA = """
create table if not exists kv(k text primary key, v text);
create table if not exists checkouts(id text primary key, status text, created real, doc text);
create table if not exists orders(id text primary key, created real, customer text, total integer, doc text);
create table if not exists holds(id integer primary key autoincrement, ref text, product text, qty real, expires real, state text);
create table if not exists events(seq integer primary key autoincrement, ts text, ref text, cid text, actor text, kind text, text text, data text, prev text, hash text);
create table if not exists runs(id text primary key, created real, doc text);
"""


MIGRATIONS = [
    "alter table checkouts add column customer text",
    "alter table holds add column world text default 'main'",
    "alter table events add column acct text",  # which account an entry belongs to; not part of the hash
]


class Store:
    def __init__(self, path=None):
        self.db = sqlite3.connect(path or DB, check_same_thread=False, isolation_level=None)
        self.db.execute("pragma journal_mode=wal")
        self.db.execute("pragma synchronous=normal")  # safe with WAL; concurrent checkouts are about 2.4x faster
        self.db.executescript(SCHEMA)
        for m in MIGRATIONS:
            try:
                self.db.execute(m)
            except sqlite3.OperationalError:
                pass  # column already there
        self.lock = threading.RLock()

    def q(self, sql, args=()):
        with self.lock:
            return self.db.execute(sql, args).fetchall()

    # key/value
    def get(self, k, default=None):
        r = self.q("select v from kv where k=?", (k,))
        return json.loads(r[0][0]) if r else default

    def put(self, k, v):
        self.q("insert into kv(k,v) values(?,?) on conflict(k) do update set v=excluded.v", (k, json.dumps(v)))

    def next_counter(self, k):
        with self.lock:
            current = self.get(k, 0) + 1
            self.put(k, current)
            return current

    # documents
    def save_checkout(self, c):
        self.q(
            "insert into checkouts(id,status,created,doc,customer) values(?,?,?,?,?) on conflict(id) do update set status=excluded.status, doc=excluded.doc, customer=excluded.customer",
            (c["id"], c["status"], time.time(), json.dumps(c), (c.get("_internal") or {}).get("customer")),
        )

    def checkout(self, cid):
        r = self.q("select doc from checkouts where id=?", (cid,))
        return json.loads(r[0][0]) if r else None

    def checkouts(self, limit=30, customer=None):
        if customer:
            return [json.loads(r[0]) for r in self.q("select doc from checkouts where customer=? order by created desc limit ?", (customer, limit))]
        return [json.loads(r[0]) for r in self.q("select doc from checkouts order by created desc limit ?", (limit,))]

    def save_order(self, o, customer, total):
        self.q("insert or replace into orders(id,created,customer,total,doc) values(?,?,?,?,?)", (o["id"], time.time(), customer, total, json.dumps(o)))

    def order(self, oid):
        r = self.q("select doc from orders where id=?", (oid,))
        return json.loads(r[0][0]) if r else None

    def orders(self, limit=30, customer=None):
        if customer:
            return [json.loads(r[0]) for r in self.q("select doc from orders where customer=? order by created desc limit ?", (customer, limit))]
        return [json.loads(r[0]) for r in self.q("select doc from orders order by created desc limit ?", (limit,))]

    def spent_since(self, customer, since):
        return self.q("select coalesce(sum(total),0) from orders where customer=? and created>=?", (customer, since))[0][0]

    # stock holds: our own ledger on top of the read-only mock API
    def reserved(self, product, exclude_ref="", world="main"):
        """Stock already promised to others. A world is one pool of stock: the stage and the floor share one, each sandbox has its own."""
        r = self.q(
            "select coalesce(sum(qty),0) from holds where product=? and ref!=? and world=? and (state='ordered' or (state='held' and expires>?))",
            (product, exclude_ref, world, time.time()),
        )
        return r[0][0]

    def hold(self, ref, lines, expires, world="main"):
        with self.lock:
            self.q("update holds set state='released' where ref=? and state='held'", (ref,))
            for product, qty in lines:
                self.q("insert into holds(ref,product,qty,expires,state,world) values(?,?,?,?,'held',?)", (ref, product, qty, expires, world))

    def settle(self, ref, state):
        self.q("update holds set state=? where ref=? and state='held'", (state, ref))

    # audit ledger: every event carries the hash of the previous link and a
    # digest of its body.  The digest lets readers verify withheld entries.
    EV = ["seq", "ts", "ref", "cid", "actor", "kind", "text", "data", "prev", "hash"]

    @staticmethod
    def _digest(ts, ref, cid, actor, kind, text, data):
        body = {"ts": ts, "ref": ref, "cid": cid, "actor": actor, "kind": kind, "text": text, "data": data}
        return hashlib.sha256(jcs(body)).hexdigest()

    @classmethod
    def _hash(cls, prev, ts, ref, cid, actor, kind, text, data):
        return hashlib.sha256(prev.encode() + cls._digest(ts, ref, cid, actor, kind, text, data).encode()).hexdigest()

    def log(self, ref, actor, kind, text, data=None, cid=None, acct=None):
        """ref = the agent's trace id (one buyer request), cid = checkout id once there is one, acct = customer number."""
        with self.lock:
            if acct is None and cid:
                r = self.q("select customer from checkouts where id=?", (cid,))
                acct = r[0][0] if r else None
            last = self.q("select hash from events order by seq desc limit 1")
            prev = last[0][0] if last else "0" * 64
            ts = datetime.now(TZ).isoformat(timespec="seconds")
            data = json.loads(json.dumps(data or {}))
            h = self._hash(prev, ts, ref or "", cid or "", actor, kind, text, data)
            self.q(
                "insert into events(ts,ref,cid,actor,kind,text,data,prev,hash,acct) values(?,?,?,?,?,?,?,?,?,?)",
                (ts, ref or "", cid or "", actor, kind, text, json.dumps(data), prev, h, acct),
            )
            return h

    def events(self, ref=None, cid=None, limit=400, acct=None, since=0):
        where, args = [], []
        if ref:
            where.append("ref=?"); args.append(ref)
        if cid:
            where.append("cid=?"); args.append(cid)
        cond = ["(" + " or ".join(where) + ")"] if where else []
        if acct:
            cond.append("acct=?"); args.append(acct)
        if since:
            cond.append("seq>?"); args.append(since)
        cols = self.EV + ["acct"]
        sql = "select " + ",".join(cols) + " from events " + ("where " + " and ".join(cond) + " " if cond else "") + "order by seq desc limit ?"
        out = [dict(zip(cols, r)) for r in self.q(sql, (*args, limit))][::-1]
        for e in out:
            e["data"] = json.loads(e["data"])
            e["digest"] = self._digest(e["ts"], e["ref"], e["cid"], e["actor"], e["kind"], e["text"], e["data"])
            e["withheld"] = False
        return out

    def ledger(self, reader_acct=None, operator=False, limit=200, since=0, open_accts=()):
        """Return a verifiable ledger view, redacting bodies outside reader_acct (and outside open_accts: demo accounts anyone may read)."""
        rows = self.events(limit=max(1, min(int(limit), 1000)), since=max(0, int(since)))
        if operator:
            return rows
        out = []
        for e in rows:
            if (reader_acct and e.get("acct") == reader_acct) or e.get("acct") in open_accts:
                out.append(e)
                continue
            out.append({"seq": e["seq"], "ts": e["ts"], "ref": None, "cid": None, "acct": None, "actor": None, "kind": e["kind"], "text": None, "data": None,
                        "prev": e["prev"], "hash": e["hash"], "digest": e["digest"], "withheld": True})
        return out

    def verify_chain(self):
        """Recompute every hash. Returns (ok, number_of_events, first_bad_seq)."""
        prev = "0" * 64
        rows = self.q("select " + ",".join(self.EV) + " from events order by seq")
        for seq, ts, ref, cid, actor, kind, text, data, p, h in rows:
            if p != prev or self._hash(prev, ts, ref, cid, actor, kind, text, json.loads(data)) != h:
                return False, len(rows), seq
            prev = h
        return True, len(rows), None

    def verify_chain_incremental(self, state=None):
        """Check only events appended since state=(last_seq,last_hash,bad,count)."""
        state = state or {"seq": 0, "prev": "0" * 64, "bad": None, "count": 0}
        rows = self.q("select " + ",".join(self.EV) + " from events where seq>? order by seq", (state["seq"],))
        prev, bad = state["prev"], state.get("bad")
        for seq, ts, ref, cid, actor, kind, text, data, p, h in rows:
            if bad is None and (p != prev or self._hash(prev, ts, ref, cid, actor, kind, text, json.loads(data)) != h):
                bad = seq
            prev = h
            state["seq"] = seq
        state["prev"], state["bad"], state["count"] = prev, bad, state.get("count", 0) + len(rows)
        if not rows:
            state["count"] = self.q("select count(*) from events")[0][0]
        return bad is None, state["count"], bad

    # agent runs shown in the demo console
    def save_run(self, run):
        self.q("insert or replace into runs(id,created,doc) values(?,?,?)", (run["id"], run.get("created", time.time()), json.dumps(run)))

    def run(self, rid):
        r = self.q("select doc from runs where id=?", (rid,))
        return json.loads(r[0][0]) if r else None

    def runs(self, limit=10):
        return [json.loads(r[0]) for r in self.q("select doc from runs order by created desc limit ?", (limit,))]
