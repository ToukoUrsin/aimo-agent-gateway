"""End-to-end check of what happens after the order: a short pick, a delay and a recall, against the live mock API.
Run: AIMO_DATA=/tmp/aimo-wpc uv run python -m tests.after   (never against the main database: it resets the demo account)

Covers: issues as signed notices, ways out judged by the rule engine, an agent settling alone, a person approving and
a person rejecting, linked orders and signed amendments, the original confirmation staying byte-for-byte the same,
a recall that reaches two accounts and blocks the product, and the ledger entries with their account tags.
"""
import json

from gateway.service import Gateway, GatewayError
from gateway.signing import digest, jwt_verify, verify_detached

g = Gateway()
g.reset()
a = g.after
D, MILK = "2026-11-16", "464201"  # Valio semi skimmed milk 1 l lactose free UHT
B = "after-test-bistro"
if not g.account(B):
    g.create_account(B, "Harbour Bistro (test)", kind="floor", customer="900777")  # kind floor: shares the stage's stock, so one recall reaches both
g.reset(B)
ta, tb = g.issue_mandate(), g.issue_mandate(B, {**g.store.get("mandate_config:900777"), "substitution": {"allow": "any", "max_unit_price_increase_pct": 10}})
pa, pb = g.identify(ta), g.identify(tb)
n = 0


def ok(cond, what):
    global n
    n += 1
    print(("ok   " if cond else "FAIL ") + what)
    assert cond, what


def signed(doc):
    return verify_detached({k: v for k, v in doc.items() if k != "proof"}, doc["proof"]["jws"], g.key.jwk)


def offer_ok(c):
    return verify_detached({k: v for k, v in c.items() if k != "ap2"}, c["ap2"]["merchant_authorization"], g.key.jwk)


def refused(fn, code):
    try:
        fn()
    except GatewayError as e:
        return e.code == code
    return False


def place(p, tok, units, trace, intent):
    c = g.create_checkout({"line_items": [{"item": {"id": MILK}, "quantity": units}], "context": {"intent": intent}, "wholesale": {"delivery_date": D, "requirements": {"claims": ["lactose_free"]}}}, p, trace)
    assert c["status"] == "ready_for_complete", c["messages"]
    r = g.complete_checkout(c["id"], {"ap2": {"checkout_mandate": g.agent_proof(c, tok)}}, p)
    return g.get_order(r["order"]["id"])


def finish(c, p, tok):
    cur = g.get_checkout(c["id"], p)
    return g.complete_checkout(c["id"], {"ap2": {"checkout_mandate": g.agent_proof(cur, tok)}}, p)


def way(issue, p, option):
    v = a.view(a._get(issue["id"]), p)
    return next((w for w in v.get("ways_out", []) if w["option"] == option), None)


def kinds(o):
    return [e["kind"] for e in g.audit(o["id"])["events"]]


# ------------------------------------------------------------------ 1. short pick: the best way out needs a person
print("\n1. Short pick: 280 of 400")
o1 = place(pa, ta, 400, "after-1", "400 litres of lactose-free milk for Monday")
frozen = json.dumps(o1, sort_keys=True)
ok(o1["line_items"][0]["wholesale"]["lot"] == f"L{MILK}-261116-01", "the order line got a lot when the order was placed")
i1 = a.raise_issue(o1, "short_pick")  # default: 70% of the first line
ok(i1["facts"]["deliverable"] == 280 and i1["facts"]["missing"] == 120, i1["what_happened"])
ok(signed(i1["notice"]) and i1["notice"]["order"]["confirmation_digest"] == digest({k: v for k, v in o1.items() if k != "proof"}), "the notice is signed by Valio Aimo and names the exact confirmation it is about")
opts = {w["option"]: w for w in i1["notice"]["ways_out"]}
subs = [k for k in opts if k.startswith("substitute_")]
ok(opts["accept_partial"]["authority"]["decision"] == "agent_may_resolve" and opts["accept_partial"]["credit_ex_vat"] == 120 * 156, "taking 280 with 120 credited is inside the mandate (P1: 70% or more)")
ok(opts["drop_line"]["authority"]["decision"] == "needs_human_approval", "dropping the line needs a person (P1)")
ok(subs and all(opts[k]["authority"]["decision"] == "needs_human_approval" for k in subs), f"{len(subs)} substitutes offered, each needs a person (S1: not on Valio Aimo's list)")
ok(all("FREE_FROM:LACTOSE" in g.cat.get(opts[k]["substitute"]["id"])["claims"] for k in subs), "every substitute offered is lactose free, as the order required")
again = lambda p: g.create_checkout({"line_items": [{"item": {"id": MILK}, "quantity": 120}], "wholesale": {"delivery_date": D}}, p, "after-1x")
c = again(pa)
ok(c["status"] == "incomplete" and c["messages"][0]["code"] == "short_at_warehouse" and again(pb)["status"] != "incomplete", "while the short pick is open, this account cannot simply order the missing goods again")
g.cancel_checkout(next(x["id"] for x in g.store.checkouts(5, pb["customer"])), pb)
la = a.list(pa, o1["label"])
ok(signed(la) and len(la["issues"]) == 1 and la["issues"][0]["order"]["request"] == "400 litres of lactose-free milk for Monday", "the agent's list is signed and shows the issue with the buyer's original request")
ok(not a.list(pb)["issues"] and refused(lambda: a.resolve(i1["id"], "accept_partial", "", pb), "forbidden"), "another account neither sees nor can answer it")
ok(refused(lambda: a.resolve(i1["id"], "keep_everything", "", pa), "unknown_option"), "an option that is not on the list is refused")
c = a.resolve(i1["id"], subs[0], "The kitchen needs the full 400 litres; this is the closest lactose-free milk.", pa, "after-1r")
rules = {r["id"]: r["outcome"] for r in c["wholesale"]["policy"]["rules"]}
ok(c["status"] == "requires_escalation" and "/approve/" in c["continue_url"] and offer_ok(c), "the substitute comes back as a signed offer that a person must approve")
ok(rules.get("B0") == "pass" and rules.get("S1") == "escalate" and rules.get("A2") == "pass", "same rule engine: B0 money on the difference, S1 substitution, A2 the order's lactose-free requirement")
ok(refused(lambda: g.update_checkout(c["id"], {"line_items": [{"item": {"id": MILK}, "quantity": 1}], "wholesale": {"delivery_date": D}}, pa), "invalid_state"), "the offer cannot be edited into something else")
ok(finish(c, pa, ta)["status"] == "requires_escalation", "the agent cannot complete it before the person approves")
ok(a.list(pa, o1["id"])["issues"][0]["pending"]["checkout_status"] == "requires_escalation", "the issue shows as waiting for a person")
g.decide(c["id"], c["continue_url"].split("k=")[1], True, "Maija (F&B manager)", "fine")
r = finish(c, pa, ta)
ok(r["status"] == "completed" and r["order"]["id"] != o1["id"], f"after approval the agent completes it: linked order {r['order']['label']}")
lo = g.get_order(r["order"]["id"])
ok(signed(lo) and lo["wholesale"]["amends"]["order_id"] == o1["id"] and lo["line_items"][0]["wholesale"]["lot"], "the linked order is a signed order of its own that points at the original")
am = a._get(i1["id"])["amendment"]
ok(signed(am) and am["order"]["confirmation_digest"] == digest({k: v for k, v in o1.items() if k != "proof"}) and am["linked_order"]["id"] == lo["id"], "signed amendment: names the original confirmation by digest and the linked order")
ok(am["decided_by"] == {"agent": pa["agent"], "mandate_id": pa["jti"], "alone": False, "person": "Maija (F&B manager)", "rationale": "The kitchen needs the full 400 litres; this is the closest lactose-free milk."}, "the amendment says who decided: the agent with Maija's approval")
ok(am["credit"]["ex_vat"] == 18720 and am["changes"][0]["to"] == 280, "the amendment credits the 120 missing and sets the line to 280")
acc = jwt_verify(am["evidence"]["agent_acceptance"], pa["agent_jwk"])
ok(bool(acc) and acc["checkout_digest"] == am["evidence"]["offer_digest"], "the agent signed the amendment's terms with the key named in its mandate")
ok(json.dumps(g.get_order(o1["id"]), sort_keys=True) == frozen and signed(g.get_order(o1["id"])), "the original confirmation is byte for byte what it was, and still verifies")
st = a.state(o1)
ok(st["status"] == "amended" and st["lines"][0]["units"] == 280 and st["credit_ex_vat"] == 18720 and st["linked_orders"][0]["id"] == lo["id"], "the order as it stands: 280 delivered, credit, linked order")
ok(g.store.q("select total from orders where id=?", (o1["id"],))[0][0] == 400 * 156 - 18720, "the credit comes off what counts against the weekly limit")
au = g.audit(o1["id"])
row = au["after"]["issues"][0]
ok(row["status"] == "resolved" and signed(row["notice"]) and signed(row["amendment"]) and row["linked_order"]["id"] == lo["id"] and digest(row["offer"]) == row["amendment"]["evidence"]["offer_digest"], "the audit carries notice, amendment, accepted offer and linked order")
ok({"order", "issue", "resolve", "policy", "approved", "amendment"} <= set(kinds(o1)), "the order's audit trail includes the after-order events")
ok(g.audit(lo["id"])["after"]["amends"]["order_id"] == o1["id"], "the linked order's audit points back")
ok(refused(lambda: a.resolve(i1["id"], "accept_partial", "", pa), "issue_settled"), "a settled issue cannot be answered again")
ok(g.lookup([MILK], D, pa)["products"][0]["wholesale"]["orderable"], "once it is settled the product can be ordered as usual")

# ------------------------------------------------------------------ 2. delay: one day alone, three days not
print("\n2. Delay")
o2 = place(pa, ta, 100, "after-2", "100 litres of lactose-free milk")
i2 = a.raise_issue(o2, "delay", days=1, reason="Truck fault at the Vantaa hub")
opts = {w["option"]: w["authority"]["decision"] for w in i2["notice"]["ways_out"]}
ok(opts == {"accept_new_date": "agent_may_resolve", "cancel_order": "needs_human_approval"}, "one day late: the agent may accept alone (D1), cancelling needs a person (P1)")
lk = g.lookup([MILK], D, pa)["products"][0]["wholesale"]
ok(lk["orderable"] is False and lk["reason"] == "delivery_delayed", "while the delay is open, the same product cannot be ordered for the original day on this account")
c = a.resolve(i2["id"], "accept_new_date", "One day is fine: the milk is UHT and the event is on Wednesday.", pa, "after-2r")
ok(c["status"] == "ready_for_complete" and not c["line_items"] and c["totals"][-1]["amount"] == 0 and offer_ok(c), "a way out with no new goods is a signed offer with no lines and no money")
bad = g.complete_checkout(c["id"], {}, pa)
ok(bad["status"] != "completed" and bad["messages"][-1]["code"] == "agent_missing_key", "it still needs the agent's signature")
r = finish(c, pa, ta)
am = a._get(i2["id"])["amendment"]
ok(r["status"] == "completed" and r["order"]["id"] == o2["id"] and signed(am) and am["decided_by"]["alone"] and am["linked_order"] is None, "completed by the agent alone: a signed amendment, no new order")
ok(am["changes"] == [{"type": "delivery_date", "from": D, "to": "2026-11-17"}] and a.state(o2)["delivery_date"] == "2026-11-17" and g.get_order(o2["id"])["wholesale"]["delivery_date"] == D, "the amendment moves the day; the confirmation still says the day it confirmed")

o3 = place(pa, ta, 100, "after-3", "100 litres of lactose-free milk")
i3 = a.raise_issue(o3, "delay", days=3)
ok(way(i3, pa, "accept_new_date")["authority"]["decision"] == "needs_human_approval", "three days late is outside the mandate (D1)")
c = a.resolve(i3["id"], "accept_new_date", "Later is better than nothing.", pa, "after-3r")
g.decide(c["id"], c["continue_url"].split("k=")[1], False, "Maija (F&B manager)", "Too late for the event, cancel it")
v = a.list(pa, o3["id"])["issues"][0]
ok(v["status"] == "open" and v["earlier_attempts"][0]["outcome"] == "rejected by Maija (F&B manager)", "the person said no: the issue is open again and says so")
c = a.resolve(i3["id"], "cancel_order", "Maija rejected the late delivery.", pa, "after-3r")
ok(c["status"] == "requires_escalation", "cancelling needs a person too")
g.decide(c["id"], c["continue_url"].split("k=")[1], True, "Maija (F&B manager)")
r = finish(c, pa, ta)
st = a.state(o3)
ok(r["status"] == "completed" and st["status"] == "cancelled" and st["credit_ex_vat"] == 100 * 156 and g.store.q("select total from orders where id=?", (o3["id"],))[0][0] == 0, "cancelled with a signed amendment, credited in full")
ok(refused(lambda: a.raise_issue(o3, "short_pick"), "invalid_state"), "nothing more can be raised on a cancelled order")

# ------------------------------------------------------------------ 3. recall: two accounts, product blocked
print("\n3. Recall")
ob = place(pb, tb, 200, "after-b", "200 litres of lactose-free milk")
o4 = place(pa, ta, 100, "after-4", "100 litres of lactose-free milk")
rc, made = a.raise_recall(o4, reason="Possible fault in the carton seal.")
want = {o1["id"]: 280, o2["id"]: 100, o4["id"]: 100, ob["id"]: 200}  # o3 is cancelled, and the 120 that were never delivered are not in it
ok({x["order_id"]: x["units"] for x in rc["affected"]} == want, f"trace from lot {rc['lot']}: 4 orders, only the units still on each")
ok({x["customer"] for x in rc["affected"]} == {pa["customer"], pb["customer"]} and len(made) == 4 and all(signed(i["notice"]) for i in made) and signed(rc["notice"]), "both customers were told at once, each with a signed notice")
ok(all(not w["option"].startswith("keep") and w["authority"]["decision"] != "not_allowed" for i in made for w in i["notice"]["ways_out"]) and all(any(w["option"] == "credit" for w in i["notice"]["ways_out"]) for i in made), "ways out are replace or credit, never keep")
lk = g.lookup([MILK], D, pa)["products"][0]["wholesale"]
ok(lk["orderable"] is False and lk["reason"] == "product_recalled", "the product shows as not orderable: product_recalled")
c = g.create_checkout({"line_items": [{"item": {"id": MILK}, "quantity": 10}], "wholesale": {"delivery_date": D}}, pb, "after-b2")
ok(c["status"] == "incomplete" and c["messages"][0]["code"] == "product_recalled", "a new checkout with it is refused: " + c["messages"][0]["content"][:90])
mine = {i["order_id"]: i for i in made}
ok(refused(lambda: a.resolve(mine[o4["id"]]["id"], "keep", "It looks fine to us.", pa, "after-4r"), "policy_denied") and any(e["kind"] == "refused" and "R1" in e["text"] for e in g.store.events(ref="after-4r")), "an agent that tries to keep the lot is refused by rule R1, on the record")
r4 = next(x for x in g.audit(o4["id"])["after"]["issues"] if x["kind"] == "recall")
ok(r4["status"] == "open" and r4["refused"][0]["option"] == "keep" and r4["refused"][0]["rule"] == "R1" and "refused" in kinds(o4), "the refusal shows on the order's own audit trail, and the issue stays open")
for o in (o4, o2, o1):
    c = a.resolve(mine[o["id"]]["id"], "credit", "Recalled lot: take the credit.", pa, "after-4r")
    r1 = next(x for x in c["wholesale"]["policy"]["rules"] if x["id"] == "R1")
    assert c["status"] == "ready_for_complete" and r1["outcome"] == "pass", c["messages"]
    finish(c, pa, ta)
ok(all(a._get(mine[o["id"]]["id"])["amendment"]["decided_by"]["alone"] for o in (o4, o2, o1)), "account A's agent took the credit on its three orders alone")
ok(a.state(o1)["lines"][0]["status"] == "recalled" and a.state(o1)["credit_ex_vat"] == 18720 + 280 * 156 and len(a.state(o1)["amendments"]) == 2, "an order can carry two amendments: the short pick, then the recall")
ib = a.list(pb)["issues"][0]
rep = next(w for w in ib["ways_out"] if w["option"].startswith("replace_"))
ok(rep["authority"]["decision"] == "agent_may_resolve", "account B's mandate allows any substitute, so its agent may replace alone")
c = a.resolve(ib["id"], rep["option"], "Same kind of milk, lactose free, same day.", pb, "after-br")
r = finish(c, pb, tb)
ok(r["status"] == "completed" and g.get_order(r["order"]["id"])["wholesale"]["customer"] == pb["customer"] and a.state(ob)["lines"][0]["units"] == 0, f"B's agent replaced the lot: linked order {r['order']['label']}")
v = a.recall_view(rc["id"])
ok(v["blocked"] and v["summary"] == {"orders": 4, "customers": 2, "units": 680, "settled": 4, "open": 0} and sorted(x["state"] for x in v["affected"]) == ["credited", "credited", "credited", "replaced"], "the recall view shows every order, what was done, and that all four are settled")
ok(g.lookup([MILK], D, pa)["products"][0]["wholesale"]["orderable"] is False, "still blocked until Valio Aimo closes the recall")
a.close_recall(rc["id"])
o5 = place(pa, ta, 50, "after-5", "50 litres of lactose-free milk")
ok(o5["line_items"][0]["wholesale"]["lot"] == f"L{MILK}-261116-02", "after closing, the product can be ordered again and new orders get the next batch")

# ------------------------------------------------------------------ the record
print("\nThe record")
chain = g.store.verify_chain()
ok(chain[0], f"the ledger chain is intact over {chain[1]} entries")
kb = {e["kind"] for e in g.store.events(acct=pb["customer"], limit=400)}
ok({"issue", "resolve", "amendment", "order"} <= kb, "B's after-order events carry B's account tag")
ok(all(json.dumps(g.get_order(o["id"]), sort_keys=True) == json.dumps(o, sort_keys=True) and signed(g.get_order(o["id"])) for o in (o1, o2, o3, o4, ob)), "every original confirmation is unchanged and still verifies")
print(f"\nALL {n} CHECKS PASS")
