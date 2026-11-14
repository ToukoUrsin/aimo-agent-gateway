"""Every claim on /access, checked against the gateway in-process, plus the site navigation and the models a visitor can pick.
Run: AIMO_DATA=/tmp/aimo-access-t uv run -q python -m tests.access   (makes its own sandbox accounts; a few calls to the mock API)
"""
import re
from pathlib import Path

from fastapi.testclient import TestClient

from gateway.app import app

WEB = Path(__file__).resolve().parent.parent / "web"
PID, J = "463380", {"content-type": "application/json"}
n = 0


def ok(cond, what):
    global n
    n += 1
    print(("ok   " if cond else "FAIL ") + what)
    assert cond, what


def visitor():
    c = TestClient(app, base_url="https://testserver")  # the session cookie is secure when PUBLIC_URL is https
    m = c.get("/api/demo/mandate-token").json()["mandate"]
    return c, {"authorization": "Bearer " + m, **J}


c, H = visitor()
prof = lambda **kw: c.post("/api/demo/profile", json=kw).json()
look = lambda: c.post("/catalog/lookup", json={"ids": [PID]}, headers=H).json()["products"][0]
order = lambda: c.post("/checkout-sessions", json={"line_items": [{"item": {"id": PID}, "quantity": 100}]}, headers=H).json()
fired = lambda co: [(r["id"], r["outcome"]) for r in co["wholesale"]["policy"]["rules"] if r["outcome"] != "pass"]
cancel = lambda co: c.post(f"/checkout-sessions/{co['id']}/cancel", headers=H)

ok(c.get("/access").status_code == 200, "/access is served")
p = look()
ok(p["wholesale"]["price"]["unit_price"] and "list_price" in p["wholesale"]["price"] and "contract_price" in p["wholesale"]["price"], "a lookup carries this account's price, the list price and the contract-price mark")
ok(p["wholesale"]["availability"] in ("available", "limited", "out") and "stock" not in p["wholesale"], "by default: a band, no stock count")
u = c.get("/sourcing/usual", headers=H).json()["usual"]
ok(any(x["id"] == PID for x in u), "the product /access asks about is one the account usually orders")
plan = c.post("/sourcing/plan", json={"query": "lactose-free milk", "preferred_product": PID, "need": {"quantity": 1000, "unit": "L"}}, headers=H).json()
ok(all(l["item"]["id"] for o in plan["options"] for l in o["line_items"]) and plan["options"], "a plan answers with options from the catalogue")
ok(any(o.get("covers_pct") for o in plan["options"]), "a plan says how much of the request can be filled")

prof(show_exact_stock=True)
ok("stock" in look()["wholesale"], "exact stock shown: the lookup carries the stock record")
prof(show_exact_stock=False, show_contract_prices=False)
p = look()
ok("price" not in p["wholesale"] and "availability" not in p["wholesale"] and "price" not in p["variants"][0], "prices hidden: public facts only, no price, no availability")
co = order()
ok(co["status"] == "ready_for_complete" and co["line_items"][0]["item"].get("price"), "prices hidden: an offer still names the price (as /access says)")
cancel(co)
prof(show_contract_prices=True, tier="read")
co = order()
ok(co["status"] == "incomplete" and ("W2", "deny") in fired(co), "look up only: an order is refused by W2")
ok(look()["wholesale"].get("price"), "look up only: lookups still answer")
prof(tier="order", enabled=False)
co = order()
ok(("W1", "deny") in fired(co), "paused: an order is refused by W1")
ok(look()["wholesale"].get("price"), "paused: lookups still answer (as /access says)")
prof(enabled=True, max_order_total=20000)
co = order()
ok(co["status"] == "requires_escalation" and ("W3", "escalate") in fired(co), "a limit below the order: a person must approve (W3)")
ok(cancel(co).json()["status"] == "canceled", "the page's attempt can be cancelled")
prof(max_order_total=1500000)

# Another customer: its order and its checkout are refused, its ledger entries are hash and kind only.
c2, H2 = visitor()
co2 = c2.post("/checkout-sessions", json={"line_items": [{"item": {"id": "464201"}, "quantity": 100}]}, headers=H2).json()
done = c2.post(f"/checkout-sessions/{co2['id']}/complete", json={}, headers=H2).json()
ok(c.get(f"/orders/{done['order']['id']}", headers={**H, "accept": "application/json"}).status_code == 401, "another customer's order is refused")
ok(c.get(f"/checkout-sessions/{co2['id']}", headers=H).status_code == 403, "another customer's checkout is refused")
led = c.get("/ledger?format=json&limit=500", headers=H).json()["events"]
hid = [e for e in led if e.get("withheld")]
ok(hid and all(e["text"] is None and e["data"] is None and e["kind"] and e["hash"] for e in hid), "other accounts' ledger entries show only a hash and an event kind")

# Navigation: the shop chat first, /chat gone, /menu and /byhand one click deeper.
lib = (WEB / "lib.js").read_text()
nav = re.findall(r'\["(/[^"]*)", "([^"]+)"\]', lib[lib.index("const SITE_NAV"):lib.index("const SITE_MORE")])
more = re.findall(r'\["(/[^"]*)", "([^"]+)"\]', lib[lib.index("const SITE_MORE"):lib.index("const OPTIONAL")])
ok([h for h, _ in nav] == ["/shop", "/demo", "/access", "/seller", "/tower", "/proof"], "main navigation in the agreed order")
ok("/chat" not in [h for h, _ in nav + more], "/chat is not in the navigation")
ok({"/menu", "/byhand"} <= {h for h, _ in more}, "/menu and /byhand are in More")

# Models: Gemma is not offered to a visitor who waits.
ok("gemma-4-31b-it" not in [m["id"] for m in c.get("/api/console").json()["models"]], "Gemma is not offered on /demo")

# Words: no page of this part reads as an agent looking outside Valio Aimo, and none leads with a chat assistant.
for f in ("home.html", "index.html", "access.html", "spec.html"):
    t = (WEB / f).read_text().lower()
    ok("chatbot" not in t and "from the web" not in t and "outside valio aimo's range" not in t, f"{f}: no chatbot, nothing from outside Valio Aimo")

# Finnish: both pages have the FI / EN switch of /shop, the prototype banner in Finnish, and plain words in the Finnish text a person sees first.
from html.parser import HTMLParser


class FiText(HTMLParser):
    """The text of every fi-only element, leaving out the fold of technical detail and scripts."""
    VOID = {"input", "br", "img", "meta", "link", "hr", "source", "wbr"}

    def __init__(self):
        super().__init__()
        self.stack, self.out = [], []

    def handle_starttag(self, tag, attrs):
        if tag in self.VOID:
            return
        cls = (dict(attrs).get("class") or "").split()
        inside = self.stack[-1] if self.stack else "no"
        self.stack.append("skip" if inside == "skip" or "tech" in cls or tag in ("script", "style") else "fi" if inside == "fi" or "fi-only" in cls else inside)

    def handle_endtag(self, tag):
        if tag not in self.VOID and self.stack:
            self.stack.pop()

    def handle_data(self, data):
        if self.stack and self.stack[-1] == "fi":
            self.out.append(data)


BANNER = "Prototyyppi Valio Aimon haastedatalla, ei Valio Aimon järjestelmä."
PLAIN = ("mandate", "digest", "jws", "es256", "mcp", "ucp", "ap2", "peppol", "gtin", "agent gateway")
for f in ("access.html", "tower.html"):
    t = (WEB / f).read_text()
    ok('data-lang="fi"' in t and 'data-lang="en"' in t and 'localStorage.getItem("shop.lang")' in t and 'class="lang"' in t, f"{f}: FI / EN switch with the shop's key")
    ok(BANNER in t, f"{f}: banner in Finnish")
    p = FiText()
    p.feed(t)
    words = " ".join(p.out)
    ok(len(words) > 400, f"{f}: Finnish text found ({len(words)} characters)")
    bad = [w for w in PLAIN if re.search(r"\b" + re.escape(w) + r"\b", words, re.I)] + re.findall(r"\b[ABCDLMNPRSTVW][0-9]\b", words)
    ok(not bad, f"{f}: no mandate, digest or rule codes in the Finnish text a person sees" + (f" (found {bad})" if bad else ""))
fa = FiText()
fa.feed((WEB / "access.html").read_text())
for w in ("asiakkaan oma tilausohjelma tai tekoälyapuri", "tilausluvan", "korvaava", "tilausraja", "hyväksyjä", "ostoskori", "sopimushinta", "toimituspäivä"):
    ok(w in " ".join(fa.out).lower(), f"access.html: the Finnish text says '{w}'")
ok("asiakasnumero" in (WEB / "access.html").read_text().lower(), "access.html: the test account line names its asiakasnumero in Finnish")
print(f"\nALL {n} CHECKS PASS")
