#!/usr/bin/env python3
"""Face-tracking-continuity pre-filter — the one usable versailles signal.

Empirical finding (docs/analysis/versailles_fail_vs_pass.md): on the
labelled corpus, **every Persona PASS held a detectable face in 100% of
sampled frames; every clip with a tracking dropout (<100%) was a FAIL**,
and the dropout was already present in the original Kling source. This is
a zero-false-positive *reject* signal: it does not turn a fail into a
pass, but it cheaply catches clips that cannot pass — before any oldcam
or Persona attempt is spent.

This tool is pure OpenCV + MediaPipe FaceLandmarker (no rPPG, not derived
from the friend's tool) so it is safe to keep in the repo and wire into
the pipeline as an upstream gate.

Usage:
    python docs/analysis/face_track_prefilter.py VIDEO [VIDEO ...]
    python docs/analysis/face_track_prefilter.py --json DIR   # scan a tree
    # exit code: 0 = all clips track >= --min-track (default 100%),
    #            2 = at least one clip below threshold (reject/regenerate)
"""
from __future__ import annotations

import argparse
import glob
import json
import os
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
DEFAULT_MODEL = REPO / "face_landmarker.task"
DEFAULT_SAMPLE_FPS = 8.0       # enough to catch multi-frame dropouts, fast
DEFAULT_MIN_TRACK = 100.0      # the corpus boundary: PASS == 100%


def _build_landmarker(model_path: str):
    import mediapipe as mp
    base = mp.tasks.BaseOptions(model_asset_path=model_path)
    opts = mp.tasks.vision.FaceLandmarkerOptions(
        base_options=base,
        running_mode=mp.tasks.vision.RunningMode.IMAGE,
        num_faces=1,
    )
    return mp.tasks.vision.FaceLandmarker.create_from_options(opts)


def track_continuity(video: str, model_path: str,
                     sample_fps: float = DEFAULT_SAMPLE_FPS) -> dict:
    """Return {sampled, with_face, track_pct, longest_gap_s} or {error}."""
    import cv2
    import mediapipe as mp

    if not os.path.exists(model_path):
        return {"error": f"model missing: {model_path}"}
    cap = cv2.VideoCapture(video)
    if not cap.isOpened():
        return {"error": "cannot open video"}
    src_fps = cap.get(cv2.CAP_PROP_FPS) or 30.0
    step = max(1, int(round(src_fps / sample_fps)))

    lm = _build_landmarker(model_path)
    sampled = with_face = 0
    cur_gap = longest_gap = 0
    idx = 0
    while True:
        ok, frame = cap.read()
        if not ok:
            break
        if idx % step == 0:
            sampled += 1
            rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
            mp_img = mp.Image(image_format=mp.ImageFormat.SRGB, data=rgb)
            res = lm.detect(mp_img)
            if res.face_landmarks:
                with_face += 1
                cur_gap = 0
            else:
                cur_gap += 1
                longest_gap = max(longest_gap, cur_gap)
        idx += 1
    cap.release()
    lm.close()
    if sampled == 0:
        return {"error": "no frames"}
    pct = round(100.0 * with_face / sampled, 2)
    return {
        "sampled": sampled,
        "with_face": with_face,
        "track_pct": pct,
        "longest_gap_s": round(longest_gap / sample_fps, 2),
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("paths", nargs="+", help="video file(s) or, with --scan, a dir")
    ap.add_argument("--scan", action="store_true",
                    help="treat paths as dirs; recurse for *.mp4")
    ap.add_argument("--model", default=str(DEFAULT_MODEL))
    ap.add_argument("--sample-fps", type=float, default=DEFAULT_SAMPLE_FPS)
    ap.add_argument("--min-track", type=float, default=DEFAULT_MIN_TRACK,
                    help="reject below this track%% (default 100 = corpus boundary)")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    videos: list[str] = []
    for p in args.paths:
        if args.scan and os.path.isdir(p):
            videos += sorted(glob.glob(os.path.join(p, "**", "*.mp4"),
                                       recursive=True))
        else:
            videos.append(p)

    os.environ.setdefault("MPLBACKEND", "Agg")
    out = []
    any_reject = False
    for v in videos:
        r = track_continuity(v, args.model, args.sample_fps)
        verdict = "ERROR"
        if "track_pct" in r:
            ok = r["track_pct"] >= args.min_track
            verdict = "OK" if ok else "REJECT"
            any_reject |= not ok
        out.append({"video": v, "verdict": verdict, **r})
        if not args.json:
            tp = r.get("track_pct", "?")
            extra = (f"  gap<= {r['longest_gap_s']}s"
                     if "longest_gap_s" in r else f"  {r.get('error', '')}")
            print(f"[{verdict:<6}] track={tp}%  {os.path.basename(v)}{extra}")

    if args.json:
        print(json.dumps(out, indent=1))
    return 2 if any_reject else 0


if __name__ == "__main__":
    raise SystemExit(main())
