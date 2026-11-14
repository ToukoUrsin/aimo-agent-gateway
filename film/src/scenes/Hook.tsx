import React from 'react';
import {AbsoluteFill, Img, staticFile, useCurrentFrame} from 'remotion';
import {SceneFade} from '../components/ui';
import {S} from '../data';
import {C, MONO, SANS, soft} from '../theme';

// 0. Hook: the shift, and the missing door. Behind it, the real control room, out of focus.
export const hookDur = 214;
const Line: React.FC<{at: number; children: React.ReactNode; size?: number; color?: string}> = ({at, children, size = 74, color = C.text}) => {
	const f = useCurrentFrame();
	const s = soft(f, at, 18);
	return <div style={{fontFamily: SANS, fontSize: size, fontWeight: 750, letterSpacing: -size * 0.035, lineHeight: 1.08, color, opacity: s, transform: `translateY(${(1 - s) * 26}px)`, filter: `blur(${(1 - s) * 8}px)`}}>{children}</div>;
};

export const SceneHook: React.FC = () => {
	const f = useCurrentFrame();
	const sh = S.reorder;
	const k = 1920 / 1120;
	return (
		<SceneFade inF={6}>
			<AbsoluteFill>
				<AbsoluteFill style={{opacity: 0.16 + 0.05 * soft(f, 100, 60), filter: 'blur(7px) saturate(0.6)', transform: `scale(${1.08 + f * 0.0004})`}}>
					<Img src={staticFile(sh.frameAt(sh.ev('placed') + 2))} style={{position: 'absolute', left: 0, top: -260 - f * 0.5, width: 1120 * k, height: sh.m.css[1] * k}} />
				</AbsoluteFill>
				<AbsoluteFill style={{background: 'radial-gradient(ellipse at 40% 50%, rgba(7,9,13,0.55), rgba(7,9,13,0.92) 75%)'}} />
				<div style={{position: 'absolute', left: 150, top: 300, width: 1600}}>
					<div style={{fontFamily: MONO, fontSize: 20, letterSpacing: 2, color: C.accent, opacity: soft(f, 2), marginBottom: 26}}>FOOD WHOLESALE · JUNCTION 2026 · VALIO AIMO CHALLENGE “TRUSTED TRADE AGENTS”</div>
					<Line at={6}>Kitchens are starting to hand</Line>
					<Line at={14}>their buying to AI agents.</Line>
					<div style={{height: 34}} />
					<Line at={104} color={C.dim} size={62}>
						A food wholesaler has <span style={{color: C.ask}}>no safe door</span> to let them in.
					</Line>
				</div>
			</AbsoluteFill>
		</SceneFade>
	);
};
