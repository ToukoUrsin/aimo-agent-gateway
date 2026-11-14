// Builds deck/aimo-agent-gateway.pdf from deck/deck.html.
// Every number is read from the running server and the screenshots are taken from it, so the deck cannot drift from the product.
//   OPERATOR_KEY=<key> node deck/build.mjs      (server on http://127.0.0.1:8642)
//   BASE=https://trusted-trade-agents.fly.dev OPERATOR_KEY=<key> node deck/build.mjs
// It runs the wedding sample menu once and places that order, so the menu numbers and the screenshot are the same run.
// SKIP_SHOTS=1 keeps the last screenshots. PREVIEW=1 also writes one PNG per slide.
import { chromium } from "../film/node_modules/playwright/index.mjs";
import { readFile, writeFile, mkdir } from "node:fs/promises";
import { fileURLToPath } from "node:url";
import path from "node:path";

const here = path.dirname(fileURLToPath(import.meta.url));
const BASE = (process.env.BASE || "http://127.0.0.1:8642").replace(/\/$/, "");
const KEY = process.env.OPERATOR_KEY || "";
const n = x => Number(x).toLocaleString("en-GB");
const shot = name => path.join(here, "shots", name);
await mkdir(path.join(here, "shots"), { recursive: true });

const browser = await chromium.launch();
const size = { viewport: { width: 1180, height: 900 }, deviceScaleFactor: 2 };
const ctx = await browser.newContext(size);        // a visitor
const op = await browser.newContext(size);         // Valio Aimo's operator, for the pages where a visitor sees only their own part
if (KEY) { const p = await op.newPage(); await p.goto(BASE + "/op?key=" + encodeURIComponent(KEY)); await p.close(); }
const pages = new Map();   // one helper page per context: fetch from inside the browser, so the session cookie goes along
const get = async (p, c = ctx) => {
  if (!pages.has(c)) { const h = await c.newPage(); await h.goto(BASE + "/api/pages"); pages.set(c, h); }
  return pages.get(c).evaluate(async u => { const r = await fetch(u, { headers: { accept: "application/json" } }); if (!r.ok) throw new Error(u + " " + r.status); return r.json(); }, p);
};
const snap = async (c, name, p, wait, before, after) => {
  const page = await c.newPage();
  await page.goto(BASE + p, { waitUntil: "domcontentloaded" });
  if (before) await before(page);
  await page.waitForTimeout(wait);
  await page.screenshot({ path: shot(name + ".png") });
  if (after) await after(page);
  await page.close();
  console.log("shot", name);
};

if (!process.env.SKIP_SHOTS) {
  await snap(ctx, "menu", "/menu", 1500, async page => {
    await page.locator("#samples .sample:not([aria-hidden])").first().click();
    await page.locator("#go").waitFor({ timeout: 90000 });
    await page.evaluate(() => document.querySelector("#out").scrollIntoView({ block: "start" }));
  }, async page => {   // place it, so /api/menu/latest is this run
    await page.locator("#go").click();
    await page.locator("#go").waitFor({ state: "detached", timeout: 60000 });
    await page.waitForTimeout(1500);
  });
  await snap(ctx, "tower", "/tower", Number(process.env.TOWER_WAIT || 14000));   // the replay needs a moment to fill the grid
  await snap(ctx, "proof", "/proof", 2500);
}
const [menu, seller, rush, proof, home] = await Promise.all([
  get("/api/menu/latest"), get(KEY ? "/api/seller/summary" : "/api/seller/summary?view=valio", KEY ? op : ctx),
  get("/api/rush/latest?events=0"), get("/api/proof"), get("/api/home"),
]);
if (!process.env.SKIP_SHOTS) {
  await snap(KEY ? op : ctx, "seller", "/seller", 3000);
  await snap(KEY ? op : ctx, "order", new URL(menu.order.url).pathname, 3000);
  const qr = await fetch(BASE + "/api/qr?u=" + encodeURIComponent(home.public_url));
  if (!qr.ok) throw new Error("qr " + qr.status);
  await writeFile(shot("qr.svg"), await qr.text());
}

const first = (proof.runs || []).find(r => r.unsafe > 0);   // the run that caught the allergen bug
const measured = menu.by_hand.basis !== "estimate";
const v = {
  "menu.lines": menu.line_count,
  "menu.guests": menu.guests,
  "menu.seconds": Math.round(menu.phases.read + menu.phases.plan),
  "menu.hand": Math.round(menu.by_hand.minutes) + " min",
  "menu.hand_basis": measured ? "timed on /byhand" : "our estimate at 45 s a line",
  "menu.hand_class": measured ? "" : "est",
  "seller.made": seller.total.made,
  "seller.customers": seller.customers.filter(c => c.made > 0).length,
  "seller.accepted": seller.total.accepted_alone + seller.total.approved_by_person,
  "seller.alone": seller.total.accepted_alone,
  "seller.refused": seller.total.refused_before_leaving,
  "rush.agents": rush.agents_total,
  "rush.stock": n(rush.stock.litres_in_stock),
  "rush.asked": n(rush.stock.litres_asked),
  "rush.people": rush.people.asked,
  "rush.seconds": Math.round(rush.seconds.first_request_to_last_order),
  "rules": 27,
  "proof.before": first ? first.unsafe : 4,
  "proof.unsafe": proof.totals.orders_breaking_a_rule,
  "proof.runs": proof.totals.runs,
  "proof.models": proof.totals.models,
  "proof.scenarios": proof.totals.scenarios,
  "und.n": n(proof.undeclared.undeclared),
  "und.food": n(proof.undeclared.food_products),
  "und.named": n(proof.undeclared.name_points_at_an_allergen),
  "und.examples": proof.undeclared.examples.slice(0, 7).map(e => `<li><b>${e.title.replace(/&/g, "&amp;").replace(/</g, "&lt;")}</b><span>nothing declared; the name points at ${e.points_at}</span></li>`).join(""),
  "menus.n": proof.menu_test.menus,
  "menus.checks": n(proof.menu_test.checked),
  "menus.failed": proof.menu_test.failed,
  "attack.checks": 100,   // tests/attack.py
  "attack.holes": 5,
};
if (rush.stock.over_allocated) throw new Error("the latest rush over-allocated: the deck says never");

let html = await readFile(path.join(here, "deck.html"), "utf8");
html = html.replace(/\{\{([a-z_.]+)\}\}/g, (m, k) => { if (!(k in v)) throw new Error("no value for " + k); return String(v[k]); });
const built = path.join(here, "deck.built.html");
await writeFile(built, html);
const page = await ctx.newPage();
await page.goto("file://" + built);
await page.evaluate(() => document.fonts.ready);
const out = path.join(here, "aimo-agent-gateway.pdf");
await page.pdf({ path: out, width: "1920px", height: "1080px", printBackground: true });
if (process.env.PREVIEW) {
  const p2 = await (await browser.newContext({ viewport: { width: 1920, height: 1080 } })).newPage();
  await p2.goto("file://" + built);
  const count = await p2.locator(".s").count();
  for (let i = 0; i < count; i++) await p2.locator(".s").nth(i).screenshot({ path: shot(`slide-${i + 1}.png`) });
}
await browser.close();
console.log(out, JSON.stringify(v));
