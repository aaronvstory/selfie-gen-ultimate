"""Analyze sourav_kinematic_results.json — does ANY kinematic metric
separate Persona PASS from FAIL on the large corpus?

Honest stats. For each metric: PASS vs FAIL distribution, overlap, and
the best achievable separation (Youden-style sweep) with its false-pos
cost. Run AFTER measure_sourav_kinematics.py finishes.
"""
from __future__ import annotations

import json
import statistics as st
from collections import defaultdict
from pathlib import Path

_REPO = Path(__file__).resolve().parent.parent.parent
RESULTS = _REPO / "docs" / "analysis" / "sourav_kinematic_results.json"

# (key, human label, direction): direction = 'low_is_fail' means a LOW
# value indicates FAIL (reject if value < T); 'high_is_fail' the reverse.
METRICS = [
    ("kin_overall", "kinematic overall score", "low_is_fail"),
    ("kin_head_jerk", "head-jerk sub-score", "low_is_fail"),
    ("kin_blink", "blink sub-score", "low_is_fail"),
    ("jerk_mag_mean", "raw jerk magnitude mean", "high_is_fail"),
    ("jerk_mag_p95", "raw jerk magnitude p95", "high_is_fail"),
    ("jerk_mag_max", "raw jerk magnitude max", "high_is_fail"),
    ("blink_dur_mean_ms", "blink duration mean (ms)", "none"),
]


def describe(xs):
    if not xs:
        return "n=0"
    xs = sorted(xs)
    def p(q):
        return xs[max(0, min(len(xs) - 1, int(round(q * (len(xs) - 1)))))]
    return (f"n={len(xs)} min={min(xs):.3g} p10={p(.1):.3g} "
            f"med={st.median(xs):.3g} p90={p(.9):.3g} max={max(xs):.3g} "
            f"mean={st.mean(xs):.3g}")


def best_split(P, F, direction):
    """Sweep every candidate threshold; report the one maximising
    (FAIL recall - PASS false-pos rate) plus the zero-false-pos best."""
    if not P or not F:
        return None
    cands = sorted(set([round(v, 4) for v in P + F]))
    best = None          # max Youden J
    best_zerofp = None    # max recall with 0 PASS rejected
    for t in cands:
        if direction == "low_is_fail":
            fr = sum(1 for v in F if v < t)
            pr = sum(1 for v in P if v < t)
        else:  # high_is_fail
            fr = sum(1 for v in F if v > t)
            pr = sum(1 for v in P if v > t)
        rec = fr / len(F)
        fp = pr / len(P)
        J = rec - fp
        if best is None or J > best[0]:
            best = (J, t, fr, pr, rec, fp)
        if pr == 0 and fr > 0 and (best_zerofp is None or fr > best_zerofp[2]):
            best_zerofp = (J, t, fr, pr, rec, fp)
    return best, best_zerofp


def main() -> None:
    recs = json.loads(RESULTS.read_text(encoding="utf-8"))
    ok = [r for r in recs if r.get("ok")]
    bad = [r for r in recs if not r.get("ok")]
    print("=" * 74)
    print("SOURAV VAI CORPUS — KINEMATIC METRIC ANALYSIS (Kling source only)")
    print("=" * 74)
    print(f"records={len(recs)} measured={len(ok)} errored={len(bad)}")
    if bad:
        es = defaultdict(int)
        for r in bad:
            es[str(r.get('error'))[:70]] += 1
        for k, v in sorted(es.items(), key=lambda x: -x[1]):
            print(f"   {v:>3}x {k}")

    nP = sum(1 for r in ok if r["label"] == "PASS")
    nF = sum(1 for r in ok if r["label"] == "FAIL")
    print(f"\nlabelled: PASS n={nP}  FAIL n={nF}\n")

    for key, lbl, direction in METRICS:
        P = [r[key] for r in ok if r["label"] == "PASS"
             and isinstance(r.get(key), (int, float))]
        F = [r[key] for r in ok if r["label"] == "FAIL"
             and isinstance(r.get(key), (int, float))]
        print("-" * 74)
        print(f"METRIC: {lbl}  [{key}]  (dir={direction})")
        print(f"  PASS {describe(P)}")
        print(f"  FAIL {describe(F)}")
        if direction == "none" or not P or not F:
            print("  (no directional hypothesis / insufficient data)")
            continue
        res = best_split(P, F, direction)
        if not res:
            continue
        best, zfp = res
        J, t, fr, pr, rec, fp = best
        print(f"  BEST SEPARATION: thresh={t:.4g}  "
              f"FAIL caught {fr}/{len(F)} ({rec*100:.0f}%)  "
              f"PASS lost {pr}/{len(P)} ({fp*100:.0f}%)  "
              f"Youden J={J:.2f}")
        if zfp:
            _, zt, zfr, _, zrec, _ = zfp
            print(f"  ZERO-FALSE-POS BEST: thresh={zt:.4g}  "
                  f"FAIL caught {zfr}/{len(F)} ({zrec*100:.0f}%)  "
                  f"PASS lost 0")
        else:
            print("  ZERO-FALSE-POS BEST: none (no threshold rejects a "
                  "FAIL without also losing a PASS)")
        verdict = ("USABLE signal" if J >= 0.30 else
                   "WEAK" if J >= 0.15 else "NO separation (overlap)")
        print(f"  VERDICT: {verdict}")

    # flag frequency
    print("-" * 74)
    fl = {"PASS": defaultdict(int), "FAIL": defaultdict(int)}
    for r in ok:
        for f in r.get("flags", []):
            fl[r["label"]][f] += 1
    print("FLAG FREQUENCY (share of label that raised each flag):")
    allf = set(fl["PASS"]) | set(fl["FAIL"])
    for f in sorted(allf):
        pp = fl["PASS"][f] / nP * 100 if nP else 0
        ff = fl["FAIL"][f] / nF * 100 if nF else 0
        print(f"  {f:28s} PASS {pp:5.1f}%   FAIL {ff:5.1f}%   "
              f"{'<- separates' if abs(ff-pp) >= 25 else ''}")


if __name__ == "__main__":
    main()
