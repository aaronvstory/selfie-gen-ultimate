# Oldcam Version Wiring Reference

> How to add a new Oldcam version (e.g., v12) to every layer of the project.
> All steps are required unless marked "auto-discovered."

---

## Version Comparison Table

| Version | Face Track | Biological Pulse | AWB Drift | MediaPipe | Signature |
|---------|:----------:|:----------------:|:---------:|:---------:|-----------|
| v7      | No         | No               | No        | No        | Basic sensor noise + film LUT |
| v8      | No         | No               | Yes       | No        | + AWB color drift |
| v9      | Yes        | No               | Yes       | Yes       | + Face-aware masking, temporal mesh |
| v10     | Yes        | Yes              | No        | Yes       | + FFT biological sync (AWB removed) |
| v11     | Yes        | Yes              | Yes       | Yes       | + AWB after FFT — best of all ★ default |

---

## Signal Pipeline Order (v9+ architectural invariant)

For any version that combines FFT biological pulse with AWB drift, the execution order is non-negotiable:

```
1. get_dynamic_region_masks()              ← face detection
2. synchronize_base_frequency()            ← FFT reads CLEAN green channel (no AWB yet)
3. apply_synchronized_spatial_fluctuation() ← biological pulse injected
4. apply_global_awb_drift()               ← AWB drift LAST (hardware layer on top)
5. apply_soft_ois_jitter()
6. apply_soft_rolling_shutter()
...
```

**Why:** `synchronize_base_frequency()` reads `g_history` (green channel values) to detect 0.8–1.8 Hz biological frequencies. If `apply_global_awb_drift()` runs first, it corrupts `g_history` with hardware-level channel noise, causing the FFT to pick up camera artifacts instead of skin microcirculation.

---

## Touch-Points Checklist (copy for v12+)

### 1. Algorithm folder

```
oldcam-vN/
├── oldcam.py               ← main algorithm (start from previous version)
├── launcher.py             ← pass-through launcher (copy from v10/v11)
├── requirements.txt        ← mediapipe + cv2 + numpy (copy from v10/v11 if using mediapipe)
├── oldcam_launcher.bat     ← Windows launcher (CRLF — see bat rules below)
└── macOS/
    ├── oldcam.py           ← byte-for-byte mirror of Windows oldcam.py
    └── oldcam.command      ← macOS launcher (LF, copy from v10/v11 and update version strings)
```

### 2. Launcher files (3 levels)

| File | Line endings | Notes |
|------|-------------|-------|
| `launchers/windows/run_oldcam_vN.bat` | CRLF | delegates to `oldcam-vN\oldcam_launcher.bat` |
| `launchers/macos/run_oldcam_vN.command` | LF | delegates to `oldcam-vN/macOS/oldcam.command` |
| `launchers/run_oldcam_vN.bat` | CRLF | delegates to `launchers\windows\run_oldcam_vN.bat` |
| `launchers/run_oldcam_vN.command` | LF | delegates to `launchers/macos/run_oldcam_vN.command` |

Pattern for `launchers/windows/run_oldcam_vN.bat`:
```bat
@echo off
setlocal
for %%I in ("%~dp0..\.") do set "ROOT_DIR=%%~fI"
call "%ROOT_DIR%\oldcam-vN\oldcam_launcher.bat" %*
set "EXIT_CODE=%ERRORLEVEL%"
endlocal & exit /b %EXIT_CODE%
```

Pattern for `launchers/run_oldcam_vN.bat`:
```bat
@echo off
setlocal
for %%I in ("%~dp0.") do set "ROOT_DIR=%%~fI"
call "%ROOT_DIR%\launchers\windows\run_oldcam_vN.bat" %*
set "EXIT_CODE=%ERRORLEVEL%"
endlocal & exit /b %EXIT_CODE%
```

### 3. Update the default (when vN replaces the current default)

| File | Change |
|------|--------|
| `launchers/windows/run_oldcam.bat` | delegate to `run_oldcam_vN.bat` |
| `launchers/macos/run_oldcam.command` | exec `run_oldcam_vN.command` |
| `launchers/run_oldcam.bat` | delegate to `launchers\windows\run_oldcam.bat` |
| `launchers/run_oldcam.command` | exec `launchers/macos/run_oldcam.command` |
| `run_oldcam.bat` (root, thin wrapper) | delegate to `launchers\windows\run_oldcam_vN.bat` |
| `automation/config.py` | `"automation_oldcam_version": "vN"` |

### 4. GUI checkbox — `kling_gui/config_panel.py`

```python
# ~line 514 — add to version_vars dict
self.oldcam_version_vars = {
    "v7": tk.BooleanVar(value=False),
    ...
    "v11": tk.BooleanVar(value=True),
    "vN": tk.BooleanVar(value=False),  # ← add (set True if new default)
}

# ~line 522 — add to loop tuple
for version in ("v7", "v8", "v9", "v10", "v11", "vN"):  # ← add "vN"

# Update _get_oldcam_version_notes() to add a row for vN
```

### 5. MediaPipe dependency flag — `kling_gui/queue_manager.py`

If vN uses MediaPipe (FaceLandmarker), add it to the set:
```python
requires_mediapipe = version in {"v9", "v10", "v11", "vN"}
```

### 6. Tests

**`tests/test_oldcam_versions.py`**:
```python
# Add vN to the versions tuple in test_oldcam_all_versions_discovered_and_sorted
for version in ("v7", "v8", "v9", "v10", "v11", "vN"):

# Add output-suffix test
def test_vN_default_output_path_uses_vN_suffix():
    ...
    assert str(output_path).endswith("-oldcam-vN.mp4")

# If uses mediapipe:
def test_oldcam_dependency_preflight_requires_mediapipe_for_vN():
    ...
    assert preflight_result["mediapipe_required"] is True

# If ordering matters (biological pulse + AWB):
def test_vN_process_frame_applies_awb_drift_after_spatial_fluctuation():
    ...
```

**`tests/test_launcher_hub_wrappers.py`** — add launcher path/target assertions:
```python
"launchers/windows/run_oldcam_vN.bat": r'call "%ROOT_DIR%\oldcam-vN\oldcam_launcher.bat" %*',
"launchers/macos/run_oldcam_vN.command": 'exec "$ROOT_DIR/oldcam-vN/macOS/oldcam.command" "$@"',
"launchers/run_oldcam_vN.bat": r'call "%ROOT_DIR%\launchers\windows\run_oldcam_vN.bat" %*',
"launchers/run_oldcam_vN.command": 'exec "$ROOT_DIR/launchers/macos/run_oldcam_vN.command" "$@"',
```

### 7. Distribution

`build_release_zip.py` auto-includes `oldcam-v*/` via glob — no change needed for the algorithm folder.

New launcher files **must** be added explicitly to the launcher include list in `build_release_zip.py`:
```python
# Existing pattern — add new filenames:
"launchers/windows/run_oldcam_vN.bat",
"launchers/macos/run_oldcam_vN.command",
"launchers/run_oldcam_vN.bat",
"launchers/run_oldcam_vN.command",
```

---

## Auto-Discovered (No Manual Wiring Needed)

These work automatically for any `oldcam-vN` folder:

| Mechanism | File | How it works |
|-----------|------|-------------|
| Version discovery | `kling_gui/queue_manager.py` | `_discover_oldcam_versions()` scans `oldcam-v*` dirs |
| Version sort key | `kling_gui/queue_manager.py` | `_oldcam_version_key()` parses `"vN"` → N via regex |
| Output filename suffix | `kling_gui/queue_manager.py` | Generic pattern: `{name}-oldcam-vN.mp4` |
| Face landmarker task | `kling_gui/queue_manager.py` | Searched generically in the version folder |
| CLI pipeline | `automation/oldcam.py` | Fully version-agnostic, reads `automation_oldcam_version` from config |
| CLI pipeline | `automation/pipeline.py` | Calls automation layer — no version hardcoding |

---

## Critical Rules for Bat Files (NON-NEGOTIABLE)

**Always write `.bat`/`.cmd` files via PowerShell `WriteAllText` with explicit CRLF:**

```powershell
$content = "line1`r`nline2`r`n..."
[System.IO.File]::WriteAllText("path\file.bat", $content, [System.Text.Encoding]::ASCII)
```

**Never use the `Write` or `Edit` tools for bat files** — they produce LF-only files that garble every command on Windows.

Other bat rules:
- Use `echo(` not `echo.` for blank lines
- Log-file redirects: `>>"%LOG_FILE%" echo message` (redirect operator before echo)
- Dep-skip stamps: built from file dates+sizes via `for %%F in (...)` loop, no `certutil` subprocess
- MediaPipe: always `--no-deps`, always filtered out of requirements before main pip install

---

## What Changes When Making vN the New Default

Checklist — update all 6 of these atomically in one commit:

- [ ] `launchers/windows/run_oldcam.bat` — point to `run_oldcam_vN.bat`
- [ ] `launchers/macos/run_oldcam.command` — exec `run_oldcam_vN.command`
- [ ] `launchers/run_oldcam.bat` — chain through `launchers\windows\run_oldcam.bat`
- [ ] `launchers/run_oldcam.command` — chain through `launchers/macos/run_oldcam.command`
- [ ] Root `run_oldcam.bat` — thin wrapper stays, just points to updated chain
- [ ] `automation/config.py` — `"automation_oldcam_version": "vN"`
- [ ] `kling_gui/config_panel.py` — flip `value=True` to new version, `value=False` on old default
- [ ] CHANGELOG + `app_version.py` — bump version, document the change
