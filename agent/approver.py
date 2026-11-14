"""A stand-in approver for tests, with a signing key of its own.

On a visitor's account or the stage account the gateway takes a yes only with a signature from the approver's own
device, the first one included. A test has no person and no phone, so this script holds a P-256 key the way a browser
keeps one: it enrols the key from the first approval link and signs every decision with it. The record then says what
is true, a key with no fingerprint behind it, under the device name given here.

    ap = KeyApprover(public_url)
    await ap.approve(http, checkout_id, link_key, "F&B manager")      # httpx.AsyncClient
    ap.approve_sync(post, checkout_id, link_key, "Test approver")     # post(path, json) -> dict
"""
import base64
import hashlib
import json

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec, utils


def b64u(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


class KeyApprover:
    def __init__(self, origin: str, name: str = "Approver script (test), its own key"):
        self.origin, self.name = origin.rstrip("/"), name
        self.key = ec.generate_private_key(ec.SECP256R1())
        self.spki = self.key.public_key().public_bytes(serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo)
        self.id = "bk-" + hashlib.sha256(self.spki).hexdigest()[:32]

    def signed(self, typ: str, challenge: str) -> dict:
        """The small statement a browser key signs: what kind of answer, for which challenge, on which site."""
        cdj = json.dumps({"type": typ, "challenge": challenge, "origin": self.origin}, separators=(",", ":")).encode()
        r, s = utils.decode_dss_signature(self.key.sign(cdj, ec.ECDSA(hashes.SHA256())))
        return {"clientDataJSON": b64u(cdj), "signature": b64u(r.to_bytes(32, "big") + s.to_bytes(32, "big"))}

    def flow(self, cid: str, k: str, by: str, note: str = ""):
        """Yields (path, body) to post and takes each answer back. Returns the gateway's last answer."""
        body = {"k": k, "approve": True, "approver": by, "note": note}
        pre = yield f"/api/approve/{cid}/device", {"k": k, "approve": True}
        if "approvals" not in pre:
            return pre
        if pre.get("link_ok"):  # a simulated customer: its approver is a script and the link decides
            return (yield f"/api/approve/{cid}", body)
        if self.id not in (pre.get("keys") or []):
            b = yield "/api/passkey/enrol/begin", {"cid": cid, "k": k}
            if "challenge" not in b:
                return b
            done = yield "/api/passkey/enrol/finish", {"cid": cid, "k": k, "challenge": b["challenge"], "name": self.name, "kind": "browser_key", "id": self.id,
                                                       "publicKey": b64u(self.spki), **self.signed("aimo.key.create", b["challenge"])}
            if "device" not in done:
                return done
        body["clears"] = [{"id": x["id"], "codes": x["codes"]} for x in pre.get("to_clear") or []]  # each line the allergen rule asked about, named
        pre = yield f"/api/approve/{cid}/device", body
        if "challenge" not in pre:
            return pre
        body["device"] = {"id": self.id, **self.signed("aimo.key.get", pre["challenge"])}
        return (yield f"/api/approve/{cid}", body)

    async def approve(self, http, cid: str, k: str, by: str, note: str = "") -> dict:
        steps = self.flow(cid, k, by, note)
        try:
            path, body = next(steps)
            while True:
                path, body = steps.send((await http.post(path, json=body)).json())
        except StopIteration as e:
            return e.value

    def approve_sync(self, post, cid: str, k: str, by: str, note: str = "") -> dict:
        steps = self.flow(cid, k, by, note)
        try:
            path, body = next(steps)
            while True:
                path, body = steps.send(post(path, body))
        except StopIteration as e:
            return e.value
