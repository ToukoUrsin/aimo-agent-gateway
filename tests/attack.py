"""Outside-in security regression suite for the local Aimo gateway.

Run against only the authorised test listener: PORT=8665 uv run -q python -m tests.attack
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import secrets
import sys
import time
from urllib.parse import parse_qs, urlparse
from urllib.error import HTTPError, URLError
from urllib.request import Request, build_opener, HTTPBasicAuthHandler

from agent.approver import KeyApprover

BASE = os.environ.get("ATTACK_BASE", "http://127.0.0.1:8665").rstrip("/")
if not BASE.startswith("http://127.0.0.1:8665"):
    raise SystemExit("refusing to test anything except http://127.0.0.1:8665")

checks = []
failures = []


class Response:
    def __init__(self, status_code, body, headers=None):
        self.status_code, self.text, self.headers = status_code, body, headers or {}

    def json(self):
        return json.loads(self.text)


class Session:
    def __init__(self):
        self.cookies = {}

    def request(self, method, url, headers=None, json=None, data=None, params=None, cookies=None, timeout=12):
        if params:
            from urllib.parse import urlencode
            url += ("&" if "?" in url else "?") + urlencode(params)
        h = dict(headers or {})
        jar = dict(self.cookies)
        jar.update(cookies or {})
        if jar:
            h["Cookie"] = "; ".join(f"{k}={v}" for k, v in jar.items())
        if json is not None:
            raw = jsonlib.dumps(json).encode()
            h.setdefault("Content-Type", "application/json")
        elif data is not None:
            from urllib.parse import urlencode
            raw = urlencode(data).encode()
            h.setdefault("Content-Type", "application/x-www-form-urlencoded")
        else:
            raw = None
        req = Request(url, data=raw, headers=h, method=method)
        try:
            with build_opener().open(req, timeout=timeout) as res:
                body = res.read().decode("utf-8", "replace")
                out = Response(res.status, body, dict(res.headers.items()))
                sc = res.headers.get("Set-Cookie", "")
                if sc:
                    pair = sc.split(";", 1)[0]
                    if "=" in pair:
                        k, v = pair.split("=", 1)
                        self.cookies[k] = v
                return out
        except HTTPError as e:
            body = e.read().decode("utf-8", "replace")
            return Response(e.code, body, dict(e.headers.items()))


jsonlib = json

def check(name, ok):
    checks.append(name)
    if not ok:
        failures.append(name)
        print(f"FAIL {name}")
    else:
        print(f"ok   {name}")


def call(s, method, path, **kw):
    kw.setdefault("timeout", 12)
    try:
        return s.request(method, BASE + path, **kw)
    except Exception as e:
        return Response(0, "", {"error": str(e)})


def obj(r):
    try:
        return r.json() if hasattr(r, "json") else {}
    except Exception:
        return {}


def codes(j):
    return {m.get("code") for m in (j.get("messages") or []) if isinstance(m, dict)}


def new_client():
    s = Session()
    r = call(s, "GET", "/api/console")
    j = obj(r)
    token = obj(call(s, "GET", "/api/demo/mandate-token")).get("mandate")
    return s, token, j.get("account", {}), {"Authorization": "Bearer " + token} if token else {}


def b64json(part):
    return json.loads(base64.urlsafe_b64decode(part + "=" * (-len(part) % 4)))


def mutate_jwt(tok, header=None, claims=None, signature=""):
    h, p, s = tok.split(".")
    hh, pp = b64json(h), b64json(p)
    if header:
        hh.update(header)
    if claims:
        pp.update(claims)
    enc = lambda x: base64.urlsafe_b64encode(json.dumps(x, separators=(",", ":")).encode()).rstrip(b"=").decode()
    return enc(hh) + "." + enc(pp) + "." + (signature or s)


def commercial_leak(x):
    if isinstance(x, dict):
        for k, v in x.items():
            if k in {"price", "stock", "orderable", "reason", "available", "availableAmount"}:
                return True
            if commercial_leak(v):
                return True
    elif isinstance(x, list):
        return any(commercial_leak(v) for v in x)
    return False


def main():
    probe = Session()
    for _ in range(30):
        if call(probe, "GET", "/.well-known/ucp").status_code == 200:
            break
        time.sleep(0.2)
    a, atok, aacct, ah = new_client()
    b, btok, bacct, bh = new_client()
    check("test server is reachable", bool(aacct.get("slug")))

    # Discovery and unauthenticated boundary.
    r = call(a, "GET", "/.well-known/ucp"); j = obj(r)
    check("discovery is local and signed", r.status_code == 200 and j.get("keys", [{}])[0].get("alg") == "ES256" and BASE in json.dumps(j))
    check("discovery publishes no private key", r.status_code == 200 and all(k not in j.get("keys", [{}])[0] for k in ("d", "private_key", "secret")))
    r = call(a, "GET", "/mcp")
    check("MCP requires a mandate", getattr(r, "status_code", 0) == 401)
    r = call(a, "POST", "/catalog/search", json={"query": "milk"}); j = obj(r)
    check("public search is available", r.status_code == 200 and bool(j.get("products")))
    check("public search hides commercial facts", not commercial_leak(j))
    r = call(a, "POST", "/catalog/lookup", json={"ids": ["464201"]}); j = obj(r)
    check("public lookup hides commercial facts", r.status_code == 200 and not commercial_leak(j))
    r = call(a, "POST", "/sourcing/plan", json={"query": "milk", "need": {"quantity": 1, "unit": "L"}}); j = obj(r)
    check("public sourcing does not issue an order", r.status_code == 200 and not j.get("options"))
    check("public sourcing explains mandate boundary", any("mandate" in str(x).lower() for x in j.get("not_offered", []) + j.get("messages", [])))
    r = call(a, "POST", "/checkout-sessions", json={"line_items": [{"item": {"id": "464201"}, "quantity": 1}], "wholesale": {"delivery_date": "2026-11-16"}}); j = obj(r)
    check("anonymous checkout cannot be ready", r.status_code == 422 and j.get("status") == "incomplete" and "mandate_required" in codes(j))
    badh = {"Authorization": "Bearer definitely.not.a.jwt"}
    r = call(a, "POST", "/checkout-sessions", headers=badh, json={"line_items": [{"item": {"id": "464201"}, "quantity": 1}], "wholesale": {"delivery_date": "2026-11-16"}}); j = obj(r)
    check("malformed mandate cannot order", r.status_code == 422 and j.get("status") == "incomplete" and bool(codes(j) & {"mandate_required", "mandate_invalid_signature", "mandate_scope_mismatch"}))
    r = call(a, "GET", "/issues")
    check("issues require a mandate", r.status_code == 401)

    # JWT tampering and claim confusion.
    for name, tok in (
        ("expired mandate tamper", mutate_jwt(atok, claims={"exp": 1})),
        ("wrong audience tamper", mutate_jwt(atok, claims={"aud": "https://evil.example"})),
        ("wrong issuer tamper", mutate_jwt(atok, claims={"iss": "https://buyer.example/other"})),
        ("alg none tamper", mutate_jwt(atok, header={"alg": "none"}, signature="")),
        ("HS256 tamper", mutate_jwt(atok, header={"alg": "HS256"}, signature="c2ln")),
        ("bad signature tamper", mutate_jwt(atok, signature="AAAA")),
    ):
        r = call(a, "POST", "/checkout-sessions", headers={"Authorization": "Bearer " + tok}, json={"line_items": [{"item": {"id": "464201"}, "quantity": 1}], "wholesale": {"delivery_date": "2026-11-16"}})
        check(name + " is rejected", r.status_code == 422 and obj(r).get("status") == "incomplete")

    # Input validation and numeric edge cases.
    malformed = [
        ("negative quantity", {"quantity": -1}, "invalid_quantity"),
        ("zero quantity", {"quantity": 0}, "invalid_quantity"),
        ("fractional quantity", {"quantity": 1.5}, "invalid_quantity"),
        ("string quantity", {"quantity": "1"}, "invalid_quantity"),
        ("boolean quantity", {"quantity": True}, "invalid_quantity"),
        ("NaN quantity", {"quantity": "NaN"}, "invalid_quantity"),
    ]
    for name, line, code in malformed:
        r = call(a, "POST", "/checkout-sessions", headers=ah, json={"line_items": [{"item": {"id": "464201"}, **line}], "wholesale": {"delivery_date": "2026-11-16"}}); j = obj(r)
        check(name + " is rejected without 5xx", r.status_code == 422 and j.get("status") == "incomplete" and code in codes(j))
    r = call(a, "POST", "/checkout-sessions", headers=ah, json={"line_items": [{"item": {"id": "464201"}, "quantity": 10_000_000}], "wholesale": {"delivery_date": "2026-11-16"}}); j = obj(r)
    check("huge quantity hits mandate cap", r.status_code == 422 and "mandate_quantity_limit" in codes(j))
    for name, body in [
        ("null line", {"line_items": [None]}),
        ("null item", {"line_items": [{"item": None, "quantity": 1}]}),
        ("list wholesale", {"line_items": [{"item": {"id": "464201"}, "quantity": 1}], "wholesale": []}),
        ("string requirements", {"line_items": [{"item": {"id": "464201"}, "quantity": 1}], "wholesale": {"requirements": "x"}}),
        ("list context", {"line_items": [{"item": {"id": "464201"}, "quantity": 1}], "context": []}),
    ]:
        r = call(a, "POST", "/checkout-sessions", headers=ah, json=body)
        check(name + " returns a client error, not 500", getattr(r, "status_code", 0) in (400, 409, 422))
    r = call(a, "POST", "/checkout-sessions", headers=ah, json={"line_items": [{"item": {"id": "does-not-exist"}, "quantity": 1}], "wholesale": {"delivery_date": "2026-11-16"}}); j = obj(r)
    check("unknown product is a structured error", r.status_code == 422 and j.get("status") == "incomplete" and "product_not_found" in codes(j))
    for name, body in [
        ("missing need", {"query": "milk"}),
        ("zero need", {"query": "milk", "need": {"quantity": 0, "unit": "L"}}),
        ("boolean need", {"query": "milk", "need": {"quantity": True, "unit": "L"}}),
        ("missing unit", {"query": "milk", "need": {"quantity": 1}}),
        ("null need", {"query": "milk", "need": None}),
    ]:
        r = call(a, "POST", "/sourcing/plan", headers=ah, json=body)
        check(name + " sourcing input is rejected", getattr(r, "status_code", 0) in (400, 422))
    r = call(a, "POST", "/sourcing/plan", headers=ah, json={"query": "milk", "need": {"quantity": 1, "unit": "unknown-unit"}})
    check("unknown sourcing unit never crashes", getattr(r, "status_code", 0) < 500)
    r = call(a, "POST", "/sourcing/plan", headers=ah, json={"query": "milk", "filters": [], "need": {"quantity": 1, "unit": "L"}})
    check("list sourcing filters are rejected", r.status_code == 400)

    # A valid checkout and account isolation.
    req = {"line_items": [{"item": {"id": "464201"}, "quantity": 1}], "context": {"intent": "<script>alert(1)</script>"}, "wholesale": {"delivery_date": "2026-11-16", "rationale": "<img src=x onerror=alert(1)>"}}
    r = call(a, "POST", "/checkout-sessions", headers=ah, json=req); c1 = obj(r)
    check("valid checkout is a signed offer", r.status_code == 201 and c1.get("status") == "ready_for_complete" and c1.get("ap2", {}).get("checkout_digest"))
    cid = c1.get("id", "")
    check("offer exposes no internal storage", "_internal" not in c1 and "approval_token" not in json.dumps(c1))
    r = call(a, "GET", f"/checkout-sessions/{cid}", headers=bh)
    check("other account cannot read checkout", r.status_code == 403)
    r = call(a, "PUT", f"/checkout-sessions/{cid}", headers=bh, json=req)
    check("other account cannot update checkout", r.status_code == 403)
    r = call(a, "POST", f"/checkout-sessions/{cid}/cancel", headers=bh)
    check("other account cannot cancel checkout", r.status_code == 403)
    r = call(a, "POST", f"/checkout-sessions/{cid}/complete", headers=bh, json={})
    check("other account cannot complete checkout", r.status_code == 403)
    r = call(a, "GET", f"/checkout-sessions/{cid}")
    check("checkout reads require mandate", r.status_code == 401)
    r = call(a, "GET", "/checkout-sessions/chk_does_not_exist", headers=ah)
    check("unguessable checkout ids fail closed", r.status_code == 404)
    r = call(a, "GET", f"/api/demo/checkout/{cid}", cookies={"aimo_s": b.cookies.get("aimo_s", "")})
    check("demo checkout has no cross-cookie leak", r.status_code == 404 or obj(r).get("id") == cid)
    r = call(a, "GET", "/api/console", headers={"x-account": "stage"})
    check("x-account cannot select stage without operator", r.status_code == 200 and not obj(r).get("operator") and obj(r).get("account", {}).get("slug") != "stage")
    r = call(a, "GET", "/api/console?acct=stage")
    check("acct query cannot select stage without operator", r.status_code == 200 and not obj(r).get("operator") and obj(r).get("account", {}).get("slug") != "stage")

    # Operator boundary and demo endpoint abuse.
    check("wrong operator key rejected", call(a, "GET", "/op?key=wrong").status_code == 403)
    check("account creation needs operator", call(a, "POST", "/api/demo/account", json={"slug": "attacker-account"}).status_code == 403)
    check("wipe needs operator", call(a, "POST", "/api/demo/wipe").status_code == 403)
    check("seller switch needs operator", call(a, "POST", "/api/seller/switch", json={"on": True}).status_code == 403)
    check("recall close cannot be unauthorised", call(a, "POST", "/api/recalls/rcl_missing/close").status_code == 403)
    check("foreign agent control does not disclose it", call(a, "POST", "/api/demo/agent", json={"id": "agent:stage/purchasing-1", "suspended": True}).status_code in (403, 404))

    # OAuth registration, redirect binding, PKCE and one-time codes.
    for name, uri in [("javascript redirect", "javascript:alert(1)"), ("data redirect", "data:text/html,x"), ("evil http redirect", "http://evil.example/cb")]:
        r = call(a, "POST", "/oauth/register", json={"client_name": name, "redirect_uris": [uri]})
        check(name + " rejected", r.status_code == 400)
    redirect = BASE + "/oauth-test/callback"
    r = call(a, "POST", "/oauth/register", json={"client_name": "Attack test", "redirect_uris": [redirect]}); reg = obj(r)
    check("valid OAuth client registration works", r.status_code == 201 and reg.get("client_id"))
    cl = reg.get("client_id", "")
    check("OAuth context binds client and redirect", call(a, "GET", "/api/oauth/context", params={"client_id": cl, "redirect_uri": redirect + "-wrong"}).status_code == 400)
    verifier = secrets.token_urlsafe(32)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    deny = call(a, "POST", "/api/oauth/decide", json={"client_id": cl, "redirect_uri": redirect, "allow": False, "code_challenge": challenge, "code_challenge_method": "S256", "state": "s"}); dj = obj(deny)
    check("OAuth denial returns to registered redirect", deny.status_code == 200 and parse_qs(urlparse(dj.get("redirect", "")).query).get("error") == ["access_denied"])
    allow = call(a, "POST", "/api/oauth/decide", json={"client_id": cl, "redirect_uri": redirect, "allow": True, "code_challenge": challenge, "code_challenge_method": "plain"}); aj = obj(allow)
    check("OAuth PKCE downgrade is refused", allow.status_code == 200 and "invalid_request" in aj.get("redirect", ""))
    allow = call(a, "POST", "/api/oauth/decide", json={"client_id": cl, "redirect_uri": redirect, "allow": True, "code_challenge": challenge, "code_challenge_method": "S256"}); aj = obj(allow)
    red = aj.get("redirect", ""); code = parse_qs(urlparse(red).query).get("code", [""])[0]
    check("OAuth consent issues a code", bool(code))
    bad = call(a, "POST", "/oauth/token", data={"grant_type": "authorization_code", "client_id": cl, "redirect_uri": redirect, "code": code, "code_verifier": "wrong"})
    check("OAuth wrong verifier rejected", bad.status_code == 400 and obj(bad).get("error") == "invalid_grant")
    good = call(a, "POST", "/oauth/token", data={"grant_type": "authorization_code", "client_id": cl, "redirect_uri": redirect, "code": code, "code_verifier": verifier}); tj = obj(good)
    check("OAuth correct verifier exchanges code", good.status_code == 200 and tj.get("access_token") and tj.get("refresh_token"))
    replay = call(a, "POST", "/oauth/token", data={"grant_type": "authorization_code", "client_id": cl, "redirect_uri": redirect, "code": code, "code_verifier": verifier})
    check("OAuth code replay rejected", replay.status_code == 400 and obj(replay).get("error") == "invalid_grant")
    rt = tj.get("refresh_token", "")
    wrong_client = call(a, "POST", "/oauth/token", data={"grant_type": "refresh_token", "refresh_token": rt, "client_id": "cl_wrong"})
    check("OAuth refresh client confusion rejected", wrong_client.status_code == 400)
    refresh = call(a, "POST", "/oauth/token", data={"grant_type": "refresh_token", "refresh_token": rt, "client_id": cl})
    check("OAuth refresh works for same client", refresh.status_code == 200 and obj(refresh).get("access_token"))
    claims = b64json(tj["access_token"].split(".")[1])
    disc = call(a, "POST", "/api/oauth/disconnect", json={"id": claims.get("sub")})
    check("OAuth disconnect is scoped to own browser", disc.status_code == 200)
    dead = call(a, "POST", "/oauth/token", data={"grant_type": "refresh_token", "refresh_token": rt, "client_id": cl})
    check("disconnected OAuth refresh dies", dead.status_code == 400)

    # Approval link secrecy, state transitions and idempotence.
    esc_req = {"line_items": [{"item": {"id": "464201"}, "quantity": 400}], "wholesale": {"delivery_date": "2026-11-16"}}
    r = call(a, "POST", "/checkout-sessions", headers=ah, json=esc_req); esc = obj(r)
    check("over-cap checkout escalates", r.status_code == 201 and esc.get("status") == "requires_escalation" and "?k=" in esc.get("continue_url", ""))
    ecid, link = esc.get("id", ""), esc.get("continue_url", "")
    k = parse_qs(urlparse(link).query).get("k", [""])[0]
    check("approval view without token rejected", call(a, "GET", f"/api/approve/{ecid}").status_code == 403)
    check("approval view wrong token rejected", call(a, "GET", f"/api/approve/{ecid}", params={"k": "wrong"}).status_code == 403)
    av = call(a, "GET", f"/api/approve/{ecid}", params={"k": k}); aj = obj(av)
    check("approval view with exact token works", av.status_code == 200 and aj.get("id") == ecid and "_internal" not in aj)
    tam = call(a, "POST", f"/api/approve/{ecid}", json={"k": k + "x", "approve": True, "approver": "Mallory"})
    check("approval decision wrong token rejected", tam.status_code == 403)
    rej = call(a, "POST", f"/api/approve/{ecid}", json={"k": k, "approve": False, "approver": "Test approver", "note": "no"}); rj = obj(rej)
    check("approval rejection cancels checkout", rej.status_code == 200 and rj.get("status") == "canceled")
    rerej = call(a, "POST", f"/api/approve/{ecid}", json={"k": k, "approve": True, "approver": "Mallory"})
    check("approval link cannot be replayed after rejection", rerej.status_code in (403, 409) and obj(call(a, "GET", f"/api/approve/{ecid}", params={"k": k})).get("status") == "canceled")
    r = call(a, "POST", "/checkout-sessions", headers=ah, json=esc_req); esc2 = obj(r); ecid2 = esc2.get("id", ""); k2 = parse_qs(urlparse(esc2.get("continue_url", "")).query).get("k", [""])[0]
    # No approver device was ever enrolled for this account. The link with a name is still not an approval: a yes needs a device's signature.
    bare = call(a, "POST", f"/api/approve/{ecid2}", json={"k": k2, "approve": True, "approver": "Someone holding the link"})
    check("link alone cannot approve on a fresh account", bare.status_code == 403 and "device_required" in codes(obj(bare)) and obj(call(a, "GET", f"/api/approve/{ecid2}", params={"k": k2})).get("status") == "requires_escalation")
    anon = call(a, "POST", f"/api/approve/{ecid2}", json={"k": k2, "approve": True})
    check("approval without a name is refused", anon.status_code == 400 and "approver_required" in codes(obj(anon)))
    approver = KeyApprover(obj(call(a, "GET", "/api/console")).get("public_url", BASE))
    apj = approver.approve_sync(lambda path, body: obj(call(a, "POST", path, json=body)), ecid2, k2, "Test approver")
    dev = apj.get("wholesale", {}).get("approval", {}).get("device") or {}
    check("approval changes state to ready", apj.get("status") == "ready_for_complete" and apj.get("wholesale", {}).get("approval", {}).get("decision") == "approved")
    check("the approval carries the device's signature over this decision", dev.get("method") == "browser_key" and dev.get("credential") == approver.id and dev.get("signed", {}).get("checkout") == ecid2 and dev.get("signed", {}).get("by") == "Test approver")
    r = call(a, "POST", "/checkout-sessions", headers=ah, json=esc_req); esc3 = obj(r); ecid3 = esc3.get("id", ""); k3 = parse_qs(urlparse(esc3.get("continue_url", "")).query).get("k", [""])[0]
    stolen = {"id": dev.get("credential"), "clientDataJSON": dev.get("client_data_json"), "signature": dev.get("signature")}
    replay = call(a, "POST", f"/api/approve/{ecid3}", json={"k": k3, "approve": True, "approver": "Test approver", "device": stolen})
    check("a device signature for one order does not approve another", replay.status_code in (400, 403) and obj(call(a, "GET", f"/api/approve/{ecid3}", params={"k": k3})).get("status") == "requires_escalation")
    other = KeyApprover(approver.origin, "Another key")
    forged = call(a, "POST", f"/api/approve/{ecid3}", json={"k": k3, "approve": True, "approver": "Test approver", "device": {"id": other.id, **other.signed("aimo.key.get", "x")}})
    check("a key that was never enrolled cannot approve", forged.status_code == 403 and "unknown_device" in codes(obj(forged)))
    call(a, "POST", f"/api/approve/{ecid3}", json={"k": k3, "approve": False, "approver": "Test approver", "note": "test over"})
    done = call(a, "POST", f"/checkout-sessions/{ecid2}/complete", headers=ah, json={}); donej = obj(done)
    check("approved keyless checkout completes", done.status_code == 200 and donej.get("status") == "completed" and donej.get("order", {}).get("id"))
    twice = call(a, "POST", f"/checkout-sessions/{ecid2}/complete", headers=ah, json={}); check("completing twice is idempotent", twice.status_code == 200 and obj(twice).get("status") == "completed" and obj(twice).get("order") == donej.get("order"))
    check("completed checkout cannot be canceled", call(a, "POST", f"/checkout-sessions/{ecid2}/cancel", headers=ah).status_code == 409)

    # Order, audit and issue scoping.
    oid = donej.get("order", {}).get("id", "")
    r = call(a, "GET", f"/orders/{oid}", headers=ah); oj = obj(r)
    check("owner can read signed order", r.status_code == 200 and oj.get("id") == oid and oj.get("proof", {}).get("jws"))
    check("other account cannot read order", call(b, "GET", f"/orders/{oid}", headers=bh).status_code == 401)
    check("anonymous cannot read order", call(Session(), "GET", f"/orders/{oid}").status_code == 401)
    check("other account cannot read audit", call(b, "GET", f"/orders/{oid}/audit", headers=bh).status_code == 401)
    check("owner can read audit", call(a, "GET", f"/orders/{oid}/audit", headers=ah).status_code == 200)
    check("unknown order is 404", call(a, "GET", "/orders/ord_missing", headers=ah).status_code == 404)
    issue_resp = call(a, "POST", "/api/demo/issue", json={"order": oid, "kind": "short_pick", "delivered": 200})
    issuej = obj(issue_resp); iid = issuej.get("issue", {}).get("id", "")
    check("demo issue notice is signed", issue_resp.status_code == 200 and issuej.get("notice", {}).get("proof", {}).get("jws"))
    check("owner can list own issue", call(a, "GET", "/issues", headers=ah).status_code == 200)
    check("other account cannot read issue", call(b, "GET", f"/issues/{iid}", headers=bh).status_code == 403)
    check("other account cannot resolve issue", call(b, "POST", f"/issues/{iid}/resolve", headers=bh, json={"option": "accept_partial"}).status_code == 403)
    check("unknown issue is 404", call(a, "GET", "/issues/iss_missing", headers=ah).status_code == 404)

    # HTML and API output encoding, plus no internal state in human-facing pages.
    page = call(a, "GET", f"/approve/{ecid2}", params={"k": k2})
    check("approval page does not embed attacker text", page.status_code == 200 and "onerror=alert" not in page.text and "<script>alert(1)</script>" not in page.text)
    order_page = call(a, "GET", f"/orders/{oid}", headers={"Accept": "text/html"})
    check("order page is a generic shell before authenticated fetch", order_page.status_code == 200 and "const oid = location.pathname" in order_page.text and "_internal" not in order_page.text)
    check("order JSON does not expose internal storage", "_internal" not in json.dumps(oj))

    # Rate limiting is keyed to the authenticated principal, not spoofable XFF.
    limited = False
    for i in range(70):
        rr = call(a, "GET", "/checkout-sessions/chk_missing", headers={**ah, "X-Forwarded-For": f"198.51.100.{i + 1}"})
        if getattr(rr, "status_code", 0) == 429:
            limited = True
            break
    check("authenticated rate limit survives spoofed XFF", limited)
    time.sleep(1.5)

    # Request-body and path traversal probes.
    huge = call(a, "POST", "/catalog/search", headers=ah, json={"query": "x" * 20000})
    check("large search input is bounded without 5xx", getattr(huge, "status_code", 0) < 500)
    for path in ("/../etc/passwd", "/static/../gateway/service.py", "/orders/%2e%2e%2fetc%2fpasswd"):
        rr = call(a, "GET", path)
        check("path traversal blocked " + path, getattr(rr, "status_code", 0) in (400, 404, 405))

    print(f"\nATTACK CHECKS: {len(checks)}; PASS: {len(checks) - len(failures)}; FAIL: {len(failures)}")
    if failures:
        print("FAILED:")
        for f in failures:
            print(" -", f)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
