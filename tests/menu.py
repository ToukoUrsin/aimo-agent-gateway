"""A menu becomes an order: the checks code makes between the model's reading and the signed order.
Run: AIMO_DATA=/tmp/aimo-menu-test uv run python -m tests.menu      (never against the main database)
     MENU_NO_MODEL=1 skips the two parts that call Gemini (the non-menu photo and the photo-to-order run over HTTP).

Covers: a typed menu planned in code into 30+ checked lines; "nut-free" keeping every tree-nut product out, a planted one included;
absurd readings (10,000 guests, a line that is not food) asked about and not ordered; a shortage solved by what the ingredient does
in the dish, with the reason on the line; a whipped topping never getting cooking cream; one signed order for the whole basket;
a photo that is not a menu refused; a sample photo placed as an order in under 60 seconds with /api/menu/latest reporting it.
A dish keeps the ingredient it is named for (a recipe without it is flagged, never silent); a note about some guests holds only their
portions, dish by dish, and only a note about the whole kitchen bans a thing on the whole order; the smallest pack that covers the need,
leftovers shown, small amounts in grams; kitchen staples needed in a small part of a pack listed as "from the pantry" with one switch to
order them; a budget on the menu compared with the order; the same request sent twice answered once; and the eight photos in
tests/menus/ (six messy real menus, a receipt, a price list) read and planned through the real path against tests/menus/expected.json.
"""
import base64
import copy
import json
import os
import pathlib
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

# ---- 2b. the ingredient a dish is named for, notes about some of the guests, packs, the pantry, the budget
SATAY = {"name": "Peanut satay chicken skewers", "source": "agent", "recipe": [
    {"item": "chicken breast", "role": "main", "amount": 130, "unit": "g"}, {"item": "peanuts", "fi": "maapähkinä", "role": "sauce base", "amount": 30, "unit": "g"},
    {"item": "rapeseed oil", "role": "frying fat", "amount": 5, "unit": "ml"}, {"item": "garlic", "role": "seasoning", "amount": 3, "unit": "g"}, {"item": "salt", "role": "seasoning", "amount": 1, "unit": "g"}]}
SALAD = {"name": "Green salad", "source": "agent", "recipe": [
    {"item": "lettuce", "role": "body", "amount": 50, "unit": "g"}, {"item": "cucumber", "role": "vegetable", "amount": 40, "unit": "g"}, {"item": "tomato", "role": "vegetable", "amount": 40, "unit": "g"},
    {"item": "olive oil", "role": "seasoning", "amount": 5, "unit": "ml"}, {"item": "black pepper", "role": "seasoning", "amount": 0.3, "unit": "g"}]}
PARTY = {"title": "Staff party", "date": menu.WEDDING["date"], "guests": 60, "notes": ["8 guests severe peanut allergy"], "budget": {"amount": 900, "per": "total", "text": "Budget 900 €"},
         "dishes": [SATAY, {"name": "Creamy salmon soup"}, SALAD, {"name": "Coffee and tea"}]}


def party(**change):
    return menu.normalize({**copy.deepcopy(PARTY), **change}, "agent")


def has(line, codes):
    return bool(set(codes) & (set(doc(line)["contains"]) | set(doc(line)["may_contain"])))


no_peanut = [i for i in SATAY["recipe"] if i["item"] != "peanuts"]
ok([c["label"] for c in menu.missing_named(SATAY["name"], "", no_peanut)] == ["peanuts"] and not menu.missing_named(SATAY["name"], "", SATAY["recipe"]),
   "a satay recipe without peanuts is caught in code against the dish name; the same recipe with peanuts passes")
ok(not menu.missing_named("Lohikeitto", "", [{"item": "salmon fillet"}]) and menu.missing_named("Lohikeitto", "", [{"item": "potato"}]) and not menu.named("Pähkinätön suklaakakku") == menu.named("Pähkinäkakku")
   and not menu.name_codes("Nut-free brownie") and menu.name_codes("Pähkinäkakku"), "dish names are read in Finnish too (lohikeitto is salmon), and “nut-free” or “pähkinätön” in a name is not a nut")
ok(not [(c["id"], a) for c in menu.CARDS.values() for a in [c["name"], *c["aliases"]] if len(c["ingredients"]) > 1 and menu.missing_named(a, a, c["ingredients"])],
   "every kitchen recipe card that is cooked from ingredients has what its own names promise")
lost = plan(party(dishes=[{**SATAY, "recipe": no_peanut}, SALAD]))
fl = [f for f in lost.get("flags", []) if f["kind"] == "named_missing"]
ok(len(fl) == 1 and fl[0]["ingredients"] == ["peanuts"] and "named for peanuts" in fl[0]["text"] and fl[0]["dish"] == SATAY["name"], "a recipe that lost the named ingredient is flagged on the plan, never silent: " + fl[0]["text"][:100] + "...")
ok(any(o.get("dish") == SATAY["name"] and (o.get("ask") or {}).get("portions") == 8 and o.get("named") for o in lost["not_ordered"]),
   "and the satay is still held for the 8 guests with a peanut allergy: the dish name is enough, whatever the recipe says")

pp = plan(party())
rule = pp["diet"][0]
ok(pp["status"] == "ready" and rule["scope"] == "guests" and rule["count"] == 8 and rule["severe"] and pp["requirements"]["exclude_allergens"] == [] and pp["checkout"]["exclude_allergens"] == [],
   "“8 guests severe peanut allergy” is a rule for 8 of 60 guests, not a ban on the whole order: " + rule["rule"][:90] + "...")
qs = [o for o in pp["not_ordered"] if o.get("dish") == SATAY["name"] and o.get("ask")]
ok(len(qs) == 1 and qs[0]["ask"] == {"portions": 8, "rest": 52, "choices": ["for_them", "for_everyone"], "severe": True} and qs[0]["named"]
   and "Change the dish for those 8, or for everyone?" in qs[0]["text"] and "Nothing is ordered for those 8" in qs[0]["text"], "the satay holds 8 portions and asks one plain question: " + qs[0]["text"][:140] + "...")
ok("The allergy is severe" in qs[0]["text"] and "safer answer" in qs[0]["text"], "severe: the question says the other 52 portions bring peanuts into the kitchen and which answer is safer")
sat = [(l, f) for l in pp["lines"] for f in l["for"] if f["dish"] == SATAY["name"]]
ok(sat and all(f["portions"] == 52 and not f["diet"] for _, f in sat) and any(f["item"] == "chicken breast" for _, f in sat), f"the other 52 portions of the satay are ordered ({len(sat)} ingredients), none for the 8")
free = [l for l in pp["lines"] if any("peanut-free" in f["diet"] for f in l["for"])]
ok(len(free) >= 8 and not any(has(l, {"AP"}) or menu.NUT_WORDS.search(doc(l)["_text"]) for l in free) and all(any(f["portions"] == 8 and "peanut-free" in f["diet"] for f in l["for"]) or all(f["portions"] == 60 for f in l["for"]) for l in free),
   f"in every other dish the 8 portions get products with no peanuts, traces included ({len(free)} lines)")
nuts = [l for l in pp["lines"] if has(l, {"AP"})]
ok(not nuts or (any(w["kind"] == "cross_contact" and "8 guests severe peanut allergy" in w["text"] for w in pp["warnings"]) and all(not f["diet"] for l in nuts for f in l["for"])),
   "severe: a product with peanuts ordered for the other guests is named in a warning to keep it apart" if nuts else "no product with peanuts could be ordered for this day, so there is nothing to warn about")
warn = next((w for w in pp["warnings"] if w["kind"] == "cross_contact"), None)
blank_rest = [l for l in pp["lines"] if not has(l, {"AP"}) and not (doc(l)["contains"] or doc(l)["may_contain"] or doc(l).get("free_from")) and "AP" not in ((l.get("requirements") or {}).get("exclude_allergens") or [])]
ok(warn is None or (warn["undeclared"] == len(blank_rest) and (not blank_rest or "more" in warn["text"] and blank_rest[0]["product"]["title"] in warn["text"])),
   f"the warning counts only the undeclared lines that are for the other guests alone ({len(blank_rest)}); the ones for the 8 are already asked from the approver, so no line is counted twice")
lac = plan(party(notes=["5 lactose-free"]))["diet"][0]
ok(lac["scope"] == "guests" and lac["count"] == 5 and "for that share of the portions in every dish, any product with milk in it must carry" in lac["rule"], "a note for 5 lactose-free guests reads as a sentence: " + lac["rule"][:120])
ok(plan(party(notes=["12 nut-free"]))["checkout"]["exclude_allergens"] == [] and plan(party(notes=["12 nut-free"]))["diet"][0]["count"] == 12
   and set(plan(party(notes=["Nut-free kitchen"]))["checkout"]["exclude_allergens"]) == {"AN", "AP"}, "“12 nut-free” covers 12 guests; only “Nut-free kitchen” excludes nuts on the whole order")
kitchen = plan(party(notes=["Nut-free kitchen"]))
kq = [o for o in kitchen["not_ordered"] if o.get("dish") == SATAY["name"]]
ok(len(kq) == 1 and kq[0].get("dish_refused") and kq[0]["ask"]["choices"] == ["for_everyone", "remove"] and not any(f["dish"] == SATAY["name"] for l in kitchen["lines"] for f in l["for"]),
   "in a nut-free kitchen the whole satay waits for one answer and none of it is ordered")

# packs
small = plan(reading(guests=8, notes=[]))
every = [("wedding for 180", base), ("wedding for 8", small), ("staff party", pp)]
far = [(name, l["product"]["title"], l["pack"]["need_text"], l["pack"]["ordered_text"]) for name, x in every for l in x["lines"]
       if l["pack"]["ordered"] >= menu.FAR * l["pack"]["need"] and l["pack"]["ordered"] - l["pack"]["need"] >= 1 and not l["partial"] and not (l["pack"]["nothing_smaller"] and l["pack"]["leftover_text"])]
ok(not far, "no line orders a pack far bigger than the need unless nothing smaller is sold, and then it says what is left over" + (": " + str(far[:3]) if far else ""))
big = [l for l in small["lines"] if l["pack"]["leftover"]]
ok(big and all(l["pack"]["leftover_text"] and "is left over" in l["checks"]["pack"]["text"] for l in big), f"a dinner for 8: {len(big)} lines say what is left over, e.g. " + big[0]["checks"]["pack"]["text"][-90:])
onion = next(l for l in small["lines"] if any(f["item"] == "onion" for f in l["for"]))
ok(onion["pack"]["ordered"] <= 2000 and "10KG" not in onion["product"]["title"].upper(), f"a dinner for 8 does not get the 10 kg onion sack: {onion['product']['title']} for {onion['pack']['need_text']}")
ok(menu.qty(200, "g") == "200 g" and menu.qty(4.2, "g") == "4.2 g" and menu.qty(1500, "g") == "1.5 kg" and menu.qty(84, "ml") == "84 ml" and menu.qty(2900, "ml") == "2.9 l",
   "small amounts are written in grams and millilitres: 200 g, 4.2 g, 84 ml; 1.5 kg and 2.9 l above a kilo")
texts = [t for _, x in every for l in x["lines"] for t in (l["pack"]["need_text"], l["pack"]["ordered_text"], l["pack"]["leftover_text"] or "")] + [x_["amount"] for _, x in every for x_ in x["pantry"]]
ok(texts and not any(t.startswith("0 ") or t.startswith("0.0") for t in texts), "nowhere does an amount read “0 kg”")

# the pantry
pan = {x["item"]: x for x in small["pantry"]}
ordered_items = {f["item"] for l in small["lines"] for f in l["for"]}
ok(len(pan) >= 3 and "salt" in pan and not set(pan) & ordered_items and all(menu.STAPLE.search(i) for i in pan) and all(x["amount"] and x["text"].startswith(x["item"].capitalize() + ": " + x["amount"]) for x in pan.values()),
   f"a dinner for 8: {len(pan)} kitchen staples are left to the pantry with the amount, not ordered ({', '.join(x['item'] + ' ' + x['amount'] for x in list(pan.values())[:4])})")
ok(all(x["smallest"] is None or x["need"] <= x["smallest"] * menu.STAPLE_SHARE + 1e-6 for x in pan.values()), "each is a small part (a third or less) of the smallest pack sold")
ok("salt" not in {x["item"] for x in base["pantry"]} and any(f["item"] == "salt" for l in base["lines"] for f in l["for"]), "for 180 guests salt is a real amount and is ordered")
withp = plan(reading(guests=8, notes=[], order_staples=True))
ok(withp["pantry"] == [] and set(pan) <= {f["item"] for l in withp["lines"] for f in l["for"]} | {o["item"] for o in withp["not_ordered"]} and withp["totals"]["lines"] > small["totals"]["lines"],
   f"one switch orders them too: {withp['totals']['lines']} lines instead of {small['totals']['lines']}, nothing left under pantry")
ok(not any(i in pan for i in ("sugar cubes", "coffee", "tea", "potato", "butter")), "what goes on the table or is the dish itself is never left to the pantry")

# the budget
b = pp["budget"]
ok(b and b["limit"] == 90000 and b["fits"] is True and b["subtotal"] == pp["totals"]["subtotal"] and "under the budget" in b["text"], "a budget on the menu is compared with the order: " + b["text"][:110])
over = plan(party(budget={"amount": 2, "per": "guest"}))["budget"]
ok(over["limit"] == 12000 and over["fits"] is False and "over the budget" in over["text"], "a budget per guest is multiplied by the guests, and an order over it says so: " + over["text"][:110])
twice = menu.normalize(menu.normalize(copy.deepcopy(PARTY), "agent"), "agent")
ok(twice["budget"]["amount"] == 900 and plan(party(budget=None))["budget"] is None, "the budget survives being read twice (still €900), and a menu without one has none")

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
# The kitchen's own recipe with its own amounts stands in place of the card: 9 kg of salmon for 60 portions is 18 kg for 120, nothing added, nothing dropped silently.
own = menu.plan_menu_order(g, {"guests": 120, "dishes": [{"name": "Creamy salmon soup", "recipe_portions": 60, "recipe": [
    {"item": "salmon fillet", "amount": 9, "unit": "kg"}, {"item": "potatoes", "amount": 12, "unit": "kg"}, {"item": "whipping cream", "amount": 6, "unit": "l"},
    {"item": "onions", "amount": 1, "unit": "kg"}, {"item": "eggs", "amount": 10, "unit": "pcs"}, {"item": "dill", "fi": "tilliä"}]}]}, p)
needs = {l["for"][0]["item"]: round(l["pack"]["need"]) for l in own["lines"]}
ok(own["status"] == "ready" and own["dishes"][0]["card"] is None and "own recipe" in own["dishes"][0]["recipe"] and needs == {"salmon fillet": 18000, "potato": 24000, "whipping cream": 12000, "onion": 2000, "egg": 1100},
   f"a kitchen's own recipe replaces the card and its amounts are kept as written, scaled to the guests: {needs}")
ok(next(l for l in own["lines"] if l["for"][0]["item"] == "onion")["pack"]["ordered"] == 2000, "1 kg of onion for 60 portions is 2 kg for 120: dividing and multiplying does not tip it into a third bag")
ok([f["kind"] for f in own["flags"]] == ["left_out"] and "tilliä" in own["flags"][0]["text"] and not any(l["for"][0]["item"] == "dill" for l in own["lines"]),
   "an ingredient with no amount is not guessed: it is left off the order and the plan says so, in the kitchen's own word")
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

# ---- 5b. paprika in a Finnish kitchen is the vegetable: a milk-free, gluten-free wok is not held over paprika crisps
caps = menu.find(g, "paprika")
ok(caps and all(str(g.cat.get(pid)["category"])[:3] == "174" for pid in caps), f"'paprika' finds only fresh capsicum ({len(caps)} products), no crisps and no spice")
ok(all(str(g.cat.get(pid)["category"])[:3] == "226" for pid in menu.find(g, "paprika powder")), "'paprika powder' still finds the spice")
wok = menu.build_plan(g, menu.normalize({"guests": 120, "dishes": [{"name": "Broileri-kasviswokki ja riisi", "notes": ["maidoton", "gluteeniton"], "source": "model",
      "recipe": [{"item": "chicken breast", "amount": 120, "unit": "g"}, {"item": "paprika", "amount": 60, "unit": "g"}, {"item": "rice", "amount": 70, "unit": "g"}]}]}, "model"), p)
ok(wok["status"] == "ready" and not [x for x in wok["not_ordered"] if x["kind"] == "no_fit"] and any(str(l["product"]["id"]) in caps for l in wok["lines"]),
   "a milk-free, gluten-free wok with paprika is ordered, with a fresh capsicum on its line")

# ---- 6. the photo: a non-menu refused, a sample placed as an order over HTTP (calls Gemini)
if MODEL:
    from fastapi.testclient import TestClient

    from gateway.app import app
    web = TestClient(app, base_url="https://testserver")  # https so the visitor's session cookie is kept whatever PUBLIC_URL is
    r = web.post("/api/menu/read", json={"sample": "notmenu"}).json()
    ok(r["status"] == "not_a_menu" and not r.get("reading") and not r.get("plan") and "nothing to cook from" in r["message"] and "Nothing is ordered" in r["message"], "the photo of a closing checklist is refused: " + r["message"][:90])
    ok({c["step"] for c in r["usage"]["calls"]} == {"is it a menu", "read the menu"} and r["usage"]["tokens_in"] == sum(c["in"] for c in r["usage"]["calls"]) > 0,
       "a photo is refused only when both looks find nothing to cook from; the cost is the two calls, from the API usage fields")
    ok(web.post("/api/menu/plan", json={"run": r["id"]}).status_code == 409 and web.post("/api/menu/order", json={"run": r["id"]}).status_code == 409, "and a refused photo cannot be planned or ordered")
    t = time.time()
    r = web.post("/api/menu/read", json={"sample": "wedding"}).json()
    ok(r["status"] == "read" and r["reading"]["guests"] == 180 and len(r["reading"]["dishes"]) >= 7 and any("nut" in x.lower() for x in r["reading"]["notes"]),
       f"the wedding photo is read: {r['reading']['guests']} guests, {len(r['reading']['dishes'])} dishes, notes {r['reading']['notes']}")
    r = web.post("/api/menu/plan", json={"run": r["id"]}).json()
    ok(r["status"] == "ready" and r["plan"]["totals"]["lines"] >= 30 and r["checkout"]["signed"], f"planned into {r['plan']['totals']['lines']} lines with a signed offer")
    ok(not any(nutty(g.cat.get(l["product"]["id"])) for l in r["plan"]["lines"]), "nut-free from the photo: no product with nuts in the basket")
    if r["checkout"]["status"] == "requires_escalation":
        # The menu page cannot approve. In this test a script stands in for the approver: it approves by the approval link, as the approval page would.
        from gateway.app import g as live
        cid = r["checkout"]["id"]
        held = web.post("/api/menu/order", json={"run": r["id"]})
        ok(held.status_code == 409 and held.json()["messages"][0]["code"] == "approval_pending", "the menu page cannot place an order that waits for its approver: " + held.json()["messages"][0]["content"][:80])
        ok(web.get(f"/api/menu/approval/{r['id']}").json()["state"] == "waiting", "the menu page is told the order waits for the approver")
        live.decide(cid, live.store.checkout(cid)["_internal"]["approval_token"], True, "Head chef (test script)", "Approved by the test's stand-in approver.")
        w = web.get(f"/api/menu/approval/{r['id']}").json()
        ok(w["state"] == "approved" and w["approval"]["by"] == "Head chef (test script)" and w["approval"]["asked_by"] == "Kitchen manager, on the menu page",
           f"after the approver says yes the menu page sees it: asked by {w['approval']['asked_by']}, approved by {w['approval']['by']}")
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

    # ---- the same order by hand reads in Finnish first: names for the list, the products and the reasons
    bh = web.get("/api/byhand/task").json()
    ok(bh["list"] and all(x.get("fi") for x in bh["list"]) and all(len(x["dishes_fi"]) == len(x["dishes"]) for x in bh["list"]) and len(bh["notes_fi"]) == len(bh["notes"]),
       f"/byhand: every item on the shopping list has a Finnish name ({bh['list'][0]['fi']}), and dishes and notes come in Finnish too")
    found = web.get("/api/byhand/search", params={"q": "kuohukerma"}).json()["products"]
    ok(found and all(x.get("title_fi") and len(x["al"]) == len(x["contains"]) and len(x["may"]) == len(x["may_contain"]) for x in found),
       f"/byhand: search rows carry the Finnish product name and the allergen codes behind the English words ({found[0]['title_fi']})")
    att = web.post("/api/byhand/start", json={}).json()["attempt"]
    res = web.post("/api/byhand/checkout", json={"attempt": att, "basket": [{"id": found[0]["id"], "units": 1}]}).json()
    ok(res["lines"] == 1 and not res["finished"] and res["needed"] == -(-len(bh["list"]) * 4 // 5) and all("why_code" in x and "hits" in x and (x["orderable"] or x["why_code"]) for x in res["rows"]),
       f"/byhand: the checked basket says in codes why a line would not go through, and how many lines count as finished ({res['needed']})")

    # ---- 7. the model's recipe is checked against the dish name (calls Gemini)
    rd = menu.normalize({"guests": 60, "dishes": [{"name": "Peanut satay chicken skewers"}, {"name": "Lohikeitto"}, {"name": "Almond cake with berries"}], "notes": ["8 guests severe peanut allergy"]}, "agent")
    calls = menu.complete(rd)
    got = {d["name"]: [i["item"] for i in d["recipe"]] for d in rd["dishes"] if not d["card"]}
    left = menu.flags(rd)
    ok(calls and all(d["card"] or d["recipe"] for d in rd["dishes"]) and all(not menu.missing_named(d["name"], d["as_written"], d["recipe"]) or any(f["dish"] == d["name"] for f in left) for d in rd["dishes"] if not d["card"]),
       "every proposed recipe has the ingredient its dish is named for, or the dish is flagged: " + "; ".join(f"{k}: {', '.join(v[:5])}" for k, v in got.items())[:230])
    sat = next(d for d in rd["dishes"] if "satay" in d["name"].lower())
    ok(any("peanut" in i["item"] for i in sat["recipe"]), "the satay keeps its peanuts although 8 guests are allergic: the note is applied in code afterwards, not by the model")

    # ---- 8. messy real menus: six photos of what a kitchen has, a receipt and a price list, through the real path
    folder = pathlib.Path(__file__).parent / "menus"
    expected = {k: v for k, v in json.loads((folder / "expected.json").read_text()).items() if not k.startswith("_")}
    fold = menu.fold

    def find_dish(rd, frag):
        return next((i for i, d in enumerate(rd["dishes"]) if fold(frag) in fold(d["as_written"]) or fold(frag) in fold(d["name"])), None)

    ok(len(expected) == 8 and sum(1 for e in expected.values() if e["menu"]) == 6 and all((folder / f).stat().st_size < 400_000 for f in expected), "tests/menus has six messy menus and two photos with nothing to cook from, each a small JPEG with what must be read next to it")
    for f, e in expected.items():
        image = "data:image/jpeg;base64," + base64.b64encode((folder / f).read_bytes()).decode()
        key = "t-" + secrets.token_hex(6)
        r = web.post("/api/menu/read", json={"image": image, "key": key}).json()
        if not e["menu"]:
            ok(r["status"] == "not_a_menu" and not r.get("reading") and "nothing to cook from" in r["message"], f"{f}: refused, nothing to cook from: " + r["message"][:80])
            continue
        rd = r.get("reading") or {}
        ok(r["status"] == "read" and not r.get("questions"), f"{f} ({e['what'][:60]}) is read as a menu with nothing to ask" + ("" if r["status"] == "read" else ": " + str(r.get("message") or r.get("questions"))[:200]))
        idx = {frag: find_dish(rd, frag) for frag in e["dishes"]}
        ok(rd["guests"] == e["guests"] and None not in idx.values() and len(set(idx.values())) == len(idx) and len(rd["dishes"]) <= e["max_dishes"],
           f"{f}: {rd['guests']} guests and {len(rd['dishes'])} dishes: " + ", ".join(d["name"] for d in rd["dishes"])[:170] + ("" if None not in idx.values() else f" (missing {[k for k, v in idx.items() if v is None]})"))
        ok((rd.get("budget") or {}).get("amount") == (e["budget"] or {}).get("amount") and (rd.get("budget") or {}).get("per") == (e["budget"] or {}).get("per"),
           f"{f}: budget " + ((rd["budget"]["text"] or "") + f" read as €{rd['budget']['amount']:g} {'per guest' if rd['budget']['per'] == 'guest' else 'in total'}" if rd.get("budget") else "none on the page, none read"))
        again = web.post("/api/menu/read", json={"image": image, "key": key}).json()
        ok(again["id"] == r["id"] and again["usage"] == r["usage"], f"{f}: the same request sent again (same key) gets the same reading and no second model call")
        pl = web.post("/api/menu/plan", json={"run": r["id"], "key": key}).json()
        plan_ = pl.get("plan") or {}
        ok(pl["status"] == "ready" and plan_["totals"]["lines"] >= 12 and pl["checkout"]["signed"], f"{f}: planned into {plan_.get('totals', {}).get('lines')} lines, {len(plan_.get('pantry') or [])} from the pantry, {len(plan_.get('not_ordered') or [])} not ordered, one signed offer"
           + ("" if pl["status"] == "ready" else ": " + str(pl.get("questions") or pl.get("messages"))[:200]))
        rules = plan_["diet"]
        notes = sorted([x["kind"], x["scope"], x["count"]] for x in rules if x["scope"] != "dish")
        ok(notes == sorted(e["notes"]) and plan_["requirements"]["exclude_allergens"] == [] and (plan_.get("checkout") or {}).get("exclude_allergens", []) == [],
           f"{f}: notes about some guests stay with those guests ({'; '.join(x['note'] + ' = ' + str(x['count']) + ' of ' + str(rd['guests']) for x in rules if x['scope'] == 'guests') or 'none on the page'}); nothing is excluded on the whole order")
        if e.get("severe"):
            ok(all(x["severe"] for x in rules if x["scope"] == "guests"), f"{f}: “severe” is read from the note")
        if e["marks"]:
            marks = {frag: sorted(x["kind"] for x in rules if x["scope"] == "dish" and x.get("dish") == idx[frag]) for frag in e["marks"]}
            ok(marks == {k: sorted(v) for k, v in e["marks"].items()}, f"{f}: the marks on each dish are rules for that dish only: " + ", ".join(f"{rd['dishes'][idx[k]]['as_written'][:26]} = {'+'.join(v)}" for k, v in marks.items())[:230])
        held = {frag: sum(o["ask"]["portions"] for o in plan_["not_ordered"] if o.get("ask") and o.get("dish_i") == idx[frag] and o.get("named")) for frag in e["held"]}
        ok(held == e["held"], f"{f}: held by its own name for the guests of the note: {held or 'no dish'}" + "".join(". " + o["text"][:150] for o in plan_["not_ordered"] if o.get("named"))[:330])
        missing = [fl_["dish"] for fl_ in plan_["flags"] if fl_["kind"] == "named_missing"]
        got_all = all(not menu.missing_named(d["name"], d["as_written"], d["recipe"]) or d["name"] in missing for d in rd["dishes"] if not d["card"] and d["food"])
        ok(got_all, f"{f}: every dish has the ingredient it is named for" + (f", or says so ({', '.join(missing)})" if missing else ""))
        far = [l["product"]["title"] for l in plan_["lines"] if l["pack"]["ordered"] >= menu.FAR * l["pack"]["need"] and l["pack"]["ordered"] - l["pack"]["need"] >= 1 and not l["partial"] and not (l["pack"]["nothing_smaller"] and l["pack"]["leftover_text"])]
        zero = [t for l in plan_["lines"] for t in (l["pack"]["need_text"], l["pack"]["ordered_text"]) if t.startswith("0 ")] + [x["amount"] for x in plan_["pantry"] if x["amount"].startswith("0 ")]
        ok(not far and not zero and not {x["item"] for x in plan_["pantry"]} & {f_["item"] for l in plan_["lines"] for f_ in l["for"]},
           f"{f}: no pack far bigger than the need unless nothing smaller is sold, no “0 kg”, pantry staples not also ordered" + (f" (far: {far[:3]})" if far else ""))
        if e["budget"]:
            b = plan_["budget"]
            ok(b and b["limit"] == round(e["budget"]["amount"] * 100) * (e["guests"] if e["budget"]["per"] == "guest" else 1) and b["fits"] == (plan_["totals"]["subtotal"] <= b["limit"]), f"{f}: " + b["text"][:150])
        same = web.post("/api/menu/plan", json={"run": r["id"], "key": key}).json()
        ok(same["checkout"]["id"] == pl["checkout"]["id"], f"{f}: the same plan request sent again gets the same offer, not a second one")
    one = web.get("/api/menu/samples").json()
    ok(any(x["id"] == "lounas" for x in one["samples"]) and web.get("/static/samples/lounaslista.jpg").status_code == 200, "the Finnish lunch list is one of the samples on /menu")

print(f"\n{n} checks passed" + ("" if MODEL else " (the parts that call Gemini were skipped)"))
