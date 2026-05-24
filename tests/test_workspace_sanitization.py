"""Workspace name validation for concurrent-launch isolation (PR #49).

`path_utils._sanitize_workspace_name` is the single gate keeping malformed
workspace names — empty strings, path-traversal sequences, Windows reserved
device names, leading dots, whitespace — from ever reaching the filesystem
layout code. A regression here would let a workspace name like ``..`` escape
the user-data root via ``<root>/workspaces/../...``, which the
``os.path.commonpath`` defense-in-depth in ``get_workspace_dir`` would still
catch but at a lower layer than the sanitizer.
"""

import pytest

import path_utils


ACCEPT_NAMES = [
    "default",
    "shoot-a",
    "shoot_b.v1",
    "A",
    "Z-9_8.7",
    "a" * 64,  # exact length limit
]

REJECT_NAMES = [
    "",                # empty
    "   ",             # whitespace-only — strips to empty
    ".",               # bare dot
    "..",              # path-traversal
    ".hidden",         # leading dot (dotfile)
    "a/b",             # slash
    "a\\b",            # backslash
    "a b",             # whitespace inside
    "a\tb",            # tab
    "a:b",             # colon (Windows-illegal in path)
    "a*b",             # glob
    "CON",             # Windows reserved device
    "con",             # case-insensitive Windows reserved
    "PRN", "AUX", "NUL", "COM1", "LPT9",
    "a" * 65,          # exceeds 64-char limit
]


@pytest.mark.parametrize("name", ACCEPT_NAMES)
def test_accept(name):
    assert path_utils._sanitize_workspace_name(name) == name


@pytest.mark.parametrize("name", REJECT_NAMES)
def test_reject(name):
    with pytest.raises(ValueError):
        path_utils._sanitize_workspace_name(name)


def test_set_workspace_round_trip(monkeypatch):
    """set_workspace -> get_workspace -> canonical name (env-mediated)."""
    monkeypatch.delenv("KLING_WORKSPACE", raising=False)
    assert path_utils.set_workspace("shoot-a") == "shoot-a"
    assert path_utils.get_workspace() == "shoot-a"


def test_get_workspace_falls_back_on_corrupt_env(monkeypatch):
    """A stale env value that fails sanitization must NOT crash get_workspace."""
    monkeypatch.setenv("KLING_WORKSPACE", "../escape")
    # No exception; falls back to "default" so the GUI can still launch.
    assert path_utils.get_workspace() == path_utils.WORKSPACE_DEFAULT


def test_get_workspace_default_when_unset(monkeypatch):
    monkeypatch.delenv("KLING_WORKSPACE", raising=False)
    assert path_utils.get_workspace() == path_utils.WORKSPACE_DEFAULT
