"""ES256 keys, JCS (RFC 8785) canonical JSON, detached JWS (RFC 7515 App. F), compact JWT.

Same construction UCP uses for `ap2.merchant_authorization`: the signature covers
base64url(header) + "." + base64url(JCS(payload)) and is shipped as `header..signature`.
"""
import base64
import hashlib
import json
import time

import rfc8785
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec, utils

from .config import DATA

KEYS = DATA / "keys"


def b64u(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


def b64d(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def jcs(obj) -> bytes:
    return rfc8785.dumps(obj)


def digest(obj) -> str:
    """sha-256 of the canonical JSON, hex."""
    return hashlib.sha256(jcs(obj)).hexdigest()


def public_jwk(pub) -> dict:
    n = pub.public_numbers()
    x, y = b64u(n.x.to_bytes(32, "big")), b64u(n.y.to_bytes(32, "big"))
    # kid = RFC 7638 JWK thumbprint, as UCP requires for dual-audience keys
    thumb = json.dumps({"crv": "P-256", "kty": "EC", "x": x, "y": y}, separators=(",", ":"))
    kid = b64u(hashlib.sha256(thumb.encode()).digest())
    return {"kty": "EC", "crv": "P-256", "x": x, "y": y, "kid": kid, "alg": "ES256", "use": "sig"}


class Key:
    def __init__(self, priv):
        self.priv = priv
        self.jwk = public_jwk(priv.public_key())
        self.kid = self.jwk["kid"]

    @classmethod
    def load(cls, name: str) -> "Key":
        KEYS.mkdir(parents=True, exist_ok=True)
        path = KEYS / f"{name}.pem"
        if path.exists():
            priv = serialization.load_pem_private_key(path.read_bytes(), None)
        else:
            priv = ec.generate_private_key(ec.SECP256R1())
            path.write_bytes(
                priv.private_bytes(
                    serialization.Encoding.PEM,
                    serialization.PrivateFormat.PKCS8,
                    serialization.NoEncryption(),
                )
            )
            path.chmod(0o600)
        return cls(priv)

    def sign(self, data: bytes) -> bytes:
        der = self.priv.sign(data, ec.ECDSA(hashes.SHA256()))
        r, s = utils.decode_dss_signature(der)
        return r.to_bytes(32, "big") + s.to_bytes(32, "big")


def _verify(jwk: dict, data: bytes, sig: bytes) -> bool:
    try:
        pub = ec.EllipticCurvePublicNumbers(
            int.from_bytes(b64d(jwk["x"]), "big"), int.from_bytes(b64d(jwk["y"]), "big"), ec.SECP256R1()
        ).public_key()
        der = utils.encode_dss_signature(int.from_bytes(sig[:32], "big"), int.from_bytes(sig[32:], "big"))
        pub.verify(der, data, ec.ECDSA(hashes.SHA256()))
        return True
    except (InvalidSignature, ValueError, KeyError):
        return False


def _header(key: Key, **extra) -> str:
    return b64u(json.dumps({"alg": "ES256", "kid": key.kid, **extra}, separators=(",", ":")).encode())


def sign_detached(obj, key: Key) -> str:
    h = _header(key)
    sig = key.sign(f"{h}.{b64u(jcs(obj))}".encode())
    return f"{h}..{b64u(sig)}"


def verify_detached(obj, jws: str, jwk: dict) -> bool:
    try:
        h, empty, s = jws.split(".")
        if empty != "":
            return False
        if json.loads(b64d(h)).get("kid") != jwk.get("kid"):
            return False
        return _verify(jwk, f"{h}.{b64u(jcs(obj))}".encode(), b64d(s))
    except Exception:
        return False


def jwt_encode(claims: dict, key: Key, typ: str = "JWT") -> str:
    h = _header(key, typ=typ)
    p = b64u(json.dumps(claims, separators=(",", ":"), ensure_ascii=False).encode())
    return f"{h}.{p}.{b64u(key.sign(f'{h}.{p}'.encode()))}"


def jwt_peek(token: str) -> tuple[dict, dict]:
    h, p, _ = token.split(".")
    return json.loads(b64d(h)), json.loads(b64d(p))


def jwt_verify(token: str, jwk: dict) -> dict | None:
    """Returns the claims if the signature is valid for this key, else None. Does not check exp."""
    try:
        h, p, s = token.split(".")
        if not _verify(jwk, f"{h}.{p}".encode(), b64d(s)):
            return None
        return json.loads(b64d(p))
    except Exception:
        return None


def now() -> int:
    return int(time.time())
