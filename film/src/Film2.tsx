import React from 'react';
import {AbsoluteFill, Audio, Img, Sequence, staticFile, useCurrentFrame} from 'remotion';
import {Screen} from './components/Screen';
import {Shot, View, fit} from './shots';
import {C, MONO, SANS, FPS, clamp, soft} from './theme';
import menuMeta from './data2/shot-menu.json';
import sellerMeta from './data2/shot-seller.json';
import towerMeta from './data2/shot-tower2.json';
import proofMeta from './data2/shot-proof2.json';
import homeMeta from './data2/shot-home2.json';
import ledgerMeta from './data2/shot-ledger2.json';
import orderMeta from './data/shot-order.json';
import rogueMeta from './data/shot-rogue.json';
import phoneMeta from './data/shot-phone.json';
import voJson from './data2/vo.json';

const M = new Shot(menuMeta as any), SEL = new Shot(sellerMeta as any), TOW = new Shot(towerMeta as any), P = new Shot(proofMeta as any);
const H = new Shot(homeMeta as any), L = new Shot(ledgerMeta as any), O = new Shot(orderMeta as any), R = new Shot(rogueMeta as any), PH = new Shot(phoneMeta as any);
const menu = (menuMeta as any).facts?.latest ?? {};
const rush = (towerMeta as any).facts?.rush ?? {};
const proof = (proofMeta as any).facts?.proof ?? {};
const anchor = false; // /api/anchor.latest was empty at capture time; no public-chain claim is shown.
const v = voJson as any;
const linesArr: any[] = Object.values(v.lines ?? {});
const starts: Record<string, number> = {h1: .2, h2: 5.1, n1: 10.4, n2: 14.1, r1: 21.5, s1: 28.5, u1: 37.1, u2: 44.4, t1: 52.8, t2: 59.7, t3: 64.1, p1: 68, a1: 81, a2: 86.4, y1: 96, y2: 103, c1: 108};
const duration = 3420;
const aspect = 16 / 9;
const full = {x: 45, y: 135, w: 1830, h: 900};
const frameAt = (s: Shot, t: number) => Math.max(0, Math.min(s.m.dur - .05, t));
const boxView = (s: Shot, key: string, t: number, w?: number): View => fit(s.boxOr(key, t), aspect, s.m.css, {pad: 18, w});
const fullView = (s: Shot, top = 0): View => [0, top, s.m.css[0]];

const Chapter: React.FC<{k: string; title: string; sub?: string}> = ({k, title, sub}) => {
	const f = useCurrentFrame(); const a = soft(f, 2, 12);
	return <div style={{position: 'absolute', left: 72, top: 42, zIndex: 4, opacity: a, fontFamily: SANS, color: '#12201D'}}>
		<div style={{display: 'flex', gap: 14, alignItems: 'center'}}><span style={{fontFamily: MONO, color: '#0B7A62', letterSpacing: 1.5, fontSize: 16}}>{k}</span><b style={{fontSize: 38, letterSpacing: -1}}>{title}</b></div>
		{sub ? <div style={{fontSize: 20, marginTop: 6, color: '#52625D'}}>{sub}</div> : null}
	</div>;
};
const Caption: React.FC = () => {
	const f = useCurrentFrame(); const sec = f / FPS;
	for (const l of linesArr) {
		const st = starts[l.id]; if (st == null || sec < st || sec > st + l.dur) continue;
		let cur: any[] = [];
		for (const w of l.words) { cur.push(w); if (/[.?!]$/.test(w.w) || cur.map(x => x.w).join(' ').length > 48) { const a = cur[0].s, b = cur.at(-1).e; if (sec - st >= a && sec - st <= b) return <div style={{position: 'absolute', zIndex: 8, left: 100, right: 100, bottom: 42, textAlign: 'center'}}><span style={{fontFamily: SANS, fontSize: 34, fontWeight: 650, color: '#fff', background: 'rgba(11,28,24,.9)', borderRadius: 10, padding: '8px 20px'}}>{cur.map(x => x.w).join(' ')}</span></div>; cur = []; } }
		if (cur.length) { const a = cur[0].s, b = cur.at(-1).e; if (sec - st >= a && sec - st <= b) return <div style={{position: 'absolute', zIndex: 8, left: 100, right: 100, bottom: 42, textAlign: 'center'}}><span style={{fontFamily: SANS, fontSize: 34, fontWeight: 650, color: '#fff', background: 'rgba(11,28,24,.9)', borderRadius: 10, padding: '8px 20px'}}>{cur.map(x => x.w).join(' ')}</span></div>; }
	}
	return null;
};
const Stat: React.FC<{x: number; y: number; value: React.ReactNode; label: string; color?: string}> = ({x, y, value, label, color = '#12201D'}) => <div style={{position: 'absolute', left: x, top: y, zIndex: 4, fontFamily: SANS, color}}><div style={{fontSize: 76, fontWeight: 800, letterSpacing: -3, lineHeight: 1}}>{value}</div><div style={{fontSize: 20, fontWeight: 650, marginTop: 8}}>{label}</div></div>;
const Tag: React.FC<{children: React.ReactNode; x: number; y: number; color?: string}> = ({children, x, y, color = '#0B7A62'}) => <div style={{position: 'absolute', left: x, top: y, zIndex: 5, fontFamily: MONO, fontSize: 17, color, background: '#E5F3EE', border: `1px solid ${color}55`, borderRadius: 8, padding: '7px 12px'}}>{children}</div>;

export const Film2: React.FC<{audio?: boolean; captions?: boolean}> = ({audio = true, captions = true}) => {
	const f = useCurrentFrame(); const sec = f / FPS;
	let content: React.ReactNode;
	if (sec < 10) {
		const t = frameAt(M, Math.min(M.m.dur - .2, sec < 3 ? .2 : .2 + (sec - 3) * 1.4));
		const view = sec < 3 ? boxView(M, 'photo', t, 800) : boxView(M, 'main', t, 990);
		content = <><Chapter k="01 · MENU → ORDER" title="A photo becomes a checked order" sub="The kitchen starts with the menu it already has."/><Screen shot={M} t={t} view={view} {...full} cursor={false}/><Tag x={84} y={1000} color="#0B7A62">{menu.guests ?? 180} guests · {menu.line_count ?? 41} checked lines</Tag></>;
	} else if (sec < 21.5) {
		const t = frameAt(M, Math.min(M.m.dur - .2, 2 + (sec - 10) * .65));
		content = <><Chapter k="02 · THE STAKE" title="The gateway does the work" sub="One machine reads the photo. Code checks every line."/><Screen shot={M} t={t} view={boxView(M, 'stats', t, 1080)} {...full} cursor={false}/><Stat x={1300} y={250} value={`${menu.line_count ?? 41}`} label="order lines"/><Stat x={1300} y={500} value={`€${((menu.subtotal ?? 0) / 100).toFixed(2)}`} label="ex VAT · today"/><Tag x={1300} y={760} color="#8A6414">By hand: about 30 minutes · estimate</Tag></>;
	} else if (sec < 37) {
		const t = frameAt(M, Math.min(M.m.dur - .2, 4 + (sec - 21.5) * .5));
		const ts = frameAt(SEL, 1.2);
		content = <><Chapter k="03 · RECIPE LOGIC" title="A shortage follows the recipe" sub="Cooking cream can replace a sauce base. It cannot replace a whipped topping."/><Screen shot={M} t={t} view={boxView(M, 'sub', t, 980)} {...full} cursor={false}/>{sec > 28 ? <Screen shot={SEL} t={ts} view={boxView(SEL, 'prop', ts, 900)} x={1040} y={220} w={820} h={650} cursor={false}/>: null}{sec > 31 ? <Screen shot={PH} t={Math.min(PH.m.dur - .2, 1)} view={boxView(PH, 'main', 1, 360)} x={1420} y={390} w={380} h={520} cursor={false}/>: null}<Tag x={100} y={900} color="#087A5E">SAUCE · cooking cream</Tag><Tag x={500} y={900} color="#9A5D16">TOPPING · real cream kept</Tag></>;
	} else if (sec < 50) {
		const t = frameAt(TOW, Math.min(TOW.m.dur - .2, .5 + (sec - 37) * 1.2));
		const r = rush.stock ?? {};
		content = <><Chapter k="04 · MONDAY RUSH" title="Sixty kitchens. One shortage." sub="The gateway allocates by contract share, while each agent sees only its own answer."/><Screen shot={TOW} t={t} view={fullView(TOW, 90)} {...full} speed={4} cursor={false}/><Stat x={85} y={760} value={`${rush.agents_total ?? 60}`} label="agents"/><Stat x={420} y={760} value={`${r.litres_in_stock ?? 698} L`} label="at Valio Aimo"/><Stat x={760} y={760} value={`${rush.outcomes?.needed_a_person ?? 9}`} label="needed a person"/><Tag x={1260} y={870} color="#0B7A62">{rush.seconds?.first_request_to_last_order ?? 42.1}s · {rush.orders ?? 71} orders · 0 over-allocation</Tag></>;
	} else if (sec < 65) {
		const t = frameAt(O, .3 + (sec - 50) * .33);
		content = <><Chapter k="05 · TRUST" title="Change one cent. Watch it fail." sub="Six signatures are checked in the browser, against the order that was signed."/><Screen shot={O} t={t} view={boxView(O, 'evidence', t, 1020)} {...full} cursor={false}/><Tag x={1250} y={280} color="#0B7A62">6 browser checks</Tag>{sec > 58 ? <Tag x={1250} y={360} color="#B33B36">tampered price → check failed</Tag> : null}{sec > 61 ? <Screen shot={L} t={.2} view={fullView(L, 140)} x={1140} y={580} w={700} h={390} cursor={false}/>: null}</>;
	} else if (sec < 80) {
		const t = frameAt(P, Math.min(P.m.dur - .2, .4 + (sec - 65) * .45)); const T = proof.totals ?? {};
		content = <><Chapter k="06 · PROOF" title="We broke it first" sub={`${T.models ?? 5} models · ${T.providers ?? 3} providers · the score comes from signed records.`}/><Screen shot={P} t={t} view={boxView(P, sec < 72 ? 'big' : 'models', t, 1100)} {...full} cursor={false}/><Stat x={1300} y={250} value={`${T.orders_breaking_a_rule ?? 0}`} label="orders broke a rule" color="#087A5E"/><Tag x={1300} y={500} color="#B33B36">Earlier run · 4 unsafe orders</Tag><Tag x={1300} y={590} color="#087A5E">Re-run · 0 unsafe orders</Tag><Tag x={1300} y={700} color="#0B7A62">{T.runs ?? 130} runs · {T.orders ?? 99} orders</Tag></>;
	} else if (sec < 96) {
		const t = frameAt(R, Math.min(R.m.dur - .2, .5 + (sec - 80) * .25));
		content = <><Chapter k="07 · CONTROL" title="An unvetted bot gets one chance" sub="Self-signed mandate: refused. The gateway suspends it without a person."/><Screen shot={R} t={t} view={boxView(R, 'steps', t, 980)} {...full} cursor={false}/><Tag x={1220} y={300} color="#B33B36">self-signed mandate · refused</Tag><Tag x={1220} y={400} color="#B33B36">suspended automatically</Tag><Tag x={1220} y={560} color="#0B7A62">agent sees its own answer, never stock count</Tag></>;
	} else if (sec < 108) {
		const t = frameAt(H, Math.min(H.m.dur - .2, .8 + (sec - 96) * .25));
		content = <><Chapter k="08 · CONNECT" title="Bring your own assistant" sub="Paste one address into Claude, ChatGPT, or any MCP client."/><Screen shot={H} t={t} view={boxView(H, 'doors', t, 1080)} {...full} cursor={false}/><Tag x={1270} y={320} color="#0B7A62">Claude</Tag><Tag x={1270} y={405} color="#0B7A62">ChatGPT</Tag><Tag x={1270} y={490} color="#0B7A62">MCP · UCP · AP2 · OAuth · Peppol</Tag></>;
	} else {
		content = <><Screen shot={H} t={Math.min(H.m.dur - .2, 1.5)} view={boxView(H, 'hero', 1.5, 1250)} {...full} cursor={false}/><div style={{position: 'absolute', left: 110, top: 220, zIndex: 5, fontFamily: SANS, color: '#12201D'}}><div style={{fontSize: 62, fontWeight: 800, letterSpacing: -2}}>Valio Aimo</div><div style={{fontSize: 38, marginTop: 14}}>Readable by any agent. Owned by Valio.</div><div style={{fontFamily: MONO, color: '#0B7A62', fontSize: 24, marginTop: 30}}>trusted-trade-agents.fly.dev</div></div></>;
	}
	return <AbsoluteFill style={{background: '#EEF3F0'}}>{content}<Caption/>{audio ? linesArr.map((l) => <Sequence key={l.id} from={Math.round(starts[l.id] * FPS)} durationInFrames={Math.ceil(l.dur * FPS) + 4}><Audio src={staticFile(l.file)} /></Sequence>) : null}{audio && v.music ? <Audio src={staticFile(v.music)} volume={(f) => .22 * (f < 40 ? 1 : .85)} /> : null}</AbsoluteFill>;
};

export const FILM2_DUR = duration;
