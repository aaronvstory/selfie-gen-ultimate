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
