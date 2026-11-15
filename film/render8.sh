#!/bin/bash
# Render version 8 in a Daytona sandbox (drun), bring it to -16 LUFS, make the poster and the review pack.
#   ./render8.sh                 full cut  -> out/demo-v8.mp4, out/poster-v8.jpg
#   ./render8.sh --draft         half size -> <bulk>/drafts/draft-v8.mp4
#   ./render8.sh --frames 0-90   a part, half size, for a look
# Never copies anywhere else: judging/demo.mp4, web/film.mp4 and web/poster.jpg stay as they are.
# Footage: $FILM_BULK/public/shots (default /bulk3/runs/valio/film6); voice and music: /bulk3/runs/valio/film6/public/audio.
set -euo pipefail
cd "$(dirname "$0")"
FILM=$PWD
BULK=${FILM_BULK:-/bulk3/runs/valio/film6}
AUDIO=/bulk3/runs/valio/film6/public
STAGE=/bulk3/runs/valio/film8/stage
NAME=demo-v8; SCALE=1; RANGE=()
case "${1:-}" in
	--draft) NAME=draft-v8; SCALE=0.5 ;;
	--frames) NAME=part-v8; SCALE=0.5; RANGE=(--frames="$2") ;;
esac
npx tsc --noEmit
node scripts/timeline8.mjs --frames > /tmp/tl8.$$.txt
grep -v FRAME /tmp/tl8.$$.txt | tail -2
POSTER=$(sed -n 's/^poster frame \([0-9]*\) .*/\1/p' /tmp/tl8.$$.txt)

# the stage: the sources, plus only the footage frames the cut shows, the voice and the music
rm -rf "${STAGE:?}/src" "${STAGE:?}/public" "${STAGE:?}/out"
mkdir -p "$STAGE/public" "$STAGE/out"
cp package.json package-lock.json tsconfig.json remotion.config.ts "$STAGE/"
cp -r src "$STAGE/src"
# each frame and its neighbours (the render's float arithmetic can land one frame either side)
sed -n 's/^FRAME //p' /tmp/tl8.$$.txt | python3 -c "
import os, re, sys
out = set()
for p in sys.stdin.read().split():
    d, n = re.match(r'(.*)/f(\d+)\.jpg', p).groups()
    for k in (int(n) - 1, int(n), int(n) + 1):
        q = f'{d}/f{k:05d}.jpg'
        if k >= 0 and os.path.exists(os.path.join('$BULK/public', q)): out.add(q)
print('\n'.join(sorted(out)))" | (cd "$BULK/public" && xargs cp --parents -t "$STAGE/public")
node -e "const v=require('./src/data8/vo.json');for(const l of Object.values(v.lines))console.log(l.file);console.log(v.music)" | (cd "$AUDIO" && xargs cp --parents -t "$STAGE/public")
rm -f /tmp/tl8.$$.txt
du -sh "$STAGE/public"

cd "$STAGE"
drun --setup "npm ci --no-audit --no-fund >/dev/null && npx remotion browser ensure >/dev/null" -- \
	"FILM_BULK=\$PWD npx remotion render src/index.ts Demo out/$NAME.raw.mp4 --codec h264 --crf 18 --concurrency 4 --scale $SCALE ${RANGE[*]} --log error && ls -la out"
cd "$FILM"
mkdir -p out
RAW=$STAGE/out/$NAME.raw.mp4
if [[ "$NAME" == demo-v8 ]]; then DEST=out/$NAME.mp4; else mkdir -p "$BULK/drafts"; DEST=$BULK/drafts/$NAME.mp4; fi

# loudness: two-pass loudnorm to -16 LUFS integrated, -1.5 dBTP
M=$(ffmpeg -hide_banner -nostats -i "$RAW" -af loudnorm=I=-16:TP=-1.5:LRA=11:print_format=json -f null - 2>&1 | sed -n '/^{/,/^}/p')
g() { echo "$M" | python3 -c "import json,sys;print(json.load(sys.stdin)['$1'])"; }
ffmpeg -hide_banner -loglevel error -y -i "$RAW" -c:v copy \
	-af "loudnorm=I=-16:TP=-1.5:LRA=11:measured_I=$(g input_i):measured_TP=$(g input_tp):measured_LRA=$(g input_lra):measured_thresh=$(g input_thresh):offset=$(g target_offset):linear=true,aresample=48000" \
	-c:a aac -b:a 192k -movflags +faststart "$DEST"
if [[ "$NAME" == demo-v8 ]]; then
	# the poster: the first cart card on the phone
	ffmpeg -hide_banner -loglevel error -y -ss "$(python3 -c "print(($POSTER + 0.5) / 30)")" -i "$DEST" -frames:v 1 -q:v 2 out/poster-v8.jpg
	scripts/review.sh "$DEST" /bulk3/runs/valio/film8/review/$NAME 1
fi
ls -la "$DEST"
