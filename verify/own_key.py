# /// script
# requires-python = ">=3.10"
# dependencies = ["cryptography", "rfc8785"]
# ///
"""Order with a key the gateway never held.

    uv run verify/own_key.py https://trusted-trade-agents.fly.dev

The demo accounts on the site keep the buyer's signing key on the gateway, which is a stand-in. This file is the real arrangement,
run from your machine: it makes a company key and an agent key here, registers only the public half, writes and signs the agent's
mandate here (with a lower limit than the default, to show whose limit binds), orders under it, and then checks the signed order
against the keys that never left this process. It imports nothing from the gateway.
"""
import http.cookiejar
import json
import secrets
import sys
import time
import urllib.error
import urllib.request

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec, utils

from verify_order import b64u, checks, jwt, show, thumbprint


class Key:
    def __init__(self):
        self.priv = ec.generate_private_key(ec.SECP256R1())
        n = self.priv.public_key().public_numbers()
        self.jwk = {"kty": "EC", "crv": "P-256", "x": b64u(n.x.to_bytes(32, "big")), "y": b64u(n.y.to_bytes(32, "big"))}
        self.jwk["kid"] = thumbprint(self.jwk)

    def jwt(self, claims: dict, typ: str) -> str:
        h = b64u(json.dumps({"alg": "ES256", "kid": self.jwk["kid"], "typ": typ}, separators=(",", ":")).encode())
        p = b64u(json.dumps(claims, separators=(",", ":")).encode())
        r, s = utils.decode_dss_signature(self.priv.sign(f"{h}.{p}".encode(), ec.ECDSA(hashes.SHA256())))
        return f"{h}.{p}.{b64u(r.to_bytes(32, 'big') + s.to_bytes(32, 'big'))}"


def main() -> int:
    if len(sys.argv) < 2:
        print(__doc__)
        return 2
    site = sys.argv[1].rstrip("/")
    web = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))

    def call(path, body=None, token=None):
        req = urllib.request.Request(site + path, data=json.dumps(body).encode() if body is not None else None,
                                     headers={"accept": "application/json", "content-type": "application/json", "user-agent": "own-key", **({"authorization": f"Bearer {token}"} if token else {})})
        try:
            with web.open(req, timeout=90) as r:
                return json.loads(r.read())
        except urllib.error.HTTPError as e:
            return json.loads(e.read())

    said = lambda d: "; ".join(f"{m.get('code')}: {m.get('content')}" for m in d.get("messages") or [] if m.get("type") == "error") or d.get("status") or str(d)[:200]
    eur = lambda c: f"€{c / 100:,.2f}"

    company, agent = Key(), Key()
    print(f"1. Made two keys on this machine. Company key {company.jwk['kid'][:16]}…, agent key {agent.jwk['kid'][:16]}…. The private halves stay in this process.")

    reg = call("/api/buyer/key", {"jwk": company.jwk})
    if "issuer" not in reg:
        print("   The gateway did not take the key: " + said(reg))
        return 1
    print(f"2. Registered the public half for a new sandbox account ({reg['customer']}). The gateway now knows this company by a key it cannot sign with.")

    limit = 40000  # €400 per order: lower than the account's default, so a refusal can only come from the mandate signed here
    terms = {**reg["mandate"], "budget": {**reg["mandate"]["budget"], "max_per_order": limit}}
    def mandate(key, **change):
        now = int(time.time())
        return key.jwt({"iss": reg["issuer"], "sub": reg["agent_prefix"] + "own-key-agent", "aud": reg["audience"], "iat": now, "exp": now + 3600, "jti": "mnd_" + secrets.token_hex(6),
                        "agent_name": "Own-key agent", "cnf": {"jwk": agent.jwk}, "mandate": terms, **change}, "mandate+jwt")
    tok = mandate(company)
    print(f"3. Wrote and signed the agent's mandate here: at most {eur(limit)} per order without a person, bound to the agent key.")

    def offer(litres):
        plan = call("/sourcing/plan", {"query": "lactose-free milk", "need": {"quantity": litres, "unit": "l"}}, tok)
        opt = next(iter(plan.get("options") or []), None)
        if not opt:
            return None, said(plan)
        c = call("/checkout-sessions", {"line_items": opt["line_items"], "context": {"intent": f"{litres} L of lactose-free milk, ordered under a mandate the buyer signed with its own key"},
                                        "wholesale": {"delivery_date": plan["delivery_date"], "rationale": "verify/own_key.py: the first option the gateway offered."}}, tok)
        return c, said(c)

    c, why = offer(100)
    if not c or c.get("status") != "ready_for_complete":
        print("   No offer could be ordered alone: " + why)
        return 1
    total = next(t["amount"] for t in c["totals"] if t["type"] == "total")
    accept = agent.jwt({"iss": reg["agent_prefix"] + "own-key-agent", "checkout_id": c["id"], "checkout_digest": c["ap2"]["checkout_digest"], "merchant_authorization": c["ap2"]["merchant_authorization"],
                        "mandate_jti": jwt(tok, company.jwk)["jti"], "iat": int(time.time())}, "checkout-mandate+jwt")
    done = call(f"/checkout-sessions/{c['id']}/complete", {"ap2": {"checkout_mandate": accept}}, tok)
    order = done.get("order")
    if not order:
        print("   The order was not placed: " + said(done))
        return 1
    print(f"4. Ordered 100 L under it: {order.get('label')}, {eur(total)} with VAT, nobody asked. The agent signed the offer with its own key.")

    big, why = offer(400)
    held = bool(big) and big.get("status") == "requires_escalation"
    rule = next((r["detail"] for r in (((big or {}).get("wholesale") or {}).get("policy") or {}).get("rules", []) if r.get("outcome") == "escalate"), why)
    print(f"5. Asked for 400 L under the same mandate: {'sent to a person' if held else 'NOT held: ' + str((big or {}).get('status'))}. {rule}")
    if big:
        call(f"/checkout-sessions/{big['id']}/cancel", {}, tok)

    forged = call("/checkout-sessions", {"line_items": c["line_items"], "wholesale": {"delivery_date": c["wholesale"]["delivery_date"]}},
                  mandate(Key(), mandate={**terms, "budget": {**terms["budget"], "max_per_order": 100_000_00}}))
    refused = forged.get("status") == "incomplete" or forged.get("ucp", {}).get("status") == "error"
    print(f"6. Sent the same mandate with the limit raised to €100,000, signed with another key: {'refused' if refused else 'NOT refused'}. {said(forged)}")
    other = call("/checkout-sessions", {"line_items": c["line_items"], "wholesale": {"delivery_date": c["wholesale"]["delivery_date"]}}, mandate(company, sub="agent:stage/purchasing-1"))
    refused2 = other.get("status") == "incomplete" or other.get("ucp", {}).get("status") == "error"
    print(f"7. Signed a mandate with the right key for another company's agent: {'refused' if refused2 else 'NOT refused'}. {said(other)}")

    print("\nThe order, checked against the keys made in step 1:\n")
    oid = order["id"]
    full, audit = call(f"/orders/{oid}", token=tok), call(f"/orders/{oid}/audit", token=tok)
    out = checks(full, audit, site, buyer_key=company.jwk)
    ev = full["wholesale"]["evidence"]
    acc = jwt(ev.get("agent_acceptance") or "", agent.jwk)
    out.append((ev.get("mandate") == tok, "The mandate in the order is the one signed here, byte for byte", "no field was added, dropped or re-signed on the way"))
    out.append((bool(acc) and acc.get("checkout_id") == c["id"], "The acceptance in the order verifies with the agent key made here", f"key {agent.jwk['kid'][:16]}…"))
    out.append(((ev.get("mandate_key") or {}).get("kid") == company.jwk["kid"] and (ev.get("mandate_key") or {}).get("held_by") == "buyer", "The order records whose key signed the mandate", "the buyer's own, registered in step 2"))
    out.append((held, "The limit signed here is the limit the gateway applied", f"{eur(limit)} per order: 100 L went through alone, 400 L went to a person"))
    out.append((refused and refused2, "A mandate this company did not sign, or signed for someone else's agent, is refused", "steps 6 and 7"))
    return show(out)


if __name__ == "__main__":
    sys.exit(main())
