"""Menu test: many menus through the same path as the menu page, then a separate check of every signed order.

A case is a menu: dishes from the kitchen's recipe cards, a head count, dietary notes. The gateway plans it in code, makes one
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
         ["Nut-free kitchen", "{k} lactose-free"], ["Nut-free kitchen", "{k} gluten-free"], ["{k} lactose-free", "{j} vegetarian"], ["Nut-free kitchen", "{k} milk-free", "{j} gluten-free"]]
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
    ("diet", "Dietary portions get a product that fits", "dietary lines", "Each line that serves lactose-free, gluten-free, milk-free, egg-free, vegan or vegetarian portions, against the manufacturer's data and claims."),
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
        rd = menu.normalize({"title": f"Menu test {i + 1}", "guests": case["guests"], "dishes": [d if isinstance(d, dict) else {"name": d} for d in case["dishes"]], "notes": case["notes"]}, "agent")
        row = {"case": i + 1, "guests": case["guests"], "dishes": [d["name"] for d in rd["dishes"]], "notes": case["notes"], "must_ask": must_ask,
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
        row.update(lines=len(plan["lines"]), not_ordered=len(plan["not_ordered"]), held_dishes=sum(1 for o in plan["not_ordered"] if o.get("dish_refused")), held_portions=sum(o.get("portions_refused", 0) for o in plan["not_ordered"]),
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
        row["cleared_lines"] = self.verify(rd, plan, order, p)
        return row

    # ------------------------------------------------------------- the checks
    def verify(self, rd: dict, plan: dict, order: dict, p) -> int:
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
        refused = {o["dish"] for o in plan["not_ordered"] if o.get("dish_refused")}
        served = {(f["dish"], f["item"]) for l in plan["lines"] for f in l["for"]}
        waiting = {}  # dish -> portions named as not ordered because a dietary rule left no product for them
        for o in plan["not_ordered"]:
            if o.get("portions_refused"):
                waiting[o["dish"]] = waiting.get(o["dish"], 0) + o["portions_refused"]
        need = {}  # ingredient -> grams or millilitres the cards ask for, waste included
        for dish in rd["dishes"]:
            portions = (dish["portions"] or rd["guests"]) * dish["days"] - waiting.get(dish["name"], 0)
            for ing in menu.CARDS[dish["card"]]["ingredients"]:
                key = (dish["name"], ing["item"])
                if dish["name"] in refused:
                    self.mark("coverage", key not in served, f"{label}: {dish['name']} is refused but {ing['item']} is ordered for it")
                    continue
                self.mark("coverage", key in served or key in left or (dish["name"], None) in left, f"{label}: {ing['item']} for {dish['name']} is neither ordered nor named")
                if key in served and key not in left:
                    need[ing["item"]] = need.get(ing["item"], 0) + portions * ing["amount"] * (1 + ing.get("waste_pct", menu.WASTE) / 100)
        short_items = {o.get("item") for o in plan["not_ordered"]}
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
        return len(silent)

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
    a = ap.parse_args()
    run, t0, rows = Run(), time.time(), []
    todo = [(c, None) for c in cases(a.cases, a.seed)] + [(c, why) for why, c in MUST_ASK]
    for i, (case, why) in enumerate(todo):
        try:
            row = run.one(i, case, why)
        except Exception as e:  # one broken case must not hide the others; it is reported as broken
            row = {"case": i + 1, "guests": case["guests"], "dishes": case["dishes"], "notes": case["notes"], "must_ask": why, "outcome": "error", "why": f"{type(e).__name__}: {e}"[:300]}
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
        "held_dishes": sum(r.get("held_dishes", 0) for r in ordered), "held_portions": sum(r.get("held_portions", 0) for r in ordered), "portions": sum(r["portions"] for r in ordered), "left_for_person": sum(r["not_ordered"] for r in ordered), "approved_by_person": sum(1 for r in ordered if r["approved_by_person"]),
        "cleared_lines": sum(r["cleared_lines"] for r in ordered), "median_plan_s": sorted(r["plan_s"] for r in ordered)[len(ordered) // 2] if ordered else None,
        "checks": checks, "checked": sum(c["passed"] + c["failed"] for c in checks), "failed": sum(c["failed"] for c in checks),
        "file": f"evals/menu-runs/{run.id}.jsonl", "rerun": "AIMO_DATA=/tmp/aimo-menu-eval uv run -q python -m agent.menu_evals",
    }
    if a.photos:
        doc["photos"] = run.photos(a.photos)
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
