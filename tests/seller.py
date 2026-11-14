"""End-to-end check of Valio Aimo's own agent (gateway/seller.py) against the live mock API, without a model.
Run: AIMO_DATA=/tmp/aimo-seller uv run python -m tests.seller   (never against the main database: it resets the demo account)

Covers: five kinds of proposal made as signed offers, one stopped by the allergen rule before it leaves, open proposals
riding along in a checkout, an agent accepting alone inside its mandate (the standing quantity changes), a person
approving by link, a person saying no, a decline, the marked-down price, a second run that repeats nothing declined and
keeps to the daily limit, Valio Aimo's switch, the model's sentence check, the summary and the ledger chain.
"""
from gateway.config import STAGE
from gateway.service import Gateway, GatewayError
from gateway.signing import verify_detached
import time

g = Gateway()
g.reset()
g.store.q("delete from kv where k like 'seller:lots:%'")  # fresh seeded lots
s, a = g.seller, g.account(STAGE)
tok = g.issue_mandate(agent=f"agent:{STAGE}/purchasing-1", name="Purchasing agent")
p = g.identify(tok)
n = 0


def ok(cond, what):
    global n
    n += 1
    print(("ok   " if cond else "FAIL ") + what)
    assert cond, what


def signed(doc):
    return verify_detached({k: v for k, v in doc.items() if k != "proof"}, doc["proof"]["jws"], g.key.jwk)


def finish(cid):
    cur = g.get_checkout(cid, p)
    return g.complete_checkout(cid, {"ap2": {"checkout_mandate": g.agent_proof(cur, tok)}}, p)


def rule(c, rid):
    return next((r for r in c["wholesale"]["policy"]["rules"] if r["id"] == rid), None)


def refused(fn, code):
    try:
        fn()
    except GatewayError as e:
        return e.code == code
    return False


# ---- first run: candidates from code, judged under the buyer's mandate
rep = s.run(a, use_model=False)
made = {m["kind"]: m for m in rep["made"]}
ok(set(made) >= {"drift", "short_dated", "cutoff", "shortage", "fill"}, f"five kinds made: {sorted(made)}")
ok(any(r["kind"] == "short_dated" and r["rule"] == "A1" for r in rep["refused"]), "almond drink stopped by A1 before it left (the standing order is nut-free)")
rows = {r["id"]: r for r in s._rows(a["customer"])}
ok(all(signed(rows[m["id"]]["doc"]) for m in rep["made"]), "every proposal carries Valio Aimo's signature")
ok(made["drift"]["authority"] == "agent_may_accept" and made["cutoff"]["authority"] == "needs_human_approval", "authority judged per proposal (drift alone, the whole basket needs a person)")
lst = s.list(p)
ok(len(lst["proposals"]) == 5 and signed(lst) and lst.get("next"), "the customer's agent lists 5 open proposals, signed")
drift = next(x for x in lst["proposals"] if x["kind"] == "drift")
ok(drift["basis"]["used_per_week"][-1] > drift["basis"]["standing_units"] and "Seeded" in drift["basis"]["seeded"], "drift shows the usage trend, labelled seeded")

# ---- the opposite drift is evidence-led and changes the standing quantity without ordering goods
down = next((x for x in s.candidates(a, "drift")[0] if "Lower" in x["title"]), None)
ok(down and down["lines"] == [] and down["facts"]["used_per_week"][-1] < down["facts"]["standing_units"] and down["facts"]["saved_units_per_week"] > 0,
   "lowering drift carries a falling trend and weekly saving")
floor = g.create_account("sumusaari-spa-hotel", "Sumusaari Spa Hotel", kind="floor", world="floor",
                         history={"463380": {"orders": 30, "typical_units": 48}, "467337": {"orders": 8, "typical_units": 4}})
floor_tok = g.issue_mandate(slug=floor["slug"], agent="agent:sumusaari-spa-hotel/purchasing-1")
floor_p = g.identify(floor_tok)
floor_rep = s.run(floor, kind="drift", use_model=False)
floor_down = next((x for x in floor_rep["made"] if x["kind"] == "drift" and "Lower" in x["title"]), None)
ok(floor_down is not None, "a floor account gets the seeded lowering proposal")
floor_doc = s._get(floor_down["id"])
floor_product = floor_doc["effect"]["standing"]["product"]
floor_before = next(l["units"] for l in s.standing(floor)["lines"] if l["id"] == floor_product)
floor_done = s.answer(floor_down["id"], "accept", "The kitchen has been crediting the unused cartons.", floor_p)
ok(floor_done["status"] == "completed" and floor_done["standing"]["to"] < floor_before, "accepting lowering drift changes the floor standing quantity")

# ---- recipe role filtering replaces the old plain substitute-only explanation
role_subs, role_use = s._shortage_substitutes("469346", s._next_day(), p, 8)
ok(role_use and role_use["dish"] and role_use["role"] and role_subs, "shortage reads a recipe-card dish and role")
from gateway.menu import Planner
role_planner = Planner(g, p)
ok(all(role_planner.role_fit(role_use["role"], x["id"])[0] for x in role_subs), "every role-aware substitute fits the dish role")

# ---- open proposals ride along in a checkout answer
c0 = g.create_checkout({"line_items": [{"item": {"id": "463380"}, "quantity": 12}], "wholesale": {"delivery_date": rows[made["drift"]["id"]]["day"]}}, p, "trc_ride")
ok(len(c0["wholesale"].get("open_proposals", {}).get("items", [])) == 5, "open proposals ride along in a checkout")
g.cancel_checkout(c0["id"], p)

# ---- accept alone: drift
c = s.answer(made["drift"]["id"], "accept", "Usage has clearly grown.", p, "trc_drift")
ok(c["status"] == "ready_for_complete" and rule(c, "V5")["outcome"] == "pass" and rule(c, "V6")["outcome"] == "pass", "drift accepted inside the mandate (V5, V6 pass)")
done = finish(c["id"])
o = g.get_order(done["order"]["id"])
ok(o["wholesale"]["proposal"]["id"] == made["drift"]["id"] and signed(o), "the order carries the proposal and both signatures")
st = s.standing(a)
ok(next(l["units"] for l in st["lines"] if l["id"] == "464201") == 400, "accepting drift changed the standing quantity to 400")

# ---- accept alone: short-dated at the marked-down price
c = s.answer(made["short_dated"]["id"], "accept", "We use coffee milk daily.", p, "trc_sd")
li = c["line_items"][0]
ok(c["status"] == "ready_for_complete" and li["item"]["price"] < li["item"]["list_price"], f"short-dated priced down: {li['item']['price']} < {li['item']['list_price']} cents")
finish(c["id"])

# ---- a person approves by link: the usual basket before the cut-off
c = s.answer(made["cutoff"]["id"], "accept", "This is our usual Monday order.", p, "trc_cut")
ok(c["status"] == "requires_escalation" and rule(c, "V5")["outcome"] == "escalate" and c.get("continue_url"), "usual basket goes to a person (V5) with an approval link")
k = g.store.checkout(c["id"])["_internal"]["approval_token"]
g.decide(c["id"], k, True, "F&B manager")
done = finish(c["id"])
ok(done["status"] == "completed", "approved by the person, then completed by the agent")

# ---- a person says no: shortage replacement
c = s.answer(made["shortage"]["id"], "accept", "We need juice for the buffet.", p, "trc_short")
ok(c["status"] == "requires_escalation", "replacement for the short juice needs a person")
g.decide(c["id"], g.store.checkout(c["id"])["_internal"]["approval_token"], False, "F&B manager", "Not that brand.")

# ---- the agent declines
r = s.answer(made["fill"]["id"], "decline", "Four extra cartons are not needed.", p, "trc_fill")
ok(r["proposal"]["status"] == "declined", "fill declined with a reason")
ok(refused(lambda: s.answer(made["fill"]["id"], "accept", "", p), "proposal_closed"), "a declined proposal cannot be accepted afterwards")

# ---- second run: nothing declined comes back, and the daily limit holds
rep2 = s.run(a, use_model=False)
ok(not rep2["made"], "second run made nothing new")
v3 = [r for r in rep2["refused"] if r["rule"] == "V3"]
ok({r["kind"] for r in v3} >= {"fill", "shortage"}, f"V3 stopped the declined ones: {sorted(r['kind'] for r in v3)}")
st = g.store.get("seller:standing:" + a["customer"])
for l in st["lines"]:
    if l["id"] == "463380":  # a second drift appears
        l["used"] = [300, 301, 299, 305, 320, 338, 352, 371]
g.store.put("seller:standing:" + a["customer"], st)
rep3 = s.run(a, kind="drift", use_model=False)
ok(not rep3["made"] and any(r["rule"] == "V2" for r in rep3["refused"]), "a new candidate over the daily limit is stopped by V2")

# ---- Valio Aimo's switch
pr = g.store.get("profile:" + a["customer"])
g.store.put("profile:" + a["customer"], {**pr, "proposals": False})
ok("switched proposals off" in " ".join(s.run(a, use_model=False)["notes"]), "switch off: no proposals made")
g.store.put("profile:" + a["customer"], {**pr, "proposals": True})

# ---- the model's sentence may not bring numbers of its own
cand = {"facts": {"standing_units": 320, "corrected_units": 400}, "template": "x", "title": "y"}
ok(s._unsupported("Raise it from 320 to 400.", cand) == "" and s._unsupported("Raise it to 450.", cand), "a number not in the facts is caught")
priced = {"facts": {}, "template": "x", "title": "y", "items": [{"item": {"price": 137, "list_price": 183}}]}
ok(s._unsupported("The unit price is €1.37.", priced) == "" and s._unsupported("The unit price is €1.41.", priced),
   "the model-number guard includes candidate line prices and rejects a number outside them")

# A new visitor sandbox gets one of each useful proposal kind and stays below
# the daily cap on its first run.
fresh = g.create_account("seller-fresh-" + str(int(time.time())), "Fresh sandbox", kind="sandbox")
fresh_rep = s.run(fresh, use_model=False)
ok({x["kind"] for x in fresh_rep["made"]} == {"drift", "short_dated", "fill"} and not any(x["rule"] == "V2" for x in fresh_rep["refused"]),
   "a fresh sandbox starts with one drift, one short-dated proposal and one fill without hitting V2")
fill = s._get(next(x["id"] for x in fresh_rep["made"] if x["kind"] == "fill"))
ok(fill["facts"]["case_size"] == 10 and fill["facts"]["full_after"] == "10 full cases" and not fill["doc"]["line_items"][0]["item"].get("list_price"),
   "fill uses the product's 10-unit case and shows no ordinary list price when there is no quantity break")

# ---- summary and ledger
sm = s.summary([a["customer"]])["total"]
print("summary:", {k: sm[k] for k in ("made", "accepted_alone", "sent_to_person", "approved_by_person", "declined", "refused_before_leaving", "euros_added_ex_vat", "extra_ex_vat", "short_dated_saved")})
ok(sm["made"] == 5 and sm["accepted_alone"] == 2 and sm["approved_by_person"] == 1 and sm["declined"] == 2 and sm["sent_to_person"] == 2, "summary counts")
ok(sm["refused_before_leaving"] >= 4 and sm["refused_by_rule"].get("A1") and sm["euros_added_ex_vat"] > 0 and sm["short_dated_saved"]["l"] > 0, "summary: refused by rule, euros added, litres saved")
ok(g.store.verify_chain()[0], "ledger chain intact")
kinds = {e["kind"] for e in g.store.events(acct=a["customer"], limit=2000)}
ok({"proposal", "refused", "proposal_answer", "proposal_settled"} <= kinds, "proposals, refusals, answers and settlements are on the ledger")
print(f"\n{n} checks passed")
