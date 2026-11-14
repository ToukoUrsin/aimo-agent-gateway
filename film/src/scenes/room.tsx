// Shared layout for the control room shots: the buyer's side (its agent) left, Valio Aimo's side right.
import React from 'react';
import {useCurrentFrame} from 'remotion';
import {Screen} from '../components/Screen';
import {CamKey, Knot, Rect, Shot, View, camAt, fit, footAt, speedAt} from '../shots';
import {C, MONO, SANS, soft} from '../theme';

export const L = {x: 80, y: 196, w: 872, h: 740};
export const R = {x: 968, y: 196, w: 872, h: 740};
export const ASPECT = L.w / L.h;
export const W = 566; // CSS px of the page across one screen (one column of the control room plus margins)

// view helpers on a shot
export const at = (sh: Shot, key: string, o: Parameters<typeof fit>[3] = {}, fallback?: string) => (t: number): View => {
	const b = sh.box(key, t) ?? (fallback ? sh.box(fallback, t) : null) ?? sh.boxOr(key, t);
	return fit(b, ASPECT, sh.m.css, {w: W, ...o});
};
// follow the newest agent step (bottom of the list), or the final answer once it is there
export const follow = (sh: Shot) => (t: number): View => {
	const done = sh.box('donestep', t);
	if (done) return fit(done, ASPECT, sh.m.css, {w: W, anchor: 'top', pad: 70});
	const last = sh.box('laststep', t);
	if (last) return fit(last, ASPECT, sh.m.css, {w: W, anchor: 'bottom', pad: 30});
	return fit(sh.boxOr('request', t), ASPECT, sh.m.css, {w: W, anchor: 'top', pad: 18});
};

export const Room: React.FC<{sh: Shot; knots: Knot[]; left: CamKey[]; right: CamKey[]; leftLabel: React.ReactNode; rightLabel: React.ReactNode; rightOpacity?: number; leftGlow?: string; rightGlow?: string}> = ({sh, knots, left, right, leftLabel, rightLabel, rightOpacity = 1, leftGlow, rightGlow}) => {
	const f = useCurrentFrame();
	const t = footAt(knots, f);
	const sp = speedAt(knots, f);
	return (
		<>
			<Screen shot={sh} t={t} view={camAt(left, f, t)} {...L} label={leftLabel} speed={sp} glow={leftGlow} />
			<Screen shot={sh} t={t} view={camAt(right, f, t)} {...R} label={rightLabel} speed={0} cursor={false} opacity={rightOpacity} glow={rightGlow} />
		</>
	);
};

export const SideLabel: React.FC<{who: string; what: string; color?: string}> = ({who, what, color = C.accent}) => (
	<>
		<span style={{color, fontWeight: 700}}>{who}</span>
		<span style={{color: C.dim, textTransform: 'none', letterSpacing: 0, fontFamily: SANS, fontSize: 19}}>{what}</span>
	</>
);

// a banner over the screens: the outcome in one line
export const Banner: React.FC<{at: number; until?: number; color: string; icon: string; children: React.ReactNode; y?: number}> = ({at: a, until = 1e9, color, icon, children, y = 846}) => {
	const f = useCurrentFrame();
	if (f < a || f > until) return null;
	const s = soft(f, a, 12);
	const out = until < 1e9 ? soft(f, until - 10, 10) : 0;
	return (
		<div style={{position: 'absolute', left: 0, right: 0, top: y, display: 'flex', justifyContent: 'center', opacity: s * (1 - out), transform: `translateY(${(1 - s) * 20}px) scale(${0.96 + 0.04 * s})`}}>
			<div style={{display: 'flex', alignItems: 'center', gap: 18, fontFamily: SANS, fontSize: 34, fontWeight: 700, color: C.text, background: 'rgba(9,11,15,0.92)', border: `1.5px solid ${color}`, borderRadius: 16, padding: '16px 30px', boxShadow: `0 20px 60px rgba(0,0,0,0.6), 0 0 40px ${color}33`, letterSpacing: -0.6}}>
				<div style={{width: 46, height: 46, borderRadius: 23, background: color, color: '#06110B', display: 'flex', alignItems: 'center', justifyContent: 'center', fontSize: 28, fontWeight: 900}}>{icon}</div>
				{children}
			</div>
		</div>
	);
};

export const Mono: React.FC<{children: React.ReactNode; color?: string}> = ({children, color = C.dim}) => <span style={{fontFamily: MONO, color, fontWeight: 500}}>{children}</span>;
