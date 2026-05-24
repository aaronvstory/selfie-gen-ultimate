"""
Kling UI - Direct GUI Launcher
Entry point for PyInstaller to create a GUI-only executable.
Bypasses the CLI menu and launches the Tkinter GUI directly.

SECURITY: Only loads modules from the bundled _MEIPASS directory.
External .py files next to the exe are NOT imported (prevents code hijacking).
"""

import sys
import os
import traceback
from pathlib import Path

try:
    from kling_gui.ml_backend_env import ensure_ml_backend_env
except Exception:
    def ensure_ml_backend_env() -> None:  # type: ignore[redef]
        os.environ["TF_USE_LEGACY_KERAS"] = "1"
        os.environ["KERAS_BACKEND"] = "tensorflow"


def get_tkinter_setup_hint() -> str:
    """Return a platform-aware help message for missing Tk support."""
    if sys.platform == "darwin":
        version = f"{sys.version_info.major}.{sys.version_info.minor}"
        return (
            "This Python installation does not include Tk support.\n\n"
            "On macOS, install a Tk-enabled Python build, then recreate the virtual environment.\n"
            f"If you are using Homebrew Python, install the matching package:\n  brew install python-tk@{version}\n\n"
            "You can also use the python.org macOS installer, which bundles Tk support."
        )

    return "This Python installation does not include Tk support."

# Add appropriate directories to path for imports
if getattr(sys, 'frozen', False):
    # Running as compiled exe
    # _MEIPASS is where PyInstaller extracts bundled files (internal)
    _bundle_dir = getattr(sys, '_MEIPASS', os.path.dirname(sys.executable))
    _app_dir = os.path.dirname(sys.executable)
    
    # SECURITY: Only use the bundled directory, NOT external files
    # This prevents code hijacking if .py files are placed next to the exe
    if _bundle_dir not in sys.path:
        sys.path.insert(0, _bundle_dir)
    # NOTE: _app_dir is NOT added to sys.path for security
    # All modules must be bundled via PyInstaller
else:
    # Running as script - app dir is where gui_launcher.py is
    _app_dir = os.path.dirname(os.path.abspath(__file__))
    if _app_dir not in sys.path:
        sys.path.insert(0, _app_dir)

# Import path utilities for frozen exe compatibility
try:
    from path_utils import get_app_dir, get_crash_log_path
    PATH_UTILS_AVAILABLE = True
except Exception:
    PATH_UTILS_AVAILABLE = False

# PR #49: workspace/instance API for concurrent-launch isolation. Imported
# separately so a missing helper (e.g. an older path_utils on disk) doesn't
# break the legacy crash-log fallback above.
try:
    from path_utils import (
        set_workspace as _pu_set_workspace,
        get_instance_id as _pu_get_instance_id,
        ensure_runtime_dirs as _pu_ensure_runtime_dirs,
        get_runtime_crash_log_path as _pu_get_runtime_crash_log_path,
        WORKSPACE_DEFAULT as _PU_WORKSPACE_DEFAULT,
    )
    WORKSPACE_API_AVAILABLE = True
except Exception:
    WORKSPACE_API_AVAILABLE = False

CLI_ERROR_MODE = os.getenv("KLING_GUI_CLI_ERRORS", "").strip() == "1"


def _resolve_workspace_and_instance():
    """Parse ``--workspace`` and bootstrap runtime dirs early in main().

    Uses ``argparse.parse_known_args`` (NOT ``parse_args``) so the PyInstaller
    bootloader can inject ``--multiprocessing-fork`` and similar without
    raising SystemExit. Sets ``KLING_WORKSPACE`` and ``KLING_INSTANCE_ID``
    env so ``KlingGUIWindow`` (and any subprocess) sees the same identity.

    Returns ``(workspace, instance_id, runtime_dir)`` on success, or
    ``(None, None, None)`` on any failure — caller falls back to the legacy
    shared crash-log path in that case. This function MUST NOT raise.
    """
    if not WORKSPACE_API_AVAILABLE:
        return None, None, None
    try:
        import argparse
        parser = argparse.ArgumentParser(prog="gui_launcher", add_help=False)
        parser.add_argument("--workspace", default=None)
        parser.add_argument("--allow-shared-workspace", action="store_true")
        parser.add_argument("-h", "--help", action="store_true")
        args, _unknown = parser.parse_known_args()
        if args.help:
            sys.stderr.write(
                "gui_launcher.py — Ultimate Selfie-Gen GUI bootstrap\n\n"
                "  --workspace NAME              Run in named workspace dir\n"
                "                                (default: 'default'; or env KLING_WORKSPACE).\n"
                "                                Per-instance runtime isolation applies\n"
                "                                regardless of workspace.\n"
                "  --allow-shared-workspace      Suppress the multi-instance heads-up log.\n"
                "  -h, --help                    Show this message.\n"
            )
            sys.exit(0)
        ws_input = args.workspace or os.environ.get("KLING_WORKSPACE") or _PU_WORKSPACE_DEFAULT
        try:
            workspace = _pu_set_workspace(ws_input)
        except ValueError as exc:
            sys.stderr.write(
                f"[selfie-gen] invalid workspace name {ws_input!r}: {exc}; "
                f"falling back to {_PU_WORKSPACE_DEFAULT!r}\n"
            )
            workspace = _pu_set_workspace(_PU_WORKSPACE_DEFAULT)
        instance_id = _pu_get_instance_id()
        os.environ["KLING_INSTANCE_ID"] = instance_id
        if args.allow_shared_workspace:
            os.environ["KLING_ALLOW_SHARED_WORKSPACE"] = "1"
        runtime_dir = _pu_ensure_runtime_dirs(workspace, instance_id)
        sys.stderr.write(
            f"[selfie-gen] workspace={workspace} instance={instance_id} "
            f"runtime={runtime_dir}\n"
        )
        return workspace, instance_id, runtime_dir
    except SystemExit:
        # argparse calling sys.exit (e.g. -h handler above) — let it propagate.
        raise
    except Exception as exc:
        sys.stderr.write(f"[selfie-gen] workspace bootstrap failed: {exc}\n")
        return None, None, None


def _resolved_crash_log_path():
    """Return the right crash log path for the current launch.

    Prefer the per-instance runtime crash log when workspace bootstrap
    succeeded; fall back to the shared legacy path otherwise. Never raises.

    Gated on ``PATH_UTILS_AVAILABLE`` first so test fixtures that mock that
    flag to False (simulating "path_utils import broken") get the legacy
    relative-path fallback as expected — without this gate, residual env
    vars from earlier work would silently route the crash log to the real
    user_data_dir, breaking tests that point _app_dir at a tmpdir.
    """
    if (
        PATH_UTILS_AVAILABLE
        and WORKSPACE_API_AVAILABLE
        and os.environ.get("KLING_WORKSPACE")
        and os.environ.get("KLING_INSTANCE_ID")
    ):
        try:
            path = _pu_get_runtime_crash_log_path()
            try:
                os.makedirs(os.path.dirname(path), exist_ok=True)
            except OSError:
                pass
            return path
        except Exception:
            pass
    if PATH_UTILS_AVAILABLE:
        try:
            return get_crash_log_path()
        except Exception:
            pass
    return "crash_log.txt"


def _run_dependency_bootstrap() -> bool:
    """Auto-install missing dependencies before GUI import."""
    if os.getenv("KLING_SKIP_PY_STARTUP_DEP_CHECK", "0") == "1":
        return True
    try:
        from dependency_checker import run_dependency_check

        return bool(run_dependency_check(auto_mode=True, install_external_tools=True))
    except Exception as exc:
        sys.stderr.write(f"Warning: dependency bootstrap failed: {exc}\n")
        return False


def _load_gui_window():
    """Import GUI entry lazily so module import is testable and robust."""
    try:
        from kling_gui.main_window import KlingGUIWindow

        return KlingGUIWindow, "", ""
    except Exception as exc:
        return None, f"{type(exc).__name__}: {exc}", traceback.format_exc()


def show_critical_error(title, message):
    """Fallback error reporting using tkinter, with silent fail if tkinter missing."""
    if CLI_ERROR_MODE:
        sys.stderr.write(f"{title}: {message}\n")
        return
    try:
        import tkinter as tk
        from tkinter import messagebox
        root = tk.Tk()
        root.withdraw()
        messagebox.showerror(title, message)
        root.destroy()
    except Exception:
        # If tkinter is also broken, we have no choice but to print (if console exists)
        # In GUI-only mode (console=False), this might not be seen unless redirected.
        sys.stderr.write(f"CRITICAL ERROR: {title}\n{message}\n")


def main():
    """Launch the Tkinter GUI directly."""
    # PR #49: resolve workspace + instance identity FIRST so per-instance
    # runtime dirs (including crash log) exist before anything else runs.
    # Failures here are non-fatal — we fall back to the legacy shared paths
    # so a workspace bug can't block the GUI from launching at all.
    _resolve_workspace_and_instance()

    # Apply TensorFlow/Keras compatibility env before any dependency checks/imports.
    ensure_ml_backend_env()
    _run_dependency_bootstrap()

    KlingGUIWindow, import_error, import_traceback = _load_gui_window()
    if KlingGUIWindow is None:
        # Fallback error handling if GUI can't be imported
        if "_tkinter" in import_error or "tkinter" in import_error.lower():
            error_msg = (
                f"Failed to initialize Kling GUI:\n\n{import_error}\n\n"
                f"{get_tkinter_setup_hint()}"
            )
        else:
            error_msg = (
                f"Failed to initialize Kling GUI:\n\n{import_error}\n\n"
                f"Please ensure all dependencies are installed:\n"
                f"pip install requests pillow rich tkinterdnd2 selenium webdriver-manager "
                f"opencv-python-headless numpy tensorflow==2.16.2 "
                f"tensorflow-intel==2.16.2 tf-keras==2.16.0 retina-face==0.0.17 deepface==0.0.92\n\n"
                f"If you're running the standalone exe, this may indicate a build issue.\n"
                f"All dependencies should be bundled internally."
            )
        
        # Log crash to file if possible. PR #49: prefer per-instance runtime
        # crash log when workspace bootstrap succeeded.
        crash_log = _resolved_crash_log_path()
        if not os.path.isabs(crash_log):
            crash_log = os.path.join(_app_dir, crash_log)

        try:
            with open(crash_log, 'w', encoding='utf-8') as f:
                f.write("Kling UI Initialization Failure\n")
                f.write("=" * 50 + "\n\n")
                f.write(f"Error: {import_error}\n\n")
                f.write("Traceback:\n")
                f.write(import_traceback)
            error_msg += f"\n\nDetails saved to:\n{crash_log}"
        except Exception:
            pass
        if CLI_ERROR_MODE:
            sys.stderr.write(f"Import Error: {error_msg}\n")
        else:
            show_critical_error("Import Error", error_msg)
        sys.exit(1)
    
    try:
        # Create and run the GUI window
        app = KlingGUIWindow()
        app.run()
    except Exception as e:
        # Log crash to file for debugging. PR #49: per-instance runtime path.
        crash_log = _resolved_crash_log_path()
        if not os.path.isabs(crash_log):
            crash_log = os.path.join(_app_dir, crash_log)

        try:
            with open(crash_log, 'w', encoding='utf-8') as f:
                f.write("Kling UI Runtime Crash Report\n")
                f.write("=" * 50 + "\n\n")
                f.write(f"Error: {str(e)}\n\n")
                f.write("Traceback:\n")
                f.write(traceback.format_exc())
        except Exception:
            pass
        
        runtime_msg = (
            f"An unexpected error occurred:\n\n{str(e)}\n\n"
            f"Crash log saved to:\n{crash_log}"
        )
        if CLI_ERROR_MODE:
            sys.stderr.write(f"Kling UI Error: {runtime_msg}\n")
        else:
            show_critical_error("Kling UI Error", runtime_msg)
        sys.exit(1)


if __name__ == "__main__":
    main()
