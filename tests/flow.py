"""End-to-end smoke test of the gateway service against the live mock API. Run: uv run python -m tests.flow"""
import json
from gateway import policy
from gateway.service import Gateway
from gateway.signing import verify_detached

g = Gateway(); g.reset()
tok = g.issue_mandate(); p = g.identify(tok); print('principal ok', p['ok'], p['jti'])
D = '2026-11-16'
pl = g.plan({'query': 'lactose-free milk', 'need': {'quantity': 1000, 'unit': 'L'}, 'delivery_date': D}, p, 't1')
for o in pl['options']: print('OPT', o['label'], o['covers_pct'], o['total_ex_vat'], o['authority'], [(l['item']['title'], l['units']) for l in o['line_items']])
print('not offered', pl['not_offered']); print([m['content'] for m in pl['messages']])
print('plan proof ok', verify_detached({k: v for k, v in pl.items() if k != 'proof'}, pl['proof']['jws'], g.key.jwk))
pl2 = g.plan({'query': 'lactose-free milk', 'need': {'quantity': 1000, 'unit': 'L'}, 'delivery_date': D, 'preferred_product': '463380'}, p, 't1')
for o in pl2['options']: print('OPT2', o['label'], o['covers_pct'], o['total_ex_vat'], o['authority'], [(l['item']['title'], l['units'], l.get('wholesale')) for l in o['line_items']])
c = g.create_checkout({'line_items': [{'item': {'id': '464201'}, 'quantity': 1000}], 'wholesale': {'delivery_date': D, 'requirements': {'claims': ['lactose_free']}, 'purchase_order': 'PO-7781'}, 'context': {'intent': '1,000 L lactose-free milk by Monday'}}, p, 't1')
print('status', c['status'], c.get('continue_url'), c['totals'], [m['content'] for m in c['messages']])
print('sig ok', verify_detached({k: v for k, v in c.items() if k != 'ap2'}, c['ap2']['merchant_authorization'], g.key.jwk))
r = g.complete_checkout(c['id'], {}, p); print('complete before approval ->', r['status'], r['messages'][-1]['content'])
a = g.decide(c['id'], c['continue_url'].split('k=')[1], True, 'Maija (F&B manager)', 'ok for the conference week'); print('after approve', a['status'])
r = g.complete_checkout(c['id'], {}, p); print('complete w/o proof ->', r['status'], r['messages'][-1]['content'])
cur = g.get_checkout(c['id'], p)
r = g.complete_checkout(c['id'], {'ap2': {'checkout_mandate': g.agent_proof(cur, tok)}}, p); print('complete ->', r['status'], r.get('order'))
o = g.get_order(r['order']['id']); print(o['label'], o['totals'], o['messages'])
print('order proof ok', verify_detached({k: v for k, v in o.items() if k != 'proof'}, o['proof']['jws'], g.key.jwk))
c2 = g.create_checkout({'line_items': [{'item': {'id': '464201'}, 'quantity': 400}], 'wholesale': {'delivery_date': D, 'requirements': {'claims': ['lactose_free']}}}, p, 't2')
print('c2', c2['status'], [(x['id'], x['outcome']) for x in c2['wholesale']['policy']['rules']])
r = g.complete_checkout(c2['id'], {'ap2': {'checkout_mandate': g.agent_proof(c2, tok)}}, p); print('c2 complete ->', r['status'], r.get('order', {}).get('label'))
c3 = g.create_checkout({'line_items': [{'item': {'id': '463380'}, 'quantity': 1000}, {'item': {'id': '464768'}, 'quantity': 5}, {'item': {'id': '999'}, 'quantity': 1}], 'wholesale': {'delivery_date': D}}, p, 't3')
print('c3', c3['status'])
for m in c3['messages']: print('  ', m['code'], m['content'], [(x['id'], x['tier'], x['title'][:30]) for x in (m.get('wholesale') or {}).get('alternatives', [])])
opt = [o for o in pl2['options'] if o['label'] == 'split_with_substitute']
if opt:
    c4 = g.create_checkout({'line_items': opt[0]['line_items'], 'wholesale': {'delivery_date': D, 'requirements': {'claims': ['lactose_free']}}}, p, 't4')
    print('c4', c4['status']); [print('  ', x['id'], x['outcome'], x['detail']) for x in c4['wholesale']['policy']['rules']]
c5 = g.create_checkout({'line_items': [{'item': {'id': '464201'}, 'quantity': 10}], 'wholesale': {'delivery_date': D}}, None, 't5'); print('c5', c5['status'], [m['content'] for m in c5['messages']])
s = g.search({'query': 'lactose-free milk', 'wholesale': {'delivery_date': D}, 'pagination': {'limit': 2}}, None); print('anon', json.dumps(s['products'][0]['wholesale'])[:300])
au = g.audit(o['id']); print('audit', json.dumps(au['ledger']))
for e in au['events']: print('  ', e['ts'][11:], e['actor'][:28].ljust(28), e['kind'].ljust(9), e['text'][:260])
print('aimo api calls', g.aimo.calls)

# With no history, Q1 cannot compare a quantity with a usual order. N1 still
# asks about the first-time product.
unknown_line = {"id": "464201", "title": "Valio semi skimmed milk", "category": "10400", "claims": [], "contains": [], "may_contain": [], "labels": [],
                "units": 3000, "unit_label": "L", "path": "$.line_items[0]", "amount": 100, "tax": 0, "tax_rate": 0}
qr = policy.evaluate([unknown_line], 100, p, policy.default_profile(), {}, 0, {})
print('unknown history', [(x['id'], x['outcome'], x['detail']) for x in qr if x['id'] in ('N1', 'Q1')])
assert next(x for x in qr if x['id'] == 'N1')['outcome'] == 'escalate'
assert 'never ordered on this account before' in next(x for x in qr if x['id'] == 'Q1')['detail']
# No allergen declaration is not "no allergens": salmon with no declaration does not pass a fish exclusion on data.
cat = g.cat
silent_line = lambda pid: {**unknown_line, **{k: cat.get(pid)[k] for k in ("id", "title", "category", "claims", "contains", "may_contain", "free_from")}, "units": 1}
salmon, napkin = silent_line("461814"), silent_line(next(d["id"] for d in cat.docs if str(d["category"]).startswith("63") and not d["contains"]))
a1 = lambda lines, **kw: [x for x in policy.evaluate(lines, 100, p, policy.default_profile(), {}, 0, {"exclude_allergens": ["AF"]}, **kw) if x["id"] == "A1"]
r = a1([salmon])
print('undeclared', [(x['outcome'], x['detail']) for x in r])
assert [x["outcome"] for x in r] == ["escalate"] and "no allergen declaration" in r[0]["detail"] and r[0]["undeclared"][0]["codes"] == ["AF"]
assert [x["outcome"] for x in a1([napkin])] == ["pass"], "things that are not food are not asked about"
r = a1([salmon], cleared={"461814": {"AF": {"by": "Maija", "at": "2026-11-14T08:00:00Z"}}})
assert [x["outcome"] for x in r] == ["pass"] and "cleared for this exclusion by Maija on 2026-11-14" in r[0]["detail"], r
many_lines = [{**unknown_line, "id": str(i), "title": f"Product {i}", "units": 1} for i in range(41)]
many_history = {str(i): {"typical_units": 10} for i in range(5)}
nr = policy.evaluate(many_lines, 100, p, policy.default_profile(), many_history, 0, {})
assert next(x for x in nr if x['id'] == 'N1')['detail'].startswith('36 of 41 products are new')
