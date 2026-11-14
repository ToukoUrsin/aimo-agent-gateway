"""Controls checks for the external-agent boundary.

Run with ``uv run -q python -m tests.controls``.  The fake Aimo client keeps
this suite deterministic and does not spend calls against the challenge API.
"""
import os
import tempfile
import time

from gateway.aimo import Aimo, AimoError
from gateway.config import STAGE
from gateway.service import Gateway, GatewayError
from gateway.store import Store


def fake_quote(ids, day, customer):
    return {i: {"avail": {"logisticModel": "Stock", "scheduleLines": [{"availableAmount": 698, "availableDate": day, "closingTime": "2099-01-01T12:00:00+02:00"}]},
                 "price": {"catalogPrice": {"value": 2.0}, "comparisonPrice": {"value": 2.0}, "comparisonUnit": "L", "tax": {"taxRate": 13.5}, "netPriceFound": True}, "time_now": "2026-11-14T10:00:00+02:00"} for i in ids}


def walk(x):
    if isinstance(x, dict):
        for k, v in x.items():
            yield k, v
            yield from walk(v)
    elif isinstance(x, list):
        for v in x:
            yield from walk(v)


def main():
    g = Gateway(Store(tempfile.mktemp()))
    g.aimo.quote = fake_quote
    token = g.issue_mandate()
    p = g.identify(token)

    public = g.search({"query": "milk", "pagination": {"limit": 3}}, None)
    assert all("price" not in k and k not in ("stock", "orderable", "reason") for k, _ in walk(public)), "public catalogue leaked commercial data"
    private = g.search({"query": "milk", "pagination": {"limit": 3}, "wholesale": {"need": {"quantity": 1000, "unit": "L"}}}, p)
    assert any(k == "covers_quantity" for k, _ in walk(private))
    assert all(not (k in ("stock", "available") and isinstance(v, (int, float)) and not isinstance(v, bool)) for k, v in walk(private)), "agent response exposed inventory counts"

    # Upstream 429 serves an expired cached value and marks it stale; no cache is a clean 503.
    a = Aimo()
    a.cache[("x", "2026-11-16", "c")] = (0, fake_quote(["x"], "2026-11-16", "c")["x"])
    a._post = lambda *args: (_ for _ in ()).throw(AimoError(code="upstream_rate_limited"))
    assert a.quote(["x"], "2026-11-16", "c")["x"]["stale"]
    try:
        a.quote(["y"], "2026-11-16", "c")
    except AimoError as e:
        assert e.status == 503
    else:
        raise AssertionError("uncached upstream failure was not a clean 503")

    old = {k: os.environ.get(k) for k in ("AIMO_AGENT_RATE", "AIMO_ACCOUNT_RATE", "AIMO_AGENT_BURST", "AIMO_ACCOUNT_BURST")}
    os.environ.update(AIMO_AGENT_RATE="2", AIMO_ACCOUNT_RATE="2", AIMO_AGENT_BURST="1", AIMO_ACCOUNT_BURST="1")
    try:
        g.rate_limit(p)
        try:
            g.rate_limit(p)
        except GatewayError as e:
            assert e.status == 429 and e.code == "rate_limited" and e.retry_after
        else:
            raise AssertionError("bucket did not return 429")
        time.sleep(0.6)
        g.rate_limit(p)  # the bucket refills after retry_after
    finally:
        for k, v in old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    # Each line with no allergen declaration is cleared on its own: the approval names exactly the product and
    # allergen pairs it clears, and the gateway refuses a list that is one line short or one line long.
    from gateway.catalog import allergens_unchecked, declares_allergens, named_allergens
    und = [d for d in g.cat.docs if allergens_unchecked({"AP"}, d) and not named_allergens(["AP"], d)][:3]
    dec = next(d for d in g.cat.docs if declares_allergens(d) and "AP" not in (d.get("contains") or []) + (d.get("may_contain") or []))
    basket = {"line_items": [{"item": {"id": d["id"]}, "quantity": 1} for d in und + [dec]], "wholesale": {"delivery_date": "2026-11-16", "requirements": {"exclude_allergens": ["peanut"]}}}

    def ask():
        c = g.create_checkout(basket, p, "clears")
        return c, c["continue_url"].split("k=")[1]

    def refused(c, k, clears, why):
        try:
            g.decide(c["id"], k, True, "Maija (F&B manager)", "", clears=clears)
        except GatewayError as e:
            assert e.code == "clears_mismatch" and e.status == 400, why
            assert g.get_checkout(c["id"], p)["status"] == "requires_escalation", why + ": the order must still be waiting"
        else:
            raise AssertionError(why)

    c, k = ask()
    want = g.clears_of(g.store.checkout(c["id"]))
    assert c["status"] == "requires_escalation" and [u["id"] for u in want] == sorted(d["id"] for d in und) and all(u["codes"] == ["AP"] for u in want), "the three undeclared lines are the ones to clear"
    ticked = [{"id": u["id"], "codes": u["codes"]} for u in want]
    refused(c, k, [], "an approval with no line ticked was accepted")
    refused(c, k, ticked[:2], "an approval one line short was accepted")
    refused(c, k, ticked + [{"id": dec["id"], "codes": ["AP"]}], "an approval that clears a line nobody asked about was accepted")
    refused(c, k, [{"id": u["id"], "codes": ["AP", "AN"]} for u in want], "an approval that clears an allergen nobody asked about was accepted")
    refused(c, k, [{"id": u["id"], "codes": []} for u in want], "an approval that names the lines but no allergen was accepted")
    assert not g.store.get("allergen_cleared:" + p["customer"]), "a refused approval must not clear anything"
    done = g.decide(c["id"], k, True, "Maija (F&B manager)", "", clears=list(reversed(ticked)))  # the order of the ticks does not matter
    ap = done["wholesale"]["approval"]
    assert done["status"] == "ready_for_complete" and [(u["id"], u["codes"]) for u in ap["clears"]] == [(u["id"], u["codes"]) for u in want] and ap["clears_confirmed"] == "each line ticked by the approver"
    assert ap["asked_by"] and ap["asked_by"] != ap["by"], "the record names who asked apart from who approved"
    from gateway.signing import verify_detached
    assert verify_detached({x: v for x, v in ap.items() if x != "signature"}, ap["signature"]["jws"], g.keys(g.account_of(p["customer"])["slug"])["approver"].jwk), "the signed approval covers the list of cleared lines"
    seen = g.store.get("allergen_cleared:" + p["customer"])
    assert set(seen) == {u["id"] for u in want} and all(set(v) == {"AP"} and v["AP"]["by"] == "Maija (F&B manager)" for v in seen.values()), "the account remembers exactly the cleared pairs"
    again, _ = ask()
    a1 = [r for r in again["wholesale"]["policy"]["rules"] if r["id"] == "A1"]
    assert len(a1) == 1 and a1[0]["outcome"] == "pass" and not g.clears_of(g.store.checkout(again["id"])), "a cleared product is not asked about twice"
    g.cancel_checkout(again["id"], p)
    # A plain link approval through the API with no list (the test scripts and the floor's approver scripts) still works,
    # and its record says the lines were cleared all at once.
    g.store.put("allergen_cleared:" + p["customer"], {})
    c, k = ask()
    ap = g.decide(c["id"], k, True, "Approver script", "")["wholesale"]["approval"]
    assert len(ap["clears"]) == 3 and ap["clears_confirmed"] == "all at once, by link through the API" and "device" not in ap
    # Rejecting needs no ticks, and clears nothing.
    g.store.put("allergen_cleared:" + p["customer"], {})
    c, k = ask()
    no = g.decide(c["id"], k, False, "Maija (F&B manager)", "not sure about the mushrooms")
    assert no["status"] == "canceled" and "clears" not in no["wholesale"]["approval"] and not g.store.get("allergen_cleared:" + p["customer"])

    # Valio Aimo's ceiling is Valio Aimo's to lift: over it the order is refused, there is no approval link to get past it,
    # and it counts what is already placed for the day, so two halves do not get through where the whole would not.
    g.store.put("allergen_cleared:" + p["customer"], {})
    token2 = g.issue_mandate(agent="agent:stage/purchasing-2")  # its own agent, so the first one's record below stays without orders
    p2 = g.identify(token2)
    day, pk = "2026-11-16", "profile:" + p["customer"]
    base = g.store.get(pk)
    sub = lambda c: next(t["amount"] for t in c["totals"] if t["type"] == "subtotal")
    rule = lambda c, rid: next((r["outcome"] for r in c["wholesale"]["policy"]["rules"] if r["id"] == rid), None)

    def order(units):
        return g.create_checkout({"line_items": [{"item": {"id": "464201"}, "quantity": units}], "wholesale": {"delivery_date": day}}, p2, "ceiling")

    def place(c, by="Maija (F&B manager)"):
        if c["status"] == "requires_escalation":
            c = g.decide(c["id"], c["continue_url"].split("k=")[1], True, by, "")
        done = g.complete_checkout(c["id"], {"ap2": {"checkout_mandate": g.agent_proof(c, token2)}}, p2)
        assert done["status"] == "completed", done["messages"]
        return done

    c = order(100)
    whole = sub(c)
    g.cancel_checkout(c["id"], p2)
    before = g._day_placed(p["customer"], day)
    g.store.put(pk, {**base, "day_ceiling": before + whole - 1})
    c = order(100)
    assert c["status"] == "incomplete" and rule(c, "W5") == "deny" and "continue_url" not in c, "an order over Valio Aimo's ceiling was not refused outright"
    try:
        g.decide(c["id"], g.store.checkout(c["id"])["_internal"]["approval_token"], True, "Maija (F&B manager)", "")
        raise AssertionError("the buyer's approver approved past Valio Aimo's ceiling")
    except GatewayError as e:
        assert e.code == "invalid_state"
    place(order(50))
    assert g._day_placed(p["customer"], day) == before + whole // 2
    c = order(50)
    assert c["status"] == "incomplete" and rule(c, "W5") == "deny", "the second half of a split order got past the ceiling the whole order did not"
    g.store.put(pk, {**base, "day_ceiling": before + whole})
    c = order(50)
    assert rule(c, "W5") is None and c["status"] != "incomplete", "inside the ceiling the order goes ahead"
    g.cancel_checkout(c["id"], p2)

    # The rules are run again when the approver says yes. A cap lowered after the offer was signed is not approved past.
    g.store.put(pk, {**base, "max_order_total": 100, "day_ceiling": 100_000_000})
    c = order(60)
    assert c["status"] == "requires_escalation" and rule(c, "W3") == "escalate"
    k = c["continue_url"].split("k=")[1]
    g.store.put(pk, {**base, "max_order_total": 100, "day_ceiling": 1})  # Valio Aimo lowers its ceiling while the approval is pending
    try:
        g.decide(c["id"], k, True, "Maija (F&B manager)", "")
        raise AssertionError("an offer was approved after Valio Aimo's ceiling was lowered under it")
    except GatewayError as e:
        assert e.code == "offer_changed" and e.status == 409
    now = g.get_checkout(c["id"], p2)
    assert now["status"] == "incomplete" and rule(now, "W5") == "deny" and not now["wholesale"].get("approval"), "the stale offer was not replaced by the refusal"
    # Unchanged rules: the yes goes through, and it needs a name. The server never makes one up.
    g.store.put(pk, {**base, "max_order_total": 100, "day_ceiling": 100_000_000})
    c = order(60)
    k = c["continue_url"].split("k=")[1]
    for nobody in ("", "   ", None):
        try:
            g.decide(c["id"], k, True, nobody, "")
            raise AssertionError("an approval with no name was accepted")
        except GatewayError as e:
            assert e.code == "approver_required" and e.status == 400
    assert g.get_checkout(c["id"], p2)["status"] == "requires_escalation"
    ok_ = g.decide(c["id"], k, True, "Maija (F&B manager)", "")
    assert ok_["status"] == "ready_for_complete" and ok_["wholesale"]["approval"]["by"] == "Maija (F&B manager)"
    g.cancel_checkout(c["id"], p2)
    g.store.put(pk, base)

    # The buyer's own signing key: the gateway holds the public half only, and a mandate signed with it is read strictly.
    from cryptography.hazmat.primitives.asymmetric import ec
    from gateway.signing import Key, jwt_encode, jwt_verify
    own, bot, stranger = (Key(ec.generate_private_key(ec.SECP256R1())) for _ in range(3))
    acct = g.account(STAGE)
    try:
        g.register_buyer_key(STAGE, {**own.jwk, "d": "AAAA"})
        raise AssertionError("a JWK with a private part was stored")
    except GatewayError as e:
        assert e.code == "bad_key" and not g.buyer_keys(acct["iss"])
    for junk in (None, "key", {"kty": "RSA"}, {"kty": "EC", "crv": "P-256", "x": "AA", "y": "AA"}):
        try:
            g.register_buyer_key(STAGE, junk)
            raise AssertionError("a key that is not a P-256 public key was stored")
        except GatewayError as e:
            assert e.code == "bad_key"
    reg = g.register_buyer_key(STAGE, own.jwk)
    assert reg["kid"] == own.jwk["kid"] and reg["issuer"] == acct["iss"] and reg["agent_prefix"] == f"agent:{STAGE}/"

    def signed(key, **change):
        t = int(time.time())
        claims = {"iss": reg["issuer"], "sub": f"agent:{STAGE}/own-key", "aud": reg["audience"], "iat": t, "exp": t + 600, "jti": "mnd_own" + os.urandom(4).hex(), "cnf": {"jwk": bot.jwk},
                  "mandate": {"budget": {"max_per_order": 40000}}, **change}
        return jwt_encode({k: v for k, v in claims.items() if v is not None}, key, typ="mandate+jwt")

    mine = signed(own)
    po = g.identify(mine)
    assert po["ok"] and po["mandate_key"] == {"kid": own.jwk["kid"], "held_by": "buyer"} and po["mandate"]["budget"] == {"currency": "EUR", "max_per_order": 40000, "max_per_week": 600000}
    assert po["mandate"]["new_products"] == "ask" and po["agent_jwk"]["kid"] == bot.jwk["kid"], "fields the buyer left out take the defaults"
    assert g.identify(token)["mandate_key"]["held_by"] == "gateway", "a demo mandate is marked as signed with the key the gateway keeps"
    for what, bad in (("another key", signed(stranger)), ("another company's agent", signed(own, sub="agent:other/purchasing-1")), ("no agent", signed(own, sub=None)),
                      ("a limit that is not a number", signed(own, mandate={"budget": {"max_per_order": "lots"}})), ("a negative limit", signed(own, mandate={"budget": {"max_per_order": -1}})),
                      ("a constraint block that is a list", signed(own, mandate=[1])), ("no id", signed(own, jti=None)), ("an agent key with a private part", signed(own, cnf={"jwk": {**bot.jwk, "d": "AAAA"}})),
                      ("another seller", signed(own, aud="https://elsewhere.example/.well-known/ucp")), ("an expired one", signed(own, exp=int(time.time()) - 5))):
        r = g.identify(bad)
        assert r and not r["ok"], f"a mandate with {what} was accepted"
    c = g.create_checkout({"line_items": [{"item": {"id": "464201"}, "quantity": 100}], "wholesale": {"delivery_date": day}}, po, "ownkey")
    assert c["status"] == "ready_for_complete", c["messages"]
    accept = jwt_encode({"iss": po["agent"], "checkout_id": c["id"], "checkout_digest": c["ap2"]["checkout_digest"], "mandate_jti": po["jti"], "iat": int(time.time())}, bot, typ="checkout-mandate+jwt")
    assert not g.complete_checkout(c["id"], {"ap2": {"checkout_mandate": g.agent_proof(c, token)}}, po).get("order"), "the gateway's own agent key completed an order bound to the buyer's agent key"
    done = g.complete_checkout(c["id"], {"ap2": {"checkout_mandate": accept}}, po)
    o = done["order"]
    ev, au = g.get_order(o["id"])["wholesale"]["evidence"], g.audit(o["id"])
    assert ev["mandate"] == mine and ev["mandate_key"] == {"kid": own.jwk["kid"], "held_by": "buyer"}
    assert au["keys"]["buyer"]["kid"] == own.jwk["kid"] and jwt_verify(ev["mandate"], au["keys"]["buyer"]) and jwt_verify(ev["agent_acceptance"], bot.jwk)
    big = g.create_checkout({"line_items": [{"item": {"id": "464201"}, "quantity": 400}], "wholesale": {"delivery_date": day}}, po, "ownkey")
    assert big["status"] == "requires_escalation" and any(r["id"] == "B1" and r["outcome"] == "escalate" and "400.00" in r["detail"] for r in big["wholesale"]["policy"]["rules"]), "the limit the buyer signed is the limit applied"
    g.cancel_checkout(big["id"], po)

    # Distinct unactioned lookups are a W4 strike and three strikes suspend.
    for start in (0, 50, 100):
        try:
            g._harvest_check(p, [str(start + i) for i in range(45)])
        except GatewayError as e:
            assert e.code == "catalogue_harvest"
    assert g.store.get("agent:" + p["agent"])["status"] == "suspended"

    # Every reader gets a verifiable chain; only its account gets bodies.
    g.store.log("a", "agent:a", "lookup", "mine", {"x": 1}, acct="mine")
    g.store.log("b", "agent:b", "lookup", "theirs", {"x": 2}, acct="theirs")
    assert g.store.verify_chain()[0]
    view = g.store.ledger("mine")
    assert view[-2]["withheld"] is False and view[-1]["withheld"] is True and view[-1]["digest"]
    assert all(not e["withheld"] for e in g.store.ledger(operator=True))
    print("controls: all checks pass")


if __name__ == "__main__":
    main()
