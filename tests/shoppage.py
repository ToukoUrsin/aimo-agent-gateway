"""The shop page (web/shop.html) and the card's menu view (web/chat/card.html): served, wired to the shop API, and the card draws plan_menu_order.
Run: AIMO_DATA=/tmp/aimo-shoppage-t uv run python -m tests.shoppage   (no model call, no mock API call)"""
import re
from pathlib import Path

from fastapi.testclient import TestClient

from gateway.app import app

WEB = Path(__file__).resolve().parent.parent / "web"
ok = 0


def check(cond, what):
    global ok
    assert cond, what
    ok += 1


shop, card = (WEB / "shop.html").read_text(), (WEB / "chat" / "card.html").read_text()
c = TestClient(app)
r = c.get("/shop")
check(r.status_code == 200 and "Tilausapuri" in r.text, "/shop serves web/shop.html")
check("Not a Valio Aimo system." in shop and "ei Valio Aimon järjestelmä" in shop and "Näin tämä toimisi Valio Aimo -verkkokaupassa" in shop, "the prototype banner says whose system this is not, and where it would sit")
check(not re.search(r"A\.I\.mo|tarjous|avustaja|agentti|Peppol|mandaat|digest|GTIN", shop, re.I), "the customer's page has none of: A.I.mo, tarjous, avustaja, agentti, Peppol, mandate, digest, GTIN")
check('"/api/shop/orders' in shop.replace("`", '"') and "data-tab=\"orders\"" in shop and "data-tab=\"approvals\"" in shop, "Tilaukset and Hyväksynnät read /api/shop/orders")
check("type: \"confirm\"" in shop and "type: \"swap\"" in shop and "type: \"again\"" in shop and "data-say=\"${esc(t(\"usual\")" not in shop, "a card's buttons post actions done in code; a tile tap builds a cart and orders nothing")
check("makeStub" not in shop and "plan_sourcing" not in shop, "no card per product: the old option cards are gone from the page")
check("Näytä rivit" in shop and "Näytä tarkistukset" in shop and "Korvaava tuote:" in shop and "Vaihda" in shop and "Vahvista tilaus" in shop, "the cart card: lines and checks behind a tap, the replacement in the shop's words, one button")
check("Omaa tilaustasi et voi hyväksyä itse" in shop and "ei voitu tarkistaa" in shop, "the card says the asker cannot approve, and what could not be checked")
for api in ("/api/shop/hello?lang=", "/api/shop/chat", "/api/shop/reset"):
    check(api in shop, f"the page calls {api}")
check('href="/byhand"' in shop and 'href="/access"' in shop, "links to the by-hand order and to Valio Aimo's side")
check("card_token" in shop and '"/mcp"' in shop, "a card's own tool calls go to /mcp with hello.card_token")
check("ui/message" in shop and "ui/open-link" in shop and "ui/notifications/size-changed" in shop, "the page answers the MCP Apps messages a card sends")
check("lib.js" not in shop, "the shop page has its own header, not the shared one")
check("toDataURL(\"image/jpeg\"" in shop and "max = 1600" in shop, "photos are downscaled to 1600 px JPEG")
check(not re.search(r"TODO|coming soon|—", shop + card), "no TODO, coming soon or em dash")
# the card: plan_menu_order has its own view, with the button that asks the assistant to make the order
check("function vMenu(d)" in card and 'S.kind === "menu" ? vMenu(S.d)' in card, "the card routes plan_menu_order to its own view")
check("Array.isArray(d.lines) && Array.isArray(d.not_ordered)" in card, "a menu plan is recognised by its lines and not_ordered")
check("Make the order from this menu plan" in card and 'data-act="say"' in card, "one button sends the order request back to the chat")
check("Replaces a short product" in card and "Not ordered, a person decides" in card, "replacements and what is not ordered are shown")
# the card in Finnish, on a phone, with a short label for the chat bubble
check('"fi.valioaimo/label"' in card, "a card's ui/message carries a short label in _meta")
check("ui/notifications/host-context-changed" in card and '=== "aimo-shop"' in card, "the card redraws on a language switch and knows it sits in the shop")
check("Korvaava tuote: paras" in card and "Korvaava tuote: parempi" in card and "Korvaava tuote: hyvä" in card, "substitute tiers are titled in Valio Aimo's words")
check("Näytä kaikki" in card, "long line lists fold after the first lines")
check("tradeoff" in card and "from_range" in card, "the menu substitute shows its tradeoff and whether it is Valio Aimo's own range")
# the card in the shop: Valio Aimo's own words, and the technical parts behind one fold
fi_card = re.sub(r'L\("(?:[^"\\]|\\.)*", ', "", card)
for w in ("Ostokset (alv 0 %)", "Yhteensä (sis. alv)", "Toimituspäivä", "Tilaa viimeistään", "Vahvista tilaus", "Vahvistettu tilaus", "Usein ostetut", "Tilaa uudelleen", "Myyntierä", "Sopimushinta", "listahinta", "toimituspoikkeama", "Näytä tarkistukset", "Ilman hyväksyntää saa tilata enintään", '"Vaihda"'):
    check(w in card, f"the card says {w}")
check(not re.search(r"[Tt]arjou|[Aa]vustaj|[Aa]gentti|GTIN \(", fi_card.split("// ------------------------------------------------------------------ views")[1]), "no tarjous, avustaja or agentti in the Finnish views")
check("const tech = html =>" in card and "if (inShop()) {" in card and 'details class="checks"' in card, "in the shop, rules, signatures, Peppol and the raw text go behind Näytä tarkistukset")
byhand = (WEB / "byhand.html").read_text()
check(c.get("/byhand").status_code == 200 and '<html lang="fi">' in byhand and "Sama tilaus käsin" in byhand and 'data-lang="en"' in byhand and "shop.lang" in byhand, "/byhand opens in Finnish with the shop's FI/EN switch")
check(not re.search(r"The agent|agentti|Aimo Agent Gateway</title>", byhand) and "Tilausapuri" in byhand and "Mitään ei tilata" in byhand, "/byhand calls the assistant tilausapuri, not an agent, and says nothing is ordered")
check("open_cart" in card and "samat tuotteet tulevat kahteen kertaan" in card, "a proposal says when the customer's own open cart already holds its products")
print(f"shoppage: all {ok} checks pass")
