import {Easing} from 'remotion';
import {loadFont} from '@remotion/google-fonts/Inter';
import {loadFont as loadMono} from '@remotion/google-fonts/JetBrainsMono';

const inter = loadFont('normal', {weights: ['400', '500', '600', '700', '800'], subsets: ['latin', 'latin-ext']});
const mono = loadMono('normal', {weights: ['400', '500', '700'], subsets: ['latin']});
export const SANS = inter.fontFamily;
export const MONO = mono.fontFamily;

export const FPS = 30;
export const C = {
	bg: '#07090D',
	panel: '#0E1218',
	line: 'rgba(255,255,255,0.09)',
	line2: 'rgba(255,255,255,0.16)',
	text: '#F3F5F8',
	dim: '#9AA3B2',
	faint: '#5E6776',
	accent: '#5AA9FF', // the gateway
	accentSoft: 'rgba(90,169,255,0.12)',
	go: '#3DD68C', // placed, passed
	goSoft: 'rgba(61,214,140,0.13)',
	ask: '#F5B544', // needs a person
	askSoft: 'rgba(245,181,68,0.13)',
	no: '#FF6B6B', // refused, failed
	noSoft: 'rgba(255,107,107,0.13)',
};
export const clamp = {extrapolateLeft: 'clamp', extrapolateRight: 'clamp'} as const;
export const ease = {
	out: Easing.bezier(0.16, 1, 0.3, 1),
	inOut: Easing.bezier(0.65, 0, 0.35, 1),
};
// 0→1 over n frames from `at`, eased
export const soft = (frame: number, at: number, n = 14) => ease.out(Math.max(0, Math.min(1, (frame - at) / n)));
export const lerp = (f: number, a: number, b: number, x: number, y: number) => (b === a ? y : x + (y - x) * Math.max(0, Math.min(1, (f - a) / (b - a))));
