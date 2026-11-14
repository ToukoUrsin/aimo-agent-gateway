#!/usr/bin/env bash
# Keeps the quick tunnel (cloudflared) to the gateway up. When it has to start a new one, the tunnel gets a new
# address, so this also tells the Fly front door (deploy/fly/Caddyfile reads it as UPSTREAM_QUICK).
# While the quick tunnel is down the front door falls back to the steady one by itself.
# Start with: (setsid nohup deploy/tunnel.sh >> data/logs/tunnel.log 2>&1 &)
cd "$(dirname "$0")/.."
PORT="${PORT:-8642}"
APP="${FLY_APP:-trusted-trade-agents}"
LOG=data/logs/cloudflared.log
while true; do
  if ! pgrep -f "cloudflared tunnel --no-autoupdate --url http://127.0.0.1:$PORT" > /dev/null; then
    echo "$(date -Is) no quick tunnel running, starting one"
    : > "$LOG"
    (setsid nohup cloudflared tunnel --no-autoupdate --url "http://127.0.0.1:$PORT" >> "$LOG" 2>&1 &)
    u=""
    for _ in $(seq 1 60); do
      u=$(grep -o "https://[a-z0-9-]*\.trycloudflare\.com" "$LOG" | head -1)
      [ -n "$u" ] && break
      sleep 1
    done
    if [ -n "$u" ]; then
      echo "$(date -Is) new address $u"
      (cd deploy/fly && hsec exec --only FLY_API_TOKEN -- fly secrets set UPSTREAM_QUICK="$u" -a "$APP" 2>&1 | tail -2)   # from deploy/fly: the app config names the Caddyfile by relative path
    else
      echo "$(date -Is) the tunnel gave no address, trying again in a minute"
    fi
  fi
  sleep 30
done
