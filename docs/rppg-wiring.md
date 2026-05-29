# rPPG Injection Wiring Reference

> How the rPPG post-process step is wired into every layer, why it runs
> last, and the verified injector contract. Companion to
> [`oldcam-wiring.md`](oldcam-wiring.md).

---

## What it is

rPPG injection installs a physiologically-correct, **sub-perceptual** pulse
into the final video so Persona's passive rPPG liveness stage sees a real
signal instead of "weak/deformed rPPG". Extensive analysis
(`oldcam_reference_bundle/`) showed oldcam *version* is not the Persona
pass/fail lever; rPPG is the genuinely-untried forward direction.

This is **not** the crude removed v10/v11 "siren" pulse (which visibly
strobed the face and was removed at oldcam v12). The injector is the
friend's mature tool in the `rPPG/` directory, invoked as an external
launcher. It is **off by default everywhere** — opt-in only.

> **Versioning note (2026-05-22, Phase D of polish/v2.3):** `rPPG/`
> was previously gitignored as confidential. Per user direction, the
> folder is now committed in-tree so a fresh clone on any OS gets
> the tool without a side-channel install. Older docs / code
> comments that refer to "the gitignored rPPG/ tool" are out of
> date — the wiring contract (this document) is unchanged; only the
> distribution mechanism flipped.

## Pipeline order (Phase E of polish/v2.3, 2026-05-22 — rPPG FIRST)

```text
Kling  ->  rPPG  ->  Loop  ->  Oldcam       (rPPG runs FIRST)
```

The order flipped on 2026-05-22 per user direction. The previous "rPPG
strictly LAST" rule produced one rPPG'd file per Oldcam version
(`<base>-rppg`, `<base>-oldcam-v8-rppg`, `<base>-oldcam-v24-rppg`,
etc.) — useful when each Oldcam variant was treated as an independent
deliverable. The new flow runs rPPG ONCE on the raw Kling output and
every downstream step builds on that single injection:

```text
existing.mp4
  -> existing-rppg.mp4              (rPPG injected, becomes the base)
  -> existing-rppg_looped.mp4       (Loop, only if loop_videos: true)
  -> existing-rppg[_looped]-oldcam-v8.mp4    (Oldcam derives from rPPG'd base)
  -> existing-rppg[_looped]-oldcam-v24.mp4
```

### Why the flip

Oldcam's resolution-crush attenuates the sub-perceptual pulse — but
the OLD flow injected the pulse AFTER Oldcam, so the attenuation
happened upstream of the deliverable and the pulse landed on the
final pixels with some loss. The NEW flow injects on the raw Kling
output where the pulse is unattenuated by downstream encoding; Oldcam
then crushes a clip that ALREADY carries the pulse. Cost: a pre-Oldcam
inject is one injection cycle (vs. one per Oldcam version in the old
flow), so it's faster too.

### Legacy per-Oldcam fan-out (opt-in)

The old "rPPG on every Oldcam output" behaviour stays available behind
a GUI checkbox + the `rppg_per_oldcam_fanout` config key (default OFF).
When enabled, the trailing per-Oldcam injection ALSO runs, producing
the legacy file set IN ADDITION to the new base-only injection. Slower
but useful for careful comparison workflows. The GUI shows it as
"Apply fresh rPPG to each Oldcam version (slower)" under the main
"Inject rPPG pulse" checkbox.

### Standalone modes

- Only rPPG, no Oldcam (default flow) → `Kling -> rPPG -> [Loop]`
- Neither → behaviour unchanged from prior releases

## Verified injector contract (DO NOT trust the README's path claim)

Invocation used everywhere (PR #43 — flipped from one-shot to iterative
to match the friend's `rPPG/rppg.bat`):

```bat
rPPG/run_rppg.bat "<abs in.mp4>" --inject --output "<abs out.mp4>" \
    --iterative --iterate-from-baseline --skip-diagnosis \
    --skip-kinematic-gate
```

Iterative mode is now the production default. The friend who wrote the
injector confirmed it is **mandatory** for production: the initial
single-shot injection rarely lands at the optimal strength, and the
iterative PID re-injects with adjusted settings until score converges.
`--iterate-from-baseline` ensures each iteration re-injects from the
ORIGINAL input (no cumulative encoding loss across iters).
`--skip-diagnosis` bypasses the post-iter Claude-API diagnosis (which
costs `ANTHROPIC_API_KEY` calls). All three are user-overridable via
config keys; default-ON to mirror the canonical `.bat`.

| Aspect | Reality (verified via `oldcam-testing/rppg_harness.py`) |
|--------|----------------------------------------------------------|
| Exit code | `0` success, `1` error, `2` py-version. Gate on this + output presence, never the injector's own "Test Result: PASS/FAIL" (its heuristic, not Persona). |
| `--output` | **Not honoured deterministically.** The injector writes `{stem}-rppg{ext}` then *renames* it to `{stem}-rppg - <snr>-<phase>-<temporal>-<motion>-<harmonic>{ext}`. |
| Output resolution | Use `automation.rppg.resolve_produced_output()` — the single source of truth (GUI queue + pipeline + harness all call it). It ranks the exact `--output` path **and** the metric-renamed siblings matching the **precise** `{stem} - *{ext}` form (space-hyphen-space, `glob.escape`d so metacharacter stems like `selfie[final]` work) **together by mtime, newest wins** — so a stale prior `*-rppg.mp4` never shadows a freshly-produced rename on a re-run. |
| Metric-suffix toggle | `rppg_metrics_in_filename` (GUI) / `automation_rppg_metrics_in_filename` (CLI), default **off**. `automation.rppg.finalize_rppg_output()` (single source of truth, GUI queue + pipeline both call it): off → rename the metric file back to clean `{stem}-rppg{ext}` + write the 5 metrics to a `{stem}-rppg.metrics.json` sidecar; on → keep the injector name. `parse_metric_suffix` is negative-phase-safe (splits on `-`; an empty `--` token marks the next value negative). Never raises — a rename hiccup keeps the delivered video. |
| Double-injection guard | `automation.rppg.is_rppg_artifact()` — refuses to re-inject an already-injected file (matches the `-rppg` token as suffix, metric-rename, OR `-rppg-` infix from rPPG-before-oldcam; case/path/prefix-safe). Used by **both** pipeline Step 8 and GUI `_rppg_video` (the guard runs *before* launcher resolution so it works even without the gitignored tool). An already-injected input IS the final deliverable → returned as-is, injector not spawned. |
| Hard timeout | `automation.rppg.stream_subprocess_with_timeout()` — shared reader-thread + wall-clock streamer (GUI + pipeline). A silently-hung injector (no newline, no EOF) is still killed on the 600s deadline → graceful skip; a bare `readline()` loop could not enforce this. |
| `--skip-kinematic-gate` | Always passed. The v8 kinematic preflight is README-marked "new, untested". **Re-enabling it is a deliberate future enhancement**, tracked here — not an oversight. |
| Strength | Default `--strength 0.005` is correct and sub-perceptual. Harness measured green-channel p2p delta = **0.26 levels** (threshold < 2.0), std unchanged → no siren. Do NOT raise amplitude. |
| Platform | Windows `.bat` only. `automation/rppg.py` is platform-agnostic and skips gracefully on hosts without the launcher. A macOS launcher is out of scope. |
| Failure | Any non-zero exit / no resolvable output → graceful skip: keep the pre-rPPG video, log a warning, never crash the queue/run. |

## Wiring surfaces (all required)

| Layer | File | What |
|-------|------|------|
| Manifest step registry | `automation/manifest.py` `STEP_NAMES` | `"rppg"` after `"oldcam"` (else `update_step` raises `Unknown step: rppg`). **Backward-compat:** `create_or_load`'s fingerprint check tolerates an additive `automation_*` key absent from a pre-rPPG manifest **only when the requested value equals the default** — so existing users' resume/run isn't broken by the new defaults, but an explicit opt-in (`automation_rppg_enabled=true`) on an old corpus forces a reprocess. `case_is_complete_and_valid` prefers the `rppg` step output when that step completed (so a deleted rPPG deliverable isn't masked as complete). |
| Config defaults | `automation/config.py` `AUTOMATION_DEFAULTS` | `automation_rppg_enabled` (False), `automation_rppg_mode` ("iterative" — PR #43, was "inject"), `automation_rppg_iterate_from_baseline` (True), `automation_rppg_skip_diagnosis` (True), `automation_rppg_skip_kinematic_gate` (True), `automation_rppg_landmark_stride` (1 — reverted from 3 in PR #53 fix/step0-composite-and-rppg-v2.5 after PR #52's snapshot race produced unplayable output on a real user run; the snapshot race itself is now fixed via tmp-validate-atomic-replace in `rPPG/rppg_injector.py::_snapshot_validates` and a shared ffprobe playability gate in `automation/rppg.py::_is_playable_video`, but the default stays at the slow-but-correct 1 until we have local proof that stride 3 is safe on a real Kling output. Power users opt back into the 3-5x speedup via `automation_rppg_landmark_stride: 3` or the GUI alias `rppg_landmark_stride: 3`), `automation_rppg_required` (False), `automation_rppg_metrics_in_filename` (False). `automation_recommended_defaults_version` bumped from 1 to 2 when iterative became default. |
| Automation module | `automation/rppg.py` | `run_rppg`, `build_rppg_output_path`, `resolve_produced_output`, `resolve_rppg_launcher`, `is_rppg_artifact`, `stream_subprocess_with_timeout` (the last two are the safety-critical double-injection guard + hard-timeout streamer; mirrors `automation/oldcam.py`) |
| Pipeline Step 8 | `automation/pipeline.py` | After oldcam, before `_finalize_case`. **Fan-out (no "primary"):** Step 7 calls `run_oldcam_all` and stashes every per-version path in the oldcam step `meta["all_outputs"]`; Step 8 injects rPPG into the BASE (`video_generate` output — automation has no loop step) AND every oldcam output, dropping already-injected candidates. Honors `keep_metrics`; records headline + `meta["all_outputs"]`. Mirrors the GUI queue main + re-run paths (base = looped clip there). Plain `-oldcam-vN` kept. |
| GUI queue | `kling_gui/queue_manager.py` | `_rppg_enabled`, `_build_rppg_output_path`, `_resolve_rppg_launcher`, `_rppg_video`; inserted in main queue order (after oldcam) + the oldcam re-run path |
| GUI checkbox | `kling_gui/config_panel.py` | Orange row (`#3A2A1F` bg / `#7D5E3A` border) below the violet oldcam frame; `rppg_var`; `_on_rppg_changed`; config load; `var_attrs` cleanup |
| CLI | `kling_automation_ui.py` | Recommended-defaults block, interactive `_ask_bool` wizard, questionary `_qs_section_oldcam`; `RECOMMENDED_DEFAULTS_VERSION` bumped to 3 |
| Tests | `tests/test_automation_cli_smoke.py`, `tests/test_automation_pipeline.py`, `tests/test_oldcam_versions.py` | merge-defaults keys, pipeline gate (skip/run/graceful-skip/required-fail), resolver rename test, GUI static-source |

## Permanent local test harness (Windows box)

`oldcam-testing/rppg_harness.py` + `oldcam-testing/run_rppg_harness.bat` are
the durable validation rig. They run the **real** injector on a permanent
gitignored Kling fixture and quantify siren vs sub-perceptual.

```bat
rem direct rPPG on the fixture
oldcam-testing\run_rppg_harness.bat

rem full Loop -> Oldcam(v24) -> rPPG chain via the automation modules
oldcam-testing\run_rppg_harness.bat --chain

rem re-analyse the last produced file only
oldcam-testing\run_rppg_harness.bat --skip-run
```

Fixture: `oldcam-testing/front_crop_nano-banana-2-edit_sim87_001_k25tStd_p4_1.mp4`
(real Kling video, ~20 MB, gitignored, **keep on disk permanently**). Outputs
land in the gitignored `oldcam-testing/rppg_harness_out/` with a `REPORT.md`.

Anti-siren verdict thresholds (face-box green channel p2p delta vs original):
SUB-PERCEPTUAL < 2.0, BORDERLINE < 5.0, else SIREN. Tune only in
`rppg_harness.py` and record the change here.

First real run (2026-05-19, direct mode): **SUB-PERCEPTUAL** — delta 0.26
levels, SNR 7.72→13.08 dB, phase 75.5°→7.8°. Default strength is correct.

## Future enhancements (explicitly deferred)

- Re-enable / evaluate the v8 kinematic preflight gate (currently
  `automation_rppg_skip_kinematic_gate=True`).
- ~~Iterative/tuned mode~~ — **landed in PR #43.** `automation_rppg_mode`
  defaults to `"iterative"`; companion flags `automation_rppg_iterate_from_baseline`,
  `automation_rppg_skip_diagnosis`, `automation_rppg_skip_kinematic_gate`
  all default ON. Argv shape is locked by `tests/test_automation_rppg_cmd.py`.
  GUI checkbox-row expansion (multi-checkbox like oldcam) is still
  deferred — V1 of iterative just flips the default; per-mode UI
  surfacing arrives with the next config-panel refresh.
- macOS injector launcher.
- rPPG-only re-run via a shared post-process re-run button (the current
  oldcam ↻/📂 already also apply rPPG when both are checked).

## Step-3 layout (canonical)

The Step-3 post-process area is a single horizontal band: an
`Options:` label, then a vertical stack of the violet **Oldcam** frame
(top) over the orange **rPPG** frame (below) -- both in the same parent
with identical pack options so they render **equal width** -- then ONE
shared **Re-Run** column (the rotate / open-folder buttons) to the
right of both frames. `Loop Video (ping-pong)` lives on the "Allow
reprocessing" row, inline after "Increment (_2, _3...)".

The single shared Re-Run pair drives `_on_oldcam_rerun_clicked` /
`_on_oldcam_pick_rerun_clicked` -> `queue_manager.rerun_oldcam_only`,
which already applies whatever is selected (any Oldcam versions AND/OR
rPPG) and re-loops first when Loop Video is on -- pure UI relocation,
zero re-run logic change. The earlier env-gated `SELFIEGEN_STEP3_LAYOUT`
preview and its `run_gui_step3_v2.bat` launchers were removed once this
became the canonical layout.
