"""rPPG injection post-process — the LAST stage of the pipeline.

Pipeline order is Kling -> Loop -> Oldcam -> **rPPG**. rPPG runs last on
purpose: loop's ping-pong reverse would play a pre-injected pulse backwards
(non-physiological, detectable) and oldcam's resolution-crush would attenuate
a pre-injected sub-perceptual pulse. Injecting on the final delivered pixels
preserves the correct physiological signal.

This module shells out to the gitignored ``rPPG/`` tool via its Windows
``run_rppg.bat`` launcher. The injector itself (``rPPG/rppg_injector.py``) is
never imported or copied into tracked code. The step degrades gracefully:
if the launcher is absent, the injector errors, or it produces no output, we
log a warning and return ``None`` — callers keep the pre-rPPG video and the
run continues. It must never raise into the queue/pipeline.

Invocation (one-shot, deterministic naming):
    rPPG/run_rppg.bat "<abs in.mp4>" --inject --output "<abs out.mp4>" --skip-kinematic-gate

``--skip-kinematic-gate`` is passed deliberately: the injector's v8 kinematic
preflight is marked "new, untested" by the tool's own README. Re-enabling that
gate is a deliberate FUTURE ENHANCEMENT, not an oversight — see
docs/rppg-wiring.md.

Platform note: ``run_rppg.bat`` is Windows-only. A macOS injector launcher is
explicitly out of scope for this pass; ``run_rppg`` simply skips gracefully
(launcher-missing path) on platforms without the .bat.
"""

from __future__ import annotations

import os
import shlex
import subprocess
from pathlib import Path
from typing import Callable, List, Optional

ProgressCB = Optional[Callable[[str, str], None]]


def _format_cmd_for_log(cmd: List[str]) -> str:
    """Render a command list shell-paste-safe for the current OS.

    POSIX shells parse ``shlex.join``; cmd.exe / PowerShell need MS-style
    quoting from ``subprocess.list2cmdline``. Mirrors automation/oldcam.py.
    """
    if os.name == "nt":
        return subprocess.list2cmdline(cmd)
    return shlex.join(cmd)


def _report(progress_cb: ProgressCB, message: str, level: str = "info") -> None:
    if progress_cb:
        progress_cb(message, level)


def resolve_rppg_launcher(repo_root: Path) -> Optional[Path]:
    """Return the rPPG launcher path if the gitignored tool is present.

    Returns ``None`` (caller skips gracefully) when ``rPPG/run_rppg.bat`` or
    ``rPPG/rppg_injector.py`` is missing — e.g. a release without the tool, or
    a non-Windows host (the launcher is a .bat).
    """
    launcher = repo_root / "rPPG" / "run_rppg.bat"
    injector = repo_root / "rPPG" / "rppg_injector.py"
    if not launcher.exists() or not injector.exists():
        return None
    return launcher


def build_rppg_output_path(input_path: Path) -> Path:
    """``video.mp4`` -> ``video-rppg.mp4`` (mirrors build_oldcam_output_path).

    Combined with oldcam this yields e.g.
    ``clip_looped-oldcam-v24-rppg.mp4`` because rPPG runs on oldcam's output.

    NOTE: this is the path we *request* via ``--output``. The injector
    then renames it, appending a metric suffix (see
    :func:`resolve_produced_output`). Do not assume this exact path exists
    after a run — resolve the actual produced file instead.
    """
    return input_path.with_name(f"{input_path.stem}-rppg{input_path.suffix}")


def resolve_produced_output(requested: Path) -> Optional[Path]:
    """Find the file the injector actually produced.

    Empirically (verified via oldcam-testing/rppg_harness.py against the
    real tool) the injector takes our ``--output`` of ``{stem}-rppg{ext}``
    and *renames* it to ``{stem}-rppg - <snr>-<phase>-<temporal>-<motion>
    -<harmonic>{ext}`` regardless of ``--output`` — the documented
    deterministic-path contract does NOT hold. So accept either the exact
    requested path or the metric-suffixed sibling, whichever is newest.
    Returns None if nothing matching was produced (graceful-skip caller).
    """
    if requested.exists():
        return requested
    stem = requested.stem  # e.g. "<clip>-rppg"
    ext = requested.suffix
    parent = requested.parent
    candidates = sorted(
        (p for p in parent.glob(f"{stem}*{ext}") if p.is_file()),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    return candidates[0] if candidates else None


def run_rppg(
    *,
    video_path: Path,
    repo_root: Path,
    progress_cb: ProgressCB = None,
    timeout_seconds: int = 600,
) -> Optional[Path]:
    """Run one-shot rPPG injection. Return the output Path, or None on any
    failure (graceful skip — caller keeps the pre-rPPG video).
    """
    launcher = resolve_rppg_launcher(repo_root)
    if launcher is None:
        _report(progress_cb, "rPPG skipped: rPPG/ tool not present.", "warning")
        return None

    input_path = Path(video_path)
    if not input_path.exists():
        _report(progress_cb, f"rPPG skipped: input missing ({input_path.name}).", "warning")
        return None

    output_path = build_rppg_output_path(input_path)
    cmd = [
        str(launcher),
        str(input_path),
        "--inject",
        "--output",
        str(output_path),
        # Deliberate: v8 kinematic preflight is README-marked untested.
        # Re-enabling is a future enhancement (docs/rppg-wiring.md).
        "--skip-kinematic-gate",
    ]
    _report(progress_cb, f"rPPG launching: {_format_cmd_for_log(cmd)}", "info")

    output_lines: List[str] = []
    try:
        process = subprocess.Popen(
            cmd,
            cwd=str(launcher.parent),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
        )
        assert process.stdout is not None
        for line in process.stdout:
            line_text = line.rstrip()
            if line_text:
                output_lines.append(line_text)
                _report(progress_cb, line_text, "info")
        completed_returncode = process.wait(timeout=timeout_seconds)
    except subprocess.TimeoutExpired:
        if "process" in locals() and process.poll() is None:
            process.kill()
        _report(progress_cb, f"rPPG timed out after {timeout_seconds}s", "warning")
        return None
    except Exception as exc:  # never raise into the pipeline
        _report(progress_cb, f"rPPG launcher error: {exc}", "warning")
        return None

    if completed_returncode != 0:
        tail = output_lines[-15:] if output_lines else []
        _report(
            progress_cb,
            f"rPPG injection failed (exit={completed_returncode}); keeping pre-rPPG video.",
            "warning",
        )
        if tail:
            for line in tail:
                _report(progress_cb, f"  {line}", "warning")
        else:
            _report(progress_cb, "  (no stdout/stderr captured)", "warning")
        return None

    produced = resolve_produced_output(output_path)
    if produced is None:
        _report(progress_cb, "rPPG ran but output missing; keeping pre-rPPG video.", "warning")
        return None
    _report(progress_cb, f"rPPG injection applied: {produced.name}", "success")
    return produced
