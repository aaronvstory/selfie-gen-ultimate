"""
Video Looper - FFmpeg wrapper for creating seamless ping-pong loop videos.

Uses the filter: [0:v]reverse[rv];[0:v][rv]concat=n=2:v=1:a=0[outv]
This creates a forward-then-reverse playback for seamless looping.
"""

import subprocess
import os
import shutil
from pathlib import Path
from typing import Optional, Tuple


def _summarize_ffmpeg_error(stderr: str) -> str:
    """Reduce a multi-line FFmpeg stderr blob to one user-friendly line.

    Looks for the most informative signal in priority order; falls back to
    a generic message. Full stderr is preserved separately in the file log
    via a "debug" level emit, so diagnostics are never lost.
    """
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
        return "FFmpeg conversion failed (see kling_gui.log for details)"
    first_line = next((l.strip() for l in stderr.splitlines() if l.strip()), "")
    if len(first_line) > 160:
        return first_line[:160] + "…"
    return first_line or "Unknown error (see kling_gui.log for details)"


def check_ffmpeg_available() -> Tuple[bool, str]:
    """
    Check if FFmpeg is available in PATH.

    Returns:
        (is_available, message)
    """
    try:
        result = subprocess.run(
            ["ffmpeg", "-version"], capture_output=True, text=True, timeout=10
        )
        if result.returncode == 0:
            # Extract version from first line
            version_line = (
                result.stdout.split("\n")[0] if result.stdout else "FFmpeg found"
            )
            return True, version_line
        else:
            return False, "FFmpeg found but returned error"
    except FileNotFoundError:
        return False, "FFmpeg not found in PATH. Please install FFmpeg."
    except subprocess.TimeoutExpired:
        return False, "FFmpeg check timed out"
    except Exception as e:
        return False, f"Error checking FFmpeg: {e}"


def create_looped_video(
    input_path: str,
    output_path: Optional[str] = None,
    suffix: str = "_looped",
    overwrite: bool = True,
    log_callback=None,
) -> Optional[str]:
    """
    Create a seamless ping-pong loop from a video file.

    Uses FFmpeg filter: [0:v]reverse[rv];[0:v][rv]concat=n=2:v=1:a=0[outv]
    This plays the video forward, then in reverse, creating a seamless loop.

    Args:
        input_path: Path to the input video file
        output_path: Optional output path. If None, uses input_path with suffix
        suffix: Suffix to add before extension (default: "_looped")
        overwrite: Whether to overwrite existing output file
        log_callback: Optional function(message, level) for logging

    Returns:
        Output path on success, None on failure
    """

    def log(msg: str, level: str = "info"):
        if log_callback:
            log_callback(msg, level)

    # Validate input
    input_file = Path(input_path)
    if not input_file.exists():
        log(f"Input file not found: {input_path}", "error")
        return None

    if not input_file.is_file():
        log(f"Input is not a file: {input_path}", "error")
        return None

    # Determine output path
    if output_path is None:
        stem = input_file.stem
        ext = input_file.suffix
        output_file = input_file.parent / f"{stem}{suffix}{ext}"
    else:
        output_file = Path(output_path)

    # Ensure output directory exists
    output_file.parent.mkdir(parents=True, exist_ok=True)

    # Check if output exists
    if output_file.exists() and not overwrite:
        log(f"Output already exists (skipping): {output_file}", "warning")
        return str(output_file)

    # Check FFmpeg availability
    ffmpeg_ok, ffmpeg_msg = check_ffmpeg_available()
    if not ffmpeg_ok:
        log(ffmpeg_msg, "error")
        return None

    # The caller (queue_manager._loop_video) already emits a friendly
    # "Creating looped video..." line; include filename only in the file
    # log to avoid a panel duplicate.
    log(f"Creating looped video: {input_file.name}", "debug")

    # Build FFmpeg command
    # Filter: play forward, then reversed, concatenated
    filter_complex = "[0:v]reverse[rv];[0:v][rv]concat=n=2:v=1:a=0[outv]"

    # Intermediate looped file: mathematically lossless H.264 so the forward+
    # reverse halves carry 100% of Kling's source detail into Oldcam. Oldcam's
    # own encoder (CRF 12 / preset slow / profile high) does the final size-
    # conscious encode downstream — the intermediate can be large.
    #
    # Use -qp 0 (constant quantizer 0) rather than -crf 0: it's the canonical
    # libx264 lossless idiom and works on every libx264 build. -crf 0 combined
    # with -profile:v high crashes on strict libx264 builds because true H.264
    # lossless requires the High 4:4:4 Predictive profile, but we need yuv420p
    # downstream (yuv444p breaks OpenCV decode paths). -qp 0 sidesteps the
    # profile-compatibility trap entirely; libx264 picks the appropriate
    # profile internally based on the chosen pix_fmt and quantizer.
    #
    # -tune film and -profile:v also dropped: -tune film's psy-rd/psy-trellis
    # are mathematically meaningless under lossless coding, and explicit
    # -profile:v constraints conflict with libx264's internal lossless path.
    #
    # Stream-copy concat rejected: PTS/DTS glitches when concatenating a
    # reversed H.264 half.
    cmd = [
        "ffmpeg",
        "-y" if overwrite else "-n",  # Overwrite or fail if exists
        "-i",
        str(input_file),
        "-filter_complex",
        filter_complex,
        "-map",
        "[outv]",
        "-c:v",
        "libx264",
        "-preset",
        "slow",
        "-qp",
        "0",
        "-pix_fmt",
        "yuv420p",
        "-movflags",
        "+faststart",
        str(output_file),
    ]

    try:
        # Step beat — file log only; the saved-file line a few seconds later
        # gives the user the meaningful "done" signal.
        log("Running FFmpeg...", "debug")

        # Run FFmpeg
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=300,  # 5 minute timeout
        )

        if result.returncode == 0:
            if output_file.exists():
                file_size = output_file.stat().st_size / (1024 * 1024)
                log(
                    f"Looped video saved: {output_file.name} ({file_size:.1f} MB)",
                    "success",
                )
                return str(output_file)
            else:
                log("FFmpeg completed but output file not found", "error")
                return None
        else:
            full_stderr = (result.stderr or "").strip()
            # Verbose dump → file log only (preserves diagnostics).
            if full_stderr:
                log(f"FFmpeg stderr (full): {full_stderr}", "debug")
            # Friendly one-liner → panel.
            log(f"Loop encode failed: {_summarize_ffmpeg_error(full_stderr)}", "error")
            return None

    except subprocess.TimeoutExpired:
        log("FFmpeg timed out (>5 minutes)", "error")
        return None
    except Exception as e:
        log(f"Error running FFmpeg: {e}", "error")
        return None


def get_video_duration(video_path: str) -> Optional[float]:
    """
    Get the duration of a video file in seconds.

    Args:
        video_path: Path to the video file

    Returns:
        Duration in seconds, or None on failure
    """
    try:
        cmd = [
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "default=noprint_wrappers=1:nokey=1",
            video_path,
        ]

        result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)

        if result.returncode == 0 and result.stdout.strip():
            return float(result.stdout.strip())
        return None

    except Exception:
        return None


# Self-test when run directly
if __name__ == "__main__":
    print("Video Looper - FFmpeg Wrapper")
    print("=" * 40)

    # Check FFmpeg
    available, message = check_ffmpeg_available()
    print(f"FFmpeg available: {available}")
    print(f"Message: {message}")

    if available:
        print("\nUsage: create_looped_video('input.mp4', 'output.mp4')")
        print("Or: create_looped_video('input.mp4')  # Creates input_looped.mp4")
