"""
Main Window - Primary GUI window that assembles all components.
"""

import tkinter as tk
from tkinter import ttk, messagebox, simpledialog
import json
import os
import sys
import logging
import time
import re
from copy import deepcopy
import webbrowser
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Optional, List, TYPE_CHECKING
from datetime import datetime

from api_keys import API_KEY_SPECS, ensure_key_fields, key_status, non_required_missing_specs
from automation.config import get_outpaint_fal_timeout_seconds
from startup_key_onboarding import missing_startup_specs, startup_prompt_specs, startup_status_lines
from tk_dialogs import select_directory, select_open_files

# Import path utilities
from path_utils import (
    get_config_path,
    get_crash_log_path,
    get_log_path,
    get_app_dir,
    get_resource_dir,
    get_user_data_dir,
    preflight_image_path,
    sanitize_path_name,
    sanitize_tree_names_portable_report,
)

from .drop_zone import DropZone, create_dnd_root, HAS_DND, DND_FILES, parse_dnd_paths
from .log_display import LogDisplay
from .config_panel import ConfigPanel
from .queue_manager import QueueManager, QueueItem
from .image_state import ImageSession
from .carousel_widget import ImageCarousel
from .compare_panel import ComparePanel
from .session_controller import SessionController
from .tabs import FaceCropTab, PrepTab, SelfieTab, ExpandTab, VideoTab
from .theme import (
    BUTTON_DISABLED_TEXT_COLOR,
    BUTTON_TEXT_COLOR,
    TTK_BTN_COMPACT,
    TTK_BTN_DANGER,
    TTK_BTN_DANGER_COMPACT,
    TTK_BTN_PRIMARY,
    TTK_BTN_SECONDARY,
    TTK_BTN_SUCCESS,
    TTK_BTN_SUCCESS_COMPACT,
    TTK_BTN_TAB_NAV,
    create_action_button,
    debounce_command,
)
from .layout_utils import (
    sanitize_saved_geometry as _sanitize_saved_geometry,
    sanitize_window_layout as _sanitize_window_layout,
    sanitize_sash_layout as _sanitize_sash_layout,
)

# Try to import the generator
try:
    from kling_generator_falai import FalAIKlingGenerator

    HAS_GENERATOR = True
except ImportError:
    HAS_GENERATOR = False
    if TYPE_CHECKING:
        from kling_generator_falai import FalAIKlingGenerator


# Color palette
COLORS = {
    "bg_main": "#2D2D30",
    "bg_panel": "#3C3C41",
    "bg_input": "#464649",
    "bg_drop": "#464649",
    "bg_hover": "#505055",
    "text_light": "#DCDCDC",
    "text_dim": "#B4B4B4",
    "text_dark": "#111111",
    "accent_blue": "#6496FF",
    "success": "#64FF64",
    "error": "#FF6464",
    "warning": "#FFA500",
    "border": "#5A5A5E",
    "drop_valid": "#329632",
    "btn_green": "#329632",
    "btn_red": "#B43232",
}

# Valid image extensions for folder scanning
VALID_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp", ".bmp", ".gif", ".tiff", ".tif"}
IS_MACOS = sys.platform == "darwin"
FONT_FAMILY = "Helvetica" if IS_MACOS else "Segoe UI"
EMOJI_FONT_FAMILY = "Apple Color Emoji" if IS_MACOS else "Segoe UI Emoji"


UI_CONFIG_DEFAULTS = {
    "window": {"width": 1100, "height": 950, "min_width": 800, "min_height": 700},
    "config_panel": {
        "prompt_preview_height": 6,
        "prompt_preview_font_size": 9,
        "negative_prompt_height": 1,
    },
    "drop_zone": {"height": 560},
    "queue_panel": {"width": 300},
    "history_panel": {"height": 260, "visible_rows": 10},
    "debug": {"enabled": False, "inspector_hotkey": "F12", "reload_hotkey": "F5"},
}


def sanitize_saved_geometry(saved_geometry: str, min_width: int, min_height: int, max_width: int, max_height: int) -> str:
    """Backwards-compatible wrapper around layout_utils implementation."""
    return _sanitize_saved_geometry(saved_geometry, min_width, min_height, max_width, max_height)


def sanitize_window_layout(window_config: dict, saved_geometry: str, screen_width: int, screen_height: int) -> tuple[dict, str, bool]:
    """Backwards-compatible wrapper around layout_utils implementation."""
    return _sanitize_window_layout(window_config, saved_geometry, screen_width, screen_height)


def sanitize_sash_layout(
    sash_dropzone,
    sash_prompt_split,
    sash_queue,
    sash_log,
    sash_log_drop_split,
    root_width: int,
    root_height: int,
) -> tuple[dict, bool]:
    """Backwards-compatible wrapper around layout_utils implementation."""
    return _sanitize_sash_layout(
        sash_dropzone,
        sash_prompt_split,
        sash_queue,
        sash_log,
        sash_log_drop_split,
        root_width,
        root_height,
    )


class FolderPreviewDialog(tk.Toplevel):
    """Dialog showing matched files before adding to queue."""

    def __init__(
        self, parent, files: List[str], folder: str, pattern: str, match_mode: str
    ):
        super().__init__(parent)
        self.title("Folder Processing Preview")
        self.result = None  # True = proceed, None = cancel
        self.files = files

        # Modal
        self.transient(parent)
        self.grab_set()
        self.configure(bg=COLORS["bg_panel"])
        self.geometry("700x550")
        self.minsize(500, 400)

        # Header
        tk.Label(
            self,
            text=f"Found {len(files)} matching images",
            font=(FONT_FAMILY, 14, "bold"),
            bg=COLORS["bg_panel"],
            fg=COLORS["text_light"],
        ).pack(pady=(15, 5))

        # Info frame
        info_frame = tk.Frame(self, bg=COLORS["bg_panel"])
        info_frame.pack(fill=tk.X, padx=20)

        tk.Label(
            info_frame,
            text=f"Folder: {folder}",
            font=(FONT_FAMILY, 9),
            bg=COLORS["bg_panel"],
            fg=COLORS["text_dim"],
            anchor="w",
        ).pack(fill=tk.X)

        mode_text = "exact name" if match_mode == "exact" else "contains"
        tk.Label(
            info_frame,
            text=f"Pattern: '{pattern}' ({mode_text})",
            font=(FONT_FAMILY, 9),
            bg=COLORS["bg_panel"],
            fg=COLORS["accent_blue"],
            anchor="w",
        ).pack(fill=tk.X)

        # File list with scrollbar
        list_frame = tk.Frame(self, bg=COLORS["bg_main"])
        list_frame.pack(fill=tk.BOTH, expand=True, padx=20, pady=15)

        scrollbar = ttk.Scrollbar(list_frame)
        scrollbar.pack(side=tk.RIGHT, fill=tk.Y)

        self.file_list = tk.Listbox(
            list_frame,
            bg=COLORS["bg_main"],
            fg=COLORS["text_light"],
            font=("Consolas", 9),
            selectbackground=COLORS["accent_blue"],
            yscrollcommand=scrollbar.set,
            borderwidth=0,
            highlightthickness=0,
        )
        self.file_list.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        scrollbar.config(command=self.file_list.yview)

        # Populate list (show relative paths)
        for f in files:
            try:
                rel_path = os.path.relpath(f, folder)
            except ValueError:
                rel_path = os.path.basename(f)
            self.file_list.insert(tk.END, rel_path)

        # Buttons
        btn_frame = tk.Frame(self, bg=COLORS["bg_panel"])
        btn_frame.pack(fill=tk.X, padx=20, pady=(0, 15))

        tk.Button(
            btn_frame,
            text="Cancel",
            font=(FONT_FAMILY, 10),
            bg=COLORS["bg_input"],
            fg=COLORS["text_light"],
            width=12,
            command=self._cancel,
        ).pack(side=tk.RIGHT, padx=5)

        tk.Button(
            btn_frame,
            text=f"Add {len(files)} to Queue",
            font=(FONT_FAMILY, 10, "bold"),
            bg=COLORS["btn_green"],
            fg="white",
            width=18,
            command=self._proceed,
        ).pack(side=tk.RIGHT, padx=5)

        # Center on parent
        self.update_idletasks()
        x = parent.winfo_x() + (parent.winfo_width() - 700) // 2
        y = parent.winfo_y() + (parent.winfo_height() - 550) // 2
        self.geometry(f"+{x}+{y}")

        self.wait_window()

    def _proceed(self):
        self.result = True
        self.destroy()

    def _cancel(self):
        self.result = None
        self.destroy()


class SessionManagerDialog(tk.Toplevel):
    """Dialog for browsing, loading, and managing saved sessions."""

    def __init__(self, parent, app_dir, image_session, config, save_config_fn, log_fn):
        super().__init__(parent)
        self.title("Session Manager")
        self.configure(bg=COLORS["bg_main"])
        self.transient(parent)
        self.resizable(True, True)

        # Store references
        self._app_dir = app_dir
        self._image_session = image_session
        self._config = config
        self._save_config_fn = save_config_fn
        self._log_fn = log_fn
        self._selected_path = None
        self._selected_record = None
        self._loaded_session_data = None

        self._build_ui()
        self._refresh_list()

        # Center and grab
        self.geometry("680x520")
        self.update_idletasks()
        # Ensure parent geometry is current before reading dimensions
        parent.update_idletasks()
        pw = parent.winfo_width()
        ph = parent.winfo_height()
        # Fallback if parent hasn't been mapped yet (returns 1)
        if pw < 10:
            pw = parent.winfo_reqwidth() or 680
        if ph < 10:
            ph = parent.winfo_reqheight() or 520
        x = parent.winfo_rootx() + (pw - 680) // 2
        y = parent.winfo_rooty() + (ph - 520) // 2
        self.geometry(f"+{max(0, x)}+{max(0, y)}")
        self.grab_set()
        self.focus_set()

    def _build_ui(self):
        # Header
        header = tk.Label(
            self, text="Saved Sessions", font=(FONT_FAMILY, 12, "bold"),
            bg=COLORS["bg_main"], fg=COLORS["text_light"],
        )
        header.pack(fill=tk.X, padx=16, pady=(12, 6))

        # Listbox with scrollbar
        list_frame = tk.Frame(self, bg=COLORS["bg_main"])
        list_frame.pack(fill=tk.BOTH, expand=True, padx=16, pady=(0, 6))

        scrollbar = tk.Scrollbar(list_frame)
        scrollbar.pack(side=tk.RIGHT, fill=tk.Y)

        self._listbox = tk.Listbox(
            list_frame, bg=COLORS["bg_input"], fg=COLORS["text_light"],
            selectbackground=COLORS["accent_blue"], selectforeground="white",
            font=("Consolas", 10), yscrollcommand=scrollbar.set,
            activestyle="none",
        )
        self._listbox.pack(fill=tk.BOTH, expand=True)
        scrollbar.config(command=self._listbox.yview)
        self._listbox.bind("<<ListboxSelect>>", self._on_select)
        self._listbox.bind("<Double-Button-1>", lambda e: self._on_load())

        # Detail label
        self._detail_label = tk.Label(
            self, text="Select a session to view details",
            font=(FONT_FAMILY, 9), bg=COLORS["bg_main"], fg=COLORS["text_dim"],
            anchor="w",
        )
        self._detail_label.pack(fill=tk.X, padx=16, pady=(0, 8))

        # Auto-save section
        autosave_frame = tk.Frame(self, bg=COLORS["bg_panel"], padx=10, pady=6)
        autosave_frame.pack(fill=tk.X, padx=16, pady=(0, 8))

        tk.Label(
            autosave_frame, text="Auto-Save:", font=(FONT_FAMILY, 9, "bold"),
            bg=COLORS["bg_panel"], fg=COLORS["text_light"],
        ).pack(side=tk.LEFT, padx=(0, 8))

        self._autosave_var = tk.BooleanVar(value=self._config.get("session_autosave_enabled", True))
        autosave_cb = tk.Checkbutton(
            autosave_frame, text="Enabled", variable=self._autosave_var,
            bg=COLORS["bg_panel"], fg=COLORS["text_light"],
            selectcolor=COLORS["bg_input"], activebackground=COLORS["bg_panel"],
            command=self._on_autosave_changed,
        )
        autosave_cb.pack(side=tk.LEFT, padx=(0, 12))

        tk.Label(
            autosave_frame, text="Interval:", font=(FONT_FAMILY, 9),
            bg=COLORS["bg_panel"], fg=COLORS["text_dim"],
        ).pack(side=tk.LEFT, padx=(0, 4))

        self._interval_var = tk.StringVar(value=self._config.get("session_autosave_interval", "after_api_action"))
        interval_menu = ttk.Combobox(
            autosave_frame, textvariable=self._interval_var,
            values=["5min", "10min", "15min", "after_api_action"],
            state="readonly", width=16,
        )
        interval_menu.pack(side=tk.LEFT)
        interval_menu.bind("<<ComboboxSelected>>", lambda e: self._on_autosave_changed())

        # Button bar
        btn_frame = tk.Frame(self, bg=COLORS["bg_main"])
        btn_frame.pack(fill=tk.X, padx=16, pady=(0, 12))

        for text, style_name, cmd in [
            ("Delete", TTK_BTN_DANGER, self._on_delete),
            ("Clear Project Sessions", TTK_BTN_SECONDARY, self._on_clear_project),
            ("Overwrite Save", TTK_BTN_PRIMARY, self._on_overwrite),
            ("Save New", TTK_BTN_SUCCESS, self._on_save_new),
        ]:
            create_action_button(
                btn_frame, text=text, command=cmd, style=style_name
            ).pack(side=tk.LEFT, padx=(0, 6))

        create_action_button(
            btn_frame, text="Close", command=self.destroy, style=TTK_BTN_SECONDARY
        ).pack(side=tk.RIGHT)

        create_action_button(
            btn_frame, text="Load", command=self._on_load, style=TTK_BTN_PRIMARY
        ).pack(side=tk.RIGHT, padx=(0, 6))

    def _refresh_list(self):
        from .session_manager import list_sessions
        self._sessions = list_sessions(self._app_dir)
        self._listbox.delete(0, tk.END)
        for rec in self._sessions:
            ts = rec.updated_at[:16].replace("T", " ") if rec.updated_at else "?"
            badge = "[AUTOSAVE]" if rec.session_kind == "autosave" else "[MANUAL]"
            row = f"  {badge:<10s} {rec.project_key:<20s} {ts}  {rec.image_count:>3d} imgs  {rec.name}"
            self._listbox.insert(tk.END, row)

    def _on_select(self, event=None):
        sel = self._listbox.curselection()
        if not sel or sel[0] >= len(self._sessions):
            self._selected_path = None
            self._selected_record = None
            self._detail_label.config(text="Select a session to view details")
            return
        rec = self._sessions[sel[0]]
        self._selected_record = rec
        self._selected_path = rec.path
        ts = rec.updated_at[:19].replace("T", " ") if rec.updated_at else "unknown"
        kind = "autosave" if rec.session_kind == "autosave" else "manual"
        self._detail_label.config(
            text=f"Selected: {rec.name} — {kind} — project {rec.project_key} — saved {ts} — {rec.image_count} images"
        )

    def _on_load(self):
        if not self._selected_path:
            return
        from .session_manager import load_session
        try:
            data = load_session(self._selected_path)
            self._loaded_session_data = data
            self._log_fn(f"Session loaded: {data.get('name', '?')}", "success")
            self.destroy()
        except Exception as e:
            self._log_fn(f"Failed to load session: {e}", "error")

    def _on_delete(self):
        if not self._selected_path:
            return
        from .session_manager import delete_session
        from tkinter import messagebox
        if not messagebox.askyesno("Delete Session", "Delete this saved session?", parent=self):
            return
        try:
            delete_session(self._selected_path)
            self._log_fn("Session deleted", "info")
            self._selected_path = None
            self._selected_record = None
            self._refresh_list()
        except Exception as e:
            self._log_fn(f"Delete failed: {e}", "error")

    def _on_clear_project(self):
        """Delete all sessions for the selected project (manual + autosave)."""
        if not self._selected_record:
            return
        from .session_manager import delete_project_sessions
        from tkinter import messagebox

        project_key = self._selected_record.project_key
        matching = [s for s in self._sessions if s.project_key == project_key]
        count = len(matching)
        if count == 0:
            self._log_fn("No project sessions to clear", "warning")
            return

        msg = (
            f"Delete all sessions for project '{project_key}'?\n\n"
            f"This will remove {count} saved session file(s), including autosaves and manual saves."
        )
        if not messagebox.askyesno("Clear Project Sessions", msg, parent=self):
            return
        try:
            removed = delete_project_sessions(self._app_dir, project_key)
            self._selected_path = None
            self._selected_record = None
            self._refresh_list()
            self._detail_label.config(text="Select a session to view details")
            self._log_fn(f"Cleared {removed} session(s) for project '{project_key}'", "success")
        except Exception as e:
            self._log_fn(f"Clear project failed: {e}", "error")

    def _on_overwrite(self):
        """Save current state to the selected session file."""
        if not self._selected_path:
            return
        if not self._image_session.count:
            self._log_fn("No images in session to save", "warning")
            return
        from .session_manager import save_session
        try:
            save_session(
                self._app_dir, self._image_session, self._config,
                overwrite_path=self._selected_path,
            )
            self._log_fn("Session overwritten", "success")
            self._refresh_list()
        except Exception as e:
            self._log_fn(f"Overwrite failed: {e}", "error")

    def _on_save_new(self):
        """Save current session state as a new session file."""
        if not self._image_session.count:
            self._log_fn("No images in session to save", "warning")
            return
        from datetime import datetime as _dt
        default_name = f"session_{_dt.now().strftime('%Y%m%d_%H%M%S')}"
        name = simpledialog.askstring(
            "Save Session", "Session name:", initialvalue=default_name, parent=self,
        )
        if not name:
            return
        from .session_manager import save_session
        try:
            save_session(
                self._app_dir, self._image_session, self._config, name=name,
            )
            self._log_fn(f"Session saved: {name}", "success")
            self._refresh_list()
        except Exception as e:
            self._log_fn(f"Save failed: {e}", "error")

    def _on_autosave_changed(self):
        self._config["session_autosave_enabled"] = self._autosave_var.get()
        self._config["session_autosave_interval"] = self._interval_var.get()
        self._save_config_fn()


class KlingGUIWindow:
    """Main GUI window for Kling video generation."""

    def __init__(self, config_path: Optional[str] = None):
        """
        Initialize the GUI window.

        Args:
            config_path: Path to the configuration JSON file (defaults to app dir)
        """
        # Use get_config_path for proper frozen exe compatibility
        if config_path is None:
            self.config_path = get_config_path("kling_config.json")
        elif os.path.isabs(config_path):
            self.config_path = config_path
        else:
            # Relative path - make it relative to app dir
            self.config_path = os.path.join(get_app_dir(), config_path)

        self.data_dir = get_user_data_dir() if sys.platform == "darwin" else get_app_dir()

        self.config = self._load_config()
        if ensure_key_fields(self.config):
            self._save_config()
        self.ui_config_path = (
            get_config_path("ui_config.json")
            if sys.platform == "darwin"
            else os.path.join(get_app_dir(), "ui_config.json")
        )
        self.ui_config = self._load_ui_config()
        self._layout_corrections_pending = False
        self.edit_mode = False
        self.dimension_labels = {}
        # History file in same directory as config
        self.history_path = os.path.join(
            os.path.dirname(self.config_path), "kling_history.json"
        )

        # Set up logging BEFORE anything that might call _log()
        self.logger = self._setup_logging()

        self.history: List[dict] = self._load_history()
        self.generator: Optional["FalAIKlingGenerator"] = None
        self.queue_manager: Optional[QueueManager] = None
        self.image_session = ImageSession()
        self._autosave_debounce_ms = 1200

        # Tell Windows this is its own app (not just "python.exe") so the
        # taskbar shows our custom icon instead of the generic Python icon.
        try:
            import ctypes
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(
                "kling-ui.ai-media-toolkit.1"
            )
        except Exception:
            pass  # Non-Windows or missing API — harmless

        # Create root window with DnD support if available
        self.root = create_dnd_root()
        self.root.title("Ultimate-Selfie-Gen")
        self.root.configure(bg=COLORS["bg_main"])
        self._drop_zone_window = None
        self._drop_zone_open_guard_until = 0.0
        self._similarity_window = None
        self._similarity_open_guard_until = 0.0
        self.session_controller = SessionController(
            root=self.root,
            data_dir=self.data_dir,
            image_session=self.image_session,
            config_getter=lambda: self.config,
            log_callback=self._log,
            logger=self.logger,
            autosave_debounce_ms=self._autosave_debounce_ms,
        )
        self.image_session.add_on_change(self._on_image_session_changed)

        # Set app icon (window title bar + taskbar)
        self._set_app_icon()

        # Restore window geometry or use defaults
        window_config = self.ui_config.get("window", {})
        screen_w = self.root.winfo_screenwidth()
        screen_h = self.root.winfo_screenheight()
        sanitized_window, sanitized_geometry, window_changed = sanitize_window_layout(
            window_config=window_config,
            saved_geometry=self.config.get("window_geometry", ""),
            screen_width=screen_w,
            screen_height=screen_h,
        )
        self.ui_config["window"] = sanitized_window
        self.config["window_geometry"] = sanitized_geometry
        if window_changed:
            self._layout_corrections_pending = True

        # Pre-sanitize sash values against initial window size before widgets render.
        pre_sash, pre_sash_changed = sanitize_sash_layout(
            sash_dropzone=self.config.get("sash_dropzone", 500),
            sash_prompt_split=self.config.get("sash_prompt_split", 620),
            sash_queue=self.config.get("sash_queue", 320),
            sash_log=self.config.get("sash_log", 150),
            sash_log_drop_split=self.config.get("sash_log_drop_split", 360),
            root_width=sanitized_window["width"],
            root_height=sanitized_window["height"],
        )
        self.config.update(pre_sash)
        if pre_sash_changed:
            self._layout_corrections_pending = True

        window_width = sanitized_window["width"]
        window_height = sanitized_window["height"]
        min_width = sanitized_window["min_width"]
        min_height = sanitized_window["min_height"]
        if sanitized_geometry:
            self.root.geometry(sanitized_geometry)
        else:
            self.root.geometry(f"{window_width}x{window_height}")
        self.root.minsize(min_width, min_height)

        # Set up the UI
        self._setup_ui()

        # Apply ui_config first (minsize, config_panel), then restore sash positions
        # after widgets are fully rendered. _restore_sash_positions must run last.
        self.root.after(50, self._apply_ui_config)
        self.root.after(250, self._restore_sash_positions)
        self.root.after(450, self._persist_layout_corrections_if_needed)

        # Enable debug hotkeys/inspector if configured
        self._setup_debug_hotkeys()

        # First-run key prompt (Fal.ai required for generation)
        self._prompt_startup_provider_keys_on_first_run()

        # Initialize generator and queue manager
        self._init_generator()

        # Protocol for window close
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

    def _set_app_icon(self):
        """Load and set the app icon from bundled resources or alongside the exe."""
        try:
            from path_utils import get_resource_dir, get_app_dir
            import tkinter as tk

            icon_name = "kling_ui.ico"

            # Check bundled resources first (PyInstaller _MEIPASS), then app dir
            for search_dir in [get_resource_dir(), get_app_dir()]:
                icon_path = os.path.join(search_dir, icon_name)
                if os.path.isfile(icon_path):
                    self.root.iconbitmap(icon_path)
                    return
        except Exception:
            pass  # Icon is cosmetic - never crash the app over it

    def _setup_logging(self) -> logging.Logger:
        """Configure rotating file logging."""
        # Use get_log_path for proper frozen exe compatibility
        log_file = get_log_path("kling_gui.log")

        logger = logging.getLogger("kling_gui")
        logger.setLevel(logging.INFO)
        logger.propagate = False

        if not any(isinstance(h, RotatingFileHandler) for h in logger.handlers):
            handler = RotatingFileHandler(
                log_file,
                maxBytes=int(self.config.get("log_max_mb", 5) * 1024 * 1024),
                backupCount=int(self.config.get("log_backups", 3)),
                encoding="utf-8",
            )
            fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s")
            handler.setFormatter(fmt)
            logger.addHandler(handler)

        return logger

    def _load_config(self) -> dict:
        """Load configuration from JSON file."""
        default_config = {
            "output_folder": "",  # Empty by default - user picks their own
            "use_source_folder": True,
            "falai_api_key": "",
            "verbose_logging": True,
            "verbose_gui_mode": True,  # Verbose GUI logging ON by default
            "log_max_mb": 5,
            "log_backups": 3,
            "duplicate_detection": True,
            "current_prompt_slot": 4,
            "saved_prompts": {str(i): "" for i in range(1, 11)},
            "negative_prompts": {str(i): "" for i in range(1, 11)},
            "model_capabilities": {},
            "current_model": "fal-ai/kling-video/v2.5-turbo/pro/image-to-video",
            "model_display_name": "Kling 2.5 Turbo Pro",
            "video_duration": 10,
            "loop_videos": True,  # Loop videos ON by default
            "oldcam_videos": True,  # Oldcam Finish ON by default
            "oldcam_version": "v7",
            "allow_reprocess": True,
            "reprocess_mode": "increment",
            "custom_models": [],
            "hidden_models": [],
            "freeimage_api_key": "",
            "bfl_api_key": "",
            "upload_provider_order": ["fal_cdn", "freeimage"],
            "openrouter_vision_system_prompt": "",
            "selfie_output_mode": "source",
            "selfie_output_folder": "",
            "selfie_poll_timeout_seconds": 300,
            "selfie_current_prompt_slot": 3,
            "outpaint_fal_timeout_seconds": 150,
            "selfie_saved_prompts": {str(i): "" for i in range(1, 11)},
            "selfie_prompt_titles": {str(i): f"Prompt {i}" for i in range(1, 11)},
            "selfie_selected_models": {
                "bfl/flux-kontext-pro": False,
                "bfl/flux-kontext-max": False,
                "bfl/flux-2-pro": False,
                "fal-ai/flux-pulid": True,
                "fal-ai/pulid": False,
                "fal-ai/instant-character": False,
                "fal-ai/z-image/turbo/image-to-image": False,
                "fal-ai/nano-banana-pro/edit": False,
                "fal-ai/qwen-image-edit": False,
                "fal-ai/bytedance/seedream/v4.5/edit": False,
                "fal-ai/bytedance/seedream/v5/edit": False,
                "fal-ai/bytedance/seedream/v5/lite/edit": False,
            },
            # Folder processing settings
            "folder_filter_pattern": "",
            "folder_match_mode": "partial",  # "partial" or "exact"
            # Window layout persistence
            "window_geometry": "",  # Empty = use default
            "sash_dropzone": 500,  # Height of top pane
            "sash_queue": 320,  # Width of left bottom pane
            "sash_log": 150,  # Height of log pane (before history)
            "sash_log_drop_split": 360,  # Width split in log pane (log | permanent drop zone)
        }

        # Layer 1: apply bundled defaults template (prompts, model, etc.)
        try:
            from path_utils import get_resource_dir
            template_path = os.path.join(get_resource_dir(), "default_config_template.json")
            if os.path.exists(template_path):
                with open(template_path, "r", encoding="utf-8") as f:
                    template = json.load(f)
                    if isinstance(template, dict):
                        self._deep_merge_dict(default_config, template)
        except Exception:
            pass  # Template is cosmetic - never crash on missing defaults

        # Layer 2: apply user's saved config (overrides everything)
        try:
            if os.path.exists(self.config_path):
                with open(self.config_path, "r", encoding="utf-8") as f:
                    loaded = json.load(f)
                    if isinstance(loaded, dict):
                        self._deep_merge_dict(default_config, loaded)
        except Exception as e:
            print(f"Warning: Could not load config: {e}")

        # Layer 3: sanitize prompt slot dicts — ensure all 10 slots exist as
        # strings, not None.  Shallow .update() above replaces the entire nested
        # dict, so slots that were missing in the user file (or set to null by
        # an older CLI) would otherwise surface as None and silently vanish.
        for key in ("saved_prompts", "negative_prompts", "prompt_titles"):
            bucket = default_config.get(key)
            if not isinstance(bucket, dict):
                bucket = {}
                default_config[key] = bucket
            for slot in (str(i) for i in range(1, 11)):
                if slot not in bucket or bucket[slot] is None:
                    bucket[slot] = ""

        for key in ("selfie_saved_prompts", "selfie_prompt_titles"):
            bucket = default_config.get(key)
            if not isinstance(bucket, dict):
                bucket = {}
                default_config[key] = bucket
            for slot in (str(i) for i in range(1, 11)):
                if key == "selfie_prompt_titles":
                    if slot not in bucket or bucket[slot] is None or str(bucket[slot]).strip() == "":
                        bucket[slot] = f"Prompt {slot}"
                elif slot not in bucket or bucket[slot] is None:
                    bucket[slot] = ""

        try:
            selfie_slot = int(default_config.get("selfie_current_prompt_slot", 3))
        except Exception:
            selfie_slot = 3
        default_config["selfie_current_prompt_slot"] = min(10, max(1, selfie_slot))
        try:
            kling_slot = int(default_config.get("current_prompt_slot", 4))
        except Exception:
            kling_slot = 4
        default_config["current_prompt_slot"] = min(10, max(1, kling_slot))
        default_config["outpaint_fal_timeout_seconds"] = get_outpaint_fal_timeout_seconds(default_config)

        # Layer 4: migrate known broken endpoint paths
        self._migrate_endpoints(default_config)

        return default_config

    @staticmethod
    def _migrate_endpoints(config: dict) -> None:
        """Auto-correct known broken fal.ai endpoint paths in saved config."""
        _ENDPOINT_MIGRATIONS = {
            "fal-ai/kling-video/v2.5/turbo-pro/image-to-video": "fal-ai/kling-video/v2.5-turbo/pro/image-to-video",
            "fal-ai/kling-video/v2.5/turbo-standard/image-to-video": "fal-ai/kling-video/v2.5-turbo/standard/image-to-video",
        }
        current = config.get("current_model", "")
        if current in _ENDPOINT_MIGRATIONS:
            config["current_model"] = _ENDPOINT_MIGRATIONS[current]
            print(f"Migrated endpoint: {current} -> {config['current_model']}")

    def _merge_ui_config(self, base: dict, updates: dict) -> dict:
        """Deep-merge UI config dictionaries."""
        for key, value in updates.items():
            if isinstance(value, dict) and isinstance(base.get(key), dict):
                self._merge_ui_config(base[key], value)
            else:
                base[key] = value
        return base

    def _deep_merge_dict(self, base: dict, updates: dict) -> dict:
        """Deep-merge configuration dictionaries."""
        for key, value in updates.items():
            if isinstance(value, dict) and isinstance(base.get(key), dict):
                self._deep_merge_dict(base[key], value)
            else:
                base[key] = value
        return base

    def _load_ui_config(self) -> dict:
        """Load UI layout configuration from ui_config.json."""
        config = deepcopy(UI_CONFIG_DEFAULTS)
        try:
            if self.ui_config_path and os.path.exists(self.ui_config_path):
                with open(self.ui_config_path, "r", encoding="utf-8") as f:
                    loaded = json.load(f)
                    if isinstance(loaded, dict):
                        self._merge_ui_config(config, loaded)
        except Exception as e:
            print(f"Warning: Could not load UI config: {e}")
        return config

    def _save_config(self):
        """Save configuration to JSON file."""
        try:
            with open(self.config_path, "w", encoding="utf-8") as f:
                json.dump(self.config, f, indent=2)
        except Exception as e:
            self._log(f"Error saving config: {e}", "error")

    def _save_ui_config(self):
        """Save ui_config.json."""
        try:
            with open(self.ui_config_path, "w", encoding="utf-8") as f:
                json.dump(self.ui_config, f, indent=2)
        except Exception as e:
            self._log(f"Error saving ui_config: {e}", "error")

    def _persist_layout_corrections_if_needed(self):
        """Persist one-time startup layout sanitization."""
        if not self._layout_corrections_pending:
            return
        self._layout_corrections_pending = False
        self._save_config()
        self._save_ui_config()
        self._log("Layout auto-adjusted for current screen size", "info")

    def _load_history(self) -> List[dict]:
        """Load processed video history from disk."""
        try:
            if os.path.exists(self.history_path):
                with open(self.history_path, "r", encoding="utf-8") as f:
                    return json.load(f)
        except Exception as e:
            self._log(f"Could not load history: {e}", "warning")
        return []

    def _save_history(self):
        """Persist processed video history."""
        try:
            with open(self.history_path, "w", encoding="utf-8") as f:
                json.dump(self.history[-500:], f, indent=2)
        except Exception as e:
            self._log(f"Could not save history: {e}", "warning")

    def _setup_ui(self):
        """Set up the main UI layout with resizable panes."""
        # Style configuration
        style = ttk.Style()
        style.theme_use("clam")
        style.configure(
            "TCombobox",
            fieldbackground=COLORS["bg_input"],
            background=COLORS["bg_panel"],
            foreground=COLORS["text_light"],
            arrowcolor=COLORS["text_light"],
            selectbackground=COLORS["accent_blue"],
            selectforeground="white",
        )
        style.map(
            "TCombobox",
            fieldbackground=[("readonly", COLORS["bg_input"])],
            foreground=[("readonly", COLORS["text_light"])],
            selectbackground=[("readonly", COLORS["accent_blue"])],
            selectforeground=[("readonly", "white")],
        )
        # Style the dropdown listbox (popdown) for all Combobox widgets
        self.root.option_add("*TCombobox*Listbox.background", COLORS["bg_input"])
        self.root.option_add("*TCombobox*Listbox.foreground", COLORS["text_light"])
        self.root.option_add("*TCombobox*Listbox.selectBackground", COLORS["accent_blue"])
        self.root.option_add("*TCombobox*Listbox.selectForeground", "white")
        self.root.option_add("*Button.Background", COLORS["bg_input"])
        self.root.option_add("*Button.Foreground", BUTTON_TEXT_COLOR)
        self.root.option_add("*Button.ActiveBackground", COLORS["bg_hover"])
        self.root.option_add("*Button.ActiveForeground", BUTTON_TEXT_COLOR)
        self.root.option_add("*Button.DisabledForeground", BUTTON_DISABLED_TEXT_COLOR)

        # Dark theme for Treeview (PROCESSED VIDEOS section)
        style.configure(
            "Treeview",
            background=COLORS["bg_panel"],
            foreground=COLORS["text_light"],
            fieldbackground=COLORS["bg_panel"],
            borderwidth=0,
            font=(FONT_FAMILY, 8),
            rowheight=18,
        )
        style.configure(
            "Treeview.Heading",
            background=COLORS["bg_input"],
            foreground=COLORS["text_light"],
            borderwidth=1,
            font=(FONT_FAMILY, 8, "bold"),
        )
        style.map(
            "Treeview",
            background=[("selected", COLORS["accent_blue"])],
            foreground=[("selected", "white")],
        )

        # Dark theme for PanedWindow sash (drag handles)
        style.configure("TPanedwindow", background=COLORS["bg_main"])
        style.configure("Sash", sashthickness=6, sashrelief=tk.FLAT)

        # Dark theme for Notebook tabs
        style.configure(
            "TNotebook",
            background=COLORS["bg_main"],
            borderwidth=0,
        )
        style.configure(
            "TNotebook.Tab",
            background=COLORS["bg_input"],
            foreground=COLORS["text_dim"],
            padding=[10, 5],
            font=(FONT_FAMILY, 9, "bold"),
        )
        style.map(
            "TNotebook.Tab",
            background=[("selected", COLORS["bg_panel"])],
            foreground=[("selected", COLORS["text_light"])],
        )

        # Cross-platform dark ttk button styles.
        style.configure(
            TTK_BTN_SECONDARY,
            font=(FONT_FAMILY, 9, "bold"),
            foreground=COLORS["text_light"],
            background=COLORS["bg_input"],
            borderwidth=1,
            padding=(10, 6),
        )
        style.map(
            TTK_BTN_SECONDARY,
            background=[("active", COLORS["bg_hover"]), ("pressed", COLORS["bg_main"]), ("disabled", "#3A3A3A")],
            foreground=[("disabled", "#8C8C8C")],
        )
        style.configure(
            TTK_BTN_PRIMARY,
            font=(FONT_FAMILY, 9, "bold"),
            foreground="white",
            background=COLORS["accent_blue"],
            borderwidth=1,
            padding=(10, 6),
        )
        style.map(
            TTK_BTN_PRIMARY,
            background=[("active", "#7AA7FF"), ("pressed", "#4A79D8"), ("disabled", "#4B4B4B")],
            foreground=[("disabled", "#9D9D9D")],
        )
        style.configure(
            TTK_BTN_SUCCESS,
            font=(FONT_FAMILY, 9, "bold"),
            foreground="white",
            background=COLORS["btn_green"],
            borderwidth=1,
            padding=(10, 6),
        )
        style.map(
            TTK_BTN_SUCCESS,
            background=[("active", "#3CAD3C"), ("pressed", "#267826"), ("disabled", "#3E3E3E")],
            foreground=[("disabled", "#9D9D9D")],
        )
        style.configure(
            TTK_BTN_SUCCESS_COMPACT,
            font=(FONT_FAMILY, 8, "bold"),
            foreground="white",
            background=COLORS["btn_green"],
            borderwidth=1,
            padding=(7, 4),
        )
        style.map(
            TTK_BTN_SUCCESS_COMPACT,
            background=[("active", "#3CAD3C"), ("pressed", "#267826"), ("disabled", "#245E24")],
            foreground=[("disabled", "#D9F1D9")],
        )
        style.configure(
            TTK_BTN_DANGER,
            font=(FONT_FAMILY, 9, "bold"),
            foreground="white",
            background=COLORS["btn_red"],
            borderwidth=1,
            padding=(10, 6),
        )
        style.map(
            TTK_BTN_DANGER,
            background=[("active", "#C24444"), ("pressed", "#862525"), ("disabled", "#3E3E3E")],
            foreground=[("disabled", "#9D9D9D")],
        )
        style.configure(
            TTK_BTN_DANGER_COMPACT,
            font=(FONT_FAMILY, 8, "bold"),
            foreground="white",
            background=COLORS["btn_red"],
            borderwidth=1,
            padding=(7, 4),
        )
        style.map(
            TTK_BTN_DANGER_COMPACT,
            background=[("active", "#C24444"), ("pressed", "#862525"), ("disabled", "#7E2424")],
            foreground=[("disabled", "#F2D8D8")],
        )
        style.configure(
            TTK_BTN_COMPACT,
            font=(FONT_FAMILY, 8, "bold"),
            foreground=COLORS["text_light"],
            background=COLORS["bg_input"],
            borderwidth=1,
            padding=(8, 4),
        )
        style.map(
            TTK_BTN_COMPACT,
            background=[("active", COLORS["bg_hover"]), ("pressed", COLORS["bg_main"]), ("disabled", "#3A3A3A")],
            foreground=[("disabled", "#8C8C8C")],
        )
        style.configure(
            TTK_BTN_TAB_NAV,
            font=(FONT_FAMILY, 9, "bold"),
            foreground=COLORS["text_light"],
            background=COLORS["bg_input"],
            borderwidth=1,
            padding=(10, 6),
        )
        style.map(
            TTK_BTN_TAB_NAV,
            background=[("active", COLORS["bg_hover"]), ("pressed", COLORS["bg_main"]), ("disabled", "#3A3A3A")],
            foreground=[("disabled", "#8C8C8C")],
        )
        style.configure(
            "DropZone.TButton",
            font=(FONT_FAMILY, 9, "bold"),
            foreground="white",
            background="#6953C6",
            borderwidth=1,
            padding=(10, 6),
        )
        style.map(
            "DropZone.TButton",
            background=[("active", "#7A67D4"), ("pressed", "#523DA8"), ("disabled", "#45397C")],
            foreground=[("disabled", "#CCC7E9")],
        )

        # Header
        self._setup_header()

        # Control buttons MUST be packed before main_frame so the pack manager
        # reserves their space at the bottom first. If packed after an expand=True
        # frame, they get pushed off-screen when the window is small.
        self._setup_controls()

        # Main content area
        main_frame = tk.Frame(self.root, bg=COLORS["bg_main"])
        main_frame.pack(fill=tk.BOTH, expand=True, padx=10, pady=(0, 10))

        # Main vertical PanedWindow: top_section | bottom_section
        self.main_paned = tk.PanedWindow(
            main_frame,
            orient=tk.VERTICAL,
            bg=COLORS["bg_input"],
            sashwidth=6,
            sashrelief=tk.RAISED,
            sashpad=1,
        )
        self.main_paned.pack(fill=tk.BOTH, expand=True)

        # ── Top section: horizontal split (left: notebook | right: prompt) ──
        top_frame = tk.Frame(self.main_paned, bg=COLORS["bg_main"])
        self.main_paned.add(top_frame, minsize=280)

        self.top_h_paned = tk.PanedWindow(
            top_frame,
            orient=tk.HORIZONTAL,
            bg=COLORS["bg_input"],
            sashwidth=6,
            sashrelief=tk.RAISED,
            sashpad=1,
        )
        self.top_h_paned.pack(fill=tk.BOTH, expand=True)

        # Left pane: Notebook with 6 tabs
        left_pane = tk.Frame(self.top_h_paned, bg=COLORS["bg_main"])

        # Create Notebook FIRST (pipeline tabs do not depend on ConfigPanel)
        self.notebook = ttk.Notebook(left_pane)

        # Tab 0: Face Crop
        self.face_crop_tab = FaceCropTab(
            self.notebook,
            image_session=self.image_session,
            config=self.config,
            config_getter=lambda: self.config,
            log_callback=self._log,
            notebook_switcher_prep=lambda: self.notebook.select(1),
            notebook_switcher_selfie=lambda: self.notebook.select(2),
            config_saver=self._save_config,
        )
        self.notebook.add(self.face_crop_tab, text="0. Face Crop / AI Polish")

        # Tab 1: AI Analysis
        self.prep_tab = PrepTab(
            self.notebook,
            image_session=self.image_session,
            config=self.config,
            config_getter=lambda: self.config,
            log_callback=self._log,
            prompt_writer=self._write_to_active_prompt,
            config_saver=self._save_config,
        )
        self.notebook.add(self.prep_tab, text="1. AI Analysis")

        # Tab 2: Generate Selfie
        self.selfie_tab = SelfieTab(
            self.notebook,
            image_session=self.image_session,
            config=self.config,
            config_getter=lambda: self.config,
            log_callback=self._log,
            on_send_to_expand=self._on_selfie_send_to_expand,
            notebook_switcher_expand=lambda: self.notebook.select(3),
            config_saver=self._save_config,
        )
        self.notebook.add(self.selfie_tab, text="2. Generate Selfie")

        # Tab 2.5: Expand
        self.expand_tab = ExpandTab(
            self.notebook,
            image_session=self.image_session,
            config=self.config,
            config_getter=lambda: self.config,
            log_callback=self._log,
            on_send_to_video=self._on_files_dropped,
            notebook_switcher_video=lambda: self.notebook.select(4),
            config_saver=self._save_config,
        )
        self.notebook.add(self.expand_tab, text="2.5 Expand")

        # Wire Step 1 → Step 2 prompt connection (set after both tabs exist)
        self.prep_tab.set_selfie_prompt_writer(self.selfie_tab.set_prompt)
        self.prep_tab.set_notebook_switcher_selfie(lambda: self.notebook.select(2))
        self.prep_tab.set_selfie_config_getter(
            lambda: {
                "composer_gender": self.selfie_tab.gender_var.get(),
                "composer_camera_style": self.selfie_tab.style_var.get(),
            }
        )

        # Tab 3: Video — skeleton first, panels attached after creation
        self.video_tab = VideoTab(
            self.notebook,
            image_session=self.image_session,
            log_callback=self._log,
            on_files_dropped=self._on_files_dropped,
        )
        self.notebook.add(self.video_tab, text="3. Selfie Video Gen")

        # ConfigPanel as proper child of VideoTab (fixes cross-parent packing)
        self.config_panel = ConfigPanel(
            self.video_tab,
            config=self.config,
            on_config_changed=self._on_config_changed,
            build_prompt=False,
            on_oldcam_rerun=self._on_oldcam_rerun_requested,
        )

        self.video_tab.attach_config_panel(self.config_panel)

        self.notebook.pack(fill=tk.BOTH, expand=True)
        self.top_h_paned.add(left_pane, minsize=480)

        # Right pane: context-sensitive (tools on Tab 0, prompts on Tab 3, hidden on Tab 1-2)
        self._right_pane = tk.Frame(self.top_h_paned, bg=COLORS["bg_panel"])

        # Content A: Tab 0 tools panel (Polish + Outpaint + Upscale + Send)
        self._right_tools_frame = tk.Frame(self._right_pane, bg=COLORS["bg_panel"])
        self.face_crop_tab.build_tools_panel(self._right_tools_frame)

        # Content B: Prompt panel (Video tab)
        self._right_prompt_frame = tk.Frame(self._right_pane, bg=COLORS["bg_panel"])
        self.config_panel.build_prompt_panel(self._right_prompt_frame)

        # Show tools by default (Tab 0 is selected on launch)
        self._right_tools_frame.pack(fill=tk.BOTH, expand=True)

        self.top_h_paned.add(self._right_pane, minsize=260)

        # Tab change handler for context-sensitive right pane
        self.notebook.bind("<<NotebookTabChanged>>", self._on_tab_changed)

        # ── Bottom section: Horizontal PanedWindow (Carousel | Log+History) ──
        self.bottom_paned = tk.PanedWindow(
            self.main_paned,
            orient=tk.HORIZONTAL,
            bg=COLORS["bg_input"],
            sashwidth=6,
            sashrelief=tk.RAISED,
            sashpad=1,
        )
        self.main_paned.add(self.bottom_paned, minsize=250)

        # Carousel panel (replaces queue in bottom-left)
        carousel_frame = tk.Frame(self.bottom_paned, bg=COLORS["bg_panel"])
        self.carousel = ImageCarousel(
            carousel_frame,
            image_session=self.image_session,
            log_callback=self._log,
        )
        self.carousel.pack(fill=tk.BOTH, expand=True)
        self.carousel.set_on_compare(self._toggle_compare)
        self.bottom_paned.add(carousel_frame, minsize=340)

        # Compare panel state (created on demand by _toggle_compare)
        self._compare_frame: Optional[tk.Frame] = None
        self._compare_panel: Optional[ComparePanel] = None

        # Queue panel internals are kept for backend flow, but surface stays hidden in Step 3 UI.
        self._queue_panel_visible = False
        self.queue_frame = tk.Frame(self.video_tab, bg=COLORS["bg_panel"])
        self._setup_queue_panel_content(self.queue_frame)
        if self._queue_panel_visible:
            self.queue_frame.pack(fill=tk.X, padx=0, pady=(3, 0))

        # Right side: Vertical PanedWindow (Log | History)
        self.right_paned = tk.PanedWindow(
            self.bottom_paned,
            orient=tk.VERTICAL,
            bg=COLORS["bg_input"],
            sashwidth=6,
            sashrelief=tk.RAISED,
            sashpad=1,
        )
        self.bottom_paned.add(self.right_paned, minsize=200)

        # Log panel (top right pane): split into log + permanent drop zone
        log_frame = tk.Frame(self.right_paned, bg=COLORS["bg_main"])
        self.log_drop_paned = tk.PanedWindow(
            log_frame,
            orient=tk.HORIZONTAL,
            bg=COLORS["bg_input"],
            sashwidth=6,
            sashrelief=tk.RAISED,
            sashpad=1,
        )
        self.log_drop_paned.pack(fill=tk.BOTH, expand=True)

        log_panel = tk.Frame(self.log_drop_paned, bg=COLORS["bg_main"])
        self.log_display = LogDisplay(log_panel)
        self.log_display.pack(fill=tk.BOTH, expand=True)
        self.log_drop_paned.add(log_panel, minsize=220)

        self.drop_zone = DropZone(
            self.log_drop_paned,
            on_files_dropped=self._add_input_images_to_session,
            on_folder_dropped=None,
            compact=True,
            tint={
                "bg_drop": "#4C4566",
                "bg_hover": "#5D537D",
                "border": "#7464C0",
                "accent": "#8D7EE2",
                "text": COLORS["text_light"],
                "text_dim": "#C3BDE2",
                "drop_valid": "#6A58C6",
            },
        )
        self.log_drop_paned.add(self.drop_zone, minsize=220)
        self.right_paned.add(log_frame, minsize=100)

        # History panel (bottom right pane)
        self.history_frame = tk.Frame(self.right_paned, bg=COLORS["bg_panel"])
        self._setup_history_panel_content(self.history_frame)
        self.right_paned.add(self.history_frame, minsize=100)

        self._start_autosave_timer()

    def _write_to_active_prompt(self, text: str):
        """Write text to the active prompt slot (used by PrepTab vision analysis)."""
        if hasattr(self, "config_panel") and self.config_panel:
            self.config_panel.set_active_prompt_text(text)

    def _dbcmd(self, key: str, command, interval_ms: int = 180):
        """Return a command wrapped with click debounce protection."""
        return debounce_command(command=command, key=key, interval_ms=interval_ms)

    def _on_selfie_send_to_expand(self, paths: List[str], active_path: Optional[str] = None):
        """Handle Step 2 -> Step 2.5 handoff."""
        if not hasattr(self, "expand_tab") or self.expand_tab is None:
            self._log("Step 2.5 tab unavailable", "warning")
            return
        self.expand_tab.receive_from_step2(paths, active_path=active_path)
        try:
            self.notebook.select(3)
        except Exception:
            pass

    # ── Context-sensitive right pane ────────────────────────────────

    def _on_tab_changed(self, event=None):
        """Swap right pane content based on the active tab."""
        try:
            idx = self.notebook.index(self.notebook.select())
        except Exception:
            return

        if idx == 0:  # Tab 0: show tools panel
            self._show_right_pane(self._right_tools_frame)
        elif idx == 3:  # Tab 2.5 Expand
            self._hide_right_pane()
            try:
                self.expand_tab.refresh_from_active_carousel()
            except Exception:
                pass
        elif idx == 4:  # Tab 3 (Video): show prompt slots
            self._show_right_pane(self._right_prompt_frame)
        else:  # Tab 1, 2, 2.5: hide right pane entirely — tabs get full width
            self._hide_right_pane()

    def _show_right_pane(self, content_frame):
        """Show the right pane with the specified content."""
        self._right_tools_frame.pack_forget()
        self._right_prompt_frame.pack_forget()
        content_frame.pack(fill=tk.BOTH, expand=True)
        pane_names = [str(p) for p in self.top_h_paned.panes()]
        if str(self._right_pane) not in pane_names:
            self.top_h_paned.add(self._right_pane, minsize=260)
            saved = self.config.get("sash_prompt_split", 640)
            self.root.after(50, lambda: self._safe_sash_place(self.top_h_paned, 0, saved, 0))

    def _hide_right_pane(self):
        """Hide the right pane entirely — left tabs get full width."""
        pane_names = [str(p) for p in self.top_h_paned.panes()]
        if str(self._right_pane) in pane_names:
            try:
                self.config["sash_prompt_split"] = self.top_h_paned.sash_coord(0)[0]
            except Exception:
                pass
            self.top_h_paned.forget(self._right_pane)

    def _safe_sash_place(self, paned, index, x, y):
        """Place a sash position, silently ignoring errors."""
        try:
            paned.sash_place(index, x, y)
        except Exception:
            pass

    # ── Similarity Launcher ─────────────────────────────────────────

    def _resolve_similarity_dir(self) -> str:
        """Resolve the bundled standalone similarity app folder."""
        base_dirs = []
        for base in (get_app_dir(), get_resource_dir()):
            if base and base not in base_dirs:
                base_dirs.append(base)

        for base in base_dirs:
            candidate = os.path.join(base, "similarity")
            if os.path.isdir(candidate):
                return candidate

        fallback_base = base_dirs[0] if base_dirs else os.getcwd()
        return os.path.join(fallback_base, "similarity")

    @staticmethod
    def _similarity_launcher_name() -> str:
        """Return platform launcher for the standalone similarity GUI."""
        import platform

        system = platform.system()
        if system == "Windows":
            return "run_gui.bat"
        if system == "Darwin":
            return "run_gui.command"
        return "run_gui.command"

    def _close_similarity_launcher(self):
        """Close similarity launcher popup window if open."""
        if self._similarity_window is None:
            return
        win = self._similarity_window
        self._similarity_window = None
        try:
            if win.winfo_exists():
                win.destroy()
        except Exception:
            pass

    def _check_similarity_early_exit(
        self,
        process,
        launcher_name: str,
        runtime_log_path: str,
        crash_log_path: str,
        show_dialog: bool,
        launch_label: Optional[str] = None,
        final_check: bool = False,
    ) -> None:
        """Check whether launcher exited immediately without blocking the UI thread."""
        try:
            exit_code = process.poll()
        except Exception as exc:
            self._log(f"Similarity early-exit probe failed: {exc}", "warning")
            return

        if exit_code is None:
            return

        status = self._classify_similarity_runtime_log(runtime_log_path)
        if status == "success":
            self._log(
                f"Similarity launcher process ended (code={exit_code}) but app startup markers were detected. "
                f"Attempt: {launch_label or launcher_name}. Runtime log: {runtime_log_path}",
                "success",
            )
            return

        if not final_check:
            self.root.after(
                3000,
                lambda p=process, ln=launcher_name, rl=runtime_log_path, cl=crash_log_path, sd=show_dialog, ll=launch_label: self._check_similarity_early_exit(
                    p, ln, rl, cl, sd, ll, True
                ),
            )
            return

        launch_hint = f" Attempt: {launch_label}." if launch_label else ""
        status_hint = f" Runtime status: {status}." if status else ""
        msg = (
            f"Similarity launcher '{launcher_name}' exited immediately "
            f"(code={exit_code}).{launch_hint}{status_hint} See logs: {runtime_log_path} / {crash_log_path}"
        )
        self._log(msg, "error")
        if show_dialog:
            try:
                messagebox.showerror("Similarity Launch Failed", msg, parent=self.root)
            except Exception:
                pass

    @staticmethod
    def _read_similarity_runtime_log_tail(runtime_log_path: str, max_chars: int = 8000) -> str:
        """Read a tail slice of the runtime log file for status classification."""
        try:
            with open(runtime_log_path, "r", encoding="utf-8", errors="ignore") as f:
                data = f.read()
            return data[-max_chars:]
        except Exception:
            return ""

    @classmethod
    def _classify_similarity_runtime_log(cls, runtime_log_path: str) -> str:
        """Classify runtime log as success/failure/unknown."""
        tail = cls._read_similarity_runtime_log_tail(runtime_log_path)
        if not tail:
            return "unknown"

        success_markers = (
            "[INFO] Launching Face Similarity GUI...",
            "[INFO] Launching Face Similarity CLI...",
            "tensorflow/core",
        )
        failure_markers = (
            "[ERROR]",
            "Traceback",
            "No Python interpreter found",
            "Failed to",
        )

        has_success = any(marker in tail for marker in success_markers)
        has_failure = any(marker in tail for marker in failure_markers)
        if has_success and not has_failure:
            return "success"
        if has_failure:
            return "failure"
        return "unknown"

    @staticmethod
    def _similarity_fallback_commands(system: str) -> List[List[str]]:
        """Return fallback command candidates for launching similarity/main.py."""
        commands: List[List[str]] = []
        if system == "Windows":
            for version in ("3.12", "3.11", "3.10", "3.9"):
                commands.append(["py", f"-{version}", "main.py"])
        commands.append(["python", "main.py"])
        commands.append(["python3", "main.py"])
        return commands

    def _try_similarity_launch_attempt(
        self,
        label: str,
        args: List[str],
        similarity_dir: str,
        launch_env: dict,
        creationflags: int = 0,
    ):
        """Run one similarity launch attempt and return tuple(success, process_or_error)."""
        import subprocess

        kwargs = {"cwd": similarity_dir, "env": launch_env}
        if creationflags:
            kwargs["creationflags"] = creationflags
        try:
            process = subprocess.Popen(args, **kwargs)
            self._log(f"Similarity launch requested: cmd='{label}' cwd='{similarity_dir}' pid={process.pid}", "info")
            self._log(f"Launched Similarity app via {label}", "success")
            return True, process
        except Exception as exc:
            self._log(f"Similarity launch attempt failed ({label}): {exc}", "warning")
            return False, exc

    def _launch_similarity_gui(self, show_dialog: bool = True) -> bool:
        """Launch the standalone similarity GUI in a non-blocking subprocess."""
        import platform
        import subprocess

        similarity_dir = self._resolve_similarity_dir()
        launcher_name = self._similarity_launcher_name()
        launcher_path = os.path.join(similarity_dir, launcher_name)
        runtime_log_path = os.path.join(similarity_dir, "launcher_runtime.log")
        crash_log_path = os.path.join(similarity_dir, "crash.log")

        if not os.path.isdir(similarity_dir):
            msg = f"Similarity folder not found: {similarity_dir}"
            self._log(msg, "error")
            if show_dialog:
                messagebox.showerror("Similarity Launch Failed", msg, parent=self.root)
            return False

        system = platform.system()
        launch_env = os.environ.copy()
        launch_env["SIMILARITY_LAUNCHED_BY_MAIN"] = "1"
        launch_env["TF_USE_LEGACY_KERAS"] = "1"
        launch_env["KERAS_BACKEND"] = "tensorflow"
        attempts: List[tuple[str, List[str], int]] = []
        attempt_errors: List[str] = []

        if os.path.isfile(launcher_path):
            if system == "Windows":
                comspec = os.environ.get("ComSpec")
                if not comspec:
                    comspec = os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "System32", "cmd.exe")
                attempts.append(
                    (
                        f"{launcher_name} (via cmd.exe)",
                        [comspec, "/c", launcher_path],
                        getattr(subprocess, "CREATE_NEW_CONSOLE", 0),
                    )
                )
            elif system == "Darwin":
                attempts.append((f"/bin/bash {launcher_path}", ["/bin/bash", launcher_path], 0))
            else:
                attempts.append((launcher_name, [launcher_path], 0))
        else:
            missing_msg = f"Similarity launcher missing: {launcher_path}"
            self._log(missing_msg, "warning")
            attempt_errors.append(missing_msg)

        for cmd in self._similarity_fallback_commands(system):
            attempts.append((" ".join(cmd), cmd, 0))

        for label, cmd, creationflags in attempts:
            success, proc_or_exc = self._try_similarity_launch_attempt(
                label=label,
                args=cmd,
                similarity_dir=similarity_dir,
                launch_env=launch_env,
                creationflags=creationflags,
            )
            if success:
                process = proc_or_exc
                if system in {"Windows", "Darwin"} and process is not None:
                    self.root.after(
                        2500,
                        lambda p=process, ln=launcher_name, rl=runtime_log_path, cl=crash_log_path, sd=show_dialog, ll=label: self._check_similarity_early_exit(
                            p, ln, rl, cl, sd, ll, False
                        ),
                    )
                self._log(f"Launched Similarity app (runtime log: {runtime_log_path})", "success")
                return True
            attempt_errors.append(f"{label}: {proc_or_exc}")

        attempts_text = "\n".join(f"- {label}" for label, _, _ in attempts)
        errors_text = "\n".join(f"- {err}" for err in attempt_errors)
        msg = (
            "Could not launch Similarity app.\n"
            f"Attempts:\n{attempts_text}\n"
            f"Errors:\n{errors_text}\n"
            f"Expected runtime log: {runtime_log_path}\n"
            f"Expected crash log: {crash_log_path}"
        )
        self._log(msg, "error")
        if show_dialog:
            messagebox.showerror("Similarity Launch Failed", msg, parent=self.root)
        return False

    def _toggle_similarity_launcher(self):
        """Toggle the floating Similarity launcher popup and auto-run app."""
        now = time.monotonic()
        if self._similarity_window is not None:
            if now < self._similarity_open_guard_until:
                return
            self._close_similarity_launcher()
            return

        win = tk.Toplevel(self.root)
        win.title("Similarity Launcher")
        win.geometry("430x210")
        win.configure(bg=COLORS["bg_panel"])
        win.attributes("-topmost", True)
        win.resizable(False, False)

        card = tk.Frame(
            win,
            bg=COLORS["bg_drop"],
            highlightbackground=COLORS["border"],
            highlightthickness=2,
        )
        card.pack(fill=tk.BOTH, expand=True, padx=12, pady=12)

        tk.Label(
            card,
            text="Similarity",
            font=(FONT_FAMILY, 14, "bold"),
            bg=COLORS["bg_drop"],
            fg=COLORS["text_light"],
        ).pack(pady=(14, 4))

        tk.Label(
            card,
            text="Launching standalone similarity GUI from bundled folder.",
            font=(FONT_FAMILY, 9),
            bg=COLORS["bg_drop"],
            fg=COLORS["text_dim"],
        ).pack(pady=(0, 12))

        status_label = tk.Label(
            card,
            text="",
            font=(FONT_FAMILY, 9, "bold"),
            bg=COLORS["bg_drop"],
            fg=COLORS["text_light"],
        )
        status_label.pack(pady=(0, 8))

        button_row = tk.Frame(card, bg=COLORS["bg_drop"])
        button_row.pack(pady=(0, 12))

        create_action_button(
            button_row,
            text="Launch Again",
            command=lambda: self._launch_similarity_gui(show_dialog=True),
            style=TTK_BTN_PRIMARY,
            width=12,
        ).pack(side=tk.LEFT, padx=(0, 6))

        create_action_button(
            button_row,
            text="Open Folder",
            command=lambda: self._open_path_in_explorer(self._resolve_similarity_dir()),
            style=TTK_BTN_SECONDARY,
            width=11,
        ).pack(side=tk.LEFT, padx=(0, 6))

        create_action_button(
            button_row,
            text="Close",
            command=self._close_similarity_launcher,
            style=TTK_BTN_SECONDARY,
            width=9,
        ).pack(side=tk.LEFT)

        launched = self._launch_similarity_gui(show_dialog=False)
        if launched:
            status_label.config(
                text="Similarity app launch requested.",
                fg=COLORS.get("success", "#50C878"),
            )
        else:
            status_label.config(
                text="Launch failed. Use Launch Again for details.",
                fg=COLORS.get("warning", "#FFA500"),
            )

        self._similarity_open_guard_until = time.monotonic() + 0.55
        win.protocol("WM_DELETE_WINDOW", self._close_similarity_launcher)
        win.bind(
            "<Destroy>",
            lambda event, this_win=win: setattr(self, "_similarity_window", None)
            if event.widget is this_win
            else None,
            add="+",
        )
        self._similarity_window = win

    # ── Floating Drop Zone ──────────────────────────────────────────

    def _close_drop_zone(self):
        """Close floating drop zone window if open."""
        if self._drop_zone_window is None:
            return
        win = self._drop_zone_window
        self._drop_zone_window = None
        try:
            if win.winfo_exists():
                win.destroy()
        except Exception:
            pass

    def _toggle_drop_zone(self):
        """Toggle the floating drop zone window."""
        now = time.monotonic()
        if self._drop_zone_window is not None:
            # Guard against immediate re-entry from the same click/release sequence.
            if now < self._drop_zone_open_guard_until:
                return
            self._close_drop_zone()
            return

        win = tk.Toplevel(self.root)
        win.title("Drop Images Here")
        win.geometry("360x260")
        win.configure(bg=COLORS["bg_panel"])
        win.attributes("-topmost", True)
        win.resizable(True, True)

        bg = COLORS["bg_drop"]
        hover_bg = COLORS.get("bg_hover", "#505055")

        # Drop area with highlight border
        drop_frame = tk.Frame(
            win,
            bg=bg,
            highlightbackground=COLORS["border"],
            highlightthickness=2,
        )
        drop_frame.pack(fill=tk.BOTH, expand=True, padx=10, pady=10)

        # Centered content container
        content = tk.Frame(drop_frame, bg=bg)
        content.place(relx=0.5, rely=0.5, anchor="center")

        # Icon
        icon_label = tk.Label(
            content,
            text="\U0001F4E5",
            font=(EMOJI_FONT_FAMILY, 32),
            bg=bg,
            fg=COLORS["accent_blue"],
        )
        icon_label.pack(pady=(0, 6))

        # Main text
        main_label = tk.Label(
            content,
            text="DRAG & DROP IMAGES",
            font=(FONT_FAMILY, 12, "bold"),
            bg=bg,
            fg=COLORS["text_light"],
        )
        main_label.pack(pady=2)

        # Sub-instruction
        sub_label = tk.Label(
            content,
            text="Left click to select  |  Right click to drag",
            font=(FONT_FAMILY, 9),
            bg=bg,
            fg=COLORS["text_dim"],
        )
        sub_label.pack(pady=(0, 8))

        # Status label for drop feedback
        status_label = tk.Label(
            drop_frame,
            text="",
            font=(FONT_FAMILY, 9, "bold"),
            bg=bg,
            fg=COLORS["text_light"],
        )
        status_label.pack(side=tk.BOTTOM, pady=8)
        self._drop_zone_status = status_label

        # All interactive widgets
        widgets = [drop_frame, content, icon_label, main_label, sub_label, status_label]

        # Hover effects
        def _set_bg(color):
            for w in widgets:
                w.config(bg=color)

        def _on_enter(event):
            _set_bg(hover_bg)
            drop_frame.config(highlightbackground=COLORS["accent_blue"])

        def _on_leave(event):
            # Only reset if truly left the drop_frame
            rx = event.x_root - drop_frame.winfo_rootx()
            ry = event.y_root - drop_frame.winfo_rooty()
            if 0 <= rx <= drop_frame.winfo_width() and 0 <= ry <= drop_frame.winfo_height():
                return
            _set_bg(bg)
            drop_frame.config(highlightbackground=COLORS["border"])

        # Make draggable via right-click
        def _start_drag(event):
            win._drag_x = event.x
            win._drag_y = event.y

        def _do_drag(event):
            x = win.winfo_x() + event.x - win._drag_x
            y = win.winfo_y() + event.y - win._drag_y
            win.geometry(f"+{x}+{y}")

        for w in widgets:
            w.bind("<Button-1>", lambda e: self._browse_and_add_images())
            w.bind("<ButtonPress-3>", _start_drag)
            w.bind("<B3-Motion>", _do_drag)
            w.bind("<Enter>", _on_enter)
            w.bind("<Leave>", _on_leave)
            w.config(cursor="hand2")

        # Try to bind DnD if available
        if HAS_DND and DND_FILES:
            try:
                for w in [drop_frame, icon_label, main_label, sub_label, content]:
                    w.drop_target_register(DND_FILES)
                    w.dnd_bind("<<DropEnter>>", lambda e: (
                        _set_bg(COLORS.get("drop_valid", "#329632")),
                        status_label.config(text="DROP TO ADD"),
                    ))
                    w.dnd_bind("<<DropLeave>>", lambda e: (
                        _set_bg(bg),
                        status_label.config(text=""),
                    ))
                    w.dnd_bind("<<Drop>>", self._on_drop_zone_drop)
            except Exception as exc:
                status_label.config(text="Drag-and-drop unavailable", fg=COLORS.get("warning", "#FFA500"))
                self._log(f"Floating drop-zone DnD bind failed: {exc}", "warning")

        self._drop_zone_open_guard_until = time.monotonic() + 0.55
        win.protocol("WM_DELETE_WINDOW", self._close_drop_zone)
        win.bind(
            "<Destroy>",
            lambda event, this_win=win: setattr(self, "_drop_zone_window", None)
            if event.widget is this_win
            else None,
            add="+",
        )
        self._drop_zone_window = win

    def _on_drop_zone_drop(self, event):
        """Handle files dropped onto the floating drop zone."""
        data = event.data
        if not data:
            return
        splitlist_fn = None
        try:
            splitlist_fn = self.root.tk.splitlist
        except Exception:
            splitlist_fn = None
        files = parse_dnd_paths(data, splitlist_fn=splitlist_fn, require_exists=True)

        added = self._add_input_images_to_session(files)

        # Show status on floating drop zone
        if hasattr(self, "_drop_zone_status") and self._drop_zone_status:
            status = self._drop_zone_status
            if added:
                status.config(text=f"Added {added} file(s)", fg=COLORS.get("success", "#50C878"))
            else:
                status.config(text="No valid images found", fg=COLORS.get("warning", "#FFA500"))
            self.root.after(2000, lambda s=status: s.config(text="") if s.winfo_exists() else None)

    def _browse_and_add_images(self):
        """Open file dialog and add selected images to carousel."""
        files = select_open_files(
            title="Select Images to Add",
            filetypes=[
                ("Image files", "*.jpg *.jpeg *.png *.webp *.bmp *.gif *.tiff *.tif"),
                ("All files", "*.*"),
            ],
        )
        if files:
            self._add_input_images_to_session(files)

    def _apply_ui_config(self):
        """Apply UI layout configuration to existing widgets."""
        if not hasattr(self, "root"):
            return

        sanitized_window, sanitized_geometry, changed = sanitize_window_layout(
            window_config=self.ui_config.get("window", {}),
            saved_geometry=self.config.get("window_geometry", ""),
            screen_width=self.root.winfo_screenwidth(),
            screen_height=self.root.winfo_screenheight(),
        )
        self.ui_config["window"] = sanitized_window
        self.config["window_geometry"] = sanitized_geometry
        if changed:
            self._layout_corrections_pending = True

        min_width = sanitized_window["min_width"]
        min_height = sanitized_window["min_height"]

        try:
            self.root.minsize(min_width, min_height)
        except Exception:
            pass

        if hasattr(self, "config_panel") and hasattr(self.config_panel, "apply_ui_config"):
            self.config_panel.apply_ui_config(self.ui_config)

        # Only configure history tree row count here.
        # Sash positions are handled exclusively by _restore_sash_positions()
        # which runs after this method to avoid conflicts.
        try:
            visible_rows = int(
                self.ui_config.get("history_panel", {}).get("visible_rows", 10)
            )
        except (TypeError, ValueError):
            visible_rows = 10

        if not getattr(self, "_queue_panel_visible", True):
            visible_rows = max(10, visible_rows)

        if hasattr(self, "history_tree"):
            try:
                self.history_tree.config(height=max(1, visible_rows))
            except Exception:
                pass

    def _setup_debug_hotkeys(self):
        """Bind debug hotkeys and inspector, when enabled in ui_config.json."""
        debug_config = self.ui_config.get("debug", {})
        self._debug_enabled = bool(debug_config.get("enabled", False))
        if not self._debug_enabled:
            return

        inspector_hotkey = debug_config.get("inspector_hotkey", "F12")
        reload_hotkey = debug_config.get("reload_hotkey", "F5")

        try:
            # Unbind existing hotkeys to prevent duplication on reload
            self.root.unbind(f"<{inspector_hotkey}>")
            self.root.unbind(f"<{reload_hotkey}>")
            self.root.unbind("<Control-e>")
            self.root.unbind("<Control-s>")
            # Note: bind_all can't be easily unbound without tracking, 
            # but Button-3 with add="+" won't cause issues
            
            # Rebind hotkeys
            self.root.bind(
                f"<{inspector_hotkey}>", lambda e: self._dump_widget_tree()
            )
            self.root.bind(f"<{reload_hotkey}>", lambda e: self._hot_reload_ui())
            self.root.bind_all("<Button-3>", self._inspect_widget, add="+")
            self.root.bind("<Control-e>", lambda e: self._toggle_edit_mode())
            self.root.bind("<Control-s>", lambda e: self._export_current_layout())
        except Exception:
            pass

        try:
            if hasattr(self, "main_paned"):
                self.main_paned.bind("<B1-Motion>", self._on_sash_drag, add="+")
            if hasattr(self, "bottom_paned"):
                self.bottom_paned.bind("<B1-Motion>", self._on_sash_drag, add="+")
            if hasattr(self, "right_paned"):
                self.right_paned.bind("<B1-Motion>", self._on_sash_drag, add="+")
            if hasattr(self, "log_drop_paned"):
                self.log_drop_paned.bind("<B1-Motion>", self._on_sash_drag, add="+")
        except Exception:
            pass

        self._log(
            "UI debug hotkeys enabled (F12 tree, F5 reload, Ctrl+E edit, Ctrl+S export)",
            "info",
        )

    def _dump_widget_tree(self, widget=None, indent=0):
        """Print widget hierarchy with sizes."""
        if not getattr(self, "_debug_enabled", False):
            return
        if widget is None:
            widget = self.root
            print("\n=== WIDGET TREE ===")

        name = str(widget)
        size = f"{widget.winfo_width()}x{widget.winfo_height()}"
        req = f"{widget.winfo_reqwidth()}x{widget.winfo_reqheight()}"
        manager = widget.winfo_manager()
        print(
            "  " * indent
            + f"{widget.__class__.__name__} {name} - {size} (req {req}) [{manager}]"
        )

        try:
            if manager == "pack":
                print("  " * (indent + 1) + f"pack: {widget.pack_info()}")
            elif manager == "grid":
                print("  " * (indent + 1) + f"grid: {widget.grid_info()}")
            elif manager == "place":
                print("  " * (indent + 1) + f"place: {widget.place_info()}")
        except Exception:
            pass

        for child in widget.winfo_children():
            self._dump_widget_tree(child, indent + 1)

    def _inspect_widget(self, event):
        """Right-click inspector."""
        if not getattr(self, "_debug_enabled", False):
            return

        w = event.widget
        self._flash_widget_border(w)
        widget_path = self._get_widget_path(w)

        info_lines = [
            f"Path: {widget_path}",
            f"Class: {w.__class__.__name__} {str(w)}",
            f"Size: {w.winfo_width()} x {w.winfo_height()} (req {w.winfo_reqwidth()} x {w.winfo_reqheight()})",
            f"Position: ({w.winfo_x()}, {w.winfo_y()}) in parent",
            f"Screen: ({w.winfo_rootx()}, {w.winfo_rooty()})",
            f"Manager: {w.winfo_manager()}",
        ]

        try:
            manager = w.winfo_manager()
            if manager == "pack":
                info_lines.append(f"Pack: {w.pack_info()}")
            elif manager == "grid":
                info_lines.append(f"Grid: {w.grid_info()}")
            elif manager == "place":
                info_lines.append(f"Place: {w.place_info()}")
        except Exception:
            pass

        print("\n=== WIDGET INSPECTOR ===")
        for line in info_lines:
            print(line)

        self._show_inspector_popup(info_lines, event.x_root, event.y_root)

    def _get_widget_path(self, widget) -> str:
        """Get full widget hierarchy path."""
        path = []
        current = widget
        while current is not None:
            name = current.__class__.__name__
            if hasattr(current, "_name"):
                name = f"{name}({current._name})"
            path.insert(0, name)
            current = current.master if hasattr(current, "master") else None
        return " -> ".join(path)

    def _flash_widget_border(self, widget):
        """Flash a red border around a widget to highlight it."""
        try:
            original_thickness = widget.cget("highlightthickness")
            original_color = widget.cget("highlightbackground")
            widget.config(highlightthickness=3, highlightbackground="#FF4040")

            def restore():
                widget.config(
                    highlightthickness=original_thickness,
                    highlightbackground=original_color,
                )

            self.root.after(400, restore)
        except Exception:
            pass

    def _toggle_edit_mode(self):
        """Ctrl+E: Toggle layout edit mode."""
        self.edit_mode = not self.edit_mode
        if self.edit_mode:
            self._log("LAYOUT EDIT MODE - Drag panels, Ctrl+S to export", "warning")
            self._show_all_dimensions()
        else:
            self._log("Edit mode disabled", "info")
            self._hide_dimension_overlays()

    def _show_all_dimensions(self):
        """Show dimension overlays on major widgets."""
        self._hide_dimension_overlays()
        widgets_to_track = [
            ("Window", self.root),
            ("Config Panel", self.config_panel if hasattr(self, "config_panel") else None),
            ("Drop Zone", self.drop_zone if hasattr(self, "drop_zone") else None),
            ("Queue Panel", self.queue_frame if hasattr(self, "queue_frame") else None),
            ("Log Display", self.log_display if hasattr(self, "log_display") else None),
            (
                "History Panel",
                self.history_frame if hasattr(self, "history_frame") else None,
            ),
        ]

        print("\n" + "=" * 50)
        print("CURRENT LAYOUT DIMENSIONS")
        print("=" * 50)

        for name, widget in widgets_to_track:
            if not widget:
                continue
            width = widget.winfo_width()
            height = widget.winfo_height()
            print(f"{name:20} {width:4} x {height:4}")

            label = tk.Label(
                widget,
                text=f"{width}x{height}",
                font=(FONT_FAMILY, 9, "bold"),
                bg="#E53935",
                fg="white",
                padx=4,
                pady=2,
            )
            label.place(relx=1.0, rely=0.0, anchor="ne")
            self.dimension_labels[name] = label

        self._print_sash_positions()
        print("=" * 50 + "\n")

    def _hide_dimension_overlays(self):
        """Remove all dimension overlay labels."""
        for label in self.dimension_labels.values():
            try:
                label.destroy()
            except Exception:
                pass
        self.dimension_labels.clear()

    def _on_sash_drag(self, event):
        """Update dimension display during sash drag."""
        if not self.edit_mode:
            return

        if getattr(self, "_sash_update_pending", False):
            return

        self._sash_update_pending = True

        def update():
            self._print_sash_positions()
            self._update_dimension_overlays()
            self._sash_update_pending = False

        self.root.after(100, update)

    def _update_dimension_overlays(self):
        """Refresh overlay labels with current sizes."""
        if not self.edit_mode:
            return

        mapping = {
            "Window": self.root,
            "Config Panel": getattr(self, "config_panel", None),
            "Drop Zone": getattr(self, "drop_zone", None),
            "Queue Panel": getattr(self, "queue_frame", None),
            "Log Display": getattr(self, "log_display", None),
            "History Panel": getattr(self, "history_frame", None),
        }

        for name, widget in mapping.items():
            if not widget:
                continue
            label = self.dimension_labels.get(name)
            if not label:
                continue
            label.config(text=f"{widget.winfo_width()}x{widget.winfo_height()}")

    def _print_sash_positions(self):
        """Print current sash positions."""
        positions = {}
        try:
            if hasattr(self, "main_paned"):
                positions["dropzone_sash"] = self.main_paned.sash_coord(0)
        except Exception:
            pass

        try:
            if hasattr(self, "bottom_paned"):
                positions["queue_sash"] = self.bottom_paned.sash_coord(0)
        except Exception:
            pass

        try:
            if hasattr(self, "right_paned"):
                positions["log_sash"] = self.right_paned.sash_coord(0)
        except Exception:
            pass

        try:
            if hasattr(self, "log_drop_paned"):
                positions["log_drop_sash"] = self.log_drop_paned.sash_coord(0)
        except Exception:
            pass

        if positions:
            self._show_sash_toast(positions)

    def _show_sash_toast(self, positions: dict):
        """Show a short-lived overlay with current sash positions."""
        if not self.edit_mode:
            return

        try:
            if hasattr(self, "_sash_toast") and self._sash_toast.winfo_exists():
                self._sash_toast.destroy()
        except Exception:
            pass

        try:
            popup = tk.Toplevel(self.root)
            popup.overrideredirect(True)
            popup.attributes("-topmost", True)
            try:
                popup.attributes("-alpha", 0.85)
            except Exception:
                pass

            text = (
                f"Drop: {positions.get('dropzone_sash', '')}  |  "
                f"Queue: {positions.get('queue_sash', '')}  |  "
                f"Log: {positions.get('log_sash', '')}  |  "
                f"Log/Drop: {positions.get('log_drop_sash', '')}"
            )

            frame = tk.Frame(popup, bg="#202225", bd=1, relief=tk.SOLID)
            frame.pack(fill=tk.BOTH, expand=True)

            label = tk.Label(
                frame,
                text=text,
                font=("Consolas", 9, "bold"),
                bg="#202225",
                fg="#F2F2F2",
                padx=10,
                pady=6,
            )
            label.pack()

            self.root.update_idletasks()
            popup.update_idletasks()

            root_x = self.root.winfo_rootx()
            root_y = self.root.winfo_rooty()
            root_w = self.root.winfo_width()
            root_h = self.root.winfo_height()
            toast_w = popup.winfo_reqwidth()
            toast_h = popup.winfo_reqheight()

            margin = 12
            x = root_x + max(0, root_w - toast_w - margin)
            y = root_y + max(0, root_h - toast_h - margin)
            popup.geometry(f"+{x}+{y}")

            popup.after(1000, popup.destroy)
            self._sash_toast = popup
        except Exception:
            pass

    def _export_current_layout(self):
        """Ctrl+S: Export current layout to JSON."""
        try:
            layout = {
                "_comment": "Exported layout - copy values to ui_config.json",
                "window": {
                    "width": self.root.winfo_width(),
                    "height": self.root.winfo_height(),
                    "min_width": self.root.winfo_width(),
                    "min_height": self.root.winfo_height(),
                },
                "config_panel": {
                    "prompt_preview_height": (
                        int(self.config_panel.prompt_preview.cget("height"))
                        if hasattr(self, "config_panel")
                        and hasattr(self.config_panel, "prompt_preview")
                        else 0
                    ),
                    "prompt_preview_font_size": 9,
                    "negative_prompt_height": 1,
                    "prompt_preview_width": (
                        self.config_panel.prompt_preview_container.winfo_width()
                        if hasattr(self, "config_panel")
                        and hasattr(self.config_panel, "prompt_preview_container")
                        else 0
                    ),
                    "prompt_preview_right_pad": 0,
                    "prompt_preview_offset_x": 16,
                },
                "drop_zone": {
                    "height": self.drop_zone.winfo_height()
                    if hasattr(self, "drop_zone")
                    else 0
                },
                "queue_panel": {
                    "width": self.queue_frame.winfo_width()
                    if hasattr(self, "queue_frame")
                    else 0
                },
                "history_panel": {
                    "height": self.history_frame.winfo_height()
                    if hasattr(self, "history_frame")
                    else 0,
                    "visible_rows": (
                        int(self.history_tree.cget("height"))
                        if hasattr(self, "history_tree")
                        else 0
                    ),
                },
                "sash_positions": {},
            }

            try:
                if hasattr(self, "main_paned"):
                    layout["sash_positions"]["dropzone"] = self.main_paned.sash_coord(0)
            except Exception:
                pass

            try:
                if hasattr(self, "bottom_paned"):
                    layout["sash_positions"]["queue"] = self.bottom_paned.sash_coord(0)
            except Exception:
                pass

            try:
                if hasattr(self, "right_paned"):
                    layout["sash_positions"]["log"] = self.right_paned.sash_coord(0)
            except Exception:
                pass

            try:
                if hasattr(self, "log_drop_paned"):
                    layout["sash_positions"]["log_drop"] = self.log_drop_paned.sash_coord(0)
            except Exception:
                pass

            export_path = Path("ui_config_exported.json")
            with open(export_path, "w", encoding="utf-8") as f:
                json.dump(layout, f, indent=2)

            self._log(
                f"Layout exported to {export_path.name}",
                "success",
            )
            print(f"\nExported to: {export_path.absolute()}")
            print(json.dumps(layout, indent=2))
        except Exception as e:
            self._log(f"Export failed: {e}", "error")

    def _show_inspector_popup(self, lines: List[str], x: int, y: int):
        """Show an on-screen tooltip with widget details."""
        try:
            if hasattr(self, "_inspector_popup") and self._inspector_popup.winfo_exists():
                self._inspector_popup.destroy()
        except Exception:
            pass

        try:
            popup = tk.Toplevel(self.root)
            popup.overrideredirect(True)
            popup.attributes("-topmost", True)

            frame = tk.Frame(popup, bg=COLORS["bg_panel"], bd=1, relief=tk.SOLID)
            frame.pack(fill=tk.BOTH, expand=True)

            for line in lines:
                tk.Label(
                    frame,
                    text=line,
                    font=("Consolas", 8),
                    bg=COLORS["bg_panel"],
                    fg=COLORS["text_light"],
                    anchor="w",
                ).pack(fill=tk.X, padx=6, pady=1)

            popup.geometry(f"+{x + 10}+{y + 10}")
            popup.after(2000, popup.destroy)
            self._inspector_popup = popup
        except Exception:
            pass

    def _hot_reload_ui(self):
        """F5 to reload UI config."""
        self.ui_config = self._load_ui_config()
        self._apply_ui_config()
        self._setup_debug_hotkeys()
        self._log("UI reloaded from ui_config.json", "success")

    def _setup_header(self):
        """Set up the header bar (title only — status indicators are in the control bar)."""
        header = tk.Frame(self.root, bg=COLORS["bg_panel"], height=40)
        header.pack(fill=tk.X, padx=10, pady=(8, 4))
        header.pack_propagate(False)

        title = tk.Label(
            header,
            text="Ultimate-Selfie-Gen",
            font=(FONT_FAMILY, 11, "bold"),
            bg=COLORS["bg_panel"],
            fg=COLORS["text_light"],
        )
        title.pack(side=tk.LEFT, padx=10, pady=6)

        # Session management buttons
        sessions_btn = create_action_button(
            header,
            text="Sessions",
            command=self._dbcmd("header_sessions", self._on_open_sessions),
            style=TTK_BTN_SECONDARY,
            width=12,
        )
        sessions_btn.pack(side=tk.RIGHT, padx=(0, 6), pady=4)

        save_session_btn = create_action_button(
            header,
            text="Save Session",
            command=self._dbcmd("header_save_session", self._on_save_session),
            style=TTK_BTN_SUCCESS,
        )
        save_session_btn.pack(side=tk.RIGHT, padx=(0, 6), pady=4)

        new_session_btn = create_action_button(
            header,
            text="New Session",
            command=self._dbcmd("header_new_session", self._on_new_session),
            style=TTK_BTN_SECONDARY,
        )
        new_session_btn.pack(side=tk.RIGHT, padx=(0, 6), pady=4)

        sanitize_folder_btn = create_action_button(
            header,
            text="Sanitize Folder",
            command=self._dbcmd("header_sanitize_folder", self._on_sanitize_folder_clicked),
            style=TTK_BTN_SECONDARY,
        )
        sanitize_folder_btn.pack(side=tk.RIGHT, padx=(0, 6), pady=4)

        # Similarity launcher popup toggle
        similarity_btn = create_action_button(
            header,
            text="Similarity",
            command=self._dbcmd("header_toggle_similarity", self._toggle_similarity_launcher),
            style=TTK_BTN_PRIMARY,
            width=12,
        )
        similarity_btn.pack(side=tk.RIGHT, padx=(0, 6), pady=4)

        # Floating drop zone toggle
        self._drop_zone_window = None
        drop_zone_btn = create_action_button(
            header,
            text="Drop Zone",
            command=self._dbcmd("header_toggle_drop_zone", self._toggle_drop_zone),
            style="DropZone.TButton",
            width=12,
        )
        drop_zone_btn.pack(side=tk.RIGHT, padx=(0, 6), pady=4)

    # -- API key badge helpers ------------------------------------------------

    def _create_api_badge(self, parent, config_key: str, label: str, prompt_text: str):
        """Create a single API key badge with colored border, stored in _api_badges."""
        is_set = key_status(self.config, config_key) == "added"
        border_color = COLORS["success"] if is_set else COLORS["error"]

        frame = tk.Frame(parent, bg=border_color, padx=2, pady=2)
        frame.pack(side=tk.LEFT, padx=(0, 6))

        indicator = tk.Label(
            frame,
            text=f"{label}: Added" if is_set else f"{label}: Missing",
            font=(FONT_FAMILY, 7, "bold"),
            bg=COLORS["bg_input"],
            fg=COLORS["text_light"],
            padx=5, pady=2,
            cursor="hand2",
        )
        indicator.pack()
        indicator.bind(
            "<Button-1>",
            lambda e, k=config_key, lb=label, pt=prompt_text: self._prompt_key(k, lb, pt),
        )

        self._api_badges[config_key] = {"frame": frame, "label_widget": indicator, "label": label}

    def _prompt_key(self, config_key: str, label: str, prompt_text: str):
        """Generic dialog to set/update any API key."""
        current = self.config.get(config_key, "")
        new_key = simpledialog.askstring(
            f"{label} API Key",
            prompt_text,
            initialvalue=current,
            parent=self.root,
        )
        if new_key is None:
            return  # User cancelled
        new_key = new_key.strip()

        self.config[config_key] = new_key
        self._save_config()
        self._update_api_badge(config_key)

        # fal.ai key changes need generator re-init
        if config_key == "falai_api_key":
            self._init_generator()

        if new_key:
            self._log(f"{label} key updated and saved.", "success")
        else:
            self._log(f"{label} key cleared.", "warning")

    def _update_api_badge(self, config_key: str):
        """Refresh border color and text for one API key badge."""
        badge = self._api_badges.get(config_key)
        if not badge:
            return
        value = self.config.get(config_key, "")
        is_set = bool(value and value.strip())
        border_color = COLORS["success"] if is_set else COLORS["error"]
        badge["frame"].config(bg=border_color)
        lbl = badge["label"]
        badge["label_widget"].config(
            text=f"{lbl}: Added" if is_set else f"{lbl}: Missing",
        )

    def _setup_queue_panel_content(self, queue_frame):
        """Set up the queue panel content inside the given frame."""
        # Header with count
        self.queue_header = tk.Label(
            queue_frame,
            text="📋 QUEUE (0/50)",
            font=(FONT_FAMILY, 9, "bold"),
            bg=COLORS["bg_panel"],
            fg=COLORS["text_light"],
        )
        self.queue_header.pack(fill=tk.X, padx=5, pady=(5, 2))

        # Queue listbox with scrollbar
        list_frame = tk.Frame(queue_frame, bg=COLORS["bg_main"])
        list_frame.pack(fill=tk.BOTH, expand=True, padx=5, pady=5)

        scrollbar = ttk.Scrollbar(list_frame)
        scrollbar.pack(side=tk.RIGHT, fill=tk.Y)

        self.queue_listbox = tk.Listbox(
            list_frame,
            bg=COLORS["bg_main"],
            fg=COLORS["text_light"],
            font=("Consolas", 8),
            selectbackground=COLORS["accent_blue"],
            selectforeground="white",
            yscrollcommand=scrollbar.set,
            borderwidth=0,
            highlightthickness=0,
        )
        self.queue_listbox.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        scrollbar.config(command=self.queue_listbox.yview)

        # Context menu for queue
        self.queue_menu = tk.Menu(self.queue_listbox, tearoff=0)
        self.queue_menu.add_command(label="Remove", command=self._remove_selected_item)
        self.queue_listbox.bind("<Button-3>", self._show_queue_menu)

    def _setup_history_panel_content(self, panel):
        """Processed videos history content inside the given frame."""
        header = tk.Frame(panel, bg=COLORS["bg_panel"])
        header.pack(fill=tk.X, padx=5, pady=(4, 2))

        tk.Label(
            header,
            text="🎞️ PROCESSED VIDEOS",
            font=(FONT_FAMILY, 9, "bold"),
            bg=COLORS["bg_panel"],
            fg=COLORS["text_light"],
        ).pack(side=tk.LEFT)

        btn_frame = tk.Frame(header, bg=COLORS["bg_panel"])
        btn_frame.pack(side=tk.RIGHT)

        ttk.Button(
            btn_frame,
            text="Open File",
            style=TTK_BTN_SECONDARY,
            command=self._dbcmd("history_open_file", self._open_selected_file),
        ).pack(side=tk.LEFT, padx=2)
        ttk.Button(
            btn_frame,
            text="Open Folder",
            style=TTK_BTN_SECONDARY,
            command=self._dbcmd("history_open_folder", self._open_selected_folder),
        ).pack(side=tk.LEFT, padx=2)
        ttk.Button(
            btn_frame,
            text="Refresh",
            style=TTK_BTN_SECONDARY,
            command=self._dbcmd("history_refresh", self._refresh_history_view),
        ).pack(side=tk.LEFT, padx=2)

        columns = ("time", "source", "output", "status")
        self.history_tree = ttk.Treeview(
            panel, columns=columns, show="headings", height=8, selectmode="browse"
        )
        for col, text, width in [
            ("time", "Time", 110),
            ("source", "Source", 180),
            ("output", "Output", 280),
            ("status", "Status", 90),
        ]:
            self.history_tree.heading(col, text=text)
            self.history_tree.column(col, width=width, anchor=tk.W)

        scrollbar = ttk.Scrollbar(
            panel, orient="vertical", command=self.history_tree.yview
        )
        self.history_tree.configure(yscrollcommand=scrollbar.set)

        self.history_tree.pack(
            side=tk.LEFT, fill=tk.BOTH, expand=True, padx=(5, 0), pady=(0, 5)
        )
        scrollbar.pack(side=tk.RIGHT, fill=tk.Y, padx=(0, 5), pady=(0, 5))

        self.history_tree.bind("<Double-1>", lambda e: self._open_selected_file())

        self._refresh_history_view()

    def _setup_controls(self):
        """Set up the control buttons."""
        control_frame = tk.Frame(self.root, bg=COLORS["bg_main"])
        # Use side=tk.BOTTOM to ensure buttons appear even with expandable main_frame
        control_frame.pack(side=tk.BOTTOM, fill=tk.X, padx=10, pady=(0, 10))

        # Left side: Add file button (fallback if DnD unavailable)
        if not HAS_DND:
            add_btn = create_action_button(
                control_frame,
                text="📁 Add...",
                command=self._dbcmd("controls_add_files", self._browse_files),
                style=TTK_BTN_SUCCESS,
            )
            add_btn.pack(side=tk.LEFT, padx=5)

        # Left side: Unified API key badges — click to set each key
        self._api_badges = {}
        keys_config = [
            (
                spec.config_key,
                spec.label,
                f"{spec.instruction}\n{spec.url}\n(leave blank to clear key)",
            )
            for spec in API_KEY_SPECS
        ]
        for config_key, label, prompt_text in keys_config:
            self._create_api_badge(control_frame, config_key, label, prompt_text)

        dnd_status = "✓ Drag-Drop Enabled" if HAS_DND else "⚠ Drag-Drop Unavailable"
        dnd_color = COLORS["success"] if HAS_DND else COLORS["warning"]
        tk.Label(
            control_frame, text=dnd_status,
            font=(FONT_FAMILY, 8), bg=COLORS["bg_main"], fg=dnd_color,
        ).pack(side=tk.LEFT)

        # Right side: Control buttons (flat styling, always visible via side=BOTTOM)
        self.close_btn = create_action_button(
            control_frame,
            text="Close",
            command=self._dbcmd("controls_close", self._on_close),
            style=TTK_BTN_DANGER,
        )
        self.close_btn.pack(side=tk.RIGHT, padx=4)

        self.clear_btn = create_action_button(
            control_frame,
            text="Clear",
            command=self._dbcmd("controls_clear", self._clear_queue),
            style=TTK_BTN_SECONDARY,
        )
        self.clear_btn.pack(side=tk.RIGHT, padx=4)

        self.retry_btn = create_action_button(
            control_frame,
            text="Retry Failed",
            command=self._dbcmd("controls_retry_failed", self._retry_failed),
            style=TTK_BTN_SECONDARY,
        )
        self.retry_btn.pack(side=tk.RIGHT, padx=4)

        self.pause_btn = create_action_button(
            control_frame,
            text="Pause",
            command=self._dbcmd("controls_pause", self._toggle_pause),
            style=TTK_BTN_TAB_NAV,
        )
        self.pause_btn.pack(side=tk.RIGHT, padx=4)
        self._set_queue_controls_enabled(False)

    def _set_queue_controls_enabled(self, enabled: bool):
        """Enable or disable queue control buttons without removing them from UI."""
        state = tk.NORMAL if enabled else tk.DISABLED
        for btn_name in ("pause_btn", "retry_btn", "clear_btn"):
            btn = getattr(self, btn_name, None)
            if btn is not None:
                try:
                    btn.config(state=state)
                except Exception:
                    pass

    def _init_generator(self):
        """Initialize the video generator and queue manager."""
        if not HAS_GENERATOR:
            self._log(
                "Generator not available - check kling_generator_falai.py", "error"
            )
            return

        api_key = self.config.get("falai_api_key", "")
        if not api_key:
            self._log("No API key configured - set it in the main app first", "warning")
            return

        try:
            self.generator = FalAIKlingGenerator(
                api_key=api_key,
                verbose=self.config.get("verbose_logging", True),
                model_endpoint=self.config.get("current_model"),
                model_display_name=self.config.get("model_display_name"),
                prompt_slot=self.config.get("current_prompt_slot", 1),
                freeimage_key=self.config.get("freeimage_api_key", ""),
            )

            self.queue_manager = QueueManager(
                generator=self.generator,
                config_getter=lambda: self.config,
                log_callback=self._log_thread_safe,
                queue_update_callback=self._update_queue_display_thread_safe,
                processing_complete_callback=self._on_item_complete,
            )

            self._log("Generator initialized successfully", "success")

        except Exception as e:
            self._log(f"Failed to initialize generator: {e}", "error")

    def _prompt_startup_provider_keys_on_first_run(self):
        """First-launch key onboarding (Fal.ai + BFL), never exits app."""
        prompt_specs = startup_prompt_specs()
        missing = missing_startup_specs(self.config)
        if not missing:
            return
        status_text = "\n".join(
            f"- {line}"
            for line in startup_status_lines(self.config)
        )
        links_text = "\n".join(f"- {spec.label}: {spec.url}" for spec in prompt_specs)
        messagebox.showinfo(
            "First Launch: API Key Setup",
            "Key status:\n"
            f"{status_text}\n\n"
            "Fal.ai and BFL keys can be added now or later.\n"
            "If skipped, key-required features will show targeted errors when used.\n\n"
            "Where to get keys:\n"
            f"{links_text}",
            parent=self.root,
        )

        for spec in missing:
            new_key = simpledialog.askstring(
                f"{spec.label} API Key",
                f"Enter your {spec.label} API key:\n{spec.url}\n\nCancel or leave blank to skip for now.",
                parent=self.root,
            )
            if new_key is None:
                self._log(f"{spec.label} API key setup skipped.", "warning")
                continue
            new_key = new_key.strip()
            if not new_key:
                self._log(f"{spec.label} API key setup skipped.", "warning")
                continue
            self.config[spec.config_key] = new_key
            self._save_config()
            self._update_api_badge(spec.config_key)
            self._log(f"{spec.label} API key saved.", "success")

        optional_missing = list(non_required_missing_specs(self.config))
        if optional_missing:
            optional_text = "\n".join(f"- {spec.label}: {spec.url}" for spec in optional_missing)
            self._log("Optional provider keys are missing; related features will remain disabled.", "warning")
            messagebox.showinfo(
                "Optional Keys Missing",
                "You can still use core flows.\n\nMissing optional keys:\n"
                f"{optional_text}\n\n"
                "Click key badges at the bottom bar to add them anytime.",
                parent=self.root,
            )

    def _log(self, message: str, level: str = "info"):
        """Log a message to the log display."""
        if hasattr(self, "log_display"):
            self.log_display.log(message, level)
        if self.logger:
            level_map = {
                "info": self.logger.info,
                "success": self.logger.info,
                "warning": self.logger.warning,
                "error": self.logger.error,
            }
            level_map.get(level, self.logger.info)(message)

    def _log_thread_safe(self, message: str, level: str = "info"):
        """Thread-safe logging using after()."""
        try:
            if self.root.winfo_exists():
                self.root.after(0, lambda: self._log(message, level))
        except tk.TclError:
            if self.logger:
                self.logger.warning("Dropped GUI log after window closed: %s", message)

    def _update_queue_display(self):
        """Update the queue listbox display."""
        if not hasattr(self, "queue_listbox") or not hasattr(self, "queue_header"):
            return
        self.queue_listbox.delete(0, tk.END)

        if self.queue_manager:
            items = self.queue_manager.get_items()
            stats = self.queue_manager.get_stats()

            # Update header
            self.queue_header.config(
                text=f"📋 QUEUE ({stats['pending'] + stats['processing']}/50)"
            )

            # Add items to listbox
            for item in items:
                status_icon = {
                    "pending": "⏳",
                    "processing": "🔄",
                    "completed": "✅",
                    "failed": "❌",
                }.get(item.status, "?")

                display = f"{status_icon} {item.filename}"
                if item.status == "failed":
                    display += " [RETRY]"

                self.queue_listbox.insert(tk.END, display)

    def _update_queue_display_thread_safe(self):
        """Thread-safe queue display update."""
        self.root.after(0, self._update_queue_display)

    def _refresh_history_view(self):
        """Reload history tree from stored list."""
        if not hasattr(self, "history_tree"):
            return
        self.history_tree.delete(*self.history_tree.get_children())
        for entry in reversed(self.history[-200:]):  # show recent first
            status = entry.get("status", "")
            tag = (
                "success"
                if status == "completed"
                else ("error" if status == "failed" else "")
            )
            self.history_tree.insert(
                "",
                tk.END,
                values=(
                    entry.get("time", ""),
                    os.path.basename(entry.get("source", "")),
                    entry.get("output", ""),
                    status,
                ),
                tags=(tag,),
            )
        # color tags
        self.history_tree.tag_configure("success", foreground=COLORS["success"])
        self.history_tree.tag_configure("error", foreground=COLORS["error"])

    def _get_selected_history(self) -> Optional[dict]:
        if not hasattr(self, "history_tree"):
            return None
        sel = self.history_tree.selection()
        if not sel:
            return None
        # Tree shows reversed order; map index
        index = self.history_tree.index(sel[0])
        # reversed list so map back
        try:
            entry = list(reversed(self.history[-200:]))[index]
            return entry
        except Exception:
            return None

    def _get_latest_completed_history(self) -> Optional[dict]:
        """Return latest completed history entry with an output path."""
        for entry in reversed(self.history):
            if entry.get("status") == "completed" and entry.get("output"):
                return entry
        return None

    def _resolve_oldcam_rerun_source(self, output_path: str) -> str:
        """Resolve Oldcam rerun source video from history output path."""
        candidate = Path(str(output_path or "")).expanduser()
        if not candidate:
            return ""
        stem = candidate.stem
        match = re.search(r"-oldcam-v\d+$", stem, re.IGNORECASE)
        if match:
            base = candidate.with_name(f"{stem[:match.start()]}{candidate.suffix}")
            if base.exists():
                return str(base)
            self._log(
                f"Base video not found for {candidate.name}; using selected output as source",
                "warning",
            )
            return str(candidate)
        return str(candidate)

    def _on_oldcam_rerun_requested(self):
        """Handle Oldcam-only rerun button from config panel."""
        if not self.queue_manager:
            self._log("Queue manager not initialized", "error")
            return

        selected = self._get_selected_history()
        history_entry = None
        if selected and selected.get("output"):
            history_entry = selected
        else:
            history_entry = self._get_latest_completed_history()

        if not history_entry:
            self._log("No completed video available for Oldcam rerun", "warning")
            return

        chosen_output = str(history_entry.get("output") or "").strip()
        if not chosen_output:
            self._log("No output video path available for Oldcam rerun", "warning")
            return

        source_video = self._resolve_oldcam_rerun_source(chosen_output)
        if not source_video or not os.path.isfile(source_video):
            self._log(f"Oldcam rerun source not found: {source_video or chosen_output}", "warning")
            return

        started = self.queue_manager.rerun_oldcam_only(
            source_video,
            completion_callback=self._on_oldcam_rerun_complete_threadsafe,
        )
        if started:
            self._log(
                f"Oldcam-only rerun queued: {os.path.basename(source_video)} "
                f"({self.config.get('oldcam_version', 'v7')})",
                "info",
            )

    def _on_oldcam_rerun_complete_threadsafe(
        self,
        success: bool,
        source_video: str,
        output_path: Optional[str],
        error: Optional[str],
    ):
        """Thread-safe bridge from queue manager Oldcam rerun callback."""
        try:
            if self.root.winfo_exists():
                self.root.after(
                    0,
                    lambda: self._record_oldcam_rerun_result(
                        success, source_video, output_path, error
                    ),
                )
        except tk.TclError:
            if self.logger:
                self.logger.warning("Dropped oldcam rerun completion after window closed")

    def _record_oldcam_rerun_result(
        self,
        success: bool,
        source_video: str,
        output_path: Optional[str],
        error: Optional[str],
    ):
        """Persist Oldcam-only rerun result in history + UI log."""
        status = "completed" if success else "failed"
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        entry = {
            "time": timestamp,
            "source": source_video,
            "output": output_path or "",
            "status": status,
            "error": error or "",
        }
        self.history.append(entry)
        self.history = self.history[-500:]
        self._save_history()
        self._refresh_history_view()

        if success and output_path:
            self._log(
                f"Oldcam-only rerun complete: {os.path.basename(source_video)} → {output_path}",
                "success",
            )
        else:
            self._log(
                f"Oldcam-only rerun failed for {os.path.basename(source_video)}: {error or 'unknown error'}",
                "error",
            )

    def _open_path_in_explorer(self, path: str):
        """Open a file or folder in the system's native file explorer.

        Uses platform-specific methods for reliable local file/folder opening.
        webbrowser.open() is unreliable for local paths on some systems.
        """
        import platform
        import subprocess

        system = platform.system()
        try:
            if system == "Windows":
                os.startfile(path)
            elif system == "Darwin":  # macOS
                subprocess.run(["open", path], check=True)
            else:  # Linux and others
                subprocess.run(["xdg-open", path], check=True)
        except Exception as e:
            self._log(f"Could not open {path}: {e}", "error")

    def _open_selected_file(self):
        entry = self._get_selected_history()
        path = entry.get("output") if entry else None
        if path and os.path.exists(path):
            self._open_path_in_explorer(path)
        elif entry and entry.get("output"):
            self._log(f"File not found: {entry['output']}", "warning")

    def _open_selected_folder(self):
        entry = self._get_selected_history()
        path = None
        if entry:
            path = entry.get("output") or entry.get("source")
        if path:
            folder = os.path.dirname(path)
            if folder and os.path.exists(folder):
                self._open_path_in_explorer(folder)
                return
        self._log("No folder to open for selection", "warning")

    def _toggle_compare(self):
        """Open or close the Compare panel beside the carousel."""
        if self._compare_panel is not None:
            # Close compare mode
            try:
                self.bottom_paned.forget(self._compare_frame)
            except tk.TclError:
                pass
            if self._compare_panel is not None:
                self._compare_panel.destroy()
            self._compare_panel = None
            self._compare_frame = None
        else:
            # Open compare mode
            self._compare_frame = tk.Frame(self.bottom_paned, bg=COLORS["bg_panel"])
            self._compare_panel = ComparePanel(
                self._compare_frame,
                image_session=self.image_session,
                log_callback=self._log,
                on_close=self._toggle_compare,
            )
            self._compare_panel.pack(fill=tk.BOTH, expand=True)
            # Insert between carousel_frame and right_paned
            self.bottom_paned.add(
                self._compare_frame, minsize=200, before=self.right_paned
            )
            # Set sash positions to give compare panel generous width
            def _set_compare_sash():
                try:
                    total_w = self.bottom_paned.winfo_width()
                    carousel_w = 350
                    compare_w = 350
                    right_w = total_w - carousel_w - compare_w
                    if right_w < 200:
                        right_w = 200
                        compare_w = total_w - carousel_w - right_w
                    self.bottom_paned.sash_place(0, carousel_w, 0)
                    self.bottom_paned.sash_place(1, carousel_w + compare_w, 0)
                except Exception:
                    pass
            self.root.after(50, _set_compare_sash)

    def _on_images_to_carousel(self, files: List[str]):
        """Handle images dropped/browsed in the prompt panel mini drop zone."""
        self._add_input_images_to_session(files)

    @staticmethod
    def _is_front_image(path: str) -> bool:
        """Return True if the filename appears to be a front-image input."""
        return "front" in os.path.basename(path).lower()

    def _refresh_session_dependent_ui(self):
        """Refresh tab UI fragments that depend on image session state."""
        if hasattr(self, "selfie_tab") and self.selfie_tab:
            try:
                self.selfie_tab._refresh_result_actions()
            except Exception:
                pass
        if hasattr(self, "expand_tab") and self.expand_tab:
            try:
                self.expand_tab.refresh_candidates(select_all_default=True)
            except Exception:
                pass

    def _clear_working_session(self, label: str = "new session"):
        """Clear current working session and refresh dependent UI."""
        self.image_session.clear()
        self._refresh_session_dependent_ui()
        self._log(f"Started {label}", "success")

    def _save_current_session_snapshot(self) -> bool:
        """Save a manual snapshot of current session, returning success flag."""
        return self.session_controller.save_current_session_snapshot()

    def _offer_save_and_start_new_for_front(self, front_path: str) -> bool:
        """Offer to save current session and start a new one for a front image."""
        if self.image_session.count == 0:
            return True
        folder_name = os.path.basename(os.path.dirname(front_path)) or "untitled"
        choice = messagebox.askyesnocancel(
            "New Front Image Detected",
            (
                f"Detected a new front image from folder:\n{folder_name}\n\n"
                "Save current session and start a new session?"
            ),
            parent=self.root,
        )
        if choice is None:
            self._log("Image add cancelled", "info")
            return False
        if not choice:
            return True
        if not self._save_current_session_snapshot():
            proceed = messagebox.askyesno(
                "Save Failed",
                "Current session could not be saved.\nStart a new session anyway?",
                parent=self.root,
            )
            if not proceed:
                return False
        self._clear_working_session(label=f"new session for {folder_name}")
        return True

    def _add_input_images_to_session(self, files: List[str]) -> int:
        """Add input images to working session with front-image session rollover prompt."""
        valid_files: List[str] = []
        for path in files:
            ok, reason = preflight_image_path(path, allowed_exts=VALID_EXTENSIONS)
            if not ok:
                self._log(
                    f"Skipped carousel add: {os.path.basename(path)} ({reason})",
                    "warning",
                )
                self.logger.error(
                    "Carousel add preflight failed path=%s reason=%s",
                    path,
                    reason,
                )
                continue
            valid_files.append(path)
        if not valid_files:
            return 0

        renamed_count = 0
        sanitized_files: List[str] = []
        for path in valid_files:
            try:
                new_path, changed = sanitize_path_name(path)
                if changed:
                    renamed_count += 1
                    self._log(
                        f"Renamed source for cross-platform safety: "
                        f"{os.path.basename(path)} -> {os.path.basename(new_path)}",
                        "warning",
                    )
                sanitized_files.append(new_path)
            except Exception as exc:
                self._log(f"Could not sanitize source path {path}: {exc}", "error")
                sanitized_files.append(path)
        valid_files = sanitized_files
        if renamed_count:
            self._log(f"Sanitized {renamed_count} input source filename(s)", "info")

        front_candidate = next((p for p in valid_files if self._is_front_image(p)), None)
        if front_candidate and self.image_session.count > 0:
            if not self._offer_save_and_start_new_for_front(front_candidate):
                return 0

        added = 0
        for path in valid_files:
            self.image_session.add_image(path, "input")
            self._log(f"Added to carousel session: {os.path.basename(path)}", "info")
            added += 1
        return added

    def _on_files_dropped(self, files: List[str]):
        """Handle files dropped onto the drop zone."""
        if not self.queue_manager:
            self._log("Queue manager not initialized", "error")
            return

        added = 0
        for file_path in files:
            try:
                original_path = file_path
                file_path, changed = sanitize_path_name(file_path)
                if changed:
                    self._log(
                        f"Renamed source for cross-platform safety: "
                        f"{os.path.basename(original_path)} -> {os.path.basename(file_path)}",
                        "warning",
                    )
            except Exception as exc:
                self._log(f"Could not sanitize source path {file_path}: {exc}", "error")
            success, message = self.queue_manager.add_to_queue(file_path)
            if success:
                added += 1
            else:
                self._log(
                    f"Skipped: {os.path.basename(file_path)} - {message}", "warning"
                )

        if added > 0:
            self._log(f"Added {added} file(s) to queue", "success")

    def _describe_sanitize_reason(self, reason: str) -> str:
        """Translate sanitize reason keys into user-facing text."""
        labels = {
            "invalid_characters": "invalid characters",
            "control_whitespace": "control whitespace",
            "edge_spaces_or_dots": "edge spaces/dots",
            "repeated_underscores": "repeated underscores",
            "trailing_spaces_or_dots": "trailing spaces/dots",
            "windows_reserved_name": "Windows reserved name",
            "length_limit": "name too long",
            "normalized": "normalized",
        }
        parts = [labels.get(part.strip(), part.strip()) for part in (reason or "").split(",")]
        parts = [part for part in parts if part]
        return ", ".join(parts) if parts else "normalized"

    def _sanitize_folder_with_manifest(self, folder_path: str):
        """Sanitize one folder tree and always emit a manifest report."""
        requested_folder = folder_path
        sanitized_folder, renames, failures, changes = sanitize_tree_names_portable_report(
            folder_path, rename_root=True
        )
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        manifest_name = f"sanitize_manifest_{stamp}.json"

        manifest = {
            "generated_at": datetime.now().isoformat(timespec="seconds"),
            "requested_folder": requested_folder,
            "sanitized_root": sanitized_folder,
            "change_count": len(renames),
            "failure_count": len(failures),
            "changes": changes,
            "failures": failures,
        }
        write_roots = [sanitized_folder, requested_folder, get_user_data_dir()]
        manifest_path = ""
        last_error = None
        for root in write_roots:
            if not root:
                continue
            try:
                os.makedirs(root, exist_ok=True)
                candidate = os.path.join(root, manifest_name)
                with open(candidate, "w", encoding="utf-8") as handle:
                    json.dump(manifest, handle, indent=2, ensure_ascii=False)
                manifest_path = candidate
                break
            except OSError as exc:
                last_error = exc
                continue

        if not manifest_path:
            raise OSError(
                f"Could not write sanitize manifest to any target root: "
                f"{[p for p in write_roots if p]}"
            ) from last_error

        return sanitized_folder, renames, failures, changes, manifest_path

    def _on_sanitize_folder_clicked(self):
        """Manually sanitize a folder tree and show a concise report."""
        folder = select_directory(title="Select Folder to Sanitize")
        if not folder:
            return
        try:
            sanitized_folder, renames, failures, changes, manifest_path = (
                self._sanitize_folder_with_manifest(folder)
            )
            if renames:
                self._log(
                    f"Sanitize Folder complete: renamed {len(renames)} item(s), skipped {len(failures)} item(s)",
                    "success" if not failures else "warning",
                )
                for change in changes[:25]:
                    old_name = change.get("old_name", "")
                    new_name = change.get("new_name", "")
                    reason = self._describe_sanitize_reason(change.get("reason", ""))
                    self._log(
                        f"Renamed: {old_name} -> {new_name} (reason: {reason})",
                        "info",
                    )
                if len(changes) > 25:
                    self._log(f"...and {len(changes) - 25} more rename(s)", "info")
            else:
                self._log(
                    "Sanitize Folder complete: no cross-platform rename needed",
                    "success",
                )

            if failures:
                self._log(
                    f"Skipped {len(failures)} locked/inaccessible item(s); remaining items still processed",
                    "warning",
                )
                for failed in failures[:25]:
                    self._log(
                        f"Skipped: {os.path.basename(failed.get('path', ''))} - "
                        f"{failed.get('error_type', 'OSError')}: {failed.get('error_message', '')}",
                        "warning",
                    )
                if len(failures) > 25:
                    self._log(f"...and {len(failures) - 25} more skip(s)", "warning")

            self._log(f"Sanitize manifest written: {manifest_path}", "info")
        except Exception as exc:
            self._log(f"Sanitize Folder failed: {exc}", "error")

    def _on_folder_dropped(self, folder_path: str):
        """Handle folder dropped onto the drop zone."""
        if not self.queue_manager:
            self._log("Queue manager not initialized", "error")
            return

        try:
            sanitized_folder, renames, failures, changes, manifest_path = (
                self._sanitize_folder_with_manifest(folder_path)
            )
            if renames:
                self._log(
                    f"Sanitized {len(renames)} file/folder name(s) for cross-platform safety",
                    "warning",
                )
                for change in changes[:25]:
                    old_name = change.get("old_name", "")
                    new_name = change.get("new_name", "")
                    reason = self._describe_sanitize_reason(change.get("reason", ""))
                    self._log(
                        f"Renamed: {old_name} -> {new_name} (reason: {reason})",
                        "info",
                    )
                if len(changes) > 25:
                    self._log(f"...and {len(changes) - 25} more rename(s)", "info")
            if failures:
                self._log(
                    f"Skipped {len(failures)} locked/inaccessible item(s) during sanitize",
                    "warning",
                )
                for failed in failures[:25]:
                    self._log(
                        f"Skipped: {os.path.basename(failed.get('path', ''))} - "
                        f"{failed.get('error_type', 'OSError')}: {failed.get('error_message', '')}",
                        "warning",
                    )
                if len(failures) > 25:
                    self._log(f"...and {len(failures) - 25} more skip(s)", "warning")
            self._log(f"Sanitize manifest written: {manifest_path}", "info")
            folder_path = sanitized_folder
        except Exception as exc:
            self._log(f"Folder name sanitization failed: {exc}", "error")

        pattern = self.config.get("folder_filter_pattern", "").strip()
        match_mode = self.config.get("folder_match_mode", "partial")

        # Require pattern for folder processing - prompt if missing
        if not pattern:
            self._log("Folder dropped but no filter pattern set", "warning")
            pattern = simpledialog.askstring(
                "Pattern Required",
                "Enter a filename pattern to filter images (e.g. 'selfie'):",
                parent=self.root,
            )

            if not pattern:
                self._log("Folder processing cancelled: No pattern provided", "info")
                return

            # Update config and UI
            pattern = pattern.strip()
            self.config["folder_filter_pattern"] = pattern
            if hasattr(self, "config_panel"):
                self.config_panel.folder_pattern_var.set(pattern)
            self._save_config()

        # Scan for matching files
        self._log(f"Scanning folder: {os.path.basename(folder_path)}", "info")
        matches = self._scan_folder_for_images(folder_path, pattern, match_mode)

        if not matches:
            mode_text = "exactly matching" if match_mode == "exact" else "containing"
            self._log(f"No images found {mode_text} '{pattern}'", "warning")
            messagebox.showinfo(
                "No Matches",
                f"No images found {mode_text} '{pattern}' in:\n{folder_path}\n\n"
                f"Pattern mode: {match_mode}\n"
                f"Searched recursively through all subfolders.",
            )
            return

        # Show preview dialog
        self._show_folder_preview_dialog(matches, folder_path, pattern, match_mode)

    def _scan_folder_for_images(
        self, folder_path: str, pattern: str, match_mode: str
    ) -> List[str]:
        """
        Recursively scan folder for images matching pattern.

        Args:
            folder_path: Root folder to scan
            pattern: Filename pattern (case-insensitive)
            match_mode: "exact" or "partial"

        Returns:
            List of matching image file paths
        """
        matches = []
        pattern_lower = pattern.lower()

        try:
            for root, dirs, files in os.walk(folder_path):
                for filename in files:
                    name, ext = os.path.splitext(filename)
                    ext_lower = ext.lower()

                    # Check if valid image extension
                    if ext_lower not in VALID_EXTENSIONS:
                        continue

                    name_lower = name.lower()

                    # Apply matching logic
                    if match_mode == "exact":
                        if name_lower == pattern_lower:
                            matches.append(os.path.join(root, filename))
                    else:  # partial
                        if pattern_lower in name_lower:
                            matches.append(os.path.join(root, filename))
        except PermissionError as e:
            self._log(f"Permission denied accessing some folders: {e}", "warning")
        except Exception as e:
            self._log(f"Error scanning folder: {e}", "error")

        return sorted(matches)

    def _show_folder_preview_dialog(
        self, files: List[str], folder: str, pattern: str, match_mode: str
    ):
        """Show preview dialog and add files to queue if confirmed."""
        dialog = FolderPreviewDialog(self.root, files, folder, pattern, match_mode)

        if dialog.result:
            if self.queue_manager:
                # Add all files to queue
                added = 0
                skipped = 0
                for file_path in files:
                    success, msg = self.queue_manager.add_to_queue(file_path)
                    if success:
                        added += 1
                    else:
                        skipped += 1

                self._log(f"Added {added} images from folder to queue", "success")
                if skipped > 0:
                    self._log(
                        f"Skipped {skipped} (already in queue or duplicates)", "info"
                    )
            else:
                # Warn user that files weren't added
                self._log(
                    "Folder preview confirmed, but items were not added because the "
                    "queue/generator is not initialized yet.",
                    "warning",
                )

    def _on_config_changed(
        self, new_config: dict, change_description: Optional[str] = None
    ):
        """Handle configuration changes from the config panel."""
        self.config.update(new_config)
        # Collect and merge any tab-specific config
        for tab in ["face_crop_tab", "prep_tab", "selfie_tab", "expand_tab"]:
            tab_widget = getattr(self, tab, None)
            if tab_widget and hasattr(tab_widget, "get_config_updates"):
                try:
                    self.config.update(tab_widget.get_config_updates())
                except Exception as exc:
                    logging.getLogger(__name__).warning(
                        "Failed to get config from %s: %s", tab, exc
                    )
        self._save_config()

        # Update generator with new model if it exists and NOT currently processing
        # This prevents race conditions where settings change mid-generation
        if self.generator:
            if self.queue_manager and self.queue_manager.is_running:
                # Processing is active - don't update generator mid-run
                # Changes will apply to the next job (config is already saved)
                msg = (
                    f"{change_description} (will apply to next job)"
                    if change_description
                    else "Settings changed (will apply to next job)"
                )
                self._log(msg, "warning")
                return
            else:
                # Safe to update generator
                self.generator.update_model(
                    str(self.config.get("current_model", "")),
                    str(self.config.get("model_display_name", "")),
                )
                self.generator.update_prompt_slot(
                    int(self.config.get("current_prompt_slot", 1))
                )
                self.generator.update_freeimage_key(
                    str(self.config.get("freeimage_api_key", ""))
                )

        # Log the specific change if description provided
        if change_description:
            self._log(change_description, "info")

    def _on_item_complete(self, item: QueueItem):
        """Called when an item finishes processing."""
        status = item.status
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        entry = {
            "time": timestamp,
            "source": item.path,
            "output": item.output_path or "",
            "status": status,
            "error": item.error_message or "",
        }
        self.history.append(entry)
        # Keep history reasonably sized
        self.history = self.history[-500:]
        self._save_history()
        self._refresh_history_view()

        if status == "completed" and item.output_path:
            self._log(
                f"Finished {os.path.basename(item.path)} → {item.output_path}",
                "success",
            )

        # Sync generator with latest config when queue becomes empty
        # This ensures any settings changed during processing take effect for next run
        if self.queue_manager and not self.queue_manager.is_running and self.generator:
            self.generator.update_model(
                str(self.config.get("current_model", "")),
                str(self.config.get("model_display_name", "")),
            )
            self.generator.update_prompt_slot(
                int(self.config.get("current_prompt_slot", 1))
            )
            self.generator.update_freeimage_key(
                str(self.config.get("freeimage_api_key", ""))
            )

    def _toggle_pause(self):
        """Toggle pause/resume."""
        if not self.queue_manager:
            return

        if self.queue_manager.is_paused:
            self.queue_manager.resume_processing()
            self.pause_btn.config(text="Pause")
        else:
            self.queue_manager.pause_processing()
            self.pause_btn.config(text="Resume")

    def _retry_failed(self):
        """Retry all failed items."""
        if self.queue_manager:
            count = self.queue_manager.retry_failed()
            if count == 0:
                self._log("No failed items to retry", "info")

    def _clear_queue(self):
        """Clear the queue."""
        if self.queue_manager:
            self.queue_manager.clear_queue()

    def _remove_selected_item(self):
        """Remove the selected item from the queue."""
        if not hasattr(self, "queue_listbox"):
            return
        selection = self.queue_listbox.curselection()
        if selection and self.queue_manager:
            self.queue_manager.remove_item(selection[0])

    def _show_queue_menu(self, event):
        """Show context menu for queue item."""
        if not hasattr(self, "queue_listbox") or not hasattr(self, "queue_menu"):
            return
        try:
            self.queue_listbox.selection_clear(0, tk.END)
            self.queue_listbox.selection_set(self.queue_listbox.nearest(event.y))
            self.queue_menu.tk_popup(event.x_root, event.y_root)
        finally:
            self.queue_menu.grab_release()

    def _browse_files(self):
        """Open chooser to add files or a folder (fallback for no DnD)."""
        choice = messagebox.askyesnocancel(
            "Add Items", "Add a folder?\n\nYes = Folder\nNo = Files"
        )
        if choice is None:
            return

        if choice:
            folder = select_directory(title="Select Folder to Process")
            if folder:
                self._on_folder_dropped(folder)
            return

        files = select_open_files(
            title="Select Images",
            filetypes=[
                ("Image files", "*.jpg *.jpeg *.png *.webp *.bmp *.gif *.tiff *.tif"),
                ("All files", "*.*"),
            ],
        )
        if files:
            self._on_files_dropped(files)

    def _restore_sash_positions(self):
        """Restore saved sash positions for all PanedWindows."""
        try:
            # Ensure all widgets are rendered before placing sashes
            self.root.update_idletasks()

            sash_values, changed = sanitize_sash_layout(
                sash_dropzone=self.config.get("sash_dropzone", 500),
                sash_prompt_split=self.config.get("sash_prompt_split", 620),
                sash_queue=self.config.get("sash_queue", 320),
                sash_log=self.config.get("sash_log", 150),
                sash_log_drop_split=self.config.get("sash_log_drop_split", 360),
                root_width=self.root.winfo_width(),
                root_height=self.root.winfo_height(),
            )
            self.config.update(sash_values)
            if changed:
                self._layout_corrections_pending = True

            top_section_pos = sash_values["sash_dropzone"]
            prompt_split = sash_values["sash_prompt_split"]
            queue_pos = sash_values["sash_queue"]
            log_pos = sash_values["sash_log"]
            log_drop_pos = sash_values["sash_log_drop_split"]

            # Restore main paned (top section height)
            if hasattr(self, "main_paned"):
                self.main_paned.sash_place(0, 0, top_section_pos)

            # Restore horizontal split (options+drop | prompt)
            if hasattr(self, "top_h_paned"):
                self.top_h_paned.sash_place(0, prompt_split, 0)

            # Restore bottom paned (queue width)
            if hasattr(self, "bottom_paned"):
                self.bottom_paned.sash_place(0, queue_pos, 0)

            # Restore right paned (log height)
            if hasattr(self, "right_paned"):
                self.right_paned.sash_place(0, 0, log_pos)

            # Restore log pane horizontal split (log | permanent drop zone)
            if hasattr(self, "log_drop_paned"):
                self.log_drop_paned.sash_place(0, log_drop_pos, 0)

        except Exception as e:
            # Sash positions may fail on first run, that's OK
            pass

    def _save_layout(self):
        """Save window geometry and sash positions to config."""
        try:
            # Save window geometry (size and position)
            self.config["window_geometry"] = self.root.geometry()

            # Save sash positions
            if hasattr(self, "main_paned"):
                try:
                    sash_pos = self.main_paned.sash_coord(0)
                    if sash_pos:
                        self.config["sash_dropzone"] = sash_pos[
                            1
                        ]  # Y position for vertical paned
                except Exception:
                    pass

            if hasattr(self, "top_h_paned"):
                try:
                    sash_pos = self.top_h_paned.sash_coord(0)
                    if sash_pos:
                        self.config["sash_prompt_split"] = sash_pos[0]  # X position
                except Exception:
                    pass

            if hasattr(self, "bottom_paned"):
                try:
                    # Only save queue sash when compare panel is closed
                    # (with compare open, sash 0 is carousel/compare, not carousel/right)
                    if self._compare_panel is None:
                        sash_pos = self.bottom_paned.sash_coord(0)
                        if sash_pos:
                            self.config["sash_queue"] = sash_pos[0]
                except Exception:
                    pass

            if hasattr(self, "right_paned"):
                try:
                    sash_pos = self.right_paned.sash_coord(0)
                    if sash_pos:
                        self.config["sash_log"] = sash_pos[
                            1
                        ]  # Y position for vertical paned
                except Exception:
                    pass

            if hasattr(self, "log_drop_paned"):
                try:
                    sash_pos = self.log_drop_paned.sash_coord(0)
                    if sash_pos:
                        self.config["sash_log_drop_split"] = sash_pos[0]
                except Exception:
                    pass

        except Exception as e:
            pass  # Don't fail on layout save errors

    # ── Session save/load ────────────────────────────────────────────────────

    def _on_save_session(self):
        """Save current session."""
        if not self.image_session.count:
            self._log("No images in session to save", "warning")
            return
        self._save_current_session_snapshot()

    def _on_new_session(self):
        """Prompt to optionally save and then start a new empty session."""
        if self.image_session.count == 0:
            self._log("Session already empty", "info")
            return
        choice = messagebox.askyesnocancel(
            "Start New Session",
            "Save current session before starting a new one?",
            parent=self.root,
        )
        if choice is None:
            return
        if choice and not self._save_current_session_snapshot():
            proceed = messagebox.askyesno(
                "Save Failed",
                "Could not save current session.\nStart a new session anyway?",
                parent=self.root,
            )
            if not proceed:
                return
        self._clear_working_session(label="new session")

    def _on_open_sessions(self):
        """Open the session manager dialog."""
        dialog = SessionManagerDialog(
            self.root, self.data_dir, self.image_session,
            self.config, self._save_config, self._log,
        )
        self.root.wait_window(dialog)
        if dialog._loaded_session_data:
            self._on_session_loaded(dialog._loaded_session_data)

    def _on_session_loaded(self, data: dict):
        """Restore session from loaded data."""
        session_data = data.get("session", {})
        images = session_data.get("images", [])
        if not images:
            self._log("Session has no images", "warning")
            return
        # Suppress auto-calc and autosave burst during restore.
        self.carousel.suppress_auto_calc(True)
        self.session_controller.autosave_suspended = True
        loaded_count = 0
        try:
            # Clear and re-populate the LIVE session (preserves tab references)
            self.image_session.clear()
            for img in images:
                path = img.get("path", "")
                if not os.path.isfile(path):
                    self._log(f"Skipped missing: {os.path.basename(path)}", "warning")
                    continue
                self.image_session.add_image(
                    path,
                    img.get("source_type", "input"),
                    label=img.get("label", ""),
                    similarity=img.get("similarity"),
                    similarity_score=img.get("similarity_score"),
                    similarity_pass=img.get("similarity_pass"),
                    similarity_override=bool(img.get("similarity_override", False)),
                    similarity_override_note=img.get("similarity_override_note", ""),
                    similarity_override_ts=img.get("similarity_override_ts"),
                    ops=img.get("ops", {}),
                )
                loaded_count += 1
            # Restore indices
            target_idx = session_data.get("current_index", -1)
            if 0 <= target_idx < self.image_session.count:
                self.image_session.navigate_to(target_idx)
            ref_idx = session_data.get("reference_index", -1)
            if 0 <= ref_idx < self.image_session.count:
                self.image_session._reference_index = ref_idx
            # Restore similarity ref
            sim_ref_idx = session_data.get("similarity_ref_index", -1)
            if 0 <= sim_ref_idx < self.image_session.count:
                self.image_session._similarity_ref_index = sim_ref_idx
            self.image_session._notify()
        finally:
            self.session_controller.autosave_suspended = False
            self.carousel.suppress_auto_calc(False)
        self._refresh_session_dependent_ui()
        self._queue_autosave(reason="session_load")
        self._log(f"Session restored: {loaded_count} images loaded", "success")

    # ── Auto-save timer ───────────────────────────────────────────────────────

    def _start_autosave_timer(self):
        """Start the auto-save timer if configured."""
        self.session_controller.start_autosave_timer()

    def _autosave_tick(self):
        """Perform auto-save if session has images, then reschedule."""
        self.session_controller.autosave_tick()

    def _on_image_session_changed(self):
        """Debounced autosave trigger for key session changes."""
        self.session_controller.on_image_session_changed()

    def _queue_autosave(self, reason: str = "state_change", debounce_ms: Optional[int] = None):
        """Queue one debounced autosave call."""
        self.session_controller.queue_autosave(reason=reason, debounce_ms=debounce_ms)

    def _run_debounced_autosave(self, reason: str):
        """Execute pending autosave callback."""
        self.session_controller.run_debounced_autosave(reason=reason)

    def _maybe_autosave(self, reason: str = "manual"):
        """Persist a versioned autosave snapshot for the current project."""
        self.session_controller.maybe_autosave(reason=reason)

    def _on_close(self):
        """Handle window close."""
        # Check both queue and tab worker threads
        busy_tabs = []
        for tab_name in ["face_crop_tab", "prep_tab", "selfie_tab", "expand_tab"]:
            tab_widget = getattr(self, tab_name, None)
            if tab_widget and getattr(tab_widget, "_busy", False):
                busy_tabs.append(tab_name.replace("_tab", "").title())

        is_processing = (
            (self.queue_manager and self.queue_manager.is_running)
            or bool(busy_tabs)
        )

        if is_processing:
            detail = ""
            if busy_tabs:
                detail = f" ({', '.join(busy_tabs)} running)"
            if not messagebox.askyesno(
                "Confirm Close",
                f"Processing is in progress{detail}. "
                "Are you sure you want to close?",
            ):
                return

            if self.queue_manager and self.queue_manager.is_running:
                self.queue_manager.stop_processing()

        # Collect tab configs before saving
        for tab in ["face_crop_tab", "prep_tab", "selfie_tab", "expand_tab"]:
            tab_widget = getattr(self, tab, None)
            if tab_widget and hasattr(tab_widget, "get_config_updates"):
                try:
                    self.config.update(tab_widget.get_config_updates())
                except Exception:
                    pass

        # Flush pending autosave and perform final best-effort save.
        self.session_controller.flush_before_close()

        # Save layout (geometry + sash positions) before closing
        self._save_layout()
        self._save_history()
        self._save_config()

        self._close_drop_zone()
        self._close_similarity_launcher()

        # Clean up tkinter variables before destroying root to prevent
        # "main thread is not in main loop" errors on Python 3.14+
        if hasattr(self, "config_panel") and self.config_panel:
            self.config_panel.cleanup()

        # Process any pending events and quit mainloop before destroy
        # This ensures cleaner shutdown on Python 3.14+ with stricter thread safety
        try:
            self.root.update_idletasks()
            self.root.quit()
        except Exception:
            pass
        self.root.destroy()

    def run(self):
        """Run the GUI main loop."""
        self._log("GUI started - drag images to process", "info")
        self.root.mainloop()


def write_crash_log(error_type: str, error_msg: str, traceback_str: str):
    """Write crash information to a log file for debugging."""
    from datetime import datetime

    crash_log_path = get_crash_log_path()

    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    crash_info = f"""
{"=" * 60}
CRASH REPORT (GUI) - {timestamp}
{"=" * 60}
Error Type: {error_type}
Error Message: {error_msg}

Full Traceback:
{traceback_str}
{"=" * 60}

"""
    try:
        # Append to crash log (keep history)
        with open(crash_log_path, "a", encoding="utf-8") as f:
            f.write(crash_info)
        print(f"\n[Crash log saved to: {crash_log_path}]")
    except Exception as log_error:
        print(f"[Could not write crash log: {log_error}]")


def launch_gui(config_path: Optional[str] = None):
    """Launch the GUI window with crash handling."""
    import traceback

    try:
        window = KlingGUIWindow(config_path=config_path)

        window.run()
    except Exception as e:
        tb_str = traceback.format_exc()

        # Print full error to console
        print("\n" + "=" * 60)
        print("  FATAL ERROR - GUI Crashed")
        print("=" * 60)
        print(f"\nError: {type(e).__name__}: {e}")
        print("\nFull traceback:")
        print(tb_str)
        print("=" * 60)

        # Write to crash log file
        write_crash_log(type(e).__name__, str(e), tb_str)

        # Re-raise to ensure non-zero exit code
        raise


if __name__ == "__main__":
    try:
        launch_gui()
    except Exception:
        # Error already printed and logged by launch_gui
        import sys

        sys.exit(1)
