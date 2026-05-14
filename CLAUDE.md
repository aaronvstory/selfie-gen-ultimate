# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

Kling UI is an AI media generation toolkit using fal.ai and BFL APIs. It provides a 5-tab Tkinter GUI for face cropping, portrait analysis, selfie generation, image outpainting, and batch video generation, plus a CLI mode via Rich.

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
clamped_queue = max(queue_min, min(int(sash_queue or queue_default), queue_max))
right_section_w = max(400, safe_w - clamped_queue)
log_drop_min = int(right_section_w * 0.42)
log_drop_max = int(right_section_w * 0.62)
```

### Current target proportions (1600px-wide window)

| Sash | Target | Range |
|------|--------|-------|
| `sash_prompt_split` (left tabs width) | 60% of window | 54–64% |
| `sash_queue` (carousel width) | 24% of window | 20–30% |
| `sash_log_drop_split` (log width within right section) | 52% of right section | 42–62% |

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

**Current default version:** v12. Mediapipe versions: v9, v10, v11, v12.
