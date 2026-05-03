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
- `run_gui.bat` — Windows launcher with auto-venv.
- `run_cli.sh` / `run_cli.command` — macOS CLI launchers.
- `run_gui.sh` / `run_gui.command` — macOS GUI launchers.
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
