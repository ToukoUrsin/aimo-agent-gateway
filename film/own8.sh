#!/bin/bash
# Film 8 with a person's voice, in one command:
#   film/own8.sh RECORDING          (wav, m4a, mp4, mov, webm: the 18 lines read in order, a pause between lines)
# It finds the takes (scripts/own8.py), sets every scene's length from the reading and renders out/demo-v8.mp4.
# If the reading would make the film longer than 1:59, the takes are played a little faster (4, 7, then 10 %).
# The computer voice comes back with: cp src/data8/vo-tts.json src/data8/vo.json
set -euo pipefail
cd "$(dirname "$0")"
rec=$(realpath "${1:?usage: film/own8.sh RECORDING}")
ok=0
for tempo in 1.00 1.04 1.07 1.10; do
	nice -n 10 python3 scripts/own8.py "$rec" --tempo "$tempo"
	total=$(node scripts/timeline8.mjs | sed -n 's/^total \([0-9.]*\) s.*/\1/p')
	echo "own8.sh: the film is $total s at tempo $tempo"
	if python3 -c "import sys; sys.exit(0 if float('$total') <= 119.4 else 1)"; then ok=1; break; fi
done
((ok)) || { echo "own8.sh: the reading is too long even 10 % faster."; exit 3; }
[[ "${2:-}" == "--no-render" ]] || ./render8.sh
