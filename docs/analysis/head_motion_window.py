#!/usr/bin/env python3
"""Windowed head-pose instability — the unexplored discriminator angle.

Established: face-track dropout in the ~5-8s head-turn window predicts
FAIL. But that is *binary* (face detected y/n). The 7 clean-tracking
FAILs hold a face 100% of frames yet still fail Persona. Open question:
is their head MOTION unstable in that window even though the face is
detectable?

This computes per-frame head pose (yaw/pitch/roll) from MediaPipe face
landmarks (pure geometry, no rPPG — repo-safe), then the angular jerk
(3rd derivative) inside the 5-8s turn window vs the rest of the clip.
Hypothesis: PASS clips have low, smooth head motion through the turn;
FAIL clips spike jerk there even when the face stays trackable.

Resumable; accumulates to analysis_frames/head_motion_results.json.
"""
from __future__ import annotations

import argparse
import json
import math
import os
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
MODEL = REPO / "face_landmarker.task"
ART = REPO / "analysis_frames"
DATASET = ART / "dataset.json"
RESULTS = ART / "head_motion_results.json"

TURN_START_S = 5.0
TURN_END_S = 8.0


def _pose_from_landmarks(lm, w: int, h: int):
    """Crude yaw/pitch/roll (deg) from canonical face landmarks.

    Uses nose tip (1), eye corners (33,263), mouth corners (61,291) —
    enough for relative angular *motion* (we only need derivatives).
    """
    def p(i):
        l = lm[i]
        return (l.x * w, l.y * h, l.z * w)

    nose = p(1)
    le, re = p(33), p(263)
    ml, mr = p(61), p(291)
    # roll: eye line tilt
    roll = math.degrees(math.atan2(re[1] - le[1], re[0] - le[0]))
    # yaw: nose horizontal offset from eye midpoint, normalised by eye span
    eye_mid_x = (le[0] + re[0]) / 2.0
    eye_span = max(1.0, math.hypot(re[0] - le[0], re[1] - le[1]))
    yaw = math.degrees(math.atan2(nose[0] - eye_mid_x, eye_span))
    # pitch: nose vertical offset from eye->mouth midline
    mouth_mid_y = (ml[1] + mr[1]) / 2.0
    eye_mid_y = (le[1] + re[1]) / 2.0
    face_h = max(1.0, mouth_mid_y - eye_mid_y)
    pitch = math.degrees(math.atan2(nose[1] - eye_mid_y, face_h))
    return yaw, pitch, roll


def _jerk(seq, fps):
    """Mean |3rd derivative| of an angle sequence (deg/s^3)."""
    if len(seq) < 4:
        return 0.0
    import numpy as np
    a = np.asarray(seq, dtype=np.float64)
    d3 = np.diff(a, 3) * (fps ** 3)
    return float(np.mean(np.abs(d3)))


def analyze(video: str, model: str) -> dict:
    import cv2
    import mediapipe as mp

    if not os.path.exists(model):
        return {"error": "model missing"}
    cap = cv2.VideoCapture(video)
    if not cap.isOpened():
        return {"error": "cannot open"}
    fps = cap.get(cv2.CAP_PROP_FPS) or 30.0
    w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
    h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))

    base = mp.tasks.BaseOptions(model_asset_path=model)
    opts = mp.tasks.vision.FaceLandmarkerOptions(
        base_options=base,
        running_mode=mp.tasks.vision.RunningMode.IMAGE,
        num_faces=1,
    )
    lm = mp.tasks.vision.FaceLandmarker.create_from_options(opts)

    sample_fps = 12.0
    step = max(1, int(round(fps / sample_fps)))
    win_yaw, win_pitch, all_yaw = [], [], []
    rest_yaw = []
    idx = 0
    while True:
        ok, frame = cap.read()
        if not ok:
            break
        if idx % step == 0:
            t = idx / fps
            rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
            res = lm.detect(mp.Image(image_format=mp.ImageFormat.SRGB,
                                     data=rgb))
            if res.face_landmarks:
                y, pi, _roll = _pose_from_landmarks(
                    res.face_landmarks[0], w, h)
                all_yaw.append(y)
                # the looped 20s clips repeat the 10s defect at +10s too
                tm = t % 10.0
                if TURN_START_S <= tm <= TURN_END_S:
                    win_yaw.append(y)
                    win_pitch.append(pi)
                else:
                    rest_yaw.append(y)
        idx += 1
    cap.release()
    lm.close()

    if len(win_yaw) < 4:
        return {"error": "insufficient window samples",
                "n_win": len(win_yaw)}
    return {
        "fps": round(fps, 2),
        "n_window": len(win_yaw),
        "n_rest": len(rest_yaw),
        "yaw_jerk_window": round(_jerk(win_yaw, sample_fps), 1),
        "pitch_jerk_window": round(_jerk(win_pitch, sample_fps), 1),
        "yaw_jerk_rest": round(_jerk(rest_yaw, sample_fps), 1),
        "yaw_jerk_ratio": round(
            _jerk(win_yaw, sample_fps)
            / (_jerk(rest_yaw, sample_fps) + 1e-6), 2),
    }


def _load(p, d):
    return json.loads(p.read_text()) if p.exists() else d


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--max", type=int, default=99)
    ap.add_argument("--report", action="store_true")
    args = ap.parse_args()

    dataset = _load(DATASET, [])
    results = _load(RESULTS, {})

    if args.report:
        _report(results)
        return 0

    os.environ.setdefault("MPLBACKEND", "Agg")
    if MODEL.exists():
        os.environ["MEDIAPIPE_FACE_LANDMARKER_MODEL"] = str(MODEL)

    queue = [(f"{d['persona']}::{role}", d, role, d.get(role))
             for d in dataset for role in ("delivered", "kling")
             if d.get(role) and f"{d['persona']}::{role}" not in results]

    done = 0
    for key, d, role, v in queue:
        if done >= args.max:
            break
        r = analyze(v, str(MODEL)) if os.path.exists(v) else {"error": "missing"}
        results[key] = {"truth": d["truth"], "persona": d["persona"],
                        "role": role, "deliv_ver": d.get("deliv_ver"), **r}
        RESULTS.write_text(json.dumps(results, indent=1))
        done += 1
        print(f"[{done}] {d['truth']:<4} {d['persona'][:24]:<25}{role:<10}"
              f"yaw_jerk_win={r.get('yaw_jerk_window')} "
              f"ratio={r.get('yaw_jerk_ratio')} {r.get('error', '')}")

    remaining = sum(1 for d in dataset for role in ("delivered", "kling")
                    if d.get(role)
                    and f"{d['persona']}::{role}" not in results)
    print(f"\nScored {done}. Remaining {remaining}.")
    if remaining == 0:
        _report(results)
    return 0


def _f(v):
    return f"{v:.1f}" if isinstance(v, (int, float)) else str(v)


def _report(results):
    rows = [r for r in results.values() if "yaw_jerk_window" in r]
    rows.sort(key=lambda r: (r["truth"], r["role"], r["persona"]))
    print(f"\n{'truth':<5}{'role':<10}{'persona':<26}"
          f"{'yaw_jerk_w':>11}{'pitch_jw':>10}{'jerk_rest':>10}{'ratio':>7}")
    for r in rows:
        print(f"{r['truth']:<5}{r['role']:<10}{r['persona'][:24]:<26}"
              f"{_f(r['yaw_jerk_window']):>11}{_f(r['pitch_jerk_window']):>10}"
              f"{_f(r['yaw_jerk_rest']):>10}{_f(r['yaw_jerk_ratio']):>7}")
    for role in ("delivered", "kling"):
        for ax in ("yaw_jerk_window", "yaw_jerk_ratio"):
            f = [r[ax] for r in rows if r["truth"] == "FAIL"
                 and r["role"] == role and isinstance(r.get(ax), (int, float))]
            p = [r[ax] for r in rows if r["truth"] == "PASS"
                 and r["role"] == role and isinstance(r.get(ax), (int, float))]
            if f and p:
                clean = max(p) < min(f) or max(f) < min(p)
                print(f"  {role:<10}{ax:<16} "
                      f"FAIL[{min(f):.1f}..{max(f):.1f}] "
                      f"PASS[{min(p):.1f}..{max(p):.1f}]  "
                      f"{'** SEPARATES **' if clean else 'overlap'}")


if __name__ == "__main__":
    raise SystemExit(main())
