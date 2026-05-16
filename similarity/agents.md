# AI Agent Instructions (agents.md)

If you are an AI assistant interacting with this repository, adhere to the following rules:

1. **Architecture Rule**: The system is split strictly into `engine.py` (business logic and ML math), `gui.py` (CustomTkinter graphical interface), and `cli.py` (Rich-powered terminal interface). Keep concerns separated. Do not put ML logic in the UI files.
2. **Library Constraint**: The ML backend explicitly requires `retina-face` (not `retinaface`). Do not break this in `requirements.txt` as it will cause unresolvable TensorFlow 2.5.0 dependency crashes on Python 3.12.
3. **Execution Rule**: `DeepFace.verify()` and `DeepFace.build_model()` are exceptionally slow on the first run. Any function calling them from `gui.py` MUST be executed inside a background daemon thread to prevent the UI from freezing.
4. **Design Rules**: The GUI must remain dark-mode focused. The CLI must utilize the `rich` library for all terminal output (using `Console`, `Panel`, `Progress`, `Table`, etc.) rather than standard `print()` statements.

Always test dependency installations in isolated virtual environments.

---

## Subproject Cross-Platform Rules (PR #21 case studies)

This `similarity/` directory ships its own launchers (`run_gui.{command,bat}`, `run_cli.{command,bat}`), its own `main.py` entry point, and runs as a **standalone** app in addition to being driven by the main Kling UI. That dual-mode life exposes a set of macOS/Windows traps that have repeatedly broken the standalone path when contributors edit on Windows. **Read these before touching anything under `similarity/`.**

The root-level `AGENTS.md` and `CLAUDE.md` have the project-wide rules (macOS Portability Rules 1–10, Hard Rules for Windows Launchers, Similarity Stack Wiring). The rules below are the **subproject-specific** ones — they would not exist if `similarity/` were not standalone-runnable.

### 5. Subproject launcher resolver MUST version-validate every venv candidate

`similarity/run_gui.command`, `run_cli.command`, and `run_gui.bat` each contain their own `resolve_python()` / `:check_py` chain — completely separate from the main GUI's `setup_macos.sh`. These resolvers MUST verify each candidate venv's Python version is in 3.9–3.12 *before* returning it; a bare `[ -x ]` test is not enough.

Why this rule exists: PR #21 commit `afe0540b`. The user had a stale `$REPO_ROOT/.venv/` symlinked to python3.14 (an unrelated experiment). The resolver short-circuited on the first executable it found → returned a python3.14 interpreter → the post-resolve gate aborted with a misleading "Unsupported Python version" error. Even though python3.11 and `similarity/.venv/` (correct) were present, neither was tried.

Canonical patterns (already shipped in this PR — do not regress):

```bash
# similarity/run_gui.command line 38
_python_supported() {
  "$1" -c 'import sys; raise SystemExit(0 if (3, 9) <= sys.version_info[:2] < (3, 13) else 2)' >/dev/null 2>&1
}

# Then gate every candidate:
if [ -x "$REPO_ROOT/.venv311/bin/python" ] && _python_supported "$REPO_ROOT/.venv311/bin/python"; then ...
```

```bat
:: similarity/run_gui.bat line 140 — :check_py subroutine (avoids nested-paren delayed-expansion hell)
:check_py
if not exist %1 exit /b 1
if /i "%~3"=="permissive" (
  %1 -V >nul 2>&1
) else (
  %1 -c "import sys; raise SystemExit(0 if ((3,9) <= sys.version_info[:2] < (3,13)) else 2)" >nul 2>&1
)
if errorlevel 1 exit /b 1
set "PYTHON_BIN=%~1"
set "ENV_KIND=%~2"
exit /b 0
```

Also: **`$REPO_ROOT/.venv311/` is the canonical macOS venv name** per the root `CLAUDE.md` Rule 6 (Homebrew python3.12+ ships without `_tkinter`). It MUST be a tried candidate.

Static-text regression guard: `tests/test_similarity_launcher_resolver.py`.

### 6. `similarity/main.py` MUST bootstrap `sys.path` for the standalone app

`similarity/main.py` imports modules from the **repo root** (`tk_dialogs`, `similarity_engine`, `face_similarity`, etc.). When the launcher runs `<python> main.py` with `cwd=similarity/`, `sys.path[0]` is `similarity/` — those root-level modules are not visible.

The bootstrap at the top of `similarity/main.py` (lines 5–14, shipped in `afe0540b`):

```python
import sys
from pathlib import Path

_REPO_ROOT = str(Path(__file__).resolve().parent.parent)
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)
```

Without this, the standalone GUI dies at import time with:
```text
Failed to load GUI components. Ensure all dependencies are installed: No module named 'tk_dialogs'
```

Do NOT remove this bootstrap. Do NOT replace it with `os.path.*` calls (CodeRabbit will flag, repo convention is `pathlib.Path`). Any future subproject that imports root-level shared modules needs the same pattern.

### 7. v1.9 polynomial curve calibration (do not drift)

The similarity score the user sees is shaped by `similarity_engine._score_from_distance` — a polynomial that maps ArcFace cosine distance `0.0 → 100%`, `0.68 → 80%`. The exponent is **`PASS_CURVE_EXPONENT = 0.5`** as of PR #21 commit `089d631b`. Reference table in the source comment at `similarity_engine.py:625-640`. Full reasoning in `similarity/CLAUDE.md` "Key Mathematical Decision".

Pre-v1.9 used exponent 2.5 which compressed the typical AI-edit distance band (0.05–0.20) into 99–100% — pegged readings that looked indistinguishable from a degenerate fallback. v1.9's 0.5 spreads that band across 95–89%.

**Do not** change the exponent without recalibration data; **do not** replace the polynomial with `(1 - distance) * 100` (the parent `similarity/CLAUDE.md` explicitly forbids this).

### 8. POSIX redirects in `.bat` files are silent killers

cmd.exe has no `/dev/null`. PR #21 commit `cb876b44` shipped 14 `/dev/null` redirects in `similarity/run_gui.bat` (I wrote them via macOS muscle memory). CodeRabbit caught it before the user pulled to Windows — without that catch, every `mkdir`, `python -V`, `python -c "import ..."`, dep-check, and stamp delete would have silently errored on Windows with "system cannot find the path specified" buried in stderr.

```bat
rem WRONG — POSIX redirect, silently fails on Windows
"!PYTHON_BIN!" -c "import tkinter" >/dev/null 2>&1

rem CORRECT — cmd-native null device
"!PYTHON_BIN!" -c "import tkinter" >nul 2>&1
```

Pre-push: `rg -n --iglob '*.bat' '/dev/null'` MUST return zero. Also avoid `&>` (POSIX), backticks, `$(...)`, `[[ ... ]]`, `/usr/`/`/bin/`/`/tmp/` paths — none work in cmd.

### 9. Name override env vars in launcher error messages

`similarity/run_gui.command`, `run_cli.command`, and `run_gui.bat` honor two env-var overrides: `SELFIEGEN_PYTHON` (explicit interpreter) and `SELFIEGEN_VENV_DIR` (explicit venv root). When the post-resolve gate rejects a python version, the error MUST name the override env var so the user knows how to unset it:

```bash
if [ -n "${SELFIEGEN_PYTHON:-}" ] || [ -n "${SELFIEGEN_VENV_DIR:-}" ]; then
  echo "[ERROR] Your SELFIEGEN_PYTHON / SELFIEGEN_VENV_DIR override points at Python ${PY_ACTUAL}, but Similarity requires 3.9-3.12. Unset the override or point at python3.11."
else
  echo "[ERROR] Resolved Python is ${PY_ACTUAL}, outside supported range 3.9-3.12 (resolver bug — file an issue)."
fi
```

Why: PR #21 commit `cb876b44`. CodeRabbit caught the Windows launcher only naming `SELFIEGEN_PYTHON` even though the resolver also accepts `SELFIEGEN_VENV_DIR`.

### 10. `.command` and `.sh` siblings MUST share `set` flags

Anywhere a `.command` and `.sh` form a sibling pair (e.g., the macOS launcher chain in `launchers/macos/run_gui.command` ↔ `run_gui.sh`), they MUST use **identical** `set` flags. Current standard is `set -euo pipefail`. Mismatches silently change error handling.

Why: PR #21 commits `e7e2cad4` and `300c88f0`. CodeRabbit caught `.command` files using `set -uo pipefail` (no `-e`) while the `.sh` siblings used `set -euo pipefail`.

The explicit `set +e / set -e` toggle around a sub-script invocation is fine and intentional — it scopes errexit OFF for that specific call only.

### Pre-push checklist for changes under `similarity/`

Run before pushing any change in this directory:

```bash
# Portability gate (CRLF in .sh/.command + exec bit on .command/.sh)
bash scripts/check_macos_portability.sh

# Static-text guards for launcher resolvers
.venv311/bin/python -m pytest tests/test_similarity_launcher_resolver.py -q

# Selfie similarity log format guard (when touching selfie_generator.py or face_similarity.py)
.venv311/bin/python -m pytest tests/test_selfie_generator_similarity_log.py -q

# Subproject's own tests
.venv311/bin/python -m pytest similarity/tests/ -q

# Windows-hygiene grep (when touching any .bat under similarity/)
rg -n --iglob 'similarity/*.bat' '/dev/null'   # MUST return empty
```

If you touched a launcher file, also do a live smoke: `SIMILARITY_LAUNCHED_BY_MAIN=1 bash similarity/run_gui.command` and confirm the standalone GUI launches (you can `pkill` after ~5s alive).

### Verified-safe (do not "fix" these)

A future contributor working on Windows may be tempted to "fix" these — don't. They're already correct, and conflating them with the subproject launcher pattern will regress something.

- **Root `run_gui.command`, `run_cli.command`, `run_gui.bat`, `run_cli.bat`** are thin pass-throughs (`exec` / `call` to `launchers/run_*`). They do not resolve Python; no resolver bug to fix there.
- **Main GUI launcher chain** (`launchers/macos/run_gui.command` → `run_gui.sh` → `setup_macos.sh`) uses its own robust `pick_python()` + `is_supported_python()` + `has_tk_support()` at `setup_macos.sh:11-125` — different file, different pattern, both work fine. Don't conflate it with the similarity subproject's `resolve_python()`. The main GUI uses `.venv-macos`; the similarity subproject uses `.venv311` or `.venv` or `similarity/.venv`.

If you genuinely need to change either of the above, see the root `CLAUDE.md` Rule 8 (macOS launcher chain) and run the full chain test (`bash run_gui.command`) before pushing.
