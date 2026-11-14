"""After the order: short picks, delays and recalls.

Valio Aimo raises an issue on a placed order. The issue is a notice signed by Valio Aimo that says what happened and
lists the ways out, computed here in code the same way plan() computes sourcing options. The customer's agent picks
one. Every choice becomes a checkout that amends the order, so the price and stock hold, the rules, the approval
link, the agent's signature and complete_checkout work exactly as they do for an order:

* a way out with new goods (a substitute) ends as a linked order plus a signed amendment on the original order
* a way out with no new goods (take what there is, accept the new day, cancel, credit) ends as a signed amendment

The original confirmation is never touched. An order's current state is its confirmation plus its amendments.

Lots do not exist in the challenge data, so each order line gets one when the order is placed: product + delivery
day + batch number. A recall finds every order line with that lot, tells each customer at once and blocks the
product for new orders until Valio Aimo closes the recall.
"""
import json
import math
import re
import secrets
import threading
import time
from datetime import datetime, timedelta

from fastapi import APIRouter, Body, Request
from fastapi.responses import Response

from . import peppol, policy
from .catalog import fold
from .config import PUBLIC_URL, order_url
from .signing import digest

SCHEMA = [
    "create table if not exists issues(id text primary key, created real, order_id text, customer text, kind text, status text, recall_id text, doc text)",
    "create table if not exists recalls(id text primary key, created real, status text, product text, world text, doc text)",
    "create index if not exists issues_order on issues(order_id)",
    "create index if not exists issues_customer on issues(customer)",
]
KINDS = {"short_pick": "short pick", "delay": "delay", "recall": "recall"}
AUTHORITY = {"auto": "agent_may_resolve", "escalate": "needs_human_approval", "deny": "not_allowed"}
MAX_SUBSTITUTES = 3
LOTS_NOTE = ("Lots are not in the challenge data. The gateway gives every order line a lot when the order is placed: product number, delivery day and a batch number "
             "that goes up by one after each recall of that product.")
RECALL_REASON = "The supplier found a fault in this batch."
NEXT = ("Answer each open issue now with resolve_order_issue (id, option, rationale). Where authority.decision is agent_may_resolve, the buyer's mandate has already allowed that way out "
        "(basis names the rule), so nobody needs to be asked. Where it is needs_human_approval, choosing it sends it to a person at the buyer for approval. Either way the choice is made here, not in chat.")
unit_word = lambda u: {"piece": "pcs", "case": "cases"}.get(u, u)
body_of = lambda doc: {k: v for k, v in doc.items() if k != "proof"}
whole = lambda x: int(x) if float(x).is_integer() else x  # 280, not 280.0, in notices and views


class After:
    def __init__(self, g):
        self.g = g
        self.lock = threading.RLock()
        for s in SCHEMA:
            g.store.q(s)

    # ------------------------------------------------------------------ rows
    def _save(self, i: dict):
        self.g.store.q("insert or replace into issues(id,created,order_id,customer,kind,status,recall_id,doc) values(?,?,?,?,?,?,?,?)",
                       (i["id"], i["created"], i["order_id"], i["customer"], i["kind"], i["status"], i["facts"].get("recall"), json.dumps(i)))

    def _get(self, iid) -> dict | None:
        r = self.g.store.q("select doc from issues where id=?", (str(iid),))
        return json.loads(r[0][0]) if r else None

    def issues_of(self, oid) -> list[dict]:
        return [json.loads(r[0]) for r in self.g.store.q("select doc from issues where order_id=? order by created", (oid,))]

    def _recall(self, rid) -> dict | None:
        r = self.g.store.q("select doc from recalls where id=?", (str(rid),))
        return json.loads(r[0][0]) if r else None

    def _save_recall(self, rc: dict):
        self.g.store.q("insert or replace into recalls(id,created,status,product,world,doc) values(?,?,?,?,?,?)", (rc["id"], rc["created"], rc["status"], rc["product"]["id"], rc["world"], json.dumps(rc)))

    def _orders_exist(self, ids) -> bool:
        ids = list(dict.fromkeys(ids))
        return bool(ids) and self.g.store.q(f"select count(*) from orders where id in ({','.join('?' * len(ids))})", tuple(ids))[0][0] == len(ids)

    # ------------------------------------------------------- lots and recalls
    def lot(self, product, day, world="main") -> str:
        """The lot an order line gets when the order is placed."""
        n = 0
        for (doc,) in self.g.store.q("select doc from recalls where product=? and world=? and status in ('open','closed')", (str(product), world)):
            n += self._orders_exist([a["order_id"] for a in json.loads(doc)["affected"]])
        return f"L{product}-{str(day).replace('-', '')[2:]}-{n + 1:02d}"

    def lot_of(self, o, line) -> str:
        return line["wholesale"].get("lot") or f"L{line['item']['id']}-{o['wholesale']['delivery_date'].replace('-', '')[2:]}-01"

    def blocked(self, world="main") -> dict[str, str]:
        """{product id: recall id} for every open recall in this pool of stock."""
        out = {}
        for rid, product, doc in self.g.store.q("select id, product, doc from recalls where status='open' and world=?", (world,)):
            if self._orders_exist([a["order_id"] for a in json.loads(doc)["affected"]]):
                out[product] = rid
            else:  # the orders it was raised on are gone (demo reset): the recall goes with them
                self.g.store.q("update recalls set status='withdrawn' where id=?", (rid,))
        return out

    def block(self, live: dict, world="main", day=None, customer=None) -> dict:
        """Called on every price and stock lookup, so that what Valio Aimo has reported holds everywhere:
        * a product under recall cannot be ordered by anyone in that pool of stock, whatever the stock says
        * while a short pick or a delay is unsettled, that account cannot order the same product for that day again,
          so an agent cannot get around the notice by ordering the missing goods a second time."""
        if not live:
            return live
        for pid, rid in self.blocked(world).items():
            if pid in live:
                live[pid] = {**live[pid], "orderable": False, "reason": "product_recalled", "recall": rid}
        if customer and day:
            for (doc,) in self.g.store.q("select doc from issues where customer=? and status in ('open','pending') and kind in ('short_pick','delay')", (customer,)):
                i = json.loads(doc)
                f = i["facts"]
                hit = [f["product"]] if i["kind"] == "short_pick" and f["delivery_date"] == day else f.get("products", []) if i["kind"] == "delay" and f["from"] <= day < f["to"] else []
                hit = [pid for pid in hit if pid in live and live[pid]["orderable"]]
                if hit and self.g.store.order(i["order_id"]):
                    for pid in hit:
                        live[pid] = {**live[pid], "orderable": False, "reason": "short_at_warehouse" if i["kind"] == "short_pick" else "delivery_delayed", "issue": i["id"]}
        return live

    # ---------------------------------------------------------- order state
    def state(self, o) -> dict:
        """The order as it stands now: the signed confirmation with every signed amendment applied in order."""
        w = o["wholesale"]
        lines = [{"id": l["id"], "product": l["item"]["id"], "title": l["item"]["title"], "unit": unit_word(l["item"]["quantity_unit"]["display_text"]), "unit_price": l["item"]["price"],
                  "ordered": whole(l["wholesale"]["units"]), "units": whole(l["wholesale"]["units"]), "lot": self.lot_of(o, l), "status": "as ordered"} for l in o["line_items"]]
        st = {"status": "confirmed", "delivery_date": w["delivery_date"], "confirmed_date": w["delivery_date"], "lines": lines, "credit_ex_vat": 0, "credit_tax": 0, "amendments": [], "linked_orders": [], "open_issues": 0}
        issues = self.issues_of(o["id"])
        st["open_issues"] = sum(1 for i in issues if i["status"] in ("open", "pending"))
        for i in sorted((i for i in issues if i.get("amendment")), key=lambda i: i.get("resolved_ts", 0)):
            a = i["amendment"]
            for ch in a["changes"]:
                if ch["type"] == "line_quantity":
                    for l in lines:
                        if l["id"] == ch["line"]:
                            l["units"], l["status"] = ch["to"], ch["status"]
                elif ch["type"] == "delivery_date":
                    st["delivery_date"] = ch["to"]
                elif ch["type"] == "cancelled":
                    st["status"] = "cancelled"
                    for l in lines:
                        l["units"], l["status"] = 0, "cancelled"
            st["credit_ex_vat"] += a["credit"]["ex_vat"]
            st["credit_tax"] += a["credit"]["tax"]
            st["amendments"].append(a["id"])
            if a.get("linked_order"):
                st["linked_orders"].append(a["linked_order"])
        if st["status"] == "confirmed" and (st["amendments"]):
            st["status"] = "amended"
        return st

    def _credit(self, o, lid, units) -> tuple[int, int]:
        """(ex VAT, VAT) credited when `units` of a line are not delivered, at the confirmed price."""
        l = next(x for x in o["line_items"] if x["id"] == lid)
        tax = sum(t["amount"] for t in l["totals"] if t["type"] == "tax")
        return round(l["item"]["price"] * units), round(tax * units / l["wholesale"]["units"]) if l["wholesale"]["units"] else 0

    def _reqs(self, o) -> dict:
        """The hard requirements the original order was placed under (claims, allergens, certificates)."""
        c = self.g.store.checkout(o["checkout_id"]) or {}
        return (c.get("_internal", {}).get("inputs") or {}).get("requirements") or {}

    # ------------------------------------------------------------- ways out
    def _judge(self, plines, day, principal, reqs, ctx, day_auto):
        """The same rule engine as an order, told that this is a change to a placed order."""
        g = self.g
        ok = bool(principal and principal.get("ok"))
        cust = principal["customer"] if ok else None
        prof = g.store.get("profile:" + cust, policy.default_profile()) if cust else policy.default_profile()
        spent = g.store.spent_since(cust, time.time() - policy.WEEK) if cust else 0
        rules = policy.evaluate(plines, sum(l["amount"] for l in plines), principal, prof, g._history(principal), spent, reqs, day_auto, after=ctx, cleared=g._cleared(principal))
        if not ok:
            return rules, {"decision": "needs_valid_mandate", "reasons": [rules[0]["detail"]]}
        basis = [f"{r['id']}: {r['detail']}" for r in rules if r["outcome"] == "pass" and r["id"] in ("D1", "P1", "R1", "S1")]  # the mandate rule that already allows it
        return rules, {"decision": AUTHORITY[policy.decision(rules)], "reasons": [r["detail"] for r in rules if r["outcome"] != "pass"], **({"basis": basis} if basis else {})}

    def _equiv(self, od, of, sd, sf, units) -> float | None:
        """How many sales units of the substitute cover `units` of the original, by litres or kilos when both state them."""
        g, need = self.g, None
        for key in ("l", "kg"):
            a, b = g._content(od, of, key), g._content(sd, sf, key)
            if a and b:
                need = units * a / b
                break
        if need is None:
            if od["unit"] != sd["unit"]:
                return None
            need = units
        step = sd["increment"] / 10 ** sd["scale"]
        return round(math.ceil(need / step - 1e-9) * step, 2)

    def _substitutes(self, pid, units, day, principal):
        """Substitutes that can cover `units` of a product on that day: Valio Aimo's listed ones first, then the closest
        by name, then the smallest price change. Each keeps every free-from and diet claim and adds no allergen."""
        g, od = self.g, self.g.cat.get(pid)
        if not od:
            return
        cands = g.substitutes(pid, day, principal, limit=12)
        live = g._live([pid] + [c["id"] for c in cands], day, principal)
        words = lambda t: set(re.findall(r"[a-z0-9]+", fold(t)))
        ow, tier = words(od["title"]), {"best": 0, "better": 1, "good": 2}
        cands.sort(key=lambda c: (tier[c["tier"]], -len(ow & words(c["title"])), abs(c["price_delta_pct"] or 0), c["id"]))
        for c in cands:
            sd, sf = g.cat.get(c["id"]), live[c["id"]]
            su = self._equiv(od, live[pid], sd, sf, units)
            if not su or (sf["stock_known"] and sf["available"] < su):
                continue
            lines = [{"item": {"id": sd["id"]}, "quantity": round(su * 10 ** sd["scale"]), "wholesale": {"substitutes_for": pid}}]
            plines, _, errors = g._lines(lines, day, principal)
            if not errors:
                yield c, su, lines, plines

    def ways(self, issue, o, principal) -> list[dict]:
        """Every way out of an issue, with what it costs or credits and whether this agent may take it alone."""
        g, f, kind, eur = self.g, issue["facts"], issue["kind"], policy.eur
        st = self.state(o)
        if st["status"] == "cancelled":
            return []
        day, reqs, out = st["delivery_date"], self._reqs(o), []
        ok = bool(principal and principal.get("ok"))
        day_auto = g._day_auto(principal["customer"], day) if ok else 0
        line = next((l for l in st["lines"] if l["id"] == f.get("line")), None)

        def add(option, title, effect, numbers, changes, credit=(0, 0), lines=None, plines=None, sub=None, on=None):
            cost = sum(l["amount"] for l in plines or [])
            ctx = {"issue": issue["id"], "kind": kind, "option": option, "title": title, "effect": effect, "order_id": o["id"], "order_label": o["label"],
                   "credit": credit[0], "credit_tax": credit[1], "changes": changes, **numbers}
            rules, authority = self._judge(plines or [], on or day, principal, reqs if lines else {}, ctx, day_auto)
            if lines and any(r["outcome"] != "pass" and r["id"] in ("A1", "A2", "A3") for r in rules):
                return False  # never offered: it contains or may contain an excluded allergen (rule A1, allergen groups included), or lacks a required claim or certificate
            out.append({"option": option, "title": title, "effect": effect, "new_goods": bool(lines), "credit_ex_vat": credit[0], "cost_ex_vat": cost, "net_ex_vat": cost - credit[0],
                        **({"substitute": sub} if sub else {}), "authority": authority, "rules": rules, "_ctx": ctx, "_lines": lines or [], "_day": on or day})
            return True

        def subinfo(c, su):
            return {"id": c["id"], "title": c["title"], "units": su, "unit_price": c["unit_price"], "tier": c["tier"], "listed_by_valio_aimo": c["listed_by_valio_aimo"],
                    "price_delta_pct": c["price_delta_pct"], "per": c["per"], "why": c["why"]}

        def lq(to, status):
            return [{"type": "line_quantity", "line": line["id"], "product": line["product"], "title": line["title"], "unit": line["unit"], "from": line["units"], "to": to, "status": status}]

        listed = lambda c: "on Valio Aimo's substitute list" if c["listed_by_valio_aimo"] else "not on Valio Aimo's substitute list"
        price = lambda c: "" if c["price_delta_pct"] is None else f", {c['price_delta_pct']:+.0f}% per {c['per']}"
        net = lambda cost, credit: "the same amount as confirmed, ex VAT" if cost == credit else f"{eur(abs(cost - credit))} {'more' if cost > credit else 'less'} than confirmed, ex VAT"
        if kind == "short_pick" and line and line["units"] > f["deliverable"]:
            have, u, t = f["deliverable"], line["unit"], line["title"]
            gap = round(line["units"] - have, 2)
            credit, n = self._credit(o, line["id"], gap), 0
            if have > 0:
                add("accept_partial", f"Take the {have:g} and get the missing {gap:g} credited",
                    f"{have:g} of {line['ordered']:g} {u} of {t} are delivered. The missing {gap:g} are credited: {eur(credit[0])} ex VAT. Nothing replaces them.",
                    {"delivered_pct": round(100 * have / line["ordered"], 1), "of": "the line"}, lq(have, "short"), credit)
            for c, su, lines, plines in self._substitutes(line["product"], gap, day, principal):
                cost = sum(l["amount"] for l in plines)
                n += add(f"substitute_{c['id']}", f"Fill the gap with {su:g} x {c['title']}",
                         f"{have:g} {u} of {t} are delivered as ordered. The missing {gap:g} come as {su:g} x {c['title']} on the same day ({listed(c)}{price(c)}). "
                         f"New goods {eur(cost)}, credit {eur(credit[0])}: {net(cost, credit[0])}.", {}, lq(have, "short"), credit, lines, plines, subinfo(c, su))
                if n >= MAX_SUBSTITUTES:
                    break
            whole = self._credit(o, line["id"], line["units"])
            add("drop_line", "Drop the line", f"None of the {line['units']:g} {u} of {t} is delivered. The line is credited: {eur(whole[0])} ex VAT.", {"delivered_pct": 0, "of": "the line"}, lq(0, "dropped"), whole)
        elif kind == "delay" and st["delivery_date"] < f["to"]:
            days = (datetime.strptime(f["to"], "%Y-%m-%d") - datetime.strptime(st["confirmed_date"], "%Y-%m-%d")).days
            add("accept_new_date", f"Accept delivery on {g_day(f['to'])}", f"The whole order arrives on {g_day(f['to'])} instead of {g_day(st['delivery_date'])}. Products, quantities and prices stay as confirmed.",
                {"delay_days": days}, [{"type": "delivery_date", "from": st["delivery_date"], "to": f["to"]}], on=f["to"])
            parts = [self._credit(o, l["id"], l["units"]) for l in st["lines"] if l["units"] > 0]
            credit = (sum(p[0] for p in parts), sum(p[1] for p in parts))
            add("cancel_order", "Cancel the order", f"Nothing from this order is delivered. {eur(credit[0])} ex VAT is credited.", {"delivered_pct": 0, "of": "the order"}, [{"type": "cancelled"}], credit)
        elif kind == "recall" and line and line["units"] > 0:
            have, u, t = line["units"], line["unit"], line["title"]
            credit, n, numbers = self._credit(o, line["id"], have), 0, {"recall": f["recall"], "lot": f["lot"]}
            for c, su, lines, plines in self._substitutes(line["product"], have, day, principal):
                cost = sum(l["amount"] for l in plines)
                n += add(f"replace_{c['id']}", f"Replace with {su:g} x {c['title']}",
                         f"The {have:g} {u} of {t} from lot {f['lot']} are taken off the order and credited. {su:g} x {c['title']} come instead on the same day "
                         f"({listed(c)}{price(c)}). New goods {eur(cost)}, credit {eur(credit[0])}: {net(cost, credit[0])}.",
                         numbers, lq(0, "recalled"), credit, lines, plines, subinfo(c, su))
                if n >= MAX_SUBSTITUTES:
                    break
            add("credit", "Take a credit, no replacement", f"The {have:g} {u} of {t} from lot {f['lot']} are taken off the order and credited: {eur(credit[0])} ex VAT. Nothing replaces them.", numbers, lq(0, "recalled"), credit)
        return out

    @staticmethod
    def _public(ways, rules=False) -> list[dict]:
        return [{k: v for k, v in w.items() if not k.startswith("_") and (rules or k != "rules")} for w in ways]

    # ---------------------------------------------------------------- raise
    def _raise(self, o, kind, facts, what, by) -> dict:
        """Write one issue: the facts, and a notice signed by Valio Aimo with the ways out as they stand right now."""
        g, w = self.g, o["wholesale"]
        issue = {"id": "iss_" + secrets.token_hex(5), "kind": kind, "status": "open", "created": time.time(), "raised_at": g_iso(), "order_id": o["id"], "order_label": o["label"], "customer": w["customer"],
                 "facts": facts, "what_happened": what, "attempts": [], "pending": None, "resolution": None, "amendment": None}
        p0, live = self._placed_under(o)
        ways = self.ways(issue, o, p0)
        issue["notice"] = g._prove({
            "type": "order_issue", "id": issue["id"], "kind": kind, "raised_at": issue["raised_at"], "raised_by": "Valio Aimo", "customer": w["customer"],
            "order": {"id": o["id"], "label": o["label"], "permalink_url": o["permalink_url"], "confirmation_digest": digest(body_of(o))},
            "what_happened": what, "facts": facts, "ways_out": self._public(ways, rules=True),
            "judged_under": {"mandate_id": w["evidence"].get("mandate_id"), "mandate_valid_now": live,
                             "note": "Whether the agent may take a way out alone is judged here under the mandate the order was placed with. It is judged again, under the mandate the agent presents, when it answers."},
            "answer_with": {"mcp": "resolve_order_issue", "rest": f"{PUBLIC_URL}/issues/{issue['id']}/resolve"},
        })
        self._save(issue)
        g.store.log(issue["id"], "gateway", "issue", f"Valio Aimo reported a {KINDS[kind]} on order {o['label']}: {what} {len(ways)} ways out sent to the customer's agent as a signed notice ({issue['id']}).",
                    {"issue": issue["id"], "kind": kind, "order_id": o["id"], "facts": facts, "ways_out": [{"option": x["option"], "authority": x["authority"]["decision"]} for x in ways], "raised_by": by}, acct=w["customer"])
        return issue

    def _placed_under(self, o) -> tuple[dict | None, bool]:
        """The mandate an order was placed with, as a principal, and whether it is still valid. A notice is judged under its
        limits even after it has run out, so that the ways out still say who could take them; answering needs a valid one."""
        g, tok = self.g, o["wholesale"]["evidence"].get("mandate")
        p = g.identify(tok)
        if p and p.get("ok"):
            return p, True
        try:
            from .signing import jwt_peek, jwt_verify
            _, c = jwt_peek(tok)
            buyer = g.store.get("buyer:" + str(c.get("iss")))
            if buyer and jwt_verify(tok, buyer["jwk"]) is not None:
                jwk = (c.get("cnf") or {}).get("jwk")
                return {"ok": True, "token": tok, "jti": c["jti"], "exp": c["exp"], "buyer": c["iss"], "buyer_name": buyer["name"], "customer": buyer["customer"], "slug": buyer.get("slug"), "world": buyer.get("world", "main"),
                        "agent": c.get("sub"), "agent_name": c.get("agent_name"), "agent_jwk": jwk, "bound": bool(jwk), "mandate": c.get("mandate") or {}, "ship_to": buyer.get("ship_to")}, False
        except Exception:
            pass
        return p, False

    def _line(self, o, st, line=None, product=None):
        """Pick the order line an issue is about: by line id, by position, by product, else the first line that still delivers something."""
        for n, l in enumerate(st["lines"]):
            if l["units"] > 0 and (line in (l["id"], n + 1, str(n + 1)) or (line is None and product in (None, l["product"]))):
                return l
        raise self.g_error(409, "invalid_state", f"Order {o['label']} has no such line that is still to be delivered.")

    def raise_issue(self, o, kind, line=None, deliverable=None, days=None, new_date=None, reason=None, by="Valio Aimo") -> dict:
        """Valio Aimo's side: report a short pick or a delay on a placed order. Recalls go through raise_recall."""
        with self.lock:
            st = self.state(o)
            if st["status"] == "cancelled":
                raise self.g_error(409, "invalid_state", f"Order {o['label']} is cancelled.")
            day = self.g_nice(st["delivery_date"])
            if kind == "short_pick":
                l = self._line(o, st, line)
                d = self.g.cat.get(l["product"]) or {"increment": 1, "scale": 0}
                step = d["increment"] / 10 ** d["scale"]
                have = l["units"] * 0.7 if deliverable is None else float(deliverable)
                have = round(math.floor(have / step + 1e-9) * step, 2)
                have = whole(have)
                if not 0 <= have < l["units"]:
                    raise self.g_error(400, "bad_request", f"A short pick delivers less than the {l['units']:g} {l['unit']} on the line.")
                facts = {"line": l["id"], "product": l["product"], "title": l["title"], "unit": l["unit"], "ordered": l["ordered"], "deliverable": have, "missing": round(l["units"] - have, 2), "delivery_date": st["delivery_date"],
                         **({"reason": str(reason)[:200]} if reason else {})}
                what = (f"The warehouse can deliver only {have:g} of the {l['units']:g} {l['unit']} of {l['title']} on {day}. {facts['missing']:g} are missing." if have
                        else f"The warehouse cannot deliver any of the {l['units']:g} {l['unit']} of {l['title']} on {day}.")
            elif kind == "delay":
                to = str(new_date or "") if re.fullmatch(r"\d{4}-\d{2}-\d{2}", str(new_date or "")) else (datetime.strptime(st["delivery_date"], "%Y-%m-%d") + timedelta(days=max(1, int(days or 1)))).strftime("%Y-%m-%d")
                if to <= st["delivery_date"]:
                    raise self.g_error(400, "bad_request", "A delay moves the delivery to a later day.")
                n = (datetime.strptime(to, "%Y-%m-%d") - datetime.strptime(st["delivery_date"], "%Y-%m-%d")).days
                facts = {"from": st["delivery_date"], "to": to, "days": n, "products": [l["product"] for l in st["lines"] if l["units"] > 0], **({"reason": str(reason)[:200]} if reason else {})}
                what = f"The delivery moves from {day} to {self.g_nice(to)}, {n} day{'s' * (n != 1)} later." + (f" Reason: {facts['reason']}." if reason else "")
            else:
                raise self.g_error(400, "bad_request", "kind is short_pick, delay or recall.")
            return self._raise(o, kind, facts, what, by)

    def raise_recall(self, o, line=None, reason=None, by="Valio Aimo") -> tuple[dict, list[dict]]:
        """Recall the lot on one order line: find every order that got the lot, tell each customer, block the product."""
        g = self.g
        with self.lock:
            l = self._line(o, self.state(o), line)
            acct = g.account_of(o["wholesale"]["customer"]) or {}
            world, reason = acct.get("world", "main"), str(reason or RECALL_REASON)[:200]
            if l["product"] in self.blocked(world):
                raise self.g_error(409, "invalid_state", f"{l['title']} is already under recall {self.blocked(world)[l['product']]}.")
            hits = []
            for x in g.store.orders(5000):
                if not any(xl["item"]["id"] == l["product"] for xl in x["line_items"]):
                    continue
                a = g.account_of(x["wholesale"]["customer"])
                if a and a.get("world", "main") == world:
                    hits += [(x, xl) for xl in self.state(x)["lines"] if xl["product"] == l["product"] and xl["lot"] == l["lot"] and xl["units"] > 0]
            hits.sort(key=lambda h: h[0]["wholesale"]["placed_at"])
            d = g.cat.get(l["product"]) or {}
            rc = {"id": "rcl_" + secrets.token_hex(4), "created": time.time(), "status": "open", "world": world, "raised_at": g_iso(), "raised_by": by, "raised_on": o["id"],
                  "product": {"id": l["product"], "title": l["title"], "gtin": d.get("gtin"), "supplier": d.get("vendor")}, "lot": l["lot"], "reason": reason,
                  "affected": [{"order_id": x["id"], "order_label": x["label"], "customer": x["wholesale"]["customer"], "buyer": (x["wholesale"].get("buyer") or {}).get("name"), "line": xl["id"], "units": xl["units"], "unit": xl["unit"]} for x, xl in hits]}
            self._save_recall(rc)  # from here on the product is blocked, so no way out below can offer it
            issues = []
            for (x, xl), a in zip(hits, rc["affected"]):
                facts = {"recall": rc["id"], "line": xl["id"], "product": xl["product"], "title": xl["title"], "unit": xl["unit"], "units": xl["units"], "lot": xl["lot"], "reason": reason}
                i = self._raise(x, "recall", facts, f"Lot {xl['lot']} of {xl['title']} is recalled: {reason} This order has {xl['units']:g} {xl['unit']} from that lot. They are not to be used; they are replaced or credited.", by)
                a["issue_id"] = i["id"]
                issues.append(i)
            rc["notice"] = g._prove({"type": "recall", "id": rc["id"], "raised_at": rc["raised_at"], "product": rc["product"], "lot": rc["lot"], "reason": reason,
                                     "instruction": "Do not use or serve this lot. It is replaced or credited. The product is blocked for new orders until Valio Aimo closes the recall.",
                                     "affected": rc["affected"], "lots": LOTS_NOTE})
            self._save_recall(rc)
            custs = {a["customer"] for a in rc["affected"]}
            g.store.log(rc["id"], "gateway", "recall", f"Recall {rc['id']}: lot {rc['lot']} of {l['title']}. {reason} {len(hits)} order{'s' * (len(hits) != 1)} at {len(custs)} customer{'s' * (len(custs) != 1)} got this lot, "
                        f"and each was sent a signed notice at once. {l['title']} is blocked for new orders until the recall is closed.",
                        {"recall": rc["id"], "product": l["product"], "lot": rc["lot"], "orders": [a["order_id"] for a in rc["affected"]], "customers": sorted(custs), "raised_by": by}, acct=o["wholesale"]["customer"])
            return rc, issues

    def close_recall(self, rid, by="Valio Aimo quality") -> dict:
        with self.lock:
            rc = self._recall(rid)
            if not rc:
                raise self.g_error(404, "not_found", "No such recall.")
            if rc["status"] == "open":
                rc.update(status="closed", closed_at=g_iso(), closed_by=by)
                self._save_recall(rc)
                self.g.store.log(rc["id"], f"human:{by}", "recall", f"{by} closed recall {rc['id']}. {rc['product']['title']} can be ordered again; new orders get the next batch.", {"recall": rc["id"], "product": rc["product"]["id"]},
                                 acct=(self.g.store.order(rc["raised_on"]) or {"wholesale": {}})["wholesale"].get("customer"))
            return self.recall_view(rid)

    def recall_view(self, rid) -> dict:
        """Batch -> product -> every order and customer that got it -> what each did about it -> state now."""
        rc = self._recall(rid)
        if not rc:
            raise self.g_error(404, "not_found", "No such recall.")
        if rc["status"] == "open" and rc["product"]["id"] not in self.blocked(rc["world"]):
            rc = self._recall(rid)
        rows = []
        for a in rc["affected"]:
            i = self._sync(self._get(a.get("issue_id")) or {"status": "gone"})
            res = i.get("resolution") or {}
            state = "order no longer exists" if i["status"] == "gone" or not self.g.store.order(a["order_id"]) else "waiting for a person" if (i.get("pending") or {}).get("status") == "requires_escalation" else \
                "waiting for the agent" if i["status"] in ("open", "pending") else "replaced" if res.get("linked_order") else "credited" if i["status"] == "resolved" else i["status"]
            rows.append({**a, "order_url": order_url(a['order_id']), "state": state,
                         "issue": {"id": a.get("issue_id"), "status": i["status"], **({k: res.get(k) for k in ("title", "agent", "person", "alone", "at", "rationale", "amendment", "linked_order", "credit_ex_vat")} if res else {})}})
        done = sum(1 for r in rows if r["issue"]["status"] == "resolved")
        return {"id": rc["id"], "status": rc["status"], "blocked": rc["status"] == "open", "raised_at": rc["raised_at"], "closed_at": rc.get("closed_at"), "closed_by": rc.get("closed_by"), "product": rc["product"], "lot": rc["lot"], "reason": rc["reason"],
                "affected": rows, "summary": {"orders": len(rows), "customers": len({r["customer"] for r in rows}), "units": whole(sum(r["units"] for r in rows)), "settled": done, "open": len(rows) - done},
                "notice": rc.get("notice"), "lots": LOTS_NOTE, "url": f"{PUBLIC_URL}/recalls/{rc['id']}"}

    # --------------------------------------------------------------- answer
    def _sync(self, issue) -> dict:
        """Bring an issue in line with its checkout: a rejected, cancelled or expired proposal opens the issue again."""
        p = issue.get("pending")
        if issue.get("status") != "pending" or not p:
            return issue
        try:
            c = self.g.get_checkout(p["checkout_id"])
        except Exception:
            c = None
        st = c["status"] if c else "gone"
        if st in ("canceled", "incomplete", "gone"):
            ap = ((c or {}).get("wholesale") or {}).get("approval") or {}
            with self.lock:
                issue = self._get(issue["id"])
                if issue["status"] == "pending":
                    issue["attempts"].append({**p, "status": st, "outcome": f"rejected by {ap['by']}" if ap.get("decision") == "rejected" else "cancelled" if st == "canceled" else "expired", **({"note": ap["note"]} if ap.get("note") else {})})
                    issue.update(pending=None, status="open")
                    self._save(issue)
        elif p.get("status") != st:
            issue["pending"]["status"] = st
        return issue

    def _mine(self, iid, principal) -> dict:
        i = self._get(iid)
        if not i or not self.g.store.order(i["order_id"]):
            raise self.g_error(404, "not_found", "No such issue.")
        if i["customer"] != principal["customer"]:
            raise self.g_error(403, "forbidden", "This issue belongs to another account.")
        return self._sync(i)

    def view(self, issue, principal=None, o=None) -> dict:
        """What an agent sees of one issue: the facts, the ways out judged under its own mandate, and where it stands."""
        o = o or self.g.store.order(issue["order_id"])
        st = self.state(o)
        v = {"id": issue["id"], "kind": issue["kind"], "status": issue["status"], "raised_at": issue["raised_at"], "what_happened": issue["what_happened"],
             "order": {"id": o["id"], "label": o["label"], "delivery_date": st["delivery_date"], "status": st["status"], "request": o["wholesale"].get("intent")}, "facts": issue["facts"]}
        if issue["kind"] == "recall":
            v["recall"] = {"id": issue["facts"]["recall"], "url": f"{PUBLIC_URL}/recalls/{issue['facts']['recall']}", "note": "A recall cannot be declined. The lot is replaced or credited, never kept."}
        if issue["status"] == "open":
            v["ways_out"] = self._public(self.ways(issue, o, principal))
            if issue["attempts"]:
                v["earlier_attempts"] = [{k: a.get(k) for k in ("option", "title", "outcome", "note") if a.get(k)} for a in issue["attempts"]]
        elif issue["status"] == "pending":
            p = issue["pending"]
            c = self.g.store.checkout(p["checkout_id"]) or {}
            v["pending"] = {"option": p["option"], "title": p["title"], "checkout_id": p["checkout_id"], "checkout_status": c.get("status"), **({"continue_url": c["continue_url"]} if c.get("continue_url") else {}),
                            "next": "A person must approve it at continue_url. Then call complete_checkout." if c.get("status") == "requires_escalation" else "Call complete_checkout with this checkout id, or cancel_checkout to choose another way out."}
        else:
            v["resolution"] = issue.get("resolution")
        return v

    def list(self, principal, order=None, include_settled=False, trace=None) -> dict:
        g = self.g
        if not principal or not principal.get("ok"):
            raise self.g_error(401, (principal or {}).get("error", "mandate_required"), (principal or {}).get("detail", "Present the buyer's mandate as Authorization: Bearer <mandate>."))
        want, out, msgs = str(order or "").strip().lower(), [], []
        for (doc,) in g.store.q("select doc from issues where customer=? order by created desc limit 200", (principal["customer"],)):
            i = json.loads(doc)
            o = g.store.order(i["order_id"])
            if not o or (want and want not in (o["id"].lower(), o["label"].lower())):
                continue
            i = self._sync(i)
            if i["status"] == "open" and not self.ways(i, o, principal):  # overtaken by a later change to the same order
                with self.lock:
                    i.update(status="closed", resolution={"title": "Overtaken by a later change to the order", "at": g_iso()})
                    self._save(i)
            if i["status"] in ("open", "pending") or include_settled:
                out.append(self.view(i, principal, o))
        if want and not any(want in (o["id"].lower(), o["label"].lower()) for o in g.store.orders(500, principal["customer"])):
            msgs.append({"type": "warning", "code": "not_found", "content": f"This account has no order '{order}'."})
        elif not out:
            msgs.append({"type": "info", "content": "No open issues" + (f" on order {order}." if want else " on this account.")})
        g._logp(principal, trace, "lookup", f"Asked Valio Aimo for {'all' if include_settled else 'open'} issues" + (f" on order {order}" if want else "") + f": {len(out)} found.", {"issues": [i["id"] for i in out]})
        todo = any(i["status"] == "open" for i in out)
        return g._prove({"ucp": g._env(), "issues": out, "messages": msgs, **({"next": NEXT} if todo else {})})

    def resolve(self, iid, option, rationale="", principal=None, trace=None) -> dict:
        """The agent's answer: one way out and a reason. Returns a checkout that amends the order. If the choice is inside
        the mandate it is ready to complete; otherwise it carries the approval link for a person."""
        g = self.g
        if not principal or not principal.get("ok"):
            raise self.g_error(401, (principal or {}).get("error", "mandate_required"), (principal or {}).get("detail", "Present the buyer's mandate as Authorization: Bearer <mandate>."))
        with self.lock:
            issue = self._mine(iid, principal)
            o, option, rationale = g.store.order(issue["order_id"]), str(option or "").strip(), str(rationale or "").strip()[:600]
            if issue["status"] in ("resolved", "closed"):
                raise self.g_error(409, "issue_settled", f"Issue {iid} is already settled: {(issue.get('resolution') or {}).get('title', '')}.")
            if issue["status"] == "pending":
                p = issue["pending"]
                if p["option"] == option:
                    return g.get_checkout(p["checkout_id"], principal)
                raise self.g_error(409, "issue_pending", f"'{p['title']}' is already waiting as checkout {p['checkout_id']}. Complete it, or cancel that checkout first to choose another way out.")
            ways = self.ways(issue, o, principal)
            way = next((x for x in ways if x["option"] == option), None)
            names = ", ".join(x["option"] for x in ways)
            if not way and issue["kind"] == "recall":
                r = policy.after_rules(principal["mandate"], {"recall": issue["facts"]["recall"], "lot": issue["facts"]["lot"], "keeps_recalled": True})[0]
                g.agent_note(principal)
                issue.setdefault("refused", []).append({"option": option[:40], "at": g_iso(), "agent": g._who(principal), "rule": r["id"], "trace": trace})
                self._save(issue)
                g.store.log(trace or iid, "gateway", "refused", f"Refused '{option[:40]}' for the recall on order {o['label']}. [{r['id']} {r['outcome']}] {r['detail']}", {"issue": iid, "rule": r}, acct=issue["customer"])
                raise self.g_error(409, "policy_denied", f"{r['title']}: {r['detail']} Choose one of: {names}.")
            if not way:
                raise self.g_error(400, "unknown_option", f"Issue {iid} has no way out '{option[:40]}'. Choose one of: {names}.")
            ctx, cid = way["_ctx"], "chk_" + secrets.token_hex(6)
            inputs = {"line_items": way["_lines"], "delivery_date": way["_day"], "requirements": self._reqs(o) if way["_lines"] else {}, "purchase_order": o["wholesale"].get("purchase_order"),
                      "intent": f"Valio Aimo reported on order {o['label']}: {issue['what_happened']} Way out chosen: {way['title']}.", "rationale": rationale or None, "amends": ctx}
            c = g._assemble(cid, inputs, principal, trace)
            c["messages"].append({"type": "info", "code": "amends_order", "content": f"This checkout changes order {o['label']}. {way['effect']}" if c["status"] != "incomplete"
                                  else "This way out is not available right now. Call list_order_issues for the current ways out."})
            g._sign_checkout(c)
            who = g._who(principal)
            g.agent_note(principal)
            g.store.log(trace, who, "resolve", f"Chose a way out for the {KINDS[issue['kind']]} on order {o['label']}: {way['title']}." + (f" Reason: {rationale}" if rationale else ""),
                        {"issue": iid, "option": option, "order_id": o["id"], "net_ex_vat": way["net_ex_vat"]}, cid=cid, acct=issue["customer"])
            if way["_lines"]:
                g._log_checkout(c, principal, "Created")
            else:
                dec, rules = c["wholesale"]["policy"]["decision"], c["wholesale"]["policy"]["rules"]
                head = {"auto": "Within the agent's authority. No person needed.", "escalate": "Outside the agent's authority. Sent to a person for sign-off.", "deny": "Refused."}.get(dec, "Not possible.")
                g.store.log(trace, "gateway", "policy", head + " " + " ".join(f"[{r['id']} {r['outcome']}] {r['detail']}" for r in rules if r["outcome"] != "pass" or r["id"] in ("D1", "P1", "R1")),
                            {"decision": dec, "rules": rules, "offer_signature": c["ap2"]["merchant_authorization"][-24:], "held_until": c["expires_at"]}, cid=cid)
                if dec == "escalate":
                    g.agent_note(principal, "asked")
            if c["status"] != "incomplete":
                issue.update(status="pending", pending={"option": option, "title": way["title"], "checkout_id": cid, "status": c["status"], "at": g_iso(), "agent": who, "trace": trace, "rationale": rationale})
                self._save(issue)
            return g.public(c)

    def settle(self, c, principal, proof, linked=None) -> dict:
        """Called by the gateway when the agent completes a checkout that amends an order: write the signed amendment.
        `linked` is the new order when the way out brought new goods."""
        g, w = self.g, c["wholesale"]
        am, ap = w["amends"], w.get("approval")
        with self.lock:
            issue, o = self._get(am["issue"]), g.store.order(am["order_id"])
            amd = g._prove({
                "type": "order_amendment", "id": "amd_" + secrets.token_hex(6), "at": g_iso(),
                "order": {"id": o["id"], "label": o["label"], "permalink_url": o["permalink_url"], "confirmation_digest": digest(body_of(o))},
                "issue": {"id": issue["id"], "kind": issue["kind"], "what_happened": issue["what_happened"], "notice_digest": digest(body_of(issue["notice"]))},
                "way_out": {"option": am["option"], "title": am["title"], "effect": am["effect"]}, "changes": am["changes"],
                "credit": {"ex_vat": am["credit"], "tax": am["credit_tax"], "total": am["credit"] + am["credit_tax"], "currency": c["currency"]},
                "linked_order": {"id": linked["id"], "label": linked["label"], "permalink_url": linked["permalink_url"], "subtotal": next(t["amount"] for t in linked["totals"] if t["type"] == "subtotal")} if linked else None,
                "decided_by": {"agent": w["agent"], "mandate_id": w["mandate_id"], "alone": not ap, "person": ap["by"] if ap else None, "rationale": w.get("agent_rationale")},
                "evidence": {"checkout_id": c["id"], "mandate": principal["token"], "offer_digest": c["_internal"]["digest"], "offer_signature": c["ap2"]["merchant_authorization"], "agent_acceptance": proof,
                             "agent_class": "key" if proof else "keyless", "keyless_receipt": c["_internal"].get("keyless_receipt"), "approval": ap, "policy": w["policy"]},
            })
            issue.update(status="resolved", pending=None, amendment=amd, resolved_ts=time.time(),
                         resolution={"option": am["option"], "title": am["title"], "at": amd["at"], "agent": w["agent"], "alone": not ap, "person": ap["by"] if ap else None, "rationale": w.get("agent_rationale"),
                                     "amendment": amd["id"], "linked_order": amd["linked_order"], "credit_ex_vat": am["credit"], "checkout_id": c["id"]})
            self._save(issue)
            if am["credit"]:  # what is credited no longer counts against the weekly limit
                g.store.q("update orders set total = total - ? where id=?", (am["credit"], o["id"]))
            for ch in am["changes"]:  # stock promised to a cancelled order or a dropped line goes back to the pool (a recalled lot does not)
                if ch["type"] == "cancelled":
                    g.store.q("update holds set state='released' where ref=? and state='ordered'", (o["checkout_id"],))
                elif ch["type"] == "line_quantity" and ch["status"] == "dropped":
                    g.store.q("update holds set state='released' where ref=? and product=? and state='ordered'", (o["checkout_id"], ch["product"]))
            who = f"Approved by {ap['by']}." if ap else "Decided by the agent alone, inside its mandate."
            g.store.log(c["_internal"].get("trace"), "gateway", "amendment", f"Order {o['label']} amended ({amd['id']}): {am['title']}. {am['effect']} {who} Signed by Valio Aimo; the original confirmation is unchanged.",
                        {"amendment": amd["id"], "issue": issue["id"], "order_id": o["id"], "option": am["option"], "credit_ex_vat": am["credit"], "linked_order": (amd["linked_order"] or {}).get("id"), "approval": bool(ap)}, cid=c["id"], acct=issue["customer"])
            c["messages"].append({"type": "info", "code": "issue_settled", "content": f"Issue {issue['id']} on order {o['label']} is settled: {am['title']}. Signed amendment {amd['id']}."})
            return {"id": o["id"], "label": o["label"], "permalink_url": o["permalink_url"], "amendment": amd["id"]}

    # ------------------------------------------------------- order page data
    def for_order(self, o) -> dict:
        """Everything that happened to an order after it was placed, with the signed documents, for GET /orders/{id}/audit."""
        g, out = self.g, []
        for i in self.issues_of(o["id"]):
            i = self._sync(i)
            a, row = i.get("amendment"), {k: i.get(k) for k in ("id", "kind", "status", "raised_at", "what_happened", "facts", "notice", "resolution", "amendment")}
            row["attempts"] = [{k: x.get(k) for k in ("option", "title", "outcome", "note", "at", "agent")} for x in i["attempts"]]
            row["pending"] = {k: i["pending"].get(k) for k in ("option", "title", "status", "at", "agent")} if i.get("pending") else None
            row["refused"] = [{k: x.get(k) for k in ("option", "at", "agent", "rule")} for x in i.get("refused", [])]
            if a:
                row["offer"] = ((g.store.checkout(a["evidence"]["checkout_id"]) or {}).get("_internal") or {}).get("accepted_offer")
                row["linked_order"] = g.store.order(a["linked_order"]["id"]) if a.get("linked_order") else None
            out.append(row)
        am = o["wholesale"].get("amends")
        orig = g.store.order(am["order_id"]) if am else None
        return {"issues": out, "state": self.state(o), "lots": LOTS_NOTE,
                "amends": {"order_id": am["order_id"], "order_label": am["order_label"], "permalink_url": orig["permalink_url"] if orig else None, "issue": am["issue"], "kind": am["kind"], "title": am["title"]} if am else None}

    def events(self, o, base: list) -> list:
        """The order's own ledger entries plus everything logged for its issues, in ledger order."""
        ev, st = {e["seq"]: e for e in base}, self.g.store
        refs, cids = set(), set()
        for i in self.issues_of(o["id"]):
            refs.add(i["id"])
            if i["facts"].get("recall"):
                refs.add(i["facts"]["recall"])
            for p in i["attempts"] + i.get("refused", []) + [i.get("pending") or {}]:
                refs.add(p.get("trace"))
                cids.add(p.get("checkout_id"))
            cids.add((i.get("resolution") or {}).get("checkout_id"))
            cids.add(((i.get("amendment") or {}).get("evidence") or {}).get("checkout_id"))
        am = o["wholesale"].get("amends")
        if am:
            refs.add(am["issue"])
        for r in filter(None, refs):
            ev.update({e["seq"]: e for e in st.events(ref=r)})
        for c in filter(None, cids):
            ev.update({e["seq"]: e for e in st.events(cid=c)})
            tr = ((st.checkout(c) or {}).get("_internal") or {}).get("trace")
            if tr:
                ev.update({e["seq"]: e for e in st.events(ref=tr)})
        return [ev[k] for k in sorted(ev)]

    def summary(self, issue, o) -> dict:
        """One row for a console."""
        p, res = issue.get("pending") or {}, issue.get("resolution") or {}
        c = self.g.store.checkout(p["checkout_id"]) if p else None
        return {"id": issue["id"], "kind": issue["kind"], "status": issue["status"], "raised_at": issue["raised_at"], "what_happened": issue["what_happened"],
                "order": {"id": o["id"], "label": o["label"], "url": order_url(o['id'])}, "ways_out": [{"option": x["option"], "title": x["title"], "authority": x["authority"]["decision"]} for x in issue["notice"]["ways_out"]],
                "pending": {"option": p["option"], "title": p["title"], "checkout_id": p["checkout_id"], "status": (c or {}).get("status"), "continue_url": (c or {}).get("continue_url")} if p else None,
                "chosen": res.get("title"), "decided_by": None if not res else "agent alone" if res.get("alone") else res.get("person"), "linked_order": res.get("linked_order"), "credit_ex_vat": res.get("credit_ex_vat"),
                "recall": {"id": issue["facts"]["recall"], "url": f"{PUBLIC_URL}/recalls/{issue['facts']['recall']}"} if issue["kind"] == "recall" else None,
                "request": f"Valio Aimo reports a problem with order {o['label']}. Sort it out."}

    # late-bound names from service.py (it imports this module, so they cannot be imported at the top)
    def g_error(self, status, code, message):
        from .service import GatewayError
        return GatewayError(status, code, message)

    def g_nice(self, day):
        from .service import nice_day
        return nice_day(day)


def g_iso() -> str:
    from .service import iso
    return iso()


def g_day(day) -> str:
    from .service import nice_day
    return nice_day(day)


def build(g, ctx, page) -> APIRouter:
    """REST for the after-order loop, the demo trigger and the recall page. The MCP tools are in mcp_tools.py."""
    r, a = APIRouter(), g.after

    def who(request: Request):
        auth = request.headers.get("authorization", "")
        return g.identify(auth[7:].strip() if auth.lower().startswith("bearer ") else None), request.headers.get("x-trace-id")

    @r.get("/issues", tags=["after the order"], summary="Open issues Valio Aimo has raised on this account's orders, each with its ways out")
    def issues(request: Request, order: str = "", include_settled: bool = False):
        p, tr = who(request)
        return a.list(p, order, include_settled, tr)

    @r.get("/issues/{iid}", tags=["after the order"], summary="One issue: the notice Valio Aimo signed, the ways out now, and the signed amendment once settled")
    def issue(iid: str, request: Request):
        p, _ = who(request)
        if not p or not p.get("ok"):
            raise a.g_error(401, (p or {}).get("error", "mandate_required"), (p or {}).get("detail", "Present the buyer's mandate as Authorization: Bearer <mandate>."))
        i = a._mine(iid, p)
        return g._prove({"ucp": g._env(), "issue": a.view(i, p), "notice": i["notice"], "amendment": i.get("amendment")})

    @r.post("/issues/{iid}/resolve", tags=["after the order"], summary="Choose a way out. Returns a checkout that amends the order: complete it like any other")
    def resolve(iid: str, request: Request, body: dict = Body(...)):
        p, tr = who(request)
        return a.resolve(iid, body.get("option"), body.get("rationale") or "", p, tr)

    @r.get("/orders/{oid}/peppol/order-response-amended.xml", tags=["after the order"], summary="The order as it stands after its amendments, as a Peppol BIS Ordering 3 OrderResponse")
    def peppol_amended(oid: str):
        o = g.get_order(oid)
        return Response(peppol.amended_response_xml(o, a.state(o)), media_type="application/xml")

    # ---- demo scaffolding: the stand-in for Valio Aimo's warehouse and quality systems, and for its recall desk
    def ask(labels) -> str:
        """The request a buyer would type to their agent. It names the orders and nothing else: the agent works the rest out from the tools."""
        names = labels[0] if len(labels) == 1 else ", ".join(labels[:-1]) + " and " + labels[-1]
        return f"Valio Aimo reports a problem with order{'' if len(labels) == 1 else 's'} {names}. Sort it out."

    def pick_order(c, ref):
        acct, ref = c["acct"], str(ref or "").strip().lower()
        if ref:  # an order id, or an order number: this account's first (numbers can repeat across accounts after a demo reset)
            o = g.store.order(ref) or next((x for x in g.store.orders(5000, acct["customer"]) if x["label"].lower() == ref), None) or \
                (next((x for x in g.store.orders(5000) if x["label"].lower() == ref), None) if c["op"] else None)
            if not o or (not c["op"] and o["wholesale"]["customer"] != acct["customer"]):
                raise a.g_error(404, "not_found", "No such order on this account.")
            return o
        live = [(o, a.state(o)) for o in g.store.orders(40, acct["customer"])]
        live = [(o, st) for o, st in live if st["status"] != "cancelled" and any(l["units"] > 0 for l in st["lines"])]
        if not live:
            raise a.g_error(409, "invalid_state", "This account has no order to report a problem on. Place one first.")
        return next((o for o, st in live if not st["open_issues"]), live[0][0])

    @r.post("/api/demo/issue", include_in_schema=False)
    def demo_issue(request: Request, response: Response, body: dict = Body(default={})):
        """Valio Aimo's side: report a short pick, a delay or a recall on an order. With an empty body: a short pick on the
        account's latest order. A visitor may do this on their own orders, the operator on any."""
        c = ctx(request, response)
        kind = str(body.get("kind") or "short_pick")
        o = pick_order(c, body.get("order"))
        by = "Valio Aimo (demo trigger)"
        if kind == "recall":
            rc, made = a.raise_recall(o, body.get("line"), body.get("reason"), by)
            mine = next((i for i in made if i["order_id"] == o["id"]), made[0])
            per = {}  # one request per customer: a recall can sit on several of its orders, and its agent settles them in one run
            for x in rc["affected"]:
                per.setdefault(x["customer"], {"customer": x["customer"], "buyer": x["buyer"], "acct": (g.account_of(x["customer"]) or {}).get("slug"), "orders": []})["orders"].append(x["order_label"])
            for v in per.values():
                v["request"] = ask(v["orders"])
            extra = {"recall": a.recall_view(rc["id"]), "issues": [a.summary(i, g.store.order(i["order_id"])) for i in made], "requests": list(per.values()),
                     "request": per[o["wholesale"]["customer"]]["request"]}
        else:
            mine = a.raise_issue(o, kind, line=body.get("line"), deliverable=body.get("delivered", body.get("deliverable")), days=body.get("days"), new_date=body.get("new_date"), reason=body.get("reason"), by=by)
            extra = {"issues": [a.summary(mine, o)]}
        return {"issue": a.summary(mine, o), "notice": mine["notice"], "order": {"id": o["id"], "label": o["label"], "url": order_url(o['id'])},
                "request": ask([o["label"]]), **extra}

    def full(c, rc) -> bool:
        """Valio Aimo (the operator) sees every row of a recall. So does a sandbox visitor, whose stock pool holds only their own orders."""
        return c["op"] or rc["world"] == c["acct"].get("world")

    @r.get("/api/demo/issues", include_in_schema=False)
    def demo_issues(request: Request, response: Response):
        """For the console: this account's issues, newest first, and the recalls that touch it."""
        c = ctx(request, response)
        out = []
        for (doc,) in g.store.q("select doc from issues where customer=? order by created desc limit 30", (c["acct"]["customer"],)):
            i = json.loads(doc)
            o = g.store.order(i["order_id"])
            if o:
                out.append(a.summary(a._sync(i), o))
        recalls = []
        for (doc,) in g.store.q("select doc from recalls where status in ('open','closed') order by created desc limit 30"):
            rc = json.loads(doc)
            if (full(c, rc) or any(x["customer"] == c["acct"]["customer"] for x in rc["affected"])) and a._orders_exist([x["order_id"] for x in rc["affected"]]):
                v = a.recall_view(rc["id"])
                if not full(c, rc):
                    v["affected"] = [x for x in v["affected"] if x["customer"] == c["acct"]["customer"]]
                    v["summary"] = {"orders": len(v["affected"]), "customers": len({x["customer"] for x in v["affected"]}),
                                     "units": sum(x["units"] for x in v["affected"]),
                                     "settled": sum(1 for x in v["affected"] if x["issue"]["status"] == "resolved"),
                                     "open": sum(1 for x in v["affected"] if x["issue"]["status"] != "resolved")}
                recalls.append({k: v[k] for k in ("id", "status", "blocked", "raised_at", "product", "lot", "summary", "url")})
        return {"issues": out, "recalls": recalls}

    @r.get("/api/recalls/{rid}", tags=["after the order"], summary="Recall trace: lot, product, every order and customer that got it, what each did, state now")
    def recall(rid: str, request: Request, response: Response):
        """Valio Aimo sees every order. Anyone else sees what is recalled, the counts, and their own orders only."""
        c, v = ctx(request, response), a.recall_view(rid)
        rc = a._recall(rid)
        if not full(c, rc):
            v["affected"] = [x for x in v["affected"] if x["customer"] == c["acct"]["customer"]]
            v["notice"] = None  # the signed notice lists every customer
            v["summary"] = {"orders": len(v["affected"]), "customers": len({x["customer"] for x in v["affected"]}),
                             "units": sum(x["units"] for x in v["affected"]),
                             "settled": sum(1 for x in v["affected"] if x["issue"]["status"] == "resolved"),
                             "open": sum(1 for x in v["affected"] if x["issue"]["status"] != "resolved")}
        return {**v, "scope": "all" if full(c, rc) else "own", "can_close": full(c, rc), "keys": {"seller": g.key.jwk}}

    @r.post("/api/recalls/{rid}/close", include_in_schema=False)
    def recall_close(rid: str, request: Request, response: Response):
        c, rc = ctx(request, response), a._recall(rid)
        if not rc or not full(c, rc):
            raise a.g_error(403, "forbidden", "Only Valio Aimo can close a recall.")
        return a.close_recall(rid, "Valio Aimo quality (demo)")

    @r.get("/recalls/{rid}", include_in_schema=False)
    def recall_page(rid: str):
        return page("recall.html")

    return r
