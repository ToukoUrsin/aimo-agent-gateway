// Print the v8 timeline (scenes, voice lines, length) without rendering: node scripts/timeline8.mjs
import {build} from 'esbuild';
import path from 'node:path';
import {fileURLToPath, pathToFileURL} from 'node:url';
const film = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const out = path.join(film, 'node_modules', '.tl8.mjs');
await build({entryPoints: [path.join(film, 'src/Film8.tsx')], bundle: true, format: 'esm', platform: 'node', outfile: out, jsx: 'automatic', logLevel: 'error',
	external: ['remotion', 'react', 'react/*', '@remotion/*'], loader: {'.json': 'json'}, nodePaths: [path.join(film, 'node_modules')]});
const m = await import(pathToFileURL(out).href);
for (const s of m.TL) console.log(`${s.id.padEnd(8)} ${s.from.toFixed(2).padStart(6)} +${s.dur.toFixed(2)}`);
for (const [k, v] of Object.entries(m.LINE_AT)) console.log(`  ${k} @ ${v.toFixed(2)}`);
console.log('total', (m.FILM8_DUR / 30).toFixed(2), 's');
if (!m.BANNER_EN) console.log('WARNING: the shop banner has new words; add them to BANNERS in Film8.tsx');
console.log('poster frame', m.POSTER_FRAME, (m.POSTER_FRAME / 30).toFixed(2), 's');
if (process.argv.includes('--frames')) for (const f of m.framesUsed()) console.log('FRAME', f);
