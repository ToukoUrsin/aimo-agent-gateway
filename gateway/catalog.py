"""Static product index built once from the challenge product file (Synkka / GS1 data).

Price and stock are never stored here: they are customer specific and change, so they
are fetched live for the handful of products a request narrows down to.
"""
import csv
import json
import math
import re
import unicodedata
from collections import Counter
from pathlib import Path

from .config import DATA, PORTAL

INDEX = DATA / "catalog.json"
KINDS = Path(__file__).with_name("kinds.json")

# GS1 AllergenTypeCode -> plain English
ALLERGENS = {
    "AM": "milk", "AE": "eggs", "AF": "fish", "AC": "crustaceans", "UM": "molluscs",
    "AP": "peanuts", "AN": "tree nuts", "AY": "soybeans", "AS": "sesame", "BM": "mustard",
    "BC": "celery", "NL": "lupin", "AU": "sulphites", "AW": "cereals containing gluten",
    "UW": "wheat", "GB": "barley", "GO": "oats", "NR": "rye", "GS": "spelt", "GK": "kamut",
    "ML": "lactose", "SA": "almond", "SC": "cashew", "SH": "hazelnut", "SM": "macadamia",
    "SP": "pecan", "SR": "brazil nut", "ST": "pistachio", "SW": "walnut",
}
# A group and its members stand for each other: a buyer who excludes tree nuts excludes hazelnut, and a product that
# only says "contains tree nuts" may contain any of them.
ALLERGEN_GROUPS = {"AN": {"SA", "SC", "SH", "SM", "SP", "SR", "ST", "SW"}, "AW": {"UW", "GB", "GO", "NR", "GS", "GK"}}


def widen_allergens(codes) -> set:
    out = set(codes or [])
    for group, members in ALLERGEN_GROUPS.items():
        if group in out:
            out |= members
        elif out & members:
            out.add(group)
    return out


def allergen_hits(excluded, product_codes, claims=()) -> set:
    """Which of a product's allergen codes fall under the buyer's exclusions. Oats count as a gluten cereal unless the
    product is certified gluten-free and the buyer did not exclude oats by name."""
    hits = widen_allergens(excluded) & set(product_codes or [])
    if "GO" in hits and "GO" not in set(excluded or []) and "FREE_FROM:GLUTEN" in set(claims or []):
        hits.discard("GO")
    return hits


# Category families that are not food or drink (tableware, cleaning, packaging, office). No allergen declaration is expected there.
NONFOOD_CATS = {"20", "30", "32", "63", "64", "67", "77", "90"}
# What a manufacturer's own claim settles when the product has no allergen declaration.
CLAIM_CLEARS = {"FREE_FROM:GLUTEN": {"AW", "UW", "GB", "GO", "NR", "GS", "GK"}, "DIET:FREE_FROM_GLUTEN": {"AW", "UW", "GB", "GO", "NR", "GS", "GK"}, "FREE_FROM:WHEAT": {"UW"},
                "DIET:VEGAN": {"AM", "ML", "AE", "AF", "AC", "UM"}, "FREE_FROM:EGGS": {"AE"}, "FREE_FROM:SOY": {"AY"}, "FREE_FROM:LACTOSE": {"ML"}}


# Words in a product name that point at an allergen. Used only on products with no declaration. A blank field can be
# cleared by a person; a blank field on a product named for what is excluded is refused (see named_hits).
ALLERGEN_WORDS = {
    "AN": r"almond|hazelnut|walnut|cashew|pistachio|pecan|macadamia|praline|marzipan|nougat|\bnuts?\b|pähkinä|manteli",
    "AP": r"peanut|maapähkinä", "AF": r"\bfish|salmon|\bcod\b|saithe|tuna|herring|trout|\bpike|perch|zander|whitefish|anchov|\bkala(?!mata)|lohi\b|silakka|\bsilli|tonnikala|siika|kuha",
    "AC": r"shrimp|prawn|crayfish|\bcrab|lobster|katkarapu|\brapu|hummeri", "UM": r"mussel|\bclam|oyster|squid|octopus|scallop|simpukka",
    "AM": r"\bmilk|cheese|\bcream|butter\b|yogh?urt|quark|halloumi|mozzarella|\bmaito|juusto|\bkerma|\bvoi\b|jogurtti|rahka", "AE": r"\beggs?\b|mayonnaise|\bmuna",
    "AY": r"\bsoya?\b|tofu|soija", "AS": r"sesame|seesami|tahini", "BM": r"mustard|sinappi", "BC": r"celery|celeriac|selleri", "NL": r"lupin",
    "AW": r"wheat|\brye\b|barley|\boats?\b|spelt|flour|bread|pasta\b|vehnä|\bruis|\bohra|\bkaura",
}
ALLERGEN_WORDS |= {"ML": ALLERGEN_WORDS["AM"], "UW": r"wheat|vehnä", "NR": r"\brye\b|\bruis", "GB": r"barley|\bohra", "GO": r"\boats?\b|\bkaura", "GS": r"spelt",
                   "SA": r"almond|manteli", "SH": r"hazelnut|hasselpähkinä", "SW": r"walnut|saksanpähkinä", "SC": r"cashew", "ST": r"pistachio|pistaasi", "SP": r"pecan", "SM": r"macadamia"}


def named_allergens(codes, d) -> list:
    """Which of these allergens the product's own name points at."""
    return sorted(c for c in codes if c in ALLERGEN_WORDS and re.search(ALLERGEN_WORDS[c], d.get("title") or "", re.I))


def named_hits(excluded, d) -> set:
    """The excluded allergens that a product with no declaration names in its own title. A name is evidence enough to
    refuse and never enough to pass: shelled almonds with a blank allergen field do not go to a nut-free kitchen, and
    no person can wave them through. (An "almond potato" is refused too. The agent picks another potato.)"""
    return set(named_allergens(sorted(allergens_unchecked(excluded, d)), d))


def declares_allergens(d) -> bool:
    """True when the manufacturer stated anything about allergens: contains, may contain or free from."""
    return bool(d.get("contains") or d.get("may_contain") or d.get("free_from"))


def allergens_unchecked(excluded, d) -> set:
    """The excluded allergens that cannot be checked on a product because its manufacturer declared no allergens at
    all. Silence is not "none": a third of the food in this catalogue has no declaration, fish and celery included.
    A manufacturer's own claim still settles what it covers. Not asked of things that are not food."""
    if not excluded or declares_allergens(d) or str(d.get("category") or "")[:2] in NONFOOD_CATS:
        return set()
    cleared = set().union(*(CLAIM_CLEARS.get(c, set()) for c in d.get("claims") or []))
    return set(excluded) - cleared


ALLERGEN_BY_NAME = {v: k for k, v in ALLERGENS.items()} | {
    "gluten": "AW", "nuts": "AN", "egg": "AE", "soy": "AY", "soya": "AY", "peanut": "AP",
}

# Friendly aliases an agent may send -> canonical claim "QUALIFIER:SUBJECT" (GS1 nutritional claim codes)
CLAIM_ALIASES = {
    "lactose_free": "FREE_FROM:LACTOSE", "laktoositon": "FREE_FROM:LACTOSE",
    "low_lactose": "LOW:LACTOSE", "vahalaktoosinen": "LOW:LACTOSE",
    "gluten_free": "FREE_FROM:GLUTEN", "gluteeniton": "FREE_FROM:GLUTEN",
    "vegan": "DIET:VEGAN", "vegaaninen": "DIET:VEGAN", "vegetarian": "DIET:VEGETARIAN",
    "no_added_sugar": "NO_ADDED:SUGARS", "egg_free": "FREE_FROM:EGGS", "soy_free": "FREE_FROM:SOY",
}
LABEL_ALIASES = {
    "organic": "EU_ORGANIC_FARMING", "luomu": "EU_ORGANIC_FARMING",
    "hyvaa_suomesta": "GOODS_FROM_FINLAND_BLUE_SWAN", "finnish": "GOODS_FROM_FINLAND_BLUE_SWAN",
    "avainlippu": "MADE_IN_FINLAND_FLAG_WITH_KEY", "sydanmerkki": "FINNISH_HEART_SYMBOL",
    "msc": "MARINE_STEWARDSHIP_COUNCIL_LABEL", "asc": "AQUACULTURE_STEWARDSHIP_COUNCIL",
    "fairtrade": "FAIR_TRADE_MARK", "rainforest_alliance": "RAINFOREST_ALLIANCE",
    "nordic_swan": "SVANEN", "eu_ecolabel": "EU_ECO_LABEL",
}
# Phrases in a free-text query that are really hard requirements, not search words.
QUERY_CLAIMS = [
    (r"lactose[\s-]*free|laktoosit\w*|laktosfri\w*", "FREE_FROM:LACTOSE"),
    (r"gluten[\s-]*free|gluteenit\w*|glutenfri\w*", "FREE_FROM:GLUTEN"),
    (r"\bvegan\w*|vegaani\w*", "DIET:VEGAN"),
]
QUERY_LABELS = [(r"\borganic\b|\bluomu\w*|\bekologisk\w*", "EU_ORGANIC_FARMING")]

# SAP sales unit code -> (UN/CEFACT Rec 20/21 code, label)
UNITS = {
    "ST": ("C62", "piece"), "KG": ("KGM", "kg"), "KI": ("CS", "case"), "CS": ("CS", "case"),
    "PAK": ("PK", "pack"), "BAG": ("BG", "bag"), "RAS": ("BX", "box"), "PRK": ("JR", "jar"),
    "BOT": ("BO", "bottle"), "SK": ("SA", "sack"), "KRT": ("CT", "carton"), "KLE": ("RO", "roll"),
}
TEMP = {7: "frozen", 8: "ambient", 9: "ambient"}  # other codes are chilled classes


def fold(s: str) -> str:
    s = unicodedata.normalize("NFKD", s.lower())
    return "".join(c for c in s if not unicodedata.combining(c))


# Words that do not change what a product is: fat level, heat treatment, claims and pack words.
_NOT_KIND = set("whole full semi skimmed skim low fat light lite uht esl organic luomu fresh lactose lactosefree free gluten glutenfree natural plain "
                "pcs kpl pack of the and for with a an some".split())
# In Finland lactose-free milk is sold as "milk drink" (maitojuoma): the name follows from how the lactose is taken out.
_KIND_SAME = {"milk drink": "milk"}


def _kind_of(text: str, brand: str = "") -> str:
    t = fold(re.sub(r"\S+[®™]", " ", text))                                    # sub-brands: Eila®, Play®
    t = re.sub(r"\d+(?:[.,]\d+)?(?:\s*x\s*\d+(?:[.,]\d+)?)?\s*(?:%|kg|g|l|dl|cl|ml|x)?(?![a-z])", " ", t)  # sizes, pack counts, percentages
    for pat, _ in QUERY_CLAIMS + QUERY_LABELS:
        t = re.sub(pat, " ", t)
    own = set(re.findall(r"[a-z]+", fold(brand)))
    words = [w for w in re.findall(r"[a-z]+", t) if w not in _NOT_KIND and w not in own]
    if words and len(words[-1]) > 3 and words[-1].endswith("s") and not words[-1].endswith("ss"):
        words[-1] = words[-1][:-1]
    k = " ".join(words)
    return _KIND_SAME.get(k, k)


def kind(d) -> str:
    """What a product is, in its own words: its English title without brand, size, pack and the words that do not change what it is.
    "Valio semi skimmed milk 1 l lactose free UHT" is "milk"; "Valio coffee milk 5 dl lactose free" is "coffee milk"; sour milk is "sour milk".
    A request for milk gets milk, and is told about the other kinds instead of being handed the cheapest of them."""
    if "_kind" not in d:
        d["_kind"] = _kind_of(d["title"], d.get("brand") or "")
    return d["_kind"]


def pieces(d) -> int | None:
    """How many pieces one sales unit holds, when the product's own name says so: "egg M10 580g" is 10, "bun 40x80g" is 40,
    "napkin 125pcs" is 125, "20 x ... egg L6" is 120. None when the name does not say."""
    if "_pieces" not in d:
        t, n = d["title"], None
        m = re.search(r"\b(\d{1,4})\s*(?:pcs|pc|kpl|pieces)\b", t, re.I) or re.search(r"\b(\d{1,3})\s*x\s*\d+(?:[.,]\d+)?\s*(?:g|kg|ml|cl|dl|l)\b", t, re.I)
        if not m and "egg" in kind(d).split():
            m = re.search(r"\b[SML]{1,2}(\d{1,3})\b", t)   # egg size and count: M10, L15, S90
        if m:
            n = int(m.group(1))
            lead = re.match(r"\s*(\d{1,3})\s*x\s+\D", t)
            if lead and m.start() > 0:
                n *= int(lead.group(1))
        d["_pieces"] = n if n and n > 1 else None
    return d["_pieces"]


def case_count(d) -> int | None:
    """How many bottles or cans a case holds when the product's name leads or ends with the count: "12 x Freixenet ... 0,2 L bottle", "24x Bonaqua", "... 0,33l x24"."""
    m = re.match(r"\s*(\d{1,3})\s*x\s*[^\d\s]", d["title"], re.I) or re.search(r"\bx\s*(\d{1,3})\s*$", d["title"], re.I)
    return int(m.group(1)) if m and int(m.group(1)) > 1 else None


_SIZE = {"g": ("kg", 0.001), "kg": ("kg", 1), "ml": ("l", 0.001), "cl": ("l", 0.01), "dl": ("l", 0.1), "l": ("l", 1), "ltr": ("l", 1)}


def named_total(title: str):
    """What a case holds when the product's own name spells it out: "12x500g" and "12 x Brand coffee 500 g" are 6 kg. Returns (unit, amount, count) or None."""
    t = title.lower()
    m = re.search(r"\b((?:\d{1,4}\s*x\s*)+)(\d+(?:[.,]\d+)?)\s*(g|kg|ml|cl|dl|l|ltr)\b", t) or re.match(r"\s*(\d{1,3}\s*x)\s+\D.*?(\d+(?:[.,]\d+)?)\s*(g|kg|ml|cl|dl|l|ltr)\b", t)
    if not m:
        return None
    n = math.prod(int(x) for x in re.findall(r"\d+", m.group(1)))
    lead = re.match(r"\s*(\d{1,3})\s*x\s+\D", t)
    if lead and m.start() > 0:
        n *= int(lead.group(1))
    key, f = _SIZE[m.group(3)]
    return key, round(n * float(m.group(2).replace(",", ".")) * f, 4), n


def query_kind(query: str) -> str:
    return _kind_of(query or "")


# Words in a request that say nothing about what the product is.
_SOFT = set("usual normal regular standard ordinary basic cheap cheapest cheaper best good our same any more extra need needed please kitchen brand".split())
# The same thing under another name. Only the plain, common ones: the kinds file carries the rest.
_ALIAS = {"ground": "minced", "mince": "minced", "yogurt": "yoghurt", "yogurts": "yoghurt", "fries": "french fry", "eggplant": "aubergine", "zucchini": "courgette",
          "cilantro": "coriander", "arugula": "rocket", "prawn": "shrimp", "prawns": "shrimp", "soya": "soy", "capsicum": "bell pepper", "spuds": "potato"}
_PLANT = {"oat", "soy", "almond", "rice", "pea", "hemp", "cashew"}   # "oat milk" is sold as oat drink


def _stem(w: str) -> str:
    """Enough to make "eggs" meet "egg" and "berries" meet "berry". Applied to both sides, so it only has to be consistent."""
    if w.endswith("atoes"):
        return w[:-2]
    if len(w) > 4 and w.endswith("ies"):
        return w[:-3] + "y"
    if len(w) > 3 and w.endswith("ie"):
        return w[:-2] + "y"
    if len(w) > 3 and w.endswith("s") and not w.endswith("ss"):
        return w[:-1]
    return w


def query_words(q: str) -> dict:
    """{stem: the forms it was written in} for the words of a request that can say what the product is. Sizes, claims and filler are dropped."""
    t = re.sub(r"\d+(?:[.,]\d+)?(?:\s*x\s*\d+(?:[.,]\d+)?)?\s*(?:%|kg|g|l|dl|cl|ml|x)?(?![a-z])", " ", fold(q or ""))
    for pat, _ in QUERY_CLAIMS + QUERY_LABELS:
        t = re.sub(pat, " ", t)
    out = {}
    for w in re.findall(r"[a-z]+", t):
        if w in _NOT_KIND or w in _SOFT or len(w) < 2:
            continue
        for a in _ALIAS.get(w, w).split():
            out.setdefault(_stem(a), set()).update({a, w})
    if "milk" in out and _PLANT & set(out):
        out["drink"] = out.pop("milk")
    return out


def _txt(arr, lang):
    for x in arr or []:
        if x.get("language") == lang and x.get("value"):
            return x["value"]
    return None


def _content(p) -> dict:
    """Litres / kg in one sales unit, from the Synkka packaging data. May be missing."""
    s = p.get("synkkaData") or {}
    base = next((u for u in s.get("unitConversions", []) if u.get("synkkaPackagingType") == "BASE_UNIT_OR_EACH"), None)
    size = next((u["sizeInBaseUnits"] for u in p.get("units", []) if u["unitId"] == p.get("salesUnit")), None)
    if size is None and p.get("salesUnit") == p.get("baseUnit"):
        size = 1.0
    out = {}
    if base and size:
        v, w = base.get("volume") or {}, base.get("netWeight") or {}
        vf = {"LTR": 1, "DLT": 0.1, "CLT": 0.01, "MLT": 0.001}.get(v.get("unit"))
        wf = {"KGM": 1, "GRM": 0.001}.get(w.get("unit"))
        if vf and v.get("value"):
            out["l"] = round(v["value"] * vf * size, 4)
        if wf and w.get("value"):
            out["kg"] = round(w["value"] * wf * size, 4)
    if p.get("salesUnit") == "KG":
        out["kg"] = 1.0
    return out


def normalize(p, rel, co2) -> dict:
    s = p.get("synkkaData") or {}
    cls = {c["name"]: c.get("values", []) for c in s.get("classifications", [])}
    claims = sorted({f"{v['unit']}:{v['synkkaId']}" for v in cls.get("nutritionalClaim", []) if v.get("synkkaId") and v.get("unit")})
    contains = sorted({v["id"] for v in cls.get("allergen", []) if v.get("unit") == "CONTAINS" and v["id"] != "X99"})
    may = sorted({v["id"] for v in cls.get("allergen", []) if v.get("unit") == "MAY_CONTAIN"})
    free = sorted({v["id"] for v in cls.get("nonAllergen", [])})
    labels = sorted({v["synkkaId"] for v in cls.get("packagingMarkedLabel", []) if v.get("synkkaId")})
    lactose = next((v["value"] for v in cls.get("nutritionFact", []) if v.get("id") == "LACS"), None)
    lot = p.get("allowedLotSize") or 0.01
    unit = p.get("salesUnit") or "ST"
    scale = 2 if unit == "KG" else 0
    increment = max(1, round(lot * 10**scale)) if (lot >= 1 or unit == "KG") else 1
    names = {l: _txt(s.get("names"), l) for l in ("en", "fi", "sv")}
    title = names["en"] or names["fi"] or next((n["value"] for n in s.get("names", [])), p["id"])
    origin = (p.get("countryOfOrigin") or "").upper() or None
    sub_units = [{"unit": u["unitId"], "size": u["sizeInBaseUnits"], **({"gtin": u["gtin"]} if u.get("gtin") else {})} for u in p.get("units", [])]
    return {
        "id": p["id"],
        "gtin": p.get("salesUnitGtin") or s.get("gtin"),
        "title": title,
        "names": {k: v for k, v in names.items() if v},
        "brand": s.get("brand"),
        "vendor": p.get("vendorName"),
        "producer": next(({"gln": x.get("id"), "name": x.get("name")} for x in s.get("producers", [])), None),
        "category": p.get("category"),
        "origin": origin,
        "temp": TEMP.get(p.get("temperatureCondition"), "chilled") if p.get("temperatureCondition") else None,
        "unit": unit,
        "scale": scale,
        "increment": increment,
        "pack": sub_units,
        "content": _content(p),
        "claims": claims,
        "contains": contains,
        "may_contain": may,
        "free_from": free,
        "labels": labels,
        "lactose_g_per_100": lactose,
        "subs": [x["substituteProduct"] for x in p.get("substitutions", []) if x.get("substituteProduct") != p["id"]],
        "reliability": rel.get(p["id"]),
        "co2_per_kg": co2.get(p["id"]),
        "vat": s.get("vatCode"),
        "variable_weight": bool(s.get("variableUnit")),
        "data_updated": s.get("lastUpdated"),
        "_text": fold(" ".join(filter(None, [*names.values(), s.get("brand"), p.get("vendorName")]))),
    }


def build() -> int:
    items = json.load(open(PORTAL / "valio_aimo_junction26_product_data.json", encoding="utf-8"))
    rel = {r["productId"]: float(r["deliveryReliability"]) for r in csv.DictReader(open(PORTAL / "valio_aimo_junction26_delivery_reliability.csv")) if r["deliveryReliability"]}
    co2 = {}
    for r in csv.DictReader(open(PORTAL / "valio_aimo_junction26_carbon_footprint.csv")):
        v = r.get("AVG kg CO2-eq/kg")
        if v:
            co2[r["productId"]] = float(v)
    docs = [normalize(p, rel, co2) for p in items if not p.get("deleted")]
    json.dump(docs, open(INDEX, "w", encoding="utf-8"), ensure_ascii=False)
    return len(docs)


_SKIMMED = re.compile(r"(?<!semi )(?<!semi-)\bskimmed\b|\bfat[- ]free\b|\bnon[- ]?fat\b|\brasvaton")
_ONE_PCT = re.compile(r"\b1 ?%|ykkos")
_WHOLE = re.compile(r"\bwhole\b|\bfull[- ]fat\b|taysmai|helmjolk")
_SEMI = re.compile(r"\bsemi[- ]?skimmed\b|\blow[- ]fat\b|kevytmai|lattmjolk")

class Catalog:
    def __init__(self):
        if not INDEX.exists():
            build()
        self.docs = json.load(open(INDEX, encoding="utf-8"))
        self.by_id = {d["id"]: d for d in self.docs}
        self.by_gtin = {d["gtin"]: d for d in self.docs if d.get("gtin")}
        # What each product is, in a buyer's words. A model wrote these once from the product names (agent/kinds.py); a product it did not
        # name falls back to its own title. Requests are matched against them in code, below.
        have = json.load(open(KINDS, encoding="utf-8")) if KINDS.exists() else {}
        named = have.get("kinds", {})
        self.kinds_named = len(named)
        self.by_kind = {}
        for d in self.docs:
            d["_w"] = set(re.findall(r"[a-z0-9]+", d["_text"]))
            # The packaging data counts some cases twice ("60 x coffee 100 g" as 360 kg: sixty times what the name spells out). There the name is right.
            total = named_total(d["title"])
            over = (d["content"].get(total[0]) or 0) / total[1] if total and total[1] else 0
            if total and total[2] > 1 and (abs(over - total[2]) <= 0.05 * total[2] or over >= 100):
                d["content"] = {**d["content"], total[0]: total[1]}
                d["content_from"] = "name"
            if named.get(d["id"]):
                d["_kind"] = named[d["id"]]
            self.by_kind.setdefault(kind(d), []).append(d)
        self.by_kind.pop("", None)
        stems = lambda name: frozenset(_stem(w) for w in name.split())
        self.kind_stems = {k: stems(k) for k in self.by_kind}
        # The plain word a kind is a sort of: spaghetti and penne are pasta, whipping and cooking cream are cream.
        self.sorts, self.broad_stems = {}, {}
        for k, b in (have.get("broader") or {}).items():
            if b and k in self.by_kind and b != k:
                self.sorts.setdefault(b, []).append(k)
                self.broad_stems[b] = stems(b)

    def pick(self, query: str, filters: dict, pref=None, use_kind: str = ""):
        """The products a request is asking for, by kind. Returns (docs, filters, notes, info).

        "milk" gets the products that are milk, not coffee milk or milk chocolate; "eggs" gets eggs, not egg white powder.
        1. The request names a kind or a broader word: the longest one whose words are all in the request, in the request's order
           when two share the words (milk chocolate / chocolate milk). Words left over ("Valio", "sliced") must be in the product's name.
        2. Otherwise (a Finnish or Swedish word, a part of a name): the kind most of the name matches are.
        info["other"] lists the kinds that share the request's words. They are named, never offered.
        info["choices"] is set when the request does not say which kind: "chicken" is one smoked chicken, 39 chicken fillets and
        33 chicken strips; "pasta" is spaghetti, penne and macaroni. The caller goes by what the account usually buys, or asks."""
        q, f, notes = self._parse(query, filters)
        text_docs = self._text_match(q, f, 60)
        words = query_words(q)
        S, order = set(words), list(words)
        said = lambda d, left: all(any(v in d["_text"] for v in words[t] | {t}) for t in left)
        fits = lambda k, left: [d for d in self.by_kind.get(k, []) if said(d, left)]
        chosen, how, docs, choices = [], "none", [], {}

        def in_order(name):
            at = [order.index(_stem(w)) for w in name.split() if _stem(w) in S]
            return at == sorted(at)

        if pref or use_kind:
            chosen, how = [use_kind or kind(pref)], "preferred" if pref else "usual"
            docs = [d for d in fits(chosen[0], (S - self.kind_stems.get(chosen[0], S)) if use_kind else set()) if self.matches(d, f)]
            if use_kind and not docs:
                docs = [d for d in self.by_kind.get(use_kind, []) if self.matches(d, f)]
        elif S:
            names = {**{b: st for b, st in self.broad_stems.items() if st <= S}, **{k: st for k, st in self.kind_stems.items() if st <= S}}
            for n in sorted({len(st) for st in names.values()}, reverse=True):
                level = [k for k, st in names.items() if len(st) == n]
                level = [k for k in level if in_order(k)] or level
                own = {k: fits(k, S - names[k]) for k in level if k in self.by_kind}
                kids = {c: fits(c, S - names[k] - self.kind_stems[c]) for k in level for c in self.sorts.get(k, [])}
                own, kids = {k: v for k, v in own.items() if v}, {k: v for k, v in kids.items() if v}
                if not own and not kids:
                    continue
                mine, most = sum(len(v) for v in own.values()), sum(len(v) for v in kids.values())
                if not kids and own:   # no sorts known: the kinds that carry the same words stand in for them
                    most = max((len(self.by_kind[k]) for k, st in self.kind_stems.items() if k not in own and frozenset().union(*(names[o] for o in own)) <= st), default=0)
                    most = most if mine < 5 else 0
                chosen, how = sorted(own), "named"
                docs = [d for v in own.values() for d in v if self.matches(d, f)]
                if mine < 5 and most >= 5 * max(mine, 1) or not own:
                    choices = {**own, **dict(sorted(kids.items(), key=lambda kv: -len(kv[1])))}
                    if not kids:
                        core = frozenset().union(*(names[o] for o in own))
                        choices.update(sorted(((k, self.by_kind[k]) for k, st in self.kind_stems.items() if k not in own and core <= st), key=lambda kv: -len(kv[1]))[:8])
                break
        if not chosen and not choices:
            # What the words mean does not depend on the requirements, so the vote is over every product. A name that is the word
            # counts most, a compound that ends in it next ("kevytmaito" is a milk, "maitorahka" is not).
            toks = [t for t in re.findall(r"[a-z0-9]+", q) if len(t) > 1 and t not in {"of", "the", "and", "for", "with", "ja"}]
            vote, loose = Counter(), Counter()
            for d in self._text_match(q, {}, 400):
                n = sum(3 if t in d["_w"] else 2 if any(w.endswith(t) for w in d["_w"]) else 0 for t in toks)
                (vote if n else loose)[kind(d)] += n or 1
            vote = vote or loose   # a word found only inside other words is the weakest evidence
            if vote:
                (k, n), total = vote.most_common(1)[0], sum(vote.values())
                chosen, how = [k], "nearest"
                docs = [d for d in self.by_kind.get(k, []) if self.matches(d, f)]
                if n < 0.5 * total:
                    choices = {kk: self.by_kind[kk] for kk, _ in vote.most_common(6)}
        if _SKIMMED.search(q):  # a named variety means that variety: "skimmed" is not "semi skimmed", "fat free" is not "lactose free, low fat"
            docs = [d for d in docs if _SKIMMED.search(d["_text"]) and not _ONE_PCT.search(d["_text"])] or docs
        for variety in (_WHOLE, _SEMI):  # "täysmaito" is whole milk, not the semi-skimmed the account usually buys
            if variety.search(fold(q)):
                docs = [d for d in docs if variety.search(d["_text"])] or docs
        first = {d["id"]: n for n, d in enumerate(text_docs)}
        docs.sort(key=lambda d: first.get(d["id"], 999))
        other = {}
        if chosen:
            own = frozenset().union(*(self.kind_stems.get(k, frozenset()) for k in chosen))
            core = (own & S) or own   # the words the request and the chosen kind share
            for k in [c for ch in chosen for c in self.sorts.get(ch, [])] + [k for k, ks in self.kind_stems.items() if core and core <= ks]:
                if k not in chosen and k not in other:
                    ds = [d for d in self.by_kind[k] if self.matches(d, f)]
                    if ds:
                        other[k] = ds
            other = dict(sorted(other.items(), key=lambda kv: -len(kv[1])))
        choices = {k: [d for d in v if self.matches(d, f)] for k, v in choices.items()}
        return docs, f, notes, {"kinds": chosen, "how": how, "other": other, "choices": {k: v for k, v in choices.items() if v}, "text": text_docs}

    def get(self, pid: str):
        return self.by_id.get(str(pid)) or self.by_gtin.get(str(pid))

    @staticmethod
    def canon_claims(vals) -> list[str]:
        out = []
        for v in vals or []:
            k = fold(str(v)).replace("-", "_").replace(" ", "_")
            out.append(CLAIM_ALIASES.get(k, str(v).upper()))
        return out

    @staticmethod
    def canon_labels(vals) -> list[str]:
        return [LABEL_ALIASES.get(fold(str(v)).replace("-", "_").replace(" ", "_"), str(v).upper()) for v in vals or []]

    @staticmethod
    def canon_allergens(vals) -> list[str]:
        out = []
        for v in vals or []:
            s = str(v).strip()
            out.append(s.upper() if s.upper() in ALLERGENS else ALLERGEN_BY_NAME.get(fold(s), s.upper()))
        return out

    def matches(self, d, f) -> bool:
        if f.get("claims") and not set(f["claims"]) <= set(d["claims"]):
            return False
        if f.get("labels") and not set(f["labels"]) <= set(d["labels"]):
            return False
        ex = f.get("exclude_allergens")
        if ex and (allergen_hits(ex, d["contains"], d["claims"]) or (f.get("strict_allergens", True) and allergen_hits(ex, d["may_contain"], d["claims"])) or named_hits(ex, d)):
            return False
        if f.get("categories") and d["category"] not in f["categories"]:
            return False
        if f.get("origin") and d["origin"] not in f["origin"]:
            return False
        if f.get("temp") and d["temp"] not in f["temp"]:
            return False
        if f.get("brand") and fold(f["brand"]) not in fold(d.get("brand") or ""):
            return False
        return True

    def search(self, query: str, filters: dict, limit: int = 40):
        """Returns (docs ranked by text match, the filters applied, notes about what was inferred from the query)."""
        q, f, notes = self._parse(query, filters)
        return self._text_match(q, f, limit), f, notes

    def _parse(self, query: str, filters: dict):
        """(the query without the requirements written in it, the filters with those requirements added, notes)."""
        f = dict(filters or {})
        f["claims"] = self.canon_claims(f.get("claims"))
        f["labels"] = self.canon_labels(f.get("labels"))
        f["exclude_allergens"] = self.canon_allergens(f.get("exclude_allergens"))
        f["origin"] = [o.upper() for o in f.get("origin") or []]
        notes = []
        q = fold(query or "")
        for pat, claim in QUERY_CLAIMS:
            if re.search(pat, q):
                q = re.sub(pat, " ", q)
                if claim not in f["claims"]:
                    f["claims"].append(claim)
                    notes.append(f"Read '{claim}' from the query and applied it as a hard requirement.")
        for pat, label in QUERY_LABELS:
            if re.search(pat, q):
                q = re.sub(pat, " ", q)
                if label not in f["labels"]:
                    f["labels"].append(label)
                    notes.append(f"Read label '{label}' from the query and applied it as a hard requirement.")
        return q, f, notes

    def _text_match(self, q: str, f: dict, limit: int):
        toks = [t for t in re.findall(r"[a-z0-9]+", q) if len(t) > 1 and t not in {"of", "the", "and", "for", "with", "ja"}]
        scored = []
        for d in self.docs:
            if not self.matches(d, f):
                continue
            if not toks:
                scored.append((0.0, d))
                continue
            text, words = d["_text"], d["_w"]
            hit, score = 0, 0.0
            for t in toks:
                if t in words:
                    hit += 1
                    score += 2.0
                elif t in text:
                    hit += 1
                    score += 1.0
            if hit == len(toks):
                scored.append((score + 5 - len(text) / 400, d))
        scored.sort(key=lambda x: -x[0])
        return [d for _, d in scored[:limit]]
