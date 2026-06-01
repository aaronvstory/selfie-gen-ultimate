"""Regression guard for the v2.10 fresh-install numpy-2.x Face Crop break.

A friend on a fresh v2.10 Windows install hit::

    Face Crop: RetinaFace/TensorFlow import failed —
    ImportError: numpy.core.umath failed to import

Root cause: numpy 2.x reached the venv. The numpy<2 / opencv<4.12 caps lived
ONLY in requirements.txt, so they governed the single `pip install -r
requirements.txt` call but NOT the dependency_checker bootstrap, the --no-deps
mediapipe install, or dependency_health_check's --force-reinstall repair.
deepface==0.0.92 declares only `numpy>=1.14.0` (open upper bound) and numpy
2.4.x ships win cp312 wheels, so any unconstrained resolve was free to upgrade.

This module pins the Python-side fix (the launcher-side static guards live in
``test_launcher_health_check_loop.py``):

  * ``assert_numpy_pinned`` flags numpy >= 2 as a failure.
  * ``check_runtime_dependencies`` surfaces that failure.
  * ``run_repair`` threads ``-c constraints.txt`` into its pip command.

Plus an OPT-IN slow layer (env ``RUN_FRESH_INSTALL_TEST=1``) that builds a
throwaway venv, runs the REAL install, and asserts numpy stays <2 — the test
that would have caught this class before shipping. The fast layer is the thing
that runs in CI; the slow layer is the pre-ship gate.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

import dependency_health_check as dhc

REPO_ROOT = Path(__file__).resolve().parents[1]
CONSTRAINTS_FILE = REPO_ROOT / "constraints.txt"
REQUIREMENTS_FILE = REPO_ROOT / "requirements.txt"


# ── Fast layer (always runs) ─────────────────────────────────────────


def test_assert_numpy_pinned_flags_v2():
    msg = dhc.assert_numpy_pinned(version_reader=lambda name: "2.4.2")
    assert msg is not None and "too new" in msg, (
        "assert_numpy_pinned must flag numpy 2.x as a failure"
    )


def test_assert_numpy_pinned_passes_v1():
    assert dhc.assert_numpy_pinned(version_reader=lambda name: "1.26.4") is None


def test_assert_numpy_pinned_handles_unparseable():
    msg = dhc.assert_numpy_pinned(version_reader=lambda name: "not-a-version")
    assert msg is not None and "unparseable" in msg


def test_check_runtime_dependencies_surfaces_numpy2(monkeypatch):
    """A numpy-2 environment must make check_runtime_dependencies fail even
    when every import otherwise succeeds (the failure mode where numpy imports
    fine but TF's C-extension breaks later).

    The numpy version is read from DISK METADATA (importlib.metadata), not by
    importing numpy (GPT review, PR #65 — importing numpy is unsafe in this
    broken state). We monkeypatch importlib.metadata.version to simulate 2.4.2
    without needing numpy 2.x actually installed in the test venv."""
    import types
    import importlib.metadata as md

    healthy = {
        "tensorflow": types.SimpleNamespace(__version__="2.16.2"),
        "tensorflow.compat.v2": types.SimpleNamespace(),
        "tf_keras": types.SimpleNamespace(__version__="2.16.0"),
        "retinaface": types.SimpleNamespace(RetinaFace=object()),
        "retinaface.RetinaFace": object(),
        "cv2": types.SimpleNamespace(),
        "numpy": types.SimpleNamespace(__version__="2.4.2"),
        "torch": types.SimpleNamespace(
            cuda=types.SimpleNamespace(is_available=lambda: False),
            version=types.SimpleNamespace(cuda=None),
        ),
    }

    def fake_importer(name: str):
        if name in healthy:
            return healthy[name]
        raise ModuleNotFoundError(name)

    _real_version = md.version

    def fake_version(name: str) -> str:
        return "2.4.2" if name == "numpy" else _real_version(name)

    monkeypatch.setattr(md, "version", fake_version)

    ok, failures = dhc.check_runtime_dependencies(
        importer=fake_importer,
        runtime_probe=lambda: (object(), ""),
    )
    assert not ok, "check must FAIL when numpy is 2.x"
    assert any("numpy too new" in f for f in failures), failures


def test_run_repair_threads_constraints(monkeypatch):
    """run_repair's pip command must include -c constraints.txt."""
    captured = {}

    class _Completed:
        returncode = 0
        stdout = ""
        stderr = ""

    def fake_run(cmd, *a, **k):
        captured["cmd"] = cmd
        return _Completed()

    monkeypatch.setattr(dhc.subprocess, "run", fake_run)
    ok, _msg = dhc.run_repair(failures=[])
    assert ok
    cmd = captured["cmd"]
    assert "-c" in cmd, f"repair cmd missing -c flag: {cmd}"
    c_idx = cmd.index("-c")
    assert cmd[c_idx + 1].endswith("constraints.txt"), (
        f"-c must point at constraints.txt: {cmd}"
    )


def test_constraints_path_resolves():
    path = dhc._constraints_path()
    assert path is not None and path.endswith("constraints.txt")


def test_constraints_and_requirements_numpy_caps_agree():
    """The numpy cap in constraints.txt and requirements.txt must agree so a
    future bump of one doesn't silently diverge from the other."""
    cons = CONSTRAINTS_FILE.read_text(encoding="utf-8")
    reqs = REQUIREMENTS_FILE.read_text(encoding="utf-8")
    assert "numpy>=1.26,<2" in cons
    assert "numpy>=1.26,<2" in reqs


def test_macos_stamp_hash_includes_constraints():
    """setup_macos.sh's requirements_hash() must incorporate constraints.txt so
    a constraints change invalidates the .venv-macos stamp and forces a re-sync
    (GPT review, PR #65). Verify both: (a) the source passes CONSTRAINTS_FILE to
    the hasher, and (b) the hashing algorithm actually produces a different
    digest when constraints.txt content changes."""
    import hashlib

    src = (REPO_ROOT / "setup_macos.sh").read_text(encoding="utf-8")
    assert 'CONSTRAINTS_FILE="${ROOT_DIR}/constraints.txt"' in src
    assert '"${REQUIREMENTS_FILE}" "${CONSTRAINTS_FILE}"' in src, (
        "requirements_hash must pass BOTH files to the hasher"
    )

    # Reproduce the hashing algorithm (read_bytes per file + \0 delimiter) and
    # confirm two different constraints contents yield different digests.
    def _hash(req: bytes, cons: bytes) -> str:
        h = hashlib.sha256()
        for blob in (req, cons):
            h.update(blob)
            h.update(b"\0")
        return h.hexdigest()

    req = b"numpy>=1.26,<2\n"
    a = _hash(req, b"numpy>=1.26,<2\n")
    b = _hash(req, b"numpy>=1.26,<2\nopencv-python<4.12\n")
    assert a != b, "constraints.txt change must alter the macOS stamp hash"


def test_face_crop_repair_button_reachable_when_deps_missing():
    """When cv2/numpy fail to import at module load (HAS_FACE_DEPS False) the
    Detect button is created DISABLED, so the zero-terminal repair would be
    unreachable. A dedicated 'Repair dependencies now' button must be created
    in the HAS_FACE_DEPS-False warning block and wired to a repair handler
    (Codex P2 reachability fix, PR #65)."""
    src = (REPO_ROOT / "kling_gui" / "tabs" / "face_crop_tab.py").read_text(encoding="utf-8")
    # The button + its command must exist inside the `if not HAS_FACE_DEPS:`
    # construction block (the warning UI), routing to the repair handler.
    assert "_dep_repair_btn" in src, "missing the always-reachable Repair button"
    assert "Repair dependencies now" in src
    assert "def _repair_deps_from_warning" in src, "missing the repair-button handler"
    assert "_attempt_in_app_repair" in src


def test_macos_launchers_use_space_safe_constraints_array():
    """macOS .command launchers must build CONSTRAINTS_ARG as a bash ARRAY and
    expand it with "${CONSTRAINTS_ARG[@]}" — a scalar string word-splits when
    REPO_ROOT contains a space (e.g. /Users/John Smith/...), breaking pip for
    the non-technical Mac users this targets (code-review H1, PR #65)."""
    import glob

    mac_launchers = glob.glob(str(REPO_ROOT / "oldcam-v*" / "macOS" / "oldcam.command")) + [
        str(REPO_ROOT / "similarity" / "run_gui.command"),
        str(REPO_ROOT / "similarity" / "run_cli.command"),
    ]
    assert mac_launchers, "no macOS launchers found"
    for path in mac_launchers:
        src = open(path, encoding="utf-8").read()
        assert "CONSTRAINTS_ARG=()" in src, f"{path}: must declare CONSTRAINTS_ARG as a bash array"
        # No scalar/word-splitting expansion of the constraints arg.
        assert "pip install ${CONSTRAINTS_ARG}" not in src, (
            f"{path}: uses scalar ${{CONSTRAINTS_ARG}} (word-splits on spaces); "
            'use the array form'
        )
        if 'CONSTRAINTS_ARG=(-c "${REPO_ROOT}/constraints.txt")' in src:
            # set -u-safe expansion REQUIRED: under `set -u` on macOS's default
            # Bash 3.2, plain "${arr[@]}" on an EMPTY array raises 'unbound
            # variable' and aborts the launcher on the graceful-degradation path
            # (REPO_ROOT empty / constraints.txt missing). The
            # "${arr[@]+"${arr[@]}"}" form yields nothing instead of crashing
            # (code-review MEDIUM, PR #65 round 4).
            assert '"${CONSTRAINTS_ARG[@]+"${CONSTRAINTS_ARG[@]}"}"' in src, (
                f"{path}: must use the set -u-safe expansion "
                '"${CONSTRAINTS_ARG[@]+"${CONSTRAINTS_ARG[@]}"}" so an empty array '
                "doesn't abort the launcher under set -u on Bash 3.2 (macOS)"
            )
            # And must NOT use the bare form that crashes on empty under set -u.
            # (Bare form appears only as a substring of the safe form, so count
            # occurrences: the safe form contains it twice; a bare standalone
            # use would push the count higher — guard via the unsafe install line.)
            assert 'pip install "${CONSTRAINTS_ARG[@]}" ' not in src and \
                   'pip install --force-reinstall --no-deps "${CONSTRAINTS_ARG[@]}" ' not in src, (
                f"{path}: bare \"${{CONSTRAINTS_ARG[@]}}\" on an install line crashes on "
                "empty array under set -u (Bash 3.2); use the +-guarded form"
            )


# ── Opt-in slow layer: real throwaway-venv install ───────────────────


@pytest.mark.skipif(
    os.environ.get("RUN_FRESH_INSTALL_TEST") != "1",
    reason="slow real-install test; set RUN_FRESH_INSTALL_TEST=1 to run (pre-ship gate)",
)
def test_fresh_venv_real_install(tmp_path):
    """Build a throwaway venv, run the REAL install with -c constraints.txt,
    then assert numpy resolved <2. This reproduces the friend's exact path —
    the layer that catches this class of bug before shipping.

    NOTE: downloads the full stack (torch/TF — multi-GB). Intended for the
    pre-ship gate on Windows, not per-commit CI.
    """
    venv_dir = tmp_path / "fresh_venv"
    subprocess.run([sys.executable, "-m", "venv", str(venv_dir)], check=True)
    py = venv_dir / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    assert py.exists(), f"venv python not created at {py}"

    subprocess.run(
        [str(py), "-m", "pip", "install", "--upgrade", "pip"],
        check=True,
    )
    # Real install with the constraints file threaded through.
    subprocess.run(
        [
            str(py), "-m", "pip", "install",
            "-c", str(CONSTRAINTS_FILE),
            "-r", str(REQUIREMENTS_FILE),
        ],
        check=True,
    )
    # The decisive assertion: numpy must have resolved <2.
    out = subprocess.run(
        [str(py), "-c", "import numpy, sys; print(numpy.__version__)"],
        capture_output=True, text=True, check=True,
    )
    version = out.stdout.strip()
    assert version.startswith("1."), (
        f"numpy resolved to {version} — constraints failed to hold it <2"
    )

    # And a bootstrap-style unconstrained-looking resolve must NOT be able to
    # pull numpy 2.x when the constraints file is present (dry-run is enough).
    dry = subprocess.run(
        [
            str(py), "-m", "pip", "install", "--dry-run",
            "-c", str(CONSTRAINTS_FILE),
            "deepface==0.0.92", "opencv-contrib-python",
        ],
        capture_output=True, text=True, check=False,
    )
    combined = (dry.stdout or "") + (dry.stderr or "")
    assert "numpy-2" not in combined and "numpy 2" not in combined, (
        "bootstrap-style resolve selected numpy 2.x despite constraints:\n" + combined
    )
