#!/usr/bin/env python3
"""Entry point used by worker.py: runs the lab pipeline (parse + verify + images + vision fix + reports)."""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from lab.pipeline import main
sys.exit(main(sys.argv[1:]))
