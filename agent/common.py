"""The buying company's side of the demo: its agent's key, its mandate, and its checks on what the seller says.

Nothing here is trusted by the gateway. It only reaches Valio Aimo through the public REST / MCP interface.
"""
import json
from datetime import datetime, timedelta

import httpx

from gateway.config import TZ
from gateway.signing import Key, b64d, jwt_encode, jwt_peek, now, verify_detached


def next_monday() -> str:
    d = datetime.now(TZ).date()
    return (d + timedelta(days=(7 - d.weekday()) % 7 or 7)).isoformat()


def eur(cents) -> str:
    return f"€{(cents or 0) / 100:,.2f}"


class BuyerSide:
    def __init__(self, base: str, run: dict):
        self.base, self.run = base.rstrip("/"), run
        self.key = Key.load(run.get("_key") or "demo_buyer_agent")  # the agent's own key; the mandate is bound to it
        self.http = httpx.AsyncClient(base_url=self.base, timeout=90)
        self.mandate = None
        self.seller_keys = {}

    async def setup(self):
        # Demo stand-in: in production the buyer's own system signs the mandate, not the seller's server.
        self.mandate = self.run.get("_mandate")
        if not self.mandate:  # run outside the console: use the stage account's mandate
            from gateway.app import g
            self.mandate = g.issue_mandate()
        prof = (await self.http.get("/.well-known/ucp")).json()
        self.seller_keys = {k["kid"]: k for k in prof["keys"]}
        _, c = jwt_peek(self.mandate)
        b = c["mandate"]["budget"]
        self.step("check", "Loaded mandate and Valio Aimo's public key",
                  f"Mandate {c['jti']}: up to {eur(b['max_per_order'])} per order and {eur(b['max_per_week'])} per week without a person. Seller key {next(iter(self.seller_keys))[:12]}… from /.well-known/ucp.")

    def headers(self, mandate=True) -> dict:
        return {**({"Authorization": f"Bearer {self.mandate}"} if mandate and self.mandate else {}), "X-Trace-Id": self.run["trace"]}

    def step(self, kind, title, text="", data=None):
        self.run["steps"].append({"t": datetime.now(TZ).strftime("%H:%M:%S"), "kind": kind, "title": title, "text": text, **({"data": data} if data is not None else {})})

    def _jwk(self, jws):
        try:
            return self.seller_keys.get(json.loads(b64d(jws.split(".")[0]))["kid"])
        except Exception:
            return None

    def verified(self, doc: dict) -> bool | None:
        """Check Valio Aimo's signature on a response. None = nothing to check."""
        if isinstance(doc.get("ap2"), dict) and doc["ap2"].get("merchant_authorization"):
            jws, body = doc["ap2"]["merchant_authorization"], {k: v for k, v in doc.items() if k != "ap2"}
        elif isinstance(doc.get("proof"), dict) and doc["proof"].get("jws"):
            jws, body = doc["proof"]["jws"], {k: v for k, v in doc.items() if k != "proof"}
        else:
            return None
        jwk = self._jwk(jws)
        return bool(jwk and verify_detached(body, jws, jwk))

    def accept(self, checkout: dict, digest: str | None = None) -> str:
        """Sign the exact terms being accepted, with the key the mandate is bound to."""
        _, c = jwt_peek(self.mandate)
        return jwt_encode({"iss": c["sub"], "checkout_id": checkout["id"], "checkout_digest": digest or checkout["ap2"]["checkout_digest"],
                           "merchant_authorization": checkout["ap2"]["merchant_authorization"], "mandate_jti": c["jti"], "iat": now()}, self.key, typ="checkout-mandate+jwt")

    async def post(self, path, body=None, mandate=True, headers=None):
        r = await self.http.post(path, json=body or {}, headers=headers or self.headers(mandate))
        return r.json()

    async def get(self, path):
        return (await self.http.get(path, headers={**self.headers(), "Accept": "application/json"})).json()

    async def close(self):
        await self.http.aclose()


def errors(doc: dict) -> list[str]:
    return [f"{m.get('code')}: {m.get('content')}" for m in doc.get("messages") or [] if m.get("type") == "error"]


def describe(doc: dict) -> str:
    """One line for the run log about any gateway response."""
    if "options" in doc:
        return " | ".join(f"{o['label']}: {eur(o['total_ex_vat'])}, {o['covers_pct']:g}% covered, {o['authority']['decision'].replace('_', ' ')}" for o in doc["options"]) or "No option."
    if "products" in doc:
        return f"{len(doc['products'])} product(s): " + "; ".join(p["title"] for p in doc["products"][:4])
    if doc.get("id", "").startswith("chk_"):
        sub = next((t["amount"] for t in doc.get("totals", []) if t["type"] == "subtotal"), 0)
        line = f"Checkout {doc['status'].replace('_', ' ')}: {eur(sub)} ex VAT."
        if doc.get("order"):
            amends = (doc.get("wholesale") or {}).get("amends")
            line += f" Order {amends['order_label']} changed." if amends and not doc.get("line_items") else f" Order {doc['order']['label']} placed."
        errs = errors(doc)
        return line + (" " + " ".join(errs) if errs else "")
    if doc.get("id", "").startswith("ord_"):
        return f"Order {doc['label']}."
    return "; ".join(errors(doc)) or "OK"
