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

from .catalog import ALLERGENS, UNITS, allergen_hits, allergens_unchecked, declares_allergens, fold, kind, named_hits, widen_allergens
from .config import DATA as DATA_DIR, STAGE, TZ, WEB, order_url
from . import passkey
from .service import REASONS, GatewayError, iso, nice_day

MENU_APPROVER, MENU_NOTE = "Kitchen manager, on the menu page", "Approved with the menu and the checked lines in view."
MENU_ASKER = "Kitchen manager, on the menu page"  # who asks for a menu order; the approver is someone else, on the approval page

DATA = json.loads((Path(__file__).parent / "recipes.json").read_text())
PANTRY, FAT, WASTE = DATA["pantry"], DATA["fat_pct"], DATA["waste_pct_default"]
CARDS = {c["id"]: c for c in DATA["cards"]}
FAMILY = {pid: p["family"] for p in PANTRY.values() if p.get("family") for pid in p["prefer"]}

# One frontier model for both jobs, the one Google makes for a person who is waiting: the guard that says whether the photo is a menu at all, and the read.
GUARD_MODEL = READ_MODEL = "gemini-4-hydrogen"
# Google's paid-tier list prices in USD per million tokens (aistudio.google.com/docs/pricing, read 14 Nov 2026), and the ECB reference rate of 13 Nov 2026.
PRICES = {"gemini-4-hydrogen": (1.50, 7.50)}
USD_PER_EUR = 1.1052
COST_BASIS = "Google's list prices on 14 Nov 2026 (Gemini 4 Hydrogen $1.50 in and $7.50 out per million tokens), at the ECB rate of 13 Nov 2026 (1 EUR = 1.1052 USD)."

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
def _allergy(en: str, fi: str) -> str:
    """How a note rules an allergen out: "peanut allergy", "nut-free", "no nuts", "allergic to sesame", "pähkinätön", "maapähkinäallergia"."""
    return (rf"\b(?:{en})s?[- ]?(?:free|allerg\w*|intoleran\w*)|\b(?:no|without|avoid\w*|allergic to|allerg\w* to|allerg\w*:)\s*(?:{en})s?\b"
            rf"|(?:{fi})(?:ton\w*|tto\w*|allergi\w*)\b|\b(?:ei|ilman)\s+(?:{fi})\w*")  # pähkinätön, pähkinätöntä, pähkinättömänä


DIETS = [
    ("peanut", _allergy("peanut", "maapahkina|jordnot")),
    ("nut", _allergy("tree nut|nut", "(?<!maa)pahkina|(?<!jord)not") + r"|\bnotfri"),
    ("sesame", _allergy("sesame", "seesami")),
    ("soy", _allergy("soya|soy", "soija")),
    ("shellfish", _allergy("shellfish|crustacean|shrimp|prawn", "ayriais|katkarapu")),
    ("celery", _allergy("celery", "selleri")),
    ("mustard", _allergy("mustard", "sinapi|sinappi")),
    ("lactose", r"lactose[- ]?free|lactose intoleran\w*|laktoosit\w*|laktosfri|\blf\b|^\(?l\)?$"),
    ("gluten", r"gluten[- ]?free|gluteenit\w*|glutenfri|coeliac\w*|celiac\w*|keliaa?k\w*|\bgf\b|^\(?g\)?$"),
    ("milk", r"milk[- ]?free|dairy[- ]?free|maidot\w*|mjolkfri|no milk|no dairy|milk allerg\w*|maitoallergi\w*|^\(?m\)?$"),
    ("vegan", r"\bvegan\w*|vegaani\w*|^\(?vg\)?$|^\(?veg?\)?$"),
    ("vegetarian", r"vegetarian\w*|kasvis\w*|\bveg\b|^\(?v\)?$|^\(?k\)?$"),
    ("egg", r"egg[- ]?free|no eggs?|egg allerg\w*|munat\w*|kananmunat\w*|muna-?allergi\w*"),
    ("fish", r"fish[- ]?free|no fish|fish allerg\w*|kalat\w*|kala-?allergi\w*"),
]
# The same allergens named bare ("allergies: peanuts, sesame"). Only read this way in a note that speaks of an allergy.
BARE = [("peanut", r"peanuts?|maapahkina\w*"), ("nut", r"\b(?:tree )?nuts?\b|(?<!maa)pahkina\w*"), ("sesame", r"sesame|seesami\w*"), ("soy", r"\bsoya?\b|soija\w*"),
        ("shellfish", r"shellfish|crustaceans?|shrimps?|prawns?|ayriaiset|katkarapu\w*"), ("celery", r"celery|selleri\w*"), ("mustard", r"mustard|sinappi\w*"),
        ("egg", r"\beggs?\b|kananmuna\w*"), ("fish", r"\bfish\b|\bkala\b"), ("milk", r"\bmilk\b|\bdairy\b|\bmaito\b"), ("gluten", r"gluten|gluteeni\w*|\bwheat\b|vehna\w*")]
DIET_WORD = {"nut": "nut-free", "peanut": "peanut-free", "sesame": "sesame-free", "soy": "soy-free", "shellfish": "shellfish-free", "celery": "celery-free", "mustard": "mustard-free",
             "lactose": "lactose-free", "gluten": "gluten-free", "milk": "milk-free", "vegan": "vegan", "vegetarian": "vegetarian", "egg": "egg-free", "fish": "fish-free"}
# What each kind of note rules out. A nut note rules out peanuts too, as a kitchen would; a peanut note does not rule out tree nuts.
EXCODES = {"peanut": {"AP"}, "nut": {"AN", "AP"}, "sesame": {"AS"}, "soy": {"AY"}, "shellfish": {"AC", "UM"}, "celery": {"BC"}, "mustard": {"BM"},
           "gluten": {"AW"}, "milk": {"AM", "ML"}, "egg": {"AE"}, "fish": {"AF"}, "vegan": {"AM", "ML", "AE", "AF"}}
# These have no stand-in in the pantry. With a head count they are applied to those guests' portions; without one, or written about the whole kitchen, to the whole order.
HARD = ("peanut", "nut", "sesame", "soy", "shellfish", "celery", "mustard")
KITCHEN = re.compile(r"kitchen|keittio|whole (?:event|order|menu|party)|every(?:one|body)|all guests|all portions|kaikille|kaikki|koko ")
SEVERE = re.compile(r"severe|serious|anaphyla\w*|life[- ]threatening|epipen|vakava\w*|hengenvaarall\w*|anafyla\w*")
SCOPE_WORDS = ["bread", "milk", "yoghurt", "yogurt", "cheese", "butter", "cream", "pasta", "macaroni", "porridge", "muesli", "coffee", "croissant", "pancake", "oat", "flour", "margarine", "spread"]
num = lambda x: f"{x:,.0f}" if abs(x - round(x)) < 0.05 else f"{x:,.1f}"
PLURAL = {"piece": "pcs", "case": "cases", "bag": "bags", "bottle": "bottles", "pack": "packs", "box": "boxes", "sack": "sacks", "jar": "jars", "carton": "cartons", "roll": "rolls"}
many = lambda label, n: label if n == 1 or label not in PLURAL else PLURAL[label]


def qty(x: float, unit: str) -> str:
    """An amount of g or ml in the unit a person reads. Under a kilo or a litre it is grams or millilitres, never "0 kg"."""
    big, small = ("l", "ml") if unit == "ml" else ("kg", "g")
    if x < 999.5:
        return f"{(round(x) if x >= 10 else round(x, 1)):g} {small}"
    return f"{num(x / 1000)} {big}"


# ------------------------------------------------- what a dish is named for
# (what it is, allergen codes, how a dish name or an ingredient says it). Finnish stems match inside compound words
# ("lohikeitto"). Checked top to bottom and each match is taken out of the text, so "kananmuna" is egg and never chicken.
_N = lambda label, codes, pat, items=None: {"label": label, "codes": set(codes.split()) if codes else set(), "name": re.compile(pat), "item": re.compile(items or pat)}
NAMED = [
    _N("peanuts", "AP", r"peanuts?|maapahkin\w*|\bsatay\b|\bsate\b|pad thai|kung pao|gado[- ]gado", r"peanut|maapahkin|satay"),
    _N("almonds", "AN", r"almonds?|manteli\w*|marzipan|marsipaani|frangipane|\bmacarons?\b|amarett\w+", r"almond|manteli|marzipan|marsipaani|amarett"),
    _N("hazelnuts", "AN", r"hazelnuts?|hasselpahkin\w*|pralines?|nougat|gianduja", r"hazelnut|hasselpahkin|praline|nougat|nut spread"),
    _N("walnuts", "AN", r"walnuts?|saksanpahkin\w*|waldorf", r"walnut|saksanpahkin"),
    _N("cashews", "AN", r"cashews?"), _N("pistachios", "AN", r"pistachios?|pistaasi\w*"), _N("pecans", "AN", r"pecans?"),
    _N("nuts", "AN", r"\bnuts?\b|\bnutty\b|pahkin\w*|baklava", r"\bnuts?\b|pahkin|almond|hazelnut|walnut|cashew|pistachio|pecan|praline"),
    _N("sesame", "AS", r"sesame|seesami\w*|tahini|hummus|halva", r"sesame|seesami|tahini"),
    _N("chickpeas", "", r"chickpeas?|kikhern\w*|falafel", r"chickpea|kikhern|garbanzo"),
    _N("egg", "AE", r"\beggs?\b|omelet\w*|frittata|kananmun\w*|munakas\w*|munavoi\w*|\bmuna(?!koiso)\w*|carbonara|mayonnaise|majoneesi\w*|aioli|meringue|marenki\w*|quiche|pavlova", r"\begg|muna|mayonnaise|majoneesi|aioli|meringue"),
    _N("salmon", "AF", r"salmon|lohi|lohta|lohen|gravlax", r"salmon|lohi|lohta|trout"),
    _N("tuna", "AF", r"\btuna\b|tonnikala\w*", r"tuna|tonnikala"),
    _N("herring", "AF", r"herring|silakka\w*|silakat|silli\w*", r"herring|silakka|silli"),
    _N("shellfish", "AC", r"shrimps?|prawns?|katkarapu\w*|\bcrab\b|\brapu\w*|lobster|hummeri\w*|crayfish|scampi", r"shrimp|prawn|katkarapu|crab|rapu|lobster|hummeri|crayfish|scampi"),
    _N("mussels", "UM", r"mussels?|simpuk\w*|\bclams?\b|squid|calamari|octopus|scallops?|oysters?", r"mussel|simpuk|clam|squid|calamari|octopus|scallop|oyster"),
    _N("fish", "AF", r"\bfish\b|kala(?!mata)\w*|\bcod\b|saithe|seiti\w*|pollock|trout|kirjolohi|zander|\bkuha\w*|perch|ahven\w*|whitefish|siika\w*|vendace|muik\w*|anchov\w*|sardines?|mackerel|makrilli\w*",
       r"fish|kala|\bcod\b|saithe|seiti|pollock|trout|zander|kuha|perch|ahven|siika|vendace|muikku|anchov|sardine|mackerel|makrilli|salmon|lohi|tuna|herring|silakka|hake|haddock|tilapia|pangasius"),
    _N("turkey", "", r"\bturkey\b|kalkkuna\w*"),
    _N("chicken", "", r"chicken|broiler\w*|\bkana\w*|kanaa\b", r"chicken|broiler|kana|poultry"),
    _N("minced meat", "", r"minced? (?:meat|beef|pork)|ground (?:beef|meat)|jauheliha\w*|\bmince\b|bolognese|lihapull\w*|lihapyoryk\w*|meatballs?|meat ?loaf|lihamureke\w*", r"mince|ground|jauheliha|beef|pork|meat|liha"),
    _N("beef", "", r"\bbeef\b|\bnauta\w*|naudan\w*|haranliha\w*", r"beef|nauta|naudan|veal|steak|\box\b|entrecote|sirloin|chuck|brisket|haran"),
    _N("pork", "", r"\bpork\b|porsa\w*|possu\w*|sianliha\w*|\bsian\w*|kassler\w*|kinkku\w*|\bham\b|bacon|pekoni\w*|pulled pork", r"pork|porsa|possu|sian|kassler|kinkku|ham\b|bacon|pekoni"),
    _N("lamb", "", r"\blamb\b|lammas\w*|lampaan\w*|karitsa\w*", r"lamb|lammas|lampaan|karitsa|mutton"),
    _N("sausage", "", r"sausages?|makkara\w*|nakki(?!leip)\w*|nakit|frankfurters?|chorizo|bratwurst|hot dogs?", r"sausage|makkara|nakki(?!leip)|frankfurter|chorizo|wurst|wiener"),
    _N("reindeer or game", "", r"reindeer|\bporo\w*|venison|riista\w*|hirvi\w*|hirven\w*|\belk\b|moose", r"reindeer|poro|venison|riista|hirvi|hirven|elk|moose|game"),
    _N("liver", "", r"\bliver\b|maksa\w*", r"liver|maksa"),
    _N("cauliflower", "", r"cauliflower|kukkakaali\w*"), _N("broccoli", "", r"broccoli|parsakaali\w*"),
    _N("cabbage", "", r"cabbage|coleslaw|kaali\w*", r"cabbage|kaali"),
    _N("potato", "", r"potato(?:es)?|peruna\w*|perunat|muusi\w*|french fries|ranskalaiset", r"potato|peruna|fries|mash|rosti"),
    _N("carrot", "", r"carrots?|porkkana\w*", r"carrot|porkkana"),
    _N("beetroot", "", r"beetroots?|\bbeets?\b|punajuur\w*", r"beet|punajuur"),
    _N("swede", "", r"\bswede\b|rutabaga|lanttu\w*", r"swede|rutabaga|lanttu|turnip"),
    _N("spinach", "", r"spinach|pinaatti\w*", r"spinach|pinaatti"),
    _N("peas", "", r"\bpeas?\b|herne\w*", r"\bpeas?\b|herne"),
    _N("beans", "", r"\bbeans?\b|\bpapu\w*|pavut", r"bean|papu|pavut"),
    _N("lentils", "", r"lentils?|linssi\w*", r"lentil|linssi"),
    _N("mushrooms", "", r"mushrooms?|sieni\w*|sienet|herkkusien\w*|kantarell\w*|chanterelles?|porcini|tatti\w*", r"mushroom|sieni|sienet|champignon|chanterelle|kantarell|porcini|tatti|funghi"),
    _N("garlic", "", r"garlic|valkosipuli\w*", r"garlic|valkosipuli"),
    _N("tomato", "", r"tomato(?:es)?|tomaatti\w*", r"tomato|tomaatti|passata"),
    _N("onion", "", r"\bonions?\b|sipuli\w*", r"onion|sipuli|shallot"),
    _N("leek", "", r"\bleeks?\b|purjo\w*", r"leek|purjo"),
    _N("pumpkin or squash", "", r"pumpkin|kurpitsa\w*|squash|butternut", r"pumpkin|kurpitsa|squash|butternut"),
    _N("rice", "", r"\brice\b|riisi\w*|risotto", r"rice|riisi|arborio"),
    _N("pasta", "", r"pasta|spaghetti|penne|tagliatelle|fusilli|macaroni|makaroni\w*|lasagn[ea]\w*|noodles?|nuudeli\w*|carbonara|bolognese", r"pasta|spaghetti|penne|tagliatelle|fusilli|macaroni|makaroni|lasagn|noodle|nuudeli"),
    _N("rye", "", r"\brye\b|ruis\w*", r"rye|ruis"),
    _N("bread", "", r"\bbread\b|leipa\w*|\btoast\b|sandwich\w*|baguette|sampyla\w*", r"bread|leipa|toast|baguette|\bbuns?\b|\brolls?\b|sampyla|loaf|ciabatta|brioche|focaccia|pita"),
    _N("oats", "", r"\boat(?:s|meal)?\b|kaura\w*|porridge", r"\boat|kaura|porridge"),
    _N("barley", "", r"barley|\bohra\w*", r"barley|ohra"),
    _N("cheese", "AM", r"cheese\w*|juusto\w*|\bfeta\b|halloumi|mozzarella|parmesan|cheddar|gouda|emmental\w*|\bbrie\b|mascarpone|ricotta|gorgonzola",
       r"cheese|juusto|feta|halloumi|mozzarella|parmesan|cheddar|gouda|emmental|brie|mascarpone|ricotta|gorgonzola|grana|pecorino"),
    _N("cream", "AM", r"\bcream(?:y|ed)?\b|kerma\w*|\bcreme\b", r"cream|kerma|creme"),
    _N("butter", "AM", r"\bbutter\b|\bvoi\b|\bvoita\b", r"butter|\bvoi\b|voita"),
    _N("yoghurt or quark", "AM", r"yogh?urt|jogurtti\w*|quark|rahka\w*|\bskyr\b|viili\w*|tzatziki", r"yogh?urt|jogurtti|quark|rahka|skyr|viili"),
    _N("milk", "AM", r"\bmilk\b|maito\w*", r"milk|maito"),
    _N("chocolate", "", r"chocolate|suklaa\w*|\bcocoa\b|kaakao\w*|brownies?|\bmocha\b", r"chocolate|cocoa|suklaa|kaakao"),
    _N("vanilla", "", r"vanilla|vanilja\w*", r"vanilla|vanilja|custard"),
    _N("coffee", "", r"coffee|kahvi\w*|espresso|tiramisu", r"coffee|kahvi|espresso"),
    _N("tea", "", r"\btea\b|\btee\b|\bteeta\b", r"\btea\b|\btee\b|tea bags?"),
    _N("strawberries", "", r"strawberr\w*|mansikka\w*", r"strawberr|mansikka"),
    _N("blueberries", "", r"blueberr\w*|bilberr\w*|mustikka\w*", r"blueberr|bilberr|mustikka"),
    _N("raspberries", "", r"raspberr\w*|vadelma\w*", r"raspberr|vadelma"),
    _N("lingonberries", "", r"lingonberr\w*|puolukka\w*", r"lingonberr|puolukka"),
    _N("cloudberries", "", r"cloudberr\w*|\blakka\w*|\bhilla\b", r"cloudberr|lakka|hilla"),
    _N("currants", "", r"currants?|herukka\w*", r"currant|herukka"),
    _N("berries", "", r"\bberr(?:y|ies)\b|marja\w*", r"berr|marja|mansikka|mustikka|vadelma|puolukka|herukka"),
    _N("apple", "", r"\bapples?\b|omena\w*", r"apple|omena"),
    _N("lemon", "", r"lemon|sitruuna\w*", r"lemon|sitruuna"),
    _N("orange", "", r"\boranges?\b|appelsiini\w*", r"orange|appelsiini"),
    _N("plums", "", r"\bplums?\b|\bprunes?\b|luumu\w*", r"plum|prune|luumu"),
    _N("rhubarb", "", r"rhubarb|raparperi\w*", r"rhubarb|raparperi"),
    _N("pineapple", "", r"pineapple|\bananas\w*", r"pineapple|\bananas"),
    _N("mango", "", r"\bmango\w*"), _N("banana", "", r"banana\w*|banaani\w*", r"banana|banaani"),
    _N("coconut", "", r"coconut|kookos\w*", r"coconut|kookos"),
    _N("tofu or soya", "AY", r"\btofu\w*|tempeh|\bsoya?\b|soija\w*|edamame", r"tofu|tempeh|soy|soija|edamame"),
    _N("mustard", "BM", r"mustard|sinappi\w*", r"mustard|sinappi"),
    _N("celery", "BC", r"celery|celeriac|selleri\w*", r"celery|celeriac|selleri"),
    _N("honey", "", r"\bhoney\b|hunaja\w*", r"honey|hunaja"),
    _N("ginger", "", r"\bginger\b|inkivaari\w*", r"ginger|inkivaari"),
    _N("curry", "", r"\bcurry\b|\bcurr(?:ies|y)\b", r"curry|garam|turmeric|masala"),
    _N("wine", "", r"\bwine\b|viini\w*", r"wine|viini"),
]
# Words that take an ingredient out of a name ("nut-free brownie"), and compound names that are not what they sound like.
_NOT_IN = re.compile(r"\b[a-z]+[- ]free\b|\b(?:no|without|ilman)\s+[a-z]+|\b[a-z]+t[oa]n\b|\b[a-z]+tt[oa]m\w+")
_COMPOUNDS = [(r"peanut butter", "peanut"), (r"(coconut|almond|oat|soya?|rice|cashew)[- ](?:milk|cream|drink|yogh?urt|butter)", r"\1"), (r"cocoa butter", "cocoa"), (r"butter ?beans?", "beans"),
              (r"butternut", "squash"), (r"butterscotch|butterfly", " "), (r"cream cheese", "cheese"), (r"cream of tartar", " "), (r"ice[- ]cream", "cream"), (r"egg ?plant|munakoiso\w*", "aubergine"),
              (r"nutmeg|doughnuts?|donitsi\w*", " "), (r"coconut", "kookos"), (r"hot dogs?", "sausage"), (r"sweet potato(?:es)?", "bataatti"), (r"kananmun", "muna"), (r"valkosipuli", "garlic")]
_VEG_NAME = re.compile(r"\b(?:vegan|vegaani\w*|vegetarian|veggie|vege|plant[- ]based|meat[- ]free|meatless|mock)\b|kasvis(?:pihv|pyoryk|pull|nakk|makkar|burger|kebab|jauhis)|\b(?:soija|kaura|harkis|nyhto)\w*")
_ANIMAL = {"salmon", "tuna", "herring", "shellfish", "mussels", "fish", "turkey", "chicken", "minced meat", "beef", "pork", "lamb", "sausage", "reindeer or game", "liver"}
# Allergens that end a dish for a guest when the dish is named for them. Milk and gluten are left to the product checks: the pantry has stand-ins for those.
NAME_BLOCKS = {"AP", "AN", "AS", "AF", "AC", "UM", "AE", "AY", "BM", "BC"}


def named(text: str) -> list[dict]:
    """The ingredients a dish name (or an ingredient name) says it is made of."""
    f = _NOT_IN.sub(" ", fold(text or ""))
    for pat, to in _COMPOUNDS:
        f = re.sub(pat, to, f)
    veg, out = bool(_VEG_NAME.search(f)), []
    for c in NAMED:
        if c["name"].search(f):
            f = c["name"].sub(" ", f)
            if not (veg and c["label"] in _ANIMAL):
                out.append(c)
    return out


def missing_named(name: str, as_written: str, recipe: list[dict]) -> list[dict]:
    """What the dish is named for and its recipe does not have."""
    items = [fold(i["item"]) + " " + fold(i.get("fi") or "") for i in recipe]
    seen, out = set(), []
    for c in named(name) + named(as_written if fold(as_written or "") != fold(name or "") else ""):
        if c["label"] not in seen and not any(c["item"].search(i) for i in items):
            out.append(c)
        seen.add(c["label"])
    return out


def name_codes(*texts) -> set:
    """Allergens a dish or an ingredient is by its own name, of the kind no stand-in fixes."""
    return {a for t in texts for c in named(t or "") for a in c["codes"]} & NAME_BLOCKS


class ModelBusy(Exception):
    pass


# ------------------------------------------------------------------ reading
def _cards_text() -> str:
    return "\n".join(f"- {c['id']}: {c['name']} (also: {', '.join(c['aliases'][:6])})" for c in CARDS.values())


READ_PROMPT = """You read what a kitchen will cook so that the food for it can be ordered from a wholesaler: a menu, a lunch list, a week plan on a whiteboard, a typed note, an email. It may be informal, handwritten, in Finnish or in English. Return one JSON object and nothing else.
Today is {today}.

{{"menu": true or false, "what": "a few words: what this is",
 "title": "the heading, or a short name for the occasion", "date": "YYYY-MM-DD of the first day served, or null", "date_text": "the date as written, or null",
 "guests": the number of guests, people, pupils or portions as an integer (also written as hlö, henkilöä, annosta, pax), or null when it is not written, "guests_text": "as written, or null",
 "budget": null, or when a budget is written: {{"amount": the number in euros, "per": "total" or "guest", "text": "as written"}},
 "dishes": [{{"name": "the dish in plain English", "as_written": "exactly as written", "card": "a card id from the list, or null",
   "portions": an integer only when the menu gives this dish its own number (for example 'x14'), else null,
   "days": how many days this dish is served (1; a daily item on a five-day plan is 5),
   "notes": ["marks or notes written on this dish, verbatim, for example L, G, M, VE, vegetarian"], "food": true or false,
   "recipe": null, or when card is null and it is food: [{{"item": "ingredient", "fi": "the same ingredient in Finnish", "role": "what it does in the dish", "amount": number per portion, "unit": "g" or "ml"}}]}}],
 "notes": ["every dietary or allergy note on the page that is not attached to one dish, verbatim and with its number of guests, one note per entry"]}}

Rules:
- Copy what is there. Never invent a dish, a number or a note. A crossed-out line is not on the menu.
- menu is false only when there is nothing to cook from: a receipt, a list of products with prices, a checklist, a photo of something else. Then dishes is []. Any list of dishes for people is a menu, however short or informal.
- One dish per thing that is cooked. A dish written with its side ("meatballs and mashed potatoes", "jauhelihakastike + perunat") is one dish. A soup and a dessert on one line are two dishes.
- On a week plan each day's dish is served once (days 1). The head count on the page is per day.
- A key that only explains the marks ("L = laktoositon") is not a note. Copy the mark on each dish it is written on.
- card: use a card only when it is the same dish. Otherwise null and give a recipe.
- recipe: per portion, as a professional kitchen normally cooks this dish, 3 to 10 ingredients, amounts in g or ml. The ingredient a dish is named for is always in its recipe (peanuts in a satay, salmon in lohikeitto). Do not change or leave out an ingredient because of a dietary note on the page: code applies the notes afterwards. Roles are short: main, body, vegetable, sauce base, soup base, whipped topping, frying fat, spread, drink, seasoning, stock, sweetener, batter, garnish. Use an item name from the pantry list when one fits; otherwise a plain English ingredient name, the way a wholesaler lists it, in the form the kitchen buys: "lasagne sheets" not "pasta", "baguette" not "bread", "dark chocolate" not "chocolate", "feta cheese" not "cheese". Never use a pantry item for a different thing: a Karelian pasty is "karelian pasty", not rye bread. Something a kitchen buys ready (pasties, bread rolls, tart shells) is one ingredient under its own name. Name the cut of meat the dish is made from ("beef chuck", "pork shoulder"), one line per meat; minced meat only in a dish that is made of it. No water.
- food is false when the line is not something to eat or drink (a decoration, a person, a room, equipment).

The kitchen's recipe cards:
{cards}

The kitchen's pantry: {pantry}"""

GUARD_PROMPT = ('Look at the image. Could a kitchen cook from it? Answer true for a menu, a lunch list, a meal or week plan, or any list of dishes to serve to people: printed, typed, handwritten, '
                'on a whiteboard, in a note, an email or a screenshot, in Finnish or English, however short or informal, with or without a budget or allergy notes. '
                'Answer false only when it has no dishes to cook: a receipt, a list of products with prices, a checklist, or a photo of something else. '
                'Answer with JSON only: {"menu": true or false, "what": "a few words saying what the image shows"}')

RECIPE_PROMPT = """A professional kitchen needs a recipe per portion for each dish below so the ingredients can be ordered. Return JSON only:
{{"dishes": [{{"name": "as given", "food": true or false, "recipe": [{{"item": "ingredient", "fi": "the same ingredient in Finnish", "role": "what it does in the dish", "amount": number per portion, "unit": "g" or "ml"}}]}}]}}
3 to 10 ingredients each, amounts in g or ml, as the dish is normally cooked. The ingredient a dish is named for is always in its recipe. Where a dish lists must_include, the recipe has each of those as its own ingredient.
Do not adapt a recipe to any diet: code applies dietary notes afterwards. Roles are short: main, body, vegetable, sauce base, soup base, whipped topping, frying fat, spread, drink, seasoning, stock, sweetener, batter, garnish.
Use an item name from the pantry list when one fits; otherwise a plain English ingredient name, the way a wholesaler lists it, in the form the kitchen buys: "lasagne sheets" not "pasta", "baguette" not "bread", "dark chocolate" not "chocolate", "feta cheese" not "cheese". Never use a pantry item for a different thing: a Karelian pasty is "karelian pasty", not rye bread. Something a kitchen buys ready (pasties, bread rolls, tart shells) is one ingredient under its own name. Name the cut of meat the dish is made from ("beef chuck", "pork shoulder"), one line per meat; minced meat only in a dish that is made of it. No water. food is false when the line is not something to eat or drink; then recipe is [].
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


def _pantry_name(item: str) -> str:
    """An ingredient written in another number under the kitchen's own name for it: "potatoes" is potato, so it gets the kitchen's usual product."""
    w = item.split()
    alt = " ".join(w[:-1] + [_sing(w[-1])]) if w else item
    return item if item in PANTRY or alt not in PANTRY else alt


PIECES = {"pcs", "pc", "kpl", "piece", "pieces", "each", "st"}
PIECE_G = {"egg": 55, "eggs in shell": 55}  # a recipe counts eggs, the shelf weighs them: a medium egg is about 55 g


def _recipe(raw, per: int = 1, left: list | None = None) -> list[dict]:
    """Ingredients with amounts in g or ml per portion. per: the amounts as given are for that many portions, and code divides.
    left: collects, as written, each ingredient that has no weight or volume and so cannot be ordered from the recipe."""
    out = []
    for i in (raw if isinstance(raw, list) else [])[:14]:
        if not isinstance(i, dict):
            continue
        item, unit = str(i.get("item") or "").strip().lower()[:48], str(i.get("unit") or "").strip().lower()
        fi, name = str(i.get("fi") or "").strip().lower()[:48], _pantry_name(item)
        try:
            amount = float(i.get("amount"))
        except (TypeError, ValueError):
            amount = math.nan
        if unit in PIECES and name in PIECE_G:
            amount, unit = amount * PIECE_G[name], "g"
        mult = {"g": 1, "ml": 1, "kg": 1000, "l": 1000, "dl": 100, "cl": 10}.get(unit)
        water = re.fullmatch(r"(?:(?:cold|hot|warm|boiling|tap|ice) )?water|vesi|ice(?: cubes)?", item)  # water comes from the tap
        if not item or mult is None or not math.isfinite(amount) or water:
            if left is not None and item and not water:
                left.append(fi or item)
            continue
        # divided amounts are cut, not rounded up: 1 kg for 60 portions must come back as 1 kg, never as a gram over a pack
        out.append({"item": name, "role": str(i.get("role") or "ingredient").strip().lower()[:32], "amount": round(amount * mult, 2) if per == 1 else math.floor(amount * mult / per * 10000) / 10000,
                    "unit": "ml" if unit in ("ml", "l", "dl", "cl") else "g", **({"fi": fi} if fi else {})})
    return out


# A model's guess at a whole dried spice is easily a pack too many: a bay leaf weighs a fifth of a gram and a pot takes a few. Its amount per
# portion is cut to what the kitchen's own recipe cards use. A person's own recipe is never touched.
SPICE_MAX = ((re.compile(r"bay lea(?:f|ves)|laakerinleh|saffron|sahrami|nutmeg|muskot|star anise|tahtianis"), 0.05),
             (re.compile(r"allspice|peppercorn|maustepippur|(?<!garlic )cloves?\b|neilik|juniper|kataja|cardamom|kardemumm|cinnamon stick|kanelitan"), 0.2))


def _spices(recipe: list[dict]) -> list[dict]:
    for r in recipe:
        top = next((m for rx, m in SPICE_MAX if rx.search(fold(r["item"]))), None) if r["unit"] == "g" else None
        if top and r["amount"] > top:
            r["amount"] = top
    return recipe


# Words a menu prints as section headings. A reading sometimes returns them as notes; a heading is never a dietary note.
HEADINGS = {"starter", "starters", "main", "mains", "main course", "main courses", "dessert", "desserts", "appetizer", "appetizers", "first course", "second course", "side", "sides", "side dishes",
            "drinks", "beverages", "coffee", "breakfast", "lunch", "dinner", "supper", "buffet", "menu", "notes", "note", "dietary notes", "allergens", "allergy notes",
            "monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday", "alkuruoka", "alkuruoat", "pääruoka", "pääruoat", "jälkiruoka", "jälkiruoat", "juomat", "aamiainen", "lounas", "illallinen"}


def _heading(note: str) -> bool:
    return re.sub(r"[^a-zäöå ]", "", str(note).lower()).strip() in HEADINGS


def _exact_card(name: str) -> str | None:
    f = re.sub(r"\s+", " ", re.sub(r"[^a-z0-9& ]", " ", fold(name or ""))).strip()
    return next((c["id"] for c in CARDS.values() for a in [c["name"], *c["aliases"]] if f and re.sub(r"\s+", " ", re.sub(r"[^a-z0-9& ]", " ", fold(a))).strip() == f), None)


def _budget(raw) -> dict | None:
    """A budget as written on the menu: {amount in euros, per total or guest, text}."""
    b = raw if isinstance(raw, dict) else {"amount": raw} if raw not in (None, "", False) else None
    if not b:
        return None
    try:
        v = b.get("amount")
        amount = float(re.sub(r"[^0-9.,]", "", v).replace(",", ".") if isinstance(v, str) else v)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(amount) or amount <= 0 or amount > 10_000_000:
        return None
    return {"amount": round(amount, 2), "per": "guest" if str(b.get("per") or "").lower() in ("guest", "person", "portion", "head") else "total", "text": str(b.get("text") or "").strip()[:60] or None}


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
        if name == name.lower():
            name = name[0].upper() + name[1:]
        written = str(x.get("as_written") or name)[:120]
        card = x.get("card") if x.get("card") in CARDS else (match_card(name) or match_card(written))
        # A card stands for the dish only when it has what the dish is named for: "nutty green salad" is not the green salad card.
        if card and card not in (_exact_card(name), _exact_card(written)) and missing_named(name, written, CARDS[card]["ingredients"]):
            card = None
        # The kitchen's own recipe (recipe_portions: how many portions its amounts make): it stands in place of the card, with the amounts as written and nothing added for waste.
        per = _int(x.get("recipe_portions")) or (1 if x.get("own") is True else None)
        left = []
        own = [{**r, "waste_pct": 0} for r in _recipe(x.get("recipe"), per, left)] if source == "agent" and per and 1 <= per <= MAX_GUESTS else []
        if own:
            card = None
        recipe = own or ([] if card else _spices(_recipe(x.get("recipe"))))
        notes = x.get("notes") if isinstance(x.get("notes"), list) else [x["notes"]] if x.get("notes") else []
        skip = [str(n).strip()[:140] for n in (x.get("skip") if isinstance(x.get("skip"), list) else []) if str(n).strip()][:6]
        dishes.append({"name": name, "as_written": written, "card": card, "portions": _int(x.get("portions")), "days": _int(x.get("days")) or 1,
                       "notes": [str(n).strip()[:80] for n in notes if str(n).strip() and not _heading(n)][:6], "food": x.get("food") is not False, "recipe": recipe,
                       "source": "card" if card else (x.get("source") if x.get("source") in ("agent", "model") else source) if recipe else None,
                       # only: this dish is for the guests of one note and nobody else. skip: the guests of these notes get another dish instead of this one.
                       **({"own": True} if own else {}), **({"left_out": left[:8]} if own and left else {}), **({"only": str(x["only"]).strip()[:140]} if x.get("only") else {}), **({"skip": skip} if skip else {})})
    notes = raw.get("notes") if isinstance(raw.get("notes"), list) else [raw["notes"]] if raw.get("notes") else []
    day = str(raw.get("date") or raw.get("event_date") or "")[:10]
    try:
        date.fromisoformat(day)
    except ValueError:
        day = None
    g = raw.get("guests")
    return {"title": str(raw.get("title") or "").strip()[:90], "date": day, "date_text": str(raw.get("date_text") or "").strip()[:60] or None,
            "guests": _int(g) if _int(g) is not None else None, "guests_text": str(raw.get("guests_text") or (g if g is not None and _int(g) is None else "") or "").strip()[:60] or None,
            "budget": _budget(raw.get("budget")), "order_staples": raw.get("order_staples") is True,
            "dishes": dishes, "notes": [str(n).strip()[:140] for n in notes if str(n).strip() and not _heading(n)][:12]}


_reads_lock = threading.Lock()


def _reads() -> dict:
    try:
        return json.loads((DATA_DIR / "photo_reads.json").read_text())
    except (OSError, ValueError):
        return {}


def read_photo(image: str, reuse: bool = False) -> tuple[dict | None, dict, list[dict]]:
    """(reading or None, verdict, model calls). The model looks at the photo twice at the same time, with two different questions: one short look that only
    says whether there is anything to cook from, and the read itself. The photo is refused only when neither look finds a dish: one doubt never turns a real menu away."""
    # reuse: the same photo on the same day is read once, so a second look at the same lunch list cannot give another cart. The verdict says when a reading
    # is reused, and the caller says it to the person. The timed menu page never reuses one: its seconds are of a real read.
    key = hashlib.sha256(image.encode()).hexdigest() + ":" + datetime.now(TZ).strftime("%Y-%m-%d")
    with _reads_lock:
        hit = _reads().get(key) if reuse else None
    if hit:
        return hit["reading"], {**hit["verdict"], "reused": True}, []
    content = lambda text: [{"type": "text", "text": text}, {"type": "image_url", "image_url": {"url": image}}]
    prompt = READ_PROMPT.format(today=datetime.now(TZ).strftime("%A %d %B %Y"), cards=_cards_text(), pantry=", ".join(PANTRY))

    def ask(model, text, effort, timeout, step):
        try:
            out, u = _gemini(model, content(text), effort, timeout)
            return out, {**u, "step": step}
        except ModelBusy:
            return None, None

    with ThreadPoolExecutor(2) as ex:
        fg, fr = ex.submit(ask, GUARD_MODEL, GUARD_PROMPT, "low", 25, "is it a menu"), ex.submit(ask, READ_MODEL, prompt, "low", 75, "read the menu")
        (guard, ug), (read, ur) = fg.result(), fr.result()
    calls = [u for u in (ug, ur) if u]
    if read is None:
        raise GatewayError(503, "model_busy", "The model that reads the photo is busy. Try again in a minute.")
    rd = normalize(read, "model")
    food = [d for d in rd["dishes"] if d["food"]]
    guard_no = guard is not None and guard.get("menu") is False
    if (read.get("menu") is False and guard_no) or (not food and (read.get("menu") is False or guard_no or guard is None)):
        what = str((read if read.get("menu") is False else guard or read).get("what") or "").strip()[:80]
        return None, {"menu": False, "what": what}, calls
    calls += complete(rd)
    verdict = {"menu": True, "what": str(read.get("what") or "").strip()[:80], "doubt": guard_no}
    if all(d["card"] or d["recipe"] or not d["food"] for d in rd["dishes"]):  # a reading the model could not finish is not kept
        with _reads_lock:
            kept = {k: v for k, v in _reads().items() if k.endswith(key[-10:])}  # today's only
            kept.setdefault(key, {"reading": rd, "verdict": verdict})  # the first reading of the day stands
            try:
                (DATA_DIR / "photo_reads.json").write_text(json.dumps(dict(list(kept.items())[-200:]), ensure_ascii=False))
            except OSError:
                pass
    return json.loads(json.dumps(rd)), verdict, calls


def fill_recipes(reading: dict, redo: dict | None = None) -> list[dict]:
    """A dish with no card and no recipe gets a proposed recipe from the model (one call for all of them).
    redo: {dish index: [ingredients its name promises]} asks again for those dishes, with the ingredient required."""
    need = [(i, d) for i, d in enumerate(reading["dishes"]) if not d["card"] and d["food"] and (i in redo if redo is not None else not d["recipe"])]
    if not need:
        return []
    ask = [{"name": d["name"], **({"as_written": d["as_written"]} if fold(d["as_written"]) != fold(d["name"]) else {}), **({"must_include": redo[i]} if redo else {})} for i, d in need]
    try:
        out, u = _gemini(READ_MODEL, RECIPE_PROMPT.format(pantry=", ".join(PANTRY), dishes=json.dumps(ask, ensure_ascii=False)), "low", 45)
    except ModelBusy:
        return []
    got = [x for x in (out.get("dishes") if isinstance(out.get("dishes"), list) else []) if isinstance(x, dict)]
    for k, (i, d) in enumerate(need):
        x = next((x for x in got if fold(str(x.get("name") or "")) == fold(d["name"])), got[k] if k < len(got) and len(got) == len(need) else None)
        if not x:
            continue
        if redo is not None:  # a second answer replaces the first only when it has what was missing
            new = _spices(_recipe(x.get("recipe")))
            if new and len(missing_named(d["name"], d["as_written"], new)) < len(missing_named(d["name"], d["as_written"], d["recipe"])):
                d["recipe"], d["source"] = new, "model"
            continue
        d["food"] = x.get("food") is not False
        d["recipe"], d["source"] = (_spices(_recipe(x.get("recipe"))), "model") if d["food"] else ([], None)
    return [{**u, "step": "check named ingredients" if redo is not None else "propose recipes"}]


def complete(reading: dict, only_new: bool = False) -> list[dict]:
    """Every dish gets a recipe, and every proposed recipe is checked in code against the dish's own name. A recipe that lost the
    ingredient the dish is named for goes back to the model once; what is still missing after that is flagged on the dish (flags()).
    A recipe an agent or a person gave is never rewritten, only flagged."""
    new = {i for i, d in enumerate(reading["dishes"]) if not d["card"] and not d["recipe"] and d["food"]}
    calls = fill_recipes(reading)
    redo = {i: [c["label"] for c in miss] for i, d in enumerate(reading["dishes"]) if not d["card"] and d["food"] and d["recipe"] and d["source"] == "model" and (i in new or not only_new)
            and (miss := missing_named(d["name"], d["as_written"], d["recipe"]))}
    if redo:
        calls += fill_recipes(reading, redo)
    return calls


def flags(rd: dict) -> list[dict]:
    """Dishes whose recipe does not have what the dish is named for, and ingredients of a kitchen's own recipe that could not be ordered as written. Never silent: shown on the dish and in the plan."""
    out = []
    for i, d in enumerate(rd["dishes"]):
        if d.get("left_out"):
            out.append({"kind": "left_out", "dish_i": i, "dish": d["name"], "ingredients": d["left_out"], "codes": [],
                        "text": f"The recipe for “{d['name']}” gives no weight or volume for {', '.join(d['left_out'])}, so that is not on this order. Add it with an amount, or order it on its own."})
        if d["card"] or not d["food"] or not d["recipe"]:
            continue
        miss = missing_named(d["name"], d["as_written"], d["recipe"])
        if miss:
            what = " and ".join(c["label"] for c in miss)
            out.append({"kind": "named_missing", "dish_i": i, "dish": d["name"], "ingredients": [c["label"] for c in miss], "codes": sorted({a for c in miss for a in c["codes"]}),
                        "text": f"“{d['name']}” is named for {what}, but the recipe proposed for it has none, so none is ordered for it. Check the recipe, or rename the dish to what you will cook."})
    return out


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
        if d.get("only") and d["only"] not in rd["notes"]:  # a dish made for the guests of a note: without the note it would be ordered with no rule at all
            ask(f"dish:{i}", f"“{d['name']}” was added for the guests of the note “{d['only']}”, and that note is no longer on the menu. Remove the dish or put the note back.")
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


def _kinds(text: str, bare: bool = False) -> list[str]:
    f = fold(text).strip()
    out = [k for k, pat in DIETS if re.search(pat, f)]
    if bare:
        out += [k for k, pat in BARE if k not in out and re.search(pat, f)]
    if "nut" in out and "peanut" in out:
        out.remove("peanut")  # a nut note rules out peanuts already
    return out


HOW = {"lactose": "any product with milk in it must carry the manufacturer's lactose-free claim.",
       "gluten": "no product with a gluten cereal (certified gluten-free oats are allowed).",
       "milk": "no product with milk; the pantry's milk-free alternative is used where there is one.",
       "vegan": "no milk, egg, fish or meat.", "vegetarian": "no meat or fish.", "egg": "no product with egg.", "fish": "no product with fish."}


def parse_notes(rd: dict) -> tuple[list[dict], list[dict]]:
    """Dietary notes -> rules. (rules, questions). A rule: {kind, note, count, items, dish, scope, severe, codes, text}.
    scope: "guests" (count of them, their share of every dish), "all" (every portion it covers), "dish" (written on one dish), "order" (the whole kitchen: every line).
    items: ingredient words it is limited to. dish: index when written on one dish."""
    rules, q, g = [], [], rd.get("guests") or 0
    for i, d in enumerate(rd["dishes"]):
        for n in d["notes"]:
            for part in re.split(r"[,;/]| and | & ", n):
                for k in _kinds(part):
                    rules.append({"kind": k, "note": n, "count": None, "items": [], "dish": i, "scope": "dish", "severe": bool(SEVERE.search(fold(n)))})
    for ni, n in enumerate(rd["notes"]):
        found, whole = False, fold(n)
        speaks = bool(re.search(r"allerg|intoleran|anaphyl|anafyl", whole))
        for part in re.split(r"[,;\n]| and (?=\d)| ja (?=\d)", n):
            f = fold(part)
            kinds = _kinds(part, speaks)
            if not kinds:
                continue
            found = True
            rest = f
            for k, pat in DIETS + BARE:
                rest = re.sub(pat, " ", rest)
            bare = re.sub(r"\b(table|room|poyta|bord|class|grade|luokka|no|nr)\.?\s*\w?\d+\w?", " ", rest)
            m = re.search(r"\b(\d{1,4})\b", bare)
            count = int(m.group(1)) if m else None
            items = sorted({w for w in SCOPE_WORDS if re.search(rf"\b{w}s?\b", rest)} | {p for p in PANTRY if re.search(rf"\b{re.escape(p)}\b", rest)})
            for k in kinds:
                if count is not None and g and count > g:
                    q.append({"about": f"note:{ni}", "text": f"The note “{n}” reads as {count:,} guests, more than the {g:,} on the menu. Fix the note or remove it."})
                    continue
                if k in HARD and (count is None or KITCHEN.search(f)):  # about the whole kitchen, or nobody counted: every line
                    rules.append({"kind": k, "note": n, "count": None, "items": [], "dish": None, "scope": "order", "severe": bool(SEVERE.search(whole))})
                else:
                    rules.append({"kind": k, "note": n, "count": count, "items": [] if k in HARD else items, "dish": None, "scope": "guests" if count else "all", "severe": bool(SEVERE.search(whole))})
        if not found:
            q.append({"about": f"note:{ni}", "text": f"The note “{n}” could not be turned into a rule the code can check. Reword it (for example “6 gluten-free”) or remove it."})
    seen, out = set(), []
    for r in rules:
        key = (r["kind"], r["count"], tuple(r["items"]), r["dish"], r["scope"])
        if key not in seen:
            seen.add(key)
            out.append(r)
    for r in out:
        r["codes"] = sorted(EXCODES.get(r["kind"], ()))
        who = f"“{rd['dishes'][r['dish']]['name']}”" if r["dish"] is not None else ("the " + ", ".join(r["items"])) if r["items"] else "every dish"
        word = DIET_WORD[r["kind"]].capitalize()
        if r["kind"] in HARD:
            what = _names(EXCODES[r["kind"]])
            if r["scope"] == "order":
                r["text"] = (f"No product on this order may contain or may contain traces of {what}. Checked on every line"
                             + (", because the note is about the whole kitchen." if KITCHEN.search(fold(r["note"])) else ", because the note gives no head count."))
            else:
                r["text"] = ((f"{word} for {r['count']} of {g:,}: in every dish, that share of the portions gets" if r["scope"] == "guests" else f"{word} in {who}:")
                             + f" no product that contains or may contain traces of {what}. The other portions are ordered as usual. A dish that is made with {what} is held for those guests and you are asked."
                             + (f" Severe: the lines for the other guests that contain {what} are listed, so the kitchen can keep them apart." if r["severe"] else ""))
        elif r["count"]:
            r["text"] = f"{word} for {r['count']} of {g:,}: for that share of the portions in {who}, {HOW[r['kind']]}"
        else:
            r["text"] = f"{word} in {who}" + ("" if r["dish"] is not None or r["items"] else ", because the note gives no head count") + f": {HOW[r['kind']]}"
    return out, q


# ------------------------------------------------------------- the planner
def _cons(rules: list[dict]) -> dict:
    ex, lf, veg, swap, why = set(), False, "", [], []
    for r in rules:
        k = r["kind"]
        ex |= EXCODES.get(k, set())
        if k == "lactose":
            lf = True
        elif k == "gluten":
            swap.append("gluten_free")
        elif k in ("milk", "vegan"):
            swap.append("milk_free")
        if k in ("vegan", "vegetarian"):
            veg = "vegan" if k == "vegan" or veg == "vegan" else "vegetarian"
        why.append(DIET_WORD[k])
    return {"ex": ex, "lf": lf, "veg": veg, "swap": swap, "why": sorted(set(why))}


def _applies(rule: dict, dish_i: int, item: str) -> bool:
    if rule["scope"] == "order":
        return False  # order-wide, handled once
    if rule["dish"] is not None:
        return rule["dish"] == dish_i
    return not rule["items"] or any(w in item for w in rule["items"])


def portions_of(d: dict, rd: dict, rules: list[dict]) -> int:
    """Portions of a dish to cook: its own number or the head count, times days, less the guests who get another dish instead."""
    total = (d["portions"] or rd["guests"]) * d["days"]
    gone = sum(min(total, math.ceil(total * r["count"] / rd["guests"])) for r in rules if r["scope"] == "guests" and r["note"] in d.get("skip", ()))
    return max(0, total - gone)


def demands(rd: dict, rules: list[dict]) -> list[dict]:
    """Every (dish, ingredient) with its amount, split where a dietary note covers only some of the portions."""
    out, g = [], rd["guests"]
    for i, d in enumerate(rd["dishes"]):
        full = (d["portions"] or g) * d["days"]
        portions = portions_of(d, rd, rules)
        recipe = CARDS[d["card"]]["ingredients"] if d["card"] else d["recipe"]
        for ing in recipe:
            mine = [r for r in rules if _applies(r, i, ing["item"]) and r["note"] not in d.get("skip", ())]
            if d.get("only"):  # a dish made for the guests of one note: that note covers all of it, and the other head counts are other people
                everyone, some = [r for r in mine if not r["count"] or r["note"] == d["only"]], []
            else:
                everyone, some = [r for r in mine if not r["count"]], [r for r in mine if r["count"]]
            parts, left = [], portions
            for r in some:
                n = min(left, math.ceil(full * r["count"] / g))
                if n > 0:
                    parts.append((n, everyone + [r]))
                    left -= n
            if left > 0:
                parts.append((left, everyone))
            waste = ing.get("waste_pct", WASTE)
            for n, rs in parts:
                c = _cons(rs)
                out.append({"dish_i": i, "dish": d["name"], "item": ing["item"], "fi": ing.get("fi"), "role": ing["role"], "amount": ing["amount"], "unit": ing["unit"], "waste": waste, "portions": n,
                            "need": n * ing["amount"] * (1 + waste / 100), "cons": c, "source": d["source"] or "card",
                            "notes": [r["note"] for r in rs if r["scope"] == "guests" or r["note"] == d.get("only")], "severe": any(r["severe"] for r in rs if r["kind"] in HARD)})
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
        "beef chuck": ("beef", "naudan"), "christmas ham": ("ham", "kinkku"), "porridge rice": ("porridge rice", "puuroriisi"), "pickled cucumber": ("pickled cucumber", "maustekurk"),
        "breadcrumbs": ("breadcrumb", "korppujauho"), "gluten-free breadcrumbs": ("breadcrumb", "korppujauho"), "dark syrup": ("syrup", "siirappi"), "beef bouillon": ("bouillon", "lihaliemi"),
        "swede": ("swede", "lanttu"), "cinnamon": ("cinnamon", "kaneli"),
    }
    terms = aliases.get(item, (item,))
    return any(all(w in text for w in fold(term).split()) for term in terms)


# What a kitchen keeps on the shelf. Needed in a small part of the smallest pack, these are left to the pantry and listed, not ordered.
STAPLE = re.compile(r"\b(?:salt|sugar|flour|starch|yeast|oil|vinegar|bouillon|stock|broth|baking powder|baking soda|bicarbonate|vanillin|vanilla (?:sugar|extract|essence)|honey|syrup|mustard|ketchup|"
                    r"soy sauce|worcestershire|tabasco|breadcrumbs|cocoa powder|gelatine?|peppercorns?|allspice|cinnamon|cardamom|nutmeg|cloves?|cumin|turmeric|paprika|curry powder|bay lea(?:f|ves)|oregano|"
                    r"marjoram|chill?i (?:powder|flakes)|garlic powder|onion powder|ground \w+|dried \w+|spices?|seasoning|sauce base)\b|(?<!bell )(?<!sweet )(?<!red )(?<!green )(?<!yellow )\bpepper\b|^thyme$")
STAPLE_SHARE = 1 / 3  # of the smallest pack
RANGE_NOTE = "Every product and price here is Valio Aimo's answer for your account. Nothing comes from outside Valio Aimo."
FAR = 1.5             # a pack is far bigger than the need when it holds this many times the need
MANY = 24             # more sales units than this of one ingredient is a great many small packs
_NEUTRAL = set("fresh organic luomu class kpl pkt ltk pss bag box sack fin finland suomi hollanti espanja italia ranska saksa puola kiina netherlands holland spain italy france germany poland china tuore pakaste frozen approx".split())
# Words that make a product another thing than the ingredient in its name: lime curd is not lime, red wine vinegar is not red wine.
_FORMS = set("soup casserole pudding sauce syrup topping bar sorbet curd juice spread pie cake cheesecake pizza salad sandwich panini marmalade jam puree pure vinegar flavour flavor flavoured flavored spice seasoning "
             "strip drink smoothie dessert candy cookie biscuit brownie muffin pastry croissant bun burger patty nugget snack chip crisp tea liqueur aroma extract oil powder paste ketchup dressing marinade mix "
             "bread baguette wrap tortilla lasagne pasta risotto stew porridge mousse butter milk lemonade soda jelly compote filling glaze coated breaded marinated pickled dried ice yoghurt yogurt pepper stick "
             "roll pesto hummus dip salsa chutney relish schnitzel mochi leaf leave concentrate base kissel improver water kombucha beer cider wine granola muesli cereal chocolate cheese cheddar cream sausage meatball strudel".split())
# What a Finnish kitchen calls it and what the wholesaler sells it as.
SOLD_AS = {"karelian pasty": ("rice pastry", "riisipiirakka"), "karelian pie": ("rice pastry", "riisipiirakka"), "karjalanpiirakka": ("rice pastry", "riisipiirakka"),
           # In a Finnish kitchen paprika is the vegetable, and the wholesaler lists it as capsicum. The spice is "paprika powder".
           **{k: ("capsicum", "paprika") for k in ("paprika", "bell pepper", "sweet pepper", "capsicum")},
           # The wholesaler's big frozen packs of the Finnish berry are listed as bilberry; a recipe says blueberry for both.
           **{k: ("bilberry", "mustikka") for k in ("blueberry", "frozen blueberry", "mustikat", "pakastemustikka")},
           **{f"{c} bell pepper": (f"capsicum {c}", f"paprika {f}") for c, f in (("red", "punainen"), ("green", "vihrea"), ("yellow", "keltainen"))}}
SNACK_CATS = ("21990", "21993", "21120")  # crisps, snacks and salted biscuits
_SPECIES = set("lamb pork beef veal chicken broiler turkey duck goose reindeer elk moose venison horse salmon trout tuna cod herring pike perch shrimp prawn crab ham bacon".split())
# The same in Finnish, where the form is the end of a compound word ("mustikkajogurtti"): the part of the word, and the English words that ask for it.
_FI_FORMS = {"jogurt": ("yoghurt", "yogurt"), "rahka": ("quark",), "viili": ("viili",), "keitto": ("soup",), "kastike": ("sauce",), "mehu": ("juice",), "hillo": ("jam",), "juoma": ("drink",), "kiisseli": ("kissel",),
             "jaatelo": ("ice",), "vanuk": ("pudding",), "piirak": ("pie", "pastry", "pasty"), "leipa": ("bread",), "patukka": ("bar",), "keksi": ("biscuit", "cookie"), "munkki": ("doughnut", "donut"), "suklaa": ("chocolate",),
             "etikka": ("vinegar",), "sose": ("puree",), "juusto": ("cheese",)}
_DIET_WORDS = set("gluten free lactose vegan gluteeniton laktoositon vegaaninen maidoton".split())
_found: dict = {}


def _sing(w: str) -> str:
    if len(w) > 4 and w.endswith("ies"):
        return w[:-3] + "y"
    if len(w) > 4 and w.endswith("oes"):
        return w[:-2]
    return w[:-1] if len(w) > 3 and w.endswith("s") and not w.endswith("ss") else w


def _toks(text: str, brand: str = "") -> set:
    """The words of a product name that say what it is: no brand, pack size, origin or handling words."""
    drop = _NEUTRAL | set(re.findall(r"[a-z]+", fold(brand or "")))
    return {_sing(t) for t in re.findall(r"[a-z]+", fold(text or "")) if len(t) > 2 and t not in drop}


YIELD = re.compile(r"(\d+(?:[.,]\d+)?)\s*kg\s*/\s*(\d+(?:[.,]\d+)?)\s*l\b")
DIET_QUERY = {"gluten-free": ("gluten free", "gluteeniton"), "lactose-free": ("lactose free", "laktoositon"), "vegan": ("vegan", "vegaaninen"), "milk-free": ("vegan", "maidoton")}


def find(g, item: str, fi: str | None = None, diet: tuple = ()) -> list[str]:
    """Catalogue products for an ingredient the pantry does not list, best first: the product whose name is mostly the ingredient
    ("chicken breast fillet" before "chicken spice mix"), searched by its English and its Finnish name; meat and fish only from the meat and fish shelves."""
    key = ("find", item, fi or "", diet)
    if key in _found:
        return _found[key]
    words, fi = fold(item).split(), fold(fi or "")
    sold = SOLD_AS.get(" ".join(_sing(w) for w in words)) or SOLD_AS.get(fi) or SOLD_AS.get(fi.rstrip("t")) or ()
    if sold and sold[0].startswith("capsicum") and re.search(r"jauhe|mauste|savu|rouhe", fi):
        sold = ()  # the Finnish name says it is the spice
    want, want_fi = _toks(item) | _toks(sold[0] if sold else ""), _toks(fi) | _toks(sold[1] if sold else "")
    fi_one = " ".join(w[:-1] if len(w) > 5 and w.endswith("t") else w for w in fi.split())  # Finnish plural: "lasagnelevyt" is sold as "lasagnelevy"
    queries = list(dict.fromkeys([item, " ".join(_sing(w) for w in words), fi or item, fi_one or item, *sold]))

    def best(queries, fallback, n):
        seen, rows = set(), []
        for qi, q in enumerate(queries + fallback):
            if qi >= len(queries) and rows:
                break  # the shorter name is tried only when the full one finds nothing
            docs, _, _ = g.cat.search(q, {}, limit=60)
            for rank, d in enumerate(docs):
                cat = str(d["category"] or "")
                if d["id"] in seen or cat[:2] not in FOOD_CATS:
                    continue
                seen.add(d["id"])
                if (_toks(d["title"]) & _FORMS) - want:
                    continue  # the ingredient made into something else
                name_fi = fold((d.get("names") or {}).get("fi") or "") + " " + fold(d["title"])
                if any(part in name_fi and part not in fi and not want & set(en) for part, en in _FI_FORMS.items()):
                    continue
                t, tf = _toks(d["title"], d.get("brand")) - _DIET_WORDS, _toks((d.get("names") or {}).get("fi") or "", d.get("brand")) - _DIET_WORDS
                share = max(len(want & t) / max(1, len(t)), len((want_fi | want) & tf) / max(1, len(tf)))
                if not share:
                    continue  # found by a part of a word only ("punaviinietikka" for "punaviini"): not the ingredient
                shelf = 0 if cat[:3] == "174" else 2 if cat[:3] == "173" else 1  # the produce shelf before the rest, ready meals last
                rows.append((-round(share, 3), shelf, qi, rank, d, bool((_toks(d["title"]) & _SPECIES) - want)))
        if any(not r[5] for r in rows):
            rows = [r for r in rows if not r[5]]  # "lamb roast beef" is not beef
        plain = [r for r in rows if str(r[4]["category"] or "")[:5] not in SNACK_CATS]
        if plain and not want & _FORMS:
            rows = plain  # crisps with the taste of the ingredient are not the ingredient
        if sold and sold[0].startswith("capsicum") and any(r[1] == 0 for r in rows):
            rows = [r for r in rows if r[1] == 0]  # the vegetable, never the spice of the same name
        if MEAT_WORDS.search(fold(item)) and any(str(r[4]["category"] or "")[:3] in MEAT_CATS for r in rows):
            rows = [r for r in rows if str(r[4]["category"] or "")[:3] in MEAT_CATS]
        rows.sort(key=lambda r: r[:4])
        return [r[4]["id"] for r in rows[:n]]

    out = best(queries, [" ".join(words[1:])] if len(words) > 2 else [], 12)
    for w in diet:  # the same ingredient made for a diet ("gluten free soy sauce"), after the plain ones
        en, fin = DIET_QUERY[w]
        out += [pid for pid in best(list(dict.fromkeys([f"{en} {item}"] + ([f"{fin} {fi}", f"{fin} {fi_one}"] if fi else []))), [], 6) if pid not in out]
    _found[key] = out
    return out


def wide(g, item: str) -> list[str]:
    """Other pack sizes of a pantry ingredient: products on the same shelf whose name says the ingredient in the words the kitchen's own products
    use and nothing else ("ONION 2KG PEELED" next to the 5 kg peeled; never red onion, sliced onion or whole pepper for ground).
    Looked at only when the kitchen's own packs are far bigger than the need."""
    key = ("wide", item)
    if key in _found:
        return _found[key]
    p, out = PANTRY.get(item), []
    if p and not p.get("family"):
        own = [g.cat.get(pid) for pid in p["prefer"] if g.cat.get(pid)]
        cats, want = {str(d["category"] or "")[:3] for d in own}, _toks(item)
        allowed = want.union(*[_toks(d["title"], d.get("brand")) for d in own]) if own else want
        for q in dict.fromkeys([item, " ".join(_sing(w) for w in fold(item).split())]):
            docs, _, _ = g.cat.search(q, {}, limit=60)
            for d in docs:
                t = _toks(d["title"], d.get("brand"))
                if d["id"] not in p["prefer"] and d["id"] not in out and str(d["category"] or "")[:3] in cats and want <= t <= allowed:
                    out.append(d["id"])
    _found[key] = out[:12]
    return _found[key]


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
        if isinstance(sib, dict):
            sib = sib.get(str(d["category"] or "")[:3], frozenset())
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
        if named_hits(ex, d):
            return "it has no allergen declaration and its name points at " + _names(named_hits(ex, d))
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

    def tradeoff(self, pid, usual) -> str:
        """What the kitchen gives and gets with this replacement, in one sentence: price per litre or kilo, fat, brand. From Valio Aimo's price data and the manufacturer's figures."""
        d, ud, bits = self.g.cat.get(pid), self.g.cat.get(usual), []
        if not d or not ud or usual not in self.live or not self.live[usual].get("price"):
            return ""
        (u1, p1), (u2, p2) = self.per(pid), self.per(usual)
        if u1 and u1 == u2 and p1 and p2 and abs(p1 - p2) >= 1:
            bits.append(f"€{abs(p1 - p2) / 100:,.2f} {'less' if p1 < p2 else 'more'} per {u1.lower()}")
        elif u1 and u1 == u2 and p1 and p2:
            bits.append(f"the same price per {u1.lower()}")
        f1, f2 = FAT.get(pid), FAT.get(usual)
        if f1 is not None and f2 is not None and abs(f1 - f2) >= 1:
            bits.append(f"{f1:g}% fat where the usual one has {f2:g}%")
        if d.get("brand") and ud.get("brand") and d["brand"] != ud["brand"]:
            bits.append(f"another brand ({d['brand']})")
        return ("The trade-off: " + ", ".join(bits) + ".") if bits else ""

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

    def pack(self, pid, unit) -> float:
        """The least of this product that can be ordered, in g or ml: one sales unit, or one order step of it."""
        return self.content(pid, unit) * self.step(pid)

    def covers(self, pid, unit, need) -> float:
        """What is ordered of this product to cover `need` g or ml."""
        return math.ceil(need / self.pack(pid, unit) - 1e-9) * self.pack(pid, unit)

    # ---------------------------------------------------------------- plan
    def plan(self, rd: dict, rules: list[dict], dms: list[dict], day: str, day_note: str) -> dict:
        g = self.g
        order_rules = [r for r in rules if r["scope"] == "order"]
        order_ex = set().union(*[EXCODES[r["kind"]] for r in order_rules]) if order_rules else set()
        order_word = " and ".join(sorted({DIET_WORD[r["kind"]] for r in order_rules}))
        seen = g._cleared(self.p)  # products with no allergen declaration that a person cleared on this account before
        key_of = lambda dm: (dm["dish_i"], tuple(dm["cons"]["why"]))
        # 1. candidates per ingredient: the pantry's products, its diet alternative, then (for a name not in the pantry) the catalogue search
        for dm in dms:
            p = PANTRY.get(dm["item"])
            if p:
                names = [dm["item"]] + [p[s] for s in dm["cons"]["swap"] if p.get(s)]
                dm["pool"], dm["known"], dm["swapped"] = list(dict.fromkeys(pid for n in names for pid in PANTRY[n]["prefer"])), True, len(names) > 1
                dm["wide"] = wide(g, dm["item"])
            else:
                # Each group of portions gets its own search: the 5 coeliac portions look for gluten-free spaghetti, the other 235 for spaghetti.
                # (One shared pool made the diet product the first choice for everyone.)
                diets = tuple(sorted({w for w in dm["cons"]["why"] if w in DIET_QUERY}))
                dm["pool"], dm["known"], dm["swapped"], dm["wide"] = find(g, dm["item"], dm.get("fi"), diets), False, False, []
            # What the other products for this ingredient contain; for a search, only those on the product's own shelf (a blueberry doughnut says nothing about blueberries).
            shelf = str((g.cat.get(dm["pool"][0]) or {}).get("category") or "")[:3] if dm["pool"] and not dm["known"] else None
            if shelf is None:
                dm["sib"] = frozenset(a for pid in dm["pool"] if g.cat.get(pid) for a in g.cat.get(pid)["contains"])
            else:  # per shelf: paprika crisps with milk in them say nothing about a fresh capsicum
                by = {}
                for pid in dm["pool"]:
                    if g.cat.get(pid):
                        by.setdefault(str(g.cat.get(pid)["category"] or "")[:3], set()).update(g.cat.get(pid)["contains"])
                dm["sib"] = {k: frozenset(v) for k, v in by.items()}
        ids = list(dict.fromkeys([pid for dm in dms for pid in dm["pool"] + dm["wide"]] + [pid for dm in dms if dm["role"] in ROLES for pid, fam in FAMILY.items() if fam == ROLES[dm["role"]]["family"]]))
        self.live = g._live(ids, day, self.p) if ids else {}
        late = [self.live[dm["pool"][0]].get("earliest_date") for dm in dms if dm["pool"] and self.live[dm["pool"][0]].get("reason") == "later_date_only"]
        if late and len(late) >= len(dms) / 2:  # the wholesaler cannot deliver that early: take its first day instead, once
            first = max(d for d in late if d)
            day_note = f"The first day Valio Aimo can deliver is {nice_day(first)}, so delivery is set to that day."
            day, self.live = first, g._live(ids, first, self.p)
        self.day = day

        # A recipe gives stock or sauce as the ready liquid; the wholesaler sells the concentrate with its yield on the pack ("1,5kg/100L").
        for dm in dms:
            if dm["unit"] == "ml" and dm["pool"] and not any(self.content(pid, "ml") for pid in dm["pool"]):
                y = next((m for m in (YIELD.search(fold(g.cat.get(pid)["title"])) for pid in dm["pool"] if g.cat.get(pid) and self.content(pid, "g")) if m), None)
                if y:
                    per = float(y.group(1).replace(",", ".")) / float(y.group(2).replace(",", "."))  # kg per litre is g per ml
                    dm["ready"] = dm.get("ready", 0) + dm["need"]
                    dm["need"], dm["amount"], dm["unit"] = dm["need"] * per, round(dm["amount"] * per, 2), "g"
                    dm["yield"] = f"{y.group(1)} kg makes {y.group(2)} l"

        # 2. the usual product per ingredient: first in the kitchen's order that passes every check
        opened, skipped, free = [], {}, _cons([])
        for dm in dms:
            dm["cands"] = []
            tried = skipped.setdefault((dm["item"], tuple(dm["cons"]["why"])), [])
            own = name_codes(dm["item"]) & (order_ex | dm["cons"]["ex"])
            if own and not dm["swapped"]:  # the ingredient is the allergen: no product of it can pass, whatever its data says
                dm["own"] = own
                if all(t["id"] for t in tried):
                    tried.insert(0, {"id": None, "title": dm["item"], "why": "the ingredient is " + _names(own)})
                continue
            for pid in dm["pool"]:
                why = self.why_not(pid, dm["unit"], dm["cons"], order_ex, dm["sib"])
                if why is None:
                    dm["cands"].append(pid)
                    continue
                diet = self.why_not(pid, dm["unit"], free, set()) is None  # it could be ordered if no dietary rule applied
                dm["diet_refused"] = dm.get("diet_refused") or diet
                if not dm["cands"] and all(t["id"] != pid for t in tried):
                    tried.append({"id": pid, "title": g.cat.get(pid)["title"] if g.cat.get(pid) else pid, "why": why, "diet": diet})
            if not dm["known"] and dm["cands"]:  # found by search, so the order is ours: what is in stock goes first, and only its own shelf may stand in for it
                dm["cands"].sort(key=lambda pid: self.cap(pid) <= 0)
                shelf = str(g.cat.get(dm["cands"][0])["category"] or "")[:3]
                dm["cands"] = [pid for pid in dm["cands"] if str(g.cat.get(pid)["category"] or "")[:3] == shelf]

        # With an exclusion in force, a product whose allergens can be checked from the manufacturer's data goes before one
        # that says nothing (the kitchen's order is kept otherwise), so fewer lines need a person to clear them.
        for dm in dms:
            ex = set(order_ex) | set(dm["cons"]["ex"])
            if ex and len(dm["cands"]) > 1:
                first = dm["cands"][0]
                dm["cands"].sort(key=lambda pid: bool({x for x in allergens_unchecked(ex, g.cat.get(pid)) if not (seen.get(pid) or {}).get(x)})
                                 or not (pid == first or not dm["known"] or _ingredient_matches(dm["item"], g.cat.get(pid))))  # never another, dearer thing just to save a tick

        # 3. A dietary refusal makes a dish a single question. Do this before allocating any of its other ingredients, so a cake
        # cannot be partly ordered while its essential nut ingredient waits for an answer. A dish that is the allergen by its
        # own name (satay and peanuts) is held for the guests the rule covers even when the recipe or the product data is silent.
        blocked, named_block = {}, {}
        dish_codes = {i: name_codes(d["name"], d["as_written"]) for i, d in enumerate(rd["dishes"])}
        for dm in sorted(dms, key=lambda d: 0 if d.get("own") else 1):
            hit = dish_codes[dm["dish_i"]] & (order_ex | dm["cons"]["ex"])
            if hit and key_of(dm) not in blocked:
                blocked[key_of(dm)], named_block[key_of(dm)] = dm, hit
        for dm in dms:
            # Held only when a dietary rule is what refuses it: the ingredient is the allergen, or a product could be ordered if the rule did not apply.
            # An ingredient nobody can get (out of range, out of stock, past the cut-off) is listed once as not ordered and holds no portions.
            if not dm["cands"] and (dm.get("own") or dm.get("diet_refused")):
                blocked.setdefault(key_of(dm), dm)
        # What is held is the portions the rule covers. Eight guests with a peanut allergy do not stop the satay for the other 52.
        total_of = {i: portions_of(d, rd, rules) for i, d in enumerate(rd["dishes"])}
        held, whole = {}, set()
        for (dish_i, _), dm in blocked.items():
            held[dish_i] = held.get(dish_i, 0) + dm["portions"]
        for (dish_i, why_key), dm in blocked.items():
            tried = skipped.get((dm["item"], tuple(dm["cons"]["why"])), [])
            reason = next((t["why"] for t in tried if t.get("diet") or not t["id"]), tried[0]["why"] if tried else "the dietary rule does not allow a product for this ingredient")
            diet = ", ".join(dm["cons"]["why"]) or order_word or "nut-free"
            meat = dm["cons"]["veg"] and (MEAT_WORDS.search(fold(dm["item"])) or (dm["pool"] and g.cat.get(dm["pool"][0]) and str(g.cat.get(dm["pool"][0])["category"] or "")[:3] in MEAT_CATS))
            if meat:
                reason = "it is meat or fish"
            for pid in dm["pool"] if not dm.get("own") and not meat else []:
                d = g.cat.get(pid)
                if not d:
                    continue
                hits = allergen_hits(set(order_ex) | set(dm["cons"]["ex"]), d["contains"], d["claims"]) | allergen_hits(set(order_ex) | set(dm["cons"]["ex"]), d["may_contain"], d["claims"])
                if hits:
                    reason = "it contains or may contain " + _names(hits)
                    break
            made_of = _names(named_block[(dish_i, why_key)]) if (dish_i, why_key) in named_block else None
            # name the rule that refuses it, not every rule on those portions: a hazelnut cake fails "nut-free", not "lactose-free"
            codes = named_block.get((dish_i, why_key)) or dm.get("own") or set()
            said = set(dm["cons"]["why"]) | {DIET_WORD[r["kind"]] for r in order_rules}
            by_rule = sorted({DIET_WORD[k] for k in EXCODES if EXCODES[k] & codes and DIET_WORD[k] in said})
            if by_rule:
                diet = " and ".join(by_rule)
            rest, n = total_of[dish_i] - held[dish_i], dm["portions"]
            those, pors = (f"those {n}" if n != 1 else "that one"), ("portions" if n != 1 else "portion")
            if rest > 0:  # only the portions under this rule wait; the rest of the dish is ordered as usual
                severe = (f" The allergy is severe and the other {rest} portions bring {made_of} into the kitchen, so changing the dish for everyone is the safer answer." if dm.get("severe") and made_of else "")
                opened.append({"kind": "no_fit", "dish": dm["dish"], "dish_i": dish_i, "item": dm["item"], "reason": "the dish is made with " + made_of if made_of else reason, "portions_refused": n, "diet": list(why_key),
                               "notes": dm.get("notes") or [], "named": bool(made_of), "ask": {"portions": n, "rest": rest, "choices": ["for_them", "for_everyone"], "severe": bool(dm.get("severe"))},
                               "text": (f"{dm['dish']} is made with {made_of}, so it cannot be made safe for the {n} {diet} {pors}." if made_of else
                                        f"{dm['dish']}: the {n} {diet} {pors} cannot be made because {dm['item']} was refused: {reason}.")
                                       + f" Nothing is ordered for {those}. The other {rest} {'portions are' if rest != 1 else 'portion is'} ordered. Change the dish for {those}, or for everyone?" + severe})
            elif dish_i not in whole:
                whole.add(dish_i)
                opened.append({"kind": "no_fit", "dish": dm["dish"], "dish_i": dish_i, "item": dm["item"], "reason": "the dish is made with " + made_of if made_of else reason, "dish_refused": True, "diet": list(why_key),
                               "named": bool(made_of), "ask": {"portions": total_of[dish_i], "rest": 0, "choices": ["for_everyone", "remove"]},
                               "text": (f"{dm['dish']} is made with {made_of}, so it cannot be made {diet}." if made_of else f"{dm['dish']}: it cannot be made for {diet} because {dm['item']} was refused: {reason}.")
                                       + " The whole dish is waiting for your answer; none of its ingredients is ordered. Change the dish, or take it off the menu."})

        # 3b. Kitchen staples needed in a small part of the smallest pack come from the pantry. They are listed with the amount, never ordered
        # unasked. Only where the kitchen's own usual product would pass the same checks: a rule that needs another product gets it ordered.
        active = [dm for dm in dms if key_of(dm) not in blocked]
        pantry = []
        by_item = {}
        for dm in active:
            by_item.setdefault(dm["item"], []).append(dm)
        for item, group in by_item.items() if not rd.get("order_staples") else []:
            if not STAPLE.search(item) or re.search(r"\bcubes?\b", item):  # sugar cubes go on the table, not in the pot
                continue
            unit, total = group[0]["unit"], sum(dm["need"] for dm in group)
            packs = [self.pack(pid, unit) for dm in group for pid in dm["cands"]]
            smallest = min(packs) if packs else None
            if total > (smallest * STAPLE_SHARE if smallest is not None else 150):
                continue
            mine = [dm for dm in group if not dm["swapped"] and not dm.get("own")
                    and ((dm["pool"] and dm["pool"][0] in dm["cands"]) or not (dm["cons"]["ex"] or dm["cons"]["lf"] or dm["cons"]["veg"] or order_ex))]
            if not mine:
                continue
            for dm in mine:
                dm["pantry"] = True
            need = sum(dm["need"] for dm in mine)
            ruled = sorted({w for dm in mine if dm["cons"]["ex"] for w in dm["cons"]["why"]})  # your own jar was not checked here: say so where an allergen is ruled out
            check = (f"This order is {order_word}: read the label on your own stock." if order_ex else f"Some of it is for the {', '.join(ruled)} portions: read the label on your own stock." if ruled else None)
            pantry.append({"item": item, "need": round(need, 1), "unit": unit, "amount": qty(need, unit), "dishes": list(dict.fromkeys(dm["dish"] for dm in mine)),
                           "smallest": round(smallest, 1) if smallest else None, "smallest_pack": qty(smallest, unit) if smallest else None, "check": check,
                           "text": f"{item.capitalize()}: {qty(need, unit)} for {', '.join(dict.fromkeys(dm['dish'] for dm in mine))}."
                                   + (f" The smallest pack is {qty(smallest, unit)}." if smallest else " Not in Valio Aimo's range for this day.")
                                   + (f" The recipe gives {qty(sum(dm['ready'] for dm in mine if dm.get('ready')), 'ml')} ready to use; that is this much concentrate ({mine[0]['yield']})." if mine[0].get("yield") else "") + (" " + check if check else "")})
        active = [dm for dm in active if not dm.get("pantry")]

        # 3c. The pack. The kitchen's first product stays unless it is far bigger than what this menu needs of the ingredient in all its dishes;
        # then the product that leaves the least over is taken, from the kitchen's list and from the same ingredient in other pack sizes.
        def common(group):  # products every group of portions may get, in the first group's order, then other pack sizes of the same ingredient
            unit = group[0]["unit"]
            out = [pid for pid in group[0]["cands"] if all(pid in dm["cands"] for dm in group)]
            return out + [w for w in group[0]["wide"] if w not in out and all(self.why_not(w, unit, dm["cons"], order_ex, dm["sib"]) is None for dm in group)]

        def least_of(group, need):
            opts, unit = common(group), group[0]["unit"]
            stocked = [pid for pid in opts if self.cap(pid) * self.content(pid, unit) >= need] or opts  # a pack that cannot be supplied is no smaller pack
            best = min(stocked, key=lambda pid: (round(self.covers(pid, unit, need), 3), opts.index(pid)))
            return best, self.covers(best, unit, need)

        def by_product():
            out = {}
            for dm in active:
                if dm["cands"]:
                    out.setdefault((dm["item"], dm["cands"][0]), []).append(dm)
            return out

        for (item, first), group in by_product().items():
            unit, need = group[0]["unit"], sum(dm["need"] for dm in group)
            best, least = least_of(group, need)
            if self.covers(first, unit, need) > FAR * need and least < self.covers(first, unit, need) - 1e-6:
                for dm in group:
                    dm["cands"], dm["sized"] = [best] + [c for c in dm["cands"] if c != best], first
        # The same ingredient for portions with and without a dietary rule: when the product the rule needs is also the ingredient for everyone
        # and one pack of it leaves less over than two different packs, everyone gets that product. Only when Valio Aimo has enough of it for
        # everyone and it costs no more: 5 coeliacs of 240 do not put the whole school on the gluten-free spaghetti.
        chosen = {}
        for (item, pid), group in by_product().items():
            chosen.setdefault(item, {})[pid] = group
        for item, mine in chosen.items():
            while len(mine) > 1:
                amount = lambda pid, dms_: self.covers(pid, dms_[0]["unit"], sum(d["need"] for d in dms_))
                cost = lambda pid, dms_: (self.live[pid].get("price") or 0) * amount(pid, dms_) / self.content(pid, dms_[0]["unit"])
                enough = lambda pid, dms_: self.cap(pid) * self.content(pid, dms_[0]["unit"]) >= sum(d["need"] for d in dms_) - 1e-6
                moves = [(amount(b, mine[a] + mine[b]) - amount(a, mine[a]) - amount(b, mine[b]), a, b) for a in mine for b in mine
                         if a != b and all(b in d["cands"] and (not d["known"] or _ingredient_matches(item, g.cat.get(b))) for d in mine[a])
                         and enough(b, mine[a] + mine[b]) and cost(b, mine[a] + mine[b]) <= cost(a, mine[a]) + cost(b, mine[b]) + 1e-6]
                if not moves or min(moves)[0] >= -1e-6:
                    break
                _, a, b = min(moves)
                for d in mine[a]:
                    d["cands"], d["shared"] = [b] + [c for c in d["cands"] if c != b], True
                mine[b] += mine.pop(a)
        # Now that every group of portions has its product: the usual pack comes back where, for all of them together, it is no bigger after all.
        for (item, pid), group in by_product().items():
            unit, need = group[0]["unit"], sum(dm["need"] for dm in group)
            usual = next((dm["sized"] for dm in group if dm.get("sized")), None)
            if usual and usual != pid and all(usual in dm["cands"] for dm in group) and self.covers(usual, unit, need) <= self.covers(pid, unit, need) + 1e-6 and self.cap(usual) * self.content(usual, unit) >= need:
                for dm in group:
                    dm["cands"] = [usual] + [c for c in dm["cands"] if c != usual]
            for dm in group:
                if dm.get("sized") in (None, dm["cands"][0]) or self.covers(dm["sized"], unit, need) <= self.covers(dm["cands"][0], unit, need) + 1e-6:
                    dm.pop("sized", None)
        # An ingredient that is not on the kitchen's own list has no usual product to keep. When the first one found would come in a great many
        # small packs (121 punnets of blueberries for a kiisseli), the bigger pack of the same ingredient is ordered instead: only where Valio Aimo
        # has enough of it, it is not far more than the menu needs, and it costs at most a half more.
        for (item, first), group in by_product().items():
            unit, need = group[0]["unit"], sum(dm["need"] for dm in group)
            packs = lambda pid: self.covers(pid, unit, need) / self.content(pid, unit)
            cost = lambda pid: (self.live[pid].get("price") or 0) * packs(pid)
            if group[0]["known"] or packs(first) <= MANY:
                continue
            same = lambda pid: kind(g.cat.get(pid)) == kind(g.cat.get(first))
            every = list(dict.fromkeys(common(group) + [pid for dm in group for pid in dm["cands"]]))  # found for any group of portions, and passing every group's checks
            opts = [pid for pid in every if pid != first and self.content(pid, unit) and same(pid) and packs(pid) * 4 <= packs(first) and self.cap(pid) >= packs(pid) - 1e-6
                    and self.covers(pid, unit, need) <= FAR * need and cost(pid) <= 1.5 * cost(first)
                    and all(pid in dm["cands"] or self.why_not(pid, unit, dm["cons"], order_ex, dm["sib"]) is None for dm in group)]
            if opts:
                best = min(opts, key=lambda pid: (cost(pid), packs(pid)))
                for dm in group:
                    dm["cands"], dm["bulk"] = [best] + [c for c in dm["cands"] if c != best], (first, round(packs(first)))
                    dm.pop("sized", None)
        # 3d. A menu planned again (more guests, a dish taken off): the product the cook already saw on the earlier cart stays, where it passes
        # the same checks for every portion and Valio Aimo has enough of it. A better-fitting pack is no reason to change a product unasked.
        keep = set(rd.get("keep") or [])
        for (item, pid), group in by_product().items() if keep else ():
            unit, need = group[0]["unit"], sum(dm["need"] for dm in group)
            had = pid if pid in keep else next((k for k in common(group) if k in keep and self.cap(k) * self.content(k, unit) >= need - 1e-6), None)
            if had and had != pid:
                for dm in group:
                    dm["cands"] = [had] + [c for c in dm["cands"] if c != had]
                    dm.pop("sized", None)
        for (item, pid), group in by_product().items():
            least = least_of(group, sum(dm["need"] for dm in group))[1]
            for dm in group:
                dm["least"] = least

        # 4. hand out stock: roles that have the fewest ways out are served first; a shortage is solved by role
        left = {pid: self.cap(pid) for pid in ids if self.live[pid].get("orderable") or self.live[pid].get("reason") == "out_of_stock"}
        alloc, shorted = [], {}  # alloc: (demand, product, sales units, usual product or None, reason); shorted: product -> the sentence that says it is short
        for dm in sorted(active, key=lambda d: 0 if ROLES.get(d["role"], {}).get("first") else 1):
            who = f"{dm['item']} for {dm['dish']}" + (f" ({', '.join(dm['cons']['why'])}, {dm['portions']} portions)" if dm["cons"]["why"] else "")
            amount = qty(dm["need"], dm["unit"])
            if not dm["cands"]:
                tried = skipped.get((dm["item"], tuple(dm["cons"]["why"])), [])
                was = next((o for o in opened if o.get("_gap") == (dm["dish_i"], dm["item"])), None)
                if was:
                    was["_need"] += dm["need"]
                    was["amount"] = qty(was["_need"], dm["unit"])
                    was["_why"] |= set(dm["cons"]["why"])
                elif not dm["pool"]:
                    opened.append({"kind": "not_in_range", "dish": dm["dish"], "item": dm["item"], "amount": amount, "_gap": (dm["dish_i"], dm["item"]), "_need": dm["need"], "_unit": dm["unit"], "_why": set(dm["cons"]["why"]),
                                   "text": f"{dm['item'].capitalize()} for {dm['dish']}: not in Valio Aimo's range. Not ordered."})
                else:
                    opened.append({"kind": "no_fit", "dish": dm["dish"], "item": dm["item"], "amount": amount, "_gap": (dm["dish_i"], dm["item"]), "_need": dm["need"], "_unit": dm["unit"], "_why": set(dm["cons"]["why"]),
                                   "text": f"{dm['item'].capitalize()} for {dm['dish']}: no product passes the checks, so it is not ordered. " + " ".join(f"{t['title']}: {t['why']}." for t in tried[:3])})
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
                    why = (f"A {dm['role']} needs {rule['needs']}. This product ({fit}) does that, so it covers {qty(take * c, dm['unit'])} for {dm['dish']}."
                           + (" Cooking cream would not whip, so it is not used here." if rule.get("whips") else " It is the cheapest per litre of the creams that fit." if rule["family"] == "cream" else ""))
                else:
                    why = f"This is the kitchen's next choice for {dm['item']}, so it covers {qty(take * c, dm['unit'])} for {dm['dish']}."
                alloc.append((dm, alt, take, usual, (have, why)))
                left[alt] -= take
                missing -= take * c
            if missing > 1e-6:
                none = (f"A {dm['role']} needs {rule['needs']}, and no other product on this kitchen's list does that" + (", so cooking cream is not used" if rule.get("whips") else "") + ".") if rule else f"There is no other product for {dm['item']} on this kitchen's list." if dm["known"] else f"No other {dm['item']} in Valio Aimo's range can be ordered for this day."
                was = next((o for o in opened if o.get("_short") == (dm["dish_i"], dm["item"], usual)), None)
                if was:  # the same gap for another group of portions of the same dish: one entry
                    was["_need"] += missing
                    was["amount"] = qty(was["_need"], dm["unit"])
                    was["text"] = f"{dm['item'].capitalize()} for {dm['dish']}: {was['amount']} is not covered. {have} {none} The line is kept short and a person decides."
                else:
                    opened.append({"kind": "short", "dish": dm["dish"], "item": dm["item"], "role": dm["role"], "amount": qty(missing, dm["unit"]), "_short": (dm["dish_i"], dm["item"], usual), "_need": missing,
                                   "text": f"{who.capitalize()}: {qty(missing, dm['unit'])} is not covered. {have} {none} The line is kept short and a person decides."})

        # an ingredient nobody could get keeps its amount as a number and the dietary rules its portions are under: the shop asks the range for it once more as a line of its own
        opened = [{**{k: v for k, v in o.items() if not k.startswith("_")}, **({"need": round(o["_need"], 1), "unit": o["_unit"], "rules": sorted(o["_why"])} if o.get("_gap") and o.get("_unit") else {})} for o in opened]
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
            c = self.content(pid, unit)
            usual = next((u for _, _, u, _ in parts if u), None)
            # A product can be the first usable candidate while still being a
            # different ingredient from the recipe (for example Emmentaler for
            # parmesan after the parmesan products fail a lactose rule). Mark it
            # explicitly so the same fact survives into the signed order.
            conceptual = next((p[0]["item"] for p in parts if p[0]["item"] in PANTRY), None)
            expected = PANTRY.get(conceptual, {}).get("prefer", [None])[0] if conceptual else None
            semantic_sub = bool(conceptual and expected and pid != expected and not _ingredient_matches(conceptual, d))
            sub_for = usual or (expected if semantic_sub else None)
            # Said only when the named product really was refused by a dietary rule; when it is merely short, the shortage sentence above is the reason.
            refused = semantic_sub and all(expected not in p[0]["cands"] for p in parts)
            sub_reason = (f"The recipe names {conceptual}; {d['title']} is used as a substitute because the named product did not pass the dietary checks." if refused else None)
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
            over = list({t["id"]: t for p in parts if p[2] is None for t in skipped.get((p[0]["item"], tuple(p[0]["cons"]["why"])), []) if t["id"]}.values())[:3]
            req = {**({"exclude_allergens": ex} if ex else {}), **({"claims": ["FREE_FROM:LACTOSE"]} if lf else {})}
            # What is needed, what the pack makes of it, and what is left over when the pack is much bigger.
            need_amt, got = raw * c, units * c
            least = sum({p[0]["item"]: p[0].get("least", got) for p in parts}.values()) if len({p[0]["item"] for p in parts}) == 1 else got
            much = not short and got >= FAR * need_amt and got - need_amt >= 1
            sized = next((p[0]["sized"] for p in parts if p[0].get("sized") and p[0]["sized"] != pid), None)
            shared = any(p[0].get("shared") for p in parts) and len({tuple(p[0]["cons"]["why"]) for p in parts}) > 1
            bulk = next((p[0]["bulk"] for p in parts if p[0].get("bulk") and p[0]["bulk"][0] != pid), None)
            pack_text = (f"Needs {qty(need_amt, unit)} {'with the waste allowance' if any(p[0].get('waste') for p in parts) else 'as the recipe gives it'}. One {label} holds {qty(c, unit)}" + (f", sold in steps of {s:g}" if s != 1 else "") + f": {units:,.10g} {many(label, units)} = {qty(got, unit)}."
                         + (f" {qty(got - need_amt, unit)} is left over" + ("; nothing smaller is sold for this ingredient." if got <= least + 1e-6 else ".") if much else "")
                         + (f" The kitchen's usual pack ({g.cat.get(sized)['title']}, {qty(self.pack(sized, unit), unit)}) is far more than this menu needs, so this smaller one is ordered." if sized else "")
                         + (f" {g.cat.get(bulk[0])['title']} would have come in {bulk[1]} packs, so this bigger pack of the same ingredient is ordered." if bulk else "")
                         + (" One product is ordered for the portions with a dietary rule and for the rest, because one pack leaves less over than two." if shared else "")
                         + (f" The recipe gives {qty(sum(p[0]['ready'] for p in parts if p[0].get('ready')), 'ml')} ready to use; the need is counted as concentrate ({next(p[0]['yield'] for p in parts if p[0].get('yield'))})." if any(p[0].get("yield") for p in parts) else ""))
            lp = f.get("list_price")
            own_price = (f"{'Your contract price' if f.get('contract_price') else 'Your price'}: €{f['price'] / 100:,.2f} per {label}" + (f", list price €{lp / 100:,.2f}" if lp and lp > f["price"] else ""))
            lines.append({
                "product": {"id": pid, "title": d["title"], "brand": d.get("brand")}, "units": units, "unit": label, "quantity": round(units * 10 ** d["scale"]),
                "for": [{"dish": p[0]["dish"], "item": p[0]["item"], "role": p[0]["role"], "portions": p[0]["portions"], "per_portion": f"{p[0]['amount']:g} {p[0]['unit']}", "waste_pct": p[0]["waste"],
                         "diet": p[0]["cons"]["why"], "share": round(p[1] / raw, 3)} for p in parts],
                "pack": {"unit": unit, "need": round(need_amt, 1), "ordered": round(got, 1), "need_text": qty(need_amt, unit), "ordered_text": qty(got, unit),
                         "leftover": round(got - need_amt, 1) if much else 0, "leftover_text": qty(got - need_amt, unit) if much else None, "nothing_smaller": bool(much and got <= least + 1e-6),
                         **({"instead_of": {"id": sized, "title": g.cat.get(sized)["title"]}} if sized else {})},
                "checks": {
                    "pack": {"ok": True, "text": pack_text},
                    "allergens": {"ok": True, "text": a_text, **({"ask": silent} if silent else {})},
                    "stock": {"ok": True, "text": (f"Only {num(self.cap(pid))} {many(label, self.cap(pid))} can be supplied, less than this menu needs." + (f" The {first[0]} gets them first." if first else "") + " The rest is covered on another line or listed under not ordered." if short else "Enough in stock for this order." if f.get("stock_known") else "Delivered to order; no stock limit for this day.")},
                    "price": {"ok": True, "unit_price": f["price"], "list_price": f.get("list_price"), "contract": bool(f.get("contract_price")),
                              "text": own_price + (f" (€{f['comparison_price'] / 100:,.2f} per {f['comparison_unit'].lower()})" if f.get("comparison_price") and f.get("comparison_unit") else "") + f". This line: €{amount / 100:,.2f} ex VAT."},
                },
                "amount": amount, "partial": bool(short),
                **({"substitutes_for": {"id": sub_for, "title": g.cat.get(sub_for)["title"]}, "reason": " ".join(dict.fromkeys([w[0] for w in why] + [w[1] for w in why] + ([sub_reason] if sub_reason else []))),
                    "from_range": f"From Valio Aimo's range for your account. {own_price}.", **({"tradeoff": self.tradeoff(pid, sub_for)} if self.tradeoff(pid, sub_for) else {})} if sub_for else {}),
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
        subtotal = sum(l["amount"] for l in lines)

        # 6. what a person should know about the whole order: a severe allergy next to lines that carry the allergen, a recipe that lost its named ingredient, the budget
        warnings = []
        for r in rules:
            if not (r["kind"] in HARD and r["severe"] and r["scope"] in ("guests", "dish", "all")):
                continue
            codes, what = EXCODES[r["kind"]], _names(EXCODES[r["kind"]])
            doc = lambda l: g.cat.get(l["product"]["id"])
            hot = [l for l in lines if allergen_hits(codes, doc(l)["contains"], doc(l)["claims"]) | allergen_hits(codes, doc(l)["may_contain"], doc(l)["claims"]) or name_codes(doc(l)["title"]) & codes]
            asked = lambda l: bool(codes & set((l.get("requirements") or {}).get("exclude_allergens") or []))  # the approver already clears these one by one
            blank = [l for l in lines if l not in hot and not declares_allergens(doc(l)) and not asked(l)]
            who = f"the {r['count']} guests" if r["count"] else "those guests"
            warnings.append({"kind": "cross_contact", "note": r["note"], "lines": [l["product"]["id"] for l in hot], "undeclared": len(blank),
                             "text": f"“{r['note']}”: " + (f"{len(hot)} {'line' if len(hot) == 1 else 'lines'} ordered for the other guests {'contains' if len(hot) == 1 else 'contain'} or may contain {what} ({', '.join(l['product']['title'] for l in hot[:4])}). "
                                                           f"Keep {'it' if len(hot) == 1 else 'them'} apart from the food for {who}, or change the dish for everyone." if hot else f"no line on this order contains or may contain {what} by the manufacturers' declarations.")
                                     + (f" {len(blank)} more {'line' if len(blank) == 1 else 'lines'} for the other guests {'has' if len(blank) == 1 else 'have'} no allergen declaration at all ({', '.join(l['product']['title'] for l in blank[:4])}) and cannot be checked from data: read the labels." if blank else "")})
        fl = flags(rd)
        budget, b = None, rd.get("budget")
        if b:
            limit = round(b["amount"] * 100) * (rd["guests"] if b["per"] == "guest" else 1)
            gap, left_out = abs(limit - subtotal), bool(opened or pantry)
            budget = {"limit": limit, "per": b["per"], "amount": b["amount"], "subtotal": subtotal, "fits": subtotal <= limit, "difference": gap,
                      "text": f"The menu gives a budget of €{b['amount']:,.2f}" + (f" per guest, €{limit / 100:,.2f} for {rd['guests']:,} guests" if b["per"] == "guest" else "")
                              + f". This order is €{subtotal / 100:,.2f} ex VAT, " + (f"€{gap / 100:,.2f} under the budget." if subtotal <= limit else f"€{gap / 100:,.2f} over the budget.")
                              + (" What is held, not ordered or taken from the pantry is not in that sum." if left_out else "")}
        n_model, n_own = sum(1 for d in rd["dishes"] if d["source"] in ("model", "agent") and not d.get("own")), sum(1 for d in rd["dishes"] if d.get("own"))
        subs = [l for l in lines if l.get("substitutes_for")]
        intent = (f"{rd['title'] or 'Menu'}: {rd['guests']:,} guests" + (f", {rd['date_text'] or rd['date']}" if rd.get("date") or rd.get("date_text") else "") + ". Dishes: " + ", ".join(d["name"] for d in rd["dishes"])
                  + (". Notes: " + "; ".join(rd["notes"]) if rd["notes"] else "") + ".")[:900]
        rationale = (f"Bill of materials worked out in code from {len(rd['dishes']) - n_model - n_own} of the kitchen's recipe cards" + (f", {n_own} recipe(s) the kitchen gave with its own amounts" if n_own else "")
                     + (f" and {n_model} proposed recipe(s) that a person should check" if n_model else "")
                     + ": portions x amount per portion + waste allowance, rounded up to pack size. Every line passed pack, allergen, stock and price checks."
                     + (f" {len(subs)} line(s) replace a short product by what the ingredient does in the dish." if subs else "") + (f" {len(opened)} item(s) are left for a person and not ordered." if opened else "")
                     + (f" {len(pantry)} kitchen staple(s) needed in small amounts are left to the kitchen's own pantry." if pantry else ""))
        flag_of = {x["dish_i"]: x["text"] for x in fl}
        return {
            "status": "ready" if lines else "nothing_to_order", "delivery_date": day, "delivery_note": day_note,
            "range_note": RANGE_NOTE,
            "requirements": {"exclude_allergens": sorted(order_ex)},
            "diet": [{"note": r["note"], "rule": r["text"], "kind": r["kind"], "scope": r["scope"], "count": r["count"], "dish": r.get("dish"), "severe": r["severe"]} for r in rules],
            "dishes": [{"name": d["name"], "portions": total_of[i], "held": held.get(i, 0), "recipe": "the kitchen's card" if d["card"] else "the kitchen's own recipe, amounts as written" if d.get("own") else "the model's recipe, check it" if d["source"] == "model" else "the agent's recipe, check it",
                        "card": CARDS[d["card"]]["name"] if d["card"] else None, "ingredients": len(CARDS[d["card"]]["ingredients"] if d["card"] else d["recipe"]),
                        **({"flag": flag_of[i]} if i in flag_of else {}), **({"only": d["only"]} if d.get("only") else {})} for i, d in enumerate(rd["dishes"])],
            "lines": lines, "not_ordered": opened, "pantry": pantry, "flags": fl, "warnings": warnings, "budget": budget,
            "totals": {"lines": len(lines), "subtotal": subtotal, "currency": "EUR", "allergen_ask": sum(1 for l in lines if l["checks"]["allergens"].get("ask")), "pantry": len(pantry),
                       "held_portions": sum(o.get("portions_refused", 0) for o in opened)},
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
        return {"status": "needs_answers", "reading": rd, "questions": q + q2, "diet": [{"note": r["note"], "rule": r["text"]} for r in rules], "flags": flags(rd)}
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
        rd, verdict, calls = read_photo(_image(req["image"]), reuse=True)
        if rd is None:
            return g._prove({"ucp": g._env(), "status": "not_a_menu", "message": NOT_MENU.format(what=verdict["what"] or "something else"), "usage": cost(calls)})
        reused = bool(verdict.get("reused"))
    else:
        rd = normalize({"title": req.get("title"), "date": req.get("event_date") or req.get("date"), "guests": req.get("guests"), "dishes": req.get("dishes") or [],
                        "budget": req.get("budget"), "order_staples": req.get("order_staples"),
                        "notes": req.get("notes") if isinstance(req.get("notes"), list) else [req["notes"]] if req.get("notes") else []}, "agent")
        if any(not d["card"] and not d["recipe"] for d in rd["dishes"]):
            _spend("agent:" + principal["customer"], 20)
            calls = complete(rd, only_new=True)
    rd["keep"] = [str(x) for x in req.get("keep_products") or [] if isinstance(x, (str, int))][:120]  # the products of the cart this plan replaces
    plan = build_plan(g, rd, principal)
    plan["usage"] = cost(calls)
    if req.get("image") and reused:
        plan["photo_read"] = "reused"  # this photo was read earlier today and that reading is used again; no model looked at it this time
    plan["next"] = ("Pass checkout to create_checkout unchanged (line_items, delivery_date, exclude_allergens, intent, rationale). Tell the buyer what is under not_ordered (each held dish asks one question), pantry (staples left to the kitchen's own shelf; send order_staples true to order them), flags, warnings and budget." if plan["status"] == "ready"
                    else "Ask the buyer the questions, fix the request and call plan_menu_order again. Do not order anything from this answer." if plan["status"] == "needs_answers" else "Nothing can be ordered from this menu.")
    g._logp(principal, trace, "menu_plan", (f"Planned a menu order: {len(rd['dishes'])} dishes for {rd['guests']:,} guests, {plan['totals']['lines']} lines, {len(plan['not_ordered'])} not ordered." if plan["status"] == "ready"
                                             else f"A menu could not be planned yet: {len(plan.get('questions') or [])} question(s) for the buyer."), {"status": plan["status"], "dishes": [d["name"] for d in rd["dishes"]]})
    return g._prove({"ucp": g._env(), **plan})


NOT_MENU = "There is nothing to cook from in this photo. The model saw: {what}. Nothing is ordered. A menu, a lunch list, a week plan or a note with dishes on it all work."
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
    {"id": "lounas", "file": "lounaslista.jpg", "title": "Lounaslista", "sub": "a Finnish lunch week, 120 portions a day"},
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

    locks: dict[str, threading.Lock] = {}

    def lock_of(name: str) -> threading.Lock:
        with lock:
            return locks.setdefault(name, threading.Lock())

    def idem(request: Request, body: dict) -> str | None:
        """The key a caller sends so that a repeat of the same request (a retry after a dropped connection) is answered with the first one's result."""
        return re.sub(r"[^A-Za-z0-9_.:-]", "", str(body.get("key") or request.headers.get("idempotency-key") or ""))[:80] or None

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
        out["flags"] = flags(run["reading"]) if run.get("reading") else []
        out["by_hand"] = with_estimate(byhand(), ((run.get("plan") or {}).get("totals") or {}).get("lines") or 0)
        return out

    def offer(run, principal):
        """The whole basket through the normal checkout: rules, approval link if a rule asks, signed offer."""
        ck = run["plan"]["checkout"]
        t = time.time()
        c = g.create_checkout({"line_items": ck["line_items"], "context": {"intent": ck["intent"]},
                               "wholesale": {"delivery_date": ck["delivery_date"], "rationale": ck["rationale"], "requirements": {"exclude_allergens": ck["exclude_allergens"]}}}, principal, run["trace"])
        run["seconds"]["offer"] = round(time.time() - t, 2)
        g.set_asker(c["id"], MENU_ASKER)  # the record keeps who asked apart from who approves
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
        slug, key = c["acct"]["slug"], idem(request, body)
        with lock_of(f"read:{slug}:{key or secrets.token_hex(4)}"):  # the same request sent twice waits for the first and gets its run
            old = g.store.get(f"menu_key:{slug}:{key}") if key else None
            if old and g.store.get("menu_run:" + old):
                return view(g.store.get("menu_run:" + old))
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
                g._logp(p, run["trace"], "menu_refused", f"A photo was refused: there is nothing to cook from in it ({verdict['what'] or 'something else'}). Nothing was ordered.", {"run": rid})
            else:
                q = sanity(rd)
                rules, q2 = parse_notes(rd) if not any(x["about"] == "guests" for x in q) else ([], [])
                run["reading"], run["questions"] = rd, q + q2
                run["status"] = "needs_answers" if q + q2 else "read"
                g._logp(p, run["trace"], "menu_read", f"Read a menu from a photo: {len(rd['dishes'])} dishes for {rd['guests'] if rd['guests'] is not None else 'an unknown number of'} guests, {len(rd['notes']) + sum(len(d['notes']) for d in rd['dishes'])} dietary notes."
                        + (f" {len(q + q2)} thing(s) to ask before anything is ordered." if q + q2 else ""), {"run": rid, "model": READ_MODEL, "tokens_in": run["usage"]["tokens_in"], "tokens_out": run["usage"]["tokens_out"]})
            save(run)
            if key:
                g.store.put(f"menu_key:{slug}:{key}", rid)
            return view(run)

    @r.post("/api/menu/plan", include_in_schema=False)
    def plan(request: Request, response: Response, body: dict = Body(...)):
        """Step 2: the bill of materials and one signed offer. Takes the person's corrections to the reading when there were questions."""
        c = ctx(request, response)
        key = idem(request, body)
        with lock_of("plan:" + run_of(body.get("run"), c)["id"]):  # one plan at a time per menu; a repeat of the same request gets the first one's result
            run = run_of(body.get("run"), c)
            if run.get("order") or (key and run.get("plan_key") == key and run.get("status") in ("ready", "needs_answers", "nothing_to_order")):
                return view(run)
            p, t = principal_of(run), time.time()
            if isinstance(body.get("reading"), dict):
                rd = normalize(body["reading"], "model")
                if any(not d["card"] and not d["recipe"] and d["food"] for d in rd["dishes"]):
                    _spend("web:" + c["sid"], 40)
                    more = complete(rd, only_new=True)
                    run["calls"] = run.get("calls", []) + more
                    run["usage"] = cost(run["calls"])
                run["reading"], run["edited"] = rd, True
            if not run.get("reading"):
                raise GatewayError(409, "invalid_state", run.get("message") or "There is no reading to plan from.")
            if "order_staples" in body:  # the one tap that adds the pantry staples to the order, or takes them off again
                run["reading"]["order_staples"] = body["order_staples"] is True
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
            run["plan_key"] = key
            save(run)
            return view(run)

    def approval_of(run) -> dict:
        """Where the approver's decision on this menu's order stands. The decision itself is made on the approval page, never here."""
        ck = run.get("checkout") or {}
        if not ck.get("id"):
            return {"state": "none"}
        c = g.get_checkout(ck["id"])  # also lets an expired offer expire
        ap = c["wholesale"].get("approval") or {}
        dev = ap.get("device") or {}
        state = ("ordered" if run.get("order") else "waiting" if c["status"] == "requires_escalation" else "rejected" if ap.get("decision") == "rejected"
                 else "approved" if c["status"] == "ready_for_complete" and ap.get("decision") == "approved" else "ready" if c["status"] == "ready_for_complete" else "gone")
        out = {"state": state, "asked_by": MENU_ASKER, "url": ck.get("continue_url"), "expires_at": c.get("expires_at"), "to_clear": len(g.clears_of(c)) if state == "waiting" else 0}
        if ap:
            out["approval"] = {"decision": ap["decision"], "by": ap["by"], "at": ap["at"], "note": ap.get("note") or "", "asked_by": ap.get("asked_by"), "cleared": len(ap.get("clears") or []),
                               "device": {"name": dev["device"], "how": dev.get("how") or "a passkey", "thumbprint": dev["thumbprint"]} if dev else None}
        return out

    @r.get("/api/menu/approval/{rid}", include_in_schema=False)
    def approval(rid: str, request: Request, response: Response):
        """The menu page waits here while the approver decides on their own device. Reading this never approves anything."""
        run = run_of(rid, ctx(request, response))
        out = approval_of(run)
        if out["state"] in ("rejected", "gone") and not run.get("order") and (run.get("checkout") or {}).get("status") == "requires_escalation":
            run["checkout"]["status"] = "canceled" if out["state"] == "rejected" else "incomplete"  # the plan on the page no longer has an open offer
            run["checkout"]["decided"] = out.get("approval")
            save(run)
        return out

    @r.post("/api/menu/order", include_in_schema=False)
    def order(request: Request, response: Response, body: dict = Body(...)):
        """Step 3: place the order. An offer every rule passed is placed at once. One that a rule sent to a person is placed
        only after the approver said yes on the approval page; this endpoint cannot approve, whoever calls it."""
        c = ctx(request, response)
        with lock:
            run = run_of(body.get("run"), c)
            if run.get("order"):
                return view(run)
            ck = run.get("checkout") or {}
            if not ck.get("id") or ck.get("status") not in ("ready_for_complete", "requires_escalation"):
                raise GatewayError(409, "invalid_state", "There is no offer to order from. Plan the menu first.")
            p, t = principal_of(run), time.time()
            where = approval_of(run)
            if where["state"] == "waiting":
                raise GatewayError(409, "approval_pending", "This order is waiting for its approver. Nobody can approve it from the menu page: the approver decides on the approval page, on their own device, and the order is placed then.")
            if where["state"] == "rejected":
                raise GatewayError(409, "rejected", f"{where['approval']['by']} rejected this order. Nothing was ordered.")
            run["seconds"]["person"] = round(t - run["t0"] - sum(run["seconds"].get(k, 0) for k in ("read", "plan")), 2)
            cur = g.get_checkout(ck["id"], p)
            done = g.complete_checkout(ck["id"], {"ap2": {"checkout_mandate": g.agent_proof(cur, run["mandate"])}}, p)
            if done.get("status") != "completed" or not done.get("order"):
                ck["status"], ck["errors"] = done.get("status"), [m["content"] for m in done.get("messages", []) if m.get("type") in ("error", "warning")][-3:]
                save(run)
                return view(run)
            now = time.time()
            run["seconds"]["order"], run["seconds"]["total"] = round(now - t, 2), round(now - run["t0"], 2)
            ck["status"] = "completed"
            ap = where.get("approval") or {}
            run["order"] = {**done["order"], "at": iso(now), "approved_by_person": bool(run["checkout"].get("continue_url")),
                            **({"asked_by": ap.get("asked_by") or MENU_ASKER, "approved_by": ap["by"], "approved_on": ap.get("device"), "cleared": ap.get("cleared", 0)} if ap.get("decision") == "approved" else {})}
            run["status"] = "ordered"
            save(run)
            pl = run["plan"]
            card = {
                "run": run["id"], "at": iso(now), "source": run["source"], "edited": bool(run.get("edited")), "seconds": run["seconds"]["total"], "phases": run["seconds"],
                "guests": run["reading"]["guests"], "dishes": len(run["reading"]["dishes"]), "title": run["reading"]["title"] if run.get("sample") else None,
                "line_count": pl["totals"]["lines"], "subtotal": pl["totals"]["subtotal"], "not_ordered": len(pl["not_ordered"]), "replaced_by_role": sum(1 for l in pl["lines"] if l.get("substitutes_for")),
                "lines": [{"title": l["product"]["title"], "units": l["units"], "unit": l["unit"], "ok": True} for l in pl["lines"]],
                "cost": {k: run["usage"][k] for k in ("tokens_in", "tokens_out", "usd", "eur", "basis")}, "models": sorted({x["model"] for x in run.get("calls", [])}),
                "order": {"id": done["order"]["id"], "label": done["order"].get("label"), "url": order_url(done["order"]["id"])}, "delivery_date": pl["delivery_date"],
            }
            g.store.put("menu_latest:" + run["slug"], card)
            if run.get("sample") and not run.get("edited"):  # only a sample menu, unchanged, is shown to other visitors: a visitor's own photo and order stay theirs
                g.store.put("menu_latest", {**card, "order": None})
            g._logp(p, run["trace"], "menu_order", f"A menu photo became order {done['order'].get('label')}: {pl['totals']['lines']} lines in {run['seconds']['total']:.0f} s from photo to order, model cost €{run['usage']['eur']:.4f}.",
                    {"run": run["id"], "order": done["order"]["id"], "seconds": run["seconds"], "tokens_in": run["usage"]["tokens_in"], "tokens_out": run["usage"]["tokens_out"]})
            return view(run)

    @r.get("/api/menu/run/{rid}", include_in_schema=False)
    def get_run(rid: str, request: Request, response: Response):
        return view(run_of(rid, ctx(request, response)))

    def shared_latest():
        """The last sample menu, unchanged, that anyone ordered: the only run other visitors may see, and without its order."""
        m = g.store.get("menu_latest")
        return {**m, "order": None} if m and m.get("title") and not m.get("edited") else None

    @r.get("/api/menu/latest", tags=["menu"], summary="The last menu photo that became an order: seconds, lines, model cost, and the by-hand time (measured or estimated)")
    def latest(request: Request):
        tok = request.cookies.get("aimo_s")
        ses = (g.store.get("session:" + tok) if tok else None) or {}
        mine = (ses.get("view") or STAGE) if ses.get("operator") else ses.get("slug")
        m = (g.store.get("menu_latest:" + mine) if mine else None) or shared_latest()  # this visitor's own last menu, else the last sample menu anyone ran
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
            last = g.store.get("menu_latest:" + c["acct"]["slug"]) or shared_latest()
            run = g.store.get("menu_run:" + last["run"]) if last else None
        if run and run.get("plan"):
            pl, rd, src = run["plan"], run["reading"], run["source"] if run.get("sample") else "the last menu you ordered here"
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
            for f in l["for"]:
                amounts[f["item"]] = amounts.get(f["item"], 0) + l["pack"]["need"] * f["share"]
                need[f["item"]]["unit"] = l["pack"]["unit"]
        from .shopchat import DISH_FI, ITEM_FI  # the shop's Finnish words for dishes and ingredients; imported here because shopchat loads after this module
        item_fi = {x.get("item"): x["fi"] for d in rd["dishes"] for x in d.get("recipe") or [] if isinstance(x, dict) and x.get("fi")}
        written = {d["name"]: d.get("as_written") for d in rd["dishes"]}
        dish_fi = lambda n: DISH_FI.get(n) or written.get(n) or n
        items = [{"item": k, "fi": item_fi.get(k) or ITEM_FI.get(k), "amount": qty(amounts[k], v["unit"]), "need": round(amounts[k], 1), "unit": v["unit"], "dishes": sorted(v["dishes"]), "dishes_fi": [dish_fi(n) for n in sorted(v["dishes"])]}
                 for k, v in need.items()]
        title = rd["title"] or "Menu"
        return {"from": src, "from_fi": BYHAND_FROM_FI.get(src) or ("näytekuva" if src.startswith("sample photo") else None), "title": title, "title_fi": BYHAND_TITLE_FI.get(title.strip().lower()), "guests": rd["guests"],
                "delivery_date": pl["delivery_date"], "notes": rd["notes"] + [f"{d['name']}: {', '.join(d['notes'])}" for d in rd["dishes"] if d["notes"]],
                "notes_fi": rd["notes"] + [f"{dish_fi(d['name'])}: {', '.join(d['notes'])}" for d in rd["dishes"] if d["notes"]],
                "exclude_allergens": pl["requirements"]["exclude_allergens"], "list": items, "agent_lines": pl["totals"]["lines"]}

    @r.get("/api/byhand/task", include_in_schema=False)
    def byhand_task(request: Request, response: Response, run: str = ""):
        return {**task_of(ctx(request, response), run), "by_hand": byhand()}

    @r.get("/api/byhand/search", include_in_schema=False)
    def byhand_search(q: str = ""):
        """The bare webshop: catalogue rows only. No live price or stock call per search, to be gentle on Valio Aimo's API; prices come at checkout."""
        docs, _, _ = g.cat.search(q[:80], {}, limit=24) if q.strip() else ([], None, None)
        row = lambda d: {"id": d["id"], "title": d["title"], "title_fi": (d.get("names") or {}).get("fi"), "brand": d.get("brand"), "unit": UNITS.get(d["unit"], (0, d["unit"]))[1], "step": d["increment"] / 10 ** d["scale"],
                         "al": list(d["contains"]), "may": list(d["may_contain"]), "cl": [x for x in d["claims"] if x in ("FREE_FROM:LACTOSE", "FREE_FROM:GLUTEN", "DIET:VEGAN")],
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
            hits = allergen_hits(ex, d["contains"], d["claims"]) | allergen_hits(ex, d["may_contain"], d["claims"]) | named_hits(ex, d)
            ok = bool(f.get("orderable")) and not (f.get("stock_known") and units > f["available"])
            amount = round(f["price"] * units) if f.get("price") else 0
            total += amount if ok else 0
            rows.append({"id": pid, "title": d["title"], "title_fi": (d.get("names") or {}).get("fi"), "units": units, "amount": amount, "orderable": ok, "why": None if ok else REASONS.get(f.get("reason"), "not enough stock") if not f.get("orderable") else "not enough stock",
                         "why_code": None if ok else (f.get("reason") or "stock") if not f.get("orderable") else "stock", "hits": sorted(str(h) for h in hits),
                         "breaks": f"contains or may contain {_names(hits)}, which this menu rules out" if hits else None})
        secs = round(now - a["t0"], 1)
        finished = len(basket) >= math.ceil(0.8 * len(task["list"]))
        res = {"seconds": secs, "minutes": round(secs / 60, 1), "lines": len(basket), "list_lines": len(task["list"]), "finished": finished, "subtotal": total, "rows": rows,
               "breaks": sum(1 for x in rows if x["breaks"]), "cannot_order": sum(1 for x in rows if not x["orderable"]), "agent_lines": task["agent_lines"], "needed": math.ceil(0.8 * len(task["list"])),
               "counts": "This attempt counts in the measured time." if finished else f"This attempt is not counted: the basket has {len(basket)} of {len(task['list'])} items on the list (at least {math.ceil(0.8 * len(task['list']))} are needed)."}
        a.update({"seconds": secs, "result": res})
        g.store.put("byhand:" + a["id"], a)
        with lock:
            g.store.put("byhand_attempts", (g.store.get("byhand_attempts", []) + [{"id": a["id"], "who": a["who"], "at": iso(now), "seconds": secs, "lines": len(basket), "list_lines": len(task["list"]), "finished": finished, "breaks": res["breaks"]}])[-300:])
        return {**res, "by_hand": byhand()}

    return r


# The wedding sample, typed. Used by /byhand before anyone has ordered from a menu, and by tests.
BYHAND_FROM_FI = {"the wedding sample menu": "häiden näytemenu", "the last menu you ordered here": "viimeisin täällä tilaamasi menu"}
BYHAND_TITLE_FI = {"wedding dinner": "Hääillallinen", "menu": "Menu"}
WEDDING = {"title": "Wedding dinner", "date": "2026-11-21", "date_text": "Sat 21 Nov", "guests": 180, "notes": ["Nut-free kitchen", "Table 6 lactose-free"],
           "dishes": [{"name": "Creamy salmon soup", "card": "salmon_soup", "notes": ["L"]}, {"name": "Green salad", "card": "green_salad"},
                      {"name": "Chicken breast with thyme cream sauce", "card": "chicken_thyme_cream", "notes": ["L", "G"]}, {"name": "Roasted potatoes and carrots", "card": "roast_potatoes"},
                      {"name": "Mushroom risotto", "card": "mushroom_risotto", "portions": 14, "notes": ["vegetarian"]}, {"name": "Strawberry cream cake", "card": "strawberry_cream_cake"},
                      {"name": "Coffee and tea", "card": "coffee_tea"}, {"name": "Rye bread and butter", "card": "bread_butter"}]}
