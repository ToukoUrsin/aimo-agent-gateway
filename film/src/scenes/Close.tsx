import React from 'react';
import {AbsoluteFill, useCurrentFrame} from 'remotion';
import {SceneFade, Wordmark} from '../components/ui';
import {S} from '../data';
import {C, MONO, SANS, soft} from '../theme';

// 8. Close: the open standards and the path to production, both from the product's own "How it works"
//    page, then the name and the address.
const SF = S.spec.m.facts as {steps: {title: string; text: string}[]; standards: {name: string}[]; oauth: boolean};
export const PUBLIC = 'trusted-trade-agents.fly.dev';
export const closeDur = 300;
const SPLIT = 196;
const short = (n: string) => n.replace('Universal Commerce Protocol', 'UCP').replace(' mandates', '').replace(' and OpenAPI', ', OpenAPI').replace(/^JOSE.*/, 'JOSE signatures');
const STD = [...SF.standards.map((s) => short(s.name)), ...(SF.oauth ? ['OAuth 2.1'] : [])];
// the four steps, first sentence each
const first = (t: string) => t.split(/(?<=\.)\s/)[0];

export const SceneClose: React.FC = () => {
	const f = useCurrentFrame();
	const a = 1 - soft(f, SPLIT - 12, 14);
	const b = soft(f, SPLIT, 20);
	return (
		<SceneFade outF={14}>
			<AbsoluteFill>
				{a > 0.01 ? (
					<div style={{position: 'absolute', inset: 0, opacity: a}}>
						<div style={{position: 'absolute', left: 110, top: 300, width: 640}}>
							<div style={{fontFamily: MONO, fontSize: 18, letterSpacing: 1.6, color: C.accent, marginBottom: 26, opacity: soft(f, 4)}}>OPEN STANDARDS, NO PLATFORM LOCK-IN</div>
							<div style={{display: 'flex', flexWrap: 'wrap', gap: 14}}>
								{STD.map((s, i) => (
									<div key={s} style={{fontFamily: SANS, fontSize: 32, fontWeight: 700, color: C.text, padding: '14px 22px', borderRadius: 14, border: `1px solid ${C.line2}`, background: 'rgba(255,255,255,0.04)', opacity: soft(f, 8 + i * 4, 12), transform: `translateY(${(1 - soft(f, 8 + i * 4, 12)) * 14}px)`}}>
										{s}
									</div>
								))}
							</div>
						</div>
						<div style={{position: 'absolute', left: 860, top: 286, width: 960}}>
							<div style={{fontFamily: MONO, fontSize: 18, letterSpacing: 1.6, color: C.go, marginBottom: 22, opacity: soft(f, 60)}}>FROM HERE TO PRODUCTION</div>
							{SF.steps.map((s, i) => (
								<div key={s.title} style={{display: 'flex', gap: 22, padding: '16px 0', borderBottom: `1px solid ${C.line}`, opacity: soft(f, 66 + i * 12, 14), transform: `translateX(${(1 - soft(f, 66 + i * 12, 14)) * 20}px)`}}>
									<div style={{flex: '0 0 46px', height: 46, borderRadius: 23, border: `1.5px solid ${C.go}`, color: C.go, fontFamily: MONO, fontWeight: 700, fontSize: 22, display: 'flex', alignItems: 'center', justifyContent: 'center'}}>{i + 1}</div>
									<div>
										<div style={{fontFamily: SANS, fontSize: 32, fontWeight: 700, color: C.text, letterSpacing: -0.6}}>{s.title}</div>
										<div style={{fontFamily: SANS, fontSize: 21, color: C.dim, marginTop: 4, lineHeight: 1.35}}>{first(s.text)}</div>
									</div>
								</div>
							))}
						</div>
					</div>
				) : null}
				{b > 0.01 ? (
					<AbsoluteFill style={{opacity: b, display: 'flex', alignItems: 'center', justifyContent: 'center', flexDirection: 'column', transform: `scale(${0.97 + 0.03 * b})`}}>
						<Wordmark size={104} />
						<div style={{fontFamily: SANS, fontSize: 46, fontWeight: 600, color: C.dim, marginTop: 30, letterSpacing: -1, opacity: soft(f, SPLIT + 26)}}>A safe door for agents.</div>
						<div style={{fontFamily: MONO, fontSize: 34, color: C.accent, marginTop: 44, opacity: soft(f, SPLIT + 40), padding: '12px 26px', border: `1px solid ${C.accent}55`, borderRadius: 12, background: C.accentSoft}}>{PUBLIC}</div>
						<div style={{fontFamily: SANS, fontSize: 19, color: C.faint, marginTop: 40, opacity: soft(f, SPLIT + 50)}}>Junction 2026 · Valio Aimo challenge “Trusted Trade Agents” · a prototype on the challenge's data, not a Valio Aimo system</div>
					</AbsoluteFill>
				) : null}
			</AbsoluteFill>
		</SceneFade>
	);
};
