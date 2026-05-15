# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

Kling UI is an AI media generation toolkit using fal.ai and BFL APIs. It provides a 5-tab Tkinter GUI for face cropping, portrait analysis, selfie generation, image outpainting, and batch video generation, plus a CLI mode via Rich.

## macOS Portability — MANDATORY (Windows agents read this first)

This repo runs on **both Windows and macOS**. Most contributors edit on Windows, and CI is Windows-leaning. Several macOS-runtime issues recur — agents working on this codebase MUST guard against them on every change that touches shell scripts, launchers, file dialogs, or path handling.

### 1. Line endings — `.sh` and `.command` must be LF

`.gitattributes` pins `*.sh` and `*.command` to `eol=lf`. **Windows editors still write CRLF, and the index can drift out of sync with the attribute.** A CRLF shebang resolves to `#!/usr/bin/env bash\r`, which on macOS makes `env` fail with `env: bash\r: No such file or directory` and exit 127.

When you create or edit any `.sh` / `.command` file:

```bash
# Verify EOL in working tree + index
git ls-files --eol <file>          # both columns must show "lf"
file <file>                        # must NOT mention "CRLF line terminators"

# If wrong:
tr -d '\r' < <file> > <file>.tmp && mv <file>.tmp <file>
git add --renormalize <file>
```

### 2. Executable bit — `.command` and `.sh` must be `100755` in git

`.command` files cannot be double-clicked from Finder unless they have the exec bit. Git stores mode independently of the working-tree perm; a file can be `chmod +x` locally but still committed as `100644`. Both must be `100755`.

```bash
# Verify
git ls-files --stage <file>        # leading number must be 100755

# Fix both working tree and index:
chmod +x <file>
git update-index --chmod=+x <file>
```

### 3. File dialogs — never use raw `tkinter.filedialog`

The macOS Tk root has a fragile lifecycle. The repo wraps every dialog in `tk_dialogs.py` (`select_directory`, `select_open_file`, `select_open_files`, `select_save_file`, `select_directory_cli_safe`). These handle ephemeral root creation, withdrawal, and destruction across Win/macOS/Linux.

```python
# WRONG — raw filedialog can hang the dialog and leak Tk roots on macOS
from tkinter import filedialog
path = filedialog.askopenfilename(title="Pick")

# RIGHT — pass parent= when a live Tk window exists, omit for CLI flows
from tk_dialogs import select_open_file
path = select_open_file(parent=self.root, title="Pick")          # GUI
path = select_open_file(title="Pick")                            # CLI (uses ephemeral root + osascript on darwin)
```

When a GUI has a live secondary window (drop-zone, modal, etc.), prefer that over the main root — see `_best_picker_parent()` in `kling_gui/main_window.py`. macOS pickers stall when their parent is withdrawn mid-dialog.

### 4. Path-separator assertions are platform-bound

`os.path.join("F:\\foo", "bar.bat")` returns `"F:\\foo/bar.bat"` on POSIX (forward slash) but `"F:\\foo\\bar.bat"` on Windows. Tests that assert on the result of any path-join with backslash inputs are intrinsically Windows-only:

```python
@pytest.mark.skipif(os.name != "nt", reason="asserts win32 backslash joins")
def test_windows_launcher_uses_comspec_then_fallback(): ...
```

### 5. Test module-mock gotchas (sys.modules caching)

When a test stubs `sys.modules` to inject fakes for `tkinter`, `deepface`, `cv2`, `mediapipe`, etc., it MUST also evict any submodules the production code re-imports later. `patch.dict(sys.modules, {"mediapipe": fake})` only intercepts `import mediapipe`; it does NOT intercept `from mediapipe.tasks.python import vision` because that goes through `__import__("mediapipe.tasks.python", ...)`.

```python
for cached in ("mediapipe", "mediapipe.tasks", "mediapipe.tasks.python", "mediapipe.tasks.python.vision"):
    monkeypatch.delitem(sys.modules, cached, raising=False)

def fake_import(name, *a, **k):
    if name == "mediapipe":
        return fake_mp
    if name.startswith("mediapipe."):
        raise ImportError(f"mocked: {name} unavailable")
    return real_import(name, *a, **k)
```

Same trap for `similarity/src/engine.py`, which is a shim that does `from similarity_engine import FaceEngine`. Tests reloading `src.engine` MUST pop **both** `src.engine` AND `similarity_engine` from `sys.modules`, otherwise the previously-bound (real) `DeepFace` stays in scope.

### 6. macOS Python — use `python3.11`, not `python3.12`+

Homebrew's `python3.12` and `python3.13` ship without `_tkinter`. Tests that import `tkinter` (transitively, anything touching the GUI or `tk_dialogs`) will fail to collect on those interpreters. Use `python3.11`:

```bash
python3.11 -m venv .venv311
.venv311/bin/python -m pytest tests/ similarity/tests/ -q   # use python -m pytest, not pytest directly,
                                                            # so the project root is on sys.path
```

### 7. The macOS launcher chain (don't break links silently)

```
run_gui.command (root)
  → launchers/run_gui.command         (compatibility wrapper)
    → launchers/macos/run_gui.command (logs + dep-chmod + invokes run_gui.sh)
      → run_gui.sh                    (calls setup_macos.sh, then runs gui_launcher.py)
        → setup_macos.sh              (creates .venv-macos and installs requirements.txt)
```

If you touch any link in that chain: chain-test it via `bash run_gui.command` once before pushing. Same chain exists for `run_cli.command` and the eight `run_oldcam_v*.command` variants.

### 8. Pre-push macOS portability check

Run before pushing any change that touches `*.sh`, `*.command`, `tk_dialogs.py`, or anything under `launchers/`, `similarity/src/`, or `kling_gui/main_window.py` picker code:

```bash
bash scripts/check_macos_portability.sh
```

Exits non-zero on CRLF in shell scripts, or `.command`/`.sh` files committed without the exec bit. Source: `scripts/check_macos_portability.sh`.

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

`front_expand -> extract_portrait -> selfie_generate -> similarity_gate -> selfie_expand -> video_generate -> oldcam`

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

## PR Bot Triage (Latest Commit Range)

When processing PR review bots:

1. Trigger fresh runs on current head (`@codoki review`, `@coderabbitai review`, `@codex review`).
2. Collect fresh actionable findings tied to the latest commit range.
3. Ignore stale historical findings already superseded by newer commits.
4. Run targeted pytest suites before pushing fix commits.

---

## Hard Rules — Windows Launchers (NON-NEGOTIABLE)

These rules exist because violating them has caused repeated launch breakage. Do not skip them.

### 1. CRLF line endings for all .bat/.cmd files

The `Write` and `Edit` tools produce LF-only files. On Windows, LF-only batch files garble every command — errors like `'"tokens=1" is not recognized'`. **Always write `.bat`/`.cmd` files using PowerShell:**

```powershell
$content = @'
@echo off
...batch content here, use `r`n manually...
'@
$crlf = $content -replace "`r`n","`n" -replace "`n","`r`n"
[System.IO.File]::WriteAllText("path\file.bat", $crlf, [System.Text.Encoding]::ASCII)
```

Verify after writing: `CRLF=True` and `LFonly=False`. Never use `Write` or `Edit` for bat files.

### 2. Use `echo(` not `echo.` for blank lines

`echo.` (dot, no space) causes `'. was unexpected at this time'` in some cmd environments, including when `enabledelayedexpansion` is active. **Always use `echo(` for blank lines.** The `echo(` form is unconditionally safe.

### 3. Log-file append: redirect operator goes before echo

```bat
rem WRONG — [ ] in unquoted echo can be misinterpreted
echo [%LAUNCH_TS%] started >> "%LOG_FILE%"

rem CORRECT — >> before echo, no ambiguity
>>"%LOG_FILE%" echo [%LAUNCH_TS%] started
```

### 4. Launcher chain

```
root\run_gui.bat  →  launchers\run_gui.bat  →  launchers\windows\run_gui.bat
```
The root files are compat wrappers only. All real logic lives in `launchers/windows/`. Do not duplicate logic in the root wrappers.

### 5. Dep-skip stamp (no subprocess for hashing)

Stamp key is built from req file dates+sizes — no `certutil` subprocess needed:

```bat
for %%F in ("%REQUIREMENTS%" ...) do (
    if exist "%%~F" set "STAMP_KEY=!STAMP_KEY!%%~tF%%~zF"
)
set "STAMP=%STATE_DIR%\deps_%STAMP_KEY:~0,60%.ok"
```

If stamp exists → `goto :launch` (skip all pip/dep work). Only run dep sync when stamp is missing or stale.

### 6. MediaPipe must be installed with `--no-deps`

```bat
"%VENV_PYTHON%" -m pip install --no-deps "mediapipe==0.10.35"
```

Always filter mediapipe out of requirements files before the main pip install, then install it separately. Letting pip resolve mediapipe deps causes conflicts.

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
| `sash_prompt_split` (left tabs width) | 60% of window | 54–64% |
| `sash_queue` (carousel width) | 25% of window | 22–32% |
| `sash_log_drop_split` (log width within right section) | 71% of right section | 55–82% |

---

## Oldcam Version Wiring

> Full checklist: [`docs/oldcam-wiring.md`](docs/oldcam-wiring.md)

When adding a new Oldcam version (e.g., v12), these are the required touch-points:

| Layer | Where | What to do |
|-------|-------|-----------|
| Algorithm | `oldcam-vN/` + `oldcam-vN/macOS/` | Create folder with `oldcam.py`, `launcher.py`, `requirements.txt`, `oldcam_launcher.bat` (CRLF) |
| Launchers (3 levels) | `launchers/windows/`, `launchers/macos/`, `launchers/` | Add 4 new launcher files (2 `.bat` CRLF, 2 `.command` LF) |
| GUI checkbox | `kling_gui/config_panel.py` | Add to `oldcam_version_vars` dict (~line 514) and loop tuple (~line 522) |
| MediaPipe flag | `kling_gui/queue_manager.py` | Add to `requires_mediapipe` set if vN uses face landmarks |
| Tests | `tests/test_oldcam_versions.py`, `tests/test_launcher_hub_wrappers.py` | Version tuple + output path + mediapipe tests + launcher assertions |
| Dist | `build_release_zip.py` | Add new launcher file paths explicitly (algorithm folder auto-included) |
| If new default | `automation/config.py`, root + hub launchers | Set `automation_oldcam_version`, update all 5 `run_oldcam` launchers |

**Auto-discovered (no changes needed):** `_discover_oldcam_versions()` in `queue_manager.py` scans `oldcam-v*` dirs; output filename suffix is generic; face landmarker task searched generically; `automation/pipeline.py` is fully version-agnostic.

**Current default version:** v13. Mediapipe versions: v9, v10, v11.

---

## Similarity Stack Wiring (NON-NEGOTIABLE — full surface coverage)

The face-similarity feature spans **TEN distinct surfaces**: main GUI carousel, automation CLI pipeline, standalone subproject (own GUI + own CLI), Windows + macOS launchers (per surface), PyInstaller frozen build, dist release zip, and tests. Touching it without updating ALL applicable surfaces ships a broken release.

**Engine layer (single source of truth — DO NOT duplicate):**

| Concern | File |
|---------|------|
| Engine class + scoring math | `similarity_engine.py` (root) |
| Standalone shim | `similarity/src/engine.py` re-exports `from similarity_engine import FaceEngine` |
| App-facing adapter (singleton + config overrides) | `face_similarity.py` (root) |
| Pipeline import | `from face_similarity import compute_face_similarity_details` in `automation/pipeline.py` |
| Main GUI import | `from face_similarity import compute_face_similarity_details` in `kling_gui/carousel_widget.py` |
| Standalone GUI/CLI import | `from src.engine import FaceEngine` in `similarity/src/{gui,cli}.py` |

### A. Adding a new ML dependency (e.g., torch, onnxruntime)

| Layer | File | Action |
|-------|------|--------|
| Main requirements | `requirements.txt` | `+ pkg>=X,<Y` |
| Standalone subproject requirements | `similarity/requirements.txt` | `+ pkg>=X,<Y` |
| Dep-checker registry | `dependency_checker.py:DEPENDENCIES` | Add `Dependency(name=…, import_name=…, pip_name=…, required=False, description=…)` |
| Auto-repair set | `dependency_checker.py:REPAIRABLE_RUNTIME_IMPORTS` | `+ "import_name"` |
| Frozen build hidden imports | `kling_gui_direct.spec:hiddenimports` | `+ 'pkg'` and optionally `collect_submodules('pkg')` |
| Dep stamps (auto-busted) | `.launcher_state/deps_*.ok` and `similarity/.launcher_state/similarity_*.ok` | Auto-busted on `requirements.txt` mtime/size change; manual `rm` if needed |

### B. Adding a similarity GUI control (checkbox/button/etc.)

| Layer | File | Action |
|-------|------|--------|
| Main carousel widget | `kling_gui/carousel_widget.py::_build_panel` | Add widget in `sim_row` (controls) or `meta_frame` (status chips) |
| Bind to engine | `_on_<control>_toggle` method on `ImageCarousel` | Apply to `_get_engine().<attr>` then call `recalc_all_similarity_now(reason=...)` |
| Standalone GUI mirror | `similarity/src/gui.py` | Add `ctk.CTkCheckBox` / `ctk.CTkSwitch` with the same name |
| Standalone CLI mirror | `similarity/src/cli.py::apply_runtime_config` + `similarity/main.py` argparse | Add `--<flag>` with `argparse.BooleanOptionalAction` |
| Config persistence | `kling_config.json` defaults + `face_similarity._apply_config_overrides` | New `automation_similarity_<name>` key |
| Test stubs (main carousel) | `tests/test_carousel_ref_controls.py` `_FakeButton()` block | Add new attribute on the `tab` instance if `_update_panel` reads it |
| Test stubs (standalone GUI) | `similarity/tests/test_gui.py::_CTkModuleStub` | Add new widget class to the stub registry |

### C. Adding a new `automation_similarity_*` config key

| Layer | File | Action |
|-------|------|--------|
| Default value | `kling_config.json` | Add key with sensible default |
| Loader | `face_similarity._apply_config_overrides` | Read with `_parse_bool(...)` for booleans (handles `"true"`/`"false"` strings), `str(...).strip()` for strings |
| Pipeline gate | `automation/pipeline.py` | Read via `self.automation.get("automation_similarity_<key>", default)` |
| Standalone CLI flag | `similarity/main.py` argparse + `similarity/src/cli.py::apply_runtime_config` | Mirror as a CLI flag |
| Tests | `tests/test_automation_pipeline.py`, `tests/test_similarity_canonical_path.py` | New gating + adapter tests |

### D. Adding a new launcher (Windows + macOS, GUI + CLI)

| Layer | Windows | macOS | Notes |
|-------|---------|-------|-------|
| Root wrapper | `run_<name>.bat` | `run_<name>.command` | Two-line passthrough |
| Hub wrapper | `launchers/run_<name>.bat` | `launchers/run_<name>.command` | Hop to platform layer |
| Platform impl | `launchers/windows/run_<name>.bat` (CRLF, `echo(` for blanks) | `launchers/macos/run_<name>.command` (LF — Apple writes the OS as "macOS") | Real venv/dep/exec logic |
| Standalone subproject | `similarity/run_<name>.{bat,command}` | same | Used by hub wrappers `launchers/{windows,macos}/run_similarity_*` (path stays lowercase, OS name in prose stays "macOS") |
| Build pipeline | `distribution/release_prep.py:copy_sanitized_tree` | same | Walks tree → auto-included unless excluded |

### E. Pre-flight checklist (run BEFORE every similarity-stack commit)

- [ ] `requirements.txt` updated if new pip dep
- [ ] `similarity/requirements.txt` updated if new pip dep
- [ ] `dependency_checker.py` (DEPENDENCIES + REPAIRABLE_RUNTIME_IMPORTS) updated
- [ ] `kling_gui_direct.spec` hiddenimports updated if new module imported lazily
- [ ] CLI flag in `similarity/main.py` argparse if user-controllable
- [ ] CTk stub in `similarity/tests/test_gui.py:_CTkModuleStub` if new widget class used
- [ ] `_FakeButton` stubs in `tests/test_carousel_ref_controls.py` if `_update_panel` reads new widget
- [ ] `python -m pytest tests/ similarity/tests/test_cli.py similarity/tests/test_gui.py -q` (all green)
- [ ] Line endings match per-file convention (`requirements.txt` LF, `kling_gui/main_window.py` CRLF — check with `python -c "..."` snippet from prior commits)
- [ ] Smoke-tested both real GUI (`launchers/windows/run_gui.bat`) AND standalone GUI (`launchers/windows/run_similarity_gui.bat`)

**Default config keys (current):** `automation_similarity_threshold` (80), `automation_similarity_use_ensemble` (true), `automation_similarity_secondary_model` ("Facenet512"), `automation_similarity_anti_spoofing` (true), `automation_similarity_require_fas_pass` (false).
