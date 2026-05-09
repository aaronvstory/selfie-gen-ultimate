from __future__ import annotations

import json
import os
import shutil
from datetime import datetime
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
    ".venv-macos",
    "venv",
    "__pycache__",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
    ".claude",
    ".serena",
    ".planning",
    ".tmp",
    "sessions",
    "release",
}

EXCLUDED_FILES: Set[str] = {
    "kling_config.json",
    "kling_config-blink-test.json.BAK",
    "kling_gui.log",
    "kling_automation.log",
    "kling_history.json",
    "crash_log.txt",
}


def _should_skip(path: Path) -> bool:
    if any(part in EXCLUDED_DIRS for part in path.parts):
        return True
    if path.name in EXCLUDED_FILES:
        return True
    if path.suffix.lower() in {".pyc", ".pyo", ".log"}:
        return True
    return False


def build_sanitized_config(template_path: Path) -> Dict[str, object]:
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


def write_bundle_readme(bundle_root: Path, flavor: str) -> None:
    instructions = {
        "windows_gui": "Start with launchers\\run_gui.bat",
        "windows_cli": "Start with launchers\\run_cli.bat",
        "macos_portable": "Run ./setup_macos.sh once, then ./run_gui.sh or ./run_cli.sh",
    }
    text = (
        "Selfie Gen Ultimate - Shareable Bundle\n\n"
        f"Launch: {instructions[flavor]}\n\n"
        "First launch key setup:\n"
        "- Fal.ai key is required at startup: https://fal.ai/dashboard/keys\n"
        "- Optional keys: BFL (https://api.bfl.ai/), OpenRouter (https://openrouter.ai/keys), "
        "Freeimage (https://freeimage.host/page/api)\n"
    )
    (bundle_root / "README_FIRST_RUN.txt").write_text(text, encoding="utf-8")


def bundle_release(repo_root: Path, release_root: Path) -> Iterable[Path]:
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    version_root = release_root / f"release_{stamp}"
    version_root.mkdir(parents=True, exist_ok=True)

    bundles = ["windows_gui", "windows_cli", "macos_portable"]
    created = []
    for name in bundles:
        bundle_dir = version_root / name / "selfie-gen-ultimate"
        bundle_dir.mkdir(parents=True, exist_ok=True)
        copy_sanitized_tree(repo_root, bundle_dir)
        config = build_sanitized_config(bundle_dir / "default_config_template.json")
        (bundle_dir / "kling_config.json").write_text(json.dumps(config, indent=2), encoding="utf-8")
        write_bundle_readme(bundle_dir, name)
        zip_base = version_root / name
        archive_path = shutil.make_archive(str(zip_base), "zip", root_dir=(version_root / name))
        created.append(Path(archive_path))
    return created
