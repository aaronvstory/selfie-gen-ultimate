# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

Kling UI is an AI media generation toolkit using fal.ai and BFL APIs. It provides a 5-tab Tkinter GUI for face cropping, portrait analysis, selfie generation, image outpainting, and batch video generation, plus a CLI mode via Rich.

## macOS Portability — MANDATORY (Windows agents read this first)

This repo runs on **both Windows and macOS**. Most contributors edit on
Windows, and CI is Windows-leaning. Several macOS-runtime issues recur — the
full set of 11 binding rules (LF line endings, exec bit, `tk_dialogs` usage,
path-separator asserts, sys.modules mock gotchas, `python3.11` requirement,
the launcher chain, the portability gate, venv-resolver version-validation,
`set`-flag parity, and `mac_padding` hit-targets) lives in:

> **➡️ [`docs/macos-portability.md`](docs/macos-portability.md) — READ BEFORE
> editing any `.sh`/`.command`, launcher, file dialog, or path-handling code.**

This is not optional reading: each of those rules has caused a real macOS
breakage. Open that file whenever your change touches shell scripts, launchers,
`tk_dialogs.py`, `similarity/src/`, or `kling_gui/main_window.py` picker code.

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

`front_expand -> extract_portrait -> selfie_generate -> similarity_gate -> selfie_expand -> video_generate -> rppg -> oldcam`

(`rppg` is opt-in and runs immediately after `video_generate` per
the Phase E ordering — same as the GUI queue. The legacy "rPPG
strictly LAST, after oldcam" arrangement is preserved behind the
`automation_rppg_per_oldcam_fanout` opt-in flag (default OFF) for
the rare case where a fresh-pulse oldcam variant is needed. See
"rPPG Injection Wiring" below.)

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

   Include `@sourcery-ai` — it was silent across PR #43 (hence the prior
   "skip" advice) but resumed responding with actionable findings on PR #49
   round 1. Drop the skip until it goes silent on a fresh PR.

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
- **Sourcery**: was silent across PR #43; resumed on PR #49. Concise,
  contract-level findings (docstring drift, no-raise guarantees, etc.).
  Address Major same round; defer LOW.

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

### 9. After merge: refresh the SSD + rebuild distributable (macOS only)

This step is macOS-only — the SSD bootstrap setup at
`/Volumes/st7Private/code/selfie-gen-ultimate/` doesn't exist on the
Windows machine. **On Windows, skip step 9 entirely**; the merge itself
is the end of the loop.

On macOS: once the PR squash-merges to `main`, immediately do the
post-merge refresh: `git pull` on the SSD source repo, refresh the
`_user_state/app_support/` snapshot from the live Application Support
dir, build a fresh `dist/SelfieGenUltimate-{vX.Y}.zip` and drop it on
the SSD root. Full playbook + verification commands in
[`docs/ssd-and-distributables.md`](docs/ssd-and-distributables.md).

Skip the SSD refresh when `/Volumes/st7Private/` isn't mounted; in
that case, explicitly tell the user the SSD copy is now stale instead
of silently ignoring it. Only rebuild the SSD's `venv-macos.tar` if
the merged PR touched `requirements.txt` or `requirements-hashed.txt`.

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

## Hard Rules — Windows Launchers + GUI Sash Layout (NON-NEGOTIABLE)

Violating these has caused repeated launch breakage and garbled commits. The
full rule set — `.bat`/`.cmd` CRLF + PowerShell-write requirement, `echo(` vs
`echo.`, log-append redirect order, the `root → launchers → launchers/windows`
chain, the dep-skip stamp, MediaPipe `--no-deps`, the autocrlf diff-verify
trap, and all the GUI sash-layout clamp/restore rules — lives in:

> **➡️ [`docs/windows-launcher-and-sash-rules.md`](docs/windows-launcher-and-sash-rules.md)
> — READ BEFORE editing any `.bat`/`.cmd`, the launcher chain, or sash-layout
> code in `kling_gui/layout_utils.py` / `main_window.py`.**

Two rules are quoted here because they bite on almost every commit:
- **Never use `Write`/`Edit` on `.bat`/`.cmd`** — they emit LF and garble batch
  files. Write via PowerShell `WriteAllText` with explicit `` `r`n ``.
- **After every `Edit`/`Write` on a tracked file, run `git diff --stat <file>`
  before committing.** A whole-file change for a small edit = an EOL flip;
  restore the committed EOL before staging (autocrlf trap, see the doc).

---

## Concurrent launches & workspaces (PR #49)

The GUI is safe to launch multiple times concurrently. Each process gets an
isolated runtime directory keyed by `<YYYYMMDD-HHMMSS>-<PID>`, so carousel
state, video history, and crash logs from one window never bleed into another.
Named workspaces (`--workspace shoot-a` / `KLING_WORKSPACE=shoot-a`) give
fully isolated state trees.

**Design rule for new on-disk state (the part that bites):** before adding any
file the GUI writes at runtime, classify it as **Shared** (cross-instance,
last-writer-wins — use `path_utils.get_user_data_dir()`) or **Per-instance**
(use `path_utils.get_runtime_dir()`). Don't introduce new shared writable files
without documenting them — that's exactly the bug PR #49 fixed.

The full state-classification table, workspace-name rules, per-launch env vars,
and the `.launcher_state/setup.lock` bootstrap-mutex details live in:

> **➡️ [`docs/concurrent-launches-workspaces.md`](docs/concurrent-launches-workspaces.md)
> — READ BEFORE adding GUI-written runtime files or touching workspace/runtime
> logic.**

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

**Pipeline order (Phase E, 2026-05-22):** `Kling → rPPG → Loop → Oldcam`.
rPPG injects on the raw Kling frames so the per-iter PID stabilizes against
clean (un-resolution-crushed) source. Loop's ping-pong reverse remains
physiological because the sub-perceptual amplitude stays well below the
visibility threshold, and Oldcam's chain preserves the injected pulse.
The slower legacy "rPPG strictly LAST" (one rPPG'd file per Oldcam
version) is preserved behind the `rppg_per_oldcam_fanout` opt-in flag
— default OFF. **rPPG itself is OFF by default everywhere — opt-in only.**
Invokes the `rPPG/` tool; degrades gracefully (skip + log, never crash)
when the tool is missing or fails. On graceful skip the GUI queue
inserts a `-NORPPG` marker into the filename so the final delivered
video unambiguously reflects that rPPG was REQUESTED but did NOT land.

**Injector contract gotcha you WILL hit:** `--output` is NOT honoured
deterministically — the injector renames its output to
`{stem}-rppg - <metrics>{ext}`. Always resolve the real file via
`automation.rppg.resolve_produced_output()` — single source of truth used by
both the GUI queue and the test harness.

**Injector contract gotcha #2 (PR `fix/rppg-failure-visibility`):** the
iterative loop's "best so far" temp file (`temp_iteration_N.mp4`) can be
pruned mid-loop, causing the final `shutil.copy2` at
`rPPG/rppg_injector.py:4562` to crash with `FileNotFoundError`. The
defensive fix in that PR snapshots the current best to a stable name
(`best_iteration_snapshot.mp4` in the injector cwd) at every
`_is_new_best` transition and cleans it up at end-of-run. Don't remove
the snapshot logic — root cause of the mid-loop deletion is still
unknown.

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

## macOS ↔ Windows bounce traps (NON-NEGOTIABLE pre-PR check matrix)

The user works on **both macOS (primary dev) and Windows (verification + use)**.
Bugs only one OS can trigger have caused multiple cross-OS bounces
(PR #49 → #50 → #51 → #55). A 7-trap pre-PR check matrix — dist build bloat
(gitignored ≠ excluded), launcher arg-forward/EOL/exec-bit asymmetry,
Windows file-handle contention, path-separator asserts, the `>nul`→`/dev/null`
linter substitution, OS-junk files (`.DS_Store`/`Thumbs.db`), and the cmd
nested-block parens crash — lives in:

> **➡️ [`docs/cross-os-bounce-traps.md`](docs/cross-os-bounce-traps.md) — RUN
> the applicable traps BEFORE opening any PR. Don't ship-and-bounce.**

If a check requires the other OS and you can't run it, say so explicitly in the
PR description. The agent-private version is the memory file
`feedback_macos_windows_bounce_traps.md`.

---

## SSD + Distributables Workflow

Full playbook: [`docs/ssd-and-distributables.md`](docs/ssd-and-distributables.md).

A portable copy of the project lives on an external SSD at
`/Volumes/st7Private/code/selfie-gen-ultimate/` for plug-and-play
launching on virgin Macs (one-click `START.command` bootstraps Python,
seeds Application Support, extracts a pre-built venv, then launches the
GUI). The SSD repo has `_user_state/` (gitignored locally + in
`.git/info/exclude` on the SSD repo) carrying `venv-macos.tar`, an
`app_support/` snapshot, and the install scripts.

**On every merge to main, if the SSD is mounted, also refresh it.**

1. `python distribution/build_release.py` → `dist/SelfieGenUltimate-{vX.Y}.zip` + alias.
2. `cd /Volumes/st7Private/code/selfie-gen-ultimate && git pull origin main`.
3. `rsync` the live `~/Library/Application Support/selfie-gen-ultimate/` →
   `_user_state/app_support/` (keeps API keys + prompts + model cache in sync).
4. `cp dist/SelfieGenUltimate-*.zip` to SSD root.
5. Rebuild `_user_state/venv-macos.tar` ONLY if `requirements.txt` or
   `requirements-hashed.txt` changed in the merged PR. Otherwise leave it.

Check `ls /Volumes/st7Private 2>/dev/null && echo MOUNTED` first. If
unmounted, tell the user "I'd refresh the SSD but it's not mounted, your
copy is now N commits behind" — don't silently skip.

Detection of bundle-bloat regressions: a healthy bundle is ~10 MB. If
`build_release.py` produces hundreds of MB, a venv or build artifact has
escaped `EXCLUDED_DIRS` in `distribution/release_prep.py` — fix that
list before shipping.

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
