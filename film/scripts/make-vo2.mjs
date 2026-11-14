#!/usr/bin/env node
// Version 2 voice-over (+ music bed) via ElevenLabs. Every number spoken is read from the product: from the
// facts the capture wrote (src/data2/facts-*.json) when they exist, otherwise from the product's API now.
// Writes <bulk>/public/audio/vo-*.mp3 and src/data2/vo.json (word timings for the captions).
//   hsec exec --only ELEVENLABS_API_KEY -- node scripts/make-vo2.mjs [--music] [--print]
import fs from 'node:fs';
import path from 'node:path';
import {fileURLToPath} from 'node:url';

const film = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const BULK = process.env.FILM_BULK || '/bulk2/runs/valio/film2';
const MAIN = process.env.FILM_MAIN || 'http://127.0.0.1:8642';
const A = process.argv.slice(2);
const D2 = path.join(film, 'src/data2');
fs.mkdirSync(D2, {recursive: true});
const facts = (n) => {
	const f = path.join(D2, `facts-${n}.json`);
	return fs.existsSync(f) ? JSON.parse(fs.readFileSync(f, 'utf8')) : null;
};
const get = async (u) => (await fetch(MAIN + u)).json();

const ONES = 'zero one two three four five six seven eight nine ten eleven twelve thirteen fourteen fifteen sixteen seventeen eighteen nineteen'.split(' ');
const TENS = ['', '', 'twenty', 'thirty', 'forty', 'fifty', 'sixty', 'seventy', 'eighty', 'ninety'];
const words = (n) => {
	n = Math.round(n);
	if (n < 20) return ONES[n];
	if (n < 100) return TENS[Math.floor(n / 10)] + (n % 10 ? '-' + ONES[n % 10] : '');
	if (n < 1000) return ONES[Math.floor(n / 100)] + ' hundred' + (n % 100 ? ' and ' + words(n % 100) : '');
	if (n < 1e6) return words(Math.floor(n / 1000)) + ' thousand' + (n % 1000 ? (n % 1000 < 100 ? ' and ' : ' ') + words(n % 1000) : '');
	return String(n);
};
const cap = (s) => s[0].toUpperCase() + s.slice(1);

// --- the numbers ---
const MENU = facts('menu')?.latest ?? (await get('/api/menu/latest'));
const RUSH = facts('tower')?.rush ?? (await get('/api/rush/latest?events=0'));
const PROOF = facts('proof')?.proof ?? (await get('/api/proof'));
const SELL = facts('seller');
const HOSP = facts('tower')?.hospital ?? null;
const ANCHOR = facts('ledger')?.anchor ?? (await get('/api/anchor'));

const secs = Math.round((MENU.phases.read || 0) + (MENU.phases.plan || 0));
const bh = MENU.by_hand || {};
const half = RUSH.stock.litres_in_stock / RUSH.stock.litres_asked;
const person = RUSH.outcomes.needed_a_person;
const firstRun = (PROOF.runs || []).find((r) => r.unsafe > 0);
const caught = firstRun ? firstRun.unsafe : 0;
const now = PROOF.totals.orders_breaking_a_rule;
// what the refused hospital agent tried, from the rule's own sentence ("X contains soybeans, which is excluded.")
const what = (HOSP?.why || '').match(/contains ([^,]+), which is excluded/)?.[1];
const soy = /soy/i.test(what || 'soy');
const sellLine = {
	cutoff: "Monday's usual basket is not ordered yet.",
	drift: 'Your milk use is climbing. Raise the standing order.',
	shortage: 'A shortage is coming. Here is a replacement.',
	short_dated: 'Stock close to its date, at a lower price.',
	fill: 'A few more units make a full case.',
}[SELL?.kind || 'cutoff'];

// [id, text]. The film places each line (src/v2/timeline.ts).
const LINES = [
	['h1', `Saturday's wedding. ${cap(words(MENU.guests))} guests. The chef takes a photo of the menu.`],
	['h2', `${cap(words(secs))} seconds later it is a checked order. ${cap(words(MENU.line_count))} lines, one tap from a person.`],
	['n1', bh.basis === 'measured' ? `By hand, the same order took ${words(bh.minutes)} minutes. We timed it.` : `By hand, the same order is about ${words(Math.round(bh.minutes / 5) * 5)} minutes. That is our estimate.`],
	['n2', `Valio Aimo's challenge says why this matters. AI agents are starting to order, and whoever defines the door owns the customer.`],
	['r1', `Now the whipping cream runs short. The sauce gets cooking cream, because it is only heated. The whipped topping keeps the real cream.`],
	['s1', `Valio Aimo's own agent speaks up too. ${sellLine} A signed proposal, and one thumb approves it.`],
	['u1', `Monday morning. ${cap(words(RUSH.agents_total))} kitchens' agents ask for the same lactose-free milk at the same second. ${half < 0.5 ? 'There is less than half of it.' : 'There is not enough.'}`],
	['u2', `The gateway splits it by contract share. ${cap(words(RUSH.seconds.first_request_to_last_order))} seconds. ${cap(words(person))} of ${words(RUSH.agents_total)} needed a person. ${RUSH.stock.over_allocated ? '' : 'Not one litre was promised twice.'}`],
	['t1', `A hospital kitchen's bot reaches for a ${soy ? 'soy' : 'substitute'} drink. The kitchen excludes ${soy ? 'soy' : 'it'}. Refused, and no person can override that.`],
	['t2', `Change one cent in a signed order, and the check fails, right in the browser.`],
	['t3', ANCHOR?.latest ? `Every step sits on a ledger your browser can verify, with its head on a public chain.` : `Every step sits on a ledger that your own browser can verify.`],
	['p1', caught && now === 0 ? `We tried to break it first. ${cap(words(PROOF.totals.models))} models, ${words(PROOF.totals.runs)} test runs. They caught a bug in our own allergen rule. ${cap(words(caught))} orders got through. We fixed it. Now: zero.` : `We tried to break it first. ${cap(words(PROOF.totals.models))} models, ${words(PROOF.totals.runs)} test runs. Orders that broke a rule: ${words(now)}.`],
	['a1', `An unvetted bot signs its own mandate. Refused, and the gateway suspends it on its own.`],
	['a2', `An agent only ever hears its own answer. Never the stock count.`],
	['y1', `Any agent can come through. Paste one address into Claude or ChatGPT, and it orders under the same rules.`],
	['y2', `Open standards, not one platform's black box.`],
	['c1', `Valio Aimo. Readable by any agent. Owned by Valio.`],
];

if (A.includes('--print')) {
	for (const l of LINES) console.log(`${l[0].padEnd(4)} ${l[1]}`);
	process.exit(0);
}

const KEY = process.env.ELEVENLABS_API_KEY;
if (!KEY) throw new Error('ELEVENLABS_API_KEY missing (run through hsec exec)');
const voice = 'nPczCjzI2devNBz1zQrb'; // "Brian" premade, as in version 1
const out = path.join(BULK, 'public/audio');
fs.mkdirSync(out, {recursive: true});
const lines = {};
const hash = (s) => [...s].reduce((h, c) => (h * 31 + c.charCodeAt(0)) >>> 0, 7).toString(36);
for (const [id, text] of LINES) {
	const base = `vo-${hash(voice + text)}`;
	const mp3 = path.join(out, `${base}.mp3`);
	const meta = path.join(out, `${base}.json`);
	let m = fs.existsSync(meta) && fs.existsSync(mp3) ? JSON.parse(fs.readFileSync(meta, 'utf8')) : null;
	if (!m) {
		const r = await fetch(`https://api.elevenlabs.io/v1/text-to-speech/${voice}/with-timestamps?output_format=mp3_44100_128`, {
			method: 'POST',
			headers: {'xi-api-key': KEY, 'content-type': 'application/json'},
			body: JSON.stringify({text, model_id: 'eleven_multilingual_v2', voice_settings: {stability: 0.5, similarity_boost: 0.8, style: 0.2, use_speaker_boost: true, speed: 1.08}}),
		});
		if (!r.ok) throw new Error(`TTS ${r.status}: ${(await r.text()).slice(0, 300)}`);
		const j = await r.json();
		fs.writeFileSync(mp3, Buffer.from(j.audio_base64, 'base64'));
		const al = j.alignment;
		const ws = [];
		let cur = null;
		al.characters.forEach((ch, k) => {
			if (/\s/.test(ch)) {
				if (cur) ws.push(cur);
				cur = null;
				return;
			}
			if (!cur) cur = {w: '', s: al.character_start_times_seconds[k], e: 0};
			cur.w += ch;
			cur.e = al.character_end_times_seconds[k];
		});
		if (cur) ws.push(cur);
		m = {text, voice, dur: al.character_end_times_seconds.at(-1), words: ws};
		fs.writeFileSync(meta, JSON.stringify(m));
	}
	console.log(`${id} (${m.dur.toFixed(2)} s): ${text}`);
	lines[id] = {id, text, file: `audio/${base}.mp3`, dur: m.dur, words: m.words};
}

const mf = path.join(out, 'music2.mp3');
if (A.includes('--music') && !fs.existsSync(mf)) {
	const r = await fetch('https://api.elevenlabs.io/v1/music?output_format=mp3_44100_128', {
		method: 'POST',
		headers: {'xi-api-key': KEY, 'content-type': 'application/json'},
		body: JSON.stringify({
			prompt: 'Confident modern product film score, instrumental, 104 BPM, warm and bright. It starts playing at once with a light plucked motif and soft pulse, no slow intro. At 3 s a clean lift: warm analog synth arpeggio, soft kick, light shaker. Steady and purposeful through 35 s. At 36 s a bigger reveal: wide pads, fuller drums, a rising bass line, building until 50 s. From 50 to 80 s tighter and more focused, crisp percussion, a little tension. From 80 s it opens up again, hopeful. A warm, resolved final chord at 108 s that rings out. No vocals. Premium, understated, not cheesy, leaves room for a voice-over.',
			music_length_ms: 118000,
			force_instrumental: true,
		}),
	});
	if (!r.ok) throw new Error(`music ${r.status}: ${(await r.text()).slice(0, 300)}`);
	fs.writeFileSync(mf, Buffer.from(await r.arrayBuffer()));
	console.log('music written');
}
// the numbers the voice used, for the film's own on-screen figures and the report
const used = {
	menu: {guests: MENU.guests, lines: MENU.line_count, seconds: Number(((MENU.phases.read || 0) + (MENU.phases.plan || 0)).toFixed(1)), by_hand: bh, order: MENU.order?.label, run: MENU.run},
	rush: {id: RUSH.id, agents: RUSH.agents_total, asked: RUSH.stock.litres_asked, stock: RUSH.stock.litres_in_stock, ordered: RUSH.stock.litres_ordered, over_allocated: RUSH.stock.over_allocated, person, refused: RUSH.outcomes.refused, seconds: RUSH.seconds.first_request_to_last_order, alone: RUSH.placed_alone, override: RUSH.refusals_a_person_could_override},
	proof: {id: PROOF.id, models: PROOF.totals.models, providers: PROOF.totals.providers, runs: PROOF.totals.runs, orders: PROOF.totals.orders, caught, now},
	anchor: ANCHOR?.latest ?? null,
};
fs.writeFileSync(path.join(D2, 'vo.json'), JSON.stringify({voice, music: fs.existsSync(mf) ? 'audio/music2.mp3' : null, used, lines}, null, 1));
console.log(`vo.json: ${Object.keys(lines).length} lines, music ${fs.existsSync(mf)}`);
