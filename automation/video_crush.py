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
from typing import Optional, Tuple


def _summarize_ffmpeg_error(stderr: str) -> str:
    """Reduce a multi-line FFmpeg stderr blob to one user-friendly line."""
    if not stderr:
        return "FFmpeg returned no output (encoder may have failed to start)"
    lower = stderr.lower()
    if "could not open encoder" in lower:
        return "FFmpeg could not open the H.264 encoder (libx264 init failed)"
    if "invalid argument" in lower:
        return "FFmpeg rejected the encoder configuration (invalid argument)"
    if "no such file" in lower or "no such directory" in lower:
        return "FFmpeg could not find the input file"
    if "permission denied" in lower:
        return "FFmpeg permission denied (close any program holding the file open)"
    if "conversion failed" in lower:
        return "FFmpeg conversion failed (check logs for details)"
    first_line = next((line.strip() for line in stderr.splitlines() if line.strip()), "")
    if len(first_line) > 160:
        return first_line[:160] + "…"
    return first_line or "Unknown error (check logs for details)"


def check_ffmpeg_available() -> Tuple[bool, str]:
    """Check if FFmpeg is available in PATH."""
    try:
        result = subprocess.run(
            ["ffmpeg", "-version"], capture_output=True, text=True,
            errors="replace", timeout=10,
        )
        if result.returncode == 0:
            version_line = result.stdout.split("\n")[0] if result.stdout else "FFmpeg found"
            return True, version_line
        return False, "FFmpeg found but returned error"
    except FileNotFoundError:
        return False, "FFmpeg not found in PATH. Please install FFmpeg."
    except subprocess.TimeoutExpired:
        return False, "FFmpeg check timed out"
    except Exception as e:
        return False, f"Error checking FFmpeg: {e}"


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

    input_file = Path(input_path)
    if not input_file.exists():
        log(f"Crush: input file not found: {input_path}", "error")
        return None
    if not input_file.is_file():
        log(f"Crush: not a file: {input_path}", "error")
        return None

    if output_path is None:
        output_file = input_file.parent / f"{input_file.stem}{suffix}{input_file.suffix}"
    else:
        output_file = Path(output_path)

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
