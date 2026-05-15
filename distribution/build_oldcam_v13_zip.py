"""Build a shareable .zip distributable of the standalone ``oldcam-v13/`` subproject.

Walks ``oldcam-v13/`` only (Win + macOS algorithm files, launchers, requirements),
excludes runtime/build artifacts, drops a ``README_FIRST.txt`` at the staging
root, then zips it as ``dist/oldcam-v13-YYYYMMDD.zip``.

Run:
    python distribution/build_oldcam_v13_zip.py

The zip is small (~100KB) because it ships source code only. Recipient's
first launch creates a local virtualenv from ``requirements.txt`` and
downloads MediaPipe model assets on demand.

Mirrors the structure of ``build_similarity_zip.py`` for consistency.
Unlike the similarity bundle, oldcam-v13 has NO root-level engine dependency
— the `oldcam.py` algorithm file is fully self-contained — so there's no
analog to the engine-bundling fix that build_similarity_zip.py needed.
"""

from __future__ import annotations

import shutil
import sys
from datetime import datetime
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
OLDCAM_ROOT = REPO_ROOT / "oldcam-v13"
DIST_DIR = REPO_ROOT / "dist"
STAGING_DIR = DIST_DIR / "oldcam-v13-staging"

# Same exclusion pattern as build_similarity_zip.py for consistency.
SKIP_DIRS = {
    "__pycache__",
    ".venv",
    "venv",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
    ".launcher_state",
    "node_modules",
    ".git",
    ".idea",
    ".vscode",
    "dist",
    "build",
    "*.egg-info",
}
SKIP_SUFFIXES = {".pyc", ".pyo", ".log"}
SKIP_FILENAMES = {
    "launcher_runtime.log",
    ".DS_Store",
    "Thumbs.db",
    "desktop.ini",
}


def _should_skip(rel_path: Path) -> bool:
    """Return True if this path (file or dir) should be excluded from the zip.

    Conservative: any segment matching SKIP_DIRS, any suffix in SKIP_SUFFIXES,
    or any name in SKIP_FILENAMES is dropped.

    Args:
        rel_path: Path relative to the oldcam-v13 root to check.

    Returns:
        True if the path should be excluded from the zip, False otherwise.
    """
    parts = set(rel_path.parts)
    for skip in SKIP_DIRS:
        if skip.startswith("*"):
            tail = skip[1:]
            if any(p.endswith(tail) for p in rel_path.parts):
                return True
        elif skip in parts:
            return True
    if rel_path.suffix in SKIP_SUFFIXES:
        return True
    if rel_path.name in SKIP_FILENAMES:
        return True
    return False


def _copy_subproject(src_root: Path, dest_root: Path) -> int:
    """Copy ``oldcam-v13/`` into ``dest_root/oldcam-v13/``, skipping excluded paths.

    Args:
        src_root: Source directory (the ``oldcam-v13/`` subproject).
        dest_root: Destination parent directory (the staging root).

    Returns:
        Count of files copied for sanity-checking that the bundle isn't empty.
    """
    target = dest_root / src_root.name
    if target.is_dir():
        shutil.rmtree(target)
    elif target.exists():
        raise RuntimeError(f"Staging target {target} exists but is not a directory")
    target.mkdir(parents=True)
    file_count = 0
    for path in src_root.rglob("*"):
        rel = path.relative_to(src_root)
        if _should_skip(rel):
            continue
        out_path = target / rel
        if path.is_dir():
            out_path.mkdir(parents=True, exist_ok=True)
        elif path.is_file():
            out_path.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, out_path)
            file_count += 1
    return file_count


def _write_readme(staging_root: Path) -> None:
    """Drop a ``README_FIRST.txt`` at the staging root with run instructions.

    Args:
        staging_root: Staging directory where ``README_FIRST.txt`` will be written.
    """
    text = (
        "Oldcam V13 - Standalone Distributable\n"
        "=====================================\n\n"
        "WHAT THIS IS\n"
        "  Oldcam V13 \"High-End Daylight\" virtual hardware simulator.\n"
        "  Applies camera-physics transformations (sensor noise, OIS jitter,\n"
        "  H.264 motion compression, lens aberration) to a video file to\n"
        "  produce a realistic phone-camera output. CPU-only, runs offline,\n"
        "  no cloud calls.\n\n"
        "QUICK START\n"
        "  Windows:  double-click oldcam-v13\\oldcam_launcher.bat\n"
        "  macOS:    double-click oldcam-v13/macOS/oldcam.command\n"
        "  Both launchers create a local .venv on first run, install\n"
        "  dependencies from requirements.txt, then process the video you\n"
        "  drag onto them (or pass as arg).\n\n"
        "USAGE EXAMPLES\n"
        "  CLI direct (after venv created):\n"
        "    python oldcam-v13/oldcam.py input.mp4\n"
        "    python oldcam-v13/oldcam.py input.mp4 --output result-v13.mp4\n\n"
        "FIRST LAUNCH\n"
        "  - Need at least Python 3.10 installed system-wide.\n"
        "  - First run downloads MediaPipe face_landmarker.task model on\n"
        "    demand (~5MB) and pip-installs cv2/numpy/mediapipe (~250MB).\n"
        "  - Subsequent runs are fast (everything cached).\n"
        "  - macOS: if Gatekeeper blocks the .command file, right-click -> Open.\n\n"
        "WHAT'S IN THE BOX\n"
        "  oldcam-v13/oldcam.py              Algorithm (Windows / Linux build)\n"
        "  oldcam-v13/launcher.py            Cross-platform launcher dispatcher\n"
        "  oldcam-v13/oldcam_launcher.bat    Windows double-click launcher\n"
        "  oldcam-v13/requirements.txt       Python dependencies\n"
        "  oldcam-v13/macOS/oldcam.py        Algorithm (macOS-tuned build)\n"
        "  oldcam-v13/macOS/oldcam.command   macOS double-click launcher\n"
        "  oldcam-v13/macOS/requirements.txt macOS-tuned dependencies\n\n"
        "TROUBLESHOOTING\n"
        "  - Output looks wrong / glitchy: input video must be a portrait or\n"
        "    upright face clip. Landscape orientation may need pre-rotation.\n"
        "  - Slow processing: V13 is single-threaded by design (deterministic\n"
        "    output). A 10-second 1080p clip takes ~30-60s on an M1/Ryzen 5.\n"
        "  - MediaPipe model download fails: pre-place face_landmarker.task\n"
        "    next to oldcam.py to skip the download.\n\n"
        "LICENSE\n"
        "  Standalone Oldcam V13 algorithm bundle.\n"
    )
    (staging_root / "README_FIRST.txt").write_text(text, encoding="utf-8", newline="\n")


def main() -> int:
    """Build a shareable .zip distributable of the ``oldcam-v13/`` subproject.

    Validates ``OLDCAM_ROOT``, copies the tree into ``STAGING_DIR`` (excluding
    artifacts via :func:`_copy_subproject`), writes a ``README_FIRST.txt`` via
    :func:`_write_readme`, creates a timestamped zip in ``DIST_DIR``, then
    cleans up staging.

    Returns:
        0 on success, 1 if ``oldcam-v13/`` is missing, 2 if zero files copied
        (which would indicate the SKIP rules are too aggressive).
    """
    if not OLDCAM_ROOT.is_dir():
        print(f"ERROR: oldcam-v13 subproject not found at {OLDCAM_ROOT}", file=sys.stderr)
        return 1
    timestamp = datetime.now().strftime("%Y%m%d")
    zip_basename = f"oldcam-v13-{timestamp}"
    zip_dest = DIST_DIR / f"{zip_basename}.zip"

    DIST_DIR.mkdir(exist_ok=True)
    if STAGING_DIR.exists():
        shutil.rmtree(STAGING_DIR)
    STAGING_DIR.mkdir(parents=True)

    print(f"[1/4] Copying oldcam-v13/ -> staging at {STAGING_DIR}...")
    file_count = _copy_subproject(OLDCAM_ROOT, STAGING_DIR)
    if file_count == 0:
        print("ERROR: zero files copied — SKIP rules may be too aggressive", file=sys.stderr)
        return 2
    print(f"      copied {file_count} files")

    print(f"[2/4] Writing README_FIRST.txt at staging root...")
    _write_readme(STAGING_DIR)

    print(f"[3/4] Creating zip {zip_dest}...")
    if zip_dest.exists():
        zip_dest.unlink()
    archive_path = shutil.make_archive(
        base_name=str(DIST_DIR / zip_basename),
        format="zip",
        root_dir=str(STAGING_DIR),
    )
    archive = Path(archive_path)

    print(f"[4/4] Cleaning staging directory...")
    shutil.rmtree(STAGING_DIR)

    size_kb = archive.stat().st_size / 1024
    print()
    print(f"Done. Distributable: {archive}")
    print(f"Size: {size_kb:.1f} KB ({file_count} files)")
    print(f"Share this file directly — recipient unzips and double-clicks the launcher.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
