import React from 'react';
import {SceneHook, hookDur} from './Hook';
import {SceneDoor, doorDur} from './Door';
import {SceneChallenge, challengeDur} from './Challenge';
import {SceneTrust, trustDur} from './Trust';
import {SceneControl, controlDur} from './Control';
import {SceneClose, closeDur} from './Close';
import {SceneTower, towerDur} from './Tower';
import {SceneProof, proofDur} from './Proof';
import {SceneAfter, afterDur} from './After';

export const SCENES: {id: string; dur: number; C: React.FC}[] = [
	{id: 'hook', dur: hookDur, C: SceneHook},
	{id: 'door', dur: doorDur, C: SceneDoor},
	{id: 'challenge', dur: challengeDur, C: SceneChallenge},
	{id: 'trust', dur: trustDur, C: SceneTrust},
	{id: 'control', dur: controlDur, C: SceneControl},
	{id: 'tower', dur: towerDur, C: SceneTower},
	{id: 'after', dur: afterDur, C: SceneAfter},
	{id: 'proof', dur: proofDur, C: SceneProof},
	{id: 'close', dur: closeDur, C: SceneClose},
];
