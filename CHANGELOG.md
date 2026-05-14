# Changelog

All notable changes to this project are documented here.

## 2026-05-14 (v1.7) — Oldcam V13 "High-End Daylight" (new default)

### Added

- **Oldcam V13 (High-End Daylight)** — new default version. Pristine pipeline tuned for
  flagship-phone-in-bright-sun footage. Strips the remaining noise / AE / ghosting layers
  that V12 still applied, leaving only the geometric and optical signatures a physical
  device imposes: sub-pixel OIS jitter, CMOS rolling shutter scan-warp, highlight blooming,
  micro-luma AWB drift, radial chromatic aberration, and vignette.

  Rationale: in stable bright daylight a high-end CMOS sensor produces a flawlessly clean
  image — its ISP cleans away the FPN and temporal grain before the encoded frame ever
  reaches you. V12's `apply_modern_sensor_noise` and `apply_ae_stepping` passes were
  re-introducing degradation signals that flagship daylight footage simply doesn't carry.
  V13 also hardcodes ghosting to 0.0 (razor-sharp frames) and drops the `--grain` CLI arg.

- Four new launchers: `launchers/{windows,macos}/run_oldcam_v13.{bat,command}` plus
  hub-level chain shims. Generic `run_oldcam` launchers across all three levels now
  delegate to V13.

### Changed

- **Default oldcam version: V12 → V13.** GUI ships with V13 checked by default in the
  Video tab; CLI default is `automation_oldcam_version="v13"`. V12 remains available
  via checkbox / `--oldcam-version v12` for users who prefer the low-light realism
  profile.
- **Performance:** V13 skips per-frame FPN + temporal noise generation. Combined with
  v1.6's CRF 12 quality bump, output is both sharper *and* faster to encode than V12.
- Release stamp bumped to v1.7; release zip is `dist/SelfieGenUltimate-v1.7.zip` with
  `dist/SelfieGenUltimate.zip` alias.

### Behavioral notes

- Users upgrading from v1.6 will see V13 checked by default in the Video tab. To keep
  v1.6 behavior, uncheck V13 and check V12.
- V13 ignores `--ghosting` (hardcoded to 0.0) and does not accept `--grain` (arg removed
  from V13 parser only; V7–V12 parsers unchanged).
- V13 does not require MediaPipe — same dependency story as V12. MediaPipe-required
  versions remain V9, V10, V11.

### Quality

- **Ping-pong looper now mathematically lossless.** `kling_gui/video_looper.py`
  bumped from CRF 12 → `-crf 0 -tune film -pix_fmt yuv420p` (libx264 lossless
  within 4:2:0 constraints). Reason: Kling delivers ~28 MB H.264 clips; the
  previous CRF 12 ping-pong re-encode was dropping intermediate files to ~7 MB
  (forward + reverse should *double* duration, so this was net quality loss
  not just compression). The intermediate file is now larger by design — it's
  consumed immediately by Oldcam V13's encoder (CRF 12 / preset slow / profile
  high) which produces the final size-conscious output. End result: zero
  generational loss between Kling source and Oldcam input.
- `yuv420p` (not `yuv444p`) chosen for OpenCV decode compatibility; stream-copy
  concat rejected to avoid PTS/DTS glitches with reversed-half H.264.

### Tests

- 3 new V13 tests in `tests/test_oldcam_versions.py`: output suffix, no-MediaPipe
  preflight, `process_frame` skips noise + AE stepping. Total: **342 pass**.
- Updated existing tests for default-switch sites (5 files).

## 2026-05-14 (v1.6)

### Added

- **Oldcam V12 (Pristine Hardware-Only)** — now the default version across CLI, GUI, automation,
  and all launcher chains. Removes rPPG biological pulse, global LUT, dynamic tone mapping (CLAHE),
  and HSV saturation. Rationale: modern Presentation Attack Detection (PAD) systems flag synthetic
  2D color pulses as a spoofing signature (3D-CNN liveness models track blood propagation through
  facial geometry, which a 2D color overlay cannot replicate). The global LUT was injecting a red
  boost causing sepia tint; CLAHE was crushing local contrast. V12 keeps physical camera artifacts
  only: OIS jitter, rolling shutter, AE stepping, highlight blooming, AWB drift, sensor noise,
  chromatic aberration, and vignette.
- **V12 launchers** at all 3 levels: `launchers/windows/run_oldcam_v12.bat`,
  `launchers/macos/run_oldcam_v12.command`, `launchers/run_oldcam_v12.bat`,
  `launchers/run_oldcam_v12.command`.
- **Oldcam version (ⓘ) tooltip** rewritten with theme + trade-off thread per version, anchored to
  fact-checked code citations.
- **`docs/oldcam-versions.md`** — full V12 section + "Version History Theme & Trade-Off" table.
- **`docs/oldcam-wiring.md`** — comprehensive checklist for adding new versions (v13+).

### Changed

- **Default Oldcam version** is now **v12** everywhere:
  - GUI: v12 checkbox checked by default (v11 unchecked)
  - CLI: `automation_oldcam_version` defaults to `v12`; CLI choice menu lists all v7–v12 + "all"
  - Launchers: root `run_oldcam.bat`, `launchers/windows/run_oldcam.bat`, and
    `launchers/macos/run_oldcam.command` all chain into v12
  - `automation/pipeline.py`: fallback default in logger format strings bumped from `v8` → `v12`
- **Oldcam GUI strip** restructured: 3-column checkbox grid, "Oldcam: ⓘ" inline label,
  top-anchored Re-Run column with label-on-top + buttons-below. Strip width stays fixed as
  versions are added; buttons standardized to font 9 / `padx=8 pady=2 width=2` so the rotate
  and folder icons render at identical sizes.
- **`queue_manager.py` Popen cleanup**: bounded `wait(timeout=5)` + explicit `stdout.close()`
  in TimeoutExpired/Exception branches to prevent pipe-buffer deadlock if the child wrote
  after our last readline().
- **Log noise filter** extended to suppress MediaPipe `portable_clearcut_uploader` telemetry
  errors (`FAILED_PRECONDITION`, `Source Location Trace`, `wireless/android/play/playlog`).

### Fixed

- Updated several v10→v11 (and now v11→v12) stale strings in launcher scripts and error messages.
- README + CLAUDE.md + AGENTS.md kept in sync with the new default + new wiring doc.

### Distribution

- Release packaging emits `SelfieGenUltimate-v1.6.zip` (canonical) +
  `SelfieGenUltimate.zip` (latest alias).

### Quality

- **FFmpeg encode bumped to near-lossless across all Oldcam versions** (except v8,
  which keeps its bitrate-cap "Temporal Smartphone" character):
  - V7/V9/V10/V11/V12: `-crf 12 -preset slow -profile:v high` (was CRF 16–18,
    preset medium on V7/V9/V10).
  - Ping-pong looper (`kling_gui/video_looper.py`) also bumped to CRF 12 / preset
    slow so the intermediate looped video preserves source quality before Oldcam
    processing.
- Result: visually-lossless H.264 throughout the pipeline. Output files are
  larger but match Kling's source fidelity.

## 2026-05-13 (v1.5)

### Added

- **Oldcam V11 (Spatial Sync + AWB Drift)**: Combines V10's FFT-based biological pulse with V9's
  AWB (Auto White Balance) drift hardware simulation. Signal ordering enforced: FFT reads the clean
  green-channel history buffer before AWB drift corrupts global channel values, so the two systems
  stack without interference. New `oldcam-v11/` standalone folder with Windows `oldcam_launcher.bat`
  and macOS `oldcam.command`. V11 is now the default version in the GUI and CLI automation pipeline.
- **V11 launchers**: `launchers/windows/run_oldcam_v11.bat`, `launchers/macos/run_oldcam_v11.command`,
  `launchers/run_oldcam_v11.bat`, `launchers/run_oldcam_v11.command`.
- **Generic `run_oldcam` launchers** updated to delegate to V11 (previously V9).
- **Oldcam version tooltip**: (ⓘ) hover icon added next to the Oldcam version checkboxes in the GUI.
  Shows a version comparison table (face tracking, biological pulse, AWB drift, MediaPipe, signature).
- **Oldcam wiring reference** (`docs/oldcam-wiring.md`): complete checklist for adding new Oldcam versions
  (v12+), covering algorithm folder structure, launchers at all 3 levels, GUI checkbox wiring, mediapipe
  flag, tests, distribution, and signal pipeline order invariant. Linked from CLAUDE.md, AGENTS.md, README.

### Fixed

- **V11 motion stutter**: Removed every-other-frame MediaPipe frame-skip (`detect_frame_count % 2`)
  that caused the face mask to freeze one frame while the face moved, creating a 15fps ghost on 30fps
  video. MediaPipe now runs 1:1 on every frame.
- **V11 AE stutter**: Removed `stutter_budget` / AE-step frame-repeat logic that intentionally wrote
  the previous frame instead of the current one. Every input frame now produces a distinct output frame.
- **V11 sepia/warm tint**: Removed `shift_intensity * 0.5 * mask * 3.0` ambient-warmth line that
  injected raw red channel values on top of the balanced BGR biological pulse shifts. AWB drift
  neutralized from red+green bias (`drift*0.35 / drift*0.15`) to equal-channel luma drift (`image_f += drift`).
- **V11 output quality**: FFmpeg encoding upgraded from CRF 18 + `preset medium` to CRF 16 +
  `preset slow` for visually lossless output closer to source file size.
- **Oldcam progress streaming**: `_run_oldcam_version` switched from `subprocess.run(capture_output=True)`
  (silent until completion) to `Popen` with `readline()` streaming so frame progress (25%, 50%…) appears
  in the GUI log in real time. Deadline-aware loop prevents silent hangs.
- **TF/MediaPipe noise filter**: `_is_tf_noise` function added; bare `"mediapipe"` pattern replaced
  with specific startup-only substrings to avoid masking real import errors.
- **Layout**: `sash_prompt_split` widened from 50–62% to 54–64% (default 56% → 60%) so the Oldcam
  version checkboxes row no longer crushes the folder-icon button.

### Changed

- Release packaging emits `SelfieGenUltimate-v1.5.zip` (canonical) + `SelfieGenUltimate.zip` (alias).

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
