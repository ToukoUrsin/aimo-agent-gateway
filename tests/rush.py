"""Proof that short stock is split fairly (rule L1, gateway/allocation.py). Run: uv run -q python -m tests.rush

Part 1 needs nothing: the split itself, on 400 random cases with a fixed seed.
Part 2 runs the gateway in this process on its own data folder (never the main database), against the live mock
API for one price and stock question: six kitchens want the same short milk, some at the same moment.
"""
import json
import math
import os
import random
import threading
import time

os.environ.setdefault("AIMO_DATA", "/tmp/aimo-rush-test")

from agent.floor import mandate  # noqa: E402
from gateway.allocation import CARE_MIN_PCT, split  # noqa: E402
from gateway.service import Gateway, GatewayError  # noqa: E402
from gateway.signing import verify_detached  # noqa: E402
from gateway.tower import stage_print  # noqa: E402

n = 0


def ok(cond, what):
    global n
    n += 1
    print(("ok   " if cond else "FAIL ") + what)
    assert cond, what


# ------------------------------------------------------------------ 1. the split, as arithmetic
print("\n1. The split (400 random cases, seed 2026)")
rng = random.Random(2026)
never_over = fair = minimum = same = whole = capped = True
for case in range(400):
    k = rng.randint(2, 60)
    step = rng.choice([1.0, 1.0, 1.0, 6.0, 0.5])
    ids = [f"c{i:02d}" for i in range(k)]
    w = {c: rng.randint(1, 30) * step for c in ids}
    care = {c for c in ids if rng.random() < 0.3}
    fl = {c: math.ceil(CARE_MIN_PCT / 100 * w[c] / step - 1e-9) * step for c in care}
    stock = rng.randint(0, int(sum(w.values()) * 1.2 / step)) * step
    x = split(stock, w, floor=fl, step=step)
    shuffled = dict(sorted(w.items(), key=lambda kv: rng.random()))
    same &= x == split(stock, shuffled, floor=dict(fl), step=step) == split(stock, w, floor=fl, step=step)
    never_over &= sum(x.values()) <= stock + 1e-9
    whole &= all(abs(v / step - round(v / step)) < 1e-9 for v in x.values())
    capped &= all(0 <= x[c] <= w[c] + 1e-9 for c in ids)
    if stock >= sum(w.values()):
        capped &= all(x[c] == w[c] for c in ids)          # enough for everyone: everyone gets all
    else:
        never_over &= sum(x.values()) > stock - step        # short: nothing is left lying
    if sum(fl.values()) <= stock:
        minimum &= all(x[c] >= fl[c] - 1e-9 for c in care)
        for i in ids:      # nobody is ahead of another once one pack of rounding each is allowed for, unless it is its guaranteed minimum or the other has all it wants
            for j in ids:
                if (x[i] - step) / w[i] > (x[j] + step) / w[j] + 1e-9 and x[j] < w[j] and x[i] > fl.get(i, 0) + 1e-9:
                    fair = False
    else:                  # not even the minimums fit: only care kitchens get anything, shrunk together
        minimum &= all(x[c] == 0 for c in ids if c not in care)
ok(never_over, "the shares never add up to more than the stock, and when stock is short all of it is shared out")
ok(whole and capped, "every share is a whole number of packs, never more than the customer's usual order")
ok(fair, "no customer gets a larger part of its usual order than another that is still short (whole packs: within one pack each; care minimums excepted)")
ok(minimum, f"care kitchens get at least {CARE_MIN_PCT}% of their usual order whenever the stock covers those minimums; if it does not, only they get anything")
ok(same, "the same inputs give the same split, whatever order the customers come in")
x = split(698, {"hotel": 80, "bistro": 40, "hospital": 60, "school": 30}, floor={"hospital": 36, "school": 18})
ok(x == split(698, {"school": 30, "hospital": 60, "bistro": 40, "hotel": 80}, floor={"school": 18, "hospital": 36}) and sum(x.values()) == 210, f"enough stock: everyone gets its usual order {x}")
x = split(100, {"hotel": 80, "bistro": 40, "hospital": 60, "school": 30}, floor={"hospital": 36, "school": 18})
ok(x["hospital"] == 36 and x["school"] == 18 and x["hotel"] + x["bistro"] == 46 and abs(x["hotel"] - 2 * x["bistro"]) <= 2, f"100 for a usual 210: care minimums first, the rest in proportion {x}")

# ------------------------------------------------------------------ 2. the gateway
print("\n2. Six kitchens, one short milk, through the gateway")
g = Gateway()
before = stage_print(g)
D, MILK, SOY, WORLD = "2026-11-16", "463380", "464056", "rush-test"
NAMES = {"hotel": ("Usvalahti Hotel (test)", None, 0.45), "bistro": ("Kivipuro Bistro (test)", None, 0.35), "hospital": ("Havumaa Hospital Kitchen (test)", "hospital", 0.30),
         "school": ("Tuulikallio School Kitchen (test)", "school", 0.25), "caterer": ("Lumikko Catering (test)", None, 0.20), "elderly": ("Saarnirinne Care Home (test)", "elderly_care", 0.15)}
first = None
ids = list(dict.fromkeys([MILK, SOY, "464320", "463047"] + [d["id"] for d in g.cat.docs if d["category"] == g.cat.get(MILK)["category"]]))
calls = g.aimo.calls
quote = g.aimo.quote(ids, D, "rush-test-warm")  # one question to the challenge API; every test account then reads this answer
S = float(quote[MILK]["avail"]["scheduleLines"][0]["availableAmount"])
ok(0 < S < 100000, f"the mock API reports a tracked stock of {S:g} for the milk on Monday (asked it {g.aimo.calls - calls} times)")
acct, tok, pr, usual = {}, {}, {}, {}
for key, (name, seg, part) in NAMES.items():
    slug = "rush-test-" + key
    usual[key] = float(round(S * part))
    hist = {MILK: {"orders": 12, "typical_units": usual[key]}}
    if g.account(slug):
        g.reset(slug)
    else:
        g.create_account(slug, name, kind="rush", world=WORLD, segment=seg, history=hist, history_seeded=True)
    a = g.account(slug)
    a.update(world=WORLD, segment=seg)
    g.store.put("acct:" + slug, a)
    g.store.put("history:" + a["customer"], hist)
    g._register(a)
    acct[key] = a
    with g.aimo.lock:
        for i in ids:
            g.aimo.cache[(i, D, a["customer"])] = (time.time() + 3600, g.aimo.cache[(i, D, "rush-test-warm")][1])
    m = mandate(2_000_000, 8_000_000, sub="any", sub_pct=40, exclude=["AY"] if key == "hospital" else ())
    tok[key] = g.issue_mandate(slug, m, agent=f"agent:{slug}/milk-bot")
    pr[key] = g.identify(tok[key])
g.store.q("delete from holds where world=?", (WORLD,))
g.allocation.clear(WORLD)
g.allocation.configure(WORLD, 4, 2)  # unclaimed shares are released 4 s after the product goes on allocation, then every 2 s
care = {"hospital", "school", "elderly"}
floors = {k: math.ceil(CARE_MIN_PCT / 100 * usual[k]) for k in care}
want = split(S, usual, floor=floors)
calls = g.aimo.calls


def ask(key, units, extra=None, rest=None, trace="rush-test"):
    line = {"item": {"id": MILK}, "quantity": int(units), **({"wholesale": {"rest": rest}} if rest else {})}
    return g.create_checkout({"line_items": [line] + (extra or []), "context": {"intent": f"{units:g} L lactose-free milk for Monday"}, "wholesale": {"delivery_date": D}}, pr[key], trace)


def signed(c):
    return verify_detached({k: v for k, v in c.items() if k != "ap2"}, c["ap2"]["merchant_authorization"], g.key.jwk)


def place(key, c):
    return g.complete_checkout(c["id"], {"ap2": {"checkout_mandate": g.agent_proof(g.get_checkout(c["id"], pr[key]), tok[key])}}, pr[key])


def held(key=None):
    rows = g.store.q("select ref, sum(qty) from holds where product=? and world=? and (state='ordered' or (state='held' and expires>?)) group by ref", (MILK, WORLD, time.time()))
    mine = {r[0] for r in g.store.q("select id from checkouts where customer=?", (acct[key]["customer"],))} if key else None
    return sum(q for ref, q in rows if mine is None or ref in mine)


# four of the six ask for their whole usual order at the same moment
gate, got = threading.Barrier(4), {}


def burst(key):
    gate.wait()
    got[key] = ask(key, usual[key])


threads = [threading.Thread(target=burst, args=(k,)) for k in ("hotel", "bistro", "hospital", "school")]
[t.start() for t in threads]
[t.join() for t in threads]
view = g.allocation.view(WORLD)[0]
ok(view["on"] and view["mode"] == "auto" and view["stock"] == S and view["basis"] == "seeded order history", f"the milk went on allocation by itself: expected demand {view['expected']:g} L against {S:g} L in stock")
ok({r["slug"][10:]: r["share"] for r in view["customers"]} == want, f"the tower's split is exactly split(): {want}")
al = {k: got[k]["line_items"][0]["wholesale"]["allocation"] for k in got}
ok(all(got[k]["status"] == "ready_for_complete" and signed(got[k]) for k in got), "all four got a signed offer they may place alone")
ok(all(al[k]["offered"] == want[k] and al[k]["requested"] == usual[k] and got[k]["line_items"][0]["wholesale"]["units"] == want[k] for k in got), "each offer is for exactly that customer's share: " + ", ".join(f"{k} {al[k]['offered']:g} of {usual[k]:g}" for k in got))
ok(all(al[k]["offered"] >= floors[k] for k in ("hospital", "school")) and al["hospital"]["care_minimum"] and "guaranteed minimum" in al["hospital"]["reason"], f"the hospital and the school got their guaranteed minimum ({CARE_MIN_PCT}% of usual), and the offer says so")
ok(held() <= S and held() == sum(want[k] for k in got), f"held at the same moment: {held():g} L of {S:g} L, nothing promised twice")
c = got["hotel"]
rule = next(r for r in c["wholesale"]["policy"]["rules"] if r["id"] == "L1")
ok(rule["outcome"] == "pass" and rule["detail"] == al["hotel"]["reason"] and any(m["code"] == "on_allocation" for m in c["messages"]), "rule L1 is in the offer's rule list and the reason is a message to the agent: " + al["hotel"]["reason"])
ways = {w["type"] for w in al["hotel"]["ways_out"]}
ok({"substitute", "rest_later", "share_only"} <= ways and all(w["authority"] in ("agent_may_order", "needs_human_approval", "not_allowed") for w in al["hotel"]["ways_out"] if w["type"] == "substitute"),
   "the offer names the ways out: substitutes already judged under this agent's mandate, the rest later, or the share alone")
text = json.dumps(c) + json.dumps(g.lookup([MILK], D, pr["hotel"])) + json.dumps(g.search({"query": "lactose free milk", "wholesale": {"delivery_date": D}}, pr["hotel"]))
others = [NAMES[k][0] for k in NAMES if k != "hotel"] + [acct[k]["customer"] for k in NAMES if k != "hotel"]


def numbers(x):
    if isinstance(x, dict):
        for v in x.values():
            yield from numbers(v)
    elif isinstance(x, list):
        for v in x:
            yield from numbers(v)
    elif isinstance(x, (int, float)) and not isinstance(x, bool):
        yield float(x)


secret = {S, view["expected"], view["shares_total"], S - want["hotel"]}
ok(not any(o in text for o in others) and not secret & set(numbers(al["hotel"])) and f"{S:g} " not in " ".join(w["content"] for w in al["hotel"]["ways_out"]) + al["hotel"]["reason"],
   "what the hotel's agent is sent names no other customer, not the total stock and not what others asked for")
ok(al["hotel"]["your_share"] == want["hotel"] and al["hotel"]["rest"] == usual["hotel"] - want["hotel"], f"it is told its own numbers only: share {al['hotel']['your_share']:g}, not confirmed {al['hotel']['rest']:g}")
led = g.ledger(pr["bistro"]["customer"]) if hasattr(g, "ledger") else None

# asking again does not get more; a second offer cannot double the share
c2 = ask("hotel", usual["hotel"])
ok(c2["status"] == "incomplete" and c2["messages"][0]["code"] == "insufficient_stock" and c2["messages"][0]["wholesale"]["can_supply"] == 0 and "contract share" in c2["messages"][0]["content"],
   "a second request while the first offer holds the share is refused: " + c2["messages"][0]["content"][:110])
r = place("hotel", got["hotel"])
ok(r["status"] == "completed" and g.get_order(r["order"]["id"])["line_items"][0]["wholesale"]["allocation"]["offered"] == want["hotel"], f"the hotel places its share alone: order {r['order']['label']}, the signed confirmation carries the allocation")

# the hospital: share plus a soy drink for the rest. Its mandate excludes soy, so the rule refuses and no person can override.
g.cancel_checkout(got["hospital"]["id"], pr["hospital"])
soy = {"item": {"id": SOY}, "quantity": int(usual["hospital"] - want["hospital"]), "wholesale": {"substitutes_for": MILK}}
h = ask("hospital", usual["hospital"], [soy])
a1 = next((r for r in h["wholesale"]["policy"]["rules"] if r["id"] == "A1"), {})
ok(h["status"] == "incomplete" and h["wholesale"]["policy"]["decision"] == "deny" and a1.get("outcome") == "deny" and not h.get("continue_url"),
   "hospital agent adds a soy drink for the rest: refused by rule A1, and there is no approval link for a person to override it. " + str(a1.get("detail"))[:120])
try:
    r = place("hospital", h)
    blocked = r["status"] != "completed"
except GatewayError:
    blocked = True
ok(blocked and held("hospital") == 0, "the refused offer cannot be completed and holds no milk")
h2 = ask("hospital", usual["hospital"], rest="later")
ok(h2["status"] == "ready_for_complete" and h2["line_items"][0]["wholesale"]["units"] == want["hospital"] and place("hospital", h2)["status"] == "completed", "the hospital then orders its share alone and joins the waiting list for the rest")
b2 = g.update_checkout(got["bistro"]["id"], {"line_items": [{"item": {"id": MILK}, "quantity": int(want["bistro"]), "wholesale": {"rest": "later"}}], "wholesale": {"delivery_date": D}}, pr["bistro"])
ok(b2["status"] == "ready_for_complete" and place("bistro", b2)["status"] == "completed" and place("school", got["school"])["status"] == "completed", "the bistro (waiting list) and the school (share only) place their shares")

# release: the caterer and the care home never came. At the stated time their shares go to the waiting list, by contract share.
unclaimed = want["caterer"] + want["elderly"]
wait = max(0.0, g.allocation.live[(WORLD, D, MILK)]["release_at"] - time.time()) + 0.3
time.sleep(wait)
view = g.allocation.view(WORLD)[0]
log = view["releases"][0]
rows = {r["slug"][10:]: r for r in view["customers"]}
ok(view["rounds"] >= 1 and log["unclaimed"] == unclaimed and log["released"] == min(unclaimed, sum(usual[k] - want[k] for k in ("hospital", "bistro"))) and set(log["to"]) == {acct["hospital"]["customer"], acct["bistro"]["customer"]},
   f"at the stated time {log['unclaimed']:g} L unclaimed were released: {log['released']:g} L to the two customers on the waiting list, none to the ones who did not ask")
exp = split(unclaimed, {k: usual[k] for k in ("hospital", "bistro")}, cap={k: usual[k] - want[k] for k in ("hospital", "bistro")})
ok({k: rows[k]["extra"] for k in exp} == exp, f"the released litres are split by contract share again: {exp}")
b3 = ask("bistro", usual["bistro"] - want["bistro"], rest="later")
ok(b3["status"] == "ready_for_complete" and b3["line_items"][0]["wholesale"]["units"] == min(exp["bistro"], usual["bistro"] - want["bistro"]) and "released" in b3["line_items"][0]["wholesale"]["allocation"]["reason"],
   "the bistro comes back and gets its released part: " + b3["line_items"][0]["wholesale"]["allocation"]["reason"][:150])
ok(place("bistro", b3)["status"] == "completed", "and places it alone")
total = held()
ok(total <= S, f"after everything: {total:g} L of {S:g} L promised, never more than the stock")
ok(all(rows[k]["ordered"] + rows[k]["held"] <= rows[k]["share"] + rows[k]["extra"] + 1e-9 for k in rows), "no customer holds more than its share plus what was released to it")

# by hand: Valio Aimo takes it off allocation and puts another product on
off = g.allocation.switch(WORLD, D, MILK, False)
late = ask("caterer", 5)
ok(not off["on"] and late["status"] == "ready_for_complete" and "allocation" not in late["line_items"][0]["wholesale"], "Valio Aimo can take the product off allocation by hand; then it is first come, first served again")
g.cancel_checkout(late["id"], pr["caterer"])
ok(held() <= S, "still never more than the stock")
ok(g.aimo.calls - calls <= 12, f"the whole scenario asked the challenge API {g.aimo.calls - calls} more times (cache)")
chain = g.store.verify_chain()
ok(chain[0], f"the ledger's hash chain is intact ({chain[1]} entries)")
ok(stage_print(g) == before, f"the stage account is untouched: fingerprint {before['hash']} before and after")
print(f"\n{n} checks passed")
