from __future__ import annotations

import sys
from pathlib import Path

# Put the resemble-score/ subproject root on sys.path so `import src.*`
# resolves when tests run from the repo root (mirrors similarity/tests).
SUBPROJECT_ROOT = Path(__file__).resolve().parent.parent
subproject_root_str = str(SUBPROJECT_ROOT)
if subproject_root_str not in sys.path:
    sys.path.insert(0, subproject_root_str)
