import React from 'react';
import {AbsoluteFill, Img, staticFile, useCurrentFrame} from 'remotion';
import {Chapter, SceneFade} from '../components/ui';
import {S, eur, euros} from '../data';
import {fit, footAt, segs} from '../shots';
import {C, MONO, SANS, soft} from '../theme';
import {ASPECT, Banner, R, Room, SideLabel, W, at, follow} from './room';

// 2. The challenge's own example. The gateway hands over ready options, a rule asks for a person,
//    the approver taps approve on a phone, the order is placed.
const sh = S.challenge;
const ph = S.phone;
const F = sh.m.facts;
const T = segs([
	[sh.ev('typed:start') - 0.3, sh.ev('sent'), 1],
	[sh.ev('sent'), sh.ev('waiting'), 1.5],
	[sh.ev('waiting'), sh.ev('approved'), 1],
	[sh.ev('approved'), sh.ev('placed'), 2],
	[sh.ev('placed'), sh.ev('placed') + 3.2, 1],
]);
export const challengeDur = T.frames + 4;
const fr = (ev: string) => {
	const t = sh.ev(ev);
	for (let i = 1; i < T.knots.length; i++) if (T.knots[i][1] >= t) return Math.round(T.knots[i - 1][0] + ((t - T.knots[i - 1][1]) / (T.knots[i][1] - T.knots[i - 1][1])) * (T.knots[i][0] - T.knots[i - 1][0]));
	return T.frames;
};
const fSent = fr('sent');
const fWait = fr('waiting');
const fPhone = fr('phone:open');
const fPlaced = fr('placed');
// plan_sourcing's options as the agent got them
const opts = (F.sourcing as string).split('|').map((s) => s.trim().match(/^(\w+):\s*(€[\d,.]+)/)).filter(Boolean).map((m) => ({label: m![1], price: euros(m![2])}));
const NAMES: Record<string, string> = {usual_product: 'Their usual product', lowest_price: 'Cheapest that covers all of it', split_with_substitute: 'Split with a substitute', most_reliable: 'Most reliable delivery', finnish_origin: 'Finnish origin', combined: 'Combined', partial_only: 'Partial only'};
const asked = (F.rules as {outcome: string; title: string; detail: string}[]).filter((r) => r.outcome === 'escalate');

export const SceneChallenge: React.FC = () => {
	const f = useCurrentFrame();
	const t = footAt(T.knots, f);
	const left = [
		{f: 0, v: at(sh, 'request', {anchor: 'top', pad: 18})},
		{f: fSent + 8, v: follow(sh), n: 26},
		{f: fWait + 4, v: at(sh, 'waitstep', {anchor: 'top', pad: 40}, 'laststep'), n: 22},
		{f: fPlaced - 30, v: follow(sh), n: 26},
	];
	const right = [
		{f: 0, v: at(sh, 'rules', {anchor: 'top', pad: 18})},
		{f: fSent + 40, v: at(sh, 'offer', {anchor: 'top', pad: 18}), n: 30},
		{f: fWait + 10, v: at(sh, 'offer', {anchor: 'bottom', pad: 12}), n: 30},
		{f: fPlaced + 4, v: (tt: number) => fit(sh.boxOr('orders', tt), ASPECT, sh.m.css, {w: W, anchor: 'bottom', pad: 120}), n: 26},
	];
	// the phone rises over Valio Aimo's side while the approver decides
	const pIn = soft(f, fPhone - 8, 18);
	const pOut = soft(f, fPlaced + 12, 16);
	const pv = pIn * (1 - pOut);
	const tp = t - sh.ev('phone:open');
	const PH = {w: 352, h: 762};
	const kp = PH.w / ph.m.css[0];
	const optsOn = f >= fSent + 70 && f < fWait + 30;
	return (
		<SceneFade>
			<AbsoluteFill>
				<Chapter n="THE CHALLENGE'S EXAMPLE" title="“1,000 litres of lactose-free milk by Monday”" />
				<Room
					sh={sh}
					knots={T.knots}
					left={left}
					right={right}
					rightOpacity={1 - 0.7 * pv}
					leftLabel={<SideLabel who="Hotel's side" what={`its purchasing agent (${F.model})`} />}
					rightLabel={<SideLabel who="Valio Aimo's side" what="signed offer and the rules that ran" color={C.go} />}
					rightGlow={f >= fWait && f < fPlaced ? C.ask : f >= fPlaced ? C.go : undefined}
				/>
				{/* the options the gateway returned, as numbers */}
				{optsOn ? (
					<div style={{position: 'absolute', left: 110, top: 724, width: 812, opacity: soft(f, fSent + 70) * (1 - soft(f, fWait + 20, 10)), fontFamily: SANS, background: 'rgba(9,11,15,0.93)', border: `1px solid ${C.line2}`, borderRadius: 14, padding: '16px 22px', boxShadow: '0 20px 50px rgba(0,0,0,0.5)'}}>
						<div style={{fontFamily: MONO, fontSize: 15, letterSpacing: 1.4, color: C.dim, marginBottom: 10}}>READY OPTIONS FROM THE GATEWAY · 1,000 L · MONDAY</div>
						{opts.map((o, i) => {
							const best = o.price === Math.min(...opts.map((x) => x.price));
							return (
								<div key={o.label} style={{display: 'flex', justifyContent: 'space-between', alignItems: 'baseline', padding: '6px 0', opacity: soft(f, fSent + 76 + i * 8), color: best ? C.go : C.text, fontSize: 27, fontWeight: best ? 700 : 500}}>
									<span>{NAMES[o.label] ?? o.label}</span>
									<span style={{fontVariantNumeric: 'tabular-nums'}}>{eur(o.price * 100)}</span>
								</div>
							);
						})}
						{opts.length > 1 ? <div style={{marginTop: 6, fontSize: 22, color: C.dim}}>Saves {eur((Math.max(...opts.map((x) => x.price)) - Math.min(...opts.map((x) => x.price))) * 100, false)}, same requirements met.</div> : null}
					</div>
				) : null}
				{/* why a person is needed: the rule, as the gateway wrote it */}
				{f >= fWait + 14 && f < fPhone + 40 && asked[0] ? (
					<div style={{position: 'absolute', left: 110, top: 760, width: 812, opacity: soft(f, fWait + 14) * (1 - soft(f, fPhone + 30, 10)), fontFamily: SANS, background: 'rgba(24,18,6,0.94)', border: `1.5px solid ${C.ask}`, borderRadius: 14, padding: '16px 22px'}}>
						<div style={{fontFamily: MONO, fontSize: 15, letterSpacing: 1.4, color: C.ask, marginBottom: 8}}>RULE · NEEDS A PERSON</div>
						<div style={{fontSize: 28, fontWeight: 700, color: C.text}}>{asked[0].title}</div>
						<div style={{fontSize: 21, color: C.dim, marginTop: 4}}>{asked[0].detail.split(':')[0]}.</div>
					</div>
				) : null}
				{pv > 0.01 ? (
					<div style={{position: 'absolute', left: R.x + (R.w - PH.w) / 2, top: 180 + (1 - pv) * 160, width: PH.w + 24, height: PH.h + 24, opacity: pv, borderRadius: 52, background: '#0b0c0f', padding: 12, boxShadow: `0 40px 100px rgba(0,0,0,0.7), 0 0 0 2px #2a2d33, 0 0 50px ${C.ask}33`}}>
						<div style={{position: 'relative', width: PH.w, height: PH.h, borderRadius: 42, overflow: 'hidden', background: '#fff'}}>
							<Img src={staticFile(ph.frameAt(Math.max(0, tp)))} style={{position: 'absolute', left: 0, top: 0, width: ph.m.css[0] * kp, height: ph.m.css[1] * kp}} />
							<PhoneTap tp={tp} k={kp} />
						</div>
						<div style={{position: 'absolute', left: -6, right: -6, top: -44, textAlign: 'center', fontFamily: MONO, fontSize: 16, letterSpacing: 1.4, color: C.ask}}>THE APPROVER'S PHONE</div>
					</div>
				) : null}
				<Banner at={fPlaced + 14} color={C.go} icon="✓">
					Approved by {F.order.approvedBy} · ordered · {eur(F.order.subtotal)}
				</Banner>
			</AbsoluteFill>
		</SceneFade>
	);
};

// a finger tap on the phone where the approver pressed
const PhoneTap: React.FC<{tp: number; k: number}> = ({tp, k}) => {
	const e = ph.m.events.find((x) => x.name === 'approve');
	if (!e) return null;
	const d = tp - e.t;
	if (d < -0.9 || d > 0.8) return null;
	const appear = Math.min(1, (d + 0.9) / 0.4);
	const p = Math.max(0, d / 0.8);
	return (
		<>
			<div style={{position: 'absolute', left: e.x! * k - 30, top: e.y! * k - 30, width: 60, height: 60, borderRadius: 30, background: `rgba(30,30,30,${0.28 * appear * (1 - p)})`, border: `2px solid rgba(255,255,255,${0.8 * appear * (1 - p)})`, transform: `scale(${d < 0 ? 1 : 1 + p})`}} />
		</>
	);
};
