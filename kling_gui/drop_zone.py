"""
Drop Zone Widget - Drag-and-drop area for image files with visual feedback.
"""

import tkinter as tk
from typing import Callable, Dict, Iterable, List, Optional
import os
import sys

from path_utils import VALID_EXTENSIONS
from tk_dialogs import select_directory, select_open_files

try:
    from tkinterdnd2 import DND_FILES, TkinterDnD

    HAS_DND = True
except ImportError:
    DND_FILES = None
    HAS_DND = False

# Allow explicit runtime opt-out when troubleshooting drag-and-drop.
if os.getenv("SELFIEGEN_MAC_DISABLE_DND", "0") == "1":
    HAS_DND = False


def parse_dnd_paths(
    data: str,
    splitlist_fn: Optional[Callable[[str], Iterable[str]]] = None,
    require_exists: bool = True,
) -> List[str]:
    """Parse TkDND payload into normalized file paths."""
    if not data:
        return []

    raw_items = []
    parser = splitlist_fn
    if parser:
        try:
            raw_items = list(parser(data))
        except Exception:
            raw_items = []

    if not raw_items:
        # Fallback parser for plain/brace-quoted payloads.
        import re

        if "{" in data:
            brace_items = re.findall(r"\{([^}]+)\}", data)
            raw_items.extend(brace_items)
            remaining = re.sub(r"\{[^}]+\}", "", data).strip()
            if remaining:
                raw_items.extend(remaining.split())
        else:
            raw_items.extend(data.split())

    cleaned = []
    for item in raw_items:
        path = str(item).strip().strip('"').strip("'")
        if not path:
            continue
        if require_exists and not os.path.exists(path):
            continue
        cleaned.append(path)

    return cleaned


def _safe_stderr(msg: str) -> None:
    """Write to stderr, tolerating ``sys.stderr is None``.

    Under ``pythonw.exe`` (the windowed launcher used so the GUI has no console)
    ``sys.stderr`` is None — a bare ``sys.stderr.write`` then raises
    AttributeError and would CRASH the app on the exact tkdnd-failure path this
    fallback exists to survive (gemini HIGH). Swallow any write failure."""
    try:
        if sys.stderr is not None:
            sys.stderr.write(msg)
    except Exception:
        pass


def create_dnd_root() -> tk.Tk:
    """Create a TkinterDnD root window if available, otherwise regular Tk.

    This is a module-level function for easy import by main_window.py.

    HAS_DND being True (the `import tkinterdnd2` succeeded) does NOT guarantee
    the native tkdnd library LOADS. ``TkinterDnD.Tk()`` runs Tcl
    ``package require tkdnd``, which raises ``tkinter.TclError: Unable to load
    tkdnd library`` (wrapped as RuntimeError) when the bundled tkdnd binary
    doesn't match the user's Tcl/Tk build — common on a fresh python.org 3.12.
    That is NOT an ImportError, so the import-time guard above can't catch it,
    and without this try/except the whole GUI crashed on startup with
    "Unable to load tkdnd library" (reported on a fresh v2.9 install).

    Drag-and-drop is OPTIONAL: degrade to a plain ``tk.Tk()`` and flip the
    module-level HAS_DND off so every drop_target_register site (here,
    main_window, config_panel) skips DnD — the file pickers still work.
    """
    global HAS_DND
    if HAS_DND:
        try:
            return TkinterDnD.Tk()
        except Exception as exc:  # tkinter.TclError / RuntimeError from tkdnd
            HAS_DND = False
            _safe_stderr(
                "[selfie-gen] drag-and-drop unavailable "
                f"(tkdnd failed to load: {type(exc).__name__}: {exc}); "
                "use the file pickers instead.\n"
            )
    return tk.Tk()


# Color palette
COLORS = {
    "bg_main": "#2D2D30",
    "bg_panel": "#3C3C41",
    "bg_drop": "#464649",
    "bg_hover": "#505055",
    "text_light": "#DCDCDC",
    "text_dim": "#B4B4B4",
    "drop_valid": "#329632",
    "drop_invalid": "#963232",
    "border": "#5A5A5E",
    "accent_blue": "#6496FF",
}

FONT_FAMILY = "Helvetica" if sys.platform == "darwin" else "Segoe UI"
EMOJI_FONT_FAMILY = "Apple Color Emoji" if sys.platform == "darwin" else "Segoe UI Emoji"


class DropZone(tk.Frame):
    """Large drop zone for dragging image files and folders."""

    def __init__(
        self,
        parent,
        on_files_dropped: Callable[[List[str]], None],
        on_folder_dropped: Callable[[str], None] = None,
        compact: bool = False,
        tint: Optional[Dict[str, str]] = None,
        **kwargs,
    ):
        """
        Initialize the drop zone.

        Args:
            parent: Parent widget
            on_files_dropped: Callback function that receives list of file paths
            on_folder_dropped: Callback function that receives folder path
            compact: Render compact single-purpose layout (icon + one title line)
            tint: Optional color override dict for compact/permanent variants.
                Supported keys:
                - bg_drop: drop-zone background color
                - bg_hover: hover background color
                - border: border color
                - accent: accent/icon color
                - text: primary text color
                - text_dim: secondary text color
                - drop_valid: valid-drop highlight color
                Unknown keys are ignored.
        """
        super().__init__(parent, bg=COLORS["bg_panel"], **kwargs)
        self.on_files_dropped = on_files_dropped
        self.on_folder_dropped = on_folder_dropped
        self.compact = compact
        supported_tint_keys = {
            "bg_drop",
            "bg_hover",
            "border",
            "accent",
            "text",
            "text_dim",
            "drop_valid",
        }
        raw_tint = tint or {}
        self._tint = {k: v for k, v in raw_tint.items() if k in supported_tint_keys}
        self._default_bg = self._tint.get("bg_drop", COLORS["bg_drop"])
        self._hover_bg = self._tint.get("bg_hover", COLORS["bg_hover"])
        self._border_color = self._tint.get("border", COLORS["border"])
        self._accent_color = self._tint.get("accent", COLORS["accent_blue"])
        self._text_color = self._tint.get("text", COLORS["text_light"])
        self._text_dim_color = self._tint.get("text_dim", COLORS["text_dim"])
        self._drop_valid_color = self._tint.get("drop_valid", COLORS["drop_valid"])

        # Create the drop area
        self.drop_frame = tk.Frame(
            self,
            bg=self._default_bg,
            highlightbackground=self._border_color,
            highlightthickness=2,
        )
        self.drop_frame.pack(fill=tk.BOTH, expand=True, padx=8 if self.compact else 12, pady=8 if self.compact else 12)

        # Container for content to help centering
        self.content_container = tk.Frame(self.drop_frame, bg=self._default_bg)
        self.content_container.place(relx=0.5, rely=0.5, anchor="center")

        # Icon/emoji label
        self.icon_label = tk.Label(
            self.content_container,
            text="📥",
            font=(EMOJI_FONT_FAMILY, 30 if self.compact else 40),
            bg=self._default_bg,
            fg=self._accent_color,
        )
        self.icon_label.pack(pady=(0, 5 if self.compact else 8))

        # Main instruction label
        main_font = (FONT_FAMILY, 11 if self.compact else 14, "bold")
        main_text = "DROP ZONE" if self.compact else "DRAG & DROP IMAGES"
        self._main_text_canvas = None
        if self.compact and sys.platform.startswith("win"):
            # Windows can clip a glyph in ttk/label text rasterization in this compact pane.
            # Canvas text renders reliably here.
            self._main_text_canvas = tk.Canvas(
                self.content_container,
                width=220,
                height=28,
                bg=self._default_bg,
                highlightthickness=0,
                bd=0,
            )
            self._main_text_canvas.create_text(
                110,
                14,
                text=main_text,
                font=("Segoe UI", 13, "bold"),
                fill=self._text_color,
            )
            self._main_text_canvas.pack(pady=(1, 1))
            self.main_label = self._main_text_canvas
        else:
            self.main_label = tk.Label(
                self.content_container,
                text=main_text,
                font=main_font,
                bg=self._default_bg,
                fg=self._text_color,
                padx=6 if self.compact else 0,
            )
            self.main_label.pack(pady=(1 if self.compact else 2))

        # Sub-instruction label (combined: left click + right click hint on one line)
        self.sub_label = tk.Label(
            self.content_container,
            text="" if self.compact else "Left click to select files  |  Right click for folder",
            font=(FONT_FAMILY, 9 if self.compact else 11),
            bg=self._default_bg,
            fg=self._text_dim_color,
        )
        if not self.compact:
            self.sub_label.pack(pady=(0, 10))

        # Bind events for hover and click
        for widget in [
            self.drop_frame,
            self.icon_label,
            self.main_label,
            self.sub_label,
            self.content_container,
        ]:
            if widget is self.sub_label and self.compact:
                continue
            widget.bind("<Button-1>", self._on_click_browse)
            widget.bind("<Button-3>", self._on_right_click_browse_folder)
            widget.bind("<Enter>", self._on_mouse_enter)
            widget.bind("<Leave>", self._on_mouse_leave)
            widget.config(cursor="hand2")

        # Status label (shows during drag)
        self.status_label = tk.Label(
            self.drop_frame,
            text="",
            font=(FONT_FAMILY, 9 if self.compact else 11, "bold"),
            bg=self._default_bg,
            fg=self._text_color,
        )
        self.status_label.pack(side=tk.BOTTOM, pady=8 if self.compact else 15)

        # Register for drag-and-drop if available
        if HAS_DND:
            self._setup_dnd()
        else:
            if isinstance(self.main_label, tk.Canvas):
                self.main_label.delete("all")
                self.main_label.create_text(
                    110,
                    14,
                    text="CLICK TO SELECT IMAGES",
                    font=("Segoe UI", 11, "bold"),
                    fill=self._text_color,
                )
            else:
                self.main_label.config(text="CLICK TO SELECT IMAGES")
            if not self.compact:
                self.sub_label.config(text="Left click to select files  |  Right click for folder")

    def _on_mouse_enter(self, event=None):
        """Handle mouse hover enter."""
        self._set_highlight(self._hover_bg)
        self.drop_frame.config(highlightbackground=self._accent_color)

    def _on_mouse_leave(self, event=None):
        """Handle mouse hover leave."""
        if event:
            df = self.drop_frame
            rel_x = event.x_root - df.winfo_rootx()
            rel_y = event.y_root - df.winfo_rooty()
            if 0 <= rel_x <= df.winfo_width() and 0 <= rel_y <= df.winfo_height():
                return  # mouse moved onto a child widget, still inside drop_frame
        self._reset_highlight()
        self.drop_frame.config(highlightbackground=self._border_color)

    def _set_highlight(self, color: str):
        """Set the highlight color for drop/hover feedback."""
        for widget in [
            self.drop_frame,
            self.icon_label,
            self.main_label,
            self.sub_label,
            self.content_container,
            self.status_label,
        ]:
            if widget is self.sub_label and self.compact:
                continue
            widget.config(bg=color)

    def _reset_highlight(self):
        """Reset to default colors."""
        self._set_highlight(self._default_bg)

    def _clear_status_later(self):
        """Clear status text only if the widget still exists."""
        self.after(
            2000,
            lambda: self.status_label.config(text="") if self.winfo_exists() else None,
        )

    def _on_click_browse(self, event=None):
        """Handle left-click to open file browser dialog."""
        self._browse_files()

    def _browse_files(self):
        """Open file browser dialog for image files."""
        # Define file types for the dialog
        filetypes = [
            ("Image files", "*.jpg *.jpeg *.png *.webp *.bmp *.gif *.tiff *.tif"),
            ("JPEG files", "*.jpg *.jpeg"),
            ("PNG files", "*.png"),
            ("WebP files", "*.webp"),
            ("All files", "*.*"),
        ]

        # Open file dialog - allow multiple file selection
        file_paths = select_open_files(
            parent=self.winfo_toplevel(),
            title="Select Images to Process",
            filetypes=filetypes,
        )

        if file_paths:
            # Filter and validate files
            valid_files = []
            invalid_count = 0

            for file_path in file_paths:
                is_valid, result_type = self.validate_file(file_path)
                if is_valid and result_type == "file":
                    valid_files.append(file_path)
                else:
                    invalid_count += 1

            # Show status and call callback
            if valid_files:
                self.status_label.config(
                    text=f"Adding {len(valid_files)} file(s)...",
                    fg=COLORS["text_light"],
                )
                self.on_files_dropped(valid_files)

            if invalid_count > 0:
                self.status_label.config(
                    text=f"Skipped {invalid_count} invalid file(s)",
                    fg=COLORS["drop_invalid"],
                )

            self._clear_status_later()

    def _browse_folder(self):
        """Open folder browser dialog."""
        folder_path = select_directory(
            parent=self.winfo_toplevel(),
            title="Select Folder to Process",
        )

        if folder_path and self.on_folder_dropped:
            self.status_label.config(
                text="Processing folder...", fg=COLORS["text_light"]
            )
            self.on_folder_dropped(folder_path)
            self._clear_status_later()

    def _on_right_click_browse_folder(self, event=None):
        """Handle right-click to open folder browser dialog."""
        self._browse_folder()

    def _setup_dnd(self):
        """Set up drag-and-drop handlers."""
        # Register all widgets in drop zone for DnD
        for widget in [
            self.drop_frame,
            self.icon_label,
            self.main_label,
            self.sub_label,
            self.status_label,
        ]:
            widget.drop_target_register(DND_FILES)
            widget.dnd_bind("<<DropEnter>>", self._on_drag_enter)
            widget.dnd_bind("<<DropLeave>>", self._on_drag_leave)
            widget.dnd_bind("<<Drop>>", self._on_drop)

    def _on_drag_enter(self, event):
        """Handle drag enter - show visual feedback."""
        self._set_highlight(self._drop_valid_color)
        self.status_label.config(
            text="DROP TO ADD" if self.compact else "DROP TO ADD TO QUEUE",
            fg=self._text_color,
        )
        return event.action

    def _on_drag_leave(self, event):
        """Handle drag leave - reset visual feedback."""
        self._reset_highlight()
        self.status_label.config(text="")
        return event.action

    def _on_drop(self, event):
        """Handle file or folder drop."""
        self._reset_highlight()
        self.status_label.config(text="")

        # Parse dropped files/folders
        items = self._parse_drop_data(event.data)

        # Separate files and folders
        valid_files = []
        folders = []
        invalid_count = 0

        for item_path in items:
            is_valid, result_type = self.validate_file(item_path)
            if is_valid:
                if result_type == "folder":
                    folders.append(item_path)
                else:
                    valid_files.append(item_path)
            else:
                invalid_count += 1

        # Handle folders first (via callback)
        for folder in folders:
            if self.on_folder_dropped:
                self.status_label.config(
                    text="Processing folder...", fg=COLORS["text_light"]
                )
                self.on_folder_dropped(folder)

        # Handle files
        if valid_files:
            self.status_label.config(
                text=f"Adding {len(valid_files)} file(s)...", fg=COLORS["text_light"]
            )
            self.on_files_dropped(valid_files)

        if invalid_count > 0:
            self.status_label.config(
                text=f"Skipped {invalid_count} invalid file(s)",
                fg=COLORS["drop_invalid"],
            )

        self._clear_status_later()

        return event.action

    def _parse_drop_data(self, data: str) -> List[str]:
        """Parse the dropped file data into a list of paths."""
        splitlist_fn = None
        try:
            splitlist_fn = self.winfo_toplevel().tk.splitlist
        except Exception:
            splitlist_fn = None
        return parse_dnd_paths(data, splitlist_fn=splitlist_fn, require_exists=True)

    def validate_file(self, file_path: str) -> tuple:
        """
        Validate a file or folder for processing.

        Returns:
            (is_valid, result_type) where result_type is "file", "folder", or error message
        """
        if not os.path.exists(file_path):
            return False, f"File not found: {file_path}"

        if os.path.isdir(file_path):
            return True, "folder"

        ext = os.path.splitext(file_path)[1].lower()
        if ext not in VALID_EXTENSIONS:
            return False, f"Unsupported format: {ext}"

        return True, "file"


# Backward compatibility alias for code that calls DropZone.create_dnd_root()
DropZone.create_dnd_root = staticmethod(create_dnd_root)
