#!/usr/bin/env node
// Version 6 voice-over. Every number is read from the facts the capture wrote (src/data6/facts-*.json),
// that is, from the page under it at capture time.
// Voice: the paid ElevenLabs voice ran out of credit on 14 Nov 2026, so this uses a local voice
// (Kokoro-82M "af_heart", scripts/tts6.py, run on this machine). Each caption chunk is spoken on its own
// and the chunks are joined with short pauses, so the captions know exactly when each chunk is said.
// Writes <bulk>/public/audio/v6-*.wav and src/data6/vo.json. Chunks are cached by their spoken text.
//   node scripts/make-vo6.mjs            (--print: the narration and the numbers only)
// Needs a Python with kokoro-onnx + soundfile: FILM_TTS_PY (default <bulk>/tts/venv/bin/python).
import fs from 'node:fs';
import path from 'node:path';
import {execFileSync} from 'node:child_process';
import {fileURLToPath} from 'node:url';

const film = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const BULK = process.env.FILM_BULK || '/bulk3/runs/valio/film6';
const PY = process.env.FILM_TTS_PY || path.join(BULK, 'tts/venv/bin/python');
const D = path.join(film, 'src/data6');
const A = process.argv.slice(2);
const facts = (n) => JSON.parse(fs.readFileSync(path.join(D, `facts-${n}.json`), 'utf8'));
const num = (s) => Number(String(s).replace(/[\s  ]/g, '').replace(/,(\d\d)$/, '.$1').replace(/,/g, ''));
const must = (re, s, what) => {
	const m = re.exec(String(s || '').replace(/[  ]/g, ' '));
	if (!m) throw new Error(`cannot read ${what} from: ${String(s).slice(0, 300)}`);
	return m;
};

// --- the numbers, from the pages ---
const W = facts('wedding');
const guests = num(must(/(\d+)\s*henke/, W.sent, 'guests')[1]);
const [, wLines, wSecs] = must(/Yhdestä viestistä (\d+) riviä ostoskorissa, (\d+) sekunni/, W.made, 'measured line');
const dishes = num(must(/(\d+) ruokalajia/, W.card, 'dishes')[1]);
const S = facts('short');
const [, sHave, sWant] = must(/Ei tarpeeksi saatavuutta:.*?, ([\d ,]+) \/ ([\d ,]+) l\./, S.nt, 'shortage');
const subLine = must(/Korvaava tuote:[^\n]*/, S.nt, 'replacement')[0];
const subContract = /sopimushinta/.test(subLine);
const pickedOnly = /Vain saatavilla/.test(S.picked || '');
const P = facts('approve');
const [, pOrder, pMax] = must(/Tilaus on ([\d ]+,\d\d) € \(alv 0 %\)\. Ilman hyväksyntää saat tilata enintään ([\d ]+,\d\d) €/, P.asker.appr, 'limit');
const over = Math.round(num(pOrder) - num(pMax));
const orderNo = must(/VA-AG-[\d-]+/, P.asker.placed, 'order number')[0];
const T = facts('tower');
const [, tKitchens, tWanted, tHad] = must(/(\d+) kitchens' agents asked for ([\d,]+) L[^.]*\.\s*Valio Aimo had ([\d,]+) L/, T.text, 'rush');
const PR = facts('proof');
const [, prOrders, prModels, prRuns] = must(/0\s*of ([\d,]+) orders broke a rule, across (\d+) models and ([\d,]+) runs/, PR.text, 'proof totals');
const prFirst = num(must(/First full run[^\n]*\n\s*(\d+)\s*\n\s*orders broke a rule/, PR.text, 'first run')[1]);

// The narration: each line is a list of caption chunks (what the screen shows). The film places each line.
const LINES = [
	['a1', [`A cook knows what the kitchen will serve.`, `In a web shop, they still add it product by product.`]],
	['a2', [`Here they say it once,`, `in a chat built to sit inside Valio Aimo's own web shop.`]],
	['a3', [`A wedding for ${guests} guests, ${dishes} dishes.`, `One message becomes one cart:`, `${wLines} lines in ${wSecs} seconds.`]],
	['a4', [`Products and pack sizes from Valio Aimo's range,`, `with the test prices from the challenge.`]],
	['b1', [`Now a product runs short.`, `${num(sWant)} litres of lactose-free whipping cream,`, `and only ${num(sHave)} are in stock.`]],
	['b2', [`The rest comes from Valio Aimo's own range,`, `with the reason in one sentence.`]],
	['b3', pickedOnly ? [`One tap shows the other choices.`, `The cook can take only what is in stock.`] : [`One tap shows the other choices,`, `and one more changes it.`]],
	['c1', [`Now the request from Valio Aimo's brief:`, `1,000 litres of lactose-free milk.`, `It is €${over} over what this account may order alone,`, `so it stops.`]],
	['c2', [`The person who asked cannot approve it.`]],
	['c3', [`Someone else says yes, on their own phone.`]],
	['c4', [`Only then can the order be confirmed.`, `The confirmation is in the shop.`]],
	['d1', [`Valio Aimo would set the rules.`, `The AI reads the request and the cart.`, `The program picks products, amounts and prices.`]],
	['d2', [`Valio Aimo decides, per customer,`, `what outside software may see and do.`, `Pause ordering, and the answer changes.`]],
	['d3', [`A recorded Monday rush:`, `${tKitchens} simulated kitchens want ${tWanted} litres of the same milk.`, `${tHad} are in stock. None is promised twice.`]],
	['e1', [`We tested the rules first.`, `The first run let ${prFirst} orders through:`, `a bug in our own allergen rule.`, `We fixed it and ran ${prRuns} more,`, `on ${prModels} AI models.`, `None breaks a rule.`]],
	['f1', [`Not checked yet: no cook has used it,`, `and it runs on the challenge data,`, `not Valio Aimo's systems.`]],
	['f2', [`The way in: the chat goes into Minun Aimo,`, `for a few pilot kitchens.`]],
];
const used = {guests, dishes, lines: +wLines, seconds: +wSecs, short: {have: num(sHave), want: num(sWant), replacement: subLine, contract: subContract, picked: S.picked},
	approval: {order: pOrder, max: pMax, over, order_no: orderNo, approver: P.asker.ok}, rush: {kitchens: +tKitchens, wanted: tWanted, had: tHad},
	proof: {orders: prOrders, models: +prModels, runs: prRuns, first: prFirst}};

// --- spoken form: numbers as words, Finnish names spelt so the English voice says them nearly right ---
const ONES = 'zero one two three four five six seven eight nine ten eleven twelve thirteen fourteen fifteen sixteen seventeen eighteen nineteen'.split(' ');
const TENS = ['', '', 'twenty', 'thirty', 'forty', 'fifty', 'sixty', 'seventy', 'eighty', 'ninety'];
const words = (n) => {
	if (n < 20) return ONES[n];
	if (n < 100) return TENS[Math.floor(n / 10)] + (n % 10 ? '-' + ONES[n % 10] : '');
	if (n < 1000) return ONES[Math.floor(n / 100)] + ' hundred' + (n % 100 ? ' and ' + words(n % 100) : '');
	if (n < 1e6) return words(Math.floor(n / 1000)) + ' thousand' + (n % 1000 ? (n % 1000 < 100 ? ' and ' : ' ') + words(n % 1000) : '');
	return String(n);
};
const spoken = (s) => s
	.replace(/€([\d,]+)/g, (_, n) => `${words(num(n))} euros`)
	.replace(/\d[\d,]*/g, (n) => words(num(n)))
	.replace(/Valio Aimo/g, 'Vahlio Eyemo')
	.replace(/Minun Aimo/g, 'Minnun Eyemo')
	.replace(/\bAI\b/g, 'A.I.');

if (A.includes('--print')) {
	for (const [id, ch] of LINES) console.log(`${id.padEnd(3)} ${ch.join(' ')}`);
	console.log(JSON.stringify(used, null, 1));
	process.exit(0);
}

const out = path.join(BULK, 'public/audio');
const cache = path.join(BULK, 'tts/chunks');
fs.mkdirSync(out, {recursive: true});
fs.mkdirSync(cache, {recursive: true});
const VOICE = process.env.FILM_VOICE || 'af_heart', SPEED = process.env.FILM_SPEED || '1.06';
const TEMPO = Number(process.env.FILM_TEMPO || 1.06); // a little faster after the voice, pitch kept (ffmpeg atempo)
const hash = (s) => [...s].reduce((h, c) => (h * 31 + c.charCodeAt(0)) >>> 0, 7).toString(36);
const chunkFile = (t) => path.join(cache, `${hash(VOICE + SPEED + t)}.wav`);
const jobs = LINES.flatMap(([, ch]) => ch.map((c) => ({out: chunkFile(spoken(c)), text: spoken(c)}))).filter((j) => !fs.existsSync(j.out));
if (jobs.length) execFileSync('nice', ['-n', '10', PY, path.join(film, 'scripts/tts6.py'), VOICE, SPEED], {input: JSON.stringify(jobs), stdio: ['pipe', 'inherit', 'inherit'], env: {...process.env, OMP_NUM_THREADS: '8'}});

const ff = (args) => execFileSync('ffmpeg', ['-hide_banner', '-loglevel', 'error', '-y', ...args]);
const dur = (f) => Number(execFileSync('ffprobe', ['-v', 'error', '-show_entries', 'format=duration', '-of', 'csv=p=0', f]).toString());
const lines = {};
for (const [id, ch] of LINES) {
	// trim each chunk's silence, then join: a short pause after a comma or colon, a longer one after a sentence
	const parts = [], ws = [];
	let t = 0;
	ch.forEach((c, k) => {
		const src = chunkFile(spoken(c)), trimmed = src.replace(/\.wav$/, '.trim.wav');
		if (!fs.existsSync(trimmed)) ff(['-i', src, '-af', 'silenceremove=start_periods=1:start_threshold=-50dB:start_silence=0.02,areverse,silenceremove=start_periods=1:start_threshold=-50dB:start_silence=0.06,areverse', '-ar', '44100', '-ac', '1', trimmed]);
		const d = dur(trimmed);
		parts.push(trimmed);
		ws.push({w: c, s: Number(t.toFixed(3)), e: Number((t + d).toFixed(3))});
		t += d;
		if (k < ch.length - 1) {
			const gap = /[.:]$/.test(c) ? 0.32 : 0.16;
			const g = path.join(cache, `gap-${gap}.wav`);
			if (!fs.existsSync(g)) ff(['-f', 'lavfi', '-i', `anullsrc=r=44100:cl=mono`, '-t', String(gap), g]);
			parts.push(g);
			t += gap;
		}
	});
	const base = `v6-${id}-${hash(VOICE + SPEED + TEMPO + ch.join('|'))}`;
	const file = path.join(out, `${base}.wav`);
	if (!fs.existsSync(file)) {
		const list = path.join(cache, `${base}.txt`);
		fs.writeFileSync(list, parts.map((p) => `file '${p}'`).join('\n'));
		ff(['-f', 'concat', '-safe', '0', '-i', list, '-af', `atempo=${TEMPO}`, '-ar', '44100', '-ac', '1', file]);
	}
	for (const w of ws) { w.s = Number((w.s / TEMPO).toFixed(3)); w.e = Number((w.e / TEMPO).toFixed(3)); }
	lines[id] = {id, text: ch.join(' '), file: `audio/${base}.wav`, dur: dur(file), words: ws};
	console.log(`${id} (${lines[id].dur.toFixed(2)} s): ${lines[id].text}`);
}
const music = path.join(out, 'music2.mp3');
if (!fs.existsSync(music)) fs.copyFileSync('/bulk2/runs/valio/film2/public/audio/music2.mp3', music);
fs.writeFileSync(path.join(D, 'vo.json'), JSON.stringify({voice: `kokoro ${VOICE} x${SPEED}, tempo ${TEMPO} (local)`, music: 'audio/music2.mp3', used, lines}, null, 1));
console.log(`vo.json: ${Object.keys(lines).length} lines, ${Object.values(lines).reduce((a, l) => a + l.dur, 0).toFixed(1)} s of voice`);
