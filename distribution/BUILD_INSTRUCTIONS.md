# Kling UI - Direct GUI Launcher Build Instructions

## Overview

This directory contains everything needed to build a **standalone .exe** that launches the Tkinter GUI **directly** (no CLI menu).

## Files Created

### Core Files
- **`gui_launcher.py`** - Entry point script that launches GUI directly
- **`kling_gui_direct.spec`** - PyInstaller configuration for building the exe
- **`build_gui_exe.bat`** - Automated build script (recommended)
- **`run_gui_direct.bat`** - Test launcher (runs Python script without building exe)

### Comparison with Existing Build

| File | Launches | Console | Entry Point |
|------|----------|---------|-------------|
| `KlingUI.exe` (existing) | CLI menu → GUI option | Yes | `kling_automation_ui.py` |
| `KlingGUI_Direct.exe` (new) | **Direct to GUI** | No | `gui_launcher.py` |

## Quick Start

### Option 1: Test Without Building (Recommended First)

```batch
run_gui_direct.bat
```

This runs the GUI launcher as a Python script to verify it works before building the exe.

### Option 2: Build the Standalone Executable

```batch
build_gui_exe.bat
```

### Keep distribution mirror synced with current root code

```batch
sync_from_root.bat
```

### Build sanitized shareable bundles (GUI/CLI/macOS)

```batch
build_release.bat
```

**What it does:**
1. Creates a timestamped release folder under `release\`
2. Stages sanitized platform bundles (`windows_gui`, `windows_cli`, `macos_portable`)
3. Writes blank-key `kling_config.json` into each staged bundle
4. Produces `windows_gui.zip`, `windows_cli.zip`, and `macos_portable.zip`

**Output location:** `release\release_YYYYMMDD_HHMMSS\`

### `build_gui_exe.bat` behavior (direct GUI .exe build)

**What it does:**
1. Checks/installs PyInstaller
2. Installs required dependencies
3. Cleans previous builds
4. Builds `KlingGUI_Direct.exe`
5. Copies support files
6. Opens the output folder

**Output location:** `dist\KlingGUI_Direct\`

**Build time:** ~2-5 minutes (depending on system)

## Manual Build (Advanced)

If you prefer manual control:

```batch
:: Install PyInstaller
pip install pyinstaller

:: Install dependencies
pip install requests Pillow rich tkinterdnd2 selenium webdriver-manager

:: Build
pyinstaller kling_gui_direct.spec --noconfirm
```

## Distribution

After building, share the **entire** `dist\KlingGUI_Direct\` folder:

```
dist\KlingGUI_Direct\
├── KlingGUI_Direct.exe    ← Main executable
├── _internal\             ← Required libraries (auto-generated)
├── kling_gui\             ← GUI modules
├── *.py                   ← Support scripts
└── README.txt             ← Usage instructions
```

**Users only need:**
- The complete `KlingGUI_Direct` folder
- No Python installation required
- Double-click `KlingGUI_Direct.exe` to launch

## Key Features

### Direct GUI Launch
- No CLI menu intermediary
- Straight to drag-and-drop interface
- Clean windowed application (no console)

### Frozen Exe Compatibility
- Uses `path_utils.py` for correct path resolution
- Config files save next to exe (portable)
- Crash logs save to `crash_log.txt`

### Error Handling
- Import errors show user-friendly dialog
- Runtime crashes logged to file
- Missing dependencies reported clearly

## Troubleshooting

### "Import Error" on Launch
Install missing dependencies:
```batch
pip install requests pillow rich tkinterdnd2
```

### Build Fails
1. Check Python is in PATH: `python --version`
2. Update pip: `python -m pip install --upgrade pip`
3. Clean build folders manually:
   ```batch
   rmdir /s /q build
   rmdir /s /q dist\KlingGUI_Direct
   ```
4. Try again: `build_gui_exe.bat`

### Drag-Drop Doesn't Work
- tkinterdnd2 DLLs may not have bundled correctly
- Rebuild with verbose output: `pyinstaller kling_gui_direct.spec --noconfirm --log-level=DEBUG`
- Check for warnings about missing tkinterdnd2 files

### Exe Shows Console Window
- Verify `console=False` in `kling_gui_direct.spec:127`
- Rebuild after changing spec file

## Configuration

### First Run
On first launch, the exe will:
1. Create `kling_config.json` (auto-detected from exe location)
2. Prompt for fal.ai API key
3. Set default output folder and prompts

### Settings Location
All config files save in the **same directory as the exe**:
- `kling_config.json` - User settings
- `kling_gui.log` - Application logs  
- `crash_log.txt` - Error reports

This makes the build **portable** - move the folder anywhere and settings persist.

## Technical Details

### PyInstaller Settings
```python
console=False          # No console window (clean GUI app)
name='KlingGUI_Direct' # Distinct from CLI version
upx=True              # Compression enabled
```

### Hidden Imports
All kling_gui submodules explicitly imported:
- `kling_gui.main_window`
- `kling_gui.config_panel`
- `kling_gui.drop_zone`
- `kling_gui.log_display`
- `kling_gui.queue_manager`
- `kling_gui.video_looper`

### Data Files Bundled
- Full `kling_gui/` package
- `kling_generator_falai.py`
- `path_utils.py`
- `balance_tracker.py`
- `selenium_balance_checker.py`
- tkinterdnd2 DLLs and TCL files

## Version Comparison

| Aspect | build_exe.bat (CLI) | build_gui_exe.bat (Direct) |
|--------|---------------------|----------------------------|
| Entry Point | kling_automation_ui.py | gui_launcher.py |
| User Flow | Menu → Select GUI option | Direct to GUI |
| Console | Visible | Hidden |
| Use Case | Power users, CLI fans | Simplicity, end users |

## Next Steps

1. **Test the script version** first: `run_gui_direct.bat`
2. **Build the exe**: `build_gui_exe.bat`  
3. **Test the exe**: `dist\KlingGUI_Direct\KlingGUI_Direct.exe`
4. **Distribute**: Share entire `dist\KlingGUI_Direct\` folder

## Support

If you encounter issues:
1. Check `kling_gui.log` for application errors
2. Check `crash_log.txt` for startup crashes
3. Run `run_gui_direct.bat` to test without exe build
4. Ensure all dependencies installed: `pip install -r requirements.txt`
