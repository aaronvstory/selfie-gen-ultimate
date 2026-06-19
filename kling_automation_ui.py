import argparse
import contextlib
import faulthandler
import io
import os
import re
import signal
import sys
import json
import time
import threading
import traceback
from pathlib import Path
from typing import Dict, Any, Optional, List, Tuple
import logging
from rich import box as _rich_box
from rich.console import Console, Group
from rich.markup import escape as _rich_markup_escape
from rich.table import Table
from rich.live import Live
from rich.panel import Panel
from rich.progress import BarColumn, Progress, SpinnerColumn, TextColumn

from api_keys import API_KEY_SPECS, ApiKeySpec, apply_env_key_fallback, ensure_key_fields, env_key_optout_list, key_is_set, key_status, non_required_missing_specs, resolve_api_key, status_lines
from startup_key_onboarding import missing_startup_specs, startup_prompt_specs, startup_status_lines

try:
    from kling_gui.ml_backend_env import ensure_ml_backend_env
except ModuleNotFoundError:
    def ensure_ml_backend_env() -> None:
        os.environ["TF_USE_LEGACY_KERAS"] = "1"
        os.environ["KERAS_BACKEND"] = "tensorflow"

# Shared Rich console for every CLI render in this module. Reusing one
# instance avoids the cost of re-creating Console() on every banner / table
# / view-all-settings call in the questionary editor, and keeps styling +
# output config (file, color depth, force_terminal) centralized in one spot.
_RICH_CONSOLE = Console()


class _QuestionarySectionAbort(Exception):
    """Raised inside a section editor when the user hits Ctrl-C / ESC.

    questionary.ask() returns None on cancellation. The _qs_* helpers convert
    that None into this exception so the calling section handler unwinds back
    to the section picker (rather than silently continuing to the next field
    and forcing the user to abort every prompt in the section).
    """


def _qs_or_abort(value):
    """Convert a None questionary result (Esc/Ctrl-C) into a section abort.

    Wrap any ad-hoc ``questionary.select()/.text().ask()`` result through this so
    a cancellation unwinds back to the section picker instead of silently
    falling through to the next field. Returns the value unchanged otherwise.
    """
    if value is None:
        raise _QuestionarySectionAbort()
    return value


try:
    # questionary is declared required=True in dependency_checker.py and is
    # installed via requirements.txt by every launcher (setup_macos.sh,
    # setup_windows.sh). The ImportError handler below is **defense-in-depth**:
    # if a user has a corrupted venv or runs the module before setup completed,
    # we degrade to the legacy input() walker instead of crashing. This is also
    # what keeps non-TTY callers (pytest, CI, piped stdin) working — the
    # dispatch checks both _QUESTIONARY_AVAILABLE and sys.stdin.isatty().
    import questionary
    from questionary import Style as _QStyle
    _QUESTIONARY_AVAILABLE = True
    # Branded style — cyan accents, dim grey instructions, green selections.
    # Applied to every questionary prompt in the sectioned settings editor.
    KLING_QUESTIONARY_STYLE = _QStyle([
        ("qmark", "fg:#00d7ff bold"),         # ? prefix on each prompt
        ("question", "fg:#ffffff bold"),       # question text
        ("answer", "fg:#5fffaf bold"),         # final answer rendering
        ("pointer", "fg:#00d7ff bold"),        # ❯ on selected choice
        ("highlighted", "fg:#00d7ff bold"),    # currently hovered choice
        ("selected", "fg:#5fffaf"),            # confirmed choice text
        ("separator", "fg:#5f5f5f"),           # divider rows
        ("instruction", "fg:#6c7086 italic"),  # hint text after the question
        ("text", "fg:#bdbdbd"),                # default body
        ("disabled", "fg:#5f5f5f italic"),
    ])
except ImportError:
    questionary = None  # type: ignore[assignment]
    KLING_QUESTIONARY_STYLE = None  # type: ignore[assignment]
    _QUESTIONARY_AVAILABLE = False

ensure_ml_backend_env()

# Import path utilities for frozen exe compatibility
from path_utils import (
    get_config_path,
    get_crash_log_path,
    get_app_dir,
    VALID_EXTENSIONS,
)

# Import the fal.ai KlingBatchGenerator
from kling_generator_falai import FalAIKlingGenerator
from automation.config import (
    merge_automation_defaults,
    from_app_config,
    get_outpaint_fal_timeout_seconds,
    resolve_cli_kling_prompt_slot,
    resolve_cli_video_duration,
    resolve_cli_video_model,
)
from automation.discovery import CaseRecord, discover_case_folders, detect_existing_outputs, find_front_in_dir
from automation.logger import resolve_automation_log_path
from automation.manifest import AutomationManifest, STEP_NAMES
from automation.pipeline import AutoPipelineRunner
from automation.oldcam import (
    _version_key as _oldcam_sort_key,
    discover_oldcam_versions,
    ensure_oldcam_dependencies,
    normalize_oldcam_versions,
)
from automation.video_crush import (
    CRUSH_RESOLUTIONS,
    normalize_crush_resolutions,
)
from automation.video_aa import (
    AA_PIPELINES,
    normalize_aa_attacks,
)
from selfie_generator import SelfieGenerator
from tk_dialogs import (
    select_directory,
    select_directory_cli_safe,
    select_open_file,
    select_open_file_cli_safe,
)
from app_version import RELEASE_VERSION
# The repo's canonical config-bool coercion (bool("false") is True; this
# parses string forms). face_similarity is stdlib-light at module level —
# the heavy TF/DeepFace imports are lazy inside its engine getter.
from face_similarity import _parse_bool as _parse_bool_cfg

# Distinguishes "no argument supplied" from an explicit None/empty value in
# helpers whose argument legitimately accepts falsy values.
_SENTINEL_UNSET = object()


def _aa_strength_valid(value: str) -> bool:
    """True when *value* parses to a float in the AA strength range 0.1–1.0."""
    try:
        f = float(value)
    except (TypeError, ValueError):
        return False
    return 0.1 <= f <= 1.0

# v2 (2026-05-19): added automation_rppg_* recommended defaults (all OFF —
# rPPG is the untested forward direction, opt-in only).
# v3 (2026-05-19): added automation_rppg_metrics_in_filename (default
# False -> clean *-rppg name + .metrics.json sidecar).
# v4 (2026-05-19): minimal-motion default prompt + recommended negative
# prompt; cfg_scale_value (0.7) + lock_end_frame (true) defaults.
# v5 (2026-05-20, PR #43): rPPG iterative mode mandatory by default
# (friend confirmed iterative is required for production — single-shot
# rarely lands at the optimal strength). Also adds the 3 companion
# flags --iterate-from-baseline / --skip-diagnosis / --skip-kinematic-
# gate as separate config keys so users can selectively override.
# CodeRabbit cycle-3 flagged that v4 users pulling the PR-43 update
# would silently stay on "inject" mode unless the apply-defaults
# helper was bumped here too.
# v6 (2026-05-27, PR #54): automation_rppg_landmark_stride default
# reverted 3 -> 1 (quality-first; the v2.5 speedup pass was dialled
# back because Kling output occasionally moves faster than the
# prompt asks for and silently degrading those clips is worse than
# the slowdown). Without this bump, v5 users would silently keep
# stride=3 in their saved config and not get prompted to refresh.
# v7 (2026-06-11, CLI UX overhaul): user-mandated "best results" baseline —
# rPPG ON (the quick-start combo is rPPG + oldcam v13; a real batch run
# burned by silently running 10 oldcam versions with NO rPPG), oldcam
# ["v13"] only (canonical multi-select list form), loop OFF, provider fal
# for BOTH expand steps ("fal.ai for everything"), composites unchanged
# (front preserve_seamless / selfie-expand none). The GUI keeps its own
# opt-in rPPG default — this preset is CLI-automation only.
RECOMMENDED_DEFAULTS_VERSION = 7
# rPPG default in the v7 recommended preset (user decision 2026-06-11:
# ON). Single flip point should that decision change.
RECOMMENDED_RPPG_ENABLED_V7 = True
DEFAULT_KLING_PROMPT_SLOT = 4
DEFAULT_AUTOMATION_SELFIE_PROMPT_SLOT = 3

# Block-letter CLI banner ("the title doesn't stand out at all" — user,
# 2026-06-11). Hand-rolled constant, no figlet dependency. Every line must
# stay ≤79 cols (tests/test_cli_gui_settings_split.py locks this); terminals
# narrower than _BANNER_MIN_WIDTH (and non-TTY output) fall back to the
# bordered Rich panel header instead.
_BANNER_ASCII = """\
███████ ███████ ██      ███████ ██ ███████      ██████  ███████ ███    ██
██      ██      ██      ██      ██ ██          ██       ██      ████   ██
███████ █████   ██      █████   ██ █████       ██   ███ █████   ██ ██  ██
     ██ ██      ██      ██      ██ ██          ██    ██ ██      ██  ██ ██
███████ ███████ ███████ ██      ██ ███████      ██████  ███████ ██   ████"""
_BANNER_MIN_WIDTH = 76

# Single-source option lists shared by the questionary AND legacy settings
# editors so the two paths can never drift (review theme D). Edit here only.
_OLDCAM_VERSION_OPTIONS = ["v7", "v8", "v9", "v10", "v11", "v12", "v13", "v14", "v15", "v24", "all"]
_EXPAND_PROVIDER_OPTIONS = ["auto", "bfl", "fal"]
_EXPAND_MODE_OPTIONS = ["document_3x4", "percent"]
_SELFIE_EXPAND_MODE_OPTIONS = ["percent", "centered_3x4"]
_COMPOSITE_MODE_OPTIONS = ["preserve_seamless", "feathered", "hard", "black_fill", "none"]
_REPROCESS_MODE_OPTIONS = ["skip", "overwrite", "increment"]
# Explicit "turn this step OFF" row shared by the crush / AA / oldcam checkbox
# pickers (2026-06-19). Ticking it clears the whole selection — a discoverable
# alternative to the previously-invisible "deselect everything" gesture. The
# value is a sentinel that can never collide with a real tier/attack/version.
_OFF_CHOICE_VALUE = "__off__"
_OFF_CHOICE_LABEL = "✖ OFF — skip this step"
# Post-processing fan-out modes (mirrors automation.postproc_plan.VALID_MODES;
# duplicated as (value, label) here so the quick-edit picker stays a single
# source without importing the planner at module load).
_FANOUT_MODE_OPTIONS = [
    ("separate_and_combined", "Separate + combined (powerset — every subset)"),
    ("combined_only", "Combined only (one cumulative chain)"),
]
_VIDEO_RESOLUTION_OPTIONS = ["480p", "720p"]
_VIDEO_ASPECT_RATIO_OPTIONS = ["21:9", "16:9", "4:3", "1:1", "3:4", "9:16"]
# Common video durations (seconds) most fal video models accept. Used to warn
# on uncommon values without rejecting them (D6).
_COMMON_VIDEO_DURATIONS = [2, 3, 4, 5, 6, 7, 8, 10, 15]
_PROMPT_SLOT_COUNT = 10  # selfie + kling video prompt slots are both 1.._PROMPT_SLOT_COUNT
# Matches ANSI SGR color/style escape sequences (ESC [ ... m). Used to keep
# non-TTY / cron logs clean of color codes forwarded from subprocess output.
_ANSI_ESCAPE_RE = re.compile(r"\x1b\[[0-9;]*m")
RECOMMENDED_KLING_PROMPT_SLOT_1 = (
    "Image-to-video: the subject performs a very subtle, slow head movement while "
    "the body and background remain completely motionless. The head turns slightly "
    "to one side, then slowly to the other side, with the smallest believable range "
    "of motion — barely past front-facing, never approaching profile. Eyes stay "
    "locked on the camera lens the entire time. Facial expression stays neutral "
    "and unchanged. Shoulders, torso, neck base, and background do not move at all. "
    "Camera is locked. Lighting matches the source image. Pacing is slow, "
    "continuous, and natural."
)

# Recommended negative prompt — only sent for models that accept
# negative_prompt (Kling 2.5 / v3); the dispatcher drops it for
# o3 / seedance. Pairs with the minimal-motion positive above + the
# end-frame lock to mechanically suppress overshoot / drift.
RECOMMENDED_KLING_NEGATIVE_SLOT_1 = (
    "profile view, full head turn, head turned away, looking away from camera, "
    "broken eye contact, eyes closed, shoulder movement, torso rotation, body "
    "twist, leaning, swaying, head tilt, smiling, changing expression, talking, "
    "blinking unnaturally, camera movement, camera pan, camera zoom, lighting "
    "change, flicker, exposure shift, color shift, background motion, fast motion, "
    "jerky motion, robotic motion, morphing face, distortion, blur, low quality"
)

_CRASH_CAPTURE_FILE: Optional[io.TextIOWrapper] = None


def _derive_model_display_name(endpoint: str) -> str:
    """Best-effort friendly name for a fal video model endpoint.

    Used by the headless ``--model`` override when no explicit ``--model-name``
    is supplied. Prefers the canonical name from models.json when the endpoint
    is a known model; otherwise prettifies the endpoint path tail (e.g.
    ``fal-ai/kling-video/v2.5-turbo/standard/image-to-video`` ->
    ``Kling Video V2.5 Turbo Standard``).
    """
    endpoint = (endpoint or "").strip()
    if not endpoint:
        return ""
    try:
        from model_metadata import get_model_by_endpoint, get_model_display_name

        known = get_model_by_endpoint(endpoint)
        if known:
            name = get_model_display_name(known)
            if name:
                return name
    except (ImportError, AttributeError, KeyError, TypeError):
        # model_metadata missing / models.json shape unexpected -> fall through
        # to the path-prettify fallback. Narrow catch so genuine bugs in the
        # lookup surface instead of being masked (Sourcery review, PR #94).
        pass
    # Fallback: prettify the path, dropping the trailing verb segment
    # (image-to-video / text-to-video) and the vendor prefix.
    parts = [p for p in endpoint.split("/") if p]
    if parts and parts[0] in {"fal-ai", "fal"}:
        parts = parts[1:]
    if parts and parts[-1] in {"image-to-video", "text-to-video", "edit"}:
        parts = parts[:-1]
    pretty = " ".join(seg.replace("-", " ").title() for seg in parts if seg)
    return pretty or endpoint


def _enable_cli_crash_capture() -> Optional[str]:
    """Enable faulthandler logging for fatal native crashes."""
    global _CRASH_CAPTURE_FILE
    crash_path = Path(get_app_dir()) / "kling_automation_crash.log"
    try:
        if _CRASH_CAPTURE_FILE is not None and not _CRASH_CAPTURE_FILE.closed:
            _CRASH_CAPTURE_FILE.close()
        _CRASH_CAPTURE_FILE = open(crash_path, "a", encoding="utf-8")
        _CRASH_CAPTURE_FILE.write(f"\n\n=== Crash capture initialized at {time.strftime('%Y-%m-%d %H:%M:%S')} ===\n")
        _CRASH_CAPTURE_FILE.flush()
        faulthandler.enable(file=_CRASH_CAPTURE_FILE, all_threads=True)
        for sig_name in ("SIGSEGV", "SIGABRT", "SIGBUS", "SIGILL"):
            sig = getattr(signal, sig_name, None)
            if sig is not None:
                try:
                    faulthandler.register(sig, file=_CRASH_CAPTURE_FILE, all_threads=True)
                except Exception:
                    pass
        return str(crash_path)
    except Exception:
        if _CRASH_CAPTURE_FILE is not None:
            try:
                _CRASH_CAPTURE_FILE.close()
            except Exception:
                pass
            _CRASH_CAPTURE_FILE = None
        return None


class KlingAutomationUI:
    legacy_pauses: bool = False

    def __init__(self, legacy_pauses: bool = False):
        self.config_file = get_config_path("kling_config.json")
        self.config = merge_automation_defaults(self.load_config())
        if ensure_key_fields(self.config):
            self.save_config()
        # Silently prefill any still-empty API key from its env var (FAL_KEY,
        # BFL_API_KEY, OPENROUTER_API_KEY, FREEIMAGE_API_KEY). In-memory only —
        # NOT saved, so the env stays the source of truth. User-saved keys win.
        self._env_prefilled_keys = apply_env_key_fallback(self.config)
        self.automation_root_folder = self.config.get("automation_root_folder", "")
        self.verbose_logging = self.config.get("verbose_logging", False)
        self.legacy_pauses = legacy_pauses
        self._last_scan_records: List[Any] = []
        self._startup_key_onboarding_done = False
        self.setup_logging()

    @staticmethod
    def _wait_for_enter(message: str) -> None:
        """input() pause that treats a closed/piped stdin as 'Enter pressed'.

        Swallows the full set of degenerate-stdin failures input() can raise:
        EOFError (stdin at EOF — piped/exhausted/Ctrl-D), RuntimeError
        ("lost sys.stdin" when sys.stdin is None — GUI wrappers, daemons,
        Windows services), ValueError ("I/O operation on closed file" when the
        stdin object is closed), and OSError (bad/closed underlying fd — EBADF,
        or BrokenPipeError on a severed pipe). KeyboardInterrupt (Ctrl-C) is
        deliberately NOT caught — main() handles it as a clean exit.
        """
        try:
            input(message)
        except (EOFError, RuntimeError, ValueError, OSError):
            pass

    def pause_continue(self, message: str = "Press Enter to continue..."):
        """Pause only when legacy pause mode is enabled."""
        if self.legacy_pauses:
            self._wait_for_enter(message)

    def pause_review(self, message: str = "Press Enter to continue..."):
        """Pause for explicit review screens or actionable error surfaces."""
        self._wait_for_enter(message)

    @staticmethod
    def _safe_input(prompt: str = "", default: str = "") -> str:
        """input() that returns ``default`` on EOF/closed stdin instead of
        raising. Used by legacy sub-menus so a piped/closed stdin cancels the
        action cleanly rather than crashing the menu loop. Covers the full set
        of degenerate-stdin failures: EOFError (EOF/piped), RuntimeError
        (sys.stdin is None), ValueError (sys.stdin closed), OSError (bad fd)."""
        try:
            return input(prompt)
        except (EOFError, RuntimeError, ValueError, OSError):
            return default

    def load_config(self) -> Dict[str, Any]:
        """Load configuration from file or create default"""
        # Default prompt slot 1 - basic head turn
        prompt_slot_1 = (
            "Turn head to the right slowly then all the way to the left slowly then to the right slowly, and to the left slowly. "
            "Make sure the body is kept still while doing this - ONLY turn THE HEAD NOT THE BODY. The subject should perform smooth, "
            "natural head movements with no body movement whatsoever. Keep shoulders, neck, and torso completely stationary. "
            "Head movements should be slow, deliberate, and realistic. Eyes can follow natural movement patterns. "
            "Maintain neutral facial expression throughout. Camera remains fixed and stationary. "
            "Generate in maximum resolution and professional quality with no blur, pixelation, or quality degradation."
        )

        default_config = {
            "output_folder": "",  # Empty by default - user picks their own
            "use_source_folder": True,  # Default: save videos alongside source images
            "falai_api_key": "",  # Will prompt user on first run
            "bfl_api_key": "",
            "openrouter_api_key": "",
            "freeimage_api_key": "",
            "outpaint_fal_timeout_seconds": 150,
            "outpaint_composite_mode": "preserve_seamless",
            "verbose_logging": True,
            "duplicate_detection": True,
            "delay_between_generations": 1,
            # Prompt slot system - recommended defaults use slot 4 for Kling video
            "current_prompt_slot": DEFAULT_KLING_PROMPT_SLOT,
            "saved_prompts": {
                "1": RECOMMENDED_KLING_PROMPT_SLOT_1,
                "2": prompt_slot_1,
                "3": None,
                "4": RECOMMENDED_KLING_PROMPT_SLOT_1,
                "5": None,
                "6": None,
                "7": None,
                "8": None,
                "9": None,
                "10": None,
            },
            "negative_prompts": {
                "1": RECOMMENDED_KLING_NEGATIVE_SLOT_1,
                "2": None,
                "3": None,
                "4": RECOMMENDED_KLING_NEGATIVE_SLOT_1,
                "5": None,
                "6": None,
                "7": None,
                "8": None,
                "9": None,
                "10": None,
            },
            # Model configuration - Kling 2.5 Turbo Standard
            "current_model": "fal-ai/kling-video/v2.5-turbo/standard/image-to-video",
            "model_display_name": "Kling 2.5 Turbo Standard",
            # Generation parameters
            "video_duration": 10,
            # 3:4 is the canonical portrait ratio for this pipeline (the selfie
            # generates at 864x1152 = exact 3:4 and the chain preserves it). Some
            # Kling endpoints (e.g. 2.5 turbo) ignore aspect_ratio and follow the
            # input image's ratio anyway, but for endpoints that DO honor it, the
            # default must match the 3:4 stills so the video isn't reframed.
            "aspect_ratio": "3:4",
            "resolution": "720p",
            "seed": -1,  # -1 = random
            "camera_fixed": False,
            "generate_audio": False,
            # Motion control (mirrors default_config_template.json so the
            # CLI and GUI new-install defaults agree). cfg 0.7 = stricter
            # prompt adherence than fal's 0.5; end-frame lock on so the
            # clip mechanically returns to the opening pose. Both are
            # gated per-model by the dispatcher's capability check.
            "cfg_scale_value": 0.7,
            "lock_end_frame": True,
            # Fresh install (no config file) starts at version 0 so the
            # one-time SILENT migration applies the FULL recommended
            # baseline (rPPG ON + oldcam v13 + cli_* Standard/slot 4) on
            # the first interactive launch. The v7 baseline deliberately
            # DIVERGES from the global defaults (automation_rppg_enabled
            # is False there) — stamping the current version here marked
            # fresh installs "already migrated" and the baseline never
            # landed (Codex bot P2, PR #96 round 11).
            "automation_recommended_defaults_version": 0,
        }

        try:
            if Path(self.config_file).exists():
                with open(self.config_file, "r") as f:
                    loaded_config = json.load(f)
                    # Merge with defaults, ensuring new fields exist
                    merged = {**default_config, **loaded_config}
                    # A config from before the recommended-defaults
                    # versioning must NOT inherit the fresh-install stamp
                    # from default_config — that would mark it "already
                    # current" and skip the one-time silent migration
                    # (Codex P2, PR #96 round 2). Absent key = version 0.
                    if "automation_recommended_defaults_version" not in loaded_config:
                        merged["automation_recommended_defaults_version"] = 0
                    # Ensure saved_prompts has all slots (1-10)
                    if "saved_prompts" not in merged:
                        merged["saved_prompts"] = default_config["saved_prompts"]
                    else:
                        for slot in [str(i) for i in range(1, _PROMPT_SLOT_COUNT + 1)]:
                            if slot not in merged["saved_prompts"] or merged["saved_prompts"][slot] is None:
                                merged["saved_prompts"][slot] = ""

                    # Ensure negative_prompts has all slots (1-10)
                    if "negative_prompts" not in merged:
                        merged["negative_prompts"] = default_config["negative_prompts"]
                    else:
                        for slot in [str(i) for i in range(1, _PROMPT_SLOT_COUNT + 1)]:
                            if slot not in merged["negative_prompts"] or merged["negative_prompts"][slot] is None:
                                merged["negative_prompts"][slot] = ""

                    return merged
        except Exception:
            pass
        return default_config

    def get_current_prompt(self) -> str:
        """Get the current prompt from the active slot"""
        slot = str(self.config.get("current_prompt_slot", DEFAULT_KLING_PROMPT_SLOT))
        saved = self.config.get("saved_prompts", {})
        prompt = saved.get(slot)
        if prompt:
            return prompt
        # Fallback to default
        return self.get_default_prompt()

    def get_current_negative_prompt(self) -> Optional[str]:
        """Get the current negative prompt from the active slot"""
        slot = str(self.config.get("current_prompt_slot", DEFAULT_KLING_PROMPT_SLOT))
        saved = self.config.get("negative_prompts", {})
        return saved.get(slot)

    def _resolve_cfg_and_lock(self) -> tuple:
        """Resolve (cfg_scale, lock_end_frame) for a video dispatch.

        Single source for the interactive CLI batch path, mirroring
        automation/pipeline.py exactly: cfg_scale clamped to [0.0, 1.0]
        (a stale/hand-edited out-of-range value must not reach the API),
        and lock_end_frame via the canonical _parse_bool with an
        unparseable value coercing to True (its default is True — GUI,
        pipeline and CLI must agree on malformed input). The generator
        still gates BOTH per-model via get_model_capabilities, so passing
        them on unsupported models is a safe no-op (code-reviewer, PR #41
        — process_all_images_concurrent previously dropped both).
        """
        try:
            _cfg = float(self.config.get("cfg_scale_value", 0.7))
        except (TypeError, ValueError):
            _cfg = 0.7
        from face_similarity import _parse_bool as _pb
        _lock = _pb(self.config.get("lock_end_frame", True))
        if _lock is None:
            _lock = True
        return max(0.0, min(1.0, _cfg)), bool(_lock)

    def get_default_prompt(self) -> str:
        """The default video prompt — the minimal-motion prompt (PR #2).

        Single source: RECOMMENDED_KLING_PROMPT_SLOT_1, so a "reset to
        default" / no-saved-prompt fallback restores the SAME prompt the
        recommended-defaults flow and the GUI/CLI templates seed
        (CodeRabbit, PR #41 — previously this returned the superseded
        turn-head text). The legacy turn-head prompt remains available
        as the slot-2 backup (default_config), not as "the default".
        """
        return RECOMMENDED_KLING_PROMPT_SLOT_1

    def fetch_model_pricing(self, model_endpoint: str) -> Optional[float]:
        """Fetch pricing for a model from fal.ai API (memoized per endpoint).

        The model picker renders pricing for every preset on each redraw, so
        without a cache a single menu render fires N network calls (each up to a
        10s timeout). Memoizing per endpoint — including the None/failure result
        — keeps the menu snappy on repeat renders within a session.
        """
        if not hasattr(self, "_pricing_cache"):
            self._pricing_cache = {}
        if model_endpoint in self._pricing_cache:
            return self._pricing_cache[model_endpoint]
        price: Optional[float] = None
        try:
            import requests

            headers = {"Authorization": f"Key {resolve_api_key(self.config, 'falai_api_key')}"}
            response = requests.get(
                f"https://api.fal.ai/v1/models/pricing?endpoint_id={model_endpoint}",
                headers=headers,
                timeout=10,
            )
            if response.status_code == 200:
                data = response.json()
                prices = data.get("prices", [])
                if prices:
                    # float() guard: a string/odd-typed unit_price from the
                    # API must degrade to "no price", not crash the f"{:.2f}"
                    # at BOTH render sites (video $/sec + selfie $/img) —
                    # a TypeError here would break every header render
                    # (code-reviewer IMPORTANT, PR #96 round 5).
                    try:
                        price = float(prices[0].get("unit_price"))
                    except (TypeError, ValueError):
                        price = None
        except Exception:
            price = None
        self._pricing_cache[model_endpoint] = price
        return price

    def fetch_available_models(self) -> list:
        """Fetch available video models from fal.ai Platform API with pagination"""
        try:
            import requests

            headers = {"Authorization": f"Key {resolve_api_key(self.config, 'falai_api_key')}"}
            all_models = []
            cursor = None

            # Paginate through all results
            while True:
                params = {"category": "image-to-video", "status": "active", "limit": 50}
                if cursor:
                    params["cursor"] = cursor

                response = requests.get(
                    "https://api.fal.ai/v1/models",
                    params=params,
                    headers=headers,
                    timeout=15,
                )

                if response.status_code != 200:
                    if self.verbose_logging:
                        print(
                            f"\033[91mAPI returned status {response.status_code}\033[0m"
                        )
                    break

                data = response.json()
                for m in data.get("models", []):
                    endpoint_id = m.get("endpoint_id", "")
                    metadata = m.get("metadata", {})
                    description = metadata.get("description", "")
                    # Keep up to 200 chars for wrapping (3 lines of ~65 chars)
                    if len(description) > 200:
                        description = description[:197] + "..."
                    all_models.append(
                        {
                            "name": metadata.get("display_name", endpoint_id),
                            "endpoint_id": endpoint_id,
                            "description": description,
                            "duration": metadata.get("duration_estimate", 10),
                        }
                    )

                # Check for more pages
                if data.get("has_more") and data.get("next_cursor"):
                    cursor = data["next_cursor"]
                else:
                    break

            # Batch fetch pricing for all models (up to 50 at a time)
            if all_models:
                endpoint_ids = [m["endpoint_id"] for m in all_models]
                prices = self.fetch_batch_pricing(endpoint_ids)
                for model in all_models:
                    model["price"] = prices.get(model["endpoint_id"])

            if all_models:
                return all_models

        except Exception as e:
            if self.verbose_logging:
                print(f"\033[91mError fetching models: {e}\033[0m")

        # Fallback to centralized model metadata
        from model_metadata import MODEL_METADATA

        # Convert to CLI format (endpoint_id instead of endpoint)
        return [
            {
                "name": m["name"],
                "endpoint_id": m["endpoint"],
                "duration_options": m["duration_options"],
                "duration_default": m["duration_default"],
                "description": m["description"],
            }
            for m in MODEL_METADATA
        ]

    def fetch_batch_pricing(self, endpoint_ids: list) -> dict:
        """Fetch pricing for multiple models at once (max 50)"""
        prices = {}
        try:
            import requests

            headers = {"Authorization": f"Key {resolve_api_key(self.config, 'falai_api_key')}"}

            # Process in batches of 50
            for i in range(0, len(endpoint_ids), 50):
                batch = endpoint_ids[i : i + 50]
                response = requests.get(
                    "https://api.fal.ai/v1/models/pricing",
                    params={"endpoint_id": batch},
                    headers=headers,
                    timeout=15,
                )
                if response.status_code == 200:
                    data = response.json()
                    for p in data.get("prices", []):
                        endpoint = p.get("endpoint_id", "")
                        unit_price = p.get("unit_price")
                        unit = p.get("unit", "")
                        prices[endpoint] = {"price": unit_price, "unit": unit}
        except Exception:
            pass
        return prices

    def _clear_env_prefill_marker(self, config_key: str):
        """Stop treating a key as env-sourced (the user explicitly set it), so
        save_config persists their value instead of stripping it."""
        env_filled = getattr(self, "_env_prefilled_keys", None)
        if env_filled and config_key in env_filled:
            env_filled.remove(config_key)

    def save_config(self):
        """Save current configuration to file.

        Env-sourced API keys are never written to disk (code-review CRITICAL):
        apply_env_key_fallback prefills them in MEMORY only, so we drop them from
        the serialized copy. A user who explicitly sets a key via the settings
        editor clears it from _env_prefilled_keys first, so their value persists.
        """
        try:
            data = self.config
            env_filled = getattr(self, "_env_prefilled_keys", None)
            if env_filled:
                data = dict(self.config)
                for k in env_filled:
                    data.pop(k, None)
            # ATOMIC write (gemini MEDIUM #73): shared config across concurrent
            # launches — write a per-process temp then os.replace so a concurrent
            # reader never sees a truncated file (atomic on Windows + POSIX).
            tmp_path = f"{self.config_file}.tmp.{os.getpid()}"
            with open(tmp_path, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2)
            os.replace(tmp_path, self.config_file)
        except Exception as e:
            if self.verbose_logging:
                print(f"Error saving config: {e}")
            try:
                if 'tmp_path' in locals() and os.path.exists(tmp_path):
                    os.remove(tmp_path)
            except Exception:
                pass

    def setup_logging(self):
        """Setup logging based on verbose setting"""
        if self.verbose_logging:
            logging.basicConfig(
                level=logging.INFO,
                format="%(asctime)s - %(levelname)s - %(message)s",
                handlers=[
                    logging.FileHandler("kling_automation.log"),
                    logging.StreamHandler(),
                ],
            )
        else:
            logging.basicConfig(
                level=logging.ERROR,
                format="%(asctime)s - %(levelname)s - %(message)s",
                handlers=[logging.FileHandler("kling_automation.log")],
            )
            logging.getLogger().setLevel(logging.CRITICAL)

    def _set_api_key(self, spec) -> None:
        """Persist a new value for one API key spec + re-opt-in to env fallback."""
        self._clear_env_prefill_marker(spec.config_key)
        optout = env_key_optout_list(self.config)
        if spec.config_key in optout:
            optout.remove(spec.config_key)
            self.config["_env_key_optout"] = optout
        self.save_config()

    def _clear_api_key(self, spec) -> None:
        """Clear one API key + persist a env-fallback opt-out so it stays clear."""
        self.config[spec.config_key] = ""
        self._clear_env_prefill_marker(spec.config_key)
        optout = env_key_optout_list(self.config)
        if spec.config_key not in optout:
            optout.append(spec.config_key)
            self.config["_env_key_optout"] = optout
        self.save_config()

    def configure_api_provider_settings(self):
        """Provider-aware API key/editor for automation and manual tools."""
        if not self._use_legacy_prompt_ui():
            self._configure_api_provider_settings_questionary()
            return
        self.clear_screen_simple()
        print("\n" + "=" * 72)
        print("  API SETUP / PROVIDER SETTINGS")
        print("=" * 72)
        print("\nProvider key status:")
        for line in status_lines(self.config):
            print(f"  - {line}")
        required_now = {spec.config_key for spec, _reason in self._startup_required_key_specs()}
        if required_now:
            print("\nCurrently required at startup (from active config):")
            for spec in API_KEY_SPECS:
                if spec.config_key in required_now:
                    print(f"  - {spec.label}")
        print("\nProvider quick setup:")
        for idx, spec in enumerate(API_KEY_SPECS, start=1):
            print(f"  {idx}) Set/update {spec.label} key")
        clear_base = len(API_KEY_SPECS)
        for idx, spec in enumerate(API_KEY_SPECS, start=1):
            print(f"  {clear_base + idx}) Clear {spec.label} key")
        print("\nCurrent key status:")
        for spec in API_KEY_SPECS:
            print(f"  - {spec.config_key}: {key_status(self.config, spec.config_key)}")
        print("  0) Back")
        print()
        choice = self._safe_input("Select option: ").strip()
        if choice == "0":
            self.pause_continue("\nPress Enter to continue...")
            return
        try:
            selected = int(choice)
        except ValueError:
            self.pause_continue("\nInvalid selection. Press Enter to continue...")
            return

        if 1 <= selected <= len(API_KEY_SPECS):
            spec = API_KEY_SPECS[selected - 1]
            print(f"\n{spec.label}: {spec.instruction}")
            print(f"Get key: {spec.url}")
            value = self._safe_input(f"Enter {spec.label} API key: ").strip()
            if value:
                self.config[spec.config_key] = value
                self._set_api_key(spec)
                print(f"Saved {spec.config_key}.")
        elif len(API_KEY_SPECS) < selected <= len(API_KEY_SPECS) * 2:
            spec = API_KEY_SPECS[selected - len(API_KEY_SPECS) - 1]
            self._clear_api_key(spec)
            print(f"Cleared {spec.config_key}.")
        self.pause_continue("\nPress Enter to continue...")

    def _configure_api_provider_settings_questionary(self):
        """Branded API-key manager (questionary): per-provider set / clear."""
        while True:
            self.display_header()
            status_table = self._styled_table("🔑  API keys / provider status")
            status_table.add_column("Provider", style="cyan", no_wrap=True)
            status_table.add_column("Status")
            for spec in API_KEY_SPECS:
                state = str(key_status(self.config, spec.config_key))
                state_style = "green" if "set" in state.lower() else "yellow"
                status_table.add_row(spec.label, f"[{state_style}]{state}[/{state_style}]")
            _RICH_CONSOLE.print(status_table)
            choices = []
            for spec in API_KEY_SPECS:
                choices.append((f"🔑  Set / update {spec.label} key", f"set:{spec.config_key}"))
            for spec in API_KEY_SPECS:
                choices.append((f"🧹  Clear {spec.label} key", f"clear:{spec.config_key}"))
            choices.append(("↩️   Back", "back"))
            pick = self._q_menu(
                "API keys",
                choices,
                show_header=False,
                show_title_rule=False,
            )
            if pick in (None, "back"):
                return
            action, _, ckey = pick.partition(":")
            spec = next((s for s in API_KEY_SPECS if s.config_key == ckey), None)
            if spec is None:
                continue
            if action == "set":
                print(f"\n{spec.label}: {spec.instruction}")
                print(f"Get key: {spec.url}")
                value = self._q_text(f"{spec.label} API key (Esc to cancel):")
                if value and value.strip():
                    self.config[spec.config_key] = value.strip()
                    self._set_api_key(spec)
                    self.print_green(f"✓ Saved {spec.label} key")
                    time.sleep(0.7)
            elif action == "clear":
                if self._confirm(f"Clear the {spec.label} key?", default=False):
                    self._clear_api_key(spec)
                    self.print_yellow(f"Cleared {spec.label} key")
                    time.sleep(0.7)

    def _run_startup_key_onboarding(self) -> None:
        if self._startup_key_onboarding_done:
            return
        self._startup_key_onboarding_done = True
        prompt_specs = startup_prompt_specs()
        # Guard isatty the same way _use_legacy_prompt_ui does: sys.stdin can be
        # None (Windows background service) or a custom stream lacking isatty()
        # (IDE/GUI wrappers, test runners). A bare sys.stdin.isatty() would
        # AttributeError there (C1). Treat "no interactive TTY" as non-interactive.
        _stdin = getattr(sys, "stdin", None)
        _is_tty = bool(_stdin) and hasattr(_stdin, "isatty") and _stdin.isatty()
        if not _is_tty:
            # Warn about the keys the ACTIVE automation config actually needs
            # (config-aware: fal for core gen + BFL only when a stage selects the
            # BFL provider), not a generic fal/BFL pair — so the non-interactive
            # warning stays accurate after BFL was dropped from the generic
            # first-launch prompt (it's still flagged here when truly required).
            required = [
                spec
                for spec, _reason in self._startup_required_key_specs()
                if not key_is_set(self.config, spec.config_key)
            ]
            if required:
                print("\nStartup API keys missing in non-interactive mode (continuing).")
                for spec in required:
                    print(f"  - {spec.label}: {spec.url}")
                print("Key-required features will show an error when used until keys are configured.")
            return

        print("\n" + "=" * 79)
        print("FIRST LAUNCH KEY CHECK")
        print("=" * 79)
        for line in startup_status_lines(self.config):
            print(f"  - {line}")
        print("\nQuick setup links:")
        for spec in prompt_specs:
            print(f"  - {spec.label}: {spec.url}")
        print("\nPress Enter to skip any key for now.")
        for spec in missing_startup_specs(self.config):
            print(f"\n{spec.label}: {spec.instruction}")
            value = self._safe_input("Enter key now (or q to skip): ").strip()
            if value.lower() == "q":
                continue
            if value:
                self.config[spec.config_key] = value
                # The user EXPLICITLY entered this key — it is no longer
                # env-sourced, so drop it from the env-prefill marker BEFORE
                # saving. Without this, save_config() strips the just-entered
                # key back out (it excludes _env_prefilled_keys), silently
                # discarding it so the user is nagged again next launch
                # (code-review CRITICAL, PR #73 — matches configure_api_provider_settings).
                self._clear_env_prefill_marker(spec.config_key)
                # A real value also opts the key BACK IN to the env fallback.
                optout = env_key_optout_list(self.config)
                if spec.config_key in optout:
                    optout.remove(spec.config_key)
                    self.config["_env_key_optout"] = optout
                self.save_config()

        missing_optional = list(non_required_missing_specs(self.config))
        if missing_optional:
            print("\nOptional keys are missing. Features may be limited until added:")
            for spec in missing_optional:
                print(f"  - {spec.label}: {spec.url}")
            print("You can add these later via menu option 7.")

    def _startup_required_key_specs(self) -> List[Tuple[ApiKeySpec, str]]:
        specs_by_key = {spec.config_key: spec for spec in API_KEY_SPECS}
        required = []
        fal_spec = specs_by_key.get("falai_api_key")
        if fal_spec:
            required.append((fal_spec, "core generation pipeline"))
        if self._is_bfl_required_on_startup():
            bfl_spec = specs_by_key.get("bfl_api_key")
            if bfl_spec:
                required.append((bfl_spec, "current automation/manual settings select BFL"))
        return required

    def _is_bfl_required_on_startup(self) -> bool:
        front_enabled = bool(self.config.get("automation_front_expand_enabled", True))
        if front_enabled and str(self.config.get("automation_front_expand_provider", "auto")).strip().lower() == "bfl":
            return True

        selfie_expand_enabled = bool(self.config.get("automation_selfie_expand_enabled", True))
        if selfie_expand_enabled and str(self.config.get("automation_selfie_expand_provider", "auto")).strip().lower() == "bfl":
            return True

        selfie_enabled = bool(self.config.get("automation_selfie_enabled", True))
        selfie_models = list(self.config.get("automation_selfie_models", []))
        if selfie_enabled and any(str(model).strip().lower().startswith("bfl/") for model in selfie_models):
            return True

        if str(self.config.get("outpaint_provider", "")).strip().lower() == "bfl":
            return True

        return False

    def clear_screen_simple(self):
        """Clear screen without dependencies"""
        os.system("cls" if os.name == "nt" else "clear")

    def clear_screen(self):
        """Clear terminal screen"""
        os.system("cls" if os.name == "nt" else "clear")

    def print_cyan(self, text):
        """Print text in cyan color"""
        print(f"\033[96m{text}\033[0m")

    def print_light_purple(self, text):
        """Print text in light purple color"""
        print(f"\033[94m{text}\033[0m")

    def print_magenta(self, text):
        """Print text in magenta color"""
        print(f"\033[95m{text}\033[0m")

    def print_green(self, text):
        """Print text in green color"""
        print(f"\033[92m{text}\033[0m", end="")

    def print_yellow(self, text):
        """Print text in yellow color"""
        print(f"\033[93m{text}\033[0m")

    def print_red(self, text):
        """Print text in red color"""
        print(f"\033[91m{text}\033[0m")

    def display_header(self):
        """Display the primary Selfie Gen Ultimate header."""
        self.clear_screen()

        # The header status line shows the AUTOMATION pipeline's effective
        # model ("Automation first") — per-surface cli_* keys, falling back
        # to the GUI keys for pre-split configs.
        _hdr_endpoint, _hdr_display = resolve_cli_video_model(self.config)
        model_name = _hdr_display or _hdr_endpoint or "Kling 2.5 Turbo Standard"
        duration = resolve_cli_video_duration(self.config)

        # Fetch pricing (cached after first call). Value-aware guard: model
        # switches reset _cached_price to None (not delattr), so a hasattr-only
        # check would keep the stale/None price forever — the header would show
        # the old model's price or "Check fal.ai" after every change (A2).
        if getattr(self, "_cached_price", None) is None:
            self._cached_price = self.fetch_model_pricing(_hdr_endpoint or "")
        price = self._cached_price
        price_str = f"${price:.2f}/sec" if price else "Check fal.ai"

        # Header v2 (2026-06-11 round 2): block-letter ASCII banner so the
        # title actually reads as a TITLE instead of blending into the
        # settings table. Version single-source: app_version.RELEASE_VERSION
        # (same constant as the GUI chip + release-zip name). Non-TTY output
        # and narrow terminals keep the bordered Rich panel (degrades cleanly,
        # and the banner would wrap into garbage under ~76 cols).
        from rich.align import Align
        from rich.console import Group as _RichGroup
        from rich.text import Text as _RichText

        # Live status: BOTH generation models (user 2026-06-11 round 3 —
        # "both are basically equally as important"). Re-built on every
        # render, so model/pricing changes show immediately.
        selfie_labels = self._selfie_model_label_map()
        raw_selfie_models = list(self.config.get("automation_selfie_models") or [])
        selfie_models = [selfie_labels.get(x, x) for x in raw_selfie_models]
        selfie_label = " + ".join(selfie_models) if selfie_models else "(none)"
        # Per-image price for the selfie model too — both generators cost
        # real money (user, round 5). fetch_model_pricing is endpoint-generic
        # and memoized; multi-model fan-out skips the price (ambiguous), and
        # non-fal endpoints simply return None.
        selfie_price = self.fetch_model_pricing(raw_selfie_models[0]) if len(raw_selfie_models) == 1 else None
        selfie_price_plain = f" · ${selfie_price:.2f}/img" if selfie_price else ""
        # markup-escape the model labels: custom endpoints / display names are
        # USER text — "[bracketed]" segments vanish and "[/x]" raises
        # MarkupError on EVERY screen repaint (entire-branch review CRITICAL).
        video_markup = (
            f"🎬 [magenta]{_rich_markup_escape(str(model_name))}[/magenta] · [green]{duration}s[/green] · [yellow]{price_str}[/yellow]"
        )
        selfie_markup = f"✨ [bold cyan]{_rich_markup_escape(str(selfie_label))}[/bold cyan]" + (
            f" · [yellow]${selfie_price:.2f}/img[/yellow]" if selfie_price else ""
        )
        plain_len = len(f"🎬 {model_name} · {duration}s · {price_str}   ·   ✨ {selfie_label}{selfie_price_plain}")
        if plain_len <= 76:
            status_lines = [_RichText.from_markup(f"{video_markup}   ·   {selfie_markup}")]
        else:
            # Long custom endpoints / multi-model fan-out: two centered lines.
            status_lines = [
                _RichText.from_markup(video_markup),
                _RichText.from_markup(selfie_markup),
            ]
        use_ascii_banner = (
            bool(getattr(_RICH_CONSOLE, "is_terminal", False))
            and (_RICH_CONSOLE.width or 0) >= _BANNER_MIN_WIDTH
        )
        if use_ascii_banner:
            from rich.rule import Rule

            tagline = _RichText.from_markup(
                f"[bold white]ULTIMATE  {RELEASE_VERSION}[/bold white]"
                "[dim]  ·  Front → Selfie → Similarity → Video → rPPG → Oldcam[/dim]"
            )
            _RICH_CONSOLE.print(Align.center(_RichText(_BANNER_ASCII, style="bold cyan")))
            print()
            _RICH_CONSOLE.print(Align.center(tagline))
            for line in status_lines:
                _RICH_CONSOLE.print(Align.center(line))
            _RICH_CONSOLE.print(Rule(style="blue"))
            print()  # one blank line between the header and whatever follows
            return

        title = _RichText(f"SELFIE GEN ULTIMATE  {RELEASE_VERSION}", style="bold white")
        subtitle = _RichText("Front DL -> Selfie -> Similarity -> Video -> rPPG -> Oldcam", style="dim")
        _RICH_CONSOLE.print(
            Panel(
                _RichGroup(Align.center(title), Align.center(subtitle),
                           *[Align.center(line) for line in status_lines]),
                border_style="blue",
                padding=(0, 2),
            )
        )

    def display_configuration_menu(self):
        """Display top-level Selfie Gen Ultimate menu.

        No title banner here — display_header() already renders the branded
        panel right above this (the duplicated double-banner was part of the
        "initial menu looks like garbage" feedback, 2026-06-11)."""
        root_value = self.automation_root_folder or "(not set)"
        print(f"  Automation root: \033[97m{root_value}\033[0m")
        for line in self._automation_status_lines():
            print(f"  {line}")
        print()
        print("  \033[93m1\033[0m   End-to-End Auto Pipeline")
        print("  \033[93m2\033[0m   Scan automation root / preview cases")
        print("  \033[93m3\033[0m   Run/resume automation batch")
        print("  \033[93m4\033[0m   Automation settings")
        print("  \033[93m5\033[0m   Manual Kling video tools")
        print("  \033[93m6\033[0m   Launch GUI manual lab")
        print("  \033[93m7\033[0m   API keys / provider settings")
        print("  \033[93m8\033[0m   Dependency check")
        print("  \033[93m9\033[0m   Advanced video/model settings")
        print()
        print("  \033[91mq\033[0m   Quit")
        print()
        print(
            "\033[92m➤ Choose a workflow or paste automation root folder path (case folders need front.png/front.jpg/front.jpeg):\033[0m ",
            end="",
            flush=True,
        )

    def select_folder_gui(self):
        """Open GUI folder selection dialog"""
        return select_directory(title="Select Input Folder")

    def select_file_gui(self):
        """Open GUI file selection dialog"""
        return select_open_file(
            title="Select Single Input Image",
            filetypes=[
                ("Image files", "*.jpg *.jpeg *.png *.bmp *.gif *.webp *.tiff *.tif"),
                ("All files", "*.*"),
            ],
        )

    def launch_gui(self):
        """Launch the Tkinter GUI mode for drag-and-drop processing."""
        try:
            from kling_gui.main_window import KlingGUIWindow

            print("\nLaunching GUI mode...")
            gui = KlingGUIWindow(config_path=self.config_file)
            gui.run()
        except ImportError as e:
            self.print_red(f"\nGUI module not found: {e}")
            self.print_yellow("Make sure kling_gui package is in the same directory.")
            self.pause_review("Press Enter to continue...")
        except Exception as e:
            self.print_red(f"\nError launching GUI: {e}")
            self.pause_review("Press Enter to continue...")

    def check_dependencies(self):
        """Check and optionally install all required dependencies."""
        try:
            from dependency_checker import run_dependency_check

            if not self._use_legacy_prompt_ui():
                self.display_header()
                _RICH_CONSOLE.print(Panel.fit(
                    "[bold]📦  Dependency check[/bold]\n"
                    "[dim]Verifies the full face/video stack and offers installs for anything missing — can take a minute.[/dim]",
                    border_style="cyan",
                ))
            print()
            run_dependency_check(auto_mode=False)
            print()
            self.pause_review("Press Enter to continue...")
        except ImportError as e:
            self.print_red(f"\nDependency checker module not found: {e}")
            self.print_yellow(
                "Make sure dependency_checker.py is in the same directory."
            )
            self.pause_review("Press Enter to continue...")
        except Exception as e:
            self.print_red(f"\nError running dependency check: {e}")
            self.pause_review("Press Enter to continue...")

    def toggle_verbose_logging(self):
        """Toggle verbose logging on/off"""
        self.verbose_logging = not self.verbose_logging
        self.config["verbose_logging"] = self.verbose_logging
        self.save_config()
        self.setup_logging()

        status = "enabled" if self.verbose_logging else "disabled"
        print(f"\nVerbose logging {status}")
        time.sleep(1)

    def change_output_mode(self):
        """Change output mode between source folder and custom folder"""
        use_source = self.config.get("use_source_folder", True)
        current_label = (
            "Same folder as source images" if use_source
            else f"Custom folder ({self.config.get('output_folder', '?')})"
        )
        if not self._use_legacy_prompt_ui():
            pick = self._q_select(
                "Where should generated videos be saved?",
                [
                    ("📁  Same folder as source images (next to each input)", "1"),
                    ("🗂️   Custom folder (all videos to one location)", "2"),
                    ("↩️   Back", "0"),
                ],
                instruction=f"(current: {current_label})",
            )
            if pick in (None, "0"):
                return
            if pick == "1":
                self.config["use_source_folder"] = True
                self.save_config()
                self.print_green("✓ Output mode: same folder as source images")
                time.sleep(0.8)
                return
            # pick == "2": custom folder. Resolve the target path FIRST and only
            # flip use_source_folder + save once we have a valid folder — an
            # Esc/empty path must NOT persist use_source_folder=False with an
            # empty/stale output_folder (that breaks automation output). (A1)
            existing = str(self.config.get("output_folder", "") or "")
            new_path = self._q_text(
                "Custom output folder path:",
                default=existing,
            )
            if new_path is None or not new_path.strip():
                # Cancelled / empty: keep an existing valid folder if there is
                # one, otherwise leave the mode untouched entirely.
                if existing:
                    self.config["use_source_folder"] = False
                    self.save_config()
                    self.print_green(f"✓ Output mode: custom folder -> {existing}")
                else:
                    self.print_yellow("Output mode unchanged (no folder provided).")
                time.sleep(0.8)
                return
            np = new_path.strip().strip('"').strip("'")
            try:
                Path(np).mkdir(parents=True, exist_ok=True)
            except OSError as e:
                self.print_red(f"Error creating folder: {e}")
                time.sleep(1.0)
                return
            self.config["use_source_folder"] = False
            self.config["output_folder"] = np
            self.save_config()
            self.print_green(f"✓ Output mode: custom folder -> {np}")
            time.sleep(0.8)
            return

        print()
        print("\033[96m" + "─" * 60 + "\033[0m")
        print("\033[95m OUTPUT MODE SETTINGS\033[0m")
        print("\033[96m" + "─" * 60 + "\033[0m")
        print()

        if use_source:
            print(f"  \033[92m✓ Current: SAME FOLDER AS SOURCE IMAGES\033[0m")
            print(f"     Videos are saved alongside each input image")
        else:
            print(f"  \033[93m✓ Current: CUSTOM FOLDER\033[0m")
            print(f"     All videos go to: {self.config['output_folder']}")
        print()

        print("\033[93mOptions:\033[0m")
        print(
            f"  \033[96m1\033[0m   Use source folder (save video next to input image)"
        )
        print(f"  \033[96m2\033[0m   Use custom folder (all videos to one location)")
        print(f"  \033[91m0\033[0m   Cancel")
        print()

        choice = self._safe_input("\033[92mSelect option: \033[0m").strip()

        if choice == "1":
            self.config["use_source_folder"] = True
            self.save_config()
            print("\n\033[92m✓ Output mode: SAME FOLDER AS SOURCE IMAGES\033[0m")
            print("  Videos will be saved alongside each input image")
            time.sleep(1.5)
        elif choice == "2":
            existing = str(self.config.get("output_folder", "") or "")
            print(f"\n\033[93mCurrent custom folder:\033[0m {existing or '(none)'}")
            new_path = self._safe_input(
                "\033[92mEnter new folder path (or Enter to keep current):\033[0m "
            ).strip()

            if new_path and (
                (new_path.startswith('"') and new_path.endswith('"'))
                or (new_path.startswith("'") and new_path.endswith("'"))
            ):
                new_path = new_path[1:-1]

            # Resolve the target FIRST; only flip use_source_folder + save once a
            # valid folder is in hand. An empty answer must not persist
            # use_source_folder=False with an empty output_folder (A1).
            if not new_path:
                if existing:
                    self.config["use_source_folder"] = False
                    self.save_config()
                    print(f"\n\033[92m✓ Output mode: CUSTOM FOLDER -> {existing}\033[0m")
                else:
                    print("\033[93mOutput mode unchanged (no folder provided).\033[0m")
                time.sleep(1.0)
                return
            try:
                Path(new_path).mkdir(parents=True, exist_ok=True)
            except Exception as e:
                self.print_red(f"Error creating folder: {e}")
                time.sleep(1.5)
                return
            self.config["use_source_folder"] = False
            self.config["output_folder"] = new_path
            self.save_config()
            print(f"\n\033[92m✓ Output mode: CUSTOM FOLDER\033[0m")
            print(f"  All videos will go to: {new_path}")
            time.sleep(1.5)
        else:
            print("\033[90mCancelled\033[0m")
            time.sleep(0.5)

    def configure_advanced_video_settings(self):
        """Configure advanced video generation settings"""
        while True:
            aspect_ratio = self.config.get("aspect_ratio", "3:4")
            resolution = self.config.get("resolution", "720p")
            seed = self.config.get("seed", -1)
            camera_fixed = self.config.get("camera_fixed", False)
            generate_audio = self.config.get("generate_audio", False)
            seed_display = "Random" if seed == -1 else str(seed)

            if not self._use_legacy_prompt_ui():
                choice = self._q_menu(
                    "Advanced Video Settings",
                    [
                        (f"📐  Aspect ratio    : {aspect_ratio}", "1"),
                        (f"🖥️   Resolution      : {resolution}", "2"),
                        (f"🎲  Seed            : {seed_display}", "3"),
                        (f"📷  Camera fixed    : {'ON' if camera_fixed else 'OFF'}", "4"),
                        (f"🔊  Generate audio  : {'ON' if generate_audio else 'OFF'}", "5"),
                        ("↩️   Back to main menu", "0"),
                    ],
                )
            else:
                print()
                print("\033[96m" + "─" * 60 + "\033[0m")
                print("\033[95m ADVANCED VIDEO SETTINGS\033[0m")
                print("\033[96m" + "─" * 60 + "\033[0m")
                print()
                camera_status = "\033[92mON\033[0m" if camera_fixed else "\033[91mOFF\033[0m"
                audio_status = "\033[92mON\033[0m" if generate_audio else "\033[91mOFF\033[0m"
                print(f"  \033[93m1\033[0m   Aspect Ratio    : \033[97m{aspect_ratio}\033[0m")
                print(f"  \033[93m2\033[0m   Resolution      : \033[97m{resolution}\033[0m")
                print(f"  \033[93m3\033[0m   Seed            : \033[97m{seed_display}\033[0m")
                print(f"  \033[93m4\033[0m   Camera Fixed    : {camera_status}")
                print(f"  \033[93m5\033[0m   Generate Audio  : {audio_status}")
                print()
                print(f"  \033[91m0\033[0m   Back to main menu")
                print()
                try:
                    choice = self._safe_input("\033[92mSelect option: \033[0m").strip().lower()
                except EOFError:
                    choice = "0"

            if choice in (None, "0", "q"):
                break
            elif choice == "1":
                self._set_aspect_ratio()
            elif choice == "2":
                self._set_resolution()
            elif choice == "3":
                self._set_seed()
            elif choice == "4":
                self.config["camera_fixed"] = not self.config.get("camera_fixed", False)
                self.save_config()
                self.print_green(f"✓ Camera fixed {'enabled' if self.config['camera_fixed'] else 'disabled'}")
                time.sleep(0.6)
            elif choice == "5":
                self.config["generate_audio"] = not self.config.get("generate_audio", False)
                self.save_config()
                self.print_green(f"✓ Generate audio {'enabled' if self.config['generate_audio'] else 'disabled'}")
                time.sleep(0.6)
            else:
                self.print_red("Invalid option")
                time.sleep(0.4)

    def _set_aspect_ratio(self):
        """Set video aspect ratio"""
        self._edit_indexed_choice_setting(
            "aspect_ratio", "Video aspect ratio",
            _VIDEO_ASPECT_RATIO_OPTIONS, default="3:4",
        )

    def _set_resolution(self):
        """Set video resolution"""
        self._edit_indexed_choice_setting(
            "resolution", "Video resolution",
            _VIDEO_RESOLUTION_OPTIONS, default="720p",
        )

    def _edit_indexed_choice_setting(self, key, label, options, default):
        """Set a config value from a fixed option list (questionary + legacy).

        The legacy path is index-based against ``options`` so it scales when an
        option is added — no per-option hardcoded branch to forget to update
        (D3/D4/G3). Cancel ("0"/Esc) leaves the value untouched.
        """
        current = self.config.get(key, default)
        if not self._use_legacy_prompt_ui():
            selected = self._q_select(
                f"{label}:", list(options), default=current,
                instruction=f"(current: {current})",
            )
            if selected:
                self.config[key] = selected
                self.save_config()
                self.print_green(f"✓ {label} set to {selected}")
                time.sleep(0.6)
            return
        print()
        print(f"\033[95mSelect {label}:\033[0m")
        for i, opt in enumerate(options, 1):
            mark = " (current)" if opt == current else ""
            print(f"  \033[96m{i}\033[0m   {opt}{mark}")
        print(f"  \033[91m0\033[0m   Cancel")
        print()
        choice = self._safe_input("\033[92mSelect: \033[0m").strip()
        if choice.isdigit() and 1 <= int(choice) <= len(options):
            selected = options[int(choice) - 1]
            self.config[key] = selected
            self.save_config()
            print(f"\n\033[92m✓ {label} set to {selected}\033[0m")
            time.sleep(0.8)
        elif choice != "0":
            print("\033[91mInvalid option\033[0m")
            time.sleep(0.5)

    def _set_seed(self):
        """Set generation seed"""
        current_seed = self.config.get("seed", -1)
        seed_display = "Random" if current_seed == -1 else str(current_seed)

        def _apply(raw: str) -> None:
            low = raw.strip().lower()
            if low in {"r", "random", "-1", ""}:
                self.config["seed"] = -1
                self.save_config()
                self.print_green("✓ Seed set to random")
            else:
                try:
                    self.config["seed"] = int(low)
                    self.save_config()
                    self.print_green(f"✓ Seed set to {int(low)}")
                except ValueError:
                    self.print_red("Invalid seed value (must be an integer or 'r')")
            time.sleep(0.7)

        if not self._use_legacy_prompt_ui():
            raw = self._q_text(
                "Seed (integer, or 'r' for random):",
                default=seed_display if seed_display != "Random" else "r",
                instruction=f"(current: {seed_display})",
            )
            if raw is not None:
                _apply(raw)
            return
        print()
        print(f"\033[95mCurrent seed:\033[0m {seed_display}")
        print("\nEnter a seed number (integer) or 'r' for random\n")
        choice = self._safe_input("\033[92mSeed: \033[0m").strip().lower()
        if choice:
            _apply(choice)

    def inspect_model_capabilities(self):
        """Show detailed capabilities of a model via OpenAPI schema inspection"""
        from model_schema_manager import ModelSchemaManager

        self.clear_screen()
        print("\033[96m" + "═" * 79 + "\033[0m")
        self.print_magenta("                       MODEL CAPABILITY INSPECTOR")
        print("\033[96m" + "═" * 79 + "\033[0m")
        print()

        # Resolve via the saved config + any fal env alias (FAL_KEY OR
        # FAL_API_KEY), not a bare os.getenv("FAL_KEY") — otherwise a user who
        # stores their key as FAL_API_KEY hits "key not set" here even though the
        # rest of the app works (code-review Codex P2 #73).
        api_key = resolve_api_key(self.config, "falai_api_key")
        if not api_key:
            self.print_red(
                "No fal.ai key found (set falai_api_key in config, or the "
                "FAL_KEY / FAL_API_KEY environment variable)."
            )
            self.pause_continue("\nPress Enter to continue...")
            return

        # Available models to inspect — derived from the shared _MODEL_PRESETS so
        # a model added to the picker is automatically inspectable (D3, no second
        # hand-maintained list to drift).
        models = {
            str(i): (endpoint, name)
            for i, (name, endpoint, _dur) in enumerate(self._MODEL_PRESETS, 1)
        }

        # Add current model if not in list
        current_model = self.config.get(
            "current_model", "fal-ai/kling-video/v2.5-turbo/standard/image-to-video"
        )
        if current_model not in [m[0] for m in models.values()]:
            models["c"] = (current_model, f"Current: {current_model.split('/')[-1]}")

        if not self._use_legacy_prompt_ui():
            choices = []
            for key, (model_id, name) in models.items():
                marker = "  (current)" if model_id == current_model else ""
                choices.append((f"{name}{marker}  —  {model_id}", key))
            choices.append(("↩️   Back", "q"))
            choice = self._q_menu("Model Capability Inspector", choices, show_header=False)
            if choice in (None, "q") or choice not in models:
                return
        else:
            print("\033[93mSelect a model to inspect:\033[0m")
            print()
            for key, (model_id, name) in models.items():
                marker = " \033[92m(current)\033[0m" if model_id == current_model else ""
                print(f"  \033[93m{key}\033[0m  {name}{marker}")
                print(f"      \033[90m{model_id}\033[0m")
            print()
            print(f"  \033[91mq\033[0m  Back to menu")
            print()
            try:
                choice = self._safe_input("\033[92m➤ Select model: \033[0m").strip().lower()
            except EOFError:
                return
            if choice == "q" or choice not in models:
                return

        model_id, model_name = models[choice]

        print()
        print(f"\033[96mFetching schema for {model_name}...\033[0m")
        print()

        try:
            schema_manager = ModelSchemaManager(api_key)
            schema = schema_manager.get_model_schema(model_id)

            if not schema:
                self.print_yellow(f"No schema found for {model_id}")
                self.print_yellow(
                    "This model may not be available or the API returned no data."
                )
                self.pause_continue("\nPress Enter to continue...")
                return

            # schema is Dict[str, ModelParameter]
            # Separate required and optional
            required = [p for p in schema.values() if p.required]
            optional = [p for p in schema.values() if not p.required]

            print("\033[96m" + "─" * 79 + "\033[0m")
            print(f"\033[97m{model_name}\033[0m")
            print(f"\033[90m{model_id}\033[0m")
            print("\033[96m" + "─" * 79 + "\033[0m")
            print()

            # Required parameters
            print(f"\033[92mREQUIRED PARAMETERS ({len(required)}):\033[0m")
            if required:
                for p in sorted(required, key=lambda x: x.name):
                    ptype = p.type
                    desc = p.description[:60] if p.description else ""
                    print(f"  \033[97m{p.name}\033[0m \033[90m({ptype})\033[0m")
                    if desc:
                        print(f"    {desc}")
            else:
                print("  \033[90m(none)\033[0m")
            print()

            # Optional parameters
            print(f"\033[93mOPTIONAL PARAMETERS ({len(optional)}):\033[0m")
            if optional:
                for p in sorted(optional, key=lambda x: x.name):
                    ptype = p.type
                    default = p.default
                    enum_vals = p.enum
                    desc = p.description[:50] if p.description else ""

                    default_str = ""
                    if default is not None:
                        default_str = f" = \033[95m{default}\033[0m"

                    print(
                        f"  \033[97m{p.name}\033[0m \033[90m({ptype}){default_str}\033[0m"
                    )

                    if enum_vals:
                        enum_preview = ", ".join(str(v) for v in enum_vals[:5])
                        if len(enum_vals) > 5:
                            enum_preview += f", ... (+{len(enum_vals) - 5})"
                        print(f"    \033[90mAllowed: [{enum_preview}]\033[0m")

                    if desc:
                        print(f"    {desc}")
            else:
                print("  \033[90m(none)\033[0m")

            print()
            print("\033[96m" + "─" * 79 + "\033[0m")

            # Show specific parameter support for key features
            key_params = [
                "seed",
                "aspect_ratio",
                "duration",
                "cfg_scale",
                "negative_prompt",
            ]
            print("\033[97mKEY FEATURE SUPPORT:\033[0m")
            for param in key_params:
                supported = schema_manager.supports_parameter(model_id, param)
                status = "\033[92m✓\033[0m" if supported else "\033[91m✗\033[0m"
                print(f"  {status} {param}")

            print()

        except Exception as e:
            self.print_red(f"Error fetching schema: {e}")

        self.pause_continue("\nPress Enter to continue...")

    def _edit_prompt_questionary(self, current_slot, current_prompt, default_prompt):
        """Branded prompt editor (questionary). Slot previews in the status
        panel; reset / edit positive / edit negative / clear actions."""
        saved_prompts = self.config.get("saved_prompts", {})
        status = ["Saved prompt slots:"]
        for i in range(1, _PROMPT_SLOT_COUNT + 1):
            key = str(i)
            p = saved_prompts.get(key) or ""
            preview = (p[:46] + "…") if len(p) > 46 else (p or "(empty)")
            active = "  (ACTIVE)" if key == current_slot else ""
            status.append(f"  [{i}] {preview}{active}")
        preview_cur = current_prompt if len(current_prompt) <= 200 else current_prompt[:200] + "…"
        status.append("")
        status.append(f"Active slot {current_slot}: {preview_cur}")
        pick = self._q_menu(
            "Kling Prompt Editor",
            [
                ("↺  Reset active slot to default (head movement)", "1"),
                ("✏️   Edit positive prompt for active slot", "2"),
                ("🚫  Edit negative prompt for active slot", "3"),
                ("🧹  Clear active slot", "4"),
                ("↩️   Return without changes", "5"),
            ],
            status_lines=status,
        )
        if pick in (None, "5"):
            return
        if pick == "1":
            if current_prompt.strip() and not self._confirm(
                f"Overwrite slot {current_slot} with the default prompt?", default=False
            ):
                return
            self.config["saved_prompts"][current_slot] = default_prompt
            self.save_config()
            self.print_green("✓ Reset to default head-movement prompt")
            time.sleep(0.8)
        elif pick == "2":
            new_prompt = self._q_text(
                f"Positive prompt for slot {current_slot}:",
                default=current_prompt,
            )
            if new_prompt and new_prompt.strip():
                self.config["saved_prompts"][current_slot] = new_prompt.strip()
                self.save_config()
                self.print_green(f"✓ Prompt saved to slot {current_slot}")
                time.sleep(0.8)
        elif pick == "3":
            existing_neg = str(self.config.get("negative_prompts", {}).get(current_slot, ""))
            neg = self._q_text(
                f"Negative prompt for slot {current_slot} (what to avoid):",
                default=existing_neg,
            )
            if neg is not None:
                self.config.setdefault("negative_prompts", {})[current_slot] = neg.strip()
                self.save_config()
                self.print_green(f"✓ Negative prompt saved to slot {current_slot}")
                time.sleep(0.8)
        elif pick == "4":
            if (current_prompt.strip() or self.config.get("negative_prompts", {}).get(current_slot)) and not self._confirm(
                f"Clear slot {current_slot} (positive + negative prompt)?", default=False
            ):
                return
            self.config["saved_prompts"][current_slot] = ""
            self.config.setdefault("negative_prompts", {})[current_slot] = ""
            self.save_config()
            self.print_yellow(f"Slot {current_slot} cleared")
            time.sleep(0.8)

    def edit_prompt(self):
        """Edit or view the Kling generation prompt (full editor with slot support)"""
        current_slot = str(self.config.get("current_prompt_slot", DEFAULT_KLING_PROMPT_SLOT))
        current_prompt = self.get_current_prompt()
        default_prompt = self.get_default_prompt()

        if not self._use_legacy_prompt_ui():
            self._edit_prompt_questionary(current_slot, current_prompt, default_prompt)
            return

        self.clear_screen()
        print("\033[96m" + "═" * 79 + "\033[0m")
        self.print_magenta("                           KLING PROMPT EDITOR")
        print("\033[96m" + "═" * 79 + "\033[0m")
        print()

        # Show all slots
        print("\033[93mSaved Prompts:\033[0m")
        saved_prompts = self.config.get("saved_prompts", {})
        for i in range(1, _PROMPT_SLOT_COUNT + 1):
            slot_key = str(i)
            prompt = saved_prompts.get(slot_key)
            active = " \033[92m(ACTIVE)\033[0m" if slot_key == current_slot else ""
            if prompt:
                preview = prompt[:50] + "..." if len(prompt) > 50 else prompt
                print(f"  [{i}] {preview}{active}")
            else:
                print(f"  [{i}] \033[90m(empty){active}\033[0m")
        print()

        # Show current prompt in full
        print("\033[93mCurrent Prompt (Slot {}):\033[0m".format(current_slot))
        print("\033[97m" + "─" * 79 + "\033[0m")
        words = current_prompt.split()
        line = ""
        for word in words:
            if len(line) + len(word) + 1 <= 75:
                line += word + " "
            else:
                print(f"  {line}")
                line = word + " "
        if line:
            print(f"  {line}")
        print("\033[97m" + "─" * 79 + "\033[0m")

        # Show negative prompt if exists
        neg_prompt = self.config.get("negative_prompts", {}).get(current_slot)
        if neg_prompt:
            print(f"\033[91mNegative Prompt:\033[0m {neg_prompt}")
            print("\033[97m" + "─" * 79 + "\033[0m")
        print()

        print("\033[92mOptions:\033[0m")
        print("  \033[93m1\033[0m - Reset to default prompt (head movement)")
        print("  \033[93m2\033[0m - Enter custom prompt for current slot")
        print("  \033[93m3\033[0m - Edit NEGATIVE prompt for current slot")
        print("  \033[93m4\033[0m - Clear current slot (make empty)")
        print("  \033[93m5\033[0m - Return without changes")
        print()

        choice = self._safe_input("\033[92mSelect option (1-5): \033[0m").strip()

        if choice == "1":
            self.config["saved_prompts"][current_slot] = default_prompt
            self.save_config()
            print("\n\033[92mReset to default head movement prompt\033[0m")
            time.sleep(1.5)
        elif choice == "2":
            print()
            print(
                "\033[93mEnter your custom prompt (press Enter twice when done):\033[0m"
            )
            print("\033[90m(Tip: You can paste multi-line text)\033[0m")
            print()

            lines = []
            empty_count = 0
            while True:
                try:
                    line = input()
                    if line:
                        lines.append(line)
                        empty_count = 0
                    else:
                        empty_count += 1
                        if empty_count >= 2:
                            break
                except EOFError:
                    break

            if lines:
                custom_prompt = " ".join(lines).strip()
                self.config["saved_prompts"][current_slot] = custom_prompt
                self.save_config()
                print(
                    "\n\033[92mCustom prompt saved to Slot {}!\033[0m".format(
                        current_slot
                    )
                )
                time.sleep(1.5)
            else:
                print("\n\033[91mNo prompt entered, keeping current\033[0m")
                time.sleep(1.5)
        elif choice == "3":
            print()
            print(
                "\033[93mEnter NEGATIVE prompt (what to avoid - e.g. 'blur, bokeh'):\033[0m"
            )
            neg_prompt = self._safe_input("\033[92m➤ \033[0m").strip()

            if neg_prompt:
                self.config["negative_prompts"][current_slot] = neg_prompt
                self.save_config()
                print(
                    "\n\033[92mNegative prompt saved to Slot {}!\033[0m".format(
                        current_slot
                    )
                )
                time.sleep(1.5)
            else:
                print("\n\033[90mCancelled\033[0m")
                time.sleep(0.5)
        elif choice == "4":
            self.config["saved_prompts"][current_slot] = ""
            self.config["negative_prompts"][current_slot] = ""
            self.save_config()
            print("\n\033[93mSlot {} cleared\033[0m".format(current_slot))
            time.sleep(1.5)

    def quick_edit_prompt(self, target: str = "gui"):
        """Quick inline prompt editor - single line input.

        ``target="cli"`` edits the text of the CLI pipeline's active slot
        (the slot POINTER is per-surface; the TEXT is shared with the GUI by
        design — saved_prompts is the single prompt store)."""
        if target == "cli":
            current_slot = str(resolve_cli_kling_prompt_slot(self.config, DEFAULT_KLING_PROMPT_SLOT))
        else:
            current_slot = str(self.config.get("current_prompt_slot", DEFAULT_KLING_PROMPT_SLOT))
        existing = str(self.config.get("saved_prompts", {}).get(current_slot, ""))
        if not self._use_legacy_prompt_ui():
            new_prompt = self._q_text(
                f"Kling prompt for slot {current_slot} (Esc to cancel):",
                default=existing,
            )
            if new_prompt and new_prompt.strip():
                self.config["saved_prompts"][current_slot] = new_prompt.strip()
                self.save_config()
                self.print_green(f"✓ Prompt saved to slot {current_slot}")
                time.sleep(0.8)
            return
        print()
        print(
            "\033[93mQuick Edit - Enter new prompt (single line, or press Enter to cancel):\033[0m"
        )
        new_prompt = self._safe_input("\033[92m➤ \033[0m").strip()
        if new_prompt:
            self.config["saved_prompts"][current_slot] = new_prompt
            self.save_config()
            print("\033[92m✓ Prompt saved to Slot {}\033[0m".format(current_slot))
            time.sleep(1)
        else:
            print("\033[90mCancelled\033[0m")
            time.sleep(0.5)

    def swap_prompt_slot(self, target: str = "gui"):
        """Swap active Kling video prompt slot across slots 1-10.

        Invalid or missing slot values fall back to slot 4 defaults.
        ``target="cli"`` moves the CLI pipeline's own slot pointer
        (cli_kling_prompt_slot) without touching the GUI's current_prompt_slot.
        """
        saved_prompts = self.config.get("saved_prompts", {})
        if target == "cli":
            current_slot = resolve_cli_kling_prompt_slot(self.config, DEFAULT_KLING_PROMPT_SLOT)
            slot_key = "cli_kling_prompt_slot"
        else:
            current_slot = self.config.get("current_prompt_slot", DEFAULT_KLING_PROMPT_SLOT)
            slot_key = "current_prompt_slot"

        def _apply(choice: str) -> None:
            self.config[slot_key] = int(choice)
            self.save_config()
            if saved_prompts.get(choice):
                self.print_green(f"✓ Switched to slot {choice}")
            else:
                self.print_yellow(f"⚠ Switched to slot {choice} (empty - will use default)")
            time.sleep(0.8)

        if not self._use_legacy_prompt_ui():
            slot_choices = []
            for i in range(1, _PROMPT_SLOT_COUNT + 1):
                key = str(i)
                prompt = saved_prompts.get(key) or ""
                preview = (prompt[:48] + "…") if len(prompt) > 48 else (prompt or "(empty)")
                active = "  ◄ ACTIVE" if key == str(current_slot) else ""
                slot_choices.append((f"[{i}] {preview}{active}", key))
            slot_choices.append(("↩️   Back", "cancel"))
            pick = self._q_select(
                "Active Kling prompt slot:", slot_choices,
                instruction=f"(current: slot {current_slot})",
            )
            if pick and pick != "cancel":
                _apply(pick)
            return

        print()
        print("\033[93mSaved Prompts:\033[0m")
        for i in range(1, _PROMPT_SLOT_COUNT + 1):
            slot_key = str(i)
            prompt = saved_prompts.get(slot_key)
            active = " \033[92m◄ ACTIVE\033[0m" if slot_key == str(current_slot) else ""
            if prompt:
                preview = prompt[:60] + "..." if len(prompt) > 60 else prompt
                print(f"  [\033[96m{i}\033[0m] {preview}{active}")
            else:
                print(f"  [\033[90m{i}\033[0m] \033[90m(empty)\033[0m{active}")
        print()

        choice = self._safe_input("\033[92mSelect slot (1-10) or Enter to cancel: \033[0m").strip()
        if choice.isdigit() and 1 <= int(choice) <= 10:
            self.config[slot_key] = int(choice)
            self.save_config()
            prompt = saved_prompts.get(choice)
            if prompt:
                print(f"\033[92m✓ Switched to Slot {choice}\033[0m")
            else:
                print(
                    f"\033[93m⚠ Switched to Slot {choice} (empty - will use default)\033[0m"
                )
            time.sleep(1)
        else:
            print("\033[90mCancelled\033[0m")
            time.sleep(0.5)

    # Preset video models offered by the model picker (name, endpoint, default
    # duration). Shared by the questionary + legacy paths so they never drift.
    _MODEL_PRESETS = [
        ("Kling 2.5 Turbo Standard", "fal-ai/kling-video/v2.5-turbo/standard/image-to-video", 10),
        ("Kling 2.5 Turbo Pro", "fal-ai/kling-video/v2.5-turbo/pro/image-to-video", 10),
        ("Kling 2.1 Standard", "fal-ai/kling-video/v2.1/standard/image-to-video", 10),
        ("Kling 2.1 Pro", "fal-ai/kling-video/v2.1/pro/image-to-video", 10),
        ("Wan 2.5", "fal-ai/wan-25-preview/image-to-video", 5),
        ("Veo 3", "fal-ai/veo3/image-to-video", 8),
        ("Ovi", "fal-ai/ovi/image-to-video", 5),
    ]

    def _apply_model_choice(self, name: str, endpoint: str, duration: int, target: str = "gui") -> None:
        # target="cli" writes the CLI automation pipeline's own keys so the
        # GUI's manual-lab selection is never clobbered (per-surface split,
        # 2026-06-11); target="gui" is the manual-tools/GUI-shared selection.
        if target == "cli":
            self.config["cli_video_model"] = endpoint
            self.config["cli_video_model_display_name"] = name
            self.config["cli_video_duration"] = duration
        else:
            self.config["current_model"] = endpoint
            self.config["model_display_name"] = name
            self.config["video_duration"] = duration
        self._cached_price = None
        self.save_config()
        self.print_green(f"✓ {'Automation video model' if target == 'cli' else 'Model'} set to: {name}")
        time.sleep(0.9)

    def select_model(self, target: str = "gui"):
        """Select AI model from presets or enter custom endpoint.

        ``target="cli"`` edits the automation pipeline's per-surface selection
        (cli_video_model); the default edits the GUI/manual-tools selection."""
        if not self._use_legacy_prompt_ui():
            if target == "cli":
                current_model, current_display = resolve_cli_video_model(self.config)
                current_model = current_model or ""
            else:
                current_model = self.config.get("current_model", "")
                current_display = self.config.get("model_display_name", "Unknown")
            choices = []
            for name, endpoint, duration in self._MODEL_PRESETS:
                price = self.fetch_model_pricing(endpoint)
                price_str = f"${price:.2f}/sec" if price else "check fal.ai"
                active = "  ◄" if endpoint == current_model else ""
                choices.append((f"{name}  ({price_str}){active}", endpoint))
            choices.append(("➕  Enter custom endpoint…", "__custom__"))
            choices.append(("🌐  Fetch all models from fal.ai", "__fetch__"))
            choices.append(("↩️   Back", "__cancel__"))
            pick = self._q_menu(
                "Automation Video Model" if target == "cli" else "Model Selection",
                choices,
                status_lines=[
                    f"Current: {current_display or 'Unknown'}",
                    f"Endpoint: {current_model}",
                ],
            )
            if pick in (None, "__cancel__"):
                return
            if pick == "__custom__":
                endpoint = self._q_text(
                    "fal.ai endpoint id (e.g. fal-ai/kling-video/v2.5-turbo/standard/image-to-video):"
                )
                if not endpoint or not endpoint.strip():
                    return
                endpoint = endpoint.strip()
                name = self._q_text("Display name:", default=endpoint) or endpoint
                dur_raw = self._q_text("Video duration seconds (5/10/15):", default="10")
                duration = int(dur_raw) if dur_raw and dur_raw.strip().isdigit() else 10
                if duration not in _COMMON_VIDEO_DURATIONS:
                    self.print_yellow(f"⚠ Uncommon duration {duration}s — verify the model supports it.")
                self._apply_model_choice(name.strip(), endpoint, duration, target=target)
                return
            if pick == "__fetch__":
                # Delegate the paginated "all models" browser to the legacy flow
                # (its number-paged input() UX is fine for this advanced path).
                return self._select_model_fetch_all_legacy(target=target)
            # A preset endpoint was chosen.
            for name, endpoint, duration in self._MODEL_PRESETS:
                if endpoint == pick:
                    self._apply_model_choice(name, endpoint, duration, target=target)
                    return
            return
        return self._select_model_legacy()

    def _select_model_legacy(self):
        """Legacy numbered model picker (non-TTY / piped stdin)."""
        self.clear_screen()

        print("\033[96m" + "═" * 79 + "\033[0m")
        self.print_magenta("                           MODEL SELECTION")
        print("\033[96m" + "═" * 79 + "\033[0m")
        print()

        current_model = self.config.get("current_model", "")
        current_name = self.config.get("model_display_name", "Unknown")
        print(f"\033[95mCurrent model:\033[0m {current_name}")
        print(f"\033[90m  Endpoint: {current_model}\033[0m")
        print()

        # Preset models — SAME source as the questionary path (the whole point
        # of _MODEL_PRESETS is that both pickers stay in lockstep, including the
        # default Kling 2.5 Turbo Standard at position 1).
        presets = self._MODEL_PRESETS

        print("\033[93mPreset Models:\033[0m")
        for idx, (name, endpoint, duration) in enumerate(presets, 1):
            # Fetch pricing
            price = self.fetch_model_pricing(endpoint)
            price_str = f"${price:.2f}/sec" if price else "check fal.ai"
            active = " \033[92m◄\033[0m" if endpoint == current_model else ""
            print(f"  \033[96m{idx}\033[0m   {name} ({price_str}){active}")

        print()
        print(f"  \033[93m6\033[0m   Enter custom endpoint")
        print(f"  \033[93m7\033[0m   Fetch all models from fal.ai")
        print(f"  \033[91m0\033[0m   Cancel")
        print()
        print(f"  \033[90mSee all: https://fal.ai/models?category=video\033[0m")
        print()

        choice = self._safe_input("\033[92mSelect option: \033[0m").strip()

        if choice == "0":
            return
        elif choice == "6":
            # Custom endpoint
            print()
            print(
                "\033[93mEnter fal.ai endpoint ID (e.g., fal-ai/kling-video/v2.1/pro/image-to-video):\033[0m"
            )
            endpoint = self._safe_input("\033[92m➤ \033[0m").strip()
            if endpoint:
                name = (
                    self._safe_input("\033[92mDisplay name for this model: \033[0m").strip()
                    or endpoint
                )
                # Duration prompt with common options
                print("\033[93mCommon durations: 5s (most models), 10s (most models), 15s (some models)\033[0m")
                duration_input = self._safe_input(
                    "\033[92mVideo duration in seconds (5, 10, 15, default 10): \033[0m"
                ).strip()

                # Parse and validate duration
                if duration_input.isdigit():
                    duration = int(duration_input)
                    # Warn about uncommon durations but allow them
                    if duration not in _COMMON_VIDEO_DURATIONS:
                        print(f"\033[93m⚠ Uncommon duration {duration}s - verify model supports this\033[0m")
                else:
                    duration = 10

                self.config["current_model"] = endpoint
                self.config["model_display_name"] = name
                self.config["video_duration"] = duration
                self._cached_price = None  # Clear cache
                self.save_config()
                print(f"\033[92m✓ Model set to: {name}\033[0m")
                time.sleep(1.5)
        elif choice == "7":
            self._select_model_fetch_all_legacy()
        elif choice.isdigit() and 1 <= int(choice) <= len(presets):
            name, endpoint, duration = presets[int(choice) - 1]
            self.config["current_model"] = endpoint
            self.config["model_display_name"] = name
            self.config["video_duration"] = duration
            self._cached_price = None  # Clear price cache
            self.save_config()
            print(f"\033[92m✓ Model set to: {name}\033[0m")
            time.sleep(1.5)

    def _select_model_fetch_all_legacy(self, target: str = "gui"):
        """Paginated browser of every fal.ai image-to-video model (input()-based).

        Reached from both the questionary picker ("Fetch all models") and the
        legacy numbered picker (option 7). The number-paged UX is intentionally
        kept as input() — it's an advanced, high-cardinality list where a flat
        arrow menu would be unwieldy. ``target`` flows through to
        _apply_model_choice (cli = automation per-surface keys).
        """
        print("\n\033[93mFetching all image-to-video models from fal.ai...\033[0m")
        models = self.fetch_available_models()
        if target == "cli":
            current_model = resolve_cli_video_model(self.config)[0] or ""
        else:
            current_model = self.config.get("current_model", "")
        page_size = 40
        page = 0
        total_pages = (len(models) + page_size - 1) // page_size

        print(f"\033[92mFound {len(models)} models total\033[0m")

        while True:
            start_idx = page * page_size
            end_idx = min(start_idx + page_size, len(models))
            page_models = models[start_idx:end_idx]

            print(f"\n\033[92m{'═' * 60}\033[0m")
            print(
                f"\033[92m  Image-to-Video Models  ·  Page {page + 1}/{total_pages}  ·  Showing {start_idx + 1}-{end_idx} of {len(models)}\033[0m"
            )
            print(f"\033[92m{'═' * 60}\033[0m\n")
            for idx, m in enumerate(page_models, start_idx + 1):
                endpoint = m.get("endpoint_id", "")
                name = m.get("name", endpoint)
                description = m.get("description", "")
                price_info = m.get("price")
                if price_info:
                    price_str = f"${price_info['price']:.3f}/{price_info['unit']}"
                else:
                    price_str = "pricing unavailable"
                active = (
                    "  \033[92m◄ CURRENT\033[0m" if endpoint == current_model else ""
                )
                print(f"  \033[96m{idx:2d}\033[0m  \033[1;97m{name}\033[0m{active}")
                print(f"       Price: \033[93m{price_str}\033[0m")
                if description:
                    words = description.split()
                    lines = []
                    current_line = ""
                    for word in words:
                        if len(current_line) + len(word) + 1 <= 65:
                            current_line += (" " if current_line else "") + word
                        else:
                            if current_line:
                                lines.append(current_line)
                            current_line = word
                        if len(lines) >= 3:
                            break
                    if current_line and len(lines) < 3:
                        lines.append(current_line)
                    for line in lines[:3]:
                        print(f"       \033[90m{line}\033[0m")
                print(f"       \033[36m{endpoint}\033[0m")
                print()

            print()
            nav_hint = []
            if page > 0:
                nav_hint.append("p=prev")
            if page < total_pages - 1:
                nav_hint.append("n=next")
            nav_str = f" ({', '.join(nav_hint)})" if nav_hint else ""

            try:
                sel = self._safe_input(
                    f"\033[92mEnter number to select{nav_str}, or Enter to cancel: \033[0m"
                ).strip().lower()
            except EOFError:
                break

            if sel == "n" and page < total_pages - 1:
                page += 1
                continue
            elif sel == "p" and page > 0:
                page -= 1
                continue
            elif sel == "" or sel == "q":
                break
            elif sel.isdigit() and 1 <= int(sel) <= len(models):
                selected = models[int(sel) - 1]
                self._apply_model_choice(
                    selected.get("name", selected.get("endpoint_id")),
                    selected.get("endpoint_id"),
                    selected.get("duration", 10),
                    target=target,
                )
                break
            else:
                print("\033[91mInvalid selection\033[0m")
                time.sleep(1)

    def run_configuration_menu(self):
        """Main top-level menu loop."""
        # Silent one-time upgrade to the current recommended pipeline baseline
        # (interactive entry only — headless --batch never lands here, so a
        # scheduled run's behavior can't flip mid-schedule).
        self._auto_upgrade_recommended_defaults()
        while True:
            if self._use_legacy_prompt_ui():
                result = self._run_configuration_menu_legacy_iteration()
            else:
                result = self._run_configuration_menu_questionary_iteration()
            if result is not None:
                # A non-None return is a selected manual-input path (from the
                # manual Kling menu) that the caller wants to act on.
                return result

    def _dispatch_configuration_choice(self, choice_lower: str) -> "Optional[str]":
        """Run the action for a top-level menu choice ("1".."9").

        Returns a manual-input path string when the manual Kling menu yields one
        (so the outer loop can return it), else None to keep looping.
        """
        if choice_lower == "1":
            self.run_automation_menu()
        elif choice_lower == "2":
            self._scan_automation_cases()
        elif choice_lower == "3":
            self._run_resume_automation()
        elif choice_lower == "4":
            self._edit_automation_settings()
        elif choice_lower == "5":
            selected_path = self._run_manual_kling_menu()
            if selected_path:
                return selected_path
        elif choice_lower == "6":
            self.launch_gui()
        elif choice_lower == "7":
            self.configure_api_provider_settings()
        elif choice_lower == "8":
            self.check_dependencies()
        elif choice_lower == "9":
            self.configure_advanced_video_settings()
        return None

    def _main_menu_choice_pairs(self) -> "List[Tuple[str, str]]":
        """(label, value) entries for the flattened top-level menu — single
        source for the grouped questionary menu and the structure test.
        Each action lives in EXACTLY one place (the old layout duplicated
        scan/run/settings/root between the top level and the End-to-End
        submenu — "bloated and confusing", user 2026-06-11). Group
        separators are added by the renderer."""
        return [
            ("🚀  Run / resume end-to-end pipeline", "run"),
            ("🎯  Run ONE folder / image…", "single"),
            ("🔍  Scan / preview cases", "scan"),
            # Dry run intentionally NOT top-level — it's offered at the
            # pre-run approval inside Run (user feedback, round 3).
            # 🔧 not ⚡ — U+26A1 renders as a broken narrow glyph in cmd.exe.
            ("🔧  Quick settings (edit the table above)", "quick"),
            ("⚙️   All settings…", "settings"),
            ("🎬  Manual Kling video tools", "manual"),
            ("🖥️   Launch GUI manual lab", "gui"),
            ("🩺  Maintenance (deps, manifest)…", "maintenance"),
            ("🚪  Quit", "q"),
        ]

    _MAIN_MENU_GROUPS = (
        ("Run", ("run", "single", "scan")),
        ("Settings", ("quick", "settings")),
        ("Tools", ("manual", "gui", "maintenance")),
        (None, ("q",)),
    )

    def _run_configuration_menu_questionary_iteration(self) -> "Optional[str]":
        """One iteration of the branded arrow-key top-level menu."""
        # The MAIN-settings Rich table replaces the old raw key=value blob
        # ("plain text not fitting with the rest" — user, 2026-06-11) and
        # matches the option-1 styling; the header banner already brands the
        # screen, so no second title rule either.
        self.display_header()
        self._render_run_settings_table(caption="🔧 Quick settings edits these")
        print()  # one blank line between the table and the menu
        by_value = {value: label for label, value in self._main_menu_choice_pairs()}
        # Inline hint after the short "Main menu" title (round 6: the hanging
        # hint line read weird here — the quick editor keeps ITS hint line
        # because that title is long); blank spacer before the first group.
        grouped: "List[Any]" = [questionary.Separator(" ")]
        for group_title, values in self._MAIN_MENU_GROUPS:
            if group_title is not None:
                grouped.append(questionary.Separator(f"  ─── {group_title} ───"))
            grouped.extend(questionary.Choice(by_value[v], v) for v in values)
            grouped.append(questionary.Separator(" "))
        grouped.pop()  # no trailing spacer after Quit
        # Plain "Main menu" message — the banner above already carries the
        # app name + version, repeating it here read as clutter (round 3).
        choice = self._q_menu(
            "Main menu",
            grouped,
            instruction="↑/↓ move · Enter to select · Esc to quit",
            show_header=False,
            show_title_rule=False,
        )
        if choice in (None, "q"):
            print("\nGoodbye!")
            sys.exit(0)
        if choice == "run":
            # Flattened run entry: offer the folder picker instead of a dead
            # red "set root first" error when no root is set yet.
            if not self.automation_root_folder:
                self._select_automation_root()
            if self.automation_root_folder:
                self._run_resume_automation()
            return None
        if choice == "single":
            self._run_single_case()
            return None
        if choice == "scan":
            self._scan_automation_cases()
            return None
        if choice == "quick":
            self._quick_edit_settings()
            return None
        if choice == "settings":
            self._settings_hub_menu()
            return None
        if choice == "manual":
            return self._run_manual_kling_menu() or None
        if choice == "gui":
            self.launch_gui()
            return None
        if choice == "maintenance":
            self._maintenance_menu()
            return None
        return None

    def _settings_hub_menu(self) -> None:
        """Every settings surface in ONE hub (questionary path only — the
        legacy numbered menu keeps its original per-option layout)."""
        while True:
            choice = self._q_menu(
                "Settings",
                [
                    ("🔧  Quick settings (main run settings)", "quick"),
                    ("⚙️   Edit ALL automation settings", "all"),
                    ("🎛️   Advanced video/model settings", "advanced"),
                    ("🔑  API keys / provider settings", "keys"),
                    ("📜  View full prompts (selfie + video)", "prompts"),
                    ("🔄  Compare with GUI settings…", "compare"),
                    ("⭐  Reset to recommended pipeline defaults", "reset"),
                    ("↩️   Back", "back"),
                ],
            )
            if choice in (None, "back"):
                return
            if choice == "quick":
                self._quick_edit_settings()
            elif choice == "all":
                self._edit_automation_settings()
            elif choice == "advanced":
                self.configure_advanced_video_settings()
            elif choice == "keys":
                self.configure_api_provider_settings()
            elif choice == "prompts":
                self._show_full_prompts()
                self.pause_review("\nPress Enter to continue...")
            elif choice == "compare":
                self._compare_gui_settings_menu()
            elif choice == "reset":
                self._apply_recommended_automation_defaults()

    def _maintenance_menu(self) -> None:
        """Housekeeping actions that don't belong next to run/settings."""
        while True:
            choice = self._q_menu(
                "Maintenance",
                [
                    ("📦  Dependency check", "deps"),
                    ("📄  Print manifest path", "manifest"),
                    ("↩️   Back", "back"),
                ],
            )
            if choice in (None, "back"):
                return
            if choice == "deps":
                self.check_dependencies()
            elif choice == "manifest":
                manifest_path = self._automation_manifest_path()
                print(f"\nManifest path: {manifest_path if manifest_path else '(set root first)'}")
                self.pause_review("\nPress Enter to continue...")

    def _run_configuration_menu_legacy_iteration(self) -> "Optional[str]":
        """One iteration of the legacy numbered top-level menu (non-TTY / pipe).

        Preserves the paste-a-path shortcut: any non-option input that resolves
        to a directory sets the automation root.
        """
        self.display_header()
        self.display_configuration_menu()
        try:
            choice = input().strip()
        except EOFError:
            # Closed / exhausted stdin (pipe) at the top-level menu: exit cleanly
            # instead of letting EOFError crash out of the while-True loop.
            print("\nGoodbye!")
            sys.exit(0)
        if choice.startswith('"') and choice.endswith('"'):
            choice = choice[1:-1]
        elif choice.startswith("'") and choice.endswith("'"):
            choice = choice[1:-1]
        choice_lower = choice.lower()
        if choice_lower == "q":
            print("\nGoodbye!")
            sys.exit(0)
        if choice_lower in {"1", "2", "3", "4", "5", "6", "7", "8", "9"}:
            return self._dispatch_configuration_choice(choice_lower)
        if choice and Path(choice).exists():
            selected_root = Path(choice)
            if selected_root.is_dir():
                self.automation_root_folder = str(selected_root)
                self.config["automation_root_folder"] = self.automation_root_folder
                self.save_config()
                self._scan_automation_cases()
            else:
                self.print_red(f"Path is not a folder: {choice}")
                self.pause_continue("Press Enter to continue...")
        elif choice:
            self.print_red(f"Path not found: {choice}")
            self.pause_continue("Press Enter to continue...")
        else:
            self.print_yellow("Please enter a valid path or select an option")
            time.sleep(1)
        return None

    def _automation_manifest_path(self, root: Optional[Path] = None) -> Optional[Path]:
        # `root` override: callers that already hold the run root (the
        # single-case flow, whose root is the case folder's parent) must not
        # depend on self.automation_root_folder — a mid-flow root change via
        # quick-edit would silently point the manifest at a DIFFERENT folder
        # than the run's root_dir (adversarial review HIGH, PR #96 round 9).
        base = str(root) if root is not None else self.automation_root_folder
        if not base:
            return None
        raw_manifest_name = str(self.config.get("automation_manifest_name", "automation_manifest.json") or "").strip()
        safe_manifest_name = Path(raw_manifest_name).name if raw_manifest_name else "automation_manifest.json"
        if not safe_manifest_name.endswith(".json"):
            safe_manifest_name = "automation_manifest.json"
        return Path(base) / safe_manifest_name

    def _resolve_provider(self, configured_provider: str) -> str:
        normalized = str(configured_provider or "auto").strip().lower()
        if normalized in {"bfl", "fal"}:
            return normalized
        if str(self.config.get("bfl_api_key", "")).strip():
            return "bfl"
        return "fal"

    def _selfie_model_label_map(self) -> Dict[str, str]:
        return {item.get("endpoint", ""): item.get("label", item.get("endpoint", "")) for item in SelfieGenerator.get_available_models()}

    def _ensure_selfie_prompt_slots(self) -> None:
        prompts = self.config.get("automation_selfie_prompts")
        if not isinstance(prompts, dict):
            prompts = {}
        defaults = merge_automation_defaults({}).get("automation_selfie_prompts", {})
        for i in range(1, _PROMPT_SLOT_COUNT + 1):
            prompts.setdefault(str(i), "")
        if not prompts.get("1"):
            prompts["1"] = defaults.get("1", "")
        if not prompts.get("3"):
            prompts["3"] = defaults.get("3", defaults.get("1", ""))
        self.config["automation_selfie_prompts"] = prompts
        slot = int(self.config.get("automation_selfie_prompt_slot", DEFAULT_AUTOMATION_SELFIE_PROMPT_SLOT))
        if slot < 1 or slot > 10:
            slot = DEFAULT_AUTOMATION_SELFIE_PROMPT_SLOT
        self.config["automation_selfie_prompt_slot"] = slot

    def _get_selected_selfie_prompt(self) -> Tuple[str, str, str]:
        self._ensure_selfie_prompt_slots()
        slot = str(self.config.get("automation_selfie_prompt_slot", DEFAULT_AUTOMATION_SELFIE_PROMPT_SLOT))
        prompt = str(self.config.get("automation_selfie_prompts", {}).get(slot, "") or "").strip()
        if prompt:
            return slot, prompt, f"slot:{slot}"
        defaults = merge_automation_defaults({}).get("automation_selfie_prompts", {})
        slot_default = str(defaults.get(slot, "") or "").strip()
        if slot_default:
            return slot, slot_default, f"default_seeded_slot:{slot}"
        default_prompt = defaults.get("1", "")
        return slot, default_prompt, "default_seeded_prompt"

    def _oldcam_readiness_status(self) -> str:
        repo_root = Path(__file__).resolve().parent
        versions = discover_oldcam_versions(repo_root)
        deps_ok, _deps_err = ensure_oldcam_dependencies()
        if not versions:
            return "unavailable(no version)"
        if not deps_ok:
            return "unavailable(deps)"
        return f"ready({','.join(versions)})"

    def _selected_oldcam_versions(self) -> List[str]:
        """Canonical (normalized list) oldcam selection from config."""
        return normalize_oldcam_versions(self.config.get("automation_oldcam_version", []))

    def _format_oldcam_versions(self, value: Any = _SENTINEL_UNSET) -> str:
        """Human display for an oldcam selection: the EXACT versions that
        will run — ``["all"]`` is expanded to the discovered list so the
        user sees every version before paying for it (2026-06-11 incident:
        a run fanned out to all 10 versions because "all" was invisible).
        """
        if value is _SENTINEL_UNSET:
            selected = self._selected_oldcam_versions()
        else:
            selected = normalize_oldcam_versions(value)
        if not selected:
            return "none selected"
        if selected == ["all"]:
            discovered = discover_oldcam_versions(Path(__file__).resolve().parent)
            if discovered:
                return f"all ({', '.join(discovered)})"
            return "all (none discovered!)"
        return ", ".join(selected)

    def _selected_crush_resolutions(self) -> List[str]:
        """Canonical crush-resolution labels from config.

        Mirrors automation.video_crush.normalize_crush_resolutions: the list
        key wins; the legacy ``automation_crush_enabled`` boolean migrates
        (True → 480p); a brand-new config defaults to 720p ON.
        """
        kwargs = {}
        if "automation_crush_resolutions" in self.config:
            kwargs["resolutions"] = self.config["automation_crush_resolutions"]
        if "automation_crush_enabled" in self.config:
            kwargs["legacy_enabled"] = self.config["automation_crush_enabled"]
        return normalize_crush_resolutions(**kwargs)

    def _format_crush_resolutions(self) -> str:
        """Human display for the crush selection (e.g. ``720p, 480p`` / ``off``)."""
        selected = self._selected_crush_resolutions()
        return ", ".join(selected) if selected else "off"

    def _selected_aa_attacks(self) -> List[str]:
        """Canonical AA attack-pipeline labels from config.

        AA is opt-in (default OFF): unlike crush, when NEITHER key is present we
        return [] rather than normalize_aa_attacks's bare default (['prime']).
        """
        attacks = self.config.get("automation_aa_attacks")
        legacy = self.config.get("automation_aa_enabled")
        if attacks is None and legacy is None:
            return []
        kwargs = {}
        if attacks is not None:
            kwargs["attacks"] = attacks
        if legacy is not None:
            kwargs["legacy_enabled"] = legacy
        return normalize_aa_attacks(**kwargs)

    def _format_aa_attacks(self) -> str:
        """Human display for the AA selection (e.g. ``prime, scenario1`` / ``off``)."""
        selected = self._selected_aa_attacks()
        return ", ".join(selected) if selected else "off"

    def _format_fanout_mode(self) -> str:
        """Human display for the post-processing fan-out mode."""
        from automation.postproc_plan import normalize_mode

        mode = normalize_mode(self.config.get("automation_postproc_fanout_mode"))
        return "powerset (separate + combined)" if mode == "separate_and_combined" else "combined only"

    def _automation_status_lines(self) -> List[str]:
        model_labels = self._selfie_model_label_map()
        selfie_models = [model_labels.get(x, x) for x in list(self.config.get("automation_selfie_models", []))]
        selfie_slot, _selfie_prompt, selfie_prompt_source = self._get_selected_selfie_prompt()
        front_configured = str(self.config.get("automation_front_expand_provider", "auto"))
        selfie_configured = str(self.config.get("automation_selfie_expand_provider", "auto"))
        default_composite = self.config.get("outpaint_composite_mode", "preserve_seamless")
        front_composite = self.config.get("automation_front_expand_composite_mode", default_composite)
        selfie_composite = self.config.get("automation_selfie_expand_composite_mode", default_composite)
        front_provider_resolved = self._resolve_provider(front_configured)
        selfie_provider_resolved = self._resolve_provider(selfie_configured)
        front_status = " ".join(
            [
                f"front mode={self.config.get('automation_front_expand_mode')}",
                f"pct={self.config.get('automation_front_expand_percent', 70)}",
                f"passes={self.config.get('automation_front_expand_passes', 2)}",
                f"provider={front_configured}->{front_provider_resolved}",
                f"composite={front_composite}",
            ]
        )
        selfie_status = " ".join(
            [
                f"selfie expand mode={self.config.get('automation_selfie_expand_mode')}",
                f"pct={self.config.get('automation_selfie_expand_percent', 30)}",
                f"provider={selfie_configured}->{selfie_provider_resolved}",
                f"composite={selfie_composite}",
            ]
        )
        lines = [
            f"root={self.automation_root_folder or '(not set)'} max_cases={self._read_max_cases_setting()}",
            f"keys fal={key_status(self.config, 'falai_api_key')} bfl={key_status(self.config, 'bfl_api_key')}",
            front_status,
            selfie_status,
            f"selfie models={', '.join(selfie_models) if selfie_models else '(none)'} prompt_slot={selfie_slot} prompt_source={selfie_prompt_source}",
            f"similarity_threshold={self.config.get('automation_similarity_threshold', 80)} video_model={(lambda e, d: d or e)(*resolve_cli_video_model(self.config))} kling_prompt_slot={resolve_cli_kling_prompt_slot(self.config, DEFAULT_KLING_PROMPT_SLOT)}",
            f"facetrack_gate={'on' if self.config.get('automation_facetrack_enabled', True) else 'off'} "
            f"min={self.config.get('automation_facetrack_min_pct', 96.0)}% "
            f"mode={'required(fail+skip oldcam)' if self.config.get('automation_facetrack_required', False) else 'advisory(manual_review)'}",
            f"rppg={'ON' if self.config.get('automation_rppg_enabled', False) else 'off'} "
            f"loop={'ON' if self.config.get('automation_loop_enabled', False) else 'off'} "
            f"crush={self._format_crush_resolutions()} "
            f"aa={self._format_aa_attacks()} "
            f"oldcam versions={self._format_oldcam_versions()} required={self.config.get('automation_oldcam_required', False)} readiness={self._oldcam_readiness_status()}",
            f"recommended_defaults_version={self.config.get('automation_recommended_defaults_version', 0)} target={RECOMMENDED_DEFAULTS_VERSION}",
            f"automation_verbose_logging={bool(self.config.get('automation_verbose_logging', self.config.get('verbose_logging', True)))} log_path={resolve_automation_log_path(self.config, self.automation_root_folder)}",
        ]
        return lines

    def _write_recommended_automation_baseline(self) -> str:
        """Write the recommended v7 baseline to the keys the CLI pipeline OWNS:
        the automation_* set plus the per-surface cli_* selection (video model /
        kling prompt slot / duration). NO GUI-shared key is touched here —
        current_model / model_display_name / current_prompt_slot /
        saved_prompts / negative_prompts / cfg_scale_value / lock_end_frame /
        outpaint_composite_mode all stay exactly as the GUI left them.

        Single source shared by the explicit ⭐ reset AND the silent one-time
        startup migration so the two can never drift. Caller saves.
        Returns the max-cases status line for the ⭐ report."""
        valid_max_cases = {"1", "5", "10", "all"}
        current_max_cases = str(self.config.get("automation_max_cases_per_run", "")).strip().lower()
        if current_max_cases in valid_max_cases:
            max_cases_status = f"preserved ({current_max_cases})"
        else:
            self.config["automation_max_cases_per_run"] = "1"
            max_cases_status = "set to 1 (invalid/missing previous value)"

        # v7 (Codex P2, PR #96): the recommended baseline must reset EVERY
        # behavior-affecting stage toggle — a config carrying e.g.
        # automation_selfie_enabled=False from earlier experimentation would
        # otherwise still skip core steps right after "apply recommended
        # defaults" claimed a known-good baseline.
        self.config["automation_front_expand_enabled"] = True
        self.config["automation_extract_enabled"] = True
        self.config["automation_selfie_enabled"] = True
        self.config["automation_selfie_expand_enabled"] = True
        # v7: provider fal for BOTH expand steps ("fal.ai for everything",
        # user mandate 2026-06-11; previously bfl).
        self.config["automation_front_expand_provider"] = "fal"
        self.config["automation_front_expand_mode"] = "percent"
        self.config["automation_front_expand_percent"] = 70
        self.config["automation_front_expand_passes"] = 2
        self.config["automation_front_expand_composite_mode"] = "preserve_seamless"
        self.config["automation_front_edge_seal_enabled"] = False
        self.config["automation_selfie_expand_provider"] = "fal"
        self.config["automation_selfie_expand_mode"] = "percent"
        self.config["automation_selfie_expand_percent"] = 30
        # Ship default for Step 2.5 selfie expand is "none" (raw AI
        # output) — must match the baseline default in
        # automation/config.py + default_config_template.json, otherwise
        # apply_recommended_defaults would silently revert the user's
        # new ship default (CodeRabbit, PR #41).
        self.config["automation_selfie_expand_composite_mode"] = "none"
        self.config["automation_selfie_expand_edge_seal_enabled"] = False
        self.config["automation_selfie_models"] = ["fal-ai/nano-banana-2/edit"]
        self.config["automation_selfie_prompt_slot"] = DEFAULT_AUTOMATION_SELFIE_PROMPT_SLOT
        self._ensure_selfie_prompt_slots()
        defaults = merge_automation_defaults({}).get("automation_selfie_prompts", {})
        self.config["automation_selfie_prompts"]["1"] = defaults.get("1", "")
        self.config["automation_selfie_prompts"]["3"] = defaults.get("3", defaults.get("1", ""))
        # Per-surface CLI selection (split, 2026-06-11): the recommended
        # Kling 2.5 Turbo Standard on prompt slot 4 lands on the cli_* keys
        # so the GUI's manual-lab model/slot selection survives untouched.
        self.config["cli_video_model"] = "fal-ai/kling-video/v2.5-turbo/standard/image-to-video"
        self.config["cli_video_model_display_name"] = "Kling 2.5 Turbo Standard"
        self.config["cli_video_duration"] = 10
        self.config["cli_kling_prompt_slot"] = DEFAULT_KLING_PROMPT_SLOT
        self.config["automation_similarity_threshold"] = 80
        self.config["automation_video_enabled"] = True
        # Face-track gate is DIAGNOSTIC-ONLY and OFF by default. A large
        # balanced corpus showed face-track % does NOT separate Persona
        # PASS from FAIL (the earlier "zero false positives at 96%" was a
        # small-sample artifact, refuted — see docs/analysis/
        # versailles_fail_vs_pass.md "DEFINITIVE LARGE-CORPUS NEGATIVE").
        # Recommended defaults keep it OFF; do not re-enable without a new
        # corpus demonstrating genuine separation.
        self.config["automation_facetrack_enabled"] = False
        self.config["automation_facetrack_min_pct"] = 96.0
        self.config["automation_facetrack_required"] = False
        self.config["automation_facetrack_sample_fps"] = 8.0
        self.config["automation_oldcam_enabled"] = True
        # v7: oldcam v13 only (canonical multi-select list form) — the
        # quick-start "best results" combo is rPPG + oldcam v13.
        self.config["automation_oldcam_version"] = ["v13"]
        self.config["automation_oldcam_required"] = True
        # v7: loop ships OFF (user mandate 2026-06-11) but is now a real
        # pipeline step toggleable from quick settings.
        self.config["automation_loop_enabled"] = False
        # rPPG injection runs Phase E (Kling -> rPPG -> Loop -> Oldcam).
        # v7 flips the recommended GATE to ON (user decision 2026-06-11:
        # "rPPG and oldcam v13 = best results"; a real batch run burned
        # because rPPG silently stayed off). The mode is "iterative" + the
        # three companion flags ON, per PR #43 (friend confirmed iterative
        # is mandatory for prod use). The global GUI default stays opt-in;
        # this preset is CLI-automation only.
        self.config["automation_rppg_enabled"] = RECOMMENDED_RPPG_ENABLED_V7
        self.config["automation_rppg_mode"] = "iterative"
        self.config["automation_rppg_iterate_from_baseline"] = True
        self.config["automation_rppg_skip_diagnosis"] = True
        self.config["automation_rppg_skip_kinematic_gate"] = True
        self.config["automation_rppg_required"] = False
        # Round-3 subagent CRITICAL (PR #54): the v5 -> v6 bump exists
        # SPECIFICALLY to push stride 3 -> 1 to v5 users (quality-first
        # revert of the v2.5 speedup pass). The apply-defaults function
        # must actually write the new stride or v5 users will see
        # version=6 in their config but keep running the degraded
        # stride=3 fast path. (Without this line the whole v6 migration
        # is a no-op for the only key it was bumped for.)
        self.config["automation_rppg_landmark_stride"] = 1
        # Clean *-rppg filename + .metrics.json sidecar by default; the
        # injector's metric-suffix rename is opt-in (see automation/rppg.py
        # finalize_rppg_output).
        self.config["automation_rppg_metrics_in_filename"] = False
        # Legacy per-oldcam rPPG fan-out stays OFF in the recommended
        # baseline (Codex P2, PR #96 — a leftover opt-in would silently
        # multiply rPPG runtime per oldcam version).
        self.config["automation_rppg_per_oldcam_fanout"] = False
        self.config["automation_recommended_defaults_version"] = RECOMMENDED_DEFAULTS_VERSION
        return max_cases_status

    def _auto_upgrade_recommended_defaults(self) -> None:
        """One-time SILENT upgrade to the current recommended pipeline baseline.

        Runs at interactive menu startup only — never on the headless --batch
        path, where a scheduled/cron run's behavior must not flip mid-schedule
        (headless users pass explicit override flags instead). Replaces the
        old jargon tip ("upgrade v1 → v7" meant nothing to end users; mandate
        2026-06-11: apply it automatically and ship fresh).

        GUI-safety contract: only automation_* + cli_* keys are written. The
        shared Kling prompt slots (1/4) are seeded ONLY where empty — prompt
        text is shared with the GUI by design and user-authored text survives.
        """
        try:
            current_version = int(self.config.get("automation_recommended_defaults_version", 0) or 0)
        except (TypeError, ValueError):
            current_version = 0
        if current_version >= RECOMMENDED_DEFAULTS_VERSION:
            # Already current — but a config stamped v7 BEFORE the
            # per-surface split has no cli_* keys, so its pipeline would
            # keep tracking the GUI selection through the resolver
            # fallback (Codex P2). Adopt the GUI values once —
            # behavior-preserving at seed time, independent afterwards.
            self._seed_cli_selection_keys_if_absent()
            return
        self._write_recommended_automation_baseline()
        saved_prompts = self.config.get("saved_prompts")
        if not isinstance(saved_prompts, dict):
            saved_prompts = {}
            self.config["saved_prompts"] = saved_prompts
        negative_prompts = self.config.get("negative_prompts")
        if not isinstance(negative_prompts, dict):
            negative_prompts = {}
            self.config["negative_prompts"] = negative_prompts
        for slot in ("1", "4"):
            if not str(saved_prompts.get(slot, "") or "").strip():
                saved_prompts[slot] = RECOMMENDED_KLING_PROMPT_SLOT_1
            if not str(negative_prompts.get(slot, "") or "").strip():
                negative_prompts[slot] = RECOMMENDED_KLING_NEGATIVE_SLOT_1
        self.save_config()
        _RICH_CONSOLE.print(
            "[dim]Pipeline settings were upgraded to the latest recommended baseline "
            "(GUI settings untouched).[/dim]"
        )

    def _seed_cli_selection_keys_if_absent(self) -> None:
        """One-time adoption of the GUI selection as the CLI's starting point.

        For configs already at the current recommended version when the
        per-surface split shipped: the resolvers were falling back to these
        exact GUI values, so seeding them is a no-op for behavior — but from
        now on a GUI model change no longer silently retargets the pipeline.
        Saves only when something was seeded."""
        c = self.config
        changed = False
        if not str(c.get("cli_video_model") or "").strip():
            gui_endpoint = str(c.get("current_model") or "").strip()
            if gui_endpoint:
                c["cli_video_model"] = gui_endpoint
                c["cli_video_model_display_name"] = str(c.get("model_display_name") or "")
                changed = True
        if c.get("cli_kling_prompt_slot") in (None, ""):
            c["cli_kling_prompt_slot"] = c.get("current_prompt_slot", DEFAULT_KLING_PROMPT_SLOT)
            changed = True
        if c.get("cli_video_duration") in (None, ""):
            c["cli_video_duration"] = c.get("video_duration", 10)
            changed = True
        if changed:
            self.save_config()

    def _apply_recommended_automation_defaults(self) -> None:
        # Explicit ⭐ reset: the automation/cli baseline PLUS the shared video
        # knobs (Kling prompt slots 1/4 text, negative prompts, cfg scale,
        # end-frame lock, outpaint composite). Confirm before clobbering a
        # customized config (E1). default=True so the headless/test path
        # (which chose this action deliberately) proceeds, while a TTY user
        # gets a y/N.
        if not self._confirm(
            "Reset to recommended pipeline defaults? This overwrites the automation "
            "settings AND the shared Kling prompt slots 1/4 + video motion knobs.",
            default=True,
        ):
            self.print_yellow("Recommended defaults not applied.")
            return
        before = {
            "front": (
                self.config.get("automation_front_expand_provider"),
                self.config.get("automation_front_expand_mode"),
                self.config.get("automation_front_expand_percent"),
                self.config.get("automation_front_expand_passes"),
                self.config.get("automation_front_expand_composite_mode"),
            ),
            "selfie_expand": (
                self.config.get("automation_selfie_expand_provider"),
                self.config.get("automation_selfie_expand_mode"),
                self.config.get("automation_selfie_expand_percent"),
                self.config.get("automation_selfie_expand_composite_mode"),
            ),
            "selfie_models": list(self.config.get("automation_selfie_models", [])),
            "video_model": (lambda e, d: d or e)(*resolve_cli_video_model(self.config)),
            "selfie_prompt_slot": self.config.get("automation_selfie_prompt_slot", DEFAULT_AUTOMATION_SELFIE_PROMPT_SLOT),
            "kling_prompt_slot": resolve_cli_kling_prompt_slot(self.config, DEFAULT_KLING_PROMPT_SLOT),
            "oldcam": (self.config.get("automation_oldcam_version"), self.config.get("automation_oldcam_required", False)),
            "rppg": bool(self.config.get("automation_rppg_enabled", False)),
            "loop": bool(self.config.get("automation_loop_enabled", False)),
            "max_cases": self._read_max_cases_setting(),
        }

        max_cases_status = self._write_recommended_automation_baseline()
        # Shared video knobs the EXPLICIT reset still normalizes (the silent
        # startup migration deliberately leaves these alone — they're shared
        # with the GUI, and the confirm above names them).
        self.config["outpaint_composite_mode"] = "preserve_seamless"
        saved_prompts = self.config.get("saved_prompts")
        if not isinstance(saved_prompts, dict):
            saved_prompts = {}
        saved_prompts["1"] = RECOMMENDED_KLING_PROMPT_SLOT_1
        saved_prompts["4"] = RECOMMENDED_KLING_PROMPT_SLOT_1
        self.config["saved_prompts"] = saved_prompts
        # Pair the minimal-motion positive with the recommended negative
        # on the same slots (the dispatcher drops it for models that
        # don't accept negative_prompt — o3 / seedance).
        negative_prompts = self.config.get("negative_prompts")
        if not isinstance(negative_prompts, dict):
            negative_prompts = {}
        negative_prompts["1"] = RECOMMENDED_KLING_NEGATIVE_SLOT_1
        negative_prompts["4"] = RECOMMENDED_KLING_NEGATIVE_SLOT_1
        self.config["negative_prompts"] = negative_prompts
        # Motion knobs: stricter prompt adherence + mechanical
        # return-to-pose via the end-frame lock.
        self.config["cfg_scale_value"] = 0.7
        self.config["lock_end_frame"] = True
        self.save_config()

        print("\nApplied recommended automation defaults (v7).")
        print("Before -> After")
        print(
            "  front expand: "
            f"{before['front'][0]} / {before['front'][1]} / {before['front'][2]} / "
            f"passes={before['front'][3]} / {before['front'][4]} "
            "-> fal / percent / 70 / passes=2 / preserve_seamless"
        )
        print(f"  selfie expand: {before['selfie_expand'][0]} / {before['selfie_expand'][1]} / {before['selfie_expand'][2]} / {before['selfie_expand'][3]} -> fal / percent / 30 / none")
        print(f"  selfie model: {before['selfie_models']} -> Nano Banana 2 Edit")
        print(f"  video model: {before['video_model']} -> Kling 2.5 Turbo Standard")
        print(f"  selfie prompt slot: {before['selfie_prompt_slot']} -> 3")
        print(f"  Kling prompt slot: {before['kling_prompt_slot']} -> 4")
        print(
            f"  oldcam: {self._format_oldcam_versions(before['oldcam'][0])} / "
            f"{'required' if before['oldcam'][1] else 'optional'} -> v13 / required"
        )
        print(f"  rPPG: {'ON' if before.get('rppg') else 'off'} -> {'ON' if RECOMMENDED_RPPG_ENABLED_V7 else 'off'} (iterative + from-baseline + skip-diag/kinematic)")
        print(f"  loop: {'ON' if before.get('loop') else 'off'} -> off")
        print(f"  max cases per run: {before['max_cases']} -> {self._read_max_cases_setting()} ({max_cases_status})")
        print("\nCurrent recommended state:")
        print("  front expand: fal / percent / 70 / preserve_seamless")
        print("  selfie expand: fal / percent / 30 / none")
        print("  selfie model: Nano Banana 2 Edit")
        print("  video model: Kling 2.5 Turbo Standard")
        print("  selfie prompt slot: 3")
        print("  Kling prompt slot: 4")
        print(f"  oldcam: v13 / required   ·   rPPG: {'ON' if RECOMMENDED_RPPG_ENABLED_V7 else 'off'}   ·   loop: off")
        print(f"  max cases per run: {self._read_max_cases_setting()}")
        self.pause_continue("\nPress Enter to continue...")

    def _display_automation_menu(self):
        self.display_header()
        self.print_magenta("═" * 79)
        self.print_magenta("                     END-TO-END AUTO PIPELINE")
        self.print_magenta("═" * 79)
        print()
        current_root = self.automation_root_folder or "(not set)"
        print(f"  Root folder: \033[97m{current_root}\033[0m")
        for line in self._automation_status_lines():
            print(f"  {line}")
        current_version = int(self.config.get("automation_recommended_defaults_version", 0) or 0)
        if current_version < RECOMMENDED_DEFAULTS_VERSION:
            print(f"  \033[93mRecommendation:\033[0m apply recommended defaults (target version {RECOMMENDED_DEFAULTS_VERSION}).")
        print()
        print("  \033[93m1\033[0m   Select automation root folder")
        print("  \033[93m2\033[0m   Scan / preview cases")
        print("  \033[93m3\033[0m   Apply recommended automation defaults")
        print("  \033[93m4\033[0m   Edit automation settings")
        print("  \033[93m5\033[0m   Dry run")
        print("  \033[93m6\033[0m   Run / resume automation")
        print("  \033[93m7\033[0m   Print manifest path")
        print("  \033[93m8\033[0m   Quick edit main settings")
        print("  \033[93m9\033[0m   View full prompts (selfie + video)")
        print("  \033[93m0\033[0m   Back")
        print()
        print("\033[92m➤ Select option:\033[0m ", end="", flush=True)

    def _select_automation_root(self):
        logging.info("automation_root_select_start")
        # Questionary path (interactive TTY): reuse the shared _qs_directory
        # picker, which already offers native folder-picker / type / keep. The
        # legacy numbered browse/type walker stays as the non-TTY fallback so
        # CI / piped stdin keep working.
        if not self._use_legacy_prompt_ui():
            try:
                picked = self._qs_directory(
                    "Select automation root folder:",
                    self.automation_root_folder,
                    "Select Automation Root Folder",
                )
            except (KeyboardInterrupt, EOFError, _QuestionarySectionAbort):
                picked = None
            if not picked:
                self.print_yellow("Automation root unchanged.")
                return
            self._commit_automation_root(picked)
            return
        return self._select_automation_root_legacy()

    def _commit_automation_root(self, selected_path: str) -> None:
        """Validate + persist a chosen automation root, then scan cases."""
        selected = Path(selected_path)
        if not selected.exists() or not selected.is_dir():
            self.print_red("Invalid folder path.")
            logging.warning("automation_root_select_invalid path=%s", selected)
            self.pause_continue("Press Enter to continue...")
            return
        self.automation_root_folder = str(selected)
        logging.info("automation_root_select_success path=%s", self.automation_root_folder)
        self.config["automation_root_folder"] = self.automation_root_folder
        self.save_config()
        self.print_yellow(f"Automation root set: {self.automation_root_folder}")
        self._scan_automation_cases()

    def _select_automation_root_legacy(self):
        print("\nSelect automation root:")
        print("  1) Browse for folder (recommended)")
        print("  2) Type folder path")
        choice = self._safe_input("Choose option [1/2, default 1]: ").strip()
        selected_path: Optional[str] = None
        use_browse = choice in {"", "1"}
        logging.info("automation_root_select_mode use_browse=%s choice=%s", use_browse, choice or "<default>")
        if use_browse:
            logging.info("automation_root_picker_browse_attempt")
            try:
                logging.info(
                    "automation_root_picker_backend backend=%s",
                    "osascript" if sys.platform == "darwin" else "tk",
                )
                selected_path = select_directory_cli_safe(title="Select Automation Root Folder")
            except Exception as exc:
                self.print_yellow(f"Folder picker unavailable ({exc}). Falling back to typed path.")
                logging.warning("automation_root_picker_browse_error error=%s", exc, exc_info=True)
                selected_path = None
            if selected_path is None:
                self.print_yellow("Folder picker canceled or unavailable. Enter a path manually.")
                logging.info("automation_root_picker_browse_canceled_or_unavailable")
            if not selected_path:
                logging.info("automation_root_typed_fallback_prompt")
                raw = self._safe_input("Enter automation root folder path (leave blank to cancel): ").strip()
                if not raw:
                    self.print_yellow("Automation root selection canceled.")
                    logging.info("automation_root_select_canceled")
                    return
                if raw.startswith('"') and raw.endswith('"'):
                    raw = raw[1:-1]
                elif raw.startswith("'") and raw.endswith("'"):
                    raw = raw[1:-1]
                selected_path = raw
        else:
            logging.info("automation_root_typed_primary_prompt")
            raw = self._safe_input("Enter automation root folder path (leave blank to cancel): ").strip()
            if not raw:
                self.print_yellow("Automation root selection canceled.")
                logging.info("automation_root_select_canceled")
                return
            if raw.startswith('"') and raw.endswith('"'):
                raw = raw[1:-1]
            elif raw.startswith("'") and raw.endswith("'"):
                raw = raw[1:-1]
            selected_path = raw

        self._commit_automation_root(selected_path)

    def _normalize_max_cases(self, value: Any) -> Optional[int]:
        raw = str(value).strip().lower()
        if raw == "all":
            return None
        if raw.isdigit():
            parsed = int(raw)
            if parsed in {1, 5, 10}:
                return parsed
        return 5

    def _read_max_cases_setting(self) -> str:
        raw = str(self.config.get("automation_max_cases_per_run", 5)).strip().lower()
        if raw in {"1", "5", "10", "all"}:
            return raw
        return "5"

    def _planned_action_for_case(self, case_entry: Dict[str, Any], existing: Any, is_complete: bool) -> str:
        status = str(case_entry.get("status", "pending"))
        if is_complete and self.config.get("automation_skip_completed", True):
            return "skip_complete"
        if status == "manual_review":
            gate_error = str(case_entry.get("steps", {}).get("similarity_gate", {}).get("error", "") or "")
            if "similarity unavailable" in gate_error.lower():
                return "run_pending"
            return "manual_review"
        if status == "failed":
            return "failed"
        if self.config.get("automation_skip_if_video_exists", True) and existing.video_candidate:
            return "skip_video_exists"
        if self.config.get("automation_skip_if_selfie_exists", True) and existing.selfie_candidate:
            return "skip_selfie_exists"
        return "run_pending"

    def _collect_case_snapshot(
        self,
        records: List[Any],
        manifest: Optional[AutomationManifest],
    ) -> Tuple[List[Dict[str, Any]], Dict[str, int], List[Any]]:
        rows: List[Dict[str, Any]] = []
        counts = {
            "discovered": len(records),
            "completed_total": 0,
            "skipped_complete": 0,
            "pending": 0,
            "manual_review": 0,
            "failed": 0,
            "existing_videos_selfies": 0,
            "will_run": 0,
        }
        runnable: List[Any] = []
        for record in records:
            case_entry = manifest.data.get("cases", {}).get(record.relative_key, {}) if manifest else {}
            prior_front = case_entry.get("front_path") if isinstance(case_entry, dict) else None
            # Discovery now selects a DIFFERENT front image than the one the
            # recorded outputs came from: the case must flow into the runner
            # (whose _reset_case_if_front_changed resets it) instead of being
            # filtered out here as skip_complete/manual_review/failed with
            # stale wrong-source outputs (codex P1, PR #96 round 9).
            front_changed = bool(prior_front and str(prior_front) != str(record.front_path))
            existing = detect_existing_outputs(record.case_dir)
            is_complete = bool(
                manifest
                and not front_changed
                and case_entry.get("status") == "complete"
                and manifest.case_is_complete_and_valid(record.relative_key)
            )
            if existing.video_candidate or existing.selfie_candidate:
                counts["existing_videos_selfies"] += 1
            if front_changed:
                planned = "run_front_changed"
            else:
                planned = self._planned_action_for_case(case_entry, existing, is_complete)
            if is_complete:
                counts["completed_total"] += 1
            if planned == "skip_complete":
                counts["skipped_complete"] += 1
            elif planned == "manual_review":
                counts["manual_review"] += 1
            elif planned == "failed":
                counts["failed"] += 1
            elif planned in {"run_pending", "run_front_changed", "skip_video_exists", "skip_selfie_exists"}:
                counts["pending"] += 1
                runnable.append(record)
            row = {
                "case": record.relative_key,
                "front": record.front_path.name,
                "front_expanded": "yes" if existing.front_expanded else "-",
                "extracted": "yes" if existing.extracted else "-",
                "selfie": "yes" if existing.selfie_candidate else "-",
                "video": "yes" if existing.video_candidate else "-",
                "manifest_status": str(case_entry.get("status", "pending")),
                "planned": planned,
            }
            rows.append(row)

        max_cases = self._normalize_max_cases(self._read_max_cases_setting())
        capped = runnable[:max_cases] if max_cases is not None else runnable
        counts["will_run"] = len(capped)
        return rows, counts, capped

    def _scan_automation_cases(self):
        if not self.automation_root_folder:
            self.print_red("Set automation root folder first.")
            self.pause_review("Press Enter to continue...")
            return
        root = Path(self.automation_root_folder)
        if not root.exists():
            self.print_red("Automation root path does not exist.")
            self.pause_review("Press Enter to continue...")
            return
        records = discover_case_folders(
            root,
            self.config.get("automation_front_names", []),
            front_globs=self.config.get("automation_front_globs", []),
        )
        # read_only: scan is a PREVIEW — it must never rename a corrupt
        # manifest aside (Codex P2, PR #96 round 4).
        manifest = AutomationManifest.load_if_exists(self._automation_manifest_path(), read_only=True)
        rows, counts, _ = self._collect_case_snapshot(records, manifest)
        interactive = not self._use_legacy_prompt_ui()
        if interactive:
            self.display_header()
            table = self._styled_table("🔍  Scan preview", show_header=True)
        else:
            table = Table(title="Automation Scan Preview")
        table.add_column("Case")
        table.add_column("Front")
        table.add_column("front-expanded")
        table.add_column("extracted")
        table.add_column("selfie")
        table.add_column("video")
        table.add_column("manifest status")
        table.add_column("planned action")
        for row in rows[:60]:
            # Folder + file names are user-controlled — escape so "case[A]" /
            # "front[1].jpg" display instead of crashing the table render.
            table.add_row(
                _rich_markup_escape(str(row["case"])),
                _rich_markup_escape(str(row["front"])),
                row["front_expanded"],
                row["extracted"],
                row["selfie"],
                row["video"],
                row["manifest_status"],
                row["planned"],
            )
        _RICH_CONSOLE.print(table)
        if len(records) > 60:
            print(f"\nShowing first 60/{len(records)} cases.")
        else:
            print(f"\nDiscovered {len(records)} case folders.")
        if interactive:
            totals = self._styled_table("Σ  Totals")
            totals.add_column("k", style="cyan", no_wrap=True)
            totals.add_column("v")
            totals.add_row("Discovered", str(counts["discovered"]))
            totals.add_row("Completed total", str(counts["completed_total"]))
            totals.add_row("Skipped complete", str(counts["skipped_complete"]))
            totals.add_row("Pending / runnable", str(counts["pending"]))
            totals.add_row("Will run this batch", f"[bold green]{counts['will_run']}[/bold green]")
            totals.add_row("Manual review / failed", f"{counts['manual_review']} / {counts['failed']}")
            totals.add_row("Existing videos+selfies", str(counts["existing_videos_selfies"]))
            totals.add_row("Max cases per run", str(self._read_max_cases_setting()))
            _RICH_CONSOLE.print(totals)
            self.pause_review("\nPress Enter to continue...")
            return
        print("\nTotals:")
        print(f"  discovered: {counts['discovered']}")
        print(f"  completed total: {counts['completed_total']}")
        print(f"  skipped complete: {counts['skipped_complete']}")
        print(f"  pending/runnable: {counts['pending']}")
        print(f"  will run this batch: {counts['will_run']}")
        print(f"  manual review: {counts['manual_review']}")
        print(f"  failed: {counts['failed']}")
        print(f"  existing videos/selfies: {counts['existing_videos_selfies']}")
        print(f"  max cases per run: {self._read_max_cases_setting()}")
        self.pause_review("\nPress Enter to continue...")

    def _edit_automation_settings(self):
        """Dispatch: questionary-based UX when available + interactive TTY,
        otherwise fall back to the legacy linear input() walker so tests and
        non-TTY callers (CI, piped stdin) keep working."""
        if self._use_legacy_prompt_ui():
            return self._edit_automation_settings_legacy()
        try:
            return self._edit_automation_settings_questionary()
        except (KeyboardInterrupt, EOFError):
            # User bailed out of questionary; persist whatever they edited so far.
            self.save_config()
            print("\nSettings editor cancelled. Edits to this point have been saved.")
        except Exception as exc:
            # Last-resort safety net: if questionary blows up unexpectedly
            # (terminal incompatibility, etc.), drop to the legacy path so the
            # user can still configure things. Log the full traceback so we
            # don't silently swallow real bugs or environment issues.
            logging.getLogger(__name__).exception(
                "questionary settings editor failed; falling back to legacy walker"
            )
            self.print_red(f"Interactive settings UI failed ({exc}); falling back to legacy walker.")
            return self._edit_automation_settings_legacy()

    def _edit_automation_settings_legacy(self):
        def _ask(prompt: str, key: str, cast_fn, validator=None):
            current = self.config.get(key)
            raw = self._safe_input(f"{prompt} (current: {current}) [Enter keep]: ").strip()
            if not raw:
                return
            try:
                value = cast_fn(raw)
                if validator and not validator(value):
                    raise ValueError("validation failed")
                self.config[key] = value
            except Exception:
                self.print_red(f"Invalid value for {key}. Keeping previous value.")

        def _ask_choice(prompt: str, key: str, choices: list):
            current = str(self.config.get(key))
            raw = self._safe_input(f"{prompt} {choices} (current: {current}) [Enter keep]: ").strip().lower()
            if not raw:
                return
            if raw not in choices:
                self.print_red(f"Invalid choice for {key}.")
                return
            self.config[key] = raw

        def _ask_bool(prompt: str, key: str):
            current = bool(self.config.get(key, False))
            raw = self._safe_input(f"{prompt} [y/n] (current: {'y' if current else 'n'}) [Enter keep]: ").strip().lower()
            if not raw:
                return
            if raw in {"y", "yes", "1", "true"}:
                self.config[key] = True
            elif raw in {"n", "no", "0", "false"}:
                self.config[key] = False
            else:
                self.print_red(f"Invalid boolean for {key}.")

        print("\nAutomation Settings Editor (Grouped)")
        print("Press Enter on any prompt to keep current value.\n")

        print("[Discovery]")
        raw_root = self._safe_input(f"Automation root path (current: {self.automation_root_folder or '(not set)'}) [Enter keep]: ").strip()
        if raw_root:
            root_path = Path(raw_root)
            if root_path.exists() and root_path.is_dir():
                self.automation_root_folder = str(root_path)
                self.config["automation_root_folder"] = self.automation_root_folder
            else:
                self.print_red("Root path invalid; keeping previous value.")
        _ask(
            "Manifest filename",
            "automation_manifest_name",
            str,
            lambda v: len(v) > 0 and v.endswith(".json") and Path(v).name == v,
        )
        max_cases_raw = self._safe_input(
            f"Max cases per run [1/5/10/all] (current: {self._read_max_cases_setting()}) [Enter keep]: "
        ).strip().lower()
        if max_cases_raw:
            if max_cases_raw in {"1", "5", "10", "all"}:
                self.config["automation_max_cases_per_run"] = max_cases_raw
            else:
                self.print_red("Invalid max cases value. Keeping previous value.")

        print("\n[Discovery Flags]")
        _ask_bool("Skip completed", "automation_skip_completed")
        _ask_bool("Skip if selfie exists", "automation_skip_if_selfie_exists")
        _ask_bool("Skip if video exists", "automation_skip_if_video_exists")
        _ask_bool("Allow reprocess", "automation_allow_reprocess")
        _ask_choice("Reprocess mode", "automation_reprocess_mode", _REPROCESS_MODE_OPTIONS)

        print("\n[Front Expansion]")
        _ask_bool("Front expand enabled", "automation_front_expand_enabled")
        _ask_choice("Front expand provider", "automation_front_expand_provider", _EXPAND_PROVIDER_OPTIONS)
        _ask_choice("Front expand mode", "automation_front_expand_mode", _EXPAND_MODE_OPTIONS)
        _ask_choice("Front composite mode", "automation_front_expand_composite_mode", _COMPOSITE_MODE_OPTIONS)
        _ask("Front expand percent", "automation_front_expand_percent", int, lambda v: v >= 0)
        _ask("Front expand passes [1|2]", "automation_front_expand_passes", int, lambda v: v in {1, 2})
        _ask_bool("Front edge seal enabled", "automation_front_edge_seal_enabled")
        _ask("Front edge seal px", "automation_front_edge_seal_px", int, lambda v: v >= 0)
        _ask("Front output name", "automation_front_output_name", str, lambda v: len(v) > 0)

        print("\n[Portrait Extraction / Selfie / Similarity]")
        _ask_bool("Portrait extraction enabled", "automation_extract_enabled")
        _ask("Extract output name", "automation_extract_output_name", str, lambda v: len(v) > 0)
        _ask("Crop multiplier", "automation_crop_multiplier", float, lambda v: v > 0)
        _ask_bool("Selfie generation enabled", "automation_selfie_enabled")
        current_models = list(self.config.get("automation_selfie_models", []))
        print("Selfie model selection:")
        print("  1) Nano Banana 2 Edit")
        print("  2) GPT Image 2 Edit")
        print("  3) Both")
        print("  4) Custom endpoints")
        model_choice = self._safe_input(f"Choose model set (current: {current_models}) [Enter keep]: ").strip()
        if model_choice == "1":
            self.config["automation_selfie_models"] = ["fal-ai/nano-banana-2/edit"]
        elif model_choice == "2":
            self.config["automation_selfie_models"] = ["openai/gpt-image-2/edit"]
        elif model_choice == "3":
            self.config["automation_selfie_models"] = ["fal-ai/nano-banana-2/edit", "openai/gpt-image-2/edit"]
        elif model_choice == "4":
            models_raw = self._safe_input("Custom selfie model endpoints comma-separated: ").strip()
            models = [m.strip() for m in models_raw.split(",") if m.strip()]
            if models:
                self.config["automation_selfie_models"] = models
        _ask_choice("Selfie model policy", "automation_selfie_model_policy", ["first_pass", "all"])
        _ask("Max attempts per model", "automation_selfie_max_attempts_per_model", int, lambda v: v > 0)
        _ask("Similarity threshold", "automation_similarity_threshold", int, lambda v: 0 <= v <= 100)
        _ask("Selfie width (px, 864=3:4)", "automation_selfie_width", int, lambda v: v > 0)
        _ask("Selfie height (px, 1152=3:4)", "automation_selfie_height", int, lambda v: v > 0)
        self._ensure_selfie_prompt_slots()
        current_slot = int(self.config.get("automation_selfie_prompt_slot", DEFAULT_AUTOMATION_SELFIE_PROMPT_SLOT))
        current_prompt = str(self.config.get("automation_selfie_prompts", {}).get(str(current_slot), "") or "")
        print(f"Selfie prompt slot: {current_slot}")
        print(f"Current selfie prompt preview: {(current_prompt[:120] + '...') if len(current_prompt) > 120 else current_prompt}")
        slot_raw = self._safe_input("Switch selfie prompt slot [1-10, Enter keep]: ").strip()
        if slot_raw.isdigit() and 1 <= int(slot_raw) <= 10:
            self.config["automation_selfie_prompt_slot"] = int(slot_raw)
            current_slot = int(slot_raw)
        edit_current = self._safe_input("Edit active selfie prompt now? [y/N]: ").strip().lower()
        if edit_current in {"y", "yes"}:
            new_prompt = self._safe_input("Enter selfie prompt text: ").strip()
            if new_prompt:
                self.config["automation_selfie_prompts"][str(current_slot)] = new_prompt
        reset_current = self._safe_input("Reset active selfie slot to default prompt? [y/N]: ").strip().lower()
        if reset_current in {"y", "yes"}:
            defaults = merge_automation_defaults({}).get("automation_selfie_prompts", {})
            self.config["automation_selfie_prompts"][str(current_slot)] = defaults.get(
                str(current_slot), defaults.get("1", "")
            )

        print("\n[Selfie Expansion / Video / Loop-Oldcam]")
        _ask_bool("Selfie expansion enabled", "automation_selfie_expand_enabled")
        _ask_choice("Selfie expand provider", "automation_selfie_expand_provider", _EXPAND_PROVIDER_OPTIONS)
        _ask_choice("Selfie expand mode", "automation_selfie_expand_mode", _SELFIE_EXPAND_MODE_OPTIONS)
        _ask_choice("Selfie composite mode", "automation_selfie_expand_composite_mode", _COMPOSITE_MODE_OPTIONS)
        _ask("Selfie expand percent", "automation_selfie_expand_percent", int, lambda v: v >= 0)
        _ask_bool("Video generation enabled", "automation_video_enabled")
        _ask("Video aspect ratio", "automation_video_aspect_ratio", str, lambda v: ":" in v)
        _ask_bool("Use existing video prompt", "automation_video_use_existing_prompt")
        print("\n[Face-Track Gate]  (DIAGNOSTIC-ONLY — OFF by default)")
        print("  A large balanced corpus showed face-track % does NOT separate")
        print("  Persona PASS from FAIL (earlier '96% zero-false-positive' claim")
        print("  was a small-sample artifact, refuted — see docs/analysis/")
        print("  versailles_fail_vs_pass.md). Leave disabled unless your own")
        print("  current labelled corpus validates a safe threshold. When")
        print("  enabled: advisory routes to manual_review; 'required' hard-fails")
        print("  and skips oldcam on a sub-threshold source.")
        _ask_bool("Face-track gate enabled", "automation_facetrack_enabled")
        _ask("Face-track min track %", "automation_facetrack_min_pct", float, lambda v: 0.0 <= v <= 100.0)
        _ask_bool("Face-track required (sub-threshold => fail+skip oldcam)", "automation_facetrack_required")
        _ask("Face-track sample fps", "automation_facetrack_sample_fps", float, lambda v: 1.0 <= v <= 30.0)
        _ask_bool("Oldcam enabled", "automation_oldcam_enabled")
        # Multi-select-aware free-text prompt: accepts a single version
        # ("v13"), a comma list ("v13,v24"), "all", or "none". Empty input
        # keeps current — existing positional test sequences stay valid.
        current_oldcam = self._format_oldcam_versions()
        raw_oldcam = self._safe_input(
            f"Oldcam versions (e.g. v13 or v13,v24 or all or none) (current: {current_oldcam}) [Enter keep]: "
        ).strip()
        if raw_oldcam:
            requested = normalize_oldcam_versions(raw_oldcam)
            valid = set(_OLDCAM_VERSION_OPTIONS) | set(discover_oldcam_versions(Path(__file__).resolve().parent))
            unknown = [v for v in requested if v != "all" and v not in valid]
            if unknown:
                self.print_red(f"Unknown oldcam version(s): {', '.join(unknown)}. Keeping previous value.")
            else:
                self.config["automation_oldcam_version"] = requested
        _ask_bool("Oldcam required", "automation_oldcam_required")
        _ask_bool("rPPG injection enabled (runs LAST; sub-perceptual pulse; untested direction, off by default)", "automation_rppg_enabled")
        _ask_bool("rPPG required (no output => fail+skip case)", "automation_rppg_required")
        _ask_bool("rPPG metrics in filename (off => clean *-rppg name + .metrics.json sidecar)", "automation_rppg_metrics_in_filename")
        _ask_bool("Loop video (seamless ping-pong, runs before oldcam)", "automation_loop_enabled")
        _ask_bool("Automation verbose logging", "automation_verbose_logging")
        _ask("Automation log max bytes", "automation_log_max_bytes", int, lambda v: v > 0)
        _ask("Automation log backup count", "automation_log_backup_count", int, lambda v: v >= 1)

        self.save_config()
        self.pause_review("Settings saved. Press Enter to continue...")

    def _edit_automation_settings_quick(self):
        """Backwards-compatible alias for older tests/callers."""
        self._edit_automation_settings()

    # ── Questionary-based settings editor (interactive sectioned UX) ──

    def _edit_automation_settings_questionary(self):
        """Sectioned, scrollable settings editor.

        Replaces the 44-prompt linear walker. The user picks a section,
        edits only the fields in that section, and returns to the section
        list — no need to Enter through every other setting.
        """
        assert questionary is not None  # for type-checkers; dispatch already gated

        # Banner shown once when the editor opens. Keeps the section list
        # visually grounded vs. just a bare select prompt.
        _RICH_CONSOLE.print(
            Panel.fit(
                "[bold cyan]Automation Settings[/bold cyan]\n"
                "[dim]Pick a section to edit. Each section only asks about its own fields.[/dim]",
                border_style="cyan",
            )
        )

        # Section → handler map. Defined once outside the while-loop so it's
        # not rebuilt on every section pick.
        section_handlers = {
            "paths": self._qs_section_paths,
            "run": self._qs_section_run,
            "front": self._qs_section_front_expand,
            "portrait": self._qs_section_portrait,
            "selfie": self._qs_section_selfie,
            "selfie_expand": self._qs_section_selfie_expand,
            "video": self._qs_section_video,
            "facetrack": self._qs_section_facetrack,
            "oldcam": self._qs_section_oldcam,
            "logging": self._qs_section_logging,
        }

        cancelled = False
        while True:
            summary = self._questionary_section_summary()
            choice = questionary.select(
                "Edit which section?",
                qmark="◆",
                instruction="(↑/↓ move · Enter pick · Ctrl-C abort)",
                choices=[
                    questionary.Choice(f"📁 Paths           {summary['paths']}", "paths"),
                    questionary.Choice(f"▶  Run scope       {summary['run']}", "run"),
                    questionary.Choice(f"🖼  Front expand    {summary['front']}", "front"),
                    questionary.Choice(f"👤 Portrait crop   {summary['portrait']}", "portrait"),
                    questionary.Choice(f"✨ Selfie gen      {summary['selfie']}", "selfie"),
                    questionary.Choice(f"➕ Selfie expand   {summary['selfie_expand']}", "selfie_expand"),
                    questionary.Choice(f"🎬 Video           {summary['video']}", "video"),
                    questionary.Choice(f"🎯 Face-track gate {summary['facetrack']}", "facetrack"),
                    questionary.Choice(f"📼 Oldcam          {summary['oldcam']}", "oldcam"),
                    questionary.Choice(f"🪵 Logging         {summary['logging']}", "logging"),
                    questionary.Separator("─" * 50),
                    questionary.Choice("👁  View / edit ALL settings (full table)", "_view"),
                    questionary.Choice("💾 Save and return", "_done"),
                ],
                # Default to the first real section ("paths") rather than
                # "Save and return" so an accidental Enter doesn't exit the
                # editor before the user has picked a section to edit.
                default="paths",
                style=KLING_QUESTIONARY_STYLE,
            ).ask()

            # questionary returns None when the user aborts (Ctrl-C, ESC).
            # Distinguish that from an explicit "_done" so the closing message
            # accurately reflects what happened.
            if choice is None:
                cancelled = True
                break
            if choice == "_done":
                break
            if choice == "_view":
                self._print_all_automation_settings()
                continue
            handler = section_handlers.get(choice)
            if handler:
                try:
                    handler()
                except _QuestionarySectionAbort:
                    # User pressed Ctrl-C / ESC inside the section — pop back
                    # to the section picker so they can pick a different
                    # section or use the explicit "Save and return" option,
                    # rather than tearing down the whole editor.
                    print("  (section edit aborted — back to section picker)")

        self.save_config()
        if cancelled:
            print("Settings editor cancelled. Edits to this point have been saved.")
        else:
            print("Settings saved.")

    def _questionary_section_summary(self) -> Dict[str, str]:
        """Per-section one-line summary shown in the section picker so the
        user can see at a glance what's set without opening the section."""
        c = self.config
        root = self.automation_root_folder or "(not set)"
        # Truncate root for the picker label
        if len(root) > 40:
            root = "..." + root[-37:]
        return {
            "paths": f"root={root}",
            "run": f"max_cases={self._read_max_cases_setting()}, reprocess={c.get('automation_reprocess_mode', 'skip')}",
            "front": (
                f"enabled={'y' if c.get('automation_front_expand_enabled') else 'n'}, "
                f"provider={c.get('automation_front_expand_provider', 'auto')}, "
                f"mode={c.get('automation_front_expand_mode', 'document_3x4')}"
            ),
            "portrait": (
                f"enabled={'y' if c.get('automation_extract_enabled') else 'n'}, "
                f"multiplier={c.get('automation_crop_multiplier', 1.5)}"
            ),
            "selfie": (
                f"enabled={'y' if c.get('automation_selfie_enabled') else 'n'}, "
                f"models={len(c.get('automation_selfie_models', []))}, "
                f"threshold={c.get('automation_similarity_threshold', 80)}%"
            ),
            "selfie_expand": (
                f"enabled={'y' if c.get('automation_selfie_expand_enabled') else 'n'}, "
                f"provider={c.get('automation_selfie_expand_provider', 'auto')}, "
                f"mode={c.get('automation_selfie_expand_mode', 'percent')}"
            ),
            "video": (
                f"enabled={'y' if c.get('automation_video_enabled') else 'n'}, "
                f"aspect={c.get('automation_video_aspect_ratio', '3:4')}"
            ),
            "facetrack": (
                f"enabled={'y' if c.get('automation_facetrack_enabled', False) else 'n'}, "
                f"min={c.get('automation_facetrack_min_pct', 96.0)}%, "
                f"{'required' if c.get('automation_facetrack_required', False) else 'advisory'}"
            ),
            "oldcam": (
                f"rppg={'ON' if c.get('automation_rppg_enabled') else 'off'}, "
                f"loop={'ON' if c.get('automation_loop_enabled') else 'off'}, "
                f"crush={self._format_crush_resolutions()}, "
                f"aa={self._format_aa_attacks()}, "
                f"versions={self._format_oldcam_versions(c.get('automation_oldcam_version'))}, "
                f"required={'y' if c.get('automation_oldcam_required') else 'n'}"
            ),
            "logging": (
                f"verbose={'y' if c.get('automation_verbose_logging') else 'n'}, "
                f"max_bytes={c.get('automation_log_max_bytes', 5_000_000)}"
            ),
        }

    def _print_all_automation_settings(self):
        """Render every automation_* setting in one Rich Table, then (on the
        questionary path) let the user pick ANY key and edit it in place —
        the "read-only table" dead-end is gone (user mandate 2026-06-11)."""
        table = self._styled_table("⚙  All automation settings", show_header=True)
        table.add_column("Setting", style="cyan", no_wrap=True)
        table.add_column("Value", style="white")
        table.add_row("automation_root_folder", str(self.automation_root_folder or "(not set)"))
        for key in sorted(k for k in self.config if str(k).startswith("automation_")):
            # Skip the field we already rendered explicitly above so the
            # table doesn't show two rows for the same setting.
            if key == "automation_root_folder":
                continue
            val = self.config.get(key)
            # Truncate giant prompt blobs so the table stays readable.
            sval = str(val)
            if len(sval) > 100:
                sval = sval[:97] + "..."
            # Prompt values are user text — escape or "[/x]" crashes the render.
            table.add_row(key, _rich_markup_escape(sval))
        _RICH_CONSOLE.print(table)
        if self._use_legacy_prompt_ui():
            self._wait_for_enter("\nPress Enter to return to the section picker...")
            return
        self._browse_all_settings()

    # Known-choice option lists for the generic editor, derived from the
    # shared option constants (single source — they can't drift from the
    # section editors).
    _SETTING_OPTIONS: Dict[str, List[str]] = {
        "automation_front_expand_provider": _EXPAND_PROVIDER_OPTIONS,
        "automation_selfie_expand_provider": _EXPAND_PROVIDER_OPTIONS,
        "automation_front_expand_mode": _EXPAND_MODE_OPTIONS,
        "automation_selfie_expand_mode": _SELFIE_EXPAND_MODE_OPTIONS,
        "automation_front_expand_composite_mode": _COMPOSITE_MODE_OPTIONS,
        "automation_selfie_expand_composite_mode": _COMPOSITE_MODE_OPTIONS,
        "automation_reprocess_mode": _REPROCESS_MODE_OPTIONS,
        "automation_selfie_model_policy": ["first_pass", "all"],
        "automation_max_cases_per_run": ["1", "5", "10", "all"],
        "automation_rppg_mode": ["iterative", "inject"],
        # Offer the fan-out mode as a choice (not free text) in the all-settings
        # editor so a typo can't be saved and silently normalized to the default
        # (CodeRabbit). Values mirror _FANOUT_MODE_OPTIONS / postproc_plan.
        "automation_postproc_fanout_mode": [v for v, _label in _FANOUT_MODE_OPTIONS],
    }

    def _browse_all_settings(self) -> None:
        """Pick any automation_* key (arrow keys / type-to-filter) and edit
        it with a type-appropriate prompt. Loops until Back."""
        while True:
            keys = sorted(k for k in self.config if str(k).startswith("automation_"))
            choices: List[Any] = []
            for key in keys:
                sval = str(self.config.get(key))
                if len(sval) > 48:
                    sval = sval[:45] + "..."
                choices.append(questionary.Choice(f"{key} = {sval}", key))
            choices.append(questionary.Choice("↩  Back to section picker", "_back"))
            pick = self._q_select(
                "Edit which setting?",
                choices,
                instruction="(type to filter · Enter to edit · Esc back)",
            )
            if pick in (None, "_back"):
                return
            try:
                self._qs_edit_any_setting(pick)
            except _QuestionarySectionAbort:
                continue
            self.save_config()

    def _qs_edit_any_setting(self, key: str) -> None:
        """Generic typed editor for one automation_* key. Type is inferred
        from AUTOMATION_DEFAULTS (fallback: the current value); known-choice
        strings get a select; the oldcam selection gets the checkbox."""
        if key == "automation_oldcam_version":
            self._qs_pick_oldcam_versions()
            return
        if key == "automation_selfie_models":
            self._qs_pick_selfie_models()
            return
        if key == "automation_selfie_prompts":
            self._qs_section_selfie_prompt_only()
            return
        defaults = merge_automation_defaults({})
        reference = defaults.get(key, self.config.get(key))
        if key in self._SETTING_OPTIONS:
            self._qs_choice(f"{key}:", key, choices=list(self._SETTING_OPTIONS[key]),
                            default=str(self.config.get(key, reference)))
        elif isinstance(reference, bool):
            self._qs_bool(f"{key}?", key, default=bool(reference))
        elif isinstance(reference, int) and not isinstance(reference, bool):
            self._qs_int(f"{key}:", key, default=int(reference) if reference is not None else 0)
        elif isinstance(reference, float):
            self._qs_float(f"{key}:", key, default=float(reference) if reference is not None else 0.0)
        elif isinstance(reference, list):
            current = self.config.get(key, reference)
            raw = self._q_text(
                f"{key} (comma-separated):",
                default=", ".join(str(x) for x in (current or [])),
            )
            if raw is not None:
                self.config[key] = [part.strip() for part in raw.split(",") if part.strip()]
        else:
            self._qs_text(f"{key}:", key, default=str(reference) if reference is not None else "")

    # ── Per-section editors ──────────────────────────────────────────

    def _qs_section_banner(self, title: str, description: str = "") -> None:
        """Print a polished section header before its prompts."""
        body = f"[bold cyan]{title}[/bold cyan]"
        if description:
            body += f"\n[dim]{description}[/dim]"
        _RICH_CONSOLE.print(Panel.fit(body, border_style="cyan"))

    def _qs_text(self, message: str, key: str, default: Optional[str] = None,
                 validator=None) -> None:
        """Edit a string setting. Live-validates the stripped value via
        questionary's `validate=` so trailing whitespace doesn't trip the
        validator (the stored value is also stripped). Empty input keeps
        current."""
        current = self.config.get(key, default)

        def _live_validate(text: str):
            t = text.strip()
            if not t:
                return True  # empty == keep current
            if validator:
                # questionary's validate= can return either bool or str.
                # Propagate str messages from the caller's validator so each
                # field can show a specific error instead of a generic one.
                res = validator(t)
                if res is not True:
                    return res if res else f"Value not accepted for {key}."
            return True

        answer = questionary.text(
            message,
            qmark="◆",
            instruction=f"(current: {current} · Enter keeps)",
            default="",
            validate=_live_validate,
            style=KLING_QUESTIONARY_STYLE,
        ).ask()
        if answer is None:
            # Ctrl-C / ESC — bubble up to the section picker so the user
            # doesn't have to abort every remaining field in this section.
            raise _QuestionarySectionAbort()
        if not answer.strip():
            return  # empty submit == keep current value
        self.config[key] = answer.strip()

    def _qs_int(self, message: str, key: str, default: int = 0,
                validator=None) -> None:
        """Edit an int setting. Uses questionary's `validate=` so the user
        gets immediate feedback for non-integer or out-of-range input
        instead of finding out only after submit. Empty input keeps current."""
        current = self.config.get(key, default)

        def _live_validate(text: str):
            t = text.strip()
            if not t:
                return True  # empty == keep current
            try:
                v = int(t)
            except ValueError:
                return "Please enter a valid integer."
            if validator:
                res = validator(v)
                if res is not True:
                    return res if res else "Value is outside the allowed range."
            return True

        answer = questionary.text(
            message,
            qmark="◆",
            instruction=f"(current: {current} · Enter keeps)",
            default="",
            validate=_live_validate,
            style=KLING_QUESTIONARY_STYLE,
        ).ask()
        if answer is None:
            raise _QuestionarySectionAbort()
        if answer.strip() == "":
            return  # empty submit == keep current value
        # Validator has already passed at the prompt; int() is now guaranteed safe.
        self.config[key] = int(answer.strip())

    def _qs_float(self, message: str, key: str, default: float = 0.0,
                  validator=None) -> None:
        """Edit a float setting. Live-validates parseable number + optional
        range check via questionary's `validate=`. Empty input keeps current."""
        current = self.config.get(key, default)

        def _live_validate(text: str):
            t = text.strip()
            if not t:
                return True
            try:
                v = float(t)
            except ValueError:
                return "Please enter a valid number."
            if validator:
                res = validator(v)
                if res is not True:
                    return res if res else "Value is outside the allowed range."
            return True

        answer = questionary.text(
            message,
            qmark="◆",
            instruction=f"(current: {current} · Enter keeps)",
            default="",
            validate=_live_validate,
            style=KLING_QUESTIONARY_STYLE,
        ).ask()
        if answer is None:
            raise _QuestionarySectionAbort()
        if answer.strip() == "":
            return  # empty submit == keep current value
        self.config[key] = float(answer.strip())

    def _qs_bool(self, message: str, key: str, default: bool = False) -> None:
        # Arrow-key ON/off select, NOT a y/n confirm — the whole editor is
        # arrow-driven and the lingering "(Y/n)" prompts read as unfinished
        # (user, PR #96 round 6). Same coercion as the settings table so a
        # hand-edited "false" string preselects off.
        current = _parse_bool_cfg(self.config.get(key, default))
        if current is None:
            current = bool(default)
        answer = questionary.select(
            message,
            qmark="◆",
            instruction=f"(current: {'ON' if current else 'off'})",
            choices=["ON", "off"],
            default="ON" if current else "off",
            style=KLING_QUESTIONARY_STYLE,
        ).ask()
        if answer is None:
            raise _QuestionarySectionAbort()
        self.config[key] = answer == "ON"

    def _qs_choice(self, message: str, key: str, choices: List[str],
                   default: Optional[str] = None,
                   cast_fn: Optional[Any] = None) -> None:
        """Single-choice picker. Optional `cast_fn` converts the selected
        string to a typed value before persisting (e.g. `int` for choices
        like ["1", "2"] that should land in config as integers). Without
        cast_fn, the raw string from `choices` is stored."""
        current = str(self.config.get(key, default if default is not None else choices[0]))
        if current not in choices:
            current = default if default in choices else choices[0]
        answer = questionary.select(
            message,
            qmark="◆",
            instruction=f"(current: {current})",
            choices=choices,
            default=current,
            style=KLING_QUESTIONARY_STYLE,
        ).ask()
        if answer is None:
            raise _QuestionarySectionAbort()
        if cast_fn is not None:
            try:
                self.config[key] = cast_fn(answer)
            except (TypeError, ValueError):
                # Narrow catch: only swallow conversion failures. Other
                # exceptions (bugs inside future cast helpers) propagate so
                # we don't silently keep stale config.
                print(f"  ✗ Could not cast {answer!r} for {key}; keeping current.")
                return
        else:
            self.config[key] = answer

    def _qs_pick_oldcam_versions(self) -> bool:
        """Spacebar multi-select of oldcam versions (none / one / many / all).

        Shared by the settings-editor Oldcam section and the option-1 quick
        editor so the two can never drift. Persists the canonical list form
        into ``automation_oldcam_version`` and keeps
        ``automation_oldcam_enabled`` coherent with an empty selection.
        Returns True when the selection changed.
        """
        repo_root = Path(__file__).resolve().parent
        discovered = discover_oldcam_versions(repo_root)
        # Union with the shared option list so a version directory that is
        # temporarily missing on THIS box still shows (flagged) instead of
        # silently dropping from a saved selection.
        known = list(dict.fromkeys(discovered + [v for v in _OLDCAM_VERSION_OPTIONS if v != "all"]))
        known.sort(key=_oldcam_sort_key)
        current = self._selected_oldcam_versions()
        run_all = current == ["all"]
        choices = [
            questionary.Choice(
                _OFF_CHOICE_LABEL, value=_OFF_CHOICE_VALUE, checked=not current,
            ),
            questionary.Choice(
                "ALL versions (every discovered oldcam)",
                value="all",
                checked=run_all,
            ),
        ]
        for version in known:
            missing = version not in discovered
            label = f"{version}  (not installed!)" if missing else version
            choices.append(
                questionary.Choice(
                    label,
                    value=version,
                    checked=(not run_all and version in current),
                )
            )
        answer = questionary.checkbox(
            "Oldcam versions to run (tick ✖ OFF, or none, to disable):",
            qmark="◆",
            instruction="(space toggles · Enter applies · Ctrl+C keeps current)",
            choices=choices,
            style=KLING_QUESTIONARY_STYLE,
        ).ask()
        if answer is None:
            raise _QuestionarySectionAbort()
        # OFF wins over any co-ticked versions (and over the ALL row).
        if _OFF_CHOICE_VALUE in (answer or []):
            new_value = []
        else:
            new_value = normalize_oldcam_versions(answer)
        changed = new_value != current
        self.config["automation_oldcam_version"] = new_value
        if not new_value:
            self.print_yellow("0 oldcam versions selected — the oldcam step will be skipped.")
            if self.config.get("automation_oldcam_enabled", True):
                self.config["automation_oldcam_enabled"] = False
                print("  (automation_oldcam_enabled set to False to match)")
        elif not self.config.get("automation_oldcam_enabled", True):
            # Picking versions while the step is disabled almost certainly
            # means the user wants it back on — keep the flags coherent.
            self.config["automation_oldcam_enabled"] = True
            print("  (automation_oldcam_enabled set to True to match selection)")
        print(f"  Oldcam will run: {self._format_oldcam_versions()}")
        return changed

    def _qs_pick_crush_resolutions(self) -> bool:
        """Spacebar multi-select of crush resolutions (none / 720p / 480p / both).

        Mirrors _qs_pick_oldcam_versions: each selected tier fans out one
        crushed file, exactly like the oldcam version list. Persists the
        canonical ``automation_crush_resolutions`` list and keeps the legacy
        ``automation_crush_enabled`` boolean coherent (True iff any tier on).
        Returns True when the selection changed.
        """
        current = self._selected_crush_resolutions()
        # Highest-first so 720p reads before 480p.
        known = sorted(CRUSH_RESOLUTIONS, key=lambda lbl: CRUSH_RESOLUTIONS[lbl], reverse=True)
        # Explicit OFF row (2026-06-19): "deselect everything" was an invisible
        # way to turn a step off. Ticking this clears the whole selection.
        choices = [
            questionary.Choice(
                _OFF_CHOICE_LABEL, value=_OFF_CHOICE_VALUE, checked=not current,
            )
        ]
        choices += [
            questionary.Choice(label, value=label, checked=(label in current))
            for label in known
        ]
        answer = questionary.checkbox(
            "Crush resolutions to run (tick ✖ OFF, or none, to disable):",
            qmark="◆",
            instruction="(space toggles · Enter applies · Ctrl+C keeps current)",
            choices=choices,
            style=KLING_QUESTIONARY_STYLE,
        ).ask()
        if answer is None:
            raise _QuestionarySectionAbort()
        # OFF wins over any co-ticked tiers.
        new_value = [] if _OFF_CHOICE_VALUE in (answer or []) else normalize_crush_resolutions(answer)
        changed = new_value != current
        self.config["automation_crush_resolutions"] = new_value
        # Keep the legacy boolean coherent for any reader still gating on it.
        self.config["automation_crush_enabled"] = bool(new_value)
        if not new_value:
            self.print_yellow("0 crush resolutions selected — the crush step will be skipped.")
        else:
            print(f"  Crush will run: {self._format_crush_resolutions()}")
        return changed

    def _qs_pick_aa_attacks(self) -> bool:
        """Spacebar multi-select of AA attack-pipelines + strength + generator.

        Mirrors _qs_pick_crush_resolutions: each selected pipeline fans out one
        AA output file. Persists ``automation_aa_attacks`` (canonical list) and
        keeps the legacy ``automation_aa_enabled`` boolean coherent. Also prompts
        for strength (0.1–1.0) and generator profile. Returns True if anything
        changed. Authorized detector-research use only.
        """
        current = self._selected_aa_attacks()
        # Display order: prime, scenario1, scenario3. All run on CPU (torch is
        # bundled in the AA venv; scenario modules use their _cpu variants when
        # no GPU is present); a GPU just speeds them up.
        known = [k for k in ("prime", "scenario1", "scenario3") if k in AA_PIPELINES]
        choices = [
            questionary.Choice(
                _OFF_CHOICE_LABEL, value=_OFF_CHOICE_VALUE, checked=not current,
            )
        ]
        choices += [
            questionary.Choice(label, value=label, checked=(label in current))
            for label in known
        ]
        answer = questionary.checkbox(
            "AA attack-pipelines to run (tick ✖ OFF, or none, to disable):",
            qmark="◆",
            instruction="(space toggles · Enter applies · Ctrl+C keeps current)",
            choices=choices,
            style=KLING_QUESTIONARY_STYLE,
        ).ask()
        if answer is None:
            raise _QuestionarySectionAbort()
        # OFF wins over any co-ticked pipelines.
        new_value = [] if _OFF_CHOICE_VALUE in (answer or []) else normalize_aa_attacks(attacks=answer)
        changed = new_value != current
        self.config["automation_aa_attacks"] = new_value
        self.config["automation_aa_enabled"] = bool(new_value)

        if not new_value:
            self.print_yellow("0 AA attacks selected — the AA step will be skipped.")
            return changed

        # Strength (0.1–1.0).
        cur_strength = self.config.get("automation_aa_strength", 0.5)
        s_answer = questionary.text(
            "AA strength (0.1–1.0):",
            qmark="◆",
            default=str(cur_strength),
            validate=lambda v: _aa_strength_valid(v) or "Enter a number 0.1–1.0",
            style=KLING_QUESTIONARY_STYLE,
        ).ask()
        if s_answer is None:
            raise _QuestionarySectionAbort()
        try:
            new_strength = max(0.1, min(1.0, float(s_answer)))
        except (TypeError, ValueError):
            new_strength = 0.5
        if new_strength != cur_strength:
            changed = True
        self.config["automation_aa_strength"] = new_strength

        # Generator profile.
        cur_gen = str(self.config.get("automation_aa_generator", "generic") or "generic")
        gen_choices = ["generic", "kling", "seedance", "runway"]
        g_answer = questionary.select(
            "AA generator profile (tunes attack strength to the source model):",
            qmark="◆",
            default=cur_gen if cur_gen in gen_choices else "generic",
            choices=gen_choices,
            style=KLING_QUESTIONARY_STYLE,
        ).ask()
        if g_answer is None:
            raise _QuestionarySectionAbort()
        if g_answer != cur_gen:
            changed = True
        self.config["automation_aa_generator"] = g_answer

        print(f"  AA will run: {self._format_aa_attacks()} "
              f"(strength {new_strength}, generator {g_answer})")
        return changed

    def _qs_pick_fanout_mode(self) -> bool:
        """Arrow-key select for the post-processing fan-out mode.

        ``powerset`` (separate + combined) produces every non-empty subset of
        the enabled modifiers; ``combined only`` produces a single cumulative
        chain. Persists ``automation_postproc_fanout_mode``. Returns True when
        the value changed.
        """
        from automation.postproc_plan import normalize_mode

        current = normalize_mode(self.config.get("automation_postproc_fanout_mode"))
        choices = [
            questionary.Choice(label, value=value, checked=(value == current))
            for value, label in _FANOUT_MODE_OPTIONS
        ]
        answer = questionary.select(
            "Output mode (how enabled modifiers combine):",
            qmark="◆",
            instruction=f"(current: {self._format_fanout_mode()})",
            choices=choices,
            default=current,
            style=KLING_QUESTIONARY_STYLE,
        ).ask()
        if answer is None:
            raise _QuestionarySectionAbort()
        changed = answer != current
        self.config["automation_postproc_fanout_mode"] = answer
        print(f"  Output mode -> {self._format_fanout_mode()}")
        return changed

    def _qs_directory(self, message: str, current_value: Optional[str],
                      picker_title: str) -> Optional[str]:
        """Pick a directory.

        Default action: open the native OS folder picker (Finder on macOS via
        osascript, native Explorer on Windows via tkinter.filedialog). User can
        skip the picker and either keep the current value or type a path
        manually. Returns the new path (or None if unchanged). The caller is
        responsible for writing the returned value into self.config — this
        helper is intentionally pure to keep "validate before persist" logic
        at the call site (e.g. checking `root_path.is_dir()` before commit).
        """
        action = questionary.select(
            message,
            qmark="◆",
            instruction=f"(current: {current_value or '(not set)'})",
            choices=[
                questionary.Choice("📂 Open folder picker (recommended)", "pick"),
                questionary.Choice("⌨  Type path manually", "type"),
                questionary.Choice("↩  Keep current", "keep"),
            ],
            default="pick",
            style=KLING_QUESTIONARY_STYLE,
        ).ask()
        if action in (None, "keep"):
            return None
        if action == "pick":
            picked = select_directory_cli_safe(title=picker_title)
            if not picked:
                # User cancelled the native picker. Offer manual entry as a
                # graceful fallback instead of silently bailing.
                print("  (picker cancelled — falling back to manual entry)")
                action = "type"
            else:
                return picked
        if action == "type":
            raw = questionary.text(
                "Folder path:",
                qmark="◆",
                default=current_value or "",
                style=KLING_QUESTIONARY_STYLE,
            ).ask()
            if raw is None or not raw.strip():
                return None
            return raw.strip()
        return None

    def _qs_section_paths(self):
        self._qs_section_banner(
            "Paths & discovery",
            "Where automation looks for case folders, and how many to process per batch.",
        )
        # Native folder picker by default — Finder on macOS, Explorer on Windows.
        # Manual text entry is a fallback when the user cancels or prefers typing.
        new_root = self._qs_directory(
            "Automation root folder:",
            current_value=self.automation_root_folder,
            picker_title="Choose automation root folder",
        )
        if new_root:
            root_path = Path(new_root).expanduser()
            if root_path.exists() and root_path.is_dir():
                self.automation_root_folder = str(root_path)
                self.config["automation_root_folder"] = self.automation_root_folder
                print(f"  ✓ Root set to {self.automation_root_folder}")
            else:
                print(f"  ✗ Path does not exist or is not a folder: {new_root}")
        self._qs_text(
            "Manifest filename:",
            "automation_manifest_name",
            default="automation_manifest.json",
            validator=lambda v: len(v) > 0 and v.endswith(".json") and Path(v).name == v,
        )
        self._qs_choice(
            "Max cases per run:",
            "automation_max_cases_per_run",
            choices=["1", "5", "10", "all"],
            default="all",
        )
        # Extra front-image glob patterns (comma-separated) for folders whose
        # front image is not literally front.jpg (e.g. *id_photo*.jpg). Empty
        # keeps exact-name-only discovery. Mirrors the --front-glob CLI flag.
        current_globs = ", ".join(self.config.get("automation_front_globs", []) or [])
        raw_globs = questionary.text(
            "Extra front-image globs (comma-separated, blank = none):",
            qmark="◆",
            default=current_globs,
            instruction=f"(current: {current_globs or '(none)'})",
            style=KLING_QUESTIONARY_STYLE,
        ).ask()
        if raw_globs is None:
            raise _QuestionarySectionAbort()
        self.config["automation_front_globs"] = [
            tok.strip() for tok in raw_globs.split(",") if tok.strip()
        ]

    def _qs_section_run(self):
        self._qs_section_banner(
            "Run scope",
            "Which case folders the next run includes, and how it handles already-processed work.",
        )
        self._qs_bool("Skip completed cases?", "automation_skip_completed", default=True)
        self._qs_bool("Skip if selfie already exists?", "automation_skip_if_selfie_exists", default=True)
        self._qs_bool("Skip if video already exists?", "automation_skip_if_video_exists", default=True)
        self._qs_bool("Allow reprocess (overrides skips)?", "automation_allow_reprocess", default=False)
        self._qs_choice(
            "Reprocess mode:",
            "automation_reprocess_mode",
            choices=_REPROCESS_MODE_OPTIONS,
            default="skip",
        )

    def _qs_section_front_expand(self):
        self._qs_section_banner(
            "Front expansion",
            "First pipeline stage: outpaint the input photo to a 3:4 / wider canvas before everything else.",
        )
        self._qs_bool("Front expand enabled?", "automation_front_expand_enabled", default=True)
        self._qs_choice("Provider:", "automation_front_expand_provider",
                        choices=_EXPAND_PROVIDER_OPTIONS, default="auto")
        self._qs_choice("Expand mode:", "automation_front_expand_mode",
                        choices=_EXPAND_MODE_OPTIONS, default="document_3x4")
        self._qs_choice("Composite mode:", "automation_front_expand_composite_mode",
                        choices=_COMPOSITE_MODE_OPTIONS,
                        default="preserve_seamless")
        self._qs_int("Expand percent:", "automation_front_expand_percent",
                     default=30, validator=lambda v: v >= 0)
        self._qs_choice("Expand passes:", "automation_front_expand_passes",
                        choices=["1", "2"], default="2", cast_fn=int)
        self._qs_bool("Edge seal enabled?", "automation_front_edge_seal_enabled", default=False)
        self._qs_int("Edge seal px:", "automation_front_edge_seal_px",
                     default=12, validator=lambda v: v >= 0)
        self._qs_text("Output filename:", "automation_front_output_name",
                      default="front-expanded.png",
                      validator=lambda v: len(v) > 0)

    def _qs_section_portrait(self):
        self._qs_section_banner(
            "Portrait extraction",
            "Crops a passport-style face from the front-expanded image for use as the selfie identity reference.",
        )
        self._qs_bool("Portrait extraction enabled?", "automation_extract_enabled", default=True)
        self._qs_text("Output filename:", "automation_extract_output_name",
                      default="extracted.png",
                      validator=lambda v: len(v) > 0)
        self._qs_float("Crop multiplier:", "automation_crop_multiplier",
                       default=1.5, validator=lambda v: v > 0)

    def _qs_section_selfie(self):
        self._qs_section_banner(
            "Selfie generation",
            "Generate identity-locked selfies; multiple attempts are gated by the similarity threshold.",
        )
        self._qs_bool("Selfie generation enabled?", "automation_selfie_enabled", default=True)
        # Shared picker (also used by the option-1 quick editor) — multiple
        # models selected = one full chain per model (fan-out).
        self._qs_pick_selfie_models()
        self._qs_choice("Model policy:", "automation_selfie_model_policy",
                        choices=["first_pass", "all"], default="first_pass")
        self._qs_int("Max attempts per model:", "automation_selfie_max_attempts_per_model",
                     default=1, validator=lambda v: v > 0)
        self._qs_int("Similarity threshold (0-100):", "automation_similarity_threshold",
                     default=80, validator=lambda v: 0 <= v <= 100)
        # 3:4 selfie dimensions (864x1152 default). Keeping width/height a true
        # 3:4 ratio is what makes the whole chain 3:4 — the ratio-preserving
        # expand and Kling both follow the still's aspect.
        self._qs_int("Selfie width (px):", "automation_selfie_width",
                     default=864, validator=lambda v: v > 0)
        self._qs_int("Selfie height (px):", "automation_selfie_height",
                     default=1152, validator=lambda v: v > 0)

        # Prompt slot — only ask if user wants to touch it.
        self._ensure_selfie_prompt_slots()
        current_slot = int(self.config.get("automation_selfie_prompt_slot", DEFAULT_AUTOMATION_SELFIE_PROMPT_SLOT))
        current_prompt = str(self.config.get("automation_selfie_prompts", {}).get(str(current_slot), "") or "")
        preview = (current_prompt[:80] + "...") if len(current_prompt) > 80 else current_prompt
        prompt_action = questionary.select(
            "Selfie prompt:",
            qmark="◆",
            instruction=f"(slot {current_slot}: \"{preview}\")",
            choices=[
                questionary.Choice("↩  Keep slot and prompt as-is", "_keep"),
                questionary.Choice("🔢 Switch to a different slot (1-10)", "switch"),
                questionary.Choice("✏  Edit the active prompt text", "edit"),
                questionary.Choice("♻  Reset the active slot to its default prompt", "reset"),
            ],
            default="_keep",
            style=KLING_QUESTIONARY_STYLE,
        ).ask()
        prompt_action = _qs_or_abort(prompt_action)  # B3: Esc aborts the section
        if prompt_action == "switch":
            slot_raw = questionary.text(
                "New slot (1-10, or Enter to keep current):",
                qmark="◆",
                # Live-validate: accept either an in-range digit or empty
                # input. Empty == "I don't actually want to switch" which is
                # the cancel path for this sub-prompt; we used to reject it
                # and force the user into Ctrl-C.
                validate=lambda t: (
                    True if (not t.strip() or (t.strip().isdigit() and 1 <= int(t.strip()) <= 10))
                    else "Please enter a number between 1 and 10."
                ),
                style=KLING_QUESTIONARY_STYLE,
            ).ask()
            if slot_raw is None:
                # Ctrl-C / ESC inside the sub-prompt → abort the whole section.
                raise _QuestionarySectionAbort()
            if slot_raw.strip():
                self.config["automation_selfie_prompt_slot"] = int(slot_raw.strip())
        elif prompt_action == "edit":
            new_prompt = questionary.text(
                "New prompt text (submit empty to clear):",
                qmark="◆",
                default=current_prompt,
                style=KLING_QUESTIONARY_STYLE,
            ).ask()
            # Only `None` means the user aborted (Ctrl-C / ESC). Empty string
            # is a deliberate clear — system falls back to defaults in
            # _get_selected_selfie_prompt when the slot is blank.
            if new_prompt is not None:
                self.config["automation_selfie_prompts"][str(current_slot)] = new_prompt.strip()
        elif prompt_action == "reset":
            defaults = merge_automation_defaults({}).get("automation_selfie_prompts", {})
            self.config["automation_selfie_prompts"][str(current_slot)] = defaults.get(
                str(current_slot), defaults.get("1", "")
            )
            print(f"  ✓ Slot {current_slot} reset to default.")

    def _qs_section_selfie_expand(self):
        self._qs_section_banner(
            "Selfie expansion",
            "Outpaint the generated selfie to wider canvas before video generation.",
        )
        self._qs_bool("Selfie expansion enabled?", "automation_selfie_expand_enabled", default=True)
        self._qs_choice("Provider:", "automation_selfie_expand_provider",
                        choices=_EXPAND_PROVIDER_OPTIONS, default="auto")
        self._qs_choice("Expand mode:", "automation_selfie_expand_mode",
                        choices=_SELFIE_EXPAND_MODE_OPTIONS, default="percent")
        self._qs_choice("Composite mode:", "automation_selfie_expand_composite_mode",
                        choices=_COMPOSITE_MODE_OPTIONS,
                        default="preserve_seamless")
        self._qs_int("Expand percent:", "automation_selfie_expand_percent",
                     default=30, validator=lambda v: v >= 0)

    def _qs_section_video(self):
        self._qs_section_banner(
            "Video generation",
            "Kling AI animation from the expanded selfie.",
        )
        self._qs_bool("Video generation enabled?", "automation_video_enabled", default=True)
        self._qs_text("Aspect ratio (e.g. 3:4):", "automation_video_aspect_ratio",
                      default="3:4", validator=lambda v: ":" in v)
        self._qs_bool("Use existing video prompt slot?", "automation_video_use_existing_prompt", default=True)

    def _qs_section_facetrack(self):
        # Mirrors the legacy walker's face-track-gate prompts (PR #37).
        # Defaults are intentionally OFF per docs/analysis/versailles_fail_vs_pass.md:
        # a large balanced corpus showed face-track % does NOT separate Persona
        # PASS from FAIL — the earlier "96% zero-false-positive" claim was a
        # small-sample artifact. Leave disabled unless your own labelled corpus
        # validates a safe threshold.
        self._qs_section_banner(
            "Face-track gate",
            "DIAGNOSTIC-ONLY. Off by default — see docs/analysis/versailles_fail_vs_pass.md. "
            "Advisory mode routes sub-threshold sources to manual_review; required mode "
            "hard-fails the case and skips oldcam.",
        )
        self._qs_bool("Face-track gate enabled?", "automation_facetrack_enabled", default=False)
        self._qs_float("Min face-track percent (0-100):", "automation_facetrack_min_pct",
                       default=96.0, validator=lambda v: 0.0 <= v <= 100.0)
        self._qs_bool("Required (sub-threshold => fail + skip oldcam)?",
                      "automation_facetrack_required", default=False)
        self._qs_float("Sample fps (1-30):", "automation_facetrack_sample_fps",
                       default=8.0, validator=lambda v: 1.0 <= v <= 30.0)

    def _qs_section_oldcam(self):
        self._qs_section_banner(
            "Post-processing: rPPG + Loop + Oldcam",
            "Final stages, applied in order Kling -> rPPG -> Loop -> Oldcam.",
        )
        # rPPG injects a sub-perceptual pulse on the raw Kling output BEFORE
        # loop/oldcam (Phase E order) so every downstream file carries it.
        self._qs_bool("rPPG injection enabled (sub-perceptual pulse)?",
                      "automation_rppg_enabled", default=False)
        self._qs_bool("rPPG required (fail case if rPPG produces no output)?",
                      "automation_rppg_required", default=False)
        self._qs_bool("rPPG metrics in filename (off = clean *-rppg + .metrics.json sidecar)?",
                      "automation_rppg_metrics_in_filename", default=False)
        # Ping-pong loop (2026-06-11): default OFF; graceful-skip when
        # ffmpeg is missing.
        self._qs_bool("Loop video (seamless ping-pong, runs before oldcam)?",
                      "automation_loop_enabled", default=False)
        # Multi-select (2026-06-11): spacebar checkbox over discovered
        # versions; the picker keeps automation_oldcam_enabled coherent
        # (empty selection => disabled), so no separate enabled? prompt.
        self._qs_pick_oldcam_versions()
        self._qs_bool("Oldcam required (fail case if oldcam fails)?",
                      "automation_oldcam_required", default=False)

    def _qs_section_logging(self):
        self._qs_section_banner(
            "Logging",
            "Automation log verbosity and rotation policy.",
        )
        self._qs_bool("Verbose automation logging?", "automation_verbose_logging", default=False)
        self._qs_int("Log max bytes (rotation size):", "automation_log_max_bytes",
                     default=5_000_000, validator=lambda v: v > 0)
        self._qs_int("Log backup count:", "automation_log_backup_count",
                     default=3, validator=lambda v: v >= 1)

    def _dry_run_automation(self):
        if not self.automation_root_folder:
            self.print_red("Set automation root folder first.")
            self.pause_review("Press Enter to continue...")
            return
        root = Path(self.automation_root_folder)
        if not root.exists():
            self.print_red("Automation root path does not exist.")
            self.pause_review("Press Enter to continue...")
            return
        records = discover_case_folders(
            root,
            self.config.get("automation_front_names", []),
            front_globs=self.config.get("automation_front_globs", []),
        )
        manifest_path = self._automation_manifest_path()
        had_manifest = manifest_path.exists()
        # read_only: dry run promises non-mutation — it must never rename a
        # corrupt manifest aside (Codex P2, PR #96 round 4).
        manifest = AutomationManifest.load_if_exists(manifest_path, read_only=True)
        manifest_warning = ""
        if manifest is None and had_manifest:
            manifest_warning = "Warning: existing manifest unreadable or schema-mismatched; dry-run ignoring manifest state."
        _rows, counts, runnable_cases = self._collect_case_snapshot(records, manifest)

        _front_blend = self.config.get('automation_front_expand_composite_mode', self.config.get('outpaint_composite_mode', 'preserve_seamless'))
        _selfie_blend = self.config.get('automation_selfie_expand_composite_mode', self.config.get('outpaint_composite_mode', 'preserve_seamless'))
        _rppg_on = bool(self.config.get("automation_rppg_enabled", False))
        _rppg_seg = " -> rppg" if _rppg_on else " -> rppg(off)"
        _loop_on = bool(self.config.get("automation_loop_enabled", False))
        _loop_seg = " -> loop" if _loop_on else " -> loop(off)"
        _crush_tiers = self._selected_crush_resolutions()
        _crush_seg = (" -> crush(" + "/".join(_crush_tiers) + ")") if _crush_tiers else " -> crush(off)"
        _aa_attacks = self._selected_aa_attacks()
        _aa_seg = (" -> aa(" + "/".join(_aa_attacks) + ")") if _aa_attacks else " -> aa(off)"
        _steps_line = f"front_expand -> extract -> selfie -> similarity -> selfie_expand -> video{_rppg_seg}{_loop_seg}{_crush_seg}{_aa_seg} -> oldcam"
        if not self._use_legacy_prompt_ui():
            # Branded repaint + Rich layout (interactive only — the legacy
            # prints below are asserted by non-TTY tests / cron logs).
            self.display_header()
            dr = self._styled_table("🧪  Dry run — no API calls")
            dr.add_column("k", style="cyan", no_wrap=True)
            dr.add_column("v")
            if manifest_warning:
                dr.add_row("Warning", f"[yellow]{manifest_warning}[/yellow]")
            dr.add_row("Discovered cases", str(counts["discovered"]))
            dr.add_row("Completed total", str(counts["completed_total"]))
            dr.add_row("Skipped complete", str(counts["skipped_complete"]))
            dr.add_row("Pending", str(counts["pending"]))
            dr.add_row("Failed / manual review", str(counts["failed"] + counts["manual_review"]))
            dr.add_row("Will run this batch", f"[bold green]{len(runnable_cases)}[/bold green]")
            dr.add_section()
            dr.add_row("Blends (front/selfie)", f"{_front_blend} / {_selfie_blend}")
            dr.add_row("Planned steps", f"[dim]{_steps_line}[/dim]")
            _RICH_CONSOLE.print(dr)
            # WHICH folders this batch slice would run, and where each starts
            # (round 8: a dry run that only repeated the approval counts had
            # no reason to exist).
            if runnable_cases:
                rows_by_key = {r["case"]: r for r in _rows}
                plan = self._styled_table(f"🗂  This batch would run ({len(runnable_cases)})")
                plan.add_column("#", style="dim", no_wrap=True)
                plan.add_column("Folder", style="cyan")
                plan.add_column("Starts at")
                for idx, rec in enumerate(runnable_cases[:15], 1):
                    key = getattr(rec, "relative_key", str(rec))
                    planned = str((rows_by_key.get(key) or {}).get("planned", "run"))
                    plan.add_row(str(idx), _rich_markup_escape(key), _rich_markup_escape(planned))
                if len(runnable_cases) > 15:
                    plan.add_row("…", f"[dim]+{len(runnable_cases) - 15} more[/dim]", "")
                print()
                _RICH_CONSOLE.print(plan)
                print()
                _RICH_CONSOLE.print(self._build_cost_estimate_table(len(runnable_cases)))
            self.pause_review("\nPress Enter to continue...")
            return
        print("\nDry run summary")
        if manifest_warning:
            print(f"  {manifest_warning}")
        print(f"  discovered cases: {counts['discovered']}")
        print(f"  completed total: {counts['completed_total']}")
        print(f"  skipped complete: {counts['skipped_complete']}")
        print(f"  pending: {counts['pending']}")
        print(f"  failed/manual_review: {counts['failed'] + counts['manual_review']}")
        print(f"  will run this batch: {len(runnable_cases)}")
        print(f"  composites: front={_front_blend} selfie={_selfie_blend}")
        print(f"  planned steps: {_steps_line}")
        self.pause_review("\nPress Enter to continue...")

    def _estimate_batch_cost_rows(self, will_run: int) -> "List[Tuple[str, str]]":
        """(label, value) rows estimating the API spend for ``will_run`` cases.

        Uses the memoized fal pricing API: selfie $/img per selected model
        (multi-model fan-out runs ONE FULL CHAIN per model, so the video cost
        multiplies too) and video $/sec × duration. Expand/outpaint steps and
        similarity-gate retries are excluded — the caption discloses that.
        Unknown prices render "n/a" instead of pretending."""
        c = self.config
        models = list(c.get("automation_selfie_models") or [])
        n_models = max(1, len(models))
        video_endpoint, _video_display = resolve_cli_video_model(c)
        duration = resolve_cli_video_duration(c)
        video_price = self.fetch_model_pricing(video_endpoint) if video_endpoint else None
        selfie_prices = [self.fetch_model_pricing(m) for m in models]
        rows: "List[Tuple[str, str]]" = []
        per_case = 0.0
        complete = True
        if models:
            known = [p for p in selfie_prices if p]
            if len(known) == len(models):
                selfie_total = sum(known)
                per_case += selfie_total
                rows.append(("Selfie image(s)", f"${selfie_total:.2f} / case  ({len(models)} model(s))"))
            else:
                complete = False
                rows.append(("Selfie image(s)", "n/a (no price for one or more models)"))
        if video_price:
            video_total = video_price * duration * n_models
            per_case += video_total
            rows.append((
                "Kling video",
                f"${video_total:.2f} / case  (${video_price:.2f}/sec × {duration}s"
                + (f" × {n_models} fan-out chains)" if n_models > 1 else ")"),
            ))
        else:
            complete = False
            rows.append(("Kling video", "n/a (price unavailable)"))
        approx = "≈" if complete else "≥"
        rows.append(("Per case", f"[bold]{approx} ${per_case:.2f}[/bold]"))
        rows.append((f"Batch ({will_run} case(s))", f"[bold yellow]{approx} ${per_case * will_run:.2f}[/bold yellow]"))
        return rows

    def _build_cost_estimate_table(self, will_run: int) -> Table:
        table = self._styled_table(
            "💰  Estimated spend",
            caption="estimate only — excludes expand/outpaint steps, retries and provider fees",
        )
        table.add_column("k", style="cyan", no_wrap=True)
        table.add_column("v")
        for label, value in self._estimate_batch_cost_rows(will_run):
            table.add_row(label, value)
        return table

    def run_automation_headless(
        self,
        root: str,
        *,
        auto_approve: bool = True,
        max_cases_override: Optional[str] = None,
        reprocess_override: Optional[str] = None,
        model_override: Optional[str] = None,
        model_name_override: Optional[str] = None,
        oldcam_version_override: Optional[str] = None,
        rppg_override: Optional[bool] = None,
        front_globs_override: Optional[List[str]] = None,
        outpaint_timeout_override: Optional[str] = None,
        provider_override: Optional[str] = None,
    ) -> int:
        """Non-interactive batch runner for the automation pipeline.

        Mirrors the runnable body of :meth:`_run_resume_automation` but reads
        NO stdin (so it can run from cron / Windows Task Scheduler) and returns
        a process exit code instead of pausing. INVARIANT: this path never
        invokes ``questionary`` or ``input()`` — it is auto-approved and exits
        on missing/invalid input rather than prompting (keep it stdin-free):

        * ``0`` -- batch ran and EVERY case completed cleanly.
        * ``1`` -- could not run: missing/invalid root, no case folders, no
                   runnable cases, manifest load error, preflight failure, or a
                   run-level exception.
        * ``2`` -- the batch ran but one or more cases ended ``failed`` or
                   ``manual_review``. A scheduled job MUST treat this as
                   needs-attention, not success -- ``manual_review`` cases
                   (similarity-gate undecided / anti-spoofing flagged) are
                   silently dropped from the operator's view if we exit 0
                   (code-review HIGH-1, PR #69).

        ``auto_approve`` must be ``True`` in headless mode -- the caller already
        opted in via ``--batch``. ``False`` is reserved for a future interactive
        confirm hook that is not yet implemented, so we abort loudly rather than
        silently proceeding (code-review HIGH-2, PR #69). The interactive
        :meth:`_run_resume_automation` is left untouched -- this is an additive
        path, not a replacement.
        """
        # TTY-aware status helpers: under cron/pipe (non-TTY) the colour-wrapping
        # print_red/print_yellow would inject raw ANSI escapes (\033[..]) into the
        # log, so headless messages use plain print() there (code-review Codex P2,
        # PR #69). sys.stdout can be None when run as a Windows background service,
        # so guard the isatty() call (code-review Gemini, PR #69).
        # sys.stdout may be None (Windows background service) OR a custom stream
        # (io.StringIO in tests, IDE/GUI console wrappers) lacking isatty(), so
        # check both before calling it (code-review Gemini, PR #69).
        _stdout = getattr(sys, "stdout", None)
        _is_tty = bool(_stdout) and hasattr(_stdout, "isatty") and _stdout.isatty()

        def _err(msg: str) -> None:
            self.print_red(msg) if _is_tty else print(msg)

        def _warn(msg: str) -> None:
            self.print_yellow(msg) if _is_tty else print(msg)

        if not auto_approve:
            # Reserved for a future confirm hook; not implemented. Fail loud
            # instead of silently running unapproved (the param was previously
            # accepted-and-ignored, which could mislead a future caller).
            _err("[batch] auto_approve=False is not supported in headless mode.")
            return 1

        # Validate --limit HERE (not via argparse choices): argparse rejects an
        # invalid choice with ArgumentParser.error() -> exit 2, which collides
        # with our documented "exit 2 = ran-but-needs-attention" contract. Doing
        # it in-runner keeps exit 2 reserved for runs that actually ran
        # (code-review Codex P2, PR #69).
        if max_cases_override is not None:
            norm_limit = str(max_cases_override).strip().lower()
            if norm_limit not in {"1", "5", "10", "all"}:
                _err(f"[batch] Invalid --limit '{max_cases_override}'; use 1, 5, 10, or all.")
                return 1
            max_cases_override = norm_limit
        # Validate --reprocess HERE too (same reasoning as --limit: argparse
        # choices= would exit 2, colliding with the contract; and a direct Python
        # caller could pass a bogus value that _effective_reprocess_mode() then
        # silently swallows back to "skip" -- code-review Codex P2 + Gemini, PR #69).
        if reprocess_override is not None:
            norm_reprocess = str(reprocess_override).strip().lower()
            if norm_reprocess not in {"skip", "overwrite", "increment"}:
                _err(f"[batch] Invalid --reprocess '{reprocess_override}'; use skip, overwrite, or increment.")
                return 1
            reprocess_override = norm_reprocess
        # NOTE: --reprocess / --limit overrides are applied AFTER the manifest
        # is loaded. Since round 7 the fingerprint EXCLUDES run-scope keys
        # (automation/manifest._FINGERPRINT_EXCLUDED_KEYS), so applying them
        # earlier would no longer break the load — but the after-load ordering
        # is kept: they are run policy, not manifest identity, and the
        # ordering documents that (original hazard: code-review Codex P1,
        # PR #69, when these keys WERE fingerprinted).

        root = (root or "").strip()
        if not root:
            _err("[batch] No automation root provided.")
            return 1
        self.automation_root_folder = root
        self.config["automation_root_folder"] = root

        root_path = Path(root)
        if not root_path.exists():
            _err(f"[batch] Automation root does not exist: {root}")
            return 1
        if not root_path.is_dir():
            # A file path passes exists() but is not a valid root; reject it as
            # the documented invalid-root preflight rather than letting it fall
            # into discover_case_folders and misreport as "no case folders"
            # (code-review CodeRabbit Major, PR #69).
            _err(f"[batch] Automation root is not a directory: {root}")
            return 1

        # --- FINGERPRINTED overrides: apply BEFORE discovery + create_or_load ---
        # automation_* keys are baked into the manifest fingerprint
        # (manifest.py). Applying them AFTER create_or_load would load a stale
        # manifest under the OLD settings and the new behavior would silently
        # no-op (the documented bug class at manifest.py:119-128). So these MUST
        # land on self.config before discover_case_folders and create_or_load.
        if oldcam_version_override is not None:
            raw_ver = str(oldcam_version_override).strip()
            if not raw_ver:
                _err("[batch] --oldcam-version must not be empty.")
                return 1
            # Accepts a single version ("v13"), a comma list ("v13,v24"),
            # "all", or "none" (explicit empty selection => oldcam skipped).
            normalized_ver = normalize_oldcam_versions(raw_ver)
            self.config["automation_oldcam_version"] = normalized_ver
            if not normalized_ver:
                # --oldcam-version none means "no oldcam this run"; drop the
                # required flag too or validate_configuration() would reject
                # the run as contradictory (required=true + empty selection).
                self.config["automation_oldcam_required"] = False
        if rppg_override is not None:
            self.config["automation_rppg_enabled"] = bool(rppg_override)
        if front_globs_override is not None:
            # Explicit override wins even when empty: passing an empty list (or
            # only-whitespace patterns) clears any saved globs for this run,
            # rather than silently leaving them active.
            self.config["automation_front_globs"] = [
                str(p).strip() for p in front_globs_override if str(p).strip()
            ]
        if provider_override is not None:
            prov = str(provider_override).strip().lower()
            if prov not in {"fal", "bfl", "auto"}:
                _err(f"[batch] Invalid --provider '{provider_override}'; use fal, bfl, or auto.")
                return 1
            # Force BOTH expand stages onto the chosen provider. This overrides
            # the saved automation_*_expand_provider keys (a personal config can
            # carry bfl while the user wants the run wholly on fal.ai).
            self.config["automation_front_expand_provider"] = prov
            self.config["automation_selfie_expand_provider"] = prov

        # --- NON-fingerprinted overrides: model + outpaint timeout. These are
        # NOT automation_* keys, so they do not affect the manifest fingerprint
        # and can be applied here (read lazily by the video factory /
        # get_outpaint_fal_timeout_seconds when the runner is built).
        if model_override is not None:
            endpoint = str(model_override).strip()
            if not endpoint:
                _err("[batch] --model must not be empty.")
                return 1
            # Per-surface split: the override targets the CLI pipeline's own
            # keys; the GUI's current_model selection stays untouched.
            self.config["cli_video_model"] = endpoint
            self.config["cli_video_model_display_name"] = (
                str(model_name_override).strip()
                if model_name_override and str(model_name_override).strip()
                else _derive_model_display_name(endpoint)
            )
        if outpaint_timeout_override is not None:
            try:
                raw_t = int(str(outpaint_timeout_override).strip())
            except (TypeError, ValueError):
                _err(f"[batch] Invalid --outpaint-timeout '{outpaint_timeout_override}'; use an integer.")
                return 1
            # Clamp to the documented [30, 300] envelope at write time + warn so
            # the stored value matches the effective behavior (the help text
            # promises a clamp; don't silently store an out-of-range value that
            # get_outpaint_fal_timeout_seconds quietly clamps on read).
            clamped_t = max(30, min(300, raw_t))
            if clamped_t != raw_t:
                _warn(f"[batch] --outpaint-timeout {raw_t} out of range [30, 300]; using {clamped_t}.")
            self.config["outpaint_fal_timeout_seconds"] = clamped_t

        # EAFP: directly attempt discovery and catch OSError (restricted FS /
        # permission errors) rather than pre-flighting (code-review Gemini, PR #69).
        try:
            records = discover_case_folders(
                root_path,
                self.config.get("automation_front_names", []),
                front_globs=self.config.get("automation_front_globs", []),
                warn_cb=_warn,
            )
        except OSError as exc:
            _err(f"[batch] Failed to scan automation root: {exc}")
            return 1
        if not records:
            _warn(f"[batch] No case folders found under {root}.")
            return 1

        # Did the caller pass an EXPLICIT fingerprinted identity override
        # (oldcam-version / rppg / provider)? If so, a fingerprint mismatch
        # against an OLD manifest is intentional — the help text promises
        # "forces a fresh manifest". Without this, a user with a v24 manifest
        # running --oldcam-version v13 would hit a mismatch ValueError and
        # exit 1 with no run (Codex HIGH). On that specific mismatch we back
        # up the stale manifest and recreate it fresh.
        # NOTE: --front-glob is NOT in this set anymore (round 7) — the
        # discovery keys are excluded from the fingerprint, so a glob change
        # keeps the manifest; the per-case front-changed guard in
        # AutoPipelineRunner._reset_case_if_front_changed reprocesses exactly
        # the cases whose front re-selected to a different file.
        _identity_override_requested = any(
            v is not None
            for v in (
                oldcam_version_override,
                rppg_override,
                provider_override,
            )
        )
        _manifest_snapshot = {k: v for k, v in self.config.items() if str(k).startswith("automation_")}
        try:
            manifest = AutomationManifest.create_or_load(
                manifest_path=self._automation_manifest_path(),
                root_dir=root_path,
                config_snapshot=_manifest_snapshot,
            )
        except Exception as exc:
            is_fingerprint_mismatch = "fingerprint mismatch" in str(exc)
            if _identity_override_requested and is_fingerprint_mismatch:
                _warn(
                    "[batch] Identity override changes the run fingerprint; "
                    "recreating a fresh manifest (old one backed up)."
                )
                try:
                    manifest = AutomationManifest.create_fresh(
                        manifest_path=self._automation_manifest_path(),
                        root_dir=root_path,
                        config_snapshot=_manifest_snapshot,
                    )
                except Exception as exc2:
                    _err(f"[batch] Failed to recreate manifest: {exc2}")
                    return 1
            else:
                _err(f"[batch] Failed to load manifest: {exc}")
                return 1

        # Apply CLI overrides NOW (post-manifest): these are run policy, not part
        # of the manifest fingerprint, so they must not influence create_or_load.
        if max_cases_override is not None:
            self.config["automation_max_cases_per_run"] = str(max_cases_override)
        if reprocess_override is not None:
            # Already normalized + validated above (skip|overwrite|increment).
            mode = reprocess_override
            self.config["automation_reprocess_mode"] = mode
            # _effective_reprocess_mode() forces "skip" unless allow_reprocess is
            # True, so an explicit --reprocess is inert without this flag.
            self.config["automation_allow_reprocess"] = True
            # For overwrite/increment, the user explicitly wants completed cases
            # RE-RUN. But _planned_action_for_case() returns "skip_complete" (and
            # the runner re-skips) while automation_skip_completed / the
            # skip_if_*_exists guards stay on -- so the reprocess command would
            # still report "no runnable cases" (code-review Codex P1, PR #69).
            # Drop those skip guards so completed cases actually flow through.
            if mode in ("overwrite", "increment"):
                self.config["automation_skip_completed"] = False
                self.config["automation_skip_if_selfie_exists"] = False
                self.config["automation_skip_if_video_exists"] = False

        try:
            _rows, counts, runnable_cases = self._collect_case_snapshot(records, manifest)
        except Exception as exc:
            # Filesystem errors on a discovered case dir (permissions, a corrupt
            # entry) must surface as a clean [batch] exit-1, not an unhandled
            # traceback bubbling to main() (code-review MEDIUM-3, PR #69).
            _err(f"[batch] Failed to build case snapshot: {exc}")
            return 1
        print("[batch] Run preview:")
        print(f"  discovered: {counts['discovered']}")
        print(f"  completed total: {counts.get('completed_total', 0)}")
        print(f"  skipped complete: {counts.get('skipped_complete', 0)}")
        print(f"  pending/runnable: {counts.get('pending', 0)}")
        print(f"  will run this batch: {counts.get('will_run', 0)}")
        print(f"  manual review: {counts.get('manual_review', 0)}")
        print(f"  failed: {counts.get('failed', 0)}")
        if not runnable_cases:
            _warn("[batch] No runnable cases for this batch; nothing to do.")
            return 1

        runner = AutoPipelineRunner(
            config=self.config,
            automation_config=from_app_config(self.config),
            manifest=manifest,
            progress_cb=None,
        )
        issues = runner.validate_configuration()
        if issues:
            _err("[batch] Automation preflight failed:")
            for issue in issues:
                print(f"  - {issue}")
            return 1

        print("[batch] Automation preflight:")
        print(f"  cases discovered: {len(records)}")
        print(f"  running this batch: {len(runnable_cases)}")
        print(f"  reprocess mode: {self.config.get('automation_reprocess_mode', 'skip')}")
        # Echo the EFFECTIVE run config so the overnight/unattended log records
        # exactly which model / oldcam version / rppg state actually ran
        # (especially the headless --model/--oldcam-version/--rppg overrides).
        _eff_endpoint, _eff_display = resolve_cli_video_model(self.config)
        print(f"  video model: {_eff_endpoint or ''} ({_eff_display or ''})")
        print(f"  expand provider: front={self.config.get('automation_front_expand_provider', 'fal')} "
              f"selfie={self.config.get('automation_selfie_expand_provider', 'fal')}")
        print(f"  oldcam: enabled={self.config.get('automation_oldcam_enabled', True)} "
              f"versions={self._format_oldcam_versions()}")
        print(f"  rppg enabled: {self.config.get('automation_rppg_enabled', False)}")
        _eff_globs = self.config.get('automation_front_globs', []) or []
        if _eff_globs:
            print(f"  front globs: {_eff_globs}")
        print(f"  outpaint timeout (s): "
              f"{get_outpaint_fal_timeout_seconds(self.config)}")
        for line in self._automation_status_lines():
            print(f"  {line}")
        # Same MAIN-settings summary the interactive table shows (Req:
        # rPPG + exact oldcam versions visible in every preflight).
        self._print_run_settings_plain()
        selfie_slot, selfie_prompt, selfie_source = self._get_selected_selfie_prompt()
        prompt_preview = selfie_prompt if len(selfie_prompt) <= 160 else f"{selfie_prompt[:160]}..."
        print(f"  selfie prompt slot/source: {selfie_slot} / {selfie_source}")
        print(f"  selfie prompt preview: {prompt_preview}")

        # Under a real terminal, show the live Rich dashboard. Under cron / Task
        # Scheduler / a pipe (no TTY), render NOTHING live -- run the pipeline
        # directly in the main thread so the log isn't polluted with ANSI escape
        # codes + we skip the dashboard's polling thread (code-review Gemini, PR #69).
        if _is_tty:
            stats, run_error = self._run_with_live_dashboard(runner, runnable_cases, manifest)
        else:
            run_error = None
            try:
                # Strip any ANSI color codes an upstream tool might forward so a
                # cron / Task-Scheduler log stays clean (A3 — latent: no current
                # generator emits color, but oldcam forwards subprocess stdout raw).
                runner.progress_cb = lambda message, level="info": print(
                    f"  [{level}] {_ANSI_ESCAPE_RE.sub('', str(message))}"
                )
                stats = runner.run(runnable_cases)
            except Exception as exc:
                stats = {"completed": 0, "failed": 0, "manual_review": 0, "skipped": 0}
                run_error = str(exc)
        if run_error:
            _err(f"[batch] Automation run failed: {run_error}")
            return 1
        print("[batch] Automation run complete.")
        print(f"  completed: {stats.get('completed', 0)}")
        print(f"  failed: {stats.get('failed', 0)}")
        print(f"  manual_review: {stats.get('manual_review', 0)}")
        print(f"  skipped: {stats.get('skipped', 0)}")
        try:
            self._write_automation_summary(manifest, runner.last_case_results, stats)
        except Exception as exc:
            # A summary-write failure must not flip an otherwise-good run to a
            # non-zero exit; surface it but keep the run's verdict.
            _warn(f"[batch] Could not write run summary: {exc}")
        # A scheduled batch must treat BOTH failed and manual_review cases as
        # needs-attention (exit 2), not success -- otherwise manual_review cases
        # silently vanish from the operator's view (code-review HIGH-1, PR #69).
        # Exit 2 (ran-but-needs-attention) is distinct from exit 1 (could-not-run)
        # so a caller can tell "nothing ran" from "ran with problem cases".
        needs_attention = int(stats.get("failed", 0)) + int(stats.get("manual_review", 0))
        if needs_attention > 0:
            _warn(
                f"[batch] {needs_attention} case(s) need attention "
                f"(failed={stats.get('failed', 0)}, manual_review={stats.get('manual_review', 0)}); exiting 2."
            )
            return 2
        return 0

    # ------------------------------------------------------------------
    # Pre-run settings visibility (2026-06-11 mandate: a real batch run
    # fanned out to ALL oldcam versions with NO rPPG because nothing in
    # the option-1 flow surfaced those settings before approval).
    # ------------------------------------------------------------------

    def _pipeline_preview_text(self) -> str:
        """Read-only one-line summary of the resulting post-processing plan.

        Derived from the SAME shared planner the orchestrator uses
        (automation.postproc_plan) so the preview can never disagree with what
        actually runs. Example::

            Kling → rPPG → AA(prime) → Oldcam(v13)  ·  + 6 more variants (powerset)
        """
        from automation.postproc_plan import build_plan, plan_preview_line

        c = self.config
        rppg_on = _parse_bool_cfg(c.get("automation_rppg_enabled", False)) or False
        loop_on = _parse_bool_cfg(c.get("automation_loop_enabled", False)) or False
        oldcam_enabled = _parse_bool_cfg(c.get("automation_oldcam_enabled", True))
        oldcam_enabled = True if oldcam_enabled is None else oldcam_enabled
        oldcam_versions = self._selected_oldcam_versions() if oldcam_enabled else []
        plan = build_plan(
            rppg_enabled=rppg_on,
            loop_enabled=loop_on,
            crush_resolutions=self._selected_crush_resolutions(),
            aa_attacks=self._selected_aa_attacks(),
            oldcam_versions=oldcam_versions,
            mode=str(c.get("automation_postproc_fanout_mode", "separate_and_combined")),
        )
        return plan_preview_line(plan)

    def _run_settings_rows(self) -> List[Tuple[str, str, str]]:
        """(label, value, rich-style) rows for the MAIN run settings — the
        single source for the Rich table, the plain headless variant, and
        the quick editor's re-render."""
        c = self.config

        def _flag(key: str, default: bool = False) -> bool:
            # Same coercion the pipeline uses (_parse_bool, the repo's
            # canonical config-bool helper): a string "false" in a
            # hand-edited config must not display as ON while running as
            # off (Codex P3, PR #96). face_similarity is stdlib-light at
            # module level (TF/DeepFace are lazy inside _get_engine), so
            # this import is cheap.
            parsed = _parse_bool_cfg(c.get(key, default))
            return parsed if parsed is not None else bool(default)

        rppg_on = _flag("automation_rppg_enabled")
        loop_on = _flag("automation_loop_enabled")
        oldcam_required = _flag("automation_oldcam_required")
        oldcam_enabled = _flag("automation_oldcam_enabled", True)
        oldcam_display = self._format_oldcam_versions()
        if not oldcam_enabled:
            # Mirror the pipeline's _oldcam_active() truth: versions may be
            # selected but the stage itself disabled.
            oldcam_display = f"DISABLED (selection: {oldcam_display})"
        model_labels = self._selfie_model_label_map()
        # `or []`: an explicit null in a hand-edited config would make
        # list(None) crash the table render (Gemini MED, round 4).
        selfie_models = [model_labels.get(x, x) for x in list(c.get("automation_selfie_models") or [])]
        selfie_slot, _prompt, selfie_source = self._get_selected_selfie_prompt()
        front_provider = self._resolve_provider(str(c.get("automation_front_expand_provider", "auto")))
        selfie_provider = self._resolve_provider(str(c.get("automation_selfie_expand_provider", "auto")))
        default_composite = c.get("outpaint_composite_mode", "preserve_seamless")
        front_passes = c.get("automation_front_expand_passes", 2)
        video_endpoint, video_display = resolve_cli_video_model(c)
        rows = [
            ("rPPG injection", "ON" if rppg_on else "OFF — no pulse will be injected",
             "bold green" if rppg_on else "bold red"),
            ("Oldcam versions", f"{oldcam_display}  ({'required' if oldcam_required else 'optional'})",
             "bold red" if (oldcam_display == "none selected" or not oldcam_enabled)
             else ("bold yellow" if oldcam_display.startswith("all") else "bold green")),
            ("Loop (ping-pong)", "ON" if loop_on else "off", "green" if loop_on else "dim"),
            ("Quality crush", self._format_crush_resolutions(),
             "green" if self._selected_crush_resolutions() else "dim"),
            ("AA (adversarial)", self._format_aa_attacks(),
             "green" if self._selected_aa_attacks() else "dim"),
            ("Pipeline plan", self._pipeline_preview_text(), "cyan"),
            ("Video model", f"{video_display or video_endpoint or '?'}  ·  kling prompt slot {resolve_cli_kling_prompt_slot(c, DEFAULT_KLING_PROMPT_SLOT)}", ""),
            ("Selfie model(s)", f"{', '.join(selfie_models) if selfie_models else '(none)'}"
             + ("  ·  FAN-OUT: one full chain per model" if len(selfie_models) > 1 else ""),
             "bold yellow" if len(selfie_models) > 1 else ""),
            ("Selfie prompt", f"slot {selfie_slot} ({selfie_source})", ""),
            ("Step 0 front expand",
             f"{front_provider} · {c.get('automation_front_expand_mode', 'percent')} · "
             f"blend={c.get('automation_front_expand_composite_mode', default_composite)} · "
             f"{c.get('automation_front_expand_percent', 70)}% · "
             f"run {front_passes}x" + (" (2-pass)" if str(front_passes) == "2" else ""),
             ""),
            ("Step 0 crop factor", str(c.get("automation_crop_multiplier", 1.5)), ""),
            ("Step 2.5 selfie expand",
             f"{selfie_provider} · {c.get('automation_selfie_expand_mode', 'percent')} · "
             f"blend={c.get('automation_selfie_expand_composite_mode', 'none')} · "
             f"{c.get('automation_selfie_expand_percent', 30)}%",
             ""),
            ("Similarity threshold", str(c.get("automation_similarity_threshold", 80)), ""),
            ("Batch scope", f"max {self._read_max_cases_setting()} case(s) · reprocess={c.get('automation_reprocess_mode', 'skip')}", ""),
            ("Root folder", self._elide_path(self.automation_root_folder, 56) if self.automation_root_folder else "(not set)", "dim"),
        ]
        return rows

    @staticmethod
    def _styled_table(title: str, *, border_style: str = "blue",
                      show_header: bool = False, caption: "Optional[str]" = None) -> Table:
        """THE house style for every interactive Rich table (round 6 sweep:
        'all wherever this exists … should be beautified'): rounded box,
        blue border, bold-white title, dim caption. One constructor so no
        screen drifts back to the bare default look."""
        return Table(
            title=f"[bold white]{title}[/bold white]",
            caption=f"[dim]{caption}[/dim]" if caption else None,
            show_header=show_header,
            expand=False,
            box=_rich_box.ROUNDED,
            border_style=border_style,
            padding=(0, 1),
        )

    @staticmethod
    def _elide_path(path: "Any", max_len: int = 48) -> str:
        """Tail-elided path for labels/tables ('…\\client\\Batch_04') — the
        tail is the part humans recognize; full paths blow out layouts."""
        p = str(path or "")
        if len(p) <= max_len:
            return p
        return "…" + p[-(max_len - 1):]

    # Visual section breaks for the settings table — after these row labels
    # the table draws a divider, grouping: stage toggles / models+prompts /
    # image prep / scoring+scope. Labels (not indexes) so _run_settings_rows
    # can evolve without silently mis-grouping.
    _SETTINGS_TABLE_SECTION_AFTER = {"Loop (ping-pong)", "Pipeline plan", "Selfie prompt", "Step 2.5 selfie expand"}

    def _render_run_settings_table(self, title: str = "Main run settings",
                                   caption: "Optional[str]" = None) -> None:
        table = self._styled_table(f"⚙  {title}", caption=caption)
        table.add_column("Setting", style="cyan", no_wrap=True)
        table.add_column("Value")
        for label, value, style in self._run_settings_rows():
            # Values carry user text (model display names, paths) — escape so
            # brackets display instead of crashing the table render.
            safe_value = _rich_markup_escape(str(value))
            table.add_row(label, f"[{style}]{safe_value}[/{style}]" if style else safe_value)
            if label in self._SETTINGS_TABLE_SECTION_AFTER:
                table.add_section()
        _RICH_CONSOLE.print(table)

    def _print_run_settings_plain(self) -> None:
        """Plain-text variant for headless --batch / non-TTY preflight."""
        print("\nMain run settings:")
        for label, value, _style in self._run_settings_rows():
            print(f"  {label}: {value}")

    def _show_full_prompts(self) -> None:
        """Show the COMPLETE selfie + kling video prompts (the table only
        shows slot numbers; the user must be able to read the full text
        before paying for a batch)."""
        if not self._use_legacy_prompt_ui():
            # Branded repaint — callers pause + repaint their own screen after.
            self.display_header()
        selfie_slot, selfie_prompt, selfie_source = self._get_selected_selfie_prompt()
        # markup-escape: bracketed prompt text must DISPLAY, not get parsed
        # as rich style tags (and "[/x]" would crash with MarkupError).
        _RICH_CONSOLE.print(Panel(
            _rich_markup_escape(selfie_prompt) if selfie_prompt else "(empty)",
            title=f"Selfie prompt — slot {selfie_slot} ({selfie_source})",
            border_style="cyan",
        ))
        kling_slot = str(resolve_cli_kling_prompt_slot(self.config, DEFAULT_KLING_PROMPT_SLOT))
        kling_prompt = str(self.config.get("saved_prompts", {}).get(kling_slot, "") or "")
        kling_negative = str(self.config.get("negative_prompts", {}).get(kling_slot, "") or "")
        _RICH_CONSOLE.print(Panel(
            _rich_markup_escape(kling_prompt) if kling_prompt else "(empty)",
            title=f"Kling video prompt — slot {kling_slot}",
            border_style="magenta",
        ))
        _RICH_CONSOLE.print(Panel(
            _rich_markup_escape(kling_negative) if kling_negative else "(empty)",
            title=f"Kling negative prompt — slot {kling_slot}",
            border_style="red",
        ))

    def _qs_pick_selfie_models(self) -> None:
        """Selfie model-set picker (preset or custom endpoints). Shared by
        the settings-editor Selfie section and the option-1 quick editor.
        Raises _QuestionarySectionAbort on Esc."""
        current_models = list(self.config.get("automation_selfie_models", []))
        current_label = ", ".join(current_models) if current_models else "(none)"
        preset = questionary.select(
            "Selfie model set:",
            qmark="◆",
            instruction=f"(current: {current_label} · multiple models = one full chain per model)",
            choices=[
                questionary.Choice("Nano Banana 2 Edit only", "nano"),
                questionary.Choice("GPT Image 2 Edit only", "gpt"),
                questionary.Choice("Both (Nano Banana + GPT Image 2) — fan-out", "both"),
                questionary.Choice("Custom comma-separated endpoints", "custom"),
                questionary.Choice("Keep current", "_keep"),
            ],
            default="_keep",
            style=KLING_QUESTIONARY_STYLE,
        ).ask()
        preset = _qs_or_abort(preset)  # B1: Esc aborts the section, not silent fall-through
        if preset == "nano":
            self.config["automation_selfie_models"] = ["fal-ai/nano-banana-2/edit"]
        elif preset == "gpt":
            self.config["automation_selfie_models"] = ["openai/gpt-image-2/edit"]
        elif preset == "both":
            self.config["automation_selfie_models"] = ["fal-ai/nano-banana-2/edit", "openai/gpt-image-2/edit"]
        elif preset == "custom":
            raw = questionary.text(
                "Custom endpoints (comma-separated):",
                qmark="◆",
                style=KLING_QUESTIONARY_STYLE,
            ).ask()
            raw = _qs_or_abort(raw)  # B2: Esc aborts rather than falling through
            if raw:
                models = [m.strip() for m in raw.split(",") if m.strip()]
                if models:
                    self.config["automation_selfie_models"] = models

    def _gui_cli_comparison_rows(self) -> "List[Dict[str, Any]]":
        """Structured rows comparing the Tkinter GUI's settings with the CLI
        pipeline's per-surface settings (same kling_config.json, different
        keys after the 2026-06-11 split).

        status: "same" / "differs" for the per-surface rows (model, slot
        pointer, duration); "shared" for single-value rows used by BOTH
        surfaces (prompt text, motion knobs) — shown so the user can see
        exactly what IS shared."""
        c = self.config
        gui_endpoint = str(c.get("current_model") or "")
        gui_model = str(c.get("model_display_name") or gui_endpoint or "(not set)")
        cli_endpoint, cli_display = resolve_cli_video_model(c)
        cli_model = str(cli_display or cli_endpoint or "(not set)")
        gui_slot = c.get("current_prompt_slot", DEFAULT_KLING_PROMPT_SLOT)
        cli_slot = resolve_cli_kling_prompt_slot(c, DEFAULT_KLING_PROMPT_SLOT)
        gui_duration = c.get("video_duration", 10)
        cli_duration = resolve_cli_video_duration(c)
        rows: "List[Dict[str, Any]]" = [
            {
                "id": "video_model", "label": "Video model",
                "gui": gui_model, "cli": cli_model,
                "status": "same" if gui_endpoint == str(cli_endpoint or "") else "differs",
            },
            {
                "id": "kling_prompt_slot", "label": "Kling prompt slot (pointer)",
                "gui": str(gui_slot), "cli": str(cli_slot),
                "status": "same" if str(gui_slot) == str(cli_slot) else "differs",
            },
            {
                "id": "video_duration", "label": "Video duration (s)",
                "gui": str(gui_duration), "cli": str(cli_duration),
                "status": "same" if str(gui_duration) == str(cli_duration) else "differs",
            },
        ]
        slot_key = str(cli_slot)
        prompt_text = str((c.get("saved_prompts") or {}).get(slot_key, "") or "")
        preview = (prompt_text[:60] + "…") if len(prompt_text) > 60 else (prompt_text or "(empty)")
        for row_id, label, value in (
            ("kling_prompt_text", f"Kling prompt text (slot {slot_key})", preview),
            ("cfg_scale", "CFG scale", str(c.get("cfg_scale_value", 0.7))),
            ("lock_end_frame", "Lock end frame", "ON" if c.get("lock_end_frame", True) else "off"),
            ("outpaint_composite", "Outpaint blend default", str(c.get("outpaint_composite_mode", "preserve_seamless"))),
        ):
            rows.append({"id": row_id, "label": label, "gui": value, "cli": value, "status": "shared"})
        return rows

    def _adopt_gui_settings(self, row_ids: "List[str]") -> None:
        """Copy the selected GUI values into the CLI pipeline's cli_* keys.

        STRICTLY one-directional — no GUI key is ever written here.
        Divergence is a supported state (the per-surface keys exist exactly
        so CLI settings don't mess with GUI settings); this is the explicit
        convergence shortcut."""
        c = self.config
        if "video_model" in row_ids:
            c["cli_video_model"] = str(c.get("current_model") or "")
            c["cli_video_model_display_name"] = str(c.get("model_display_name") or "")
            c["cli_video_duration"] = c.get("video_duration", 10)
            self._cached_price = None
        if "video_duration" in row_ids and "video_model" not in row_ids:
            c["cli_video_duration"] = c.get("video_duration", 10)
        if "kling_prompt_slot" in row_ids:
            c["cli_kling_prompt_slot"] = c.get("current_prompt_slot", DEFAULT_KLING_PROMPT_SLOT)
        self.save_config()

    def _compare_gui_settings_menu(self) -> None:
        """Side-by-side GUI ⇄ CLI settings view with one-press adopt."""
        if self._use_legacy_prompt_ui():
            print("\nGUI vs CLI pipeline settings:")
            for row in self._gui_cli_comparison_rows():
                print(f"  {row['label']}: GUI={row['gui']} | CLI={row['cli']} ({row['status']})")
            self.pause_review("\nPress Enter to continue...")
            return
        while True:
            self.display_header()  # clears screen — stable repaint, no stacking
            rows = self._gui_cli_comparison_rows()
            table = self._styled_table("🔄  GUI ⇄ CLI pipeline settings", show_header=True)
            table.add_column("Setting", style="cyan", no_wrap=True)
            table.add_column("GUI (Tkinter)")
            table.add_column("CLI pipeline")
            table.add_column("")
            markers = {
                "same": "[green]✔ same[/green]",
                "differs": "[yellow]≠ differs[/yellow]",
                "shared": "[dim]shared[/dim]",
            }
            for row in rows:
                # Values can carry prompt-text previews — escape so brackets
                # display instead of being parsed as rich markup.
                table.add_row(
                    row["label"],
                    _rich_markup_escape(str(row["gui"])),
                    _rich_markup_escape(str(row["cli"])),
                    markers[row["status"]],
                )
            _RICH_CONSOLE.print(table)
            _RICH_CONSOLE.print(
                "[dim]Shared rows are one value used by both surfaces. Adopting copies "
                "GUI → CLI only — your GUI settings are never changed from here.[/dim]"
            )
            differing = [r for r in rows if r["status"] == "differs"]
            choice = self._q_select(
                "Compare with GUI:",
                [
                    ("⬇  Adopt ALL GUI values into the CLI pipeline", "adopt_all"),
                    ("☑  Adopt selected settings… (spacebar)", "adopt_some"),
                    ("↩  Back", "back"),
                ],
            )
            if choice in (None, "back"):
                return
            if choice == "adopt_all":
                self._adopt_gui_settings([r["id"] for r in rows if r["status"] != "shared"])
                self.print_green("✓ CLI pipeline settings now match the GUI.")
                time.sleep(0.8)
            elif choice == "adopt_some":
                if not differing:
                    self.print_yellow("Nothing differs — the CLI already matches the GUI.")
                    time.sleep(0.8)
                    continue
                try:
                    picks = questionary.checkbox(
                        "Adopt which GUI values?",
                        qmark="◆",
                        choices=[
                            questionary.Choice(f"{r['label']}: {r['gui']}  (CLI now: {r['cli']})", r["id"], checked=True)
                            for r in differing
                        ],
                        style=KLING_QUESTIONARY_STYLE,
                    ).ask()
                except (KeyboardInterrupt, EOFError):
                    picks = None
                if picks:
                    self._adopt_gui_settings(list(picks))
                    self.print_green(f"✓ Adopted {len(picks)} setting(s) from the GUI.")
                    time.sleep(0.8)

    def _quick_edit_choice_pairs(self) -> "List[Tuple[str, str]]":
        """(label, value) entries for the quick editor — single source so the
        menu and the structure test can never drift. One entry per FIELD,
        each label carrying its current value (round 3: "configurable right
        then and there… not having to go into a sub menu for each"), grouped
        chronologically by pipeline step via _QUICK_EDIT_GROUPS."""
        c = self.config
        # _parse_bool_cfg (not raw bool): a hand-edited "false" string must
        # show OFF here exactly like the table does, or the label and the
        # toggle disagree (code-reviewer MED, PR #96 round 2).
        rppg_on = _parse_bool_cfg(c.get("automation_rppg_enabled", False)) or False
        loop_on = _parse_bool_cfg(c.get("automation_loop_enabled", False)) or False
        video_endpoint, video_display = resolve_cli_video_model(c)
        kling_slot = resolve_cli_kling_prompt_slot(c, DEFAULT_KLING_PROMPT_SLOT)
        kling_text = str((c.get("saved_prompts") or {}).get(str(kling_slot), "") or "")
        try:
            selfie_slot = int(c.get("automation_selfie_prompt_slot", DEFAULT_AUTOMATION_SELFIE_PROMPT_SLOT))
        except (TypeError, ValueError):
            selfie_slot = DEFAULT_AUTOMATION_SELFIE_PROMPT_SLOT
        selfie_text = str((c.get("automation_selfie_prompts") or {}).get(str(selfie_slot), "") or "")
        selfie_labels = self._selfie_model_label_map()
        selfie_models = [selfie_labels.get(x, x) for x in list(c.get("automation_selfie_models") or [])]

        def _preview(text: str, n: int = 38) -> str:
            return (text[:n] + "…") if len(text) > n else (text or "(empty)")

        return [
            # ── Step 0 · Front prep ──
            (f"🖼  Front expand provider: {c.get('automation_front_expand_provider', 'fal')}", "front_provider"),
            (f"🖼  Front expand blend: {c.get('automation_front_expand_composite_mode', 'preserve_seamless')}", "front_blend"),
            (f"🖼  Front expand percent: {c.get('automation_front_expand_percent', 70)}%", "front_percent"),
            (f"🖼  Front expand passes: {c.get('automation_front_expand_passes', 2)}x", "front_passes"),
            (f"👤 Crop factor: {c.get('automation_crop_multiplier', 1.5)}", "crop"),
            # ── Step 2 · Selfie ──
            (f"✨ Selfie model set: {', '.join(selfie_models) if selfie_models else '(none)'}", "selfie_models"),
            (f"📝 Selfie prompt: slot {selfie_slot} · \"{_preview(selfie_text)}\"", "selfie_prompt"),
            (f"🎯 Similarity threshold: {c.get('automation_similarity_threshold', 80)}", "similarity"),
            # ── Step 2.5 · Selfie expand ──
            (f"➕ Selfie expand provider: {c.get('automation_selfie_expand_provider', 'fal')}", "sexp_provider"),
            (f"➕ Selfie expand blend: {c.get('automation_selfie_expand_composite_mode', 'none')}", "sexp_blend"),
            (f"➕ Selfie expand percent: {c.get('automation_selfie_expand_percent', 30)}%", "sexp_percent"),
            # ── Step 3 · Video (Kling) ──
            (f"🎬 Video model: {video_display or video_endpoint or '(not set)'}", "video_model"),
            (f"📝 Kling prompt: slot {kling_slot} · \"{_preview(kling_text)}\"", "kling_prompt"),
            # ── Post · rPPG → Loop → Crush → Oldcam ──
            (f"💉 rPPG injection: {'ON' if rppg_on else 'OFF'} — toggle", "rppg"),
            (f"🔁 Loop (ping-pong): {'ON' if loop_on else 'off'} — toggle", "loop"),
            (f"💥 Crush (quality-destroy): {self._format_crush_resolutions()} — pick (spacebar)", "crush"),
            (f"🛡️  AA (adversarial pass): {self._format_aa_attacks()} — pick (spacebar)", "aa"),
            (f"📼 Oldcam versions: {self._format_oldcam_versions()} — pick (spacebar)", "oldcam"),
            (f"🧩 Output mode: {self._format_fanout_mode()} — toggle", "fanout_mode"),
            # ── Run scope ──
            (f"📦 Max cases per run: {self._read_max_cases_setting()}", "batch_max"),
            (f"📦 Reprocess mode: {c.get('automation_reprocess_mode', 'skip')}", "batch_reprocess"),
            (f"📂 Root folder: {self._elide_path(self.automation_root_folder, 44) if self.automation_root_folder else '(not set)'}", "root"),
            # ── ungrouped actions ──
            ("📜 View FULL prompts (selfie + video)", "prompts"),
            ("⚙️  All settings (full editor)…", "all"),
            ("💾 Done (save and return)", "done"),
        ]

    # Chronological pipeline-step grouping for the quick editor (mirrors the
    # Tkinter step tabs; Post group ordered rPPG → Loop → Oldcam = the actual
    # Phase-E chain). The renderer interleaves Separator headers + blank-line
    # spacers exactly like the main menu.
    _QUICK_EDIT_GROUPS = (
        ("Step 0 · Front prep", ("front_provider", "front_blend", "front_percent", "front_passes", "crop")),
        ("Step 2 · Selfie", ("selfie_models", "selfie_prompt", "similarity")),
        ("Step 2.5 · Selfie expand", ("sexp_provider", "sexp_blend", "sexp_percent")),
        ("Step 3 · Video (Kling)", ("video_model", "kling_prompt")),
        ("Post · rPPG → Loop → Crush → AA → Oldcam", ("rppg", "loop", "crush", "aa", "oldcam", "fanout_mode")),
        ("Run scope", ("batch_max", "batch_reprocess", "root")),
        (None, ("prompts", "all", "done")),
    )

    def _quick_pick_cli_video_model(self) -> None:
        """Inline automation-model picker for the quick editor — a single
        _q_select, no full-page Model Selection screen (round 3)."""
        current_endpoint, current_display = resolve_cli_video_model(self.config)
        choices: "List[Tuple[str, str]]" = []
        for name, endpoint, _duration in self._MODEL_PRESETS:
            active = "  ◄" if endpoint == (current_endpoint or "") else ""
            choices.append((f"{name}{active}", endpoint))
        choices.append(("➕ Custom endpoint…", "__custom__"))
        choices.append(("🌐 Fetch all models from fal.ai…", "__fetch__"))
        choices.append(("↩ Keep current", "__keep__"))
        pick = self._q_select(
            "Automation video model:",
            choices,
            instruction=f"(current: {current_display or current_endpoint or '(not set)'})",
        )
        if pick in (None, "__keep__"):
            return
        if pick == "__custom__":
            endpoint = self._q_text(
                "fal.ai endpoint id (e.g. fal-ai/kling-video/v2.5-turbo/standard/image-to-video):"
            )
            if not endpoint or not endpoint.strip():
                return
            endpoint = endpoint.strip()
            name = self._q_text("Display name:", default=endpoint) or endpoint
            dur_raw = self._q_text("Video duration seconds (5/10/15):", default="10")
            duration = int(dur_raw) if dur_raw and dur_raw.strip().isdigit() else 10
            if duration not in _COMMON_VIDEO_DURATIONS:
                self.print_yellow(f"⚠ Uncommon duration {duration}s — verify the model supports it.")
            self._apply_model_choice(name.strip(), endpoint, duration, target="cli")
            return
        if pick == "__fetch__":
            return self._select_model_fetch_all_legacy(target="cli")
        for name, endpoint, duration in self._MODEL_PRESETS:
            if endpoint == pick:
                self._apply_model_choice(name, endpoint, duration, target="cli")
                return

    def _quick_edit_settings(self) -> None:
        """Grouped, value-bearing quick editor: every field visible with its
        current value, one Enter = one inline edit, repainted (banner + list)
        after each change. No settings table here — each row IS its value
        (round 3: the stacked banner+table+table screen "looks like a mess").
        Persists on exit."""
        if self._use_legacy_prompt_ui():
            # Non-TTY: the grouped legacy walker already covers everything.
            self._edit_automation_settings()
            return
        while True:
            self.display_header()  # clears screen — stable repaint, no stacking
            c = self.config
            rppg_on = _parse_bool_cfg(c.get("automation_rppg_enabled", False)) or False
            loop_on = _parse_bool_cfg(c.get("automation_loop_enabled", False)) or False
            by_value = {v: label for label, v in self._quick_edit_choice_pairs()}
            # Control hints live on their OWN line below the title (a leading
            # Separator row — the only way to render text under a questionary
            # message), then a blank line before the first step group
            # (user feedback, round 5).
            grouped: "List[Any]" = [
                questionary.Separator("   ↑/↓ move · Enter edits one field · Esc saves and returns"),
                questionary.Separator(" "),
            ]
            for group_title, values in self._QUICK_EDIT_GROUPS:
                if group_title is not None:
                    grouped.append(questionary.Separator(f"  ── {group_title} ──"))
                grouped.extend(questionary.Choice(by_value[v], v) for v in values)
                grouped.append(questionary.Separator(" "))
            grouped.pop()  # no trailing spacer
            choice = self._q_select(
                "Quick settings",
                grouped,
                instruction=" ",
            )
            if choice in (None, "done"):
                self.save_config()
                return
            try:
                # ── Step 0 · Front prep ──
                if choice == "front_provider":
                    self._qs_choice("Front expand provider:", "automation_front_expand_provider",
                                    choices=_EXPAND_PROVIDER_OPTIONS, default="fal")
                elif choice == "front_blend":
                    self._qs_choice("Front expand blend (composite) mode:", "automation_front_expand_composite_mode",
                                    choices=_COMPOSITE_MODE_OPTIONS, default="preserve_seamless")
                elif choice == "front_percent":
                    self._qs_int("Front expand percent:", "automation_front_expand_percent",
                                 default=70, validator=lambda v: v >= 0)
                elif choice == "front_passes":
                    self._qs_choice("Run front expansion how many times (passes):", "automation_front_expand_passes",
                                    choices=["1", "2"], default="2", cast_fn=int)
                elif choice == "crop":
                    self._qs_float("Crop multiplier (extraction factor):", "automation_crop_multiplier",
                                   default=1.5, validator=lambda v: v > 0)
                # ── Step 2 · Selfie ──
                elif choice == "selfie_models":
                    self._qs_pick_selfie_models()
                elif choice == "selfie_prompt":
                    self._prompt_slot_browser("selfie")
                elif choice == "similarity":
                    self._qs_int("Similarity threshold (0-100):", "automation_similarity_threshold",
                                 default=80, validator=lambda v: 0 <= v <= 100)
                # ── Step 2.5 · Selfie expand ──
                elif choice == "sexp_provider":
                    self._qs_choice("Selfie expand provider:", "automation_selfie_expand_provider",
                                    choices=_EXPAND_PROVIDER_OPTIONS, default="fal")
                elif choice == "sexp_blend":
                    self._qs_choice("Selfie expand blend (composite) mode:", "automation_selfie_expand_composite_mode",
                                    choices=_COMPOSITE_MODE_OPTIONS, default="none")
                elif choice == "sexp_percent":
                    self._qs_int("Selfie expand percent:", "automation_selfie_expand_percent",
                                 default=30, validator=lambda v: v >= 0)
                # ── Step 3 · Video (Kling) ──
                elif choice == "video_model":
                    # Inline picker writing the CLI per-surface keys — never
                    # the GUI / manual-tools model.
                    self._quick_pick_cli_video_model()
                elif choice == "kling_prompt":
                    self._prompt_slot_browser("kling")
                # ── Post · rPPG → Loop → Crush → Oldcam ──
                elif choice == "rppg":
                    c["automation_rppg_enabled"] = not rppg_on
                    print(f"  rPPG injection -> {'ON' if c['automation_rppg_enabled'] else 'OFF'}")
                elif choice == "loop":
                    c["automation_loop_enabled"] = not loop_on
                    print(f"  Loop -> {'ON' if c['automation_loop_enabled'] else 'off'}")
                elif choice == "crush":
                    self._qs_pick_crush_resolutions()
                elif choice == "aa":
                    self._qs_pick_aa_attacks()
                elif choice == "oldcam":
                    self._qs_pick_oldcam_versions()
                elif choice == "fanout_mode":
                    self._qs_pick_fanout_mode()
                # ── Run scope ──
                elif choice == "batch_max":
                    self._qs_choice("Max cases per run:", "automation_max_cases_per_run",
                                    choices=["1", "5", "10", "all"], default="5")
                elif choice == "batch_reprocess":
                    self._qs_choice("Reprocess mode:", "automation_reprocess_mode",
                                    choices=_REPROCESS_MODE_OPTIONS, default="skip")
                    # _effective_reprocess_mode forces "skip" unless
                    # automation_allow_reprocess is set — a quick-picked
                    # overwrite/increment must actually take effect (Codex P2).
                    c["automation_allow_reprocess"] = (
                        str(c.get("automation_reprocess_mode", "skip")).strip().lower()
                        in ("overwrite", "increment")
                    )
                elif choice == "root":
                    self._select_automation_root()
                # ── actions ──
                elif choice == "prompts":
                    self._show_full_prompts()
                    self.pause_review("Press Enter to continue...")
                elif choice == "all":
                    self._edit_automation_settings()
            except _QuestionarySectionAbort:
                continue  # Esc inside a field -> back to the quick menu
            self.save_config()

    def _prompt_slot_browser(self, kind: str) -> None:
        """Prompt slot BROWSER (round 6): the active slot's COMPLETE text
        stays visible in a panel while you navigate; Enter on a slot
        activates it (revealing its full text), with in-place edit and an
        explicit Back row. Replaces the blind preview-only pickers — "if
        we're showing only the first sentence … how can we even know which
        prompt is what".

        kind="kling": shared saved_prompts/negative_prompts TEXT (by design,
        GUI ⇄ CLI) + the per-surface cli_kling_prompt_slot pointer.
        kind="selfie": automation_selfie_prompts + automation_selfie_prompt_slot.
        """
        while True:
            self.display_header()
            if kind == "kling":
                prompts = self.config.get("saved_prompts") or {}
                negatives = self.config.get("negative_prompts") or {}
                active = resolve_cli_kling_prompt_slot(self.config, DEFAULT_KLING_PROMPT_SLOT)
                label = "Kling video prompt"
            else:
                self._ensure_selfie_prompt_slots()
                prompts = self.config.get("automation_selfie_prompts") or {}
                negatives = None
                try:
                    active = int(self.config.get("automation_selfie_prompt_slot", DEFAULT_AUTOMATION_SELFIE_PROMPT_SLOT))
                except (TypeError, ValueError):
                    active = DEFAULT_AUTOMATION_SELFIE_PROMPT_SLOT
                label = "Selfie prompt"
            full = str(prompts.get(str(active), "") or "")
            # markup-escape the USER text: rich eats "[bracketed]" segments
            # as style tags and a literal "[/x]" raises MarkupError — the
            # whole point of this browser is showing the COMPLETE prompt
            # (adversarial review, round 7).
            _RICH_CONSOLE.print(Panel(
                _rich_markup_escape(full) if full else "[dim](empty — pick '✏  Edit' below to write one)[/dim]",
                title=f"[bold white]{label} — slot {active} (ACTIVE)[/bold white]",
                border_style="magenta" if kind == "kling" else "cyan",
            ))
            if negatives is not None:
                neg_full = str(negatives.get(str(active), "") or "")
                if neg_full:
                    _RICH_CONSOLE.print(Panel(
                        _rich_markup_escape(neg_full),
                        title=f"[bold white]Negative prompt — slot {active}[/bold white]",
                        border_style="red",
                    ))
            print()
            choices: "List[Any]" = []
            for i in range(1, _PROMPT_SLOT_COUNT + 1):
                text = str(prompts.get(str(i), "") or "")
                preview = (text[:58] + "…") if len(text) > 58 else (text or "(empty)")
                marker = "  ◄ ACTIVE" if i == active else ""
                choices.append((f"[{i}] {preview}{marker}", f"slot:{i}"))
            choices.append(questionary.Separator(" "))
            choices.append((f"✏  Edit slot {active} text", "edit"))
            if negatives is not None:
                choices.append((f"✏  Edit slot {active} NEGATIVE prompt", "edit_neg"))
            choices.append(("↩  Back", "back"))
            pick = self._q_select(
                f"{label} slots — Enter activates a slot (full text shows above):",
                choices,
            )
            if pick in (None, "back"):
                return
            if pick == "edit":
                new_text = self._q_text(f"{label} for slot {active}:", default=full)
                if new_text is not None:
                    prompts[str(active)] = new_text
                    if kind == "kling":
                        self.config["saved_prompts"] = prompts
                    else:
                        self.config["automation_selfie_prompts"] = prompts
                    self.save_config()
            elif pick == "edit_neg" and negatives is not None:
                neg_now = str(negatives.get(str(active), "") or "")
                new_neg = self._q_text(f"Negative prompt for slot {active}:", default=neg_now)
                if new_neg is not None:
                    negatives[str(active)] = new_neg
                    self.config["negative_prompts"] = negatives
                    self.save_config()
            elif pick.startswith("slot:"):
                new_slot = int(pick.split(":", 1)[1])
                if kind == "kling":
                    # Per-surface pointer — never the GUI's current_prompt_slot.
                    self.config["cli_kling_prompt_slot"] = new_slot
                else:
                    self.config["automation_selfie_prompt_slot"] = new_slot
                self.save_config()

    def _qs_section_selfie_prompt_only(self) -> None:
        """The selfie prompt slot/edit sub-flow, runnable standalone from the
        quick editor (extracted behavior parity with _qs_section_selfie)."""
        self._ensure_selfie_prompt_slots()
        current_slot = int(self.config.get("automation_selfie_prompt_slot", DEFAULT_AUTOMATION_SELFIE_PROMPT_SLOT))
        current_prompt = str(self.config.get("automation_selfie_prompts", {}).get(str(current_slot), "") or "")
        preview = (current_prompt[:80] + "...") if len(current_prompt) > 80 else current_prompt
        action = self._q_select(
            "Selfie prompt:",
            [
                ("👁  View full prompt", "view"),
                ("🔢 Switch to a different slot (1-10)", "switch"),
                ("✏  Edit the active prompt text", "edit"),
                ("↩  Keep as-is", "_keep"),
            ],
            instruction=f"(slot {current_slot}: \"{preview}\")",
        )
        if action == "view":
            self._show_full_prompts()
            self.pause_review("Press Enter to continue...")
        elif action == "switch":
            slot_raw = self._q_text(
                "New slot (1-10):",
                validate=lambda t: (
                    True if (not t.strip() or (t.strip().isdigit() and 1 <= int(t.strip()) <= 10))
                    else "Please enter a number between 1 and 10."
                ),
            )
            if slot_raw and slot_raw.strip():
                self.config["automation_selfie_prompt_slot"] = int(slot_raw.strip())
        elif action == "edit":
            new_prompt = self._q_text("New prompt text:", default=current_prompt)
            if new_prompt is not None:
                self.config["automation_selfie_prompts"][str(current_slot)] = new_prompt

    def _resume_intelligence_lines(self, records, manifest: AutomationManifest) -> List[str]:
        """Human lines describing what a Run/Resume will actually do with
        partially-processed cases (which step each in-progress case resumes
        at) — manifest-based, surfaced BEFORE approval."""
        lines: List[str] = []
        cases = manifest.data.get("cases", {})
        resume_points: Dict[str, int] = {}
        for record in records:
            entry = cases.get(record.relative_key)
            if not entry:
                continue
            if entry.get("status") in {"complete", "skipped"}:
                continue
            steps = entry.get("steps", {})
            ran_any = any(
                (steps.get(name, {}) or {}).get("status") not in (None, "pending")
                for name in steps
            )
            if not ran_any:
                continue
            next_step = next(
                (name for name in STEP_NAMES
                 if (steps.get(name, {}) or {}).get("status") in (None, "pending", "running", "failed")),
                None,
            )
            label = next_step or "(re-validate)"
            resume_points[label] = resume_points.get(label, 0) + 1
        for step, count in sorted(resume_points.items()):
            lines.append(f"{count} case(s) resume at: {step}")
        return lines

    def _run_resume_automation(self):
        if not self.automation_root_folder:
            self.print_red("Set automation root folder first.")
            self.pause_review("Press Enter to continue...")
            return

        root = Path(self.automation_root_folder)
        if not root.exists():
            self.print_red("Automation root path does not exist.")
            self.pause_review("Press Enter to continue...")
            return
        if self._use_legacy_prompt_ui():
            return self._run_resume_automation_legacy(root)
        return self._run_resume_automation_interactive(root)

    def _discover_and_load_manifest(self, root: Path, *, interactive: bool):
        """Shared discover + manifest load. Returns (records, manifest) or
        (None, None) after printing the reason. The interactive path offers
        the fingerprint-mismatch RECREATE prompt (the legacy/non-TTY path
        keeps the historical hard-error so tests/cron behavior is
        unchanged)."""
        records = discover_case_folders(
            root,
            self.config.get("automation_front_names", []),
            front_globs=self.config.get("automation_front_globs", []),
            # Surface glob warnings (invalid pattern etc.) in interactive
            # runs too — they were silently dropped (Gemini, PR #96 r3).
            warn_cb=self.print_yellow,
        )
        if not records:
            self.print_yellow("No case folders found.")
            self.pause_review("Press Enter to continue...")
            return None, None
        manifest = self._load_manifest_with_recreate_prompt(root, interactive=interactive)
        if manifest is None:
            return None, None
        return records, manifest

    def _load_manifest_with_recreate_prompt(
        self, root: Path, *, interactive: bool, single_case: bool = False
    ) -> "Optional[AutomationManifest]":
        """create_or_load with the interactive fingerprint-mismatch
        backup-and-recreate prompt. Extracted from _discover_and_load_manifest
        (round 9) so the SINGLE-case flow shares the exact same manifest
        semantics; messages/pauses preserved verbatim. The manifest path is
        derived from the `root` PARAMETER (not the mutable
        self.automation_root_folder) so a mid-flow root change cannot point
        path and root_dir at different folders. ``single_case`` reframes the
        recreate prompt: superseding the manifest resets the recorded state
        for EVERY case under root, which deserves an explicit warning (and a
        No default) on a screen framed as a one-case run."""
        snapshot = {k: v for k, v in self.config.items() if str(k).startswith("automation_")}
        try:
            return AutomationManifest.create_or_load(
                manifest_path=self._automation_manifest_path(root),
                root_dir=root,
                config_snapshot=snapshot,
            )
        except ValueError as exc:
            if interactive and "fingerprint mismatch" in str(exc):
                # OUTPUT-AFFECTING settings changed since the manifest was
                # created (run-scope knobs like max-cases are excluded from
                # the fingerprint and never land here — automation/manifest
                # _FINGERPRINT_EXCLUDED_KEYS). Offer the same backup-and-
                # recreate path headless --batch already has.
                scope_note = (
                    "\n[yellow]This manifest tracks ALL case folders under "
                    f"{_rich_markup_escape(self._elide_path(str(root), 60))} — recreating it resets the "
                    "recorded run state for every one of them (files on disk are untouched).[/yellow]"
                    if single_case
                    else ""
                )
                _RICH_CONSOLE.print(Panel(
                    "[yellow]Run settings changed since this manifest was created — "
                    "existing outputs may not match the new settings.[/yellow]"
                    + scope_note + "\n"
                    f"[dim]{_rich_markup_escape(str(exc))}[/dim]",
                    title="[bold yellow]⚠  Manifest mismatch[/bold yellow]",
                    border_style="yellow",
                ))
                if self._confirm(
                    "Back up the old manifest and start fresh with the new settings?",
                    default=not single_case,
                ):
                    fresh = AutomationManifest.create_fresh(
                        manifest_path=self._automation_manifest_path(root),
                        root_dir=root,
                        config_snapshot=snapshot,
                    )
                    print("  Old manifest backed up (.superseded.*); fresh manifest created.")
                    return fresh
                self.print_yellow("Run cancelled (manifest unchanged).")
                self.pause_review("Press Enter to continue...")
                return None
            self.print_red(f"Failed to load manifest: {exc}")
            self.pause_review("Press Enter to continue...")
            return None
        except Exception as exc:
            self.print_red(f"Failed to load manifest: {exc}")
            self.pause_review("Press Enter to continue...")
            return None

    def _resolve_single_case_target(self, raw: str) -> "Tuple[Optional[CaseRecord], str]":
        """Turn a user-picked path into a single CaseRecord (round 9).

        Accepts EITHER a folder (the front image is located inside it with
        the exact same matching semantics as batch discovery — names +
        image-only globs, first sorted match, multi-match warning) OR an
        image file used directly as the front. Returns (record, "") on
        success or (None, reason)."""
        path = Path(str(raw).strip().strip('"').strip("'")).expanduser()
        try:
            path = path.resolve()
        except OSError:
            return None, f"Path is not accessible: {path}"
        if not path.exists():
            return None, f"Path does not exist: {path}"
        if path.is_file():
            if path.suffix.lower() not in VALID_EXTENSIONS:
                return None, (
                    f"'{path.name}' is not an image file ({path.suffix or 'no extension'}) — "
                    "pick a png/jpg/jpeg/webp/bmp."
                )
            case_dir = path.parent
            if not case_dir.name:
                # Image sitting at a filesystem root: there is no folder to
                # act as the case (and "C:\\" would become a nonsense
                # manifest key). Ask for a real folder instead.
                return None, (
                    f"'{path.name}' sits at a filesystem root — put it in a folder "
                    "and pick that (the folder becomes the case)."
                )
            return CaseRecord(case_dir=case_dir, front_path=path, relative_key=case_dir.name), ""
        front = find_front_in_dir(
            path,
            self.config.get("automation_front_names", []),
            front_globs=self.config.get("automation_front_globs", []),
            warn_cb=self.print_yellow,
        )
        if front is None:
            names = ", ".join(self.config.get("automation_front_names", []) or []) or "(none configured)"
            globs = self.config.get("automation_front_globs", []) or []
            hint = f" or globs {globs}" if globs else ""
            return None, f"No front image found in {path} (looked for: {names}{hint})."
        rel_key = path.name or str(path)
        return CaseRecord(case_dir=path, front_path=front, relative_key=rel_key), ""

    def _run_single_case(self) -> None:
        """🎯 Single-case mode (round 9): run the FULL pipeline on exactly one
        folder (front image auto-located inside) or one image file picked
        directly — no batch root required."""
        if self._use_legacy_prompt_ui():
            raw = self._safe_input("Folder or front-image path for the single run: ").strip()
            if not raw:
                return
            record, err = self._resolve_single_case_target(raw)
            if record is None:
                self.print_red(err)
                self.pause_review("Press Enter to continue...")
                return
            self._run_single_case_with(record)
            return
        choice = self._q_select(
            "Single run — pick the input:",
            [
                ("📁  Choose a FOLDER (front image auto-detected inside)…", "folder"),
                ("🖼  Choose an IMAGE file directly…", "file"),
                ("⌨  Type a path (folder or image)…", "manual"),
                ("↩  Back", "back"),
            ],
        )
        if choice in (None, "back"):
            return
        raw: "Optional[str]" = None
        if choice == "folder":
            try:
                raw = select_directory_cli_safe(title="Select the case folder (contains the front image)")
            except Exception as exc:
                self.print_yellow(f"Folder picker unavailable ({exc}). Falling back to typed path.")
            if not raw:
                print("  (picker cancelled — falling back to manual entry)")
                raw = self._q_text("Folder path:")
        elif choice == "file":
            try:
                raw = select_open_file_cli_safe(title="Select the front image", image_only=True)
            except Exception as exc:
                self.print_yellow(f"File picker unavailable ({exc}). Falling back to typed path.")
            if not raw:
                print("  (picker cancelled — falling back to manual entry)")
                raw = self._q_text("Image file path:")
        else:
            raw = self._q_text("Folder or image path:")
        if not raw or not str(raw).strip():
            return
        record, err = self._resolve_single_case_target(raw)
        if record is None:
            self.print_red(err)
            self.pause_review("\nPress Enter to continue...")
            return
        self._run_single_case_with(record)

    def _run_single_case_with(self, record: "CaseRecord") -> None:
        """Approval + execution for one resolved case. The case folder's
        PARENT acts as the run root (manifest + logs live there), so a
        single run of a folder inside an existing batch root records into
        the SAME manifest — batch Run/Resume stays coherent with it."""
        root = record.case_dir.parent
        saved_root_attr = self.automation_root_folder
        saved_root_cfg = self.config.get("automation_root_folder")
        # The quick-edit screen reachable below exposes the "Root folder"
        # row; a deliberate re-pick there is the user's saved batch root for
        # AFTER this run — the single run itself stays pinned to the case
        # folder's parent (adversarial review HIGH, PR #96 round 9).
        user_root_attr: Optional[str] = None
        user_root_cfg: Optional[str] = None
        self.automation_root_folder = str(root)
        try:
            while True:
                self.automation_root_folder = str(root)
                interactive = not self._use_legacy_prompt_ui()
                manifest = self._load_manifest_with_recreate_prompt(
                    root, interactive=interactive, single_case=True
                )
                if manifest is None:
                    return
                entry = (manifest.data.get("cases") or {}).get(record.relative_key) or {}
                already_complete = str(entry.get("status", "")) == "complete"
                if not interactive:
                    if not self._confirm(f"Run single case '{record.relative_key}'?", default=False):
                        print("Run cancelled.")
                        return
                    self._execute_automation_run(manifest, [record], [record])
                    return
                self.display_header()
                _RICH_CONSOLE.print(Panel(
                    f"[bold cyan]{_rich_markup_escape(record.relative_key)}[/bold cyan]\n"
                    f"[dim]Folder:[/dim] {_rich_markup_escape(self._elide_path(str(record.case_dir), 72))}\n"
                    f"[dim]Front: [/dim] {_rich_markup_escape(record.front_path.name)}"
                    + ("\n[yellow]Already recorded as COMPLETE here — pick re-run to redo every step.[/yellow]"
                       if already_complete else ""),
                    title="[bold white]🎯  Single case[/bold white]",
                    border_style="blue",
                ))
                print()
                self._render_run_settings_table(title="Main run settings — review before starting")
                print()
                _RICH_CONSOLE.print(self._build_cost_estimate_table(1))
                print()
                options: "List[Tuple[str, str]]" = [("▶  Start run", "run")]
                if already_complete:
                    options.append(("🔄  Re-run from scratch (reset this case)", "rerun"))
                options += [
                    ("🔧  Quick edit settings first", "edit"),
                    ("📜  View FULL prompts", "prompts"),
                    ("✗  Cancel", "cancel"),
                ]
                action = self._q_select("Run this single case?", options)
                if action in (None, "cancel"):
                    print("Run cancelled.")
                    return
                if action == "edit":
                    self._quick_edit_settings()
                    if self.automation_root_folder != str(root):
                        # User re-picked the root folder inside quick-edit:
                        # remember it as their new saved batch root, then
                        # re-pin this run to the case parent (loop top).
                        user_root_attr = self.automation_root_folder
                        user_root_cfg = self.config.get("automation_root_folder")
                    continue  # reload manifest — the fingerprint may have changed
                if action == "prompts":
                    self._show_full_prompts()
                    self.pause_review("\nPress Enter to continue...")
                    continue
                if action == "rerun":
                    # reset_case_for_new_front IS the generic per-case reset:
                    # it rewrites the case to a fresh state (same mechanism
                    # the front-changed guard uses). The manifest reset alone
                    # is NOT enough though — file-based reuse (skip mode +
                    # skip_if_*_exists) would silently re-adopt the existing
                    # on-disk selfie/video, so "redo every step" must force a
                    # real reprocess for this run (same override set the
                    # headless --reprocess flag uses); increment keeps the old
                    # outputs under their names (adversarial review HIGH, r9).
                    manifest.reset_case_for_new_front(record.relative_key, record.case_dir, record.front_path)
                    rerun_overrides = {
                        "automation_allow_reprocess": True,
                        "automation_reprocess_mode": "increment",
                        "automation_skip_completed": False,
                        "automation_skip_if_selfie_exists": False,
                        "automation_skip_if_video_exists": False,
                    }
                    saved_policy = {k: self.config.get(k) for k in rerun_overrides}
                    self.config.update(rerun_overrides)
                    try:
                        self._execute_automation_run(manifest, [record], [record])
                    finally:
                        for key, value in saved_policy.items():
                            if value is None:
                                self.config.pop(key, None)
                            else:
                                self.config[key] = value
                    return
                self._execute_automation_run(manifest, [record], [record])
                return
        finally:
            # Restore BOTH the attr and the config key — _execute_automation_run
            # copies the attr into config, and a later save_config must never
            # persist the single-run root over the user's saved batch root.
            # A root the user deliberately re-picked mid-flow wins over the
            # pre-run value (it was already save_config'd by the picker).
            self.automation_root_folder = user_root_attr if user_root_attr else saved_root_attr
            if user_root_attr:
                self.config["automation_root_folder"] = user_root_cfg
            elif saved_root_cfg is None:
                self.config.pop("automation_root_folder", None)
            else:
                self.config["automation_root_folder"] = saved_root_cfg

    def _run_resume_automation_legacy(self, root: Path):
        """Non-TTY / legacy path: behavior-identical to the historical flow
        (plain prints + single y/N confirm) — test choreography depends on
        it."""
        records, manifest = self._discover_and_load_manifest(root, interactive=False)
        if not records:
            return
        rows, counts, runnable_cases = self._collect_case_snapshot(records, manifest)
        print("\nRun preview:")
        print(f"  discovered: {counts['discovered']}")
        print(f"  completed total: {counts['completed_total']}")
        print(f"  skipped complete: {counts['skipped_complete']}")
        print(f"  pending/runnable: {counts['pending']}")
        print(f"  will run this batch: {counts['will_run']}")
        print(f"  manual review: {counts['manual_review']}")
        print(f"  failed: {counts['failed']}")
        if not runnable_cases:
            self.print_yellow("No runnable cases for this batch.")
            self.pause_review("Press Enter to continue...")
            return
        if not self._confirm("Approve batch run?", default=False):
            print("Run cancelled.")
            self.pause_review("Press Enter to continue...")
            return
        self._execute_automation_run(manifest, records, runnable_cases)

    def _run_resume_automation_interactive(self, root: Path):
        """Interactive approval loop: case preview + the MAIN-settings table
        + [Approve / Quick edit / View full prompts / Cancel]. Quick edits
        reload the manifest (they may change the run fingerprint)."""
        while True:
            # Full repaint like every other screen (round 8): without it the
            # main menu's questionary echo line sat directly above the
            # preview table with no separation.
            self.display_header()
            records, manifest = self._discover_and_load_manifest(root, interactive=True)
            if not records:
                return
            rows, counts, runnable_cases = self._collect_case_snapshot(records, manifest)
            preview = self._styled_table("📋  Run preview")
            preview.add_column("k", style="cyan", no_wrap=True)
            preview.add_column("v")
            preview.add_row("Discovered", str(counts["discovered"]))
            preview.add_row("Completed total", str(counts["completed_total"]))
            preview.add_row("Pending / runnable", str(counts["pending"]))
            preview.add_row("Will run this batch", f"[bold green]{counts['will_run']}[/bold green]")
            _mr, _fl = counts["manual_review"], counts["failed"]
            preview.add_row(
                "Manual review / failed",
                f"[yellow]{_mr}[/yellow] / [red]{_fl}[/red]" if (_mr or _fl) else "0 / 0",
            )
            for line in self._resume_intelligence_lines(records, manifest):
                preview.add_row("Resume", f"[dim]{line}[/dim]")
            _RICH_CONSOLE.print(preview)
            print()
            if runnable_cases:
                # WHICH folders this batch will work on — by name, before any
                # money is spent (round 7: "it should clearly show which of
                # the 5 it will work on"). The live dashboard then tracks the
                # same list with ok/>>/pending glyphs during the run.
                batch_table = self._styled_table(f"🗂  Cases in this batch ({len(runnable_cases)})")
                batch_table.add_column("#", style="dim", no_wrap=True)
                batch_table.add_column("Folder", style="cyan")
                for idx, rec in enumerate(runnable_cases[:15], 1):
                    batch_table.add_row(str(idx), _rich_markup_escape(str(getattr(rec, "relative_key", rec))))
                if len(runnable_cases) > 15:
                    batch_table.add_row("…", f"[dim]+{len(runnable_cases) - 15} more[/dim]")
                _RICH_CONSOLE.print(batch_table)
                print()
            self._render_run_settings_table(title="Main run settings — review before approving")
            print()
            if not runnable_cases:
                self.print_yellow("No runnable cases for this batch.")
                self.pause_review("Press Enter to continue...")
                return
            action = self._q_select(
                "Start the batch with these settings?",
                [
                    (f"✅ Approve & run ({counts['will_run']} case(s))", "run"),
                    ("🧪 Dry run first (no API calls)", "dry_run"),
                    ("🔧 Quick edit settings first", "edit"),
                    ("📜 View FULL prompts (selfie + video)", "prompts"),
                    ("✗ Cancel", "cancel"),
                ],
            )
            if action in (None, "cancel"):
                print("Run cancelled.")
                return
            if action == "dry_run":
                # Moved here from the main menu (round 3) — dry run is a
                # pre-flight check, not a top-level destination. Loops back
                # to this same approval afterwards.
                self._dry_run_automation()
                continue
            if action == "edit":
                self._quick_edit_settings()
                continue  # re-discover + reload manifest (fingerprint may have changed)
            if action == "prompts":
                self._show_full_prompts()
                self.pause_review("Press Enter to continue...")
                continue
            break
        self._execute_automation_run(manifest, records, runnable_cases)

    def _execute_automation_run(self, manifest: AutomationManifest, records, runnable_cases) -> None:
        """Shared validate -> preflight echo -> live run -> summary tail."""
        self.config["automation_root_folder"] = self.automation_root_folder
        runner = AutoPipelineRunner(
            config=self.config,
            automation_config=from_app_config(self.config),
            manifest=manifest,
            progress_cb=None,
        )
        issues = runner.validate_configuration()
        if issues:
            print("\nAutomation preflight failed:")
            for issue in issues:
                print(f"  - {issue}")
            self.pause_review("\nPress Enter to continue...")
            return

        interactive = not self._use_legacy_prompt_ui()
        selfie_slot, selfie_prompt, selfie_source = self._get_selected_selfie_prompt()
        prompt_preview = selfie_prompt if len(selfie_prompt) <= 160 else f"{selfie_prompt[:160]}..."
        if interactive:
            # Branded repaint + compact Rich preflight (the full settings
            # blob was already reviewed on the approval screen); legacy
            # keeps the historical prints below for tests/cron logs.
            self.display_header()
            pre = self._styled_table("🚦  Preflight — starting run")
            pre.add_column("k", style="cyan", no_wrap=True)
            pre.add_column("v")
            pre.add_row("Cases discovered", str(len(records)))
            pre.add_row("Running this batch", f"[bold green]{len(runnable_cases)}[/bold green]")
            pre.add_row("Reprocess mode", str(self.config.get("automation_reprocess_mode", "skip")))
            pre.add_row(
                "Skip if selfie/video exists",
                f"{self.config.get('automation_skip_if_selfie_exists', True)} / {self.config.get('automation_skip_if_video_exists', True)}",
            )
            pre.add_row("Selfie prompt", f"slot {selfie_slot} ({selfie_source})")
            pre.add_row("Prompt preview", f"[dim]{_rich_markup_escape(prompt_preview)}[/dim]")
            _RICH_CONSOLE.print(pre)
        else:
            print("\nAutomation preflight:")
            print(f"  cases discovered: {len(records)}")
            print(f"  running this batch: {len(runnable_cases)}")
            print(f"  reprocess mode: {self.config.get('automation_reprocess_mode', 'skip')}")
            print(f"  skip selfie/video existing: {self.config.get('automation_skip_if_selfie_exists', True)} / {self.config.get('automation_skip_if_video_exists', True)}")
            for line in self._automation_status_lines():
                print(f"  {line}")
            print(f"  selfie prompt slot/source: {selfie_slot} / {selfie_source}")
            print(f"  selfie prompt preview: {prompt_preview}")
        stats, run_error = self._run_with_live_dashboard(runner, runnable_cases, manifest)
        if run_error:
            self.print_red(f"Automation run failed: {run_error}")
            self.pause_review("\nPress Enter to continue...")
            return
        if interactive:
            # One wide row of counts — the stacked two-column version was
            # narrower than its own title, which wrapped (round 8).
            done = self._styled_table("✅  Automation run complete", border_style="green", show_header=True)
            done.add_column("Completed", justify="center")
            done.add_column("Failed", justify="center")
            done.add_column("Manual review", justify="center")
            done.add_column("Skipped", justify="center")
            failed_n = stats.get("failed", 0)
            done.add_row(
                f"[bold green]{stats.get('completed', 0)}[/bold green]",
                f"[bold red]{failed_n}[/bold red]" if failed_n else "0",
                str(stats.get("manual_review", 0)),
                str(stats.get("skipped", 0)),
            )
            print()
            _RICH_CONSOLE.print(done)
            print()
        else:
            print("\nAutomation run complete.")
            print(f"  completed: {stats.get('completed', 0)}")
            print(f"  failed: {stats.get('failed', 0)}")
            print(f"  manual_review: {stats.get('manual_review', 0)}")
            print(f"  skipped: {stats.get('skipped', 0)}")
        if interactive:
            table = self._styled_table("Per-case summary", show_header=True)
        else:
            table = Table(title="Per-Case Summary")
        table.add_column("Case")
        table.add_column("Status")
        table.add_column("Reason")
        for key, result in sorted(runner.last_case_results.items(), key=lambda item: item[0].lower()):
            # Folder names + error reasons are user-influenced text — escape.
            table.add_row(
                _rich_markup_escape(key),
                str(result.get("status", "")),
                _rich_markup_escape(str(result.get("reason", ""))),
            )
        _RICH_CONSOLE.print(table)
        self._write_automation_summary(manifest, runner.last_case_results, stats)
        self.pause_review("\nPress Enter to continue...")

    _DASHBOARD_STEP_LABELS = {
        "front_expand": "1 front expand",
        "extract_portrait": "2 extract portrait",
        "selfie_generate": "3 generate selfie",
        "similarity_gate": "4 similarity gate",
        "selfie_expand": "5 selfie expand",
        "video_generate": "6 kling video",
        "facetrack_gate": "6.5 face-track gate",
        "rppg": "7 rppg injection",
        "loop": "7.5 loop",
        "oldcam": "8 oldcam",
    }

    @staticmethod
    @contextlib.contextmanager
    def _suppress_stream_logging():
        """Detach console (stdout/stderr) handlers from the ROOT logger for
        the duration of the Rich Live display, keeping every file handler.

        This is THE fix for the stacked-panel disaster: setup_logging()
        attaches a StreamHandler to the root logger, so any library log line
        (e.g. model_schema_manager's "Parsed N parameters from schema")
        printed raw between Live frames shattered the panel into dozens of
        partial reprints. File logging (kling_automation.log + the rotating
        automation log) is untouched. NOTE: FileHandler subclasses
        StreamHandler, so the keep-check must test FileHandler FIRST.
        """
        root = logging.getLogger()
        removed: List[logging.Handler] = []
        # logging.lastResort guard (round 8, found via REAL run capture): if
        # absl/TF imported before setup_logging, basicConfig is a NO-OP and
        # the strip can leave root with ZERO handlers — then Python's
        # lastResort handler (NOT in root.handlers, unstrippable) prints
        # every WARNING bare to stderr through Live. A NullHandler makes
        # "a handler exists" true so lastResort can never fire; file
        # handlers (when present) still write normally.
        null_guard = logging.NullHandler()

        def _strip() -> None:
            # Remove EVERY non-file-backed root handler. Round-8 lessons:
            # (1) the heavy face stack imports LAZILY inside the run, and
            # absl/TF install a fresh console handler on the ROOT logger at
            # import time — i.e. AFTER a one-shot strip ("Anti-spoofing…" /
            # "Pricing API returned 400" printed bare mid-panel and shattered
            # it). The dashboard loop re-invokes this every tick. (2) absl's
            # ABSLHandler is NOT a StreamHandler subclass and Live's
            # redirect replaces sys.stdout, so neither the isinstance- nor
            # the stream-identity check is reliable — keep file handlers,
            # drop everything else for the Live window.
            for handler in list(root.handlers):
                if isinstance(handler, logging.FileHandler) or handler is null_guard:
                    continue  # file-backed / our guard — keep
                root.removeHandler(handler)
                removed.append(handler)

        root.addHandler(null_guard)
        _strip()
        # Restore ONLY the handlers that existed BEFORE this window: handlers
        # absl/TF install MID-run get stripped per tick and must NOT leak
        # onto the root logger afterwards (entire-branch review MED — a
        # caller with no console handler before the run would otherwise gain
        # absl's handler after it).
        initially_removed = list(removed)
        try:
            yield _strip
        finally:
            root.removeHandler(null_guard)
            for handler in initially_removed:
                root.addHandler(handler)

    @classmethod
    def _build_dashboard_panel(
        cls,
        *,
        total: int,
        counts: Dict[str, int],
        current_case: str,
        current_step: str,
        similarity: str,
        last_output: str,
        error_reason: str,
        events: List[Tuple[str, str, str]],
        footer: str,
        queue_lines: "Optional[List[str]]" = None,
        next_step: str = "",
        step_pos: str = "",
        step_progress: str = "",
    ) -> Panel:
        """Pure renderable builder (unit-testable without threads). ASCII
        body — conhost wobbles on emoji width inside panels.

        Round-7 additions (all optional, defaults keep old callers/tests
        intact): the per-case QUEUE with live status glyphs, the NEXT step,
        the current step's position (k/N), and the in-step progress line
        fed by the pipeline's progress_update events — so the user always
        sees which folder is being worked, where in the chain it is, and
        how far the current step has come."""
        done = sum(counts.get(k, 0) for k in ("completed", "failed", "manual_review", "skipped"))
        pct = int((done / total) * 100) if total else 100
        bar_width = 30
        filled = int(bar_width * (done / total)) if total else bar_width
        bar = "#" * filled + "-" * (bar_width - filled)
        # MARKUP-ESCAPE every literal bracket + every dynamic string: rich
        # parses "[####...]" as a hex-color tag and "[p]"/"[a]" as style
        # tags, silently EATING the progress bar and the key hints (the
        # long-standing "live view looks off" bug, found round 7). \\[ is
        # rich's literal-bracket escape; dynamic content (paths, messages)
        # goes through markup-escape so a bracketed filename can't break
        # the panel.
        _esc = _rich_markup_escape
        lines = [
            f"[bold]Progress[/bold]   [cyan]\\[{bar}][/cyan] {done}/{total} ({pct}%)",
        ]
        if queue_lines:
            lines.append("")
            lines.append("[bold]Cases[/bold]")
            lines.extend(f"  {q}" for q in queue_lines)
            lines.append("")
        step_line = f"[bold]Step[/bold]       [cyan]{_esc(current_step)}[/cyan]"
        if step_pos:
            step_line += f"  [dim]({_esc(step_pos)})[/dim]"
        lines.append(f"[bold]Case[/bold]       [bold cyan]{_esc(current_case)}[/bold cyan]")
        lines.append(step_line)
        if next_step:
            lines.append(f"[bold]Next[/bold]       [dim]{_esc(next_step)}[/dim]")
        if step_progress:
            lines.append(f"[bold]Step prog[/bold]  [green]{_esc(step_progress)}[/green]")
        lines.extend(
            [
                f"[bold]Similarity[/bold] {_esc(similarity)}",
                f"[bold]Last out[/bold]   {_esc(last_output)}",
                f"[bold]Issue[/bold]      {_esc(error_reason)}",
                f"completed=[green]{counts.get('completed', 0)}[/green] failed=[red]{counts.get('failed', 0)}[/red] "
                f"manual_review={counts.get('manual_review', 0)} skipped={counts.get('skipped', 0)} "
                f"remaining={max(0, total - done)}",
            ]
        )
        if events:
            lines.append("[dim]--- last events ---------------------------------[/dim]")
            for ts, level, message in events:
                style = {"error": "red", "warning": "yellow", "success": "green"}.get(level)
                if style is None:
                    # Keyword tinting for info-level events — an all-grey
                    # trail hid the interesting lines (round 8).
                    low = message.lower()
                    if any(k in low for k in ("complete", " pass", "downloaded", "saved", "done", "✓")):
                        style = "green"
                    elif any(k in low for k in ("launching", "starting", "submit", "processing", "fetching", "polling", "generating")):
                        style = "cyan"
                    else:
                        style = "dim"
                lines.append(f"[dim]{ts}[/dim] [{style}]{_esc(message)}[/{style}]")
        lines.append(f"[dim]{_esc(footer)}[/dim]")
        return Panel("\n".join(lines), title="Automation Live Progress", border_style="cyan")

    # Live case-queue glyphs: (markup color, 4-char ASCII tag). ASCII tags —
    # emoji width wobbles inside Live panels on conhost.
    _QUEUE_GLYPHS = {
        "complete": ("green", " ok "),
        "failed": ("red", "FAIL"),
        "manual_review": ("yellow", " MR "),
        "skipped": ("dim", "skip"),
        "running": ("bold cyan", " >> "),
    }

    @classmethod
    def _build_queue_lines(cls, case_keys: "List[str]", snapshot: "Dict[str, Dict[str, Any]]",
                           max_rows: int = 12) -> "List[str]":
        """Per-case status lines for the live panel. Small batches list every
        folder; large ones collapse the finished block and the far tail so
        the panel never scrolls the step/event area off screen."""
        entries = []
        for key in case_keys:
            status = str((snapshot.get(key) or {}).get("status", "pending"))
            color, glyph = cls._QUEUE_GLYPHS.get(status, ("dim", "    "))
            # \\[ escapes the literal bracket (rich would eat "[FAIL]" as a
            # style tag); folder names are markup-escaped for the same reason.
            entries.append((status, f"[{color}]\\[{glyph}][/{color}] {_rich_markup_escape(key)}"))
        if len(entries) <= max_rows:
            return [line for _status, line in entries]
        finished = {"complete", "failed", "manual_review", "skipped"}
        lines: "List[str]" = []
        done_block = [e for e in entries if e[0] in finished]
        rest = [e for e in entries if e[0] not in finished]
        if done_block:
            lines.append(f"[green]\\[ ok ][/green] [dim]{len(done_block)} case(s) finished[/dim]")
        shown = rest[: max_rows - len(lines) - 1]
        lines.extend(line for _status, line in shown)
        hidden = len(rest) - len(shown)
        if hidden > 0:
            lines.append(f"[dim]       … +{hidden} more pending[/dim]")
        return lines

    def _run_with_live_dashboard(
        self,
        runner: AutoPipelineRunner,
        run_cases: List[Any],
        manifest: AutomationManifest,
    ) -> Tuple[Dict[str, int], Optional[str]]:
        """ONE pinned, in-place-updating dashboard panel.

        2026-06-11 rebuild — the previous implementation stacked dozens of
        partial panels because (a) the worker thread mutated shared state +
        manifest.data while the UI thread rendered them unlocked, (b) a
        second Console + transient=True fought the app console, and (c) the
        root logger's StreamHandler wrote raw lines through Live. Now: a
        locked event/state holder, manifest.snapshot_statuses() copies, the
        SHARED _RICH_CONSOLE with redirect_stdout/stderr, console logging
        suppressed for the duration (file logs untouched), and pause/abort
        keys ([p]=finish current case then stop, [a]=stop after current
        step; both resumable via Run/Resume)."""
        state_lock = threading.Lock()
        state: Dict[str, Any] = {
            "message": "",
            "level": "info",
            "last_output": "-",
            "error_reason": "-",
            "step_progress": "",  # in-step % / elapsed line (progress_update)
            "events": [],  # list of (time, level, message), newest last
        }

        def _cb(message: str, level: str = "info"):
            timestamp = time.strftime("%H:%M:%S")
            with state_lock:
                if level == "progress_update":
                    # High-frequency in-step progress (video poll ticks, rPPG
                    # iterations, oldcam frames): pinned to its own line, NOT
                    # appended to events — it would flush real events out.
                    state["step_progress"] = message[:110]
                    return
                state["message"] = message
                state["level"] = level
                lowered = message.lower()
                if ".mp4" in lowered or "output:" in lowered:
                    # Just the FILENAME of the newest artifact — a full rPPG
                    # launch command in "Last out" was an unreadable wall of
                    # wrapped paths (round 8).
                    hits = re.findall(r"[^\s\"']+\.mp4", message)
                    if hits:
                        state["last_output"] = Path(hits[-1]).name[-70:]
                    elif "output:" in lowered:
                        tail = message.split(":", 1)[1].strip()
                        state["last_output"] = ("…" + tail[-69:]) if len(tail) > 70 else tail
                if level in {"error", "warning"}:
                    state["error_reason"] = message
                events = state["events"]
                events.append((timestamp, level, message[:110]))
                del events[:-8]  # keep the last 8

        runner.progress_cb = _cb
        run_result: Dict[str, Any] = {"stats": None, "error": None}

        def _worker():
            try:
                run_result["stats"] = runner.run(run_cases)
            except Exception as exc:
                run_result["error"] = str(exc)

        worker = threading.Thread(target=_worker, daemon=True)
        worker.start()

        total_cases = len(run_cases)
        case_keys = [case.relative_key for case in run_cases]
        footer_active = (
            "[p] pause after current case · [a] abort after current step · "
            "paused/aborted runs RESUME from the menu (Run / resume)"
        )
        # Ordered step labels for the "next:" hint + step k/N position.
        step_labels_ordered = list(self._DASHBOARD_STEP_LABELS.values())
        # getattr-defensive: tests drive this with stub runners that may not
        # carry the pause/abort events (PR #73 lesson — stub objects lack
        # new fields).
        pause_event = getattr(runner, "pause_event", None) or threading.Event()
        abort_event = getattr(runner, "abort_event", None) or threading.Event()

        def _poll_keys() -> None:
            """Windows: non-blocking key reads while Live owns the screen."""
            if os.name != "nt":
                return
            try:
                import msvcrt
                while msvcrt.kbhit():
                    key = msvcrt.getwch().lower()
                    if key == "p" and not pause_event.is_set():
                        pause_event.set()
                        _cb("PAUSE requested — finishing the current case, then stopping.", "warning")
                    elif key == "a" and not abort_event.is_set():
                        abort_event.set()
                        _cb("ABORT requested — stopping after the current step.", "warning")
            except Exception:
                pass  # key polling is best-effort; never break the run

        def _render() -> Panel:
            snap_fn = getattr(manifest, "snapshot_statuses", None)
            if callable(snap_fn):
                snapshot = snap_fn(case_keys)
            else:  # stub manifests in tests
                cases = manifest.data.get("cases", {})
                if not isinstance(cases, dict):
                    cases = {}  # corrupted manifest: never crash the render (Gemini MED, r5)
                snapshot = {}
                for key in case_keys:
                    entry = cases.get(key)
                    if not isinstance(entry, dict):
                        entry = {}
                    snapshot[key] = {
                        "status": str(entry.get("status", "pending")),
                        "active_step": entry.get("active_step"),
                        "similarity": None,
                    }
            counts = {"completed": 0, "failed": 0, "manual_review": 0, "skipped": 0}
            current_case = "-"
            current_step = "-"
            similarity = "-"
            for key in case_keys:
                entry = snapshot[key]
                status = entry["status"]
                if status == "complete":
                    counts["completed"] += 1
                elif status in counts:
                    counts[status] += 1
                if status == "running":
                    current_case = key
                    step_name = str(entry.get("active_step") or "")
                    current_step = self._DASHBOARD_STEP_LABELS.get(step_name, step_name or "-")
                    if entry.get("similarity") is not None:
                        similarity = str(entry["similarity"])
            with state_lock:
                last_output = state["last_output"]
                error_reason = state["error_reason"]
                step_progress = state["step_progress"]
                events = list(state["events"])
            if abort_event.is_set():
                footer = "ABORTING after current step... (progress saved — Run / resume continues)"
            elif pause_event.is_set():
                footer = "PAUSING after current case... (progress saved — Run / resume continues)"
            else:
                footer = footer_active
            next_step = ""
            step_pos = ""
            if current_step in step_labels_ordered:
                idx = step_labels_ordered.index(current_step)
                step_pos = f"step {idx + 1}/{len(step_labels_ordered)}"
                next_step = (
                    step_labels_ordered[idx + 1]
                    if idx + 1 < len(step_labels_ordered)
                    else "finish case"
                )
            return self._build_dashboard_panel(
                total=total_cases,
                counts=counts,
                current_case=current_case,
                current_step=current_step,
                similarity=similarity,
                last_output=last_output,
                error_reason=error_reason,
                events=events,
                footer=footer,
                queue_lines=self._build_queue_lines(case_keys, snapshot),
                next_step=next_step,
                step_pos=step_pos,
                step_progress=step_progress,
            )

        try:
            with self._suppress_stream_logging() as restrip_console_logging:
                with Live(
                    _render(),
                    console=_RICH_CONSOLE,
                    refresh_per_second=4,
                    transient=False,
                    redirect_stdout=True,
                    redirect_stderr=True,
                ) as live:
                    while worker.is_alive():
                        _poll_keys()
                        # Lazily-imported libs (deepface/TF/absl) add fresh
                        # console handlers MID-RUN — re-strip every tick or
                        # their log lines shatter the pinned panel (round 8).
                        restrip_console_logging()
                        live.update(_render())
                        time.sleep(0.2)
                    worker.join()
                    live.update(_render())  # final pinned frame = end state
        except KeyboardInterrupt:
            abort_event.set()
            self.print_yellow("Ctrl-C — aborting after the current step (progress is saved)...")
            worker.join()

        stopped_reason = getattr(runner, "stopped_reason", None)
        if stopped_reason:
            self.print_yellow(
                f"Run {stopped_reason}. Progress is saved — use Run/Resume to continue where it left off."
            )
        return run_result.get("stats") or {"completed": 0, "failed": 0, "manual_review": 0, "skipped": 0}, run_result.get("error")

    def _write_automation_summary(
        self,
        manifest: AutomationManifest,
        last_case_results: Dict[str, Dict[str, str]],
        stats: Dict[str, int],
    ) -> None:
        summary_lines = [
            "# Automation Run Summary",
            "",
            f"- completed: {stats.get('completed', 0)}",
            f"- failed: {stats.get('failed', 0)}",
            f"- manual_review: {stats.get('manual_review', 0)}",
            f"- skipped: {stats.get('skipped', 0)}",
            f"- manifest: {manifest.manifest_path}",
            "",
            "## Per-case outputs",
        ]
        for case_key, result in sorted(last_case_results.items(), key=lambda item: item[0].lower()):
            case_entry = manifest.data.get("cases", {}).get(case_key, {})
            steps = case_entry.get("steps", {})
            video_step = steps.get("video_generate", {})
            video_out = video_step.get("output") or "-"
            oldcam_out = steps.get("oldcam", {}).get("output") or "-"
            # New post-chain stages + fan-out branches surfaced too —
            # omitting them hid paid deliverables from the summary
            # (Codex P3, PR #96 round 4).
            rppg_out = steps.get("rppg", {}).get("output") or "-"
            loop_out = steps.get("loop", {}).get("output") or "-"
            summary_lines.append(
                f"- `{case_key}`: status={result.get('status', '')}, video={video_out}, "
                f"rppg={rppg_out}, loop={loop_out}, oldcam={oldcam_out}, reason={result.get('reason', '')}"
            )
            for branch in (video_step.get("meta", {}) or {}).get("branches", []) or []:
                branch_bits = [f"status={branch.get('status', '')}"]
                for field in ("video", "rppg", "loop"):
                    if branch.get(field):
                        branch_bits.append(f"{field}={branch[field]}")
                if branch.get("oldcam_outputs"):
                    branch_bits.append(f"oldcam={'; '.join(branch['oldcam_outputs'])}")
                if branch.get("error"):
                    branch_bits.append(f"error={branch['error']}")
                summary_lines.append(
                    f"  - branch `{branch.get('endpoint', '?')}`: {', '.join(branch_bits)}"
                )
        summary_path = manifest.manifest_path.parent / "automation_run_summary.md"
        summary_path.write_text("\n".join(summary_lines) + "\n", encoding="utf-8")
        print(f"\nSummary written: {summary_path}")

    def run_automation_menu(self):
        while True:
            choice = self._automation_menu_choice()
            if choice == "0":
                return
            if choice == "1":
                self._select_automation_root()
            elif choice == "2":
                self._scan_automation_cases()
            elif choice == "3":
                self._apply_recommended_automation_defaults()
            elif choice == "4":
                self._edit_automation_settings()
            elif choice == "5":
                self._dry_run_automation()
            elif choice == "6":
                self._run_resume_automation()
            elif choice == "7":
                manifest_path = self._automation_manifest_path()
                print(f"\nManifest path: {manifest_path if manifest_path else '(set root first)'}")
                self.pause_review("\nPress Enter to continue...")
            elif choice == "8":
                self._quick_edit_settings()
            elif choice == "9":
                self._show_full_prompts()
                self.pause_review("\nPress Enter to continue...")
            else:
                self.print_red("Unknown option.")
                time.sleep(1)

    @staticmethod
    def _use_legacy_prompt_ui() -> bool:
        """True when interactive questionary prompts must fall back to input().

        Single source of truth for the questionary gate used by every
        interactive helper (_confirm, _automation_menu_choice,
        _select_automation_root, the settings editor). Legacy/input() is used
        when questionary is unavailable, stdin is not an interactive TTY, or the
        KLING_LEGACY_SETTINGS_UI escape hatch is set. The stdin check is fully
        guarded: sys.stdin can be None (Windows background service) OR a custom
        stream without isatty() (IDE/GUI wrappers, some test runners), so we
        getattr + hasattr before calling it (Gemini review, PR #94).
        """
        stdin = getattr(sys, "stdin", None)
        is_tty = bool(stdin) and hasattr(stdin, "isatty") and stdin.isatty()
        return (
            not _QUESTIONARY_AVAILABLE
            or not is_tty
            or os.environ.get("KLING_LEGACY_SETTINGS_UI") == "1"
        )

    def _confirm(self, message: str, default: bool = False) -> bool:
        """Yes/no confirm, questionary when interactive else input() fallback.

        Non-TTY / questionary-unavailable callers (CI, piped stdin, headless)
        get the legacy ``[y/N]`` input() prompt; an empty answer or EOF returns
        ``default`` so an unattended pipe never hangs.
        """
        if not self._use_legacy_prompt_ui():
            try:
                answer = questionary.confirm(
                    message,
                    qmark="◆",
                    default=default,
                    style=KLING_QUESTIONARY_STYLE,
                ).ask()
                if answer is None:
                    # Esc/Ctrl-C: give explicit feedback in interactive mode so the
                    # user knows nothing happened, then fall back to default (E6).
                    self.print_yellow("Cancelled.")
                    return default
                return bool(answer)
            except (KeyboardInterrupt, EOFError):
                return default
        suffix = "[Y/n]" if default else "[y/N]"
        try:
            raw = input(f"{message} {suffix}: ").strip().lower()
        except EOFError:
            return default
        if not raw:
            return default
        return raw in {"y", "yes", "1", "true"}

    # ------------------------------------------------------------------
    # Shared questionary toolkit
    #
    # A small, consistent set of helpers so every interactive menu looks and
    # behaves the same: arrow-key selection, branded ◆ marker + cyan/green
    # style, graceful Esc/Ctrl-C handling. Each helper is paired with an
    # input()-based fallback the CALLER provides, gated on _use_legacy_prompt_ui()
    # so non-TTY / piped-stdin / questionary-unavailable contexts keep working.
    # ------------------------------------------------------------------

    def _q_menu(
        self,
        title: str,
        choices: "List[Any]",
        *,
        status_lines: "Optional[List[str]]" = None,
        instruction: str = "↑/↓ to move · Enter to select · Esc to go back",
        show_header: bool = True,
        show_title_rule: bool = True,
    ) -> "Optional[str]":
        """Render a branded arrow-key menu and return the selected value.

        ``choices`` is a list of ``questionary.Choice`` (or (label, value) tuples
        / plain strings). Returns the selected value, or ``None`` if the user
        pressed Esc / Ctrl-C (callers treat that as "back/cancel"). Only call on
        the questionary path — guard with ``self._use_legacy_prompt_ui()`` first.
        ``show_title_rule=False`` skips the ═ title block for callers whose
        screen is already branded (the main menu's header panel carries the
        same title — the doubled banner was part of the "looks like garbage"
        feedback, 2026-06-11).
        """
        if show_header:
            self.display_header()
        if show_title_rule:
            self.print_magenta("═" * 79)
            _t = title.upper()
            self.print_magenta(f"{' ' * max(0, (79 - len(_t)) // 2)}{_t}")
            self.print_magenta("═" * 79)
            print()
        if status_lines:
            for line in status_lines:
                print(f"  {line}")
            print()
        normalized: "List[Any]" = []
        for ch in choices:
            if isinstance(ch, tuple) and len(ch) == 2:
                normalized.append(questionary.Choice(ch[0], ch[1]))
            else:
                normalized.append(ch)
        try:
            answer = questionary.select(
                title,
                qmark="◆",
                instruction=instruction,
                choices=normalized,
                style=KLING_QUESTIONARY_STYLE,
            ).ask()
        except (KeyboardInterrupt, EOFError):
            return None
        return answer

    def _q_select(
        self,
        message: str,
        choices: "List[Any]",
        *,
        default: "Optional[str]" = None,
        instruction: str = "",
    ) -> "Optional[str]":
        """A simple inline single-select (no header). Returns value or None."""
        normalized: "List[Any]" = []
        for ch in choices:
            if isinstance(ch, tuple) and len(ch) == 2:
                normalized.append(questionary.Choice(ch[0], ch[1]))
            else:
                normalized.append(ch)
        try:
            return questionary.select(
                message,
                qmark="◆",
                instruction=instruction,
                choices=normalized,
                default=default,
                style=KLING_QUESTIONARY_STYLE,
            ).ask()
        except (KeyboardInterrupt, EOFError):
            return None

    def _q_text(
        self,
        message: str,
        *,
        default: str = "",
        validate: "Optional[Any]" = None,
        instruction: str = "",
    ) -> "Optional[str]":
        """Free-text input. Returns the string, or None on Esc/Ctrl-C."""
        try:
            kwargs: "Dict[str, Any]" = {
                "qmark": "◆",
                "default": default,
                "style": KLING_QUESTIONARY_STYLE,
            }
            if instruction:
                kwargs["instruction"] = instruction
            if validate is not None:
                kwargs["validate"] = validate
            return questionary.text(message, **kwargs).ask()
        except (KeyboardInterrupt, EOFError):
            return None

    def _automation_menu_choice(self) -> str:
        """Top-level automation menu picker.

        Uses a questionary.select arrow menu when questionary is available AND
        we're on an interactive TTY; otherwise prints the legacy numbered menu
        and reads a numeric choice via input() so CI / piped-stdin / non-TTY
        callers keep working. Returns the numeric choice string ("0".."7").
        """
        if self._use_legacy_prompt_ui():
            self._display_automation_menu()
            try:
                return input().strip().lower()
            except EOFError:
                # Closed/piped stdin (no input available) -> behave like "Back"
                # rather than crashing the menu loop (CodeRabbit, PR #94).
                return "0"
        # Questionary path: header + the MAIN-settings Rich table (2026-06-11
        # mandate — rPPG/oldcam/models/providers must be impossible to miss
        # when entering option 1), then the arrow menu. Esc -> Back. The
        # _q_menu title rule is the SINGLE "END-TO-END AUTO PIPELINE" banner
        # (no manual duplicate).
        self.display_header()
        self._render_run_settings_table()
        answer = self._q_menu(
            "End-to-End Auto Pipeline",
            [
                ("▶️   Run / resume automation", "6"),
                ("⚡  Quick edit main settings", "8"),
                ("📜  View full prompts (selfie + video)", "9"),
                ("📂  Select automation root folder", "1"),
                ("🔍  Scan / preview cases", "2"),
                ("⭐  Apply recommended automation defaults", "3"),
                ("⚙️   Edit ALL automation settings", "4"),
                ("🧪  Dry run", "5"),
                ("📄  Print manifest path", "7"),
                ("↩️   Back", "0"),
            ],
            show_header=False,
            show_title_rule=False,
        )
        return answer if answer is not None else "0"

    def _run_manual_kling_menu(self) -> Optional[str]:
        """Manual Kling-first tools grouped under a menu.

        Returns a selected input path (folder/file) when the user picks one, so
        the caller can act on it; otherwise loops until "Back" / Esc.
        """
        while True:
            if self._use_legacy_prompt_ui():
                choice = self._manual_kling_menu_choice_legacy()
            else:
                _out_mode = "source folder" if self.config.get("use_source_folder", True) else f"custom ({self.config.get('output_folder', '?')})"
                choice = self._q_menu(
                    "Manual Kling Video Tools",
                    [
                        ("🎞️   Change output mode", "1"),
                        ("✏️   Edit / view Kling prompt", "2"),
                        ("🔊  Toggle verbose logging", "3"),
                        ("📁  Select input folder (GUI)", "4"),
                        ("🖼️   Select single image (GUI)", "5"),
                        ("🔬  Inspect model capabilities", "6"),
                        ("🔀  Change model", "7"),
                        ("🎚️   Swap prompt slot", "8"),
                        ("⚡  Quick edit prompt", "e"),
                        ("↩️   Back", "0"),
                    ],
                    status_lines=[
                        f"Model: {self.config.get('model_display_name', 'Kling 2.5 Turbo Standard')}",
                        f"Prompt slot: {self.config.get('current_prompt_slot', DEFAULT_KLING_PROMPT_SLOT)}",
                        f"Output: {_out_mode}",
                        f"Verbose logging: {'on' if self.verbose_logging else 'off'}",
                    ],
                )
            if choice in (None, "0"):
                return None
            if choice == "1":
                self.change_output_mode()
            elif choice == "2":
                self.edit_prompt()
            elif choice == "3":
                self.toggle_verbose_logging()
            elif choice == "4":
                selected_path = self.select_folder_gui()
                if selected_path:
                    return selected_path
            elif choice == "5":
                selected_path = self.select_file_gui()
                if selected_path:
                    return selected_path
            elif choice == "6":
                self.inspect_model_capabilities()
            elif choice == "7":
                self.select_model()
            elif choice == "8":
                self.swap_prompt_slot()
            elif choice == "e":
                self.quick_edit_prompt()
            else:
                self.print_red("Unknown option.")
                time.sleep(1)

    def _manual_kling_menu_choice_legacy(self) -> str:
        """Legacy numbered manual-tools menu (non-TTY / piped stdin)."""
        self.display_header()
        print("Manual Kling Video Tools")
        print("  1) Change output mode")
        print("  2) Edit/view Kling prompt")
        print("  3) Toggle verbose logging")
        print("  4) Select input folder (GUI)")
        print("  5) Select single image (GUI)")
        print("  6) Inspect model capabilities")
        print("  7) Change model")
        print("  8) Swap prompt slot")
        print("  e) Quick edit prompt")
        print("  0) Back")
        try:
            return input("\nSelect option: ").strip().lower()
        except EOFError:
            return "0"

    def count_genx_files(self, root_directory: str) -> int:
        """Count total genx files to process"""
        count = 0

        try:
            for folder_path in Path(root_directory).iterdir():
                if folder_path.is_dir():
                    for file_path in folder_path.iterdir():
                        if (
                            file_path.is_file()
                            and file_path.suffix.lower() in VALID_EXTENSIONS
                            and "genx" in file_path.name.lower()
                        ):
                            count += 1
        except Exception:
            pass
        return count

    def get_all_folders(self, root_directory: str):
        """Get all folders that contain genx images"""
        folders = []
        try:
            if self.get_genx_files_in_folder(root_directory):
                folders.append(root_directory)

            for folder_path in Path(root_directory).iterdir():
                if folder_path.is_dir():
                    if self.get_genx_files_in_folder(str(folder_path)):
                        folders.append(str(folder_path))
        except Exception:
            pass
        return folders

    def get_genx_files_in_folder(self, folder_path: str):
        """Get genx files in a specific folder"""
        genx_files = []

        try:
            for file_path in Path(folder_path).iterdir():
                if (
                    file_path.is_file()
                    and file_path.suffix.lower() in VALID_EXTENSIONS
                    and "genx" in file_path.name.lower()
                ):
                    genx_files.append(str(file_path))
        except Exception:
            pass
        return genx_files

    def start_processing(self, input_folder: str):
        """Start the video generation process with Rich UI"""
        from rich.console import Console
        from rich.progress import (
            Progress,
            SpinnerColumn,
            TextColumn,
            BarColumn,
            MofNCompleteColumn,
            TimeElapsedColumn,
        )
        from rich.panel import Panel
        from rich.text import Text
        from rich.table import Table
        from rich.align import Align
        from rich.spinner import Spinner
        from rich.live import Live
        from rich.console import Group

        console = Console(force_terminal=True, width=120)
        self.clear_screen()

        # Header panel - show configured model
        model_name = self.config.get("model_display_name", "Kling 2.5 Turbo Standard")
        header_text = Text()
        header_text.append(
            f"🚀 {model_name.upper()} BATCH VIDEO GENERATOR 🚀", style="bold cyan"
        )

        header_panel = Panel(
            Align.center(header_text), style="bright_blue", padding=(0, 1)
        )

        console.print(header_panel)

        # Create loading spinner
        def create_loading_spinner(message):
            return Spinner("dots", text=message, style="green bold")

        with Live(
            create_loading_spinner("Analyzing input..."),
            console=console,
            refresh_per_second=10,
        ) as loading_live:
            # Use fal.ai API with configurable model
            generator = FalAIKlingGenerator(
                api_key=self.config["falai_api_key"],
                verbose=self.verbose_logging,
                model_endpoint=self.config.get("current_model"),
                model_display_name=self.config.get("model_display_name"),
                prompt_slot=self.config.get("current_prompt_slot", DEFAULT_KLING_PROMPT_SLOT),
            )

            # Gate negative_prompt by model capability (like GUI does)
            # This prevents API errors for models that don't support negative prompts
            model_endpoint = self.config.get("current_model", "")
            negative_prompt = self.get_current_negative_prompt()
            if negative_prompt:
                if not generator.schema_manager.supports_parameter(
                    model_endpoint, "negative_prompt"
                ):
                    negative_prompt = None
                    if self.verbose_logging:
                        print(
                            f"Note: {self.config.get('model_display_name', 'Selected model')} does not support negative prompts - ignoring"
                        )

            # Get use_source_folder setting early for consistent use throughout
            use_source = self.config.get("use_source_folder", True)

            input_path = Path(input_folder)
            if input_path.is_file():
                genx_count = 1
                folders = [
                    input_folder
                ]  # Treat file as single item list for processing logic
                total_files = 1
                loading_live.update(
                    create_loading_spinner(f"Prepared single file: {input_path.name}")
                )
            else:
                loading_live.update(
                    create_loading_spinner(
                        "Analyzing folders and checking for duplicates..."
                    )
                )
                genx_count = self.count_genx_files(input_folder)
                folders = self.get_all_folders(input_folder)

                loading_live.update(
                    create_loading_spinner("Filtering out duplicates...")
                )

                total_files = 0
                for folder in folders:
                    genx_images = generator.get_genx_image_files(
                        folder, use_source, self.config["output_folder"]
                    )
                    total_files += len(genx_images)

        # Clear screen
        console.clear()
        os.system("cls" if os.name == "nt" else "clear")
        time.sleep(0.1)

        console.print(header_panel)

        # Balance tracking removed - use fal.ai dashboard instead
        # Dashboard link shown in header

        try:
            if not self.verbose_logging:
                # Configuration panel
                config_table = Table.grid(padding=0)
                config_table.add_column(
                    style="cyan", justify="left", width=18
                )  # Increased width for longer labels
                config_table.add_column(style="white", justify="left")

                if Path(input_folder).is_file():
                    config_table.add_row(
                        "Input:", f"Single File: {Path(input_folder).name}"
                    )
                else:
                    config_table.add_row("Files Amt:", f"{total_files} GenX files")

                model_name = self.config.get(
                    "model_display_name", "Kling 2.5 Turbo Standard"
                )
                duration = self.config.get("video_duration", 10)
                price = self.fetch_model_pricing(self.config.get("current_model", ""))
                price_str = f"${price:.2f}/sec" if price else "Check fal.ai"

                config_table.add_row("Provider:", "fal.ai API")
                config_table.add_row("Model:", model_name)
                config_table.add_row("Duration:", f"{duration} seconds")
                config_table.add_row("Cost:", price_str)
                # Show output mode
                use_source = self.config.get("use_source_folder", True)
                if use_source:
                    config_table.add_row("Output:", "📂 Same folder as source images")
                else:
                    config_table.add_row("Output folder:", self.config["output_folder"])
                config_table.add_row("Verbose mode:", "Hidden")

                config_panel = Panel(
                    config_table,
                    title="Configuration",
                    border_style="green",
                    title_align="left",
                    padding=(0, 1),
                )
                console.print(config_panel)
                print()  # Blank line after panel

                # Progress bar
                with Progress(
                    SpinnerColumn(style="bright_cyan"),
                    TextColumn("[progress.description]{task.description}"),
                    BarColumn(bar_width=None),
                    MofNCompleteColumn(),
                    TextColumn("•"),
                    TimeElapsedColumn(),
                    console=console,
                ) as progress:
                    if Path(input_folder).is_file():
                        main_task = progress.add_task(
                            "📊 [cyan]0% complete[/cyan] • 🎬 Processing Single File... 🚀",
                            total=total_files,
                        )
                    else:
                        main_task = progress.add_task(
                            "📊 [cyan]0% complete[/cyan] • 🎬 Processing GenX files... 🚀",
                            total=total_files,
                        )

                    active_generations = []  # Track currently processing files
                    recent_status = ""
                    processed = 0
                    videos_completed = 0  # Track successful completions for cost
                    all_files = []  # Track ALL files for "Next" display

                    # Collect all files upfront for Next display
                    if Path(input_folder).is_file():
                        all_files.append(Path(input_folder).stem)
                    else:
                        for folder in folders:
                            genx_images = generator.get_genx_image_files(
                                folder, use_source, self.config["output_folder"]
                            )
                            for img in genx_images:
                                folder_name = Path(folder).name
                                all_files.append(folder_name)

                    def create_colorful_spinners():
                        activity_text = Text()
                        activity_text.append("🔥 Activity: ", style="bright_green bold")

                        if active_generations:
                            # Show only 2 names to avoid overflow
                            activity_text.append(
                                f"{len(active_generations)} concurrent • ",
                                style="bright_cyan",
                            )
                            display_names = [
                                Path(f).stem[:15] for f in active_generations[:2]
                            ]  # Only 2 names, shorter
                            activity_text.append(
                                ", ".join(display_names), style="white"
                            )
                            if len(active_generations) > 2:
                                activity_text.append(
                                    f" (+{len(active_generations) - 2} more)",
                                    style="bright_yellow",
                                )
                        elif recent_status:
                            if "Completed:" in recent_status:
                                filename = recent_status.replace("Completed: ", "")
                                activity_text.append(
                                    "✅ Completed: ", style="bright_green"
                                )
                                activity_text.append(
                                    filename[:30], style="white"
                                )  # Limit length
                            elif "Failed:" in recent_status:
                                filename = recent_status.replace("Failed: ", "")
                                activity_text.append("❌ Failed: ", style="bright_red")
                                activity_text.append(
                                    filename[:30], style="white"
                                )  # Limit length
                            else:
                                activity_text.append(recent_status, style="bright_cyan")
                        else:
                            activity_text.append("Initializing...", style="bright_cyan")
                        activity_spinner = Spinner(
                            "dots", text=activity_text, style="bright_green"
                        )

                        # Action spinner (balance tracking removed - check fal.ai dashboard)
                        action_text = Text()
                        action_text.append("⚡ Action: ", style="bright_blue bold")
                        action_text.append(
                            "💰 Balance: fal.ai/dashboard • ", style="bright_yellow"
                        )
                        action_text.append(
                            "Monitoring for Interrupts...", style="bright_white"
                        )
                        action_spinner = Spinner(
                            "dots", text=action_text, style="bright_blue"
                        )

                        next_text = Text()
                        next_text.append("🔮 Next: ", style="bright_magenta bold")

                        # Calculate remaining (not yet started)
                        total_in_progress = processed + len(active_generations)
                        remaining_to_start = len(all_files) - total_in_progress

                        # Show next folder names (not yet processed or in progress)
                        if remaining_to_start > 0:
                            upcoming = all_files[
                                total_in_progress : total_in_progress + 3
                            ]  # Next 3 folders

                            # Get unique folder names
                            unique_folders = []
                            seen = set()
                            for folder_name in upcoming:
                                if folder_name not in seen:
                                    unique_folders.append(folder_name)
                                    seen.add(folder_name)

                            if unique_folders:
                                display = ", ".join(unique_folders[:3])
                                if remaining_to_start > 3:
                                    display += f" (+{remaining_to_start - 3} more)"
                                next_text.append(display, style="bright_yellow")
                            else:
                                next_text.append(
                                    f"{remaining_to_start} videos remaining in queue",
                                    style="bright_yellow",
                                )
                        else:
                            next_text.append(
                                "All generations complete", style="bright_green"
                            )
                        next_spinner = Spinner(
                            "dots", text=next_text, style="bright_magenta"
                        )

                        return Group(activity_spinner, action_spinner, next_spinner)

                    with Live(
                        create_colorful_spinners(),
                        console=console,
                        refresh_per_second=10,
                    ) as live:

                        def update_progress(completed, total, new_status):
                            nonlocal recent_status, processed, active_generations
                            recent_status = new_status
                            processed = completed

                            # Update active generations list
                            if "Generating:" in new_status:
                                filename = new_status.replace("Generating: ", "")
                                if filename not in active_generations:
                                    active_generations.append(filename)
                            elif "Completed:" in new_status or "Failed:" in new_status:
                                filename = new_status.replace(
                                    "Completed: ", ""
                                ).replace("Failed: ", "")
                                if filename in active_generations:
                                    active_generations.remove(filename)

                            current_pct = (
                                int((completed / total) * 100) if total > 0 else 0
                            )
                            progress.update(
                                main_task,
                                completed=completed,
                                description=f"📊 [cyan]{current_pct}% complete[/cyan] • 🚀",
                            )
                            live.update(create_colorful_spinners())

                        # Use concurrent processing with 5 workers (Kling API max)
                        use_source = self.config.get("use_source_folder", True)
                        _cfg_scale, _lock_ef = self._resolve_cfg_and_lock()
                        generator.process_all_images_concurrent(
                            target_directory=input_folder,
                            output_directory=self.config["output_folder"],
                            max_workers=5,
                            custom_prompt=self.get_current_prompt(),
                            negative_prompt=negative_prompt,  # Uses gated value from line 1525
                            progress_callback=update_progress,
                            use_source_folder=use_source,
                            duration=self.config.get("video_duration", 10),
                            aspect_ratio=self.config.get("aspect_ratio", "3:4"),
                            resolution=self.config.get("resolution", "720p"),
                            seed=self.config.get("seed", -1),
                            camera_fixed=self.config.get("camera_fixed", False),
                            generate_audio=self.config.get("generate_audio", False),
                            cfg_scale=_cfg_scale,
                            lock_end_frame=_lock_ef,
                        )

                        if total_files > 0:
                            progress.update(
                                main_task,
                                completed=total_files,
                                description="📊 [cyan]100% complete[/cyan] • 🎉 All files processed!",
                            )
                            recent_status = "Processing complete!"
                            active_generations.clear()
                            live.update(create_colorful_spinners())

                        time.sleep(2)

            else:
                # Verbose processing with concurrent execution
                print("Processing started with verbose logging...")
                print("Using 5 concurrent workers for faster processing...")
                use_source = self.config.get("use_source_folder", True)
                if use_source:
                    print("Output mode: Videos saved alongside source images")
                else:
                    print(f"Output folder: {self.config['output_folder']}")
                print("All detailed logs will be displayed below:")
                print()

                _cfg_scale, _lock_ef = self._resolve_cfg_and_lock()
                generator.process_all_images_concurrent(
                    target_directory=input_folder,
                    output_directory=self.config["output_folder"],
                    max_workers=5,
                    custom_prompt=self.get_current_prompt(),
                    negative_prompt=negative_prompt,  # Uses gated value from line 1525
                    use_source_folder=use_source,
                    duration=self.config.get("video_duration", 10),
                    aspect_ratio=self.config.get("aspect_ratio", "3:4"),
                    resolution=self.config.get("resolution", "720p"),
                    seed=self.config.get("seed", -1),
                    camera_fixed=self.config.get("camera_fixed", False),
                    generate_audio=self.config.get("generate_audio", False),
                    cfg_scale=_cfg_scale,
                    lock_end_frame=_lock_ef,
                )

        except Exception as e:
            print(f"\nError during processing: {e}")
            if self.verbose_logging:
                import traceback

                print(f"{traceback.format_exc()}")

        print("\nProcessing complete!")
        use_source = self.config.get("use_source_folder", True)
        if use_source:
            print("Videos saved alongside source images in their respective folders")
        else:
            print(f"Check your videos in: {self.config['output_folder']}")
        self.pause_review("\nPress Enter to return to main menu...")

    def run(self):
        """Main application loop"""
        self._run_startup_key_onboarding()
        while True:
            input_folder = self.run_configuration_menu()
            if input_folder:
                self.start_processing(input_folder)

    def run_auto_mode(self):
        """Direct launch into automation flow."""
        self._run_startup_key_onboarding()
        self.run_automation_menu()

    def run_manual_video_mode(self):
        """Direct launch into legacy manual Kling tools."""
        self._run_startup_key_onboarding()
        while True:
            selected = self._run_manual_kling_menu()
            if selected:
                self.start_processing(selected)
            else:
                return


def main(argv=None):
    """Entry point"""
    try:
        parser = argparse.ArgumentParser(add_help=True)
        parser.add_argument("--auto", action="store_true", help="Launch directly into the interactive automation menu")
        parser.add_argument(
            "--batch",
            metavar="ROOT",
            default=None,
            help="Run the automation pipeline NON-INTERACTIVELY over ROOT and exit "
            "(0=success, non-zero=failure). For cron / Task Scheduler.",
        )
        parser.add_argument(
            "--limit",
            metavar="N",
            default=None,
            # No argparse `choices=` here: an invalid choice makes argparse exit
            # with status 2, which collides with our "exit 2 = ran-but-needs-
            # attention" contract. run_automation_headless validates --limit and
            # returns 1 on a bad value instead (code-review Codex P2, PR #69).
            help="Headless --batch only: cap cases per run (1, 5, 10, or 'all'). "
            "Overrides automation_max_cases_per_run. An invalid value exits 1.",
        )
        parser.add_argument(
            "--reprocess",
            metavar="MODE",
            default=None,
            # No argparse choices= (would exit 2 on a bad value, colliding with
            # the "exit 2 = ran-but-needs-attention" contract). Validated inside
            # run_automation_headless -> exit 1 on a bad value (code-review Codex
            # P2 + Gemini, PR #69; matches --limit).
            help="Headless --batch only: reprocess mode (skip|overwrite|increment). "
            "An invalid value exits 1.",
        )
        parser.add_argument(
            "--yes",
            "-y",
            action="store_true",
            help="Headless --batch only: auto-approve the run (default in --batch).",
        )
        # Headless --batch operational overrides. These let a batch run differ
        # from the saved kling_config.json / distributable defaults WITHOUT
        # editing config on disk (e.g. test on the cheaper STANDARD model, pin a
        # specific oldcam version, force rPPG on/off). Threaded into
        # run_automation_headless and applied at the correct point relative to
        # the manifest fingerprint (fingerprinted automation_* keys BEFORE
        # create_or_load; cli_video_model / timeout / run-scope keys AFTER —
        # they are not fingerprinted; --front-glob keeps the manifest and the
        # per-case front-changed guard reprocesses affected cases).
        parser.add_argument(
            "--model",
            metavar="ENDPOINT",
            default=None,
            help="Headless --batch only: override the video model endpoint "
            "(e.g. fal-ai/kling-video/v2.5-turbo/standard/image-to-video). "
            "Overrides current_model. Not part of the manifest fingerprint.",
        )
        parser.add_argument(
            "--model-name",
            metavar="NAME",
            default=None,
            help="Headless --batch only: friendly display name for --model "
            "(e.g. 'Kling 2.5 Turbo Standard'). Defaults to a name derived "
            "from the endpoint when omitted.",
        )
        parser.add_argument(
            "--oldcam-version",
            metavar="VER",
            default=None,
            help="Headless --batch only: override the oldcam version selection "
            "(a single version like v13, a comma list like v13,v24, 'all', or "
            "'none' to skip oldcam). Overrides automation_oldcam_version. "
            "Changing this is part of the run identity, so it forces a fresh "
            "manifest.",
        )
        parser.add_argument(
            "--rppg",
            dest="rppg",
            action="store_true",
            default=None,
            help="Headless --batch only: force rPPG injection ON for this run "
            "(overrides automation_rppg_enabled). Forces a fresh manifest.",
        )
        parser.add_argument(
            "--no-rppg",
            dest="rppg",
            action="store_false",
            default=None,
            help="Headless --batch only: force rPPG injection OFF for this run.",
        )
        parser.add_argument(
            "--front-glob",
            metavar="GLOB",
            action="append",
            default=None,
            help="Headless --batch only: extra fnmatch pattern(s) for the per-folder "
            "front image, matched in addition to front.jpg/png/jpeg (e.g. "
            "'*id_photo*.jpg'). Repeatable. Keeps the manifest; any case whose "
            "front image re-selects to a different file is reprocessed from scratch.",
        )
        parser.add_argument(
            "--outpaint-timeout",
            metavar="SECONDS",
            default=None,
            help="Headless --batch only: override the fal outpaint poll timeout in "
            "seconds (clamped 30-300). Default 150; bump for unattended runs.",
        )
        parser.add_argument(
            "--provider",
            metavar="PROVIDER",
            default=None,
            help="Headless --batch only: force the outpaint provider (fal|bfl|auto) "
            "for BOTH front-expand and selfie-expand, overriding the saved "
            "automation_*_expand_provider keys. Use 'fal' to keep everything on "
            "fal.ai. Forces a fresh manifest.",
        )
        parser.add_argument("--manual-video", action="store_true", help="Launch legacy manual Kling tools")
        parser.add_argument("--gui", action="store_true", help="Launch GUI manual lab directly")
        parser.add_argument("--verbose-startup", action="store_true", help="Show full startup dependency diagnostics")
        parser.add_argument("--legacy-pauses", action="store_true", help="Restore legacy 'Press Enter to continue' pauses")
        args = parser.parse_args(argv)
        verbose_startup = args.verbose_startup or os.getenv("KLING_VERBOSE_STARTUP", "0") == "1"
        legacy_pauses = args.legacy_pauses or os.getenv("KLING_LEGACY_PAUSES", "0") == "1"
        crash_log_path = _enable_cli_crash_capture()
        if verbose_startup and crash_log_path:
            print(f"Native crash capture enabled: {crash_log_path}")

        if os.name == "nt":
            os.system("color")

        # Optional Python-side dependency check for direct python launches.
        if os.getenv("KLING_SKIP_PY_STARTUP_DEP_CHECK", "0") != "1":
            try:
                from dependency_checker import run_dependency_check

                if verbose_startup:
                    ok = run_dependency_check(auto_mode=True, enforce_all=True, install_external_tools=False)
                else:
                    print("Checking startup dependencies...")
                    dep_buffer = io.StringIO()
                    with contextlib.redirect_stdout(dep_buffer), contextlib.redirect_stderr(dep_buffer):
                        ok = run_dependency_check(auto_mode=True, enforce_all=True, install_external_tools=False)
                    if ok:
                        print("Startup dependency check: OK")
                    else:
                        print("Startup dependency check failed. Details below.")
                        print(dep_buffer.getvalue())
                if not ok:
                    sys.exit(1)
            except Exception as e:
                print(f"Warning: Startup dependency check failed: {e}")

        if sys.platform == "win32":
            try:
                import codecs

                sys.stdout = codecs.getwriter("utf-8")(sys.stdout.buffer, "strict")
                sys.stderr = codecs.getwriter("utf-8")(sys.stderr.buffer, "strict")
            except:
                pass

        app = KlingAutomationUI(legacy_pauses=legacy_pauses)
        if args.batch is not None:
            # Non-interactive batch: run + exit with the runner's status code so
            # cron / Task Scheduler can detect failures. --yes is implied here.
            rc = app.run_automation_headless(
                args.batch,
                auto_approve=True,
                max_cases_override=args.limit,
                reprocess_override=args.reprocess,
                model_override=args.model,
                model_name_override=args.model_name,
                oldcam_version_override=args.oldcam_version,
                rppg_override=args.rppg,
                front_globs_override=args.front_glob,
                outpaint_timeout_override=args.outpaint_timeout,
                provider_override=args.provider,
            )
            sys.exit(int(rc))
        if args.gui:
            app.launch_gui()
            return
        if args.auto:
            app.run_auto_mode()
            return
        if args.manual_video:
            app.run_manual_video_mode()
            return
        app.run()
    except KeyboardInterrupt:
        print("\n\nGoodbye!")
        sys.exit(0)
    except Exception as e:
        logging.error("Fatal error: %s", e)
        logging.error("Fatal traceback:\n%s", traceback.format_exc())
        print(f"Fatal error: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
