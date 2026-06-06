"""Compare Panel — side-by-side image comparison with independent navigation."""

import os
import tkinter as tk
from tkinter import ttk
from typing import Callable, Optional
import logging

from .theme import (
    COLORS,
    FONT_FAMILY,
    TTK_BTN_COMPACT,
    TTK_BTN_DANGER_COMPACT,
)
from .image_state import ImageSession
from .carousel_widget import _format_image_info, _truncate_filename, _sim_color
from .tag_utils import derive_display_tag

logger = logging.getLogger(__name__)


class ComparePanel(tk.Frame):
    """Comparison panel with independent navigation through all session images."""

    def __init__(
        self,
        parent,
        image_session: ImageSession,
        log_callback: Callable[[str, str], None],
        on_close: Callable[[], None],
        **kwargs,
    ):
        super().__init__(parent, bg=COLORS["bg_panel"], **kwargs)
        self.image_session = image_session
        self.log = log_callback
        self._on_close = on_close

        # Independent navigation index
        self._compare_index: int = -1

        # PhotoImage ref to prevent GC
        self._photo: Optional[tk.PhotoImage] = None

        # Hover state
        self._hover_popup: Optional[tk.Toplevel] = None
        self._hover_photo_left = None
        self._hover_photo_right = None
        self._hover_job: Optional[str] = None
        # Subagent M3 round 2 on PR #83: track the cursor-off-popup
        # polling chain so `_cancel_hover` can cancel it. Without
        # this an old poll outlives a fresh popup.
        self._hover_poll_job: Optional[str] = None

        # Re-entrancy guard
        self._updating: bool = False

        self._build_panel()

        # Pick initial compare index (next image after current)
        self._init_compare_index()

        # Listen for session changes
        self.image_session.add_on_change(self._on_session_change)

        self._update_display()

    def destroy(self):
        self._cancel_hover()
        self._destroy_hover()
        self.image_session.remove_on_change(self._on_session_change)
        super().destroy()

    def _on_session_change(self):
        """Called when the image session changes — update our display."""
        # Clamp compare index if images were removed
        if self.image_session.count == 0:
            self._compare_index = -1
        elif self._compare_index >= self.image_session.count:
            self._compare_index = self.image_session.count - 1
        self._update_display()

    def _init_compare_index(self):
        n = self.image_session.count
        if n < 2:
            self._compare_index = -1
            return
        current = self.image_session.current_index
        self._compare_index = (current + 1) % n

    # ── Panel layout ────────────────────────────────────────────────

    def _build_panel(self):
        # Header row
        header = tk.Frame(self, bg=COLORS["bg_panel"])
        header.pack(side=tk.TOP, fill=tk.X, padx=8, pady=(5, 2))

        tk.Label(
            header,
            text="COMPARE",
            font=(FONT_FAMILY, 9, "bold"),
            bg=COLORS["bg_panel"],
            fg=COLORS["text_light"],
        ).pack(side=tk.LEFT)

        # Close button
        # ttk.Button (HIView tint persists). The original used
        # fg=COLORS["error"] for the "X" connotation; we keep that by
        # picking the existing TTK_BTN_DANGER_COMPACT style (red
        # foreground+fill, more visible than the prior fg-only red).
        close_btn = ttk.Button(
            header,
            text="X Close",
            command=self._on_close,
            style=TTK_BTN_DANGER_COMPACT,
        )
        close_btn.pack(side=tk.RIGHT)

        # Nav buttons + counter
        self.next_btn = ttk.Button(
            header,
            text=">",
            command=lambda: self._navigate(1),
            width=2,
            style=TTK_BTN_COMPACT,
        )
        self.next_btn.pack(side=tk.RIGHT, padx=(2, 4))

        self.counter_label = tk.Label(
            header,
            text="",
            font=(FONT_FAMILY, 9),
            bg=COLORS["bg_panel"],
            fg=COLORS["text_dim"],
        )
        self.counter_label.pack(side=tk.RIGHT, padx=2)

        self.prev_btn = ttk.Button(
            header,
            text="<",
            command=lambda: self._navigate(-1),
            width=2,
            style=TTK_BTN_COMPACT,
        )
        self.prev_btn.pack(side=tk.RIGHT)

        # Metadata row (resolution + filesize on left, similarity on right)
        self.meta_frame = tk.Frame(self, bg=COLORS["bg_panel"])
        self.meta_frame.pack(side=tk.BOTTOM, fill=tk.X, padx=8, pady=(0, 2))

        self.meta_label = tk.Label(
            self.meta_frame, text="", font=(FONT_FAMILY, 9),
            bg=COLORS["bg_panel"], fg=COLORS["text_dim"], anchor=tk.W,
        )
        self.meta_label.pack(side=tk.LEFT)

        self.sim_label = tk.Label(
            self.meta_frame, text="", font=(FONT_FAMILY, 9, "bold"),
            bg=COLORS["bg_panel"], fg=COLORS["text_dim"], anchor=tk.E,
        )
        self.sim_label.pack(side=tk.RIGHT)

        # Info label (type tag + name) — pack second = just above meta
        self.info_label = tk.Label(
            self,
            text="",
            font=(FONT_FAMILY, 9),
            bg=COLORS["bg_panel"],
            fg=COLORS["text_dim"],
            anchor=tk.W,
        )
        self.info_label.pack(side=tk.BOTTOM, fill=tk.X, padx=8, pady=(0, 0))

        # Canvas for image
        self.canvas = tk.Canvas(self, bg=COLORS["bg_main"], highlightthickness=0)
        self.canvas.pack(fill=tk.BOTH, expand=True, padx=8, pady=2)
        self.canvas.bind("<Configure>", lambda _e: self._update_display())
        self.canvas.bind("<Enter>", self._on_canvas_enter)
        self.canvas.bind("<Leave>", self._on_hover_leave)

    # ── Navigation ──────────────────────────────────────────────────

    def _navigate(self, delta: int):
        n = self.image_session.count
        if n < 2:
            return
        self._compare_index = (self._compare_index + delta) % n
        self._update_display()

    # ── Display ─────────────────────────────────────────────────────

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
        n = self.image_session.count

        nav_state = tk.NORMAL if n > 1 else tk.DISABLED
        self.prev_btn.config(state=nav_state)
        self.next_btn.config(state=nav_state)

        if n == 0 or self._compare_index < 0:
            self.counter_label.config(text="")
            self.info_label.config(text="No images to compare", fg=COLORS["text_dim"])
            self.meta_label.config(text="")
            self.sim_label.config(text="")
            return

        # Clamp index
        if self._compare_index >= n:
            self._compare_index = n - 1

        self.counter_label.config(text=f"{self._compare_index + 1}/{n}")

        images = self.image_session.images
        entry = images[self._compare_index]

        if entry.exists:
            self._show_image_on_canvas(entry)
            tag, color_key = derive_display_tag(entry)
            color = COLORS.get(color_key, COLORS["text_dim"])
            display_name = _truncate_filename(entry.filename)
            is_sim_ref = (self._compare_index == self.image_session.similarity_ref_index
                          and self.image_session.similarity_ref_index >= 0)
            ref_prefix = "\u2605 " if is_sim_ref else ""
            self.info_label.config(text=f"{ref_prefix}{tag} {display_name}", fg=color)

            # Meta line: dimensions + filesize (left, gray)
            info = _format_image_info(entry.path)
            self.meta_label.config(text=info.strip("()") if info else "")

            # Similarity (right, colored)
            if entry.similarity is not None:
                sim_fg = _sim_color(entry.similarity) or COLORS["text_dim"]
                self.sim_label.config(text=f"Sim: {entry.similarity}", fg=sim_fg)
            else:
                self.sim_label.config(text="")
        else:
            self.info_label.config(text="File not found", fg=COLORS["error"])
            self.meta_label.config(text="")
            self.sim_label.config(text="")

    def _show_image_on_canvas(self, entry):
        try:
            from PIL import Image, ImageTk, ImageOps

            # Video items: render cv2 first-frame + a centered ▶ glyph.
            # PIL.Image.open() would raise UnidentifiedImageError on a
            # .mp4 and the user would just see the generic error text —
            # broken UX given videos are now first-class carousel items.
            is_video = getattr(entry, "is_video", False)
            if is_video:
                # H2 fix (subagent on 2eb16f37): use the ASYNC helper to
                # avoid blocking the Tk thread on the first render of an
                # uncached video (100-500ms cv2.VideoCapture stall).
                # Cache is shared with carousel via _VIDEO_THUMB_CACHE, so
                # subsequent renders are fast either way — we only need
                # the async path for first-touch.
                from .carousel_widget import (
                    _extract_video_first_frame,
                    _extract_video_first_frame_async,
                    _VIDEO_THUMB_CACHE,
                )
                # (path, mtime) cache key per M1 — sees regens.
                try:
                    _vt_mtime = os.path.getmtime(entry.path)
                except OSError:
                    _vt_mtime = 0
                img = _VIDEO_THUMB_CACHE.get((entry.path, _vt_mtime))
                if img is None:
                    started = _extract_video_first_frame_async(
                        entry.path, self.canvas,
                        on_done=lambda _img: self._update_display(),
                    )
                    if started:
                        # Render placeholder for this pass; on_done
                        # triggers _update_display when the frame lands.
                        cw_pl = max(1, self.canvas.winfo_width())
                        ch_pl = max(1, self.canvas.winfo_height())
                        self.canvas.create_rectangle(
                            2, 2, cw_pl - 2, ch_pl - 2,
                            fill=COLORS["bg_input"], outline="",
                        )
                        self.canvas.create_text(
                            cw_pl // 2, ch_pl // 2,
                            text="▶", fill="#FFFFFF",
                            font=(FONT_FAMILY, 48, "bold"),
                        )
                        return
                    # If we couldn't start (e.g. pending dedup), fall
                    # through to a sync decode — placeholder will appear
                    # briefly but the user still sees the frame on the
                    # SAME render cycle.
                    img = _extract_video_first_frame(entry.path)
                if img is None:
                    # cv2 missing OR decode failed — placeholder + glyph.
                    cw_pl = max(1, self.canvas.winfo_width())
                    ch_pl = max(1, self.canvas.winfo_height())
                    self.canvas.create_rectangle(
                        2, 2, cw_pl - 2, ch_pl - 2,
                        fill=COLORS["bg_input"], outline="",
                    )
                    self.canvas.create_text(
                        cw_pl // 2, ch_pl // 2,
                        text="▶", fill="#FFFFFF",
                        font=(FONT_FAMILY, 48, "bold"),
                    )
                    return
            else:
                # with/copy pattern: PIL.Image.open holds an FD on the
                # source file under its lazy-decoder lock. _show_image_on_canvas
                # fires on every Configure event (resize, sash drag) and
                # navigate, so without the explicit close + copy the FD
                # leaks (and on Windows the queue worker hits WinError 32
                # when trying to rename/overwrite an in-flight image).
                with Image.open(entry.path) as _src:
                    _src.load()
                    img = _src.copy()

            # Auto-correct EXIF orientation (no-op on cv2-derived frames)
            img = ImageOps.exif_transpose(img)

            # Apply user rotation (skip on videos — rotation is an
            # image-edit concept, mirror carousel behavior).
            if entry.rotation and not is_video:
                img = img.rotate(-entry.rotation, expand=True)

            cw = max(1, self.canvas.winfo_width() - 4)
            ch = max(1, self.canvas.winfo_height() - 4)

            ratio = min(cw / img.width, ch / img.height)
            new_w = max(1, int(img.width * ratio))
            new_h = max(1, int(img.height * ratio))
            img = img.resize((new_w, new_h), Image.Resampling.LANCZOS)

            photo = ImageTk.PhotoImage(img)
            self._photo = photo
            cx_p = cw // 2 + 2
            cy_p = ch // 2 + 2
            self.canvas.create_image(
                cx_p, cy_p, image=photo, anchor=tk.CENTER
            )
            if is_video:
                # Centered ▶ overlay so the user can tell at a glance
                # this is a video, not a still. Size scales with thumbnail.
                short_dim = min(new_w, new_h)
                glyph_size = max(36, min(96, int(short_dim * 0.22)))
                self.canvas.create_text(
                    cx_p, cy_p,
                    text="▶", fill="#FFFFFF",
                    font=(FONT_FAMILY, glyph_size, "bold"),
                )
        except ImportError:
            cw = max(1, self.canvas.winfo_width())
            ch = max(1, self.canvas.winfo_height())
            self.canvas.create_text(
                cw // 2, ch // 2,
                text="PIL not available",
                fill=COLORS["warning"],
                font=(FONT_FAMILY, 9),
            )
        except Exception as e:
            cw = max(1, self.canvas.winfo_width())
            ch = max(1, self.canvas.winfo_height())
            self.canvas.create_text(
                cw // 2, ch // 2,
                text=f"Cannot load: {e}",
                fill=COLORS["error"],
                font=(FONT_FAMILY, 9),
            )

    # ── Hover Preview (side-by-side) ─────────────────────────────

    def _on_canvas_enter(self, _event):
        """Start hover timer when mouse enters compare canvas."""
        entry = self.image_session.active_entry
        n = self.image_session.count
        if (
            not entry
            or not entry.exists
            or n == 0
            or self._compare_index < 0
            or self._compare_index >= n
        ):
            return
        compare_entry = self.image_session.images[self._compare_index]
        if not compare_entry.exists:
            return
        self._schedule_hover(entry, compare_entry)

    def _schedule_hover(self, left_entry, right_entry):
        self._cancel_hover()
        self._hover_job = self.after(
            500, lambda: self._show_hover_preview(left_entry, right_entry)
        )

    def _cancel_hover(self):
        if self._hover_job:
            try:
                self.after_cancel(self._hover_job)
            except tk.TclError:
                pass
            self._hover_job = None
        if self._hover_poll_job:
            try:
                self.after_cancel(self._hover_poll_job)
            except tk.TclError:
                pass
            self._hover_poll_job = None

    def _force_destroy_hover(self, _event=None):
        """Force-destroy bypasses the cursor-over-popup guard. Bound to
        `<Button-1>` on the popup so the user can click-to-dismiss
        without having to move the cursor off the popup first
        (subagent H2 round 2 — the round-1 patch wired Button-1 to
        `_on_hover_leave`, which always re-checks `_cursor_over_popup`
        and so refused to destroy on the same widget the click landed
        on)."""
        self._cancel_hover()
        self._destroy_hover()

    def _cursor_over_popup(self) -> bool:
        """True when the cursor's screen coords are inside the hover popup.

        The hover popup is positioned at the centre of the parent window,
        which usually overlaps the canvas. When the popup appears, the
        cursor is instantly INSIDE the popup → the canvas fires `<Leave>`
        → without this guard, _on_hover_leave would destroy the popup → on
        the next refresh the canvas fires `<Enter>` again → 500ms later
        the popup re-appears → loop = flashing.

        This helper queries Tk's `winfo_pointerxy()` (screen coords) and
        compares against the popup's bbox so we can keep the popup alive
        when the cursor is INSIDE it even though it's outside the canvas.
        """
        popup = self._hover_popup
        if not popup:
            return False
        try:
            px, py = popup.winfo_pointerxy()
            x1 = popup.winfo_rootx()
            y1 = popup.winfo_rooty()
            x2 = x1 + popup.winfo_width()
            y2 = y1 + popup.winfo_height()
            return x1 <= px <= x2 and y1 <= py <= y2
        except tk.TclError:
            return False

    def _on_hover_leave(self, _event=None):
        # Anti-flash (PR fix/gpt-content-policy-and-hover-flash):
        # if the cursor is INSIDE the popup, the canvas's `<Leave>` is
        # just an artefact of the cursor crossing into the borderless
        # Toplevel. Keep the popup alive and poll until the cursor
        # really leaves both the canvas AND the popup.
        if self._cursor_over_popup():
            self._poll_cursor_off_popup()
            return
        self._cancel_hover()
        self._destroy_hover()

    def _poll_cursor_off_popup(self):
        """Poll until the cursor exits the popup bbox; then destroy."""
        if not self._hover_popup:
            self._hover_poll_job = None
            return
        # M2 round 2: bail when the top-level window loses focus
        # (alt-tab to another app). Tk fires no `<Leave>` in that
        # case so the polling loop would otherwise keep a borderless
        # topmost popup floating over the foreground application.
        try:
            top = self.winfo_toplevel()
            focus_win = top.focus_displayof()
            if focus_win is None:
                self._cancel_hover()
                self._destroy_hover()
                return
        except tk.TclError:
            pass
        if not self._cursor_over_popup():
            self._cancel_hover()
            self._destroy_hover()
            return
        try:
            self._hover_poll_job = self.after(120, self._poll_cursor_off_popup)
        except tk.TclError:
            # Widget gone (e.g. tab destroyed); abandon the poll.
            self._hover_poll_job = None
            self._destroy_hover()

    def _destroy_hover(self):
        if self._hover_popup:
            try:
                self._hover_popup.destroy()
            except tk.TclError:
                pass
            self._hover_popup = None
            self._hover_photo_left = None
            self._hover_photo_right = None

    def _show_hover_preview(self, left_entry, right_entry):
        """Show a side-by-side popup with carousel image (left) and compare image (right)."""
        self._destroy_hover()
        try:
            from PIL import Image, ImageTk, ImageOps

            max_dim = 500

            def load_thumb(entry):
                if getattr(entry, "is_video", False):
                    # Hover preview uses the sync extract (sees cache after
                    # the main canvas warmed it). The async helper isn't
                    # right for hover — the popup is ephemeral, can't
                    # outlive a second render, and an on_done callback might
                    # land after the popup's parent canvas is gone. The
                    # main-canvas async (above) populates _VIDEO_THUMB_CACHE
                    # before any hover would fire, so this is fast in
                    # practice.
                    from .carousel_widget import _extract_video_first_frame
                    img = _extract_video_first_frame(entry.path)
                    if img is None:
                        # Fall back to a small black image rather than
                        # raising — the hover popup just shows a blank
                        # tile, which still beats crashing the popup.
                        img = Image.new("RGB", (max_dim, int(max_dim * 9 / 16)), "#000000")
                else:
                    # with/copy pattern — see _show_image_on_canvas comment
                    # above for the FD-leak/WinError 32 rationale.
                    with Image.open(entry.path) as _src:
                        _src.load()
                        img = _src.copy()
                img = ImageOps.exif_transpose(img)
                if entry.rotation and not getattr(entry, "is_video", False):
                    img = img.rotate(-entry.rotation, expand=True)
                ratio = min(max_dim / img.width, max_dim / img.height, 1.0)
                if ratio < 1.0:
                    img = img.resize(
                        (max(1, int(img.width * ratio)), max(1, int(img.height * ratio))),
                        Image.Resampling.LANCZOS,
                    )
                return img

            left_img = load_thumb(left_entry)
            right_img = load_thumb(right_entry)

            left_photo = ImageTk.PhotoImage(left_img)
            right_photo = ImageTk.PhotoImage(right_img)
            self._hover_photo_left = left_photo
            self._hover_photo_right = right_photo

            popup = tk.Toplevel(self)
            popup.overrideredirect(True)
            popup.attributes("-topmost", True)
            popup.config(bg=COLORS["bg_main"])

            container = tk.Frame(popup, bg=COLORS["bg_main"])
            container.pack(padx=2, pady=2)

            # Left: carousel active image
            left_frame = tk.Frame(container, bg=COLORS["bg_main"])
            left_frame.pack(side=tk.LEFT, padx=(0, 2))
            tk.Label(
                left_frame, text="Carousel", bg=COLORS["bg_main"],
                fg=COLORS["text_dim"], font=(FONT_FAMILY, 9),
            ).pack()
            tk.Label(
                left_frame, image=left_photo, bg=COLORS["bg_main"],
                bd=1, relief=tk.SOLID,
            ).pack()

            # Right: compare image
            right_frame = tk.Frame(container, bg=COLORS["bg_main"])
            right_frame.pack(side=tk.LEFT, padx=(2, 0))
            tk.Label(
                right_frame, text="Compare", bg=COLORS["bg_main"],
                fg=COLORS["text_dim"], font=(FONT_FAMILY, 9),
            ).pack()
            tk.Label(
                right_frame, image=right_photo, bg=COLORS["bg_main"],
                bd=1, relief=tk.SOLID,
            ).pack()

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

            # Clamp to screen edges
            sw = root.winfo_screenwidth()
            sh = root.winfo_screenheight()
            x = max(0, min(x, sw - pw))
            y = max(0, min(y, sh - ph))

            popup.geometry(f"+{x}+{y}")
            popup.bind("<Leave>", self._on_hover_leave)
            # Click on the popup is an explicit user intent to dismiss —
            # use the force-destroy bypass instead of `_on_hover_leave`
            # which would re-check `_cursor_over_popup` and refuse to
            # destroy on the very widget the click landed on (subagent
            # H2 round 2 on PR #83).
            popup.bind("<Button-1>", self._force_destroy_hover)
            self._hover_popup = popup
        except Exception as e:
            logger.debug("Compare hover preview error: %s", e)
