// An approver's own device signs the decision: enrol, approve, verify, and the ways around it that must fail.
// Two paths, side by side: a passkey (a software one inside a test browser, Chromium's virtual authenticator, so no
// real phone is needed) and a browser key (a browser with no authenticator makes its own ES256 key with WebCrypto).
// Run against a scratch server opened as localhost (passkeys need https or localhost):
//   OPERATOR_KEY=test AIMO_DATA=/tmp/aimo-pk PORT=8673 PUBLIC_URL=http://localhost:8673 ./run.sh
//   BASE=http://localhost:8673 node tests/passkey.mjs
import { createRequire } from "node:module";
const require = createRequire(new URL("../film/package.json", import.meta.url));
const { chromium } = require("playwright");

const BASE = process.env.BASE || "http://localhost:8673";
let pass = 0, fail = 0;
const ok = (cond, what, extra = "") => { cond ? pass++ : fail++; console.log(`${cond ? "ok  " : "FAIL"} ${what}${extra ? "  " + extra : ""}`); };

async function withPasskey(browser) {
  const ctx = await browser.newContext();
  await english(ctx);
  const page = await ctx.newPage();
  const cdp = await ctx.newCDPSession(page);
  await cdp.send("WebAuthn.enable");
  await cdp.send("WebAuthn.addVirtualAuthenticator", { options: { protocol: "ctap2", transport: "internal", hasResidentKey: true, hasUserVerification: true, isUserVerified: true, automaticPresenceSimulation: true } });
  return { ctx, page };
}
// A checkout that needs a person: 1,000 L is over the account's limit for one order.
const askPerson = page => page.evaluate(async () => {
  const m = await (await fetch("/api/demo/mandate-token")).json();
  const r = await fetch("/checkout-sessions", { method: "POST", headers: { "content-type": "application/json", authorization: "Bearer " + m.mandate },
    body: JSON.stringify({ line_items: [{ item: { id: "464201" }, quantity: 1000 }], wholesale: { delivery_date: "2026-11-16", requirements: { claims: ["lactose_free"] } } }) });
  const c = await r.json();
  const u = new URL(c.continue_url);
  return { id: c.id, status: c.status, k: u.searchParams.get("k") };
});
const post = (page, path, body) => page.evaluate(async ([path, body]) => { const r = await fetch(path, { method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify(body) }); return { status: r.status, body: await r.json() }; }, [path, body]);
const code = r => (r.body.messages || [{}])[0].code;
// A plain browser: no fingerprint, face or screen lock, like many desktop browsers and an automated Chrome.
async function plain(browser) { const ctx = await browser.newContext(); await english(ctx); return { ctx, page: await ctx.newPage() }; }
// The approval page opens in Finnish (the shop's language switch); these checks read its English.
const english = ctx => ctx.addInitScript(() => { try { localStorage.setItem("shop.lang", "en"); } catch (e) {} });
// A checkout with lines that have no allergen declaration on a peanut-free order. A line the wholesaler cannot deliver is dropped and asked again.
const UNDECLARED = ["460006", "460007", "460012", "460026", "460027"], DECLARED = "460001";
const askClear = (page, n = 3) => page.evaluate(async ([ids, declared, n]) => {
  const m = await (await fetch("/api/demo/mandate-token")).json();
  let lines = [...ids, declared], c = null;
  for (let i = 0; i < 4; i++) {
    const und = lines.filter(x => x !== declared).slice(0, n);
    const r = await fetch("/checkout-sessions", { method: "POST", headers: { "content-type": "application/json", authorization: "Bearer " + m.mandate },
      body: JSON.stringify({ line_items: [...und, declared].map(id => ({ item: { id }, quantity: 1 })), wholesale: { delivery_date: "2026-11-16", requirements: { exclude_allergens: ["peanut"] } }, context: { intent: "Vegetables for a peanut-free lunch" } }) });
    c = await r.json();
    const bad = (c.messages || []).filter(x => x.type === "error" && x.path && x.code !== "approval_required").map(x => [...und, declared][+x.path.match(/\[(\d+)\]/)[1]]);
    if (!bad.length) break;
    lines = lines.filter(x => !bad.includes(x));
  }
  if (!c.continue_url) return { id: c.id, status: c.status, messages: c.messages };
  const und = c.wholesale.policy.rules.flatMap(r => r.undeclared || []);
  return { id: c.id, status: c.status, k: new URL(c.continue_url).searchParams.get("k"), und: und.map(u => ({ id: u.id, codes: u.codes, title: u.title })) };
}, [UNDECLARED, DECLARED, n]);
const approval = (page, c) => page.evaluate(async ([id, k]) => (await (await fetch(`/api/approve/${id}?k=${k}`)).json()).wholesale.approval, [c.id, c.k]);
const bend = sig => sig.slice(0, -6) + (sig.slice(-6) === "AAAAAA" ? "BBBBBB" : "AAAAAA");

const shot = async (page, name) => { if (process.env.SHOTS) await page.screenshot({ path: `${process.env.SHOTS}/${name}.png`, fullPage: true }); };

// ---- The browser-key path: a browser with no fingerprint, face or screen lock. One press on the approval page makes
// this browser the approver and signs; after it the link alone is refused; each undeclared line is ticked on its own.
async function browserKeyPath(browser) {
  const C = await plain(browser), D = await plain(browser);
  const errors = [];
  C.page.on("pageerror", e => errors.push(String(e)));
  await C.page.goto(BASE + "/enrol");
  await C.page.waitForFunction(() => typeof PK !== "undefined");
  ok(await C.page.evaluate(() => PK.kindHere()) === "browser_key", "a browser with no authenticator would make its own key");

  // 12. The approval page on an account with no device: none of the undeclared lines is ticked, and Approve waits for every one.
  let c = await askClear(C.page);
  ok(c.status === "requires_escalation" && c.und.length === 3, "a peanut-free order with three undeclared lines asks a person", c.status + " " + JSON.stringify(c.messages || c.und.length));
  await C.page.evaluate(() => localStorage.setItem("shop.lang", "en"));  // the approval page opens in Finnish; these checks read its English
  await C.page.goto(`${BASE}/approve/${c.id}?k=${c.k}`);
  await C.page.waitForSelector(".tick input", { timeout: 15000 });
  await C.page.waitForSelector(".pk b", { timeout: 15000, state: "attached" });  // the signing details sit behind the closed "Show the checks" fold
  const boxes = C.page.locator(".tick input");
  ok((await boxes.count()) === 3 && (await C.page.locator(".tick input:checked").count()) === 0, "each undeclared line has its own tick and none is ticked at first");
  ok(!/tick all|select all/i.test(await C.page.textContent("#main")), "there is no 'tick all'");
  ok(await C.page.isDisabled("#yes"), "Approve is off until every line is ticked");
  ok(/makes a signing key here/.test(await C.page.textContent("#main")) && /a key held in this browser, no fingerprint/.test(await C.page.textContent("#main")), "the page says, before the press, that this browser will sign with its own key and no fingerprint");
  await shot(C.page, "approve-ticks");
  await boxes.nth(0).check(); await boxes.nth(1).check();
  ok(await C.page.isDisabled("#yes") && /2 of 3 confirmed/.test(await C.page.textContent("#tickn")), "two of three ticked is still not enough");

  // 13. A failed poll never destroys the form: a small notice, the ticks and the name stay, and the first press works.
  await C.page.fill("#who", "Maija Korhonen");
  await C.page.route("**/api/approve/*?k=*", route => route.abort());
  await C.page.waitForSelector("#net:not([hidden])", { timeout: 8000 });
  ok(await C.page.inputValue("#who") === "Maija Korhonen" && (await C.page.locator(".tick input:checked").count()) === 2, "with the gateway unreachable the form, the name and the ticks are still there, under a small notice");
  await C.page.unroute("**/api/approve/*?k=*");
  await C.page.waitForSelector("#net", { state: "hidden", timeout: 8000 });
  await boxes.nth(2).check();
  ok(!(await C.page.isDisabled("#yes")), "with every line ticked Approve is on");
  // The API refuses a list that is one line short even when a device would sign it.
  let r = await post(C.page, `/api/approve/${c.id}/device`, { k: c.k, approve: true, approver: "Maija Korhonen", note: "", clears: c.und.slice(0, 2).map(u => ({ id: u.id, codes: u.codes })) });
  ok(r.status === 400 && code(r) === "clears_mismatch", "the gateway will not prepare a signature for an approval that is one line short", code(r));

  // 14. One press: this browser becomes the approver and signs.
  await C.page.click("#yes");
  await C.page.waitForSelector(".big-mark.ok", { timeout: 20000 });
  ok(errors.length === 0, "the first press worked, with no page error", errors.join(" | "));
  let ap = await approval(C.page, c);
  ok(ap && ap.device && ap.device.method === "browser_key" && ap.device.user_verified === false && ap.device.how === "a key held in the browser, no fingerprint", "the record says a key held in the browser signed, with no fingerprint", ap && ap.device && ap.device.how);
  ok(/Signed by Maija Korhonen on .* with a key held in that browser, no fingerprint/.test(await C.page.textContent("#main")), "and so does the page");
  ok(ap.clears.length === 3 && ap.clears_confirmed === "each line ticked by the approver" && JSON.stringify(ap.clears.map(u => u.id)) === JSON.stringify(c.und.map(u => u.id).sort()) && JSON.stringify(ap.device.signed.clears) === JSON.stringify(ap.clears),
    "the signed approval names exactly the three product and allergen pairs it clears");
  ok(ap.asked_by && ap.by === "Maija Korhonen" && ap.asked_by !== ap.by, "who asked and who approved are two names in the record", ap.asked_by);
  ok(await C.page.evaluate(([d, a]) => PK.verify(d, a), [ap.device, ap]), "the browser verifies the browser-key signature from the record alone");
  ok(!(await C.page.evaluate(([d, a]) => PK.verify(d, { ...a, clears: a.clears.slice(1) }), [ap.device, ap])), "and fails it when a cleared line is taken out of the record");
  ok(!(await C.page.evaluate(([d, a]) => PK.verify({ ...d, signature: d.signature.slice(0, -6) + (d.signature.slice(-6) === "AAAAAA" ? "BBBBBB" : "AAAAAA") }, a), [ap.device, ap])), "and when the signature is changed");
  const kept = await C.page.evaluate(async () => { const k = (await PK.keys())[0]; try { await crypto.subtle.exportKey("pkcs8", k.key); return "exported"; } catch (e) { return k.key.extractable === false ? "non-extractable" : "error"; } });
  ok(kept === "non-extractable", "the private key is kept in this browser and cannot be exported", kept);
  let st = await C.page.evaluate(() => fetch("/api/passkey").then(r => r.json()));
  ok(st.approvals === "device" && st.devices.length === 1 && st.devices[0].kind === "browser_key", "the account now has one approver device, a browser key");
  await shot(C.page, "approve-signed");

  // 15. The visible refusal: the same link, without the device, on the approved page.
  await C.page.evaluate(() => document.querySelectorAll("details.fold").forEach(d => { d.open = true; }));  // open "Show the checks"
  await C.page.click("#try");
  await C.page.waitForSelector(".try pre", { timeout: 10000 });
  const shown = await C.page.textContent(".try");
  ok(/403/.test(shown) && /device_required/.test(shown) && /Refused/.test(shown), "'Try the same link without the device' shows the gateway's real refusal");
  await shot(C.page, "approve-refused");

  // 16. From now on the link alone cannot approve for this account, from any browser; a stranger cannot enrol either.
  c = await askClear(C.page, 2);
  ok(c.status === "requires_escalation" && c.und.length === 0, "the same products again are not asked about for allergens (cleared), other rules still ask", c.status + " " + (c.und || []).length);
  c = await askPerson(C.page);
  await D.page.goto(BASE + "/enrol");
  r = await post(D.page, `/api/approve/${c.id}`, { k: c.k, approve: true, approver: "Mallory", note: "" });
  ok(r.status === 403 && code(r) === "device_required", "link-only approval is refused once a browser key is enrolled", code(r));
  r = await post(C.page, `/api/approve/${c.id}`, { k: c.k, approve: true, approver: "Maija Korhonen", note: "" });
  ok(r.status === 403 && code(r) === "device_required", "also from the approver's own browser when no signature is sent", code(r));
  r = await post(D.page, "/api/passkey/enrol/begin", { cid: c.id, k: c.k });
  ok(r.status === 403, "after the first device the approval link cannot enrol another browser", String(r.status));
  await D.page.goto(`${BASE}/approve/${c.id}?k=${c.k}`);
  await D.page.waitForSelector(".pk b", { timeout: 15000, state: "attached" });
  ok(/not one of the account's approver devices/.test(await D.page.textContent("#main")) && await D.page.isDisabled("#yes") && !(await D.page.isDisabled("#no")), "on another browser the page says so: Approve is off, Reject still works");

  // 17. A browser-key signature fits one decision only, and a tampered one is refused.
  await C.page.goto(BASE + "/enrol");
  await C.page.waitForFunction(() => typeof PK !== "undefined");
  const sign = (page, body) => page.evaluate(async body => { const pre = await api(`/api/approve/${body.cid}/device`, body); return { pre, device: await PK.sign(pre) }; }, body);
  const yes = await sign(C.page, { cid: c.id, k: c.k, approve: true, approver: "Maija Korhonen", note: "" });
  r = await post(C.page, `/api/approve/${c.id}`, { k: c.k, approve: true, approver: "Maija Korhonen", note: "", device: { ...yes.device, signature: bend(yes.device.signature) } });
  ok(r.status === 403 && code(r) === "bad_signature", "a tampered browser-key signature is refused", code(r));
  r = await post(C.page, `/api/approve/${c.id}`, { k: c.k, approve: true, approver: "Someone else", note: "", device: yes.device });
  ok(r.status === 400 && code(r) === "wrong_decision", "a signature made for one name does not approve under another", code(r));
  const other = await askPerson(C.page);
  r = await post(C.page, `/api/approve/${other.id}`, { k: other.k, approve: true, approver: "Maija Korhonen", note: "", device: yes.device });
  ok(r.status === 400 && code(r) === "wrong_decision", "an approval signed for one order does not fit another", code(r));
  // A statement signed by a key this account never enrolled: a second browser makes its own key and tries.
  const forged = await D.page.evaluate(async pre => {
    const pair = await crypto.subtle.generateKey({ name: "ECDSA", namedCurve: "P-256" }, false, ["sign"]);
    const cdj = utf8(JSON.stringify({ type: "aimo.key.get", challenge: pre.challenge, origin: location.origin }));
    return { id: pre.keys[0], clientDataJSON: b64u(cdj), signature: b64u(new Uint8Array(await crypto.subtle.sign({ name: "ECDSA", hash: "SHA-256" }, pair.privateKey, cdj))) };
  }, yes.pre);
  r = await post(D.page, `/api/approve/${c.id}`, { k: c.k, approve: true, approver: "Maija Korhonen", note: "", device: forged });
  ok(r.status === 403 && code(r) === "bad_signature", "another browser's key, sent under the enrolled key's name, is refused", code(r));
  r = await post(C.page, `/api/approve/${c.id}`, { k: c.k, approve: true, approver: "Maija Korhonen", note: "", device: yes.device });
  ok(r.status === 200 && r.body.status === "ready_for_complete" && r.body.wholesale.approval.device.method === "browser_key", "the untouched signature is accepted");
  r = await post(D.page, `/api/approve/${other.id}`, { k: other.k, approve: false, approver: "Night manager", note: "" });
  ok(r.status === 200 && r.body.status === "canceled", "rejecting still needs no device", String(r.status));

  // 18. The approver card on /demo: it names the kind of key, and shows the same refusal.
  c = await askPerson(C.page);
  await C.page.goto(BASE + "/demo");
  await C.page.waitForSelector("#approver", { timeout: 20000 });
  await C.page.evaluate(() => { document.querySelector("#approver").open = true; });
  await C.page.waitForSelector("#pktry", { timeout: 15000 });
  ok(/a key held in that browser, no fingerprint/.test(await C.page.textContent("#pkbody")), "the /demo card says the approver device is a browser key with no fingerprint");
  await C.page.click("#pktry");
  await C.page.waitForSelector("#pkbody pre", { timeout: 10000 });
  const card = await C.page.textContent("#pkbody");
  ok(/403/.test(card) && /device_required/.test(card) && /Refused/.test(card), "'Try the same link without the device' on /demo shows the gateway's refusal");
  await shot(C.page, "demo-card");
  ok((await approval(C.page, c)) === null, "and that try approved nothing");

  // 19. The approver's phone is another browser than the one that asked: the link the gateway sent makes it the first device.
  const E = await plain(browser), F = await plain(browser);
  await E.page.goto(BASE + "/enrol");
  c = await askPerson(E.page);
  await F.page.goto(`${BASE}/approve/${c.id}?k=${c.k}`);
  await F.page.waitForSelector(".pk b", { timeout: 15000, state: "attached" });
  await F.page.fill("#who", "Night manager");
  await F.page.click("#yes");
  await F.page.waitForSelector(".big-mark.ok", { timeout: 20000 });
  ap = await approval(F.page, c);
  ok(ap && ap.device && ap.device.method === "browser_key", "a browser that only holds the approval link becomes the account's first approver and signs");
  const led = await E.page.evaluate(() => fetch("/ledger?limit=1000", { headers: { accept: "application/json" } }).then(r => r.json()));
  const en = (led.events || []).filter(e => e.kind === "approver_device_enrolled" && !e.withheld);
  ok(en.length === 1 && /from the approval link, as the account's first device/.test(en[0].text) && /no fingerprint/.test(en[0].text), "the ledger says how that device was enrolled, and what kind of key it is", en.map(e => e.text).join(" | ").slice(0, 160));
  const apd = (led.events || []).filter(e => e.kind === "approved" && !e.withheld);
  ok(apd.length === 1 && /Asked by /.test(apd[0].text) && /a key held in the browser, no fingerprint/.test(apd[0].text), "the approval's ledger line names who asked and what signed");
  c = await askPerson(E.page);
  r = await post(E.page, `/api/approve/${c.id}`, { k: c.k, approve: true, approver: "The one who asked", note: "" });
  ok(r.status === 403 && code(r) === "device_required", "the browser that asked cannot approve its own request by link any more", code(r));

  // 20. The order page: its checks name the browser key, and the device's signature verifies there.
  await C.page.goto(BASE + "/enrol");
  await C.page.waitForFunction(() => typeof PK !== "undefined");
  const small = await askClear(C.page, 0);
  const placed = await C.page.evaluate(async ([cid, k]) => {
    const body = { cid, k, approve: true, approver: "Maija Korhonen", note: "", clears: [] };
    const pre = await api(`/api/approve/${cid}/device`, body);
    const a = await api(`/api/approve/${cid}`, { ...body, device: await PK.sign(pre) });
    const m = await (await fetch("/api/demo/mandate-token")).json();
    const done = await (await fetch(`/checkout-sessions/${cid}/complete`, { method: "POST", headers: { "content-type": "application/json", authorization: "Bearer " + m.mandate }, body: "{}" })).json();
    return { approved: a.status, status: done.status, url: done.order && (done.order.url || done.order.permalink_url) };
  }, [small.id, small.k]);
  ok(placed.approved === "ready_for_complete" && placed.status === "completed" && !!placed.url, "a request with nothing to clear is approved with an empty list, and the order is placed", placed.approved + " " + placed.status);
  if (placed.url) {
    await C.page.goto(BASE + new URL(placed.url).pathname + new URL(placed.url).search);
    await C.page.waitForSelector("#verdict .verdict.ok, #verdict .verdict.bad", { timeout: 30000 });
    const rows = await C.page.locator("#checks .check").allTextContents();
    ok(rows.length === 7 && (await C.page.locator("#verdict .verdict.ok").count()) === 1, "the order page runs seven checks and all pass", String(rows.length));
    ok(rows.some(t => /signed in the approver's own browser/.test(t) && /No fingerprint was involved/.test(t) && /Nobody without that browser/.test(t)), "one of them is the browser key's signature, called what it is");
    ok(rows.some(t => /Asked by .*; approved by Maija Korhonen/.test(t)), "and the approval row names who asked and who approved");
    await shot(C.page, "order-browser-key");
  }
  await C.ctx.close(); await D.ctx.close(); await E.ctx.close(); await F.ctx.close();
}

const browser = await chromium.launch();
try {
  const A = await withPasskey(browser);
  await A.page.goto(BASE + "/enrol");
  await A.page.waitForFunction(() => typeof PK !== "undefined");

  // 1. Before any device is enrolled the link alone still cannot approve: a yes needs a device's signature, the first one included.
  let c = await askPerson(A.page);
  ok(c.status === "requires_escalation", "an order over the limit asks a person", c.status);
  let pre = (await post(A.page, `/api/approve/${c.id}/device`, { k: c.k, approve: true, approver: "Maija", note: "" })).body;
  ok(pre.approvals === "link" && pre.link_ok === false && pre.allow.length === 0 && pre.keys.length === 0 && pre.can_enrol === true, "no device yet: the link alone will not approve, and the account's own browser may enrol one");
  let r = await post(A.page, `/api/approve/${c.id}`, { k: c.k, approve: true, approver: "Maija", note: "" });
  ok(r.status === 403 && code(r) === "device_required", "through the API the link and a name do not approve, even while no device was ever enrolled", code(r));
  r = await post(A.page, `/api/approve/${c.id}`, { k: c.k, approve: true, note: "" });
  ok(r.status === 400 && code(r) === "approver_required", "and an approval with no name is refused: the server never makes one up", code(r));
  r = await post(A.page, `/api/approve/${c.id}`, { k: c.k, approve: false, approver: "Maija", note: "not this week" });
  ok(r.status === 200 && r.body.status === "canceled" && r.body.wholesale.approval.asked_by && r.body.wholesale.approval.asked_by !== "Maija", "the link can still say no, and the record names who asked apart from who decided", r.body.status);

  // 2. A stranger cannot enrol a device: not with nothing, not with a wrong link, not with the link of a request already decided.
  const B = await withPasskey(browser);
  await B.page.goto(BASE + "/enrol");
  await B.page.waitForFunction(() => typeof PK !== "undefined");
  r = await post(B.page, "/api/passkey/enrol/begin", {});
  ok(r.status === 403, "a browser with no account cannot enrol a device", String(r.status));
  r = await post(B.page, "/api/passkey/enrol/begin", { cid: c.id, k: c.k });
  ok(r.status === 403, "the link of a request that is already decided cannot enrol a device", String(r.status));
  c = await askPerson(A.page);
  r = await post(B.page, "/api/passkey/enrol/begin", { cid: c.id, k: c.k + "x" });
  ok(r.status === 403, "a wrong approval key cannot enrol a device", String(r.status));
  pre = (await post(B.page, `/api/approve/${c.id}/device`, { k: c.k, approve: true, approver: "Mallory", note: "" })).body;
  ok(pre.can_enrol === true, "while the account has no device, the open approval link the gateway sent may make the first one");

  // 3. The account enrols its own device through the page.
  await A.page.goto(BASE + "/enrol");
  await A.page.click("#go");
  await A.page.waitForSelector(".big-mark.ok", { timeout: 15000 });
  ok(/now approves for/.test(await A.page.textContent("h2")), "the enrol page says the device now approves");
  const st = await A.page.evaluate(() => fetch("/api/passkey").then(r => r.json()));
  ok(st.approvals === "device" && st.devices.length === 1 && st.devices[0].kind === "passkey" && !!st.enrol_url, "the account lists one device, a passkey, and offers an enrolment link", JSON.stringify(st.devices.map(d => d.name)));
  pre = (await post(B.page, `/api/approve/${c.id}/device`, { k: c.k, approve: true, approver: "Mallory", note: "" })).body;
  r = await post(B.page, "/api/passkey/enrol/begin", { cid: c.id, k: c.k });
  ok(pre.can_enrol === false && r.status === 403, "after the first device, holding an approval link does not allow enrolling another", String(r.status));

  // 4. From now on the link alone cannot approve, from any browser.
  r = await post(B.page, `/api/approve/${c.id}`, { k: c.k, approve: true, approver: "Mallory", note: "" });
  ok(r.status === 403 && code(r) === "device_required", "link-only approval is refused once a device is enrolled", code(r));
  r = await post(A.page, `/api/approve/${c.id}`, { k: c.k, approve: true, approver: "Maija", note: "" });
  ok(r.status === 403 && code(r) === "device_required", "also from the account's own browser without a signature", code(r));

  // 5. A signature over one decision cannot be used for another.
  await A.page.goto(BASE + "/enrol");
  await A.page.waitForFunction(() => typeof PK !== "undefined");
  const sign = (page, body) => page.evaluate(async body => { const pre = await api(`/api/approve/${body.cid}/device`, body); return { pre, device: await PK.sign(pre) }; }, body);
  const no = await sign(A.page, { cid: c.id, k: c.k, approve: false, approver: "Maija", note: "" });
  r = await post(A.page, `/api/approve/${c.id}`, { k: c.k, approve: true, approver: "Maija", note: "", device: no.device });
  ok(r.status === 400 && code(r) === "wrong_decision", "a signed 'reject' cannot be replayed as an 'approve'", code(r));
  const other = await askPerson(A.page);
  const forOther = await sign(A.page, { cid: other.id, k: other.k, approve: true, approver: "Maija", note: "" });
  r = await post(A.page, `/api/approve/${c.id}`, { k: c.k, approve: true, approver: "Maija", note: "", device: forOther.device });
  ok(r.status === 400 && code(r) === "wrong_decision", "an approval signed for another order does not fit this one", code(r));
  const yes = await sign(A.page, { cid: c.id, k: c.k, approve: true, approver: "Maija", note: "ok this week" });
  r = await post(A.page, `/api/approve/${c.id}`, { k: c.k, approve: true, approver: "Maija", note: "changed note", device: yes.device });
  ok(r.status === 400 && code(r) === "wrong_decision", "changing the note after signing breaks the signature", code(r));
  const bent = { ...yes.device, signature: bend(yes.device.signature) };
  r = await post(A.page, `/api/approve/${c.id}`, { k: c.k, approve: true, approver: "Maija", note: "ok this week", device: bent });
  ok(r.status === 403 && code(r) === "bad_signature", "a changed signature is refused", code(r));

  // 6. The real thing: the device signs, the gateway accepts, and the browser can check it again from the record alone.
  r = await post(A.page, `/api/approve/${c.id}`, { k: c.k, approve: true, approver: "Maija", note: "ok this week", device: yes.device });
  const ap = r.body.wholesale && r.body.wholesale.approval;
  ok(r.status === 200 && r.body.status === "ready_for_complete" && ap && ap.device && ap.device.method === "passkey", "a device-signed approval is accepted and kept with the checkout");
  ok(ap && ap.device.user_verified === true, "the record says the device checked the person (fingerprint, face or screen lock)");
  ok(await A.page.evaluate(([d, a]) => PK.verify(d, a), [ap.device, ap]), "the browser verifies the device signature from the record");
  ok(!(await A.page.evaluate(([d, a]) => PK.verify({ ...d, signed: { ...d.signed, decision: "rejected" } }, a), [ap.device, ap])), "and fails it when the decision in the record is changed");
  ok(!(await A.page.evaluate(([d, a]) => PK.verify(d, { ...a, by: "Someone else" }), [ap.device, ap])), "and when the approver's name is changed");

  // 7. Saying no needs no device: anyone holding the link may stop an order.
  r = await post(B.page, `/api/approve/${other.id}`, { k: other.k, approve: false, approver: "Night manager", note: "" });
  ok(r.status === 200 && r.body.status === "canceled", "a link alone can still reject", String(r.status));

  // 8. A second approver joins through the account's enrolment link; a bent link does not work.
  await B.page.goto(st.enrol_url.replace(/t=([^.]+)\./, "t=sandbox-00000000."));
  await B.page.waitForSelector(".state", { timeout: 15000 });
  ok(/expired/.test(await B.page.textContent("h2")), "an enrolment link for another account is refused");
  await B.page.goto(st.enrol_url);
  await B.page.click("#go");
  await B.page.waitForSelector(".big-mark.ok", { timeout: 15000 });
  const st2 = await A.page.evaluate(() => fetch("/api/passkey").then(r => r.json()));
  ok(st2.devices.length === 2, "the second device is enrolled through the link", String(st2.devices.length));
  c = await askPerson(A.page);
  const byB = await B.page.evaluate(async body => { const pre = await api(`/api/approve/${body.cid}/device`, body); const device = await PK.sign(pre); return api(`/api/approve/${body.cid}`, { ...body, device }); }, { cid: c.id, k: c.k, approve: true, approver: "Second approver", note: "" });
  ok(byB.status === "ready_for_complete" && byB.wholesale.approval.device.credential !== ap.device.credential, "the second device approves with its own key");

  // 9. The approval page itself: with a device enrolled, Approve asks the device and the page says who signed.
  const shot = async (page, name) => { if (process.env.SHOTS) await page.screenshot({ path: `${process.env.SHOTS}/${name}.png`, fullPage: true }); };
  c = await askPerson(A.page);
  await A.page.goto(`${BASE}/approve/${c.id}?k=${c.k}`);
  await A.page.waitForSelector("#yes:not([disabled])", { timeout: 15000 });
  await A.page.waitForSelector(".pk b", { timeout: 15000, state: "attached" });
  ok(/Signed on an approver's device/.test(await A.page.textContent("#main")) && /a passkey/.test(await A.page.textContent("#main")), "the approval page says approvals are signed on a device, with a passkey");
  await shot(A.page, "approve-device");
  await A.page.click("#yes");
  ok(/Type your name first/.test(await A.page.textContent("#bar .hint")) && !(await A.page.locator(".big-mark.ok").count()), "without a name nothing is approved: the page asks for it");
  await A.page.fill("#who", "Maija Korhonen");
  await A.page.click("#yes");
  await A.page.waitForSelector(".big-mark.ok", { timeout: 20000 });
  const seen = await A.page.evaluate(async ([id, k]) => (await (await fetch(`/api/approve/${id}?k=${k}`)).json()).wholesale.approval, [c.id, c.k]);
  ok(seen && seen.device && seen.device.method === "passkey" && /Signed by .* on .* with a passkey/.test(await A.page.textContent("#main")), "one tap on the page: the device signed and the page says a passkey did");

  // 10. Order from a menu on the same account: the menu page cannot approve. It shows the approval link and waits;
  // the approver ticks each undeclared line on the approval page and signs on the device; then the menu page shows the order.
  await A.page.goto(BASE + "/menu");
  await A.page.waitForSelector('.sample[data-id="wedding"]', { timeout: 20000 });
  await A.page.click('.sample[data-id="wedding"]');
  const planned = await A.page.waitForSelector("#await, #go", { timeout: 150000 }).then(() => true).catch(() => false);
  if (!planned) console.log("skip the menu part: the model did not read the sample photo in time");
  else {
    const text = await A.page.textContent("#out");
    ok(/no allergen declaration from the manufacturer/.test(text) && !/Every line passed its four checks/.test(text), "the plan says which lines have no allergen declaration instead of calling them passed");
    ok((await A.page.locator(".chk.unk").count()) > 0, "those lines carry 'Allergens: not declared', not a tick");
    ok((await A.page.locator("#go").count()) === 0 && !/Approve and place/.test(text) && /Waiting for the approver/.test(await A.page.textContent("#await")), "the menu page has no approve button: it waits for the approver");
    const link = await A.page.getAttribute("#aplink", "href");
    ok(/^\/approve\/chk_\w+\?k=/.test(link || "") && (await A.page.locator('#await img[src^="/api/qr"]').count()) === 1, "it shows the approval link and its QR code");
    await shot(A.page, "menu-waiting");
    const run = await A.page.evaluate(() => localStorage.getItem("menu.run"));
    r = await post(A.page, "/api/menu/order", { run });
    ok(r.status === 409 && code(r) === "approval_pending", "the menu page's own order call cannot approve or place it", code(r));
    r = await post(A.page, "/api/menu/order", { run, device: { id: "x" }, approve: true });
    ok(r.status === 409 && code(r) === "approval_pending", "whatever it sends", code(r));
    // The approver's side, on the device with the passkey.
    await A.page.goto(BASE + link);
    await A.page.waitForSelector(".tick input", { timeout: 15000 });
    const ticks = A.page.locator(".tick input"), nTicks = await ticks.count();
    ok(nTicks > 1 && (await A.page.locator(".tick input:checked").count()) === 0 && await A.page.isDisabled("#yes"), `the approval page lists ${nTicks} undeclared lines, none ticked, Approve off`);
    ok(/Kitchen manager, on the menu page/.test(await A.page.textContent("#main")), "it names who asked");
    for (let i = 0; i < nTicks; i++) await ticks.nth(i).check();
    await A.page.fill("#who", "Event approver");
    await shot(A.page, "menu-approve");
    await A.page.click("#yes");
    await A.page.waitForSelector(".big-mark.ok", { timeout: 20000 });
    // Back on the menu page: the plan is picked up and the order is placed without another tap.
    await A.page.goto(BASE + "/menu");
    await A.page.waitForSelector("#r-go", { timeout: 20000 });
    await A.page.click("#r-go");
    await A.page.waitForSelector(".donec", { timeout: 60000 });
    ok(/is placed/.test(await A.page.textContent(".donec h2")), "after the approver's signature the order shows on the menu page");
    const doneText = await A.page.textContent(".donec");
    ok(/Kitchen manager, on the menu page asked\. Event approver approved/.test(doneText) && /with a passkey/.test(doneText) && new RegExp(`each of the ${nTicks} lines`).test(doneText), "it names who asked and who approved, separately, and what signed");
    ok((await A.page.locator(".chk.unk").count()) === 0 && /Event approver cleared each one with its own tick/.test(await A.page.textContent("#out")), "the lines read as cleared by the approver, one tick each");
    await shot(A.page, "menu-done");
    await A.page.click('.donec a[href^="/orders/"]');
    await A.page.waitForSelector("#verdict .verdict.ok, #verdict .verdict.bad", { timeout: 30000 });
    const rows = await A.page.locator("#checks .check").allTextContents();
    ok(rows.length === 7 && (await A.page.locator("#verdict .verdict.ok").count()) === 1, "the order page runs seven checks and all pass", String(rows.length));
    ok(rows.some(t => /signed on the approver's own device/.test(t) && /Nobody without that device/.test(t) && /each line it clears/.test(t)), "one of them is the device's own signature, checked in the browser");
    ok(rows.some(t => /Asked by Kitchen manager, on the menu page; approved by Event approver/.test(t) && new RegExp(`It clears ${nTicks} lines`).test(t)), "and the approval row names who asked, who approved and the lines cleared");
    await shot(A.page, "order-device");
    // The same menu again: what a person cleared is remembered, so the allergen rule no longer asks.
    await A.page.goto(BASE + "/menu");
    await A.page.waitForSelector('.sample[data-id="wedding"]', { timeout: 20000 });
    await A.page.click('.sample[data-id="wedding"]');
    if (await A.page.waitForSelector("#await, #go", { timeout: 150000 }).then(() => true).catch(() => false)) {
      const again = await A.page.evaluate(async () => (await (await fetch("/api/menu/run/" + localStorage.getItem("menu.run"))).json()));
      const a1 = (again.checkout.rules || []).filter(x => x.id === "A1");
      ok(a1.length === 1 && a1[0].outcome === "pass" && /cleared for this exclusion by/.test(a1[0].detail), "ordering the same menu again does not ask about the cleared products", a1.map(x => x.outcome).join(","));
    } else console.log("skip the second menu: the model did not read the sample photo in time");
  }

  // 11. The ledger has the enrolment, and removing the devices returns the account to link approval.
  const led = await A.page.evaluate(() => fetch("/ledger?limit=1000", { headers: { accept: "application/json" } }).then(r => r.json()));
  ok((led.events || []).filter(e => e.kind === "approver_device_enrolled" && !e.withheld).length === 2, "both enrolments are on the ledger");
  r = await post(A.page, "/api/passkey/remove", {});
  ok(r.body.removed === 2 && r.body.approvals === "link", "removing the devices returns the account to link approval");

  await browserKeyPath(browser);
} finally {
  await browser.close();
}
console.log(fail ? `${fail} of ${pass + fail} CHECKS FAIL` : `ALL ${pass} CHECKS PASS`);
process.exit(fail ? 1 : 0);
