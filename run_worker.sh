#!/usr/bin/env bash
# Dr Harry Lab - run the worker on any Linux box (GitHub Codespaces, VPS). Token comes from $LAB_TOKEN, ~/.drh_token, or is asked once.
set -u
cd "$(dirname "$0")"
export HUB_URL="${HUB_URL:-https://darkslategrey-pigeon-166872.hostingersite.com}"
if [ -z "${LAB_TOKEN:-}" ]; then
  [ -f "$HOME/.drh_token" ] || { read -r -s -p "Hub token: " T; echo; printf '%s' "$T" > "$HOME/.drh_token"; chmod 600 "$HOME/.drh_token"; }
  export LAB_TOKEN="$(cat "$HOME/.drh_token")"
fi
export WORKER_NAME="${WORKER_NAME:-codespace1}"
while true; do python3 -u worker/worker.py; echo "worker ruk gaya, 5 sec baad dobara..."; sleep 5; done
