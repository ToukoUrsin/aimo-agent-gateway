import React from 'react';
import {AbsoluteFill, Sequence, useCurrentFrame} from 'remotion';
import {Screen} from '../components/Screen';
import {Chapter, SceneFade} from '../components/ui';
import {S} from '../data';
import {camAt, fit, footAt, segs, speedAt} from '../shots';
import {C, MONO, SANS, soft} from '../theme';
import {ASPECT, Banner, L, R, Room, SideLabel, W, at, follow} from './room';

// 4. Control: an unvetted bot tries six ways around the rules, all refused, suspended on its own.
//    Then Valio Aimo's switch for this customer: off, and even the good agent cannot order.
const rg = S.rogue;
const RF = rg.m.facts;
const A = segs([[rg.ev('picked') + 0.6, rg.ev('picked') + 8.4, 1]]);
const sw = S.switch;
const SF = sw.m.facts;
const B = segs([
	[sw.ev('off') - 0.8, sw.ev('off') + 0.5, 1],
	[sw.ev('off') + 0.5, sw.ev('sent'), 2],
	[sw.ev('sent'), sw.ev('done'), 4],
	[sw.ev('done'), sw.ev('done') + 2.4, 1],
]);
export const controlSplit = A.frames;
export const controlDur = A.frames + B.frames + 4;

const Rogue: React.FC = () => {
	const f = useCurrentFrame();
	const t = footAt(A.knots, f);
	const fr = (tt: number) => (tt - A.knots[0][1]) * 30;
	const t1 = rg.ev('firstrefusal');
	const t2 = rg.ev('done');
	const sus = rg.has('suspended') ? rg.ev('suspended') : null;
	const left = [
		{f: 0, v: at(rg, 'request', {anchor: 'top', pad: 18})},
		{f: fr(rg.ev('sent')) + 2, v: follow(rg), n: 20},
	];
	const agentsView = (tt: number) => fit(rg.boxOr('agents', tt), 872 / 300, rg.m.css, {pad: 12});
	return (
		<>
			<Screen shot={rg} t={t} view={camAt(left, f, t)} {...L} label={<SideLabel who="An unvetted bot" what="scripted attacks over the REST API" color={C.no} />} speed={speedAt(A.knots, f)} glow={t >= t1 ? C.no : undefined} />
			<div style={{position: 'absolute', left: R.x, top: R.y - 40, width: R.w, fontFamily: SANS}}>
				<div style={{fontFamily: MONO, fontSize: 17, letterSpacing: 1.4, color: C.dim, marginBottom: 14}}>WHAT THE GATEWAY REFUSED</div>
				{(RF.refused as string[]).map((r, i) => {
					const ti = t1 + ((t2 - t1) * i) / Math.max(1, RF.refused.length - 1);
					const on = t >= ti;
					return (
						<div key={i} style={{display: 'flex', alignItems: 'center', gap: 16, padding: '9px 0', borderBottom: `1px solid ${C.line}`, opacity: on ? soft(f, fr(ti), 8) : 0.0, transform: `translateX(${on ? (1 - soft(f, fr(ti), 8)) * 20 : 20}px)`}}>
							<div style={{flex: '0 0 34px', height: 34, borderRadius: 8, background: C.noSoft, color: C.no, display: 'flex', alignItems: 'center', justifyContent: 'center', fontWeight: 900, fontSize: 20}}>✕</div>
							<div style={{fontSize: 27, color: C.text, fontWeight: 600, letterSpacing: -0.3}}>{r[0].toUpperCase() + r.slice(1)}</div>
						</div>
					);
				})}
			</div>
			{sus !== null ? (
				<Screen shot={rg} t={t} view={agentsView(t)} x={R.x} y={636} w={872} h={300} cursor={false} opacity={soft(f, fr(sus) - 6, 12)} glow={C.no} label={<span style={{color: C.no}}>SUSPENDED BY THE GATEWAY ITSELF · {RF.agents.find((a: {status: string}) => a.status === 'suspended')?.reason}</span>} />
			) : null}
		</>
	);
};

const Switch: React.FC = () => {
	const f = useCurrentFrame();
	const t = footAt(B.knots, f);
	const fSent = B.knots.find((k) => k[1] === sw.ev('sent'))![0];
	const fDone = B.knots.find((k) => k[1] === sw.ev('done'))![0];
	const left = [
		{f: 0, v: at(sw, 'request', {anchor: 'top', pad: 18})},
		{f: fSent + 4, v: follow(sw), n: 22},
	];
	const RH = 300;
	const rulesView = (tt: number) => fit(sw.boxOr('rules', tt), R.w / RH, sw.m.css, {pad: 10});
	const off = t >= sw.ev('off');
	const sentences = (SF.final as string).split(/(?<=[.!])\s+/);
	return (
		<>
			<Screen shot={sw} t={t} view={camAt(left, f, t)} {...L} label={<SideLabel who="Hotel's side" what="the same good agent, a normal request" />} speed={speedAt(B.knots, f)} />
			<Screen shot={sw} t={t} view={rulesView(t)} x={R.x} y={R.y} w={R.w} h={RH} label={<SideLabel who="Valio Aimo's side" what="one switch per customer" color={C.go} />} glow={off ? C.no : undefined} />
			{f >= fDone ? (
				<div style={{position: 'absolute', left: R.x, top: R.y + RH + 40, width: R.w, opacity: soft(f, fDone, 14), transform: `translateY(${(1 - soft(f, fDone, 14)) * 16}px)`, fontFamily: SANS, background: 'rgba(30,10,10,0.6)', border: `1.5px solid ${C.no}`, borderRadius: 16, padding: '22px 28px'}}>
					<div style={{fontFamily: MONO, fontSize: 16, letterSpacing: 1.4, color: C.no, marginBottom: 10}}>THE AGENT'S ANSWER</div>
					<div style={{fontSize: 32, lineHeight: 1.3, color: C.text, fontWeight: 600, letterSpacing: -0.4}}>“{sentences.slice(0, 2).join(' ')}”</div>
				</div>
			) : null}
		</>
	);
};

export const SceneControl: React.FC = () => {
	const f = useCurrentFrame();
	return (
		<SceneFade>
			<AbsoluteFill>
				{f < controlSplit + 10 ? <Chapter n="CONTROL" title="Rules the agent cannot talk its way around" /> : <Chapter n="CONTROL" title="Valio Aimo keeps the switch" />}
				<Sequence durationInFrames={controlSplit + 6} layout="none">
					<div style={{position: 'absolute', inset: 0, opacity: 1 - soft(f, controlSplit - 6, 12)}}>
						<Rogue />
					</div>
				</Sequence>
				<Sequence from={controlSplit} layout="none">
					<div style={{position: 'absolute', inset: 0, opacity: soft(f, controlSplit, 12)}}>
						<Switch />
					</div>
				</Sequence>
			</AbsoluteFill>
		</SceneFade>
	);
};
export {ASPECT, W};
