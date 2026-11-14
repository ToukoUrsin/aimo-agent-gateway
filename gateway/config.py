"""Paths and settings. Secrets come from .env (not committed)."""
import os
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent.parent


def _load_env() -> None:
    f = ROOT / ".env"
    if f.exists():
        for line in f.read_text().splitlines():
            if "=" in line and not line.lstrip().startswith("#"):
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip())


_load_env()

DATA = Path(os.environ.get("AIMO_DATA") or ROOT / "data")  # AIMO_DATA: a separate state dir, e.g. for a second test instance
DATA.mkdir(parents=True, exist_ok=True)
PORTAL = ROOT / "portal"
WEB = ROOT / "web"

AIMO_BASE = os.environ.get("AIMO_BASE", "https://junction26.valioaimo.fi")
AIMO_KEY = os.environ.get("AIMO_API_KEY", "")
PUBLIC_URL = os.environ.get("PUBLIC_URL", "http://localhost:8642").rstrip("/")
PORT = int(os.environ.get("PORT", "8642"))

UCP_VERSION = "2026-08-25"
EXT = "fi.valioaimo.wholesale"  # vendor-namespaced UCP extension
TZ = ZoneInfo("Europe/Helsinki")
CURRENCY = "EUR"

# The mock price/availability API ignores the customer number; a real one would not.
DEMO_CUSTOMER = "100482"
STAGE = "nordic-harbour"  # the account used on stage; every visitor gets a sandbox account of their own
# Whoever holds this key runs the stage account and Valio Aimo's side. Empty = nobody can (visitors still get sandboxes).
OPERATOR_KEY = os.environ.get("OPERATOR_KEY", "")
# Optional fixed "now" (ISO) for cut-off checks, for demos after the mock API's only cut-off (Sun 15 Nov 17:00) has passed.
FREEZE_NOW = os.environ.get("AIMO_FREEZE_NOW", "")
HOLD_MINUTES = 20  # how long a signed offer holds price and stock


def order_key(oid: str) -> str:
    """The key in an order's link. An agent gets the link with its confirmation and hands it to a person, who has no session here:
    the link itself is the permission to read that one order, like the approval link. It is derived, so nothing extra is stored."""
    import hashlib, hmac
    return hmac.new((OPERATOR_KEY or "dev").encode(), ("order:" + oid).encode(), hashlib.sha256).hexdigest()[:20]


def order_url(oid: str, path: str = "") -> str:
    return f"{PUBLIC_URL}/orders/{oid}{path}?k={order_key(oid)}"
