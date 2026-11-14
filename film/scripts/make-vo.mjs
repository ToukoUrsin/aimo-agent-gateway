#!/usr/bin/env node
// Voice-over (+ music bed) via ElevenLabs. Every number spoken is read from the facts the capture wrote
// (src/data/facts-*.json), so voice and screen always agree after a re-capture.
// Writes <bulk>/public/audio/vo-NN.mp3 and src/data/vo.json (word timings for the captions).
//   hsec exec --only ELEVENLABS_API_KEY -- node scripts/make-vo.mjs [--music] [--print]
import fs from 'node:fs';
import path from 'node:path';
import {fileURLToPath} from 'node:url';

const film = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const BULK = process.env.FILM_BULK || '/bulk2/runs/valio/film';
const A = process.argv.slice(2);
const facts = (n) => {
	const f = path.join(film, 'src/data', `facts-${n}.json`);
	return fs.existsSync(f) ? JSON.parse(fs.readFileSync(f, 'utf8')) : null;
};

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
const euros = (s) => Number(String(s).replace(/[€,]/g, ''));

const R = facts('reorder');
const CH = facts('challenge');
const OR = facts('order');
const RG = facts('rogue');
const TW = facts('tower');
const AF = facts('recall');
const PR = facts('proof');
if (!R || !CH || !OR || !RG) throw new Error('run the capture first (facts for reorder, challenge, order, rogue)');

// what the agent saw: plan_sourcing's options (usual product vs cheapest that covers everything)
const opt = (label) => {
	const m = (CH.sourcing || '').match(new RegExp(label + ':\\s*(€[\\d,.]+)'));
	return m ? euros(m[1]) : null;
};
const saved = opt('usual_product') && opt('lowest_price') ? Math.round(opt('usual_product') - opt('lowest_price')) : null;
const checks = OR.before.checks.length;
const refused = RG.refused.length;
const asked = CH.rules.filter((r) => r.outcome === 'escalate');

// [scene, seconds into the scene, text]
const LINES = [
	['hook', 0.35, 'Kitchens are starting to hand their buying to AI agents.'],
	['hook', 3.5, 'But a food wholesaler has no safe door to let them in.'],
	['door', 0.3, 'This is that door, for Valio Aimo. A hotel kitchen asks its own agent for the usual milk.'],
	['door', 6.6, `The agent reads live prices and stock, gets a signed offer, and orders. ${cap(words(R.seconds))} seconds, and nobody had to step in.`],
	['challenge', 0.3, `Now the challenge's own example: a thousand litres of lactose-free milk by Monday.`],
	['challenge', 5.6, saved ? `The gateway hands the agent ready options. It picks one that saves ${words(saved)} euros.` : 'The gateway hands the agent ready options, and it picks the cheapest.'],
	['challenge', 11.4, asked.some((r) => r.id === 'N1') ? 'But the hotel has never bought that product, so a rule asks for a person.' : 'But it is outside the limits, so a rule asks for a person.'],
	['challenge', 16.6, `The manager taps approve on a phone, and the order is placed.`],
	['trust', 0.3, `Every order page checks ${words(checks)} signatures, right in the browser.`],
	['trust', 4.6, 'Lower one price by a cent, and the check fails.'],
	['trust', 8.0, 'The rules run in code. A model never decides what is allowed.'],
	['control', 0.3, `Now an unvetted bot tries ${words(refused)} ways around the rules.`],
	['control', 3.7, `${RG.notRefused === 0 ? `All ${words(refused)} are refused` : 'They are refused'}${RG.cut ? ', and the gateway suspends it on its own' : ''}.`],
	['control', 8.2, 'And Valio Aimo can switch agent ordering off for any customer.'],
];
if (TW) {
	const fs_ = TW.floor.summary;
	LINES.push(
		['tower', 0.3, "Valio Aimo watches every customer's agents on one screen."],
		['tower', 3.8, `Here, simulated customers order all at once. In ${words(Math.round(fs_.seconds / 60))} minutes, ${words(fs_.orders)} orders, and ${words(fs_.alone)} needed nobody.`],
	);
}
if (AF && AF.recall && AF.issue && AF.final) {
	const replaced = AF.recall.states.includes('replaced');
	LINES.push(
		['after', 0.3, 'After the order, a batch is recalled.'],
		['after', 2.5, "The gateway traces it to every order, blocks the product, and tells the hotel's agent."],
		['after', 7.75, replaced ? (AF.issue.by ? 'The agent picks a replacement, and the manager approves.' : 'The agent picks a replacement on its own.') : 'The agent takes a credit.'],
	);
}
if (PR) {
	const t = PR.totals;
	LINES.push(
		['proof', 0.3, `We gave the same ${words(t.scenarios)} requests to ${words(t.models)} models from ${words(t.providers)} providers.`],
		['proof', 4.4, t.orders_breaking_a_rule === 0 ? `Of ${words(t.orders)} orders, not one broke a rule.` : `${cap(words(t.orders_breaking_a_rule))} of ${words(t.orders)} orders broke a rule.`],
		['proof', 7.7, 'Any assistant can use it. Paste one address into Claude or ChatGPT.'],
	);
}
LINES.push(
	['close', 0.3, 'It is built on open standards. The way to production starts read only, with a few pilot customers.'],
	['close', 6.6, 'Aimo Agent Gateway. A safe door for agents.'],
);

if (A.includes('--print')) {
	for (const l of LINES) console.log(`${l[0].padEnd(10)} +${l[1]}s  ${l[2]}`);
	process.exit(0);
}

const KEY = process.env.ELEVENLABS_API_KEY;
if (!KEY) throw new Error('ELEVENLABS_API_KEY missing (run through hsec exec)');
const voice = A.includes('--voice') ? A[A.indexOf('--voice') + 1] : 'nPczCjzI2devNBz1zQrb'; // "Brian" premade
const out = path.join(BULK, 'public/audio');
fs.mkdirSync(out, {recursive: true});
const lines = [];
const hash = (s) => [...s].reduce((h, c) => (h * 31 + c.charCodeAt(0)) >>> 0, 7).toString(36);
for (const [scene, at, text] of LINES) {
	const base = `vo-${hash(voice + text)}`;
	const mp3 = path.join(out, `${base}.mp3`);
	const meta = path.join(out, `${base}.json`);
	let m = fs.existsSync(meta) && fs.existsSync(mp3) ? JSON.parse(fs.readFileSync(meta, 'utf8')) : null;
	if (!m) {
		const r = await fetch(`https://api.elevenlabs.io/v1/text-to-speech/${voice}/with-timestamps?output_format=mp3_44100_128`, {
			method: 'POST',
			headers: {'xi-api-key': KEY, 'content-type': 'application/json'},
			body: JSON.stringify({text, model_id: 'eleven_multilingual_v2', voice_settings: {stability: 0.5, similarity_boost: 0.8, style: 0.2, use_speaker_boost: true, speed: 1.05}}),
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
		console.log(`vo (${m.dur.toFixed(2)} s): ${text}`);
	}
	lines.push({scene, at, text, file: `audio/${base}.mp3`, dur: m.dur, words: m.words});
}

const mf = path.join(out, 'music.mp3');
if (A.includes('--music') && !fs.existsSync(mf)) {
	const r = await fetch('https://api.elevenlabs.io/v1/music?output_format=mp3_44100_128', {
		method: 'POST',
		headers: {'xi-api-key': KEY, 'content-type': 'application/json'},
		body: JSON.stringify({
			prompt: 'Cinematic, confident modern product film score, instrumental, 100 BPM. 0-8 s: low tense pulse, soft ticking, a sense of something about to open. At 8 s a clean lift: warm analog synth arpeggio, soft kick, light shaker. A steady, purposeful build through the middle with crisp percussion and a rising bass line; a tense, darker passage around 50-62 s; a bigger, brighter lift at 64 s with wide pads; calm, resolved, warm ending at 116 s with a soft final chord. No vocals. Premium, understated, not cheesy, leaves room for a voice-over.',
			music_length_ms: 122000,
			force_instrumental: true,
		}),
	});
	if (!r.ok) throw new Error(`music ${r.status}: ${(await r.text()).slice(0, 300)}`);
	fs.writeFileSync(mf, Buffer.from(await r.arrayBuffer()));
	console.log('music written');
}
fs.writeFileSync(path.join(film, 'src/data/vo.json'), JSON.stringify({voice, music: fs.existsSync(mf) ? 'audio/music.mp3' : null, lines}, null, 1));
console.log(`vo.json: ${lines.length} lines, music ${fs.existsSync(mf)}`);
