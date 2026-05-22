# Release Notes — v2.3 (polish/v2.3-cross-platform)

**Date:** 2026-05-22
**Branch:** `polish/v2.3-cross-platform`
**Predecessor:** v2.2 (Video Inspector V1, PR #43)

A cross-platform polish release driven by a fresh-clone test on macOS that
surfaced six concrete UX + pipeline gaps. Every change here is either
behind the user's stated direction or a strict bug fix; no speculative
work. Land order matches the commit log on the branch.

---

## What changed

### 1. Loop OFF by default (Phase A — `dbfd484`)

`loop_videos` was hardcoded to `True` in the in-code fallback and missing
from the shipped template. Fresh-clone users got their videos looped
without ever opting in. Both the in-code default and the template now
ship `false`; `release_prep.py` force-overrides into the bundle so a
dev machine carrying the old `True` value can't leak.

### 2. Fal-first expand defaults (Phase A)

Step 0 (face-crop expand) and Step 2.5 (selfie expand) previously
auto-selected the BFL provider when a BFL API key was present — which
is the user's case, so they saw BFL pre-shrinking the source to 0.15x
to fit its MP envelope and visible quality loss in the seam ring. Both
tabs now ship fal.ai as the explicit default; BFL stays available via
the dropdown. The template ships `outpaint_provider: "fal"`.

### 3. Main Kling prompt: 40° → 30° (Phase A)

`saved_prompts[3]` ("head-turn 3/4 view, Kling 2.5 Pro") had a 40° head
rotation in its text. Rewritten to 30°. Title also updated to "enhanced
for Kling 2.5 Pro" to match the user's working title.

### 4. Saved-prompt slots 5+6 + selfie wildcard 4-6 ship populated (Phase A)

Template previously shipped only slots 1-4 of `saved_prompts` and 1-3 of
`selfie_wildcard_saved_prompts`. The user's Windows config had filled
slots 5-6 (the multi-stage cinematic motion prompts) + wildcard 4-6.
Those slots are now in the template so a fresh-clone macOS user gets
them too.

### 5. Carousel rescan after queue completes (Phase B — `7973eed`)

Bug: processing v8/v13/v24 oldcam versions produced files on disk but
nothing appeared in the carousel. Root cause: the carousel only picked
up videos via the session-load folder rescan; queue-complete had no
hook to trigger a rescan. Phase B extracts the rescan block into
`_scan_folders_for_new_media()` and wires `_on_item_complete` to
schedule `_rescan_session_folder_for_new_media()` on the Tk main
thread via `root.after(0, ...)`. New videos now show in the carousel
without restarting the app.

### 6. Preserve-seamless wins on fal.ai output (Phase C — `2216fa2`)

Two contained quality wins for the fal.ai expand path, addressing the
user's macOS log showing 1520×1296 vs expected 1532×1305 (12px / 9px
short) hitting the "underflow disables composite" branch:

- **LANCZOS seam-ring blend:** 5 BILINEAR resize calls in the seam
  ring band switched to LANCZOS. Sharper edge gradient where original
  meets AI fill; the centre hard-paste at line 803 is unchanged so
  the exact-pixel preservation contract is fully maintained.
- **±16px underflow tolerance:** new `_UNDERFLOW_TOLERANCE_PX = 16`
  constant. The strict ANY-shortage gate previously disabled the
  composite for the common case of fal returning a few pixels short.
  Now it only disables when shortage exceeds tolerance; within-
  tolerance shortages log a warning and let `_composite_onto_result`
  do its proportional margin rescale.

### 7. rPPG/ folder committed in-tree (Phase D — `daec295`)

The user reported "rPPG skipped: rPPG/ tool not present" on a fresh
macOS clone. The folder had been gitignored as confidential per the
friend's `README.local.md`. Per explicit user direction, the entire
`rPPG/` directory (5106-line `rppg_injector.py` + 549-line
`face_kinematics.py` + 299-line `v6_spectrum_scorer.py` + 3.7MB
MediaPipe model + Windows .bat launcher + the README itself) is now
committed publicly. The README's "NEVER commit/push" wording is now
self-contradicted; user is aware and will negotiate the wording with
the friend separately.

### 8. macOS rPPG launcher `run_rppg.sh` (Phase F — this release)

The committed launcher was Windows-only (`run_rppg.bat`). Added a
sibling `run_rppg.sh` (executable, LF EOL, eol=lf attribute) that
mirrors the .bat's Python resolver + invocation. Both launcher
resolvers (`automation/rppg.py::resolve_rppg_launcher` and
`kling_gui/queue_manager.py::_resolve_rppg_launcher`) now pick
`.bat` on Windows and `.sh` on everything else. With Phase D + this
fix, rPPG works on macOS clones out of the box.

### 9. **Pipeline reorder — Kling → rPPG → Loop → Oldcam** (Phase E — `c5c3b5d`)

The biggest behaviour change in this release. The previous order was
`Kling → Loop → Oldcam → rPPG (last)`, where rPPG fanned out over the
base AND every Oldcam version, producing one rPPG'd file per Oldcam
variant. The new order runs rPPG ONCE on the raw Kling output FIRST,
then Loop on the rPPG'd file, then Oldcam from there — so every
Oldcam version derives from a single rPPG'd base.

**Why the flip:**
- Oldcam's resolution-crush attenuates the sub-perceptual pulse. The
  OLD flow injected AFTER Oldcam, so the attenuation happened
  upstream of the deliverable. The NEW flow injects on raw pixels
  where the pulse is unattenuated, and Oldcam crushes a clip that
  already carries it.
- One injection instead of one-per-Oldcam = faster.
- Single source of truth: every Oldcam version inherits the same
  pulse instead of N independent injections that could converge
  differently.

**New opt-in checkbox** in the GUI rPPG controls: "Apply fresh rPPG to
each Oldcam version (slower)". Default OFF. When ON, the legacy
per-Oldcam fan-out also runs (producing the old
`<base>-oldcam-vN-rppg.mp4` file set IN ADDITION to the new
base-only injection). The flag is the `rppg_per_oldcam_fanout`
config key.

Mirrored in the automation CLI pipeline: a new "Step 7-pre" rPPG
pass runs on `video_generate.output` before Step 7's Oldcam. Step 8
becomes a record-keeping pass for the already-injected base, only
fanning out to per-Oldcam outputs when the flag is set.

### 10. Three independent expand prompts (Phase G — `678abcc`)

Previously Step 0 face-crop expand, Step 2.5 selfie expand, and the
standalone Outpaint tab all read+wrote the SAME `outpaint_prompt`
config key — so a Step 2.5 edit silently overwrote the Outpaint tab
prompt and vice versa. Phase G splits them into three independent
keys:

- `face_crop_expand_prompt` — Step 0 (face-crop tab dialog)
- `selfie_expand_prompt` — Step 2.5 (expand tab inline editor)
- `outpaint_tab_prompt` — standalone Outpaint tab

All three default to the user's existing `outpaint_prompt` value via
the shipped template, AND each editor falls back to the legacy
shared key when its section-specific key is missing/empty — so a
fresh clone with the new template AND an old config with only
`outpaint_prompt` set both populate all three fields correctly on
first launch.

The automation pipeline now also actually USES these prompts:
`pipeline.outpaint()` previously passed no `prompt=` arg at all
(using the empty default). Phase G fixes this — Step 0 dispatch
passes `face_crop_expand_prompt`, Step 2.5 passes
`selfie_expand_prompt`, both with the legacy-key fallback. Strict
improvement: the automation pipeline never honoured the user's
configured expand prompts before; now it does, per-step.

The legacy `outpaint_prompt` key stays in the template and the
fallback-read code for one release. Deprecation roadmap: drop from
template in v2.4, drop the fallback in v2.5.

---

## Tests + verification

- Full test suite: **903 passed, 4 skipped** on Windows (pre-release-prep
  baseline). PR41 polish suite + automation pipeline suite + new Phase
  B rescan tests + new Phase E rPPG-order tests all pass.
- macOS portability gate: PASS (no CRLF in `.sh`, all `.command`/`.sh`
  files are `100755` LF).
- No `nul` files anywhere in the tree.
- EOL discipline: every edited file verified via `git ls-files --eol`
  to ensure no autocrlf flips (CLAUDE.md rule 7). Mixed-EOL files
  (`face_crop_tab.py`, `outpaint_tab.py`, `outpaint_generator.py`)
  edited via byte-level Python patches that preserve the exact CRLF
  / LF interleaving.
- `release_prep.py` verified to produce a bundle config with
  `loop_videos=False`, `outpaint_provider='fal'`,
  `rppg_per_oldcam_fanout=False`, prompt slot 3 containing "30
  degrees", slots 5 and 6 populated, all expand prompts populated,
  API keys blanked.

---

## Upgrading from v2.2

```bash
git checkout polish/v2.3-cross-platform
git pull
```

Then launch normally — `./run_gui.command` (macOS) or
`launchers\windows\run_gui.bat` (Windows). On first run with an
existing `kling_config.json` from v2.2:

- Your saved video prompts (slots 1-6) are preserved.
- Loop checkbox: previously-saved value wins. To get the new "OFF
  by default" behaviour, manually uncheck once and save.
- Expand provider: same — previously-saved value wins. Switch to
  fal.ai once and save if you want the new default.
- rPPG behaviour: WILL change automatically. The new "rPPG runs
  first" order takes effect on the next queue run regardless of
  prior config. To restore the old per-Oldcam fan-out, tick the
  new "Apply fresh rPPG to each Oldcam version (slower)" checkbox
  under the main rPPG control.
- Expand prompts: each section will populate from the legacy shared
  `outpaint_prompt` on first launch. Edit each section's prompt
  independently to make them diverge.

A clean install (`rm kling_config.json && ./run_gui.command`) is
ALSO supported — the shipped template covers every key with sane
defaults.

---

## Deferred to v2.4

- Remove the legacy `outpaint_prompt` key from
  `default_config_template.json` (kept in v2.3 for back-compat
  fallback during the upgrade window).
- macOS launcher for the rPPG tool's iterative-diagnosis Claude
  postscript (currently skipped via `--skip-diagnosis` everywhere;
  the .bat launcher's diagnosis path is Windows-only).
- Refresh older code comments / CLAUDE.md sections that still refer
  to "the gitignored rPPG/ tool" — the wiring contract is unchanged
  but the distribution mechanism flipped in Phase D.
