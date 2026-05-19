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
friend's mature tool in the gitignored `rPPG/` directory, invoked as an
external launcher. It is **off by default everywhere** — opt-in only.

## Pipeline order (locked)

```
Kling  ->  Loop  ->  Oldcam  ->  rPPG       (rPPG strictly LAST)
```

Why last: Loop's ping-pong reverse would play a pre-injected pulse backwards
(non-physiological, detectable); oldcam v24's resolution-crush would
attenuate a pre-injected sub-perceptual pulse. Injecting on the final
delivered pixels preserves the correct pulse.

- Only rPPG, no oldcam → `Kling -> [Loop] -> rPPG`
- Neither → behaviour unchanged

## Verified injector contract (DO NOT trust the README's path claim)

Invocation used everywhere:

```
rPPG/run_rppg.bat "<abs in.mp4>" --inject --output "<abs out.mp4>" --skip-kinematic-gate
```

| Aspect | Reality (verified via `oldcam-testing/rppg_harness.py`) |
|--------|----------------------------------------------------------|
| Exit code | `0` success, `1` error, `2` py-version. Gate on this + output presence, never the injector's own "Test Result: PASS/FAIL" (its heuristic, not Persona). |
| `--output` | **Not honoured deterministically.** The injector writes `{stem}-rppg{ext}` then *renames* it to `{stem}-rppg - <snr>-<phase>-<temporal>-<motion>-<harmonic>{ext}`. |
| Output resolution | Use `automation.rppg.resolve_produced_output()` — the single source of truth (GUI queue + pipeline + harness all call it). It ranks the exact `--output` path **and** the metric-renamed siblings matching the **precise** `{stem} - *{ext}` form (space-hyphen-space, `glob.escape`d so metacharacter stems like `selfie[final]` work) **together by mtime, newest wins** — so a stale prior `*-rppg.mp4` never shadows a freshly-produced rename on a re-run. |
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
| Config defaults | `automation/config.py` `AUTOMATION_DEFAULTS` | `automation_rppg_enabled` (False), `automation_rppg_mode` ("inject"), `automation_rppg_required` (False) |
| Automation module | `automation/rppg.py` | `run_rppg`, `build_rppg_output_path`, `resolve_produced_output`, `resolve_rppg_launcher`, `is_rppg_artifact`, `stream_subprocess_with_timeout` (the last two are the safety-critical double-injection guard + hard-timeout streamer; mirrors `automation/oldcam.py`) |
| Pipeline Step 8 | `automation/pipeline.py` | After oldcam, before `_finalize_case`; input = oldcam output else video_generate output; mirrors facetrack manifest reporting |
| GUI queue | `kling_gui/queue_manager.py` | `_rppg_enabled`, `_build_rppg_output_path`, `_resolve_rppg_launcher`, `_rppg_video`; inserted in main queue order (after oldcam) + the oldcam re-run path |
| GUI checkbox | `kling_gui/config_panel.py` | Orange row (`#3A2A1F` bg / `#7D5E3A` border) below the violet oldcam frame; `rppg_var`; `_on_rppg_changed`; config load; `var_attrs` cleanup |
| CLI | `kling_automation_ui.py` | Recommended-defaults block, interactive `_ask_bool` wizard, questionary `_qs_section_oldcam`; `RECOMMENDED_DEFAULTS_VERSION` bumped to 2 |
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

- Re-enable / evaluate the v8 kinematic preflight gate (currently always
  `--skip-kinematic-gate`).
- Iterative/tuned mode (`--inject --iterative`); `automation_rppg_mode`
  already reserves `"iterative"`. Add a second GUI checkbox mirroring the
  oldcam multi-checkbox pattern.
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
