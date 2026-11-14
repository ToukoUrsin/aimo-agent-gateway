"""The proof page: what the last eval run measured (agent/evals.py). Serves the file that run wrote; scores nothing itself."""
import json
import re

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse

from .catalog import ALLERGENS, NONFOOD_CATS, declares_allergens, named_allergens
from .config import PUBLIC_URL, ROOT

EXAMPLES = ["SALMON FILLET C-CUT DEBONED A10KG NORWAY", "SMOKED SHRIMP WHOLE 2KG", "RB 1KG ALMOND BLANCED 27/30", "Katjang Peanuts 1 kg", "FALLOT 5KG DIJON MUSTARD",
            "RAPSONA 5KG REAL MAYONNAISE 80%", "BIRKKALAN 800G ORGANIC SPELTFLOUR", "OH AIK GUAN 650ML SESAME OIL ROASTED", "RACLETTE A.5KG CHEESE", "DEK SOM BOON 2L LIGHT SOY SAUCE"]
RERUN = "uv run -q python -m agent.evals --base http://127.0.0.1:8642 --models claude-opus-5-5,claude-haiku-5-5,gpt-6-sol,gemini-3.8-flash,gemma-4-31b-it --concurrency 3"


def build(g, ctx, page) -> APIRouter:
    r = APIRouter(include_in_schema=False)
    evals = ROOT / "evals"

    def latest():
        try:
            return json.loads((evals / "latest.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None

    silent = {}

    def undeclared():
        """Counted from the challenge's product data when first asked: food products whose manufacturer declared nothing about allergens."""
        if not silent:
            food = [d for d in g.cat.docs if str(d.get("category") or "")[:2] not in NONFOOD_CATS]
            quiet = [d for d in food if not declares_allergens(d)]
            named = [(d, named_allergens(list(ALLERGENS), d)) for d in quiet]
            named = [(d, codes) for d, codes in named if codes]
            # Examples a reader can look up: products whose own name says what is inside, yet whose record declares nothing.
            by_title = {d["title"]: d for d, _ in named}
            ex = [by_title[t] for t in EXAMPLES if t in by_title]
            silent.update({"food_products": len(food), "undeclared": len(quiet), "pct": round(100 * len(quiet) / max(1, len(food)), 1), "name_points_at_an_allergen": len(named),
                           "examples": [{"id": d["id"], "title": d["title"], "points_at": ALLERGENS.get(named_allergens(list(ALLERGENS), d)[0])} for d in ex]})
        return silent

    def more():
        """Evidence beside the model test, each read from the file its own run wrote: the test suites, the menu test, the last Monday rush."""
        out = {}
        for key, name in (("tests", "tests-latest.json"), ("menu_test", "menu-latest.json"), ("rush", "rush-latest.json")):
            try:
                d = json.loads((evals / name).read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            # The rush file holds every agent's story; the proof page needs its totals, and links to /api/rush/latest for the rest.
            out[key] = {k: v for k, v in d.items() if not isinstance(v, (list, dict)) or k in ("stock", "outcomes")} if key == "rush" else d
        out["undeclared"] = undeclared()
        return out

    @r.get("/proof")
    def proof_page():
        return page("proof.html")

    @r.get("/api/proof")
    def proof():
        s = latest()
        if not s:
            return {"ready": False, "rerun": RERUN, **more()}
        # Order pages exist only on the instance the eval ran against. Say which orders this instance can show.
        ids = [o["id"] for x in s.get("results", []) for o in x.get("orders", [])]
        return {**s, "ready": True, "this_url": PUBLIC_URL, "orders_here": [i for i in ids if g.store.order(i)], **more()}

    @r.get("/api/proof/runs.jsonl")
    def proof_runs(id: str = ""):
        """Every run of one eval, one JSON line each, as the scorer wrote it. Without id: the last eval."""
        rid = id or (latest() or {}).get("id") or ""
        f = evals / "runs" / f"{rid}.jsonl"
        if not re.fullmatch(r"\d{8}T\d{6}Z", rid) or not f.is_file():
            raise HTTPException(404, "No such eval run.")
        return FileResponse(f, media_type="application/x-ndjson", filename=f.name)

    @r.get("/api/proof/menu-runs.jsonl")
    def menu_runs():
        """Every menu of the last menu test, one JSON line each."""
        try:
            rid = json.loads((evals / "menu-latest.json").read_text(encoding="utf-8")).get("id") or ""
        except (OSError, ValueError):
            rid = ""
        f = evals / "menu-runs" / f"{rid}.jsonl"
        if not re.fullmatch(r"\d{8}T\d{6}Z", rid) or not f.is_file():
            raise HTTPException(404, "No menu test has been run.")
        return FileResponse(f, media_type="application/x-ndjson", filename="menu-" + f.name)

    return r
