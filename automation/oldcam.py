from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple


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
    completed = subprocess.run(
        cmd,
        cwd=str(oldcam_dir),
        capture_output=True,
        text=True,
        timeout=timeout_seconds,
        check=False,
    )
    if completed.returncode != 0:
        err = (completed.stderr or completed.stdout or "").strip()
        _report(progress_cb, f"Oldcam {version} failed ({completed.returncode}): {err}", "warning")
        return None

    output_path = build_oldcam_output_path(video_path, version)
    if not output_path.exists():
        _report(progress_cb, f"Oldcam {version} ran but output missing.", "warning")
        return None
    _report(progress_cb, f"Oldcam {version} output: {output_path.name}", "success")
    return output_path


def run_oldcam(
    *,
    video_path: Path,
    version_setting: str,
    repo_root: Path,
    progress_cb: ProgressCB = None,
) -> Optional[Path]:
    available = discover_oldcam_versions(repo_root)
    if not available:
        _report(progress_cb, "No oldcam versions discovered.", "warning")
        return None

    selected = version_setting.lower()
    if selected == "all":
        targets = available
    else:
        targets = [selected] if selected in available else []
    if not targets:
        _report(progress_cb, f"Requested oldcam version '{version_setting}' unavailable.", "warning")
        return None

    outputs: List[Tuple[str, Path]] = []
    for version in targets:
        out = run_oldcam_version(video_path=video_path, version=version, repo_root=repo_root, progress_cb=progress_cb)
        if out:
            outputs.append((version, out))
    if not outputs:
        return None
    return max(outputs, key=lambda item: _version_key(item[0]))[1]
