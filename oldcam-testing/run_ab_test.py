#!/usr/bin/env python3
"""
run_ab_test.py — quick V15-vs-V16 (or any oldcam variant) A/B harness.

Standalone experiment helper. Given one or more Kling/source videos it:

  1. Runs each through V15 (oldcam-v15/oldcam.py) and the standalone V16
     (oldcam-testing/oldcam_v16.py), writing both into a fresh timestamped
     run folder under oldcam-testing/runs/.
  2. Invokes the resemble-score CLI on that folder
     (python resemble-score/main.py --cli --folder <run> --all) so every
     produced clip — plus the original — is scored by the Resemble
     deepfake-detection API. Lower score = more "real" = better.
  3. Parses resemble-score's resemble_results.json and appends a ranked
     summary row to oldcam-testing/SCOREBOARD.md so results accumulate
     across runs and you can see whether V16 actually beats V15.

This file is intentionally NOT wired into the app (no launcher, no GUI, no
discovery, no main test suite). It is a throwaway bench for one question:
does the V16 motion-coupling idea score better than V15 before we invest in
integrating it everywhere?

Usage:
    python oldcam-testing/run_ab_test.py CLIP.mp4 [CLIP2.mp4 ...]
    python oldcam-testing/run_ab_test.py CLIP.mp4 --variants v15 v16
    python oldcam-testing/run_ab_test.py CLIP.mp4 --no-score   # make only

Requires: RESEMBLE_API_KEY discoverable by resemble-score (see its README);
ffmpeg on PATH (the oldcam pipeline needs it for the final H.264 encode).
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path

# oldcam-testing/  ->  repo root
_HERE = Path(__file__).resolve().parent
_REPO_ROOT = _HERE.parent

VARIANT_SCRIPTS = {
    "v15": _REPO_ROOT / "oldcam-v15" / "oldcam.py",
    "v16": _HERE / "oldcam_v16.py",
}
RESEMBLE_MAIN = _REPO_ROOT / "resemble-score" / "main.py"
RUNS_DIR = _HERE / "runs"
SCOREBOARD = _HERE / "SCOREBOARD.md"


def _run(cmd: list[str]) -> int:
    print(f"  $ {' '.join(str(c) for c in cmd)}", flush=True)
    return subprocess.run(cmd, cwd=str(_REPO_ROOT)).returncode


def make_variant(variant: str, source: Path, run_dir: Path) -> Path | None:
    """Run one oldcam variant on `source`; return the produced video path."""
    script = VARIANT_SCRIPTS[variant]
    if not script.is_file():
        print(f"  ! {variant}: script not found at {script}", flush=True)
        return None
    out = run_dir / f"{source.stem}-oldcam-{variant}{source.suffix}"
    rc = _run([sys.executable, str(script), str(source), "-o", str(out)])
    if rc != 0 or not out.is_file():
        print(f"  ! {variant}: oldcam run failed (rc={rc})", flush=True)
        return None
    print(f"  + {variant}: {out.name}", flush=True)
    return out


def score_folder(run_dir: Path) -> dict | None:
    """Invoke the resemble-score CLI on run_dir; return parsed results."""
    if not RESEMBLE_MAIN.is_file():
        print(f"  ! resemble-score not found at {RESEMBLE_MAIN}", flush=True)
        return None
    rc = _run([
        sys.executable, str(RESEMBLE_MAIN),
        "--cli", "--folder", str(run_dir), "--all",
    ])
    results = run_dir / "resemble_results.json"
    if rc != 0 or not results.is_file():
        print(f"  ! scoring failed (rc={rc}); see resemble-score output above",
              flush=True)
        return None
    try:
        return json.loads(results.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        print(f"  ! could not read resemble_results.json: {exc}", flush=True)
        return None


def _extract_rows(results: dict) -> list[tuple[str, float | str]]:
    """Best-effort flatten of resemble_results.json into (label, score) rows.

    resemble-score's exact JSON shape may evolve; accept the common cases
    (a top-level list, or a dict with a 'results'/'ranked' list) and fall
    back to stringifying so a schema change degrades to "still logged".
    """
    items = results
    if isinstance(results, dict):
        for key in ("ranked", "results", "videos", "items"):
            if isinstance(results.get(key), list):
                items = results[key]
                break
    rows: list[tuple[str, float | str]] = []
    if isinstance(items, list):
        for it in items:
            if not isinstance(it, dict):
                continue
            label = (
                it.get("name") or it.get("file") or it.get("path")
                or it.get("video") or "?"
            )
            score = (
                it.get("score", it.get("deepfake_score",
                        it.get("mean_score", "n/a")))
            )
            rows.append((str(Path(str(label)).name), score))
    return rows


def append_scoreboard(source: Path, run_dir: Path, results: dict) -> None:
    SCOREBOARD.parent.mkdir(parents=True, exist_ok=True)
    if not SCOREBOARD.exists():
        SCOREBOARD.write_text(
            "# Oldcam A/B Scoreboard\n\n"
            "Resemble deepfake-API scores. **Lower = more real = better.**\n"
            "Appended by `run_ab_test.py`; newest run at the bottom.\n",
            encoding="utf-8",
        )
    rows = _extract_rows(results)
    rows_sorted = sorted(
        rows, key=lambda r: r[1] if isinstance(r[1], (int, float)) else 1e9
    )
    ts = datetime.now().strftime("%Y-%m-%d %H:%M")
    lines = [
        f"\n## {ts} — `{source.name}`",
        f"\nRun folder: `{run_dir.relative_to(_REPO_ROOT)}`\n",
        "| Rank | Video | Resemble score |",
        "|------|-------|----------------|",
    ]
    for i, (label, score) in enumerate(rows_sorted, 1):
        flag = " 🏆" if i == 1 else ""
        lines.append(f"| {i} | {label} | {score}{flag} |")
    if not rows_sorted:
        lines.append("| — | (could not parse results — see "
                      "resemble_results.json in the run folder) | — |")
    with SCOREBOARD.open("a", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    print(f"\n  -> scoreboard updated: {SCOREBOARD.relative_to(_REPO_ROOT)}",
          flush=True)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="Quick V15-vs-V16 A/B via oldcam + resemble-score."
    )
    ap.add_argument("inputs", nargs="+", help="Source/Kling video file(s).")
    ap.add_argument(
        "--variants", nargs="+", default=["v15", "v16"],
        choices=sorted(VARIANT_SCRIPTS), help="Which variants to make.",
    )
    ap.add_argument(
        "--no-score", action="store_true",
        help="Only produce the oldcam'd files; skip the Resemble API call.",
    )
    args = ap.parse_args(argv)

    sources = [Path(p).resolve() for p in args.inputs]
    missing = [str(s) for s in sources if not s.is_file()]
    if missing:
        print(f"Input file(s) not found: {', '.join(missing)}", file=sys.stderr)
        return 2

    run_dir = RUNS_DIR / datetime.now().strftime("run_%Y%m%d_%H%M%S")
    run_dir.mkdir(parents=True, exist_ok=True)
    print(f"Run folder: {run_dir}\n", flush=True)

    produced = 0
    for src in sources:
        # Keep a copy of the original in the run folder so resemble-score
        # also scores the untouched Kling render as the baseline.
        shutil.copy2(src, run_dir / src.name)
        print(f"[{src.name}]", flush=True)
        for variant in args.variants:
            if make_variant(variant, src, run_dir):
                produced += 1

    if produced == 0:
        print("\nNo variants produced — aborting.", file=sys.stderr)
        return 1

    if args.no_score:
        print(f"\n--no-score: {produced} file(s) in {run_dir}. "
              "Score later with resemble-score --cli --folder <that> --all.",
              flush=True)
        return 0

    print("\nScoring with resemble-score ...", flush=True)
    results = score_folder(run_dir)
    if results is None:
        print("\nScoring did not complete; the produced videos are still in "
              f"{run_dir} — you can score them manually.", file=sys.stderr)
        return 1

    append_scoreboard(sources[0], run_dir, results)
    print("\nDone. Compare V15 vs V16 in "
          f"{SCOREBOARD.relative_to(_REPO_ROOT)} (lower score wins).",
          flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
