"""
tests/utils/conftest.py

Puts utils/ on sys.path so tests here can `import export_state` directly,
mirroring tests/server/helpers.py's equivalent setup for server/ (see that
file's comment for the underlying reason - export_state.py is written to be
run standalone from its own directory, e.g. `python export_state.py`, not
as an installed package with an absolute import path).
"""

import os
import sys

UTILS_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "utils"))
if UTILS_DIR not in sys.path:
    sys.path.insert(0, UTILS_DIR)
