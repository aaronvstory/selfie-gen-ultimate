#!/usr/bin/env python3
"""Persona pre-submission risk filter — combined bias-the-odds gate.

Derived from the versailles FAILED-vs-DASHERS corpus (see
versailles-fail-vs-pass.md). NOT a guaranteed pass predictor — a
*reject / regenerate* recommender that, on the labelled corpus,
classified 12/13 personas correctly (2/2 PASS, 10/11 FAIL; the single
miss, BRESLEY, carries a separate blink anomaly).

Three independently-motivated signals, all pointing the same way:

  1. Face-track continuity of the KLING SOURCE must be 100% (no
     dropout in the ~5-8s head-turn window). Strongest signal,
     zero false positives on the corpus.
  2. Source should NOT be outpaint-expanded (`_exp_` / `expanded`):
     0/2 PASS used it, 7/11 FAIL did.
  3. Oldcam should be gentle (v13/v15), NOT the aggressive v24 crush
     (v24 failed 4/4 and also degrades trackability).

Pure OpenCV + MediaPipe (reuses face_track_prefilter); no rPPG, repo-safe.

Usage:
    python docs/analysis/persona_prefilter.py KLING_SOURCE.mp4 \\
        [--delivered DELIVERED.mp4] [--oldcam-version 13]
    # exit 0 = looks OK to submit; 2 = high-risk, regenerate/adjust
"""
from __future__ import annotations

import argparse
import os
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from face_track_prefilter import track_continuity  # noqa: E402

REPO = Path(__file__).resolve().parents[2]
DEFAULT_MODEL = REPO / "face_landmarker.task"


def assess(kling_source: str, delivered: str | None,
           oldcam_version: int | None, model: str) -> dict:
    reasons: list[str] = []

    tc = track_continuity(kling_source, model)
    src_track = tc.get("track_pct")
    if "error" in tc:
        reasons.append(f"source track error: {tc['error']}")
    elif src_track is not None and src_track < 99.9:
        reasons.append(
            f"SOURCE face-track {src_track}% < 100% "
            f"(dropout, likely the 5-8s head-turn) — REGENERATE source"
        )

    name = os.path.basename(delivered or kling_source)
    if "_exp_" in name or "expanded" in name:
        reasons.append(
            "outpaint-expanded source (0/2 PASS used this) — "
            "prefer a non-expanded crop"
        )

    ver = oldcam_version
    if ver is None:
        m = re.search(r"oldcam-v(\d+)", name)
        ver = int(m.group(1)) if m else None
    if ver is not None and ver not in (13, 15):
        reasons.append(
            f"oldcam v{ver} — v24-class crush failed 4/4 and degrades "
            f"trackability; use gentle v13/v15"
        )

    return {
        "kling_source": kling_source,
        "src_track_pct": src_track,
        "oldcam_version": ver,
        "high_risk": bool(reasons),
        "reasons": reasons,
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("kling_source", help="the Kling source clip (pre-oldcam)")
    ap.add_argument("--delivered", default=None,
                    help="the final delivered clip (for name-based checks)")
    ap.add_argument("--oldcam-version", type=int, default=None)
    ap.add_argument("--model", default=str(DEFAULT_MODEL))
    args = ap.parse_args()

    os.environ.setdefault("MPLBACKEND", "Agg")
    r = assess(args.kling_source, args.delivered,
               args.oldcam_version, args.model)

    print(f"src_track={r['src_track_pct']}%  oldcam=v{r['oldcam_version']}")
    if r["high_risk"]:
        print("VERDICT: HIGH RISK — likely to FAIL Persona. Fix before submit:")
        for i, why in enumerate(r["reasons"], 1):
            print(f"  {i}. {why}")
        return 2
    print("VERDICT: clears the known FAIL signals (no guarantee — submit).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
