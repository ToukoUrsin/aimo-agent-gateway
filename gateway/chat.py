"""Cards a chat shows next to the agent's words (MCP Apps, SEP-1865, `io.modelcontextprotocol/ui`).

A chat host that supports MCP Apps (Claude on the web, desktop and phone, ChatGPT, VS Code, Goose and others) reads one
HTML file from this server, `ui://valio-aimo/card.html`, and renders it in a sandboxed frame under each tool call. The
file draws the answer the tool gave: the ranked options, the signed offer with every rule's outcome, the hand-off to a
second person, the placed order with its signatures checked inside the frame, the Peppol document, the proposals of
Valio Aimo's own agent. A host without MCP Apps shows the same answer as text, as before: nothing here changes what a
tool returns to the model.

What the card needs and the model does not (the QR of the approval link, the audit document, the Peppol XML, the
mandate's limits) travels in the result's `_meta`, which a host hands to the frame and not to the model. The card can
also ask for it again through `card_data`, a tool only the card may call.
"""
from __future__ import annotations

import json
import time
from contextvars import ContextVar

import segno
from mcp.server.apps import APP_MIME_TYPE, Apps
from mcp.server.mcpserver.resources import FunctionResource
from mcp_types import CallToolResult, TextContent, ToolAnnotations

from . import peppol, peppol_check, policy
from .config import PUBLIC_URL, WEB, order_url
from .service import GatewayError, iso

UI = "ui://valio-aimo/card.html"
META = "fi.valioaimo/card"
# `ui.resourceUri` is the spec's key; the flat key is the one older hosts read.
TOOL_META = {"ui": {"resourceUri": UI}, "ui/resourceUri": UI}
APP_ONLY = {"ui": {"resourceUri": UI, "visibility": ["app"]}}

# Who is calling, for the card's extras. Set by the MCP layer per request (one task per request).
CALLER: ContextVar[dict | None] = ContextVar("card_caller", default=None)

# (title a chat shows, read-only, destructive). A host asks its user before it runs a tool that is not read-only.
TOOLS = {
    "search_catalog": ("Search the catalog", True, False), "lookup_catalog": ("Look up products", True, False),
    "plan_sourcing": ("Plan how to cover a need", True, False), "usual_orders": ("What this kitchen usually orders", True, False),
    "plan_menu_order": ("Plan the order for a menu", True, False),
    "create_checkout": ("Get a signed offer", False, False), "update_checkout": ("Change the offer", False, False), "get_checkout": ("Read the offer", True, False),
    "complete_checkout": ("Place the order", False, False), "cancel_checkout": ("Cancel the offer", False, True), "get_order": ("Read the order", True, False),
    "list_order_issues": ("Problems on placed orders", True, False), "resolve_order_issue": ("Settle a problem on an order", False, False),
    "list_proposals": ("Proposals from Valio Aimo's agent", True, False), "answer_proposal": ("Answer a proposal", False, False),
    "card_data": ("Refresh the card", True, False),
}

_peppol: dict = {}  # order id -> validation result; an order never changes after it is signed


def html() -> str:
    return (WEB / "chat" / "card.html").read_text()


def apps() -> Apps:
    a = Apps()
    # No network for the frame: everything it shows arrives in the tool result. The one allowance is the host's own font files.
    a.add_resource(FunctionResource(uri=UI, name="valio-aimo-cards", title="Valio Aimo order cards", mime_type=APP_MIME_TYPE, fn=html,
                                    description="Draws what a tool answered: options, the signed offer and its rules, the approval hand-off, the order with its signature checks, the Peppol document.",
                                    meta={"ui": {"prefersBorder": False, "permissions": {"clipboardWrite": {}}, "csp": {"resourceDomains": ["https://assets.claude.ai"]}}}))
    return a


def describe(srv) -> None:
    """Give every tool the title and the read-only or write hint a chat shows before it runs it."""
    for name, (title, ro, destructive) in TOOLS.items():
        t = srv._tool_manager.get_tool(name)
        if t:
            t.title = title
            t.annotations = ToolAnnotations(title=title, read_only_hint=ro, destructive_hint=destructive, idempotent_hint=ro, open_world_hint=False)


def qr(url: str) -> str:
    return segno.make(url, error="m").svg_inline(scale=1, border=2, dark="#000", light="#fff", omitsize=True)


def peppol_doc(o: dict) -> dict:
    """The order as a Peppol BIS Ordering 3 Order, validated here and now against the published schema and rules."""
    xml = peppol.order_xml(o)
    if o["id"] not in _peppol:
        r = peppol_check.check(xml)
        if r.get("ok") is None:
            return {"xml": xml, "check": r, "url": order_url(o["id"], "/peppol/order.xml")}
        _peppol[o["id"]] = {**r, "at": iso()}
    return {"xml": xml, "check": _peppol[o["id"]], "url": order_url(o["id"], "/peppol/order.xml"), "response_url": order_url(o["id"], "/peppol/order-response.xml"),
            "stand_in": "Not sent: this prototype has no Peppol access point, and the GLNs in the document are test numbers."}


def order_extras(g, oid: str) -> dict:
    o, a = g.get_order(oid), g.audit(oid)
    return {"order": o, "audit": {"keys": a["keys"], "offer": a["offer"], "ledger": a["ledger"]}, "peppol": peppol_doc(o), "order_url": order_url(oid)}


def mandate_view(g, p: dict) -> dict:
    """The limits the buyer signed for this assistant, and how much of the week's room is used. This is what authorises the spend."""
    b = (p.get("mandate") or {}).get("budget") or {}
    return {"id": p["jti"], "buyer": p.get("buyer_name"), "agent": p.get("agent"), "agent_name": p.get("agent_name"), "expires": iso(p["exp"]) if p.get("exp") else None,
            "max_per_order": b.get("max_per_order"), "max_per_week": b.get("max_per_week"), "spent_week": g.store.spent_since(p["customer"], time.time() - policy.WEEK),
            "keyless": not p.get("bound"), "key_held_by": (p.get("mandate_key") or {}).get("held_by")}


def product_facts(g, out: dict, p: dict) -> dict:
    """For a sourcing plan: the catalogue facts of each product in its options, so the card can show origin, allergens and the order cut-off."""
    ids = list(dict.fromkeys(l["item"]["id"] for o in out.get("options") or [] for l in o.get("line_items") or []))[:12]
    if not ids:
        return {}
    live, facts = g._live(ids, out.get("delivery_date"), p), {}  # the quotes the plan just read: served from the cache
    for i in ids:
        d = g.cat.get(i)
        if not d:
            continue
        w = g._product(d, live[i], p, None, out.get("delivery_date")).get("wholesale") or {}
        facts[i] = {k: w.get(k) for k in ("gtin", "brand", "supplier", "origin", "storage", "claims", "allergens", "labels", "availability", "data_source") if w.get(k) is not None}
        facts[i]["order_by"] = (w.get("delivery") or {}).get("order_by")
    return facts


def extras(g, tool: str, out: dict, p: dict | None = None) -> dict:
    """What the card needs on top of the tool's answer."""
    x = {"tool": tool, "site": PUBLIC_URL, "keys": {"seller": g.key.jwk}}
    try:
        if p and p.get("ok"):
            x["mandate"] = mandate_view(g, p)
        if isinstance(out.get("ap2"), dict) and out.get("id"):  # a checkout
            if out.get("status") == "requires_escalation" and out.get("continue_url"):
                x["qr"] = qr(out["continue_url"])
            if out.get("status") == "completed" and (out.get("order") or {}).get("id"):
                x.update(order_extras(g, out["order"]["id"]))
        elif tool == "get_order" and out.get("id") and out.get("proof"):
            x.update(order_extras(g, out["id"]))
        elif isinstance(out.get("options"), list) and out.get("need") and p and p.get("ok"):
            x["products"] = product_facts(g, out, p)
    except Exception as e:  # the card still draws the answer without its extras
        x["extras_error"] = f"{type(e).__name__}: {e}"
    return x


def result(g, tool: str, out: dict) -> CallToolResult:
    """The same JSON the model always got, as text and as structured content, plus the card's extras where the model does not read."""
    x = {"tool": tool, "site": PUBLIC_URL} if tool == "card_data" else extras(g, tool, out, CALLER.get())  # card_data carries its extras in the answer itself
    return CallToolResult(content=[TextContent(type="text", text=json.dumps(out, ensure_ascii=False))], structured_content=out, _meta={META: x})


def card_data(g, kind: str, id: str, p) -> dict:
    """For the card only: the current state of a checkout, or an order's extras, of the caller's own account."""
    if not p or not p.get("ok"):
        raise GatewayError(401, (p or {}).get("error", "mandate_required"), (p or {}).get("detail", "This needs the account's mandate."))
    if kind == "checkout":
        c = g.get_checkout(id, p)
        return {"checkout": c, "card": extras(g, "get_checkout", c, p)}
    if kind == "order":
        o = g.get_order(id)
        if o["wholesale"].get("customer") != p.get("customer"):
            raise GatewayError(403, "forbidden", "This order belongs to another account.")
        return {"order": o, "card": extras(g, "get_order", o, p)}
    raise GatewayError(400, "bad_request", "kind is checkout or order.")
