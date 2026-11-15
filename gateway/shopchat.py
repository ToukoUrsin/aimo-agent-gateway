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
import math
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

from . import chat, menu, pictures, policy
from .aimo import parse_ts
from .catalog import ALLERGENS, UNITS, allergen_hits, allergens_unchecked, declares_allergens, fold, kind, pieces
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
GREETING = {"fi": "Hei! Kerro mitä tarvitset tai mitä aiot tarjoilla, niin kokoan siitä ostoskorin Valio Aimon valikoimasta. Mitään ei tilata ennen kuin vahvistat.",
            "en": "Hello! Tell me what you need or what you plan to serve, and I will build the cart from Valio Aimo's range. Nothing is ordered until you confirm."}
BUSY = {"fi": "Tilausapuri on juuri nyt ruuhkautunut. Kokeile hetken päästä uudelleen.", "en": "The assistant is busy right now. Try again in a minute."}
SLOW = {"fi": "Tämä kesti liian kauan, joten jätin sen kesken. Se mitä kortit näyttävät pitää paikkansa; kokeile uudelleen.",
        "en": "That took too long, so I stopped. What the cards show stands; try again."}
FAILED = {"fi": "Jokin meni pieleen, enkä saanut tätä valmiiksi. Kokeile uudelleen.", "en": "Something went wrong and I could not finish this. Try again."}

# What the code says itself, without a model: the answer to a button on a card.
W = {
    "fi": {"confirm": "Vahvista tilaus", "late_ok": "Tilaa silti toimituspäivälle", "swap": "Vaihda: {x}", "day": "Siirrä päivälle {x}", "drop": "Jätä pois: {x}", "again": "Tilaa uudelleen: {x}", "usual": "Tavallinen määrä: {x}", "cancel": "Peru ostoskori", "recheck": "Tarkista uudelleen",
           "placed": "Tilaus {label} on vahvistettu. Toimituspäivä {day}",
           "amend_confirm": "Vahvista muutos", "cancel_confirm": "Vahvista peruutus", "amend_placed": "Muutos tilaukseen {label} on vahvistettu. {what}", "cancel_placed": "Tilaus {label} on peruttu. {what}",
           "amend_off": "Muutosta ei tehty. Tilaus {label} on ennallaan.",
           "waits": "Tämä ostoskori odottaa vielä hyväksyntää, joten sitä ei voi vahvistaa. Lähetä kortin linkki hyväksyjälle.",
           "changed": "Hinta tai saatavuus muuttui sen jälkeen kun ostoskori koottiin, joten mitään ei tilattu. Tarkista päivitetty ostoskori ja vahvista uudelleen.",
           "stopped": "Valio Aimo on pysäyttänyt tilausapurin tilaukset tältä asiakasnumerolta, joten mitään ei tilattu. Kortti kertoo lisää.",
           "und_tick": "Ostoskorissa on {n} tuotetta, joiden allergeeneja valmistaja ei ole ilmoittanut. Rastita kortista, että olet lukenut niiden pakkausmerkinnät, ja paina sitten Vahvista tilaus.",
           "alg_line": "{name}: valmistajan ilmoituksen mukaan {x}.", "alg_has": "sisältää: {x}", "alg_may": "saattaa sisältää: {x}", "alg_zero": "ei ilmoitettuja allergeeneja", "alg_claim": "merkintä: {x}",
           "alg_blank": "{name}: valmistaja ei ole ilmoittanut allergeeneja, joten sitä ei voi tarkistaa tästä. Lue pakkaus.",
           "alg_blank_c": "{name}: valmistaja ei ole ilmoittanut allergeeniluetteloa, mutta tuotteessa on valmistajan merkintä: {c}.", "alg_b_ok": "Merkintä kattaa kysymästäsi: {x}.", "alg_b_open": "Tästä ei voi tarkistaa: {x}. Lue pakkaus.", "alg_b_rest": "muita allergeeneja",
           "alg_no": "Siinä on siis sitä, mistä kysyit.", "alg_maybe": "Se voi sisältää kysymääsi allergeenia, joten älä pidä sitä sopivana.", "alg_ok": "Kysymääsi allergeenia ei ole ilmoituksessa.",
           "alg_tail": "Tiedot ovat valmistajan ilmoituksesta, eivät apurin arvio.",
           "outside": "Kokoan ostoskorin vain Valio Aimon valikoimasta, muualta en hae tuotteita.",
           "alg_which": "En arvioi itse, mikä tuote sopii kenellekin. Kysy ostoskorin tuotteesta sen nimellä, niin näytän valmistajan ilmoituksen siitä.",
           "alg_k_has": "Kysymistäsi ilmoituksessa on: {x}.", "alg_k_may": "Voi sisältää: {x}.", "alg_k_none": "Ilmoituksessa ei ole: {x}.",
           "alg_c_has": "Valmistajan ilmoituksen mukaan kysymääsi ({k}) on näissä ostoskorin tuotteissa: {x}.", "alg_c_may": "Voi sisältää kysymääsi ({k}): {x}.",
           "alg_c_none": "Ostoskorin tuotteissa, joilla on valmistajan ilmoitus, ei ole kysymääsi ({k}).",
           "alg_c_blank": "Valmistajan ilmoitus puuttuu ({n}): {x}. Sitä ei voi tarkistaa tästä, lue pakkaus.",
           "gone": "Tätä ostoskoria ei enää ole. Kerro uudelleen mitä tarvitset.", "not_placed": "Tilausta ei tehty. {why}",
           "swapped": "Vaihdettu. Ostoskori on päivitetty; mitään ei ole vielä tilattu.", "moved": "Ostoskorin toimituspäivä on nyt {day} Mitään ei ole vielä tilattu.",
           "dropped": "Poistettu ostoskorista.", "kind_set": "Lisätty ostoskoriin. Mitään ei ole vielä tilattu.", "kind": "{a}: {x}", "canceled": "Ostoskori on peruttu. Mitään ei tilattu.", "recheck_ok": "Tarkistin ostoskorin uudelleen: sen voi nyt tilata. Mitään ei ole vielä tilattu.", "recheck_no": "Tarkistin ostoskorin uudelleen: sitä ei vieläkään voi tilata. Syy on kortilla.", "empty": "Ostoskoriin ei jäänyt yhtään riviä.",
           "again_none": "En löytänyt tälle tuotteelle tavallista määrää. Kerro kuinka paljon tarvitset.",
           "bad_amount": "Määrän pitää olla suurempi kuin nolla, joten en koonnut ostoskoria pyynnöstä ”{x}”. Viestin muutkaan rivit eivät menneet ostoskoriin: lähetä se uudelleen oikealla määrällä.",
           "built": "Ostoskori on koottu. Mitään ei ole vielä tilattu: tilaus lähtee vasta, kun painat Vahvista tilaus.",
           "built_esc": "Ostoskori on koottu. Se tarvitsee hyväksynnän ennen kuin sen voi vahvistaa: lähetä kortin linkki hyväksyjälle.",
           "built_stop": "Ostoskoria ei voi tilata tällaisenaan. Kortti kertoo miksi.",
           "chg_added": "Lisätty: {x}.", "chg_removed": "Poistettu: {x}.", "chg_amount": "Määrä muutettu: {x}.", "chg_swapped": "Tuote vaihtui: {x}.", "chg_day": "Toimituspäivä on nyt {x}",
           "chg_none": "Ostoskori ei muuttunut.", "chg_early": "Päivälle {a} ei voi enää tilata: aikaisin toimituspäivä on {day} Ostoskori on sille päivälle.",
           "bad_line": "”{x}” jäi pois: määrän pitää olla suurempi kuin nolla.", "chg_budget": "Valitsin budjettiisi sopivan tuotteen: {a}, {s} (alv 0 %). {u}{b} olisi maksanut {t}.", "chg_nomatch": "Ostoskorista ei löytynyt riviä sanalla ”{x}”. Kerro, mitä riviä tarkoitat.", "chg_more": "ja {n} muuta",
           "chg_fail": "Muutosta ei voitu tehdä, joten ostoskori on ennallaan. Kokeile uudelleen.",
           "chg_short": "{a}: pyysit {asked}, mutta saatavilla on vain {got}. Ostoskorissa on se määrä; muut vaihtoehdot ovat kortin Vaihda-napin takana.",
           "chg_repl": "{a}: pyysit {asked}, mutta sitä on saatavilla vain {got}. Loput on ostoskorissa korvaavana tuotteena {sub}; kortti kertoo miksi.",
           "chg_keep": "”{a}”: {asked} ei ole saatavilla toimituspäivälle {day}, joten rivi on ennallaan ({now}).",
           "chg_stays": "Ostoskorissa on siksi edelleen {x} ({q}); poista se, jos et halua sitä.",
           "chg_miss": "Valio Aimon valikoimassa ei ole tuotetta nimellä ”{n}”, joten ostoskorissa on lähin vastaava: {got}.",
           "chg_new": "Tämä ostoskori korvaa aiemman.", "chg_guests": "Henkilömäärä on nyt {n} (oli {m}), ja määrät ja pakkaukset on laskettu sille uudelleen.", "chg_same": "Rivit ovat samat kuin aiemmassa ostoskorissa.", "chg_gone": "Poistui: {x}.", "chg_fresh": "Uutta: {x}.", "chg_amounts": "Määrä muuttui {n} rivillä.",
           "off_kind": "”{a}” ei ole ostoskorissa: valitse kortista, mitä lajia tarkoitat.", "off_amount": "”{a}” ei ole ostoskorissa: kerro, paljonko tarvitset.",
           "off_usual": "”{a}” ei ole ostoskorissa: kerro määrä tai valitse kortista tavallinen määräsi.", "off_day": "”{a}” ei ole ostoskorissa: sitä ei saa toimituspäivälle {day}",
           "off_none": "”{a}” ei ole ostoskorissa: Valio Aimon valikoimasta ei löytynyt sitä.",
           "off_much": "”{a}” ei ole ostoskorissa: määrä ylittää sen, mitä tilausapuri saa laittaa yhteen tilaukseen ({n} myyntiyksikköä). Kirjoita pienempi määrä.",
           "off_near": "Kortilla on lähimmät Valio Aimon valikoimasta, jotka saa: valitse niistä.",
           "off_strict": "{a} ei ole saatavilla toimituspäivälle {day} Pyysit, ettei korvaavia tuotteita laiteta, joten sitä ei ole ostoskorissa.",
           "off_strict_never": "{a} on Valio Aimon valikoimassa, mutta sitä ei juuri nyt saa millekään toimituspäivälle. Pyysit, ettei korvaavia tuotteita laiteta, joten sitä ei ole ostoskorissa.",
           "off_strict_none": "Valio Aimon valikoimassa ei ole tuotetta nimellä ”{a}”. Pyysit, ettei korvaavia tuotteita laiteta, joten sitä ei ole ostoskorissa.",
           "off_never": "”{a}” ei ole ostoskorissa: Valio Aimon valikoimassa on sitä, mutta sitä ei juuri nyt saa millekään toimituspäivälle.", "off_excl": "”{a}” ei ole ostoskorissa: Valio Aimon valikoimasta ei löytynyt sille tuotetta, joka sopii rajaukseesi ({x}).",
           "next": "Mitään ei ole vielä tilattu: tilaus lähtee vasta, kun painat Vahvista tilaus.", "next_esc": "Ostoskori tarvitsee hyväksynnän ennen kuin sen voi vahvistaa: lähetä kortin linkki hyväksyjälle.",
           "next_stop": "Ostoskoria ei voi tilata tällaisenaan. Kortti kertoo miksi."},
    "en": {"confirm": "Confirm the order", "late_ok": "Order anyway for", "swap": "Change to: {x}", "day": "Move to {x}", "drop": "Leave out: {x}", "again": "Order again: {x}", "usual": "The usual amount: {x}", "cancel": "Cancel the cart", "recheck": "Check again",
           "placed": "Order {label} is confirmed. Delivery {day}.",
           "amend_confirm": "Confirm the change", "cancel_confirm": "Confirm the cancellation", "amend_placed": "The change to order {label} is confirmed. {what}", "cancel_placed": "Order {label} is cancelled. {what}",
           "amend_off": "No change was made. Order {label} stands as it was.",
           "waits": "This cart is still waiting for approval, so it cannot be confirmed. Send the link on the card to the approver.",
           "changed": "The price or the stock changed after the cart was built, so nothing was ordered. Check the updated cart and confirm again.",
           "stopped": "Valio Aimo has stopped the assistant's orders for this customer number, so nothing was ordered. The card says more.",
           "und_tick": "The cart has {n} products whose manufacturer declared no allergens. Tick on the card that you have read their labels, then press Confirm the order.",
           "alg_line": "{name}: by the manufacturer's declaration, {x}.", "alg_has": "contains: {x}", "alg_may": "may contain: {x}", "alg_zero": "no declared allergens", "alg_claim": "claim: {x}",
           "alg_blank": "{name}: the manufacturer declared no allergens, so it cannot be checked here. Read the label.",
           "alg_blank_c": "{name}: the manufacturer gave no list of allergens, but the product carries the manufacturer's claim: {c}.", "alg_b_ok": "Of what you asked about, the claim covers: {x}.", "alg_b_open": "This cannot be checked here: {x}. Read the label.", "alg_b_rest": "other allergens",
           "alg_no": "So it has what you asked about.", "alg_maybe": "It may contain what you asked about, so do not count it as suitable.", "alg_ok": "What you asked about is not in the declaration.",
           "alg_tail": "This is the manufacturer's declaration, not the assistant's judgement.",
           "outside": "I build the cart only from Valio Aimo's range and do not look for products anywhere else.",
           "alg_which": "I do not judge myself what suits whom. Ask about a product on the cart by its name and I show the manufacturer's declaration for it.",
           "alg_k_has": "Of what you asked about, the declaration lists: {x}.", "alg_k_may": "May contain: {x}.", "alg_k_none": "The declaration does not list: {x}.",
           "alg_c_has": "By the manufacturers' declarations, what you asked about ({k}) is in these products on the cart: {x}.", "alg_c_may": "May contain what you asked about ({k}): {x}.",
           "alg_c_none": "The products on the cart that have a manufacturer's declaration do not list what you asked about ({k}).",
           "alg_c_blank": "No manufacturer's declaration ({n}): {x}. That cannot be checked here, read the label.",
           "gone": "This cart is no longer there. Tell me again what you need.", "not_placed": "The order was not placed. {why}",
           "swapped": "Changed. The cart is updated; nothing is ordered yet.", "moved": "The cart is now for {day}. Nothing is ordered yet.",
           "dropped": "Removed from the cart.", "kind_set": "Added to the cart. Nothing is ordered yet.", "kind": "{a}: {x}", "canceled": "The cart is cancelled. Nothing was ordered.", "recheck_ok": "I checked the cart again: it can be ordered now. Nothing is ordered yet.", "recheck_no": "I checked the cart again: it still cannot be ordered. The card says why.", "empty": "No line was left in the cart.",
           "again_none": "I found no usual amount for this product. Tell me how much you need.",
           "bad_amount": "An amount has to be more than zero, so I built no cart from \"{x}\". The other lines of the message were not put on a cart either: send it again with the right amount.",
           "built": "The cart is built. Nothing is ordered yet: the order goes out only when you press Confirm the order.",
           "built_esc": "The cart is built. It needs an approval before it can be confirmed: send the link on the card to the approver.",
           "built_stop": "This cart cannot be ordered as it is. The card says why.",
           "chg_added": "Added: {x}.", "chg_removed": "Removed: {x}.", "chg_amount": "Amount changed: {x}.", "chg_swapped": "Product changed: {x}.", "chg_day": "The delivery day is now {x}.",
           "chg_none": "The cart did not change.", "chg_early": "Delivery on {a} is no longer possible: the earliest delivery day is {day}. The cart is for that day.",
           "bad_line": "\"{x}\" was left out: an amount has to be more than zero.", "chg_budget": "I picked the product that fits your budget: {a}, {s} ex VAT. {u}{b} would have cost {t}.", "chg_nomatch": "No line in the cart matches \"{x}\". Tell me which line you mean.", "chg_more": "and {n} more",
           "chg_fail": "The change could not be made, so the cart is as it was. Try again.",
           "chg_short": "{a}: you asked for {asked}, but only {got} can be delivered. That amount is in the cart; the other choices are behind the card's Change button.",
           "chg_repl": "{a}: you asked for {asked}, but only {got} of it can be delivered. The rest is in the cart as the replacement {sub}; the card says why.",
           "chg_keep": "\"{a}\": {asked} cannot be delivered on {day}, so the line is as it was ({now}).",
           "chg_stays": "So the cart still has {x} ({q}); remove it if you do not want it.",
           "chg_miss": "Valio Aimo's range has no product called \"{n}\", so the cart has the nearest one of that kind: {got}.",
           "chg_new": "This cart replaces the earlier one.", "chg_guests": "It is now for {n} people (was {m}), and the amounts and packs are worked out again for that.", "chg_same": "Its lines are the same as in the earlier cart.", "chg_gone": "No longer in it: {x}.", "chg_fresh": "New in it: {x}.", "chg_amounts": "The amount changed on {n} line(s).",
           "off_kind": "\"{a}\" is not in the cart: pick on the card which kind you mean.", "off_amount": "\"{a}\" is not in the cart: tell me how much you need.",
           "off_usual": "\"{a}\" is not in the cart: write an amount or pick your usual amount on the card.", "off_day": "\"{a}\" is not in the cart: it cannot be delivered on {day}.",
           "off_none": "\"{a}\" is not in the cart: nothing like it was found in Valio Aimo's range.",
           "off_much": "\"{a}\" is not in the cart: the amount is over what this assistant may put in one order ({n} sales units). Write a smaller amount.",
           "off_near": "The card has the nearest ones from Valio Aimo's range that can come: pick one there.",
           "off_strict": "{a} is not available for delivery on {day}. You asked for no substitutes, so it is not in the cart.",
           "off_strict_never": "{a} is in Valio Aimo's range, but right now it cannot be delivered on any day. You asked for no substitutes, so it is not in the cart.",
           "off_strict_none": "Valio Aimo's range has no product called \"{a}\". You asked for no substitutes, so it is not in the cart.",
           "off_never": "\"{a}\" is not in the cart: Valio Aimo's range has it, but right now it cannot be delivered on any day.", "off_excl": "\"{a}\" is not in the cart: Valio Aimo's range has no product for it that fits your exclusion ({x}).",
           "next": "Nothing is ordered yet: the order goes out only when you press Confirm the order.", "next_esc": "The cart needs an approval before it can be confirmed: send the link on the card to the approver.",
           "next_stop": "This cart cannot be ordered as it is. The card says why."},
}
# A sentence of a model that says what a change did. On a turn that changes a cart the shop says that itself, from the cart's lines before and after.
CHANGE_CLAIM = re.compile(r"poistettu|poistin|poistui|lisätty|lisäsin|vaihdettu|vaihdoin|vaihtui|korvattu|korvasin|nostettu|nostin|laskettu|laskin|päivitetty|päivitin|muutettu|muutin|otettu pois|otin pois|jätetty pois|jätin pois"
                          r"|\b(?:removed|added|changed|updated|increased|raised|lowered|reduced|replaced|swapped|switched|took out|taken out|left out)\b", re.I)
MORE = re.compile(r"(?<![\wäöå])(?:lisää|lisätään|vielä|lisäksi|more|another|extra|additional)(?![\wäöå])", re.I)
# An allergy the person states for the kitchen rules that allergen out of every cart of the conversation. The rule engine checks it; a model's caution is not a check.
STATED_STEM = [("peanut", "maapahkin"), ("nut", "(?<!maa)pahkin"), ("sesame", "seesam"), ("soy", "soija"), ("milk", "maito|maido"), ("gluten", "gluteeni"), ("egg", "(?:kanan)?muna"), ("fish", "kala")]
ALLERGY_ONLY = [("gluten", r"keliak|coeliac|celiac"), ("milk", r"(?:maito|milk|dairy)[- ]?allerg"), ("egg", r"(?:kanan)?muna[- ]?allerg|\begg[- ]?allerg"), ("fish", r"\bkala[- ]?allerg|\bfish[- ]?allerg")]
EXCL_WORD = {"fi": {"nut": "pähkinätön", "peanut": "maapähkinätön", "sesame": "seesamiton", "soy": "soijaton", "shellfish": "äyriäisetön", "celery": "selleritön", "mustard": "sinapiton", "milk": "maidoton", "egg": "kananmunaton", "fish": "kalaton", "gluten": "gluteeniton"},
             "en": {"nut": "nut-free", "peanut": "peanut-free", "sesame": "sesame-free", "soy": "soy-free", "shellfish": "shellfish-free", "celery": "celery-free", "mustard": "mustard-free", "milk": "milk-free", "egg": "egg-free", "fish": "fish-free", "gluten": "gluten-free"}}


def word_hit(word: str, hay: str, whole: bool = False) -> bool:
    """Whether the person's word for a line names it: the word itself or its stem ("sipulit" names "sipuli"), and every word of a longer one.
    whole: only as a word of its own, so "sipuli" names the onion and not "valkosipuli"."""
    if " " in word:
        return (not whole and word in hay) or all(word_hit(w, hay, whole) for w in word.split() if len(w) > 2)
    forms = [word] + ([word[:-1]] if len(word) >= 5 else []) + ([word[:-2]] if len(word) >= 7 else [])
    return any(re.search(r"(?<![\wäöå])" + re.escape(f) + r"\w{0,3}(?![\wäöå])", hay) for f in forms) if whole else any(f in hay for f in forms)


# "Meillä on gluteeniton keittiö": said of the whole kitchen, it rules the allergen out of the cart. "12 keliaakikkoa" does not: that is a share of the portions.
KITCHEN_FREE = [("gluten", r"gluteeniton|gluten[- ]free"), ("milk", r"maidoton|milk[- ]free|dairy[- ]free"), ("egg", r"munaton|egg[- ]free"), ("fish", r"kalaton|fish[- ]free")]
# Another seller named in a request ("jos ei ole, ota Kesprolta"): the shop says in words that it does not look there.
AMOUNT_HEAD = re.compile(r"^\s*\d+(?:[.,]\d+)?\s*(?:kpl|kg|g|l|dl|ml|litraa?|kiloa?|grammaa|pkt|pakettia|ltk|laatikkoa|rs|pss|pussia|prk|purkkia|tlk|plo|pulloa|sakkia|säkkiä|pcs|x)?(?![\w,.])\s*", re.I)
INNER = re.compile(r"(?<![\wäöå])(?:pullo\w*|tölk\w*|bottles?|cans?)(?![\wäöå])", re.I)  # "15 pulloa": counted by the bottle, not by the case
ALSO = re.compile(r"(?<![\wäöå])(?:myös|päälle|ylimäär\w*|also|as well|on top)(?![\wäöå])", re.I)
NO_SUBS = re.compile(r"ei korvaav|ala korvaa|ilman korvaav|ei saa korvata|ei vaihtoeht|ei muuta tuotetta|ei muita tuotteita|vain mita on\b|vain sita,? mita on|no substitut|no replacement|not substitute|n.?t substitute|without substitut|only what is available|only what.s available|nothing else instead")
OUTSIDE = re.compile(r"kespro|metro-?tuk|wihuri|heinon tuk|meira nova|\bpatu\b|\blidl|prisma|citymarket|s-ryhma|k-ryhma|toiselta tuk|toisesta tuk|muulta toimittaj|another wholesaler|other supplier|another supplier|somewhere else|elsewhere")
QUESTION = re.compile(r"[\s/–-]*(?:ja\s+|enta\s+|and\s+)?(?:(?:sopii?ko|soveltuuko|onko|sisaltaako|voiko|voinko|saako|saanko|kelpaako|kayko|tuleeko|loytyyko|uskaltaako|mita|mika|mitka|missa|miksi)\b|(?:is|does|can|are|do|will|would|could|may|what|which|why)\s)")


BUDGET = re.compile(r"(?:budjet\w*|budget)[^\d€.]{0,40}?(\d[\d \u00a0]*(?:[.,]\d{1,2})?)\s*(?:€|e\b|eur\w*)|(\d[\d \u00a0]*(?:[.,]\d{1,2})?)\s*(?:€|eur\w*)\s*(?:n\s+)?(?:budjet\w*|budget)", re.I)


def budget_of(text) -> float | None:
    """The budget the person wrote for the order, in euros ("budjetti enintään 300 euroa"); the last one said holds. The shop never cuts a cart to fit it: it says when the cart is over."""
    m = None
    for m in BUDGET.finditer(str(text or "")):
        pass
    if not m:
        return None
    try:
        return float((m.group(1) or m.group(2)).replace(" ", "").replace("\u00a0", "").replace(",", ".")) or None
    except ValueError:
        return None


def stated_exclusions(text: str) -> list[str]:
    """The allergens the person's own words rule out: "pähkinätön", "vakava pähkinäallergia", "ei pähkinöitä", "nut-free", "allergic to sesame", "kala-allergia"."""
    # a question states nothing: "Sopiiko tämä pähkinäallergiselle?" asks, "meillä on pähkinäallergikko" tells
    said = []
    for sent in re.split(r"\s+/\s+|(?<=[.!?])\s+|\n+", str(text or "")):
        for part in sent.split(","):  # "meillä on pähkinäallergikko, sopiiko tämä?": what stands before the question still tells
            if QUESTION.match(menu.fold(part)):
                break
            said.append(part)
    f = menu.fold(" . ".join(said))
    out = [k for k in menu._kinds(f) if k in menu.HARD] + [k for k, pat in ALLERGY_ONLY if re.search(pat, f)]
    out += [k for k, pat in KITCHEN_FREE if re.search(rf"(?:keittio\w*|kitchen|koko tilau\w*|whole order|\bkaikki|\bkaiken|\beverything)[^.]{{0,40}}?(?:{pat})|(?:{pat})\w*\s+(?:keittio|kitchen)", f)]
    # said as a ban: "ei saa sisältää maitoa", "älä laita pähkinää", "pähkinävapaa", and for the allergens with no stand-in also "pähkinät pois"
    for k, stem in STATED_STEM:
        if re.search(rf"\b(?:ei saa (?:sisaltaa|olla)|ei yhtaan|ala laita|alkaa laittako)\s+(?:mitaan\s+|yhtaan\s+)?(?:{stem})\w*|\b(?:{stem})\w*[- ]?vapaa\w*", f) or (k in menu.HARD and re.search(rf"\b(?:{stem})\w*\s+pois\b", f)):
            out.append(k)
    if "nut" in out and "peanut" in out:
        out.remove("peanut")  # a nut note rules out peanuts already
    if re.search(r"\b(?:ei|ilman)\s+maapahkino\w*", f):
        out.append("peanut")
    elif re.search(r"\b(?:ei|ilman)\s+pahkino\w*", f):  # "ei pähkinöitä": the plural's stem is not the singular's
        out.append("nut")
    return list(dict.fromkeys(out))

# A question about a product's allergens is answered from the manufacturer's declaration, by code. A model's sentence about what suits whom is never shown.
ALG_FI = {"AM": "maito", "AE": "kananmuna", "AF": "kala", "AC": "äyriäiset", "UM": "nilviäiset", "AP": "maapähkinä", "AN": "pähkinät", "AY": "soija", "AS": "seesami", "BM": "sinappi", "BC": "selleri", "NL": "lupiini",
          "AU": "sulfiitit", "AW": "gluteenia sisältävät viljat", "UW": "vehnä", "GB": "ohra", "GO": "kaura", "NR": "ruis", "GS": "speltti", "GK": "kamut", "ML": "laktoosi", "SA": "manteli", "SC": "cashewpähkinä",
          "SH": "hasselpähkinä", "SM": "makadamiapähkinä", "SP": "pekaanipähkinä", "SR": "parapähkinä", "ST": "pistaasipähkinä", "SW": "saksanpähkinä"}
CLAIM_WORD = {"fi": {"FREE_FROM:LACTOSE": "laktoositon", "FREE_FROM:GLUTEN": "gluteeniton", "DIET:FREE_FROM_GLUTEN": "gluteeniton", "FREE_FROM:WHEAT": "vehnätön", "FREE_FROM:EGGS": "kananmunaton", "FREE_FROM:SOY": "soijaton", "DIET:VEGAN": "vegaaninen"},
              "en": {"FREE_FROM:LACTOSE": "lactose-free", "FREE_FROM:GLUTEN": "gluten-free", "DIET:FREE_FROM_GLUTEN": "gluten-free", "FREE_FROM:WHEAT": "wheat-free", "FREE_FROM:EGGS": "egg-free", "FREE_FROM:SOY": "soy-free", "DIET:VEGAN": "vegan"}}
ASK_KIND = [("peanut", r"maapahkin|peanut"), ("nut", r"pahkin|\bnuts?\b"), ("gluten", r"gluteeni|keliaak|gluten|coeliac|celiac"), ("milk", r"\bmaito|maido|\bmilk|dairy"), ("lactose", r"laktoos|lactose"),
            ("egg", r"muna|\beggs?\b"), ("fish", r"\bkala|\bfish"), ("soy", r"soija|\bsoy"), ("sesame", r"seesam|sesame"), ("vegan", r"vegaani|vegan")]
ASK_CODES = {**menu.EXCODES, "lactose": {"ML"}}
RULE_KIND = {v: k for k, v in menu.DIET_WORD.items()}
# The dietary rules under which a menu's missing main ingredient may still be asked again as a line: the ones an allergen declaration can answer. Never vegan or vegetarian.
GAP_RULES = {r for r, k in RULE_KIND.items() if k in ASK_CODES and k != "vegan"}
ASK_NAME = {"fi": {"peanut": "maapähkinä", "nut": "pähkinät", "gluten": "gluteeni", "milk": "maito", "lactose": "laktoosi", "egg": "kananmuna", "fish": "kala", "soy": "soija", "sesame": "seesami", "vegan": "eläinperäiset ainekset",
                   "shellfish": "äyriäiset", "celery": "selleri", "mustard": "sinappi"},
            "en": {"peanut": "peanuts", "nut": "nuts", "gluten": "gluten", "milk": "milk", "lactose": "lactose", "egg": "egg", "fish": "fish", "soy": "soy", "sesame": "sesame", "vegan": "animal products",
                   "shellfish": "shellfish", "celery": "celery", "mustard": "mustard"}}
ALLERGEN_ASK = re.compile(r"sopii|soveltu|allerg|sisalta|turvalli|suitable|\bsafe\b|contain|" + "|".join(pat for _, pat in ASK_KIND))
ALLERGEN_CLAIM = re.compile(r"sopii|sopivat|\bsovi\b|soveltu|ei sisällä|sisältä|gluteenit|laktoosit|pähkinät|maidot|maitotuot|vegaani|turvalli|allerg|suitable|\bsafe\b|[- ]free\b|\bcontains?\b|allergen|coeliac|celiac", re.I)
ASK_STOP = {"miksi", "korissa", "ostoskorissa", "onko", "tämä", "tuote", "tuotteet", "mitä", "voiko", "myös", "niille", "heille", "siinä", "kaikille", "what", "does", "this", "that", "cart", "with", "have", "there", "product",
            "which", "about", "would", "could", "everyone", "them",
            "sen", "tai", "ole", "eli", "jos", "kun", "vai", "the", "and", "for", "can", "are", "has", "its", "but", "any", "not", "you", "how", "one"}


SOFT = lambda s: re.sub(r"([kpt])\1", r"\1", s).replace("d", "t")


def allergen_facts(text: str, lines: list[dict], lang: str) -> str | None:
    """What the manufacturer declared for the cart's products the question names, and whether the allergen asked about is among it. None when the
    message is not a question about allergens or names no product of the cart. lines: catalogue records of the cart's products."""
    f = fold(text)
    if not ALLERGEN_ASK.search(f) or ("?" not in text and not re.match(r"\s*(sopii?ko|soveltuuko|onko|sisaltaako|voiko|is |does |can |are )", f)):
        return None
    L = W[lang]
    raw = [w for w in re.findall(r"[a-zäöå0-9]{3,}", str(text).lower()) if w not in ASK_STOP]
    words = {w for w in raw if not ALLERGEN_ASK.search(fold(w))}
    # "kalaliemessä", "maitojuomassa": a compound that begins with an allergen's word may name a product of the cart; then it does not ask about fish or milk
    comp = {w for w in raw if w not in words and (lambda m: m and m.start() == 0 and len(w) - m.end() >= 6)(ALLERGEN_ASK.search(fold(w)))}
    used = set()
    # a short word or stem ("voi", "voissa") names a product only as a whole word of its name
    stems = lambda w: {x for x in (w, w[:-1], w[:-2], w[:-3], w[:-4]) if len(x) >= 5} | ({w} if len(w) > 3 else set()) | {f" {x} " for x in (w, w[:-1], w[:-2], w[:-3]) if 3 <= len(x) <= 4}
    scored = []
    brand = {b for d in lines for b in re.findall(r"[a-zäöå0-9]{3,}", str(d.get("brand") or "").lower())}
    for d in lines:
        hay = " " + re.sub(r"[^a-zäöå0-9]+", " ", " ".join([str(d.get("title") or ""), str((d.get("names") or {}).get("fi") or ""), str(d.get("brand") or ""), str(d.get("_said") or "")]).lower()) + " "
        soft_hay = SOFT(hay)  # Finnish stems soften inside a word: maito is "maidossa", kinkku is "kinkussa"
        in_hay = lambda w: any(x in hay or SOFT(x) in soft_hay for x in stems(w))
        mine = {w for w in comp if any(len(x) >= 7 and (x in hay or SOFT(x) in soft_hay) for x in (w, w[:-1], w[:-2], w[:-3], w[:-4]))}
        n = sum(1 for w in words | mine if in_hay(w))
        if n and any(in_hay(w) for w in words | mine if w not in brand):  # the brand alone names no product: "Valio kuohukerma" is not the cart's Valio milk
            scored.append((n, d))
            d["_asked_by"] = {w for w in words | mine if w not in brand and in_hay(w)}
            used |= mine
    for w in used:
        f = f.replace(fold(w), " ")
    kinds = list(dict.fromkeys([k for k in menu._kinds(f) if k in ASK_CODES] + [k for k, pat in ASK_KIND if re.search(pat, f)]))
    codes = set().union(*(ASK_CODES[k] for k in kinds)) if kinds else set()
    names = lambda cs: ", ".join((ALG_FI if lang == "fi" else ALLERGENS).get(c, c) for c in cs)
    if not scored:
        if not lines or not (kinds or "allerg" in f):
            return None  # "sopiiko toimitus tiistaille?" is no question about allergens
        if not codes or not re.search(r"ostoskor|\bkori|tilau|\bkaik|\bnama\b|\bnaissa\b|\bcart\b|\border\b|\bthese\b|\ball\b|everything", f):
            return ""  # a question about allergens that names nothing on the cart
        # the whole cart: which of its products declare what was asked about, and which declare nothing
        nm = lambda d: ((d.get("names") or {}).get("fi") if lang == "fi" else None) or d["title"]
        lst = lambda v: ", ".join(v[:6]) + ((f" ja {len(v) - 6} muuta" if lang == "fi" else f" and {len(v) - 6} more") if len(v) > 6 else "")
        uniq = list({d["id"]: d for d in lines}.values())
        has = [d for d in uniq if allergen_hits(codes, d.get("contains"), d.get("claims"))]
        may = [d for d in uniq if d not in has and allergen_hits(codes, d.get("may_contain"), d.get("claims"))]
        blank = [d for d in uniq if allergens_unchecked(codes, d)]
        k = ", ".join(ASK_NAME[lang].get(x, x) for x in kinds)
        out = [L["alg_c_has"].format(k=k, x=lst([nm(d) for d in has]))] if has else []
        out += [L["alg_c_may"].format(k=k, x=lst([nm(d) for d in may]))] if may else []
        out = out or [L["alg_c_none"].format(k=k)]
        out += [L["alg_c_blank"].format(n=len(blank), x=lst([nm(d) for d in blank]))] if blank else []
        return " ".join(out + [L["alg_tail"]])
    top = max(n for n, _ in scored)
    out = []
    asked = list({d["id"]: d for n, d in scored if n == top}.values())[:3]
    seen = set().union(*(d.get("_asked_by") or set() for d in asked))
    for n, d in sorted(scored, key=lambda x: -x[0]):  # several products named in one question: each one a word of the question names alone gets its answer too
        if len(asked) < 4 and d["id"] not in {x["id"] for x in asked} and (d.get("_asked_by") or set()) - seen:
            asked.append(d)
            seen |= d["_asked_by"]
    for d in asked:
        name = ((d.get("names") or {}).get("fi") if lang == "fi" else None) or d["title"]
        if not declares_allergens(d):
            # No list of allergens from the manufacturer. Its own claim on the product (gluteeniton) still settles what it covers, as it does in the rule
            # that checks the cart: the answer says the claim, what of the question it covers and what it leaves open.
            marks = list(dict.fromkeys(CLAIM_WORD[lang][c] for c in d.get("claims") or [] if c in CLAIM_WORD[lang]))
            if not marks:
                out.append(L["alg_blank"].format(name=name))
                continue
            settled = [ASK_NAME[lang].get(k, k) for k in kinds if not allergens_unchecked(ASK_CODES[k], d)]
            left = [ASK_NAME[lang].get(k, k) for k in kinds if allergens_unchecked(ASK_CODES[k], d)]
            out.append(L["alg_blank_c"].format(name=name, c=", ".join(marks)) + (" " + L["alg_b_ok"].format(x=", ".join(settled)) if settled else "") + (" " + L["alg_b_open"].format(x=", ".join(left) or L["alg_b_rest"]) if left or not kinds else ""))
            continue
        parts = ([L["alg_has"].format(x=names(d["contains"]))] if d.get("contains") else []) + ([L["alg_may"].format(x=names(d["may_contain"]))] if d.get("may_contain") else [])
        parts = parts or [L["alg_zero"]]
        claims = list(dict.fromkeys(CLAIM_WORD[lang][c] for c in d.get("claims") or [] if c in CLAIM_WORD[lang]))
        if claims:
            parts.append(L["alg_claim"].format(x=", ".join(claims)))
        line = L["alg_line"].format(name=name, x="; ".join(parts))
        if len(kinds) > 1:  # several things asked at once: each gets its own answer, so "it has milk" is never read as "it has gluten too"
            how = lambda k: "has" if allergen_hits(ASK_CODES[k], d.get("contains"), d.get("claims")) else "may" if allergen_hits(ASK_CODES[k], d.get("may_contain"), d.get("claims")) else "none"
            by = {h: [ASK_NAME[lang].get(k, k) for k in kinds if how(k) == h] for h in ("has", "may", "none")}
            line += "".join(" " + L["alg_k_" + h].format(x=", ".join(by[h])) for h in ("has", "may", "none") if by[h])
        elif codes:
            line += " " + (L["alg_no"] if allergen_hits(codes, d.get("contains"), d.get("claims")) else L["alg_maybe"] if allergen_hits(codes, d.get("may_contain"), d.get("claims")) else L["alg_ok"])
        out.append(line)
    return " ".join(out + [L["alg_tail"]])


PLACED_CLAIM = re.compile(r"tilaus on (nyt )?(tehty|vahvistettu|lähetetty|lähtenyt|tilattu)|tilasin (sen|ne|tuotteet)|olen tilannut|order (is|has been|was) (now )?(placed|confirmed|sent)|i (have )?(placed|ordered)", re.I)

FI_DAYS = ["ma", "ti", "ke", "to", "pe", "la", "su"]
NUMBER_WORDS = {"yksi": 1, "yhdellä": 1, "yhden": 1, "yhdelle": 1, "one": 1, "kaksi": 2, "kahdella": 2, "kahden": 2, "kahdelle": 2, "two": 2, "kolme": 3, "kolmella": 3, "kolmen": 3, "three": 3,
                "neljä": 4, "neljällä": 4, "four": 4, "viisi": 5, "viidellä": 5, "five": 5, "kuusi": 6, "kuudella": 6, "six": 6, "seitsemän": 7, "seven": 7, "kahdeksan": 8, "eight": 8,
                "yhdeksän": 9, "nine": 9, "kymmenen": 10, "kymmenellä": 10, "ten": 10}
# The recipe cards and shelf ingredients go by English names in code; the Finnish page shows these.
DISH_FI = {"Creamy salmon soup": "Kermainen lohikeitto", "Meat soup": "Lihakeitto", "Karelian stew": "Karjalanpaisti", "Mashed potatoes": "Perunamuusi", "Crushed lingonberries": "Puolukkasurvos", "Christmas ham": "Joulukinkku", "Carrot casserole": "Porkkanalaatikko", "Rosolli beetroot salad": "Rosolli", "Rice porridge": "Riisipuuro", "Green salad with vinaigrette": "Vihreä salaatti ja vinegretti", "Green salad": "Vihreä salaatti", "Chicken breast with thyme cream sauce": "Broilerin rintafilee ja timjamikermakastike",
           "Roasted potatoes and carrots": "Paahdetut perunat ja porkkanat", "Mushroom risotto": "Sienirisotto", "Strawberry cream cake": "Mansikkakermakakku", "Coffee and tea": "Kahvi ja tee",
           "Rye bread and butter": "Ruisleipä ja voi", "Macaroni casserole": "Makaronilaatikko", "Chicken sauce and rice": "Broilerikastike ja riisi",
           "Meatballs, mashed potatoes and brown sauce": "Lihapullat, perunamuusi ja ruskeakastike", "Pea soup": "Hernekeitto", "Oven pancake with strawberry jam": "Pannukakku ja mansikkahillo",
           "Fish fingers and boiled potatoes": "Kalapuikot ja keitetyt perunat", "Grated carrot": "Porkkanaraaste", "Crisp bread and spread": "Näkkileipä ja levite", "Milk to drink": "Ruokajuomaksi maito",
           "Scrambled eggs": "Munakokkeli", "Fried bacon": "Paistettu pekoni", "Breakfast sausages": "Aamiaismakkarat", "Oat porridge": "Kaurapuuro", "Boiled eggs": "Keitetyt kananmunat",
           "Croissants": "Croissantit", "Cheese, ham and vegetables": "Juusto, kinkku ja kasvikset", "Yoghurt, muesli and jam": "Jogurtti, mysli ja hillo", "Fresh fruit": "Tuoreet hedelmät", "Orange juice": "Appelsiinimehu"}
ITEM_FI = {"beef chuck": "naudan etuselkä", "christmas ham": "joulukinkku", "pork shoulder": "porsaan lapa", "lingonberry": "puolukka", "gluten-free flour mix": "gluteeniton jauhoseos", "swede": "lanttu", "beef bouillon": "lihaliemi", "porridge rice": "puuroriisi", "pickled cucumber": "maustekurkku", "breadcrumbs": "korppujauho", "gluten-free breadcrumbs": "gluteeniton korppujauho", "dark syrup": "tumma siirappi", "cinnamon": "kaneli", "vegan cheese": "vegaaninen juusto", "whipping cream": "vispikerma", "cooking cream": "ruokakerma", "plant whipping cream": "kasvipohjainen vispikerma", "oat cooking cream": "kauraruokakerma", "milk": "maito", "coffee milk": "kahvimaito",
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
           "macaroni": "makaroni", "sausage": "makkara", "ham": "kinkku", "bacon": "pekoni", "olive oil": "oliiviöljy", "rapeseed oil": "rypsiöljy",
           # the kinds offered in place of one that cannot come: said in the cook's words, not as an example product
           "smoked ham": "palvikinkku", "cured ham": "ilmakuivattu kinkku", "christmas ham": "joulukinkku", "ham strip": "kinkkusuikale", "parma ham": "parmankinkku", "ham sausage": "kinkkumakkara",
           "pork neck": "porsaan kassler", "pork shoulder": "porsaan lapa", "pork loin": "porsaan ulkofilee", "pork tenderloin": "porsaan sisäfilee", "pork belly": "porsaan kylki", "pork rib": "porsaan grillikylki",
           "diced pork": "porsaan kuutiot", "pork strip": "porsaan suikale", "pork escalope": "porsaan leike", "pork loin steak": "porsaan ulkofileepihvi", "pork chop": "porsaankyljys",
           "beef brisket": "naudan rinta", "diced beef": "naudan kuutiot", "beef strip": "naudan suikale", "beef topside": "naudan sisäpaisti", "beef rump": "naudan paahtopaisti", "beef entrecote": "naudan entrecôte",
           "beef tenderloin steak": "naudan sisäfileepihvi", "beef chuck": "naudan etuselkä", "salmon cube": "lohikuutiot", "cod fillet": "turskafilee", "pike perch fillet": "kuhafilee",
           "rainbow trout fillet": "kirjolohifilee", "whitefish fillet": "siikafilee", "chicken strip": "broilerinsuikale", "coffee cream": "kahvikerma", "whipped cream": "kermavaahto", "smoked salmon": "savulohi",
           "wheat bread": "vehnäleipä", "toast bread": "paahtoleipä", "multigrain bread": "moniviljaleipä"}

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
  Taking a product off a cart that came from a menu ("poista vehnäjauho") is also build_cart with add_to and remove; the rest of the menu cart stays as it is.
  Another pack of a product a menu put on the cart ("vaihda makaroni 10 kg säkkeihin"): build_cart with add_to and that one item, query = its kind, as_written = their words, no quantity and no remove: the shop keeps the amount and changes the pack.
  Another product in place of one on the cart ("vaihda vehnäjauho gluteenittomaan jauhoon", "ota voin tilalle margariinia"): one build_cart with add_to, remove = the line that goes, and one item for the new product whose quantity is the amount of the line it replaces, worked out from the list (3 × 2 kg = 6 kg). Never ask how much.
  A request that also names another seller ("jos ei ole, ota Kesprolta", "from Metro"): build the cart here for everything asked; the shop itself adds the sentence that it does not look anywhere else. Never refuse the whole message for it.
  The shop itself tells the person what a change did, from the cart's lines; never say yourself that something was added, removed or changed.
- An allergy or a "-free" kitchen ("koko keittiö on pähkinätön", "vakava pähkinäallergia", "nut-free"): the shop reads it from the person's words and checks every line of the cart against it. Pass the items as asked;
  never leave a product out yourself because of it and never judge a product's allergens yourself. Never say that a product suits, is safe for or is free from anything:
  when the person asks that about a product on the cart, the shop adds the manufacturer's declaration to your answer, so answer only the rest of the question. When the person says it after a cart is built, call build_cart with add_to = that cart's id and no items.
- "This is what I will serve" (a menu, an event with its dishes, a recipe, a photo): call plan_menu_order with the dishes as the person named them, the guests, the date of the event and every dietary note.
  A dish written in another language than English goes as {{"name": the dish in plain English, "as_written": the dish exactly as the person wrote it}}: the card shows the person's own words.
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
    A confirmed order can be cancelled as a whole until its ordering cut-off: when the person says in so many words to cancel a confirmed order, call cancel_order with its number (list_orders gives the numbers; "the order I just made" is the latest one; ask which when it is unclear). It answers with a card where the person confirms the cancellation with a button: say that in one sentence and never say the order is cancelled before they have pressed it. When it is refused because the cut-off has passed, say so with the time.
    A confirmed order cannot be changed line by line in this prototype: say that in one sentence, and offer to cancel it and build a new cart, or to build a new cart for what is missing.
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


UNIT_WORD = r"(?:l|ltr|litra\w*|litre\w*|liter\w*|dl|ml|kg|kilo\w*|g|gramm\w*|kpl|pcs|pieces?|pkt|ltk|prk|tlk|plo|pss|laatikko\w*|box(?:es)?|cases?)"
BAD_AMOUNT = re.compile(r"(?<![\w.,])(?:[-−]\d+(?:[.,]\d+)?|0+(?:[.,]0+)?)\s*" + UNIT_WORD + r"\b", re.I)


ABOUT_ORDER = re.compile(r"VA-AG-\d|\btilauks\w*|\btilau\w*\b.*\b(?:muut|vaihd|peru|poist)|\b(?:muut|vaihd|peru|poist)\w*\b.*\btilau|\b(?:change|cancel|amend)\w*\b.*\border\b|\b(?:my|placed|confirmed) order\b", re.I)


def bad_part(text: str, bad: str) -> tuple[str, str]:
    """The item of a list that carries an amount no one can order ("-3 kg juustoa"), and the message without it."""
    parts = [x for x in re.split(r"([,;\n]+|\s+(?:ja|sekä|and)\s+)", str(text or ""))]
    for i in range(0, len(parts), 2):
        if bad in parts[i]:
            rest = "".join(parts[:i - 1] + parts[i + 1:]) if i else "".join(parts[2:])
            return parts[i].strip(), rest.strip(" ,;\n")
    return bad, ""


def bad_amount(text: str) -> str | None:
    """An amount no one can order, as the person wrote it: a negative one ("-5 litraa") or zero ("0 kg"). Not the zeros of
    "1 000 l", not a range ("5 l -10 l", "10-20 kg"), and not the dash that starts each line of a list."""
    t = str(text or "")
    lines = [l.strip() for l in t.splitlines() if l.strip()]
    bullets = len(lines) > 1 and sum(1 for l in lines if l[:1] in "-−") >= 2
    for m in BAD_AMOUNT.finditer(t):
        before = t[:m.start()].rstrip(" \u00a0\u202f")
        if re.search(r"\d$", before) or re.search(r"(?<![\w])" + UNIT_WORD + r"$", before, re.I):
            continue  # a thousands group or the second half of a range
        if m.group(0)[:1] in "-−" and bullets:
            continue
        return m.group(0).strip()
    return None


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
        """The mandate the account signed for its shop assistant: the account's own limits, and that the person in the shop confirms every order
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
            try:  # what happened after the order: an open delivery exception, a signed change or a cancellation shows on the order's own row
                st = g.after.state(o)
                kinds = sorted({i["kind"] for i in g.after.issues_of(o["id"]) if i["status"] in ("open", "pending")})
                now = {"state": st["status"], "open_issues": len([k for k in kinds if k != "cancel_request"]), "kinds": [k for k in kinds if k != "cancel_request"], "cancel_pending": "cancel_request" in kinds}
            except Exception:
                now = {}
            placed.append({**now, "id": o["id"], "label": o.get("label"), "placed_at": w.get("placed_at"), "delivery_date": w.get("delivery_date"), "lines": len(o.get("line_items") or []),
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
    async def hello(request: Request, response: Response, lang: str = "fi", demo: str = ""):
        lang = "en" if lang == "en" else "fi"
        c = ctx(request, response)
        a = c["acct"]
        _, p = principal(a["slug"])
        if demo == "1" and not g.store.get("review:" + a["slug"]):
            # The shop was opened from its review link (/shop?demo=1). Only then can one person on one device open the approver's view;
            # the switch is kept here, per test kitchen, and the ledger says when it was turned on.
            g.store.put("review:" + a["slug"], {"at": time.time()})
            g.store.log(None, "gateway", "review_mode", "Review mode was switched on for this test kitchen by opening the shop from its review link (/shop?demo=1). One person on one device can now open the approver's view; "
                        "every approval given that way is recorded as a demo approval. A shop opened without the link has no such view.", {"account": a["slug"]}, acct=p.get("customer"))
        elif demo != "1" and g.store.get("review:" + a["slug"]):
            # Review mode follows the link, not the browser: the shop opened without it is the plain shop again.
            g.store.q("delete from kv where k=?", ("review:" + a["slug"],))
            g.store.log(None, "gateway", "review_mode", "Review mode was switched off for this test kitchen: the shop was opened without its review link. The approver's view can no longer be opened on the device that asked.",
                        {"account": a["slug"]}, acct=p.get("customer"))
        review = bool(g.store.get("review:" + a["slug"]))
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
                "orders": len(g.store.orders(30, p["customer"])), "products": products, "card_token": card_token, "review": review}

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
        if it.get("own_pick"):
            out["own_pick"] = True  # a kind the person tapped on the card themselves: their own pick, on any day the cart moves to
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
        return "replaced" if ch.get("substitute") else "short" if (ch.get("covers_pct") or 100) < 100 else "ok"

    def plan_item(it: dict, day: str, p: dict, tr: str, excl: tuple = ()) -> dict:
        """Plan one asked item for one day. Never raises: an item that cannot be planned says why."""
        out = dict(it)
        for k in ("choices", "chosen", "kinds", "why", "available_on", "status", "usual_amount", "named_miss", "no_fit", "never", "too_much", "near", "named_off", "named_none"):
            out.pop(k, None)
        strict = bool(it.get("strict")) and not it.get("own_pick")  # "ei korvaavia tuotteita": only what was asked for, in the amount that can come
        qty, unit, pref = it.get("quantity"), it.get("unit"), it.get("preferred_product")
        toks = lambda t: set(re.findall(r"[a-z0-9]+", fold(t or "")))
        named = str(pref).strip() if pref and not g.cat.get(str(pref)) else ""
        if not pref or named:
            # A brand the person wrote ("2 purkkia Nutellaa") is the product they asked for, whether or not the model passed it on;
            # and a pack size written with a count of packs ("2 purkkia … 3 kg") means that pack, not the first one of the brand.
            def docs_of(q_):
                try:
                    return g.cat.pick(q_, {})[0]
                except Exception:
                    return []
            docs = docs_of(it["query"])
            bare = " ".join(re.sub(r"(?<!\w)\d+(?:[.,]\d+)?\s*(?:kg|g|l|dl|ml)\b", " ", str(it["query"]), flags=re.I).split())
            if not docs and bare and bare != it["query"] and docs_of(bare):
                # A pack size written into the product's name ("Nutella 3 kg") is read as a size below, not searched for as words of the name.
                docs, it = docs_of(bare), {**it, "query": bare}
                out["query"] = bare
            said = toks(it.get("as_written"))
            said |= {w[:-1] for w in said if len(w) > 4} | {w[:-2] for w in said if len(w) > 6}  # "Nutellaa", "Nutellasta"
            if not pref:
                brands = sorted({str(d.get("brand") or "").strip() for d in docs} - {""}, key=len, reverse=True)
                named = next((b for b in brands if len(fold(b)) > 2 and toks(b) <= said - toks(it["query"])), "") or next((b for b in brands if len(fold(b)) > 2 and toks(b) == toks(it["query"])), "")
                pref = out["preferred_product"] = named or None
            m = re.search(r"(\d+(?:[.,]\d+)?)\s*(kg|g|l|dl|ml)\b", fold(it.get("as_written") or "")) if named else \
                re.search(r"(\d+(?:[.,]\d+)?)\s*(kg|g|l|dl|ml)\b:?n?\s+(?:sak|pus|purk|purn|tolk|kanist|astia|laatik|pak|ampar|sack|bag|jar|box|can|pack|carton|bucket|tub)", fold(it.get("as_written") or ""))
            if m:
                v, u = float(m.group(1).replace(",", ".")), m.group(2)
                size, key = v / {"kg": 1, "g": 1000, "l": 1, "dl": 10, "ml": 1000}[u], "kg" if u in ("kg", "g") else "l"
                brand_words = toks(named) & {w for d in docs for w in toks(d.get("brand"))} or toks(named) - toks(str(v)) - toks(m.group(1)) - {u}
                exact = next((d for d in docs if brand_words <= toks(d.get("_text") or d["title"]) and abs(((d.get("content") or {}).get(key) or 0) - size) <= 0.01 * size), None)
                if exact:
                    pref = out["preferred_product"] = str(exact["id"])
                    named = ""
                    # "2 purkkia … 3 kg" passed on as 6 kg is still two of that pack.
                    per = {"kg": {"kg": 1, "g": 0.001}, "l": {"l": 1, "dl": 0.1, "ml": 0.001}}[key].get(str(unit or "").lower())
                    n = (qty or 0) * per / size if per and size else 0
                    if n >= 0.99 and abs(n - round(n)) < 0.01:
                        qty, unit = out["quantity"], out["unit"] = round(n), "pcs"
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
            by_bottle = str(unit or "").lower() == "pcs" and (it.get("by_bottle") or INNER.search(str(it.get("as_written") or "")))
            pl = g.plan({"query": it["query"], "need": {"quantity": qty, "unit": "bottles" if by_bottle else unit}, "delivery_date": day, "preferred_product": pref,
                         "filters": {**({"claims": it["claims"]} if it.get("claims") else {}), **({"exclude_allergens": list(excl)} if excl else {})}}, p, tr)
        except GatewayError as e:
            return {**out, "status": "not_found", "why": e.message}
        notes = " ".join(str(m.get("content")) for m in pl.get("messages") or [])
        opts = [o for o in pl.get("options") or [] if (o.get("authority") or {}).get("decision") != "invalid"]
        opts.sort(key=lambda o: (o.get("authority") or {}).get("decision") == "not_allowed")  # stable: the planner's own order, what a rule refuses last
        if not opts and not pl.get("kinds_to_choose") and not it.get("_alt"):
            # A product the person named may be filed under another kind than the model's plain word for it ("KNORR Ruskea kastikepohja 2,5kg" asked
            # as "brown sauce base"): when nothing of that kind can come, the range is asked once more by the words as written. The answer counts only
            # when those words name a product or a brand of the range, so the line is that product or says which product stands in its place.
            alt = AMOUNT_HEAD.sub("", str(it.get("as_written") or "")).strip()
            if alt and fold(alt) != fold(it["query"]):
                try:
                    mine, theirs = {d["id"] for d in g.cat.pick(it["query"], {})[0]}, {d["id"] for d in g.cat.pick(alt, {})[0]}
                except Exception:
                    theirs = set()
                if theirs and not theirs <= mine and len(theirs) <= 60:
                    again = plan_item({**it, "query": alt, "_alt": True}, day, p, tr, excl)
                    if again.get("preferred_product") and (again.get("choices") or again.get("named_off")):
                        return again
            out["_alt"] = True  # asked once; the later days are not asked twice each
        np_ = pl.get("named_product") if named else None  # the name the person wrote, found among the products of the range
        ids = {str(i) for i in (np_ or {}).get("ids") or []} or ({str(pref)} if pref and g.cat.get(str(pref)) and not it.get("usual") else set())
        one = next(iter(ids)) if len(ids) == 1 else None

        def off():  # what was asked for by name: one product, or a brand that several products carry
            return {"id": one, "title": (g.cat.get(one) or {}).get("title"), "title_fi": fi_name(one, (g.cat.get(one) or {}).get("title"))} if one else {"name": named or str(it.get("as_written") or it["query"])}
        if strict and opts:
            # Never another product in place of the one asked for: the product itself in full, or the part of it that can come, or nothing.
            kept = [] if named and not np_ else [o for o in opts if not o.get("substitute") and (not ids or all(str(l["item"]["id"]) in ids for l in o.get("line_items") or []))]
            if not kept:
                if named and not np_:
                    return {**out, "status": "not_found", "why": f"no product called {named}", "named_none": named, "strict": True}
                gone = bool((np_ or {}).get("gone"))
                return {**out, "status": "not_found" if gone else "not_on_day", **({"never": True} if gone else {}), "named_off": off(), "strict": True, "why": "the product asked for cannot come, and no replacement was wanted"}
            opts = kept
        if not opts:
            if any("Rule M2" in str(r) for o in pl.get("options") or [] for r in (o.get("authority") or {}).get("reasons") or []):
                # the range has it and it could come, but not this much in one order: said as that, never as "not available on this day"
                return {**out, "status": "not_found", "why": "over the per-order limit", "too_much": ((p or {}).get("mandate") or {}).get("max_units_per_order") or 0}
            if pl.get("kinds_to_choose"):
                # Only kinds that can come on the day are offered to choose from, each with a product that can: one availability request for the question.
                try:
                    of_kind = g.cat.pick(it["query"], {**({"claims": it["claims"]} if it.get("claims") else {}), **({"exclude_allergens": list(excl)} if excl else {})})[3].get("choices") or {}
                    pool = {k.get("kind"): list(of_kind.get(k.get("kind")) or [])[:25] for k in pl["kinds_to_choose"][:6]}
                    seen = g._live([d["id"] for ds in pool.values() for d in ds], day, p) if any(pool.values()) else {}
                    can = {k: [d for d in ds if seen.get(d["id"], {}).get("orderable")] for k, ds in pool.items()}
                    pl["kinds_to_choose"] = [{**k, "example": {"id": can[k.get("kind")][0]["id"], "title": can[k.get("kind")][0]["title"]}} for k in pl["kinds_to_choose"][:6] if can.get(k.get("kind"))]
                except Exception:
                    can = None
                if can is not None and not pl["kinds_to_choose"]:
                    return {**out, "status": "not_on_day", "why": "none of the kinds this could mean can be delivered on that day"}
                return {**out, "status": "which_kind", "kinds": [{"kind": k.get("kind"), "fi": KIND_FI.get(str(k.get("kind")).lower()), "example": (k.get("example") or {}).get("title"),
                                                              "example_fi": fi_name((k.get("example") or {}).get("id"), (k.get("example") or {}).get("title"))} for k in pl["kinds_to_choose"][:6]]}
            reasons = {x.get("reason") for x in pl.get("not_offered") or []}
            said = " ".join(m.get("content", "") for m in pl.get("messages") or [] if m.get("type") in ("warning", "error"))
            nothing = any("range is called" in str(m.get("content")) for m in pl.get("messages") or [])  # no product by that name at all: not a matter of the day
            if excl and "none of them meets the requirements" in notes:  # the kind is in the range, and nothing of it fits what the kitchen ruled out: not a matter of the day either
                return {**out, "status": "not_found", "why": said, "no_fit": True}
            if not nothing and reasons - {None} and reasons - {None} <= {"not_deliverable", "sales_block", "product_recalled"}:
                # in the range, and it cannot come on any day right now: no later day is asked for it, and the card says that, not "not on this day"
                return {**out, "status": "not_found", "why": said, "never": True, "reasons": sorted(x for x in reasons if x)}
            if not nothing and (reasons & {"cutoff_passed", "later_date_only", "out_of_stock", "allocation_share_used", "delivery_delayed", "short_at_warehouse"} or "can be delivered on" in said):
                return {**out, "status": "not_on_day", "why": said, "reasons": sorted(x for x in reasons if x)}
            return {**out, "status": "not_found", "why": said, **({"no_fit": True} if excl and "none of them meets the requirements" in notes else {})}
        out["choices"], out["chosen"] = [choice(o) for o in opts[:4]], 0
        out["status"] = state_of(out["choices"][0])
        if ids and not it.get("own_pick"):
            if not any(str(l["id"]) in ids for l in out["choices"][0]["lines"]):
                # what was asked for by name is in the range and cannot come: the cart says which product stands in its place, and whether another day would help
                gone = bool(np_.get("gone")) if np_ else (pl.get("preferred_off") or {}).get("reason") in ("not_deliverable", "sales_block", "product_recalled")
                out["named_off"] = {**off(), **({"never": True} if gone else {})}
        elif named:  # the product that stands on the line does not carry the name the person gave: the cart says so, by both names
            d0 = g.cat.get(str(out["choices"][0]["lines"][0]["id"])) or {}
            if not ((toks(named) - toks(it["query"])) or toks(named)) <= toks(str(d0.get("_text") or "") + " " + str(d0.get("title") or "") + " " + str(d0.get("brand") or "")):
                out["named_miss"] = named
        return out

    async def plan_all(items: list[dict], day: str, p: dict, tr: str, excl: tuple = ()) -> list[dict]:
        sem = asyncio.Semaphore(4)

        async def one(it):
            async with sem:
                return await asyncio.to_thread(plan_item, it, day, p, tr, excl)
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
        day, excl = cart["day"], tuple(cart.get("exclude_allergens") or ())
        if cart.get("no_subs"):
            items = [x | {"strict": True} for x in items]
        own = [cart["id"]] if cart.get("id") else []
        hold_state(own, "held", "paused")  # a line planned again is planned as if the cart's own stock were free: it is the same cart asking
        try:
            got = await plan_all(items, day, p, tr, excl)
            failing = [i for i, x in enumerate(got) if x["status"] == "not_on_day"]
            others = [x for x in cart["items"] if x.get("choices") and not x.get("dropped")] + [x for x in got if x.get("choices")] + [l for l in cart.get("fixed") or [] if not l.get("dropped")]
            if failing and others:  # the rest of the cart can come: these stay off it, each with the first day it could come (the days are asked side by side)
                sem = asyncio.Semaphore(4)

                async def probe_one(i, d2):
                    async with sem:
                        return i, d2, await asyncio.to_thread(plan_item, got[i], d2, p, tr, excl)
                for i, d2, x in sorted(await asyncio.gather(*(probe_one(i, d2) for i in [i for i in failing if not got[i].get("gap")][:4] for d2 in later_days(day, 4))), key=lambda t: t[1]):
                    if x.get("choices") and not got[i].get("available_on"):
                        got[i]["available_on"] = d2
            elif failing:
                for d2 in later_days(day, 3):
                    probe = await plan_all([got[i] for i in failing], d2, p, tr, excl)
                    if all(x.get("choices") for x in probe):  # nothing can come on the asked day: the whole cart moves
                        for i, x in zip(failing, probe):
                            got[i] = x
                        cart["moved"], cart["day"] = {"from": cart.get("asked_day") or day, "to": d2}, d2
                        break
            # What cannot come on the cart's day, or on any day: the nearest kinds of Valio Aimo's own range that can, for the person to pick from.
            # At most three such lookups a turn; each is one availability request.
            lost = [x for x in got if not x.get("choices") and x.get("quantity") and x.get("unit") and (x.get("status") == "not_on_day" or x.get("never"))][:3]

            def near_of(x):
                try:
                    return g.near_kinds(x["query"], {"quantity": x["quantity"], "unit": x["unit"]}, cart["day"], p, {"exclude_allergens": list(excl)} if excl else {})
                except GatewayError:
                    return []
            for x, near in zip(lost, await asyncio.gather(*(asyncio.to_thread(near_of, x) for x in lost))):
                if near:
                    x["near"] = True
                    x["kinds"] = [{"kind": k["kind"], "fi": KIND_FI.get(str(k["kind"]).lower()), "example": k["example"]["title"], "example_fi": fi_name(k["example"]["id"], k["example"]["title"])} for k in near]
        finally:
            hold_state(own, "paused", "held")  # the cart keeps its stock until its checkout is updated

        def same(x, y):  # the same thing asked again (a new amount): same kind, and not two different named products
            if not y.get("choices") and x.get("query", "").lower() in [str(k.get("kind")).lower() for k in y.get("kinds") or []]:
                return True  # the answer to "which kind?" takes the question's place, and so does a near kind picked for what cannot come
            if not y.get("choices") and y.get("status") in ("ask_amount", "not_found") and y.get("query", "").lower() == x.get("query", "").lower():
                return True  # so does the answer to "how much?": the question never stays on the card beside its answer
            return y.get("query", "").lower() == x.get("query", "").lower() and (not x.get("preferred_product") or not y.get("preferred_product") or x["preferred_product"] == y["preferred_product"])
        refs(cart)
        fresh = []  # two items of one request are two lines ("10 kg spagettia ja 1 kg gluteenitonta spagettia"), never one taking the other's place
        for x in got:  # an item takes the place of the line it names, or of the earlier one of the same kind; anything else is a new line
            ln = x.pop("line", None)
            earlier = [y for y in cart["items"] if not any(y is f for f in fresh)]
            hit = [y for y in earlier if ln and y.get("ref") == ln] or [y for y in earlier if same(x, y)]
            fresh.append(x)
            if hit and not x.get("choices") and hit[0].get("choices") and not hit[0].get("dropped") and x.get("status") in ("not_on_day", "not_found"):
                # the new amount cannot be delivered: the line stays as it was, and the person is told so. A change never takes a line off the cart.
                hit[0]["_miss"] = {"t": x.get("_t"), "quantity": x.get("quantity"), "unit": x.get("unit"), "status": x.get("status")}
                continue
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
        keep = ("as_written", "query", "quantity", "unit", "usual", "usual_amount", "status", "kinds", "available_on", "why", "reasons", "chosen", "choices", "dropped", "ref", "named_miss", "no_fit", "never", "too_much", "near", "named_off", "named_none", "strict", "gap", "budget_pick")
        return {"id": cart.get("id"), "day": cart["day"], "asked_day": cart.get("asked_day"), "moved": cart.get("moved"), "title": cart.get("title"), "same_as_last_week": cart.get("same_as_last_week"), "stated": cart.get("stated") or [], "no_subs": bool(cart.get("no_subs")), "budget": cart.get("budget"), "blocked": cart.get("blocked") or [],
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
        am = w.get("amends")
        if am:  # not a cart: a change to an order that is already placed (a delivery problem settled, or a cancellation the person asked for)
            out = [f"This card is a change to placed order {am.get('order_label')}: {am.get('title')}. {am.get('effect')}"]
            if st == "ready_for_complete":
                btn = W[lang]["cancel_confirm" if any(x.get("type") == "cancelled" for x in am.get("changes") or []) else "amend_confirm"]
                return out[0] + f" Nothing has changed on the order yet: the person confirms it with the button \"{btn}\" on the card. Say that in one sentence, and never say it is done."
            if st == "completed":
                return out[0] + " It is confirmed and signed; the order confirmation shows it."
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
            if it.get("gap"):
                nm3 = lambda x: (x.get("title_fi") if lang == "fi" else None) or x.get("title")
                tell.append(f"'{a}' ({it.get('quantity'):g} {it.get('unit')}) is an ingredient of the menu that the menu's own products could not cover on that day. "
                            + (f"The cart has {' + '.join(str(nm3(l)) for l in ch['lines'])} for it instead, in the amount the menu needs. Say this plainly, by both names." if ch else "Say it first, plainly: the dish has no such ingredient on the cart yet, and the card offers what can come in its place with the amount already worked out."))
            if it.get("named_off") and ch:
                nm1 = lambda x: (x.get("title_fi") if lang == "fi" else None) or x.get("title") or f"a product called '{x.get('name')}'"
                tell.append(f"'{a}': {nm1(it['named_off'])} is in Valio Aimo's range but cannot be delivered on {'any day right now' if it['named_off'].get('never') else cart['day']}. The cart has the nearest product of that kind instead: {' + '.join(str(nm1(l)) for l in ch['lines'])}. Say this plainly, by both names; the card says it too.")
            if it.get("strict") and not ch:
                nm2 = (it.get("named_off") or {}).get("title_fi" if lang == "fi" else "title") or (it.get("named_off") or {}).get("title") or (it.get("named_off") or {}).get("name") or it.get("named_none")
                tell.append(f"'{a}': the person asked for no substitutes, so nothing was put on the cart in place of {nm2}. Say exactly that. Never say a replacement was added.")
            if it.get("named_miss") and ch:
                nm0 = lambda x: (x.get("title_fi") if lang == "fi" else None) or x.get("title")
                tell.append(f"'{a}': Valio Aimo's range has no product called '{it['named_miss']}'. The cart has the nearest product of that kind instead: {' + '.join(str(nm0(l)) for l in ch['lines'])}. Say this plainly, by both names; the card says it too.")
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
                tell.append(f"'{a}' is NOT on the cart: " + (f"Valio Aimo's range has no product of that kind that fits the kitchen's stated exclusion ({', '.join(EXCL_WORD['en'].get(k, k) for k in (cart or {}).get('stated') or [])}). Say so plainly."
                                                         if it.get("no_fit") else "Valio Aimo's range has it, but right now it cannot be delivered on any day. Say exactly that; do not say it was not found." if it.get("never")
                                                         else f"the amount is over what this shop assistant may put in one order ({it['too_much']:g} sales units). Say exactly that and ask for a smaller amount; do not say it is unavailable." if it.get("too_much")
                                                         else f"nothing like it was found in Valio Aimo's range ({it.get('why') or 'no match'})."))
            if it.get("near") and it.get("kinds") and not ch:
                nk = lambda k: (k.get("fi") or k.get("example_fi") if lang == "fi" else None) or k.get("kind")
                tell.append(f"For '{a}' the card offers the nearest kinds from Valio Aimo's own range that can come that day, as buttons: " + ", ".join(str(nk(k)) for k in it["kinds"]) + ". Say that the card has them; do not pick one yourself.")
        if (cart or {}).get("no_subs"):
            tell.append("The person asked for no substitutes: every line is the product asked for, or the part of it that can come, or it is left off the cart. Nothing stands in for another product.")
        if (cart or {}).get("stated"):
            tell.append("The person's own words rule out for this kitchen: " + ", ".join(EXCL_WORD["en"].get(k, k) for k in cart["stated"]) + ". Every line of the cart is checked against it by Valio Aimo's rules, and the card says so. "
                        "A product that contains it cannot be ordered; one whose manufacturer declared no allergens is named on the card for the person to check.")
        menu_ = (cart or {}).get("menu")
        if menu_:
            tell += menu_.get("tell") or []
        for it in (cart or {}).get("items") or []:
            bp = it.get("budget_pick")
            if bp and it.get("choices") and not it.get("dropped") and st not in ("completed", "canceled"):
                ch_ = it["choices"][it.get("chosen") or 0]
                tell.append(f"The person's budget picked the product: {ch_['lines'][0].get('title')} at {money_text(ch_.get('total'), lang)} ex VAT is on the cart, because {'the product this account orders most often' if bp.get('usual') else 'the first choice'} "
                            f"({bp['from'].get('title')}) would have cost {money_text(bp.get('from_total'), lang)}. Say this in one sentence; the card says it too and has the button to change it.")
        over = (_amount(full.get("totals")) or 0) - round(((cart or {}).get("budget") or 0) * 100)
        if (cart or {}).get("budget") and over > 0 and st not in ("completed", "canceled"):
            tell.append(f"The person said a budget of at most {money_text(round(cart['budget'] * 100), lang)}. This cart is {money_text(_amount(full.get('totals')), lang)} ex VAT, {money_text(over, lang)} over it. "
                        "Say this plainly, with the figures; the card warns about it above the button. Do not change the cart on your own: the person decides what to leave out.")
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
        if t.get("skipped"):
            out += f"\n\nThe shop left out this part of the message, because an amount has to be more than zero: \"{t['skipped']}\". Do not build it, with any amount, and do not ask about it: the shop tells the person. Build everything else they asked for."
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
            placed_ = t is not th["turns"][turn - 1] and not t.get("cart") and ABOUT_ORDER.search(str(t.get("text") or ""))  # about an order already placed ("vaihda tilauksen perunat"): not a word of the next cart's request
            if not t.get("action") and not asked_only and not placed_ and not t.get("refused"):
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

    def cart_allergens(T: Turn, text: str) -> str | None:
        """The manufacturer's declaration for the products of this conversation's latest cart that the person's question names."""
        cid = next((t.get("cart") for t in reversed(T.th["turns"]) if t.get("cart")), None)
        if not cid or not ALLERGEN_ASK.search(fold(text)):
            return None
        try:
            cur = g.get_checkout(str(cid), T.p)
        except GatewayError:
            return None
        said, cart = {}, cart_of(str(cid)) or {}  # a line also goes by what the person or the menu called it: "spagetti" is the cart's Spaghetti n.5
        for d_ in ((cart.get("menu") or {}).get("by_dish") or []):
            for l in d_.get("lines") or []:
                said[str(l.get("id"))] = said.get(str(l.get("id")), "") + f" {l.get('fi') or ''} {l.get('item') or ''}"
        for it in cart.get("items") or []:
            if it.get("choices") and not it.get("dropped"):
                for l in it["choices"][it.get("chosen") or 0]["lines"]:
                    said[str(l["id"])] = said.get(str(l["id"]), "") + f" {AMOUNT_HEAD.sub('', str(it.get('as_written') or ''))} {KIND_FI.get(str(it.get('query') or '').lower(), '')}"
        return allergen_facts(text, [{**d, "_said": said.get(str(d["id"]), "")} for d in (g.cat.get(str(l["item"]["id"])) for l in cur.get("line_items") or []) if d], T.lang)

    def typed_words(T: Turn) -> str:
        return " / ".join(str(t.get("text") or "") for t in T.th["turns"][:T.turn] if not t.get("action"))

    def apply_stated(T: Turn, cart: dict) -> bool:
        """An allergy the person stated anywhere in this conversation is a hard exclusion on the cart: the planner leaves such products out and the rule engine checks every line. -> whether the cart got a new one."""
        if NO_SUBS.search(fold(typed_words(T))):
            cart["no_subs"] = True  # said once, it holds for the cart: a line gets the product asked for or stays off it
        kinds = [k for k in stated_exclusions(typed_words(T)) if k not in (cart.get("per_guest") or [])]
        codes = sorted(set(cart.get("exclude_allergens") or []) | set().union(*(menu.EXCODES[k] for k in kinds))) if kinds else None
        if not kinds or (codes == sorted(cart.get("exclude_allergens") or []) and set(kinds) <= set(cart.get("stated") or [])):
            return False
        cart["stated"], cart["exclude_allergens"] = sorted(set(cart.get("stated") or []) | set(kinds)), codes
        return True

    def fit_budget(cart: dict, turn: int) -> None:
        """A budget the person wrote picks the product when more than one meets the request: when the first choice would take the cart over the
        budget and a cheaper product covers the whole amount, the cheaper one is on the cart, and the card and the chat say which it stands in
        front of. Only for a product the person did not name, in the turn it was asked, and never on a menu's cart."""
        b = round((cart.get("budget") or 0) * 100)
        live = [it for it in cart["items"] if it.get("choices") and not it.get("dropped")]
        if not b or cart.get("fixed") or not live:
            return
        tot = lambda it: it["choices"][it.get("chosen") or 0].get("total") or 0
        for it in sorted(live, key=lambda it: -tot(it)):
            if sum(tot(x) for x in live) <= b:
                break
            cur = it["choices"][it.get("chosen") or 0]
            if it.get("_t") != turn or it.get("preferred_product") or it.get("budget_pick") or cur.get("substitute") or len(cur.get("lines") or []) != 1:
                continue
            better = [(c.get("total") or 0, i) for i, c in enumerate(it["choices"]) if (c.get("covers_pct") or 0) >= 100 and len(c.get("lines") or []) == 1 and not c.get("substitute") and 0 < (c.get("total") or 0) < tot(it)]
            if better:
                i = min(better)[1]
                it["budget_pick"] = {"from": {k: cur["lines"][0].get(k) for k in ("id", "title", "title_fi")}, "from_total": cur.get("total"), "t": turn, "usual": cur.get("label") == "usual_product"}
                it["chosen"], it["status"] = i, state_of(it["choices"][i])

    def amount_text(q, unit, lang: str) -> str:
        if not q:
            return ""
        n = f"{float(q):,.2f}".rstrip("0").rstrip(".").replace(",", "\u00a0" if lang == "fi" else ",")  # never "1e+06": the amount as a person writes it
        return f"{n.replace('.', ',') if lang == 'fi' else n} " + {"KG": "kg", "L": "l", "pcs": "kpl" if lang == "fi" else "pcs"}.get(str(unit), str(unit or ""))

    def amend_public(full: dict, lang: str) -> dict:
        """A change to a placed order, in the person's language, from the change's own figures: what the card and the chat say."""
        am, fi, what, done = (full.get("wholesale") or {}).get("amends") or {}, lang == "fi", [], full.get("status") == "completed"
        for ch in am.get("changes") or []:
            if ch.get("type") == "cancelled":
                what.append(("Mitään ei toimiteta." if done else "Koko tilaus perutaan, mitään ei toimiteta.") if fi else ("Nothing is delivered." if done else "The whole order is cancelled and nothing is delivered."))
            elif ch.get("type") == "delivery_date":
                what.append(f"Koko tilaus tulee {day_text(ch.get('to'), lang)} eikä {day_text(ch.get('from'), lang)}." if fi else f"The whole order arrives on {day_text(ch.get('to'), lang)} instead of {day_text(ch.get('from'), lang)}.")
            elif ch.get("type") == "line_quantity":
                name, u = (fi_name(ch.get("product"), ch.get("title")) if fi else ch.get("title")), {"L": "l", "pcs": "kpl" if fi else "pcs"}.get(str(ch.get("unit")), str(ch.get("unit") or "").lower())
                n = lambda x: f"{float(x or 0):,.2f}".rstrip("0").rstrip(".").replace(",", "\u00a0" if fi else ",")
                if ch.get("status") == "short":
                    what.append(f"{name}: toimitetaan {n(ch.get('to'))} / {n(ch.get('from'))} {u}." if fi else f"{name}: {n(ch.get('to'))} of {n(ch.get('from'))} {u} are delivered.")
                elif ch.get("status") == "recalled":
                    what.append(f"{name}: takaisinvedetty erä poistuu tilauksesta." if fi else f"{name}: the recalled lot leaves the order.")
                else:
                    what.append(f"{name}: riviä ei toimiteta." if fi else f"{name}: the line is not delivered.")
        new = [f"{float((l.get('wholesale') or {}).get('units') or l.get('quantity') or 0):g} × {fi_name(l['item']['id'], l['item'].get('title')) if fi else l['item'].get('title')}" for l in full.get("line_items") or []]
        if new:
            what.append(("Tilalle tulee: " if fi else "In its place: ") + ", ".join(new) + ".")
        credit = int(am.get("credit") or 0)
        if credit:
            what.append(f"Hyvitys {money_text(credit, lang)} (alv 0 %)." if fi else f"Credit {money_text(credit, lang)} ex VAT.")
        return {"order": am.get("order_label"), "option": am.get("option"), "kind": am.get("kind"), "cancel": any(x.get("type") == "cancelled" for x in am.get("changes") or []),
                "what": what, "credit": credit, "credit_total": credit + int(am.get("credit_tax") or 0), "goods": bool(new)}

    def snap(cart: dict | None, lang: str) -> dict:
        """What a cart holds, line by line: for saying in code what a change changed."""
        out = {}
        nm = lambda l: (l.get("title_fi") if lang == "fi" else None) or l.get("title") or ""
        for it in (cart or {}).get("items") or []:
            if it.get("dropped") or not it.get("choices"):
                continue
            ch = it["choices"][it.get("chosen") or 0]
            ls = ch["lines"]
            # asked in pieces, sold by weight or volume: the reply says what the cart holds ("2,5 kg"), not the pieces that were asked
            by_weight = len(ls) == 1 and it.get("unit") == "pcs" and str(ls[0].get("unit") or "").upper() in ("KG", "L")
            # asked in pieces, sold in packs: "15 pulloa" in cases of six is 18 bottles on the cart, and the reply says 18
            in_packs = not by_weight and it.get("unit") == "pcs" and it.get("status") == "ok" and ch.get("covers") and it.get("quantity") and float(ch["covers"]) > float(it["quantity"]) + 1e-6
            out[("i", it.get("ref"))] = {"ids": tuple(str(l["id"]) for l in ls), "name": " + ".join(str(nm(l)) for l in ls),
                                         "amount": amount_text(ls[0].get("units"), str(ls[0].get("unit")).upper(), lang) if by_weight else amount_text(ch["covers"], "pcs", lang) if in_packs else amount_text(it.get("quantity"), it.get("unit"), lang), "units": tuple(l.get("units") for l in ls),
                                         "state": it.get("status"), "first": str(nm(ls[0])), "got": amount_text(ls[0].get("covers") if it.get("status") == "replaced" else ch.get("covers"), it.get("unit"), lang),
                                         "sub": str(nm(ch["substitute"])) if ch.get("substitute") else "", "miss": it.get("named_miss")}
        for l in (cart or {}).get("fixed") or []:
            if not l.get("dropped"):
                pid = str(l["item"]["id"])
                d_, c_, q_ = g.cat.get(pid) or {}, holds(cart, pid), l.get("quantity")
                n_ = float((q_.get("total") if isinstance(q_, dict) else q_) or 0) / 10 ** (d_.get("scale") or 0)
                held = amount_text(n_, "KG", lang) if d_.get("unit") == "KG" else amount_text(round(n_ * c_["kg"], 2), "KG", lang) if c_.get("kg") else amount_text(round(n_ * c_["l"], 2), "L", lang) if c_.get("l") else ""
                out[("f", pid)] = {"ids": (pid,), "name": name_of(pid, (g.cat.get(pid) or {}).get("title"), lang), "amount": "", "held": held, "units": (json.dumps(l.get("quantity"), sort_keys=True),)}
        return out

    def some(names: list, lang: str, n: int = 4) -> str:
        names = [str(x) for x in names if x]
        return ", ".join(names[:n]) + (" " + W[lang]["chg_more"].format(n=len(names) - n) if len(names) > n else "")

    def change_text(T: Turn, before: dict, cart: dict, day0: str, nomatch: list, ok: bool) -> str:
        """What a change to a cart did, said by the shop from the cart's lines before and after. Never a model's account of it."""
        L, lang = W[T.lang], T.lang
        if not ok:
            return L["chg_fail"]
        after = snap(cart, lang)
        withq = lambda x: x["name"] + (f" ({x['amount']})" if x["amount"] else "")
        changed = lambda k: k not in before or before[k]["ids"] != after[k]["ids"] or before[k]["units"] != after[k]["units"] or before[k]["amount"] != after[k]["amount"]
        # what could not be done as asked comes first, each in its own sentence: a product that is short, and what stands for the rest
        special = {k: (L["chg_repl"].format(a=x["first"], got=x["got"], asked=x["amount"], sub=x["sub"]) if x.get("state") == "replaced" else L["chg_short"].format(a=x["name"], got=x["got"], asked=x["amount"]))
                   for k, x in after.items() if x.get("state") in ("replaced", "short") and changed(k)}
        both = [k for k in after if k in before and k not in special]
        # A typed amount that took the place of the menu's own line of the same product is a new amount of it, not a line added and a line removed.
        took = {k: next((b for b in before if b not in after and b[0] == "f" and before[b]["ids"] == after[k]["ids"]), None) for k in after if k not in before and k[0] == "i"}
        took = {k: b for k, b in took.items() if b}
        added, removed = [withq(after[k]) for k in after if k not in before and k not in special and k not in took], [before[k]["name"] for k in before if k not in after and k not in took.values()]
        swapped = [f"{before[k]['name']} → {withq(after[k])}" for k in both if before[k]["ids"] != after[k]["ids"]]
        amount = [f"{after[k]['name']} {before[k]['amount']} → {after[k]['amount']}" if before[k]["amount"] and after[k]["amount"] and before[k]["amount"] != after[k]["amount"] else withq(after[k])
                  for k in both if before[k]["ids"] == after[k]["ids"] and (before[k]["units"] != after[k]["units"] or before[k]["amount"] != after[k]["amount"])]
        amount += [f"{after[k]['name']} {before[b].get('held') or ''} → {after[k]['amount']}".replace("  ", " ") for k, b in took.items() if k not in special]
        out = list(special.values()) + [L[key].format(x=some(v, lang)) for key, v in (("chg_added", added), ("chg_removed", removed), ("chg_amount", amount), ("chg_swapped", swapped)) if v]
        out += [L["chg_miss"].format(n=x["miss"], got=x["name"]) for k, x in after.items() if x.get("miss") and changed(k)]
        if cart["day"] != day0:
            out.append(L["chg_day"].format(x=day_text(cart["day"], lang)))
        out += [L["chg_nomatch"].format(x=w) for w in nomatch[:2]]
        for it in cart["items"]:  # asked for in this turn and not on the cart: said with the reason, never passed over
            ms = it.get("_miss") or {}
            if ms.get("t") == T.turn:
                out.append(L["chg_keep"].format(a=it.get("as_written"), asked=amount_text(ms.get("quantity"), ms.get("unit"), lang), day=day_text(cart["day"], lang), now=amount_text(it.get("quantity"), it.get("unit"), lang)))
            if it.get("_t") != T.turn or it.get("choices") or it.get("dropped"):
                continue
            st = it.get("status")
            key = "off_kind" if st == "which_kind" else "off_usual" if st == "ask_amount" and it.get("usual_amount") else "off_amount" if st == "ask_amount" else "off_day" if st == "not_on_day" else "off_excl" if it.get("no_fit") else "off_never" if it.get("never") else "off_much" if it.get("too_much") else "off_none"
            if it.get("strict"):
                key, nm_ = "off_strict_none" if it.get("named_none") else "off_strict_never" if it.get("never") else "off_strict", (it.get("named_off") or {}).get("title_fi" if lang == "fi" else "title") or (it.get("named_off") or {}).get("title") or it.get("named_none") or f"”{it.get('as_written')}”"
                out.append(L[key].format(a=nm_, day=day_text(cart["day"], lang)) + (" " + L["off_near"] if it.get("near") and it.get("kinds") else ""))
                continue
            out.append(L[key].format(a=it.get("as_written"), day=day_text(cart["day"], lang), n=f"{it.get('too_much') or 0:g}", x=", ".join(EXCL_WORD[lang].get(k, k) for k in cart.get("stated") or []))
                       + (" " + L["off_near"] if it.get("near") and it.get("kinds") else ""))
            if (it.get("_stays") or {}).get("t") == T.turn:
                out.append(L["chg_stays"].format(x=it["_stays"]["x"], q=it["_stays"]["q"]))
        for it in cart["items"]:
            bp = it.get("budget_pick") or {}
            if bp.get("t") == T.turn and it.get("choices") and not it.get("dropped"):
                ch_ = it["choices"][it.get("chosen") or 0]
                out.append(L["chg_budget"].format(a=name_of(str(ch_["lines"][0]["id"]), ch_["lines"][0].get("title"), lang), s=money_text(ch_.get("total"), lang), b=name_of(str(bp["from"]["id"]), bp["from"].get("title"), lang), t=money_text(bp.get("from_total"), lang),
                                                  u=("" if not bp.get("usual") else "Useimmin tilaamasi " if lang == "fi" else "The one you order most often, ")))
        early = T.rec.pop("early", None)
        if early:
            out.insert(0, L["chg_early"].format(a=day_text(early, lang), day=day_text(cart["day"], lang)))
        if not out:
            out.append(L["chg_none"])
            T.rec["nochange"] = True
        nxt = {"ready_for_complete": "next", "requires_escalation": "next_esc", "canceled": "empty", "incomplete": "next_stop"}.get(T.state or "")
        return " ".join(out + ([L[nxt]] if nxt else []))

    def lines_of(full: dict | None, lang: str) -> dict:
        return {str(l["item"]["id"]): {"name": name_of(l["item"]["id"], l["item"].get("title"), lang), "units": (l.get("wholesale") or {}).get("units")} for l in (full or {}).get("line_items") or []}

    def replace_text(T: Turn, before: dict, after: dict) -> str:
        """A new cart took an earlier one's place: what is different between the two, by product, said by the shop."""
        L, lang = W[T.lang], T.lang
        gone, fresh = [before[k]["name"] for k in before if k not in after], [after[k]["name"] for k in after if k not in before]
        n = sum(1 for k in after if k in before and before[k]["units"] != after[k]["units"])
        out = [L["chg_new"]] + ([L["chg_gone"].format(x=some(gone, lang, 8))] if gone else []) + ([L["chg_fresh"].format(x=some(fresh, lang, 8))] if fresh else []) + ([L["chg_amounts"].format(n=n)] if n else [])
        return " ".join(out + ([] if gone or fresh or n else [L["chg_same"]]))

    def blocked_of(full: dict, lines: list[dict]) -> list[dict]:
        """The lines that stop a cart, by product and in figures, for the card to say in the person's language: the door's own message is English."""
        out = []
        for m in full.get("messages") or []:
            at = re.match(r"\$\.line_items\[(\d+)\]", str(m.get("path") or ""))
            if m.get("type") != "error" or not at or int(at.group(1)) >= len(lines):
                continue
            pid, w = str(lines[int(at.group(1))]["item"]["id"]), m.get("wholesale") or {}
            d = g.cat.get(pid) or {}
            if any(x["id"] == pid for x in out):
                continue
            same = [float(x.get("quantity") or 0) / 10 ** (d.get("scale") or 0) for x in lines if str(x["item"]["id"]) == pid and isinstance(x.get("quantity"), (int, float))]
            if m.get("code") == "insufficient_stock" and len(same) > 1 and w.get("requested") is not None:  # the product is on two lines: said as the whole of it against what can come
                w = {"can_supply": round((w.get("can_supply") or 0) + sum(same) - w["requested"], 2), "requested": round(sum(same), 2)}
            out.append({"code": m.get("code"), "id": pid, "title": d.get("title"), "title_fi": fi_name(pid, d.get("title")), "can": w.get("can_supply"), "asked": w.get("requested"), "unit": UNITS.get(d.get("unit"), (0, str(d.get("unit") or "").lower()))[1]})
        return out[:6]

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
            elif any(x.get("status") in ("which_kind", "not_on_day") or x.get("kinds") or (x.get("status") == "ask_amount" and x.get("usual_amount")) for x in cart["items"]):
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
        cart["id"], cart["blocked"] = full["id"], blocked_of(full, lines)
        g.store.put("shop_cart:" + full["id"], cart)
        (res.setdefault("_meta", {})).setdefault(META, {})["cart"] = cart_public(cart)
        T.show(name, shown_args, res, int((time.time() - started) * 1000))
        T.rec["cart"], T.state = full["id"], full.get("status")
        return cart_read(cart, full, T.lang), full

    FI_KIND = {fold(v): k for k, v in {**ITEM_FI, **KIND_FI}.items()}

    def kind_by_word(text: str) -> str | None:
        """The kind a Finnish word names, by the shop's own word list, whatever its case ending: "3 kg porkkanaa" is carrot, "voita" is butter."""
        ws = re.findall(r"[a-zäöå]+", str(text or "").lower())
        for w in [" ".join(ws[i:i + 2]) for i in range(len(ws) - 1)] + ws:
            for base in (w, w[:-1], w[:-2], w[:-3]):
                if len(base) >= 3 and fold(base) in FI_KIND:
                    return FI_KIND[fold(base)]
        return None

    def own_word(x: dict) -> bool:
        """True when an item's query is missing, or is the person's own word for it and not the kind's plain English name ("porkkanaa", "voita"):
        the range is searched by kind, and a Finnish word in its partitive finds baby food and croissants."""
        q = str(x.get("query") or "").strip()
        if not q:
            return bool(str(x.get("as_written") or "").strip())
        if x.get("preferred_product") or x.get("line"):
            return False
        try:
            how = g.cat.pick(q, {})[3].get("how")
        except Exception:
            return False
        words = lambda t: set(re.findall(r"[a-zäöå]{4,}", str(t or "").lower()))
        return how in ("nearest", "none") and bool(words(q) & words(x.get("as_written")))

    def holds(cart: dict, pid) -> dict:
        """What one sales unit of a product holds, in kg or l: the catalogue's figure, or (a punnet has none there) what the menu that put it on the cart counted with."""
        c = dict((g.cat.get(str(pid)) or {}).get("content") or {})
        for d_ in ((cart.get("menu") or {}).get("by_dish") or []):
            for l in d_.get("lines") or []:
                key = {"g": "kg", "ml": "l"}.get(str(l.get("unit")))
                if str(l.get("id")) == str(pid) and key and l.get("per") and not c.get(key):
                    c[key] = float(l["per"]) / 1000
        return c

    def menu_lines_of(cart: dict, query: str, any_size: bool = False) -> tuple[list[dict], float, str]:
        """The lines a menu put on the cart for one kind of product, with the amount they hold: (lines, amount, KG or L). Lines bought for a dietary share are not among them."""
        try:
            kinds_ = set(g.cat.pick(query, {})[3].get("kinds") or [])
        except Exception:
            kinds_ = set()
        got = [(l, g.cat.get(str(l["item"]["id"])) or {}) for l in cart.get("fixed") or [] if not l.get("dropped") and not (l.get("wholesale") or {}).get("requirements") and not (l.get("wholesale") or {}).get("substitutes_for")]
        got = [(l, holds(cart, l["item"]["id"])) for l, d in got if kinds_ and kind(d) in kinds_]
        if any_size:
            return [l for l, _ in got], 0.0, ""
        for key, unit in (("kg", "KG"), ("l", "L")):
            if got and all(c.get(key) for _, c in got):
                return [l for l, _ in got], round(sum(float(l.get("quantity") or 0) * c[key] for l, c in got), 3), unit
        return [], 0.0, ""

    async def tool_build_cart(T: Turn, args: dict) -> str:
        started = time.time()
        for x in args.get("items") or []:
            if isinstance(x, dict) and own_word(x):  # the shop's own word list settles the common ones without asking the model again
                k = kind_by_word(x.get("as_written") or x.get("query"))
                if k:
                    x["as_written"], x["query"] = str(x.get("as_written") or x.get("query")), k
        vague = [x for x in args.get("items") or [] if isinstance(x, dict) and own_word(x)]
        if vague and not T.rec.get("asked_plain"):
            T.rec["asked_plain"] = True  # asked once in a turn; the second answer is taken as it comes
            raise Stop("Nothing was built yet. Every item needs query = the kind of product in plain English (carrot, butter, minced beef, macaroni), next to as_written = the person's own words. "
                       "Fix: " + "; ".join(f"'{str(x.get('as_written') or x.get('query'))[:60]}'" for x in vague[:8]) + ". Call build_cart again with every item of this request.")
        asked_day = str(args.get("delivery_date") or "").strip() or None
        old = cart_of(args.get("add_to")) if args.get("add_to") else None
        if args.get("add_to") and not old:
            raise Stop("There is no cart with that id. Build a new one without add_to.")
        if old:
            cur = await asyncio.to_thread(g.get_checkout, old["id"], T.p)
            if cur.get("status") in ("completed", "canceled"):
                raise Stop(f"That cart is already {cur['status']}. Build a new one without add_to.")
        first = g._day(None)
        before, day0 = (snap(old, T.lang), old["day"]) if old else (None, None)
        if old:
            cart = old
            early = g._day(asked_day) if asked_day and g._day(asked_day) < first else None
            if early:  # "jo tänään": a day before the first one that can be delivered. The shop says so; the question is never passed over
                T.rec["early"] = early
            if early and cart["day"] == first:
                cart.update({"asked_day": early, "moved": {"from": early, "to": first}})  # the cart already stands on the first day: nothing to plan again
            elif asked_day and g._day(asked_day) != cart["day"]:  # a new day: everything on the cart is planned again for it
                cart.update({"asked_day": g._day(asked_day), "day": max(g._day(asked_day), first), "moved": {"from": early, "to": first} if early else None})
                again = [norm(x) | {"usual": x.get("usual"), "ref": x.get("ref")} for x in cart["items"] if not x.get("dropped") and x.get("status") not in ("which_kind", "ask_amount")]
                cart["items"] = []
                await plan_cart(cart, again, T.p, T.tr)
        else:
            day = g._day(asked_day)
            cart = {"id": None, "day": max(day, first), "asked_day": day if asked_day else None, "moved": {"from": day, "to": first} if day < first else None, "items": [], "thread": T.th["id"]}
        cart["intent"] = (cart["intent"] if old and cart.get("intent") and cart["intent"].endswith(T.rec["text"]) else (cart["intent"] + " / " + T.rec["text"]) if old and cart.get("intent") else words_since_cart(T.th, T.turn))[:1000]  # the person's own words, never a model's
        cart["title"] = cart.get("title") or title_of(cart["intent"])
        cart["budget"] = budget_of(cart["intent"])
        stated_now = apply_stated(T, cart)
        items = [norm(x) for x in args.get("items") or [] if isinstance(x, dict)]
        items = [x | {"_t": T.turn} for x in items if x["query"]][:40]
        said_ = str(T.rec.get("text") or "").replace(",", ".")
        for x in items:  # "15 pulloa" is fifteen bottles whether or not the model kept the word with the item
            if str(x.get("unit") or "").lower() == "pcs" and x["quantity"] and re.search(rf"(?<![\d.]){x['quantity']:g}\s*(?:pullo|tölk|bottle|can)", said_, re.I):
                x["by_bottle"] = True
        by_ref = {y["ref"]: y for y in refs(cart)["items"] if not y.get("dropped")} if old else {}
        for x in items:
            y = by_ref.get(x.get("line"))
            if not y and old:  # the same kind asked for again without its line's number: it is that line, not a second one
                y = next((z for z in by_ref.values() if z.get("choices") and str(z.get("query") or "").lower() == x["query"].lower()
                          and (not x["preferred_product"] or not z.get("preferred_product") or x["preferred_product"] == z["preferred_product"]) and (x["claims"] or []) == (z.get("claims") or [])), None)
                if y:
                    x["line"] = y["ref"]
                    if x["quantity"] and y.get("quantity") and (x["unit"] or y.get("unit")) == y.get("unit") and MORE.search(str(T.rec.get("text") or "")):
                        x["quantity"] += y["quantity"]  # "lisää vielä 40 kg": more of what the cart already has
            if not y and old:  # what the menu could not get, answered in words and not by a tap: the line takes the gap's place and, with no amount said, its amount
                try:
                    ks = {str(k).lower() for k in g.cat.pick(x["query"], {})[3].get("kinds") or []}
                except Exception:
                    ks = set()
                y = next((z for z in by_ref.values() if z.get("gap") and not z.get("choices") and ks & {str(k.get("kind")).lower() for k in z.get("kinds") or []}), None)
                if y:
                    x["line"], x["for_gap"] = y["ref"], y["gap"]
            if not y:
                x.pop("line", None)
                if old and not x["quantity"] and not x.get("use_usual"):
                    # Another pack of something a menu put on the cart ("vaihda makaroni 10 kg säkkeihin"): the same amount, asked for in the pack named.
                    # The menu's own lines give way only when the new line is on the cart.
                    took, amount, unit_ = menu_lines_of(cart, x["query"])
                    if took and amount > 0:
                        x.update({"quantity": amount, "unit": unit_, "_takes": [str(l["item"]["id"]) for l in took]})
                elif old and x["quantity"] and not x.get("use_usual") and not MORE.search(str(T.rec.get("text") or "")) and not ALSO.search(str(T.rec.get("text") or "")):
                    # "Ota lohta 30 kg" on a menu's cart is the amount of the menu's salmon, not 30 kg on top of its 20: the one product the menu
                    # put there for that kind gives way when the new line is on the cart. "Lisää 5 kg" stays an addition.
                    took = menu_lines_of(cart, x["query"], True)[0]
                    if len({str(l["item"]["id"]) for l in took}) == 1:
                        x["_takes"] = [str(l["item"]["id"]) for l in took]
                        if not x["preferred_product"] and not x["claims"]:
                            x["preferred_product"] = x["_takes"][0]  # the product the menu chose stays the product; only its amount is new
                continue
            said_now = str(T.rec.get("text") or "")
            if x["quantity"] and y.get("quantity") and NO_SUBS.search(fold(said_now)) and not re.search(r"\d", said_now):
                x["quantity"], x["unit"] = y["quantity"], y.get("unit")  # "ei korvaavia tuotteita" says nothing about amounts: what was asked stays asked, and the card says how much of it can come
            if x["query"].lower() == str(y.get("query") or "").lower():  # the same product, another amount
                # The product on the line stays the product: a new amount never changes the brand, the pack or fresh to frozen without saying so.
                # When there is not enough of it, the planner offers a replacement, and the card shows that as a replacement with its reason.
                on_line = str(y["choices"][y.get("chosen") or 0]["lines"][0]["id"]) if y.get("choices") else None
                now_ = x["quantity"] and x["quantity"] != y.get("quantity") and re.search(r"\d", str(x.get("as_written") or ""))  # a new amount in the person's own words: the card quotes those, not the amount it took the place of
                x.update({"query": y["query"], "as_written": (x["as_written"] if now_ else None) or y.get("as_written") or x["as_written"], "preferred_product": x["preferred_product"] or y.get("preferred_product") or on_line, "claims": x["claims"] or y.get("claims") or []})
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
        gone, nomatch, took_i, took_f = [], [], [], []
        said_as = {}  # a line that came from a menu is also known by its ingredient's name ("wheat flour", "vehnäjauho"), not only by the product's
        for d_ in ((cart.get("menu") or {}).get("by_dish") or []):
            for l in d_.get("lines") or []:
                said_as[str(l.get("id"))] = said_as.get(str(l.get("id")), "") + f" {l.get('item') or ''} {l.get('fi') or ''}"
        for word in [str(x).lower().strip() for x in args.get("remove") or [] if str(x).strip()]:
            n0 = len(gone)
            names_of = lambda it: " ".join([str(it.get("as_written")), str(it.get("query"))] + [l.get("title", "") + " " + l.get("title_fi", "") for c in it.get("choices") or [] for l in c["lines"]]).lower()
            hay_of = lambda l: (str(l.get("_title", "")) + said_as.get(str(l["item"]["id"]), "")).lower()
            live_items, live_fixed = [it for it in cart["items"] if not it.get("dropped")], [l for l in cart.get("fixed") or [] if not l.get("dropped")]
            if word.isdigit():
                hit_i, hit_f = [it for it in live_items if it.get("ref") == int(word)], []
            else:  # a word of its own first; a word inside a longer one only when nothing carries it alone
                hit_i, hit_f = [it for it in live_items if word_hit(word, names_of(it), True)], [l for l in live_fixed if word_hit(word, hay_of(l), True)]
                if not hit_i and not hit_f:
                    hit_i, hit_f = [it for it in live_items if word_hit(word, names_of(it))], [l for l in live_fixed if word_hit(word, hay_of(l))]
            for it in hit_i:
                it["dropped"] = True
                gone.append(it.get("as_written"))
                took_i.append(it)
            for l in hit_f:
                l["dropped"] = True
                gone.append(l.get("_title"))
                took_f.append(l)
            if len(gone) == n0:
                nomatch.append(word)
        # "Vaihda vehnäjauho gluteenittomaan jauhoon": one line goes and one product comes with no amount said. The new one takes the amount of the line it replaces.
        bare = [x for x in items if not x["quantity"] and not x.get("use_usual") and not x.get("line") and not x.get("_takes")]
        if old and len(bare) == 1 and len(took_i) + len(took_f) == 1:
            if took_i and took_i[0].get("quantity"):
                bare[0].update({"quantity": took_i[0]["quantity"], "unit": took_i[0].get("unit") or ""})
            for l in took_f:
                held = holds(cart, l["item"]["id"])
                key = next((k for k in ("kg", "l") if held.get(k)), None)
                if key and float(l.get("quantity") or 0) > 0:
                    bare[0].update({"quantity": round(float(l["quantity"]) * held[key], 3), "unit": key.upper()})
        if not items and not gone and not (old and asked_day) and not (old and stated_now):
            if old and nomatch:
                T.rec["fixed_answer"] = change_text(T, before, cart, day0, nomatch, True)
                return "Nothing was removed: no line of the cart matched those words. The shop asks the person which line they mean."
            return "Nothing to build: pass the items the person asked for."
        cands = [] if old else await open_carts(T, str(args.get("replaces") or ""))
        hold_state(cands, "held", "paused")  # planned as if the earlier open cart were not there: this one is about to take its place
        try:
            if items:
                await plan_cart(cart, items, T.p, T.tr)
                fit_budget(cart, T.turn)
                for it in cart["items"]:
                    ids = it.pop("_takes", None)
                    if ids and it.get("choices") and not it.get("dropped"):
                        for l in cart.get("fixed") or []:
                            if str(l["item"]["id"]) in ids and not l.get("dropped"):
                                l["dropped"] = True
                                gone.append(l.get("_title"))
                    elif ids and not it.get("dropped"):  # it cannot come: the menu's own lines stay, and the shop says so in the same breath
                        kept = [l for l in cart.get("fixed") or [] if str(l["item"]["id"]) in ids and not l.get("dropped")]
                        if kept:
                            it["_stays"] = {"t": T.turn, "x": ", ".join(name_of(str(l["item"]["id"]), (g.cat.get(str(l["item"]["id"])) or {}).get("title"), T.lang) for l in kept), "q": amount_text(it.get("quantity"), it.get("unit"), T.lang)}
                # What the menu could not get is on the cart now in another form the person named (a near kind): the open question about it is answered.
                new_kinds = {str(kind(g.cat.get(str(l["id"])) or {})).lower() for x in cart["items"] if x.get("_t") == T.turn and x.get("choices") and not x.get("dropped") for l in x["choices"][x.get("chosen") or 0]["lines"]}
                for gp in cart["items"]:
                    if gp.get("gap") and not gp.get("choices") and not gp.get("dropped") and new_kinds & {str(k.get("kind")).lower() for k in gp.get("kinds") or []}:
                        gp["dropped"] = True
        finally:
            if cands and not cart_lines(cart):
                hold_state(cands, "paused", "held")  # nothing to put in its place: the earlier cart stands
                cands = []
        try:
            prev = await asyncio.to_thread(g.get_checkout, cands[-1], T.p) if cands else None
        except GatewayError:
            prev = None
        gone_carts = await withdraw(T, cands)
        if gone_carts and gone_carts[-1].get("intent"):
            cart["intent"] = (gone_carts[-1]["intent"] + " / " + cart["intent"])[:1000]  # the whole request, in the person's words: what they first asked and what they changed
        read, full = await put_cart(T, cart, started)
        read += replaced_read(gone_carts)
        if args.get("remove"):
            read += "\nRemoved from the cart: " + (", ".join(str(x) for x in gone) if gone else "nothing matched those words; ask which line they mean.")
        if old:  # a change to a cart is told by the shop, from the lines before and after
            T.rec["fixed_answer"] = change_text(T, before, cart, day0, nomatch, not read.startswith("The cart could not be built"))
            read += "\nThe shop tells the person in its own words exactly what this change did, from the cart's lines before and after. Your answer is not shown for this turn."
        elif gone_carts and full:
            T.rec["lead_answer"] = replace_text(T, lines_of(prev, T.lang), lines_of(full, T.lang))
            read += "\nThe shop itself says first what differs from the earlier cart. Do not say what was added, removed or changed."
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

    def switched_off(full: dict | None) -> bool:
        """Valio Aimo's own switch for this customer number refused the cart (rules W1, W2, W4)."""
        return any(r.get("id") in ("W1", "W2", "W4") and r.get("outcome") != "pass" for r in (((full or {}).get("wholesale") or {}).get("policy") or {}).get("rules") or [])

    async def confirm(T: Turn, cid: str, by_button: bool, read_labels: bool = False) -> tuple[str, str | None]:
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
                return "The hold had expired and the fresh cart differs or cannot be ordered alone. " + read, L["stopped" if switched_off(cur) else "changed"]
        if cur.get("status") == "requires_escalation":
            return "The cart still waits for an approver; nothing was ordered.", L["waits"]
        am = (cur.get("wholesale") or {}).get("amends")  # a change to a placed order, not a cart
        if cur.get("status") == "completed" and am:
            a = amend_public(cur, T.lang)
            return f"The change to order {a['order']} is already confirmed.", L["cancel_placed" if a["cancel"] else "amend_placed"].format(label=a["order"], what=" ".join(a["what"]))
        if cur.get("status") == "completed":
            return f"Already confirmed as order {(cur.get('order') or {}).get('label')}.", L["placed"].format(label=(cur.get("order") or {}).get("label"), day=day_text((cur.get("wholesale") or {}).get("delivery_date"), T.lang))
        if cur.get("status") != "ready_for_complete":
            why = " ".join(m.get("content", "") for m in cur.get("messages") or [] if m.get("type") == "error")[:300]
            return f"The cart is {cur.get('status')} and cannot be confirmed. {why}", L["not_placed"].format(why="") .strip() if T.lang == "fi" else L["not_placed"].format(why=why)
        blank = [u for r in ((cur.get("wholesale") or {}).get("policy") or {}).get("rules") or [] if r.get("id") == "A1" and r.get("outcome") == "pass" for u in r.get("undeclared") or []]
        blank = list({u["id"]: u for u in blank}.values())
        if blank and not read_labels:  # an exclusion is in force and these products say nothing about allergens: the person ticks that they read the labels, or nothing is ordered
            return f"{len(blank)} products have no allergen declaration and the person has not ticked on the card that they read the labels; nothing was ordered.", L["und_tick"].format(n=len(blank))
        if blank:
            g.store.log(T.tr, f"human:{T.p.get('buyer_name')} (in the shop)", "labels_read", f"The person in the shop ticked on the cart that they had read the labels of {len(blank)} product{'s' if len(blank) != 1 else ''} whose manufacturer declared no allergens: "
                        + "; ".join(f"{u['title']} ({u.get('what')})" for u in blank[:12]) + (f" and {len(blank) - 12} more" if len(blank) > 12 else "") + ". The rule engine could not check them; the tick is this person's word.",
                        {"checkout": cid, "products": [u["id"] for u in blank]}, cid=str(cid), acct=T.p.get("customer"))
        g.store.log(T.tr, f"human:{T.p.get('buyer_name')} (in the shop)", "confirmed", "The person in the shop " + ((f"pressed the confirm button on the card that changes order {am.get('order_label')}" if by_button else f"told the assistant to confirm the change to order {am.get('order_label')} they had seen") if am else
                                                                                                                         ("pressed Vahvista tilaus on the cart" if by_button else "told the assistant to confirm the cart they had seen"))
                    + (" in the shop chat. The shop assistant makes the change on that word, signing the exact offer with its own key." if am else " in the shop chat. The shop assistant places the order on that word, signing the exact offer with its own key."), {"checkout": cid, "by": "button" if by_button else "typed"}, cid=str(cid), acct=T.p.get("customer"))
        args = {"id": str(cid), "checkout_mandate": g.agent_proof(cur, T.mandate)}
        res, ms = await T.call("complete_checkout", args)
        full = full_of(res)
        if cart:
            (res.setdefault("_meta", {})).setdefault(META, {})["cart"] = cart_public(cart)
        if am and is_checkout(full):
            (res.setdefault("_meta", {})).setdefault(META, {})["amend"] = amend_public(full, T.lang)
        T.show("complete_checkout", {"id": str(cid)}, res, ms)
        T.state = full.get("status")
        if full.get("status") == "completed" and am:
            a = amend_public(full, T.lang)
            return f"The change to order {a['order']} is confirmed and signed: {am.get('title')}. {am.get('effect')}", L["cancel_placed" if a["cancel"] else "amend_placed"].format(label=a["order"], what=" ".join(a["what"]))
        if full.get("status") == "completed":
            label, day = (full.get("order") or {}).get("label"), (full.get("wholesale") or {}).get("delivery_date")
            return f"Order {label} is confirmed, {money_text(_amount(full.get('totals')), T.lang)} ex VAT, delivery {day}.", L["placed"].format(label=label, day=day_text(day, T.lang))
        if switched_off(full):
            return "Not ordered: Valio Aimo's switch for this customer number refuses the assistant's orders.", L["stopped"]
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
                              "whole": bool(x.get("dish_refused")), "diet": [str(k) for k in x.get("diet") or []], "amount": x.get("amount"), "need": x.get("need"), "unit": x.get("unit"), "rules": x.get("rules") or []} for x in plan.get("not_ordered") or []],
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
            short = [x for x in m["not_ordered"] if x.get("kind") == "short" and x not in part]
            other = [x for x in m["not_ordered"] if x not in part and x not in short]
            for x in short[:4]:  # the product is on the cart, only not all of it
                tell.append(f"{x['item']} ({x.get('dish')}): the cart has what Valio Aimo can deliver that day, and {x.get('amount')} of what the menu needs is not covered. {x.get('why') or ''} Say it is short by that much; never say it is not on the cart.")
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
        stated = stated_exclusions(typed_words(T))
        if stated:  # an allergy the person wrote is on the plan whether or not the model passed it on
            notes = args.get("notes") if isinstance(args.get("notes"), list) else [args["notes"]] if args.get("notes") else []
            have = menu._kinds(" / ".join(str(n) for n in notes))
            args["notes"] = [str(n) for n in notes] + [f"{menu.DIET_WORD[k]} kitchen" for k in stated if k not in have]
        started = time.time()
        cands = await open_carts(T, str(args.pop("replaces", None) or ""), str(args.get("event_date") or ""))
        try:
            prev = await asyncio.to_thread(g.get_checkout, cands[-1], T.p) if cands else None
        except GatewayError:
            prev = None
        hold_state(cands, "held", "paused")  # planned as if the earlier open cart were not there: this one is about to take its place
        ready = False
        try:  # the products of the cart this one replaces stay where they still fit: the cook has already seen them
            res, ms = await T.call("plan_menu_order", {**args, "keep_products": [str(l["item"]["id"]) for l in (prev or {}).get("line_items") or []]} if prev else args)
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
        # What the menu could not get (the cut of meat the recipe names is out of stock): asked once more as a line of its own, so the cart has the range's
        # own replacement with its reason, or the card offers the nearest kinds that can come that day with the amount already worked out. Never for
        # portions a dietary note holds, and at most three: each is one availability request.
        cart["exclude_allergens"], cart["budget"] = ck.get("exclude_allergens") or [], budget_of(cart["intent"])
        # An allergy of some guests ("yksi vieras on maitoallerginen") is planned for those portions only: it is not a rule for the whole cart, and the card does not say it is.
        whole = [k for k in stated if set(menu.EXCODES.get(k) or ["?"]) <= set(cart["exclude_allergens"])]
        # "Koko keittiö on maidoton" is planned on every portion (milk has stand-ins, so it is a requirement on each line, not on the order): it is still the
        # whole kitchen's rule, so a line added to this cart later is held to it too. Only what the plan counted for some guests or one dish stays theirs.
        diet = plan.get("diet") or []
        every = {x.get("kind") for x in diet if x.get("scope") in ("all", "order")} - {x.get("kind") for x in diet if x.get("scope") in ("guests", "dish")}
        cart["per_guest"] = [k for k in stated if k not in whole and k not in every]
        gaps = [x for x in cart["menu"]["not_ordered"] if x.get("kind") in ("no_fit", "not_in_range") and x.get("need") and x.get("unit") in ("g", "ml")
                and not x.get("portions") and not x.get("whole") and not x.get("diet") and set(x.get("rules") or []) <= GAP_RULES][:3]
        if gaps:
            # What the cook already chose for such an ingredient on the cart this one replaces (a tap on "porsaan kylki") stays their choice, in the amount the menu needs now.
            picked = {y["for_gap"]: y for c_ in gone_carts[-1:] for y in (c_.get("items") or []) if y.get("for_gap") and y.get("choices") and not y.get("dropped")}
            asked = [norm({"query": picked[x["item"]]["query"] if x["item"] in picked else x["item"], "as_written": (x.get("fi") if T.lang == "fi" else None) or x["item"], "quantity": math.ceil(x["need"] / 100 - 1e-9) / 10,
                           "unit": "kg" if x["unit"] == "g" else "l", "own_pick": bool(x["item"] in picked and picked[x["item"]].get("own_pick"))})
                     | {"_t": T.turn} | ({"for_gap": x["item"]} if x["item"] in picked else {"gap": x["item"]}) for x in gaps]
            try:
                await plan_cart(cart, asked, T.p, T.tr)
            except Exception:
                cart["items"] = []
            for it in list(cart["items"]):
                x = next((x for x in gaps if x["item"] == (it.get("gap") or it.get("for_gap"))), None)
                if x and it.get("for_gap") and not it.get("choices"):  # the kind they chose cannot come in the new amount: the menu's own note stands again
                    cart["items"] = [y for y in cart["items"] if y is not it]
                    continue
                ids_ = [str(l["id"]) for l in it["choices"][it.get("chosen") or 0]["lines"]] if it.get("choices") else []
                milk = x and "lactose-free" in (x.get("rules") or []) and any({"AM", "ML"} & set((g.cat.get(i) or {}).get("contains") or []) and not any("LACTOSE" in str(c) for c in (g.cat.get(i) or {}).get("claims") or []) for i in ids_)
                # a kitchen-wide rule (gluten-free, nut-free): the product that stands in is read against the manufacturer's declaration, contains and may contain
                codes = set().union(*(ASK_CODES.get(RULE_KIND.get(r), set()) for r in (x.get("rules") or []) if r != "lactose-free")) if x else set()
                milk = milk or bool(codes and any(allergen_hits(codes, list((g.cat.get(i) or {}).get("contains") or []) + list((g.cat.get(i) or {}).get("may_contain") or []), (g.cat.get(i) or {}).get("claims")) for i in ids_))
                if not x or milk or not (it.get("choices") or (it.get("near") and it.get("kinds")) or it.get("status") == "which_kind"):
                    cart["items"] = [y for y in cart["items"] if y is not it]  # nothing to offer for it: the menu's own note stands
                else:
                    x["as_item"] = True  # the line says it now; the note "not on the cart" would say it twice
            done = {x["item"] for x in gaps if x.get("as_item")}
            if done:
                left_ = [x for x in cart["menu"]["not_ordered"] if not x.get("as_item") and x.get("kind") != "short" and not (x.get("portions") and not x.get("whole"))]
                cart["menu"]["tell"] = [t for t in cart["menu"]["tell"] if not t.startswith("Not on the cart, a person decides: ")] + (["Not on the cart, a person decides: " + "; ".join(f"{x['item']} ({x.get('why') or ''})" for x in left_[:6])] if left_ else [])
        lines = [{k: v for k, v in l.items() if k != "_title"} for l in cart_lines(cart)]
        cargs = {"line_items": lines, "delivery_date": ck["delivery_date"], "exclude_allergens": ck.get("exclude_allergens") or [], "intent": cart["intent"], "rationale": cart["rationale"]}
        res2, _ = await T.call("create_checkout", cargs)
        full = full_of(res2)
        if res2.get("isError") or not is_checkout(full):
            said = " ".join(m.get("content", "") for m in (full.get("messages") or [])) or "".join(x.get("text", "") for x in res2.get("content") or [])
            read = f"The menu was planned but its cart could not be built: {said[:600]}"
        else:
            cart["id"], cart["exclude_allergens"], cart["stated"] = full["id"], ck.get("exclude_allergens") or [], whole
            g.store.put("shop_cart:" + full["id"], cart)
            (res2.setdefault("_meta", {})).setdefault(META, {})["cart"] = cart_public(cart)
            T.show("create_checkout", {"menu": [d.get("name") for d in plan.get("dishes") or []], "guests": (plan.get("reading") or {}).get("guests"), "delivery_date": ck["delivery_date"]}, res2, int((time.time() - started) * 1000))
            T.rec["cart"], T.state = full["id"], full.get("status")
            read = f"The menu is planned: {len(plan.get('dishes') or [])} dishes for {(plan.get('reading') or {}).get('guests')} guests.\n" + cart_read(cart, full, T.lang) + replaced_read(gone_carts)
            if gone_carts:
                T.rec["lead_answer"] = replace_text(T, lines_of(prev, T.lang), lines_of(full, T.lang))
                g0, g1 = (gone_carts[-1].get("menu") or {}).get("guests"), (cart.get("menu") or {}).get("guests")
                if g0 and g1 and g0 != g1:  # the number of people is what changed every amount: said by its numbers
                    T.rec["lead_answer"] = T.rec["lead_answer"].replace(W[T.lang]["chg_new"], W[T.lang]["chg_new"] + " " + W[T.lang]["chg_guests"].format(n=g1, m=g0), 1)
                read += "\nThe shop itself says first what differs from the earlier cart. Do not say what was added, removed or changed; say only what the person must still know."
        T.step(c.name, shown, read)
        return {"id": c.id, "name": c.name, "content": read}

    async def turn_run(th, turn: int, lang: str, model_pick: str | None):
        T, used, chat_ = Turn(th, turn, lang), [], None
        rec = T.rec
        try:
            bad = bad_amount(rec.get("text"))
            part, rest = bad_part(rec.get("text"), bad) if bad else ("", "")
            if bad and re.search(r"\d", rest) and not bad_amount(rest):
                rec["skipped"], bad = part, None  # one line of a list: that line is left out and said, the rest of the message is built
            if bad:  # checked in code, before any model reads it: a negative or zero amount builds nothing
                text = W[lang]["bad_amount"].format(x=bad)
                rec["refused"] = True  # nothing was built from this message: it is not part of any later cart's request
                rec["answer"] = text
                T.step("check_amount", {"amount": bad}, "Refused: an amount has to be more than zero. No cart was built.")
                g.store.log(T.tr, f"agent:{th['slug']}/{AGENT}", "refused", f"The request asked for an amount that cannot be ordered ({bad}). The shop built no cart.", {"thread": th["id"], "turn": turn, "request": rec["text"][:1000]}, acct=T.p.get("customer"))
                emit(th, {"turn": turn, "type": "text", "text": text})
                emit(th, {"turn": turn, "type": "done", "ms": int((time.time() - T.t0) * 1000), "model": "none", "label": "no model", "first_card_ms": None, "models": []})
                return
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
                            if (full.get("wholesale") or {}).get("amends"):  # a change to a placed order: the card says so in the person's language
                                (res.setdefault("_meta", {})).setdefault(META, {})["amend"] = amend_public(full, lang)
                            T.state = full.get("status")
                            if name in ("answer_proposal", "resolve_order_issue", "cancel_order"):
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
                    lead = rec.pop("lead_answer", None)
                    if lead:  # a new cart took an earlier one's place: the shop says what differs, and no sentence of the model's about what changed is shown
                        kept = [x for x in re.split(r"(?<=[.!?])\s+", text) if x and not CHANGE_CLAIM.search(x) and not re.search(r"korvaa aiemman|replaces the earlier", x, re.I)]
                        text = (lead + " " + " ".join(kept)).strip()
                    facts = None
                    same = rec.pop("nochange", None)  # the model touched the cart and nothing on it changed
                    if (not rec.get("fixed_answer") and not rec.get("cart")) or same:  # a question about a product already on the cart
                        facts = await asyncio.to_thread(cart_allergens, T, str(rec.get("text") or ""))
                    if rec.get("fixed_answer") and lead:
                        # a change to the cart and then a new cart in its place, in one turn: both are said, the change first and what differs in the new cart after it
                        fa = rec["fixed_answer"]
                        tail = next((W[lang][k] for k in ("next", "next_esc", "next_stop", "empty") if W[lang].get(k) and fa.endswith(W[lang][k])), "")
                        rec["fixed_answer"] = " ".join(x for x in (fa[:len(fa) - len(tail)].strip(), lead, tail) if x)
                    if rec.get("fixed_answer") and not (same and facts is not None):  # a confirmation or a change to a cart is answered by code: what happened, never what a model thinks happened
                        text = rec.pop("fixed_answer")
                    elif facts is not None and (facts or same or ALLERGEN_CLAIM.search(text)):
                        # What suits whom is read from the manufacturer's declaration; the model's own sentences about it are not shown, also when the question named no product of the cart.
                        rec.pop("fixed_answer", None)
                        kept = [x for x in re.split(r"(?<=[.!?])\s+", text) if x and not ALLERGEN_CLAIM.search(x)] if facts and not same else []
                        facts = facts or W[lang]["alg_which"]
                        text = " ".join(kept + [facts]).strip()
                        g.store.log(T.tr, f"agent:{th['slug']}/{AGENT}", "allergen_answer", "A question about a product's allergens was answered from the manufacturer's declaration, by code. The model's own sentences about it were left out.",
                                    {"answer": facts[:400]}, acct=T.p.get("customer"))
                    elif T.state != "completed" and PLACED_CLAIM.search(text):
                        # The model said the order is placed and it is not. The person reads the true sentence instead.
                        text = W[lang]["built_esc" if T.state == "requires_escalation" else "built_stop" if T.state in ("incomplete", "canceled") else "built"]
                        g.store.log(T.tr, f"agent:{th['slug']}/{AGENT}", "corrected", "The model's sentence said the order was placed when it was not. The shop replaced it with the cart's real state.", {"state": T.state}, acct=T.p.get("customer"))
                    if OUTSIDE.search(fold(str(rec.get("text") or ""))) and W[lang]["outside"] not in text:
                        text = (text + " " + W[lang]["outside"]).strip()
                    if rec.get("skipped"):
                        text = (W[lang]["bad_line"].format(x=rec["skipped"]) + " " + text).strip()
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
                read, said = await confirm(T, cid, by_button=True, read_labels=bool(a.get("read_labels")))
                T.step("confirm", {"cart": cid}, read)
                T.say(said)
            elif kind_ == "again":
                d = g.cat.get(str(a.get("product") or ""))
                if not d:
                    raise Stop(L["again_none"])
                started = time.time()
                said_before = words_since_cart(th, turn) if a.get("asked") else ""  # the button answers "how much?": the request it belongs to stays with the cart, word for word
                cart = {"id": None, "day": g._day(None), "asked_day": None, "moved": None, "items": [], "thread": th["id"], "intent": ((said_before + " / ") if said_before else "") + T.rec["text"], "title": T.rec["text"]}
                apply_stated(T, cart)
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
            elif kind_ == "cancel" and not cart_of(cid):  # the card of a change to a placed order: leaving it leaves the order as it was
                cur = await asyncio.to_thread(g.get_checkout, cid, T.p)
                am = (cur.get("wholesale") or {}).get("amends")
                if not am or cur.get("status") in ("completed", "canceled"):
                    raise Stop(L["gone"])
                res, ms = await T.call("cancel_checkout", {"id": cid})
                if is_checkout(full_of(res)):
                    (res.setdefault("_meta", {})).setdefault(META, {})["amend"] = amend_public(full_of(res), lang)
                T.show("cancel_checkout", {"id": cid}, res, ms)
                T.step("cancel", {"cart": cid}, f"The change to order {am.get('order_label')} was dropped; the order stands as it was.")
                T.say(L["amend_off"].format(label=am.get("order_label")))
            elif kind_ in ("swap", "drop", "day", "cancel", "kind", "usual", "recheck"):
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
                if kind_ == "recheck":  # a cart the door stopped (ordering paused, a rule since changed) is put through the door again as it is
                    read, full = await put_cart(T, cart, started)
                    T.step("recheck", {"cart": cid}, read)
                    T.say(L["recheck_ok"] if (full or {}).get("status") in ("ready_for_complete", "requires_escalation") else L["recheck_no"])
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
                        asked, told = dict(it), (snap(cart, lang), cart["day"])
                        await plan_cart(cart, [{**norm(it), "query": want["kind"], "_t": T.turn, **({"preferred_product": None, "own_pick": True} if it.get("near") else {}),
                                               **({"for_gap": it.get("gap") or it.get("for_gap")} if it.get("gap") or it.get("for_gap") else {})}], T.p, T.tr)
                        new = next((x for x in cart["items"] if x.get("_t") == T.turn), None)
                        rest = [k for k in asked.get("kinds") or [] if k is not want]
                        if new is not None and not new.get("choices") and new.get("status") in ("not_on_day", "not_found") and not new.get("available_on") and rest:
                            # nothing of that kind can come: the question stays on the card with the kinds that are left, and the chat says why this one is not there
                            label = (want.get("fi") or want.get("example_fi") if lang == "fi" else None) or want["kind"]
                            fixed = L["off_day" if new.get("status") == "not_on_day" else "off_never" if new.get("never") else "off_none"].format(a=label, day=day_text(cart["day"], lang))
                            cart["items"][cart["items"].index(new)] = {**asked, "kinds": rest}
                            told = None
                        said = None
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
                if said is None:  # what the tap did, read from the cart before and after
                    said = change_text(T, told[0], cart, told[1], [], not read.startswith("The cart could not be built")) if told else fixed
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
        if k == "recheck":
            return L["recheck"]
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
        action = body.get("action") if isinstance(body.get("action"), dict) and body["action"].get("type") in ("confirm", "swap", "drop", "day", "again", "cancel", "kind", "usual", "recheck") else None
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
