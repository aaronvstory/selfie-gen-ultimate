# rPPG (local, gitignored — NEVER commit/push)

Friend's liveness/rppg tool. Sensitive, sent in confidence. The whole
`rPPG/` dir is gitignored (`.gitignore` + `**/rppg_injector*`).

## Run it (Windows)

Use `run_rppg.bat` — it resolves the repo's shared `venv` automatically
(all deps already present: cv2/numpy/mediapipe/scipy/sklearn/absl, no pip
step) and points MediaPipe at the repo's `face_landmarker.task`.

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
