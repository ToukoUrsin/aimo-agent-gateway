import React from 'react';
import {Composition} from 'remotion';
import {Film8, FILM8_DUR} from './Film8';

export const Root: React.FC = () => (
	<>
		<Composition id="Demo" component={Film8} durationInFrames={FILM8_DUR} fps={30} width={1920} height={1080} defaultProps={{audio: true, captions: false, music: 0.07}} />
	</>
);
