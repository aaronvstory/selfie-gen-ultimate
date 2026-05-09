# Kling UI

AI media generation toolkit for face cropping, portrait analysis, selfie generation, image expansion/outpainting, and batch video creation from one GUI.

![Kling UI](kling_ui_preview.png)

## What It Does

| Tab | Purpose |
| --- | --- |
| Face Crop | Extract 3:4 face crops and prepare portrait inputs |
| Prep | Analyze portraits with OpenRouter vision models and build prompt material |
| Selfie | Generate identity-based selfies with fal.ai and BFL models |
| Expand / Outpaint | Expand images with fal.ai/BFL workflows |
| Video | Batch image-to-video generation across supported fal.ai video models |

Images move through the pipeline: input -> crop -> prep -> selfie -> expand/outpaint -> video.

## CLI Automated Pipeline (End-to-End)

The CLI includes a manifest-driven automation flow in `kling_automation_ui.py`:

`front_expand -> extract_portrait -> selfie_generate -> similarity_gate -> selfie_expand -> video_generate -> oldcam`

Open CLI:

```powershell
python kling_automation_ui.py
```

Use **End-to-End Auto Pipeline** menu to:

1. select root folder
2. scan/preview cases
3. dry run
4. run/resume automation

Resume behavior:

- Completed valid cases are skipped when `automation_skip_completed=true`.
- Cases marked `manual_review` due to `similarity unavailable` are retryable in later runs.
- Manifest stores per-step status, output path, error, and metadata.
- Recommended expansion defaults: front expand `70%`, selfie expand `30%`.

## Reusable Test Folders (Repeatable Batch Testing)

Use a stable test root with reusable case folders:

```text
test_root/
  case_a/
    front.jpg  (or front.png)
  case_b/
    front.jpg  (or front.png)
```

Retest policy:

- You can rerun one case or both cases repeatedly across future validation cycles.
- Use **Run / resume** to continue from previous manifest state.
- To force clean-path retests:
  - use a fresh root folder, or
  - remove/reset `automation_manifest.json` and generated outputs for selected cases, then rerun.
- To test reuse logic specifically, keep previous outputs and rerun with skip/reuse settings enabled.

## Cross-Platform Launch Matrix

| Mode | Windows | macOS |
| --- | --- | --- |
| GUI | `launchers/run_gui.bat` | `launchers/run_gui.command` (or `./run_gui.sh`) |
| CLI | `launchers/run_cli.bat` | `launchers/run_cli.command` (or `./run_cli.sh`) |
| Setup | `python -m venv venv` + `pip install -r requirements.txt` | `./setup_macos.sh` |

macOS compatibility constraints:

- Python 3.11+ recommended.
- Tk must be available (`python -c "import tkinter"` must pass in `.venv-macos`).
- `.command` launchers may require one-time Gatekeeper approval.
- Shared pipeline behavior is intended to remain platform-consistent; avoid Windows-only assumptions in automation logic.

## Quick Start: Windows

1. Install Python 3.10+ from [python.org](https://python.org) and enable **Add Python to PATH**.
2. Double-click `launchers/run_gui.bat`.
3. Enter API keys in GUI settings.

Manual launch:

```powershell
python -m venv venv
venv\Scripts\pip install -r requirements.txt
venv\Scripts\python gui_launcher.py
```

The CLI menu is available with:

```powershell
python kling_automation_ui.py
```

## Quick Start: macOS

macOS needs Python 3.11+ with Tk support. The mac launchers prefer a Tk-capable interpreter and create `.venv-macos` automatically.

Run setup once, or whenever dependencies change:

```bash
./setup_macos.sh
```

Launch GUI from Terminal:

```bash
./run_gui.sh
```

Launch CLI from Terminal:

```bash
./run_cli.sh
```

Finder-friendly launchers:

- `launchers/run_gui.command`: opens the GUI
- `run_kling_ui.command`: GUI alias for users expecting the app name
- `launchers/run_cli.command`: opens the CLI menu

Compatibility wrappers remain at repo root:
- `run_gui.bat`, `run_cli.bat`
- `run_gui.command`, `run_cli.command`

If macOS Gatekeeper blocks a `.command` file, right-click it, choose **Open**, then confirm once.

If execute permissions are lost:

```bash
chmod +x setup_macos.sh run_gui.sh run_gui.command run_kling_ui.sh run_kling_ui.command run_cli.sh run_cli.command
```

## macOS Python And Tk

The GUI requires `tkinter`. If Homebrew Python lacks Tk, install the matching package, then rerun setup:

```bash
brew install python@3.11 python-tk@3.11
./setup_macos.sh
```

The python.org macOS installer usually includes Tk already. You can force a specific interpreter with:

```bash
KLING_PYTHON=/path/to/python3.11 ./setup_macos.sh
```

## API Keys

| Provider | Unlocks | Required for |
| --- | --- | --- |
| fal.ai | Video, selfie, expand/outpaint models | Most generation flows |
| Freeimage.host | Public image URLs for fal.ai workflows | Upload-dependent fal.ai flows |
| BFL | FLUX Kontext / FLUX 2 selfie models | BFL selfie and image tools |
| OpenRouter | Vision analysis | Prep tab |

The app can start without keys. Missing, rejected, or rate-limited keys should appear as targeted status messages instead of generic startup crashes.

## Outputs And User Data

Generated media still goes to `gen-images/` or `gen-videos/` near the source image. The path helpers avoid nesting generated folders when outputs are reused across tabs.

Runtime data locations:

- Windows: portable app-local files remain the default, matching the tested Windows workflow.
- macOS: config, logs, crash reports, model cache, and sessions live under `~/Library/Application Support/selfie-gen-ultimate/`.

## Dependency Checks

Check installed packages and external tools:

```bash
python dependency_checker.py
```

Check and optionally repair the runtime face stack:

```bash
python dependency_health_check.py --mode check
python dependency_health_check.py --mode repair
```

`tensorflow-intel` repair is Windows-only. macOS uses the normal TensorFlow package path.

## Build Standalone Windows EXE

```powershell
build_gui_exe.bat
```

This uses PyInstaller to produce a portable `dist/KlingUI/` folder. `tkinterdnd2` must be available in the build environment.

## Build Shareable Release Zips (Windows GUI/CLI + macOS Portable)

```powershell
python distribution/build_release.py
```

This creates versioned bundles in `release/`:
- `windows_gui.zip`
- `windows_cli.zip`
- `macos_portable.zip`

Each bundle is sanitized for sharing:
- API keys removed from distributable config
- personal runtime files/logs/history removed
- first-launch instructions included (`README_FIRST_RUN.txt`)

## Requirements

- Python 3.10+ on Windows
- Python 3.11+ with Tk on macOS
- `requests`, `Pillow`, `rich`, `tkinterdnd2`
- Face tools: `opencv-python-headless`, `retina-face`, `deepface`, `tf-keras`
- Optional: `selenium`, `webdriver-manager` for balance/browser workflows
- Optional: FFmpeg on PATH for video looping

## Troubleshooting

- GUI fails immediately on macOS: run `./setup_macos.sh` and confirm `python -c "import tkinter"` works in `.venv-macos`.
- Drag/drop unavailable: install or repair `tkinterdnd2`; file picker fallback remains usable.
- Model list fails: check fal.ai key, account status, and rate limits. Cached metadata remains usable when available.
- Face crop/prep fails: run `python dependency_health_check.py --mode check` to find broken TensorFlow/RetinaFace imports.
- API generation fails: verify provider credits and API keys in settings.

## GitHub Review Loop (No Auto-Merge)

Recommended PR loop:

1. push branch updates
2. trigger bot reviews (`@codoki review`, `@coderabbitai review`, `@codex review`)
3. fix only fresh actionable findings on latest commit range
4. rerun targeted tests
5. push again and re-check comments/checks

This repository workflow keeps PRs open for iterative validation; do not merge automatically after first green run.

## License

Private project - not for redistribution.
