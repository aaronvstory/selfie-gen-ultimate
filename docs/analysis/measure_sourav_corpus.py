"""Measure face-track % across the Sourav Vai labelled corpus.

DUPES/        -> passed Persona  (label = PASS)
FAILED PERSONA/ -> failed Persona (label = FAIL)

Each persona folder holds a raw Kling source (`*_k25tPro_p*_1.mp4`) and a
looped/delivered twin (`*_1_looped.mp4`). The validated signal
(docs/analysis/versailles_fail_vs_pass.md) is the *Kling source* track%,
so we measure both but treat the non-looped source as primary.

Uses the exact production gate (automation.face_track_gate.measure_face_track)
so numbers equal what the pipeline would compute.

Output: docs/analysis/sourav_facetrack_results.json  (one record per video)
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

_REPO = Path(__file__).resolve().parent.parent.parent
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

from automation.face_track_gate import measure_face_track  # noqa: E402

BASE = Path(r"F:\Downloads\Telegram Desktop\DLs\Sourav Vai")
LABELS = {"DUPES": "PASS", "FAILED PERSONA": "FAIL"}
VIDEO_EXTS = {".mp4", ".mov", ".avi", ".mkv", ".webm"}
SAMPLE_FPS = 8.0
OUT = _REPO / "docs" / "analysis" / "sourav_facetrack_results.json"


def kind_of(name: str) -> str:
    low = name.lower()
    if low.endswith("_looped.mp4") or "looped" in low:
        return "looped"
    return "kling_source"


def main() -> None:
    records = []
    todo = []
    for folder, label in LABELS.items():
        root = BASE / folder
        for vid in sorted(root.rglob("*")):
            if vid.is_file() and vid.suffix.lower() in VIDEO_EXTS:
                todo.append((vid, label))

    total = len(todo)
    print(f"[corpus] {total} videos to measure (PASS=DUPES, FAIL=FAILED PERSONA)", flush=True)
    t0 = time.time()
    for i, (vid, label) in enumerate(todo, 1):
        persona = vid.parent.name
        kind = kind_of(vid.name)
        try:
            r = measure_face_track(str(vid), _REPO, sample_fps=SAMPLE_FPS)
            rec = {
                "label": label,
                "persona": persona,
                "kind": kind,
                "file": vid.name,
                "available": r.available,
                "track_pct": r.track_pct,
                "passed_96": (r.track_pct is not None and r.track_pct >= 96.0),
                "reason": r.reason,
                "meta": r.to_meta(),
            }
        except Exception as exc:  # noqa: BLE001 - never abort the sweep
            rec = {
                "label": label,
                "persona": persona,
                "kind": kind,
                "file": vid.name,
                "available": False,
                "track_pct": None,
                "passed_96": False,
                "reason": f"exception: {exc!r}",
                "meta": {},
            }
        records.append(rec)
        if i % 10 == 0 or i == total:
            el = time.time() - t0
            tp = rec.get("track_pct")
            tp = f"{tp:.1f}" if isinstance(tp, (int, float)) else "n/a"
            print(
                f"[{i}/{total}] {el:6.1f}s  {label:4s} {kind:12s} "
                f"track={tp:>5s}  {persona[:42]}",
                flush=True,
            )

    OUT.write_text(json.dumps(records, indent=2), encoding="utf-8")
    print(f"[done] {len(records)} records -> {OUT}", flush=True)


if __name__ == "__main__":
    main()
