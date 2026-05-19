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
from .theme import apply_macos_button_fix

try:
    from tkinterdnd2 import DND_FILES as _DND_FILES
    _HAS_DND = True
except ImportError:
    _DND_FILES = None
    _HAS_DND = False

if os.getenv("SELFIEGEN_MAC_DISABLE_DND", "0") == "1":
    _HAS_DND = False


# Color palette
COLORS = {
    "bg_main": "#2D2D30",
    "bg_panel": "#3C3C41",
    "bg_input": "#464649",
    "text_light": "#DCDCDC",
    "text_dim": "#B4B4B4",
    "accent_blue": "#6496FF",
    "border": "#5A5A5E",
    "warning": "#FFB347",
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

# UI Configuration
COMBOBOX_DROPDOWN_HEIGHT = 25  # Number of items visible in dropdown (default ~10)

# Module logger
logger = logging.getLogger(__name__)


class HoverTooltip:
    """Dark-themed floating tooltip shown when hovering a widget."""

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
        wh = self._widget.winfo_height()
        sh = self._widget.winfo_screenheight()

        x = max(0, wx - tip_w + self._widget.winfo_width())
        y = wy + wh + 4
        if y + tip_h > sh - 40:
            y = wy - tip_h - 4  # flip above if near bottom of screen

        self._tip.wm_geometry(f"+{x}+{y}")

    def _hide(self, event=None):
        if self._tip:
            self._tip.destroy()
            self._tip = None


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
        """Merge factory models (minus hidden) with custom models."""
        hidden = set(config.get("hidden_models", []))
        custom = config.get("custom_models", [])
        factory = [m for m in MODEL_METADATA if m.get("endpoint") not in hidden]
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
            bg=COLORS["bg_input"], fg=COLORS["text_dim"],
        )
        self.model_info_icon.pack(side=tk.RIGHT, padx=(6, 0))
        HoverTooltip(self.model_info_icon, self._get_current_model_notes)

        # "Manage…" button — opens the Model Manager dialog
        self.manage_models_btn = tk.Button(
            row1, text="Manage\u2026", font=(FONT_FAMILY, 9, "bold"),
            bg=COLORS["bg_panel"], fg=COLORS["text_light"],
            activebackground=COLORS["bg_main"], activeforeground=COLORS["text_light"],
            padx=8, pady=2, relief=tk.FLAT, borderwidth=0, command=self._open_model_manager,
        )
        self.manage_models_btn.pack(side=tk.RIGHT, padx=(4, 0))
        apply_macos_button_fix(self.manage_models_btn)

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

        self.browse_btn = tk.Button(
            row2, text="BROWSE", font=(FONT_FAMILY, 9, "bold"),
            bg=COLORS["bg_panel"], fg=COLORS["text_light"],
            padx=10, relief=tk.FLAT, borderwidth=0, command=self._browse_output_folder,
        )
        self.browse_btn.pack(side=tk.LEFT, padx=2)
        apply_macos_button_fix(self.browse_btn)

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
        # vertical stack: oldcam (top) over rPPG (below), equal width
        _pp_stack = tk.Frame(rA, bg=COLORS["bg_input"])
        _pp_stack.pack(side=tk.LEFT, padx=(8, 0), fill=tk.X, expand=True)
        self.oldcam_controls_frame = tk.Frame(
            _pp_stack,
            bg="#2A1F34",
            highlightthickness=1,
            highlightbackground="#5E3A7D",
            bd=0,
            padx=6,
            pady=2,
        )
        self.oldcam_controls_frame.pack(fill=tk.X, expand=True, pady=(0, 4))
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
            fg=COLORS["text_dim"],
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
        self.rppg_controls_frame.pack(fill=tk.X, expand=True)
        _rppg_label_row = tk.Frame(self.rppg_controls_frame, bg="#3A2A1F")
        _rppg_label_row.pack(side=tk.LEFT, anchor="n", padx=(0, 6))
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
            fg=COLORS["text_dim"],
        )
        self.rppg_info_icon.pack(side=tk.LEFT, padx=(4, 0))
        HoverTooltip(
            self.rppg_info_icon,
            lambda: (
                "Sub-perceptual rPPG pulse injection (runs LAST,\n"
                "after Loop + Oldcam). Aims to give Persona's\n"
                "passive rPPG stage a physiologically-correct\n"
                "signal. Untested forward direction — off by\n"
                "default. Skips gracefully if the rPPG tool is\n"
                "absent or injection fails (keeps the pre-rPPG\n"
                "video; never crashes the run)."
            ),
        )
        self.rppg_var = tk.BooleanVar(value=False)
        self.rppg_checkbox = tk.Checkbutton(
            self.rppg_controls_frame,
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
        self.rppg_checkbox.pack(side=tk.LEFT, anchor="n", padx=(2, 8))
        tk.Label(
            self.rppg_controls_frame,
            text="(sub-perceptual · runs last · off = unchanged)",
            font=(FONT_FAMILY, 8),
            bg="#3A2A1F",
            fg=COLORS["text_dim"],
        ).pack(side=tk.LEFT, anchor="n", padx=(0, 4))

        # ONE shared Re-Run column, packed into the band (rA) to the
        # RIGHT of the Oldcam/rPPG stack — applies to whatever is
        # selected: any Oldcam versions AND/OR rPPG, and re-loops first
        # when Loop Video is on (queue_manager.rerun_oldcam_only already
        # handles loop → oldcam → rPPG ordering). Top-anchored so it
        # aligns with the top of the stack, not its vertical centre.
        _shared_rerun_col = tk.Frame(rA, bg=COLORS["bg_input"])
        _shared_rerun_col.pack(side=tk.LEFT, anchor="n", padx=(12, 0))
        tk.Label(
            _shared_rerun_col,
            text="Re-Run:",
            font=(FONT_FAMILY, 10),
            bg=COLORS["bg_input"],
            fg=COLORS["text_light"],
        ).pack(anchor="w")
        _shared_rerun_btn_row = tk.Frame(_shared_rerun_col, bg=COLORS["bg_input"])
        _shared_rerun_btn_row.pack(anchor="w", pady=(2, 0))
        self.oldcam_rerun_btn = tk.Button(
            _shared_rerun_btn_row,
            text="↻",
            font=(FONT_FAMILY, 9, "bold"),
            bg=COLORS["bg_panel"],
            fg=COLORS["text_light"],
            activebackground=COLORS["bg_main"],
            activeforeground=COLORS["text_light"],
            width=2,
            padx=8,
            pady=2,
            relief=tk.FLAT,
            borderwidth=0,
            command=self._on_oldcam_rerun_clicked,
        )
        self.oldcam_rerun_btn.pack(side=tk.LEFT, padx=(0, 4))
        apply_macos_button_fix(self.oldcam_rerun_btn)
        HoverTooltip(
            self.oldcam_rerun_btn,
            lambda: (
                "Re-run post-processing on a generated video.\n"
                "Applies whatever is selected — Oldcam version(s)\n"
                "and/or rPPG — and re-loops first if Loop Video is\n"
                "on. Uses the selected Processed Videos row, or the\n"
                "latest completed output if none selected."
            ),
        )
        self.oldcam_pick_btn = tk.Button(
            _shared_rerun_btn_row,
            text="📂",
            font=(FONT_FAMILY, 9),
            bg=COLORS["bg_panel"],
            fg=COLORS["text_light"],
            activebackground=COLORS["bg_main"],
            activeforeground=COLORS["text_light"],
            width=2,
            padx=8,
            pady=2,
            relief=tk.FLAT,
            borderwidth=0,
            command=self._on_oldcam_pick_rerun_clicked,
        )
        self.oldcam_pick_btn.pack(side=tk.LEFT, padx=(0, 0))
        apply_macos_button_fix(self.oldcam_pick_btn)
        HoverTooltip(
            self.oldcam_pick_btn,
            lambda: (
                "Pick a video file and re-run post-processing on it.\n"
                "Applies whatever is selected — Oldcam version(s)\n"
                "and/or rPPG — and re-loops first if Loop Video is\n"
                "on. Respects Allow reprocessing / Increment."
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
        rB.pack(fill=tk.X, pady=(0, 4))
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
            rB, text="(requires FFmpeg)", font=(FONT_FAMILY, 8),
            bg=COLORS["bg_input"], fg=COLORS["text_dim"],
        )
        self.loop_info_label.pack(side=tk.LEFT, padx=4)

        # Logging
        rC = tk.Frame(left_col, bg=COLORS["bg_input"])
        rC.pack(fill=tk.X, pady=(0, 4))
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
            rC, text="(detailed processing log)", font=(FONT_FAMILY, 8),
            bg=COLORS["bg_input"], fg=COLORS["text_dim"],
        )
        self.verbose_info_label.pack(side=tk.LEFT, padx=4)

        # rPPG metric-suffix toggle (shares the Logging: row). When OFF
        # (default) the injector's "{stem}-rppg - <SNR>-<Phase>-..." name
        # is stripped to a clean "{stem}-rppg" and the metrics go to a
        # .metrics.json sidecar (automation/rppg.finalize_rppg_output).
        self.rppg_metrics_var = tk.BooleanVar(value=False)
        self.rppg_metrics_checkbox = tk.Checkbutton(
            rC, text="rPPG metrics in filename", variable=self.rppg_metrics_var,
            font=(FONT_FAMILY, 10), bg=COLORS["bg_input"], fg=COLORS["text_light"],
            selectcolor=COLORS["bg_main"], activebackground=COLORS["bg_input"],
            activeforeground=COLORS["text_light"],
            command=self._on_rppg_metrics_changed,
        )
        self.rppg_metrics_checkbox.pack(side=tk.LEFT, padx=(12, 0))
        self.rppg_metrics_info_label = tk.Label(
            rC, text="(off = clean name + .metrics.json sidecar)",
            font=(FONT_FAMILY, 8),
            bg=COLORS["bg_input"], fg=COLORS["text_dim"],
        )
        self.rppg_metrics_info_label.pack(side=tk.LEFT, padx=4)

        # File Filter — replaces the old "Folder:" row with clearer labeling
        rD = tk.Frame(left_col, bg=COLORS["bg_input"])
        rD.pack(fill=tk.X, pady=(0, 2))
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
        # Filter help text — explains what Folder/Match do
        rD2 = tk.Frame(left_col, bg=COLORS["bg_input"])
        rD2.pack(fill=tk.X, pady=(0, 6))
        tk.Label(rD2, text="", bg=COLORS["bg_input"], width=lbl_w).pack(side=tk.LEFT)
        tk.Label(
            rD2,
            text="Subfolder name to match when processing a folder (blank \u2192 all files)"
                 " \u00b7 Partial: name contains filter \u00b7 Exact: name equals filter",
            font=(FONT_FAMILY, 8), bg=COLORS["bg_input"], fg=COLORS["text_dim"],
            wraplength=280, justify="left",
        ).pack(side=tk.LEFT)

        # Video settings
        rE = tk.Frame(left_col, bg=COLORS["bg_input"])
        rE.pack(fill=tk.X, pady=(0, 4))
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
        self.schema_diagnostic_label = tk.Label(
            rE, text="", font=(FONT_FAMILY, 9), bg=COLORS["bg_input"], fg=COLORS["text_dim"],
        )
        self.schema_diagnostic_label.pack(side=tk.LEFT, padx=2)
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
        rEF.pack(fill=tk.X, pady=(0, 4))
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
        self.cfg_scale_label = tk.Label(
            rEF, text="cfg:", font=(FONT_FAMILY, 9),
            bg=COLORS["bg_input"], fg=COLORS["text_dim"],
        )
        self.cfg_scale_label.pack(side=tk.LEFT, padx=(0, 3))
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
            rEF, text="", font=(FONT_FAMILY, 8),
            bg=COLORS["bg_input"], fg=COLORS["text_dim"],
        )
        self.model_caps_label.pack(side=tk.RIGHT, padx=4)

        # Seed & misc options
        rF = tk.Frame(left_col, bg=COLORS["bg_input"])
        rF.pack(fill=tk.X, pady=(0, 4))
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
            font=(FONT_FAMILY, 8, "bold"),
            bg=COLORS["bg_panel"], fg=COLORS["text_dim"], anchor="w",
        ).pack(fill=tk.X, pady=(0, 2))
        neg_text_frame = tk.Frame(
            self._negative_prompt_section, bg=COLORS["bg_panel"]
        )
        neg_text_frame.pack(fill=tk.BOTH, expand=True)
        self.negative_prompt_preview = tk.Text(
            neg_text_frame, font=(FONT_FAMILY, 10),
            bg=COLORS["bg_main"], fg=COLORS["text_dim"],
            height=5, wrap=tk.WORD, relief=tk.FLAT, borderwidth=0,
            insertbackground=COLORS["text_light"], state="disabled",
            disabledforeground=COLORS["text_dim"],
            disabledbackground=COLORS["bg_main"],
        )
        neg_scroll = ttk.Scrollbar(
            neg_text_frame, orient=tk.VERTICAL,
            command=self.negative_prompt_preview.yview,
        )
        self.negative_prompt_preview.configure(yscrollcommand=neg_scroll.set)
        neg_scroll.pack(side=tk.RIGHT, fill=tk.Y)
        self.negative_prompt_preview.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        # Shrink the positive box so the split reads as roughly two
        # halves rather than the negative being a thin afterthought.
        self.prompt_preview.config(height=7)
        # Visibility is set on first model-change; default-hide until then.
        self._negative_prompt_section.pack_forget()

        # Footer: char count badge + Edit button + Save button
        prompt_footer = tk.Frame(right_inner, bg=COLORS["bg_panel"])
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
        self.edit_prompt_btn = tk.Button(
            prompt_footer, text="Edit", font=(FONT_FAMILY, 9, "bold"),
            bg=COLORS["bg_input"], fg=COLORS["text_light"],
            activebackground=COLORS["bg_main"], activeforeground=COLORS["text_light"],
            padx=10, pady=2, relief=tk.FLAT, borderwidth=0,
            command=self._enter_edit_mode,
        )
        self.edit_prompt_btn.pack(side=tk.RIGHT, padx=(4, 0))
        apply_macos_button_fix(self.edit_prompt_btn)
        self.save_prompt_btn = tk.Button(
            prompt_footer, text="Save Prompt", font=(FONT_FAMILY, 9, "bold"),
            bg=COLORS["bg_input"], fg=COLORS["text_light"],
            activebackground=COLORS["bg_main"], activeforeground=COLORS["text_light"],
            padx=10, pady=2, relief=tk.FLAT, borderwidth=0,
            state="disabled", command=self._save_prompt,
        )
        self.save_prompt_btn.pack(side=tk.RIGHT)
        apply_macos_button_fix(self.save_prompt_btn)

        # Load prompt config now that widgets exist
        self._load_prompt_config()

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

        inner = tk.Frame(outer, bg=_bg, cursor="hand2")
        inner.pack(fill=tk.BOTH, expand=True)

        # Centered content (like original drop_zone.py)
        center = tk.Frame(inner, bg=_bg, cursor="hand2")
        center.place(relx=0.5, rely=0.5, anchor="center")

        lbl_icon = tk.Label(
            center, text="\U0001f4e5", font=(EMOJI_FONT_FAMILY, 28),
            bg=_bg, fg=COLORS["accent_blue"], cursor="hand2",
        )
        lbl_icon.pack(pady=(0, 4))

        lbl_main = tk.Label(
            center, text="DROP IMAGES HERE",
            font=(FONT_FAMILY, 11, "bold"),
            bg=_bg, fg=COLORS["text_light"], cursor="hand2",
        )
        lbl_main.pack(pady=1)

        lbl_sub = tk.Label(
            center, text="or click to browse",
            font=(FONT_FAMILY, 9),
            bg=_bg, fg=COLORS["text_dim"], cursor="hand2",
        )
        lbl_sub.pack(pady=(0, 2))

        self._mini_dz_status = tk.Label(
            center, text="", font=(FONT_FAMILY, 9, "bold"),
            bg=_bg, fg=COLORS["success"], cursor="hand2",
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

        # DnD registration
        if _HAS_DND and _DND_FILES:
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
                except Exception:
                    pass

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
                    "name": self.config.get("model_display_name", "Kling 2.5 Turbo Pro"),
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
        self._check_ffmpeg_status()
        selected_versions = self._resolve_oldcam_versions_from_config()
        for version, var in self.oldcam_version_vars.items():
            var.set(version in selected_versions)
        self.config["oldcam_versions"] = selected_versions
        self.config["oldcam_version"] = selected_versions[-1] if selected_versions else "v24"

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
        self.lock_end_frame_var.set(
            bool(_parse_bool(self.config.get("lock_end_frame", True)))
        )
        try:
            _cfg = float(self.config.get("cfg_scale_value", 0.7))
        except (TypeError, ValueError):
            _cfg = 0.7
        self.cfg_scale_var.set(f"{max(0.0, min(1.0, _cfg)):g}")

        # Folder filter options
        self.folder_pattern_var.set(self.config.get("folder_filter_pattern", ""))
        self.folder_match_mode_var.set(self.config.get("folder_match_mode", "partial"))

        # Advanced video settings
        self.aspect_ratio_var.set(self.config.get("aspect_ratio", "9:16"))
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

        self.update_parameter_visibility(model_endpoint)
        self._update_motion_controls(model_endpoint)

        duration_text = self.duration_var.get().rstrip("s").strip()
        if duration_text.isdigit():
            self.config["video_duration"] = int(duration_text)

        self._update_model_info_icon(selected_model)
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

        # Pricing (from API)
        pricing = model.get("pricing_info", {})
        if pricing:
            unit = pricing.get("unit", "")
            price = pricing.get("unit_price", 0)
            if price:
                if unit == "second":
                    price_str = f"${price:.3f}/second (${price * 5:.2f}/5s, ${price * 10:.2f}/10s)"
                elif unit == "video":
                    price_str = f"${price:.2f}/video (flat rate)"
                elif unit == "image":
                    price_str = f"${price:.2f}/image"
                else:
                    price_str = f"${price:.3f}/{unit}" if unit else f"${price:.3f}"
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
        """Return version comparison tooltip for the Oldcam (ⓘ) icon."""
        lines = [
            "─── Oldcam Version Comparison ───",
            "",
            "v7   Modern phone imperfection",
            "     JPEG cycle, arm-sway rolling shutter, AF hunting. No face tracking.",
            "     Trade-off: too subtle — looked nearly identical to source.",
            "",
            "v8   Hardware physics upgrade",
            "     Spring-damper OIS, 3D channel noise, AWB drift, hard bitrate cap.",
            "     Trade-off: bitrate cap over-compressed and lost detail.",
            "",
            "v9   Face-aware portrait pass",
            "     MediaPipe FaceLandmarker, 4-region masks, AWB drift, soft background.",
            "     Trade-off: background blur read as fake depth-of-field.",
            "",
            "v10  rPPG biological sync",
            "     FFT on green channel → phase-locked color pulse in 4 face regions.",
            "     Trade-off: visible color siren; AWB removed to keep FFT signal clean.",
            "",
            "v11  Best-of-all combination",
            "     V10 pulse + V9 AWB, applied AFTER the FFT read.",
            "     Trade-off: 2D rPPG flagged by modern PAD; global LUT tints sepia.",
            "",
            "v12  Pristine hardware-only (anti-spoofing aware)",
            "     No rPPG, no LUT, no CLAHE, no HSV. Pure OIS / AE / noise / vignette.",
            "     Best for low-light realism; preserves Kling's color fidelity.",
            "",
            "v13  High-end daylight (pristine optics)",
            "     No sensor noise, no AE hunting, no ghosting, no MediaPipe.",
            "     Pure OIS / rolling shutter / blooming / AWB drift / aberration / vignette.",
            "     Trade-off: scalar-add AWB, static pixels, double-lossy encode (V14 fixes these).",
            "",
            "v14  Forensic daylight (physics-corrected)",
            "     V13 optics + true multiplicative AWB, sub-perceptual sensor floor,",
            "     smoothstep bloom, lossless temp encode, audio-preserving.",
            "     Trade-off: the preserved sensor floor is a frequency-detector tell",
            "     (Resemble testing) — V15 removes it and restores ghosting.",
            "",
            "v15  Temporal Mute (synthesis)",
            "     V14 math/encoding + V13 noise-free philosophy + V12 temporal blend.",
            "     No sensor noise; --ghosting 0.18 bleeds the previous frame to",
            "     defeat consistency detectors. Superseded as default by v24.",
            "",
            "v24  Crush Laundromat (synthesis)   ★ default",
            "     V15 + a uniform resolution round-trip (downscale x0.40 ->",
            "     Lanczos upscale + light unsharp) before the encode. Destroys",
            "     the AI fingerprint uniformly; ~9x better Resemble-API score",
            "     than v15 while staying visually sharp. Best result.",
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
        self._notify_change(f"Loop video {status}")

    def _on_rppg_changed(self):
        """Handle rPPG injection checkbox change."""
        self.config["rppg_enabled"] = self.rppg_var.get()
        status = "enabled" if self.rppg_var.get() else "disabled"
        self._notify_change(f"rPPG injection {status}")

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
        if selected_versions:
            self._notify_change("Oldcam versions set to " + ", ".join(selected_versions))
        else:
            self._notify_change("Oldcam disabled (no versions selected)")

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
            from model_metadata import get_model_capabilities

            caps = get_model_capabilities(model_endpoint)
        except Exception:
            return  # never break the model-change flow over a label

        has_end = caps.get("end_image_param") is not None
        has_cfg = bool(caps.get("supports_cfg_scale"))
        has_neg = bool(caps.get("supports_negative_prompt"))

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

        The widgets are created once (see the prompt-editor build); here
        we only pack_forget / re-pack the negative section so its text
        survives toggling. No-op until the split editor exists."""
        neg = getattr(self, "_negative_prompt_section", None)
        if neg is None:
            return
        try:
            if has_neg:
                if not neg.winfo_ismapped():
                    neg.pack(fill=tk.BOTH, expand=True, pady=(4, 0))
            else:
                if neg.winfo_ismapped():
                    neg.pack_forget()
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
        self._notify_change(f"Resolution set to {resolution}")

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
            preview_font_size = int(config_panel.get("prompt_preview_font_size", 9))
        except (TypeError, ValueError):
            return
        if hasattr(self, "prompt_preview"):
            self.prompt_preview.config(
                height=max(4, preview_height),
                font=(FONT_FAMILY, max(6, preview_font_size)),
            )

    def set_active_prompt_text(self, text: str):
        """Set the text of the active prompt slot (called by PrepTab vision analysis).

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

            api_key = os.getenv("FAL_KEY") or self.config.get("falai_api_key", "")
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
            if duration_param and hasattr(duration_param, 'enum') and duration_param.enum:
                # Model specifies exact allowed durations
                duration_values = [f"{int(d)}s" for d in duration_param.enum]
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
