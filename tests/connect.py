"""An assistant with no key connects the way Claude or ChatGPT would: the MCP SDK's own OAuth client does
discovery, registration and PKCE against the gateway; this script plays the person on the consent page.
Run: uv run python -m tests.connect [base_url]"""
import asyncio
import json
import sys
from urllib.parse import parse_qs, urlparse

import httpx
from mcp import Client
from mcp.client.auth import AuthorizationCodeResult, OAuthClientProvider, TokenStorage
from mcp.client.streamable_http import create_mcp_http_client, streamable_http_client
from mcp.shared.auth import OAuthClientMetadata

BASE = (sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8642").rstrip("/")
DAY = "2026-11-16"


class Mem(TokenStorage):
    tokens = info = None
    async def get_tokens(self): return self.tokens
    async def set_tokens(self, t): self.tokens = t
    async def get_client_info(self): return self.info
    async def set_client_info(self, i): self.info = i


async def main():
    person = httpx.AsyncClient(base_url=BASE, timeout=30)  # the buyer's browser, with its own cookie
    got = {}

    async def open_consent_page(url):
        q = {k: v[0] for k, v in parse_qs(urlparse(url).query).items()}
        assert (await person.get(urlparse(url).path)).status_code == 200
        ctx = (await person.get("/api/oauth/context", params={"client_id": q["client_id"], "redirect_uri": q["redirect_uri"]})).json()
        print("consent page:", ctx["client"], "wants to order for", ctx["account"]["name"])
        r = (await person.post("/api/oauth/decide", json={**q, "allow": True, "limits": {"max_per_order": 80000, "max_per_week": 300000, "new_products": "ask", "substitution": "listed"}})).json()
        back = {k: v[0] for k, v in parse_qs(urlparse(r["redirect"]).query).items()}
        got.update(code=back["code"], state=back.get("state"))

    async def callback():
        return AuthorizationCodeResult(code=got["code"], state=got["state"])

    auth = OAuthClientProvider(server_url=f"{BASE}/mcp", storage=Mem(), redirect_handler=open_consent_page, callback_handler=callback,
                               client_metadata=OAuthClientMetadata(client_name="Test assistant", redirect_uris=["http://localhost:3999/callback"], grant_types=["authorization_code", "refresh_token"], response_types=["code"], scope="order"))
    async with create_mcp_http_client(auth=auth) as hc, Client(streamable_http_client(f"{BASE}/mcp", http_client=hc)) as mcp:
        tools = [t.name for t in (await mcp.list_tools()).tools]
        print("tools:", tools)

        async def call(name, args):
            res = await mcp.call_tool(name, args)
            return res.structured_content or json.loads(res.content[0].text)

        small = await call("create_checkout", {"line_items": [{"item": {"id": "464201"}, "quantity": 200}], "delivery_date": DAY, "intent": "200 L of our usual lactose-free milk"})
        rules = {r["id"]: r["outcome"] for r in small["wholesale"]["policy"]["rules"]}
        print("small re-order:", small["status"], rules)
        assert small["status"] == "ready_for_complete" and rules.get("T1") == "pass", small["messages"]
        # The same assistant in a chat that draws the card (its client says so, MCP Apps) reads a short answer instead: what happened, what the
        # person sees, and the fields its next call needs. The whole signed answer goes to the card, and stays readable by the result's id.
        said = {"io.modelcontextprotocol/protocolVersion": "2026-07-28", "io.modelcontextprotocol/clientInfo": {"name": "test-chat", "version": "1"},
                "io.modelcontextprotocol/clientCapabilities": {"extensions": {"io.modelcontextprotocol/ui": {"mimeTypes": ["text/html;profile=mcp-app"]}}}}
        bearer = {"authorization": "Bearer " + (await auth.context.storage.get_tokens()).access_token, "accept": "application/json, text/event-stream"}

        async def in_chat(name, args):
            r = (await person.post("/mcp", headers=bearer, json={"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": name, "arguments": args, "_meta": said}})).json()["result"]
            return r

        r = await in_chat("get_checkout", {"id": small["id"]})
        short, text, card = r["structuredContent"], r["content"][0]["text"], r["_meta"]["fi.valioaimo/card"]
        print("in a chat:", text.splitlines()[0][:90], "|", len(text), "characters instead of", len(json.dumps(small)))
        assert short["status"] == "ready_for_complete" and short["rules"]["T1"] == "pass" and "wholesale" not in short and "ap2" not in short, "the assistant was handed the whole answer to re-type"
        assert len(text) < 1200 and card["full"]["ap2"] and card["full"]["wholesale"]["policy"]["rules"], "the card did not get the whole answer"
        whole = (await in_chat("card_data", {"kind": "result", "id": short["card"]}))["structuredContent"]["full"]
        assert whole["id"] == small["id"] and whole["ap2"], "the card cannot read the whole answer by the result's id"
        done = await call("complete_checkout", {"id": small["id"]})
        print("ordered without a key:", done["status"], done.get("order", {}).get("label"))
        assert done["status"] == "completed"
        big = await call("create_checkout", {"line_items": [{"item": {"id": "464201"}, "quantity": 400}], "delivery_date": DAY})
        rules = {r["id"]: r["outcome"] for r in big["wholesale"]["policy"]["rules"]}
        print("bigger re-order:", big["status"], rules)
        assert big["status"] == "requires_escalation" and rules.get("T1") == "escalate"
        await call("cancel_checkout", {"id": big["id"]})
    order = (await person.get(f"/orders/{done['order']['id']}", headers={"accept": "application/json"})).json()
    ev = order["wholesale"]["evidence"]
    print("evidence:", ev["agent_class"], ev["keyless_receipt"]["type"], "| placed by", order["wholesale"]["placed_by"])
    ref = (await person.post("/oauth/token", data={"grant_type": "refresh_token", "refresh_token": Mem.tokens.refresh_token if Mem.tokens else auth.context.current_tokens.refresh_token})).json()
    print("refresh:", "access_token" in ref)
    print("OK")

asyncio.run(main())
