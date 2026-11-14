"""Numbers for the home page: what agents did across every account, in one cheap call."""
import json
import time

from fastapi import APIRouter

from .config import PUBLIC_URL, ROOT


def build(g, chain_status) -> APIRouter:
    r = APIRouter(include_in_schema=False)
    cache = {"t": 0.0, "v": None}

    def proof():
        try:
            s = json.loads((ROOT / "evals" / "latest.json").read_text())
        except Exception:
            return None
        return s.get("summary") or s

    def compute():
        accounts = g.accounts()
        names = {a["slug"]: a["name"] for a in accounts}
        public = {a["slug"] for a in accounts if a["kind"] != "sandbox"}
        rows = g.store.q("select doc from orders order by created desc limit 5000")
        orders = [json.loads(x[0]) for x in rows]
        euros = sum(next(t["amount"] for t in o["totals"] if t["type"] == "subtotal") for o in orders)
        approved = sum(1 for o in orders if (o["wholesale"]["evidence"].get("approval") or {}).get("by"))
        agents = g.agents()
        intact, events, bad = chain_status()
        feed = [{"ts": e["ts"], "who": names.get(e.get("acct"), ""), "actor": e["actor"], "text": e["text"]}
                for e in g.store.events(limit=60) if e.get("acct") in public and e["kind"] not in ("plan", "search", "lookup", "checkout")][-8:]
        return {"public_url": PUBLIC_URL, "orders": len(orders), "euros": euros, "alone": len(orders) - approved, "approved": approved,
                "agents": len(agents), "suspended": sum(1 for a in agents if a.get("status") == "suspended"),
                "accounts": len(accounts), "ledger": {"intact": intact, "events": events}, "feed": feed, "proof": proof(),
                "film": (ROOT / "web" / "film.mp4").exists()}

    @r.get("/api/home")
    def home():
        if time.time() - cache["t"] > 3:
            cache.update(t=time.time(), v=compute())
        return cache["v"]

    return r
