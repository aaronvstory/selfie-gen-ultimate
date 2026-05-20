# Similarity Stack Wiring (NON-NEGOTIABLE — full surface coverage)

Detailed wiring tables for every surface the face-similarity
feature touches. The summary table + this file's pointer live
in CLAUDE.md; everything else (sections A-E below) lives here
so CLAUDE.md stays focused on hot-path rules.


The face-similarity feature spans **TEN distinct surfaces**: main GUI carousel, automation CLI pipeline, standalone subproject (own GUI + own CLI), Windows + macOS launchers (per surface), PyInstaller frozen build, dist release zip, and tests. Touching it without updating ALL applicable surfaces ships a broken release.

**Engine layer (single source of truth — DO NOT duplicate):**

| Concern | File |
|---------|------|
| Engine class + scoring math | `similarity_engine.py` (root) |
| Standalone shim | `similarity/src/engine.py` re-exports `from similarity_engine import FaceEngine` |
| App-facing adapter (singleton + config overrides) | `face_similarity.py` (root) |
| Pipeline import | `from face_similarity import compute_face_similarity_details` in `automation/pipeline.py` |
| Main GUI import | `from face_similarity import compute_face_similarity_details` in `kling_gui/carousel_widget.py` |
| Standalone GUI/CLI import | `from src.engine import FaceEngine` in `similarity/src/{gui,cli}.py` |

## A. Adding a new ML dependency (e.g., torch, onnxruntime)

| Layer | File | Action |
|-------|------|--------|
| Main requirements | `requirements.txt` | `+ pkg>=X,<Y` |
| Standalone subproject requirements | `similarity/requirements.txt` | `+ pkg>=X,<Y` |
| Dep-checker registry | `dependency_checker.py:DEPENDENCIES` | Add `Dependency(name=…, import_name=…, pip_name=…, required=False, description=…)` |
| Auto-repair set | `dependency_checker.py:REPAIRABLE_RUNTIME_IMPORTS` | `+ "import_name"` |
| Frozen build hidden imports | `kling_gui_direct.spec:hiddenimports` | `+ 'pkg'` and optionally `collect_submodules('pkg')` |
| Dep stamps (auto-busted) | `.launcher_state/deps_*.ok` and `similarity/.launcher_state/similarity_*.ok` | Auto-busted on `requirements.txt` mtime/size change; manual `rm` if needed |

## B. Adding a similarity GUI control (checkbox/button/etc.)

| Layer | File | Action |
|-------|------|--------|
| Main carousel widget | `kling_gui/carousel_widget.py::_build_panel` | Add widget in `sim_row` (controls) or `meta_frame` (status chips) |
| Bind to engine | `_on_<control>_toggle` method on `ImageCarousel` | Apply to `_get_engine().<attr>` then call `recalc_all_similarity_now(reason=...)` |
| Standalone GUI mirror | `similarity/src/gui.py` | Add `ctk.CTkCheckBox` / `ctk.CTkSwitch` with the same name |
| Standalone CLI mirror | `similarity/src/cli.py::apply_runtime_config` + `similarity/main.py` argparse | Add `--<flag>` with `argparse.BooleanOptionalAction` |
| Config persistence | `kling_config.json` defaults + `face_similarity._apply_config_overrides` | New `automation_similarity_<name>` key |
| Test stubs (main carousel) | `tests/test_carousel_ref_controls.py` `_FakeButton()` block | Add new attribute on the `tab` instance if `_update_panel` reads it |
| Test stubs (standalone GUI) | `similarity/tests/test_gui.py::_CTkModuleStub` | Add new widget class to the stub registry |

## C. Adding a new `automation_similarity_*` config key

| Layer | File | Action |
|-------|------|--------|
| Default value | `kling_config.json` | Add key with sensible default |
| Loader | `face_similarity._apply_config_overrides` | Read with `_parse_bool(...)` for booleans (handles `"true"`/`"false"` strings), `str(...).strip()` for strings |
| Pipeline gate | `automation/pipeline.py` | Read via `self.automation.get("automation_similarity_<key>", default)` |
| Standalone CLI flag | `similarity/main.py` argparse + `similarity/src/cli.py::apply_runtime_config` | Mirror as a CLI flag |
| Tests | `tests/test_automation_pipeline.py`, `tests/test_similarity_canonical_path.py` | New gating + adapter tests |

## D. Adding a new launcher (Windows + macOS, GUI + CLI)

| Layer | Windows | macOS | Notes |
|-------|---------|-------|-------|
| Root wrapper | `run_<name>.bat` | `run_<name>.command` | Two-line passthrough |
| Hub wrapper | `launchers/run_<name>.bat` | `launchers/run_<name>.command` | Hop to platform layer |
| Platform impl | `launchers/windows/run_<name>.bat` (CRLF, `echo(` for blanks) | `launchers/macos/run_<name>.command` (LF — Apple writes the OS as "macOS") | Real venv/dep/exec logic |
| Standalone subproject | `similarity/run_<name>.{bat,command}` | same | Used by hub wrappers `launchers/{windows,macos}/run_similarity_*` (path stays lowercase, OS name in prose stays "macOS") |
| Build pipeline | `distribution/release_prep.py:copy_sanitized_tree` | same | Walks tree → auto-included unless excluded |

## E. Pre-flight checklist (run BEFORE every similarity-stack commit)

- [ ] `requirements.txt` updated if new pip dep
- [ ] `similarity/requirements.txt` updated if new pip dep
- [ ] `dependency_checker.py` (DEPENDENCIES + REPAIRABLE_RUNTIME_IMPORTS) updated
- [ ] `kling_gui_direct.spec` hiddenimports updated if new module imported lazily
- [ ] CLI flag in `similarity/main.py` argparse if user-controllable
- [ ] CTk stub in `similarity/tests/test_gui.py:_CTkModuleStub` if new widget class used
- [ ] `_FakeButton` stubs in `tests/test_carousel_ref_controls.py` if `_update_panel` reads new widget
- [ ] `python -m pytest tests/ similarity/tests/test_cli.py similarity/tests/test_gui.py -q` (all green)
- [ ] Line endings match per-file convention (`requirements.txt` LF, `kling_gui/main_window.py` CRLF — check with `python -c "..."` snippet from prior commits)
- [ ] Smoke-tested both real GUI (`launchers/windows/run_gui.bat`) AND standalone GUI (`launchers/windows/run_similarity_gui.bat`)

**Default config keys (current):** `automation_similarity_threshold` (80), `automation_similarity_use_ensemble` (true), `automation_similarity_secondary_model` ("Facenet512"), `automation_similarity_anti_spoofing` (true), `automation_similarity_require_fas_pass` (false).
