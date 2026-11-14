// Everything the film shows or says comes from these files, written by capture/capture.mjs and scripts/make-vo.mjs.
import {Shot, ShotMeta} from './shots';
import reorder from './data/shot-reorder.json';
import challenge from './data/shot-challenge.json';
import phone from './data/shot-phone.json';
import order from './data/shot-order.json';
import rogue from './data/shot-rogue.json';
import sw from './data/shot-switch.json';
import spec from './data/shot-spec.json';
import tower from './data/shot-tower.json';
import proof from './data/shot-proof.json';
import home from './data/shot-home.json';
import recall from './data/shot-recall.json';
import trace from './data/shot-trace.json';
import voJson from './data/vo.json';

export const S = {
	reorder: new Shot(reorder as unknown as ShotMeta),
	challenge: new Shot(challenge as unknown as ShotMeta),
	phone: new Shot(phone as unknown as ShotMeta),
	order: new Shot(order as unknown as ShotMeta),
	rogue: new Shot(rogue as unknown as ShotMeta),
	switch: new Shot(sw as unknown as ShotMeta),
	spec: new Shot(spec as unknown as ShotMeta),
	tower: new Shot(tower as unknown as ShotMeta),
	proof: new Shot(proof as unknown as ShotMeta),
	home: new Shot(home as unknown as ShotMeta),
	recall: new Shot(recall as unknown as ShotMeta),
	trace: new Shot(trace as unknown as ShotMeta),
};
export type VoLine = {scene: string; at: number; text: string; file: string; dur: number; words: {w: string; s: number; e: number}[]};
export const vo = voJson as {voice: string; music: string | null; lines: VoLine[]};

export const eur = (cents: number, dec = true) => '€' + (cents / 100).toLocaleString('en-GB', {minimumFractionDigits: dec ? 2 : 0, maximumFractionDigits: dec ? 2 : 0});
export const euros = (s: string) => Number(s.replace(/[€,]/g, ''));
