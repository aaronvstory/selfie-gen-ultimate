## Runtime Priority (Codex App)

- Follow global Codex hooks policy first.
- Serena-first workflow is REQUIRED each coding task (fallback only when unavailable/inapplicable).
- Serena active means symbol tools are available (`find_symbol`, `get_symbols_overview`), not just activation.
- If Serena symbol tools are missing, explicitly report degraded mode before fallback.
- Caveman concise communication style is required.
- Do not paste raw Serena manual/tool dumps in user-facing output unless explicitly requested.
- Prefer canonical repo path handling (`F:\claude\...` as source of truth).
# AGENTS.md - Kling UI Codebase Guide

> For AI coding agents working in this repository. Last updated: 2026-05-04

## GitHub Workflow Requirement

- For all GitHub operations (PR creation, PR updates, issue triage, review handling, CI investigation), use the GitHub app/plugin workflow first.
- Treat end-to-end GitHub handling as expected default behavior for agents in this repo.
- Use `gh` CLI only as a fallback when app coverage is unavailable for a specific action.

## macOS Portability — MANDATORY (Windows agents read this first)

This repo runs on **both Windows and macOS**. Most editing happens on Windows; macOS-runtime issues recur and the user has paid for them in lost time. Every change touching shell scripts, launchers, file dialogs, path handling, or test mocks MUST be checked against the rules below.

| # | Rule | Why | How to verify / fix |
|---|---|---|---|
| 1 | `.sh` and `.command` files MUST be LF-terminated | A CRLF shebang resolves to `#!/usr/bin/env bash\r` → `env: bash\r: No such file or directory` (exit 127) on macOS | `file <f>` shows no "CRLF"; `git ls-files --eol <f>` shows `i/lf w/lf`. Fix: `tr -d '\r' < f > f.tmp && mv f.tmp f && git add --renormalize f` |
| 2 | `.command` and `.sh` files MUST be `100755` in git | `100644` files cannot be Finder-double-clicked on macOS | `git ls-files --stage <f>` starts with `100755`. Fix: `chmod +x f && git update-index --chmod=+x f` |
| 3 | Use `tk_dialogs` wrappers, never raw `tkinter.filedialog` | macOS Tk root has a fragile lifecycle; raw `filedialog` calls hang or leak Tk roots | Import `select_directory` / `select_open_file` / `select_open_files` / `select_save_file` from `tk_dialogs`. Pass `parent=` when a live window exists |
| 4 | Don't assert on `os.path.join` output cross-platform | `os.path.join("F:\\foo", "bar")` is `"F:\\foo/bar"` on POSIX, `"F:\\foo\\bar"` on Windows | Mark Windows-only tests `@pytest.mark.skipif(os.name != "nt", reason="...")` |
| 5 | Stub ALL submodules when mocking `sys.modules` | `patch.dict(sys.modules, {"x": fake})` does NOT intercept `from x.y import z`; that goes through `__import__("x.y", ...)` separately | Pop cached submodules with `monkeypatch.delitem(sys.modules, "x.y", raising=False)`; broaden the importer mock to match `x.*` |
| 6 | When reloading `src.engine`, also pop `similarity_engine` | `src/engine.py` is a thin shim that does `from similarity_engine import FaceEngine`; a prior test leaves real `DeepFace` bound | `sys.modules.pop("src.engine", None); sys.modules.pop("similarity_engine", None)` in `setUp` |
| 7 | macOS Python = `python3.11` (Tk-capable) | Homebrew `python3.12+` ships without `_tkinter`; everything that imports `tkinter` fails to collect | Use `python3.11 -m venv .venv311`. Run pytest as `python -m pytest ...` so cwd is on `sys.path` (no root `conftest.py`) |
| 8 | Don't break the macOS launcher chain | `run_gui.command → launchers/run_gui.command → launchers/macos/run_gui.command → run_gui.sh → setup_macos.sh → gui_launcher.py`. Same shape for `run_cli` and `run_oldcam_v*` | After any link change: `bash run_gui.command` (or `run_cli.command`) once before pushing |
| 9 | Python resolvers in `.command`/`.bat` MUST version-validate **every** venv candidate (not just `[ -x ]`). `.venv311/` is the canonical macOS venv name and MUST be a tried candidate. | A stale `$REPO_ROOT/.venv` symlinking to python3.14 silently gets returned, then the post-resolve gate aborts with a misleading "Unsupported Python version" error. Fixed in commit `afe0540b`. | Reuse the `_python_supported()` Bash helper at `similarity/run_gui.command:38-40` (or `:check_py` cmd subroutine at `similarity/run_gui.bat:140-151`) and gate every candidate. Add `.venv311/bin/python` (macOS) / `.venv311\Scripts\python.exe` (Windows) explicitly. Static-text test pattern in `tests/test_similarity_launcher_resolver.py`. |
| 10 | `.command` and `.sh` siblings MUST use **identical** `set` flags | Mismatches like `.command` with `set -uo pipefail` vs `.sh` with `set -euo pipefail` silently change error-handling behavior. CodeRabbit caught this on `launchers/macos/run_gui.command` in PR #21 (fixed in commit `300c88f0`). | Both files use `set -euo pipefail`. The explicit `set +e / set -e` toggle around sub-script invocations is fine — it still scopes errexit OFF for that one call. |

### Pre-push verification

Run before any push that touches `*.sh`, `*.command`, `tk_dialogs.py`, `launchers/`, `similarity/src/`, or `kling_gui/main_window.py` picker code:

```bash
bash scripts/check_macos_portability.sh
```

Exits non-zero on CRLF in shell scripts or `.command`/`.sh` files committed without the exec bit.

When the user says **"ensure this works on macOS"** while you are editing on Windows, your minimum bar is rules 1, 2, 3, and 8 — re-read them, then run `scripts/check_macos_portability.sh`.

## Quick Reference

### Build/Run Commands

```bash
# Run CLI application (main entry point)
python kling_automation_ui.py

# Launch GUI directly
python -c "from kling_gui import KlingGUIWindow; KlingGUIWindow().run()"

# Check dependencies
python dependency_checker.py

# Test balance checker (opens Chrome for login)
python selenium_balance_checker.py
```

### Install Dependencies

```bash
pip install requests pillow rich selenium webdriver-manager tkinterdnd2
```

### Testing

Pytest suites are present and should be used for regression checks:

```bash
pytest tests/
pytest tests/test_automation_pipeline.py -q
pytest tests/test_automation_manifest.py -q
pytest tests/test_automation_cli_smoke.py -q
```

Manual checks still matter for end-to-end provider behavior:

```bash
# Manual CLI testing
python kling_automation_ui.py

# Manual GUI testing
python -c "from kling_gui import KlingGUIWindow; KlingGUIWindow().run()"
```

---

## Code Style Guidelines

### Naming Conventions

| Element | Convention | Example |
|---------|------------|---------|
| Files | snake_case | `kling_generator_falai.py` |
| Functions | snake_case | `get_model_short_name()` |
| Private methods | underscore prefix | `_process_queue()` |
| Classes | PascalCase | `FalAIKlingGenerator` |
| Dataclasses | PascalCase | `QueueItem` |
| Constants | UPPER_SNAKE_CASE | `VALID_EXTENSIONS` |
| Variables | snake_case | `image_path`, `output_folder` |
| Private attributes | underscore prefix | `self._progress_callback` |

### Formatting

- **Indentation:** 4 spaces (no tabs)
- **Strings:** Double quotes (`"string"` not `'string'`)
- **Line length:** ~120 characters (no strict enforcement)
- **No automated formatter** configured (Black, autopep8, etc.)

### Import Organization

```python
# 1. Standard library imports
import os
import json
import threading
from pathlib import Path
from typing import List, Optional, Callable

# 2. Third-party packages
import requests
from PIL import Image
from rich.console import Console

# 3. Local modules
from path_utils import get_config_path, VALID_EXTENSIONS
from kling_generator_falai import FalAIKlingGenerator

# Relative imports within kling_gui/ package
from .drop_zone import DropZone
from .video_looper import create_looped_video
```

### Type Hints

Always use type hints for function signatures:

```python
def get_output_video_path(
    image_path: str, 
    output_folder: str, 
    model_short: str = "kling", 
    prompt_slot: int = 1
) -> Path:
    """Get the default output video path for an image."""
    ...

def validate_file(self, file_path: str) -> tuple:
    """Returns (is_valid: bool, error_message: str)"""
    ...
```

Common type imports:
```python
from typing import List, Optional, Dict, Any, Callable, Tuple
```

### Docstrings

Use Google-style docstrings:

```python
def create_kling_generation(
    self,
    character_image_path: str,
    output_folder: str = None,
    custom_prompt: str = None,
    duration: int = 10
) -> Optional[str]:
    """Create Kling video via fal.ai.

    Args:
        character_image_path: Path to source image
        output_folder: Fallback output folder
        custom_prompt: Custom generation prompt
        duration: Video duration in seconds (default 10)

    Returns:
        Output video path on success, None on failure
    """
```

---

## Error Handling

### Pattern: Try-except at operation boundaries

```python
# API calls - use specific exceptions with retry logic
try:
    response = requests.post(url, headers=headers, json=payload, timeout=30)
except requests.exceptions.Timeout:
    logger.warning("Request timeout, retrying...")
    # Retry logic
except requests.exceptions.ConnectionError as e:
    logger.error(f"Connection error: {e}")
    return None
```

### Pattern: Per-item errors in batch processing

```python
# Continue processing remaining items on per-item errors
for item in items:
    try:
        result = process_item(item)
    except Exception as e:
        item.status = "failed"
        item.error_message = str(e)
        self.log(f"Error processing {item.filename}: {e}", "error")
        continue  # Don't abort entire batch
```

### Pattern: GUI error display

```python
# Log error, don't crash
try:
    result = risky_operation()
except Exception as e:
    self.log(f"Operation failed: {e}", "error")
    # GUI stays responsive
```

---

## Architecture Quick Reference

### Entry Points

| Entry | File | Description |
|-------|------|-------------|
| CLI | `kling_automation_ui.py` | Menu-driven terminal UI |
| GUI | `kling_gui/main_window.py` | Tkinter drag-and-drop interface |

### Core Components

| Component | File | Purpose |
|-----------|------|---------|
| Generator | `kling_generator_falai.py` | fal.ai API integration |
| Queue | `kling_gui/queue_manager.py` | Thread-safe processing queue |
| Config Panel | `kling_gui/config_panel.py` | Model/prompt/output settings |
| Drop Zone | `kling_gui/drop_zone.py` | Drag-and-drop + click-to-browse |
| Log Display | `kling_gui/log_display.py` | Color-coded scrolling log |
| Path Utils | `path_utils.py` | Path helpers, PyInstaller compat |

### Automation Pipeline Components

| Component | File | Purpose |
|-----------|------|---------|
| Defaults / config merge | `automation/config.py` | Normalize `automation_*` settings from app config |
| Case discovery | `automation/discovery.py` | Find case folders with `front.jpg/png` and existing outputs |
| Manifest | `automation/manifest.py` | Atomic per-case/per-step state for resume/retry |
| Runner | `automation/pipeline.py` | Orchestrates full 7-step automated flow |
| Face extraction service | `face_crop_service.py` | Headless portrait crop for CLI pipeline |

### Oldcam Version Wiring

See [`docs/oldcam-wiring.md`](docs/oldcam-wiring.md) for the complete step-by-step checklist.

Quick touch-points when adding vN:

- `oldcam-vN/` folder: `oldcam.py`, `launcher.py`, `requirements.txt`, `oldcam_launcher.bat` (CRLF)
- `oldcam-vN/macOS/`: `oldcam.py`, `oldcam.command` (LF)
- Launcher files at 3 levels: `launchers/windows/`, `launchers/macos/`, `launchers/`
- `kling_gui/config_panel.py`: add `"vN"` to `oldcam_version_vars` dict + loop tuple; update tooltip method
- `kling_gui/queue_manager.py`: add `"vN"` to `requires_mediapipe` set if vN uses face landmarks
- `tests/test_oldcam_versions.py`: add vN to version tuple + output-suffix test + mediapipe test
- `tests/test_launcher_hub_wrappers.py`: add launcher path/target assertions
- `distribution/release_prep.py`: add new launcher filenames explicitly (algorithm folder auto-included via tree walk)
- If new default: update `automation/config.py` + 5 `run_oldcam` launcher files

**Auto-discovered:** `_discover_oldcam_versions()` scans `oldcam-v*` dirs — no hardcoded version list anywhere in pipeline/automation code.

### Automation Manifest Semantics

- Fixed step keys:
  - `front_expand`, `extract_portrait`, `selfie_generate`, `similarity_gate`, `selfie_expand`, `video_generate`, `oldcam`
- Step fields:
  - `status`, `output`, `error`, `meta`, timestamps
- Statuses:
  - `pending`, `running`, `complete`, `manual_review`, `failed`, `skipped`
- Resume behavior depends on manifest state plus config fingerprint compatibility.
- Corrupt/invalid manifests are quarantined and recreated by loader logic.

### Threading Model

```python
# GUI updates must use root.after() for thread safety
def update_from_worker_thread():
    self.root.after(0, lambda: self.update_display())

# Queue manager uses Lock for shared state
with self.lock:
    for item in self.items:
        if item.status == "pending":
            item.status = "processing"
            return item
```

---

## Key Implementation Patterns

### Progress Callback Pattern

```python
# Generator supports callback injection for verbose mode
def progress_callback(message: str, level: str = "info"):
    self.log_verbose(message, level)

if config.get("verbose_gui_mode", False):
    self.generator.set_progress_callback(progress_callback)
```

### Configuration Access

```python
# Config is JSON file, loaded at startup
config = self.load_config()  # Returns dict

# Access with defaults
use_source = config.get("use_source_folder", True)
model = config.get("current_model", "fal-ai/kling-video/v2.1/pro/image-to-video")

# Save after changes
self.save_config()
```

### Filename Generation

```python
# Output filename pattern: {image_stem}_kling_{model_short}_p{slot}.mp4
# Example: selfie_kling_k25turbo_p2.mp4

model_short = self.get_model_short_name()  # k25turbo, wan25, veo3, etc.
filename = f"{image_stem}_kling_{model_short}_p{prompt_slot}.mp4"
```

---

## Log Levels

| Level | Color | Usage |
|-------|-------|-------|
| info | Light gray | General information |
| success | Bright green | Completion messages |
| error | Coral red | Failures |
| warning | Yellow | Non-fatal issues |
| upload | Dark cyan | Upload progress (verbose) |
| task | Sky blue | Task creation (verbose) |
| progress | Gold | Generation progress (verbose) |
| debug | Gray | Debug info (verbose) |
| resize | Plum | Image resize (verbose) |
| download | Pale green | Download progress (verbose) |
| api | Orchid | API responses (verbose) |

---

## File Locations

| Purpose | Location |
|---------|----------|
| User config | `kling_config.json` (auto-generated) |
| GUI log | `kling_gui.log` |
| CLI log | `kling_automation.log` |
| Chrome profile | `chrome_profile/` (for balance tracker) |
| Distribution | `distribution/` (self-contained copy) |

---

## Valid Image Extensions

```python
VALID_EXTENSIONS = {'.jpg', '.jpeg', '.png', '.webp', '.bmp', '.gif', '.tiff', '.tif'}
```

---

## Common Pitfalls

1. **Thread Safety:** Always use `threading.Lock()` when accessing shared queue state
2. **Tkinter Updates:** Never update UI from worker thread - use `root.after()`
3. **Path Handling:** Use `Path` objects, handle both Windows and Unix paths
4. **API Timeouts:** Always set timeout on requests (30s for API, 120s for downloads)
5. **Duplicate Detection:** Check both base and `_looped` variants when checking for existing videos
6. **Distribution Sync:** Files in `distribution/` must be manually synced with root
7. **Resume Expectations:** Existing case folders are reusable test fixtures, not one-time-only runs.
8. **Platform Drift:** Avoid Windows-only assumptions in shared automation code paths.

---

## Reusable Test Folder Pattern

Use stable repeatable automation roots:

```text
test_root/
  case_a/front.jpg|front.png
  case_b/front.jpg|front.png
```

Guidance:

- Re-run these folders across multiple test cycles.
- Use run/resume for manifest continuation validation.
- Use fresh root or cleaned outputs/manifest for strict clean-path retests.

---

## macOS Guardrails For Agents

- Keep launcher/documentation parity for:
  - `setup_macos.sh`
  - `run_gui.sh` / `run_gui.command`
  - `run_cli.sh` / `run_cli.command`
- Preserve Tk requirements in docs and troubleshooting.
- Do not prescribe Windows-only fix commands for shared macOS workflows.

---

## Hard Rules — Windows Launchers (NON-NEGOTIABLE)

Repeated breakage has occurred from ignoring these. Treat as blocking requirements.

### CRLF endings — bat/cmd files

`Write` and `Edit` tools produce LF-only files. LF-only batch files garble every command on Windows. **Never use `Write`/`Edit` for `.bat`/`.cmd` files.** Always write via PowerShell:

```powershell
$crlf = $content -replace "`r`n","`n" -replace "`n","`r`n"
[System.IO.File]::WriteAllText("path\file.bat", $crlf, [System.Text.Encoding]::ASCII)
```

Verify: `CRLF=True`, `LFonly=False`. macOS `.sh`/`.command` files use LF — `Write`/`Edit` are fine for those.

### Blank lines — use `echo(` not `echo.`

`echo.` causes `'. was unexpected at this time'` with `enabledelayedexpansion`. Use `echo(` unconditionally for blank lines.

### Log appends — redirect operator before echo

```bat
>>"%LOG_FILE%" echo [%LAUNCH_TS%] message   ← CORRECT
echo [%LAUNCH_TS%] message >> "%LOG_FILE%"  ← WRONG ([ ] may be misinterpreted)
```

### Launcher chain

```
root\run_gui.bat  →  launchers\run_gui.bat  →  launchers\windows\run_gui.bat
```

Root files are pass-through wrappers only. All logic lives in `launchers/windows/`.

### Dep-skip stamp (no subprocess)

```bat
for %%F in (req files...) do set "STAMP_KEY=!STAMP_KEY!%%~tF%%~zF"
set "STAMP=%STATE_DIR%\deps_%STAMP_KEY:~0,60%.ok"
if exist "%STAMP%" goto :launch
```

No `certutil` or hash subprocess — date+size fingerprint is sufficient and instant.

### MediaPipe install

Always filter mediapipe from requirements before pip, then install separately:

```bat
"%VENV_PYTHON%" -m pip install --no-deps "mediapipe==0.10.35"
```

### POSIX redirects don't work in cmd — use `nul` not `/dev/null`

cmd.exe has no `/dev/null`. A POSIX `>/dev/null 2>&1` redirect silently fails on Windows (cmd interprets `/dev/null` as a path and errors with "the system cannot find the path specified", buried in stderr). I shipped 14 such redirects to `similarity/run_gui.bat` in commit `afe0540b` and CodeRabbit caught it; without that catch, every venv-create / python -V probe / dep-check would have failed on your Windows pull.

```bat
rem WRONG — POSIX redirect, silently fails on Windows
"!PYTHON_BIN!" -c "import tkinter" >/dev/null 2>&1

rem CORRECT — cmd-native null device
"!PYTHON_BIN!" -c "import tkinter" >nul 2>&1
```

Pre-push grep: `rg -n --iglob '*.bat' --iglob '*.cmd' '/dev/null'` MUST return zero hits.

Also watch for: `&>` (POSIX merged redirect), backticks, `$(...)` command substitution, `[[ ... ]]` tests, Unix-style `/usr` / `/bin` / `/tmp` paths. None of these work in cmd.

### Name override env vars in launcher error messages

Launchers that honor env-var overrides (`SELFIEGEN_PYTHON`, `SELFIEGEN_VENV_DIR`) MUST mention the active override by name in any error message so users know how to unset it. If you add a new override branch, mirror the existing pattern:

```bat
if not "%SELFIEGEN_PYTHON%"=="" (
  echo ERROR: SELFIEGEN_PYTHON points at Python !PY_ACTUAL!, but requires X.Y. Unset it or point at supported version.
) else if not "%SELFIEGEN_VENV_DIR%"=="" (
  echo ERROR: SELFIEGEN_VENV_DIR points at Python !PY_ACTUAL!, ...
) else (
  echo ERROR: resolved Python is !PY_ACTUAL!, ... (resolver bug — file an issue)
)
```

Reference implementation: `similarity/run_gui.bat:65-72` (fixed in commit `cb876b44` after CodeRabbit caught that only `SELFIEGEN_PYTHON` was named).

---

## Hard Rules — GUI Sash Layout

### Saved config wins over code defaults

`kling_config.json` sash values (gitignored) are restored on every launch and override all code-level defaults. When changing layout targets, you **must also** clear stale saved values:

```python
import json; c=json.load(open('kling_config.json'))
for k in ['sash_dropzone','sash_queue','sash_log','sash_log_drop_split','sash_prompt_split']: c.pop(k,None)
json.dump(c,open('kling_config.json','w'),indent=2)
```

### `sash_log_drop_split` is relative to right section, not full window

`log_drop_paned` is inside the right section (`safe_w - sash_queue`). Clamping as % of full window width produces values larger than the pane. Always:

```python
right_section_w = max(400, safe_w - clamped_queue)
log_drop_min = int(right_section_w * 0.42)
log_drop_max = int(right_section_w * 0.62)
```

### Target proportions

| Sash | Target | Range |
|------|--------|-------|
| `sash_prompt_split` | 56% of window | 50–62% |
| `sash_queue` (carousel) | 24% of window | 20–30% |
| `sash_log_drop_split` | 52% of right section | 42–62% |

## Adding New Features

### New GUI Component

1. Create `kling_gui/new_component.py`
2. Add to `kling_gui/__init__.py` exports
3. Import and use in `kling_gui/main_window.py`

### New API Integration

1. Create new file in root (e.g., `new_api_client.py`)
2. Follow `FalAIKlingGenerator` pattern with progress callbacks
3. Import in queue_manager or create parallel processing path

### New CLI Command

1. Add method to `KlingAutomationUI` class in `kling_automation_ui.py`
2. Add menu option in `display_configuration_menu()`
3. Handle in `run_configuration_menu()` switch

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
| Standalone GUI/CLI import | `from src.engine import FaceEngine` in `similarity/src/{gui,cli}.py` (engine score is a raw float — caller must format) |

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
| Platform impl | `launchers/windows/run_<name>.bat` (CRLF, `echo(` for blanks) | `launchers/macos/run_<name>.command` (LF) | Real venv/dep/exec logic |
| Standalone subproject | `similarity/run_<name>.{bat,command}` | same | Used by hub wrappers `launchers/{windows,macos}/run_similarity_*` |
| Build pipeline | `distribution/release_prep.py:copy_sanitized_tree` | same | Walks tree → auto-included unless excluded |

### E. Score display formatting (avoid raw floats)

| Surface | File | Format |
|---------|------|--------|
| Main GUI badge | `kling_gui/carousel_widget.py` (uses `face_similarity.compute_face_similarity_details` which returns int 0-100) | `f"{score}%"` (already int) |
| Standalone GUI label | `similarity/src/gui.py::_on_comparison_complete` | `f"{float(result['score']):.1f}%"` (engine returns float) |
| Standalone CLI Rich panel | `similarity/src/cli.py::_display_result` | `f"{float(result['score']):.1f}%"` (engine returns float) |
| Filename `_simN_` tag | `selfie_generator.py` | `int(round(...))` (no decimal) |
| Diagnostic logs | `face_similarity._diag_summary` | Raw `mapped=N` (int after adapter) |
| **User-facing selfie-generation log** | `selfie_generator.py:_format_similarity_diagnostics` (helper at lines 189-213) | `f"Similarity: {N}% (cosine_distance={d:.3f}, threshold={t:.2f}, models={m})"` — MUST include raw distance + threshold; falls back gracefully when diagnostics missing |

**Why the user-facing row matters:** the polynomial mapping in `similarity_engine._score_from_distance` deliberately spreads ArcFace cosine distances 0.0–0.68 across scores 100–80%. A pegged 99% reading from a small distance is mathematically correct, but indistinguishable from a degenerate fallback if the raw distance is hidden. Always emit `_format_similarity_diagnostics(details.get("diagnostics"))` alongside the mapped score. v1.9 calibration with reference table lives in `similarity_engine.py:625-640` (`PASS_CURVE_EXPONENT`). Canonical threshold constant at `face_similarity.RAW_DISTANCE_THRESHOLD` (`= 0.68`). Tests in `tests/test_selfie_generator_similarity_log.py`. Fixed in commits `e7ac7284` + `089d631b`.

### F. FAS (anti-spoof) display wording

FAS is **ADVISORY ONLY** — it does NOT gate the verdict (unless `automation_similarity_require_fas_pass=true`). False positives are common on passport photos, low-light selfies, and printed IDs. Always render in **amber** (`#FFC107` / `yellow`) with softened wording like "possible synthetic input on ref/target (advisory only)" — NEVER as a hard red FAIL when the similarity verdict passes.

### G. Pre-flight checklist (run BEFORE every similarity-stack commit)

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

