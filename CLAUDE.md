# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

Kling UI is an AI media generation toolkit using fal.ai and BFL APIs. It provides a 5-tab Tkinter GUI for face cropping, portrait analysis, selfie generation, image outpainting, and batch video generation, plus a CLI mode via Rich.

## macOS Portability — MANDATORY (Windows agents read this first)

This repo runs on **both Windows and macOS**. Most contributors edit on
Windows, and CI is Windows-leaning. Several macOS-runtime issues recur — the
full set of 14 binding rules (LF line endings, exec bit, `tk_dialogs` usage,
path-separator asserts, sys.modules mock gotchas, `python3.11` requirement,
the launcher chain, the portability gate, venv-resolver version-validation,
`set`-flag parity, `mac_padding` hit-targets, **Tk-dep `osx-arm64` Tcl-ABI
check**, **`sys.platform`-pin for CUDA-mock tests**, and
**`dependency_checker.py` `pip_name` spec carry**) lives in:

> **➡️ [`docs/macos-portability.md`](docs/macos-portability.md) — READ BEFORE
> editing any `.sh`/`.command`, launcher, file dialog, or path-handling code.**

This is not optional reading: each of those rules has caused a real macOS
breakage. Open that file whenever your change touches shell scripts, launchers,
`tk_dialogs.py`, `similarity/src/`, or `kling_gui/main_window.py` picker code.

**Authoring on Windows?** Run the
[`docs/macos-readiness-for-windows-authors.md`](docs/macos-readiness-for-windows-authors.md)
5-minute checklist BEFORE pushing. It's the proactive complement to the
runtime rules above — wheel-inspection commands, test-author patterns,
and PR-description templates that prevent the macOS-sync round-trip.

## Commands

```bash
# Setup
python -m venv venv && venv/Scripts/pip install -r requirements.txt

# Run CLI (menu-driven)
python kling_automation_ui.py

# Launch GUI directly
python -c "from kling_gui import KlingGUIWindow; KlingGUIWindow().run()"

# Check/install dependencies
python dependency_checker.py

# Build standalone EXE (PyInstaller)
build_gui_exe.bat

# Type checking
npx pyright  # uses pyrightconfig.json (basic mode, Python 3.10)
```

Pytest suites are present and should be used for targeted regression checks:

```bash
pytest tests/test_automation_pipeline.py -q
pytest tests/test_automation_manifest.py -q
pytest tests/test_automation_cli_smoke.py -q
```

Manual GUI and live provider validation are still required for full end-to-end confidence.

## Architecture

### Entry Points

- `kling_automation_ui.py` — CLI menu system (Rich UI). Option 6 launches GUI.
- `kling_gui/main_window.py` — GUI entry. Creates `ttk.Notebook` with 5 tabs + `ImageCarousel` + `ComparePanel`.
- `gui_launcher.py` — PyInstaller-compatible GUI bootstrap.
- `launchers/windows/run_gui.bat` and `launchers/windows/run_cli.bat` — canonical Windows launchers.
- `launchers/macos/run_gui.command` and `launchers/macos/run_cli.command` — canonical macOS launchers.
- root `run_gui.bat`, `run_cli.bat`, `run_gui.command`, `run_cli.command` are compatibility wrappers.
- `run_cli.sh` / `run_gui.sh` — macOS shell launchers used by `.command` entrypoints.
- `setup_macos.sh` — macOS environment bootstrap for Tk-capable runtime.

### CLI Automated Pipeline

The CLI includes a manifest-driven automated pipeline:

`front_expand -> extract_portrait -> selfie_generate -> similarity_gate -> selfie_expand -> video_generate -> rppg -> oldcam`

(`rppg` is opt-in and runs immediately after `video_generate` per
the Phase E ordering — same as the GUI queue. The legacy "rPPG
strictly LAST, after oldcam" arrangement is preserved behind the
`automation_rppg_per_oldcam_fanout` opt-in flag (default OFF) for
the rare case where a fresh-pulse oldcam variant is needed. See
"rPPG Injection Wiring" below.)

Core pipeline modules:

- `automation/config.py`
- `automation/discovery.py`
- `automation/manifest.py`
- `automation/pipeline.py`

Manifest behavior supports repeated reruns on the same test folders and run/resume continuation.

### GUI Tab Architecture (`kling_gui/tabs/`)

| Tab | File | Class | Purpose |
|-----|------|-------|---------|
| 0. Face Crop | `face_crop_tab.py` | `FaceCropTab` | Extract 3:4 passport face crops via RetinaFace (optional deps: cv2, retinaface) |
| 1. Prep | `prep_tab.py` | `PrepTab` | Vision AI portrait analysis (OpenRouter API) |
| 2. Selfie | `selfie_tab.py` | `SelfieTab` | Generate selfies from identity reference via fal.ai + BFL |
| 3. Outpaint | `outpaint_tab.py` | `OutpaintTab` | Expand images using fal.ai outpaint |
| 4. Video | `video_tab.py` | `VideoTab` | Batch video generation (wraps ConfigPanel + DropZone + Queue) |

All tabs share an `ImageSession` instance for image pipeline state and a `log_callback` for the unified `LogDisplay`.

### Image Pipeline State (`image_state.py`)

`ImageSession` tracks images through the multi-tab pipeline. Each `ImageEntry` has a `source_type` ("input", "selfie", "outpaint") and flows through:

```
Input image → Face Crop → Prep (analyze) → Selfie (generate) → Outpaint (expand) → Video (animate)
```

The `ImageCarousel` widget provides visual navigation; `ComparePanel` offers side-by-side comparison with independent navigation.

### Backend Generators

All generators follow the same pattern: `set_progress_callback(cb)` for GUI logging, `_report(msg, level)` internally.

| Module | Class | External API |
|--------|-------|-------------|
| `kling_generator_falai.py` | `FalAIKlingGenerator` | fal.ai queue API (video gen) |
| `selfie_generator.py` | `SelfieGenerator` | fal.ai (FLUX PuLID, Instant Character, etc.) + BFL (Kontext Pro/Max, FLUX 2 Pro) |
| `outpaint_generator.py` | `OutpaintGenerator` | fal.ai outpaint |
| `vision_analyzer.py` | `VisionAnalyzer` | OpenRouter chat completions (vision) |
| `selfie_prompt_composer.py` | `SelfiePromptComposer` | None (local prompt assembly) |

### Dual API Providers in Selfie Generator

`SelfieGenerator` supports two providers selected per-model via `AVAILABLE_MODELS[].provider`:
- **fal.ai** (default): Uses `fal_utils.fal_queue_submit/poll` pattern. Needs freeimage.host upload first.
- **BFL** (`provider: "bfl"`): Direct REST API to `api.bfl.ai`. Uses base64 image encoding + polling. Needs separate `bfl_api_key`.

The provider is selected automatically based on the model's `provider` field. BFL models have `api_url` pointing to `api.bfl.ai/v1/...`.

### Shared Utilities

| Module | Purpose |
|--------|---------|
| `fal_utils.py` | `upload_to_freeimage()`, `fal_queue_submit()`, `fal_queue_poll()`, `fal_download_file()` — shared by all fal.ai generators |
| `model_metadata.py` | Loads endpoint list from `models.json`, provides `get_model_by_endpoint()`, `get_model_display_name()`, `get_prompt_limit()` |
| `model_schema_manager.py` | Queries and caches fal.ai OpenAPI schemas (TTL-based, disk + memory cache at `~/.kling-ui/model_cache/`) |
| `path_utils.py` | PyInstaller-compatible path resolution (`get_app_dir()`, `get_config_path()`, `is_frozen()`, `VALID_EXTENSIONS`) |

### Data-Driven Model Configuration

**Video models** are defined in `models.json` as a list of dicts with `endpoint`, `name`, and `release` fields. `model_metadata.py` provides `get_model_by_endpoint()`, `get_model_display_name()`, etc. The file also has a `user_notes` map for per-model descriptions. At runtime, `ModelSchemaManager` enriches models with pricing and parameter info from the fal.ai API.

**Selfie models** are hardcoded in `SelfieGenerator.AVAILABLE_MODELS` as a list of dicts with `endpoint`, `label`, `slug`, `provider`, `api_url`.

### Known Code Inconsistency

`config_panel.py` duplicates a `COLORS` dict and `FONT_FAMILY` instead of importing from `theme.py`. All other modules use `theme.py`. When editing theme values, update both locations or refactor to use the single source.

### API Keys Required

| Key | Config Field | Used By |
|-----|-------------|---------|
| fal.ai | `falai_api_key` | All fal.ai generators (video, selfie, outpaint) |
| Freeimage.host | `freeimage_api_key` | Image uploads (fal.ai requires public URLs) |
| BFL (Black Forest Labs) | `bfl_api_key` | FLUX Kontext / FLUX 2 Pro selfie models |
| OpenRouter | `openrouter_api_key` | Vision analysis in Prep tab |

### Configuration (`kling_config.json`)

Auto-generated, persisted JSON. Defaults for new installs come from `default_config_template.json` (only covers video prompts + model selection).

Key sections:
- **Video**: `saved_prompts` (6 slots), `negative_prompts`, `current_model`, `video_duration`, `loop_videos`
- **Selfie**: `selfie_selected_models`, `selfie_prompt_template`, `selfie_scene_templates`, `selfie_prompt_mode` (json_handoff or wildcard), `selfie_wildcard_template`, `selfie_id_weight`, `selfie_width/height`
- **Outpaint**: `outpaint_expand_*` (L/R/T/B), `outpaint_prompt`, `outpaint_format`, `outpaint_expand_mode` (pixels or percentage)
- **Vision**: `openrouter_api_key`, `openrouter_model`, `openrouter_vision_system_prompt`
- **Face Crop**: `face_crop_multiplier`, `face_crop_auto_switch`
- **UI state**: `window_geometry`, `sash_*` positions

### Key Data Flows

**Selfie generation (two paths):**
```
json_handoff mode:  Portrait → VisionAnalyzer (OpenRouter) → JSON traits → SelfiePromptComposer template → fal.ai/BFL model
wildcard mode:      Wildcard template with {opt1|opt2|opt3} → resolve_wildcards() → fal.ai/BFL model
```
Vision analyzer returns structured JSON (`hair`, `skin`, `eyes`, `face_shape`, `age_range`, `gender`, `clothing`, `expression`). FLUX PuLID gets an extra realism suffix appended. DeepFace computes face similarity scores post-generation.

**Video generation:**
```
Image → freeimage.host upload → fal.ai queue submit → poll status_url → download .mp4
```
Output naming: `{imagename}_kling_{model_short}_{pN}.mp4` (model short names derived in `queue_manager._model_short_from_endpoint()`)

### Threading Model

- GUI updates via `root.after()` for thread-safe Tkinter calls
- `QueueManager` uses `threading.Lock()` on its items list, daemon worker thread
- All generators run in background threads spawned by their respective tabs
- `ModelSchemaManager` has its own `threading.Lock()` for cache access
- Balance tracker (optional) runs headless Chrome in a daemon thread

### Build / Distribution

- `build_gui_exe.bat` runs PyInstaller with `kling_gui_direct.spec`
- `hooks/hook-tkinterdnd2.py` — custom PyInstaller hook for tkinterdnd2
- `path_utils.py` ensures correct paths whether running as script or frozen exe
- `create_icon.py` generates `kling_ui.ico` from scratch
- Output goes to `dist/KlingUI/`

## Default Workflow After Feature Work (MANDATORY)

The default loop whenever feature/fix work reaches a runnable state:
**branch → commit → push → open PR → trigger bots + spawn code-reviewer
subagent in PARALLEL → address findings autonomously per the rubric →
loop until clean → hand off for merge.** Don't ask permission for
intermediate steps; do not work on `main`.

> **➡️ Full contract: [`docs/pr-review-loop.md`](docs/pr-review-loop.md)**
> — 9 steps including the autonomous-loop directives, per-bot disposition
> table, finding triage rubric (CRITICAL/HIGH same round, MEDIUM unless
> >2h, LOW defer to PR-close), bot-comment poll snippets for bash AND
> PowerShell, inline-reply pattern, round wrap-up template, skip
> conditions, and the post-merge SSD refresh.

The agent-private mirror lives in
`feedback_autonomous_pr_review_loop` + `feedback_dont_defer_fix_everything`.

Quick reference — the pre-commit invariants this loop enforces:

- `bash scripts/check_macos_portability.sh` exits 0
- `pytest tests/ similarity/tests/ -q` passes (macOS) or equivalent (Windows)
- EOL invariants preserved per `git ls-files --eol`
- No `nul` file in tree

Commit-message contract: type prefix (`feat:` / `fix:` / `perf:` / `chore:` /
`refactor:` / `docs:`) + one-line subject + body with the WHY + last line
`Co-Authored-By: Claude Opus <noreply@anthropic.com>`.

## Hard Rules — Windows Launchers + GUI Sash Layout (NON-NEGOTIABLE)

Violating these has caused repeated launch breakage and garbled commits. The
full rule set — `.bat`/`.cmd` CRLF + PowerShell-write requirement, `echo(` vs
`echo.`, log-append redirect order, the `root → launchers → launchers/windows`
chain, the dep-skip stamp, MediaPipe `--no-deps`, the autocrlf diff-verify
trap, and all the GUI sash-layout clamp/restore rules — lives in:

> **➡️ [`docs/windows-launcher-and-sash-rules.md`](docs/windows-launcher-and-sash-rules.md)
> — READ BEFORE editing any `.bat`/`.cmd`, the launcher chain, or sash-layout
> code in `kling_gui/layout_utils.py` / `main_window.py`.**

Two rules are quoted here because they bite on almost every commit:
- **Never use `Write`/`Edit` on `.bat`/`.cmd`** — they emit LF and garble batch
  files. Write via PowerShell `WriteAllText` with explicit `` `r`n ``.
- **After every `Edit`/`Write` on a tracked file, run `git diff --stat <file>`
  before committing.** A whole-file change for a small edit = an EOL flip;
  restore the committed EOL before staging (autocrlf trap, see the doc).

---

## Concurrent launches & workspaces (PR #49)

The GUI is safe to launch multiple times concurrently. Each process gets an
isolated runtime directory keyed by `<YYYYMMDD-HHMMSS>-<PID>`, so carousel
state, video history, and crash logs from one window never bleed into another.
Named workspaces (`--workspace shoot-a` / `KLING_WORKSPACE=shoot-a`) give
fully isolated state trees.

**Design rule for new on-disk state (the part that bites):** before adding any
file the GUI writes at runtime, classify it as **Shared** (cross-instance,
last-writer-wins — use `path_utils.get_user_data_dir()`) or **Per-instance**
(use `path_utils.get_runtime_dir()`). Don't introduce new shared writable files
without documenting them — that's exactly the bug PR #49 fixed.

The full state-classification table, workspace-name rules, per-launch env vars,
and the `.launcher_state/setup.lock` bootstrap-mutex details live in:

> **➡️ [`docs/concurrent-launches-workspaces.md`](docs/concurrent-launches-workspaces.md)
> — READ BEFORE adding GUI-written runtime files or touching workspace/runtime
> logic.**

---

## Hard Rules — GUI Sash Layout

### Sash positions are restored from `kling_config.json` on every launch

Saved sash values from `kling_config.json` are applied by `_restore_sash_positions()` and by `_show_right_pane()` after the window is built. Editing defaults alone does nothing if stale values are already saved. **When changing layout targets:**

1. Update clamp ranges in `kling_gui/layout_utils.py` → `sanitize_sash_layout()`
2. Update fallback defaults in `kling_gui/main_window.py` (the `UI_CONFIG_DEFAULTS` dict and `_show_right_pane` fallback)
3. Clear stale values from `kling_config.json` (it is gitignored — delete the sash keys directly):
   ```python
   import json; c=json.load(open('kling_config.json'))
   for k in ['sash_dropzone','sash_queue','sash_log','sash_log_drop_split','sash_prompt_split']: c.pop(k,None)
   json.dump(c,open('kling_config.json','w'),indent=2)
   ```

### `sash_log_drop_split` is relative to the right section, not the full window

`log_drop_paned` lives inside the space to the right of the carousel (`safe_w - sash_queue`). Clamping it as a % of `safe_w` allows values larger than the pane itself. Always compute the right-section width first:

```python
clamped_queue = max(queue_min, min(int(sash_queue) if sash_queue else queue_default, queue_max))
right_section_w = max(400, safe_w - clamped_queue)
log_drop_min = max(220, int(right_section_w * 0.55))
log_drop_max = max(log_drop_min, int(right_section_w * 0.82))
log_drop_default = int(right_section_w * 0.71)
```

### Current target proportions (1600px-wide window)

> **Single source of truth:** `kling_gui/layout_utils.py::sanitize_sash_layout()`.
> These proportions are tuned per direct user feedback and are intentionally
> revised over time (current values reflect the v5.2 layout pass: wider
> carousel + wider log at the drop zone's expense). When they change, update
> this table to match the code — do **not** treat the code as drifting from
> this table.

| Sash | Target | Range |
|------|--------|-------|
| `sash_prompt_split` (left tabs width) | 72% of window | 400px–82% |
| `sash_queue` (carousel width) | 25% of window | 22–32% |
| `sash_log_drop_split` (log width within right section) | 71% of right section | 55–82% |

> Note on `sash_prompt_split`: bumped from 60% → 72% on 2026-05-22 per user
> feedback that the Step 3 (Video) horizontal controls — model row + output
> row + Oldcam/rPPG checkboxes + Re-Run buttons — were getting clipped against
> the right prompt panel at 60%. The right prompt panel only needs ~28-30%
> to comfortably show its slot picker + title + positive/negative prompt
> previews. If the table above shows a different value than `layout_utils.py`,
> trust the code; this table is a documentation mirror.

---

## Oldcam Version Wiring

Full wiring checklist with per-layer tables: [`docs/oldcam-wiring.md`](docs/oldcam-wiring.md).

**Current default version:** v24 (Crush Laundromat; superseded v15). The skipped
app-version range v16-v23 are bench experiments in `oldcam-testing/` evaluated
against the resemble-score metric — which turned out NOT to be the metric Persona
actually uses downstream, so the experiments are paused-pending-re-evaluation,
not rejected. v14 / v15 / v24 do **not** use mediapipe; v9 / v10 / v11 do.

**Auto-discovered (no changes needed):** `_discover_oldcam_versions()` in
`queue_manager.py` scans `oldcam-v*` dirs; output filename suffix is generic;
`automation/pipeline.py` is fully version-agnostic.

---

## rPPG Injection Wiring

Full wiring tables + harness usage: [`docs/rppg-wiring.md`](docs/rppg-wiring.md).

**Pipeline order (Phase E, 2026-05-22):** `Kling → rPPG → Loop → Oldcam`.
rPPG injects on the raw Kling frames so the per-iter PID stabilizes against
clean (un-resolution-crushed) source. Loop's ping-pong reverse remains
physiological because the sub-perceptual amplitude stays well below the
visibility threshold, and Oldcam's chain preserves the injected pulse.
The slower legacy "rPPG strictly LAST" (one rPPG'd file per Oldcam
version) is preserved behind the `rppg_per_oldcam_fanout` opt-in flag
— default OFF. **rPPG itself is OFF by default everywhere — opt-in only.**
Invokes the `rPPG/` tool; degrades gracefully (skip + log, never crash)
when the tool is missing or fails. On graceful skip the GUI queue
inserts a `-NORPPG` marker into the filename so the final delivered
video unambiguously reflects that rPPG was REQUESTED but did NOT land.

**Injector contract gotcha you WILL hit:** `--output` is NOT honoured
deterministically — the injector renames its output to
`{stem}-rppg - <metrics>{ext}`. Always resolve the real file via
`automation.rppg.resolve_produced_output()` — single source of truth used by
both the GUI queue and the test harness.

**Injector contract gotcha #2 (PR `fix/rppg-failure-visibility`):** the
iterative loop's "best so far" temp file (`temp_iteration_N.mp4`) can be
pruned mid-loop, causing the final `shutil.copy2` at
`rPPG/rppg_injector.py:4562` to crash with `FileNotFoundError`. The
defensive fix in that PR snapshots the current best to a stable name
(`best_iteration_snapshot.mp4` in the injector cwd) at every
`_is_new_best` transition and cleans it up at end-of-run. Don't remove
the snapshot logic — root cause of the mid-loop deletion is still
unknown.

**GPU on a box with a SYSTEM CUDA Toolkit (v2.23.3 → v2.23.4 cure):** rPPG uses
CuPy, which JIT-compiles its kernels. If an OLD system CUDA Toolkit is present
(e.g. CUDA v11.8), CuPy `_get_cuda_path()` resolves it via `CUDA_PATH` OR
`shutil.which('nvcc')` and compiles against its headers, breaking CuPy 13.6's
bundled CCCL 2.8.0 (`cuda/std/limits(633): constexpr function return is
non-constant`) → CPU fallback. THE FIX (`_force_cupy_bundled_cuda_headers()` in
`rPPG/rppg_injector.py`, mirrored in the `gpu_bootstrap` probe): before the first
compile, set `cupy._environment.get_cuda_path`/`get_nvcc_path` to return `None`
(and pop `CUDA_PATH`/`CUDA_HOME`) so CuPy uses its OWN bundled headers — immune to
both env vars and nvcc-on-PATH. **`RPPG_KEEP_CUDA_PATH=1`** opts out (rare box that
NEEDS the system toolkit). nvrtc/runtime/nvjitlink are pinned `>=13.3,<13.4` to
match the bundled CCCL. The injector ALSO clears the CuPy JIT cache + force-injects
the wheel include dir on a compile failure before retrying once, then degrades to
CPU. The bogus `--expt-relaxed-constexpr` "belt" was REMOVED — nvrtc rejects it.
End users with a contaminating system toolkit can run the safe one-click
`fix/Run CUDA Cleanup.bat` (ships in the release zip).

**Validation rig:** `oldcam-testing/rppg_harness.py` + `run_rppg_harness.bat`
run the real injector against a permanent gitignored fixture and emit an
anti-siren `REPORT.md`. Use it before pushing any rPPG/oldcam change. See
the wiring doc for the three invocation modes (direct / chain / --skip-run).

---

## Similarity Stack Wiring (NON-NEGOTIABLE — full surface coverage)

Full per-action tables (adding a dep, adding a GUI control, adding a config
key, adding a launcher, pre-flight checklist):
[`docs/similarity-wiring.md`](docs/similarity-wiring.md).

**The feature spans TEN distinct surfaces:** main GUI carousel, automation CLI
pipeline, standalone subproject (own GUI + own CLI), Windows + macOS launchers
(per surface), PyInstaller frozen build, dist release zip, and tests. Touching
it without updating ALL applicable surfaces ships a broken release. Use the
wiring doc's per-action checklist before commit.

**Engine layer (single source of truth — DO NOT duplicate):**

| Concern | File |
|---------|------|
| Engine class + scoring math | `similarity_engine.py` (root) |
| Standalone shim | `similarity/src/engine.py` re-exports `from similarity_engine import FaceEngine` |
| App-facing adapter (singleton + config overrides) | `face_similarity.py` (root) |
| Pipeline import | `from face_similarity import compute_face_similarity_details` in `automation/pipeline.py` |
| Main GUI import | `from face_similarity import compute_face_similarity_details` in `kling_gui/carousel_widget.py` |
| Standalone GUI/CLI import | `from src.engine import FaceEngine` in `similarity/src/{gui,cli}.py` |

**Default config keys (current):** `automation_similarity_threshold` (80),
`automation_similarity_use_ensemble` (true), `automation_similarity_secondary_model`
("Facenet512"), `automation_similarity_anti_spoofing` (true),
`automation_similarity_require_fas_pass` (false).

---

## macOS ↔ Windows bounce traps (NON-NEGOTIABLE pre-PR check matrix)

The user works on **both macOS (primary dev) and Windows (verification + use)**.
Bugs only one OS can trigger have caused multiple cross-OS bounces
(PR #49 → #50 → #51 → #55). A 7-trap pre-PR check matrix — dist build bloat
(gitignored ≠ excluded), launcher arg-forward/EOL/exec-bit asymmetry,
Windows file-handle contention, path-separator asserts, the `>nul`→`/dev/null`
linter substitution, OS-junk files (`.DS_Store`/`Thumbs.db`), and the cmd
nested-block parens crash — lives in:

> **➡️ [`docs/cross-os-bounce-traps.md`](docs/cross-os-bounce-traps.md) — RUN
> the applicable traps BEFORE opening any PR. Don't ship-and-bounce.**

If a check requires the other OS and you can't run it, say so explicitly in the
PR description. The agent-private version is the memory file
`feedback_macos_windows_bounce_traps.md`.

---

## SSD + Distributables Workflow

Full playbook: [`docs/ssd-and-distributables.md`](docs/ssd-and-distributables.md).

A portable copy of the project lives on an external SSD at
`/Volumes/st7Private/code/selfie-gen-ultimate/` for plug-and-play
launching on virgin Macs (one-click `START.command` bootstraps Python,
seeds Application Support, extracts a pre-built venv, then launches the
GUI). The SSD repo has `_user_state/` (gitignored locally + in
`.git/info/exclude` on the SSD repo) carrying `venv-macos.tar`, an
`app_support/` snapshot, and the install scripts.

**Before EVERY dist build (personal or release), bump `app_version.py`
first.** The version chip rendered in the GUI top-bar header (right of
the "Ultimate-Selfie-Gen" title) reads `app_version.RELEASE_VERSION`,
which is the same constant `distribution/release_prep.py` uses for zip
naming. A stale `app_version.py` ships a `v2.7` zip that the GUI still
labels `v2.6` — silent version drift that costs the user an "is this
the new one?" cycle on every cross-machine pull.

Concretely:

* Edit `app_version.py` → bump `RELEASE_VERSION` to the new version
  string.
* THEN run `distribution/build_release_personal.py` (or
  `build_release.py` for a sanitized public build).
* Verify the GUI chip shows the new version on first launch of the
  new zip — `python -c "from app_version import RELEASE_VERSION; print(RELEASE_VERSION)"`
  is the quickest sanity check.

User mandate 2026-05-27 during the v2.7 build.

**On every merge to main, if the SSD is mounted, also refresh it.**

1. `python distribution/build_release.py` → `dist/SelfieGenUltimate-{vX.Y}.zip` + alias.
2. `cd /Volumes/st7Private/code/selfie-gen-ultimate && git pull origin main`.
3. `rsync` the live `~/Library/Application Support/selfie-gen-ultimate/` →
   `_user_state/app_support/` (keeps API keys + prompts + model cache in sync).
4. `cp dist/SelfieGenUltimate-*.zip` to SSD root.
5. Rebuild `_user_state/venv-macos.tar` ONLY if `requirements.txt` or
   `requirements-hashed.txt` changed in the merged PR. Otherwise leave it.

Check `ls /Volumes/st7Private 2>/dev/null && echo MOUNTED` first. If
unmounted, tell the user "I'd refresh the SSD but it's not mounted, your
copy is now N commits behind" — don't silently skip.

Detection of bundle-bloat regressions: a healthy bundle is ~10 MB. If
`build_release.py` produces hundreds of MB, a venv or build artifact has
escaped `EXCLUDED_DIRS` in `distribution/release_prep.py` — fix that
list before shipping.

---

## numpy<2 / constraints.txt invariant (NON-NEGOTIABLE — v2.11)

A fresh v2.10 Windows install broke Face Crop with `ImportError:
numpy.core.umath failed to import` — numpy 2.x reached the venv and broke
TensorFlow 2.16.2 (ml-dtypes~=0.3.1 needs numpy 1.26.x). The numpy<2 /
opencv<4.12 caps lived ONLY in `requirements.txt`, so they governed one
`pip install -r` call but NOT the bootstrap, the `--no-deps` mediapipe step,
or the repair's `--force-reinstall`. `deepface==0.0.92` declares only
`numpy>=1.14.0` (open upper bound) and numpy 2.x ships wheels, so any
unconstrained resolve was free to upgrade.

**The fix — and the rules to keep it fixed:**

- **`constraints.txt` at repo root is the single source of truth** for the
  numpy<2 + opencv<4.12 + TF caps. It is passed via `-c constraints.txt` to
  **every** `pip install` the project issues. When you add/bump a pinned dep
  whose transitive deps could pull numpy, update `constraints.txt` to match
  `requirements.txt` — keep them in lockstep.
- **Every launcher's project-dep `pip install` MUST carry `-c constraints.txt`**
  (Windows `.bat` + macOS `.command`/`.sh`). This includes the oldcam v7–v24
  launchers, the similarity standalone launchers, rPPG self-heal,
  `setup_macos.sh`, `dependency_checker.install_pip_package`, and
  `dependency_health_check.run_repair`. The guard tests live in
  `tests/test_launcher_health_check_loop.py` (parametrized over both Windows
  launchers) + `tests/test_fresh_install_numpy_pin.py`.
- **Sub-project `requirements.txt` files must self-cap** numpy<2 + opencv<4.12
  too (defense-in-depth that travels with the file): `oldcam-v*/requirements.txt`,
  `similarity/requirements.txt`. The `-c` in launchers is the belt; these caps
  are the suspenders.
- **The launcher "healthy" stamp is gated on the health probe passing.** Never
  write `deps_*.ok` (Windows) / `.health.sha256` (macOS) unconditionally — a
  venv that failed the probe must NOT be cached as healthy.
- **Runtime safety net:** if the face stack import fails in the GUI, it
  auto-repairs in-process via `kling_gui/dependency_repair_dialog.py` and
  retries — NO terminal command shown to the user (they're non-technical).
  `dependency_health_check.assert_numpy_pinned()` makes the probe fail on
  numpy ≥ 2 directly.
- **Pre-ship gate:** dry-run + import-probe is INSUFFICIENT for the dep stack.
  Before shipping any zip, run the real fresh-venv test
  (`RUN_FRESH_INSTALL_TEST=1 pytest tests/test_fresh_install_numpy_pin.py` on
  Windows) OR extract the zip to a clean dir and launch end-to-end clicking
  Face Crop. Record which verification you ran in the PR description.

When adding any NEW launcher or NEW `pip install` site, thread `-c
constraints.txt` through it and add it to the launcher guard test, or the
numpy-2 hole reopens.

## uv dependency management (v2.20 — primary path, pip fallback)

Dependency management migrated from pip + requirements.txt + constraints.txt
to **uv with a committed `uv.lock`** (the `cpu`/`cu121`/`cu128` torch extras
carry the GPU split). Full design + file map + cross-OS wheel-gap notes:

> **➡️ [`docs/uv-migration.md`](docs/uv-migration.md) — READ BEFORE editing
> `pyproject.toml`, `uv.lock`, or any `scripts/uv_*` / `*_uv_*` launcher**

Load-bearing rules (the ones that bite):

- **The pip path is KEPT as an automatic fallback.** Every launcher tries
  `scripts/uv_sync_deps.py` first; on any uv problem it returns exit **3** and
  the launcher falls through to the legacy pip install. `KLING_USE_PIP=1`
  forces pip. Do NOT delete `requirements.txt` / `constraints.txt` until uv is
  proven on both OSes in production.
- **The same numpy<2 / opencv<4.12 / TF 2.16.2 / tf-keras / absl / scipy /
  mediapipe invariants apply.** On the **uv path** they are enforced by
  `uv.lock`. On the **pip fallback path** (still in-tree) they remain enforced
  by `-c constraints.txt` at EVERY pip-install site — do NOT drop that. When
  you bump a pin in `pyproject.toml`, re-run `uv lock`
  and verify the always-on real-import probes (`pytest
  tests/test_uv_lock_imports.py`) still pass (numpy stays <2, deep mediapipe
  Tasks-API still imports).
- **The uv bootstrap chain (`ensure_uv` -> `uv_torch_select` -> `uv_sync_deps`
  -> `gpu_bootstrap`) MUST stay stdlib-only** — it runs with the SYSTEM Python
  BEFORE `uv sync` materializes the env. A third-party import there breaks
  fresh-install provisioning.
- **`.bat` helpers (`win_uv_sync.bat`, the preflight uv block) follow ALL the
  Windows launcher rules** — CRLF via byte-level write (the generators
  `scripts/_gen_*` / `_patch_*` are committed for reproducibility), no
  `/dev/null`, and FLAT `if`/`goto` (never nested-`if` + paren block — the
  Windows 11 25H2 `. was unexpected at this time` crash, bounce-traps Trap 7).
- **`required-environments` in `pyproject.toml`** forces the lock to resolve
  for win-AMD64 + darwin-arm64 + linux-x64, catching cross-OS wheel gaps at
  lock time. Intel macOS is intentionally omitted (mediapipe has no
  darwin-x86_64 wheel). Don't add it back.

---
## CLI UX architecture (v2.31, PR #96) — binding invariants

The CLI (`kling_automation_ui.py`) was fully overhauled in PR #96. The rules
that bite, learned across 7 review rounds:

- **Rich markup-escape EVERY user string** rendered through `_RICH_CONSOLE`
  (Panel bodies, Table cells, `Text.from_markup`, dashboard lines): prompts,
  model display names, endpoints, folder/file names, paths, error text.
  `[bracketed]` text silently vanishes and a literal `[/x]` raises
  `MarkupError` — one unescaped sink crashed every screen repaint. Use
  `_rich_markup_escape` (imported at top); literal bracket glyphs in OUR
  markup use the `\[` escape. questionary/prompt_toolkit labels are plain
  text and safe.
- **Per-surface config keys**: the CLI pipeline reads `cli_video_model` /
  `cli_video_model_display_name` / `cli_kling_prompt_slot` /
  `cli_video_duration` via `automation/config.py::resolve_cli_*` (fallback to
  the GUI's `current_model` / `model_display_name` / `current_prompt_slot` /
  `video_duration` for pre-split configs). Prompt slot TEXT
  (`saved_prompts`/`negative_prompts`) stays SHARED GUI⇄CLI by design.
  **Never `automation_`-prefix these keys**: `automation/manifest.py`
  fingerprints every `automation_*` key and the model is deliberately
  non-fingerprinted (resume-with-different-model + `--model` contract).
- **Manifest fingerprint**: run-scope/discovery/metadata keys are excluded
  (`_FINGERPRINT_EXCLUDED_KEYS`). The fingerprint answers exactly one
  question — "would re-running produce different OUTPUTS?". The lost
  front-discovery protection is replaced by the per-case
  `AutoPipelineRunner._reset_case_if_front_changed` guard. Manifest reads
  from any thread hold `manifest.lock` (both sides of every read/write pair).
- **Menus**: choice lists are single-source `(label, value)` pair methods
  (`_main_menu_choice_pairs`, `_quick_edit_choice_pairs`) + group specs
  (`_MAIN_MENU_GROUPS`, `_QUICK_EDIT_GROUPS`) — tests assert pairs⇄groups
  consistency AND dispatch wiring (`tests/test_cli_gui_settings_split.py`).
  Every interactive screen repaints via `display_header()` (clears + banner);
  **legacy/non-TTY output must stay byte-identical** — gate all restyles on
  `not self._use_legacy_prompt_ui()`. Tables come from `_styled_table()`
  only. No `questionary.confirm` y/n — `_qs_bool` is an ON/off select.
- **`RUN_SUITE.bat` / `RUN_SUITE.command`** (root): the unified front door.
  They DELEGATE to `launchers/windows|macos/*` and must never install
  anything themselves. The `.bat` is ASCII-only with runtime ANSI (forfiles
  0x1B trick) gated on VT-capable consoles; cmd traps that bit here: `%CD%`
  etc. are dynamic variables (never use as var names), `for /f` running a
  command with `=`/`,` flags needs a `cmd /c "..."` wrapper.
- **Release zips**: `distribution/build_release.py` CLOBBERS `dist/` — run
  `build_release_personal.py` LAST. Verify every zip: no `.scratch*`, keys
  blanked, `RUN_SUITE.*` present, import probe from the extracted tree.
- **Dev box**: `KLING_UV_DEV=1` (set user-wide) makes the launcher `uv sync`
  include the `dev` dependency group so pytest survives app launches;
  without it every launch uninstalls pytest.

## Supply Chain Audit

Two project-level scanners run on every commit that touches a dep manifest,
workflow, `.pth`, or `.claude/*` file. Both must exit clean. They cover
different gaps; don't pick one.

| Scanner | Strengths | File |
|---|---|---|
| Project scanner | GitHub exfil-repo discovery (`--github`), C2 domain check, Dune-name repo patterns, project-specific IoC tables | `scripts/detect_compromise.py` |
| Hulud-kit scanner (v1.1) | **`.claude/` persistence detection** (TeamPCP attack vector against Claude Code), spoofed git author check, campaign string markers, SARIF 2.1.0 output | `scripts/hulud_kit_scan.py` |

Both wired into `scripts/git-hooks/pre-commit` and run automatically when a
matched file is staged. Bypass only with `git commit --no-verify` after a
manual review.

### Manual invocations

```bash
# Project scanner (GitHub exfil + project IoCs):
python scripts/detect_compromise.py --repo-root .
python scripts/detect_compromise.py --all          # adds --venv + --github

# Hulud-kit scanner (.claude/ persistence + 9 checks):
python scripts/hulud_kit_scan.py --root .
python scripts/hulud_kit_scan.py --sarif results.sarif   # for GitHub Security tab

# Machine-wide audit (~30s, queries OSV.dev live):
/hulud-kit quick                                   # via Claude Code slash command
~/.shai-hulud/shai-hulud-audit.sh --mode quick     # direct invocation
```

### When adding a dependency

Audit the full `requirements-hashed.txt` (falling back to `requirements.txt`)
in a disposable venv before merging any dep change. The script takes no
positional args — it always re-installs the project's pinned set into a
fresh venv and runs `pip-audit`:

```bash
./scripts/sandbox_install.sh         # macOS / Linux
.\scripts\sandbox_install.bat        # Windows
```

After any dep change, regenerate the hash-pinned lockfile. Run this on
**Windows**, not macOS — the current `requirements-hashed.txt` is pinned
to Intel/Windows TensorFlow wheels and macOS pip-compile output overwrites
the Windows wheel set, breaking the bundled release:

```bash
pip install pip-tools
pip-compile --generate-hashes --output-file=requirements-hashed.txt requirements.txt
```

### When an audit fails

Read findings carefully — `.claude/execution.js` or `.claude/setup.mjs` alerts
from the kit scanner are TeamPCP persistence payloads, treat as confirmed
compromise. Then follow `docs/security/IOC_DETECTION_CHECKLIST.md` for the
incident-response runbook (credential rotation order, what to check next).

### Updating the kit scanner

The kit scanner is sourced from <https://github.com/aaronvstory/shai-hulud-kit>.
To pull a newer version: `cp /path/to/shai-hulud-kit/scripts/detect_compromise.py
scripts/hulud_kit_scan.py`, then re-add `"hulud_kit_scan.py"` to the
`SCANNER_FILENAMES` set in `check_campaign_markers()` so the scanner doesn't
flag itself.
