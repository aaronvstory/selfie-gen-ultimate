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


def _import_validator():
    """Import _snapshot_validates without spinning up the injector class.

    Returns the function or skips the test if importing the module
    fails on this machine (e.g. mediapipe unavailable in CI).
    """
    try:
        import sys
        sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "rPPG"))
        from rppg_injector import _snapshot_validates
        return _snapshot_validates
    except Exception as exc:
        import pytest
        pytest.skip(f"rppg_injector unavailable on this machine: {exc}")


def test_snapshot_validates_rejects_size_mismatch(tmp_path: Path):
    """A truncated snapshot (different size from source) is rejected
    before even opening cv2 — the cheap size pre-check catches it.
    """
    _snapshot_validates = _import_validator()

    src = tmp_path / "iter_5.mp4"
    snapshot = tmp_path / "snapshot.tmp.mp4"
    src.write_bytes(b"a" * 4096)
    snapshot.write_bytes(b"a" * 2048)  # truncated
    assert _snapshot_validates(str(snapshot), str(src)) is False


def test_snapshot_validates_rejects_missing_file(tmp_path: Path):
    """A snapshot path that doesn't exist on disk is rejected
    (getsize raises OSError, caught and treated as failure).
    """
    _snapshot_validates = _import_validator()

    src = tmp_path / "iter_5.mp4"
    src.write_bytes(b"data")
    missing = tmp_path / "nonexistent.tmp.mp4"
    assert _snapshot_validates(str(missing), str(src)) is False


def test_snapshot_validates_rejects_unopenable_video(tmp_path: Path):
    """Even when sizes match, a snapshot that cv2 can't open
    (e.g. zero frames, garbage bytes that happen to be the right
    size) is rejected. This is the safety net for the case where
    the file is "complete" by byte-count but the H.264 container
    header is malformed.
    """
    _snapshot_validates = _import_validator()

    src = tmp_path / "iter_5.mp4"
    snapshot = tmp_path / "snapshot.tmp.mp4"
    src.write_bytes(b"x" * 1024)
    snapshot.write_bytes(b"x" * 1024)  # same size but not a valid mp4
    # cv2.VideoCapture on garbage bytes either returns isOpened=False or
    # nb_frames=0 — both branches reject. (We don't mock cv2 here
    # because the validator is the unit under test.)
    assert _snapshot_validates(str(snapshot), str(src)) is False
