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
        if src.is_dir():
            if dst.exists():
                shutil.rmtree(dst)
            shutil.copytree(src, dst)
        else:
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)
        print(f"Synced: {item}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
