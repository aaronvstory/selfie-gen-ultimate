"""Run the full kinematic metric suite over the Sourav Vai corpus.

Complements measure_sourav_corpus.py (face-track %). This adds the
GEOMETRIC motion signal the analysis doc says is the real Persona
candidate: head-pose angular jerk + blink interval/duration, via
rPPG/face_kinematics.score_face_kinematics (pure landmark geometry,
no rPPG pulse — repo-safe, fast ~10-15s/clip).

Kling source only (user: looped doesn't matter; validated signal is
the raw Kling source). rPPG --analyze (5 liveness metrics, ~3min/clip)
is deliberately SKIPPED — the doc already proved it non-discriminating.

Output: docs/analysis/sourav_kinematic_results.json (one record/clip).
Resumable: re-running skips clips already in the JSON.
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

_REPO = Path(__file__).resolve().parent.parent.parent
for p in (str(_REPO), str(_REPO / "rPPG")):
    if p not in sys.path:
        sys.path.insert(0, p)

from face_kinematics import score_face_kinematics  # noqa: E402  (rPPG/)

BASE = Path(r"F:\Downloads\Telegram Desktop\DLs\Sourav Vai")
LABELS = {"DUPES": "PASS", "FAILED PERSONA": "FAIL"}
VIDEO_EXTS = {".mp4", ".mov", ".avi", ".mkv", ".webm"}
OUT = _REPO / "docs" / "analysis" / "sourav_kinematic_results.json"


def is_kling_source(name: str) -> bool:
    low = name.lower()
    return not (low.endswith("_looped.mp4") or "looped" in low)


def main() -> None:
    done = {}
    if OUT.exists():
        try:
            done = {(r["label"], r["persona"], r["file"]): r
                    for r in json.loads(OUT.read_text(encoding="utf-8"))}
        except Exception:  # noqa: BLE001
            done = {}

    todo = []
    for folder, label in LABELS.items():
        root = BASE / folder
        for vid in sorted(root.rglob("*")):
            if (vid.is_file() and vid.suffix.lower() in VIDEO_EXTS
                    and is_kling_source(vid.name)):
                todo.append((vid, label))

    total = len(todo)
    print(f"[kinematics] {total} Kling-source clips "
          f"(PASS=DUPES, FAIL=FAILED PERSONA); {len(done)} already done",
          flush=True)
    records = list(done.values())
    t0 = time.time()
    for i, (vid, label) in enumerate(todo, 1):
        persona = vid.parent.name
        key = (label, persona, vid.name)
        if key in done:
            continue
        try:
            res = score_face_kinematics(str(vid))
            d = res.details or {}
            hd = d.get("head_jerk", {}) or {}
            bd = d.get("blink", {}) or {}
            rec = {
                "label": label, "persona": persona, "file": vid.name,
                "ok": True,
                "kin_overall": round(float(res.overall), 4),
                "kin_head_jerk": round(float(res.head_jerk), 4),
                "kin_blink": round(float(res.blink), 4),
                "flags": list(res.flags),
                "jerk_mag_mean": hd.get("jerk_mag_mean"),
                "jerk_mag_p95": hd.get("jerk_mag_p95"),
                "jerk_mag_max": hd.get("jerk_mag_max"),
                "blink_dur_mean_ms": bd.get("duration_mean_ms"),
                "blink_dur_min_ms": bd.get("duration_min_ms"),
                "blink_dur_max_ms": bd.get("duration_max_ms"),
                "frames_used": d.get("frames_sampled"),
            }
        except Exception as exc:  # noqa: BLE001 - never abort the sweep
            rec = {
                "label": label, "persona": persona, "file": vid.name,
                "ok": False, "error": f"{exc!r}",
            }
        records.append(rec)
        if i % 5 == 0 or i == total:
            el = time.time() - t0
            ov = rec.get("kin_overall")
            ov = f"{ov:.3f}" if isinstance(ov, (int, float)) else "n/a"
            print(
                f"[{i}/{total}] {el:6.1f}s {label:4s} "
                f"ov={ov:>5s} hj={rec.get('kin_head_jerk','n/a')} "
                f"bl={rec.get('kin_blink','n/a')}  {persona[:38]}",
                flush=True,
            )
            OUT.write_text(json.dumps(records, indent=2), encoding="utf-8")

    OUT.write_text(json.dumps(records, indent=2), encoding="utf-8")
    print(f"[done] {len(records)} records -> {OUT}", flush=True)


if __name__ == "__main__":
    main()
