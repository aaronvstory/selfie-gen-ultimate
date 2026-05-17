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

        # Paths that have already failed to render. Prevents per-resize log
        # spam (Configure events re-fire _update_display) and avoids
        # re-tripping the same PIL recursion on the same bad PNG. Lazily
        # re-initialized inside _show_image_on_canvas for any test/instance
        # that bypassed __init__ via __new__, so we don't need a class-level
        # mutable default (which would leak failures across instances).
        self._render_failed_paths: set[str] = set()

        # NOTE: the sys.setrecursionlimit(5000) bump lives at the GUI entry
        # point (kling_gui/main_window.py module-level) so the side effect is
        # explicit and centralized rather than buried in a widget constructor.

        # Similarity computation state
        self._sim_lock = threading.Lock()
        self._sim_busy: bool = False
        self._auto_var = tk.BooleanVar(value=True)
        # Anti-spoof default OFF — user-confirmed preference. The standalone
        # similarity GUI defaults Anti-spoof ON for strict KYC use; the main
        # carousel runs in a more generative-content workflow where the
        # liveness signal usually isn't relevant.
        self._anti_spoof_var = tk.BooleanVar(value=False)
        # Show/hide green face-bounding-box overlay on the active carousel image.
        # ON by default — user-confirmed preference. Helpful at-a-glance for
        # the carousel preview; pure UI redraw on toggle, no recompute cost.
        self._show_face_box_var = tk.BooleanVar(value=True)
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

        # NOTE: "Boxes" face-bbox toggle now lives in meta_frame next to the
        # recalc button (v5.2 per user request) — see meta_frame build below.
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

        # NOTE: manual recompute "Recalc" button moved to meta_frame (next
        # to the SIM badge) as an icon-only ⟳ button in v5 per user request.
        # See meta_frame build below for the new widget. Comment kept as a
        # wayfinding marker for future editors.

        self.open_active_folder_btn = ttk.Button(
            sim_row,
            text="📂",
            style=TTK_BTN_COMPACT,
            width=2,
            command=debounce_command(self._on_open_active_image_folder, key="carousel_open_folder"),
        )
        self.open_active_folder_btn.pack(side=tk.LEFT, padx=(6, 0))

        # NOTE: All three toggle checkboxes (Auto, Anti-spoof, Boxes) moved
        # to a single dedicated bottom_row below meta_frame in v5.4 per user
        # request — see bottom_row build below. Sim_row now only carries
        # action buttons (★ Ref, Compare, 📂 folder).

        # v5.5 bottom row: ALL THREE toggle checkboxes RIGHT-ALIGNED on one
        # line: [..............☐ Auto | ☐ Anti-spoof | ☐ Boxes]. Packed
        # BEFORE meta_frame (both side=BOTTOM) so bottom_row sits BELOW
        # meta_frame — Tk stacks BOTTOM-packed widgets from bottom up in
        # pack-call order. pady=(0, 2) keeps the row compact so the carousel
        # canvas above gets maximum vertical real-estate. Defaults:
        # Auto=ON (auto-recalc), Anti-spoof=OFF (advisory opt-in),
        # Boxes=ON (face-bbox overlay).
        #
        # pack(side=tk.RIGHT) order is right-to-left visually, so to get
        # display order [Auto | Anti-spoof | Boxes] left-to-right, we pack
        # rightmost (Boxes) FIRST, then Anti-spoof, then Auto.
        bottom_row = tk.Frame(self.panel_frame, bg=COLORS["bg_panel"])
        bottom_row.pack(side=tk.BOTTOM, fill=tk.X, padx=8, pady=(0, 2))

        self._show_face_box_chk = tk.Checkbutton(
            bottom_row,
            text="Boxes",
            variable=self._show_face_box_var,
            command=self._on_show_face_box_toggle,
            font=(FONT_FAMILY, 8),
            bg=COLORS["bg_panel"],
            fg=COLORS["text_light"],
            selectcolor=COLORS["bg_input"],
            activebackground=COLORS["bg_panel"],
            activeforeground=COLORS["text_light"],
            padx=2,
        )
        self._show_face_box_chk.pack(side=tk.RIGHT, padx=(0, 0))

        self._anti_spoof_chk = tk.Checkbutton(
            bottom_row,
            text="Anti-spoof",
            variable=self._anti_spoof_var,
            command=self._on_anti_spoof_toggle,
            font=(FONT_FAMILY, 8),
            bg=COLORS["bg_panel"],
            fg=COLORS["text_light"],
            selectcolor=COLORS["bg_input"],
            activebackground=COLORS["bg_panel"],
            activeforeground=COLORS["text_light"],
            padx=2,
        )
        self._anti_spoof_chk.pack(side=tk.RIGHT, padx=(0, 8))

        self._auto_chk = tk.Checkbutton(
            bottom_row,
            text="Auto",
            variable=self._auto_var,
            font=(FONT_FAMILY, 8),
            bg=COLORS["bg_panel"],
            fg=COLORS["text_light"],
            selectcolor=COLORS["bg_input"],
            activebackground=COLORS["bg_panel"],
            activeforeground=COLORS["text_light"],
            padx=2,
        )
        self._auto_chk.pack(side=tk.RIGHT, padx=(0, 8))

        # Metadata row (resolution + filesize on left, similarity on right).
        # Packed AFTER bottom_row (both side=BOTTOM) so meta_frame sits
        # ABOVE bottom_row.
        self.meta_frame = tk.Frame(self.panel_frame, bg=COLORS["bg_panel"])
        self.meta_frame.pack(side=tk.BOTTOM, fill=tk.X, padx=8, pady=(0, 2))

        self.meta_label = tk.Label(
            self.meta_frame, text="", font=(FONT_FAMILY, 8),
            bg=COLORS["bg_panel"], fg=COLORS["text_dim"], anchor=tk.W,
        )
        self.meta_label.pack(side=tk.LEFT)

        # v5.4: meta_frame now ONLY carries the result chips [LIVE, SIM].
        # The Boxes/Auto/Anti-spoof checkboxes all live on bottom_row above.
        # The recalc button was removed in v5.3.

        # NOTE: Manual recalc button removed in v5.3 per user request — the
        # method recalc_all_similarity_now() below stays in place because the
        # auto-recalc paths (post-restore, post anti-spoof toggle, post
        # session-rebuild) still call it programmatically. Only the visible
        # button widget is gone. Visual order in meta_frame is now just
        # [LIVE | SIM | ☐ Boxes].

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

        # FAS (anti-spoof) PASS/FAIL chip — appears LEFT of sim_label when active.
        # v5: font + padding bumped to size 11 bold + padx=8 to match SIM badge
        # exactly. Previously this chip was font 8 + padx=4 which read as
        # "ugly tiny chip next to nice big SIM" — they should be a matched pair.
        # width=8 chars enforces equal visual footprint with "SIM 100%" / "LIVE ⚠".
        self.fas_label = tk.Label(
            self.meta_frame,
            text="",
            font=(FONT_FAMILY, 11, "bold"),
            bg=COLORS["bg_panel"],
            fg=COLORS["text_dim"],
            anchor=tk.CENTER,
            bd=0,
            relief=tk.FLAT,
            padx=0,
            pady=0,
            highlightthickness=0,
            width=8,
        )
        self.fas_label.pack(side=tk.RIGHT, padx=(0, 6))

        # v5.5: info_label is ALWAYS packed (constant row height) so the
        # canvas doesn't resize/jump when an image is added or removed.
        # When empty: shows "Add images to start" (replaces the
        # canvas-internal hint that previously rendered there). When
        # populated: shows "★ tag filename" of the active image.
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
        # Clear render-failure memo so re-added or re-saved paths get a fresh
        # try (the memo exists to stop per-resize spam, not to permanently
        # blacklist a path).
        self._render_failed_paths.clear()
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
            # v5.5: info_label stays packed (constant row height) and just
            # shows the "Add images to start" hint when empty so the canvas
            # above doesn't resize when images are added/removed. The
            # canvas itself is left blank in the empty state.
            self.info_label.config(text="Add images to start", fg=COLORS["text_dim"])
            self.meta_label.config(text="")
            self.sim_label.config(text="")
            return
        self.counter_label.config(text=f"{session.current_index + 1}/{n}")

        # Show the active image
        if entry and entry.exists:
            self._show_image_on_canvas(self.canvas, entry.path, "_photo")

            # Type-colored info label (line 1: tag + truncated filename).
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
            # FAS PASS/FAIL chip — only renders when the engine returned anti_spoofing data.
            self._render_fas_chip(getattr(entry, "similarity_diagnostics", None))
        elif entry:
            self.info_label.config(text="File not found", fg=COLORS["error"])
            self.meta_label.config(text="")
            self.sim_label.config(text="")
            self._render_fas_chip(None)

    def _render_fas_chip(self, diag):
        # Single source of truth for FAS verdict — see similarity_engine.summarize_fas_pair.
        # Three possible verdicts: pass / fail / unavailable. Same diag → same chip,
        # always (no more "ref+target sometimes, target only sometimes" drift).
        try:
            from similarity_engine import FaceEngine
            summary = FaceEngine.summarize_fas_pair(diag)
        except Exception:
            summary = {"verdict": "unavailable", "color_hint": "muted"}
        verdict = summary.get("verdict", "unavailable")
        if verdict == "unavailable":
            # Hide the chip entirely when FAS isn't assessable — full message
            # surfaces in the Processing Log instead, keeping the carousel tidy.
            self.fas_label.config(
                text="",
                bg=COLORS["bg_panel"],
                fg=COLORS["text_dim"],
                highlightthickness=0,
                padx=0,
            )
            return
        if verdict == "fail":
            # FAS is ADVISORY ONLY — amber chip, never red. The similarity
            # verdict is the actual gate; the chip says "heads-up, the
            # liveness model flagged this side", not "this comparison failed"
            # (codex + coderabbit on PR #19 reaffirmed advisory-only policy).
            text, fg, bg, border = "LIVE ⚠", "#3A2A00", "#FFC107", "#C99500"
        else:
            # v5: green colors aligned to SIM 100% palette (#0B5D1E bg /
            # #F4FFF6 fg / #2B8A3E border) so a passing LIVE chip and a
            # passing SIM badge read as a coherent matched pair instead of
            # two unrelated greens.
            text, fg, bg, border = "LIVE ✓", "#F4FFF6", "#0B5D1E", "#2B8A3E"
        # Padding matches SIM badge exactly (padx=8 pady=2) so the two chips
        # have identical height and visual weight.
        self.fas_label.config(
            text=text,
            fg=fg,
            bg=bg,
            highlightthickness=1,
            highlightbackground=border,
            highlightcolor=border,
            padx=8,
            pady=2,
        )

    # ── Image rendering helper ──────────────────────────────────────

    def _show_image_on_canvas(self, canvas: tk.Canvas, path: str, attr_name: str):
        """Load and display an image on a canvas, aspect-fitted with EXIF + rotation.

        When the 'Boxes' toggle is on, also draws a green rectangle around the
        face the engine selected for similarity scoring (read from the cached
        entry.face_bbox). The bbox is in original image coordinates so we
        scale by the same ratio used for the image fit.
        """
        # Skip paths we've already failed on — Configure events re-fire this
        # on every window resize, so without the memo a single bad PNG floods
        # the log and re-trips the same PIL recursion repeatedly.
        # Lazy-init for instances built via __new__ (test fixtures) so each
        # gets its own set instead of sharing a class-level mutable default.
        if not hasattr(self, "_render_failed_paths"):
            self._render_failed_paths = set()
        if path in self._render_failed_paths:
            cw = max(1, canvas.winfo_width())
            ch = max(1, canvas.winfo_height())
            canvas.create_text(
                cw // 2, ch // 2,
                text="(render skipped — see earlier error)",
                fill=COLORS["text_dim"],
                font=(FONT_FAMILY, 9),
            )
            return False

        # NOTE: recursion limit is bumped to 5000 once at GUI startup
        # (see kling_gui.main_window) — we no longer touch it per-render.
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

            # `ratio` is computed against the (post-EXIF, post-rotation) image
            # dimensions, which is also the coordinate space DeepFace's bbox is in.
            ratio = min(cw / img.width, ch / img.height)
            new_w = max(1, int(img.width * ratio))
            new_h = max(1, int(img.height * ratio))
            img = img.resize((new_w, new_h), Image.Resampling.LANCZOS)

            photo = ImageTk.PhotoImage(img, master=canvas)
            setattr(self, attr_name, photo)
            cx, cy = cw // 2 + 2, ch // 2 + 2
            canvas.create_image(cx, cy, image=photo, anchor=tk.CENTER)

            # Draw face bbox overlay if toggle is on AND we have a cached bbox.
            # NOTE: bbox is in pre-rotation, post-EXIF coordinates from DeepFace.
            # When the user has applied a manual rotation we skip the overlay
            # (would need full rotation math) — better to be silent than show
            # a misaligned box.
            # Guard against test stubs that bypass _build_panel and don't have the var.
            show_box_var = getattr(self, "_show_face_box_var", None)
            if show_box_var is not None and show_box_var.get() and entry and not entry.rotation:
                bbox = getattr(entry, "face_bbox", None)
                if isinstance(bbox, dict):
                    try:
                        bx = int(bbox.get("x", 0))
                        by = int(bbox.get("y", 0))
                        bw = int(bbox.get("w", 0))
                        bh = int(bbox.get("h", 0))
                    except (TypeError, ValueError):
                        bx = by = bw = bh = 0
                    if bw > 0 and bh > 0:
                        # Project original-image coords into on-canvas coords.
                        # The PIL image was placed CENTER-anchored at (cx, cy)
                        # with dimensions (new_w, new_h), so its top-left corner
                        # on the canvas is (cx - new_w/2, cy - new_h/2).
                        img_left = cx - new_w / 2
                        img_top = cy - new_h / 2
                        x0 = img_left + bx * ratio
                        y0 = img_top + by * ratio
                        x1 = img_left + (bx + bw) * ratio
                        y1 = img_top + (by + bh) * ratio
                        # Clamp to image bounds so partial-detection bboxes stay tidy.
                        x0 = max(img_left, min(img_left + new_w, x0))
                        y0 = max(img_top, min(img_top + new_h, y0))
                        x1 = max(img_left, min(img_left + new_w, x1))
                        y1 = max(img_top, min(img_top + new_h, y1))
                        canvas.create_rectangle(
                            x0, y0, x1, y1,
                            outline="#48DB7A",  # brighter green for visibility
                            width=3,
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
        except RecursionError as e:
            # Memo this path so we don't retry on every window resize.
            self._render_failed_paths.add(path)
            cw = max(1, canvas.winfo_width())
            ch = max(1, canvas.winfo_height())
            canvas.create_text(
                cw // 2, ch // 2,
                text="Cannot load: image too deeply nested (PIL recursion)",
                fill=COLORS["error"],
                font=(FONT_FAMILY, 9),
            )
            self.log(
                f"Carousel render failed: {os.path.basename(path)} (RecursionError — path memoed, will not retry)",
                "error",
            )
            logger.exception("Carousel render failed path=%s (RecursionError)", path)
            return False
        except Exception as e:
            # Memo to avoid re-spam on Configure events; non-recursion failures
            # are usually permanent (corrupt file, wrong format) for the same path.
            self._render_failed_paths.add(path)
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
        """Thread-safe log wrapper for similarity — routes to Processing Log.

        face_similarity._log already prefixes its messages with "Sim: ", so we
        only add the prefix when the message arrived without one (e.g. our own
        carousel-internal calls like "anti-spoof = True").
        """
        prefixed = msg if msg.startswith("Sim:") else f"Sim: {msg}"
        self.after(0, lambda: self.log(prefixed, lvl))

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
        # Stash full diagnostics so the FAS chip can render PASS/FAIL alongside the score.
        diag = (details or {}).get("diagnostics") if details else None
        diag_dict = diag if isinstance(diag, dict) else None
        setattr(entry, "similarity_diagnostics", diag_dict)
        # Cache the per-image face bbox for the optional bbox overlay toggle.
        # `entry` is always the TARGET in the comparison call; the REF entry gets
        # its own bbox written via the ref-side branch below.
        if diag_dict:
            boxes = diag_dict.get("selected_face_boxes") or {}
            target_box = boxes.get("target") if isinstance(boxes, dict) else None
            ref_box = boxes.get("ref") if isinstance(boxes, dict) else None
            setattr(entry, "face_bbox", target_box if isinstance(target_box, dict) else None)
            # Push the ref-side bbox onto the ref entry too — single comparison
            # produces both, no point recomputing later.
            ref_entry, _ref_source = self.image_session.get_effective_similarity_ref()
            if ref_entry is not None and isinstance(ref_box, dict):
                setattr(ref_entry, "face_bbox", ref_box)
        else:
            setattr(entry, "face_bbox", None)
        self.image_session._notify()
        if score is not None:
            fas_chip = self._fas_summary_from_diag(diag)
            self._sim_log(f"result: {score}%{(' ' + fas_chip) if fas_chip else ''}", "info")

    @staticmethod
    def _fas_summary_from_diag(diag):
        """Return a short 'liveness=… (ref=X% real, target=Y% real)' string.

        Routes through similarity_engine.summarize_fas_pair so the Processing Log,
        the LIVE chip, and the standalone GUI all agree on the verdict and scores.
        """
        try:
            from similarity_engine import FaceEngine
            summary = FaceEngine.summarize_fas_pair(diag)
        except Exception:
            return ""
        verdict = summary.get("verdict", "unavailable")
        if verdict == "unavailable":
            return ""
        verdict_word = "PASS" if verdict == "pass" else "advisory"
        # Use the engine-interpreted real_conf (already folds is_real into the
        # score), NOT the raw antispoof_score — otherwise spoofs flagged with
        # is_real=False, antispoof_score=0.99 log as "99% real" instead of
        # "1% real" (coderabbit finding on PR #19, same class of bug as the
        # standalone GUI had before the v4 engine fix). Fall back to raw
        # ref_score only if real_conf is missing for some reason — keeps
        # back-compat with any older diagnostics blocks.
        ref_real_conf = summary.get("ref_real_conf")
        tgt_real_conf = summary.get("target_real_conf")
        if not isinstance(ref_real_conf, (int, float)):
            ref_real_conf = summary.get("ref_score")
        if not isinstance(tgt_real_conf, (int, float)):
            tgt_real_conf = summary.get("target_score")
        score_bits = []
        if isinstance(ref_real_conf, (int, float)):
            score_bits.append(f"ref={float(ref_real_conf) * 100:.1f}% real")
        if isinstance(tgt_real_conf, (int, float)):
            score_bits.append(f"target={float(tgt_real_conf) * 100:.1f}% real")
        if score_bits:
            return f"liveness={verdict_word} ({', '.join(score_bits)})"
        return f"liveness={verdict_word}"

    def _on_show_face_box_toggle(self):
        """Toggle the face-bbox overlay. Pure redraw — no recompute."""
        # Clear the cached PhotoImage attrs so _show_image_on_canvas re-renders
        # with the new overlay state on the next _update_panel pass.
        if hasattr(self, "_photo"):
            try:
                delattr(self, "_photo")
            except AttributeError:
                pass
        self._update_display()

    def _on_anti_spoof_toggle(self):
        """Apply checkbox state to the engine and recompute scores in-place."""
        try:
            from face_similarity import _get_engine
            engine = _get_engine(report_cb=self._sim_log)
            if engine is not None:
                engine.anti_spoofing = bool(self._anti_spoof_var.get())
                self._sim_log(
                    f"anti-spoof = {engine.anti_spoofing} (recomputing all)", "info"
                )
        except Exception as exc:
            self._sim_log(f"anti-spoof toggle failed: {exc!r}", "warning")
            return
        # Trigger a fresh batch recompute so the displayed scores reflect the new setting.
        self._calc_all_similarity(auto_triggered=False, reason="anti-spoof toggle")

    def recalc_all_similarity_now(self, reason: str = "manual recalc") -> bool:
        """Public entry to force a batch similarity recompute.

        Returns True if a recompute was kicked off, False if there was nothing to do
        (no reference image, no targets, etc). Always emits a Processing Log line so
        the user can see *why* a request didn't recompute.
        """
        return self._calc_all_similarity(auto_triggered=False, reason=reason)

    def _calc_all_similarity(self, auto_triggered: bool = False, reason: str = "manual recalc") -> bool:
        """Compute similarity for all non-ref generated images. Returns True if work started.

        `auto_triggered` is preserved in the signature for callers that distinguish
        auto vs manual triggers; the log line is now always at info level so the
        Processing Log shows recompute activity in both cases.
        """
        del auto_triggered  # keep signature stable; opt out of unused-parameter warning
        ref, ref_source = self.image_session.get_effective_similarity_ref()
        if not ref:
            self._sim_log(
                f"recalc skipped ({reason}): no similarity reference set — pick one with the ★ Ref button.",
                "warning",
            )
            return False
        targets = [e for e in self.image_session.images
                   if e.source_type != "input" and e is not ref and e.exists]
        if not targets:
            self._sim_log(
                f"recalc skipped ({reason}): no eligible targets (need at least one generated/outpaint image other than the reference).",
                "warning",
            )
            return False
        ref_name = os.path.basename(ref.path)
        source_label = {
            "manual_star_ref": "manual ★ Ref",
            "auto_crop": "auto crop fallback",
            "auto_front": "auto front fallback",
            "auto_first_input": "auto first-input fallback",
            "none": "none",
        }.get(ref_source, ref_source or "unknown")
        # Always log batch start at info level so the user can see recompute activity in
        # the Processing Log (the previous "debug only when manual" gating made the new
        # post-restore recompute completely invisible).
        self._sim_log(
            f"batch start: {len(targets)} images, ref={ref_name}, source={source_label}, reason={reason}",
            "info",
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
        return True

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
