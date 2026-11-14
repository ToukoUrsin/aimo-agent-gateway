#!/bin/bash
# Every 5 minutes: is the public site answering? Logs one line per check; the serve.sh loop restarts the gateway itself.
cd "$(dirname "$0")/.."
URL="${PUBLIC_URL:-$(grep ^PUBLIC_URL .env | cut -d= -f2)}"
while true; do
  for p in /api/home /tower /proof /mcp; do
    c=$(curl -s -o /dev/null -m 20 -w "%{http_code}" "$URL$p")
    case "$c" in 2*|3*|401|405|406) ;; *) echo "$(date -u +%FT%TZ) $p -> $c" ;; esac
  done
  echo "$(date -u +%FT%TZ) ok" > data/logs/probe.last
  sleep 300
done
