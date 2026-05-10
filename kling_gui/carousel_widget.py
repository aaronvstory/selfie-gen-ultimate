"""Image Carousel Widget — unified carousel showing all images with hover preview."""

import tkinter as tk
from tkinter import ttk
from typing import Callable, Optional
import os
import platform
import logging
import threading
import subprocess

from .theme import (
    COLORS,
    FONT_FAMILY,
    TTK_BTN_COMPACT,
    TTK_BTN_DANGER_COMPACT,
    TTK_BTN_SECONDARY,
    TTK_BTN_SUCCESS_COMPACT,
    BUTTON_TEXT_COLOR,
    BUTTON_DISABLED_TEXT_COLOR,
    debounce_command,
)
from .image_state import ImageSession
from .tag_utils import derive_display_tag
from tk_dialogs import select_open_files
from path_utils import preflight_image_path


def _format_image_info(path: str) -> str:
    """Return '(WxH, X.X KB)' for a file, or '' on error."""
    try:
        size_kb = os.path.getsize(path) / 1024
        from PIL import Image as _Img
        with _Img.open(path) as img:
            w, h = img.size
        if size_kb >= 1024:
            return f"({w}\u00d7{h}, {size_kb/1024:.1f} MB)"
        return f"({w}\u00d7{h}, {size_kb:.0f} KB)"
    except Exception:
        return ""


def _truncate_filename(name: str, max_chars: int = 35) -> str:
    """Truncate long filenames: 'very_long_na...ol_exp.png'"""
    if len(name) <= max_chars:
        return name
    stem, ext = os.path.splitext(name)
    keep = max_chars - len(ext) - 3  # 3 for "..."
    if keep < 6:
        return name[:max_chars - 3] + "..."
    half = keep // 2
    return stem[:half] + "..." + stem[-(keep - half):] + ext


def _sim_color(similarity_str) -> Optional[str]:
    """Return a color hex for similarity percentage. None if no sim."""
    if not similarity_str:
        return None
    try:
        val = int(str(similarity_str).rstrip("%"))
    except ValueError:
        return None
    if val >= 100:
        return "#0B5D1E"  # very dark green
    if val >= 90:
        return "#0F7A2B"  # dark green
    if val >= 80:
        return "#6EA80D"  # yellow-green
    if val >= 70:
        return "#B35B00"  # orange/dark orange-red
    if val >= 60:
        return "#A93A14"  # transition red-orange
    return "#8B0000"  # deep red


def _sim_badge_style(similarity_str) -> Optional[dict]:
    """Return badge style for similarity indicator."""
    if similarity_str is None:
        return None
    raw = str(similarity_str).strip().lower()
    if not raw:
        return None
    if raw in {"n/a", "na", "none"}:
        return {"fg": "#F1F1F1", "bg": "#4A4A4A", "border": "#6A6A6A"}
    try:
        val = int(raw.rstrip("%"))
    except ValueError:
        return {"fg": "#F1F1F1", "bg": "#4A4A4A", "border": "#6A6A6A"}
    if val >= 100:
        return {"fg": "#F4FFF6", "bg": "#0B5D1E", "border": "#2B8A3E"}
    if val >= 80:
        return {"fg": "#F8FFE9", "bg": "#567F00", "border": "#84B800"}
    if val >= 70:
        return {"fg": "#FFF3E8", "bg": "#8C4300", "border": "#C96A1A"}
    if val >= 60:
        return {"fg": "#FFEDE5", "bg": "#8B2E10", "border": "#BE4A20"}
    return {"fg": "#FFEAEA", "bg": "#7A0000", "border": "#B21B1B"}


logger = logging.getLogger(__name__)
_REF_ACTIVE_BG = "#E5C100"
_REF_ACTIVE_FG = "#111111"

class ImageCarousel(tk.Frame):
    """Unified carousel showing all images (input + selfie + outpaint) in one stream.

    Same constructor signature as before so main_window.py needs minimal changes.
    """

    def __init__(
        self,
        parent,
        image_session: ImageSession,
        log_callback: Callable[[str, str], None],
        **kwargs,
    ):
        super().__init__(parent, bg=COLORS["bg_panel"], **kwargs)
        self.image_session = image_session
        self.log = log_callback

        # PhotoImage ref to prevent GC
        self._photo: Optional[tk.PhotoImage] = None

        # Hover preview state
        self._hover_popup: Optional[tk.Toplevel] = None
        self._hover_photo = None
        self._hover_job: Optional[str] = None

        # Compare callback (set by main_window)
        self._on_compare_callback: Optional[Callable[[], None]] = None

        # Re-entrancy guard
        self._updating: bool = False

        # Similarity computation state
        self._sim_lock = threading.Lock()
        self._sim_busy: bool = False
        self._auto_var = tk.BooleanVar(value=True)
        self._last_known_count: int = 0
        self._suppress_auto_calc: bool = False

        self._build_panel()

        # Listen for session changes
        self.image_session.set_on_change(self._on_session_change)

    # ── Public API ──────────────────────────────────────────────────

    def set_on_compare(self, callback: Callable[[], None]):
        """Register the callback invoked when the Compare button is clicked."""
        self._on_compare_callback = callback

    # ── Panel layout ────────────────────────────────────────────────

    def _build_panel(self):
        self.panel_frame = tk.Frame(self, bg=COLORS["bg_panel"])
        self.panel_frame.pack(side=tk.TOP, fill=tk.BOTH, expand=True)

        # Header row
        header = tk.Frame(self.panel_frame, bg=COLORS["bg_panel"])
        header.pack(side=tk.TOP, fill=tk.X, padx=8, pady=(5, 2))

        tk.Label(
            header,
            text="IMAGE CAROUSEL",
            font=(FONT_FAMILY, 9, "bold"),
            bg=COLORS["bg_panel"],
            fg=COLORS["text_light"],
        ).pack(side=tk.LEFT)

        self.counter_label = tk.Label(
            header,
            text="0/0",
            font=(FONT_FAMILY, 9, "bold"),
            bg=COLORS["bg_panel"],
            fg=COLORS["text_dim"],
        )
        self.counter_label.pack(side=tk.LEFT, padx=(8, 8))

        controls = tk.Frame(header, bg=COLORS["bg_panel"])
        controls.pack(side=tk.RIGHT)

        # Prev/next + remove/add controls
        self.prev_btn = ttk.Button(
            controls,
            text="\u25C0",
            style=TTK_BTN_COMPACT,
            command=debounce_command(lambda: self.image_session.navigate(-1), key="carousel_prev", interval_ms=120),
            width=2,
        )
        self.prev_btn.pack(side=tk.LEFT, padx=(0, 2))

        self.next_btn = ttk.Button(
            controls,
            text="\u25B6",
            style=TTK_BTN_COMPACT,
            command=debounce_command(lambda: self.image_session.navigate(1), key="carousel_next", interval_ms=120),
            width=2,
        )
        self.next_btn.pack(side=tk.LEFT, padx=(0, 4))

        self.remove_btn = ttk.Button(
            controls,
            text="-",
            style=TTK_BTN_DANGER_COMPACT,
            command=debounce_command(self._on_remove_image, key="carousel_remove"),
            width=2,
            state=tk.DISABLED,
        )
        self.remove_btn.pack(side=tk.LEFT, padx=(0, 2))

        add_btn = ttk.Button(
            controls,
            text="+",
            style=TTK_BTN_SUCCESS_COMPACT,
            command=debounce_command(self._on_add_image, key="carousel_add"),
            width=2,
        )
        add_btn.pack(side=tk.LEFT, padx=(0, 2))

        # Similarity controls row: ★ Ref + Compare + Auto
        sim_row = tk.Frame(self.panel_frame, bg=COLORS["bg_panel"])
        sim_row.pack(side=tk.TOP, fill=tk.X, padx=8, pady=(0, 2))

        self._ref_btn = tk.Button(
            sim_row,
            text="\u2605 Ref",
            command=debounce_command(self._toggle_sim_ref, key="carousel_ref"),
            state=tk.DISABLED,
            width=10,
            font=(FONT_FAMILY, 9, "bold"),
            bg=COLORS["bg_panel"],
            fg=BUTTON_TEXT_COLOR,
            activebackground=COLORS["bg_hover"],
            activeforeground=BUTTON_TEXT_COLOR,
            disabledforeground=BUTTON_DISABLED_TEXT_COLOR,
            highlightbackground=COLORS["bg_main"],
            highlightthickness=1,
            relief=tk.FLAT,
            bd=0,
            padx=8,
            pady=4,
            cursor="hand2",
        )
        self._ref_btn.pack(side=tk.LEFT)

        self.compare_btn = tk.Button(
            sim_row,
            text="Compare",
            command=debounce_command(self._on_compare, key="carousel_compare"),
            state=tk.DISABLED,
            width=10,
            font=(FONT_FAMILY, 9, "bold"),
            bg=COLORS["bg_panel"],
            fg=BUTTON_TEXT_COLOR,
            activebackground=COLORS["bg_hover"],
            activeforeground=BUTTON_TEXT_COLOR,
            disabledforeground=BUTTON_DISABLED_TEXT_COLOR,
            highlightbackground=COLORS["bg_main"],
            highlightthickness=1,
            relief=tk.FLAT,
            bd=0,
            padx=8,
            pady=4,
            cursor="hand2",
        )
        self.compare_btn.pack(side=tk.LEFT, padx=(6, 0))

        self.open_active_folder_btn = ttk.Button(
            sim_row,
            text="📂",
            style=TTK_BTN_COMPACT,
            width=2,
            command=debounce_command(self._on_open_active_image_folder, key="carousel_open_folder"),
        )
        self.open_active_folder_btn.pack(side=tk.LEFT, padx=(6, 0))

        self._auto_chk = tk.Checkbutton(
            sim_row,
            text="Auto",
            variable=self._auto_var,
            font=(FONT_FAMILY, 7),
            bg=COLORS["bg_panel"],
            fg=COLORS["text_dim"],
            selectcolor=COLORS["bg_input"],
            activebackground=COLORS["bg_panel"],
            activeforeground=COLORS["text_light"],
        )
        self._auto_chk.pack(side=tk.RIGHT)

        # Metadata row (resolution + filesize on left, similarity on right)
        self.meta_frame = tk.Frame(self.panel_frame, bg=COLORS["bg_panel"])
        self.meta_frame.pack(side=tk.BOTTOM, fill=tk.X, padx=8, pady=(0, 2))

        self.meta_label = tk.Label(
            self.meta_frame, text="", font=(FONT_FAMILY, 8),
            bg=COLORS["bg_panel"], fg=COLORS["text_dim"], anchor=tk.W,
        )
        self.meta_label.pack(side=tk.LEFT)

        self.sim_label = tk.Label(
            self.meta_frame,
            text="",
            font=(FONT_FAMILY, 11, "bold"),
            bg=COLORS["bg_panel"],
            fg=COLORS["text_dim"],
            anchor=tk.E,
            bd=0,
            relief=tk.FLAT,
            padx=0,
            pady=0,
            highlightthickness=0,
        )
        self.sim_label.pack(side=tk.RIGHT)

        # Info label (type tag + name) — pack second = just above meta
        self.info_label = tk.Label(
            self.panel_frame,
            text="",
            font=(FONT_FAMILY, 8),
            bg=COLORS["bg_panel"],
            fg=COLORS["text_dim"],
            anchor=tk.W,
        )
        self.info_label.pack(side=tk.BOTTOM, fill=tk.X, padx=8, pady=(0, 0))

        # Canvas for image display
        self.canvas = tk.Canvas(
            self.panel_frame, bg=COLORS["bg_main"], highlightthickness=0
        )
        self.canvas.pack(fill=tk.BOTH, expand=True, padx=8, pady=2)
        self.canvas.bind("<Configure>", lambda _e: self._update_display())
        self.canvas.bind("<Enter>", self._on_canvas_enter)
        self.canvas.bind("<Leave>", self._on_hover_leave)
        self.canvas.bind("<Button-3>", self._show_context_menu)

    # ── Session change handler ──────────────────────────────────────

    def _on_session_change(self):
        self._update_display()
        self.after(50, self._update_display)
        # Auto-calc similarity for newly added images
        n = self.image_session.count
        if (not self._suppress_auto_calc
                and self._auto_var.get()
                and n > self._last_known_count
                and self.image_session.similarity_ref_entry
                and not self._sim_busy):
            entry = self.image_session.active_entry
            ref = self.image_session.similarity_ref_entry
            if entry and entry is not ref and entry.similarity is None:
                self._run_sim_calc(entry, ref)
        self._last_known_count = n

    # ── Main update ─────────────────────────────────────────────────

    def _update_display(self):
        if self._updating:
            return
        self._updating = True
        try:
            self._update_panel()
        finally:
            self._updating = False

    def _update_panel(self):
        self.canvas.delete("all")
        session = self.image_session
        entry = session.active_entry
        n = session.count

        # Button states
        self.remove_btn.config(state=tk.NORMAL if n > 0 else tk.DISABLED)
        self.compare_btn.config(state=tk.NORMAL if n >= 2 else tk.DISABLED)

        nav_state = tk.NORMAL if n > 1 else tk.DISABLED
        self.prev_btn.config(state=nav_state)
        self.next_btn.config(state=nav_state)

        # Sim ref button state
        is_manual_ref = (
            session.current_index == session.similarity_ref_index
            and session.similarity_ref_index >= 0
        )
        is_effective_ref = (
            entry is session.effective_similarity_ref_entry and entry is not None
        )

        if n > 0:
            self._ref_btn.config(state=tk.NORMAL)
            if is_manual_ref:
                self._ref_btn.config(text="\u2605 Clear")
                self._ref_btn.config(
                    bg=_REF_ACTIVE_BG,
                    fg=_REF_ACTIVE_FG,
                    activebackground=_REF_ACTIVE_BG,
                    activeforeground=_REF_ACTIVE_FG,
                )
            else:
                self._ref_btn.config(text="\u2605 Ref")
                self._ref_btn.config(
                    bg=_REF_ACTIVE_BG if is_effective_ref else COLORS["bg_panel"],
                    fg=_REF_ACTIVE_FG if is_effective_ref else BUTTON_TEXT_COLOR,
                    activebackground=_REF_ACTIVE_BG if is_effective_ref else COLORS["bg_hover"],
                    activeforeground=_REF_ACTIVE_FG if is_effective_ref else BUTTON_TEXT_COLOR,
                )
        else:
            self._ref_btn.config(state=tk.DISABLED, text="\u2605 Ref")
            self._ref_btn.config(
                bg=COLORS["bg_panel"],
                fg=BUTTON_TEXT_COLOR,
                activebackground=COLORS["bg_hover"],
                activeforeground=BUTTON_TEXT_COLOR,
            )

        if n == 0:
            self.counter_label.config(text="0/0")
            self.info_label.config(text="Add images to start", fg=COLORS["text_dim"])
            self.meta_label.config(text="")
            self.sim_label.config(text="")
            cw = max(1, self.canvas.winfo_width())
            ch = max(1, self.canvas.winfo_height())
            self.canvas.create_text(
                cw // 2,
                ch // 2,
                text="Add images to start",
                fill=COLORS["text_dim"],
                font=(FONT_FAMILY, 10),
            )
            return
        self.counter_label.config(text=f"{session.current_index + 1}/{n}")

        # Show the active image
        if entry and entry.exists:
            self._show_image_on_canvas(self.canvas, entry.path, "_photo")

            # Type-colored info label (line 1: tag + truncated filename)
            tag, color_key = derive_display_tag(entry)
            color = COLORS.get(color_key, COLORS["text_dim"])
            display_name = _truncate_filename(entry.filename)
            ref_prefix = "\u2605 " if is_effective_ref else ""
            self.info_label.config(text=f"{ref_prefix}{tag} {display_name}", fg=color)

            # Meta line: dimensions + filesize (left, gray)
            info = _format_image_info(entry.path)
            self.meta_label.config(text=info.strip("()") if info else "")

            # Similarity (right, colored)
            if entry.similarity_recalculating:
                self.sim_label.config(
                    text="  SIM \u21bb  ",
                    fg=COLORS["text_dim"],
                    bg=COLORS["bg_panel"],
                    highlightthickness=1,
                    highlightbackground=COLORS["border"],
                    highlightcolor=COLORS["border"],
                    padx=8,
                    pady=2,
                )
            elif entry.similarity is not None:
                badge = _sim_badge_style(entry.similarity)
                if badge:
                    self.sim_label.config(
                        text=f"  SIM {entry.similarity}  ",
                        fg=badge["fg"],
                        bg=badge["bg"],
                        highlightthickness=1,
                        highlightbackground=badge["border"],
                        highlightcolor=badge["border"],
                        padx=8,
                        pady=2,
                    )
                else:
                    self.sim_label.config(
                        text=f"  SIM {entry.similarity}  ",
                        fg="#E6E6E6",
                        bg="#4A4A4A",
                        highlightthickness=1,
                        highlightbackground="#6A6A6A",
                        highlightcolor="#6A6A6A",
                        padx=8,
                        pady=2,
                    )
            else:
                self.sim_label.config(
                    text="",
                    fg=COLORS["text_dim"],
                    bg=COLORS["bg_panel"],
                    highlightthickness=0,
                    padx=0,
                    pady=0,
                )
        elif entry:
            self.info_label.config(text="File not found", fg=COLORS["error"])
            self.meta_label.config(text="")
            self.sim_label.config(text="")

    # ── Image rendering helper ──────────────────────────────────────

    def _show_image_on_canvas(self, canvas: tk.Canvas, path: str, attr_name: str):
        """Load and display an image on a canvas, aspect-fitted with EXIF + rotation."""
        try:
            from PIL import Image, ImageTk, ImageOps

            with Image.open(path) as img_src:
                img_src.load()
                img = img_src.copy()

            # Auto-correct EXIF orientation
            img = ImageOps.exif_transpose(img)

            # Apply user rotation (stored on the active entry)
            entry = self.image_session.active_entry
            if entry and entry.rotation:
                # PIL rotates counterclockwise, so negate for CW convention
                img = img.rotate(-entry.rotation, expand=True)

            cw = max(1, canvas.winfo_width() - 4)
            ch = max(1, canvas.winfo_height() - 4)

            ratio = min(cw / img.width, ch / img.height)
            new_w = max(1, int(img.width * ratio))
            new_h = max(1, int(img.height * ratio))
            img = img.resize((new_w, new_h), Image.Resampling.LANCZOS)

            photo = ImageTk.PhotoImage(img, master=canvas)
            setattr(self, attr_name, photo)
            canvas.create_image(
                cw // 2 + 2, ch // 2 + 2, image=photo, anchor=tk.CENTER
            )
            return True
        except ImportError:
            cw = max(1, canvas.winfo_width())
            ch = max(1, canvas.winfo_height())
            canvas.create_text(
                cw // 2, ch // 2,
                text="PIL not available",
                fill=COLORS["warning"],
                font=(FONT_FAMILY, 9),
            )
            self.log("Carousel render failed: PIL not available", "error")
            logger.exception("Carousel render failed: PIL not available for path=%s", path)
            return False
        except Exception as e:
            cw = max(1, canvas.winfo_width())
            ch = max(1, canvas.winfo_height())
            canvas.create_text(
                cw // 2, ch // 2,
                text=f"Cannot load: {e}",
                fill=COLORS["error"],
                font=(FONT_FAMILY, 9),
            )
            self.log(
                f"Carousel render failed: {os.path.basename(path)} ({type(e).__name__}: {e})",
                "error",
            )
            logger.exception("Carousel render failed path=%s", path)
            return False

    # ── Actions ─────────────────────────────────────────────────────

    def _on_add_image(self):
        """Open file dialog to add image(s) to session as input."""
        filetypes = [
            (
                "Image files",
                "*.jpg *.jpeg *.png *.webp *.bmp *.gif *.tiff *.tif",
            ),
            ("All files", "*.*"),
        ]
        paths = select_open_files(
            parent=self.winfo_toplevel(),
            title="Select Images", filetypes=filetypes
        )
        for p in paths:
            ok, reason = preflight_image_path(p)
            if not ok:
                self.log(
                    f"Skipped carousel add: {os.path.basename(p)} ({reason})",
                    "warning",
                )
                logger.error("Carousel add preflight failed path=%s reason=%s", p, reason)
                continue
            self.image_session.add_image(p, "input")
            self.log(f"Added to carousel session: {os.path.basename(p)}", "info")

    def _on_remove_image(self):
        """Remove the currently active image from the carousel."""
        entry = self.image_session.active_entry
        if entry is None:
            return
        name = entry.filename
        self.image_session.remove_current()
        self.log(f"Removed from carousel: {name}", "info")

    def _on_compare(self):
        if self._on_compare_callback:
            self._on_compare_callback()

    def _open_path_in_explorer(self, path: str):
        try:
            system = platform.system()
            if system == "Windows":
                os.startfile(path)
            elif system == "Darwin":
                subprocess.run(["open", path], check=True)
            else:
                subprocess.run(["xdg-open", path], check=True)
        except Exception as e:
            self.log(f"Could not open {path}: {e}", "error")

    def _on_open_active_image_folder(self):
        entry = self.image_session.active_entry
        if not entry:
            self.log("No active carousel image to open folder for", "warning")
            return
        path = str(entry.path or "").strip()
        if not path:
            self.log("No folder to open for active carousel image", "warning")
            return
        folder = os.path.dirname(path)
        if not folder or not os.path.isdir(folder):
            self.log("No folder to open for active carousel image", "warning")
            return
        self._open_path_in_explorer(folder)

    def _rotate(self, degrees: int):
        """Rotate the active image by the given degrees (positive = clockwise)."""
        entry = self.image_session.active_entry
        if not entry:
            return
        entry.rotation = (entry.rotation + degrees) % 360
        self._update_display()

    def _show_context_menu(self, event):
        """Show right-click context menu with rotation + similarity options."""
        session = self.image_session
        entry = session.active_entry
        if not entry:
            return
        menu = tk.Menu(self, tearoff=0)
        menu.add_command(label="Rotate Left (CCW)", command=lambda: self._rotate(-90))
        menu.add_command(label="Rotate Right (CW)", command=lambda: self._rotate(90))
        menu.add_command(label="Rotate 180\u00b0", command=lambda: self._rotate(180))
        menu.add_separator()
        menu.add_command(label="Reset Rotation", command=lambda: self._reset_rotation())

        # Similarity section
        menu.add_separator()
        is_ref = (session.current_index == session.similarity_ref_index
                  and session.similarity_ref_index >= 0)
        ref = session.similarity_ref_entry
        if is_ref:
            menu.add_command(label="Clear Similarity Ref",
                             command=self._toggle_sim_ref)
        else:
            menu.add_command(label="Set as Similarity Ref",
                             command=self._toggle_sim_ref)
        calc_state = tk.NORMAL if (ref and not is_ref) else tk.DISABLED
        menu.add_command(label="Compute Similarity (this image)",
                         command=self._calc_similarity, state=calc_state)
        calc_all_state = tk.NORMAL if ref else tk.DISABLED
        menu.add_command(label="Compute Similarity (all images)",
                         command=self._calc_all_similarity, state=calc_all_state)

        try:
            menu.tk_popup(event.x_root, event.y_root)
        finally:
            menu.grab_release()

    def _reset_rotation(self):
        """Reset rotation of the active image to 0."""
        entry = self.image_session.active_entry
        if not entry:
            return
        entry.rotation = 0
        self._update_display()

    # ── Similarity computation ────────────────────────────────────

    def suppress_auto_calc(self, suppress: bool):
        """Suppress auto-calc (e.g. during session restore)."""
        self._suppress_auto_calc = suppress
        if not suppress:
            self._last_known_count = self.image_session.count

    def _sim_log(self, msg: str, lvl: str = "debug"):
        """Thread-safe log wrapper for similarity — routes to Processing Log."""
        self.after(0, lambda: self.log(f"Sim: {msg}", lvl))

    def _toggle_sim_ref(self):
        """Toggle the similarity reference on/off for the active image."""
        session = self.image_session
        active_is_manual_ref = (
            session.current_index == session.similarity_ref_index
            and session.similarity_ref_index >= 0
        )
        if active_is_manual_ref:
            session.set_similarity_ref(-1)
            self.log("Similarity reference cleared", "info")
            recalc_reason = "manual ref cleared"
        else:
            session.set_similarity_ref(session.current_index)
            entry = session.active_entry
            name = entry.filename if entry else "?"
            self.log(f"Similarity reference set: {name}", "info")
            recalc_reason = "manual ref changed"
        
        # Trigger recompute for all generated images against new effective ref
        self._calc_all_similarity(auto_triggered=True, reason=recalc_reason)

    def _calc_similarity(self):
        """Compute similarity for the active image vs ref (context menu)."""
        entry = self.image_session.active_entry
        ref = self.image_session.effective_similarity_ref_entry
        if not entry or not ref or entry is ref:
            return
        self._run_sim_calc(entry, ref)

    def _run_sim_calc(self, entry, ref):
        """Run similarity in a background thread (single-worker lock)."""
        ref_path, target_path = ref.path, entry.path
        self._sim_log(
            f"ref={os.path.basename(ref_path)} "
            f"target={os.path.basename(target_path)}", "debug"
        )
        
        entry.similarity_recalculating = True
        self.image_session._notify()

        def _worker():
            if not self._sim_lock.acquire(blocking=False):
                self._sim_log("busy \u2014 skipped", "debug")
                entry.similarity_recalculating = False
                self.after(0, self.image_session._notify)
                return
            try:
                self._sim_busy = True
                from face_similarity import compute_face_similarity_details

                details = compute_face_similarity_details(
                    ref_path, target_path, report_cb=self._sim_log,
                )
            except Exception as exc:
                self._sim_log(f"FAIL {type(exc).__name__}: {exc!r}", "warning")
                details = None
            finally:
                self._sim_busy = False
                self._sim_lock.release()
            self.after(0, lambda: self._apply_sim(entry, details))

        threading.Thread(target=_worker, daemon=True).start()

    def _apply_sim(self, entry, details):
        """Apply a computed similarity score to an entry and refresh display."""
        score = None
        if details and not details.get("error"):
            score = details.get("score")
        entry.similarity_recalculating = False
        entry.update_similarity(score)
        self.image_session._notify()
        if score is not None:
            self._sim_log(f"result: {score}%", "info")

    def _calc_all_similarity(self, auto_triggered: bool = False, reason: str = "manual recalc"):
        """Compute similarity for all non-ref generated images."""
        ref, ref_source = self.image_session.get_effective_similarity_ref()
        if not ref:
            return
        targets = [e for e in self.image_session.images
                   if e.source_type != "input" and e is not ref and e.exists]
        if not targets:
            return
        ref_name = os.path.basename(ref.path)
        source_label = {
            "manual_star_ref": "manual ★ Ref",
            "auto_crop": "auto crop fallback",
            "auto_front": "auto front fallback",
            "auto_first_input": "auto first-input fallback",
            "none": "none",
        }.get(ref_source, ref_source or "unknown")
        self._sim_log(
            f"batch start: {len(targets)} images, ref={ref_name}, source={source_label}, reason={reason}",
            "info" if auto_triggered else "debug",
        )
            
        for t in targets:
            t.similarity_recalculating = True
        self.image_session._notify()
        
        ref_path = ref.path

        def _worker():
            if not self._sim_lock.acquire(blocking=True, timeout=10):
                self._sim_log("busy \u2014 batch skipped", "warning")
                for t in targets:
                    t.similarity_recalculating = False
                self.after(0, self.image_session._notify)
                return
            try:
                self._sim_busy = True
                from face_similarity import compute_face_similarity_details
                for target in targets:
                    try:
                        details = compute_face_similarity_details(
                            ref_path, target.path, report_cb=self._sim_log,
                        )
                    except Exception as exc:
                        self._sim_log(
                            f"FAIL {os.path.basename(target.path)}: {exc!r}",
                            "warning",
                        )
                        details = None
                    self.after(0, lambda e=target, d=details: self._apply_sim(e, d))
            finally:
                self._sim_busy = False
                self._sim_lock.release()
            self.after(0, lambda: self._sim_log("batch complete", "info"))

        threading.Thread(target=_worker, daemon=True).start()

    # ── Hover preview ───────────────────────────────────────────────

    def _on_canvas_enter(self, event):
        entry = self.image_session.active_entry
        if entry and entry.exists:
            self._schedule_hover(entry.path, event)

    def _schedule_hover(self, path: str, event):
        self._cancel_hover()
        self._hover_job = self.after(
            500, lambda: self._show_hover_preview(path, event)
        )

    def _cancel_hover(self):
        if self._hover_job:
            self.after_cancel(self._hover_job)
            self._hover_job = None

    def _on_hover_leave(self, _event=None):
        self._cancel_hover()
        self._destroy_hover()

    def _destroy_hover(self):
        if self._hover_popup:
            try:
                self._hover_popup.destroy()
            except tk.TclError:
                pass
            self._hover_popup = None
            self._hover_photo = None

    def _show_hover_preview(self, path: str, event):
        """Show a borderless popup with a large preview of the image."""
        self._destroy_hover()
        try:
            from PIL import Image, ImageTk, ImageOps

            with Image.open(path) as img_src:
                img_src.load()
                img = img_src.copy()

            # Auto-correct EXIF orientation (match _show_image_on_canvas)
            img = ImageOps.exif_transpose(img)

            # Apply user rotation if any
            entry = self.image_session.active_entry
            if entry and entry.rotation:
                img = img.rotate(-entry.rotation, expand=True)

            max_dim = 600
            ratio = min(max_dim / img.width, max_dim / img.height, 1.0)
            if ratio < 1.0:
                new_w = max(1, int(img.width * ratio))
                new_h = max(1, int(img.height * ratio))
                img = img.resize((new_w, new_h), Image.Resampling.LANCZOS)

            photo = ImageTk.PhotoImage(img, master=self)
            self._hover_photo = photo

            popup = tk.Toplevel(self)
            popup.overrideredirect(True)
            popup.attributes("-topmost", True)
            popup.config(bg=COLORS["bg_main"])

            label = tk.Label(popup, image=photo, bg=COLORS["bg_main"], bd=1, relief=tk.SOLID)
            label.pack()

            # Center popup on the application window
            popup.update_idletasks()
            pw = popup.winfo_reqwidth()
            ph = popup.winfo_reqheight()

            root = self.winfo_toplevel()
            rx = root.winfo_rootx()
            ry = root.winfo_rooty()
            rw = root.winfo_width()
            rh = root.winfo_height()

            x = rx + (rw - pw) // 2
            y = ry + (rh - ph) // 2

            # Clamp to virtual desktop edges (multi-monitor aware)
            sw = root.winfo_vrootwidth()
            sh = root.winfo_vrootheight()
            x = max(0, min(x, sw - pw))
            y = max(0, min(y, sh - ph))

            popup.geometry(f"+{x}+{y}")

            popup.bind("<Leave>", self._on_hover_leave)
            popup.bind("<Button-1>", self._on_hover_leave)

            self._hover_popup = popup
        except Exception as e:
            logger.debug("Hover preview error: %s", e)
