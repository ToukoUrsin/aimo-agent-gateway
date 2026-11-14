"""A person's own device signs the approval.

Two kinds of key, and the record always says which one signed:

* a passkey (WebAuthn): made inside the phone or laptop, used after its fingerprint, face or screen lock;
* a browser key: where a browser has no such lock, it makes its own ES256 key with WebCrypto, marked non-extractable
  and kept in that browser.  No fingerprint is involved, and the record says so.

Either way the private half never reaches the gateway, which keeps only the public half, so nobody else can produce
that approval: not the agent, not Valio Aimo, not this server.  What the device signs is the decision itself (which
checkout, its digest, approved or rejected, by whom, the note, and each product and allergen it clears), so the
signature cannot be moved to another order, flipped from reject to approve, or stretched to a line nobody ticked.

Enrolling a device is the buyer's act: from the account's own session, or through an enrolment link the account made.
One more way exists for the very first device only: the approval link the gateway sent to the approver may make the
device it is opened on the account's approver, while the account has none and that request is still open.  After that
first device, an approval link alone neither approves nor enrols.  In production the buyer's company sign-in would
gate enrolment; here the demo account's own browser session does.
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
from cryptography.hazmat.primitives.asymmetric.utils import encode_dss_signature
from fastapi import APIRouter, Body, Request

from .config import OPERATOR_KEY, PUBLIC_URL
from .signing import b64d, b64u, jcs

ALGS = {-7: "ES256", -257: "RS256"}
ENROL_TTL = 15 * 60
MAX_DEVICES = 5
# What signed, in the words the pages and the record use.
HOW = {"passkey": "a passkey", "browser_key": "a key held in the browser, no fingerprint"}
FIRST_BY_LINK = ("sandbox", "stage")  # account kinds whose approval link may make the first approver device (not the simulated customers: a script approves for them)


class PasskeyError(Exception):
    def __init__(self, code, message, status=400):
        super().__init__(message)
        self.code, self.message, self.status = code, message, status


def devices(store, slug: str) -> list[dict]:
    return store.get("passkeys:" + slug) or []


def public(d: dict) -> dict:
    """What a page may show about an enrolled device."""
    kind = d.get("kind", "passkey")
    return {"id": d["id"], "name": d["name"], "enrolled_at": d["enrolled_at"], "alg": d["alg"], "thumbprint": d["thumbprint"], "kind": kind, "how": HOW[kind]}


def decision_of(cid: str, digest: str, approve: bool, by: str, note: str, clears: list | None = None) -> dict:
    """What a device signs. clears: each product with no allergen declaration this approval clears, and for which allergens."""
    return {"checkout": cid, "checkout_digest": digest, "decision": "approved" if approve else "rejected", "by": by, "note": note, "clears": (clears or []) if approve else []}


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


def _verify(key, alg: int, sig: bytes, data: bytes, raw: bool = False) -> bool:
    try:
        if raw:  # WebCrypto gives r||s; the verifier wants DER
            if len(sig) != 64:
                return False
            sig = encode_dss_signature(int.from_bytes(sig[:32], "big"), int.from_bytes(sig[32:], "big"))
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


VIA = {"account": "from the account's own session", "enrol_link": "through the account's enrolment link", "approval_link": "from the approval link, as the account's first device"}


def enrol_finish(store, slug: str, customer, body: dict, request: Request, via: str = "account") -> dict:
    ch = str(body.get("challenge") or "")
    pend = store.get("passkey_challenge:" + ch)
    if not pend or pend.get("slug") != slug or pend.get("exp", 0) < time.time():
        raise PasskeyError("expired", "That took too long. Start again.")
    store.put("passkey_challenge:" + ch, None)
    kind = "browser_key" if body.get("kind") == "browser_key" else "passkey"
    try:
        if kind == "browser_key":
            # A key the browser made itself. It proves it holds the private half by signing this enrolment's challenge.
            cdj, spki, sig, alg = b64d(body["clientDataJSON"]), b64d(body["publicKey"]), b64d(body["signature"]), -7
            cred = "bk-" + hashlib.sha256(spki).hexdigest()[:32]
        else:
            cdj, ad, spki, alg, cred = b64d(body["clientDataJSON"]), b64d(body["authenticatorData"]), b64d(body["publicKey"]), int(body["alg"]), str(body["id"])
    except (KeyError, ValueError, TypeError):
        raise PasskeyError("bad_device_data", "The device's answer could not be read.")
    if kind == "browser_key":
        origin = _client(cdj, "aimo.key.create", ch, origins(request))
        if not _verify(_load_key(spki, alg), alg, sig, cdj, raw=True):
            raise PasskeyError("bad_signature", "The browser did not prove it holds this key.", 403)
        info = {"rp_id": urlparse(origin).hostname or "", "user_verified": False}
    else:
        origin = _client(cdj, "webauthn.create", ch, origins(request))
        info = _auth_data(ad, origin)
        _load_key(spki, alg)
    devs = devices(store, slug)
    if any(d["id"] == cred for d in devs):
        raise PasskeyError("already", "This device is already enrolled.", 409)
    if len(devs) >= MAX_DEVICES:
        raise PasskeyError("too_many", f"An account can have {MAX_DEVICES} approver devices. Remove one first.", 409)
    dev = {"id": cred, "public_key": b64u(spki), "alg": alg, "rp_id": info["rp_id"], "name": (str(body.get("name") or "").strip() or "Approver's device")[:40],
           "enrolled_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "thumbprint": hashlib.sha256(spki).hexdigest()[:16], "user_verified": info["user_verified"], "kind": kind, "enrolled_by": via}
    store.put("passkeys:" + slug, devs + [dev])
    store.log(None, "human:approver", "approver_device_enrolled", f"An approver's device was enrolled for this account ({dev['name']}, {HOW[kind]}, key {dev['thumbprint']}), {VIA[via]}. From now on an approval needs a signature from an enrolled device.",
              {"device": public(dev), "public_key": dev["public_key"], "enrolled_by": via}, acct=customer)
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
        cred, cdj, sig = str(dev["id"]), b64d(dev["clientDataJSON"]), b64d(dev["signature"])
    except (KeyError, ValueError, TypeError):
        raise PasskeyError("bad_device_data", "The device's answer could not be read.")
    known = next((d for d in devices(store, slug) if d["id"] == cred), None)
    if not known:
        raise PasskeyError("unknown_device", "This device is not enrolled as an approver for this account.", 403)
    if known.get("kind") == "browser_key":
        # The browser's own key signs a small statement that names this decision's digest and the site.
        _client(cdj, "aimo.key.get", challenge_of(dec), origins(request))
        if not _verify(_load_key(b64d(known["public_key"]), known["alg"]), known["alg"], sig, cdj, raw=True):
            raise PasskeyError("bad_signature", "The device's signature does not match this decision.", 403)
        return {"method": "browser_key", "how": HOW["browser_key"], "credential": cred, "device": known["name"], "alg": ALGS[known["alg"]], "public_key": known["public_key"], "thumbprint": known["thumbprint"],
                "enrolled_at": known["enrolled_at"], "user_verified": False, "signed": dec, "client_data_json": b64u(cdj), "signature": b64u(sig)}
    try:
        ad = b64d(dev["authenticatorData"])
    except (KeyError, ValueError, TypeError):
        raise PasskeyError("bad_device_data", "The device's answer could not be read.")
    origin = _client(cdj, "webauthn.get", challenge_of(dec), origins(request))
    info = _auth_data(ad, origin)
    if not _verify(_load_key(b64d(known["public_key"]), known["alg"]), known["alg"], sig, ad + hashlib.sha256(cdj).digest()):
        raise PasskeyError("bad_signature", "The device's signature does not match this decision.", 403)
    return {"method": "passkey", "how": HOW["passkey"], "credential": cred, "device": known["name"], "alg": ALGS[known["alg"]], "public_key": known["public_key"], "thumbprint": known["thumbprint"], "enrolled_at": known["enrolled_at"],
            "user_verified": info["user_verified"], "signed": dec, "authenticator_data": b64u(ad), "client_data_json": b64u(cdj), "signature": b64u(sig)}


def check(g, cid: str, token: str | None, approve: bool, by: str, note: str, dev: dict | None, request: Request, clears: list | None = None) -> dict | None:
    """Called before a decision is recorded. None: a rejection, or a simulated customer whose approver is a script. A record: the device signature to keep.

    On a visitor's account and the stage account a yes always carries a signature from the approver's device, the first one included:
    the approval page enrols the device it is opened on and signs there. The link alone can only say no.

    clears: the lines with no allergen declaration the approver ticked. A device-signed approval must name exactly the
    lines the allergen rule asked about, so a yes never covers a line nobody looked at."""
    from .service import GatewayError
    c = g._load(cid)
    if not secrets.compare_digest(token or "", c["_internal"]["approval_token"]):
        raise GatewayError(403, "forbidden", "This approval link is not valid.")
    a = g.account_of(c["_internal"].get("customer")) or {}
    slug = a.get("slug")
    enrolled = bool(slug) and bool(devices(g.store, slug))
    if not slug or (not enrolled and a.get("kind") not in FIRST_BY_LINK):
        return None  # a simulated customer: a script stands in for its approver, and the record says so
    if not dev:
        if not approve:
            return None  # anyone holding the link may say no
        raise GatewayError(403, "device_required", "Approvals for this account are signed on an enrolled approver's device. Open this link on that device." if enrolled else
                           "An approval for this account is signed on the approver's own device. Open this link in a browser: the first device to approve becomes the account's approver device. The link alone cannot approve.")
    g.get_checkout(cid)  # the same refresh the decision itself does, so both see the same digest
    c = g._load(cid)
    canon = g.check_clears(c, clears or []) if approve else []
    try:
        return verify_decision(g.store, slug, decision_of(cid, c["_internal"]["digest"], approve, by, note, canon), dev, request)
    except PasskeyError as e:
        raise GatewayError(e.status, e.code, e.message)


def prepare(g, cid: str, approve: bool, by: str, note: str, request: Request, clears: list | None = None) -> dict:
    """What an approver's device is asked to sign for one decision, and which enrolled devices may sign it.

    clears=None only asks how this account approves; a list is checked against the lines the allergen rule asked about."""
    g.get_checkout(cid)  # the same refresh the decision itself does, so both see the same digest
    c = g._load(cid)
    slug = (g.account_of(c["_internal"].get("customer")) or {}).get("slug") or ""
    devs = devices(g.store, slug)
    canon = g.check_clears(c, clears) if approve and clears is not None else []
    dec = decision_of(cid, c["_internal"]["digest"], approve, by, note, canon)
    host = request.headers.get("host", "").split(":")[0]
    is_bk = lambda d: d.get("kind") == "browser_key"
    kind = (g.account_of(c["_internal"].get("customer")) or {}).get("kind")
    return {"approvals": "device" if devs else "link", "link_ok": not devs and kind not in FIRST_BY_LINK, "decision": dec, "challenge": challenge_of(dec), "rp_id": host if host == "localhost" else urlparse(PUBLIC_URL).hostname,
            "allow": [d["id"] for d in devs if not is_bk(d)], "keys": [d["id"] for d in devs if is_bk(d)], "devices": [public(d) for d in devs], "to_clear": g.clears_of(c), "account": slug}


def link_account(g, cid: str, k: str) -> str | None:
    """The account whose first approver device this approval link may make: none enrolled yet, and the request still open."""
    c = g.store.checkout(str(cid or ""))
    if not c or not k or not secrets.compare_digest(str(k), c["_internal"]["approval_token"]) or c["status"] != "requires_escalation":
        return None
    a = g.account_of(c["_internal"].get("customer"))
    if not a or a.get("kind") not in FIRST_BY_LINK or devices(g.store, a["slug"]):
        return None
    return a["slug"]


def last_asked(g, customer) -> dict | None:
    """The account's latest request a person was asked about: one still open, else the last one approved. For the 'try the link without the device' action."""
    rows = g.store.checkouts(limit=40, customer=customer)
    pick = next((c for c in rows if c["status"] == "requires_escalation"), None) or next((c for c in rows if ((c.get("wholesale") or {}).get("approval") or {}).get("decision") == "approved"), None)
    if not pick:
        return None
    return {"checkout": pick["id"], "k": pick["_internal"]["approval_token"], "status": pick["status"], "url": f"/approve/{pick['id']}?k={pick['_internal']['approval_token']}"}


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

    def enrolling(request: Request, body: dict) -> tuple[str | None, str]:
        """(the account a device is being enrolled for, what allows it). (None, "") when nothing allows it. Never makes a new account."""
        t, cid, k = str(body.get("t") or ""), str(body.get("cid") or ""), str(body.get("k") or "")
        if t:
            return enrol_slug(t), "enrol_link"
        if cid and k:  # from an approval page: the account is the one that request belongs to
            c = g.store.checkout(cid)
            a = g.account_of(c["_internal"].get("customer")) if c and secrets.compare_digest(k, c["_internal"]["approval_token"]) else None
            if a and may_enrol(request, a["slug"]):
                return a["slug"], "account"
            if a and link_account(g, cid, k) == a["slug"]:
                return a["slug"], "approval_link"
            return None, ""
        return slug_for(request), "account"

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
        out = {"account": a["name"], "devices": [public(d) for d in devs], "approvals": "device" if devs else "link", "can_enrol": may_enrol(request, slug, t)}
        if not t and out["can_enrol"]:
            out["enrol_url"] = f"{PUBLIC_URL}/enrol?t={enrol_token(slug)}"
            out["enrol_minutes"] = ENROL_TTL // 60
            out["last_asked"] = last_asked(g, a["customer"])  # the account's own page may show the gateway refusing this link without the device
        return out

    @r.post("/api/passkey/enrol/begin", include_in_schema=False)
    def begin(request: Request, body: dict = Body(default={})):
        slug, _ = enrolling(request, body)
        if not slug or not g.account(slug):
            raise GatewayError(403, "forbidden", "Only the account itself can enrol an approver's device. Use the account's own page or its enrolment link.")
        return enrol_begin(g.store, slug, g.account(slug)["name"], request)

    @r.post("/api/passkey/enrol/finish", include_in_schema=False)
    def finish(request: Request, body: dict = Body(...)):
        slug, via = enrolling(request, body)
        if not slug or not g.account(slug):
            raise GatewayError(403, "forbidden", "Only the account itself can enrol an approver's device.")
        return wrap(lambda: {"device": enrol_finish(g.store, slug, g.account(slug)["customer"], body, request, via), "approvals": "device"})

    @r.post("/api/passkey/remove", include_in_schema=False)
    def remove_device(request: Request, body: dict = Body(default={})):
        slug = slug_for(request)
        if not slug:
            raise GatewayError(403, "forbidden", "Only the account itself can remove an approver's device.")
        return {"removed": remove(g.store, slug, g.account(slug)["customer"], body.get("id")), "approvals": "device" if devices(g.store, slug) else "link"}

    @r.post("/api/approve/{cid}/device", include_in_schema=False)
    def approve_begin(cid: str, request: Request, body: dict = Body(...)):
        """What the approver's device is asked to sign for this decision, and which enrolled devices may sign it."""
        c = g._load(cid)
        if not secrets.compare_digest(str(body.get("k") or ""), c["_internal"]["approval_token"]):
            raise GatewayError(403, "forbidden", "This approval link is not valid.")
        clears = body.get("clears") if isinstance(body.get("clears"), list) else None
        pre = prepare(g, cid, bool(body.get("approve")), str(body.get("approver") or "").strip()[:60], str(body.get("note") or "")[:300], request, clears)
        slug = pre.pop("account")
        a = g.account(slug) if slug else None
        first = bool(slug) and link_account(g, cid, str(body.get("k") or "")) == slug
        return {**pre, "can_enrol": bool(slug) and (may_enrol(request, slug) or first), "account_name": (a or {}).get("name"), "account_kind": (a or {}).get("kind")}

    return r
