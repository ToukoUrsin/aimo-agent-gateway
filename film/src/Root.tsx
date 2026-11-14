import React from 'react';
import {Composition} from 'remotion';
import {Film2, FILM2_DUR} from './Film2';

export const Root: React.FC = () => (
	<>
    <Composition id="Demo" component={Film2} durationInFrames={FILM2_DUR} fps={30} width={1920} height={1080} defaultProps={{audio: true, captions: true}} />
	</>
);
