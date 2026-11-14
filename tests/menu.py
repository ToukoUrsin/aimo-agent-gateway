"""A menu becomes an order: the checks code makes between the model's reading and the signed order.
Run: AIMO_DATA=/tmp/aimo-menu-test uv run python -m tests.menu      (never against the main database)
     MENU_NO_MODEL=1 skips the two parts that call Gemini (the non-menu photo and the photo-to-order run over HTTP).

Covers: a typed menu planned in code into 30+ checked lines; "nut-free" keeping every tree-nut product out, a planted one included;
absurd readings (10,000 guests, a line that is not food) asked about and not ordered; a shortage solved by what the ingredient does
in the dish, with the reason on the line; a whipped topping never getting cooking cream; one signed order for the whole basket;
a photo that is not a menu refused; a sample photo placed as an order in under 60 seconds with /api/menu/latest reporting it.
"""
import copy
import os
import secrets
import time

from gateway import menu
from gateway.service import Gateway

MODEL = not os.environ.get("MENU_NO_MODEL")
g = Gateway()
slug = "menu-test-" + secrets.token_hex(3)
acct = g.create_account(slug, "Menu Test Kitchen", kind="sandbox")
tok = g.issue_mandate(slug, agent=f"agent:{slug}/menu", name="Menu agent")
p = g.identify(tok)
CREAM, COOKING = "469346", "464249"  # Arla Pro whipping cream 36% (stock-tracked), Valio cooking cream 15%
n = 0


def ok(cond, what):
    global n
    n += 1
    print(("ok   " if cond else "FAIL ") + what)
    assert cond, what


def reading(**change):
    return menu.normalize({**copy.deepcopy(menu.WEDDING), **change}, "card")


def plan(rd):
    return menu.build_plan(g, rd, p)


def roles_on(line):
    return {f["role"] for f in line["for"]}


def doc(line):
    return g.cat.get(line["product"]["id"])


def nutty(d):
    return bool({"AN", "AP"} & (set(d["contains"]) | set(d["may_contain"]))) or bool(menu.NUT_WORDS.search(d["_text"]))


# ---- 1. a typed menu, planned in code
base = plan(reading())
ok(base["status"] == "ready" and base["totals"]["lines"] >= 30, f"the wedding menu plans into {base['totals']['lines']} lines (30 or more)")
ok(all(c["ok"] for l in base["lines"] for c in l["checks"].values()) and all(set(l["checks"]) == {"pack", "allergens", "stock", "price"} for l in base["lines"]),
   "every line carries its four checks: pack size, allergens, stock, price")
ok(base["checkout"]["exclude_allergens"] == ["AN", "AP"] and len(base["checkout"]["line_items"]) == base["totals"]["lines"], "the plan is ready for create_checkout, with nuts and peanuts excluded on the whole order")
ok(not any(nutty(doc(l)) for l in base["lines"]), "nut-free: no line has a product that contains, may contain or names nuts")
lf = [l for l in base["lines"] if any("lactose-free" in f["diet"] for f in l["for"]) and {"AM", "ML"} & set(doc(l)["contains"])]
ok(lf and all("FREE_FROM:LACTOSE" in doc(l)["claims"] and "FREE_FROM:LACTOSE" in l["requirements"]["claims"] for l in lf), f"lactose-free notes: {len(lf)} milk lines carry the lactose-free claim and ask the gateway to enforce it")
parm = next(l for l in base["lines"] if any(f["item"] == "parmesan" for f in l["for"]))
ok("FREE_FROM:LACTOSE" in doc(parm)["claims"] and any("lists no allergens at all" in o["why"] for o in parm.get("passed_over", [])),
   "a cheese with no allergen data at all is passed over under lactose-free (missing data is not the same as no milk), and the line says so")
veg = [l for l in base["lines"] if any("vegetarian" in f["diet"] for f in l["for"])]
ok(veg and not any(str(doc(l)["category"] or "")[:3] in menu.MEAT_CATS for l in veg), "the vegetarian risotto gets no meat or fish")
units = {l["product"]["id"]: l for l in base["lines"]}
ok(all(abs(l["units"] / (doc(l)["increment"] / 10 ** doc(l)["scale"]) - round(l["units"] / (doc(l)["increment"] / 10 ** doc(l)["scale"]))) < 1e-6 for l in base["lines"]), "every quantity is a whole number of order steps")

# ---- 2. a planted tree-nut product never gets in
day = base["delivery_date"]
first = next(l for l in base["lines"] if l["source"] == "pantry" and len({f["item"] for f in l["for"]}) == 1 and menu.PANTRY[l["for"][0]["item"]]["unit"] == "g")
item = first["for"][0]["item"]
cands = [d["id"] for d in g.cat.docs if "AN" in d["contains"] and d["content"].get("kg") and str(d["category"] or "")[:2] in menu.FOOD_CATS][:25]
live = g._live(cands, day, p)
nut = next(pid for pid in cands if live[pid].get("orderable") and not live[pid].get("stock_known"))
kept = list(menu.PANTRY[item]["prefer"])
menu.PANTRY[item]["prefer"] = [nut] + kept
try:
    loose = plan(reading(notes=["Table 6 lactose-free"]))
    ok(any(l["product"]["id"] == nut for l in loose["lines"]), f"without a nut note the planted product ({g.cat.get(nut)['title']}) is ordered for {item}, so the test bites")
    tight = plan(reading())
    ok(not any(l["product"]["id"] == nut for l in tight["lines"]) and any(f["item"] == item for l in tight["lines"] for f in l["for"]),
       f"with “Nut-free kitchen” the planted product is passed over and {item} comes from the next choice")
finally:
    menu.PANTRY[item]["prefer"] = kept
extra = reading(dishes=menu.WEDDING["dishes"] + [{"name": "Nut brittle", "source": "agent", "recipe": [{"item": "hazelnuts", "role": "main", "amount": 30, "unit": "g"}, {"item": "sugar", "role": "sweetener", "amount": 20, "unit": "g"}]}])
brittle = plan(extra)
ok(not any(nutty(doc(l)) for l in brittle["lines"]) and any(o["item"] == "hazelnuts" and o["kind"] in ("no_fit", "not_in_range") for o in brittle["not_ordered"]),
   "a recipe that asks for hazelnuts in a nut-free kitchen: listed as not ordered, nothing with nuts on the order")

# An essential refused ingredient holds the whole dish, including its otherwise
# acceptable flour, egg and sugar, as one question.
cake_reading = copy.deepcopy(menu.WEDDING)
cake_reading["dishes"] = copy.deepcopy(cake_reading["dishes"])
cake_reading["dishes"][5] = {"name": "Hazelnut praline cake", "source": "agent", "recipe": [
    {"item": "hazelnuts", "role": "main", "amount": 30, "unit": "g"},
    {"item": "wheat flour", "role": "body", "amount": 20, "unit": "g"},
    {"item": "egg", "role": "body", "amount": 35, "unit": "g"},
    {"item": "sugar", "role": "sweetener", "amount": 28, "unit": "g"},
]}
blocked_cake = plan(menu.normalize(cake_reading, "card"))
cake_question = next((o for o in blocked_cake["not_ordered"] if o.get("dish") == "Hazelnut praline cake"), {})
ok(not any(any(f["dish"] == "Hazelnut praline cake" for f in l["for"]) for l in blocked_cake["lines"])
   and sum(o.get("dish") == "Hazelnut praline cake" for o in blocked_cake["not_ordered"]) == 1
   and "nut-free" in cake_question.get("text", ""),
   "a dish refused by its nut rule is one question and none of its other ingredients is ordered")

# ---- 3. wrong or absurd readings are asked about, not ordered
def asked(rd, word):
    out = plan(rd)
    return out["status"] == "needs_answers" and "lines" not in out and "checkout" not in out and any(word in q["text"] for q in out["questions"])


ok(asked(reading(guests=10000), "10,000 guests"), "10,000 guests: asked, nothing planned")
ok(asked(reading(guests=None), "does not say how many"), "no number of guests: asked")
ok(asked(reading(dishes=menu.WEDDING["dishes"] + [{"name": "Table decorations", "food": False}]), "does not look like food"), "a line that is not food: asked")
ok(asked(reading(dishes=[{"name": "Creamy salmon soup", "portions": 5000}]), "5,000 portions"), "one dish with 5,000 portions for 180 guests: asked")
ok(asked(reading(notes=["400 gluten-free"]), "more than the 180"), "a dietary note for more people than there are guests: asked")
ok(asked(reading(notes=["ask Maija about table 4"]), "could not be turned into a rule"), "a note code cannot turn into a rule: asked, not dropped")
ok(asked(reading(notes=["gluten-free croissants for 5"]), "no ingredient on this menu matches"), "a note about an ingredient the menu does not have: asked, not silently ignored")
ok(asked(reading(dishes=[{"name": "Chef's surprise"}]), "no recipe card"), "a dish with no card and no recipe: asked")
ok(asked(reading(dishes=[{"name": "Odd stew", "recipe": [{"item": "potatoes", "role": "body", "amount": 9000, "unit": "g"}]}]), "does not add up to a portion"), "a recipe with 9 kg of potatoes per portion: asked")
api = menu.plan_menu_order(g, {"dishes": ["Creamy salmon soup", "Strawberry cream cake"], "guests": 10000}, p)
ok(api["status"] == "needs_answers" and "checkout" not in api and "Do not order" in api["next"], "plan_menu_order tells an agent to ask and order nothing")

# ---- 4. a shortage is solved by what the ingredient does in the dish
pl = menu.Planner(g, p)
ok(pl.role_fit("sauce base", COOKING)[0] and not pl.role_fit("whipped topping", COOKING)[0] and pl.role_fit("whipped topping", CREAM)[0],
   "the role table: cooking cream 15% fits a sauce base, not a whipped topping; whipping cream 36% fits a whipped topping")
need = sum(l["units"] for l in base["lines"] if l["product"]["id"] == CREAM)
have = g._live([CREAM], day, p)[CREAM]["available"]
ok(CREAM in units and roles_on(units[CREAM]) >= {"whipped topping", "sauce base", "soup base"} and need > 20, f"with full stock one whipping cream serves the soup, the sauce and the cake ({need:g} of {have:g})")


def short(leave):
    g.store.hold("menu_test_short", [(CREAM, have - leave)], time.time() + 3600, acct["world"])
    return plan(reading())


try:
    s20 = short(20)
    by = {l["product"]["id"]: l for l in s20["lines"]}
    sub = next((l for l in s20["lines"] if (l.get("substitutes_for") or {}).get("id") == CREAM), None)
    ok(by[CREAM]["units"] == 20 and by[CREAM]["partial"] and "whipped topping" in roles_on(by[CREAM]), "20 left: the short cream line is kept at 20 and the whipped topping is served first")
    ok(sub and sub["product"]["id"] == COOKING and not roles_on(sub) & {"whipped topping"} and roles_on(sub) <= {"sauce base", "soup base"}, "20 left: the sauce and the soup move to cooking cream 15%")
    ok("is short" in sub["reason"] and "sauce base needs cream that stands heat" in sub["reason"] and "15% fat" in sub["reason"], "the reason is on the line in plain words: " + sub["reason"][:110] + "...")
    ok(all(pl.role_fit(f["role"], l["product"]["id"])[0] for l in s20["lines"] for f in l["for"] if f["role"] == "whipped topping"), "no line serves a whipped topping with a cream that does not whip")
    ok(all(c["ok"] for l in s20["lines"] for c in l["checks"].values()) and "Only 20" in by[CREAM]["checks"]["stock"]["text"], "the stock check on the short line says what can be supplied")

    s8 = short(8)
    top = [l for l in s8["lines"] if "whipped topping" in roles_on(l)]
    ok(len(top) == 2 and all(pl.role_fit("whipped topping", l["product"]["id"])[0] for l in top) and COOKING not in {l["product"]["id"] for l in top},
       "8 left: the whipped topping is split over two creams that both whip, never cooking cream")
    other = next(l for l in top if l["product"]["id"] != CREAM)
    ok(other["substitutes_for"]["id"] == CREAM and "Cooking cream would not whip" in other["reason"], "8 left: the second whipping cream is flagged as a substitute and says why cooking cream is not used")

    fam, prefer = dict(menu.FAMILY), list(menu.PANTRY["whipping cream"]["prefer"])
    for pid in [x for x, f in fam.items() if f == "cream" and x != CREAM and pl.role_fit("whipped topping", x)[0]]:
        del menu.FAMILY[pid]
    menu.PANTRY["whipping cream"]["prefer"] = [CREAM]
    try:
        alone = short(8)
    finally:
        menu.FAMILY.clear(); menu.FAMILY.update(fam)
        menu.PANTRY["whipping cream"]["prefer"] = prefer
    top = [l for l in alone["lines"] if "whipped topping" in roles_on(l)]
    gap = [o for o in alone["not_ordered"] if o["kind"] == "short" and o.get("role") == "whipped topping"]
    ok([l["product"]["id"] for l in top] == [CREAM] and top[0]["units"] == 8 and top[0]["partial"], "no other cream whips: the topping line is kept short at 8, not swapped")
    ok(len(gap) == 1 and "cooking cream is not used" in gap[0]["text"] and "a person decides" in gap[0]["text"], "and the gap goes to a person: " + gap[0]["text"][:120] + "...")
    ok(any(l["product"]["id"] == COOKING and not roles_on(l) & {"whipped topping"} for l in alone["lines"]), "while the sauce and the soup still get cooking cream")

    # ---- 5. one signed offer, one order, with the shortage on
    s20 = short(20)
    ck = s20["checkout"]
    c = g.create_checkout({"line_items": ck["line_items"], "context": {"intent": ck["intent"]},
                           "wholesale": {"delivery_date": ck["delivery_date"], "rationale": ck["rationale"], "requirements": {"exclude_allergens": ck["exclude_allergens"]}}}, p, "menu-test")
    rules = {r["id"]: r["outcome"] for r in c["wholesale"]["policy"]["rules"]}
    ok(c["status"] in ("ready_for_complete", "requires_escalation") and "deny" not in rules.values() and len(c["line_items"]) == s20["totals"]["lines"],
       f"the whole basket is one signed offer of {len(c['line_items'])} lines; no rule refuses it ({', '.join(k for k, v in rules.items() if v == 'escalate') or 'none'} ask for a person)")
    ok(bool((c.get("ap2") or {}).get("merchant_authorization")), "the offer is signed by the gateway")
    if c["status"] == "requires_escalation":
        g.decide(c["id"], g.store.checkout(c["id"])["_internal"]["approval_token"], True, "Kitchen manager (test)", "Approved in the test.")
    done = g.complete_checkout(c["id"], {"ap2": {"checkout_mandate": g.agent_proof(g.get_checkout(c["id"], p), tok)}}, p)
    order = g.get_order(done["order"]["id"])
    ok(done["status"] == "completed" and len(order["line_items"]) >= 30, f"order {order['label']} is placed with {len(order['line_items'])} lines")
    ok(not any(nutty(g.cat.get(l["item"]["id"])) for l in order["line_items"]), "the placed order has no product with nuts")
    a1 = next(r for r in order["wholesale"]["evidence"]["policy"]["rules"] if r["id"] == "A1")
    silent = [l for l in s20["lines"] if l["checks"]["allergens"].get("ask")]
    ok(a1["outcome"] == "escalate" and len(a1.get("undeclared") or []) == len(silent) > 0 and f"{len(silent)} of {len(order['line_items'])} lines have no allergen declaration" in a1["detail"],
       "a product whose manufacturer declared no allergens is not passed on data: " + a1["detail"][:150] + "...")
    ok("A1" in order["wholesale"]["evidence"]["approval"]["overrides"] and s20["totals"]["allergen_ask"] == len(silent), "the person who approved is on record as the one who cleared them")
    again = plan(reading())
    c2 = g.create_checkout({"line_items": again["checkout"]["line_items"], "context": {"intent": again["checkout"]["intent"]},
                            "wholesale": {"delivery_date": again["checkout"]["delivery_date"], "rationale": again["checkout"]["rationale"], "requirements": {"exclude_allergens": again["checkout"]["exclude_allergens"]}}}, p, "menu-test")
    a1b = next(r for r in c2["wholesale"]["policy"]["rules"] if r["id"] == "A1")
    ok(a1b["outcome"] == "pass" and "cleared for this exclusion by Kitchen manager (test)" in a1b["detail"] and "no line contains or may contain" in a1b["detail"].lower() and "checked on" in a1b["detail"]
       and again["totals"]["allergen_ask"] == 0, "the same menu again is not asked twice: " + a1b["detail"][-120:])
    ok(any(x.get("wholesale", {}).get("substitutes_for") for x in order["line_items"]),
       "the signed order marks a product that differs from the recipe ingredient as a substitute")
finally:
    g.store.settle("menu_test_short", "released")

# ---- 6. the photo: a non-menu refused, a sample placed as an order over HTTP (calls Gemini)
if MODEL:
    from fastapi.testclient import TestClient

    from gateway.app import app
    web = TestClient(app, base_url="https://testserver")  # https so the visitor's session cookie is kept whatever PUBLIC_URL is
    r = web.post("/api/menu/read", json={"sample": "notmenu"}).json()
    ok(r["status"] == "not_a_menu" and not r.get("reading") and not r.get("plan") and "Nothing was read and nothing is ordered" in r["message"], "the photo of a closing checklist is refused: " + r["message"][:90])
    ok(len(r["usage"]["calls"]) == 1 and r["usage"]["calls"][0]["step"] == "is it a menu" and r["usage"]["tokens_in"] == r["usage"]["calls"][0]["in"],
       "a refused photo is charged only for the guard call, using the API usage fields")
    ok(web.post("/api/menu/plan", json={"run": r["id"]}).status_code == 409 and web.post("/api/menu/order", json={"run": r["id"]}).status_code == 409, "and a refused photo cannot be planned or ordered")
    t = time.time()
    r = web.post("/api/menu/read", json={"sample": "wedding"}).json()
    ok(r["status"] == "read" and r["reading"]["guests"] == 180 and len(r["reading"]["dishes"]) >= 7 and any("nut" in x.lower() for x in r["reading"]["notes"]),
       f"the wedding photo is read: {r['reading']['guests']} guests, {len(r['reading']['dishes'])} dishes, notes {r['reading']['notes']}")
    r = web.post("/api/menu/plan", json={"run": r["id"]}).json()
    ok(r["status"] == "ready" and r["plan"]["totals"]["lines"] >= 30 and r["checkout"]["signed"], f"planned into {r['plan']['totals']['lines']} lines with a signed offer")
    ok(not any(nutty(g.cat.get(l["product"]["id"])) for l in r["plan"]["lines"]), "nut-free from the photo: no product with nuts in the basket")
    r = web.post("/api/menu/order", json={"run": r["id"]}).json()
    took = time.time() - t
    ok(r["status"] == "ordered" and r["order"]["label"] and took < 60 and r["seconds"]["total"] < 60, f"photo to placed order in {r['seconds']['total']:.1f} s (under 60), order {r['order']['label']}")
    ok(r["usage"]["tokens_in"] > 0 and r["usage"]["tokens_out"] > 0 and 0 < r["usage"]["eur"] < 0.05, f"model cost from real usage: {r['usage']['tokens_in']} tokens in, {r['usage']['tokens_out']} out, €{r['usage']['eur']:.4f}")
    last = web.get("/api/menu/latest").json()
    ok(last["real"] and last["run"] == r["id"] and last["seconds"] == r["seconds"]["total"] and last["line_count"] == len(last["lines"]) >= 30 and last["cost"]["eur"] == r["usage"]["eur"]
       and last["by_hand"]["basis"] in ("measured", "estimate") and last["by_hand"]["minutes"] and last["by_hand"]["how"], f"/api/menu/latest reports that run; by hand: {last['by_hand']['minutes']} min ({last['by_hand']['basis']})")
    typed = web.post("/menu/plan", headers={"authorization": "Bearer " + tok}, json={"dishes": ["Creamy salmon soup", {"name": "Mushroom risotto", "portions": 14, "notes": ["vegetarian"]}, "Strawberry cream cake"],
                                                                                    "guests": 180, "event_date": "2026-11-21", "notes": ["Nut-free kitchen"]}).json()
    ok(typed["status"] == "ready" and typed["checkout"]["line_items"] and typed["usage"]["tokens_in"] == 0 and typed.get("proof"), f"POST /menu/plan with typed dishes: {typed['totals']['lines']} lines, signed, no model used")
    ok(web.post("/menu/plan", json={"dishes": ["Creamy salmon soup"], "guests": 20}).status_code == 401, "POST /menu/plan without a mandate is refused")

print(f"\n{n} checks passed" + ("" if MODEL else " (the parts that call Gemini were skipped)"))
