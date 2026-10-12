#!/data/data/com.termux/files/usr/bin/bash
# Dr Harry Lab - Claude bridge launcher for Termux. Run:  bash termux_bridge.sh
# First run asks for the hub token once and keeps it in ~/.drh_token (private). Restarts the bridge if it crashes.
set -u
HUB_DEFAULT="https://darkslategrey-pigeon-166872.hostingersite.com"
cd "$(dirname "$0")"
command -v termux-wake-lock >/dev/null 2>&1 && termux-wake-lock
for p in git python nodejs; do command -v "${p/python/python3}" >/dev/null 2>&1 || pkg install -y "$p"; done
# Newer Claude Code needs a glibc binary that Termux (Android) cannot run; 2.1.112 is the last JS build that works there.
export DISABLE_AUTOUPDATER=1
command -v claude >/dev/null 2>&1 || npm install -g @anthropic-ai/claude-code@2.1.112
command -v claude >/dev/null 2>&1 || { echo "claude CLI install nahi hua"; exit 1; }
claude --version >/dev/null 2>&1 || { echo "claude chal nahi raha. Chalao: DISABLE_AUTOUPDATER=1 npm install -g @anthropic-ai/claude-code@2.1.112"; exit 1; }
[ -f "$HOME/.drh_token" ] || { read -r -s -p "Hub token: " T; echo; printf '%s' "$T" > "$HOME/.drh_token"; chmod 600 "$HOME/.drh_token"; }
export HUB="${HUB:-$HUB_DEFAULT}" TOKEN="$(cat "$HOME/.drh_token")"
git checkout -- worker.zip 2>/dev/null; git pull -q 2>/dev/null || true   # latest bridge/worker files
while true; do
  python3 bridge.py
  echo "bridge ruk gaya, 5 sec baad dobara..."; sleep 5
done
