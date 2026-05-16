"""resemble-score entry point.

Routes to the Tkinter GUI (default) or the Rich CLI (``--cli`` or any CLI
flag). Mirrors ``similarity/main.py``: repo-root sys.path bootstrap so the
standalone subproject can import root-level shared modules (``tk_dialogs``),
plus crash logging that preserves the original failure.
"""

import argparse
import os
import sys
import traceback
from pathlib import Path

# Make the repo root importable so this standalone subproject can pull in
# shared root-level modules (tk_dialogs). Without this, launching via
# main.py crashes at import time on `from tk_dialogs ...` because sys.path[0]
# only contains resemble-score/ itself. (CLAUDE.md §3.)
_REPO_ROOT = str(Path(__file__).resolve().parent.parent)
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)


def _write_crash_log(exc: Exception) -> str:
    """Persist a fatal crash traceback to resemble-score/crash.log."""
    crash_path = os.path.join(os.path.dirname(__file__), "crash.log")
    try:
        with open(crash_path, "a", encoding="utf-8") as f:
            f.write("=" * 80 + "\n")
            f.write(f"Fatal error: {exc}\n")
            f.write(traceback.format_exc())
            f.write("\n")
    except Exception as log_exc:
        raise RuntimeError(
            f"Could not write crash log at {crash_path}: {log_exc}"
        ) from log_exc
    return crash_path


def _run_with_crash_logging() -> None:
    """Run the entry point, preserving the original failure if logging fails."""
    try:
        main()
    except SystemExit:
        raise
    except Exception as exc:
        try:
            crash_log_path = _write_crash_log(exc)
            print(
                f"[FATAL] resemble-score crashed: {exc}. "
                f"Traceback saved to: {crash_log_path}",
                file=sys.stderr,
            )
        except Exception as log_exc:
            print(
                f"[FATAL] resemble-score crashed: {exc}. "
                f"Crash logging failed: {log_exc}",
                file=sys.stderr,
            )
        raise


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Score & compare oldcam/kling videos via the Resemble AI "
        "deepfake-detection API.",
        epilog="With no arguments, launches the GUI.",
    )
    parser.add_argument(
        "--cli", action="store_true", help="Run in terminal (CLI) mode."
    )
    parser.add_argument(
        "--folder", type=str, help="Folder to scan for videos (CLI mode)."
    )
    parser.add_argument(
        "--recursive",
        dest="recursive",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Recurse into subfolders (default: on). Use --no-recursive.",
    )
    parser.add_argument(
        "--all",
        dest="select_all",
        action="store_true",
        help="Score every discovered video without prompting (CLI mode).",
    )
    parser.add_argument(
        "--select",
        type=str,
        help='Non-interactive selection: e.g. "1,3", "g:oldcam", '
        '"g:original", "all" (CLI mode).',
    )

    args = parser.parse_args()

    cli_requested = bool(
        args.cli or args.folder or args.select_all or args.select
    )

    if not cli_requested:
        try:
            from src.gui import run_gui
        except ImportError as e:
            print(
                "Failed to load GUI components. Ensure dependencies are "
                f"installed: {e}"
            )
            sys.exit(1)
        run_gui()
        return

    try:
        from src.cli import run_cli
    except ImportError as e:
        print(
            "Failed to load CLI components. Ensure dependencies are "
            f"installed: {e}"
        )
        sys.exit(1)

    sys.exit(
        run_cli(
            folder=args.folder,
            recursive=args.recursive,
            select_all=args.select_all,
            select=args.select,
        )
    )


if __name__ == "__main__":
    _run_with_crash_logging()
