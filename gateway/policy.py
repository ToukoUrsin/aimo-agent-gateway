"""Guardrails. Every decision is made here, in code, from two machine-readable documents:

* the buyer's MANDATE: what the buying company lets its agent commit to (signed by the buyer)
* the wholesaler's AGENT PROFILE: what Valio Aimo lets agents do on this account

No model is involved. Each rule returns pass / escalate / deny with the numbers behind it,
and the full list is embedded in the signed checkout, so both sides see why.
"""
import time

from .catalog import ALLERGENS, allergen_hits, allergens_unchecked, named_allergens

WEEK = 7 * 24 * 3600


def eur(cents: int) -> str:
    return f"€{cents / 100:,.2f}"


def rule(id, title, outcome, detail, path=None):
    return {"id": id, "title": title, "outcome": outcome, "detail": detail, **({"path": path} if path else {})}


def default_mandate() -> dict:
    """The constraint block of a mandate. Amounts are minor units, ex VAT."""
    return {
        "type": "open_checkout",
        "budget": {"currency": "EUR", "max_per_order": 150000, "max_per_week": 600000},
        "max_units_per_order": 5000,
        "categories": ["*"],
        "exclude_allergens": [],
        "undeclared_allergens": "ask",  # a product whose manufacturer declared nothing about allergens: "ask" a person, or "allow"
        "substitution": {"allow": "listed", "max_unit_price_increase_pct": 10},
        "new_products": "ask",
        "quantity_anomaly_factor": 3,
        "after_order": dict(AFTER_ORDER),
        "proposals": dict(PROPOSALS),
        "approver": {"name": "F&B manager", "channel": "link"},
    }


# What the agent may settle alone when Valio Aimo reports a problem with a placed order (rules D1 and P1).
AFTER_ORDER = {"accept_delay_days": 1, "accept_partial_min_pct": 70}


def after_rules(mandate: dict, after: dict) -> list[dict]:
    """Rules for a change to an order that is already placed: a short pick, a delay or a recall.
    `after` holds the numbers of the way out being judged: delay_days, delivered_pct, recall, keeps_recalled."""
    lim = {**AFTER_ORDER, **(mandate.get("after_order") or {})}
    out = []
    if after.get("recall"):
        out.append(
            rule("R1", "Recalled goods are never kept", "deny", f"Lot {after.get('lot')} is recalled. It is replaced or credited. Nobody, agent or person, can choose to keep it.")
            if after.get("keeps_recalled") else rule("R1", "Recalled goods are never kept", "pass", f"Lot {after.get('lot')} leaves the order: it is replaced or credited, not kept.")
        )
    if after.get("delay_days") is not None:
        n, ok = after["delay_days"], lim["accept_delay_days"]
        out.append(
            rule("D1", "Delay the agent may accept", "escalate", f"Delivery would be {n} day{'s' * (n != 1)} late. The mandate lets the agent accept up to {ok} day{'s' * (ok != 1)} on its own.")
            if n > ok else rule("D1", "Delay the agent may accept", "pass", f"Delivery is {n} day{'s' * (n != 1)} late, within the {ok} day{'s' * (ok != 1)} the mandate lets the agent accept on its own.")
        )
    if after.get("delivered_pct") is not None:
        pct, ok = after["delivered_pct"], lim["accept_partial_min_pct"]
        what = after.get("of", "the line")
        out.append(
            rule("P1", "Partial delivery the agent may accept", "escalate", f"Only {pct:g}% of {what} would be delivered. The mandate lets the agent accept {ok:g}% or more on its own.")
            if pct < ok else rule("P1", "Partial delivery the agent may accept", "pass", f"{pct:g}% of {what} is still delivered, and the mandate lets the agent accept {ok:g}% or more on its own.")
        )
    return out


# Valio Aimo's own agent makes proposals (gateway/seller.py). What the buyer's agent may accept alone (rules V5 and V6),
# and how many proposals Valio Aimo lets its agent make to one customer a day (rule V2, Valio Aimo's side).
PROPOSALS = {"accept_alone": ["drift", "short_dated", "fill", "shortage"], "max_extra_per_week": 25000}
PROPOSALS_PER_DAY = 5
PROPOSAL_KINDS = {"drift": "standing order drift", "short_dated": "short-dated stock", "cutoff": "before the cut-off", "shortage": "shortage ahead", "fill": "fill the case"}


def proposal_rules(mandate: dict, profile: dict, p: dict, subtotal: int, spent_week: int) -> list[dict]:
    """Rules for a proposal from Valio Aimo's agent. stage make: before it leaves (a deny means it is never sent).
    stage accept: when the buyer's agent accepts it. p: kind, extra_ex_vat, extra_week_alone, made_today, declined_before."""
    lim = {**PROPOSALS, **(mandate.get("proposals") or {})}
    name = PROPOSAL_KINDS.get(p.get("kind"), p.get("kind"))
    out = [rule("V1", "Valio Aimo's proposals switched on", "deny", "Valio Aimo has switched off its agent's proposals for this customer.")
           if profile.get("proposals") is False else rule("V1", "Valio Aimo's proposals switched on", "pass", "Valio Aimo's agent may make proposals to this customer.")]
    if p.get("stage") == "make":
        n, cap = p.get("made_today", 0), profile.get("proposals_per_day", PROPOSALS_PER_DAY)
        out.append(rule("V2", "Proposals per customer per day", "deny", f"{n} proposals already made to this customer today; Valio Aimo allows {cap}.")
                   if n >= cap else rule("V2", "Proposals per customer per day", "pass", f"Proposal {n + 1} of at most {cap} today."))
        d = p.get("declined_before")
        out.append(rule("V3", "A declined proposal is never made again", "deny", f"The customer declined this same proposal on {str(d.get('at', ''))[:10]}" + (f": {d['reason']}" if d.get("reason") else "."))
                   if d else rule("V3", "A declined proposal is never made again", "pass", "The customer has not declined this proposal before."))
    wk = (mandate.get("budget") or {}).get("max_per_week")
    if wk is not None:
        out.append(rule("V4", "A proposal may not go over the weekly cap", "deny", f"{eur(spent_week)} already ordered this week; with this proposal it would be {eur(spent_week + subtotal)}, over the buyer's {eur(wk)}.")
                   if spent_week + subtotal > wk else rule("V4", "A proposal may not go over the weekly cap", "pass", f"{eur(spent_week + subtotal)} of the buyer's {eur(wk)} a week, including this proposal."))
    alone = p.get("kind") in (lim.get("accept_alone") or [])
    out.append(rule("V5", "Proposals the agent may accept alone", "pass", f"The mandate lets the agent accept {name} proposals alone.")
               if alone else rule("V5", "Proposals the agent may accept alone", "escalate", f"The mandate does not let the agent accept {name} proposals alone, so a person decides."))
    extra, used, cap = p.get("extra_ex_vat", 0), p.get("extra_week_alone", 0), lim.get("max_extra_per_week", 0)
    if extra:
        out.append(rule("V6", "Extra spend from proposals per week", "escalate", f"This proposal adds {eur(extra)}; with {eur(used)} accepted alone this week that is {eur(used + extra)}, over the {eur(cap)} the agent may accept alone.")
                   if used + extra > cap else rule("V6", "Extra spend from proposals per week", "pass", f"Adds {eur(extra)}; {eur(used + extra)} of the {eur(cap)} a week the agent may accept alone from proposals."))
    return out


def default_profile() -> dict:
    """Valio Aimo's side: per-customer switches for agent ordering."""
    return {"enabled": True, "tier": "order", "max_order_total": 1500000, "show_contract_prices": True, "show_exact_stock": False, "keyless_cap": 50000}


def evaluate(lines, subtotal, principal, profile, history, spent_week, requirements, day_auto=0, after=None, proposal=None, cleared=None) -> list[dict]:
    """after: set when the lines change an order that is already placed (see after_rules). The money rules then
    judge the difference (new goods less what is credited), and the after-order rules are added."""
    out = []
    if not principal or not principal.get("ok"):
        err = (principal or {}).get("error", "mandate_required")
        detail = (principal or {}).get("detail", "No mandate was presented. Ordering needs a mandate signed by the buying company.")
        return [rule("M1", "Valid mandate", "deny", f"{detail} ({err})")]
    m = principal["mandate"]
    out.append(rule("M1", "Valid mandate", "pass", f"Mandate {principal['jti']} signed by {principal['buyer_name']}, valid until {time.strftime('%d %b %Y', time.gmtime(principal['exp']))}."))

    max_units = m.get("max_units_per_order")
    if max_units is not None and sum(l.get("units", 0) for l in lines) > max_units:
        out.append(rule("M2", "Mandate quantity limit", "deny", f"This request is over the mandate's {max_units:g} sales-unit per-order limit. Reduce the quantity or ask the buyer to issue a wider mandate."))
        return out

    if profile.get("global_pause") or not profile.get("enabled", True):
        out.append(rule("W1", "Agent ordering enabled by Valio Aimo", "deny", "Valio Aimo has paused agent ordering on this account."))
        return out
    out.append(rule("W1", "Agent ordering enabled by Valio Aimo", "pass", "Agent ordering is on for this account."))
    if principal.get("suspended"):
        out.append(rule("W4", "Agent in good standing", "deny", f"Valio Aimo has suspended this agent: {principal['suspended']}. It can look up products but cannot order until it is reinstated."))
        return out

    tier = profile.get("tier", "order")
    if tier == "read":
        out.append(rule("W2", "Autonomy tier", "deny", "This account's agents may look up prices and stock but not order."))
        return out
    if after is not None:
        out += after_rules(m, after)
        if not lines:  # a way out that brings no new goods: nothing else to check
            return out
        credit = after.get("credit", 0)
        goods, subtotal = subtotal, max(0, subtotal - credit)
        out.append(rule("B0", "Money is judged on the difference", "pass", f"New goods {eur(goods)} ex VAT less {eur(credit)} credited on order {after.get('order_label', '')}: the limits below are checked on {eur(subtotal)}."))
    if tier == "reorder":
        new = [l for l in lines if l["id"] not in history]
        out.append(
            rule("W2", "Autonomy tier", "escalate", f"Account is limited to re-orders; {len(new)} line(s) were never ordered before.")
            if new else rule("W2", "Autonomy tier", "pass", "Re-order of products this account has bought before.")
        )
    cap = profile.get("max_order_total")
    if cap and subtotal > cap:
        out.append(rule("W3", "Valio Aimo cap per agent order", "escalate", f"Order is {eur(subtotal)}; Valio Aimo lets agents place up to {eur(cap)} without a person."))

    if not principal.get("bound", True):
        # An assistant that signed in instead of holding a key cannot prove an order came from it, so it gets less room.
        kcap = profile.get("keyless_cap", 50000)
        out.append(
            rule("T1", "Assistant without a signing key", "escalate", f"This assistant holds no signing key. Valio Aimo lets such assistants order alone up to {eur(kcap)}; this order is {eur(subtotal)}.")
            if subtotal > kcap else rule("T1", "Assistant without a signing key", "pass", f"No signing key, and {eur(subtotal)} is within the {eur(kcap)} Valio Aimo allows such assistants alone.")
        )

    for l in lines:  # short stock: gateway/allocation.py already cut the line to this customer's share
        if l.get("allocation"):
            out.append(rule("L1", "Short stock is split by contract share", "pass", l["allocation"]["reason"], l["path"]))

    b = m.get("budget", {})
    lim = b.get("max_per_order")
    if lim is not None:
        out.append(
            rule("B1", "Spend limit per order", "escalate", f"Order is {eur(subtotal)} ex VAT, {eur(subtotal - lim)} over the agent's {eur(lim)} limit.")
            if subtotal > lim else rule("B1", "Spend limit per order", "pass", f"{eur(subtotal)} ex VAT is within the {eur(lim)} limit.")
        )
        # Split-order check: orders the agent placed alone for the same delivery day count as one order.
        if subtotal <= lim and day_auto:
            out.append(
                rule("B3", "Split-order check", "escalate", f"The agent already ordered {eur(day_auto)} for this delivery day without a person. With this order that is {eur(day_auto + subtotal)}, over the {eur(lim)} limit, so it is treated as one order.")
                if day_auto + subtotal > lim else rule("B3", "Split-order check", "pass", f"Agent-only orders for this delivery day total {eur(day_auto + subtotal)} including this one, within {eur(lim)}.")
            )
    wk = b.get("max_per_week")
    if wk is not None:
        out.append(
            rule("B2", "Spend limit per week", "escalate", f"{eur(spent_week)} already ordered in the last 7 days; this order would bring it to {eur(spent_week + subtotal)}, over {eur(wk)}.")
            if spent_week + subtotal > wk else rule("B2", "Spend limit per week", "pass", f"{eur(spent_week + subtotal)} of {eur(wk)} used this week including this order.")
        )

    cats = m.get("categories") or ["*"]
    if "*" not in cats:
        bad = [l for l in lines if l["category"] not in cats]
        out.append(
            rule("C1", "Product categories", "escalate", "Outside the categories this agent may buy: " + ", ".join(f"{l['title']} ({l['category']})" for l in bad))
            if bad else rule("C1", "Product categories", "pass", "All lines are in categories the agent may buy.")
        )

    # Allergens: the mandate's standing exclusions plus anything stated for this request.
    # A requirement can cover the whole order or one line ("the oat drink must be gluten-free").
    own = lambda l, k: set((l.get("req") or {}).get(k) or [])
    # Requirements in the checkout cover every line.  A line requirement is
    # narrower and must be described as such.  Keeping the two sets separate
    # prevents a passing rule from claiming that a per-line exclusion was
    # checked on the whole order.
    base = set(m.get("exclude_allergens") or []) | set(requirements.get("exclude_allergens") or [])
    line_exclusions = [(l, own(l, "exclude_allergens")) for l in lines if own(l, "exclude_allergens")]
    asked = sorted(base.union(*[x for _, x in line_exclusions]))
    if asked:
        # A group covers its members: excluding tree nuts excludes hazelnut. Certified gluten-free oats are not a gluten cereal.
        hit = lambda l, where: sorted(allergen_hits(base | own(l, "exclude_allergens"), l[where], l["claims"]))
        hard = [(l, hit(l, "contains")) for l in lines if hit(l, "contains")]
        soft = [(l, hit(l, "may_contain")) for l in lines if hit(l, "may_contain") and not hit(l, "contains")]
        names = lambda codes: ", ".join(ALLERGENS.get(c, c) for c in codes)
        for l, codes in hard:
            out.append(rule("A1", "Allergen exclusions", "deny", f"{l['title']} contains {names(codes)}, which is excluded.", l["path"]))
        for l, codes in soft:
            out.append(rule("A1", "Allergen exclusions", "escalate", f"{l['title']} may contain traces of {names(codes)}. A person must decide.", l["path"]))
        # No allergen declaration is not "no allergens". Such a line is never passed silently: a person clears it once
        # for that allergen, and the account remembers who did.
        silent = [(l, sorted(allergens_unchecked(base | own(l, "exclude_allergens"), l))) for l in lines]
        silent = [(l, codes) for l, codes in silent if codes]
        seen = lambda l, c: ((cleared or {}).get(l["id"]) or {}).get(c)
        open_ = [(l, [c for c in codes if not seen(l, c)]) for l, codes in silent]
        open_ = [(l, codes) for l, codes in open_ if codes] if m.get("undeclared_allergens", "ask") == "ask" else []
        if open_:
            what = names(sorted(set().union(*(codes for _, codes in open_))))
            hint = {l["id"]: named_allergens(codes, l) for l, codes in open_}  # a name that points at what is excluded goes first, and says so
            open_.sort(key=lambda x: not hint[x[0]["id"]])
            which = ", ".join(l["title"] + (f" (its name points at {names(hint[l['id']])})" if hint[l["id"]] else "") for l, _ in open_[:3]) + (f" and {len(open_) - 3} more" if len(open_) > 3 else "")
            n, rest = len(open_), len(lines) - len(silent)
            r = rule("A1", "Allergen exclusions", "escalate",
                     f"{'This product has' if len(lines) == 1 else f'{n} of {len(lines)} lines have'} no allergen declaration from the manufacturer, so the exclusion of {what} cannot be checked "
                     f"{'on it' if len(lines) == 1 else 'on ' + ('it' if n == 1 else 'them')}: {which}. A person must decide; the account then remembers who cleared {'it' if n == 1 else 'them'}."
                     + (f" The other {rest} declare their allergens and none contains or may contain what is excluded." if rest and not hard and not soft else ""))
            r["undeclared"] = [{"id": l["id"], "title": l["title"], "codes": codes, "what": names(codes), **({"named": hint[l["id"]], "named_what": names(hint[l["id"]])} if hint[l["id"]] else {})} for l, codes in open_]
            out.append(r)
        if not hard and not soft and not open_:
            parts = []
            if base:
                parts.append(f"no line contains or may contain {names(sorted(base))}, excluded on the whole order")
            for code in sorted(set().union(*(codes for _, codes in line_exclusions))):
                covered = [l["title"] for l, codes in line_exclusions if code in codes]
                parts.append(f"{names([code])} checked on {len(covered)} named line{'s' if len(covered) != 1 else ''}: {', '.join(covered)}")
            text = "; ".join(parts) + "."
            if silent:  # say plainly which lines rest on a person's earlier decision, or on the mandate, instead of on data
                n = len(silent)
                who = sorted({f"{v['by']} on {v['at'][:10]}" for l, codes in silent for c in codes for v in [seen(l, c)] if v})
                text += (f" {n} line{'s have' if n != 1 else ' has'} no allergen declaration from the manufacturer; "
                         + (f"cleared for this exclusion by {', '.join(who[:2])}." if who and m.get("undeclared_allergens", "ask") == "ask" else "this mandate lets the agent order them."))
            out.append(rule("A1", "Allergen exclusions", "pass", text[0].upper() + text[1:]))

    need = set(requirements.get("claims") or [])
    per_line = [l for l in lines if own(l, "claims") - need]
    if need or per_line:
        missing = lambda l: sorted((need | own(l, "claims")) - set(l["claims"]))
        bad = [(l, missing(l)) for l in lines if missing(l)]
        for l, miss in bad:
            near = " It is only LOW in lactose, which is not the same thing." if "FREE_FROM:LACTOSE" in miss and "LOW:LACTOSE" in l["claims"] else ""
            out.append(rule("A2", "Stated product requirements", "deny", f"{l['title']} does not carry {', '.join(miss)}.{near}", l["path"]))
        if not bad:
            said = ([f"every line carries {', '.join(sorted(need))}"] if need else []) + [f"{l['title']} carries {', '.join(sorted(own(l, 'claims')))}" for l in per_line]
            text = "; ".join(said)
            out.append(rule("A2", "Stated product requirements", "pass", f"{text[0].upper()}{text[1:]}, by the manufacturer's GS1 data."))
    labels = set(requirements.get("labels") or [])
    if labels or any(own(l, "labels") for l in lines):
        lacking = lambda l: sorted((labels | own(l, "labels")) - set(l["labels"]))
        bad = [l for l in lines if lacking(l)]
        for l in bad:
            out.append(rule("A3", "Required certificates", "deny", f"{l['title']} lacks {', '.join(lacking(l))}.", l["path"]))
        if not bad:
            out.append(rule("A3", "Required certificates", "pass", "Every line carries the certificates required of it: " + ", ".join(sorted(labels.union(*[own(l, "labels") for l in lines]))) + "."))

    sub = m.get("substitution", {})
    for l in lines:
        s = l.get("substitution")
        if not s:
            continue
        allow, cap_pct = sub.get("allow", "listed"), sub.get("max_unit_price_increase_pct", 10)
        why = []
        if allow == "none":
            why.append("the mandate allows no substitutions")
        elif allow == "listed" and not s["listed"]:
            why.append("it is not on Valio Aimo's substitute list for the requested product")
        if s["lost_claims"]:
            why.append("it drops " + ", ".join(s["lost_claims"]))
        if s["new_allergens"] and s.get("for_undeclared"):
            why.append("the product it replaces has no allergen declaration to compare with (this one contains " + ", ".join(ALLERGENS.get(c, c) for c in s["new_allergens"]) + ")")
        elif s["new_allergens"]:
            why.append("it adds allergens: " + ", ".join(ALLERGENS.get(c, c) for c in s["new_allergens"]))
        if s["price_delta_pct"] is not None and s["price_delta_pct"] > cap_pct:
            why.append(f"it costs {s['price_delta_pct']:.0f}% more per {s['per']} (limit {cap_pct}%)")
        base = f"{l['title']} replaces {s['for_title']}"
        out.append(
            rule("S1", "Substitution", "escalate", f"{base}: {'; '.join(why)}.", l["path"])
            if why else rule("S1", "Substitution", "pass", f"{base}: listed substitute, same claims, no new allergens, {s['price_delta_pct'] or 0:+.0f}% per {s['per']}.", l["path"])
        )

    if m.get("new_products", "ask") == "ask":
        new = [l for l in lines if l["id"] not in history and not l.get("substitution")]
        if new:
            # A short order names the products. A long one (a whole menu) says it once; each new line is marked on the order itself.
            what = ("Never ordered on this account before: " + ", ".join(l["title"] for l in new) + ". A person must decide.") if len(new) <= 3 else \
                f"{len(new)} of {len(lines)} products are new to this account. A person must decide about the first-time products."
            out.append(rule("N1", "First-time products", "escalate", what))
        else:
            out.append(rule("N1", "First-time products", "pass", "Every product has been ordered on this account before or is an allowed substitute."))

    factor = m.get("quantity_anomaly_factor")
    if factor:
        odd = [(l, history[l["id"]]["typical_units"]) for l in lines if l["id"] in history and l["units"] > factor * history[l["id"]]["typical_units"]]
        unknown = [l for l in lines if l["id"] not in history]
        for l, typ in odd:
            out.append(rule("Q1", "Unusual quantity", "escalate", f"{l['units']:g} {l['unit_label']} of {l['title']} is {l['units'] / typ:.1f}x this kitchen's usual {typ:g}.", l["path"]))
        # With no history there is nothing to compare with, and the rule must say so instead of claiming the quantity is usual.
        n1 = "; rule N1 asks a person about first-time products" if m.get("new_products", "ask") == "ask" else ""
        known = len(lines) - len(unknown)
        if unknown and not known:
            no_history = f"{'This product was' if len(lines) == 1 else 'These products were'} never ordered on this account before, so there is no usual quantity to compare with{n1}."
        elif unknown:
            no_history = f"Products never ordered on this account before have no usual quantity to compare with{n1}."
        if not odd:
            usual = f"No {'other ' if unknown else ''}line is more than {factor}x the usual order."
            out.append(rule("Q1", "Unusual quantity", "pass", no_history if unknown and not known else f"{no_history} {usual}" if unknown else usual))
        elif unknown:
            out.append(rule("Q1", "Unusual quantity", "pass", no_history))
    if proposal is not None:  # the lines come from a proposal by Valio Aimo's agent (gateway/seller.py)
        out += proposal_rules(m, profile, proposal, subtotal, spent_week)
    return out


def decision(rules) -> str:
    o = {r["outcome"] for r in rules}
    return "deny" if "deny" in o else "escalate" if "escalate" in o else "auto"
