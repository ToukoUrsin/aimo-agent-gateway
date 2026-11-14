// Recorded shots (capture/capture.mjs): frame times, element boxes and events of the real product's pages.
// Film code frames page elements by name and maps film time onto footage time, so a re-capture needs no edit here.
import {ease} from './theme';

export type Ev = {t: number; name: string; x?: number; y?: number; to?: string; travel?: number; click?: boolean; box?: number[]};
export type ShotMeta = {
	name: string;
	dir: string;
	css: [number, number];
	scale: number;
	dur: number;
	frames: number[];
	events: Ev[];
	boxT: number[];
	boxes: Record<string, (number[] | null)[]>;
	facts: any;
};
export type Rect = [number, number, number, number]; // x, y, w, h in CSS px of the page

export class Shot {
	m: ShotMeta;
	order: number[];
	constructor(m: ShotMeta) {
		this.m = m;
		// screencast frames can arrive a little out of order: index them by time
		this.order = m.frames.map((t, i) => [t, i]).sort((a, b) => a[0] - b[0]).map((x) => x[1]);
	}
	get name() {
		return this.m.name;
	}
	ev(name: string, nth = 0): number {
		const e = this.m.events.filter((x) => x.name === name)[nth];
		if (!e) throw new Error(`shot ${this.m.name}: no event ${name}`);
		return e.t;
	}
	has(name: string) {
		return this.m.events.some((x) => x.name === name);
	}
	// the screencast frame on screen at footage time t
	frameAt(t: number): string {
		let lo = 0;
		for (let k = 0; k < this.order.length; k++) {
			if (this.m.frames[this.order[k]] <= t) lo = k;
			else break;
		}
		const i = this.order[lo];
		return `${this.m.dir}/f${String(i).padStart(5, '0')}.jpg`;
	}
	// element box at footage time t (the last sample at or before t where it existed)
	box(key: string, t: number): Rect | null {
		const arr = this.m.boxes[key];
		if (!arr) throw new Error(`shot ${this.m.name}: no tracked element ${key}`);
		// the latest sample at or before t; an element that does not exist yet has no box
		let i = 0;
		while (i + 1 < this.m.boxT.length && this.m.boxT[i + 1] <= t) i++;
		return (arr[i] ?? null) as Rect | null;
	}
	// for framing: the box at t, or the first box it ever had
	boxOr(key: string, t: number): Rect {
		const b = this.box(key, t) ?? (this.m.boxes[key].find((x) => x) as Rect | undefined);
		if (!b) throw new Error(`shot ${this.m.name}: ${key} never on screen`);
		return b;
	}
}

// --- time: film frames (scene-local) -> footage seconds, piecewise linear ---
export type Knot = [number, number]; // [scene frame, footage seconds]
export const footAt = (knots: Knot[], f: number) => {
	if (f <= knots[0][0]) return knots[0][1];
	for (let i = 1; i < knots.length; i++) {
		const [f1, t1] = knots[i];
		const [f0, t0] = knots[i - 1];
		if (f <= f1) return t0 + ((t1 - t0) * (f - f0)) / (f1 - f0);
	}
	return knots[knots.length - 1][1];
};
// playback speed at frame f (footage seconds per film second)
export const speedAt = (knots: Knot[], f: number, fps = 30) => {
	for (let i = 1; i < knots.length; i++) {
		if (f < knots[i][0] && f >= knots[i - 1][0]) return ((knots[i][1] - knots[i - 1][1]) / (knots[i][0] - knots[i - 1][0])) * fps;
	}
	return 0;
};
// knots from a list of segments: [footage from, footage to, speed]; returns knots and the frame count
export const segs = (list: [number, number, number][], fps = 30, start = 0): {knots: Knot[]; frames: number} => {
	const knots: Knot[] = [[start, list[0][0]]];
	let f = start;
	for (const [a, b, sp] of list) {
		if (knots[knots.length - 1][1] !== a) knots.push([f, a]);
		f += ((b - a) / sp) * fps;
		knots.push([Math.round(f), b]);
	}
	return {knots, frames: Math.round(f)};
};

// --- camera: what part of the page fills the frame ---
export type View = [number, number, number]; // left, top, width in CSS px (height follows the frame's aspect)
export type FitOpt = {pad?: number; w?: number; anchor?: 'top' | 'center' | 'bottom'; dx?: number; dy?: number};
export const fit = (b: Rect, aspect: number, page: [number, number], o: FitOpt = {}): View => {
	const pad = o.pad ?? 14;
	let w = o.w ?? Math.max(b[2] + 2 * pad, (b[3] + 2 * pad) * aspect);
	w = Math.min(w, page[0]);
	const h = w / aspect;
	let x = b[0] + b[2] / 2 - w / 2 + (o.dx ?? 0);
	let y = o.anchor === 'top' ? b[1] - pad : o.anchor === 'bottom' ? b[1] + b[3] + pad - h : b[1] + b[3] / 2 - h / 2;
	y += o.dy ?? 0;
	x = Math.max(0, Math.min(page[0] - w, x));
	y = Math.max(0, Math.min(page[1] - h, y));
	return [x, y, w];
};
export type CamKey = {f: number; v: (t: number) => View; n?: number};
export const camAt = (keys: CamKey[], f: number, t: number): View => {
	let k = 0;
	for (let i = 0; i < keys.length; i++) if (keys[i].f <= f) k = i;
	const cur = keys[k].v(t);
	if (k === 0) return cur;
	const n = keys[k].n ?? 22;
	const p = ease.inOut(Math.max(0, Math.min(1, (f - keys[k].f) / n)));
	if (p >= 1) return cur;
	const prev = keys[k - 1].v(t);
	return [prev[0] + (cur[0] - prev[0]) * p, prev[1] + (cur[1] - prev[1]) * p, prev[2] + (cur[2] - prev[2]) * p];
};
