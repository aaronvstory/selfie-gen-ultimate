"""Opt-in wrapper for the $0 full-pipeline harness (scripts/dev_local_e2e.py).

Gated like RUN_FRESH_INSTALL_TEST: it needs the LOCAL gitignored canned
artifacts (test-material/canned-pipeline/, harvested from a real run), the
full face stack, and ideally the GPU for rPPG (~2-3 minutes wall time).

    RUN_LOCAL_E2E=1 pytest tests/test_local_e2e_harness.py -q

Everything except the four paid steps runs FOR REAL — extraction, similarity
gate, rPPG, oldcam, manifest flow, and the Live dashboard (captured and
checked for panel-shatter symptoms by the harness itself).
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
CANNED = REPO / "test-material" / "canned-pipeline"

_ENABLED = os.environ.get("RUN_LOCAL_E2E") == "1"


@pytest.mark.skipif(
    not _ENABLED,
    reason="set RUN_LOCAL_E2E=1 (needs local canned fixtures + face stack; ~3 min)",
)
def test_local_e2e_full_pipeline_passes():
    assert CANNED.is_dir() and any(CANNED.iterdir()), (
        f"canned artifacts missing at {CANNED} — harvest from a real run first "
        "(see scripts/dev_local_e2e.py docstring)"
    )
    proc = subprocess.run(
        [sys.executable, "-X", "utf8", str(REPO / "scripts" / "dev_local_e2e.py")],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=900,
        cwd=str(REPO),
    )
    tail = (proc.stdout or "")[-2500:] + (proc.stderr or "")[-800:]
    assert proc.returncode == 0, f"local E2E harness FAILED:\n{tail}"
    assert "VERDICT: PASS" in (proc.stdout or ""), tail
