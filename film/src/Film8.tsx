// Version 8: opens on the gateway (an outside agent's real request, allowed and refused), then the cook through the same door,
// the approval, Valio Aimo's side, the rush, the proof, the close. One line of voice per picture: scene lengths follow the voice.
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
import accM from './data6/shot-access.json';
import towM from './data6/shot-tower.json';
import prM from './data6/shot-proof.json';
import fWed from './data6/facts-wedding.json';
import fShort from './data6/facts-short.json';
import fApp from './data6/facts-approve.json';
import fAcc from './data6/facts-access.json';
import voJson from './data8/vo.json';
import gateJson from './data8/gate.json';

const WED = new Shot(wedM as any), SH = new Shot(shortM as any), ASK = new Shot(askM as any), OWN = new Shot(ownM as any), APR = new Shot(aprM as any);
const ACC = new Shot(accM as any), TOW = new Shot(towM as any), PR = new Shot(prM as any);
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

// ---------- version 8: drawn pieces ----------
const DARK = '#0D1117', GO = '#3DD68C', NO = '#FF6B6B', MINT = '#7FD8B8';
const gate = gateJson as any;
const GA = gate.calls[0], GB = gate.calls[1];
const cents = (n: number) => (n / 100).toLocaleString('en-GB', {minimumFractionDigits: 2, maximumFractionDigits: 2});
const fmtEur = (s: string) => {
	const n = Number(String(s).replace(/[\s  ]/g, '').replace(',', '.'));
	return n.toLocaleString('en-GB', {minimumFractionDigits: 2, maximumFractionDigits: 2});
};
// when chunk k of a line is said (seconds into the line); a reading without chunk times falls back to a share of the line
const cue = (id: string, k: number, share: number) => { const w = vo.lines[id]?.words; return w && w.length > k && w.length > 1 ? w[k].s : D(id) * share; };
const Foot: React.FC<{dark?: boolean}> = ({dark}) => <div style={{position: 'absolute', right: 64, bottom: 24, fontFamily: MONO, fontSize: 17, letterSpacing: 0.5, color: dark ? '#6F7885' : '#7A828D', zIndex: 20}}>A prototype on Valio Aimo's challenge data. Not Valio Aimo's system.</div>;
const Big: React.FC<{children: React.ReactNode; x?: number; y: number; w?: number; o?: number; size?: number; color?: string; weight?: number}> = ({children, x = RX, y, w = RW, o = 1, size = 46, color = INK, weight = 680}) =>
	<div style={{position: 'absolute', left: x, top: y, width: w, fontFamily: SANS, fontSize: size, fontWeight: weight, lineHeight: 1.22, letterSpacing: size > 40 ? -0.8 : 0, color, opacity: o}}>{children}</div>;
const Stat: React.FC<{v: React.ReactNode; l: React.ReactNode; o?: number; color?: string; dark?: boolean; size?: number}> = ({v, l, o = 1, color, dark, size = 92}) => <div style={{opacity: o}}>
	<div style={{fontFamily: SANS, fontSize: size, fontWeight: 780, letterSpacing: -2.6, lineHeight: 1, color: color ?? (dark ? '#F4F6F8' : INK), whiteSpace: 'nowrap'}}>{v}</div>
	<div style={{fontFamily: SANS, fontSize: 28, fontWeight: 560, color: dark ? '#AEB7C2' : MUTED, marginTop: 12, whiteSpace: 'nowrap'}}>{l}</div>
</div>;
const Stats: React.FC<{children: React.ReactNode; x?: number; y: number; gap?: number}> = ({children, x = RX, y, gap = 70}) => <div style={{position: 'absolute', left: x, top: y, display: 'flex', gap, alignItems: 'flex-end'}}>{children}</div>;
const Frame: React.FC<{shot: Shot; t: number; view: View; speed?: number}> = ({shot, t, view, speed = 1}) => {
	const w = 1760, h = 990, k = w / view[2];
	return <div style={{position: 'absolute', left: 80, top: 80, width: w, height: h, borderRadius: 16, overflow: 'hidden', boxShadow: '0 24px 60px rgba(10,20,30,.18), 0 0 0 1px rgba(0,0,0,.08)'}}>
		<Img2 shot={shot} t={t} view={view} w={w} h={h} />
		<Finger shot={shot} t={t} view={view} k={k} />
		{speed > 1.2 ? <SpeedBadge speed={speed} /> : null}
	</div>;
};
const Crop: React.FC<{shot: Shot; t: number; at: Rect; x: number; y: number; w: number; maxH?: number; o?: number}> = ({shot, t, at, x, y, w, maxH = 960, o = 1}) => {
	const h = Math.min(maxH, at[3] * w / at[2]);
	return <div style={{position: 'absolute', left: x, top: y, width: w, height: h, borderRadius: 16, overflow: 'hidden', boxShadow: '0 18px 50px rgba(10,20,30,.14), 0 0 0 1px rgba(0,0,0,.08)', opacity: o}}>
		<Img2 shot={shot} t={t} view={[at[0], at[1], at[2]]} w={w} h={h} />
		<Finger shot={shot} t={t} view={[at[0], at[1], at[2]]} k={w / at[2]} />
	</div>;
};
const Tag: React.FC<{children: React.ReactNode; x: number; y: number; dark?: boolean}> = ({children, x, y, dark}) => <div style={{position: 'absolute', left: x, top: y, fontFamily: MONO, fontSize: 22, letterSpacing: 1.5, color: dark ? MINT : ACC_C, fontWeight: 700}}>{children}</div>;

// ---------- scenes (s = seconds from the scene start) ----------
// 0. the door: what this is, before any screen
const SGate: React.FC<{s: number}> = ({s}) => {
	const f = s * FPS, two = (0.4 + cue('g1', 1, 0.5)) * FPS;
	const chip = (t: string, i: number) => <div key={i} style={{opacity: soft(f, two + 6 + i * 5, 8), fontFamily: SANS, fontSize: 29, fontWeight: 600, color: '#E6EAF0', background: '#1B222C', border: '1px solid #2B3440', borderRadius: 14, padding: '15px 24px', whiteSpace: 'nowrap'}}>{t}</div>;
	return <AbsoluteFill style={{background: DARK}}>
		<Tag x={120} y={96} dark>AIMO AGENT GATEWAY</Tag>
		<div style={{position: 'absolute', left: 120, top: 150, width: 1680, fontFamily: SANS, color: '#F4F6F8', fontSize: 86, fontWeight: 780, letterSpacing: -2.8, lineHeight: 1.05, opacity: soft(f, 3, 10)}}>Kitchens are starting to let software do their ordering.</div>
		<div style={{position: 'absolute', left: 120, top: 392, width: 1680, fontFamily: SANS, color: '#C9D1DA', fontSize: 46, fontWeight: 560, lineHeight: 1.25, opacity: soft(f, two, 10)}}>This is the door we built for Valio Aimo to put in front of it.</div>
		<div style={{position: 'absolute', left: 120, top: 560, width: 1680, display: 'flex', alignItems: 'center', gap: 34}}>
			<div style={{display: 'flex', flexDirection: 'column', gap: 14}}>{["A kitchen's own software", 'A chat assistant', "The cook's chat in the web shop"].map(chip)}</div>
			<div style={{fontFamily: MONO, fontSize: 54, color: MINT, opacity: soft(f, two + 22, 8)}}>→</div>
			<div style={{opacity: soft(f, two + 26, 10), border: `3px solid ${MINT}`, borderRadius: 22, padding: '34px 44px', background: 'rgba(127,216,184,.08)'}}>
				<div style={{fontFamily: SANS, fontSize: 60, fontWeight: 780, letterSpacing: -1.8, color: '#F4F6F8', whiteSpace: 'nowrap'}}>One door</div>
				<div style={{fontFamily: SANS, fontSize: 28, color: '#C9D1DA', marginTop: 8, whiteSpace: 'nowrap'}}>the same rules for every caller</div>
			</div>
			<div style={{fontFamily: MONO, fontSize: 54, color: MINT, opacity: soft(f, two + 32, 8)}}>→</div>
			<div style={{opacity: soft(f, two + 36, 8), fontFamily: SANS, fontSize: 34, fontWeight: 600, color: '#E6EAF0', lineHeight: 1.3}}>Valio Aimo's range,<br />prices and stock</div>
		</div>
		<Foot dark />
	</AbsoluteFill>;
};
// 1 and 2. one real request and the gateway's answer (src/data8/gate.json, asked of the running site)
const SCall: React.FC<{s: number; c: any; ok: boolean; line: string}> = ({s, c, ok, line}) => {
	const f = s * FPS, ans = (0.3 + cue(line, 1, 0.5)) * FPS - 4; // the answer lands as the second sentence starts
	const col = ok ? GO : NO;
	const li = c.request.line_items[0], rq = c.request.wholesale.requirements;
	const sub = c.totals.find((t: any) => t.type === 'subtotal').amount;
	const deny = c.rules.find((r: any) => r.outcome === 'deny');
	const msg = (c.messages || []).find((m: any) => m.code === 'policy_denied');
	const passed = c.rules.filter((r: any) => r.outcome === 'pass').length;
	const P: React.CSSProperties = {position: 'absolute', top: 190, height: 590, borderRadius: 20, background: '#141A22', border: '1px solid #26303C', padding: '30px 36px', boxSizing: 'border-box'};
	const lab: React.CSSProperties = {fontFamily: MONO, fontSize: 20, letterSpacing: 2, color: '#8C96A3', fontWeight: 700, marginBottom: 18};
	const code: React.CSSProperties = {margin: 0, fontFamily: MONO, fontSize: 25, lineHeight: 1.52, color: '#C9D1DA', whiteSpace: 'pre'};
	const hi = (t: string, c2 = MINT) => <span style={{color: c2, fontWeight: 700}}>{t}</span>;
	return <AbsoluteFill style={{background: DARK}}>
		<Tag x={100} y={96} dark>{ok ? "A KITCHEN'S OWN SOFTWARE ASKS TO ORDER" : 'THE SAME SOFTWARE, FOR A NUT-FREE KITCHEN'}</Tag>
		<div style={{...P, left: 100, width: 850}}>
			<div style={lab}>THE REQUEST</div>
			<div style={{fontFamily: SANS, fontSize: 36, fontWeight: 650, color: '#F4F6F8', lineHeight: 1.25, marginBottom: 22}}>“{c.request.wholesale.intent}”</div>
			<pre style={code}>{hi('POST /checkout-sessions', '#F4F6F8')}{'\n{\n  "line_items": [{\n    "item": { "id": '}{hi(`"${li.item.id}"`)}{' },\n    "quantity": '}{hi(String(li.quantity))}{'\n  }],\n  "wholesale": {\n    "delivery_date": "'}{c.request.wholesale.delivery_date}{'"'}{rq ? <>{',\n    "requirements": { '}{hi(`"exclude_allergens": ${JSON.stringify(rq.exclude_allergens)}`, '#F5B544')}{' }'}</> : null}{'\n  }\n}'}</pre>
		</div>
		<div style={{...P, left: 990, width: 830, opacity: soft(f, ans, 8), borderColor: col}}>
			<div style={lab}>THE GATEWAY'S ANSWER</div>
			<pre style={code}>{'HTTP '}{c.http}{'\n"status": '}{hi(`"${c.status}"`, col)}{'\n"decision": '}{hi(`"${c.decision}"`, col)}</pre>
			<div style={{display: 'flex', flexWrap: 'wrap', gap: 10, margin: '22px 0 24px'}}>{c.rules.map((r: any) => {
				const k = r.outcome === 'pass' ? GO : r.outcome === 'deny' ? NO : '#F5B544';
				return <div key={r.id} style={{fontFamily: MONO, fontSize: 24, fontWeight: 700, color: k, border: `2px solid ${k}`, borderRadius: 10, padding: '6px 12px', background: r.outcome === 'deny' ? 'rgba(255,107,107,.16)' : 'transparent'}}>{r.id} {r.outcome === 'pass' ? '✓' : r.outcome === 'deny' ? '✗' : '!'}</div>;
			})}</div>
			{ok ? <>
				<div style={{fontFamily: SANS, fontSize: 32, fontWeight: 600, color: '#F4F6F8', lineHeight: 1.3}}>{c.quantity} × {c.title}</div>
				<div style={{fontFamily: SANS, fontSize: 32, color: '#C9D1DA', marginTop: 6}}>€{cents(sub)} ex VAT, price and stock held</div>
				<pre style={{...code, fontSize: 21, color: '#8C96A3', marginTop: 22}}>{'signed offer  '}{String(c.jws).slice(0, 34)}{'…'}</pre>
			</> : <>
				<pre style={code}>{'"code": '}{hi('"policy_denied"', NO)}</pre>
				<div style={{fontFamily: SANS, fontSize: 32, fontWeight: 600, color: '#F4F6F8', lineHeight: 1.3, marginTop: 14}}>{deny ? `${deny.id} ${deny.title}: ` : ''}{String(msg?.content || '').replace(/^Allergen exclusions: /, '')}</div>
			</>}
		</div>
		<div style={{position: 'absolute', left: 990, top: 815, width: 830, opacity: soft(f, ans + 6, 8)}}>
			<div style={{display: 'inline-block', fontFamily: SANS, fontSize: 50, fontWeight: 800, letterSpacing: -1, color: DARK, background: col, borderRadius: 16, padding: '16px 34px', whiteSpace: 'nowrap'}}>{ok ? `May order alone · ${passed} of ${c.rules.length} rules pass` : `Refused by rule ${deny?.id ?? ''}, in code`}</div>
		</div>
		<div style={{position: 'absolute', left: 100, top: 830, width: 850, fontFamily: SANS, fontSize: 25, color: '#8C96A3', lineHeight: 1.35}}>A real request to the running gateway, {new Date(gate.capturedAt).toLocaleDateString('en-GB', {day: 'numeric', month: 'short', year: 'numeric'})}.<br />The answer as it came back, shortened.</div>
		<Foot dark />
	</AbsoluteFill>;
};
const SGateA: React.FC<{s: number}> = ({s}) => <SCall s={s} c={GA} ok line="g2" />;
const SGateB: React.FC<{s: number}> = ({s}) => <SCall s={s} c={GB} ok={false} line="g3" />;

// 3. the cook comes through the same door: one message, one cart
const W = (k: string) => ev(WED, k);
const wedLen = 0.2 + D('k1') + 0.5;
const wedLead = 1.3 + (W('card:shown') - W('ask') - 0.3) / 6;
const wedCardEnd = W('rows') - 0.7;
const wedSp = Math.max(0.55, Math.min(1, (wedCardEnd - W('card:shown')) / Math.max(1, wedLen - wedLead)));
const wedPlan = plan([
	[W('ask') - 1.0, W('ask') + 0.3, 1],
	[W('ask') + 0.3, W('card:shown'), 6],
	[W('card:shown'), wedCardEnd, wedSp],
]);
const weddingCard = String((fWed as any).card || '');
const BANNERS: [RegExp, string][] = [
	[/^Näin tämä toimisi Valio Aimo -verkkokaupassa\. Prototyyppi Valio Aimon haastedatalla, ei Valio Aimon järjestelmä\.?$/, "How this would work in Valio Aimo's web shop. A prototype on Valio Aimo's challenge data, not Valio Aimo's system."],
	[/^Junction 2026 -prototyyppi Valio Aimon haastedatalla\. Ei Valio Aimon järjestelmä\.?$/, "A Junction 2026 prototype on Valio Aimo's challenge data. Not Valio Aimo's system."],
];
const bannerFi = String((fWed as any).banner || '').replace(/\s+/g, ' ').trim();
export const BANNER_EN = bannerFi ? BANNERS.find(([re]) => re.test(bannerFi))?.[1] ?? null : BANNERS[0][1];
const sumOf = (card: string) => {
	const L = card.split('\n').map((x) => x.replace(/[  ]/g, ' ').trim());
	const v = (k: string) => { const i = L.indexOf(k); return i >= 0 ? L[i + 1] : ''; };
	return {day: v('Toimituspäivä'), ex: v('Ostokset (alv 0 %)'), inc: v('Yhteensä (sis. alv)')};
};
const wsum = sumOf(weddingCard);
const SWedding: React.FC<{s: number}> = ({s}) => {
	const t = wedPlan.at(s), sp = wedPlan.sp(s), f = s * FPS;
	const cardAt = wedPlan.when(W('card:shown') + 0.5) * FPS;
	return <>
		<Phone shot={WED} t={t} {...PH} speed={sp} />
		<Head n="THE COOK · THROUGH THE SAME DOOR" title={<>One message in the web shop's chat becomes one cart.</>} />
		<Big y={330} size={34} weight={520} color={MUTED} o={1 - soft(f, cardAt - 4, 6)}>“Wedding on Saturday, {used.guests} people: salmon soup, chicken breast in thyme cream sauce, roast potatoes and carrots, strawberry cream cake and coffee.”</Big>
		<Stats y={350}>
			<Stat v={used.lines} l="lines in the cart" o={soft(f, cardAt, 8)} />
			<Stat v={`${used.seconds} s`} l="from one message" o={soft(f, cardAt + 8, 8)} />
			{wsum.ex ? <Stat v={`€${fmtEur(wsum.ex.replace(' €', ''))}`} l="before VAT, test prices" o={soft(f, cardAt + 16, 8)} /> : null}
		</Stats>
		<Big y={590} size={38} weight={560} o={soft(f, cardAt + 24, 8)}>Products and pack sizes from Valio Aimo's range.<br />Nothing is ordered until the cook confirms.</Big>
		<Foot />
	</>;
};

// 4. a product runs short
const SS = (k: string) => ev(SH, k);
const shLen = 0.2 + D('k2') + 0.5;
const shT0 = SS('scroll:card') + 0.9;
const shortPlan = plan([[shT0, Math.min(SS('swapopen') - 1.2, shT0 + shLen), 1]]);
const short = used.short;
const SShort: React.FC<{s: number}> = ({s}) => {
	const t = shortPlan.at(s), f = s * FPS;
	return <>
		<Phone shot={SH} t={t} {...PH} />
		<Head n="A PRODUCT RUNS SHORT" title={<>The rest comes from Valio Aimo's own range.</>} />
		<Stats y={350}>
			<Stat v={<>{short.have} <span style={{fontSize: 46, color: MUTED, letterSpacing: -1}}>of {short.want} l</span></>} l="of the whipping cream is in stock" o={soft(f, 6, 8)} />
		</Stats>
		<Big y={570} size={38} weight={560} o={soft(f, 16, 8)}>The card names what replaces the rest, and why:<br />the same kind of product, the same diet labels, no new allergens.</Big>
		<Big y={760} size={25} weight={500} color={MUTED} o={soft(f, 26, 8)}>Here it picks from all Valio Aimo products in the challenge data. With Valio Aimo's data it would offer only this customer's contract products.</Big>
		<Foot />
	</>;
};

// 5. over the account's limit: it stops, and someone else has to say yes
const A = (k: string) => ev(ASK, k);
const P = (k: string) => ev(APR, k);
const ownStart = OWN.m.frames.length ? Math.min(...OWN.m.frames) : A('scroll:appr') + 3;
const ownEnd = OWN.m.dur;
const aprStart = APR.m.frames.length ? Math.min(...APR.m.frames) : P('name:focus') - 4;
const appALen = 0.2 + D('c1') + 0.4;
const aT0 = A('scroll:appr') + 0.9;
const appA = plan([[aT0, Math.min(A('turned') - 1.5, aT0 + appALen), 1]]);
const appB = plan([[ownStart + 0.3, Math.min(ownEnd, ownStart + 0.3 + Math.max(2.2, D('c2') + 0.7)), 1]]);
const appC = plan([
	[aprStart + 0.4, aprStart + 1.2, 1],
	[P('name:focus') - 0.3, P('approved') + 0.6, 2.6],
	[A('turned') - 0.3, A('turned') + 1.1, 1],
]);
const appD = plan([
	[A('confirm') - 0.6, A('confirm') + 0.3, 1],
	[A('confirm') + 0.3, A('placed'), 3],
	[A('placed'), A('scroll:placed') + 1.5, 1],
]);
const SApproveA: React.FC<{s: number}> = ({s}) => {
	const t = appA.at(s), f = s * FPS;
	return <>
		<Phone shot={ASK} t={t} {...PH} />
		<Head n="A LIMIT, SET PER ACCOUNT" title={<>Over what this account may order alone, the order stops.</>} />
		<Stats y={360}>
			<Stat v={`€${fmtEur(used.approval.order)}`} l="1,000 l of lactose-free milk" o={soft(f, 6, 8)} />
			<Stat v={`€${fmtEur(used.approval.max)}`} l="the most without approval" o={soft(f, 14, 8)} color={MUTED} />
		</Stats>
		<Big y={600} size={40} weight={600} o={soft(f, 22, 8)}>€{used.approval.over} over. The confirm button waits for an approval.</Big>
		<Foot />
	</>;
};
const SApproveB: React.FC<{s: number}> = ({s}) => {
	const t = appB.at(s);
	return <>
		<Phone shot={OWN} t={t} {...PH} />
		<Head n="NOT THEIR OWN ORDER" title={<>The person who asked cannot approve it.</>} />
		<Big y={360} size={40} weight={600}>The cook opens the approval link on their own phone: refused.</Big>
		<Foot />
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
		<Head n="A SECOND PERSON" title={<>Someone else says yes, on their own phone.</>} x={1230} w={640} y={90} />
		<Big x={1230} y={470} w={620} size={40} weight={600} color={done ? ACC_C : INK}>{done ? <>Approved. The cook's card turns by itself.</> : <>They see what is asked and why, and approve.</>}</Big>
		<Big x={1230} y={760} w={620} size={24} weight={500} color={MUTED}>Here the approver types a name. In Minun Aimo it would be the signed-in user who may approve.</Big>
		<Foot />
	</>;
};
const SApproveD: React.FC<{s: number}> = ({s}) => {
	const t = appD.at(s), sp = appD.sp(s);
	const placed = t >= A('placed');
	return <>
		<Phone shot={ASK} t={t} {...PH} speed={sp} />
		<Head n="ONLY THEN" title={<>Only then does the order go through.</>} />
		<Big y={360} size={44} o={placed ? 1 : 0.25}>Confirmed order {used.approval.order_no}</Big>
		<Big y={440} size={32} weight={520} color={MUTED} o={placed ? 1 : 0}>with the approval on its record.</Big>
		<Foot />
	</>;
};

// 6. Valio Aimo's side: the rules per customer, then the pause
const AC = (k: string) => ev(ACC, k);
const accFinger = ACC.m.events.find((e) => e.name === 'finger')?.t ?? AC('pause') - 0.45;
const accT0 = 1.0, accTAns = Math.min(ACC.m.dur - 0.3, AC('scroll:answers') + 2.6);
const accB = plan([[accFinger - 0.35, AC('scroll:answers') - 0.25, 1]]);
const SAccessA: React.FC<{s: number}> = ({s}) => {
	const f = s * FPS;
	const ctl = ACC.boxOr('ctl', accT0);
	const rows: [string, string][] = [['W2', 'What outside software may do: look up, re-order or order'], ['W3', 'The order size that needs a person'], ['', 'Whether prices and stock are shown'], ['W1', 'Pause all agent ordering']];
	return <>
		<Crop shot={ACC} t={accT0} at={[ctl[0] - 6, ctl[1] - 6, ctl[2] + 12, ctl[3] + 12]} x={110} y={66} w={440} maxH={960} />
		<Head n="VALIO AIMO'S SIDE · /access" title={<>For each customer, Valio Aimo would set the rules.</>} x={640} w={1180} />
		{rows.map(([id, t], i) => <div key={i} style={{position: 'absolute', left: 640, top: 330 + i * 104, width: 1180, display: 'flex', alignItems: 'baseline', gap: 22, opacity: soft(f, 10 + i * 12, 8)}}>
			<div style={{width: 110, fontFamily: MONO, fontSize: 24, fontWeight: 700, color: ACC_C, letterSpacing: 1}}>{id ? `rule ${id}` : ''}</div>
			<div style={{fontFamily: SANS, fontSize: 40, fontWeight: 620, color: INK, letterSpacing: -0.5}}>{t}</div>
		</div>)}
		<Foot />
	</>;
};
const SAccessB: React.FC<{s: number}> = ({s}) => {
	const t = accB.at(s), f = s * FPS;
	const on = ACC.boxOr('on', accT0), order = ACC.boxOr('order', accTAns), look = ACC.boxOr('look', accTAns);
	const shown = (accB.when(AC('changed')) + 0.5) * FPS;
	return <>
		<Head n="VALIO AIMO'S SIDE · /access" title={<>Pause ordering, and the next order is refused.</>} x={110} w={1700} />
		<Crop shot={ACC} t={t} at={[on[0] - 22, on[1] - 44, on[2] + 44, on[3] + 108]} x={110} y={250} w={560} />
		<div style={{position: 'absolute', left: 740, top: 236, fontFamily: MONO, fontSize: 20, letterSpacing: 1.5, color: MUTED, opacity: soft(f, shown, 8)}}>WHAT AN OUTSIDE AGENT GETS NOW</div>
		<Crop shot={ACC} t={accTAns} at={[order[0] - 14, order[1] - 12, order[2] + 28, order[3] + 24]} x={740} y={274} w={1080} o={soft(f, shown, 8)} />
		<Crop shot={ACC} t={accTAns} at={[look[0] - 14, look[1] - 12, look[2] + 28, look[3] + 24]} x={740} y={274 + (order[3] + 24) * 1080 / (order[2] + 28) + 26} w={1080} o={soft(f, shown + 14, 8)} />
		<Big x={110} y={770} w={1700} size={54} weight={740} o={soft(f, shown + 6, 8)}>The order is refused by rule W1. Lookups still answer.</Big>
		<Big x={110} y={860} w={1700} size={30} weight={520} color={MUTED} o={soft(f, shown + 16, 8)}>No approval can change this. Only Valio Aimo can.</Big>
		<Foot />
	</>;
};

// 7. the Monday rush, replayed
const towT2 = 0.3 + D('t1') + GAP; // the second line starts here
const towLen = towT2 + D('t2') + 0.8;
const towT0 = Math.max(1.0, evOr(TOW, 'replay', 0) + 1.5);
const towSp = Math.max(1.3, (TOW.m.dur - 0.4 - towT0) / Math.max(4, towLen - 1.2));
const towPlan = plan([[towT0, TOW.m.dur - 0.4, towSp]]);
const STower: React.FC<{s: number}> = ({s}) => {
	const t = towPlan.at(s), f = s * FPS;
	const at = (k: number, share: number) => (towT2 + cue('t2', k, share)) * FPS;
	return <>
		<Frame shot={TOW} t={t} view={[56, 255, 1328]} speed={towSp} />
		<Tag x={90} y={36}>VALIO AIMO'S SIDE · /tower · A RECORDED RUN, REPLAYED</Tag>
		<div style={{position: 'absolute', left: 80, top: 80 + 990 - 236, width: 1760, height: 236, background: 'rgba(13,17,23,.94)', borderRadius: '0 0 16px 16px', display: 'flex', alignItems: 'center', justifyContent: 'space-around', padding: '0 40px', boxSizing: 'border-box'}}>
			<Stat dark v={`${used.rush.wanted} L`} l={`wanted by ${used.rush.kitchens} kitchens' agents at once`} o={soft(f, at(0, 0), 8)} />
			<Stat dark v={`${used.rush.had} L`} l="in stock, split by contract share" o={soft(f, at(1, 0.42), 8)} />
			<Stat dark v="0 L" l="promised twice" color={GO} o={soft(f, at(3, 0.8), 8)} />
		</div>
	</>;
};

// 8. the proof: the bug the first run found, then the run after the fix
const prT = Math.min(PR.m.dur - 0.4, ev(PR, 'scroll:fix') + 3.5);
const SProofBoth: React.FC<{s: number; fixed: boolean}> = ({s, fixed}) => {
	const f = s * FPS;
	const st = PR.boxOr('story', prT);
	const row = (label: string, col: string, text: React.ReactNode, y: number, o: number) => <div style={{position: 'absolute', left: 110, top: y, width: 1700, display: 'flex', alignItems: 'center', gap: 28, opacity: o}}>
		<div style={{width: 170, textAlign: 'center', fontFamily: MONO, fontSize: 26, fontWeight: 700, letterSpacing: 2, color: '#fff', background: col, borderRadius: 12, padding: '12px 0'}}>{label}</div>
		<div style={{flex: 1, fontFamily: SANS, fontSize: 40, fontWeight: 640, color: INK, lineHeight: 1.2, letterSpacing: -0.5}}>{text}</div>
	</div>;
	const before = fixed ? 1 : soft(f, (0.2 + D('p1') + GAP) * FPS, 8);
	return <>
		<Tag x={110} y={50}>WE TRIED TO BREAK IT FIRST · /proof</Tag>
		<Crop shot={PR} t={prT} at={[st[0] - 20, st[1] - 48, st[2] + 40, st[3] + 60]} x={110} y={96} w={1700} maxH={470} />
		{row('BEFORE', '#B3261E', <>“hazelnut” was not counted under “tree nuts”: {used.proof.first} orders went through.</>, 610, before)}
		{row('AFTER', ACC_C, <>Ruling out a group rules out its members: refused, in code.</>, 720, fixed ? soft(f, 4, 8) : 0)}
		{fixed ? <Big x={110} y={850} w={1700} size={54} weight={760} o={soft(f, (0.2 + cue('p2', 2, 0.7)) * FPS, 8)}>{used.proof.runs} runs on {used.proof.models} AI models: 0 orders broke a rule.</Big> : null}
		<Foot />
	</>;
};
const SProofA: React.FC<{s: number}> = ({s}) => <SProofBoth s={s} fixed={false} />;
const SProofB: React.FC<{s: number}> = ({s}) => <SProofBoth s={s} fixed />;

// close: what was not checked; then the end card. The words on screen are the voice's.
const SClose: React.FC<{s: number}> = ({s}) => <AbsoluteFill style={{background: DARK}}>
	<div style={{position: 'absolute', left: 150, top: 340, width: 1620, fontFamily: SANS, color: '#F4F6F8', opacity: soft(s * FPS, 0, 8)}}>
		<div style={{fontFamily: MONO, fontSize: 24, letterSpacing: 2, color: '#E8B04A', fontWeight: 700, marginBottom: 26}}>NOT CHECKED YET</div>
		<div style={{fontSize: 58, fontWeight: 720, letterSpacing: -1.4, lineHeight: 1.18}}>No cook has used it.<br />It runs on the challenge data, not on Valio Aimo's systems.</div>
	</div>
</AbsoluteFill>;
const SEnd: React.FC<{s: number}> = ({s}) => <AbsoluteFill style={{background: DARK}}>
	<div style={{position: 'absolute', left: 150, top: 250, width: 1620, fontFamily: SANS, color: '#F4F6F8', opacity: soft(s * FPS, 0, 8)}}>
		<div style={{fontFamily: MONO, fontSize: 24, letterSpacing: 2, color: MINT, fontWeight: 700, marginBottom: 26}}>AIMO AGENT GATEWAY</div>
		<div style={{fontSize: 80, fontWeight: 780, letterSpacing: -2.6, lineHeight: 1.06}}>One door for the cook and for any agent.</div>
		<div style={{fontSize: 42, color: '#C9D1DA', marginTop: 40, lineHeight: 1.3}}>Valio Aimo would set the rules. Code checks every order.</div>
		<div style={{fontFamily: MONO, fontSize: 36, color: MINT, marginTop: 60}}>trusted-trade-agents.fly.dev</div>
	</div>
</AbsoluteFill>;

// ---------- the timeline: each scene lasts its footage or its voice, whichever is longer ----------
type TLItem = {id: string; from: number; dur: number; cap: 'right' | 'right2' | 'center' | 'none'};
export const TL: TLItem[] = [];
export const LINE_AT: Record<string, number> = {};
{
	let T = 0;
	const scene = (id: string, len: number, lines: [string, number][], tail = 0.35) => {
		let end = len;
		for (const [l, at] of lines) { LINE_AT[l] = T + at; end = Math.max(end, at + D(l) + tail); }
		TL.push({id, from: T, dur: end, cap: 'none'});
		T += end;
	};
	scene('gate', 0, [['g1', 0.4]], 0.6);
	scene('gateA', 0, [['g2', 0.3]], 0.7);
	scene('gateB', 0, [['g3', 0.3]], 0.9);
	scene('wedding', wedPlan.len, [['k1', 0.2]], 0.5);
	scene('short', shortPlan.len, [['k2', 0.2]], 0.5);
	scene('appA', appA.len, [['c1', 0.2]], 0.4);
	scene('appB', appB.len, [['c2', 0.2]], 0.3);
	scene('appC', appC.len, [['c3', 0.3]], 0.3);
	scene('appD', appD.len, [['c4', 0.2]], 0.5);
	scene('accA', 0, [['d1', 0.2]], 0.6);
	scene('accB', accB.len, [['d2', 0.2]], 1.3);
	scene('tower', 0, [['t1', 0.3], ['t2', towT2]], 0.9);
	scene('proofA', 0, [['p1', 0.2], ['p1b', 0.2 + D('p1') + GAP]], 0.5);
	scene('proofB', 0, [['p2', 0.2]], 1.1);
	scene('close', 0, [['f1', 0.3]], 0.4);
	scene('end', 0, [['f2', 0.3]], 1.3);
}
export const FILM8_DUR = Math.round((TL.at(-1)!.from + TL.at(-1)!.dur) * FPS);

const SCENES: Record<string, React.FC<{s: number}>> = {gate: SGate, gateA: SGateA, gateB: SGateB, wedding: SWedding, short: SShort, appA: SApproveA, appB: SApproveB, appC: SApproveC, appD: SApproveD, accA: SAccessA, accB: SAccessB, tower: STower, proofA: SProofA, proofB: SProofB, close: SClose, end: SEnd};
// which footage frames the cut shows (render8.sh stages only these) and the poster's frame (the allowed request)
const USES: Record<string, [{at: (s: number) => number}, Shot[]]> = {wedding: [wedPlan, [WED]], short: [shortPlan, [SH]], appA: [appA, [ASK]], appB: [appB, [OWN]], appC: [appC, [ASK, APR]], appD: [appD, [ASK]], accB: [accB, [ACC]], tower: [towPlan, [TOW]]};
export const framesUsed = () => {
	const set = new Set<string>([ACC.frameAt(accT0), ACC.frameAt(accTAns), PR.frameAt(prT)]);
	for (const sc of TL) {
		const u = USES[sc.id];
		if (!u) continue;
		for (let F = Math.floor(sc.from * FPS) - 1; F <= Math.ceil((sc.from + sc.dur) * FPS) + 1; F++) { const s = F / FPS - sc.from; if (s < 0 || s >= sc.dur) continue; for (const sh of u[1]) set.add(sh.frameAt(u[0].at(s))); }
	}
	return [...set].sort();
};
export const POSTER_FRAME = Math.round((TL.find((x) => x.id === 'gateA')!.from + TL.find((x) => x.id === 'gateA')!.dur - 0.3) * FPS);

export const Film8: React.FC<{audio?: boolean; captions?: boolean; music?: number}> = ({audio = true, captions = false, music = 0.07}) => {
	const sec = useCurrentFrame() / FPS;
	const sc = TL.find((s) => sec >= s.from && sec < s.from + s.dur) ?? TL[TL.length - 1];
	const Sc = SCENES[sc.id];
	return <AbsoluteFill style={{background: BG}}>
		<Sc s={sec - sc.from} />
		{captions ? <Caption sec={sec} /> : null}
		{audio ? Object.keys(LINE_AT).filter((id) => vo.lines[id]).map((id) => <Sequence key={id} from={Math.round(LINE_AT[id] * FPS)} durationInFrames={Math.ceil(vo.lines[id].dur * FPS) + 4}><Audio src={staticFile(vo.lines[id].file)} /></Sequence>) : null}
		{audio && vo.music && music > 0 ? <Audio src={staticFile(vo.music)} volume={(f) => { const s = f / FPS, end = TL.at(-1)!.from + TL.at(-1)!.dur; return music * Math.min(1, s / 0.6) * Math.min(1, Math.max(0, (end - s) / 1.5)); }} /> : null}
	</AbsoluteFill>;
};
