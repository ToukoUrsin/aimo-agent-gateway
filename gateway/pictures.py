"""Product pictures by GTIN from Open Food Facts, the open product database (openfoodfacts.org; its pictures are CC BY-SA).

The challenge data has no pictures, and this is not Valio Aimo's own picture feed. A product shows a picture only when
somebody has photographed that GTIN for Open Food Facts; every other product shows a plain placeholder. Each GTIN is asked
for once and the answer is kept in the data folder, a miss included, so a card never waits on it twice.

    uv run python -m gateway.pictures 464201 464306 ...      fetch the pictures of these products now (ids or GTINs)
"""
from __future__ import annotations

import base64
import json
import threading
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor, wait

from .config import DATA

DIR = DATA / "pictures"
API = "https://world.openfoodfacts.org/api/v2/product/{}.json?fields=code,image_front_small_url,image_front_thumb_url"
AGENT = "aimo-agent-gateway-prototype/0.2"  # Open Food Facts asks every client to name itself
SOURCE = {"name": "Open Food Facts", "url": "https://world.openfoodfacts.org", "licence": "CC BY-SA 3.0",
          "note": "Public pictures matched by GTIN from Open Food Facts. Not Valio Aimo's own picture feed."}
MISS_TTL = 3 * 86400
MAX_BYTES = 40_000

_pool = ThreadPoolExecutor(4, thread_name_prefix="picture")
_lock = threading.Lock()
_busy: dict = {}
_miss: dict = {}
_read = 0.0


def _misses() -> dict:
    """GTINs Open Food Facts has no picture for, and when it said so. Read again when another process (the command below) has added to the file."""
    global _read
    try:
        at = (DIR / "misses.json").stat().st_mtime
        if at != _read:
            _miss.update(json.loads((DIR / "misses.json").read_text()))
            _read = at
    except Exception:
        pass
    return _miss


def _get(url: str, timeout: float) -> bytes:
    return urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": AGENT}), timeout=timeout).read()


def _fetch(gtin: str) -> bool:
    """Ask Open Food Facts for this GTIN's front picture and keep it. True when a picture was stored."""
    found = None
    try:
        try:
            p = (json.loads(_get(API.format(gtin), 8)) or {}).get("product") or {}
        except urllib.error.HTTPError as e:
            if e.code != 404:
                raise
            p = {}
        url = p.get("image_front_small_url") or p.get("image_front_thumb_url")
        if url and url.startswith("https://images.openfoodfacts.org/"):
            data = _get(url, 8)
            if len(data) > MAX_BYTES and p.get("image_front_thumb_url"):
                data = _get(p["image_front_thumb_url"], 8)
            if data[:2] == b"\xff\xd8" and len(data) <= MAX_BYTES:
                DIR.mkdir(parents=True, exist_ok=True)
                (DIR / f"{gtin}.jpg").write_bytes(data)
                found = True
        if not found:
            found = False
    except Exception:
        pass  # the network failed: no answer either way, ask again next time
    with _lock:
        _busy.pop(gtin, None)
        if found is False:
            _misses()[gtin] = int(time.time())
            try:
                DIR.mkdir(parents=True, exist_ok=True)
                (DIR / "misses.json").write_text(json.dumps(_misses()))
            except Exception:
                pass
    return bool(found)


def _have(gtin: str) -> str | None:
    f = DIR / f"{gtin}.jpg"
    return "data:image/jpeg;base64," + base64.b64encode(f.read_bytes()).decode() if f.exists() else None


def pictures(gtins: list[str], budget: float = 1.2, most: int = 12) -> dict:
    """{gtin: data URI} for the GTINs that have a picture. GTINs never asked before are looked up now, for at most `budget` seconds together;
    what is not back by then arrives on a later call."""
    out, asked = {}, []
    for gtin in dict.fromkeys(str(x) for x in gtins if x and str(x).isdigit() and 8 <= len(str(x)) <= 14):
        if len(out) + len(asked) >= most:
            break
        pic = _have(gtin)
        if pic:
            out[gtin] = pic
            continue
        with _lock:
            if time.time() - _misses().get(gtin, 0) < MISS_TTL:
                continue
            if gtin not in _busy:
                _busy[gtin] = _pool.submit(_fetch, gtin)
            asked.append((gtin, _busy[gtin]))
    if asked and budget > 0:
        wait([f for _, f in asked], timeout=budget)
        for gtin, _ in asked:
            pic = _have(gtin)
            if pic:
                out[gtin] = pic
    return out


if __name__ == "__main__":
    import sys

    from .catalog import Catalog

    cat, n = Catalog(), 0
    for arg in sys.argv[1:]:
        d = cat.get(arg)
        gtin = (d or {}).get("gtin") or arg
        if (DIR / f"{gtin}.jpg").exists():
            got = True
        else:
            got = _fetch(str(gtin))
            time.sleep(0.7)  # Open Food Facts allows 100 product reads a minute
        n += bool(got)
        print(("picture " if got else "none    ") + str(gtin), (d or {}).get("title", ""))
    print(f"{n} of {len(sys.argv) - 1} have a picture")
