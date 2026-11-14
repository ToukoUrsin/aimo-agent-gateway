import React from 'react';
import {AbsoluteFill, Audio, Sequence, interpolate, staticFile, useCurrentFrame} from 'remotion';
import {Backdrop, Grain} from './components/ui';
import {vo, type VoLine} from './data';
import {C, FPS, SANS, clamp} from './theme';
import {SCENES} from './scenes';

// The demo film: 1920x1080, 30 fps. Scene lengths follow the footage (each scene computes its own from
// the capture's event times), voice lines sit at fixed seconds inside their scene.
export const sceneStarts = () => {
	let t = 0;
	return SCENES.map((s) => {
		const o = {...s, from: t};
		t += s.dur;
		return o;
	});
};
export const FILM_DUR = SCENES.reduce((a, s) => a + s.dur, 0);
const lines = () => vo.lines.filter((l) => SCENES.some((s) => s.id === l.scene));
export const lineFrom = (l: VoLine) => {
	const s = sceneStarts().find((x) => x.id === l.scene)!;
	return s.from + Math.round(l.at * FPS);
};

// burned-in captions: the voice-over, a few words at a time, timed by the TTS alignment
const chunks = (l: VoLine) => {
	type W = (typeof l.words)[number];
	const out: {text: string; s: number; e: number}[] = [];
	const push = (ws: W[]) => ws.length && out.push({text: ws.map((w) => w.w).join(' '), s: ws[0].s, e: ws[ws.length - 1].e});
	const len = (ws: W[]) => ws.map((w) => w.w).join(' ').length;
	const split = (ws: W[]) => {
		if (len(ws) <= 52 || ws.length < 4) return push(ws);
		let best = Math.floor(ws.length / 2);
		let bestScore = 1e9;
		for (let i = 2; i <= ws.length - 2; i++) {
			const score = Math.abs(len(ws.slice(0, i)) - len(ws.slice(i))) - (/[,:;]$/.test(ws[i - 1].w) ? 16 : 0);
			if (score < bestScore) {
				bestScore = score;
				best = i;
			}
		}
		split(ws.slice(0, best));
		split(ws.slice(best));
	};
	let cur: W[] = [];
	for (const w of l.words) {
		cur.push(w);
		if (/[.?!]$/.test(w.w)) {
			split(cur);
			cur = [];
		}
	}
	split(cur);
	return out;
};
// lines that are already on screen as big text are not captioned twice
const onScreen = (l: VoLine) => l.scene === 'hook' || /A safe door for agents/.test(l.text);
const Captions: React.FC = () => {
	const frame = useCurrentFrame();
	for (const l of lines()) {
		if (onScreen(l)) continue;
		const from = lineFrom(l);
		const cs = chunks(l);
		for (let i = 0; i < cs.length; i++) {
			const c = cs[i];
			const a = from + c.s * FPS - 2;
			const next = cs[i + 1] ? from + cs[i + 1].s * FPS - 2 : 1e9;
			const b = Math.min(from + c.e * FPS + 10, next);
			if (frame >= a && frame < b) {
				const op = Math.min(interpolate(frame, [a, a + 3], [0, 1], clamp), interpolate(frame, [b - 3, b], [1, 0], clamp));
				return (
					<div style={{position: 'absolute', left: 0, right: 0, top: 968, display: 'flex', justifyContent: 'center', opacity: op}}>
						<div style={{fontFamily: SANS, fontSize: 36, fontWeight: 600, color: '#F4F6F9', padding: '9px 26px', borderRadius: 12, background: 'rgba(5,6,9,0.86)', border: `1px solid ${C.line}`, letterSpacing: -0.3}}>{c.text}</div>
					</div>
				);
			}
		}
	}
	return null;
};

const voiceOn = (f: number) => lines().some((l) => f >= lineFrom(l) - 8 && f <= lineFrom(l) + l.dur * FPS + 8);
const musicVol = (f: number) => {
	const fade = interpolate(f, [0, 12], [0, 1], clamp) * interpolate(f, [FILM_DUR - 75, FILM_DUR - 2], [1, 0], clamp);
	return fade * (voiceOn(f) ? 0.12 : 0.3);
};

export const Film: React.FC<{audio?: boolean; captions?: boolean}> = ({audio = true, captions = true}) => (
	<AbsoluteFill style={{background: C.bg}}>
		<Backdrop />
		{sceneStarts().map((s) => (
			<Sequence key={s.id} from={s.from} durationInFrames={s.dur} name={s.id}>
				<s.C />
			</Sequence>
		))}
		<AbsoluteFill style={{pointerEvents: 'none', background: 'radial-gradient(ellipse at 50% 50%, transparent 66%, rgba(0,0,0,0.3) 100%)'}} />
		<Grain />
		{captions ? <Captions /> : null}
		{audio
			? lines().map((l, i) => (
					<Sequence key={i} from={lineFrom(l)} durationInFrames={Math.ceil(l.dur * FPS) + 6} name={`vo-${l.scene}`}>
						<Audio src={staticFile(l.file)} />
					</Sequence>
				))
			: null}
		{audio && vo.music ? <Audio src={staticFile(vo.music)} volume={(f) => musicVol(f)} /> : null}
	</AbsoluteFill>
);

// every voice line must end inside its scene and before the next one starts
{
	const st = sceneStarts();
	const ls = lines().sort((a, b) => lineFrom(a) - lineFrom(b));
	ls.forEach((l, i) => {
		const s = st.find((x) => x.id === l.scene)!;
		const end = lineFrom(l) + l.dur * FPS;
		if (end > s.from + s.dur - 3) throw new Error(`VO "${l.text.slice(0, 40)}" ends ${Math.round(end - s.from - s.dur)} frames after scene ${l.scene}`);
		if (ls[i + 1] && end > lineFrom(ls[i + 1]) - 5) throw new Error(`VO "${l.text.slice(0, 40)}" overlaps the next line`);
	});
}
