"""Regression tests for the iter-best snapshot integrity guard added
in PR fix/step0-composite-and-rppg-v2.5.

The bug: PR #52's `best_iteration_snapshot.mp4` defensive snapshot
did a direct `shutil.copy2(iter_output, _BEST_SNAPSHOT_NAME)` and
adopted the result as `best_path`. When the iter writer had not yet
fully flushed the file, the copy captured a torn mp4 — and the
post-loop final-copy from `best_path` shipped that broken file as
the final -rppg.mp4 deliverable (ffprobe Invalid NAL unit size).

The fix: copy to a `.tmp.mp4`, validate via `_snapshot_validates`,
then `os.replace` only if validation passes. The previous good
snapshot is NEVER overwritten by a torn copy.

These tests cover `_snapshot_validates` directly because the
snapshot adoption code lives inside the giant
`process_video_with_iterative_pid_manipulation` method which
needs the full mediapipe + opencv stack to instantiate; the helper
is the testable unit.
"""

from pathlib import Path


def _import_validator(monkeypatch=None):
    """Import _snapshot_validates without spinning up the injector class.

    Returns the function or skips the test if a DEPENDENCY (e.g.,
    mediapipe) is unavailable in CI. CodeRabbit PR #53 round 3:
    narrow the except clause to ImportError/ModuleNotFoundError so a
    real runtime regression in rppg_injector (e.g. SyntaxError, a
    crash at module-import time) fails loudly instead of being
    silently skipped.

    Subagent L2 round 5: when a monkeypatch fixture is passed, use
    ``syspath_prepend`` so the rPPG/ insertion is reverted on test
    teardown. Without that, a `sys.path.insert(0, ...)` here leaked
    for the rest of the pytest process and could shadow same-named
    modules in later tests.
    """
    rppg_dir = str(Path(__file__).resolve().parent.parent / "rPPG")
    if monkeypatch is not None:
        monkeypatch.syspath_prepend(rppg_dir)
    else:
        import sys
        sys.path.insert(0, rppg_dir)
    try:
        from rppg_injector import _snapshot_validates
        return _snapshot_validates
    except (ImportError, ModuleNotFoundError) as exc:
        import pytest
        pytest.skip(f"rppg_injector dependency unavailable: {exc}")


def test_snapshot_validates_rejects_size_mismatch(tmp_path: Path, monkeypatch):
    """A truncated snapshot (different size from source) is rejected
    before even opening cv2 — the cheap size pre-check catches it.
    """
    _snapshot_validates = _import_validator(monkeypatch)

    src = tmp_path / "iter_5.mp4"
    snapshot = tmp_path / "snapshot.tmp.mp4"
    src.write_bytes(b"a" * 4096)
    snapshot.write_bytes(b"a" * 2048)  # truncated
    assert _snapshot_validates(str(snapshot), str(src)) is False


def test_snapshot_validates_rejects_missing_file(tmp_path: Path, monkeypatch):
    """A snapshot path that doesn't exist on disk is rejected
    (getsize raises OSError, caught and treated as failure).
    """
    _snapshot_validates = _import_validator(monkeypatch)

    src = tmp_path / "iter_5.mp4"
    src.write_bytes(b"data")
    missing = tmp_path / "nonexistent.tmp.mp4"
    assert _snapshot_validates(str(missing), str(src)) is False


def test_blacklist_key_idempotent_for_same_file(tmp_path, monkeypatch):
    """PR #53 round 9 (subagent M4): the corrupt-file blacklist key
    function must produce the same key for the same on-disk file
    even when the caller passes lexically-different paths
    (e.g. ``foo/../foo/bar.mp4`` vs ``foo/bar.mp4``). Symlinks AND
    case-insensitivity collapse the same way.
    """
    import os as _os
    import automation.rppg as rppg_mod
    rppg_mod._corrupt_blacklist.clear()

    target = tmp_path / "victim.mp4"
    target.write_bytes(b"junk")

    rppg_mod._blacklist(target)
    assert rppg_mod._is_blacklisted(target) is True

    # Lexically different but referring to the same file: round-trip
    # via parent/.. — realpath collapses this back to `target`.
    via_dotdot = tmp_path / "victim.mp4" / ".." / "victim.mp4"
    assert rppg_mod._is_blacklisted(via_dotdot) is True

    # If the filesystem is case-insensitive AND normcase is non-
    # identity (Windows), a different-cased path should also hit.
    # Skip the case-insensitivity branch on platforms where the
    # combination doesn't hold (POSIX with case-sensitive fs OR
    # macOS where realpath doesn't canonicalize case AND normcase
    # is identity).
    different_case = tmp_path / "VICTIM.MP4"
    if different_case.exists():
        canonical_target = _os.path.normcase(_os.path.realpath(str(target)))
        canonical_dc = _os.path.normcase(_os.path.realpath(str(different_case)))
        if canonical_target == canonical_dc:
            assert rppg_mod._is_blacklisted(different_case) is True


def test_snapshot_validates_rejects_unopenable_video(tmp_path: Path, monkeypatch):
    """Even when sizes match, a snapshot that cv2 can't open
    (e.g. zero frames, garbage bytes that happen to be the right
    size) is rejected. This is the safety net for the case where
    the file is "complete" by byte-count but the H.264 container
    header is malformed.
    """
    _snapshot_validates = _import_validator(monkeypatch)

    src = tmp_path / "iter_5.mp4"
    snapshot = tmp_path / "snapshot.tmp.mp4"
    src.write_bytes(b"x" * 1024)
    snapshot.write_bytes(b"x" * 1024)  # same size but not a valid mp4
    # cv2.VideoCapture on garbage bytes either returns isOpened=False or
    # nb_frames=0 — both branches reject. (We don't mock cv2 here
    # because the validator is the unit under test.)
    assert _snapshot_validates(str(snapshot), str(src)) is False
