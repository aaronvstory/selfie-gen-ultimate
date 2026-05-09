from __future__ import annotations

import json
import os
import shutil
from pathlib import Path
from typing import Dict, Iterable, Set

try:
    from api_keys import API_KEY_SPECS, ensure_key_fields
except ModuleNotFoundError:
    import sys

    REPO_ROOT = Path(__file__).resolve().parent.parent
    if str(REPO_ROOT) not in sys.path:
        sys.path.insert(0, str(REPO_ROOT))
    from api_keys import API_KEY_SPECS, ensure_key_fields


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
    )
    (bundle_root / "README_FIRST_RUN.txt").write_text(text, encoding="utf-8")


def _write_top_level_launchers(bundle_root: Path) -> None:
    (bundle_root / "Start GUI.bat").write_text(
        "@echo off\n"
        "setlocal\n"
        "cd /d \"%~dp0\"\n"
        "call launchers\\run_gui.bat\n",
        encoding="utf-8",
    )
    (bundle_root / "Start CLI.bat").write_text(
        "@echo off\n"
        "setlocal\n"
        "cd /d \"%~dp0\"\n"
        "call launchers\\run_cli.bat\n",
        encoding="utf-8",
    )

    (bundle_root / "Start GUI.command").write_text(
        "#!/usr/bin/env bash\n"
        "set -euo pipefail\n"
        "cd \"$(dirname \"$0\")\"\n"
        "if [[ -x ./run_gui.command ]]; then\n"
        "  exec ./run_gui.command\n"
        "fi\n"
        "exec ./run_gui.sh\n",
        encoding="utf-8",
    )
    (bundle_root / "Start CLI.command").write_text(
        "#!/usr/bin/env bash\n"
        "set -euo pipefail\n"
        "cd \"$(dirname \"$0\")\"\n"
        "if [[ -x ./run_cli.command ]]; then\n"
        "  exec ./run_cli.command\n"
        "fi\n"
        "exec ./run_cli.sh\n",
        encoding="utf-8",
    )
    for name in ("Start GUI.command", "Start CLI.command"):
        os.chmod(bundle_root / name, 0o755)


def bundle_release(repo_root: Path, dist_root: Path) -> Iterable[Path]:
    """Create one universal release bundle and return generated zip path.

    Args:
        repo_root: Source repository root.
        dist_root: Output root for distributable zip files.

    Returns:
        Iterable of created zip archive paths.
    """
    dist_root.mkdir(parents=True, exist_ok=True)
    for old_zip in dist_root.glob("SelfieGenUltimate-*.zip"):
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
    zip_path = dist_root / "SelfieGenUltimate.zip"
    if zip_path.exists():
        zip_path.unlink()
    archive_path = shutil.make_archive(str(zip_path.with_suffix("")), "zip", root_dir=staging_root)
    created = [Path(archive_path)]
    shutil.rmtree(dist_root / "_staging", ignore_errors=True)
    return created
