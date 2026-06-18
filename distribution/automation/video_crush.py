"""
Video Crush - FFmpeg wrapper for quality-degradation downscale.

Mimics the "upload to WhatsApp and re-download" quality destruction that
increases Persona pass rates. Re-encodes at 480p / CRF 35 (perceptible
compression), which is roughly equivalent to WhatsApp's transcoding tier.

Pipeline slot: Loop → Crush → Oldcam (Phase E order).
Oldcam runs on the crushed file so the compression artefact carries through.
"""

import subprocess
from pathlib import Path
from typing import List, Optional

from automation.video_loop import (
    check_ffmpeg_available,
    _summarize_ffmpeg_error,
)

# ---------------------------------------------------------------------------
# Selectable crush resolutions (2026-06-18). Crush gained a second tier so
# the user can fan out one or both quality-destroy passes, exactly like the
# Oldcam version checkboxes. 720p is the default for fresh installs; 480p is
# the original (harsher) tier. Ordered highest-first so the headline output
# is the higher-quality variant.
#
#   label -> target height (px)
# ---------------------------------------------------------------------------
CRUSH_RESOLUTIONS = {
    "720p": 720,
    "480p": 480,
}

# Fresh-install default: 720p ON (per user direction 2026-06-18).
DEFAULT_CRUSH_RESOLUTIONS: List[str] = ["720p"]

# Distinct stem suffix per resolution so the two crushed files never collide
# (e.g. clip_crush720.mp4 + clip_crush480.mp4).
CRUSH_SUFFIXES = {
    "720p": "_crush720",
    "480p": "_crush480",
}

# Sentinel distinguishing "key absent" from an explicit None/False so the
# legacy-migration branch can tell a brand-new config from one where the user
# deliberately turned crush off.
_UNSET = object()


def _canon_resolution(value) -> Optional[str]:
    """Map a loose resolution token to a canonical label or None.

    Accepts ``"720p"``, ``"720"``, ``720`` → ``"720p"`` (same for 480).
    """
    if value is None:
        return None
    token = str(value).strip().lower().rstrip("p")
    for label, height in CRUSH_RESOLUTIONS.items():
        if token == str(height):
            return label
    return None


def normalize_crush_resolutions(resolutions=_UNSET, legacy_enabled=_UNSET) -> List[str]:
    """Resolve the effective ordered list of crush resolution labels.

    Single source of truth shared by the GUI queue, the CLI pipeline, and the
    config panel so the legacy-key migration behaves identically everywhere.

    Precedence:
      1. ``resolutions`` present (list/tuple/str) → filter to valid labels,
         dedup, order highest-first. An explicit empty list means "crush off".
      2. else fall back on the legacy boolean ``crush_enabled``:
           True  → ['480p']  (preserve pre-2026-06-18 behaviour — crush was 480p)
           False → []        (user deliberately disabled crush; stay off)
      3. else (neither key present — a brand-new config) → DEFAULT (['720p']).

    Args:
        resolutions:    The ``crush_resolutions`` config value, or ``_UNSET``
                        when the key is absent.
        legacy_enabled: The legacy ``crush_enabled`` boolean, or ``_UNSET``
                        when that key is absent.
    """
    if resolutions is not _UNSET and resolutions is not None:
        if isinstance(resolutions, str):
            resolutions = [resolutions]
        if isinstance(resolutions, (list, tuple)):
            seen: set = set()
            valid: List[str] = []
            for raw in resolutions:
                label = _canon_resolution(raw)
                if label and label not in seen:
                    seen.add(label)
                    valid.append(label)
            # Order highest-first (720 before 480) for deterministic headline.
            return sorted(valid, key=lambda lbl: CRUSH_RESOLUTIONS[lbl], reverse=True)
        # Unknown type → treat as "not set".
    if legacy_enabled is not _UNSET and legacy_enabled is not None:
        return ["480p"] if bool(legacy_enabled) else []
    return list(DEFAULT_CRUSH_RESOLUTIONS)


def crush_suffix(label: str) -> str:
    """Return the stem suffix for a resolution label (``"720p"`` → ``_crush720``)."""
    return CRUSH_SUFFIXES.get(label, "_crush")


def crush_video(
    input_path: str,
    output_path: Optional[str] = None,
    suffix: str = "_crush",
    target_height: int = 480,
    crf: int = 35,
    overwrite: bool = True,
    log_callback=None,
) -> Optional[str]:
    """
    Quality-crush a video to mimic WhatsApp/social-media transcoding.

    Re-encodes at 480p height (width auto-scaled, divisible by 2) with
    CRF 35 — perceptible compression in the WhatsApp-upload tier.  This
    artefact pattern is associated with higher Persona pass rates when the
    crushed file is fed into Oldcam downstream.

    Args:
        input_path:    Path to the input video file.
        output_path:   Optional explicit output path.  If None, derives from
                       input stem + suffix (e.g. clip_crush.mp4).
        suffix:        Stem suffix when output_path is None (default: "_crush").
        target_height: Output height in pixels (default: 480).  Width is
                       auto-calculated as ``-2`` so it stays even-divisible.
        crf:           H.264 CRF quality value (default: 35 ≈ WhatsApp tier).
                       Lower = higher quality / larger file.
        overwrite:     Whether to overwrite an existing output file.
        log_callback:  Optional function(message: str, level: str) for logging.

    Returns:
        Absolute output path string on success, None on failure.
    """

    def log(msg: str, level: str = "info") -> None:
        if log_callback:
            log_callback(msg, level)

    input_file = Path(input_path).resolve()
    if not input_file.exists():
        log(f"Crush: input file not found: {input_path}", "error")
        return None
    if not input_file.is_file():
        log(f"Crush: not a file: {input_path}", "error")
        return None

    if output_path is None:
        output_file = input_file.parent / f"{input_file.stem}{suffix}{input_file.suffix}"
    else:
        output_file = Path(output_path).resolve()

    try:
        output_file.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        log(f"Crush: failed to create output directory: {exc}", "error")
        return None

    if output_file.exists() and not overwrite:
        log(f"Crush: output already exists (skipping): {output_file}", "warning")
        return str(output_file)

    ffmpeg_ok, ffmpeg_msg = check_ffmpeg_available()
    if not ffmpeg_ok:
        log(ffmpeg_msg, "error")
        return None

    log(f"Quality-crushing: {input_file.name} → {target_height}p CRF {crf}", "debug")

    # scale=-2:<height> keeps width auto-calculated and divisible by 2.
    # yuv420p: broadest player compatibility.
    # -c:a copy: audio untouched — we only want video artefacts.
    # -movflags +faststart: web-compatible MP4 header placement.
    cmd = [
        "ffmpeg",
        "-y" if overwrite else "-n",
        "-i", str(input_file),
        "-vf", f"scale=-2:{target_height}",
        "-c:v", "libx264",
        "-crf", str(crf),
        "-preset", "fast",
        "-pix_fmt", "yuv420p",
        "-c:a", "copy",
        "-movflags", "+faststart",
        str(output_file),
    ]

    try:
        log("Running FFmpeg crush…", "debug")
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            errors="replace",
            timeout=300,
        )

        if result.returncode == 0:
            if output_file.exists():
                mb = output_file.stat().st_size / (1024 * 1024)
                log(f"Crush saved: {output_file.name} ({mb:.1f} MB)", "success")
                return str(output_file)
            log("FFmpeg completed but crush output not found", "error")
            return None

        full_stderr = (result.stderr or "").strip()
        if full_stderr:
            log(f"FFmpeg crush stderr:\n{full_stderr}", "debug")
        friendly = _summarize_ffmpeg_error(full_stderr)
        log(f"Crush failed: {friendly}", "error")
        if output_file.exists():
            try:
                output_file.unlink()
            except OSError:
                pass
        return None

    except subprocess.TimeoutExpired:
        log("Crush timed out after 5 minutes", "error")
        if output_file.exists():
            try:
                output_file.unlink()
            except OSError:
                pass
        return None
    except Exception as exc:
        log(f"Crush unexpected error: {exc}", "error")
        return None
