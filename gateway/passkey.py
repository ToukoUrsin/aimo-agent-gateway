"""A person's own device signs the approval (a WebAuthn passkey).

The key is made inside the phone or laptop and never leaves it.  The gateway keeps only the public half, so nobody
else can produce that approval: not the agent, not Valio Aimo, not this server.  What the device signs is the decision
itself (which checkout, its digest, approved or rejected, by whom, the note), so the signature cannot be moved to
another order or flipped from reject to approve.

Enrolling a device is the buyer's act: from the account's own session, or through an enrolment link the account made.
The approval link alone cannot enrol one.  Once an account has an enrolled device, approving by link alone stops
working for that account.  In production the buyer's company sign-in would gate enrolment; here the demo account's own
browser session does.
"""
import hashlib
import hmac
import json
import secrets
import time
from urllib.parse import urlparse

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec, padding, rsa
from fastapi import APIRouter, Body, Request

from .config import OPERATOR_KEY, PUBLIC_URL
from .signing import b64d, b64u, jcs

ALGS = {-7: "ES256", -257: "RS256"}
ENROL_TTL = 15 * 60
MAX_DEVICES = 5


class PasskeyError(Exception):
    def __init__(self, code, message, status=400):
        super().__init__(message)
        self.code, self.message, self.status = code, message, status


def devices(store, slug: str) -> list[dict]:
    return store.get("passkeys:" + slug) or []


def public(d: dict) -> dict:
    """What a page may show about an enrolled device."""
    return {"id": d["id"], "name": d["name"], "enrolled_at": d["enrolled_at"], "alg": d["alg"], "thumbprint": d["thumbprint"]}


def decision_of(cid: str, digest: str, approve: bool, by: str, note: str) -> dict:
    return {"checkout": cid, "checkout_digest": digest, "decision": "approved" if approve else "rejected", "by": by, "note": note}


def challenge_of(obj) -> str:
    return b64u(hashlib.sha256(jcs(obj)).digest())


def origins(request: Request) -> set[str]:
    """Where a signature may come from: the public address, and this machine when it is opened as localhost."""
    out = {PUBLIC_URL}
    host = request.headers.get("host", "")
    if host.split(":")[0] == "localhost":
        out.add("http://" + host)
    return out


def _client(cdj: bytes, typ: str, challenge: str, allowed: set[str]) -> str:
    try:
        cd = json.loads(cdj)
    except ValueError:
        raise PasskeyError("bad_device_data", "The device's answer could not be read.")
    if cd.get("type") != typ:
        raise PasskeyError("bad_device_data", "The device answered a different kind of request.")
    if not hmac.compare_digest(str(cd.get("challenge", "")), challenge):
        raise PasskeyError("wrong_decision", "The device signed something other than this decision.")
    if cd.get("origin") not in allowed:
        raise PasskeyError("wrong_site", "The device signed for a different website.")
    return cd["origin"]


def _auth_data(ad: bytes, origin: str) -> dict:
    rp = urlparse(origin).hostname or ""
    if len(ad) < 37 or not hmac.compare_digest(ad[:32], hashlib.sha256(rp.encode()).digest()):
        raise PasskeyError("wrong_site", "The device signed for a different website.")
    flags = ad[32]
    if not flags & 0x01:
        raise PasskeyError("no_person", "The device did not confirm that a person was there.")
    return {"rp_id": rp, "user_present": True, "user_verified": bool(flags & 0x04), "counter": int.from_bytes(ad[33:37], "big")}


def _load_key(spki: bytes, alg: int):
    try:
        key = serialization.load_der_public_key(spki)
    except Exception:
        raise PasskeyError("bad_key", "The device's public key could not be read.")
    if alg == -7 and isinstance(key, ec.EllipticCurvePublicKey) and key.curve.name == "secp256r1":
        return key
    if alg == -257 and isinstance(key, rsa.RSAPublicKey) and key.key_size >= 2048:
        return key
    raise PasskeyError("bad_key", "This kind of device key is not supported.")


def _verify(key, alg: int, sig: bytes, data: bytes) -> bool:
    try:
        if alg == -7:
            key.verify(sig, data, ec.ECDSA(hashes.SHA256()))
        else:
            key.verify(sig, data, padding.PKCS1v15(), hashes.SHA256())
        return True
    except InvalidSignature:
        return False


# ------------------------------------------------------------------ enrolment
def enrol_token(slug: str, exp: int | None = None) -> str:
    exp = exp or int(time.time()) + ENROL_TTL
    mac = hmac.new((OPERATOR_KEY or "dev").encode(), f"enrol:{slug}:{exp}".encode(), hashlib.sha256).hexdigest()[:24]
    return f"{slug}.{exp}.{mac}"


def enrol_slug(token: str) -> str | None:
    try:
        slug, exp, _ = token.rsplit(".", 2)
        if int(exp) < time.time() or not hmac.compare_digest(token, enrol_token(slug, int(exp))):
            return None
        return slug
    except ValueError:
        return None


def enrol_begin(store, slug: str, account_name: str, request: Request) -> dict:
    ch = b64u(secrets.token_bytes(32))
    store.put("passkey_challenge:" + ch, {"slug": slug, "exp": time.time() + 300})
    host = (request.headers.get("host", "").split(":")[0]) if request.headers.get("host", "").split(":")[0] == "localhost" else urlparse(PUBLIC_URL).hostname
    return {"challenge": ch, "rp": {"id": host, "name": "Aimo Agent Gateway"},
            "user": {"id": b64u(hashlib.sha256(("approver:" + slug).encode()).digest()[:16] + secrets.token_bytes(8)), "name": f"Approver, {account_name}", "displayName": f"Approver, {account_name}"},
            "pubKeyCredParams": [{"type": "public-key", "alg": a} for a in ALGS], "exclude": [d["id"] for d in devices(store, slug)], "account": account_name}


def enrol_finish(store, slug: str, customer, body: dict, request: Request) -> dict:
    ch = str(body.get("challenge") or "")
    pend = store.get("passkey_challenge:" + ch)
    if not pend or pend.get("slug") != slug or pend.get("exp", 0) < time.time():
        raise PasskeyError("expired", "That took too long. Start again.")
    store.put("passkey_challenge:" + ch, None)
    try:
        cdj, ad, spki, alg, cred = b64d(body["clientDataJSON"]), b64d(body["authenticatorData"]), b64d(body["publicKey"]), int(body["alg"]), str(body["id"])
    except (KeyError, ValueError, TypeError):
        raise PasskeyError("bad_device_data", "The device's answer could not be read.")
    origin = _client(cdj, "webauthn.create", ch, origins(request))
    info = _auth_data(ad, origin)
    _load_key(spki, alg)
    devs = devices(store, slug)
    if any(d["id"] == cred for d in devs):
        raise PasskeyError("already", "This device is already enrolled.", 409)
    if len(devs) >= MAX_DEVICES:
        raise PasskeyError("too_many", f"An account can have {MAX_DEVICES} approver devices. Remove one first.", 409)
    dev = {"id": cred, "public_key": b64u(spki), "alg": alg, "rp_id": info["rp_id"], "name": (str(body.get("name") or "").strip() or "Approver's device")[:40],
           "enrolled_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "thumbprint": hashlib.sha256(spki).hexdigest()[:16], "user_verified": info["user_verified"]}
    store.put("passkeys:" + slug, devs + [dev])
    store.log(None, "human:approver", "approver_device_enrolled", f"An approver's device was enrolled for this account ({dev['name']}, key {dev['thumbprint']}). From now on an approval needs a signature from an enrolled device.",
              {"device": public(dev), "public_key": dev["public_key"]}, acct=customer)
    return public(dev)


def remove(store, slug: str, customer, cred: str | None) -> int:
    devs = devices(store, slug)
    keep = [d for d in devs if cred and d["id"] != cred]
    store.put("passkeys:" + slug, keep)
    for d in devs:
        if d not in keep:
            store.log(None, "human:approver", "approver_device_removed", f"An approver's device was removed from this account ({d['name']}, key {d['thumbprint']}).", {"device": public(d)}, acct=customer)
    return len(devs) - len(keep)


# ------------------------------------------------------------------- approval
def verify_decision(store, slug: str, dec: dict, dev: dict, request: Request) -> dict:
    """Check a device's signature over one decision. Returns the record kept with the approval, or raises."""
    try:
        cred, ad, cdj, sig = str(dev["id"]), b64d(dev["authenticatorData"]), b64d(dev["clientDataJSON"]), b64d(dev["signature"])
    except (KeyError, ValueError, TypeError):
        raise PasskeyError("bad_device_data", "The device's answer could not be read.")
    known = next((d for d in devices(store, slug) if d["id"] == cred), None)
    if not known:
        raise PasskeyError("unknown_device", "This device is not enrolled as an approver for this account.", 403)
    origin = _client(cdj, "webauthn.get", challenge_of(dec), origins(request))
    info = _auth_data(ad, origin)
    if not _verify(_load_key(b64d(known["public_key"]), known["alg"]), known["alg"], sig, ad + hashlib.sha256(cdj).digest()):
        raise PasskeyError("bad_signature", "The device's signature does not match this decision.", 403)
    return {"method": "passkey", "credential": cred, "device": known["name"], "alg": ALGS[known["alg"]], "public_key": known["public_key"], "thumbprint": known["thumbprint"], "enrolled_at": known["enrolled_at"],
            "user_verified": info["user_verified"], "signed": dec, "authenticator_data": b64u(ad), "client_data_json": b64u(cdj), "signature": b64u(sig)}


def check(g, cid: str, token: str | None, approve: bool, by: str, note: str, dev: dict | None, request: Request) -> dict | None:
    """Called before a decision is recorded. None: this account approves by link. A record: the device signature to keep."""
    from .service import GatewayError
    c = g._load(cid)
    if not secrets.compare_digest(token or "", c["_internal"]["approval_token"]):
        raise GatewayError(403, "forbidden", "This approval link is not valid.")
    slug = (g.account_of(c["_internal"].get("customer")) or {}).get("slug")
    if not slug or not devices(g.store, slug):
        return None
    if not dev:
        if not approve:
            return None  # anyone holding the link may say no
        raise GatewayError(403, "device_required", "Approvals for this account are signed on an enrolled approver's device. Open this link on that device.")
    g.get_checkout(cid)  # the same refresh the decision itself does, so both see the same digest
    c = g._load(cid)
    try:
        return verify_decision(g.store, slug, decision_of(cid, c["_internal"]["digest"], approve, by, note), dev, request)
    except PasskeyError as e:
        raise GatewayError(e.status, e.code, e.message)


def prepare(g, cid: str, approve: bool, by: str, note: str, request: Request) -> dict:
    """What an approver's device is asked to sign for one decision, and which enrolled devices may sign it."""
    g.get_checkout(cid)  # the same refresh the decision itself does, so both see the same digest
    c = g._load(cid)
    slug = (g.account_of(c["_internal"].get("customer")) or {}).get("slug") or ""
    devs = devices(g.store, slug)
    dec = decision_of(cid, c["_internal"]["digest"], approve, by, note)
    host = request.headers.get("host", "").split(":")[0]
    return {"approvals": "passkey" if devs else "link", "decision": dec, "challenge": challenge_of(dec), "rp_id": host if host == "localhost" else urlparse(PUBLIC_URL).hostname,
            "allow": [d["id"] for d in devs], "account": slug}


def build(g, ctx, page, cookie: str) -> APIRouter:
    from .service import GatewayError
    r = APIRouter()

    def peek(request: Request) -> tuple[str | None, bool]:
        """(account of this browser's session, holds the operator key). Never creates a session."""
        op = bool(OPERATOR_KEY) and hmac.compare_digest(request.headers.get("x-operator-key", ""), OPERATOR_KEY)
        s = g.store.get("session:" + request.cookies.get(cookie, "")) or {}
        return s.get("slug"), op or bool(s.get("operator"))

    def may_enrol(request: Request, slug: str, token: str = "") -> bool:
        mine, op = peek(request)
        return op or mine == slug or enrol_slug(token) == slug

    def slug_for(request: Request, t: str = "") -> str | None:
        """The account a request is about: the enrolment link's, else this browser's own. Never makes a new account."""
        if t:
            return enrol_slug(t)
        mine, op = peek(request)
        return ctx(request)["acct"]["slug"] if op else mine

    def wrap(fn):
        try:
            return fn()
        except PasskeyError as e:
            raise GatewayError(e.status, e.code, e.message)

    @r.get("/enrol", include_in_schema=False)
    def enrol_page():
        return page("enrol.html")

    @r.get("/api/passkey", include_in_schema=False)
    def state(request: Request, t: str = ""):
        """The account's approver devices. With an enrolment link (t) it answers for that link's account."""
        slug = slug_for(request, t)
        if t and (not slug or not g.account(slug)):
            raise GatewayError(403, "expired", "This enrolment link has expired. Make a new one on the account's page.")
        if not slug or not g.account(slug):
            return {"account": None, "devices": [], "approvals": "link", "can_enrol": False}
        a = g.account(slug)
        devs = devices(g.store, slug)
        out = {"account": a["name"], "devices": [public(d) for d in devs], "approvals": "passkey" if devs else "link", "can_enrol": may_enrol(request, slug, t)}
        if not t and out["can_enrol"]:
            out["enrol_url"] = f"{PUBLIC_URL}/enrol?t={enrol_token(slug)}"
            out["enrol_minutes"] = ENROL_TTL // 60
        return out

    @r.post("/api/passkey/enrol/begin", include_in_schema=False)
    def begin(request: Request, body: dict = Body(default={})):
        t = str(body.get("t") or "")
        slug = slug_for(request, t)
        if not slug or not g.account(slug) or not may_enrol(request, slug, t):
            raise GatewayError(403, "forbidden", "Only the account itself can enrol an approver's device. Use the account's own page or its enrolment link.")
        return enrol_begin(g.store, slug, g.account(slug)["name"], request)

    @r.post("/api/passkey/enrol/finish", include_in_schema=False)
    def finish(request: Request, body: dict = Body(...)):
        t = str(body.get("t") or "")
        slug = slug_for(request, t)
        if not slug or not g.account(slug) or not may_enrol(request, slug, t):
            raise GatewayError(403, "forbidden", "Only the account itself can enrol an approver's device.")
        return wrap(lambda: {"device": enrol_finish(g.store, slug, g.account(slug)["customer"], body, request), "approvals": "passkey"})

    @r.post("/api/passkey/remove", include_in_schema=False)
    def remove_device(request: Request, body: dict = Body(default={})):
        slug = slug_for(request)
        if not slug:
            raise GatewayError(403, "forbidden", "Only the account itself can remove an approver's device.")
        return {"removed": remove(g.store, slug, g.account(slug)["customer"], body.get("id")), "approvals": "passkey" if devices(g.store, slug) else "link"}

    @r.post("/api/approve/{cid}/device", include_in_schema=False)
    def approve_begin(cid: str, request: Request, body: dict = Body(...)):
        """What the approver's device is asked to sign for this decision, and which enrolled devices may sign it."""
        c = g._load(cid)
        if not secrets.compare_digest(str(body.get("k") or ""), c["_internal"]["approval_token"]):
            raise GatewayError(403, "forbidden", "This approval link is not valid.")
        pre = prepare(g, cid, bool(body.get("approve")), str(body.get("approver") or "F&B manager")[:60], str(body.get("note") or "")[:300], request)
        slug = pre.pop("account")
        return {**pre, "can_enrol": bool(slug) and may_enrol(request, slug)}

    return r
