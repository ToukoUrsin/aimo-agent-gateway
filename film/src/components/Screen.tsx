import React from 'react';
import {Img, staticFile} from 'remotion';
import {C, MONO, SANS} from '../theme';
import {Shot, View} from '../shots';

// A window onto recorded footage: shows the part `view` of the page at footage time t, inside a frame
// of w x h film pixels. Draws the recorded pointer and its clicks.
export const Screen: React.FC<{
	shot: Shot;
	t: number;
	view: View;
	x: number;
	y: number;
	w: number;
	h: number;
	label?: React.ReactNode;
	speed?: number;
	radius?: number;
	cursor?: boolean;
	glow?: string;
	opacity?: number;
	style?: React.CSSProperties;
}> = ({shot, t, view, x, y, w, h, label, speed = 1, radius = 16, cursor = true, glow, opacity = 1, style}) => {
	const k = w / view[2]; // film px per CSS px
	const [pw, ph] = shot.m.css;
	return (
		<div style={{position: 'absolute', left: x, top: y, width: w, height: h, opacity, ...style}}>
			{label ? (
				<div style={{position: 'absolute', left: 2, top: -40, fontFamily: MONO, fontSize: 17, letterSpacing: 1.4, textTransform: 'uppercase', color: C.dim, whiteSpace: 'nowrap', display: 'flex', alignItems: 'center', gap: 10}}>{label}</div>
			) : null}
			<div
				style={{
					position: 'absolute',
					inset: 0,
					borderRadius: radius,
					overflow: 'hidden',
					background: '#fff',
					boxShadow: `0 30px 80px rgba(0,0,0,0.55), 0 0 0 1px ${C.line2}${glow ? `, 0 0 0 3px ${glow}, 0 0 60px ${glow}55` : ''}`,
				}}
			>
				<Img src={staticFile(shot.frameAt(t))} style={{position: 'absolute', left: -view[0] * k, top: -view[1] * k, width: pw * k, height: ph * k, maxWidth: 'none'}} />
				{cursor ? <Pointer shot={shot} t={t} view={view} k={k} /> : null}
			</div>
			{speed > 1.2 ? <SpeedBadge speed={speed} /> : null}
		</div>
	);
};

export const SpeedBadge: React.FC<{speed: number; right?: number; top?: number}> = ({speed, right = 14, top = 14}) => (
	<div style={{position: 'absolute', right, top, fontFamily: MONO, fontWeight: 700, fontSize: 20, color: '#fff', background: 'rgba(10,12,16,0.82)', borderRadius: 8, padding: '5px 11px', letterSpacing: 0.5, display: 'flex', gap: 6, alignItems: 'center'}}>
		<span style={{fontSize: 14}}>▶▶</span>
		{Math.abs(speed - Math.round(speed)) < 0.05 ? Math.round(speed) : speed.toFixed(1)}×
	</div>
);

// the recorded mouse: glides to each target, then a ring where it clicked
const Pointer: React.FC<{shot: Shot; t: number; view: View; k: number}> = ({shot, t, view, k}) => {
	const evs = shot.m.events.filter((e) => e.x !== undefined);
	if (!evs.length || t < evs[0].t) return null;
	let pos: [number, number] = [evs[0].x!, evs[0].y!];
	let from: [number, number] = [evs[0].x! + 160, evs[0].y! + 120];
	for (const e of evs) {
		if (e.t > t) break;
		if (e.name === 'move') {
			const p = Math.min(1, (t - e.t) / Math.max(0.05, e.travel ?? 0.5));
			const q = 1 - Math.pow(1 - p, 3);
			pos = [from[0] + (e.x! - from[0]) * q, from[1] + (e.y! - from[1]) * q];
			if (p >= 1) from = [e.x!, e.y!];
		} else {
			pos = [e.x!, e.y!];
			from = pos;
		}
	}
	const clicks = evs.filter((e) => e.click && t >= e.t && t < e.t + 0.6);
	const last = evs.filter((e) => e.t <= t).at(-1)!;
	if (t - last.t > 2.2 && last.click) return null; // pointer leaves after the click
	const sx = (pos[0] - view[0]) * k;
	const sy = (pos[1] - view[1]) * k;
	const s = Math.max(1, Math.min(2.2, k * 0.75));
	return (
		<>
			{clicks.map((c, i) => {
				const p = (t - c.t) / 0.6;
				return <div key={i} style={{position: 'absolute', left: (c.x! - view[0]) * k - 28 * s * p, top: (c.y! - view[1]) * k - 28 * s * p, width: 56 * s * p, height: 56 * s * p, borderRadius: '50%', border: `${3 * s}px solid rgba(90,169,255,${1 - p})`, background: `rgba(90,169,255,${0.25 * (1 - p)})`}} />;
			})}
			<svg style={{position: 'absolute', left: sx - 3 * s, top: sy - 2 * s, width: 24 * s, height: 30 * s, filter: 'drop-shadow(0 3px 6px rgba(0,0,0,0.35))'}} viewBox="0 0 24 30">
				<path d="M3 2 L3 24 L9 18.5 L13 27 L16.5 25.5 L12.6 17 L20.5 17 Z" fill="#111" stroke="#fff" strokeWidth="1.8" strokeLinejoin="round" />
			</svg>
		</>
	);
};

export const fmtSec = (s: number) => `${Math.max(0, s).toFixed(1)} s`;
export {SANS};
