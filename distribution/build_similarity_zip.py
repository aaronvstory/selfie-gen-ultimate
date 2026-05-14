"""Build a shareable .zip distributable of the standalone `similarity/` subproject.

Walks `similarity/` (NOT the whole repo), excludes runtime/build artifacts and
virtualenvs, stages into `dist/similarity-staging/` with a top-level
`similarity/` folder + a `README_FIRST.txt`, then zips it as
`dist/similarity-vX.Y.Z-YYYYMMDD.zip`.

Run:
    python distribution/build_similarity_zip.py

The zip is small (~5-10MB) because it ships source code only — the recipient's
first launch creates a local venv and downloads ML models (~1.5GB) on demand.

Inspired by the `_should_skip` pattern in `distribution/release_prep.py` but
narrower scope (subproject only) and no full-repo bundle scaffolding.
"""

from __future__ import annotations

import re
import shutil
import sys
from datetime import datetime
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SIM_ROOT = REPO_ROOT / "similarity"
DIST_DIR = REPO_ROOT / "dist"
STAGING_DIR = DIST_DIR / "similarity-staging"
CHANGELOG = SIM_ROOT / "CHANGELOG.md"

# Path components to skip during the walk. Matched against any path segment
# (case-insensitive on Windows by default since Path comparison is OS-aware).
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

# File suffixes / exact filenames to skip.
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
    or any name in SKIP_FILENAMES is dropped. Wildcards in SKIP_DIRS are
    handled with a simple ``endswith`` for the ``*.egg-info`` case.

    Args:
        rel_path: Path relative to the similarity root to check.

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


def _detect_version() -> str:
    """Read the latest released version tag from ``similarity/CHANGELOG.md``.

    Falls back to ``"unreleased"`` if no ``[x.y.z]`` header is found. The
    ``[Unreleased]`` section is intentionally skipped — we want a stable
    shippable version string for the zip filename, not a moving target.

    Returns:
        Version string in ``"x.y.z"`` format, or ``"unreleased"`` if no
        released version header is present (or the CHANGELOG is missing).
    """
    if not CHANGELOG.is_file():
        return "unreleased"
    pattern = re.compile(r"^##\s*\[(\d+\.\d+\.\d+)\]", re.MULTILINE)
    match = pattern.search(CHANGELOG.read_text(encoding="utf-8"))
    return match.group(1) if match else "unreleased"


def _copy_subproject(src_root: Path, dest_root: Path) -> int:
    """Copy ``similarity/`` into ``dest_root/similarity/``, skipping excluded paths.

    Args:
        src_root: Source directory to copy (e.g., the ``similarity/`` subproject).
        dest_root: Destination parent directory (the staging root).

    Returns:
        Count of files copied for sanity-checking that the bundle isn't empty
        (zero would indicate the SKIP rules are too aggressive).
    """
    target = dest_root / src_root.name
    # Use is_dir() rather than exists() so a stray file at this path raises a
    # clear error rather than rmtree's misleading "not a directory" trace
    # (coderabbit nit on PR #19).
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


def _write_readme(staging_root: Path, version: str) -> None:
    """Drop a ``README_FIRST.txt`` at the staging root with run instructions.

    Sits ALONGSIDE the ``similarity/`` folder so the recipient sees it first
    when they unzip. Uses LF line endings so it renders cleanly on macOS,
    Linux, and modern Windows (Notepad gained LF support in Win10 1809).

    Args:
        staging_root: Staging directory path where ``README_FIRST.txt`` will
            be written.
        version: Version string to include in the README header.
    """
    text = (
        f"Face Similarity Pro - Standalone Distributable (v{version})\n"
        "============================================================\n\n"
        "WHAT THIS IS\n"
        "  An offline, locally-running face-similarity tool. No biometric data\n"
        "  ever leaves your machine. ArcFace + Facenet512 ensemble for the\n"
        "  similarity score, RetinaFace for face detection, optional DeepFace\n"
        "  anti-spoofing for liveness checking.\n\n"
        "QUICK START\n"
        "  Windows:  double-click similarity\\run_gui.bat (GUI)\n"
        "                          similarity\\run_cli.bat (terminal)\n"
        "  macOS:    double-click similarity/run_gui.command (GUI)\n"
        "                          similarity/run_cli.command (terminal)\n"
        "  Linux:    bash similarity/run_cli.sh from the terminal\n\n"
        "FIRST LAUNCH\n"
        "  - The launcher creates a local virtual environment in similarity/.venv\n"
        "    (one-time, takes a couple of minutes).\n"
        "  - On first comparison, the ML models download from the DeepFace mirror\n"
        "    (~1.5GB total: ArcFace, Facenet512, RetinaFace, anti-spoof).\n"
        "    Subsequent runs are fast (models cached in ~/.deepface/).\n"
        "  - macOS: if Gatekeeper blocks the .command file, right-click -> Open.\n\n"
        "WHAT'S IN THE BOX\n"
        "  similarity/main.py            Entry point (dispatches to GUI or CLI)\n"
        "  similarity/src/               Engine, GUI, CLI source\n"
        "  similarity/extract_face.py    Standalone face extraction tool\n"
        "  similarity/requirements.txt   Python dependencies (auto-installed)\n"
        "  similarity/README.md          Full documentation\n"
        "  similarity/CHANGELOG.md       Version history\n\n"
        "TROUBLESHOOTING\n"
        "  - Need at least Python 3.10 installed system-wide for the venv setup.\n"
        "  - First launch downloads models — needs an internet connection.\n"
        "  - Model files cached at ~/.deepface/weights/ (Windows: %USERPROFILE%\\.deepface\\weights\\)\n"
        "    — you can pre-seed these to skip the download.\n\n"
        "LICENSE\n"
        "  Standalone face similarity application bundle.\n"
        "  See similarity/README.md for full project details.\n"
    )
    (staging_root / "README_FIRST.txt").write_text(text, encoding="utf-8", newline="\n")


def main() -> int:
    """Build a shareable .zip distributable of the ``similarity/`` subproject.

    Validates ``SIM_ROOT``, copies the tree into ``STAGING_DIR`` (excluding
    artifacts via :func:`_copy_subproject`), detects the version from
    ``CHANGELOG.md`` via :func:`_detect_version`, writes a ``README_FIRST.txt``
    via :func:`_write_readme`, creates a timestamped zip in ``DIST_DIR``, then
    cleans up staging.

    Returns:
        0 on success, 1 if ``similarity/`` is missing, 2 if zero files copied
        (which would indicate the SKIP rules are too aggressive).
    """
    if not SIM_ROOT.is_dir():
        print(f"ERROR: similarity subproject not found at {SIM_ROOT}", file=sys.stderr)
        return 1
    version = _detect_version()
    timestamp = datetime.now().strftime("%Y%m%d")
    zip_basename = f"similarity-v{version}-{timestamp}"
    zip_dest = DIST_DIR / f"{zip_basename}.zip"

    DIST_DIR.mkdir(exist_ok=True)
    if STAGING_DIR.exists():
        shutil.rmtree(STAGING_DIR)
    STAGING_DIR.mkdir(parents=True)

    print(f"[1/4] Copying similarity/ -> staging at {STAGING_DIR}...")
    file_count = _copy_subproject(SIM_ROOT, STAGING_DIR)
    if file_count == 0:
        print("ERROR: zero files copied — SKIP rules may be too aggressive", file=sys.stderr)
        return 2
    print(f"      copied {file_count} files")

    print(f"[2/4] Writing README_FIRST.txt at staging root...")
    _write_readme(STAGING_DIR, version)

    print(f"[3/4] Creating zip {zip_dest}...")
    if zip_dest.exists():
        zip_dest.unlink()
    # shutil.make_archive appends ".zip" automatically when format="zip".
    archive_path = shutil.make_archive(
        base_name=str(DIST_DIR / zip_basename),
        format="zip",
        root_dir=str(STAGING_DIR),
    )
    archive = Path(archive_path)

    print(f"[4/4] Cleaning staging directory...")
    shutil.rmtree(STAGING_DIR)

    size_mb = archive.stat().st_size / (1024 * 1024)
    print()
    print(f"Done. Distributable: {archive}")
    print(f"Size: {size_mb:.1f} MB ({file_count} files)")
    print(f"Share this file directly — recipient unzips and double-clicks the launcher.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
