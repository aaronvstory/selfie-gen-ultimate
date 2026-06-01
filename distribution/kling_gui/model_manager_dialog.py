"""
Model Manager Dialog - Add, edit, test, browse, hide/restore models from the GUI.

Manages two config keys:
  - custom_models: list of user-added model dicts (each has "custom": True)
  - hidden_models: list of endpoint strings for factory models the user hid
"""

import tkinter as tk
from tkinter import ttk, messagebox
import threading
import logging
from typing import List, Dict, Optional, Callable

from .theme import (
    COLORS,
    FONT_FAMILY,
    TTK_BTN_DANGER,
    TTK_BTN_SECONDARY,
    TTK_BTN_SUCCESS,
    macos_widget_pad,
)
from model_metadata import MODEL_METADATA, get_model_display_name

logger = logging.getLogger(__name__)

# Default duration options for the add-model form
_DURATION_CHOICES = [5, 10]


def _sanitize_release_date(raw: str) -> str:
    """Convert ISO date string to 'Mon YYYY' (e.g., '2026-02-15' -> 'Feb 2026').

    Already-friendly strings like 'Feb 2026' or '2024' pass through unchanged.
    Handles fractional seconds (e.g. '.6847') and full ISO-8601 timestamps.
    """
    if not raw:
        return ""
    import re
    from datetime import datetime
    # Strip fractional seconds (e.g. .6847) before parsing
    cleaned = re.sub(r'\.\d+', '', raw.strip())
    for fmt in ("%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d", "%Y-%m"):
        try:
            dt = datetime.strptime(cleaned, fmt)
            return dt.strftime("%b %Y")
        except ValueError:
            continue
    # If it's already "Feb 2026" or "2024", return as-is
    return raw.strip()


def _format_short_price(pricing_info: dict) -> str:
    """Format pricing_info dict into a short string like '$2.24/10s'."""
    if not pricing_info:
        return ""
    price = pricing_info.get("unit_price", 0)
    if not price:
        return ""
    unit = pricing_info.get("unit", "")
    if unit == "second":
        return f"${price * 10:.2f}/10s"
    elif unit == "compute second":
        return f"${price * 10:.2f}/10s"
    elif unit == "video":
        return f"${price:.2f}/video"
    elif unit == "image":
        return f"${price:.2f}/image"
    elif unit:
        return f"${price:.2f}/{unit}"
    return f"${price:.2f}"


class ModelManagerDialog(tk.Toplevel):
    """Modal dialog for managing the model list (factory + custom)."""

    def __init__(
        self,
        parent: tk.Widget,
        config: dict,
        api_key: str,
        on_save: Optional[Callable[[], None]] = None,
    ):
        super().__init__(parent)
        self.title("Model Manager")
        self.configure(bg=COLORS["bg_main"])
        self.resizable(True, True)
        self.minsize(820, 540)
        self.geometry("960x720")

        self._config = config
        self._api_key = api_key
        self._on_save = on_save

        # Work on local copies — committed to config only on Save
        self._custom_models: List[Dict] = [
            dict(m) for m in config.get("custom_models", [])
        ]
        self._hidden_endpoints: List[str] = list(
            config.get("hidden_models", [])
        )

        # Combined model list (rebuilt on any change)
        self._all_models: List[Dict] = []
        self._health_cache: Dict[str, Optional[bool]] = {}  # endpoint -> True/False/None

        # Edit mode tracking
        self._edit_mode = False
        self._edit_index: Optional[int] = None

        # Browse state
        self._browse_visible = False
        self._browse_models: List[Dict] = []

        # Fetch All state
        self._fetch_cancel = False
        self._fetch_thread: Optional[threading.Thread] = None

        self._build_ui()
        self._rebuild_model_list()

        # Load persisted enrichment data and merge onto model dicts
        self._load_enrichment_data()

        # Modal behavior
        self.transient(parent)
        self.grab_set()
        self.protocol("WM_DELETE_WINDOW", self._on_cancel)
        self.focus_set()

    # ------------------------------------------------------------------
    # UI Construction
    # ------------------------------------------------------------------

    def _build_ui(self):
        # Main horizontal split: left (list) | right (add/edit form)
        body = tk.Frame(self, bg=COLORS["bg_main"])
        body.pack(fill=tk.BOTH, expand=True, padx=12, pady=(12, 0))

        self._build_left_panel(body)
        self._build_right_panel(body)
        self._build_footer()

    def _build_left_panel(self, parent: tk.Frame):
        """Left panel: model listbox + action buttons + browse pane."""
        self._left_frame = tk.LabelFrame(
            parent, text="  Models  ", font=(FONT_FAMILY, 10, "bold"),
            bg=COLORS["bg_panel"], fg=COLORS["text_light"],
            highlightbackground=COLORS["border"], highlightthickness=1,
        )
        self._left_frame.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, padx=(0, 6))

        # Listbox with scrollbar
        list_frame = tk.Frame(self._left_frame, bg=COLORS["bg_panel"])
        list_frame.pack(fill=tk.BOTH, expand=True, padx=8, pady=(4, 4))

        scrollbar = ttk.Scrollbar(list_frame, orient=tk.VERTICAL)
        self._listbox = tk.Listbox(
            list_frame, font=(FONT_FAMILY, 10),
            bg=COLORS["bg_main"], fg=COLORS["text_light"],
            selectbackground=COLORS["accent_blue"], selectforeground="#FFFFFF",
            activestyle="none", borderwidth=0, highlightthickness=0,
            yscrollcommand=scrollbar.set,
        )
        scrollbar.config(command=self._listbox.yview)
        scrollbar.pack(side=tk.RIGHT, fill=tk.Y)
        self._listbox.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

        # Bind selection to edit mode
        self._listbox.bind("<<ListboxSelect>>", self._on_listbox_select)

        # Action buttons row
        btn_row = tk.Frame(self._left_frame, bg=COLORS["bg_panel"])
        btn_row.pack(fill=tk.X, padx=8, pady=(0, 4))

        self._test_btn = ttk.Button(
            btn_row, text="Test Selected",
            command=self._test_selected,
            style=TTK_BTN_SECONDARY,
        )
        self._test_btn.pack(side=tk.LEFT, padx=(0, 4))

        self._fetch_all_btn = ttk.Button(
            btn_row, text="Fetch All",
            command=self._fetch_all_details,
            style=TTK_BTN_SECONDARY,
        )
        self._fetch_all_btn.pack(side=tk.LEFT, padx=(0, 4))

        self._remove_btn = ttk.Button(
            btn_row, text="Remove",
            command=self._remove_selected,
            style=TTK_BTN_DANGER,
        )
        self._remove_btn.pack(side=tk.LEFT, padx=(0, 4))

        self._restore_btn = ttk.Button(
            btn_row, text="Restore Hidden",
            command=self._restore_hidden,
            style=TTK_BTN_SECONDARY,
        )
        self._restore_btn.pack(side=tk.LEFT)

        # Second button row: + Add New, Browse fal.ai
        btn_row2 = tk.Frame(self._left_frame, bg=COLORS["bg_panel"])
        btn_row2.pack(fill=tk.X, padx=8, pady=(0, 8))

        self._add_new_btn = ttk.Button(
            btn_row2, text="+ Add New",
            command=self._switch_to_add_mode,
            style=TTK_BTN_SUCCESS,
        )
        self._add_new_btn.pack(side=tk.LEFT, padx=(0, 4))

        self._browse_btn = ttk.Button(
            btn_row2, text="Browse fal.ai",
            command=self._toggle_browse_pane,
            style=TTK_BTN_SECONDARY,
        )
        self._browse_btn.pack(side=tk.LEFT)

        # Browse pane (initially hidden)
        self._browse_frame = tk.LabelFrame(
            self._left_frame, text="  Browse fal.ai Models  ",
            font=(FONT_FAMILY, 9, "bold"),
            bg=COLORS["bg_panel"], fg=COLORS["text_light"],
            highlightbackground=COLORS["border"], highlightthickness=1,
        )
        # Not packed yet — toggled by _toggle_browse_pane

        browse_inner = tk.Frame(self._browse_frame, bg=COLORS["bg_panel"])
        browse_inner.pack(fill=tk.BOTH, expand=True, padx=6, pady=4)

        self._browse_status = tk.Label(
            browse_inner, text="Click to load available models",
            font=(FONT_FAMILY, 9), bg=COLORS["bg_panel"], fg=COLORS["text_dim"],
        )
        self._browse_status.pack(anchor="w")

        browse_list_frame = tk.Frame(browse_inner, bg=COLORS["bg_panel"])
        browse_list_frame.pack(fill=tk.BOTH, expand=True, pady=(2, 4))

        browse_scroll = ttk.Scrollbar(browse_list_frame, orient=tk.VERTICAL)
        self._browse_listbox = tk.Listbox(
            browse_list_frame, font=(FONT_FAMILY, 9),
            bg=COLORS["bg_main"], fg=COLORS["text_light"],
            selectbackground=COLORS["accent_blue"], selectforeground="#FFFFFF",
            activestyle="none", borderwidth=0, highlightthickness=0,
            selectmode=tk.EXTENDED, height=8,
            yscrollcommand=browse_scroll.set,
        )
        browse_scroll.config(command=self._browse_listbox.yview)
        browse_scroll.pack(side=tk.RIGHT, fill=tk.Y)
        self._browse_listbox.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

        self._browse_add_btn = ttk.Button(
            browse_inner, text="Add Selected",
            command=self._add_browsed_models,
            style=TTK_BTN_SUCCESS,
        )
        self._browse_add_btn.pack(anchor="w", pady=(2, 0))

    def _build_right_panel(self, parent: tk.Frame):
        """Right panel: add/edit form (switches between modes)."""
        self._right_frame = tk.LabelFrame(
            parent, text="  Add Custom Model  ", font=(FONT_FAMILY, 10, "bold"),
            bg=COLORS["bg_panel"], fg=COLORS["text_light"],
            highlightbackground=COLORS["border"], highlightthickness=1,
        )
        self._right_frame.pack(side=tk.LEFT, fill=tk.BOTH, padx=(6, 0), ipadx=4)
        self._right_frame.configure(width=320)

        inner = tk.Frame(self._right_frame, bg=COLORS["bg_panel"])
        inner.pack(fill=tk.BOTH, expand=True, padx=10, pady=8)
        self._form_inner = inner

        lbl_opts = dict(font=(FONT_FAMILY, 9, "bold"), bg=COLORS["bg_panel"], fg=COLORS["text_dim"], anchor="w")
        entry_opts = dict(
            font=(FONT_FAMILY, 10), bg=COLORS["bg_main"], fg=COLORS["text_light"],
            insertbackground=COLORS["text_light"], relief=tk.FLAT,
            highlightthickness=1, highlightbackground=COLORS["border"],
        )

        # Name
        tk.Label(inner, text="Name:", **lbl_opts).pack(fill=tk.X, pady=(0, 2))
        self._name_var = tk.StringVar()
        self._name_entry = tk.Entry(inner, textvariable=self._name_var, **entry_opts)
        self._name_entry.pack(fill=tk.X, pady=(0, 6))

        # Endpoint
        tk.Label(inner, text="Endpoint:", **lbl_opts).pack(fill=tk.X, pady=(0, 2))
        self._endpoint_var = tk.StringVar()
        self._endpoint_entry = tk.Entry(inner, textvariable=self._endpoint_var, **entry_opts)
        self._endpoint_entry.pack(fill=tk.X, pady=(0, 2))
        self._endpoint_hint = tk.Label(
            inner, text="e.g. fal-ai/kling-video/v3/pro/image-to-video",
            font=(FONT_FAMILY, 9), bg=COLORS["bg_panel"], fg=COLORS["text_dim"],
        )
        self._endpoint_hint.pack(anchor="w", pady=(0, 6))

        # Release (optional)
        tk.Label(inner, text="Release (optional):", **lbl_opts).pack(fill=tk.X, pady=(0, 2))
        self._release_var = tk.StringVar()
        tk.Entry(inner, textvariable=self._release_var, **entry_opts).pack(fill=tk.X, pady=(0, 6))

        # Duration checkboxes
        tk.Label(inner, text="Durations:", **lbl_opts).pack(fill=tk.X, pady=(0, 2))
        dur_frame = tk.Frame(inner, bg=COLORS["bg_panel"])
        dur_frame.pack(fill=tk.X, pady=(0, 4))

        self._dur_5_var = tk.BooleanVar(value=True)
        self._dur_10_var = tk.BooleanVar(value=True)
        cb_opts = dict(
            font=(FONT_FAMILY, 9), bg=COLORS["bg_panel"], fg=COLORS["text_light"],
            selectcolor=COLORS["bg_main"], activebackground=COLORS["bg_panel"],
            activeforeground=COLORS["text_light"],
            **macos_widget_pad(),
        )
        tk.Checkbutton(dur_frame, text="5s", variable=self._dur_5_var, **cb_opts).pack(side=tk.LEFT, padx=(0, 8))
        tk.Checkbutton(dur_frame, text="10s", variable=self._dur_10_var, **cb_opts).pack(side=tk.LEFT, padx=(0, 8))

        # Default duration
        tk.Label(inner, text="Default:", **lbl_opts).pack(fill=tk.X, pady=(0, 2))
        self._default_dur_var = tk.StringVar(value="10")
        def_dur_frame = tk.Frame(inner, bg=COLORS["bg_panel"])
        def_dur_frame.pack(fill=tk.X, pady=(0, 6))
        rb_opts = dict(
            font=(FONT_FAMILY, 9), bg=COLORS["bg_panel"], fg=COLORS["text_light"],
            selectcolor=COLORS["bg_main"], activebackground=COLORS["bg_panel"],
            activeforeground=COLORS["text_light"],
            **macos_widget_pad(),
        )
        tk.Radiobutton(def_dur_frame, text="5s", variable=self._default_dur_var, value="5", **rb_opts).pack(side=tk.LEFT, padx=(0, 8))
        tk.Radiobutton(def_dur_frame, text="10s", variable=self._default_dur_var, value="10", **rb_opts).pack(side=tk.LEFT)

        # Notes
        tk.Label(inner, text="Notes (optional):", **lbl_opts).pack(fill=tk.X, pady=(0, 2))
        self._notes_text = tk.Text(
            inner, font=(FONT_FAMILY, 9), height=3,
            bg=COLORS["bg_main"], fg=COLORS["text_light"],
            insertbackground=COLORS["text_light"], relief=tk.FLAT,
            borderwidth=0, highlightthickness=1, highlightbackground=COLORS["border"],
            wrap=tk.WORD,
        )
        self._notes_text.pack(fill=tk.X, pady=(0, 6))

        # Test + Add/Save buttons
        self._form_btns = tk.Frame(inner, bg=COLORS["bg_panel"])
        self._form_btns.pack(fill=tk.X, pady=(0, 4))

        self._test_new_btn = ttk.Button(
            self._form_btns, text="Test",
            command=self._test_new_endpoint,
            style=TTK_BTN_SECONDARY,
        )
        self._test_new_btn.pack(side=tk.LEFT, padx=(0, 4))

        self._add_btn = ttk.Button(
            self._form_btns, text="Add",
            command=self._add_custom_model,
            style=TTK_BTN_SUCCESS,
        )
        self._add_btn.pack(side=tk.LEFT)

        # Save button (hidden in add mode, shown in edit mode)
        self._save_edit_btn = ttk.Button(
            self._form_btns, text="Save Changes",
            command=self._save_edit,
            style=TTK_BTN_SUCCESS,
        )
        # Not packed initially — shown in edit mode

        # Test result area (multi-line for capability info)
        self._test_result_text = tk.Text(
            inner, font=(FONT_FAMILY, 9), height=8,
            bg=COLORS["bg_panel"], fg=COLORS["text_dim"],
            relief=tk.FLAT, borderwidth=0, wrap=tk.WORD,
            state="disabled",
        )
        self._test_result_text.pack(fill=tk.BOTH, expand=True)
        self._test_result_text.tag_configure("success", foreground=COLORS["success"])
        self._test_result_text.tag_configure("error", foreground=COLORS["error"])
        self._test_result_text.tag_configure("warning", foreground=COLORS["warning"])
        self._test_result_text.tag_configure("info", foreground=COLORS["text_light"])
        self._test_result_text.tag_configure("dim", foreground=COLORS["text_dim"])

    def _build_footer(self):
        """Footer with Cancel / Save buttons."""
        footer = tk.Frame(self, bg=COLORS["bg_main"])
        footer.pack(fill=tk.X, padx=12, pady=10)

        _footer_save_btn = ttk.Button(
            footer, text="Save",
            command=self._on_save_click,
            style=TTK_BTN_SUCCESS,
            width=10,
        )
        _footer_save_btn.pack(side=tk.RIGHT, padx=(6, 0))

        # Labeled "Close" not "Cancel" because _on_cancel autosaves
        # custom_models, hidden_models, and enrichment data — the user
        # asked for this behavior (no data loss on dialog close), but a
        # button labeled "Cancel" that commits edits is dishonest UX
        # (CodeRabbit major on 253a9b4).
        _footer_cancel_btn = ttk.Button(
            footer, text="Close",
            command=self._on_cancel,
            style=TTK_BTN_SECONDARY,
            width=10,
        )
        _footer_cancel_btn.pack(side=tk.RIGHT)

    # ------------------------------------------------------------------
    # Add / Edit mode switching
    # ------------------------------------------------------------------

    def _switch_to_add_mode(self):
        """Switch right panel to Add mode with blank form."""
        self._edit_mode = False
        self._edit_index = None
        self._right_frame.config(text="  Add Custom Model  ")

        # Show Add button, hide Save button
        self._save_edit_btn.pack_forget()
        self._add_btn.pack(side=tk.LEFT)

        # Enable endpoint editing
        self._endpoint_entry.config(state="normal")
        self._endpoint_hint.config(text="e.g. fal-ai/kling-video/v3/pro/image-to-video")

        # Clear form
        self._clear_form()

        # Deselect listbox
        self._listbox.selection_clear(0, tk.END)

    def _switch_to_edit_mode(self, index: int):
        """Switch right panel to Edit mode, populated with selected model."""
        model = self._all_models[index]
        self._edit_mode = True
        self._edit_index = index
        is_factory = model.get("_factory", True)

        name = model.get("name", "")
        self._right_frame.config(text=f"  Edit: {name}  " if name else "  Edit Model  ")

        # Hide Add button, show Save button
        self._add_btn.pack_forget()
        self._save_edit_btn.pack(side=tk.LEFT)

        # Endpoint: read-only for factory models, editable for custom
        if is_factory:
            self._endpoint_entry.config(state="disabled")
            self._endpoint_hint.config(text="(factory model — endpoint is read-only)")
        else:
            self._endpoint_entry.config(state="normal")
            self._endpoint_hint.config(text="e.g. fal-ai/kling-video/v3/pro/image-to-video")

        # Populate form
        self._name_var.set(model.get("name", ""))
        # Must set endpoint while entry is normal, then disable if needed
        ep = model.get("endpoint", "")
        self._endpoint_entry.config(state="normal")
        self._endpoint_var.set(ep)
        if is_factory:
            self._endpoint_entry.config(state="disabled")

        self._release_var.set(model.get("release", ""))

        # Duration checkboxes. models.json now persists durations as
        # JSON strings ("5"/"10"); the editor (and _save_*) work purely
        # in ints (_DURATION_CHOICES = [5, 10]). Normalize at this read
        # boundary so factory models open with the correct boxes ticked
        # — otherwise `5 in ["5","10"]` is False, every model opens with
        # both unchecked, and a plain Save silently collapses durations
        # to [10], dropping 5s support (Codex P2, PR #41). Tolerates
        # ints, numeric strings, and junk (skipped).
        def _as_int_durations(raw):
            out = []
            # Tolerate a scalar (`10`) too — a hand-edited or stale
            # config could persist `duration_options` as a bare int /
            # string instead of a list, which the bare `for v in raw`
            # would crash on before rendering (CodeRabbit, PR #41).
            if not isinstance(raw, (list, tuple, set)):
                raw = [raw] if raw is not None else []
            for v in raw:
                try:
                    out.append(int(v))
                except (TypeError, ValueError):
                    continue
            return out

        dur_opts = _as_int_durations(model.get("duration_options", [5, 10]))
        self._dur_5_var.set(5 in dur_opts)
        self._dur_10_var.set(10 in dur_opts)

        try:
            default_dur = int(model.get("duration_default", 10))
        except (TypeError, ValueError):
            default_dur = 10
        self._default_dur_var.set(str(default_dur))

        # Notes: prefer user_notes, then notes, then api_description as starting point
        notes = model.get("user_notes", "") or model.get("notes", "")
        if not notes and model.get("api_description"):
            notes = model["api_description"]
        self._notes_text.delete("1.0", tk.END)
        self._notes_text.insert("1.0", notes)

        # Clear test result
        self._test_result_text.config(state="normal")
        self._test_result_text.delete("1.0", tk.END)
        self._test_result_text.config(state="disabled")

    def _on_listbox_select(self, event):
        """Handle listbox selection -> switch to edit mode."""
        idx = self._get_selected_index()
        if idx is not None:
            self._switch_to_edit_mode(idx)

    def _clear_form(self):
        """Reset all form fields to defaults."""
        self._name_var.set("")
        self._endpoint_var.set("")
        self._release_var.set("")
        self._dur_5_var.set(True)
        self._dur_10_var.set(True)
        self._default_dur_var.set("10")
        self._notes_text.delete("1.0", tk.END)
        self._test_result_text.config(state="normal")
        self._test_result_text.delete("1.0", tk.END)
        self._test_result_text.config(state="disabled")

    # ------------------------------------------------------------------
    # Model list management
    # ------------------------------------------------------------------

    def _rebuild_model_list(self):
        """Rebuild the combined model list and refresh the listbox."""
        self._all_models = []

        # Factory models (from MODEL_METADATA / models.json)
        for m in MODEL_METADATA:
            entry = dict(m)
            ep = entry.get("endpoint", "")
            entry["_factory"] = True
            entry["_hidden"] = ep in self._hidden_endpoints
            self._all_models.append(entry)

        # Custom models
        for m in self._custom_models:
            entry = dict(m)
            entry["_factory"] = False
            entry["_hidden"] = False
            entry["custom"] = True
            self._all_models.append(entry)

        self._refresh_listbox()

    def _refresh_listbox(self):
        """Redraw the listbox from self._all_models."""
        self._listbox.delete(0, tk.END)

        for m in self._all_models:
            ep = m.get("endpoint", "")
            name = m.get("name", ep)
            is_hidden = m.get("_hidden", False)
            is_custom = not m.get("_factory", True)

            # Build display string
            prefix = "\u2605 " if is_custom else ""  # star for custom
            suffix = ""

            # Release date
            release = m.get("release", "")
            if release:
                sanitized = _sanitize_release_date(release)
                if sanitized:
                    suffix += f" ({sanitized})"

            # Pricing info
            price_str = _format_short_price(m.get("pricing_info"))
            if price_str:
                suffix += f", {price_str}" if suffix else f" \u2014 {price_str}"

            if is_hidden:
                suffix += "  [hidden]"

            health = self._health_cache.get(ep)
            if health is True:
                suffix += "  \u2714"  # checkmark
            elif health is False:
                suffix += "  \u2718 UNAVAILABLE"  # X mark

            display = f"{prefix}{name}{suffix}"
            self._listbox.insert(tk.END, display)

            # Color coding
            idx = self._listbox.size() - 1
            if is_hidden:
                self._listbox.itemconfig(idx, fg=COLORS["text_dim"])
            elif health is False:
                self._listbox.itemconfig(idx, fg=COLORS["error"])
            elif is_custom:
                self._listbox.itemconfig(idx, fg=COLORS["accent_blue"])

    # ------------------------------------------------------------------
    # Enrichment persistence
    # ------------------------------------------------------------------

    def _load_enrichment_data(self):
        """Load persisted enrichment data from config and merge onto model dicts."""
        enrichment = self._config.get("model_enrichment", {})
        if not enrichment:
            return

        for model in self._all_models:
            ep = model.get("endpoint", "")
            data = enrichment.get(ep)
            if not data:
                continue
            if data.get("pricing_info") and not model.get("pricing_info"):
                model["pricing_info"] = data["pricing_info"]
            if data.get("capabilities") and not model.get("capabilities"):
                model["capabilities"] = data["capabilities"]
            if data.get("api_display_name") and not model.get("api_display_name"):
                model["api_display_name"] = data["api_display_name"]
            if data.get("api_description") and not model.get("api_description"):
                model["api_description"] = data["api_description"]
            if data.get("health") is not None:
                self._health_cache.setdefault(ep, data["health"])

        # Refresh listbox to show pricing from enrichment
        self._refresh_listbox()

    def _save_enrichment_data(self):
        """Collect enrichment data from model dicts and save to config."""
        enrichment = {}
        for model in self._all_models:
            ep = model.get("endpoint", "")
            if not ep:
                continue
            data = {}
            if model.get("pricing_info"):
                data["pricing_info"] = model["pricing_info"]
            if model.get("capabilities"):
                data["capabilities"] = model["capabilities"]
            if model.get("api_display_name"):
                data["api_display_name"] = model["api_display_name"]
            if model.get("api_description"):
                data["api_description"] = model["api_description"]
            health = self._health_cache.get(ep)
            if health is not None:
                data["health"] = health
            if data:
                enrichment[ep] = data
        self._config["model_enrichment"] = enrichment

    def _enrich_model_from_test(self, endpoint: str, alive: Optional[bool],
                                caps: dict = None, pricing: dict = None, meta: dict = None):
        """Write test results back onto the model dict in self._all_models."""
        model = self._find_model_by_endpoint(endpoint)
        if not model or not alive:
            return
        if pricing:
            model["pricing_info"] = pricing
        if caps:
            model["capabilities"] = caps
        if meta:
            if meta.get("display_name"):
                model["api_display_name"] = meta["display_name"]
            if meta.get("description"):
                model["api_description"] = meta["description"]
            # Always update release date from API (overwrite stale estimates)
            if meta.get("date"):
                model["release"] = _sanitize_release_date(meta["date"])

    def _find_model_by_endpoint(self, endpoint: str) -> Optional[Dict]:
        """Find a model dict in self._all_models by endpoint."""
        for m in self._all_models:
            if m.get("endpoint") == endpoint:
                return m
        return None

    # ------------------------------------------------------------------
    # Actions
    # ------------------------------------------------------------------

    def _get_selected_index(self) -> Optional[int]:
        sel = self._listbox.curselection()
        if not sel:
            return None
        return sel[0]

    def _test_selected(self):
        """Test the selected model endpoint -- fetch full API info."""
        idx = self._get_selected_index()
        if idx is None:
            messagebox.showinfo("Test", "Select a model first.", parent=self)
            return

        model = self._all_models[idx]
        ep = model.get("endpoint", "")
        if not ep:
            return

        self._test_btn.config(state="disabled", text="Testing...")

        def _check():
            caps = None
            pricing_info = None
            meta = None
            try:
                from model_schema_manager import check_endpoint_health, ModelSchemaManager
                alive = check_endpoint_health(self._api_key, ep)

                # If alive and API key present, fetch full info
                if alive and self._api_key:
                    try:
                        schema_mgr = ModelSchemaManager(self._api_key)
                        caps = schema_mgr.extract_capabilities(ep)
                        pricing = schema_mgr.get_model_pricing([ep])
                        pricing_info = pricing.get(ep)
                        meta = schema_mgr.get_model_metadata(ep)
                    except Exception as e:
                        logger.debug(f"Capability fetch failed for {ep}: {e}")
            except Exception:
                alive = None
            self.after(0, self._on_test_result, ep, alive, True, caps, pricing_info, meta)

        threading.Thread(target=_check, daemon=True).start()

    def _on_test_result(self, endpoint: str, alive: Optional[bool], is_list_test: bool,
                        caps: dict = None, pricing: dict = None, meta: dict = None):
        """Handle test result on main thread."""
        self._health_cache[endpoint] = alive

        # Enrich model dict with test data
        self._enrich_model_from_test(endpoint, alive, caps, pricing, meta)

        if is_list_test:
            self._test_btn.config(state="normal", text="Test Selected")
            self._refresh_listbox()
            # Show full results in the test result widget
            self._set_test_result_text(alive, caps, pricing, meta)
        else:
            # Result for the add-form test
            self._test_new_btn.config(state="normal", text="Test")
            self._set_test_result_text(alive, caps, pricing, meta)

            # Auto-populate name from API if user hasn't entered one
            if alive and meta and meta.get("display_name") and not self._name_var.get().strip():
                self._name_var.set(meta["display_name"])

            # Auto-populate release from API if user hasn't entered one
            if alive and meta and meta.get("date") and not self._release_var.get().strip():
                self._release_var.set(_sanitize_release_date(meta["date"]))

    def _set_test_result_text(self, alive: Optional[bool], caps: dict = None,
                               pricing: dict = None, meta: dict = None):
        """Write multi-line test result into the result text widget."""
        self._test_result_text.config(state="normal")
        self._test_result_text.delete("1.0", tk.END)

        if alive is True:
            self._test_result_text.insert(tk.END, "\u2714 Endpoint is alive\n", "success")

            # Model name from API
            if meta and meta.get("display_name"):
                self._test_result_text.insert(tk.END, f"  Name: {meta['display_name']}\n", "info")

            # Pricing -- enhanced display
            if pricing:
                unit = pricing.get("unit", "")
                price = pricing.get("unit_price", 0)
                if price:
                    if unit == "second":
                        price_str = f"${price:.3f}/second (${price * 5:.2f}/5s, ${price * 10:.2f}/10s)"
                    elif unit == "video":
                        price_str = f"${price:.2f}/video"
                    else:
                        price_str = f"${price:.3f}/{unit}" if unit else f"${price:.3f}"
                    self._test_result_text.insert(tk.END, f"  Pricing: {price_str}\n", "info")

            # Capabilities
            if caps:
                # Duration list
                durations = caps.get("duration_options", [])
                if durations:
                    dur_str = ", ".join(str(d) for d in durations)
                else:
                    dur_str = "N/A"

                # Aspect ratios
                aspects = caps.get("aspect_ratios", [])
                ar_str = ", ".join(aspects) if aspects else "N/A"

                self._test_result_text.insert(
                    tk.END, f"  Durations: {dur_str}\n", "dim"
                )
                self._test_result_text.insert(
                    tk.END, f"  Aspect Ratios: {ar_str}\n", "dim"
                )

                # Feature flags
                flags = []
                flag_map = [
                    ("supports_audio", "Audio"),
                    ("supports_camera_fixed", "Camera Lock"),
                    ("supports_seed", "Seed"),
                    ("supports_resolution", "Resolution"),
                    ("supports_negative_prompt", "Neg Prompt"),
                ]
                for key, label in flag_map:
                    icon = "\u2713" if caps.get(key) else "\u2717"
                    flags.append(f"{icon} {label}")

                self._test_result_text.insert(tk.END, f"  {' | '.join(flags)}\n", "dim")

            # Category/status from metadata
            if meta:
                parts = []
                if meta.get("category"):
                    parts.append(f"Category: {meta['category']}")
                if meta.get("status"):
                    parts.append(f"Status: {meta['status']}")
                if parts:
                    self._test_result_text.insert(tk.END, f"  {' | '.join(parts)}\n", "dim")

        elif alive is False:
            self._test_result_text.insert(tk.END, "\u2718 Endpoint unavailable (404)", "error")
        else:
            self._test_result_text.insert(tk.END, "? Could not determine status", "warning")

        self._test_result_text.config(state="disabled")

    def _remove_selected(self):
        """Remove/hide the selected model."""
        idx = self._get_selected_index()
        if idx is None:
            messagebox.showinfo("Remove", "Select a model first.", parent=self)
            return

        model = self._all_models[idx]
        ep = model.get("endpoint", "")
        is_factory = model.get("_factory", True)

        if is_factory:
            # Factory models get hidden, not deleted
            if model.get("_hidden"):
                messagebox.showinfo("Remove", "This model is already hidden.", parent=self)
                return
            if ep and ep not in self._hidden_endpoints:
                self._hidden_endpoints.append(ep)
        else:
            # Custom models get actually removed
            self._custom_models = [
                m for m in self._custom_models if m.get("endpoint") != ep
            ]

        self._rebuild_model_list()
        self._switch_to_add_mode()

    def _restore_hidden(self):
        """Restore all hidden factory models."""
        if not self._hidden_endpoints:
            messagebox.showinfo("Restore", "No hidden models to restore.", parent=self)
            return

        count = len(self._hidden_endpoints)
        self._hidden_endpoints.clear()
        self._rebuild_model_list()
        messagebox.showinfo("Restored", f"Restored {count} hidden model(s).", parent=self)

    def _test_new_endpoint(self):
        """Test the endpoint entered in the form + fetch capabilities."""
        ep = self._endpoint_var.get().strip()
        if not ep:
            self._test_result_text.config(state="normal")
            self._test_result_text.delete("1.0", tk.END)
            self._test_result_text.insert(tk.END, "Enter an endpoint first", "warning")
            self._test_result_text.config(state="disabled")
            return

        self._test_new_btn.config(state="disabled", text="Testing...")
        self._test_result_text.config(state="normal")
        self._test_result_text.delete("1.0", tk.END)
        self._test_result_text.insert(tk.END, "Checking endpoint...", "dim")
        self._test_result_text.config(state="disabled")

        def _check():
            caps = None
            pricing_info = None
            meta = None
            try:
                from model_schema_manager import check_endpoint_health, ModelSchemaManager
                alive = check_endpoint_health(self._api_key, ep)

                # If alive, also fetch schema capabilities + pricing + metadata
                if alive and self._api_key:
                    try:
                        schema_mgr = ModelSchemaManager(self._api_key)
                        caps = schema_mgr.extract_capabilities(ep)
                        pricing = schema_mgr.get_model_pricing([ep])
                        pricing_info = pricing.get(ep)
                        meta = schema_mgr.get_model_metadata(ep)
                    except Exception as e:
                        logger.debug(f"Capability fetch failed for {ep}: {e}")
            except Exception:
                alive = None
            self.after(0, self._on_test_result, ep, alive, False, caps, pricing_info, meta)

        threading.Thread(target=_check, daemon=True).start()

    def _add_custom_model(self):
        """Add a custom model from the form fields."""
        name = self._name_var.get().strip()
        endpoint = self._endpoint_var.get().strip()

        if not name:
            messagebox.showwarning("Missing Name", "Please enter a model name.", parent=self)
            return
        if not endpoint:
            messagebox.showwarning("Missing Endpoint", "Please enter a fal.ai endpoint.", parent=self)
            return

        # Duplicate check against all models
        all_endpoints = set()
        for m in MODEL_METADATA:
            all_endpoints.add(m.get("endpoint", ""))
        for m in self._custom_models:
            all_endpoints.add(m.get("endpoint", ""))

        if endpoint in all_endpoints:
            messagebox.showwarning(
                "Duplicate Endpoint",
                f"Endpoint already exists in the model list:\n{endpoint}",
                parent=self,
            )
            return

        # Build duration options
        durations = []
        if self._dur_5_var.get():
            durations.append(5)
        if self._dur_10_var.get():
            durations.append(10)
        if not durations:
            durations = [10]

        default_dur = int(self._default_dur_var.get())
        if default_dur not in durations:
            default_dur = durations[0]

        notes = self._notes_text.get("1.0", tk.END).strip()
        release = self._release_var.get().strip()

        new_model = {
            "name": name,
            "endpoint": endpoint,
            "duration_options": durations,
            "duration_default": default_dur,
            "custom": True,
        }
        if release:
            new_model["release"] = release
        if notes:
            new_model["notes"] = notes

        self._custom_models.append(new_model)
        self._rebuild_model_list()

        # Clear form
        self._clear_form()
        self._test_result_text.config(state="normal")
        self._test_result_text.delete("1.0", tk.END)
        self._test_result_text.insert(tk.END, f"Added: {name}", "success")
        self._test_result_text.config(state="disabled")

    def _save_edit(self):
        """Save changes from edit mode back into the model list."""
        if self._edit_index is None:
            return

        model = self._all_models[self._edit_index]
        ep = model.get("endpoint", "")
        is_factory = model.get("_factory", True)

        new_name = self._name_var.get().strip()
        new_release = self._release_var.get().strip()
        new_notes = self._notes_text.get("1.0", tk.END).strip()

        if not new_name:
            messagebox.showwarning("Missing Name", "Please enter a model name.", parent=self)
            return

        # Build duration options
        durations = []
        if self._dur_5_var.get():
            durations.append(5)
        if self._dur_10_var.get():
            durations.append(10)
        if not durations:
            durations = [10]

        default_dur = int(self._default_dur_var.get())
        if default_dur not in durations:
            default_dur = durations[0]

        if is_factory:
            # For factory models: update the model dict in _all_models directly.
            # These overrides are stored via user_notes map + config overrides.
            model["name"] = new_name
            model["release"] = new_release
            model["user_notes"] = new_notes
            model["duration_options"] = durations
            model["duration_default"] = default_dur

            # Also update the corresponding MODEL_METADATA entry so dropdown refreshes
            for m in MODEL_METADATA:
                if m.get("endpoint") == ep:
                    m["name"] = new_name
                    m["release"] = new_release
                    m["user_notes"] = new_notes
                    break

            # Persist name/release overrides in config for next session
            overrides = self._config.get("model_overrides", {})
            overrides[ep] = {"name": new_name, "release": new_release}
            self._config["model_overrides"] = overrides

            # Persist notes in user_notes map
            user_notes = self._config.get("model_user_notes", {})
            if new_notes:
                user_notes[ep] = new_notes
            elif ep in user_notes:
                del user_notes[ep]
            self._config["model_user_notes"] = user_notes

        else:
            # For custom models: update in _custom_models list
            new_endpoint = self._endpoint_var.get().strip()
            if not new_endpoint:
                messagebox.showwarning("Missing Endpoint", "Please enter an endpoint.", parent=self)
                return

            for cm in self._custom_models:
                if cm.get("endpoint") == ep:
                    cm["name"] = new_name
                    cm["endpoint"] = new_endpoint
                    cm["duration_options"] = durations
                    cm["duration_default"] = default_dur
                    if new_release:
                        cm["release"] = new_release
                    if new_notes:
                        cm["notes"] = new_notes
                    break

        self._rebuild_model_list()

        # Show confirmation
        self._test_result_text.config(state="normal")
        self._test_result_text.delete("1.0", tk.END)
        self._test_result_text.insert(tk.END, f"Saved: {new_name}", "success")
        self._test_result_text.config(state="disabled")

    # ------------------------------------------------------------------
    # Browse fal.ai
    # ------------------------------------------------------------------

    def _toggle_browse_pane(self):
        """Toggle the browse pane visibility."""
        if self._browse_visible:
            self._browse_frame.pack_forget()
            self._browse_visible = False
            self._browse_btn.config(text="Browse fal.ai")
        else:
            self._browse_frame.pack(fill=tk.BOTH, expand=True, padx=8, pady=(0, 8))
            self._browse_visible = True
            self._browse_btn.config(text="Hide Browse")
            # Start loading if not already loaded
            if not self._browse_models:
                self._load_browse_models()

    def _load_browse_models(self):
        """Fetch available models from fal.ai catalog in background."""
        self._browse_status.config(text="Loading from fal.ai API...")
        self._browse_listbox.delete(0, tk.END)

        def _fetch():
            models = []
            error_msg = ""
            try:
                from model_schema_manager import ModelSchemaManager
                if not self._api_key:
                    error_msg = "No API key configured"
                else:
                    schema_mgr = ModelSchemaManager(self._api_key)
                    models = schema_mgr.list_models(category="image-to-video", status="active")
                    if not models:
                        error_msg = "API returned 0 models (endpoint may not support catalog listing)"
                        logger.warning("Browse: fal.ai catalog API returned empty result")
            except Exception as e:
                error_msg = str(e)
                logger.warning(f"Browse fetch failed: {e}")
            self.after(0, self._on_browse_loaded, models, error_msg)

        threading.Thread(target=_fetch, daemon=True).start()

    def _on_browse_loaded(self, models: list, error_msg: str = ""):
        """Populate browse listbox with discovered models."""
        self._browse_models = models
        self._browse_listbox.delete(0, tk.END)

        # Collect already-added endpoints
        existing = set()
        for m in MODEL_METADATA:
            existing.add(m.get("endpoint", ""))
        for m in self._custom_models:
            existing.add(m.get("endpoint", ""))

        if not models:
            msg = f"No models found: {error_msg}" if error_msg else "No models found (check API key)"
            self._browse_status.config(text=msg)
            return

        count = 0
        for m in models:
            ep = m.get("endpoint", "")
            already = ep in existing
            prefix = "[\u2713] " if already else ""  # [checkmark] prefix for existing
            # Always show endpoint path — it's the only unambiguous identifier
            self._browse_listbox.insert(tk.END, f"{prefix}{ep}")
            idx = self._browse_listbox.size() - 1
            if already:
                self._browse_listbox.itemconfig(idx, fg=COLORS["text_dim"])
            count += 1

        self._browse_status.config(text=f"{count} models found")

    def _add_browsed_models(self):
        """Add selected models from the browse list."""
        selection = self._browse_listbox.curselection()
        if not selection:
            messagebox.showinfo("Browse", "Select one or more models first.", parent=self)
            return

        # Existing endpoints
        existing = set()
        for m in MODEL_METADATA:
            existing.add(m.get("endpoint", ""))
        for m in self._custom_models:
            existing.add(m.get("endpoint", ""))

        added = 0
        for idx in selection:
            if idx >= len(self._browse_models):
                continue
            bm = self._browse_models[idx]
            ep = bm.get("endpoint", "")
            if ep in existing:
                continue  # Skip already-added

            name = bm.get("display_name", "") or ep
            new_model = {
                "name": name,
                "endpoint": ep,
                "custom": True,
            }
            if bm.get("date"):
                new_model["release"] = _sanitize_release_date(bm["date"])
            if bm.get("description"):
                new_model["api_description"] = bm["description"]
            self._custom_models.append(new_model)
            existing.add(ep)
            added += 1

        if added:
            self._rebuild_model_list()
            # Refresh browse list to show checkmarks
            self._on_browse_loaded(self._browse_models)

            self._test_result_text.config(state="normal")
            self._test_result_text.delete("1.0", tk.END)
            self._test_result_text.insert(tk.END, f"Added {added} model(s) from browse", "success")
            self._test_result_text.config(state="disabled")
        else:
            messagebox.showinfo("Browse", "All selected models are already in your list.", parent=self)

    # ------------------------------------------------------------------
    # Fetch All Details
    # ------------------------------------------------------------------

    def _fetch_all_details(self):
        """Fetch health, capabilities, pricing, and metadata for all models."""
        self._fetch_cancel = False
        total = len(self._all_models)
        if total == 0:
            return

        self._fetch_all_btn.config(state="disabled", text=f"Fetching... (0/{total})")

        def _worker():
            from model_schema_manager import check_endpoint_health, ModelSchemaManager

            schema_mgr = None
            if self._api_key:
                schema_mgr = ModelSchemaManager(self._api_key)

            for i, model in enumerate(self._all_models):
                if self._fetch_cancel:
                    break

                ep = model.get("endpoint", "")
                if not ep:
                    self.after(0, self._fetch_all_progress, i + 1, total)
                    continue

                caps = None
                pricing_info = None
                meta = None
                try:
                    alive = check_endpoint_health(self._api_key, ep)
                    if alive and schema_mgr:
                        try:
                            caps = schema_mgr.extract_capabilities(ep)
                            pricing = schema_mgr.get_model_pricing([ep])
                            pricing_info = pricing.get(ep)
                            meta = schema_mgr.get_model_metadata(ep)
                        except Exception as e:
                            logger.debug(f"Fetch All: capability fetch failed for {ep}: {e}")
                except Exception:
                    alive = None

                # Update on main thread
                self.after(0, self._fetch_all_update, ep, alive, caps, pricing_info, meta, i + 1, total)

            self.after(0, self._fetch_all_done)

        self._fetch_thread = threading.Thread(target=_worker, daemon=True)
        self._fetch_thread.start()

    def _fetch_all_progress(self, completed: int, total: int):
        """Update progress label during Fetch All."""
        self._fetch_all_btn.config(text=f"Fetching... ({completed}/{total})")

    def _fetch_all_update(self, endpoint: str, alive: Optional[bool],
                          caps: dict, pricing: dict, meta: dict,
                          completed: int, total: int):
        """Handle one model's result during Fetch All (main thread)."""
        self._health_cache[endpoint] = alive
        self._enrich_model_from_test(endpoint, alive, caps, pricing, meta)
        self._fetch_all_btn.config(text=f"Fetching... ({completed}/{total})")
        self._refresh_listbox()

    def _fetch_all_done(self):
        """Fetch All completed — re-enable button."""
        self._fetch_all_btn.config(state="normal", text="Fetch All")
        self._refresh_listbox()

    # ------------------------------------------------------------------
    # Save / Cancel
    # ------------------------------------------------------------------

    def _on_save_click(self):
        """Commit changes to config and close."""
        self._config["custom_models"] = self._custom_models
        self._config["hidden_models"] = self._hidden_endpoints

        # Save enrichment data (pricing, caps, descriptions from tests)
        self._save_enrichment_data()

        # If the currently active model was hidden or removed, pick the first visible
        current_ep = self._config.get("current_model", "")
        visible_models = [
            m for m in self._all_models
            if not m.get("_hidden", False)
        ]
        current_still_visible = any(
            m.get("endpoint") == current_ep for m in visible_models
        )
        if not current_still_visible and visible_models:
            fallback = visible_models[0]
            self._config["current_model"] = fallback.get("endpoint", "")
            self._config["model_display_name"] = fallback.get("name", "")
            logger.info(
                f"Active model was removed/hidden; auto-selected: {fallback.get('name')}"
            )

        if self._on_save:
            self._on_save()

        self.grab_release()
        self.destroy()

    def _on_cancel(self):
        """Auto-save changes and close (prevents losing work from closing without Save)."""
        self._fetch_cancel = True  # Stop any running Fetch All
        # Auto-save to prevent data loss (custom models, hidden endpoints, enrichment)
        self._config["custom_models"] = self._custom_models
        self._config["hidden_models"] = self._hidden_endpoints
        self._save_enrichment_data()
        if self._on_save:
            self._on_save()
        self.grab_release()
        self.destroy()
