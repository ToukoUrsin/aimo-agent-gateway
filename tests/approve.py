"""The person who asked for an order in the shop chat cannot approve it in their own browser; someone else, on their own
device, can. Run: AIMO_DATA=/tmp/aimo-approve-t uv run python -m tests.approve

Three checkouts against the live mock API: one asked by the shop assistant and refused to its asker, then approved from a
second browser with a key of its own; one asked by the shop assistant and rejected by its asker; one asked by another
agent (the visitor's own client), where the visitor is the approver and nothing is refused."""
import hashlib
import json
import re
from pathlib import Path

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature
from fastapi.testclient import TestClient

from gateway import passkey
from gateway.app import app, g
from gateway.config import PUBLIC_URL
from gateway.signing import b64u

WEB = Path(__file__).resolve().parent.parent / "web"
D, n = "2026-11-16", 0


def ok(cond, what):
    global n
    n += 1
    print(("ok   " if cond else "FAIL ") + what)
    assert cond, what


def browser():
    c = TestClient(app, base_url="https://testserver")  # the session cookie is secure when PUBLIC_URL is https
    m = c.get("/api/demo/mandate-token").json()["mandate"]
    return c, m


SAID = "1 000 litraa laktoositonta maitoa maanantaiksi"  # the asker's own words, kept on the checkout as its intent


def ask(token):
    r = asker.post("/checkout-sessions", json={"line_items": [{"item": {"id": "464201"}, "quantity": 1000}], "wholesale": {"delivery_date": D, "requirements": {"claims": ["lactose_free"]}, "intent": SAID}},
                   headers={"authorization": "Bearer " + token, "content-type": "application/json"}).json()
    return r["id"], r["status"], (r.get("continue_url") or "").split("k=")[-1]


# --- an order asked in the shop chat
asker, own_client = browser()
slug = g.store.get("session:" + asker.cookies.get("aimo_s"))["slug"]
shop = g.issue_mandate(slug, agent=f"agent:{slug}/shop-assistant", name="Valio Aimo's shop assistant", ttl=3600)
cid, status, k = ask(shop)
ok(status == "requires_escalation" and k, f"1,000 l asked in the shop chat needs a second person ({status})")
v = asker.get(f"/api/approve/{cid}", params={"k": k}).json()
ok(v.get("_own") is True, "opened in the asker's own browser: the page is told it is their own order")
fi_cat = (g.cat.get("464201") or {}).get("names", {}).get("fi")
nf = {x["id"]: x for x in v.get("_names_fi") or []}
ok(fi_cat and fi_cat != v["line_items"][0]["item"]["title"] and nf.get("464201") == {"id": "464201", "title": v["line_items"][0]["item"]["title"], "fi": fi_cat},
   f"the page's data carries the line's Finnish catalogue name next to the English title ({(nf.get('464201') or {}).get('fi')})")
ok(v["wholesale"]["intent"] == SAID, "the page's data carries the request word for word")
r = asker.post(f"/api/approve/{cid}", json={"k": k, "approve": True, "approver": "Maija Korhonen", "note": ""})
m = (r.json().get("messages") or [{}])[0]
ok(r.status_code == 403 and m.get("code") == "own_request", f"the asker's own yes is refused with 403 own_request ({r.status_code} {m.get('code')})")
v = asker.get(f"/api/approve/{cid}", params={"k": k}).json()
ok(v["status"] == "requires_escalation" and not (v["wholesale"].get("approval") or {}), "nothing was approved")
ok(any(e["kind"] == "refused" and "tried to approve it in their own browser" in e["text"] for e in v["_events"]), "the ledger says the asker tried to approve their own order and was refused")

# A second browser: no session of this account. It opens the same link, makes a key of its own and approves.
other = TestClient(app, base_url="https://testserver")
v = other.get(f"/api/approve/{cid}", params={"k": k}).json()
ok("_own" not in v and v["status"] == "requires_escalation", "opened in another browser: not the asker's own")
key = ec.generate_private_key(ec.SECP256R1())
spki = key.public_key().public_bytes(serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo)
raw = lambda data: b"".join(x.to_bytes(32, "big") for x in decode_dss_signature(key.sign(data, ec.ECDSA(hashes.SHA256()))))
b = other.post("/api/passkey/enrol/begin", json={"cid": cid, "k": k}).json()
cdj = json.dumps({"type": "aimo.key.create", "challenge": b["challenge"], "origin": PUBLIC_URL}).encode()
cred = "bk-" + hashlib.sha256(spki).hexdigest()[:32]
f = other.post("/api/passkey/enrol/finish", json={"cid": cid, "k": k, "challenge": b["challenge"], "name": "Approver's phone", "kind": "browser_key", "id": cred,
                                                   "publicKey": b64u(spki), "clientDataJSON": b64u(cdj), "signature": b64u(raw(cdj))}).json()
ok((f.get("device") or {}).get("id") == cred, "the second browser becomes the account's approver device from the link")
body = {"k": k, "approve": True, "approver": "Maija Korhonen", "note": "", "clears": [{"id": x["id"], "codes": x["codes"]} for x in g.clears_of(g._load(cid))]}
pre = other.post(f"/api/approve/{cid}/device", json=body).json()
cdj = json.dumps({"type": "aimo.key.get", "challenge": pre["challenge"], "origin": PUBLIC_URL}).encode()
r = other.post(f"/api/approve/{cid}", json={**body, "device": {"id": cred, "clientDataJSON": b64u(cdj), "signature": b64u(raw(cdj))}})
ok(r.status_code == 200 and r.json()["status"] == "ready_for_complete", f"the same yes from the second browser goes through ({r.status_code} {r.json().get('status')})")
ok(r.json()["wholesale"]["approval"]["by"] == "Maija Korhonen", "the record names the approver")

# One person alone on one device: the approval page's demo view, in the browser that asked. Its yes goes through, is kept
# as a demo approval inside the signed record, and makes no device the account's approver device.
cidd, status, kd = ask(shop)
before = len(passkey.devices(g.store, slug))
r = asker.post(f"/api/approve/{cidd}", json={"k": kd, "approve": True, "approver": "", "note": "", "demo": True})
ok(r.status_code != 200, f"a demo approval still needs a name ({r.status_code})")
r = asker.post(f"/api/approve/{cidd}", json={"k": kd, "approve": True, "approver": "Demo-hyväksyjä", "note": "", "demo": True})
ap = (r.json().get("wholesale") or {}).get("approval") or {}
ok(r.status_code == 200 and r.json()["status"] == "ready_for_complete", f"the asker's demo yes goes through ({r.status_code} {r.json().get('status')})")
ok("Demo approval" in (ap.get("demo") or "") and "browser that asked" in ap["demo"] and ap.get("by") == "Demo-hyväksyjä" and not ap.get("device"), "the approval record says it was a demo approval in the asker's browser, with no approver device")
v = asker.get(f"/api/approve/{cidd}", params={"k": kd}).json()
ok(any("Demo approval" in e["text"] for e in v["_events"]), "the ledger says it was a demo approval")
ok(len(passkey.devices(g.store, slug)) == before, "the asker's browser did not become an approver device")
cide, status, ke = ask(shop)
r = other.post(f"/api/approve/{cide}", json={"k": ke, "approve": True, "approver": "Maija Korhonen", "note": "", "demo": True, "clears": [{"id": x["id"], "codes": x["codes"]} for x in g.clears_of(g._load(cide))]})
ap = (r.json().get("wholesale") or {}).get("approval") or {}
ok(not ap.get("demo"), f"demo sent from a browser that did not ask is not a demo approval ({r.status_code})")
other.post(f"/api/approve/{cide}", json={"k": ke, "approve": False, "approver": "", "note": ""})

# The asker may still withdraw their own request.
cid2, status, k2 = ask(shop)
r = asker.post(f"/api/approve/{cid2}", json={"k": k2, "approve": False, "approver": "", "note": ""})
ok(r.status_code == 200 and r.json()["status"] == "canceled", f"the asker's own reject goes through ({r.status_code})")

# An order another agent asked for: the visitor is the approver, nothing is refused as "own".
cid3, status, k3 = ask(own_client)
v = asker.get(f"/api/approve/{cid3}", params={"k": k3}).json()
ok(status == "requires_escalation" and "_own" not in v, "an order the visitor's own client asked for is not the asker's own")
r = asker.post(f"/api/approve/{cid3}", json={"k": k3, "approve": True, "approver": "Maija Korhonen", "note": ""})
code = ((r.json().get("messages") or [{}])[0]).get("code")
ok(code != "own_request", f"its yes is not refused as own (answer: {r.status_code} {code or r.json().get('status')})")
asker.post(f"/api/approve/{cid3}", json={"k": k3, "approve": False, "approver": "", "note": ""})

# --- the page itself
page = (WEB / "approve.html").read_text()
ok('localStorage.getItem("shop.lang")' in page and "navigator.languages" in page, "the approval page reads the shop's language switch, else the browser's language")
ok('data-fold="honest"' in page and "Miten tämä toimisi Minun Aimossa" in page and "How this would work in Minun Aimo" in page, "the prototype note sits under one line")
ok("demo=1" in page and "Demo-hyväksyjä" in page and "demo: true" in page, "the demo view prefills a name and sends its yes as a demo approval")
ok('id="site"' not in page and "lib.js" in page, "no gateway header or nav on the approval page")
ok("Et voi hyväksyä omaa tilaustasi." in page and "Kopioi linkki" in page, "the asker's own browser is told plainly and offered the link to send")
ok("Näytä tarkistukset" in page and "Show the checks" in page, "the technical record sits behind one fold")
ok("pyytää hyväksyntääsi" in page and "Yhteensä (sis. alv)" in page and "Ostokset (alv 0 %)" in page and "Toimituspäivä" in page, "the first screen speaks the shop's words")
ok(not re.search(r"Tarjous|tarjous|—|TODO", page), "no Tarjous for the thing approved, no em dash, no TODO")
ok(not re.search(r"\b(te|teidän|teille|tilaatte|hyväksytte)\b", page), "sinä throughout, no te-forms")

# Finnish and plain outside one last fold; everything technical inside it
T_FI = page.split("\n  fi: {", 1)[1].split("\n  en: {", 1)[0]
ok("goods in" not in T_FI and 'goodsIn: "tavaran vastaanotto"' in T_FI and "esc(shipL(w.ship_to))" in page and 'replace(/, goods in$/, ", " + t("goodsIn"))' in page,
   "the Finnish view says tavaran vastaanotto, not goods in")
ok("nameFi(l.item.id, l.item.title)" in page and "nameFi(x.id, x.title)" in page and "C._names_fi" in page, "the Finnish view names lines by their Finnish catalogue name")
ok('tech: "Tekninen tallenne (englanniksi)"' in T_FI and 'fold("checks", t("checks"), checksHtml(c));\n  h += fold("tech", t("tech"), techHtml(c));\n  $("#main").innerHTML = h;' in page,
   "the checks fold holds only the plain list; the technical record is one last fold after it")
tech_src = page.split("function signoff()", 1)[1].split("function techHtml(c)", 1)[0] + page.split("function techHtml(c)", 1)[1].split("\n}\n", 1)[0]
chk_src = page.split("function checksHtml(c)", 1)[1].split("\n}\n", 1)[0]
ok("Try to cheat: the same link without the device" in tech_src and page.count("Try to cheat") == 1 and "${signed(c)}" in tech_src, "the cheat button sits in the technical record")
ok('t("request")' in chk_src and "esc(w.intent)" in chk_src and 'request: "Pyyntö"' in T_FI and 'madeShop: "Tilauksen kokosi verkkokaupan tilausapuri tilaajan pyynnöstä."' in T_FI,
   "the checks fold shows the request word for word and who put the cart together, without naming a person")
ok(not re.search(r"mandate|digest|ledger|gemini|claude|gpt|w\.agent|_events|rulesTable", chk_src, re.I), "no mandate, digest, ledger, agent id or model name in the checks fold")
ok('outcome: { pass: "läpi", escalate: "tarvitsee hyväksynnän", deny: "estetty" }' in T_FI and "RULEFI.title(r.title)" in chk_src and '"Spend limit per order": "Tilauskohtainen raja"' in page,
   "rules in the checks fold have Finnish titles and outcome words")
ok("Prototyyppi ei tarkista, kuka hyväksyjä on" in T_FI, "the page still says what the approval does not prove")

print(f"approve: all {n} checks pass")
