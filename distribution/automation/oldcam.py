from __future__ import annotations

import os
import re
import shlex
import subprocess
import sys
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple


def _format_cmd_for_log(cmd: List[str]) -> str:
    """Render a command list in a shell-paste-safe form for the current OS.

    POSIX shells parse the output of `shlex.join`; cmd.exe / PowerShell
    instead need MS-style quoting which `subprocess.list2cmdline` produces.
    Python's `shlex` docs explicitly warn its quoting is not guaranteed on
    non-POSIX shells, so the launch-command log line stays copy-pasteable
    on either platform.
    """
    if os.name == "nt":
        return subprocess.list2cmdline(cmd)
    return shlex.join(cmd)


ProgressCB = Optional[Callable[[str, str], None]]


def _report(progress_cb: ProgressCB, message: str, level: str = "info") -> None:
    if progress_cb:
        progress_cb(message, level)


def _version_key(version: str) -> Tuple[int, str]:
    match = re.search(r"(\d+)$", version)
    number = int(match.group(1)) if match else -1
    return number, version


def discover_oldcam_versions(repo_root: Path) -> List[str]:
    versions: List[str] = []
    for item in repo_root.iterdir():
        if not item.is_dir():
            continue
        name = item.name.lower()
        if name.startswith("oldcam-v") and (item / "launcher.py").exists():
            versions.append(name.replace("oldcam-", ""))
    return sorted(set(versions), key=_version_key)


def build_oldcam_output_path(input_path: Path, version: str) -> Path:
    v = version.lower()
    return input_path.with_name(f"{input_path.stem}-oldcam-{v}{input_path.suffix}")


def resolve_oldcam_dir(repo_root: Path, version: str) -> Path:
    return repo_root / f"oldcam-{version.lower().replace('v', 'v')}"


def ensure_oldcam_dependencies() -> Tuple[bool, Optional[str]]:
    try:
        import cv2  # noqa: F401
        import numpy  # noqa: F401
        return True, None
    except Exception as exc:
        return False, str(exc)


def run_oldcam_version(
    *,
    video_path: Path,
    version: str,
    repo_root: Path,
    progress_cb: ProgressCB = None,
    timeout_seconds: int = 600,
) -> Optional[Path]:
    oldcam_dir = repo_root / f"oldcam-{version.lower()}"
    launcher = oldcam_dir / "launcher.py"
    if not launcher.exists():
        _report(progress_cb, f"Oldcam {version} launcher missing.", "warning")
        return None

    deps_ok, deps_error = ensure_oldcam_dependencies()
    if not deps_ok:
        _report(progress_cb, f"Oldcam deps missing: {deps_error}", "warning")
        return None

    cmd = [sys.executable, "-u", str(launcher), str(video_path)]
    # Log the exact command via a platform-aware formatter so paths with spaces
    # or shell-special characters render unambiguously and copy-paste cleanly
    # into the host's shell (POSIX → shlex.join, Windows → list2cmdline).
    _report(progress_cb, f"Oldcam {version} launching: {_format_cmd_for_log(cmd)}", "info")
    output_lines: List[str] = []
    try:
        process = subprocess.Popen(
            cmd,
            cwd=str(oldcam_dir),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            errors="replace",  # oldcam stdout may carry non-UTF-8 bytes
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
        _report(progress_cb, f"Oldcam {version} timed out after {timeout_seconds}s", "warning")
        return None
    except Exception as exc:
        _report(progress_cb, f"Oldcam {version} launcher error: {exc}", "warning")
        return None
    if completed_returncode != 0:
        tail = output_lines[-15:] if output_lines else []
        _report(progress_cb, f"Oldcam {version} failed (exit={completed_returncode}):", "warning")
        if tail:
            for line in tail:
                _report(progress_cb, f"  {line}", "warning")
        else:
            _report(progress_cb, "  (no stdout/stderr captured)", "warning")
        return None

    output_path = build_oldcam_output_path(video_path, version)
    if not output_path.exists():
        _report(progress_cb, f"Oldcam {version} ran but output missing.", "warning")
        return None
    _report(progress_cb, f"Oldcam {version} output: {output_path.name}", "success")
    return output_path


def run_oldcam_all(
    *,
    video_path: Path,
    version_setting: str,
    repo_root: Path,
    progress_cb: ProgressCB = None,
) -> List[Tuple[str, Path]]:
    """Run EVERY selected oldcam version and return ``[(version, path)]``
    for all that succeeded (empty list if none).

    This is the fan-out-aware primitive: the GUI queue and the automation
    pipeline both need *every* per-version output (so rPPG can inject into
    each — there is no privileged "primary"). ``run_oldcam`` below is the
    back-compat single-path wrapper (highest version) for callers that
    only want one. Single source of truth for version selection.
    """
    available = discover_oldcam_versions(repo_root)
    if not available:
        _report(progress_cb, "No oldcam versions discovered.", "warning")
        return []

    selected = version_setting.lower()
    if selected == "all":
        targets = available
    else:
        targets = [selected] if selected in available else []
    if not targets:
        _report(progress_cb, f"Requested oldcam version '{version_setting}' unavailable.", "warning")
        return []

    outputs: List[Tuple[str, Path]] = []
    for version in targets:
        out = run_oldcam_version(video_path=video_path, version=version, repo_root=repo_root, progress_cb=progress_cb)
        if out:
            outputs.append((version, out))
    return outputs


def run_oldcam(
    *,
    video_path: Path,
    version_setting: str,
    repo_root: Path,
    progress_cb: ProgressCB = None,
) -> Optional[Path]:
    """Back-compat single-path wrapper: returns the HIGHEST-version oldcam
    output (or None). Callers needing every per-version output (rPPG
    fan-out) must use :func:`run_oldcam_all` instead.
    """
    outputs = run_oldcam_all(
        video_path=video_path,
        version_setting=version_setting,
        repo_root=repo_root,
        progress_cb=progress_cb,
    )
    if not outputs:
        return None
    return max(outputs, key=lambda item: _version_key(item[0]))[1]
