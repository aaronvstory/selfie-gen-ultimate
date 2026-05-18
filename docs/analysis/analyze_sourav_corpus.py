"""Analyze sourav_facetrack_results.json — distributions, threshold sweep,
and any other substantive signal. Honest stats, no spin.

Run AFTER measure_sourav_corpus.py finishes.
"""
from __future__ import annotations

import json
import statistics as st
from collections import defaultdict
from pathlib import Path

_REPO = Path(__file__).resolve().parent.parent.parent
RESULTS = _REPO / "docs" / "analysis" / "sourav_facetrack_results.json"


def pctile(xs, q):
    if not xs:
        return None
    xs = sorted(xs)
    i = max(0, min(len(xs) - 1, int(round(q * (len(xs) - 1)))))
    return xs[i]


def describe(xs):
    if not xs:
        return "n=0"
    return (
        f"n={len(xs)} min={min(xs):.1f} p10={pctile(xs,.10):.1f} "
        f"p25={pctile(xs,.25):.1f} median={st.median(xs):.1f} "
        f"p75={pctile(xs,.75):.1f} p90={pctile(xs,.90):.1f} "
        f"max={max(xs):.1f} mean={st.mean(xs):.1f}"
    )


def sweep(pass_vals, fail_vals, thresholds):
    """For each threshold T: a clip is REJECTED if track% < T.
    recall = FAILs rejected / total FAIL; fp = PASSes wrongly rejected / total PASS.
    """
    print(f"\n  {'thresh':>7} | {'FAIL rejected':>16} | {'PASS wrongly rej':>18} | verdict")
    print("  " + "-" * 64)
    nP, nF = len(pass_vals), len(fail_vals)
    for t in thresholds:
        fr = sum(1 for v in fail_vals if v < t)
        pr = sum(1 for v in pass_vals if v < t)
        fp_rate = (pr / nP * 100) if nP else 0.0
        rec = (fr / nF * 100) if nF else 0.0
        flag = "ZERO false pos" if pr == 0 else f"{pr} PASS lost"
        print(
            f"  {t:>6.1f}% | {fr:>3}/{nF} ({rec:4.1f}%) | "
            f"{pr:>3}/{nP} ({fp_rate:4.1f}%) | {flag}"
        )


def main() -> None:
    recs = json.loads(RESULTS.read_text(encoding="utf-8"))
    avail = [r for r in recs if r["available"] and r["track_pct"] is not None]
    unavail = [r for r in recs if not (r["available"] and r["track_pct"] is not None)]

    print("=" * 72)
    print("SOURAV VAI CORPUS — FACE-TRACK ANALYSIS")
    print("=" * 72)
    print(f"total records           : {len(recs)}")
    print(f"measured (face detector): {len(avail)}")
    print(f"unmeasurable (skipped)  : {len(unavail)}")
    if unavail:
        rs = defaultdict(int)
        for r in unavail:
            rs[r["reason"][:60]] += 1
        for k, v in sorted(rs.items(), key=lambda x: -x[1]):
            print(f"   - {v:>3}x  {k}")

    # KLING_SOURCE is the validated signal (primary). LOOPED is shown only
    # as a sanity check — user note: it should not make a difference; if it
    # diverges wildly from source that is itself informative.
    def report_kind(kind, primary):
        sub = [r for r in avail if r["kind"] == kind]
        P = [r["track_pct"] for r in sub if r["label"] == "PASS"]
        F = [r["track_pct"] for r in sub if r["label"] == "FAIL"]
        tag = "PRIMARY (validated signal)" if primary else "SANITY CHECK ONLY"
        print("\n" + "=" * 72)
        print(f"KIND = {kind.upper()}  [{tag}]  (PASS n={len(P)}, FAIL n={len(F)})")
        print("=" * 72)
        print(f"  PASS  {describe(P)}")
        print(f"  FAIL  {describe(F)}")
        if not P or not F:
            print("  (insufficient data for this kind)")
            return
        print(f"\n  PASS min = {min(P):.1f}   FAIL spans [{min(F):.1f}, {max(F):.1f}]")
        below96P = sum(1 for v in P if v < 96)
        below96F = sum(1 for v in F if v < 96)
        print(
            f"  < 96% : PASS {below96P}/{len(P)} ({below96P/len(P)*100:.1f}%), "
            f"FAIL {below96F}/{len(F)} ({below96F/len(F)*100:.1f}%)"
        )
        sweep(P, F, [80, 85, 88, 90, 92, 94, 95, 96, 97, 98, 99, 99.5, 100])
        clean_fail = sum(1 for v in F if v >= 96)
        print(
            f"\n  HONEST LIMIT: {clean_fail}/{len(F)} "
            f"({clean_fail/len(F)*100:.1f}%) of FAILs track >=96% "
            f"(invisible to this gate — no static/track tell)"
        )
        print("\n  PASS track% sorted:", sorted(round(v, 1) for v in P))
        print("\n  FAIL track% sorted:", sorted(round(v, 1) for v in F))

    report_kind("kling_source", primary=True)
    report_kind("looped", primary=False)

    # Pooled per-persona (Kling src preferred, looped fallback) — the
    # "what the gate would actually decide per persona" view.
    by_persona = defaultdict(dict)
    for r in avail:
        by_persona[(r["label"], r["persona"])][r["kind"]] = r["track_pct"]
    pooled = {"PASS": [], "FAIL": []}
    for (label, _), kinds in by_persona.items():
        v = kinds.get("kling_source", kinds.get("looped"))
        if v is not None:
            pooled[label].append(v)
    print("\n" + "=" * 72)
    print(f"POOLED PER-PERSONA (Kling src preferred)  "
          f"PASS n={len(pooled['PASS'])}, FAIL n={len(pooled['FAIL'])}")
    print("=" * 72)
    print(f"  PASS  {describe(pooled['PASS'])}")
    print(f"  FAIL  {describe(pooled['FAIL'])}")
    if pooled["PASS"] and pooled["FAIL"]:
        sweep(pooled["PASS"], pooled["FAIL"],
              [80, 85, 88, 90, 92, 94, 95, 96, 97, 98, 99, 100])


if __name__ == "__main__":
    main()
