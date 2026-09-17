# -*- coding: utf-8 -*-
"""Throwaway runner: rerun a task conf via f2dl core. Delete after use."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from f2dl import core

rc = core.rerun_task(Path(sys.argv[1]))
print("RC=", rc)
sys.exit(rc)
