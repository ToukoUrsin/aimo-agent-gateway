// Builds deck/aimo-agent-gateway.pdf from deck/deck.html.
// Every number is read from the server and every screenshot is taken from it, so the deck cannot drift from the product.
//   node deck/build.mjs                                   (the public site, https://trusted-trade-agents.fly.dev)
//   BASE=http://127.0.0.1:8642 node deck/build.mjs        (another server)
// It talks to /shop three times as a cook would, on a phone, in Finnish: the wedding line, the short cream, and the 1,000 L of milk
// that goes to an approver in a second browser and is then confirmed. Those carts are real carts on that server; the milk is a real order.
// SKIP_SHOTS=1 keeps the last screenshots and the numbers read from them (deck/shots/shop.json). PREVIEW=1 also writes one PNG per slide.
// Screenshots use WebKit: headless Chromium fails page loads on this box. Chromium only prints the local HTML file to PDF.
import { createRequire } from "node:module";
import { readFile, writeFile, mkdir } from "node:fs/promises";
import { fileURLToPath } from "node:url";
import path from "node:path";

const require = createRequire(new URL("../film/package.json", import.meta.url));
const { webkit, chromium } = require("playwright");
const here = path.dirname(fileURLToPath(import.meta.url));
const BASE = (process.env.BASE || "https://trusted-trade-agents.fly.dev").replace(/\/$/, "");
const PREVIEW_DIR = process.env.PREVIEW_DIR || "/bulk3/runs/valio/deck2/preview";
const n = x => Number(x).toLocaleString("en-GB");
const esc = s => String(s).replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;");
const shot = name => path.join(here, "shots", name);
await mkdir(path.join(here, "shots"), { recursive: true });

const api = async p => {
  for (let i = 0; ; i++) {
    try { const r = await fetch(BASE + p, { headers: { accept: "application/json" } }); if (!r.ok) throw new Error(p + " " + r.status); return await r.json(); }
    catch (e) { if (i >= 4) throw e; await new Promise(r => setTimeout(r, 4000)); }   // the site restarts for a few seconds on a deploy
  }
};
const retry = async (what, fn) => {
  for (let i = 0; ; i++) {
    try { return await fn(); }
    catch (e) { if (i >= 2) throw e; console.log(what, "failed, again:", String(e.message).split("\n")[0]); await new Promise(r => setTimeout(r, 6000)); }
  }
};

// ---------------------------------------------------------------- the shop on a phone
const PHONE = { viewport: { width: 390, height: 844 }, deviceScaleFactor: 3, isMobile: true, hasTouch: true, locale: "fi-FI" };
const CHAT = {
  wedding: "Häät lauantaina, 180 henkeä: lohikeitto, broilerin rintafilee timjamikermakastikkeessa, paahdetut perunat ja porkkanat, mansikkakermakakku ja kahvi",
  short: "60 litraa laktoositonta Arla Pro -vispikermaa maanantaiksi",
  milk: "1 000 litraa laktoositonta maitojuomaa maanantaiksi",
};
const APPROVER = "Ravintolapäällikkö";   // a role, not a person

async function phone(browser) {
  const ctx = await browser.newContext(PHONE);
  await ctx.addInitScript(() => { try { localStorage.setItem("shop.lang", "fi"); } catch (e) {} });
  const page = await ctx.newPage();
  await page.goto(BASE + "/shop", { waitUntil: "load" });
  await page.waitForSelector(".greet .msg", { timeout: 30000 });
  return { ctx, page };
}
const idle = async page => { await page.waitForTimeout(600); await page.waitForFunction(() => !document.querySelector(".status"), null, { timeout: 150000 }); await page.waitForTimeout(800); };
async function say(page, text) {
  const t0 = Date.now();
  await page.fill("#text", text);
  await page.click("#send");
  await page.waitForSelector(".cart", { timeout: 150000 });
  const s = (Date.now() - t0) / 1000;      // from pressing send to the cart on the screen, measured here
  await idle(page);
  return s;
}
// Scroll the chat so that `top` sits just under the tabs and, when given, `bottom` stays above the input bar.
async function frame(page, top, pad = 12) {
  await page.evaluate(([sel, pad]) => {
    const log = document.querySelector(".log"), el = [...document.querySelectorAll(sel)].pop();
    log.scrollTop += el.getBoundingClientRect().top - log.getBoundingClientRect().top - pad;
  }, [top, pad]);
  await page.waitForTimeout(400);
}
const cartText = page => page.evaluate(() => [...document.querySelectorAll(".cart")].pop().innerText);

async function shopShots(browser) {
  const out = {};
  // 1. One line, one cart.
  await retry("wedding", async () => {
    const { ctx, page } = await phone(browser);
    out.wedding_s = await say(page, CHAT.wedding);
    const made = await page.evaluate(() => { const m = [...document.querySelectorAll(".cart .made")].pop(); return m ? m.innerText : ""; });
    const mm = /(\d+) riviä ostoskorissa, (\d+) sekunnissa/.exec(made);
    if (!mm) throw new Error("the wedding cart printed no line count: " + made);
    out.wedding_lines = +mm[1]; out.wedding_page_s = +mm[2]; out.wedding_made = made;
    out.wedding_contract = await page.evaluate(() => { const c = Object.values(carts).pop(); return c.d.line_items.filter(l => (l.wholesale || {}).contract_price).length; });
    out.wedding_total = /Yhteensä \(sis\. alv\)\s*([\d\s ]+,\d\d\s*€)/.exec(await cartText(page))?.[1].trim();
    await frame(page, ".msg.me");
    await page.screenshot({ path: shot("shop-wedding.png") });
    await page.locator(".cart [data-act=rows]").last().click();   // the lines themselves, for the title slide
    await page.waitForTimeout(500);
    await frame(page, ".cart .made", 8);
    await page.screenshot({ path: shot("shop-rows.png") });
    await ctx.close();
  });
  console.log("shot wedding", out.wedding_lines, "lines");
  // 2. A product runs short.
  await retry("short", async () => {
    const { ctx, page } = await phone(browser);
    await say(page, CHAT.short);
    const t = await cartText(page);
    if (!/Korvaava tuote:/.test(t)) throw new Error("no replacement on the cream cart: " + t.slice(0, 300));
    out.short_have = /Ei tarpeeksi saatavuutta:[^\n]*?(\d+ \/ \d+ \S+)/.exec(t)?.[1]?.replace(/\.$/, "");
    out.short_sub = /Korvaava tuote:\s*([^\n]+)/.exec(t)?.[1];
    out.short_why = /(Sama tuotelaji[^\n]+)/.exec(t)?.[1];
    out.short_contract = /sopimushinta/.test(/Korvaava tuote:\s*([^\n]+)/.exec(t)?.[1] || "");   // the card on the slide says "sopimushinta"
    // Rows open by default on a small cart: close them, so the card is the replacement, the price and the button.
    const rows = page.locator(".cart [data-act=rows]").last();
    if (/Piilota/.test(await rows.innerText())) { await rows.click(); await page.waitForTimeout(300); }
    await frame(page, ".msg.me");
    await page.screenshot({ path: shot("shop-short.png") });
    await page.locator(".cart .nt [data-act=swapopen], .cart [data-act=swapopen]").first().click();
    await page.waitForSelector(".cart .alts");
    await page.waitForTimeout(600);
    out.short_alts = await page.locator(".cart .alts button").count();
    await frame(page, ".cart", 8);
    await page.screenshot({ path: shot("shop-swap.png") });
    await ctx.close();
  });
  console.log("shot short", out.short_sub);
  // 3. Over the limit: stops, someone else approves on their own phone, then it is confirmed.
  await retry("milk", async () => {
    const { ctx, page } = await phone(browser);
    await say(page, CHAT.milk);
    const t = await cartText(page);
    const lim = /Tilaus on ([\d\s ]+,\d\d\s*€) \(alv[^)]*\)\. Ilman hyväksyntää saat tilata enintään\s*([\d\s ]+,\d\d\s*€)/.exec(t);
    if (!lim) throw new Error("the milk cart did not stop for an approver: " + t.slice(0, 300));
    out.milk_eur = lim[1].trim(); out.milk_limit = lim[2].trim();
    await frame(page, ".msg.me");
    await page.screenshot({ path: shot("shop-stop.png") });
    const url = await page.evaluate("Object.values(carts).map(c => c.d.continue_url).filter(Boolean).pop()");
    const c2 = await browser.newContext(PHONE), p2 = await c2.newPage();   // the approver's own phone: another browser
    await p2.goto(url, { waitUntil: "load" });
    await p2.waitForSelector("#who", { timeout: 30000 });
    await p2.waitForTimeout(800);
    await p2.screenshot({ path: shot("approve.png") });
    await p2.fill("#who", APPROVER);
    await p2.click("#yes");
    await p2.waitForTimeout(3000);
    await c2.close();
    await page.waitForFunction(() => /Hyväksynyt/.test([...document.querySelectorAll(".cart")].pop().innerText), null, { timeout: 30000 });
    await page.locator(".cart [data-act=confirm]").last().click();
    await page.waitForSelector(".cart.done", { timeout: 60000 });
    await idle(page);
    out.milk_order = /Vahvistettu tilaus (\S+)/.exec(await cartText(page))?.[1];
    const rows = page.locator(".cart.done [data-act=rows]").last();
    if (await rows.count() && /Piilota/.test(await rows.innerText())) { await rows.click(); await page.waitForTimeout(300); }
    await frame(page, ".msg.me", 12);
    await page.screenshot({ path: shot("shop-done.png") });
    await ctx.close();
  });
  console.log("shot milk", out.milk_order);
  return out;
}

// ---------------------------------------------------------------- the desktop pages
async function deskShots(browser) {
  const ctx = await browser.newContext({ viewport: { width: 1180, height: 900 }, deviceScaleFactor: 2 });
  await ctx.addInitScript(() => { try { localStorage.setItem("shop.lang", "en"); } catch (e) {} });   // the deck is in English; these two pages open in Finnish
  await retry("access", async () => {
    const page = await ctx.newPage();
    await page.goto(BASE + "/access", { waitUntil: "load" });
    await page.waitForTimeout(4000);
    await page.locator("#ctl").screenshot({ path: shot("access.png") });
    await page.close();
  });
  console.log("shot access");
  await retry("tower", async () => {
    const page = await ctx.newPage();
    await page.goto(BASE + "/tower", { waitUntil: "load" });
    await page.waitForFunction(() => document.querySelectorAll("#cells > *").length >= 60, null, { timeout: 60000 });
    await page.waitForTimeout(Number(process.env.TOWER_WAIT || 12000));   // the replay needs a moment to fill the grid
    const clip = await page.evaluate(() => {
      const a = document.querySelector("#rush").getBoundingClientRect(), b = document.querySelector("#rkey").getBoundingClientRect();
      return { x: a.left, y: a.top + scrollY, width: a.width, height: b.bottom - a.top + 20 };
    });
    await page.screenshot({ path: shot("tower.png"), clip, fullPage: true });
    await page.close();
  });
  console.log("shot tower");
  await ctx.close();
}

// ---------------------------------------------------------------- build
const [menu, rush, proof, home] = [await api("/api/menu/latest"), await api("/api/rush/latest?events=0"), await api("/api/proof"), await api("/api/home")];
let shop;
if (!process.env.SKIP_SHOTS) {
  const wk = await webkit.launch();
  shop = await shopShots(wk);
  await deskShots(wk);
  await wk.close();
  const shopUrl = home.public_url.replace(/\/$/, "") + "/shop";
  const qr = await fetch(BASE + "/api/qr?u=" + encodeURIComponent(shopUrl));
  if (!qr.ok) throw new Error("qr " + qr.status);
  await writeFile(shot("qr.svg"), await qr.text());
  await writeFile(shot("shop.json"), JSON.stringify(shop, null, 1));
} else shop = JSON.parse(await readFile(shot("shop.json"), "utf8"));

const first = (proof.runs || []).find(r => r.unsafe > 0);   // the run that caught the allergen bug
if (!first) throw new Error("no run with an unsafe order in /api/proof: slide 7 has no story");
const bh = menu.by_hand || {};
const measured = bh.basis === "measured";
const v = {
  "home.url": home.public_url.replace(/^https?:\/\//, "").replace(/\/$/, ""),
  "shop.ask": esc(CHAT.wedding),
  "shop.made": esc(shop.wedding_made),
  "shop.lines": shop.wedding_lines,
  "shop.page_s": shop.wedding_page_s,
  "shop.wall_s": Math.round(shop.wedding_s),
  "shop.total": esc(shop.wedding_total || ""),
  "hand.min": measured ? Math.round(bh.minutes) + " min" : "~" + Math.round(shop.wedding_lines * bh.seconds_per_line / 60) + " min",
  "hand.what": measured ? `the wedding menu by hand on /byhand: median of ${bh.attempts} timed attempts by ${bh.n} ${bh.n === 1 ? "person" : "people"}`
                        : `the same ${shop.wedding_lines} lines added one by one. Our estimate, not measured: ${bh.seconds_per_line} s a line`,
  "hand.class": measured ? "" : "est",
  "short.ask": esc(CHAT.short),
  "short.have": esc(shop.short_have || ""),
  "short.sub": esc((shop.short_sub || "").split(" · ")[0]),
  "short.diff": esc(((shop.short_sub || "").split(" · ").pop().match(/([−+-])(\d+)\s*%\/(\S+)/) || []).slice(1).reduce((a, x, i, m) => i ? a : `${m[1]}% ${m[0] === "+" ? "dearer" : "cheaper"} per ${m[2] === "l" ? "litre" : m[2]}`, "")),
  "short.why": esc(shop.short_why || ""),
  "short.why_en": "Same kind of product, same diet markings, no new allergens. From Valio Aimo's " + (/korvaavien tuotteiden listalta/.test(shop.short_why) ? "list of replacements" : "range")
                  + ".",
  "short.price": shop.short_contract ? "marked sopimushinta (contract price) in the test data" : "with the price difference shown",
  "shop.contract": shop.wedding_contract,
  "short.alts": shop.short_alts,
  "milk.ask": esc(CHAT.milk),
  "milk.eur": esc(shop.milk_eur),
  "milk.limit": esc(shop.milk_limit),
  "milk.order": esc(shop.milk_order || ""),
  "approver": esc(APPROVER),
  "und.n": n(proof.undeclared.undeclared),
  "und.food": n(proof.undeclared.food_products),
  "und.pct": Math.round(proof.undeclared.pct),
  "rush.agents": rush.agents_total,
  "rush.models": rush.mix.model_agents,
  "rush.scripts": rush.mix.scripted_standing_order_bots + rush.mix.scripted_planner_bots,
  "rush.stock": n(rush.stock.litres_in_stock),
  "rush.asked": n(rush.stock.litres_asked),
  "rush.ordered": n(rush.stock.litres_ordered),
  "rush.people": rush.people.asked,
  "rush.seconds": Math.round(rush.seconds.first_request_to_last_order),
  "rush.when": new Date(rush.when).toLocaleString("en-GB", { day: "numeric", month: "short", hour: "2-digit", minute: "2-digit", timeZone: "Europe/Helsinki" }),
  "proof.before": first.unsafe,
  "proof.before_runs": first.runs,
  "proof.before_models": first.models,
  "proof.unsafe": proof.totals.orders_breaking_a_rule,
  "proof.runs": proof.totals.runs,
  "proof.models": proof.totals.models,
  "proof.scenarios": proof.totals.scenarios,
  "proof.orders": proof.totals.orders,
};
if (rush.stock.over_allocated) throw new Error("the latest rush over-allocated: the deck says never");

let html = await readFile(path.join(here, "deck.html"), "utf8");
html = html.replace(/\{\{([a-z_.]+)\}\}/g, (m, k) => { if (!(k in v) || v[k] === undefined || v[k] === "") throw new Error("no value for " + k); return String(v[k]); });
for (const w of [/\bdoor\b/i, /A\.I\.mo/, /Claude/, /ChatGPT/, /—/]) if (w.test(html.replace(/<style[\s\S]*?<\/style>/, ""))) throw new Error("the deck says " + w);
const built = path.join(here, "deck.built.html");
await writeFile(built, html);

const out = path.join(here, "aimo-agent-gateway.pdf");
const print = async () => {
  const b = await chromium.launch();
  try {
    const page = await (await b.newContext()).newPage();
    await page.goto("file://" + built, { waitUntil: "load" });
    await page.evaluate(() => document.fonts.ready);
    await page.pdf({ path: out, width: "1920px", height: "1080px", printBackground: true });
    if (process.env.PREVIEW) {
      await mkdir(PREVIEW_DIR, { recursive: true });
      await page.setViewportSize({ width: 1920, height: 1080 });
      const count = await page.locator(".s").count();
      for (let i = 0; i < count; i++) await page.locator(".s").nth(i).screenshot({ path: path.join(PREVIEW_DIR, `slide-${i + 1}.png`) });
      console.log("previews in", PREVIEW_DIR);
    }
  } finally { await b.close(); }
};
await retry("pdf", print);
console.log(out, JSON.stringify(v));
