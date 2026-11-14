import React from 'react';
import {AbsoluteFill, Audio, Img, Sequence, staticFile, useCurrentFrame} from 'remotion';
import {Screen} from './components/Screen';
import {Shot, View, fit} from './shots';
import {MONO, SANS, FPS, soft} from './theme';
import menuMeta from './data2/shot-menu.json';
import towerMeta from './data2/shot-tower2.json';
import proofMeta from './data2/shot-proof2.json';
import homeMeta from './data2/shot-home2.json';
import rogueMeta from './data/shot-rogue.json';
import passkeyFacts from './data2/facts-passkey.json';
import orderDeviceFacts from './data2/facts-order-device.json';
import proofFacts from './data2/facts-proof.json';
import towerFacts from './data2/facts-tower.json';
import menuFacts from './data2/facts-menu.json';
import voJson from './data2/vo.json';

const M = new Shot(menuMeta as any);
const TOW = new Shot(towerMeta as any);
const P = new Shot(proofMeta as any);
const H = new Shot(homeMeta as any);
const R = new Shot(rogueMeta as any);
const PK = new Shot((passkeyFacts as any).meta);
const OD = new Shot((orderDeviceFacts as any).meta);
const menu = (menuFacts as any).latest ?? {};
const rush = (towerFacts as any).rush ?? {};
const proof = (proofFacts as any).proof ?? {};
const U = proof.undeclared ?? {};
const v = voJson as any;
const linesArr: any[] = Object.values(v.lines ?? {});
const starts: Record<string, number> = {
	h1: .20, h2: 5.00, n2: 10.20, d1: 18.00, d2: 24.00, r1: 32.00,
	pk1: 39.00, u1: 49.00, u2: 57.00, t1: 66.00, t2: 70.50,
	p1: 76.00, p2: 85.00, a1: 90.00, y1: 97.00, c1: 107.00,
};
const duration = 3420;
const aspect = 16 / 9;
const full = {x: 80, y: 130, w: 1760, h: 990};
const frameAt = (s: Shot, t: number) => Math.max(0, Math.min(s.m.dur - .05, t));
const panel = (s: Shot, key: string, t: number, w = 1250, anchor: 'top' | 'center' | 'bottom' = 'center'): View => fit(s.boxOr(key, t), aspect, s.m.css, {pad: 18, w, anchor});
// The home capture begins on the hero, then records a scroll into the ways-in
// list. Keep the hero shot on the actual first frame and use its stable box;
// the tracked box after the scroll describes the same element off-screen.
const homeHeroView: View = fit([104, 87, 1072, 595], aspect, H.m.css, {pad: 18, w: 1450, anchor: 'top'});

const Chapter: React.FC<{k: string; title: string; sub?: string}> = ({k, title, sub}) => {
	const f = useCurrentFrame();
	return <div style={{position: 'absolute', left: 74, top: 38, zIndex: 6, opacity: soft(f, 2, 12), fontFamily: SANS, color: '#10201C'}}>
		<div style={{display: 'flex', gap: 14, alignItems: 'center'}}><span style={{fontFamily: MONO, color: '#0A7B61', letterSpacing: 1.5, fontSize: 16}}>{k}</span><b style={{fontSize: 38, letterSpacing: -1}}>{title}</b></div>
		{sub ? <div style={{fontSize: 20, marginTop: 6, color: '#52625D'}}>{sub}</div> : null}
	</div>;
};
const Caption: React.FC = () => {
	const sec = useCurrentFrame() / FPS;
	for (const l of linesArr) {
		const st = starts[l.id]; if (st == null || sec < st || sec > st + l.dur) continue;
		let cur: any[] = [];
		for (const w of l.words) {
			cur.push(w);
			if (/[.?!]$/.test(w.w) || cur.map(x => x.w).join(' ').length > 48) {
				const a = cur[0].s, b = cur.at(-1).e;
				if (sec - st >= a && sec - st <= b) return <CaptionText text={cur.map(x => x.w).join(' ')} />;
				cur = [];
			}
		}
		if (cur.length) { const a = cur[0].s, b = cur.at(-1).e; if (sec - st >= a && sec - st <= b) return <CaptionText text={cur.map(x => x.w).join(' ')} />; }
	}
	return null;
};
const CaptionText: React.FC<{text: string}> = ({text}) => <div style={{position: 'absolute', zIndex: 20, left: 90, right: 90, bottom: 36, textAlign: 'center'}}><span style={{fontFamily: SANS, fontSize: 34, lineHeight: 1.15, fontWeight: 650, color: '#fff', background: 'rgba(11,28,24,.92)', borderRadius: 10, padding: '8px 20px'}}>{text}</span></div>;
const Tag: React.FC<{children: React.ReactNode; x: number; y: number; color?: string; w?: number}> = ({children, x, y, color = '#0B7A62', w}) => <div style={{position: 'absolute', left: x, top: y, zIndex: 10, width: w, fontFamily: MONO, fontSize: 18, lineHeight: 1.25, color, background: '#E5F3EE', border: `1px solid ${color}66`, borderRadius: 9, padding: '8px 13px'}}>{children}</div>;
const Big: React.FC<{x: number; y: number; value: React.ReactNode; label: string; color?: string}> = ({x, y, value, label, color = '#10201C'}) => <div style={{position: 'absolute', left: x, top: y, zIndex: 10, fontFamily: SANS, color}}><div style={{fontSize: 72, fontWeight: 850, letterSpacing: -3, lineHeight: .95}}>{value}</div><div style={{fontSize: 20, fontWeight: 700, marginTop: 8}}>{label}</div></div>;
const ZeroCard: React.FC<{x: number; y: number; title: string; sub: string}> = ({x, y, title, sub}) => <div style={{position: 'absolute', left: x, top: y, zIndex: 10, width: 360, padding: '16px 18px', borderRadius: 12, background: 'rgba(255,255,255,.96)', border: '1px solid #C8D9D3', boxShadow: '0 10px 25px #1232  ', fontFamily: SANS, color: '#10201C'}}><div style={{fontSize: 64, lineHeight: .9, fontWeight: 850, color: '#087A5E'}}>0</div><div style={{fontSize: 20, fontWeight: 750, marginTop: 9}}>{title}</div><div style={{fontSize: 16, color: '#52625D', marginTop: 5}}>{sub}</div></div>;

export const Film2: React.FC<{audio?: boolean; captions?: boolean}> = ({audio = true, captions = true}) => {
	const sec = useCurrentFrame() / FPS;
	let content: React.ReactNode;
	if (sec < 10) {
		const t = frameAt(M, sec < 3.2 ? .4 : .8 + (sec - 3.2) * 1.15);
		content = sec < 3.2
			? <><Chapter k="01 · MENU → ORDER" title="A photo becomes a checked order" sub="Saturday's wedding. The kitchen starts with the menu it already has."/><Screen shot={M} t={t} view={panel(M, 'photo', t, 900)} x={150} y={160} w={1620} h={912} cursor={false}/><Tag x={150} y={870}>180 guests · wedding dinner</Tag></>
			: <><Chapter k="01 · MENU → ORDER" title="A photo becomes a checked order" sub="The model reads it. Code checks every line."/><Screen shot={M} t={t} view={panel(M, 'main', t, 1250)} x={80} y={140} w={1760} h={990} cursor={false}/><Big x={1260} y={245} value={menu.line_count ?? 41} label="checked lines"/><Tag x={1260} y={430}>{(menuReadPlan(menu) || 8.9).toFixed(1)}s read + plan</Tag></>;
	} else if (sec < 18) {
		const t = frameAt(H, .05);
		content = <><Chapter k="02 · THE STAKE" title="Aimo owns the door" sub="The gateway is the interface between a kitchen's agent and Valio Aimo."/><Screen shot={H} t={t} view={homeHeroView} {...full} cursor={false}/><Tag x={124} y={820} w={720}>AI agents are starting to order. Whoever defines the door owns the customer.</Tag></>;
	} else if (sec < 24) {
		const t = frameAt(P, .5);
		content = <><Chapter k="03 · THE MISS" title="Silence is not a pass" sub="Four tests. The first one is the data itself."/><Screen shot={P} t={t} view={panel(P, 'matrix', t, 1200, 'top')} {...full} cursor={false}/><Big x={130} y={790} value={`${(U.undeclared ?? 4233).toLocaleString()} / ${(U.food_products ?? 13546).toLocaleString()}`} label="food products with no allergen declaration" color="#A04A19"/><Big x={1300} y={790} value={`${U.pct ?? 31.2}%`} label="of the catalogue" color="#A04A19"/></>;
	} else if (sec < 32) {
		const t = frameAt(P, .5);
		content = <><Chapter k="03 · THE MISS" title="A salmon can say nothing" sub="So the gateway asks a person once, and remembers who cleared it."/><Screen shot={P} t={t} view={panel(P, 'matrix', t, 1180, 'top')} {...full} cursor={false}/><Tag x={120} y={810} color="#A04A19" w={500}>SALMON FILLET · allergens not declared</Tag><Tag x={670} y={810} color="#A04A19" w={500}>ALMONDS · allergens not declared</Tag><Tag x={1220} y={810} color="#A04A19" w={500}>DIJON MUSTARD · allergens not declared</Tag></>;
	} else if (sec < 39) {
		const t = frameAt(M, 5.7 + (sec - 32) * .65);
		content = <><Chapter k="04 · RECIPE LOGIC" title="A shortage follows the recipe" sub="Cooking cream works in the sauce. It cannot replace a whipped topping."/><Screen shot={M} t={t} view={panel(M, 'sub', t, 1200)} {...full} cursor={false}/><Tag x={160} y={850}>SAUCE · cooking cream</Tag><Tag x={800} y={850} color="#9A5D16">TOPPING · real cream kept</Tag></>;
	} else if (sec < 49) {
		const t = frameAt(PK, sec < 43 ? 1.1 : 2.5);
		const v0 = sec < 43 ? panel(PK, 'main', t, 390, 'top') : panel(PK, 'pk', t, 360, 'center');
		content = <><Chapter k="05 · THE APPROVER" title="The person signs on their own device" sub="The agent can ask. It cannot make this signature."/><Screen shot={PK} t={t} view={v0} x={520} y={158} w={880} h={495} cursor={false}/>{sec < 43 ? <Tag x={1500} y={330}>TAP APPROVE</Tag> : <Tag x={1480} y={330}>SIGNED ON THIS DEVICE</Tag>}<Tag x={530} y={730} color="#6D5B16" w={850}>Fingerprint, face, or screen lock. The key never leaves the device.</Tag></>;
	} else if (sec < 57) {
		const t = frameAt(TOW, .5 + (sec - 49) * 1.1);
		content = <><Chapter k="06 · MONDAY RUSH" title="Sixty kitchens. One shortage." sub="Aimo sees the floor. Each agent sees only its own answer."/><Screen shot={TOW} t={t} view={panel(TOW, 'cells', t, 1440)} {...full} speed={4} cursor={false}/><Big x={150} y={825} value={rush.agents_total ?? 60} label="agents at once"/><Big x={610} y={825} value={`${rush.stock?.litres_in_stock ?? 698} L`} label="in stock"/><Tag x={1250} y={850}>4× replay · allocation in code</Tag></>;
	} else if (sec < 66) {
		const t = frameAt(TOW, 6 + (sec - 57) * .7);
		content = <><Chapter k="06 · MONDAY RUSH" title="Every litre is allocated once" sub="Contract share first. Substitutes only when the recipe allows them."/><Screen shot={TOW} t={t} view={panel(TOW, 'cells', t, 1440)} {...full} speed={4} cursor={false}/><Big x={150} y={825} value="42s" label="first request to last order"/><Big x={720} y={825} value="9 / 60" label="needed a person"/><Tag x={1390} y={850}>0 over-allocation</Tag></>;
	} else if (sec < 70.5) {
		const t = frameAt(OD, 2.6 + (sec - 66) * .8);
		content = <><Chapter k="07 · TRUST" title="Change one cent. Watch it fail." sub="The order page fetches the signed record and verifies it in your browser."/><Screen shot={OD} t={t} view={panel(OD, 'checks', t, 1200)} {...full} cursor={false}/><Tag x={1390} y={245}>7 browser checks</Tag><Tag x={1390} y={360} color="#B33B36">price − €0.01 → failed</Tag></>;
	} else if (sec < 76) {
		const t = frameAt(OD, 4.8 + (sec - 70.5) * .35);
		content = <><Chapter k="07 · TRUST" title="The signature binds the order" sub="Restore the price and every check passes again."/><Screen shot={OD} t={t} view={panel(OD, 'checks', t, 1200)} {...full} cursor={false}/><Tag x={1390} y={245}>7 / 7 pass</Tag><Tag x={1390} y={360} color="#087A5E">the device signature is checked too</Tag></>;
	} else if (sec < 85) {
		const t = frameAt(P, 4.8);
		content = <><Chapter k="08 · WE BROKE IT FIRST" title="The models found our bug" sub="The score is computed from signed records, not graded by a model."/><Screen shot={P} t={t} view={panel(P, 'models', t, 1200)} {...full} cursor={false}/><Tag x={145} y={820} color="#B33B36" w={530}>FIRST RUN · 4 unsafe orders</Tag><Tag x={760} y={820} color="#087A5E" w={530}>AFTER THE FIX · 0 unsafe orders</Tag></>;
	} else if (sec < 90) {
		const t = frameAt(P, .5);
		content = <><Chapter k="08 · WE BROKE IT FIRST" title="Four tests. Every one is zero." sub="130 runs across five models. The browser can open every record."/><Screen shot={P} t={t} view={panel(P, 'matrix', t, 1200, 'top')} {...full} cursor={false}/><ZeroCard x={90} y={790} title="orders broke a rule" sub="99 orders · 5 models · 130 runs"/><ZeroCard x={515} y={790} title="menu checks failed" sub="4,124 checks · 60 menus"/><ZeroCard x={940} y={790} title="litres promised twice" sub="1,472 L wanted · 698 L in stock"/><ZeroCard x={1365} y={790} title="suite checks failed" sub="331 checks · 8 suites"/></>;
	} else if (sec < 97) {
		const t = frameAt(R, .8 + (sec - 90) * .35);
		content = <><Chapter k="09 · CONTROL" title="An unvetted bot gets one chance" sub="Self-signed mandate: refused. The gateway suspends it without a person."/><Screen shot={R} t={t} view={panel(R, 'steps', t, 1250)} {...full} cursor={false}/><Tag x={1400} y={280} color="#B33B36">refused</Tag><Tag x={1400} y={410} color="#B33B36">suspended automatically</Tag></>;
	} else if (sec < 107) {
		const t = frameAt(H, 2.8);
		content = <><Chapter k="10 · CONNECT" title="Bring your own assistant" sub="One address. The same rules."/><Screen shot={H} t={t} view={panel(H, 'hero', t, 1450, 'top')} {...full} cursor={false}/><Tag x={155} y={790} color="#0B7A62" w={970}>https://trusted-trade-agents.fly.dev/mcp</Tag><Tag x={1160} y={790} w={610}>Claude · ChatGPT · MCP · UCP · AP2 · OAuth · Peppol</Tag></>;
	} else {
		content = <div style={{position: 'absolute', inset: 0, background: '#10201C', color: '#F4FAF7', fontFamily: SANS}}><div style={{position: 'absolute', left: 150, top: 220}}><div style={{fontSize: 82, fontWeight: 850, letterSpacing: -3}}>Valio Aimo</div><div style={{fontSize: 46, marginTop: 18, color: '#BDE8D9'}}>Readable by any agent. Owned by Valio.</div><div style={{fontFamily: MONO, color: '#69D6B0', fontSize: 28, marginTop: 36}}>trusted-trade-agents.fly.dev</div></div><div style={{position: 'absolute', right: 150, bottom: 150, fontFamily: MONO, fontSize: 20, color: '#8FBCAF'}}>Aimo Agent Gateway · Junction 2026</div></div>;
	}
	return <AbsoluteFill style={{background: '#EEF3F0'}}>{content}{captions ? <Caption/> : null}{audio ? linesArr.map((l) => <Sequence key={l.id} from={Math.round(starts[l.id] * FPS)} durationInFrames={Math.ceil(l.dur * FPS) + 6}><Audio src={staticFile(l.file)} /></Sequence>) : null}{audio && v.music ? <Audio src={staticFile(v.music)} volume={(f) => .20 * (f < 45 ? 1 : .85)} /> : null}</AbsoluteFill>;
};

const menuReadPlan = (m: any) => Number(((m.phases?.read || 0) + (m.phases?.plan || 0)).toFixed(1));
export const FILM2_DUR = duration;
