"""
Queue Manager - Thread-safe queue for processing images with status tracking.
"""

import threading
import os
import logging
import re
import subprocess
import sys
import shutil
import time
from dataclasses import dataclass, field
from typing import Callable, Optional, List, Tuple, Dict
from pathlib import Path
from datetime import datetime

from path_utils import (
    VALID_EXTENSIONS,
    get_app_dir,
    get_gen_images_folder,
    get_resource_dir,
    sanitize_stem,
)
from log_utils import format_exception_detail, format_exception_traceback

logger = logging.getLogger(__name__)


def _model_short_from_endpoint(endpoint: str) -> str:
    """Derive model short name using the same mapping logic as generator.

    Each model variant (pro/standard/master) gets a distinct name so filenames
    are unambiguous.  E.g. k25tPro vs k25tStd for 2.5 Turbo Pro vs Standard.
    """
    endpoint = (endpoint or "").lower()
    if not endpoint:
        return "model"

    def _tier(ep: str) -> str:
        if "/master/" in ep or ep.endswith("/master"):
            return "Master"
        if "/standard/" in ep or ep.endswith("/standard"):
            return "Std"
        return "Pro"

    if "kling" in endpoint:
        if "/v3/" in endpoint or "/v3-" in endpoint or endpoint.endswith("/v3"):
            return "k30pro" if "pro" in endpoint else "k30std"
        if "v2.5-turbo" in endpoint or "v2.5/turbo" in endpoint or "v2.5turbo" in endpoint:
            return f"k25t{_tier(endpoint)}"
        if "v2.6" in endpoint:
            return f"k26{_tier(endpoint).lower()}"
        if "v2.5" in endpoint:
            return "k25"
        if "v2.1" in endpoint:
            return f"k21{_tier(endpoint).lower()}"
        if "v2/" in endpoint or endpoint.endswith("/v2"):
            return f"k20{_tier(endpoint).lower()}"
        if "v1.6" in endpoint:
            return f"k16{_tier(endpoint).lower()}"
        if "v1.5" in endpoint:
            return f"k15{_tier(endpoint).lower()}"
        if "o1" in endpoint:
            return "kO1"
        return "kling"

    if "wan" in endpoint:
        return "wan25" if "25" in endpoint else "wan"
    if "veo3" in endpoint:
        return "veo3"
    if "veo" in endpoint:
        return "veo"
    if "ovi" in endpoint:
        return "ovi"
    if "ltx" in endpoint:
        return "ltx2"
    if "pixverse" in endpoint:
        return "pix5" if "v5" in endpoint else "pixverse"
    if "hunyuan" in endpoint:
        return "hunyuan"
    if "minimax" in endpoint:
        return "minimax"

    parts = endpoint.replace("/image-to-video", "").split("/")
    for part in reversed(parts):
        if part and part != "fal-ai":
            clean = part.replace("-", "").replace("_", "")[:8]
            return clean if clean else "model"
    return "model"


def _get_model_short_name(generator) -> str:
    """Get model short name with compatibility fallback for older generators."""
    getter = getattr(generator, "get_model_short_name", None)
    if callable(getter):
        try:
            value = str(getter()).strip()
            if value:
                return value
        except (AttributeError, TypeError, ValueError) as exc:
            logger.warning(
                "Falling back to endpoint-based model short name after get_model_short_name() error: %s",
                exc,
            )

    endpoint = str(getattr(generator, "model_endpoint", "")).strip()
    if endpoint:
        return _model_short_from_endpoint(endpoint)

    fallback = str(getattr(generator, "model_display_name", "model"))
    fallback = re.sub(r"[^A-Za-z0-9]+", "", fallback).lower()
    return fallback[:16] if fallback else "model"


def get_output_video_path(
    image_path: str,
    output_folder: str,
    generator,
    config: dict = None,
    timestamp: Optional[datetime] = None,
) -> Path:
    """Get the output video path: {stem}_{model_short}_{index}.mp4

    The index is determined by scanning output_folder for existing files so
    each call returns the next available path for this image+model combination.
    """
    image_name = sanitize_stem(Path(image_path).stem, default="image")
    try:
        filename = generator.get_output_filename(image_name, output_folder)
    except (TypeError, AttributeError) as _e:
        # Backward compatibility for older generator signatures.
        # AttributeError can occur when a generator expects a config dict but
        # receives output_folder (a string) and calls .get() on it.
        # Only treat AttributeError as a legacy-signature indicator when
        # output_folder is a str (the always-true case); re-raise for anything
        # else so genuine internal bugs are not silently swallowed.
        if isinstance(_e, AttributeError) and not isinstance(output_folder, str):
            raise
        logger.warning("Legacy generator API fallback triggered (%s); trying older signatures", _e)
        try:
            filename = generator.get_output_filename(image_name, config or {}, timestamp)
        except (TypeError, AttributeError):
            filename = generator.get_output_filename(image_name)
    return Path(output_folder) / filename


def check_video_exists(
    image_path: str,
    output_folder: str,
    generator,
    config: dict = None,
) -> Tuple[bool, Optional[str]]:
    """Check if any video already exists for this image+model combination.

    Matches pattern: {stem}_{model_short}_*.mp4  (and *_looped.mp4 variant).

    Returns:
        (exists: bool, found_path: str | None)
    """
    import glob

    image_name = sanitize_stem(Path(image_path).stem, default="image")
    model_short = _get_model_short_name(generator)
    prompt_slot = (config or {}).get("current_prompt_slot", getattr(generator, "prompt_slot", 1))
    try:
        prompt_slot = max(1, int(prompt_slot))
    except (TypeError, ValueError):
        prompt_slot = 1

    slot_prefix = f"{image_name}_{model_short}_p{prompt_slot}_"
    slot_matches = sorted(glob.glob(str(Path(output_folder) / f"{slot_prefix}*.mp4")))
    if slot_matches:
        return True, slot_matches[0]

    # Backward compatibility: legacy filenames without slot suffix map to slot 1.
    if prompt_slot == 1:
        legacy_prefix = f"{image_name}_{model_short}_"
        legacy_matches = sorted(glob.glob(str(Path(output_folder) / f"{legacy_prefix}*.mp4")))
        for path in legacy_matches:
            if f"_{model_short}_p" not in Path(path).stem:
                return True, path
    return False, None


# Model duration constraints (from fal.ai API documentation)
# Format: (pattern, allowed_durations, priority)
# Higher priority patterns are checked first to ensure more specific matches
MODEL_DURATION_CONSTRAINTS = [
    # Kling family - most specific patterns first
    ("kling-video/v2.1", [5, 10], 3),
    ("kling-video/v2.5", [5, 10], 3),
    ("kling-video/v2", [5, 10], 2),  # Catch-all for v2.x
    ("kling-video/v1", [5, 10], 2),
    ("kling-video/o1", [5, 10], 2),

    # Wan models - specific versions first
    ("wan/v2.6", [5, 10, 15], 3),  # v2.6 added 15-second support
    ("wan/v2.5", [5, 10], 3),
    ("wan-25-preview", [5, 10], 3),

    # Pixverse models - specific versions first
    ("pixverse/v5.5", [5, 8, 10], 3),
    ("pixverse/v5", [5, 8], 3),

    # Other models - alphabetically sorted
    ("haiper-video-v2", [4, 6], 2),
    ("hunyuan-video", [5, 10], 2),
    ("ltx-2", [5, 10], 2),
    ("minimax-video", [6], 2),
    ("ovi", [5, 10], 2),
    ("veo3", [5, 6, 7, 8], 2),
    ("vidu", [2, 3, 4, 5, 6, 7, 8], 2),  # Vidu Q2 supports 2-8 seconds
]


def validate_duration(model_endpoint: str, duration: int) -> None:
    """Validate duration against model-specific constraints.

    Uses priority-based pattern matching to ensure more specific model versions
    are matched before generic patterns (e.g., "kling-video/v2.5" before "kling-video/v2").

    Args:
        model_endpoint: Full model endpoint (e.g., "fal-ai/kling-video/v2.1/pro/image-to-video")
        duration: Requested duration in seconds

    Raises:
        ValueError: If duration is not valid for the model
    """
    if not isinstance(duration, int) or duration <= 0:
        raise ValueError(f"Duration must be a positive integer, got: {duration}")

    # Sort constraints by priority (highest first) for most specific matching
    sorted_constraints = sorted(MODEL_DURATION_CONSTRAINTS, key=lambda x: x[2], reverse=True)

    # Check against known constraints with priority-based matching
    for pattern, allowed_durations, _ in sorted_constraints:
        if pattern in model_endpoint:
            if duration not in allowed_durations:
                allowed_str = ', '.join(f"{d}s" for d in allowed_durations)
                model_name = model_endpoint.split('/')[-1] if '/' in model_endpoint else model_endpoint
                raise ValueError(
                    f"Duration {duration}s invalid for {model_name}. "
                    f"Allowed durations: {allowed_str}"
                )
            logger.debug(f"Duration {duration}s validated for pattern '{pattern}'")
            return  # Valid duration found

    # Unknown model - log warning but allow (graceful degradation)
    logger.warning(
        f"No duration constraints found for model '{model_endpoint}'. "
        f"Proceeding with duration {duration}s (may fail at API level if unsupported)."
    )


def get_duration_options_for_model(model_endpoint: str) -> list:
    """Get available duration options for a given model.
    
    Args:
        model_endpoint: Full model endpoint
        
    Returns:
        List of allowed duration values in seconds, or [5, 10] if unknown
    """
    # Sort constraints by priority for most specific matching
    sorted_constraints = sorted(MODEL_DURATION_CONSTRAINTS, key=lambda x: x[2], reverse=True)
    
    for pattern, allowed_durations, _ in sorted_constraints:
        if pattern in model_endpoint:
            return allowed_durations
    
    # Default for unknown models
    return [5, 10]


_TF_NOISE_PATTERNS = (
    "tensorflow/",
    "onednn",
    "w0000 ",
    "tflite",
    "inference_feedback_manager",
    "warning:tensorflow",
    "tf_enable_onednn",
    "face_landmarker_graph",
    "xnnpack",
    "i tensorflow",
    "mediapipe version",
    "mediapipe graph",
    "mediapipe init",
    "mediapipe info",
    # MediaPipe clearcut telemetry noise (Android playlog tries to phone home,
    # fails with FAILED_PRECONDITION on dev machines, and dumps a 3-line trace).
    "portable_clearcut_uploader",
    "failed to send to clearcut",
    "=== source location trace: ===",
    "wireless/android/play/playlog",
)


def _is_tf_noise(line: str) -> bool:
    low = line.lower()
    return any(pat in low for pat in _TF_NOISE_PATTERNS)


# Lines from the Oldcam subprocess that already have a friendlier equivalent
# in the queue_manager's own logging (or are pure path-dump noise). Routed
# to the file log only to keep the user-facing panel readable.
# Substring match, lowercase, mirroring _TF_NOISE_PATTERNS.
_PANEL_NOISE_PATTERNS = (
    "input :",
    "input:",
    "output:",
    "saved video to:",
    "video processing complete.",
    "finalizing video with ffmpeg codec",
)


def _is_panel_noise(line: str) -> bool:
    low = line.lower()
    return any(pat in low for pat in _PANEL_NOISE_PATTERNS)


# Diagnostic lines emitted by rPPG/run_rppg.bat's dependency self-heal — the
# per-module rppg_import_diag.py report ("[rppg-diag] ...") plus the launcher's
# own WARN/ERROR setup status. These must be promoted to the user-facing panel
# (at warning/error) instead of being demoted to a hidden "debug" line by the
# progress tracker, so a non-technical user can see EXACTLY which dependency
# failed without digging out .launcher_state/rppg.log (v2.16 logging overhaul).
_RPPG_SETUP_DIAG_PATTERNS = (
    "[rppg-diag]",
    "rppg deps missing",
    "rppg deps still missing",
    "rppg deps installed",
    "installing mediapipe separately",
    "retrying without binary constraint",
    "self-heal pip install",
    "syncing repo requirements",
)


def _is_rppg_setup_diag(line: str) -> bool:
    low = line.lower()
    return any(pat in low for pat in _RPPG_SETUP_DIAG_PATTERNS)


def _extract_rppg_failed_modules(lines) -> str:
    """Distil the failing rPPG dependency name(s) into a short friendly string.

    Parses the launcher's ``[rppg-diag] BROKEN <name>`` / ``MISSING <name>``
    per-module lines and the ``RESULT: N required module(s) not importable:
    a, b`` verdict (and a legacy ``Still missing: a, b`` line) into a concise
    comma-joined list like ``"mediapipe (broken), scipy (missing)"`` — for a
    ONE-line user-facing summary, NOT a raw dump. Returns "" if nothing
    nameable is found (caller then shows only the log-file pointer).
    """
    import re

    broken_missing: "list[str]" = []
    result_modules: "list[str]" = []
    seen = set()

    def _add(name: str, kind: str) -> None:
        name = name.strip()
        if name and name.lower() not in seen:
            seen.add(name.lower())
            broken_missing.append(f"{name} ({kind})")

    for raw in lines or []:
        ln = str(raw).strip()
        low = ln.lower()
        # "[rppg-diag] BROKEN  mediapipe    (required) ModuleNotFoundError: ..."
        m = re.match(r"\[rppg-diag\]\s+(BROKEN|MISSING)\s+(\S+)", ln, re.IGNORECASE)
        if m:
            _add(m.group(2), m.group(1).lower())
            continue
        # "[rppg-diag] RESULT: 1 required module(s) not importable: mediapipe"
        if "not importable:" in low:
            tail = ln.split("not importable:", 1)[1]
            result_modules = [t.strip() for t in tail.split(",") if t.strip()]
            continue
        # Legacy: "Still missing: scipy, absl"
        if low.startswith("still missing:"):
            tail = ln.split(":", 1)[1]
            for t in (x.strip() for x in tail.split(",")):
                _add(t, "missing")

    if broken_missing:
        return ", ".join(broken_missing)
    if result_modules:
        return ", ".join(result_modules)
    return ""


def _is_rppg_failure_detail_line(line: str) -> bool:
    """True only for diagnostic lines that explain a FAILURE.

    Used when surfacing rPPG failure detail into the GUI log: a failure that
    occurs AFTER imports succeed must not echo "[rppg-diag] OK ..." lines or the
    "all required modules import OK" verdict as if they explained the failure
    (CodeRabbit Major, PR #67). Matches per-module BROKEN/MISSING, the
    failing-RESULT verdict, "still missing", and the numpy-2 warning — but NOT
    the OK lines or the all-clear verdict.
    """
    low = line.lower()
    if low.startswith("[rppg-diag]"):
        return (
            "broken" in low
            or "missing" in low
            or "not importable" in low  # "RESULT: N required module(s) not importable"
            or ("numpy-version" in low and "warning" in low)
        )
    return "still missing" in low


def get_next_available_path(
    image_path: str,
    output_folder: str,
    generator,
    config: dict = None,
    timestamp: Optional[datetime] = None,
) -> Path:
    """Return the next available output path for this image+model combination.

    Starts from generator-provided naming, then enforces a free candidate locally
    to keep increment behavior deterministic regardless of generator internals.
    """
    candidate = get_output_video_path(image_path, output_folder, generator, config, timestamp)
    candidate_loop = candidate.with_name(f"{candidate.stem}_looped{candidate.suffix}")
    if not candidate.exists() and not candidate_loop.exists():
        return candidate

    match = re.match(r"^(.*_)(\d+)$", candidate.stem)
    if match:
        stem_prefix = match.group(1)
        counter = int(match.group(2))
    else:
        stem_prefix = f"{candidate.stem}_"
        counter = 0

    while True:
        counter += 1
        candidate = candidate.with_name(f"{stem_prefix}{counter}{candidate.suffix}")
        candidate_loop = candidate.with_name(f"{candidate.stem}_looped{candidate.suffix}")
        if not candidate.exists() and not candidate_loop.exists():
            return candidate


@dataclass
class QueueItem:
    """Represents a single queued image with status tracking."""

    path: str
    status: str = "pending"  # "pending", "processing", "completed", "failed"
    error_message: Optional[str] = None
    output_path: Optional[str] = None
    # rPPG dispatch state — populated by the queue worker so the
    # post-processing summary line and the NORPPG filename marker can
    # tell whether rPPG was requested but failed (vs. simply disabled).
    rppg_requested: bool = False
    rppg_succeeded: bool = False
    # Per-item progress for the queue row. ``stage`` is one of
    # "queued"/"kling"/"rppg"/"loop"/"oldcam"/"done"/"failed".
    # ``stage_percent`` is 0-100 within the current stage; the queue
    # widget renders a unicode bar from these two fields.
    stage: str = "queued"
    stage_percent: int = 0

    @property
    def filename(self) -> str:
        return os.path.basename(self.path)

    @property
    def source_folder(self) -> str:
        return os.path.dirname(self.path)


class QueueManager:
    """Thread-safe queue manager for image processing."""

    MAX_QUEUE_SIZE = 50

    def __init__(
        self,
        generator,
        config_getter: Callable[[], dict],
        log_callback: Callable[[str, str], None],
        queue_update_callback: Callable[[], None],
        processing_complete_callback: Optional[Callable[[QueueItem], None]] = None,
    ):
        """
        Initialize the queue manager.

        Args:
            generator: FalAIKlingGenerator instance
            config_getter: Function that returns current config dict
            log_callback: Function(message, level) for logging
            queue_update_callback: Function called when queue changes
            processing_complete_callback: Function called when an item finishes
        """
        self.items: List[QueueItem] = []
        self.lock = threading.Lock()
        self.generator = generator
        self.get_config = config_getter
        self.log = log_callback
        self.update_queue_display = queue_update_callback
        self.on_processing_complete = processing_complete_callback

        self.is_paused = False
        self.is_running = False
        self.worker_thread: Optional[threading.Thread] = None
        self._stop_flag = False
        # Abort support: a threading.Event the GUI sets to cancel the IN-FLIGHT
        # job (rPPG can run 10 iters / 20+ min, so item-boundary stop isn't
        # enough). The active long-running subprocess handle is published here so
        # abort_current_job() can .kill() it immediately instead of waiting for
        # the stage to finish. Both are reset at the start of each new item.
        self._abort_event = threading.Event()
        self._active_subprocess: Optional[subprocess.Popen] = None
        self._oldcam_deps_status_by_version: Dict[str, bool] = {}
        self._last_oldcam_run_summary: Optional[Dict[str, object]] = None
        self._oldcam_rerun_thread: Optional[threading.Thread] = None

    def log_verbose(self, message: str, level: str = "info"):
        """Log a message only if verbose mode is enabled."""
        config = self.get_config()
        if config.get("verbose_gui_mode", False):
            self.log(message, level)

    def validate_file(self, file_path: str) -> tuple:
        """
        Validate a file for processing.

        Returns:
            (is_valid, error_message)
        """
        if not os.path.exists(file_path):
            return False, f"File not found: {file_path}"

        ext = os.path.splitext(file_path)[1].lower()
        if ext not in VALID_EXTENSIONS:
            return False, f"Unsupported format: {ext}"

        # Check if already in queue
        with self.lock:
            for item in self.items:
                if item.path == file_path and item.status in ("pending", "processing"):
                    return False, "Already in queue"

        return True, ""

    def add_to_queue(self, file_path: str) -> tuple:
        """
        Add a file to the processing queue.

        Returns:
            (success, message)
        """
        # Validate file
        is_valid, error = self.validate_file(file_path)
        if not is_valid:
            return False, error

        with self.lock:
            # Check queue limit
            pending_count = sum(
                1 for item in self.items if item.status in ("pending", "processing")
            )
            if pending_count >= self.MAX_QUEUE_SIZE:
                return False, f"Queue full ({self.MAX_QUEUE_SIZE} items max)"

            # Add to queue
            item = QueueItem(path=file_path)
            self.items.append(item)

        self.update_queue_display()
        self.log(f"Added to queue: {os.path.basename(file_path)}", "info")

        # Start processing if not already running
        if not self.is_running and not self.is_paused:
            self.start_processing()

        return True, "Added to queue"

    def start_processing(self):
        """Start the worker thread to process queue items."""
        if self.is_running:
            return

        self._stop_flag = False
        self.is_running = True
        self.is_paused = False
        self.worker_thread = threading.Thread(target=self._process_queue, daemon=True)
        self.worker_thread.start()
        self.log("▶ Processing started", "info")

    def pause_processing(self):
        """Pause processing after current item completes."""
        self.is_paused = True
        self.log("Processing paused", "warning")

    def resume_processing(self):
        """Resume processing."""
        self.is_paused = False
        if not self.is_running:
            self.start_processing()
        else:
            self.log("Processing resumed", "info")

    def stop_processing(self):
        """Stop processing completely."""
        self._stop_flag = True
        self.is_paused = True

    def abort_current_job(self):
        """Abort the IN-FLIGHT job immediately (the GUI 'Abort' button).

        Sets the abort Event (which the rPPG/Oldcam stream loops poll every
        ≤1s) AND kills the active subprocess right now so the user doesn't wait
        out the rest of a 20-minute rPPG run. Stops the queue after this item
        (sets is_paused) but does NOT mark remaining items failed — the user
        can resume. Safe to call from the GUI thread: Event.set() + Popen.kill()
        are both thread-safe, and the worker thread observes the Event on its
        next poll.
        """
        self._abort_event.set()
        # Also pause the queue so it doesn't roll straight into the next item.
        self.is_paused = True
        proc = self._active_subprocess
        if proc is not None:
            try:
                if proc.poll() is None:
                    proc.kill()
            except Exception:  # noqa: BLE001 — handle may have just exited
                pass
        self.log("⛔ Abort requested — stopping the current job…", "warning")

    def _abort_requested(self) -> bool:
        """True if the user asked to abort the in-flight job."""
        return self._abort_event.is_set()

    def _handle_item_abort(self, item):
        """Clean up after a user Abort mid-item (code-review Codex P2).

        Called from the worker loop when ``_abort_requested()`` is observed
        between post-processing stages. Re-queues the item (so a later Resume
        re-runs it cleanly rather than leaving a half-finished output marked
        done), drops the worker out of the run loop (the queue is already
        paused by abort_current_job), and logs it. Does NOT mark the item
        failed — an abort is a user choice, not an error.
        """
        with self.lock:
            if item.status == "processing":
                item.status = "pending"
                item.stage = "queued"
                item.stage_percent = 0
        self.is_running = False
        self.log(
            f"⛔ Aborted '{item.filename}' mid-job; re-queued. "
            "Press Resume to re-run it.",
            "warning",
        )
        self.update_queue_display()

    def _publish_active_subprocess(self, proc: Optional[subprocess.Popen]):
        """Record the long-running child so abort_current_job() can kill it.

        Passed as ``on_process_start`` to the rPPG streamer and set directly by
        the Oldcam loop. A dead handle left here is harmless — abort_current_job
        only kills it when ``poll() is None`` — but callers clear it (pass None)
        when the stage ends so the next abort can't target a finished process.
        """
        self._active_subprocess = proc

    def retry_failed(self):
        """Re-queue all failed items."""
        count = 0
        with self.lock:
            for item in self.items:
                if item.status == "failed":
                    item.status = "pending"
                    item.error_message = None
                    count += 1

        if count > 0:
            self.update_queue_display()
            self.log(f"Retrying {count} failed item(s)", "info")
            if not self.is_running and not self.is_paused:
                self.start_processing()
        return count

    def clear_queue(self):
        """Remove all pending and failed items from queue."""
        with self.lock:
            self.items = [
                item
                for item in self.items
                if item.status in ("processing", "completed")
            ]
        self.update_queue_display()
        self.log("Queue cleared", "info")

    def remove_item(self, index: int):
        """Remove a specific item by index."""
        with self.lock:
            if 0 <= index < len(self.items):
                item = self.items[index]
                if item.status not in ("processing",):
                    self.items.pop(index)
                    self.update_queue_display()
                    return True
        return False

    def get_items(self) -> List[QueueItem]:
        """Get a copy of all queue items."""
        with self.lock:
            return list(self.items)

    def get_pending_count(self) -> int:
        """Get count of pending items."""
        with self.lock:
            return sum(1 for item in self.items if item.status == "pending")

    def get_stats(self) -> dict:
        """Get queue statistics."""
        with self.lock:
            return {
                "pending": sum(1 for item in self.items if item.status == "pending"),
                "processing": sum(
                    1 for item in self.items if item.status == "processing"
                ),
                "completed": sum(
                    1 for item in self.items if item.status == "completed"
                ),
                "failed": sum(1 for item in self.items if item.status == "failed"),
                "total": len(self.items),
            }

    @staticmethod
    def _oldcam_version_key(version: str) -> int:
        """Sortable numeric key for versions like v7, v8, v10."""
        match = re.fullmatch(r"v(\d+)", str(version or "").strip().lower())
        if not match:
            return -1
        try:
            return int(match.group(1))
        except ValueError:
            return -1

    def _discover_oldcam_versions(self) -> List[str]:
        """Discover available oldcam folders like oldcam-v7, oldcam-v8."""
        roots = [
            Path(get_app_dir()),
            Path(get_resource_dir()),
            Path(__file__).parent.parent.resolve(),
        ]
        found = set()
        for root in roots:
            if not root.exists():
                continue
            try:
                for candidate in root.iterdir():
                    if not candidate.is_dir():
                        continue
                    match = re.fullmatch(r"oldcam-v(\d+)", candidate.name.lower())
                    if not match:
                        continue
                    if (candidate / "launcher.py").exists():
                        found.add(f"v{int(match.group(1))}")
            except OSError:
                continue

        if not found:
            return ["v7", "v8", "v9", "v10"]
        return sorted(found, key=self._oldcam_version_key)

    def _get_selected_oldcam_versions(self) -> List[str]:
        """Resolve selected Oldcam versions from config with legacy fallback."""
        config = self.get_config()
        available = self._discover_oldcam_versions()

        configured = config.get("oldcam_versions")
        selected: List[str] = []
        has_versions_key = isinstance(configured, list)
        if has_versions_key:
            selected = [str(v).lower() for v in configured if isinstance(v, str)]

        if not selected and not has_versions_key:
            legacy = str(config.get("oldcam_version", "v9")).lower()
            if legacy == "all":
                selected = list(available)
            elif legacy:
                selected = [legacy]

        valid = sorted(
            {version for version in selected if version in available},
            key=self._oldcam_version_key,
        )
        if valid:
            return valid
        if has_versions_key:
            return []
        if available:
            # Intentional product default: v9 is the safe fallback when no valid
            # selection survives migration/validation. Explicit legacy choices
            # (e.g. oldcam_version=v7) still win via the valid path above.
            if "v9" in available:
                return ["v9"]
            latest = available[-1]
            return [latest]
        return ["v9"]

    def _get_oldcam_versions_to_run(self) -> List[str]:
        """Return selected oldcam versions in ascending order."""
        return self._get_selected_oldcam_versions()

    def _get_next_incremented_oldcam_input(self, source_video: Path, versions: List[str]) -> Path:
        """Return copied input whose derived oldcam outputs do not exist for any selected version."""
        counter = 2
        while True:
            candidate_input = source_video.with_name(
                f"{source_video.stem}_{counter}{source_video.suffix}"
            )
            candidate_outputs = [
                self._build_oldcam_output_path(candidate_input, version)
                for version in versions
            ]
            if (
                not candidate_input.exists()
                and all(not candidate_output.exists() for candidate_output in candidate_outputs)
            ):
                shutil.copy2(source_video, candidate_input)
                return candidate_input
            counter += 1

    def rerun_oldcam_only(
        self,
        video_path: str,
        completion_callback: Optional[
            Callable[[bool, str, Optional[str], Optional[str]], None]
        ] = None,
    ) -> bool:
        """Apply Oldcam-only rerun to an existing video without new Kling generation.

        Args:
            video_path: Existing video file path used as Oldcam source.
            completion_callback: Optional callback(success, source_video, output_path, error).
        """
        source_video = Path(str(video_path or "")).expanduser().resolve()
        if not source_video.exists() or not source_video.is_file():
            message = f"Oldcam rerun source video not found: {source_video}"
            self.log(message, "warning")
            if completion_callback:
                completion_callback(False, str(source_video), None, message)
            return False

        if self.is_running:
            message = "Cannot run Oldcam rerun while queue processing is active"
            self.log(message, "warning")
            if completion_callback:
                completion_callback(False, str(source_video), None, message)
            return False

        if self._oldcam_rerun_thread and self._oldcam_rerun_thread.is_alive():
            message = "Oldcam rerun already running"
            self.log(message, "warning")
            if completion_callback:
                completion_callback(False, str(source_video), None, message)
            return False

        def _worker():
            nonlocal source_video
            temp_input: Optional[Path] = None
            try:
                config = self.get_config()
                versions_to_run = self._get_oldcam_versions_to_run()
                selected_versions = self._get_selected_oldcam_versions()
                rppg_on = self._rppg_enabled()
                loop_on = bool(config.get("loop_videos", False))

                # User feedback 2026-05-22: re-run on a picked video must
                # apply WHATEVER post-processes are selected — not just
                # Oldcam. So rPPG-only, Loop-only, or any combination is
                # valid. Only error when NONE of (rPPG, Loop, Oldcam) are
                # selected — genuinely nothing to apply.
                if not (rppg_on or loop_on or versions_to_run):
                    message = (
                        "Re-run: nothing to apply (rPPG, Loop, and Oldcam "
                        "all unselected — pick at least one post-process)."
                    )
                    self.log(message, "warning")
                    if completion_callback:
                        completion_callback(False, str(source_video), None, message)
                    return

                # Mirror the normal queue's order (Phase E of polish/v2.3,
                # 2026-05-22): rPPG -> Loop -> Oldcam. The re-run path's
                # ``source_video`` plays the same role as the main queue's
                # raw Kling output.
                #
                # Skip rPPG step entirely on inputs that are already rPPG
                # artifacts (the _rppg_video helper enforces this too, but
                # short-circuiting here also avoids the misleading
                # "Applying rPPG injection..." log line).
                #
                # Track which stages actually produced output so the
                # no-Oldcam early-return below can distinguish "rPPG+Loop
                # both succeeded → genuine new output" from "all selected
                # stages failed → reporting the unchanged input as the
                # rerun output would be a false-success lie" (subagent
                # HIGH on a17fb658). Each stage flips its slot True only
                # when it returns a truthy new path.
                from automation.rppg import is_rppg_artifact
                rppg_produced = False
                loop_produced = False
                rppg_attempted = False
                loop_attempted = False
                # CodeRabbit Major (2026-05-22): capture
                # "already looped" status from the ORIGINAL source
                # stem BEFORE rPPG renames it. rPPG turns
                # ``clip_looped.mp4`` into
                # ``clip_looped-rppg-<metrics>.mp4``, which no longer
                # ends with ``_looped``. Without this pre-capture the
                # later Loop step would proceed and produce
                # ``clip_looped-rppg_looped.mp4`` instead of skipping.
                source_was_already_looped = source_video.stem.endswith("_looped")
                if rppg_on and not is_rppg_artifact(source_video):
                    rppg_attempted = True
                    self.log("Re-Run: applying rPPG (rPPG enabled)", "info")
                    rppg_first = self._rppg_video(str(source_video), QueueItem(str(source_video)))
                    if rppg_first:
                        source_video = Path(rppg_first).resolve()
                        rppg_produced = True
                        self.log(f"Re-Run rPPG intermediate: {source_video.name}", "debug")
                    else:
                        self.log(
                            "Re-Run: rPPG step failed/skipped; continuing with un-injected source",
                            "warning",
                        )

                # Loop step: same gating as before — skip if the input
                # was already a loop (use the pre-rPPG capture above)
                # to avoid ``..._looped_looped.mp4``. The source may
                # now carry a ``-rppg`` suffix from the prior step,
                # which is fine — the rPPG'd intermediate is still
                # logically a loop and the looper would just stack
                # another ``_looped`` on top if we let it.
                loop_already_satisfied = False
                if loop_on:
                    # Belt-and-suspenders: also check the CURRENT stem
                    # in case rPPG was skipped (then current ==
                    # original). Either way, if the input is a loop,
                    # skip the loop step.
                    if source_was_already_looped or source_video.stem.endswith("_looped"):
                        # Subagent HIGH on 69dee05: when the user
                        # selected Loop but the input is ALREADY a
                        # loop, treat that as a satisfied stage — the
                        # source_video already IS the loop they
                        # wanted, so producing nothing new is the
                        # correct outcome (NOT a failure). Track via a
                        # separate flag so the no-Oldcam early-return
                        # branch can report success-with-no-new-output
                        # instead of "every post-process failed" or
                        # "nothing to do".
                        loop_already_satisfied = True
                        self.log(
                            f"Re-Run: source already looped ({source_video.name}); skipping loop step",
                            "info",
                        )
                    else:
                        loop_attempted = True
                        self.log("Re-Run: looping source (loop enabled)", "info")
                        looped_path = self._loop_video(str(source_video), QueueItem(str(source_video)))
                        if looped_path:
                            source_video = Path(looped_path).resolve()
                            loop_produced = True
                            # "Looped video saved: <name> (X.Y MB)" already
                            # told the user the loop succeeded; this is just
                            # the structured event for the file log.
                            self.log(f"Re-Run loop intermediate: {source_video.name}", "debug")
                        else:
                            self.log(
                                "Re-Run: loop step failed; falling back to un-looped source",
                                "warning",
                            )

                # No-Oldcam early-return path:
                #   - SUCCESS when rPPG and/or Loop actually produced new
                #     output (`source_video` now points at it).
                #   - FAILURE when every selected pre-Oldcam stage either
                #     was attempted-and-failed (e.g. scipy missing crashing
                #     rPPG) OR no stage was attempted (e.g. rppg_on was
                #     True but the source was already a rPPG artifact, AND
                #     loop_on was False). Reporting the unchanged input
                #     file as the "rerun output" with success=True was
                #     the bug subagent HIGH#3 on a17fb658 flagged.
                if not versions_to_run:
                    any_produced = rppg_produced or loop_produced
                    any_attempted = rppg_attempted or loop_attempted
                    if any_produced:
                        self.log(
                            f"Re-run complete (no Oldcam selected): {source_video.name}",
                            "info",
                        )
                        if completion_callback:
                            completion_callback(True, str(source_video), str(source_video), None)
                        return
                    # Subagent HIGH on 69dee05: when the only selected
                    # post-process was Loop AND the input was already
                    # a loop, the source_video already IS the
                    # deliverable — report SUCCESS with the original
                    # path, not failure.
                    if loop_already_satisfied and not rppg_attempted:
                        self.log(
                            f"Re-run complete: source already satisfies Loop ({source_video.name})",
                            "info",
                        )
                        if completion_callback:
                            completion_callback(True, str(source_video), str(source_video), None)
                        return
                    # All attempted stages failed, or no stage attempted
                    # (e.g. rPPG selected but the picked video was already
                    # a rPPG artifact, AND no other stage selected). Either
                    # way: nothing was produced — report failure honestly.
                    if any_attempted:
                        message = (
                            "Re-run: every selected post-process "
                            "(rPPG / Loop) failed — no output produced."
                        )
                    else:
                        message = (
                            "Re-run: nothing to do — the picked video is "
                            "already a rPPG artifact and Loop/Oldcam are "
                            "unselected."
                        )
                    self.log(message, "warning")
                    if completion_callback:
                        completion_callback(False, str(source_video), None, message)
                    return

                primary_version = max(versions_to_run, key=self._oldcam_version_key)
                allow_reprocess = bool(config.get("allow_reprocess", False))
                reprocess_mode = str(config.get("reprocess_mode", "increment") or "increment").lower()
                expected_output = self._build_oldcam_output_path(source_video, primary_version)
                run_input = source_video
                existing_outputs = [
                    self._build_oldcam_output_path(source_video, run_version)
                    for run_version in versions_to_run
                    if self._build_oldcam_output_path(source_video, run_version).exists()
                ]

                if existing_outputs:
                    if not allow_reprocess:
                        existing_names = ", ".join(path.name for path in existing_outputs)
                        message = (
                            f"Oldcam output already exists ({existing_names}). "
                            "Enable 'Allow reprocessing' to rerun."
                        )
                        self.log(message, "warning")
                        if completion_callback:
                            completion_callback(False, str(source_video), None, message)
                        return

                    if reprocess_mode == "overwrite":
                        for existing_output in existing_outputs:
                            try:
                                existing_output.unlink()
                                self.log(
                                    f"Deleted existing Oldcam output: {existing_output.name}",
                                    "warning",
                                )
                            except Exception as exc:
                                message = f"Could not delete existing Oldcam output: {exc}"
                                self.log(message, "error")
                                if completion_callback:
                                    completion_callback(False, str(source_video), None, message)
                                return
                    else:
                        temp_input = self._get_next_incremented_oldcam_input(source_video, versions_to_run)
                        run_input = temp_input
                        expected_output = self._build_oldcam_output_path(run_input, primary_version)
                        # Structured event for the file log; the final
                        # "Oldcam vN Finish applied: <name>" panel line a few
                        # seconds later shows the user the actual produced
                        # file, so this preview is verbose noise in the panel.
                        self.log(f"Oldcam rerun increment target: {expected_output.name}", "debug")

                self.log(
                    "Oldcam-only rerun: source="
                    + f"{source_video.name}, versions={','.join(selected_versions)}",
                    "info",
                )
                output_path = self._oldcam_video(str(run_input), QueueItem(str(source_video)))
                # OPTIONAL per-Oldcam rPPG fan-out (Phase E of
                # polish/v2.3, 2026-05-22). The main rPPG pass already
                # ran on ``source_video`` at the top of this worker
                # (the new Kling -> rPPG -> Loop -> Oldcam order), so
                # we only run the slower per-Oldcam-output rPPG
                # injection when the user has explicitly opted in via
                # the ``rppg_per_oldcam_fanout`` config flag. Default
                # OFF — most workflows don't need it because Oldcam's
                # resolution-crush attenuates the pulse and the
                # already-rPPG'd base is the cleaner deliverable.
                summary = self._last_oldcam_run_summary or {}
                if (
                    self._rppg_enabled()
                    and config.get("rppg_per_oldcam_fanout", False)
                ):
                    rerun_oldcam_outputs = list(summary.get("outputs") or [])
                    last_rppg: Optional[str] = None
                    for src in rerun_oldcam_outputs:
                        rppg_path = self._rppg_video(src, QueueItem(str(source_video)))
                        if rppg_path:
                            last_rppg = rppg_path
                    # Only adopt the rPPG result when oldcam itself
                    # produced output (output_path truthy). If oldcam
                    # produced NOTHING, leaving output_path falsy lets
                    # the downstream "if output_path and exists()"
                    # correctly report the oldcam rerun as FAILED — a
                    # base-only rPPG must not mask total oldcam failure
                    # as success (CodeRabbit Major, PR #40).
                    if last_rppg and output_path:
                        preferred = self._build_rppg_output_path(Path(output_path))
                        output_path = (
                            str(preferred) if preferred.exists() else last_rppg
                        )
                requested_versions = summary.get("requested_versions", [])
                succeeded_versions = summary.get("succeeded_versions", [])
                failed_versions = summary.get("failed_versions", [])
                primary_output = summary.get("primary_output", "")
                if requested_versions:
                    # Same payload already emitted by _oldcam_video as
                    # "Oldcam summary:" — demote to debug so it stays in the
                    # file log without duplicating the panel line.
                    self.log(
                        "Oldcam-only rerun summary: requested versions="
                        + ",".join(str(v) for v in requested_versions)
                        + "; succeeded versions="
                        + ",".join(str(v) for v in succeeded_versions)
                        + "; failed/skipped versions="
                        + ",".join(f"{v} ({r})" for v, r in failed_versions)
                        + "; primary output="
                        + str(primary_output),
                        "debug",
                    )
                if output_path and Path(output_path).exists():
                    # The user-facing "rerun complete: <src> → <output>" line is
                    # emitted by main_window's completion callback (it has the
                    # source name + arrow + full output path). Demote this
                    # basename-only twin to debug to avoid duplicating in the
                    # panel.
                    self.log(f"Oldcam-only rerun complete: {Path(output_path).name}", "debug")
                    if completion_callback:
                        completion_callback(True, str(source_video), str(output_path), None)
                    return

                message = f"Oldcam-only rerun failed for {source_video.name}"
                self.log(message, "warning")
                if completion_callback:
                    completion_callback(False, str(source_video), None, message)
            except Exception as exc:
                message = f"Oldcam-only rerun error: {exc}"
                self.log(message, "error")
                if completion_callback:
                    completion_callback(False, str(source_video), None, message)
            finally:
                if temp_input is not None:
                    try:
                        temp_input.unlink(missing_ok=True)
                    except Exception:
                        pass

        self._oldcam_rerun_thread = threading.Thread(
            target=_worker,
            daemon=True,
            name="oldcam-rerun-worker",
        )
        self._oldcam_rerun_thread.start()
        return True

    def _get_next_pending(self) -> Optional[QueueItem]:
        """Get next pending item and mark it as processing."""
        with self.lock:
            for item in self.items:
                if item.status == "pending":
                    item.status = "processing"
                    item.stage = "kling"
                    item.stage_percent = 0
                    return item
        return None

    def _process_queue(self):
        """Worker thread that processes queue items."""
        while not self._stop_flag:
            # Check if paused
            if self.is_paused:
                self.is_running = False
                return

            # Get next item
            item = self._get_next_pending()
            if item is None:
                # No more items to process
                self.is_running = False
                self.log("🏁 Queue processing complete", "success")
                return

            # Fresh abort slate for this item — a prior item's abort (or a
            # leftover set from a cancelled run) must not cancel this one.
            self._abort_event.clear()
            self._active_subprocess = None

            # Re-check pause AFTER clearing the abort slate (code-review
            # CRITICAL #1): abort_current_job() sets BOTH _abort_event and
            # is_paused from the GUI thread. If the user clicked Abort in the
            # window between the top-of-loop is_paused check and the clear()
            # above, the clear() would have wiped that abort — and the queue
            # would roll straight into THIS item instead of stopping. Honour
            # the pause here so an abort landing in that window stops the queue
            # (the item stays 'processing'→re-queued on resume, not run+killed).
            if self.is_paused:
                # Put the item back to pending so resume re-runs it cleanly.
                with self.lock:
                    if item.status == "processing":
                        item.status = "pending"
                        item.stage = "queued"
                        item.stage_percent = 0
                self.is_running = False
                self.update_queue_display()
                return

            self.update_queue_display()
            self.log(f"🎬 Processing: {item.filename}", "info")

            # v2.17: the QueueManager is now created even without a fal.ai key
            # (so local rPPG/Oldcam/Loop re-runs work). But Kling GENERATION
            # needs the generator — if a key-less user drops a file to GENERATE,
            # self.generator is None and the unconditional update_prompt_slot()
            # below would AttributeError inside this worker thread, failing the
            # item with a confusing error. Guard here with a clear, actionable
            # message instead (code-review C1, 2026-06-03).
            if self.generator is None:
                item.status = "failed"
                item.error_message = "No fal.ai API key — add one to generate videos"
                self.log(
                    "Video generation needs a fal.ai API key. Add it in the app, "
                    "then re-add this file. (rPPG / Oldcam / Loop re-runs work "
                    "without a key.)",
                    "error",
                )
                self.update_queue_display()
                if self.on_processing_complete:
                    self.on_processing_complete(item)
                continue

            try:
                # Capture timestamp at start of processing (for consistent filenames)
                generation_timestamp = datetime.now()

                # Get current config
                config = self.get_config()
                use_source_folder = config.get("use_source_folder", True)
                output_folder = config.get("output_folder", "")
                prompt = self._get_current_prompt(config)
                negative_prompt = self._get_current_negative_prompt(config)
                allow_reprocess = config.get("allow_reprocess", False)
                reprocess_mode = config.get("reprocess_mode", "increment")
                video_duration = config.get("video_duration", 10)
                model_endpoint = config.get("current_model", "")
                prompt_slot = config.get("current_prompt_slot", 1)

                # Advanced video settings
                aspect_ratio = config.get("aspect_ratio", "9:16")
                resolution = config.get("resolution", "720p")
                seed = config.get("seed", -1)
                camera_fixed = config.get("camera_fixed", False)
                generate_audio = config.get("generate_audio", False)

                # Update generator's prompt slot for filename generation
                self.generator.update_prompt_slot(prompt_slot)

                # Validate duration before making API request
                try:
                    validate_duration(model_endpoint, video_duration)
                except ValueError as e:
                    item.status = "failed"
                    item.error_message = str(e)
                    self.log(f"Invalid duration: {e}", "error")
                    self.update_queue_display()
                    if self.on_processing_complete:
                        self.on_processing_complete(item)
                    continue

                # Verbose: Show configuration being used
                self.log_verbose(f"  Model: {self.generator.model_display_name}", "api")
                self.log_verbose(
                    f"  Duration: {config.get('video_duration', 10)}s", "debug"
                )
                if prompt:
                    prompt_preview = prompt[:60] + "..." if len(prompt) > 60 else prompt
                    self.log_verbose(f"  Prompt: {prompt_preview}", "debug")

                # Determine output folder
                if use_source_folder:
                    actual_output = get_gen_images_folder(item.path)
                    os.makedirs(actual_output, exist_ok=True)
                    self.log_verbose("  Output: gen-images/", "debug")
                elif not output_folder or not os.path.isdir(output_folder):
                    # Custom folder selected but not set or invalid - use gen-images/
                    actual_output = get_gen_images_folder(item.path)
                    os.makedirs(actual_output, exist_ok=True)
                    self.log(
                        "No valid output folder set - saving to gen-images/",
                        "warning",
                    )
                else:
                    actual_output = output_folder
                    self.log_verbose(f"  Output: {actual_output}", "debug")

                # Check if video already exists (with current model and prompt slot)
                video_exists, found_video_path = check_video_exists(
                    item.path, actual_output, self.generator, config
                )
                custom_output_path = None

                if video_exists:
                    if not allow_reprocess:
                        # Reprocessing disabled - fail with clear message
                        # Use the actual found filename instead of generating a new one
                        existing_path = Path(found_video_path) if found_video_path else get_output_video_path(
                            item.path, actual_output, self.generator, config, generation_timestamp
                        )
                        item.status = "failed"
                        item.error_message = f"Video already exists: {existing_path.name}. Enable 'Allow reprocessing' to regenerate."
                        self.log(
                            f"Skipped: {item.filename} - video already exists",
                            "warning",
                        )
                        self.log(f"  Existing: {existing_path}", "info")
                        self.log(f"  Enable 'Allow reprocessing' to regenerate", "info")
                        self.update_queue_display()
                        if self.on_processing_complete:
                            self.on_processing_complete(item)
                        continue

                    elif reprocess_mode == "overwrite":
                        # Overwrite mode - delete existing file and looped variant
                        existing_path = (
                            Path(found_video_path)
                            if found_video_path
                            else get_output_video_path(
                                item.path, actual_output, self.generator, config, generation_timestamp
                            )
                        )
                        looped_path = existing_path.with_name(
                            f"{existing_path.stem}_looped{existing_path.suffix}"
                        )
                        try:
                            existing_path.unlink()
                            if looped_path.exists():
                                looped_path.unlink()
                            self.log(
                                f"Deleted existing: {existing_path.name} (+ looped variant)",
                                "warning",
                            )
                        except Exception as e:
                            self.log(f"Could not delete existing file: {e}", "error")
                            item.status = "failed"
                            item.error_message = f"Could not delete existing file: {e}"
                            self.update_queue_display()
                            if self.on_processing_complete:
                                self.on_processing_complete(item)
                            continue

                        # Keep overwrite target stable even if generator picks a new indexed filename.
                        custom_output_path = str(existing_path)

                    elif reprocess_mode == "increment":
                        # Increment mode - find next available filename
                        try:
                            next_path = get_next_available_path(
                                item.path, actual_output, self.generator, config, generation_timestamp
                            )
                            custom_output_path = str(next_path)
                            self.log(
                                f"Will save as: {next_path.name} (incremented)", "info"
                            )
                        except ValueError as e:
                            item.status = "failed"
                            item.error_message = str(e)
                            self.log(f"Error: {e}", "error")
                            self.update_queue_display()
                            if self.on_processing_complete:
                                self.on_processing_complete(item)
                            continue

                # Process the image
                # Skip duplicate check if we've already handled it (overwrite mode deleted file,
                # increment mode uses custom path)
                skip_check = video_exists and reprocess_mode == "overwrite"
                result = self._generate_video(
                    item,
                    actual_output,
                    prompt,
                    negative_prompt,
                    False,  # always False — we already computed gen-images/ path
                    custom_output_path,
                    skip_duplicate_check=skip_check,
                    video_duration=video_duration,
                    aspect_ratio=aspect_ratio,
                    resolution=resolution,
                    seed=seed,
                    camera_fixed=camera_fixed,
                    generate_audio=generate_audio,
                    generation_timestamp=generation_timestamp,
                )

                if result:
                    item.status = "completed"
                    self.log(f"✓ Completed: {item.filename}", "success")

                    # NEW pipeline order (Phase E of polish/v2.3,
                    # 2026-05-22): Kling -> rPPG -> Loop -> Oldcam. The
                    # rPPG'd base feeds Loop, the looped-or-not rPPG'd
                    # base feeds every Oldcam version. Each Oldcam
                    # output sits next to its plain pre-Oldcam parent.
                    # This replaces the prior "rPPG strictly LAST"
                    # order where rPPG fan-out happened on each Oldcam
                    # output. The slower legacy mode is preserved
                    # behind the rppg_per_oldcam_fanout opt-in flag
                    # below (defaults OFF).
                    final_video = result

                    # Step 1: rPPG on raw Kling FIRST when enabled.
                    # Produces ``<stem>-rppg.mp4`` if it succeeds. On
                    # graceful skip (tool missing or injection fail),
                    # rename the raw Kling output to insert a
                    # ``-NORPPG`` marker so the final delivered video
                    # unambiguously reflects that rPPG was REQUESTED
                    # but did NOT land. Downstream Loop+Oldcam still
                    # run on the marked file, producing chains like
                    # ``..._k25tPro_p3_1-NORPPG-oldcam-v13.mp4``.
                    item.rppg_requested = self._rppg_enabled()
                    item.rppg_succeeded = False
                    if item.rppg_requested:
                        item.stage = "rppg"
                        item.stage_percent = 0
                        self.update_queue_display()
                        rppg_base = self._rppg_video(final_video, item)
                        if rppg_base:
                            final_video = rppg_base
                            item.rppg_succeeded = True
                        else:
                            final_video = self._mark_norppg(final_video)

                    # Abort guard (code-review Codex P2): _rppg_video returns
                    # None on a user Abort exactly like an ordinary skip, so
                    # WITHOUT this check the worker would march on into Loop /
                    # Oldcam / final "done" and still produce + mark a completed
                    # output AFTER the user pressed Abort. Bail the whole item
                    # the instant an abort is observed (the queue is already
                    # paused; the item is re-queued so resume re-runs it clean).
                    if self._abort_requested():
                        self._handle_item_abort(item)
                        continue

                    # Step 2: Loop on the rPPG'd (or raw if rPPG was
                    # OFF/skipped) base. After this step, ``final_video``
                    # is the single source every Oldcam version + the
                    # headline output derive from.
                    if config.get("loop_videos", False):
                        item.stage = "loop"
                        item.stage_percent = 0
                        self.update_queue_display()
                        looped_video = self._loop_video(final_video, item)
                        if looped_video:
                            final_video = looped_video

                    if self._abort_requested():
                        self._handle_item_abort(item)
                        continue

                    # Step 3: Oldcam runs EVERY selected version.
                    # _oldcam_video returns the highest version's path;
                    # _last_oldcam_run_summary["outputs"] holds ALL
                    # per-version outputs. The plain pre-Oldcam files
                    # (raw Kling, rPPG'd, looped) remain on disk
                    # alongside — non-destructive.
                    oldcam_outputs: List[str] = []
                    if self._get_oldcam_versions_to_run():
                        item.stage = "oldcam"
                        item.stage_percent = 0
                        self.update_queue_display()
                        oldcam_video = self._oldcam_video(final_video, item)
                        if oldcam_video:
                            final_video = oldcam_video
                        summary = self._last_oldcam_run_summary or {}
                        oldcam_outputs = list(summary.get("outputs") or [])

                    if self._abort_requested():
                        self._handle_item_abort(item)
                        continue

                    # Step 4 (OPTIONAL): legacy per-Oldcam rPPG fan-out.
                    # When ``rppg_per_oldcam_fanout`` is True AND rPPG
                    # itself is enabled, also inject into every Oldcam
                    # output, producing ``<base>-rppg-oldcam-vN-rppg.mp4``.
                    # User-direction 2026-05-22: this is slower but lets
                    # a careful workflow get a fresh-pulse Oldcam variant.
                    # Default OFF — most workflows do NOT need it because
                    # Oldcam's resolution-crush attenuates the pulse and
                    # the rPPG'd base is the cleaner deliverable.
                    fanout_failed = 0
                    fanout_total = 0
                    if (
                        self._rppg_enabled()
                        and config.get("rppg_per_oldcam_fanout", False)
                        and oldcam_outputs
                    ):
                        last_rppg: Optional[str] = None
                        for src in oldcam_outputs:
                            # Abort guard (code-review MEDIUM): without this an
                            # abort fires N spurious launch-and-kill cycles (one
                            # per remaining oldcam output) + a misleading
                            # FANOUT-FAILED N/M log. Break on the first observed
                            # abort; the outer post-stage guard handles the item.
                            if self._abort_requested():
                                break
                            fanout_total += 1
                            rppg_video = self._rppg_video(src, item)
                            if rppg_video:
                                last_rppg = rppg_video
                            else:
                                fanout_failed += 1
                        # If any (or all) fan-out injections failed,
                        # surface it loudly so the user doesn't think
                        # every requested fresh-pulse oldcam variant
                        # actually got injected. The summary line built
                        # below in the "ALL POSTPROCESS DONE" milestone
                        # adds an explicit "FANOUT-FAILED N/M" segment
                        # when fanout_failed > 0.
                        if fanout_failed > 0:
                            self.log(
                                f"❌ RPPG per-oldcam fan-out: "
                                f"{fanout_failed}/{fanout_total} variants "
                                f"failed to inject. Deliverable still has "
                                f"the BASE rPPG bake-in (Phase E) — "
                                f"requested fresh-pulse oldcam variants "
                                f"were NOT all produced.",
                                "error_bold",
                            )
                        if last_rppg:
                            # Surface the highest Oldcam version's rPPG
                            # as headline when present, else fall back
                            # to the last successful injection.
                            preferred = self._build_rppg_output_path(
                                Path(final_video)
                            )
                            final_video = (
                                str(preferred) if preferred.exists()
                                else last_rppg
                            )

                    # Final abort guard before marking the item done — an abort
                    # during the fan-out loop must not fall through to "done".
                    if self._abort_requested():
                        self._handle_item_abort(item)
                        continue

                    item.output_path = final_video
                    item.stage = "done"
                    item.stage_percent = 100
                    self.log(f"💾 Saved to: {final_video}", "info")
                    # Synthesize a final summary milestone so the user
                    # can see, at a glance, what was applied (and what
                    # was requested but failed). Bot/code-reviewer
                    # context: order here mirrors the Phase E pipeline
                    # in this method (rPPG → Loop → Oldcam) so what
                    # the user reads matches what actually ran.
                    summary_parts: List[str] = []
                    if item.rppg_requested:
                        summary_parts.append(
                            "RPPG" if item.rppg_succeeded else "RPPG-FAILED"
                        )
                    if config.get("loop_videos", False):
                        summary_parts.append("LOOP")
                    oldcam_versions = self._get_oldcam_versions_to_run()
                    if oldcam_versions:
                        summary_parts.append(
                            f"OLDCAM-{','.join(str(v) for v in oldcam_versions)}"
                        )
                    # Surface per-oldcam fan-out failures so the headline
                    # summary line is never misleadingly green when the
                    # opt-in fan-out only partially landed (subagent H2
                    # on PR #52 round 1).
                    if fanout_total > 0 and fanout_failed > 0:
                        summary_parts.append(
                            f"FANOUT-FAILED-{fanout_failed}/{fanout_total}"
                        )
                    summary = " + ".join(summary_parts) if summary_parts else "kling only"
                    self.log(
                        f"✅ ALL POSTPROCESS DONE ({summary}) → "
                        f"{Path(final_video).name}",
                        "milestone",
                    )
                else:
                    item.status = "failed"
                    item.stage = "failed"
                    item.stage_percent = 0
                    item.error_message = self._get_generation_error_message()
                    self.log(f"Failed {item.filename}: {item.error_message}", "error")

            except Exception as e:
                item.status = "failed"
                item.error_message = format_exception_detail(e)
                self.log(
                    f"Error processing {item.filename}: {item.error_message}",
                    "error",
                )
                # Full stack to the file log / verbose mode so the panel stays
                # readable but the root cause is recoverable.
                self.log(format_exception_traceback(e), "debug")

            self.update_queue_display()

            if self.on_processing_complete:
                self.on_processing_complete(item)

        self.is_running = False

    def _generate_video(
        self,
        item: QueueItem,
        output_folder: str,
        prompt: str,
        negative_prompt: str,
        use_source_folder: bool,
        custom_output_path: Optional[str] = None,
        skip_duplicate_check: bool = False,
        video_duration: int = 10,
        aspect_ratio: str = "9:16",
        resolution: str = "720p",
        seed: int = -1,
        camera_fixed: bool = False,
        generate_audio: bool = False,
        generation_timestamp: Optional[datetime] = None,
    ) -> Optional[str]:
        """
        Generate video using the generator.

        Args:
            item: Queue item being processed
            output_folder: Output folder path
            prompt: Generation prompt
            negative_prompt: Negative prompt (optional, model-dependent)
            use_source_folder: Whether to save to source folder
            custom_output_path: Optional custom output path (for increment mode)
            skip_duplicate_check: Whether to skip duplicate detection
            video_duration: Video duration in seconds (default: 10)
            aspect_ratio: Video aspect ratio (default: 9:16)
            resolution: Video resolution (default: 720p)
            seed: Random seed, -1 for random (default: -1)
            camera_fixed: Lock camera movement (default: False)
            generate_audio: Generate audio track (default: False)
            generation_timestamp: Generation start time for consistent filenames (default: None)

        Returns:
            Output path on success, None on failure
        """
        # Respect model capability via the SINGLE source of truth
        # (model_metadata.get_model_capabilities — the dispatcher reads
        # the exact same flags, so the GUI pre-drop and the payload can
        # never diverge). The dispatcher gates again defensively; dropping
        # here too keeps logs/console honest about what was sent.
        from model_metadata import (
            get_model_capabilities,
            get_model_by_endpoint,
        )
        from face_similarity import _parse_bool

        _caps = get_model_capabilities(self.generator.model_endpoint)
        # Mirror the dispatcher (kling_generator_falai.py): an
        # endpoint NOT in MODEL_METADATA is a custom model whose
        # true caps come from the live fal.ai schema, NOT the
        # conservative get_model_capabilities default. Without this
        # the GUI queue pre-strips neg/cfg to None before the
        # dispatcher's own _is_known_model bypass can keep them, so
        # custom-model users silently lose negative_prompt +
        # cfg_scale (code-reviewer, PR #41). Known models keep
        # precise per-model gating (o3 / seedance still drop both).
        _is_known = (
            get_model_by_endpoint(self.generator.model_endpoint)
            is not None
        )
        neg_for_payload = (
            negative_prompt
            if (_caps["supports_negative_prompt"] or not _is_known)
            else None
        )
        _cfg_cfg = self.get_config()
        if _caps["supports_cfg_scale"] or not _is_known:
            try:
                _cfg_scale = float(_cfg_cfg.get("cfg_scale_value", 0.7))
            except (TypeError, ValueError):
                _cfg_scale = 0.7
            # Clamp to the fal.ai-valid range. The automation
            # pipeline already does max(0.0, min(1.0, ...)); a
            # stale / hand-edited out-of-range persisted value must
            # not let the GUI submit an invalid cfg_scale while the
            # CLI silently clamps (Codex P2, PR #41 — GUI/CLI drift).
            _cfg_scale = max(0.0, min(1.0, _cfg_scale))
        else:
            _cfg_scale = None
        # Mirror automation/pipeline.py: an UNPARSEABLE lock_end_frame
        # (_parse_bool -> None) defaults to True, NOT bool(None)=False.
        # lock default is True, so GUI and CLI must agree on malformed
        # input (gemini-code-assist HIGH, PR #41). (Contrast
        # rppg_metrics_in_filename, default False, where bool(None)=False
        # is the correct default — do not "unify" that one.)
        _raw_lock_ef = _parse_bool(_cfg_cfg.get("lock_end_frame", True))
        _lock_end_frame = True if _raw_lock_ef is None else bool(_raw_lock_ef)

        # Set up verbose callback for generator progress
        def progress_callback(message: str, level: str = "info"):
            self.log_verbose(message, level)

        # Attach callback to generator if verbose mode
        config = self.get_config()
        if config.get("verbose_gui_mode", False):
            self.generator.set_progress_callback(progress_callback)
        else:
            self.generator.set_progress_callback(None)

        # For increment mode, we need to handle the output path ourselves
        if custom_output_path:
            # Generate with skip_duplicate_check since we've already handled it
            result = self.generator.create_kling_generation(
                character_image_path=item.path,
                output_folder=output_folder,
                custom_prompt=prompt,
                negative_prompt=neg_for_payload,
                use_source_folder=use_source_folder,
                skip_duplicate_check=True,  # We've already handled duplicate check
                duration=video_duration,
                aspect_ratio=aspect_ratio,
                resolution=resolution,
                seed=seed,
                camera_fixed=camera_fixed,
                generate_audio=generate_audio,
                cfg_scale=_cfg_scale,
                lock_end_frame=_lock_end_frame,
                config=config,
                timestamp=generation_timestamp,
            )

            if result and custom_output_path != result:
                # Rename to custom path (incremented filename)
                try:
                    import shutil

                    shutil.move(result, custom_output_path)
                    return custom_output_path
                except Exception as e:
                    self.log(f"Could not rename output: {e}", "warning")
                    return result  # Return original path if rename fails

            return result
        else:
            # Normal generation
            return self.generator.create_kling_generation(
                character_image_path=item.path,
                output_folder=output_folder,
                custom_prompt=prompt,
                negative_prompt=neg_for_payload,
                use_source_folder=use_source_folder,
                skip_duplicate_check=skip_duplicate_check,
                duration=video_duration,
                aspect_ratio=aspect_ratio,
                resolution=resolution,
                seed=seed,
                camera_fixed=camera_fixed,
                generate_audio=generate_audio,
                cfg_scale=_cfg_scale,
                lock_end_frame=_lock_end_frame,
                config=config,
                timestamp=generation_timestamp,
            )

    def _get_generation_error_message(self) -> str:
        """Get most specific generator failure message available."""
        raw = getattr(self.generator, "last_error_message", "")
        message = str(raw or "").strip()
        return message if message else "Generation failed"

    def _loop_video(self, video_path: str, item: QueueItem):
        """
        Create a looped version of the generated video.

        Args:
            video_path: Path to the generated video
            item: Queue item being processed
        """
        try:
            from .video_looper import create_looped_video

            self.log(f"Creating looped video...", "info")

            # Create looped version (adds _looped suffix)
            looped_path = create_looped_video(
                input_path=video_path,
                suffix="_looped",
                overwrite=True,
                log_callback=self.log,
            )

            if looped_path:
                # Note: video_looper.create_looped_video already emits the
                # user-facing "Looped video saved: <name> (X.Y MB)" success
                # line via log_callback. Mirror it to the file log only so we
                # have the structured event without duplicating the panel.
                self.log(
                    f"Looped video saved: {os.path.basename(looped_path)}", "debug"
                )
                return looped_path
            else:
                self.log("Failed to create looped video", "warning")
                return None

        except ImportError:
            self.log("Video looper module not available", "warning")
            return None
        except Exception as e:
            self.log(f"Error creating looped video: {e}", "warning")
            return None

    def _get_oldcam_version(self) -> str:
        """Return legacy single Oldcam version view (highest selected)."""
        selected = self._get_selected_oldcam_versions()
        return selected[-1] if selected else "v9"

    def _build_oldcam_output_path(self, input_path: Path, version: str) -> Path:
        """Build versioned Oldcam output path next to input video."""
        suffix = f"-oldcam-{str(version).lower()}"
        return input_path.with_name(f"{input_path.stem}{suffix}{input_path.suffix}")

    def _resolve_oldcam_dir(self, version: str = "v7") -> Path:
        """Resolve selected oldcam directory for script and frozen builds."""
        folder_name = f"oldcam-{str(version).lower()}"
        app_dir = Path(get_app_dir())
        resource_dir = Path(get_resource_dir())
        candidates = [
            app_dir / folder_name,
            resource_dir / folder_name,
            Path(__file__).parent.parent.resolve() / folder_name,
        ]
        for candidate in candidates:
            if candidate.exists():
                return candidate
        return app_dir / folder_name

    # Matches "[Oldcam] Processing: 50% complete..." style progress lines
    # emitted by every oldcam launcher we ship. Keeps the regex permissive
    # enough that a future launcher variant (e.g. omitting the trailing
    # "complete...") still updates the queue widget bar.
    _OLDCAM_PCT_PAT = re.compile(r"\[Oldcam\]\s+Processing:\s+(\d+)%")
    # Matches the rPPG progress-tracker's synthesized
    #   "rPPG iter 3/10 frame 144/242 (~50%)"
    # progress lines. Class-level for parity with _OLDCAM_PCT_PAT and to
    # survive __new__-constructed test instances that bypass __init__.
    # (Gemini medium on PR #52 round 2 — was previously compiled
    # inside _rppg_video; moving to class level removes one re-compile
    # per queue item and keeps regex catalogue centralized.)
    _RPPG_PCT_PAT = re.compile(
        r"rPPG iter\s+(\d+)/(\d+)\s+frame\s+\d+/\d+\s+\(~(\d+)%\)"
    )

    def _run_oldcam_version(
        self,
        video_path: str,
        version: str,
        item: Optional[QueueItem] = None,
    ) -> Optional[str]:
        """Run one oldcam version and return output path if successful.

        ``item`` (optional) is used to update the per-queue-item progress
        bar as the oldcam launcher reports its 25/50/75/100% milestones.
        Kept optional so any future call site that doesn't have a
        ``QueueItem`` in scope doesn't have to fabricate one.
        """
        oldcam_dir = self._resolve_oldcam_dir(version)
        launcher_path = oldcam_dir / "launcher.py"
        if not launcher_path.exists():
            self.log(f"Oldcam {version} launcher not found", "warning")
            return None

        if not self._ensure_oldcam_dependencies(oldcam_dir, version):
            self.log(f"Skipping Oldcam {version} Finish due to missing dependencies", "warning")
            return None

        self.log(f"📷 Applying Oldcam {version} Finish...", "info")
        run_cmd = [sys.executable, "-u", str(launcher_path), video_path]
        output_lines: list[str] = []
        returncode = -1
        process: Optional[subprocess.Popen] = None
        _TIMEOUT = 600
        try:
            process = subprocess.Popen(
                run_cmd,
                cwd=str(oldcam_dir),
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                errors="replace",  # oldcam launcher stdout may carry non-UTF-8 bytes
                bufsize=1,
            )
            assert process.stdout is not None
            # Publish for the GUI Abort button (kills it instantly).
            self._publish_active_subprocess(process)
            deadline = time.monotonic() + _TIMEOUT
            while True:
                # Abort check (GUI Abort button). Oldcam is a blocking
                # readline loop, so a kill from abort_current_job() unblocks
                # readline with EOF; we also raise TimeoutExpired here so a
                # check between lines fires promptly.
                if self._abort_requested():
                    raise subprocess.TimeoutExpired(run_cmd, _TIMEOUT)
                line = process.stdout.readline()
                if not line:
                    break
                if time.monotonic() > deadline:
                    raise subprocess.TimeoutExpired(run_cmd, _TIMEOUT)
                line_text = line.rstrip()
                if line_text:
                    output_lines.append(line_text)
                    if not _is_tf_noise(line_text):
                        # Panel-noisy lines (already summarized elsewhere or
                        # pure path dumps) go to the file log only; everything
                        # else continues to the user-facing panel.
                        level = "debug" if _is_panel_noise(line_text) else "info"
                        self.log(line_text, level)
                    # Update per-item progress bar from the launcher's
                    # native progress lines. Capped at 99 here; the
                    # caller flips to 100/done when the success branch
                    # runs. (Avoids a brief flicker of "[████] oldcam 100%"
                    # before the queue row swaps to the ✅ icon.) Skip
                    # the redraw if the value didn't change — subagent
                    # M3 on PR #52 round 1 (avoids ~40 redundant
                    # listbox repaints per rPPG run).
                    if item is not None:
                        m = self._OLDCAM_PCT_PAT.search(line_text)
                        if m is not None:
                            try:
                                pct = max(0, min(99, int(m.group(1))))
                            except (TypeError, ValueError):
                                pct = 0
                            if pct != item.stage_percent:
                                item.stage_percent = pct
                                self.update_queue_display()
            returncode = process.wait(timeout=max(0, deadline - time.monotonic()))
        except subprocess.TimeoutExpired:
            if process is not None:
                if process.poll() is None:
                    process.kill()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    pass
                if process.stdout is not None:
                    try:
                        process.stdout.close()
                    except Exception:
                        pass
            if self._abort_requested():
                self.log(f"⛔ Oldcam {version} aborted by user.", "warning")
            else:
                self.log(f"Oldcam {version} timed out after 600s", "warning")
            return None
        except Exception as exc:
            if process is not None:
                if process.poll() is None:
                    process.kill()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    pass
                if process.stdout is not None:
                    try:
                        process.stdout.close()
                    except Exception:
                        pass
            self.log(f"Oldcam {version} launcher error: {exc}", "warning")
            return None
        finally:
            # Clear the published handle so a later Abort can't target this
            # (now-finished) oldcam process.
            self._publish_active_subprocess(None)

        if returncode == 0:
            input_path = Path(video_path)
            oldcam_output = self._build_oldcam_output_path(input_path, version)
            if oldcam_output.exists():
                self.log(f"Oldcam {version} output: {oldcam_output.name}", "success")
                self.log(f"✅ OLDCAM {version} DONE", "milestone")
                return str(oldcam_output)
            self.log(f"Oldcam {version} process completed but output file was not found", "warning")
            return None

        self.log(f"Oldcam {version} Finish failed (code {returncode})", "warning")
        if output_lines:
            self.log(output_lines[-1], "warning")
        return None

    def _ensure_oldcam_dependencies(self, oldcam_dir: Path, version: str) -> bool:
        """Check Oldcam requirements in current interpreter and emit install guidance."""
        if self._oldcam_deps_status_by_version.get(version) is True:
            return True

        required_modules = ["cv2", "numpy"]
        requires_mediapipe = version in {"v9", "v10", "v11"}
        if requires_mediapipe:
            required_modules.append("mediapipe")

        try:
            for module_name in required_modules:
                __import__(module_name)
            if requires_mediapipe:
                mp_ok, diagnostics = self._validate_mediapipe_tasks_api(oldcam_dir)
                if not mp_ok:
                    self.log(
                        f"MediaPipe Tasks FaceLandmarker API unavailable; Oldcam {version} cannot run.",
                        "warning",
                    )
                    self.log(
                        "Validation command: "
                        + f'"{sys.executable}" -c "import mediapipe as mp; '
                        + 'from mediapipe.tasks.python import vision; '
                        + 'print(getattr(mp, \'__version__\', \'unknown\')); '
                        + 'print(hasattr(vision, \'FaceLandmarker\'))"',
                        "warning",
                    )
                    self.log(
                        "Diagnostics: "
                        + ", ".join(f"{k}={v}" for k, v in diagnostics.items()),
                        "warning",
                    )
                    return False
            self._oldcam_deps_status_by_version[version] = True
            return True
        except ImportError as e:
            requirements_path = oldcam_dir / "requirements.txt"
            self.log(f"Oldcam {version} dependencies missing: {e}", "warning")
            if requirements_path.exists():
                install_cmd = f'{sys.executable} -m pip install -r "{requirements_path}"'
                self.log(
                    f"Oldcam {version} dependencies are not installed. "
                    f"Please install them before processing Oldcam jobs: {install_cmd}",
                    "warning",
                )
                if version in {"v9", "v10", "v11"}:
                    mp_install = (
                        f"{sys.executable} -m pip install "
                        f"--force-reinstall --no-deps mediapipe==0.10.35"
                    )
                    self.log(
                        f"Oldcam {version} requires mediapipe + {self._task_model_filename()}. "
                        f"Step 1: {install_cmd}  "
                        f"Step 2: {mp_install}",
                        "warning",
                    )
            else:
                self.log(f"Oldcam requirements missing: {requirements_path}", "warning")
            return False

    def _task_model_filename(self) -> str:
        return "face_landmarker.task"

    def _resolve_face_landmarker_task_path(self, oldcam_dir: Path) -> Tuple[Optional[Path], List[Path]]:
        searched: List[Path] = []
        env_override = str(os.environ.get("OLDCAM_FACE_LANDMARKER_TASK", "")).strip()
        if env_override:
            env_path = Path(env_override).expanduser()
            searched.append(env_path)
            if env_path.exists():
                return env_path.resolve(), searched

        app_root = Path(__file__).resolve().parents[1]
        dist_root = app_root.parent
        candidates = [
            oldcam_dir / self._task_model_filename(),
            app_root / self._task_model_filename(),
            dist_root / self._task_model_filename(),
            Path.cwd() / self._task_model_filename(),
        ]
        for candidate in candidates:
            searched.append(candidate)
            if candidate.exists():
                return candidate.resolve(), searched
        return None, searched

    def _validate_mediapipe_tasks_api(self, oldcam_dir: Path) -> Tuple[bool, Dict[str, str]]:
        """Validate MediaPipe Tasks API + task model file used by oldcam v9/v10."""
        diagnostics: Dict[str, str] = {
            "python_executable": sys.executable,
            "sys_path_0": sys.path[0] if sys.path else "",
        }
        try:
            import mediapipe as mp  # noqa: F401
        except Exception as exc:
            diagnostics["import_error"] = f"{exc.__class__.__name__}: {exc}"
            return False, diagnostics

        mp_obj = locals().get("mp")
        diagnostics["mediapipe_file"] = str(getattr(mp_obj, "__file__", "unknown"))
        diagnostics["mediapipe_version"] = str(getattr(mp_obj, "__version__", "unknown"))
        try:
            from mediapipe.tasks.python import vision  # type: ignore
            facelandmarker_ok = hasattr(vision, "FaceLandmarker")
        except Exception as exc:
            diagnostics["facelandmarker_import_error"] = f"{exc.__class__.__name__}: {exc}"
            facelandmarker_ok = False
        diagnostics["facelandmarker_import_ok"] = str(facelandmarker_ok)
        if not facelandmarker_ok:
            return False, diagnostics
        task_path, searched = self._resolve_face_landmarker_task_path(oldcam_dir)
        diagnostics["task_file_path"] = str(task_path) if task_path else ""
        diagnostics["task_file_exists"] = str(task_path is not None)
        diagnostics["task_file_searched"] = ";".join(str(path) for path in searched)
        if task_path is None:
            return False, diagnostics

        # Shadowing hint: local repo paths usually indicate wrong module import.
        try:
            repo_root = str(Path(__file__).resolve().parents[1]).lower()
            mp_file = str(getattr(mp_obj, "__file__", "")).lower()
            diagnostics["shadowing_suspected"] = str(mp_file.startswith(repo_root))
        except Exception:
            diagnostics["shadowing_suspected"] = "unknown"
        return True, diagnostics

    def _oldcam_video(self, video_path: str, item: QueueItem) -> Optional[str]:
        """
        Process the video with selected Oldcam version.

        Args:
            video_path: Path to the generated or looped video
            item: Queue item being processed (used for per-stage
                progress reporting in the queue widget)
        """
        try:
                versions_to_run = self._get_oldcam_versions_to_run()
                # The per-version "Applying Oldcam vN Finish..." line that
                # follows shortly tells the user which version is running;
                # this overview is a structured event for the file log only.
                self.log("Oldcam selected: running " + ", ".join(versions_to_run), "debug")
                outputs: List[Tuple[str, str]] = []
                failures: List[Tuple[str, str]] = []
                for version in versions_to_run:
                    # Reset percent so the bar shows each version's own
                    # progression rather than starting where the previous
                    # version stopped.
                    item.stage_percent = 0
                    self.update_queue_display()
                    output_path = self._run_oldcam_version(video_path, version, item)
                    if output_path:
                        outputs.append((version, output_path))
                    else:
                        failures.append((version, f"Oldcam {version} failed or was skipped"))
                requested = list(versions_to_run)
                if not outputs:
                    self._last_oldcam_run_summary = {
                        "requested_versions": requested,
                        "succeeded_versions": [],
                        "failed_versions": failures,
                        "primary_output": "",
                    }
                    self.log(
                        "Oldcam summary: requested versions="
                        + ",".join(requested)
                        + "; succeeded versions=; failed/skipped versions="
                        + ",".join(f"{version} ({reason})" for version, reason in failures),
                        "warning",
                    )
                    return None
                # Highest version is returned as the single "result" for
                # callers that want one path, but ALL successful per-version
                # outputs are exposed so downstream rPPG can fan out across
                # every selected version (there is no privileged "primary"
                # — each selected version is its own deliverable).
                primary = max(outputs, key=lambda entry: self._oldcam_version_key(entry[0]))
                self._last_oldcam_run_summary = {
                    "requested_versions": requested,
                    "succeeded_versions": [version for version, _ in outputs],
                    "failed_versions": failures,
                    "primary_output": primary[1],
                    "outputs": [path for _version, path in outputs],
                }
                # Structured success summary for the file log. The user-facing
                # panel already saw "Oldcam vN Finish applied: <name>" for each
                # successful version, and main_window emits a final friendly
                # "Oldcam-only rerun complete: <src> -> <output>" line; no
                # need to duplicate the version list + full path here.
                self.log(
                    "Oldcam summary: requested versions="
                    + ",".join(requested)
                    + "; succeeded versions="
                    + ",".join(version for version, _ in outputs)
                    + "; failed/skipped versions="
                    + ",".join(f"{version} ({reason})" for version, reason in failures)
                    + "; primary output="
                    + primary[1],
                    "debug",
                )
                return primary[1]
        except Exception as e:
            self.log(f"Error applying Oldcam Finish: {e}", "warning")
            self._last_oldcam_run_summary = None
            return None

    # ------------------------------------------------------------------
    # rPPG injection (Phase E of polish/v2.3, 2026-05-22):
    #     pipeline order is now Kling -> rPPG -> Loop -> Oldcam.
    #
    # Earlier order was Kling -> Loop -> Oldcam -> rPPG. Inverted because
    # the user explicitly wants rPPG'd outputs as the primary deliverable
    # (loop + oldcam are optional flourishes). The opt-in
    # `rppg_per_oldcam_fanout` config flag re-applies rPPG to each Oldcam
    # output when the user wants the post-Oldcam pulse on top — default
    # OFF since Oldcam's resolution-crush attenuates the pulse and the
    # base rPPG'd frame is the cleaner deliverable.
    #
    # The injector lives in `rPPG/` (committed in-tree as of Phase D,
    # 2026-05-22) and is shelled out to via `run_rppg.bat` on Windows or
    # `run_rppg.sh` on macOS / Linux. Every failure path is a graceful
    # skip (warn + return None) so the caller keeps the pre-rPPG video
    # and the queue never crashes.
    # ------------------------------------------------------------------
    def _rppg_enabled(self) -> bool:
        return bool(self.get_config().get("rppg_enabled", False))

    def _build_rppg_output_path(self, input_path: Path) -> Path:
        """``video.mp4`` -> ``video-rppg.mp4`` (mirrors oldcam suffixing).

        Chained after oldcam this yields e.g.
        ``clip_looped-oldcam-v24-rppg.mp4``.
        """
        return input_path.with_name(f"{input_path.stem}-rppg{input_path.suffix}")

    def _resolve_rppg_launcher(self) -> Optional[Path]:
        """Resolve the rPPG launcher for script and frozen builds.

        Phase D + Phase F of polish/v2.3 (2026-05-22): rPPG/ is now
        committed in-tree (was gitignored before Phase D), and a
        ``run_rppg.sh`` was added alongside ``run_rppg.bat`` for
        macOS / Linux. We pick the right launcher per-OS:
          - Windows (os.name == 'nt'): run_rppg.bat
          - Everywhere else:           run_rppg.sh

        Returns None (caller skips gracefully) when the launcher or
        injector is missing — e.g. a partial clone, or a future
        packaging that ships only one OS's launcher.

        DELIBERATELY distinct from automation.rppg.resolve_rppg_launcher
        (NOT accidental duplication — do not "dedup" them): the GUI
        runs in frozen PyInstaller builds where the tool may be
        relocated, so this searches app_dir / resource_dir / repo-root
        (mirrors _resolve_oldcam_dir). The automation pipeline always
        runs from source, so its resolver takes an explicit repo_root
        and does a single check. Both require launcher AND injector
        to exist.
        """
        launcher_name = "run_rppg.bat" if os.name == "nt" else "run_rppg.sh"
        app_dir = Path(get_app_dir())
        resource_dir = Path(get_resource_dir())
        candidates = [
            app_dir / "rPPG",
            resource_dir / "rPPG",
            Path(__file__).parent.parent.resolve() / "rPPG",
        ]
        for base in candidates:
            launcher = base / launcher_name
            injector = base / "rppg_injector.py"
            if launcher.exists() and injector.exists():
                return launcher
        return None

    def _mark_norppg(self, video_path: str) -> str:
        """Rename ``video_path`` to insert a ``-NORPPG`` marker so the
        final delivered video unambiguously reflects that rPPG was
        REQUESTED but failed. Downstream stages (loop, oldcam) see
        the renamed file and chain their own suffixes onto it, e.g.::

            ..._k25tPro_p3_1.mp4
              -> NORPPG marker -> ..._k25tPro_p3_1-NORPPG.mp4
              -> oldcam        -> ..._k25tPro_p3_1-NORPPG-oldcam-v13.mp4

        Returns the new path on success, or the original path on rename
        failure (logged at warning — the run keeps going on the unmarked
        file rather than failing the whole queue item for a cosmetic).
        Idempotent: if the path already contains a ``-NORPPG`` token in
        the stem, returns it unchanged.
        """
        try:
            p = Path(video_path)
            # Idempotency check is a terminal-token match so a user
            # file that happens to contain ``-NORPPG`` mid-stem
            # (e.g. ``holiday-NORPPG-cut.mp4``) is still marked, not
            # skipped. Sourcery bug_risk on PR #52 round 1 — substring
            # matches would silently no-op on legitimately-needing-
            # marker files.
            if p.stem.endswith("-NORPPG"):
                return video_path
            marked = p.with_name(f"{p.stem}-NORPPG{p.suffix}")
            # Stale marked sibling from a previous run would block rename
            # on Windows. os.replace is atomic same-dir overwrite on both
            # POSIX and Windows; Path.rename is not.
            os.replace(str(p), str(marked))
            return str(marked)
        except OSError as exc:
            self.log(
                f"Could not mark NORPPG on {Path(video_path).name}: "
                f"{type(exc).__name__}: {exc}",
                "warning",
            )
            return video_path

    def _rppg_video(self, video_path: str, item: QueueItem) -> Optional[str]:
        """Run one-shot rPPG injection on ``video_path``.

        Returns the injected output path, or None on ANY failure
        (graceful skip — caller keeps the pre-rPPG video). ``item`` is
        used to update the per-queue-item progress bar as the rPPG
        injector emits synthesized "(~PCT%)" progress lines.
        """
        # Reset the once-per-run "checking dependencies" note flag so the
        # friendly note shows once for THIS run (not suppressed by a prior one).
        self._rppg_dep_note_shown = False
        try:
            input_path = Path(video_path)
            if not input_path.exists():
                self.log(f"rPPG skipped: input missing ({input_path.name})", "warning")
                return None

            # Never re-inject an already-injected file. The 📂 re-run
            # picker can be pointed at ANY video, including a prior
            # "*-rppg - <metrics>" artifact; injecting that again would
            # double-inject (-rppg-rppg) and compound the pulse out of the
            # non-negotiable sub-perceptual range. It IS the final
            # deliverable — return it as-is. Symmetric with the pipeline
            # guard (automation/pipeline.py Step 8); shared single source
            # of truth for the marker. (Codex P2 class, PR #39.)
            #
            # This MUST run BEFORE resolving the launcher: accepting an
            # already-injected file as the final deliverable requires no
            # external tool, so a release without the gitignored rPPG/
            # tool must still honor the no-reinject contract instead of
            # graceful-skipping past it. (Codex P2, PR #39.)
            from automation.rppg import is_rppg_artifact

            if is_rppg_artifact(input_path):
                self.log(
                    f"rPPG skipped: input is already injected ({input_path.name}); "
                    "keeping it as the final deliverable",
                    "info",
                )
                return str(input_path)

            launcher = self._resolve_rppg_launcher()
            if launcher is None:
                self.log("rPPG skipped: rPPG/ tool not present", "warning")
                return None

            output_path = self._build_rppg_output_path(input_path)
            self.log("[rPPG] Applying rPPG injection...", "info")
            # Iterative-mode flags. Defaults match rPPG/rppg.bat (the
            # friend's canonical launcher): --iterative is MANDATORY
            # for production because the initial single-shot rarely
            # lands at the optimal strength; --iterate-from-baseline
            # avoids cumulative encoding loss across iterations;
            # --skip-diagnosis dodges the Claude-API "clod diagnostics"
            # postscript. All three default ON, user-overridable via
            # the config keys.
            cfg = self.get_config()
            # Reuse the canonical str-to-bool coercion so a JSON value
            # of "false"/"no"/"0" parses as False instead of True. Raw
            # bool() on a non-empty string is True — exactly the bug
            # coderabbit flagged on PR #19 for the similarity strict
            # gate. Same fix is applied in automation.pipeline._read_bool
            # (single canonical helper, face_similarity._parse_bool).
            from face_similarity import _parse_bool

            # The GUI config uses BARE keys (rppg_enabled,
            # rppg_metrics_in_filename) while the automation pipeline
            # uses the automation_-prefixed namespace. For PR-43's new
            # iterative-mode flags the GUI config_panel doesn't yet
            # surface UI controls, so users who hand-edit the config
            # may set either the bare key (matches the GUI namespace
            # convention) OR the automation_ prefix (matches what's in
            # automation/config.py). Try the bare key first (it's the
            # canonical GUI name), then fall back to the automation_
            # name so the two namespaces stay in sync. PR #43 / code-
            # reviewer P1.
            def _cfg_get(bare_key: str, automation_key: str, default):
                val = cfg.get(bare_key, None)
                if val is None:
                    val = cfg.get(automation_key, default)
                return val

            def _cfg_bool(bare_key: str, automation_key: str, default: bool) -> bool:
                raw = _cfg_get(bare_key, automation_key, default)
                parsed = _parse_bool(raw)
                return parsed if parsed is not None else bool(default)

            rppg_mode_raw = _cfg_get("rppg_mode", "automation_rppg_mode", "iterative")
            rppg_mode = str(rppg_mode_raw or "iterative").strip().lower()
            iterative = rppg_mode == "iterative"
            iterate_from_baseline = _cfg_bool(
                "rppg_iterate_from_baseline",
                "automation_rppg_iterate_from_baseline",
                True,
            )
            skip_diagnosis = _cfg_bool(
                "rppg_skip_diagnosis",
                "automation_rppg_skip_diagnosis",
                True,
            )
            skip_kinematic_gate = _cfg_bool(
                "rppg_skip_kinematic_gate",
                "automation_rppg_skip_kinematic_gate",
                True,
            )
            # Landmark-detection stride: per the injector's own
            # ``--landmark-stride`` help, running MediaPipe only every
            # Nth frame and interpolating between via the ROIStabilizer
            # gives a "3-5x reduction in per-frame detection cost at
            # negligible quality loss on mostly-still faces."
            #
            # Default is 1 (every frame) for safety after PR #52's
            # snapshot race produced unplayable output on a real user
            # run. The snapshot race itself is now fixed
            # (rPPG/rppg_injector.py::_snapshot_validates) and a
            # playability gate (automation/rppg.py::_is_playable_video)
            # catches future regressions, but stride 1 stays the
            # default until we have local smoke-test proof that
            # stride > 1 is safe on real Kling output. Users can opt
            # into the 3-5x speedup via
            # config["rppg_landmark_stride"] = 3 (or the automation
            # alias automation_rppg_landmark_stride). CodeRabbit
            # round 3 on PR #53 — keep this comment in sync with the
            # actual default below.
            landmark_stride_raw = _cfg_get(
                "rppg_landmark_stride",
                "automation_rppg_landmark_stride",
                1,
            )
            try:
                landmark_stride = max(1, int(landmark_stride_raw))
            except (TypeError, ValueError):
                landmark_stride = 1
            run_cmd = [
                str(launcher),
                str(input_path),
                "--inject",
                "--output",
                str(output_path),
            ]
            # ORDER mirrors rPPG/rppg.bat:
            #     --inject --iterative --iterate-from-base --skip-diagnosis
            # Plus --skip-kinematic-gate + --landmark-stride.
            if iterative:
                run_cmd.append("--iterative")
                if iterate_from_baseline:
                    run_cmd.append("--iterate-from-baseline")
                if skip_diagnosis:
                    run_cmd.append("--skip-diagnosis")
            if skip_kinematic_gate:
                # v8 kinematic preflight is README-marked "new, untested"
                # (see docs/rppg-wiring.md).
                run_cmd.append("--skip-kinematic-gate")
            if landmark_stride > 1:
                run_cmd.extend(["--landmark-stride", str(landmark_stride)])
            output_lines: list[str] = []
            returncode = -1
            # Initial deadline depends on mode (PR #43 friend feedback
            # "no arbitrary timeout"): iterative mode legitimately runs
            # ~10min so the old hard 600s was borderline; we bump the
            # initial to 1800s for iterative and keep 600s for one-shot.
            # Even past the initial, the deadline RATCHETS FORWARD by
            # 90s every time the injector emits a new "Iteration N/M"
            # marker — so a healthy iterative run that keeps making
            # progress NEVER hits the wall. The wall only fires on a
            # genuinely-silent injector (real stall / wedge).
            _TIMEOUT = 1800 if iterative else 600

            # Progress tracker (PR #43, user feedback: "show progress
            # like we do for oldcam"). Parses Iteration N/M markers
            # from stdout, emits user-friendly progress at info, and
            # provides a deadline-extender callback that ratchets the
            # wall clock forward as long as the injector is making
            # forward progress. Honors the GUI verbose_logging flag
            # (or its non-prefixed alias) so "more nitty gritty when
            # verbose is checked" works.
            from automation.rppg import _RppgProgressTracker
            verbose = _cfg_bool(
                "verbose_logging", "automation_verbose_logging", True,
            )

            # The tracker emits synthesized progress lines like
            #   "rPPG iter 3/10 frame 144/242 (~50%)"
            # which we parse to update the per-queue-item progress bar
            # in the queue widget. The bar shows overall rPPG progress
            # roughly as (completed_iters / max_iters * 100) blended with
            # the within-iter frame percent. Assumes max_iters=10 (the
            # injector default); early-stop just means the bar never
            # reaches 100 — fine, the milestone line is the real "done"
            # signal. The regex lives at class level (_RPPG_PCT_PAT)
            # for consistency with _OLDCAM_PCT_PAT and to survive
            # __new__-constructed test instances.

            def _progress_report(message: str, level: str) -> None:
                # Bridge the tracker's (message, level) into self.log.
                self.log(message, level)
                m = self._RPPG_PCT_PAT.search(message)
                if m is not None:
                    try:
                        cur_iter = int(m.group(1))
                        max_iter = max(1, int(m.group(2)))
                        frame_pct = int(m.group(3))
                    except (TypeError, ValueError):
                        return
                    overall = max(
                        0,
                        min(
                            99,
                            int(((cur_iter - 1) * 100 + frame_pct) / max_iter),
                        ),
                    )
                    # Skip the redraw on identical-value re-emissions
                    # (subagent M3 on PR #52 round 1).
                    if overall != item.stage_percent:
                        item.stage_percent = overall
                        self.update_queue_display()

            tracker = _RppgProgressTracker(
                report_cb=_progress_report, verbose=verbose,
            )
            # Round-3 subagent HIGH (PR #54): anchor the tracker's
            # elapsed clock to subprocess launch time so the
            # completion banner matches the heartbeat output. Without
            # this, the GUI heartbeat would log "⏳ rPPG warming up...
            # 7 min elapsed" then the tracker would say "1m 20s
            # elapsed" at completion — visually contradictory for the
            # same job. The CLI path (automation/rppg.py:run_rppg) got
            # the same fix in commit 93d1fbe; this is the missing
            # GUI-side sibling. Use time.monotonic() to match what
            # stream_subprocess_with_timeout uses internally.
            import time as _time
            tracker.anchor_start_time(_time.monotonic())
            tracker_extender = tracker.deadline_extender if iterative else None

            def _on_rppg_line(text: str) -> None:
                if _is_tf_noise(text):
                    return
                stripped = text.strip()
                # The launcher's own dependency self-heal diagnostics (the
                # rppg_import_diag.py per-module report + WARN/ERROR status
                # lines) are kept OUT of the user-facing panel to avoid a
                # wall of raw [rppg-diag] text in front of a non-technical
                # user (user feedback, PR #67). They still go to the rotating
                # FILE log (debug level) so they're fully recoverable, and a
                # single friendly "checking dependencies" note is shown once.
                # The precise 1–2 line cause + a pointer to rppg.log is
                # surfaced by _surface_rppg_failure_detail only IF rPPG fails.
                if _is_rppg_setup_diag(stripped):
                    if not getattr(self, "_rppg_dep_note_shown", False):
                        self._rppg_dep_note_shown = True
                        self.log(
                            "[rPPG] Checking / preparing rPPG dependencies… "
                            "(details in the log file)",
                            "info",
                        )
                    self.log(stripped, "debug")  # file-only unless verbose
                    return
                # Panel noise (heavy TF chatter) is always debug-level
                # regardless of verbose, otherwise pass to the tracker
                # which decides user-friendly progress + verbose gating.
                if _is_panel_noise(text):
                    self.log(text, "debug")
                else:
                    tracker.on_line(text)
                # The injector emits its OWN internal pass/fail
                # self-grade against strict metric targets. At
                # the sub-perceptual --strength 0.005 ship default
                # it's mathematically impossible to satisfy all of
                # phase<=18°, temporal>=0.85, harmonic>=0.7
                # simultaneously, so this line routinely reads FAIL
                # even though the pulse was successfully injected. The
                # pipeline contract is exit-code-based — exit 0 ==
                # success — so annotate the confusing line to prevent
                # users reading it as a pipeline error (user feedback,
                # PR #41).
                if text.strip().startswith("Test Result:"):
                    self.log(
                        "  ^ injector internal self-grade only; "
                        "pipeline OK if injector exits 0 "
                        "(sub-perceptual --strength 0.005 "
                        "can't satisfy all strict targets).",
                        "debug",
                    )

            try:
                # Shared reader-thread + hard wall-clock helper (single
                # source of truth with automation/rppg.py). A bare
                # readline() loop — even with a deadline checked first —
                # blocks forever if the injector stalls mid-line, freezing
                # this worker with is_running set and breaking the
                # documented graceful-skip guarantee. The helper owns the
                # wall clock on the main thread so a silent hang is still
                # killed + skipped on schedule.
                from automation.rppg import (
                    is_rppg_progress_line,
                    stream_subprocess_with_timeout,
                )

                # Heartbeat callback — v2.7 fix for the "8-min silent
                # gap" UX bug. The rPPG injector takes ~7-8 min on CPU
                # between "Launching rppg_injector.py" and its first
                # visible stdout line (MediaPipe model load + baseline
                # ROI extraction happen silently). Without a heartbeat
                # the user thinks the process is wedged. Fires every
                # 60s during the silent window, stops the moment the
                # first injector line arrives. Lives in the PARENT
                # streamer (not the child progress tracker) because
                # the silent window is BEFORE the child emits anything.
                def _on_rppg_heartbeat(elapsed_seconds: float) -> None:
                    mins = int(elapsed_seconds // 60)
                    self.log(
                        f"⏳ rPPG warming up... {mins} min elapsed "
                        f"(loading MediaPipe model + extracting "
                        f"baseline ROIs; first iteration starts soon)",
                        "info",
                    )

                returncode, output_lines = stream_subprocess_with_timeout(
                    run_cmd,
                    cwd=str(launcher.parent),
                    timeout_seconds=_TIMEOUT,
                    on_line=_on_rppg_line,
                    deadline_extender=tracker_extender,
                    on_heartbeat=_on_rppg_heartbeat,
                    heartbeat_silence_predicate=is_rppg_progress_line,
                    abort_event=self._abort_event,
                    on_process_start=self._publish_active_subprocess,
                )
            except subprocess.TimeoutExpired:
                # A user Abort raises the same TimeoutExpired as a real
                # timeout — distinguish them so the log message is honest.
                if self._abort_requested():
                    self.log(
                        "⛔ rPPG aborted by user. Continuing without rPPG; "
                        "filename will be marked with -NORPPG.",
                        "warning",
                    )
                    return None
                minutes = max(1, int(_TIMEOUT // 60))
                self.log(
                    f"❌ RPPG FAILED — took longer than {minutes} min "
                    f"and was stopped. Continuing without rPPG; filename "
                    f"will be marked with -NORPPG.",
                    "error_bold",
                )
                return None
            except Exception as exc:
                self.log(
                    f"❌ RPPG FAILED — could not launch the rPPG tool "
                    f"({type(exc).__name__}: {exc}). Continuing without "
                    f"rPPG; filename will be marked with -NORPPG.",
                    "error_bold",
                )
                return None
            finally:
                # Clear the published handle so a later Abort can't target this
                # (now-finished) rPPG process before the next stage publishes
                # its own (code-review HIGH #3).
                self._publish_active_subprocess(None)

            if returncode == 0:
                # The injector renames our --output to append a metric
                # suffix ({stem}-rppg - <snr>-<phase>-...{ext}) regardless
                # of --output. Resolve the actual produced file, then apply
                # the user's metric-in-filename preference (clean name +
                # .metrics.json sidecar when off). Both helpers are the
                # shared single source of truth with the automation
                # pipeline — do not reimplement here.
                from automation.rppg import (
                    finalize_rppg_output,
                    resolve_produced_output,
                )

                # Thread the queue's logger into the resolver so the
                # playability gate's quarantine messages (PR #53)
                # actually surface in the GUI log instead of being
                # swallowed silently. Subagent H1 round 1.
                produced = resolve_produced_output(
                    output_path,
                    progress_cb=lambda m, lvl="info": self.log(m, lvl),
                )
                if produced is not None:
                    # _parse_bool tolerates string-backed JSON
                    # ("false"/"0"/...) — a bare bool() treats the string
                    # "false" as truthy and would silently flip this on
                    # for a manually-edited config (CodeRabbit, PR #40).
                    # None (uncoercible) -> default False.
                    from face_similarity import _parse_bool

                    keep_metrics = bool(
                        _parse_bool(
                            self.get_config().get("rppg_metrics_in_filename", False)
                        )
                    )
                    final = finalize_rppg_output(
                        produced,
                        output_path,
                        keep_metrics=keep_metrics,
                        progress_cb=lambda msg, lvl="info": self.log(msg, lvl),
                    )
                    self.log(f"✓ rPPG output: {final.name}", "success")
                    self.log("✅ RPPG DONE", "milestone")
                    return str(final)
                self.log(
                    "❌ RPPG FAILED — the rPPG tool finished but no "
                    "output video was produced. Continuing without "
                    "rPPG; filename will be marked with -NORPPG.",
                    "error_bold",
                )
                return None

            reason = self._rppg_failure_description(returncode)
            self.log(
                f"❌ RPPG FAILED — {reason} Continuing without rPPG; "
                f"filename will be marked with -NORPPG.",
                "error_bold",
            )
            # Surface the actionable diagnostic, not just the last raw line.
            # The launcher's per-module import report ([rppg-diag] ...) and
            # any "Still missing:" detail are the lines that name the failing
            # dependency; promote them ahead of the last-line tail so the user
            # sees WHY rPPG failed (v2.16 logging overhaul).
            self._surface_rppg_failure_detail(output_lines, launcher)
            return None
        except Exception as e:
            self.log(
                f"❌ RPPG FAILED — unexpected error in queue worker "
                f"({type(e).__name__}: {e}). Continuing without rPPG; "
                f"filename will be marked with -NORPPG.",
                "error_bold",
            )
            return None

    def _surface_rppg_failure_detail(self, output_lines, launcher) -> None:
        """Show a FRIENDLY 1–2 line cause + a pointer to the full log.

        The GUI processing log is read by a non-technical end user, so this
        must NOT dump a wall of raw ``[rppg-diag]`` lines or an 8 KB log tail
        (user feedback, PR #67). Instead:

          • If the failing module(s) can be extracted from the captured output,
            show ONE concise line naming them ("📦 missing/broken: mediapipe").
          • Always show a single pointer line to ``.launcher_state/rppg.log``
            ("📄 Full details: <path>") so the precise stack is one click away.

        The full per-module diagnostic still lands in that file (the launcher
        tees it there) and in the GUI's own rotating file log (the live diag
        lines are logged at ``debug``). Every branch is wrapped so a logging
        failure can never turn a graceful rPPG skip into a crash.

        macOS note: ``run_rppg.sh`` has no dependency self-heal/import-probe, so
        it emits no ``[rppg-diag]`` lines — ``modules`` is empty by design on
        macOS and only the ``📄`` log pointer shows (the rppg.log path is still
        correct + current). Per-module naming parity on macOS is the v2.17
        unified-deps work (see docs/v2.17-unified-gpu-deps-handoff.md).
        """
        modules = ""
        try:
            lines = [str(ln).strip() for ln in (output_lines or []) if str(ln).strip()]
            modules = _extract_rppg_failed_modules(lines)
        except Exception:  # noqa: BLE001 — extraction must never crash the skip
            modules = ""

        if modules:
            self.log(
                f"   [deps] rPPG dependency problem: {modules}. "
                "The app will keep working; only the rPPG step is skipped.",
                "warning",
            )

        # Always give the user a single, clickable path to the full detail
        # instead of pasting the log into the panel.
        try:
            repo_root = Path(launcher).resolve().parent.parent
            log_path = repo_root / ".launcher_state" / "rppg.log"
            if log_path.is_file():
                self.log(f"   [log] Full details in: {log_path}", "warning")
        except Exception:  # noqa: BLE001 — best-effort; never break the skip
            pass

    @staticmethod
    def _rppg_failure_description(returncode: int) -> str:
        """Translate a subprocess exit code into plain-English text.

        End users will never know what ``code 1`` means; replace the
        numeric exit code with a one-sentence description that hints
        at the actual class of problem. Returncode 0 should never reach
        here (it's the success path).
        """
        if returncode == 1:
            return "the rPPG processor crashed unexpectedly."
        if returncode in (124, 137):
            # 124 = GNU timeout; 137 = SIGKILL (often OOM-killed)
            return "the rPPG processor was stopped (timeout or out of memory)."
        if returncode == 2:
            return "the rPPG processor rejected its arguments (internal bug)."
        if returncode < 0:
            # POSIX: returncode = -N means killed by signal N
            return f"the rPPG processor was killed by signal {-returncode}."
        return f"the rPPG processor exited unexpectedly (code {returncode})."

    def _get_current_prompt(self, config: dict) -> str:
        """Get the current prompt from config."""
        slot = config.get("current_prompt_slot", 1)
        saved_prompts = config.get("saved_prompts", {})
        return saved_prompts.get(str(slot), "") or ""

    def _get_current_negative_prompt(self, config: dict) -> str:
        """Get current negative prompt from config."""
        slot = config.get("current_prompt_slot", 1)
        saved = config.get("negative_prompts", {})
        return saved.get(str(slot), "") or ""
