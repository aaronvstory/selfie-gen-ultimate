"""
face_kinematics.py — v8 liveness ensemble gate / verdict head.

Scores AI-gen vs real face videos on two motion axes that survive
visual post-processing because they're geometric, not pixel-level:

  - Head-pose angular jerk  (third derivative of head rotation)
  - Blink interval / duration distribution

Both are aggregated HumanScore-style as:
      score = 1 - (0.5*frequency + 0.3*severity + 0.2*persistence)

so a score of 1.0 is most-real, 0.0 is most-synthetic.

Used by:
  - rppg_injector.py   — preflight gate before --inject / --analyze
  - batch_analyze.py   — preflight gate + per-video verdict columns

Designed to be self-contained: own MediaPipe FaceLandmarker init, own
model-path resolution mirroring rppg_injector's pattern, OpenCV for
frame reads. Loose default threshold (pass_threshold=0.30) until we
calibrate against a labelled corpus.
"""
from __future__ import annotations

import math
import os
import sys
import urllib.request
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Optional

import cv2
import numpy as np

try:
    import mediapipe as mp
except ImportError as exc:
    raise ImportError(
        "face_kinematics requires mediapipe. Install with `pip install mediapipe`."
    ) from exc


DEFAULT_MODEL_URL = (
    "https://storage.googleapis.com/mediapipe-models/face_landmarker/"
    "face_landmarker/float16/1/face_landmarker.task"
)

# Loose defaults — tighten with labelled data once distributions are known.
DEFAULT_PASS_THRESHOLD = 0.30
DEFAULT_MAX_FRAMES = 600            # cap compute (~20s @ 30fps); decimate beyond
DEFAULT_BLINK_BLENDSHAPE_THRESHOLD = 0.45

# Head-pose jerk thresholds (deg / s^3). Real handheld video sits well
# below 1e4; generators that produce micro-tremor or sudden snaps push
# above. Keep liberal until we have data.
JERK_FLAG_THRESHOLD = 8.0e3
JERK_SEVERE_THRESHOLD = 5.0e4

# Blink prior (resting adult, awake, not staring at a screen for too long).
# https://en.wikipedia.org/wiki/Blink#Frequency cites 10-20/min as typical.
BLINK_RATE_PRIOR_MIN = 6.0          # blinks/min
BLINK_RATE_PRIOR_MAX = 30.0
BLINK_DURATION_PRIOR_MIN_MS = 80.0
BLINK_DURATION_PRIOR_MAX_MS = 450.0


@dataclass
class KinematicResult:
    overall: float                  # 0..1, higher = more human-like
    head_jerk: float                # 0..1
    blink: float                    # 0..1
    passed: bool                    # overall >= pass_threshold
    flags: list = field(default_factory=list)
    details: dict = field(default_factory=dict)

    def to_csv_row(self) -> dict:
        """Flat dict for batch_analyze CSV columns."""
        return {
            'kinematic_score': round(self.overall, 4),
            'kinematic_head_jerk': round(self.head_jerk, 4),
            'kinematic_blink': round(self.blink, 4),
            'kinematic_flags': ';'.join(self.flags),
            'kinematic_passed': 'true' if self.passed else 'false',
        }


# ── model path resolution (mirrors rppg_injector.py) ──────────────────────
def _resolve_model_path(explicit: Optional[str] = None) -> Optional[str]:
    if explicit and os.path.exists(explicit):
        return explicit

    env = os.environ.get('MEDIAPIPE_FACE_LANDMARKER_MODEL')
    if env and os.path.exists(env):
        return env

    candidates = [
        'face_landmarker.task',
        'face_landmarker_v2_with_blendshapes.task',
        os.path.join('models', 'face_landmarker.task'),
        os.path.join('models', 'face_landmarker_v2_with_blendshapes.task'),
        os.path.join('input', 'face_landmarker.task'),
        os.path.join('input', 'face_landmarker_v2_with_blendshapes.task'),
    ]
    for c in candidates:
        if os.path.exists(c):
            return c

    return _auto_download_model()


def _auto_download_model() -> Optional[str]:
    url = os.environ.get('MEDIAPIPE_FACE_LANDMARKER_MODEL_URL', DEFAULT_MODEL_URL)
    dest = os.path.join('models', 'face_landmarker.task')
    os.makedirs('models', exist_ok=True)
    tmp = dest + '.download'
    try:
        print(f"[face_kinematics] downloading FaceLandmarker model: {url}")
        with urllib.request.urlopen(url, timeout=90) as resp, open(tmp, 'wb') as out:
            while True:
                chunk = resp.read(1024 * 1024)
                if not chunk:
                    break
                out.write(chunk)
        if os.path.getsize(tmp) == 0:
            return None
        os.replace(tmp, dest)
        return dest
    except Exception as exc:
        print(f"[face_kinematics] auto-download failed: {exc}")
        return None
    finally:
        if os.path.exists(tmp):
            try: os.remove(tmp)
            except OSError: pass


# ── landmarker lifecycle ──────────────────────────────────────────────────
def _build_landmarker(model_path: str):
    base = mp.tasks.BaseOptions(model_asset_path=model_path)
    opts = mp.tasks.vision.FaceLandmarkerOptions(
        base_options=base,
        running_mode=mp.tasks.vision.RunningMode.IMAGE,
        num_faces=1,
        min_face_detection_confidence=0.5,
        min_face_presence_confidence=0.5,
        min_tracking_confidence=0.5,
        output_face_blendshapes=True,
        output_facial_transformation_matrixes=True,
    )
    return mp.tasks.vision.FaceLandmarker.create_from_options(opts)


# ── per-frame extraction ──────────────────────────────────────────────────
def _euler_from_matrix(m: np.ndarray) -> tuple:
    """Decompose a 4x4 rotation-translation matrix to (yaw, pitch, roll) deg.

    Uses the ZYX (yaw-pitch-roll) Tait-Bryan convention. Handles gimbal
    lock by falling back to roll=0 when |sy| is near zero."""
    r = m[:3, :3]
    sy = math.sqrt(r[0, 0] ** 2 + r[1, 0] ** 2)
    if sy > 1e-6:
        yaw = math.degrees(math.atan2(r[1, 0], r[0, 0]))
        pitch = math.degrees(math.atan2(-r[2, 0], sy))
        roll = math.degrees(math.atan2(r[2, 1], r[2, 2]))
    else:
        yaw = math.degrees(math.atan2(-r[0, 1], r[1, 1]))
        pitch = math.degrees(math.atan2(-r[2, 0], sy))
        roll = 0.0
    return yaw, pitch, roll


def _blink_score_from_blendshapes(bs) -> float:
    """Mean of eyeBlinkLeft + eyeBlinkRight scores (each 0..1)."""
    left = right = 0.0
    for entry in bs:
        name = entry.category_name
        if name == 'eyeBlinkLeft':
            left = entry.score
        elif name == 'eyeBlinkRight':
            right = entry.score
    return 0.5 * (left + right)


# ── HumanScore-style aggregation ──────────────────────────────────────────
def _aggregate(frequency: float, severity: float, persistence: float) -> float:
    """Combine the three penalties into a single score in [0, 1].

    All inputs in [0, 1]: frequency = fraction of flagged samples,
    severity = normalised magnitude of flagged samples, persistence =
    longest_run / total_flagged. Returns 1 - weighted_sum so higher is
    better (more human-like)."""
    f = max(0.0, min(1.0, frequency))
    s = max(0.0, min(1.0, severity))
    p = max(0.0, min(1.0, persistence))
    penalty = 0.5 * f + 0.3 * s + 0.2 * p
    return max(0.0, min(1.0, 1.0 - penalty))


def _persistence(flags: np.ndarray) -> float:
    """Longest contiguous run of True / total True. 0 if no flags."""
    total = int(flags.sum())
    if total == 0:
        return 0.0
    longest = current = 0
    for v in flags:
        if v:
            current += 1
            if current > longest:
                longest = current
        else:
            current = 0
    return longest / float(total)


# ── scorer cores ──────────────────────────────────────────────────────────
def _score_head_jerk(yaws: np.ndarray, pitches: np.ndarray,
                     rolls: np.ndarray, fps: float) -> tuple:
    """Returns (score, details_dict)."""
    if len(yaws) < 5 or fps <= 0:
        return 1.0, {'reason': 'too_few_samples', 'n': len(yaws)}

    dt = 1.0 / fps
    # Stack into one (n,3) array for vectorised diffs.
    angles = np.stack([yaws, pitches, rolls], axis=1)
    # Unwrap each axis to avoid 360-deg discontinuities.
    angles = np.unwrap(np.deg2rad(angles), axis=0)
    angles = np.rad2deg(angles)

    vel = np.diff(angles, axis=0) / dt           # deg/s
    acc = np.diff(vel, axis=0) / dt              # deg/s^2
    jerk = np.diff(acc, axis=0) / dt             # deg/s^3
    jerk_mag = np.linalg.norm(jerk, axis=1)      # per-frame magnitude

    flagged = jerk_mag > JERK_FLAG_THRESHOLD
    frequency = float(flagged.mean())

    if flagged.any():
        # Log-normalised severity, capped at JERK_SEVERE_THRESHOLD.
        clipped = np.minimum(jerk_mag[flagged], JERK_SEVERE_THRESHOLD)
        # Map [FLAG, SEVERE] log-linear to [0, 1].
        lo, hi = math.log(JERK_FLAG_THRESHOLD), math.log(JERK_SEVERE_THRESHOLD)
        sev_vals = (np.log(clipped) - lo) / (hi - lo)
        severity = float(np.clip(sev_vals.mean(), 0.0, 1.0))
    else:
        severity = 0.0

    persistence = _persistence(flagged)

    score = _aggregate(frequency, severity, persistence)
    return score, {
        'n_samples': int(len(jerk_mag)),
        'jerk_mag_mean': float(jerk_mag.mean()),
        'jerk_mag_p95': float(np.percentile(jerk_mag, 95)),
        'jerk_mag_max': float(jerk_mag.max()),
        'frequency': frequency,
        'severity': severity,
        'persistence': persistence,
    }


def _detect_blinks(blink_signal: np.ndarray, fps: float,
                   threshold: float = DEFAULT_BLINK_BLENDSHAPE_THRESHOLD) -> list:
    """Return list of (start_frame, end_frame, duration_ms) for each blink."""
    above = blink_signal > threshold
    blinks = []
    i, n = 0, len(above)
    while i < n:
        if above[i]:
            j = i
            while j < n and above[j]:
                j += 1
            duration_ms = (j - i) * (1000.0 / fps) if fps > 0 else 0.0
            blinks.append((i, j - 1, duration_ms))
            i = j
        else:
            i += 1
    return blinks


def _score_blinks(blink_signal: np.ndarray, fps: float,
                  total_seconds: float) -> tuple:
    """Returns (score, details_dict)."""
    if total_seconds <= 0 or len(blink_signal) < 5:
        return 1.0, {'reason': 'too_short', 'seconds': total_seconds}

    blinks = _detect_blinks(blink_signal, fps)
    n_blinks = len(blinks)
    rate_per_min = n_blinks / (total_seconds / 60.0) if total_seconds > 0 else 0.0

    durations = np.array([b[2] for b in blinks]) if blinks else np.array([])

    # Penalty 1 — rate outside human prior.
    if rate_per_min < BLINK_RATE_PRIOR_MIN:
        rate_pen = min(1.0, (BLINK_RATE_PRIOR_MIN - rate_per_min) / BLINK_RATE_PRIOR_MIN)
    elif rate_per_min > BLINK_RATE_PRIOR_MAX:
        rate_pen = min(1.0, (rate_per_min - BLINK_RATE_PRIOR_MAX) / BLINK_RATE_PRIOR_MAX)
    else:
        rate_pen = 0.0

    # Penalty 2 — duration outside prior (averaged across blinks).
    if durations.size:
        out_of_range = np.where(
            durations < BLINK_DURATION_PRIOR_MIN_MS, 1.0,
            np.where(durations > BLINK_DURATION_PRIOR_MAX_MS, 1.0, 0.0)
        )
        dur_pen = float(out_of_range.mean())
        # Severity = how far the mean duration is outside the prior band.
        mean_dur = float(durations.mean())
        if mean_dur < BLINK_DURATION_PRIOR_MIN_MS:
            dur_sev = (BLINK_DURATION_PRIOR_MIN_MS - mean_dur) / BLINK_DURATION_PRIOR_MIN_MS
        elif mean_dur > BLINK_DURATION_PRIOR_MAX_MS:
            dur_sev = (mean_dur - BLINK_DURATION_PRIOR_MAX_MS) / BLINK_DURATION_PRIOR_MAX_MS
        else:
            dur_sev = 0.0
        dur_sev = min(1.0, dur_sev)
    else:
        dur_pen = 1.0 if rate_pen > 0 else 0.0
        dur_sev = 0.0

    # Persistence here doesn't carry the same meaning as for jerk — we
    # repurpose it as "blink-interval uniformity": near-zero IQR over a
    # long video is unnatural. Many gens produce blinks on a metronome.
    if len(blinks) >= 4:
        intervals = np.diff([b[0] for b in blinks]) / fps  # seconds
        iqr = float(np.percentile(intervals, 75) - np.percentile(intervals, 25))
        median_iv = float(np.median(intervals))
        # If IQR << median (very regular), penalise.
        if median_iv > 0:
            uniformity = max(0.0, 1.0 - (iqr / median_iv))
        else:
            uniformity = 0.0
    else:
        uniformity = 0.0

    frequency = max(rate_pen, dur_pen)
    severity = max(rate_pen, dur_sev)
    persistence = uniformity

    score = _aggregate(frequency, severity, persistence)
    return score, {
        'n_blinks': n_blinks,
        'rate_per_min': rate_per_min,
        'duration_mean_ms': float(durations.mean()) if durations.size else 0.0,
        'duration_min_ms': float(durations.min()) if durations.size else 0.0,
        'duration_max_ms': float(durations.max()) if durations.size else 0.0,
        'interval_uniformity': persistence,
        'seconds_analysed': total_seconds,
    }


# ── public entrypoint ─────────────────────────────────────────────────────
def score_face_kinematics(
    video_path,
    pass_threshold: float = DEFAULT_PASS_THRESHOLD,
    max_frames: int = DEFAULT_MAX_FRAMES,
    model_path: Optional[str] = None,
    verbose: bool = False,
) -> KinematicResult:
    """Score a video's facial kinematics. Returns KinematicResult.

    Failure modes (return passed=True conservatively):
      - MediaPipe model unavailable      → flag 'no_model'
      - Video unreadable                 → flag 'no_video'
      - No face detected in any frame    → flag 'no_face_detected'
    The gate is fail-closed on signal, fail-open on infrastructure."""
    video_path = str(video_path)

    resolved = _resolve_model_path(model_path)
    if resolved is None:
        return KinematicResult(
            overall=1.0, head_jerk=1.0, blink=1.0,
            passed=True, flags=['no_model'],
            details={'video_path': video_path},
        )

    cap = cv2.VideoCapture(video_path)
    if not cap.isOpened():
        return KinematicResult(
            overall=1.0, head_jerk=1.0, blink=1.0,
            passed=True, flags=['no_video'],
            details={'video_path': video_path},
        )

    src_fps = cap.get(cv2.CAP_PROP_FPS) or 30.0
    src_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)

    # Decimate if too long, to preserve consistent dt.
    if src_frames > max_frames and src_frames > 0:
        stride = max(1, src_frames // max_frames)
    else:
        stride = 1
    sample_fps = src_fps / stride

    yaws, pitches, rolls, blinks = [], [], [], []

    landmarker = None
    try:
        landmarker = _build_landmarker(resolved)
    except Exception as exc:
        cap.release()
        return KinematicResult(
            overall=1.0, head_jerk=1.0, blink=1.0,
            passed=True, flags=['landmarker_init_failed'],
            details={'video_path': video_path, 'error': str(exc)},
        )

    try:
        idx = 0
        sampled = 0
        with_face = 0
        while True:
            ret, frame_bgr = cap.read()
            if not ret:
                break
            if idx % stride == 0:
                sampled += 1
                rgb = cv2.cvtColor(frame_bgr, cv2.COLOR_BGR2RGB)
                mp_img = mp.Image(image_format=mp.ImageFormat.SRGB, data=rgb)
                try:
                    result = landmarker.detect(mp_img)
                except Exception:
                    result = None
                if (result and result.facial_transformation_matrixes
                        and result.face_blendshapes):
                    with_face += 1
                    matrix = np.array(result.facial_transformation_matrixes[0])
                    y, p, r = _euler_from_matrix(matrix)
                    yaws.append(y); pitches.append(p); rolls.append(r)
                    blinks.append(_blink_score_from_blendshapes(
                        result.face_blendshapes[0]))
                else:
                    # Mark a gap — interpolate later or just drop.
                    yaws.append(np.nan); pitches.append(np.nan)
                    rolls.append(np.nan); blinks.append(0.0)
            idx += 1
    finally:
        cap.release()
        try: landmarker.close()
        except Exception: pass

    yaws = np.array(yaws); pitches = np.array(pitches)
    rolls = np.array(rolls); blink_sig = np.array(blinks)

    valid = ~np.isnan(yaws)
    if valid.sum() < 5:
        return KinematicResult(
            overall=1.0, head_jerk=1.0, blink=1.0,
            passed=True, flags=['no_face_detected'],
            details={
                'video_path': video_path,
                'frames_sampled': int(sampled),
                'frames_with_face': int(with_face),
            },
        )

    # Drop gaps for jerk (require contiguous samples for finite diffs).
    yaws_v = yaws[valid]; pitches_v = pitches[valid]; rolls_v = rolls[valid]

    head_score, head_details = _score_head_jerk(
        yaws_v, pitches_v, rolls_v, sample_fps)

    total_seconds = sampled / sample_fps if sample_fps > 0 else 0.0
    blink_score, blink_details = _score_blinks(
        blink_sig, sample_fps, total_seconds)

    # Overall = arithmetic mean of the two heads. No weighting yet
    # since we lack calibration data on which is the stronger signal.
    overall = 0.5 * (head_score + blink_score)

    flags = []
    if head_score < pass_threshold: flags.append('head_jerk_fail')
    if blink_score < pass_threshold: flags.append('blink_fail')
    if overall < pass_threshold: flags.append('kinematic_overall_fail')

    res = KinematicResult(
        overall=overall,
        head_jerk=head_score,
        blink=blink_score,
        passed=overall >= pass_threshold,
        flags=flags,
        details={
            'video_path': video_path,
            'sample_fps': sample_fps,
            'frames_sampled': int(sampled),
            'frames_with_face': int(with_face),
            'pass_threshold': pass_threshold,
            'head_jerk': head_details,
            'blink': blink_details,
        },
    )

    if verbose:
        print_gate_banner(res)
    return res


# ── console banner for the gate ───────────────────────────────────────────
def print_gate_banner(res: KinematicResult, video_label: Optional[str] = None) -> None:
    """Loud, scannable status block for both passes and fails."""
    bar = '═' * 72
    title = 'KINEMATIC GATE — PASS' if res.passed else 'KINEMATIC GATE — REJECT'
    print()
    print(bar)
    print(f"  {title}")
    if video_label:
        print(f"  video: {video_label}")
    print(f"  overall   : {res.overall:.3f}  (threshold {res.details.get('pass_threshold', DEFAULT_PASS_THRESHOLD):.2f})")
    print(f"  head_jerk : {res.head_jerk:.3f}")
    print(f"  blink     : {res.blink:.3f}")
    if res.flags:
        print(f"  flags     : {', '.join(res.flags)}")
    hd = res.details.get('head_jerk', {})
    bd = res.details.get('blink', {})
    if 'jerk_mag_p95' in hd:
        print(f"  jerk p95  : {hd['jerk_mag_p95']:.1f} deg/s^3   "
              f"max: {hd['jerk_mag_max']:.1f}")
    if 'rate_per_min' in bd:
        print(f"  blinks    : {bd['n_blinks']} "
              f"({bd['rate_per_min']:.1f}/min)   "
              f"mean dur: {bd['duration_mean_ms']:.0f} ms")
    print(bar)
    print()


# ── CLI for ad-hoc scoring ────────────────────────────────────────────────
if __name__ == '__main__':
    import argparse, json
    ap = argparse.ArgumentParser(description="Score a video's facial kinematics.")
    ap.add_argument('video', help='Path to input video')
    ap.add_argument('--threshold', type=float, default=DEFAULT_PASS_THRESHOLD)
    ap.add_argument('--max-frames', type=int, default=DEFAULT_MAX_FRAMES)
    ap.add_argument('--model', default=None, help='Path to .task model')
    ap.add_argument('--json', action='store_true', help='Emit JSON only (no banner)')
    args = ap.parse_args()

    result = score_face_kinematics(
        args.video,
        pass_threshold=args.threshold,
        max_frames=args.max_frames,
        model_path=args.model,
        verbose=not args.json,
    )

    if args.json:
        print(json.dumps(asdict(result), indent=2, default=str))

    sys.exit(0 if result.passed else 2)
