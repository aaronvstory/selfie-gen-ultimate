# V13 / v1.7 Release Hand-off

> **Status 2026-05-14:** ✅ V13 / v1.7 shipped. PR #17 (v1.6) merged to main at 808ba08; V13 work landed on a fresh `feat/oldcam-v13` branch in a new PR. This hand-off is now **historical** — kept for reference. Live spec is in `docs/oldcam-versions.md` (V13 section) and `docs/oldcam-wiring.md` (updated comparison table).
>
> **Purpose (original):** Drop-in context for a fresh Claude Code session resuming the v1.7 release work.
> **Authored:** 2026-05-14 (light prep session — no code changes, only this doc)
> **Original location intent:** `.claude/v13-handoff.md` — but `.claude/` is gitignored, so this lives in `docs/` instead.

---

## 1. PR / Branch Snapshot (as of this hand-off)

- **Branch:** `feat/oldcam-v11` — head commit `f154773` (CRF 12 bump)
- **PR:** [#17](https://github.com/aaronvstory/selfie-gen-ultimate/pull/17) — *Release v1.6: Oldcam V11 (signal ordering) + V12 (pristine hardware-only, default)*
- **Merge state:** `MERGEABLE` + `mergeStateStatus: CLEAN`
- **CI:** CodeRabbit ✅ SUCCESS · Kilo Code Review ✅ SUCCESS · Sourcery skipping
- **Fresh bot comments since `f154773`:** zero (bots' last review was May 13 16:58 — already addressed in `f2dc62d` + `f154773`)
- **Local working tree:** clean (nul-file sweep already done)
- **Open task:** add Oldcam V13 + bump release to v1.7, land in same PR, then re-trigger bots for a final review before merge

---

## 2. What V13 ("High-End Daylight") Does

### Removed (vs V12)

- `apply_modern_sensor_noise` — no FPN, no temporal noise, no grain (this is the big perf win)
- `apply_ae_stepping` — no auto-exposure walk; V13 assumes stable daylight
- Ghosting blend forced to `0.0` regardless of `--ghosting` arg — razor-sharp frames
- `--grain` CLI arg removed from V13's parser (dead since `apply_modern_sensor_noise` is no longer called)

### Kept

- `apply_soft_ois_jitter` — sub-pixel hand motion residual
- `apply_soft_rolling_shutter` — CMOS scan-line warp
- `apply_highlight_blooming(threshold=232, strength=0.055)` — photons scattering through glass
- `apply_global_awb_drift` — AWB algorithm recalculating in microscale
- `apply_radial_chromatic_aberration(scale=0.0006)` — corner fringing
- Vignette

### MediaPipe Status

V12 already lazy-imports MediaPipe and never calls it from `process_frame`. V13 inherits that pattern (no MediaPipe in `process_frame`). **Do NOT add `v13` to `requires_mediapipe = {"v9", "v10", "v11"}` in `kling_gui/queue_manager.py`.**

### Reference Pipeline (V13 `process_frame`)

```python
# V13: High-End Daylight Profile
# No face tracking, no sensor noise, no AE hunting, no ghosting.
# Pure physical camera hardware emulation only.

# 1. Physics & Motion
image = apply_soft_ois_jitter(image, state, rng)
image = apply_soft_rolling_shutter(image, state, rng)

# 2. Exposure & Highlights (no AE stepping — V13 assumes stable daylight)
image = apply_highlight_blooming(image, threshold=232, strength=0.055)

# 3. Global Luma Drift (white-balance recalculation in microscale)
image = apply_global_awb_drift(image, state, rng)

# 4. Optics (no sensor noise — flagship phone in bright daylight is grain-free)
image = apply_radial_chromatic_aberration(image, scale=0.0006)

# 5. Vignette
adjusted_vignette = state.get("adjusted_vignette_mask")
if adjusted_vignette is None and vignette_mask is not None:
    vignette_strength = getattr(args, "vignette_strength", 0.55)
    if vignette_strength > 0:
        adjusted_vignette = (1.0 - ((1.0 - vignette_mask) * vignette_strength)).astype(np.float32)
if adjusted_vignette is not None:
    image = np.clip(image.astype(np.float32) * adjusted_vignette, 0, 255).astype(np.uint8)

return image
```

In `naturalize_video` frame loop, hardcode ghosting to 0:

```python
processed = blend_with_previous_frame(current_processed, previous_processed, 0.0)
```

---

## 3. Execution Checklist

Follow `docs/oldcam-wiring.md` for the canonical wiring procedure. V13-specific notes inlined below.

### 3.1 Scaffold `oldcam-v13/`

Clone `oldcam-v12/` → `oldcam-v13/` (Windows + macOS twins). 7 files total:

- `oldcam-v13/oldcam.py` — surgery: edit `process_frame` (remove noise + AE), drop `--grain` from parser, update docstring to "V13 High-End Daylight", swap output suffix `-oldcam-v12` → `-oldcam-v13`, preview caption `Oldcam V12` → `Oldcam V13`, FaceLandmarker error string `Oldcam v9/v10/v11/v12` → `Oldcam v9/v10/v11`
- `oldcam-v13/macOS/oldcam.py` — same surgery as Windows twin
- `oldcam-v13/launcher.py` — V12 → V13 strings (title, docstring, parser description)
- `oldcam-v13/oldcam_launcher.bat` — banner V12 → V13, stamp basename `oldcam_v12_*` → `oldcam_v13_*`. **CRLF required.**
- `oldcam-v13/macOS/oldcam.command` — path matching `oldcam-v12` → `oldcam-v13`, AppleScript prompt V12 → V13, stamp basename → `oldcam_v13_*`
- `oldcam-v13/requirements.txt` — same as v12 (numpy + opencv, no mediapipe)
- `oldcam-v13/macOS/requirements.txt` — same

### 3.2 Four New Hub Launchers

| File | Endings | Content pattern |
|---|---|---|
| `launchers/windows/run_oldcam_v13.bat` | CRLF | `for %%I in ("%~dp0..\..")` → calls `oldcam-v13\oldcam_launcher.bat` |
| `launchers/macos/run_oldcam_v13.command` | LF | `exec "$ROOT_DIR/oldcam-v13/macOS/oldcam.command" "$@"` |
| `launchers/run_oldcam_v13.bat` | CRLF | calls `launchers\windows\run_oldcam_v13.bat` |
| `launchers/run_oldcam_v13.command` | LF | exec `launchers/macos/run_oldcam_v13.command` |

Bat files via PowerShell `[System.IO.File]::WriteAllText` with explicit `` "`r`n" `` joins. Verify with `[System.IO.File]::ReadAllBytes($path)` byte-count of CRLF pairs.

### 3.3 Switch Default v12 → v13

| Layer | File | Edit |
|---|---|---|
| CLI default | `automation/config.py:81` | `"automation_oldcam_version": "v12"` → `"v13"` |
| CLI fallbacks (3) | `kling_automation_ui.py:1564, 1589, 1631` | string `'v12'` → `'v13'` |
| CLI menu list | `kling_automation_ui.py:2027` | append `"v13"` to choice list |
| GUI BooleanVar | `kling_gui/config_panel.py` (~line 519) | v12 `value=True` → `False`; add `"v13": tk.BooleanVar(value=True)` |
| GUI grid tuple | `kling_gui/config_panel.py` (~line 539) | append `"v13"` to enumerate tuple |
| GUI tooltip | `_get_oldcam_version_notes()` | move ★ default marker v12 → v13; add v13 entry |
| Root launcher | `run_oldcam.bat` | chain → `launchers\windows\run_oldcam_v13.bat` |
| Hub Win | `launchers/windows/run_oldcam.bat` | chain → `run_oldcam_v13.bat` |
| Hub macOS | `launchers/macos/run_oldcam.command` | exec → `run_oldcam_v13.command` |
| MediaPipe set | `kling_gui/queue_manager.py:1278` + `:1318` | **DO NOT** add v13 |

### 3.4 Tests (5 files)

- `tests/test_oldcam_versions.py:399` — assertion tuple needs `"v13"` appended
- `tests/test_oldcam_versions.py` — add 3 new tests: `test_v13_default_output_path_uses_v13_suffix`, `test_oldcam_dependency_preflight_does_not_require_mediapipe_for_v13` (asserts True returned), `test_v13_process_frame_skips_noise_and_ae_stepping` (mock both, assert not called)
- `tests/test_launcher_hub_wrappers.py` — add 4 v13 launcher path/target rows, update generic `run_oldcam` chain targets v12 → v13, add v13 launchers to exit-code-preservation list
- `tests/test_oldcam_launcher_env_cache.py` — rename `test_run_oldcam_defaults_to_v12_*` → `*_v13_*`; update assertion `run_oldcam_v12.bat` → `run_oldcam_v13.bat`
- `tests/test_automation_manifest.py:186` — `"v12"` → `"v13"`
- `tests/test_automation_cli_smoke.py:616` — `"v12"` → `"v13"`

### 3.5 Docs

- `README.md` — Oldcam version table: add V13 row + ★ default marker (move from V12 to V13); requirements section already covers v9/v10/v11 mediapipe scope
- `CLAUDE.md` — "Current default version: v12" → "v13"
- `CHANGELOG.md` — add new `## 2026-05-14 (v1.7)` section above v1.6 (V13 added, default switched, perf note about MediaPipe + noise removal)
- `docs/oldcam-versions.md` — add V13 section (theme + flaw + pipeline diagram), extend Side-by-Side table, extend Version History table with V13 row, update Selection Guide to mark V13 as new ★ default
- `docs/oldcam-wiring.md` — add V13 row to comparison table, update "Current default version" footnote, update GUI wiring example tuple

### 3.6 Release Stamp + Dist

- `app_version.py` — `RELEASE_VERSION = "v1.6"` → `"v1.7"`
- Build `dist/SelfieGenUltimate-v1.7.zip` + canonical `dist/SelfieGenUltimate.zip` alias via Python `zipfile` (see prior dist commits for the builder script; exclude `venv/`, `__pycache__/`, `.git/`, `dist/`, `.claude/`, `distribution/`, `.recovery/`)
- Verify in zip: `selfie-gen-ultimate/oldcam-v13/oldcam.py` present, no `apply_modern_sensor_noise(` in `process_frame`, `app_version.py` reads `"v1.7"`, all `.bat` files have CRLF preserved

### 3.7 Commit + Push + Verify

```bash
# Pre-commit hygiene
find . -name "nul" -type f -delete

# Tests
python -m pytest tests/ -q  # expect 340+ passed

# Commit
git add -A
git commit -m "Release v1.7: Oldcam V13 (High-End Daylight) — default..."
git push

# Confirm PR clean
gh pr checks 17

# Optional: re-trigger bots for final review
gh pr comment 17 --body "@coderabbitai review"
```

Update PR title via `gh pr edit 17 --title "Release v1.7: Oldcam V13 (High-End Daylight, default) + V12 (pristine hardware-only) + V11 (signal ordering)"`.

---

## 4. Hard Rules — Don't Re-Learn These

1. **`.bat`/`.cmd` MUST be CRLF.** Always write via PowerShell `[System.IO.File]::WriteAllText` with explicit `` "`r`n" `` joins. The `Write`/`Edit` tools produce LF-only files, which garble every Windows batch command (errors like `'"tokens=1" is not recognized'`).
2. Use `echo(` not `echo.` for blank lines in bat (`echo.` fails under `enabledelayedexpansion`).
3. Log-file redirect: `>>"%LOG_FILE%" echo message` (operator before echo, not after).
4. **Stay on `feat/oldcam-v11` branch.** Despite the original prompt suggesting `feat/oldcam-v13`, the user explicitly said to keep V13 on the same branch — V13 lands in PR #17 alongside V11 + V12.
5. **No new branch.** PR #17 stays open; just push v13 commits to its existing head.
6. Run `find . -name "nul" -type f -delete` immediately before any commit (Windows phantom-file rule).
7. **Mediapipe set is closed.** V12 already exited it; do not let V13 sneak back in.
8. **Sash widths stay at 60% / 54–64%.** Don't revert to 56% / 50–62% — that's a standing user decision documented in CLAUDE.md.

---

## 5. Verification Commands

```bash
# Test suite
python -m pytest tests/ -q
# Expected: 340+ passed (337 baseline + 3 new v13 tests)

# Smoke test imports + default
python -c "from automation.config import merge_automation_defaults; \
  d = merge_automation_defaults({}); \
  assert d['automation_oldcam_version'] == 'v13', f'wrong default: {d[\"automation_oldcam_version\"]}'; \
  print('CLI default OK: v13')"

# GUI sanity (manual)
# - Launch GUI → Video tab
# - Oldcam strip shows v7 v8 v9 v10 v11 v12 v13 with v13 checked by default
# - Hover (ⓘ) tooltip lists v13 with ★ default marker
# - Re-Run controls still visible (not crushed)

# Zip inspection
python -c "import zipfile; z = zipfile.ZipFile('dist/SelfieGenUltimate-v1.7.zip'); \
  names = z.namelist(); \
  assert any('oldcam-v13/oldcam.py' in n for n in names), 'v13 algorithm missing'; \
  assert b'RELEASE_VERSION = \"v1.7\"' in z.read('selfie-gen-ultimate/app_version.py'), 'version not bumped'; \
  v13_src = z.read('selfie-gen-ultimate/oldcam-v13/oldcam.py').decode(); \
  assert 'apply_modern_sensor_noise(' not in v13_src.split('def process_frame')[1].split('def ')[0], 'noise still in process_frame'; \
  print('Zip artifact OK')"

# PR check
gh pr checks 17
gh pr view 17 --json mergeable,mergeStateStatus
```

---

## 6. Pointer to Original Plan

Detailed line-level execution plan with explanatory text lives at:
`C:\Users\d0nbxx\.claude\plans\radiant-wishing-rain.md`

That plan is local-only (not in repo). This hand-off doc is the canonical committed version for future sessions / reviewers.
