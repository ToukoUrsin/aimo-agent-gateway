// Version 6 capture helpers: Playwright WebKit (headless Chromium fails page loads on this box).
// A recorder takes screenshots of one page as fast as WebKit gives them (about 10 to 14 a second at 3x),
// each with its real time, and samples the boxes of named elements with every frame. Taps and typing are
// marked as events so the film can draw a finger. Several recorders can share one clock (two phones).
// Frames: <bulk>/public/shots/<name>/f00000.jpg. Meta: film/src/data6/shot-<name>.json (the format src/shots.ts reads).
import {webkit} from 'playwright';
import fs from 'node:fs';
import path from 'node:path';
import {fileURLToPath} from 'node:url';

export const FILM = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
export const BULK = process.env.FILM_BULK || '/bulk3/runs/valio/film6';
export const BASE = process.env.FILM_BASE || 'https://trusted-trade-agents.fly.dev';
export const DATA = path.join(FILM, 'src', 'data6');
export const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
fs.mkdirSync(DATA, {recursive: true});

export async function browser() {
	return webkit.launch();
}
// a phone: 390 x 844 CSS px, 3 device px per CSS px, touch, Finnish
export async function phone(b, {lang = 'fi', w = 390, h = 844} = {}) {
	const ctx = await b.newContext({viewport: {width: w, height: h}, deviceScaleFactor: 3, isMobile: true, hasTouch: true, locale: 'fi-FI', timezoneId: 'Europe/Helsinki', colorScheme: 'light'});
	await ctx.addInitScript((l) => {
		try {
			localStorage.setItem('shop.lang', l);
		} catch (e) {}
	}, lang);
	const page = await ctx.newPage();
	page.on('pageerror', (e) => console.log(`PAGEERROR ${e.message}`));
	return {ctx, page, w, h, scale: 3};
}
// a desktop window for Valio Aimo's pages
// (English: the pages open in Finnish and keep the visitor's FI/EN choice under the same key as /shop)
export async function desk(b, {w = 1440, h = 900, scale = 1.5, lang = 'en'} = {}) {
	const ctx = await b.newContext({viewport: {width: w, height: h}, deviceScaleFactor: scale, locale: 'en-GB', timezoneId: 'Europe/Helsinki', colorScheme: 'light'});
	await ctx.addInitScript((l) => {
		try {
			localStorage.setItem('shop.lang', l);
		} catch (e) {}
	}, lang);
	const page = await ctx.newPage();
	page.on('pageerror', (e) => console.log(`PAGEERROR ${e.message}`));
	return {ctx, page, w, h, scale};
}
// load a page, retrying while the site restarts for a deploy
export async function go(page, url, ready, {tries = 8} = {}) {
	for (let k = 1; ; k++) {
		try {
			const r = await page.goto(url.startsWith('http') ? url : BASE + url, {waitUntil: 'load', timeout: 45000});
			if (r && r.status() >= 500) throw new Error(`status ${r.status()}`);
			if (ready) await page.waitForSelector(ready, {timeout: 30000});
			return;
		} catch (e) {
			if (k >= tries) throw e;
			console.log(`load ${url}: ${e.message.split('\n')[0]}; again in 10 s`);
			await sleep(10000);
		}
	}
}

export class Clock {
	constructor() {
		this.t0 = Date.now() / 1000;
	}
	now() {
		return Number((Date.now() / 1000 - this.t0).toFixed(3));
	}
}

export class Rec {
	// track: {key: css selector}; the LAST matching element is tracked (the newest card), boxes in viewport CSS px
	constructor(name, win, track = {}, clock = new Clock(), {quality = 86} = {}) {
		Object.assign(this, {name, win, page: win.page, track, clock, quality});
		this.rel = `shots/${name}`;
		this.dir = path.join(BULK, 'public', this.rel);
		fs.rmSync(this.dir, {recursive: true, force: true});
		fs.mkdirSync(this.dir, {recursive: true});
		this.frames = [];
		this.events = [];
		this.boxT = [];
		this.boxes = Object.fromEntries(Object.keys(track).map((k) => [k, []]));
		this.facts = {};
	}
	now() {
		return this.clock.now();
	}
	async start() {
		this.running = true;
		this.loop = (async () => {
			while (this.running) {
				try {
					const t = this.now();
					const buf = await this.page.screenshot({type: 'jpeg', quality: this.quality, caret: 'initial', timeout: 8000});
					const t1 = this.now();
					const file = `f${String(this.frames.length).padStart(5, '0')}.jpg`;
					this.frames.push(Number(((t + t1) / 2).toFixed(3)));
					fs.writeFileSync(path.join(this.dir, file), buf);
					const boxes = await this.page.evaluate((track) => {
						const o = {};
						for (const [k, sel] of Object.entries(track)) {
							const all = document.querySelectorAll(sel);
							const e = all[all.length - 1];
							const r = e && e.getBoundingClientRect();
							o[k] = r && r.width && r.height ? [Math.round(r.x), Math.round(r.y), Math.round(r.width), Math.round(r.height)] : null;
						}
						return o;
					}, this.track);
					this.boxT.push(this.frames.at(-1));
					for (const k of Object.keys(this.track)) this.boxes[k].push(boxes[k]);
				} catch (e) {
					await sleep(60); // navigation in progress
				}
			}
		})();
		await sleep(400);
	}
	mark(name, extra = {}) {
		const e = {t: this.now(), name, ...extra};
		this.events.push(e);
		return e;
	}
	loc(target) {
		return typeof target === 'string' ? this.page.locator(target) : target;
	}
	// a tap the film draws: the finger lands (lead ms before the tap), then the tap
	async tap(target, name, {lead = 450, settle = 250, nth = 'last'} = {}) {
		let l = this.loc(target);
		l = nth === 'last' ? l.last() : l.first();
		await l.scrollIntoViewIfNeeded();
		await sleep(150);
		const b = await l.boundingBox();
		if (!b) throw new Error(`${this.name}: nothing to tap for ${name}`);
		const x = Math.round(b.x + b.width / 2), y = Math.round(b.y + b.height / 2);
		this.mark('finger', {x, y, to: name});
		await sleep(lead);
		this.mark(name, {x, y, click: true, box: [Math.round(b.x), Math.round(b.y), Math.round(b.width), Math.round(b.height)]});
		await l.tap();
		await sleep(settle);
	}
	async type(target, text, name, {delay = 55} = {}) {
		const l = this.loc(target).first();
		await this.tap(l, name + ':focus', {lead: 300, nth: 'first'});
		this.mark(name + ':start');
		await l.pressSequentially(text, {delay});
		this.mark(name + ':end');
	}
	async waitFor(fn, arg, name, {timeout = 150000} = {}) {
		await this.page.waitForFunction(fn, arg, {timeout, polling: 100});
		return name ? this.mark(name) : null;
	}
	// scroll the shop's chat log (or any scroller) so `sel` sits `pad` px from the top
	async scrollTo(sel, name, {scroller = '#log', pad = 10, wait = 900} = {}) {
		await this.page.evaluate(([sel, scroller, pad]) => {
			const all = document.querySelectorAll(sel), el = all[all.length - 1];
			const s = document.querySelector(scroller) || document.scrollingElement;
			const top = el.getBoundingClientRect().top - s.getBoundingClientRect().top + s.scrollTop - pad;
			s.scrollTo({top: Math.max(0, Math.min(top, s.scrollHeight - s.clientHeight)), behavior: 'smooth'});
		}, [sel, scroller, pad]);
		if (name) this.mark(name);
		await sleep(wait);
	}
	async stop() {
		await sleep(300);
		this.running = false;
		await this.loop;
		const dur = this.now();
		const meta = {name: this.name, dir: this.rel, base: BASE, capturedAt: new Date().toISOString(), css: [this.win.w, this.win.h], scale: this.win.scale, dur, frames: this.frames, events: this.events, boxT: this.boxT, boxes: this.boxes, facts: this.facts};
		fs.writeFileSync(path.join(DATA, `shot-${this.name}.json`), JSON.stringify(meta));
		const rate = this.frames.length / Math.max(0.1, dur - (this.frames[0] || 0));
		console.log(`shot ${this.name}: ${dur.toFixed(1)} s, ${this.frames.length} frames (${rate.toFixed(1)}/s), ${this.events.filter((e) => e.name !== 'finger').map((e) => `${e.name}@${e.t.toFixed(1)}`).join(' ')}`);
		return meta;
	}
}

export const text = (page, sel) => page.evaluate((sel) => {
	const all = document.querySelectorAll(sel), e = all[all.length - 1];
	return e ? e.innerText.replace(/\s+\n/g, '\n').trim() : null;
}, sel);
