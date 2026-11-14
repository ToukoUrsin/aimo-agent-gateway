#!/bin/bash
# Render the film, bring it to -16 LUFS, review it, and copy the cut where the product and the judges expect it.
#   ./render.sh            full cut -> out/demo-v2.mp4 (+ judging/demo.mp4, web/film.mp4)
#   ./render.sh --draft    half-size draft -> out/draft.mp4 (no copies)
set -euo pipefail
cd "$(dirname "$0")"
BULK=${FILM_BULK:-/bulk2/runs/valio/film2}
NAME=demo-v2
SCALE=1
export FILM_BULK="$BULK"
if [[ "${1:-}" == "--draft" ]]; then NAME=draft; SCALE=0.5; fi
mkdir -p out
npx tsc --noEmit
npx remotion render src/index.ts Demo "out/$NAME.raw.mp4" --codec h264 --crf 18 --concurrency 2 --scale "$SCALE" --log error
# loudness: two-pass loudnorm to -16 LUFS integrated, -1.5 dBTP
M=$(ffmpeg -hide_banner -nostats -i "out/$NAME.raw.mp4" -af loudnorm=I=-16:TP=-1.5:LRA=11:print_format=json -f null - 2>&1 | sed -n '/^{/,/^}/p')
g() { echo "$M" | python3 -c "import json,sys;print(json.load(sys.stdin)['$1'])"; }
ffmpeg -hide_banner -loglevel error -y -i "out/$NAME.raw.mp4" -c:v copy \
	-af "loudnorm=I=-16:TP=-1.5:LRA=11:measured_I=$(g input_i):measured_TP=$(g input_tp):measured_LRA=$(g input_lra):measured_thresh=$(g input_thresh):offset=$(g target_offset):linear=true,aresample=48000" \
	-c:a aac -b:a 192k -movflags +faststart "out/$NAME.mp4"
rm -f "out/${NAME:?}.raw.mp4"
scripts/review.sh "out/$NAME.mp4" "$BULK/review/$NAME" 2
if [[ "$NAME" == demo-v2 ]]; then
	cp "out/$NAME.mp4" ../judging/demo.mp4
	cp "out/$NAME.mp4" ../web/film.mp4
fi
ls -la "out/$NAME.mp4"
