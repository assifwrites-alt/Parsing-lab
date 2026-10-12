#!/bin/bash
# Fresh Ubuntu/Debian machine (Hostinger VPS, Colab, ...): install what m2.py needs.
set -e
if [ "$(id -u)" = "0" ]; then SUDO=""; else SUDO="sudo"; fi
$SUDO apt-get update -qq
$SUDO apt-get install -y -qq poppler-utils tesseract-ocr tesseract-ocr-eng mupdf-tools python3-pip python3-pil python3-numpy > /dev/null
python3 -m pip install -q --break-system-packages pymupdf 2>/dev/null || python3 -m pip install -q pymupdf || true
echo "ready: pdfimages=$(which pdfimages) tesseract=$(which tesseract)"
