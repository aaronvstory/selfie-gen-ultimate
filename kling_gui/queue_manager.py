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
        self.log("Processing started", "info")

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
            temp_input: Optional[Path] = None
            try:
                config = self.get_config()
                versions_to_run = self._get_oldcam_versions_to_run()
                selected_versions = self._get_selected_oldcam_versions()
                if not versions_to_run:
                    message = "Oldcam not selected."
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
                        self.log(f"Oldcam rerun increment target: {expected_output.name}", "info")

                self.log(
                    "Oldcam-only rerun: source="
                    + f"{source_video.name}, versions={','.join(selected_versions)}",
                    "info",
                )
                output_path = self._oldcam_video(str(run_input), QueueItem(str(source_video)))
                summary = self._last_oldcam_run_summary or {}
                requested_versions = summary.get("requested_versions", [])
                succeeded_versions = summary.get("succeeded_versions", [])
                failed_versions = summary.get("failed_versions", [])
                primary_output = summary.get("primary_output", "")
                if requested_versions:
                    self.log(
                        "Oldcam-only rerun summary: requested versions="
                        + ",".join(str(v) for v in requested_versions)
                        + "; succeeded versions="
                        + ",".join(str(v) for v in succeeded_versions)
                        + "; failed/skipped versions="
                        + ",".join(f"{v} ({r})" for v, r in failed_versions)
                        + "; primary output="
                        + str(primary_output),
                        "info",
                    )
                if output_path and Path(output_path).exists():
                    self.log(f"Oldcam-only rerun complete: {Path(output_path).name}", "success")
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
                self.log("Queue processing complete", "success")
                return

            self.update_queue_display()
            self.log(f"Processing: {item.filename}", "info")

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
                    self.log(f"Completed: {item.filename}", "success")

                    final_video = result
                    # Check if loop video is enabled
                    if config.get("loop_videos", False):
                        looped_video = self._loop_video(result, item)
                        if looped_video:
                            final_video = looped_video

                    # Run oldcam only when one or more versions are selected.
                    if self._get_oldcam_versions_to_run():
                        oldcam_video = self._oldcam_video(final_video, item)
                        if oldcam_video:
                            final_video = oldcam_video

                    item.output_path = final_video
                    self.log(f"Saved to: {final_video}", "info")
                else:
                    item.status = "failed"
                    item.error_message = self._get_generation_error_message()
                    self.log(f"Failed {item.filename}: {item.error_message}", "error")

            except Exception as e:
                item.status = "failed"
                item.error_message = str(e)
                self.log(f"Error processing {item.filename}: {e}", "error")

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
        # Respect model capability: if not supported, drop negative_prompt
        supports_negative = (
            self.get_config()
            .get("model_capabilities", {})
            .get(self.generator.model_endpoint, False)
        )
        neg_for_payload = negative_prompt if supports_negative else None

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
                self.log(
                    f"Looped video saved: {os.path.basename(looped_path)}", "success"
                )
                return looped_path
            else:
                self.log(f"Failed to create looped video", "warning")
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

    def _run_oldcam_version(self, video_path: str, version: str) -> Optional[str]:
        """Run one oldcam version and return output path if successful."""
        oldcam_dir = self._resolve_oldcam_dir(version)
        launcher_path = oldcam_dir / "launcher.py"
        if not launcher_path.exists():
            self.log(f"Oldcam {version} launcher not found", "warning")
            return None

        if not self._ensure_oldcam_dependencies(oldcam_dir, version):
            self.log(f"Skipping Oldcam {version} Finish due to missing dependencies", "warning")
            return None

        self.log(f"Applying Oldcam {version} Finish...", "info")
        run_cmd = [sys.executable, "-u", str(launcher_path), video_path]
        completed = subprocess.run(
            run_cmd,
            cwd=str(oldcam_dir),
            capture_output=True,
            text=True,
            timeout=600,
            check=False,
        )
        if completed.returncode == 0:
            input_path = Path(video_path)
            oldcam_output = self._build_oldcam_output_path(input_path, version)
            if oldcam_output.exists():
                self.log(f"Oldcam {version} Finish applied: {oldcam_output.name}", "success")
                return str(oldcam_output)
            self.log(f"Oldcam {version} process completed but output file was not found", "warning")
            return None

        self.log(f"Oldcam {version} Finish failed (code {completed.returncode})", "warning")
        err = (completed.stderr or completed.stdout or "").strip()
        if err:
            self.log(err.splitlines()[-1], "warning")
        return None

    def _ensure_oldcam_dependencies(self, oldcam_dir: Path, version: str) -> bool:
        """Check Oldcam requirements in current interpreter and emit install guidance."""
        if self._oldcam_deps_status_by_version.get(version) is True:
            return True

        required_modules = ["cv2", "numpy"]
        requires_mediapipe = version in {"v9", "v10"}
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
                if version in {"v9", "v10"}:
                    self.log(
                        f"Oldcam {version} requires mediapipe tasks + {self._task_model_filename()}. Install guidance: {install_cmd}",
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
            item: Queue item being processed
        """
        del item  # Reserved for future per-item status hooks
        try:
                versions_to_run = self._get_oldcam_versions_to_run()
                self.log("Oldcam selected: running " + ", ".join(versions_to_run), "info")
                outputs: List[Tuple[str, str]] = []
                failures: List[Tuple[str, str]] = []
                for version in versions_to_run:
                    output_path = self._run_oldcam_version(video_path, version)
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
                # Primary output is highest version number among successful runs.
                primary = max(outputs, key=lambda entry: self._oldcam_version_key(entry[0]))
                self._last_oldcam_run_summary = {
                    "requested_versions": requested,
                    "succeeded_versions": [version for version, _ in outputs],
                    "failed_versions": failures,
                    "primary_output": primary[1],
                }
                self.log(
                    "Oldcam summary: requested versions="
                    + ",".join(requested)
                    + "; succeeded versions="
                    + ",".join(version for version, _ in outputs)
                    + "; failed/skipped versions="
                    + ",".join(f"{version} ({reason})" for version, reason in failures)
                    + "; primary output="
                    + primary[1],
                    "info",
                )
                return primary[1]
        except Exception as e:
            self.log(f"Error applying Oldcam Finish: {e}", "warning")
            self._last_oldcam_run_summary = None
            return None

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
