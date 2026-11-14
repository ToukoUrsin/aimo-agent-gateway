"""Buyer agent 2: a standing-order bot with no language model, speaking plain REST.

Same gateway, same documents, same guardrails as the Claude agent. It shows the interface
does not care what kind of agent is on the other end.
"""
from .common import BuyerSide, describe, errors, eur, next_monday

STANDING_ORDER = {"query": "lactose-free milk", "need": {"quantity": 400, "unit": "L"}}


async def run(run: dict, base: str):
    side = BuyerSide(base, run)
    await side.setup()
    day = next_monday()
    intent = run["request"] or f"Standing order: {STANDING_ORDER['need']['quantity']} L of our usual lactose-free milk every Monday."

    side.step("say", "A bot with no AI", f"This bot does not read the request. It sends the same standing order every time: {STANDING_ORDER['need']['quantity']} L of the usual lactose-free milk for the next Monday.")
    side.step("call", "POST /sourcing/plan", "", {"arguments": {**STANDING_ORDER, "delivery_date": day}})
    plan = await side.post("/sourcing/plan", {**STANDING_ORDER, "delivery_date": day})
    if side.verified(plan) is not True:
        return side.step("error", "Stopped", "Valio Aimo's signature on the plan did not verify.")
    side.step("result", "Options", describe(plan) + " Seller signature verified.", {"response": {k: v for k, v in plan.items() if k not in ("ucp", "proof")}})
    opt = next((o for o in plan["options"] if o["label"] == "usual_product"), None) or (plan["options"][0] if plan["options"] else None)
    if not opt:
        return side.step("done", "Nothing to order", "No option meets the standing order today.")
    side.step("say", "Rule", f"Fixed rule: take the usual product. Chose '{opt['label']}' at {eur(opt['total_ex_vat'])}.")

    body = {"line_items": [{k: l[k] for k in ("item", "quantity", "wholesale") if k in l} for l in opt["line_items"]], "context": {"intent": intent},
            "wholesale": {"delivery_date": day, "requirements": plan["requirements"], "purchase_order": f"NHH-STANDING-{day}"}}
    side.step("call", "POST /checkout-sessions", "", {"arguments": body})
    c = await side.post("/checkout-sessions", body)
    if side.verified(c) is not True:
        return side.step("error", "Stopped", "Valio Aimo's signature on the offer did not verify.")
    run["checkout_id"] = c["id"]
    side.step("result", "Signed offer", describe(c) + " Seller signature verified.", {"response": {k: v for k, v in c.items() if k not in ("ucp", "ap2")}})

    if c["status"] == "requires_escalation":
        run["approval_url"] = c["continue_url"]
        run["final"] = "Outside the bot's authority. It left the order with a person: " + " ".join(errors(c))
        return side.step("done", "Handed to a person", run["final"], {"continue_url": c["continue_url"]})
    if c["status"] != "ready_for_complete":
        run["final"] = "Could not order: " + " ".join(errors(c))
        return side.step("done", "Stopped", run["final"])

    side.step("check", "Signed the offer", f"Signed digest {c['ap2']['checkout_digest'][:16]}… with the agent key the mandate is bound to.")
    done = await side.post(f"/checkout-sessions/{c['id']}/complete", {"ap2": {"checkout_mandate": side.accept(c)}})
    side.step("result", "POST /checkout-sessions/{id}/complete", describe(done), {"response": {k: v for k, v in done.items() if k not in ("ucp", "ap2")}})
    if done.get("order"):
        run["order"] = done["order"]
        l = done["line_items"][0]
        run["final"] = f"Ordered {l['wholesale']['units']:g} x {l['item']['title']} for {day}, {eur(opt['total_ex_vat'])} ex VAT. Order {done['order']['label']}. No person was involved."
    else:
        run["final"] = "Order was not placed: " + " ".join(errors(done))
    side.step("done", "Bot's report", run["final"])
    await side.close()
