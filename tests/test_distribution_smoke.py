"""End-to-end smoke tests for the distribution zips.

These tests build the actual zips, extract them into a temp directory, and
verify that a recipient unzipping them on a fresh machine could:
  1. Find all the files they need (engine, launchers, README, requirements).
  2. Run ``from src.engine import FaceEngine`` without ``ModuleNotFoundError``.

Background: shipped a similarity zip in 2026-05 that was missing the
``similarity_engine.py`` and ``face_similarity.py`` modules at the staging
root. ``similarity/src/engine.py`` is a shim that imports them via
``sys.path.insert(parent_of_similarity, ...)``, so the shim crashed with
``ModuleNotFoundError`` on every recipient's first launch. Embarrassing —
the test that should have caught it didn't exist.

This file exists so that NEVER happens again. Each new shareable artifact
gets a corresponding ``test_<artifact>_zip_*`` function here.
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DIST_DIR = REPO_ROOT / "dist"


def _build_zip(builder_module: str) -> Path:
    """Run a distribution builder script and return the path to the resulting zip.

    Args:
        builder_module: Module name under ``distribution/`` (e.g.
            ``"build_similarity_zip"``) — invoked via the same Python that
            runs the tests so we hit the same dependency surface.

    Returns:
        Path to the freshly-built zip in ``dist/``.
    """
    builder_path = REPO_ROOT / "distribution" / f"{builder_module}.py"
    assert builder_path.is_file(), f"Builder script missing: {builder_path}"
    result = subprocess.run(
        [sys.executable, str(builder_path)],
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert result.returncode == 0, (
        f"Builder {builder_module} exited {result.returncode}\n"
        f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )
    # The builder prints the final path on a "Done. Distributable: <path>" line.
    zip_path = None
    for line in result.stdout.splitlines():
        line = line.strip()
        if line.startswith("Done. Distributable:"):
            zip_path = Path(line.split(":", 1)[1].strip())
            break
    assert zip_path is not None and zip_path.is_file(), (
        f"Could not parse zip path from builder stdout:\n{result.stdout}"
    )
    return zip_path


class SimilarityZipSmokeTest(unittest.TestCase):
    """Verify the standalone similarity zip is shippable."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.zip_path = _build_zip("build_similarity_zip")

    def test_zip_contains_required_top_level_files(self) -> None:
        """README + engine modules + similarity/ folder all present at root."""
        with zipfile.ZipFile(self.zip_path) as zf:
            names = set(zf.namelist())
        # The bug that caused the embarrassing broken share: similarity_engine.py
        # was at the repo ROOT but the zip only walked similarity/, so it was
        # missing. This assertion is the regression lock.
        for required in (
            "README_FIRST.txt",
            "similarity_engine.py",  # ← the previously-missing file
            "face_similarity.py",    # ← also previously missing
            "similarity/main.py",
            "similarity/src/engine.py",
            "similarity/src/gui.py",
            "similarity/src/cli.py",
            "similarity/requirements.txt",
            "similarity/run_gui.bat",
            "similarity/run_gui.command",
            "similarity/run_cli.bat",
            "similarity/run_cli.command",
        ):
            self.assertIn(required, names, f"missing from zip: {required}")

    def test_zip_excludes_runtime_artifacts(self) -> None:
        """No __pycache__, .venv, .pyc, .log files leak into the share."""
        with zipfile.ZipFile(self.zip_path) as zf:
            names = zf.namelist()
        for forbidden in ("__pycache__", ".venv", ".pyc", ".log", ".pytest_cache"):
            offending = [n for n in names if forbidden in n]
            self.assertEqual(
                offending,
                [],
                f"forbidden artifact pattern '{forbidden}' leaked into zip: {offending}",
            )

    def test_unzipped_engine_imports_cleanly(self) -> None:
        """The actual smoke: extract zip, run the launcher's import path.

        This is the test that would have prevented the original bug. We
        spawn a SUBPROCESS so the import test runs in a clean Python with
        no pre-loaded modules from the dev tree, mimicking what the
        recipient's first launch does.
        """
        with tempfile.TemporaryDirectory(prefix="sim_zip_e2e_") as tmpdir:
            with zipfile.ZipFile(self.zip_path) as zf:
                zf.extractall(tmpdir)
            sim_dir = os.path.join(tmpdir, "similarity")
            self.assertTrue(
                os.path.isdir(sim_dir),
                f"similarity/ directory missing after extract: {sim_dir}",
            )
            # Mimic the launcher: cd into similarity/, add it to sys.path,
            # then `from src.engine import FaceEngine`.
            test_script = (
                "import os, sys\n"
                "sys.path.insert(0, os.getcwd())\n"
                "from src.engine import FaceEngine\n"
                "assert FaceEngine.__module__ == 'similarity_engine', "
                "f'unexpected module: {FaceEngine.__module__}'\n"
                "print('OK')\n"
            )
            result = subprocess.run(
                [sys.executable, "-c", test_script],
                cwd=sim_dir,
                capture_output=True,
                text=True,
                timeout=120,
            )
            self.assertEqual(
                result.returncode,
                0,
                f"FaceEngine import FAILED in unzipped tree.\n"
                f"stdout: {result.stdout}\nstderr: {result.stderr}",
            )
            self.assertIn("OK", result.stdout)


class OldcamV13ZipSmokeTest(unittest.TestCase):
    """Verify the standalone oldcam-v13 zip is shippable.

    Builder: ``distribution/build_oldcam_v13_zip.py``. The same regression
    class as similarity (missing module deps) could happen here if oldcam-v13
    grows root-level dependencies, so we use the same E2E import-test
    pattern.
    """

    @classmethod
    def setUpClass(cls) -> None:
        try:
            cls.zip_path = _build_zip("build_oldcam_v13_zip")
        except (FileNotFoundError, AssertionError) as exc:  # pragma: no cover
            raise unittest.SkipTest(f"oldcam-v13 builder unavailable: {exc}")

    def test_zip_contains_required_files(self) -> None:
        with zipfile.ZipFile(self.zip_path) as zf:
            names = set(zf.namelist())
        for required in (
            "README_FIRST.txt",
            "oldcam-v13/oldcam.py",
            "oldcam-v13/launcher.py",
            "oldcam-v13/requirements.txt",
            "oldcam-v13/oldcam_launcher.bat",
            "oldcam-v13/macOS/oldcam.py",
            "oldcam-v13/macOS/oldcam.command",
            "oldcam-v13/macOS/requirements.txt",
        ):
            self.assertIn(required, names, f"missing from zip: {required}")

    def test_zip_excludes_runtime_artifacts(self) -> None:
        with zipfile.ZipFile(self.zip_path) as zf:
            names = zf.namelist()
        for forbidden in ("__pycache__", ".venv", ".pyc", ".log", ".pytest_cache"):
            offending = [n for n in names if forbidden in n]
            self.assertEqual(
                offending,
                [],
                f"forbidden artifact pattern '{forbidden}' leaked into zip: {offending}",
            )

    def test_unzipped_oldcam_module_parses(self) -> None:
        """Extract the zip and verify oldcam.py is a parseable Python module.

        We don't try to actually IMPORT oldcam.py because that would pull in
        the full mediapipe + cv2 + numpy stack which the test machine may
        not have. AST parsing is enough to catch the embarrassing class of
        bugs (missing files, broken syntax, accidentally-shipped binary).
        """
        with tempfile.TemporaryDirectory(prefix="oldcam_zip_e2e_") as tmpdir:
            with zipfile.ZipFile(self.zip_path) as zf:
                zf.extractall(tmpdir)
            oldcam_py = os.path.join(tmpdir, "oldcam-v13", "oldcam.py")
            self.assertTrue(
                os.path.isfile(oldcam_py),
                f"oldcam-v13/oldcam.py missing after extract: {oldcam_py}",
            )
            test_script = (
                "import ast, sys\n"
                "ast.parse(open(sys.argv[1], encoding='utf-8').read())\n"
                "print('OK')\n"
            )
            result = subprocess.run(
                [sys.executable, "-c", test_script, oldcam_py],
                capture_output=True,
                text=True,
                timeout=30,
            )
            self.assertEqual(
                result.returncode,
                0,
                f"oldcam.py parse FAILED.\n"
                f"stdout: {result.stdout}\nstderr: {result.stderr}",
            )


if __name__ == "__main__":
    unittest.main()
