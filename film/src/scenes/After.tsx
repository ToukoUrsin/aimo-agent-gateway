import React from 'react';
import {AbsoluteFill, useCurrentFrame} from 'remotion';
import {Screen} from '../components/Screen';
import {Chapter, SceneFade} from '../components/ui';
import {S} from '../data';
import {View, fit, footAt, segs} from '../shots';
import {C, MONO, SANS, soft} from '../theme';
import {ASPECT, Banner, Room, SideLabel, W, at, follow} from './room';

// 6. After the order: Valio Aimo recalls a batch. The gateway traces it to every order, blocks the product and
//    sends the customer's agent a signed notice; the agent picks a replacement, a person approves it.
const sh = S.recall;
const tr = S.trace;
type RF = {issue: {chosen: string; by: string; status: string}; recall: {lot: string; product: {title: string}; summary: {orders: number; customers: number; units: number; settled: number}; states: string[]}};
const F = sh.m.facts as RF;
const T = segs([
	[sh.ev('pick') - 0.6, sh.ev('started'), 1],
	[sh.ev('started'), sh.ev('waiting'), 4],
	[sh.ev('waiting'), sh.ev('done'), 8],
	[sh.ev('done'), sh.ev('done') + 0.9, 1],
]);
const A = T.frames;
const B = 156; // the trace page
export const afterDur = A + B;
const fStarted = T.knots.find((k) => k[1] === sh.ev('started'))![0];
const fDone = T.knots.find((k) => k[1] === sh.ev('done'))![0];
const SC = {x: 80, y: 196, w: 1120, h: 740};
const trView: View = [100, 112, 900];

export const SceneAfter: React.FC = () => {
	const f = useCurrentFrame();
	const t = footAt(T.knots, Math.min(f, A));
	const a = 1 - soft(f, A - 8, 10);
	const b = soft(f, A - 4, 12);
	const left = [
		{f: 0, v: at(sh, 'request', {anchor: 'top', pad: 18})},
		{f: fStarted + 4, v: follow(sh), n: 20},
	];
	const right = [{f: 0, v: (tt: number) => fit(sh.boxOr('orders', tt), ASPECT, sh.m.css, {w: W, anchor: 'center'})}];
	const tt = Math.min(0.2 + Math.max(0, f - A) / 30, tr.m.dur - 0.3);
	const ok = F.issue.status === 'resolved';
	return (
		<SceneFade>
			<AbsoluteFill>
				<Chapter n="AFTER" title="When a batch is recalled" sub="Valio Aimo raises it once. The gateway traces the lot to every order and tells each customer's agent." />
				{a > 0.01 ? (
					<div style={{position: 'absolute', inset: 0, opacity: a}}>
						<Room
							sh={sh}
							knots={T.knots}
							left={left}
							right={right}
							leftLabel={<SideLabel who="Hotel's side" what="its agent gets a signed recall notice" />}
							rightLabel={<SideLabel who="Valio Aimo's side" what="a batch on this order is recalled" color={C.no} />}
							rightGlow={f < fDone ? C.no : C.go}
						/>
					</div>
				) : null}
				{b > 0.01 ? (
					<div style={{position: 'absolute', inset: 0, opacity: b}}>
						<Screen shot={tr} t={tt} view={trView} {...SC} cursor={false} label={<span style={{color: C.dim}}>THE RECALL TRACE · {F.recall.lot}</span>} />
						<div style={{position: 'absolute', left: 1240, top: 200, width: 600, fontFamily: SANS}}>
							{[
								['LOT', F.recall.lot, C.text],
								['TRACED TO', `${F.recall.summary.orders} order${F.recall.summary.orders === 1 ? '' : 's'}, ${F.recall.summary.units} units`, C.text],
								['NEW ORDERS', 'blocked for this product', C.no],
								['THE AGENT CHOSE', F.issue.chosen.replace(/^Replace with/, 'A replacement:'), C.text],
								['APPROVED BY', F.issue.by, C.go],
							].map(([k, v, col], i) => (
								<div key={k} style={{padding: '12px 0', borderBottom: `1px solid ${C.line}`, opacity: soft(f, A + 6 + i * 7, 12)}}>
									<div style={{fontFamily: MONO, fontSize: 15, letterSpacing: 1.4, color: C.dim}}>{k}</div>
									<div style={{fontSize: 24, fontWeight: 700, color: col, marginTop: 4, lineHeight: 1.25}}>{v}</div>
								</div>
							))}
						</div>
					</div>
				) : null}
				{ok ? (
					<Banner at={fDone + 2} until={A + 4} color={C.go} icon="✓">
						Recall settled: replaced, {F.issue.by} approved
					</Banner>
				) : null}
			</AbsoluteFill>
		</SceneFade>
	);
};
