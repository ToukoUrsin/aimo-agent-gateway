"""HTTP surface of the gateway.

* UCP REST binding (catalog, checkout sessions, orders) + the wholesale extension (/sourcing/plan)
* UCP MCP binding at /mcp (same operations, same documents)
* the approval link a person opens on a phone
* a demo console (clearly separate: everything under /api/demo is demo scaffolding)
"""
import asyncio
import io
import json
import secrets
import time
from contextlib import asynccontextmanager

import segno
from fastapi import Body, FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from mcp.server.transport_security import TransportSecuritySettings

from . import home, mcp_tools, oauth, peppol, policy, tower
from . import proof
from . import after
from . import anchor
from . import seller
from . import menu
from . import passkey
from .config import OPERATOR_KEY, PORT, PUBLIC_URL, STAGE, WEB, order_key, order_url
from .service import Gateway, GatewayError

g = Gateway()
mcp_server = mcp_tools.build(g)
mcp_app = mcp_server.streamable_http_app(streamable_http_path="/mcp", json_response=True, stateless_http=True,
                                         transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False))


@asynccontextmanager
async def lifespan(_):
    async with mcp_server.session_manager.run():
        yield


app = FastAPI(title="Valio Aimo agent gateway (Junction 2026 mock)", version="0.2.0", lifespan=lifespan,
              description="UCP-shaped REST binding for external purchasing agents. Present the buyer's mandate as `Authorization: Bearer <mandate JWT>`. Synthetic challenge data.")
# Agents and MCP clients running in a browser call from other origins. No cookies are accepted cross-origin.
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"], expose_headers=["WWW-Authenticate", "Mcp-Session-Id"])


@app.exception_handler(GatewayError)
async def gateway_error(_, e: GatewayError):
    body = {"ucp": {"status": "error"}, "messages": [{"type": "error", "code": e.code, "severity": "unrecoverable", "content": e.message}]}
    if e.retry_after is not None:
        body["retry_after"] = e.retry_after
    return JSONResponse(body, status_code=e.status, headers={"Retry-After": str(e.retry_after)} if e.retry_after is not None else None)


@app.middleware("http")
async def request_limiter(request: Request, call_next):
    if request.url.path.startswith(("/catalog/", "/sourcing/", "/checkout-sessions", "/mcp", "/orders/", "/ledger")):
        auth = request.headers.get("authorization", "")
        p = g.identify(auth[7:].strip()) if auth.lower().startswith("bearer ") else None
        forwarded = request.headers.get("x-forwarded-for", "").split(",", 1)[0].strip()
        ip = forwarded or (request.client.host if request.client else "unknown")
        try:
            g.rate_limit(p, None if p and p.get("ok") else ip, request.method + " " + request.url.path)
        except GatewayError as e:
            body = {"ucp": {"status": "error"}, "messages": [{"type": "error", "code": e.code, "severity": "unrecoverable", "content": e.message}], "retry_after": e.retry_after}
            return JSONResponse(body, status_code=e.status, headers={"Retry-After": str(e.retry_after)})
    return await call_next(request)


def who(request: Request):
    auth = request.headers.get("authorization", "")
    return g.identify(auth[7:].strip() if auth.lower().startswith("bearer ") else None), request.headers.get("x-trace-id")


def member(request: Request):
    """Reading or changing a checkout needs a valid mandate from the account that owns it."""
    p, tr = who(request)
    if not p or not p.get("ok"):
        raise GatewayError(401, (p or {}).get("error", "mandate_required"), (p or {}).get("detail", "Present the buyer's mandate as Authorization: Bearer <mandate>."))
    return p, tr


def page(name):
    return FileResponse(WEB / name, headers={"Cache-Control": "no-store"})


# ----------------------------------------------------------------- discovery
@app.get("/.well-known/ucp", tags=["discovery"], summary="Business profile: endpoints, capabilities and signing keys")
def profile():
    return g.profile_doc()


# ------------------------------------------------------------------- catalog
@app.post("/catalog/search", tags=["catalog"], summary="Search products with live price, stock and product facts")
def catalog_search(request: Request, body: dict = Body(...)):
    p, tr = who(request)
    return g.search(body, p, tr)


@app.post("/catalog/lookup", tags=["catalog"], summary="Look up products by id or GTIN")
def catalog_lookup(request: Request, body: dict = Body(...)):
    p, tr = who(request)
    ids = body.get("ids") or body.get("product_ids") or body.get("gtins") or []
    if isinstance(ids, (str, int)):
        ids = [ids]
    if not ids:
        raise GatewayError(400, "missing_ids", 'Send the product ids or GTINs to look up as a list in "ids", for example {"ids": ["460791"]}.')
    return g.lookup([str(i) for i in ids], (body.get("wholesale") or {}).get("delivery_date"), p, tr)


@app.post("/sourcing/plan", tags=["wholesale extension"], summary="Turn a need (quantity, date, requirements) into ranked, ready-to-order options")
def sourcing_plan(request: Request, body: dict = Body(...)):
    p, tr = who(request)
    return g.plan(body, p, tr)


# ------------------------------------------------------------------ checkout
@app.post("/checkout-sessions", tags=["checkout"], summary="Create a checkout: validated lines, guardrail decision, signed offer with held price and stock", status_code=201)
def checkout_create(request: Request, body: dict = Body(...)):
    p, tr = who(request)
    return g.create_checkout(body, p, tr)


@app.get("/checkout-sessions/{cid}", tags=["checkout"], summary="Get a checkout")
def checkout_get(cid: str, request: Request):
    return g.get_checkout(cid, member(request)[0])


@app.put("/checkout-sessions/{cid}", tags=["checkout"], summary="Replace a checkout's lines and get a fresh signed offer")
def checkout_update(cid: str, request: Request, body: dict = Body(...)):
    p, tr = member(request)
    return g.update_checkout(cid, body, p, tr)


@app.post("/checkout-sessions/{cid}/complete", tags=["checkout"], summary="Place the order (needs status ready_for_complete and the agent's signature over the offer)")
def checkout_complete(cid: str, request: Request, body: dict = Body(default={})):
    return g.complete_checkout(cid, body, who(request)[0])


@app.post("/checkout-sessions/{cid}/cancel", tags=["checkout"], summary="Cancel a checkout and release held stock")
def checkout_cancel(cid: str, request: Request):
    return g.cancel_checkout(cid, member(request)[0])


# -------------------------------------------------------------------- orders
@app.get("/orders/{oid}", tags=["orders"], summary="Signed order confirmation with evidence (HTML in a browser)")
def order_get(oid: str, request: Request):
    if "text/html" in request.headers.get("accept", ""):
        return page("order.html")
    return order_for_reader(oid, request)


def order_for_reader(oid: str, request: Request):
    o = g.get_order(oid)
    k = request.query_params.get("k") or ""
    if k and secrets.compare_digest(k, order_key(oid)):  # the link from the confirmation: whoever holds it may read this one order
        return o
    auth = request.headers.get("authorization", "")
    p = g.identify(auth[7:].strip()) if auth.lower().startswith("bearer ") else None
    if p and p.get("ok") and p.get("customer") == o["wholesale"].get("customer"):
        return o
    c = ctx(request)
    if c["op"] or c["acct"]["customer"] == o["wholesale"].get("customer"):
        return o
    raise GatewayError(401, "mandate_required", "Reading an order confirmation needs the account's mandate, or the full link from the confirmation (it ends in ?k=...).")


@app.get("/orders/{oid}/audit", tags=["orders"], summary="Hash-chained audit trail for an order")
def order_audit(oid: str, request: Request):
    order_for_reader(oid, request)
    return g.audit(oid)


@app.get("/orders/{oid}/peppol/order.xml", tags=["orders"], summary="The order as a Peppol BIS Ordering 3 Order (UBL 2.1)")
def order_peppol(oid: str, request: Request):
    return Response(peppol.order_xml(order_for_reader(oid, request)), media_type="application/xml")


@app.get("/orders/{oid}/peppol/order-response.xml", tags=["orders"], summary="The confirmation as a Peppol BIS Ordering 3 OrderResponse (UBL 2.1)")
def order_peppol_response(oid: str, request: Request):
    return Response(peppol.order_response_xml(order_for_reader(oid, request)), media_type="application/xml")


@app.get("/ledger", tags=["orders"], summary="A paged, hash-chained audit ledger with account-scoped redaction")
def ledger(request: Request, format: str = "", limit: int = 200, since: int = 0, view: str = ""):
    if format != "json" and "text/html" in request.headers.get("accept", "") and (WEB / "ledger.html").exists():
        return page("ledger.html")  # people get the readable page; agents and scripts get the JSON (or ?format=json)
    auth = request.headers.get("authorization", "")
    p = g.identify(auth[7:].strip()) if auth.lower().startswith("bearer ") else None
    op = bool(OPERATOR_KEY) and secrets.compare_digest(request.headers.get("x-operator-key", ""), OPERATOR_KEY)
    acct = p.get("customer") if p and p.get("ok") else None
    if not p:
        s = ctx(request)
        op, acct = s["op"], s["acct"]["customer"]
    # Demo only: the invented accounts on the stage and the floor are open to every visitor (view=valio), so anyone can
    # read the ledger the way Valio Aimo would. A visitor's own sandbox is never shown to anyone else.
    demo = {a["customer"] for a in g.accounts() if a["kind"] != "sandbox"} if view == "valio" and not op else set()
    events = g.store.ledger(acct, op, limit=limit, since=since, open_accts=demo)
    reader = {"account": acct, "sees": "everything" if op else "own account and the demo's invented accounts" if demo else "own account"}
    return {"genesis": "0" * 64, "hash": "sha256(prev_hash_hex + digest_hex)", "reader": reader, "events": events, "next_since": events[-1]["seq"] if events else since}


# ------------------------------------------------------------ human approval
@app.get("/approve/{cid}", include_in_schema=False)
def approve_page(cid: str):
    return page("approve.html")


@app.get("/api/approve/{cid}", include_in_schema=False)
def approve_view(cid: str, k: str = ""):
    return g.approval_view(cid, k)


@app.post("/api/approve/{cid}", include_in_schema=False)
def approve_decide(cid: str, request: Request, body: dict = Body(...)):
    approve, by, note = bool(body.get("approve")), str(body.get("approver") or "F&B manager")[:60], str(body.get("note") or "")[:300]
    device = passkey.check(g, cid, body.get("k"), approve, by, note, body.get("device"), request)  # None: this account approves by link
    return g.decide(cid, body.get("k"), approve, by, note, **({"device": device} if device else {}))


# ------------------------------------------------------ who is looking (demo)
# Everything under /api/demo is demo scaffolding. A visitor gets a sandbox account of their own (own stock, own
# agents, own limits) and plays both sides of it. The stage account and Valio Aimo's view of every account need
# the operator key.
COOKIE = "aimo_s"
RUNS: dict[str, dict] = {}
TASKS: set = set()
NO_MODEL = {"script", "rogue"}  # agents that call no model
LIMITS = {"llm_running": 10, "llm_per_visitor_day": 40, "llm_per_day": 800, "runs_per_visitor_day": 150}
_used: dict[tuple, int] = {}


def ctx(request: Request, response: Response | None = None) -> dict:
    """{acct, op, sid}: the account this browser works on, and whether it holds the operator key."""
    op = bool(OPERATOR_KEY) and secrets.compare_digest(request.headers.get("x-operator-key", ""), OPERATOR_KEY)
    tok = request.cookies.get(COOKIE)
    s = g.store.get("session:" + tok) if tok else None
    dirty = False
    if s is None and not op:
        tok, s, dirty = secrets.token_urlsafe(24), {"created": time.time()}, True
    s = s or {}
    op = op or bool(s.get("operator"))
    if op:
        want = request.query_params.get("acct") or request.headers.get("x-account") or s.get("view") or STAGE
        slug = want if g.account(want) else STAGE
    else:
        slug = s.get("slug")
        if not slug or not g.account(slug):
            slug = "sandbox-" + secrets.token_hex(4)
            g.create_account(slug, f"Sandbox Kitchen {secrets.randbelow(9000) + 1000}", kind="sandbox")
            s["slug"], dirty = slug, True
    if dirty:
        g.store.put("session:" + tok, s)
        if response is not None:
            response.set_cookie(COOKIE, tok, max_age=30 * 86400, httponly=True, samesite="lax", secure=PUBLIC_URL.startswith("https"))
    return {"acct": g.account(slug), "op": op, "sid": tok or "operator"}


def operator(request: Request) -> dict:
    c = ctx(request)
    if not c["op"]:
        raise GatewayError(403, "forbidden", "This needs the operator key.")
    return c


@app.get("/op", include_in_schema=False)
def operator_sign_in(request: Request, key: str = ""):
    """Open /op?key=<OPERATOR_KEY> once: this browser then runs the stage account and sees Valio Aimo's side."""
    if not OPERATOR_KEY or not secrets.compare_digest(key, OPERATOR_KEY):
        raise GatewayError(403, "forbidden", "Wrong operator key.")
    tok = request.cookies.get(COOKIE) or secrets.token_urlsafe(24)
    s = g.store.get("session:" + tok) or {"created": time.time()}
    s["operator"] = True
    g.store.put("session:" + tok, s)
    r = RedirectResponse("/demo", status_code=303)
    r.set_cookie(COOKIE, tok, max_age=30 * 86400, httponly=True, samesite="lax", secure=PUBLIC_URL.startswith("https"))
    return r


# -------------------------------------------------------------- demo console
@app.get("/", include_in_schema=False)
def home_page():
    return page("home.html" if (WEB / "home.html").exists() else "index.html")


@app.get("/demo", include_in_schema=False)
def console_page():
    return page("index.html")


@app.get("/spec", include_in_schema=False)
def spec_page():
    return page("spec.html")


@app.get("/favicon.ico", include_in_schema=False)
def favicon():
    return FileResponse(WEB / "favicon.svg", media_type="image/svg+xml", headers={"Cache-Control": "public, max-age=86400"})


@app.get("/api/pages", include_in_schema=False)
def live_pages():
    """Which optional pages exist right now, so the shared header in web/lib.js links only to pages that work."""
    def walk(routes):
        for r in routes:
            sub = getattr(getattr(r, "original_router", None), "routes", None)
            yield from walk(sub) if sub is not None else [getattr(r, "path", None)]
    paths = set(walk(app.routes))
    want = {"/menu": "menu.html", "/seller": "seller.html", "/tower": "tower.html", "/ledger": "ledger.html"}
    return {"live": [p for p, f in want.items() if p in paths and (WEB / f).exists()]}


def _summary(c):
    w = c["wholesale"]
    return {"id": c["id"], "status": c["status"], "subtotal": next((t["amount"] for t in c["totals"] if t["type"] == "subtotal"), 0), "agent": w["agent"], "delivery_date": w["delivery_date"],
            "lines": [{"title": l["item"]["title"], "units": l["wholesale"]["units"], "unit": l["item"]["quantity_unit"]["display_text"]} for l in c["line_items"]],
            "decision": w["policy"]["decision"], "continue_url": c.get("continue_url"), "order": c.get("order"), "expires_at": c["expires_at"]}


def order_row(o):
    w = o["wholesale"]
    return {"id": o["id"], "label": o["label"], "url": order_url(o["id"]), "subtotal": next(t["amount"] for t in o["totals"] if t["type"] == "subtotal"), "customer": w["customer"], "buyer": (w.get("buyer") or {}).get("name"),
            "approved_by": (w["evidence"].get("approval") or {}).get("by"), "placed_by": w["placed_by"], "placed_at": w["placed_at"], "status": w.get("status", "confirmed")}


_chain = {"t": 0.0, "v": (True, 0, None), "state": {"seq": 0, "prev": "0" * 64, "bad": None, "count": 0}}


def chain_status():
    """Verify only newly appended links; the store keeps the last checked hash."""
    if time.time() - _chain["t"] > 4:
        state = _chain["state"]
        max_seq = g.store.q("select coalesce(max(seq),0) from events")[0][0]
        if max_seq < state["seq"]:
            state = {"seq": 0, "prev": "0" * 64, "bad": None, "count": 0}
        _chain["state"] = state
        _chain.update(t=time.time(), v=g.store.verify_chain_incremental(state))
    return _chain["v"]


def _models():
    try:
        from agent.llm import MODELS  # the buyer's side; the gateway runs without it
        return MODELS
    except Exception:
        return []


@app.get("/api/console", include_in_schema=False)
def console(request: Request, response: Response, since: int = 0):
    c = ctx(request, response)
    a = c["acct"]
    cust = a["customer"]
    ok, n, bad = chain_status()
    hist = g.store.get("history:" + cust, {})
    floor = max(since, a.get("since_seq", 0))
    runs = sorted((r for r in RUNS.values() if r["acct"] == a["slug"]), key=lambda r: -r["created"])[:8]
    events = {e["seq"]: e for e in g.store.events(limit=300, acct=cust, since=floor)}
    for r in runs[:3]:  # attempts the gateway could not tie to an account (no or forged mandate) still belong to the run that made them
        events.update({e["seq"]: e for e in g.store.events(ref=r["trace"], limit=200, since=floor)})
    return {
        "public_url": PUBLIC_URL, "operator": c["op"], "account": {k: a[k] for k in ("slug", "name", "kind", "customer")},
        "buyer": a["name"], "agent": f"agent:{a['slug']}/purchasing-1", "customer": cust,
        "profile": g.store.get("profile:" + cust), "mandate": g.store.get("mandate_config:" + cust),
        "history": [{"id": k, "title": (g.cat.get(k) or {}).get("title", k), **v} for k, v in hist.items()],
        "checkouts": [_summary(x) for x in g.store.checkouts(12, cust)],
        "orders": [order_row(o) for o in g.store.orders(12, cust)],
        "agents": [{k: ag.get(k) for k in ("id", "name", "kind", "status", "reason", "orders", "asked", "refused", "last_seen")} for ag in g.agents(a["slug"])],
        "events": [events[k] for k in sorted(events)], "ledger": {"intact": ok, "events": n, "first_bad_seq": bad},
        "spent_week": g.store.spent_since(cust, time.time() - policy.WEEK), "aimo_calls": g.aimo.calls,
        "models": _models(),
        "runs": [{"id": r["id"], "agent": r["agent"], "model": r.get("model"), "request": r["request"], "status": r["status"]} for r in runs],
    }


@app.get("/api/demo/checkout/{cid}", include_in_schema=False)
def console_checkout(cid: str, request: Request):
    c, x = g.store.checkout(cid), ctx(request)
    if not c or (not x["op"] and c["_internal"].get("customer") not in (None, x["acct"]["customer"])):
        raise GatewayError(404, "not_found", "No such checkout.")
    return g.public(c)


@app.get("/api/qr", include_in_schema=False)
def qr(u: str):
    if not u.startswith(PUBLIC_URL):
        raise GatewayError(400, "bad_request", "Only this gateway's own links.")
    buf = io.BytesIO()
    segno.make(u, error="m").save(buf, kind="svg", scale=5, border=2, dark="#111", light="#fff")
    return Response(buf.getvalue(), media_type="image/svg+xml")


@app.post("/api/demo/profile", include_in_schema=False)
def set_profile(request: Request, body: dict = Body(...)):
    """Valio Aimo's side: switch agent ordering on/off, set the autonomy tier and caps for one account."""
    c = ctx(request)
    a = c["acct"]
    if c["op"] and "global_pause" in body:
        g.store.put("global_pause", bool(body["global_pause"]))
    prof = g.store.get("profile:" + a["customer"])
    before = dict(prof)
    for k in ("enabled", "tier", "max_order_total", "show_contract_prices", "show_exact_stock", "exact_stock", "exact_counts", "keyless_cap"):
        if k in body:
            prof[k] = body[k]
    g.store.put("profile:" + a["customer"], prof)
    changed = {k: prof[k] for k in prof if prof[k] != before.get(k)}
    if changed:
        g.store.log(None, "human:Valio Aimo account manager", "control", f"Valio Aimo changed its agent rules for {a['name']}: " + ", ".join(f"{k} = {v}" for k, v in changed.items()) + ".", {"profile": prof}, acct=a["customer"])
    return prof


@app.post("/api/demo/agent", include_in_schema=False)
def set_agent(request: Request, body: dict = Body(...)):
    """Valio Aimo's side: suspend or reinstate one agent."""
    c = ctx(request)
    ag = g.store.get("agent:" + str(body.get("id"))) or {}
    if not c["op"] and ag.get("slug") != c["acct"]["slug"]:
        raise GatewayError(404, "not_found", "No such agent.")
    return g.set_agent(body.get("id"), bool(body.get("suspended")))


@app.post("/api/demo/mandate", include_in_schema=False)
def set_mandate(request: Request, body: dict = Body(...)):
    """The buyer's side: edit the mandate the agent works under. The next run gets a freshly signed mandate."""
    a = ctx(request)["acct"]
    m = g.store.get("mandate_config:" + a["customer"])
    m.update({k: v for k, v in body.items() if k in policy.default_mandate()})
    g.store.put("mandate_config:" + a["customer"], m)
    g.store.log(None, "human:buyer admin", "control", f"{a['name']} changed its agent's mandate: per-order limit {policy.eur(m['budget']['max_per_order'])}, substitutions '{m['substitution']['allow']}', new products '{m['new_products']}'.", {"mandate": m}, acct=a["customer"])
    return m


@app.post("/api/demo/reset", include_in_schema=False)
def reset(request: Request):
    a = ctx(request)["acct"]
    g.reset(a["slug"])
    for rid in [k for k, r in RUNS.items() if r["acct"] == a["slug"]]:
        RUNS.pop(rid)
    return {"ok": True}


@app.post("/api/demo/account", include_in_schema=False)
def make_account(request: Request, body: dict = Body(...)):
    """Operator: create (or return) an account. Used by the floor simulation and the evals; kind is sandbox or floor."""
    operator(request)
    slug = str(body.get("slug") or "sandbox-" + secrets.token_hex(4))
    a = g.account(slug) or g.create_account(slug, str(body.get("name") or f"Sandbox Kitchen {secrets.randbelow(9000) + 1000}"), kind="floor" if body.get("kind") == "floor" else "sandbox",
                                            ship_to=body.get("ship_to"), history=body.get("history"), mandate=body.get("mandate"), world=body.get("world"))
    if body.get("profile"):
        g.store.put("profile:" + a["customer"], {**policy.default_profile(), **body["profile"]})
    return a


@app.post("/api/demo/wipe", include_in_schema=False)
def wipe(request: Request):
    operator(request)
    g.wipe()
    RUNS.clear()
    return {"ok": True}


@app.get("/api/demo/mandate-token", include_in_schema=False)
def mandate_token(request: Request, response: Response):
    """Demo stand-in for the buyer's own system: issues a signed mandate so you can try the API with curl or any MCP client."""
    a = ctx(request, response)["acct"]
    return {"mandate": g.issue_mandate(a["slug"], agent=f"agent:{a['slug']}/own-client", name="Your own client", bound=False, ttl=86400), "account": a["name"], "use": "Authorization: Bearer <mandate>", "mcp": f"{PUBLIC_URL}/mcp", "rest": PUBLIC_URL,
            "note": "A key-less mandate: no signature needed to complete, and Valio Aimo gives it less room (rule T1). An agent with its own key gets a mandate bound to that key."}


def _allow_run(c, kind):
    if c["op"]:
        return
    slug, day, llm = c["acct"]["slug"], int(time.time() // 86400), kind not in NO_MODEL
    live = [r for r in RUNS.values() if r["status"] in ("running", "waiting_approval") and not r.get("_op")]  # Valio Aimo's own test runs do not take a visitor's place
    if any(r["acct"] == slug for r in live):
        raise GatewayError(429, "busy", "One run at a time. Wait for the current one to finish.", retry_after=5)
    busy = lambda: GatewayError(429, "busy", "Many people are trying this right now. It retries by itself in a few seconds.", retry_after=8)
    if llm and (len([r for r in live if r["status"] == "running" and r["agent"] not in NO_MODEL]) >= LIMITS["llm_running"] or _used.get(("llm", day), 0) >= LIMITS["llm_per_day"]):
        raise busy()
    if _used.get((c["sid"], day, llm), 0) >= (LIMITS["llm_per_visitor_day"] if llm else LIMITS["runs_per_visitor_day"]):
        raise GatewayError(429, "busy", "That is a lot of runs for one day. Come back tomorrow.")
    _used[(c["sid"], day, llm)] = _used.get((c["sid"], day, llm), 0) + 1
    if llm:
        _used[("llm", day)] = _used.get(("llm", day), 0) + 1


AGENT_NAMES = {"script": ("standing-order-bot", "Standing-order bot"), "rogue": ("unvetted-bot", "Unvetted bot")}


def new_run(slug: str, kind: str, request_text: str, model: str | None = None, op: bool = False) -> dict:
    """Start one buyer-side agent for an account. The run carries the mandate its company signed for it."""
    from agent import runner  # the buyer's side; imported lazily so the gateway runs without it

    a = g.account(slug)
    aid, name = AGENT_NAMES.get(kind, ("purchasing-1", "Purchasing agent"))
    if kind == "rogue":
        aid += "-" + secrets.token_hex(2)  # every run is a new unknown bot, so one suspension does not hide the next run
    run = {"id": "run_" + secrets.token_hex(5), "created": time.time(), "agent": kind, "model": model, "acct": slug, "buyer_name": a["name"], "request": request_text[:600], "status": "running", "steps": [],
           "trace": "trc_" + secrets.token_hex(6), "_mandate": g.issue_mandate(slug, agent=f"agent:{slug}/{aid}", name=name), "_key": ("demo_buyer" if slug == STAGE else f"b_{slug}") + "_agent", "_op": op}
    RUNS[run["id"]] = run
    for rid in sorted(RUNS, key=lambda k: RUNS[k]["created"])[:-400]:
        RUNS.pop(rid)
    task = asyncio.create_task(runner.start(run, f"http://127.0.0.1:{PORT}"))
    TASKS.add(task)
    task.add_done_callback(TASKS.discard)
    return run


@app.post("/api/demo/run", include_in_schema=False)
async def run_start(request: Request, response: Response, body: dict = Body(...)):
    c = ctx(request, response)
    kind = str(body.get("agent") or "claude")
    _allow_run(c, kind)
    model = str(body.get("model") or "")
    run = new_run(c["acct"]["slug"], kind, str(body.get("request") or "")[:2000], model if model in {m["id"] for m in _models()} else None, op=c["op"])
    return {"id": run["id"], "trace": run["trace"]}


@app.get("/api/demo/run/{rid}", include_in_schema=False)
def run_get(rid: str, request: Request):
    r, c = RUNS.get(rid), ctx(request)
    if not r or (not c["op"] and r["acct"] != c["acct"]["slug"]):
        raise GatewayError(404, "not_found", "No such run.")
    return {k: v for k, v in r.items() if not k.startswith("_")}


# ----------------------------------------------------------- feature routers
app.include_router(oauth.build(g, ctx, page))
app.include_router(home.build(g, chain_status))
app.include_router(tower.build(g, ctx, page))
app.include_router(proof.build(g, ctx, page))
app.include_router(after.build(g, ctx, page))  # after the order: issues, amendments, recalls
app.include_router(anchor.build(g, ctx))  # ledger head anchored on Solana devnet
app.include_router(seller.build(g, ctx, page))  # Valio Aimo's own agent: proposals, summary, /seller
app.include_router(passkey.build(g, ctx, page, COOKIE))  # an approver's own device signs the decision
app.include_router(menu.build(g, ctx, page))  # a menu photo becomes an order: /menu, /byhand, POST /menu/plan


class McpGuard:
    """MCP needs a mandate. Without one the client gets a 401 that points at the sign-in (RFC 9728), which is how
    Claude, ChatGPT and other MCP clients know to open the consent page."""

    def __init__(self, inner):
        self.inner = inner

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http" and scope["path"].rstrip("/") == "/mcp" and scope["method"] != "OPTIONS":
            auth = dict(scope["headers"]).get(b"authorization", b"").decode()
            p = g.identify(auth[7:].strip()) if auth.lower().startswith("bearer ") else None
            if not p or not p.get("ok"):
                www = f'Bearer resource_metadata="{PUBLIC_URL}/.well-known/oauth-protected-resource/mcp"' + (f', error="invalid_token", error_description="{p["detail"]}"' if p else "")
                body = json.dumps({"error": "invalid_token" if p else "unauthorized", "error_description": (p or {}).get("detail", "Sign in or present a mandate as Authorization: Bearer <mandate>.")}).encode()
                await send({"type": "http.response.start", "status": 401, "headers": [(b"content-type", b"application/json"), (b"www-authenticate", www.encode()), (b"access-control-allow-origin", b"*"), (b"access-control-expose-headers", b"WWW-Authenticate")]})
                await send({"type": "http.response.body", "body": body})
                return
        await self.inner(scope, receive, send)


app.mount("/static", StaticFiles(directory=WEB), name="static")
app.mount("/", McpGuard(mcp_app))
