"""The agent gateway: catalog lookup, sourcing plan, signed checkout, approval, order.

Transport neutral. REST (app.py) and MCP (mcp_tools.py) both call these methods and
return the same UCP-shaped documents.
"""
import json
import math
import os
import re
import secrets
import threading
import time
from collections import Counter
from datetime import datetime, timedelta

from cryptography.hazmat.primitives.asymmetric import ec

from . import policy
from .aimo import Aimo, AimoError, facts, parse_ts
from .catalog import ALLERGENS, UNITS, Catalog, case_count, declares_allergens, fold, kind, pieces
from .config import CURRENCY, DEMO_CUSTOMER, EXT, FREEZE_NOW, HOLD_MINUTES, PUBLIC_URL, STAGE, TZ, UCP_VERSION, order_url
from .signing import Key, b64d, digest, jwt_encode, jwt_peek, jwt_verify, now, public_jwk, sign_detached
from .store import Store

BUYER_NAME = "Nordic Harbour Hotels"
SEED_HISTORY = {  # what a new demo account has bought before: product id -> orders so far, usual units
    "464201": {"orders": 41, "typical_units": 400},  # Valio semi skimmed milk 1 l lactose free UHT
    "463380": {"orders": 23, "typical_units": 300},  # Arla Cafe milk 1 L UHT lactose free
    "464237": {"orders": 9, "typical_units": 120},   # Valio coffee milk 1 l lactose free UHT
    "473609": {"orders": 17, "typical_units": 96},   # Oddlygood Barista oat drink 1 l
    "469512": {"orders": 6, "typical_units": 60},    # Elovena oat drink barista 1 l
}
STRIKE_LIMIT, STRIKE_WINDOW = 3, 600  # refused attempts inside this many seconds suspend an agent
STRIKE_RULES = {"A1", "A2", "A3"}     # refusals that count as a strike: the agent tried to order what the request rules out
HARVEST_WINDOW = int(os.environ.get("AIMO_HARVEST_WINDOW", "600"))
HARVEST_LIMIT = int(os.environ.get("AIMO_HARVEST_LIMIT", "40"))
CAPS = ["dev.ucp.shopping.catalog.search", "dev.ucp.shopping.catalog.lookup", "dev.ucp.shopping.checkout", "dev.ucp.shopping.order", f"{EXT}.offer", f"{EXT}.sourcing", f"{EXT}.mandate"]
REASONS = {
    "cutoff_passed": "the order cut-off for that delivery date has passed",
    "not_deliverable": "it cannot currently be delivered on any date",
    "sales_block": "it is blocked for sale",
    "out_of_stock": "it is out of stock",
    "later_date_only": "it is only available on a later date",
    "no_price": "no price is available for this customer",
    "no_data": "no availability data",
    "product_recalled": "a batch of it is recalled, so it is blocked for new orders until Valio Aimo closes the recall",
    "short_at_warehouse": "Valio Aimo has reported a short pick on it for this account and day, so there is no more of it to order until that issue is settled (see list_order_issues)",
    "allocation_share_used": "it is on allocation because stock is short, and this account's contract share of it for that day is already taken",
    "delivery_delayed": "Valio Aimo has reported a delay on it for this account, so it cannot come on that day while that issue is open (see list_order_issues)",
}


def iso(ts: float | None = None) -> str:
    return datetime.fromtimestamp(ts or time.time(), TZ).isoformat(timespec="seconds")


def nice_day(day: str) -> str:
    try:
        return datetime.strptime(day, "%Y-%m-%d").strftime("%a %d %b")
    except ValueError:
        return day


def _snap(x: float) -> float:
    r = round(x * 4) / 4
    return r if r > 0 and abs(x - r) / x < 0.03 else round(x, 3)


def _need_key(unit: str) -> str | None:
    u = (unit or "").strip().lower()
    if u in ("l", "ltr", "litre", "liter", "litres", "liters"):
        return "l"
    if u in ("kg", "kgm", "kilogram", "kilograms", "kilo", "kilos"):
        return "kg"
    if u in ("pcs", "pc", "piece", "pieces", "kpl", "each", "egg", "eggs", "portion", "portions"):
        return "pcs"
    if u in ("bottle", "bottles", "can", "cans"):  # counted by the bottle: a case of twelve holds twelve of them
        return "inner"
    return None


class GatewayError(Exception):
    def __init__(self, status, code, message, retry_after=None):
        super().__init__(message)
        self.status, self.code, self.message, self.retry_after = status, code, message, retry_after


class Gateway:
    def __init__(self, store: Store | None = None):
        self.cat = Catalog()
        self.aimo = Aimo()
        self.store = store or Store()
        self.key = Key.load("valio_aimo")  # the wholesaler's signing key, published in /.well-known/ucp
        self._keys: dict[str, dict] = {}
        self._rate: dict[tuple, tuple[float, float]] = {}
        self._rate_lock = threading.Lock()
        self.audience = f"{PUBLIC_URL}/.well-known/ucp"
        from .after import After  # what happens after the order: short picks, delays, recalls (imported here: it imports this module)
        self.after = After(self)
        from .allocation import Allocation  # short stock is split by contract share (rule L1)
        self.allocation = Allocation(self)
        from .seller import Seller  # Valio Aimo's own agent: proposals to customers' agents
        self.seller = Seller(self)
        self._seed()

    # ------------------------------------------------------------------ setup
    def _seed(self):
        if not self.account(STAGE):
            self.create_account(STAGE, BUYER_NAME, kind="stage", customer=DEMO_CUSTOMER, ship_to="Nordic Harbour Hotel Espoo, receiving dock B")
        self._register(self.account(STAGE))

    # --------------------------------------------------------------- accounts
    # One account = one buying company: its customer number, keys, order history, mandate and agents.
    def keys(self, slug: str) -> dict:
        """Demo stand-ins for keys that in production live with the buyer, never with Valio Aimo."""
        if slug not in self._keys:
            pre = "demo_buyer" if slug == STAGE else f"b_{slug}"
            self._keys[slug] = {r: Key.load(f"{pre}_{r}") for r in ("org", "agent", "approver")}
        return self._keys[slug]

    def account(self, slug) -> dict | None:
        return self.store.get("acct:" + str(slug))

    def account_of(self, customer) -> dict | None:
        slug = self.store.get("cust:" + str(customer))
        return self.account(slug) if slug else None

    def accounts(self, kind=None) -> list[dict]:
        rows = [json.loads(r[0]) for r in self.store.q("select v from kv where k like 'acct:%'")]
        return [a for a in rows if not kind or a["kind"] == kind]

    def _new_customer(self) -> str:
        while True:  # two accounts must never share a customer number
            c = "9" + f"{secrets.randbelow(10**7):07d}"
            if not self.store.get("cust:" + c):
                return c

    def create_account(self, slug, name, kind="sandbox", customer=None, ship_to=None, history=None, mandate=None, world=None, **extra) -> dict:
        """kind: stage (the demo account), floor (simulated customers sharing the stage's stock) or sandbox (one per visitor, own stock)."""
        if not re.fullmatch(r"[a-z0-9-]{2,40}", slug):
            raise GatewayError(400, "bad_request", "Account ids are lowercase letters, digits and dashes.")
        a = {"slug": slug, "name": name, "kind": kind, "customer": customer or self._new_customer(), "iss": f"https://buyer.example/{slug}",
             "ship_to": ship_to or f"{name}, goods in", "world": world or (slug if kind == "sandbox" else "main"), "created": iso(),
             "since_seq": self.store.q("select coalesce(max(seq),0) from events")[0][0], **extra}
        self.store.put("acct:" + slug, a)
        self.store.put("cust:" + a["customer"], slug)
        self._seed_account(a, history, mandate)
        return a

    def _seed_account(self, a, history=None, mandate=None):
        s, c = self.store, a["customer"]
        if s.get("profile:" + c) is None:
            s.put("profile:" + c, policy.default_profile())
        if s.get("history:" + c) is None:
            s.put("history:" + c, json.loads(json.dumps(SEED_HISTORY if history is None else history)))
        if s.get("mandate_config:" + c) is None:
            s.put("mandate_config:" + c, mandate or policy.default_mandate())

    def _register(self, a):
        """Onboarding: the buying company registers its public key with Valio Aimo once."""
        k = self.keys(a["slug"])
        self.store.put("buyer:" + a["iss"], {"name": a["name"], "customer": a["customer"], "slug": a["slug"], "world": a["world"], "jwk": k["org"].jwk, "approver_jwk": k["approver"].jwk, "ship_to": a["ship_to"]})

    def reset(self, slug=STAGE):
        """Start one account over. The ledger is append-only, so its entries stay; the account just stops showing older ones."""
        a = self.account(slug)
        if not a:
            return
        c, q = a["customer"], self.store.q
        q("delete from holds where ref in (select id from checkouts where customer=?)", (c,))
        q("delete from checkouts where customer=?", (c,))
        q("delete from orders where customer=?", (c,))
        for k in ("profile:", "history:", "mandate_config:", "allergen_cleared:"):
            q("delete from kv where k=?", (k + c,))
        q("delete from kv where k like ?", (f"agent:agent:{slug}/%",))
        a["since_seq"] = q("select coalesce(max(seq),0) from events")[0][0]
        self.store.put("acct:" + slug, a)
        self._seed_account(a)

    def wipe(self):
        """Everything back to zero, ledger included. Sign-in sessions survive."""
        for t in ("checkouts", "orders", "holds", "events", "runs"):
            self.store.q(f"delete from {t}")
        self.store.q("delete from kv where k not like 'session:%'")
        self._seed()

    # ---------------------------------------------------------------- agents
    def agent_note(self, principal, what="seen", cid=None, trace=None):
        """Each agent's record with Valio Aimo: orders, times it had to ask a person, refusals.
        Three refusals inside ten minutes suspend the agent automatically (rule W4)."""
        if not principal or not principal.get("ok"):
            return
        k = "agent:" + principal["agent"]
        ag = self.store.get(k) or {"id": principal["agent"], "name": principal.get("agent_name") or principal["agent"].split("/")[-1], "slug": principal.get("slug"), "customer": principal["customer"],
                               "buyer_name": principal["buyer_name"], "kind": "key" if principal.get("bound") else "keyless", "first_seen": iso(), "orders": 0, "asked": 0, "refused": 0, "rate_limited": 0, "lookups": [], "strikes": [], "status": "active"}
        ag["last_seen"] = iso()
        if what == "order":
            ag["orders"] += 1
        elif what == "asked":
            ag["asked"] += 1
        elif what == "rate_limited":
            ag["rate_limited"] = ag.get("rate_limited", 0) + 1
        elif what == "strike":
            t = time.time()
            ag["refused"] += 1
            ag["strikes"] = [s for s in ag["strikes"] if t - s < STRIKE_WINDOW] + [t]
            if len(ag["strikes"]) >= STRIKE_LIMIT and ag["status"] == "active":
                ag.update(status="suspended", reason=f"{STRIKE_LIMIT} refused attempts within {STRIKE_WINDOW // 60} minutes", suspended_at=iso())
                self.store.log(trace, "gateway", "control", f"Suspended {ag['id']} automatically: {ag['reason']}. It can still look up products, but cannot order until Valio Aimo reinstates it.",
                               {"agent": ag["id"], "rule": "W4"}, cid=cid, acct=principal["customer"])
        self.store.put(k, ag)

    def agents(self, slug=None) -> list[dict]:
        rows = [json.loads(r[0]) for r in self.store.q("select v from kv where k like 'agent:%'")]
        return sorted((a for a in rows if not slug or a.get("slug") == slug), key=lambda a: a.get("last_seen", ""), reverse=True)

    def set_agent(self, agent_id, suspended: bool, by="Valio Aimo account manager", reason="suspended by Valio Aimo"):
        ag = self.store.get("agent:" + str(agent_id))
        if not ag:
            raise GatewayError(404, "not_found", "No such agent.")
        ag.update(status="suspended" if suspended else "active", reason=reason if suspended else None, strikes=[] if not suspended else ag["strikes"])
        self.store.put("agent:" + ag["id"], ag)
        self.store.log(None, f"human:{by}", "control", f"{by} {'suspended' if suspended else 'reinstated'} {ag.get('name') or ag['id']} ({ag['id']})." + (f" Reason: {reason}." if suspended else ""), {"agent": ag["id"]}, acct=ag["customer"])
        return ag

    def rate_limit(self, principal=None, ip=None, operation="read"):
        """Token buckets shared by REST and MCP. Defaults leave room for the floor/evals."""
        now_m = time.monotonic()
        agent_rate = float(os.environ.get("AIMO_AGENT_RATE", "5"))
        account_rate = float(os.environ.get("AIMO_ACCOUNT_RATE", "5"))
        agent_burst = float(os.environ.get("AIMO_AGENT_BURST", "60"))
        account_burst = float(os.environ.get("AIMO_ACCOUNT_BURST", "60"))
        rate, burst = agent_rate, agent_burst
        keys = []
        if principal and principal.get("ok"):
            keys = [("agent", principal.get("agent"), agent_rate, agent_burst), ("account", principal.get("customer"), account_rate, account_burst)]
        elif ip:
            rate = float(os.environ.get("AIMO_IP_RATE", "4"))
            burst = float(os.environ.get("AIMO_IP_BURST", "60"))
            keys = [("ip", ip, rate, burst)]
        with self._rate_lock:
            retry = 0.0
            for kind, ident, key_rate, key_burst in keys:
                key = (kind, ident)
                tokens, at = self._rate.get(key, (key_burst, now_m))
                tokens = min(key_burst, tokens + (now_m - at) * key_rate)
                if tokens < 1:
                    retry = max(retry, (1 - tokens) / key_rate)
                else:
                    self._rate[key] = (tokens - 1, now_m)
            if retry:
                if principal and principal.get("ok"):
                    self.agent_note(principal, "rate_limited")
                raise GatewayError(429, "rate_limited", "Rate limit reached for this agent or account.", retry_after=max(1, round(retry, 1)))

    def _harvest_check(self, principal, ids, trace=None):
        if not principal or not principal.get("ok"):
            return
        key = "agent:" + principal["agent"]
        ag = self.store.get(key)
        if not ag:
            self.agent_note(principal)
            ag = self.store.get(key)
        now_t = time.time()
        events = [x for x in ag.get("lookups", []) if now_t - x[0] < HARVEST_WINDOW]
        seen = {x[1] for x in events}
        events.extend((now_t, str(i)) for i in ids if str(i) not in seen)
        ag["lookups"] = events[-500:]
        if len({x[1] for x in events}) >= HARVEST_LIMIT and not ag.get("orders"):
            self.store.put(key, ag)
            self.agent_note(principal, "strike", trace=trace)
            self.store.log(trace, "gateway", "refused", "Refused catalogue harvesting without a checkout under rule W4.", {"code": "catalogue_harvest", "rule": "W4"}, acct=principal["customer"])
            raise GatewayError(403, "catalogue_harvest", "Catalogue harvesting without a checkout is refused under rule W4.")
        self.store.put(key, ag)

    def _logp(self, principal, trace, kind, text, data=None):
        self.agent_note(principal)
        self.store.log(trace, self._who(principal), kind, text, data, acct=(principal or {}).get("customer"))

    def profile_doc(self) -> dict:
        """Business profile served at /.well-known/ucp: where to talk to us and which keys we sign with."""
        ent = {"version": UCP_VERSION}
        return {
            "ucp": {
                "version": UCP_VERSION,
                "services": {"dev.ucp.shopping": [
                    {**ent, "transport": "rest", "endpoint": PUBLIC_URL, "schema": f"{PUBLIC_URL}/openapi.json"},
                    {**ent, "transport": "mcp", "endpoint": f"{PUBLIC_URL}/mcp"},
                ]},
                "capabilities": {c: [{**ent, **({"spec": f"{PUBLIC_URL}/spec#{c.rsplit('.', 1)[-1]}"} if c.startswith(EXT) else {})}] for c in CAPS},
                "payment_handlers": {},
            },
            "keys": [self.key.jwk],
            "business": {"name": "Valio Aimo (Junction 2026 mock)", "note": "Synthetic challenge data. Not Valio Aimo's production systems."},
        }

    def _env(self) -> dict:
        return {"version": UCP_VERSION, "capabilities": {c: [{"version": UCP_VERSION}] for c in CAPS}, "payment_handlers": {}}

    # --------------------------------------------------------------- identity
    def issue_mandate(self, slug: str = STAGE, constraints: dict | None = None, ttl: int = 7 * 86400, agent: str | None = None, bound: bool = True, name: str | None = None) -> str:
        """Demo helper: signs a mandate with a buyer's key. In production the buyer does this.
        bound=False is a mandate for an assistant that holds no key (it connected with a sign-in instead)."""
        a, k, t = self.account(slug), self.keys(slug), now()
        self._register(a)
        claims = {
            "iss": a["iss"], "sub": agent or f"agent:{slug}/purchasing-1", "aud": self.audience, "iat": t, "exp": t + ttl,
            "jti": "mnd_" + secrets.token_hex(6), **({"cnf": {"jwk": k["agent"].jwk}} if bound else {}), **({"agent_name": name} if name else {}),
            "mandate": constraints or self.store.get("mandate_config:" + a["customer"]),
        }
        return jwt_encode(claims, k["org"], typ="mandate+jwt")

    def buyer_keys(self, iss) -> list[dict]:
        """Public keys a buying company registered itself. Their private halves never reach this gateway."""
        return self.store.get("buyer_keys:" + str(iss), [])

    def register_buyer_key(self, slug: str, jwk) -> dict:
        """The buying company registers its own signing key: the public half only. From then on it can sign its agents' mandates itself,
        and an order placed under one carries a signature this gateway could not have made."""
        a = self.account(slug)
        if not a:
            raise GatewayError(404, "not_found", "No such account.")
        if not isinstance(jwk, dict) or "d" in jwk:
            raise GatewayError(400, "bad_key", "Send the public half of the key only, as a JWK. A JWK with a private part (d) is refused and not stored.")
        try:
            if jwk.get("kty") != "EC" or jwk.get("crv") != "P-256":
                raise ValueError
            x, y = b64d(str(jwk["x"])), b64d(str(jwk["y"]))
            pub = public_jwk(ec.EllipticCurvePublicNumbers(int.from_bytes(x, "big"), int.from_bytes(y, "big"), ec.SECP256R1()).public_key())  # raises unless the point is on the curve
        except Exception:
            raise GatewayError(400, "bad_key", "The key must be a P-256 public key as a JWK (kty EC, crv P-256, x, y), the kind ES256 signs with.")
        self._register(a)
        keys = [k for k in self.buyer_keys(a["iss"]) if k["jwk"]["kid"] != pub["kid"]][-4:] + [{"jwk": pub, "at": iso()}]
        self.store.put("buyer_keys:" + a["iss"], keys)
        self.store.log(None, "human:buyer admin", "control", f"{a['name']} registered its own signing key with Valio Aimo (key {pub['kid'][:16]}…). The gateway holds the public half only: mandates signed with it are the buyer's own.",
                       {"kid": pub["kid"]}, acct=a["customer"])
        return {"kid": pub["kid"], "issuer": a["iss"], "audience": self.audience, "customer": a["customer"], "agent_prefix": f"agent:{a['slug']}/", "mandate": self.store.get("mandate_config:" + a["customer"]),
                "keys": [k["jwk"]["kid"] for k in keys]}

    def identify(self, token: str | None) -> dict | None:
        if not token:
            return None
        bad = lambda code, detail: {"ok": False, "error": code, "detail": detail}
        try:
            _, c = jwt_peek(token)
            if not isinstance(c, dict):
                raise ValueError
        except Exception:
            return bad("mandate_invalid_signature", "The mandate is not a readable JWT.")
        buyer = self.store.get("buyer:" + str(c.get("iss")))
        if not buyer:
            return bad("mandate_scope_mismatch", "Unknown issuer: this company has not registered a signing key with Valio Aimo.")
        own = next((k["jwk"] for k in self.buyer_keys(c.get("iss")) if jwt_verify(token, k["jwk"]) is not None), None)
        if own is None and jwt_verify(token, buyer["jwk"]) is None:
            return bad("mandate_invalid_signature", "The mandate's signature does not match the buyer's registered key.")
        if own:  # the buyer wrote and signed this mandate itself, so nothing in it has passed through this gateway's hands: read it strictly
            if not isinstance(c.get("jti"), str) or not re.fullmatch(r"[A-Za-z0-9_.:-]{6,80}", c["jti"]) or not isinstance(c.get("exp"), (int, float)):
                return bad("mandate_scope_mismatch", "A mandate needs an id (jti) and an expiry (exp).")
            if not isinstance(c.get("sub"), str) or not re.fullmatch(re.escape(f"agent:{buyer.get('slug')}/") + r"[A-Za-z0-9_.-]{1,60}", c["sub"]):
                return bad("mandate_scope_mismatch", f"The agent named in the mandate (sub) must be one of this company's own: agent:{buyer.get('slug')}/<name>.")
            try:
                c["mandate"] = policy.fit_mandate(c.get("mandate") or {})
            except ValueError as e:
                return bad("mandate_scope_mismatch", f"The mandate's constraint block cannot be read: {e} has the wrong type.")
            if c.get("cnf") is not None and not (isinstance(c["cnf"], dict) and isinstance(c["cnf"].get("jwk"), dict) and c["cnf"]["jwk"].get("kty") == "EC" and "d" not in c["cnf"]["jwk"]):
                return bad("mandate_scope_mismatch", "cnf.jwk must be the agent's public key.")
        if c.get("exp", 0) < now():
            return bad("mandate_expired", "The mandate has expired.")
        if c.get("aud") != self.audience:
            return bad("mandate_scope_mismatch", "The mandate was issued for a different seller.")
        if c.get("jti") in (self.store.get("revoked") or []):
            return bad("mandate_expired", "The buyer has revoked this mandate.")
        jwk = (c.get("cnf") or {}).get("jwk")
        p = {"ok": True, "token": token, "jti": c["jti"], "exp": c["exp"], "buyer": c["iss"], "buyer_name": buyer["name"], "customer": buyer["customer"], "slug": buyer.get("slug"), "world": buyer.get("world", "main"),
             "agent": c.get("sub"), "agent_name": str(c.get("agent_name") or "")[:60] or None, "agent_jwk": jwk, "bound": bool(jwk), "mandate": c.get("mandate") or {}, "ship_to": buyer.get("ship_to"),
             "mandate_key": {"kid": (own or buyer["jwk"])["kid"], "held_by": "buyer" if own else "gateway"}}
        ag = self.store.get("agent:" + str(p["agent"]))
        if ag and ag.get("status") == "suspended":
            p["suspended"] = ag.get("reason") or "suspended"
        return p

    def _who(self, principal) -> str:
        return (principal or {}).get("agent") or "agent:anonymous"

    def _full(self, principal) -> bool:
        return bool(principal and principal.get("ok") and self.store.get("profile:" + principal["customer"], {}).get("show_contract_prices", True))

    # ---------------------------------------------------------------- lookups
    def _day(self, day) -> str:
        """The delivery day as YYYY-MM-DD. No date given: the next day that is not a Sunday. A date that cannot be read is an error, never a silent guess."""
        if isinstance(day, str) and re.fullmatch(r"\d{4}-\d{2}-\d{2}([T ].*)?", day.strip()):
            day = day.strip()[:10]
            try:
                datetime.strptime(day, "%Y-%m-%d")
                return day
            except ValueError:
                pass
        if day not in (None, ""):
            raise GatewayError(400, "invalid_delivery_date", f"delivery_date must be a date as YYYY-MM-DD, for example 2026-11-16. Got {str(day)[:40]!r}.")
        d = (parse_ts(FREEZE_NOW) or datetime.now(TZ)) + timedelta(days=1)
        if d.weekday() == 6:
            d += timedelta(days=1)
        return d.strftime("%Y-%m-%d")

    @staticmethod
    def _asked_day(req: dict):
        """The delivery date of a request, wherever the agent put it: at the top or under "wholesale"."""
        w = req.get("wholesale") if isinstance(req.get("wholesale"), dict) else {}
        return w.get("delivery_date") or req.get("delivery_date") or w.get("requested_date") or req.get("requested_date")

    def _live(self, ids, day, principal, exclude_ref="") -> dict:
        if not principal or not principal.get("ok"):
            why = (principal or {}).get("detail") or "Present the buyer's mandate as Authorization: Bearer <mandate>."
            return {i: {"orderable": False, "reason": (principal or {}).get("error") or "mandate_required", "said": why[0].lower() + why[1:].rstrip("."), "available": 0.0, "stock_known": False} for i in ids}
        customer = principal["customer"]
        try:
            raw = self.aimo.quote([i for i in ids if self.cat.get(i)], day, customer)
        except AimoError as e:
            raise GatewayError(503, e.code, e.message)
        world = (principal or {}).get("world", "main")
        # recalled products are not orderable; nor, for this account, is one with an unsettled short pick or delay on that day
        return self.allocation.cap(self.seller.mark(self.after.block({i: facts(raw.get(i), day, self.store.reserved(i, exclude_ref, world)) for i in ids}, world, day, None if customer == "public" else customer), exclude_ref), world, day, customer, exclude_ref)

    def _content(self, d, f, key) -> float | None:
        if key == "pcs":   # 600 eggs are 60 boxes of ten, not 600 boxes
            return float(pieces(d) or 1)
        if key == "inner":
            return float(pieces(d) or case_count(d) or 1)
        if key is None:
            return 1.0
        if d["content"].get(key):
            return d["content"][key]
        if f.get("content_per_unit") and f.get("comparison_unit") == ("L" if key == "l" else "KG"):
            return _snap(f["content_per_unit"])
        return None

    def _qunit(self, d) -> dict:
        code, label = UNITS.get(d["unit"], (d["unit"], d["unit"].lower()))
        q = {"unit": code, "display_text": label}
        if d["scale"]:
            q["scale"] = d["scale"]
        if d["increment"] > 1:
            q["increment"] = d["increment"]
        return q

    def _attrs(self, d) -> dict:
        al = lambda codes: [{"code": c, "name": ALLERGENS.get(c, c)} for c in codes]
        return {
            "gtin": d.get("gtin"), "brand": d.get("brand"), "supplier": d.get("vendor"), "producer": d.get("producer"),
            "origin": d.get("origin"), "storage": d.get("temp"), "claims": d["claims"],
            "allergens": {"contains": al(d["contains"]), "may_contain": al(d["may_contain"]), "declared": declares_allergens(d)},
            "labels": d["labels"], "delivery_reliability": d.get("reliability"), "co2_kg_per_kg": d.get("co2_per_kg"),
            "listed_substitutes": d["subs"], "data_source": "GS1 Synkka (manufacturer-supplied)", "data_updated": d.get("data_updated"),
        }

    def _fit(self, d, f, need) -> dict | None:
        if not need or not need.get("quantity"):
            return None
        key = _need_key(need.get("unit", ""))
        per = self._content(d, f, key)
        if not per:
            return {"computable": False, "note": f"No {need.get('unit')} content in the product data."}
        step = d["increment"] / 10 ** d["scale"]
        units = math.ceil(need["quantity"] / per / step - 1e-9) * step
        can = units if not f.get("stock_known") else math.floor(min(units, f["available"]) / step + 1e-9) * step
        out = {"computable": True, "units_needed": round(units, 2), "units_can_be_supplied": round(can, 2), "covers_quantity": round(can * per, 2),
               "covers_pct": round(min(100.0, 100 * can * per / need["quantity"]), 1), "content_per_unit": per}
        if "price" in f:
            out["line_total_ex_vat"] = round(f["price"] * units)
        return out

    def _exact_stock(self, principal) -> bool:
        prof = self.store.get("profile:" + principal["customer"], {}) if principal and principal.get("ok") else {}
        return bool(prof.get("show_exact_stock", prof.get("exact_stock", prof.get("exact_counts", False))))

    @staticmethod
    def _availability(f, need=None) -> str:
        if not f.get("orderable") or (f.get("stock_known") and f.get("available", 0) <= 0):
            return "out"
        if f.get("stock_known") and need and need.get("quantity"):
            return "limited" if f.get("available", 0) < need["quantity"] else "available"
        return "limited" if f.get("stock_known") and f.get("available", 0) < 10 else "available"

    def _product(self, d, f, principal, need, day) -> dict:
        full = self._full(principal)
        price = f.get("price") if full else None
        w = {**self._attrs(d)}
        if full:
            w.update({"orderable": f["orderable"], "reason": f["reason"], "availability": self._availability(f, need),
                      "delivery": {"requested_date": day, "earliest_date": f.get("earliest_date"), "order_by": f.get("cutoff"), "logistic_model": f.get("logistic_model")}})
            if f.get("stale"):
                w["data_stale"] = True
            if self._exact_stock(principal):
                w["stock"] = {"known": f["stock_known"], **({"available": f["available"]} if f["stock_known"] else {"note": "Supplier-shipped or unlimited: quantity not tracked."})}
            w["price"] = {"unit_price": f.get("price"), "list_price": f.get("list_price"), "contract_price": f.get("contract_price", False), "tax_rate_pct": f.get("tax_rate"),
                          **({"comparison": {"amount": f["comparison_price"], "per": f["comparison_unit"]}} if f.get("comparison_price") else {})}
            fit = self._fit(d, f, need)
            if fit:
                w["fit"] = fit
        else:
            w["visibility"] = "Public catalogue facts only. Present a mandate to see prices and availability."
        amount = {"amount": price, "currency": CURRENCY} if price is not None else None
        variant = {
            "id": d["id"], "sku": d["id"], "title": d["title"], "description": {"plain": d["names"].get("fi", d["title"])},
            "quantity_unit": self._qunit(d),
            **({"barcodes": [{"type": "GTIN", "value": d["gtin"]}]} if d.get("gtin") else {}),
        }
        if amount is not None:
            variant["price"] = amount
        if full:
            variant["availability"] = {"available": f["orderable"], "band": self._availability(f, need)}
        product = {"id": d["id"], "title": d["title"], "description": {"plain": " · ".join(filter(None, [d.get("brand"), d.get("vendor"), d.get("origin")]))},
                "categories": [{"value": d["category"], "taxonomy": "fi.valioaimo.category"}], "variants": [variant], "wholesale": w}
        if amount is not None:
            product["price_range"] = {"min": amount, "max": amount}
        return product

    def _prove(self, doc: dict) -> dict:
        """Sign any response so the agent can hold Valio Aimo to its price, stock and origin claims."""
        body = {k: v for k, v in doc.items() if k != "proof"}
        doc["proof"] = {"type": "JWS detached, JCS (RFC 8785), ES256", "kid": self.key.kid, "signed_at": iso(), "jws": sign_detached(body, self.key), "keys": f"{PUBLIC_URL}/.well-known/ucp"}
        return doc

    def search(self, req: dict, principal=None, trace=None) -> dict:
        w = req.get("wholesale") or {}
        day, need = self._day(self._asked_day(req)), w.get("need")
        limit = max(1, min(int((req.get("pagination") or {}).get("limit", 10)), 25))
        same, f, notes, picked = self.cat.pick(req.get("query", ""), req.get("filters") or {})
        docs = picked["text"]
        if same and picked["how"] == "named":  # what was asked for comes first; products that only share its words follow
            ids = {d["id"] for d in same}
            docs = same[:60] + [d for d in docs if d["id"] not in ids]
            if picked["other"]:
                notes.append(f"{len(same)} products are plainly {' / '.join(picked['kinds'])}; they come first. Other kinds that share the words: {', '.join(list(picked['other'])[:5])}.")
        self._harvest_check(principal, [d["id"] for d in docs[:limit]], trace)
        cats = Counter(d["category"] for d in docs)
        example = {}
        for d in docs:
            example.setdefault(d["category"], d["title"])
        docs = docs[:limit]
        live = self._live([d["id"] for d in docs], day, principal)
        products = [self._product(d, live[d["id"]], principal, need, day) for d in docs]
        ok = sum(1 for p in products if p["wholesale"].get("orderable"))
        resp = {
            "ucp": self._env(), "products": products,
            "messages": [{"type": "info", "content": n} for n in notes],
            "wholesale": {"delivery_date": day, "requirements_applied": {k: v for k, v in f.items() if v and k in ("claims", "labels", "exclude_allergens", "categories", "origin")},
                          "categories_matched": [{"value": c, "count": n, "example": example[c]} for c, n in cats.most_common(6)],
                          "visibility": "contract" if self._full(principal) else "public"},
        }
        if self._full(principal) and any(p["wholesale"].get("data_stale") for p in products):
            resp["wholesale"]["data_stale"] = True
        self._logp(principal, trace, "lookup", f"Looked up '{req.get('query', '')}' for delivery {nice_day(day)}: {len(products)} products returned, {ok} orderable for that date.",
                       {"query": req.get("query"), "requirements": resp["wholesale"]["requirements_applied"], "products": [p["id"] for p in products]})
        return self._prove(resp)

    def lookup(self, ids: list[str], day=None, principal=None, trace=None) -> dict:
        day = self._day(day)
        self._harvest_check(principal, ids[:25], trace)
        docs = [d for d in (self.cat.get(i) for i in ids[:25]) if d]
        live = self._live([d["id"] for d in docs], day, principal)
        resp = {"ucp": self._env(), "products": [self._product(d, live[d["id"]], principal, None, day) for d in docs],
                "messages": [{"type": "warning", "code": "not_found", "content": f"Unknown product {i}."} for i in ids if not self.cat.get(i)], "wholesale": {"delivery_date": day}}
        if self._full(principal) and any(p["wholesale"].get("data_stale") for p in resp["products"]):
            resp["wholesale"]["data_stale"] = True
        self._logp(principal, trace, "lookup", f"Looked up {len(docs)} product(s) by id for delivery {nice_day(day)}.", {"products": [d["id"] for d in docs]})
        return self._prove(resp)

    # ------------------------------------------------------------ substitutes
    def _subinfo(self, d, f, od, of) -> dict:
        def crit(x):  # a vegan product is vegetarian whether or not the manufacturer ticked both boxes
            c = {c for c in x["claims"] if c.startswith(("FREE_FROM:", "DIET:"))}
            return c | ({"DIET:VEGETARIAN"} if "DIET:VEGAN" in c else set())
        per, delta = "unit", None
        if f.get("comparison_price") and of.get("comparison_price") and f.get("comparison_unit") == of.get("comparison_unit"):
            per, delta = f["comparison_unit"], 100 * (f["comparison_price"] - of["comparison_price"]) / of["comparison_price"]
        elif f.get("price") and of.get("price"):
            delta = 100 * (f["price"] - of["price"]) / of["price"]
        return {"for": od["id"], "for_title": od["title"], "listed": d["id"] in od["subs"], "lost_claims": sorted(crit(od) - crit(d)), "for_undeclared": not declares_allergens(od),
                "new_allergens": sorted((set(d["contains"]) | set(d["may_contain"])) - (set(od["contains"]) | set(od["may_contain"]))),
                "price_delta_pct": None if delta is None else round(delta, 1), "per": per}

    def substitutes(self, pid: str, day: str, principal, limit=5) -> list[dict]:
        """Good / better / best within Valio Aimo's own range, ranked by rule, not by a model."""
        od = self.cat.get(pid)
        if not od:
            return []
        listed = [s for s in od["subs"] if self.cat.get(s) and s != od["id"]]
        listed_set = set(listed)
        # Beyond Valio Aimo's own list: products that are the same kind of thing (milk for milk, never coffee milk), same storage class first.
        same = [d for d in self.cat.by_kind.get(kind(od), []) if d["id"] != od["id"] and d["id"] not in listed_set]
        rest = [d["id"] for d in sorted(same, key=lambda d: d["temp"] != od["temp"])]
        ids = (listed + rest)[:120]
        live = self._live(ids + [od["id"]], day, principal)
        of, out = live[od["id"]], []
        hist = self._history(principal) if principal and principal.get("ok") else {}
        for i in ids:
            d, f = self.cat.get(i), live[i]
            if not f["orderable"]:
                continue
            s = self._subinfo(d, f, od, of)
            if s["lost_claims"] or s["new_allergens"]:
                continue  # never offered: would break a dietary claim or add an allergen
            # Best: Valio Aimo names it as the substitute, or this kitchen already orders it. Better: this account has a contract price for it,
            # or it is the same brand, or within 10 % of the price. Good: the same kind of product at the list price.
            own, contract = hist.get(d["id"]), bool(f.get("contract_price") and self._full(principal))
            tier = "best" if s["listed"] or own else "better" if (contract or d.get("brand") == od.get("brand") or abs(s["price_delta_pct"] or 0) <= 10) else "good"
            out.append({"id": d["id"], "title": d["title"], "tier": tier, "listed_by_valio_aimo": s["listed"], "contract_price": contract, "price_delta_pct": s["price_delta_pct"], "per": s["per"],
                        "unit_price": f.get("price"), "availability": self._availability(f), "delivery_reliability": d.get("reliability"),
                        "why": ("On Valio Aimo's substitute list. " if s["listed"] else (f"This kitchen already orders it ({own['orders']} past orders). " if own else "") + f"The same kind of product ({kind(od)})" + (". " if d["temp"] == od["temp"] else f", but {d['temp']} where yours is {od['temp']}. "))
                               + "Keeps every free-from and diet claim; adds no allergens. From Valio Aimo's own range, "
                               + ("at this account's contract price." if contract else "at the list price: this account has no contract price for it." if self._full(principal) else "at this account's price.")})
        order = {"best": 0, "better": 1, "good": 2}
        out.sort(key=lambda x: (order[x["tier"]], not x["contract_price"], abs(x["price_delta_pct"] or 0)))  # within a rank, what this account has a contract price for comes first
        return out[:limit]

    def near_kinds(self, query: str, need: dict, day: str, principal, filters: dict | None = None, limit: int = 4) -> list[dict]:
        """Nothing of the kind asked for can come: the nearest kinds in Valio Aimo's own range that can come that day, for the kitchen to pick from.
        A near kind shares a word with the one asked for and sits on the same shelf (smoked ham for Christmas ham, pork neck for pork shoulder).
        One availability request covers all of them; nothing is put on a cart from here."""
        _, f, _, picked = self.cat.pick(query, filters or {})
        asked = picked["kinds"]
        if not asked or not principal or not principal.get("ok"):
            return []
        own = frozenset().union(*(self.cat.kind_stems.get(k, frozenset()) for k in asked))
        shelf = {d["category"] for k in asked for d in self.cat.by_kind.get(k, [])}
        cand = {}
        for k, st in self.cat.kind_stems.items():
            if k in asked or not st & own:
                continue
            ds = [d for d in self.cat.by_kind.get(k, []) if self.cat.matches(d, f)]
            # on the same shelf, or two words of the kind in common on the shelf next to it ("brown sauce powder" for "brown sauce base")
            if ds and (2 * sum(d["category"] in shelf for d in ds) >= len(ds) or (len(st & own) >= 2 and 2 * sum(str(d["category"])[:3] in {str(x)[:3] for x in shelf} for d in ds) >= len(ds))):
                cand[k] = ds[:20]
        near = lambda k: -len(self.cat.kind_stems.get(k, frozenset()) & own)  # "brown sauce powder" is nearer "brown sauce base" than "red wine sauce" is
        cand = dict(sorted(cand.items(), key=lambda kv: (near(kv[0]), -len(kv[1]), kv[0]))[:8])
        ids = [d["id"] for ds in cand.values() for d in ds]
        if not ids:
            return []
        live, out = self._live(ids, day, principal), []
        for k, ds in cand.items():
            ok = [d for d in ds if live[d["id"]]["orderable"] and "price" in live[d["id"]] and (self._fit(d, live[d["id"]], need) or {}).get("covers_pct", 0) >= 100]
            if ok:
                out.append({"kind": k, "products": len(ok), "example": {"id": ok[0]["id"], "title": ok[0]["title"]}})
        out.sort(key=lambda x: (near(x["kind"]), -x["products"]))
        self._logp(principal, None, "plan", f"Nothing that is {' / '.join(asked)} can come on {nice_day(day)}. Nearest kinds in Valio Aimo's own range that can: " + (", ".join(f"{x['kind']} ({x['products']})" for x in out[:limit]) or "none") + ".",
                   {"asked": asked, "near": [x["kind"] for x in out[:limit]]})
        return out[:limit]

    # ----------------------------------------------------------------- plan
    def plan(self, req: dict, principal=None, trace=None) -> dict:
        """Turn a need ('1,000 L by Monday') into ranked, ready-to-order options, with the policy outcome of each."""
        need, day = req.get("need") or {}, self._day(self._asked_day(req))
        if not isinstance(need, dict) or not isinstance(need.get("quantity"), (int, float)) or isinstance(need.get("quantity"), bool) or not math.isfinite(need.get("quantity")) or need.get("quantity") <= 0 or not isinstance(need.get("unit"), str) or not need.get("unit").strip():
            raise GatewayError(400, "invalid_need", "need.quantity must be a positive number and need.unit must be a non-empty string.")
        if req.get("filters") is not None and not isinstance(req.get("filters"), dict):
            raise GatewayError(400, "invalid_filters", "filters must be an object.")
        key = _need_key(need.get("unit", ""))
        reqs = {"claims": self.cat.canon_claims((req.get("filters") or {}).get("claims")), "exclude_allergens": self.cat.canon_allergens((req.get("filters") or {}).get("exclude_allergens")),
                "labels": self.cat.canon_labels((req.get("filters") or {}).get("labels"))}
        query = req.get("query") or need.get("query") or need.get("product") or need.get("item") or ""  # the product wanted, also when a client put it inside "need"
        if not isinstance(query, str) or not query.strip():
            raise GatewayError(400, "missing_query", 'Say which product is needed in "query", for example {"query": "lactose-free milk", "need": {"quantity": 1000, "unit": "L"}, "delivery_date": "2026-11-16"}.')
        pref = self.cat.get(req.get("preferred_product")) if req.get("preferred_product") else None
        named = str(req.get("preferred_product")).strip() if req.get("preferred_product") and not pref else ""  # a brand or product name instead of an id
        hist = self._history(principal)
        # A request for milk gets milk: coffee milk and sour milk are other kinds, however cheap. They are named, not offered.
        docs, f, notes, picked = self.cat.pick(query, req.get("filters") or {}, pref)
        reqs = {k: sorted(set(reqs[k]) | set(f.get(k) or [])) for k in reqs}
        choices = picked["choices"]
        if choices and not pref:
            # The request does not say which kind ("chicken", "pasta", "cream"). What this account has ordered settles it; otherwise the agent is asked.
            past = {k: sum(hist[d["id"]]["orders"] for d in v if d["id"] in hist) for k, v in choices.items()}
            usual = max(past, key=past.get) if any(past.values()) else None
            listed = ", ".join(f"{k} ({len(v)})" for k, v in list(choices.items())[:6])
            if usual:
                docs, f, _, picked = self.cat.pick(query, req.get("filters") or {}, use_kind=usual)
                notes.append(f"'{query}' does not say which kind: {listed}. This account has ordered {usual} {past[usual]} time(s), so that is what is offered. Ask for another kind by name to get it.")
            else:
                resp = {"ucp": self._env(), "need": need, "delivery_date": day, "requirements": {k: v for k, v in reqs.items() if v}, "options": [],
                        "kinds_to_choose": [{"kind": k, "products": len(v), "example": {"id": v[0]["id"], "title": v[0]["title"]}} for k, v in list(choices.items())[:8]],
                        "messages": [{"type": "info", "content": n} for n in notes] + [{"type": "warning", "code": "which_kind", "content":
                                     f"'{query}' does not say which kind of product is meant, and this account has not ordered any of them before: {listed}. Nothing is offered until the kind is named. "
                                     "Call again with one of these kinds as the query if the request settles it; otherwise ask the kitchen."}]}
                self._logp(principal, trace, "plan", f"Asked for {need.get('quantity'):g} {need.get('unit')} of '{query}' by {nice_day(day)}. That could be {listed}, so no option was offered and the agent was told to say which.",
                           {"need": need, "kinds_to_choose": [k for k in choices][:8]})
                return self._prove(resp)
        other_kinds = picked["other"]
        names = " / ".join(picked["kinds"])
        if picked["kinds"] and not docs:
            n = sum(len(self.cat.by_kind.get(k, [])) for k in picked["kinds"])
            notes.append(f"Valio Aimo lists {n} product(s) that are {names}, and none of them meets the requirements of this request. Nothing of another kind is offered in their place.")
        elif picked["kinds"] and other_kinds and picked["how"] != "usual":
            n = sum(len(v) for v in other_kinds.values())
            notes.append(f"Offered from the {len(docs)} products that are plainly {names}. Left out {n} of other kinds that share the words ({', '.join(f'{k} {len(v)}' for k, v in list(other_kinds.items())[:5])}). Ask for one of those by name to get it.")
        elif not picked["kinds"]:
            notes.append(f"Nothing in Valio Aimo's range is called '{query}'. Try the plain name of the product.")
        docs.sort(key=lambda d: d["id"] not in hist)   # what the account has ordered before is always priced
        named_ids = []  # the products of the range that carry the name the request gave
        if named:
            # "Arla Pro" for whipping cream: the product of this kind whose name carries every word of the name that the request itself does not.
            words = lambda t: set(re.findall(r"[a-z0-9]+", fold(t)))
            want = words(named) - words(query) or words(named)  # "Nutella" asked for as "Nutella": the name is the whole request
            hits = [d for d in docs if want and want <= words(d.get("_text") or d["title"])]
            if not hits and want and docs:
                # The range may file the named product under a neighbouring kind ("Arla Cafè milk" is a coffee milk): the product named is the one
                # asked for. Only on the same shelf and with a word of the kind in common, so "Fazer milk" never finds milk chocolate.
                shelf, own, qw = {d["category"] for d in docs}, frozenset().union(*(self.cat.kind_stems.get(k, frozenset()) for k in picked["kinds"])), words(query)
                wide = [d for d in self.cat.docs if want <= d["_w"] and d["category"] in shelf and self.cat.kind_stems.get(kind(d), frozenset()) & own and self.cat.matches(d, f)]
                hits = sorted(wide, key=lambda d: -len(qw & d["_w"]))[:1]
            if hits:
                pref, named_ids = hits[0], [d["id"] for d in hits[:60]]
            else:
                notes.append(f"Valio Aimo's range has no {names or 'product'} called '{named}' that meets this request, so the options are the nearest products that do.")
        if pref and pref["id"] not in [d["id"] for d in docs]:
            docs.insert(0, pref)
        if len(docs) > 200:
            notes.append(f"Priced the first 200 of {len(docs)} products of this kind. Add a requirement or a brand to narrow it.")
        docs = docs[:200]
        live = self._live([d["id"] for d in docs], day, principal)
        cands, skipped = [], Counter()
        for d in docs:
            fx = live[d["id"]]
            fit = self._fit(d, fx, need)
            if not fx["orderable"]:
                skipped[fx["reason"]] += 1
            elif not fit or not fit["computable"] or "price" not in fx:
                skipped["no_content_data"] += 1
            else:
                cands.append((d, fx, fit))
        cands.sort(key=lambda c: c[1]["price"] / c[2]["content_per_unit"])
        # Cheapest means what the kitchen pays for its need, not the price per kilo of a case six times too big.
        every = sorted((c for c in cands if c[2]["covers_pct"] >= 100), key=lambda c: (c[2].get("line_total_ex_vat", 0), c[1]["price"] / c[2]["content_per_unit"]))
        # Six kilos of butter is not a 20 kg case, however cheap the case: a pack that brings over half again what was asked is offered as a choice, never picked first.
        sized = lambda c: c[2]["units_needed"] * c[2]["content_per_unit"] <= 1.5 * need["quantity"]
        full = [c for c in every if sized(c)] or every
        bigger = every[0] if every and every[0] is not full[0] and every[0][2].get("line_total_ex_vat", 0) < full[0][2].get("line_total_ex_vat", 0) else None

        def line(c, units, sub_for=None):
            d, fx, fit = c
            return {"item": {"id": d["id"], "title": d["title"]}, "quantity": round(units * 10 ** d["scale"]), "units": units, "unit": UNITS.get(d["unit"], (0, d["unit"]))[1],
                    "covers": round(units * fit["content_per_unit"], 2), "unit_price": fx["price"], "line_total_ex_vat": round(fx["price"] * units),
                    **({"pieces_per_unit": int(fit["content_per_unit"])} if key in ("pcs", "inner") and fit["content_per_unit"] > 1 else {}),
                    **({"wholesale": {"substitutes_for": sub_for}} if sub_for else {})}

        options = []

        def add(label, lines, tradeoff):
            sig = tuple(sorted((l["item"]["id"], l["quantity"]) for l in lines))
            if any(o["_sig"] == sig for o in options):
                return
            covered = sum(l["covers"] for l in lines)
            total = sum(l["line_total_ex_vat"] for l in lines)
            pre = self._preview(lines, day, principal, reqs)
            options.append({"_sig": sig, "label": label, "tradeoff": tradeoff, "covers": round(covered, 2), "covers_pct": round(min(100, 100 * covered / need["quantity"]), 1) if need.get("quantity") else None,
                            "total_ex_vat": total, "authority": pre, "line_items": lines})

        pref_label, pref_text, pref_off = "as_requested", "Exactly the product asked for", None
        if not pref:
            usual = sorted([c for c in cands if c[0]["id"] in hist], key=lambda c: -hist[c[0]["id"]]["orders"])
            if usual:
                pref = usual[0][0]
                pref_label, pref_text = "usual_product", f"What this account orders most often ({hist[pref['id']]['orders']} past orders)"
        if pref:
            c = next((c for c in cands if c[0]["id"] == pref["id"]), None)
            if c and c[2]["covers_pct"] >= 100:
                add(pref_label, [line(c, c[2]["units_needed"])], f"{pref_text}, in full, delivered {nice_day(day)}.")
            elif c:
                rest = need["quantity"] - c[2]["covers_quantity"]
                subs = self.substitutes(pref["id"], day, principal, limit=60)
                tiers = set()  # one choice per tier, best first: the kitchen picks between Valio Aimo's best, better and good replacement
                for s in subs:
                    sc = next((x for x in cands if x[0]["id"] == s["id"]), None)
                    if not sc or s["tier"] in tiers:
                        continue
                    step = sc[0]["increment"] / 10 ** sc[0]["scale"]
                    su = math.ceil(rest / sc[2]["content_per_unit"] / step - 1e-9) * step
                    if sc[1]["stock_known"] and sc[1]["available"] < su:
                        continue
                    n = len(options)
                    add("split_with_substitute", [line(c, c[2]["units_can_be_supplied"]), line(sc, su, pref["id"])],
                        f"We can supply {c[2]['covers_quantity']:g} of your {need['quantity']:g} {need['unit']} as {pref['title']}; the remaining {rest:g} {need['unit']} uses '{s['tier']}' substitute {sc[0]['title']}. {s['why']}")
                    if len(options) > n:
                        tiers.add(s["tier"])
                        options[-1]["substitute"] = {"tier": s["tier"], "id": s["id"], "title": s["title"], "listed_by_valio_aimo": s["listed_by_valio_aimo"], "contract_price": s["contract_price"],
                                                     "price_delta_pct": s["price_delta_pct"], "per": s["per"], "why": s["why"]}
                add("partial_only", [line(c, c[2]["units_can_be_supplied"])], f"We can supply {c[2]['covers_quantity']:g} of your {need['quantity']:g} {need['unit']} of {pref['title']}; {rest:g} {need['unit']} is short.")
            else:
                why = REASONS.get(live.get(pref["id"], {}).get("reason", ""), "it is not orderable")
                pref_off = {"id": pref["id"], "title": pref["title"], "reason": live.get(pref["id"], {}).get("reason") or "not_orderable"}
                notes.append(f"The preferred product {pref['title']} cannot be ordered for {nice_day(day)}: {why}.")
        if full:
            add("lowest_price", [line(full[0], full[0][2]["units_needed"])], "Cheapest product that meets every requirement and covers the full quantity on the requested date.")
            rel = max(full, key=lambda c: (c[0].get("reliability") or 0, -c[1]["price"] / c[2]["content_per_unit"]))
            if rel[0].get("reliability") and rel[0]["id"] != full[0][0]["id"] and (rel[0]["reliability"] or 0) > (full[0][0].get("reliability") or 0):
                add("most_reliable", [line(rel, rel[2]["units_needed"])], f"Best delivery record ({rel[0]['reliability']:.1%} of ordered quantity delivered historically).")
            fin = [c for c in full if c[0].get("origin") == "FI" and "GOODS_FROM_FINLAND_BLUE_SWAN" in c[0]["labels"]]
            if fin:
                add("finnish_origin", [line(fin[0], fin[0][2]["units_needed"])], "Cheapest option carrying the Hyvää Suomesta (Finnish origin) label.")
            if bigger:
                got = bigger[2]["units_needed"] * bigger[2]["content_per_unit"]
                add("bigger_pack", [line(bigger, bigger[2]["units_needed"])], f"A bigger pack costs less in total, but it is {got:g} {need['unit']} where {need['quantity']:g} was asked.")
        elif cands and not pref:
            rest, lines = need["quantity"], []
            for c in cands:
                if rest <= 0:
                    break
                take = min(c[2]["units_can_be_supplied"], math.ceil(rest / c[2]["content_per_unit"]))
                if take > 0:
                    lines.append(line(c, take))
                    rest -= take * c[2]["content_per_unit"]
            add("combined", lines, "No single product covers the quantity; this combines the cheapest ones that meet every requirement." + (f" Still {rest:g} {need['unit']} short." if rest > 0 else ""))
        if pref:
            for o in options:
                diffs = []
                for l in o["line_items"]:
                    d = self.cat.get(l["item"]["id"])
                    if d["id"] != pref["id"] and d.get("temp") != pref.get("temp"):
                        diffs.append(f"{d['title']} is {d.get('temp')} storage; {pref['title']} is {pref.get('temp')}.")
                if diffs:
                    o["differs_from_usual"] = diffs
        used = {l["item"]["id"] for o in options for l in o["line_items"]}
        others = [{"id": c[0]["id"], "title": c[0]["title"], "price_per_" + {"pcs": "piece", "inner": "piece"}.get(key, key or "unit"): round(c[1]["price"] / c[2]["content_per_unit"]), "units_needed": c[2]["units_needed"],
                   "covers_pct": c[2]["covers_pct"], "total_ex_vat": c[2].get("line_total_ex_vat"), "ordered_before": c[0]["id"] in hist, "delivery_reliability": c[0].get("reliability")}
                  for c in cands if c[0]["id"] not in used][:8]
        for o in options:
            del o["_sig"]
        skipped_txt = [{"reason": r, "products": n, "meaning": REASONS.get(r, "pack content unknown, so quantity cannot be computed")} for r, n in skipped.most_common()]
        resp = {"ucp": self._env(), "need": need, "delivery_date": day, "requirements": {k: v for k, v in reqs.items() if v}, "options": options[:5],
                "also_qualifying": others, "not_offered": skipped_txt, "messages": [{"type": "info", "content": n} for n in notes],
                "range_note": "Every product, price and substitute in this answer is from Valio Aimo's own range, as Valio Aimo's systems answer for this account. Nothing comes from outside Valio Aimo."}
        if other_kinds:
            resp["other_kinds"] = [{"kind": k, "products": len(v), "example": {"id": v[0]["id"], "title": v[0]["title"]}} for k, v in list(other_kinds.items())[:8]]
        if pref_off:  # the product asked for cannot be ordered for this day, and why
            resp["preferred_off"] = pref_off
        if named_ids:  # the name in the request is these products, whether or not they can come
            seen = [live[i] for i in named_ids if i in live]
            resp["named_product"] = {"id": named_ids[0], "title": self.cat.get(named_ids[0])["title"], "ids": named_ids, "orderable": any(x.get("orderable") for x in seen),
                                     "gone": bool(seen) and all(x.get("reason") in ("not_deliverable", "sales_block", "product_recalled") for x in seen)}
        if any(fx.get("stale") for fx in live.values()):
            resp["data_stale"] = True
        if not options:
            resp["messages"].append({"type": "warning", "code": "no_option", "content": f"Nothing meeting the requirements can be delivered on {nice_day(day)}."})
        self._logp(principal, trace, "plan", f"Asked for {need.get('quantity'):g} {need.get('unit')} of '{req.get('query', '')}' by {nice_day(day)}. {len(cands)} products qualify; {sum(skipped.values())} were ruled out"
                       + (" (" + ", ".join(f"{n} {r.replace('_', ' ')}" for r, n in skipped.most_common()) + ")" if skipped else "") + f". Returned {len(options[:5])} option(s).",
                       {"need": need, "requirements": resp["requirements"], "options": [{"label": o["label"], "total_ex_vat": o["total_ex_vat"], "authority": o["authority"]["decision"]} for o in options[:5]]})
        self.seller.ride(resp, principal)  # open proposals from Valio Aimo's agent ride along
        return self._prove(resp)

    # -------------------------------------------------------------- checkout
    def _lines(self, items, day, principal, exclude_ref=""):
        """Validate requested lines against live price/stock. Returns (policy_lines, ucp_line_items, errors)."""
        ids = [str((it.get("item") or {}).get("id")) for it in items]
        max_units = ((principal or {}).get("mandate") or {}).get("max_units_per_order") if principal and principal.get("ok") else None
        if max_units is not None:
            requested_units = sum((float(it.get("quantity") or 0) / 10 ** (self.cat.get(pid) or {}).get("scale", 0)) for pid, it in zip(ids, items) if self.cat.get(pid))
            if requested_units > max_units:
                return [], [], [{"type": "error", "code": "mandate_quantity_limit", "severity": "unrecoverable", "path": "$.line_items",
                                 "content": f"Rule M2: this request is over the mandate's {max_units:g} sales-unit per-order limit."}]
        subs = [str((it.get("wholesale") or {}).get("substitutes_for")) for it in items if (it.get("wholesale") or {}).get("substitutes_for")]
        today = (parse_ts(FREEZE_NOW) or datetime.now(TZ)).strftime("%Y-%m-%d")
        if day < today:
            return [], [], [{"type": "error", "code": "delivery_date_passed", "severity": "recoverable", "path": "$.wholesale.delivery_date",
                             "content": f"The delivery date {nice_day(day)} has already passed. Ask for a date from {nice_day(today)} on."}]
        live = self._live(list(dict.fromkeys(ids + subs)), day, principal, exclude_ref)
        plines, out, errors = [], [], []
        for n, it in enumerate(items):
            pid, path = ids[n], f"$.line_items[{n}]"
            d = self.cat.get(pid)
            err = lambda code, text, **x: errors.append({"type": "error", "code": code, "severity": "recoverable", "path": path, "content": text, **({"wholesale": x} if x else {})})
            if not d:
                err("product_not_found", f"Unknown product '{pid}'.")
                continue
            f, qty = live[d["id"]], it.get("quantity")
            if not isinstance(qty, int) or isinstance(qty, bool) or qty < 1:
                err("invalid_quantity", f"Quantity for {d['title']} must be a positive integer number of steps.")
                continue
            units = qty / 10 ** d["scale"]
            label = UNITS.get(d["unit"], (0, d["unit"]))[1]
            qty, units = self.allocation.trim(it, d, f, qty, units, day, principal, exclude_ref, err)  # on allocation: the line is cut to this customer's share (rule L1)
            if not units:
                continue
            if not f["orderable"]:
                err(f["reason"], f"{d['title']} cannot be ordered for {nice_day(day)}: {f.get('said') or REASONS.get(f['reason'], f['reason'])}." + (f" The cut-off was {f['cutoff']}." if f["reason"] == "cutoff_passed" else ""),
                    alternatives=self.substitutes(d["id"], day, principal, 3))
                continue
            if qty % d["increment"]:
                err("invalid_quantity_increment", f"{d['title']} is sold in multiples of {d['increment'] / 10 ** d['scale']:g} {label}.", increment=d["increment"])
                continue
            if f["stock_known"] and units > f["available"]:
                can = max(0, f["available"])
                err("insufficient_stock", f"We can supply {can:g} of your {units:g} {label} of {d['title']} for {nice_day(day)}; the requested quantity cannot be met.", can_supply=can, requested=units, alternatives=self.substitutes(d["id"], day, principal, 3))
                continue
            amount = round(f["price"] * units)
            tax = round(amount * f["tax_rate"] / 100)
            sub = None
            sid = (it.get("wholesale") or {}).get("substitutes_for")
            if sid and self.cat.get(str(sid)):
                sub = self._subinfo(d, f, self.cat.get(str(sid)), live[str(sid)])
            plines.append({"id": d["id"], "title": d["title"], "category": d["category"], "claims": d["claims"], "contains": d["contains"], "may_contain": d["may_contain"], "free_from": d["free_from"], "labels": d["labels"],
                           "units": units, "unit_label": label, "path": path, "substitution": sub, "req": (it.get("wholesale") or {}).get("requirements"), "amount": amount, "tax": tax, "tax_rate": f["tax_rate"], "cutoff": f.get("cutoff")})
            out.append({
                "id": f"li_{n + 1}",
                "item": {"id": d["id"], "title": d["title"], "price": f["price"], **({"list_price": f["list_price"]} if f.get("list_price") else {}), "quantity_unit": self._qunit(d)},
                "quantity": qty,
                "totals": [{"type": "subtotal", "amount": amount}, {"type": "tax", "display_text": f"VAT {f['tax_rate']:g} %", "amount": tax}, {"type": "total", "amount": amount + tax}],
                "wholesale": {"units": units, "order_by": f.get("cutoff"), "logistic_model": f.get("logistic_model"), "availability": self._availability(f), **({"data_stale": True} if f.get("stale") else {}),
                              "contract_price": f.get("contract_price", False), **({"substitutes_for": {k: sub[k] for k in ("for", "for_title", "listed", "price_delta_pct", "per")}} if sub else {}),
                              **({"requirements": plines[-1]["req"]} if plines[-1]["req"] else {}),
                              **{k: v for k, v in self._attrs(d).items() if k in ("gtin", "origin", "claims", "allergens", "labels", "delivery_reliability", "supplier")}},
            })
            self.allocation.mark(plines[-1], out[-1], it, f, day, principal, exclude_ref)
        return plines, out, errors

    def _history(self, principal):
        return self.store.get("history:" + principal["customer"], {}) if principal and principal.get("ok") else {}

    def usual(self, query: str = "", principal=None, trace=None) -> dict:
        """What this account orders, how often and how much at a time. "We need some milk" then gets one proposal to confirm, not three questions."""
        if not principal or not principal.get("ok"):
            raise GatewayError(401, (principal or {}).get("error", "mandate_required"), (principal or {}).get("detail", "An account's order history needs its mandate."))
        hist, query = self._history(principal), (query or "").strip()
        asked = near = None
        if query:
            picked = self.cat.pick(query, {})[3]
            asked, near = set(picked["kinds"]) | set(picked["choices"]), set(picked["other"])

        def row(pid, h):
            d = self.cat.get(pid)
            per, unit = next(((d["content"][k], u) for k, u in (("l", "L"), ("kg", "KG")) if d["content"].get(k)), (pieces(d) or 1, "pcs"))
            return {"id": pid, "title": d["title"], "kind": kind(d), "past_orders": h["orders"], "usual_units": h["typical_units"],
                    "usual_amount": {"quantity": round(h["typical_units"] * per, 2), "unit": unit}}

        have = sorted(((p, h) for p, h in hist.items() if self.cat.get(p)), key=lambda x: -x[1]["orders"])
        mine = [row(p, h) for p, h in have if asked is None or kind(self.cat.get(p)) in asked]
        also = [row(p, h) for p, h in have if near and kind(self.cat.get(p)) in near]
        resp = {"ucp": self._env(), "query": query, "usual": mine[:12], "messages": []}
        if also:
            resp["other_kinds_ordered"] = also[:6]
        if query and not mine:
            resp["messages"].append({"type": "info", "code": "no_history", "content": f"This account has not ordered '{query}' through Valio Aimo before" + (", only other kinds that share the word." if also else ".")})
        self._logp(principal, trace, "usual", (f"Asked what this account usually orders for '{query}'. " if query else "Asked what this account usually orders. ")
                   + (f"{mine[0]['title']}: {mine[0]['past_orders']} past orders, usually {mine[0]['usual_amount']['quantity']:g} {mine[0]['usual_amount']['unit']} at a time." if mine else "Nothing in its history."),
                   {"query": query, "usual": [m["id"] for m in mine[:12]]})
        return self._prove(resp)

    def _cleared(self, principal) -> dict:
        """Products with no allergen declaration that a person cleared on this account: {product: {allergen: {by, at, checkout}}}."""
        return self.store.get("allergen_cleared:" + principal["customer"], {}) if principal and principal.get("ok") else {}

    def _day_auto(self, customer, day, exclude="") -> int:
        """What agents committed for this delivery day with no person involved: placed orders plus
        offers still open. Used by the split-order check."""
        sub = lambda doc: next((t["amount"] for t in doc["totals"] if t["type"] == "subtotal"), 0)
        placed_docs = [json.loads(r[0]) for r in self.store.q("select doc from orders where customer=? order by created desc", (customer,))]
        placed = sum(sub(o) for o in placed_docs
                     if o["wholesale"]["delivery_date"] == day and not o["wholesale"]["evidence"].get("approval"))
        checkout_docs = [json.loads(r[0]) for r in self.store.q("select doc from checkouts where customer=? and status='ready_for_complete' order by created desc", (customer,))]
        open_ = sum(sub(c) for c in checkout_docs
                    if c["id"] != exclude and c["wholesale"]["delivery_date"] == day
                    and not c["wholesale"].get("approval") and parse_ts(c["expires_at"]).timestamp() > time.time())
        return placed + open_

    def _day_placed(self, customer, day) -> int:
        """Everything already placed through agents for this delivery day, approved by a person or not. Valio Aimo's ceiling counts it."""
        docs = [json.loads(r[0]) for r in self.store.q("select doc from orders where customer=? order by created desc", (customer,))]
        return sum(next((t["amount"] for t in o["totals"] if t["type"] == "subtotal"), 0) for o in docs if o["wholesale"]["delivery_date"] == day)

    def _preview(self, lines, day, principal, reqs) -> dict:
        """What the guardrails would say about these lines, without creating anything."""
        plines, _, errors = self._lines(lines, day, principal)
        if errors:
            return {"decision": "invalid", "reasons": [e["content"] for e in errors]}
        subtotal = sum(l["amount"] for l in plines)
        prof = self.store.get("profile:" + principal["customer"], policy.default_profile()) if principal and principal.get("ok") else policy.default_profile()
        prof["global_pause"] = bool(self.store.get("global_pause", False))
        spent = self.store.spent_since(principal["customer"], time.time() - policy.WEEK) if principal and principal.get("ok") else 0
        rules = policy.evaluate(plines, subtotal, principal, prof, self._history(principal), spent, reqs, self._day_auto(principal["customer"], day) if principal and principal.get("ok") else 0, cleared=self._cleared(principal),
                                day_placed=self._day_placed(principal["customer"], day) if principal and principal.get("ok") else 0)
        dec = policy.decision(rules)
        return {"decision": {"auto": "agent_may_order", "escalate": "needs_human_approval", "deny": "not_allowed"}[dec], "reasons": [r["detail"] for r in rules if r["outcome"] != "pass"]}

    def _assemble(self, cid, inputs, principal, trace, internal=None) -> dict:
        day = inputs["delivery_date"]
        reqs = inputs.get("requirements") or {}
        plines, items, errors = self._lines(inputs["line_items"], day, principal, exclude_ref=cid)
        subtotal = sum(l["amount"] for l in plines)
        taxes = Counter()
        for l in plines:
            taxes[l["tax_rate"]] += l["tax"]
        totals = [{"type": "subtotal", "amount": subtotal}] + [{"type": "tax", "display_text": f"VAT {r:g} %", "amount": a} for r, a in sorted(taxes.items())] + [{"type": "total", "amount": subtotal + sum(taxes.values())}]
        messages, rules, dec = list(errors), [], "invalid"
        amends = inputs.get("amends")  # set by after.py: this checkout changes an order that is already placed, and may carry no goods
        standing = (inputs.get("proposal") or {}).get("standing")  # set by seller.py: a standing-order change, which may carry no goods
        if not inputs["line_items"] and not amends and not standing:
            messages.append({"type": "error", "code": "empty_checkout", "severity": "recoverable", "content": "No line items."})
        cust = principal["customer"] if principal and principal.get("ok") else None
        if not errors and (inputs["line_items"] or amends or standing):
            prof = self.store.get("profile:" + cust, policy.default_profile()) if cust else policy.default_profile()
            prof["global_pause"] = bool(self.store.get("global_pause", False))
            spent = self.store.spent_since(cust, time.time() - policy.WEEK) if cust else 0
            rules = policy.evaluate(plines, subtotal, principal, prof, self._history(principal), spent, reqs, self._day_auto(cust, day, cid) if cust else 0, after=amends, proposal=inputs.get("proposal"), cleared=self._cleared(principal),
                                    day_placed=self._day_placed(cust, day) if cust else 0)
            dec = policy.decision(rules)
            for r in rules:
                if r["outcome"] == "escalate":
                    messages.append({"type": "error", "code": "approval_required", "severity": "requires_buyer_review", "content": f"{r['title']}: {r['detail']}", **({"path": r["path"]} if r.get("path") else {})})
                elif r["outcome"] == "deny":
                    hard = r["id"] in ("M1", "W1", "W2", "W4")
                    messages.append({"type": "error", "code": (principal or {}).get("error", "mandate_required") if r["id"] == "M1" else "policy_denied", "severity": "unrecoverable" if hard else "recoverable",
                                     "content": f"{r['title']}: {r['detail']}", **({"path": r["path"]} if r.get("path") else {})})
        messages += [{"type": "warning", "code": "on_allocation", "content": l["allocation"]["reason"], "path": l["path"]} for l in plines if (l.get("allocation") or {}).get("rest")]
        status = {"auto": "ready_for_complete", "escalate": "requires_escalation"}.get(dec, "incomplete")
        cutoffs = [parse_ts(l["cutoff"]) for l in plines if l.get("cutoff")]
        cutoff = min([c for c in cutoffs if c], default=None)
        ttl = HOLD_MINUTES * 60 if status == "ready_for_complete" else 4 * 3600
        expires = time.time() + ttl
        if cutoff and not FREEZE_NOW:
            expires = min(expires, cutoff.timestamp())
        internal = internal or {"approval_token": secrets.token_urlsafe(16), "trace": trace, "created": iso()}
        internal.update({"inputs": inputs, "customer": cust, "principal_token": (principal or {}).get("token")})
        c = {
            "ucp": self._env(), "id": cid, "status": status, "currency": CURRENCY, "line_items": items, "totals": totals, "messages": messages,
            "links": [{"type": "terms_of_service", "url": f"{PUBLIC_URL}/spec#terms", "title": "Agent ordering terms (mock)"}],
            "expires_at": iso(expires),
            "buyer": {"name": (principal or {}).get("buyer_name"), "id": (principal or {}).get("buyer")},
            "wholesale": {
                "delivery_date": day, "order_by": iso(cutoff.timestamp()) if cutoff else None, "ship_to": (principal or {}).get("ship_to"), "customer": cust, "agent": self._who(principal),
                "mandate_id": (principal or {}).get("jti"), "purchase_order": inputs.get("purchase_order"), "intent": inputs.get("intent"), "agent_rationale": inputs.get("rationale"), "requirements": reqs,
                "payment": "Invoice on Valio Aimo's existing account terms. No payment data passes through the agent.",
                "policy": {"decision": dec, "rules": rules, "evaluated_at": iso()}, "approval": None,
                "hold": {"price_and_stock_held_until": iso(expires)} if status != "incomplete" else None,
                **({"amends": amends} if amends else {}),
                **({"proposal": inputs["proposal"]} if inputs.get("proposal") else {}),
            },
        }
        if status == "requires_escalation":
            c["continue_url"] = f"{PUBLIC_URL}/approve/{cid}?k={internal['approval_token']}"
        c["_internal"] = internal
        self.seller.ride(c, principal, exclude=(inputs.get("proposal") or {}).get("id"))
        if status != "incomplete":
            self.store.hold(cid, [(l["id"], l["units"]) for l in plines], expires, (principal or {}).get("world", "main"))
        else:
            self.store.settle(cid, "released")
        return c

    def _sign_checkout(self, c: dict) -> dict:
        body = {k: v for k, v in c.items() if k not in ("ap2", "_internal")}
        c["ap2"] = {"merchant_authorization": sign_detached(body, self.key), "checkout_digest": digest(body)}
        c["_internal"]["digest"] = c["ap2"]["checkout_digest"]
        self.store.save_checkout(c)
        return c

    @staticmethod
    def public(c: dict) -> dict:
        return {k: v for k, v in c.items() if k != "_internal"}

    def _line_extras(self, it: dict) -> dict:
        """What an agent may say about one line: which product it stands in for, and requirements that apply to this line only."""
        w, out = it.get("wholesale") or {}, {}
        if w.get("substitutes_for"):
            out["substitutes_for"] = str(w["substitutes_for"])
        if w.get("rest") == "later":  # on allocation: wait for released shares for what the offer could not cover
            out["rest"] = "later"
        r = it.get("requirements") or w.get("requirements") or {}
        r = {"claims": self.cat.canon_claims(r.get("claims")), "exclude_allergens": self.cat.canon_allergens(r.get("exclude_allergens")), "labels": self.cat.canon_labels(r.get("labels"))}
        if any(r.values()):
            out["requirements"] = {k: v for k, v in r.items() if v}
        return {"wholesale": out} if out else {}

    def _inputs(self, req: dict) -> dict:
        raw_w = req.get("wholesale")
        if raw_w is not None and not isinstance(raw_w, dict):
            raise GatewayError(400, "invalid_checkout", "wholesale and wholesale.requirements must be objects.")
        w = raw_w or {}
        raw_items = req.get("line_items") or []
        if not isinstance(raw_items, list) or any(not isinstance(it, dict) for it in raw_items):
            raise GatewayError(400, "invalid_line_items", "line_items must be a list of objects.")
        if not isinstance(w, dict):
            raise GatewayError(400, "invalid_checkout", "wholesale and wholesale.requirements must be objects.")
        r = w.get("requirements") or {}
        if not isinstance(r, dict):
            raise GatewayError(400, "invalid_checkout", "wholesale and wholesale.requirements must be objects.")
        raw_context = req.get("context")
        if raw_context is not None and not isinstance(raw_context, dict):
            raise GatewayError(400, "invalid_checkout", "context must be an object.")
        context = raw_context or {}
        for it in raw_items:
            if not isinstance(it.get("item"), dict) or not isinstance(it.get("wholesale") or {}, dict) or not isinstance(it.get("requirements") or (it.get("wholesale") or {}).get("requirements") or {}, dict):
                raise GatewayError(400, "invalid_line_items", "Each line needs an item object and object-valued wholesale requirements.")
        return {
            "line_items": [{"item": {"id": str((it.get("item") or {}).get("id"))}, "quantity": it.get("quantity"), **self._line_extras(it)} for it in raw_items],
            "delivery_date": self._day(w.get("delivery_date")),
            "requirements": {"claims": self.cat.canon_claims(r.get("claims")), "exclude_allergens": self.cat.canon_allergens(r.get("exclude_allergens")), "labels": self.cat.canon_labels(r.get("labels"))},
            "purchase_order": w.get("purchase_order"), "intent": context.get("intent") or w.get("intent"),
            "rationale": str(w.get("rationale") or "")[:600] or None,
        }

    def _log_checkout(self, c, principal, verb):
        w, t = c["wholesale"], c["totals"]
        total = next((x["amount"] for x in t if x["type"] == "subtotal"), 0)
        lines = "; ".join(f"{l['wholesale']['units']:g} x {l['item']['title']}" for l in c["line_items"]) or "no valid lines"
        tr = c["_internal"].get("trace")
        self.agent_note(principal)
        self.store.log(tr, self._who(principal), "checkout", f"{verb} checkout for delivery {nice_day(w['delivery_date'])}: {lines}. {policy.eur(total)} ex VAT.", {"line_items": [(l["item"]["id"], l["quantity"]) for l in c["line_items"]], "subtotal": total}, cid=c["id"])
        for m in c["messages"]:
            if m.get("severity") == "recoverable":
                self.store.log(tr, "gateway", "exception", m["content"], {"code": m["code"], "alternatives": [a["id"] for a in (m.get("wholesale") or {}).get("alternatives", [])]}, cid=c["id"])
        rules = w["policy"]["rules"]
        if rules:
            dec = w["policy"]["decision"]
            head = {"auto": "Within the agent's authority. No person needed.", "escalate": "Outside the agent's authority. Sent to a person for sign-off.", "deny": "Refused."}[dec]
            self.store.log(tr, "gateway", "policy", head + " " + " ".join(f"[{r['id']} {r['outcome']}] {r['detail']}" for r in dict((r["id"], r) for r in rules if r["outcome"] != "pass" or r["id"] in ("B1", "A2", "S1", "B0", "D1", "P1", "R1", "L1")).values()),
                           {"decision": dec, "rules": rules, "offer_signature": c["ap2"]["merchant_authorization"][-24:], "held_until": c["expires_at"]}, cid=c["id"])
            if dec == "escalate":
                self.agent_note(principal, "asked")
            elif dec == "deny" and any(r["outcome"] == "deny" and r["id"] in STRIKE_RULES for r in rules):
                self.agent_note(principal, "strike", cid=c["id"], trace=tr)

    def create_checkout(self, req: dict, principal=None, trace=None) -> dict:
        cid = "chk_" + secrets.token_hex(6)
        c = self._sign_checkout(self._assemble(cid, self._inputs(req), principal, trace))
        self._log_checkout(c, principal, "Created")
        return self.public(c)

    def _load(self, cid, principal=None):
        c = self.store.checkout(cid)
        if not c:
            raise GatewayError(404, "not_found", "No such checkout.")
        if principal and principal.get("ok") and c["_internal"].get("customer") not in (None, principal["customer"]):
            raise GatewayError(403, "forbidden", "This checkout belongs to another account.")
        return c

    def get_checkout(self, cid, principal=None) -> dict:
        c = self._load(cid, principal)
        if c["status"] in ("ready_for_complete", "requires_escalation") and parse_ts(c["expires_at"]).timestamp() < time.time():
            c["status"] = "incomplete"
            c["messages"].append({"type": "error", "code": "offer_expired", "severity": "recoverable", "content": "The price and stock hold has expired. Update the checkout to get a fresh signed offer."})
            c["wholesale"]["hold"] = None
            self.store.settle(cid, "released")
            self._sign_checkout(c)
            self.store.log(c["_internal"].get("trace"), "gateway", "expired", "The signed offer expired; stock released.", cid=cid)
        return self.public(c)

    def update_checkout(self, cid, req, principal=None, trace=None) -> dict:
        old = self._load(cid, principal)
        if old["status"] in ("completed", "canceled", "complete_in_progress"):
            raise GatewayError(409, "invalid_state", f"Checkout is {old['status']}.")
        if old["_internal"]["inputs"].get("proposal"):
            raise GatewayError(409, "invalid_state", "This checkout accepts a proposal from Valio Aimo's agent and cannot be edited. Cancel it, or decline the proposal.")
        if old["_internal"]["inputs"].get("amends"):
            raise GatewayError(409, "invalid_state", "This checkout settles an issue on a placed order and cannot be edited. Cancel it and choose another way out with resolve_order_issue.")
        c = self._sign_checkout(self._assemble(cid, self._inputs(req), principal, trace or old["_internal"].get("trace"), internal=old["_internal"]))
        self._log_checkout(c, principal, "Updated")
        return self.public(c)

    def cancel_checkout(self, cid, principal=None) -> dict:
        c = self._load(cid, principal)
        if c["status"] == "completed":
            raise GatewayError(409, "invalid_state", "Checkout is already completed.")
        c["status"] = "canceled"
        self.store.settle(cid, "released")
        self._sign_checkout(c)
        self.store.log(c["_internal"].get("trace"), self._who(principal), "canceled", "Checkout canceled; stock released.", cid=cid)
        return self.public(c)

    # --------------------------------------------------------------- approval
    def approval_view(self, cid, token) -> dict:
        c = self._load(cid)
        if not secrets.compare_digest(token or "", c["_internal"]["approval_token"]):
            raise GatewayError(403, "forbidden", "This approval link is not valid.")
        v = self.public(self.store.checkout(cid)) | {"_events": self.store.events(ref=c["_internal"].get("trace"), cid=cid), "_asked_by": c["_internal"].get("asked_by") or c["wholesale"].get("agent")}
        return v | {"_names_fi": self._names_fi(v)}

    def _names_fi(self, v) -> list[dict]:
        """The Finnish catalogue name (names.fi) of every product the approval page names: its lines, what a line replaces,
        the lines the allergen rule could not check. The page shows these in its Finnish view; {id, title, fi}."""
        seen, out = set(), []
        def add(pid, title=None):
            d = (self.cat.get(str(pid)) or {}) if pid else {}
            fi, en = (d.get("names") or {}).get("fi"), title or d.get("title")
            if fi and en and (str(pid), en) not in seen:
                seen.add((str(pid), en))
                out.append({"id": str(pid), "title": en, "fi": fi})
        for l in v.get("line_items") or []:
            add(l["item"]["id"], l["item"].get("title"))
            sub = (l.get("wholesale") or {}).get("substitutes_for") or {}
            add(sub.get("for"), sub.get("for_title"))
        for r in ((v.get("wholesale") or {}).get("policy") or {}).get("rules") or []:
            for u in r.get("undeclared") or []:
                add(u.get("id"), u.get("title"))
        st = ((v.get("wholesale") or {}).get("proposal") or {}).get("standing") or {}
        add(st.get("id") or st.get("product"), st.get("title"))
        return out

    @staticmethod
    def clears_of(c) -> list[dict]:
        """What approving this checkout clears: each product with no allergen declaration, with the excluded allergens it is cleared for."""
        out = {}
        for r in c["wholesale"]["policy"]["rules"]:
            for u in r.get("undeclared") or []:
                e = out.setdefault(u["id"], {"id": u["id"], "title": u["title"], "codes": []})
                e["codes"] = sorted(set(e["codes"]) | set(u["codes"]))
        return [{**out[k], "what": ", ".join(ALLERGENS.get(x, x) for x in out[k]["codes"])} for k in sorted(out)]

    def check_clears(self, c, sent) -> list[dict]:
        """The approver ticks each line with no allergen declaration on its own. The list they send must be exactly the
        product and allergen pairs the rule asked about: one line short, or one line more, and nothing is approved."""
        want = self.clears_of(c)
        pairs = lambda rows: {(str(x.get("id")), str(code)) for x in rows if isinstance(x, dict) for code in (x.get("codes") if isinstance(x.get("codes"), list) else [])}
        if not isinstance(sent, list):
            sent = []
        missing, extra = pairs(want) - pairs(sent), pairs(sent) - pairs(want)
        if extra:
            raise GatewayError(400, "clears_mismatch", "This approval names a product or allergen that the allergen rule did not ask about on this order. Nothing was approved.")
        if missing:
            left = len({i for i, _ in missing})
            raise GatewayError(400, "clears_mismatch", f"{left} of the {len(want)} lines with no allergen declaration {'was' if left == 1 else 'were'} not confirmed. Each one is ticked on its own, or the order is rejected. Nothing was approved.")
        return want

    @staticmethod
    def _material(c) -> tuple:
        """What an approver says yes to: the lines, the money and what each rule answered."""
        return (next((t["amount"] for t in c["totals"] if t["type"] == "total"), 0), c["status"],
                sorted((l["item"]["id"], l["quantity"]) for l in c["line_items"]),
                sorted((r["id"], r["outcome"], r.get("path") or "") for r in c["wholesale"]["policy"]["rules"] if r["outcome"] != "pass"))

    def set_asker(self, cid, who: str):
        """Name the person who asked for this checkout (the menu page does), so the record keeps who asked apart from who approved."""
        c = self._load(cid)
        c["_internal"]["asked_by"] = str(who)[:80]
        self.store.save_checkout(c)

    def decide(self, cid, token, approve: bool, approver: str, note: str = "", device: dict | None = None, clears: list | None = None, demo: bool = False) -> dict:
        c = self._load(cid)
        if not secrets.compare_digest(token or "", c["_internal"]["approval_token"]):
            raise GatewayError(403, "forbidden", "This approval link is not valid.")
        self.get_checkout(cid)
        c = self._load(cid)
        if c["status"] != "requires_escalation":
            raise GatewayError(409, "invalid_state", f"Nothing to decide: checkout is {c['status']}.")
        approver = str(approver or "").strip()
        if approve and not approver:
            raise GatewayError(400, "approver_required", "An approval carries the name of the person who gives it. Nothing was approved.")
        if approve:
            # The rules are run again at the moment of the yes: a cap lowered, an agent suspended, a week's budget used up
            # or a price moved since the offer was signed all show up here, and a person is never asked to approve yesterday's offer.
            fresh = self._assemble(cid, c["_internal"]["inputs"], self.identify(c["_internal"].get("principal_token")), c["_internal"].get("trace"), internal=dict(c["_internal"]))
            if self._material(fresh) != self._material(c):
                self._sign_checkout(fresh)
                now_says = [r["detail"] for r in fresh["wholesale"]["policy"]["rules"] if r["outcome"] == "deny"] or [r["detail"] for r in fresh["wholesale"]["policy"]["rules"] if r["outcome"] == "escalate"]
                self.store.log(c["_internal"].get("trace"), "gateway", "exception", "The rules were run again when the approver said yes, and the answer was no longer the one on the offer. Nothing was approved; the offer was replaced by a fresh one.", cid=cid)
                raise GatewayError(409, "offer_changed", "Nothing was approved: this offer changed after it was signed. " + ("It can no longer be ordered: " if fresh["status"] == "incomplete" else "It now needs no approval. " if fresh["status"] == "ready_for_complete" else "The page now shows the current one: ")
                                   + (" ".join(now_says[:3]) if fresh["status"] != "ready_for_complete" else ""))
        rec = {"decision": "approved" if approve else "rejected", "by": approver or "Someone holding the link", "at": iso(), "note": note, "checkout_digest": c["_internal"]["digest"],
               "overrides": [r["id"] for r in c["wholesale"]["policy"]["rules"] if r["outcome"] == "escalate"],
               "asked_by": c["_internal"].get("asked_by") or c["wholesale"].get("agent") or "the buyer's agent"}
        cleared = []
        if approve:
            # A device-signed approval, or one that sends a list, names each line it clears and must match the rule exactly.
            # A plain link approval through the API with no list (test scripts, the floor's approver scripts) clears them all at once, and the record says so.
            listed = (device or {}).get("signed", {}).get("clears") if device else clears
            cleared = self.check_clears(c, listed) if device or clears is not None else self.clears_of(c)
            if cleared:
                rec["clears"] = cleared
                rec["clears_confirmed"] = "each line ticked by the approver" if device or clears is not None else "all at once, by link through the API"
        if device:  # the approver's own device signed this decision (gateway/passkey.py checked it); the proof is kept with the approval
            rec["device"] = device
        if demo and approve:  # one person on one device looked at the approver's side: part of the signed record, so no later page can show it as a real second person
            rec["demo"] = "Demo approval: given in the browser that asked for the order, through the approval page's demo view. No second person or device was involved."
        # The gateway's own record of the decision. Where no approver device is enrolled, this server-held key stands in for it.
        akey = self.keys((self.account_of(c["_internal"].get("customer")) or {}).get("slug", STAGE))["approver"]
        rec["signature"] = {"kid": akey.kid, "jws": sign_detached(dict(rec), akey)}
        c["wholesale"]["approval"] = rec
        c["messages"] = [m for m in c["messages"] if m.get("code") != "approval_required"]
        c.pop("continue_url", None)
        tr = c["_internal"].get("trace")
        what = f"the change to order {c['wholesale']['amends']['order_label']}" if c["wholesale"].get("amends") else "the order"
        if approve:
            c["status"] = "ready_for_complete"
            exp = time.time() + 60 * 60
            cutoff = parse_ts(c["wholesale"].get("order_by"))
            exp = min(exp, cutoff.timestamp()) if cutoff and not FREEZE_NOW else exp
            c["expires_at"] = iso(exp)
            c["wholesale"]["hold"] = {"price_and_stock_held_until": iso(exp)}
            signed_how = f"{device['device']}, {device.get('how') or 'a passkey'}, key {device['thumbprint']}" if device else ""
            c["messages"].append({"type": "info", "code": "approved", "content": f"Approved by {rec['by']} at {rec['at']}" + (f", signed on their own device ({signed_how})." if device else " (demo approval, given in the browser that asked)." if rec.get("demo") else ".")})
            self.store.q("update holds set expires=? where ref=? and state='held'", (exp, cid))
            # A person saw the lines with no allergen declaration and said yes: the account remembers it, so the same product is not asked about twice.
            cust = c["_internal"].get("customer")
            if cleared and cust:
                seen = self.store.get("allergen_cleared:" + cust, {})
                for u in cleared:
                    seen.setdefault(u["id"], {}).update({code: {"by": rec["by"], "at": rec["at"], "checkout": cid} for code in u["codes"]})
                self.store.put("allergen_cleared:" + cust, seen)
            names = lambda codes: ", ".join(ALLERGENS.get(x, x) for x in codes)
            cleared_text = (f" Cleared, {rec['clears_confirmed']}: " + "; ".join(f"{u['title']} for {names(u['codes'])}" for u in cleared[:12]) + (f" and {len(cleared) - 12} more" if len(cleared) > 12 else "") + ".") if cleared else ""
            self.store.log(tr, f"human:{rec['by']}", "approved", f"{rec['by']} approved {what}, overriding {', '.join(rec['overrides'])}. Asked by {rec['asked_by']}." + (f" Signed on their own device ({signed_how})." if device else f" {rec['demo']} The gateway's key signed the record." if rec.get("demo") else " No approver device is enrolled on this account, so the link approved and the gateway's key signed the record.")
                           + cleared_text + (f" Note: {note}" if note else ""), {"approval": rec}, cid=cid)
        else:
            c["status"] = "canceled"
            self.store.settle(cid, "released")
            c["messages"].append({"type": "info", "code": "rejected", "content": f"Rejected by {rec['by']}." + (f" {note}" if note else "")})
            self.store.log(tr, f"human:{rec['by']}", "rejected", f"{rec['by']} rejected {what}." + (f" Note: {note}" if note else ""), {"approval": rec}, cid=cid)
        return self.public(self._sign_checkout(c))

    # --------------------------------------------------------------- complete
    def agent_proof(self, checkout: dict, mandate_token: str) -> str:
        """What the agent's harness does at commit time (demo key): sign the exact terms it accepts."""
        _, claims = jwt_peek(mandate_token)
        return jwt_encode({"iss": claims["sub"], "checkout_id": checkout["id"], "checkout_digest": checkout["ap2"]["checkout_digest"],
                           "merchant_authorization": checkout["ap2"]["merchant_authorization"], "mandate_jti": claims["jti"], "iat": now()},
                          self.keys((self.store.get("buyer:" + claims["iss"]) or {}).get("slug", STAGE))["agent"], typ="checkout-mandate+jwt")

    def complete_checkout(self, cid, req, principal=None) -> dict:
        self.get_checkout(cid, principal)
        c = self._load(cid, principal)
        tr = c["_internal"].get("trace")
        fail = lambda code, text, sev="recoverable": self.public(c) | {"messages": c["messages"] + [{"type": "error", "code": code, "severity": sev, "content": text}]}
        if c["status"] == "completed":
            return self.public(c)
        proof = ((req or {}).get("ap2") or {}).get("checkout_mandate")
        # Who is asking comes before whether the offer is ready: a commit without the agent's key, or over other terms, is refused and counted
        # against the agent in every state of the offer, also while it waits for a person.
        if principal and principal.get("ok") and principal.get("agent_jwk"):
            claims = jwt_verify(proof, principal["agent_jwk"]) if proof else None
            if not claims:
                self.store.log(tr, "gateway", "refused", "Refused to complete: the request was not signed with the agent key the mandate is bound to.", cid=cid)
                self.agent_note(principal, "strike", cid=cid, trace=tr)
                return fail("agent_missing_key" if not proof else "mandate_invalid_signature", "The mandate is bound to an agent key. Sign the checkout digest with it (ap2.checkout_mandate).")
            if claims.get("checkout_digest") != c["_internal"]["digest"] or claims.get("checkout_id") != cid:
                self.store.log(tr, "gateway", "refused", "Refused to complete: the agent signed different terms than the current signed offer.", cid=cid)
                self.agent_note(principal, "strike", cid=cid, trace=tr)
                return fail("mandate_scope_mismatch", "The signed digest does not match the current checkout. Fetch it again and sign that.")
        if c["status"] != "ready_for_complete":
            return fail("invalid_state", f"Checkout is '{c['status']}', not ready_for_complete." + (" A person must approve it first at continue_url." if c["status"] == "requires_escalation" else ""))
        if not principal or not principal.get("ok"):
            return fail((principal or {}).get("error", "mandate_required"), (principal or {}).get("detail", "Completing an order needs a valid mandate."), "unrecoverable")
        # Re-check live price and stock at the moment of commitment.
        fresh = self._assemble(cid, c["_internal"]["inputs"], principal, tr, internal=dict(c["_internal"]))
        ft = next(x["amount"] for x in fresh["totals"] if x["type"] == "total")
        ct = next(x["amount"] for x in c["totals"] if x["type"] == "total")
        if fresh["status"] == "incomplete" or ft != ct:
            fresh["wholesale"]["approval"] = None
            self._sign_checkout(fresh)
            self.store.log(tr, "gateway", "exception", "Price or stock changed between the offer and the commit. Sent the agent a fresh signed offer instead of ordering.", cid=cid)
            return self.public(fresh) | {"messages": fresh["messages"] + [{"type": "warning", "code": "offer_changed", "content": "Price or availability changed since the offer was signed. Review this fresh offer."}]}
        if not principal.get("agent_jwk"):
            # An assistant with no key cannot sign. The gateway records what it accepted, as a receipt the seller signs.
            proof = None
            c["_internal"]["keyless_receipt"] = self._prove({"type": "keyless_acceptance", "agent": principal["agent"], "mandate_jti": principal["jti"], "checkout_id": cid, "checkout_digest": c["_internal"]["digest"], "at": iso()})
        c["_internal"]["accepted_offer"] = json.loads(json.dumps({k: v for k, v in c.items() if k not in ("ap2", "_internal")}))
        # A change to a placed order that brings no new goods becomes a signed amendment, not an order.
        order = self.after.settle(c, principal, proof) if c["wholesale"].get("amends") and not c["line_items"] else \
            self.seller.change_standing(c, principal, proof) if (c["wholesale"].get("proposal") or {}).get("standing") and not c["line_items"] else self._order(c, principal, proof)
        c["status"] = "completed"
        c["order"] = {"id": order["id"], "label": order["label"], "permalink_url": order["permalink_url"]}
        c["wholesale"]["hold"] = None
        self.store.settle(cid, "ordered")
        self._sign_checkout(c)
        return self.public(c)

    def _order(self, c, principal, proof) -> dict:
        n = self.store.next_counter("order_label_counter")
        oid = "ord_" + secrets.token_hex(6)
        w = c["wholesale"]
        subtotal = next(x["amount"] for x in c["totals"] if x["type"] == "subtotal")
        o = {
            "ucp": self._env(), "id": oid, "label": f"VA-AG-{datetime.now(TZ):%y%m%d}-{n:04d}", "checkout_id": c["id"], "permalink_url": order_url(oid),
            "line_items": [{"id": l["id"], "item": l["item"], "quantity": {"original": l["quantity"], "total": l["quantity"], "fulfilled": 0}, "totals": l["totals"], "status": "processing",
                            "wholesale": {**l["wholesale"], "lot": self.after.lot(l["item"]["id"], w["delivery_date"], principal.get("world", "main"))}} for l in c["line_items"]],
            "fulfillment": {"expectations": [{"id": "exp_1", "line_items": [{"id": l["id"], "quantity": l["quantity"]} for l in c["line_items"]], "method_type": "shipping",
                                              "description": f"Delivery {nice_day(w['delivery_date'])} to {w.get('ship_to') or 'the account address'}"}], "events": []},
            "currency": c["currency"], "totals": c["totals"], "messages": [{"type": "info", "content": "Placed by an AI agent under a buyer mandate." + (f" Approved by {w['approval']['by']}." if w.get("approval") else " No human approval was needed.")}],
            "wholesale": {
                "delivery_date": w["delivery_date"], "ship_to": w.get("ship_to"), "customer": w["customer"], "buyer": c["buyer"], "placed_by": w["agent"], "channel": "agent", "purchase_order": w.get("purchase_order"), "intent": w.get("intent"),
                "placed_at": iso(), "payment": w["payment"],
                "evidence": {"mandate_id": w["mandate_id"], "mandate": principal["token"], "offer_digest": c["_internal"]["digest"], "offer_signature": c["ap2"]["merchant_authorization"],
                             "agent_acceptance": proof, "agent_class": "key" if proof else "keyless", "keyless_receipt": c["_internal"].get("keyless_receipt"), "approval": w.get("approval"), "policy": w["policy"],
                             # whose hands the buyer's signing key is in: "buyer" = the company registered its own public key and this gateway never held the private half
                             "mandate_key": principal.get("mandate_key")},
                "documents": {"peppol_order": order_url(oid, "/peppol/order.xml"), "peppol_order_response": order_url(oid, "/peppol/order-response.xml"), "audit": f"{PUBLIC_URL}/orders/{oid}/audit"},
            },
        }
        if w.get("amends"):  # a linked order: it replaces goods on an order placed earlier
            o["wholesale"]["amends"] = w["amends"]
            o["messages"].append({"type": "info", "code": "amends_order", "content": f"Replaces goods on order {w['amends']['order_label']} ({w['amends']['title']})."})
        if w.get("proposal"):  # made from a proposal by Valio Aimo's agent
            o["wholesale"]["proposal"] = w["proposal"]
        self._prove(o)
        self.store.save_order(o, w["customer"], subtotal)
        hist = self.store.get("history:" + w["customer"], {})
        for l in c["line_items"]:
            h = hist.setdefault(l["item"]["id"], {"orders": 0, "typical_units": l["wholesale"]["units"]})
            h["orders"] += 1
        self.store.put("history:" + w["customer"], hist)
        self.agent_note(principal, "order")
        # Who, if anyone, said yes: an approver, and in the shop chat the person in the shop who pressed the button. Only an order nobody touched says so.
        ap, in_shop = w.get("approval") or {}, str(w.get("agent") or "").endswith("/shop-assistant")
        who = ", ".join(x for x in [(f"approved by {ap.get('by')}" + (" (a demo approval, in the browser that asked)" if ap.get("demo") else "")) if ap else None,
                                    "confirmed in the shop by the person who asked" if in_shop else None] if x) or "no human involved"
        self.store.log(c["_internal"].get("trace"), w["agent"], "order", f"Order {o['label']} placed: {policy.eur(subtotal)} ex VAT, delivery {nice_day(w['delivery_date'])}, {who}. Signed by the agent and by Valio Aimo.",
                       {"order_id": oid, "label": o["label"], "subtotal": subtotal, "evidence": {"mandate_id": w["mandate_id"], "offer_digest": c["_internal"]["digest"], "agent_signed": bool(proof), "approval": bool(w.get("approval"))}}, cid=c["id"])
        if w.get("amends"):
            self.after.settle(c, principal, proof, linked=o)
        if w.get("proposal"):
            self.seller.settled(c, o, principal)
        return o

    def get_order(self, oid) -> dict:
        o = self.store.order(oid)
        if not o:
            raise GatewayError(404, "not_found", "No such order.")
        return o

    def audit(self, oid) -> dict:
        o = self.get_order(oid)
        c = self.store.checkout(o["checkout_id"])
        ok, n, bad = self.store.verify_chain()
        buyer = self.store.get("buyer:" + str(o["wholesale"]["buyer"].get("id")), {})
        tok = (o["wholesale"].get("evidence") or {}).get("mandate")
        own = next((k["jwk"] for k in self.buyer_keys(o["wholesale"]["buyer"].get("id")) if tok and jwt_verify(tok, k["jwk"]) is not None), None)
        if own:  # the mandate behind this order was signed with a key the buyer registered itself
            buyer = {**buyer, "jwk": own}
        return {"order": o["id"], "label": o["label"], "events": self.after.events(o, self.store.events(ref=c["_internal"].get("trace"), cid=c["id"])), "ledger": {"intact": ok, "events_checked": n, "first_bad_seq": bad},
                "offer": c["_internal"].get("accepted_offer"), "keys": {"seller": self.key.jwk, "buyer": buyer.get("jwk"), "approver": buyer.get("approver_jwk")},
                "after": self.after.for_order(o)}  # issues raised after the order, the signed amendments and linked orders
