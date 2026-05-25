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

    monkeypatch.setattr(rppg_mod, "_is_playable_video", lambda _p, **_kw: False)

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
    def fake_playable(path, **_kw):
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

    monkeypatch.setattr(rppg_mod, "_is_playable_video", lambda _p, **_kw: True)

    result = resolve_produced_output(older)
    assert result is not None
    assert str(result) == str(newer)


def test_is_playable_video_timeout_fails_open_when_secondary_inconclusive(
    monkeypatch, tmp_path: Path,
):
    """PR #53 round 4 — Codex P2 + round 5 H2. A timeout means
    ffprobe took too long to validate, which is ambiguous (long video
    vs real hang). We run a cheap secondary container check; if THAT
    can't decide (own timeout / ffprobe missing), fail-open. If the
    secondary explicitly rejects the file as corrupt, return False.

    This test covers the inconclusive case — the round-5 H2 fix
    added the secondary check.
    """
    import subprocess as _sp

    fake_video = tmp_path / "long.mp4"
    fake_video.write_bytes(b"junk")

    # Reset _warned_missing so the FNF advisory doesn't bleed across
    # tests via shared module state. M5 round 5 — subagent flagged
    # the existing test was order-dependent.
    monkeypatch.delattr(
        rppg_mod._is_playable_video, "_warned_missing", raising=False,
    )

    def raise_timeout(*_a, **_kw):
        raise _sp.TimeoutExpired(cmd="ffprobe", timeout=180)

    monkeypatch.setattr(_sp, "run", raise_timeout)
    # Force the secondary check to be inconclusive (returns None).
    monkeypatch.setattr(rppg_mod, "_is_playable_secondary", lambda *_a, **_k: None)

    logged = []
    result = rppg_mod._is_playable_video(
        fake_video, progress_cb=lambda m, l="info": logged.append((l, m)),
    )
    assert result is True
    assert any(
        l == "warning" and "playability gate timed out" in m
        for l, m in logged
    )


def test_is_playable_video_timeout_rejects_when_secondary_says_corrupt(
    monkeypatch, tmp_path: Path,
):
    """PR #53 round 5 H2: when -count_frames times out AND the
    secondary container probe explicitly identifies corruption, we
    return False (reject) — the gate's primary purpose is to catch
    torn mp4s, which is EXACTLY the input that hangs -count_frames.
    """
    import subprocess as _sp

    fake_video = tmp_path / "torn.mp4"
    fake_video.write_bytes(b"junk")

    monkeypatch.delattr(
        rppg_mod._is_playable_video, "_warned_missing", raising=False,
    )

    def raise_timeout(*_a, **_kw):
        raise _sp.TimeoutExpired(cmd="ffprobe", timeout=180)

    monkeypatch.setattr(_sp, "run", raise_timeout)
    # Secondary check says: this IS corrupt.
    monkeypatch.setattr(rppg_mod, "_is_playable_secondary", lambda *_a, **_k: False)

    logged = []
    result = rppg_mod._is_playable_video(
        fake_video, progress_cb=lambda m, l="info": logged.append((l, m)),
    )
    assert result is False
    assert any(
        l == "warning" and "secondary container probe rejected" in m
        for l, m in logged
    )


def test_quarantine_blacklists_on_rename_failure(monkeypatch, tmp_path: Path):
    """PR #53 round 6 (reviewer blocker): when path.replace fails
    (Windows file-handle contention is the common case), the corrupt
    file's absolute path must be added to the in-memory blacklist so
    subsequent resolve_produced_output passes skip it instead of
    re-running 180s ffprobe on it.
    """
    # Clean blacklist for test isolation.
    rppg_mod._corrupt_blacklist.clear()

    victim = tmp_path / "clip-rppg.mp4"
    victim.write_bytes(b"junk")

    def fail_replace(self, _target):
        raise PermissionError("simulated Windows handle contention")

    monkeypatch.setattr(
        "pathlib.Path.replace", fail_replace, raising=True,
    )

    rppg_mod._quarantine_corrupt(victim)
    # Even though the rename failed, the path is now blacklisted.
    assert rppg_mod._is_blacklisted(victim) is True


def test_resolve_skips_blacklisted_without_calling_playability_gate(
    monkeypatch, tmp_path: Path,
):
    """PR #53 round 6: the resolver loop short-circuits on blacklisted
    candidates BEFORE the expensive _is_playable_video call. Without
    this, a locked corrupt file would eat 180s of ffprobe per
    resolver invocation forever in this process.
    """
    rppg_mod._corrupt_blacklist.clear()

    older = tmp_path / "clip-rppg.mp4"
    _touch_mp4(older, mtime_offset=-100.0)
    newer = tmp_path / "clip-rppg - 9.0-3.0-0.5-0.1-0.5.mp4"
    _touch_mp4(newer, mtime_offset=+100.0)

    # Pre-blacklist the newer file.
    rppg_mod._blacklist(newer)

    # Track whether the gate was called on the blacklisted candidate.
    calls: list[Path] = []

    def tracked_playable(path, **_kw):
        calls.append(path)
        return True

    monkeypatch.setattr(rppg_mod, "_is_playable_video", tracked_playable)

    result = rppg_mod.resolve_produced_output(older)
    # The blacklisted candidate is skipped; the older one is checked
    # and returned.
    assert result is not None
    assert str(result) == str(older)
    # _is_playable_video was called once (on `older`), NOT on `newer`.
    assert len(calls) == 1
    assert str(calls[0]) == str(older)


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
