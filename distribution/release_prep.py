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
    # Venv dirs. Cover every flavor a contributor might create:
    # canonical (.venv, venv), platform-suffixed (.venv-macos), and
    # version-suffixed (.venv311 from `python3.11 -m venv .venv311`)
    # — both dotted and undotted forms, since `python -m venv venv311`
    # without the leading dot is also accepted and would otherwise leak.
    # Forgetting one inflates the release zip by hundreds of MB; add
    # new variants here whenever a new Python minor lands.
    ".venv",
    ".venv-macos",
    ".venv311",
    ".venv312",
    ".venv313",
    ".venv314",
    "venv311",
    "venv312",
    "venv313",
    "venv314",
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

# PR #51 (Windows-side dist verification): gitignored research/A-B-testing
# artifacts that must never ship in the release bundle. release_prep sweeps
# the working tree (not git ls-files), so .gitignore alone doesn't shield
# them — they must also be in EXCLUDED_DIRS below. Discovered when a
# Windows-side build came in at 182 MB instead of the expected ~10 MB.
# Named separately from the legacy EXCLUDED_DIRS entries so tests can
# derive the expected set from a single source of truth (anti-drift).
LOCAL_ONLY_RESEARCH_DIRS: Set[str] = {
    "oldcam_reference_bundle",   # ~5 MB confidential analysis docs
    "analysis_frames",           # ~3 MB frame extracts for offline analysis
    "test-material",             # ~35 MB fixture videos
    "rppg_harness_out",          # rPPG harness byproducts (lives nested
                                 # under oldcam-testing/ in practice)
    "_friend_logs",              # PII: friend debug logs with real emails +
                                 # machine paths. .gitignore alone does NOT
                                 # shield it from the working-tree release sweep
                                 # (Codex P1 PR #72 — same leak class as the
                                 # PR #51 PII + PR #61 analysis-file leaks).
    "aa-video",                  # adversarial-attack subproject (sensitive
                                 # detector-evasion tool + heavy isolated venv).
                                 # The committed automation/video_aa.py wrapper
                                 # graceful-skips when this dir is absent, so the
                                 # shipped build runs without it (2026-06-18).
}
# Note: oldcam-testing/ itself is kept (frozen A/B oldcam_v*.py files are
# tracked + ship). Only the byproduct subdir + the gitignored *.mp4 / reports/
# entries inside it are excluded; see _should_skip path-aware filter below.
EXCLUDED_DIRS |= LOCAL_ONLY_RESEARCH_DIRS

# PR #51 round-1 code review (CRITICAL): gitignored corpus measurement
# outputs containing SSN-format persona identifiers (78 + 40 hits at
# the time of discovery). They were leaking to every release zip because
# .gitignore alone doesn't shield from the release sweep. PII redaction
# is more important than the bloat savings. Named separately so tests
# can derive the expected set.
PII_EXCLUDED_FILES: Set[str] = {
    "sourav_facetrack_results.json",
    "sourav_kinematic_results.json",
}

# PR #61 follow-up (release-hardening before the friend-facing v2.10 zip):
# local-only analysis artifacts that were added to .gitignore but NOT to the
# release sweep. Same bug class as the PR #51 research-dir leak — release_prep
# walks the working tree, so .gitignore alone doesn't shield them. These two
# top-level briefs (~130 KB of internal A/B decision notes) were shipping in
# every personal zip. Named separately so the test can derive the expected set.
LOCAL_ANALYSIS_FILES: Set[str] = {
    "OLDCAM_DECISION_BRIEF.md",
    "OLDCAM_GUIDE.md",
}

EXCLUDED_FILES: Set[str] = {
    "kling_config.json",
    "kling_config-blink-test.json.BAK",
    "kling_gui.log",
    "kling_automation.log",
    "kling_history.json",
    "crash_log.txt",
    "ui_config.json",
} | PII_EXCLUDED_FILES | LOCAL_ANALYSIS_FILES

# OS-junk files matched CASE-INSENSITIVELY (stored lowercased). release_prep
# sweeps the WORKING TREE, and on macOS Finder constantly regenerates .DS_Store
# (gitignored ≠ excluded) — these shipped into the v2.32 zip. Case-insensitive
# because the macOS/Windows filesystems that create them are case-insensitive,
# so a variant like Desktop.ini / thumbs.db can surface (cross-OS bounce trap:
# OS-junk). Kept separate from the case-SENSITIVE EXCLUDED_FILES above (which
# holds real config filenames that must match exactly).
_OS_JUNK_FILENAMES: Set[str] = {".ds_store", "thumbs.db", "desktop.ini"}

# Path-relative directory prefixes pruned from the release sweep. Unlike
# EXCLUDED_DIRS (which matches a bare dir NAME anywhere in the tree), these are
# anchored to a specific location so we don't over-prune a same-named dir
# elsewhere. Mirrors the .gitignore entries added in the PR #61 round:
#   - docs/analysis/  : committed + gitignored A/B study scripts, frames, JSON
#   - rPPG/iteration_history/ : per-run rPPG iteration JSON/TSV byproducts
# Stored as POSIX tuples so the match is OS-independent.
LOCAL_ANALYSIS_DIR_PREFIXES: tuple = (
    ("docs", "analysis"),
    ("rPPG", "iteration_history"),
)
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
    # PR #96 round 7: agent/review scratch artifacts (.scratch_*.txt review
    # outputs, .scratch_* probe venvs/scripts). Gitignored, but release_prep
    # sweeps the WORKING TREE — 7 of them shipped in the first v2.31 zip.
    # Prefix match on any path part covers files and dirs alike.
    if any(part.startswith(".scratch") for part in path.parts):
        return True
    if path.name in EXCLUDED_FILES:
        return True
    if path.name.lower() in _OS_JUNK_FILENAMES:
        return True
    if path.name.startswith("session-ses_") and path.suffix.lower() == ".md":
        return True
    if path.name.startswith("map-codebase-session-") and path.suffix.lower() == ".md":
        return True
    if path.suffix.lower() in {".pyc", ".pyo", ".log"}:
        return True
    if path.suffix.lower() == ".bak":
        return True
    # PR #51 round-1 code review (HIGH): stray *.zip artifacts in the repo
    # working tree (oldcam_reference_bundle.zip, oldcam-v13.zip, rPPG
    # injector zip) were shipping. All gitignored (.gitignore:*.zip) but
    # release_prep sweeps the working tree, not git ls-files. Skipping all
    # .zip extensions is safe here because the dist output goes to dist/
    # which is already in EXCLUDED_DIRS — global .zip skip only affects
    # sibling stray zips, not the release output.
    if path.suffix.lower() == ".zip":
        return True
    # PR #50/#51 follow-up: prune gitignored byproducts inside oldcam-testing/.
    # The dir itself stays (frozen oldcam_v*.py scripts ship as research
    # artifacts), but two byproduct classes must not bloat the release zip:
    #   - *.mp4 fixtures (~134 MB on a contributor's working tree)
    #   - reports/ HTML A/B reports (~112 KB — scoping to oldcam-testing/
    #     specifically because a future repo-root reports/ would be unrelated)
    # Mirrors the .gitignore patterns oldcam-testing/*.mp4 +
    # oldcam-testing/reports/. The rppg_harness_out/ dir is already in
    # EXCLUDED_DIRS above and prunes via the dir-name match. Consolidated
    # from two adjacent ifs per Gemini round-1 review.
    if len(path.parts) >= 2 and path.parts[0] == "oldcam-testing":
        if path.suffix.lower() == ".mp4" or path.parts[1] == "reports":
            return True
    # PR #61 follow-up: prune local-only analysis dirs anchored to a specific
    # location (docs/analysis/, rPPG/iteration_history/). Anchored prefix match
    # — not a bare dir-name match — so a future unrelated analysis/ elsewhere is
    # unaffected. Mirrors the .gitignore entries added the same round.
    for prefix in LOCAL_ANALYSIS_DIR_PREFIXES:
        if path.parts[: len(prefix)] == prefix:
            return True
    # PR #61 follow-up: rPPG injector iteration byproducts. The injector writes
    # temp_iteration_N.mp4 + best_iteration_snapshot[_N].mp4 into rPPG/ during a
    # run (see rppg-wiring.md "Injector contract gotcha #2"). All gitignored but
    # shipping. Scope to rPPG/ so a same-named file elsewhere is unaffected.
    if len(path.parts) >= 2 and path.parts[0] == "rPPG":
        name = path.name
        if name.startswith("temp_iteration_") and path.suffix.lower() == ".mp4":
            return True
        if name.startswith("best_iteration_snapshot") and path.suffix.lower() == ".mp4":
            return True
    return False


# Machine-specific / per-install fields blanked in the shipped config
# (everything else of the user's current state is preserved verbatim).
#
# v2.7 dist-build audit (2026-05-28) caught two "last_*" path fields that
# remembered the dev's most-recent input file/folder. The video-inspector
# one leaked a subject's full name in the path string — CLAUDE.md Trap 4
# (PII in working-tree-only files). Adding them here so the shipped zip
# stays subject-name-free and machine-path-free.
_DIST_BLANKED_PATH_KEYS = (
    "output_folder",
    "automation_root_folder",
    "selfie_output_folder",
    "oldcam_last_source_video",
    "video_inspector_last_folder",
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
        # Guard against a user-edited live kling_config.json carrying
        # a non-mapping at saved_prompts — without this dict(non_dict)
        # raises ValueError and aborts release prep instead of
        # gracefully sanitizing (Codex P2, PR #41).
        _live_sp = config.get("saved_prompts")
        sp = dict(_live_sp) if isinstance(_live_sp, dict) else {}
        for _sl in _force_slots:
            sp[_sl] = _t_saved.get(_sl) or _t_saved["1"]
        config["saved_prompts"] = sp
    if isinstance(_t_neg, dict) and _t_neg.get("1"):
        _live_np = config.get("negative_prompts")
        npd = dict(_live_np) if isinstance(_live_np, dict) else {}
        for _sl in _force_slots:
            npd[_sl] = _t_neg.get(_sl) or _t_neg["1"]
        config["negative_prompts"] = npd
    # Ship the active slot's title too (e.g. "enhanced for kling
    # 2.5 pro") so the GUI shows the right label on first launch.
    _t_titles = template.get("prompt_titles")
    if isinstance(_t_titles, dict):
        _live_pt = config.get("prompt_titles")
        pt = dict(_live_pt) if isinstance(_live_pt, dict) else {}
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
    # CLI-owned per-surface video selection (PR #96 v2.31 split): the CLI
    # pipeline ships Kling 2.5 Turbo STANDARD on prompt slot 4 at 10s —
    # independent of the GUI's Pro default above. Without these a
    # template-only build (no live kling_config.json) would leave the
    # resolvers falling back to the GUI keys, so headless --batch (which
    # never runs the interactive defaults migration) would generate with
    # Pro / slot 5 (codex P2, PR #96 round 9). Deliberately NOT stamping
    # automation_recommended_defaults_version: the first interactive
    # launch silently applies the full recommended baseline on top.
    config["cli_video_model"] = str(
        template.get(
            "cli_video_model",
            "fal-ai/kling-video/v2.5-turbo/standard/image-to-video",
        )
    ).strip()
    config["cli_video_model_display_name"] = str(
        template.get("cli_video_model_display_name", "Kling 2.5 Turbo Standard")
    ).strip()
    config["cli_kling_prompt_slot"] = template.get("cli_kling_prompt_slot", 4)
    config["cli_video_duration"] = template.get("cli_video_duration", 10)
    # Template-driven so default_config_template.json explicitly
    # setting lock_end_frame: false actually ships false (Codex P2,
    # PR #41). Unparseable / missing template key -> True (the
    # canonical default; matches the queue_manager + pipeline
    # _parse_bool None -> True coercion).
    from face_similarity import _parse_bool as _pb_release
    _t_lock = _pb_release(template.get("lock_end_frame", True))
    config["lock_end_frame"] = True if _t_lock is None else bool(_t_lock)
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
    # Step 0 Generative Expand "Run 2x" — as of PR #81 (v2.25) this is
    # SESSION-ONLY state, never persisted. The user mandate 2026-06-06:
    # "for all versions all future dists never should 'run 2x' be checked
    # by default". The key is STRIPPED from the bundle config so the
    # GUI's BooleanVar always defaults to False at launch.
    config.pop("outpaint_double_expand", None)
    # Pre-stamp the one-time migration markers so a fresh-bundle launch
    # does not re-fire any migration (it's a no-op anyway, but this keeps
    # the "first launch is silent" promise).
    # PR fix/step0-composite-and-rppg-v2.5 (v2), round 10 (v3), PR #81 (v4).
    config["outpaint_2x_default_reset_v2"] = bool(
        template.get("outpaint_2x_default_reset_v2", True)
    )
    config["outpaint_2x_default_reset_v3"] = bool(
        template.get("outpaint_2x_default_reset_v3", True)
    )
    config["outpaint_2x_session_only_v4"] = bool(
        template.get("outpaint_2x_session_only_v4", True)
    )
    # v2.3 ship defaults (user request 2026-05-22): loop OFF.
    # Dev kling_config.json typically still carries ``loop_videos: True``
    # from prior sessions; without the override the new template default
    # would never reach the bundle. OVERRIDE (not setdefault) for the
    # same reason as outpaint_double_expand above.
    config["loop_videos"] = bool(template.get("loop_videos", False))
    # v2.3 ship default (user direction 2026-05-22 final): "fal" for
    # everything everywhere. The Phase A revert (a1c1b099) was over-
    # broad — what the user actually wanted reverted was just the
    # macOS LANCZOS + 16px composite tweaks in outpaint_generator.py
    # (rolled back in d48bbc8), NOT the provider default itself.
    # Switching providers is a one-click dropdown change in the GUI;
    # the default should be "fal" out of the box. OVERRIDE (not
    # setdefault) so a dev kling_config.json carrying "bfl" from a
    # tuned session doesn't leak into the bundle.
    #
    # GUI provider key:
    config["outpaint_provider"] = str(template.get("outpaint_provider", "fal"))
    # Automation pipeline provider keys (CodeRabbit Major on
    # 36b5e0b 2026-05-22): the GUI ``outpaint_provider`` only
    # affects the manual tab dispatch. The automation CLI uses two
    # parallel keys (front + selfie) defaulting to "bfl" in
    # automation/config.py DEFAULTS. Without these overrides, a dev
    # kling_config.json carrying the old "bfl" automation defaults
    # would ship to fresh-clone users and they'd get BFL for
    # automation runs even though the GUI now defaults to fal.
    # Force both to "fal" to match Phase A intent.
    config["automation_front_expand_provider"] = "fal"
    config["automation_selfie_expand_provider"] = "fal"
    # Phase E of polish/v2.3 (2026-05-22): the new pipeline order is
    # Kling -> rPPG -> Loop -> Oldcam. The slower legacy per-Oldcam
    # fan-out (one rPPG injection per Oldcam version) is preserved
    # behind this opt-in flag. Default OFF; dev kling_config.json
    # values from prior sessions don't leak into the bundle.
    config["rppg_per_oldcam_fanout"] = bool(
        template.get("rppg_per_oldcam_fanout", False)
    )
    # Post-processing fan-out mode (2026-06-19): seed the bundle from the
    # template (powerset default) so a dev kling_config.json's mode never
    # leaks into the release.
    config["postproc_fanout_mode"] = str(
        template.get("postproc_fanout_mode", "separate_and_combined")
    )
    # Subagent HIGH on 286613c (2026-05-22): Phase G per-section
    # expand prompts (face_crop_expand_prompt, selfie_expand_prompt,
    # outpaint_tab_prompt) were filled via the earlier setdefault
    # merge — but setdefault won't overwrite an existing empty
    # string. A dev kling_config.json that saved any of these as ""
    # (e.g. while testing R4's "explicit empty string is preserved"
    # behaviour) would ship a bundle with blank expand prompts.
    # Fix: replace blank/whitespace-only values with the template
    # text. Non-empty user values still survive (intentional dev
    # customisation is preserved into the bundle).
    for _pk in (
        "face_crop_expand_prompt",
        "selfie_expand_prompt",
        "outpaint_tab_prompt",
    ):
        _live = config.get(_pk, "")
        if not isinstance(_live, str) or not _live.strip():
            _template_value = template.get(_pk)
            if isinstance(_template_value, str):
                config[_pk] = _template_value

    ensure_key_fields(config)
    for spec in API_KEY_SPECS:
        config[spec.config_key] = ""
    for key in _DIST_BLANKED_PATH_KEYS:
        config[key] = ""
    # _env_key_optout is PER-MACHINE state: it lists keys the building developer
    # explicitly "cleared" in their own GUI/CLI. Shipping it would silently
    # suppress the env-var fallback for a RECIPIENT who has e.g. FAL_KEY set —
    # defeating the auto-prefill feature for new users (code-review MEDIUM,
    # PR #73). Reset it like the blanked path keys.
    config["_env_key_optout"] = []
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
        "5) No API key is required to start. rPPG / Oldcam / Loop re-runs work with no key.\n"
        "6) Add a Fal.ai key (via the bottom-bar badge or CLI settings) only when you want to GENERATE video/selfies.\n"
        "7) BFL key is optional (only needed if you switch providers to BFL in the GUI).\n"
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
    # FLAT layout (user direction 2026-06-04): the app files live at the ZIP
    # ROOT, not under a nested ``selfie-gen-ultimate/`` folder. Windows Explorer
    # already extracts ``SelfieGenUltimate-vX.Y.zip`` into a
    # ``SelfieGenUltimate-vX.Y/`` folder, so the old inner folder produced an
    # ugly double nest (``…-personal/selfie-gen-ultimate/<app>``). Zipping the
    # bundle CONTENTS at the root means one clean level: ``…-personal/<app>``.
    # Nothing depends on the inner dir name at runtime — the top-level
    # ``Start GUI.bat`` does ``cd /d "%~dp0"`` and calls launchers relatively.
    bundle_dir = staging_root
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
    _make_zip_preserving_exec_bits(bundle_dir, versioned_zip_path)
    shutil.copy2(versioned_zip_path, latest_alias_zip_path)
    created = [versioned_zip_path, latest_alias_zip_path]
    shutil.rmtree(dist_root / "_staging", ignore_errors=True)
    return created
