"""Valio Aimo's shop assistant (gateway/shopchat.py) end to end over HTTP, the way the shop page uses it.
Run: uv run python -m tests.shop [base_url]        SHOP_NO_MODEL=1: a scripted stand-in drives the tools instead of a model,
so the door, the signing, the events, the cart's buttons and the thread memory are still tested without a model call.

What it holds the shop to: a request gives ONE ostoskori and nothing is ordered until the person confirms; a product that
is short comes with a replacement from Valio Aimo's own range and the other choices; a cart over the limit waits for
someone else; and the page can list what was ordered and what waits."""
import os
import sys
import time

import httpx

BASE = (sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8693").rstrip("/")
SCRIPT = os.environ.get("SHOP_NO_MODEL") == "1"
CARD = "fi.valioaimo/card"
ok = 0


SEEN = []  # every event of the last turn, for the checks that look past tools and text


def check(cond, what):
    global ok
    assert cond, what
    ok += 1


def turn(web, thread, text=None, action=None, lang="fi"):
    body = {"thread": thread, "lang": lang, **({"model": "script"} if SCRIPT else {}), **({"action": action} if action else {"text": text})}
    r = web.post("/api/shop/chat", json=body)
    assert r.status_code == 200, r.text
    t, t0, after, evs = r.json(), time.time(), 0, []
    while True:
        d = web.get(f"/api/shop/chat/{t['thread']}", params={"after": after}).json()
        for e in d["events"]:
            assert e["n"] == after + 1, "events are numbered from 1 without gaps"
            after = e["n"]
            if e["turn"] == t["turn"]:
                evs.append(e)
        if not d["busy"]:
            break
        assert time.time() - t0 < 100, "a turn ran past 90 s"
        time.sleep(0.4)
    end = evs[-1]
    SEEN[:] = evs
    tools = [e for e in evs if e["type"] == "tool"]
    said = " ".join(e["text"] for e in evs if e["type"] == "text")
    print(f"  {(text or action)!r}: {[e['name'] for e in tools]} -> {said[:120]!r} | {end['type']}"
          + (f" {end['ms'] / 1000:.1f} s, {end['model']}" if end["type"] == "done" else f": {end.get('text')}"))
    assert end["type"] == "done", end
    return t["thread"], tools, said, end


def cart_of(tools):
    """The one cart a turn drew: the checkout and the page's notes for it."""
    carts = [(e["result"]["_meta"][CARD]["full"], e["result"]["_meta"][CARD]) for e in tools
             if not e.get("quiet") and ((e["result"].get("_meta") or {}).get(CARD) or {}).get("full", {}).get("ap2")]
    assert len(carts) == 1, f"one request, one cart: {[e['name'] for e in tools]}"
    return carts[0]


def placed(web):
    return web.get("/api/shop/orders").json()


def main():
    web = httpx.Client(base_url=BASE, timeout=30)  # the visitor's browser, with its own cookie (its own sandbox account)
    h = web.get("/api/shop/hello", params={"lang": "fi"}).json()
    print("hello:", h["account"]["name"], "|", h["greeting"][:60], "| proposals", h["proposals"], "| shelf", len(h["products"]))
    check(h["account"]["name"] and h["account"]["customer"] and "ennen kuin vahvistat" in h["greeting"], "the greeting says nothing is ordered before the person confirms")
    labels = [c["label"] for c in h["chips"]]
    check("Tuote loppuu kesken (esimerkki)" in labels and "Sama kuin viime viikolla" in labels and "Mitä tilasin viime viikolla?" in labels and any(c.get("image") for c in h["chips"]), labels)
    check(h["card_token"].count(".") == 2 and isinstance(h["proposals"], int) and h["waiting"] == 0 and h["orders"] == 0, "hello carries the card token and the counts for the tabs")
    check(all({"id", "title", "price_text", "picture"} <= set(p) for p in h["products"]) and len(h["products"]) <= 8, "the shelf")
    en = web.get("/api/shop/hello", params={"lang": "en"}).json()
    check("Same as last week" in [c["text"] for c in en["chips"]] and "confirm" in en["greeting"].lower(), "English behind the switch")

    # 1. A product that is short: one cart, the replacement from Valio Aimo's own range on it, the other choices beside it. Nothing ordered.
    th, tools, said, end = turn(web, None, "60 litraa laktoositonta Arla Pro -vispikermaa maanantaiksi")
    full, meta = cart_of(tools)
    notes = meta["cart"]
    it = notes["items"][0]
    check(full["status"] == "ready_for_complete" and not full.get("order"), "a cart, not an order")
    check(not any(e["name"] in ("plan_sourcing", "complete_checkout") for e in tools), "no option cards and nothing placed in the turn that built the cart")
    check(full["wholesale"]["intent"] == "60 litraa laktoositonta Arla Pro -vispikermaa maanantaiksi", "the request is kept word for word")
    sub = [l for l in full["line_items"] if (l["wholesale"].get("substitutes_for") or {}).get("for")]
    check(it["status"] == "replaced" and len(sub) == 1 and len(full["line_items"]) == 2, "the short product and its replacement are two lines of the one cart")
    ch = it["choices"][it["chosen"]]
    check(ch["substitute"]["title_fi"] and ch["substitute"]["tier"] in ("good", "better", "best") and ch["substitute"]["contract_price"] is not None, "the replacement comes with its rank and whether it is at the contract price")
    check(len(it["choices"]) >= 3 and any((c.get("covers_pct") or 100) < 99.5 for c in it["choices"]), "the other choices: another replacement, or only what is in stock")
    check(meta["names_fi"] and full["ap2"]["merchant_authorization"] and full["wholesale"]["policy"]["rules"], "Finnish names for the lines; the signed offer with every rule's outcome behind the checks")
    check(not placed(web)["orders"], "nothing is ordered before the person confirms")
    cid, total = full["id"], full["totals"]

    # 2. "Vaihda": the person picks another choice; the same cart is updated in code, with no model.
    th, tools, said, end = turn(web, th, action={"type": "swap", "checkout": cid, "item": 0, "choice": 1})
    full, meta = cart_of(tools)
    check(end["model"] == "none" and full["id"] == cid and full["totals"] != total and meta["cart"]["items"][0]["chosen"] == 1, "the swap changed the same cart")
    check(not placed(web)["orders"], "a swap orders nothing")

    # 3. "Vahvista tilaus": the person's own button places it.
    th, tools, said, end = turn(web, th, action={"type": "confirm", "checkout": cid})
    full, meta = cart_of(tools)
    check(full["status"] == "completed" and full["order"]["label"].startswith("VA-AG-") and full["order"]["label"] in said, "confirmed, and the answer names the order")
    v = placed(web)
    check(len(v["orders"]) == 1 and v["orders"][0]["label"] == full["order"]["label"] and v["orders"][0]["lines"] == 2 and v["orders"][0]["url"], "Tilaukset lists it with its confirmation")
    check(v["last_week"]["sample"] and len(v["last_week"]["items"]) >= 3, "last week's order is marked as sample data")

    # 4. Over the limit: the cart waits for someone else; the asker's own button and own browser cannot get past that.
    th, tools, said, end = turn(web, th, "1 000 litraa laktoositonta maitojuomaa maanantaiksi")
    full, meta = cart_of(tools)
    big = full["id"]
    rules = {r["id"]: r["outcome"] for r in full["wholesale"]["policy"]["rules"] if r["outcome"] != "pass"}
    print("  cart:", full["status"], rules)
    check(full["status"] == "requires_escalation" and rules == {"B1": "escalate"}, "only the order limit sends it to an approver: a first-time product alone does not")
    check(meta.get("qr") and full.get("continue_url"), "the card has the approver's QR and link")
    if SCRIPT:
        check("on tehty" not in said.lower() and "hyväksy" in said.lower(), "a sentence claiming an order was placed is replaced in code: " + said)
    th, tools, said, end = turn(web, th, action={"type": "confirm", "checkout": big})
    check(not any(e["name"] == "complete_checkout" for e in tools) and len(placed(web)["orders"]) == 1, "the asker's button places nothing while it waits")
    v = placed(web)
    check(len(v["waiting"]) == 1 and v["waiting"][0]["id"] == big and v["waiting"][0]["rules"] == ["B1"] and v["waiting"][0]["url"], "Hyväksynnät lists what waits, with the link for the approver")
    k = full["continue_url"].split("k=")[1]
    r = web.post(f"/api/approve/{big}", json={"approve": True, "approver": "The cook", "k": k})
    check(r.status_code == 403 and "own_request" in r.text, "the browser that asked cannot approve its own order")

    # 5. A tile tap ("Tilaa uudelleen") builds a cart for the usual amount; it orders nothing.
    if h["products"]:
        n = len(placed(web)["orders"])
        th, tools, said, end = turn(web, th, action={"type": "again", "product": h["products"][0]["id"]})
        full, meta = cart_of(tools)
        check(full["status"] in ("ready_for_complete", "requires_escalation") and len(placed(web)["orders"]) == n, "a tile tap gives a cart to confirm, not an order")

    # 6. "Mitä tilasin viime viikolla?" is answered in the chat, with the list as a card.
    th, tools, said, end = turn(web, th, "Mitä tilasin viime viikolla?")
    lo = next(e for e in tools if e["name"] == "list_orders")["result"]["structuredContent"]
    check(lo["last_week"]["items"] and len(lo["orders"]) == 1 and said, "the chat lists orders and last week's order")

    # The assistant is a caller of the gateway's own door: the ledger has its calls under its own agent id, the person's confirmation under theirs, and the request word for word.
    led = web.get("/ledger", params={"format": "json", "limit": 300}).json()
    rows = led.get("events") if isinstance(led, dict) else led
    mine = [e for e in rows if "/shop-assistant" in str(e.get("actor"))]
    kinds = {e.get("kind") for e in rows}
    print("  ledger:", len(mine), "events by the shop assistant;", sorted(k for k in kinds if k)[:14])
    check("shop_turn" in {e.get("kind") for e in mine} and any(e.get("cid") == cid for e in rows), "the assistant's turns are in the ledger")
    check(any(e.get("kind") == "confirmed" and str(e.get("actor")).startswith("human:") for e in rows), "the confirmation is logged as the signed-in person's, not the assistant's")
    check(any(e.get("kind") == "refused" and "own_request" in str(e.get("data")) for e in rows), "the refused self-approval is in the ledger")
    check(any(e.get("kind") == "shop_turn" and "60 litraa laktoositonta Arla Pro -vispikermaa maanantaiksi" in str(e.get("data")) for e in rows), "the ledger keeps the request word for word")

    # 7. A changed request replaces the earlier cart; a question about the shop gets examples to tap; a late event says so on the cart.
    if SCRIPT:
        t2, tools, said, end = turn(web, None, "60 litraa laktoositonta Arla Pro -vispikermaa maanantaiksi")
        first = cart_of(tools)[0]
        t2, tools, said, end = turn(web, t2, "sittenkin 40 litraa kermaa")
        gone = [e for e in tools if e.get("replaced")]
        new = [e["result"]["_meta"][CARD]["full"] for e in tools if not e.get("replaced") and ((e["result"].get("_meta") or {}).get(CARD) or {}).get("full", {}).get("ap2")]
        check(len(gone) == 1 and gone[0]["result"]["_meta"][CARD]["full"]["id"] == first["id"] and gone[0]["result"]["_meta"][CARD]["full"]["status"] == "canceled" and gone[0]["result"]["_meta"][CARD]["cart"]["replaced"],
              "a changed request withdraws the earlier cart and its card is told so")
        check(len(new) == 1 and new[0]["id"] != first["id"] and new[0]["status"] in ("ready_for_complete", "requires_escalation") and new[0]["wholesale"]["intent"] == first["wholesale"]["intent"] + " / sittenkin 40 litraa kermaa",
              "the new cart takes its place and keeps both sentences word for word")
        waits = web.get("/api/shop/orders").json()["waiting"]
        check(first["id"] not in [w["id"] for w in waits], "the withdrawn cart waits for nobody")
        t3, tools, said, end = turn(web, None, "Miten tämä eroaa verkkokaupasta?")
        ex = [e for e in SEEN if e["type"] == "examples"]
        check(len(ex) == 1 and 1 <= len(ex[0]["chips"]) <= 2 and all(c.get("label") and c.get("text") for c in ex[0]["chips"]) and not tools, "a question about the shop gets one or two examples to tap, and no cart")
        t4, tools, said, end = turn(web, None, "tänään lounas 80 hengelle")
        full, meta = cart_of(tools)
        check(meta["cart"]["menu"]["late"] and full["wholesale"]["delivery_date"] > meta["cart"]["menu"]["event_date"], "an event the delivery cannot reach is marked late on the cart, so the page asks before it offers confirming")

    # 8. No amount given: nothing goes on a cart and the usual amount is offered; one tap takes it. Another amount for a line changes that line.
    if SCRIPT:
        t5, tools, said, end = turn(web, None, "tarvitsen maitoa")
        asks = [e for e in SEEN if e["type"] == "tool" and e["name"] == "build_cart"]
        it = asks[0]["result"]["structuredContent"]["cart"]["items"][0] if asks else {}
        ua = it.get("usual_amount") or {}
        check(len(asks) == 1 and len(tools) == 1 and it.get("status") == "ask_amount" and ua.get("quantity") and ua.get("unit") and ua.get("title_fi") and not [e for e in tools if "checkout" in e["name"]],
              "a product with no amount is not put on a cart: the question names the kitchen's usual amount")
        t5, tools, said, end = turn(web, t5, action={"type": "again", "product": ua["id"], "asked": True})
        full, meta = cart_of(tools)
        line = full["line_items"][0]
        check(len(full["line_items"]) == 1 and line["item"]["id"] == ua["id"] and full["wholesale"]["intent"].startswith("tarvitsen maitoa / ") and not meta["cart"]["items"][0].get("usual"),
              "one tap puts the usual amount on a cart, and the cart keeps the request word for word")
        before = line["wholesale"]["units"]
        t5, tools, said, end = turn(web, t5, "maitoa puolet vähemmän")
        full2, meta2 = cart_of(tools)
        check(full2["id"] == full["id"] and len(full2["line_items"]) == 1 and full2["line_items"][0]["wholesale"]["units"] < before and len([x for x in meta2["cart"]["items"] if not x.get("dropped")]) == 1
              and meta2["cart"]["items"][0].get("ref") == meta["cart"]["items"][0].get("ref"), "another amount for a line changes that line in the same cart: no line is added")

    # Errors are plain: nothing to say is a 400, another visitor cannot read the thread, reset forgets it.
    check(web.post("/api/shop/chat", json={"thread": None, "text": "  "}).status_code == 400, "an empty message is a 400")
    other = httpx.Client(base_url=BASE, timeout=30)
    check(other.get(f"/api/shop/chat/{th}").status_code == 404 and not other.get("/api/shop/orders").json()["orders"], "a thread and its orders are the visitor's own")
    check(web.post("/api/shop/reset").json()["ok"] and web.get(f"/api/shop/chat/{th}").status_code == 404, "reset forgets the thread")
    print(f"OK: {ok} checks passed")


main()
