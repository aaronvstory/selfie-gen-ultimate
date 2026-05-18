#!/usr/bin/env python3
"""Resumable liveness-metric calibration harness for the versailles corpus.

Runs the friend's rPPG analyzer (``rPPG/rppg_injector.py --analyze``) +
``face_kinematics`` over the labelled FAILED/DASHER set and accumulates the
five liveness metrics (SNR / phase coherence / temporal consistency / motion
artifacts / harmonic alignment) plus the kinematic gate (head jerk / blink),
for BOTH the delivered video (v24-if-present else v13 rule) AND the original
Kling source.

Goal: find which metric/threshold actually separates Persona pass vs fail.

- Resumable: results appended to results.json; already-scored clips skipped.
- Bounded: ``--max N`` scores at most N *clips* per run so it fits a 10-min
  loop iteration (rPPG analyze is ~3 min/clip, CPU-only here).
- The rPPG tool stays gitignored; THIS harness (repo-side) is committable.

Usage:
    python docs/analysis/calibrate_liveness.py --max 3        # one loop slice
    python docs/analysis/calibrate_liveness.py --report       # print table only
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
RPPG = REPO / "rPPG"
MODEL = REPO / "face_landmarker.task"
ART = REPO / "analysis_frames"
DATASET = ART / "dataset.json"
RESULTS = ART / "calibration_results.json"

_METRIC_RE = {
    "snr": re.compile(r"Global SNR:\s*([-\d.]+)\s*dB"),
    "phase": re.compile(r"Phase Coherence:\s*([-\d.]+)\s*deg"),
    "temporal": re.compile(r"Temporal Consistency:\s*([-\d.]+)"),
    "motion": re.compile(r"Motion Artifacts:\s*([-\d.]+)"),
    "harmonic": re.compile(r"Harmonic Alignment:\s*([-\d.]+)"),
    "result": re.compile(r"Test Result:\s*(\w+)"),
}


def _parse_analyze(text: str) -> dict:
    out: dict = {}
    for key, rx in _METRIC_RE.items():
        m = rx.search(text)
        if m:
            g = m.group(1)
            out[key] = g if key == "result" else float(g)
    return out


def _run_analyze(video: str, timeout: int = 600) -> dict:
    """rppg_injector --analyze (read-only). Returns parsed metrics or {}."""
    env = dict(os.environ)
    if MODEL.exists():
        env["MEDIAPIPE_FACE_LANDMARKER_MODEL"] = str(MODEL)
    py = REPO / "venv" / "Scripts" / "python.exe"
    try:
        r = subprocess.run(
            [str(py), "rppg_injector.py", video, "--analyze",
             "--skip-kinematic-gate"],
            cwd=str(RPPG), env=env, capture_output=True, text=True,
            timeout=timeout,
        )
        return _parse_analyze(r.stdout + "\n" + r.stderr)
    except subprocess.TimeoutExpired:
        return {"error": "timeout"}
    except Exception as exc:  # pragma: no cover - defensive
        return {"error": str(exc)[:120]}


def _run_kinematic(video: str) -> dict:
    """face_kinematics.score_face_kinematics — head jerk + blink gate."""
    sys.path.insert(0, str(RPPG))
    if MODEL.exists():
        os.environ["MEDIAPIPE_FACE_LANDMARKER_MODEL"] = str(MODEL)
    try:
        from face_kinematics import score_face_kinematics
        r = score_face_kinematics(video)
        return {
            "kin_overall": round(r.overall, 4),
            "kin_jerk": round(r.head_jerk, 4),
            "kin_blink": round(r.blink, 4),
            "kin_flags": ";".join(r.flags),
        }
    except Exception as exc:
        return {"kin_error": str(exc)[:120]}


def load(path: Path, default):
    if path.exists():
        return json.loads(path.read_text())
    return default


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--max", type=int, default=3,
                    help="max clips to analyze this run (loop budget)")
    ap.add_argument("--report", action="store_true",
                    help="print the accumulated table and exit")
    ap.add_argument("--timeout", type=int, default=600)
    args = ap.parse_args()

    dataset = load(DATASET, [])
    results = load(RESULTS, {})

    if args.report:
        _report(dataset, results)
        return 0

    # Build the work queue: each persona contributes a "delivered" and a
    # "kling" clip. key = f"{persona}::{role}".
    queue = []
    for d in dataset:
        for role in ("delivered", "kling"):
            v = d.get(role)
            if not v:
                continue
            key = f"{d['persona']}::{role}"
            if key in results and "metrics" in results[key]:
                continue
            queue.append((key, d, role, v))

    done = 0
    for key, d, role, v in queue:
        if done >= args.max:
            break
        if not os.path.exists(v):
            results[key] = {"truth": d["truth"], "role": role,
                            "error": "missing file"}
            continue
        t0 = time.time()
        metrics = _run_analyze(v, timeout=args.timeout)
        kin = _run_kinematic(v)
        results[key] = {
            "truth": d["truth"],
            "persona": d["persona"],
            "role": role,
            "deliv_ver": d.get("deliv_ver"),
            "video": os.path.basename(v),
            "metrics": metrics,
            "kinematic": kin,
            "secs": round(time.time() - t0, 1),
        }
        RESULTS.write_text(json.dumps(results, indent=1))
        done += 1
        print(f"[{done}/{args.max}] {key} -> "
              f"{metrics.get('result', metrics.get('error', '?'))} "
              f"temporal={metrics.get('temporal')} "
              f"phase={metrics.get('phase')} "
              f"kin={kin.get('kin_overall')} ({results[key]['secs']}s)")

    remaining = sum(
        1 for d in dataset for role in ("delivered", "kling")
        if d.get(role) and f"{d['persona']}::{role}" not in results
    )
    print(f"\nScored this run: {done}. Remaining: {remaining}.")
    if remaining == 0:
        print("\n=== ALL CLIPS SCORED ===")
        _report(dataset, results)
    return 0


def _fmt(v):
    return f"{v:.3f}" if isinstance(v, (int, float)) else str(v)


def _report(dataset, results):
    rows = []
    for key, r in results.items():
        if "metrics" not in r:
            continue
        m, k = r["metrics"], r.get("kinematic", {})
        rows.append((
            r["truth"], r["role"], r.get("persona", "?")[:24],
            r.get("deliv_ver"),
            m.get("temporal"), m.get("phase"), m.get("motion"),
            m.get("harmonic"), m.get("snr"), m.get("result"),
            k.get("kin_overall"), k.get("kin_jerk"),
        ))
    rows.sort(key=lambda x: (x[0], x[1], x[2]))
    hdr = ("truth", "role", "persona", "ver", "temporal", "phase",
           "motion", "harm", "snr", "rppg", "kin", "jerk")
    print("\n" + " ".join(f"{h:>9}" for h in hdr))
    for row in rows:
        print(" ".join(f"{_fmt(c):>9}" for c in row))

    # Separation summary per metric (FAIL vs PASS, delivered role only)
    print("\n--- delivered-clip metric ranges (the actual question) ---")
    for mi, name in ((4, "temporal"), (5, "phase"), (6, "motion"),
                      (7, "harmonic"), (10, "kin_overall"), (11, "kin_jerk")):
        f = [r[mi] for r in rows if r[0] == "FAIL" and r[1] == "delivered"
             and isinstance(r[mi], (int, float))]
        p = [r[mi] for r in rows if r[0] == "PASS" and r[1] == "delivered"
             and isinstance(r[mi], (int, float))]
        if f and p:
            sep = "SEPARATES" if (max(p) < min(f) or max(f) < min(p)) else "overlap"
            print(f"  {name:<12} FAIL[{min(f):.3f}..{max(f):.3f}] "
                  f"PASS[{min(p):.3f}..{max(p):.3f}]  {sep}")


if __name__ == "__main__":
    raise SystemExit(main())
