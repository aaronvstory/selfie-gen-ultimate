"""Real-production Persona success rate by oldcam version.

Rules (locked with user 2026-05-19):
  PASS folders = DASHERS, BANNED, FAILED BGR, DUPES  (all real Persona PASS)
  FAIL folders = FAILED (versailles), FAILED PERSONA  (real Persona FAIL)
  1 attempt   = 1 persona subfolder, tagged by the HIGHEST oldcam-vN .mp4
                in it (the "what shipped" rule). No oldcam file -> 'no-oldcam'.
  Per-version table excludes no-oldcam folders (reported separately).
  Sourav Vai  = real-selfie (no oldcam); everything else = gen-selfie.

Output: docs/analysis/prod_success_by_version.json + a printed report.
Reads folders in place; commits nothing.
"""
from __future__ import annotations

import json
import re
import sys
from collections import defaultdict
from pathlib import Path

CORPORA = [
    # (path, label, population)
    (r"F:\Downloads\Telegram Desktop\DLs\versailles\organized\FAILED", "FAIL", "gen"),
    (r"F:\Downloads\Telegram Desktop\DLs\versailles\organized\DASHERS", "PASS", "gen"),
    (r"F:\Downloads\Telegram Desktop\DLs\USA omnapayments scans\DASHERS", "PASS", "gen"),
    (r"F:\Downloads\Telegram Desktop\DLs\USA omnapayments scans\BANNED", "PASS", "gen"),
    (r"F:\Downloads\Telegram Desktop\DLs\USA omnapayments scans\FAILED BGR", "PASS", "gen"),
    (r"F:\Downloads\Telegram Desktop\DLs\USA omnapayments scans\FAILED PERSONA", "FAIL", "gen"),
    (r"F:\Downloads\Telegram Desktop\DLs\USA omnapayments scans\DUPES", "PASS", "gen"),
    (r"F:\Downloads\Telegram Desktop\DLs\Sourav Vai\DUPES", "PASS", "real"),
    (r"F:\Downloads\Telegram Desktop\DLs\Sourav Vai\FAILED PERSONA", "FAIL", "real"),
]

VIDEO_EXTS = {".mp4", ".mov", ".avi", ".mkv", ".webm"}
# oldcam version token in a filename, e.g. "...-oldcam-v24.mp4", "_oldcam_v13_"
VER_RE = re.compile(r"oldcam[-_]?v(\d+)", re.IGNORECASE)


def highest_oldcam_version(folder: Path) -> int | None:
    """Highest oldcam vN across all video files in this persona folder.
    None = no oldcam'd video present (raw Kling / real selfie)."""
    best = None
    for f in folder.rglob("*"):
        if f.is_file() and f.suffix.lower() in VIDEO_EXTS:
            m = VER_RE.search(f.name)
            if m:
                v = int(m.group(1))
                if best is None or v > best:
                    best = v
    return best


def main() -> None:
    # per (population) -> per version -> {PASS,FAIL} counts
    table: dict = defaultdict(lambda: defaultdict(lambda: {"PASS": 0, "FAIL": 0}))
    no_oldcam: dict = defaultdict(lambda: {"PASS": 0, "FAIL": 0})
    detail = []
    corpus_summary = []

    for raw, label, pop in CORPORA:
        root = Path(raw)
        if not root.is_dir():
            corpus_summary.append(f"  !! MISSING: {raw}")
            continue
        personas = [d for d in sorted(root.iterdir()) if d.is_dir()]
        # if a corpus has files directly (no persona subdirs), treat the
        # corpus root itself as a single "persona" only if it has videos
        if not personas:
            personas = [root]
        n_pass = n_fail = n_noold = 0
        for d in personas:
            v = highest_oldcam_version(d)
            if v is None:
                no_oldcam[(pop, label)]["PASS" if label == "PASS" else "FAIL"] += 0
                no_oldcam[(pop,)][label] += 1
                n_noold += 1
                detail.append({"folder": d.name, "corpus": root.name,
                                "label": label, "pop": pop,
                                "version": None})
            else:
                table[pop][f"v{v}"][label] += 1
                detail.append({"folder": d.name, "corpus": root.name,
                               "label": label, "pop": pop,
                               "version": f"v{v}"})
            if label == "PASS":
                n_pass += 1
            else:
                n_fail += 1
        corpus_summary.append(
            f"  {root.name:<24} [{label}/{pop}] personas={len(personas)} "
            f"(no-oldcam={n_noold})")

    # ---- print report ----
    print("=" * 72)
    print("REAL-PRODUCTION PERSONA SUCCESS BY OLDCAM VERSION")
    print("=" * 72)
    print("\nCorpora scanned:")
    for s in corpus_summary:
        print(s)

    for pop, popname in (("gen", "GENERATED-SELFIE (versailles + omnapayments)"),
                         ("real", "REAL-SELFIE (Sourav Vai)")):
        print("\n" + "=" * 72)
        print(f"{popname}")
        print("=" * 72)
        vt = table.get(pop, {})
        if vt:
            print(f"  {'version':>8} | {'attempts':>8} | {'PASS':>5} | "
                  f"{'FAIL':>5} | pass-rate")
            print("  " + "-" * 56)
            def vkey(k):
                return int(k[1:])
            for ver in sorted(vt, key=vkey):
                c = vt[ver]
                a = c["PASS"] + c["FAIL"]
                pr = (c["PASS"] / a * 100) if a else 0.0
                print(f"  {ver:>8} | {a:>8} | {c['PASS']:>5} | "
                      f"{c['FAIL']:>5} | {pr:5.1f}%")
        else:
            print("  (no oldcam-versioned folders in this population)")
        no = no_oldcam.get((pop,), {"PASS": 0, "FAIL": 0})
        if no["PASS"] or no["FAIL"]:
            a = no["PASS"] + no["FAIL"]
            pr = no["PASS"] / a * 100 if a else 0.0
            print(f"  {'NO-OLDCAM':>8} | {a:>8} | {no['PASS']:>5} | "
                  f"{no['FAIL']:>5} | {pr:5.1f}%   (raw Kling / real "
                  f"selfie; not attributable to any version)")

    out = {
        "rules": "1 attempt = 1 persona folder, tagged by highest "
                 "oldcam-vN; PASS={DASHERS,BANNED,FAILED BGR,DUPES}, "
                 "FAIL={FAILED,FAILED PERSONA}; Sourav Vai=real-selfie",
        "by_population": {p: {v: dict(c) for v, c in vt.items()}
                          for p, vt in table.items()},
        "no_oldcam": {f"{k}": dict(v) for k, v in no_oldcam.items()
                      if len(k) == 1},
        "detail": detail,
    }
    Path("docs/analysis/prod_success_by_version.json").write_text(
        json.dumps(out, indent=2), encoding="utf-8")
    print(f"\n[done] detail for {len(detail)} persona folders -> "
          f"docs/analysis/prod_success_by_version.json")


if __name__ == "__main__":
    sys.exit(main())
