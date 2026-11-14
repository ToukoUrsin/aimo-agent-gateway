"""Valio Aimo's shop assistant: a chat inside Valio Aimo's own web shop, for the signed-in customer.

The cook or purchasing manager says what they need ("10 kg jauhelihaa, 5 l kermaa ja 200 munaa", "häät lauantaina, 180 henkeä:
lohikeitto ...", a pasted menu, a photo) and gets ONE ostoskori (cart), not a card per product. A product that is short gets a
replacement from Valio Aimo's own range on the cart, with the other choices one tap away. Nothing is ordered until the person
presses "Vahvista tilaus"; an order over the account's limits first goes to a second person.

The assistant is the first caller of the gateway's own door, not a way around it. A model (Gemini 4 Hydrogen, Claude Opus 5.5
when that fails) reads the request and names what is asked for; code here plans each item, puts every line into one checkout
through the same MCP tools over HTTP on this same server, under a mandate the account signed for `agent:<slug>/shop-assistant`,
so every call lands in the ledger and the control tower and the same rules decide. The harness here holds what a model must
not: the mandate, the agent key that signs a completion, the approval link. The model has no tool that places an order.

    GET  /api/shop/hello?lang=fi|en        the account, the first line, chips, the shelf, a token for the card
    POST /api/shop/chat                    {thread, text, image, lang} or {thread, action, lang} -> {thread, turn}; the turn runs in the background
    GET  /api/shop/chat/{thread}?after=n   {busy, events}
    GET  /api/shop/orders?lang=            placed orders, carts waiting for an approver, last week's order
    POST /api/shop/reset                   forget this visitor's threads
    GET  /shop                             the shop page (web/shop.html)
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import secrets
import time
import traceback
from datetime import datetime, timedelta

import httpx
from fastapi import APIRouter, Body, Request, Response

from agent import llm

from . import chat, pictures, policy
from .aimo import parse_ts
from .catalog import kind, pieces
from .config import FREEZE_NOW, PORT, TZ, WEB
from .service import GatewayError

MODEL, FALLBACK_MODEL = "gemini-4-hydrogen", "claude-opus-5-5"
AGENT, AGENT_NAME = "shop-assistant", "Valio Aimo's shop assistant"
META = chat.META
TURN_S = 90          # a turn never runs longer than this
MAX_STEPS = 10       # model calls in one turn
MCP_BASE = os.environ.get("SHOP_MCP_BASE", f"http://127.0.0.1:{PORT}")
CLIENT = {"name": "valio-aimo-shop", "version": "1.0"}
# Sent on every call, as a chat host does: this client draws MCP Apps cards, so the gateway hands the model a short answer and the card the whole one.
SAID = {"io.modelcontextprotocol/protocolVersion": "2026-07-28", "io.modelcontextprotocol/clientInfo": CLIENT,
        "io.modelcontextprotocol/clientCapabilities": {"extensions": {"io.modelcontextprotocol/ui": {"mimeTypes": ["text/html;profile=mcp-app"]}}}}

CHIPS = {
    "fi": [{"label": "Häät lauantaina, 180 henkeä", "text": "Häät lauantaina, 180 henkeä: lohikeitto, broilerin rintafilee timjamikermakastikkeessa, paahdetut perunat ja porkkanat, mansikkakermakakku ja kahvi"},
           {"label": "Kuva lounaslistasta (näyte)", "text": "Tässä ensi viikon lounaslista", "image": "/static/samples/lounaslista.jpg"},
           {"label": "Tuote loppuu kesken (esimerkki)", "text": "60 litraa laktoositonta Arla Pro -vispikermaa maanantaiksi"},
           {"label": "1 000 l laktoositonta maitojuomaa", "text": "1 000 litraa laktoositonta maitojuomaa maanantaiksi"},
           {"label": "Sama kuin viime viikolla", "text": "Sama kuin viime viikolla"},
           {"label": "Mitä tilasin viime viikolla?", "text": "Mitä tilasin viime viikolla?"},
           {"label": "Valio Aimon ehdotukset", "text": "Onko Valio Aimolla ehdotuksia?"}],
    "en": [{"label": "Wedding on Saturday, 180 guests", "text": "Wedding on Saturday, 180 guests: salmon soup, chicken breast with thyme cream sauce, roasted potatoes and carrots, strawberry cream cake and coffee"},
           {"label": "Photo of a menu (sample)", "text": "Here is Saturday's menu, 180 guests", "image": "/static/samples/wedding.jpg"},
           {"label": "A product runs short (example)", "text": "60 litres of lactose-free Arla Pro whipping cream by Monday"},
           {"label": "1,000 L lactose-free milk", "text": "1,000 litres of lactose-free milk by Monday"},
           {"label": "Same as last week", "text": "Same as last week"},
           {"label": "What did I order last week?", "text": "What did I order last week?"},
           {"label": "Valio Aimo's proposals", "text": "Does Valio Aimo have any proposals for us?"}],
}
GREETING = {"fi": "Hei! Kerro mitä tarvitset tai mitä aiot tarjoilla, niin kokoan siitä ostoskorin sopimushinnoillasi. Mitään ei tilata ennen kuin vahvistat.",
            "en": "Hello! Tell me what you need or what you plan to serve, and I will build the cart at your contract prices. Nothing is ordered until you confirm."}
BUSY = {"fi": "Tilausapuri on juuri nyt ruuhkautunut. Kokeile hetken päästä uudelleen.", "en": "The assistant is busy right now. Try again in a minute."}
SLOW = {"fi": "Tämä kesti liian kauan, joten jätin sen kesken. Se mitä kortit näyttävät pitää paikkansa; kokeile uudelleen.",
        "en": "That took too long, so I stopped. What the cards show stands; try again."}
FAILED = {"fi": "Jokin meni pieleen, enkä saanut tätä valmiiksi. Kokeile uudelleen.", "en": "Something went wrong and I could not finish this. Try again."}

# What the code says itself, without a model: the answer to a button on a card.
W = {
    "fi": {"confirm": "Vahvista tilaus", "late_ok": "Tilaa silti toimituspäivälle", "swap": "Vaihda: {x}", "day": "Siirrä päivälle {x}", "drop": "Jätä pois: {x}", "again": "Tilaa uudelleen: {x}", "usual": "Tavallinen määrä: {x}", "cancel": "Peru ostoskori",
           "placed": "Tilaus {label} on vahvistettu. Toimituspäivä {day}",
           "waits": "Tämä ostoskori odottaa vielä hyväksyntää, joten sitä ei voi vahvistaa. Lähetä kortin linkki hyväksyjälle.",
           "changed": "Hinta tai saatavuus muuttui sen jälkeen kun ostoskori koottiin, joten mitään ei tilattu. Tarkista päivitetty ostoskori ja vahvista uudelleen.",
           "gone": "Tätä ostoskoria ei enää ole. Kerro uudelleen mitä tarvitset.", "not_placed": "Tilausta ei tehty. {why}",
           "swapped": "Vaihdettu. Ostoskori on päivitetty; mitään ei ole vielä tilattu.", "moved": "Ostoskorin toimituspäivä on nyt {day} Mitään ei ole vielä tilattu.",
           "dropped": "Poistettu ostoskorista.", "kind_set": "Lisätty ostoskoriin. Mitään ei ole vielä tilattu.", "kind": "{a}: {x}", "canceled": "Ostoskori on peruttu. Mitään ei tilattu.", "empty": "Ostoskoriin ei jäänyt yhtään riviä.",
           "again_none": "En löytänyt tälle tuotteelle tavallista määrää. Kerro kuinka paljon tarvitset.",
           "built": "Ostoskori on koottu. Mitään ei ole vielä tilattu: tilaus lähtee vasta, kun painat Vahvista tilaus.",
           "built_esc": "Ostoskori on koottu. Se tarvitsee hyväksynnän ennen kuin sen voi vahvistaa: lähetä kortin linkki hyväksyjälle.",
           "built_stop": "Ostoskoria ei voi tilata tällaisenaan. Kortti kertoo miksi."},
    "en": {"confirm": "Confirm the order", "late_ok": "Order anyway for", "swap": "Change to: {x}", "day": "Move to {x}", "drop": "Leave out: {x}", "again": "Order again: {x}", "usual": "The usual amount: {x}", "cancel": "Cancel the cart",
           "placed": "Order {label} is confirmed. Delivery {day}.",
           "waits": "This cart is still waiting for approval, so it cannot be confirmed. Send the link on the card to the approver.",
           "changed": "The price or the stock changed after the cart was built, so nothing was ordered. Check the updated cart and confirm again.",
           "gone": "This cart is no longer there. Tell me again what you need.", "not_placed": "The order was not placed. {why}",
           "swapped": "Changed. The cart is updated; nothing is ordered yet.", "moved": "The cart is now for {day}. Nothing is ordered yet.",
           "dropped": "Removed from the cart.", "kind_set": "Added to the cart. Nothing is ordered yet.", "kind": "{a}: {x}", "canceled": "The cart is cancelled. Nothing was ordered.", "empty": "No line was left in the cart.",
           "again_none": "I found no usual amount for this product. Tell me how much you need.",
           "built": "The cart is built. Nothing is ordered yet: the order goes out only when you press Confirm the order.",
           "built_esc": "The cart is built. It needs an approval before it can be confirmed: send the link on the card to the approver.",
           "built_stop": "This cart cannot be ordered as it is. The card says why."},
}
PLACED_CLAIM = re.compile(r"tilaus on (nyt )?(tehty|vahvistettu|lähetetty|lähtenyt|tilattu)|tilasin (sen|ne|tuotteet)|olen tilannut|order (is|has been|was) (now )?(placed|confirmed|sent)|i (have )?(placed|ordered)", re.I)

FI_DAYS = ["ma", "ti", "ke", "to", "pe", "la", "su"]
NUMBER_WORDS = {"yksi": 1, "yhdellä": 1, "yhden": 1, "yhdelle": 1, "one": 1, "kaksi": 2, "kahdella": 2, "kahden": 2, "kahdelle": 2, "two": 2, "kolme": 3, "kolmella": 3, "kolmen": 3, "three": 3,
                "neljä": 4, "neljällä": 4, "four": 4, "viisi": 5, "viidellä": 5, "five": 5, "kuusi": 6, "kuudella": 6, "six": 6, "seitsemän": 7, "seven": 7, "kahdeksan": 8, "eight": 8,
                "yhdeksän": 9, "nine": 9, "kymmenen": 10, "kymmenellä": 10, "ten": 10}
# The recipe cards and shelf ingredients go by English names in code; the Finnish page shows these.
DISH_FI = {"Creamy salmon soup": "Kermainen lohikeitto", "Green salad with vinaigrette": "Vihreä salaatti ja vinegretti", "Green salad": "Vihreä salaatti", "Chicken breast with thyme cream sauce": "Broilerin rintafilee ja timjamikermakastike",
           "Roasted potatoes and carrots": "Paahdetut perunat ja porkkanat", "Mushroom risotto": "Sienirisotto", "Strawberry cream cake": "Mansikkakermakakku", "Coffee and tea": "Kahvi ja tee",
           "Rye bread and butter": "Ruisleipä ja voi", "Macaroni casserole": "Makaronilaatikko", "Chicken sauce and rice": "Broilerikastike ja riisi",
           "Meatballs, mashed potatoes and brown sauce": "Lihapullat, perunamuusi ja ruskeakastike", "Pea soup": "Hernekeitto", "Oven pancake with strawberry jam": "Pannukakku ja mansikkahillo",
           "Fish fingers and boiled potatoes": "Kalapuikot ja keitetyt perunat", "Grated carrot": "Porkkanaraaste", "Crisp bread and spread": "Näkkileipä ja levite", "Milk to drink": "Ruokajuomaksi maito",
           "Scrambled eggs": "Munakokkeli", "Fried bacon": "Paistettu pekoni", "Breakfast sausages": "Aamiaismakkarat", "Oat porridge": "Kaurapuuro", "Boiled eggs": "Keitetyt kananmunat",
           "Croissants": "Croissantit", "Cheese, ham and vegetables": "Juusto, kinkku ja kasvikset", "Yoghurt, muesli and jam": "Jogurtti, mysli ja hillo", "Fresh fruit": "Tuoreet hedelmät", "Orange juice": "Appelsiinimehu"}
ITEM_FI = {"whipping cream": "vispikerma", "cooking cream": "ruokakerma", "plant whipping cream": "kasvipohjainen vispikerma", "oat cooking cream": "kauraruokakerma", "milk": "maito", "coffee milk": "kahvimaito",
           "oat drink": "kaurajuoma", "butter": "voi", "margarine": "margariini", "rapeseed oil": "rypsiöljy", "olive oil": "oliiviöljy", "parmesan": "parmesaani", "grated cheese": "juustoraaste",
           "cheese slices": "juustoviipaleet", "yoghurt": "jogurtti", "egg": "kananmuna", "eggs in shell": "kananmunat", "potato": "peruna", "carrot": "porkkana", "onion": "sipuli", "leek": "purjo", "dill": "tilli",
           "lettuce": "salaatti", "cucumber": "kurkku", "tomato": "tomaatti", "mushroom": "herkkusieni", "garlic": "valkosipuli", "lemon": "sitruuna", "strawberry": "mansikka", "banana": "banaani", "apple": "omena",
           "white cabbage": "valkokaali", "beetroot": "punajuuri", "parsley": "persilja", "chives": "ruohosipuli", "shallot": "salottisipuli", "salmon fillet": "lohifilee", "saithe fillet": "seitifilee",
           "fish fingers": "kalapuikot", "chicken breast": "broilerin rintafilee", "minced beef": "naudan jauheliha", "meatballs": "lihapullat", "bacon": "pekoni", "ham": "kinkku", "frankfurters": "nakit",
           "macaroni": "makaroni", "gluten-free pasta": "gluteeniton pasta", "rice": "riisi", "risotto rice": "risottoriisi", "wheat flour": "vehnäjauho", "potato flour": "perunajauho",
           "pancake flour mix": "pannukakkujauhoseos", "oat flakes": "kaurahiutaleet", "gluten-free oat flakes": "gluteenittomat kaurahiutaleet", "muesli": "mysli", "gluten-free muesli": "gluteeniton mysli",
           "sugar": "sokeri", "sugar cubes": "palasokeri", "vanilla sugar": "vaniljasokeri", "baking powder": "leivinjauhe", "salt": "suola", "black pepper": "mustapippuri", "white pepper": "valkopippuri",
           "allspice": "maustepippuri", "bay leaf": "laakerinlehti", "thyme": "timjami", "curry powder": "curry", "fish bouillon": "kalaliemi", "chicken bouillon": "kanaliemi", "vegetable bouillon": "kasvisliemi",
           "brown sauce base": "ruskeakastikepohja", "white wine vinegar": "valkoviinietikka", "mustard": "sinappi", "ketchup": "ketsuppi", "honey": "hunaja", "strawberry jam": "mansikkahillo",
           "lingonberry jam": "puolukkahillo", "pea soup concentrate": "hernekeittotiiviste", "rye bread": "ruisleipä", "gluten-free bread": "gluteeniton leipä", "crisp bread": "näkkileipä",
           "gluten-free crisp bread": "gluteeniton näkkileipä", "croissant": "croissant", "coffee": "kahvi", "tea": "tee", "orange juice": "appelsiinimehu", "apple juice": "omenamehu"}

KIND_FI = {"cream": "kerma", "cooking cream": "ruokakerma", "whipping cream": "vispikerma", "plant based whipping cream": "kasvipohjainen vispikerma", "plant whipping cream": "kasvipohjainen vispikerma",
           "creme fraiche": "ranskankerma", "sour cream": "kermaviili", "smetana": "smetana", "milk": "maito", "coffee milk": "kahvimaito", "sour milk": "piimä", "oat drink": "kaurajuoma", "barista oat drink": "barista-kaurajuoma",
           "butter": "voi", "margarine": "margariini", "cheese": "juusto", "cheese slice": "juustoviipaleet", "grated cheese": "juustoraaste", "cream cheese": "tuorejuusto", "blue cheese": "sinihomejuusto",
           "salad cheese": "salaattijuusto", "feta": "feta", "processed cheese": "sulatejuusto", "yoghurt": "jogurtti", "quark": "rahka", "ice cream": "jäätelö", "egg": "kananmuna", "potato": "peruna", "onion": "sipuli",
           "carrot": "porkkana", "tomato": "tomaatti", "cucumber": "kurkku", "minced beef": "naudan jauheliha", "minced meat": "jauheliha", "minced pork": "sian jauheliha", "chicken fillet": "broilerinfilee",
           "salmon fillet": "lohifilee", "bread": "leipä", "rye bread": "ruisleipä", "bread roll": "sämpylä", "wheat flour": "vehnäjauho", "sugar": "sokeri", "coffee": "kahvi", "juice": "mehu", "rice": "riisi", "pasta": "pasta",
           "macaroni": "makaroni", "sausage": "makkara", "ham": "kinkku", "bacon": "pekoni", "olive oil": "oliiviöljy", "rapeseed oil": "rypsiöljy"}

SHOP = """
You are the order assistant (tilausapuri) in a chat inside Valio Aimo's web shop, talking to a signed-in customer: {name}, a professional kitchen.
You never place an order. You build an ostoskori (cart): the shop draws it as a card with the lines, the delivery day and the total, and the order goes out only when the person presses "{confirm}" on that card.

How you answer
- In {language}{fi_note}. One or two short sentences, 30 words at most in all: the person reads it on a phone above the card. The card shows the lines, prices, total, the replacement with its reason and the buttons: never list products, prices or rules yourself, and never describe the card.
- Their words: ostoskori (never "tarjous"), toimituspäivä, sopimushinta, korvaava tuote, hyväksyjä, vahvista tilaus. Never mention tools, mandates, agents, the gateway, rule codes, JSON or ids.
- Tool answers are written in English for you; your answer is always in {language}. Name a product only by the name a tool answer gives for it, never by a word of your own for it.
- Everything comes from Valio Aimo's own range at this customer's prices. Never suggest looking anywhere else. Say that something is not in the range only when a tool answer says so.
- Never say an order is placed, sent or confirmed unless a tool answer in this turn says so. After a cart, end with what the person does next in a few words: press "{confirm}"; or, when it needs approval, send the link on the card to the approver (someone else, on their own phone).
- Say plainly what could not be done as asked: a delivery day that moved (and that it is after the event, when it is), a product that is short and what replaces it (the card gives the reason), an item left out with the one question that settles it. Ask one question at most, and only when a tool answer needs it.

What you do
- Products and amounts, one or many ("10 kg jauhelihaa, 5 l kermaa ja 200 munaa", "1 000 l laktoositonta maitojuomaa maanantaiksi"): call build_cart ONCE with every item. Never one call per product.
  Each item: query = the kind of product in plain English ("minced beef", "lactose-free milk", "eggs"; a lactose-free milk drink is "lactose-free milk"), as_written = the person's own words for it, quantity and unit (L, KG or pcs),
  preferred_product only when they named a brand or a product ("Arla Pro"), claims for hard requirements (lactose_free, gluten_free, vegan). Leave quantity out when they gave none, and never guess one:
  that item is then left off the cart, the tool answer gives the kitchen's usual amount for it when there is one, and you ask how much, naming that usual amount (the card has a button for it).
  delivery_date only when the person gave a day (from the dates below); otherwise leave it out.
- "Sama kuin viime viikolla", the usual order: build_cart with same_as_last_week true.
- A change to a cart ("lisää 5 kg perunaa", "maitoa 80 litraa", "kauramaitoa puolet vähemmän", "poista sipulit", "toimitus tiistaille"): build_cart with add_to = that cart's id and only what changes.
  The cart's answer lists its lines with numbers. A product that is already on the cart (another amount, half, double, another variety of it): pass that line's number as line, with the new quantity worked out from the amount in the list.
  An item without line is ADDED as a new line, so use it only for a product the cart does not have yet. When the words fit several lines, change each of them if that is plainly meant; otherwise ask which.
  remove = the person's words (or the line's number) for what goes, delivery_date for a new day.
- "This is what I will serve" (a menu, an event with its dishes, a recipe, a photo): call plan_menu_order with the dishes as the person named them, the guests, the date of the event and every dietary note.
  Guests missing: ask once. No dishes named ("häät lauantaina, 180 henkeä"): do not make up a menu. Ask what they will serve and suggest three or four fitting dishes from the kitchen's recipe cards below, by their Finnish names, that they can accept with one word.
  Dietary notes as written, with the head count as a digit ("1 coeliac", "6 vegan"). A note with no count applies to every portion: when the person plainly means only some guests and gave no number ("yksi pöytä laktoositon"), ask how many first.
  A recipe with the person's own amounts: pass that dish as {{"name", "recipe": [{{"item": the ingredient in plain English, "fi": as written, "role", "amount", "unit"}}], "recipe_portions": how many portions those amounts make}} with every amount exactly as written (never divide, round or change one), and guests = how many they want to serve; a count keeps unit "pcs" (10 eggs = amount 10, unit "pcs"); an ingredient with no amount is still passed, without one.
  A photo: call plan_menu_order with image "attached" and the guests if said.
  When the plan is ready the shop builds its cart at once and the answer comes back with it: say what it lists under tell_the_person, in your own short words.
- A change to an order that is already a cart from a menu or an event ("vaihda broileri kasvisvaihtoehtoon", "20 vierasta lisää", "sittenkin perjantaiksi"): call plan_menu_order again with the whole menu as it now stands and replaces = the earlier cart's id.
  The earlier cart is withdrawn and its card says so, also when it waits for an approver (then say the approver needs the new link). Say that the new cart replaces the earlier one; never say a cart was "updated" when a new one was built.
- A new, different request while an earlier cart is still open is its own cart, without replaces: both stay and the person confirms each.
- "Vahvista", "tilaa se" typed instead of pressing the button, about a cart the person has already seen: call confirm_order with that cart's id.
- "Mitä tilasin viime viikolla", "missä tilaukseni on", "mitä olen tilannut", "mikä odottaa hyväksyntää": call list_orders (about = last_week, placed or waiting) and answer from it in one or two sentences; the list is drawn as a card.
- A question about how this works ("miten tämä menee tilaukseen", "miten tämä eroaa siitä, että lisään tuotteet itse tuote kerrallaan", "mitä jos tuotetta ei ole saatavilla", "mitä osaat"): first call show_examples with the one or two
  examples that show it (differs from adding one by one: menu and photo; a product not available: shortage; how it becomes an order, who approves: list and approval). Then answer in two or three short sentences, 50 words at most, from these facts only:
  · One message about what the kitchen will serve or needs becomes one ostoskori. The shop finds each product in Valio Aimo's range at this customer's sopimushinta, works out the amounts for the guests in whole pack sizes, and checks that each line can be delivered on the toimituspäivä. The person checks the cart and confirms; they do not search for and add each product themselves.
  · A product that is short for the day: the cart shows a korvaava tuote from Valio Aimo's own range with the reason and the price difference, the "Vaihda" button shows the other choices, and when nothing fits the cart says so and leaves that item for the person to decide.
  · Nothing is ordered before the person presses "{confirm}" on the cart. After that the order is confirmed for its toimituspäivä and is listed under Tilaukset. A cart over what the person may order alone goes to an approver first, with the link on the card.
    This is a prototype: a confirmed order stays here and does not reach Valio Aimo's real order system.
- A product question ("onko teillä gluteenitonta pastaa", a price, an allergen): search_catalog or lookup_catalog, then one sentence.
- A problem with a delivery (toimituspoikkeama): list_order_issues. Proposals from Valio Aimo: list_proposals; accept or decline one only when the person says so. When a proposal carries open_cart, the person already has a cart with the same products for that day: say that first, and that accepting too would order them twice.
- Money in tool answers is in euro cents; write euros ({money}). Dates: write them as in "{day_example}", from the dates below; never work out a weekday yourself.

Kitchen's recipe cards (English name as the tool knows it = Finnish name): {cards}.
Ingredient names the kitchen's shelf goes by (use one whenever it fits an ingredient of a recipe): {pantry}.
Now: {now} Helsinki time. Coming days: {days}."""

# What the assistant can put under an answer for the person to try: showing, not describing.
EXAMPLES = {
    "fi": {"menu": CHIPS["fi"][0], "photo": CHIPS["fi"][1], "shortage": CHIPS["fi"][2], "approval": CHIPS["fi"][3], "last_week": CHIPS["fi"][4],
           "list": {"label": "10 kg jauhelihaa, 5 l kermaa ja 200 munaa", "text": "10 kg jauhelihaa, 5 l kermaa ja 200 munaa torstaiksi"}},
    "en": {"menu": CHIPS["en"][0], "photo": CHIPS["en"][1], "shortage": CHIPS["en"][2], "approval": CHIPS["en"][3], "last_week": CHIPS["en"][4],
           "list": {"label": "10 kg minced beef, 5 l cream and 200 eggs", "text": "10 kg minced beef, 5 l cream and 200 eggs for Thursday"}},
}
REPLACES = ("The id of the earlier cart of this conversation that this request changes: the same order with another dish, other guests, another day or other amounts. That cart is withdrawn, also when it waits for an approver, "
            "and the new cart takes its place. Leave out for a new, different request: that is its own cart, and the earlier one stays.")
LOCAL_TOOLS = [
    {"name": "build_cart", "description": "Build ONE cart (ostoskori) from everything the person asked for, or change a cart. Code finds each product in Valio Aimo's range at this customer's price, rounds to pack sizes, "
                                          "checks stock for the delivery day, and when a product is short puts a replacement from Valio Aimo's own range on the cart with the other choices. Nothing is ordered: the person confirms on the card.",
     "schema": {"type": "object", "properties": {
         "items": {"type": "array", "description": "Every product the person asked for, in one call.", "items": {"type": "object", "properties": {
             "query": {"type": "string", "description": "The kind of product in plain English: 'minced beef', 'lactose-free milk', 'eggs', 'whipping cream'."},
             "as_written": {"type": "string", "description": "The person's own words for this item."},
             "quantity": {"type": "number", "description": "How much. Leave out when the person gave no amount."},
             "line": {"type": "integer", "description": "With add_to: the number of the cart line this item changes (another amount or another variety of a product that is already on the cart). Repeat that line's query; give the new quantity and unit."},
             "unit": {"type": "string", "description": "L, KG or pcs."},
             "preferred_product": {"type": "string", "description": "A brand or product name, only when the person named one."},
             "claims": {"type": "array", "items": {"type": "string"}, "description": "Hard requirements: lactose_free, gluten_free, vegan, organic."}}, "required": ["query"]}},
         "delivery_date": {"type": "string", "description": "YYYY-MM-DD. Only when the person gave a day."},
         "same_as_last_week": {"type": "boolean", "description": "True for 'sama kuin viime viikolla' / the usual order. No items needed."},
         "add_to": {"type": "string", "description": "The id of a cart to change instead of building a new one."},
         "replaces": {"type": "string", "description": REPLACES},
         "remove": {"type": "array", "items": {"type": "string"}, "description": "With add_to: the person's words for what leaves the cart, or the line's number."}}}},
    {"name": "show_examples", "description": "Draw one or two tappable examples under your answer, so the person can try what you describe instead of reading about it. For questions about how this works, how it differs from adding products one by one, "
                                              "how a cart becomes an order, or what happens when a product is not available.",
     "schema": {"type": "object", "properties": {"examples": {"type": "array", "items": {"type": "string", "enum": ["menu", "photo", "list", "shortage", "approval", "last_week"]},
                "description": "menu = an event with its dishes becomes one cart; photo = a photo of a menu becomes one cart; list = products and amounts in one message; shortage = a product that is short gets a replacement; "
                               "approval = a cart over the limit goes to an approver; last_week = the usual order again."}}, "required": ["examples"]}},
    {"name": "list_orders", "description": "This customer's orders: the confirmed ones with delivery day and total, the carts that wait for an approver, and last week's order. Drawn for the person as a list.",
     "schema": {"type": "object", "properties": {"about": {"type": "string", "enum": ["last_week", "placed", "waiting"], "description": "What the person asked about; that part is drawn first."}}}},
    {"name": "confirm_order", "description": "Confirm a cart the person has already seen and now says to order ('vahvista', 'tilaa se'). Refused for a cart built in this same turn, and for one that waits for an approver.",
     "schema": {"type": "object", "properties": {"id": {"type": "string", "description": "The cart's id."}}, "required": ["id"]}},
]
# The model in the shop has no tool that makes an offer or places an order on its own: build_cart and the person's button do that.
HIDDEN = {"plan_sourcing", "create_checkout", "update_checkout", "complete_checkout", "usual_orders"}
# Tools that answer a question and build nothing.
ASKING = {"show_examples", "list_orders", "search_catalog", "lookup_catalog", "list_proposals", "list_order_issues", "get_order"}
# A typed confirmation has to say so in so many words; "ok" or "ne käy" is not an order.
SAYS_ORDER = re.compile(r"\b(vahvist\w*|tilaa\w*|tilataan|lähetä\w*|confirm\w*|place (the|my|this) order|order it|send it)\b", re.I)


def _cards() -> tuple[str, str]:
    try:
        d = json.loads((WEB.parent / "gateway" / "recipes.json").read_text())
        return "; ".join(f"{c['name']} = {DISH_FI.get(c['name'], c['name'])}" for c in d["cards"]), ", ".join(d["pantry"])
    except Exception:
        return "(not available)", "(not available)"


CARDS, PANTRY = _cards()


def _now() -> datetime:
    return parse_ts(FREEZE_NOW) or datetime.now(TZ)


def day_text(day, lang: str = "fi") -> str:
    try:
        d = datetime.strptime(str(day)[:10], "%Y-%m-%d")
    except ValueError:
        return str(day or "")
    return f"{FI_DAYS[d.weekday()]} {d.day}.{d.month}." if lang == "fi" else f"{d:%a} {d.day} {d:%b}"


def money_text(cents, lang: str = "fi") -> str:
    if cents is None:
        return ""
    return f"{cents / 100:,.2f} €".replace(",", " ").replace(".", ",") if lang == "fi" else f"€{cents / 100:,.2f}"


def digits(note: str) -> str:
    """'yksi keliaakikko' -> '1 keliaakikko': the menu planner reads a head count only as a digit, and a note with no count covers every portion."""
    return re.sub(r"(?<![\wäöå])(" + "|".join(NUMBER_WORDS) + r")(?![\wäöå])", lambda m: str(NUMBER_WORDS[m.group(1).lower()]), str(note), flags=re.I)


def system_prompt(acct: dict, lang: str) -> str:
    now = _now()
    days = ", ".join(f"{(now + timedelta(days=i)):%A} {(now + timedelta(days=i)):%Y-%m-%d} ({day_text((now + timedelta(days=i)).strftime('%Y-%m-%d'), lang)})" for i in range(1, 9))
    fi = lang == "fi"
    return SHOP.format(
        name=acct.get("name") or "a kitchen", language="Finnish" if fi else "English", confirm=W[lang]["confirm"],
        fi_note=": plain kitchen Finnish as a Finnish wholesaler's own web shop writes it, never translated English. Address the person as sinä, never te" if fi else
                (": plain English only. The Finnish words in these instructions are for a Finnish answer; an English answer says cart (never \"ostoskori\"), delivery day, contract price, replacement, approver, "
                 f"the \"{W['en']['confirm']}\" button, the \"Change\" button and the Orders tab"),
        money="1 560,00 €" if fi else "€1,560.00", day_example="ma 16.11." if fi else "Mon 16 Nov", cards=CARDS, pantry=PANTRY, now=f"{now:%A %d %B %Y, %H:%M}", days=days)


class Stop(Exception):
    """A turn ends with one plain sentence for the person."""


def _amount(totals, k="subtotal"):
    return next((t.get("amount") for t in totals or [] if t.get("type") == k), None)


def full_of(res: dict) -> dict:
    """The whole answer of a tool call: the card's copy when the model got the short one."""
    return (((res or {}).get("_meta") or {}).get(META) or {}).get("full") or (res or {}).get("structuredContent") or {}


def is_checkout(d: dict) -> bool:
    return isinstance(d, dict) and isinstance(d.get("ap2"), dict) and bool(d.get("id")) and "line_items" in d


# ------------------------------------------------------------------------------------------------ the gateway's own door
class Door:
    """MCP over HTTP to this same server, as a chat host that draws cards. Stateless JSON-RPC, one POST per call."""

    _tools: list | None = None

    def __init__(self, mandate: str, trace: str):
        self.http = httpx.AsyncClient(base_url=MCP_BASE, timeout=60, headers={"authorization": f"Bearer {mandate}", "accept": "application/json, text/event-stream", "x-trace-id": trace})

    async def rpc(self, method: str, params: dict) -> dict:
        r = await self.http.post("/mcp", json={"jsonrpc": "2.0", "id": secrets.randbelow(10**9), "method": method, "params": {**params, "_meta": SAID}})
        if r.status_code != 200:
            raise RuntimeError(f"MCP {method}: HTTP {r.status_code}")
        if r.headers.get("content-type", "").startswith("text/event-stream"):
            body = next(json.loads(l[5:]) for l in r.text.splitlines()[::-1] if l.startswith("data:"))
        else:
            body = r.json()
        if body.get("error"):
            raise RuntimeError(f"MCP {method}: {body['error'].get('message')}")
        return body["result"]

    async def tools(self) -> list[dict]:
        """What the model may call: the door's tools that neither make an offer nor place an order, and the shop's own."""
        if Door._tools is None:
            out = []
            for t in (await self.rpc("tools/list", {}))["tools"]:
                if "model" not in (((t.get("_meta") or {}).get("ui") or {}).get("visibility") or ["model"]):
                    continue  # card_data: for the card only
                schema = json.loads(json.dumps(t.get("inputSchema") or {"type": "object"}))
                schema.get("properties", {}).pop("checkout_mandate", None)  # the harness signs; the model never holds a key
                if t["name"] == "plan_menu_order":
                    schema.setdefault("properties", {})["replaces"] = {"type": "string", "description": REPLACES}
                out.append({"name": t["name"], "description": t.get("description") or "", "schema": schema})
            Door._tools = out
        return [t for t in Door._tools if t["name"] not in HIDDEN] + LOCAL_TOOLS

    async def call(self, name: str, args: dict) -> dict:
        return await self.rpc("tools/call", {"name": name, "arguments": args})

    async def close(self):
        await self.http.aclose()


# ------------------------------------------------------------------------------------------------ a stand-in for the model
class ScriptChat(llm.Chat):
    """No model: fixed moves, so the plumbing (door, signing, events, thread memory) is tested without a model. Same Chat interface."""

    def __init__(self, thread: dict, system, tools, note=None):
        super().__init__("script", system, tools, note)
        self.provider, self.thread, self.step, self.text = "script", thread, 0, ""

    def _cart(self):
        for e in reversed(self.thread["events"]):
            if e["type"] == "tool" and is_checkout(full_of(e["result"])):
                return full_of(e["result"])["id"]
        return None

    async def user(self, text):
        self.text = text.rsplit("The person now writes:", 1)[-1].lower()
        return self._next()

    async def results(self, results):
        self.step += 1
        return self._next()

    def _next(self):
        t, d = self.text, (_now() + timedelta(days=(7 - _now().weekday()) % 7 or 7)).strftime("%Y-%m-%d")  # next Monday
        call = lambda name, args: llm.Reply("", [llm.Call("s" + str(self.step), name, args)], "tools", "script")
        if self.step == 0:
            if "sittenkin" in t and self._cart():  # the same order with another amount: the earlier cart is replaced
                return call("build_cart", {"items": [{"query": "lactose-free whipping cream", "as_written": "40 litraa laktoositonta vispikermaa", "quantity": 40, "unit": "L"}], "delivery_date": d, "replaces": self._cart()})
            if "eroaa" in t or "differ" in t:
                return call("show_examples", {"examples": ["menu", "photo", "shortage"]})
            if "tarvitsen" in t:  # no amount given: nothing goes on a cart, the usual amount is offered
                return call("build_cart", {"items": [{"query": "milk", "as_written": "maitoa"}]})
            if "puolet" in t and self._cart():  # another amount for a line that is on the cart: that line changes, no line is added
                return call("build_cart", {"add_to": self._cart(), "items": [{"query": "milk", "as_written": "maitoa puolet vähemmän", "line": 1, "quantity": 200, "unit": "L"}]})
            if "tänään" in t:  # an event the first delivery day comes after
                return call("plan_menu_order", {"dishes": ["Creamy salmon soup"], "guests": 80, "event_date": _now().strftime("%Y-%m-%d")})
            if "viime viikolla" in t and "sama" in t or "same as last week" in t:
                return call("build_cart", {"same_as_last_week": True})
            if "tilasin" in t or "did i order" in t:
                return call("list_orders", {"about": "last_week"})
            if ("vahvista" in t or "confirm" in t) and self._cart():
                return call("confirm_order", {"id": self._cart()})
            if "kerma" in t or "cream" in t:
                return call("build_cart", {"items": [{"query": "lactose-free whipping cream", "as_written": "60 litraa laktoositonta Arla Pro -vispikermaa", "quantity": 60, "unit": "L", "preferred_product": "Arla Pro"}], "delivery_date": d})
            if "maito" in t or "milk" in t:
                return call("build_cart", {"items": [{"query": "lactose-free milk", "as_written": "1 000 litraa laktoositonta maitojuomaa", "quantity": 1000, "unit": "L", "claims": ["lactose_free"]}], "delivery_date": d})
            return llm.Reply("Kerro mitä tarvitset.", model="script")
        return llm.Reply("Tilaus on tehty." if "maito" in t and "tarvitsen" not in t and "puolet" not in t else "Tässä.", model="script")  # the first is wrong on purpose: the harness must not let it stand


# ------------------------------------------------------------------------------------------------ routes
def build(g, ctx, page) -> APIRouter:
    r = APIRouter()
    live: dict[str, dict] = {}   # thread id -> thread, while this process runs; every change is also kept in the store
    tasks: set = set()

    def principal(slug: str, thread: dict | None = None):
        """The mandate the account signed for its shop assistant: the account's own limits, and that the signed-in person confirms every order
        themselves (so a first-time product or an unusual amount needs no second person; money limits and allergens still do).
        Bound to the agent key: the harness signs each completion with it."""
        tok = (thread or {}).get("mandate")
        p = g.identify(tok) if tok else None
        if not p or not p.get("ok") or not (p.get("mandate") or {}).get("person_confirms"):
            tok = shop_mandate(slug, 2 * 86400)
            p = g.identify(tok)
            if thread is not None:
                thread["mandate"] = tok
        return tok, p

    def shop_mandate(slug: str, ttl: int) -> str:
        a = g.account(slug)
        base = g.store.get("mandate_config:" + a["customer"]) or policy.default_mandate()
        return g.issue_mandate(slug, constraints={**base, "person_confirms": True}, agent=f"agent:{slug}/{AGENT}", name=AGENT_NAME, ttl=ttl)

    def save(th):
        g.store.put("shop_thread:" + th["id"], th)

    def thread_of(tid, c) -> dict:
        th = live.get(tid) or g.store.get("shop_thread:" + str(tid))
        if not th or th["slug"] != c["acct"]["slug"] or th["sid"] != c["sid"]:
            raise GatewayError(404, "not_found", "No such conversation.")
        if th["id"] not in live and th.get("busy"):
            th["busy"] = False  # its turn was cut by a restart of this server
        live[th["id"]] = th
        return th

    def emit(th, ev: dict):
        ev = {"n": len(th["events"]) + 1, **ev}
        th["events"].append(ev)
        save(th)
        return ev

    def fi_name(pid, title=None):
        return ((g.cat.get(str(pid)) or {}).get("names") or {}).get("fi") or title or (g.cat.get(str(pid)) or {}).get("title") or str(pid)

    def name_of(pid, title, lang):
        return fi_name(pid, title) if lang == "fi" else (title or (g.cat.get(str(pid)) or {}).get("title") or str(pid))

    # ------------------------------------------------------------------- the shop page's first look
    @r.get("/shop", include_in_schema=False)
    def shop_page():
        if not (WEB / "shop.html").exists():
            raise GatewayError(404, "not_found", "The shop page is not here yet.")
        return page("shop.html")

    def shelf(slug: str, p: dict, lang: str) -> list[dict]:
        """Up to 8 products this account usually orders, with its own prices. Kept for 15 minutes; pictures from the local cache only."""
        key = f"shop_shelf:{slug}"
        kept = g.store.get(key)
        if not kept or time.time() - kept["at"] > 900:
            hist = g._history(p)
            ids = sorted(hist, key=lambda i: -hist[i].get("orders", 0))
            try:
                ids += [x.get("id") or x.get("product") for x in (g.seller.standing(g.account(slug)).get("lines") or [])]
            except Exception:
                pass
            ids = [i for i in dict.fromkeys(str(i) for i in ids if i) if g.cat.get(i)][:8]
            day = g._day(None)
            live_q = g._live(ids, day, p) if ids else {}
            rows = []
            for i in ids:
                d = g.cat.get(i)
                try:
                    w = g._product(d, live_q[i], p, None, day).get("wholesale") or {}
                    pr = w.get("price") or {}
                except Exception:
                    pr = {}
                rows.append({"id": i, "title": d["title"], "title_fi": (d.get("names") or {}).get("fi") or d["title"], "gtin": d.get("gtin"), "unit": d.get("unit"),
                             "cents": pr.get("unit_price"), "per": (pr.get("comparison") or {}).get("per"), "per_cents": (pr.get("comparison") or {}).get("amount")})
            kept = {"at": time.time(), "rows": rows}
            g.store.put(key, kept)
        pics = pictures.pictures([x["gtin"] for x in kept["rows"] if x.get("gtin")], budget=0)  # never waits: a picture not kept yet comes on a later visit
        fi = lang == "fi"
        unit = {"ST": "kpl" if fi else "pc", "KG": "kg", "L": "l", "PS": "ps" if fi else "bag", "KI": "ltk" if fi else "case"}
        out = []
        for x in kept["rows"]:
            txt = None
            if x.get("cents") is not None:
                txt = f"{money_text(x['cents'], lang)} / {unit.get(x.get('unit'), (x.get('unit') or '').lower())}"
                if x.get("per") and x.get("per_cents") is not None and x["per_cents"] != x["cents"]:
                    txt += f" ({money_text(x['per_cents'], lang)}/{x['per'].lower()})"
            out.append({"id": x["id"], "title": (x.get("title_fi") or x["title"]) if fi else x["title"], "price_text": txt, "picture": pics.get(x.get("gtin"))})
        return out

    def open_proposals(slug: str, p: dict) -> int:
        from .seller import OPEN, PENDING
        a = g.account(slug)
        if a.get("kind") == "sandbox" and time.time() - (g.store.get("shop_seller_run:" + slug) or 0) > 6 * 3600:
            # A visitor's own sandbox is not on Valio Aimo's agent's timer (only the stage and floor accounts are): opening the shop is when it looks at this account.
            g.store.put("shop_seller_run:" + slug, time.time())
            g.seller.run(a, use_model=False, why="the customer opened the web shop")
        g.seller.sync_all(a)
        return sum(1 for x in g.seller._rows(p["customer"]) if x["status"] in (OPEN, PENDING) and g.seller._current(x, a))

    # ------------------------------------------------------------------- orders, carts that wait, last week
    def usual_rows(p: dict) -> list[dict]:
        """What this account orders and how much at a time, most ordered first (the invented history every demo account starts with)."""
        hist, rows = g._history(p), []
        for pid, h in sorted(hist.items(), key=lambda x: -x[1].get("orders", 0)):
            d = g.cat.get(pid)
            if d and h.get("orders", 0) >= 3:  # a product first ordered today is not part of "last week"
                rows.append({"id": pid, "title": d["title"], "title_fi": fi_name(pid, d["title"]), "kind": kind(d), "units": h.get("typical_units"), "unit": (d.get("unit") or "").upper(), "orders": h.get("orders")})
        return rows[:12]

    def waiting(cust: str) -> list[dict]:
        out, now = [], time.time()
        for c in g.store.checkouts(60, cust):
            w, i = c.get("wholesale") or {}, c.get("_internal") or {}
            ok = (w.get("approval") or {}).get("decision") == "approved" and c.get("status") == "ready_for_complete"  # the approver said yes; the person has not confirmed yet
            if not (ok or c.get("status") == "requires_escalation") or (parse_ts(c.get("expires_at")) or datetime.now(TZ)).timestamp() < now:
                continue
            out.append({"id": c["id"], "asked_at": i.get("created"), "delivery_date": w.get("delivery_date"), "lines": len(c.get("line_items") or []), "subtotal": _amount(c.get("totals")),
                        "total": _amount(c.get("totals"), "total"), "intent": w.get("intent"), "url": c.get("continue_url"), "expires_at": c.get("expires_at"),
                        **({"approved_by": (w.get("approval") or {}).get("by")} if ok else {}),
                        "rules": [x.get("id") for x in (w.get("policy") or {}).get("rules") or [] if x.get("outcome") != "pass"]})
        return out

    def orders_view(p: dict) -> dict:
        cust, placed = p["customer"], []
        for o in g.store.orders(30, cust):
            w = o.get("wholesale") or {}
            placed.append({"id": o["id"], "label": o.get("label"), "placed_at": w.get("placed_at"), "delivery_date": w.get("delivery_date"), "lines": len(o.get("line_items") or []),
                           "subtotal": _amount(o.get("totals")), "total": _amount(o.get("totals"), "total"), "url": o.get("permalink_url"), "intent": w.get("intent"),
                           "approved_by": ((w.get("evidence") or {}).get("approval") or {}).get("by"),
                           "items": [{"id": l["item"]["id"], "title": l["item"].get("title"), "title_fi": fi_name(l["item"]["id"], l["item"].get("title")), "units": (l.get("wholesale") or {}).get("units"),
                                      "unit": (l["item"].get("quantity_unit") or {}).get("display_text")} for l in (o.get("line_items") or [])[:60]]})
        us = usual_rows(p)
        last = None
        if us:  # last week's order: the usual products at their usual amounts, a week before the next delivery day. Invented history, and the page says so.
            d = datetime.strptime(g._day(None), "%Y-%m-%d") - timedelta(days=7)
            last = {"sample": True, "delivery_date": d.strftime("%Y-%m-%d"), "items": [{"id": u["id"], "title": u["title"], "title_fi": u["title_fi"], "units": u["units"], "unit": u["unit"]} for u in us]}
        return {"orders": placed, "waiting": waiting(cust), "last_week": last}

    @r.get("/api/shop/orders", include_in_schema=False)
    def shop_orders(request: Request, response: Response):
        c = ctx(request, response)
        _, p = principal(c["acct"]["slug"])
        return orders_view(p)

    @r.get("/api/shop/hello", include_in_schema=False)
    async def hello(request: Request, response: Response, lang: str = "fi"):
        lang = "en" if lang == "en" else "fi"
        c = ctx(request, response)
        a = c["acct"]
        _, p = principal(a["slug"])
        card_token = shop_mandate(a["slug"], 12 * 3600)
        try:
            products = await asyncio.wait_for(asyncio.to_thread(shelf, a["slug"], p, lang), 12)
        except Exception:
            products = []
        try:
            n = await asyncio.wait_for(asyncio.to_thread(open_proposals, a["slug"], p), 8)
        except Exception:
            n = 0
        return {"account": {"name": a["name"], "customer": a["customer"]}, "greeting": GREETING[lang], "chips": CHIPS[lang], "proposals": n, "waiting": len(waiting(p["customer"])),
                "orders": len(g.store.orders(30, p["customer"])), "products": products, "card_token": card_token}

    # ------------------------------------------------------------------- one cart from everything that was asked
    def norm(it: dict) -> dict:
        """One asked item, as the planner takes it."""
        q = str(it.get("query") or it.get("as_written") or "").strip()[:120]
        qty, unit = it.get("quantity"), str(it.get("unit") or "").strip().lower()
        try:
            qty = float(qty) if qty not in (None, "", 0) else None
        except (TypeError, ValueError):
            qty = None
        if qty is not None and qty <= 0:
            qty = None
        if unit in ("g", "gram", "grams", "grammaa"):
            qty, unit = (qty / 1000 if qty else qty), "kg"
        elif unit in ("dl",):
            qty, unit = (qty / 10 if qty else qty), "l"
        elif unit in ("ml",):
            qty, unit = (qty / 1000 if qty else qty), "l"
        unit = {"l": "L", "litra": "L", "litraa": "L", "ltr": "L", "litre": "L", "litres": "L", "liter": "L", "liters": "L", "kg": "KG", "kilo": "KG", "kiloa": "KG", "kilos": "KG",
                "kpl": "pcs", "pcs": "pcs", "pc": "pcs", "st": "pcs", "piece": "pcs", "pieces": "pcs", "kappaletta": "pcs", "each": "pcs", "": ""}.get(unit, unit)
        claims = [str(x) for x in it.get("claims") or [] if x][:6] if isinstance(it.get("claims"), list) else []
        out = {"query": q, "as_written": str(it.get("as_written") or q).strip()[:200], "quantity": qty, "unit": unit, "preferred_product": str(it.get("preferred_product") or "").strip()[:80] or None, "claims": claims}
        if it.get("use_usual"):
            out["use_usual"] = True  # "sama kuin viime viikolla", "tilaa uudelleen": the usual amount is what was asked for
        try:
            if int(it.get("line") or 0) > 0:
                out["line"] = int(it["line"])
        except (TypeError, ValueError):
            pass
        return out

    def refs(cart: dict) -> dict:
        """Every line of a cart has a number that stays the same through its changes, so "that line" means one thing to the model and to the code."""
        for it in cart.get("items") or []:
            if not it.get("ref"):
                cart["seq"] = int(cart.get("seq") or 0) + 1
                it["ref"] = cart["seq"]
        return cart

    def choice(o: dict) -> dict:
        """One of the planner's options, as a choice on the cart."""
        sub = o.get("substitute") or {}
        lines = []
        for l in o.get("line_items") or []:
            pid, f = str(l["item"]["id"]), (l.get("wholesale") or {}).get("substitutes_for")
            lines.append({"id": pid, "title": l["item"].get("title"), "title_fi": fi_name(pid, l["item"].get("title")), "quantity": l["quantity"]["total"] if isinstance(l.get("quantity"), dict) else l.get("quantity"),
                          "units": l.get("units"), "unit": l.get("unit"), "covers": l.get("covers"), "unit_price": l.get("unit_price"), "amount": l.get("line_total_ex_vat"), **({"for": str(f)} if f else {})})
        out = {"label": o.get("label"), "covers": o.get("covers"), "covers_pct": o.get("covers_pct"), "total": o.get("total_ex_vat"), "lines": lines, "tradeoff": o.get("tradeoff"),
               "decision": (o.get("authority") or {}).get("decision")}
        if sub:
            out["substitute"] = {"tier": sub.get("tier"), "id": sub.get("id"), "title": sub.get("title"), "title_fi": fi_name(sub.get("id"), sub.get("title")), "contract_price": sub.get("contract_price"),
                                 "price_delta_pct": sub.get("price_delta_pct"), "per": sub.get("per"), "why": sub.get("why"), "listed": sub.get("listed_by_valio_aimo")}
        return out

    def state_of(ch: dict) -> str:
        return "replaced" if ch.get("substitute") else "short" if (ch.get("covers_pct") or 100) < 99.5 else "ok"

    def plan_item(it: dict, day: str, p: dict, tr: str) -> dict:
        """Plan one asked item for one day. Never raises: an item that cannot be planned says why."""
        out = dict(it)
        for k in ("choices", "chosen", "kinds", "why", "available_on", "status", "usual_amount"):
            out.pop(k, None)
        qty, unit, pref = it.get("quantity"), it.get("unit"), it.get("preferred_product")
        if not qty or not unit:
            row = None
            if pref and g.cat.get(pref):
                h = g._history(p).get(str(pref))
                row = {"id": str(pref), "units": (h or {}).get("typical_units")} if h else None
            else:
                try:
                    us = g.usual(it["query"], p, tr).get("usual") or []
                except GatewayError:
                    us = []
                row = {"id": us[0]["id"], "amount": us[0]["usual_amount"]} if us else None
            if not row:
                return {**out, "status": "ask_amount"}
            if row.get("amount"):
                qty, unit, pref = row["amount"]["quantity"], row["amount"]["unit"], pref or row["id"]
            else:
                d = g.cat.get(row["id"])
                per, unit = next(((d["content"][k], u) for k, u in (("l", "L"), ("kg", "KG")) if (d.get("content") or {}).get(k)), (pieces(d) or 1, "pcs"))
                qty = round((row["units"] or 1) * per, 2)
            if not it.get("use_usual"):  # nobody asked for the usual amount: it is offered, not put on the cart
                d = g.cat.get(str(row["id"])) or {}
                return {**out, "status": "ask_amount", "usual_amount": {"quantity": qty, "unit": unit, "id": str(row["id"]), "title": d.get("title"), "title_fi": fi_name(row["id"], d.get("title"))}}
            out.update({"quantity": qty, "unit": unit, "preferred_product": pref, "usual": True})
        try:
            pl = g.plan({"query": it["query"], "need": {"quantity": qty, "unit": unit}, "delivery_date": day, "preferred_product": pref, "filters": {"claims": it["claims"]} if it.get("claims") else {}}, p, tr)
        except GatewayError as e:
            return {**out, "status": "not_found", "why": e.message}
        opts = [o for o in pl.get("options") or [] if (o.get("authority") or {}).get("decision") != "invalid"]
        opts.sort(key=lambda o: (o.get("authority") or {}).get("decision") == "not_allowed")  # stable: the planner's own order, what a rule refuses last
        if not opts:
            if pl.get("kinds_to_choose"):
                return {**out, "status": "which_kind", "kinds": [{"kind": k.get("kind"), "fi": KIND_FI.get(str(k.get("kind")).lower()), "example": (k.get("example") or {}).get("title"),
                                                              "example_fi": fi_name((k.get("example") or {}).get("id"), (k.get("example") or {}).get("title"))} for k in pl["kinds_to_choose"][:6]]}
            reasons = {x.get("reason") for x in pl.get("not_offered") or []}
            said = " ".join(m.get("content", "") for m in pl.get("messages") or [] if m.get("type") in ("warning", "error"))
            nothing = any("range is called" in str(m.get("content")) for m in pl.get("messages") or [])  # no product by that name at all: not a matter of the day
            if not nothing and (reasons & {"cutoff_passed", "later_date_only", "out_of_stock", "allocation_share_used", "delivery_delayed", "short_at_warehouse"} or "can be delivered on" in said):
                return {**out, "status": "not_on_day", "why": said, "reasons": sorted(x for x in reasons if x)}
            return {**out, "status": "not_found", "why": said}
        out["choices"], out["chosen"] = [choice(o) for o in opts[:4]], 0
        out["status"] = state_of(out["choices"][0])
        return out

    async def plan_all(items: list[dict], day: str, p: dict, tr: str) -> list[dict]:
        sem = asyncio.Semaphore(4)

        async def one(it):
            async with sem:
                return await asyncio.to_thread(plan_item, it, day, p, tr)
        return list(await asyncio.gather(*(one(i) for i in items)))

    def later_days(day: str, n: int = 3) -> list[str]:
        d, out = datetime.strptime(day, "%Y-%m-%d"), []
        while len(out) < n:
            d += timedelta(days=1)
            if d.weekday() != 6:
                out.append(d.strftime("%Y-%m-%d"))
        return out

    async def plan_cart(cart: dict, items: list[dict], p: dict, tr: str):
        """Plan the given items for the cart's day and put them on the cart. When nothing asked for can come on that day, the cart moves to the first day it can, and says so."""
        day = cart["day"]
        got = await plan_all(items, day, p, tr)
        failing = [i for i, x in enumerate(got) if x["status"] == "not_on_day"]
        others = [x for x in cart["items"] if x.get("choices") and not x.get("dropped")] + [x for x in got if x.get("choices")]
        if failing:
            for d2 in later_days(day, 3 if not others else 1):
                probe = await plan_all([items[i] for i in failing], d2, p, tr)
                if not others and all(x.get("choices") for x in probe):  # nothing can come on the asked day: the whole cart moves
                    for i, x in zip(failing, probe):
                        got[i] = x
                    cart["moved"], cart["day"] = {"from": cart.get("asked_day") or day, "to": d2}, d2
                    break
                if others:  # the rest of the cart can come: these stay off it, with the day they could come
                    for i, x in zip(failing, probe):
                        if x.get("choices"):
                            got[i]["available_on"] = d2
                    break
        def same(x, y):  # the same thing asked again (a new amount): same kind, and not two different named products
            if y.get("status") == "which_kind" and x.get("query", "").lower() in [str(k.get("kind")).lower() for k in y.get("kinds") or []]:
                return True  # the answer to "which kind?" takes the question's place
            return y.get("query", "").lower() == x.get("query", "").lower() and (not x.get("preferred_product") or not y.get("preferred_product") or x["preferred_product"] == y["preferred_product"])
        refs(cart)
        for x in got:  # an item takes the place of the line it names, or of the earlier one of the same kind; anything else is a new line
            ln = x.pop("line", None)
            hit = [y for y in cart["items"] if ln and y.get("ref") == ln] or [y for y in cart["items"] if same(x, y)]
            if hit:
                at = next(i for i, y in enumerate(cart["items"]) if y is hit[0])
                x["ref"] = hit[0]["ref"]
                cart["items"][at] = x
                cart["items"] = [y for y in cart["items"] if y is x or not any(y is h for h in hit)]
            else:
                cart["items"].append(x)
        refs(cart)

    def cart_lines(cart: dict) -> list[dict]:
        merged: dict = {}
        for it in cart["items"]:
            if it.get("dropped") or not it.get("choices"):
                continue
            for l in it["choices"][it.get("chosen") or 0]["lines"]:
                m = merged.setdefault((l["id"], l.get("for")), {"item": {"id": l["id"]}, "quantity": 0})
                m["quantity"] += l["quantity"]
                w = {**({"substitutes_for": l["for"]} if l.get("for") else {}), **({"requirements": {"claims": it["claims"]}} if it.get("claims") else {})}
                if w:
                    m["wholesale"] = {**(m.get("wholesale") or {}), **w}
        return list(merged.values()) + [l for l in cart.get("fixed") or [] if not l.get("dropped")]

    def cart_public(cart: dict) -> dict:
        """The cart's notes for the page: what was asked, what stands for it, the other choices."""
        keep = ("as_written", "query", "quantity", "unit", "usual", "usual_amount", "status", "kinds", "available_on", "why", "reasons", "chosen", "choices", "dropped", "ref")
        return {"id": cart.get("id"), "day": cart["day"], "asked_day": cart.get("asked_day"), "moved": cart.get("moved"), "title": cart.get("title"), "same_as_last_week": cart.get("same_as_last_week"),
                "items": [{k: it[k] for k in keep if k in it} for it in cart["items"]], "menu": cart.get("menu"), "replaced": bool(cart.get("replaced"))}

    def cart_of(cid: str):
        return g.store.get("shop_cart:" + str(cid))

    def title_of(text: str) -> str:
        t = re.sub(r"\s+", " ", str(text or "")).strip()
        return t if len(t) <= 90 else t[:87].rstrip() + "…"

    def cart_read(cart: dict | None, full: dict, lang: str, fresh: bool = True) -> str:
        """What the model reads about a cart: the outcome and what the person must hear, never the lines."""
        st, w = full.get("status"), full.get("wholesale") or {}
        stops = [f"{x.get('title')}: {x.get('detail')}" for x in (w.get("policy") or {}).get("rules") or [] if x.get("outcome") != "pass"]
        errs = [m.get("content", "") for m in full.get("messages") or [] if m.get("type") == "error" and m.get("code") not in ("approval_required", "policy_denied")]
        out = [f"Cart {full.get('id')}: {len(full.get('line_items') or [])} lines, {money_text(_amount(full.get('totals')), lang)} ex VAT, delivery {w.get('delivery_date')} ({day_text(w.get('delivery_date'), lang)})."]
        if st == "ready_for_complete":
            out.append(f"It is ready: the person confirms it with the button \"{W[lang]['confirm']}\" on the card. Nothing is ordered yet.")
        elif st == "requires_escalation":
            out.append("It needs a second person's approval before it can be confirmed, because: " + " | ".join(stops) + ". The card shows the link and QR code for the approver; the person who asked cannot approve it themselves. Nothing is ordered yet.")
        elif st == "completed":
            out.append(f"It is confirmed: order {(full.get('order') or {}).get('label')}.")
        elif st == "canceled":
            out.append("It is cancelled.")
        else:
            out.append("It cannot be ordered as it is: " + " | ".join(stops + errs) + ".")
        tell = []
        if cart and cart.get("moved"):
            m = cart["moved"]
            tell.append(f"Nothing asked for can be delivered on {m['from']} ({day_text(m['from'], lang)}), so the cart is for the first day it can: {m['to']} ({day_text(m['to'], lang)}). Say this first, plainly.")
        for it in (cart or {}).get("items") or []:
            if it.get("dropped"):
                continue
            a, s = it.get("as_written"), it.get("status")
            ch = (it.get("choices") or [None])[it.get("chosen") or 0] if it.get("choices") else None
            if it.get("usual") and ch:
                tell.append(f"'{a}': no amount was given, so the kitchen's usual amount is on the cart ({it.get('quantity'):g} {it.get('unit')}).")
            if s == "replaced" and ch:
                sub, first = ch["substitute"], ch["lines"][0]
                nm = lambda x: (x.get("title_fi") if lang == "fi" else None) or x.get("title")
                tell.append(f"'{a}': only {first.get('covers'):g} of {it.get('quantity'):g} {it.get('unit')} of {nm(first)} can be delivered; the rest is on the cart as the replacement {nm(sub)} "
                            f"({sub.get('tier')}, {'at the contract price' if sub.get('contract_price') else 'at the list price'}, {sub.get('price_delta_pct') or 0:+.0f}% per {sub.get('per')}). Why it fits: {sub.get('why')} "
                            f"The card has a \"Vaihda\" button with {len(it['choices']) - 1} other choice(s).")
            elif s == "short" and ch:
                tell.append(f"'{a}': only {ch.get('covers'):g} of {it.get('quantity'):g} {it.get('unit')} can be delivered that day and nothing in the range replaces the rest; the cart has what can come.")
            elif s == "which_kind":
                tell.append(f"'{a}' is NOT on the cart: the word does not say which kind. Ask which: " + ", ".join(str(k.get("kind")) for k in it.get("kinds") or []) + ".")
            elif s == "ask_amount" and it.get("usual_amount"):
                ua = it["usual_amount"]
                tell.append(f"'{a}' is NOT on the cart: no amount was given. Ask how much, and say that the kitchen's usual amount is {ua['quantity']:g} {ua['unit']} of {(ua.get('title_fi') if lang == 'fi' else None) or ua.get('title')}; "
                            "the card has a button that puts that usual amount on the cart.")
            elif s == "ask_amount":
                tell.append(f"'{a}' is NOT on the cart: no amount was given and the kitchen has no usual amount for it. Ask how much.")
            elif s == "not_on_day":
                tell.append(f"'{a}' is NOT on the cart: it cannot be delivered on {cart['day']}" + (f"; it can on {it['available_on']} ({day_text(it['available_on'], lang)}), and the card offers to move the whole cart there." if it.get("available_on") else "."))
            elif s == "not_found":
                tell.append(f"'{a}' is NOT on the cart: nothing like it was found in Valio Aimo's range ({it.get('why') or 'no match'}).")
        menu = (cart or {}).get("menu")
        if menu:
            tell += menu.get("tell") or []
        if tell:
            out.append("tell_the_person:\n- " + "\n- ".join(tell))
        out.append("The person sees the cart as a card: every line, the delivery day, the total and the button. Do not list it." + (" Never say it is ordered." if st != "completed" else ""))
        nm = lambda x: (x.get("title_fi") if lang == "fi" else None) or x.get("title")
        index = [f"{it['ref']}. {it.get('query')}: {it.get('quantity') or 0:g} {it.get('unit')} ({' + '.join(str(nm(l)) for l in it['choices'][it.get('chosen') or 0]['lines'])})"
                 for it in refs(cart or {}).get("items") or [] if it.get("choices") and not it.get("dropped")]
        if index and st in ("ready_for_complete", "requires_escalation", "incomplete"):
            out.append("Its lines, for changing one later (line = the number; never read this list out):\n" + "\n".join(index[:60]))
        return "\n".join(out)

    # ------------------------------------------------------------------- one turn
    def history_text(th, upto: int) -> str:
        """The conversation so far as plain text, so any model (and the fallback) picks it up where it was."""
        parts = []
        for t in th["turns"][:upto]:
            s = (f"The person pressed a button on a card: {t['text']}" if t.get("action") else f"The person wrote: {t['text']}") + (" (with a photo)" if t.get("image") else "")
            for st in t.get("steps") or []:
                s += f"\n{'The shop did' if t.get('action') else 'You called'} {st['tool']} {json.dumps(st['args'], ensure_ascii=False)[:600]} and read: {st['read'][:4500]}"
            s += f"\n{'The shop answered' if t.get('action') else 'You answered'}: {t.get('answer') or '(nothing)'}"
            parts.append(s)
        return "\n\n".join(parts)

    def ask_text(th, turn: int, partial: list | None = None) -> str:
        t = th["turns"][turn - 1]
        earlier = history_text(th, turn - 1)
        now = t["text"] + ("\n(The person attached a photo. For plan_menu_order pass image \"attached\".)" if t.get("image") else "")
        out = (f"This continues a conversation in the shop. So far:\n\n{earlier}\n\nThe person now writes: {now}" if earlier else f"The person now writes: {now}")
        if partial:
            out += "\n\nIn this turn you already did:\n" + "\n".join(f"Called {st['tool']} {json.dumps(st['args'], ensure_ascii=False)[:600]} and read: {st['read'][:4500]}" for st in partial)
            out += "\nCarry on from there."
        return out

    def words_since_cart(th, turn: int) -> str:
        """The person's own words that led to this cart, word for word: every message since the last cart, never a model's rewording."""
        said = []
        for t in reversed(th["turns"][:turn]):
            if t.get("cart") and said:
                break
            asked_only = t is not th["turns"][turn - 1] and not t.get("cart") and any(x.get("tool") in ASKING for x in t.get("steps") or [])  # a question answered on the way ("mitä tilasin viime viikolla?") is not part of the order
            if not t.get("action") and not asked_only:
                said.append(t["text"])
        return " / ".join(reversed(said))[:1000]

    class Turn:
        """One running turn: who asks, the door, and what has been shown so far."""

        def __init__(self, th, turn, lang):
            self.th, self.turn, self.lang, self.t0 = th, turn, lang, time.time()
            self.rec = th["turns"][turn - 1]
            self.rec["steps"] = []
            self.tr = f"shop-{th['id'][3:]}-{turn}"
            self.mandate, self.p = principal(th["slug"], th)
            self.door = Door(self.mandate, self.tr)
            self.first_card = None
            self.state = None   # the status of the last cart shown in this turn

        def show(self, name: str, args: dict, res: dict, ms: int, quiet: bool = False) -> dict:
            shown = {k: v for k, v in args.items() if k not in ("image", "checkout_mandate")}
            if args.get("image"):
                shown["image"] = "(the attached photo)"
            emit(self.th, {"turn": self.turn, "type": "tool", "name": name, "args": shown, "ms": ms, "since_ms": int((time.time() - self.t0) * 1000), **({"quiet": True} if quiet else {}),
                           "result": {"content": res.get("content") or [], "structuredContent": res.get("structuredContent"), "_meta": res.get("_meta") or {}, "isError": bool(res.get("isError"))}})
            if not res.get("isError") and not quiet and self.first_card is None:
                self.first_card = int((time.time() - self.t0) * 1000)
            return shown

        def step(self, tool: str, shown: dict, read: str):
            self.rec["steps"].append({"tool": tool, "args": shown, "read": read})

        def say(self, text: str):
            self.rec["answer"] = text
            emit(self.th, {"turn": self.turn, "type": "text", "text": text})

        async def call(self, name: str, args: dict) -> tuple[dict, int]:
            started = time.time()
            try:
                res = await self.door.call(name, args)
            except GatewayError as e:
                res = {"content": [{"type": "text", "text": e.message}], "isError": True}
            return res, int((time.time() - started) * 1000)

        async def close(self):
            await self.door.close()

    async def open_carts(T: Turn, also: str = "", event: str = "") -> list[str]:
        """This conversation's earlier carts that the new one takes the place of: the one the request names as changed, and a menu cart for the same event. Any other open cart is a different order and stays."""
        out = []
        for cid in dict.fromkeys(t.get("cart") for t in T.th["turns"] if t.get("cart")):
            cart = cart_of(cid)
            if not cart or not (cid == also or (event and (cart.get("menu") or {}).get("event_date") == event)):
                continue
            try:
                cur = await asyncio.to_thread(g.get_checkout, cid, T.p)
            except GatewayError:
                continue
            if cur.get("status") in ("ready_for_complete", "incomplete", "requires_escalation"):
                out.append(cid)
        return out

    def hold_state(cids: list[str], frm: str, to: str):
        for cid in cids:
            g.store.q("update holds set state=? where ref=? and state=?", (to, cid, frm))

    async def withdraw(T: Turn, cids: list[str]) -> list[dict]:
        """A changed order is one cart, not two. The earlier cart is cancelled before the new one is priced, so its stock is free for it and it does not count toward the day's limit; its card says it was replaced."""
        out = []
        for cid in cids:
            res, ms = await T.call("cancel_checkout", {"id": cid})
            if res.get("isError"):
                hold_state([cid], "paused", "held")
                continue
            hold_state([cid], "paused", "released")
            cart = {**cart_of(cid), "replaced": True}
            g.store.put("shop_cart:" + cid, cart)
            (res.setdefault("_meta", {})).setdefault(META, {})["cart"] = cart_public(cart)
            emit(T.th, {"turn": T.turn, "type": "tool", "name": "cancel_checkout", "args": {"id": cid}, "ms": ms, "replaced": True,
                        "result": {"content": res.get("content") or [], "structuredContent": res.get("structuredContent"), "_meta": res.get("_meta") or {}, "isError": False}})
            out.append(cart)
        return out

    def replaced_read(gone: list[dict]) -> str:
        return ("\nThe earlier cart of this conversation (" + ", ".join(str(c.get("id")) for c in gone) + ") was withdrawn and its card now says it was replaced: the person has this one cart. "
                "Say in a few words that this cart replaces the earlier one. Never say the earlier cart was updated.") if gone else ""

    async def put_cart(T: Turn, cart: dict, started: float) -> tuple[str, dict]:
        """Put the cart's lines into one checkout through the door (a new one, or the same one updated) and show it. -> (what the model reads, the checkout)."""
        lines = cart_lines(cart)
        shown_args = {"items": [x.get("as_written") for x in cart["items"] if not x.get("dropped")], "delivery_date": cart["day"]}
        if not lines:
            if cart.get("id"):
                res, _ = await T.call("cancel_checkout", {"id": cart["id"]})
                g.store.put("shop_cart:" + cart["id"], cart)
                (res.setdefault("_meta", {})).setdefault(META, {})["cart"] = cart_public(cart)
                T.show("cancel_checkout", {"id": cart["id"]}, res, int((time.time() - started) * 1000))
                T.state = "canceled"
            elif any(x.get("status") in ("which_kind", "not_on_day") or (x.get("status") == "ask_amount" and x.get("usual_amount")) for x in cart["items"]):
                T.show("build_cart", shown_args, {"content": [], "structuredContent": {"cart": cart_public(cart)}, "_meta": {}}, int((time.time() - started) * 1000))
            read = cart_read(cart, {"id": None, "line_items": [], "totals": [], "status": "incomplete", "wholesale": {"delivery_date": cart["day"]}}, T.lang).split("\n", 2)[-1]
            return "No cart was built: nothing asked for could be put on it yet.\n" + read, {}
        args = {"line_items": lines, "delivery_date": cart["day"], "intent": cart.get("intent") or "", "rationale": cart.get("rationale") or "Built in the shop chat from the customer's own request; each line is the planner's first choice for what was asked."}
        name = "update_checkout" if cart.get("id") else "create_checkout"
        if cart.get("id"):
            args["id"] = cart["id"]
        if cart.get("exclude_allergens"):
            args["exclude_allergens"] = cart["exclude_allergens"]
        res, _ = await T.call(name, args)
        full = full_of(res)
        if res.get("isError") or not is_checkout(full):
            said = " ".join(m.get("content", "") for m in (full.get("messages") or [])) or "".join(c.get("text", "") for c in res.get("content") or [])
            T.show(name, shown_args, res, int((time.time() - started) * 1000), quiet=True)
            return f"The cart could not be built: {said[:600]}", {}
        cart["id"] = full["id"]
        g.store.put("shop_cart:" + full["id"], cart)
        (res.setdefault("_meta", {})).setdefault(META, {})["cart"] = cart_public(cart)
        T.show(name, shown_args, res, int((time.time() - started) * 1000))
        T.rec["cart"], T.state = full["id"], full.get("status")
        return cart_read(cart, full, T.lang), full

    async def tool_build_cart(T: Turn, args: dict) -> str:
        started = time.time()
        asked_day = str(args.get("delivery_date") or "").strip() or None
        old = cart_of(args.get("add_to")) if args.get("add_to") else None
        if args.get("add_to") and not old:
            raise Stop("There is no cart with that id. Build a new one without add_to.")
        if old:
            cur = await asyncio.to_thread(g.get_checkout, old["id"], T.p)
            if cur.get("status") in ("completed", "canceled"):
                raise Stop(f"That cart is already {cur['status']}. Build a new one without add_to.")
        first = g._day(None)
        if old:
            cart = old
            if asked_day and g._day(asked_day) != cart["day"]:  # a new day: everything on the cart is planned again for it
                cart.update({"asked_day": g._day(asked_day), "day": max(g._day(asked_day), first), "moved": None})
                again = [norm(x) | {"usual": x.get("usual"), "ref": x.get("ref")} for x in cart["items"] if not x.get("dropped") and x.get("status") not in ("which_kind", "ask_amount")]
                cart["items"] = []
                await plan_cart(cart, again, T.p, T.tr)
        else:
            day = g._day(asked_day)
            cart = {"id": None, "day": max(day, first), "asked_day": day if asked_day else None, "moved": {"from": day, "to": first} if day < first else None, "items": [], "thread": T.th["id"]}
        cart["intent"] = ((cart["intent"] + " / " + T.rec["text"]) if old and cart.get("intent") else words_since_cart(T.th, T.turn))[:1000]  # the person's own words, never a model's
        cart["title"] = cart.get("title") or title_of(cart["intent"])
        items = [norm(x) for x in args.get("items") or [] if isinstance(x, dict)]
        items = [x for x in items if x["query"]][:40]
        by_ref = {y["ref"]: y for y in refs(cart)["items"] if not y.get("dropped")} if old else {}
        for x in items:
            y = by_ref.get(x.get("line"))
            if not y:
                x.pop("line", None)
                continue
            if x["query"].lower() == str(y.get("query") or "").lower():  # the same product, another amount
                x.update({"query": y["query"], "as_written": y.get("as_written") or x["as_written"], "preferred_product": x["preferred_product"] or y.get("preferred_product"), "claims": x["claims"] or y.get("claims") or []})
            if not x["quantity"]:  # another variety, the amount stays
                x["quantity"], x["unit"] = y.get("quantity"), y.get("unit")
            elif not x["unit"]:
                x["unit"] = y.get("unit")
        if args.get("same_as_last_week"):
            us = usual_rows(T.p)
            if not us and not items:
                return "This customer has no order history, so there is no last week's order to repeat. Ask what they need."
            cart["same_as_last_week"] = True
            items += [{"query": u["kind"], "as_written": name_of(u["id"], u["title"], T.lang), "quantity": None, "unit": "", "preferred_product": u["id"], "claims": [], "use_usual": True} for u in us]
        gone = []
        for word in [str(x).lower().strip() for x in args.get("remove") or [] if str(x).strip()]:
            for it in cart["items"]:
                names = " ".join([str(it.get("as_written")), str(it.get("query"))] + [l.get("title", "") + " " + l.get("title_fi", "") for c in it.get("choices") or [] for l in c["lines"]]).lower()
                if word.isdigit():
                    if not it.get("dropped") and it.get("ref") == int(word):
                        it["dropped"] = True
                        gone.append(it.get("as_written"))
                    continue
                if not it.get("dropped") and (word in names or any(w in names for w in word.split() if len(w) > 3)):
                    it["dropped"] = True
                    gone.append(it.get("as_written"))
            for l in cart.get("fixed") or []:
                if not l.get("dropped") and (word in str(l.get("_title", "")).lower() or any(w in str(l.get("_title", "")).lower() for w in word.split() if len(w) > 3)):
                    l["dropped"] = True
                    gone.append(l.get("_title"))
        if not items and not gone and not (old and asked_day):
            return "Nothing to build: pass the items the person asked for."
        cands = [] if old else await open_carts(T, str(args.get("replaces") or ""))
        hold_state(cands, "held", "paused")  # planned as if the earlier open cart were not there: this one is about to take its place
        try:
            if items:
                await plan_cart(cart, items, T.p, T.tr)
        finally:
            if cands and not cart_lines(cart):
                hold_state(cands, "paused", "held")  # nothing to put in its place: the earlier cart stands
                cands = []
        gone_carts = await withdraw(T, cands)
        if gone_carts and gone_carts[-1].get("intent"):
            cart["intent"] = (gone_carts[-1]["intent"] + " / " + cart["intent"])[:1000]  # the whole request, in the person's words: what they first asked and what they changed
        read, _ = await put_cart(T, cart, started)
        read += replaced_read(gone_carts)
        if args.get("remove"):
            read += "\nRemoved from the cart: " + (", ".join(str(x) for x in gone) if gone else "nothing matched those words; ask which line they mean.")
        return read

    async def tool_list_orders(T: Turn, about: str = "") -> str:
        started = time.time()
        v = await asyncio.to_thread(orders_view, T.p)
        v["about"] = about if about in ("last_week", "placed", "waiting") else ("waiting" if v["waiting"] and not v["orders"] else "placed")
        T.show("list_orders", {"about": v["about"]}, {"content": [], "structuredContent": v, "_meta": {}}, int((time.time() - started) * 1000))
        lang, out = T.lang, []
        out.append(f"{len(v['orders'])} confirmed order(s)" + (": " + "; ".join(f"{o['label']}, {o['lines']} lines, {money_text(o['subtotal'], lang)} ex VAT, delivery {day_text(o['delivery_date'], lang)}, confirmed {str(o.get('placed_at') or '')[:16]}" for o in v["orders"][:8]) if v["orders"] else "") + ".")
        out.append(f"{len(v['waiting'])} cart(s) waiting for an approver" + (": " + "; ".join(f"{money_text(w['subtotal'], lang)} ex VAT for {day_text(w['delivery_date'], lang)} ('{title_of(w.get('intent'))}')" for w in v["waiting"][:6]) if v["waiting"] else "") + ".")
        lw = v.get("last_week")
        if lw:
            out.append(f"Last week's order (delivery {day_text(lw['delivery_date'], lang)}; this prototype's order history is invented sample data, and the card says so): "
                       + ", ".join(f"{x['units']:g} × {name_of(x['id'], x['title'], lang)}" for x in lw["items"]) + ".")
        else:
            out.append("No order history for last week.")
        out.append("The person sees this list as a card. Answer what they asked in one or two sentences; do not repeat the whole list. A delivery day in the future means the order is confirmed and on its way for that day; this prototype has no live tracking of a delivery.")
        return "\n".join(out)

    async def confirm(T: Turn, cid: str, by_button: bool) -> tuple[str, str | None]:
        """The person confirms a cart: the harness signs the exact offer with the assistant's key and completes it. -> (what happened, the sentence for the person)."""
        L = W[T.lang]
        try:
            cur = await asyncio.to_thread(g.get_checkout, str(cid), T.p)
        except GatewayError:
            return "There is no such cart.", L["gone"]
        cart = cart_of(cid)
        if cur.get("status") == "incomplete" and any(m.get("code") == "offer_expired" for m in cur.get("messages") or []) and cart:
            # The held price and stock ran out while the person was reading. Ask again for the same lines; order only if nothing changed.
            before = _amount(cur.get("totals"), "total")
            read, cur = await put_cart(T, cart, time.time())
            if not cur or cur.get("status") != "ready_for_complete" or _amount(cur.get("totals"), "total") != before:
                return "The hold had expired and the fresh cart differs or cannot be ordered alone. " + read, L["changed"]
        if cur.get("status") == "requires_escalation":
            return "The cart still waits for an approver; nothing was ordered.", L["waits"]
        if cur.get("status") == "completed":
            return f"Already confirmed as order {(cur.get('order') or {}).get('label')}.", L["placed"].format(label=(cur.get("order") or {}).get("label"), day=day_text((cur.get("wholesale") or {}).get("delivery_date"), T.lang))
        if cur.get("status") != "ready_for_complete":
            why = " ".join(m.get("content", "") for m in cur.get("messages") or [] if m.get("type") == "error")[:300]
            return f"The cart is {cur.get('status')} and cannot be confirmed. {why}", L["not_placed"].format(why="") .strip() if T.lang == "fi" else L["not_placed"].format(why=why)
        g.store.log(T.tr, f"human:{T.p.get('buyer_name')} (signed in)", "confirmed", "The signed-in person " + ("pressed Vahvista tilaus on the cart" if by_button else "told the assistant to confirm the cart they had seen")
                    + " in the shop chat. The shop assistant places the order on that word, signing the exact offer with its own key.", {"checkout": cid, "by": "button" if by_button else "typed"}, cid=str(cid), acct=T.p.get("customer"))
        args = {"id": str(cid), "checkout_mandate": g.agent_proof(cur, T.mandate)}
        res, ms = await T.call("complete_checkout", args)
        full = full_of(res)
        if cart:
            (res.setdefault("_meta", {})).setdefault(META, {})["cart"] = cart_public(cart)
        T.show("complete_checkout", {"id": str(cid)}, res, ms)
        T.state = full.get("status")
        if full.get("status") == "completed":
            label, day = (full.get("order") or {}).get("label"), (full.get("wholesale") or {}).get("delivery_date")
            return f"Order {label} is confirmed, {money_text(_amount(full.get('totals')), T.lang)} ex VAT, delivery {day}.", L["placed"].format(label=label, day=day_text(day, T.lang))
        if any(m.get("code") == "offer_changed" for m in full.get("messages") or []):
            return "Price or stock changed between the cart and the confirmation; a fresh cart is shown and nothing was ordered.", L["changed"]
        why = " ".join(m.get("content", "") for m in full.get("messages") or [] if m.get("type") == "error")[:300]
        return f"Not ordered: {why}", L["not_placed"].format(why="" if T.lang == "fi" else why).strip()

    def menu_notes(T: Turn, args: dict, plan: dict) -> dict:
        """What the cart of a menu says on top of its lines: the dishes, what was replaced and why, what is left to the kitchen, and whether it comes in time."""
        rd, lang = plan.get("reading") or {}, T.lang
        ev, day = rd.get("date") or str(args.get("event_date") or "")[:10] or None, plan.get("delivery_date")
        fi_item = {}
        for d in rd.get("dishes") or []:
            for x in d.get("recipe") or []:
                if x.get("fi"):
                    fi_item[x.get("item")] = x["fi"]
        item_fi = lambda s: fi_item.get(s) or ITEM_FI.get(str(s), str(s))
        written = {d.get("name"): d.get("as_written") for d in rd.get("dishes") or []}
        dish_fi = lambda n: DISH_FI.get(n) or (written.get(n) if written.get(n) and written.get(n) != n else None) or n
        names = [d.get("name") for d in rd.get("dishes") or []]

        def dish_of(i):  # the planner names a dish-scoped note's dish by its place in the request
            n = names[i] if isinstance(i, int) and 0 <= i < len(names) else (i if isinstance(i, str) else None)
            return {"dish": n, "dish_fi": dish_fi(n)} if n else {"dish": None}

        m = {"guests": rd.get("guests"), "event_date": ev, "late": bool(ev and day and day > ev), "same_day": bool(ev and day and day == ev), "delivery_note": plan.get("delivery_note"),
             "dishes": [{"name": d.get("name"), "fi": dish_fi(d.get("name")), "portions": d.get("portions"), "held": d.get("held"), "own_recipe": bool(d.get("card")) or "kitchen" in str(d.get("recipe"))} for d in plan.get("dishes") or []],
             "replaced": [{"id": l["product"]["id"], "title": l["product"].get("title"), "title_fi": fi_name(l["product"]["id"], l["product"].get("title")), "for_title": (l.get("substitutes_for") or {}).get("title"),
                           "for_title_fi": fi_name((l.get("substitutes_for") or {}).get("id"), (l.get("substitutes_for") or {}).get("title")), "reason": l.get("reason"), "tradeoff": l.get("tradeoff"),
                           "from_range": l.get("from_range")} for l in plan.get("lines") or [] if l.get("substitutes_for")],
             "partial": [{"id": l["product"]["id"], "title_fi": fi_name(l["product"]["id"], l["product"].get("title")), "title": l["product"].get("title")} for l in plan.get("lines") or [] if l.get("partial")],
             "not_ordered": [{"item": x.get("item") or x.get("ingredient") or x.get("dish"), "fi": item_fi(x.get("item") or x.get("ingredient") or x.get("dish")), "dish": x.get("dish"), "dish_fi": dish_fi(x.get("dish")) if x.get("dish") else None,
                              "why": x.get("why") or x.get("reason") or x.get("text"), "kind": x.get("kind"), "portions": x.get("portions_refused"), "rest": (x.get("ask") or {}).get("rest"),
                              "whole": bool(x.get("dish_refused")), "diet": [str(k) for k in x.get("diet") or []], "amount": x.get("amount")} for x in plan.get("not_ordered") or []],
             "pantry": [{"item": x.get("item"), "fi": item_fi(x.get("item")), "amount": x.get("amount")} for x in plan.get("pantry") or []],
             "diet": [{"note": x.get("note"), "rule": x.get("rule"), "kind": x.get("kind"), "scope": x.get("scope"), "count": x.get("count"), **dish_of(x.get("dish"))} for x in plan.get("diet") or []],
             "flags": [f.get("text") for f in plan.get("flags") or []] + [w.get("text") if isinstance(w, dict) else str(w) for w in plan.get("warnings") or []],
             "invented": [d.get("name") for d in plan.get("dishes") or [] if "model" in str(d.get("recipe"))],
             "invented_fi": [dish_fi(d.get("name")) for d in plan.get("dishes") or [] if "model" in str(d.get("recipe"))],
             **({"photo_reused": True} if plan.get("photo_read") == "reused" else {})}
        # The cart card shows the first lines under their dish, so the amounts are on screen without a tap. A product used
        # in two dishes stands under the one that takes most of it. "per" is what one sales unit holds, in g or ml.
        by = {}
        for l in plan.get("lines") or []:
            fs, pk = l.get("for") or [], l.get("pack") or {}
            if not fs or not l.get("units") or not pk.get("ordered"):
                continue
            f, d = max(fs, key=lambda x: x.get("share") or 0), g.cat.get(l["product"]["id"])
            n = pieces(d) if d else None
            by.setdefault(f.get("dish"), []).append({"id": l["product"]["id"], "item": f.get("item"), "fi": item_fi(f.get("item")), "per": round(pk["ordered"] / l["units"], 3), "unit": pk.get("unit"),
                                                      **({"pieces": n} if n and n > 1 else {})})
        m["by_dish"] = [{"dish": d.get("name"), "lines": by[d.get("name")]} for d in plan.get("dishes") or [] if by.get(d.get("name"))]
        tell = []
        if m.get("photo_reused"):
            tell.append("This same photo was already read earlier today, and that reading is used again, so the same photo gives the same cart. The card says so. Do not say how fast it was.")
        if m["late"]:
            tell.append(f"IT DOES NOT COME IN TIME: the event is on {ev} ({day_text(ev, lang)}) and the first possible delivery day is {day} ({day_text(day, lang)}), after the event. Say this first, plainly. The card does not offer confirming or approving until the person presses its button \"{W[lang]['late_ok']} {day_text(day, lang)}\": never tell them to press \"{W[lang]['confirm']}\"; say they can still order it for that day from the card if they want it.")
        elif m["same_day"]:
            tell.append(f"Delivery is on the day of the event itself, {day} ({day_text(day, lang)}): there is no earlier delivery day. Say so.")
        for x in m["diet"]:
            tell.append(f"Dietary note \"{x['note']}\" was applied as: {x['rule']}")
        if m["replaced"]:
            tell.append(f"{len(m['replaced'])} line(s) are replacements from Valio Aimo's own range (the card says why for each): " + "; ".join(f"{x['title_fi'] if lang == 'fi' else x['title']} for {x['for_title_fi'] if lang == 'fi' else x['for_title']} ({x.get('reason') or x.get('tradeoff') or ''})" for x in m["replaced"][:4]))
        if m["not_ordered"]:
            part = [x for x in m["not_ordered"] if x.get("portions") and not x.get("whole")]
            other = [x for x in m["not_ordered"] if x not in part]
            for x in part[:4]:  # the dish itself is ordered; only the portions under the dietary note wait
                tell.append(f"{x['dish']}: the other {x.get('rest')} portions are ordered in full. Nothing is ordered for the {x['portions']} {' and '.join(x['diet'])} portion(s), because {x['item']} does not fit that diet; the person decides what those get. Do not say {x['item']} was left out of the order.")
            if other:
                tell.append("Not on the cart, a person decides: " + "; ".join(f"{x['item']} ({x.get('why') or ''})" for x in other[:6]))
        if m["invented"]:
            tell.append("These dishes are not on the kitchen's recipe cards, so the amounts come from a recipe the model proposed; say they should check them: " + ", ".join(m["invented"]))
        tell += [str(f) for f in m["flags"] if f][:4]
        m["tell"] = tell
        return m

    async def tool_menu(T: Turn, c: llm.Call, args: dict) -> dict:
        """plan_menu_order through the door; a ready plan goes into one cart at once, unchanged, with no line re-typed by a model."""
        if T.rec.get("image_data") and (args.get("image") or not args.get("dishes")):
            args["image"] = T.rec["image_data"]
        else:
            args.pop("image", None)
        if isinstance(args.get("notes"), list):
            args["notes"] = [digits(n) for n in args["notes"]]
        elif isinstance(args.get("notes"), str):
            args["notes"] = digits(args["notes"])
        started = time.time()
        cands = await open_carts(T, str(args.pop("replaces", None) or ""), str(args.get("event_date") or ""))
        hold_state(cands, "held", "paused")  # planned as if the earlier open cart were not there: this one is about to take its place
        ready = False
        try:
            res, ms = await T.call("plan_menu_order", args)
            plan = full_of(res)
            ready = not res.get("isError") and plan.get("status") == "ready" and plan.get("checkout")
        finally:
            if not ready:
                hold_state(cands, "paused", "held")  # no new cart: the earlier one stands
        shown = T.show("plan_menu_order", args, res, ms, quiet=True)  # the cart is what the person sees; the plan stays behind "Näytä tarkistukset"
        if not ready:
            text = "".join(x.get("text", "") for x in res.get("content") or [] if x.get("type") == "text")
            short = {k: plan.get(k) for k in ("status", "questions", "delivery_date", "delivery_note", "flags", "warnings") if plan.get(k)}
            read = ("No cart was built. " + (json.dumps(short, ensure_ascii=False) if short else text))[:6000] + "\nAsk the person what the questions ask, in one or two sentences. Nothing is drawn for this."
            T.step(c.name, shown, read)
            return {"id": c.id, "name": c.name, "content": read, **({"error": True} if res.get("isError") else {})}
        ck = plan["checkout"]
        gone_carts = await withdraw(T, cands)
        titles = {l["product"]["id"]: l["product"].get("title") for l in plan.get("lines") or []}
        fixed = [{**l, "_title": f"{titles.get(l['item']['id'], '')} {fi_name(l['item']['id'])}"} for l in ck["line_items"]]
        cart = {"id": None, "day": ck["delivery_date"], "asked_day": None, "moved": None, "items": [], "fixed": fixed, "thread": T.th["id"], "intent": (((gone_carts[-1]["intent"] + " / ") if gone_carts and gone_carts[-1].get("intent") else "") + words_since_cart(T.th, T.turn))[:1000],
                "title": title_of(words_since_cart(T.th, T.turn)), "rationale": ck.get("rationale") or "", "menu": menu_notes(T, args, plan)}
        lines = [{k: v for k, v in l.items() if k != "_title"} for l in fixed]
        cargs = {"line_items": lines, "delivery_date": ck["delivery_date"], "exclude_allergens": ck.get("exclude_allergens") or [], "intent": cart["intent"], "rationale": cart["rationale"]}
        res2, _ = await T.call("create_checkout", cargs)
        full = full_of(res2)
        if res2.get("isError") or not is_checkout(full):
            said = " ".join(m.get("content", "") for m in (full.get("messages") or [])) or "".join(x.get("text", "") for x in res2.get("content") or [])
            read = f"The menu was planned but its cart could not be built: {said[:600]}"
        else:
            cart["id"], cart["exclude_allergens"] = full["id"], ck.get("exclude_allergens") or []
            g.store.put("shop_cart:" + full["id"], cart)
            (res2.setdefault("_meta", {})).setdefault(META, {})["cart"] = cart_public(cart)
            T.show("create_checkout", {"menu": [d.get("name") for d in plan.get("dishes") or []], "guests": (plan.get("reading") or {}).get("guests"), "delivery_date": ck["delivery_date"]}, res2, int((time.time() - started) * 1000))
            T.rec["cart"], T.state = full["id"], full.get("status")
            read = f"The menu is planned: {len(plan.get('dishes') or [])} dishes for {(plan.get('reading') or {}).get('guests')} guests.\n" + cart_read(cart, full, T.lang) + replaced_read(gone_carts)
        T.step(c.name, shown, read)
        return {"id": c.id, "name": c.name, "content": read}

    async def turn_run(th, turn: int, lang: str, model_pick: str | None):
        T, used, chat_ = Turn(th, turn, lang), [], None
        rec = T.rec
        try:
            tools = await T.door.tools()
            system = system_prompt(g.account(th["slug"]) or {}, lang)

            def open_(model):
                if model == "script":
                    return ScriptChat(th, system, tools)
                ch = llm.open_chat(model, system, tools)
                ch.patient = False
                ch.models = [model]  # one model per chat; the switch to the fallback is made here, with the turn so far
                return ch

            async def use(c: llm.Call) -> dict:
                args, name = dict(c.args), c.name
                try:
                    if c.bad_args:
                        raise Stop(c.bad_args)
                    if name in HIDDEN:
                        raise Stop("That is not available in the shop chat. Use build_cart: it builds one cart, and the person confirms it on the card.")
                    if name == "plan_menu_order":
                        return await tool_menu(T, c, args)
                    if name == "build_cart":
                        read = await tool_build_cart(T, args)
                    elif name == "list_orders":
                        read = await tool_list_orders(T, str(args.get("about") or ""))
                    elif name == "show_examples":
                        picked = [EXAMPLES[lang][k] for k in dict.fromkeys(args.get("examples") or []) if k in EXAMPLES[lang]][:2]
                        if not picked:
                            raise Stop("Pass one or two of: menu, photo, list, shortage, approval, last_week.")
                        emit(th, {"turn": turn, "type": "examples", "chips": picked})
                        read = "Drawn under your answer as buttons the person can tap: " + "; ".join(c["label"] for c in picked) + ". Now answer the question in two or three short sentences. Do not list the examples; you may say they can try one below."
                    elif name == "confirm_order":
                        cid = str(args.get("id") or "")
                        if rec.get("cart") == cid:
                            raise Stop("That cart was built in this same turn: the person has not seen it yet. They confirm it with the button on the card.")
                        if not SAYS_ORDER.search(str(rec.get("text") or "")):
                            raise Stop(f"The person did not say to order in so many words, so nothing is ordered. They confirm with the button \"{W[T.lang]['confirm']}\" on the card, or by writing that they confirm the order.")
                        read, said = await confirm(T, cid, by_button=False)
                        rec["fixed_answer"] = said
                    else:
                        res, ms = await T.call(name, args)
                        full = full_of(res)
                        if is_checkout(full):  # a cart read or cancelled by its id: drawn as the cart it is
                            cart = cart_of(full["id"])
                            if cart:
                                (res.setdefault("_meta", {})).setdefault(META, {})["cart"] = cart_public(cart)
                            T.state = full.get("status")
                            if name in ("answer_proposal", "resolve_order_issue"):
                                rec["cart"] = full["id"]  # made in this turn: the person confirms it on the card, never the model in the same breath
                        shown = T.show(name, args, res, ms)
                        read = cart_read(cart_of(full["id"]), full, lang) if is_checkout(full) else "".join(x.get("text", "") for x in res.get("content") or [] if x.get("type") == "text")[:14000]
                        T.step(name, shown, read)
                        return {"id": c.id, "name": name, "content": read, **({"error": True} if res.get("isError") else {})}
                except Stop as e:
                    read = str(e)
                    T.step(name, {k: v for k, v in args.items() if k != "image"}, read)
                    return {"id": c.id, "name": name, "content": read, "error": True}
                except GatewayError as e:
                    read = e.message
                    T.step(name, {k: v for k, v in args.items() if k != "image"}, read)
                    return {"id": c.id, "name": name, "content": read, "error": True}
                T.step(name, {k: v for k, v in args.items() if k != "image"}, read)
                return {"id": c.id, "name": name, "content": read}

            model = model_pick or MODEL
            chat_ = open_(model)
            reply = await chat_.user(ask_text(th, turn))
            for step in range(MAX_STEPS + 1):
                if reply is None and model not in (FALLBACK_MODEL, "script"):
                    await chat_.close()
                    model = FALLBACK_MODEL  # the turn goes on with Claude, from where it was
                    chat_ = open_(model)
                    reply = await chat_.user(ask_text(th, turn, rec["steps"]))
                if reply is None:
                    raise Stop(BUSY[lang])
                used.append(reply.model or model)
                if reply.stop == "refusal":
                    raise Stop(FAILED[lang])
                if not reply.calls or step == MAX_STEPS:
                    text = (reply.text or "").strip()
                    if rec.get("fixed_answer"):  # a confirmation is answered by code: what happened, never what a model thinks happened
                        text = rec.pop("fixed_answer")
                    elif T.state != "completed" and PLACED_CLAIM.search(text):
                        # The model said the order is placed and it is not. The person reads the true sentence instead.
                        text = W[lang]["built_esc" if T.state == "requires_escalation" else "built_stop" if T.state in ("incomplete", "canceled") else "built"]
                        g.store.log(T.tr, f"agent:{th['slug']}/{AGENT}", "corrected", "The model's sentence said the order was placed when it was not. The shop replaced it with the cart's real state.", {"state": T.state}, acct=T.p.get("customer"))
                    rec["answer"] = text
                    if text:
                        emit(th, {"turn": turn, "type": "text", "text": text})
                    break
                results = [await use(c) for c in reply.calls]
                reply = await chat_.results(results)
            ms = int((time.time() - T.t0) * 1000)
            last = used[-1] if used else model
            emit(th, {"turn": turn, "type": "done", "ms": ms, "model": last, "label": llm.label_of(last), "first_card_ms": T.first_card, "models": list(dict.fromkeys(used))})
            g.store.log(T.tr, f"agent:{th['slug']}/{AGENT}", "shop_turn", f"The shop assistant answered a request in the web shop in {ms / 1000:.1f} s ({llm.label_of(last)})"
                        + (f", first card after {T.first_card / 1000:.1f} s." if T.first_card else ".") + " The model saw no mandate and no key, and has no tool that places an order.",
                        {"thread": th["id"], "turn": turn, "model": last, "ms": ms, "first_card_ms": T.first_card, "tools": [s["tool"] for s in rec["steps"]], "request": rec["text"][:1000]}, acct=T.p.get("customer"))
        except Stop as e:
            emit(th, {"turn": turn, "type": "error", "text": str(e)})
        finally:
            if chat_ is not None:
                try:
                    await chat_.close()
                except Exception:
                    pass
            await T.close()

    async def action_run(th, turn: int, lang: str, a: dict):
        """A button on a card: done in code, with no model. Confirm the cart, change a replacement, move the day, leave an item out, order a product again."""
        T, L, kind_ = Turn(th, turn, lang), W[lang], a.get("type")
        try:
            cid = str(a.get("checkout") or "")
            if kind_ == "confirm":
                read, said = await confirm(T, cid, by_button=True)
                T.step("confirm", {"cart": cid}, read)
                T.say(said)
            elif kind_ == "again":
                d = g.cat.get(str(a.get("product") or ""))
                if not d:
                    raise Stop(L["again_none"])
                started = time.time()
                said_before = words_since_cart(th, turn) if a.get("asked") else ""  # the button answers "how much?": the request it belongs to stays with the cart, word for word
                cart = {"id": None, "day": g._day(None), "asked_day": None, "moved": None, "items": [], "thread": th["id"], "intent": ((said_before + " / ") if said_before else "") + T.rec["text"], "title": T.rec["text"]}
                await plan_cart(cart, [{"query": kind(d), "as_written": name_of(d["id"], d["title"], lang), "quantity": None, "unit": "", "preferred_product": str(d["id"]), "claims": [], "use_usual": True}], T.p, T.tr)
                if a.get("asked"):
                    for x in cart["items"]:
                        x.pop("usual", None)  # the person picked the usual amount themselves: the cart need not explain it
                if not any(x.get("choices") for x in cart["items"]) and cart["items"] and cart["items"][0].get("status") == "ask_amount":
                    T.step("order_again", {"product": d["id"]}, "No usual amount for it.")
                    T.say(L["again_none"])
                else:
                    read, full = await put_cart(T, cart, started)
                    T.step("order_again", {"product": d["id"]}, read)
                    T.say(L["built_esc" if T.state == "requires_escalation" else "built_stop" if T.state != "ready_for_complete" else "built"])
            elif kind_ in ("swap", "drop", "day", "cancel", "kind", "usual"):
                cart = cart_of(cid)
                cur = await asyncio.to_thread(g.get_checkout, cid, T.p) if cart else None
                if not cart or cur.get("status") in ("completed", "canceled"):
                    raise Stop(L["gone"])
                started = time.time()
                if kind_ == "cancel":
                    res, ms = await T.call("cancel_checkout", {"id": cid})
                    (res.setdefault("_meta", {})).setdefault(META, {})["cart"] = cart_public(cart)
                    T.show("cancel_checkout", {"id": cid}, res, ms)
                    T.step("cancel", {"cart": cid}, "The cart is cancelled.")
                    T.say(L["canceled"])
                    return
                if kind_ == "day":
                    day = g._day(str(a.get("date") or ""))
                    cart.update({"asked_day": day, "day": max(day, g._day(None)), "moved": None})
                    again = [norm(x) | {"usual": x.get("usual"), "ref": x.get("ref")} for x in cart["items"] if not x.get("dropped") and x.get("status") not in ("which_kind", "ask_amount")]
                    cart["items"] = []
                    await plan_cart(cart, again, T.p, T.tr)
                    said = L["moved"].format(day=day_text(cart["day"], lang))
                else:
                    i = int(a.get("item", -1))
                    if not 0 <= i < len(cart["items"]):
                        raise Stop(L["gone"])
                    it = cart["items"][i]
                    if kind_ == "drop":
                        it["dropped"], said = True, L["dropped"]
                    elif kind_ == "kind":
                        want = next((k for k in it.get("kinds") or [] if k.get("kind") == a.get("kind")), None)
                        if not want:
                            raise Stop(L["gone"])
                        await plan_cart(cart, [{**norm(it), "query": want["kind"]}], T.p, T.tr)
                        said = L["kind_set"]
                    elif kind_ == "usual":
                        ua = it.get("usual_amount") or {}
                        if it.get("status") != "ask_amount" or not ua:
                            raise Stop(L["gone"])
                        await plan_cart(cart, [{**norm(it), "quantity": ua["quantity"], "unit": ua["unit"], "preferred_product": ua["id"], "line": it.get("ref")}], T.p, T.tr)
                        said = L["kind_set"]
                    else:
                        k = int(a.get("choice", -1))
                        if not it.get("choices") or not 0 <= k < len(it["choices"]):
                            raise Stop(L["gone"])
                        it["chosen"], it["status"], said = k, state_of(it["choices"][k]), L["swapped"]
                read, full = await put_cart(T, cart, started)
                T.step(kind_, {k: v for k, v in a.items() if k != "type"}, read)
                T.say(said if full or kind_ != "drop" else L["empty"])
            else:
                raise Stop(FAILED[lang])
            ms = int((time.time() - T.t0) * 1000)
            emit(th, {"turn": turn, "type": "done", "ms": ms, "model": "none", "label": "no model", "first_card_ms": T.first_card, "models": []})
        except Stop as e:
            T.rec["answer"] = str(e)
            emit(th, {"turn": turn, "type": "text", "text": str(e)})
            emit(th, {"turn": turn, "type": "done", "ms": int((time.time() - T.t0) * 1000), "model": "none", "label": "no model", "first_card_ms": T.first_card, "models": []})
        finally:
            await T.close()

    async def turn_guarded(th, turn, lang, model_pick, action=None):
        try:
            await asyncio.wait_for(action_run(th, turn, lang, action) if action else turn_run(th, turn, lang, model_pick), TURN_S)
        except asyncio.TimeoutError:
            emit(th, {"turn": turn, "type": "error", "text": SLOW[lang]})
        except Exception as e:
            msg = str(e)
            print("shop turn failed:", "".join(traceback.format_exception(e))[-3000:], flush=True)
            emit(th, {"turn": turn, "type": "error", "text": BUSY[lang] if "429" in msg or "rate" in msg.lower() else FAILED[lang]})
        finally:
            th["busy"] = False
            th["turns"][turn - 1].pop("image_data", None)  # the photo is not kept after its turn
            save(th)

    def action_label(a: dict, lang: str) -> str:
        """The person's side of a button press, in the conversation: what they pressed."""
        L, k = W[lang], a.get("type")
        if k == "confirm":
            return L["confirm"]
        if k == "cancel":
            return L["cancel"]
        if k == "again":
            d = g.cat.get(str(a.get("product") or ""))
            return L["usual" if a.get("asked") else "again"].format(x=name_of(d["id"], d["title"], lang) if d else "?")
        if k == "day":
            return L["day"].format(x=day_text(a.get("date"), lang))
        cart = cart_of(str(a.get("checkout") or "")) or {}
        try:
            it = cart["items"][int(a.get("item", -1))]
        except (LookupError, TypeError, ValueError):
            return L["swap"].format(x="?") if k == "swap" else L["drop"].format(x="?")
        if k == "drop":
            return L["drop"].format(x=it.get("as_written"))
        if k == "usual":
            ua = it.get("usual_amount") or {}
            return L["usual"].format(x=(ua.get("title_fi") if lang == "fi" else ua.get("title")) or it.get("as_written"))
        if k == "kind":
            want = next((x for x in it.get("kinds") or [] if x.get("kind") == a.get("kind")), {})
            return L["kind"].format(a=it.get("as_written"), x=(want.get("fi") or want.get("example_fi")) if lang == "fi" else want.get("kind"))
        try:
            ch = it["choices"][int(a.get("choice", -1))]
        except (LookupError, TypeError, ValueError):
            return L["swap"].format(x="?")
        sub = ch.get("substitute")
        return L["swap"].format(x=(sub.get("title_fi") if lang == "fi" else sub.get("title")) if sub else (ch["lines"][0].get("title_fi") if lang == "fi" else ch["lines"][0].get("title")))

    @r.post("/api/shop/chat", include_in_schema=False)
    async def chat_post(request: Request, response: Response, body: dict = Body(...)):
        c = ctx(request, response)
        lang = "en" if body.get("lang") == "en" else "fi"
        action = body.get("action") if isinstance(body.get("action"), dict) and body["action"].get("type") in ("confirm", "swap", "drop", "day", "again", "cancel", "kind", "usual") else None
        text = str(body.get("text") or "").strip()[:4000]
        image = body.get("image") or None
        if image is not None and not action:
            from .menu import _image
            image = _image(image)
        if action:
            text, image = action_label(action, lang), None
        if not text and not image:
            raise GatewayError(400, "bad_request", "Write what you need, or attach a photo.")
        tid = body.get("thread")
        if tid:
            th = thread_of(tid, c)
            if th.get("busy"):
                raise GatewayError(409, "busy", "The assistant is still answering the last message.")
        else:
            th = {"id": "st_" + secrets.token_hex(6), "slug": c["acct"]["slug"], "sid": c["sid"], "events": [], "turns": [], "busy": False, "at": time.time()}
            live[th["id"]] = th
            mine = g.store.get("shop_threads:" + c["sid"], [])
            g.store.put("shop_threads:" + c["sid"], (mine + [th["id"]])[-50:])
        if len(th["turns"]) >= 60:
            raise GatewayError(409, "too_long", "This conversation is long. Start a new one.")
        th["turns"].append({"text": text or ("Tässä kuva." if lang == "fi" else "Here is a photo."), "image": bool(image), **({"image_data": image} if image else {}), **({"action": action} if action else {})})
        turn = len(th["turns"])
        th["busy"] = True
        label = str(body.get("label") or "").strip()[:120]  # a card's button sends a long instruction for the assistant and a short label for the person
        emit(th, {"turn": turn, "type": "user", "text": text, "image": bool(image), **({"label": label} if label else {}), **({"action": action["type"]} if action else {})})
        pick = body.get("model") if body.get("model") in ("script", MODEL, FALLBACK_MODEL) else None
        task = asyncio.get_running_loop().create_task(turn_guarded(th, turn, lang, pick, action))
        tasks.add(task)
        task.add_done_callback(tasks.discard)
        return {"thread": th["id"], "turn": turn}

    @r.get("/api/shop/chat/{tid}", include_in_schema=False)
    def chat_get(tid: str, request: Request, response: Response, after: int = 0):
        th = thread_of(tid, ctx(request, response))
        return {"busy": bool(th.get("busy")), "events": [e for e in th["events"] if e["n"] > after]}

    @r.post("/api/shop/reset", include_in_schema=False)
    def reset(request: Request, response: Response):
        c = ctx(request, response)
        for tid in g.store.get("shop_threads:" + c["sid"], []):
            th = live.pop(tid, None) or g.store.get("shop_thread:" + tid)
            try:  # a conversation thrown away takes its unconfirmed carts with it: their stock is free again. Carts sent to an approver stay, under Hyväksynnät.
                p = principal(th["slug"], th)[1]
                for cid in dict.fromkeys(t.get("cart") for t in th.get("turns") or [] if t.get("cart")):
                    cur = g.get_checkout(cid, p)
                    if cur.get("status") in ("ready_for_complete", "incomplete") and not (cur.get("wholesale") or {}).get("approval"):
                        g.cancel_checkout(cid, p)
            except Exception:
                pass
            g.store.q("delete from kv where k=?", ("shop_thread:" + tid,))
        g.store.put("shop_threads:" + c["sid"], [])
        return {"ok": True}

    return r
