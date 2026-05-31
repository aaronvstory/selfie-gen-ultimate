# Hard Rules — Windows Launchers + GUI Sash Layout

> **Relocated from `CLAUDE.md` (2026-05-29) to reduce always-loaded context.**
> These rules are still binding (NON-NEGOTIABLE). Read this file BEFORE editing
> any `.bat`/`.cmd` launcher, the launcher chain, or GUI sash-layout code.

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

### 4b. Shared Python resolver + auto-install (`scripts/win_resolve_python.bat`)

**All Python detection for the main Windows launchers lives in ONE shared
file — do not re-add inline `where python` detection to a launcher.**
`launchers/windows/run_gui.bat` and `run_cli.bat` each `call
"%ROOT_DIR%\scripts\win_resolve_python.bat"` when the venv is missing. The
resolver runs in the caller's environment (it is `call`ed, not run with its
own `setlocal`) and returns `VENV_PYTHON` + `RESOLVE_RC` (0 = ok).

Why this exists: a non-technical user installed Python 3.12 but did not tick
"Add Python to PATH", so the old `where python` gate failed even though a
supported Python was present. The resolver fixes that and adds silent
auto-install.

Resolution order (every candidate version-gated to 3.9–3.12 via the flat-goto
`:pyres_check`, mirroring `oldcam-v24/oldcam_launcher.bat`):

1. existing venvs (`%VENV_PYTHON%`, `.venv311`, `.venv`) + `SELFIEGEN_PYTHON`
   / `SELFIEGEN_VENV_DIR` overrides
2. **`py` launcher** — `py -3.11`, `-3.12`, `-3.10`, `-3.9`. This is the key
   fix: `py.exe` ships with every python.org installer and selects by version
   from the registry, so it finds an interpreter **without** "Add to PATH".
3. `python` on PATH (last; may be an unsupported 3.13+)
4. common install dirs (`%LocalAppData%\Programs\Python\Python31{1,2}`,
   `%ProgramFiles%\Python31{1,2}`, `C:\Python31{1,2}`)
5. **auto-install Python 3.12** — `winget install Python.Python.3.12` first,
   then a python.org silent installer download
   (`/quiet PrependPath=1 Include_launcher=1`) as fallback.

Hard rules for this file:

- **Target 3.12, never "latest".** `mediapipe==0.10.35` has wheels for
  3.9–3.12 only; auto-installing 3.13+ would fail the version gate.
- **Post-install re-detection must use the `py` launcher / an absolute path,
  NOT a fresh `where python`** — the installer's PATH edit does not reach the
  already-running shell.
- **Flat-goto `:pyres_check` only** (no `if (...) else (...)`), and every
  paren-bearing `echo` must `^(`/`^)`-escape, or cmd's nested-block parser
  crashes with `was unexpected at this time`. `echo(` (no space) is the safe
  blank-line idiom and is exempt.
- Static guards live in `tests/test_win_python_resolver.py`; the relocated
  gate assertions are in `tests/test_launcher_health_check_loop.py`.

**macOS note:** `setup_macos.sh::pick_python` already resolves any installed
3.11/3.12 across Homebrew + system paths, so macOS does NOT need this resolver.
No macOS auto-install was added — Homebrew installs are interactive/slow and
the SSD bundle ships a prebuilt venv. macOS is not regressed; it simply has a
different (already-working) resolution path.

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

### 7. After editing ANY tracked text file, verify the diff is your change ONLY (autocrlf trap)

**This box has an autocrlf-like behavior: many tracked files are `i/lf` in git but the `Write`/`Edit` tools (and the working tree) are CRLF.** Editing such a file with `Edit`/`Write` and committing flips the **entire file** LF→CRLF in the committed blob — a silent regression that ships broken/garbled files and wastes a full cycle reverting. This already happened once (`release_prep.py`, PR #22) and must not recur.

**MANDATORY pre-commit check after every `Edit`/`Write` on a tracked file:**

```bash
git diff --stat <file>          # insertions/deletions must ≈ your logical change
git diff <file> | grep -c '^[+-]' # not hundreds-of-lines for a 1-line edit
```

If `--stat` shows the whole file changed (e.g. "288 insertions, 251 deletions" for a 5-line edit), the line endings flipped. **Before committing**, restore the file's committed eol:

```bash
git show HEAD:<file> > /tmp/orig && \
python -c "import sys;p=sys.argv[1];b=open(p,'rb').read();open(p,'wb').write(b.replace(b'\r\n',b'\n'))" <file> && \
git add <file> && git ls-files --eol <file>   # confirm i/lf restored
```

Authoritative eol checks (working-tree `\r` scans give false positives here): `git ls-files --eol <file>` (both columns) and `git show HEAD:<file> | tr -cd '\r' | wc -c` (committed blob CR bytes — 0 = LF). **A file's committed eol must match its siblings** (e.g. `distribution/*.py` are all `i/lf` — keep them LF). This rule is general (not launcher-only): it applies to `.py`, `.md`, `.json`, `.txt`, anything tracked.

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
