"""Privacy guard: the shipped source tree must never contain the building
user's home-directory path (user mandate 2026-06-18 — "make sure the app
doesn't ship my username anywhere").

This scans every git-tracked file for the running user's resolved home path
(e.g. ``/Users/<name>``, ``/home/<name>``, ``C:\\Users\\<name>``). Nothing
tracked should embed it — runtime paths must be derived at run time via
``os.path.expanduser`` / env vars, which resolve on the END user's machine.

The test deliberately uses the LIVE home path (never a hard-coded username),
so it can't leak the name itself and it protects whoever builds the release.
"""

from __future__ import annotations

import getpass
import os
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent

# Generic CI / container home dirs that aren't a developer's identity — the
# guard targets a real person's machine, so skip these to avoid false hits on
# e.g. a workflow file that legitimately mentions /home/runner.
_GENERIC_HOMES = {
    "/root", "/home/runner", "/home/circleci", "/home/ubuntu",
    "/home/vscode", "/github/home", "/Users/runner",
}


def _tracked_files() -> list[Path]:
    out = subprocess.run(
        ["git", "ls-files"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=True,
    )
    return [REPO_ROOT / line for line in out.stdout.splitlines() if line.strip()]


def _home_needles() -> list[str]:
    """Home-path strings to hunt for, in both / and \\ separator forms."""
    home = os.path.expanduser("~").rstrip("/\\")
    user = getpass.getuser()
    needles: set[str] = set()
    if home and home not in _GENERIC_HOMES and home not in ("", "/"):
        # Require a real leaf (the username segment) to avoid scanning "/".
        leaf = home.replace("\\", "/").rstrip("/").split("/")[-1]
        if leaf and len(leaf) >= 3:
            needles.add(home)
            needles.add(home.replace("\\", "/"))   # normalize win -> posix
            needles.add(home.replace("/", "\\"))   # and posix -> win
    # The bare username only when it is specific enough to not false-positive
    # on ordinary prose (e.g. "a", "ci", "test").
    if user and len(user) >= 5 and user.isalnum():
        needles.add(user)
    return sorted(n for n in needles if n)


def test_no_tracked_file_leaks_builder_home_path() -> None:
    needles = _home_needles()
    if not needles:
        pytest.skip("home path / username too generic to scan safely")
    offenders: dict[str, list[str]] = {}
    for path in _tracked_files():
        try:
            text = path.read_bytes().decode("utf-8", "ignore")
        except OSError:
            continue
        hits = [n for n in needles if n in text]
        if hits:
            offenders[str(path.relative_to(REPO_ROOT))] = hits
    assert not offenders, (
        "Tracked files leak the builder's home path / username — these ship in "
        "the release. Use os.path.expanduser/env at runtime instead:\n"
        + "\n".join(f"  {f}: {hits}" for f, hits in offenders.items())
    )
