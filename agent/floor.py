"""The floor: nine made-up customers ordering from Valio Aimo at the same time, each through its own agents.

    uv run -q python -m agent.floor --base http://127.0.0.1:8642 --minutes 3
    uv run -q python -m agent.floor --base http://127.0.0.1:8642 --rush        (the Monday rush, see the end of this file)

Every agent here is an outside party. It holds its own key, carries a mandate signed by its company and
talks to the gateway over the public REST interface, exactly like the single agents in script.py and
rogue.py. Nothing is written into the gateway's tables to make a result appear: an order exists because
the gateway accepted it, a refusal because a rule in policy.py refused it.

What is simulated around the agents:
* each company's approver, who opens the approval link after a few seconds and says yes or no;
* Valio Aimo's account manager, who pauses one account mid-run and switches it back on (operator key);
* the companies' own systems, which sign the mandates (POST /api/tower/floor/mandate stands in for them).

The names are invented. All accounts share one stock pool ("floor"), separate from the stage account.
Runs are paced: the gateway caches the challenge's price and stock API, and the floor keeps to about
two gateway calls a second.
"""
import argparse
import asyncio
import json
import math
import random
import re
import secrets
import sys
import time
from collections import Counter
from datetime import datetime, timezone

import httpx
from cryptography.hazmat.primitives.asymmetric import ec

from gateway.config import OPERATOR_KEY, TZ
from gateway.signing import Key

from .common import BuyerSide, errors, eur

DAY = "2026-11-16"   # the one delivery day the challenge's price and stock API answers for
ROUND_S = 20         # one round: every agent that is due takes one turn, at a random moment inside it
BASE_ROUNDS = 9      # baskets are sized for a 3 minute run; longer runs order smaller amounts more often
PACE_S = 0.5         # least time between two calls to the gateway
COOL_S = 20          # how long every agent waits when the gateway says it is busy
LLM_MODEL = "claude-haiku-5-5"
MAX_LLM = 1          # the model pool is shared: one model agent at a time, and none unless asked for
NUTS = ["AN", "AP", "SA", "SC", "SH", "SM", "SP", "SR", "ST", "SW"]


class FloorError(Exception):
    """The run cannot go on (wrong operator key, another run active, old server)."""


def mandate(per_order, per_week, sub="listed", sub_pct=10, new="ask", exclude=(), approver="F&B manager") -> dict:
    """What the company lets its agents commit to. Amounts in cents, ex VAT."""
    return {"type": "open_checkout", "budget": {"currency": "EUR", "max_per_order": per_order, "max_per_week": per_week}, "categories": ["*"], "exclude_allergens": list(exclude),
            "substitution": {"allow": sub, "max_unit_price_increase_pct": sub_pct}, "new_products": new, "quantity_anomaly_factor": 3, "approver": {"name": approver, "channel": "link"}}


def bought(usual: dict) -> dict:
    """Order history: product id -> usual units. The number of past orders is made up but stable."""
    return {pid: {"orders": 8 + int(pid) % 37, "typical_units": units} for pid, units in usual.items()}


# kind: standing (fixed basket over REST, no model) | planner (asks /sourcing/plan, picks by a fixed rule)
#       watch (look-ups only) | unvetted (a careless third-party bot) ; keyless = a signed-in assistant with no key.
# short: what a standing-order bot does when stock is short: substitute (only what its mandate allows alone),
#        ask (take the first alternative and let a person decide), partial (order what is there).
CAST = [
    {"slug": "sumusaari-spa-hotel", "name": "Sumusaari Spa Hotel", "ship_to": "Sumusaari Spa Hotel, kitchen door", "po": "SSH", "approver": "Aino, head chef",
     "mandate": mandate(600000, 2500000, sub="any", sub_pct=20, approver="Head chef"),
     "history": bought({"463380": 48, "467337": 4, "466540": 12, "464517": 5, "461747": 8, "462150": 10, "464249": 20, "473775": 5}),
     "agents": [
         {"id": "breakfast-bot", "name": "Breakfast standing order", "kind": "standing", "short": "substitute", "intent": "Breakfast buffet standing order",
          "lines": [("463380", 48), ("467337", 4), ("466540", 12), ("464517", 5), ("461747", 8)]},
         {"id": "kitchen-planner", "name": "Kitchen planner", "kind": "planner", "rule": "usual", "every": 2, "offset": 1,
          "needs": [("salmon", 10, "kg", "462150"), ("cooking cream lactose free", 20, "L"), ("jasmin rice", 25, "kg")]},
     ]},
    {"slug": "hallavaara-hotels", "name": "Hallavaara Hotels", "ship_to": "Hallavaara Hotel Centrum, goods lift 2", "po": "HVH", "approver": "Mikael, purchasing manager",
     "mandate": mandate(300000, 1200000, approver="Purchasing manager"),
     "history": bought({"467337": 8, "466540": 24, "469512": 24, "463203": 6, "463380": 60, "463797": 10, "464249": 20, "464201": 80}),
     "agents": [
         {"id": "breakfast-bot", "name": "Breakfast standing order", "kind": "standing", "short": "ask", "every": 2, "intent": "Breakfast standing order for three hotels",
          "lines": [("467337", 8), ("466540", 24), ("469512", 24), ("463203", 6), ("463380", 60)]},
         {"id": "banquet-planner", "name": "Banquet planner", "kind": "planner", "rule": "reliable", "every": 3, "offset": 1,
          "needs": [("cooking cream lactose free", 20, "L"), ("lactose-free milk", 80, "L"), ("orange juice", 30, "L")]},
     ],
     "llm": ["We need 60 litres of lactose-free milk for the breakfast buffets, delivered Monday 16 November 2026. Take what we usually buy.",
             "Order 24 litres of barista oat drink for delivery on Monday 16 November 2026. Our usual one if it is in stock."]},
    {"slug": "pulpetti-school-meals", "name": "Pulpetti School Meals", "ship_to": "Pulpetti central kitchen, ramp A", "po": "PSM", "approver": "Leena, kitchen manager",
     "mandate": mandate(500000, 1500000, exclude=NUTS, approver="Kitchen manager"),
     "history": bought({"464201": 120, "476591": 30, "463190": 20, "461577": 15, "461747": 6, "473775": 8, "460596": 60, "461660": 20}),
     "agents": [
         {"id": "staples-bot", "name": "Weekly staples bot", "kind": "standing", "short": "substitute", "intent": "Weekly staples for the school kitchens",
          "lines": [("464201", 120), ("476591", 30), ("463190", 20), ("461577", 15), ("461747", 6)]},
         {"id": "menu-planner", "name": "Menu planner", "kind": "planner", "rule": "usual", "every": 2,
          "needs": [("jasmin rice", 40, "kg"), ("potato", 60, "kg"), ("french fries", 50, "kg")],
          "script": [{"at": 0.55, "lines": [("465250", 4)], "intent": "Pastries for the staff day"}]},
         {"id": "lunchbox-deals-bot", "name": "Lunchbox Deals (unvetted)", "kind": "unvetted", "start": 0.25, "ids": ["466942", "463047", "464201"]},
     ]},
    {"slug": "tyrskylinja-ferries", "name": "Tyrskylinja Ferries", "ship_to": "Tyrskylinja terminal, quay 4 stores", "po": "TLF", "approver": "Jari, catering manager",
     "mandate": mandate(600000, 2500000, sub="any", sub_pct=15, new="allow", approver="Catering manager"),
     "history": bought({"461660": 30, "462150": 10, "463797": 12, "463511": 4, "467337": 2, "469512": 30, "472598": 30, "466540": 30, "464201": 60}),
     "agents": [
         {"id": "galley-bot", "name": "Galley standing order", "kind": "standing", "short": "substitute", "every": 2, "intent": "Galley standing order",
          "lines": [("461660", 30), ("462150", 10), ("463797", 12), ("463511", 4), ("467337", 2)]},
         {"id": "cafe-deck-planner", "name": "Cafe deck planner", "kind": "planner", "rule": "reliable", "every": 2, "offset": 1,
          "needs": [("oat drink barista", 30, "L"), ("orange juice", 30, "L"), ("lactose-free milk", 60, "L")]},
     ],
     "pause": (0.5, 0.74)},  # Valio Aimo pauses this account for a part of the run
    {"slug": "kaneli-corner-cafes", "name": "Kaneli Corner Cafes", "ship_to": "Kaneli Corner, back door on the yard side", "po": "KCC", "approver": "Sanna, owner",
     "mandate": mandate(50000, 150000, approver="Owner"),
     "history": bought({"473609": 24, "472598": 12, "463380": 24, "467337": 2, "466540": 6}),
     "agents": [
         {"id": "barista-bot", "name": "Barista stock bot", "kind": "standing", "short": "ask", "every": 2, "intent": "Cafe restock",
          "lines": [("473609", 24), ("472598", 12), ("463380", 24), ("467337", 2), ("466540", 6)],
          "script": [{"at": 0.4, "lines": [("473609", 240), ("472598", 12), ("463380", 24)], "intent": "Cafe restock", "fixed": True}]},  # a typo: ten times the usual
     ]},
    {"slug": "koivukumpu-hospital", "name": "Koivukumpu Hospital Kitchen", "ship_to": "Koivukumpu Hospital, kitchen receiving K1", "po": "KHK", "approver": "Tuula, nutrition manager",
     "mandate": mandate(800000, 4000000, sub="none", approver="Nutrition manager"), "profile": {"tier": "reorder"},
     "history": bought({"464201": 150, "468197": 8, "461577": 20, "460643": 25, "464256": 4}),
     "agents": [
         {"id": "ward-kitchen-bot", "name": "Ward kitchen standing order", "kind": "standing", "short": "partial", "every": 2, "offset": 1, "intent": "Ward kitchens standing order",
          "lines": [("464201", 150), ("468197", 8), ("461577", 20), ("460643", 25), ("464256", 4)]},
         {"id": "diets-planner", "name": "Special diets planner", "kind": "planner", "rule": "cheapest", "every": 3, "offset": 1,
          "needs": [("gluten free oat drink", 24, "L"), ("cooking cream lactose free", 20, "L")]},
     ]},
    {"slug": "ratas-works-canteen", "name": "Ratas Works Canteen", "ship_to": "Ratas Works, canteen loading bay", "po": "RWC", "approver": "Pekka, restaurant manager",
     "mandate": mandate(200000, 800000, approver="Restaurant manager"), "profile": {"tier": "read"},
     "history": bought({"464201": 60, "463511": 4, "461660": 20, "464256": 4}),
     "agents": [
         {"id": "price-watch", "name": "Price watch", "kind": "watch", "every": 2, "ids": ["464201", "463511", "461660", "464256", "473775", "463190", "460596", "464411", "476601", "469058", "463254", "466983"],
          "script": [{"at": 0.3, "lines": [("464201", 60), ("461660", 20)], "intent": "Prices are good today, order the usual"},
                     {"at": 0.75, "lines": [("463511", 4)], "intent": "Rice is running low"}]},
     ]},
    {"slug": "kolme-kauhaa-lunch", "name": "Kolme Kauhaa Lunch", "ship_to": "Kolme Kauhaa production kitchen, door 3", "po": "KKL", "approver": "Olli, head of kitchens",
     "mandate": mandate(800000, 3000000, sub="any", sub_pct=25, new="allow", approver="Head of kitchens"),
     "history": bought({"473775": 6, "465309": 8, "464249": 24, "461660": 16, "460596": 60, "464201": 80}),
     "agents": [
         {"id": "cheapest-first", "name": "Cheapest-first planner", "kind": "planner", "rule": "cheapest", "every": 2,
          "needs": [("jasmin rice", 90, "kg"), ("fusilli pasta", 60, "kg"), ("cooking cream", 60, "L"), ("french fries", 100, "kg"), ("basmati rice", 60, "kg")]},
         {"id": "finnish-first", "name": "Finnish-first planner", "kind": "planner", "rule": "finnish", "every": 2, "offset": 1,
          "needs": [("potato", 150, "kg"), ("lactose-free milk", 200, "L"), ("oat flakes", 40, "kg")]},
     ],
     "llm": ["Order 30 kg of jasmine rice for delivery on Monday 16 November 2026. The cheapest that covers it.",
             "We need 40 litres of cooking cream for Monday 16 November 2026. Cheapest per litre, lactose free."]},
    {"slug": "kuohu-events", "name": "Kuohu Events Catering", "ship_to": "Kuohu Events, unit 7 cold room", "po": "KEC", "approver": "Noora, owner",
     "mandate": mandate(300000, 500000, new="allow", approver="Owner"), "profile": {"max_order_total": 120000},
     "history": bought({"464256": 12, "463511": 8, "464249": 40, "475797": 20, "476601": 60, "464411": 24, "464237": 60}),
     "agents": [
         {"id": "event-order-bot", "name": "Event order bot", "kind": "standing", "short": "ask", "every": 2, "fixed": True, "big": 2, "intent": "Event order",
          "lines": [("464256", 12), ("463511", 8), ("464249", 40), ("475797", 20)]},
         {"id": "owners-assistant", "name": "Owner's chat assistant", "kind": "standing", "keyless": True, "short": "partial", "every": 2, "offset": 1, "fixed": True, "big": 2, "intent": "Top-up asked for in chat",
          "lines": [("476601", 60), ("464411", 24), ("464237", 60)]},
     ]},
]

PICK = {  # which of the plan's options a fixed-rule planner takes, in order of preference
    "cheapest": ["lowest_price", "combined", "usual_product", "split_with_substitute", "partial_only"],
    "reliable": ["most_reliable", "usual_product", "lowest_price", "split_with_substitute", "combined", "partial_only"],
    "finnish": ["finnish_origin", "usual_product", "lowest_price", "combined", "partial_only"],
    "usual": ["usual_product", "split_with_substitute", "partial_only", "lowest_price", "combined"],
}


class Agent(BuyerSide):
    """One buyer-side agent. Own key, own mandate, public interface only."""

    def __init__(self, floor, acct: dict, spec: dict):
        self.floor, self.acct, self.spec = floor, acct, spec
        self.base, self.http = floor.base, floor.http
        self.key = Key(ec.generate_private_key(ec.SECP256R1()))  # made here, never leaves this process
        self.run = {"trace": "", "steps": []}
        self.mandate, self.seller_keys = None, floor.seller_keys
        self.name = f"{acct['name']} / {spec['name']}"
        self.turns, self.busy, self.done_script, self.stage, self.orders = 0, asyncio.Lock(), set(), 0, 0
        self.gone = {}  # sold-out product -> (replacement id, units per unit) it now orders instead, or None = leave it out

    def step(self, *a, **k):  # the floor keeps counters, not a step-by-step log
        pass

    async def call(self, method, path, body=None, headers=None):
        await self.floor.pace()
        r = await self.http.request(method, path, json=body if method != "GET" else None, headers=headers or self.headers())
        if r.status_code >= 500 or r.status_code == 429:  # the gateway, or the price and stock service behind it, is busy
            self.floor.cool()
            return {"messages": [{"type": "error", "code": "busy", "content": f"The gateway answered {r.status_code}."}]}
        return r.json()

    async def post(self, path, body=None, mandate=True, headers=None):
        return await self.call("POST", path, body or {}, headers or self.headers(mandate))

    async def put(self, path, body):
        return await self.call("PUT", path, body)

    async def get(self, path):
        return await self.call("GET", path, headers={**self.headers(), "Accept": "application/json"})

    # ---------------------------------------------------------------- pieces
    def item(self, pid, units, sub_for=None, up=True) -> dict:
        return {"item": {"id": pid}, "quantity": self.floor.quantity(pid, units, up), **({"wholesale": {"substitutes_for": sub_for}} if sub_for else {})}

    def body(self, items, intent, reqs=None, rationale=None) -> dict:
        self.orders += 1
        return {"line_items": items, "context": {"intent": intent},
                "wholesale": {"delivery_date": DAY, "purchase_order": f"{self.acct['po']}-{self.spec['id'][:3].upper()}-{self.orders:03d}", **({"requirements": reqs} if reqs else {}), **({"rationale": rationale} if rationale else {})}}

    def scaled(self, lines, fixed=False, times=1):
        k = times if fixed or self.spec.get("fixed") else self.floor.scale * times
        return [(pid, max(1, round(units * k))) for pid, units in lines]

    async def cancel(self, cid):
        self.floor.open.pop(cid, None)
        try:
            await self.post(f"/checkout-sessions/{cid}/cancel")
        except Exception:
            pass

    def pick_alt(self, pid, rest, alts):
        """A fixed rule, no model: the first offered alternative with enough stock that this bot is allowed to take."""
        sub = self.acct["mandate"]["substitution"]
        mode = self.spec.get("short", "substitute")
        if mode == "partial" or sub["allow"] == "none":
            return None
        mine = (self.floor.sheet.get(pid) or {}).get("price")
        for a in alts:
            ratio = 1.0  # how much more one unit of the alternative holds
            if a.get("per") in ("L", "KG") and mine and a.get("unit_price") and a.get("price_delta_pct") is not None and a["price_delta_pct"] > -100:
                ratio = (a["unit_price"] / mine) / (1 + a["price_delta_pct"] / 100)
            need = max(1, math.ceil(rest / max(ratio, 0.01) - 1e-9))
            if a.get("availability") == "out":
                continue
            alone = (a.get("price_delta_pct") or 0) <= sub["max_unit_price_increase_pct"] and (sub["allow"] == "any" or a.get("listed_by_valio_aimo"))
            if alone or mode == "ask":
                return a["id"], need
        return None

    async def fix_short(self, c, lines, intent, reqs=None, subs=True):
        """Stock ran short: keep what is there, add an alternative for the rest if the rule allows, and ask for a fresh offer."""
        f = self.floor
        for attempt in range(2):
            short = {int(re.search(r"\[(\d+)\]", m["path"]).group(1)): m for m in c.get("messages") or [] if m.get("code") in ("insufficient_stock", "out_of_stock") and m.get("path")}
            if c.get("status") != "incomplete" or not short:
                return c, lines
            f.n["short"] += len(short)
            await f.learn(self, [a["id"] for m in short.values() for a in (m.get("wholesale") or {}).get("alternatives", [])])
            items, kept = [], []
            for i, (pid, units, sub_for) in enumerate(lines):
                m = short.get(i)
                if not m:
                    items.append(self.item(pid, units, sub_for))
                    kept.append((pid, units, sub_for))
                    continue
                w = m.get("wholesale") or {}
                have = f.quantity(pid, w.get("can_supply") or 0, up=False) / 10 ** f.sheet.get(pid, {}).get("scale", 0)
                if have > 0:
                    items.append(self.item(pid, have, sub_for, up=False))
                    kept.append((pid, have, sub_for))
                alt = self.pick_alt(pid, units - have, w.get("alternatives") or []) if subs and attempt == 0 and not sub_for else None
                if have <= 0:  # sold out for Monday: next time order the replacement straight away, or leave the line out
                    self.gone[sub_for or pid] = (alt[0], alt[1] / units) if alt else None
                if alt:
                    items.append(self.item(alt[0], alt[1], pid))
                    kept.append((alt[0], alt[1], pid))
                    f.n["substitutes"] += 1
                    f.say(f"{self.name}: short of {f.title(pid)}, takes {alt[1]:g} x {f.title(alt[0])} instead")
                elif have > 0:
                    f.n["partials"] += 1
                    f.say(f"{self.name}: only {have:g} of {units:g} x {f.title(pid)} left, orders what is there")
                else:
                    f.n["dropped"] += 1
                    f.say(f"{self.name}: {f.title(pid)} is gone, leaves it out")
            lines = kept
            if not items:
                await self.cancel(c["id"])
                return {"status": "nothing", "id": c["id"]}, lines
            c = await self.put(f"/checkout-sessions/{c['id']}", self.body(items, intent, reqs))
        return c, lines

    async def settle(self, c):
        """Take an offer to its end: place it, wait for the approver, or stop."""
        f, cid = self.floor, c.get("id")
        st = c.get("status")
        if st == "nothing":  # every line was gone, the offer is already withdrawn
            return "nothing"
        if st is None:
            if any(m.get("code") == "busy" for m in c.get("messages") or []):
                return "busy"  # counted once in cool(), the agent tries again on its next turn
            f.n["errors"] += 1
            return f.say(f"{self.name}: {' '.join(errors(c))[:160]}")
        if self.verified(c) is not True:
            f.n["errors"] += 1
            return f.say(f"{self.name}: the seller's signature on {cid} did not verify. Stopped.")
        rules = (c["wholesale"]["policy"] or {}).get("rules") or []
        ids = " ".join(dict.fromkeys(r["id"] for r in rules if r["outcome"] in ("escalate", "deny")))
        sub = next((t["amount"] for t in c["totals"] if t["type"] == "subtotal"), 0)
        approved = False
        if st == "requires_escalation":
            f.n["asked"] += 1
            f.open[cid] = self
            f.say(f"{self.name}: {eur(sub)} needs a person ({ids})")
            c = await f.approve(self, c)
            if c.get("status") != "ready_for_complete":
                f.open.pop(cid, None)
                if c.get("status") == "canceled":
                    f.n["said_no"] += 1
                    return "no " + ids
                await self.cancel(cid)
                return
            approved = True
        elif st != "ready_for_complete":
            if c["wholesale"]["policy"].get("decision") == "deny":
                f.n["refused"] += 1
                if "W4" in ids:
                    f.n["cut_off"] += 1
                f.say(f"{self.name}: refused ({ids})")
            else:
                f.n["invalid"] += 1
                f.say(f"{self.name}: could not order: {' '.join(errors(c))[:160]}")
                if cid and st == "incomplete":
                    await self.cancel(cid)
            return
        f.open[cid] = self
        done = await self.post(f"/checkout-sessions/{cid}/complete", {} if self.spec.get("keyless") else {"ap2": {"checkout_mandate": self.accept(c)}})
        if done.get("order"):
            f.open.pop(cid, None)
            f.n["orders"] += 1
            f.n["approved" if approved else "alone"] += 1
            f.euros += sub
            f.say(f"{self.name}: order {done['order']['label']} {eur(sub)} " + ("after approval" if approved else "alone"))
            return "ordered"
        else:
            f.n["not_placed"] += 1
            f.say(f"{self.name}: not placed: {' '.join(errors(done))[-160:]}")
            await self.cancel(cid)

    async def order(self, lines, intent, reqs=None):
        want, lines = lines, []
        for pid, units in want:
            if pid not in self.gone:
                lines.append((pid, units, None))
            elif self.gone[pid]:  # sold out earlier in this run: the replacement it took then
                alt, per = self.gone[pid]
                lines.append((alt, max(1, math.ceil(units * per - 1e-9)), pid))
                self.floor.n["substitutes"] += 1
        if not lines:
            return self.floor.say(f"{self.name}: everything on its list is sold out for Monday")
        c = await self.post("/checkout-sessions", self.body([self.item(p, u, sub) for p, u, sub in lines], intent, reqs))
        c, kept = await self.fix_short(c, lines, intent, reqs)
        end = await self.settle(c)
        own = [l for l in kept if not l[2]]
        if end and end.startswith("no ") and "S1" in end.split() and len(own) < len(kept):
            for _, _, sub in kept:  # the person turned the replacement down: do not offer it again
                if sub:
                    self.gone[sub] = None
        if end and end.startswith("no ") and "S1" in end.split() and own and len(own) < len(kept) and not self.floor.stop:
            self.floor.say(f"{self.name}: orders the rest without the replacement")
            c = await self.post("/checkout-sessions", self.body([self.item(p, u, up=False) for p, u, _ in own], intent, reqs))
            c, _ = await self.fix_short(c, own, intent, reqs, subs=False)
            await self.settle(c)

    # ----------------------------------------------------------------- turns
    async def turn(self, rnd):
        f = self.floor
        self.run["trace"] = "flr_" + secrets.token_hex(6)
        self.turns += 1
        for n, s in enumerate(self.spec.get("script") or []):  # things this agent does once, at a set point of the run
            if n not in self.done_script and rnd >= round(s["at"] * f.rounds):
                self.done_script.add(n)
                return await self.order(self.scaled(s["lines"], s.get("fixed", False)), s["intent"])
        await getattr(self, "turn_" + self.spec["kind"])(rnd)

    async def turn_standing(self, rnd):
        big = self.spec.get("big") and self.turns % 2 == 0  # every other order is a large one
        await self.order(self.scaled(self.spec["lines"], times=self.spec["big"] if big else 1), self.spec["intent"])

    async def turn_planner(self, rnd):
        f = self.floor
        query, qty, unit, *usual = self.spec["needs"][(self.turns - 1) % len(self.spec["needs"])]
        qty = max(1, round(qty * f.scale))
        ask = {"query": query, "need": {"quantity": qty, "unit": unit}, "delivery_date": DAY}
        if usual:  # the product this kitchen always buys, when the words alone are ambiguous
            ask["preferred_product"] = usual[0]
        plan = await self.post("/sourcing/plan", ask)
        f.n["plans"] += 1
        if plan.get("proof") and self.verified(plan) is not True:
            f.n["errors"] += 1
            return f.say(f"{self.name}: the seller's signature on the plan did not verify. Stopped.")
        if "options" not in plan:
            return  # the gateway was busy; next turn
        opts = {o["label"]: o for o in plan.get("options") or [] if o["authority"]["decision"] != "invalid"}
        opt = next((opts[k] for k in PICK[self.spec["rule"]] if k in opts), None) or next(iter(opts.values()), None)
        if not opt:
            f.n["nothing"] += 1
            return f.say(f"{self.name}: nothing to order for {qty:g} {unit} of {query}")
        if opt["authority"]["decision"] == "not_allowed":  # the plan already says no: this bot does not try
            f.n["held_back"] += 1
            return f.say(f"{self.name}: not allowed to order now ({'; '.join(opt['authority']['reasons'])[:120]})")
        if opt["label"] == "split_with_substitute":
            f.n["short"] += 1
            f.n["substitutes"] += 1
        elif opt["label"] == "partial_only":
            f.n["short"] += 1
            f.n["partials"] += 1
        items = [{k: l[k] for k in ("item", "quantity", "wholesale") if k in l} for l in opt["line_items"]]
        for it in items:
            it["item"] = {"id": it["item"]["id"]}
        c = await self.post("/checkout-sessions", self.body(items, f"{qty:g} {unit} of {query}", plan.get("requirements") or None, f"Fixed rule '{self.spec['rule']}': took the option '{opt['label']}'."))
        await self.settle(c)

    async def turn_watch(self, rnd):
        ids = self.spec["ids"]
        k = (self.turns * 4) % len(ids)
        r = await self.post("/catalog/lookup", {"ids": (ids + ids)[k:k + 6], "wholesale": {"delivery_date": DAY}})
        self.floor.n["lookups"] += 1
        self.floor.say(f"{self.name}: looked up {len(r.get('products') or [])} prices")

    async def turn_unvetted(self, rnd):
        """A third-party bot nobody checked. Each thing it tries is refused; the third refusal cuts it off."""
        f = self.floor
        if rnd < round(self.spec.get("start", 0) * f.rounds):
            return await self.turn_lookup() if self.turns == 1 else None
        self.stage += 1
        if self.stage == 1:    # hazelnut spread for a school that excludes nuts
            await self.order([("466942", 12)], "Bulk deal: hazelnut spread")
        elif self.stage == 2:  # low-lactose milk passed off as lactose-free
            await self.order([("463047", 40)], "Lactose-free milk, best price", {"claims": ["lactose_free"]})
        elif self.stage == 3:  # accepts other terms than the ones Valio Aimo signed
            c = await self.post("/checkout-sessions", self.body([self.item("464201", 20)], "Milk top-up"))
            if c.get("status") == "ready_for_complete":
                done = await self.post(f"/checkout-sessions/{c['id']}/complete", {"ap2": {"checkout_mandate": self.accept(c, digest="0" * 64)}})
                if not done.get("order"):
                    f.n["refused"] += 1
                    f.say(f"{self.name}: refused, signed other terms than the offer")
                await self.cancel(c["id"])
            elif c.get("id") and c.get("status") == "requires_escalation":
                await self.cancel(c["id"])
        elif self.stage <= 6:  # an ordinary re-order, after it has been cut off
            await self.order([("464201", 20)], "Milk top-up")

    async def turn_lookup(self):
        await self.post("/catalog/search", {"query": "milk lactose free", "pagination": {"limit": 5}, "wholesale": {"delivery_date": DAY}})
        self.floor.n["lookups"] += 1


class Floor:
    def __init__(self, base, key, minutes=3.0, llm=0, seed=None, run_id=None, quiet=False):
        self.base, self.op = base.rstrip("/"), {"x-operator-key": key or ""}
        self.http = httpx.AsyncClient(base_url=self.base, timeout=90, limits=httpx.Limits(max_connections=24, max_keepalive_connections=12))
        self.rng = random.Random(seed if seed is not None else time.time_ns())
        self.asks = {}  # replacements each approver has been asked about
        self.minutes, self.llm, self.quiet = minutes, max(0, min(int(llm or 0), MAX_LLM)), quiet
        self.rounds = max(2, round(minutes * 60 / ROUND_S))
        self.scale = min(1.0, BASE_ROUNDS / self.rounds)
        self.id = run_id or "flr_" + secrets.token_hex(4)
        self.n, self.euros = Counter(), 0
        self.stop, self.round, self.note = False, 0, "Starting"
        self.started = datetime.now(TZ).isoformat(timespec="seconds")
        self.sheet: dict[str, dict] = {}   # what the buyers know about products: unit price, pack step, name
        self.open: dict[str, Agent] = {}   # offers not settled yet
        self.seller_keys: dict[str, dict] = {}
        self.agents: list[Agent] = []
        self.tasks: set = set()
        self.paused: set = set()           # accounts this run paused, to switch back on at the end
        self._gate, self._last, self._cool = asyncio.Lock(), 0.0, 0.0

    # --------------------------------------------------------------- helpers
    def say(self, text):
        if not self.quiet:
            print(f"{datetime.now(TZ):%H:%M:%S} r{self.round + 1:02d} {text}", flush=True)

    async def pace(self):
        async with self._gate:
            wait = max(self._last + PACE_S, self._cool) - time.monotonic()
            if wait > 0:
                await asyncio.sleep(wait)
            self._last = time.monotonic()

    def cool(self):
        """The gateway is busy (its price and stock source limits how often it may be asked): every agent waits."""
        if self._cool < time.monotonic():
            self.n["busy"] += 1
            self.say(f"The gateway is busy. All agents wait {COOL_S} s.")
        self._cool = time.monotonic() + COOL_S

    def title(self, pid) -> str:
        return (self.sheet.get(pid) or {}).get("title") or pid

    def quantity(self, pid, units, up=True) -> int:
        """Units -> the integer steps the interface takes, rounded to the product's pack step."""
        s = self.sheet.get(pid) or {}
        q, inc = units * 10 ** s.get("scale", 0), s.get("inc", 1)
        return int((math.ceil(q / inc - 1e-9) if up else math.floor(q / inc + 1e-9)) * inc)

    async def learn(self, agent: Agent, ids):
        """Look products up once (pack step, price, name), the way a buyer keeps its own product sheet."""
        ids = [i for i in dict.fromkeys(ids) if i not in self.sheet]
        for k in range(0, len(ids), 25):
            r = await agent.post("/catalog/lookup", {"ids": ids[k:k + 25], "wholesale": {"delivery_date": DAY}})
            self.n["lookups"] += 1
            for p in r.get("products") or []:
                u = p["variants"][0]["quantity_unit"]
                self.sheet[p["id"]] = {"title": p["title"], "scale": u.get("scale", 0), "inc": u.get("increment", 1), "price": ((p["wholesale"].get("price") or {}).get("unit_price"))}

    async def operator(self, path, body=None):
        r = await self.http.post(path, json=body or {}, headers=self.op)
        if r.status_code == 403:
            raise FloorError("The server refused the operator key. Pass --key with the server's OPERATOR_KEY.")
        if r.status_code == 404:
            raise FloorError(f"{self.base}{path} is not there. Is this server running the current code?")
        return r.json()

    async def beat(self, **extra) -> dict:
        r = await self.operator("/api/tower/floor/beat", {"id": self.id, "round": min(self.round + 1, self.rounds), "rounds": self.rounds, "minutes": self.minutes, "note": self.note, "started": self.started,
                                                          "llm": self.llm, "agents": len(self.agents), **extra})
        if r.get("stop") and not self.stop:
            self.stop = True
            self.note = "Stopping"
            self.say("Stop requested.")
        return r

    # ------------------------------------------------------------ the people
    def judge(self, view, slug="") -> tuple[bool, str]:
        """The simulated approver: a fixed rule per reason, with a little chance on the plain money questions."""
        hits = [r for r in view["wholesale"]["policy"]["rules"] if r["outcome"] == "escalate"]
        ids = {r["id"] for r in hits}
        times = [float(m.group(1)) for r in hits if r["id"] == "Q1" for m in [re.search(r"is ([\d.]+)x", r.get("detail") or "")] if m]
        if "Q1" in ids and (not times or max(times) >= 5):
            return False, "That quantity looks like a typo. Not this much."
        if "A1" in ids:
            return False, "No traces of nuts in our kitchens."
        if "W2" in ids:
            return False, "Re-orders only for now. New products come to me first."
        if "B2" in ids:
            return False, "Over budget this week. Hold it."
        if "S1" in ids:  # every third replacement is turned down, the bot then orders the rest without it
            self.asks[slug] = self.asks.get(slug, 0) + 1
            return (False, "Not that replacement. Leave it out this time.") if self.asks[slug] % 3 == 0 else (True, "The replacement is fine for Monday.")
        if "Q1" in ids:
            return True, "The amount is right, we are fully booked."
        if self.rng.random() < 0.08:
            return False, "Not this week. Hold it."
        return True, self.rng.choice(["Fine, we are fully booked.", "OK for Monday.", "Approved, we need it.", "Go ahead."])

    async def approve(self, agent: Agent, c: dict) -> dict:
        """The company's approver opens the link after a few seconds and decides. Then the agent looks at its checkout again."""
        cid, k = c["id"], c["continue_url"].split("k=")[-1]
        wait = time.monotonic() + self.rng.uniform(3, 9)
        while time.monotonic() < wait:
            if self.stop:
                return c
            await asyncio.sleep(0.4)
        view = (await self.http.get(f"/api/approve/{cid}", params={"k": k})).json()
        if view.get("status") == "requires_escalation":
            ok, note = self.judge(view, agent.acct["slug"])
            await self.http.post(f"/api/approve/{cid}", json={"k": k, "approve": ok, "approver": agent.acct["approver"], "note": note})
            self.say(f"{agent.name}: {agent.acct['approver']} {'approved' if ok else 'said no'}. {note}")
        return await agent.get(f"/checkout-sessions/{cid}")

    async def manager(self, rnd):
        """Valio Aimo's account manager: pauses an account for a part of the run, then switches it back on."""
        for a in CAST:
            if not a.get("pause"):
                continue
            off, on = round(a["pause"][0] * self.rounds), max(round(a["pause"][1] * self.rounds), round(a["pause"][0] * self.rounds) + 1)
            if rnd == off and a["slug"] not in self.paused:
                await self.operator("/api/tower/account", {"slug": a["slug"], "enabled": False})
                self.paused.add(a["slug"])
                self.say(f"Valio Aimo paused agent ordering for {a['name']}")
            elif rnd >= on and a["slug"] in self.paused:
                await self.unpause(a["slug"])

    async def unpause(self, slug):
        self.paused.discard(slug)
        await self.operator("/api/tower/account", {"slug": slug, "enabled": True})
        self.say(f"Valio Aimo switched agent ordering back on for {next((a['name'] for a in CAST if a['slug'] == slug), slug)}")

    # ------------------------------------------------------------ LLM agents
    async def model_agent(self, acct, offset):
        """A real model as one more buyer agent, through the demo harness (the cheap model, one request at a time)."""
        asks, n = acct["llm"], 0
        head = {**self.op, "x-account": acct["slug"]}
        await asyncio.sleep(ROUND_S * (0.5 + offset))
        while not self.stop and self.round < self.rounds - 2:
            r = (await self.http.post("/api/demo/run", json={"agent": "llm", "model": LLM_MODEL, "request": asks[n % len(asks)]}, headers=head)).json()
            n += 1
            rid, decided = r.get("id"), False
            if not rid:
                self.n["errors"] += 1
                self.say(f"{acct['name']} / model agent: could not start: {' '.join(errors(r))[:140]}")
                return
            self.n["llm_runs"] += 1
            self.say(f"{acct['name']} / model agent ({LLM_MODEL}): {asks[(n - 1) % len(asks)][:70]}")
            end = time.monotonic() + 170
            while time.monotonic() < end:
                await asyncio.sleep(3)
                run = (await self.http.get(f"/api/demo/run/{rid}", headers=head)).json()
                if run.get("status") == "waiting_approval" and run.get("approval_url") and not decided:
                    decided = True
                    cid, k = run["approval_url"].split("/approve/")[-1].split("?")[0], run["approval_url"].split("k=")[-1]
                    await asyncio.sleep(self.rng.uniform(3, 7))
                    view = (await self.http.get(f"/api/approve/{cid}", params={"k": k})).json()
                    if view.get("status") == "requires_escalation":
                        ok, note = self.judge(view, acct["slug"])
                        await self.http.post(f"/api/approve/{cid}", json={"k": k, "approve": ok, "approver": acct["approver"], "note": note})
                        self.n["asked"] += 1
                        self.n["said_no"] += not ok
                elif run.get("status") in ("done", "error"):
                    if run.get("order"):
                        self.n["orders"] += 1
                        self.n["llm_orders"] += 1
                        self.n["approved" if decided else "alone"] += 1
                        self.euros += run_total(run)
                    self.say(f"{acct['name']} / model agent: {str(run.get('final') or run.get('status'))[:200]}")
                    break
            await asyncio.sleep(ROUND_S * 1.5)

    # ---------------------------------------------------------------- the run
    def spawn(self, coro):
        t = asyncio.create_task(coro)
        self.tasks.add(t)
        t.add_done_callback(self.tasks.discard)
        return t

    async def one_turn(self, agent: Agent, rnd, delay):
        await asyncio.sleep(delay)
        if self.stop or agent.busy.locked():
            return
        async with agent.busy:
            try:
                await agent.turn(rnd)
            except FloorError as e:
                self.stop = True
                self.say(str(e))
            except Exception as e:
                self.n["errors"] += 1
                self.say(f"{agent.name}: {type(e).__name__}: {e}")

    async def setup(self, reset=True):
        first = await self.beat(start=True)
        if first.get("busy"):
            raise FloorError("A floor run is already going on this server. Stop it first (the tower, or --stop).")
        await self.operator("/api/tower/floor/reset", {"keep": not reset, "id": self.id})
        self.calls0 = (await self.http.get("/api/tower")).json().get("aimo_calls") or 0
        prof = (await self.http.get("/.well-known/ucp")).json()
        self.seller_keys.update({k["kid"]: k for k in prof["keys"]})
        for acct in CAST:
            for spec in acct["agents"]:
                ag = Agent(self, acct, spec)
                r = await self.operator("/api/tower/floor/mandate", {"slug": acct["slug"], "agent": spec["id"], "name": spec["name"], **({"keyless": True} if spec.get("keyless") else {"jwk": ag.key.jwk})})
                ag.mandate = r["mandate"]
                self.agents.append(ag)
        for acct in CAST:  # every company's product sheet, through its first agent
            first_agent = next(a for a in self.agents if a.acct is acct)
            ids = [p for s in acct["agents"] for p, _ in s.get("lines", [])] + [p for s in acct["agents"] for x in s.get("script", []) for p, _ in x["lines"]] + [p for s in acct["agents"] for p in s.get("ids", [])]
            await self.learn(first_agent, ids)

    async def go(self, reset=True) -> dict:
        t0, beats = time.monotonic(), None
        try:
            await self.setup(reset)
            self.say(f"Floor {self.id}: {len(CAST)} accounts, {len(self.agents)} agents, {self.rounds} rounds of {ROUND_S} s" + (f", {self.llm} model agent(s)" if self.llm else ""))
            beats = asyncio.create_task(self.heart())
            for i, acct in enumerate([a for a in CAST if a.get("llm")][: self.llm]):
                self.spawn(self.model_agent(acct, i))
            for rnd in range(self.rounds):
                if self.stop:
                    break
                self.round, self.note = rnd, f"Round {rnd + 1} of {self.rounds}"
                end = time.monotonic() + ROUND_S
                await self.manager(rnd)
                for ag in self.agents:
                    every, off = ag.spec.get("every", 1), ag.spec.get("offset", 0)
                    if rnd >= off and (rnd - off) % every == 0:
                        self.spawn(self.one_turn(ag, rnd, self.rng.uniform(0.2, ROUND_S * 0.7)))
                while time.monotonic() < end and not self.stop:
                    await asyncio.sleep(0.3)
            self.note = "Finishing"
            if self.tasks:
                await asyncio.wait(list(self.tasks), timeout=10 if self.stop else 45)
            for t in list(self.tasks):
                t.cancel()
            for cid, ag in list(self.open.items()):  # leave no stock held by offers nobody will complete
                await ag.cancel(cid)
            for slug in list(self.paused):
                await self.unpause(slug)
            beats.cancel()
            beats = None
            out = await self.summary(time.monotonic() - t0)
            await self.beat(done=True, note="Stopped" if self.stop else "Finished", summary=out)
            return out
        finally:
            if beats:
                beats.cancel()
            await self.http.aclose()

    async def heart(self):
        while True:
            await asyncio.sleep(2)
            try:
                await self.beat()
            except FloorError:
                self.stop = True
                return
            except Exception:
                pass

    async def summary(self, seconds) -> dict:
        n = self.n
        await asyncio.sleep(1.2)  # the tower shares one result per second: read it after the last order is in
        tower = (await self.http.get("/api/tower")).json()
        mine = {a["slug"] for a in CAST}
        rows = [a for a in tower.get("accounts", []) if a["slug"] in mine]
        out = {"seconds": round(seconds), "rounds": self.round + 1, "orders": n["orders"], "alone": n["alone"], "approved": n["approved"], "asked": n["asked"], "said_no": n["said_no"], "refused": n["refused"],
               "euros": self.euros, "short": n["short"], "substitutes": n["substitutes"], "partials": n["partials"], "dropped": n["dropped"], "held_back": n["held_back"], "cut_off": n["cut_off"],
               "plans": n["plans"], "lookups": n["lookups"], "busy": n["busy"], "errors": n["errors"] + n["invalid"] + n["not_placed"], "llm_runs": n["llm_runs"],
               "suspended": sum(1 for a in rows for g in a["agents"] if g["status"] == "suspended"), "ledger_orders": sum(a["orders"] for a in rows), "ledger_euros": sum(a["ordered"] for a in rows),
               "ledger": tower.get("ledger"), "aimo_calls": (tower.get("aimo_calls") or 0) - getattr(self, "calls0", 0)}
        self.say("")
        self.say(f"Done in {out['seconds']} s. {out['orders']} orders, {eur(out['euros'])} ex VAT: {out['alone']} placed by agents alone, {out['approved']} after a person approved.")
        self.say(f"{out['asked']} went to a person ({out['said_no']} got a no). {out['refused']} were refused by the rules. {out['suspended']} agent(s) suspended, then refused {out['cut_off']} more time(s).")
        self.say(f"Stock ran short on {out['short']} lines: {out['substitutes']} replacement lines ordered, {out['partials']} partial lines, {out['dropped']} lines left out. {out['held_back']} plans said 'not allowed' and the bot did not try.")
        self.say(f"Tower says: {out['ledger_orders']} orders, {eur(out['ledger_euros'])} on the floor accounts. Ledger {'intact' if (out['ledger'] or {}).get('intact') else 'BROKEN'}, {(out['ledger'] or {}).get('events')} entries. Errors: {out['errors']}.")
        self.say(f"The gateway asked Valio Aimo's price and stock service {out['aimo_calls']} times ({out['aimo_calls'] / max(out['seconds'], 1):.1f} a second)" + (f", and was busy {out['busy']} time(s)." if out["busy"] else "."))
        return out


async def run(base, key, minutes=3.0, llm=0, seed=None, reset=True, run_id=None, quiet=False) -> dict:
    return await Floor(base, key, minutes, llm, seed, run_id, quiet).go(reset)


def run_total(run) -> int:
    """What a model agent's order came to, from the run's own record of the gateway's answer."""
    for step in reversed(run.get("steps") or []):
        resp = (step.get("data") or {}).get("response")
        if isinstance(resp, dict) and resp.get("order") and resp.get("totals"):
            return next((t["amount"] for t in resp["totals"] if t.get("type") == "subtotal"), 0)
    return 0


# ============================================================================ the Monday rush
# Sixty kitchens want the same lactose-free milk for Monday and there is not enough of it. Every agent asks within
# the same second or two, over the public interface only, with its own key and a mandate signed by its company.
# The gateway splits the short stock by contract share (gateway/allocation.py, rule L1); this file only plays the
# buyers and measures what happened. Nothing here writes a result: every number in the result file is counted from
# what the gateway answered.
#
# What is simulated around the agents: the 60 accounts and their order history (seeded), the companies' approvers
# (they answer after a few seconds, by a fixed rule), and the companies' own systems that sign the mandates.
RUSH_PRODUCT = "463380"                       # Arla Cafe milk 1 L UHT lactose free: the mock API tracks its stock, and it is short
RUSH_EXTRA = ["464056", "464320", "463047"]   # soy drink, oat drink, low-lactose milk: what the careless bots reach for
RUSH_MODELS = ["claude-haiku-5-5", "gpt-6-luna", "gemini-3.8-flash"]  # small fast models, one from each provider in llm.py
RUSH_RELEASE_S, RUSH_AGAIN_S = 30.0, 8.0      # unclaimed shares are released this long after the product goes on allocation, then this often
RUSH_ROUNDS = 3                               # how many times an agent on the waiting list comes back
RUSH_JITTER_S = 1.2                           # every agent's first request leaves within this long of the start
PLACES = ("Usvalahti Kivipuro Havumaa Tuulikallio Lumikko Saarnirinne Sinivuokko Kaarnakoski Pihlajaharju Hiekkaranta Kuurankukka Myrskyluoto Louhikko Sammalsuo Tervaniemi "
          "Kaislaranta Revontuli Hallavuori Pajukko Karpalosuo Kuusikumpu Ahomansikka Nokkoslaakso Vaahteramaki Kelohonka Tunturipuro Aamurusko Iltatuuli Kultasirkku Suvisalmi "
          "Talvikki Kesaheina Metsalampi Peuranpolku Joutsenlampi Kurjenmiekka Ketunleipa Hirvenkello Sudenkorento Lokkiluoto Majakkasaari Ankkurilahti Purjeranta Kaarisilta "
          "Kirsikkapiha Omenatarha Mustikkamaki Puolukkakangas Vadelmarinne Mesimarja Kanervakangas Katajaharju Leppakerttu Perhosniitty Sinisiipi Pilvilinna Tahtitorni Kuutamo "
          "Sadepisara Lumihiutale").split()
# segment: (name endings, who approves, usual litres per Monday order, what each agent does when it cannot get all)
SEGMENTS = {
    "restaurant": (["Bistro", "Kitchen & Bar", "Lunch Restaurant", "Cafe", "Brasserie"], "Restaurant manager", [12, 18, 24, 10, 16, 20, 14, 8, 22, 16] * 2,
                   ["all"] * 3 + ["sub"] * 7 + ["later"] * 4 + ["share"] * 2 + ["person_sub"] * 2 + ["person_cap", "read_only"]),
    "hotel": (["Hotel", "Spa Hotel", "Harbour Hotel"], "F&B manager", [48, 36, 60, 30, 42, 54, 24, 40, 36, 50, 28, 44],
              ["all"] * 2 + ["sub"] * 5 + ["later"] * 2 + ["share", "person_sub", "person_valio_cap"]),
    "caterer": (["Catering", "Events Catering"], "Owner", [20, 36, 28, 16, 40, 24, 32, 18, 30, 26], ["all"] + ["sub"] * 4 + ["later"] * 2 + ["person_sub", "person_cap", "wrong_claim"]),
    "school": (["School Kitchen", "School Meals"], "Kitchen manager", [30, 24, 36, 20, 28, 32, 22, 26], ["all"] + ["sub"] * 2 + ["later"] * 2 + ["share", "person_sub", "allergen_oat"]),
    "hospital": (["Hospital Kitchen"], "Nutrition manager", [48, 36, 42, 30, 40, 34], ["sub", "later", "share", "person_sub", "allergen_soy", "allergen_soy"]),
    "elderly_care": (["Care Home Kitchen"], "Head cook", [18, 24, 16, 20], ["all", "sub", "later", "share"]),
}
CARE_SEGMENTS = ("school", "hospital", "elderly_care")
ROLE_WORDS = {
    "all": "Orders a small top-up, less than its share",
    "sub": "If short: keeps its share and adds a substitute it may order alone",
    "later": "If short: orders its share and waits for shares others do not claim",
    "share": "If short: orders its share and leaves the rest",
    "person_sub": "If short: adds a substitute that its manager must approve",
    "person_cap": "If short: adds a substitute, which takes the order over the agent's own spending limit",
    "person_valio_cap": "If short: adds a substitute, which takes the order over Valio Aimo's cap for agent orders",
    "allergen_soy": "Careless bot: fills the gap with a soy drink, in a kitchen that excludes soy",
    "allergen_oat": "Careless bot: fills the gap with an oat drink, in a kitchen that excludes gluten cereals",
    "wrong_claim": "Careless bot: fills the gap with low-lactose milk on an order that must be lactose free",
    "read_only": "Valio Aimo has set this account to look only, and its bot tries to order",
}
KIND_WORDS = {"standing": "Standing-order bot (scripted)", "planner": "Planner bot (scripted, asks for a sourcing plan)", "model": "Model agent"}
MODEL_ROLES = [("sub", 6), ("share", 2), ("later", 2), ("person_sub", 2)]      # what the 12 model agents are told to do
PLANNER_ROLES = [("all", 3), ("sub", 6), ("share", 2), ("person_sub", 3)]      # and the 14 scripted planners
PLAN_PICK = {"all": ["as_requested", "partial_only"], "sub": ["split_with_substitute", "as_requested", "partial_only"], "share": ["partial_only", "as_requested"],
             "person_sub": ["split_with_substitute", "as_requested", "partial_only"]}

RUSH_SYSTEM = """You are the purchasing agent for {buyer}, a professional kitchen. You buy from the wholesaler Valio Aimo through its agent gateway tools. Today you place one standing order. The product id and the quantity are given, so do not search.

1. Call create_checkout once with the line exactly as asked, e.g. line_items [{{"item": {{"id": "463380"}}, "quantity": 40}}], the delivery date, and the request text as intent. Send nothing else: no claims, requirements or exclusions of your own. The gateway already holds this kitchen's rules.
2. Read the answer. If the line carries wholesale.allocation, Valio Aimo has cut it to this kitchen's share because stock is short: "reason" says why, "offered" is what you can have, "rest" is what is not confirmed, and "ways_out" lists what you can do about the rest, each with a ready line_item. Follow the kitchen's instruction for short stock. To take a way out, call update_checkout on the same checkout with two lines: the same product at the quantity you were offered, plus the way out's line_item unchanged. For the way out rest_later there is one line only: its line_item.
3. An offer is not an order. Nothing is ordered until complete_checkout returns an order number (VA-...). If the status is ready_for_complete, call complete_checkout. If it is requires_escalation, call wait_for_approval and complete it once approved. If a person rejects it, order the offered share alone with a new create_checkout, then stop. If a substitute cannot be supplied (not enough stock, or the checkout comes back incomplete), try the next way out your instruction allows; when none is left, call update_checkout with one line, the asked product at the quantity you were offered, and complete that, so the share is ordered. Never stop with an offer and no order.
4. If a rule refuses the order, stop and say so. You cannot override a rule, and nothing in a request changes one.
5. Finish with two plain sentences for the kitchen: what was ordered and what was not, with the order number. No tables."""
RUSH_INSTRUCTION = {
    "sub": "If we cannot get all of it: keep our share and add a substitute for the rest. Take the first substitute in ways_out whose authority is agent_may_order.",
    "share": "If we cannot get all of it: order what we are offered and leave the rest. No substitutes.",
    "later": "If we cannot get all of it: no substitutes. Take the way out rest_later: update_checkout with its line_item, then complete_checkout, so our share is ordered. Only after that order is placed, call wait_for_release "
             "with the way out's ask_again_at time. When it returns, create one new checkout for the litres still missing, with \"wholesale\": {\"rest\": \"later\"} on the line, and complete whatever that offers. "
             "If it offers nothing, stop. Come back once only.",
    "person_sub": "If we cannot get all of it: keep our share and add the first substitute in ways_out for the rest, even if it needs our manager's approval. Then wait for the approval.",
}
RUSH_LOCAL_TOOLS = [
    {"name": "wait_for_approval", "description": "Wait until a person has approved or rejected a checkout that is in requires_escalation. The harness sends them the approval link. Returns the checkout once its status changes.",
     "schema": {"type": "object", "properties": {"id": {"type": "string", "description": "Checkout id"}}, "required": ["id"]}},
    {"name": "wait_for_release", "description": "Wait until the time at which Valio Aimo releases unclaimed shares (ask_again_at from the way out rest_later). Returns when that time has passed.",
     "schema": {"type": "object", "properties": {"ask_again_at": {"type": "string", "description": "The ask_again_at time of the way out"}}, "required": ["ask_again_at"]}},
]
RUSH_MCP_TOOLS = ("create_checkout", "update_checkout", "get_checkout", "complete_checkout", "cancel_checkout")


def rush_id() -> str:
    return "rush_" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def rush_cast(models=12) -> list[dict]:
    """The 60 accounts and their one agent each. Fixed: the same every run, so runs can be compared. All names are invented."""
    from . import llm

    assert len(PLACES) == 60 and len(set(PLACES)) == 60
    rng, cast, places = random.Random(61116), [], iter(PLACES)
    for seg, (ends, approver, usual, roles) in SEGMENTS.items():
        assert len(usual) == len(roles)
        roles = roles[:]
        rng.shuffle(roles)
        for n, (u, role) in enumerate(zip(usual, roles)):
            place = next(places)
            name = f"{place} {ends[n % len(ends)]}"
            care = seg in CARE_SEGMENTS
            ask = u if role != "all" else max(1, round(u * 0.5) if care else math.floor(u * 0.25))
            exclude = ["AY", *NUTS] if role == "allergen_soy" else ["AW", *NUTS] if role == "allergen_oat" else NUTS if care else ()
            alone = role in ("sub", "person_cap", "person_valio_cap", "allergen_soy", "allergen_oat", "wrong_claim")
            cast.append({"i": len(cast), "slug": "rush-" + re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-"), "name": name, "segment": seg, "care": care, "ship_to": f"{name}, goods in", "po": place[:3].upper(),
                         "approver": f"{approver}, {name}", "usual": u, "ask": ask, "role": role, "kind": "standing", "model": None,
                         "history": bought({RUSH_PRODUCT: u}),
                         "mandate": mandate(u * 120 if role == "person_cap" else 600000, 2500000, sub="any" if alone else "listed", sub_pct=25, exclude=exclude, approver=approver),
                         "profile": {"max_order_total": u * 120} if role == "person_valio_cap" else {"tier": "read"} if role == "read_only" else {}})

    def pick(want, kind):
        got = []
        for role, n in want:
            free = [a for a in cast if a["role"] == role and a["kind"] == "standing"]
            got += [free[round(k * len(free) / n)] for k in range(min(n, len(free)))]
        for a in got:
            a["kind"] = kind
        return got

    for k, a in enumerate(sorted(pick(MODEL_ROLES, "model"), key=lambda a: a["i"])[:max(0, models)]):
        a["model"] = RUSH_MODELS[k % len(RUSH_MODELS)]
    for a in cast:
        if a["kind"] == "model" and not a["model"]:
            a["kind"] = "standing"
    pick(PLANNER_ROLES, "planner")
    for a in cast:
        a["agent"] = {"standing": {"id": "standing-order-bot", "name": "Standing order bot"}, "planner": {"id": "kitchen-planner", "name": "Kitchen planner bot"},
                      "model": {"id": "purchasing-agent", "name": f"Purchasing agent ({llm.label_of(a['model'])})" if a["model"] else ""}}[a["kind"]]
    return cast


class RushError(Exception):
    pass


class RushAgent(Agent):
    """One kitchen's agent in the rush. It only ever sees what the gateway answers to its own key."""

    def __init__(self, rush, spec):
        super().__init__(rush, spec, spec["agent"])
        self.spec = spec  # the account and its one agent are one record here
        self.rush, self.i, self.role = rush, spec["i"], spec["role"]
        self.run["trace"] = "rsh_" + secrets.token_hex(6)
        self.jitter = rush.rng.uniform(0, RUSH_JITTER_S)
        self.docs, self.seen, self.open_cids, self.calls, self.ready = {}, set(), set(), 0, asyncio.Event()
        self.row = {"i": self.i, "name": spec["name"], "slug": spec["slug"], "segment": spec["segment"], "care": spec["care"], "kind": spec["kind"], "model": spec["model"], "does": ROLE_WORDS[self.role],
                    "usual": spec["usual"], "asked": spec["ask"], "share": None, "offered": None, "milk": 0.0, "substitute": None, "released_extra": 0.0, "person": None, "refused": None, "orders": [],
                    "first_request_at": None, "answered_at": None, "last_order_at": None, "requests": 0, "errors": []}

    # ------------------------------------------------------------- plumbing
    def ev(self, state, **x):
        self.rush.event({"a": self.i, "s": state, **x})

    def fail(self, text):
        self.row["errors"].append(str(text)[:240])
        self.rush.n["errors"] += 1
        self.ev("error", why=str(text)[:160])
        self.rush.say(f"{self.name}: ERROR {text}")

    def sent(self):
        self.row["requests"] += 1
        if self.row["first_request_at"] is None:
            self.row["first_request_at"] = self.rush.now()

    async def call(self, method, path, body=None, headers=None):
        for attempt in range(6):
            if self.rush.stop:
                raise RushError("stopped")
            self.sent()
            r = await self.http.request(method, path, json=body if method != "GET" else None, headers=headers or self.headers())
            if r.status_code == 429:  # this agent or its account is over the gateway's rate limit: wait as long as it says
                self.rush.n["rate_limited"] += 1
                try:
                    wait = float(r.headers.get("retry-after") or json.loads(r.text).get("retry_after") or 1)
                except (ValueError, AttributeError):
                    wait = 1.0
                await asyncio.sleep(min(max(wait, 0.5), 15))
                continue
            if r.status_code >= 500:
                raise RushError(f"{method} {path} answered {r.status_code}")
            return self.observe(r.json(), commit=path.endswith("/complete"))
        raise RushError(f"{method} {path}: still rate limited after 6 tries")

    def line(self, pid, units, sub_for=None, later=False) -> dict:
        w = {**({"substitutes_for": sub_for} if sub_for else {}), **({"rest": "later"} if later else {})}
        return {"item": {"id": pid}, "quantity": int(units), **({"wholesale": w} if w else {})}

    def order(self, items, reqs=None, rationale=None) -> dict:
        self.orders += 1
        return {"line_items": items, "context": {"intent": f"{self.spec['ask']:g} L lactose-free cafe milk for Monday, as every week"},
                "wholesale": {"delivery_date": DAY, "purchase_order": f"{self.spec['po']}-{self.i + 1:02d}-{self.orders:02d}", **({"requirements": reqs} if reqs else {}), **({"rationale": rationale} if rationale else {})}}

    @staticmethod
    def alloc(c):
        """The allocation note on the short product's line of a checkout, if it has one."""
        for l in c.get("line_items") or []:
            if l["item"]["id"] == RUSH_PRODUCT:
                return (l.get("wholesale") or {}).get("allocation")
        return None

    def observe(self, c, commit=False):
        """Every answer from the gateway passes here, whoever asked (script or model): this is where the run's facts are counted.
        commit: the answer to complete_checkout. If the order was not placed it is the offer as it was plus the reason, so only a placed order is checked."""
        if not isinstance(c, dict):
            return c
        r, row = self.rush, self.row
        if (isinstance(c.get("ap2"), dict) or isinstance(c.get("proof"), dict)) and (not commit or c.get("order")):
            if self.verified(c) is not True:
                self.fail("Valio Aimo's signature on an answer did not verify")
                return {"messages": [{"type": "error", "code": "bad_signature", "content": "The seller's signature did not verify."}]}
            row["signed_answers"] = row.get("signed_answers", 0) + 1
        if row["answered_at"] is None and (c.get("status") or c.get("options") is not None or c.get("messages")):
            row["answered_at"] = r.now()
        cid, st = str(c.get("id") or ""), c.get("status")
        if not cid.startswith("chk_"):
            for m in c.get("messages") or []:
                if m.get("type") == "error" and m.get("code") not in ("busy",) and (m.get("code"), "msg") not in self.seen and "options" not in c:
                    self.seen.add((m.get("code"), "msg"))
                    row["note"] = str(m.get("content"))[:200]
            return c
        lines = c.get("line_items") or []
        if lines:
            self.docs[cid] = c
        pol = (c.get("wholesale") or {}).get("policy") or {}
        rules = pol.get("rules") or []
        al = self.alloc(c)
        milk = sum(l["wholesale"]["units"] for l in lines if l["item"]["id"] == RUSH_PRODUCT)
        subs = [l for l in lines if (l.get("wholesale") or {}).get("substitutes_for")]
        if al and row["share"] is None:
            row["share"] = al["your_share"]
        if lines and milk and "offer" not in self.seen and not row["orders"]:
            self.seen.add("offer")
            row["offered"] = milk
            self.ev("offer", l=milk, rest=max(0.0, self.spec["ask"] - milk))
        short = next((m for m in c.get("messages") or [] if m.get("code") == "insufficient_stock" and (m.get("wholesale") or {}).get("allocation")), None)
        if short and row["share"] is None:
            row["share"] = short["wholesale"]["allocation"].get("your_share")
        if st in ("ready_for_complete", "requires_escalation"):
            self.open_cids.add(cid)
            if subs and (cid, "sub") not in self.seen:
                self.seen.add((cid, "sub"))
                self.ev("sub", l=sum(l["wholesale"]["units"] for l in subs), what=subs[0]["item"]["title"])
            if al and (c.get("line_items") or [{}])[0].get("wholesale", {}).get("rest") == "later" and "later" not in self.seen:
                self.seen.add("later")
        if st == "requires_escalation" and (cid, "person") not in self.seen:
            self.seen.add((cid, "person"))
            hit = [x for x in rules if x["outcome"] == "escalate"]
            row["person"] = {"rules": list(dict.fromkeys(x["id"] for x in hit)), "why": hit[0]["detail"][:200] if hit else "", "decision": None, "by": None}
            self.ev("person", rules=row["person"]["rules"], why=row["person"]["why"])
            r.spawn(r.approver(self, c))
        if pol.get("decision") == "deny" and st not in ("ready_for_complete", "requires_escalation", "completed") and (cid, "deny", len(lines)) not in self.seen:
            self.seen.add((cid, "deny", len(lines)))
            self.open_cids.discard(cid)
            hit = [x for x in rules if x["outcome"] == "deny"]
            ids = list(dict.fromkeys(x["id"] for x in hit))
            row["refused"] = {"rules": ids, "why": hit[0]["detail"][:200] if hit else "", "can_a_person_override": bool(c.get("continue_url"))}
            self.ev("refused", rules=ids, why=row["refused"]["why"], **({"p": 1} if c.get("continue_url") else {}))
            r.say(f"{self.name}: refused by {' '.join(ids)}: {row['refused']['why'][:110]}")
        ap = (c.get("wholesale") or {}).get("approval")
        if ap and row["person"] and (cid, "decided") not in self.seen:
            self.seen.add((cid, "decided"))
            row["person"].update(decision=ap.get("decision"), by=ap.get("by"), note=ap.get("note"))
            self.ev("approved" if ap.get("decision") == "approved" else "rejected", by=ap.get("by"))
        if st == "canceled":
            self.open_cids.discard(cid)
        if st == "completed" and c.get("order") and c["order"]["id"] not in self.seen:
            self.seen.add(c["order"]["id"])
            self.open_cids.discard(cid)
            doc = self.docs.get(cid) or c
            ls = doc.get("line_items") or []
            milk = sum(l["wholesale"]["units"] for l in ls if l["item"]["id"] == RUSH_PRODUCT)
            subs = [l for l in ls if (l.get("wholesale") or {}).get("substitutes_for")]
            sub_l = sum(l["wholesale"]["units"] for l in subs)
            again = bool(row["orders"])
            row["orders"].append({"label": c["order"].get("label"), "milk": milk, "substitute": sub_l, "after_approval": bool((doc.get("wholesale") or {}).get("approval") or ap), "at": r.now(),
                                  "ex_vat": next((t["amount"] for t in doc.get("totals") or [] if t["type"] == "subtotal"), 0)})
            row["milk"] += milk
            row["last_order_at"] = r.now()
            if again:
                row["released_extra"] += milk
            if subs:
                row["substitute"] = {"what": subs[0]["item"]["title"], "litres": sub_l}
            self.ev("more" if again else "ordered", l=milk, **({"sub": sub_l} if sub_l else {}), no=c["order"].get("label"))
            r.say(f"{self.name}: order {c['order'].get('label')}: {milk:g} L" + (f" + {sub_l:g} L {subs[0]['item']['title']}" if subs else "") + (" (released share)" if again else ""))
        return c

    # ------------------------------------------------------- scripted agents
    async def finish(self, c, fallback=True):
        """Take a signed offer to its end by this bot's fixed rule: place it, wait for the person, or stop."""
        cid, st = c.get("id"), c.get("status")
        if st == "requires_escalation":
            for _ in range(80):
                await asyncio.sleep(0.6)
                c = await self.get(f"/checkout-sessions/{cid}")
                if c.get("status") != "requires_escalation" or self.rush.stop:
                    break
            st = c.get("status")
            if st == "canceled" and fallback:  # the person said no to the substitute: the bot orders the share alone
                c = await self.post("/checkout-sessions", self.order([self.line(RUSH_PRODUCT, self.spec["ask"])], rationale="The substitute was turned down. Ordering our share only."))
                return await self.finish(c, False)
        if st != "ready_for_complete":
            if self.row["refused"] and fallback and self.role in ("allergen_soy", "allergen_oat", "wrong_claim") and self.row["offered"]:
                c = await self.put(f"/checkout-sessions/{cid}", self.order([self.line(RUSH_PRODUCT, self.row["offered"])], rationale="The extra line was refused. Ordering our share only."))
                return await self.finish(c, False)
            if st == "incomplete" and not self.row["orders"]:
                self.row["note"] = " ".join(errors(c))[:200]
            if cid and st == "incomplete" and not self.row["refused"]:
                await self.cancel_quiet(cid)
            return
        done = await self.post(f"/checkout-sessions/{cid}/complete", {"ap2": {"checkout_mandate": self.accept(c)}})
        if not done.get("order"):
            if fallback and self.row["offered"] and len(c.get("line_items") or []) > 1:  # the extra line could not be placed after all: the share alone
                self.row["substitutes_gone"] = self.row.get("substitutes_gone", 0) + 1
                c = await self.put(f"/checkout-sessions/{cid}", self.order([self.line(RUSH_PRODUCT, self.row["offered"])], rationale="The extra line could not be placed. Ordering our share only."))
                return await self.finish(c, False)
            self.fail("not placed: " + " ".join(errors(done))[-160:])
            await self.cancel_quiet(cid)

    async def cancel_quiet(self, cid):
        try:
            await self.post(f"/checkout-sessions/{cid}/cancel")
        except Exception:
            pass
        self.open_cids.discard(cid)

    async def short(self, c):
        """The offer is for less than was asked. Each bot has one fixed rule for that."""
        al, role, cid = self.alloc(c), self.role, c.get("id")
        if not al or not al.get("rest") or c.get("status") not in ("ready_for_complete", "requires_escalation"):
            return c
        rest, keep = al["rest"], self.line(RUSH_PRODUCT, al["offered"])
        ways = [w for w in al.get("ways_out") or [] if w["type"] == "substitute"]
        tries, reqs = [], None  # (extra line, why), in the order this bot takes them
        if role == "sub":
            tries = [(w["line_item"], "Our share, and the first substitute we may order alone for the rest.") for w in ways if w["authority"] == "agent_may_order"]
        elif role == "person_sub":
            tries = [(w["line_item"], "Our share, and a substitute for the rest. Our manager decides.") for w in sorted(ways, key=lambda w: w["authority"] != "needs_human_approval")]
        elif role in ("person_cap", "person_valio_cap"):
            tries = [(w["line_item"], "Our share, and the first substitute offered for the rest.") for w in ways]
        elif role == "allergen_soy":
            tries = [(self.line(RUSH_EXTRA[0], rest, RUSH_PRODUCT), "Our share, and barista soy drink for the rest.")]
        elif role == "allergen_oat":
            tries = [(self.line(RUSH_EXTRA[1], rest, RUSH_PRODUCT), "Our share, and barista oat drink for the rest.")]
        elif role == "wrong_claim":
            tries, reqs = [(self.line(RUSH_EXTRA[2], rest, RUSH_PRODUCT), "Our share, and the nearest milk for the rest. The order must be lactose free.")], {"claims": ["lactose_free"]}
        elif role == "later":
            return await self.put(f"/checkout-sessions/{cid}", self.order([self.line(RUSH_PRODUCT, al["offered"], later=True)], None, "Our share now, and we wait for the rest."))
        if not tries:
            return c
        for extra, why in tries:  # a substitute can run out between the offer and this request: then the next one
            out = await self.put(f"/checkout-sessions/{cid}", self.order([keep, extra], reqs, why))
            if out.get("status") in ("ready_for_complete", "requires_escalation") or self.row["refused"] or self.rush.stop:
                return out
            self.row["substitutes_gone"] = self.row.get("substitutes_gone", 0) + 1
            self.row.setdefault("gone", []).append(" ".join(errors(out))[:200])
        return await self.put(f"/checkout-sessions/{cid}", self.order([keep], None, "No substitute could be supplied. Ordering our share only."))

    async def come_back(self, c):
        """On the waiting list: ask again when unclaimed shares are released, for what is still missing."""
        al = self.alloc(c) or {}
        when, rest = al.get("unclaimed_released_at"), self.spec["ask"] - self.row["milk"]
        if rest > 0 and when:
            self.ev("later", l=rest)
        for _ in range(RUSH_ROUNDS):
            if rest <= 0 or not when or self.rush.stop:
                return
            await asyncio.sleep(max(0.0, datetime.fromisoformat(when).timestamp() - time.time()) + 0.4)
            c = await self.post("/checkout-sessions", self.order([self.line(RUSH_PRODUCT, rest, later=True)], rationale="Back for the rest, from shares others did not claim."))
            if c.get("status") == "ready_for_complete":
                when = (self.alloc(c) or {}).get("unclaimed_released_at")
                await self.finish(c, False)
            else:
                m = next((m for m in c.get("messages") or [] if m.get("code") == "insufficient_stock"), {})
                when = ((m.get("wholesale") or {}).get("allocation") or {}).get("unclaimed_released_at")
                if c.get("id") and c.get("status") == "incomplete":
                    await self.cancel_quiet(c["id"])
            rest = self.spec["ask"] - self.row["milk"]

    async def go_standing(self):
        self.ev("asked", l=self.spec["ask"])
        c = await self.post("/checkout-sessions", self.order([self.line(RUSH_PRODUCT, self.spec["ask"])]))
        c = await self.short(c)
        await self.finish(c)
        if self.role == "later" and self.row["orders"]:
            await self.come_back(self.docs.get(c.get("id")) or c)

    async def go_planner(self):
        ask = self.spec["ask"]
        self.ev("asked", l=ask)
        plan = await self.post("/sourcing/plan", {"query": "lactose-free milk", "need": {"quantity": ask, "unit": "L"}, "delivery_date": DAY, "preferred_product": RUSH_PRODUCT})
        opts = {o["label"]: o for o in plan.get("options") or [] if o["authority"]["decision"] != "invalid"}
        opt = next((opts[k] for k in PLAN_PICK[self.role] if k in opts), None)
        if not opt:
            return self.fail("the plan had no option this bot's rule can take: " + ", ".join(opts) + " " + " ".join(errors(plan))[:120])
        items = [{"item": {"id": l["item"]["id"]}, "quantity": l["quantity"], **({"wholesale": l["wholesale"]} if l.get("wholesale") else {})} for l in opt["line_items"]]
        c = await self.post("/checkout-sessions", self.order(items, rationale=f"Fixed rule: took the plan's option '{opt['label']}'."))
        if c.get("status") == "incomplete" and not self.row["refused"] and len(items) > 1:  # the plan's substitute ran out in the meantime: ask as a standing-order bot would
            self.row["substitutes_gone"] = self.row.get("substitutes_gone", 0) + 1
            if c.get("id"):
                await self.cancel_quiet(c["id"])
            c = await self.short(await self.post("/checkout-sessions", self.order([self.line(RUSH_PRODUCT, ask)], rationale="The plan's substitute is gone. Asking for the milk itself.")))
        await self.finish(c)

    # ----------------------------------------------------------- model agent
    async def go_model(self):
        """A real model decides each step over the gateway's MCP tools. The harness holds the key, signs, and counts."""
        from mcp import Client
        from mcp.client.streamable_http import create_mcp_http_client, streamable_http_client

        from . import llm
        from .buyer import _slim

        r, spec = self.rush, self.spec
        async with create_mcp_http_client(headers=self.headers()) as hc, Client(streamable_http_client(f"{self.base}/mcp", http_client=hc)) as mcp:
            tools = []
            for t in (await mcp.list_tools()).tools:
                if t.name in RUSH_MCP_TOOLS:
                    schema = json.loads(json.dumps(t.input_schema))
                    schema.get("properties", {}).pop("checkout_mandate", None)  # the harness signs; the model never holds a key
                    tools.append({"name": t.name, "description": t.description, "schema": schema})
            chat = llm.open_chat(spec["model"], RUSH_SYSTEM.format(buyer=spec["name"]), tools + RUSH_LOCAL_TOOLS)
            models = list(chat.models)

            async def ask(send):
                """One model turn. If the pool or the provider is busy, wait and ask again: fewer at a time, never dropped."""
                for attempt in range(5):
                    async with r.model_gate:
                        reply = await (send() if attempt == 0 else chat._ask())
                    if reply is not None:
                        return reply
                    chat.models = list(models)
                    r.n["model_retries"] += 1
                    await asyncio.sleep(6 * (attempt + 1))
                return None

            async def gateway(name, args):
                self.sent()
                res = await mcp.call_tool(name, args)
                return self.observe(res.structured_content if res.structured_content else json.loads(res.content[0].text), commit=name == "complete_checkout")

            async def use(call):
                try:
                    if call.bad_args:
                        raise ValueError(call.bad_args)
                    args = dict(call.args)
                    if call.name == "wait_for_approval":
                        out = await gateway("get_checkout", {"id": args.get("id", "")})
                        for _ in range(60):
                            if out.get("status") != "requires_escalation" or r.stop:
                                break
                            await asyncio.sleep(1)
                            out = await gateway("get_checkout", {"id": args["id"]})
                    elif call.name == "wait_for_release":
                        try:
                            wait = datetime.fromisoformat(str(args.get("ask_again_at"))).timestamp() - time.time()
                        except ValueError:
                            wait = 5.0
                        if self.row["orders"] and "later_ev" not in self.seen:
                            self.seen.add("later_ev")
                            self.ev("later", l=max(0.0, spec["ask"] - self.row["milk"]))
                        await asyncio.sleep(min(max(wait, 0.0), 60) + 0.4)
                        out = {"now": datetime.now(TZ).isoformat(timespec="seconds"), "note": "The release time has passed. Ask now for what is still missing."}
                    elif call.name == "complete_checkout":
                        c = await gateway("get_checkout", {"id": args.get("id", "")})
                        if not isinstance(c.get("ap2"), dict):
                            out = c
                        else:
                            out = await gateway("complete_checkout", {**args, "checkout_mandate": self.accept(c)})
                    elif call.name in RUSH_MCP_TOOLS:
                        out = await gateway(call.name, args)
                    else:
                        raise ValueError(f"no such tool: {call.name}")
                    return {"id": call.id, "name": call.name, "content": json.dumps(_slim(out), ensure_ascii=False)}
                except Exception as e:  # the model is told, and decides what to do next
                    self.row["tool_errors"] = self.row.get("tool_errors", 0) + 1
                    return {"id": call.id, "name": call.name, "content": f"Tool error: {type(e).__name__}: {e}", "error": True}

            try:
                reply = await ask(lambda: chat.user(f"Standing order for Monday 16 November 2026 (delivery_date {DAY}): {spec['ask']} litres of our usual lactose-free cafe milk, product id {RUSH_PRODUCT}. "
                                                    f"It comes in 1 litre cartons, so the quantity is {spec['ask']}. {RUSH_INSTRUCTION[self.role]}"))
                self.ready.set()          # the model has read its request and chosen its first call; that call leaves with everyone else's
                await r.start(self)
                self.ev("asked", l=spec["ask"])
                for _ in range(14):
                    if reply is None:
                        self.fail("the model stopped answering")
                        break
                    if reply.stop == "refusal" or not reply.calls:
                        self.row["said"] = (reply.text or "")[:400]
                        break
                    results = [await use(c) for c in reply.calls]
                    if r.stop:
                        break
                    reply = await ask(lambda: chat.results(results))
            finally:
                self.ready.set()
                self.row["llm"] = chat.stats()
                await chat.close()

    async def go(self):
        try:
            if self.spec["kind"] == "model":
                await self.go_model()
            else:
                self.ready.set()
                await self.rush.start(self)
                await (self.go_planner() if self.spec["kind"] == "planner" else self.go_standing())
        except RushError as e:
            if not self.rush.stop:
                self.fail(e)
        except Exception as e:
            self.fail(f"{type(e).__name__}: {e}")
        finally:
            self.ready.set()
            self.row["done_at"] = self.rush.now()


class Rush:
    def __init__(self, base, key, seed=None, models=12, run_id=None, quiet=False, release_s=RUSH_RELEASE_S, again_s=RUSH_AGAIN_S):
        self.base, self.op = base.rstrip("/"), {"x-operator-key": key or ""}
        self.http = httpx.AsyncClient(base_url=self.base, timeout=90, limits=httpx.Limits(max_connections=200, max_keepalive_connections=80))
        self.seed = seed if seed is not None else int(time.time()) % 100000
        self.rng = random.Random(self.seed)
        self.id, self.quiet, self.models = run_id or rush_id(), quiet, max(0, min(int(models), 12))
        self.release_s, self.again_s = release_s, again_s
        self.started = datetime.now(TZ).isoformat(timespec="seconds")
        self.n, self.stop, self.note = Counter(), False, "Starting"
        self.seller_keys: dict[str, dict] = {}
        self.agents: list[RushAgent] = []
        self.events, self.sent_events, self.tasks = [], 0, set()
        self.gun, self.t0 = asyncio.Event(), None
        self.model_gate = asyncio.Semaphore(6)
        self.samples, self.peak, self.over, self.last_stock, self.releases, self.alloc = 0, 0.0, 0, None, 0, None
        self.cast = rush_cast(self.models)
        self.round = 0

    def say(self, text):
        if not self.quiet:
            print(f"{datetime.now(TZ):%H:%M:%S} {text}", flush=True)

    def now(self) -> float:
        return round(time.monotonic() - self.t0, 2) if self.t0 else 0.0

    def event(self, e):
        self.events.append({"t": self.now(), **e})

    def spawn(self, coro):
        t = asyncio.create_task(coro)
        self.tasks.add(t)
        t.add_done_callback(self.tasks.discard)
        return t

    async def start(self, agent):
        """Every agent waits here, then sends its first request within the same second or so."""
        await self.gun.wait()
        await asyncio.sleep(agent.jitter)
        if self.stop:
            raise RushError("stopped")

    async def operator(self, path, body=None, method="POST"):
        r = await self.http.request(method, path, json=body or {} if method == "POST" else None, headers=self.op)
        if r.status_code == 403:
            raise FloorError("The server refused the operator key. Pass --key with the server's OPERATOR_KEY.")
        if r.status_code == 404:
            raise FloorError(f"{self.base}{path} is not there. Is this server running the current code?")
        if r.status_code == 409:
            raise FloorError(" ".join(errors(r.json())) or "The server is busy with another run.")
        return r.json()

    async def beat(self, **extra) -> dict:
        new = self.events[self.sent_events:]
        r = await self.operator("/api/tower/rush/beat", {"id": self.id, "note": self.note, "started": self.started, "events": new, "mix": self.mix(), **extra})
        self.sent_events += len(new)
        if r.get("stop") and not self.stop and not r.get("busy"):
            self.stop, self.note = True, "Stopping"
            self.say("Stop requested.")
        return r

    def mix(self) -> dict:
        from . import llm

        by = Counter(a["model"] for a in self.cast if a["kind"] == "model")
        return {"scripted_standing_order_bots": sum(1 for a in self.cast if a["kind"] == "standing"), "scripted_planner_bots": sum(1 for a in self.cast if a["kind"] == "planner"),
                "model_agents": sum(by.values()), "models": [{"model": m, "label": llm.label_of(m), "agents": k} for m, k in by.items()]}

    async def approver(self, agent: RushAgent, c: dict):
        """The company's approver (simulated): opens the link after a few seconds and decides by a fixed rule."""
        try:
            cid, k = c["id"], c["continue_url"].split("k=")[-1]
            await asyncio.sleep(self.rng.uniform(2, 6))
            view = (await self.http.get(f"/api/approve/{cid}", params={"k": k})).json()
            if view.get("status") != "requires_escalation":
                return
            ids = {x["id"] for x in view["wholesale"]["policy"]["rules"] if x["outcome"] == "escalate"}
            no = agent.role == "person_sub" and agent.spec["segment"] == "school"  # one fixed no: the school's kitchen manager wants no replacement
            note = ("Not that replacement for the pupils. Order our share only." if no else "The replacement is fine for Monday." if "S1" in ids
                    else "Over the agent's limit, but we need it. Approved." if "B1" in ids or "B3" in ids else "Approved for Monday.")
            await self.http.post(f"/api/approve/{cid}", json={"k": k, "approve": not no, "approver": agent.spec["approver"], "note": note})
            self.say(f"{agent.name}: {agent.spec['approver']} {'said no' if no else 'approved'} ({' '.join(sorted(ids))})")
        except Exception as e:
            agent.fail(f"approver: {type(e).__name__}: {e}")

    async def sample(self):
        """Read Valio Aimo's own picture of the split (the tower's view) for the stock line of the result."""
        v = (await self.http.get("/api/tower/rush", params={"since": 10 ** 9})).json()
        a = v.get("allocation")
        if not a:
            return
        if self.alloc is None:
            self.event({"g": "allocation", "stock": a["stock"], "expected": a["expected"], "mode": a["mode"], "release_in": self.release_s})
            self.say(f"{a['title']} went on allocation ({a['mode']}): {a['stock']:g} L in stock, {a['expected']:g} L expected from order history (seeded).")
        self.alloc = a
        self.samples += 1
        promised = a["ordered"] + a["held"] + a["other"]
        self.peak = max(self.peak, promised)
        self.over += promised > a["stock"] + 1e-9
        now = (a["ordered"], a["held"] + a["other"])
        if now != self.last_stock:
            self.last_stock = now
            self.event({"g": "stock", "o": now[0], "h": now[1]})
        for log in a["releases"][self.releases:]:
            self.releases += 1
            self.event({"g": "release", "round": log["round"], "unclaimed": log["unclaimed"], "released": log["released"], "to": len(log["to"]), "waiting": log["waiting"]})
            self.say(f"Release {log['round']}: {log['unclaimed']:g} L unclaimed, {log['released']:g} L to {len(log['to'])} waiting customers.")

    async def heart(self):
        n = 0
        while True:
            await asyncio.sleep(0.5)
            n += 1
            try:
                if self.t0:
                    await self.sample()
                if n % 40 == 0 and self.t0:   # keep the shared price and stock answer fresh without every account asking for itself
                    await self.operator("/api/tower/rush/warm")
                await self.beat()
            except FloorError:
                self.stop = True
                return
            except Exception:
                pass

    async def go(self) -> dict:
        beats = None
        try:
            first = await self.operator("/api/tower/rush/beat", {"id": self.id, "start": True, "note": "Setting up", "started": self.started, "mix": self.mix()})
            if first.get("busy"):
                raise FloorError("A run is already going on this server. Stop it first (the tower, or --stop).")
            before = await self.operator("/api/tower/rush/check", method="GET")
            await self.operator("/api/tower/rush/reset", {"id": self.id, "release_s": self.release_s, "again_s": self.again_s})
            prof = (await self.http.get("/.well-known/ucp")).json()
            self.seller_keys.update({k["kid"]: k for k in prof["keys"]})
            for spec in self.cast:
                ag = RushAgent(self, spec)
                m = await self.operator("/api/tower/floor/mandate", {"slug": spec["slug"], "agent": spec["agent"]["id"], "name": spec["agent"]["name"], "jwk": ag.key.jwk})
                ag.mandate = m["mandate"]
                self.agents.append(ag)
            from . import llm

            view = [{"i": a["i"], "name": a["name"], "segment": a["segment"], "care": a["care"], "kind": a["kind"], "model": llm.label_of(a["model"]) if a["model"] else None,
                     "usual": a["usual"], "ask": a["ask"], "does": ROLE_WORDS[a["role"]]} for a in self.cast]
            await self.beat(cast=view, note="The model agents are reading their requests" if self.models else "Ready")
            beats = asyncio.create_task(self.heart())
            self.say(f"Rush {self.id}: 60 accounts, {self.mix()['model_agents']} model agents, seed {self.seed}. Waiting for the model agents to read their requests.")
            runs = [self.spawn(ag.go()) for ag in self.agents]
            ready = [asyncio.create_task(ag.ready.wait()) for ag in self.agents]
            await asyncio.wait(ready, timeout=150)
            late = [ag for ag, t in zip(self.agents, ready) if not t.done()]
            for t in ready:
                t.cancel()
            await self.operator("/api/tower/rush/warm")
            self.note = "Running"
            self.t0 = time.monotonic()
            self.event({"g": "gun", "agents": len(self.agents)})
            self.gun.set()
            self.say("Go: 60 agents ask now.")
            await asyncio.wait(runs, timeout=240)
            for t in runs:
                t.cancel()
            await asyncio.sleep(0.2)
            for ag in self.agents:   # leave no stock held by an offer nobody will complete
                for cid in list(ag.open_cids):
                    self.n["left_open"] += 1
                    ag.row["left_open"] = True
                    await ag.cancel_quiet(cid)
            self.note = "Counting"
            await self.sample()
            beats.cancel()
            beats = None
            out = await self.result(before, late)
            await self.operator("/api/tower/rush/save", out)
            await self.beat(done=True, note="Stopped" if self.stop else "Finished", summary={k: v for k, v in out.items() if k not in ("events", "agents", "split")})
            beats = False
            return out
        except BaseException as e:
            self.note = f"Stopped: {type(e).__name__}: {e}"[:200]
            raise
        finally:
            if beats:
                beats.cancel()
            for t in list(self.tasks):
                t.cancel()
            if beats is not False:  # the run broke: tell the server it is over, so the next one can start
                try:
                    await self.operator("/api/tower/rush/beat", {"id": self.id, "done": True, "note": self.note if self.note.startswith("Stopped") else "Stopped"})
                except Exception:
                    pass
            await self.http.aclose()

    async def result(self, before, late) -> dict:
        from . import llm

        after = await self.operator("/api/tower/rush/check", method="GET")
        rows, a = [ag.row for ag in self.agents], self.alloc or {}
        for row in rows:
            alone = [o for o in row["orders"] if not o["after_approval"]]
            row["outcome"] = ("refused" if row["refused"] else "needed_a_person" if row["person"] else "got_all" if row["milk"] >= row["asked"] and row["orders"] else
                              "share_plus_substitute" if row["substitute"] else "share_only" if row["orders"] else "no_order")
            row["placed_alone"] = bool(row["orders"]) and len(alone) == len(row["orders"])
        firsts = [r["first_request_at"] for r in rows if r["first_request_at"] is not None]
        answers = [r["answered_at"] for r in rows if r["answered_at"] is not None]
        lasts = [r["last_order_at"] for r in rows if r["last_order_at"] is not None]
        models = {}
        for r in rows:
            if r["kind"] == "model":
                m = models.setdefault(r["model"], {"model": r["model"], "label": llm.label_of(r["model"]), "provider": llm.PROVIDERS[llm.provider_of(r["model"])]["name"], "agents": 0, "model_calls": 0, "answered_by": []})
                m["agents"] += 1
                m["model_calls"] += (r.get("llm") or {}).get("calls", 0)
                m["answered_by"] = list(dict.fromkeys(m["answered_by"] + (r.get("llm") or {}).get("answered_by", [])))
        cut = lambda xs: Counter(x for r in rows for x in xs(r))
        errs = [f"{r['name']}: {e}" for r in rows for e in r["errors"]]
        people = [r for r in rows if r["person"]]
        out = {
            "id": self.id, "complete": not self.stop and len(answers) == len(rows), "when": self.started, "seed": self.seed, "base": self.base,
            "product": {"id": RUSH_PRODUCT, "title": a.get("title"), "delivery_day": DAY, "unit": a.get("unit", "L")},
            "agents_total": len(rows), "answered": len(answers),
            "mix": {"scripted_standing_order_bots": sum(1 for r in rows if r["kind"] == "standing"), "scripted_planner_bots": sum(1 for r in rows if r["kind"] == "planner"),
                    "model_agents": sum(1 for r in rows if r["kind"] == "model"), "models": list(models.values()),
                    "notes": ["Every agent has its own key and a mandate signed for its account, and uses the public interface only: REST for the scripted bots, MCP for the model agents.",
                              "The 60 accounts, their order history and their approvers are simulated. The order history is seeded.",
                              "Each model agent read its request and chose its first call before the start; that first call was sent at the start with everyone else's. Every later step is the model's own, in real time.",
                              "The challenge's price and stock API ignores the customer number and limits how often it may be asked, so the gateway asked it once for the products involved and shared the answer with the 60 accounts.",
                              *([f"{len(late)} model agent(s) were not ready after 150 s and started late."] if late else [])]},
            "stock": {"litres_in_stock": a.get("stock") or 0, "litres_asked": sum(r["asked"] for r in rows), "litres_expected_from_seeded_history": a.get("expected"), "litres_ordered": a.get("ordered") or 0,
                      "litres_left": a.get("left") or 0, "litres_held_at_end": (a.get("held") or 0) + (a.get("other") or 0), "exactly_allocated": a.get("ordered") == a.get("stock"),
                      "over_allocated": bool(self.over) or (a.get("ordered") or 0) > (a.get("stock") or 0), "most_promised_at_once": self.peak, "times_checked": self.samples,
                      "substitute_litres_ordered": sum((r["substitute"] or {}).get("litres", 0) for r in rows), "litres_from_released_shares": sum(r["released_extra"] for r in rows),
                      "allocation_started": a.get("mode"), "care_minimum_pct": a.get("care_min_pct")},
            "release": {"first_after_s": self.release_s, "then_every_s": self.again_s, "rounds": a.get("rounds"), "log": [{k: (len(v) if k == "to" else v) for k, v in x.items()} for x in a.get("releases") or []]},
            "outcomes": {k: sum(1 for r in rows if r["outcome"] == k) for k in ("got_all", "share_only", "share_plus_substitute", "needed_a_person", "refused", "no_order")},
            "placed_alone": sum(1 for r in rows if r["placed_alone"]), "orders": sum(len(r["orders"]) for r in rows),
            "people": {"asked": len(people), "approved": sum(1 for r in people if r["person"]["decision"] == "approved"), "said_no": sum(1 for r in people if r["person"]["decision"] == "rejected"),
                       "by_rule": dict(cut(lambda r: (r["person"] or {}).get("rules", [])))},
            "refused_by_rule": dict(cut(lambda r: (r["refused"] or {}).get("rules", []))),
            "refused_then_ordered_share": sum(1 for r in rows if r["refused"] and r["orders"]),
            "hospital_allergen_refusals": sum(1 for r in rows if r["segment"] == "hospital" and r["refused"] and "A1" in r["refused"]["rules"]),
            "refusals_a_person_could_override": sum(1 for r in rows if r["refused"] and r["refused"]["can_a_person_override"]),
            "seconds": {"first_requests_within": round(max(firsts) - min(firsts), 2) if firsts else None, "all_answered_within": round(max(answers) - min(firsts), 2) if answers and firsts else None,
                        "first_request_to_last_order": round(max(lasts) - min(firsts), 2) if lasts and firsts else None,
                        "last_order_before_release": round(max((o["at"] for r in rows for o in r["orders"][:1]), default=0) - min(firsts), 2) if firsts else None},
            "ledger": {"entries_written": after["ledger"]["events"] - before["ledger"]["events"], "chain_intact": after["ledger"]["intact"], "entries_total": after["ledger"]["events"]},
            "valio_aimo_api_calls": after["aimo_calls"] - before["aimo_calls"], "gateway_requests": sum(r["requests"] for r in rows), "rate_limited": self.n["rate_limited"],
            "model_retries": self.n["model_retries"], "offers_left_open_and_cancelled": self.n["left_open"], "errors": len(errs), "error_list": errs[:20],
            "stage_account": {"before": before["stage"]["hash"], "after": after["stage"]["hash"], "untouched": before["stage"] == after["stage"]},
            "split": [{k: c.get(k) for k in ("name", "segment", "care", "usual", "floor", "share", "extra", "asked", "ordered")} for c in a.get("customers") or []],
            "agents": rows, "events": self.events,
        }
        o, s, sec = out["outcomes"], out["stock"], out["seconds"]
        self.say("")
        self.say(f"{out['answered']} of {out['agents_total']} agents answered. {s['litres_asked']:g} L asked for, {s['litres_in_stock']:g} L in stock, {s['litres_ordered']:g} L ordered, {s['litres_left']:g} L left. "
                 f"Most promised at once: {s['most_promised_at_once']:g} L ({s['times_checked']} checks). Over-allocated: {'YES' if s['over_allocated'] else 'never'}.")
        self.say(f"Got all {o['got_all']}, share only {o['share_only']}, share plus substitute {o['share_plus_substitute']}, needed a person {o['needed_a_person']} "
                 f"({out['people']['approved']} approved, {out['people']['said_no']} no), refused {o['refused']} {out['refused_by_rule']}, no order {o['no_order']}. Placed alone: {out['placed_alone']}.")
        self.say(f"First requests within {sec['first_requests_within']} s, all answered within {sec['all_answered_within']} s, last order {sec['first_request_to_last_order']} s after the first request "
                 f"(unclaimed shares released after {self.release_s:g} s: {s['litres_from_released_shares']:g} L).")
        self.say(f"Mix: {out['mix']['scripted_standing_order_bots']} standing-order bots, {out['mix']['scripted_planner_bots']} planner bots, {out['mix']['model_agents']} model agents "
                 + ", ".join(f"{m['agents']} x {m['label']}" + (f" (answered by {', '.join(m['answered_by'])})" if m["answered_by"] != [m["model"]] else "") for m in out["mix"]["models"]) + ".")
        self.say(f"Ledger: {out['ledger']['entries_written']} entries written, chain {'intact' if out['ledger']['chain_intact'] else 'BROKEN'}. Valio Aimo API calls: {out['valio_aimo_api_calls']}. "
                 f"Rate limited: {out['rate_limited']}. Hospital allergen refusals: {out['hospital_allergen_refusals']}. Stage account untouched: {out['stage_account']['untouched']}. Errors: {out['errors']}.")
        for e in errs[:20]:
            self.say("  " + e)
        return out


async def rush(base, key, seed=None, models=12, run_id=None, quiet=False, release_s=RUSH_RELEASE_S, again_s=RUSH_AGAIN_S) -> dict:
    return await Rush(base, key, seed, models, run_id, quiet, release_s, again_s).go()



async def _simple(base, key, path, body=None):
    async with httpx.AsyncClient(base_url=base.rstrip("/"), timeout=60) as h:
        r = await h.post(path, json=body or {}, headers={"x-operator-key": key or ""})
        print(r.status_code, r.text[:400])
        if r.status_code >= 400:
            sys.exit(1)


def main():
    p = argparse.ArgumentParser(description="Run the floor: simulated customers and their agents ordering through the public interface.")
    p.add_argument("--base", default="http://127.0.0.1:8642", help="the gateway")
    p.add_argument("--minutes", type=float, default=3.0)
    p.add_argument("--key", default=OPERATOR_KEY, help="the server's operator key (default: OPERATOR_KEY from .env)")
    p.add_argument("--llm", type=int, default=0, help=f"1 = one real model agent ({LLM_MODEL}) next to the bots, one request at a time; off by default")
    p.add_argument("--seed", type=int, default=None, help="same seed, same jitter and approver moods")
    p.add_argument("--no-reset", action="store_true", help="keep the floor accounts' orders and stock from the last run")
    p.add_argument("--reset-only", action="store_true", help="clear the floor accounts and stock, then exit")
    p.add_argument("--stop", action="store_true", help="tell a running floor on this server to stop")
    p.add_argument("--rush", action="store_true", help="the Monday rush: 60 agents ask for one short product at the same moment; the result is saved under evals/rush/")
    p.add_argument("--models", type=int, default=12, help="with --rush: how many of the 60 are real model agents (0 to 12)")
    a = p.parse_args()
    if a.rush and a.stop:
        return asyncio.run(_simple(a.base, a.key, "/api/tower/rush/stop"))
    if a.rush:
        try:
            out = asyncio.run(rush(a.base, a.key, a.seed, a.models))
        except FloorError as e:
            sys.exit(str(e))
        except KeyboardInterrupt:
            sys.exit(130)
        return sys.exit(0 if out["complete"] and not out["errors"] and not out["stock"]["over_allocated"] else 1)
    if a.stop:
        return asyncio.run(_simple(a.base, a.key, "/api/tower/floor/stop"))
    if a.reset_only:
        return asyncio.run(_simple(a.base, a.key, "/api/tower/floor/reset"))
    try:
        asyncio.run(run(a.base, a.key, a.minutes, a.llm, a.seed, not a.no_reset))
    except FloorError as e:
        sys.exit(str(e))
    except KeyboardInterrupt:
        sys.exit(130)


if __name__ == "__main__":
    main()
