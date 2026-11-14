import {Config} from '@remotion/cli/config';
Config.setVideoImageFormat('jpeg');
Config.setJpegQuality(92);
// footage, voice and music live outside the repo (many files)
Config.setPublicDir(process.env.FILM_BULK ? process.env.FILM_BULK + '/public' : '/bulk2/runs/valio/film/public');
