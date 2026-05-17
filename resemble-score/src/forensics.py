"""Offline temporal-forensics — predict whether oldcam (V24) will help a
clip BEFORE spending a Resemble API call.

Discovered empirically (oldcam-testing bench, 3 known-outcome clips +
calibration on the omnapayments corpus): the Resemble deepfake detector
keys on TWO independent tells, and oldcam's destructive resolution
round-trip (V24) can only attack one of them:

  1. Spatial diffusion fingerprint — high-frequency AI texture. V24's
     uniform downscale->Lanczos crush destroys this. Clips whose tell is
     spatial respond hugely (GISELLE: 0.99 -> 0.018; sim86: 1.0 -> 0.45).
  2. Temporal-cadence brokenness — irregular motion rhythm: bursts of
     fast motion interleaved with near-frozen frames, jerky acceleration.
     The signature of a low-fps-upsampled or generation-unstable clip.
     V24 only touches pixels, so this tell SURVIVES intact (signal-142926:
     1.0 -> 1.0, no movement at all).

This module measures the *temporal* axis from a clip's raw motion
trajectory — no model, no network, ~1-2s/clip — and emits a verdict:

  - "spatial"  : smooth cadence; the tell (if any) is spatial -> V24 is
                 the right tool, likely a big win. Worth the API call.
  - "temporal" : broken cadence; the tell is temporal -> V24 cannot help
                 (it never touches timing). Re-generate the source or try
                 a temporal-resmoothing pass; do NOT waste an API call
                 expecting V24 to fix it.
  - "uncertain": in the calibration grey zone -> score it to find out.

Thresholds are provisional (calibrated on a finite corpus) and live in
TEMPORAL_THRESHOLDS so they can be re-tuned as more labelled data lands.
"""

from __future__ import annotations

from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Optional

import cv2
import numpy as np

# Analysis frame size — small enough to be fast, large enough that the
# motion trajectory is faithful. (The metrics are ratios so absolute
# resolution does not matter; this only bounds compute.)
_PROBE_W, _PROBE_H = 96, 160
# Cap sampled frames so a very long clip can't dominate runtime; the
# cadence statistics converge well before this.
_MAX_FRAMES = 900

# Decision thresholds on the composite temporal-instability score (see
# temporal_instability_score). Calibrated against THREE ground-truth API
# outcomes plus the offline distribution of a 181-clip real corpus
# (omnapayments scans):
#
#   ground truth (actual Resemble outcomes through oldcam V24):
#     GISELLE  composite 0.585  -> V24 BIG WIN  (0.99 -> 0.018)   = spatial
#     sim86    composite 0.517  -> V24 partial  (1.00 -> 0.45)    = spatial
#     signal   composite 4.97   -> V24 NOTHING  (1.00 -> 1.00)    = temporal
#
#   181-clip corpus: 65% <= 0.75, 80% <= 0.90, median 0.63, p95 1.26,
#   max 2.16 (the omnapayments worst case is FAR milder than the
#   Signal-relayed `signal` clip, confirming that's an extreme outlier).
#
# spatial_max=0.80 comfortably contains both proven V24 wins (0.52, 0.59)
# and the tight corpus bulk -> ~65% classify as confident "spatial".
# temporal_min=1.30 sits above ~96% of the corpus, flagging only the
# genuinely broken minority (and the proven 4.97 failure) as "don't waste
# the API call". The 0.80-1.30 grey band (~23% of corpus) is the honest
# "score it to find out" zone — biased so a false "skip" (which would
# cost a usable source) stays rare. Re-tune as more labelled outcomes
# land; the bench RESUME.md / SCOREBOARD are the system of record.
TEMPORAL_THRESHOLDS = {
    "spatial_max": 0.80,   # composite <= this  -> spatial tell, V24 likely helps
    "temporal_min": 1.30,  # composite >= this  -> temporal tell, V24 won't help
    # between the two -> "uncertain" (score it to find out)
}


@dataclass
class TemporalForensics:
    """Raw temporal-cadence metrics + the derived verdict for one clip."""

    path: str
    frames_analyzed: int
    fps: float
    duration_s: float
    width: int
    height: int

    motion_median: float       # median frame-to-frame mean-abs-diff
    smoothness: float          # motion std / mean  (regular cadence -> low)
    burst_pct: float           # % frames with motion > 2.5x median
    freeze_pct: float          # % frames with motion < 0.25x median
    jerk: float                # mean |2nd-derivative of motion| / median

    composite: float           # weighted instability score
    verdict: str               # "spatial" | "temporal" | "uncertain"
    recommendation: str        # human-readable next-step

    def to_dict(self) -> dict:
        return asdict(self)


def _safe_div(a: float, b: float) -> float:
    return a / b if b > 1e-9 else 0.0


def temporal_instability_score(
    smoothness: float, burst_pct: float, freeze_pct: float, jerk: float
) -> float:
    """Combine the four cadence metrics into one instability number.

    Each term is on a comparable scale: smoothness and jerk are already
    ratios ~0.2-0.9; burst%/freeze% are divided by 10 so a 16% burst
    contributes ~1.6 (matching the empirical separation where the broken
    clip sat ~2.4 and the good ones ~0.5). Weights are deliberately simple
    and equal-ish — the signal is strong enough not to need a fitted model
    at n=3; revisit if corpus calibration shows otherwise.
    """
    return (
        smoothness
        + jerk
        + (burst_pct / 10.0)
        + (freeze_pct / 10.0)
    )


def _verdict(composite: float) -> tuple[str, str]:
    t = TEMPORAL_THRESHOLDS
    if composite <= t["spatial_max"]:
        return (
            "spatial",
            "Smooth cadence — the deepfake tell (if any) is spatial. "
            "oldcam V24's resolution crush is the right tool; an API "
            "score is worth running (expect a large improvement).",
        )
    if composite >= t["temporal_min"]:
        return (
            "temporal",
            "Broken motion cadence (bursts/freezes/jerk) — the tell is "
            "TEMPORAL, which V24 cannot touch (it only alters pixels). "
            "Do NOT expect V24 to help; re-generate the source or try a "
            "uniform temporal-resmoothing pass before scoring.",
        )
    return (
        "uncertain",
        "Cadence is in the calibration grey zone — V24 may or may not "
        "help. Scoring is the only way to know for this clip.",
    )


def analyze_clip(path: str | Path) -> Optional[TemporalForensics]:
    """Compute temporal forensics for one video. Returns None if the clip
    cannot be opened or has too few frames to judge."""
    p = str(path)
    cap = cv2.VideoCapture(p)
    if not cap.isOpened():
        cap.release()
        return None

    total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT)) or 0
    fps = float(cap.get(cv2.CAP_PROP_FPS) or 0.0)
    width = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH) or 0)
    height = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT) or 0)

    prev: Optional[np.ndarray] = None
    flow: list[float] = []
    read = 0
    while read < _MAX_FRAMES:
        ok, frame = cap.read()
        if not ok:
            break
        read += 1
        g = cv2.cvtColor(
            cv2.resize(frame, (_PROBE_W, _PROBE_H)), cv2.COLOR_BGR2GRAY
        ).astype(np.int16)
        if prev is not None:
            flow.append(float(np.mean(np.abs(g - prev))))
        prev = g
    cap.release()

    # Need a handful of inter-frame deltas for the statistics to mean
    # anything (a 1-3 frame clip has no cadence).
    if len(flow) < 8:
        return None

    fl = np.asarray(flow, dtype=np.float64)
    med = float(np.median(fl))
    mean = float(fl.mean())
    std = float(fl.std())

    smoothness = _safe_div(std, mean)
    burst_pct = 100.0 * float(np.mean(fl > 2.5 * med)) if med > 0 else 0.0
    freeze_pct = 100.0 * float(np.mean(fl < 0.25 * med)) if med > 0 else 0.0
    # 2nd difference = change in acceleration; a natural camera has near-0
    # jerk, AI cadence breaks spike it. Normalised by median motion.
    jerk = _safe_div(float(np.mean(np.abs(np.diff(fl, 2)))), med) if med > 0 else 0.0

    composite = temporal_instability_score(
        smoothness, burst_pct, freeze_pct, jerk
    )
    verdict, recommendation = _verdict(composite)

    frames_analyzed = len(fl) + 1
    # CAP_PROP_FRAME_COUNT (`total`) is unreliable on many container/codec
    # combos (returns 0, or fewer frames than actually decodable). When it
    # is missing or inconsistent with what we actually read, fall back to
    # the frames we analyzed — those came from real cap.read() calls.
    if fps > 0:
        if total > 0 and total >= frames_analyzed:
            duration_s = total / fps
        else:
            duration_s = frames_analyzed / fps
    else:
        duration_s = 0.0

    return TemporalForensics(
        path=p,
        frames_analyzed=frames_analyzed,
        fps=round(fps, 3),
        duration_s=round(duration_s, 2),
        width=width,
        height=height,
        motion_median=round(med, 4),
        smoothness=round(smoothness, 4),
        burst_pct=round(burst_pct, 2),
        freeze_pct=round(freeze_pct, 2),
        jerk=round(jerk, 4),
        composite=round(composite, 4),
        verdict=verdict,
        recommendation=recommendation,
    )


def format_line(f: TemporalForensics) -> str:
    """One-line summary for CLI / batch output."""
    return (
        f"{f.verdict:9s} composite={f.composite:6.3f} "
        f"smooth={f.smoothness:5.2f} burst={f.burst_pct:4.0f}% "
        f"freeze={f.freeze_pct:4.0f}% jerk={f.jerk:5.2f}  "
        f"{Path(f.path).name}"
    )
