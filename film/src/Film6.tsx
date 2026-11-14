// Version 6: the three promises, on a phone, in the Finnish shop page; then Valio Aimo's side; then the close.
// Every shot is a recording of the public site (capture/shop6.mjs). Film time maps onto footage time through
// the shot's named events, so a re-capture needs no edit here. Every number in a caption or a gloss is read
// from the capture's facts (src/data6/facts-*.json), i.e. from the page under it.
import React from 'react';
import {AbsoluteFill, Audio, Img, Sequence, staticFile, useCurrentFrame} from 'remotion';
import {Shot, View, Rect, fit, segs, footAt, speedAt, Knot} from './shots';
import {MONO, SANS, FPS, soft} from './theme';
import {SpeedBadge} from './components/Screen';
import wedM from './data6/shot-wedding.json';
import shortM from './data6/shot-short.json';
import askM from './data6/shot-approve-asker.json';
import ownM from './data6/shot-approve-own.json';
import aprM from './data6/shot-approve-approver.json';
import byM from './data6/shot-byhand.json';
import accM from './data6/shot-access.json';
import towM from './data6/shot-tower.json';
import prM from './data6/shot-proof.json';
import fWed from './data6/facts-wedding.json';
import fShort from './data6/facts-short.json';
import fApp from './data6/facts-approve.json';
import fBy from './data6/facts-byhand.json';
import fAcc from './data6/facts-access.json';
import voJson from './data6/vo.json';

const WED = new Shot(wedM as any), SH = new Shot(shortM as any), ASK = new Shot(askM as any), OWN = new Shot(ownM as any), APR = new Shot(aprM as any);
const BY = new Shot(byM as any), ACC = new Shot(accM as any), TOW = new Shot(towM as any), PR = new Shot(prM as any);
const vo = voJson as any;
const D = (id: string) => vo.lines[id]?.dur ?? 3; // seconds of voice for a line
const GAP = 0.25;
const used = vo.used;
const INK = '#111418', MUTED = '#5C636E', BG = '#ECEEF1', PANEL = '#FFFFFF', ACC_C = '#0B6B4F', WARN = '#8A5A00';

// ---------- pieces ----------
const ev = (s: Shot, name: string, nth = 0) => s.ev(name, nth);
// the button a tap lands on, from the tap's own event (with room around it)
const tapBox = (s: Shot, name: string): Rect => { const b = s.m.events.find((e) => e.name === name)?.box as Rect | undefined; return b ? [b[0] - 8, b[1] - 40, b[2] + 16, b[3] + 80] : [0, 600, 390, 200]; };
const evOr = (s: Shot, name: string, fb: number) => (s.has(name) ? s.ev(name) : fb);
// a smoothed element box (average of the samples around t), so zooms do not jitter
// the first sampled time after t0 at which an element is on the page (the sent message appears a moment after the tap)
const firstBox = (s: Shot, key: string, t0: number): number => {
	const b = s.m.boxes[key] || [];
	for (let i = 0; i < s.m.boxT.length; i++) if (s.m.boxT[i] >= t0 && b[i]) return s.m.boxT[i];
	return t0 + 0.6;
};
const sbox = (s: Shot, key: string, t: number): Rect => {
	const xs = [-0.25, -0.12, 0, 0.12, 0.25].map((d) => s.box(key, t + d)).filter(Boolean) as Rect[];
	if (!xs.length) return s.boxOr(key, t);
	return [0, 1, 2, 3].map((i) => xs.reduce((a, b) => a + b[i], 0) / xs.length) as Rect;
};

const Img2: React.FC<{shot: Shot; t: number; view: View; w: number; h: number}> = ({shot, t, view, w, h}) => {
	const k = w / view[2];
	const [pw, ph] = shot.m.css;
	return <div style={{position: 'absolute', inset: 0, overflow: 'hidden', background: '#fff'}}>
		<Img src={staticFile(shot.frameAt(t))} style={{position: 'absolute', left: -view[0] * k, top: -view[1] * k, width: pw * k, height: ph * k, maxWidth: 'none'}} />
	</div>;
};

// the finger: a soft dot from the "finger" event to the tap, a ripple at the tap
const Finger: React.FC<{shot: Shot; t: number; view: View; k: number}> = ({shot, t, view, k}) => {
	const out: React.ReactNode[] = [];
	const evs = shot.m.events;
	evs.forEach((e, i) => {
		if (e.name !== 'finger' || e.x === undefined) return;
		const tap = evs.slice(i + 1).find((x) => x.click);
		const tt = tap ? tap.t : e.t + 0.45;
		if (t < e.t || t > tt + 0.55) return;
		const x = (e.x - view[0]) * k, y = (e.y! - view[1]) * k;
		const a = Math.min(1, (t - e.t) / 0.15) * (t > tt + 0.3 ? Math.max(0, 1 - (t - tt - 0.3) / 0.25) : 1);
		const p = t >= tt ? Math.min(1, (t - tt) / 0.45) : 0;
		const r = 26 * Math.max(1, Math.min(2.2, k));
		out.push(<div key={i} style={{position: 'absolute', left: x - r, top: y - r, width: 2 * r, height: 2 * r, borderRadius: '50%', background: `rgba(30,40,55,${0.28 * a})`, border: `3px solid rgba(255,255,255,${0.8 * a})`, boxShadow: `0 4px 14px rgba(0,0,0,${0.25 * a})`, transform: `scale(${t >= tt ? 0.85 : 1})`}} />);
		if (p > 0 && p < 1) out.push(<div key={'r' + i} style={{position: 'absolute', left: x - r * (1 + p), top: y - r * (1 + p), width: 2 * r * (1 + p), height: 2 * r * (1 + p), borderRadius: '50%', border: `4px solid rgba(11,107,79,${1 - p})`}} />);
	});
	return <>{out}</>;
};

// a phone: the whole screen of the recording inside a dark body
const Phone: React.FC<{shot: Shot; t: number; x: number; y: number; h: number; speed?: number; dim?: number}> = ({shot, t, x, y, h, speed = 1, dim = 0}) => {
	const [cw, ch] = shot.m.css;
	const k = h / ch, w = cw * k, b = 14;
	return <div style={{position: 'absolute', left: x - b, top: y - b, width: w + 2 * b, height: h + 2 * b, borderRadius: 54, background: '#14171C', boxShadow: '0 30px 70px rgba(10,20,30,.28), inset 0 0 0 2px #2A2F37'}}>
		<div style={{position: 'absolute', left: b, top: b, width: w, height: h, borderRadius: 42, overflow: 'hidden'}}>
			<Img2 shot={shot} t={t} view={[0, 0, cw]} w={w} h={h} />
			<Finger shot={shot} t={t} view={[0, 0, cw]} k={k} />
			{dim ? <div style={{position: 'absolute', inset: 0, background: `rgba(236,238,241,${dim})`}} /> : null}
		</div>
		{speed > 1.2 ? <SpeedBadge speed={speed} right={-8} top={-14} /> : null}
	</div>;
};

// a magnified part of the same recording, framed on a tracked element
const Zoom: React.FC<{shot: Shot; t: number; at: Rect; x: number; y: number; w: number; h: number; pad?: number; vw?: number; anchor?: 'top' | 'center' | 'bottom'; dy?: number; o?: number}> = ({shot, t, at, x, y, w, h, pad = 6, vw, anchor = 'center', dy = 0, o = 1}) => {
	const lg = shot.m.boxes.log ? shot.box('log', t) : null;
	const top = lg ? lg[1] : 0, bot = lg ? lg[1] + lg[3] : shot.m.css[1];
	const y0 = Math.max(top, at[1]), y1 = Math.min(bot, at[1] + at[3]);
	const vis: Rect = y1 - y0 > 40 ? [at[0], y0, at[2], y1 - y0] : at; // only the part of the element on screen
	const view = fit(vis, w / h, shot.m.css, {pad, w: vw, anchor, dy});
	const k = w / view[2];
	return <div style={{position: 'absolute', left: x, top: y, width: w, height: h, borderRadius: 18, overflow: 'hidden', boxShadow: '0 18px 50px rgba(10,20,30,.16), 0 0 0 1px rgba(0,0,0,.08)', opacity: o, background: '#fff'}}>
		<Img2 shot={shot} t={t} view={view} w={w} h={h} />
		<Finger shot={shot} t={t} view={view} k={k} />
	</div>;
};

const Head: React.FC<{n?: string; title: React.ReactNode; x?: number; y?: number; w?: number; at?: number}> = ({n, title, x = 700, y = 70, w = 1140, at = 0}) => {
	const f = useCurrentFrame();
	return <div style={{position: 'absolute', left: x, top: y, width: w, fontFamily: SANS, color: INK, opacity: soft(f, at, 10)}}>
		{n ? <div style={{fontFamily: MONO, fontSize: 22, letterSpacing: 2, color: ACC_C, fontWeight: 700, marginBottom: 10}}>{n}</div> : null}
		<div style={{fontSize: 52, fontWeight: 760, letterSpacing: -1.6, lineHeight: 1.08}}>{title}</div>
	</div>;
};
// what the Finnish on screen means
const Gloss: React.FC<{fi?: string; en: React.ReactNode; x: number; y: number; w: number; at?: number}> = ({fi, en, x, y, w, at = 0}) => {
	const f = useCurrentFrame();
	return <div style={{position: 'absolute', left: x, top: y, width: w, fontFamily: SANS, opacity: soft(f, at, 8)}}>
		{fi ? <div style={{fontSize: 24, color: MUTED, fontStyle: 'italic', lineHeight: 1.3}}>“{fi}”</div> : null}
		<div style={{fontSize: 30, color: INK, lineHeight: 1.3, marginTop: fi ? 6 : 0, fontWeight: 560}}><span style={{fontFamily: MONO, fontSize: 20, color: ACC_C, marginRight: 10, letterSpacing: 1}}>MEANS</span>{en}</div>
	</div>;
};
const Note: React.FC<{children: React.ReactNode; x: number; y: number; w?: number; at?: number; big?: boolean}> = ({children, x, y, w = 1140, at = 0, big}) => {
	const f = useCurrentFrame();
	return <div style={{position: 'absolute', left: x, top: y, width: w, fontFamily: SANS, fontSize: big ? 44 : 32, fontWeight: big ? 720 : 560, lineHeight: 1.25, color: INK, opacity: soft(f, at, 8), letterSpacing: big ? -1 : 0}}>{children}</div>;
};

// the burned captions: the chunk of the voice being said now
const Caption: React.FC<{sec: number}> = ({sec}) => {
	let text: string | null = null;
	for (const id of Object.keys(LINE_AT)) {
		const l = vo.lines[id];
		if (!l) continue;
		const st = LINE_AT[id];
		for (const w of l.words) if (sec >= st + w.s - 0.05 && sec <= st + w.e + 0.2) text = w.w;
	}
	const phone = TL.find((s) => sec >= s.from && sec < s.from + s.dur)?.cap ?? 'center';
	if (!text || phone === 'none') return null; // the close and the end card say it in full on screen
	const box: React.CSSProperties = phone === 'right' ? {left: 700, width: 1140, bottom: 46, textAlign: 'left'} : phone === 'right2' ? {left: 1230, width: 640, bottom: 46, textAlign: 'left'} : {left: 120, right: 120, bottom: 40, textAlign: 'center'};
	return <div style={{position: 'absolute', zIndex: 30, ...box}}><span style={{display: 'inline-block', fontFamily: SANS, fontSize: phone === 'right2' ? 34 : 38, lineHeight: 1.2, fontWeight: 650, color: '#fff', background: 'rgba(17,20,24,.9)', borderRadius: 12, padding: '10px 22px'}}>{text}</span></div>;
};

// ---------- scenes (local seconds s from the scene start) ----------
const PH = {x: 150, y: 54, h: 972}; // the phone on the left
const RX = 700, RW = 1140; // the right column

// time plan helper: footage segments [from, to, speed] -> footage time at local second s
const plan = (list: [number, number, number][]) => {
	const L = list.filter((x) => x[1] > x[0]);
	const {knots, frames} = segs(L, FPS);
	// the local second at which footage time t is first shown
	const when = (t: number) => {
		let f = 0;
		for (const [a, b, sp] of L) {
			if (t <= b) return f + Math.max(0, t - a) / sp;
			f += (b - a) / sp;
		}
		return f;
	};
	return {at: (s: number) => footAt(knots, s * FPS), sp: (s: number) => speedAt(knots, s * FPS, FPS), len: frames / FPS, when};
};

// 0. by hand: the web shop today
const byPlan = (() => { const t0 = evOr(BY, 'type:lohifilee:start', 2.8); return plan([[t0 - 1.2, t0 + 3.4, 1]]); })();
const SByhand: React.FC<{s: number}> = ({s}) => {
	const t = byPlan.at(s);
	// the running clock alone (the header line beside it is about another order), and the search and the cart
	const clock = BY.boxOr('clock', t), q = BY.boxOr('q', t);
	return <>
		<Phone shot={BY} t={t} {...PH} />
		<Head n="TODAY" title={<>In a web shop, the cook adds it product by product.</>} />
		<Crop shot={BY} t={t} at={[clock[0] - 32, clock[1] - 14, clock[2] + 44, clock[3] + 28] as Rect} x={RX} y={260} w={300} />
		<Crop shot={BY} t={t} at={[q[0] - 12, q[1] - 56, q[2] + 24, 330] as Rect} x={RX + 360} y={260} w={480} />
		<Gloss en={<>An order by hand: each product searched, its pack size checked, added to the cart. A clock runs.</>} x={RX} y={760} w={RW - 120} at={6} />
	</>;
};

// 1. Promise 1: one line becomes one cart
const W = (k: string) => ev(WED, k);
// the voice: a2 from the start, a3 after it, a4 when the lines open
const wedA4 = 0.15 + D('a2') + GAP + D('a3') + GAP;
const wedCardSp = Math.max(0.5, Math.min(1.5, (W('rows') - 0.7 - W('card:shown')) / Math.max(1, wedA4 - 1.0 - (2.9 + (W('card:shown') - W('ask') - 0.3) / 6))));
const wedPlan = plan([
	[W('ask') - 2.6, W('ask') + 0.3, 1],
	[W('ask') + 0.3, W('card:shown'), 6],
	[W('card:shown'), W('rows') - 0.7, wedCardSp],
	[W('rows') - 0.7, W('rows') + 0.5, 1],
	[W('scroll:rows'), WED.m.dur - 0.3, 1],
]);
const weddingCard = String((fWed as any).card || '');
const BANNERS: [RegExp, string][] = [
	[/^Näin tämä toimisi Valio Aimo -verkkokaupassa\. Prototyyppi Valio Aimon haastedatalla, ei Valio Aimon järjestelmä\.?$/, "How this would work in Valio Aimo's web shop. A prototype on Valio Aimo's challenge data, not Valio Aimo's system."],
	[/^Junction 2026 -prototyyppi Valio Aimon haastedatalla\. Ei Valio Aimon järjestelmä\.?$/, "A Junction 2026 prototype on Valio Aimo's challenge data. Not Valio Aimo's system."],
];
const bannerFi = String((fWed as any).banner || '').replace(/\s+/g, ' ').trim();
export const BANNER_EN = bannerFi ? BANNERS.find(([re]) => re.test(bannerFi))?.[1] ?? null : BANNERS[0][1];
const madeLine = String((fWed as any).made || '');
const sumOf = (card: string) => {
	const L = card.split('\n').map((x) => x.replace(/[\u00a0\u202f]/g, ' ').trim());
	const v = (k: string) => { const i = L.indexOf(k); return i >= 0 ? L[i + 1] : ''; };
	return {day: v('Toimituspäivä'), ex: v('Ostokset (alv 0 %)'), inc: v('Yhteensä (sis. alv)')};
};
const wsum = sumOf(weddingCard);
const wMe = firstBox(WED, 'me', W('ask'));
const SWedding: React.FC<{s: number}> = ({s}) => {
	const t = wedPlan.at(s), sp = wedPlan.sp(s);
	const before = t < wMe, waiting = t >= wMe && t < W('card:shown') + 0.6, card = t >= W('card:shown') + 0.6 && t < W('scroll:rows') + 0.3, rows = t >= W('scroll:rows') + 0.3;
	return <>
		<Phone shot={WED} t={t} {...PH} speed={sp} />
		<Head n="1 · ONE LINE, ONE CART" title={<>One line about what the kitchen will serve becomes one cart.</>} />
		{before ? <>
			<Zoom shot={WED} t={t} at={sbox(WED, 'banner', t)} x={RX} y={300} w={RW} h={150} pad={4} />
			<Gloss fi={bannerFi || undefined} en={<>{BANNER_EN ?? 'A prototype on Valio Aimo\'s challenge data, not Valio Aimo\'s system.'}</>} x={RX} y={480} w={RW} />
			<Note x={RX} y={640} at={20}>A chat built to sit inside Valio Aimo's own web shop. Our sketch, not theirs.</Note>
		</> : null}
		{waiting ? <>
			<Zoom shot={WED} t={wMe + 0.6} at={sbox(WED, 'me', wMe + 0.6)} x={RX} y={300} w={RW} h={330} vw={360} />
			<Gloss en={<>“Wedding on Saturday, 180 people: salmon soup, chicken breast in thyme cream sauce, roast potatoes and carrots, strawberry cream cake and coffee.”</>} x={RX} y={660} w={RW} />
		</> : null}
		{card ? <>
			<Zoom shot={WED} t={t} at={sbox(WED, 'cart', t)} x={RX} y={250} w={RW} h={560} vw={372} anchor="top" />
			<Gloss fi={madeLine} en={<>Cart ready: from one message, {used.lines} lines in the cart, in {used.seconds} seconds{wsum.ex ? <>, €{fmtEur(wsum.ex.replace(' €', ''))} before VAT</> : null}.</>} x={RX} y={830} w={RW} />
		</> : null}
		{rows ? <>
			<Zoom shot={WED} t={t} at={sbox(WED, 'rows', t)} x={RX} y={250} w={RW} h={560} vw={372} anchor="top" />
			<Gloss en={<>The lines: products from Valio Aimo's range, in packs, with the challenge's test prices. Nothing is ordered yet.</>} x={RX} y={830} w={RW} />
		</> : null}
	</>;
};
export const WEDDING_LEN = wedPlan.len;

// 2. Promise 2: a missing product gets a replacement from Valio Aimo's own range
const SS = (k: string) => ev(SH, k);
// b1 from the start, b2 over the card, b3 when "Vaihda" opens the choices
const shB3 = 0.2 + D('b1') + GAP + D('b2') + GAP;
const shCard0 = 1.1 + (SS('card:shown') - SS('ask') - 0.3) / 6;
// the capture script can sit still for a while before a tap lands (the page does not change then): those stills are cut
const shCardEnd = Math.min(SS('swapopen') - 0.7, SS('scroll:card') + 3.2);
const shAltsHold = Math.min(SS('scroll:alts') - 0.3, SS('swapopen') + 1.4);
const shCardSp = Math.max(0.4, Math.min(1.5, (shCardEnd - SS('card:shown')) / Math.max(1, shB3 - 0.5 - shCard0)));
const shortPlan = plan([
	[SS('ask') - 0.8, SS('ask') + 0.3, 1],
	[SS('ask') + 0.3, SS('card:shown'), 6],
	[SS('card:shown'), shCardEnd, shCardSp],
	[SS('swapopen') - 0.6, shAltsHold, 1],
	[Math.max(shAltsHold, SS('scroll:alts') - 0.3), SS('scroll:alts') + 0.9, 1],
	[SS('pick') - 0.5, Math.min(SH.m.dur - 0.2, SS('picked') + 1.0), 1],
]);
const short = used.short;
const shMe = firstBox(SH, 'me', SS('ask'));
const shAlts = firstBox(SH, 'alts', SS('swapopen'));
// the shortage note above the lines (older captures only tracked the last note, which is the same one there)
const NTK = SH.m.boxes.ntfirst?.some(Boolean) ? 'ntfirst' : 'nt';
const SShort: React.FC<{s: number}> = ({s}) => {
	const t = shortPlan.at(s), sp = shortPlan.sp(s);
	const cardP = t >= SS('card:shown') && t < SS('swapopen') - 0.6, altsP = t >= SS('swapopen') - 0.6 && t < SS('picked'), after = t >= SS('picked');
	return <>
		<Phone shot={SH} t={t} {...PH} speed={sp} />
		<Head n="2 · A PRODUCT RUNS SHORT" title={<>The replacement comes from Valio Aimo's own range.</>} />
		{t < shMe ? <>
			<Zoom shot={SH} t={t} at={tapBox(SH, 'ask')} x={RX} y={300} w={RW} h={240} vw={360} />
			<Gloss fi="Tuote loppuu kesken (esimerkki)" en={<>A product runs short (an example to try).</>} x={RX} y={570} w={RW} />
		</> : null}
		{t >= shMe && t < SS('card:shown') ? <>
			<Zoom shot={SH} t={shMe + 0.6} at={sbox(SH, 'me', shMe + 0.6)} x={RX} y={300} w={RW} h={240} vw={360} />
			<Gloss en={<>“{short.want} litres of lactose-free Arla Pro whipping cream for Monday.”</>} x={RX} y={570} w={RW} />
		</> : null}
		{cardP ? <>
			<Zoom shot={SH} t={t} at={sbox(SH, NTK, t)} x={RX} y={250} w={RW} h={470} vw={372} />
			<Gloss en={<>Not enough in stock: {short.have} of {short.want} l. Replacement for the rest: {shortRepl}. Same kind of product, same diet labels, no new allergens.</>} x={RX} y={740} w={RW} />
		</> : null}
		{altsP ? <>
			{t < shAlts ? <Zoom shot={SH} t={t} at={tapBox(SH, 'swapopen')} x={RX} y={300} w={RW} h={300} vw={360} />
				: <Zoom shot={SH} t={t} at={sbox(SH, 'alts', Math.max(t, shAlts + 0.3))} x={RX} y={250} w={RW} h={470} vw={372} />}
			<Gloss fi="Vaihda" en={<>Change: the other choices, each from Valio Aimo's range, with what it costs more or less.</>} x={RX} y={740} w={RW} />
		</> : null}
		{after ? <>
			<Zoom shot={SH} t={t} at={sbox(SH, NTK, t)} x={RX} y={250} w={RW} h={470} vw={372} />
			<Gloss en={pickedGloss} x={RX} y={740} w={RW} />
		</> : null}
		{t >= SS('card:shown') ? <Note x={RX} y={890} w={RW} at={0}><span style={{fontSize: 24, color: MUTED}}>Here it picks from all Valio Aimo products in the challenge data. With Valio Aimo's data it would offer only this customer's contract products.</span></Note> : null}
	</>;
};
// the replacement named in the card, glossed: "Valio kuohukerma 1 l laktoositon, luomu, 12 l · parempi · sopimushinta · −25 %/l"
const shortRepl = (() => {
	const m = /Korvaava tuote: (.*?), ([\d ,]+ l) · (\S+) · (\S+) · (.*)$/.exec(String(short.replacement || ''));
	if (!m) return String(short.replacement || '').replace('Korvaava tuote: ', '');
	const tier: Record<string, string> = {hyvä: 'good', parempi: 'better', paras: 'best'};
	const price: Record<string, string> = {sopimushinta: 'contract price', listahinta: 'list price'};
	return `${m[1].replace('kuohukerma', 'whipping cream').replace('laktoositon', 'lactose-free').replace('luomu', 'organic')}, ${m[2]}, ${tier[m[3]] ?? m[3]}, ${price[m[4]] ?? m[4]}, ${m[5].replace('/l', ' a litre')}`;
})();
const pickedGloss = /Vain saatavilla/.test(String(short.picked || '')) ? <>Picked: only what is in stock. The card changes at once.</> : <>Picked another choice. The card changes at once.</>;
export const SHORT_LEN = shortPlan.len;

// 3. Promise 3: nothing goes out before someone confirms. One shared clock for the phones.
const A = (k: string) => ev(ASK, k);
const P = (k: string) => ev(APR, k);
const ownStart = OWN.m.frames.length ? Math.min(...OWN.m.frames) : A('scroll:appr') + 3;
const ownEnd = OWN.m.dur;
const aprStart = APR.m.frames.length ? Math.min(...APR.m.frames) : P('name:focus') - 4;
// part a: the request stops (asker phone + zoom on the approval box)
// c1 runs over all of part a; the asker's phone is idle while the approver works, so the hold is real footage
const appA0 = 1.1 + (A('card:shown') - A('ask') - 0.3) / 6;
const appA = plan([
	[A('ask') - 0.8, A('ask') + 0.3, 1],
	[A('ask') + 0.3, A('card:shown'), 6],
	[A('card:shown'), Math.min(ownStart - 0.2, A('card:shown') + Math.max(4, 0.2 + D('c1') + 0.35 - appA0)), 1],
]);
// part b: the asker opens the link on their own phone: refused
const appB = plan([[ownStart + 0.3, Math.min(ownEnd, ownStart + Math.max(2.6, D('c2') + 1.0)), 1]]);
// part c: two phones, one clock: the approver types a name and says yes; the asker's card turns
const appC = plan([
	[aprStart + 0.4, aprStart + 1.6, 1],
	[P('name:focus') - 0.3, P('approved') + 0.8, 2],
	[A('turned') - 0.4, A('turned') + 1.3, 1],
]);
// part d: confirm, the order number, the confirmation in the shop
const appD = plan([
	[A('confirm') - 0.8, A('confirm') + 0.3, 1],
	[A('confirm') + 0.3, A('placed'), 3],
	[A('placed'), A('scroll:placed') + 1.6, 1],
	[evOr(ASK, 'record:shown', A('record:open') + 1) + 0.15, evOr(ASK, 'record:checked', A('record:open') + 3) + 1.4, 1],
]);
const ap = (fApp as any);
const aMe = firstBox(ASK, 'me', A('ask'));
const SApproveA: React.FC<{s: number}> = ({s}) => {
	const t = appA.at(s), sp = appA.sp(s);
	return <>
		<Phone shot={ASK} t={t} {...PH} speed={sp} />
		<Head n="3 · NOTHING GOES OUT BEFORE SOMEONE CONFIRMS" title={<>Over this account's limit, the order stops.</>} />
		{t < aMe ? <>
			<Zoom shot={ASK} t={t} at={tapBox(ASK, 'ask')} x={RX} y={330} w={RW} h={200} vw={360} />
			<Gloss en={<>“1,000 l of lactose-free milk drink”: the request in Valio Aimo's brief.</>} x={RX} y={560} w={RW} />
		</> : t < A('card:shown') + 0.6 ? <>
			<Zoom shot={ASK} t={aMe + 0.6} at={sbox(ASK, 'me', aMe + 0.6)} x={RX} y={330} w={RW} h={200} vw={360} />
			<Gloss en={<>“1,000 litres of lactose-free milk drink for Monday.” The request in Valio Aimo's brief.</>} x={RX} y={560} w={RW} />
		</> : <>
			<Zoom shot={ASK} t={t} at={sbox(ASK, 'appr', Math.max(t, A('card:shown') + 0.8))} x={RX} y={300} w={RW} h={470} vw={372} anchor="top" />
			<Gloss en={<>Needs approval. The order is €{fmtEur(used.approval.order)} before VAT. Without approval you may order up to €{fmtEur(used.approval.max)}. The confirm button waits.</>} x={RX} y={800} w={RW} />
		</>}
	</>;
};
const SApproveB: React.FC<{s: number}> = ({s}) => {
	const t = appB.at(s);
	return <>
		<Phone shot={OWN} t={t} {...PH} />
		<Head n="3 · NOTHING GOES OUT BEFORE SOMEONE CONFIRMS" title={<>The person who asked cannot approve it.</>} />
		<Zoom shot={OWN} t={t} at={(ap.own?.refusal_box as Rect) ?? ([0, 95, 390, 150] as Rect)} x={RX} y={300} w={RW} h={420} vw={390} anchor="top" />
		<Gloss fi="Et voi hyväksyä omaa tilaustasi." en={<>You cannot approve your own order. Send this link to the approver. They approve on their own device.</>} x={RX} y={750} w={RW} />
	</>;
};
const SApproveC: React.FC<{s: number}> = ({s}) => {
	const t = appC.at(s), sp = appC.sp(s);
	const done = t >= P('approved');
	return <>
		<Phone shot={ASK} t={t} x={110} y={54} h={972} />
		<Phone shot={APR} t={t} x={640} y={54} h={972} speed={sp} />
		<div style={{position: 'absolute', left: 110, top: 8, fontFamily: MONO, fontSize: 20, color: MUTED, letterSpacing: 1.5}}>THE COOK'S PHONE</div>
		<div style={{position: 'absolute', left: 640, top: 8, fontFamily: MONO, fontSize: 20, color: MUTED, letterSpacing: 1.5}}>SOMEONE ELSE'S PHONE</div>
		<Head n="3 · CONFIRM" title={<>Someone else says yes, on their own phone.</>} x={1230} w={640} y={90} />
		{!done ? <Gloss fi="… pyytää hyväksyntääsi" en={<>“… asks for your approval”, with why: €{fmtEur(used.approval.order)} is over the €{fmtEur(used.approval.max)} limit. A name, then “Hyväksy”: approve.</>} x={1230} y={420} w={620} />
			: <Gloss fi={`Hyväksytty. · ${String(used.approval.approver || '').replace('✓ ', '✓ ')}`} en={<>Approved. The cook's card turns by itself.</>} x={1230} y={420} w={620} />}
		<Note x={1230} y={760} w={620} at={10}><span style={{fontSize: 24, color: MUTED}}>Here the approver types a name. In Minun Aimo it would be the signed-in user who may approve.</span></Note>
	</>;
};
const SApproveD: React.FC<{s: number}> = ({s}) => {
	const t = appD.at(s), sp = appD.sp(s);
	const rec = t >= evOr(ASK, 'record:shown', A('record:open') + 1) - 0.01;
	return <>
		<Phone shot={ASK} t={t} {...PH} speed={sp} />
		<Head n="3 · CONFIRM" title={<>Only then can the order be confirmed.</>} />
		{!rec ? <>
			<Zoom shot={ASK} t={t} at={sbox(ASK, 'cart', t)} x={RX} y={280} w={RW} h={500} vw={372} anchor="top" />
			<Gloss fi={t >= A('placed') ? String(ap.asker?.placed || '').split('\n')[0] : 'Vahvista tilaus'} en={t >= A('placed') ? <>Confirmed order {used.approval.order_no}.</> : <>Confirm order.</>} x={RX} y={810} w={RW} />
		</> : <>
			<Zoom shot={ASK} t={t} at={[0, 60, 390, 380] as Rect} x={RX} y={280} w={RW} h={500} vw={390} anchor="top" />
			<Gloss fi="tilausvahvistus" en={<>The order confirmation, in the shop: order {used.approval.order_no}, approved by the name typed on the other phone.</>} x={RX} y={810} w={RW} />
		</>}
	</>;
};
const fmtEur = (s: string) => {
	const n = Number(String(s).replace(/[\s  ]/g, '').replace(',', '.'));
	return n.toLocaleString('en-GB', {minimumFractionDigits: 2, maximumFractionDigits: 2});
};

// 4. Valio Aimo would set the rules
const AC = (k: string) => ev(ACC, k);
// d1 over the page as it stands, d2 as the switch is flipped: the tap lands on "Pause ordering"
const accFinger = ACC.m.events.find((e) => e.name === 'finger')?.t ?? AC('pause') - 0.45;
const accD2 = 0.2 + D('d1') + GAP;
const accPauseWord = (vo.lines.d2?.words?.[2]?.s ?? 4.1) + accD2;
const accPlan = plan([
	[0.4, accFinger - 0.1, Math.max(0.05, (accFinger - 0.5) / Math.max(1, accPauseWord - 0.35))],
	[accFinger - 0.1, Math.min(ACC.m.dur - 0.3, AC('scroll:answers') + 2.6), 1],
]);
const SAccess: React.FC<{s: number}> = ({s}) => {
	const t = accPlan.at(s);
	const tc = Math.min(t, AC('scroll:answers') - 0.4); // the switches as they were just before the page scrolled to the answers
	const ctl = ACC.boxOr('ctl', tc), on = ACC.boxOr('on', tc);
	const flip = s >= accD2 - 0.2;
	const answers = t >= AC('scroll:answers') + 0.7;
	return <>
		<Crop shot={ACC} t={tc} at={[ctl[0] - 6, ctl[1] - 6, ctl[2] + 12, ctl[3] + 12]} x={150} y={60} w={430} maxH={960} />
		<Head n="VALIO AIMO'S SIDE · /access" title={<>Valio Aimo would set the rules.</>} />
		{!answers && flip ? <>
			<Crop shot={ACC} t={tc} at={[on[0] - 26, on[1] - 44, on[2] + 52, on[3] + 104]} x={RX} y={300} w={760} />
			<Note x={RX} y={300 + (on[3] + 104) * 760 / (on[2] + 52) + 40} w={RW} at={0}><b>Pause all agent ordering</b>: Valio Aimo stops every order from outside software for this customer. Lookups still answer.</Note>
		</> : !answers ? <>
			<Note x={RX} y={320} w={RW} at={8}>Per customer, Valio Aimo would decide what outside software may do: look up, re-order or order; the order size that needs a person; whether price and stock are shown.</Note>
		</> : <>
			<Crop shot={ACC} t={t} at={(() => { const r = sbox(ACC, 'rows', t); return [r[0] - 12, r[1] - 10, r[2] + 24, r[3] + 20] as Rect; })()} x={RX} y={260} w={RW} maxH={560} />
			<Gloss en={<>Asked again after the pause: a lookup still answers. The order is refused by rule W1.</>} x={RX} y={850} w={RW} />
		</>}
	</>;
};
const Frame: React.FC<{shot: Shot; t: number; view: View; speed?: number}> = ({shot, t, view, speed = 1}) => {
	const w = 1760, h = 990, k = w / view[2];
	return <div style={{position: 'absolute', left: 80, top: 80, width: w, height: h, borderRadius: 16, overflow: 'hidden', boxShadow: '0 24px 60px rgba(10,20,30,.18), 0 0 0 1px rgba(0,0,0,.08)'}}>
		<Img2 shot={shot} t={t} view={view} w={w} h={h} />
		<Finger shot={shot} t={t} view={view} k={k} />
		{speed > 1.2 ? <SpeedBadge speed={speed} /> : null}
	</div>;
};
const Crop: React.FC<{shot: Shot; t: number; at: Rect; x: number; y: number; w: number; maxH?: number}> = ({shot, t, at, x, y, w, maxH = 900}) => {
	const h = Math.min(maxH, at[3] * w / at[2]);
	return <div style={{position: 'absolute', left: x, top: y, width: w, height: h, borderRadius: 16, overflow: 'hidden', boxShadow: '0 18px 50px rgba(10,20,30,.14), 0 0 0 1px rgba(0,0,0,.08)'}}>
		<Img2 shot={shot} t={t} view={[at[0], at[1], at[2]]} w={w} h={h} />
		<Finger shot={shot} t={t} view={[at[0], at[1], at[2]]} k={w / at[2]} />
	</div>;
};
const Tag: React.FC<{children: React.ReactNode; x: number; y: number}> = ({children, x, y}) => <div style={{position: 'absolute', left: x, top: y, fontFamily: MONO, fontSize: 22, letterSpacing: 1.5, color: ACC_C, fontWeight: 700}}>{children}</div>;
const towSp = Math.max(2, (TOW.m.dur - 1.4) / (0.3 + D('d3') + 0.5));
const towPlan = plan([[Math.max(1.0, evOr(TOW, 'replay', 0) + 1.5), TOW.m.dur - 0.4, towSp]]);
const STower: React.FC<{s: number}> = ({s}) => {
	const t = towPlan.at(s);
	return <>
		<Frame shot={TOW} t={t} view={[40, 255, 1080]} speed={towSp} />
		<Tag x={90} y={36}>VALIO AIMO'S SIDE · /tower · A RECORDED RUN, REPLAYED</Tag>
	</>;
};
// e1: the first run that found the bug and the fix, then the totals after the fix ("We fixed it and ran ...")
const prFix = 0.2 + (vo.lines.e1?.words?.[3]?.s ?? 5.0) - 0.2;
const prPlan = plan([[ev(PR, 'scroll:fix') + 0.9, Math.min(PR.m.dur - 0.3, ev(PR, 'scroll:fix') + 0.9 + prFix), 1], [0.3, Math.min(ev(PR, 'scroll:fix') - 0.1, 0.3 + 0.2 + D('e1') + 0.4 - prFix), 1]]);
const SProof: React.FC<{s: number}> = ({s}) => {
	const t = prPlan.at(s);
	const top = t < ev(PR, 'scroll:fix');
	const has = (k: string) => PR.m.boxes[k]?.some(Boolean);
	if (!has('tiles')) return <><Frame shot={PR} t={t} view={top ? [150, 60, 1140] : [140, 30, 940]} /><Tag x={90} y={36}>WE BROKE IT FIRST · /proof</Tag></>;
	const pad = (r: Rect, l: number, tp: number, rt: number, b: number): Rect => [r[0] - l, r[1] - tp, r[2] + l + rt, r[3] + tp + b];
	if (top) {
		const tb = Math.min(t, ev(PR, 'scroll:fix') - 0.35), ti0 = Math.min(t, ev(PR, 'scroll:fix') - 0.1); // before the page starts to scroll
		const h = sbox(PR, 'head', tb), ti = sbox(PR, 'tiles', tb);
		return <>
			<Tag x={160} y={110}>WE BROKE IT FIRST · /proof</Tag>
			<Crop shot={PR} t={ti0} at={pad([h[0], h[1], h[2], ti[1] + ti[3] - h[1]], 20, 12, 20, 22)} x={110} y={160} w={1700} />
			<Note x={110} y={760} w={1700} big at={10}>After the fix: 0 of {used.proof.orders} orders broke a rule, across {used.proof.models} AI models and {used.proof.runs} runs.</Note>
		</>;
	}
	const st = sbox(PR, 'story', t), pr = sbox(PR, 'prose', t);
	return <>
		<Tag x={160} y={110}>WE BROKE IT FIRST · /proof</Tag>
		<Crop shot={PR} t={t} at={[st[0] - 20, st[1] - 50, st[2] + 40, 50 + 122]} x={110} y={160} w={1700} />
		<Crop shot={PR} t={t} at={pad(pr, 20, 10, 20, 10)} x={110} y={480} w={1000} />
		<Note x={1170} y={500} w={640} big at={10}>The first full run: {used.proof.first} orders broke a rule.</Note>
		<Note x={1170} y={660} w={640} at={30}>A bug in our own allergen rule. Fixed in code; after the fix, 0.</Note>
	</>;
};

// close: what was not checked; then the end card with the way in. The words on screen are the voice's, so no caption.
const SClose: React.FC<{s: number}> = ({s}) => {
	const f = useCurrentFrame();
	return <AbsoluteFill style={{background: INK}}>
		<div style={{position: 'absolute', left: 150, top: 340, width: 1620, fontFamily: SANS, color: '#F4F6F8', opacity: soft(s * FPS, 0, 8)}}>
			<div style={{fontFamily: MONO, fontSize: 24, letterSpacing: 2, color: '#E8B04A', fontWeight: 700, marginBottom: 26}}>NOT CHECKED YET</div>
			<div style={{fontSize: 58, fontWeight: 720, letterSpacing: -1.4, lineHeight: 1.18}}>No cook has used it.<br />It runs on the challenge data, not on Valio Aimo's systems.</div>
		</div>
	</AbsoluteFill>;
};
const SEnd: React.FC<{s: number}> = ({s}) => {
	const f = useCurrentFrame();
	return <AbsoluteFill style={{background: INK}}>
		<div style={{position: 'absolute', left: 150, top: 290, width: 1620, fontFamily: SANS, color: '#F4F6F8', opacity: soft(s * FPS, 0, 8)}}>
			<div style={{fontSize: 76, fontWeight: 780, letterSpacing: -2.4, lineHeight: 1.06}}>From what the kitchen will serve to a confirmed order.</div>
			<div style={{fontSize: 40, color: '#C9D1DA', marginTop: 44, lineHeight: 1.3}}>The way in: the chat goes into Minun Aimo, for a few pilot kitchens.</div>
			<div style={{fontFamily: MONO, fontSize: 36, color: '#7FD8B8', marginTop: 56}}>trusted-trade-agents.fly.dev/shop</div>
		</div>
	</AbsoluteFill>;
};

// ---------- the timeline: each scene lasts its footage or its voice, whichever is longer ----------
type TLItem = {id: string; from: number; dur: number; cap: 'right' | 'right2' | 'center' | 'none'};
export const TL: TLItem[] = [];
export const LINE_AT: Record<string, number> = {};
{
	let T = 0;
	const scene = (id: string, len: number, cap: TLItem['cap'], lines: [string, number][], tail = 0.35) => {
		let end = len;
		for (const [l, at] of lines) { LINE_AT[l] = T + at; end = Math.max(end, at + D(l) + tail); }
		TL.push({id, from: T, dur: end, cap});
		T += end;
	};
	scene('byhand', byPlan.len, 'right', [['a1', 0.3]], 0.2);
	scene('wedding', wedPlan.len, 'right', [['a2', 0.15], ['a3', 0.15 + D('a2') + GAP], ['a4', Math.max(wedA4, wedPlan.when(W('rows') - 0.7) + 0.8)]]);
	scene('short', shortPlan.len, 'right', [['b1', 0.2], ['b2', 0.2 + D('b1') + GAP], ['b3', Math.max(shB3, shortPlan.when(SS('swapopen')) - 0.3)]]);
	scene('appA', appA.len, 'right', [['c1', 0.2]], 0.2);
	scene('appB', appB.len, 'right', [['c2', 0.2]], 0.2);
	scene('appC', appC.len, 'right2', [['c3', 0.3]], 0.2);
	scene('appD', appD.len, 'right', [['c4', 0.2]]);
	scene('access', accPlan.len, 'right', [['d1', 0.2], ['d2', accD2]]);
	scene('tower', towPlan.len, 'center', [['d3', 0.3]]);
	scene('proof', prPlan.len, 'center', [['e1', 0.2]]);
	scene('close', 0, 'none', [['f1', 0.3]], 0.3);
	scene('end', 0, 'none', [['f2', 0.3]], 1.0);
}
export const FILM6_DUR = Math.round((TL.at(-1)!.from + TL.at(-1)!.dur) * FPS);

const SCENES: Record<string, React.FC<{s: number}>> = {byhand: SByhand, wedding: SWedding, short: SShort, appA: SApproveA, appB: SApproveB, appC: SApproveC, appD: SApproveD, access: SAccess, tower: STower, proof: SProof, close: SClose, end: SEnd};
// which footage frames the cut shows (render6.sh stages only these) and the poster's frame (the first cart card)
const USES: Record<string, [{at: (s: number) => number}, Shot[]]> = {byhand: [byPlan, [BY]], wedding: [wedPlan, [WED]], short: [shortPlan, [SH]], appA: [appA, [ASK]], appB: [appB, [OWN]], appC: [appC, [ASK, APR]], appD: [appD, [ASK]], access: [accPlan, [ACC]], tower: [towPlan, [TOW]], proof: [prPlan, [PR]]};
export const framesUsed = () => {
	const set = new Set<string>();
	for (const sc of TL) {
		const u = USES[sc.id];
		if (!u) continue;
		// the same global frames, and the same arithmetic, as the render
		for (let F = Math.floor(sc.from * FPS) - 1; F <= Math.ceil((sc.from + sc.dur) * FPS) + 1; F++) { const s = F / FPS - sc.from; if (s < 0 || s >= sc.dur) continue; for (const sh of u[1]) set.add(sh.frameAt(u[0].at(s))); }
	}
	return [...set].sort();
};
export const POSTER_FRAME = Math.round((TL.find((x) => x.id === 'wedding')!.from + wedPlan.when(Math.min(W('card:shown') + 1.5, W('rows') - 1.0))) * FPS);
export const PLAN_LEN = {byhand: byPlan.len, wedding: wedPlan.len, short: shortPlan.len, appA: appA.len, appB: appB.len, appC: appC.len, appD: appD.len, access: accPlan.len, tower: towPlan.len, proof: prPlan.len};

export const Film6: React.FC<{audio?: boolean; captions?: boolean}> = ({audio = true, captions = true}) => {
	const sec = useCurrentFrame() / FPS;
	const sc = TL.find((s) => sec >= s.from && sec < s.from + s.dur) ?? TL[TL.length - 1];
	const Sc = SCENES[sc.id];
	return <AbsoluteFill style={{background: BG}}>
		<Sc s={sec - sc.from} />
		{captions ? <Caption sec={sec} /> : null}
		{audio ? Object.keys(LINE_AT).filter((id) => vo.lines[id]).map((id) => <Sequence key={id} from={Math.round(LINE_AT[id] * FPS)} durationInFrames={Math.ceil(vo.lines[id].dur * FPS) + 4}><Audio src={staticFile(vo.lines[id].file)} /></Sequence>) : null}
		{audio && vo.music ? <Audio src={staticFile(vo.music)} volume={(f) => { const s = f / FPS, end = TL.at(-1)!.from + TL.at(-1)!.dur; return 0.13 * Math.min(1, s / 0.6) * Math.min(1, Math.max(0, (end - s) / 1.5)); }} /> : null}
	</AbsoluteFill>;
};
