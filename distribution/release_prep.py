from __future__ import annotations

import json
import os
import shutil
import zipfile
from pathlib import Path
from typing import Dict, Iterable, Set, Optional

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


# Machine-specific / per-install fields blanked in the shipped config
# (everything else of the user's current state is preserved verbatim).
_DIST_BLANKED_PATH_KEYS = (
    "output_folder",
    "automation_root_folder",
    "selfie_output_folder",
)
# window_geometry is intentionally NOT blanked (user 2026-05-19: ship
# the dev's window sizing too — everything except API keys).


def build_sanitized_config(
    template_path: Path,
    live_config_path: Optional[Path] = None,
) -> Dict[str, object]:
    """Build the runtime config shipped inside a release bundle.

    The bundle must carry the user's CURRENT state -- ALL saved prompt
    slots, every setting and default exactly as configured -- so a fresh
    install behaves like the dev machine. Only secrets (the four API
    keys) and machine-specific paths are blanked.

    Sourcing order:
      1. ``live_config_path`` (the dev machine's real
         ``kling_config.json``) -- the full ~140-key current state.
      2. ``template_path`` (``default_config_template.json``) merged in
         ONLY for keys the live config is missing, so a brand-new key
         still gets a sane default if the dev never touched it.
      3. If neither exists, an empty config (ensure_key_fields fills the
         required key fields).

    Args:
        template_path: Path to ``default_config_template.json``.
        live_config_path: Path to the dev machine's ``kling_config.json``
            (the source of truth for current prompts/settings).

    Returns:
        Config dict: full current state with API keys + machine paths
        blanked.
    """
    template: Dict[str, object] = {}
    if template_path.exists():
        loaded = json.loads(template_path.read_text(encoding="utf-8"))
        if isinstance(loaded, dict):
            template.update(loaded)

    config: Dict[str, object] = {}
    if live_config_path is not None and live_config_path.exists():
        # A corrupt / non-JSON live config must NOT abort the release
        # build -- fall back to the template-only path (old behaviour).
        try:
            loaded_live = json.loads(
                live_config_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError, ValueError):
            loaded_live = None
        if isinstance(loaded_live, dict):
            config.update(loaded_live)

    # Template only fills GAPS -- it never overwrites a value the user
    # actually set (preserve current state verbatim).
    for key, value in template.items():
        config.setdefault(key, value)

    # A handful of keys are FORCED to the project's new desired
    # defaults even if the dev machine still carries an older value
    # (user 2026-05-19: ship the new minimal-motion prompt + negative,
    # default model = Kling 2.5 Turbo Pro, end-frame lock on). Sourced
    # from the template so there is ONE definition of the new defaults.
    _t_saved = template.get("saved_prompts")
    _t_neg = template.get("negative_prompts")
    # The bundle ships current_prompt_slot from the template (4 in
    # practice). Force that slot to the template's value too, NOT
    # just slot 1 — the GUI/CLI generate from the ACTIVE slot, so a
    # dev machine carrying legacy slot-4 text would otherwise ship
    # the old high-motion prompt + empty negative despite this
    # override (Codex P2, PR #41). Pin current_prompt_slot itself so
    # the dev's stale slot choice can't carry either.
    _tmpl_slot = str(template.get("current_prompt_slot", 4))
    config["current_prompt_slot"] = template.get("current_prompt_slot", 4)
    # Force slot 1 (the proven minimal-motion fallback) AND the
    # active slot, but each from its OWN template text — the active
    # slot now carries a distinct "enhanced for kling 2.5 pro"
    # prompt + negative, so stamping slot-1's text onto it (the old
    # behaviour) would clobber the shipped enhanced prompt. A dev
    # machine's stale slot text still can't survive — we overwrite
    # from the template, falling back to slot 1 only if the active
    # slot is empty in the template (PR #41, user request).
    _force_slots = {"1", _tmpl_slot}
    if isinstance(_t_saved, dict) and _t_saved.get("1"):
        sp = dict(config.get("saved_prompts") or {})
        for _sl in _force_slots:
            sp[_sl] = _t_saved.get(_sl) or _t_saved["1"]
        config["saved_prompts"] = sp
    if isinstance(_t_neg, dict) and _t_neg.get("1"):
        npd = dict(config.get("negative_prompts") or {})
        for _sl in _force_slots:
            npd[_sl] = _t_neg.get(_sl) or _t_neg["1"]
        config["negative_prompts"] = npd
    # Ship the active slot's title too (e.g. "enhanced for kling
    # 2.5 pro") so the GUI shows the right label on first launch.
    _t_titles = template.get("prompt_titles")
    if isinstance(_t_titles, dict):
        pt = dict(config.get("prompt_titles") or {})
        for _sl in _force_slots:
            if _t_titles.get(_sl):
                pt[_sl] = _t_titles[_sl]
        config["prompt_titles"] = pt
    # Template-driven so the next default-model bump is a single
    # default_config_template.json edit, not a literal change here
    # too (CodeRabbit Refactor, PR #41). Hardcoded fallback is the
    # current ship target so a template missing those keys still
    # builds a working bundle.
    config["current_model"] = str(
        template.get(
            "current_model",
            "fal-ai/kling-video/v2.5-turbo/pro/image-to-video",
        )
    ).strip()
    config["model_display_name"] = str(
        template.get("model_display_name", "Kling 2.5 Turbo Pro")
    ).strip()
    config["lock_end_frame"] = True
    # Unconditionally OVERRIDE (not setdefault) — a stale live
    # cfg_scale_value (e.g. 0.5) must not survive into the bundle;
    # the intended shipped default is 0.7 (Codex P3, PR #41).
    config["cfg_scale_value"] = template.get("cfg_scale_value", 0.7)
    config["rppg_metrics_in_filename"] = bool(
        template.get("rppg_metrics_in_filename", False)
    )
    # Composite modes are user-facing ship defaults that must be
    # deterministic — OVERRIDE from the template (not inherit a
    # stale dev kling_config.json value). Step 2.5 selfie expand
    # ships raw AI output ('none'); Step 0 Face Crop / outpaint
    # ships 'preserve_seamless' (user request, PR #41).
    config["automation_selfie_expand_composite_mode"] = template.get(
        "automation_selfie_expand_composite_mode", "none"
    )
    config["outpaint_composite_mode"] = template.get(
        "outpaint_composite_mode", "preserve_seamless"
    )

    ensure_key_fields(config)
    for spec in API_KEY_SPECS:
        config[spec.config_key] = ""
    for key in _DIST_BLANKED_PATH_KEYS:
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
    config = build_sanitized_config(
        bundle_dir / "default_config_template.json",
        live_config_path=repo_root / "kling_config.json",
    )
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
