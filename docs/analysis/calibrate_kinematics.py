#!/usr/bin/env python3
"""Full-corpus kinematic calibration (the friend's MAIN directive).

Per the rPPG tool author: Persona gates on geometric motion/kinematics,
NOT the rPPG pulse. rppg_injector's temporal_consistency / motion_artifacts
are pulse-SNR-derived (see compute_segmented_snr) — they are NOT the
post-processing-surviving signal. The genuinely geometric signal is
``face_kinematics`` (head-pose angular jerk + blink interval/duration),
which is fast (~10-15s/clip) so the WHOLE labelled corpus fits one run.

Scores EVERY persona's delivered clip (v24-if-present else v13 rule) AND
its original Kling source AND its pre-oldcam looped clip, so we can see
where (if anywhere) the kinematic signal separates Persona PASS vs FAIL,
and whether it's the source or the oldcam pass that moves it.

Resumable + bounded; accumulates to analysis_frames/kinematic_results.json.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
RPPG = REPO / "rPPG"
MODEL = REPO / "face_landmarker.task"
ART = REPO / "analysis_frames"
DATASET = ART / "dataset.json"
RESULTS = ART / "kinematic_results.json"


def _score(video: str) -> dict:
    sys.path.insert(0, str(RPPG))
    if MODEL.exists():
        os.environ["MEDIAPIPE_FACE_LANDMARKER_MODEL"] = str(MODEL)
    os.environ["MPLBACKEND"] = "Agg"
    try:
        from face_kinematics import score_face_kinematics
        r = score_face_kinematics(video)
        d = r.details if isinstance(r.details, dict) else {}
        return {
            "overall": round(r.overall, 4),
            "head_jerk": round(r.head_jerk, 4),
            "blink": round(r.blink, 4),
            "passed": r.passed,
            "flags": ";".join(r.flags),
            "details": d,
        }
    except Exception as exc:  # pragma: no cover
        return {"error": str(exc)[:160]}


def _load(p: Path, default):
    return json.loads(p.read_text()) if p.exists() else default


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

    queue = []
    for d in dataset:
        for role in ("delivered", "looped", "kling"):
            v = d.get(role)
            if v and f"{d['persona']}::{role}" not in results:
                queue.append((f"{d['persona']}::{role}", d, role, v))

    done = 0
    for key, d, role, v in queue:
        if done >= args.max:
            break
        if not os.path.exists(v):
            results[key] = {"truth": d["truth"], "role": role,
                            "error": "missing"}
        else:
            t0 = time.time()
            sc = _score(v)
            results[key] = {
                "truth": d["truth"], "persona": d["persona"], "role": role,
                "deliv_ver": d.get("deliv_ver"),
                "video": os.path.basename(v), "secs": round(time.time() - t0, 1),
                **sc,
            }
            print(f"[{done + 1}] {d['truth']:<4} {d['persona'][:26]:<27}"
                  f"{role:<10} overall={sc.get('overall')} "
                  f"jerk={sc.get('head_jerk')} blink={sc.get('blink')} "
                  f"flags={sc.get('flags', '')}")
        RESULTS.write_text(json.dumps(results, indent=1))
        done += 1

    remaining = sum(
        1 for d in dataset for role in ("delivered", "looped", "kling")
        if d.get(role) and f"{d['persona']}::{role}" not in results
    )
    print(f"\nScored {done} this run. Remaining {remaining}.")
    if remaining == 0:
        print("\n=== ALL SCORED ===")
        _report(results)
    return 0


def _f(v):
    return f"{v:.3f}" if isinstance(v, (int, float)) else str(v)


def _report(results: dict):
    rows = [r for r in results.values() if "overall" in r]
    rows.sort(key=lambda r: (r["truth"], r["role"], r["persona"]))
    print(f"\n{'truth':<5}{'role':<10}{'persona':<28}{'ver':>4}"
          f"{'overall':>9}{'jerk':>8}{'blink':>8}  flags")
    for r in rows:
        print(f"{r['truth']:<5}{r['role']:<10}{r['persona'][:26]:<28}"
              f"{('v'+str(r['deliv_ver'])) if r.get('deliv_ver') else '-':>4}"
              f"{_f(r['overall']):>9}{_f(r['head_jerk']):>8}"
              f"{_f(r['blink']):>8}  {r.get('flags', '')}")

    print("\n--- separation by role (FAIL vs PASS) ---")
    for role in ("delivered", "looped", "kling"):
        for axis in ("overall", "head_jerk", "blink"):
            f = [r[axis] for r in rows if r["truth"] == "FAIL"
                 and r["role"] == role and isinstance(r.get(axis), (int, float))]
            p = [r[axis] for r in rows if r["truth"] == "PASS"
                 and r["role"] == role and isinstance(r.get(axis), (int, float))]
            if f and p:
                clean = max(p) < min(f) or max(f) < min(p)
                tag = "** SEPARATES **" if clean else "overlap"
                print(f"  {role:<10}{axis:<11} "
                      f"FAIL[{min(f):.3f}..{max(f):.3f}] "
                      f"PASS[{min(p):.3f}..{max(p):.3f}]  {tag}")


if __name__ == "__main__":
    raise SystemExit(main())
