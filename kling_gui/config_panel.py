"""
Config Panel Widget - Model selection, output mode, and prompt editing.
With dynamic model fetching from fal.ai API.
"""

import tkinter as tk
from tkinter import ttk
from typing import Callable, Optional, List, Dict
import threading
import time
import os
import re
import logging
import sys
from tk_dialogs import select_directory, select_open_files
# COLORS/FONT_FAMILY are intentionally duplicated below (documented
# inconsistency in CLAUDE.md); we still import this one macOS button
# helper from the single source of truth rather than re-implement it.
from .theme import TTK_BTN_SECONDARY, TTK_BTN_COMPACT

try:
    from tkinterdnd2 import DND_FILES as _DND_FILES
    _HAS_DND = True
except ImportError:
    _DND_FILES = None
    _HAS_DND = False

if os.getenv("SELFIEGEN_MAC_DISABLE_DND", "0") == "1":
    _HAS_DND = False


def _dnd_live() -> bool:
    """Live drag-and-drop availability.

    ``_HAS_DND`` above is True whenever ``import tkinterdnd2`` succeeded — but
    the native tkdnd library can still fail to LOAD at runtime, in which case
    ``drop_zone.create_dnd_root()`` catches the failure and flips the SHARED
    ``drop_zone.HAS_DND`` to False. This module's own ``_HAS_DND`` is a separate
    by-value flag that never sees that runtime flip, so a gate on bare
    ``_HAS_DND`` would still attempt ``drop_target_register`` against a root with
    no tkdnd loaded. Read the live shared flag (falling back to our import-time
    value if drop_zone isn't importable) — mirrors main_window._dnd_live()."""
    if not _HAS_DND:
        return False
    try:
        from . import drop_zone as _dz
        return bool(getattr(_dz, "HAS_DND", _HAS_DND))
    except Exception:
        return _HAS_DND


# Color palette
COLORS = {
    "bg_main": "#2D2D30",
    "bg_panel": "#3C3C41",
    "bg_input": "#464649",
    "text_light": "#DCDCDC",
    "text_dim": "#B4B4B4",
    "accent_blue": "#6496FF",
    "border": "#5A5A5E",
    # Reconciled with theme.py's single source of truth (#FFA500) — this local
    # copy had drifted to #FFB347, rendering a visibly different warning color
    # than the rest of the app (G1).
    "warning": "#FFA500",
    "success": "#64FF64",
    "error": "#FF6464",
    "text_unsupported": "#666666",
    "bg_unsupported": "#3A3A3A",
}

# Import centralized model metadata to avoid duplication
from model_metadata import MODEL_METADATA, get_model_display_name, _endpoint_to_short_name

# Minimal fallback - ONLY used when API fails AND no cache exists
# Models change frequently - this is just a safety net with user's preferred model
# The app dynamically fetches all available models from fal.ai API
FALLBACK_MODELS = MODEL_METADATA

# fal.ai URLs for model browsing and API reference
FAL_MODELS_URL = "https://fal.ai/models?categories=image-to-video"
FAL_EXPLORE_URL = "https://fal.ai/explore/models"
FAL_API_DOCS_URL = "https://docs.fal.ai"

# Global font — change this one line to switch the entire UI typeface.
# JetBrains Mono and Inter look great if installed; Segoe UI is the safe fallback.
FONT_FAMILY = "Helvetica" if sys.platform == "darwin" else "Segoe UI"
EMOJI_FONT_FAMILY = "Apple Color Emoji" if sys.platform == "darwin" else "Segoe UI Emoji"

# Vertical breathing room between the Step-3 (Video) config rows. Windows
# Tk renders Checkbutton/Combobox/Entry taller than macOS, so the stacked
# Logging/Filter/Video/Motion rows overflow on Windows while macOS is fine.
# Tighten the inter-row gap on Windows only; macOS keeps its roomier spacing.
_IS_MACOS = sys.platform == "darwin"
_ROW_PADY = (0, 4) if _IS_MACOS else (0, 1)
_ROW_PADY_TIGHT = (0, 2) if _IS_MACOS else (0, 1)

# UI Configuration
COMBOBOX_DROPDOWN_HEIGHT = 25  # Number of items visible in dropdown (default ~10)

# Module logger
logger = logging.getLogger(__name__)


def _safe_stderr(msg: str) -> None:
    """Write to stderr, tolerating ``sys.stderr is None`` (pythonw.exe windowed
    builds have no console → stderr is None; a bare write would AttributeError
    and crash the GUI). gemini HIGH."""
    try:
        if sys.stderr is not None:
            sys.stderr.write(msg)
    except Exception:
        pass


class HoverTooltip:
    """Dark-themed floating tooltip shown when hovering a widget.

    Root-cause note (GPT diagnosis 2026-05-21): tall tooltips covering
    their own hover target cause Tk to fire ``<Leave>`` on the trigger
    widget the moment the Toplevel paints over it, which destroys the
    tooltip in ``_hide()`` and starts a flicker/no-show loop. The fix
    has two parts:

      1. Position the tooltip to the RIGHT of the trigger when there's
         room (else LEFT, else clamp), so it doesn't overlap the
         widget that's listening for ``<Leave>``.
      2. Keep the lifecycle dead-simple: ``<Enter>`` → ``_show``,
         ``<Leave>`` → ``_hide``. No delayed show, no off-screen
         staging, no withdraw/deiconify — those layered hacks made the
         oldcam tooltip stop showing at all on Windows.
    """

    _BG = "#1A1A1E"
    _FG = "#DCDCDC"
    _BORDER = "#6496FF"
    _WRAP = 500  # px wraplength

    def __init__(self, widget: tk.Widget, text_func):
        """
        Args:
            widget: Widget that triggers the tooltip on hover.
            text_func: Callable() → str evaluated at show-time.
                       Empty/None string suppresses the popup.
        """
        self._widget = widget
        self._text_func = text_func
        self._tip: Optional[tk.Toplevel] = None
        widget.bind("<Enter>", self._show)
        widget.bind("<Leave>", self._hide)
        # M1 (subagent on cac29c8f): if the widget is destroyed while
        # the tooltip is showing (e.g. pill row torn down on slot-load),
        # <Leave> never fires + the floating Toplevel stays orphaned.
        # Bind <Destroy> to tear down the tip explicitly. add="+" so we
        # don't clobber any existing <Destroy> binding on the widget.
        try:
            widget.bind("<Destroy>", self._on_widget_destroy, add="+")
        except tk.TclError:
            pass

    def _show(self, event=None):
        text = self._text_func()
        if not text or self._tip:
            return

        self._tip = tk.Toplevel(self._widget)
        self._tip.wm_overrideredirect(True)

        outer = tk.Frame(
            self._tip, bg=self._BG,
            highlightbackground=self._BORDER, highlightthickness=1,
        )
        outer.pack()
        tk.Label(
            outer, text=text,
            bg=self._BG, fg=self._FG,
            font=(FONT_FAMILY, 9),
            wraplength=self._WRAP, justify=tk.LEFT,
            padx=14, pady=10,
        ).pack(anchor="w")

        self._tip.update_idletasks()
        tip_w = self._tip.winfo_reqwidth()
        tip_h = self._tip.winfo_reqheight()

        wx = self._widget.winfo_rootx()
        wy = self._widget.winfo_rooty()
        ww = self._widget.winfo_width()
        wh = self._widget.winfo_height()
        sw = self._widget.winfo_screenwidth()
        sh = self._widget.winfo_screenheight()

        # HORIZONTAL — prefer RIGHT of the trigger so the tooltip
        # doesn't paint over its own hover target (GPT diagnosis
        # 2026-05-21: covering the trigger was the source of the
        # flicker loop — Tk fires <Leave> the moment the Toplevel
        # overlaps the icon, destroying the tooltip).
        right_x = wx + ww + 8
        left_x = wx - tip_w - 8
        if right_x + tip_w <= sw - 20:
            x = right_x
        elif left_x >= 20:
            x = left_x
        else:
            # Last-resort clamp — keep on-screen even if it overlaps
            # (better than off-screen). Try to bias away from the
            # trigger center.
            x = max(20, min(wx, sw - tip_w - 20))

        # VERTICAL — below by default; flip above if no room; pin to
        # top if neither fits (very tall tooltip on low-res screen).
        below_y = wy + wh + 4
        above_y = wy - tip_h - 4
        if below_y + tip_h <= sh - 40:
            y = below_y
        elif above_y >= 20:
            y = above_y
        else:
            y = 20

        self._tip.wm_geometry(f"+{x}+{y}")

    def _hide(self, event=None):
        if self._tip:
            try:
                self._tip.destroy()
            except tk.TclError:
                pass
            self._tip = None

    def _on_widget_destroy(self, _event=None) -> None:
        """Tear down the floating tooltip when the parent widget dies.

        Without this, a HoverTooltip on a transient widget (e.g. the
        Video Inspector pill row, which is rebuilt on every slot load)
        leaves an orphan Toplevel on screen whenever the parent is
        destroyed while the mouse is still hovering it (<Leave> never
        fires). M1 finding on cac29c8f.
        """
        self._hide()


class ModelFetcher:
    """Fetches available models from fal.ai API dynamically."""

    CACHE_TTL = 3600  # 1 hour cache (models don't change that often)

    @staticmethod
    def fetch_models(
        api_key: str, callback: Callable[[List[Dict], Optional[str]], None]
    ):
        """
        Fetch models in a background thread.

        Args:
            api_key: fal.ai API key
            callback: Called with (models_list, error_message) when done
        """

        def _fetch():
            try:
                import requests

                headers = {"Authorization": f"Key {api_key}"}
                all_models = []
                cursor = None
                seen_cursors = set()

                # Paginate through all results
                while True:
                    params = {
                        "category": "image-to-video",
                        "status": "active",
                        "limit": 50,
                    }
                    if cursor:
                        params["cursor"] = cursor

                    response = requests.get(
                        "https://api.fal.ai/v1/models",
                        params=params,
                        headers=headers,
                        timeout=15,
                    )

                    if response.status_code != 200:
                        # Log detail for debugging but don't expose to user (may contain sensitive info)
                        detail = response.text[:200] if response.text else ""
                        logger.debug(
                            "Fal API error %s: %s", response.status_code, detail
                        )
                        if response.status_code in (401, 403):
                            callback([], "fal.ai API key rejected. Update the key in settings.")
                        elif response.status_code == 429:
                            retry_after = response.headers.get("Retry-After", "").strip()
                            suffix = f" Wait about {retry_after}s and try again." if retry_after.isdigit() else ""
                            callback([], f"fal.ai rate limited model loading.{suffix}")
                        else:
                            callback([], f"fal.ai model loading failed (HTTP {response.status_code})")
                        return

                    data = response.json()
                    for m in data.get("models", []):
                        endpoint_id = m.get("endpoint_id", "")
                        metadata = m.get("metadata", {})
                        api_display_name = metadata.get("display_name", "")

                        # Smart display name: prefer endpoint-derived name when
                        # API name is missing, vague, or lacks version info
                        _vague = {"kling video", "pixverse", "wan effects", "longcat video", "pika"}
                        name_lower = api_display_name.strip().lower()
                        is_vague = any(v in name_lower for v in _vague)

                        has_version_in_name = bool(
                            re.search(r"\bv\d+(?:\.\d+)?", api_display_name, re.IGNORECASE)
                            or re.search(r"\b\d+\.\d+\b", api_display_name)
                        )
                        has_version_in_endpoint = bool(
                            re.search(r"/v\d+\.?\d*", endpoint_id)
                        )

                        if (
                            not api_display_name
                            or is_vague
                            or (has_version_in_endpoint and not has_version_in_name)
                        ):
                            display_name = _endpoint_to_short_name(endpoint_id)
                        else:
                            display_name = api_display_name

                        all_models.append(
                            {
                                "name": display_name,
                                "endpoint": endpoint_id,
                                "duration": metadata.get("duration_estimate", 10),
                                "description": metadata.get("description", "")[:100],
                            }
                        )

                    # Check for more pages
                    next_cursor = data.get("next_cursor")
                    if data.get("has_more") and next_cursor:
                        if next_cursor in seen_cursors:
                            logger.warning(
                                "Fal model pagination returned repeated cursor; stopping at %s",
                                next_cursor,
                            )
                            break
                        seen_cursors.add(next_cursor)
                        cursor = next_cursor
                    else:
                        break

                if all_models:
                    # Sort models alphabetically by display name
                    all_models.sort(key=lambda m: m["name"].lower())
                    callback(all_models, None)
                else:
                    callback([], "No models found")

            except Exception as e:
                callback([], str(e))

        thread = threading.Thread(target=_fetch, daemon=True)
        thread.start()

    @staticmethod
    def get_cached_or_fallback(config: dict) -> List[Dict]:
        """Get cached models or fallback list."""
        cached = config.get("cached_models", {})
        cached_list = cached.get("models", [])
        cached_time = cached.get("timestamp", 0)

        # If cache exists, use it (even if stale - background refresh will update)
        # Only fall back if no cached models at all
        if cached_list:
            return cached_list

        return FALLBACK_MODELS

    @staticmethod
    def get_merged_models(config: dict) -> List[Dict]:
        """Merge factory models (minus hidden) with custom models.

        Two hiding mechanisms, both honoured:
          * ``config["hidden_models"]`` — user-hidden endpoints.
          * per-model ``"hidden": true`` in models.json — endpoints kept
            out of normal selection by default (e.g. Seedance, an
            internal/experimental endpoint). Codex P2, PR #41: the flag
            was set but never read, so it still leaked into the dropdown.
        A model deliberately persisted as ``current_model`` is NOT
        filtered, so an explicit prior selection keeps working.
        """
        hidden = set(config.get("hidden_models", []))
        current = config.get("current_model", "")
        custom = config.get("custom_models", [])
        # A deliberately-persisted current_model is exempt from BOTH
        # hiding mechanisms — user-hiding (hidden_models) AND the
        # per-model "hidden": true flag. The previous order applied
        # hidden_models BEFORE the current-exempt check, so a model
        # the user explicitly hid would still drop out even if it was
        # the active selection (CodeRabbit, PR #41).
        factory = [
            m
            for m in MODEL_METADATA
            if (
                m.get("endpoint") == current
                or (
                    m.get("endpoint") not in hidden
                    and not m.get("hidden")
                )
            )
        ]
        merged = factory + list(custom)
        if merged:
            return merged
        return ModelFetcher.get_cached_or_fallback(config)

    @staticmethod
    def cache_models(config: dict, models: List[Dict]):
        """Save models to config cache."""
        config["cached_models"] = {"models": models, "timestamp": time.time()}


class ConfigPanel(tk.Frame):
    """Configuration panel for model, output, and prompt settings."""

    def __init__(
        self,
        parent,
        config: dict,
        on_config_changed: Callable[..., None],  # Flexible signature for compatibility
        build_prompt: bool = True,
        on_images_dropped: Optional[Callable[[List[str]], None]] = None,
        on_oldcam_rerun: Optional[Callable[[], None]] = None,
        on_oldcam_pick_rerun: Optional[Callable[[], None]] = None,
        **kwargs,
    ):
        """
        Initialize the config panel.

        Args:
            parent: Parent widget
            config: Current configuration dict
            on_config_changed: Callback when config changes
            build_prompt: If False, prompt panel is not built inline — caller
                must call build_prompt_panel(parent) separately after init.
            on_images_dropped: Callback when images are dropped/browsed in the
                mini drop zone (built inside the prompt panel).
            on_oldcam_rerun: Callback to rerun Oldcam-only processing on an
                existing generated video.
        """
        super().__init__(parent, bg=COLORS["bg_panel"], **kwargs)
        self.config = config
        self.on_config_changed = on_config_changed
        self._build_prompt_inline = build_prompt
        self._on_images_dropped = on_images_dropped
        self._on_oldcam_rerun = on_oldcam_rerun
        self._on_oldcam_pick_rerun = on_oldcam_pick_rerun

        # Configure dark theme for ttk Combobox widgets
        self._setup_combobox_style()

        # Configure dark theme for ttk Combobox widgets
        self._setup_combobox_style()

        self._setup_ui()
        self._load_config()

    def _setup_combobox_style(self):
        """Configure dark theme styling for ttk Combobox widgets."""
        style = ttk.Style(self)

        # Dark theme for all Combobox widgets in this panel
        style.configure(
            "Dark.TCombobox",
            fieldbackground=COLORS["bg_main"],
            background=COLORS["bg_input"],
            foreground=COLORS["text_light"],
            arrowcolor=COLORS["text_light"],
            borderwidth=0,
        )
        style.map(
            "Dark.TCombobox",
            fieldbackground=[
                ("readonly", COLORS["bg_main"]),
                ("disabled", COLORS["bg_panel"]),
            ],
            foreground=[
                ("readonly", COLORS["text_light"]),
                ("disabled", COLORS["text_dim"]),
            ],
            selectbackground=[("readonly", COLORS["accent_blue"])],
            selectforeground=[("readonly", "#FFFFFF")],
            arrowcolor=[
                ("disabled", COLORS["text_dim"]),
            ],
        )

        # Dropdown listbox colors are configured centrally in main_window.py.
        # Keep a local defensive fallback to avoid styling regressions if panel
        # is ever instantiated before the main window applies global options.
        try:
            root = self.winfo_toplevel()
            root.option_add("*TCombobox*Listbox.background", COLORS["bg_main"])
            root.option_add("*TCombobox*Listbox.foreground", COLORS["text_light"])
            root.option_add(
                "*TCombobox*Listbox.selectBackground", COLORS["accent_blue"]
            )
            root.option_add("*TCombobox*Listbox.selectForeground", "#FFFFFF")
        except tk.TclError:
            pass

    def _setup_ui(self):
        """Set up the configuration UI — two-column layout."""
        self._prompt_edit_mode = False

        # Main config frame (no CONFIGURATION heading — spec item 5)
        config_frame = tk.Frame(self, bg=COLORS["bg_input"], padx=10, pady=10)
        config_frame.pack(fill=tk.X, padx=10, pady=(8, 10))
        self._config_frame = config_frame

        # ── Two-column layout from the top: left (model/output/options) | right (prompt) ──
        body_frame = tk.Frame(config_frame, bg=COLORS["bg_input"])
        body_frame.pack(fill=tk.BOTH, expand=True)

        # ── LEFT COLUMN: model row, output row, separator, then option rows ──────────────
        left_col = tk.Frame(body_frame, bg=COLORS["bg_input"])
        left_col.pack(side=tk.LEFT, fill=tk.Y, padx=(0, 14), anchor="n")

        # Row 1: Model selection (in left_col so right column gets full panel height)
        row1 = tk.Frame(left_col, bg=COLORS["bg_input"])
        row1.pack(fill=tk.X, pady=(0, 10))

        tk.Label(
            row1, text="MODEL", font=(FONT_FAMILY, 10, "bold"),
            bg=COLORS["bg_input"], fg=COLORS["text_dim"], width=8, anchor="w",
        ).pack(side=tk.LEFT)

        # ⓘ info icon (larger, no text label) — hover to see model notes
        self.model_info_icon = tk.Label(
            row1, text="\u24D8", font=(FONT_FAMILY, 14),
            cursor="question_arrow",
            bg=COLORS["bg_input"], fg=COLORS["accent_blue"],
        )
        self.model_info_icon.pack(side=tk.RIGHT, padx=(6, 0))
        HoverTooltip(self.model_info_icon, self._get_current_model_notes)

        # "Manage…" button — opens the Model Manager dialog.
        # ttk under the clam theme so the dark fill survives macOS Aqua
        # HIView re-renders post-click (raw tk.Button reverts to
        # white-bezel-with-black-text after the first focus/click event).
        self.manage_models_btn = ttk.Button(
            row1, text="Manage\u2026", style=TTK_BTN_COMPACT,
            command=self._open_model_manager,
        )
        self.manage_models_btn.pack(side=tk.RIGHT, padx=(4, 0))

        self.model_var = tk.StringVar()
        self.model_combo = ttk.Combobox(
            row1, textvariable=self.model_var, state="readonly",
            font=(FONT_FAMILY, 10, "bold"),
            style="Dark.TCombobox", height=COMBOBOX_DROPDOWN_HEIGHT,
        )
        self.model_combo.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(5, 6))
        self.model_combo.bind("<<ComboboxSelected>>", self._on_model_changed)

        # Row 2: Output mode
        row2 = tk.Frame(left_col, bg=COLORS["bg_input"])
        row2.pack(fill=tk.X, pady=(0, 10))

        tk.Label(
            row2, text="OUTPUT", font=(FONT_FAMILY, 10, "bold"),
            bg=COLORS["bg_input"], fg=COLORS["text_dim"], width=8, anchor="w",
        ).pack(side=tk.LEFT)

        self.output_mode_var = tk.StringVar(value="source")
        self.source_radio = tk.Radiobutton(
            row2, text="Same as Source", variable=self.output_mode_var, value="source",
            font=(FONT_FAMILY, 10), bg=COLORS["bg_input"], fg=COLORS["text_light"],
            selectcolor=COLORS["bg_main"], activebackground=COLORS["bg_input"],
            activeforeground=COLORS["text_light"], command=self._on_output_mode_changed,
        )
        self.source_radio.pack(side=tk.LEFT, padx=(5, 10))

        self.custom_radio = tk.Radiobutton(
            row2, text="Custom Folder:", variable=self.output_mode_var, value="custom",
            font=(FONT_FAMILY, 10), bg=COLORS["bg_input"], fg=COLORS["text_light"],
            selectcolor=COLORS["bg_main"], activebackground=COLORS["bg_input"],
            activeforeground=COLORS["text_light"], command=self._on_output_mode_changed,
        )
        self.custom_radio.pack(side=tk.LEFT)

        self.output_path_var = tk.StringVar()
        self.output_entry = tk.Entry(
            row2, textvariable=self.output_path_var, font=(FONT_FAMILY, 10),
            bg=COLORS["bg_input"], fg=COLORS["text_light"],
            insertbackground=COLORS["text_light"],
            disabledbackground=COLORS["bg_input"], disabledforeground=COLORS["text_dim"],
            width=16, borderwidth=0, highlightthickness=1, highlightbackground=COLORS["border"],
        )
        self.output_entry.pack(side=tk.LEFT, padx=8, pady=2, fill=tk.Y)

        self.browse_btn = ttk.Button(
            row2, text="BROWSE", style=TTK_BTN_COMPACT,
            command=self._browse_output_folder,
        )
        self.browse_btn.pack(side=tk.LEFT, padx=2)

        # Separator (between model/output rows and option rows)
        ttk.Separator(left_col, orient=tk.HORIZONTAL).pack(fill=tk.X, pady=(2, 10))

        lbl_w = 10  # consistent label width in chars

        # Post-process band: [Options:] label | vertical stack of the
        # violet Oldcam frame (top) + orange rPPG frame (below), both in
        # the SAME parent with identical pack opts so they render EQUAL
        # width | one SHARED Re-Run column to the right of both frames.
        # Loop Video is NOT here — it moved to the reprocessing row.
        rA = tk.Frame(left_col, bg=COLORS["bg_input"])
        rA.pack(fill=tk.X, pady=(0, 4))
        tk.Label(rA, text="Options:", font=(FONT_FAMILY, 10),
                 bg=COLORS["bg_input"], fg=COLORS["text_light"],
                 width=lbl_w, anchor="nw").pack(side=tk.LEFT, anchor="n")
        # Loop widgets built now (keep attr names + callback) but packed
        # later into the reprocessing row (rB), not here.
        self.loop_video_var = tk.BooleanVar(value=False)
        # vertical stack: oldcam (top) over rPPG (below), equal width.
        # Packed WITHOUT fill/expand (constrained-resolution polish): the
        # stack shrink-wraps to its widest child instead of eating all of
        # rA's horizontal slack. That stops the tinted borders running far
        # right over empty space AND leaves the Re-Run column its natural
        # width so both its buttons render on narrow windows (Mac).
        _pp_stack = tk.Frame(rA, bg=COLORS["bg_input"])
        _pp_stack.pack(side=tk.LEFT, anchor="n", padx=(8, 0))
        self.oldcam_controls_frame = tk.Frame(
            _pp_stack,
            bg="#2A1F34",
            highlightthickness=1,
            highlightbackground="#5E3A7D",
            bd=0,
            padx=6,
            pady=2,
        )
        # fill=tk.X (no expand): stretches to the stack's width so the
        # oldcam + rPPG frames stay equal-width as a stacked pair, but the
        # stack width is now content-driven (see _pp_stack above), not the
        # whole row — so the border no longer trails empty purple space.
        self.oldcam_controls_frame.pack(fill=tk.X, pady=(0, 4))
        # "Oldcam: ⓘ" inline on top of the controls frame — top-anchored
        _oldcam_label_row = tk.Frame(self.oldcam_controls_frame, bg="#2A1F34")
        _oldcam_label_row.pack(side=tk.LEFT, anchor="n", padx=(0, 6))
        tk.Label(
            _oldcam_label_row,
            text="Oldcam:",
            font=(FONT_FAMILY, 10),
            bg="#2A1F34",
            fg=COLORS["text_light"],
        ).pack(side=tk.LEFT)
        self.oldcam_info_icon = tk.Label(
            _oldcam_label_row,
            text="ⓘ",
            font=(FONT_FAMILY, 11),
            cursor="question_arrow",
            bg="#2A1F34",
            fg=COLORS["accent_blue"],
        )
        self.oldcam_info_icon.pack(side=tk.LEFT, padx=(4, 0))
        HoverTooltip(self.oldcam_info_icon, self._get_oldcam_version_notes)
        self.oldcam_version_vars = {
            "v7": tk.BooleanVar(value=False),
            "v8": tk.BooleanVar(value=False),
            "v9": tk.BooleanVar(value=False),
            "v10": tk.BooleanVar(value=False),
            "v11": tk.BooleanVar(value=False),
            "v12": tk.BooleanVar(value=False),
            "v13": tk.BooleanVar(value=False),
            "v14": tk.BooleanVar(value=False),
            "v15": tk.BooleanVar(value=False),
            "v24": tk.BooleanVar(value=True),
        }
        # Row-major grid capped at 2 rows — fill left→right across a row,
        # then wrap; new versions overflow into additional columns to the
        # RIGHT (vertical space is scarce in this view; horizontal space to
        # the right is plentiful). 10 versions → 5 cols × 2 rows
        # (v7-v11 / v12-v24).
        _oldcam_versions = ("v7", "v8", "v9", "v10", "v11", "v12", "v13", "v14", "v15", "v24")
        _OLDCAM_ROWS = 2
        _OLDCAM_COLS = -(-len(_oldcam_versions) // _OLDCAM_ROWS)  # ceil division
        _check_grid = tk.Frame(self.oldcam_controls_frame, bg="#2A1F34")
        _check_grid.pack(side=tk.LEFT, anchor="n")
        self.oldcam_version_checks = {}
        for i, version in enumerate(_oldcam_versions):
            check = tk.Checkbutton(
                _check_grid,
                text=version,
                variable=self.oldcam_version_vars[version],
                font=(FONT_FAMILY, 9),
                bg="#2A1F34",
                fg=COLORS["text_light"],
                selectcolor=COLORS["bg_main"],
                activebackground="#2A1F34",
                activeforeground=COLORS["text_light"],
                command=self._on_oldcam_versions_changed,
            )
            check.grid(row=i // _OLDCAM_COLS, column=i % _OLDCAM_COLS, sticky="w", padx=(2, 8), pady=0)
            self.oldcam_version_checks[version] = check
            # No per-version hover. The (i) info icon next to "Oldcam
            # injection" surfaces the full per-version breakdown via
            # _get_oldcam_version_notes, so the per-checkbox hover was
            # redundant clutter.
        # (Re-Run controls are no longer inside the Oldcam frame — they
        # live in ONE shared column to the right of both tinted frames,
        # built after the rPPG frame below.)

        # rPPG injection — stacked directly UNDER the violet Oldcam
        # frame inside the SAME parent (_pp_stack) with identical pack
        # opts, so the two tinted frames render EQUAL width and read as
        # a clean stacked pair. rPPG runs LAST (Kling → Loop → Oldcam →
        # rPPG); combinable with Oldcam + Loop. Off by default. The
        # shared Re-Run buttons also apply rPPG when this is checked.
        self.rppg_controls_frame = tk.Frame(
            _pp_stack,
            bg="#3A2A1F",
            highlightthickness=1,
            highlightbackground="#7D5E3A",
            bd=0,
            padx=6,
            pady=2,
        )
        # fill=tk.X (no expand) — equal width with the oldcam frame above,
        # content-driven width (matches the stack). The controls inside are
        # now stacked VERTICALLY (rPPG: / inject / fanout) so this frame is
        # much narrower than the old single-line layout.
        self.rppg_controls_frame.pack(fill=tk.X)
        # Line 1: "rPPG:" + ⓘ — anchor="w" so it stacks above the checkboxes.
        _rppg_label_row = tk.Frame(self.rppg_controls_frame, bg="#3A2A1F")
        _rppg_label_row.pack(anchor="w")
        tk.Label(
            _rppg_label_row,
            text="rPPG:",
            font=(FONT_FAMILY, 10),
            bg="#3A2A1F",
            fg=COLORS["text_light"],
        ).pack(side=tk.LEFT)
        self.rppg_info_icon = tk.Label(
            _rppg_label_row,
            text="ⓘ",
            font=(FONT_FAMILY, 11),
            cursor="question_arrow",
            bg="#3A2A1F",
            fg=COLORS["accent_blue"],
        )
        self.rppg_info_icon.pack(side=tk.LEFT, padx=(4, 0))
        HoverTooltip(
            self.rppg_info_icon,
            lambda: (
                "Sub-perceptual rPPG pulse injection.\n"
                "\n"
                "Installs a faint, physiologically-correct pulse into the\n"
                "face so a passive-rPPG liveness check sees a real signal.\n"
                "\n"
                "Runs FIRST in the chain:\n"
                "Kling → rPPG → Loop → Crush → AA → Oldcam,\n"
                "so every later step builds on the rPPG'd base.\n"
                "\n"
                "Untested forward direction — off by default.\n"
                "\n"
                "Skips gracefully if the rPPG tool is missing or injection\n"
                "fails (keeps the pre-rPPG video; never crashes the run)."
            ),
        )
        # Line 2: "Inject rPPG pulse" checkbox + its inline desc, wrapped in
        # a row frame so the two sit together on one line BELOW the label.
        self.rppg_var = tk.BooleanVar(value=False)
        _rppg_inject_row = tk.Frame(self.rppg_controls_frame, bg="#3A2A1F")
        _rppg_inject_row.pack(anchor="w")
        self.rppg_checkbox = tk.Checkbutton(
            _rppg_inject_row,
            text="Inject rPPG pulse",
            variable=self.rppg_var,
            font=(FONT_FAMILY, 9),
            bg="#3A2A1F",
            fg=COLORS["text_light"],
            selectcolor=COLORS["bg_main"],
            activebackground="#3A2A1F",
            activeforeground=COLORS["text_light"],
            command=self._on_rppg_changed,
        )
        self.rppg_checkbox.pack(side=tk.LEFT, padx=(2, 8))
        tk.Label(
            _rppg_inject_row,
            text="(sub-perceptual · runs FIRST · default OFF)",
            font=(FONT_FAMILY, 9),
            bg="#3A2A1F",
            fg=COLORS["text_dim"],
        ).pack(side=tk.LEFT, padx=(0, 4))

        # Phase E of polish/v2.3 (2026-05-22): legacy per-Oldcam
        # fan-out as opt-in. Default OFF — when ON, rPPG ALSO runs
        # on each Oldcam output (slower but lets a careful workflow
        # get a fresh-pulse Oldcam variant). Sits on its own line
        # directly under the main rPPG inject checkbox (vertical
        # layout) so the relationship is visible.
        self.rppg_per_oldcam_fanout_var = tk.BooleanVar(value=False)
        self.rppg_per_oldcam_fanout_checkbox = tk.Checkbutton(
            self.rppg_controls_frame,
            text="Apply fresh rPPG to each Oldcam version (slower)",
            variable=self.rppg_per_oldcam_fanout_var,
            font=(FONT_FAMILY, 9),
            bg="#3A2A1F",
            fg=COLORS["text_dim"],
            selectcolor=COLORS["bg_main"],
            activebackground="#3A2A1F",
            activeforeground=COLORS["text_light"],
            command=self._on_rppg_per_oldcam_fanout_changed,
        )
        # Line 3: fanout checkbox on its own line under the inject row.
        self.rppg_per_oldcam_fanout_checkbox.pack(anchor="w", padx=(2, 0))

        # Quality-crush frame — dark red, stacked under rPPG (Phase E order:
        # Kling -> rPPG -> Loop -> Crush -> Oldcam). Mimics WhatsApp quality
        # destruction: 480p re-encode at CRF 35. DEFAULT OFF / opt-in only.
        _crush_bg = "#3A1F1F"
        _crush_border = "#7D3A3A"
        self.crush_controls_frame = tk.Frame(
            _pp_stack,
            bg=_crush_bg,
            highlightthickness=1,
            highlightbackground=_crush_border,
            padx=6,
            pady=2,
        )
        self.crush_controls_frame.pack(fill=tk.X, pady=(4, 0))
        _crush_label_row = tk.Frame(self.crush_controls_frame, bg=_crush_bg)
        _crush_label_row.pack(anchor="w")
        tk.Label(
            _crush_label_row,
            text="Crush:",
            bg=_crush_bg,
            fg=COLORS["text_light"],
            font=(FONT_FAMILY, 10),
        ).pack(side=tk.LEFT)
        self.crush_info_icon = tk.Label(
            _crush_label_row,
            text="ⓘ",
            font=(FONT_FAMILY, 11),
            cursor="question_arrow",
            bg=_crush_bg,
            fg=COLORS["accent_blue"],
        )
        self.crush_info_icon.pack(side=tk.LEFT, padx=(4, 0))
        HoverTooltip(
            self.crush_info_icon,
            lambda: (
                "Quality-crush re-encode.\n"
                "\n"
                "Re-compresses the video hard at 480p or 720p, low bitrate.\n"
                "\n"
                "Mimics the quality loss of a WhatsApp / social\n"
                "upload-and-redownload round-trip.\n"
                "\n"
                "That destruction can strip the clean spatial fingerprint\n"
                "that AI renders carry.\n"
                "\n"
                "Tick one or both tiers — each produces its own crushed file.\n"
                "720p is lighter; 480p is harsher.\n"
                "\n"
                "Needs FFmpeg; skips gracefully if it's missing."
            ),
        )
        tk.Label(
            _crush_label_row,
            text="  WhatsApp-style 480p/720p quality-crush re-encode",
            bg=_crush_bg,
            fg=COLORS["text_dim"],
            font=(FONT_FAMILY, 8),
        ).pack(side=tk.LEFT)
        # Selectable resolution tiers (2026-06-18) — checkboxes like the
        # Oldcam version list. 720p is the default (pre-checked on fresh
        # installs); 480p is the original harsher tier. Choosing both fans
        # out one crushed file per tier; choosing neither = crush OFF.
        from automation.video_crush import CRUSH_RESOLUTIONS as _CRUSH_RES
        _crush_row = tk.Frame(self.crush_controls_frame, bg=_crush_bg)
        _crush_row.pack(anchor="w", padx=(2, 0))
        self.crush_resolution_vars = {}
        self.crush_resolution_checks = {}
        # Highest-first so 720p reads left-to-right before 480p.
        for label in sorted(_CRUSH_RES, key=lambda lbl: _CRUSH_RES[lbl], reverse=True):
            var = tk.BooleanVar(value=False)
            self.crush_resolution_vars[label] = var
            check = tk.Checkbutton(
                _crush_row,
                text=label,
                variable=var,
                bg=_crush_bg,
                fg=COLORS["text_light"],
                selectcolor=_crush_bg,
                activebackground=_crush_bg,
                activeforeground=COLORS["text_light"],
                font=(FONT_FAMILY, 9),
                command=self._on_crush_resolutions_changed,
            )
            check.pack(side=tk.LEFT, padx=(0, 10))
            self.crush_resolution_checks[label] = check

        # AA (adversarial-attack) column — a NEW column in the band (rA) to the
        # RIGHT of the Oldcam/rPPG/Crush stack. Step 3 is vertically busy, so AA
        # goes HORIZONTALLY here (user direction 2026-06-18) and the shared
        # Re-Run buttons move BENEATH this column. Dark-green box (distinct from
        # oldcam purple / rPPG brown / crush red). Three attack-pipeline
        # checkboxes fan out like the crush tiers; Prime is the default ON.
        _aa_col = tk.Frame(rA, bg=COLORS["bg_input"])
        _aa_col.pack(side=tk.LEFT, anchor="n", padx=(12, 0))
        _aa_bg = "#1F3A1F"
        _aa_border = "#3A7D3A"
        self.aa_controls_frame = tk.Frame(
            _aa_col,
            bg=_aa_bg,
            highlightthickness=1,
            highlightbackground=_aa_border,
            padx=6,
            pady=2,
        )
        self.aa_controls_frame.pack(fill=tk.X)
        _aa_label_row = tk.Frame(self.aa_controls_frame, bg=_aa_bg)
        _aa_label_row.pack(anchor="w")
        tk.Label(
            _aa_label_row,
            text="AA:",
            bg=_aa_bg,
            fg=COLORS["text_light"],
            font=(FONT_FAMILY, 10),
        ).pack(side=tk.LEFT)
        self.aa_info_icon = tk.Label(
            _aa_label_row,
            text="ⓘ",
            font=(FONT_FAMILY, 11),
            cursor="question_arrow",
            bg=_aa_bg,
            fg=COLORS["accent_blue"],
        )
        self.aa_info_icon.pack(side=tk.LEFT, padx=(4, 0))
        # Inline description on the same row as "AA: ⓘ" to save vertical space.
        tk.Label(
            _aa_label_row,
            text="  adversarial detector-evasion re-encode",
            bg=_aa_bg,
            fg=COLORS["text_dim"],
            font=(FONT_FAMILY, 8),
        ).pack(side=tk.LEFT)
        HoverTooltip(
            self.aa_info_icon,
            lambda: (
                "Adversarial-attack re-encode (detector evasion).\n"
                "Adds perturbations engineered to fool AI-video detectors.\n"
                "Each ticked pipeline produces its own output file.\n"
                "Each output then fans through Oldcam (like the crush tiers).\n"
                "Phase E order: … → Crush → AA → Oldcam.\n"
                "\n"
                "  • Prime — pixel + temporal + trace + recompress\n"
                "      (targets generic AI-vs-real classifiers).\n"
                "  • Scenario1 — replay / pre-recorded evasion.\n"
                "  • Scenario3 — smoothing / puppeteering evasion.\n"
                "\n"
                "All three run on CPU (a GPU just makes them faster).\n"
                "Runs in an isolated venv, auto-provisioned on first use.\n"
                "Authorized detector-research use only.\n"
                "Off by default; skips gracefully if unavailable."
            ),
        )
        # Attack-pipeline checkboxes (fan-out). Order = prime, scenario1,
        # scenario3 (matching AA_PIPELINES display order). Prime pre-checked is
        # applied in apply_config from the saved aa_attacks; constructed unchecked.
        from automation.video_aa import AA_PIPELINES as _AA_PIPES
        # All three pipelines run on CPU (torch is bundled in the AA venv; the
        # scenario modules dispatch to their _cpu variants when no GPU is
        # present, and prime never needs torch). A GPU just makes them faster.
        _aa_labels = {
            "prime": "Prime",
            "scenario1": "Scenario1",
            "scenario3": "Scenario3",
        }
        _aa_row = tk.Frame(self.aa_controls_frame, bg=_aa_bg)
        _aa_row.pack(anchor="w", padx=(2, 0))
        self.aa_attack_vars = {}
        self.aa_attack_checks = {}
        for _key in ("prime", "scenario1", "scenario3"):
            if _key not in _AA_PIPES:
                continue
            var = tk.BooleanVar(value=False)
            self.aa_attack_vars[_key] = var
            check = tk.Checkbutton(
                _aa_row,
                text=_aa_labels.get(_key, _key),
                variable=var,
                bg=_aa_bg,
                fg=COLORS["text_light"],
                selectcolor=_aa_bg,
                activebackground=_aa_bg,
                activeforeground=COLORS["text_light"],
                font=(FONT_FAMILY, 9),
                command=self._on_aa_attacks_changed,
            )
            check.pack(side=tk.LEFT, padx=(0, 8))
            self.aa_attack_checks[_key] = check

        # Output-mode + pipeline preview (2026-06-19). Placed in the AA column
        # BENEATH the green AA box (and ABOVE the Re-Run column, which shifts
        # down) to conserve scarce vertical space in the left Oldcam/rPPG/Crush
        # stack (user direction 2026-06-19). Output mode picks how the enabled
        # modifiers combine: "Separate + combined" (powerset — every subset) vs
        # "Combined only" (one cumulative chain). The preview line shows the
        # resulting plan live, from the SAME shared planner the queue uses
        # (automation.postproc_plan) so it can never disagree.
        _mode_bg = "#1F2A3A"
        _mode_border = "#3A5E7D"
        self.fanout_controls_frame = tk.Frame(
            _aa_col,
            bg=_mode_bg,
            highlightthickness=1,
            highlightbackground=_mode_border,
            padx=6,
            pady=2,
        )
        self.fanout_controls_frame.pack(fill=tk.X, pady=(6, 0))
        _mode_row = tk.Frame(self.fanout_controls_frame, bg=_mode_bg)
        _mode_row.pack(anchor="w", fill=tk.X)
        tk.Label(
            _mode_row,
            text="Output mode:",
            bg=_mode_bg,
            fg=COLORS["text_light"],
            font=(FONT_FAMILY, 10),
        ).pack(side=tk.LEFT)
        # Initialize with the user-facing DISPLAY string (not the internal key)
        # so the combobox never shows a raw "separate_and_combined" if
        # apply_config hasn't run yet (gemini MEDIUM).
        self.fanout_mode_var = tk.StringVar(
            value=self._FANOUT_DISPLAY["separate_and_combined"]
        )
        self.fanout_mode_combo = ttk.Combobox(
            _mode_row,
            state="readonly",
            width=30,
            textvariable=self.fanout_mode_var,
            values=[
                "Separate + combined (powerset)",
                "Combined only (one chain)",
            ],
            font=(FONT_FAMILY, 9),
        )
        self.fanout_mode_combo.pack(side=tk.LEFT, padx=(6, 0))
        self.fanout_mode_combo.bind(
            "<<ComboboxSelected>>", lambda _e: self._on_fanout_mode_changed()
        )
        # Live read-only plan preview.
        self.pipeline_preview_label = tk.Label(
            self.fanout_controls_frame,
            text="",
            bg=_mode_bg,
            fg=COLORS["accent_blue"],
            font=(FONT_FAMILY, 8),
            justify=tk.LEFT,
            anchor="w",
            wraplength=320,
        )
        self.pipeline_preview_label.pack(anchor="w", fill=tk.X, pady=(2, 0))

        # ONE shared Re-Run column, now packed BENEATH the AA box (inside
        # _aa_col) per user direction 2026-06-18 (was a sibling column in rA).
        # Applies to whatever is selected: any Oldcam versions AND/OR rPPG /
        # Crush / AA, and re-loops first when Loop Video is on
        # (queue_manager.rerun_oldcam_only handles the full Phase E ordering).
        # Distinct GROOVE border (beveled, not the flat coloured highlight the
        # stage boxes use) so the Re-Run actions read as a different KIND of
        # control — they re-apply the stages above, they aren't one of them
        # (user direction 2026-06-19).
        _shared_rerun_col = tk.Frame(
            _aa_col,
            bg=COLORS["bg_input"],
            relief=tk.GROOVE,
            bd=2,
            padx=6,
            pady=4,
        )
        _shared_rerun_col.pack(anchor="w", fill=tk.X, pady=(6, 0))
        _rerun_label_row = tk.Frame(_shared_rerun_col, bg=COLORS["bg_input"])
        _rerun_label_row.pack(anchor="w")
        tk.Label(
            _rerun_label_row,
            text="Re-Run:",
            font=(FONT_FAMILY, 10),
            bg=COLORS["bg_input"],
            fg=COLORS["text_light"],
        ).pack(side=tk.LEFT)
        self.rerun_info_icon = tk.Label(
            _rerun_label_row,
            text="ⓘ",
            font=(FONT_FAMILY, 11),
            cursor="question_arrow",
            bg=COLORS["bg_input"],
            fg=COLORS["accent_blue"],
        )
        self.rerun_info_icon.pack(side=tk.LEFT, padx=(4, 0))
        HoverTooltip(
            self.rerun_info_icon,
            lambda: (
                "Re-run post-processing — no new Kling generation.\n"
                "\n"
                "Both buttons re-apply WHATEVER is ticked above\n"
                "(rPPG / Loop / Crush / AA / Oldcam), in pipeline order,\n"
                "to an EXISTING video.\n"
                "\n"
                "So you can try different post-processing combos without\n"
                "paying to re-generate from Kling.\n"
                "\n"
                "↻ Active — runs on the video currently shown in the\n"
                "    carousel (or the latest completed one).\n"
                "📂 File… — opens a picker to run on ANY video on disk,\n"
                "    even one not made by this app.\n"
                "\n"
                "Respects Allow reprocessing / Increment."
            ),
        )
        _shared_rerun_btn_row = tk.Frame(_shared_rerun_col, bg=COLORS["bg_input"])
        _shared_rerun_btn_row.pack(anchor="w", pady=(2, 0))
        # Two distinct re-run buttons (labelled so the difference is obvious
        # at a glance, not an enigma):
        #   ↻ Active  — re-run on the video already in the carousel
        #   📂 File   — pick ANY video on disk and re-run on it
        # Both apply WHATEVER post-processes are ticked above (rPPG / Loop /
        # Crush / AA / Oldcam, in Phase E order) WITHOUT a new Kling generation.
        self.oldcam_rerun_btn = ttk.Button(
            _shared_rerun_btn_row,
            text="↻ Active",
            style=TTK_BTN_COMPACT,
            width=8,
            command=self._on_oldcam_rerun_clicked,
        )
        self.oldcam_rerun_btn.pack(side=tk.LEFT, padx=(0, 4))
        HoverTooltip(
            self.oldcam_rerun_btn,
            lambda: (
                "↻  Re-run on the ACTIVE video\n"
                "\n"
                "Re-applies the ticked post-processing\n"
                "(rPPG / Loop / Crush / AA / Oldcam) to the video\n"
                "currently selected in the carousel — or the latest\n"
                "completed one if none is selected.\n"
                "\n"
                "NO new Kling generation: it reuses the existing\n"
                "Kling output, so you can try different post-processing\n"
                "combos without paying to re-generate.\n"
                "\n"
                "Needs a generated video already present."
            ),
        )
        self.oldcam_pick_btn = ttk.Button(
            _shared_rerun_btn_row,
            text="📂 File…",
            style=TTK_BTN_COMPACT,
            width=8,
            command=self._on_oldcam_pick_rerun_clicked,
        )
        self.oldcam_pick_btn.pack(side=tk.LEFT, padx=(0, 0))
        HoverTooltip(
            self.oldcam_pick_btn,
            lambda: (
                "📂  Re-run on ANY file from disk\n"
                "\n"
                "Opens a file picker — choose any video on your drive\n"
                "and run any combination of post-processing on it\n"
                "manually (rPPG / Loop / Crush / AA / any Oldcam\n"
                "versions, in Phase E order).\n"
                "\n"
                "The video doesn't need to come from this app —\n"
                "drop in any clip. This is the manual workhorse for\n"
                "post-processing existing footage.\n"
                "\n"
                "Respects Allow reprocessing / Increment."
            ),
        )

        # NOTE: The face-track gate GUI controls were removed (2026-05-19).
        # A large balanced corpus (21 PASS / 23 FAIL, all Kling-from-real-
        # selfie) showed face-track % does NOT separate Persona PASS from
        # FAIL — see docs/analysis/versailles_fail_vs_pass.md "DEFINITIVE
        # LARGE-CORPUS NEGATIVE". The pipeline keys still exist but default
        # OFF (automation/config.py); the check is an opt-in diagnostic,
        # not a GUI-promoted quality gate, so it is not surfaced here.

        # Allow reprocessing
        rB = tk.Frame(left_col, bg=COLORS["bg_input"])
        rB.pack(fill=tk.X, pady=_ROW_PADY)
        tk.Label(rB, text="", font=(FONT_FAMILY, 10),
                 bg=COLORS["bg_input"], width=lbl_w).pack(side=tk.LEFT)
        self.reprocess_var = tk.BooleanVar(value=False)
        self.reprocess_checkbox = tk.Checkbutton(
            rB, text="Allow reprocessing", variable=self.reprocess_var,
            font=(FONT_FAMILY, 10), bg=COLORS["bg_input"], fg=COLORS["text_light"],
            selectcolor=COLORS["bg_main"], activebackground=COLORS["bg_input"],
            activeforeground=COLORS["text_light"], command=self._on_reprocess_changed,
        )
        self.reprocess_checkbox.pack(side=tk.LEFT)
        self.reprocess_mode_frame = tk.Frame(rB, bg=COLORS["bg_input"])
        self.reprocess_mode_frame.pack(side=tk.LEFT, padx=(8, 0))
        self.reprocess_mode_var = tk.StringVar(value="increment")
        self.overwrite_radio = tk.Radiobutton(
            self.reprocess_mode_frame, text="Overwrite",
            variable=self.reprocess_mode_var, value="overwrite",
            font=(FONT_FAMILY, 9), bg=COLORS["bg_input"], fg=COLORS["text_light"],
            selectcolor=COLORS["bg_main"], activebackground=COLORS["bg_input"],
            activeforeground=COLORS["text_light"], command=self._on_reprocess_mode_changed,
        )
        self.overwrite_radio.pack(side=tk.LEFT, padx=2)
        self.increment_radio = tk.Radiobutton(
            self.reprocess_mode_frame, text="Increment (_2, _3\u2026)",
            variable=self.reprocess_mode_var, value="increment",
            font=(FONT_FAMILY, 9), bg=COLORS["bg_input"], fg=COLORS["text_light"],
            selectcolor=COLORS["bg_main"], activebackground=COLORS["bg_input"],
            activeforeground=COLORS["text_light"], command=self._on_reprocess_mode_changed,
        )
        self.increment_radio.pack(side=tk.LEFT, padx=2)
        self._update_reprocess_mode_visibility()
        # Loop Video moved here (was on the old Options row) — inline
        # right after "Increment (_2, _3…)". Attr names + callback
        # unchanged; only the parent/placement moved.
        self.loop_checkbox = tk.Checkbutton(
            rB, text="Loop Video (ping-pong)", variable=self.loop_video_var,
            font=(FONT_FAMILY, 10), bg=COLORS["bg_input"], fg=COLORS["text_light"],
            selectcolor=COLORS["bg_main"], activebackground=COLORS["bg_input"],
            activeforeground=COLORS["text_light"], command=self._on_loop_changed,
        )
        self.loop_checkbox.pack(side=tk.LEFT, padx=(16, 0))
        self.loop_info_label = tk.Label(
            rB, text="(requires FFmpeg)", font=(FONT_FAMILY, 9),
            bg=COLORS["bg_input"], fg=COLORS["text_dim"],
        )
        self.loop_info_label.pack(side=tk.LEFT, padx=4)

        # Logging
        rC = tk.Frame(left_col, bg=COLORS["bg_input"])
        rC.pack(fill=tk.X, pady=_ROW_PADY)
        tk.Label(rC, text="Logging:", font=(FONT_FAMILY, 10),
                 bg=COLORS["bg_input"], fg=COLORS["text_light"],
                 width=lbl_w, anchor="w").pack(side=tk.LEFT)
        self.verbose_gui_var = tk.BooleanVar(value=False)
        self.verbose_checkbox = tk.Checkbutton(
            rC, text="Verbose Mode", variable=self.verbose_gui_var,
            font=(FONT_FAMILY, 10), bg=COLORS["bg_input"], fg=COLORS["text_light"],
            selectcolor=COLORS["bg_main"], activebackground=COLORS["bg_input"],
            activeforeground=COLORS["text_light"], command=self._on_verbose_changed,
        )
        self.verbose_checkbox.pack(side=tk.LEFT)
        self.verbose_info_label = tk.Label(
            rC, text="(detailed processing log)", font=(FONT_FAMILY, 9),
            bg=COLORS["bg_input"], fg=COLORS["text_dim"],
        )
        self.verbose_info_label.pack(side=tk.LEFT, padx=4)

        # rPPG metric-suffix toggle (shares the Logging: row). When OFF
        # (default) the injector's "{stem}-rppg - <SNR>-<Phase>-..." name
        # is stripped to a clean "{stem}-rppg" and the metrics go to a
        # .metrics.json sidecar (automation/rppg.finalize_rppg_output).
        # rPPG metrics-in-filename toggle — HIDDEN (user request 2026-06-29:
        # unused, reclaims horizontal room on the Logging row). The var +
        # widgets are still CREATED (just never packed) so config save/load and
        # the injector-naming code that reads rppg_metrics_var keep working.
        self.rppg_metrics_var = tk.BooleanVar(value=False)
        self.rppg_metrics_checkbox = tk.Checkbutton(
            rC, text="rPPG metrics in filename", variable=self.rppg_metrics_var,
            font=(FONT_FAMILY, 10), bg=COLORS["bg_input"], fg=COLORS["text_light"],
            selectcolor=COLORS["bg_main"], activebackground=COLORS["bg_input"],
            activeforeground=COLORS["text_light"],
            command=self._on_rppg_metrics_changed,
        )
        # NOTE: rppg_metrics_checkbox + info label intentionally NOT packed.
        self.rppg_metrics_info_label = tk.Label(
            rC, text="(off = clean name + .metrics.json sidecar)",
            font=(FONT_FAMILY, 9),
            bg=COLORS["bg_input"], fg=COLORS["text_dim"],
        )

        # File Filter — HIDDEN (user request 2026-06-29: unused, reclaims a
        # full row of vertical space on Step 3). The widgets + vars are still
        # CREATED (just never packed) so config save/load and any code that
        # reads folder_pattern_var / folder_match_mode_var keep working.
        rD = tk.Frame(left_col, bg=COLORS["bg_input"])
        # NOTE: rD is intentionally NOT packed.
        tk.Label(rD, text="Filter:", font=(FONT_FAMILY, 10),
                 bg=COLORS["bg_input"], fg=COLORS["text_light"],
                 width=lbl_w, anchor="w").pack(side=tk.LEFT)
        self.folder_pattern_var = tk.StringVar(value="")
        self.folder_pattern_entry = tk.Entry(
            rD, textvariable=self.folder_pattern_var, font=(FONT_FAMILY, 10),
            bg=COLORS["bg_main"], fg=COLORS["text_light"],
            insertbackground=COLORS["text_light"], width=16,
            borderwidth=0, highlightthickness=1, highlightbackground=COLORS["border"],
        )
        self.folder_pattern_entry.pack(side=tk.LEFT, padx=(0, 8))
        self.folder_pattern_entry.bind("<FocusOut>", self._on_folder_pattern_changed)
        self.folder_pattern_entry.bind("<Return>", self._on_folder_pattern_changed)
        tk.Label(rD, text="Match:", font=(FONT_FAMILY, 9),
                 bg=COLORS["bg_input"], fg=COLORS["text_dim"]).pack(side=tk.LEFT, padx=(0, 4))
        self.folder_match_mode_var = tk.StringVar(value="partial")
        self.partial_radio = tk.Radiobutton(
            rD, text="Partial", variable=self.folder_match_mode_var, value="partial",
            font=(FONT_FAMILY, 9), bg=COLORS["bg_input"], fg=COLORS["text_light"],
            selectcolor=COLORS["bg_main"], activebackground=COLORS["bg_input"],
            activeforeground=COLORS["text_light"], command=self._on_folder_match_mode_changed,
        )
        self.partial_radio.pack(side=tk.LEFT, padx=2)
        self.exact_radio = tk.Radiobutton(
            rD, text="Exact", variable=self.folder_match_mode_var, value="exact",
            font=(FONT_FAMILY, 9), bg=COLORS["bg_input"], fg=COLORS["text_light"],
            selectcolor=COLORS["bg_main"], activebackground=COLORS["bg_input"],
            activeforeground=COLORS["text_light"], command=self._on_folder_match_mode_changed,
        )
        self.exact_radio.pack(side=tk.LEFT, padx=2)
        # Filter help moved into an ⓘ hover (same pattern
        # as the oldcam / rPPG descriptors) placed inline at
        # the end of the row — reclaims the vertical space the
        # old multi-line help label consumed (user request,
        # PR #41).
        self.filter_info_icon = tk.Label(
            rD, text="ⓘ", font=(FONT_FAMILY, 11),
            cursor="question_arrow",
            bg=COLORS["bg_input"], fg=COLORS["accent_blue"],
        )
        self.filter_info_icon.pack(side=tk.LEFT, padx=(6, 0))
        HoverTooltip(
            self.filter_info_icon,
            lambda: (
                "Subfolder name to match when processing a "
                "folder\n(blank → all files)\n\n"
                "Partial: filename contains the filter string"
                "\nExact:  filename equals the filter string "
                "exactly"
            ),
        )

        # Video settings
        rE = tk.Frame(left_col, bg=COLORS["bg_input"])
        rE.pack(fill=tk.X, pady=_ROW_PADY)
        tk.Label(rE, text="Video:", font=(FONT_FAMILY, 10),
                 bg=COLORS["bg_input"], fg=COLORS["text_light"],
                 width=lbl_w, anchor="w").pack(side=tk.LEFT)
        tk.Label(rE, text="Aspect:", font=(FONT_FAMILY, 9),
                 bg=COLORS["bg_input"], fg=COLORS["text_dim"]).pack(side=tk.LEFT, padx=(0, 3))
        self.aspect_ratio_var = tk.StringVar(value="9:16")
        self.aspect_ratio_combo = ttk.Combobox(
            rE, textvariable=self.aspect_ratio_var,
            values=["21:9", "16:9", "4:3", "1:1", "3:4", "9:16"],
            state="readonly", width=6, font=(FONT_FAMILY, 9), style="Dark.TCombobox",
        )
        self.aspect_ratio_combo.pack(side=tk.LEFT, padx=(0, 12))
        self.aspect_ratio_combo.bind("<<ComboboxSelected>>", self._on_aspect_ratio_changed)
        tk.Label(rE, text="Duration:", font=(FONT_FAMILY, 9),
                 bg=COLORS["bg_input"], fg=COLORS["text_dim"]).pack(side=tk.LEFT, padx=(0, 3))
        self.duration_var = tk.StringVar(value="10s")
        self.duration_combo = ttk.Combobox(
            rE, textvariable=self.duration_var, values=["5s", "10s"],
            state="readonly", width=5, font=(FONT_FAMILY, 9), style="Dark.TCombobox",
        )
        self.duration_combo.pack(side=tk.LEFT, padx=(0, 12))
        self.duration_combo.bind("<<ComboboxSelected>>", self._on_duration_changed)
        tk.Label(rE, text="Res:", font=(FONT_FAMILY, 9),
                 bg=COLORS["bg_input"], fg=COLORS["text_dim"]).pack(side=tk.LEFT, padx=(0, 3))
        self.resolution_var = tk.StringVar(value="720p")
        self.resolution_combo = ttk.Combobox(
            rE, textvariable=self.resolution_var, values=["480p", "720p"],
            state="readonly", width=5, font=(FONT_FAMILY, 9), style="Dark.TCombobox",
        )
        self.resolution_combo.pack(side=tk.LEFT, padx=(0, 8))
        self.resolution_combo.bind("<<ComboboxSelected>>", self._on_resolution_changed)
        # Live token-cost estimate for the current model+resolution+duration
        # (token-priced models only, e.g. Seedance — empty for flat-priced ones).
        self.cost_estimate_label = tk.Label(
            rE, text="", font=(FONT_FAMILY, 9), bg=COLORS["bg_input"], fg=COLORS["accent_blue"],
        )
        self.cost_estimate_label.pack(side=tk.LEFT, padx=(0, 8))
        self.schema_diagnostic_label = tk.Label(
            rE, text="", font=(FONT_FAMILY, 9), bg=COLORS["bg_input"], fg=COLORS["text_dim"],
        )
        self.schema_diagnostic_label.pack(side=tk.LEFT, padx=2)
        # Blue ⓘ that decodes the cryptic capability indicators on this row +
        # the Motion row (user request 2026-06-29 — the ✓dur|✗cam / negative
        # · end-frame · cfg shorthand is hard to read at a glance).
        self.caps_info_icon = tk.Label(
            rE, text="ⓘ", font=(FONT_FAMILY, 11), cursor="question_arrow",
            bg=COLORS["bg_input"], fg=COLORS["accent_blue"],
        )
        self.caps_info_icon.pack(side=tk.LEFT, padx=(4, 0))
        HoverTooltip(self.caps_info_icon, lambda: (
            "Model capability indicators — ✓ = the currently selected video "
            "model supports this parameter, ✗ = it does not (the control is "
            "greyed out when unsupported).\n\n"
            "Video row:\n"
            "  dur = clip duration (5s / 10s)\n"
            "  asp = aspect ratio (9:16, 16:9, …)\n"
            "  res = output resolution (480p / 720p)\n"
            "  see = seed (reproducible randomness)\n"
            "  cam = camera-fixed (lock the camera so only the subject moves)\n\n"
            "Motion row:\n"
            "  negative  = honors a negative prompt (things to avoid)\n"
            "  end-frame = can lock the last frame to the start image\n"
            "  cfg       = CFG scale (prompt-adherence strength) is adjustable"
        ))
        self.video_settings_info = tk.Label(
            rE, text="(model-dependent)", font=(FONT_FAMILY, 9),
            bg=COLORS["bg_input"], fg=COLORS["text_dim"],
        )
        self.video_settings_info.pack(side=tk.LEFT, padx=2)

        # Motion control: end-frame lock + cfg_scale. Capability is the
        # single source of truth (model_metadata.get_model_capabilities —
        # the dispatcher + queue_manager read the SAME flags, so UI and
        # payload never disagree). Per the user's chosen UX: the
        # end-frame checkbox is ALWAYS visible but GRAYED OUT
        # (state=disabled) for models that don't expose an end-frame
        # param, and toggle-checkable for those that do (e.g. Kling 2.5
        # Pro). When locked the SAME image is used for both start and
        # end. Widgets are created ONCE; only their `state` changes —
        # never destroyed/recreated — so values survive model switches.
        rEF = tk.Frame(left_col, bg=COLORS["bg_input"])
        rEF.pack(fill=tk.X, pady=_ROW_PADY)
        self._motion_row = rEF
        tk.Label(rEF, text="Motion:", font=(FONT_FAMILY, 10),
                 bg=COLORS["bg_input"], fg=COLORS["text_light"],
                 width=lbl_w, anchor="w").pack(side=tk.LEFT)
        self.lock_end_frame_var = tk.BooleanVar(value=True)
        self.lock_end_frame_checkbox = tk.Checkbutton(
            rEF, text="Lock End Frame to Start Image",
            variable=self.lock_end_frame_var, font=(FONT_FAMILY, 9),
            bg=COLORS["bg_input"], fg=COLORS["text_light"],
            selectcolor=COLORS["bg_main"], activebackground=COLORS["bg_input"],
            activeforeground=COLORS["text_light"],
            disabledforeground=COLORS["text_dim"],
            command=self._on_lock_end_frame_changed,
        )
        self.lock_end_frame_checkbox.pack(side=tk.LEFT, padx=(0, 12))
        HoverTooltip(self.lock_end_frame_checkbox, lambda: (
            "Lock End Frame — force the video to end on (or very near) "
            "the start image. Pairs with the ping-pong Loop step:\n\n"
            "ON (default): start = end natively, so the clip plays "
            "forward and STOPS cleanly at the source. Looping is "
            "NOT needed (and adds nothing — the forward clip alone "
            "already returns to source).\n"
            "OFF: model decides the end freely (better motion realism). "
            "Use the Loop step to seamlessly play forward + reverse, "
            "which hides any start-to-end mismatch via ping-pong."
        ))
        self.cfg_scale_label = tk.Label(
            rEF, text="cfg:", font=(FONT_FAMILY, 9),
            bg=COLORS["bg_input"], fg=COLORS["text_dim"],
        )
        self.cfg_scale_label.pack(side=tk.LEFT, padx=(0, 3))
        _cfg_tip = lambda: (
            "CFG (Classifier-Free Guidance) — how strictly the model "
            "follows the prompt vs. exercises its own \"taste\".\n\n"
            "0.0 = loose, model improvises (best for vague prompts)\n"
            "0.5 = fal.ai default (balanced)\n"
            "0.7 = recommended for our pipeline (sticks to head-turn\n"
            "      prompt closely, low surprise)\n"
            "1.0 = max adherence, can over-tighten\n\n"
            "If subjects drift off-prompt: raise. If outputs feel "
            "robotic: lower."
        )
        HoverTooltip(self.cfg_scale_label, _cfg_tip)
        self.cfg_scale_var = tk.StringVar(value="0.7")
        self.cfg_scale_entry = tk.Entry(
            rEF, textvariable=self.cfg_scale_var, font=(FONT_FAMILY, 10),
            bg=COLORS["bg_main"], fg=COLORS["text_light"],
            insertbackground=COLORS["text_light"], width=5,
            borderwidth=0, highlightthickness=1,
            highlightbackground=COLORS["border"],
            disabledbackground=COLORS["bg_input"],
            disabledforeground=COLORS["text_dim"],
        )
        self.cfg_scale_entry.pack(side=tk.LEFT, padx=(0, 8))
        self.cfg_scale_entry.bind("<FocusOut>", self._on_cfg_scale_changed)
        self.cfg_scale_entry.bind("<Return>", self._on_cfg_scale_changed)
        self.model_caps_label = tk.Label(
            rEF, text="", font=(FONT_FAMILY, 9),
            bg=COLORS["bg_input"], fg=COLORS["text_dim"],
        )
        self.model_caps_label.pack(side=tk.RIGHT, padx=4)

        # Seed & misc options
        rF = tk.Frame(left_col, bg=COLORS["bg_input"])
        rF.pack(fill=tk.X, pady=_ROW_PADY)
        tk.Label(rF, text="Options:", font=(FONT_FAMILY, 10),
                 bg=COLORS["bg_input"], fg=COLORS["text_light"],
                 width=lbl_w, anchor="w").pack(side=tk.LEFT)
        tk.Label(rF, text="Seed:", font=(FONT_FAMILY, 9),
                 bg=COLORS["bg_input"], fg=COLORS["text_dim"]).pack(side=tk.LEFT, padx=(0, 3))
        self.seed_var = tk.StringVar(value="-1")
        self.seed_entry = tk.Entry(
            rF, textvariable=self.seed_var, font=(FONT_FAMILY, 10),
            bg=COLORS["bg_main"], fg=COLORS["text_light"],
            insertbackground=COLORS["text_light"], width=10,
            borderwidth=0, highlightthickness=1, highlightbackground=COLORS["border"],
        )
        self.seed_entry.pack(side=tk.LEFT, padx=(0, 3))
        self.seed_entry.bind("<FocusOut>", self._on_seed_changed)
        self.seed_entry.bind("<Return>", self._on_seed_changed)
        self.random_seed_var = tk.BooleanVar(value=True)
        self.random_seed_checkbox = tk.Checkbutton(
            rF, text="Random", variable=self.random_seed_var, font=(FONT_FAMILY, 9),
            bg=COLORS["bg_input"], fg=COLORS["text_light"], selectcolor=COLORS["bg_main"],
            activebackground=COLORS["bg_input"], activeforeground=COLORS["text_light"],
            command=self._on_random_seed_changed,
        )
        self.random_seed_checkbox.pack(side=tk.LEFT, padx=(0, 12))
        self.camera_fixed_var = tk.BooleanVar(value=False)
        self.camera_fixed_checkbox = tk.Checkbutton(
            rF, text="Camera Fixed", variable=self.camera_fixed_var, font=(FONT_FAMILY, 9),
            bg=COLORS["bg_input"], fg=COLORS["text_light"], selectcolor=COLORS["bg_main"],
            activebackground=COLORS["bg_input"], activeforeground=COLORS["text_light"],
            command=self._on_camera_fixed_changed,
        )
        self.camera_fixed_checkbox.pack(side=tk.LEFT, padx=(0, 12))
        self.generate_audio_var = tk.BooleanVar(value=False)
        self.generate_audio_checkbox = tk.Checkbutton(
            rF, text="Generate Audio", variable=self.generate_audio_var, font=(FONT_FAMILY, 9),
            bg=COLORS["bg_input"], fg=COLORS["text_light"], selectcolor=COLORS["bg_main"],
            activebackground=COLORS["bg_input"], activeforeground=COLORS["text_light"],
            command=self._on_generate_audio_changed,
        )
        self.generate_audio_checkbox.pack(side=tk.LEFT)
        self._update_seed_entry_state()

        # ── RIGHT COLUMN: built inline or externally via build_prompt_panel() ──
        if self._build_prompt_inline:
            self.build_prompt_panel(body_frame)

    def build_prompt_panel(self, parent: tk.Widget) -> tk.Frame:
        """Build the prompt editor UI into `parent`.

        Called automatically during _setup_ui (default) when build_prompt=True,
        or externally by main_window.py for side-by-side drop zone + prompt layout.
        """
        right_col = tk.Frame(
            parent, bg=COLORS["bg_panel"],
            highlightthickness=1, highlightbackground=COLORS["border"],
        )
        right_col.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        self.prompt_preview_container = right_col  # compatibility reference

        right_inner = tk.Frame(right_col, bg=COLORS["bg_panel"], padx=8, pady=6)
        right_inner.pack(fill=tk.BOTH, expand=True)

        # Slot selector bar — 1 through 10 in a single inline row
        slot_bar = tk.Frame(right_inner, bg=COLORS["bg_panel"])
        slot_bar.pack(fill=tk.X, pady=(0, 8))
        tk.Label(slot_bar, text="SLOT", font=(FONT_FAMILY, 9, "bold"),
                 bg=COLORS["bg_panel"], fg=COLORS["text_dim"]).pack(side=tk.LEFT, padx=(0, 8))
        self.slot_var = tk.IntVar(value=1)
        self._slot_buttons = []
        for i in range(1, 11):
            rb = tk.Radiobutton(
                slot_bar, text=str(i), variable=self.slot_var, value=i,
                font=(FONT_FAMILY, 10, "bold"),
                bg=COLORS["bg_panel"], fg=COLORS["text_light"],
                selectcolor=COLORS["accent_blue"],
                activebackground=COLORS["bg_panel"], activeforeground=COLORS["accent_blue"],
                indicatoron=False, width=2, relief=tk.FLAT,
                command=self._on_slot_changed,
            )
            rb.pack(side=tk.LEFT, padx=1)
            self._slot_buttons.append(rb)

        # Title row (read-only by default; Edit mode enables it)
        title_row = tk.Frame(right_inner, bg=COLORS["bg_panel"])
        title_row.pack(fill=tk.X, pady=(0, 6))
        tk.Label(title_row, text="Title:", font=(FONT_FAMILY, 10, "bold"),
                 bg=COLORS["bg_panel"], fg=COLORS["text_dim"]).pack(side=tk.LEFT)
        self.prompt_title_var = tk.StringVar()
        self.prompt_title_entry = tk.Entry(
            title_row, textvariable=self.prompt_title_var,
            font=(FONT_FAMILY, 10), bg=COLORS["bg_main"], fg=COLORS["text_light"],
            insertbackground=COLORS["text_light"], relief=tk.FLAT,
            highlightthickness=1, highlightbackground=COLORS["border"],
            state="disabled", disabledforeground=COLORS["text_dim"],
            disabledbackground=COLORS["bg_main"],
        )
        self.prompt_title_entry.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(8, 0))

        # Prompt text area — scrollbar packed first so it claims its rightmost space
        text_frame = tk.Frame(right_inner, bg=COLORS["bg_panel"])
        text_frame.pack(fill=tk.BOTH, expand=True)
        self.prompt_preview = tk.Text(
            text_frame, font=(FONT_FAMILY, 10),
            bg=COLORS["bg_main"], fg=COLORS["text_dim"],
            height=12, wrap=tk.WORD, relief=tk.FLAT, borderwidth=0,
            insertbackground=COLORS["text_light"], state="disabled",
        )
        preview_scroll = ttk.Scrollbar(text_frame, orient=tk.VERTICAL, command=self.prompt_preview.yview)
        self.prompt_preview.configure(yscrollcommand=preview_scroll.set)
        preview_scroll.pack(side=tk.RIGHT, fill=tk.Y)  # scrollbar first
        self.prompt_preview.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        self.prompt_preview.bind("<KeyRelease>", self._update_prompt_char_count)

        # Negative-prompt half (split-box UX, user 2026-05-19). The
        # editor splits horizontally: the box above is the POSITIVE
        # prompt; this section is the NEGATIVE prompt. It is shown only
        # for models whose schema accepts negative_prompt (e.g. Kling
        # 2.5 / v3) and pack_forget()-hidden for ones that dropped it
        # (o3 / seedance) — created ONCE so its text survives toggling.
        # Backed by config["negative_prompts"][slot] (the same dict
        # queue_manager._get_current_negative_prompt reads), so the
        # split editor and the submitted payload stay in lock-step.
        self._negative_prompt_section = tk.Frame(
            right_inner, bg=COLORS["bg_panel"]
        )
        tk.Label(
            self._negative_prompt_section, text="Negative prompt",
            font=(FONT_FAMILY, 9, "bold"),
            bg=COLORS["bg_panel"], fg=COLORS["text_dim"], anchor="w",
        ).pack(fill=tk.X, pady=(0, 2))
        neg_text_frame = tk.Frame(
            self._negative_prompt_section, bg=COLORS["bg_panel"]
        )
        neg_text_frame.pack(fill=tk.BOTH, expand=True)
        # NOTE: tk.Text does NOT support disabledforeground /
        # disabledbackground (those are Entry/Button options) — passing
        # them raises TclError and crashes panel construction. Mirror the
        # working positive prompt_preview: state="disabled" + the
        # edit-mode methods toggle fg via .config() for the dim effect.
        self.negative_prompt_preview = tk.Text(
            neg_text_frame, font=(FONT_FAMILY, 10),
            bg=COLORS["bg_main"], fg=COLORS["text_dim"],
            height=5, wrap=tk.WORD, relief=tk.FLAT, borderwidth=0,
            insertbackground=COLORS["text_light"], state="disabled",
        )
        neg_scroll = ttk.Scrollbar(
            neg_text_frame, orient=tk.VERTICAL,
            command=self.negative_prompt_preview.yview,
        )
        self.negative_prompt_preview.configure(yscrollcommand=neg_scroll.set)
        neg_scroll.pack(side=tk.RIGHT, fill=tk.Y)
        self.negative_prompt_preview.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        # Positive-box height is managed DYNAMICALLY by
        # _apply_negative_prompt_visibility (7 when the negative half is
        # shown so the split reads as ~two halves; restored to the full
        # 12 when collapsed). Do NOT shrink it unconditionally here —
        # that left the editor permanently short on models without
        # negative-prompt support (CodeRabbit Major, PR #41). Remember
        # the full height so the toggle can restore it.
        self._positive_prompt_full_height = 12
        # Split height MUST stay strictly < full height — otherwise
        # showing the negative half would GROW the positive box (the
        # opposite of "split into two halves"). Keep it ~= full - 5
        # but floor at 3 so it's always usable. apply_ui_config will
        # re-derive both proportionally when the live ui_config
        # height differs from 12 (Codex P2, PR #41).
        self._positive_prompt_split_height = max(
            3, min(7, self._positive_prompt_full_height - 5)
        )
        # Visibility is set on first model-change; default-hide until then.
        self._negative_prompt_section.pack_forget()
        # Explicit desired-visibility flag (see
        # _apply_negative_prompt_visibility — winfo_ismapped() is
        # unreliable before the window is realized). Starts hidden;
        # _update_motion_controls flips it per the selected model.
        self._neg_visible = False

        # Footer: char count badge + Edit button + Save button.
        # Kept as self._prompt_footer so the negative-prompt split
        # section can be re-packed BEFORE it (correct slot: under the
        # positive box, above the footer) when a model that supports
        # negative_prompt is selected.
        prompt_footer = tk.Frame(right_inner, bg=COLORS["bg_panel"])
        self._prompt_footer = prompt_footer
        prompt_footer.pack(fill=tk.X, pady=(6, 0))
        char_badge = tk.Frame(
            prompt_footer, bg=COLORS["bg_input"],
            highlightthickness=1, highlightbackground=COLORS["border"],
            padx=6, pady=2,
        )
        char_badge.pack(side=tk.LEFT)
        self.prompt_char_count_label = tk.Label(
            char_badge, text="0 chars", font=(FONT_FAMILY, 9),
            bg=COLORS["bg_input"], fg=COLORS["text_dim"],
        )
        self.prompt_char_count_label.pack()
        self.edit_prompt_btn = ttk.Button(
            prompt_footer, text="Edit", style=TTK_BTN_COMPACT,
            command=self._enter_edit_mode,
        )
        self.edit_prompt_btn.pack(side=tk.RIGHT, padx=(4, 0))
        self.save_prompt_btn = ttk.Button(
            prompt_footer, text="Save Prompt", style=TTK_BTN_COMPACT,
            state="disabled", command=self._save_prompt,
        )
        self.save_prompt_btn.pack(side=tk.RIGHT)

        # Load prompt config now that widgets exist
        self._load_prompt_config()

        # Re-apply the negative-prompt split visibility now that the
        # widgets exist. When ConfigPanel is constructed with
        # ``build_prompt=False`` (main_window's split layout —
        # prompt panel built later via build_prompt_panel here), the
        # _setup_ui → _load_config → _update_motion_controls chain
        # runs BEFORE this method creates ``_negative_prompt_section``.
        # That first call no-ops (the section is None), so without
        # this re-trigger the split editor stays hidden for models
        # that support a negative prompt — even though caps say YES.
        # User feedback 2026-05-21: Kling 2.5 Pro selected but no
        # negative-prompt half visible.
        try:
            current_model = self.config.get(
                "current_model",
                "fal-ai/kling-video/v2.1/pro/image-to-video",
            )
            self._update_motion_controls(current_model)
        except Exception:
            # Never break panel construction over a label/visibility
            # update.
            pass

        # Mini drop zone (below the prompt area, fills remaining space)
        if self._on_images_dropped is not None:
            self._build_mini_drop_zone(right_inner)

        return right_col

    def _load_prompt_config(self):
        """Load prompt slot/title/text. Safe to call before build_prompt_panel() — returns early."""
        if not hasattr(self, "slot_var"):
            return
        self.slot_var.set(self.config.get("current_prompt_slot", 1))
        self._update_prompt_preview()
        self._update_slot_button_colors()

    def _update_slot_button_colors(self):
        """Set selected slot button to black text on blue; others to light text on dark."""
        if not hasattr(self, "_slot_buttons"):
            return
        current = self.slot_var.get()
        for i, rb in enumerate(self._slot_buttons, 1):
            if i == current:
                rb.config(fg="#111111", bg=COLORS["accent_blue"])
            else:
                rb.config(fg=COLORS["text_light"], bg=COLORS["bg_panel"])

    # ── Mini drop zone (inside prompt panel) ─────────────────────────

    _VALID_IMG_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".bmp", ".gif", ".tiff", ".tif"}

    def _build_mini_drop_zone(self, parent):
        """Build a drop target below the prompt area, matching the original drop zone style."""
        _bg = COLORS["bg_input"]  # same as original bg_drop (#464649)
        _hover = "#505055"

        outer = tk.Frame(
            parent, bg=COLORS["bg_panel"],
            highlightthickness=2, highlightbackground=COLORS["border"],
        )
        outer.pack(fill=tk.BOTH, expand=True, pady=(8, 0))

        inner = tk.Frame(outer, bg=_bg, cursor="question_arrow")
        inner.pack(fill=tk.BOTH, expand=True)

        # Centered content (like original drop_zone.py)
        center = tk.Frame(inner, bg=_bg, cursor="question_arrow")
        center.place(relx=0.5, rely=0.5, anchor="center")

        lbl_icon = tk.Label(
            center, text="\U0001f4e5", font=(EMOJI_FONT_FAMILY, 28),
            bg=_bg, fg=COLORS["accent_blue"], cursor="question_arrow",
        )
        lbl_icon.pack(pady=(0, 4))

        lbl_main = tk.Label(
            center, text="DROP IMAGES HERE",
            font=(FONT_FAMILY, 11, "bold"),
            bg=_bg, fg=COLORS["text_light"], cursor="question_arrow",
        )
        lbl_main.pack(pady=1)

        lbl_sub = tk.Label(
            center, text="or click to browse",
            font=(FONT_FAMILY, 9),
            bg=_bg, fg=COLORS["text_dim"], cursor="question_arrow",
        )
        lbl_sub.pack(pady=(0, 2))

        self._mini_dz_status = tk.Label(
            center, text="", font=(FONT_FAMILY, 9, "bold"),
            bg=_bg, fg=COLORS["success"], cursor="question_arrow",
        )
        self._mini_dz_status.pack()

        self._mini_dz_inner = inner
        self._mini_dz_outer = outer
        _all = (inner, center, lbl_icon, lbl_main, lbl_sub, self._mini_dz_status)

        # Click-to-browse on all widgets
        for w in _all:
            w.bind("<Button-1>", lambda _e: self._mini_dz_browse())

        # Hover effects
        def _on_enter(_e):
            for c in _all:
                c.config(bg=_hover)
            outer.config(highlightbackground=COLORS["accent_blue"])

        def _on_leave(_e):
            for c in _all:
                c.config(bg=_bg)
            outer.config(highlightbackground=COLORS["border"])

        for w in _all:
            w.bind("<Enter>", _on_enter)
            w.bind("<Leave>", _on_leave)

        # DnD registration. Gate on the LIVE shared flag (_dnd_live), not the
        # stale local _HAS_DND, so a runtime tkdnd load failure that flipped
        # drop_zone.HAS_DND off also skips this register loop entirely instead
        # of relying on the per-widget except below to catch the doomed attempt
        # (subagent H1).
        if _dnd_live() and _DND_FILES:
            for w in (inner, center, lbl_icon, lbl_main, lbl_sub):
                try:
                    w.drop_target_register(_DND_FILES)
                    w.dnd_bind("<<DropEnter>>", lambda _e: (
                        [c.config(bg="#329632") for c in _all],
                    ))
                    w.dnd_bind("<<DropLeave>>", lambda _e: (
                        [c.config(bg=_bg) for c in _all],
                    ))
                    w.dnd_bind("<<Drop>>", self._mini_dz_on_drop)
                except Exception as exc:
                    # tkdnd can fail to load at runtime even though the import
                    # succeeded (_HAS_DND True). Log ONCE and stop (gemini MED:
                    # the per-widget loop would otherwise print 5 identical
                    # lines). The main file pickers still work. _safe_stderr
                    # tolerates sys.stderr is None under pythonw.exe (gemini
                    # HIGH).
                    _safe_stderr(
                        "[selfie-gen] config-panel drag-and-drop unavailable "
                        f"({type(exc).__name__}: {exc})\n"
                    )
                    break

    def _mini_dz_on_drop(self, event):
        """Handle DnD drop event on the mini drop zone."""
        paths = self._mini_dz_parse(event.data)
        if paths:
            self._mini_dz_deliver(paths)

    @staticmethod
    def _mini_dz_parse(data: str) -> List[str]:
        """Parse Windows DnD data string into a list of existing file paths."""
        import re as _re
        # tkinterdnd2 on Windows wraps paths with spaces in braces: {C:/my path/file.png}
        results = []
        for m in _re.finditer(r'\{([^}]+)\}|(\S+)', data):
            p = m.group(1) or m.group(2)
            if p and os.path.exists(p):
                results.append(p)
        return results

    def _mini_dz_browse(self):
        """Open file dialog to select images for the carousel."""
        filetypes = [
            ("Image files", "*.jpg *.jpeg *.png *.webp *.bmp *.gif *.tiff *.tif"),
            ("All files", "*.*"),
        ]
        paths = select_open_files(
            parent=self.winfo_toplevel(),
            title="Select Images",
            filetypes=filetypes,
        )
        if paths:
            self._mini_dz_deliver(list(paths))

    def _mini_dz_deliver(self, paths: List[str]):
        """Validate image extensions, call callback, show status feedback."""
        valid = [p for p in paths if os.path.splitext(p)[1].lower() in self._VALID_IMG_EXTS]
        if not valid:
            return
        if self._on_images_dropped:
            self._on_images_dropped(valid)
        n = len(valid)
        self._mini_dz_status.config(text=f"Added {n} image{'s' if n != 1 else ''}")
        # Auto-clear status after 2 seconds
        self.after(2000, lambda: (
            self._mini_dz_status.config(text="")
            if hasattr(self, "_mini_dz_status") else None
        ))

    def _load_config(self):
        """Load configuration values into UI."""
        # Model list + inline model selector
        self.models = ModelFetcher.get_merged_models(self.config)
        if not self.models:
            self.models = [
                {
                    # `or` (not a .get default): a JSON null model_display_name
                    # would return None with .get(key, default); fall through to
                    # the label instead.
                    "name": self.config.get("model_display_name") or "Kling 2.5 Turbo Standard",
                    "endpoint": self.config.get("current_model", ""),
                    "duration_default": self.config.get("video_duration", 10),
                }
            ]

        model_names = [get_model_display_name(m) for m in self.models]
        self.model_combo["values"] = model_names

        current_model = self.config.get("current_model", "")
        selected_index = 0
        for i, model in enumerate(self.models):
            if model.get("endpoint") == current_model:
                selected_index = i
                break
        else:
            current_name = self.config.get("model_display_name", "")
            name_index = next(
                (i for i, model in enumerate(self.models) if model.get("name") == current_name),
                None,
            )
            if name_index is not None:
                selected_index = name_index

        if model_names:
            self.model_combo.current(selected_index)
            self.model_var.set(model_names[selected_index])
            self._update_model_info_icon()

        # Background metadata + pricing enrichment from fal.ai API
        self._start_api_enrichment()

        # Output mode
        if self.config.get("use_source_folder", True):
            self.output_mode_var.set("source")
        else:
            self.output_mode_var.set("custom")
        self.output_path_var.set(self.config.get("output_folder", ""))
        self._update_output_entry_state()

        # Prompt slot/editor (deferred if prompt panel not yet built externally)
        self._load_prompt_config()

        # Duration
        duration = self.config.get("video_duration", 10)
        self.duration_var.set(f"{duration}s")
        logger.debug(f"Loaded duration: {duration}s")

        # Loop video option
        self.loop_video_var.set(self.config.get("loop_videos", False))
        # rPPG injection (off by default)
        self.rppg_var.set(self.config.get("rppg_enabled", False))
        # Per-Oldcam rPPG fan-out (Phase E of polish/v2.3 — opt-in slow
        # path; default OFF). The legacy 'rPPG on every Oldcam output'
        # behaviour from PR #40.
        self.rppg_per_oldcam_fanout_var.set(
            self.config.get("rppg_per_oldcam_fanout", False)
        )
        # Quality crush — selectable 720p/480p tiers (2026-06-18). Resolve
        # the effective list from the canonical key, migrating the legacy
        # ``crush_enabled`` boolean (True → 480p) and defaulting fresh
        # installs to 720p ON. Persist the canonical list back so the rest
        # of the app reads one shape.
        selected_crush = self._resolve_crush_resolutions_from_config()
        for label, var in self.crush_resolution_vars.items():
            var.set(label in selected_crush)
        self.config["crush_resolutions"] = selected_crush
        self.config["crush_enabled"] = bool(selected_crush)
        # AA attack-pipelines (opt-in, default OFF). normalize_aa_attacks returns
        # [] when neither key is present, so a fresh config leaves all unchecked.
        selected_aa = self._resolve_aa_attacks_from_config()
        for key, var in self.aa_attack_vars.items():
            var.set(key in selected_aa)
        self.config["aa_attacks"] = selected_aa
        self.config["aa_enabled"] = bool(selected_aa)
        self._check_ffmpeg_status()
        selected_versions = self._resolve_oldcam_versions_from_config()
        for version, var in self.oldcam_version_vars.items():
            var.set(version in selected_versions)
        self.config["oldcam_versions"] = selected_versions
        self.config["oldcam_version"] = selected_versions[-1] if selected_versions else "v24"

        # Output (fan-out) mode + live pipeline preview (2026-06-19). Bridge the
        # automation_-prefixed key the CLI writes so a mode chosen in the CLI
        # shows here too. Default = powerset.
        from automation.postproc_plan import normalize_mode as _norm_fanout
        # Prefer the CANONICAL automation_ key (the fingerprinted one the CLI
        # writes) over the GUI alias so a mode set in the CLI is honoured here,
        # then write BOTH back in lockstep so neither surface can drift
        # (CodeRabbit Major — same class as the rppg_per_oldcam_fanout bridge).
        _fanout_mode = _norm_fanout(
            self.config.get("automation_postproc_fanout_mode",
                            self.config.get("postproc_fanout_mode", "separate_and_combined"))
        )
        self.config["automation_postproc_fanout_mode"] = _fanout_mode
        self.config["postproc_fanout_mode"] = _fanout_mode
        if hasattr(self, "fanout_mode_var"):
            self.fanout_mode_var.set(self._FANOUT_DISPLAY.get(_fanout_mode, self._FANOUT_DISPLAY["separate_and_combined"]))
        self._refresh_pipeline_preview()

        # Reprocess options
        self.reprocess_var.set(self.config.get("allow_reprocess", False))
        self.reprocess_mode_var.set(self.config.get("reprocess_mode", "increment"))
        self._update_reprocess_mode_visibility()

        # Verbose GUI mode
        self.verbose_gui_var.set(self.config.get("verbose_gui_mode", False))

        # rPPG metric-in-filename toggle (default OFF -> clean name +
        # sidecar). _parse_bool tolerates a string-backed value
        # ("false"/"0") from a hand-edited kling_config.json — a bare
        # truthiness check treats "false" as True (CodeRabbit, PR #40).
        # None (uncoercible) -> default False.
        from face_similarity import _parse_bool

        self.rppg_metrics_var.set(
            bool(_parse_bool(self.config.get("rppg_metrics_in_filename", False)))
        )

        # Motion controls (end-frame lock + cfg_scale). lock default True
        # (mechanical return-to-pose is the intended selfie behaviour);
        # cfg default 0.7 (stricter prompt adherence than fal's 0.5).
        # lock_end_frame defaults to True, so an unparseable / null
        # value (_parse_bool -> None) must resolve to True, NOT
        # bool(None)=False — otherwise the GUI silently loads with the
        # end-frame lock OFF (mirrors queue_manager + pipeline; the
        # contrasting rppg_metrics_in_filename, default False, correctly
        # keeps bool(None)=False — do NOT unify them).
        _raw_lock = _parse_bool(self.config.get("lock_end_frame", True))
        self.lock_end_frame_var.set(
            True if _raw_lock is None else bool(_raw_lock)
        )
        try:
            _cfg = float(self.config.get("cfg_scale_value", 0.7))
        except (TypeError, ValueError):
            _cfg = 0.7
        self.cfg_scale_var.set(f"{max(0.0, min(1.0, _cfg)):g}")

        # Folder filter options
        self.folder_pattern_var.set(self.config.get("folder_filter_pattern", ""))
        self.folder_match_mode_var.set(self.config.get("folder_match_mode", "partial"))

        # Advanced video settings (3:4 is the canonical portrait default — the
        # selfie chain generates 864x1152 = 3:4 and this keeps video to match).
        self.aspect_ratio_var.set(self.config.get("aspect_ratio", "3:4"))
        self.resolution_var.set(self.config.get("resolution", "720p"))

        # Seed settings
        seed = self.config.get("seed", -1)
        self.seed_var.set(str(seed))
        self.random_seed_var.set(seed == -1)
        self._update_seed_entry_state()

        # Additional options
        self.camera_fixed_var.set(self.config.get("camera_fixed", False))
        self.generate_audio_var.set(self.config.get("generate_audio", False))

        # Update parameter visibility based on current model
        current_model = self.config.get(
            "current_model", "fal-ai/kling-video/v2.1/pro/image-to-video"
        )
        self.update_parameter_visibility(current_model)
        # Also sync the Motion row (end-frame checkbox / cfg entry
        # grayed-state + caps label + neg-prompt split visibility) to
        # the SAVED model on startup — _update_motion_controls is
        # otherwise only wired to _on_model_changed, so without this the
        # Motion row showed its construction-time state until the user
        # manually switched models (code-review finding, PR #41).
        self._update_motion_controls(current_model)
        # Sync the resolution combo (per-model options + enabled state) and the
        # live cost estimate to the SAVED model on startup — same rationale as
        # the motion-controls sync above (these are otherwise only wired to
        # _on_model_changed, so a fresh launch would show the construction-time
        # static ["480p","720p"] list + a blank cost until the user switches).
        self._sync_resolution_to_model(current_model)
        self._update_cost_estimate()

    def _sync_resolution_to_model(self, endpoint):
        """Set the resolution combo's values + enabled state from the model's
        resolution_options (empty/absent → disabled). Preserves the saved
        resolution when it's valid for the model, else uses the model default.

        Sets ``self._resolution_model_aware`` so update_parameter_visibility
        (which otherwise unconditionally re-enables every combo) defers to this
        method's disabled state for models with no selectable resolution — else
        a Kling model would show the previous Seedance's stale 480p/720p/1080p/4k
        options as selectable (code-reviewer, PR #114)."""
        try:
            from model_metadata import get_resolution_options, get_resolution_default
            options = get_resolution_options(endpoint)
            if options:
                self._resolution_model_aware = True
                self.resolution_combo.config(values=options, state="readonly")
                current = self.resolution_var.get()
                if current not in options:
                    current = get_resolution_default(endpoint)
                self.resolution_var.set(current)
                self.config["resolution"] = current
            else:
                # No selectable resolution — clear the stale option list AND the
                # displayed value (Gemini PR #114: a leftover "1080p" on a model
                # that ignores resolution is misleading), then disable so
                # update_parameter_visibility leaves it alone.
                self._resolution_model_aware = False
                self.resolution_combo.config(values=["—"], state="disabled")
                self.resolution_var.set("—")
        except Exception:
            pass

    def _start_api_enrichment(self):
        """Background: fetch schema metadata + pricing for all models, then refresh UI."""
        api_key = self.config.get("falai_api_key", "")
        if not api_key or not self.models:
            return

        models_ref = self.models  # capture reference

        def _enrich():
            try:
                from model_schema_manager import ModelSchemaManager
                schema_mgr = ModelSchemaManager(api_key)

                # 1. For each model, get metadata from cached/live schema
                for model in models_ref:
                    ep = model.get("endpoint", "")
                    if not ep:
                        continue
                    try:
                        meta = schema_mgr.get_model_metadata(ep)
                        if meta:
                            if meta.get("display_name"):
                                model["api_display_name"] = meta["display_name"]
                            if meta.get("description"):
                                model["api_description"] = meta["description"]
                            if meta.get("date"):
                                model["api_date"] = meta["date"]
                    except Exception as e:
                        logger.debug(f"Metadata fetch failed for {ep}: {e}")

                # 2. Batch-fetch pricing
                endpoints = [m.get("endpoint", "") for m in models_ref if m.get("endpoint")]
                try:
                    pricing = schema_mgr.get_model_pricing(endpoints)
                    for model in models_ref:
                        ep = model.get("endpoint", "")
                        if ep in pricing:
                            model["pricing_info"] = pricing[ep]
                except Exception as e:
                    logger.debug(f"Pricing fetch failed: {e}")

                # 3. Thread-safe GUI update
                try:
                    self.winfo_toplevel().after(0, self._refresh_model_dropdown)
                except Exception:
                    pass  # widget destroyed

            except Exception as e:
                logger.debug(f"API enrichment failed: {e}")

        threading.Thread(target=_enrich, daemon=True).start()

    def _refresh_model_dropdown(self):
        """Refresh the model dropdown with enriched data (runs on main thread)."""
        if not hasattr(self, "model_combo") or not self.models:
            return

        current_idx = self.model_combo.current()
        model_names = [get_model_display_name(m) for m in self.models]
        self.model_combo["values"] = model_names

        if 0 <= current_idx < len(model_names):
            self.model_combo.current(current_idx)
            self.model_var.set(model_names[current_idx])

        # Update info icon for the current model
        self._update_model_info_icon()

    def _on_model_changed(self, event=None):
        """Handle inline model selection changes."""
        selected_name = self.model_var.get()
        if not selected_name:
            return

        selected_model = None
        selected_index = self.model_combo.current()
        if 0 <= selected_index < len(self.models):
            selected_model = self.models[selected_index]
        else:
            for model in self.models:
                if model.get("name") == selected_name:
                    selected_model = model
                    break

        if not selected_model:
            return

        model_endpoint = selected_model.get("endpoint", "")
        if not model_endpoint:
            return

        self.config["current_model"] = model_endpoint
        self.config["model_display_name"] = selected_model.get("name", selected_name)

        # Keep duration choices aligned with model metadata even without schema/API access.
        duration_options = selected_model.get("duration_options", [])
        if isinstance(duration_options, int):
            duration_options = [duration_options]
        if isinstance(duration_options, list):
            normalized = []
            for value in duration_options:
                try:
                    normalized.append(int(value))
                except (TypeError, ValueError):
                    continue
            if normalized:
                self.duration_combo.config(values=[f"{value}s" for value in normalized])
                default_duration = selected_model.get("duration_default", normalized[0])
                try:
                    default_duration = int(default_duration)
                except (TypeError, ValueError):
                    default_duration = normalized[0]

                current_text = self.duration_var.get().rstrip("s").strip()
                current_duration = (
                    int(current_text) if current_text.isdigit() else default_duration
                )
                if current_duration not in normalized:
                    current_duration = default_duration

                self.duration_var.set(f"{current_duration}s")
                self.config["video_duration"] = current_duration

        # Keep resolution choices aligned with the model (mirrors the duration
        # block above). A model with a non-empty resolution_options list gets a
        # per-model enabled combo; a model with none (most Kling tiers — fal
        # fixes their res) gets the combo DISABLED so we never offer a value the
        # live schema would just drop. Shared with the startup sync.
        self._sync_resolution_to_model(model_endpoint)

        self.update_parameter_visibility(model_endpoint)
        self._update_motion_controls(model_endpoint)

        duration_text = self.duration_var.get().rstrip("s").strip()
        if duration_text.isdigit():
            self.config["video_duration"] = int(duration_text)

        self._update_model_info_icon(selected_model)
        self._update_cost_estimate()
        self._notify_change(f"Model changed to {self.config['model_display_name']}")

    def _get_current_model_notes(self) -> str:
        """Return structured tooltip for the currently selected model (used by HoverTooltip).

        Sections (shown when available):
          - Provider Info: API description from fal.ai
          - Pricing: live pricing from API
          - User Notes: from models.json user_notes
        """
        idx = self.model_combo.current()
        if not (hasattr(self, "models") and 0 <= idx < len(self.models)):
            return ""

        model = self.models[idx]
        sections = []

        # Provider info (from API metadata)
        api_desc = model.get("api_description", "")
        if api_desc:
            sections.append(f"\u2500\u2500 Provider Info \u2500\u2500\n{api_desc}")

        # Pricing. Two sources: the LIVE fal.ai quote (``pricing_info``,
        # populated by the API enrichment) and the VERIFIED static value in
        # models.json (``pricing_fallback``). The live fal.ai quote has been
        # observed returning wrong/stale numbers for the Kling models, so when
        # a model defines a ``pricing_fallback`` we treat THAT as authoritative
        # and prefer it over any nonzero live price (Codex P2 2026-06-03) —
        # otherwise the tooltip shows a stale live number even though we have a
        # verified one. Only models WITHOUT a fallback use the live quote.
        # Both lookups coerce to {} if the JSON value isn't a dict (a list /
        # str / null would otherwise crash .get() — Gemini/Codex 2026-06-03).
        def _as_dict(v):
            return v if isinstance(v, dict) else {}

        live = _as_dict(model.get("pricing_info"))
        fb = _as_dict(model.get("pricing_fallback"))
        is_fallback = False
        if fb.get("unit_price"):
            # Verified value wins over the unreliable live quote.
            unit = fb.get("unit", "second")
            price = fb.get("unit_price", 0)
            is_fallback = True
        else:
            unit = live.get("unit", "")
            price = live.get("unit_price", 0)
        if price:
            if unit == "second":
                price_str = f"${price:.3f}/second (${price * 5:.2f}/5s, ${price * 10:.2f}/10s)"
            elif unit == "video":
                price_str = f"${price:.2f}/video (flat rate)"
            elif unit == "image":
                price_str = f"${price:.2f}/image"
            else:
                price_str = f"${price:.3f}/{unit}" if unit else f"${price:.3f}"
            if is_fallback:
                price_str += "\n(verified reference price, audio off)"
            sections.append(f"\u2500\u2500 Pricing \u2500\u2500\n{price_str}")

        # User notes (from models.json user_notes field)
        user_notes = model.get("user_notes", "")
        if user_notes:
            sections.append(f"\u2500\u2500 User Notes \u2500\u2500\n{user_notes}")

        # Legacy fallback: if no sections but has old-style "notes"
        if not sections:
            return model.get("notes", "")

        return "\n\n".join(sections)

    def _get_oldcam_version_notes(self) -> str:
        """Return version comparison tooltip for the Oldcam (ⓘ) icon.

        One line per version — keeps the tooltip short enough to fit
        on-screen WITHOUT covering the trigger icon (GPT diagnosis
        2026-05-21: tall tooltips overlapping the trigger caused a
        flicker/destroy loop on Windows).
        """
        lines = [
            "═══ OLDCAM VERSION GUIDE ═══",
            "",
            "Default: v24. Use it unless you're A/B testing.",
            "",
            "v7   Modern phone imperfections — subtle JPEG/OIS/AF artifacts.",
            "v8   Hardware physics — stronger OIS/noise/AWB; can over-compress.",
            "v9   Face-aware pass — region masks; blur can look artificial.",
            "v10  rPPG sync — biological pulse; can show as color pulsing.",
            "v11  Combined pass — rPPG + AWB; can tint sepia.",
            "v12  Pristine hardware-only — clean realism, good low-light.",
            "v13  High-end daylight — pristine optics, no sensor noise.",
            "v14  Forensic daylight — corrected AWB/bloom; sensor-floor tell.",
            "v15  Temporal Mute — ghosting blend defeats consistency detectors.",
            "v24  Crush Laundromat ★ — v15 + resolution round-trip; best tested.",
        ]
        return "\n".join(lines)

    def _update_model_info_icon(self, model: dict = None):
        """Set info icon color: blue when model has notes/info, dim otherwise."""
        if not hasattr(self, "model_info_icon"):
            return
        if model is None:
            idx = self.model_combo.current()
            model = self.models[idx] if hasattr(self, "models") and 0 <= idx < len(self.models) else {}
        has_info = bool(
            model.get("notes", "")
            or model.get("user_notes", "")
            or model.get("api_description", "")
            or model.get("pricing_info", {})
        )
        self.model_info_icon.config(
            fg=COLORS["accent_blue"] if has_info else COLORS["text_dim"]
        )

    def _open_model_manager(self):
        """Open the Model Manager dialog."""
        from .model_manager_dialog import ModelManagerDialog
        ModelManagerDialog(
            parent=self.winfo_toplevel(),
            config=self.config,
            api_key=self.config.get("falai_api_key", ""),
            on_save=self._on_model_manager_saved,
        )

    def _on_model_manager_saved(self):
        """Refresh the model dropdown after Model Manager changes."""
        prev_endpoint = self.config.get("current_model", "")
        self.models = ModelFetcher.get_merged_models(self.config)
        if not self.models:
            self.models = FALLBACK_MODELS
        model_names = [get_model_display_name(m) for m in self.models]
        self.model_combo["values"] = model_names
        selected_index = 0
        for i, m in enumerate(self.models):
            if m.get("endpoint") == prev_endpoint:
                selected_index = i
                break
        if model_names:
            self.model_combo.current(selected_index)
            self.model_var.set(model_names[selected_index])
            chosen = self.models[selected_index]
            self.config["current_model"] = chosen.get("endpoint", "")
            self.config["model_display_name"] = chosen.get("name", "")
            self._update_model_info_icon(chosen)
        self._notify_change("Model list updated via Model Manager")

    def _check_ffmpeg_status(self):
        """Check if FFmpeg is available and update UI."""
        try:
            from .video_looper import check_ffmpeg_available

            available, message = check_ffmpeg_available()
            if available:
                self.loop_info_label.config(text="(FFmpeg ready)", fg="#64FF64")
            else:
                self.loop_info_label.config(text="(FFmpeg not found)", fg="#FF6464")
        except Exception:
            self.loop_info_label.config(text="(requires FFmpeg)", fg=COLORS["text_dim"])

    def _on_loop_changed(self):
        """Handle loop video checkbox change."""
        self.config["loop_videos"] = self.loop_video_var.get()
        status = "enabled" if self.loop_video_var.get() else "disabled"
        self._refresh_pipeline_preview()
        self._notify_change(f"Loop video {status}")

    def _on_rppg_changed(self):
        """Handle rPPG injection checkbox change."""
        self.config["rppg_enabled"] = self.rppg_var.get()
        status = "enabled" if self.rppg_var.get() else "disabled"
        self._refresh_pipeline_preview()
        self._notify_change(f"rPPG injection {status}")

    def _on_rppg_per_oldcam_fanout_changed(self):
        """Handle the opt-in per-Oldcam rPPG fan-out checkbox.

        Phase E of polish/v2.3 (2026-05-22): default flow runs rPPG
        ONCE on the base and every Oldcam version derives from that
        injection. Enabling this checkbox restores the legacy
        per-Oldcam fan-out (an additional rPPG pass per Oldcam
        output — slower but produces a fresh-pulse Oldcam variant).
        """
        value = self.rppg_per_oldcam_fanout_var.get()
        self.config["rppg_per_oldcam_fanout"] = value
        status = "enabled (slow path)" if value else "disabled"
        self._notify_change(f"rPPG per-Oldcam fan-out {status}")

    def _resolve_crush_resolutions_from_config(self) -> List[str]:
        """Resolve the effective crush-resolution labels from config.

        Single shape for the rest of the app: canonical ``crush_resolutions``
        list wins; otherwise the legacy ``crush_enabled`` boolean migrates
        (True → 480p, the pre-multi behaviour); a brand-new config (neither
        key set) defaults to 720p ON. Mirrors
        automation.video_crush.normalize_crush_resolutions exactly.
        """
        from automation.video_crush import normalize_crush_resolutions

        kwargs = {}
        if "crush_resolutions" in self.config:
            kwargs["resolutions"] = self.config["crush_resolutions"]
        if "crush_enabled" in self.config:
            kwargs["legacy_enabled"] = self.config["crush_enabled"]
        valid = tuple(self.crush_resolution_vars.keys())
        return [r for r in normalize_crush_resolutions(**kwargs) if r in valid]

    def _resolve_aa_attacks_from_config(self) -> List[str]:
        """Resolve the effective AA attack-pipeline labels from config.

        AA is opt-in (default OFF): unlike crush, when NEITHER key is present we
        return [] rather than normalize's bare default. Mirrors the pipeline /
        queue resolvers.
        """
        from automation.video_aa import normalize_aa_attacks

        attacks = self.config.get("aa_attacks")
        legacy = self.config.get("aa_enabled")
        if attacks is None and legacy is None:
            return []
        kwargs = {}
        if attacks is not None:
            kwargs["attacks"] = attacks
        if legacy is not None:
            kwargs["legacy_enabled"] = legacy
        valid = tuple(self.aa_attack_vars.keys())
        return [a for a in normalize_aa_attacks(**kwargs) if a in valid]

    def _on_crush_resolutions_changed(self) -> None:
        """Handle a quality-crush resolution checkbox toggle.

        Persists the canonical ``crush_resolutions`` list (highest-first) plus
        the back-compat ``crush_enabled`` boolean (True iff any tier is on)."""
        from automation.video_crush import CRUSH_RESOLUTIONS as _CR

        selected = [
            label
            for label, var in self.crush_resolution_vars.items()
            if var.get()
        ]
        selected = sorted(selected, key=lambda lbl: _CR[lbl], reverse=True)
        self.config["crush_resolutions"] = selected
        self.config["crush_enabled"] = bool(selected)
        if selected:
            status = "enabled (" + ", ".join(selected) + ")"
        else:
            status = "disabled"
        self._refresh_pipeline_preview()
        self._notify_change(f"Quality crush {status}")

    def _on_aa_attacks_changed(self) -> None:
        """Handle an AA attack-pipeline checkbox toggle.

        Persists the canonical ``aa_attacks`` list (display order) plus the
        back-compat ``aa_enabled`` boolean (True iff any pipeline is on)."""
        from automation.video_aa import normalize_aa_attacks

        selected = [
            key for key, var in self.aa_attack_vars.items() if var.get()
        ]
        # Route through normalize for canonical ordering + dedup.
        selected = normalize_aa_attacks(attacks=selected)
        self.config["aa_attacks"] = selected
        self.config["aa_enabled"] = bool(selected)
        if selected:
            status = "enabled (" + ", ".join(selected) + ")"
        else:
            status = "disabled"
        self._refresh_pipeline_preview()
        self._notify_change(f"AA adversarial pass {status}")

    def _oldcam_version_key(self, version: str) -> int:
        try:
            return int(str(version).lower().replace("v", "", 1))
        except ValueError:
            return -1

    def _resolve_oldcam_versions_from_config(self) -> List[str]:
        configured = self.config.get("oldcam_versions")
        valid_versions = tuple(self.oldcam_version_vars.keys())
        has_versions_key = isinstance(configured, list)
        if has_versions_key:
            versions = [str(v).lower() for v in configured if str(v).lower() in valid_versions]
        else:
            versions = []

        if has_versions_key:
            return sorted(set(versions), key=self._oldcam_version_key)

        if not versions:
            legacy = str(self.config.get("oldcam_version", "v24")).lower()
            if legacy == "all":
                versions = list(valid_versions)
            elif legacy in valid_versions:
                versions = [legacy]
            else:
                versions = ["v24"]

        return sorted(set(versions), key=self._oldcam_version_key)

    def _on_oldcam_versions_changed(self):
        """Handle oldcam version checkbox changes."""
        selected_versions = [
            version
            for version in self.oldcam_version_vars
            if self.oldcam_version_vars[version].get()
        ]
        selected_versions = sorted(set(selected_versions), key=self._oldcam_version_key)
        self.config["oldcam_versions"] = selected_versions
        # Legacy compatibility key: highest selected version, or v24 default when empty.
        self.config["oldcam_version"] = selected_versions[-1] if selected_versions else "v24"
        self._refresh_pipeline_preview()
        if selected_versions:
            self._notify_change("Oldcam versions set to " + ", ".join(selected_versions))
        else:
            self._notify_change("Oldcam disabled (no versions selected)")

    # Display labels for the output-mode combobox (value <-> friendly text).
    _FANOUT_DISPLAY = {
        "separate_and_combined": "Separate + combined (powerset)",
        "combined_only": "Combined only (one chain)",
    }
    _FANOUT_DISPLAY_TO_VALUE = {v: k for k, v in _FANOUT_DISPLAY.items()}

    def _pipeline_preview_text(self) -> str:
        """Read-only one-line summary of the resulting post-processing plan.

        Built from the SAME shared planner the queue executor uses
        (automation.postproc_plan) so the preview can never disagree with what
        actually runs. Reads config directly via the canonical normalizers so it
        is testable without live Tk widgets.
        """
        from automation.postproc_plan import build_plan, plan_preview_line
        from automation.video_crush import normalize_crush_resolutions
        from automation.video_aa import normalize_aa_attacks
        from automation.oldcam import normalize_oldcam_versions

        # Defensive: a corrupted/hand-edited config could be a non-dict; treat
        # it as empty rather than crashing the preview render (gemini HIGH).
        cfg = self.config if isinstance(self.config, dict) else {}
        ckwargs = {}
        if "crush_resolutions" in cfg:
            ckwargs["resolutions"] = cfg["crush_resolutions"]
        if "crush_enabled" in cfg:
            ckwargs["legacy_enabled"] = cfg["crush_enabled"]
        crush = normalize_crush_resolutions(**ckwargs)

        aa_present = ("aa_attacks" in cfg) or ("aa_enabled" in cfg)
        akwargs = {}
        if "aa_attacks" in cfg:
            akwargs["attacks"] = cfg["aa_attacks"]
        if "aa_enabled" in cfg:
            akwargs["legacy_enabled"] = cfg["aa_enabled"]
        aa = normalize_aa_attacks(**akwargs) if aa_present else []

        oldcam = normalize_oldcam_versions(
            cfg.get("oldcam_versions", cfg.get("oldcam_version", []))
        )
        plan = build_plan(
            rppg_enabled=bool(cfg.get("rppg_enabled", False)),
            loop_enabled=bool(cfg.get("loop_videos", False)),
            crush_resolutions=crush,
            aa_attacks=aa,
            oldcam_versions=oldcam,
            mode=str(cfg.get("postproc_fanout_mode", "separate_and_combined")),
        )
        return plan_preview_line(plan)

    def _refresh_pipeline_preview(self) -> None:
        """Update the live preview label (no-op if the widget isn't built yet)."""
        label = getattr(self, "pipeline_preview_label", None)
        if label is None:
            return
        try:
            label.config(text=self._pipeline_preview_text())
        except Exception:
            # Don't crash the UI on a preview build error, but log it so
            # planner/normalizer wiring breakage is debuggable (CodeRabbit).
            logger.debug("Failed to refresh pipeline preview", exc_info=True)

    def _on_fanout_mode_changed(self) -> None:
        """Persist the chosen output (fan-out) mode and refresh the preview."""
        display = self.fanout_mode_var.get()
        value = self._FANOUT_DISPLAY_TO_VALUE.get(display, "separate_and_combined")
        # Write BOTH keys so the GUI alias and the canonical CLI key stay in
        # lockstep (CodeRabbit Major — no cross-surface drift).
        self.config["postproc_fanout_mode"] = value
        self.config["automation_postproc_fanout_mode"] = value
        self._refresh_pipeline_preview()
        self._notify_change(
            "Output mode: "
            + ("powerset (separate + combined)" if value == "separate_and_combined" else "combined only")
        )

    def _on_oldcam_rerun_clicked(self):
        """Trigger Oldcam-only rerun callback from the host window."""
        if callable(self._on_oldcam_rerun):
            self._on_oldcam_rerun()
            return
        self._notify_change("Oldcam rerun action unavailable")

    def _on_oldcam_pick_rerun_clicked(self):
        """Open file picker and run Oldcam on selected video(s)."""
        if callable(self._on_oldcam_pick_rerun):
            self._on_oldcam_pick_rerun()
            return
        self._notify_change("Oldcam pick-and-rerun action unavailable")

    def _on_reprocess_changed(self):
        """Handle reprocess checkbox change."""
        self.config["allow_reprocess"] = self.reprocess_var.get()
        self._update_reprocess_mode_visibility()
        status = "enabled" if self.reprocess_var.get() else "disabled"
        self._notify_change(f"Reprocessing {status}")

    def _on_reprocess_mode_changed(self):
        """Handle reprocess mode radio change."""
        mode = self.reprocess_mode_var.get()
        self.config["reprocess_mode"] = mode
        self._notify_change(f"Reprocess mode set to {mode}")

    def _on_verbose_changed(self):
        """Handle verbose mode checkbox change."""
        self.config["verbose_gui_mode"] = self.verbose_gui_var.get()
        status = "enabled" if self.verbose_gui_var.get() else "disabled"
        self._notify_change(f"Verbose mode {status}")

    def _on_rppg_metrics_changed(self):
        """Handle the rPPG metric-in-filename toggle.

        OFF (default): injector's metric-suffixed name is stripped to a
        clean ``{stem}-rppg`` and the 5 metrics go to a ``.metrics.json``
        sidecar. ON: the metric suffix stays in the filename.
        """
        self.config["rppg_metrics_in_filename"] = self.rppg_metrics_var.get()
        if self.rppg_metrics_var.get():
            status = "kept in filename"
        else:
            status = "moved to .metrics.json sidecar"
        self._notify_change(f"rPPG metrics {status}")

    def _on_lock_end_frame_changed(self):
        """Persist the end-frame-lock toggle. Only meaningful for models
        whose schema exposes an end-frame param (the checkbox is grayed
        out otherwise); the dispatcher re-checks capability anyway."""
        self.config["lock_end_frame"] = bool(self.lock_end_frame_var.get())
        state = "on" if self.lock_end_frame_var.get() else "off"
        self._notify_change(f"End-frame lock {state}")

    def _on_cfg_scale_changed(self, event=None):
        """Persist cfg_scale (clamped to the documented 0-1 fal.ai range).
        Ignored at submit for models that dropped cfg_scale (o3/seedance);
        the dispatcher gates on capability."""
        raw = self.cfg_scale_var.get().strip()
        try:
            val = float(raw)
        except (TypeError, ValueError):
            val = 0.7
        val = max(0.0, min(1.0, val))
        # Reflect the clamp back so the user sees the effective value.
        self.cfg_scale_var.set(f"{val:g}")
        self.config["cfg_scale_value"] = val
        self._notify_change(f"cfg_scale set to {val:g}")

    def _update_motion_controls(self, model_endpoint: str):
        """Enable/disable the motion row from the model's capabilities.

        Single source of truth: model_metadata.get_model_capabilities
        (the dispatcher + queue_manager read the SAME flags). Per the
        user's chosen UX the controls are NEVER hidden — an unsupported
        end-frame / cfg control is GRAYED OUT (state=disabled) so the
        layout is stable and it's obvious which models can do what. The
        caps label echoes the support set.
        """
        try:
            from model_metadata import (
                get_model_capabilities,
                get_model_by_endpoint,
            )

            caps = get_model_capabilities(model_endpoint)
        except Exception:
            return  # never break the model-change flow over a label

        # A custom / unknown endpoint is NOT in MODEL_METADATA, so caps
        # are the conservative defaults (no neg/cfg/end). But the
        # dispatcher + GUI queue deliberately DON'T pre-drop neg/cfg for
        # unknown endpoints (the live schema decides — Codex P2). The
        # Motion row must mirror that: enable cfg + the negative-prompt
        # split for a custom model rather than always graying them out
        # (a UI/dispatch inconsistency otherwise — Codex P2, PR #41).
        # end-frame stays caps-driven — a custom model with no known
        # end param has nowhere to send it.
        try:
            _is_known = get_model_by_endpoint(model_endpoint) is not None
        except Exception:
            # Fail safe -> treat as custom/unknown endpoint so the
            # cfg + negative-prompt controls stay ENABLED and the live
            # schema decides. This matches the intentional bypass
            # philosophy used in the dispatcher + queue_manager: an
            # endpoint we can't classify must NOT be silently graded
            # as a known model with no caps (which would gray out
            # controls the user might actually need). The old fail-safe
            # `True` produced the opposite of the design intent (full-
            # diff subagent finding, PR #41).
            _is_known = False

        has_end = caps.get("end_image_param") is not None
        has_cfg = bool(caps.get("supports_cfg_scale")) or not _is_known
        has_neg = bool(caps.get("supports_negative_prompt")) or not _is_known

        if hasattr(self, "lock_end_frame_checkbox"):
            self.lock_end_frame_checkbox.config(
                state=tk.NORMAL if has_end else tk.DISABLED
            )
        if hasattr(self, "cfg_scale_entry"):
            self.cfg_scale_entry.config(
                state=tk.NORMAL if has_cfg else tk.DISABLED
            )
        if hasattr(self, "cfg_scale_label"):
            self.cfg_scale_label.config(
                fg=COLORS["text_dim"] if not has_cfg else COLORS["text_light"]
            )
        if hasattr(self, "model_caps_label"):
            def _mark(ok):
                return "✓" if ok else "✗"
            self.model_caps_label.config(
                text=(
                    f"negative {_mark(has_neg)} · "
                    f"end-frame {_mark(has_end)} · "
                    f"cfg {_mark(has_cfg)}"
                )
            )
        # Reflect negative-prompt support in the prompt editor: a
        # supported model splits the prompt box into positive/negative;
        # an unsupported one collapses back to a single positive box.
        self._apply_negative_prompt_visibility(has_neg)

    def _apply_negative_prompt_visibility(self, has_neg: bool):
        """Show/hide the negative-prompt half of the split prompt editor.

        Widgets are created once; we only pack / pack_forget the section
        so its text survives toggling. Desired visibility is tracked with
        an explicit flag (``_neg_visible``) — NOT ``winfo_ismapped()``,
        which is False until the whole window is realized and would make
        the section never appear on initial load. When showing, pack it
        BEFORE the footer so it always lands in the right slot (under the
        positive box, above the Edit/Save row) regardless of call order.
        No-op until the split editor exists."""
        neg = getattr(self, "_negative_prompt_section", None)
        if neg is None:
            return
        want = bool(has_neg)
        if getattr(self, "_neg_visible", None) == want:
            return  # idempotent — already in the desired state
        try:
            pp = getattr(self, "prompt_preview", None)
            if want:
                footer = getattr(self, "_prompt_footer", None)
                if footer is not None:
                    neg.pack(
                        fill=tk.BOTH, expand=True, pady=(4, 0),
                        before=footer,
                    )
                else:
                    neg.pack(fill=tk.BOTH, expand=True, pady=(4, 0))
                # Shrink the positive box so the split reads as ~two
                # halves rather than the negative being a thin afterthought.
                if pp is not None:
                    pp.config(
                        height=getattr(self, "_positive_prompt_split_height", 7)
                    )
            else:
                neg.pack_forget()
                # Negative hidden -> restore the positive box to full
                # height (CodeRabbit Major, PR #41 — it was left short).
                if pp is not None:
                    pp.config(
                        height=getattr(self, "_positive_prompt_full_height", 12)
                    )
            self._neg_visible = want
        except Exception:
            pass

    def _on_folder_pattern_changed(self, event=None):
        """Handle folder pattern entry change."""
        pattern = self.folder_pattern_var.get().strip()
        self.config["folder_filter_pattern"] = pattern
        if pattern:
            self._notify_change(f"Folder pattern set to '{pattern}'")
        else:
            self._notify_change("Folder pattern cleared")

    def _on_folder_match_mode_changed(self):
        """Handle folder match mode radio change."""
        mode = self.folder_match_mode_var.get()
        self.config["folder_match_mode"] = mode
        self._notify_change(f"Folder match mode set to {mode}")

    def _on_aspect_ratio_changed(self, event=None):
        """Handle aspect ratio combobox change."""
        ratio = self.aspect_ratio_var.get()
        self.config["aspect_ratio"] = ratio
        self._notify_change(f"Aspect ratio set to {ratio}")

    def _on_resolution_changed(self, event=None):
        """Handle resolution combobox change."""
        resolution = self.resolution_var.get()
        self.config["resolution"] = resolution
        self._update_cost_estimate()
        self._notify_change(f"Resolution set to {resolution}")

    def _update_cost_estimate(self):
        """Refresh the live token-cost estimate label for the current
        model + resolution + duration + audio. Token-priced models (Seedance)
        show e.g. "≈ $3.02 / 10s @ 720p"; flat-priced models clear the label
        (their default-res price already shows in the dropdown name)."""
        label = getattr(self, "cost_estimate_label", None)
        if label is None:
            return
        try:
            from model_metadata import estimate_cost_usd
            endpoint = str(self.config.get("current_model") or "")
            resolution = self.resolution_var.get() or self.config.get("resolution")
            dur_text = self.duration_var.get().rstrip("s").strip()
            duration = int(dur_text) if dur_text.isdigit() else self.config.get("video_duration", 10)
            audio = bool(self.config.get("generate_audio", False))
            cost = estimate_cost_usd(endpoint, resolution, duration, audio=audio)
            if cost is not None:
                label.config(text=f"≈ ${cost:.2f} / {duration}s @ {resolution}")
            else:
                label.config(text="")
        except Exception:
            # A cost estimate must never break the config panel.
            label.config(text="")

    def _on_duration_changed(self, event=None):
        """Handle duration selection change with validation."""
        try:
            # Extract numeric value from "10s" format
            duration_str = self.duration_var.get().rstrip('s').strip()
            if not duration_str.isdigit():
                logger.error(f"Invalid duration format: {self.duration_var.get()}")
                return
            
            duration = int(duration_str)
            
            # Validate duration is positive
            if duration <= 0:
                logger.error(f"Duration must be positive, got: {duration}")
                return

            # Update config
            self.config["video_duration"] = duration

            # Refresh the live cost estimate (duration scales token cost).
            self._update_cost_estimate()

            # Notify parent window of change
            self._notify_change(f"Duration set to {duration}s")

            logger.debug(f"Duration changed to: {duration}s")

        except (ValueError, AttributeError) as e:
            logger.error(f"Error changing duration: {e}")
            # Revert to previous valid value
            current_duration = self.config.get("video_duration", 10)
            self.duration_var.set(f"{current_duration}s")

    def _on_seed_changed(self, event=None):
        """Handle seed entry change."""
        try:
            seed_str = self.seed_var.get().strip()
            seed = int(seed_str) if seed_str else -1
            self.config["seed"] = seed
            # Update random checkbox to reflect current state
            self.random_seed_var.set(seed == -1)
            self._notify_change(
                f"Seed set to {seed}" if seed != -1 else "Seed set to random"
            )
        except ValueError:
            # Invalid input, reset to -1
            self.seed_var.set("-1")
            self.config["seed"] = -1
            self.random_seed_var.set(True)
            self._notify_change("Invalid seed, reset to random")

    def _on_random_seed_changed(self):
        """Handle random seed checkbox change."""
        if self.random_seed_var.get():
            self.seed_var.set("-1")
            self.config["seed"] = -1
            self._notify_change("Seed set to random")
        else:
            # If unchecking random, set a default seed value
            self.seed_var.set("42")
            self.config["seed"] = 42
            self._notify_change("Seed set to 42 (editable)")
        self._update_seed_entry_state()

    def _update_seed_entry_state(self):
        """Enable/disable seed entry based on random checkbox."""
        if self.random_seed_var.get():
            self.seed_entry.config(state="disabled")
        else:
            self.seed_entry.config(state="normal")

    def _on_camera_fixed_changed(self):
        """Handle camera fixed checkbox change."""
        self.config["camera_fixed"] = self.camera_fixed_var.get()
        status = "enabled" if self.camera_fixed_var.get() else "disabled"
        self._notify_change(f"Camera fixed {status}")

    def _on_generate_audio_changed(self):
        """Handle generate audio checkbox change."""
        self.config["generate_audio"] = self.generate_audio_var.get()
        # Audio affects cost on models with an audio_rate_multiplier (1.5 Pro).
        self._update_cost_estimate()
        status = "enabled" if self.generate_audio_var.get() else "disabled"
        self._notify_change(f"Generate audio {status}")

    def _update_reprocess_mode_visibility(self):
        """Show/hide reprocess mode options based on checkbox."""
        if self.reprocess_var.get():
            self.overwrite_radio.config(state="normal")
            self.increment_radio.config(state="normal")
        else:
            self.overwrite_radio.config(state="disabled")
            self.increment_radio.config(state="disabled")

    def _on_output_mode_changed(self):
        """Handle output mode radio change."""
        is_source = self.output_mode_var.get() == "source"
        self.config["use_source_folder"] = is_source
        self._update_output_entry_state()
        mode_desc = "source folder" if is_source else "custom folder"
        self._notify_change(f"Output mode set to {mode_desc}")

    def _update_output_entry_state(self):
        """Enable/disable output path entry based on mode."""
        if self.output_mode_var.get() == "source":
            self.output_entry.config(state="disabled")
            self.browse_btn.config(state="disabled")
        else:
            self.output_entry.config(state="normal")
            self.browse_btn.config(state="normal")

    def _browse_output_folder(self):
        """Open folder browser for output path."""
        folder = select_directory(
            parent=self.winfo_toplevel(),
            title="Select Output Folder",
            initialdir=self.output_path_var.get() or ".",
        )
        if folder:
            self.output_path_var.set(folder)
            self.config["output_folder"] = folder
            self._notify_change(f"Output folder set to {folder}")

    def _on_slot_changed(self):
        """Handle prompt slot change — discard any unsaved edits first."""
        if self._prompt_edit_mode:
            self._exit_edit_mode_internal()
        slot = self.slot_var.get()
        self.config["current_prompt_slot"] = slot
        self._update_prompt_preview()
        self._update_slot_button_colors()
        self._notify_change(f"Prompt slot changed to {slot}")

    def _update_prompt_preview(self):
        """Load selected slot title/prompt into the read-only display widgets."""
        slot = self.slot_var.get()
        saved_prompts = self.config.setdefault("saved_prompts", {})
        saved_titles = self.config.setdefault("prompt_titles", {})
        prompt = saved_prompts.get(str(slot), "") or ""
        title = saved_titles.get(str(slot), "") or ""

        # Title entry: briefly enable to update, then restore disabled state
        self.prompt_title_entry.config(state="normal")
        self.prompt_title_var.set(title)
        if not self._prompt_edit_mode:
            self.prompt_title_entry.config(state="disabled")

        # Text widget: briefly enable to update, then restore disabled state
        self.prompt_preview.config(state="normal")
        self.prompt_preview.delete("1.0", tk.END)
        self.prompt_preview.insert("1.0", prompt)
        if not self._prompt_edit_mode:
            self.prompt_preview.config(state="disabled")

        # Negative half (split editor) — same slot, backed by
        # config["negative_prompts"]. Mirrors the positive box's
        # enable/disable lifecycle so editing one edits both.
        if hasattr(self, "negative_prompt_preview"):
            neg_prompts = self.config.setdefault("negative_prompts", {})
            neg = neg_prompts.get(str(slot), "") or ""
            self.negative_prompt_preview.config(state="normal")
            self.negative_prompt_preview.delete("1.0", tk.END)
            self.negative_prompt_preview.insert("1.0", neg)
            if not self._prompt_edit_mode:
                self.negative_prompt_preview.config(state="disabled")

        self._update_prompt_char_count()

    def _update_prompt_char_count(self, event=None):
        """Update live prompt character count."""
        prompt_text = self.prompt_preview.get("1.0", "end-1c")
        self.prompt_char_count_label.config(text=f"{len(prompt_text)} chars")

    def _save_prompt(self):
        """Persist title and prompt text for the currently selected slot."""
        slot = str(self.slot_var.get())
        saved_prompts = self.config.setdefault("saved_prompts", {})
        saved_titles = self.config.setdefault("prompt_titles", {})

        saved_prompts[slot] = self.prompt_preview.get("1.0", "end-1c")
        saved_titles[slot] = self.prompt_title_var.get().strip()

        self.config["saved_prompts"] = saved_prompts
        self.config["prompt_titles"] = saved_titles

        # Persist the negative half to the same slot in
        # config["negative_prompts"] (queue_manager reads from there).
        if hasattr(self, "negative_prompt_preview"):
            neg_prompts = self.config.setdefault("negative_prompts", {})
            neg_prompts[slot] = self.negative_prompt_preview.get("1.0", "end-1c")
            self.config["negative_prompts"] = neg_prompts

        self._update_prompt_char_count()
        self._exit_edit_mode_internal()  # widgets already saved above
        self._notify_change(f"Saved prompt slot {slot}")

    def _notify_change(self, description: Optional[str] = None):
        """Notify that config has changed."""
        if self.on_config_changed:
            self.on_config_changed(self.config, description)

    def _enter_edit_mode(self):
        """Switch prompt editor to editable mode."""
        self._prompt_edit_mode = True
        self.prompt_title_entry.config(state="normal")
        self.prompt_preview.config(state="normal", fg=COLORS["text_light"])
        if hasattr(self, "negative_prompt_preview"):
            self.negative_prompt_preview.config(
                state="normal", fg=COLORS["text_light"]
            )
        self.save_prompt_btn.config(state="normal")
        self.edit_prompt_btn.config(text="Cancel", command=self._cancel_edit)

    def _cancel_edit(self):
        """Discard edits and return to read-only mode."""
        self._exit_edit_mode_internal()
        # Reload the saved content to discard in-progress changes
        self._update_prompt_preview()

    def _exit_edit_mode_internal(self):
        """Return prompt editor to read-only mode."""
        self._prompt_edit_mode = False
        self.prompt_title_entry.config(state="disabled")
        self.prompt_preview.config(state="disabled", fg=COLORS["text_dim"])
        if hasattr(self, "negative_prompt_preview"):
            self.negative_prompt_preview.config(
                state="disabled", fg=COLORS["text_dim"]
            )
        self.save_prompt_btn.config(state="disabled")
        self.edit_prompt_btn.config(text="Edit", command=self._enter_edit_mode)

    def _position_prompt_preview(self):
        """No-op — prompt editor is now in the right column (pack layout)."""
        pass

    def apply_ui_config(self, ui_config: dict):
        """Apply UI layout configuration to config panel widgets."""
        if not ui_config:
            return
        self._ui_config = ui_config
        config_panel = ui_config.get("config_panel", {})
        try:
            preview_height = int(config_panel.get("prompt_preview_height", 6))
            # Default 10 (NOT 9): the negative-prompt editor is built at
            # (FONT_FAMILY, 10) and apply_ui_config never touched it, so
            # the old default 9 left the positive box one size smaller
            # than the negative — a visible font mismatch the user
            # disliked. They prefer the larger negative font; unify on it.
            preview_font_size = int(config_panel.get("prompt_preview_font_size", 10))
        except (TypeError, ValueError):
            return
        if hasattr(self, "prompt_preview"):
            resolved_height = max(4, preview_height)
            _resolved_font = (FONT_FAMILY, max(6, preview_font_size))
            # Re-derive both height targets up front — apply_ui_config
            # fires ~50ms after launch and the resolved (ui_config)
            # full height IS the "no-negative" target. The split target
            # must stay strictly < full or showing the negative half
            # would GROW the positive box (Codex P2, PR #41).
            self._positive_prompt_full_height = resolved_height
            self._positive_prompt_split_height = max(
                3, min(7, resolved_height - 5)
            )
            # _load_config calls _update_motion_controls BEFORE
            # apply_ui_config fires, which can flip _neg_visible to
            # True at startup (for neg-supporting models). The previous
            # unconditional `height=resolved_height` then snapped the
            # box back to FULL while the negative half was visible —
            # the two halves overlapped visually until the user
            # toggled (Codex P2, PR #41). Honour the current
            # visibility instead.
            _current_target = (
                self._positive_prompt_split_height
                if getattr(self, "_neg_visible", False)
                else self._positive_prompt_full_height
            )
            self.prompt_preview.config(
                height=_current_target,
                font=_resolved_font,
            )
            # Keep the negative editor's font locked to the positive
            # one so the split editor reads as a single coherent box in
            # BOTH states (split + collapsed), regardless of the
            # ui_config size value.
            if hasattr(self, "negative_prompt_preview"):
                self.negative_prompt_preview.config(font=_resolved_font)

    def set_active_prompt_text(self, text: str):
        """Set the text of the active prompt slot (legacy prompt-writer helper).

        Writes into the currently selected slot and persists to config.
        """
        if not hasattr(self, "prompt_preview") or self.prompt_preview is None:
            return
        # Temporarily enter edit mode to allow modification
        was_edit_mode = self._prompt_edit_mode
        slot = str(self.slot_var.get()) if hasattr(self, "slot_var") else "1"

        self.prompt_preview.config(state="normal")
        self.prompt_preview.delete("1.0", tk.END)
        self.prompt_preview.insert("1.0", text)
        if not was_edit_mode:
            self.prompt_preview.config(state="disabled", fg=COLORS["text_dim"])

        # Persist to config
        saved_prompts = self.config.setdefault("saved_prompts", {})
        saved_prompts[slot] = text
        self.config["saved_prompts"] = saved_prompts
        self._update_prompt_char_count()
        if self.on_config_changed:
            self.on_config_changed(self.config, f"Prompt slot {slot} updated from vision analysis")

    def get_config(self) -> dict:
        """Get current configuration."""
        return self.config.copy()

    def cleanup(self):
        """Clean up tkinter variables to prevent thread-related errors on exit.

        This must be called before the root window is destroyed to avoid
        'main thread is not in main loop' errors on Python 3.14+.
        """
        # List all tkinter variable attributes to clean up
        var_attrs = [
            "output_mode_var",
            "output_path_var",
            "model_var",
            "slot_var",
            "prompt_title_var",
            "loop_video_var",
            "oldcam_version_vars",
            "rppg_var",
            "crush_resolution_vars",
            "crush_resolution_checks",
            "aa_attack_vars",
            "aa_attack_checks",
            "aa_info_icon",
            "crush_info_icon",
            "rerun_info_icon",
            "reprocess_var",
            "reprocess_mode_var",
            "verbose_gui_var",
            "rppg_metrics_var",
            "lock_end_frame_var",
            "cfg_scale_var",
            "folder_pattern_var",
            "folder_match_mode_var",
            "duration_var",
            "aspect_ratio_var",
            "resolution_var",
            "seed_var",
            "random_seed_var",
            "camera_fixed_var",
            "generate_audio_var",
        ]
        for attr in var_attrs:
            if hasattr(self, attr):
                try:
                    delattr(self, attr)
                except Exception:
                    pass

    def update_parameter_visibility(self, model_endpoint: str):
        """Update visibility of parameter controls based on model capabilities.

        Uses ModelSchemaManager to determine which parameters the selected model
        supports. Controls for unsupported parameters are visually disabled with
        grayed-out styling to indicate they won't be sent to the API.

        Args:
            model_endpoint: The fal.ai model endpoint (e.g., "fal-ai/kling-video/v2.5/pro/image-to-video")
        """
        # Map UI controls to their corresponding API parameter names
        # Format: param_name -> (controls_tuple, associated_labels_tuple)
        param_controls = {
            "seed": (
                (self.seed_entry, self.random_seed_checkbox),
                (),  # No additional labels - "Seed:" label is always visible
            ),
            "aspect_ratio": (
                (self.aspect_ratio_combo,),
                (),  # "Aspect:" label handled separately in row
            ),
            "resolution": (
                (self.resolution_combo,),
                (),  # "Resolution:" label handled separately in row
            ),
            "camera_fixed": ((self.camera_fixed_checkbox,), ()),
            "generate_audio": ((self.generate_audio_checkbox,), ()),
        }

        try:
            from model_schema_manager import ModelSchemaManager

            # Saved config first, then any fal env alias (FAL_KEY / FAL_API_KEY)
            # — bare os.getenv("FAL_KEY") missed users who store FAL_API_KEY
            # (code-review Codex P2 #73).
            from api_keys import resolve_api_key
            api_key = resolve_api_key(self.config, "falai_api_key")
            if not api_key:
                logger.warning("No API key available for schema lookup")
                # Apply conservative default: enable all controls with unknown status
                param_controls = {
                    "seed": ((self.seed_entry, self.random_seed_checkbox), ()),
                    "aspect_ratio": ((self.aspect_ratio_combo,), ()),
                    "resolution": ((self.resolution_combo,), ()),
                    "camera_fixed": ((self.camera_fixed_checkbox,), ()),
                    "generate_audio": ((self.generate_audio_checkbox,), ()),
                }

                for param_name, (controls, labels) in param_controls.items():
                    for control in controls:
                        if control is None:
                            continue
                        # The resolution combo's enabled state is OWNED by
                        # _sync_resolution_to_model (per-model options); don't
                        # re-enable it here when that method disabled it.
                        if (
                            control is self.resolution_combo
                            and not getattr(self, "_resolution_model_aware", True)
                        ):
                            continue
                        try:
                            if isinstance(control, ttk.Combobox):
                                control.config(state="readonly")
                            elif isinstance(control, tk.Entry):
                                control.config(state="normal")
                            elif isinstance(control, tk.Checkbutton):
                                control.config(state="normal")
                        except tk.TclError as e:
                            logger.debug(f"Could not reset {param_name} control: {e}")

                # Update info label
                if (
                    hasattr(self, "video_settings_info")
                    and self.video_settings_info is not None
                ):
                    try:
                        self.video_settings_info.config(
                            text="(model-dependent)",
                            fg=COLORS["text_dim"],
                        )
                    except tk.TclError as e:
                        logger.debug(f"Could not update video_settings_info: {e}")
                return

            schema_manager = ModelSchemaManager(api_key)

            # Get all supported parameters for this model (defensive handling)
            supported_params = set(
                schema_manager.get_supported_parameters(model_endpoint) or []
            )

            # Visual styling for supported vs unsupported
            SUPPORTED_FG = COLORS["text_light"]
            UNSUPPORTED_FG = COLORS["text_unsupported"]
            SUPPORTED_BG = COLORS["bg_main"]
            UNSUPPORTED_BG = COLORS["bg_unsupported"]

            # Get duration options from schema
            duration_param = schema_manager.get_parameter_info(model_endpoint, "duration")
            # Some live schemas (e.g. Seedance 2.0) list non-numeric duration
            # enum values like "auto" next to the numeric seconds. The dropdown
            # and the downstream cost/payload logic are numeric-only, so keep
            # just the integer options here -- int("auto") would otherwise raise
            # ValueError and get mislabeled as a "Schema fetch failed" error.
            numeric_durations = []
            if duration_param and getattr(duration_param, 'enum', None):
                for _d in duration_param.enum:
                    try:
                        numeric_durations.append(int(_d))
                    except (TypeError, ValueError):
                        continue
                numeric_durations.sort()
            if numeric_durations:
                duration_values = [f"{d}s" for d in numeric_durations]
                logger.debug(f"Model {model_endpoint} supports durations: {duration_values}")
            else:
                # Fallback: use model_metadata.py duration_options
                from model_metadata import get_duration_options
                duration_secs = get_duration_options(model_endpoint)
                duration_values = [f"{d}s" for d in duration_secs]
                logger.debug(f"Using metadata durations for {model_endpoint}: {duration_values}")

            # Update duration dropdown with model-specific values
            if hasattr(self, 'duration_combo') and self.duration_combo is not None:
                try:
                    current_value = self.duration_var.get()
                    self.duration_combo.config(values=duration_values)

                    # Preserve selection if valid, else reset to first option
                    if current_value not in duration_values:
                        new_default = duration_values[0] if duration_values else "10s"
                        self.duration_var.set(new_default)
                        logger.info(f"Duration reset to {new_default} (was {current_value})")
                except tk.TclError as e:
                    logger.debug(f"Could not update duration dropdown: {e}")

            # Update aspect ratio dropdown from schema
            ar_param = schema_manager.get_parameter_info(model_endpoint, "aspect_ratio")
            if ar_param and hasattr(ar_param, 'enum') and ar_param.enum:
                ar_values = list(ar_param.enum)
                if hasattr(self, 'aspect_ratio_combo') and self.aspect_ratio_combo is not None:
                    try:
                        current_ar = self.aspect_ratio_var.get()
                        self.aspect_ratio_combo.config(values=ar_values)
                        if current_ar not in ar_values:
                            default_ar = str(ar_param.default) if ar_param.default else (ar_values[0] if ar_values else "16:9")
                            self.aspect_ratio_var.set(default_ar)
                            logger.debug(f"Aspect ratio reset to {default_ar}")
                    except tk.TclError as e:
                        logger.debug(f"Could not update aspect ratio dropdown: {e}")

            # Update resolution dropdown from schema
            res_param = schema_manager.get_parameter_info(model_endpoint, "resolution")
            if res_param and hasattr(res_param, 'enum') and res_param.enum:
                res_values = list(res_param.enum)
                if hasattr(self, 'resolution_combo') and self.resolution_combo is not None:
                    try:
                        current_res = self.resolution_var.get()
                        self.resolution_combo.config(values=res_values)
                        if current_res not in res_values:
                            default_res = str(res_param.default) if res_param.default else (res_values[0] if res_values else "720p")
                            self.resolution_var.set(default_res)
                            logger.debug(f"Resolution reset to {default_res}")
                    except tk.TclError as e:
                        logger.debug(f"Could not update resolution dropdown: {e}")

            for param_name, (controls, labels) in param_controls.items():
                supported = param_name in supported_params
                state = "normal" if supported else "disabled"
                fg_color = SUPPORTED_FG if supported else UNSUPPORTED_FG
                bg_color = SUPPORTED_BG if supported else UNSUPPORTED_BG

                for control in controls:
                    if control is None:
                        continue
                    # The resolution combo's enabled state is OWNED by
                    # _sync_resolution_to_model; skip it here when it was
                    # disabled for a model with no selectable resolution.
                    if (
                        control is self.resolution_combo
                        and not getattr(self, "_resolution_model_aware", True)
                    ):
                        continue
                    try:
                        # Handle different widget types
                        if isinstance(control, ttk.Combobox):
                            # Always readonly (clickable) but visually dimmed when unsupported
                            control.config(state="readonly")

                            # Apply visual feedback via foreground color
                            # Note: ttk.Combobox styling is limited, but we can try
                            try:
                                if not supported:
                                    # Try to dim the text (may not work on all platforms)
                                    control.configure(foreground=UNSUPPORTED_FG)
                                else:
                                    control.configure(foreground=SUPPORTED_FG)
                            except Exception:
                                # Some ttk themes don't support foreground
                                pass
                        elif isinstance(control, tk.Entry):
                            control.config(
                                state=state,
                                fg=fg_color,
                                bg=bg_color if state == "normal" else UNSUPPORTED_BG,
                                disabledforeground=UNSUPPORTED_FG,
                                disabledbackground=UNSUPPORTED_BG,
                            )
                        elif isinstance(control, tk.Checkbutton):
                            control.config(
                                state=state,
                                fg=fg_color,
                                disabledforeground=UNSUPPORTED_FG,
                            )
                        else:
                            # Generic fallback
                            control.config(state=state)
                    except tk.TclError as e:
                        logger.debug(f"Could not configure {param_name} control: {e}")

                # Update associated labels
                for label in labels:
                    if label is not None:
                        try:
                            label.config(fg=fg_color)
                        except tk.TclError as e:
                            logger.debug(f"Could not configure {param_name} label: {e}")

            # Update info label to show model capability status
            if hasattr(self, "video_settings_info"):
                key_params = {
                    "seed",
                    "aspect_ratio",
                    "resolution",
                    "camera_fixed",
                    "generate_audio",
                }
                supported_count = len(key_params & supported_params)

                if supported_count == len(key_params):
                    status_text = "All params supported"
                    status_color = COLORS["success"]
                elif supported_count == 0:
                    status_text = "Limited params"
                    status_color = COLORS["warning"]
                else:
                    status_text = f"{supported_count}/{len(key_params)} params"
                    status_color = COLORS["text_dim"]

                self.video_settings_info.config(
                    text=f"({status_text})", fg=status_color
                )

            # Show parameter support status in diagnostic label (for debugging)
            if hasattr(self, "schema_diagnostic_label"):
                param_icons = []
                for param in ["duration", "aspect_ratio", "resolution", "seed", "camera_fixed"]:
                    icon = "✓" if param in supported_params else "✗"
                    param_icons.append(f"{icon}{param[:3]}")
                self.schema_diagnostic_label.config(text=" | ".join(param_icons))

            logger.debug(
                f"Updated parameter visibility for {model_endpoint}: {len(supported_params)} supported"
            )

        except Exception as e:
            logger.error(f"Failed to update parameter visibility: {e}")
            import traceback
            logger.error(f"Traceback: {traceback.format_exc()}")

            # Show error to user in GUI
            if hasattr(self, "schema_diagnostic_label"):
                self.schema_diagnostic_label.config(
                    text="⚠ Schema fetch failed - check logs",
                    fg=COLORS.get("error", "#FF6464")
                )
