#!/usr/bin/env bash
# Rebuild worker.zip (served by the hub; Colab / VPS download it) from worker/.
set -e; cd "$(dirname "$0")/worker"; rm -f ../worker.zip
zip -q -r ../worker.zip worker.py run_lab.py m2.py html_parse.py doctor.py make_sample.py setup_linux.sh lab -x "*/__pycache__/*"
echo "worker.zip: $(unzip -l ../worker.zip | tail -1)"
