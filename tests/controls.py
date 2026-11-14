"""Controls checks for the external-agent boundary.

Run with ``uv run -q python -m tests.controls``.  The fake Aimo client keeps
this suite deterministic and does not spend calls against the challenge API.
"""
import os
import tempfile
import time

from gateway.aimo import Aimo, AimoError
from gateway.service import Gateway, GatewayError
from gateway.store import Store


def fake_quote(ids, day, customer):
    return {i: {"avail": {"logisticModel": "Stock", "scheduleLines": [{"availableAmount": 698, "availableDate": day, "closingTime": "2099-01-01T12:00:00+02:00"}]},
                 "price": {"catalogPrice": {"value": 2.0}, "comparisonPrice": {"value": 2.0}, "comparisonUnit": "L", "tax": {"taxRate": 13.5}, "netPriceFound": True}, "time_now": "2026-11-14T10:00:00+02:00"} for i in ids}


def walk(x):
    if isinstance(x, dict):
        for k, v in x.items():
            yield k, v
            yield from walk(v)
    elif isinstance(x, list):
        for v in x:
            yield from walk(v)


def main():
    g = Gateway(Store(tempfile.mktemp()))
    g.aimo.quote = fake_quote
    token = g.issue_mandate()
    p = g.identify(token)

    public = g.search({"query": "milk", "pagination": {"limit": 3}}, None)
    assert all("price" not in k and k not in ("stock", "orderable", "reason") for k, _ in walk(public)), "public catalogue leaked commercial data"
    private = g.search({"query": "milk", "pagination": {"limit": 3}, "wholesale": {"need": {"quantity": 1000, "unit": "L"}}}, p)
    assert any(k == "covers_quantity" for k, _ in walk(private))
    assert all(not (k in ("stock", "available") and isinstance(v, (int, float)) and not isinstance(v, bool)) for k, v in walk(private)), "agent response exposed inventory counts"

    # Upstream 429 serves an expired cached value and marks it stale; no cache is a clean 503.
    a = Aimo()
    a.cache[("x", "2026-11-16", "c")] = (0, fake_quote(["x"], "2026-11-16", "c")["x"])
    a._post = lambda *args: (_ for _ in ()).throw(AimoError(code="upstream_rate_limited"))
    assert a.quote(["x"], "2026-11-16", "c")["x"]["stale"]
    try:
        a.quote(["y"], "2026-11-16", "c")
    except AimoError as e:
        assert e.status == 503
    else:
        raise AssertionError("uncached upstream failure was not a clean 503")

    old = {k: os.environ.get(k) for k in ("AIMO_AGENT_RATE", "AIMO_ACCOUNT_RATE", "AIMO_AGENT_BURST", "AIMO_ACCOUNT_BURST")}
    os.environ.update(AIMO_AGENT_RATE="2", AIMO_ACCOUNT_RATE="2", AIMO_AGENT_BURST="1", AIMO_ACCOUNT_BURST="1")
    try:
        g.rate_limit(p)
        try:
            g.rate_limit(p)
        except GatewayError as e:
            assert e.status == 429 and e.code == "rate_limited" and e.retry_after
        else:
            raise AssertionError("bucket did not return 429")
        time.sleep(0.6)
        g.rate_limit(p)  # the bucket refills after retry_after
    finally:
        for k, v in old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    # Distinct unactioned lookups are a W4 strike and three strikes suspend.
    for start in (0, 50, 100):
        try:
            g._harvest_check(p, [str(start + i) for i in range(45)])
        except GatewayError as e:
            assert e.code == "catalogue_harvest"
    assert g.store.get("agent:" + p["agent"])["status"] == "suspended"

    # Every reader gets a verifiable chain; only its account gets bodies.
    g.store.log("a", "agent:a", "lookup", "mine", {"x": 1}, acct="mine")
    g.store.log("b", "agent:b", "lookup", "theirs", {"x": 2}, acct="theirs")
    assert g.store.verify_chain()[0]
    view = g.store.ledger("mine")
    assert view[-2]["withheld"] is False and view[-1]["withheld"] is True and view[-1]["digest"]
    assert all(not e["withheld"] for e in g.store.ledger(operator=True))
    print("controls: all checks pass")


if __name__ == "__main__":
    main()
