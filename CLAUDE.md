# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

Kling UI is an AI media generation toolkit using fal.ai and BFL APIs. It provides a 5-tab Tkinter GUI for face cropping, portrait analysis, selfie generation, image outpainting, and batch video generation, plus a CLI mode via Rich.

## macOS Portability — MANDATORY (Windows agents read this first)

This repo runs on **both Windows and macOS**. Most contributors edit on Windows, and CI is Windows-leaning. Several macOS-runtime issues recur — agents working on this codebase MUST guard against them on every change that touches shell scripts, launchers, file dialogs, or path handling.

### 1. Line endings — `.sh` and `.command` must be LF

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

### 2. Executable bit — `.command` and `.sh` must be `100755` in git

`.command` files cannot be double-clicked from Finder unless they have the exec bit. Git stores mode independently of the working-tree perm; a file can be `chmod +x` locally but still committed as `100644`. Both must be `100755`.

```bash
# Verify
git ls-files --stage <file>        # leading number must be 100755

# Fix both working tree and index:
chmod +x <file>
git update-index --chmod=+x <file>
```

### 3. File dialogs — never use raw `tkinter.filedialog`

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

### 4. Path-separator assertions are platform-bound

`os.path.join("F:\\foo", "bar.bat")` returns `"F:\\foo/bar.bat"` on POSIX (forward slash) but `"F:\\foo\\bar.bat"` on Windows. Tests that assert on the result of any path-join with backslash inputs are intrinsically Windows-only:

```python
@pytest.mark.skipif(os.name != "nt", reason="asserts win32 backslash joins")
def test_windows_launcher_uses_comspec_then_fallback(): ...
```

### 5. Test module-mock gotchas (sys.modules caching)

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

### 6. macOS Python — use `python3.11`, not `python3.12`+

Homebrew's `python3.12` and `python3.13` ship without `_tkinter`. Tests that import `tkinter` (transitively, anything touching the GUI or `tk_dialogs`) will fail to collect on those interpreters. Use `python3.11`:

```bash
python3.11 -m venv .venv311
.venv311/bin/python -m pytest tests/ similarity/tests/ -q   # use python -m pytest, not pytest directly,
                                                            # so the project root is on sys.path
```

### 7. The macOS launcher chain (don't break links silently)

```text
run_gui.command (root)
  → launchers/run_gui.command         (compatibility wrapper)
    → launchers/macos/run_gui.command (logs + dep-chmod + invokes run_gui.sh)
      → run_gui.sh                    (calls setup_macos.sh, then runs gui_launcher.py)
        → setup_macos.sh              (creates .venv-macos and installs requirements.txt)
```

If you touch any link in that chain: chain-test it via `bash run_gui.command` once before pushing. Same chain exists for `run_cli.command` and the eight `run_oldcam_v*.command` variants.

### 8. Pre-push macOS portability check

Run before pushing any change that touches `*.sh`, `*.command`, `tk_dialogs.py`, or anything under `launchers/`, `similarity/src/`, or `kling_gui/main_window.py` picker code:

```bash
bash scripts/check_macos_portability.sh
```

Exits non-zero on CRLF in shell scripts, or `.command`/`.sh` files committed without the exec bit. Source: `scripts/check_macos_portability.sh`.

**The portability gate does NOT catch:** Python resolver bugs (rule 9), set-flag parity mismatches (rule 10), `/dev/null` in `.bat` files, or `sys.path` import bugs in subprojects. Those are caught only by code review + the static-text test `tests/test_similarity_launcher_resolver.py`.

### 9. Launcher Python resolvers MUST version-validate every venv candidate

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

### 10. `.command` and `.sh` siblings MUST use identical `set` flags

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

`front_expand -> extract_portrait -> selfie_generate -> similarity_gate -> selfie_expand -> video_generate -> oldcam -> rppg`

(`rppg` is the final, opt-in post-process — runs strictly LAST, after
`oldcam`; off by default. See "rPPG Injection Wiring" below.)

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

## Default Workflow After Feature Work (MANDATORY)

User directive (2026-05-21): "i want u to always push commit into pr on
branch... never work on main, then run code reviewer ur own while bots
work, then when done implementing subagent findings, should be enough
time for checking bot comments". This is the **default loop** whenever
feature/fix work reaches a runnable state — don't skip steps unless the
user explicitly says "skip review" or "just push".

### 1. Never work on `main`

All work happens on a feature branch tied to a PR. If `main` is checked
out, create or switch to the right branch BEFORE editing. Use
`git rev-parse --abbrev-ref HEAD` to confirm. If unsure which branch to
use, ask the user rather than guessing.

### 2. Commit + push to the PR branch when work reaches a runnable state

"Runnable state" = tests pass + portability gate green + the change
doesn't leave the GUI/CLI in a broken intermediate. **Push every such
commit to the remote PR branch** — the user works on multiple machines
(macOS + Windows) and pulls from the remote, so unpushed local work is
invisible. Commit-message contract:

* Type prefix: `feat:` / `fix:` / `perf:` / `chore:` / `refactor:` / `docs:`
* One-line subject explaining the user-visible outcome
* Body explains the WHY + cites the finding/bot/issue source if any
* Last line: `Co-Authored-By: Claude Opus ... <noreply@anthropic.com>`

Pre-commit invariants — fail-fast if any violated:

- `bash scripts/check_macos_portability.sh` exits 0
- `.venv311/bin/python -m pytest tests/ similarity/tests/ -q` (on macOS)
  or equivalent on Windows passes
- EOL invariants preserved on every edited file
  (`git ls-files --eol` left column == right column, CR counts match
  HEAD on CRLF files)
- No `nul` file in tree (`rm -f nul`)

### 3. Trigger bot reviews + spawn code-reviewer subagent in PARALLEL

Right after `git push`, do BOTH of these in the same turn (parallel
tool calls, not sequential):

**a) Trigger PR bots on the PR.** Single comment listing all active
   bot mentions:

   ```text
   Branch head: <sha> — <one-line summary>. Bot pass please.

   @coderabbitai review
   @codex review
   @gemini-code-assist review
   ```

   Skip `@sourcery-ai` until/unless it starts responding again — it's
   been silent across multiple rounds on PR #43.

**b) Spawn the code-reviewer subagent on the ENTIRE branch diff.** Use
   the `general-purpose` agent type with a prompt that:
   * Points it at the FULL branch diff (`git diff main...HEAD`), NOT
     the latest commit range. User directive 2026-05-21: "i think the
     subagent codereview should better be ran on the entire branch
     diff, not just the commit individuals." Reasoning: the subagent's
     unique value over the bots is cross-cutting analysis — it catches
     bugs in code that LOOKED safe at commit-time but interacts badly
     with adjacent code added in earlier commits on the same branch.
     Bots already do the per-commit review well; the subagent should
     do what bots are bad at.
   * Names the project context (macOS + Windows Tk app + the
     wiring-doc cross-references for similarity / oldcam / rPPG).
   * Asks for severity-tagged findings (CRITICAL/HIGH/MEDIUM/LOW).
   * Caps the response length (1500-2000 words).
   * Includes a list of findings ALREADY ADDRESSED in earlier commits
     on the same branch with their commit SHAs — tells the subagent
     NOT to re-flag them. Without this list, the subagent re-discovers
     the same bugs every round and the report becomes noise.
   * If branch is large (>1500 LOC of net diff) and would exceed the
     subagent's effective review window, focus on the high-traffic
     files (touched in 3+ commits) and the new files. Explicitly say
     so in the prompt — don't silently sample.

The subagent typically returns in 4-7 minutes; bots typically respond
in 5-30 minutes. Running them in parallel cuts wall-clock by ~40%.

### 4. Address subagent findings first

The subagent returns before the bots usually do. Triage its findings
using a SINGLE rubric (the prior draft had two conflicting thresholds —
fixed here per the code-review M5 finding on 4ddb0252):

- **CRITICAL / HIGH**: fix in this round, write a regression test if
  the bug was non-obvious, commit + push to the PR branch.
- **MEDIUM**: fix in this round UNLESS the work is genuinely large
  (subjectively: ">2 hours of focused work" or "requires an API
  change in code the user explicitly asked not to touch"). Otherwise
  do it now. Do NOT preemptively defer mediums as "V2 work" — the
  user has called this out as a lazy pattern. The shipping cost of
  carrying a known medium into the next round is always higher than
  the cost of fixing it now.
- **LOW**: defer to a cleanup pass at PR-close.

### 5. Check bot comments

By the time subagent fixes are pushed, bots should have responded.
Pull the new comments using whichever snippet matches the current
shell. Both forms produce identical filtered output; the difference
is purely quoting (M6 code-review on 4ddb0252 — the prior single
bash snippet was unusable from the L3 Windows machine's PowerShell):

**bash / zsh / git-bash** (macOS, WSL, git-bash on Windows):
```bash
SINCE="<timestamp-of-trigger>"
gh api "repos/<owner>/<repo>/pulls/<n>/comments?per_page=50" \
  --paginate --jq '.[] | select(.created_at > "'"$SINCE"'" \
  and .user.login != "<your-gh-username>")'
```

**PowerShell** (native Windows shell):
```powershell
$SINCE = "<timestamp-of-trigger>"
$Q = '.[] | select(.created_at > "' + $SINCE + '" and .user.login != "<your-gh-username>")'
gh api "repos/<owner>/<repo>/pulls/<n>/comments?per_page=50" `
  --paginate --jq $Q
```

Per-bot disposition (per PR #43 retro, evidence-driven):

- **Codex** (chatgpt-codex-connector): highest signal-to-noise. P1/P2
  badges. Catches semantic contract violations and cross-cutting bugs.
  Address every P1 same round; P2 same round if tractable.
- **Gemini** (gemini-code-assist): broadest coverage. HIGH/Medium/Low.
  Catches widget patterns, perf nits, threading. Address HIGH same
  round; triage mediums per the V2 rule above.
- **CodeRabbit** (coderabbitai): thorough but noisy. Major/Minor +
  inline lint rules. Address Major same round; batch Minors at PR-close.
  Skip its "Analysis chain" issue-level comments — those are
  verification scripts CR ran, not findings to address.
- **Sourcery**: currently silent across PR #43; skip the trigger.

### 6. Address bot findings + reply inline

For each finding:

1. Fix the code if Step 4's triage rules say "this round."
2. Reply inline on the comment with `gh api -X POST .../comments/<id>/replies`
   pointing at the fix commit SHA.
3. If declining/deferring, post a real rationale (NOT "V2 work").
4. Commit + push the fix batch as ONE commit with a message that
   itemizes all the addressed findings.

### 7. Post a round wrap-up comment

After pushing the fix batch, post a PR comment summarizing the round:
a table of "finding → fix commit / disposition", final test count,
portability gate result, branch head SHA. This is the user's entry
point when they pull the branch on another machine.

### 8. Loop if findings keep landing

If the bot pass produces findings that warrant a new fix commit, the
next push triggers a fresh bot round. Repeat from step 3. Two rounds
is normal; three rounds is acceptable; four rounds usually means a
CRITICAL was missed in the original implementation and we should
pause to do a wider audit before continuing.

### Skip conditions (don't run the full loop)

- The user explicitly says "skip review" / "just push" / "WIP"
- The commit is purely a typo / docs fix
- The branch is mid-experiment and not ready for review (push without
  trigger; resume the loop when work stabilizes)
- The user says "don't engage the bots yet"

### Why this is committed to CLAUDE.md (not just a local memory)

The user works on multiple machines and pulls from the remote branch.
Local-only memories live in `~/.claude/projects/...` on a single
machine; CLAUDE.md ships with the repo so the same workflow runs on
Windows after `git pull`. If you find yourself wanting to add a
workflow rule that should apply on both OSes, put it HERE, not in a
local memory.

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

---

## Oldcam Version Wiring

Full wiring checklist with per-layer tables: [`docs/oldcam-wiring.md`](docs/oldcam-wiring.md).

**Current default version:** v24 (Crush Laundromat; superseded v15). The skipped
app-version range v16-v23 are bench experiments in `oldcam-testing/` evaluated
against the resemble-score metric — which turned out NOT to be the metric Persona
actually uses downstream, so the experiments are paused-pending-re-evaluation,
not rejected. v14 / v15 / v24 do **not** use mediapipe; v9 / v10 / v11 do.

**Auto-discovered (no changes needed):** `_discover_oldcam_versions()` in
`queue_manager.py` scans `oldcam-v*` dirs; output filename suffix is generic;
`automation/pipeline.py` is fully version-agnostic.

---

## rPPG Injection Wiring

Full wiring tables + harness usage: [`docs/rppg-wiring.md`](docs/rppg-wiring.md).

**Pipeline order (NON-NEGOTIABLE):** `Kling → Loop → Oldcam → rPPG`. rPPG runs
**last** so it sees the oldcam-distorted frames as its source, otherwise the
pulse signal gets washed by the downstream stages. **Off by default everywhere
— opt-in only.** Invokes the gitignored `rPPG/` external tool; degrades
gracefully (skip + log, never crash) when the tool is missing or fails.

**Injector contract gotcha you WILL hit:** `--output` is NOT honoured
deterministically — the injector renames its output to
`{stem}-rppg - <metrics>{ext}`. Always resolve the real file via
`automation.rppg.resolve_produced_output()` — single source of truth used by
both the GUI queue and the test harness.

**Validation rig:** `oldcam-testing/rppg_harness.py` + `run_rppg_harness.bat`
run the real injector against a permanent gitignored fixture and emit an
anti-siren `REPORT.md`. Use it before pushing any rPPG/oldcam change. See
the wiring doc for the three invocation modes (direct / chain / --skip-run).

---

## Similarity Stack Wiring (NON-NEGOTIABLE — full surface coverage)

Full per-action tables (adding a dep, adding a GUI control, adding a config
key, adding a launcher, pre-flight checklist):
[`docs/similarity-wiring.md`](docs/similarity-wiring.md).

**The feature spans TEN distinct surfaces:** main GUI carousel, automation CLI
pipeline, standalone subproject (own GUI + own CLI), Windows + macOS launchers
(per surface), PyInstaller frozen build, dist release zip, and tests. Touching
it without updating ALL applicable surfaces ships a broken release. Use the
wiring doc's per-action checklist before commit.

**Engine layer (single source of truth — DO NOT duplicate):**

| Concern | File |
|---------|------|
| Engine class + scoring math | `similarity_engine.py` (root) |
| Standalone shim | `similarity/src/engine.py` re-exports `from similarity_engine import FaceEngine` |
| App-facing adapter (singleton + config overrides) | `face_similarity.py` (root) |
| Pipeline import | `from face_similarity import compute_face_similarity_details` in `automation/pipeline.py` |
| Main GUI import | `from face_similarity import compute_face_similarity_details` in `kling_gui/carousel_widget.py` |
| Standalone GUI/CLI import | `from src.engine import FaceEngine` in `similarity/src/{gui,cli}.py` |

**Default config keys (current):** `automation_similarity_threshold` (80),
`automation_similarity_use_ensemble` (true), `automation_similarity_secondary_model`
("Facenet512"), `automation_similarity_anti_spoofing` (true),
`automation_similarity_require_fas_pass` (false).

---

## Supply Chain Audit

Two project-level scanners run on every commit that touches a dep manifest,
workflow, `.pth`, or `.claude/*` file. Both must exit clean. They cover
different gaps; don't pick one.

| Scanner | Strengths | File |
|---|---|---|
| Project scanner | GitHub exfil-repo discovery (`--github`), C2 domain check, Dune-name repo patterns, project-specific IoC tables | `scripts/detect_compromise.py` |
| Hulud-kit scanner (v1.1) | **`.claude/` persistence detection** (TeamPCP attack vector against Claude Code), spoofed git author check, campaign string markers, SARIF 2.1.0 output | `scripts/hulud_kit_scan.py` |

Both wired into `scripts/git-hooks/pre-commit` and run automatically when a
matched file is staged. Bypass only with `git commit --no-verify` after a
manual review.

### Manual invocations

```bash
# Project scanner (GitHub exfil + project IoCs):
python scripts/detect_compromise.py --repo-root .
python scripts/detect_compromise.py --all          # adds --venv + --github

# Hulud-kit scanner (.claude/ persistence + 9 checks):
python scripts/hulud_kit_scan.py --root .
python scripts/hulud_kit_scan.py --sarif results.sarif   # for GitHub Security tab

# Machine-wide audit (~30s, queries OSV.dev live):
/hulud-kit quick                                   # via Claude Code slash command
~/.shai-hulud/shai-hulud-audit.sh --mode quick     # direct invocation
```

### When adding a dependency

Audit the full `requirements-hashed.txt` (falling back to `requirements.txt`)
in a disposable venv before merging any dep change. The script takes no
positional args — it always re-installs the project's pinned set into a
fresh venv and runs `pip-audit`:

```bash
./scripts/sandbox_install.sh         # macOS / Linux
.\scripts\sandbox_install.bat        # Windows
```

After any dep change, regenerate the hash-pinned lockfile. Run this on
**Windows**, not macOS — the current `requirements-hashed.txt` is pinned
to Intel/Windows TensorFlow wheels and macOS pip-compile output overwrites
the Windows wheel set, breaking the bundled release:

```bash
pip install pip-tools
pip-compile --generate-hashes --output-file=requirements-hashed.txt requirements.txt
```

### When an audit fails

Read findings carefully — `.claude/execution.js` or `.claude/setup.mjs` alerts
from the kit scanner are TeamPCP persistence payloads, treat as confirmed
compromise. Then follow `docs/security/IOC_DETECTION_CHECKLIST.md` for the
incident-response runbook (credential rotation order, what to check next).

### Updating the kit scanner

The kit scanner is sourced from <https://github.com/aaronvstory/shai-hulud-kit>.
To pull a newer version: `cp /path/to/shai-hulud-kit/scripts/detect_compromise.py
scripts/hulud_kit_scan.py`, then re-add `"hulud_kit_scan.py"` to the
`SCANNER_FILENAMES` set in `check_campaign_markers()` so the scanner doesn't
flag itself.
