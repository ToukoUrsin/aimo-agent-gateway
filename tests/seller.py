"""End-to-end check of Valio Aimo's own agent (gateway/seller.py) against the live mock API, without a model.
Run: AIMO_DATA=/tmp/aimo-seller uv run python -m tests.seller   (never against the main database: it resets the demo account)

Covers: five kinds of proposal made as signed offers; a short-dated lot never adds volume the kitchen will not use before
its date (the judge's coffee-milk case becomes a swap for part of the standing delivery, and no offer at all once the usual
order is placed); a lot the allergen rule stops before it leaves; standing-order changes, up and down, go to a person
through the approval link (rule V7) unless the mandate says "alone"; open proposals riding along in a checkout; the
usual basket corrected after a raise and a swap so nothing is ordered twice; a person approving and a person saying no;
a decline; the marked-down price; a second run that repeats nothing declined and keeps to the daily limit; Valio Aimo's
switch; a fill only when the kitchen uses the full amount; the model's sentence check; the summary and the ledger chain.
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


# ---- the judge's case, in code: 50 short-dated coffee milk "that fit your usage" while the standing order already covers it
cands, notes = s.candidates(a, "short_dated")
coffee = next(c for c in cands if c["products"] == ["464237"])
f = coffee["facts"]
ok(f["used_by_then"] == 51 and f["already_coming"] == 120 and f["room_for_extra"] == 0, f"coffee milk: uses {f['used_by_then']} by the date, {f['already_coming']} already coming, no room for extra")
ok(f["how"] == "swap" and coffee["extra_units"] == 0 and f["offered_units"] == 50 and f["standing_delivery"] == 120 and f["regular_after_swap"] == 70,
   "so the lot is offered only as a swap: 50 of the 120 on the standing delivery, nothing extra")
ok(all(c["extra_units"] <= max(0, c["facts"]["used_by_then"] - c["facts"]["already_coming"]) for c in cands), "no short-dated candidate adds more than the room left before the date")
ok(any(n.startswith("No short-dated offer of Alpro Almond drink") and "so more would expire" in n for n in notes), "the almond lot is not offered: the plant drinks already coming cover the use, said in one line")
# with room to spare the lot is offered as extra, but never more than the room
st0 = s.standing(a)
saved_lines = [dict(l) for l in st0["lines"]]
for l in st0["lines"]:
    if l["id"] == "464237":
        l["units"] = 20
g.store.put("seller:standing:" + a["customer"], st0)
extra = next(c for c in s.candidates(a, "short_dated")[0] if c["products"] == ["464237"])
ok(extra["facts"]["how"] == "extra" and extra["extra_units"] == 30 and extra["facts"]["room_for_extra"] == 31, "standing order of 20: 31 units of room, so 30 extra (whole cases) and no more")
# the allergen rule still stops a lot code would offer: plant drinks used far above the standing order, but the kitchen is nut-free
for l in st0["lines"]:
    if l["id"] == "469512":
        l["used"] = l["used"][:5] + [260, 270, 280]
g.store.put("seller:standing:" + a["customer"], st0)
almond = next(c for c in s.candidates(a, "short_dated")[0] if c["products"] == ["461360"])
rules_a, _, _ = s.judge(a, almond, "prj_")
ok(almond["facts"]["how"] == "extra" and any(r["id"] == "A1" and r["outcome"] == "deny" for r in rules_a), "an almond lot with room is still stopped by A1: the standing order is nut-free")
st0["lines"] = saved_lines
g.store.put("seller:standing:" + a["customer"], st0)

# ---- first run: candidates from code, judged under the buyer's mandate
rep = s.run(a, use_model=False)
made = {m["kind"]: m for m in rep["made"]}
ok(set(made) >= {"drift", "short_dated", "cutoff", "shortage", "fill"}, f"five kinds made: {sorted(made)}")
ok(not rep["refused"] and any("Alpro Almond" in n for n in rep["notes"]), "nothing stopped on the first run; the run says in one line why the almond lot was not offered")
rows = {r["id"]: r for r in s._rows(a["customer"])}
ok(all(signed(rows[m["id"]]["doc"]) for m in rep["made"]), "every proposal carries Valio Aimo's signature")
ok(made["drift"]["authority"] == "needs_human_approval" and made["short_dated"]["authority"] == "agent_may_accept" and made["cutoff"]["authority"] == "needs_human_approval",
   "authority judged per proposal (the standing-order change and the whole basket need a person, the swap does not)")
v7 = next(r for r in rows[made["drift"]["id"]]["rules"] if r["id"] == "V7")
ok(v7["outcome"] == "escalate" and "every week" in v7["detail"] and "leaves standing-order changes to a person" in v7["detail"], "V7: a standing-order change is a commitment for every week, so a person decides")
lst = s.list(p)
ok(len(lst["proposals"]) == 5 and signed(lst) and lst.get("next"), "the customer's agent lists 5 open proposals, signed")
drift = next(x for x in lst["proposals"] if x["kind"] == "drift")
ok(drift["basis"]["used_per_week"][-1] > drift["basis"]["standing_units"] and "Seeded" in drift["basis"]["seeded"], "drift shows the usage trend, labelled seeded")
fill0 = next(x for x in lst["proposals"] if x["kind"] == "fill")
ok(fill0["basis"]["average_last_3_weeks"] >= fill0["basis"]["fills_to_units"], "fill only when the kitchen already uses the full amount")

# ---- the opposite drift: a lowering carries no goods, and still goes to a person through the approval link
down = next((x for x in s.candidates(a, "drift")[0] if "Lower" in x["title"]), None)
ok(down and down["lines"] == [] and down["facts"]["used_per_week"][-1] < down["facts"]["standing_units"] and down["facts"]["saved_units_per_week"] > 0,
   "lowering drift carries a falling trend and weekly saving")
floor = g.create_account("sumusaari-spa-hotel", "Sumusaari Spa Hotel", kind="floor", world="floor",
                         history={"463380": {"orders": 30, "typical_units": 48}, "467337": {"orders": 8, "typical_units": 4}})
floor_tok = g.issue_mandate(slug=floor["slug"], agent="agent:sumusaari-spa-hotel/purchasing-1")
floor_p = g.identify(floor_tok)
floor_rep = s.run(floor, kind="drift", use_model=False)
floor_down = next((x for x in floor_rep["made"] if x["kind"] == "drift" and "Lower" in x["title"]), None)
ok(floor_down is not None and floor_down["authority"] == "needs_human_approval", "a floor account gets the seeded lowering proposal, marked as needing a person")
floor_doc = s._get(floor_down["id"])
floor_product = floor_doc["effect"]["standing"]["product"]
floor_before = next(l["units"] for l in s.standing(floor)["lines"] if l["id"] == floor_product)
fc = s.answer(floor_down["id"], "accept", "The kitchen has been crediting the unused cartons.", floor_p)
ok(fc["status"] == "requires_escalation" and fc.get("continue_url", "").split("?")[0].endswith("/approve/" + fc["id"]) and fc["line_items"] == [],
   "accepting the lowering makes a checkout with no goods that waits at the approval link")
ok(next(l["units"] for l in s.standing(floor)["lines"] if l["id"] == floor_product) == floor_before, "nothing changes before the person decides")
ok(s.rows_for([floor["customer"]], links=True)[0]["checkout"].get("approve_url", "").startswith("/approve/"), "the page shows it as waiting for a person, with the approval link")
g.decide(fc["id"], g.store.checkout(fc["id"])["_internal"]["approval_token"], True, "Head chef")
cur = g.get_checkout(fc["id"], floor_p)
floor_done = g.complete_checkout(fc["id"], {"ap2": {"checkout_mandate": g.agent_proof(cur, floor_tok)}}, floor_p)
after_units = next(l["units"] for l in s.standing(floor)["lines"] if l["id"] == floor_product)
ok(floor_done["status"] == "completed" and floor_done["order"]["id"].startswith("soc_") and after_units < floor_before, f"after the approval the standing order changes ({floor_before} to {after_units}); no goods ordered")
change = g.store.get("seller:change:" + floor_done["order"]["id"])
ok(signed(change) and change["decided_by"]["person"] == "Head chef" and change["evidence"]["approval"]["by"] == "Head chef", "a signed standing-order change names who approved it")
fp = s._get(floor_down["id"])
ok(fp["status"] == "accepted" and fp["order"]["approved_by"] == "Head chef" and not fp["alone"], "the proposal is accepted with the approver's name")
fev = [e for e in g.store.events(acct=floor["customer"], limit=500) if e["kind"] == "proposal_settled"]
ok(any("approved by Head chef through the approval link" in e["text"] for e in fev), "the ledger says who approved the standing-order change")

# ---- the knob: a buyer whose mandate lets the agent change standing orders alone
from gateway import policy
alone_m = {**policy.default_mandate(), "proposals": {**policy.PROPOSALS, "standing_changes": "alone"}}
fl2 = g.create_account("sumusaari-alone", "Sumusaari Annex", kind="floor", world="floor", mandate=alone_m,
                       history={"463380": {"orders": 30, "typical_units": 48}, "467337": {"orders": 8, "typical_units": 4}})
st2 = s.standing(fl2)
st2["lines"][0].update(used=[52, 50, 48, 46, 42, 38, 34, 30])  # Arla milk, standing 48, falling
g.store.put("seller:standing:" + fl2["customer"], st2)
r2 = s.run(fl2, kind="drift", use_model=False)
d2 = next((x for x in r2["made"] if "Lower" in x["title"]), None)
ok(d2 and d2["authority"] == "agent_may_accept" and any(r["id"] == "V7" and r["outcome"] == "pass" for r in s._get(d2["id"])["rules"]), "with standing_changes: alone, V7 lets the agent accept alone")
t2 = g.issue_mandate(slug=fl2["slug"], agent="agent:sumusaari-alone/purchasing-1")
p2 = g.identify(t2)
c2 = s.answer(d2["id"], "accept", "Usage has fallen.", p2)
done2 = g.complete_checkout(c2["id"], {"ap2": {"checkout_mandate": g.agent_proof(g.get_checkout(c2["id"], p2), t2)}}, p2)
ok(c2["status"] == "ready_for_complete" and done2["status"] == "completed" and s._get(d2["id"])["alone"], "and the agent finishes it without a person")

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

# ---- the raise: goods for Monday plus a new standing quantity, so a person approves it by link
c = s.answer(made["drift"]["id"], "accept", "Usage has clearly grown.", p, "trc_drift")
ok(c["status"] == "requires_escalation" and rule(c, "V5")["outcome"] == "pass" and rule(c, "V7")["outcome"] == "escalate" and c.get("continue_url"),
   "the raise waits for a person (V7), even though drift proposals may otherwise be accepted alone (V5)")
ok(s.view(s._get(made["drift"]["id"]))["pending"]["next"].startswith("A person must approve"), "the agent is told a person must approve it")
g.decide(c["id"], g.store.checkout(c["id"])["_internal"]["approval_token"], True, "F&B manager")
done = finish(c["id"])
o = g.get_order(done["order"]["id"])
ok(o["wholesale"]["proposal"]["id"] == made["drift"]["id"] and signed(o) and o["wholesale"]["evidence"]["approval"]["by"] == "F&B manager", "the order carries the proposal, both signatures and the approval")
st = s.standing(a)
ok(next(l["units"] for l in st["lines"] if l["id"] == "464201") == 400 and st["changes"][-1]["approved_by"] == "F&B manager", "the standing quantity is 400, recorded with who approved it")
ok(s._get(made["cutoff"]["id"])["status"] == "replaced", "the usual-basket offer made before the raise is withdrawn, so the 80 are not ordered twice")

# ---- the swap: alone, at the marked-down price, and the usual basket shrinks by the same units
c = s.answer(made["short_dated"]["id"], "accept", "Same litres, lower price.", p, "trc_sd")
li = c["line_items"][0]
ok(c["status"] == "ready_for_complete" and li["item"]["price"] < li["item"]["list_price"] and any(m.get("code") == "short_dated" and "order 70 as usual" in m["content"] for m in c["messages"]),
   f"swap priced down ({li['item']['price']} < {li['item']['list_price']} cents) and the checkout says to order 70 as usual")
finish(c["id"])
ok(s.standing(a)["swaps"][-1]["units"] == 50, "the swap is recorded on the standing order for that day")
rep_cut = s.run(a, kind="cutoff", use_model=False)
cut = s._get(rep_cut["made"][0]["id"]) if rep_cut["made"] else None
units = {x["item"]["id"]: x["quantity"] for x in cut["lines"]} if cut else {}
ok(cut and units.get("464237") == 70 and units.get("464201") == 320, f"the corrected basket holds 70 coffee milk and 320 milk (400 less the 80 already ordered): {units}")
again = s.candidates(a, "short_dated")[0]
ok(not any(x["products"] == ["464237"] for x in again), "with the swap taken, no second coffee-milk offer")

# ---- a person approves by link: the corrected usual basket before the cut-off
c = s.answer(cut["id"], "accept", "This is our usual Monday order.", p, "trc_cut")
ok(c["status"] == "requires_escalation" and rule(c, "V5")["outcome"] == "escalate" and c.get("continue_url"), "usual basket goes to a person (V5) with an approval link")
k = g.store.checkout(c["id"])["_internal"]["approval_token"]
g.decide(c["id"], k, True, "F&B manager")
done = finish(c["id"])
ok(done["status"] == "completed", "approved by the person, then completed by the agent")
cands_after, notes_after = s.candidates(a, "short_dated")
ok(not any(x["products"] == ["464237"] and x["extra_units"] for x in cands_after), "once the usual order is placed, coffee milk gets no extra short-dated offer")

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
fk = [x["kind"] for x in fresh_rep["made"]]
ok(len(fk) == 3 and len(set(fk)) == 3 and {"drift", "short_dated"} <= set(fk) and not any(x["rule"] == "V2" for x in fresh_rep["refused"]),
   f"a fresh sandbox starts with three different proposals, a drift and a short-dated swap among them, without hitting V2: {fk}")
sd = s._get(next(x["id"] for x in fresh_rep["made"] if x["kind"] == "short_dated"))
ok(sd["facts"]["how"] == "swap" and sd["extra_ex_vat"] == 0, "the sandbox's short-dated offer is a swap too, nothing extra")
fill_c = next((c for c in s.candidates(a, "fill")[0]), None)
ok(fill_c is None or (fill_c["facts"]["case_size"] == 10 and fill_c["facts"]["full_after"] == "10 full cases"), "fill uses the product's 10-unit case")
fill = s._get(made["fill"]["id"])
ok(not fill["doc"]["line_items"][0]["item"].get("list_price"), "fill shows no ordinary list price when there is no quantity break")
sb = s.standing(fresh)
oat = next(l for l in sb["lines"] if l["id"] == "473609")
ok(not any(c["kind"] == "fill" for c in s.candidates(fresh, "fill")[0]) and any(n.startswith("No fill for") for n in s.candidates(fresh, "fill")[1]) if sum(oat["used"][-3:]) / 3 < 100 else True,
   "a kitchen that uses about 96 gets no fill to 100, and the run says why")

# ---- summary and ledger
sm = s.summary([a["customer"]])["total"]
print("summary:", {k: sm[k] for k in ("made", "accepted_alone", "sent_to_person", "approved_by_person", "declined", "refused_before_leaving", "euros_added_ex_vat", "extra_ex_vat", "short_dated_saved")})
ok(sm["made"] == 6 and sm["accepted_alone"] == 1 and sm["approved_by_person"] == 2 and sm["declined"] == 2 and sm["sent_to_person"] == 3, "summary counts")
ok(sm["refused_before_leaving"] >= 3 and sm["refused_by_rule"].get("V3") and sm["euros_added_ex_vat"] > 0 and sm["short_dated_saved"]["l"] > 0, "summary: refused by rule, euros added, litres saved")
ok(g.store.verify_chain()[0], "ledger chain intact")
kinds = {e["kind"] for e in g.store.events(acct=a["customer"], limit=2000)}
ok({"proposal", "refused", "proposal_answer", "proposal_settled"} <= kinds, "proposals, refusals, answers and settlements are on the ledger")
print(f"\n{n} checks passed")
