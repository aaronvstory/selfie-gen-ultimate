"""Tests for the offline temporal-forensics pre-test.

These use synthetic OpenCV-written clips (no network, no fixtures) to
prove the cadence metrics and the spatial/temporal verdict behave as the
calibration found: a smooth clip -> "spatial" (V24 worth scoring), a
burst/freeze clip -> "temporal" (V24 cannot help).
"""

from __future__ import annotations

import numpy as np
import pytest

cv2 = pytest.importorskip("cv2")

from src.forensics import (  # noqa: E402
    TEMPORAL_THRESHOLDS,
    analyze_clip,
    temporal_instability_score,
)


def _write(path, frames, fps=24):
    h, w = frames[0].shape[:2]
    vw = cv2.VideoWriter(str(path), cv2.VideoWriter_fourcc(*"mp4v"), fps, (w, h))
    assert vw.isOpened()
    for f in frames:
        vw.write(f)
    vw.release()


def _smooth_pan(n=160, w=160, h=256):
    """Full-frame texture drifting at a constant, substantial velocity —
    a regular cadence with real per-frame change every frame (mimics a
    steady talking-head / panning shot: no freezes, no bursts)."""
    rng = np.random.default_rng(7)
    bg = rng.integers(0, 255, (h, w * 3, 3), np.uint8)  # wide so we can pan
    out = []
    for i in range(n):
        off = 2 + (i * 3)  # constant 3 px/frame -> steady motion every frame
        out.append(np.ascontiguousarray(bg[:, off:off + w]))
    return out


def _burst_freeze(n=160, w=160, h=256):
    """Long frozen holds punctuated by violent jumps — the broken cadence
    of a low-fps-upsampled / generation-unstable AI clip (freeze + burst)."""
    rng = np.random.default_rng(11)
    bg = rng.integers(0, 255, (h, w * 4, 3), np.uint8)
    out = []
    off = 5
    for i in range(n):
        if i % 10 == 0 and i > 0:
            off = 5 + (off + 200) % (w * 3)  # periodic teleport = burst
        # between teleports the frame is byte-identical = freeze
        out.append(np.ascontiguousarray(bg[:, off:off + w]))
    return out


def test_smooth_clip_predicts_spatial(tmp_path):
    p = tmp_path / "smooth.mp4"
    _write(p, _smooth_pan())
    r = analyze_clip(p)
    assert r is not None
    assert r.verdict == "spatial", (
        f"a regular-cadence clip must read 'spatial'; got "
        f"{r.verdict} (composite={r.composite})"
    )
    assert r.burst_pct == 0.0
    assert r.composite <= TEMPORAL_THRESHOLDS["spatial_max"]


def test_burst_freeze_clip_predicts_temporal(tmp_path):
    p = tmp_path / "broken.mp4"
    _write(p, _burst_freeze())
    r = analyze_clip(p)
    assert r is not None
    assert r.verdict == "temporal", (
        f"a burst/freeze clip must read 'temporal'; got "
        f"{r.verdict} (composite={r.composite})"
    )
    # the broken clip must be clearly separated from the smooth one;
    # irregular cadence shows up as high smoothness (motion variance),
    # bursts, freezes, or jerk — at least one must be elevated.
    assert r.composite >= TEMPORAL_THRESHOLDS["temporal_min"]
    assert (
        r.smoothness > 1.0
        or r.burst_pct > 0
        or r.freeze_pct > 0
        or r.jerk > 0.5
    )


def test_composite_is_monotonic_in_each_term():
    base = temporal_instability_score(0.3, 0.0, 0.0, 0.2)
    assert temporal_instability_score(0.9, 0.0, 0.0, 0.2) > base  # smoothness
    assert temporal_instability_score(0.3, 20.0, 0.0, 0.2) > base  # burst
    assert temporal_instability_score(0.3, 0.0, 20.0, 0.2) > base  # freeze
    assert temporal_instability_score(0.3, 0.0, 0.0, 0.9) > base   # jerk


def test_unreadable_or_too_short_returns_none(tmp_path):
    # not a video at all
    bad = tmp_path / "nope.mp4"
    bad.write_bytes(b"not a video")
    assert analyze_clip(bad) is None
    # a 3-frame clip has no cadence to judge
    short = tmp_path / "short.mp4"
    _write(short, _smooth_pan(n=3))
    assert analyze_clip(short) is None


def test_thresholds_form_a_valid_grey_band():
    assert (
        0.0
        < TEMPORAL_THRESHOLDS["spatial_max"]
        < TEMPORAL_THRESHOLDS["temporal_min"]
    ), "spatial_max must be below temporal_min (a non-empty 'uncertain' band)"
