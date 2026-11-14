"""MCP binding. Same operations and documents as the REST binding, using UCP's MCP tool names.

The mandate travels as an HTTP Authorization header on the MCP connection, set by the
agent's harness. The model never sees or handles keys or tokens.
"""
import anyio
from mcp.server.mcpserver import Context, MCPServer

from .service import Gateway, GatewayError

INSTRUCTIONS = """Valio Aimo agent gateway (Junction 2026 mock). You are an external purchasing agent ordering from a food-service wholesaler.
Flow: plan_sourcing (or search_catalog) -> create_checkout -> complete_checkout.
- Every response is signed by Valio Aimo; prices and availability in a checkout are held until expires_at. Stock is reported as a band or as the amount of the buyer's request that can be met, never as warehouse inventory.
- Quantities are integer steps of the item's quantity_unit. Use the line_items an option gives you unchanged.
- A checkout's status tells you your authority: ready_for_complete = you may order now; requires_escalation = a person must
  approve at continue_url first (give them that link and the reasons, then wait); incomplete = fix the listed errors.
- The guardrails are enforced by the gateway, not by you. Never try to work around a limit, for example by splitting an order.
- Money amounts are EUR minor units (cents), ex VAT unless stated.
After an order is placed Valio Aimo may report a problem with it: a short pick (only part of a line can be delivered), a delay, or a recall of a lot.
Flow: list_order_issues -> resolve_order_issue -> complete_checkout.
- Each issue lists its ways out, computed by Valio Aimo: what each one does, what it costs or credits, and under authority whether you may take it alone.
- Settle every open issue in this run. Do not stop to ask the buyer which way out to take: the mandate already says what you may settle alone
  (agent_may_resolve), and a way out that needs_human_approval is put to a person through the approval link, not through chat.
- Choose the way out that serves the buyer's original request best. If that one needs a person, choose it anyway; if the person says no, the issue
  opens again and you choose another.
- resolve_order_issue returns a checkout that changes the order. Finish it exactly like an order: ready_for_complete = call complete_checkout;
  requires_escalation = a person must approve at continue_url first, then call complete_checkout. Nothing changes on the order until complete_checkout succeeds.
- A recall cannot be declined: the lot is replaced or credited, never kept.
- While an issue is open the affected product cannot be ordered again for that day, so do not try to get around a notice with a new order.
Valio Aimo's own agent may also make proposals to this account: a standing order that has drifted from use, short-dated stock at a markdown, the usual basket before the cut-off,
a replacement for a product about to run short, or filling a part case. They work for Valio Aimo, not for the buyer.
Flow: list_proposals -> answer_proposal -> complete_checkout. Open proposals also ride along as open_proposals in plan_sourcing and checkout answers.
- Each proposal is a signed offer already judged under the buyer's mandate: authority.decision agent_may_accept = you may accept alone; needs_human_approval = accepting sends it to a person.
- Accept only what serves the buyer; decline the rest with a short reason. A declined proposal is never made again.
- Accepting returns a checkout: finish it like any other. Say in your final answer which proposals you took and which you left.
A whole menu or meal plan is ordered in one go. Flow: plan_menu_order -> create_checkout -> complete_checkout.
- Give plan_menu_order the dishes, the number of guests, the date and every dietary note as written (or a photo of the menu). Code works out the bill of materials from the kitchen's
  recipe cards, maps each ingredient to a product, rounds to pack size and checks allergens, stock and price per line. A short product is replaced by what the ingredient does in the dish
  (cooking cream may stand in as a sauce base, never as a whipped topping), with the reason on the line.
- status ready: pass its checkout (line_items, delivery_date, exclude_allergens, intent, rationale) to create_checkout unchanged, and tell the buyer what is under not_ordered.
  status needs_answers: ask the buyer the questions; order nothing from that answer. Do not look up menu ingredients one by one with plan_sourcing."""


def build(g: Gateway) -> MCPServer:
    srv = MCPServer("valio-aimo-agent-gateway", title="Valio Aimo agent gateway (mock)", instructions=INSTRUCTIONS, version="0.1.0")

    def who(ctx: Context):
        h = {k.lower(): v for k, v in dict(ctx.headers or {}).items()}
        auth = h.get("authorization", "")
        return g.identify(auth[7:].strip() if auth.lower().startswith("bearer ") else None), h.get("x-trace-id")

    async def guard(fn):
        try:
            return await anyio.to_thread.run_sync(fn)
        except GatewayError as e:
            out = {"ucp": {"status": "error"}, "messages": [{"type": "error", "code": e.code, "severity": "unrecoverable", "content": e.message}]}
            if e.retry_after is not None:
                out["retry_after"] = e.retry_after
            return out

    def filters(claims, exclude_allergens, labels, categories, origin):
        return {k: v for k, v in {"claims": claims, "exclude_allergens": exclude_allergens, "labels": labels, "categories": categories, "origin": origin}.items() if v}

    @srv.tool(description="Search Valio Aimo's catalog. With a mandate, returns products with contract price and availability band, order cut-off, allergens, claims, certificates, origin, delivery reliability and CO2 for the delivery date. Without one, returns public catalogue facts only. "
                          "claims are hard requirements such as lactose_free, gluten_free, vegan. need_quantity + need_unit (L or KG) adds how many sales units cover the need.")
    async def search_catalog(ctx: Context, query: str, delivery_date: str = "", claims: list[str] = [], exclude_allergens: list[str] = [], labels: list[str] = [],
                       categories: list[str] = [], origin: list[str] = [], need_quantity: float = 0, need_unit: str = "", limit: int = 8) -> dict:
        p, tr = who(ctx)
        need = {"quantity": need_quantity, "unit": need_unit} if need_quantity else None
        return await guard(lambda: g.search({"query": query, "filters": filters(claims, exclude_allergens, labels, categories, origin), "pagination": {"limit": limit},
                                       "wholesale": {"delivery_date": delivery_date, "need": need}}, p, tr))

    @srv.tool(description="Look up products by Valio Aimo product id or GTIN, with contract price and an availability band for the delivery date when a mandate permits it.")
    async def lookup_catalog(ctx: Context, ids: list[str], delivery_date: str = "") -> dict:
        p, tr = who(ctx)
        return await guard(lambda: g.lookup(ids, delivery_date, p, tr))

    @srv.tool(description="Turn a need into ranked ready-to-order options. Give the product type as query (e.g. 'lactose-free milk'), need_quantity and need_unit (L or KG) and delivery_date (YYYY-MM-DD). "
                          "Handles pack sizes, partial stock and substitutes, and tells you for each option whether you may order it yourself or a person must approve. "
                          "Set preferred_product to a product id when the buyer wants a specific product.")
    async def plan_sourcing(ctx: Context, query: str, need_quantity: float, need_unit: str, delivery_date: str, preferred_product: str = "", claims: list[str] = [],
                      exclude_allergens: list[str] = [], labels: list[str] = [], categories: list[str] = [], origin: list[str] = []) -> dict:
        p, tr = who(ctx)
        return await guard(lambda: g.plan({"query": query, "need": {"quantity": need_quantity, "unit": need_unit}, "delivery_date": delivery_date, "preferred_product": preferred_product or None,
                                     "filters": filters(claims, exclude_allergens, labels, categories, origin)}, p, tr))

    def checkout_req(line_items, delivery_date, claims, exclude_allergens, labels, purchase_order, intent, rationale):
        return {"line_items": line_items, "context": {"intent": intent},
                "wholesale": {"delivery_date": delivery_date, "purchase_order": purchase_order or None, "rationale": rationale, "requirements": filters(claims, exclude_allergens, labels, None, None)}}

    @srv.tool(description="Create a checkout: Valio Aimo validates the lines against live price and availability, runs the buyer's mandate and its own agent rules, holds price and stock, and returns a signed offer. "
                          "line_items: [{item: {id}, quantity, wholesale?: {substitutes_for: product_id}, requirements?: {claims, exclude_allergens, labels}}]. Pass the buyer's hard requirements again so they are enforced in code: "
                          "in claims / exclude_allergens / labels when they hold for the whole order, or in a line's own requirements when they hold for that product only (a basket with milk and a gluten-free oat drink: put FREE_FROM:GLUTEN on the oat drink's line). "
                          "intent: the buyer's request in their own words, for the audit trail. rationale: one or two sentences on why you chose these lines; a human approver reads it.")
    async def create_checkout(ctx: Context, line_items: list[dict], delivery_date: str, intent: str = "", rationale: str = "", claims: list[str] = [], exclude_allergens: list[str] = [], labels: list[str] = [], purchase_order: str = "") -> dict:
        p, tr = who(ctx)
        return await guard(lambda: g.create_checkout(checkout_req(line_items, delivery_date, claims, exclude_allergens, labels, purchase_order, intent, rationale), p, tr))

    @srv.tool(description="Replace the lines of an existing checkout (for example after an insufficient_stock error) and get a fresh signed offer.")
    async def update_checkout(ctx: Context, id: str, line_items: list[dict], delivery_date: str, intent: str = "", rationale: str = "", claims: list[str] = [], exclude_allergens: list[str] = [], labels: list[str] = [], purchase_order: str = "") -> dict:
        p, tr = who(ctx)
        return await guard(lambda: g.update_checkout(id, checkout_req(line_items, delivery_date, claims, exclude_allergens, labels, purchase_order, intent, rationale), p, tr))

    @srv.tool(description="Fetch a checkout's current state, for example to see whether a person has approved it.")
    async def get_checkout(ctx: Context, id: str) -> dict:
        p, _ = who(ctx)
        return await guard(lambda: g.get_checkout(id, p))

    @srv.tool(description="Place the order. Only works when the checkout status is ready_for_complete. The agent's harness adds the signature over the offer; leave checkout_mandate empty.")
    async def complete_checkout(ctx: Context, id: str, checkout_mandate: str = "") -> dict:
        p, _ = who(ctx)
        return await guard(lambda: g.complete_checkout(id, {"ap2": {"checkout_mandate": checkout_mandate}} if checkout_mandate else {}, p))

    @srv.tool(description="Cancel a checkout and release the held stock.")
    async def cancel_checkout(ctx: Context, id: str) -> dict:
        p, _ = who(ctx)
        return await guard(lambda: g.cancel_checkout(id, p))

    @srv.tool(description="Fetch a placed order with its signed confirmation and evidence.")
    async def get_order(ctx: Context, id: str) -> dict:
        p, _ = who(ctx)
        def read():
            if not p or not p.get("ok"):
                raise GatewayError(401, (p or {}).get("error", "mandate_required"), (p or {}).get("detail", "Reading an order confirmation needs the account's mandate."))
            o = g.get_order(id)
            if o["wholesale"].get("customer") != p.get("customer"):
                raise GatewayError(403, "forbidden", "This order belongs to another account.")
            return o
        return await guard(read)

    @srv.tool(description="List the problems Valio Aimo has reported on this account's placed orders: short picks, delays and recalls. order: an order id or its number (VA-AG-...), empty for all orders. "
                          "Each issue says what happened, shows the buyer's original request, and lists its ways out: option (the id to pass to resolve_order_issue), what it does, credit_ex_vat, cost_ex_vat, "
                          "net_ex_vat, and authority (agent_may_resolve = you may take it alone; needs_human_approval = a person must approve it; reasons say why). Signed by Valio Aimo.")
    async def list_order_issues(ctx: Context, order: str = "", include_settled: bool = False) -> dict:
        p, tr = who(ctx)
        return await guard(lambda: g.after.list(p, order, include_settled, tr))

    @srv.tool(description="Answer an issue on a placed order by choosing one of its ways out. id: the issue id. option: the option id of the way out, exactly as listed. "
                          "rationale: one or two sentences on why this way out serves the buyer; a person reads it. Returns a signed checkout that changes the order: if its status is ready_for_complete, "
                          "call complete_checkout with its id to make the change final; if requires_escalation, a person must approve at continue_url first, then call complete_checkout. "
                          "A way out with replacement goods ends as a new order linked to the original; any other ends as a signed amendment to the original order. The original confirmation is never altered.")
    async def resolve_order_issue(ctx: Context, id: str, option: str, rationale: str = "") -> dict:
        p, tr = who(ctx)
        return await guard(lambda: g.after.resolve(id, option, rationale, p, tr))

    @srv.tool(description="List proposals from Valio Aimo's own agent to this account: each has kind, title, reason, basis (the numbers behind it), line_items, total_ex_vat, extra_ex_vat "
                          "and authority (agent_may_accept = you may accept alone under the buyer's mandate; needs_human_approval = accepting sends it to a person). Signed by Valio Aimo.")
    async def list_proposals(ctx: Context, include_answered: bool = False) -> dict:
        p, tr = who(ctx)
        return await guard(lambda: g.seller.list(p, include_answered, tr))

    @srv.tool(description="Answer a proposal from Valio Aimo's agent. answer: accept or decline. reason: one sentence on why, for the buyer and for Valio Aimo. "
                          "accept returns a signed checkout: ready_for_complete = call complete_checkout; requires_escalation = a person must approve at continue_url first. decline is final.")
    async def answer_proposal(ctx: Context, id: str, answer: str, reason: str = "") -> dict:
        p, tr = who(ctx)
        return await guard(lambda: g.seller.answer(id, answer, reason, p, tr))

    @srv.tool(description="Turn a whole menu or meal plan into one checked order plan. dishes: [\"Creamy salmon soup\", {name, portions?, days?, notes?: [\"L\", \"G\", \"vegetarian\"], recipe?: [{item, role, amount, unit: g|ml}]}]; "
                          "portions only when a dish has its own number, recipe (per portion) only for a dish the kitchen has no card for. guests: how many it is for. event_date: YYYY-MM-DD of the first day served. "
                          "notes: every dietary note as written, e.g. [\"Nut-free kitchen\", \"Table 6 lactose-free\", \"6 gluten-free\"]. image: instead of dishes, a photo of the menu as a data URL (JPEG, PNG or WebP). "
                          "Returns lines (product, units, the dishes each is for, and the pack, allergen, stock and price checks code ran), not_ordered (what a person must sort out), diet (the rules applied) "
                          "and checkout: pass it to create_checkout unchanged. status needs_answers means code found something to ask the buyer (for example 10,000 guests, or a line that is not food): order nothing.")
    async def plan_menu_order(ctx: Context, dishes: list = [], guests: int = 0, event_date: str = "", notes: list[str] = [], title: str = "", image: str = "") -> dict:
        from . import menu
        p, tr = who(ctx)
        return await guard(lambda: menu.plan_menu_order(g, {"dishes": dishes, "guests": guests or None, "event_date": event_date, "notes": notes, "title": title, "image": image or None}, p, tr))

    return srv
