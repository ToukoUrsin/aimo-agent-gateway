#!/usr/bin/env bash
# Put a commit live. The site runs from its own checkout (../valio-live), so work in this one never reaches visitors half-done.
# Usage: deploy/live.sh [commit]   (default: main)
set -euo pipefail
cd "$(dirname "$0")/.."
rev="$(git rev-parse "${1:-main}")"
git -C ../valio-live checkout -q -f --detach "$rev"
[ -f web/film.mp4 ] && cp -f web/film.mp4 ../valio-live/web/film.mp4
(cd ../valio-live && uv sync -q)
systemctl --user restart aimo-gateway
for _ in $(seq 1 60); do curl -fsS -o /dev/null "http://127.0.0.1:${PORT:-8642}/.well-known/ucp" 2>/dev/null && { echo "live at ${rev:0:7}"; exit 0; }; sleep 1; done
echo "the gateway did not come up" >&2; exit 1
