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
    "model_schema_manager.py",
    "dependency_checker.py",
    "hooks/hook-tkinterdnd2.py",
    "kling_gui",
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
