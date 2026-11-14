"""Fair allocation of short stock. Rule L1, decided in code like every other rule.

When the stock of a product for a delivery day is under the demand Valio Aimo expects from order history,
the product goes "on allocation" for that day: automatically the first time anyone asks for it, or by hand
from the tower. From then on each customer can take its contract share and no more:

* the share is the customer's usual order of that product against the short stock, the same fraction for all;
* care kitchens (hospitals, schools, elderly care) are guaranteed a minimum part of their usual order first;
* an agent that asks for more gets a signed offer for its share, with the reason in plain words and the ways
  out: a substitute for the rest, the rest later (a waiting list), or the share alone;
* at a stated time the shares nobody claimed are released to the customers still waiting, again by contract
  share. What is left after that is open to whoever asks first.

Confidentiality: an agent is only ever told its own numbers ("46 of your 80 L"). The total stock, the other
customers and what they asked for are visible to Valio Aimo alone (the tower).

Stock that is promised is a row in the holds table, as everywhere else in the gateway. This module adds no
second book: it only decides how much of the short product one customer may still hold.
"""
import json
import math
import os
import threading
import time
from datetime import datetime

from .catalog import UNITS
from .config import TZ
from .service import GatewayError, nice_day

CARE = {"hospital": "hospital kitchen", "school": "school kitchen", "elderly_care": "elderly care kitchen"}
CARE_MIN_PCT = 60        # the part of its usual order a care kitchen is guaranteed
RELEASE_S = float(os.environ.get("AIMO_ALLOC_RELEASE_S", "900"))  # unclaimed shares are released this long after allocation starts
AGAIN_S = float(os.environ.get("AIMO_ALLOC_AGAIN_S", "300"))      # and again this often, while anything is unclaimed
MAX_ROUNDS = 6
RULE = "L1"
TITLE = "Short stock is split by contract share"


def split(stock, weight, cap=None, floor=None, step=1.0) -> dict:
    """Share `stock` between customers. Pure and deterministic: the same inputs give the same split.

    weight: {customer: usual units}   the contract weight
    cap:    {customer: units}         the most each may get (default: its usual order)
    floor:  {customer: units}         guaranteed minimum (care kitchens)
    Every customer gets clamp(k * weight, floor, cap) for the one k that uses the stock up, rounded to whole
    pack steps: down first, then the steps left over go to the largest remainders, ties by customer id."""
    ids = sorted(weight)
    whole = lambda x: math.floor(x / step + 1e-9)
    capn = {c: whole(max(0.0, float((weight if cap is None else cap).get(c, 0)))) for c in ids}
    total = min(whole(max(0.0, stock)), sum(capn.values()))
    if total <= 0:
        return {c: 0.0 for c in ids}
    w = {c: float(weight[c]) if weight[c] > 0 else float(capn[c]) for c in ids}
    fl = {c: min(capn[c], math.ceil(max(0.0, float((floor or {}).get(c, 0))) / step - 1e-9)) for c in ids}
    if sum(fl.values()) > total:  # not even the guaranteed minimums fit: they shrink together and nobody else gets anything
        w, capn, fl = {c: float(fl[c]) for c in ids}, dict(fl), {c: 0 for c in ids}
    at = lambda k: {c: min(capn[c], max(fl[c], k * w[c])) for c in ids}
    lo, hi = 0.0, max((capn[c] / w[c] for c in ids if w[c] > 0), default=0.0)
    for _ in range(80):
        mid = (lo + hi) / 2
        if sum(at(mid).values()) < total:
            lo = mid
        else:
            hi = mid
    x = at(hi)
    got = {c: min(capn[c], math.floor(x[c] + 1e-9)) for c in ids}
    left = total - sum(got.values())
    order = sorted(ids, key=lambda c: (-(x[c] - got[c]), c))
    while left > 0:
        moved = False
        for c in order:
            if left > 0 and got[c] < capn[c]:
                got[c] += 1
                left -= 1
                moved = True
        if not moved:
            break
    return {c: got[c] * step for c in ids}


def unit_word(d) -> str:
    return "L" if (d.get("content") or {}).get("l") == 1 else UNITS.get(d["unit"], (0, d["unit"]))[1]


def _iso(t) -> str:
    return datetime.fromtimestamp(t, TZ).isoformat(timespec="seconds")


def _clock(t) -> str:
    return datetime.fromtimestamp(t, TZ).strftime("%H:%M:%S")


class Allocation:
    def __init__(self, g):
        self.g, self.store = g, g.store
        self.lock = threading.RLock()
        self.tl = threading.local()             # the checkout being assembled on this thread
        self.live: dict[tuple, dict] = {}       # (world, day, product) -> state
        self.checked: dict[tuple, float] = {}   # (world, day, product) -> when it was last found not short
        self.refs: dict[str, str] = {}          # checkout id -> customer
        self._worlds = (0.0, {})
        self.dirty: dict[tuple, float] = {}     # state changed in memory and not yet written down
        for (v,) in self.store.q("select v from kv where k like 'alloc:%'"):
            a = json.loads(v)
            self.live[(a["world"], a["day"], a["product"])] = a

    # ---------------------------------------------------------------- inputs
    def demand(self, world) -> dict:
        """What Valio Aimo expects each customer of one stock pool to order: {product: {customer: {...}}}, from order history."""
        t, worlds = self._worlds
        if time.time() - t > 15:  # read once in a while, not on every look-up
            worlds = {}
            for a in self.g.accounts():
                out = worlds.setdefault(a.get("world", "main"), {})
                for pid, h in (self.store.get("history:" + a["customer"]) or {}).items():
                    if (h.get("typical_units") or 0) > 0:
                        out.setdefault(pid, {})[a["customer"]] = {"usual": float(h["typical_units"]), "care": a.get("segment") if a.get("segment") in CARE else None,
                                                                  "name": a["name"], "slug": a["slug"], "segment": a.get("segment")}
            self._worlds = (time.time(), worlds)
        return worlds.get(world, {})

    def _source(self, pid, day, customer, ask=False):
        """The stock Valio Aimo's own system reports, before anything the gateway has promised."""
        with self.g.aimo.lock:
            hit = self.g.aimo.cache.get((pid, day, customer))
        rec = hit[1] if hit else (self.g.aimo.quote([pid], day, customer).get(pid) if ask else None)
        lines = ((rec or {}).get("avail") or {}).get("scheduleLines") or []
        return float(lines[0].get("availableAmount") or 0) if lines else None

    def _taken(self, a) -> dict:
        """{customer: {checkout: units}} of this product promised right now: ordered, or held by a signed offer."""
        rows = self.store.q("select ref, sum(qty), sum(case when state='ordered' then qty else 0 end) from holds where product=? and world=? and (state='ordered' or (state='held' and expires>?)) group by ref",
                            (a["product"], a["world"], time.time()))
        out = {}
        for ref, qty, ordered in rows:
            c = self.refs.get(ref)
            if c is None:
                r = self.store.q("select customer from checkouts where id=?", (ref,))
                c = (r[0][0] if r else None) or ""
                if r:
                    self.refs[ref] = c
            out.setdefault(c, {})[ref] = (qty or 0.0, ordered or 0.0)
        return out

    def _save(self, a, soon=False):
        """Write the state down. soon: within a couple of seconds, with the next look, so a burst of requests is one write and not sixty."""
        key = (a["world"], a["day"], a["product"])
        if soon:
            self.dirty.setdefault(key, time.time())
            return
        self.dirty.pop(key, None)
        self.store.put(f"alloc:{a['world']}:{a['day']}:{a['product']}", a)

    def _hold(self, ref, pid, units, world, again):
        """Promise units to one checkout for the moment it takes to assemble its offer; the offer's own hold replaces this."""
        if not again and self.store.q("select 1 from holds where ref=? and product=? and state='held' limit 1", (ref, pid)):
            self.store.q("update holds set state='released' where ref=? and product=? and state='held'", (ref, pid))
        self.store.q("insert into holds(ref,product,qty,expires,state,world) values(?,?,?,?,'held',?)", (ref, pid, units, time.time() + 15, world))

    def configure(self, world, release_s=None, again_s=None):
        """How long after the start unclaimed shares are released in one stock pool (the tower sets this for a rush)."""
        self.store.put("alloc_cfg:" + world, {"release_s": float(release_s or RELEASE_S), "again_s": float(again_s or AGAIN_S)})

    def clear(self, world):
        with self.lock:
            for key in [k for k in self.live if k[0] == world]:
                del self.live[key]
            for key in [k for k in self.checked if k[0] == world]:
                del self.checked[key]
            self.store.q("delete from kv where k like ?", (f"alloc:{world}:%",))
            for key in [k for k in self.dirty if k[0] == world]:
                del self.dirty[key]
            self._worlds = (0.0, {})

    # ------------------------------------------------------------ on and off
    def _start(self, world, day, pid, stock, dem, mode, release_s=None, by=None):
        d = self.g.cat.get(pid)
        step = d["increment"] / 10 ** d["scale"]
        cfg = self.store.get("alloc_cfg:" + world) or {}
        release_s = float(release_s or cfg.get("release_s") or RELEASE_S)
        t = time.time()
        a = {"world": world, "day": day, "product": pid, "title": d["title"], "unit": unit_word(d), "step": step, "stock": stock, "on": True, "mode": mode,
             "since": _iso(t), "release_at": t + release_s, "again_s": float(cfg.get("again_s") or AGAIN_S), "rounds": 0, "care_min_pct": CARE_MIN_PCT,
             "expected": sum(v["usual"] for v in dem.values()), "basis": "seeded order history", "shares": {}, "log": []}
        taken = {c: sum(q for q, _ in refs.values()) for c, refs in self._taken(a).items()}
        floors = {c: math.ceil(CARE_MIN_PCT / 100 * v["usual"] / step - 1e-9) * step for c, v in dem.items() if v["care"]}
        x = split(stock - sum(taken.values()), {c: v["usual"] for c, v in dem.items()}, cap={c: max(0.0, v["usual"] - taken.get(c, 0)) for c, v in dem.items()},
                  floor={c: max(0.0, f - taken.get(c, 0)) for c, f in floors.items()}, step=step)
        for c, v in dem.items():
            share = taken.get(c, 0) + x[c]
            a["shares"][c] = {"name": v["name"], "slug": v["slug"], "segment": v.get("segment"), "care": v["care"], "usual": v["usual"], "floor": floors.get(c, 0.0),
                              "share": share, "grant": share, "asked": 0.0, "extra": 0.0}
        self.live[(world, day, pid)] = a
        self._save(a)
        who = f"{by} put" if by else "Went"
        self.store.log(None, f"human:{by}" if by else "gateway", "control",
                       f"{who} {d['title']} on allocation for {nice_day(day)}{'' if by else ': its stock is under the demand expected from order history'}. "
                       f"Each customer can take its contract share; care kitchens are guaranteed {CARE_MIN_PCT}% of their usual order. Unclaimed shares are released at {_clock(a['release_at'])}.",
                       {"rule": RULE, "product": pid, "day": day, "mode": mode})
        return a

    def _state(self, world, day, pid, customer):
        key = (world, day, pid)
        a = self.live.get(key)
        if a:
            return a if a["on"] else None
        if time.time() - self.checked.get(key, 0) < 10:
            return None
        with self.lock:
            a = self.live.get(key)
            if a:
                return a if a["on"] else None
            if time.time() - self.checked.get(key, 0) < 10:
                return None
            dem = self.demand(world).get(pid) or {}
            stock = self._source(pid, day, customer) if len(dem) >= 2 else None
            if stock is None or sum(v["usual"] for v in dem.values()) <= stock:
                self.checked[key] = time.time()
                return None
            return self._start(world, day, pid, stock, dem, "auto")

    def switch(self, world, day, pid, on: bool, release_s=None, by="Valio Aimo account manager") -> dict:
        """Valio Aimo's own switch: put a product on allocation for a day by hand, or take it off."""
        d = self.g.cat.get(pid)
        if not d:
            raise GatewayError(404, "not_found", "No such product.")
        with self.lock:
            key = (world, day, pid)
            a = self.live.get(key)
            if not on:
                if a and a["on"]:
                    a.update(on=False, mode="off by hand")
                    self._save(a)
                    self.store.log(None, f"human:{by}", "control", f"{by} took {d['title']} off allocation for {nice_day(day)}. What is left is open to whoever asks first.", {"rule": RULE, "product": pid, "day": day})
                elif not a:  # remember the decision, so the automatic rule does not switch it straight back on
                    a = {"world": world, "day": day, "product": pid, "title": d["title"], "unit": unit_word(d), "on": False, "mode": "off by hand", "shares": {}, "log": [], "stock": 0, "rounds": 0}
                    self.live[key] = a
                    self._save(a)
                return a
            if a and a["on"]:
                return a
            dem = self.demand(world).get(pid) or {}
            first = next(iter(dem), None)
            stock = self._source(pid, day, first, ask=True) if first else None
            if stock is None or stock >= 999_999:
                raise GatewayError(409, "invalid_state", "This product has no tracked stock for that day, or no customer has ordered it before, so there is nothing to split.")
            return self._start(world, day, pid, stock, dem, "by hand", release_s, by)

    # --------------------------------------------------------------- release
    def _tick(self, a):
        while a["on"] and a.get("release_at") and time.time() >= a["release_at"] and a["rounds"] < MAX_ROUNDS:
            self._release(a)

    def _release(self, a):
        """The stated time has come: what was not claimed goes to the customers still waiting, by contract share."""
        step = a["step"]
        taken = {c: sum(q for q, _ in refs.values()) for c, refs in self._taken(a).items()}
        freed = 0.0
        for c, sh in a["shares"].items():
            t = taken.get(c, 0.0)
            if sh["grant"] > t:
                freed += sh["grant"] - t
                sh["grant"] = t
        pool = max(0.0, a["stock"] - sum(taken.values()))
        want = {c: sh["later"] - taken.get(c, 0.0) for c, sh in a["shares"].items() if sh.get("later", 0.0) - taken.get(c, 0.0) >= step}  # the waiting list
        extra = split(pool, {c: a["shares"][c]["usual"] for c in want}, cap=want, step=step) if want and pool >= step else {}
        for c, x in extra.items():
            if x > 0:
                a["shares"][c]["grant"] = taken.get(c, 0.0) + x
                a["shares"][c]["extra"] += x
        a["rounds"] += 1
        given = sum(extra.values())
        t = time.time()
        a["log"].append({"at": _iso(t), "round": a["rounds"], "unclaimed": round(freed, 2), "pool": round(pool, 2), "waiting": len(want), "released": round(given, 2), "to": {c: x for c, x in extra.items() if x > 0}})
        a["release_at"] = t + a["again_s"] if a["rounds"] < MAX_ROUNDS else None
        self._save(a)
        if given > 0 or a["rounds"] == 1:
            self.store.log(None, "gateway", "control",
                           (f"Released unclaimed shares of {a['title']} for {nice_day(a['day'])}: {given:g} {a['unit']} went to the customers still waiting, by contract share." if given > 0
                            else f"Release time for {a['title']} for {nice_day(a['day'])}: no customer is waiting for more, so what is unclaimed is open to whoever asks first."),
                           {"rule": RULE, "product": a["product"], "day": a["day"], "round": a["rounds"]})

    # ------------------------------------------------------------- the hooks
    def _remaining(self, a, customer, ref="") -> float:
        """What this customer may still take, not counting the checkout it is working on."""
        tk = self._taken(a)
        t = lambda c: sum(q for r, (q, _) in tk.get(c, {}).items() if r != ref)
        sh = a["shares"].get(customer)
        rem = max(0.0, (sh["grant"] if sh else 0.0) - t(customer))
        if a["rounds"] > 0:  # after the release, what nobody holds a share of is open
            promised = sum(max(s["grant"], t(c)) for c, s in a["shares"].items()) + sum(t(c) for c in tk if c not in a["shares"])
            rem += max(0.0, a["stock"] - promised)
        return rem

    def cap(self, live, world, day, customer, ref="") -> dict:
        """Called with the live facts of every look-up: for a product on allocation, a customer sees its own share, not the stock."""
        if ref:
            self.tl.asm = (ref, {})
        if not customer or customer == "public":
            return live
        for pid, f in live.items():
            if not f.get("stock_known"):
                continue
            a = self._state(world, day, pid, customer)
            if not a:
                continue
            with self.lock:
                self._tick(a)
                if time.time() - self.dirty.get((world, day, pid), 1e18) > 2:
                    self._save(a)
                rem = self._remaining(a, customer, ref)
            rem = math.floor(min(rem, f["available"]) / a["step"] + 1e-9) * a["step"]
            f["allocation"] = {"key": (world, day, pid), "remaining": rem}
            f["available"] = rem
            if rem <= 0 and f["orderable"]:
                f.update(orderable=False, reason="allocation_share_used")
        return live

    def _facts(self, a, customer, ref) -> dict:
        sh = a["shares"].get(customer) or {"grant": 0.0, "share": 0.0, "extra": 0.0, "care": None, "usual": 0.0}
        mine = sum(q for r, (q, _) in self._taken(a).get(customer, {}).items() if r != ref)
        return {"share": sh["share"], "extra": sh["extra"], "care": sh["care"], "usual": sh["usual"], "taken": mine, "release_at": a.get("release_at"), "rounds": a["rounds"]}

    def trim(self, it, d, f, qty, units, day, principal, ref, err):
        """The one place a line's quantity is cut: an agent asking for more than its share gets its share, as a signed partial offer.
        Returns (quantity, units); units 0 means the line was refused here and an error was added."""
        al = f.get("allocation")
        if not al:
            self._promise(d, f, qty, units, day, principal, ref)
            return qty, units
        if not principal or not principal.get("ok") or (not f["orderable"] and f.get("reason") != "allocation_share_used"):
            return qty, units
        a, customer = self.live[al["key"]], principal["customer"]
        step, pid = a["step"], d["id"]
        w = it.get("wholesale") or {}
        asked = max(units, (w.get("allocation") or {}).get("requested") or 0)
        asm = getattr(self.tl, "asm", None)
        took = asm[1] if ref and asm and asm[0] == ref else {}
        with self.lock:
            rem = (self._remaining(a, customer, ref) if ref else al["remaining"]) - took.get(pid, 0.0)
            rem = math.floor(max(0.0, rem) / step + 1e-9) * step
            fx = self._facts(a, customer, ref)
            if ref and (asked > min(units, rem) or w.get("rest") == "later"):
                sh = a["shares"].setdefault(customer, {"name": principal.get("buyer_name"), "slug": principal.get("slug"), "segment": None, "care": None, "usual": 0.0, "floor": 0.0,
                                                       "share": 0.0, "grant": 0.0, "asked": 0.0, "extra": 0.0})
                want = max(sh["asked"], fx["taken"] + asked)
                if want > sh["asked"] or (w.get("rest") == "later" and want > sh.get("later", 0)):
                    sh["asked"] = want
                    if w.get("rest") == "later":  # the agent took the way out "the rest later": it is on the waiting list for the rest of the most it asked for
                        sh["later"] = want
                    self._save(a, soon=True)
            ok = rem >= step and not qty % d["increment"]
            if ok:
                if units > rem:
                    units = rem
                    qty = round(units * 10 ** d["scale"])
                    it["quantity"] = qty
                if ref:  # promise it now, under the lock, so two requests at the same moment cannot both take the same litres
                    self.refs[ref] = customer
                    self._hold(ref, pid, units, a["world"], bool(took.get(pid)))
                    took[pid] = took.get(pid, 0.0) + units
        if rem < step:
            mine = fx["share"] + fx["extra"]
            when = f" Unclaimed shares are released at {_clock(fx['release_at'])}; ask again then." if fx["release_at"] and fx["rounds"] < MAX_ROUNDS else ""
            err("insufficient_stock", f"{d['title']} is on allocation for {nice_day(day)} because stock is short, and "
                + (f"your contract share of {mine:g} {a['unit']} is already taken." if mine > 0 else "this account has no contract share of it yet.") + when,
                can_supply=0, requested=units, allocation={"on_allocation": True, "your_share": mine, "already_taken": fx["taken"],
                                                            **({"unclaimed_released_at": _iso(fx["release_at"])} if fx["release_at"] and fx["rounds"] < MAX_ROUNDS else {})},
                alternatives=self.g.substitutes(pid, day, principal, 3))
            return qty, 0
        if not ok:
            return qty, units  # a wrong pack step is reported by the gateway's own check
        f["available"] = max(f["available"], units)
        f["allocation"].update(requested=asked, offered=units, facts=fx)
        return qty, units

    def _promise(self, d, f, qty, units, day, principal, ref):
        """Tracked stock that is not on allocation (a substitute many agents reach for at once): first come, first served, but checked
        and promised in one step, so the same packs are never in two signed offers. If it no longer fits, the gateway's own stock check answers."""
        if not ref or not principal or not principal.get("ok") or not f.get("stock_known") or not f["orderable"] or units > f["available"] or qty % d["increment"]:
            return
        pid, world = d["id"], principal.get("world", "main")
        asm = getattr(self.tl, "asm", None)
        took = asm[1] if asm and asm[0] == ref else {}
        with self.lock:
            src = self._source(pid, day, principal["customer"])
            if src is None:
                return
            free = min(f["available"], src - self.store.reserved(pid, ref, world)) - took.get(pid, 0.0)
            if units > free:
                f["available"] = max(0.0, free)
                return
            self._hold(ref, pid, units, world, bool(took.get(pid)))
            took[pid] = took.get(pid, 0.0) + units

    def mark(self, pl, out, it, f, day, principal, ref):
        """Write the allocation onto the line: for the rule engine (L1), for the signed offer and for the agent."""
        al = f.get("allocation")
        if not al or "offered" not in al:
            return
        a, fx = self.live[al["key"]], al["facts"]
        asked, offered, u, title = al["requested"], al["offered"], a["unit"], a["title"]
        rest = max(0.0, asked - offered)
        old = (it.get("wholesale") or {}).get("allocation")
        if old and old.get("offered") == offered and old.get("requested") == asked:
            info = old  # the same offer assembled again (at commit): the same words
        else:
            mine = fx["share"] + fx["extra"]
            basis = ("your contract share" if fx["extra"] <= 0 else f"your contract share plus {fx['extra']:g} {u} released from shares others did not claim" if fx["taken"] < fx["share"]
                     else f"released to you from shares others did not claim, on top of your contract share of {fx['share']:g} {u}")
            care = f" It includes the guaranteed minimum for a {CARE[fx['care']]}: {CARE_MIN_PCT}% of your usual order." if fx["care"] else ""
            when = f" Unclaimed shares are released at {_clock(fx['release_at'])}." if fx["release_at"] and fx["rounds"] < MAX_ROUNDS else ""
            if rest > 0:
                reason = (f"{offered:g} of your {asked:g} {u}: {basis} of {title} for {nice_day(day)}, which is on allocation because stock is short.{care} "
                          f"The other {rest:g} {u} are not confirmed.{when}")
            else:
                reason = f"{offered:g} {u} of {title} is inside {basis} for {nice_day(day)} ({mine:g} {u}). The product is on allocation because stock is short.{care}"
            info = {"on_allocation": True, "requested": asked, "offered": offered, "rest": rest, "your_share": mine, "already_taken": fx["taken"], "care_minimum": bool(fx["care"]), "reason": reason,
                    **({"unclaimed_released_at": _iso(fx["release_at"])} if fx["release_at"] and fx["rounds"] < MAX_ROUNDS else {})}
            if rest > 0 and ref:
                info["ways_out"] = self._ways(a, pl["id"], rest, day, principal, info.get("unclaimed_released_at"), out["quantity"])
            it.setdefault("wholesale", {})["allocation"] = info
        pl["allocation"] = info
        out["wholesale"]["allocation"] = info

    def _ways(self, a, pid, rest, day, principal, release, offered_qty) -> list[dict]:
        """The usual ways out for the part that is not confirmed, each already judged under this agent's mandate."""
        od, ways, u = self.g.cat.get(pid), [], a["unit"]
        per = (od.get("content") or {}).get("l") or (od.get("content") or {}).get("kg")
        for s in self.g.substitutes(pid, day, principal, 6):
            if sum(1 for x in ways if x["type"] == "substitute") >= 3:
                break
            sd = self.g.cat.get(s["id"])
            sper = (sd.get("content") or {}).get("l" if (od.get("content") or {}).get("l") else "kg")
            step = sd["increment"] / 10 ** sd["scale"]
            units = math.ceil(rest * (per / sper if per and sper else 1.0) / step - 1e-9) * step
            line = {"item": {"id": s["id"]}, "quantity": round(units * 10 ** sd["scale"]), "wholesale": {"substitutes_for": pid}}
            pre = self.g._preview([dict(line)], day, principal, {})
            if pre["decision"] == "invalid":  # not enough of it for the rest, or not orderable for that day: not a way out
                continue
            ways.append({"type": "substitute", "id": s["id"], "title": s["title"], "tier": s["tier"], "units": units, "price_delta_pct": s["price_delta_pct"], "authority": pre["decision"],
                         "reasons": pre["reasons"], "line_item": line, "content": f"Add {units:g} x {s['title']} for the other {rest:g} {u}. {s['why']}"})
        if release:
            ways.append({"type": "rest_later", "ask_again_at": release, "line_item": {"item": {"id": pid}, "quantity": offered_qty, "wholesale": {"rest": "later"}},
                         "content": f"Order your share with \"rest\": \"later\" on the line. That puts you on the waiting list for the other {rest:g} {u}: ask again after {release[11:19]}, "
                                    "when unclaimed shares are released to the waiting customers by contract share. It is not guaranteed."})
        ways.append({"type": "share_only", "content": f"Order your share and leave the other {rest:g} {u}."})
        return ways

    # ------------------------------------------------------------- the tower
    def view(self, world=None) -> list[dict]:
        """The whole picture. For Valio Aimo only: never part of anything an agent is sent."""
        out = []
        with self.lock:
            for a in list(self.live.values()):
                if (world and a["world"] != world) or not a.get("shares"):
                    continue
                if a["on"]:
                    self._tick(a)
                if (a["world"], a["day"], a["product"]) in self.dirty:
                    self._save(a)
                tk = self._taken(a)
                rows = []
                for c, sh in a["shares"].items():
                    refs = tk.get(c, {})
                    rows.append({"customer": c, "slug": sh.get("slug"), "name": sh.get("name"), "segment": sh.get("segment"), "care": bool(sh["care"]), "usual": sh["usual"], "floor": sh["floor"],
                                 "share": sh["share"], "grant": sh["grant"], "extra": sh["extra"], "asked": sh["asked"], "waiting": max(0.0, sh.get("later", 0.0) - sum(q for q, _ in refs.values())),
                                 "ordered": sum(o for _, o in refs.values()), "held": sum(q - o for q, o in refs.values())})
                rows.sort(key=lambda r: (-r["usual"], r["name"] or ""))
                ordered, held = sum(r["ordered"] for r in rows), sum(r["held"] for r in rows)
                other = sum(q for c, refs in tk.items() if c not in a["shares"] for q, _ in refs.values())
                out.append({"world": a["world"], "day": a["day"], "product": a["product"], "title": a["title"], "unit": a["unit"], "on": a["on"], "mode": a["mode"], "since": a.get("since"),
                            "stock": a["stock"], "expected": a.get("expected"), "basis": a.get("basis"), "care_min_pct": a.get("care_min_pct", CARE_MIN_PCT),
                            "release_at": _iso(a["release_at"]) if a.get("release_at") and a["rounds"] < MAX_ROUNDS else None, "rounds": a["rounds"], "releases": a["log"],
                            "ordered": ordered, "held": held, "other": other, "left": max(0.0, a["stock"] - ordered - held - other),
                            "shares_total": sum(r["share"] for r in rows), "asked_total": sum(max(r["asked"], r["ordered"] + r["held"]) for r in rows), "customers": rows})
        return out
