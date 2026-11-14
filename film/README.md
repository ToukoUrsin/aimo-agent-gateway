# The demo film

A 2 minute film of Aimo Agent Gateway, made from recordings of the real product (Remotion, 1920x1080, 30 fps).
Every number on screen and in the voice-over is read from the product while it is recorded.

Cut: `out/demo-v2.mp4`, copied to `../judging/demo.mp4` and `../web/film.mp4`.

## Re-make it

```bash
cd film
npm install                                      # once; node_modules is a link into the bulk folder
node capture/capture.mjs                         # record the control-room/order shots
node capture/v2capture.mjs menu seller            # v2 finished menu and seller shots
node capture/v2capture_late.mjs tower proof home ledger # v2 late/restyled pages
hsec exec --only ELEVENLABS_API_KEY -- node scripts/make-vo.mjs --music   # voice lines and music bed
./render.sh                                      # render v2, -16 LUFS, review sheets, copy the cut
```

- `node capture/capture.mjs tower recall` records only the named shots. Order matters for the stage
  account: `reorder challenge order rogue switch` run in that order (the order shot uses the challenge's order).
- `FILM_BASE=http://127.0.0.1:8643 node capture/capture.mjs ...` records from another instance.
- `./render.sh --draft` renders at half size to `out/draft.mp4` without copying it anywhere.
- If the pages are restyled, the selectors at the top of `capture/capture.mjs` and in each shot are the only
  thing to check. The film frames page elements by their recorded boxes, not by fixed coordinates.
- Voice lines are cached by their text, so only changed lines cost a call. `--print` shows the narration.

## Where things are

- `capture/` Playwright scripts. Each shot writes its frames to `/bulk2/runs/valio/film/public/shots/<shot>/`
  and `src/data/shot-<shot>.json` (frame times, element boxes, clicks, and the facts read from the product).
- `scripts/make-vo.mjs` narration (numbers from `src/data/facts-*.json`), ElevenLabs voice "Brian" with word
  timings for the captions, and the music bed.
- `src/scenes/` one file per beat: Hook, Door (re-order), Challenge, Trust, Control, Tower, After (recall),
  Proof (with connect), Close.
- `render.sh` render, loudness, review. `scripts/review.sh` contact sheets, loudness and black-frame check.
- Bulk output (frames, audio, review sheets, node_modules): `/bulk2/runs/valio/film/`.
