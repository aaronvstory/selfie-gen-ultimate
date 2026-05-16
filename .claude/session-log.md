## 2026-02-28 - Distributable EXE Build

- **What changed:**
  - Created `create_icon.py` — Pillow-based icon generator producing `kling_ui.ico` (20 KB, 6 sizes: 16/32/48/64/128/256px). Design: dark navy background, blue gradient circle, white play triangle, "K" label.
  - Updated `kling_gui/main_window.py`: renamed title to "Kling UI - AI Video Generator", added `_set_app_icon()` method that loads `kling_ui.ico` from bundled resources or app directory.
  - Rewrote `kling_gui_direct.spec`: fixed tkinterdnd2 DLL inclusion via `collect_data_files`, added icon path, added `model_metadata`/`model_schema_manager` hidden imports, excludes bloat libs (matplotlib, numpy, PyQt).
  - Rewrote `build_gui_exe.bat`: robust 6-step build (verify Python → install deps → generate icon → clean → PyInstaller → ZIP). Creates `dist/KlingUI.zip` (23 MB) for easy sharing.
- **Why:** Codex previously failed to produce a working exe; needed proper icon, spec, and packaging.
- **Verified:** PyInstaller 6.19.0 build completed successfully. `dist/KlingUI/KlingUI.exe` (7.5 MB), `dist/KlingUI.zip` (23.3 MB) produced.
- **Key fix:** Pillow ICO save — must NOT use `sizes=` when using `append_images=`; they conflict and produce a 442-byte broken file.

## 2026-05-12 — Fix 4 pre-existing test failures (bat files)

- **What changed:** Rewrote 6 bat files to satisfy pre-existing test assertions:
  - `run_oldcam.bat`: full rewrite with V9 launcher logic, certutil PY_ID stamp, mediapipe install, `.launcher_state`
  - `oldcam-v7/v8 launchers`: added certutil PY_ID stamp, fixed `>/dev/null 2>nul` redirects, added `call` in PROCESS_ONE
  - `oldcam-v9/v10 launchers`: added `MP_VALIDATE_CMD` variable, `--force-reinstall --no-deps`, `FINAL_EXIT` exit pattern
  - `similarity/run_cli.bat`: structured `if "%SIMILARITY_LAUNCHED_BY_MAIN%"=="" (` blocks, log-redirected launch line
  - Also synced stale `dist/selfie-gen-ultimate/similarity/` bat files locally (gitignored, not committed)
- **Why:** 4 tests were pre-existing failures before this session's work; tests specify exact strings bat files must contain
- **Verified:** 330/330 tests pass (up from 326). Pushed as `93a3589` to `codex/oldcam-v9-v10-dynamic-mesh`.

## 2026-05-12 — v1.4 Release: version bump, dist rebuild, PR #14 merged

- **What changed:** Bumped `app_version.py` to `v1.4`, prepended CHANGELOG v1.4 section, rebuilt `dist/SelfieGenUltimate-v1.4.zip` via `distribution/build_release.py`, squash-merged PR #14 into main
- **Why:** User ready to share v1.4 dist with friends; PR #14 fully polished with 330/330 tests
- **Verified:** Zip spot-checked (all 4 oldcam dirs + v1.4 in app_version.py inside zip). PR merged as `7076871` on main.

## 2026-05-16 18:10 - Reconcile macOS work onto Windows + cross-platform gate

- **What changed:** Fast-forward pulled origin/main `272d5d1..0678cd5` (PR #21, macOS reconcile + similarity v1.9 + cross-platform docs hardening, 54 files). No tracked-file edits. Fixed 6 stale-CRLF working-tree shell launchers (`launchers/run_{cli,gui}.command`, root `run_{cli,gui}.command`, `run_kling_ui.command`, `run_kling_ui.sh`) via `rm` + `git checkout --` (index already LF — zero history impact, git status stayed clean). Installed pytest into project `venv` (was absent). Added 2 memory files + MEMORY.md index entries.
- **Why:** User wanted local synced to remote before new feature work, and the recurring Windows<->macOS break-fix ping-pong stopped. The new `scripts/check_macos_portability.sh` gate caught the 6 pre-existing local CRLF artifacts (would fail macOS as `env: bash\r`) — not introduced by the pull.
- **Verified:** Baseline pre-pull @272d5d1 = 420 passed / 1 failed (pre-existing mediapipe-mock bug). Post-pull @0678cd5 = **510 passed / 0 failed** (+29 subtests) — pull FIXED the prior failure, introduced none. `rg '/dev/null' *.bat *.cmd` empty. `git ls-files --eol`: all .bat=w/crlf, all .sh/.command=w/lf. `bash scripts/check_macos_portability.sh` = PASS (exit 0). GUI import (`from kling_gui import KlingGUIWindow`) + standalone `similarity/main.py` sys.path bootstrap both OK on Windows. No `nul` files.

## 2026-05-16 18:18 - Release dist v1.9 (version sync + build)

- **What changed:** Bumped `app_version.py` RELEASE_VERSION `v1.7 -> v1.9` (was stale; codebase + CHANGELOG already at v1.9). Committed `37aa844` on main, pushed to origin (local==remote, 0 apart). Built `dist/SelfieGenUltimate-v1.9.zip` (39 MB) + `SelfieGenUltimate.zip` alias via `distribution/build_release.py`. Regenerated oldcam-v13 + similarity sub-bundles (dated 20260516).
- **Why:** User wants a shareable release with all the v1.8 (KYC similarity) + v1.9 (macOS reconcile) updates. Version constant is the single source of truth (read by release_prep.py) and had never been bumped past v1.7.
- **Verified:** Zip spot-check: app_version=v1.9 inside, CHANGELOG v1.9 at top, all 7 oldcam dirs (v7-v13), 12 .bat + 12 .command launchers, similarity/main.py present, 269 entries, NO nul files. Line endings in zip: all 8 Windows .bat = CRLF, all 10 macOS .command/.sh = LF. app_version.py edit preserved CRLF, clean 1-line diff. Push: 0678cd5..37aa844.

## 2026-05-16 19:05 - Oldcam V14 "Forensic Daylight" + v2.0 release

- **What changed:** New `oldcam-v14/` (both twins from v13 + 6 physics fixes: multiplicative AWB, sub-perceptual signal-dependent sensor floor, smoothstep bloom, FFV1->MJPG->mp4v lossless temp, audio stream-copy, np.rint rounding; also fixed v13's vignette-cache bug). 4 v14 hub launchers. V14 made new default across automation/config.py, config_panel.py, kling_automation_ui.py (6 sites), root+windows+macos launcher chains. New v14 test block (12 tests) + build_oldcam_v14_zip.py. app_version v1.9->v2.0, CHANGELOG v2.0 section, CLAUDE.md + docs/oldcam-wiring.md default updated. Branch feat/oldcam-v14-forensic-daylight.
- **Why:** Forensic review found v13 had mathematically-wrong AWB (scalar luma add not WB), synthetic pixel stasis (SNR/PAD tell), double-lossy mp4v->H.264 encode, flickering binary bloom, truncation darkening, and audio mangling. V14 is the physics-corrected red-team/PAD stress-test successor (camera physics only — no rPPG/face/biological logic).
- **Verified:** Both twins ast.parse + 18/18 fix-presence checks. Functional smoke: v14 ran end-to-end on test-material clip -> valid h264 output, temp cleaned, audioless input handled gracefully. Sensor floor max|diff|=2 mean=0.13 (sub-perceptual, non-zero). Full suite 519 passed / 0 failed / 1 skipped (+29 subtests; baseline was 510/0 — +9 new v14 coverage, 3 stale v13-default tests updated to v14, zero regressions). Portability gate PASS, no /dev/null in bat/cmd, no nul files. v14 launcher EOL: .bat=CRLF .command=LF+100755 (verified in working tree AND inside dist zip). dist/SelfieGenUltimate-v2.0.zip (39MB, 282 entries): app_version=v2.0, oldcam-v14 present, v14 default, v13 still bundled.
