// Shared helpers. No dependencies.
const $ = (s, r = document) => r.querySelector(s);
const esc = s => String(s ?? "").replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const eur = c => "€" + ((c || 0) / 100).toLocaleString("en-IE", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
const eur0 = c => "€" + Math.round((c || 0) / 100).toLocaleString("en-IE");
const nice = s => String(s || "").replace(/_/g, " ");
const day = d => d ? new Date(d + "T12:00:00").toLocaleDateString("en-GB", { weekday: "long", day: "numeric", month: "long" }) : "";
// All times are shown in Valio Aimo's time zone, whatever the viewer's device is set to.
const TZ = "Europe/Helsinki";
const clock = iso => iso ? new Date(iso).toLocaleTimeString("en-GB", { hour: "2-digit", minute: "2-digit", timeZone: TZ }) : "";
// A link the server built (it may name another host, and carries the order's key): keep it on this site.
const siteUrl = u => { try { const x = new URL(u, location.href); return x.pathname + x.search + x.hash; } catch (e) { return u || ""; } };
const when = iso => iso ? new Date(iso).toLocaleString("en-GB", { day: "numeric", month: "short", year: "numeric", hour: "2-digit", minute: "2-digit", timeZone: TZ }) : "";

// One call to the gateway: { status, headers, body }. A connection that drops on the way (a phone changing networks, a tunnel
// hiccup) is tried again by itself. A POST carries one key for all its attempts, and the gateway does the work once for that key:
// a retry gets the first attempt's answer, also when the first attempt did reach the gateway and only the answer was lost.
async function call(path, body, method) {
  const m = method || (body ? "POST" : "GET");
  const headers = { "content-type": "application/json", accept: "application/json" };
  if (m === "POST") headers["idempotency-key"] = (crypto.randomUUID ? crypto.randomUUID() : Date.now() + "-" + Math.random().toString(36).slice(2));
  const payload = body ? JSON.stringify(body) : undefined;
  let last;
  for (let i = 0; i < 5; i++) {
    if (i) await new Promise(ok => setTimeout(ok, [0, 300, 800, 1500, 2500][i]));
    try {
      const r = await fetch(path, { method: m, headers, body: payload });
      if ([502, 503, 504].includes(r.status) && !(r.headers.get("content-type") || "").includes("json")) { last = new Error("The gateway is not answering (" + r.status + ")."); continue; }
      return { status: r.status, headers: r.headers, body: await r.json() };
    } catch (e) { last = e; }
  }
  throw last;
}
const api = async (path, body, method) => (await call(path, body, method)).body;

// ---- Drawn icons, one stroke weight. Use as innerHTML.
const ICON = {
  check: '<svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="m3.5 8.5 3 3 6-7"/></svg>',
  cross: '<svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" aria-hidden="true"><path d="m4.5 4.5 7 7m0-7-7 7"/></svg>',
  arrow: '<svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M3 8h10M9 4l4 4-4 4"/></svg>',
  out: '<svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M6 3H3.5v9.5H13V10M9 3h4v4M13 3 7.5 8.5"/></svg>',
  copy: '<svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linejoin="round" aria-hidden="true"><rect x="5.5" y="5.5" width="8" height="8" rx="1.5"/><path d="M10.5 3.5v-.5a1 1 0 0 0-1-1H3a1 1 0 0 0-1 1v6.5a1 1 0 0 0 1 1h.5"/></svg>',
  menu: '<svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" aria-hidden="true"><path d="M2.5 5h11M2.5 11h11"/></svg>',
  close: '<svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" aria-hidden="true"><path d="m4 4 8 8m0-8-8 8"/></svg>',
  play: '<svg viewBox="0 0 16 16" fill="currentColor" aria-hidden="true"><path d="M5 3.5v9l7.5-4.5z"/></svg>',
  mark: '<svg viewBox="0 0 32 32" aria-hidden="true"><rect width="32" height="32" rx="8" fill="currentColor"/><path d="M10 23V10h12v13" fill="none" stroke="var(--bg)" stroke-width="3" stroke-linecap="round" stroke-linejoin="round"/></svg>',
};
// A round pass / fail / waiting mark.
const markHtml = ok => `<span class="mark ${ok === null ? "wait" : ok ? "ok" : "bad"}">${ok === null ? '<span class="spin" style="width:10px;height:10px"></span>' : ok ? ICON.check : ICON.cross}</span>`;
// Placeholder while data loads.
const skel = (w = "5ch") => `<span class="skel" style="width:${w}"></span>`;
// A wait in words: what the page is waiting for, never a bare grey bar.
const waitHtml = text => `<span class="wait"><span class="spin"></span><span>${esc(text)}</span></span>`;

// ---- The one header every page shares. A page puts <header id="site"></header> where it goes;
// anything already inside it (a page's own buttons) moves to the right-hand side.
// Six places in the order the story goes: the kitchen's chat in Valio Aimo's own web shop, the customer's own purchasing
// software through the same door, what Valio Aimo lets an agent see, then Valio Aimo's side. Everything else is one click deeper.
// /chat (a chat assistant's cards) still works but is not on show: an outside assistant is one caller among many.
const SITE_NAV = [
  ["/shop", "Chat in the web shop"],
  ["/demo", "The customer's own software"],
  ["/access", "What outside software may see"],
  ["/seller", "Valio Aimo's proposals"],
  ["/tower", "Control tower"],
  ["/proof", "Proof"],
];
const SITE_MORE = [
  ["/menu", "Order from a menu photo"],
  ["/byhand", "An order by hand"],
  ["/spec", "How it works"],
  ["/architecture", "Architecture"],
  ["/ledger", "Ledger"],
  ["/demo#connect", "Connect outside software"],
  ["/docs", "REST API"],
];
// Pages that may not exist yet are shown only when the server says they are live (GET /api/pages).
const OPTIONAL = new Set(["/menu", "/seller", "/tower", "/ledger"]);
let pagesAsk = null;
function livePages() {
  // One question per page load; a failed one is asked again next time.
  return pagesAsk ||= (async () => {
    const r = await fetch("/api/pages", { headers: { accept: "application/json" } });
    if (!r.ok) throw new Error("pages");
    const live = Object.fromEntries(((await r.json()).live || []).map(p => [p, true]));
    try { sessionStorage.setItem("aimo-pages", JSON.stringify({ t: Date.now(), live })); } catch (e) { }
    return live;
  })().catch(e => { pagesAsk = null; throw e; });
}
function mountSite(el) {
  const path = location.pathname.replace(/\/+$/, "") || "/";
  // An order, its approval and the by-hand comparison belong to the place they came from.
  const cur = href => href === "/" ? path === "/" : path === href || path.startsWith(href + "/") || (href === "/demo" && /^\/(orders|approve)\//.test(path));
  // Until the server answers, show what this tab saw last time, or the pages that always exist.
  let live = { "/tower": true, "/ledger": true };
  try { live = (JSON.parse(sessionStorage.getItem("aimo-pages") || "null") || {}).live || live; } catch (e) { }
  const own = [...el.childNodes];
  const on = n => !OPTIONAL.has(n[0]) || live[n[0]];
  const link = ([href, label]) => `<a href="${href}"${cur(href) && !href.includes("#") ? ' aria-current="page"' : ""}>${esc(label)}</a>`;
  const links = () => SITE_NAV.filter(on).map(link).join("");
  const more = () => SITE_MORE.filter(on).map(link).join("");
  const sheetHtml = () => `<div class="grp main">${links()}</div><div class="grp more">${link(["/", "Home"])}${more()}</div>`;
  el.classList.add("site");
  el.innerHTML = `<div class="in"><a class="brand" href="/" aria-label="Aimo Agent Gateway, home">${ICON.mark}<span>Aimo Agent Gateway</span></a><nav aria-label="Main">${links()}</nav><div class="acts"><button class="btn ghost small menu-btn" type="button" aria-expanded="false" aria-controls="site-sheet"><span class="ic">${ICON.menu}</span><span class="lm">Menu</span><span class="ld">More</span></button></div></div><div class="sheet" id="site-sheet" hidden>${sheetHtml()}</div>`;
  const acts = $(".acts", el), btn = $(".menu-btn", el), sheet = $(".sheet", el);
  for (const n of own) acts.insertBefore(n, btn);
  const set = open => { sheet.hidden = !open; btn.setAttribute("aria-expanded", String(open)); $(".ic", btn).innerHTML = open ? ICON.close : ICON.menu; $(".lm", btn).textContent = open ? "Close" : "Menu"; };
  btn.onclick = () => set(sheet.hidden);
  document.addEventListener("keydown", e => { if (e.key === "Escape" && !sheet.hidden) { set(false); btn.focus(); } });
  document.addEventListener("click", e => { if (!sheet.hidden && !el.contains(e.target)) set(false); });
  sheet.addEventListener("click", e => { if (e.target.closest("a")) set(false); });
  // The footer every page shares: the same places, and the ones that left the top bar.
  let foot = null;
  if (!/^\/(approve|enrol|oauth|connect)(\/|$)/.test(path) && !document.getElementById("sitefoot")) {
    foot = document.createElement("footer"); foot.id = "sitefoot"; foot.className = "sitefoot";
    document.body.appendChild(foot);
  }
  const footHtml = () => `<div class="in"><div class="fb"><a class="brand" href="/">${ICON.mark}<span>Aimo Agent Gateway</span></a><p>Junction 2026, Valio Aimo's challenge "Trusted Trade Agents". A prototype on the challenge's data and mock price and availability API.</p><p><a href="https://github.com/ToukoUrsin/aimo-agent-gateway" rel="noopener">Source code (MIT)</a></p></div><nav aria-label="Pages"><b>Pages</b>${link(["/", "Home"])}${links()}</nav><nav aria-label="More"><b>More</b>${more()}</nav><nav aria-label="For agents and developers"><b>For agents</b><a href="/.well-known/ucp">/.well-known/ucp</a><a href="/openapi.json">/openapi.json</a><a href="/spec#standards">Open standards used</a><a href="/spec#rules">The rules</a></nav></div>`;
  if (foot) foot.innerHTML = footHtml();
  livePages().then(l => { if (JSON.stringify(l) === JSON.stringify(live)) return; live = l; $("nav", el).innerHTML = links(); sheet.innerHTML = sheetHtml(); if (foot) foot.innerHTML = footHtml(); }).catch(() => { });
}
(function boot() {
  if (!document.querySelector("link[rel~=icon]")) {
    const l = document.createElement("link"); l.rel = "icon"; l.type = "image/svg+xml"; l.href = "/static/favicon.svg"; document.head.appendChild(l);
  }
  const s = document.getElementById("site");
  if (s && !s.classList.contains("site")) mountSite(s);
})();

// Copy a field's value; the button says it worked.
async function copyFrom(input, btn) {
  const was = btn.innerHTML;
  try { await navigator.clipboard.writeText(input.value); btn.innerHTML = ICON.check + "<span>Copied</span>"; }
  catch (e) { input.select(); btn.innerHTML = "<span>Press Ctrl+C</span>"; }
  setTimeout(() => { btn.innerHTML = was; }, 1600);
}

// Small, safe markdown: bold, bullets, paragraphs.
function md(text) {
  const out = [];
  let list = false;
  for (const raw of String(text || "").split("\n")) {
    const line = esc(raw).replace(/\*\*(.+?)\*\*/g, "<b>$1</b>").replace(/`(.+?)`/g, '<span class="mono">$1</span>');
    const m = line.match(/^\s*[-*•]\s+(.*)/);
    if (m) { if (!list) { out.push("<ul>"); list = true; } out.push("<li>" + m[1] + "</li>"); continue; }
    if (list) { out.push("</ul>"); list = false; }
    if (line.trim()) out.push("<p>" + line + "</p>");
  }
  if (list) out.push("</ul>");
  return out.join("");
}

// ---- Verification in the browser: JCS (RFC 8785) + ES256 detached JWS, the same thing the gateway signs.
function jcs(v) {
  if (v === null || typeof v !== "object") return JSON.stringify(v);
  if (Array.isArray(v)) return "[" + v.map(jcs).join(",") + "]";
  return "{" + Object.keys(v).sort().map(k => JSON.stringify(k) + ":" + jcs(v[k])).join(",") + "}";
}
const utf8 = s => new TextEncoder().encode(s);
function b64u(bytes) {
  let s = "";
  for (let i = 0; i < bytes.length; i += 0x8000) s += String.fromCharCode.apply(null, bytes.subarray(i, i + 0x8000));
  return btoa(s).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
}
const b64d = s => Uint8Array.from(atob(s.replace(/-/g, "+").replace(/_/g, "/")), c => c.charCodeAt(0));
async function ecKey(jwk) {
  return crypto.subtle.importKey("jwk", { kty: jwk.kty, crv: jwk.crv, x: jwk.x, y: jwk.y }, { name: "ECDSA", namedCurve: "P-256" }, false, ["verify"]);
}
async function verifyBytes(jwk, signingInput, sigB64) {
  try { return await crypto.subtle.verify({ name: "ECDSA", hash: "SHA-256" }, await ecKey(jwk), b64d(sigB64), utf8(signingInput)); } catch (e) { return false; }
}
async function verifyDetached(obj, jws, jwk) {
  if (!jws || !jwk) return false;
  const [h, mid, sig] = jws.split(".");
  if (mid !== "") return false;
  return verifyBytes(jwk, h + "." + b64u(utf8(jcs(obj))), sig);
}
async function verifyJwt(token, jwk) {
  if (!token || !jwk) return null;
  const [h, p, sig] = token.split(".");
  if (!(await verifyBytes(jwk, h + "." + p, sig))) return null;
  return JSON.parse(new TextDecoder().decode(b64d(p)));
}
async function sha256hex(bytes) {
  const d = new Uint8Array(await crypto.subtle.digest("SHA-256", bytes));
  return Array.from(d, b => b.toString(16).padStart(2, "0")).join("");
}
const without = (o, ...keys) => Object.fromEntries(Object.entries(o).filter(([k]) => !keys.includes(k)));

// ---- The ledger's hash chain, recomputed here. Works for both shapes the gateway has served:
// hash = sha256(prev + JCS(body)), and hash = sha256(prev + digest) with digest = sha256(JCS(body)),
// where an entry withheld from this reader carries only its digest. Returns one verdict per entry:
// {link: the entry chains onto the one before, body: its content matches its digest (null if withheld)}.
const ledgerBody = e => ({ ts: e.ts, ref: e.ref, cid: e.cid, actor: e.actor, kind: e.kind, text: e.text, data: e.data });
async function verifyChain(events, genesis, onProgress) {
  const out = [];
  let prev = null, firstBad = null;
  for (const [i, e] of events.entries()) {
    // The first entry in view chains onto genesis only if it is the first entry ever; otherwise onto what it names.
    const expect = prev ?? (e.seq === 1 || e.seq === undefined ? (genesis || "0".repeat(64)) : e.prev);
    let link, body = null;
    if ("digest" in e) {
      if (!e.withheld) body = await sha256hex(utf8(jcs(ledgerBody(e)))) === e.digest;
      link = e.prev === expect && await sha256hex(utf8(expect + e.digest)) === e.hash;
    } else {
      const b = utf8(jcs(ledgerBody(e))), p = utf8(expect), buf = new Uint8Array(p.length + b.length);
      buf.set(p); buf.set(b, p.length);
      link = body = e.prev === expect && await sha256hex(buf) === e.hash;
    }
    const ok = link && body !== false;
    if (!ok && firstBad === null) firstBad = i;
    out.push({ link, body, ok });
    prev = e.hash;
    if (onProgress && i % 200 === 199) { onProgress(i + 1, events.length); await new Promise(r => setTimeout(r)); }
  }
  return { results: out, firstBad, fromGenesis: !events.length || events[0].seq === 1 || events[0].seq === undefined };
}

function rulesTable(rules) {
  if (!rules || !rules.length) return "";
  const word = { pass: "pass", escalate: "needs a person", deny: "refused" };
  return '<table class="rules">' + rules.map(r => `<tr><td><span class="pill ${esc(r.outcome)}">${word[r.outcome] || esc(r.outcome)}</span></td><td><b>${esc(r.title)}</b> <span class="mono muted">${esc(r.id)}</span><div class="detail">${esc(r.detail)}</div></td></tr>`).join("") + "</table>";
}
// GS1 claim codes in plain words: FREE_FROM:LACTOSE -> "lactose-free", DIET:VEGAN -> "vegan", LOW:FAT -> "low fat".
function claimWord(c) {
  const [k, v] = String(c).split(":"), w = nice(v || k).toLowerCase();
  return k === "FREE_FROM" ? w + "-free" : k === "DIET" || !v ? w : nice(k).toLowerCase() + " " + w;
}
function claimWords(claims, max = 4) {
  const first = /LACTOSE|GLUTEN|MILK|SOY|NUT|EGG/, list = (claims || []).slice().sort((a, b) => first.test(b) - first.test(a));
  return list.slice(0, max).map(claimWord).join(", ") + (list.length > max ? ` +${list.length - max} more` : "");
}
const unitWord = u => ({ piece: "pcs", case: "cases" }[u] || u);
function linesTable(items) {
  return '<div class="lines"><div class="lhead"><span>Product</span><span class="num">Qty</span><span class="num">Unit price</span><span class="num">Ex VAT</span></div>' + items.map(l => {
    const w = l.wholesale || {}, sub = w.substitutes_for;
    const facts = [claimWords(w.claims), w.origin ? "origin " + w.origin : "", w.delivery_reliability ? "delivery reliability " + Math.round(w.delivery_reliability * 1000) / 10 + "%" : ""].filter(Boolean).join(" · ");
    return `<div class="lrow"><div class="lp"><b>${esc(l.item.title)}</b> <span class="mono muted">${esc(l.item.id)}</span>${sub ? `<div class="detail lsub">Substitute for ${esc(sub.for_title)}</div>` : ""}<div class="muted lfacts">${esc(facts)}</div></div>
      <div class="num lq">${(w.units ?? (l.quantity.total ?? l.quantity)).toLocaleString("en-IE")} ${esc(unitWord(l.item.quantity_unit.display_text))}</div><div class="num lu">${eur(l.item.price)}</div><div class="num lt">${eur(l.totals[0].amount)}</div></div>`;
  }).join("") + "</div>";
}
function totalsBlock(totals) {
  return '<table class="totals" style="margin-top:6px">' + totals.map(t => `<tr${t.type === "total" ? ' class="grand"' : ""}><td class="muted">${t.type === "subtotal" ? "Subtotal ex VAT" : t.type === "tax" ? esc(t.display_text || "VAT") : "<b>Total incl. VAT</b>"}</td><td class="num">${t.type === "total" ? "<b>" + eur(t.amount) + "</b>" : eur(t.amount)}</td></tr>`).join("") + "</table>";
}

// ---- The checks behind an order (six, or seven when an approver's device signed), run in the browser. Used by the order page and the home page.
// o: signed order, audit: /orders/{id}/audit, profile: /.well-known/ucp, led: ledger entries, genesis: ledger genesis.
// Returns [[ok, title, detail], ...].
const kidOfJws = jws => { try { return JSON.parse(new TextDecoder().decode(b64d(jws.split(".")[0]))).kid; } catch (e) { return ""; } };
async function orderChecks(o, audit, profile, led, genesis) {
  const ev = o.wholesale.evidence, out = [];
  const seller = profile.keys.find(x => x.kid === kidOfJws(o.proof.jws));

  const c1 = await verifyDetached(without(o, "proof"), o.proof.jws, seller);
  out.push([c1, "Valio Aimo signed this confirmation", c1 ? `Every field below is covered by Valio Aimo's signature. Key ${seller.kid.slice(0, 16)}… published at /.well-known/ucp.` : "The document does not match Valio Aimo's signature. It was changed after signing."]);

  const m = await verifyJwt(ev.mandate, audit.keys.buyer);
  out.push([!!m, "The buying company signed the agent's mandate", m ? `Mandate ${m.jti} from ${m.iss}: up to ${eur(m.mandate.budget.max_per_order)} per order without a person. ` + ((ev.mandate_key || {}).held_by === "buyer" ? "Signed with a key the buyer registered itself; this server holds the public half only and could not have signed it." : "Checked against the key the buyer registered at onboarding, which this server keeps for demo accounts.") : "The mandate's signature does not match the buyer's registered key."]);

  // An agent with its own key signs its acceptance. An assistant that signed in instead has no key: Valio Aimo signs a receipt of what it accepted.
  const keyless = ev.agent_class === "keyless", rc = ev.keyless_receipt;
  let acc = null;
  if (keyless) {
    if (m && rc && rc.mandate_jti === m.jti && await verifyDetached(without(rc, "proof"), rc.proof.jws, seller)) acc = { iss: rc.agent, mandate_jti: rc.mandate_jti, checkout_digest: rc.checkout_digest };
    out.push([!!acc, "The assistant accepted the offer under its mandate", acc ? `${acc.iss} signed in instead of holding a key, so it cannot sign. Valio Aimo signed a receipt of what it accepted, and gives such assistants less room (rule T1).` : "The receipt of the assistant's acceptance does not verify."]);
  } else {
    acc = m && m.cnf ? await verifyJwt(ev.agent_acceptance, m.cnf.jwk) : null;
    out.push([!!acc && acc.mandate_jti === m.jti, "The agent accepted the offer with the key named in the mandate", acc ? `Signed by ${acc.iss}. A copied mandate is useless without this key.` : "The acceptance is not signed with the agent key the mandate is bound to."]);
  }

  const dig = audit.offer ? await sha256hex(utf8(jcs(audit.offer))) : "";
  const c4 = !!acc && dig === ev.offer_digest && acc.checkout_digest === dig && await verifyDetached(audit.offer, ev.offer_signature, seller) && jcs(audit.offer.totals) === jcs(o.totals);
  out.push([c4, "Both signed exactly the same terms", c4 ? `Offer digest sha-256 ${dig.slice(0, 24)}… is what Valio Aimo signed, what the agent accepted, and its totals equal this order's.` : "The offer the agent accepted is not the offer behind this order."]);

  if (ev.approval) {
    const c5 = await verifyDetached(without(ev.approval, "signature"), ev.approval.signature.jws, audit.keys.approver);
    const dv = ev.approval.device;
    out.push([c5, `${ev.approval.by} approved it`, c5 ? `Approval recorded at ${when(ev.approval.at)}, overriding ${ev.approval.overrides.join(", ")}. ` + (dv ? "The gateway signed the record; the approver's own device signed the decision (next check)." : "No approver device was enrolled on this account, so the approval came by link and the gateway's key signed the record.") : "The approval record's signature does not verify."]);
    // A passkey: the key was made inside the approver's phone or laptop and never left it. Checked here from the record alone.
    if (dv) {
      const c5b = typeof PK !== "undefined" && await PK.verify(dv, ev.approval);
      out.push([c5b, "The approval was signed on the approver's own device", c5b ? `A passkey on ${dv.device} (key ${dv.thumbprint}) signed this order's digest, the decision, the name and the note${dv.user_verified ? ", after the device checked the person by fingerprint, face or screen lock" : ""}. Nobody without that device can make this signature: not the agent, not Valio Aimo, not this server. What it does not prove is who holds that device: in this prototype the first device that approves for an account becomes its approver and the name is typed, so a second browser of the asker's own would pass. In production the approver signs in with their own Minun Aimo account and role before a device is enrolled.` : "The device signature does not match this approval."]);
    }
  } else if (/\/shop-assistant$/.test(String(o.wholesale.placed_by || ""))) out.push([true, "No second person was needed", "The ledger records that the signed-in person confirmed this cart in the shop. Every rule passed, so no approver was asked."]);
  else out.push([true, "No person was needed", "Every guardrail passed, so the mandate alone authorised this order."]);

  const chain = await verifyChain(led, genesis);
  const bad = chain.firstBad, hidden = led.filter(e => e.withheld).length;
  out.push([bad === null, "The audit ledger has not been edited", bad === null
    ? `Recomputed ${chain.fromGenesis ? "all " + led.length + " entries" : "the latest " + led.length + " entries"} here: each link includes the previous hash and the entry digest.${hidden ? ` ${hidden} belong to other customers and are checked by their digest alone.` : ""}`
    : `The chain breaks at entry ${led[bad].seq ?? bad + 1}: that entry was changed after it was written.`]);
  return out;
}
