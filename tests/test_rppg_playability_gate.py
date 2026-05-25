"""Regression tests for the rPPG playability gate added in
PR fix/step0-composite-and-rppg-v2.5.

`resolve_produced_output` now iterates candidates newest-to-oldest
and validates each via ffprobe. Corrupt candidates are quarantined
with a `.broken` suffix so future resolver passes can't re-select
them AND the user can still inspect them. Only the newest PLAYABLE
candidate is returned; None if all are corrupt.

The previous behaviour (newest candidate wins unconditionally) shipped
a corrupt -rppg.mp4 to the user as if it were the final deliverable
because the iter-best snapshot race (also fixed in this PR) produced
a torn copy.
"""

from pathlib import Path

import automation.rppg as rppg_mod
from automation.rppg import resolve_produced_output


def _touch_mp4(path: Path, mtime_offset: float = 0.0):
    """Create a stub mp4 at *path* and bump its mtime by *mtime_offset* seconds.

    Negative offsets make the file OLDER than wall-clock; positive newer.
    """
    path.write_bytes(b"dummy-mp4")
    import os
    if mtime_offset:
        st = path.stat()
        os.utime(path, (st.st_atime, st.st_mtime + mtime_offset))


def test_resolve_returns_none_when_all_candidates_corrupt(
    monkeypatch, tmp_path: Path,
):
    """All candidates fail ffprobe -> resolver returns None and
    quarantines them all.
    """
    requested = tmp_path / "clip-rppg.mp4"
    _touch_mp4(requested)
    _touch_mp4(tmp_path / "clip-rppg - 9.0-3.0-0.5-0.1-0.5.mp4")

    monkeypatch.setattr(rppg_mod, "_is_playable_video", lambda _p: False)

    result = resolve_produced_output(requested)
    assert result is None

    # Both originals are quarantined (renamed with .broken). Either
    # the requested or the metric-suffix sibling — they're both gone
    # from their original names.
    assert not requested.exists()
    broken = list(tmp_path.glob("*.broken"))
    assert len(broken) == 2


def test_resolve_skips_corrupt_returns_next_playable(
    monkeypatch, tmp_path: Path,
):
    """Newest candidate is corrupt, older one is playable.
    Resolver quarantines the corrupt newer file and returns the
    older valid one (instead of throwing the valid file away as the
    old `return candidates[0]` would have done).
    """
    older = tmp_path / "clip-rppg.mp4"
    _touch_mp4(older, mtime_offset=-100.0)
    newer = tmp_path / "clip-rppg - 9.0-3.0-0.5-0.1-0.5.mp4"
    _touch_mp4(newer, mtime_offset=+100.0)

    # Newer is corrupt; older is playable.
    def fake_playable(path):
        return str(path) != str(newer)

    monkeypatch.setattr(rppg_mod, "_is_playable_video", fake_playable)

    result = resolve_produced_output(older)
    assert result is not None
    assert str(result) == str(older)
    assert not newer.exists()
    # And the .broken sibling is there for post-mortem.
    assert any(p.suffix == ".broken" for p in tmp_path.iterdir())


def test_resolve_returns_newest_when_all_playable(
    monkeypatch, tmp_path: Path,
):
    """Sanity: when nothing is corrupt, the resolver still returns
    the newest candidate exactly as before.
    """
    older = tmp_path / "clip-rppg.mp4"
    _touch_mp4(older, mtime_offset=-100.0)
    newer = tmp_path / "clip-rppg - 9.0-3.0-0.5-0.1-0.5.mp4"
    _touch_mp4(newer, mtime_offset=+100.0)

    monkeypatch.setattr(rppg_mod, "_is_playable_video", lambda _p: True)

    result = resolve_produced_output(older)
    assert result is not None
    assert str(result) == str(newer)


def test_is_playable_video_detects_nal_errors(monkeypatch, tmp_path: Path):
    """`_is_playable_video` recognises the H.264 NAL-corruption
    signatures the user reported in their bug.
    """
    import subprocess

    fake_video = tmp_path / "bad.mp4"
    fake_video.write_bytes(b"junk")

    class FakeProc:
        def __init__(self, returncode, stdout="", stderr=""):
            self.returncode = returncode
            self.stdout = stdout
            self.stderr = stderr

    # NAL error -> not playable
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *_a, **_k: FakeProc(
            returncode=0, stdout="0\n",
            stderr="[h264 @ 0xff] Invalid NAL unit size (0 > 857270).",
        ),
    )
    assert rppg_mod._is_playable_video(fake_video) is False

    # Clean playable -> True
    monkeypatch.setattr(
        subprocess, "run",
        lambda *_a, **_k: FakeProc(returncode=0, stdout="242\n", stderr=""),
    )
    assert rppg_mod._is_playable_video(fake_video) is True

    # ffprobe missing -> skip gate (return True with warning so we
    # don't block a healthy run when the validator is unavailable).
    def raise_fnf(*_a, **_k):
        raise FileNotFoundError("ffprobe")
    monkeypatch.setattr(subprocess, "run", raise_fnf)
    # Reset the one-time warning flag so this assertion is meaningful.
    if hasattr(rppg_mod._is_playable_video, "_warned_missing"):
        del rppg_mod._is_playable_video._warned_missing  # type: ignore[attr-defined]
    assert rppg_mod._is_playable_video(fake_video) is True

    # Zero frames -> not playable
    monkeypatch.setattr(
        subprocess, "run",
        lambda *_a, **_k: FakeProc(returncode=0, stdout="0\n", stderr=""),
    )
    assert rppg_mod._is_playable_video(fake_video) is False
