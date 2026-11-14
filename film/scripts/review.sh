#!/bin/bash
# Review pack for a render: probe, frames every N s (default 1), 4x4 contact sheets, loudness, black stretches.
#   scripts/review.sh out/demo-v1.mp4 /bulk2/runs/valio/film/review/v1 [every_s]
set -euo pipefail
in=$1; out=$2; every=${3:-1}
mkdir -p "$out/frames"
find "${out:?}" -maxdepth 2 \( -path "*/frames/f*.jpg" -o -name "sheet-*.jpg" \) -delete
ffprobe -v error -show_entries format=duration,size:stream=codec_name,width,height,r_frame_rate -of default=nw=1 "$in" > "$out/probe.txt"
ffmpeg -nostdin -loglevel error -i "$in" -vf "fps=1/$every,scale=960:-1,drawtext=text='%{pts\:hms}':x=8:y=8:fontsize=22:fontcolor=yellow:box=1:boxcolor=black@0.6" -q:v 3 "$out/frames/f%03d.jpg"
ffmpeg -nostdin -loglevel error -pattern_type glob -i "$out/frames/f*.jpg" -vf "tile=4x4:padding=6:color=black" -q:v 3 "$out/sheet-%02d.jpg"
ffmpeg -nostdin -hide_banner -i "$in" -af ebur128=peak=true -f null - > "$out/ebur.txt" 2>&1 || true
grep -A14 "Summary" "$out/ebur.txt" > "$out/loudness.txt" || true
ffmpeg -nostdin -hide_banner -i "$in" -vf "blackdetect=d=0.5:pix_th=0.05" -an -f null - 2>&1 | grep blackdetect > "$out/black.txt" || true
cat "$out/probe.txt"; grep -E "I:|Peak:" "$out/loudness.txt" || true; cat "$out/black.txt"; ls "$out" | grep sheet
