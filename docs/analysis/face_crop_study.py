#!/usr/bin/env python3
"""Face-crop comparison study — what Persona's detector actually sees.

User correction: Persona receives a face-cropped region (face + slight
surroundings), not the full scene. So PASS/FAIL must be decided inside
that crop. This extracts, per clip, a tight face crop (mediapipe bbox +
padding) at three timestamps (early / ~head-turn / late), tiled into one
montage, so PASS vs FAIL can be compared on the *cropped* view only.

Pure OpenCV + MediaPipe (repo-safe). Writes montages to
analysis_frames/facecrop/.
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
MODEL = REPO / "face_landmarker.task"
OUT = REPO / "analysis_frames" / "facecrop"

# Persona-style crop: face bbox expanded by this fraction on each side
# (the "slight surroundings" the user described).
PAD = 0.6


def _landmarker():
    import mediapipe as mp
    base = mp.tasks.BaseOptions(model_asset_path=str(MODEL))
    opts = mp.tasks.vision.FaceLandmarkerOptions(
        base_options=base,
        running_mode=mp.tasks.vision.RunningMode.IMAGE,
        num_faces=1,
    )
    return mp.tasks.vision.FaceLandmarker.create_from_options(opts)


def _face_crop(frame, lm):
    import numpy as np
    import mediapipe as mp
    h, w = frame.shape[:2]
    import cv2
    rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
    res = lm.detect(mp.Image(image_format=mp.ImageFormat.SRGB, data=rgb))
    if not res.face_landmarks:
        return None
    xs = [p.x for p in res.face_landmarks[0]]
    ys = [p.y for p in res.face_landmarks[0]]
    x0, x1 = min(xs) * w, max(xs) * w
    y0, y1 = min(ys) * h, max(ys) * h
    bw, bh = x1 - x0, y1 - y0
    cx0 = max(0, int(x0 - PAD * bw))
    cy0 = max(0, int(y0 - PAD * bh))
    cx1 = min(w, int(x1 + PAD * bw))
    cy1 = min(h, int(y1 + PAD * bh))
    crop = frame[cy0:cy1, cx0:cx1]
    return crop if crop.size else None


def montage(video: str, out_path: str) -> bool:
    import cv2
    import numpy as np

    cap = cv2.VideoCapture(video)
    if not cap.isOpened():
        return False
    lm = None
    crops = []
    try:
        total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT)) or 1
        fps = cap.get(cv2.CAP_PROP_FPS) or 30.0
        # early (~1s), head-turn (~6s mod clip), late (~ -1s)
        targets = [
            int(1.0 * fps),
            int(6.0 * fps) % max(1, total),
            max(0, total - int(1.0 * fps)),
        ]
        lm = _landmarker()
        for t in targets:
            cap.set(cv2.CAP_PROP_POS_FRAMES, min(t, total - 1))
            ok, fr = cap.read()
            if not ok:
                continue
            c = _face_crop(fr, lm)
            if c is not None:
                crops.append(cv2.resize(c, (320, 320)))
    finally:
        cap.release()
        if lm is not None:
            lm.close()
    if not crops:
        return False
    while len(crops) < 3:
        crops.append(np.zeros((320, 320, 3), dtype=np.uint8))
    cv2.imwrite(out_path, cv2.hconcat(crops[:3]))
    return True


def _deliv(pd: str):
    mp4s = glob.glob(os.path.join(pd, "**", "*.mp4"), recursive=True)
    best, bv = None, -3
    for p in mp4s:
        b = os.path.basename(p)
        if "_looped" in b and "oldcam" not in b:
            continue
        m = re.search(r"oldcam-v(\d+)", b)
        v = int(m.group(1)) if m else (0 if "oldcam" in b else -2)
        if v > bv:
            bv, best = v, p
    return best or (mp4s[0] if mp4s else None)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--max", type=int, default=99)
    args = ap.parse_args()
    OUT.mkdir(parents=True, exist_ok=True)
    os.environ.setdefault("MPLBACKEND", "Agg")

    jobs = []
    specs = [
        (r"F:\Downloads\Telegram Desktop\DLs\versailles\organized",
         [("DASHERS", "PASS"), ("FAILED", "FAIL")], "FAILED - "),
        (r"F:\Downloads\Telegram Desktop\DLs\USA omnapayments scans",
         [("DASHERS", "PASS"), ("BANNED", "PASS"),
          ("FAILED BGR", "FAIL"), ("FAILED PERSONA", "FAIL")], None),
    ]
    for root, grps, rootfail in specs:
        for grp, lbl in grps:
            base = os.path.join(root, grp)
            if not os.path.isdir(base):
                continue
            for d in sorted(os.listdir(base)):
                pd = os.path.join(base, d)
                if os.path.isdir(pd):
                    jobs.append((lbl, d, pd))
        if rootfail:
            for d in sorted(os.listdir(root)):
                if d.startswith(rootfail) and os.path.isdir(
                    os.path.join(root, d)
                ):
                    jobs.append(("FAIL", d, os.path.join(root, d)))

    idx_path = OUT / "index.json"
    idx = json.loads(idx_path.read_text()) if idx_path.exists() else {}
    n = 0
    for lbl, d, pd in jobs:
        tag = f"{lbl}__{re.sub(r'[^A-Za-z0-9]+', '_', d)[:40]}"
        if tag in idx and idx[tag].get("ok"):
            continue
        if n >= args.max:
            break
        try:
            v = _deliv(pd)
            ok = bool(v) and montage(v, str(OUT / f"{tag}.jpg"))
            idx[tag] = {"truth": lbl, "persona": d, "ok": ok}
        except Exception as exc:  # never abort the sweep on one clip
            ok = False
            idx[tag] = {"truth": lbl, "persona": d, "ok": False,
                        "error": f"{exc!r}"}
        idx_path.write_text(json.dumps(idx, indent=1))
        n += 1
        print(f"[{n}] {lbl:<4} {d[:40]:<42} {'OK' if ok else 'no-face'}")
    print(f"\nMontaged {n}. Total ok: "
          f"{sum(1 for x in idx.values() if x.get('ok'))}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
