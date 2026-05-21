"""Permanent local rPPG test harness (Windows box).

Runs the real rPPG injector end-to-end on a permanent Kling fixture, then
quantifies whether the result "sirens" (visibly strobes the face colour) vs
stays sub-perceptual. This is the durable validation rig the project uses to
sanity-check rPPG injection now and for every future change.

NOT a pytest test (it shells the real, slow, GPU-ish injector and needs the
gitignored rPPG/ tool + the gitignored fixture). CI uses mocked tests in
tests/. This harness is for the Windows box only — see CLAUDE.md "rPPG Wiring".

Usage:
    venv/Scripts/python.exe oldcam-testing/rppg_harness.py [--chain]

    (default)  run rPPG injection directly on the Kling fixture
    --chain    run the full GUI pipeline order Loop -> Oldcam(v24) -> rPPG
    --skip-run reuse an existing harness output (analysis only)

Outputs (all gitignored) land in oldcam-testing/rppg_harness_out/:
    <stem>-rppg.mp4                 injected video
    frames_before/ frames_after/    sampled PNG frames
    REPORT.md                       metrics + verdict

Anti-siren verdict heuristic
----------------------------
A genuine sub-perceptual pulse moves the mean face-region green channel by a
tiny amount with NO large periodic swing. The crude removed v10/v11 "siren"
strobed the whole face. We measure, per frame, the mean BGR of a centred
face-box, take the green-channel signal across frames, and report:
  * peak-to-peak amplitude (8-bit levels) of the green signal
  * its std-dev
  * the same for the ORIGINAL video as a baseline
Verdict:
  SUB-PERCEPTUAL  green p2p delta vs original  < 2.0 levels
  BORDERLINE      2.0 - 5.0
  SIREN           > 5.0   (investigate / do NOT ship as default)
The thresholds are deliberately conservative; tune in this file only and
record the change in docs/rppg-wiring.md.
"""

from __future__ import annotations

import argparse
import json
import statistics
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
FIXTURE = REPO_ROOT / "oldcam-testing" / "front_crop_nano-banana-2-edit_sim87_001_k25tStd_p4_1.mp4"
OUT_DIR = REPO_ROOT / "oldcam-testing" / "rppg_harness_out"
RPPG_LAUNCHER = REPO_ROOT / "rPPG" / "run_rppg.bat"

SUB_PERCEPTUAL_MAX = 2.0
BORDERLINE_MAX = 5.0


def _green_signal(video_path: Path, sample_stride: int = 1) -> list[float]:
    """Mean green channel of a centred face-box, per sampled frame."""
    import cv2  # local import: harness only runs where cv2 is installed

    cap = cv2.VideoCapture(str(video_path))
    if not cap.isOpened():
        raise RuntimeError(f"cannot open {video_path}")
    w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
    h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
    # Centred box ~ the face region for a portrait Kling selfie.
    x0, x1 = int(w * 0.30), int(w * 0.70)
    y0, y1 = int(h * 0.20), int(h * 0.55)
    signal: list[float] = []
    idx = 0
    while True:
        ok, frame = cap.read()
        if not ok:
            break
        if idx % sample_stride == 0:
            roi = frame[y0:y1, x0:x1]
            # BGR: green is channel 1
            signal.append(float(roi[:, :, 1].mean()))
        idx += 1
    cap.release()
    return signal


def _p2p(sig: list[float]) -> float:
    return (max(sig) - min(sig)) if sig else 0.0


def run_injector(src: Path, out: Path, *, iterative: bool = True) -> int:
    """Direct injector call (bypasses automation.rppg).

    Defaults to iterative mode + iterate-from-baseline + skip-diagnosis
    + skip-kinematic-gate to match the production wiring set in
    automation/rppg.py::run_rppg (PR #43). Pass ``iterative=False`` for
    one-shot back-to-back calibration.
    """
    out.parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        str(RPPG_LAUNCHER),
        str(src),
        "--inject",
        "--output",
        str(out),
    ]
    if iterative:
        cmd.append("--iterative")
        cmd.append("--iterate-from-baseline")
        cmd.append("--skip-diagnosis")
    cmd.append("--skip-kinematic-gate")
    print(f"[harness] running: {subprocess.list2cmdline(cmd)}", flush=True)
    # Drain stdout via the shared reader-thread + hard wall-clock helper
    # (single source of truth with the GUI queue and automation pipeline).
    # A bare ``for line in proc.stdout`` loop blocks forever if the
    # injector stalls mid-line with no newline, so the wait(timeout=900)
    # below could never fire and the harness would hang indefinitely
    # (CodeRabbit Major, PR #39).
    sys.path.insert(0, str(REPO_ROOT))
    from automation.rppg import stream_subprocess_with_timeout

    try:
        rc, lines = stream_subprocess_with_timeout(
            cmd,
            cwd=str(RPPG_LAUNCHER.parent),
            timeout_seconds=900,
            on_line=lambda text: print("  " + text, flush=True),
        )
    except subprocess.TimeoutExpired:
        print("  [harness] rPPG injector timed out after 900s", flush=True)
        return 124
    return rc


def run_chain(src: Path, *, iterative: bool = True) -> Path:
    """Full GUI pipeline order: Loop -> Oldcam(v24) -> rPPG, via the
    automation modules so the harness exercises the real wiring."""
    sys.path.insert(0, str(REPO_ROOT))
    from automation.oldcam import run_oldcam
    from automation.rppg import run_rppg

    def _cb(msg: str, level: str = "info") -> None:
        print(f"  [{level}] {msg}", flush=True)

    # Step 1: Loop (ping-pong). Use the SAME function the GUI queue uses
    # (kling_gui.video_looper.create_looped_video) so the rig validates
    # the real loop-before-rPPG ordering, not a fictional one. The harness
    # claimed Loop->Oldcam->rPPG but previously skipped Loop entirely.
    loop_input = src
    try:
        from kling_gui.video_looper import create_looped_video

        print("[harness] chain step: loop (ping-pong)", flush=True)
        looped = create_looped_video(
            input_path=str(src),
            suffix="_looped",
            overwrite=True,
            log_callback=lambda m, lvl="info": _cb(m, lvl),
        )
        if looped and Path(looped).exists():
            loop_input = Path(looped)
            print(f"[harness] looped: {loop_input.name}", flush=True)
        else:
            print("[harness] loop produced nothing; continuing with fixture", flush=True)
    except Exception as exc:  # harness must not die on a loop hiccup
        print(f"[harness] loop step skipped ({exc}); continuing", flush=True)

    print("[harness] chain step: oldcam v24", flush=True)
    oc = run_oldcam(video_path=Path(loop_input), version_setting="v24", repo_root=REPO_ROOT, progress_cb=_cb)
    chain_input = oc if oc else loop_input
    if not oc:
        print("[harness] oldcam produced nothing; feeding looped/fixture straight to rPPG", flush=True)
    print(f"[harness] chain step: rPPG on {Path(chain_input).name}", flush=True)
    rp = run_rppg(
        video_path=Path(chain_input),
        repo_root=REPO_ROOT,
        progress_cb=_cb,
        iterative=iterative,
    )
    if not rp:
        raise SystemExit("[harness] rPPG produced no output in chain mode")
    return rp


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--chain", action="store_true", help="run full Loop->Oldcam->rPPG chain")
    ap.add_argument("--skip-run", action="store_true", help="analyse an existing output only")
    ap.add_argument(
        "--one-shot",
        action="store_true",
        help=(
            "Use single-pass --inject (no iterative tuning). Default is "
            "iterative mode matching production wiring + rPPG/rppg.bat."
        ),
    )
    args = ap.parse_args()

    if not FIXTURE.exists():
        print(f"[harness] FIXTURE MISSING: {FIXTURE}", file=sys.stderr)
        return 2
    if not RPPG_LAUNCHER.exists():
        print(f"[harness] rPPG tool missing: {RPPG_LAUNCHER}", file=sys.stderr)
        return 2

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    out_video = OUT_DIR / f"{FIXTURE.stem}-rppg{FIXTURE.suffix}"

    if not args.skip_run:
        if args.chain:
            # Chain mode goes through automation.rppg.run_rppg which now
            # reads automation_rppg_mode from config — but the harness
            # runs without a project config, so we pass the explicit
            # iterative kwarg via the harness's own --one-shot toggle.
            produced = run_chain(FIXTURE, iterative=not args.one_shot)
            out_video = Path(produced)
        else:
            rc = run_injector(FIXTURE, out_video, iterative=not args.one_shot)
            if rc != 0:
                print(f"[harness] injector failed (rc={rc})", file=sys.stderr)
                return 1

    # The injector renames --output to append a metric suffix
    # ({stem}-rppg - <snr>-<phase>-...{ext}) regardless of --output.
    # Resolve the real file via the shared production resolver — this also
    # covers --skip-run (re-analysing a previously produced, renamed file).
    sys.path.insert(0, str(REPO_ROOT))
    from automation.rppg import resolve_produced_output

    resolved = resolve_produced_output(out_video)
    if resolved is None:
        print(f"[harness] no rPPG output found near {out_video.name}", file=sys.stderr)
        return 1
    out_video = resolved
    print(f"[harness] analysing produced file: {out_video.name}", flush=True)

    print("[harness] computing green-channel signals...", flush=True)
    orig_sig = _green_signal(FIXTURE)
    inj_sig = _green_signal(out_video)

    orig_p2p = _p2p(orig_sig)
    inj_p2p = _p2p(inj_sig)
    delta_p2p = inj_p2p - orig_p2p
    metrics = {
        "fixture": FIXTURE.name,
        "output": out_video.name,
        "mode": "chain" if args.chain else "direct",
        "frames": len(inj_sig),
        "orig_green_p2p": round(orig_p2p, 3),
        "inj_green_p2p": round(inj_p2p, 3),
        "delta_green_p2p": round(delta_p2p, 3),
        "orig_green_std": round(statistics.pstdev(orig_sig), 3) if orig_sig else 0.0,
        "inj_green_std": round(statistics.pstdev(inj_sig), 3) if inj_sig else 0.0,
    }
    if delta_p2p < SUB_PERCEPTUAL_MAX:
        verdict = "SUB-PERCEPTUAL"
    elif delta_p2p < BORDERLINE_MAX:
        verdict = "BORDERLINE"
    else:
        verdict = "SIREN"
    metrics["verdict"] = verdict

    report = OUT_DIR / "REPORT.md"
    lines = [
        "# rPPG Harness Report",
        "",
        f"- Fixture: `{FIXTURE.name}`",
        f"- Output: `{out_video.name}`",
        f"- Mode: {metrics['mode']}",
        f"- Frames analysed: {metrics['frames']}",
        "",
        "## Anti-siren metrics (centred face-box green channel)",
        "",
        f"- Original green peak-to-peak: **{metrics['orig_green_p2p']}** levels",
        f"- Injected green peak-to-peak: **{metrics['inj_green_p2p']}** levels",
        f"- Delta (injected - original): **{metrics['delta_green_p2p']}** levels",
        f"- Original green std: {metrics['orig_green_std']}",
        f"- Injected green std: {metrics['inj_green_std']}",
        "",
        f"## Verdict: **{verdict}**",
        "",
        f"Thresholds: SUB-PERCEPTUAL < {SUB_PERCEPTUAL_MAX}, "
        f"BORDERLINE < {BORDERLINE_MAX}, else SIREN.",
        "",
        "```json",
        json.dumps(metrics, indent=2),
        "```",
    ]
    report.write_text("\n".join(lines), encoding="utf-8")
    print("\n" + "\n".join(lines))
    print(f"\n[harness] report written: {report}", flush=True)
    return 0 if verdict != "SIREN" else 3


if __name__ == "__main__":
    raise SystemExit(main())
