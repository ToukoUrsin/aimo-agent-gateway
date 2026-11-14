"""A photo of a menu becomes one checked order.

A model (Gemini) only reads: is this a menu, which dishes, how many guests, which date, which dietary notes, and,
for a dish the kitchen has no recipe card for, a proposed recipe. Everything after that is code:

* sanity checks on the reading (10,000 guests or a dish that is not food is asked about, never ordered)
* dietary notes turned into allergen exclusions and claims, per order and per line
* the bill of materials: portions x amount per portion + waste allowance, from the kitchen's recipe cards (recipes.json)
* every ingredient mapped to a catalogue product through the gateway's own price and stock path, rounded to pack size
* a shortage solved by what the ingredient does in the dish (ROLES below), not by the nearest product
* one checkout through the normal rules, approval and signing

The same plan is open to any agent: POST /menu/plan and the MCP tool plan_menu_order.
/byhand times a person doing the same basket in a bare webshop over the same catalogue.
"""
import base64
import hashlib
import json
import math
import re
import secrets
import statistics
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta
from pathlib import Path

import httpx
from fastapi import APIRouter, Body, Request, Response

from .catalog import ALLERGENS, UNITS, allergen_hits, allergens_unchecked, declares_allergens, fold, widen_allergens
from .config import TZ, WEB, order_url
from . import passkey
from .service import REASONS, GatewayError, iso, nice_day

MENU_APPROVER, MENU_NOTE = "Kitchen manager, on the menu page", "Approved with the menu and the checked lines in view."

DATA = json.loads((Path(__file__).parent / "recipes.json").read_text())
PANTRY, FAT, WASTE = DATA["pantry"], DATA["fat_pct"], DATA["waste_pct_default"]
CARDS = {c["id"]: c for c in DATA["cards"]}
FAMILY = {pid: p["family"] for p in PANTRY.values() if p.get("family") for pid in p["prefer"]}

GUARD_MODEL, READ_MODEL = "gemini-3.5-flash-lite", "gemini-3.8-flash"
# Google's paid-tier list prices in USD per million tokens (ai.google.dev pricing, read 13 Nov 2026), and the ECB reference rate of that day.
PRICES = {"gemini-3.8-flash": (0.75, 3.75), "gemini-3.5-flash-lite": (0.30, 2.50)}
USD_PER_EUR = 1.1052
COST_BASIS = "Google's list prices on 13 Nov 2026 (Gemini 3.8 Flash $0.75 in and $3.75 out, 3.5 Flash-Lite $0.30 in and $2.50 out per million tokens), at the ECB rate of 13 Nov 2026 (1 EUR = 1.1052 USD)."

MAX_GUESTS, MAX_DISHES, MAX_DAYS = 3000, 40, 14
BYHAND_S_PER_LINE = 45
MEAT_CATS = ("187", "417", "172")  # fresh meat and fish, frozen meat and fish, cold cuts
MEAT_WORDS = re.compile(r"\b(salmon|fish|saithe|chicken|beef|pork|bacon|ham|sausages?|frankfurters?|meat\w*|kala|liha|kana|lohi|nakki)\b")
NUT_WORDS = re.compile(r"\b(nuts?|peanuts?|almonds?|hazelnuts?|cashews?|pecans?|pistachios?|walnuts?|pahkina\w*|manteli\w*|mandel\w*|notter|jordnot\w*|hasselnot\w*|maapahkina\w*)\b")
FOOD_CATS = ("10", "11", "17", "18", "21", "22", "41", "52", "84", "85")

# What an ingredient does in the dish decides what may replace it. One row per role; the numbers are checked against
# the manufacturers' data (fat per 100 g from GS1 nutrition facts, product names), never against a model's opinion.
ROLES = {
    "whipped topping": {"family": "cream", "fat_min": 30, "whips": True, "first": True, "needs": "cream that whips, which takes 30% fat or more"},
    "sauce base": {"family": "cream", "fat_min": 10, "heat": True, "needs": "cream that stands heat; it is only heated, never whipped"},
    "soup base": {"family": "cream", "fat_min": 10, "heat": True, "needs": "cream that stands heat; it is only heated, never whipped"},
    "frying fat": {"family": "fat", "fat_min": 60, "needs": "a fat with 60% fat or more"},
    "roasting fat": {"family": "fat", "fat_min": 60, "needs": "a fat with 60% fat or more"},
    "spread": {"family": "fat", "fat_min": 40, "needs": "a spreadable fat with 40% fat or more"},
    "drink": {"family": "milk", "needs": "a milk or drink of the same kind"},
    "coffee milk": {"family": "milk", "needs": "a milk or drink of the same kind"},
}
WHIP = re.compile(r"whipp|vispi|vispgr|kuohu")
NO_WHIP = re.compile(r"spray|pre-?whipped|aerosol")
COOK = re.compile(r"cooking|ruoka|matlag|imat")

# Dietary words -> the rule code applies. Checked in this order; the first groups win over the later ones.
DIETS = [
    ("nut", r"nut[- ]?free|no nuts|nut allerg\w*|nuts? allerg\w*|without nuts|pahkinat\w*|notfri|peanut"),
    ("lactose", r"lactose[- ]?free|laktoosit\w*|laktosfri|\blf\b|^\(?l\)?$"),
    ("gluten", r"gluten[- ]?free|gluteenit\w*|glutenfri|coeliac|celiac|keliaki\w*|\bgf\b|^\(?g\)?$"),
    ("milk", r"milk[- ]?free|dairy[- ]?free|maidot\w*|mjolkfri|no milk|no dairy|milk allerg\w*|^\(?m\)?$"),
    ("vegan", r"\bvegan\w*|vegaani\w*|^\(?vg\)?$|^\(?ve\)?$"),
    ("vegetarian", r"vegetarian|kasvis\w*|\bveg\b|^\(?v\)?$"),
    ("egg", r"egg[- ]?free|no eggs?|egg allerg\w*|munat\w*"),
    ("fish", r"fish[- ]?free|no fish|fish allerg\w*|kalat\w*"),
]
DIET_WORD = {"nut": "nut-free", "lactose": "lactose-free", "gluten": "gluten-free", "milk": "milk-free", "vegan": "vegan", "vegetarian": "vegetarian", "egg": "egg-free", "fish": "fish-free"}
SCOPE_WORDS = ["bread", "milk", "yoghurt", "yogurt", "cheese", "butter", "cream", "pasta", "macaroni", "porridge", "muesli", "coffee", "croissant", "pancake", "oat", "flour", "margarine", "spread"]
num = lambda x: f"{x:,.0f}" if abs(x - round(x)) < 0.05 else f"{x:,.1f}"
PLURAL = {"piece": "pcs", "case": "cases", "bag": "bags", "bottle": "bottles", "pack": "packs", "box": "boxes", "sack": "sacks", "jar": "jars", "carton": "cartons", "roll": "rolls"}
many = lambda label, n: label if n == 1 or label not in PLURAL else PLURAL[label]


class ModelBusy(Exception):
    pass


# ------------------------------------------------------------------ reading
def _cards_text() -> str:
    return "\n".join(f"- {c['id']}: {c['name']} (also: {', '.join(c['aliases'][:6])})" for c in CARDS.values())


READ_PROMPT = """You read a kitchen's menu or meal plan so that the food for it can be ordered from a wholesaler. Return one JSON object and nothing else.
Today is {today}.

{{"menu": true or false, "what": "a few words: what this is",
 "title": "the heading, or a short name for the occasion", "date": "YYYY-MM-DD of the first day served, or null", "date_text": "the date as written, or null",
 "guests": the number of guests, pupils or portions as an integer, or null when it is not written, "guests_text": "as written, or null",
 "dishes": [{{"name": "the dish in plain English", "as_written": "exactly as written", "card": "a card id from the list, or null",
   "portions": an integer only when the menu gives this dish its own number (for example 'x14'), else null,
   "days": how many days this dish is served (1; a daily item on a five-day plan is 5),
   "notes": ["marks or notes written on this dish, verbatim, for example L, G, vegetarian"], "food": true or false,
   "recipe": null, or when card is null and it is food: [{{"item": "ingredient", "role": "what it does in the dish", "amount": number per portion, "unit": "g" or "ml"}}]}}],
 "notes": ["every dietary or allergy note on the page that is not attached to one dish, verbatim"]}}

Rules:
- Copy what is there. Never invent a dish, a number or a note. A crossed-out line is not on the menu.
- menu is false when this is not a menu, a meal plan or a list of dishes to serve.
- card: use a card only when it is the same dish. Otherwise null and give a recipe.
- recipe: per portion, as a professional kitchen cooks it, 3 to 10 ingredients, amounts in g or ml. Roles are short: main, body, vegetable, sauce base, soup base, whipped topping, frying fat, spread, drink, seasoning, stock, sweetener, batter, garnish. Use an item name from the pantry list when one fits; otherwise a plain English ingredient name.
- food is false when the line is not something to eat or drink (a decoration, a person, a room, equipment).

The kitchen's recipe cards:
{cards}

The kitchen's pantry: {pantry}"""

GUARD_PROMPT = ('Look at the image. Is it a menu, a meal plan or a list of dishes that a kitchen will serve? Handwriting, a whiteboard, a printed page or a screen all count. '
                'Answer with JSON only: {"menu": true or false, "what": "a few words saying what the image shows"}')

RECIPE_PROMPT = """A professional kitchen needs a recipe per portion for each dish below so the ingredients can be ordered. Return JSON only:
{{"dishes": [{{"name": "as given", "food": true or false, "recipe": [{{"item": "ingredient", "role": "what it does in the dish", "amount": number per portion, "unit": "g" or "ml"}}]}}]}}
3 to 10 ingredients each, amounts in g or ml. Roles are short: main, body, vegetable, sauce base, soup base, whipped topping, frying fat, spread, drink, seasoning, stock, sweetener, batter, garnish.
Use an item name from the pantry list when one fits; otherwise a plain English ingredient name. food is false when the line is not something to eat or drink; then recipe is [].
Pantry: {pantry}
Dishes: {dishes}"""

_key_lock = threading.Lock()


def _gemini(model: str, content, effort: str | None = None, timeout: float = 45) -> tuple[dict, dict]:
    """One JSON answer from a Gemini model, with the real token counts of the call. Raises ModelBusy when the model cannot answer now."""
    from agent import llm  # lazy: the gateway runs without the agent package's model keys until a menu is read
    with _key_lock:
        try:
            key = llm._key("google")
        except Exception as e:
            raise ModelBusy(str(e))
    body = {"model": model, "messages": [{"role": "user", "content": content}], "temperature": 0, "response_format": {"type": "json_object"}}
    if effort:
        body["reasoning_effort"] = effort
    url, t, last = llm.PROVIDERS["google"]["base"].rstrip("/") + "/chat/completions", time.time(), ""
    for attempt in range(2):
        try:
            r = httpx.post(url, headers={"Authorization": "Bearer " + key}, json=body, timeout=timeout)
        except httpx.HTTPError as e:
            last = type(e).__name__
            continue
        if r.status_code == 200:
            j = r.json()
            text = (j["choices"][0]["message"].get("content") or "").strip()
            text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text)
            u = j.get("usage") or {}
            tin = int(u.get("prompt_tokens") or 0)
            tout = int(u.get("completion_tokens") or 0)
            if not tout:
                tout = max(0, int(u.get("total_tokens") or 0) - tin)
            usage = {"model": model, "in": tin, "out": tout, "seconds": round(time.time() - t, 2)}
            try:
                out = json.loads(text)
            except ValueError:
                raise ModelBusy("The model's answer was not readable.")
            return (out if isinstance(out, dict) else {}), usage
        last = f"HTTP {r.status_code}"
        if r.status_code not in (429, 500, 502, 503, 504):
            break
        time.sleep(1.5)
    raise ModelBusy(last)


def cost(calls: list[dict]) -> dict:
    """Model cost of one run from the real usage numbers of its calls."""
    tin, tout = sum(c["in"] for c in calls), sum(c["out"] for c in calls)
    usd = sum((c["in"] * PRICES[c["model"]][0] + c["out"] * PRICES[c["model"]][1]) / 1e6 for c in calls)
    return {"tokens_in": tin, "tokens_out": tout, "usd": round(usd, 5), "eur": round(usd / USD_PER_EUR, 5), "calls": calls, "basis": COST_BASIS if calls else "No model was used: the dishes were typed and every one has a recipe card."}


def match_card(name: str) -> str | None:
    """A typed dish name to one of the kitchen's cards: the same name, or a card name that makes up most of it."""
    f = re.sub(r"\s+", " ", re.sub(r"[^a-z0-9& ]", " ", fold(name or ""))).strip()
    if not f:
        return None
    best = (0.0, None)
    for c in CARDS.values():
        for a in [c["name"], *c["aliases"]]:
            a = re.sub(r"\s+", " ", re.sub(r"[^a-z0-9& ]", " ", fold(a))).strip()
            if a == f:
                return c["id"]
            if len(a) >= 5 and re.search(rf"\b{re.escape(a)}\b", f) and len(a) / len(f) >= 0.6 and len(a) / len(f) > best[0]:
                best = (len(a) / len(f), c["id"])
    return best[1]


def _int(x):
    try:
        if isinstance(x, str):
            x = re.sub(r"[ ,.](?=\d{3}\b)", "", x.strip())
        v = float(x)
        return int(v) if math.isfinite(v) and v == int(v) else None
    except (TypeError, ValueError):
        return None


def _recipe(raw) -> list[dict]:
    out = []
    for i in (raw if isinstance(raw, list) else [])[:14]:
        if not isinstance(i, dict):
            continue
        item, unit = str(i.get("item") or "").strip().lower()[:48], str(i.get("unit") or "").strip().lower()
        try:
            amount = float(i.get("amount"))
        except (TypeError, ValueError):
            continue
        mult = {"g": 1, "ml": 1, "kg": 1000, "l": 1000, "dl": 100, "cl": 10}.get(unit)
        if not item or mult is None or not math.isfinite(amount):
            continue
        out.append({"item": item, "role": str(i.get("role") or "ingredient").strip().lower()[:32], "amount": round(amount * mult, 2), "unit": "ml" if unit in ("ml", "l", "dl", "cl") else "g"})
    return out


# Words a menu prints as section headings. A reading sometimes returns them as notes; a heading is never a dietary note.
HEADINGS = {"starter", "starters", "main", "mains", "main course", "main courses", "dessert", "desserts", "appetizer", "appetizers", "first course", "second course", "side", "sides", "side dishes",
            "drinks", "beverages", "coffee", "breakfast", "lunch", "dinner", "supper", "buffet", "menu", "notes", "note", "dietary notes", "allergens", "allergy notes",
            "monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday", "alkuruoka", "alkuruoat", "pääruoka", "pääruoat", "jälkiruoka", "jälkiruoat", "juomat", "aamiainen", "lounas", "illallinen"}


def _heading(note: str) -> bool:
    return re.sub(r"[^a-zäöå ]", "", str(note).lower()).strip() in HEADINGS


def normalize(raw: dict, source: str) -> dict:
    """The model's (or a caller's) reading as the plain structure the code plans from. Nothing here trusts a number: sanity() checks them."""
    dishes = []
    for x in (raw.get("dishes") if isinstance(raw.get("dishes"), list) else [])[:60]:
        if isinstance(x, str):
            x = {"name": x}
        if not isinstance(x, dict):
            continue
        name = str(x.get("name") or x.get("as_written") or "").strip()[:90]
        if not name:
            continue
        card = x.get("card") if x.get("card") in CARDS else (match_card(name) or match_card(str(x.get("as_written") or "")))
        recipe = [] if card else _recipe(x.get("recipe"))
        notes = x.get("notes") if isinstance(x.get("notes"), list) else [x["notes"]] if x.get("notes") else []
        dishes.append({"name": name, "as_written": str(x.get("as_written") or name)[:120], "card": card, "portions": _int(x.get("portions")), "days": _int(x.get("days")) or 1,
                       "notes": [str(n).strip()[:80] for n in notes if str(n).strip() and not _heading(n)][:6], "food": x.get("food") is not False, "recipe": recipe,
                       "source": "card" if card else (x.get("source") if x.get("source") in ("agent", "model") else source) if recipe else None})
    notes = raw.get("notes") if isinstance(raw.get("notes"), list) else [raw["notes"]] if raw.get("notes") else []
    day = str(raw.get("date") or raw.get("event_date") or "")[:10]
    try:
        date.fromisoformat(day)
    except ValueError:
        day = None
    g = raw.get("guests")
    return {"title": str(raw.get("title") or "").strip()[:90], "date": day, "date_text": str(raw.get("date_text") or "").strip()[:60] or None,
            "guests": _int(g) if _int(g) is not None else None, "guests_text": str(raw.get("guests_text") or (g if g is not None and _int(g) is None else "") or "").strip()[:60] or None,
            "dishes": dishes, "notes": [str(n).strip()[:140] for n in notes if str(n).strip() and not _heading(n)][:12]}


def read_photo(image: str) -> tuple[dict | None, dict, list[dict]]:
    """(reading or None, verdict, model calls). The guard runs first so a refused image is never fully read."""
    content = lambda text: [{"type": "text", "text": text}, {"type": "image_url", "image_url": {"url": image}}]
    prompt = READ_PROMPT.format(today=datetime.now(TZ).strftime("%A %d %B %Y"), cards=_cards_text(), pantry=", ".join(PANTRY))
    guard = read = None
    calls = []
    try:
        guard, u = _gemini(GUARD_MODEL, content(GUARD_PROMPT), None, 25)
        calls.append({**u, "step": "is it a menu"})
    except ModelBusy:
        pass
    if guard is not None and guard.get("menu") is False:
        what = str(guard.get("what") or "").strip()[:80]
        return None, {"menu": False, "what": what}, calls
    try:
        read, u = _gemini(READ_MODEL, content(prompt), "low", 60)
        calls.append({**u, "step": "read the menu"})
    except ModelBusy:
        pass
    if guard is None and read is None:
        raise GatewayError(503, "model_busy", "The model that reads the photo is busy. Try again in a minute.")
    is_menu = (guard or read).get("menu") is not False and (read is None or read.get("menu") is not False)
    what = str(((guard if guard and guard.get("menu") is False else read) or guard or {}).get("what") or "").strip()[:80]
    if not is_menu:
        return None, {"menu": False, "what": what}, calls
    if read is None:
        raise GatewayError(503, "model_busy", "The model that reads the photo is busy. Try again in a minute.")
    return normalize(read, "model"), {"menu": True, "what": what}, calls


def fill_recipes(reading: dict) -> list[dict]:
    """A typed dish with no card and no recipe gets a proposed recipe from the model (one call for all of them)."""
    need = [d for d in reading["dishes"] if not d["card"] and not d["recipe"] and d["food"]]
    if not need:
        return []
    try:
        out, u = _gemini(READ_MODEL, RECIPE_PROMPT.format(pantry=", ".join(PANTRY), dishes=json.dumps([d["name"] for d in need])), "low", 45)
    except ModelBusy:
        return []
    got = [x for x in (out.get("dishes") if isinstance(out.get("dishes"), list) else []) if isinstance(x, dict)]
    for i, d in enumerate(need):
        x = next((x for x in got if fold(str(x.get("name") or "")) == fold(d["name"])), got[i] if i < len(got) and len(got) == len(need) else None)
        if x:
            d["food"] = x.get("food") is not False
            d["recipe"], d["source"] = (_recipe(x.get("recipe")), "model") if d["food"] else ([], None)
    return [{**u, "step": "propose recipes"}]


# ---------------------------------------------------- checks on the reading
def sanity(rd: dict) -> list[dict]:
    """What code will not order without a person's answer. Each question names what to fix."""
    q, g = [], rd.get("guests")
    ask = lambda about, text: q.append({"about": about, "text": text})
    if g is None:
        ask("guests", "The menu does not say how many guests or portions it is for" + (f" (it reads “{rd['guests_text']}”)" if rd.get("guests_text") else "") + ". How many?")
    elif g < 1:
        ask("guests", f"The menu reads as {g} guests. How many is it for?")
    elif g > MAX_GUESTS:
        ask("guests", f"The menu reads as {g:,} guests. Nothing over {MAX_GUESTS:,} is ordered without asking. How many is it for?")
    if not rd["dishes"]:
        ask("dishes", "No dishes could be read from this menu.")
    if len(rd["dishes"]) > MAX_DISHES:
        ask("dishes", f"The menu reads as {len(rd['dishes'])} dishes. Nothing over {MAX_DISHES} is planned in one order. Remove some.")
    for i, d in enumerate(rd["dishes"]):
        if not d["food"]:
            ask(f"dish:{i}", f"“{d['name']}” does not look like food or drink. Remove it, or rename it if the reading is wrong.")
            continue
        if d["days"] < 1 or d["days"] > MAX_DAYS:
            ask(f"dish:{i}", f"“{d['name']}” reads as served on {d['days']} days. Nothing over {MAX_DAYS} days is planned in one order.")
        p = d["portions"]
        if p is not None and (p < 1 or p > MAX_GUESTS or (g and 1 <= g <= MAX_GUESTS and p > 2 * g)):
            ask(f"dish:{i}", f"“{d['name']}” reads as {p:,} portions" + (f" for {g:,} guests" if g else "") + ". How many portions?")
        if d["card"]:
            continue
        if not d["recipe"]:
            ask(f"dish:{i}", f"There is no recipe card for “{d['name']}” and no recipe could be proposed. Rename it to a dish the kitchen has a card for, or remove it.")
            continue
        bad = [r for r in d["recipe"] if not 0.05 <= r["amount"] <= 800]
        total = sum(r["amount"] for r in d["recipe"])
        if bad or total > 1500:
            ask(f"dish:{i}", f"The proposed recipe for “{d['name']}” does not add up to a portion ({num(total)} g or ml in total" + (f", {num(bad[0]['amount'])} {bad[0]['unit']} of {bad[0]['item']}" if bad else "") + "). Remove the dish or rename it.")
    return q


def _kinds(text: str) -> list[str]:
    f = fold(text).strip()
    return [k for k, pat in DIETS if re.search(pat, f)]


def parse_notes(rd: dict) -> tuple[list[dict], list[dict]]:
    """Dietary notes -> rules. (rules, questions). A rule: {kind, note, count, items, dish, text}.
    count: how many guests it is for (None = everyone it covers). items: ingredient words it is limited to. dish: index when written on one dish."""
    rules, q, g = [], [], rd.get("guests") or 0
    for i, d in enumerate(rd["dishes"]):
        for n in d["notes"]:
            for part in re.split(r"[,;/]| and | & ", n):
                m = re.search(r"(?:x|for)\s*(\d+)|(\d+)\s*(?:x|pcs|kpl)?$", fold(part).strip())
                kinds = _kinds(part)
                for k in kinds:
                    rules.append({"kind": k, "note": n, "count": None, "items": [], "dish": i})
                if not kinds and m and not d["portions"]:
                    pass  # a bare number on a dish is its portion count; the reading carries it
    for ni, n in enumerate(rd["notes"]):
        found = False
        for part in re.split(r"[,;\n]", n):
            f = fold(part)
            kinds = _kinds(part)
            if not kinds:
                continue
            found = True
            rest = f
            for k, pat in DIETS:
                rest = re.sub(pat, " ", rest)
            bare = re.sub(r"\b(table|room|poyta|bord|class|grade|luokka|no|nr)\.?\s*\w?\d+\w?", " ", rest)
            m = re.search(r"\b(\d{1,4})\b", bare)
            count = int(m.group(1)) if m else None
            items = sorted({w for w in SCOPE_WORDS if re.search(rf"\b{w}s?\b", rest)} | {p for p in PANTRY if re.search(rf"\b{re.escape(p)}\b", rest)})
            for k in kinds:
                if count is not None and g and count > g:
                    q.append({"about": f"note:{ni}", "text": f"The note “{n}” reads as {count:,} guests, more than the {g:,} on the menu. Fix the note or remove it."})
                    continue
                rules.append({"kind": k, "note": n, "count": None if k == "nut" else count, "items": [] if k == "nut" else items, "dish": None})
        if not found:
            q.append({"about": f"note:{ni}", "text": f"The note “{n}” could not be turned into a rule the code can check. Reword it (for example “6 gluten-free”) or remove it."})
    seen, out = set(), []
    for r in rules:
        key = (r["kind"], r["count"], tuple(r["items"]), r["dish"])
        if key not in seen:
            seen.add(key)
            out.append(r)
    for r in out:
        who = f"“{rd['dishes'][r['dish']]['name']}”" if r["dish"] is not None else ("the " + ", ".join(r["items"])) if r["items"] else "every dish"
        how = {"nut": "No product on this order may contain or may contain traces of tree nuts or peanuts. Checked on every line.",
               "lactose": "any product with milk in it must carry the manufacturer's lactose-free claim.",
               "gluten": "no product with a gluten cereal (certified gluten-free oats are allowed).",
               "milk": "no product with milk; the pantry's milk-free alternative is used where there is one.",
               "vegan": "no milk, egg, fish or meat.", "vegetarian": "no meat or fish.", "egg": "no product with egg.", "fish": "no product with fish."}[r["kind"]]
        if r["kind"] == "nut":
            r["text"] = how
        elif r["count"]:
            r["text"] = f"{DIET_WORD[r['kind']].capitalize()} for {r['count']} of {g:,}: in {who}, that share of the portions gets {how}"
        else:
            r["text"] = f"{DIET_WORD[r['kind']].capitalize()} in {who}" + ("" if r["dish"] is not None or r["items"] else ", because the note gives no head count") + f": {how}"
    return out, q


# ------------------------------------------------------------- the planner
def _cons(rules: list[dict]) -> dict:
    ex, lf, veg, swap, why = set(), False, "", [], []
    for r in rules:
        k = r["kind"]
        if k == "lactose":
            lf = True
        elif k == "gluten":
            ex |= {"AW"}; swap.append("gluten_free")
        elif k == "milk":
            ex |= {"AM", "ML"}; swap.append("milk_free")
        elif k == "egg":
            ex |= {"AE"}
        elif k == "fish":
            ex |= {"AF"}
        elif k == "vegan":
            ex |= {"AM", "ML", "AE", "AF"}; veg = "vegan"; swap.append("milk_free")
        elif k == "vegetarian":
            veg = veg or "vegetarian"
        if k != "nut":
            why.append(DIET_WORD[k])
    return {"ex": ex, "lf": lf, "veg": veg, "swap": swap, "why": sorted(set(why))}


def _applies(rule: dict, dish_i: int, item: str) -> bool:
    if rule["kind"] == "nut":
        return False  # order-wide, handled once
    if rule["dish"] is not None:
        return rule["dish"] == dish_i
    return not rule["items"] or any(w in item for w in rule["items"])


def demands(rd: dict, rules: list[dict]) -> list[dict]:
    """Every (dish, ingredient) with its amount, split where a dietary note covers only some of the portions."""
    out, g = [], rd["guests"]
    for i, d in enumerate(rd["dishes"]):
        portions = (d["portions"] or g) * d["days"]
        recipe = CARDS[d["card"]]["ingredients"] if d["card"] else d["recipe"]
        for ing in recipe:
            mine = [r for r in rules if _applies(r, i, ing["item"])]
            everyone, some = [r for r in mine if not r["count"]], [r for r in mine if r["count"]]
            parts, left = [], portions
            for r in some:
                n = min(left, math.ceil(portions * r["count"] / g))
                if n > 0:
                    parts.append((n, everyone + [r]))
                    left -= n
            if left > 0:
                parts.append((left, everyone))
            waste = ing.get("waste_pct", WASTE)
            for n, rs in parts:
                c = _cons(rs)
                out.append({"dish_i": i, "dish": d["name"], "item": ing["item"], "role": ing["role"], "amount": ing["amount"], "unit": ing["unit"], "waste": waste, "portions": n,
                            "need": n * ing["amount"] * (1 + waste / 100), "cons": c, "source": d["source"] or "card"})
    return out


def _key(unit: str) -> str:
    return "l" if unit == "ml" else "kg"


def _names(codes) -> str:
    return ", ".join(sorted({ALLERGENS.get(c, c).lower() for c in codes}))


def _ingredient_matches(item: str, d: dict) -> bool:
    """Whether the catalogue product is the ingredient named by the recipe."""
    text = fold(" ".join((d.get("names") or {}).values()) + " " + (d.get("title") or ""))
    aliases = {
        "parmesan": ("parmesan", "parmigiano", "hard cheese"),
        "coffee milk": ("coffee milk", "milk", "maito", "mjolk"),
        "vegetable bouillon": ("bouillon", "stock", "liemikuutio", "kasvisliemi"),
        "rapeseed oil": ("rapeseed", "canola", "rypsi", "oil", "olje"),
    }
    terms = aliases.get(item, (item,))
    return any(all(w in text for w in fold(term).split()) for term in terms)


class Planner:
    """One plan: one batch of live facts, then everything in code."""

    def __init__(self, g, principal):
        self.g, self.p = g, principal
        self.live, self.day = {}, None

    def content(self, pid, unit) -> float | None:
        """How much of the recipe's unit (g or ml) one sales unit holds."""
        d, f = self.g.cat.get(pid), self.live.get(pid) or {}
        c = self.g._content(d, f, _key(unit))
        return c * 1000 if c else None

    def step(self, pid) -> float:
        d = self.g.cat.get(pid)
        return d["increment"] / 10 ** d["scale"]

    def cap(self, pid) -> float:
        """Sales units that can be ordered, in whole steps."""
        f, s = self.live[pid], self.step(pid)
        return math.floor(max(0.0, f["available"]) / s + 1e-9) * s if f.get("stock_known") else math.inf

    def why_not(self, pid, unit, cons, order_ex, sib=frozenset()) -> str | None:
        """None when the product may go on the order for this ingredient, else the reason in plain words. This is the only gate."""
        d, f = self.g.cat.get(pid), self.live.get(pid) or {}
        if not d:
            return "it is not in the catalogue"
        if not f.get("orderable") and not (f.get("reason") == "out_of_stock" and f.get("stock_known")):
            return "it cannot be ordered for this day: " + REASONS.get(f.get("reason"), str(f.get("reason")))
        key = _key(unit)
        if not self.content(pid, unit):
            return "its pack size is not in the data"
        cc, lc = d["content"].get(key), f.get("content_per_unit") if f.get("comparison_unit") == ("L" if key == "l" else "KG") else None
        if cc and lc and abs(cc - lc) / max(cc, lc) > 0.15:
            return "its pack size differs between the catalogue and the price data"
        ex = set(order_ex) | set(cons["ex"])
        hits = allergen_hits(ex, d["contains"], d["claims"]) | allergen_hits(ex, d["may_contain"], d["claims"])
        if hits:
            return "it contains or may contain " + _names(hits)
        if not (d["contains"] or d["may_contain"] or d.get("free_from")):
            # No allergen data at all is not the same as no allergens: when the other products for this ingredient declare what is ruled out, this one cannot be checked.
            claims, cleared = set(d["claims"]), set()  # what the manufacturer's own claims rule out
            if "FREE_FROM:GLUTEN" in claims:
                cleared |= set(widen_allergens({"AW"}))
            if "DIET:VEGAN" in claims:
                cleared |= {"AM", "ML", "AE", "AF"}
            if "FREE_FROM:EGGS" in claims:
                cleared |= {"AE"}
            if "FREE_FROM:LACTOSE" in claims and not {"AM", "ML"} & ex:
                cleared |= {"AM", "ML"}
            risk = allergen_hits(ex | ({"AM", "ML"} if cons["lf"] else set()), sib) - cleared
            if risk:
                return f"its data lists no allergens at all, while other products for this ingredient contain {_names(risk)}, so it cannot be checked"
        if ex & {"AN", "AP"} and NUT_WORDS.search(d["_text"]):
            return "its name mentions nuts"
        if cons["lf"] and {"AM", "ML"} & set(d["contains"]) and "FREE_FROM:LACTOSE" not in d["claims"]:
            return "it has milk in it and no lactose-free claim"
        if cons["veg"] and str(d["category"] or "")[:3] in MEAT_CATS:
            return "it is meat or fish"
        return None

    def role_fit(self, role, pid) -> tuple[bool, str]:
        """Does this product do the job the role needs? From the manufacturer's fat figure and the product name."""
        rule, d = ROLES.get(role), self.g.cat.get(pid)
        if not rule:
            return True, ""
        fat, text = FAT.get(pid), fold(" ".join(d["names"].values()))
        if rule.get("fat_min") and (fat is None or fat < rule["fat_min"]):
            return False, f"{fat:g}% fat" if fat is not None else "no fat figure"
        if rule.get("whips") and (not WHIP.search(text) or NO_WHIP.search(text)):
            return False, "not a whipping cream"
        if rule.get("heat") and not (COOK.search(text) or (fat or 0) >= 30):
            return False, "not made for cooking"
        return True, (f"{fat:g}% fat" if fat is not None else "") + (", whips" if rule.get("whips") else ", made for cooking" if rule.get("heat") and COOK.search(text) else "")

    def rule_for(self, role, usual):
        """The role's rule, only when the ingredient's own product is of the rule's family: "sauce base" says something about cream, nothing about olive oil."""
        rule = ROLES.get(role)
        return rule if rule and FAMILY.get(usual) == rule["family"] else None

    def per(self, pid):
        f = self.live[pid]
        return (f.get("comparison_unit") or "", f.get("comparison_price") or f.get("price") or 0)

    def alternatives(self, dm, usual, order_ex) -> list[str]:
        """What may stand in for `usual` in this role, best first. Never a product that fails the role, drops lactose-free or adds an allergen."""
        rule, ud = self.rule_for(dm["role"], usual), self.g.cat.get(usual)
        pool = [p for p in dm["cands"] if p != usual] + ([p for p, fam in FAMILY.items() if fam == rule["family"]] if rule else [])
        out = []
        for pid in dict.fromkeys(pool):
            d = self.g.cat.get(pid)
            if pid == usual or not d or self.why_not(pid, dm["unit"], dm["cons"], order_ex, dm["sib"]) or (rule and not self.role_fit(dm["role"], pid)[0]):
                continue
            if "FREE_FROM:LACTOSE" in ud["claims"] and {"AM", "ML"} & set(d["contains"]) and "FREE_FROM:LACTOSE" not in d["claims"]:
                continue
            if (set(d["contains"]) | set(d["may_contain"])) - (set(ud["contains"]) | set(ud["may_contain"])):
                continue
            out.append(pid)
        if rule:
            unit = self.per(usual)[0]
            out.sort(key=lambda p: (self.per(p)[0] != unit, self.per(p)[1]))
        return out

    # ---------------------------------------------------------------- plan
    def plan(self, rd: dict, rules: list[dict], dms: list[dict], day: str, day_note: str) -> dict:
        g = self.g
        order_ex = {"AN", "AP"} if any(r["kind"] == "nut" for r in rules) else set()
        seen = g._cleared(self.p)  # products with no allergen declaration that a person cleared on this account before
        # 1. candidates per ingredient: the pantry's products, its diet alternative, then (for a name not in the pantry) the catalogue search
        for dm in dms:
            p = PANTRY.get(dm["item"])
            if p:
                names = [dm["item"]] + [p[s] for s in dm["cons"]["swap"] if p.get(s)]
                dm["pool"], dm["known"] = list(dict.fromkeys(pid for n in names for pid in PANTRY[n]["prefer"])), True
            else:
                docs, _, _ = g.cat.search(dm["item"], {}, limit=40)
                dm["pool"], dm["known"] = [d["id"] for d in docs if str(d["category"] or "")[:2] in FOOD_CATS][:8], False
            dm["sib"] = frozenset(a for pid in dm["pool"] if g.cat.get(pid) for a in g.cat.get(pid)["contains"])
        ids = list(dict.fromkeys([pid for dm in dms for pid in dm["pool"]] + [pid for dm in dms if dm["role"] in ROLES for pid, fam in FAMILY.items() if fam == ROLES[dm["role"]]["family"]]))
        self.live = g._live(ids, day, self.p) if ids else {}
        late = [self.live[dm["pool"][0]].get("earliest_date") for dm in dms if dm["pool"] and self.live[dm["pool"][0]].get("reason") == "later_date_only"]
        if late and len(late) >= len(dms) / 2:  # the wholesaler cannot deliver that early: take its first day instead, once
            first = max(d for d in late if d)
            day_note = f"The first day Valio Aimo can deliver is {nice_day(first)}, so delivery is set to that day."
            day, self.live = first, g._live(ids, first, self.p)
        self.day = day

        # 2. the usual product per ingredient: first in the kitchen's order that passes every check
        opened, skipped = [], {}
        for dm in dms:
            dm["cands"] = []
            for pid in dm["pool"]:
                why = self.why_not(pid, dm["unit"], dm["cons"], order_ex, dm["sib"])
                if why is None:
                    dm["cands"].append(pid)
                elif not dm["cands"]:
                    tried = skipped.setdefault((dm["item"], tuple(dm["cons"]["why"])), [])
                    if all(t["id"] != pid for t in tried):
                        tried.append({"id": pid, "title": g.cat.get(pid)["title"] if g.cat.get(pid) else pid, "why": why})

        # With an exclusion in force, a product whose allergens can be checked from the manufacturer's data goes before one
        # that says nothing (the kitchen's order is kept otherwise), so fewer lines need a person to clear them.
        for dm in dms:
            ex = set(order_ex) | set(dm["cons"]["ex"])
            if ex and len(dm["cands"]) > 1:
                dm["cands"].sort(key=lambda pid: bool({x for x in allergens_unchecked(ex, g.cat.get(pid)) if not (seen.get(pid) or {}).get(x)}))

        # 3. A dietary refusal makes a dish a single question. Do this before
        # allocating any of its other ingredients, so a cake cannot be partly
        # ordered while its essential nut ingredient waits for an answer.
        blocked, opened = {}, []
        for dm in dms:
            tried = skipped.get((dm["item"], tuple(dm["cons"]["why"])), [])
            catalogue_dietary = any(
                (g.cat.get(pid) and (
                    allergen_hits(set(order_ex) | set(dm["cons"]["ex"]), g.cat.get(pid)["contains"], g.cat.get(pid)["claims"])
                    or allergen_hits(set(order_ex) | set(dm["cons"]["ex"]), g.cat.get(pid)["may_contain"], g.cat.get(pid)["claims"])))
                for pid in dm["pool"]
            )
            dietary_block = bool(dm["cons"]["why"] or catalogue_dietary or any(re.search(r"allergen|contain|nuts?", t.get("why", ""), re.I) for t in tried))
            if dm["cands"] or not dietary_block:
                continue
            blocked.setdefault((dm["dish_i"], tuple(dm["cons"]["why"])), dm)
        # What is held is the portions the rule covers. Twelve vegetarians do not stop the chicken for the other 168.
        total_of = {i: (d["portions"] or rd["guests"]) * d["days"] for i, d in enumerate(rd["dishes"])}
        held, whole = {}, set()
        for (dish_i, _), dm in blocked.items():
            held[dish_i] = held.get(dish_i, 0) + dm["portions"]
        for (dish_i, why_key), dm in blocked.items():
            tried = skipped.get((dm["item"], tuple(dm["cons"]["why"])), [])
            reason = tried[0]["why"] if tried else "the dietary rule does not allow a product for this ingredient"
            diet = ", ".join(dm["cons"]["why"]) or "nut-free"
            for pid in dm["pool"]:
                d = g.cat.get(pid)
                if not d:
                    continue
                hits = allergen_hits(set(order_ex) | set(dm["cons"]["ex"]), d["contains"], d["claims"]) | allergen_hits(set(order_ex) | set(dm["cons"]["ex"]), d["may_contain"], d["claims"])
                if hits:
                    reason = "it contains or may contain " + _names(hits)
                    if set(hits) & {"AN", "AP"}:
                        diet = "nut-free"
                    break
            rest = total_of[dish_i] - held[dish_i]
            if rest > 0:  # only the portions under this rule wait; the rest of the dish is ordered as usual
                opened.append({"kind": "no_fit", "dish": dm["dish"], "item": dm["item"], "reason": reason, "portions_refused": dm["portions"], "diet": list(why_key),
                               "text": f"{dm['dish']}: the {dm['portions']} {diet} portions cannot be made because {dm['item']} was refused: {reason}. "
                                       f"Nothing is ordered for those {dm['portions']}; a person decides what they get. The other {rest} portions are ordered."})
            elif dish_i not in whole:
                whole.add(dish_i)
                opened.append({"kind": "no_fit", "dish": dm["dish"], "item": dm["item"], "reason": reason, "dish_refused": True,
                               "text": f"{dm['dish']}: it cannot be made for {diet} because {dm['item']} was refused: {reason}. The whole dish is waiting for a person's answer; none of its ingredients is ordered."})

        # 4. hand out stock: roles that have the fewest ways out are served first; a shortage is solved by role
        left = {pid: self.cap(pid) for pid in ids if self.live[pid].get("orderable") or self.live[pid].get("reason") == "out_of_stock"}
        alloc, shorted = [], {}  # alloc: (demand, product, sales units, usual product or None, reason); shorted: product -> the sentence that says it is short
        for dm in sorted(dms, key=lambda d: 0 if ROLES.get(d["role"], {}).get("first") else 1):
            if (dm["dish_i"], tuple(dm["cons"]["why"])) in blocked:
                continue
            who = f"{dm['item']} for {dm['dish']}" + (f" ({', '.join(dm['cons']['why'])}, {dm['portions']} portions)" if dm["cons"]["why"] else "")
            amount = f"{num(dm['need'] / 1000)} {'l' if dm['unit'] == 'ml' else 'kg'}"
            if not dm["cands"]:
                tried = skipped.get((dm["item"], tuple(dm["cons"]["why"])), [])
                if not dm["pool"]:
                    opened.append({"kind": "not_in_range", "dish": dm["dish"], "item": dm["item"], "amount": amount, "text": f"{who.capitalize()}: not in Valio Aimo's range. Not ordered."})
                else:
                    opened.append({"kind": "no_fit", "dish": dm["dish"], "item": dm["item"], "amount": amount,
                                   "text": f"{who.capitalize()}: no product passes the checks, so it is not ordered. " + " ".join(f"{t['title']}: {t['why']}." for t in tried[:3])})
                continue
            usual = dm["cands"][0]
            need = dm["need"] / self.content(usual, dm["unit"])
            take = min(need, left[usual])
            if take > 1e-9:
                alloc.append((dm, usual, take, None, None))
                left[usual] -= take
            missing = (need - take) * self.content(usual, dm["unit"])  # back in g or ml
            if missing <= 1e-6:
                continue
            ut, rule = g.cat.get(usual)["title"], self.rule_for(dm["role"], usual)
            have = f"{ut} is short: {num(self.cap(usual))} {many(self.label(usual), self.cap(usual))} can be supplied and this menu needs more." if self.cap(usual) else f"{ut} is out of stock for this day."
            shorted[usual] = have
            for alt in self.alternatives(dm, usual, order_ex):
                if missing <= 1e-6:
                    break
                c = self.content(alt, dm["unit"])
                take = min(missing / c, left.get(alt, 0))
                if take <= 1e-9:
                    continue
                at, fit = g.cat.get(alt)["title"], self.role_fit(dm["role"], alt)[1]
                if rule:
                    why = (f"A {dm['role']} needs {rule['needs']}. This product ({fit}) does that, so it covers {num(take * c / 1000)} {'l' if dm['unit'] == 'ml' else 'kg'} for {dm['dish']}."
                           + (" Cooking cream would not whip, so it is not used here." if rule.get("whips") else " It is the cheapest per litre of the creams that fit." if rule["family"] == "cream" else ""))
                else:
                    why = f"This is the kitchen's next choice for {dm['item']}, so it covers {num(take * c / 1000)} {'l' if dm['unit'] == 'ml' else 'kg'} for {dm['dish']}."
                alloc.append((dm, alt, take, usual, (have, why)))
                left[alt] -= take
                missing -= take * c
            if missing > 1e-6:
                none = (f"A {dm['role']} needs {rule['needs']}, and no other product on this kitchen's list does that" + (", so cooking cream is not used" if rule.get("whips") else "") + ".") if rule else f"There is no other product for {dm['item']} on this kitchen's list."
                opened.append({"kind": "short", "dish": dm["dish"], "item": dm["item"], "role": dm["role"], "amount": f"{num(missing / 1000)} {'l' if dm['unit'] == 'ml' else 'kg'}",
                               "text": f"{who.capitalize()}: {num(missing / 1000)} {'l' if dm['unit'] == 'ml' else 'kg'} is not covered. {have} {none} The line is kept short and a person decides."})

        # 5. one line per product, rounded up to the pack size and the order step
        lines, by = [], {}
        for dm, pid, units, usual, why in alloc:
            by.setdefault(pid, []).append((dm, units, usual, why))
        for pid, parts in by.items():
            d, f, s = g.cat.get(pid), self.live[pid], self.step(pid)
            raw = sum(u for _, u, _, _ in parts)
            units = min(math.ceil(raw / s - 1e-9) * s, self.cap(pid))
            units = round(units, 4)
            label = UNITS.get(d["unit"], (0, d["unit"]))[1]
            unit = parts[0][0]["unit"]
            c, big = self.content(pid, unit), "l" if unit == "ml" else "kg"
            usual = next((u for _, _, u, _ in parts if u), None)
            # A product can be the first usable candidate while still being a
            # different ingredient from the recipe (for example Emmentaler for
            # parmesan after the parmesan products fail a lactose rule). Mark it
            # explicitly so the same fact survives into the signed order.
            conceptual = next((p[0]["item"] for p in parts if p[0]["item"] in PANTRY), None)
            expected = PANTRY.get(conceptual, {}).get("prefer", [None])[0] if conceptual else None
            semantic_sub = bool(conceptual and expected and pid != expected and not _ingredient_matches(conceptual, d))
            sub_for = usual or (expected if semantic_sub else None)
            sub_reason = (f"The recipe names {conceptual}; {d['title']} is used as a substitute because the named product did not pass the dietary checks." if semantic_sub else None)
            ex = sorted(set().union(*[p[0]["cons"]["ex"] for p in parts]))
            lf = any(p[0]["cons"]["lf"] for p in parts) and bool({"AM", "ML"} & set(d["contains"]))
            asked = sorted(order_ex | set(ex))
            # No declaration is not "no allergens": such a line is not passed on data. The approver clears it, once per product and allergen.
            silent = sorted(allergens_unchecked(asked, d))
            silent = [x for x in silent if not (seen.get(pid) or {}).get(x)]
            a_text = (f"The manufacturer declared no allergens for this product, so {_names(silent)} cannot be checked from data. You clear it when you approve; the account remembers." if silent else
                      ("No " + _names(asked) if asked else "Nothing is excluded on this line") + (", and lactose-free by the manufacturer's claim" if lf else "") + "."
                      + ("" if declares_allergens(d) or not asked else " The manufacturer declared no allergens; a person cleared it on this account before.")) + (f" Contains {_names(d['contains'])}." if d["contains"] else "")
            short = pid in shorted
            first = sorted({p[0]["role"] for p in parts if ROLES.get(p[0]["role"], {}).get("first")})
            why = list(dict.fromkeys(w for _, _, _, w in parts if w))
            amount = round(f["price"] * units)
            over = list({t["id"]: t for p in parts if p[2] is None for t in skipped.get((p[0]["item"], tuple(p[0]["cons"]["why"])), [])}.values())[:3]
            req = {**({"exclude_allergens": ex} if ex else {}), **({"claims": ["FREE_FROM:LACTOSE"]} if lf else {})}
            lines.append({
                "product": {"id": pid, "title": d["title"], "brand": d.get("brand")}, "units": units, "unit": label, "quantity": round(units * 10 ** d["scale"]),
                "for": [{"dish": p[0]["dish"], "item": p[0]["item"], "role": p[0]["role"], "portions": p[0]["portions"], "per_portion": f"{p[0]['amount']:g} {p[0]['unit']}", "waste_pct": p[0]["waste"],
                         "diet": p[0]["cons"]["why"], "share": round(p[1] / raw, 3)} for p in parts],
                "checks": {
                    "pack": {"ok": True, "text": f"Needs {num(raw * c / 1000)} {big} with the waste allowance. One {label} holds {c / 1000:g} {big}" + (f", sold in steps of {s:g}" if s != 1 else "") + f": {num(units)} {many(label, units)} = {num(units * c / 1000)} {big}."},
                    "allergens": {"ok": True, "text": a_text, **({"ask": silent} if silent else {})},
                    "stock": {"ok": True, "text": (f"Only {num(self.cap(pid))} {many(label, self.cap(pid))} can be supplied, less than this menu needs." + (f" The {first[0]} gets them first." if first else "") + " The rest is covered on another line or listed under not ordered." if short else "Enough in stock for this order." if f.get("stock_known") else "Delivered to order; no stock limit for this day.")},
                    "price": {"ok": True, "text": f"€{f['price'] / 100:,.2f} per {label}" + (f" (€{f['comparison_price'] / 100:,.2f} per {f['comparison_unit'].lower()})" if f.get("comparison_price") and f.get("comparison_unit") else "") + f", €{amount / 100:,.2f} ex VAT."},
                },
                "amount": amount, "partial": bool(short),
                **({"substitutes_for": {"id": sub_for, "title": g.cat.get(sub_for)["title"]}, "reason": " ".join(dict.fromkeys([w[0] for w in why] + [w[1] for w in why] + ([sub_reason] if sub_reason else [])))} if sub_for else {}),
                **({"requirements": req} if req else {}),
                **({"passed_over": over} if over else {}),
                "source": "search" if not all(p[0]["known"] for p in parts) else "pantry",
                "recipe": sorted({p[0]["source"] for p in parts}),
                "_order": min(dms.index(p[0]) for p in parts),
            })
        lines.sort(key=lambda l: l.pop("_order"))
        items = [{"item": {"id": l["product"]["id"]}, "quantity": l["quantity"],
                  **({"wholesale": {**({"substitutes_for": l["substitutes_for"]["id"]} if l.get("substitutes_for") else {}), **({"requirements": l["requirements"]} if l.get("requirements") else {})}} if l.get("substitutes_for") or l.get("requirements") else {})}
                 for l in lines]
        n_model = sum(1 for d in rd["dishes"] if d["source"] in ("model", "agent"))
        subs = [l for l in lines if l.get("substitutes_for")]
        intent = (f"{rd['title'] or 'Menu'}: {rd['guests']:,} guests" + (f", {rd['date_text'] or rd['date']}" if rd.get("date") or rd.get("date_text") else "") + ". Dishes: " + ", ".join(d["name"] for d in rd["dishes"])
                  + (". Notes: " + "; ".join(rd["notes"]) if rd["notes"] else "") + ".")[:900]
        rationale = (f"Bill of materials worked out in code from {len(rd['dishes']) - n_model} of the kitchen's recipe cards" + (f" and {n_model} proposed recipe(s) that a person should check" if n_model else "")
                     + ": portions x amount per portion + waste allowance, rounded up to pack size. Every line passed pack, allergen, stock and price checks."
                     + (f" {len(subs)} line(s) replace a short product by what the ingredient does in the dish." if subs else "") + (f" {len(opened)} item(s) are left for a person and not ordered." if opened else ""))
        return {
            "status": "ready" if lines else "nothing_to_order", "delivery_date": day, "delivery_note": day_note,
            "requirements": {"exclude_allergens": sorted(order_ex)},
            "diet": [{"note": r["note"], "rule": r["text"]} for r in rules],
            "dishes": [{"name": d["name"], "portions": (d["portions"] or rd["guests"]) * d["days"], "recipe": "the kitchen's card" if d["card"] else "the model's recipe, check it" if d["source"] == "model" else "the agent's recipe, check it",
                        "card": CARDS[d["card"]]["name"] if d["card"] else None, "ingredients": len(CARDS[d["card"]]["ingredients"] if d["card"] else d["recipe"])} for d in rd["dishes"]],
            "lines": lines, "not_ordered": opened,
            "totals": {"lines": len(lines), "subtotal": sum(l["amount"] for l in lines), "currency": "EUR", "allergen_ask": sum(1 for l in lines if l["checks"]["allergens"].get("ask"))},
            "checkout": {"line_items": items, "delivery_date": day, "exclude_allergens": sorted(order_ex), "claims": [], "labels": [], "intent": intent, "rationale": rationale},
        }

    def label(self, pid) -> str:
        d = self.g.cat.get(pid)
        return UNITS.get(d["unit"], (0, d["unit"]))[1]


def delivery_day(g, rd: dict) -> tuple[str, str]:
    """The day before the first serving day, never earlier than tomorrow."""
    first = date.fromisoformat(g._day(None))
    ev = date.fromisoformat(rd["date"]) if rd.get("date") else None
    if not ev:
        return first.isoformat(), "The menu gives no date, so delivery is set to the first possible day."
    if ev <= first:
        return first.isoformat(), f"The menu's date ({nice_day(ev.isoformat())}) is too close or has passed, so delivery is set to the first possible day."
    want = ev - timedelta(days=1)
    return want.isoformat(), f"The day before the first serving day ({nice_day(ev.isoformat())})."


def build_plan(g, rd: dict, principal) -> dict:
    """reading -> checked plan, or the questions a person must answer first. No model is called here."""
    q = sanity(rd)
    rules, q2 = parse_notes(rd) if not any(x["about"] == "guests" for x in q) else ([], [])
    if rd.get("date"):
        ev, today = date.fromisoformat(rd["date"]), datetime.now(TZ).date()
        if ev > today + timedelta(days=120):
            q.append({"about": "date", "text": f"The menu reads as {rd['date']}, more than four months away. Which date is it for?"})
    dms = demands(rd, rules) if not q and not q2 else []
    for r in rules if dms else []:  # a note about one ingredient that matches nothing would change nothing: ask, do not guess
        if r["items"] and not any(_applies(r, dm["dish_i"], dm["item"]) for dm in dms):
            q2.append({"about": "note", "text": f"The note “{r['note']}” is about {', '.join(r['items'])}, and no ingredient on this menu matches that. Reword it or remove it."})
    if q or q2:
        return {"status": "needs_answers", "reading": rd, "questions": q + q2, "diet": [{"note": r["note"], "rule": r["text"]} for r in rules]}
    if not principal or not principal.get("ok"):
        raise GatewayError(401, (principal or {}).get("error", "mandate_required"), (principal or {}).get("detail", "Present the buyer's mandate as Authorization: Bearer <mandate>."))
    day, note = delivery_day(g, rd)
    out = Planner(g, principal).plan(rd, rules, dms, day, note)
    return {**out, "reading": rd, "questions": []}


def plan_menu_order(g, req: dict, principal, trace=None) -> dict:
    """For any agent (REST and MCP): dishes with portions and dietary notes, or an image, in; the checked plan out, ready for create_checkout."""
    if not principal or not principal.get("ok"):
        raise GatewayError(401, (principal or {}).get("error", "mandate_required"), (principal or {}).get("detail", "Present the buyer's mandate as Authorization: Bearer <mandate>."))
    calls = []
    if req.get("image"):
        _spend("agent:" + principal["customer"], 20)
        rd, verdict, calls = read_photo(_image(req["image"]))
        if rd is None:
            return g._prove({"ucp": g._env(), "status": "not_a_menu", "message": NOT_MENU.format(what=verdict["what"] or "something else"), "usage": cost(calls)})
    else:
        rd = normalize({"title": req.get("title"), "date": req.get("event_date") or req.get("date"), "guests": req.get("guests"), "dishes": req.get("dishes") or [],
                        "notes": req.get("notes") if isinstance(req.get("notes"), list) else [req["notes"]] if req.get("notes") else []}, "agent")
        if any(not d["card"] and not d["recipe"] for d in rd["dishes"]):
            _spend("agent:" + principal["customer"], 20)
            calls = fill_recipes(rd)
    plan = build_plan(g, rd, principal)
    plan["usage"] = cost(calls)
    plan["next"] = ("Pass checkout to create_checkout unchanged (line_items, delivery_date, exclude_allergens, intent, rationale). Tell the buyer what is under not_ordered." if plan["status"] == "ready"
                    else "Ask the buyer the questions, fix the request and call plan_menu_order again. Do not order anything from this answer." if plan["status"] == "needs_answers" else "Nothing can be ordered from this menu.")
    g._logp(principal, trace, "menu_plan", (f"Planned a menu order: {len(rd['dishes'])} dishes for {rd['guests']:,} guests, {plan['totals']['lines']} lines, {len(plan['not_ordered'])} not ordered." if plan["status"] == "ready"
                                             else f"A menu could not be planned yet: {len(plan.get('questions') or [])} question(s) for the buyer."), {"status": plan["status"], "dishes": [d["name"] for d in rd["dishes"]]})
    return g._prove({"ucp": g._env(), **plan})


NOT_MENU = "This does not look like a menu or meal plan. It looks like {what}. Nothing was read and nothing is ordered."
_spent: dict[tuple, int] = {}
_spend_lock = threading.Lock()


def _spend(who: str, limit: int, total: int = 600):
    """Caps on model reads: per visitor or account per day, and for the whole server per day."""
    day = datetime.now(TZ).strftime("%Y-%m-%d")
    with _spend_lock:
        if _spent.get((day, who), 0) >= limit or _spent.get((day, "*"), 0) >= total:
            raise GatewayError(429, "busy", "That is a lot of menus for one day. Come back tomorrow.")
        _spent[(day, who)] = _spent.get((day, who), 0) + 1
        _spent[(day, "*")] = _spent.get((day, "*"), 0) + 1


def _image(data: str) -> str:
    if not isinstance(data, str) or not re.match(r"^data:image/(jpeg|png|webp);base64,[A-Za-z0-9+/=\s]+$", data[:64] + "AAAA") or len(data) > 9_000_000:
        raise GatewayError(400, "bad_image", "Send the photo as a JPEG, PNG or WebP data URL under 6 MB.")
    return data


SAMPLES = [
    {"id": "wedding", "file": "wedding.jpg", "title": "Wedding dinner", "sub": "180 guests"},
    {"id": "school", "file": "school.jpg", "title": "School lunch week", "sub": "420 pupils"},
    {"id": "hotel", "file": "hotel.jpg", "title": "Hotel breakfast", "sub": "240 guests"},
    {"id": "notmenu", "file": "notmenu.jpg", "title": "Not a menu", "sub": "a closing checklist, to see the refusal"},
]


# ------------------------------------------------------------------- routes
def build(g, ctx, page) -> APIRouter:
    r = APIRouter()
    lock = threading.RLock()

    def who(request: Request):
        auth = request.headers.get("authorization", "")
        return g.identify(auth[7:].strip() if auth.lower().startswith("bearer ") else None), request.headers.get("x-trace-id")

    def run_of(rid, c):
        run = g.store.get("menu_run:" + str(rid))
        if not run or run["slug"] != c["acct"]["slug"]:
            raise GatewayError(404, "not_found", "No such menu run on this account.")
        return run

    def save(run):
        g.store.put("menu_run:" + run["id"], run)

    def byhand() -> dict:
        """The by-hand time next to the agent's: measured once people have finished /byhand, an estimate until then."""
        done = [a for a in g.store.get("byhand_attempts", []) if a.get("finished")]
        if done:
            people = len({a["who"] for a in done})
            return {"basis": "measured", "minutes": round(statistics.median(a["seconds"] for a in done) / 60, 1), "n": people, "attempts": len(done),
                    "how": f"Median of {len(done)} finished attempt(s) by {people} " + ("person" if people == 1 else "people") + " on the by-hand page, same catalogue, clock from the first key press to checkout."}
        return {"basis": "estimate", "minutes": None, "n": 0, "attempts": 0, "seconds_per_line": BYHAND_S_PER_LINE,
                "how": f"An estimate, not a measurement: {BYHAND_S_PER_LINE} seconds per line to search, compare pack size and allergens, and add to the basket. That figure is our assumption. Nobody has finished the by-hand page yet."}

    def with_estimate(b: dict, lines: int) -> dict:
        return b if b["basis"] == "measured" else {**b, "minutes": round(lines * BYHAND_S_PER_LINE / 60, 1)}

    def view(run) -> dict:
        """What the page gets: never the mandate or the photo."""
        out = {k: run.get(k) for k in ("id", "source", "status", "message", "reading", "questions", "plan", "usage", "seconds", "checkout", "order", "at")}
        out["by_hand"] = with_estimate(byhand(), ((run.get("plan") or {}).get("totals") or {}).get("lines") or 0)
        return out

    def offer(run, principal):
        """The whole basket through the normal checkout: rules, approval link if a rule asks, signed offer."""
        ck = run["plan"]["checkout"]
        t = time.time()
        c = g.create_checkout({"line_items": ck["line_items"], "context": {"intent": ck["intent"]},
                               "wholesale": {"delivery_date": ck["delivery_date"], "rationale": ck["rationale"], "requirements": {"exclude_allergens": ck["exclude_allergens"]}}}, principal, run["trace"])
        run["seconds"]["offer"] = round(time.time() - t, 2)
        rules = c["wholesale"]["policy"]["rules"]
        run["checkout"] = {"id": c["id"], "status": c["status"], "decision": c["wholesale"]["policy"]["decision"], "continue_url": c.get("continue_url"), "expires_at": c.get("expires_at"),
                           "totals": c["totals"], "lines": len(c["line_items"]),
                           "rules": [{"id": x["id"], "title": x["title"], "outcome": x["outcome"], "detail": x["detail"]} for x in rules],
                           "errors": [m["content"] for m in c["messages"] if m.get("type") == "error" and m.get("code") not in ("approval_required",)],
                           "signed": bool((c.get("ap2") or {}).get("merchant_authorization"))}

    def principal_of(run):
        return g.identify(run["mandate"])

    def drop_open(slug):
        """Cancel the offer this account's last menu run left open, so the stock it holds is free again."""
        old = g.store.get("menu_run:" + str(g.store.get("menu_open:" + slug) or ""))
        if old and not old.get("order") and (old.get("checkout") or {}).get("id"):
            try:
                g.cancel_checkout(old["checkout"]["id"], principal_of(old))
            except GatewayError:
                pass
            old["checkout"] = None
            save(old)

    @r.get("/menu", include_in_schema=False)
    def menu_page():
        return page("menu.html")

    @r.get("/byhand", include_in_schema=False)
    def byhand_page():
        return page("byhand.html")

    @r.get("/api/menu/samples", include_in_schema=False)
    def samples():
        return {"samples": [{**s, "url": "/static/samples/" + s["file"], "label": "sample photo"} for s in SAMPLES if (WEB / "samples" / s["file"]).exists()]}

    @r.post("/api/menu/read", include_in_schema=False)
    def read(request: Request, response: Response, body: dict = Body(...)):
        """Step 1: is it a menu, and what does it say. Starts the clock."""
        c, t0 = ctx(request, response), time.time()
        slug = c["acct"]["slug"]
        _spend("web:" + c["sid"], 40)
        sample = next((s for s in SAMPLES if s["id"] == body.get("sample")), None)
        if sample:
            raw = (WEB / "samples" / sample["file"]).read_bytes()
            image, source = "data:image/jpeg;base64," + base64.b64encode(raw).decode(), f"sample photo: {sample['title'].lower()}"
        else:
            image, source = _image(body.get("image")), "own photo"
        rid = "mr_" + secrets.token_hex(6)
        run = {"id": rid, "slug": slug, "source": source, "sample": sample["id"] if sample else None, "t0": t0, "at": iso(t0), "trace": "menu-" + rid[3:], "seconds": {},
               "mandate": g.issue_mandate(slug, agent=f"agent:{slug}/menu", name="Menu agent")}
        drop_open(slug)  # a new photo replaces the offer left open by the last one and frees its stock
        g.store.put("menu_open:" + slug, rid)
        rd, verdict, calls = read_photo(image)
        run["seconds"]["read"] = round(time.time() - t0, 2)
        run["calls"], run["usage"] = calls, cost(calls)
        p = principal_of(run)
        if rd is None:
            run["status"], run["message"] = "not_a_menu", NOT_MENU.format(what=verdict["what"] or "something else")
            g._logp(p, run["trace"], "menu_refused", f"A photo was refused: it is not a menu ({verdict['what'] or 'something else'}). Nothing was read or ordered.", {"run": rid})
        else:
            q = sanity(rd)
            rules, q2 = parse_notes(rd) if not any(x["about"] == "guests" for x in q) else ([], [])
            run["reading"], run["questions"] = rd, q + q2
            run["status"] = "needs_answers" if q + q2 else "read"
            g._logp(p, run["trace"], "menu_read", f"Read a menu from a photo: {len(rd['dishes'])} dishes for {rd['guests'] if rd['guests'] is not None else 'an unknown number of'} guests, {len(rd['notes']) + sum(len(d['notes']) for d in rd['dishes'])} dietary notes."
                    + (f" {len(q + q2)} thing(s) to ask before anything is ordered." if q + q2 else ""), {"run": rid, "model": READ_MODEL, "tokens_in": run["usage"]["tokens_in"], "tokens_out": run["usage"]["tokens_out"]})
        save(run)
        return view(run)

    @r.post("/api/menu/plan", include_in_schema=False)
    def plan(request: Request, response: Response, body: dict = Body(...)):
        """Step 2: the bill of materials and one signed offer. Takes the person's corrections to the reading when there were questions."""
        c = ctx(request, response)
        run = run_of(body.get("run"), c)
        if run.get("order"):
            return view(run)
        p, t = principal_of(run), time.time()
        if isinstance(body.get("reading"), dict):
            rd = normalize(body["reading"], "model")
            if any(not d["card"] and not d["recipe"] and d["food"] for d in rd["dishes"]):
                _spend("web:" + c["sid"], 40)
                more = fill_recipes(rd)
                run["calls"] = run.get("calls", []) + more
                run["usage"] = cost(run["calls"])
            run["reading"], run["edited"] = rd, True
        if not run.get("reading"):
            raise GatewayError(409, "invalid_state", run.get("message") or "There is no reading to plan from.")
        if (run.get("checkout") or {}).get("id"):
            try:
                g.cancel_checkout(run["checkout"]["id"], p)  # a new plan replaces the old offer and frees its stock
            except GatewayError:
                pass
            run["checkout"] = None
        out = build_plan(g, run["reading"], p)
        run["status"], run["questions"] = out["status"], out.get("questions") or []
        run["plan"] = {k: v for k, v in out.items() if k not in ("reading", "questions")} if out["status"] in ("ready", "nothing_to_order") else None
        if out["status"] == "ready":
            offer(run, p)
            run["seconds"]["plan"] = round(time.time() - t, 2)  # the bill of materials, the checks and the signed offer
            g._logp(p, run["trace"], "menu_plan", f"Planned the menu in code: {out['totals']['lines']} lines, {len(out['not_ordered'])} not ordered, delivery {nice_day(out['delivery_date'])}.", {"run": run["id"], "checkout": run["checkout"]["id"]})
        save(run)
        return view(run)

    @r.post("/api/menu/sign", include_in_schema=False)
    def sign(request: Request, response: Response, body: dict = Body(...)):
        """For an account with an enrolled approver's device: what that device signs to approve this menu's order."""
        c = ctx(request, response)
        run = run_of(body.get("run"), c)
        ck = run.get("checkout") or {}
        if not ck.get("id") or run.get("order"):
            raise GatewayError(409, "invalid_state", "There is no offer waiting for a person on this menu.")
        pre = passkey.prepare(g, ck["id"], True, MENU_APPROVER, MENU_NOTE, request)
        pre.pop("account")
        return pre

    @r.post("/api/menu/order", include_in_schema=False)
    def order(request: Request, response: Response, body: dict = Body(...)):
        """Step 3: the person's one tap. Approves what the rules sent to a person, then the agent signs and the order is placed."""
        c = ctx(request, response)
        with lock:
            run = run_of(body.get("run"), c)
            if run.get("order"):
                return view(run)
            ck = run.get("checkout") or {}
            if not ck.get("id") or ck.get("status") not in ("ready_for_complete", "requires_escalation"):
                raise GatewayError(409, "invalid_state", "There is no offer to order from. Plan the menu first.")
            p, t = principal_of(run), time.time()
            run["seconds"]["person"] = round(t - run["t0"] - sum(run["seconds"].get(k, 0) for k in ("read", "plan")), 2)
            if ck["status"] == "requires_escalation" and g.get_checkout(ck["id"], p)["status"] == "requires_escalation":  # not yet approved elsewhere
                token = g.store.checkout(ck["id"])["_internal"]["approval_token"]
                # An account with an enrolled approver's device approves with that device here too: the tap alone is refused (device_required).
                device = passkey.check(g, ck["id"], token, True, MENU_APPROVER, MENU_NOTE, body.get("device"), request)
                g.decide(ck["id"], token, True, MENU_APPROVER, MENU_NOTE, **({"device": device} if device else {}))
            cur = g.get_checkout(ck["id"], p)
            done = g.complete_checkout(ck["id"], {"ap2": {"checkout_mandate": g.agent_proof(cur, run["mandate"])}}, p)
            if done.get("status") != "completed" or not done.get("order"):
                ck["status"], ck["errors"] = done.get("status"), [m["content"] for m in done.get("messages", []) if m.get("type") in ("error", "warning")][-3:]
                save(run)
                return view(run)
            now = time.time()
            run["seconds"]["order"], run["seconds"]["total"] = round(now - t, 2), round(now - run["t0"], 2)
            ck["status"] = "completed"
            run["order"] = {**done["order"], "at": iso(now), "approved_by_person": bool(run["checkout"].get("continue_url"))}
            run["status"] = "ordered"
            save(run)
            pl = run["plan"]
            g.store.put("menu_latest", {
                "run": run["id"], "at": iso(now), "source": run["source"], "edited": bool(run.get("edited")), "seconds": run["seconds"]["total"], "phases": run["seconds"],
                "guests": run["reading"]["guests"], "dishes": len(run["reading"]["dishes"]), "title": run["reading"]["title"] if run.get("sample") else None,
                "line_count": pl["totals"]["lines"], "subtotal": pl["totals"]["subtotal"], "not_ordered": len(pl["not_ordered"]), "replaced_by_role": sum(1 for l in pl["lines"] if l.get("substitutes_for")),
                "lines": [{"title": l["product"]["title"], "units": l["units"], "unit": l["unit"], "ok": True} for l in pl["lines"]],
                "cost": {k: run["usage"][k] for k in ("tokens_in", "tokens_out", "usd", "eur", "basis")}, "models": sorted({x["model"] for x in run.get("calls", [])}),
                "order": {"id": done["order"]["id"], "label": done["order"].get("label"), "url": order_url(done["order"]["id"])}, "delivery_date": pl["delivery_date"],
            })
            g._logp(p, run["trace"], "menu_order", f"A menu photo became order {done['order'].get('label')}: {pl['totals']['lines']} lines in {run['seconds']['total']:.0f} s from photo to order, model cost €{run['usage']['eur']:.4f}.",
                    {"run": run["id"], "order": done["order"]["id"], "seconds": run["seconds"], "tokens_in": run["usage"]["tokens_in"], "tokens_out": run["usage"]["tokens_out"]})
            return view(run)

    @r.get("/api/menu/run/{rid}", include_in_schema=False)
    def get_run(rid: str, request: Request, response: Response):
        return view(run_of(rid, ctx(request, response)))

    @r.get("/api/menu/latest", tags=["menu"], summary="The last menu photo that became an order: seconds, lines, model cost, and the by-hand time (measured or estimated)")
    def latest():
        m = g.store.get("menu_latest")
        if not m:
            return {"none": True, "lines": [], "by_hand": byhand(), "note": "No menu photo has become an order on this server yet."}
        m = {**m, "order": {**m["order"], "url": order_url(m["order"]["id"])}} if (m.get("order") or {}).get("id") else m
        return {**m, "real": True, "by_hand": with_estimate(byhand(), m["line_count"])}

    # ---- test control: make a product short, in this account's own pool of stock
    def short_ref(c):
        return "menu_short:" + (c["acct"].get("world") or "main")

    def short_state(c):
        s = g.store.get(short_ref(c))
        return s if s and s["until"] > time.time() else None

    @r.get("/api/menu/short", include_in_schema=False)
    def short_get(request: Request, response: Response):
        return {"short": short_state(ctx(request, response)), "default": {"id": PANTRY["whipping cream"]["prefer"][0], "title": g.cat.get(PANTRY["whipping cream"]["prefer"][0])["title"]}}

    @r.post("/api/menu/short", include_in_schema=False)
    def short_set(request: Request, response: Response, body: dict = Body(...)):
        """Pretend other customers reserved the stock: holds all but `leave` units for two hours. Valio Aimo's data is not changed."""
        c = ctx(request, response)
        world = c["acct"].get("world") or "main"
        if world == "main" and not c["op"]:
            raise GatewayError(403, "forbidden", "This needs the operator key.")
        pid = str(body.get("product") or PANTRY["whipping cream"]["prefer"][0])
        d, leave = g.cat.get(pid), max(0.0, float(body.get("leave") or 0))
        if not d:
            raise GatewayError(404, "not_found", "No such product.")
        ref, day = short_ref(c), g._day(body.get("day"))
        drop_open(c["acct"]["slug"])  # the offer on screen holds this stock too; it is planned again after this
        f = g._live([pid], day, {"ok": True, "customer": c["acct"]["customer"], "world": world}, exclude_ref=ref)[pid]
        if not f.get("stock_known"):
            raise GatewayError(409, "not_tracked", f"{d['title']} is delivered to order, so there is no stock figure to make short.")
        hold, until = max(0.0, f["available"] - leave), time.time() + 2 * 3600
        g.store.hold(ref, [(pid, hold)], until, world)
        s = {"product": pid, "title": d["title"], "leave": min(leave, f["available"]), "held": hold, "until": until, "until_iso": iso(until)}
        g.store.put(ref, s)
        g.store.log(None, "demo:control", "demo", f"Test control: {d['title']} made short on this account, {s['leave']:g} left for two hours. Valio Aimo's data is not changed.", {"product": pid, "world": world}, acct=c["acct"]["customer"])
        return {"short": s}

    @r.post("/api/menu/short/clear", include_in_schema=False)
    def short_clear(request: Request, response: Response):
        c = ctx(request, response)
        if (c["acct"].get("world") or "main") == "main" and not c["op"]:
            raise GatewayError(403, "forbidden", "This needs the operator key.")
        g.store.settle(short_ref(c), "released")
        g.store.put(short_ref(c), None)
        return {"short": None}

    # ---- open to any agent
    @r.post("/menu/plan", tags=["menu"], summary="Dishes with portions and dietary notes, or a photo, in; a checked plan out, ready for create_checkout")
    def menu_plan(request: Request, body: dict = Body(...)):
        p, tr = who(request)
        g.rate_limit(p, request.client.host if request.client else None)
        return plan_menu_order(g, body, p, tr)

    # ---- by hand, timed
    def task_of(c, rid=None) -> dict:
        run = g.store.get("menu_run:" + str(rid)) if rid else None
        if not run or run["slug"] != c["acct"]["slug"] or not run.get("plan"):
            last = g.store.get("menu_latest")
            run = g.store.get("menu_run:" + last["run"]) if last else None
        if run and run.get("plan"):
            pl, rd, src = run["plan"], run["reading"], run["source"] if run.get("sample") else "a menu ordered on this server"
        else:  # nobody has ordered from a menu here yet: the wedding sample, typed, planned in code (no model)
            rd = normalize(WEDDING, "card")
            pl, src = build_plan(g, rd, {"ok": True, "customer": c["acct"]["customer"], "world": c["acct"].get("world") or "main"}), "the wedding sample menu"
        need = {}
        for l in pl["lines"]:
            for f in l["for"]:
                need.setdefault(f["item"], {"item": f["item"], "dishes": set(), "lines": set()})
                need[f["item"]]["dishes"].add(f["dish"]); need[f["item"]]["lines"].add(l["product"]["id"])
        amounts = {}
        for l in pl["lines"]:
            m = re.search(r"Needs ([\d.,]+) (kg|l)", l["checks"]["pack"]["text"])
            for f in l["for"]:
                amounts[f["item"]] = amounts.get(f["item"], 0) + (float(m.group(1).replace(",", "")) * f["share"] if m else 0)
                need[f["item"]]["unit"] = m.group(2) if m else ""
        items = [{"item": k, "amount": round(amounts[k], 1) if amounts[k] >= 1 else round(amounts[k], 2), "unit": v.get("unit", ""), "dishes": sorted(v["dishes"])} for k, v in need.items()]
        return {"from": src, "title": rd["title"] or "Menu", "guests": rd["guests"], "delivery_date": pl["delivery_date"], "notes": rd["notes"] + [f"{d['name']}: {', '.join(d['notes'])}" for d in rd["dishes"] if d["notes"]],
                "exclude_allergens": pl["requirements"]["exclude_allergens"], "list": items, "agent_lines": pl["totals"]["lines"]}

    @r.get("/api/byhand/task", include_in_schema=False)
    def byhand_task(request: Request, response: Response, run: str = ""):
        return {**task_of(ctx(request, response), run), "by_hand": byhand()}

    @r.get("/api/byhand/search", include_in_schema=False)
    def byhand_search(q: str = ""):
        """The bare webshop: catalogue rows only. No live price or stock call per search, to be gentle on Valio Aimo's API; prices come at checkout."""
        docs, _, _ = g.cat.search(q[:80], {}, limit=24) if q.strip() else ([], None, None)
        row = lambda d: {"id": d["id"], "title": d["title"], "brand": d.get("brand"), "unit": UNITS.get(d["unit"], (0, d["unit"]))[1], "step": d["increment"] / 10 ** d["scale"],
                         "pack": " / ".join(f"{v:g} {k}" for k, v in d["content"].items()) or "pack size not in the data",
                         "contains": [ALLERGENS.get(x, x) for x in d["contains"]], "may_contain": [ALLERGENS.get(x, x) for x in d["may_contain"]],
                         "claims": [x.split(":")[1].replace("_", " ").lower() + ("-free" if x.startswith("FREE_FROM") else "") for x in d["claims"] if x in ("FREE_FROM:LACTOSE", "FREE_FROM:GLUTEN", "DIET:VEGAN")]}
        return {"products": [row(d) for d in docs]}

    @r.post("/api/byhand/start", include_in_schema=False)
    def byhand_start(request: Request, response: Response, body: dict = Body(default={})):
        c = ctx(request, response)
        aid = "bh_" + secrets.token_hex(5)
        g.store.put("byhand:" + aid, {"id": aid, "slug": c["acct"]["slug"], "who": hashlib.sha256(c["sid"].encode()).hexdigest()[:10], "t0": time.time(), "run": str(body.get("run") or "")})
        return {"attempt": aid}

    @r.post("/api/byhand/checkout", include_in_schema=False)
    def byhand_checkout(request: Request, response: Response, body: dict = Body(...)):
        """Stops the clock and checks the basket in code. Nothing is ordered."""
        c, now = ctx(request, response), time.time()
        a = g.store.get("byhand:" + str(body.get("attempt")))
        if not a or a["slug"] != c["acct"]["slug"]:
            raise GatewayError(404, "not_found", "No such attempt.")
        if a.get("seconds"):
            return a["result"]
        task = task_of(c, a.get("run"))
        basket = [(str(b.get("id")), float(b.get("units") or 0)) for b in (body.get("basket") or [])[:200] if g.cat.get(str(b.get("id"))) and float(b.get("units") or 0) > 0]
        live = g._live([pid for pid, _ in basket], task["delivery_date"], {"ok": True, "customer": c["acct"]["customer"], "world": c["acct"].get("world") or "main"}) if basket else {}
        ex, rows, total = set(task["exclude_allergens"]), [], 0
        for pid, units in basket:
            d, f = g.cat.get(pid), live[pid]
            hits = allergen_hits(ex, d["contains"], d["claims"]) | allergen_hits(ex, d["may_contain"], d["claims"])
            ok = bool(f.get("orderable")) and not (f.get("stock_known") and units > f["available"])
            amount = round(f["price"] * units) if f.get("price") else 0
            total += amount if ok else 0
            rows.append({"id": pid, "title": d["title"], "units": units, "amount": amount, "orderable": ok, "why": None if ok else REASONS.get(f.get("reason"), "not enough stock") if not f.get("orderable") else "not enough stock",
                         "breaks": f"contains or may contain {_names(hits)}, which this menu rules out" if hits else None})
        secs = round(now - a["t0"], 1)
        finished = len(basket) >= math.ceil(0.8 * len(task["list"]))
        res = {"seconds": secs, "minutes": round(secs / 60, 1), "lines": len(basket), "list_lines": len(task["list"]), "finished": finished, "subtotal": total, "rows": rows,
               "breaks": sum(1 for x in rows if x["breaks"]), "cannot_order": sum(1 for x in rows if not x["orderable"]), "agent_lines": task["agent_lines"],
               "counts": "This attempt counts in the measured time." if finished else f"This attempt is not counted: the basket has {len(basket)} of {len(task['list'])} items on the list (at least {math.ceil(0.8 * len(task['list']))} are needed)."}
        a.update({"seconds": secs, "result": res})
        g.store.put("byhand:" + a["id"], a)
        with lock:
            g.store.put("byhand_attempts", (g.store.get("byhand_attempts", []) + [{"id": a["id"], "who": a["who"], "at": iso(now), "seconds": secs, "lines": len(basket), "list_lines": len(task["list"]), "finished": finished, "breaks": res["breaks"]}])[-300:])
        return {**res, "by_hand": byhand()}

    return r


# The wedding sample, typed. Used by /byhand before anyone has ordered from a menu, and by tests.
WEDDING = {"title": "Wedding dinner", "date": "2026-11-21", "date_text": "Sat 21 Nov", "guests": 180, "notes": ["Nut-free kitchen", "Table 6 lactose-free"],
           "dishes": [{"name": "Creamy salmon soup", "card": "salmon_soup", "notes": ["L"]}, {"name": "Green salad", "card": "green_salad"},
                      {"name": "Chicken breast with thyme cream sauce", "card": "chicken_thyme_cream", "notes": ["L", "G"]}, {"name": "Roasted potatoes and carrots", "card": "roast_potatoes"},
                      {"name": "Mushroom risotto", "card": "mushroom_risotto", "portions": 14, "notes": ["vegetarian"]}, {"name": "Strawberry cream cake", "card": "strawberry_cream_cake"},
                      {"name": "Coffee and tea", "card": "coffee_tea"}, {"name": "Rye bread and butter", "card": "bread_butter"}]}
