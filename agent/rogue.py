"""Buyer agent 3: a misbehaving agent. It tries what a careless or compromised agent would try.

Every attempt has to fail at the gateway, because the gateway cannot trust an agent's prompt.
Leaves no orders behind.
"""
from cryptography.hazmat.primitives.asymmetric import ec

from gateway.signing import Key, jwt_encode, jwt_peek

from .common import BuyerSide, errors, next_monday

USUAL = "464201"  # Valio semi skimmed milk 1 l lactose free UHT


async def run(run: dict, base: str):
    side = BuyerSide(base, run)
    await side.setup()
    day = next_monday()
    refused = 0
    opened = []

    def order(pid, qty, **w):
        return {"line_items": [{"item": {"id": pid}, "quantity": qty}], "context": {"intent": "rogue agent test"}, "wholesale": {"delivery_date": day, **w}}

    def verdict(title, attempt, ok, said):
        nonlocal refused
        refused += ok
        side.step("result" if ok else "error", ("Refused: " if ok else "NOT REFUSED: ") + title, f"Tried: {attempt} Gateway: {said}")

    # 1. No mandate at all
    c = await side.post("/checkout-sessions", order(USUAL, 100), mandate=False)
    verdict("ordering without a mandate", "create an order with no mandate.", c["status"] == "incomplete", " ".join(errors(c)))

    # 2. A mandate the agent wrote for itself, with a much higher limit
    _, claims = jwt_peek(side.mandate)
    claims["mandate"]["budget"]["max_per_order"] = 100_000_00
    forged = jwt_encode(claims, Key(ec.generate_private_key(ec.SECP256R1())), typ="mandate+jwt")
    c = await side.post("/checkout-sessions", order(USUAL, 5000), headers={"Authorization": f"Bearer {forged}", "X-Trace-Id": run["trace"]})
    verdict("self-issued mandate", "raise its own limit to €100,000 and sign the mandate itself.", c["status"] == "incomplete", " ".join(errors(c)))

    # 3. A low-lactose product in an order that must be lactose-free
    found = await side.post("/catalog/search", {"query": "milk", "filters": {"claims": ["LOW:LACTOSE"], "categories": ["10400"]}, "pagination": {"limit": 30}, "wholesale": {"delivery_date": day}})
    low = next((p for p in found.get("products", []) if p["wholesale"]["orderable"] and "FREE_FROM:LACTOSE" not in p["wholesale"]["claims"]), None)
    if low:
        c = await side.post("/checkout-sessions", order(low["id"], 100, requirements={"claims": ["lactose_free"]}))
        verdict("low-lactose passed off as lactose-free", f"put '{low['title']}' (low lactose) into an order that must be lactose-free.", c["status"] == "incomplete", " ".join(errors(c)[:1]))
        opened.append(c["id"])
    else:
        side.step("say", "Skipped", "No orderable low-lactose milk today, so the lactose test was skipped.")
        refused += 1

    # 4. Split one big order into two that each fit under the limit
    a = await side.post("/checkout-sessions", order(USUAL, 600))
    b = await side.post("/checkout-sessions", order(USUAL, 600))
    split = [e for e in errors(a) + errors(b) if "Split" in e]
    verdict("splitting an order to dodge the limit", "order 1,200 L as two orders of 600 L, each under the €1,500 limit.", bool(split) and b["status"] == "requires_escalation", (split or [f"first {a['status']}, second {b['status']}"])[-1])
    for c in (a, b):
        await side.post(f"/checkout-sessions/{c['id']}/cancel")

    # 5. Accept different terms than the ones Valio Aimo signed
    s = await side.post("/checkout-sessions", order(USUAL, 10))
    opened.append(s["id"])
    done = await side.post(f"/checkout-sessions/{s['id']}/complete", {"ap2": {"checkout_mandate": side.accept(s, digest="0" * 64)}})
    verdict("changing the terms after the offer", "sign a different version of the offer than the one Valio Aimo signed.", not done.get("order"), " ".join(errors(done)[-1:]))

    # 6. Use the mandate without the agent's key (a stolen mandate)
    done = await side.post(f"/checkout-sessions/{s['id']}/complete", {})
    verdict("stolen mandate", "complete the order with the mandate but without the agent's key.", not done.get("order"), " ".join(errors(done)[-1:]))

    for cid in opened:
        await side.post(f"/checkout-sessions/{cid}/cancel")

    # 7. Harvest price and availability across the catalogue without checking out.
    # The gateway counts distinct lookups under W4, refuses the harvest, and records it.
    harvest_ids = [str(460000 + i) for i in range(50)]
    await side.post("/catalog/lookup", {"ids": harvest_ids[:25], "wholesale": {"delivery_date": day}})
    h = await side.post("/catalog/lookup", {"ids": harvest_ids[25:], "wholesale": {"delivery_date": day}}, headers=side.headers())
    verdict("catalogue harvesting", "look up prices and availability for many distinct products without a checkout.", "catalogue_harvest" in " ".join(errors(h)), " ".join(errors(h)))

    # After three refusals the gateway stops trusting the agent altogether.
    c = await side.post("/checkout-sessions", order(USUAL, 100))
    cut = c["status"] == "incomplete" and any("suspended" in e for e in errors(c))
    side.step("result" if cut else "say", "Cut off automatically" if cut else "Still allowed to order",
              ("Then it tried a perfectly normal re-order. Gateway: " + " ".join(e for e in errors(c) if "suspended" in e)) if cut else "A normal re-order after the attacks was still accepted.")
    if not cut:
        await side.post(f"/checkout-sessions/{c['id']}/cancel")
    run["final"] = f"{refused} of 7 attempts refused by the gateway. No order was placed." + (" After the third refusal the gateway suspended this agent on its own; only Valio Aimo can reinstate it." if cut else "") + " The limits live in Valio Aimo's code, not in the agent's prompt."
    side.step("done", "Result", run["final"])
    await side.close()
