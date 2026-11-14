#!/bin/bash
# Put the 10-second cold open and its bridge in place of version 6's first shot (scene "byhand", 0 to 5.25 s). No scene is rendered again:
# the open, 1.27 s of the bridge (the ticket becomes the shop's first chip on the phone; from 1.25 s it is version 6's own frame),
# then version 6 from the first shop shot on. Narration line a1 stays, laid over the open's own sound.
#   ./open_cut.sh            -> out/demo-v7.mp4 (and out/poster-v7.jpg, the same poster frame as version 6)
# The open comes from /bulk3/runs/opens/final/valio (see its README.md); run this again when a newer open_bridge.mp4 lands there.
# Never copies anywhere else: judging/demo.mp4 and web/film.mp4 stay as they are.
set -euo pipefail
cd "$(dirname "$0")"
OPEN=${OPEN_DIR:-/bulk3/runs/opens/final/valio}
V6=out/demo-v6.mp4
A1=/bulk3/runs/valio/film6/public/audio/v6-a1-1rocfvj.wav
CUT=5.25          # where scene "wedding" starts in version 6 (scripts/timeline6.mjs)
OLEN=11.2667      # the open (10 s) and the bridge up to where it is the film's own frame: 338 frames at 30 fps
A1_AT=700         # ms into the open: "A cook knows what the kitchen will serve." on the ticket, the second sentence on the packages
WORK=/bulk3/runs/valio/film6/open
mkdir -p "$WORK"

ffmpeg -loglevel error -y -i "$OPEN/open_bridge.mp4" -i "$A1" -i "$V6" -filter_complex "
  [0:v]fps=30,trim=end_frame=338,setpts=PTS-STARTPTS,scale=1920:1080,format=yuv420p,setsar=1[ov];
  [2:v]trim=start=$CUT,setpts=PTS-STARTPTS,format=yuv420p,setsar=1[fv];
  [ov][fv]concat=n=2:v=1:a=0[v];
  [0:a]aresample=48000,aformat=channel_layouts=stereo,volume=0.6,afade=t=out:st=11.05:d=0.2,apad=whole_dur=$OLEN,atrim=0:$OLEN[bed];
  [1:a]aresample=48000,aformat=channel_layouts=stereo,loudnorm=I=-16:TP=-2:LRA=11,adelay=$A1_AT|$A1_AT,apad=whole_dur=$OLEN,atrim=0:$OLEN[vo];
  [bed][vo]amix=inputs=2:normalize=0,alimiter=limit=0.89[oa];
  [2:a]atrim=start=$CUT,asetpts=PTS-STARTPTS,afade=t=in:st=0:d=0.1[fa];
  [oa][fa]concat=n=2:v=0:a=1[a]" \
  -map "[v]" -map "[a]" -c:v libx264 -preset slow -crf 17 -pix_fmt yuv420p -r 30 -c:a aac -b:a 192k -ar 48000 -movflags +faststart "$WORK/demo-v7.mp4"

D=$(ffprobe -v error -show_entries format=duration -of csv=p=0 "$WORK/demo-v7.mp4")
python3 -c "import sys; d=float('$D'); print(f'length {d:.2f} s'); sys.exit(0 if d <= 119.5 else 1)" || { echo "too long for 2:00"; exit 1; }
cp "$WORK/demo-v7.mp4" out/demo-v7.mp4
cp out/poster-v6.jpg out/poster-v7.jpg
echo "out/demo-v7.mp4"
