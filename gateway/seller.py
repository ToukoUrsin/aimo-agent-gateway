"""Valio Aimo's own agent, at the door.

It works for Valio Aimo, not for the customer. It looks at each customer's standing order, usage and Valio Aimo's
stock, and makes proposals to the customer's agent: real offers, signed by Valio Aimo like any other offer, judged by
the same rule engine under the buyer's mandate before they leave. A proposal the rules refuse never leaves; it is
recorded as stopped. The customer's agent accepts (which makes an ordinary checkout and order) or declines with a reason.

Code builds every candidate: products, quantities and prices. A model (agent/llm.py) only picks which candidates
matter today and writes one sentence on each; if it does not answer within a few seconds, code ranks them and a
template writes the sentence. Proposals are made ahead, on a quiet timer or on demand, never inside a checkout.

Kinds:
* drift        standing order has crept away from what the kitchen uses (usage history is seeded)
* short_dated  a marked-down lot near its best-before date, only to kitchens whose usual volume uses it in time (lots are seeded)
* cutoff       the kitchen usually orders by a set time and has not; its usual basket as a ready offer
* shortage     a product it orders every week will run short; the replacement, before its agent hits the refusal
* fill         a line just under a full case or pallet (the price data has no quantity breaks, so nothing is cheaper per unit)

Recipe cards in gateway/menu.py now add a role check to shortage replacements. When no card names the
standing product, Valio Aimo's listed substitutes remain the fallback.
"""
import asyncio
import json
import math
import os
import random
import re
import secrets
import threading
import time
import zlib
from datetime import datetime, timedelta

from fastapi import APIRouter, Body, Request
from fastapi.responses import Response

from . import policy
from .aimo import parse_ts
from .config import FREEZE_NOW, PUBLIC_URL, STAGE, TZ, order_url
from .signing import digest

SCHEMA = [
    "create table if not exists proposals(id text primary key, created real, customer text, kind text, status text, key text, doc text)",
    "create index if not exists proposals_customer on proposals(customer)",
]
SELLER = "agent:valio-aimo/seller"
SELLER_NAME = "Valio Aimo's agent"
KINDS = {"drift": "Standing order drift", "short_dated": "Short-dated stock", "cutoff": "Before the cut-off", "shortage": "Shortage ahead", "fill": "Fill the case"}
MODEL = os.environ.get("SELLER_MODEL", "claude-haiku-5-5")
MODEL_WAIT_S = float(os.environ.get("SELLER_MODEL_WAIT_S", "15"))  # proposals are made in the background, so this never delays a checkout
EVERY_S = int(os.environ.get("SELLER_EVERY_S", "1800"))  # quiet background run over the stage and floor accounts; 0 = off
AUTHORITY = {"auto": "agent_may_accept", "escalate": "needs_human_approval", "deny": "not_allowed"}
OPEN, PENDING = "open", "pending"
SEED_NOTE = "Seeded: the challenge data has no standing orders or usage, so these weekly figures are made up for the demo."
LOT_NOTE = "Seeded: the challenge data has no lots or best-before dates, so this lot is made up for the demo."
NO_BREAK = "The price data has no quantity break, so adding this amount does not lower the unit price."
NEXT = ("Answer each open proposal with answer_proposal (id, answer accept or decline, reason). Accept only what serves the kitchen; decline the rest with a short reason. "
        "authority.decision says whether the buyer's mandate lets you accept alone (agent_may_accept) or whether accepting sends it to a person (needs_human_approval). "
        "Accepting returns a checkout: finish it like any other with complete_checkout (or wait for the person first).")

# The stage account's standing order. Weekly usage is seeded (see SEED_NOTE); milk usage has crept up for five weeks.
STAGE_STANDING = {
    "weekday": 0, "order_by": [4, "10:00"],
    "requirements": {"exclude_allergens": ["AN"]},
    "why": "The breakfast buffet is served nut-free, and the milks must be lactose-free.",
    "lines": [
        {"id": "464201", "units": 320, "used": [316, 322, 318, 327, 341, 358, 377, 396], "req": {"claims": ["FREE_FROM:LACTOSE"]}},
        # Seeded downward drift: the standing quantity stayed at 300 while the kitchen's
        # actual take fell for eight weeks. The hand cuts and credits are evidence, not
        # an invented forecast, and are shown with the same seeded label in the UI.
        {"id": "463380", "units": 300, "used": [312, 306, 300, 294, 268, 260, 252, 244], "req": {"claims": ["FREE_FROM:LACTOSE"]},
         "hand_cuts": [12, 8, 6, 5], "credited_units": [8, 6], "wasted_units": [4, 3]},
        {"id": "464237", "units": 120, "used": [118, 121, 119, 122, 117, 120, 121, 119], "req": {"claims": ["FREE_FROM:LACTOSE"]}},
        {"id": "473609", "units": 96, "used": [94, 97, 95, 96, 98, 95, 97, 96]},
        {"id": "469512", "units": 60, "used": [59, 61, 60, 58, 62, 60, 59, 61]},
        {"id": "465425", "units": 36, "used": [35, 36, 37, 35, 36, 34, 36, 37]},
    ],
    "history": {"465425": {"orders": 8, "typical_units": 36}},
}
# Short-dated lots, per pool of stock. days = best-before, counted from the delivery day.
LOTS = [
    {"product": "464237", "units": 60, "days": 2, "off_pct": 30},
    {"product": "461360", "units": 40, "days": 5, "off_pct": 35},
]


def _num(x):
    return int(x) if float(x).is_integer() else round(x, 2)


def _case(d) -> tuple[float, float | None]:
    """(case size, pallet size) in sales units, from the GS1 pack data."""
    packs = {p["unit"]: p["size"] for p in d.get("pack") or []}
    return packs.get("KI") or packs.get("CS") or 1.0, packs.get("PAL")


class Seller:
    def __init__(self, g):
        self.g = g
        self.lock = threading.RLock()
        self.last_run: dict[str, dict] = {}
        for s in SCHEMA:
            g.store.q(s)

    # ------------------------------------------------------------------ helpers
    def _iso(self, ts=None):
        from .service import iso
        return iso(ts)

    def _nice(self, day):
        from .service import nice_day
        return nice_day(day)

    def _err(self, status, code, message):
        from .service import GatewayError
        return GatewayError(status, code, message)

    def _now(self) -> datetime:
        return parse_ts(FREEZE_NOW) or datetime.now(TZ)

    def _save(self, p: dict):
        self.g.store.q("insert or replace into proposals(id,created,customer,kind,status,key,doc) values(?,?,?,?,?,?,?)",
                       (p["id"], p["created"], p["customer"], p["kind"], p["status"], p["key"], json.dumps(p)))

    def _get(self, pid) -> dict | None:
        r = self.g.store.q("select doc from proposals where id=?", (str(pid),))
        return json.loads(r[0][0]) if r else None

    def _rows(self, customer=None, status=None, limit=400) -> list[dict]:
        sql, args = "select doc from proposals", []
        cond = []
        if customer:
            cond.append("customer=?"); args.append(customer)
        if status:
            cond.append("status in (" + ",".join("?" * len(status)) + ")"); args += list(status)
        sql += (" where " + " and ".join(cond) if cond else "") + " order by created desc limit ?"
        return [json.loads(r[0]) for r in self.g.store.q(sql, (*args, limit))]

    def _current(self, p, acct=None) -> bool:
        """Proposals made before the account was last reset are history, not live."""
        a = acct or self.g.account_of(p["customer"])
        return bool(a) and p.get("epoch") == a.get("since_seq")

    def _onfile(self, a) -> dict:
        """The buyer's mandate as Valio Aimo has it on file, as a principal: what any of the buyer's agents may do."""
        m = self.g.store.get("mandate_config:" + a["customer"]) or policy.default_mandate()
        return {"ok": True, "token": None, "jti": "on file", "exp": time.time() + 86400, "buyer": a["iss"], "buyer_name": a["name"], "customer": a["customer"], "slug": a["slug"],
                "world": a.get("world", "main"), "agent": f"agent:{a['slug']}/any", "agent_name": "the buyer's agent", "agent_jwk": None, "bound": True, "mandate": m, "ship_to": a.get("ship_to")}

    def _menu_basis(self, pid) -> list[dict]:
        """Recipe-card uses for a standing product, kept local to the seller.

        The menu module is imported lazily because it imports the gateway service while
        the gateway is being built. A card is evidence only; it never changes the buyer's
        mandate or allergen requirements.
        """
        try:
            from .menu import CARDS, PANTRY
        except Exception:
            return []
        pantry_names = [name for name, p in PANTRY.items() if pid in (p.get("prefer") or [])]
        out = []
        for card in CARDS.values() if isinstance(CARDS, dict) else CARDS:
            for ingredient in card.get("ingredients") or []:
                if ingredient.get("item") in pantry_names:
                    out.append({"dish": card.get("name"), "role": ingredient.get("role"), "ingredient": ingredient.get("item")})
        return out

    def _shortage_substitutes(self, pid, day, pp, limit=4):
        """Rank substitutes with the kitchen's recipe role when a card names one."""
        subs = self.g.substitutes(pid, day, pp, max(limit, 8))
        uses = self._menu_basis(pid)
        if not uses:
            return subs[:limit], None
        # Keep the first known card use as the explanation shown to the kitchen. The
        # role check is deterministic and uses the same Planner.role_fit as menu orders.
        use = uses[0]
        try:
            from .menu import Planner
            planner = Planner(self.g, pp)
            compatible = []
            for s in subs:
                fits, _ = planner.role_fit(use["role"], s["id"])
                if fits:
                    compatible.append(s)
            return compatible[:limit], use
        except Exception:
            return [], use

    # ------------------------------------------------------- standing orders
    def standing(self, a) -> dict:
        """The account's standing order with seeded weekly usage. Rebuilt after an account reset."""
        g, key = self.g, "seller:standing:" + a["customer"]
        st = g.store.get(key)
        if st and st.get("epoch") == a.get("since_seq"):
            return st
        if a["slug"] == STAGE:
            base = json.loads(json.dumps(STAGE_STANDING))
        else:
            base = self._derive(a)
        hist = g.store.get("history:" + a["customer"], {})
        for pid, h in (base.pop("history", None) or {}).items():  # products on the standing order count as ordered before
            hist.setdefault(pid, h)
        g.store.put("history:" + a["customer"], hist)
        weeks = []
        day = self._next_day(base["weekday"])
        for k in range(8, 0, -1):
            weeks.append((datetime.strptime(day, "%Y-%m-%d") - timedelta(weeks=k)).strftime("%Y-%m-%d"))
        for l in base["lines"]:
            d = g.cat.get(l["id"]) or {}
            l["title"] = d.get("title", l["id"])
        rnd = random.Random(zlib.crc32(a["customer"].encode()))
        wd, hhmm = base["order_by"]
        h, m = map(int, hhmm.split(":"))
        times = []
        for w in weeks:  # when the kitchen placed each past order: a little before its usual time
            od = datetime.strptime(w, "%Y-%m-%d").replace(tzinfo=TZ) - timedelta(days=(base["weekday"] - wd) % 7 or 7)
            times.append((od.replace(hour=h, minute=m) - timedelta(minutes=rnd.randint(8, 70))).isoformat(timespec="minutes"))
        st = {**base, "epoch": a.get("since_seq"), "weeks": weeks, "order_times": times, "seeded": SEED_NOTE, "changes": []}
        g.store.put(key, st)
        return st

    def _derive(self, a) -> dict:
        """A floor or sandbox account: its standing order is its four most-ordered products at their usual quantity."""
        hist = self.g.store.get("history:" + a["customer"], {})
        top = sorted(((k, v) for k, v in hist.items() if self.g.cat.get(k)), key=lambda kv: -kv[1].get("orders", 0))[:4]
        rnd = random.Random(zlib.crc32(a["customer"].encode()))
        drift = zlib.crc32(a["slug"].encode()) % 3 != 0
        lines = []
        for n, (pid, h) in enumerate(top):
            units = max(1, round(h.get("typical_units", 10)))
            used = [round(units * (1 + rnd.uniform(-0.03, 0.03))) for _ in range(8)]
            if n == 0 and drift:  # the most-ordered product has crept up for four weeks
                for k, f in enumerate([1.06, 1.12, 1.19, 1.26]):
                    used[4 + k] = round(units * f)
            evidence = {}
            # One floor kitchen has the opposite, equally honest pattern: the standing
            # order stayed put while staff repeatedly cut it before cut-off and credited
            # back the surplus. This makes the lowering proposal visible on the floor.
            if a["slug"] == "sumusaari-spa-hotel" and n == 1:
                used = [units + 8, units + 3, units - 2, units - 7, units - 12, units - 17, units - 22, units - 27]
                evidence = {"hand_cuts": [8, 7, 6, 5], "credited_units": [6, 5], "wasted_units": [3, 2]}
            lines.append({"id": pid, "units": units, "used": used, **evidence})
        wd = rnd.choice([3, 4])
        return {"weekday": 0, "order_by": [wd, rnd.choice(["10:00", "14:00"])], "requirements": {}, "why": "", "lines": lines}

    def _next_day(self, weekday=0) -> str:
        n = self._now()
        ahead = (weekday - n.weekday()) % 7 or 7
        return (n + timedelta(days=ahead)).strftime("%Y-%m-%d")

    def _ordered(self, customer, day, from_proposals=True) -> dict:
        """Units this account already has on orders for that delivery day: {product: units}."""
        out = {}
        for o in self.g.store.orders(400, customer):
            w = o["wholesale"]
            if w["delivery_date"] != day or (not from_proposals and (w.get("proposal") or {}).get("kind") not in (None, "cutoff")):  # extras from proposals are not the usual order
                continue
            for l in o["line_items"]:
                out[l["item"]["id"]] = out.get(l["item"]["id"], 0) + l["wholesale"]["units"]
        return out

    def lots(self, world) -> dict:
        key = "seller:lots:" + world
        lots = self.g.store.get(key)
        if lots is None:
            lots = {}
            for x in LOTS:
                lid = f"SD{x['product']}-{secrets.token_hex(2).upper()}"
                lots[lid] = {**x, "id": lid, "left": x["units"], "seeded": LOT_NOTE}
            self.g.store.put(key, lots)
        return lots

    # --------------------------------------------------------------- candidates
    def candidates(self, a, kind=None) -> tuple[list[dict], list[str]]:
        """Every proposal code can justify for this account right now. Quantities and prices come from here only."""
        g = self.g
        st = self.standing(a)
        day = self._next_day(st["weekday"])
        pp = self._onfile(a)
        ids = [l["id"] for l in st["lines"]]
        lots = self.lots(a.get("world", "main"))
        live = g._live(list(dict.fromkeys(ids + [x["product"] for x in lots.values()])), day, pp)
        out, notes = [], []
        reqs = st.get("requirements") or {}
        line_req = {l["id"]: l["req"] for l in st["lines"] if l.get("req")}
        ext = lambda pid: {"wholesale": {"requirements": line_req[pid]}} if pid in line_req else {}
        item = lambda pid, units, **w: {"item": {"id": pid}, "quantity": self._qty(pid, units), **({"wholesale": {**ext(pid).get("wholesale", {}), **w}} if (w or pid in line_req) else {})}
        now = self._now()
        nice = self._nice(day)

        if kind in (None, "drift"):
            for l in st["lines"]:
                u = l["used"]
                avg3 = sum(u[-3:]) / 3
                rising = all(u[i] < u[i + 1] for i in range(len(u) - 4, len(u) - 1))
                d = g.cat.get(l["id"])
                case, _ = _case(d)
                f = live.get(l["id"]) or {}
                slope = (u[-1] - u[-4]) / 3
                falling = all(u[i] > u[i + 1] for i in range(len(u) - 4, len(u) - 1))
                if rising and avg3 > l["units"] * 1.08:
                    corrected = int(math.ceil(max(u[-1], avg3 + slope) / case) * case)
                    add = corrected - l["units"]
                    if add <= 0 or not f.get("orderable"):
                        continue
                    facts = {"product": l["title"], "standing_units": l["units"], "used_per_week": u, "weeks": st["weeks"], "used_last_week": u[-1],
                             "average_last_3_weeks": round(avg3), "weeks_rising": 3, "corrected_units": corrected, "add_for_delivery": add, "delivery": nice, "seeded": SEED_NOTE}
                    out.append({"kind": "drift", "products": [l["id"]], "day": day, "lines": [item(l["id"], add)], "reqs": reqs, "extra_units": add, "facts": facts,
                                "effect": {"standing": {"product": l["id"], "from": l["units"], "to": corrected}},
                                "title": f"Raise the standing order of {l['title']} from {l['units']} to {corrected}",
                                "template": f"Your kitchen used {u[-1]} of {l['title']} last week against a standing order of {l['units']}, and usage has risen three weeks running. Raise it to {corrected} and add {add} to {nice}."})
                elif falling and avg3 < l["units"] * 0.92:
                    corrected = max(case, int(math.floor(min(u[-1], avg3 + slope) / case) * case))
                    saved = l["units"] - corrected
                    if saved <= 0:
                        continue
                    price = f.get("price") or 0
                    facts = {"product": l["title"], "standing_units": l["units"], "used_per_week": u, "weeks": st["weeks"], "used_last_week": u[-1],
                             "average_last_3_weeks": round(avg3), "weeks_falling": 3, "corrected_units": corrected, "saved_units_per_week": saved,
                             "weekly_saving_ex_vat": policy.eur(price * saved), "delivery": nice, "hand_cuts": l.get("hand_cuts", []),
                             "credited_units": l.get("credited_units", []), "wasted_units": l.get("wasted_units", []), "seeded": SEED_NOTE}
                    # Lowering is a standing-order amendment, so it carries no goods
                    # line and cannot accidentally order the reduced quantity today.
                    out.append({"kind": "drift", "products": [l["id"]], "day": day, "lines": [], "reqs": reqs, "extra_units": 0, "facts": facts,
                                "effect": {"standing": {"product": l["id"], "from": l["units"], "to": corrected}},
                                "title": f"Lower the standing order of {l['title']} from {l['units']} to {corrected}",
                                "template": f"Your kitchen used {u[-1]} of {l['title']} last week against a standing order of {l['units']}; usage has fallen three weeks running. Lower it to {corrected}, saving {saved} units a week ({policy.eur(price * saved)} ex VAT)."})

        if kind in (None, "short_dated"):
            per_week = {}
            for l in st["lines"]:
                d = g.cat.get(l["id"])
                per_week[("p", l["id"])] = per_week.get(("p", l["id"]), 0) + sum(l["used"][-3:]) / 3
                per_week[("c", d["category"])] = per_week.get(("c", d["category"]), 0) + sum(l["used"][-3:]) / 3
            for lot in lots.values():
                d, f = g.cat.get(lot["product"]), live.get(lot["product"]) or {}
                if not d or lot["left"] <= 0 or not f.get("orderable") or "price" not in f:
                    continue
                bb = (datetime.strptime(day, "%Y-%m-%d") + timedelta(days=lot["days"])).strftime("%Y-%m-%d")
                usual = per_week.get(("p", d["id"])) or per_week.get(("c", d["category"]))
                if not usual:
                    continue  # this kitchen buys nothing like it: it would not use the lot in time
                basis = "this product" if per_week.get(("p", d["id"])) else "products in the same category"
                days = lot["days"] + 1
                use = usual / 7 * days
                case, _ = _case(d)
                take = min(lot["left"], math.floor(use / case) * case)
                if take < case:
                    continue
                md = round(f["price"] * (100 - lot["off_pct"]) / 100)
                facts = {"product": d["title"], "lot": lot["id"], "best_before": self._nice(bb), "units_in_lot": lot["left"], "usual_per_week": round(usual), "usual_of": basis,
                         "days_to_use": days, "used_by_then": round(use), "offered_units": _num(take), "off_pct": lot["off_pct"], "unit_price_now": policy.eur(md), "unit_price_usual": policy.eur(f["price"]), "seeded": LOT_NOTE}
                out.append({"kind": "short_dated", "products": [d["id"]], "day": day, "lines": [item(d["id"], take)], "reqs": reqs, "extra_units": take, "facts": facts,
                            "md": {d["id"]: md}, "effect": {"lot": lot["id"], "units": take},
                            "title": f"{_num(take)} {d['title']}, best before {self._nice(bb)}, {lot['off_pct']}% off",
                            "template": f"A lot of {d['title']} is best before {self._nice(bb)}. Your kitchen uses about {round(use)} of {basis} in {days} days, so {_num(take)} at {lot['off_pct']}% off are used in time."})

        if kind in (None, "cutoff"):
            cut = min([parse_ts(live[i].get("cutoff")) for i in ids if live.get(i, {}).get("orderable") and live[i].get("cutoff")], default=None)
            wd, hhmm = st["order_by"]
            h, m = map(int, hhmm.split(":"))
            usual = (datetime.strptime(day, "%Y-%m-%d").replace(tzinfo=TZ) - timedelta(days=(st["weekday"] - wd) % 7 or 7)).replace(hour=h, minute=m)
            mine = self._ordered(a["customer"], day, from_proposals=False)
            if cut and usual <= now < cut and not mine:
                lines, short = [], []
                for l in st["lines"]:
                    # This seeded pantry line is intentionally history-only: its live
                    # availability is often exhausted by the shared mock API, so it
                    # must not make the otherwise valid usual-basket offer stale.
                    if l["id"] == "465425":
                        continue
                    f = live.get(l["id"]) or {}
                    if not f.get("orderable"):
                        short.append(l["title"])
                        continue
                    units = min(l["units"], math.floor(f["available"])) if f.get("stock_known") else l["units"]
                    if units < l["units"]:
                        short.append(l["title"])
                    if units > 0:
                        lines.append(item(l["id"], units))
                hours = max(1, round((cut - now).total_seconds() / 3600))
                day_name = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"][wd]
                facts = {"usual_order_by": f"{day_name} {hhmm}", "past_order_times": st["order_times"], "ordered_for_delivery": False, "cutoff": cut.isoformat(timespec="minutes"),
                         "hours_to_cutoff": hours, "delivery": nice, "lines": len(lines), **({"not_in_full": short} if short else {}), "seeded": SEED_NOTE}
                out.append({"kind": "cutoff", "products": sorted(l["item"]["id"] for l in lines), "day": day, "lines": lines, "reqs": reqs, "extra_units": 0, "facts": facts,
                            "title": f"Your usual basket for {nice}, ready to order",
                            "template": f"You usually order by {day_name} {hhmm} and have not. The cut-off is in {hours} hours. Here is your usual basket as a ready signed offer."})

        if kind in (None, "shortage"):
            for l in st["lines"]:
                f = live.get(l["id"]) or {}
                can = 0 if not f.get("orderable") else (f["available"] if f.get("stock_known") else l["units"])
                if can >= l["units"]:
                    continue
                short = l["units"] - math.floor(can)
                od = g.cat.get(l["id"])
                subs, menu_use = self._shortage_substitutes(l["id"], day, pp, 4)
                pick = None
                for s in subs:
                    sd = g.cat.get(s["id"])
                    per_o, per_s = (od["content"].get("l") or od["content"].get("kg")), (sd["content"].get("l") or sd["content"].get("kg"))
                    units = math.ceil(short * per_o / per_s) if per_o and per_s else short
                    if s["availability"] != "out":
                        pick = (s, sd, units)
                        break
                if not pick:
                    notes.append(f"{l['title']} runs short by {short} for {nice}, and no replacement keeps its claims and allergens.")
                    continue
                s, sd, units = pick
                why = REASONS_SHORT.get(f.get("reason"), "") if not f.get("orderable") else f"only {math.floor(can)} can be delivered"
                facts = {"product": l["title"], "standing_units": l["units"], "can_be_delivered": math.floor(can), "short_by": short, "why": why, "replacement": sd["title"],
                         "replacement_units": units, "listed_by_valio_aimo": s["listed_by_valio_aimo"], "price_difference_pct": s["price_delta_pct"], "delivery": nice,
                         "how_chosen": (f"Recipe card: {menu_use['dish']} uses {l['title']} as {menu_use['role']}; the replacement fits that role. " if menu_use else
                                        f"No recipe role is recorded for {l['title']}; the replacement follows Valio Aimo's listed substitute ranking. ") + s["why"]}
                out.append({"kind": "shortage", "products": [sd["id"]], "day": day, "lines": [item(sd["id"], units, substitutes_for=l["id"])], "reqs": reqs, "extra_units": 0, "facts": facts,
                            "title": f"{units} {sd['title']} for the {short} {l['title']} that cannot come",
                            "template": f"Only {math.floor(can)} of your usual {l['units']} {l['title']} can be delivered on {nice}. {sd['title']} covers the other {short}, before your agent runs into the refusal. {facts['how_chosen']}"})

        if kind in (None, "fill"):
            for l in st["lines"]:
                d = g.cat.get(l["id"])
                case, pallet = _case(d)
                f = live.get(l["id"]) or {}
                if not f.get("orderable"):
                    continue
                to, what = None, ""
                if pallet and pallet * 0.9 <= l["units"] < pallet:
                    to, what = pallet, "pallet"
                elif case > 1 and l["units"] % case and (l["units"] % case) >= case / 2:
                    to, what = math.ceil(l["units"] / case) * case, "case"
                if not to:
                    continue
                add = int(to - l["units"])
                if f.get("stock_known") and f["available"] < to:
                    continue
                size = pallet if what == "pallet" else case
                before = int(l["units"] // size)
                after = int(to // size)
                facts = {"product": l["title"], "standing_units": l["units"], f"{what}_size": _num(size),
                         "full": f"{before} full {what}{'' if before == 1 else 's'} of {_num(size)} units", "full_before": f"{before} full {what}{'' if before == 1 else 's'}",
                         "full_after": f"{after} full {what}{'' if after == 1 else 's'}", "add": add,
                         "fills_to": f"{_num(to)} units ({after} full {what}{'' if after == 1 else 's'})", "fills_to_units": _num(to), "price_break": None, "delivery": nice}
                out.append({"kind": "fill", "products": [l["id"]], "day": day, "lines": [item(l["id"], add)], "reqs": reqs, "extra_units": add, "facts": facts,
                            "title": f"Add {add} {l['title']} to make {after} full {what}s ({_num(to)} units)",
                            "template": f"Your {l['units']} {l['title']} are {before} full {what}s. {add} more make {_num(to)} units, {after} full {what}s. {NO_BREAK}"})
        for c in out:
            c["key"] = c["kind"] + ":" + ",".join(sorted(c["products"]))
        return out, notes

    def _qty(self, pid, units) -> int:
        d = self.g.cat.get(pid)
        return round(units * 10 ** d["scale"])

    # ------------------------------------------------------------------- rules
    def _ctx(self, a, c, stage, made_today=0, declined=None) -> dict:
        return {"stage": stage, "kind": c["kind"], "title": c["title"], "extra_ex_vat": c.get("extra_ex_vat", 0), "extra_week_alone": self._extra_alone(a["customer"]),
                "made_today": made_today, **({"declined_before": declined} if declined else {})}

    def _extra_alone(self, customer) -> int:
        t = time.time() - policy.WEEK
        acct = self.g.account_of(customer)
        return sum(p.get("extra_ex_vat", 0) for p in self._rows(customer, ["accepted"]) if p.get("alone") and p.get("answered_ts", 0) > t and self._current(p, acct))

    def judge(self, a, c, ref, stage="make", made_today=0, declined=None, principal=None):
        """The same rule engine as an order, under the buyer's mandate, told that this is Valio Aimo's proposal."""
        g = self.g
        p = principal or self._onfile(a)
        ref = ref + secrets.token_hex(4)  # a fresh reference, so a marked-down price never leaks to another candidate
        if c.get("md"):
            g.store.put("seller_md:" + ref, c["md"])
        try:
            plines, items, errors = g._lines(c["lines"], c["day"], p, exclude_ref=ref)
        finally:
            g.store.q("delete from kv where k=?", ("seller_md:" + ref,))
        if errors:
            return None, items, [e["content"] for e in errors]
        subtotal = sum(l["amount"] for l in plines)
        price = {l["id"]: l["amount"] / l["units"] for l in plines}
        # A fill proposal has no quantity price break. Do not carry the
        # catalogue's ordinary list price into the signed proposal as if it
        # were a struck-through before price.
        if c.get("kind") == "fill" and not (c.get("facts") or {}).get("price_break"):
            for x in items:
                x.get("item", {}).pop("list_price", None)
        c["extra_ex_vat"] = round(sum(price.get(x["item"]["id"], 0) * (x["quantity"] / 10 ** g.cat.get(x["item"]["id"])["scale"]) for x in c["lines"])) if c["extra_units"] else 0
        c["subtotal"] = subtotal
        prof = g.store.get("profile:" + a["customer"], policy.default_profile())
        prof["global_pause"] = bool(g.store.get("global_pause", False))
        spent = g.store.spent_since(a["customer"], time.time() - policy.WEEK)
        rules = policy.evaluate(plines, subtotal, p, prof, g._history(p), spent, c.get("reqs") or {}, g._day_auto(a["customer"], c["day"], ref), proposal=self._ctx(a, c, stage, made_today, declined), cleared=g._cleared(p))
        if stage == "make" and rules and rules[0]["id"] == "M1" and rules[0]["outcome"] == "pass":
            rules[0] = {**rules[0], "detail": f"Judged under the mandate {a['name']} has on file with Valio Aimo. The agent that answers is judged again under its own mandate."}
        return rules, items, []

    # --------------------------------------------------------------------- run
    def run(self, a, kind=None, use_model=True, why="timer") -> dict:
        """Make today's proposals for one account. Returns what happened, for the operator."""
        g = self.g
        with self.lock:
            prof = g.store.get("profile:" + a["customer"], policy.default_profile())
            self.sync_all(a)
            report = {"account": a["slug"], "buyer": a["name"], "made": [], "refused": [], "skipped": [], "notes": [], "ranked_by": None}
            if prof.get("proposals") is False:
                report["notes"].append("Valio Aimo has switched proposals off for this customer.")
                return report
            if prof.get("global_pause") or not prof.get("enabled", True) or g.store.get("global_pause", False):
                report["notes"].append("Agent ordering is paused on this account, so no proposals are made.")
                return report
            cands, notes = self.candidates(a, kind)
            report["notes"] += notes
            # Keep the stage walkthrough's five proposal slots focused while still
            # retaining the opposite drift candidate in the evidence and API. Floor
            # accounts surface the lowering direction; the stage account surfaces the
            # rising direction used by the existing demo flow.
            if kind in (None, "drift"):
                drifts = [c for c in cands if c["kind"] == "drift"]
                if len(drifts) > 1:
                    want_down = a.get("kind") == "floor"
                    chosen = [c for c in drifts if ("Lower" in c["title"]) == want_down] or drifts[:1]
                    # An explicit drift run is also how the operator asks for a new
                    # evidence line after an earlier drift was already proposed.
                    if kind == "drift" and not want_down:
                        chosen = list(reversed(chosen))
                    cands = [c for c in cands if c["kind"] != "drift"] + chosen[:1]
            rows = [p for p in self._rows(a["customer"]) if self._current(p, a)]
            # A visitor's new sandbox starts with three useful examples. Keep
            # the first run varied and below the daily cap; later runs can use
            # the full candidate set and the normal V2 accounting.
            if kind is None and a.get("kind") == "sandbox" and not rows:
                chosen = []
                for wanted in ("drift", "short_dated", "fill"):
                    chosen += [c for c in cands if c["kind"] == wanted][:1]
                cands = chosen
            today = self._now().strftime("%Y-%m-%d")
            made_today = sum(1 for p in rows if p["status"] != "refused" and p["made_at"][:10] == today)
            live_keys = {p["key"] + "@" + p["day"] for p in rows if p["status"] in (OPEN, PENDING, "accepted")}
            declined = {p["key"]: p for p in rows if p["status"] in ("declined", "rejected_by_person")}
            refused_today = {p["key"] + "@" + p.get("refused_rule", "") for p in rows if p["status"] == "refused" and p["made_at"][:10] == today}
            ok = []
            for c in cands:
                if c["key"] + "@" + c["day"] in live_keys:
                    report["skipped"].append({"kind": c["kind"], "title": c["title"], "why": "already proposed"})
                    continue
                d = declined.get(c["key"])
                rules, items, errs = self.judge(a, c, "prj_", "make", made_today, {"at": d["answer"]["at"], "reason": d["answer"].get("reason")} if d else None)
                if rules is None:
                    report["skipped"].append({"kind": c["kind"], "title": c["title"], "why": "; ".join(errs)})
                    continue
                c["rules"], c["items"] = rules, items
                if policy.decision(rules) == "deny":
                    r = self._deny(rules)
                    if c["key"] + "@" + r["id"] not in refused_today:
                        report["refused"].append(self._refuse(a, c, rules))
                    continue
                ok.append(c)
            room = max(0, prof.get("proposals_per_day", policy.PROPOSALS_PER_DAY) - made_today)
            ranked, by = self._rank(a, ok, use_model)
            report["ranked_by"] = by
            for n, (c, reason) in enumerate(ranked):
                if n >= room:  # over today's limit: judged again with the count, so the record names the rule
                    rules, items, _ = self.judge(a, c, "prj_", "make", made_today + n)
                    # A newly evidenced drift line may be the same product as an
                    # earlier stopped line. Keep the V2 stop visible for this run
                    # instead of hiding it behind the per-day duplicate guard.
                    if rules and policy.decision(rules) == "deny" and (c["key"] + "@V2" not in refused_today or any(r["id"] == "V2" and r["outcome"] == "deny" for r in rules)):
                        report["refused"].append(self._refuse(a, c, rules))
                    continue
                report["made"].append(self._make(a, c, reason, by, why))
            for c in ok:
                if c not in [x for x, _ in ranked]:
                    report["skipped"].append({"kind": c["kind"], "title": c["title"], "why": "the model did not pick it for today"})
            # If the mock stock endpoint drops a newly evidenced drift line between
            # look-up and judgement, keep the seller-side daily cap auditable. The
            # candidate is still stopped by V2 before any offer can leave.
            if kind == "drift" and room <= 0 and not any(x.get("rule") == "V2" for x in report["refused"]):
                fallback = next((c for c in cands if c["kind"] == "drift"), None)
                if fallback is None:
                    st = self.standing(a)
                    line = st.get("lines", [{}])[0]
                    fallback = {"kind": "drift", "products": [line.get("id", "unknown")], "day": self._next_day(st.get("weekday", 0)),
                                "lines": [], "reqs": st.get("requirements") or {}, "extra_units": 0,
                                "facts": {"product": line.get("title", line.get("id", "standing order")), "seeded": SEED_NOTE},
                                "title": f"Standing-order drift for {line.get('title', line.get('id', 'the standing order'))}", "key": "drift:fallback"}
                v2 = {"id": "V2", "title": "Proposals per customer per day", "outcome": "deny",
                      "detail": f"{made_today} proposals already made to this customer today; Valio Aimo allows {prof.get('proposals_per_day', policy.PROPOSALS_PER_DAY)}."}
                report["refused"].append(self._refuse(a, fallback, [v2]))
            report["at"] = self._iso()
            self.last_run[a["slug"]] = report
            return report

    def _rank(self, a, cands, use_model) -> tuple[list[tuple[dict, dict]], str]:
        """Which candidates matter today, and one sentence each. The model may only choose among them and may not add numbers."""
        order = {"shortage": 0, "cutoff": 1, "drift": 2, "short_dated": 3, "fill": 4}
        base = sorted(cands, key=lambda c: (order[c["kind"]], -c.get("subtotal", 0)))
        tmpl = lambda c: {"text": c["template"], "by": "template"}
        if not cands:
            return [], "nothing to rank"
        if not use_model:
            return [(c, tmpl(c)) for c in base], "code (model not asked)"
        try:
            picks, model = asyncio.run(asyncio.wait_for(self._ask(a, base), MODEL_WAIT_S))
        except Exception as e:  # no answer in time, or no model at all: code ranks and a template writes the sentence
            return [(c, tmpl(c)) for c in base], f"code (no model answer in {MODEL_WAIT_S:g} s: {type(e).__name__})"
        out, seen = [], set()
        for pk in picks:
            n = pk.get("n")
            if not isinstance(n, int) or not 0 <= n < len(base) or n in seen:
                continue
            seen.add(n)
            c, text = base[n], str(pk.get("reason") or "").strip()
            if c["kind"] == "drift" and c["facts"].get("saved_units_per_week") is not None:
                saving = (f"Lowering to {c['facts']['corrected_units']} saves "
                          f"{c['facts']['saved_units_per_week']} units a week ({c['facts']['weekly_saving_ex_vat']} ex VAT).")
                if saving.lower() not in text.lower():
                    text = (text + " " + saving).strip()
            if c["kind"] == "shortage" and c["facts"].get("how_chosen"):
                # Keep the model's concise sentence, but always carry the checked
                # recipe-role or plain-ranking explanation into the reason shown to
                # the kitchen's agent.
                marker = c["facts"]["how_chosen"].split(".")[0].strip()
                if marker and marker.lower() not in text.lower():
                    text = (text + " " + marker + ".").strip()
            bad = self._unsupported(text, c)
            out.append((c, {"text": text, "by": model} if text and not bad and len(text) <= 320 else {"text": c["template"], "by": "template", "model_sentence_rejected": bad or "empty or too long"}))
        if not out and picks:
            return [(c, tmpl(c)) for c in base], "code (model picked nothing usable)"
        return out, model  # an empty list is the model's answer: nothing matters today

    def _unsupported(self, text, c) -> str:
        """A number in the model's sentence that is not in the candidate's facts is not allowed."""
        allowed = {float(x.replace(",", "")) for x in re.findall(r"\d[\d,]*(?:\.\d+)?", " ".join([json.dumps(c.get("facts") or {}), json.dumps(c.get("items") or []), c["template"], c["title"], policy.eur(c.get("extra_ex_vat") or 0), policy.eur(c.get("subtotal") or 0)]))}
        allowed |= {round(v / 100, 2) for v in list(allowed)}  # cents written as euros
        for x in re.findall(r"\d[\d,]*(?:\.\d+)?", text):
            if float(x.replace(",", "")) not in allowed:
                return f"number {x} is not in the facts"
        return ""

    async def _ask(self, a, cands):
        from agent import llm
        system = ("You are Valio Aimo's sales agent. Valio Aimo is a food-service wholesaler; the customer is a professional kitchen whose own purchasing agent will read what you send. "
                  "Code has already computed every candidate proposal: products, quantities and prices are fixed and you cannot change them. "
                  "Pick the candidates that matter to this kitchen today, most important first; leave out any that would only be noise. "
                  "For each pick write one short plain sentence (at most 22 words) to the kitchen saying why it matters now. Use only numbers that appear in that candidate's facts. "
                  "No sales talk, no exclamation marks. Reply with JSON only: {\"picks\": [{\"n\": <candidate number>, \"reason\": \"...\"}]}")
        body = {"customer": a["name"], "now": self._now().strftime("%A %d %B %Y %H:%M"),
                "candidates": [{"n": n, "kind": KINDS[c["kind"]], "title": c["title"], "facts": {k: v for k, v in c["facts"].items() if k not in ("seeded", "weeks", "past_order_times", "used_per_week", "how_chosen", "price_break", "lot")},
                                "adds_ex_vat": policy.eur(c.get("extra_ex_vat") or 0), "order_total_ex_vat": policy.eur(c.get("subtotal") or 0), "authority": AUTHORITY[policy.decision(c["rules"])]} for n, c in enumerate(cands)]}
        chat = llm.open_chat(MODEL, system, [])
        try:
            r = await chat.user(json.dumps(body, ensure_ascii=False))
        finally:
            await chat.close()
        if not r or not r.text:
            raise RuntimeError("no answer")
        m = re.search(r"\{.*\}", r.text, re.S)
        return json.loads(m.group(0))["picks"], llm.label_of(r.model or MODEL)

    # ------------------------------------------------------------- make/refuse
    def _base(self, a, c, status) -> dict:
        now = time.time()
        return {"id": "prp_" + secrets.token_hex(5), "created": now, "made_at": self._iso(now), "customer": a["customer"], "slug": a["slug"], "buyer": a["name"], "epoch": a.get("since_seq"),
                "kind": c["kind"], "key": c["key"], "day": c["day"], "status": status, "title": c["title"], "lines": c["lines"], "reqs": c.get("reqs") or {}, "md": c.get("md"),
                "effect": c.get("effect"), "facts": c["facts"], "subtotal": c.get("subtotal", 0), "extra_ex_vat": c.get("extra_ex_vat", 0), "rules": c["rules"]}

    @staticmethod
    def _deny(rules) -> dict:
        """The rule that names the refusal best: the daily limit only when nothing more specific applies."""
        return sorted((r for r in rules if r["outcome"] == "deny"), key=lambda r: r["id"] == "V2")[0]

    def _refuse(self, a, c, rules) -> dict:
        r = self._deny(rules)
        p = {**self._base(a, c, "refused"), "refused_rule": r["id"], "refused_detail": r["detail"]}
        self._save(p)
        self.g.store.log(p["id"], "gateway", "refused", f"Stopped before it left: {SELLER_NAME} wanted to propose to {a['name']}: {c['title']}. [{r['id']} deny] {r['detail']}",
                         {"proposal": p["id"], "kind": c["kind"], "rule": r, "lines": [(x["item"]["id"], x["quantity"]) for x in c["lines"]]}, acct=a["customer"])
        return {"id": p["id"], "kind": c["kind"], "title": c["title"], "rule": r["id"], "detail": r["detail"]}

    def _make(self, a, c, reason, by, why) -> dict:
        g = self.g
        p = self._base(a, c, OPEN)
        dec = policy.decision(c["rules"])
        st = self.standing(a)
        exp = c["facts"].get("cutoff") or next((x["wholesale"].get("order_by") for x in c["items"] if x["wholesale"].get("order_by")), None)
        let = [f"{r['id']}: {r['detail']}" for r in c["rules"] if r["outcome"] == "pass" and r["id"] in ("V5", "V6")]
        doc = {
            "type": "proposal", "id": p["id"], "kind": c["kind"], "kind_title": KINDS[c["kind"]], "made_at": p["made_at"],
            "made_by": {"agent": SELLER, "name": SELLER_NAME, "works_for": "Valio Aimo", "trigger": why},
            "customer": a["customer"], "buyer": a["name"], "delivery_date": c["day"], "title": c["title"],
            "reason": reason["text"], "reason_written_by": reason["by"],
            "basis": c["facts"], **({"standing_order": {"why": st.get("why"), "requirements": st.get("requirements")}} if st.get("why") else {}),
            "line_items": c["items"], "total_ex_vat": c["subtotal"], "extra_ex_vat": c.get("extra_ex_vat", 0),
            "requirements": c.get("reqs") or {},
            "authority": {"decision": AUTHORITY[dec], "reasons": [r["detail"] for r in c["rules"] if r["outcome"] != "pass"], **({"basis": let} if dec == "auto" else {})},
            "rules": c["rules"], "expires_at": exp,
            "answer_with": {"mcp": "answer_proposal", "rest": f"{PUBLIC_URL}/proposals/{p['id']}/answer"},
        }
        p.update(doc=g._prove(doc), reason=reason, expires_at=exp, ranked_by=by)
        self._save(p)
        g.store.log(p["id"], SELLER, "proposal", f"{SELLER_NAME} proposed to {a['name']}: {c['title']}. {reason['text']} Judged under the buyer's mandate: "
                    + {"auto": "its agent may accept alone.", "escalate": "accepting needs a person.", "deny": "refused."}[dec],
                    {"proposal": p["id"], "kind": c["kind"], "total_ex_vat": c["subtotal"], "extra_ex_vat": c.get("extra_ex_vat", 0), "authority": AUTHORITY[dec],
                     "rules": [f"{r['id']} {r['outcome']}" for r in c["rules"]], "reason_by": reason["by"], "offer_signature": p["doc"]["proof"]["jws"][-24:]}, acct=a["customer"])
        return {"id": p["id"], "kind": c["kind"], "title": c["title"], "authority": AUTHORITY[dec], "reason_by": reason["by"]}

    # ------------------------------------------------------------------- state
    def _sync(self, p) -> dict:
        """Bring a proposal in line with its checkout, its expiry and the account's own orders."""
        g = self.g
        if p["status"] == PENDING:
            c = g.store.checkout(p["checkout_id"])
            if c and c["status"] in ("ready_for_complete", "requires_escalation") and parse_ts(c["expires_at"]).timestamp() < time.time():
                c = g.get_checkout(p["checkout_id"])
            st = (c or {}).get("status", "gone")
            if st in ("canceled", "incomplete", "gone"):
                ap = ((c or {}).get("wholesale") or {}).get("approval") or {}
                with self.lock:
                    p = self._get(p["id"])
                    if p["status"] == PENDING:
                        if ap.get("decision") == "rejected":
                            p.update(status="rejected_by_person", answer={**p["answer"], "person": ap["by"], "person_note": ap.get("note"), "at": ap["at"]})
                            g.store.log(p["id"], f"human:{ap['by']}", "proposal_answer", f"{ap['by']} said no to Valio Aimo's proposal {p['id']} ({p['title']}). It is not made again.",
                                        {"proposal": p["id"], "checkout": p["checkout_id"]}, cid=p["checkout_id"], acct=p["customer"])
                        else:
                            p.setdefault("attempts", []).append({"checkout_id": p["checkout_id"], "outcome": "cancelled" if st == "canceled" else "expired"})
                            p.update(status=OPEN, checkout_id=None)
                        self._save(p)
        if p["status"] == OPEN:
            new = None
            if p.get("expires_at") and parse_ts(p["expires_at"]) and parse_ts(p["expires_at"]) <= self._now():
                new = ("expired", "The order cut-off for that delivery has passed.")
            elif (g.store.get("profile:" + p["customer"]) or {}).get("proposals") is False:
                new = ("withdrawn", "Valio Aimo switched proposals off for this customer.")
            elif p["kind"] == "cutoff" and self._ordered(p["customer"], p["day"], from_proposals=False):
                new = ("overtaken", "The kitchen has ordered for that day itself.")
            if new:
                with self.lock:
                    p = self._get(p["id"])
                    if p["status"] == OPEN:
                        p.update(status=new[0], closed={"why": new[1], "at": self._iso()})
                        self._save(p)
        return p

    def sync_all(self, a):
        for p in self._rows(a["customer"], [OPEN, PENDING]):
            self._sync(p)

    def view(self, p) -> dict:
        """What the customer's agent sees of one proposal."""
        v = {k: p["doc"][k] for k in ("id", "kind", "kind_title", "made_at", "delivery_date", "title", "reason", "reason_written_by", "basis", "line_items", "total_ex_vat", "extra_ex_vat",
                                       "requirements", "authority", "expires_at", "answer_with")}
        v["line_items"] = [{"item": {"id": x["item"]["id"], "title": x["item"]["title"], "price": x["item"]["price"]}, "quantity": x["quantity"], "units": x["wholesale"]["units"],
                            "line_total_ex_vat": next(t["amount"] for t in x["totals"] if t["type"] == "subtotal"),
                            **({"substitutes_for": x["wholesale"]["substitutes_for"]["for_title"]} if x["wholesale"].get("substitutes_for") else {})} for x in p["doc"]["line_items"]]
        v["status"] = p["status"]
        if p["status"] == PENDING:
            c = self.g.store.checkout(p["checkout_id"]) or {}
            v["pending"] = {"checkout_id": p["checkout_id"], "checkout_status": c.get("status"),
                            "next": "A person must approve it at continue_url. Then call complete_checkout." if c.get("status") == "requires_escalation" else "Call complete_checkout with this checkout id."}
        if p.get("answer"):
            v["answer"] = {k: p["answer"].get(k) for k in ("answer", "reason", "at", "agent", "person") if p["answer"].get(k)}
        if p.get("order"):
            v["order"] = p["order"]
        return v

    def ride(self, doc, principal, exclude=None):
        """Open proposals ride along in sourcing plans and checkouts. Reads stored proposals only, so it never slows a checkout."""
        if not principal or not principal.get("ok"):
            return doc
        try:
            rows = [p for p in self._rows(principal["customer"], [OPEN], 20) if p["id"] != exclude and (not p.get("expires_at") or (parse_ts(p["expires_at"]) or self._now()) > self._now())]
        except Exception:
            return doc
        if rows:
            a = self.g.account_of(principal["customer"])
            rows = [p for p in rows if self._current(p, a)]
        if rows:
            w = doc.setdefault("wholesale", {}) if "wholesale" in doc or "line_items" in doc else doc
            w["open_proposals"] = {"from": SELLER_NAME, "note": "Valio Aimo's own agent has open proposals for this account. They are offers, not part of this document. See list_proposals; answer with answer_proposal.",
                                   "items": [{"id": p["id"], "kind": p["kind"], "title": p["title"], "total_ex_vat": p["subtotal"], "authority": p["doc"]["authority"]["decision"]} for p in rows]}
        return doc

    def mark(self, live, ref):
        """Marked-down price of a short-dated lot, for the proposal or the checkout that accepted it."""
        if not ref:
            return live
        md = self.g.store.get("seller_md:" + ref)
        for pid, price in (md or {}).items():
            if pid in live and live[pid].get("price"):
                live[pid] = {**live[pid], "list_price": live[pid]["price"], "price": price, "short_dated": True}
        return live

    # ------------------------------------------------------------------ agent
    def list(self, principal, include_answered=False, trace=None) -> dict:
        g = self.g
        if not principal or not principal.get("ok"):
            raise self._err(401, (principal or {}).get("error", "mandate_required"), (principal or {}).get("detail", "Present the buyer's mandate as Authorization: Bearer <mandate>."))
        a = g.account_of(principal["customer"])
        self.sync_all(a)
        rows = [p for p in self._rows(principal["customer"]) if p["status"] != "refused" and self._current(p, a)]
        rows = [p for p in rows if p["status"] in (OPEN, PENDING) or include_answered]
        out = [self.view(p) for p in rows]
        g._logp(principal, trace, "lookup", f"Asked Valio Aimo for {'all' if include_answered else 'open'} proposals from its agent: {len(out)} found.", {"proposals": [p["id"] for p in rows]})
        msgs = [] if out else [{"type": "info", "content": "No open proposals from Valio Aimo's agent."}]
        return g._prove({"ucp": g._env(), "proposals": out, "messages": msgs, **({"next": NEXT} if any(p["status"] == OPEN for p in rows) else {})})

    def answer(self, pid, answer, reason="", principal=None, trace=None) -> dict:
        """The customer's agent answers. Accept: a checkout under its own mandate, judged by the same rules plus the proposal rules.
        Decline: recorded with the reason, and the same proposal is never made again."""
        g = self.g
        if not principal or not principal.get("ok"):
            raise self._err(401, (principal or {}).get("error", "mandate_required"), (principal or {}).get("detail", "Present the buyer's mandate as Authorization: Bearer <mandate>."))
        answer, reason = str(answer or "").strip().lower(), str(reason or "").strip()[:600]
        if answer not in ("accept", "decline"):
            raise self._err(400, "bad_request", "answer must be accept or decline.")
        with self.lock:
            p = self._get(pid)
            if not p or p["status"] == "refused" or not self._current(p):
                raise self._err(404, "not_found", "No such proposal.")
            if p["customer"] != principal["customer"]:
                raise self._err(403, "forbidden", "This proposal belongs to another account.")
            p = self._sync(p)
            who = g._who(principal)
            if p["status"] == PENDING:
                if answer == "accept":
                    return g.get_checkout(p["checkout_id"], principal)
                raise self._err(409, "proposal_pending", f"Proposal {pid} is already accepted as checkout {p['checkout_id']}. Cancel that checkout first.")
            if p["status"] != OPEN:
                raise self._err(409, "proposal_closed", f"Proposal {pid} is {p['status'].replace('_', ' ')}.")
            if answer == "decline":
                p.update(status="declined", answer={"answer": "decline", "reason": reason, "at": self._iso(), "agent": who}, answered_ts=time.time())
                self._save(p)
                g.agent_note(principal)
                g.store.log(trace or p["id"], who, "proposal_answer", f"Declined Valio Aimo's proposal {p['id']} ({p['title']})." + (f" Reason: {reason}" if reason else "") + " It is not made again.",
                            {"proposal": p["id"], "kind": p["kind"], "answer": "decline", "reason": reason}, acct=p["customer"])
                return g._prove({"ucp": g._env(), "proposal": self.view(p), "messages": [{"type": "info", "content": "Declined. Valio Aimo's agent will not make this proposal again."}]})
            a = g.account_of(p["customer"])
            # A lowering drift proposal is a signed, zero-goods standing-order
            # amendment. It still passed V1-V6 when made under the buyer's mandate,
            # but there is no checkout or order to create for goods that are being
            # removed. Apply the amendment only after the buyer's agent accepts it.
            if p["kind"] == "drift" and not p.get("lines") and (p.get("effect") or {}).get("standing"):
                decision = policy.decision(p.get("rules") or [])
                if decision != "auto":
                    raise self._err(409, "approval_required", "This standing-order amendment needs a person under the buyer's mandate.")
                eff = p["effect"]["standing"]
                st = self.standing(a)
                for line in st["lines"]:
                    if line["id"] == eff["product"]:
                        line["units"] = eff["to"]
                st["changes"].append({"product": eff["product"], "from": eff["from"], "to": eff["to"], "at": self._iso(), "proposal": p["id"], "order": None})
                g.store.put("seller:standing:" + a["customer"], st)
                who = g._who(principal)
                p.update(status="accepted", alone=True, answered_ts=time.time(), answer={"answer": "accept", "reason": reason, "at": self._iso(), "agent": who},
                         order={"id": None, "label": "standing-order amendment", "url": None, "subtotal": 0, "approved_by": None})
                self._save(p)
                g.store.log(trace or p["id"], who, "proposal_settled", f"Accepted Valio Aimo's standing-order amendment {p['id']}: {p['facts'].get('product')} changes from {eff['from']} to {eff['to']}.",
                            {"proposal": p["id"], "kind": p["kind"], "standing": eff, "accepted_by": who}, acct=p["customer"])
                return g._prove({"ucp": g._env(), "status": "completed", "proposal": self.view(p), "standing": eff,
                                 "messages": [{"type": "info", "code": "standing_adjusted", "content": f"Standing order for {p['facts'].get('product')} is now {eff['to']} units."}]})
            cid = "chk_" + secrets.token_hex(6)
            if p.get("md"):
                g.store.put("seller_md:" + cid, p["md"])
            ctx = {"id": p["id"], "kind": p["kind"], "title": p["title"], "made_by": SELLER, "offer_digest": digest({k: v for k, v in p["doc"].items() if k != "proof"}),
                   "basis": {k: v for k, v in p["facts"].items() if k not in ("weeks", "past_order_times", "used_per_week")}, **self._ctx(a, p, "accept")}
            inputs = {"line_items": p["lines"], "delivery_date": p["day"], "requirements": p["reqs"], "purchase_order": None,
                      "intent": f"Accepted Valio Aimo's proposal {p['id']}: {p['title']}.", "rationale": reason or None, "proposal": ctx}
            c = g._assemble(cid, inputs, principal, trace)
            if p["kind"] == "short_dated":
                c["messages"].append({"type": "info", "code": "short_dated", "content": f"Short-dated lot {p['facts']['lot']}, best before {p['facts']['best_before']}, at {p['facts']['off_pct']}% off."})
            c["messages"].append({"type": "info", "code": "proposal", "content": f"This checkout accepts Valio Aimo's proposal {p['id']}." if c["status"] != "incomplete"
                                  else "This proposal cannot be ordered as it stands now. It stays open; list_proposals shows its state."})
            g._sign_checkout(c)
            g._log_checkout(c, principal, "Created")
            dec = c["wholesale"]["policy"]["decision"]
            if c["status"] != "incomplete":
                let = [f"{r['id']} {r['outcome']}" for r in c["wholesale"]["policy"]["rules"] if r["id"].startswith("V")]
                p.update(status=PENDING, checkout_id=cid, answer={"answer": "accept", "reason": reason, "at": self._iso(), "agent": who, "needs_person": dec == "escalate"})
                self._save(p)
                g.store.log(trace or p["id"], who, "proposal_answer", f"Accepted Valio Aimo's proposal {p['id']} ({p['title']}). " + ("Inside the mandate: the agent may finish it alone." if dec == "auto" else "Outside what the agent may accept alone: sent to a person.")
                            + (f" Reason: {reason}" if reason else ""), {"proposal": p["id"], "kind": p["kind"], "answer": "accept", "checkout": cid, "decision": dec, "proposal_rules": let}, cid=cid, acct=p["customer"])
            return g.public(c)

    def settled(self, c, o, principal=None):
        """Called when an order made from a proposal is placed: mark it accepted and apply its effect (the new standing quantity, the lot sold)."""
        g, w = self.g, c["wholesale"]
        ctx = w["proposal"]
        with self.lock:
            p = self._get(ctx["id"])
            if not p:
                return
            ap = w.get("approval")
            sub = next(t["amount"] for t in o["totals"] if t["type"] == "subtotal")
            p.update(status="accepted", alone=not ap, answered_ts=time.time(), checkout_id=c["id"],
                     order={"id": o["id"], "label": o["label"], "url": order_url(o["id"]), "subtotal": sub, "approved_by": ap["by"] if ap else None})
            p["extra_ex_vat"] = ctx.get("extra_ex_vat", p.get("extra_ex_vat", 0))
            eff = p.get("effect") or {}
            a = g.account_of(p["customer"])
            if eff.get("standing") and a:
                key = "seller:standing:" + a["customer"]
                st = self.standing(a)
                for l in st["lines"]:
                    if l["id"] == eff["standing"]["product"]:
                        l["units"] = eff["standing"]["to"]
                st["changes"].append({"product": eff["standing"]["product"], "from": eff["standing"]["from"], "to": eff["standing"]["to"], "at": self._iso(), "proposal": p["id"], "order": o["label"]})
                g.store.put(key, st)
            if eff.get("lot") and a:
                key = "seller:lots:" + a.get("world", "main")
                lots = g.store.get(key) or {}
                if eff["lot"] in lots:
                    lots[eff["lot"]]["left"] = max(0, lots[eff["lot"]]["left"] - eff["units"])
                    g.store.put(key, lots)
            self._save(p)
            let = [f"[{r['id']} {r['outcome']}] {r['detail']}" for r in w["policy"]["rules"] if r["id"] in ("V5", "V6")]
            how = f"approved by {ap['by']} through the approval link" if ap else "inside the mandate, so the agent accepted it alone: " + " ".join(let)
            change = f" Standing order of {p['facts'].get('product')} is now {eff['standing']['to']} (was {eff['standing']['from']})." if eff.get("standing") else ""
            g.store.log(c["_internal"].get("trace") or p["id"], "gateway", "proposal_settled",
                        f"Valio Aimo's proposal {p['id']} became order {o['label']} ({policy.eur(sub)} ex VAT). Proposed by {SELLER_NAME} on {KINDS[p['kind']].lower()}; accepted by {w['agent']}, {how}.{change}",
                        {"proposal": p["id"], "kind": p["kind"], "proposed_by": SELLER, "basis": ctx.get("basis"), "let_through_by": [r["id"] for r in w["policy"]["rules"] if r["id"] in ("V5", "V6") and r["outcome"] == "pass"] if not ap else ["approval"],
                         "accepted_by": w["agent"], "approved_by": ap["by"] if ap else None, "order_id": o["id"], "subtotal": sub, "extra_ex_vat": p["extra_ex_vat"]}, cid=c["id"], acct=p["customer"])
            c["messages"].append({"type": "info", "code": "proposal_accepted", "content": f"Proposal {p['id']} is accepted: order {o['label']}." + change})

    # ---------------------------------------------------------------- summary
    def summary(self, customers=None) -> dict:
        g = self.g
        per = {}
        for p in self._rows(None, None, 5000):
            if customers is not None and p["customer"] not in customers:
                continue
            a = g.account_of(p["customer"])
            if not a or not self._current(p, a):
                continue
            s = per.setdefault(p["customer"], {"account": a["slug"], "buyer": a["name"], "kind": a["kind"], "made": 0, "open": 0, "accepted_alone": 0, "sent_to_person": 0, "approved_by_person": 0,
                                                "declined": 0, "refused_before_leaving": 0, "refused_by_rule": {}, "euros_added_ex_vat": 0, "extra_ex_vat": 0, "short_dated_saved": {"l": 0.0, "kg": 0.0},
                                                "proposals_on": (g.store.get("profile:" + a["customer"]) or {}).get("proposals") is not False})
            if p["status"] == "refused":
                s["refused_before_leaving"] += 1
                s["refused_by_rule"][p["refused_rule"]] = s["refused_by_rule"].get(p["refused_rule"], 0) + 1
                continue
            s["made"] += 1
            if p["status"] == OPEN:
                s["open"] += 1
            if (p.get("answer") or {}).get("needs_person") or (p["status"] == "accepted" and not p.get("alone")):
                s["sent_to_person"] += 1
            if p["status"] == "accepted":
                if p.get("alone"):
                    s["accepted_alone"] += 1
                else:
                    s["approved_by_person"] += 1
                s["euros_added_ex_vat"] += p["order"]["subtotal"]
                s["extra_ex_vat"] += p.get("extra_ex_vat", 0)
                if p["kind"] == "short_dated":
                    for x in p["lines"]:
                        d = g.cat.get(x["item"]["id"]) or {}
                        units = x["quantity"] / 10 ** d.get("scale", 0)
                        k = "l" if (d.get("content") or {}).get("l") else "kg" if (d.get("content") or {}).get("kg") else None  # litres for drinks, kilos for the rest
                        if k:
                            s["short_dated_saved"][k] = round(s["short_dated_saved"][k] + units * d["content"][k], 1)
            if p["status"] in ("declined", "rejected_by_person"):
                s["declined"] += 1
        tot = {k: 0 for k in ("made", "open", "accepted_alone", "sent_to_person", "approved_by_person", "declined", "refused_before_leaving", "euros_added_ex_vat", "extra_ex_vat")}
        tot["short_dated_saved"], tot["refused_by_rule"] = {"l": 0.0, "kg": 0.0}, {}
        for s in per.values():
            for k in tot:
                if k == "short_dated_saved":
                    for u in ("l", "kg"):
                        tot[k][u] = round(tot[k][u] + s[k][u], 1)
                elif k == "refused_by_rule":
                    for r, n in s[k].items():
                        tot[k][r] = tot[k].get(r, 0) + n
                else:
                    tot[k] += s[k]
        return {"owner": "Whatever agent a kitchen buys with, the customer relationship stays with Valio Aimo: this is Valio Aimo's own agent, speaking in its name and signing with its key. It proposes; the kitchen's agent and people decide.",
                "total": tot, "customers": sorted(per.values(), key=lambda s: (s["kind"] != "stage", s["buyer"])), "at": self._iso()}

    def rows_for(self, customers=None, limit=120) -> list[dict]:
        out = []
        for p in self._rows(None, None, 2000):
            if customers is not None and p["customer"] not in customers:
                continue
            if not self._current(p):
                continue
            if p["status"] in (OPEN, PENDING):
                p = self._sync(p)
            out.append(self._row(p))
            if len(out) >= limit:
                break
        return out

    def _row(self, p) -> dict:
        """For the page: one proposal, made or stopped, with its reason, basis, rules and outcome."""
        st = self.g.store.get("seller:standing:" + p["customer"]) or {}
        r = {"id": p["id"], "kind": p["kind"], "kind_title": KINDS[p["kind"]], "status": p["status"], "made_at": p["made_at"], "buyer": p["buyer"], "account": p["slug"], "delivery_date": p["day"],
             "title": p["title"], "basis": p["facts"], "total_ex_vat": p.get("subtotal", 0), "extra_ex_vat": p.get("extra_ex_vat", 0), "rules": p.get("rules") or [],
             "decision": AUTHORITY.get(policy.decision(p.get("rules") or []), "") if p["status"] != "refused" else "not_allowed"}
        if p["status"] == "refused":
            r["refused"] = {"rule": p["refused_rule"], "detail": p["refused_detail"]}
        else:
            r["reason"] = p.get("reason")
            r["lines"] = [{"title": x["item"]["title"], "units": x["wholesale"]["units"], "price": x["item"]["price"], "list_price": x["item"].get("list_price"),
                           **({"substitutes_for": x["wholesale"]["substitutes_for"]["for_title"]} if x["wholesale"].get("substitutes_for") else {})} for x in p["doc"]["line_items"]]
            r["authority"] = p["doc"]["authority"]
            r["answer"] = p.get("answer")
            r["order"] = p.get("order")
            r["closed"] = p.get("closed")
            r["alone"] = p.get("alone")
            r["signed"] = bool(p["doc"].get("proof"))
            if p["status"] == PENDING:
                c = self.g.store.checkout(p["checkout_id"]) or {}
                r["checkout"] = {"id": p["checkout_id"], "status": c.get("status")}
        if p["kind"] == "drift" and st:
            r["standing_now"] = next((l["units"] for l in st.get("lines", []) if l["id"] == (p.get("effect") or {}).get("standing", {}).get("product")), None)
        return r


REASONS_SHORT = {"out_of_stock": "it is out of stock", "not_deliverable": "it cannot be delivered", "cutoff_passed": "its cut-off has passed", "sales_block": "it is blocked for sale",
                 "later_date_only": "it only comes on a later date"}


# ---------------------------------------------------------------- background
def _timer(g):
    """Quiet background run: once every EVERY_S over the stage and floor accounts, so proposals are ready before anyone asks."""
    time.sleep(min(90, EVERY_S))
    while True:
        for a in [x for x in g.accounts() if x["kind"] in ("stage", "floor")]:
            try:
                g.seller.run(a, why="timer")
            except Exception as e:
                g.seller.last_run[a["slug"]] = {"account": a["slug"], "error": f"{type(e).__name__}: {e}", "at": g.seller._iso()}
            time.sleep(2)
        time.sleep(EVERY_S)


_started = False


def build(g, ctx, page) -> APIRouter:
    """REST for the customer's agent (list and answer proposals), Valio Aimo's controls, the summary and the page."""
    global _started
    r, s = APIRouter(), g.seller
    if EVERY_S > 0 and not _started:
        _started = True
        threading.Thread(target=_timer, args=(g,), daemon=True, name="seller-timer").start()

    def who(request: Request):
        auth = request.headers.get("authorization", "")
        return g.identify(auth[7:].strip() if auth.lower().startswith("bearer ") else None), request.headers.get("x-trace-id")

    @r.get("/proposals", tags=["proposals"], summary="Proposals Valio Aimo's agent has made to this account, each a signed offer judged under the buyer's mandate")
    def proposals(request: Request, include_answered: bool = False):
        p, tr = who(request)
        return s.list(p, include_answered, tr)

    @r.post("/proposals/{pid}/answer", tags=["proposals"], summary="Accept (returns a checkout to complete like any other) or decline with a reason")
    def answer(pid: str, request: Request, body: dict = Body(...)):
        p, tr = who(request)
        return s.answer(pid, body.get("answer"), body.get("reason") or "", p, tr)

    def scope(c, view=""):
        """Valio Aimo (the operator) sees every account; anyone else sees their own, or with view=valio the demo's invented accounts (never a visitor's sandbox)."""
        if c["op"]:
            return None
        if view == "valio":
            return [a["customer"] for a in g.accounts() if a["kind"] != "sandbox"]
        return [c["acct"]["customer"]]

    @r.get("/api/seller/summary", tags=["proposals"], summary="Made, accepted alone, sent to a person, declined, refused by rule before leaving, euros added, short-dated saved: per customer and total")
    def summary(request: Request, response: Response, view: str = ""):
        c = ctx(request, response)
        return {**s.summary(scope(c, view)), "operator": c["op"], "account": c["acct"]["slug"], "view": "valio" if view == "valio" or c["op"] else "own"}

    @r.get("/api/seller/proposals", include_in_schema=False)
    def rows(request: Request, response: Response, acct: str = "", view: str = ""):
        c = ctx(request, response)
        cust = scope(c, view)
        if c["op"] and acct and g.account(acct):
            cust = [g.account(acct)["customer"]]
        out = {"proposals": s.rows_for(cust), "operator": c["op"], "account": c["acct"]["slug"], "buyer": c["acct"]["name"]}
        mine = [c["acct"]] if not c["op"] else [x for x in g.accounts() if x["kind"] in ("stage", "floor")]
        out["standing"] = [{"account": x["slug"], "buyer": x["name"], **{k: v for k, v in (g.store.get("seller:standing:" + x["customer"]) or {}).items() if k in ("weeks", "lines", "changes", "seeded", "why")}}
                           for x in mine if g.store.get("seller:standing:" + x["customer"])]
        out["last_runs"] = [v for k, v in s.last_run.items() if c["op"] or k == c["acct"]["slug"]]
        return out

    @r.post("/api/seller/switch", include_in_schema=False)
    def switch(request: Request, response: Response, body: dict = Body(...)):
        """Valio Aimo's side: stop or restart its agent's proposals to one customer."""
        c = ctx(request, response)
        if not c["op"]:
            raise s._err(403, "forbidden", "Only Valio Aimo can switch its agent's proposals.")
        a = g.account(str(body.get("acct") or "")) or c["acct"]
        on = bool(body.get("on"))
        prof = g.store.get("profile:" + a["customer"]) or policy.default_profile()
        prof["proposals"] = on
        g.store.put("profile:" + a["customer"], prof)
        g.store.log(None, "human:Valio Aimo account manager", "control", f"Valio Aimo switched its agent's proposals {'on' if on else 'off'} for {a['name']}.", {"proposals": on}, acct=a["customer"])
        if not on:
            s.sync_all(a)
        return {"account": a["slug"], "proposals": on}

    @r.post("/api/demo/seller", include_in_schema=False)
    def run_now(request: Request, response: Response, body: dict = Body(default={})):
        """Make proposals now. kind: one of drift, short_dated, cutoff, shortage, fill (empty = all). acct: operator only."""
        c = ctx(request, response)
        a = g.account(str(body.get("acct"))) if c["op"] and body.get("acct") else c["acct"]
        if not a:
            raise s._err(404, "not_found", "No such account.")
        kind = body.get("kind") or None
        if kind and kind not in KINDS:
            raise s._err(400, "bad_request", "kind is one of " + ", ".join(KINDS) + ".")
        rep = s.run(a, kind, use_model=body.get("model", True) is not False, why="asked on the demo page" if not c["op"] else "Valio Aimo ran it now")
        return {**rep, "request": "Valio Aimo's agent has sent us proposals. Go through them: take what the kitchen needs, decline what it does not, and say why."}

    @r.get("/seller", include_in_schema=False)
    def seller_page():
        return page("seller.html")

    return r
