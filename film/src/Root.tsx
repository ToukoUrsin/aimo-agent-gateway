import React from 'react';
import {Composition} from 'remotion';
import {Film6, FILM6_DUR} from './Film6';

export const Root: React.FC = () => (
	<>
		<Composition id="Demo" component={Film6} durationInFrames={FILM6_DUR} fps={30} width={1920} height={1080} defaultProps={{audio: true, captions: true}} />
	</>
);
