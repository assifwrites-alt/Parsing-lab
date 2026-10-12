#!/usr/bin/env bash
# Dr Harry Lab - one-command, idempotent setup for a Debian/Ubuntu machine (VPS, Docker, Colab). Safe to re-run.
#   bash setup.sh            -> system packages, python libs, doctor
set -u
cd "$(dirname "$0")"
SUDO=""; [ "$(id -u)" != "0" ] && SUDO="sudo"
echo "== system packages =="
if command -v apt-get >/dev/null 2>&1; then
  $SUDO apt-get update -qq || echo "WARN: apt-get update failed"
  # install one by one so a single unavailable package (e.g. libgl1 name differences) does not abort the rest
  while read -r p; do
    [ -z "$p" ] && continue
    dpkg -s "$p" >/dev/null 2>&1 || $SUDO apt-get install -y -qq "$p" >/dev/null 2>&1 || echo "WARN: could not install $p"
  done < packages.txt
else
  echo "WARN: no apt-get; install the packages in packages.txt yourself"
fi
echo "== python libs =="
PIP="python3 -m pip install -q --disable-pip-version-check"
$PIP --break-system-packages -r requirements.txt 2>/dev/null || $PIP -r requirements.txt 2>/dev/null || {
  echo "WARN: pinned install failed on this Python; installing unpinned names"
  sed -E 's/==.*//; /^#/d; /^$/d' requirements.txt | while read -r p; do $PIP --break-system-packages "$p" 2>/dev/null || $PIP "$p" 2>/dev/null || echo "WARN: could not install $p"; done
}
python3 -c "import pymupdf" 2>/dev/null || echo "NOTE: pymupdf unavailable; continuing with pypdf + poppler (optional)."
echo "== doctor =="
python3 worker/doctor.py
