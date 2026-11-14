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
import secrets
import time
from contextvars import ContextVar

import segno
from mcp.server.apps import APP_MIME_TYPE, Apps
from mcp.server.mcpserver.resources import FunctionResource
from mcp_types import CallToolResult, TextContent, ToolAnnotations

from . import peppol, peppol_check, pictures, policy
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


def product_ids(out: dict) -> list[str]:
    """Every product an answer names, in the order the card shows them."""
    ids, lines = [], lambda x: [(l.get("item") or {}).get("id") for l in (x or {}).get("line_items") or [] if isinstance(l, dict)]
    for o in out.get("options") or []:
        ids += lines(o)
    ids += lines(out) + lines(out.get("checkout")) + lines(out.get("order"))
    for k in ("usual", "other_kinds_ordered", "products", "also_qualifying"):
        ids += [r.get("id") for r in out.get(k) or [] if isinstance(r, dict)]
    for pr in out.get("proposals") or []:
        ids += lines(pr)
    for l in out.get("lines") or []:  # a menu plan
        if isinstance(l, dict) and isinstance(l.get("product"), dict):
            ids.append(l["product"].get("id"))
    for i in out.get("issues") or []:
        for w in i.get("ways_out") or []:
            ids += lines(w)
    return list(dict.fromkeys(str(i) for i in ids if i))


def product_pictures(g, out: dict) -> dict:
    """{product id: picture} for the products of an answer that have a public picture (gateway/pictures.py)."""
    gtin = {i: (g.cat.get(i) or {}).get("gtin") for i in product_ids(out)}
    got = pictures.pictures([v for v in gtin.values() if v])
    return {i: got[v] for i, v in gtin.items() if v in got}


def extras(g, tool: str, out: dict, p: dict | None = None) -> dict:
    """What the card needs on top of the tool's answer."""
    x = {"tool": tool, "site": PUBLIC_URL, "keys": {"seller": g.key.jwk}}
    try:
        pics = product_pictures(g, out)
        if pics:
            x["pictures"], x["pictures_from"] = pics, pictures.SOURCE
    except Exception:
        pass  # a card without pictures is still the whole answer
    try:  # the shop page shows a product by its Finnish name when the catalogue has one
        stood_in = [str(((l.get("wholesale") or {}).get("substitutes_for") or {}).get("for") or "") for d in (out, out.get("checkout") or {}, out.get("order") or {}) for l in d.get("line_items") or [] if isinstance(l, dict)]
        fi = {i: n for i in product_ids(out) + [i for i in stood_in if i] if (n := ((g.cat.get(i) or {}).get("names") or {}).get("fi"))}  # and the product a replacement stands in for
        if fi:
            x["names_fi"] = x["names"] = fi  # the card reads "names", the shop page "names_fi"
    except Exception:
        pass
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


# ---------------------------------------------------------------------------------------------------------------------
# What the assistant reads when a person sees the card. A chat that is handed the whole answer types it out again under
# the card, as a table. So an assistant that signed in through the consent page (a chat with a person) gets one line on
# what the card shows and what to do next, and only the fields it needs for its next call. The whole answer, signed, goes
# to the card. Every other caller (an agent with a mandate of its own, the REST side) reads the whole answer as before.

_chat: dict = {}
_kept: dict = {}  # result id -> (customer, tool, answer): for a chat that keeps _meta from the card, which then asks for it
CLIENTS: dict = {}  # agent id -> what its MCP client last said about itself (name, protocol, capabilities); kept by the /mcp gate in app.py
UI_EXT = "io.modelcontextprotocol/ui"
SEEN = "The person sees this as a card under your reply"


def in_chat(g, p) -> bool:
    """Is this an assistant in a chat that draws the card? It signed in through the consent page, and its client says it draws MCP Apps
    (clients that say nothing about themselves are taken as chats: the consent page is how chats arrive)."""
    if p and p.get("ok") and str(p.get("agent") or "").endswith("/shop-assistant"):  # Valio Aimo's own shop assistant (gateway/shopchat.py): its page draws the card when its client says so
        c = CLIENTS.get(p["agent"]) or g.store.get(f"mcp_client:{p['agent']}") or {}
        return UI_EXT in ((c.get("capabilities") or {}).get("extensions") or {})
    if not p or not p.get("ok") or p.get("bound"):
        return False
    a = p.get("agent")
    if a not in _chat or (not _chat[a][0] and time.time() - _chat[a][1] > 60):
        _chat[a] = (any((g.store.get(k) or {}).get("agent") == a for k, in g.store.q("select k from kv where k like 'oauth_grant:%'")), time.time())
    if not _chat[a][0]:
        return False
    c = CLIENTS.get(a) or g.store.get(f"mcp_client:{a}")
    return not c or UI_EXT in ((c.get("capabilities") or {}).get("extensions") or {})


def _eur(cents) -> str:
    return "€{:,.2f}".format((cents or 0) / 100)


def _amount(totals, kind="subtotal"):
    return next((t.get("amount") for t in totals or [] if t.get("type") == kind), None)


def _lines(items) -> list:
    return [{"item": {"id": (l.get("item") or {}).get("id"), "title": (l.get("item") or {}).get("title")}, "quantity": l["quantity"]["total"] if isinstance(l.get("quantity"), dict) else l.get("quantity")} for l in items or []]


def _said(out) -> str:
    return " ".join(m.get("content", "") for m in out.get("messages") or [] if m.get("type") in ("error", "warning") and m.get("content"))


def brief(tool: str, out: dict):
    """(one line for the assistant, the fields it needs for its next call), or None where the assistant reads the whole answer."""
    if (out.get("ucp") or {}).get("status") == "error":
        return None
    day = (out.get("wholesale") or {}).get("delivery_date") or out.get("delivery_date")
    if isinstance(out.get("options"), list) and out.get("need"):
        opts, need = out["options"], out["need"]
        slim = {"need": need, "delivery_date": day, "options": [{"label": o.get("label"), "line_items": _lines(o.get("line_items")), "covers_pct": o.get("covers_pct"), "total_ex_vat": o.get("total_ex_vat"),
                                                                 "authority": {"decision": (o.get("authority") or {}).get("decision")},
                                                                 **({"replacement": {k: o["substitute"].get(k) for k in ("tier", "title", "price_delta_pct", "why")}} if o.get("substitute") else {})} for o in opts]}
        for k in ("kinds_to_choose",):
            if out.get(k):
                slim[k] = out[k]
        if not opts:
            return f"Nothing can be offered as asked. {_said(out) or ' '.join(m.get('content', '') for m in out.get('messages') or [])} {SEEN}. Say it in one or two sentences.", slim
        names = "; ".join(f"{str(o.get('label')).replace('_', ' ')} {_eur(o.get('total_ex_vat'))} ex VAT ({'needs a second person' if (o.get('authority') or {}).get('decision') == 'needs_human_approval' else str((o.get('authority') or {}).get('decision')).replace('_', ' ')})" for o in opts)
        return (f"{len(opts)} option{'s' if len(opts) != 1 else ''} for {need.get('quantity'):g} {need.get('unit')}, delivery {day}: {names}. {SEEN}, each option with its price, stock, cut-off and a Choose button. "
                "Do not list or describe the options again. If the person already said which one they want, call create_checkout with that option's line_items now; otherwise say in one short sentence that they can choose on the card."), slim
    if isinstance(out.get("ap2"), dict) and out.get("id"):
        st, w = out.get("status"), out.get("wholesale") or {}
        ap, sub = w.get("approval"), _eur(_amount(out.get("totals")))
        stops = [r.get("title") for r in (w.get("policy") or {}).get("rules") or [] if r.get("outcome") != "pass"]
        slim = {"id": out["id"], "status": st, "delivery_date": day, "line_items": _lines(out.get("line_items")), "subtotal_ex_vat": _amount(out.get("totals")), "total": _amount(out.get("totals"), "total")}
        slim["rules"] = {r.get("id"): r.get("outcome") for r in (w.get("policy") or {}).get("rules") or []}
        if ap:
            slim["approval"] = {"decision": ap.get("decision"), "by": ap.get("by")}
        if out.get("order"):
            slim["order"] = out["order"]
        if st == "requires_escalation":
            slim["continue_url"] = out.get("continue_url")
            return (f"Signed offer {out['id']}, {sub} ex VAT, stopped for a second person ({', '.join(stops) or 'a rule'}). {SEEN}, with the reasons, a QR code and the approval link; the card watches for the approval by itself. "
                    "Say in one or two short sentences that a second person has to approve it on their own phone. Do not repeat the offer, do not open the link yourself, and do not call complete_checkout before you are told it is approved."), slim
        if st == "ready_for_complete":
            return (f"Signed offer {out['id']}, {sub} ex VAT, {'approved by ' + str(ap.get('by')) if ap and ap.get('decision') == 'approved' else 'every rule passed'}: ready to order. {SEEN}. "
                    "If the person told you to order this, call complete_checkout now. If they only asked for an offer, ask once, in one sentence. Do not repeat the offer."), slim
        if st == "completed":
            return (f"Order {(out.get('order') or {}).get('label')} is placed, {sub} ex VAT, delivery {day}. {SEEN}, with the signature checks they can run themselves, the Peppol document and a one-cent tamper test. "
                    "Confirm in one short sentence. Do not repeat the lines or the totals."), slim
        return f"Offer {out['id']} is {str(st).replace('_', ' ')}. {_said(out)} {SEEN}. Say what stopped it in one or two sentences.", slim
    if out.get("label") and out.get("proof") and (out.get("wholesale") or {}).get("evidence") is not None:
        slim = {"id": out.get("id"), "label": out["label"], "delivery_date": day, "line_items": _lines(out.get("line_items")), "subtotal_ex_vat": _amount(out.get("totals")), "total": _amount(out.get("totals"), "total"), "permalink_url": out.get("permalink_url")}
        return (f"Order {out['label']}, {_eur(_amount(out.get('totals')))} ex VAT, delivery {day}. {SEEN}, with seven signature checks run in the chat, the Peppol document and a one-cent tamper test. "
                "Say one short sentence. Do not repeat the order."), slim
    if isinstance(out.get("usual"), list):
        row = lambda u: {"id": u.get("id"), "title": u.get("title"), "kind": u.get("kind"), "usual_amount": u.get("usual_amount")}
        us, ot = out["usual"], out.get("other_kinds_ordered") or []
        slim = {"usual": [row(u) for u in us], "other_kinds_ordered": [row(u) for u in ot]}
        return (f"{len(us)} product{'s' if len(us) != 1 else ''} this kitchen usually orders{': ' + ', '.join(dict.fromkeys(str(u.get('kind')) for u in us)) if us else ''}. {SEEN}, as a list with the usual amounts and a button to order the usual. "
                "Do not list or tabulate them again. Say one short sentence and ask what they need."), slim
    if isinstance(out.get("products"), list):
        ps = out["products"]
        slim = {"delivery_date": day, "products": [{"id": x.get("id"), "title": x.get("title"), "unit_price": ((x.get("wholesale") or {}).get("price") or {}).get("unit_price"), "availability": (x.get("wholesale") or {}).get("availability"),
                                                      "orderable": (x.get("wholesale") or {}).get("orderable")} for x in ps[:12]]}
        return (f"{len(ps)} product{'s' if len(ps) != 1 else ''} found. {SEEN}, each with its price, stock, cut-off, origin and allergens. Do not list them again. "
                "Say one short sentence, or go on with what the person asked for."), slim
    if isinstance(out.get("proposals"), list):
        ps = out["proposals"]
        slim = {"proposals": [{"id": x.get("id"), "kind": x.get("kind"), "title": x.get("title"), "status": x.get("status"), "total_ex_vat": x.get("total_ex_vat"), "extra_ex_vat": x.get("extra_ex_vat"),
                               "authority": {"decision": (x.get("authority") or {}).get("decision")}} for x in ps]}
        if not ps:
            return "No open proposals from Valio Aimo's agent. The person sees one line saying so. Say it in a few words, or not at all if they did not ask.", slim
        return (f"{len(ps)} proposal{'s' if len(ps) != 1 else ''} from Valio Aimo's agent: {'; '.join(str(x.get('title')) for x in ps)}. {SEEN}, each with its reason, the numbers behind it and Accept and Decline buttons. "
                "Do not repeat them. Say one short sentence."), slim
    if isinstance(out.get("issues"), list):
        xs = out["issues"]
        slim = {"issues": [{"id": x.get("id"), "kind": x.get("kind"), "status": x.get("status"), "order": (x.get("order") or {}).get("label"), "what_happened": x.get("what_happened"),
                            "ways_out": [{"option": w.get("option"), "title": w.get("title"), "authority": {"decision": (w.get("authority") or {}).get("decision")}} for w in x.get("ways_out") or []]} for x in xs]}
        if not xs:
            return "No open problems on this account's placed orders. The person sees one line saying so. Say it in a few words.", slim
        return (f"{len(xs)} problem{'s' if len(xs) != 1 else ''} on placed orders. {SEEN}, each with its ways out, what each costs and a Choose button. Do not repeat them. "
                "Say one short sentence, or settle one with resolve_order_issue if the person asked you to and its way out says agent_may_resolve."), slim
    return None


def result(g, tool: str, out: dict) -> CallToolResult:
    """What the model reads (text and structured content) and what only the card reads (_meta)."""
    if tool == "card_data":  # for the card only; it carries its extras in the answer itself
        return CallToolResult(content=[TextContent(type="text", text=json.dumps(out, ensure_ascii=False))], structured_content=out, _meta={META: {"tool": tool, "site": PUBLIC_URL}})
    p = CALLER.get()
    x = extras(g, tool, out, p)
    try:
        b = brief(tool, out) if in_chat(g, p) else None
    except Exception:
        b = None  # the assistant reads the whole answer, as every other client does
    if not b:
        return CallToolResult(content=[TextContent(type="text", text=json.dumps(out, ensure_ascii=False))], structured_content=out, _meta={META: x})
    line, slim = b
    rid = "res_" + secrets.token_hex(6)
    _kept[rid] = (p.get("customer"), tool, out)
    while len(_kept) > 400:
        _kept.pop(next(iter(_kept)))
    slim = {"note": line, **slim, "card": rid}
    return CallToolResult(content=[TextContent(type="text", text=line + "\nFor your next call only, not for the person: " + json.dumps({k: v for k, v in slim.items() if k != "note"}, ensure_ascii=False))],
                          structured_content=slim, _meta={META: {**x, "full": out}})


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
    if kind == "result":  # a chat that kept the result's _meta from the card: the card asks for the whole answer by its id
        kept = _kept.get(id)
        if not kept or kept[0] != p.get("customer"):
            raise GatewayError(404, "not_found", "That answer is no longer kept. Ask the assistant again.")
        return {"full": kept[2], "card": extras(g, kept[1], kept[2], p)}
    raise GatewayError(400, "bad_request", "kind is checkout, order or result.")
