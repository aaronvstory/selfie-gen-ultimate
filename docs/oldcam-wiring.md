# Oldcam Version Wiring Reference

> How to add a new Oldcam version (e.g., v13) to every layer of the project.
> All steps are required unless marked "auto-discovered."

---

## Version Comparison Table

| Version | Face Track | Biological Pulse | AWB Drift | Sensor Noise | AE Stepping | MediaPipe | Signature |
|---------|:----------:|:----------------:|:---------:|:------------:|:-----------:|:---------:|-----------|
| v7      | No         | No               | No        | Yes          | Yes         | No        | Basic sensor noise + film LUT |
| v8      | No         | No               | Yes       | Yes          | Yes         | No        | + AWB color drift |
| v9      | Yes        | No               | Yes       | Yes          | Yes         | Yes       | + Face-aware masking, temporal mesh |
| v10     | Yes        | Yes              | No        | Yes          | Yes         | Yes       | + FFT biological sync (AWB removed) |
| v11     | Yes        | Yes              | Yes       | Yes          | Yes         | Yes       | + AWB after FFT — best of all |
| v12     | No         | No               | Yes       | Yes          | Yes         | No        | Pristine hardware-only (anti-spoofing aware): no rPPG, no LUT, no CLAHE |
| v13     | No         | No               | Yes       | **No**       | **No**      | No        | High-end daylight: no noise / AE / ghosting, pure optics (superseded by v14) |
| v14     | No         | No               | Yes (mult.)| Floor*      | **No**      | No        | Forensic daylight: multiplicative AWB, sub-perceptual sensor floor, smoothstep bloom, lossless temp encode, audio-preserving (superseded by v15) |
| v15     | No         | No               | Yes (mult.)| **No**       | **No**      | No        | Temporal Mute ★ default: V14 math/encoding + V13 noise-free + V12 ghosting (--ghosting 0.18). No sensor floor (it was V14's frequency-detector tell). Best Resemble-API result |

\* **Floor** = a sub-perceptual read/shot sensor *floor* (max ≈ 2/255), not the
visible ISO grain v7–v11 add. It defeats SNR/PAD detectors without altering the
look. v15 removes the floor entirely (Resemble testing showed the preserved
floor is itself a frequency-detector tell) and restores V12 temporal blending.
**Current default version: v15** (superseded v14). Mediapipe versions:
v9, v10, v11 — v14/v15 do not use mediapipe.

---

## Signal Pipeline Order (v9+ architectural invariant)

For any version that combines FFT biological pulse with AWB drift, the execution order is non-negotiable:

```text
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

```text
oldcam-vN/
├── oldcam.py               ← main algorithm (start from previous version)
├── launcher.py             ← pass-through launcher (Tk filedialog — Windows-only by convention)
├── requirements.txt        ← numpy + cv2 (+ mediapipe ONLY if version uses face landmarks)
├── oldcam_launcher.bat     ← Windows launcher (CRLF — see bat rules below)
└── macOS/
    ├── oldcam.py           ← byte-for-byte mirror of Windows oldcam.py
    └── oldcam.command      ← macOS launcher (LF + 100755)
```

> **DO NOT copy launcher files from v7–v13.** They predate CLAUDE.md macOS Rule 9
> (Python-version validation on every venv candidate) and Rule 10 (`set -euo
> pipefail` parity). Copying from them silently reintroduces the resolver bug
> that bites users with a stale `.venv` symlinked to an unsupported Python
> (3.13+). **Use `oldcam-v15/macOS/oldcam.command` and
> `oldcam-v15/oldcam_launcher.bat` (or the identical v14 pair) as your
> reference templates** — they ship with `_python_supported()` / `:check_py`
> per-candidate gates.
>
> For non-launcher files (`oldcam.py`, `launcher.py`, `requirements.txt`),
> copying from the previous version is fine and conventional.

### 2. Launcher files (3 levels)

| File | Line endings | Notes |
|------|-------------|-------|
| `launchers/windows/run_oldcam_vN.bat` | CRLF | delegates to `oldcam-vN\oldcam_launcher.bat` |
| `launchers/macos/run_oldcam_vN.command` | LF | delegates to `oldcam-vN/macOS/oldcam.command` |
| `launchers/run_oldcam_vN.bat` | CRLF | delegates to `launchers\windows\run_oldcam_vN.bat` |
| `launchers/run_oldcam_vN.command` | LF | delegates to `launchers/macos/run_oldcam_vN.command` |

Pattern for `launchers/windows/run_oldcam_vN.bat` (a script in `launchers/windows/` needs `..\..` to reach repo root):
```bat
@echo off
setlocal
for %%I in ("%~dp0..\..") do set "ROOT_DIR=%%~fI"
call "%ROOT_DIR%\oldcam-vN\oldcam_launcher.bat" %*
set "EXIT_CODE=%ERRORLEVEL%"
endlocal & exit /b %EXIT_CODE%
```

Pattern for `launchers/run_oldcam_vN.bat` (a script in `launchers/` needs `..` to reach repo root):
```bat
@echo off
setlocal
for %%I in ("%~dp0..") do set "ROOT_DIR=%%~fI"
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

# ~line 530 — add to enumerate tuple (checkboxes use grid(), _OLDCAM_COLS=3)
for i, version in enumerate(("v7", "v8", "v9", "v10", "v11", "v12", "v13", "vN")):  # ← add "vN"
# The grid layout auto-wraps: new versions add rows, never widen the strip.
# _OLDCAM_COLS = 3 is defined just before the loop. With 5 versions it shows 2 rows
# (3 + 2); 6 versions → 2 even rows (3 + 3); 7+ → 3 rows. Change to 4 if going wider.

# Update _get_oldcam_version_notes() to add an entry for vN
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

`distribution/release_prep.py` auto-includes `oldcam-v*/` via tree walk — no change needed for the algorithm folder.

New launcher files **must** be added explicitly to the launcher include list in `distribution/release_prep.py`:
```python
# Existing pattern — add new filenames:
"launchers/windows/run_oldcam_vN.bat",
"launchers/macos/run_oldcam_vN.command",
"launchers/run_oldcam_vN.bat",
"launchers/run_oldcam_vN.command",
```

### 8. Pre-commit portability checklist (mandatory for every new vN)

The macOS portability rules in `CLAUDE.md` and `AGENTS.md` bite hard when adding a new oldcam version because you're introducing **8 new launcher files at once**. Run this verification BEFORE pushing:

**Per-file line-ending + exec-bit requirements:**

| File | Line endings | Exec bit | How to write |
|---|---|---|---|
| `oldcam-vN/oldcam_launcher.bat` | **CRLF** | n/a | PowerShell `[System.IO.File]::WriteAllText` with explicit CRLF; never use `Write`/`Edit` tools |
| `oldcam-vN/macOS/oldcam.command` | **LF** | **100755** | `Write`/`Edit` is OK for content (LF default); then `chmod +x f && git update-index --chmod=+x f` |
| `launchers/windows/run_oldcam_vN.bat` | **CRLF** | n/a | PowerShell with explicit CRLF |
| `launchers/macos/run_oldcam_vN.command` | **LF** | **100755** | Same as `oldcam.command` above |
| `launchers/run_oldcam_vN.bat` | **CRLF** | n/a | PowerShell with explicit CRLF |
| `launchers/run_oldcam_vN.command` | **LF** | **100755** | Same as above |

**Verification commands (run for every new file):**

```bash
# Line endings — must match table above for every file
git ls-files --eol oldcam-vN/oldcam_launcher.bat                          # i/crlf w/crlf
git ls-files --eol oldcam-vN/macOS/oldcam.command                          # i/lf   w/lf
git ls-files --eol launchers/{windows,macos,/}/run_oldcam_vN.{bat,command} # match table per row

# Exec bit on .command files — must be 100755
git ls-files --stage oldcam-vN/macOS/oldcam.command                        # 100755 ...
git ls-files --stage launchers/macos/run_oldcam_vN.command                 # 100755 ...
git ls-files --stage launchers/run_oldcam_vN.command                       # 100755 ...

# Whole-tree pre-push gate (catches CRLF in .sh/.command and missing exec bits)
bash scripts/check_macos_portability.sh

# Windows-launcher hygiene (catches /dev/null POSIX redirects in .bat)
rg -n --iglob 'oldcam-vN/*.bat' --iglob 'launchers/**/run_oldcam_vN.bat' '/dev/null'   # MUST return empty

# CLAUDE.md macOS Rule 9 — Python-version validation on every venv candidate
grep -q '_python_supported' oldcam-vN/macOS/oldcam.command     # MUST match
grep -q ':check_py' oldcam-vN/oldcam_launcher.bat              # MUST match

# CLAUDE.md macOS Rule 10 — set -euo pipefail parity across .command siblings
grep -q 'set -euo pipefail' oldcam-vN/macOS/oldcam.command            # MUST match
grep -q 'set -euo pipefail' launchers/macos/run_oldcam_vN.command     # MUST match
grep -q 'set -euo pipefail' launchers/run_oldcam_vN.command           # MUST match
```

**The portability gate (`scripts/check_macos_portability.sh`) catches CRLF in shell scripts and missing exec bits, but NOT `/dev/null` in `.bat` files, NOT Rule 9 resolver-validation absence, NOT Rule 10 set-flag drift** — those are only caught by the greps above, by `tests/test_oldcam_launcher_resolver.py` (Rule 9 static-text gate for v14 and v15), or by code review. See `AGENTS.md` "Hard Rules — Windows Launchers" for the full set of `.bat` traps (`/dev/null`, `echo.` vs `echo(`, redirect operator order, override env-var naming), and CLAUDE.md macOS Rules 9–10 for the full resolver pattern with its reasoning.

### 9. Known defect — pre-v14 launchers do not implement Rule 9/10

This is a deliberate carve-out, not an instruction to follow the same pattern:

- `oldcam-v7/...v13/macOS/oldcam.command` start with `set -u` instead of `set -euo pipefail`, in violation of CLAUDE.md macOS Rule 10. (v14 and v15 are compliant.)
- `oldcam-v7/...v13/oldcam_launcher.bat` lack the `:check_py` subroutine and silently accept any `.venv` candidate without validating the Python version, in violation of CLAUDE.md macOS Rule 9. (v14 and v15 are compliant.)

These versions ship and work for users on stock Python installs because no user has yet hit the failure-case Python-version drift. **Do not "fix" them in unrelated PRs.** If you need to retrofit them, do it in a dedicated PR with explicit live testing on each version, because mediapipe-using versions (v9 / v10 / v11) have additional Python-version constraints (mediapipe wheels are only built for a narrow range) and need careful smoke-testing on a clean venv.

The blessed reference for new vN+1 launchers is **v14 or v15** (their launcher pairs are identical Rule 9/10-compliant templates) — explicitly stated in §1 above.

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
