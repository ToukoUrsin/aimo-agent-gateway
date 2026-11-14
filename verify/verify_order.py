# /// script
# requires-python = ">=3.10"
# dependencies = ["cryptography", "rfc8785"]
# ///
"""Check one order's signatures without trusting the gateway that served it.

    uv run verify/verify_order.py "https://trusted-trade-agents.fly.dev/orders/ord_...?k=..."
    uv run verify/verify_order.py order.json audit.json          # two saved files, fully offline
    uv run verify/verify_order.py https://trusted-trade-agents.fly.dev/api/proof/orders/ord_...   # an order of the model test on /proof
    uv run verify/verify_order.py URL --tamper                    # lower one price by a cent first: the checks must fail

This file imports nothing from the gateway. It reads the order and its audit document as plain JSON and redoes the
arithmetic: ES256 over canonical JSON (RFC 8785), the same construction UCP uses for ap2.merchant_authorization.
Valio Aimo's key is compared with the copy pinned in verify/pinned-keys.json, so a server that swapped its key to
match a changed order would not pass. The page at /orders/{id} runs the same checks in the browser; this is the
version that does not take its code from the server either.
"""
import base64
import copy
import hashlib
import json
import pathlib
import sys
import urllib.parse
import urllib.request

import rfc8785
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec, padding, utils

PINS = pathlib.Path(__file__).with_name("pinned-keys.json")


def b64d(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def b64u(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


def es256(jwk: dict, data: bytes, sig: bytes) -> bool:
    """A raw r||s ES256 signature against a P-256 JWK."""
    try:
        pub = ec.EllipticCurvePublicNumbers(int.from_bytes(b64d(jwk["x"]), "big"), int.from_bytes(b64d(jwk["y"]), "big"), ec.SECP256R1()).public_key()
        pub.verify(utils.encode_dss_signature(int.from_bytes(sig[:32], "big"), int.from_bytes(sig[32:], "big")), data, ec.ECDSA(hashes.SHA256()))
        return True
    except (InvalidSignature, ValueError, KeyError, TypeError):
        return False


def detached(obj, jws: str, jwk: dict) -> bool:
    """header..signature over base64url(header) + "." + base64url(JCS(obj))."""
    try:
        h, empty, s = jws.split(".")
        return empty == "" and json.loads(b64d(h)).get("kid") == jwk.get("kid") and es256(jwk, f"{h}.{b64u(rfc8785.dumps(obj))}".encode(), b64d(s))
    except Exception:
        return False


def jwt(token: str, jwk: dict):
    try:
        h, p, s = token.split(".")
        return json.loads(b64d(p)) if es256(jwk, f"{h}.{p}".encode(), b64d(s)) else None
    except Exception:
        return None


def thumbprint(jwk: dict) -> str:
    """RFC 7638."""
    return b64u(hashlib.sha256(json.dumps({"crv": jwk["crv"], "kty": jwk["kty"], "x": jwk["x"], "y": jwk["y"]}, separators=(",", ":")).encode()).digest())


def device(d: dict, approval: dict, site: str) -> tuple[bool, str]:
    """The approver's own device signed this decision: a WebAuthn assertion, or a key the browser holds."""
    try:
        cdj = b64d(d["client_data_json"])
        cd = json.loads(cdj)
        own = d.get("method") == "browser_key"
        signed = d["signed"]
        if cd.get("type") != ("aimo.key.get" if own else "webauthn.get"):
            return False, "the device answered a different kind of request"
        if cd.get("challenge") != b64u(hashlib.sha256(rfc8785.dumps(signed)).digest()):
            return False, "the device signed something other than the decision in the record"
        if any(signed.get(k) != approval.get(k) for k in ("checkout_digest", "decision", "by")) or rfc8785.dumps(signed.get("clears") or []) != rfc8785.dumps(approval.get("clears") or []):
            return False, "the decision the device signed is not the approval on this order"
        local = (urllib.parse.urlparse(cd.get("origin") or "").hostname or "") == "localhost"  # the gateway opened on its own machine
        if site and cd.get("origin") != site and not local:
            return False, f"the device signed for {cd.get('origin')}, not {site}"
        key = serialization.load_der_public_key(b64d(d["public_key"]))
        sig = b64d(d["signature"])
        if own:
            key.verify(utils.encode_dss_signature(int.from_bytes(sig[:32], "big"), int.from_bytes(sig[32:], "big")), cdj, ec.ECDSA(hashes.SHA256()))
            return True, f"a key held in the approver's browser ({d.get('device')}), no fingerprint" + (f", on {cd['origin']}" if local else "")
        ad = b64d(d["authenticator_data"])
        host = urllib.parse.urlparse(cd["origin"]).hostname or ""
        if ad[:32] != hashlib.sha256(host.encode()).digest() or not ad[32] & 0x01:
            return False, "the passkey answered for another site, or no person was present"
        data = ad + hashlib.sha256(cdj).digest()
        if d.get("alg") == "RS256":
            key.verify(sig, data, padding.PKCS1v15(), hashes.SHA256())
        else:
            key.verify(sig, data, ec.ECDSA(hashes.SHA256()))
        return True, f"a passkey on {d.get('device')}" + (", after fingerprint, face or screen lock" if ad[32] & 0x04 else "") + (f", on {cd['origin']}" if local else "")
    except InvalidSignature:
        return False, "the device's signature does not match"
    except Exception as e:
        return False, f"the device record could not be read ({type(e).__name__})"


def get(url: str):
    req = urllib.request.Request(url, headers={"accept": "application/json", "user-agent": "order-verifier"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read())


def checks(order: dict, audit: dict, site: str = "", buyer_key: dict | None = None) -> list[tuple]:
    """Every check on one order: (True, False or None for a note; what; detail).
    buyer_key: the buying company's public key from the buyer's own hands. Given, the mandate must verify with it, not with what the server lists."""
    ev = order["wholesale"]["evidence"]
    seller = audit["keys"]["seller"]
    pins = json.load(open(PINS)) if PINS.exists() else {}
    pinned = (pins.get(site) or {}).get("seller") if site else next((v.get("seller") for v in pins.values() if v.get("seller", {}).get("kid") == seller.get("kid")), None)
    out = []

    if pinned:
        out.append((thumbprint(pinned) == thumbprint(seller) == seller.get("kid"), "Valio Aimo's key is the one pinned in this repository", f"key {seller.get('kid', '')[:16]}…"))
        seller = pinned
    else:
        out.append((None, "Valio Aimo's key is not pinned for this address", "taken from the server; pin it in verify/pinned-keys.json to stop trusting that"))

    ok = detached({k: v for k, v in order.items() if k != "proof"}, order["proof"]["jws"], seller)
    out.append((ok, "Valio Aimo signed this confirmation", f"order {order.get('label')}, every field covered" if ok else f"order {order.get('label')} does not match the signature: it was changed after signing"))

    bk = buyer_key or audit["keys"].get("buyer")
    m = jwt(ev["mandate"], bk) if bk else None
    held = (ev.get("mandate_key") or {}).get("held_by")
    whose = ("with the key this check was given from the buyer's own hands" if buyer_key else "with a key the buyer registered itself, says the gateway; it holds the public half only" if held == "buyer"
             else "with the buyer's key, which the gateway keeps for this demo account (a stand-in: the buyer's own system holds it in production)")
    out.append((bool(m), "The buying company signed the agent's mandate", f"mandate {m['jti']} from {m['iss']}, {whose}" if m else "the mandate does not match the buyer's key"))

    acc = None
    if ev.get("agent_class") == "keyless":
        rc = ev.get("keyless_receipt") or {}
        if m and rc.get("mandate_jti") == m["jti"] and detached({k: v for k, v in rc.items() if k != "proof"}, rc["proof"]["jws"], seller):
            acc = rc
        out.append((bool(acc), "The assistant accepted the offer under its mandate", "it holds no key, so Valio Aimo signed a receipt of what it accepted"))
    else:
        acc = jwt(ev["agent_acceptance"], m["cnf"]["jwk"]) if m and m.get("cnf") and ev.get("agent_acceptance") else None
        out.append((bool(acc) and acc.get("mandate_jti") == m["jti"], "The agent accepted the offer with the key named in the mandate", f"signed by {acc['iss']}" if acc else "not signed with the agent key the mandate is bound to"))

    offer = audit.get("offer")
    dig = hashlib.sha256(rfc8785.dumps(offer)).hexdigest() if offer else ""
    same = bool(acc) and dig == ev.get("offer_digest") == acc.get("checkout_digest") and detached(offer, ev["offer_signature"], seller) and rfc8785.dumps(offer["totals"]) == rfc8785.dumps(order["totals"])
    out.append((same, "Both signed exactly the same terms", f"offer digest sha-256 {dig[:24]}…" if same else "the offer the agent accepted is not the offer behind this order"))

    ap = ev.get("approval")
    if ap:
        ok = bool(audit["keys"].get("approver")) and detached({k: v for k, v in ap.items() if k != "signature"}, ap["signature"]["jws"], audit["keys"]["approver"])
        out.append((ok, f"{ap['by']} approved it", f"overriding {', '.join(ap.get('overrides') or [])}; asked by {ap.get('asked_by', 'the agent')}"))
        if ap.get("device"):
            ok, how = device(ap["device"], ap, site)
            out.append((ok, "The approval was signed on the approver's own device", how))
        else:
            out.append((None, "No approver device signed this approval", "it came by link, and the gateway's own key signed the record"))
    else:
        out.append((True, "No person was needed", "every rule passed, so the mandate alone authorised the order"))

    return out


def show(out: list[tuple]) -> int:
    for ok, what, detail in out:
        print(f"{'PASS' if ok else 'NOTE' if ok is None else 'FAIL'}  {what}: {detail}")
    bad = sum(1 for ok, _, _ in out if ok is False)
    print(f"\n{'All checks pass.' if not bad else f'{bad} check(s) failed.'}")
    return 1 if bad else 0


def main() -> int:
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    tamper = "--tamper" in sys.argv
    if not args:
        print(__doc__)
        return 2
    site = ""
    if args[0].startswith("http"):
        u = urllib.parse.urlparse(args[0])
        site = f"{u.scheme}://{u.netloc}"
        q = f"?{u.query}" if u.query else ""
        first = get(f"{site}{u.path}{q}")
        if "audit" in first:  # a kept test order (/api/proof/orders/...): one document holds both, signed by the instance the test ran on
            order, audit, site = first["order"], first["audit"], ""
        else:
            order, audit = first, get(f"{site}{u.path}/audit{q}")
    else:
        first = json.load(open(args[0]))
        order, audit = (first["order"], first["audit"]) if "audit" in first else (first, json.load(open(args[1])))
    if tamper:
        order = copy.deepcopy(order)
        t = next(x for x in order["totals"] if x["type"] == "total")
        t["amount"] -= 1
        print("Tampered: the order's total was lowered by one cent before checking.\n")
    return show(checks(order, audit, site))


if __name__ == "__main__":
    sys.exit(main())
