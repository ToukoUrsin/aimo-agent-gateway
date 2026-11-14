import React from 'react';
import {AbsoluteFill, interpolate, staticFile, useCurrentFrame, useVideoConfig} from 'remotion';
import {C, MONO, SANS, clamp, soft} from '../theme';

export const Backdrop: React.FC<{tint?: string}> = ({tint = C.accent}) => {
	const frame = useCurrentFrame();
	const drift = Math.sin(frame / 110) * 5;
	return (
		<AbsoluteFill style={{background: C.bg, overflow: 'hidden'}}>
			<AbsoluteFill style={{background: `radial-gradient(1200px 760px at ${28 + drift}% 18%, ${tint}1f, transparent 70%), radial-gradient(1000px 700px at ${84 - drift}% 92%, #3DD68C0f, transparent 70%)`}} />
			<AbsoluteFill
				style={{
					backgroundImage: 'linear-gradient(rgba(255,255,255,0.022) 1px, transparent 1px), linear-gradient(90deg, rgba(255,255,255,0.022) 1px, transparent 1px)',
					backgroundSize: '80px 80px',
					maskImage: 'radial-gradient(ellipse at 50% 40%, black 30%, transparent 78%)',
					WebkitMaskImage: 'radial-gradient(ellipse at 50% 40%, black 30%, transparent 78%)',
				}}
			/>
		</AbsoluteFill>
	);
};

export const Grain: React.FC = () => {
	const frame = useCurrentFrame();
	return (
		<AbsoluteFill style={{pointerEvents: 'none', mixBlendMode: 'overlay', opacity: 0.06}}>
			<AbsoluteFill style={{backgroundImage: `url(${staticFile('fx/noise.png')})`, backgroundPosition: `${(frame * 137) % 512}px ${(frame * 251) % 512}px`}} />
		</AbsoluteFill>
	);
};

// scene envelope: soft fade and blur at the edges
export const SceneFade: React.FC<{children: React.ReactNode; inF?: number; outF?: number}> = ({children, inF = 10, outF = 10}) => {
	const frame = useCurrentFrame();
	const {durationInFrames} = useVideoConfig();
	const op = Math.min(interpolate(frame, [0, inF], [0, 1], clamp), interpolate(frame, [durationInFrames - outF, durationInFrames], [1, 0], clamp));
	const blur = (1 - op) * 8;
	return <AbsoluteFill style={{opacity: op, filter: blur > 0.2 ? `blur(${blur}px)` : undefined}}>{children}</AbsoluteFill>;
};

// chapter heading, top left
export const Chapter: React.FC<{n: string; title: string; sub?: string; at?: number; x?: number; y?: number}> = ({n, title, sub, at = 3, x = 80, y = 46}) => {
	const frame = useCurrentFrame();
	const s = soft(frame, at);
	const s2 = soft(frame, at + 6);
	return (
		<div style={{position: 'absolute', left: x, top: y, fontFamily: SANS}}>
			<div style={{display: 'flex', alignItems: 'center', gap: 16, opacity: s, transform: `translateY(${(1 - s) * 12}px)`}}>
				<div style={{fontFamily: MONO, fontSize: 17, color: C.accent, fontWeight: 700, letterSpacing: 1.5, padding: '5px 10px', border: `1px solid ${C.accent}55`, borderRadius: 6, background: C.accentSoft}}>{n}</div>
				<div style={{fontSize: 44, fontWeight: 700, color: C.text, letterSpacing: -1.2}}>{title}</div>
			</div>
			{sub ? <div style={{marginTop: 8, fontSize: 23, color: C.dim, fontWeight: 450, opacity: s2, letterSpacing: -0.2}}>{sub}</div> : null}
		</div>
	);
};

// a big number with a label, appearing at `at`
export const Stat: React.FC<{value: React.ReactNode; label: React.ReactNode; at: number; color?: string; x: number; y: number; w?: number; size?: number; sub?: React.ReactNode}> = ({value, label, at, color = C.text, x, y, w = 440, size = 84, sub}) => {
	const frame = useCurrentFrame();
	const s = soft(frame, at, 16);
	if (frame < at) return null;
	return (
		<div style={{position: 'absolute', left: x, top: y, width: w, opacity: s, transform: `translateY(${(1 - s) * 18}px)`, fontFamily: SANS}}>
			<div style={{fontSize: size, fontWeight: 800, color, letterSpacing: -size * 0.04, lineHeight: 1, fontVariantNumeric: 'tabular-nums'}}>{value}</div>
			<div style={{marginTop: 12, fontSize: 24, color: C.text, fontWeight: 600, letterSpacing: -0.3, lineHeight: 1.25}}>{label}</div>
			{sub ? <div style={{marginTop: 6, fontSize: 19, color: C.dim, lineHeight: 1.35}}>{sub}</div> : null}
		</div>
	);
};

export const Tag: React.FC<{children: React.ReactNode; color?: string; bg?: string; size?: number; style?: React.CSSProperties}> = ({children, color = C.text, bg = 'rgba(255,255,255,0.06)', size = 18, style}) => (
	<div style={{display: 'inline-flex', alignItems: 'center', gap: 8, fontFamily: MONO, fontSize: size, color, background: bg, border: `1px solid ${color}44`, borderRadius: 8, padding: '6px 12px', whiteSpace: 'nowrap', ...style}}>{children}</div>
);

// a row in a list that appears at `at`
export const Row: React.FC<{at: number; children: React.ReactNode; style?: React.CSSProperties}> = ({at, children, style}) => {
	const frame = useCurrentFrame();
	const s = soft(frame, at, 12);
	if (frame < at) return null;
	return <div style={{opacity: s, transform: `translateX(${(1 - s) * 18}px)`, ...style}}>{children}</div>;
};

// small honest note under footage
export const Note: React.FC<{children: React.ReactNode; x: number; y: number; at?: number}> = ({children, x, y, at = 0}) => {
	const frame = useCurrentFrame();
	return <div style={{position: 'absolute', left: x, top: y, fontFamily: MONO, fontSize: 15, color: C.faint, opacity: soft(frame, at), letterSpacing: 0.3}}>{children}</div>;
};

export const Wordmark: React.FC<{size?: number}> = ({size = 96}) => (
	<div style={{display: 'flex', alignItems: 'center', gap: size * 0.22, fontFamily: SANS, fontWeight: 800, fontSize: size, letterSpacing: -size * 0.04, color: C.text}}>
		<div style={{width: size * 0.86, height: size * 0.86, borderRadius: size * 0.22, background: `linear-gradient(140deg, ${C.accent}, #3B6FD9)`, boxShadow: `0 ${size * 0.1}px ${size * 0.45}px ${C.accent}55, inset 0 1px 0 rgba(255,255,255,0.35)`, position: 'relative'}}>
			{/* a door, ajar */}
			<div style={{position: 'absolute', left: '30%', top: '20%', width: '40%', height: '62%', border: `${size * 0.05}px solid #fff`, borderBottom: 'none', borderRadius: `${size * 0.04}px ${size * 0.04}px 0 0`}} />
			<div style={{position: 'absolute', left: '30%', top: '20%', width: '26%', height: '66%', background: '#fff', transform: 'skewY(-14deg)', transformOrigin: 'left top', borderRadius: size * 0.02}} />
		</div>
		<span>
			Aimo Agent Gateway
		</span>
	</div>
);
