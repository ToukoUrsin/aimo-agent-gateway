"""The words of a request against the range: does "milk" get milk, do "600 eggs" get 600 eggs, and what happens on the way in
when a date is wrong, a connection drops or Valio Aimo's API is busy. Against the live mock API for prices and stock.
Run: AIMO_DATA=/tmp/aimo-need uv run -q python -m tests.need   (never against the main database: it resets the demo account)
"""
import httpx
from fastapi.testclient import TestClient

from gateway import aimo
from gateway.app import app, g
from gateway.catalog import kind, pieces
from gateway.service import GatewayError

g.reset()
D = "2026-11-16"
tok = g.issue_mandate()
p = g.identify(tok)
n = 0


def ok(cond, what):
    global n
    n += 1
    print(("ok   " if cond else "FAIL ") + what)
    assert cond, what


def plan(query, qty, unit, **more):
    return g.plan({"query": query, "need": {"quantity": qty, "unit": unit}, "delivery_date": D, **more}, p)


def kinds(r):
    return {kind(g.cat.get(l["item"]["id"])) for o in r["options"] for l in o["line_items"]}


def codes(r):
    return {m.get("code") for m in r.get("messages") or []}


print("What each product is")
ok(g.cat.kinds_named == len(g.cat.docs) and len(g.cat.docs) > 17000, f"every one of the {len(g.cat.docs):,} products has a kind, written once by a model from its name")
ok(kind(g.cat.get("464201")) == "milk" and kind(g.cat.get("463335")) == "milk", "lactose-free milk sold as 'milk drink' is milk")
ok(kind(g.cat.get("463380")) == "coffee milk", "coffee milk is its own kind, not milk")

print("\nA request for milk gets milk")
r = plan("milk", 400, "l")
ok(kinds(r) == {"milk"}, "every option for 'milk' is milk")
ok({"coffee milk", "sour milk"} <= {k["kind"] for k in r.get("other_kinds", [])}, "coffee milk and sour milk are named as other kinds, not offered")
r = plan("lactose-free milk", 1000, "l")
by = {o["label"]: o for o in r["options"]}
ok(by["usual_product"]["line_items"][0]["item"]["id"] == "464201", "'lactose-free milk' offers this kitchen's usual milk first")
ok(kinds(r) == {"milk"} and all("FREE_FROM:LACTOSE" in g.cat.get(l["item"]["id"])["claims"] for o in r["options"] for l in o["line_items"]), "the cheapest option is also lactose-free milk, not the cheapest thing with milk in its name")
ok(kinds(plan("coffee milk lactose free", 20, "l")) == {"coffee milk"}, "'coffee milk' gets coffee milk")
ok(kinds(plan("usual milk", 400, "l", filters={"claims": ["lactose_free"]})) == {"milk"}, "filler words ('usual') do not change what is asked for")
ok(kinds(plan("laktoositon maito", 400, "l")) == {"milk"}, "the same request in Finnish gets the same kind")

print("\nBeyond milk")
r = plan("eggs", 600, "pcs")
ok(kinds(r) == {"egg"}, "'eggs' gets eggs, not egg white powder or chocolate eggs")
ok(all(600 <= o["covers"] < 800 for o in r["options"]) and all(l["units"] * pieces(g.cat.get(l["item"]["id"])) == l["covers"] for o in r["options"] for l in o["line_items"]),
   "600 eggs are 600 eggs: boxes are counted by the eggs in them (" + ", ".join(f"{l['units']:g} x {pieces(g.cat.get(l['item']['id']))}" for o in r["options"] for l in o["line_items"]) + ")")
ok(kinds(plan("minced beef", 30, "kg")) == {"minced beef"} and kinds(plan("ground beef", 30, "kg")) == {"minced beef"}, "'minced beef' and 'ground beef' get minced beef")
ok(kinds(plan("oat milk", 60, "l")) == {"oat drink"}, "'oat milk' gets oat drink, not milk")
a, b = g.cat.pick("milk chocolate", {})[3]["kinds"], g.cat.pick("chocolate milk", {})[3]["kinds"]
ok(a == ["milk chocolate"] and b == ["chocolate milk"], "'milk chocolate' and 'chocolate milk' are told apart by word order")
r = plan("Valio butter", 10, "kg")
ok(kinds(r) == {"butter"} and all("valio" in l["item"]["title"].lower() for o in r["options"] for l in o["line_items"]), "a brand in the request is kept: every option for 'Valio butter' is Valio butter")
ok(kinds(plan("potatoes", 100, "kg")) == {"potato"}, "'potatoes' gets potatoes, not crisps or mash")

r = plan("tomatoes", 15, "kg")
ok(kinds(r) == {"tomato"} and not any(w in l["item"]["title"].lower() for o in r["options"] for l in o["line_items"] for w in ("diced", "dried", "paste")),
   "'tomatoes' gets tomatoes: diced, canned and sun-dried are other kinds (every kind was read a second time as a whole)")
r = plan("coffee", 10, "kg")
ok(r["options"] and all(10 <= o["covers"] <= 13 for o in r["options"]), "10 kg of coffee is 10 kg: a case the product data counts as 360 kg is read from its name, 60 x 100 g")
r = plan("rye bread", 50, "pcs")
ok(next(o for o in r["options"] if o["label"] == "lowest_price")["total_ex_vat"] == min(o["total_ex_vat"] for o in r["options"]), "lowest price is the lowest bill for the need, not the lowest price per piece of a case twice too big")

print("\nWhen the words do not say which")
r = plan("chicken", 40, "kg")
ok(not r["options"] and "which_kind" in codes(r) and {"chicken fillet", "chicken thigh"} <= {k["kind"] for k in r["kinds_to_choose"]}, "'chicken' offers nothing and asks which kind: fillet, strips, thighs")
r = plan("pasta", 25, "kg")
ok(not r["options"] and {"spaghetti", "penne"} <= {k["kind"] for k in r["kinds_to_choose"]}, "'pasta' asks which: spaghetti, penne, fusilli")
fillet = next(o for o in plan("chicken fillet", 40, "kg")["options"])["line_items"][0]["item"]["id"]
hist = g.store.get("history:" + p["customer"], {})
g.store.put("history:" + p["customer"], {**hist, fillet: {**next(iter(hist.values())), "orders": 6, "typical_units": 40}})
r = plan("chicken", 40, "kg")
ok(kinds(r) == {"chicken fillet"} and any("has ordered chicken fillet" in m["content"] for m in r["messages"]), "once the account has ordered chicken fillet, 'chicken' means that, and the answer says so")
g.store.put("history:" + p["customer"], hist)
r = plan("goat milk", 10, "l")
ok(not r["options"] and "no_option" in codes(r), "'goat milk' is not in the range: nothing is offered, cow's milk least of all")
r = plan("almonds", 5, "kg", filters={"exclude_allergens": ["tree nuts"]})
ok(not r["options"] and any("none of them meets the requirements" in m["content"] for m in r["messages"]), "almonds for a kitchen that excludes tree nuts: nothing offered, and the answer says why")

print("\nWhen the request leaves the rest out")
u = g.usual("milk", p)
ok(u["usual"][0]["id"] == "464201" and u["usual"][0]["usual_amount"] == {"quantity": 400, "unit": "L"}, "'some milk': the account's usual milk and its usual 400 L come back, so the agent can propose one order to confirm")
ok({r["kind"] for r in u["usual"]} == {"milk"} and "coffee milk" in {r["kind"] for r in u.get("other_kinds_ordered", [])}, "coffee milk this account also orders is listed apart, not mixed in")
u = g.usual("chicken", p)
ok(not u["usual"] and "no_history" in codes(u), "nothing in the history for 'chicken': the answer says so, and the agent has to ask")

print("\nSubstitutes")
subs = g.substitutes("464201", D, p, limit=12)
ok(subs and all(kind(g.cat.get(s["id"])) == "milk" or s["listed_by_valio_aimo"] for s in subs), f"substitutes for the usual milk are milk ({len(subs)} offered), never coffee milk from the same shelf")

print("\nDates")
try:
    g.plan({"query": "milk", "need": {"quantity": 10, "unit": "l"}, "delivery_date": "next monday-ish"}, p)
    bad = None
except GatewayError as e:
    bad = e
ok(bad is not None and bad.status == 400 and bad.code == "invalid_delivery_date", "a date that cannot be read is refused, not replaced by a guess")
c = g.create_checkout({"line_items": [{"item": {"id": "464201"}, "quantity": 10}], "wholesale": {"delivery_date": "2026-11-01"}}, p)
ok(c["status"] == "incomplete" and "delivery_date_passed" in codes(c), "a date in the past is refused as a date in the past")
r = g.lookup(["464201"], g._asked_day({"delivery_date": D}), p)
ok(r["wholesale"]["delivery_date"] == D, "a delivery date given at the top of a request is used")

print("\nOn the way in")
web = TestClient(app)
head = {"Authorization": f"Bearer {tok}"}
r = web.post("/checkout-sessions", json={"line_items": [{"item": {"id": "no-such-product"}, "quantity": 5}], "wholesale": {"delivery_date": D}}, headers=head)
ok(r.status_code == 422 and r.json()["status"] == "incomplete" and "product_not_found" in codes(r.json()), "a checkout with no line that can be ordered answers 422, not 201 Created")
body = {"line_items": [{"item": {"id": "464201"}, "quantity": 10}], "wholesale": {"delivery_date": D}}
one = web.post("/checkout-sessions", json=body, headers={**head, "Idempotency-Key": "need-test-1"})
two = web.post("/checkout-sessions", json=body, headers={**head, "Idempotency-Key": "need-test-1"})
new = web.post("/checkout-sessions", json=body, headers={**head, "Idempotency-Key": "need-test-2"})
ok(one.status_code == 201 and one.json()["id"] == two.json()["id"], "the same request sent twice with one Idempotency-Key is done once: a dropped connection cannot order twice")
ok(new.json()["id"] != one.json()["id"], "a new key is a new request")


class Busy:
    """Valio Aimo's API answering 429 twice, then normally."""
    def __init__(self, real, fails):
        self.real, self.fails, self.seen = real, fails, 0

    def post(self, *a, **k):
        self.seen += 1
        if self.seen <= self.fails:
            return httpx.Response(429, json={"error": "rate limited"})
        return self.real.post(*a, **k)


real, sleep = g.aimo.http, aimo.time.sleep
aimo.time.sleep = lambda s: None
try:
    g.aimo.http = Busy(real, 2)
    g.aimo.cache.clear()
    r = plan("lactose-free milk", 100, "l")
    ok(bool(r["options"]) and g.aimo.http.seen >= 3, "Valio Aimo's API rate-limits twice: the gateway waits and the request still gets its answer")
    g.aimo.http = Busy(real, 99)
    g.aimo.cache.clear()
    try:
        plan("lactose-free milk", 100, "l")
        err = None
    except GatewayError as e:
        err = e
    ok(err is not None and err.status == 503 and err.code == "upstream_rate_limited", "still rate-limited after four tries: a plain retryable error, never a made-up price")
finally:
    g.aimo.http, aimo.time.sleep = real, sleep

print(f"\nALL {n} CHECKS PASS")
