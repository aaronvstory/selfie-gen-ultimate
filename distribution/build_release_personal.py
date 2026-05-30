"""Build a personal release zip that ships the user's CURRENT live config.

Unlike the standard distribution/build_release.py, this build:
  - Preserves the user's live kling_config.json values VERBATIM for the
    full ~140-key surface (prompts, slots, model, cfg_scale, end-frame
    lock, composite modes, expand provider, loop toggle, sash positions,
    automation settings, similarity thresholds, selfie model selections,
    wildcard templates, oldcam/rPPG toggles, window geometry, etc.).
  - Bypasses release_prep's forced-override block (~15 keys it would
    otherwise reset to template defaults).
  - STILL blanks the 4 API keys (falai, freeimage, bfl, openrouter) so
    the zip is safe to share with a trusted teammate or to redeploy on
    another machine.
  - STILL blanks the 3 machine-path keys (output_folder,
    automation_root_folder, selfie_output_folder) so the receiver
    doesn't inherit dev-machine paths.

Output: dist/SelfieGenUltimate-<version>-personal.zip plus the
        SelfieGenUltimate-personal.zip latest alias.

Not committed to main — ad-hoc personal builder. Run with:
    python distribution\\build_release_personal.py
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Dict, Optional

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
DIST_DIR = ROOT / "distribution"
if str(DIST_DIR) not in sys.path:
    sys.path.insert(0, str(DIST_DIR))

import release_prep  # type: ignore  # noqa: E402

# Derive the secret-key blanklist from the single source of truth
# (api_keys.API_KEY_SPECS) rather than hardcoding it. Code-review MEDIUM
# (PR #54): a literal tuple is a secret-leak surface — if a 5th API key
# is ever added to API_KEY_SPECS, the standard release_prep path picks it
# up automatically (it iterates the spec), but a hardcoded list here would
# silently ship the new key UNblanked into a zip the docstring calls "safe
# to share." The hardcoded tuple is kept ONLY as the minimal-checkout
# fallback (api_keys.py absent), where the share-safety contract still
# holds because these are the keys that existed when this file was written.
try:
    from api_keys import API_KEY_SPECS as _API_KEY_SPECS  # type: ignore
    _API_KEY_FIELDS = tuple(spec.config_key for spec in _API_KEY_SPECS)
except ImportError:
    _API_KEY_FIELDS = (
        "falai_api_key",
        "freeimage_api_key",
        "bfl_api_key",
        "openrouter_api_key",
    )

# Test-fixture exclusions added 2026-05-23. release_prep.py keeps these in the
# bundle by default (and they're useful for the bench harness), but for a
# shareable personal zip they bloat it from 13MB → 191MB without contributing
# to runtime. Reqs install on first launch via the launcher.
_FIXTURE_DIRS = (
    "test-material",        # ~35MB of demo .mp4 inputs
    "analysis_frames",      # ~3.5MB of labelled pass/fail jpgs (research corpus)
    "oldcam_reference_bundle",  # ~5MB historical reference; not loaded at runtime
)
# Inside oldcam-testing/ we keep the .py / .bat / .command frozen bench
# scripts (memory: project_oldcam_testing_frozen_bench) but drop the
# .mp4 corpus + the rppg_harness_out/ directory.
_FIXTURE_SUBDIRS_OF_OLDCAM_TESTING = ("rppg_harness_out",)
_FIXTURE_FILE_EXTS = {".mp4", ".mov", ".avi", ".webm", ".mkv"}
# Exclude this top-level archive too (legacy historical bundle).
_FIXTURE_FILES = {"oldcam_reference_bundle.zip"}

_orig_should_skip = release_prep._should_skip


def _slim_should_skip(path: Path) -> bool:
    """Wrap release_prep._should_skip to also drop test fixtures.

    Round-2 review fix: the prior body indexed ``path.parts[0]``
    directly. The CURRENT caller (release_prep.bundle_release) always
    passes a repo-relative path, so this worked — but a future caller
    handing in an absolute path would silently lose every fixture
    exclusion (parts[0] would be ``"F:\\"`` or ``"/"`` instead of
    ``"oldcam-testing"``) and the slim builder would ship the
    fixtures it's supposed to strip.

    Defend by normalising to repo-relative when the path is
    absolute. If the path is absolute AND outside the repo
    (shouldn't happen), fall through to ``_orig_should_skip``'s
    judgement and don't try to apply slim rules.
    """
    if _orig_should_skip(path):
        return True
    rel = path
    if path.is_absolute():
        try:
            rel = path.relative_to(ROOT)
        except ValueError:
            # Absolute path outside the repo — slim rules don't
            # apply; trust the original predicate's verdict (False
            # here, since we passed its `if` above).
            return False
    parts = rel.parts
    if parts and parts[0] in _FIXTURE_DIRS:
        return True
    if len(parts) >= 2 and parts[0] == "oldcam-testing":
        if parts[1] in _FIXTURE_SUBDIRS_OF_OLDCAM_TESTING:
            return True
    if rel.suffix.lower() in _FIXTURE_FILE_EXTS:
        return True
    if rel.name in _FIXTURE_FILES:
        return True
    return False


def _personal_build_config(
    template_path: Path,
    live_config_path: Optional[Path] = None,
) -> Dict[str, object]:
    """Same input contract as release_prep.build_sanitized_config but
    DROPS the forced-override block — preserves the user's live values.
    """
    template: Dict[str, object] = {}
    if template_path.exists():
        # Round-3 subagent MEDIUM (PR #54): guard the template read
        # symmetrically with the live-config read below. The personal
        # builder is an ad-hoc by-hand script, so a one-time
        # corruption of default_config_template.json (mid-edit by the
        # user, or a partial git checkout) shouldn't crash the build —
        # fall back to an empty template (the live config still
        # provides the user's actual values).
        try:
            loaded = json.loads(template_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError, ValueError):
            loaded = None
        if isinstance(loaded, dict):
            template.update(loaded)
    config: Dict[str, object] = {}
    if live_config_path is not None and live_config_path.exists():
        try:
            loaded_live = json.loads(
                live_config_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError, ValueError):
            loaded_live = None
        if isinstance(loaded_live, dict):
            config.update(loaded_live)
    for key, value in template.items():
        config.setdefault(key, value)
    for k in _API_KEY_FIELDS:
        if k in config:
            config[k] = ""
    for k in release_prep._DIST_BLANKED_PATH_KEYS:
        if k in config:
            config[k] = ""
    # Round-2 review fix: replace bare ``except Exception: pass``.
    # Narrow to ImportError (api_keys missing in a partial checkout —
    # legitimately swallowable) and re-raise anything else so a real
    # bug in ensure_key_fields doesn't silently corrupt the personal
    # config payload.
    try:
        from api_keys import ensure_key_fields  # type: ignore
    except ImportError:
        # api_keys.py absent — minimal-checkout build path, skip key
        # normalisation entirely. Config still has the 4 fields blanked
        # by the loop above so the share-safety contract holds.
        return config
    ensure_key_fields(config)
    return config


def main() -> int:
    # gemini MEDIUM + codex P2 (PR #54): mutating these module globals is
    # non-idempotent (".replace(...)" / "+ '-personal'" stack up on repeat
    # calls) AND the function monkeypatches (build_sanitized_config /
    # _should_skip) leak into any later in-process call to the normal
    # release_prep.bundle_release — making a subsequent PUBLIC release use the
    # personal config builder + slim skip rules. Save ALL five originals and
    # restore them in a finally block; guard each rename with an endswith
    # check so repeated calls are safe.
    orig_build_config = release_prep.build_sanitized_config
    orig_should_skip = release_prep._should_skip
    orig_versioned = release_prep.VERSIONED_ZIP_NAME
    orig_alias = release_prep.LATEST_ALIAS_ZIP_NAME
    orig_basename = release_prep.RELEASE_BASENAME
    try:
        release_prep.build_sanitized_config = _personal_build_config  # type: ignore[assignment]
        release_prep._should_skip = _slim_should_skip  # type: ignore[assignment]
        if not orig_versioned.endswith("-personal.zip"):
            release_prep.VERSIONED_ZIP_NAME = orig_versioned.replace(
                ".zip", "-personal.zip")
        if not orig_alias.endswith("-personal.zip"):
            release_prep.LATEST_ALIAS_ZIP_NAME = orig_alias.replace(
                ".zip", "-personal.zip")
        if not orig_basename.endswith("-personal"):
            release_prep.RELEASE_BASENAME = orig_basename + "-personal"
        out = list(release_prep.bundle_release(ROOT, ROOT / "dist"))
    finally:
        release_prep.build_sanitized_config = orig_build_config
        release_prep._should_skip = orig_should_skip
        release_prep.VERSIONED_ZIP_NAME = orig_versioned
        release_prep.LATEST_ALIAS_ZIP_NAME = orig_alias
        release_prep.RELEASE_BASENAME = orig_basename
    print()
    for p in out:
        size_mb = p.stat().st_size / 1024 / 1024
        print(f"  Built: {p}  ({size_mb:.1f} MB)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
