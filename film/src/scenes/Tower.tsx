import React from 'react';
import {AbsoluteFill, useCurrentFrame} from 'remotion';
import {Screen} from '../components/Screen';
import {Chapter, SceneFade} from '../components/ui';
import {S} from '../data';
import {View, camAt, footAt, segs, speedAt} from '../shots';
import {C} from '../theme';
import {Banner} from './room';

// 5. Scale: Valio Aimo's control tower while simulated customers' agents order at once. The whole run is
//    recorded; the film plays it fast, with the speed on screen.
const sh = S.tower;
type Sum = {seconds: number; orders: number; alone: number; approved: number; refused: number; short: number; substitutes: number; euros: number; ledger: {intact: boolean}};
const F = sh.m.facts as {floor: {minutes: number; agents: number; summary: Sum}; accounts: unknown[]};
export const TS = F.floor.summary;
const FAST = 24;
const T = segs([
	[sh.ev('start') - 1.1, sh.ev('start') + 0.5, 1],
	[sh.ev('start') + 0.5, sh.ev('ended'), FAST],
	[sh.ev('ended'), sh.ev('ended') + 2.0, 1],
]);
export const towerDur = T.frames + 4;
const fEnd = T.knots.find((k) => k[1] === sh.ev('ended'))![0];
const SC = {x: 80, y: 196, w: 1760, h: 740};
const wide: View = [0, 96, 1440];

export const SceneTower: React.FC = () => {
	const f = useCurrentFrame();
	const t = footAt(T.knots, f);
	const sp = speedAt(T.knots, f);
	const keys = [{f: 0, v: () => wide}];
	return (
		<SceneFade>
			<AbsoluteFill>
				<Chapter n="SCALE" title="One screen for Valio Aimo" sub={`Every customer's agents at once: ${F.floor.agents} simulated agents ordering through the same public door.`} />
				<Screen shot={sh} t={t} view={camAt(keys, f, t)} {...SC} speed={sp} label={<span style={{color: C.dim}}>THE CONTROL TOWER · /tower · A {F.floor.minutes} MINUTE FLOOR RUN</span>} />
				<Banner at={fEnd + 8} color={C.go} icon="✓" y={830}>
					This run: {TS.orders} orders in {Math.round(TS.seconds / 60)} minutes · {TS.alone} needed nobody · ledger {TS.ledger.intact ? 'intact' : 'broken'}
				</Banner>
			</AbsoluteFill>
		</SceneFade>
	);
};
