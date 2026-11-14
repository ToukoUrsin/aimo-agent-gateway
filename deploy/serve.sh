#!/usr/bin/env bash
# Keeps the gateway up: restarts it if it exits. Start with: (setsid nohup deploy/serve.sh >> data/logs/server.log 2>&1 &)
cd "$(dirname "$0")/.."
while true; do
  ./run.sh
  echo "$(date -Is) gateway exited, restarting in 2 s"
  sleep 2
done
