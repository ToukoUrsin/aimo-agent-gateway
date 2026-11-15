"""A kitchen-wide allergy, however the cook words it, is read by code from the person's own words: no model decides whether it was said.
Run: AIMO_DATA=/tmp/aimo-stated uv run python -m tests.stated"""
from gateway.shopchat import stated_exclusions

SAID = {
    "nut": ["Koulun keittiö, kaikki pähkinätöntä. Tilaa 1 purkki Nutellaa 3 kg tiistaiksi.", "Koko keittiö on pähkinätön.", "Meillä on pähkinätön keittiö", "pähkinätön", "kaikki pähkinättömänä", "kaikki pähkinätöntä",
            "pähkinättömät tuotteet", "ei pähkinää", "ei pähkinöitä", "ilman pähkinöitä", "pähkinäallergia", "yhdellä lapsella on pähkinäallergia", "meillä on pähkinäallergikko", "pähkinäallergisia lapsia",
            "ei saa sisältää pähkinää", "ei saa olla yhtään pähkinää", "pähkinävapaa keittiö", "pähkinät pois", "älä laita pähkinää", "nut-free kitchen", "no nuts", "allergic to nuts"],
    "milk": ["kaikki maidotonta", "maidoton keittiö", "maitoallergia", "ei saa sisältää maitoa"],
    "gluten": ["kaikki gluteenitonta", "gluteeniton keittiö", "keliakia"],
    "egg": ["kaikki kananmunatonta"], "sesame": ["ei seesamia", "seesamiton"], "soy": ["soijaton", "ei soijaa"], "peanut": ["maapähkinätöntä", "ei maapähkinää"],
}
NOT_SAID = ["Sopiiko tämä pähkinäallergiselle?", "20 kg pähkinöitä", "5 kg cashewpähkinöitä", "pähkinäsekoitus 2 kg", "maitoa 20 litraa", "kalaa 10 kg", "kananmunia 300", "kaikki maidot ja kermat", "maito pois",
            "ei maitoa tällä kertaa", "maidoton vaihtoehto kahdelle", "Tilaa kaikki sama kuin viime viikolla", "gluteeniton pasta 5 kg", "laktoositon maito 20 l", "ota kala pois"]
n = 0
for kind, ways in SAID.items():
    for w in ways:
        n += 1
        assert kind in stated_exclusions(w), f"FAIL not read as {kind}: {w!r} -> {stated_exclusions(w)}"
for w in NOT_SAID:
    n += 1
    assert not stated_exclusions(w), f"FAIL read as an exclusion: {w!r} -> {stated_exclusions(w)}"
both = stated_exclusions("Koulun keittiö, kaikki pähkinätöntä ja maidotonta. Tilaa 2 purkkia Nutellaa 3 kg, 10 kg voita ja 5 kg cashewpähkinöitä huomiseksi.")
n += 1
assert both == ["nut", "milk"], both
print(f"ok   {sum(len(v) for v in SAID.values())} ways of stating an allergy are read, {len(NOT_SAID)} ordinary sentences are not\nALL {n} CHECKS PASS")
