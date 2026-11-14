// An approver's own device signs the decision: enrol, approve, verify, and the ways around it that must fail.
// Uses a software passkey inside a test browser (Chromium's virtual authenticator), so no real phone is needed.
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

const browser = await chromium.launch();
try {
  const A = await withPasskey(browser);
  await A.page.goto(BASE + "/enrol");
  await A.page.waitForFunction(() => typeof PK !== "undefined");

  // 1. Before any device is enrolled the account approves by link.
  let c = await askPerson(A.page);
  ok(c.status === "requires_escalation", "an order over the limit asks a person", c.status);
  let pre = (await post(A.page, `/api/approve/${c.id}/device`, { k: c.k, approve: true, approver: "Maija", note: "" })).body;
  ok(pre.approvals === "link" && pre.allow.length === 0 && pre.can_enrol === true, "no device yet: approvals are by link, and the account's own browser may enrol one");
  let r = await post(A.page, `/api/approve/${c.id}`, { k: c.k, approve: true, approver: "Maija", note: "" });
  ok(r.status === 200 && r.body.status === "ready_for_complete" && !r.body.wholesale.approval.device, "link approval works while no device is enrolled");

  // 2. A stranger with only an approval link cannot enrol a device.
  const B = await withPasskey(browser);
  await B.page.goto(BASE + "/enrol");
  await B.page.waitForFunction(() => typeof PK !== "undefined");
  r = await post(B.page, "/api/passkey/enrol/begin", {});
  ok(r.status === 403, "a browser with no account cannot enrol a device", String(r.status));
  c = await askPerson(A.page);
  pre = (await post(B.page, `/api/approve/${c.id}/device`, { k: c.k, approve: true, approver: "Mallory", note: "" })).body;
  ok(pre.can_enrol === false, "holding an approval link does not allow enrolling a device");

  // 3. The account enrols its own device through the page.
  await A.page.goto(BASE + "/enrol");
  await A.page.click("#go");
  await A.page.waitForSelector(".big-mark.ok", { timeout: 15000 });
  ok(/now approves for/.test(await A.page.textContent("h2")), "the enrol page says the device now approves");
  const st = await A.page.evaluate(() => fetch("/api/passkey").then(r => r.json()));
  ok(st.approvals === "passkey" && st.devices.length === 1 && !!st.enrol_url, "the account lists one device and offers an enrolment link", JSON.stringify(st.devices.map(d => d.name)));

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
  const bent = { ...yes.device, signature: yes.device.signature.slice(0, -6) + (yes.device.signature.slice(-6) === "AAAAAA" ? "BBBBBB" : "AAAAAA") };
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
  await A.page.waitForSelector(".pk", { timeout: 15000 });
  ok(/Signed on an approver's device/.test(await A.page.textContent(".pk")), "the approval page says approvals are signed on a device");
  await shot(A.page, "approve-device");
  await A.page.click("#yes");
  await A.page.waitForSelector(".big-mark.ok", { timeout: 20000 });
  const seen = await A.page.evaluate(async ([id, k]) => (await (await fetch(`/api/approve/${id}?k=${k}`)).json()).wholesale.approval, [c.id, c.k]);
  ok(seen && seen.device && seen.device.method === "passkey" && /Signed on /.test(await A.page.textContent("#main")), "one tap on the page: the device signed and the page names it");

  // 10. Order from a menu on the same account: unchecked allergens are shown as such, the tap alone is refused, the device signs.
  await A.page.goto(BASE + "/menu");
  await A.page.waitForSelector('.sample[data-id="wedding"]', { timeout: 20000 });
  await A.page.click('.sample[data-id="wedding"]');
  const planned = await A.page.waitForSelector("#go", { timeout: 150000 }).then(() => true).catch(() => false);
  if (!planned) console.log("skip the menu part: the model did not read the sample photo in time");
  else {
    const text = await A.page.textContent("#out");
    ok(/no allergen declaration from the manufacturer/.test(text) && !/Every line passed its four checks/.test(text), "the plan says which lines have no allergen declaration instead of calling them passed");
    ok((await A.page.locator(".chk.unk").count()) > 0, "those lines carry 'Allergens: not declared', not a tick");
    ok(/Approve on this device/.test(await A.page.textContent("#go")), "the order button says the approval is signed on this device");
    await shot(A.page, "menu-plan");
    const run = await A.page.evaluate(() => localStorage.getItem("menu.run"));
    r = await post(A.page, "/api/menu/order", { run });
    ok(r.status === 403 && code(r) === "device_required", "a tap without the device cannot place the menu order", code(r));
    await A.page.click("#go");
    await A.page.waitForSelector(".donec", { timeout: 60000 });
    ok(/is placed/.test(await A.page.textContent(".donec h2")), "with the device's signature the order is placed");
    ok((await A.page.locator(".chk.unk").count()) === 0 && /you cleared/.test(await A.page.textContent("#out")), "after approval the lines read as cleared by the person");
    await shot(A.page, "menu-done");
    await A.page.click('.donec a[href^="/orders/"]');
    await A.page.waitForSelector("#verdict .verdict.ok, #verdict .verdict.bad", { timeout: 30000 });
    const rows = await A.page.locator("#checks .check").allTextContents();
    ok(rows.length === 7 && (await A.page.locator("#verdict .verdict.ok").count()) === 1, "the order page runs seven checks and all pass", String(rows.length));
    ok(rows.some(t => /signed on the approver's own device/.test(t) && /Nobody without that device/.test(t)), "one of them is the device's own signature, checked in the browser");
    await shot(A.page, "order-device");
    // The same menu again: what a person cleared is remembered, so the allergen rule no longer asks.
    await A.page.goto(BASE + "/menu");
    await A.page.waitForSelector('.sample[data-id="wedding"]', { timeout: 20000 });
    await A.page.click('.sample[data-id="wedding"]');
    if (await A.page.waitForSelector("#go", { timeout: 150000 }).then(() => true).catch(() => false)) {
      const again = await A.page.evaluate(async () => (await (await fetch("/api/menu/run/" + localStorage.getItem("menu.run"))).json()));
      const a1 = (again.checkout.rules || []).filter(x => x.id === "A1");
      ok(a1.length === 1 && a1[0].outcome === "pass" && /cleared for this exclusion by/.test(a1[0].detail), "ordering the same menu again does not ask about the cleared products", a1.map(x => x.outcome).join(","));
    } else console.log("skip the second menu: the model did not read the sample photo in time");
  }

  // 11. The ledger has the enrolment, and removing the devices returns the account to link approval.
  const led = await A.page.evaluate(() => fetch("/ledger?limit=1000", { headers: { accept: "application/json" } }).then(r => r.json()));
  ok((led.events || []).filter(e => e.kind === "approver_device_enrolled").length === 2, "both enrolments are on the ledger");
  r = await post(A.page, "/api/passkey/remove", {});
  ok(r.body.removed === 2 && r.body.approvals === "link", "removing the devices returns the account to link approval");
} finally {
  await browser.close();
}
console.log(fail ? `${fail} of ${pass + fail} CHECKS FAIL` : `ALL ${pass} CHECKS PASS`);
process.exit(fail ? 1 : 0);
