from __future__ import annotations

import shutil
from pathlib import Path


SYNC_ITEMS = [
    "gui_launcher.py",
    "kling_gui_direct.spec",
    "path_utils.py",
    # tk_dialogs.py: kling_gui/{drop_zone,config_panel}.py import it
    # (`from tk_dialogs import ...`). The distribution copy must carry it or
    # `cwd=distribution python -c "import kling_gui.drop_zone"` raises
    # ModuleNotFoundError (Codex P1, PR #61). Keep it in SYNC_ITEMS so a future
    # sync_from_root run doesn't drop it again.
    "tk_dialogs.py",
    # log_utils.py: kling_gui/{queue_manager,tabs/*}.py + kling_generator_falai.py
    # import it (`from log_utils import format_exception_detail`) for uniform
    # failure messages (v2.16). The distribution copy must carry it or
    # `cwd=distribution python -c "import kling_gui.queue_manager"` raises
    # ModuleNotFoundError (Codex P2, PR #67).
    "log_utils.py",
    "model_schema_manager.py",
    "dependency_checker.py",
    # dependency_health_check.py: kling_gui/dependency_repair_dialog.py imports it
    # (`from dependency_health_check import run_repair, verify_in_fresh_process`)
    # for the in-app face-stack repair (v2.11). The distribution copy must carry it
    # or `cwd=distribution python -c "import kling_gui.dependency_repair_dialog"`
    # raises ModuleNotFoundError. dependency_checker.py also delegates to it.
    "dependency_health_check.py",
    # constraints.txt: the project-wide numpy<2 / opencv<4.12 cap file passed via
    # `-c` to every pip install + read by dependency_health_check._constraints_path
    # during in-app repair. Must ship so the repair can hold numpy <2 (v2.11).
    "constraints.txt",
    # pyproject.toml + uv.lock: the v2.20 uv dependency manifest + lockfile. The
    # uv fast-path (scripts/uv_sync_deps.py, called by the launchers) needs both
    # next to the project root it syncs. The release zip already ships them
    # (release_prep walks the working tree); these keep the distribution/
    # direct-run + frozen tree in parity so a cwd=distribution uv sync resolves.
    "pyproject.toml",
    "uv.lock",
    "hooks/hook-tkinterdnd2.py",
    "kling_gui",
    # First-party root modules the kling_gui/ package imports transitively.
    # Without these the distribution/ tree is NOT standalone-importable — its
    # script-mode launcher (run_gui_direct.bat does `cd distribution & python
    # gui_launcher.py`, adding only distribution/ to sys.path) raised
    # ModuleNotFoundError on every one of them (PR #64). The frozen .exe still
    # worked because the PyInstaller spec bundles the whole repo, but the
    # dev-facing direct launcher was broken. Full transitive set below — keep it
    # in sync when kling_gui/ grows a new root-module import.
    "api_keys.py",
    "app_version.py",
    "automation",
    "crop_polisher.py",
    "crop_upscaler.py",
    "face_crop_service.py",
    "face_similarity.py",
    "fal_utils.py",
    "kling_generator_falai.py",
    "model_metadata.py",
    # models.json: model_metadata.py loads it from "next to this file", so the
    # distribution copy needs it too or a cwd=distribution run falls back to the
    # hardcoded list (losing the pricing_fallback + capability user_notes). It
    # already ships in the release zip (release_prep walks the working tree);
    # this keeps the distribution/ direct-run tree in parity.
    "models.json",
    # scripts/: kling_gui/main_window._start_gpu_bootstrap_async imports
    # gpu_bootstrap from REPO_ROOT/scripts at runtime. Without this, a
    # cwd=distribution direct launch (run_gui_direct.bat) silently skips GPU
    # setup (ModuleNotFoundError swallowed). Sync the whole scripts/ dir so the
    # distribution tree can self-bootstrap GPU like the root tree (code-review
    # MEDIUM 2026-06-03). win_resolve_python.bat etc. ride along harmlessly.
    "scripts",
    "outpaint_generator.py",
    "outpaint_geometry.py",
    "selfie_generator.py",
    "selfie_prompt_composer.py",
    "similarity_engine.py",
    "startup_key_onboarding.py",
    "vision_analyzer.py",
]


def main() -> int:
    dist_dir = Path(__file__).resolve().parent
    repo_root = dist_dir.parent
    for item in SYNC_ITEMS:
        src = repo_root / item
        dst = dist_dir / item
        if not src.exists():
            continue
        # Clear any existing dst EAFP-style (gemini MED): pre-flight dst.exists()
        # checks miss the file-vs-dir-mismatch / symlink / restricted-fs cases
        # and can themselves raise. Try rmtree (dir), fall back to unlink (file
        # or symlink); ignore if there's nothing to remove.
        try:
            shutil.rmtree(dst)
        except OSError:
            try:
                dst.unlink()
            except OSError:
                pass
        if src.is_dir():
            shutil.copytree(src, dst)
        else:
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)
        print(f"Synced: {item}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
