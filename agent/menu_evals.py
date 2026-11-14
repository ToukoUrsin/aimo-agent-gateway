"""Menu test: many menus through the same path as the menu page, then a separate check of every signed order.

A case is a menu: dishes from the kitchen's recipe cards, a head count, dietary notes. Every fifth menu, and every menu with a
note about some guests' nut or peanut allergy, also has one dish with a written recipe that is named for an allergen (peanut satay, hazelnut cake); in some the recipe has lost that ingredient, which
the plan must flag. The gateway plans it in code, makes one
signed offer, a person approves what the rules send to a person, and the order is placed. This file then checks the signed order
with its own code. It reads the catalogue, the recipe cards and the notes; it does not call the planner's checks.

Runs in its own process on scratch data, against the live price and stock API. No model is called for the cases.
--photos N also has Gemini read each sample photo N times and counts how often the readings agree with what is printed on it.

Run:  AIMO_DATA=/tmp/aimo-menu-eval uv run -q python -m agent.menu_evals              (60 menus, about three minutes)
      AIMO_DATA=/tmp/aimo-menu-eval uv run -q python -m agent.menu_evals --cases 20 --photos 5
Writes evals/menu-latest.json (what /proof shows) and evals/menu-runs/<id>.jsonl (one line per menu).
"""
import argparse
import base64
import json
import math
import os
import random
import re
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if not os.environ.get("AIMO_DATA"):
    raise SystemExit("Set AIMO_DATA to a scratch folder. This test never runs on the live database.")

from gateway import menu  # noqa: E402
from gateway.service import Gateway, GatewayError  # noqa: E402

from .evals import signatures  # noqa: E402  the model test's own signature check

GUESTS = [12, 24, 40, 85, 120, 180, 260, 420, 650]
NOTES = [[], [], ["Nut-free kitchen"], ["{k} lactose-free"], ["{k} gluten-free"], ["{k} vegetarian"], ["{k} milk-free"], ["{k} egg-free"], ["{k} vegan"], ["Lactose-free"], ["Gluten-free"],
         ["Nut-free kitchen", "{k} lactose-free"], ["Nut-free kitchen", "{k} gluten-free"], ["{k} lactose-free", "{j} vegetarian"], ["Nut-free kitchen", "{k} milk-free", "{j} gluten-free"],
         ["{k} nut-free"], ["{k} guests severe peanut allergy"], ["{k} guests with a peanut allergy", "{j} lactose-free"]]
# Dishes with a written recipe, named for an allergen. No model writes these: the test gives the recipe, as a model would.
_R = lambda item, role, amount, unit="g", **more: {"item": item, "role": role, "amount": amount, "unit": unit, **more}
SATAY = [_R("chicken breast", "main", 130), _R("peanuts", "sauce base", 30, fi="maapähkinä"), _R("garlic", "seasoning", 3), _R("salt", "seasoning", 1)]
WRITTEN = [
    {"name": "Peanut satay chicken skewers", "source": "agent", "recipe": SATAY},
    {"name": "Hazelnut praline cake", "source": "agent", "recipe": [_R("hazelnuts", "main", 30, fi="hasselpähkinä"), _R("wheat flour", "body", 20), _R("egg", "body", 35), _R("sugar", "sweetener", 28)]},
    {"name": "Peanut satay chicken skewers", "source": "agent", "recipe": [i for i in SATAY if i["item"] != "peanuts"], "lost": "peanuts"},   # the recipe lost what the dish is named for
    {"name": "Maapähkinäkana", "source": "agent", "recipe": SATAY},                                                                              # the same dish under a Finnish name
    {"name": "Hazelnut praline cake", "source": "agent", "recipe": [_R("wheat flour", "body", 20), _R("egg", "body", 35), _R("sugar", "sweetener", 28)], "lost": "hazelnuts"},
]
# Menus where the only right answer is a question. Nothing may be ordered from them.
MUST_ASK = [
    ("10,000 guests", {"guests": 10000, "dishes": ["Creamy salmon soup", "Green salad"], "notes": []}),
    ("a note for more people than there are guests", {"guests": 180, "dishes": ["Creamy salmon soup", "Green salad"], "notes": ["400 gluten-free"]}),
    ("a note that is not a dietary rule", {"guests": 60, "dishes": ["Pea soup", "Oven pancake"], "notes": ["ask Maija about table 4"]}),
    ("a dish with no recipe", {"guests": 60, "dishes": ["Chef's surprise"], "notes": []}),
    ("a note about an ingredient the menu does not have", {"guests": 60, "dishes": ["Pea soup"], "notes": ["gluten-free croissants for 5"]}),
    ("5,000 portions of one dish for 180 guests", {"guests": 180, "dishes": [{"name": "Creamy salmon soup", "portions": 5000}], "notes": []}),
]
GROUPS = {"AN": {"SA", "SC", "SH", "SM", "SP", "SR", "ST", "SW"}, "AW": {"UW", "GB", "NR", "GS", "GK", "GO"}}
GLUTEN = {"AW"} | GROUPS["AW"]
NUTS = {"AN", "AP"} | GROUPS["AN"]
BANS = {"nut-free": NUTS, "peanut-free": {"AP"}, "milk-free": {"AM", "ML"}, "egg-free": {"AE"}, "fish-free": {"AF"}, "gluten-free": GLUTEN}  # what an order-wide exclusion for this note would look like
# What a dish name promises, in English and Finnish, and the ingredient that keeps the promise. This file's own list, not the planner's.
PROMISES = [(r"peanut|satay|maapahkina", r"peanut"), (r"hazelnut|hasselpahkina|praline", r"hazelnut"), (r"almond|manteli", r"almond"), (r"salmon|lohi", r"salmon"), (r"chicken|broiler|kana", r"chicken|broiler"),
            (r"\bpea\b|herne", r"\bpeas?\b"), (r"spinach|pinaatti", r"spinach"), (r"mushroom|sieni", r"mushroom|champignon|chanterelle"), (r"potato|peruna", r"potato"), (r"beef|naudan", r"beef"),
            (r"macaroni|makaroni", r"macaroni"), (r"rice\b|riisi", r"rice"), (r"carrot|porkkana", r"carrot"), (r"tomato|tomaatti", r"tomato"), (r"lingonberr|puolukka", r"lingonberr"), (r"strawberr|mansikka", r"strawberr")]
PLAIN = lambda t: re.sub(r"\b\w+[- ]free\b|\w+t[oö]n\b", " ", str(t).lower().replace("ä", "a").replace("ö", "o"))   # "nut-free" and "pähkinätön" promise nothing


def promised(dish: dict, ings: list[dict]) -> list[str]:
    """The ingredients this dish's name promises that its recipe does not have."""
    name, have = PLAIN(dish["name"]), " | ".join(PLAIN(i["item"]) for i in ings)
    return [want for word, want in PROMISES if re.search(word, name) and not re.search(want, have)]
ANIMAL = re.compile(r"\b(salmon|fish|saithe|herring|tuna|shrimp|chicken|broiler|turkey|beef|pork|bacon|ham|sausages?|frankfurters?|meat\w*|kala|liha|kana|lohi|nakki|kinkku|pekoni)\b", re.I)


def widen(codes) -> set:
    out = set(codes)
    for group, members in GROUPS.items():
        if group in out:
            out |= members
    return out


def carries(d, codes) -> set:
    return (set(d["contains"]) | set(d["may_contain"])) & codes


def animal(d) -> bool:
    return bool(ANIMAL.search(d["title"])) and not {"DIET:VEGAN", "DIET:VEGETARIAN"} & set(d["claims"])


# For portions under a dietary note: what makes a product wrong for them. From the manufacturer's data only.
WRONG = {
    "nut-free": lambda d: bool(carries(d, NUTS)),
    "peanut-free": lambda d: bool(carries(d, {"AP"})),
    "lactose-free": lambda d: bool({"AM", "ML"} & set(d["contains"])) and "FREE_FROM:LACTOSE" not in d["claims"],
    "gluten-free": lambda d: bool(carries(d, GLUTEN - ({"GO"} if "FREE_FROM:GLUTEN" in d["claims"] else set()))),
    "milk-free": lambda d: bool(carries(d, {"AM", "ML"})),
    "egg-free": lambda d: bool(carries(d, {"AE"})),
    "fish-free": lambda d: bool(carries(d, {"AF"})),
    "vegan": lambda d: bool(carries(d, {"AM", "ML", "AE", "AF", "AC", "UM"})) or animal(d),
    "vegetarian": animal,
}
CHECKS = [
    ("allergen", "No excluded allergen on any order", "order lines", "Every line of every order with an order-wide exclusion (a nut-free kitchen), against the manufacturer's contains and may-contain data."),
    ("diet", "Dietary portions get a product that fits", "dietary lines", "Each line that serves nut-free, peanut-free, lactose-free, gluten-free, milk-free, egg-free, vegan or vegetarian portions, against the manufacturer's data and claims, traces included."),
    ("guests", "A note about some guests stays with those guests", "dish ingredients", "A note with a head count never becomes a ban on the whole order. In every dish exactly that share of the portions is ordered under the note or held with a question, and every portion is accounted for."),
    ("named", "A dish keeps the ingredient it is named for", "dishes", "Each dish name, in English or Finnish, against its recipe. Where the named ingredient is missing the plan must flag the dish; where the dish is the allergen, those guests' portions must be held."),
    ("pack", "No pack far bigger than the need", "order lines", "Ordered against needed on every line. More than one and a half times the need is allowed only when the line says nothing smaller is sold and what is left over, and no smaller pack of the kitchen's own products was in stock."),
    ("pantry", "Staples left to the pantry are small and named", "pantry lines", "Every staple left off the order is listed with its amount, is at most a third of the smallest pack sold, matches the recipe cards' sums and is not on the order."),
    ("quantity", "Enough is ordered, and at most one pack too much", "ingredients", "Portions times the recipe card's amount plus its waste allowance, worked out again here, against pack size times packs ordered."),
    ("coverage", "Nothing is dropped silently", "dish ingredients", "Every ingredient of every dish is on an order line or named under not ordered. A refused dish is named once and none of it is ordered."),
    ("signed", "The order is the plan, and every signature verifies", "orders", "Lines and quantities on the signed order equal the plan. Valio Aimo's, the buyer's, the agent's and the approver's signatures are checked."),
    ("silence", "No allergen pass without data", "orders", "Where a product has no allergen declaration and something is excluded, the signed record shows a person cleared it. It never says the line passed."),
    ("substitute", "A substitute is marked as one", "substitute lines", "A product that differs from what the recipe names is marked on the signed order with what it replaces."),
]


def cases(n: int, seed: int) -> list[dict]:
    rnd, cards, out = random.Random(seed), list(menu.CARDS.values()), []
    for i in range(n):
        g = rnd.choice(GUESTS)
        k, j = max(1, round(g * rnd.choice([0.05, 0.1, 0.2]))), max(1, round(g * rnd.choice([0.05, 0.1])))
        out.append({"guests": g, "dishes": [c["name"] for c in rnd.sample(cards, rnd.randint(3, 8))], "notes": [t.format(k=k, j=j) for t in NOTES[i % len(NOTES)]]})
    n = 0
    for i, c in enumerate(out):  # after the draw, so the seed gives the same card menus as before
        if i % 5 == 4 or any(re.match(r"\{?\d+.*nut", t) for t in c["notes"]):
            c["dishes"].append(WRITTEN[n % len(WRITTEN)])
            n += 1
    return out


class Run:
    def __init__(self):
        self.g = Gateway()
        self.id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        self.tally = {c[0]: {"passed": 0, "failed": 0, "examples": []} for c in CHECKS}

    def mark(self, check: str, ok: bool, what: str = ""):
        t = self.tally[check]
        t["passed" if ok else "failed"] += 1
        if not ok and len(t["examples"]) < 5:
            t["examples"].append(what[:300])

    def one(self, i: int, case: dict, must_ask: str | None = None) -> dict:
        g, slug = self.g, f"mt-{self.id[9:15].lower()}-{i:03d}"
        acct = g.create_account(slug, f"Menu test kitchen {i + 1}", kind="sandbox")
        cfg = {**g.store.get("mandate_config:" + acct["customer"]), "max_units_per_order": 200000}  # a banquet is many sales units; the money limit still sends it to a person
        tok = g.issue_mandate(slug, constraints=cfg, agent=f"agent:{slug}/menu", name="Menu agent")
        p = g.identify(tok)
        rd = menu.normalize({"title": f"Menu test {i + 1}", "guests": case["guests"], "dishes": [{k: v for k, v in d.items() if k != "lost"} if isinstance(d, dict) else {"name": d} for d in case["dishes"]], "notes": case["notes"]}, "agent")
        lost = {d["name"]: d["lost"] for d in case["dishes"] if isinstance(d, dict) and d.get("lost")}
        row = {"case": i + 1, "guests": case["guests"], "dishes": [d["name"] for d in rd["dishes"]], "notes": case["notes"], "must_ask": must_ask, "written": [d["name"] for d in rd["dishes"] if not d["card"]], "lost": lost,
               "portions": sum((d["portions"] or rd["guests"] or 0) * d["days"] for d in rd["dishes"])}
        t0 = time.time()
        try:
            plan = menu.build_plan(g, rd, p)
        except GatewayError as e:
            return {**row, "outcome": "error", "why": e.message}
        row["plan_s"] = round(time.time() - t0, 2)
        if plan["status"] == "needs_answers":
            return {**row, "outcome": "asked", "questions": [q["text"] for q in plan["questions"]]}
        if plan["status"] != "ready":
            return {**row, "outcome": "nothing", "not_ordered": [o["text"] for o in plan.get("not_ordered", [])][:6]}
        ck = plan["checkout"]
        c = g.create_checkout({"line_items": ck["line_items"], "context": {"intent": ck["intent"]},
                               "wholesale": {"delivery_date": ck["delivery_date"], "rationale": ck["rationale"], "requirements": {"exclude_allergens": ck["exclude_allergens"]}}}, p, f"menu-eval-{i}")
        rules = c["wholesale"]["policy"]["rules"]
        row.update(lines=len(plan["lines"]), pantry=len(plan["pantry"]), flags=len(plan["flags"]), questions=sum(1 for o in plan["not_ordered"] if o.get("ask")), not_ordered=len(plan["not_ordered"]), held_dishes=sum(1 for o in plan["not_ordered"] if o.get("dish_refused")), held_portions=sum(o.get("portions_refused", 0) for o in plan["not_ordered"]),
                   asked_rules=sorted({r["id"] for r in rules if r["outcome"] == "escalate"}))
        if c["status"] == "requires_escalation":
            g.decide(c["id"], g.store.checkout(c["id"])["_internal"]["approval_token"], True, "Kitchen manager (menu test)", "")
        elif c["status"] != "ready_for_complete":
            return {**row, "outcome": "refused", "why": [r["detail"] for r in rules if r["outcome"] == "deny"][:3] or [m["content"] for m in c["messages"]][:3]}
        done = g.complete_checkout(c["id"], {"ap2": {"checkout_mandate": g.agent_proof(g.get_checkout(c["id"], p), tok)}}, p)
        if done.get("status") != "completed":
            return {**row, "outcome": "refused", "why": [m["content"] for m in done.get("messages", [])][:3]}
        order = g.get_order(done["order"]["id"])
        row.update(outcome="ordered", order=order["label"], subtotal=next(t["amount"] for t in order["totals"] if t["type"] == "subtotal"), total_s=round(time.time() - t0, 2),
                   approved_by_person=bool(order["wholesale"]["evidence"].get("approval")))
        row["cleared_lines"] = self.verify(rd, plan, order, p, case, lost)
        return row

    # ------------------------------------------------------------- the checks
    def verify(self, rd: dict, plan: dict, order: dict, p, case: dict, lost: dict) -> int:
        g, cat, label = self.g, self.g.cat, order["label"]
        ev = order["wholesale"]["evidence"]
        rules = {r["id"]: r for r in ev["policy"]["rules"] if r["id"] in ("A1",)}
        order_ex = widen(plan["checkout"]["exclude_allergens"])
        by_line = {l["product"]["id"]: l for l in plan["lines"]}
        # 1. order-wide exclusion, from the signed order's own lines
        for li in order["line_items"]:
            d = cat.get(li["item"]["id"])
            if order_ex:
                hit = carries(d, order_ex)
                self.mark("allergen", not hit, f"{label}: {d['title']} carries {sorted(hit)}")
        # 2. dietary portions
        for l in plan["lines"]:
            d = cat.get(l["product"]["id"])
            for diet in sorted({x for f in l["for"] for x in f["diet"]}):
                if diet in WRONG:
                    self.mark("diet", not WRONG[diet](d), f"{label}: {d['title']} serves {diet} portions")
        # 3 and 4. quantities and coverage, from the recipe cards
        left = {(o["dish"], o.get("item")) for o in plan["not_ordered"]}
        gone = {(o["dish"], o.get("item")) for o in plan["not_ordered"] if not o.get("portions_refused")}  # the item itself is missing; held portions only make the dish smaller
        shelf = {(dish, x["item"]) for x in plan["pantry"] for dish in x["dishes"]}
        recipe = lambda dish: menu.CARDS[dish["card"]]["ingredients"] if dish["card"] else dish["recipe"]
        refused = {o["dish"] for o in plan["not_ordered"] if o.get("dish_refused")}
        served = {(f["dish"], f["item"]) for l in plan["lines"] for f in l["for"]}
        waiting = {}  # dish -> portions named as not ordered because a dietary rule left no product for them
        for o in plan["not_ordered"]:
            if o.get("portions_refused"):
                waiting[o["dish"]] = waiting.get(o["dish"], 0) + o["portions_refused"]
        need = {}  # ingredient -> grams or millilitres the cards ask for, waste included
        for dish in rd["dishes"]:
            portions = (dish["portions"] or rd["guests"]) * dish["days"] - waiting.get(dish["name"], 0)
            for ing in recipe(dish):
                key = (dish["name"], ing["item"])
                if dish["name"] in refused:
                    self.mark("coverage", key not in served, f"{label}: {dish['name']} is refused but {ing['item']} is ordered for it")
                    continue
                self.mark("coverage", key in served or key in left or key in shelf or (dish["name"], None) in left, f"{label}: {ing['item']} for {dish['name']} is neither ordered, left to the pantry nor named")
                if key in served and key not in gone:
                    need[ing["item"]] = need.get(ing["item"], 0) + portions * ing["amount"] * (1 + ing.get("waste_pct", menu.WASTE) / 100)
        short_items = {i for _, i in gone}
        live = g._live(list(by_line), plan["delivery_date"], p)
        groups = []  # lines and ingredients that share a product are counted together
        for l in plan["lines"]:
            items = {f["item"] for f in l["for"]}
            hit = [gr for gr in groups if gr["items"] & items]
            gr = {"items": items.union(*[x["items"] for x in hit]), "lines": [l] + [y for x in hit for y in x["lines"]]}
            groups = [x for x in groups if x not in hit] + [gr]
        for gr in groups:
            units = {u for l in gr["lines"] for f in l["for"] for u in [f["per_portion"].split()[-1]]}
            if len(units) != 1 or gr["items"] & short_items or any(l.get("partial") for l in gr["lines"]) or not gr["items"] <= set(need):
                continue  # mixed units, or a shortage a person was told about: not a quantity this check can judge
            key = "l" if units == {"ml"} else "kg"
            got = slack = 0
            for l in gr["lines"]:
                d = cat.get(l["product"]["id"])
                c = (g._content(d, live[d["id"]], key) or 0) * 1000
                got += l["units"] * c
                slack += c * max(d["increment"] / 10 ** d["scale"], 1 if d["scale"] == 0 else 0.001)
            want = sum(need[i] for i in gr["items"])
            self.mark("quantity", want * 0.999 <= got <= want + slack + 1, f"{label}: {', '.join(sorted(gr['items']))} needs {want:,.0f} {units.pop()}, ordered {got:,.0f}")
        # 5. the signed order is the plan
        placed = {li["item"]["id"]: li["quantity"]["total"] for li in order["line_items"]}
        bad = signatures(order, g.audit(order["id"]), g.key.jwk)
        self.mark("signed", placed == {x["item"]["id"]: x["quantity"] for x in plan["checkout"]["line_items"]} and not bad, f"{label}: " + (", ".join(bad) or "lines differ from the plan"))
        # 6. silence is not a pass
        asked = widen(plan["checkout"]["exclude_allergens"]) | {c for li in order["line_items"] for c in ((li.get("wholesale") or {}).get("requirements") or {}).get("exclude_allergens", [])}
        silent = [li for li in order["line_items"] if asked and self.silent(cat.get(li["item"]["id"]), li, plan)]
        a1, ap = rules.get("A1"), ev.get("approval") or {}
        if silent:
            self.mark("silence", bool(a1) and a1["outcome"] == "escalate" and "A1" in ap.get("overrides", []) and ap.get("decision") == "approved" and bool(ap.get("by")),
                      f"{label}: {len(silent)} line(s) have no allergen declaration, and the record says {a1['outcome'] if a1 else 'nothing'}: {(a1 or {}).get('detail', '')[:120]}")
        elif asked:
            self.mark("silence", bool(a1) and a1["outcome"] in ("pass", "escalate"), f"{label}: exclusions asked, no allergen rule on the record")
        # 7. substitutes
        for l in plan["lines"]:
            if l.get("substitutes_for"):
                li = next(x for x in order["line_items"] if x["item"]["id"] == l["product"]["id"])
                self.mark("substitute", bool((li.get("wholesale") or {}).get("substitutes_for")), f"{label}: {l['product']['title']} replaces {l['substitutes_for'].get('title')} and is not marked")
        # 8. a note about some guests stays with those guests: no ban on the whole order, that share of every dish, every portion accounted for
        guests = rd["guests"]
        words_of = lambda note: {menu.DIET_WORD[x] for x in menu._kinds(note)}
        counted = [(int(m.group(1)), words_of(n)) for n in case["notes"] for m in [re.match(r"\s*(\d+)\b", n)] if m]
        kitchen = set().union(*[words_of(n) for n in case["notes"] if not re.match(r"\s*\d", n)])
        held = {}  # (dish, dietary word) -> portions held with a question
        for o in plan["not_ordered"]:
            for w in o.get("diet") or []:
                if o.get("portions_refused"):
                    held[(o["dish"], w)] = held.get((o["dish"], w), 0) + o["portions_refused"]
        fors = {}
        for l in plan["lines"]:
            for f in l["for"]:
                fors.setdefault((f["dish"], f["item"]), []).append(f)
        for k, words in counted:
            for w in sorted(words - kitchen):
                if w in BANS:
                    self.mark("guests", not order_ex & BANS[w], f"{label}: a note for {k} of {guests} guests ({w}) became an exclusion on the whole order: {sorted(order_ex & BANS[w])}")
            for dish in rd["dishes"]:
                total = (dish["portions"] or guests) * dish["days"]
                share = k * total / guests
                for ing in recipe(dish):
                    key = (dish["name"], ing["item"])
                    if dish["name"] in refused or key not in served or key in gone or words & kitchen:
                        continue
                    groups = {tuple(f["diet"]): f["portions"] for f in fors[key]}  # a group of portions split over two products (a shortage) is on both lines; it is counted once
                    under = sum(n for diet, n in groups.items() if words & set(diet)) + max(held.get((dish["name"], w), 0) for w in words)
                    every = sum(groups.values()) + waiting.get(dish["name"], 0)
                    self.mark("guests", share - 1e-6 <= under <= share + dish["days"] and every == total,
                              f"{label}: {ing['item']} for {dish['name']}: {under} of {every} portions are under “{', '.join(sorted(words))}”, the note is for {k} of {guests} guests and the dish is {total} portions")
        # 9. a dish keeps the ingredient it is named for, or the plan says so; a dish that is the allergen is held for the guests who cannot have it
        flagged = {f["dish"] for f in plan["flags"] if f["kind"] == "named_missing"}
        for dish in rd["dishes"]:
            ings, name = recipe(dish), dish["name"]
            if len(ings) > 1:  # one ingredient is something bought ready under its own name
                miss = promised(dish, ings)
                self.mark("named", (name in flagged) == bool(miss), f"{label}: {name} " + (f"has no {', '.join(miss)} in its recipe and the plan does not flag it" if miss else "is flagged though its recipe has what the name promises"))
            made_of = None if dish["card"] else "peanut-free" if re.search(PROMISES[0][0], PLAIN(name)) else "nut-free" if re.search(PROMISES[1][0], PLAIN(name)) else None
            for k, words in [(k, w) for k, w in counted] + [(None, kitchen)] if made_of else []:
                if not {made_of, "nut-free"} & words:
                    continue
                mine = [o for o in plan["not_ordered"] if o["dish"] == name and (o.get("ask") or o.get("dish_refused"))]
                total = (dish["portions"] or guests) * dish["days"]
                if k is None:
                    good = len(mine) == 1 and bool(mine[0].get("dish_refused")) and not any(d == name for d, _ in served)
                else:
                    good = (len(mine) == 1 and k * total / guests - 1e-6 <= mine[0].get("portions_refused", 0) <= k * total / guests + dish["days"] and bool(mine[0].get("ask"))
                            and not any(words & set(f["diet"]) for (d, _), fs in fors.items() if d == name for f in fs))
                self.mark("named", good, f"{label}: {name} is made of what “{', '.join(sorted(words))}” rules out, and the plan has {[(o.get('portions_refused'), o.get('dish_refused')) for o in mine]} for {k or 'all'} guests")
        # 10. packs: far more than the need only when nothing smaller is sold, and then the line says what is left over
        for l in plan["lines"]:
            pk = l["pack"]
            if l.get("partial"):
                continue
            good, why = True, ""
            if pk["ordered"] >= 1.5 * pk["need"] and pk["ordered"] - pk["need"] >= 1:
                good, why = bool(pk["nothing_smaller"] and pk["leftover_text"]), "and the line does not say that nothing smaller is sold and what is left over"
                smaller = good and not order_ex and not any(f["diet"] for f in l["for"]) and self.smaller(l, plan["delivery_date"], p)
                if smaller:
                    good, why = False, f"while {smaller} is smaller and in stock"
            self.mark("pack", good, f"{label}: {l['product']['title']}: {pk['ordered_text']} ordered for {pk['need_text']} {why}")
        # 11. the pantry
        on_order = {f["item"] for l in plan["lines"] for f in l["for"]}
        for x in plan["pantry"]:
            cards = [(dish, ing) for dish in rd["dishes"] if dish["name"] not in refused for ing in recipe(dish) if ing["item"] == x["item"]]
            want = sum(((dish["portions"] or guests) * dish["days"] - waiting.get(dish["name"], 0)) * ing["amount"] * (1 + ing.get("waste_pct", menu.WASTE) / 100) for dish, ing in cards)
            same = bool(cards) and all(ing["unit"] == x["unit"] for _, ing in cards)  # a concentrate is counted in another unit than the card's; its sum is not compared
            self.mark("pantry", x["item"] not in on_order and bool(x["amount"]) and not re.match(r"0(\.0+)? ", x["amount"]) and x["amount"] in x["text"]
                      and (x["smallest"] is None or x["need"] <= x["smallest"] / 3 + 1e-6) and (not same or abs(want - x["need"]) <= max(0.5, want * 0.01)),
                      f"{label}: pantry line {x['item']} {x['amount']} (need {x['need']}, the cards give {want:,.1f}, smallest pack {x['smallest']})")
        return len(silent)

    def smaller(self, line: dict, day: str, p) -> str | None:
        """A smaller pack of the kitchen's own products for this ingredient that is in stock and would cover the need with less. Worked out here from the catalogue and live stock."""
        g, pk, items = self.g, line["pack"], {f["item"] for f in line["for"]}
        if pk["unit"] not in ("g", "ml") or not all(i in menu.PANTRY for i in items):
            return None
        pids = sorted({pid for i in items for pid in menu.PANTRY[i]["prefer"]} - {line["product"]["id"]})
        live = g._live(pids, day, p) if pids else {}
        for pid in pids:
            d, f = g.cat.get(pid), live.get(pid) or {}
            c = d and f.get("orderable") and g._content(d, f, "l" if pk["unit"] == "ml" else "kg")
            if not c:
                continue
            step = d["increment"] / 10 ** d["scale"]
            pack = c * 1000 * step
            if f.get("stock_known") and math.floor(max(0.0, f["available"]) / step + 1e-9) * pack < pk["need"]:
                continue
            if math.ceil(pk["need"] / pack - 1e-9) * pack < pk["ordered"] - 1e-6:
                return d["title"]
        return None

    @staticmethod
    def silent(d, li, plan) -> bool:
        """A food line with nothing declared by its manufacturer, under an exclusion that no claim of its own settles."""
        if d["contains"] or d["may_contain"] or d.get("free_from") or str(d["category"])[:2] not in menu.FOOD_CATS:
            return False
        ex = set(plan["checkout"]["exclude_allergens"]) | set(((li.get("wholesale") or {}).get("requirements") or {}).get("exclude_allergens", []))
        claims = set(d["claims"])
        if "FREE_FROM:GLUTEN" in claims:
            ex -= GLUTEN
        if "DIET:VEGAN" in claims:
            ex -= {"AM", "ML", "AE", "AF", "AC", "UM"}
        if "FREE_FROM:EGGS" in claims:
            ex -= {"AE"}
        if "FREE_FROM:LACTOSE" in claims:
            ex -= {"ML"}
        return bool(ex)

    # ------------------------------------------------------------- the photos
    def photos(self, n: int) -> dict:
        out = {"reads_each": n, "samples": []}
        for s in menu.SAMPLES:
            image = "data:image/jpeg;base64," + base64.b64encode((ROOT / "web" / "samples" / s["file"]).read_bytes()).decode()
            printed = int(re.sub(r"\D", "", s["sub"]) or 0)
            def read(_):
                t = time.time()
                try:
                    rd, _, _ = menu.read_photo(image)
                except Exception as e:  # a model call that failed is a reading that did not happen; it is counted, not hidden
                    return {"error": str(e)[:120]}
                if rd is None:
                    return None
                q = menu.sanity(rd)
                q = q or menu.parse_notes(rd)[1]
                return {"guests": rd["guests"], "dishes": sorted(d["card"] or re.sub(r"\s*&\s*", " and ", d["name"].lower()) for d in rd["dishes"]), "notes": sorted(k for x in rd["notes"] for k in menu._kinds(x)), "asks": bool(q), "s": time.time() - t}

            with ThreadPoolExecutor(4) as pool:
                got = list(pool.map(read, range(n)))
            refused, seen = sum(1 for x in got if x is None), [x for x in got if x is not None]
            secs = [x["s"] for x in seen if "s" in x]
            if s["id"] == "notmenu":
                out["samples"].append({"id": s["id"], "title": s["title"], "reads": n, "refused": refused})
                continue
            good = [x for x in seen if "error" not in x]
            most = lambda key: max((json.dumps(x[key]) for x in good), key=[json.dumps(x[key]) for x in good].count) if good else None
            out["samples"].append({"id": s["id"], "title": s["title"], "printed_guests": printed, "reads": n, "failed_calls": len(seen) - len(good), "refused": refused,
                                   "guests_right": sum(1 for x in good if x["guests"] == printed), "same_dishes": sum(1 for x in good if json.dumps(x["dishes"]) == most("dishes")),
                                   "same_notes": sum(1 for x in good if json.dumps(x["notes"]) == most("notes")), "no_question": sum(1 for x in good if not x["asks"]), "dishes": len(json.loads(most("dishes") or "[]")),
                                   "differences": sorted({d for x in good for d in set(x["dishes"]) ^ set(json.loads(most("dishes") or "[]"))})[:8],
                                   "median_s": round(sorted(secs)[len(secs) // 2], 1) if secs else None})
        return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--cases", type=int, default=60)
    ap.add_argument("--seed", type=int, default=2026)
    ap.add_argument("--photos", type=int, default=0)
    ap.add_argument("--dry", action="store_true", help="print the result, write no files")
    a = ap.parse_args()
    run, t0, rows = Run(), time.time(), []
    todo = [(c, None) for c in cases(a.cases, a.seed)] + [(c, why) for why, c in MUST_ASK]
    for i, (case, why) in enumerate(todo):
        try:
            row = run.one(i, case, why)
        except Exception as e:  # one broken case must not hide the others; it is reported as broken
            row = {"case": i + 1, "guests": case["guests"], "dishes": [d["name"] if isinstance(d, dict) else d for d in case["dishes"]], "notes": case["notes"], "must_ask": why, "outcome": "error", "why": f"{type(e).__name__}: {e}"[:300]}
        rows.append(row)
        print(f"{row['case']:3} {row['outcome']:8} {row['guests']:5} guests  {len(row['dishes'])} dishes  {', '.join(row['notes']) or 'no notes':44} "
              + (f"{row['order']}  {row['lines']} lines, {row['not_ordered']} left" if row["outcome"] == "ordered" else str(row.get("why") or (row.get("questions") or row.get("not_ordered") or [""])[0])[:90]), flush=True)
    ordered, must = [r for r in rows if r["outcome"] == "ordered"], [r for r in rows if r["must_ask"]]
    checks = [{"id": c, "title": title, "unit": unit, "what": what, **run.tally[c]} for c, title, unit, what in CHECKS]
    kinds = lambda r: sorted({menu.DIET_WORD[k] for n in r["notes"] for k in menu._kinds(n)})
    doc = {
        "id": run.id, "at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"), "seconds": round(time.time() - t0), "seed": a.seed,
        "menus": len(rows) - len(must), "orders": len(ordered), "lines": sum(r["lines"] for r in ordered), "eur": round(sum(r["subtotal"] for r in ordered) / 100),
        "asked": sum(1 for r in rows if r["outcome"] == "asked" and not r["must_ask"]), "nothing": sum(1 for r in rows if r["outcome"] == "nothing"),
        "refused": sum(1 for r in rows if r["outcome"] == "refused"), "errors": sum(1 for r in rows if r["outcome"] == "error"),
        "must_ask": {"menus": len(must), "asked": sum(1 for r in must if r["outcome"] == "asked"), "ordered": sum(1 for r in must if r["outcome"] == "ordered"), "what": [r["must_ask"] for r in must]},
        "with_diet": sum(1 for r in rows if r["notes"] and not r["must_ask"]), "diets": {k: sum(1 for r in rows if not r["must_ask"] and k in kinds(r)) for k in sorted({k for r in rows for k in kinds(r)})},
        "written": sum(1 for r in rows if r.get("written")), "lost": sum(1 for r in rows if r.get("lost")), "flags": sum(r.get("flags", 0) for r in ordered), "questions": sum(r.get("questions", 0) for r in ordered), "pantry_lines": sum(r.get("pantry", 0) for r in ordered),
        "held_dishes": sum(r.get("held_dishes", 0) for r in ordered), "held_portions": sum(r.get("held_portions", 0) for r in ordered), "portions": sum(r["portions"] for r in ordered), "left_for_person": sum(r["not_ordered"] for r in ordered), "approved_by_person": sum(1 for r in ordered if r["approved_by_person"]),
        "cleared_lines": sum(r["cleared_lines"] for r in ordered), "median_plan_s": sorted(r["plan_s"] for r in ordered)[len(ordered) // 2] if ordered else None,
        "checks": checks, "checked": sum(c["passed"] + c["failed"] for c in checks), "failed": sum(c["failed"] for c in checks),
        "file": f"evals/menu-runs/{run.id}.jsonl", "rerun": "AIMO_DATA=/tmp/aimo-menu-eval uv run -q python -m agent.menu_evals",
    }
    if a.photos:
        doc["photos"] = run.photos(a.photos)
    if not a.dry:
        out = ROOT / "evals" / "menu-runs"
        out.mkdir(parents=True, exist_ok=True)
        (out / f"{run.id}.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")
        (ROOT / "evals" / "menu-latest.json").write_text(json.dumps(doc, indent=1) + "\n", encoding="utf-8")
    print(json.dumps({k: v for k, v in doc.items() if k not in ("checks", "photos")}, indent=1))
    for c in checks:
        print(f"{c['passed']:5} ok {c['failed']:4} failed  {c['title']}" + ("".join("\n        " + e for e in c["examples"]) if c["examples"] else ""))
    if a.photos:
        print(json.dumps(doc["photos"], indent=1))
    return 1 if doc["failed"] or doc["errors"] or doc["must_ask"]["ordered"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
