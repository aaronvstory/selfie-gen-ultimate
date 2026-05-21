"""Image Carousel Widget — unified carousel showing all images with hover preview."""

import tkinter as tk
from tkinter import ttk
from typing import Callable, Optional
import os
import platform
import logging
import threading
import subprocess
from pathlib import Path

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
from .image_state import ImageSession, _VIDEO_EXTENSIONS as _CAROUSEL_VIDEO_EXTS
from .tag_utils import derive_display_tag
from .video_discovery import find_video_for_image
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


# ----------------------------------------------------------------------
# Video thumbnail helper. Used by _show_image_on_canvas to render a
# carousel item whose underlying file is a video (session-folder rescan
# adds these). Cache is keyed by absolute path so the carousel doesn't
# re-decode on every resize Configure event. FIFO-capped to bound
# memory — each entry is a PIL.Image of the first frame (~2-3 MB at
# 1280×720 RGB), so an unbounded cache leaked 100s of MB over long
# sessions. Mirrors the _BEST_VIDEO_CACHE cap pattern in video_discovery.
#
# THREAD-SAFETY: ``_extract_video_first_frame`` is called from BOTH the
# Tk thread (synchronously in ``_show_image_on_canvas``) and decoder
# daemon threads (via ``_extract_video_first_frame_async``). The dict
# check-then-pop FIFO eviction is not atomic, so without a lock two
# threads racing the eviction can ``RuntimeError: dictionary changed
# size during iteration`` or one ``KeyError``s the other's pop. The
# lock guards every mutation of the cache (read-only ``.get`` is safe
# without it under CPython GIL — single bytecode op).
# (Gemini HIGH on 9d9a473.)
# ----------------------------------------------------------------------
_VIDEO_THUMB_CACHE: "dict[str, object]" = {}
_VIDEO_THUMB_CACHE_MAX = 64
_VIDEO_THUMB_CACHE_LOCK = threading.Lock()


def _extract_video_first_frame(video_path: str):
    """Return the first frame of a video as a PIL.Image, or None on failure.

    Uses cv2 lazily — wheel availability on macOS-ARM has historically
    lagged, so a missing cv2 falls back to None (caller renders a generic
    placeholder). Result is cached per path; a video rewritten in place
    would serve a stale thumbnail until the process restarts, which is
    acceptable for the derived-output workflow this targets.
    """
    # M1 fix (subagent on 2eb16f37): key on (path, mtime) so an
    # in-place regen (Kling queue overwrites a take, oldcam re-runs)
    # invalidates the thumbnail. mtime stat is cheap; the alternative
    # (stale thumb until process restart) was a real UX bug.
    try:
        mtime = os.path.getmtime(video_path)
    except OSError:
        mtime = 0  # missing file — the cv2 open below will None-out anyway
    cache_key = (video_path, mtime)
    # Guard the read with the same lock as the eviction/insert path
    # below. Single dict.get is GIL-atomic on simple lookups, but if
    # another thread is mid-eviction (the FIFO pop in the write path
    # below) the dict's internal hash table could be momentarily
    # inconsistent. Holding the lock for the read is cheap (the lock
    # itself is uncontended ~99.99% of the time) and removes the
    # last theoretical race. (Gemini MEDIUM on be30379.)
    with _VIDEO_THUMB_CACHE_LOCK:
        cached = _VIDEO_THUMB_CACHE.get(cache_key)
    if cached is not None:
        return cached
    try:
        import cv2
        from PIL import Image
    except ImportError:
        return None
    cap = None
    try:
        cap = cv2.VideoCapture(video_path)
        if not cap.isOpened():
            return None
        ok, frame = cap.read()
        if not ok or frame is None:
            return None
        rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
        img = Image.fromarray(rgb)
        # FIFO evict oldest entries until strictly under cap. dict
        # preserves insertion order in Python 3.7+ so next(iter(...))
        # is the oldest. While-loop (not if-single-eviction) so a
        # bulk folder rescan that inserts N>1 entries without
        # interleaved reads can't temporarily exceed the cap.
        # (Gemini medium PR #43, finding 3277077712; lock added per
        # Gemini HIGH on 9d9a473 — Tk thread + decoder thread both
        # mutate this dict.)
        with _VIDEO_THUMB_CACHE_LOCK:
            while len(_VIDEO_THUMB_CACHE) >= _VIDEO_THUMB_CACHE_MAX:
                try:
                    _VIDEO_THUMB_CACHE.pop(next(iter(_VIDEO_THUMB_CACHE)))
                except (StopIteration, KeyError):
                    break
            _VIDEO_THUMB_CACHE[cache_key] = img
        return img
    except Exception:
        logger.debug("video-thumb extract failed for %s", video_path, exc_info=True)
        return None
    finally:
        if cap is not None:
            try:
                cap.release()
            except Exception:
                logger.debug("video-thumb cap.release() failed for %s", video_path, exc_info=True)


# Tracks paths whose async decode is in-flight so we don't spawn a second
# worker for the same path before the first lands. MOSTLY Tk-thread —
# add() at the spawn site and discard() inside _finish() (after()-
# dispatched, Tk thread). HOWEVER the worker's exception-fallback path
# also discards (line ~186) so the widget can re-spawn after a
# widget-destroyed-mid-decode case. That fallback runs on the WORKER
# thread, so cross-thread mutation IS possible — a lock is required to
# keep ``add()``+``discard()``+``in`` atomic per path.
# (Gemini MEDIUM on 19fc413.)
_VIDEO_THUMB_PENDING: "set[str]" = set()
_VIDEO_THUMB_PENDING_LOCK = threading.Lock()


def _extract_video_first_frame_async(
    video_path: str,
    widget,
    on_done,
) -> bool:
    """Decode a video's first frame off-thread.

    Returns ``True`` if a decode was started, ``False`` if cached (caller
    can just use ``_extract_video_first_frame`` synchronously) or if a
    decode is already in flight for this path (caller should render the
    placeholder and let the prior decode finish).

    ``on_done`` runs on the Tk thread via ``widget.after(0, ...)`` with
    the PIL.Image (or None on failure) as its single argument. The
    callback should re-render the carousel — the now-cached frame will
    serve via the sync helper next pass. Wrapped in try/TclError so a
    widget destroyed mid-decode doesn't raise into the Tk loop.

    Addresses Gemini PR #43 finding: cv2.VideoCapture on the Tk thread
    froze the UI for 100-500ms on first render of each unique video.
    """
    try:
        _vt_mtime = os.path.getmtime(video_path)
    except OSError:
        _vt_mtime = 0
    # Lock the cache membership check; same rationale as the .get()
    # in _extract_video_first_frame (Gemini MEDIUM on be30379).
    with _VIDEO_THUMB_CACHE_LOCK:
        if (video_path, _vt_mtime) in _VIDEO_THUMB_CACHE:
            return False
    # Atomic check-then-add: must be inside the lock to prevent a
    # racing worker-thread discard() (the widget-destroyed-mid-decode
    # branch below) from clearing the marker between the check and
    # the add (Gemini MEDIUM on 19fc413).
    with _VIDEO_THUMB_PENDING_LOCK:
        if video_path in _VIDEO_THUMB_PENDING:
            return False
        _VIDEO_THUMB_PENDING.add(video_path)

    def _worker():
        img = _extract_video_first_frame(video_path)
        def _finish():
            with _VIDEO_THUMB_PENDING_LOCK:
                _VIDEO_THUMB_PENDING.discard(video_path)
            try:
                on_done(img)
            except tk.TclError:
                pass  # widget destroyed mid-decode
            except Exception:
                logger.debug("async-thumb on_done failed", exc_info=True)
        try:
            widget.after(0, _finish)
        except (tk.TclError, RuntimeError):
            # Widget destroyed between spawn and dispatch — drop the
            # pending marker so a later open of the same file can retry.
            # This is the WORKER-thread mutation that motivates the lock.
            with _VIDEO_THUMB_PENDING_LOCK:
                _VIDEO_THUMB_PENDING.discard(video_path)

    threading.Thread(
        target=_worker, name=f"video-thumb:{os.path.basename(video_path)}",
        daemon=True,
    ).start()
    return True


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

        # Video Inspector callbacks (set by main_window). Two distinct
        # entry points: clicking the carousel play-badge passes the
        # selected video path; clicking the toolbar button opens the
        # inspector with no preload (factory picks last folder).
        self._on_video_callback: Optional[Callable[[Path], None]] = None
        self._on_video_inspector_toolbar_cb: Optional[Callable[[], None]] = None

        # Re-entrancy guard
        self._updating: bool = False

        # Paths that have already failed to render. Prevents per-resize log
        # spam (Configure events re-fire _update_display) and avoids
        # re-tripping the same PIL recursion on the same bad PNG. Lazily
        # re-initialized inside _show_image_on_canvas for any test/instance
        # that bypassed __init__ via __new__, so we don't need a class-level
        # mutable default (which would leak failures across instances).
        # Keyed by (path, mtime) so a clip that fails on first decode
        # (still being written, transient read error) re-tries after
        # the file is updated — without keying on mtime, a permanent
        # blacklist would persist even after the file becomes valid.
        # (Codex P2 on 9d9a473.)
        self._render_failed_paths: set[tuple[str, float]] = set()

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

    def set_on_video(self, callback: Callable[[Path], None]):
        """Register the callback invoked when the carousel play-badge is
        clicked. The callback receives the video Path to preload into
        the Video Inspector's slot A."""
        self._on_video_callback = callback

    def set_on_video_toolbar(self, callback: Callable[[], None]):
        """Register the callback invoked when the Videos toolbar button
        is clicked. The Video Inspector opens with no preload (it falls
        back to the last-opened folder via config)."""
        self._on_video_inspector_toolbar_cb = callback

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

        # ttk.Button (not tk.Button) per the b3bc7398 follow-up: raw
        # tk.Button loses its tint after the first macOS Aqua HIView
        # repaint. Custom styles defined in main_window._setup_ui swap
        # between active (yellow) and inactive (panel-bg) state via
        # _update_panel.
        self._ref_btn = ttk.Button(
            sim_row,
            text="\u2605 Ref",
            command=debounce_command(self._toggle_sim_ref, key="carousel_ref"),
            state=tk.DISABLED,
            width=10,
            style="CarouselRefInactive.TButton",
        )
        self._ref_btn.pack(side=tk.LEFT)

        self.compare_btn = ttk.Button(
            sim_row,
            text="Compare",
            command=debounce_command(self._on_compare, key="carousel_compare"),
            state=tk.DISABLED,
            width=10,
            style=TTK_BTN_SECONDARY,
        )
        self.compare_btn.pack(side=tk.LEFT, padx=(6, 0))

        # Videos button — opens the Video Inspector modal. Mirrors the
        # Compare button recipe (same colors, same macOS button fix) so
        # they sit visually identical in the sim_row. Always enabled —
        # the modal handles the empty-folder case gracefully.
        self.video_inspector_btn = ttk.Button(
            sim_row,
            text="Videos",
            command=debounce_command(
                self._on_open_video_inspector, key="carousel_video_inspector"
            ),
            width=10,
            style=TTK_BTN_SECONDARY,
        )
        self.video_inspector_btn.pack(side=tk.LEFT, padx=(6, 0))

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
            font=(FONT_FAMILY, 9),
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
            font=(FONT_FAMILY, 9),
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
            font=(FONT_FAMILY, 9),
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
            self.meta_frame, text="", font=(FONT_FAMILY, 9),
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
            font=(FONT_FAMILY, 9),
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
        # blacklist a path). Guard hasattr() so __new__-constructed test
        # instances that bypass __init__ don't AttributeError here.
        if hasattr(self, "_render_failed_paths"):
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
            # H3: video carousel items can't be a similarity ref — disable
            # the button on those entries. (_calc_all_similarity also guards
            # the recalc path, but the disabled state is a better affordance.)
            # ttk.Button dynamic styling via style= swap (preserves dark
            # tint through macOS HIView repaints). The two style names
            # are defined in main_window._setup_ui alongside TTK_BTN_*.
            if entry is not None and entry.is_video:
                self._ref_btn.configure(
                    state=tk.DISABLED, text="\u2605 Ref",
                    style="CarouselRefInactive.TButton",
                )
            elif is_manual_ref:
                self._ref_btn.configure(
                    state=tk.NORMAL, text="\u2605 Clear",
                    style="CarouselRefActive.TButton",
                )
            else:
                self._ref_btn.configure(
                    state=tk.NORMAL, text="\u2605 Ref",
                    style=("CarouselRefActive.TButton" if is_effective_ref
                           else "CarouselRefInactive.TButton"),
                )
        else:
            self._ref_btn.configure(
                state=tk.DISABLED, text="\u2605 Ref",
                style="CarouselRefInactive.TButton",
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
        #
        # User-visibility gate (PR #43 feedback): if the Anti-spoof
        # checkbox is OFF, hide the chip entirely even when stored
        # diagnostics from a prior run contain liveness data. The chip's
        # presence implies "we're actively checking liveness right
        # now" — surfacing a stale LIVE/FAIL when the user has the
        # feature toggled off is confusing and triggered this exact
        # report. (The diagnostics dict stays cached so re-enabling
        # the checkbox surfaces the chip again without recomputing.)
        anti_spoof_var = getattr(self, "_anti_spoof_var", None)
        if anti_spoof_var is not None and not bool(anti_spoof_var.get()):
            self.fas_label.config(
                text="",
                bg=COLORS["bg_panel"],
                fg=COLORS["text_dim"],
                highlightthickness=0,
                padx=0,
            )
            return
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
        # Compute the mtime once so the failure check + the eventual
        # ``add()`` use the same key. If the file is gone, treat mtime=0
        # (matches what the thumb-cache fallback uses below).
        try:
            _fail_mtime = os.path.getmtime(path)
        except OSError:
            _fail_mtime = 0.0
        _fail_key = (path, _fail_mtime)
        if _fail_key in self._render_failed_paths:
            # Clear any stale items (prior image, bbox overlay, etc.) so the
            # placeholder is the only thing on this canvas. Mirrors what the
            # successful-render path does implicitly via create_image replacing
            # the prior content + the size change clearing overlays.
            canvas.delete("all")
            cw = max(1, canvas.winfo_width())
            ch = max(1, canvas.winfo_height())
            # For memoized VIDEO failures keep the play-glyph placeholder +
            # click-to-inspector binding (M3 fix — the user can still
            # click through to the Inspector which has its own external-
            # player fallback). For still-image failures keep the text
            # placeholder so the user sees there was an earlier error.
            ext = os.path.splitext(path)[1].lower()
            if ext in _CAROUSEL_VIDEO_EXTS:
                canvas.create_rectangle(
                    2, 2, cw - 2, ch - 2,
                    fill=COLORS["bg_input"], outline="",
                )
                canvas.create_text(
                    cw // 2, ch // 2 - 14,
                    text="▶", fill="#FFFFFF",
                    font=(FONT_FAMILY, 48, "bold"),
                )
                canvas.create_text(
                    cw // 2, ch // 2 + 24,
                    text=os.path.basename(path),
                    fill=COLORS["text_light"],
                    font=(FONT_FAMILY, 9),
                )
                self._bind_video_canvas_click(canvas, path)
                return True
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

            # Video carousel item: render the first frame via cv2 and
            # overlay a big centered ▶ (drawn later). Bbox + corner
            # play-badge are skipped — they presume a still-image source
            # and a derived video; this entry IS the video.
            ext = os.path.splitext(path)[1].lower()
            is_video = ext in _CAROUSEL_VIDEO_EXTS
            if is_video:
                # Try the in-memory cache first (instant on resize/navigate-
                # back). On miss, render the placeholder + ▶ NOW and kick
                # off a background decode — the carousel stays responsive
                # while cv2 decodes (100-500ms typical) and re-renders
                # automatically when the frame lands.
                try:
                    _vt_mtime = os.path.getmtime(path)
                except OSError:
                    _vt_mtime = 0
                # Locked read — same rationale as the helper above
                # (Gemini MEDIUM on be30379).
                with _VIDEO_THUMB_CACHE_LOCK:
                    img = _VIDEO_THUMB_CACHE.get((path, _vt_mtime))
                if img is None:
                    # CR Major (PR #43, bot pass on 907da866): the async
                    # decode can return None for corrupt/unsupported clips
                    # or when cv2 is missing. Without memoizing the failure
                    # here, the rerender triggered by on_done would spawn
                    # another worker for the same path — infinite loading
                    # spinner. Add the path to _render_failed_paths so the
                    # next render hits the early-return placeholder branch.
                    def _on_thumb_ready(_img, _p=path, _m=_vt_mtime):
                        if _img is None:
                            # Key by (path, mtime) so the next regen of
                            # the same path (e.g. queue worker overwrote
                            # the failed take) clears the blacklist.
                            self._render_failed_paths.add((_p, _m))
                        self._update_display()
                    started = _extract_video_first_frame_async(
                        path, canvas,
                        on_done=_on_thumb_ready,
                    )
                    if started or path in _VIDEO_THUMB_PENDING:
                        # Render placeholder for this pass; the async
                        # on_done will trigger _update_display when ready.
                        canvas.delete("all")
                        cw_pl = max(1, canvas.winfo_width())
                        ch_pl = max(1, canvas.winfo_height())
                        canvas.create_rectangle(
                            2, 2, cw_pl - 2, ch_pl - 2,
                            fill=COLORS["bg_input"], outline="",
                        )
                        canvas.create_text(
                            cw_pl // 2, ch_pl // 2 - 14,
                            text="▶", fill="#FFFFFF",
                            font=(FONT_FAMILY, 48, "bold"),
                        )
                        canvas.create_text(
                            cw_pl // 2, ch_pl // 2 + 24,
                            text=os.path.basename(path) + "  (loading…)",
                            fill=COLORS["text_light"],
                            font=(FONT_FAMILY, 9),
                        )
                        self._bind_video_canvas_click(canvas, path)
                        return True
                if img is None:
                    # cv2 unavailable OR decode failed. Show a neutral
                    # placeholder + ▶ so the user still sees that a
                    # video lives here — clicking opens the Inspector
                    # which has its own external-player fallback.
                    # M3 (code-review): memoize the failure so corrupt-
                    # header videos don't re-open VideoCapture on every
                    # Configure event. Key by (path, _vt_mtime) so a
                    # later regen of the same path is retried instead
                    # of being permanently blacklisted (Codex P2 on
                    # 9d9a473).
                    self._render_failed_paths.add((path, _vt_mtime))
                    canvas.delete("all")
                    cw_pl = max(1, canvas.winfo_width())
                    ch_pl = max(1, canvas.winfo_height())
                    canvas.create_rectangle(
                        2, 2, cw_pl - 2, ch_pl - 2,
                        fill=COLORS["bg_input"], outline="",
                    )
                    canvas.create_text(
                        cw_pl // 2, ch_pl // 2 - 14,
                        text="▶", fill="#FFFFFF",
                        font=(FONT_FAMILY, 48, "bold"),
                    )
                    canvas.create_text(
                        cw_pl // 2, ch_pl // 2 + 24,
                        text=os.path.basename(path),
                        fill=COLORS["text_light"],
                        font=(FONT_FAMILY, 9),
                    )
                    self._bind_video_canvas_click(canvas, path)
                    return True
            else:
                with Image.open(path) as img_src:
                    img_src.load()
                    img = img_src.copy()

            # Auto-correct EXIF orientation (no-op on cv2-derived frames)
            img = ImageOps.exif_transpose(img)

            # Apply user rotation (stored on the active entry).
            # Videos use their natural orientation — rotation is an
            # image-edit concept and the carousel doesn't expose it for
            # video clips.
            entry = self.image_session.active_entry
            if entry and entry.rotation and not is_video:
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
            if not is_video and show_box_var is not None and show_box_var.get() and entry and not entry.rotation:
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
                            # width=3 was visually heavy per user
                            # feedback on PR #43; 1.5px (rounded to 2 on
                            # Tk's integer pen width) reads as a thin,
                            # crisp outline without obscuring face
                            # features.
                            width=2,
                        )

            # Video Inspector play-badge overlay. Draws a circular play
            # button in the top-right corner of the fitted image rect
            # when the active image has at least one derived video in
            # the same folder. Click on the badge opens the inspector
            # modal pre-loaded with the most-processed variant.
            #
            # tag_bind (item-scoped) is used instead of canvas.bind so
            # the existing <Button-3> global binding (right-click) is
            # not affected. Re-drawn unconditionally on every render
            # so re-resize/re-select continues to expose the badge.
            try:
                video_path = find_video_for_image(Path(path))
            except (OSError, ValueError) as exc:
                # OSError covers folder-stat / permission / I/O issues
                # against the parent dir; ValueError catches malformed
                # paths. Anything else propagates (bug we should see).
                logger.debug(
                    "video_inspector: discovery failed for %s: %s",
                    path, exc,
                )
                video_path = None
            if not is_video and video_path is not None and self._on_video_callback is not None:
                # Badge sizing scales with the visible image rect — a
                # fixed 18px radius looks dwarfed on a large thumbnail
                # and grotesque on a small one. Use ~7% of the smaller
                # image dimension, clamped to [12, 22] so a tiny preview
                # still shows a clickable target and a huge preview
                # doesn't get a giant black blob. Margin from edge
                # scales with the radius so the badge never bleeds
                # past the image rect.
                short_dim = min(new_w, new_h)
                badge_r = max(12, min(22, int(short_dim * 0.07)))
                margin = badge_r + 4
                badge_x = cx + new_w / 2 - margin
                badge_y = cy - new_h / 2 + margin
                # Explicit integer rounding because create_oval with
                # float coords can sub-pixel-align differently on
                # different platforms (causing visible asymmetry on
                # Windows Aqua). Force integer math.
                x0, y0 = int(badge_x - badge_r), int(badge_y - badge_r)
                x1, y1 = int(badge_x + badge_r), int(badge_y + badge_r)
                bg_id = canvas.create_oval(
                    x0, y0, x1, y1,
                    fill="#000000", outline="#FFFFFF", width=2,
                )
                # ▶ glyph: font size proportional to badge radius so
                # the play arrow stays visually balanced inside the
                # circle regardless of thumbnail size. No extra x-nudge
                # — Tk's CENTER anchor positions the glyph correctly
                # by its baseline+halfwidth; the prior `+2` was
                # over-correcting for the asymmetric U+25B6 right-edge
                # whitespace and was making the badge LOOK lopsided.
                play_font_size = max(10, int(badge_r * 1.0))
                play_id = canvas.create_text(
                    int(badge_x), int(badge_y),
                    text="▶",
                    font=(FONT_FAMILY, play_font_size, "bold"),
                    fill="#FFFFFF",
                )
                cb = self._on_video_callback
                vp = video_path
                canvas.tag_bind(
                    bg_id, "<Button-1>", lambda _e, p=vp: cb(p),
                )
                canvas.tag_bind(
                    play_id, "<Button-1>", lambda _e, p=vp: cb(p),
                )

            # Big centered ▶ overlay + canvas-wide click binding for
            # video carousel items. Drawn AFTER the main image so it sits
            # on top. Click anywhere on the canvas opens the Inspector.
            if is_video and self._on_video_callback is not None:
                short_dim = min(new_w, new_h)
                glyph_size = max(36, min(96, int(short_dim * 0.22)))
                canvas.create_text(
                    cx, cy,
                    text="▶", fill="#FFFFFF",
                    font=(FONT_FAMILY, glyph_size, "bold"),
                )
                self._bind_video_canvas_click(canvas, path)
            elif not is_video:
                # Non-video item: clear any stale video click binding
                # (this canvas may have just shown a video, then the
                # user navigated to an image). AttributeError catches
                # _FakeCanvas test stubs that don't implement unbind.
                try:
                    canvas.unbind("<Button-1>")
                except (tk.TclError, AttributeError):
                    pass
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
            # Key by (path, mtime) for symmetry with the video-thumb
            # failure path (Codex P2 on 9d9a473).
            try:
                _rec_mtime = os.path.getmtime(path)
            except OSError:
                _rec_mtime = 0.0
            self._render_failed_paths.add((path, _rec_mtime))
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
            # NOTE: we deliberately do NOT memo generic exceptions here.
            # Render errors can be transient (file being written, briefly
            # locked, partial download) and a permanent skip would leave a
            # never-recovering placeholder. Only RecursionError (above) is
            # treated as deterministic and added to _render_failed_paths.
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

    def _on_open_video_inspector(self):
        """Internal handler for the Videos toolbar button."""
        if self._on_video_inspector_toolbar_cb:
            self._on_video_inspector_toolbar_cb()

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
        active_entry = session.active_entry
        # H3 (code-review 2026-05-20): videos cannot be a similarity ref
        # — they have no face to score against. Refuse + log.
        if active_entry is not None and active_entry.is_video:
            self.log(
                "Similarity ref cannot be a video — pick a still image.",
                "warning",
            )
            return
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
            name = active_entry.filename if active_entry else "?"
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

    def _bind_video_canvas_click(self, canvas, video_path):
        """Bind a canvas-wide click handler that opens the Video Inspector.

        Used by video carousel items so clicking anywhere on the rendered
        first-frame thumbnail launches playback in the Inspector. The bind
        is per-canvas-per-render („unbind” on the non-video branch in
        _show_image_on_canvas clears it when the user navigates back to an
        image item) so stale clips never trigger the wrong video.
        """
        cb = self._on_video_callback
        if cb is None:
            return
        from pathlib import Path as _Path
        p = _Path(video_path)
        try:
            canvas.unbind("<Button-1>")
        except (tk.TclError, AttributeError):
            pass
        try:
            canvas.bind("<Button-1>", lambda _e, _p=p: cb(_p))
        except (tk.TclError, AttributeError):
            pass

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
        # Immediately hide/show the LIVE chip without waiting for the
        # recompute pass to land — toggling OFF should be instant
        # feedback. _render_fas_chip itself short-circuits when the
        # checkbox is off.
        active = self.image_session.active_entry if hasattr(self, "image_session") else None
        diag = getattr(active, "similarity_diagnostics", None) if active else None
        self._render_fas_chip(diag)
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
        # H3 (code-review 2026-05-20): videos cannot be a similarity ref.
        # get_effective_similarity_ref filters on source_type=="input" for
        # the auto-fallback path, but the MANUAL ref slot does not — if a
        # user somehow set a video as manual ref (programmatically or via a
        # stale session), refuse the recalc with a clear message rather than
        # feeding a video path into compute_face_similarity_details.
        if ref.is_video:
            self._sim_log(
                f"recalc skipped ({reason}): similarity reference is a video — pick a still image.",
                "warning",
            )
            return False
        targets = [e for e in self.image_session.images
                   if e.source_type != "input" and not e.is_video and e is not ref and e.exists]
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
