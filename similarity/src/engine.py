"""Thin compatibility shim to canonical repo similarity engine."""

import os
import sys

_THIS_DIR = os.path.dirname(__file__)
_SIMILARITY_DIR = os.path.dirname(_THIS_DIR)
_REPO_ROOT = os.path.dirname(_SIMILARITY_DIR)
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from similarity_engine import FaceEngine  # noqa: E402,F401
