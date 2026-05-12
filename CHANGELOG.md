# Changelog

All notable changes to this project are documented here.

## 2026-05-12 (v1.4)

### Added

- **Oldcam V9 (Dynamic Mesh)**: MediaPipe FaceLandmarker face detection, region-aware effect masks,
  AWB color drift simulation, background blur, temporal smoothing of mesh landmarks.
- **Oldcam V10 (Spatial Sync)**: All of V9 plus FFT-based per-region frequency analysis,
  phase-locked oscillations per face region, dynamic relighting, graceful degradation for short clips.
- GUI: Re-Run button right-sized with folder picker for alternate output directories.
- GUI: Rerun icon button restored; sash layout proportions tuned (Step 3 wider, drop zone narrower).

### Fixed

- Oldcam V9/V10 H.264 video quality: upgraded from `baseline` + 1500k bitrate cap to CRF 18 +
  `profile:v high`, eliminating motion-detail crushing on face-aware output videos.
- Preview output collision: each version now writes version-tagged preview files
  (`clip-preview-v7.mp4` through `clip-preview-v10.mp4`) instead of all overwriting the same filename.
- macOS `.command` launchers (all 4 versions): added `[ -n "$REPO_ROOT" ]` guard before venv path
  probes, preventing false filesystem matches when `find_repo_root()` returns empty.
- `setup_macos.sh`: tightened mediapipe grep pattern and added `|| true` to prevent script abort
  under `set -euo pipefail`.
- Windows bat launchers V7/V8: added certutil-based PY_ID stamp, fixed `>nul 2>nul` redirects,
  added `call` keyword in PROCESS_ONE subroutine.
- Windows bat launchers V9/V10: added `MP_VALIDATE_CMD` variable, `--force-reinstall --no-deps`
  for MediaPipe install, `FINAL_EXIT` exit pattern.
- `run_oldcam.bat`: full rewrite with V9 launcher logic, mediapipe install, stamp cache.
- `similarity/run_cli.bat`: structured `if "%SIMILARITY_LAUNCHED_BY_MAIN%"=="" (` blocks with
  log-redirected launch path alongside direct invocation.

### Changed

- Release packaging emits `SelfieGenUltimate-v1.4.zip` (canonical) + `SelfieGenUltimate.zip` (alias).

### Docs

- Added "Oldcam: Virtual Camera Simulator" section to root `README.md` with version comparison
  table, requirements, and standalone launcher instructions.
- Added complete macOS READMEs for `oldcam-v9/macOS/` and `oldcam-v10/macOS/`.

## 2026-05-10 (v1.2)

### Fixed

- Carousel rendering reliability for valid image inputs (including `.jpeg`) by binding `PhotoImage` to an explicit Tk master.
- Carousel ingest logging mismatch: failed preflight/render paths now emit actionable errors instead of success-only add logs.

### Changed

- Added strict portable folder-tree sanitizer for `Sanitize Folder` flows:
  - preserves valid names such as `.ocr` and repeated underscores
  - only renames true cross-platform hazards (invalid chars, control chars, trailing spaces/dots, Windows reserved names)
- Release packaging now emits:
  - `SelfieGenUltimate-v1.2.zip` (canonical)
  - `SelfieGenUltimate.zip` (latest alias)

## 2026-05-04

### Added

- Documented end-to-end CLI automation pipeline flow and run/resume semantics in `README.md`.
- Added reusable test-folder guidance for repeatable batch validation:
  - `test_root/case_a/front.jpg|png`
  - `test_root/case_b/front.jpg|png`
- Added PR bot triage workflow documentation for fresh actionable feedback on latest commit range.
- Added this `CHANGELOG.md`.

### Changed

- Automation defaults:
  - Front expansion recommended/default percent changed from `30` to `70`.
  - Selfie expansion remains `30`.
- Similarity/runtime hardening:
  - Added fallback path in `similarity_engine.py` for TensorFlow/Keras runtime mismatch during face extraction.
  - Early ML backend environment bootstrap added for CLI path.
- Automation retry behavior:
  - Cases in `manual_review` because of `similarity unavailable` are now rerunnable in case planning.
- Manifest robustness:
  - `AutomationManifest.create_or_load()` now quarantines corrupt/invalid payloads and recreates a fresh manifest payload in the same invocation.
- Runnable-case selection:
  - Existing selfie/video cases are kept runnable for downstream continuation checks instead of being excluded from batch execution.

### Verified

- Offline regression suite passed for targeted automation/manifest/CLI smoke/pipeline tests.
- Two paid live end-to-end verification runs completed with strict oldcam requirement:
  - Resume path on an existing prior-failure case.
  - Clean path on a fresh root.
- Latest successful runs confirmed:
  - No current-run `KerasTensor` similarity failure.
  - Complete per-step outputs through `oldcam`.

### Docs

- Updated `README.md` for CLI automation, reusable retesting workflow, macOS compatibility constraints, and GitHub review loop.
- Updated `AGENTS.md` to reflect active pytest usage, manifest semantics, reusable test-folder practice, and macOS guardrails.
- Updated `CLAUDE.md` for current testing reality, CLI automated pipeline internals, and fresh PR bot triage workflow.
