import React from 'react';
import {AbsoluteFill, useCurrentFrame} from 'remotion';
import {Screen} from '../components/Screen';
import {Chapter, SceneFade} from '../components/ui';
import {S} from '../data';
import {Rect, camAt, fit, footAt} from '../shots';
import {C, MONO, SANS, soft} from '../theme';

// 3. The order page checks six signatures in the browser; one cent off a price and the check fails.
const sh = S.order;
const F = sh.m.facts;
const tTamper = sh.ev('tamper');
const tFailed = sh.ev('failed');
const tUndo = sh.ev('undo');
// footage at 1x from the start; hold before the undo
const START = 0.1;
export const trustDur = 372; // the footage holds on the failed check before its undo
const SC = {x: 80, y: 196, w: 1120, h: 740};
const asp = SC.w / SC.h;

export const SceneTrust: React.FC = () => {
	const f = useCurrentFrame();
	const t = Math.min(START + f / 30, tUndo - 0.25);
	const fTamper = (tTamper - START) * 30;
	const fFailed = (tFailed - START) * 30;
	const top = (tt: number) => {
		const ev = sh.boxOr('evidence', tt);
		const head = sh.box('head', tt) ?? ev;
		const u: Rect = [ev[0], head[1], ev[2], ev[1] + ev[3] - head[1]];
		return fit(u, asp, sh.m.css, {pad: 16});
	};
	const keys = [
		{f: 0, v: (tt: number) => fit(sh.boxOr('evidence', tt), asp, sh.m.css, {pad: 16})},
		{f: fTamper - 46, v: top, n: 30},
		{f: fFailed + 66, v: (tt: number) => fit(sh.boxOr('guard', tt), asp, sh.m.css, {pad: 16, anchor: 'top'}), n: 34},
	];
	const failed = t >= tFailed;
	const checks = (failed ? F.after : F.before).checks as {ok: boolean; title: string}[];
	return (
		<SceneFade>
			<AbsoluteFill>
				<Chapter n="TRUST" title="Every order proves itself" sub={`Order ${F.label}, opened by anyone. The browser checks the signatures itself.`} />
				<Screen shot={sh} t={t} view={camAt(keys, f, t)} {...SC} label={<span style={{color: C.dim}}>THE ORDER PAGE · {F.label}</span>} glow={failed ? C.no : undefined} />
				<div style={{position: 'absolute', left: 1240, top: 196, width: 600, fontFamily: SANS}}>
					<div style={{fontFamily: MONO, fontSize: 16, letterSpacing: 1.4, color: C.dim, marginBottom: 16}}>CHECKED IN THE BROWSER</div>
					{checks.map((c, i) => (
						<div key={i} style={{display: 'flex', gap: 14, alignItems: 'flex-start', padding: '11px 0', borderBottom: `1px solid ${C.line}`, opacity: soft(f, 4 + i * 5, 10)}}>
							<div style={{flex: '0 0 34px', height: 34, borderRadius: 17, background: c.ok ? C.goSoft : C.noSoft, color: c.ok ? C.go : C.no, display: 'flex', alignItems: 'center', justifyContent: 'center', fontWeight: 900, fontSize: 20, transform: !c.ok ? `scale(${1 + 0.25 * (1 - soft(f, fFailed, 12))})` : undefined}}>{c.ok ? '✓' : '✕'}</div>
							<div style={{fontSize: 23, lineHeight: 1.25, color: c.ok ? C.text : C.no, fontWeight: c.ok ? 500 : 700}}>{c.title}</div>
						</div>
					))}
					<div style={{marginTop: 22, display: 'flex', alignItems: 'center', gap: 14, opacity: soft(f, fTamper - 20)}}>
						<div style={{fontFamily: MONO, fontSize: 16, letterSpacing: 1.2, color: C.dim}}>UNIT PRICE</div>
						<div style={{fontSize: 34, fontWeight: 800, color: C.text, fontVariantNumeric: 'tabular-nums', textDecoration: failed ? 'line-through' : undefined, opacity: failed ? 0.5 : 1}}>{F.before.price}</div>
						{failed ? <div style={{fontSize: 34, fontWeight: 800, color: C.no, opacity: soft(f, fFailed, 8)}}>→ {F.after.price}</div> : null}
					</div>
					<div style={{marginTop: 14, fontSize: 24, fontWeight: 700, color: failed ? C.no : C.go}}>{failed ? F.after.pill : F.before.pill}</div>
				</div>
			</AbsoluteFill>
		</SceneFade>
	);
};
