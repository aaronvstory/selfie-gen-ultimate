# macOS Portability — MANDATORY (Windows agents read this first)

> **Relocated from `CLAUDE.md` (2026-05-29) to reduce always-loaded context.**
> These rules are still binding. Read this file BEFORE any change that touches
> shell scripts, launchers, file dialogs, path handling, or the macOS venv.

This repo runs on **both Windows and macOS**. Most contributors edit on Windows, and CI is Windows-leaning. Several macOS-runtime issues recur — agents working on this codebase MUST guard against them on every change that touches shell scripts, launchers, file dialogs, or path handling.

## 1. Line endings — `.sh` and `.command` must be LF

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

## 2. Executable bit — `.command` and `.sh` must be `100755` in git

`.command` files cannot be double-clicked from Finder unless they have the exec bit. Git stores mode independently of the working-tree perm; a file can be `chmod +x` locally but still committed as `100644`. Both must be `100755`.

```bash
# Verify
git ls-files --stage <file>        # leading number must be 100755

# Fix both working tree and index:
chmod +x <file>
git update-index --chmod=+x <file>
```

## 3. File dialogs — never use raw `tkinter.filedialog`

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

**Standalone subprojects (e.g., `similarity/`) MUST bootstrap `sys.path` before importing `tk_dialogs`.** `tk_dialogs.py` lives at the repo root. A subproject launched with `cwd=similarity/` has only `similarity/` on `sys.path[0]`; `from tk_dialogs import select_open_file` raises `ModuleNotFoundError` at import time. Fix at the top of the subproject entry point:

```python
# similarity/main.py (fixed in commit afe0540b)
import sys
from pathlib import Path

_REPO_ROOT = str(Path(__file__).resolve().parent.parent)
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)
```

Without this, the standalone Similarity GUI crashes with `Failed to load GUI components. Ensure all dependencies are installed: No module named 'tk_dialogs'` even when the launcher resolves Python correctly. Same trap applies to any future subproject that imports root-level `similarity_engine`, `face_similarity`, `path_utils`, etc.

## 4. Path-separator assertions are platform-bound

`os.path.join("F:\\foo", "bar.bat")` returns `"F:\\foo/bar.bat"` on POSIX (forward slash) but `"F:\\foo\\bar.bat"` on Windows. Tests that assert on the result of any path-join with backslash inputs are intrinsically Windows-only:

```python
@pytest.mark.skipif(os.name != "nt", reason="asserts win32 backslash joins")
def test_windows_launcher_uses_comspec_then_fallback(): ...
```

## 5. Test module-mock gotchas (sys.modules caching)

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

## 6. macOS Python — use `python3.11`, not `python3.12`+

Homebrew's `python3.12` and `python3.13` ship without `_tkinter`. Tests that import `tkinter` (transitively, anything touching the GUI or `tk_dialogs`) will fail to collect on those interpreters. Use `python3.11`:

```bash
python3.11 -m venv .venv311
.venv311/bin/python -m pytest tests/ similarity/tests/ -q   # use python -m pytest, not pytest directly,
                                                            # so the project root is on sys.path
```

## 7. The macOS launcher chain (don't break links silently)

```text
run_gui.command (root)
  → launchers/run_gui.command         (compatibility wrapper)
    → launchers/macos/run_gui.command (logs + dep-chmod + invokes run_gui.sh)
      → run_gui.sh                    (calls setup_macos.sh, then runs gui_launcher.py)
        → setup_macos.sh              (creates .venv-macos and installs requirements.txt)
```

If you touch any link in that chain: chain-test it via `bash run_gui.command` once before pushing. Same chain exists for `run_cli.command` and the eight `run_oldcam_v*.command` variants.

## 8. Pre-push macOS portability check

Run before pushing any change that touches `*.sh`, `*.command`, `tk_dialogs.py`, or anything under `launchers/`, `similarity/src/`, or `kling_gui/main_window.py` picker code:

```bash
bash scripts/check_macos_portability.sh
```

Exits non-zero on CRLF in shell scripts, or `.command`/`.sh` files committed without the exec bit. Source: `scripts/check_macos_portability.sh`.

**The portability gate does NOT catch:** Python resolver bugs (rule 9), set-flag parity mismatches (rule 10), `/dev/null` in `.bat` files, or `sys.path` import bugs in subprojects. Those are caught only by code review + the static-text test `tests/test_similarity_launcher_resolver.py`.

## 9. Launcher Python resolvers MUST version-validate every venv candidate

`.command` and `.bat` launchers that resolve a Python interpreter via a chain of venv candidates (e.g., `$REPO_ROOT/venv`, `$REPO_ROOT/.venv`, `$REPO_ROOT/.venv311`, `.venv` local fallback) MUST verify the candidate's version is in the supported range *before* returning it. Without this, a stale `.venv` symlinked to an unsupported Python (3.13, 3.14) is accepted by `[ -x ]`, then the post-resolve gate aborts the launcher with a confusing "Unsupported Python version" error — even though supported pythons are installed.

This is the **exact bug PR #21 fixed in commit `afe0540b`** for the standalone Similarity launcher. The same defect existed on Windows and was fixed in the same commit.

**Canonical pattern (macOS):**

```bash
# Single source of truth for the version expression
_python_supported() {
  "$1" -c 'import sys; raise SystemExit(0 if (3, 9) <= sys.version_info[:2] < (3, 13) else 2)' >/dev/null 2>&1
}

resolve_python() {
  if [ -n "$REPO_ROOT" ] && [ -x "$REPO_ROOT/.venv311/bin/python" ] && _python_supported "$REPO_ROOT/.venv311/bin/python"; then
    echo "$REPO_ROOT/.venv311/bin/python|shared root .venv311"; return 0
  fi
  # ... gate every subsequent candidate with _python_supported ...
  # Auto-create path also validates `pybin` BEFORE `python -m venv`
}
```

**Canonical pattern (Windows):** `:check_py` subroutine at end of `.bat`, called per-candidate (avoids nested-paren delayed-expansion landmines). Reference implementation: `similarity/run_gui.bat:140-151`.

**Rules:**
- `.venv311/` is the canonical macOS venv name. **It MUST be a tried candidate** ahead of `.venv/` (per rule 6).
- macOS fallback chain MUST be `python3.11 || python3.12 || python3 || python` (python3.11-first per rule 6).
- The post-resolve gate stays as defense-in-depth; split its error message to distinguish "your SELFIEGEN_PYTHON override points at unsupported python" from "resolver bug".
- New launcher resolvers MUST be covered by `tests/test_similarity_launcher_resolver.py` (static-text regex assertions, no subprocess).

## 10. `.command` and `.sh` siblings MUST use identical `set` flags

Sibling launcher files in `launchers/macos/` and the project root MUST share the same `set` flags. The current standard is `set -euo pipefail`. Mismatches (e.g., `.command` with `set -uo pipefail` but `.sh` with `set -euo pipefail`) silently change error handling between launch paths.

CodeRabbit caught this on `launchers/macos/run_gui.command` in PR #21. Initial shebang fix (`e7e2cad4`) only handled half the parity; the full fix landed in `300c88f0`.

The explicit `set +e / set -e` toggle around sub-script invocations is fine — it still scopes errexit OFF for that one call:

```bash
#!/usr/bin/env bash
set -euo pipefail   # ← top-level: full strict mode

# ... setup ...

set +e
"${ROOT_DIR}/run_gui.sh"   # ← errexit scoped OFF for this one call
status=$?
set -e                     # ← restore strict mode
```

When you add a new sibling pair, set both to `set -euo pipefail` from the start.

## 11. macOS hit-target sizing — route tight `ttk.Button` styles through `mac_padding`

On macOS, raw `tk.Button` was migrated to `ttk.Button` under clam in PR #40 /
commit `b3bc7398` to fix the HIView tint-reversion bug. That also widened the
hit area for buttons with comfortable padding `(10, 6)` / `(14, 7)`. But
several tight styles (SLOT `(6, 3)`, COMPACT `(8, 4)`, SUCCESS/DANGER_COMPACT
`(7, 4)`, CarouselRef `(8, 4)`) still missed clicks 2-10× before registering.

The fix: every tight ttk button style declares padding via
`mac_padding((default), (macos))` in `kling_gui/theme.py`. Windows + Linux
get the original tuple unchanged; macOS gets a bumped tuple. Raw
`tk.Checkbutton` / `tk.Radiobutton` / `tk.Menubutton` widgets in high-use
areas spread `**macos_widget_pad()` into their constructor — a no-op on
non-macOS, a `padx=6 pady=3` bump on macOS.

When you add a new button style or raw tk widget:
- New `ttk.Button` style with padding tighter than `(10, 6)` → wrap with
  `mac_padding`. The static test
  `tests/test_main_window_styles.py::test_no_hardcoded_tight_padding`
  catches re-introduced `(6, 3)` / `(7, 4)` literals.
- New raw `tk.Checkbutton` / `tk.Radiobutton` / `tk.Menubutton` in a
  high-use Step 0 / Step 2 row → spread `**macos_widget_pad()` into the
  constructor. The helper lives in `kling_gui/theme.py`.

For diagnosing a future "missed clicks on macOS" report:
```bash
KLING_DEBUG_CLICKS=1 bash run_gui.command
```
then transiently wire `attach_click_diagnostics(self._suspect_btn, "label")`
in the relevant tab. Logs press/release coords + widget bounds at WARNING
level. Remove the wiring before commit — the helper is opt-in for a reason.

The `mac_padding` / `macos_widget_pad` / `CLICK_DEBUG` contract is covered
by `tests/test_theme_mac_padding.py`.
