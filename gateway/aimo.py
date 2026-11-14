"""Client for Valio Aimo's mock price and availability APIs.

Only ever called for products a request has already narrowed down to (the API docs
ask for that), with a short cache so one agent conversation does not re-query.
"""
import threading
import time
import os
from datetime import datetime

import httpx

from .config import AIMO_BASE, AIMO_KEY, FREEZE_NOW, TZ

TTL = float(os.environ.get("AIMO_TTL", "45"))
UNLIMITED = 999_999  # Terminal products report 999999, unlimited Stock 9999999999


class AimoError(Exception):
    def __init__(self, status=503, code="upstream_unavailable", message="Valio Aimo is temporarily unavailable"):
        super().__init__(message)
        self.status, self.code, self.message = status, code, message


class Aimo:
    def __init__(self):
        self.http = httpx.Client(base_url=AIMO_BASE, headers={"X-API-Key": AIMO_KEY}, timeout=25)
        self.cache: dict[tuple, tuple[float, dict]] = {}
        self.lock = threading.Lock()
        self.calls = 0

    def _post(self, path, ids, day, customer):
        """One upstream call. A rate limit or a hiccup upstream is waited out here, briefly, so one busy second at Valio Aimo
        does not fail a kitchen's order. What is still failing after that goes back to the caller as a retryable error."""
        err = None
        for wait in (0, 0.6, 1.8, 4.0):
            if wait:
                time.sleep(wait)
            self.calls += 1
            try:
                r = self.http.post(path, json={"customer": customer, "requestedDate": day, "products": ids})
            except httpx.HTTPError:
                err = AimoError(message="Valio Aimo could not be reached; retry the request.")
                continue
            if r.status_code == 429:
                err = AimoError(code="upstream_rate_limited", message="Valio Aimo is rate limiting this gateway; retry the request.")
                continue
            if r.status_code >= 500:
                err = AimoError(message="Valio Aimo is temporarily unavailable; retry the request.")
                continue
            if r.status_code >= 400:
                raise AimoError(status=503, code="upstream_error", message="Valio Aimo rejected the gateway request; retry the request.")
            return r.json()
        raise err

    def quote(self, ids: list[str], day: str, customer: str) -> dict[str, dict]:
        """{product_id: {price, avail, time_now}} for the requested delivery day."""
        ids = list(dict.fromkeys(ids))
        out, missing, t = {}, [], time.time()
        with self.lock:
            for i in ids:
                hit = self.cache.get((i, day, customer))
                if hit and t - hit[0] < TTL:
                    out[i] = hit[1]
                else:
                    missing.append(i)
        for k in range(0, len(missing), 1000):
            chunk = missing[k : k + 1000]
            try:
                av = self._post("/api/availability", chunk, day, customer)
                pr = self._post("/api/price", chunk, day, customer)
            except AimoError:
                # A cached quote is still useful during an upstream outage.  It is
                # deliberately marked stale so a caller can decide whether to retry.
                with self.lock:
                    cached = {i: self.cache.get((i, day, customer)) for i in chunk}
                missing_cache = [i for i, hit in cached.items() if not hit]
                if missing_cache:
                    raise
                for i, hit in cached.items():
                    rec = dict(hit[1])
                    rec["stale"] = True
                    rec["cached_at"] = hit[0]
                    out[i] = rec
                continue
            prices = {p["productId"]: p for p in pr.get("products", [])}
            for a in av.get("products", []):
                rec = {"avail": a, "price": prices.get(a["productId"]), "time_now": av.get("timeNow"), "stale": False}
                out[a["productId"]] = rec
                with self.lock:
                    self.cache[(a["productId"], day, customer)] = (t, rec)
        return out


def parse_ts(s: str | None):
    if not s:
        return None
    try:
        d = datetime.fromisoformat(s)
        return d if d.tzinfo else d.replace(tzinfo=TZ)
    except ValueError:
        return None


def facts(rec: dict | None, day: str, reserved: float = 0.0) -> dict:
    """Flatten one product's live price + availability into the facts the gateway reasons on."""
    f = {"orderable": False, "reason": "no_data", "available": 0.0, "stock_known": False}
    if not rec:
        return f
    a, p = rec.get("avail") or {}, rec.get("price") or {}
    f["stale"] = bool(rec.get("stale"))
    now = parse_ts(FREEZE_NOW) or parse_ts(rec.get("time_now")) or datetime.now(TZ)
    lines = a.get("scheduleLines") or []
    f["logistic_model"] = a.get("logisticModel")
    f["cutoff"] = a.get("closingTime")
    if p and not p.get("error") and p.get("catalogPrice"):
        cp = p["catalogPrice"]
        f["price"] = round(cp["value"] * 100)  # per sales unit, ex VAT, minor units
        f["price_valid_to"] = cp.get("validTo")
        f["tax_rate"] = (p.get("tax") or {}).get("taxRate", 0.0)
        f["contract_price"] = bool(p.get("netPriceFound"))
        if p.get("listPrice"):
            f["list_price"] = round(p["listPrice"]["value"] * 100)
        if p.get("comparisonPrice") and p["comparisonPrice"].get("value"):
            f["comparison_price"] = round(p["comparisonPrice"]["value"] * 100)
            f["comparison_unit"] = p.get("comparisonUnit")
            f["content_per_unit"] = round(cp["value"] / p["comparisonPrice"]["value"], 4)
    if a.get("salesBlock") or (lines and lines[0].get("salesBlock")):
        f["reason"] = "sales_block"
        return f
    if not lines:
        f["reason"] = "not_deliverable"
        return f
    amt = lines[0].get("availableAmount") or 0
    f["earliest_date"] = lines[0].get("availableDate")
    f["stock_known"] = a.get("logisticModel") == "Stock" and amt < UNLIMITED
    f["available"] = max(0.0, amt - reserved) if f["stock_known"] else float(UNLIMITED)
    cutoff = parse_ts(a.get("closingTime"))
    if "price" not in f:
        f["reason"] = "no_price"
    elif cutoff and cutoff <= now:
        f["reason"] = "cutoff_passed"
    elif f["earliest_date"] and f["earliest_date"] > day:
        f["reason"] = "later_date_only"
    elif f["available"] <= 0:
        f["reason"] = "out_of_stock"
    else:
        f["orderable"], f["reason"] = True, "ok"
    return f
