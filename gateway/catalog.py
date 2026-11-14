"""Static product index built once from the challenge product file (Synkka / GS1 data).

Price and stock are never stored here: they are customer specific and change, so they
are fetched live for the handful of products a request narrows down to.
"""
import csv
import json
import re
import unicodedata

from .config import DATA, PORTAL

INDEX = DATA / "catalog.json"

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


# Words in a product name that point at an allergen. Used only on products with no declaration, and only to put them in
# front of the person who decides: a name is a hint, not data ("almond potato" is a potato).
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


class Catalog:
    def __init__(self):
        if not INDEX.exists():
            build()
        self.docs = json.load(open(INDEX, encoding="utf-8"))
        self.by_id = {d["id"]: d for d in self.docs}
        self.by_gtin = {d["gtin"]: d for d in self.docs if d.get("gtin")}

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
        if ex and (allergen_hits(ex, d["contains"], d["claims"]) or (f.get("strict_allergens", True) and allergen_hits(ex, d["may_contain"], d["claims"]))):
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
        """Returns (docs ranked by text match, notes about what was inferred from the query)."""
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
        toks = [t for t in re.findall(r"[a-z0-9]+", q) if len(t) > 1 and t not in {"of", "the", "and", "for", "with", "ja"}]
        scored = []
        for d in self.docs:
            if not self.matches(d, f):
                continue
            if not toks:
                scored.append((0.0, d))
                continue
            text = d["_text"]
            words = set(re.findall(r"[a-z0-9]+", text))
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
        return [d for _, d in scored[:limit]], f, notes
