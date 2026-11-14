"""Valio Aimo's control tower: every account and every agent on one screen, with the switches.

Anyone may look. Every control needs the operator.

The numbers come from the ledger. Each ledger entry is read once and folded into counters, so the page
costs the same with fifty entries or fifty thousand. The hash chain is checked the same way (each new
entry against the one before it), with a full recompute in the background every half minute.

The floor (agent/floor.py) is a set of simulated customers that order through the public interface. This
module only seeds their accounts, hands their agents a mandate (a stand-in for the buyer's own system)
and keeps the run's heartbeat. It never places, approves or refuses anything for them.

The Monday rush is the same idea at one moment: 60 accounts, one short product, every agent asking in the
same second. The split of the short stock is gateway/allocation.py; this module shows it, keeps the run's
events for the screen and stores the measured result of each run (evals/rush/).
"""
import asyncio
import hashlib
import json
import math
import re
import secrets
import threading
import time
from collections import deque
from datetime import datetime

from fastapi import APIRouter, Body, Request

from . import policy
from .aimo import facts
from .catalog import UNITS
from .config import OPERATOR_KEY, PORT, ROOT, STAGE, TZ
from .service import GatewayError
from .signing import jwt_encode, now

COOKIE = "aimo_s"
GENESIS = "0" * 64
TIERS = {"order": "orders alone", "reorder": "re-orders only", "read": "look up only"}
RULE_TITLES = {"SIG": "Order not signed with the agent's key"}
INDEXES = [
    "create index if not exists ix_events_acct on events(acct, seq)",
    "create index if not exists ix_events_ref on events(ref)",
    "create index if not exists ix_events_cid on events(cid)",
    "create index if not exists ix_holds_product on holds(product, world)",
    "create index if not exists ix_holds_ref on holds(ref)",
    "create index if not exists ix_checkouts_customer on checkouts(customer, created)",
    "create index if not exists ix_checkouts_status on checkouts(status)",
    "create index if not exists ix_orders_customer on orders(customer, created)",
]
COLS = "seq,ts,ref,cid,actor,kind,text,data,prev,hash,acct"


def _blank() -> dict:
    return {"orders": 0, "ordered": 0, "alone": 0, "approved": 0, "refused": 0, "rejected": 0, "asked": 0, "short": 0}


def _cut(text: str, n: int = 170) -> str:
    t = str(text or "").strip()
    return t if len(t) <= n else t[: n - 1].rstrip() + "…"


def _first(text: str, n: int = 170) -> str:
    """The first sentence, for a feed line."""
    t = str(text or "").strip()
    m = re.search(r"(?<=[a-z0-9)])\. ", t)
    return _cut(t[: m.start() + 1] if m else t, n)


def _line(kind: str, text: str, data: dict):
    """(tone, head, text) for the live feed, or None for entries that are not news."""
    if kind == "order":
        ok = bool((data.get("evidence") or {}).get("approval"))
        return "order", f"Ordered {policy.eur(data.get('subtotal') or 0)}", ("A person approved it. " if ok else "Alone, inside its mandate. ") + str(data.get("label") or "")
    if kind == "policy":
        dec = data.get("decision")
        hit = [r for r in data.get("rules") or [] if r.get("outcome") == {"deny": "deny", "escalate": "escalate"}.get(dec)]
        if not hit:  # nothing refused or asked: news only when short stock was split (rule L1) and the agent got less than it asked for
            cut = next((r for r in data.get("rules") or [] if r.get("id") == "L1" and "not confirmed" in str(r.get("detail"))), None)
            return ("share", "Offered its share", _first(cut["detail"], 200)) if cut else None
        ids = " ".join(dict.fromkeys(r["id"] for r in hit))
        return ("deny", "Refused", f"{ids}: {hit[0]['detail']}") if dec == "deny" else ("ask", "Asked a person", f"{ids}: {hit[0]['detail']}")
    if kind in ("approved", "rejected"):
        ap = data.get("approval") or {}
        ids = " ".join(dict.fromkeys(ap.get("overrides") or []))
        said = f"{ap['by']}{', about ' + ids if ids else ''}: {ap.get('note') or ('yes' if kind == 'approved' else 'no')}" if ap.get("by") else text
        return ("human", "A person approved", _cut(said, 220)) if kind == "approved" else ("no", "A person said no", _cut(said, 220))
    if kind == "refused":
        return "deny", "Refused", _first(text)
    if kind == "control":
        return ("deny", "Agent suspended", _first(text)) if data.get("rule") == "W4" else ("control", "Valio Aimo", _first(text, 200))
    if kind == "exception":
        return "short", "Stock short" if data.get("code") in ("insufficient_stock", "out_of_stock") else "Exception", _first(text)
    return None


class Fold:
    """Today's numbers and the feed, kept current from the ledger."""

    def __init__(self, g):
        self.g, self.q = g, g.store.q
        self.lock = threading.Lock()
        self.first = None      # seq of the first ledger entry (changes only when the ledger is wiped)
        self.seq = 0           # last entry read
        self.prev = GENESIS    # its hash
        self.bad = None        # first entry that breaks the chain
        self.count = 0
        self.full = {"t": 0.0, "busy": False}
        self.known: dict[str, int] = {}  # customer -> since_seq the counters were built with
        self.since: dict[str, int] = {}
        self._clear()

    def _clear(self):
        self.day = datetime.now(TZ).strftime("%Y-%m-%d")
        self.acct: dict[str, dict] = {}
        self.rules: dict[str, dict] = {}
        self.feed = deque(maxlen=60)
        self.traces: dict[str, tuple] = {}
        self.suspended_today = 0

    def _rows(self, after: int, upto: int | None = None):
        while True:
            rows = self.q(f"select {COLS} from events where seq>? order by seq limit 4000", (after,))
            for r in rows:
                if upto is not None and r[0] > upto:
                    return
                yield r
            if len(rows) < 4000:
                return
            after = rows[-1][0]

    def _take(self, row, data):
        seq, ts, ref, cid, actor, kind, text, _, _, _, cust = row
        if actor.startswith("agent:") and ref:
            if len(self.traces) > 30000:
                self.traces.clear()
            self.traces[ref] = (cust or self.traces.get(ref, (None,))[0], actor)
        if ts[:10] != self.day:
            return
        known = self.traces.get(ref) or (None, None)
        cust = cust or known[0]
        if cust and seq <= self.since.get(cust, 0):
            return
        c = self.acct.setdefault(cust or "", _blank())
        if kind == "order":
            c["orders"] += 1
            c["ordered"] += data.get("subtotal") or 0
            c["approved" if (data.get("evidence") or {}).get("approval") else "alone"] += 1
        elif kind == "policy":
            dec = data.get("decision")
            if dec == "deny":
                c["refused"] += 1
            elif dec == "escalate":
                c["asked"] += 1
            for rid, out, title in {(r["id"], r["outcome"], r["title"]) for r in data.get("rules") or [] if r.get("outcome") in ("escalate", "deny")}:
                x = self.rules.setdefault(rid, {"id": rid, "title": title, "asked": 0, "refused": 0})
                x["asked" if out == "escalate" else "refused"] += 1
        elif kind == "refused":
            c["refused"] += 1
            self.rules.setdefault("SIG", {"id": "SIG", "title": RULE_TITLES["SIG"], "asked": 0, "refused": 0})["refused"] += 1
        elif kind == "rejected":
            c["rejected"] += 1
        elif kind == "exception" and data.get("code") in ("insufficient_stock", "out_of_stock"):
            c["short"] += 1
        elif kind == "control" and data.get("rule") == "W4":
            self.suspended_today += 1
        line = _line(kind, text, data)
        if line:
            last = self.feed[-1] if self.feed else None
            if line[0] == "short" and last and last["tone"] == "short" and last["ref"] == (cid or ref):
                last["more"] += 1  # several lines of one order ran short: one entry in the feed
                last["text"] = f"{last['first']} And {last['more']} more product{'s' if last['more'] > 1 else ''}."
                return
            agent = actor if actor.startswith("agent:") else data.get("agent") or known[1]
            if last and (last["cust"], last["agent"], last["tone"], last["head"], last["first"]) == (cust, agent, line[0], line[1], line[2]):
                last.update(n=last["n"] + 1, seq=seq, ts=ts)  # the same thing again: one line with a count, never the same line twice in a row
                return
            self.feed.append({"seq": seq, "ts": ts, "cust": cust, "agent": agent, "who": actor[6:] if actor.startswith("human:") else None, "tone": line[0], "head": line[1], "text": line[2],
                              "ref": cid or ref, "first": line[2], "more": 0, "n": 1})

    def sync(self, since: dict[str, int]):
        """Read what is new in the ledger. `since` = for each customer, the entry its account was last reset at."""
        with self.lock:
            lo, hi, n = self.q("select min(seq), max(seq), count(*) from events")[0]
            self.count = n
            if lo != self.first:  # empty, or wiped and started again: start over from the first entry
                self.first, self.seq, self.prev, self.bad = lo, 0, GENESIS, None
                self.known = {}
                self._clear()
            redo = any(c in self.known and self.known[c] != s for c, s in since.items()) or datetime.now(TZ).strftime("%Y-%m-%d") != self.day
            self.since, self.known = since, dict(since)
            if redo:  # an account was reset (or the day turned): count today again, without re-checking the chain
                self._clear()
                start = self.q("select coalesce(min(seq),1) from events where ts>=?", (self.day,))[0][0]
                for row in self._rows(start - 1, self.seq):
                    self._take(row, json.loads(row[7]))
            for row in self._rows(self.seq):
                data = json.loads(row[7])
                if self.bad is None and (row[8] != self.prev or self.g.store._hash(self.prev, row[1], row[2], row[3], row[4], row[5], row[6], data) != row[9]):
                    self.bad = row[0]
                self.seq, self.prev = row[0], row[9]
                self._take(row, data)
            if time.time() - self.full["t"] > max(30.0, 100 * self.full.get("took", 0.0)) and not self.full["busy"]:
                self.full["busy"] = True
                threading.Thread(target=self._full, daemon=True).start()

    def _full(self):
        """Recompute the whole chain now and then, so a change to an old entry shows up too."""
        t0 = time.time()
        try:
            top = self.q("select coalesce(max(seq),0) from events")[0][0]
            ok, _, bad = self.g.store.verify_chain()
            with self.lock:
                if not ok and (self.bad is None or bad < self.bad):
                    self.bad = bad
                elif ok and self.bad is not None and self.bad <= top:
                    self.bad = None
        finally:
            self.full.update(t=time.time(), busy=False, took=time.time() - t0)


def stage_print(g) -> dict:
    """A fingerprint of the stage account: a run, a stop or a reset of the floor or the rush must leave it exactly as it was.
    Proposals Valio Aimo's own agent makes to the stage account on its timer (refs prj_... and prp_...) are not part of it: they come whether or not anything runs."""
    a = g.account(STAGE) or {}
    c = a.get("customer", "")
    q = g.store.q
    parts = {"account": a, "profile": g.store.get("profile:" + c), "history": g.store.get("history:" + c), "mandate": g.store.get("mandate_config:" + c),
             "orders": q("select id from orders where customer=? order by id", (c,)), "checkouts": q("select id, status from checkouts where customer=? order by id", (c,)),
             "holds": q("select ref, product, qty, state from holds where world='main' and substr(ref, 1, 4) not in ('prj_', 'prp_') order by ref, product, qty, state"),
             "agents": q("select k, v from kv where k like ? order by k", (f"agent:agent:{STAGE}/%",)), "events": q("select count(*) from events where acct=? and (ref is null or substr(ref, 1, 4) not in ('prj_', 'prp_'))", (c,))[0][0]}
    return {"hash": hashlib.sha256(json.dumps(parts, sort_keys=True, default=str).encode()).hexdigest()[:20], "orders": len(parts["orders"]), "checkouts": len(parts["checkouts"]), "holds": len(parts["holds"]), "ledger_entries": parts["events"]}


def build(g, ctx, page) -> APIRouter:
    r = APIRouter(include_in_schema=False)
    for sql in INDEXES:
        g.store.q(sql)
    fold = Fold(g)
    cache = {"t": 0.0, "v": None}
    cache_lock = threading.Lock()
    source: dict[str, tuple] = {}  # product -> (seen at, stock at Valio Aimo or None when not tracked)
    fl = {"id": None, "seen": 0.0, "stop": False, "status": {}, "task": None}
    rs = {"id": None, "seen": 0.0, "stop": False, "status": {}, "events": [], "cast": [], "task": None,
          "world": "rush", "live": False, "cool_until": 0.0, "ledger_refs": []}  # the live rush
    LIVE_COUNT, LIVE_MODELS, LIVE_COOLDOWN = 12, 3, 8.0
    evals = ROOT / "evals"

    # ------------------------------------------------------------------ who
    def looks_like_operator(request: Request) -> bool:
        """For showing or hiding the controls. No side effects (ctx() would open a sandbox for a first-time visitor)."""
        if OPERATOR_KEY and secrets.compare_digest(request.headers.get("x-operator-key", ""), OPERATOR_KEY):
            return True
        tok = request.cookies.get(COOKIE)
        return bool(tok and (g.store.get("session:" + tok) or {}).get("operator"))

    def need_operator(request: Request):
        if not looks_like_operator(request) or not ctx(request)["op"]:
            raise GatewayError(403, "forbidden", "This needs the operator key.")

    def kv_range(prefix: str) -> list[dict]:
        return [json.loads(v) for v, in g.store.q("select v from kv where k>=? and k<?", (prefix, prefix[:-1] + chr(ord(prefix[-1]) + 1)))]

    def running() -> bool:
        return bool(fl["id"]) and not fl["status"].get("done") and time.time() - fl["seen"] < 25

    def rush_world() -> str:
        return rs.get("world") or "rush"

    # ---------------------------------------------------------------- stock
    def floor_day() -> str:
        try:
            from agent.floor import DAY
            return DAY
        except Exception:
            return ""

    def stock() -> list[dict]:
        rows = g.store.q("select product, sum(case when state='ordered' then qty else 0 end), sum(case when state='held' and expires>? then qty else 0 end) "
                         "from holds where world=? group by product", (time.time(), rush_world() if rush_running() else "floor"))
        live = {p: (o or 0, h or 0) for p, o, h in rows if (o or 0) + (h or 0) > 0}
        if not live:
            return []
        with g.aimo.lock:
            seen = list(g.aimo.cache.items())
        for (pid, day, _), (t, rec) in seen:
            if pid in live and (pid not in source or source[pid][0] < t):
                f = facts(rec, day)
                source[pid] = (t, f["available"] if f["stock_known"] else None)
        missing, day = [p for p in live if p not in source][:25], floor_day()
        if missing and day:  # after a restart: ask the stock API once for what nobody has looked up yet
            try:
                for pid, rec in g.aimo.quote(missing, day, "public").items():
                    f = facts(rec, day)
                    source[pid] = (time.time(), f["available"] if f["stock_known"] else None)
            except Exception:
                pass
            for pid in missing:
                source.setdefault(pid, (time.time(), None))
        out = []
        for pid, (ordered, held) in live.items():
            total = (source.get(pid) or (0, None))[1]
            d = g.cat.get(pid)
            if not total or not d:
                continue
            left = max(0.0, total - ordered - held)
            out.append({"id": pid, "title": d["title"], "unit": UNITS.get(d["unit"], (0, d["unit"]))[1], "source": total, "ordered": round(ordered, 2), "held": round(held, 2), "left": round(left, 2),
                        "used_pct": round(min(100.0, 100 * (ordered + held) / total), 1)})
        out.sort(key=lambda x: (-x["used_pct"], x["title"]))
        return out[:8]

    # -------------------------------------------------------------- payload
    def compute() -> dict:
        accounts = kv_range("acct:")
        by_cust = {a["customer"]: a for a in accounts}
        paused = g.store.get("tower:paused")
        if paused:  # accounts opened while everything is paused start paused too
            for a in accounts:
                if a.get("created", "") > paused["at"] and a["slug"] not in paused["slugs"]:
                    prof = g.store.get("profile:" + a["customer"]) or policy.default_profile()
                    if prof.get("enabled", True):
                        prof["enabled"] = False
                        g.store.put("profile:" + a["customer"], prof)
                        paused["slugs"].append(a["slug"])
                        g.store.put("tower:paused", paused)
        fold.sync({a["customer"]: a.get("since_seq", 0) for a in accounts})
        today = fold.day
        agents = kv_range("agent:")
        t_now = time.time()

        def recent(ag):
            try:
                return t_now - datetime.fromisoformat(ag["last_seen"]).timestamp() < 120
            except Exception:
                return False

        shown = [a for a in accounts if a["kind"] != "sandbox"]
        shown.sort(key=lambda a: (a["kind"] != "stage", a["name"].lower()))
        waiting = dict(g.store.q("select customer, count(*) from checkouts where status='requires_escalation' group by customer"))
        rows = []
        for a in shown:
            prof = g.store.get("profile:" + a["customer"]) or policy.default_profile()
            c = fold.acct.get(a["customer"]) or _blank()
            mine = sorted((ag for ag in agents if ag.get("slug") == a["slug"]), key=lambda ag: ag.get("first_seen", ""))
            rows.append({"slug": a["slug"], "name": a["name"], "kind": a["kind"], "segment": a.get("segment"), "enabled": bool(prof.get("enabled", True)), "tier": prof.get("tier", "order"), "cap": prof.get("max_order_total"),
                         "orders": c["orders"], "ordered": c["ordered"], "asked": c["asked"], "refused": c["refused"] + c["rejected"], "waiting": waiting.get(a["customer"], 0),
                         "agents": [{"id": ag["id"], "name": ag.get("name") or ag["id"].split("/")[-1], "kind": ag.get("kind", "key"), "status": ag.get("status", "active"), "reason": ag.get("reason"),
                                     "orders": ag.get("orders", 0), "asked": ag.get("asked", 0), "refused": ag.get("refused", 0), "last_seen": ag.get("last_seen"), "now": recent(ag)} for ag in mine]})
        total = _blank()
        for c in fold.acct.values():
            for k in total:
                total[k] += c[k]
        names = {ag["id"]: ag.get("name") for ag in agents}

        def label(cust):
            a = by_cust.get(cust)
            return "Valio Aimo" if not cust else "A visitor's sandbox" if not a or a["kind"] == "sandbox" else a["name"]

        feed = [{"seq": e["seq"], "ts": e["ts"], "account": label(e["cust"]) if e["cust"] or e["tone"] == "control" else "Unknown caller",
                 "agent": names.get(e["agent"]) or (e["agent"].split("/")[-1] if e["agent"] else e["who"] or ""), "tone": e["tone"], "head": e["head"], "n": e.get("n", 1),
                 "text": e["text"].replace(e["agent"], names[e["agent"]]) if e["agent"] in names else e["text"]} for e in list(fold.feed)[-40:]][::-1]
        rules = sorted(fold.rules.values(), key=lambda x: -(x["asked"] + x["refused"]))
        st = fl["status"]
        return {
            "now": datetime.now(TZ).isoformat(timespec="seconds"), "day": today, "paused_all": bool(paused),
            "today": {**total, "waiting": sum(waiting.values()),
                      "agents_active": sum(1 for ag in agents if ag.get("status") != "suspended" and str(ag.get("last_seen", ""))[:10] == today),
                      "agents_now": sum(1 for ag in agents if ag.get("status") != "suspended" and recent(ag)),
                      "agents_suspended": sum(1 for ag in agents if ag.get("status") == "suspended"), "suspended_today": fold.suspended_today,
                      "agents_known": len(agents), "last_call": max((str(ag.get("last_seen") or "") for ag in agents), default="") or None,
                      "accounts": len(shown), "accounts_paused": sum(1 for x in rows if not x["enabled"]), "sandboxes": len(accounts) - len(shown)},
            "ledger": {"intact": fold.bad is None, "events": fold.count, "first_bad_seq": fold.bad},
            "accounts": rows, "feed": feed, "stock": stock(), "rules": rules, "aimo_calls": g.aimo.calls,
            "floor": {"running": running(), "stopping": running() and fl["stop"], **{k: st.get(k) for k in ("round", "rounds", "minutes", "note", "started", "llm", "agents", "summary") if k in st}},
            "rush": {"running": rush_running(), "stopping": rush_running() and rs["stop"], "id": rs["id"], "live": bool(rs.get("live")),
                     "cooldown_s": max(0, round(rs.get("cool_until", 0) - time.time(), 1)),
                     **{k: rs["status"].get(k) for k in ("note", "started", "mix", "summary") if k in rs["status"]}},
            "allocations": [{k: v for k, v in a.items() if k != "customers"} for a in g.allocation.view()],
        }

    @r.get("/tower")
    def tower_page():
        return page("tower.html")

    @r.get("/api/tower")
    def tower(request: Request):
        with cache_lock:
            if time.time() - cache["t"] > 1.0:
                cache.update(v=compute(), t=time.time())
            return {**cache["v"], "operator": looks_like_operator(request)}

    def changed():
        cache["t"] = 0.0

    # ------------------------------------------------------------- controls
    @r.post("/api/tower/account")
    def set_account(request: Request, body: dict = Body(...)):
        """Valio Aimo's side: pause one account, change its tier or its cap per agent order."""
        need_operator(request)
        a = g.account(str(body.get("slug")))
        if not a:
            raise GatewayError(404, "not_found", "No such account.")
        prof = g.store.get("profile:" + a["customer"]) or policy.default_profile()
        did = []
        if "enabled" in body and bool(body["enabled"]) != bool(prof.get("enabled", True)):
            prof["enabled"] = bool(body["enabled"])
            did.append("switched agent ordering back on" if prof["enabled"] else "paused agent ordering")
        if "tier" in body and body["tier"] != prof.get("tier"):
            if body["tier"] not in TIERS:
                raise GatewayError(400, "bad_request", "tier is order, reorder or read.")
            prof["tier"] = body["tier"]
            did.append(f"set the agents' tier to '{TIERS[prof['tier']]}'")
        if "max_order_total" in body:
            try:
                cap = max(0, min(int(body["max_order_total"]), 100_000_000))
            except (TypeError, ValueError):
                raise GatewayError(400, "bad_request", "max_order_total is an amount in cents.")
            if cap != prof.get("max_order_total"):
                prof["max_order_total"] = cap
                did.append(f"set the cap per agent order to {policy.eur(cap)}")
        if "day_ceiling" in body:
            try:
                ceil = max(0, min(int(body["day_ceiling"]), 100_000_000))
            except (TypeError, ValueError):
                raise GatewayError(400, "bad_request", "day_ceiling is an amount in cents.")
            if ceil != prof.get("day_ceiling"):
                prof["day_ceiling"] = ceil
                did.append(f"set the ceiling for agent orders per delivery day to {policy.eur(ceil)}")
        if did:
            g.store.put("profile:" + a["customer"], prof)
            g.store.log(None, "human:Valio Aimo account manager", "control", f"Valio Aimo {' and '.join(did)} for {a['name']}.", {"profile": prof}, acct=a["customer"])
            changed()
        return prof

    @r.post("/api/tower/agent")
    def set_agent(request: Request, body: dict = Body(...)):
        """Valio Aimo's side: suspend or reinstate one agent."""
        need_operator(request)
        ag = g.set_agent(str(body.get("id")), bool(body.get("suspended")))
        changed()
        return {k: ag.get(k) for k in ("id", "status", "reason")}

    @r.post("/api/tower/pause-all")
    def pause_all(request: Request, body: dict = Body(...)):
        """One switch for every account. Switching it back on restores only what this switch paused."""
        need_operator(request)
        paused, n = g.store.get("tower:paused"), 0
        if body.get("paused") and not paused:
            slugs = []
            for a in kv_range("acct:"):
                prof = g.store.get("profile:" + a["customer"]) or policy.default_profile()
                if prof.get("enabled", True):
                    prof["enabled"] = False
                    g.store.put("profile:" + a["customer"], prof)
                    slugs.append(a["slug"])
            g.store.put("tower:paused", {"at": datetime.now(TZ).isoformat(timespec="seconds"), "slugs": slugs})
            n = len(slugs)
            g.store.log(None, "human:Valio Aimo account manager", "control", f"Valio Aimo paused agent ordering on every account ({n} accounts). Agents can still look up products.", {"accounts": n})
        elif not body.get("paused") and paused:
            for slug in paused["slugs"]:
                a = g.account(slug)
                if a:
                    prof = g.store.get("profile:" + a["customer"]) or policy.default_profile()
                    prof["enabled"] = True
                    g.store.put("profile:" + a["customer"], prof)
                    n += 1
            g.store.q("delete from kv where k='tower:paused'")
            g.store.log(None, "human:Valio Aimo account manager", "control", f"Valio Aimo switched agent ordering back on ({n} accounts).", {"accounts": n})
        changed()
        return {"paused_all": bool(g.store.get("tower:paused")), "accounts": n}

    # ---------------------------------------------------------------- floor
    def seed(keep: bool) -> dict:
        from agent.floor import CAST  # the buyer side; imported lazily so the gateway runs without it

        made = 0
        for spec in CAST:
            a = g.account(spec["slug"])
            if a and a["kind"] != "floor":
                raise GatewayError(409, "invalid_state", f"{spec['slug']} exists and is not a floor account.")
            fresh = not a
            if fresh:
                a = g.create_account(spec["slug"], spec["name"], kind="floor", ship_to=spec["ship_to"], history=spec["history"], mandate=spec["mandate"], world="floor", history_seeded=True)
                made += 1
            elif not keep:
                g.reset(spec["slug"])
                a = g.account(spec["slug"])
                a.update(name=spec["name"], ship_to=spec["ship_to"], world="floor")
                g.store.put("acct:" + a["slug"], a)
            if fresh or not keep:
                c = a["customer"]
                g.store.put("history:" + c, json.loads(json.dumps(spec["history"])))
                g.store.put("mandate_config:" + c, json.loads(json.dumps(spec["mandate"])))
                g.store.put("profile:" + c, {**policy.default_profile(), **spec.get("profile", {})})
                g._register(a)
        if not keep:
            g.store.q("delete from holds where world='floor'")
            g.allocation.clear("floor")
            g.store.log(None, "human:Valio Aimo account manager", "control", f"The demo floor starts over: {len(CAST)} simulated customer accounts, fresh stock.", {"accounts": [s["slug"] for s in CAST]})
        changed()
        return {"accounts": len(CAST), "created": made, "reset": not keep}

    @r.post("/api/tower/floor/reset")
    def floor_reset(request: Request, body: dict = Body(default={})):
        """Clear the floor accounts' orders, agents and stock, and seed them again. keep=true only adds what is missing."""
        need_operator(request)
        if running() and not body.get("keep") and body.get("id") != fl["id"]:  # the run that is just starting may reset
            raise GatewayError(409, "busy", "The floor is running. Stop it first.")
        if rush_running():
            raise GatewayError(409, "busy", "The Monday rush is running. Wait for it to finish, or stop it.")
        return seed(bool(body.get("keep")))

    @r.post("/api/tower/floor/mandate")
    def floor_mandate(request: Request, body: dict = Body(...)):
        """Stand-in for a floor customer's own system: signs a mandate for one of its agents, bound to the key the agent
        made for itself (jwk), or without a key for a signed-in assistant (keyless)."""
        need_operator(request)
        a = g.account(str(body.get("slug")))
        if not a or a["kind"] not in ("floor", "rush"):
            raise GatewayError(404, "not_found", "No such floor account.")
        agent = f"agent:{a['slug']}/" + (re.sub(r"[^a-z0-9]+", "-", str(body.get("agent") or "agent").lower()).strip("-")[:32] or "agent")
        name = str(body.get("name") or "")[:60] or None
        m = body.get("mandate") or g.store.get("mandate_config:" + a["customer"])
        ttl = 6 * 3600
        if body.get("keyless"):
            return {"agent": agent, "mandate": g.issue_mandate(a["slug"], constraints=m, ttl=ttl, agent=agent, bound=False, name=name)}
        jwk = body.get("jwk") or {}
        if jwk.get("kty") != "EC" or jwk.get("crv") != "P-256" or not jwk.get("x") or not jwk.get("y"):
            raise GatewayError(400, "bad_request", "jwk must be the agent's public P-256 key.")
        g._register(a)
        t = now()
        claims = {"iss": a["iss"], "sub": agent, "aud": g.audience, "iat": t, "exp": t + ttl, "jti": "mnd_" + secrets.token_hex(6),
                  "cnf": {"jwk": {k: jwk[k] for k in ("kty", "crv", "x", "y", "kid", "alg", "use") if k in jwk}}, **({"agent_name": name} if name else {}), "mandate": m}
        return {"agent": agent, "mandate": jwt_encode(claims, g.keys(a["slug"])["org"], typ="mandate+jwt")}

    @r.post("/api/tower/floor/beat")
    def floor_beat(request: Request, body: dict = Body(...)):
        """The running floor reports in every couple of seconds and learns here whether to stop."""
        need_operator(request)
        rid = str(body.get("id") or "")
        if body.get("start"):
            if running() and rid != fl["id"]:
                return {"stop": True, "busy": True}
            fl.update(id=rid, stop=False, status={})
        elif rid != fl["id"]:
            return {"stop": True, "busy": True}
        fl["seen"] = time.time()
        fl["status"] = {k: body[k] for k in ("round", "rounds", "minutes", "note", "started", "llm", "agents", "summary", "done") if k in body}
        return {"stop": fl["stop"]}

    @r.post("/api/tower/floor/start")
    async def floor_start(request: Request, body: dict = Body(default={})):
        need_operator(request)
        if running():
            raise GatewayError(409, "busy", "The floor is already running.")
        if rush_running():
            raise GatewayError(409, "busy", "The Monday rush is running. Wait for it to finish, or stop it.")
        from agent import floor as engine

        try:
            minutes = max(0.5, min(float(body.get("minutes") or 3), 15))
            llm = max(0, min(int(body.get("llm") or 0), 1))  # the model pool is shared: one model agent at most
        except (TypeError, ValueError):
            raise GatewayError(400, "bad_request", "minutes and llm are numbers.")
        rid = "flr_" + secrets.token_hex(4)
        fl.update(id=rid, stop=False, seen=time.time(), status={"round": 0, "note": "Starting", "minutes": minutes})
        async def go():
            try:
                await engine.run(f"http://127.0.0.1:{PORT}", OPERATOR_KEY, minutes=minutes, llm=llm, seed=body.get("seed"), reset=body.get("reset", True) is not False, run_id=rid, quiet=True)
            except Exception as e:  # a failed run must not take the server with it
                fl["status"] = {**fl["status"], "done": True, "note": f"Stopped: {type(e).__name__}: {e}"[:200]}

        fl["task"] = asyncio.create_task(go())
        changed()
        return {"id": rid, "minutes": minutes, "llm": llm}

    @r.post("/api/tower/floor/stop")
    def floor_stop(request: Request):
        need_operator(request)
        fl["stop"] = True
        changed()
        return {"stopping": running()}

    # ----------------------------------------------------------------- rush
    def rush_running() -> bool:
        return bool(rs["id"]) and not rs["status"].get("done") and time.time() - rs["seen"] < 25

    def rush_customers(world=None) -> list[str]:
        world = world or rush_world()
        return [a["customer"] for a in kv_range("acct:") if a["kind"] == "rush" and a.get("world") == world]

    def rush_warm(world=None) -> dict:
        """One question to Valio Aimo's price and stock API for the products a rush touches, and the answer shared with all
        60 accounts. The challenge's API ignores the customer number and limits how often it may be asked, so 60 accounts asking
        the same thing at the same second would only spend that allowance. The result of every run says this in plain words."""
        from agent.floor import DAY, RUSH_EXTRA, RUSH_PRODUCT

        world = world or rush_world()
        custs = rush_customers(world)
        if not custs:
            return {"customers": 0}
        cat = g.cat.get(RUSH_PRODUCT)["category"]
        ids = list(dict.fromkeys([RUSH_PRODUCT, *RUSH_EXTRA] + [d["id"] for d in g.cat.docs if d["category"] == cat]))
        base, t, calls = custs[0], time.time(), g.aimo.calls
        with g.aimo.lock:
            if t - min((g.aimo.cache.get((i, DAY, base)) or (0.0,))[0] for i in ids) > 20:
                for i in ids:
                    g.aimo.cache.pop((i, DAY, base), None)
        g.aimo.quote(ids, DAY, base)
        with g.aimo.lock:
            for i in ids:
                hit = g.aimo.cache.get((i, DAY, base))
                if hit:
                    for c in custs[1:]:
                        g.aimo.cache[(i, DAY, c)] = hit
        return {"customers": len(custs), "products": len(ids), "asked_valio_aimo": g.aimo.calls - calls}

    def rush_seed(release_s, again_s, world="rush", count=60, models=12) -> dict:
        from agent.floor import rush_cast

        cast = rush_cast(models=models, count=count, prefix="live-rush-" if world == "live-rush" else "rush-",
                         model_choices=None)
        for spec in cast:
            a = g.account(spec["slug"])
            if a and a["kind"] != "rush":
                raise GatewayError(409, "invalid_state", f"{spec['slug']} exists and is not a rush account.")
            if a:
                g.reset(spec["slug"])
                a = g.account(spec["slug"])
                a.update(name=spec["name"], ship_to=spec["ship_to"], world=world, segment=spec["segment"], history_seeded=True)
                g.store.put("acct:" + a["slug"], a)
            else:
                a = g.create_account(spec["slug"], spec["name"], kind="rush", ship_to=spec["ship_to"], history=spec["history"], mandate=spec["mandate"], world=world, segment=spec["segment"], history_seeded=True)
            c = a["customer"]
            g.store.put("history:" + c, json.loads(json.dumps(spec["history"])))
            g.store.put("mandate_config:" + c, json.loads(json.dumps(spec["mandate"])))
            g.store.put("profile:" + c, {**policy.default_profile(), **spec.get("profile", {})})
            g._register(a)
        g.store.q("delete from holds where world=?", (world,))
        g.allocation.clear(world)
        g.allocation.configure(world, release_s, again_s)
        g.store.log(None, "human:Valio Aimo account manager", "control", f"The {'live ' if world == 'live-rush' else ''}Monday rush starts over: {len(cast)} simulated customer accounts with seeded order history, fresh stock.", {"accounts": len(cast), "world": world})
        changed()
        return {"accounts": len(cast), "world": world}

    @r.get("/api/tower/rush/check")
    def rush_check(request: Request):
        """What a run measures itself against, before and after: the stage account's fingerprint, the ledger and the calls to Valio Aimo's API."""
        need_operator(request)
        ok, n, bad = g.store.verify_chain()
        return {"stage": stage_print(g), "ledger": {"intact": ok, "events": n, "first_bad_seq": bad}, "aimo_calls": g.aimo.calls}

    @r.post("/api/tower/rush/reset")
    def rush_reset(request: Request, body: dict = Body(default={})):
        """Seed the recorded or small live rush accounts again, and say when unclaimed shares are released."""
        need_operator(request)
        if rush_running() and body.get("id") != rs["id"]:
            raise GatewayError(409, "busy", "The Monday rush is running. Stop it first.")
        if running():
            raise GatewayError(409, "busy", "The floor is running. Stop it first.")
        try:
            world = str(body.get("world") or "rush")
            if world not in ("rush", "live-rush"):
                raise ValueError
            floor = 2.0 if world == "live-rush" else 5.0
            release_s, again_s = max(floor, min(float(body.get("release_s") or 40), 3600)), max(floor, min(float(body.get("again_s") or 15), 3600))
            count = LIVE_COUNT if world == "live-rush" else max(1, min(int(body.get("count") or 60), 60))
            models = LIVE_MODELS if world == "live-rush" else max(0, min(int(body.get("models") or 12), 12))
        except (TypeError, ValueError):
            raise GatewayError(400, "bad_request", "release_s and again_s are seconds; world, count and models are valid rush settings.")
        return {**rush_seed(release_s, again_s, world, count, models), "warm": rush_warm(world)}

    @r.post("/api/tower/rush/warm")
    def rush_warm_route(request: Request):
        need_operator(request)
        return rush_warm()

    @r.post("/api/tower/rush/beat")
    def rush_beat(request: Request, body: dict = Body(...)):
        """The running rush reports what its agents saw (events for the screen) and learns here whether to stop."""
        need_operator(request)
        rid = str(body.get("id") or "")
        if body.get("start"):
            if (rush_running() and rid != rs["id"]) or running():
                return {"stop": True, "busy": True}
            rs.update(id=rid, stop=False, status={}, events=[], cast=[], world=str(body.get("world") or "rush"),
                      live=bool(body.get("live")), ledger_refs=[])
        elif rid != rs["id"]:
            return {"stop": True, "busy": True}
        rs["seen"] = time.time()
        if isinstance(body.get("cast"), list):
            rs["cast"] = body["cast"][:80]
        if isinstance(body.get("events"), list):
            rs["events"] += body["events"][:2000]
        rs["status"] = {**rs["status"], **{k: body[k] for k in ("note", "started", "mix", "summary", "done", "t0") if k in body}}
        if isinstance(body.get("ledger_refs"), list):
            rs["ledger_refs"] = [str(x) for x in body["ledger_refs"][:LIVE_COUNT]]
        if body.get("done") and rs.get("live"):
            rs["cool_until"] = time.time() + LIVE_COOLDOWN
            rs["status"]["cooldown_s"] = LIVE_COOLDOWN
        changed()
        return {"stop": rs["stop"], "events": len(rs["events"])}

    @r.get("/api/tower/rush")
    def rush_view(since: int = 0):
        """The live rush for the screen: who the agents are, what each has seen so far, and the split of the short stock."""
        al = g.allocation.view(rush_world())
        return {"running": rush_running(), "stopping": rush_running() and rs["stop"], "id": rs["id"], "status": rs["status"], "cast": rs["cast"] if since <= 0 else None,
                "live": bool(rs.get("live")), "cooldown_s": max(0, round(rs.get("cool_until", 0) - time.time(), 1)),
                "events": rs["events"][max(0, since):], "n": len(rs["events"]), "allocation": al[0] if al else None, "now": time.time()}

    @r.post("/api/tower/rush/start")
    async def rush_start(request: Request, body: dict = Body(default={})):
        if not looks_like_operator(request):
            return await rush_live_start(request)
        need_operator(request)
        if rush_running() or running():
            raise GatewayError(409, "busy", "A run is already going. Wait for it to finish, or stop it.")
        from agent import floor as engine

        try:
            models = max(0, min(int(body.get("models") if body.get("models") is not None else 12), 24))
        except (TypeError, ValueError):
            raise GatewayError(400, "bad_request", "models is a number.")
        rid = engine.rush_id()
        rs.update(id=rid, stop=False, seen=time.time(), status={"note": "Starting"}, events=[], cast=[], world="rush", live=False, cool_until=0.0, ledger_refs=[])

        async def go():
            try:
                await engine.rush(f"http://127.0.0.1:{PORT}", OPERATOR_KEY, seed=body.get("seed"), models=models, run_id=rid, quiet=True)
            except Exception as e:  # a failed run must not take the server with it
                rs["status"] = {**rs["status"], "done": True, "note": f"Stopped: {type(e).__name__}: {e}"[:200]}

        rs["task"] = asyncio.create_task(go())
        changed()
        return {"id": rid, "models": models}

    @r.post("/api/tower/rush/live/start")
    async def rush_live_start(request: Request):
        """A visitor may start or join one small live rush. It has its own accounts and stock world."""
        if running():
            raise GatewayError(409, "busy", "The floor is running. The live rush will be available when it finishes.")
        if rush_running():
            if rs.get("live"):
                return {"id": rs["id"], "joined": True, "agents": len(rs.get("cast") or []) or LIVE_COUNT}
            raise GatewayError(409, "busy", "Valio Aimo's recorded rush is running. Wait for it to finish.")
        wait = max(0.0, rs.get("cool_until", 0) - time.time())
        if wait:
            raise GatewayError(429, "cooldown", f"The last live rush finished. A new one can start in {math.ceil(wait)} seconds.", retry_after=math.ceil(wait))
        from agent import floor as engine

        rid = engine.rush_id()
        rs.update(id=rid, stop=False, seen=time.time(), status={"note": "Setting up the 12-agent live rush", "live": True}, events=[], cast=[],
                  world="live-rush", live=True, cool_until=0.0, ledger_refs=[])

        async def go():
            try:
                out = await engine.rush(f"http://127.0.0.1:{PORT}", OPERATOR_KEY, models=LIVE_MODELS, run_id=rid, quiet=True,
                                        release_s=3, again_s=2, world="live-rush", count=LIVE_COUNT, live=True)
                rs["ledger_refs"] = out.get("ledger_refs") or []
            except asyncio.CancelledError:
                raise
            except Exception as e:
                rs["status"] = {**rs["status"], "done": True, "note": f"Stopped: {type(e).__name__}: {e}"[:200], "summary": {"live": True, "errors": 1}}
                rs["cool_until"] = time.time() + LIVE_COOLDOWN
            finally:
                rs["seen"] = time.time()
                changed()

        rs["task"] = asyncio.create_task(go())
        changed()
        return {"id": rid, "joined": False, "agents": LIVE_COUNT, "model_agents": LIVE_MODELS}

    @r.get("/api/tower/rush/live/ledger/{rid}")
    def rush_live_ledger(rid: str):
        """The synthetic live run's own ledger entries, linked from its result."""
        if rid != rs.get("id") or not rs.get("live"):
            raise GatewayError(404, "not_found", "That live rush is no longer available on this server.")
        refs = set(rs.get("ledger_refs") or [])
        rows = []
        for ref in refs:
            rows.extend(g.store.events(ref=ref, limit=5000))
        rows.sort(key=lambda e: e["seq"])
        return {"run": rid, "entries": rows, "count": len(rows), "chain": g.store.verify_chain()[0], "url": f"/api/tower/rush/live/ledger/{rid}"}

    @r.post("/api/tower/rush/stop")
    def rush_stop(request: Request):
        need_operator(request)
        rs["stop"] = True
        changed()
        return {"stopping": rush_running()}

    @r.post("/api/tower/rush/save")
    def rush_save(request: Request, body: dict = Body(...)):
        """Keep the measured result of a run: one file per run, and the latest one for the pages."""
        need_operator(request)
        rid = str(body.get("id") or "")
        if not re.fullmatch(r"rush_\d{8}T\d{6}Z", rid):
            raise GatewayError(400, "bad_request", "Not a rush result.")
        (evals / "rush").mkdir(parents=True, exist_ok=True)
        text = json.dumps(body, ensure_ascii=False)
        filename = f"{rid}{'-live' if body.get('live') else ''}.json"
        (evals / "rush" / filename).write_text(text, encoding="utf-8")
        if body.get("complete") and not body.get("live"):  # live runs never replace the recorded 60-agent replay
            (evals / "rush-latest.json").write_text(text, encoding="utf-8")
        latest["t"] = 0.0
        return {"saved": f"evals/rush/{filename}", "latest": bool(body.get("complete") and not body.get("live"))}

    latest = {"t": 0.0, "v": None, "m": 0.0}

    @r.get("/api/rush/latest")
    def rush_latest(events: int = 1):
        """The last complete Monday rush, as that run measured itself. events=0 leaves out the per-agent rows and the event list."""
        f = evals / "rush-latest.json"
        try:
            m = f.stat().st_mtime
            if latest["v"] is None or m != latest["m"]:
                latest.update(v=json.loads(f.read_text(encoding="utf-8")), m=m)
        except (OSError, ValueError):
            return {"ready": False, "rerun": "uv run -q python -m agent.floor --rush --base http://127.0.0.1:8642"}
        v = latest["v"]
        return {"ready": True, **(v if events else {k: x for k, x in v.items() if k not in ("events", "agents", "split")})}

    @r.post("/api/tower/allocation")
    def set_allocation(request: Request, body: dict = Body(...)):
        """Valio Aimo's own switch: put a product on allocation for a delivery day by hand, or take it off."""
        need_operator(request)
        from agent.floor import DAY

        try:
            release = float(body["release_in"]) if body.get("release_in") else None
        except (TypeError, ValueError):
            raise GatewayError(400, "bad_request", "release_in is seconds.")
        a = g.allocation.switch(str(body.get("world") or "floor"), str(body.get("day") or DAY), str(body.get("product") or ""), bool(body.get("on", True)), release)
        changed()
        return {k: a.get(k) for k in ("world", "day", "product", "title", "on", "mode", "stock", "expected")}

    return r
