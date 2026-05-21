# rPPG (in-tree as of polish/v2.3 — Phase D)

Friend's liveness/rPPG tool. Was previously gitignored ("sensitive, sent
in confidence") but committed in-tree at commit `daec295d` (Phase D of
polish/v2.3, 2026-05-22) with explicit owner sign-off so macOS clones
get the tool without a manual side-channel copy. Update this header if
the consent ever needs to revert.

## Run it (Windows)

Use `run_rppg.bat` — it resolves the repo's shared `venv` automatically
and points MediaPipe at `rPPG/models/face_landmarker.task` (also
committed in Phase D). Deps: cv2/numpy/mediapipe/scipy/absl —
`scipy` was added to `requirements.txt` in v2.3 (the dep was missing
from the Phase D in-tree commit and crashed fresh macOS installs).

## Run it (macOS / Linux)

Use `run_rppg.sh` (added in Phase G of polish/v2.3). The shell launcher
resolves the same Python chain (`.venv311` → `.venv` → system 3.11)
that the GUI uses. On macOS arm64 the injector forces the mediapipe
**CPU delegate** because the GPU path SIGABRTs in
`ImageCloneCalculator::Process` during frame inference — slower but
stable.

```bat
rem analyze a video's rPPG / liveness metrics (no write)
run_rppg.bat "F:\path\to\video.mp4" --analyze

rem iterative inject toward target metrics, then auto-diagnose via Claude
run_rppg.bat "F:\path\to\video.mp4" --inject --iterative

rem skip the (new, untested) kinematic preflight gate
run_rppg.bat "F:\path\to\video.mp4" --analyze --skip-kinematic-gate
```

## What matters for us (Persona)

Per the tool author: Persona gates on **kinematic/temporal motion**, not
the rPPG pulse. The primary engine is `rppg_injector.py` (mature,
diagnostic-tuned). `face_kinematics.py` is a **new, untested** preflight
gate quarantined in its own file — promising (head-pose jerk) but not
yet calibrated. The 3 target metrics live in `rppg_injector.py`:
temporal_consistency>=0.85, motion_artifacts 0.03..0.15, harmonic>=0.7.

See `docs/analysis/versailles-fail-vs-pass.md` (committed) for the
ground-truth analysis and the calibration plan.
