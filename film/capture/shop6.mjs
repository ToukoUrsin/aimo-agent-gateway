#!/usr/bin/env node
// Version 6 shots, one at a time (the challenge API behind the site is shared and rate-limited):
//   node capture/shop6.mjs wedding short approve byhand access tower proof     (all, in this order)
//   node capture/shop6.mjs approve                                             (one)
// Phones: /shop, the approval page and the order confirmation at 390 x 844, 3x, touch, Finnish.
// Valio Aimo's pages: 1440 wide desktop, English. Every number the film shows is read here, into facts.
import fs from 'node:fs';
import path from 'node:path';
import {browser, phone, desk, go, Rec, Clock, sleep, text, DATA, BASE} from './rec6.mjs';

const SHOP_TRACK = {
	banner: '.banner', tabs: '.tabs', log: '#log', me: '.msg.me', bot: '.msg.bot', cart: '.cart', ask: '.cart .ask', made: '.cart .made',
	nt: '.cart .nt', ntfirst: '.cart .nt:not(.rows *)', sum: '.cart .sum', cta: '.cart .cta', folds: '.cart .folds', rows: '.cart .rows',
	alts: '.cart .alts', appr: '.cart .appr', qr: '.cart .qr', ok: '.cart .okline', quick: '.quick', compose: '.compose', status: '.status',
};
const btn = (page, label) => page.locator('button:visible, a:visible').filter({hasText: label});
const idle = (r, name, timeout = 150000) => r.waitFor(() => !document.querySelector('.status') && document.querySelectorAll('.cart').length > 0, null, name, {timeout});
// wait for a new cart after a request: the number of carts grows and the chat goes quiet
async function newCart(r, before, name) {
	await r.page.waitForFunction((n) => document.querySelectorAll('.cart').length > n, before, {timeout: 150000, polling: 100});
	r.mark(name + ':shown');
	await r.waitFor(() => !document.querySelector('.status'), null, name);
}
const carts = (page) => page.evaluate(() => document.querySelectorAll('.cart').length);
async function openShop(win) {
	await go(win.page, '/shop', '.greet .msg');
	await sleep(1500);
}
const save = (name, facts) => fs.writeFileSync(path.join(DATA, `facts-${name}.json`), JSON.stringify({capturedAt: new Date().toISOString(), base: BASE, ...facts}, null, 1));

const SHOTS = {
	// Promise 1: one line about what the kitchen will serve becomes one cart at contract prices.
	async wedding(b) {
		const w = await phone(b);
		await openShop(w);
		const r = new Rec('wedding', w, SHOP_TRACK);
		await r.start();
		await sleep(1800);
		const n0 = await carts(w.page);
		await r.tap(btn(w.page, 'Häät lauantaina'), 'ask', {nth: 'first'});
		await newCart(r, n0, 'card');
		r.facts.banner = await text(w.page, '.banner');
		r.facts.sent = await text(w.page, '.msg.me');
		r.facts.card = await text(w.page, '.cart');
		r.facts.made = await text(w.page, '.cart .made');
		await sleep(1200);
		await r.scrollTo('.cart', 'scroll:card', {pad: 8, wait: 2600});
		await r.tap(btn(w.page, 'Näytä rivit'), 'rows', {nth: 'last'});
		await sleep(700);
		await r.scrollTo('.cart .rows', 'scroll:rows', {pad: 120, wait: 2600});
		r.facts.rows = await text(w.page, '.cart .rows');
		await r.page.evaluate(() => { const s = document.querySelector('#log'); s.scrollBy({top: 320, behavior: 'smooth'}); });
		r.mark('scroll:rows2');
		await sleep(2200);
		await r.stop();
		save('wedding', r.facts);
		await w.ctx.close();
	},
	// Promise 2: a missing product gets a replacement from Valio Aimo's own range.
	async short(b) {
		const w = await phone(b);
		await openShop(w);
		const r = new Rec('short', w, SHOP_TRACK);
		await r.start();
		await sleep(1200);
		const n0 = await carts(w.page);
		await r.tap(btn(w.page, 'Tuote loppuu kesken'), 'ask', {nth: 'first'});
		await newCart(r, n0, 'card');
		r.facts.sent = await text(w.page, '.msg.me');
		r.facts.card = await text(w.page, '.cart');
		r.facts.nt = await text(w.page, '.cart .nt');
		await sleep(600);
		await r.scrollTo('.cart', 'scroll:card', {pad: 8, wait: 2400});
		await r.tap(w.page.locator('.cart .nt button[data-act=swapopen]'), 'swapopen', {nth: 'last'});
		await sleep(900);
		r.facts.alts = await w.page.evaluate(() => [...document.querySelectorAll('.cart .alts button')].map((b) => ({text: b.innerText.trim(), current: b.getAttribute('aria-current') === 'true'})));
		await r.scrollTo('.cart .alts', 'scroll:alts', {pad: 160, wait: 1600});
		// pick the first choice that is not the current one
		// "only what is in stock" if offered (the kitchen's own call), else the next choice
		const open = w.page.locator('.cart .alts button:not([disabled])');
		const only = open.filter({hasText: /Vain saatavilla/});
		const pick = (await only.count()) ? only.first() : open.first();
		r.facts.picked = (await pick.innerText()).trim();
		const before = await text(w.page, '.cart .nt');
		await r.tap(pick, 'pick', {nth: 'first'});
		await w.page.waitForFunction((b) => { const e = [...document.querySelectorAll('.cart .nt')].pop(); return !document.querySelector('.status') && e && e.innerText.trim() !== b; }, before, {timeout: 60000, polling: 100}).catch(() => console.log('short: card did not change after the pick'));
		r.mark('picked');
		await sleep(400);
		await r.scrollTo('.cart', 'scroll:after', {pad: 8, wait: 2200});
		r.facts.after = await text(w.page, '.cart');
		await r.stop();
		save('short', r.facts);
		await w.ctx.close();
	},
	// Promise 3: nothing goes out before someone confirms. Two phones on one clock.
	async approve(b) {
		const clock = new Clock();
		const a = await phone(b);
		await openShop(a);
		const r = new Rec('approve-asker', a, SHOP_TRACK, clock);
		await r.start();
		await sleep(1000);
		const n0 = await carts(a.page);
		await r.tap(btn(a.page, '1 000 l laktoositonta'), 'ask', {nth: 'first'});
		await newCart(r, n0, 'card');
		r.facts.sent = await text(a.page, '.msg.me');
		r.facts.card = await text(a.page, '.cart');
		r.facts.appr = await text(a.page, '.cart .appr');
		r.facts.cta = await text(a.page, '.cart .cta');
		await sleep(500);
		await r.scrollTo('.cart .appr', 'scroll:appr', {pad: 230, wait: 2600});
		const url = await a.page.evaluate('Object.values(carts).map(c => c.d.continue_url).filter(Boolean).pop()');
		if (!url) throw new Error('approve: no approval link on the card');
		// the asker opens the approval link themselves: refused
		const own = await a.ctx.newPage();
		const ro = new Rec('approve-own', {page: own, w: a.w, h: a.h, scale: 3}, {main: 'main'}, clock);
		await go(own, url, 'main');
		await sleep(1500);
		await ro.start();
		await sleep(2500);
		ro.facts.text = await text(own, 'main');
		// where the refusal sits, so the film can frame it
		ro.facts.refusal_box = await own.evaluate(() => { const e = [...document.querySelectorAll('main *')].filter((x) => /^Et voi hyväksyä/.test(x.innerText || '')).sort((a, b) => a.innerText.length - b.innerText.length)[0]; const p = e && e.parentElement && e.parentElement.innerText.length < 400 ? e.parentElement : e; const r = p && p.getBoundingClientRect(); return r ? [Math.round(r.x), Math.round(r.y), Math.round(r.width), Math.round(r.height)] : null; });
		if (await own.locator('#who').count()) {
			await ro.type('#who', 'Minä itse', 'name', {delay: 30});
			await ro.tap('#yes', 'yes', {nth: 'first'});
			await sleep(2200);
			ro.facts.after = await text(own, 'main');
		}
		await ro.stop();
		await own.close();
		// the approver: a different phone
		const p = await phone(b);
		await go(p.page, url, '#who');
		await sleep(1200);
		const rp = new Rec('approve-approver', p, {main: 'main', who: '#who', yes: '#yes', no: '#no', why: '.why, .nt, [data-why]'}, clock);
		await rp.start();
		await sleep(1600);
		rp.facts.text = await text(p.page, 'main');
		await rp.type('#who', 'Maija Korhonen, ravintolapäällikkö', 'name', {delay: 38});
		await sleep(300);
		await rp.tap('#yes', 'yes', {nth: 'first'});
		await rp.waitFor(() => /Hyväksytty/.test(document.querySelector('main').innerText), null, 'approved', {timeout: 30000});
		rp.facts.after = await text(p.page, 'main');
		// the asker's card turns by itself
		await r.waitFor(() => !!document.querySelector('.cart .okline'), null, 'turned', {timeout: 60000});
		r.facts.ok = await text(a.page, '.cart .okline');
		await sleep(1800);
		await rp.stop();
		await p.ctx.close();
		await r.scrollTo('.cart .okline', 'scroll:ok', {pad: 300, wait: 1400});
		await r.tap(a.page.locator('.cart .cta'), 'confirm', {nth: 'last'});
		await r.waitFor(() => /Vahvistettu tilaus/.test(document.querySelector('.cart:last-of-type .hd, .cart .hd') ? [...document.querySelectorAll('.cart .hd')].pop().innerText : ''), null, 'placed', {timeout: 60000});
		r.facts.placed = await text(a.page, '.cart .hd');
		r.facts.after = await text(a.page, '.cart');
		await sleep(600);
		await r.scrollTo('.cart', 'scroll:placed', {pad: 8, wait: 2200});
		const rec = await a.page.evaluate(() => { const l = [...document.querySelectorAll('.cart a.cta')].pop(); return l ? l.href : null; });
		r.facts.record_url = rec ? rec.replace(/[?#].*$/, '') : null;
		if (rec) {
			await r.tap(a.page.locator('.cart a.cta'), 'record', {nth: 'last', settle: 0});
			// the link opens a new tab; the film shows it in the same phone
			await a.page.goto(rec, {waitUntil: 'load'}).catch(() => {});
			r.mark('record:open');
			await a.page.waitForFunction(() => /Tilaus VA-/.test(document.body.innerText), null, {timeout: 20000, polling: 100}).catch(() => {});
			r.mark('record:shown');
			await a.page.waitForFunction(() => /tarkistusta tehtiin/.test(document.body.innerText), null, {timeout: 20000, polling: 100}).catch(() => {});
			r.mark('record:checked');
			await sleep(1500);
			r.facts.record = (await a.page.evaluate(() => document.body.innerText)).slice(0, 3000);
		}
		await r.stop();
		save('approve', {asker: r.facts, own: ro.facts, approver: rp.facts});
		await a.ctx.close();
	},
	// the contrast: the same list by hand, product by product, the clock running
	async byhand(b) {
		const w = await phone(b);
		await go(w.page, '/byhand', '#q');
		await w.page.waitForFunction(() => document.querySelectorAll('#list [id^=n]').length > 3, null, {timeout: 60000}).catch(() => {});
		await w.page.evaluate(() => { const q = document.querySelector('#q'); q.scrollIntoView({block: 'center'}); });
		await sleep(1200);
		const r = new Rec('byhand', w, {watch: '#watch', clock: '#t', q: '#q', found: '#found', basket: '#basket', list: '#list'});
		await r.start();
		await sleep(800);
		r.facts.before = await text(w.page, '#watch');
		for (const [word, n] of [['lohifilee', 0], ['peruna', 0]]) {
			await r.page.fill('#q', '');
			await r.type('#q', word, 'type:' + word, {delay: 120});
			await sleep(1600);
			const add = w.page.locator('#found button:visible').nth(n);
			if (await add.count()) await r.tap(add, 'add:' + word, {nth: 'first'});
			await sleep(900);
		}
		r.facts.watch = await text(w.page, '#watch');
		r.facts.page = (await w.page.evaluate(() => document.body.innerText)).slice(0, 1500);
		await r.stop();
		save('byhand', r.facts);
		await w.ctx.close();
	},
	// Valio Aimo's switches for one customer; flip one and the answer below changes
	async access(b) {
		const w = await desk(b, {w: 1440, h: 1000});
		await go(w.page, '/access', '#ctl');
		await w.page.waitForSelector('#r-order', {timeout: 30000});
		await sleep(3000);
		await w.page.evaluate(() => { const c = document.querySelector('#ctl'); window.scrollTo(0, c.getBoundingClientRect().top + window.scrollY - 70); });
		await sleep(800);
		const r = new Rec('access', w, {ctl: '#ctl', on: '#on', rows: '#rows', look: '#r-look', plan: '#r-plan', order: '#r-order'});
		r.facts.before = (await w.page.evaluate(() => document.body.innerText)).slice(0, 6000);
		r.facts.order_before = await text(w.page, '#r-order');
		await r.start();
		await sleep(1800);
		const paused = w.page.locator('#on button[data-v="0"]').first(); // "Paused" (the button holds both languages)
		const bb = await paused.boundingBox();
		const x = Math.round(bb.x + bb.width / 2), y = Math.round(bb.y + bb.height / 2);
		r.mark('finger', {x, y, to: 'pause'});
		await sleep(450);
		r.mark('pause', {x, y, click: true});
		await paused.click();
		await w.page.waitForFunction((b) => document.querySelector('#r-order').innerText !== b, r.facts.order_before, {timeout: 30000, polling: 100}).catch(() => console.log('access: answer did not change'));
		r.mark('changed');
		await sleep(1200);
		await w.page.evaluate(() => { const c = document.querySelector('#rows'); window.scrollTo({top: c.getBoundingClientRect().top + window.scrollY - 120, behavior: 'smooth'}); });
		r.mark('scroll:answers');
		await sleep(4500);
		r.facts.order_after = await text(w.page, '#r-order');
		r.facts.after = (await w.page.evaluate(() => document.body.innerText)).slice(0, 6000);
		await r.stop();
		// put the switch back (it only touches this capture's own test account)
		const on = w.page.locator('#on button[data-v="1"]').first();
		if (await on.count()) await on.click().catch(() => {});
		save('access', r.facts);
		await w.ctx.close();
	},
	// the recorded Monday rush, as the page replays it
	async tower(b) {
		const w = await desk(b, {w: 1440, h: 1000});
		await go(w.page, '/tower', 'main');
		await sleep(3000);
		const rush = w.page.getByText(/The Monday rush|Maanantairuuhka/).first();
		await rush.scrollIntoViewIfNeeded();
		await w.page.evaluate(() => { const h = [...document.querySelectorAll('h2,h3')].find((e) => /Monday rush|Maanantairuuhka/i.test(e.innerText)); if (h) window.scrollTo(0, h.getBoundingClientRect().top + window.scrollY - 30); });
		await sleep(600);
		const r = new Rec('tower', w, {body: 'main'});
		const from = w.page.getByRole('button', {name: /From the start|Alusta/}).first();
		await r.start();
		await sleep(600);
		if (await from.count()) { const bb = await from.boundingBox(); r.mark('replay', {x: Math.round(bb.x + bb.width / 2), y: Math.round(bb.y + bb.height / 2), click: true}); await from.click(); }
		await sleep(23000);
		r.facts.text = (await w.page.evaluate(() => document.body.innerText)).slice(0, 5000);
		await r.stop();
		save('tower', r.facts);
		await w.ctx.close();
	},
	// we broke it first
	async proof(b) {
		const w = await desk(b, {w: 1440, h: 1000});
		await go(w.page, '/proof', 'main');
		await sleep(3500);
		await w.page.evaluate(() => window.scrollTo(0, 0));
		const r = new Rec('proof', w, {body: 'main', head: '.page-head', tiles: '#tiles', story: '#found .story', prose: '#found .prose', when: '#found .story .when'});
		r.facts.text = (await w.page.evaluate(() => document.body.innerText)).slice(0, 6000);
		await r.start();
		await sleep(5500);
		await w.page.evaluate(() => { const h = [...document.querySelectorAll('h2,h3')].find((e) => /failure the test found|testi löysi/i.test(e.innerText)); if (h) window.scrollTo({top: h.getBoundingClientRect().top + window.scrollY - 40, behavior: 'smooth'}); });
		r.mark('scroll:fix');
		await sleep(7500);
		await r.stop();
		save('proof', r.facts);
		await w.ctx.close();
	},
};

const names = process.argv.slice(2);
const list = names.length ? names : Object.keys(SHOTS);
const b = await browser();
let bad = 0;
for (const n of list) {
	if (!SHOTS[n]) throw new Error(`no shot ${n}; shots: ${Object.keys(SHOTS).join(' ')}`);
	for (let k = 1; k <= 2; k++) {
		try {
			console.log(`--- ${n} (${new Date().toISOString()})`);
			await SHOTS[n](b);
			break;
		} catch (e) {
			console.log(`${n} failed (try ${k}): ${e.message.split('\n')[0]}`);
			if (k === 2) bad++;
			else await sleep(15000);
		}
	}
}
await b.close();
process.exit(bad ? 1 : 0);
