import React from 'react';
import {AbsoluteFill, useCurrentFrame} from 'remotion';
import {Screen} from '../components/Screen';
import {Chapter, SceneFade} from '../components/ui';
import {S} from '../data';
import {fit} from '../shots';
import {C, MONO, SANS, soft} from '../theme';

// 7. Proof: the measured run across models, as /proof publishes it. Then any assistant can connect: the
//    address from the home page goes into Claude or ChatGPT as a connector.
const P = S.proof;
const H = S.home;
const PF = P.m.facts as {totals: {runs: number; orders: number; orders_breaking_a_rule: number; scenarios: number; models: number; providers: number; need_met: number; needed_a_person: number; asked_when_needed: number}; models: {label: string; provider: string; runs: number; broke: number}[]};
const HF = H.m.facts as {mcp: string};
export const T = PF.totals;
export const SPLIT = 222; // frames: proof, then connect
export const proofDur = 366;
const SC = {x: 80, y: 196, w: 1120, h: 740};
const asp = SC.w / SC.h;
const PROVIDER: Record<string, string> = {anthropic: 'Anthropic', openai: 'OpenAI', google: 'Google'};

export const SceneProof: React.FC = () => {
	const f = useCurrentFrame();
	const a = 1 - soft(f, SPLIT - 10, 12);
	const b = soft(f, SPLIT, 16);
	// proof footage at 1x from 0.6 s; the page scrolls from the tiles to the per-model table
	const tp = Math.min(0.6 + f / 30, P.m.dur - 0.4);
	const vp = (() => {
		const big = P.boxOr('big', tp);
		const lead = P.boxOr('lead', tp);
		const h1 = P.boxOr('h1', tp);
		const models = P.boxOr('models', tp);
		const top = P.ev('scroll') < tp ? models : h1;
		const r: [number, number, number, number] = P.ev('scroll') < tp ? [models[0], models[1] - 60, models[2], models[3] + 60] : [lead[0], top[1], lead[2], big[1] + big[3] - top[1]];
		return fit(r, asp, P.m.css, {pad: 24, anchor: 'top'});
	})();
	// home footage from 1.2 s: the "bring your own assistant" door and its Copy click
	const th = Math.min(1.2 + Math.max(0, f - SPLIT) / 30, H.m.dur - 0.4);
	const vh = fit(H.boxOr('mcpdoor', th), asp, H.m.css, {pad: 40});
	const broke = T.orders_breaking_a_rule;
	return (
		<SceneFade>
			<AbsoluteFill>
				{a > 0.01 ? (
					<div style={{position: 'absolute', inset: 0, opacity: a}}>
						<Chapter n="PROOF" title="Measured, not promised" sub={`The same ${T.scenarios} requests, ${T.models} models from ${T.providers} providers. Scored in code from the signed records.`} />
						<Screen shot={P} t={tp} view={vp} {...SC} label={<span style={{color: C.dim}}>THE PROOF PAGE · /proof</span>} />
						<div style={{position: 'absolute', left: 1240, top: 196, width: 600, fontFamily: SANS}}>
							<div style={{fontSize: 150, fontWeight: 800, lineHeight: 1, letterSpacing: -6, color: broke === 0 ? C.go : C.no, opacity: soft(f, 70, 14)}}>{broke}</div>
							<div style={{fontSize: 30, fontWeight: 700, color: C.text, marginTop: 10, opacity: soft(f, 76, 14)}}>orders that broke a rule</div>
							<div style={{fontSize: 22, color: C.dim, marginTop: 6, opacity: soft(f, 80, 14)}}>
								of {T.orders} placed, in {T.runs} runs
							</div>
							<div style={{marginTop: 34}}>
								{PF.models.map((m, i) => (
									<div key={m.label} style={{display: 'flex', alignItems: 'center', gap: 16, padding: '11px 0', borderBottom: `1px solid ${C.line}`, opacity: soft(f, 100 + i * 6, 12)}}>
										<div style={{flex: 1, fontSize: 25, fontWeight: 700, color: C.text}}>{m.label}</div>
										<div style={{fontFamily: MONO, fontSize: 16, color: C.dim, letterSpacing: 1}}>{(PROVIDER[m.provider] || m.provider).toUpperCase()}</div>
										<div style={{fontFamily: MONO, fontSize: 20, fontWeight: 700, color: m.broke === 0 ? C.go : C.no, width: 120, textAlign: 'right'}}>{m.broke} broke</div>
									</div>
								))}
							</div>
						</div>
					</div>
				) : null}
				{b > 0.01 ? (
					<div style={{position: 'absolute', inset: 0, opacity: b}}>
						<Chapter n="CONNECT" title="Bring your own assistant" sub="The same door over MCP. You set what it may order alone." at={SPLIT + 2} />
						<Screen shot={H} t={th} view={vh} {...SC} label={<span style={{color: C.dim}}>THE HOME PAGE</span>} />
						<div style={{position: 'absolute', left: 1240, top: 250, width: 600, fontFamily: SANS}}>
							<div style={{fontFamily: MONO, fontSize: 16, letterSpacing: 1.4, color: C.dim, marginBottom: 18, opacity: soft(f, SPLIT + 30)}}>ADD AS A CONNECTOR IN</div>
							{['Claude', 'ChatGPT', 'any MCP client'].map((n, i) => (
								<div key={n} style={{fontSize: 44, fontWeight: 800, letterSpacing: -1.2, color: i < 2 ? C.text : C.dim, padding: '6px 0', opacity: soft(f, SPLIT + 36 + i * 8, 12), transform: `translateX(${(1 - soft(f, SPLIT + 36 + i * 8, 12)) * 18}px)`}}>
									{n}
								</div>
							))}
							<div style={{marginTop: 36, fontFamily: MONO, fontSize: 25, color: C.accent, padding: '14px 18px', border: `1px solid ${C.accent}55`, borderRadius: 12, background: C.accentSoft, opacity: soft(f, SPLIT + 64, 14), wordBreak: 'break-all'}}>{HF.mcp}</div>
						</div>
					</div>
				) : null}
			</AbsoluteFill>
		</SceneFade>
	);
};
