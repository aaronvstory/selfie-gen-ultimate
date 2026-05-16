from __future__ import annotations

import json
import os
import shutil
import zipfile
from pathlib import Path
from typing import Dict, Iterable, Set

try:
    from api_keys import API_KEY_SPECS, ensure_key_fields
    from app_version import RELEASE_VERSION
except ModuleNotFoundError:
    import sys

    REPO_ROOT = Path(__file__).resolve().parent.parent
    if str(REPO_ROOT) not in sys.path:
        sys.path.insert(0, str(REPO_ROOT))
    from api_keys import API_KEY_SPECS, ensure_key_fields
    from app_version import RELEASE_VERSION


EXCLUDED_DIRS: Set[str] = {
    ".git",
    ".venv",
    ".venv-macos",
    "build",
    "dist",
    "venv",
    "__pycache__",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
    ".claude",
    ".dual-graph",
    ".gsd",
    ".serena",
    ".planning",
    ".launcher_state",
    ".recovery",
    ".tmp_pytest",
    ".tmp",
    "handoffs",
    "reviews",
    "sessions",
    "release",
    "distribution",
    "tests",
    "tests_tmp",
}

EXCLUDED_FILES: Set[str] = {
    "kling_config.json",
    "kling_config-blink-test.json.BAK",
    "kling_gui.log",
    "kling_automation.log",
    "kling_history.json",
    "crash_log.txt",
    "ui_config.json",
}
RELEASE_BASENAME = "SelfieGenUltimate"
VERSIONED_ZIP_NAME = f"SelfieGenUltimate-{RELEASE_VERSION}.zip"
LATEST_ALIAS_ZIP_NAME = "SelfieGenUltimate.zip"


def _should_skip(path: Path) -> bool:
    """Decide whether a path should be excluded from release bundles.

    Args:
        path: Path relative to the repo root.

    Returns:
        True if the path matches excluded directories/files/extensions.
    """
    if any(part in EXCLUDED_DIRS for part in path.parts):
        return True
    if any(part.startswith("kling_ui_shareable_") for part in path.parts):
        return True
    if path.name in EXCLUDED_FILES:
        return True
    if path.name.startswith("session-ses_") and path.suffix.lower() == ".md":
        return True
    if path.name.startswith("map-codebase-session-") and path.suffix.lower() == ".md":
        return True
    if path.suffix.lower() in {".pyc", ".pyo", ".log"}:
        return True
    if path.suffix.lower() == ".bak":
        return True
    return False


def build_sanitized_config(template_path: Path) -> Dict[str, object]:
    """Build a sanitized runtime config for distributable bundles.

    Args:
        template_path: Path to default config template JSON.

    Returns:
        Config dictionary with keys/path-like runtime fields blanked.
    """
    config: Dict[str, object] = {}
    if template_path.exists():
        loaded = json.loads(template_path.read_text(encoding="utf-8"))
        if isinstance(loaded, dict):
            config.update(loaded)
    ensure_key_fields(config)
    for spec in API_KEY_SPECS:
        config[spec.config_key] = ""
    for key in ("output_folder", "automation_root_folder", "selfie_output_folder", "window_geometry"):
        config[key] = ""
    return config


def copy_sanitized_tree(repo_root: Path, dest_root: Path) -> None:
    """Copy repo content to bundle staging while excluding unsafe artifacts.

    Args:
        repo_root: Source repository root.
        dest_root: Destination staging root.
    """
    for root, dirnames, filenames in os.walk(repo_root):
        root_path = Path(root)
        rel_root = root_path.relative_to(repo_root)

        pruned_dirs = []
        for dirname in dirnames:
            rel_dir = rel_root / dirname
            if not _should_skip(rel_dir):
                pruned_dirs.append(dirname)
        dirnames[:] = pruned_dirs

        for filename in filenames:
            rel_file = rel_root / filename
            if _should_skip(rel_file):
                continue
            src_file = root_path / filename
            dest = dest_root / rel_file
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src_file, dest)


def write_bundle_readme(bundle_root: Path) -> None:
    """Write first-run instructions for the universal bundle.

    Args:
        bundle_root: Bundle directory root.
    """
    text = (
        "Selfie Gen Ultimate - Shareable Bundle\n\n"
        "1) Unzip this package.\n"
        '2) Windows: double-click "Start GUI.bat" or "Start CLI.bat".\n'
        '3) macOS: double-click "Start GUI.command" or "Start CLI.command".\n'
        "4) If macOS blocks it, right-click -> Open once.\n"
        "5) On first launch, enter required API keys.\n"
        "6) Fal.ai key is required.\n"
        "7) BFL key may be required by default automation settings.\n"
        "8) First launch creates a local virtual environment.\n"
        "9) All prompts are stored in kling_config.json (editable by GUI/CLI or manual edit).\n"
    )
    (bundle_root / "README_FIRST_RUN.txt").write_text(text, encoding="utf-8")


def _write_top_level_launchers(bundle_root: Path) -> None:
    """Write top-level Windows/macOS launcher scripts for the bundle.

    Args:
        bundle_root: Root folder of the assembled distributable bundle.

    Returns:
        None.

    Side Effects:
        Creates `Start GUI/CLI` launcher files and applies execute permission
        to generated `.command` scripts.
    """
    # newline="\r\n" is MANDATORY: a release built on macOS/Linux would
    # otherwise emit LF-only .bat files, which cmd.exe garbles on Windows
    # ('"tokens=1" is not recognized'). .bat must be CRLF regardless of the
    # host OS the release is built on (symmetric to the .command LF rule below).
    (bundle_root / "Start GUI.bat").write_text(
        "@echo off\n"
        "setlocal\n"
        "cd /d \"%~dp0\"\n"
        "call launchers\\windows\\run_gui.bat\n",
        encoding="utf-8",
        newline="\r\n",
    )
    (bundle_root / "Start CLI.bat").write_text(
        "@echo off\n"
        "setlocal\n"
        "cd /d \"%~dp0\"\n"
        "call launchers\\windows\\run_cli.bat\n",
        encoding="utf-8",
        newline="\r\n",
    )

    # newline="\n" is MANDATORY: without it, write_text() on Windows translates
    # \n -> \r\n, producing a CRLF shebang (#!/usr/bin/env bash\r) that fails on
    # macOS with `env: bash\r: No such file or directory`. .command must be LF
    # regardless of the host OS the release is built on.
    (bundle_root / "Start GUI.command").write_text(
        "#!/usr/bin/env bash\n"
        "set -euo pipefail\n"
        "cd \"$(dirname \"$0\")\"\n"
        "if [[ -f ./run_gui.command ]]; then\n"
        "  exec /bin/bash ./run_gui.command\n"
        "fi\n"
        "exec /bin/bash ./run_gui.sh\n",
        encoding="utf-8",
        newline="\n",
    )
    (bundle_root / "Start CLI.command").write_text(
        "#!/usr/bin/env bash\n"
        "set -euo pipefail\n"
        "cd \"$(dirname \"$0\")\"\n"
        "if [[ -f ./run_cli.command ]]; then\n"
        "  exec /bin/bash ./run_cli.command\n"
        "fi\n"
        "exec /bin/bash ./run_cli.sh\n",
        encoding="utf-8",
        newline="\n",
    )
    for name in ("Start GUI.command", "Start CLI.command"):
        os.chmod(bundle_root / name, 0o755)


def _make_zip_preserving_exec_bits(staging_root: Path, zip_path: Path) -> None:
    """Zip ``staging_root`` so ``.command``/``.sh`` files keep their exec bit.

    ``shutil.make_archive`` (and zipfile defaults) on Windows store a generic
    0o666 mode, so every ``.command`` in the release would extract on macOS
    WITHOUT the execute bit — Finder then opens it in a text editor instead of
    running it. We set ``ZipInfo.external_attr`` explicitly: 0o755 for shell
    launchers, 0o644 for everything else. This makes the zip correct regardless
    of the host OS the release is built on.

    Args:
        staging_root: Directory whose contents become the zip root.
        zip_path: Destination ``.zip`` path.
    """
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for path in sorted(staging_root.rglob("*")):
            if path.is_dir():
                continue
            arcname = path.relative_to(staging_root).as_posix()
            info = zipfile.ZipInfo(arcname)
            data = path.read_bytes()
            info.compress_type = zipfile.ZIP_DEFLATED
            is_exec = path.suffix in (".command", ".sh")
            # high 16 bits = Unix mode; 0o755 for launchers, 0o644 otherwise
            info.external_attr = (0o755 if is_exec else 0o644) << 16
            zf.writestr(info, data)


def bundle_release(repo_root: Path, dist_root: Path) -> Iterable[Path]:
    """Create one universal release bundle and return generated zip path.

    Args:
        repo_root: Source repository root.
        dist_root: Output root for distributable zip files.

    Returns:
        Iterable of created zip archive paths.
    """
    dist_root.mkdir(parents=True, exist_ok=True)
    for old_zip in dist_root.glob(f"{RELEASE_BASENAME}-*.zip"):
        old_zip.unlink()

    staging_root = dist_root / "_staging" / "universal"
    if staging_root.exists():
        shutil.rmtree(staging_root)
    bundle_dir = staging_root / "selfie-gen-ultimate"
    bundle_dir.mkdir(parents=True, exist_ok=True)
    copy_sanitized_tree(repo_root, bundle_dir)
    config = build_sanitized_config(bundle_dir / "default_config_template.json")
    (bundle_dir / "kling_config.json").write_text(json.dumps(config, indent=2), encoding="utf-8")
    _write_top_level_launchers(bundle_dir)
    write_bundle_readme(bundle_dir)
    versioned_zip_path = dist_root / VERSIONED_ZIP_NAME
    latest_alias_zip_path = dist_root / LATEST_ALIAS_ZIP_NAME
    for path in (versioned_zip_path, latest_alias_zip_path):
        if path.exists():
            path.unlink()
    _make_zip_preserving_exec_bits(staging_root, versioned_zip_path)
    shutil.copy2(versioned_zip_path, latest_alias_zip_path)
    created = [versioned_zip_path, latest_alias_zip_path]
    shutil.rmtree(dist_root / "_staging", ignore_errors=True)
    return created
