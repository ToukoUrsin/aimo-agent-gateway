import React from 'react';
import {AbsoluteFill, useCurrentFrame} from 'remotion';
import {Chapter, SceneFade} from '../components/ui';
import {S, eur} from '../data';
import {fit, footAt, segs} from '../shots';
import {C, FPS} from '../theme';
import {ASPECT, Banner, Room, SideLabel, W, at, follow} from './room';

// 1. A plain re-order inside both companies' limits: typed in plain words, placed with nobody involved.
const sh = S.reorder;
const F = sh.m.facts;
const SPEED = 1.5;
const T = segs([
	[sh.ev('typed:start') - 0.35, sh.ev('sent'), 1],
	[sh.ev('sent'), sh.ev('placed'), SPEED],
	[sh.ev('placed'), sh.ev('placed') + 4.2, 1],
]);
export const doorDur = T.frames + 4;
const fSent = T.knots.find((k) => k[1] === sh.ev('sent'))![0];
const fPlaced = T.knots.find((k) => k[1] === sh.ev('placed'))![0];

export const SceneDoor: React.FC = () => {
	const f = useCurrentFrame();
	const t = footAt(T.knots, f);
	const right = [
		{f: 0, v: at(sh, 'rules', {anchor: 'top', pad: 18})},
		{f: fSent + 40, v: at(sh, 'offer', {anchor: 'top', pad: 18}), n: 30},
		{f: fPlaced + 6, v: (tt: number) => fit(sh.boxOr('orders', tt), ASPECT, sh.m.css, {w: W, anchor: 'bottom', pad: 90}), n: 26},
	];
	const left = [
		{f: 0, v: at(sh, 'request', {anchor: 'top', pad: 18})},
		{f: fSent + 8, v: follow(sh), n: 26},
	];
	return (
		<SceneFade>
			<AbsoluteFill>
				<Chapter n="LIVE" title="Aimo Agent Gateway" sub="The door a customer's AI agent orders through. Valio Aimo's challenge data, a real model, nothing staged." />
				<Room
					sh={sh}
					knots={T.knots}
					left={left}
					right={right}
					leftLabel={<SideLabel who="Hotel's side" what={`its own purchasing agent (${F.model})`} />}
					rightLabel={<SideLabel who="Valio Aimo's side" what="the gateway: prices, signed offer, rules, order" color={C.go} />}
					rightGlow={t >= sh.ev('placed') ? C.go : undefined}
				/>
				<Banner at={fPlaced + 10} color={C.go} icon="✓">
					Ordered in {F.seconds.toFixed(1)} s · {eur(F.order.subtotal)} · no person involved
				</Banner>
			</AbsoluteFill>
		</SceneFade>
	);
};
export const DOOR_PLACED_S = fPlaced / FPS;
