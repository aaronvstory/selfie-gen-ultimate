# Changelog

All notable changes to this project are documented here.

## 2026-05-15 (v1.9) — macOS launcher resilience + recalibrated similarity curve

### Fixed

- **Standalone Similarity GUI no longer dies on macOS with "Unsupported Python
  version" when the repo has a stale `.venv/` symlinking to python3.13+.** The
  resolver in `similarity/run_gui.command`, `similarity/run_cli.command`, and
  `similarity/run_gui.bat` now version-validates *every* candidate venv (and the
  auto-create path) instead of accepting the first executable interpreter found.
  Adds `.venv311/` as an explicit candidate (the venv name CLAUDE.md prescribes
  on macOS), reorders the macOS fallback chain to prefer `python3.11` first
  (only Homebrew Python with bundled `_tkinter`), and split the post-resolve
  error message to distinguish override-pointing-at-bad-python from resolver
  bugs.
- **`similarity/main.py` now adds the repo root to `sys.path`** so the
  standalone Similarity app can import shared modules (`tk_dialogs`,
  `similarity_engine`, `face_similarity`) that live one directory up. Without
  this, launching the standalone GUI crashed at import time with
  `No module named 'tk_dialogs'`.
- **`launchers/macos/run_gui.command` shebang aligned to siblings** —
  `#!/bin/bash` → `#!/usr/bin/env bash` (parity with `run_cli.command`,
  per CodeRabbit review on PR #21).

### Changed

- **Recalibrated similarity polynomial curve.** `PASS_CURVE_EXPONENT` lowered
  from `2.5` (v1.8) to `0.5` (square root). Reference points (threshold 0.68):
  - distance 0.05 → **94.58%** (was 99.97%)
  - distance 0.10 → **92.33%** (was 99.83%)
  - distance 0.15 → **90.61%** (was 99.55%)
  - distance 0.20 → **89.15%** (was 99.06%)
  - distance 0.30 → **86.72%** (was 97.42%)
  - distance 0.50 → **82.85%** (was 90.71%)
  - distance 0.68 → 80.00% (unchanged — ArcFace official threshold pin)

  Rationale: modern AI edit models (Nano Banana 2 Edit, FLUX Kontext) preserve
  identity strongly enough that the typical post-generation cosine distance
  sits in 0.05–0.15. v1.8's exponent 2.5 compressed that whole band into
  99–100%, making the score visually indistinguishable from a degenerate
  fallback. v1.9 spreads the band across 95–91% so the score conveys
  meaningful gradation while still guaranteeing 100% at distance 0 and 80% at
  the official threshold. Updated `similarity/CLAUDE.md` accordingly.

- **User-facing similarity log line now includes the raw cosine distance,
  threshold, and per-model breakdown:**
  ```text
  [Nano Banana 2 Edit] Similarity: 92% (cosine_distance=0.083, threshold=0.68, models=ArcFace+Facenet512)
  ```
  Previously only the mapped score was logged, so a 99% reading was
  indistinguishable from a degenerate fallback. Falls back to bare
  `Similarity: NN%` when diagnostics are absent (DeepFace unavailable).

## 2026-05-14 (v1.8) — KYC-grade similarity scoring

### Added (v1.8 follow-up — full-stack FAS toggle integration)

- **`Anti-spoof` checkbox in the main GUI carousel**, sitting next to the existing `Auto`
  checkbox. Toggling it flips `FaceEngine.anti_spoofing` and immediately triggers a fresh
  batch recompute via `_calc_all_similarity()` so on-screen scores reflect the new state.
- **`LIVE ✓ / LIVE ✖` PASS/FAIL chip** rendered alongside the SIM badge in the carousel
  meta row. Sourced from `diagnostics.anti_spoofing.{ref,target}.spoof_detected`. The chip
  is hidden when `anti_spoofing=false` (no FAS data on the result).
- **Carousel pane widened from ~24% → ~26% of window width** (clamp range
  20–30% → 22–32%, default 220 px → 260 px) to fit the new checkbox + chip without
  crowding the SIM badge. Stale `sash_*` keys are cleared from `kling_config.json` so
  users see the new defaults on first launch.
- **Standalone CLI `--anti-spoof` / `--no-anti-spoof` flag** (uses
  `argparse.BooleanOptionalAction`) plumbed through `apply_runtime_config(anti_spoofing=...)`
  to set `engine.anti_spoofing` before any comparison. The Rich result panel now also
  emits a `Liveness (anti-spoof): PASS/FAIL` line when FAS data is present.
- **Standalone GUI `Anti-spoof (face liveness)` checkbox** above the Run button.
  Toggling auto-re-runs the comparison if both images are loaded. A second result label
  surfaces `Liveness (anti-spoof): PASS` (green) or `FAIL` (red).
- **`automation_similarity_require_fas_pass` config key** (default `false`). When set
  to `true`, the automation pipeline routes any case whose ref OR target image is
  flagged as spoofed to `manual_review` even if the similarity score passes. This
  upgrades FAS from observational to enforcement for KYC-strict deployments.
- **Stale-score invalidation on session load.** Sessions saved by v1.7 (or earlier)
  carry similarity scores produced by the old linear formula. On load, the GUI now
  detects the absence of `similarity_engine_version: "1.8"` in the session JSON and
  drops the stored scores so the carousel auto-recomputes them with the v1.8 engine.
  Manual user overrides (`similarity_override=true`) are always preserved. v1.8+ saves
  always include `"similarity_engine_version": "1.8"` at the top level of the session
  JSON so this only happens once per legacy session.
- **Filename `_sim{N}` continues to embed the score at generation time.** That tag is
  now produced by the v1.8 polynomial curve (and ensemble + FAS when enabled), so new
  outputs may carry slightly different numeric tags than v1.7 outputs sitting on disk.
  The on-screen SIM badge always reflects a fresh recompute — the embedded tag is a
  permanent record of the score at the moment of generation, not a runtime cache.

### Added

- **Polynomial easing curve** for similarity scores. Replaces the linear distance→score
  mapping in `similarity_engine._score_from_distance` with a curve
  (`80 + 20*(1 − r^2.5)` for matches, `79*(1 − r^0.5)` for non-matches). Distance 0 still
  scores 100; the cosine threshold (0.68) still scores exactly 80. Borderline matches now
  spread across the high 80s instead of bunching into the 82–86% band that previously
  trapped most AI-generated selfies.

- **Multi-model ensemble (ArcFace + Facenet512).** Cosine distances from both DeepFace
  models are averaged before scoring. Two independently trained embedding spaces voting
  on identity catch synthetic-texture artefacts that single-model ArcFace misses. Toggle
  via `automation_similarity_use_ensemble` (default `true`). Falls back to primary-only
  automatically if the secondary model errors at runtime.

- **Anti-spoofing (FAS) diagnostics** via DeepFace `anti_spoofing=True`. Per-face
  `is_real` and `antispoof_score` flow into `diagnostics.anti_spoofing`. **Log-only** —
  the FAS verdict does not gate the pipeline (FAS false-positive rates are too high to
  hard-block on; pass/fail still keys off the similarity score). Toggle via
  `automation_similarity_anti_spoofing` (default `true`).

- New diagnostic fields on every similarity result:
  - `diagnostics.per_model_distances` — `{ "ArcFace": 0.21, "Facenet512": 0.18 }`
  - `diagnostics.anti_spoofing` — per-face `{ is_real, antispoof_score }` records and
    a `spoof_detected` summary flag for ref/target.

- Three new keys in `kling_config.json`:
  - `automation_similarity_use_ensemble` (default `true`)
  - `automation_similarity_secondary_model` (default `"Facenet512"`)
  - `automation_similarity_anti_spoofing` (default `true`)

### Changed

- `FaceEngine.initialize_models` warms both primary and secondary embedding models when
  ensemble is enabled, so first-comparison latency does not pay the Facenet512
  cold-start tax.
- Pipeline summary log line now includes `per_model=…`. A separate WARNING line
  (`anti-spoofing warning ref=… target=…`) appears when FAS flags any input.
  Pipeline routing is **unchanged** — FAS is observational only.

### Performance

- Similarity check now runs ~2× DeepFace.represent calls per image pair when ensemble
  is on. FAS adds ~50 ms per face. Typical per-comparison cost: ~600 ms → ~1.3 s on CPU.
- To restore v1.7 single-model speed without losing the polynomial curve, set
  `automation_similarity_use_ensemble=false`.

### Backward compatibility

- All public return shapes preserved. `compare_images`, `compute_face_similarity_details`,
  and `compute_face_similarity` return the same dict keys / scalar types.
- Filename `_sim{N}` tagging in `selfie_generator.py` unchanged.
- OpenCV fallback path remains primary-model-only with no FAS — fallback fires only when
  the DeepFace TF runtime is broken; adding a second TF model would defeat the purpose.
- `automation_similarity_threshold` default unchanged (80). Pass/fail decisions at the
  threshold are unchanged.

### Behavioral notes

- Cases that scored 90–92 in v1.7 may now score 86–89 in v1.8. The 80 gate is
  unchanged; logs will look tighter.
- To restore v1.7 scoring entirely, set `automation_similarity_use_ensemble=false` and
  `automation_similarity_anti_spoofing=false`. The polynomial curve itself is **not**
  gated — it is the new scoring contract.
- No Windows/macOS launcher changes required: the GUI and CLI wrappers
  (`launchers/windows/run_gui.bat`, `launchers/macos/run_gui.command`,
  `gui_launcher.py`, `kling_automation_ui.py`) all bootstrap into the same Python
  module tree, and `face_similarity._get_engine` automatically loads the new config
  keys at runtime. PyInstaller builds (`build_gui_exe.bat`, `kling_gui_direct.spec`)
  remain compatible — no new dependencies, no new resource files.

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

### Fixed

- **Re-Run Oldcam now honors the Loop checkbox.** Previously, the
  `rerun_oldcam_only()` worker in `kling_gui/queue_manager.py` hardcoded its
  input to the un-looped source and ignored `config["loop_videos"]`, so users
  who pressed Re-Run with Loop checked got `..._k25tStd-oldcam-vN.mp4` built
  directly on the 28 MB Kling source instead of the expected
  `..._k25tStd_looped-oldcam-vN.mp4` built on a freshly-looped intermediate.
  The fix mirrors the normal queue path (loop → Oldcam) and overwrites any
  stale `_looped.mp4` with the v1.7 lossless looper output. Re-Run on a
  source whose stem already ends in `_looped` skips the loop step to avoid
  `..._looped_looped.mp4`.

- **Looper FFmpeg crash on strict libx264 builds.** The v1.7 looper used
  `-profile:v high -crf 0 -tune film -pix_fmt yuv420p`, which crashes on
  strict libx264 builds with `Could not open encoder before EOF` /
  `Invalid argument (-22)`. Root cause: H.264 true-lossless coding
  requires the High 4:4:4 Predictive profile, not plain High, but we need
  yuv420p downstream for OpenCV compatibility. Replaced with the canonical
  `-qp 0 -preset slow -pix_fmt yuv420p` idiom (no `-profile:v`, no `-tune`)
  which works on every libx264 build. libx264 picks the appropriate profile
  internally based on pix_fmt + quantizer. Intermediate file is ~5-15%
  larger than `-crf 0` but Oldcam re-encodes it immediately, so size of
  the throwaway intermediate is irrelevant.

### Logging & UX

- **Verbose Mode checkbox now properly gates panel verbosity.** The
  existing "Verbose Mode" checkbox in the config panel previously only
  controlled per-frame generator progress callbacks (a small slice of
  logging). `KlingGUIWindow._log()` now consults `verbose_gui_mode`: when
  OFF (default), `"debug"`-level emits stay out of the panel and go only
  to `kling_gui.log`; when ON, the panel ALSO shows the debug stream
  (raw FFmpeg stderr, subprocess path dumps, all the demoted duplicates
  below) so power users can see everything without opening the log file.

- **5 more panel duplicates removed/demoted to debug:**
  - `video_looper.py`: dropped the second `Creating looped video: <name>`
    line (queue_manager already prints a friendlier `Creating looped
    video...` immediately before it) and demoted the `Running FFmpeg...`
    beat to file-only.
  - `queue_manager._loop_video`: the wrapper's basename-only
    `Looped video saved: <name>` emit is now debug-level; the looper
    itself already prints the user-facing `Looped video saved: <name>
    (X.Y MB)` success line with file size.
  - `queue_manager` Re-Run path: `Re-Run loop intermediate: <name>` is
    now debug — the user just saw the looper's success line for the
    same file one line above.
  - `queue_manager._oldcam_video`: `Oldcam selected: running v13`
    demoted to debug — the per-version `Applying Oldcam vN Finish...`
    and `Oldcam vN Finish applied: <name>` panel pair already conveys
    which version ran.
  - `queue_manager._oldcam_video`: the success-summary line
    `Oldcam summary: requested versions=...; succeeded versions=...;
    primary output=<full path>` is now debug — `Oldcam vN Finish
    applied: <name>` (already in the panel) plus `main_window`'s
    final `Oldcam-only rerun complete: <src> → <output>` cover the
    user-facing summary without the full-path noise.

  Net effect: a Re-Run with Loop + V13 now produces ~9 panel lines
  instead of ~17. File log content is unchanged (everything still
  recorded at DEBUG level).

- **`verbose_gui_mode` default flipped True → False** with a one-shot
  migration in `_load_config._migrate_legacy_defaults`. Pre-v1.7 installs
  shipped Verbose Mode ON, which dumped raw FFmpeg stderr, subprocess
  path lines, and every demoted summary into the panel — even after the
  v1.7 logging cleanup, existing users still saw the noisy stream because
  their saved config explicitly carried the old True. The migration flips
  legacy `True` → `False` exactly once (stamped via the
  `verbose_gui_mode_migrated_v17` flag) so users who later opt INTO
  verbose mode aren't overridden on subsequent boots. New regression test
  `test_default_oldcam_version_is_v13_across_all_layers` asserts CLI / GUI
  / launcher chains all agree on v13 across Windows + macOS; another test
  asserts the verbose default + migration semantics.

- **`Oldcam rerun increment target: <next-name>` line demoted to debug.**
  Pure preview of the filename Oldcam is about to write; the
  `Oldcam vN Finish applied: <name>` panel line ~10 s later shows the
  actual file.

- **In-app log panel decluttered (file log retains everything).** Added a
  `"debug"` level to `KlingGUIWindow._log()` that routes to the rotating
  file handler (`~/.kling-ui/kling_gui.log`) only — never to the user-facing
  panel. Applied at three known noisy sites:
  - `video_looper.py`: FFmpeg failure now emits a single friendly one-liner
    to the panel (e.g., `"Loop encode failed: FFmpeg could not open the
    H.264 encoder (libx264 init failed)"`) with the full multi-line stderr
    blob going to the file log at debug level. New `_summarize_ffmpeg_error()`
    helper does priority-ordered classification: encoder init, invalid
    argument, missing file, permission denied, generic conversion failure.
  - `queue_manager.py`: subprocess stdout lines matching the new
    `_PANEL_NOISE_PATTERNS` tuple (`"Input :"`, `"Output:"`, `"Saved video
    to:"`, `"Video processing complete."`, `"Finalizing video with FFmpeg
    codec"`) are routed to file-only. Progress lines (`[Oldcam] Processing:
    N% complete`) and friendly summaries still show in the panel.
  - Duplicate `"Oldcam-only rerun summary:"` and basename-only
    `"Oldcam-only rerun complete:"` emits demoted to debug; `main_window.py`
    already emits the friendlier `<source> → <output>` arrow line for the
    panel.
- File logger level bumped from `INFO` → `DEBUG` so the demoted lines
  actually reach the disk file for diagnostics. Log file rotation
  (5 MB × 3 backups) unchanged.

### Tests

- 3 V13 algorithm tests + 4 Re-Run loop-wiring tests + 2 logging-UX tests
  in `tests/test_oldcam_versions.py`; new `tests/test_video_looper.py`
  with 12 tests covering `_summarize_ffmpeg_error` priority order,
  the canonical `-qp 0` cmd structure (no `-profile:v`, no `-tune`,
  no `-crf`), and the friendly-vs-debug stderr split on failure.
  Total: **370 tests** all passing.
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
