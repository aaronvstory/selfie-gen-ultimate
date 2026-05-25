"""Face Crop Tab — Extract passport-style 3:4 face crops from ID card photos."""

import os
import platform
import tempfile
import tkinter as tk
from tkinter import ttk
import threading
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

from ..theme import (
    COLORS,
    FONT_FAMILY,
    TTK_BTN_COMPACT,
    TTK_BTN_DANGER,
    TTK_BTN_PRIMARY,
    TTK_BTN_SECONDARY,
    TTK_BTN_SUCCESS,
    TTK_BTN_TAB_NAV,
    TTK_BTN_WORKFLOW,
    debounce_command,
    macos_widget_pad,
)
from ..image_state import ImageSession
from ..ml_backend_env import ensure_ml_backend_env
from path_utils import get_gen_images_folder
from tk_dialogs import select_open_file
from automation.config import get_outpaint_fal_timeout_seconds

# Optional heavy dependencies — tab degrades gracefully if missing/broken
cv2 = None
np = None
HAS_FACE_DEPS = False
FACE_DEPS_ERROR = ""
_RETINAFACE_CLASS = None
_RETINAFACE_ERROR = ""
try:
    import cv2 as _cv2
    import numpy as _np

    cv2 = _cv2
    np = _np
    HAS_FACE_DEPS = True
except Exception as exc:
    FACE_DEPS_ERROR = f"{type(exc).__name__}: {exc}"

# PIL for canvas thumbnails
try:
    from PIL import Image, ImageTk, ImageDraw, ImageOps

    HAS_PIL = True
except ImportError:
    HAS_PIL = False

VALID_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp", ".bmp", ".tiff", ".tif"}

# Accordion header background colors
_HEADER_BG_COLLAPSED = "#333338"  # noticeably darker than bg_panel (#3C3C41)
_HEADER_BG_OPEN = "#505055"       # matches COLORS["bg_hover"]


def _platform_gui_repair_launcher() -> str:
    """Return the platform-appropriate GUI repair launcher name."""
    system = platform.system()
    if system == "Windows":
        return "run_gui.bat"
    if system == "Darwin":
        return "run_gui.command"
    return "run_gui.sh"


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


_DEFAULT_POLISH_PROMPT = (
    "Carefully remove all text, numbers, watermarks, seals, and document artifacts "
    "from the image. Clean up the background to make it seamless. "
    "CRITICAL: Do NOT alter the person's face, facial features, hair, expression, "
    "or clothing in any way. Keep the original photo quality, lighting, and realism "
    "exactly the same. Do not beautify or change the identity."
)


def _load_retinaface():
    """Import RetinaFace lazily so broken TF stack cannot break GUI startup."""
    global _RETINAFACE_CLASS
    global _RETINAFACE_ERROR
    if _RETINAFACE_CLASS is not None:
        return _RETINAFACE_CLASS, ""

    if not HAS_FACE_DEPS:
        return None, FACE_DEPS_ERROR or "opencv/numpy not available"

    try:
        ensure_ml_backend_env()
        from retinaface import RetinaFace as _RetinaFace

        _RETINAFACE_CLASS = _RetinaFace
        _RETINAFACE_ERROR = ""
        return _RETINAFACE_CLASS, ""
    except Exception as exc:
        _RETINAFACE_ERROR = f"{type(exc).__name__}: {exc}"
        return None, _RETINAFACE_ERROR


class FaceCropTab(tk.Frame):
    """Tab 0: Detect face in ID card photo and produce a 3:4 passport crop."""

    def __init__(
        self,
        parent,
        image_session: ImageSession,
        config: dict,
        config_getter: Callable[[], dict],
        log_callback: Callable[[str, str], None],
        notebook_switcher: Optional[Callable[[], None]] = None,
        notebook_switcher_prep: Optional[Callable[[], None]] = None,
        notebook_switcher_selfie: Optional[Callable[[], None]] = None,
        config_saver: Optional[Callable[[], None]] = None,
        **kwargs,
    ):
        super().__init__(parent, bg=COLORS["bg_panel"], **kwargs)
        self.image_session = image_session
        self.config = config
        self.get_config = config_getter
        self.log = log_callback
        self._config_saver = config_saver
        # Legacy single-switcher kept for backward compat
        self._notebook_switcher = notebook_switcher
        self._notebook_switcher_prep = notebook_switcher_prep or notebook_switcher
        self._notebook_switcher_selfie = notebook_switcher_selfie

        # Detection state
        self._source_path: Optional[str] = None
        self._original_path: Optional[str] = None  # user-selected path, pre-EXIF correction
        self._cv2_img = None  # numpy array (BGR)
        self._face_box = None  # (fx, fy, fw, fh)
        self._crop_result = None  # numpy array of current crop
        self._last_crop_path: Optional[str] = None  # last saved crop (for naming)
        self._busy = False

        # Tk variables
        self._multiplier_var = tk.DoubleVar(
            value=config.get("face_crop_multiplier", 1.9)
        )
        self._auto_switch_var = tk.BooleanVar(
            value=config.get("face_crop_auto_switch", True)
        )

        # Polish state
        self._polish_counter = 0
        self._polish_busy = False
        self._polish_provider_var = tk.StringVar(
            value=config.get("face_crop_polish_provider", "BFL (Kontext Pro)")
        )
        self._polish_strength_var = tk.DoubleVar(
            value=config.get("polish_strength", 0.8)
        )

        # Upscale state
        self._upscale_counter = 0
        self._upscale_busy = False
        self._upscale_provider_var = tk.StringVar(
            value=config.get("upscale_provider", "Crystal (Portraits)")
        )
        self._upscale_scale_var = tk.StringVar(
            value=config.get("upscale_scale", "2x")
        )
        self._upscale_creativity_var = tk.DoubleVar(
            value=config.get("upscale_creativity", 0.0)
        )
        self._upscale_resemblance_var = tk.DoubleVar(
            value=config.get("upscale_resemblance", 0.9)
        )

        # Accordion state (default to Generative Expand open on launch).
        self._expanded_sections = ["expand"]

        # Outpaint state
        self._outpaint_busy = False
        self._outpaint_cancel_event: Optional[threading.Event] = None
        self._outpaint_run_token = 0
        self._expand_mode_var = tk.StringVar(
            value=config.get("outpaint_expand_mode", "percentage")
        )
        self._pct_var = tk.IntVar(
            value=config.get("outpaint_expand_percentage", 30)
        )
        self._outpaint_format_var = tk.StringVar(
            value=config.get("outpaint_format", "png")
        )
        # Pixel vars
        self._expand_top_var = tk.IntVar(value=config.get("outpaint_expand_top", 140))
        self._expand_bottom_var = tk.IntVar(value=config.get("outpaint_expand_bottom", 140))
        self._expand_left_var = tk.IntVar(value=config.get("outpaint_expand_left", 140))
        self._expand_right_var = tk.IntVar(value=config.get("outpaint_expand_right", 140))
        # PR #48 round 6: "none" is NEVER a valid load-time default for
        # Step 0. The raw fal output (composite_mode="none") doesn't
        # preserve the original pixels — the user sees a "this isn't
        # expanded" result with the face redrawn/warped inside the
        # expanded canvas. The user repeatedly reported "it keeps
        # defaulting to none" because Step 0 saves whatever's in the
        # var to disk on quit, and a prior session's "none" choice
        # persists across launches. Force preserve_seamless on load
        # whenever the saved value is unset/blank/"none". The user
        # can still pick "None" mid-session via the dropdown if they
        # explicitly want the raw output for an A/B compare; it just
        # won't survive the next launch.
        _saved_composite = config.get("outpaint_composite_mode", "preserve_seamless")
        if not isinstance(_saved_composite, str) or _saved_composite.strip().lower() in ("", "none"):
            _saved_composite = "preserve_seamless"
        self._outpaint_composite_var = tk.StringVar(value=_saved_composite)
        # Outpaint provider: "bfl" or "fal". Default = "fal" everywhere
        # (user direction 2026-05-22 final). The Phase A revert that
        # restored the BFL-if-key-present default was over-broad: the
        # user only wanted the macOS composite/feather changes
        # reverted (the LANCZOS + 16px tolerance edits in
        # outpaint_generator.py, already rolled back by d48bbc8). The
        # provider default itself stays "fal" -- switching providers
        # is a one-click dropdown change.
        self._outpaint_provider_var = tk.StringVar(
            value=config.get("outpaint_provider", "fal")
        )
        # Default UNCHECKED 2026-05-21 per user request: "Run 2x" was
        # defaulting ON for new users which doubled their API spend
        # silently on first run. Existing users with the key already
        # in their kling_config.json keep their chosen value.
        #
        # One-time migration (PR fix/step0-composite-and-rppg-v2.5):
        # PR #48 fixed the in-code default but pre-existing configs
        # from before that PR persist outpaint_double_expand=True
        # forever. The migration below forces the value to False
        # once per machine if the marker key is absent; after that,
        # the user's manual choice is sticky. New installs pre-stamp
        # the marker via default_config_template.json so the migration
        # is a no-op on fresh bundles.
        #
        # _parse_bool is used (not bool(...)) so that string-backed
        # JSON ("false"/"0"/...) parses correctly — a bare bool()
        # treats "false" as truthy. See face_similarity._parse_bool
        # for the helper.
        #
        # Subagent M3 round 5: an uncoercible marker value (list/dict/
        # garbage) returns None from _parse_bool. Treat None as
        # "marker present, skip migration" — conservative semantics
        # so we never overwrite a user's explicit (if unusual)
        # outpaint_double_expand on subsequent launches just because
        # the marker key has a weird value. The migration is one-time
        # by design; "weird marker = treat as present" honors that.
        from face_similarity import _parse_bool as _pb
        _marker_raw = config.get("outpaint_2x_default_reset_v2", False)
        _marker_parsed = _pb(_marker_raw)
        _migration_already_done = (
            _marker_parsed is True
            or _marker_parsed is None  # uncoercible -> treat as done
        )
        if not _migration_already_done:
            prior = config.get("outpaint_double_expand", False)
            config["outpaint_double_expand"] = False
            config["outpaint_2x_default_reset_v2"] = True
            self._outpaint_2x_migration_fired = True
            self._outpaint_2x_migration_prior = prior
        else:
            self._outpaint_2x_migration_fired = False
            self._outpaint_2x_migration_prior = None

        _persisted_2x = _pb(config.get("outpaint_double_expand", False))
        self._outpaint_double_expand_var = tk.BooleanVar(
            value=bool(_persisted_2x) if _persisted_2x is not None else False
        )

        # PhotoImage references (prevent GC)
        self._source_photo = None
        self._crop_photo = None

        self._build_ui()
        self.image_session.add_on_change(self._on_image_session_change)

        # Persist the migration immediately so a user who launches and
        # closes without doing anything still has the marker stamped.
        # The log goes to the GUI log display (now wired by _build_ui).
        # Subagent M3 round 1: bare except was masking real persistence
        # failures (disk full, unwritable config path) — if the marker
        # never lands, the migration re-fires every launch. Log the
        # exception to stderr so a real failure leaves a trail even if
        # the in-app log display isn't visible yet.
        if self._outpaint_2x_migration_fired:
            try:
                # Subagent M4 round 5: at __init__ time the other tabs
                # haven't been constructed yet, so calling the full
                # get_config_updates() round-trip via _save_config_now()
                # would write the shared config in its pre-tab-load
                # state. Persist ONLY the two migration keys via the
                # config_saver — narrower blast radius, no ordering
                # invariant locked in.
                if self._config_saver:
                    # Migration mutated `config` (shared dict) in
                    # __init__; just trigger the on-disk save.
                    self._config_saver()
            except Exception as exc:
                import sys as _sys
                print(
                    f"[face_crop_tab] WARNING: Run 2x migration save "
                    f"failed ({type(exc).__name__}: {exc}); marker "
                    f"may not persist and migration may re-fire next "
                    f"launch.",
                    file=_sys.stderr,
                )
            try:
                if _pb(self._outpaint_2x_migration_prior):
                    self.log(
                        "One-time reset: Run 2x default → unchecked. "
                        "Re-toggle in Step 0 if you want 2x expand.",
                        "info",
                    )
            except Exception as exc:
                import sys as _sys
                print(
                    f"[face_crop_tab] WARNING: Run 2x migration log "
                    f"call failed ({type(exc).__name__}: {exc}); "
                    f"user won't see the reset notification.",
                    file=_sys.stderr,
                )

    # ── Config persistence ────────────────────────────────────────

    def _save_config_now(self):
        """Update shared config dict and persist to disk immediately."""
        self.config.update(self.get_config_updates())
        if self._config_saver:
            self._config_saver()

    # ── UI Construction ─────────────────────────────────────────────

    def _build_ui(self):
        # Dependency warning
        if not HAS_FACE_DEPS:
            repair_launcher = _platform_gui_repair_launcher()
            warn = tk.Label(
                self,
                text=(
                    f"Face Crop deps missing. Auto-repair via {repair_launcher}, or install manually: "
                    "pip install opencv-python-headless numpy tensorflow==2.16.2 "
                    "tensorflow-intel==2.16.2 tf-keras==2.16.0 retina-face==0.0.17"
                ),
                bg=COLORS["bg_panel"],
                fg=COLORS["warning"],
                font=(FONT_FAMILY, 10, "bold"),
                anchor="w",
            )
            warn.pack(fill=tk.X, padx=8, pady=(8, 0))

        # ── Source & Detection ──────────────────────────────────────
        source_frame = tk.LabelFrame(
            self,
            text=" Add Image ",
            bg=COLORS["bg_panel"],
            fg=COLORS["text_light"],
            font=(FONT_FAMILY, 10, "bold"),
            bd=1,
            relief="groove",
        )
        source_frame.pack(fill=tk.X, padx=8, pady=(6, 2))

        # Browse row
        browse_row = tk.Frame(source_frame, bg=COLORS["bg_panel"])
        browse_row.pack(fill=tk.X, padx=6, pady=(4, 2))
        self._browse_row = browse_row
        self._source_frame = source_frame

        self._path_var = tk.StringVar()
        path_entry = tk.Entry(
            browse_row,
            textvariable=self._path_var,
            bg=COLORS["bg_input"],
            fg=COLORS["text_light"],
            font=(FONT_FAMILY, 9),
            insertbackground=COLORS["text_light"],
            relief="flat",
            width=52,
        )
        path_entry.pack(side=tk.LEFT, ipady=3)
        self._path_entry = path_entry

        browse_btn = ttk.Button(
            browse_row,
            text="Browse",
            style=TTK_BTN_SECONDARY,
            command=debounce_command(self._browse_image, key="facecrop_browse"),
        )
        browse_btn.pack(side=tk.LEFT, padx=(6, 0))
        self._browse_btn = browse_btn

        # Hidden status label retained for internal error state compatibility.
        self._status_label = tk.Label(
            source_frame,
            text="",
            bg=COLORS["bg_panel"],
            fg=COLORS["text_dim"],
            font=(FONT_FAMILY, 9),
            anchor="w",
            justify=tk.LEFT,
        )

        # Slider row
        slider_row = tk.Frame(source_frame, bg=COLORS["bg_panel"])
        slider_row.pack(fill=tk.X, padx=6, pady=(2, 4))

        tk.Label(
            slider_row,
            text="Crop Multiplier:",
            bg=COLORS["bg_panel"],
            fg=COLORS["text_light"],
            font=(FONT_FAMILY, 9),
        ).pack(side=tk.LEFT)

        ttk.Button(
            slider_row,
            text="-",
            style=TTK_BTN_TAB_NAV,
            width=2,
            command=debounce_command(lambda: self._adjust_multiplier(-0.1), key="facecrop_multiplier_down", interval_ms=100),
        ).pack(side=tk.LEFT, padx=(6, 0))

        self._slider = tk.Scale(
            slider_row,
            from_=1.0,
            to=3.0,
            resolution=0.1,
            orient=tk.HORIZONTAL,
            variable=self._multiplier_var,
            bg=COLORS["bg_panel"],
            fg=COLORS["text_light"],
            troughcolor=COLORS["bg_input"],
            highlightthickness=0,
            font=(FONT_FAMILY, 9),
            length=200,
            command=self._on_slider_changed,
        )
        self._slider.pack(side=tk.LEFT, padx=(2, 2), fill=tk.X, expand=True)

        ttk.Button(
            slider_row,
            text="+",
            style=TTK_BTN_TAB_NAV,
            width=2,
            command=debounce_command(lambda: self._adjust_multiplier(0.1), key="facecrop_multiplier_up", interval_ms=100),
        ).pack(side=tk.LEFT)

        # Workflow-primary on Step 0: "Detect Face & Crop" is the main
        # action users hit first. Distinguished from secondary controls
        # via TTK_BTN_WORKFLOW (accent-blue + dark border + larger
        # padding) so it stands out without being garish.
        self._detect_btn = ttk.Button(
            slider_row,
            text="Detect Face & Crop",
            style=TTK_BTN_WORKFLOW,
            command=debounce_command(self._detect_face, key="facecrop_detect"),
            state=tk.DISABLED if not HAS_FACE_DEPS else tk.NORMAL,
        )
        self._detect_btn.pack(side=tk.LEFT, padx=(8, 0))

        # "Add to Carousel" — workflow-primary (next step after crop).
        self._add_carousel_btn = ttk.Button(
            slider_row,
            text="Add to Carousel",
            style=TTK_BTN_WORKFLOW,
            command=debounce_command(self._add_crop_to_carousel, key="facecrop_add_carousel"),
            state=tk.DISABLED,
        )
        self._add_carousel_btn.pack(side=tk.LEFT, padx=(8, 0))

        # ── Preview ─────────────────────────────────────────────────
        preview_frame = tk.LabelFrame(
            self,
            text=" Preview ",
            bg=COLORS["bg_panel"],
            fg=COLORS["text_light"],
            font=(FONT_FAMILY, 10, "bold"),
            bd=1,
            relief="groove",
        )
        preview_frame.pack(fill=tk.BOTH, expand=True, padx=8, pady=(2, 4))

        canvas_container = tk.Frame(preview_frame, bg=COLORS["bg_panel"])
        canvas_container.pack(fill=tk.BOTH, expand=True, padx=4, pady=4)

        # Left: source with face box overlay
        left_frame = tk.Frame(canvas_container, bg=COLORS["bg_panel"])
        left_frame.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        self._source_label = tk.Label(
            left_frame,
            text="Source",
            bg=COLORS["bg_panel"],
            fg=COLORS["text_dim"],
            font=(FONT_FAMILY, 9),
        )
        self._source_label.pack()
        self._source_canvas = tk.Canvas(
            left_frame, bg=COLORS["bg_input"], highlightthickness=0
        )
        self._source_canvas.pack(fill=tk.BOTH, expand=True)

        # Separator
        tk.Frame(canvas_container, bg=COLORS["border"], width=2).pack(
            side=tk.LEFT, fill=tk.Y, padx=4
        )

        # Right: crop result
        right_frame = tk.Frame(canvas_container, bg=COLORS["bg_panel"])
        right_frame.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        self._crop_label = tk.Label(
            right_frame,
            text="Crop Result",
            bg=COLORS["bg_panel"],
            fg=COLORS["text_dim"],
            font=(FONT_FAMILY, 9),
        )
        self._crop_label.pack()
        self._crop_canvas = tk.Canvas(
            right_frame, bg=COLORS["bg_input"], highlightthickness=0
        )
        self._crop_canvas.pack(fill=tk.BOTH, expand=True)

    # ── Right Pane Tools Panel (built by main_window) ────────────────

    _SECTIONS = ["polish", "expand", "upscale"]  # accordion section names

    def build_tools_panel(self, parent):
        """Build the tools panel (Polish + Outpaint + Upscale + Send) inside *parent*.

        Called by main_window to populate the context-sensitive right pane
        when Tab 0 is active. All tools operate on the active carousel image.
        Sections are collapsible (accordion, radio behavior). Send is always visible
        (pinned at bottom). Accordion sections live in a scrollable canvas region.
        """
        # ── Send to Next Tab (pinned at bottom, always visible) ───────
        tk.Frame(parent, bg=COLORS["border"], height=1).pack(
            fill=tk.X, side=tk.BOTTOM, padx=4
        )
        send_lf = tk.LabelFrame(
            parent,
            text=" Send to Next Tab ",
            bg=COLORS["bg_panel"],
            fg=COLORS["text_light"],
            font=(FONT_FAMILY, 9, "bold"),
            bd=1,
            relief="groove",
        )
        send_lf.pack(fill=tk.X, side=tk.BOTTOM, padx=4, pady=(3, 6))

        send_inner = tk.Frame(send_lf, bg=COLORS["bg_panel"])
        send_inner.pack(fill=tk.X, padx=4, pady=(3, 1))

        self._auto_switch_cb = tk.Checkbutton(
            send_inner,
            text="Auto-switch after send",
            variable=self._auto_switch_var,
            bg=COLORS["bg_panel"],
            fg=COLORS["text_light"],
            selectcolor=COLORS["bg_input"],
            activebackground=COLORS["bg_panel"],
            activeforeground=COLORS["text_light"],
            font=(FONT_FAMILY, 9),
            **macos_widget_pad(),
        )
        self._auto_switch_cb.pack(anchor="w")

        send_btn_row = tk.Frame(send_lf, bg=COLORS["bg_panel"])
        send_btn_row.pack(fill=tk.X, padx=4, pady=(0, 3))

        self._send_prep_btn = ttk.Button(
            send_btn_row,
            text="Send to 1 (AI Analysis)",
            style=TTK_BTN_PRIMARY,
            command=debounce_command(self._send_to_prep, key="facecrop_send_prep"),
        )
        self._send_prep_btn.pack(fill=tk.X, pady=(0, 2))

        self._send_selfie_btn = ttk.Button(
            send_btn_row,
            text="Send to 2 (Generate Selfie)",
            style=TTK_BTN_SUCCESS,
            command=debounce_command(self._send_to_selfie, key="facecrop_send_selfie"),
        )
        self._send_selfie_btn.pack(fill=tk.X)

        # ── Scrollable region for accordion sections ──────────────────
        scroll_canvas = tk.Canvas(
            parent, bg=COLORS["bg_panel"], highlightthickness=0
        )
        scrollbar = ttk.Scrollbar(
            parent, orient="vertical", command=scroll_canvas.yview
        )
        scroll_inner = tk.Frame(scroll_canvas, bg=COLORS["bg_panel"])

        scroll_inner.bind(
            "<Configure>",
            lambda e: scroll_canvas.configure(
                scrollregion=scroll_canvas.bbox("all")
            ),
        )

        self._scroll_window_id = scroll_canvas.create_window(
            (0, 0), window=scroll_inner, anchor="nw"
        )
        scroll_canvas.configure(yscrollcommand=scrollbar.set)

        scrollbar.pack(side=tk.RIGHT, fill=tk.Y)
        scroll_canvas.pack(fill=tk.BOTH, expand=True)

        inner = scroll_inner
        self._tools_inner = inner
        self._scroll_canvas = scroll_canvas

        # ── AI Polish (collapsible) ─────────────────────────────────
        polish_wrapper = tk.Frame(inner, bg=COLORS["bg_panel"])
        polish_wrapper.pack(fill=tk.X, padx=4, pady=(6, 0))

        # Header row: [accent_bar | button]
        self._polish_header_row = tk.Frame(polish_wrapper, bg=_HEADER_BG_COLLAPSED)
        self._polish_header_row.pack(fill=tk.X)

        self._polish_accent = tk.Frame(self._polish_header_row, bg=COLORS["accent_blue"], width=3)

        self._polish_toggle_btn = ttk.Button(
            self._polish_header_row, text="\u25b6  [AI POLISH]",
            style=TTK_BTN_TAB_NAV,
            command=debounce_command(lambda: self._toggle_section("polish"), key="facecrop_toggle_polish", interval_ms=120),
        )
        self._polish_toggle_btn.pack(side=tk.LEFT, fill=tk.X, expand=True, ipady=4)
        self._bind_header_hover(self._polish_toggle_btn)

        # 2px border below header
        tk.Frame(polish_wrapper, bg=COLORS["border"], height=2).pack(fill=tk.X)

        self._polish_body = tk.Frame(polish_wrapper, bg=COLORS["bg_panel"])
        # Inner frame provides uniform indentation
        polish_body_inner = tk.Frame(self._polish_body, bg=COLORS["bg_panel"])
        polish_body_inner.pack(fill=tk.X, padx=8, pady=(4, 6))
        polish_parent = polish_body_inner

        # Row 1: [AI Polish] [Edit Prompt] Provider: [dropdown] [Strength slider]
        polish_row = tk.Frame(polish_parent, bg=COLORS["bg_panel"])
        polish_row.pack(fill=tk.X, pady=(2, 4))

        self._polish_btn = ttk.Button(
            polish_row,
            text="AI Polish",
            style=TTK_BTN_PRIMARY,
            command=debounce_command(self._polish_crop, key="facecrop_polish"),
        )
        self._polish_btn.pack(side=tk.LEFT)

        ttk.Button(
            polish_row,
            text="Edit Prompt",
            style=TTK_BTN_SECONDARY,
            command=debounce_command(self._open_polish_prompt_editor, key="facecrop_edit_polish_prompt", interval_ms=120),
        ).pack(side=tk.LEFT, padx=(6, 0))

        tk.Label(
            polish_row, text="Provider:", font=(FONT_FAMILY, 9),
            bg=COLORS["bg_panel"], fg=COLORS["text_light"],
        ).pack(side=tk.LEFT, padx=(8, 0))

        self._polish_provider_combo = ttk.Combobox(
            polish_row,
            textvariable=self._polish_provider_var,
            values=["BFL (Kontext Pro)", "fal.ai (FLUX.2 Edit)"],
            state="readonly",
            width=16,
        )
        self._polish_provider_combo.pack(side=tk.LEFT, padx=(3, 0))

        # fal.ai-specific strength slider (hidden when BFL is selected)
        self._polish_strength_frame = tk.Frame(polish_row, bg=COLORS["bg_panel"])

        tk.Label(
            self._polish_strength_frame, text="Str:", font=(FONT_FAMILY, 9),
            bg=COLORS["bg_panel"], fg=COLORS["text_light"],
        ).pack(side=tk.LEFT, padx=(6, 0))
        self._polish_strength_scale = tk.Scale(
            self._polish_strength_frame, from_=0.0, to=1.0, resolution=0.05,
            orient=tk.HORIZONTAL, variable=self._polish_strength_var,
            bg=COLORS["bg_panel"], fg=COLORS["text_light"],
            troughcolor=COLORS["bg_input"], highlightthickness=0,
            font=(FONT_FAMILY, 9), length=80,
        )
        self._polish_strength_scale.pack(side=tk.LEFT, padx=(2, 0))

        # Show/hide based on initial provider selection
        self._polish_provider_combo.bind("<<ComboboxSelected>>", self._on_polish_provider_changed)
        self._toggle_polish_strength()

        # Status label on row 2 (below action row)
        self._polish_status = tk.Label(
            polish_parent,
            text="",
            bg=COLORS["bg_panel"],
            fg=COLORS["text_dim"],
            font=(FONT_FAMILY, 9),
            anchor="w",
        )
        self._polish_status.pack(fill=tk.X)

        # ── Outpaint / Expand (collapsible) ─────────────────────────
        expand_wrapper = tk.Frame(inner, bg=COLORS["bg_panel"])
        expand_wrapper.pack(fill=tk.X, padx=4, pady=(3, 0))

        self._expand_header_row = tk.Frame(expand_wrapper, bg=_HEADER_BG_COLLAPSED)
        self._expand_header_row.pack(fill=tk.X)

        self._expand_accent = tk.Frame(self._expand_header_row, bg=COLORS["accent_blue"], width=3)

        self._expand_toggle_btn = ttk.Button(
            self._expand_header_row, text="\u25b6  [GENERATIVE EXPAND]",
            style=TTK_BTN_TAB_NAV,
            command=debounce_command(lambda: self._toggle_section("expand"), key="facecrop_toggle_expand", interval_ms=120),
        )
        self._expand_toggle_btn.pack(side=tk.LEFT, fill=tk.X, expand=True, ipady=4)
        self._bind_header_hover(self._expand_toggle_btn)

        # 2px border below header
        tk.Frame(expand_wrapper, bg=COLORS["border"], height=2).pack(fill=tk.X)

        self._expand_body = tk.Frame(expand_wrapper, bg=COLORS["bg_panel"])
        # Inner frame provides uniform indentation
        expand_body_inner = tk.Frame(self._expand_body, bg=COLORS["bg_panel"])
        expand_body_inner.pack(fill=tk.X, padx=8, pady=(4, 6))
        expand_parent = expand_body_inner

        # Row 1: [Expand Image] [Edit Prompt] (o)Percentage (o)Pixels  status
        btn_row = tk.Frame(expand_parent, bg=COLORS["bg_panel"])
        btn_row.pack(fill=tk.X, pady=(2, 5))

        self._expand_btn = ttk.Button(
            btn_row,
            text="Expand Image",
            style=TTK_BTN_WORKFLOW,
            command=debounce_command(self._outpaint_image, key="facecrop_expand"),
        )
        self._expand_btn.pack(side=tk.LEFT)
        self._expand_abort_btn = ttk.Button(
            btn_row,
            text="Abort",
            style=TTK_BTN_DANGER,
            command=debounce_command(self._abort_outpaint, key="facecrop_abort_expand"),
            state=tk.DISABLED,
        )
        self._expand_abort_btn.pack(side=tk.LEFT, padx=(6, 0))

        ttk.Button(
            btn_row,
            text="Edit Prompt",
            style=TTK_BTN_SECONDARY,
            command=debounce_command(self._open_expand_prompt_editor, key="facecrop_edit_expand_prompt", interval_ms=120),
        ).pack(side=tk.LEFT, padx=(6, 0))

        tk.Radiobutton(
            btn_row,
            text="Percentage",
            variable=self._expand_mode_var,
            value="percentage",
            command=self._on_expand_mode_changed,
            bg=COLORS["bg_panel"],
            fg=COLORS["text_light"],
            selectcolor=COLORS["bg_input"],
            activebackground=COLORS["bg_panel"],
            font=(FONT_FAMILY, 9),
            **macos_widget_pad(),
        ).pack(side=tk.LEFT, padx=(10, 0))
        tk.Radiobutton(
            btn_row,
            text="Pixels",
            variable=self._expand_mode_var,
            value="pixels",
            command=self._on_expand_mode_changed,
            bg=COLORS["bg_panel"],
            fg=COLORS["text_light"],
            selectcolor=COLORS["bg_input"],
            activebackground=COLORS["bg_panel"],
            font=(FONT_FAMILY, 9),
            **macos_widget_pad(),
        ).pack(side=tk.LEFT, padx=(4, 0))
        tk.Checkbutton(
            btn_row,
            text="Run 2x",
            variable=self._outpaint_double_expand_var,
            bg=COLORS["bg_panel"],
            fg=COLORS["text_light"],
            selectcolor=COLORS["bg_input"],
            activebackground=COLORS["bg_panel"],
            font=(FONT_FAMILY, 9),
            **macos_widget_pad(),
        ).pack(side=tk.LEFT, padx=(8, 0))

        self._outpaint_status = tk.Label(
            expand_parent, text="", font=(FONT_FAMILY, 9),
            bg=COLORS["bg_panel"], fg=COLORS["text_dim"], anchor="w",
        )
        self._outpaint_status.pack(fill=tk.X, pady=(0, 2))

        # Percentage controls
        self._pct_frame = tk.Frame(expand_parent, bg=COLORS["bg_panel"])

        pct_row = tk.Frame(self._pct_frame, bg=COLORS["bg_panel"])
        pct_row.pack(fill=tk.X, pady=(0, 3))

        tk.Label(
            pct_row, text="Expand:", font=(FONT_FAMILY, 9),
            bg=COLORS["bg_panel"], fg=COLORS["text_light"],
        ).pack(side=tk.LEFT)

        self._pct_scale = tk.Scale(
            pct_row,
            from_=5, to=100, resolution=5,
            orient=tk.HORIZONTAL,
            variable=self._pct_var,
            bg=COLORS["bg_panel"],
            fg=COLORS["text_light"],
            troughcolor=COLORS["bg_input"],
            highlightthickness=0,
            font=(FONT_FAMILY, 9),
        )
        self._pct_scale.pack(side=tk.LEFT, padx=(3, 0), fill=tk.X, expand=True)

        tk.Label(
            pct_row, text="%", font=(FONT_FAMILY, 9),
            bg=COLORS["bg_panel"], fg=COLORS["text_light"],
        ).pack(side=tk.LEFT)

        # Pixels controls (single row: T/B/L/R)
        self._px_frame = tk.Frame(expand_parent, bg=COLORS["bg_panel"])

        px_row = tk.Frame(self._px_frame, bg=COLORS["bg_panel"])
        px_row.pack(fill=tk.X, pady=(0, 3))

        for label_text, var in [
            ("T:", self._expand_top_var),
            ("B:", self._expand_bottom_var),
            ("L:", self._expand_left_var),
            ("R:", self._expand_right_var),
        ]:
            tk.Label(
                px_row, text=label_text, font=(FONT_FAMILY, 9),
                bg=COLORS["bg_panel"], fg=COLORS["text_light"],
            ).pack(side=tk.LEFT, padx=(4, 0))
            tk.Entry(
                px_row, textvariable=var, width=5,
                bg=COLORS["bg_input"], fg=COLORS["text_light"],
                insertbackground=COLORS["text_light"], font=(FONT_FAMILY, 9),
            ).pack(side=tk.LEFT, padx=(2, 0))

        # Outpaint prompt stored in config (edited via dialog).
        # Phase G of polish/v2.3 (2026-05-22): the Step 0 face-crop
        # expand has its own ``face_crop_expand_prompt`` key now,
        # independent of Step 2.5 (``selfie_expand_prompt``) and
        # the standalone Outpaint tab (``outpaint_tab_prompt``).
        # Legacy ``outpaint_prompt`` is read as a fallback so users
        # with old configs see their saved prompt on first launch.
        # Codex P1 on 0967564 (2026-05-22): key-presence
        # semantics, NOT truthiness. An explicitly-saved empty
        # ``face_crop_expand_prompt`` must NOT be silently
        # replaced by the legacy shared ``outpaint_prompt`` --
        # the user cleared the prompt on purpose.
        _section_prompt = self.config.get("face_crop_expand_prompt")
        if isinstance(_section_prompt, str):
            self._outpaint_prompt_str = _section_prompt
        else:
            self._outpaint_prompt_str = str(
                self.config.get("outpaint_prompt", "") or ""
            )

        # Provider + Format + Composite row
        _PROVIDER_LABELS = {
            "bfl": "BFL Expand",
            "fal": "fal.ai (700px)",
        }
        _LABEL_TO_PROVIDER = {v: k for k, v in _PROVIDER_LABELS.items()}

        opts_row = tk.Frame(expand_parent, bg=COLORS["bg_panel"])
        opts_row.pack(fill=tk.X, pady=(0, 2))

        tk.Label(
            opts_row, text="Provider:", font=(FONT_FAMILY, 9),
            bg=COLORS["bg_panel"], fg=COLORS["text_light"],
        ).pack(side=tk.LEFT)

        self._outpaint_provider_combo = ttk.Combobox(
            opts_row,
            values=list(_PROVIDER_LABELS.values()),
            state="readonly", width=12,
        )
        self._outpaint_provider_combo.set(
            _PROVIDER_LABELS.get(self._outpaint_provider_var.get(), _PROVIDER_LABELS["bfl"])
        )

        def _on_provider_combo(event=None):
            label = self._outpaint_provider_combo.get()
            self._outpaint_provider_var.set(_LABEL_TO_PROVIDER.get(label, "bfl"))

        self._outpaint_provider_combo.bind("<<ComboboxSelected>>", _on_provider_combo)
        self._outpaint_provider_combo.pack(side=tk.LEFT, padx=(3, 8))
        self._provider_labels = _PROVIDER_LABELS
        self._label_to_provider = _LABEL_TO_PROVIDER

        tk.Label(
            opts_row, text="Format:", font=(FONT_FAMILY, 9),
            bg=COLORS["bg_panel"], fg=COLORS["text_light"],
        ).pack(side=tk.LEFT)
        ttk.Combobox(
            opts_row, textvariable=self._outpaint_format_var,
            values=["png", "jpg"], state="readonly", width=4,
        ).pack(side=tk.LEFT, padx=(3, 8))

        tk.Label(
            opts_row, text="Composite:", font=(FONT_FAMILY, 9),
            bg=COLORS["bg_panel"], fg=COLORS["text_light"],
        ).pack(side=tk.LEFT)
        self._outpaint_composite_labels = {
            "preserve_seamless": "Preserve Seamless",
            "feathered": "Feathered",
            "hard": "Hard",
            "none": "None",
        }
        composite_value = self._outpaint_composite_var.get().strip()
        if composite_value not in self._outpaint_composite_labels:
            composite_value = "preserve_seamless"
            self._outpaint_composite_var.set(composite_value)
        self._outpaint_composite_label_var = tk.StringVar(
            value=self._outpaint_composite_labels[composite_value]
        )

        composite_btn = tk.Menubutton(
            opts_row,
            textvariable=self._outpaint_composite_label_var,
            relief=tk.RAISED,
            width=18,
            bg=COLORS["bg_input"],
            fg=COLORS["text_light"],
            activebackground=COLORS["bg_input"],
            activeforeground=COLORS["text_light"],
            direction="below",
            anchor="w",
            padx=6,
        )
        composite_menu = tk.Menu(composite_btn, tearoff=0)

        def _set_outpaint_composite_mode(mode_key: str) -> None:
            self._outpaint_composite_var.set(mode_key)
            self._outpaint_composite_label_var.set(self._outpaint_composite_labels[mode_key])

        for mode_key, mode_label in self._outpaint_composite_labels.items():
            composite_menu.add_command(
                label=mode_label,
                command=lambda m=mode_key: _set_outpaint_composite_mode(m),
            )

        composite_btn.configure(menu=composite_menu)
        composite_btn.pack(side=tk.LEFT, padx=(3, 0))
        self._outpaint_composite_btn = composite_btn

        # Apply initial mode visibility
        self._apply_expand_mode_ui()

        # ── Upscale (collapsible) ───────────────────────────────────
        upscale_wrapper = tk.Frame(inner, bg=COLORS["bg_panel"])
        upscale_wrapper.pack(fill=tk.X, padx=4, pady=(3, 0))

        self._upscale_header_row = tk.Frame(upscale_wrapper, bg=_HEADER_BG_COLLAPSED)
        self._upscale_header_row.pack(fill=tk.X)

        self._upscale_accent = tk.Frame(self._upscale_header_row, bg=COLORS["accent_blue"], width=3)

        self._upscale_toggle_btn = ttk.Button(
            self._upscale_header_row, text="\u25b6  [UPSCALING]",
            style=TTK_BTN_TAB_NAV,
            command=debounce_command(lambda: self._toggle_section("upscale"), key="facecrop_toggle_upscale", interval_ms=120),
        )
        self._upscale_toggle_btn.pack(side=tk.LEFT, fill=tk.X, expand=True, ipady=4)
        self._bind_header_hover(self._upscale_toggle_btn)

        # 2px border below header
        tk.Frame(upscale_wrapper, bg=COLORS["border"], height=2).pack(fill=tk.X)

        self._upscale_body = tk.Frame(upscale_wrapper, bg=COLORS["bg_panel"])
        # Inner frame provides uniform indentation
        upscale_body_inner = tk.Frame(self._upscale_body, bg=COLORS["bg_panel"])
        upscale_body_inner.pack(fill=tk.X, padx=8, pady=(4, 6))
        upscale_parent = upscale_body_inner

        upscale_row = tk.Frame(upscale_parent, bg=COLORS["bg_panel"])
        upscale_row.pack(fill=tk.X, pady=(2, 4))

        self._upscale_btn = ttk.Button(
            upscale_row,
            text="Upscale",
            style=TTK_BTN_PRIMARY,
            command=debounce_command(self._upscale_image, key="facecrop_upscale"),
        )
        self._upscale_btn.pack(side=tk.LEFT)

        tk.Label(
            upscale_row, text="Provider:", font=(FONT_FAMILY, 9),
            bg=COLORS["bg_panel"], fg=COLORS["text_light"],
        ).pack(side=tk.LEFT, padx=(8, 2))

        self._upscale_combo = ttk.Combobox(
            upscale_row,
            textvariable=self._upscale_provider_var,
            values=["Crystal (Portraits)", "Aura SR v2 (Fast)"],
            state="readonly",
            width=18,
        )
        self._upscale_combo.pack(side=tk.LEFT)

        # Scale dropdown on action row (Crystal-only, toggled with provider)
        self._upscale_scale_frame = tk.Frame(upscale_row, bg=COLORS["bg_panel"])
        tk.Label(
            self._upscale_scale_frame, text="Scale:", font=(FONT_FAMILY, 9),
            bg=COLORS["bg_panel"], fg=COLORS["text_light"],
        ).pack(side=tk.LEFT, padx=(8, 0))
        ttk.Combobox(
            self._upscale_scale_frame, textvariable=self._upscale_scale_var,
            values=["2x", "4x"], state="readonly", width=4,
        ).pack(side=tk.LEFT, padx=(3, 0))

        self._upscale_status = tk.Label(
            upscale_parent, text="", font=(FONT_FAMILY, 9),
            bg=COLORS["bg_panel"], fg=COLORS["text_dim"], anchor="w",
        )
        self._upscale_status.pack(fill=tk.X, pady=(0, 2))

        # Crystal-specific sliders (hidden when Aura SR is selected)
        self._crystal_settings_frame = tk.Frame(upscale_parent, bg=COLORS["bg_panel"])

        crystal_row = tk.Frame(self._crystal_settings_frame, bg=COLORS["bg_panel"])
        crystal_row.pack(fill=tk.X, pady=(0, 2))

        tk.Label(
            crystal_row, text="Creativity:", font=(FONT_FAMILY, 9),
            bg=COLORS["bg_panel"], fg=COLORS["text_light"],
        ).pack(side=tk.LEFT)
        self._creativity_scale = tk.Scale(
            crystal_row, from_=0.0, to=1.0, resolution=0.1,
            orient=tk.HORIZONTAL, variable=self._upscale_creativity_var,
            bg=COLORS["bg_panel"], fg=COLORS["text_light"],
            troughcolor=COLORS["bg_input"], highlightthickness=0,
            font=(FONT_FAMILY, 9), length=70,
        )
        self._creativity_scale.pack(side=tk.LEFT, padx=(2, 8))

        tk.Label(
            crystal_row, text="Resemblance:", font=(FONT_FAMILY, 9),
            bg=COLORS["bg_panel"], fg=COLORS["text_light"],
        ).pack(side=tk.LEFT)
        self._resemblance_scale = tk.Scale(
            crystal_row, from_=0.0, to=1.0, resolution=0.1,
            orient=tk.HORIZONTAL, variable=self._upscale_resemblance_var,
            bg=COLORS["bg_panel"], fg=COLORS["text_light"],
            troughcolor=COLORS["bg_input"], highlightthickness=0,
            font=(FONT_FAMILY, 9), length=70,
        )
        self._resemblance_scale.pack(side=tk.LEFT, padx=(2, 0))

        # Show/hide based on initial provider selection
        self._upscale_combo.bind("<<ComboboxSelected>>", self._on_upscale_provider_changed)
        self._toggle_crystal_settings()

        # ── Apply initial accordion state ───────────────────────────
        self._apply_accordion_state()

        # Width sync: inner frame fills canvas width
        def _sync_scroll_width(e):
            scroll_canvas.itemconfigure(self._scroll_window_id, width=e.width)

        scroll_canvas.bind("<Configure>", _sync_scroll_width)

        # Recursive mousewheel binding
        self._bind_scroll_mousewheel(scroll_canvas, scroll_inner)
        self.after(0, self._refresh_responsive_layout)
        self.bind("<Configure>", lambda _e: self._refresh_responsive_layout())

    def _refresh_responsive_layout(self):
        """Keep Step 0 controls readable at narrow widths on all platforms."""
        self._refresh_browse_row_layout()
        self._refresh_status_wraplengths()

    def _refresh_browse_row_layout(self):
        # Browse row no longer has a secondary action button.
        return

    def _refresh_status_wraplengths(self):
        source_w = max(220, self._source_frame.winfo_width() - 80) if hasattr(self, "_source_frame") else 320
        self._status_label.config(wraplength=source_w)
        self._outpaint_status.config(wraplength=max(220, self.winfo_width() - 100))
        self._upscale_status.config(wraplength=max(220, self.winfo_width() - 100))

    # ── Accordion toggle ────────────────────────────────────────────

    def _bind_header_hover(self, btn):
        """Add hover highlight to a collapsed accordion header button."""
        _HOVER_MID = "#484850"
        if isinstance(btn, ttk.Button):
            return

        def on_enter(e):
            cur_bg = btn.cget("bg")
            if cur_bg == _HEADER_BG_COLLAPSED:
                btn.config(bg=_HOVER_MID)
                # Also update parent header row
                btn.master.config(bg=_HOVER_MID)

        def on_leave(e):
            if btn.cget("bg") == _HOVER_MID:
                btn.config(bg=_HEADER_BG_COLLAPSED)
                btn.master.config(bg=_HEADER_BG_COLLAPSED)

        btn.bind("<Enter>", on_enter)
        btn.bind("<Leave>", on_leave)

    def _bind_scroll_mousewheel(self, canvas, inner_frame):
        """Bind mousewheel to canvas scroll, recursively on all children."""

        def _on_mousewheel(event):
            canvas.yview_scroll(int(-1 * (event.delta / 120)), "units")

        def _on_mousewheel_linux_up(event):
            canvas.yview_scroll(-1, "units")

        def _on_mousewheel_linux_down(event):
            canvas.yview_scroll(1, "units")

        def _bind_recursive(widget):
            widget.bind("<MouseWheel>", _on_mousewheel)
            widget.bind("<Button-4>", _on_mousewheel_linux_up)
            widget.bind("<Button-5>", _on_mousewheel_linux_down)
            for child in widget.winfo_children():
                _bind_recursive(child)

        _bind_recursive(canvas)
        _bind_recursive(inner_frame)

    def _toggle_section(self, name):
        """Open *name*; always keep exactly 2 sections open. Evict oldest if needed."""
        if name not in self._SECTIONS:
            return
        if name in self._expanded_sections:
            return  # already open — do nothing
        # Evict oldest to make room, then append
        if len(self._expanded_sections) >= 2:
            self._expanded_sections.pop(0)
        self._expanded_sections.append(name)
        self._apply_accordion_state()

    def _apply_accordion_state(self):
        """Sync visual state of all accordion sections to self._expanded_sections."""
        for sec in self._SECTIONS:
            btn = getattr(self, f"_{sec}_toggle_btn")
            body = getattr(self, f"_{sec}_body")
            accent = getattr(self, f"_{sec}_accent")
            header_row = getattr(self, f"_{sec}_header_row")
            if sec in self._expanded_sections:
                body.pack(fill=tk.X, padx=0, pady=(0, 3))
                accent.pack(side=tk.LEFT, fill=tk.Y, before=btn)
                btn.config(text=btn.cget("text").replace("\u25b6", "\u25bc"))
                header_row.config(bg=_HEADER_BG_OPEN)
            else:
                body.pack_forget()
                # Keep accent bar visible when collapsed
                accent.pack(side=tk.LEFT, fill=tk.Y, before=btn)
                btn.config(text=btn.cget("text").replace("\u25bc", "\u25b6"))
                header_row.config(bg=_HEADER_BG_COLLAPSED)

    # ── Outpaint expand mode switching ────────────────────────────────

    def _on_expand_mode_changed(self):
        self._apply_expand_mode_ui()

    def _apply_expand_mode_ui(self):
        if self._expand_mode_var.get() == "percentage":
            self._px_frame.pack_forget()
            self._pct_frame.pack(fill=tk.X, padx=0, pady=0)
        else:
            self._pct_frame.pack_forget()
            self._px_frame.pack(fill=tk.X, padx=0, pady=0)

    # ── Browse ──────────────────────────────────────────────────────

    def _browse_image(self):
        ftypes = [("Images", " ".join(f"*{e}" for e in VALID_EXTENSIONS))]
        path = select_open_file(
            parent=self.winfo_toplevel(),
            title="Select ID card / passport photo",
            filetypes=ftypes,
        )
        if not path:
            return
        self._path_var.set(path)
        self._load_source(path)

    def _load_source(self, path: str, silent: bool = False):
        if not HAS_PIL:
            self._status_label.config(text="Pillow not installed", fg=COLORS["error"])
            return
        if not os.path.isfile(path):
            self._status_label.config(text="File not found", fg=COLORS["error"])
            return

        self._source_path = path
        self._original_path = path
        self._face_box = None
        self._crop_result = None
        self._polish_counter = 0
        self._upscale_counter = 0
        self._add_carousel_btn.config(state=tk.DISABLED)

        # Load with PIL, fix EXIF rotation, save corrected temp for cv2/RetinaFace
        try:
            pil_img = Image.open(path)
            pil_img = ImageOps.exif_transpose(pil_img)

            # Save orientation-corrected image so cv2.imread and RetinaFace
            # see the same upright pixels as the Tkinter preview.
            corrected_path = os.path.join(
                tempfile.gettempdir(), f"kling_facecrop_{os.path.basename(path)}"
            )
            save_img = pil_img.convert("RGB") if pil_img.mode not in ("RGB", "L") else pil_img
            save_img.save(corrected_path, format="JPEG", quality=95)
            self._source_path = corrected_path

            pil_img.thumbnail((400, 400))
            self._source_pil = pil_img.copy()
            self._source_photo = self._show_on_canvas(self._source_canvas, pil_img)
            self._crop_canvas.delete("all")
            self._crop_label.config(text="Crop Result")
            if not silent:
                self._status_label.config(
                    text=f"Loaded ({pil_img.width}x{pil_img.height} preview)",
                    fg=COLORS["text_light"],
                )
            # Show dimensions + filesize on source label
            info = _format_image_info(path)
            self._source_label.config(text=f"Source  {info}" if info else "Source")
            if not silent:
                self.log(f"Face Crop: loaded {os.path.basename(path)}", "info")
        except Exception as exc:
            self._status_label.config(text=f"Load error: {exc}", fg=COLORS["error"])

    def _on_image_session_change(self):
        """Mirror active carousel image in left preview without extra user clicks."""
        active_path = self.image_session.active_image_path
        if not active_path or not os.path.isfile(active_path):
            return
        if os.path.abspath(active_path) == os.path.abspath(self._original_path or ""):
            return
        self._path_var.set(active_path)
        self._load_source(active_path, silent=True)

    # ── Detection ───────────────────────────────────────────────────

    def _detect_face(self):
        if self._busy:
            return
        if not self._source_path:
            active_path = self.image_session.active_image_path
            if not active_path:
                self._status_label.config(text="No image in carousel", fg=COLORS["warning"])
                return
            self._path_var.set(active_path)
            self._load_source(active_path)
        elif self.image_session.active_image_path and (
            os.path.abspath(self.image_session.active_image_path) != os.path.abspath(self._original_path or "")
        ):
            # Keep detection source synced with currently active carousel image.
            active_path = self.image_session.active_image_path
            self._path_var.set(active_path)
            self._load_source(active_path)

        if not self._source_path:
            self._status_label.config(text="No source image loaded", fg=COLORS["warning"])
            return
        if not HAS_FACE_DEPS:
            self._status_label.config(
                text="Face Crop deps missing (see warning)", fg=COLORS["error"]
            )
            return
        retinaface_cls, retinaface_error = _load_retinaface()
        if retinaface_cls is None:
            repair_launcher = _platform_gui_repair_launcher()
            self._status_label.config(
                text=f"RetinaFace unavailable (TensorFlow/Keras backend mismatch or missing deps): {retinaface_error}",
                fg=COLORS["error"],
            )
            self.log(
                "Face Crop: RetinaFace/TensorFlow import failed. "
                f"Run {repair_launcher} for automatic dependency repair.",
                "error",
            )
            return

        self._busy = True
        self._detect_btn.config(state=tk.DISABLED, text="Detecting...")
        self._status_label.config(text="Running RetinaFace...", fg=COLORS["progress"])
        self.log("Face Crop: running RetinaFace detection...", "info")

        threading.Thread(
            target=self._detect_worker,
            args=(retinaface_cls,),
            daemon=True,
        ).start()

    def _detect_worker(self, retinaface_cls):
        try:
            source = self._source_path
            if not source:
                self._after_detect(None, None, "No source image loaded")
                return

            img = cv2.imread(source)
            if img is None:
                self._after_detect(None, None, "Could not read image with OpenCV")
                return

            try:
                faces = retinaface_cls.detect_faces(source)
            except Exception as exc:
                msg = str(exc)
                lowered = msg.lower()
                is_backend_runtime = (
                    "kerastensor" in lowered
                    or "tensorflow function" in lowered
                    or "symbolic placeholder" in lowered
                )
                if not is_backend_runtime:
                    raise
                self.log(
                    "Face Crop: RetinaFace backend incompatibility detected; "
                    "falling back to OpenCV detector.",
                    "warning",
                )
                fallback_box = self._detect_face_with_opencv_fallback(img)
                if fallback_box is None:
                    self._after_detect(
                        img,
                        None,
                        "No face detected (RetinaFace fallback via OpenCV)",
                    )
                    return
                self._after_detect(img, fallback_box, None)
                return
            if not faces or len(faces) == 0:
                self._after_detect(img, None, "No face detected")
                return

            # Pick largest face
            best_key: Optional[str] = None
            best_area = 0
            for key, data in faces.items():
                area = data["facial_area"]
                w = area[2] - area[0]
                h = area[3] - area[1]
                if w * h > best_area:
                    best_area = w * h
                    best_key = key

            if best_key is None:
                self._after_detect(img, None, "No face detected")
                return

            fa = faces[best_key]["facial_area"]
            fx, fy = fa[0], fa[1]
            fw, fh = fa[2] - fa[0], fa[3] - fa[1]
            self._after_detect(img, (fx, fy, fw, fh), None)

        except Exception as exc:
            self._after_detect(None, None, str(exc))

    def _detect_face_with_opencv_fallback(self, img):
        """Fallback detector when RetinaFace runtime is incompatible."""
        try:
            gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
            cascade_path = os.path.join(cv2.data.haarcascades, "haarcascade_frontalface_default.xml")
            classifier = cv2.CascadeClassifier(cascade_path)
            if classifier.empty():
                return None
            faces = classifier.detectMultiScale(
                gray,
                scaleFactor=1.1,
                minNeighbors=5,
                minSize=(60, 60),
            )
            if faces is None or len(faces) == 0:
                return None
            best = max(faces, key=lambda f: int(f[2]) * int(f[3]))
            fx, fy, fw, fh = [int(v) for v in best]
            return fx, fy, fw, fh
        except Exception:
            return None

    def _after_detect(self, cv2_img, face_box, error):
        def _update():
            self._busy = False
            self._detect_btn.config(state=tk.NORMAL, text="Detect Face and Crop")

            if error:
                self._status_label.config(text=error, fg=COLORS["error"])
                self.log(f"Face Crop: {error}", "error")
                self._add_carousel_btn.config(state=tk.DISABLED)
                return

            self._cv2_img = cv2_img
            self._face_box = face_box

            if face_box:
                fx, fy, fw, fh = face_box
                self._status_label.config(
                    text=f"Face found: {fw}x{fh} at ({fx},{fy})",
                    fg=COLORS["success"],
                )
                self.log(
                    f"Face Crop: detected face {fw}x{fh} at ({fx},{fy})", "success"
                )
                self._draw_source_with_box()
                self._update_crop_preview()
                self._add_carousel_btn.config(state=tk.NORMAL)
            else:
                self._status_label.config(text="No face detected", fg=COLORS["error"])
                self.log("Face Crop: no face detected in image", "warning")
                self._add_carousel_btn.config(state=tk.DISABLED)

        self.after(0, _update)

    # ── Crop Math (from validated test_crop.py) ─────────────────────

    def _compute_crop(self):
        """Return (x_start, y_start, x_end, y_end) for the current multiplier."""
        if self._cv2_img is None or self._face_box is None:
            return None
        fx, fy, fw, fh = self._face_box
        h_img, w_img = self._cv2_img.shape[:2]
        mult = self._multiplier_var.get()

        face_center_x = fx + (fw // 2)
        face_center_y = fy + (fh // 2)
        target_w = int(fw * mult)
        target_h = int(target_w * 1.333)  # 3:4 ratio

        x_start = face_center_x - (target_w // 2)
        y_start = face_center_y - (target_h // 2)
        x_end = x_start + target_w
        y_end = y_start + target_h

        # Boundary shifting
        if x_start < 0:
            x_end -= x_start
            x_start = 0
        if x_end > w_img:
            x_start -= x_end - w_img
            x_end = w_img
        if y_start < 0:
            y_end -= y_start
            y_start = 0
        if y_end > h_img:
            y_start -= y_end - h_img
            y_end = h_img

        # Final clamp
        x_start = max(0, x_start)
        y_start = max(0, y_start)
        x_end = min(w_img, x_end)
        y_end = min(h_img, y_end)

        return x_start, y_start, x_end, y_end

    # ── Canvas Drawing ──────────────────────────────────────────────

    def _show_on_canvas(self, canvas: tk.Canvas, pil_img: "Image.Image"):
        """Fit a PIL image into a canvas, centered."""
        canvas.update_idletasks()
        cw = max(canvas.winfo_width(), 100)
        ch = max(canvas.winfo_height(), 100)

        img = pil_img.copy()
        img.thumbnail((cw, ch))
        photo = ImageTk.PhotoImage(img)

        canvas.delete("all")
        canvas.create_image(cw // 2, ch // 2, image=photo, anchor="center")

        # Return photo ref to prevent GC
        return photo

    def _draw_source_with_box(self):
        """Redraw source image with a face bounding box overlay."""
        if not HAS_PIL or self._source_path is None:
            return
        try:
            pil_img = Image.open(self._source_path).copy()
            if self._face_box:
                fx, fy, fw, fh = self._face_box
                draw = ImageDraw.Draw(pil_img)
                draw.rectangle(
                    [fx, fy, fx + fw, fy + fh], outline="#00FF88", width=3
                )
            pil_img.thumbnail((400, 400))
            self._source_photo = self._show_on_canvas(self._source_canvas, pil_img)
        except Exception:
            pass

    def _update_crop_preview(self):
        """Recompute crop from cached data and show on crop canvas."""
        if not HAS_PIL or self._cv2_img is None:
            return
        coords = self._compute_crop()
        if coords is None:
            return
        x1, y1, x2, y2 = coords
        crop_bgr = self._cv2_img[y1:y2, x1:x2]
        if crop_bgr.size == 0:
            return

        self._crop_result = crop_bgr
        h, w = crop_bgr.shape[:2]
        self._crop_label.config(text=f"Crop Result  ({w}\u00d7{h})")
        # Convert BGR → RGB → PIL
        crop_rgb = cv2.cvtColor(crop_bgr, cv2.COLOR_BGR2RGB)
        pil_crop = Image.fromarray(crop_rgb)
        pil_crop.thumbnail((400, 400))
        self._crop_photo = self._show_on_canvas(self._crop_canvas, pil_crop)

    # ── Slider Callback ─────────────────────────────────────────────

    def _on_slider_changed(self, _value=None):
        if self._face_box is not None:
            self._update_crop_preview()

    def _adjust_multiplier(self, delta: float):
        val = round(self._multiplier_var.get() + delta, 1)
        val = max(1.0, min(3.0, val))
        self._multiplier_var.set(val)
        self._on_slider_changed()

    # ── Add crop to carousel ─────────────────────────────────────────

    def _add_crop_to_carousel(self):
        """Save current crop to gen-images/ and add to carousel."""
        path = self._save_crop()
        if path:
            self.log(f"Crop added to carousel: {path.name}", "success")

    # ── Save crop helper ─────────────────────────────────────────────

    def _save_crop(self) -> Optional[Path]:
        """Save current crop to disk and add to session. Returns path or None."""
        if self._crop_result is None or self._source_path is None:
            return None

        # Use original user-selected path (not the EXIF-corrected temp copy)
        origin = Path(self._original_path or self._source_path)
        gen_dir = Path(get_gen_images_folder(str(origin)))
        gen_dir.mkdir(parents=True, exist_ok=True)

        # Collision-safe naming
        out_path = gen_dir / f"{origin.stem}_crop.jpg"
        counter = 2
        while out_path.exists():
            out_path = gen_dir / f"{origin.stem}_crop_{counter}.jpg"
            counter += 1

        try:
            cv2.imwrite(str(out_path), self._crop_result)
        except Exception:
            out_path = Path(tempfile.gettempdir()) / f"{origin.stem}_crop.jpg"
            cv2.imwrite(str(out_path), self._crop_result)

        self.log(
            f"Face Crop: saved {out_path.name} "
            f"({self._crop_result.shape[1]}x{self._crop_result.shape[0]})",
            "success",
        )
        self._last_crop_path = str(out_path)
        self.image_session.add_image(str(out_path), "input", label=out_path.name)
        # Auto-set as similarity ref if none chosen yet
        if self.image_session.similarity_ref_index == -1:
            self.image_session.set_similarity_ref(self.image_session.count - 1)
            self.log("Auto-set crop as similarity reference", "info")
        return out_path

    # ── Send crop to next phase ─────────────────────────────────────

    def _send_to_prep(self):
        if self._auto_switch_var.get() and self._notebook_switcher_prep:
            self._notebook_switcher_prep()

    def _send_to_selfie(self):
        if self._auto_switch_var.get() and self._notebook_switcher_selfie:
            self._notebook_switcher_selfie()

    # ── Get source reference for tool actions ───────────────────────

    def _find_crop_ref_path(self) -> Optional[str]:
        """Find the first crop image in the session — the similarity reference.

        Scans carousel for the first input-type image with ``_crop`` in its
        filename.  Works across sessions because the crop file persists on disk.

        Trusts the cached ``entry.exists`` flag — fast but may be stale.
        For the per-pass similarity check use :meth:`_resolve_live_crop_ref`
        instead, which verifies on-disk presence right now.
        """
        for entry in self.image_session.images:
            if entry.source_type == "input" and "_crop" in entry.filename and entry.exists:
                return entry.path
        return None

    def _resolve_live_crop_ref(self) -> Optional[str]:
        """Resolve the similarity reference path RIGHT NOW (per-pass).

        Resilient version of :meth:`_find_crop_ref_path`. Falls back
        through:

        1. Session entries whose path actually exists on disk now.
           Re-queries presence via ``Path(entry.path).is_file()`` —
           ``ImageEntry.exists`` is a @property that already re-reads
           disk on every access (see ``kling_gui/image_state.py:93``),
           so no manual invalidation is needed; subsequent reads
           automatically reflect current state.
        2. ``self._last_crop_path`` if set and still on disk — covers
           "the entry got pruned but the file is still there".
        3. ``{stem}_crop.*`` glob in the current Step 0 source's
           ``gen-images/`` — covers "the absolute path drifted but the
           file is still in gen-images on this folder" (e.g. user
           moved the source folder after a crop was saved). Stem is
           ``glob.escape``d so filenames containing ``[``/``]``/``?``
           are matched literally.
        4. ``None`` — caller should skip similarity with a debug-level
           log (NOT warning/error). PR fix/step0-composite-and-rppg-v2.5.
        """
        import glob as _glob
        from pathlib import Path as _P

        def _safe_mtime(p: _P) -> float:
            """mtime for sort. Falls back to 0.0 if the file vanished
            between glob() and stat() — a race between the resolver and
            an external cleanup. Sort still works; the disappeared
            candidate sinks to the bottom and the next is_file() check
            filters it out."""
            try:
                return p.stat().st_mtime
            except (OSError, ValueError):
                return 0.0

        def _log_source(label: str, hit_path: str) -> None:
            """Debug-log which fallback source produced the live ref.
            Subagent L4 round 5: helps triage "similarity score looks
            wrong" reports without re-running the worker. Best-effort —
            silently no-op if `self.log` isn't wired yet (this method
            is also reachable from non-GUI contexts in tests).
            """
            try:
                self.log(
                    f"[SIM] live crop ref source={label} path={hit_path}",
                    "debug",
                )
            except Exception:
                pass

        # Step 1: walk session entries, verify on-disk. Defensive
        # exception list per PR #53 round 4: an entry whose path was
        # set to None by upstream code (or any non-str type) would
        # raise TypeError on Path() construction; guard against that
        # AND empty-string path before touching the filesystem.
        #
        # Snapshot the list before iterating (PR #53 round 7
        # Gemini cleanup) so a concurrent GUI-thread mutation of
        # image_session.images can't skip/duplicate entries
        # mid-iteration. Same idea as list(dict.items()) when the
        # dict may be mutated under us.
        for entry in list(self.image_session.images):
            if entry.source_type != "input":
                continue
            # Guard non-string filename (PR #53 round 7 Gemini).
            entry_filename = getattr(entry, "filename", None)
            if not isinstance(entry_filename, str):
                continue
            if "_crop" not in entry_filename:
                continue
            entry_path = getattr(entry, "path", None)
            if not entry_path:
                continue
            try:
                exists_now = _P(entry_path).is_file()
            except (OSError, ValueError, TypeError):
                exists_now = False
            if exists_now:
                _log_source("session_entry", entry_path)
                return entry_path

        # Step 2: last-saved crop path. Same TypeError guard for the
        # same reason — _last_crop_path is set from many call sites and
        # could go non-string in a future refactor.
        last = getattr(self, "_last_crop_path", None)
        if last:
            try:
                if _P(last).is_file():
                    _log_source("_last_crop_path", last)
                    return last
            except (OSError, ValueError, TypeError):
                pass

        # Step 3: glob gen-images siblings of the CURRENT Step 0
        # source (NOT self._get_gen_dir() — that helper is anchored
        # to image_session.images[0] which can be a different folder
        # in a multi-source carousel; CodeRabbit major round 3).
        # CodeRabbit PR #53 round 8: prefer `_original_path` over
        # `_source_path`. _source_path is the EXIF-corrected temp
        # copy used by the cropper; _original_path is the actual
        # user-selected file on disk. The gen-images folder we want
        # for the crop fallback is anchored to the user's selection,
        # not to the temp file.
        active_src = None
        for _src_attr in ("_original_path", "_source_path"):
            _src_val = getattr(self, _src_attr, None)
            if not _src_val:
                continue
            try:
                _p = _P(_src_val)
                if _p.is_file() or _p.parent.is_dir():
                    active_src = _src_val
                    break
            except (TypeError, ValueError, OSError):
                continue
        if active_src is None:
            try:
                _sess_active = self.image_session.active_image_path
            except AttributeError:
                _sess_active = None
            if _sess_active:
                active_src = _sess_active
        gen_dir = None
        if active_src:
            try:
                gen_dir = _P(get_gen_images_folder(str(active_src)))
            except (TypeError, ValueError, OSError):
                gen_dir = None
        if gen_dir is not None:
            try:
                gen_dir_is_dir = gen_dir.is_dir()
            except (OSError, ValueError, TypeError):
                gen_dir_is_dir = False
            if gen_dir_is_dir:
                active_stem = None
                try:
                    active_stem = (
                        _P(active_src).stem if active_src else None
                    )
                except (TypeError, ValueError):
                    active_stem = None
                # Subagent H3 round 5: when the active source is a
                # derived artifact (e.g. "alice-expanded.jpg"), its
                # exact-stem pattern ("alice-expanded_crop.*") may miss.
                # Before falling back to ANY *_crop.*, try a prefix-
                # relaxed match on the first hyphen-split segment
                # ("alice_crop.*") — handles the common "{name}-
                # expanded" / "{name}-cropped" derived-artifact case.
                # If THAT also misses AND there are multiple *_crop.*
                # siblings, REFUSE to pick (silent wrong-identity
                # scoring is worse than skipping similarity). Only
                # fall back to mtime-newest *_crop.* when there's
                # EXACTLY ONE candidate.
                #
                # All patterns use glob.escape so a stem like
                # "selfie[final]" or "clip (1)" is matched literally,
                # not as a glob character class. Same trap addressed in
                # automation/rppg.py::resolve_produced_output for the
                # rPPG metric-rename glob.
                stem_candidates = []
                if active_stem:
                    stem_candidates.append(active_stem)
                    # Prefix-relaxed: "alice-expanded" -> "alice".
                    head = active_stem.split("-", 1)[0]
                    if head and head != active_stem:
                        stem_candidates.append(head)
                # Exact + prefix-relaxed stem patterns (literal-escaped).
                for stem_idx, stem_cand in enumerate(stem_candidates):
                    label = (
                        "glob_stem_exact" if stem_idx == 0
                        else "glob_stem_prefix"
                    )
                    try:
                        matches = [
                            c for c in gen_dir.glob(
                                f"{_glob.escape(stem_cand)}_crop.*"
                            )
                            if c.is_file()
                        ]
                    except OSError:
                        matches = []
                    if matches:
                        matches.sort(key=_safe_mtime, reverse=True)
                        for cand in matches:
                            try:
                                if cand.is_file():
                                    _log_source(label, str(cand))
                                    return str(cand)
                            except (OSError, ValueError, TypeError):
                                continue
                # Last-resort generic pattern. ONLY return a winner
                # if there is exactly ONE *_crop.* in this folder —
                # multiple means we'd be guessing which subject's
                # crop to score against. Skip rather than guess.
                try:
                    all_crops = [
                        c for c in gen_dir.glob("*_crop.*")
                        if c.is_file()
                    ]
                except OSError:
                    all_crops = []
                if len(all_crops) == 1:
                    cand = all_crops[0]
                    try:
                        if cand.is_file():
                            _log_source("glob_solo", str(cand))
                            return str(cand)
                    except (OSError, ValueError, TypeError):
                        pass

        return None

    def _get_gen_dir(self) -> Optional[Path]:
        """Get the gen-images folder relative to the first image (source root)."""
        # Use first carousel entry as root source
        entries = self.image_session.images
        if entries:
            ref_path = entries[0].path
        else:
            ref_path = self.image_session.active_image_path
        if not ref_path:
            return None
        gen_dir = Path(get_gen_images_folder(ref_path))
        gen_dir.mkdir(parents=True, exist_ok=True)
        return gen_dir

    # ── Polish Crop ─────────────────────────────────────────────────

    def _on_polish_provider_changed(self, _event=None):
        self._toggle_polish_strength()

    def _toggle_polish_strength(self):
        """Show strength slider only when fal.ai is selected."""
        if "fal.ai" in self._polish_provider_var.get():
            self._polish_strength_frame.pack(side=tk.LEFT)
        else:
            self._polish_strength_frame.pack_forget()

    def _polish_crop(self):
        """Polish the active carousel image in a background thread."""
        image_path = self.image_session.active_image_path
        if not image_path or self._polish_busy:
            return

        gen_dir = self._get_gen_dir()
        if not gen_dir:
            self.log("No images in session", "warning")
            return

        self._polish_busy = True
        self._polish_btn.config(state=tk.DISABLED, text="Polishing...")

        provider_label = self._polish_provider_var.get()
        provider = "bfl" if "BFL" in provider_label else "fal"
        self._polish_status.config(
            text=f"Running {provider_label}...", fg=COLORS["progress"]
        )

        prompt = self.get_config().get("face_crop_polish_prompt", _DEFAULT_POLISH_PROMPT)

        # Build ops-based filename
        from kling_gui.tag_utils import increment_ops, build_ops_filename

        input_entry = self.image_session.active_entry
        input_ops = input_entry.ops if input_entry else {}
        new_ops = increment_ops(input_ops, "pol")

        stem = Path(image_path).stem
        output_name = build_ops_filename(stem, new_ops)
        output_path = str(gen_dir / output_name)
        counter = 2
        while os.path.exists(output_path):
            output_path = str(gen_dir / build_ops_filename(stem, new_ops, ext=f"_v{counter}.png"))
            counter += 1

        # Find crop ref for similarity (works across sessions)
        ref_path = self._find_crop_ref_path()
        self.log(f"[SIM] polish input={Path(image_path).name} ref={Path(ref_path).name if ref_path else 'none'}", "debug")

        def _worker():
            from crop_polisher import CropPolisher

            cfg = self.get_config()
            polisher = CropPolisher(
                falai_api_key=cfg.get("falai_api_key", ""),
                bfl_api_key=cfg.get("bfl_api_key", ""),
                freeimage_key=cfg.get("freeimage_api_key", ""),
            )
            polisher.set_progress_callback(
                lambda msg, lvl: self.log(f"Polish: {msg}", lvl)
            )

            result = polisher.polish(
                image_path=image_path,
                output_path=output_path,
                provider=provider,
                prompt=prompt,
                strength=self._polish_strength_var.get(),
            )

            similarity = None
            if ref_path and result:
                try:
                    from face_similarity import compute_face_similarity
                    sim_val = compute_face_similarity(ref_path, result, report_cb=self.log)
                    if sim_val is not None:
                        similarity = f"{sim_val}%"
                except Exception as exc:
                    self.after(0, lambda e=exc: self.log(f"Sim: {type(e).__name__}: {e!r}", "warning"))

            if result:
                self.after(0, lambda: self._on_polish_done(result, similarity, new_ops))
            else:
                self.after(0, lambda: self._on_polish_error("Polish failed (see log)"))

        threading.Thread(target=_worker, daemon=True).start()

    def _on_polish_done(self, result_path: str, similarity=None, ops=None):
        """Handle successful polish: add to carousel."""
        self._polish_busy = False
        self._polish_btn.config(text="AI Polish", state=tk.NORMAL)

        basename = os.path.basename(result_path)
        self._polish_status.config(text=f"Done: {basename}", fg=COLORS["success"])
        self.log(f"Polish: saved {basename}", "success")

        self.image_session.add_image(result_path, "polish", label=basename,
                                     similarity=similarity, ops=ops)

    def _on_polish_error(self, error: str):
        """Handle polish failure."""
        self._polish_busy = False
        self._polish_btn.config(text="AI Polish", state=tk.NORMAL)
        self._polish_status.config(text=error, fg=COLORS["error"])

    def _open_polish_prompt_editor(self):
        """Open a modal dialog to view/edit the polish prompt."""
        dialog = tk.Toplevel(self.winfo_toplevel())
        dialog.title("Polish Prompt")
        dialog.transient(self.winfo_toplevel())
        dialog.grab_set()
        dialog.configure(bg=COLORS["bg_main"])
        dialog.geometry("520x340")
        dialog.resizable(True, True)

        tk.Label(
            dialog,
            text="Instruction prompt sent to the AI editor:",
            bg=COLORS["bg_main"],
            fg=COLORS["text_light"],
            font=(FONT_FAMILY, 10),
            anchor="w",
        ).pack(fill=tk.X, padx=12, pady=(12, 4))

        text_widget = tk.Text(
            dialog,
            bg=COLORS["bg_input"],
            fg=COLORS["text_light"],
            insertbackground=COLORS["text_light"],
            font=(FONT_FAMILY, 10),
            wrap=tk.WORD,
            height=10,
        )
        text_widget.pack(fill=tk.BOTH, expand=True, padx=12, pady=4)

        current_prompt = self.get_config().get(
            "face_crop_polish_prompt", _DEFAULT_POLISH_PROMPT
        )
        text_widget.insert("1.0", current_prompt)

        btn_frame = tk.Frame(dialog, bg=COLORS["bg_main"])
        btn_frame.pack(fill=tk.X, padx=12, pady=(4, 12))

        def _reset():
            text_widget.delete("1.0", tk.END)
            text_widget.insert("1.0", _DEFAULT_POLISH_PROMPT)

        def _save():
            new_prompt = text_widget.get("1.0", tk.END).strip()
            self.config["face_crop_polish_prompt"] = new_prompt
            self._save_config_now()
            dialog.destroy()

        ttk.Button(
            btn_frame, text="Reset to Default",
            style=TTK_BTN_SECONDARY, command=_reset,
        ).pack(side=tk.LEFT)

        ttk.Button(
            btn_frame, text="Cancel",
            style=TTK_BTN_SECONDARY, command=dialog.destroy,
        ).pack(side=tk.RIGHT, padx=(6, 0))

        ttk.Button(
            btn_frame, text="Save",
            style=TTK_BTN_PRIMARY, command=_save,
        ).pack(side=tk.RIGHT)

    # ── Expand prompt editor ────────────────────────────────────────

    def _open_expand_prompt_editor(self):
        """Open a modal dialog to view/edit the outpaint/expand prompt."""
        dialog = tk.Toplevel(self.winfo_toplevel())
        dialog.title("Step 0 Expand Prompt")
        dialog.transient(self.winfo_toplevel())
        dialog.grab_set()
        dialog.configure(bg=COLORS["bg_main"])
        dialog.geometry("520x280")
        dialog.resizable(True, True)

        tk.Label(
            dialog,
            text="Optional prompt for the outpaint/expand model:",
            bg=COLORS["bg_main"],
            fg=COLORS["text_light"],
            font=(FONT_FAMILY, 10),
            anchor="w",
        ).pack(fill=tk.X, padx=12, pady=(12, 4))

        text_widget = tk.Text(
            dialog,
            bg=COLORS["bg_input"],
            fg=COLORS["text_light"],
            insertbackground=COLORS["text_light"],
            font=(FONT_FAMILY, 10),
            wrap=tk.WORD,
            height=8,
        )
        text_widget.pack(fill=tk.BOTH, expand=True, padx=12, pady=4)
        text_widget.insert("1.0", self._outpaint_prompt_str)

        btn_frame = tk.Frame(dialog, bg=COLORS["bg_main"])
        btn_frame.pack(fill=tk.X, padx=12, pady=(4, 12))

        def _clear():
            text_widget.delete("1.0", tk.END)

        def _save():
            self._outpaint_prompt_str = text_widget.get("1.0", tk.END).strip()
            self._save_config_now()
            dialog.destroy()

        ttk.Button(
            btn_frame, text="Clear",
            style=TTK_BTN_SECONDARY, command=_clear,
        ).pack(side=tk.LEFT)

        ttk.Button(
            btn_frame, text="Cancel",
            style=TTK_BTN_SECONDARY, command=dialog.destroy,
        ).pack(side=tk.RIGHT, padx=(6, 0))

        ttk.Button(
            btn_frame, text="Save",
            style=TTK_BTN_PRIMARY, command=_save,
        ).pack(side=tk.RIGHT)

    # ── Upscale ──────────────────────────────────────────────────────

    def _on_upscale_provider_changed(self, _event=None):
        self._toggle_crystal_settings()

    def _toggle_crystal_settings(self):
        """Show Crystal-specific settings only when Crystal is selected."""
        if "Crystal" in self._upscale_provider_var.get():
            self._upscale_scale_frame.pack(side=tk.LEFT)
            self._crystal_settings_frame.pack(fill=tk.X)
        else:
            self._upscale_scale_frame.pack_forget()
            self._crystal_settings_frame.pack_forget()

    def _upscale_image(self):
        """Upscale the active carousel image in a background thread."""
        image_path = self.image_session.active_image_path
        if not image_path or self._upscale_busy:
            return

        gen_dir = self._get_gen_dir()
        if not gen_dir:
            self.log("No images in session", "warning")
            return

        self._upscale_busy = True
        self._upscale_btn.config(state=tk.DISABLED, text="Upscaling...")

        provider_label = self._upscale_provider_var.get()
        provider = "crystal" if "Crystal" in provider_label else "aura_sr"
        self._upscale_status.config(
            text=f"Running {provider_label}...", fg=COLORS["progress"]
        )

        # Build ops-based filename
        from kling_gui.tag_utils import increment_ops, build_ops_filename

        input_entry = self.image_session.active_entry
        input_ops = input_entry.ops if input_entry else {}
        new_ops = increment_ops(input_ops, "ups")

        stem = Path(image_path).stem
        output_name = build_ops_filename(stem, new_ops)
        output_path = str(gen_dir / output_name)
        counter = 2
        while os.path.exists(output_path):
            output_path = str(gen_dir / build_ops_filename(stem, new_ops, ext=f"_v{counter}.png"))
            counter += 1

        ref_path = self._find_crop_ref_path()
        self.log(f"[SIM] upscale input={Path(image_path).name} ref={Path(ref_path).name if ref_path else 'none'}", "debug")

        def _worker():
            from crop_upscaler import CropUpscaler

            cfg = self.get_config()
            upscaler = CropUpscaler(
                falai_api_key=cfg.get("falai_api_key", ""),
                freeimage_key=cfg.get("freeimage_api_key", ""),
            )
            upscaler.set_progress_callback(
                lambda msg, lvl: self.log(f"Upscale: {msg}", lvl)
            )

            scale_str = self._upscale_scale_var.get()
            scale_factor = 4 if scale_str == "4x" else 2

            result = upscaler.upscale(
                image_path=image_path,
                output_path=output_path,
                provider=provider,
                scale_factor=scale_factor,
                creativity=self._upscale_creativity_var.get(),
                resemblance=self._upscale_resemblance_var.get(),
            )

            similarity = None
            if ref_path and result:
                try:
                    from face_similarity import compute_face_similarity
                    sim_val = compute_face_similarity(ref_path, result, report_cb=self.log)
                    if sim_val is not None:
                        similarity = f"{sim_val}%"
                except Exception as exc:
                    self.after(0, lambda e=exc: self.log(f"Sim: {type(e).__name__}: {e!r}", "warning"))

            if result:
                self.after(0, lambda: self._on_upscale_done(result, similarity, new_ops))
            else:
                self.after(0, lambda: self._on_upscale_error("Upscale failed (see log)"))

        threading.Thread(target=_worker, daemon=True).start()

    def _on_upscale_done(self, result_path: str, similarity=None, ops=None):
        self._upscale_busy = False
        self._upscale_btn.config(text="Upscale", state=tk.NORMAL)
        basename = os.path.basename(result_path)
        self._upscale_status.config(text=f"Done: {basename}", fg=COLORS["success"])
        self.log(f"Upscale: saved {basename}", "success")
        self.image_session.add_image(result_path, "upscale", label=basename,
                                     similarity=similarity, ops=ops)

    def _on_upscale_error(self, error: str):
        self._upscale_busy = False
        self._upscale_btn.config(text="Upscale", state=tk.NORMAL)
        self._upscale_status.config(text=error, fg=COLORS["error"])

    # ── Outpaint ─────────────────────────────────────────────────────

    def _outpaint_image(self):
        """Outpaint (expand) the active carousel image in a background thread."""
        image_path = self.image_session.active_image_path
        if not image_path or self._outpaint_busy:
            return

        cfg = self.get_config()
        api_key = cfg.get("falai_api_key", "")
        if not api_key:
            self.log("fal.ai API key required", "error")
            return

        # Provider selection — only pass BFL key when BFL selected
        provider = self._outpaint_provider_var.get()  # "bfl" or "fal"
        use_bfl = provider == "bfl" and bool(cfg.get("bfl_api_key"))

        if provider == "bfl" and not cfg.get("bfl_api_key"):
            self.log("BFL API key required — set in API Keys or switch to fal.ai", "error")
            return

        max_per_side = 2048 if use_bfl else 700

        gen_dir = self._get_gen_dir()
        if not gen_dir:
            self.log("No images in session", "warning")
            return

        mode = self._expand_mode_var.get()
        prompt = self._outpaint_prompt_str
        output_format = self._outpaint_format_var.get()

        if mode == "percentage":
            try:
                pct = self._pct_var.get()
            except (tk.TclError, ValueError):
                self.log("Invalid percentage value", "error")
                return
            try:
                from PIL import Image as PILImage, ImageOps as PILImageOps
                with PILImage.open(image_path) as img:
                    img = PILImageOps.exif_transpose(img)
                    width, height = img.size
                pct_frac = pct / 100.0
                expand_left = expand_right = min(max_per_side, int(width * pct_frac))
                expand_top = expand_bottom = min(max_per_side, int(height * pct_frac))
            except Exception as e:
                self.log(f"Could not read image dimensions: {e}", "error")
                return
            self.log(
                f"Expanding {pct}% → L={expand_left} R={expand_right} "
                f"T={expand_top} B={expand_bottom} px",
                "info",
            )
        else:
            try:
                expand_left = self._expand_left_var.get()
                expand_right = self._expand_right_var.get()
                expand_top = self._expand_top_var.get()
                expand_bottom = self._expand_bottom_var.get()
            except (tk.TclError, ValueError):
                self.log("Invalid pixel values", "error")
                return

        # Plan deterministic per-pass output paths up-front. Step 0 expand
        # outputs use the dedicated "-expanded" / "-expanded-2x" naming
        # convention (NOT the ops-tag scheme from build_ops_filename, which
        # is still used elsewhere for polish/upscale). Each pass gets its
        # own collision suffix so a re-run never overwrites earlier files.
        from ..tag_utils import increment_ops, build_expand_filenames

        input_entry = self.image_session.active_entry
        input_ops = input_entry.ops if input_entry else {}
        do_2x = bool(self._outpaint_double_expand_var.get())

        pass1_path, pass2_path = build_expand_filenames(
            base_stem=Path(image_path).stem,
            ext=output_format,
            gen_dir=gen_dir,
            do_2x=do_2x,
        )
        planned_paths = [pass1_path] + ([pass2_path] if pass2_path is not None else [])

        ref_path = self._find_crop_ref_path()
        self.log(f"[SIM] outpaint input={Path(image_path).name} ref={Path(ref_path).name if ref_path else 'none'}", "debug")

        self._outpaint_busy = True
        self._outpaint_run_token += 1
        run_token = self._outpaint_run_token
        self._outpaint_cancel_event = threading.Event()
        self._expand_btn.config(state=tk.DISABLED, text="Expanding...")
        self._expand_abort_btn.config(state=tk.NORMAL)
        self._outpaint_status.config(text="Processing...", fg=COLORS["progress"])

        bfl_key = cfg.get("bfl_api_key") if use_bfl else None
        composite_mode = self._outpaint_composite_var.get()

        def _worker():
            try:
                from outpaint_generator import OutpaintGenerator
                # Hoisted out of the per-pass loop per code-review L5
                # on subagent ae2dd01f. Python's import cache made the
                # in-loop import a perf-non-issue, but hoisting makes
                # the failure surface (ImportError) unambiguous and
                # matches the OutpaintGenerator import style above.
                from face_similarity import compute_face_similarity

                # Tk-safe log callback (code-review M1 on subagent
                # ae2dd01f). face_similarity invokes report_cb directly
                # from the worker thread; without the after-marshal it
                # mutates the LogDisplay Tk widget from a non-GUI thread
                # which is undefined on macOS.
                def tk_safe_log(message: str, level: str) -> None:
                    self.winfo_toplevel().after(
                        0,
                        lambda m=message, lvl=level: self.log(m, lvl),
                    )

                # Recovery-round banner (PR #48 round 6): make do_2x +
                # composite_mode + every planned target visible at
                # worker entry. The user's last "expanded but not
                # expanded" report had no log line that pinned which
                # of those three the worker actually saw. Without
                # this banner the silent-failure modes (composite
                # disabled, do_2x off when user thought it was on,
                # wrong target paths) are indistinguishable from a
                # true compositing regression in the log.
                self.winfo_toplevel().after(
                    0,
                    lambda d=do_2x, cm=composite_mode:
                    self.log(
                        f"Step 0 expand: do_2x={d} composite_mode={cm}",
                        "info",
                    ),
                )
                for _i, _planned in enumerate(planned_paths, start=1):
                    self.winfo_toplevel().after(
                        0,
                        lambda i=_i, name=_planned.name:
                        self.log(
                            f"Step 0 expand: planned pass {i} -> {name}",
                            "info",
                        ),
                    )
                if composite_mode == "none":
                    self.winfo_toplevel().after(
                        0,
                        lambda: self.log(
                            'Step 0 expand: composite mode is "None" — '
                            'preserve-seamless blending is DISABLED. '
                            'Original pixels will NOT be preserved on top '
                            'of the expanded canvas. Set Composite to '
                            '"Preserve Seamless" for blended output.',
                            "warning",
                        ),
                    )

                freeimage_key = cfg.get("freeimage_api_key")
                gen = OutpaintGenerator(
                    api_key, freeimage_key=freeimage_key, bfl_api_key=bfl_key,
                )
                self.outpaint_generator = gen
                gen.set_progress_callback(tk_safe_log)
                current_input = image_path
                current_ops = dict(input_ops or {})
                # Per-pass results: (path, similarity_str_or_None, ops_dict).
                per_pass_results = []
                total_passes = len(planned_paths)
                for pass_index, planned_target in enumerate(planned_paths):
                    pass_no = pass_index + 1
                    self.winfo_toplevel().after(
                        0,
                        lambda i=pass_no, n=total_passes, cm=composite_mode,
                               inp=Path(current_input).name,
                               outp=planned_target.name:
                        self.log(
                            f"Expand pass {i}/{n}: composite_mode={cm} "
                            f"in={inp} out={outp}",
                            "info",
                        ),
                    )
                    # PR #48 round 6 recovery: call gen.outpaint with
                    # output_path=None so the generator auto-names + runs
                    # its compositing step exactly the way main does. The
                    # naming convention is then enforced by a best-effort
                    # rename below. This isolates the naming work from the
                    # compositing work — any visual blend regression can
                    # no longer be blamed on the output_path argument.
                    result = gen.outpaint(
                        image_path=current_input,
                        output_folder=str(gen_dir),
                        expand_left=expand_left,
                        expand_right=expand_right,
                        expand_top=expand_top,
                        expand_bottom=expand_bottom,
                        prompt=prompt,
                        output_format=output_format,
                        composite_mode=composite_mode,
                        output_path=None,
                        poll_timeout_seconds=get_outpaint_fal_timeout_seconds(cfg),
                        cancel_event=self._outpaint_cancel_event,
                    )
                    if not result:
                        break

                    if Path(result) != planned_target:
                        try:
                            Path(result).replace(planned_target)
                            result = str(planned_target)
                        except OSError as exc:
                            self.winfo_toplevel().after(
                                0,
                                lambda e=exc, src=result,
                                       dst=str(planned_target):
                                self.log(
                                    f"Outpaint: rename {Path(src).name} -> "
                                    f"{Path(dst).name} failed: {e}; "
                                    f"keeping auto-name",
                                    "warning",
                                ),
                            )

                    current_ops = increment_ops(current_ops, "exp")

                    sim = None
                    # Re-resolve the crop reference RIGHT BEFORE each
                    # similarity call (PR fix/step0-composite-and-rppg-v2.5).
                    # The outer-scope ref_path captured at worker start
                    # could be stale if the crop file moved or its
                    # entry.exists flag went out of sync with disk
                    # state. _resolve_live_crop_ref verifies on-disk and
                    # falls back through last_crop_path + gen-images
                    # glob before giving up.
                    live_ref = self._resolve_live_crop_ref()
                    if live_ref is None:
                        # Gemini PR #53 round 5 MED: use the locally-
                        # scoped tk_safe_log helper instead of an inline
                        # winfo_toplevel().after — same widget-lifecycle
                        # safety, less code, matches the rest of this
                        # worker thread.
                        tk_safe_log(
                            f"Sim pass {pass_no}: skipped — no crop "
                            f"reference on disk (looked in session "
                            f"entries, _last_crop_path, "
                            f"gen-images/*_crop.*).",
                            "debug",
                        )
                    else:
                        try:
                            sim_val = compute_face_similarity(
                                live_ref, result, report_cb=tk_safe_log,
                            )
                            if sim_val is not None:
                                sim = f"{sim_val}%"
                        except FileNotFoundError as exc:
                            # Disk state changed between resolve and
                            # compute. Downgrade to info — this is a
                            # race, not a real failure.
                            tk_safe_log(
                                f"Sim pass {pass_no}: crop vanished "
                                f"mid-call ({exc}); skipping silently.",
                                "info",
                            )
                        except Exception as exc:
                            tk_safe_log(
                                f"Sim pass {pass_no}: "
                                f"{type(exc).__name__}: {exc!r}",
                                "warning",
                            )

                    per_pass_results.append((result, sim, dict(current_ops)))
                    current_input = result

                self.winfo_toplevel().after(
                    0,
                    lambda r=per_pass_results, t=total_passes:
                    self._on_outpaint_done(r, t, run_token),
                )
            except Exception as e:
                err = str(e)
                self.winfo_toplevel().after(0, lambda: self._on_outpaint_error(err, run_token))

        threading.Thread(target=_worker, daemon=True).start()

    def _abort_outpaint(self):
        if not self._outpaint_busy or self._outpaint_cancel_event is None:
            return
        self._outpaint_cancel_event.set()
        self.log("Expand abort requested by user", "warning")
        self._outpaint_status.config(text="Aborting...", fg=COLORS["warning"])
        self._expand_abort_btn.config(state=tk.DISABLED, text="Aborting...")

    def _on_outpaint_done(
        self,
        per_pass_results: List[Tuple[str, Optional[str], Dict[str, int]]],
        total_passes: int,
        run_token: Optional[int] = None,
    ) -> None:
        """Finalize a Step 0 expand run.

        ``per_pass_results`` is a list of ``(path, similarity_str|None,
        ops_dict)`` tuples - one entry per SUCCESSFUL pass.
        ``total_passes`` is how many passes the worker tried (1 for
        single-pass, 2 for 2x).

        Three success paths:
        - All passes succeeded → status "Done: <last>", carousel gets
          every entry.
        - Some passes succeeded then a later pass failed (partial 2x) →
          status "Partial: K/N", carousel gets the successful entries,
          warning log with the underlying error detail (code-review H1
          on subagent ae2dd01f).
        - Zero passes succeeded → status "Failed", error log.

        Even on cancellation, successful passes are added to the carousel
        before the abort short-circuit (code-review H2 on subagent
        ae2dd01f) — otherwise the on-disk file is orphaned and the user
        loses real work to a click.
        """
        if run_token is not None and run_token != self._outpaint_run_token:
            return
        cancelled = (
            self._outpaint_cancel_event is not None
            and self._outpaint_cancel_event.is_set()
        )
        self._outpaint_busy = False
        self._outpaint_cancel_event = None
        self._expand_btn.config(text="Expand Image", state=tk.NORMAL)
        self._expand_abort_btn.config(text="Abort", state=tk.DISABLED)

        # H2: surface every successfully-saved pass to the carousel
        # FIRST, regardless of cancel/partial state. The file is on disk
        # whether or not the user clicked Abort mid-run.
        for path, sim, ops in per_pass_results:
            basename = os.path.basename(path)
            self.image_session.add_image(
                path, "outpaint", label=basename,
                similarity=sim, ops=ops,
            )
            self.log(f"Outpaint: saved {basename}", "success")

        if cancelled:
            self._outpaint_status.config(
                text="Aborted by user", fg=COLORS["warning"],
            )
            if per_pass_results:
                self.log(
                    f"Expand aborted by user — kept "
                    f"{len(per_pass_results)} successful pass(es) "
                    f"in carousel",
                    "warning",
                )
            else:
                self.log("Expand aborted by user", "warning")
            return

        if not per_pass_results:
            self._outpaint_status.config(text="Failed", fg=COLORS["error"])
            detail = ""
            gen = getattr(self, "outpaint_generator", None)
            if gen is not None and hasattr(gen, "get_last_outpaint_error_detail"):
                detail = gen.get_last_outpaint_error_detail() or ""
            from outpaint_generator import OutpaintGenerator
            msg = OutpaintGenerator.format_error_detail(detail)
            self.log(msg, "error")
            return

        # H1: distinguish full-success from partial-2x-success.
        final_basename = os.path.basename(per_pass_results[-1][0])
        if len(per_pass_results) >= total_passes:
            self._outpaint_status.config(
                text=f"Done: {final_basename}", fg=COLORS["success"],
            )
        else:
            failed_pass = len(per_pass_results) + 1
            detail = ""
            gen = getattr(self, "outpaint_generator", None)
            if gen is not None and hasattr(gen, "get_last_outpaint_error_detail"):
                detail = gen.get_last_outpaint_error_detail() or ""
            if detail:
                from outpaint_generator import OutpaintGenerator
                err_msg = OutpaintGenerator.format_error_detail(detail)
            else:
                err_msg = "no detail available"
            self._outpaint_status.config(
                text=(
                    f"Partial: {len(per_pass_results)}/{total_passes} "
                    f"(pass {failed_pass} failed)"
                ),
                fg=COLORS["warning"],
            )
            self.log(
                f"Outpaint: partial 2x — pass {failed_pass}/{total_passes} "
                f"failed. {err_msg}",
                "warning",
            )

    def _on_outpaint_error(self, error, run_token=None):
        if run_token is not None and run_token != self._outpaint_run_token:
            return
        self._outpaint_busy = False
        self._outpaint_cancel_event = None
        self._expand_btn.config(text="Expand Image", state=tk.NORMAL)
        self._expand_abort_btn.config(text="Abort", state=tk.DISABLED)
        self._outpaint_status.config(text=error, fg=COLORS["error"])
        self.log(f"Outpaint error: {error}", "error")

    # ── Config Persistence ──────────────────────────────────────────

    def get_config_updates(self) -> dict:
        updates = {
            "face_crop_multiplier": self._multiplier_var.get(),
            "face_crop_auto_switch": self._auto_switch_var.get(),
            "face_crop_polish_provider": self._polish_provider_var.get(),
            "polish_strength": self._polish_strength_var.get(),
            "upscale_provider": self._upscale_provider_var.get(),
            "upscale_scale": self._upscale_scale_var.get(),
            "upscale_creativity": self._upscale_creativity_var.get(),
            "upscale_resemblance": self._upscale_resemblance_var.get(),
            "outpaint_expand_mode": self._expand_mode_var.get(),
            "outpaint_expand_percentage": self._pct_var.get(),
            "outpaint_expand_left": self._expand_left_var.get(),
            "outpaint_expand_right": self._expand_right_var.get(),
            "outpaint_expand_top": self._expand_top_var.get(),
            "outpaint_expand_bottom": self._expand_bottom_var.get(),
            "outpaint_format": self._outpaint_format_var.get(),
            "outpaint_composite_mode": self._outpaint_composite_var.get(),
            "outpaint_provider": self._outpaint_provider_var.get(),
            "outpaint_double_expand": self._outpaint_double_expand_var.get(),
            # One-time reset marker — persisted as True after first
            # launch under fix/step0-composite-and-rppg-v2.5 so
            # subsequent launches honor the user's sticky choice.
            "outpaint_2x_default_reset_v2": True,
            "accordion_expanded": self._expanded_sections,
        }
        # Persist Step 0 face-crop expand prompt (Phase G of
        # polish/v2.3: section-specific key, NOT the legacy
        # shared key — keeps Step 0 / Step 2.5 / Outpaint-tab
        # prompts independent of each other).
        updates["face_crop_expand_prompt"] = self._outpaint_prompt_str
        # Always persist polish prompt (reads from shared config dict)
        updates["face_crop_polish_prompt"] = self.config.get(
            "face_crop_polish_prompt", _DEFAULT_POLISH_PROMPT
        )
        return updates
