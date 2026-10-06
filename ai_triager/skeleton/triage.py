#!/usr/bin/env python3
"""Workspace entry point for the ai-triager CLI; see `./triage.py --help`."""
import os
import sys

from pathlib import Path

ROOT = Path(__file__).resolve().parent
# The interpreter ai-triager was installed into, which has the model and decision SDKs.
PYTHON = '{{triager_python}}'

if os.path.exists(PYTHON) and os.path.abspath(sys.executable) != PYTHON:
    os.execv(PYTHON, [PYTHON, __file__, *sys.argv[1:]])

try:
    from ai_triager.cli import main
except ImportError:
    # Not installed in this interpreter: use the checkout the workspace was created from.
    sys.path.append('{{triager_path}}')
    from ai_triager.cli import main

main(workspace=ROOT)
