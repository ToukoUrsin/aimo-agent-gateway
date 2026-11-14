"""Sign-in for assistants that hold no key of their own (Claude, ChatGPT, any MCP client).

The standard MCP authorization flow: protected-resource metadata (RFC 9728), authorization-server
metadata (RFC 8414), dynamic client registration (RFC 7591), authorization code with PKCE (S256).

What is specific here is the consent step. The person does not tick "allow access". They set what the
assistant may order alone, and that becomes a mandate signed by their company. The access token IS that
mandate, so the same rules in policy.py apply to a chat assistant as to any other agent.
"""
import base64
import hashlib
import re
import secrets
import time
from urllib.parse import urlencode, urlparse

from fastapi import APIRouter, Body, Request
from fastapi.responses import JSONResponse, Response

from . import policy
from .config import PUBLIC_URL
from .service import GatewayError

TOKEN_TTL = 8 * 3600
CODE_TTL = 300
SCOPE = "order"


def build(g, ctx, page) -> APIRouter:
    r = APIRouter()
    codes: dict[str, dict] = {}

    def resource_meta():
        return {"resource": f"{PUBLIC_URL}/mcp", "authorization_servers": [PUBLIC_URL], "bearer_methods_supported": ["header"], "scopes_supported": [SCOPE],
                "resource_name": "Valio Aimo agent gateway (Junction 2026 mock)", "resource_documentation": f"{PUBLIC_URL}/spec"}

    def server_meta():
        return {"issuer": PUBLIC_URL, "authorization_endpoint": f"{PUBLIC_URL}/oauth/authorize", "token_endpoint": f"{PUBLIC_URL}/oauth/token", "registration_endpoint": f"{PUBLIC_URL}/oauth/register",
                "response_types_supported": ["code"], "grant_types_supported": ["authorization_code", "refresh_token"], "code_challenge_methods_supported": ["S256"],
                "token_endpoint_auth_methods_supported": ["none"], "scopes_supported": [SCOPE], "service_documentation": f"{PUBLIC_URL}/spec"}

    for path in ("/.well-known/oauth-protected-resource", "/.well-known/oauth-protected-resource/mcp"):
        r.add_api_route(path, resource_meta, methods=["GET"], tags=["discovery"], summary="Where an MCP client signs in (RFC 9728)")
    for path in ("/.well-known/oauth-authorization-server", "/.well-known/oauth-authorization-server/mcp", "/.well-known/openid-configuration", "/.well-known/openid-configuration/mcp", "/mcp/.well-known/openid-configuration"):
        r.add_api_route(path, server_meta, methods=["GET"], include_in_schema=path.endswith("authorization-server"), tags=["discovery"], summary="Sign-in endpoints (RFC 8414)")

    def ok_redirect(u: str) -> bool:
        x = urlparse(u)
        return bool(x.scheme) and x.scheme not in ("javascript", "data", "file", "vbscript") and (x.scheme != "http" or x.hostname in ("localhost", "127.0.0.1", "::1")) and not x.fragment

    @r.post("/oauth/register", status_code=201, tags=["discovery"], summary="Register an MCP client (RFC 7591)")
    def register(body: dict = Body(...)):
        uris = [str(u) for u in body.get("redirect_uris") or []][:10]
        if not uris or not all(ok_redirect(u) for u in uris):
            return JSONResponse({"error": "invalid_redirect_uri", "error_description": "redirect_uris must be https, a localhost http address, or an app scheme."}, status_code=400)
        cl = {"client_id": "cl_" + secrets.token_urlsafe(12), "client_name": re.sub(r"[^\w .()+\-]", "", str(body.get("client_name") or "An assistant"))[:60].strip() or "An assistant", "redirect_uris": uris,
              "grant_types": ["authorization_code", "refresh_token"], "response_types": ["code"], "token_endpoint_auth_method": "none", "client_id_issued_at": int(time.time())}
        g.store.put("oauth_client:" + cl["client_id"], cl)
        return cl

    def client_for(client_id, redirect_uri):
        cl = g.store.get("oauth_client:" + str(client_id))
        if not cl:
            raise GatewayError(400, "invalid_client", "Unknown client. Register first (the client does this itself).")
        if redirect_uri not in cl["redirect_uris"]:
            raise GatewayError(400, "invalid_redirect_uri", "This redirect address is not registered for the client.")
        return cl

    @r.get("/oauth/authorize", include_in_schema=False)
    def authorize():
        return page("connect.html")

    @r.get("/api/oauth/context", include_in_schema=False)
    def context(request: Request, response: Response, client_id: str = "", redirect_uri: str = ""):
        cl, a = client_for(client_id, redirect_uri), ctx(request, response)["acct"]
        return {"client": cl["client_name"], "returns_to": urlparse(redirect_uri).hostname or urlparse(redirect_uri).scheme, "account": {"name": a["name"], "kind": a["kind"]},
                "mandate": g.store.get("mandate_config:" + a["customer"]), "keyless_cap": (g.store.get("profile:" + a["customer"]) or {}).get("keyless_cap", 50000)}

    @r.post("/api/oauth/decide", include_in_schema=False)
    def decide(request: Request, response: Response, body: dict = Body(...)):
        """The person's answer on the consent page. Allow = a mandate with the limits they just set."""
        cl, a = client_for(body.get("client_id"), body.get("redirect_uri")), ctx(request, response)["acct"]
        back = lambda q: {"redirect": body["redirect_uri"] + ("&" if "?" in body["redirect_uri"] else "?") + urlencode({**q, **({"state": body["state"]} if body.get("state") else {})})}
        if body.get("code_challenge_method", "S256") != "S256" or not body.get("code_challenge"):
            return back({"error": "invalid_request", "error_description": "PKCE with S256 is required."})
        if not body.get("allow"):
            g.store.log(None, "human:buyer admin", "control", f"{a['name']} declined to connect {cl['client_name']}.", acct=a["customer"])
            return back({"error": "access_denied"})
        lim = body.get("limits") or {}
        m = policy.default_mandate()
        m["budget"].update(max_per_order=max(0, min(int(lim.get("max_per_order", 50000)), 5_000_000)), max_per_week=max(0, min(int(lim.get("max_per_week", 200000)), 20_000_000)))
        m["new_products"] = "allow" if lim.get("new_products") == "allow" else "ask"
        m["substitution"]["allow"] = "none" if lim.get("substitution") == "none" else "listed"
        agent = f"agent:{a['slug']}/{re.sub(r'[^a-z0-9]+', '-', cl['client_name'].lower()).strip('-')[:24] or 'assistant'}-{secrets.token_hex(2)}"
        code = secrets.token_urlsafe(24)
        codes[code] = {"exp": time.time() + CODE_TTL, "client_id": cl["client_id"], "redirect_uri": body["redirect_uri"], "challenge": body["code_challenge"],
                       "grant": {"slug": a["slug"], "agent": agent, "name": cl["client_name"], "client_id": cl["client_id"], "mandate": m, "created": time.time()}}
        g.store.log(None, "human:buyer admin", "control", f"{a['name']} connected {cl['client_name']} as {agent}: alone up to {policy.eur(m['budget']['max_per_order'])} per order and {policy.eur(m['budget']['max_per_week'])} per week, "
                    f"new products '{m['new_products']}', substitutes '{m['substitution']['allow']}'. Everything else comes to a person.", {"agent": agent, "mandate": m}, acct=a["customer"])
        return back({"code": code})

    def tokens(grant, refresh):
        tok = g.issue_mandate(grant["slug"], constraints=grant["mandate"], ttl=TOKEN_TTL, agent=grant["agent"], bound=False, name=grant["name"])
        return JSONResponse({"access_token": tok, "token_type": "Bearer", "expires_in": TOKEN_TTL, "refresh_token": refresh, "scope": SCOPE}, headers={"Cache-Control": "no-store"})

    @r.post("/oauth/token", tags=["discovery"], summary="Exchange a code (or refresh token) for a mandate")
    async def token(request: Request):
        f = dict(await request.form())
        err = lambda code, text: JSONResponse({"error": code, "error_description": text}, status_code=400)
        if f.get("grant_type") == "authorization_code":
            code = str(f.get("code"))
            c = codes.get(code)
            if not c or c["exp"] < time.time():
                return err("invalid_grant", "The code is unknown, used or expired.")
            if f.get("client_id") != c["client_id"] or f.get("redirect_uri") != c["redirect_uri"]:
                return err("invalid_grant", "client_id or redirect_uri does not match the request the code was issued for.")
            want = base64.urlsafe_b64encode(hashlib.sha256(str(f.get("code_verifier") or "").encode()).digest()).rstrip(b"=").decode()
            if not secrets.compare_digest(want, c["challenge"]):
                return err("invalid_grant", "PKCE check failed.")
            codes.pop(code, None)
            refresh = "rt_" + secrets.token_urlsafe(32)
            g.store.put("oauth_grant:" + refresh, c["grant"])
            return tokens(c["grant"], refresh)
        if f.get("grant_type") == "refresh_token":
            grant = g.store.get("oauth_grant:" + str(f.get("refresh_token")))
            if not grant or (f.get("client_id") and f["client_id"] != grant["client_id"]) or not g.account(grant["slug"]):
                return err("invalid_grant", "The refresh token is unknown or was disconnected.")
            return tokens(grant, f["refresh_token"])
        return err("unsupported_grant_type", "Use authorization_code or refresh_token.")

    @r.post("/api/oauth/disconnect", include_in_schema=False)
    def disconnect(request: Request, body: dict = Body(...)):
        """The buyer's side: cut one connected assistant off. Its refresh token dies and the agent is suspended."""
        a, n = ctx(request)["acct"], 0
        for k, in g.store.q("select k from kv where k like 'oauth_grant:%'"):
            gr = g.store.get(k)
            if gr and gr["slug"] == a["slug"] and gr["agent"] == body.get("id"):
                g.store.q("delete from kv where k=?", (k,))
                n += 1
        if g.store.get("agent:" + str(body.get("id"))):
            g.set_agent(body["id"], True, by=a["name"], reason="disconnected by the buyer")
        return {"disconnected": n}

    return r
