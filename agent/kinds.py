"""What each product is, in a buyer's words. Run once over the catalogue: uv run -q python -m agent.kinds

The challenge data has 349 category numbers and no names, and one category holds milk, coffee milk and sour milk side by side.
A request for "milk" must get milk. So every product gets a kind: the one to three plain words a kitchen buyer would type to
get it and its direct equivalents ("milk", "coffee milk", "egg", "minced beef"). A model writes the kinds here, once, from the
products' own names, then reads every kind again as a whole and moves what does not belong (--check: frozen onion cubes out of
"onion"), then names the broader word of each kind (--broader: spaghetti is pasta). The gateway only reads the finished file (gateway/kinds.json) and matches requests against it in code.
Valio Aimo's own product hierarchy would replace this file.
"""
import argparse
import asyncio
import json
import re
import time
from collections import defaultdict

import anthropic

from agent.llm import PROVIDERS, _key
from gateway.catalog import Catalog, fold
from gateway.config import ROOT

OUT = ROOT / "gateway" / "kinds.json"
MODEL = "claude-opus-5-5"
CHUNK = 110
SYSTEM = """You name what food-service products are, for a wholesaler's search. For every product line you get (id | English name | Finnish name), answer one line: id<TAB>kind.

The kind is what a professional kitchen buyer would type to get this product and the products they would accept in its place:
- one to three lowercase English words, singular, the plainest common name ("milk", "egg", "minced beef", "potato", "rye bread", "salmon fillet", "dish soap").
- no brand, no size, no pack, no count.
- leave out what a buyer would give as a requirement instead: fat level, salt level, organic, lactose-free, gluten-free, origin, fresh/UHT, free-range, grade or size class. Semi-skimmed lactose-free UHT milk is "milk"; salted butter is "butter"; free-range eggs size M are "egg".
- keep what changes how it is used: "coffee milk", "sour milk", "milk powder", "whipping cream", "cooking cream", "sour cream", "egg white", "boiled egg", "smoked salmon", "minced beef and pork", "oat drink", "barista oat drink", "chocolate milk".
- flavoured or ready-made things are named for what they are: "strawberry yoghurt" is "yoghurt"; "meatball" is "meatball"; a ready meal is named by the dish ("lasagne").
- lactose-free milk sold as "milk drink" (maitojuoma) is "milk".
- use the same kind for every product that is the same thing, also across your answers: prefer the shortest common name.
- non-food gets a kind too ("napkin", "dish soap", "disposable glove").

Answer with exactly one line per product, nothing else."""


BROADER = """You sort a wholesaler's product kinds for its search. For every kind you get (kind | number of products), answer one line: kind<TAB>broader.

The broader word is the one plain word (two at most) a professional kitchen buyer types when they have not said which sort they want, and this kind is one of the sorts they could mean:
- spaghetti, penne, fusilli -> pasta. basmati rice, parboiled rice, risotto rice -> rice. wheat flour, rye flour -> flour. whipping cream, cooking cream -> cream.
- chicken fillet, chicken thigh, chicken strip -> chicken. salmon fillet -> salmon. beef striploin, minced beef -> beef. cheese slice, grated cheese, blue cheese, edam -> cheese. rye bread, toast bread -> bread. red onion -> onion. cherry tomato -> tomato.
- only when the kind really is a sort of the broader thing, usable in its place in a kitchen. coconut milk, coffee milk and milk chocolate are not sorts of milk; peanut butter is not a sort of butter; ice cream is not a sort of cream; chicken bouillon, chicken soup and chicken salad are not sorts of chicken; a pasta plate is not pasta. For those answer -.
- ready meals, sauces, soups, snacks, sweets, equipment and non-food usually have no broader word: answer -.
- a kind that is already the plain word (milk, egg, butter, potato) gets -.

Answer with exactly one line per kind, nothing else."""
CHECK = """You check a wholesaler's product kinds. A first pass gave every product a kind: the plain words a professional kitchen buyer types to get it. You get blocks, each one kind with the products that carry it (id | English name | Finnish name), and the neighbouring kinds that already exist.

A kind is right when a buyer who asks for it could take any product of the block in place of another. Find the products that do not belong:
- another form the buyer would not accept for the plain word: fresh onions and frozen onion cubes, tomatoes and diced or canned tomatoes, a whole fish and its fillet, raw and cooked or breaded, a powder, a ready-made dish, a mix. Whole, peeled and washed are the same kind.
- something else entirely that only shares a word.
For each of those answer one line: id<TAB>better kind. Use a neighbouring kind when one fits, otherwise the plainest name of one to three lowercase English words, singular, and the same name for every product that is the same thing ("onion cube", "diced tomato", "frozen berry mix").

Leave alone what a buyer gives as a requirement: brand, size, pack, fat or salt level, organic, lactose-free, gluten-free, origin, grade, and chilled or frozen where both are bought for the same use (meat, fish, bread, berries). Semi-skimmed lactose-free UHT milk stays "milk".
Do not rename a kind, and do not split a block that is fine: most blocks need no line at all.

Answer only the lines for products that move. If nothing moves, answer: none"""
KEEP_S = ("us", "is", "ss", "fries")   # hummus, couscous, asparagus, french fries


def norm(kind: str) -> str:
    words = re.findall(r"[a-z]+", fold(kind))[:4]   # fold: "purée" stays one word
    if words and len(words[-1]) > 3 and words[-1].endswith("s") and not words[-1].endswith(KEEP_S):
        words[-1] = words[-1][:-1]
    return " ".join(words)


async def broader(client, sem, chunk, out, stats):
    lines = "\n".join(f"{k} | {n}" for k, n in chunk)
    for attempt in range(4):
        async with sem:
            try:
                r = await client.messages.create(model=MODEL, max_tokens=8000, system=BROADER, messages=[{"role": "user", "content": lines}])
            except Exception:
                stats["retries"] += 1
                await asyncio.sleep(4 + 6 * attempt)
                continue
        names = {k for k, _ in chunk}
        for line in "".join(b.text for b in r.content if b.type == "text").splitlines():
            m = re.match(r"\s*([a-z ]+?)\s*[\t|:]\s*([a-z -]+?)\s*$", line.lower())
            if m and m.group(1) in names:
                out[m.group(1)] = norm(m.group(2))   # "-" becomes ""
        return


async def check(client, sem, blocks, by_id, moved, stats):
    text = "\n\n".join(f"## {k}\nneighbouring kinds: {', '.join(near) or '-'}\n" + "\n".join(f"{d['id']} | {d['names'].get('en') or d['title']} | {d['names'].get('fi') or ''}" for d in docs)
                       for k, near, docs in blocks)
    mine = {d["id"]: k for k, _, docs in blocks for d in docs}
    for attempt in range(4):
        async with sem:
            try:
                r = await client.messages.create(model=MODEL, max_tokens=6000, system=CHECK, messages=[{"role": "user", "content": text}])
            except Exception:
                stats["retries"] += 1
                await asyncio.sleep(4 + 6 * attempt)
                continue
        for line in "".join(b.text for b in r.content if b.type == "text").splitlines():
            m = re.match(r"\s*(\d+)\s*[\t|:]\s*(.+?)\s*$", line)
            if m and m.group(1) in mine and norm(m.group(2)) and norm(m.group(2)) != mine[m.group(1)]:
                moved[m.group(1)] = norm(m.group(2))
        return
    stats["gave_up"] += len(mine)


async def label(client, sem, chunk, done, stats):
    lines = "\n".join(f"{d['id']} | {d['names'].get('en') or d['title']} | {d['names'].get('fi') or ''}" for d in chunk)
    for attempt in range(5):
        async with sem:
            try:
                r = await client.messages.create(model=MODEL, max_tokens=6000, system=SYSTEM, messages=[{"role": "user", "content": lines}])
            except Exception as e:
                stats["retries"] += 1
                await asyncio.sleep(4 + 6 * attempt)
                continue
        got = {}
        for line in "".join(b.text for b in r.content if b.type == "text").splitlines():
            m = re.match(r"\s*(\d+)\s*[\t|:]\s*(.+?)\s*$", line)
            if m and norm(m.group(2)):
                got[m.group(1)] = norm(m.group(2))
        ids = {d["id"] for d in chunk}
        done.update({i: k for i, k in got.items() if i in ids})
        chunk = [d for d in chunk if d["id"] not in got]
        if not chunk:
            return
        lines = "\n".join(f"{d['id']} | {d['names'].get('en') or d['title']} | {d['names'].get('fi') or ''}" for d in chunk)
    stats["gave_up"] += len(chunk)


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--parallel", type=int, default=5)
    ap.add_argument("--limit", type=int, default=0, help="only this many products (a trial)")
    ap.add_argument("--check", action="store_true", help="third pass: every kind with three or more products is read again as a whole; products that do not belong move")
    ap.add_argument("--broader", action="store_true", help="second pass: the broader word for each kind (spaghetti -> pasta)")
    ap.add_argument("--dry", action="store_true", help="with --check: write the moves to /tmp/kinds-moved.json and leave kinds.json alone")
    a = ap.parse_args()
    docs = Catalog().docs
    have = json.load(open(OUT)) if OUT.exists() else {}
    done = have.get("kinds", {})
    up = have.get("broader", {})
    client = anthropic.AsyncAnthropic(base_url=PROVIDERS["anthropic"]["base"], api_key=_key("anthropic"), max_retries=0, timeout=180)
    sem, stats, t0 = asyncio.Semaphore(a.parallel), {"retries": 0, "gave_up": 0}, time.time()
    by_cat = defaultdict(list)
    for d in docs:
        if d["id"] not in done:
            by_cat[d["category"]].append(d)
    chunks = [v[i:i + CHUNK] for _, v in sorted(by_cat.items()) for i in range(0, len(v), CHUNK)]
    if a.limit:
        chunks = chunks[: max(1, a.limit // CHUNK)]
    def save():
        json.dump({"what": "kinds: product id -> what the product is, in a buyer's words. broader: kind -> the plain word it is a sort of (spaghetti -> pasta). "
                           "Written by a model from the product names (agent/kinds.py); read by gateway/catalog.py.",
                   "model": MODEL, "products": len(done), "kinds": dict(sorted(done.items())), "broader": dict(sorted(up.items()))}, open(OUT, "w"), ensure_ascii=False, separators=(",", ":"))

    if a.check:
        from collections import Counter
        by_kind = defaultdict(list)
        for d in docs:
            if done.get(d["id"]):
                by_kind[done[d["id"]]].append(d)
        count = Counter(done.values())
        word = defaultdict(set)
        for k in count:
            for w in k.split():
                word[w].add(k)
        near = lambda k: [f"{n} ({count[n]})" for n in sorted({n for w in k.split() for n in word[w]} - {k}, key=lambda n: -count[n])[:14]]
        parts, cur, size = [], [], 0
        for k, ds in sorted(by_kind.items()):
            if len(ds) < 3:
                continue
            for i in range(0, len(ds), 150):   # a very large kind is read in pieces
                piece = ds[i:i + 150]
                if cur and size + len(piece) > 130:
                    parts.append(cur)
                    cur, size = [], 0
                cur.append((k, near(k), piece))
                size += len(piece)
        if cur:
            parts.append(cur)
        if a.limit:
            parts = parts[: max(1, a.limit // 130)]
        moved = {}
        tasks = [asyncio.create_task(check(client, sem, p, {}, moved, stats)) for p in parts]
        for n, t in enumerate(asyncio.as_completed(tasks), 1):
            await t
            if n % 20 == 0 or n == len(tasks):
                print(f"{n}/{len(tasks)} requests, {len(moved)} products moved, {stats}, {time.time() - t0:.0f} s", flush=True)
        json.dump(moved, open("/tmp/kinds-moved.json", "w"), ensure_ascii=False, indent=0)
        if not a.dry:
            done.update(moved)
            save()
        print(f"{len(moved)} of {sum(len(d) for p in parts for _, _, d in p)} products moved to another kind; {len(set(done.values()))} kinds now", flush=True)
        return

    if a.broader:
        from collections import Counter
        count = Counter(done.values())
        todo = sorted((k, n) for k, n in count.items() if k not in up)
        parts = [todo[i:i + 250] for i in range(0, len(todo), 250)]
        tasks = [asyncio.create_task(broader(client, sem, c, up, stats)) for c in parts]
        for n, t in enumerate(asyncio.as_completed(tasks), 1):
            await t
        save()
        print(f"{len(up)} of {len(count)} kinds sorted, {sum(1 for v in up.values() if v)} have a broader word, {len(set(v for v in up.values() if v))} broader words, {stats}, {time.time() - t0:.0f} s", flush=True)
        return

    tasks = [asyncio.create_task(label(client, sem, c, done, stats)) for c in chunks]
    for n, t in enumerate(asyncio.as_completed(tasks), 1):
        await t
        if n % 20 == 0 or n == len(tasks):
            save()
            print(f"{n}/{len(tasks)} chunks, {len(done)} products named, {len(set(done.values()))} kinds, {stats}, {time.time() - t0:.0f} s", flush=True)
    save()


if __name__ == "__main__":
    asyncio.run(main())
