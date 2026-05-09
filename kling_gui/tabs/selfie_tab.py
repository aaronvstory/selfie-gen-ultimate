"""Selfie Tab — Generate selfie-style portraits using FLUX PuLID."""

import tkinter as tk
from tkinter import ttk
import threading
import os
import shutil
import platform
import subprocess
import json
import re
from typing import Callable, Dict, List, Optional

from ..theme import (
    BUTTON_FILLED_TEXT_COLOR,
    BUTTON_TEXT_COLOR,
    COLORS,
    FONT_FAMILY,
    TTK_BTN_COMPACT,
    TTK_BTN_DANGER,
    TTK_BTN_PRIMARY,
    TTK_BTN_SECONDARY,
    TTK_BTN_SUCCESS,
    create_action_button,
    debounce_command,
)
from ..image_state import ImageSession
from path_utils import get_gen_images_folder
from tk_dialogs import select_directory, select_save_file

try:
    from selfie_prompt_composer import DEFAULT_GENDER
except Exception:
    DEFAULT_GENDER = "female"


class SelfieTab(tk.Frame):
    """Tab 2: Generate selfie from identity reference."""

    DEFAULT_MODEL_ENDPOINT = "fal-ai/nano-banana-2/edit"
    DEFAULT_PROMPT_TEMPLATE = (
        "Transform this passport photo into a natural selfie: A {json.gender} with "
        "{json.hair}, {json.skin}, {json.eyes}, and a {json.face_shape}, wearing "
        "{json.clothing}, taking a front-facing camera selfie with one arm FULLY "
        "extended but phone not visible in frame, looking directly at camera with "
        "{json.expression}. Shot in portrait orientation zoomed out to show head "
        "SLIGHTLY OFF-CENTER, with extensive space around all sides. Full torso "
        "visible with significant background space above and around subject. "
        "Background: {sunny backyard patio|cozy kitchen|home office|living room "
        "couch|coffee shop window}. Lighting: natural lighting. Authentic "
        "front-facing iPhone X camera quality, natural skin imperfections, uneven "
        "lighting, slightly off-center composition. Include realistic flaws: minor "
        "focus issues, natural skin texture with EVIDENT pores and minimal makeup, "
        "casual messy hair, wrinkled clothing, candid expression. Other arm relaxed "
        "at side, natural one-handed selfie pose. Maintain EXACT facial features, "
        "bone structure, and identity from reference image. Raw unfiltered AMATEUR "
        "smartphone selfie aesthetic with imperfect framing and natural shadows."
    )
    DEFAULT_WILDCARD_TEMPLATE = (
        "A raw, unedited iPhone 7 front-camera selfie of a {young adult|middle-aged} "
        "{woman|man} wearing a {black|gray|white|maroon|navy} "
        "{hoodie|t-shirt|sweater|jacket|blouse}. "
        "{Neutral|Slight smile|Relaxed|Confident} expression. "
        "Phone held at arm's length, slightly off-center, natural edge distortion. "
        "Background is {sunny park with green trees|cozy kitchen with warm light|"
        "living room with TV glow|outdoor street in urban daylight|"
        "cafe with soft window light}. "
        "Warm practical lighting. Amateur photography aesthetic, unfiltered iPhone 7 quality."
    )
    DISABLED_BY_DEFAULT_ENDPOINTS = set()
    SLOT_COUNT = 10
    DEFAULT_SELFIE_PROMPT_SLOT = 3

    # Known fields for auto-migrating old {field} syntax → {json.field}
    _KNOWN_JSON_FIELDS = {
        "hair", "skin", "eyes", "face_shape", "age_range",
        "gender", "clothing", "expression",
    }

    @staticmethod
    def _extract_json_fields(template: str) -> List[str]:
        """Extract {json.FIELD} tag names from template. Returns ['hair', 'skin', ...]."""
        return re.findall(r"\{json\.([a-zA-Z0-9_]+)\}", template)

    @classmethod
    def _migrate_template_syntax(cls, template: str) -> str:
        """Migrate old {field} syntax to {json.field} for known fields.

        E.g. '{hair}' → '{json.hair}' but '{sunny patio|cozy kitchen}' stays unchanged
        (wildcards contain '|').
        """
        def _maybe_migrate(match):
            inner = match.group(1)
            # Wildcards contain '|' — skip them
            if "|" in inner:
                return match.group(0)
            # Known field names get the json. prefix
            if inner in cls._KNOWN_JSON_FIELDS:
                return "{json." + inner + "}"
            return match.group(0)
        return re.sub(r"\{([^{}]+)\}", _maybe_migrate, template)

    def __init__(
        self,
        parent,
        image_session: ImageSession,
        config: dict,
        config_getter: Callable[[], dict],
        log_callback: Callable[[str, str], None],
        on_send_to_expand: Optional[Callable[[List[str], Optional[str]], None]] = None,
        notebook_switcher_expand: Optional[Callable[[], None]] = None,
        config_saver: Optional[Callable[[], None]] = None,
        **kwargs,
    ):
        super().__init__(parent, bg=COLORS["bg_panel"], **kwargs)
        self.image_session = image_session
        self.config = config
        self.get_config = config_getter
        self.log = log_callback
        self._config_saver = config_saver
        self._on_send_to_expand_cb = on_send_to_expand
        self._notebook_switcher_expand = notebook_switcher_expand
        self._busy = False
        self._last_result_path = ""
        self._last_batch_result_paths: List[str] = []
        # Cancellation events (three-tier)
        self._cancel_current = threading.Event()  # skip active model, advance
        self._cancel_all = threading.Event()       # stop after current model
        self._abort_flow = threading.Event()       # immediate full termination
        self._model_options = self._load_model_options()
        self._supported_model_endpoints = {
            model.get("endpoint", "") for model in self._model_options if model.get("endpoint")
        }
        self._migrate_selected_models_config()
        self._model_vars: Dict[str, tk.BooleanVar] = {}
        self._handoff_identity_data: Optional[Dict[str, str]] = None
        self._handoff_resolved = False
        self._prompt_template_edit_mode = False
        self._wildcard_edit_mode = False
        self._edit_original_raw_template = ""
        self._edit_original_wildcard_template = ""
        self._edit_original_slot_title = ""
        self._raw_template = ""  # overwritten in _build_ui
        self._run_ref_path = ""
        self._run_ref_source = "none"
        self._slot_title_var = tk.StringVar(value="")
        self._selfie_slot_var = tk.IntVar(value=1)
        self._prompt_mode_var = tk.StringVar(
            value=config.get("selfie_prompt_mode", "json_handoff")
        )
        self._init_selfie_prompt_slots()

        self._build_ui()

    def _migrate_selected_models_config(self) -> None:
        """Limit saved model map to supported endpoints and force new defaults."""
        saved_models_raw = self.config.get("selfie_selected_models", {})
        saved_models: Dict[str, bool] = {}
        if isinstance(saved_models_raw, list):
            saved_models = {str(endpoint): True for endpoint in saved_models_raw if isinstance(endpoint, str)}
        elif isinstance(saved_models_raw, dict):
            saved_models = {str(k): bool(v) for k, v in saved_models_raw.items()}

        enabled = {endpoint for endpoint, is_enabled in saved_models.items() if is_enabled}
        stale_old_default = enabled == {"openai/gpt-image-2/edit"}

        migrated = {endpoint: bool(saved_models.get(endpoint, False)) for endpoint in self._supported_model_endpoints}
        if stale_old_default or not any(migrated.values()):
            for endpoint in migrated.keys():
                migrated[endpoint] = endpoint == self.DEFAULT_MODEL_ENDPOINT

        self.config["selfie_selected_models"] = migrated

    # ── Config persistence ────────────────────────────────────────

    def _save_config_now(self):
        """Update shared config dict and persist to disk immediately."""
        self.config.update(self.get_config_updates())
        if self._config_saver:
            self._config_saver()

    @staticmethod
    def _default_slot_title(slot: int) -> str:
        return f"Prompt {slot}"

    def _init_selfie_prompt_slots(self):
        saved_prompts = self.config.get("selfie_saved_prompts")
        saved_wildcards = self.config.get("selfie_wildcard_saved_prompts")
        saved_titles = self.config.get("selfie_prompt_titles")
        if not isinstance(saved_prompts, dict):
            saved_prompts = {}
        if not isinstance(saved_wildcards, dict):
            saved_wildcards = {}
        if not isinstance(saved_titles, dict):
            saved_titles = {}

        slot_keys = [str(i) for i in range(1, self.SLOT_COUNT + 1)]
        normalized_prompts = {k: str(saved_prompts.get(k, "") or "") for k in slot_keys}
        normalized_wildcards = {k: str(saved_wildcards.get(k, "") or "") for k in slot_keys}
        normalized_titles = {k: str(saved_titles.get(k, "") or "") for k in slot_keys}

        # One-time migration: seed slot 1 from legacy template only if slots are empty.
        if not any(v.strip() for v in normalized_prompts.values()):
            legacy_template = str(self.config.get("selfie_prompt_template", "") or "").strip()
            if legacy_template:
                normalized_prompts["1"] = legacy_template

        # One-time migration for old global wildcard template.
        if not any(v.strip() for v in normalized_wildcards.values()):
            legacy_wildcard = str(self.config.get("selfie_wildcard_template", "") or "").strip()
            if legacy_wildcard:
                normalized_wildcards["1"] = legacy_wildcard

        current_slot = self.config.get("selfie_current_prompt_slot", self.DEFAULT_SELFIE_PROMPT_SLOT)
        try:
            current_slot = int(current_slot)
        except Exception:
            current_slot = self.DEFAULT_SELFIE_PROMPT_SLOT
        if current_slot < 1 or current_slot > self.SLOT_COUNT:
            current_slot = self.DEFAULT_SELFIE_PROMPT_SLOT

        self.config["selfie_saved_prompts"] = normalized_prompts
        self.config["selfie_wildcard_saved_prompts"] = normalized_wildcards
        self.config["selfie_prompt_titles"] = normalized_titles
        self.config["selfie_current_prompt_slot"] = current_slot
        self.config["selfie_wildcard_template"] = normalized_wildcards.get(str(current_slot), "")
        self._selfie_slot_var.set(current_slot)
        self._raw_template = normalized_prompts.get(str(current_slot), "") or ""

    def _build_ui(self):
        # Pack btn_frame FIRST so it always reserves its bottom strip,
        # even when content_frame overflows vertically.
        btn_frame = tk.Frame(self, bg=COLORS["bg_panel"])
        btn_frame.pack(side=tk.BOTTOM, fill=tk.X, padx=8, pady=8)
        self._btn_frame = btn_frame  # buttons added at end of method

        content_frame = tk.Frame(self, bg=COLORS["bg_panel"])
        content_frame.pack(fill=tk.BOTH, expand=True)

        # Prompt section (uses composed prompt or custom)
        prompt_frame = tk.LabelFrame(
            content_frame,
            text="Selfie Prompt",
            font=(FONT_FAMILY, 9, "bold"),
            bg=COLORS["bg_panel"],
            fg=COLORS["text_light"],
            labelanchor="nw",
        )
        prompt_frame.pack(fill=tk.X, padx=8, pady=(8, 4))

        # Prompt mode toggle
        mode_row = tk.Frame(prompt_frame, bg=COLORS["bg_panel"])
        mode_row.pack(fill=tk.X, padx=4, pady=(4, 2))
        tk.Label(
            mode_row,
            text="Mode:",
            font=(FONT_FAMILY, 9, "bold"),
            bg=COLORS["bg_panel"],
            fg=COLORS["text_light"],
        ).pack(side=tk.LEFT, padx=(0, 6))
        tk.Radiobutton(
            mode_row,
            text="Customized (AI Analysis)",
            variable=self._prompt_mode_var,
            value="json_handoff",
            command=self._on_prompt_mode_changed,
            bg=COLORS["bg_panel"],
            fg=COLORS["text_light"],
            selectcolor=COLORS["bg_input"],
            activebackground=COLORS["bg_panel"],
            font=(FONT_FAMILY, 9),
        ).pack(side=tk.LEFT, padx=(0, 10))
        tk.Radiobutton(
            mode_row,
            text="Generic (Wildcards)",
            variable=self._prompt_mode_var,
            value="wildcards",
            command=self._on_prompt_mode_changed,
            bg=COLORS["bg_panel"],
            fg=COLORS["text_light"],
            selectcolor=COLORS["bg_input"],
            activebackground=COLORS["bg_panel"],
            font=(FONT_FAMILY, 9),
        ).pack(side=tk.LEFT)
        self._mode_hint_label = tk.Label(
            mode_row,
            text="",
            font=(FONT_FAMILY, 8),
            bg=COLORS["bg_panel"],
            fg=COLORS["text_dim"],
            anchor="e",
        )
        self._mode_hint_label.pack(side=tk.RIGHT, fill=tk.X, expand=True, padx=(10, 0))

        tk.Label(
            mode_row,
            text="Slots:",
            font=(FONT_FAMILY, 8, "bold"),
            bg=COLORS["bg_panel"],
            fg=COLORS["text_light"],
        ).pack(side=tk.LEFT, padx=(14, 6))
        self._slot_buttons = []
        for slot in range(1, self.SLOT_COUNT + 1):
            btn = tk.Button(
                mode_row,
                text=str(slot),
                width=2,
                font=(FONT_FAMILY, 8, "bold"),
                bg=COLORS["bg_input"],
                fg=BUTTON_TEXT_COLOR,
                activebackground=COLORS["accent_blue"],
                activeforeground=BUTTON_FILLED_TEXT_COLOR,
                relief=tk.SOLID,
                bd=1,
                highlightthickness=0,
                command=lambda s=slot: self._on_selfie_slot_changed(s),
            )
            btn.pack(side=tk.LEFT, padx=(0, 3))
            self._slot_buttons.append(btn)

        tk.Label(
            mode_row,
            text="Title:",
            font=(FONT_FAMILY, 8, "bold"),
            bg=COLORS["bg_panel"],
            fg=COLORS["text_light"],
        ).pack(side=tk.LEFT, padx=(10, 6))
        self._slot_title_entry = tk.Entry(
            mode_row,
            textvariable=self._slot_title_var,
            width=20,
            state="readonly",
            bg=COLORS["bg_input"],
            readonlybackground=COLORS["bg_input"],
            fg=COLORS["text_light"],
            insertbackground=COLORS["text_light"],
            font=(FONT_FAMILY, 8),
            relief=tk.SOLID,
            bd=1,
        )
        self._slot_title_entry.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(0, 8))

        # Hidden variables for composer (kept for config compat)
        self.gender_var = tk.StringVar(
            value=self.config.get("composer_gender", DEFAULT_GENDER)
        )
        self.style_var = tk.StringVar(
            value=self.config.get("composer_camera_style", "candid")
        )

        # Customized (AI Analysis) mode — single template text box
        self._customized_frame = tk.LabelFrame(
            prompt_frame,
            text="Prompt Template  {json.field} + {opt1|opt2} wildcards",
            font=(FONT_FAMILY, 8, "bold"),
            bg=COLORS["bg_panel"],
            fg=COLORS["text_light"],
        )
        # Don't pack yet — managed by _apply_prompt_mode_ui()

        self.prompt_template_text = tk.Text(
            self._customized_frame,
            height=7,
            wrap=tk.WORD,
            bg=COLORS["bg_input"],
            fg=COLORS["text_light"],
            font=("Consolas", 9),
            insertbackground=COLORS["text_light"],
            padx=5,
            pady=4,
        )
        self.prompt_template_text.pack(fill=tk.BOTH, expand=True, padx=4, pady=(4, 2))

        # Load saved template with migration from old {field} → {json.field} syntax
        saved_template = self.config.get("selfie_prompt_template", "")
        saved_template = (saved_template or "").strip()
        saved_template = self._migrate_template_syntax(saved_template)

        # One-time migration: old template used {scene} placeholder.
        if "{scene}" in saved_template:
            saved_template = ""
            self.config["selfie_prompt_template"] = saved_template

        self.prompt_template_text.insert("1.0", self._raw_template or saved_template)
        self.prompt_template_text.config(state=tk.DISABLED)

        template_actions = tk.Frame(self._customized_frame, bg=COLORS["bg_panel"])
        template_actions.pack(fill=tk.X, padx=4, pady=(0, 2))
        self.edit_template_btn = ttk.Button(
            template_actions,
            text="Edit Template",
            style=TTK_BTN_SECONDARY,
            command=debounce_command(self._on_edit_prompt_template, key="selfie_edit_template"),
        )
        self.edit_template_btn.pack(side=tk.LEFT)
        self.save_template_btn = ttk.Button(
            template_actions,
            text="Save Template",
            style=TTK_BTN_PRIMARY,
            command=debounce_command(self._on_save_prompt_template, key="selfie_save_template"),
            state=tk.DISABLED,
        )
        self.save_template_btn.pack(side=tk.LEFT, padx=(5, 0))
        self.reset_template_btn = ttk.Button(
            template_actions,
            text="Reset Template",
            style=TTK_BTN_DANGER,
            command=debounce_command(self._on_reset_prompt_template, key="selfie_reset_template"),
        )
        self.reset_template_btn.pack(side=tk.LEFT, padx=(5, 0))

        self._customized_status = tk.Label(
            self._customized_frame,
            text="Template ready \u2014 run AI Analysis in Step 1, then Send to Step 2",
            font=(FONT_FAMILY, 8),
            bg=COLORS["bg_panel"],
            fg=COLORS["text_dim"],
            anchor="w",
        )
        self._customized_status.pack(fill=tk.X, padx=6, pady=(0, 3))

        # Wildcard template (Dynamic Wildcards mode — hidden by default)
        self._wildcard_frame = tk.LabelFrame(
            prompt_frame,
            text="Wildcard Template  {option1|option2|option3}",
            font=(FONT_FAMILY, 8, "bold"),
            bg=COLORS["bg_panel"],
            fg=COLORS["text_light"],
        )
        # Don't pack yet — managed by _apply_prompt_mode_ui()

        self._wildcard_text = tk.Text(
            self._wildcard_frame,
            height=7,
            wrap=tk.WORD,
            bg=COLORS["bg_input"],
            fg=COLORS["text_light"],
            font=("Consolas", 9),
            insertbackground=COLORS["text_light"],
            padx=5,
            pady=4,
        )
        self._wildcard_text.pack(fill=tk.BOTH, expand=True, padx=4, pady=(4, 2))

        saved_wildcard = self.config.get("selfie_wildcard_template", "")
        self._wildcard_text.insert("1.0", (saved_wildcard or "").strip())
        self._wildcard_text.config(state=tk.DISABLED)

        wildcard_actions = tk.Frame(self._wildcard_frame, bg=COLORS["bg_panel"])
        wildcard_actions.pack(fill=tk.X, padx=4, pady=(0, 3))
        self._edit_wildcard_btn = ttk.Button(
            wildcard_actions,
            text="Edit Template",
            style=TTK_BTN_SECONDARY,
            command=debounce_command(self._on_edit_wildcard_template, key="selfie_edit_wildcard"),
        )
        self._edit_wildcard_btn.pack(side=tk.LEFT)
        self._save_wildcard_btn = ttk.Button(
            wildcard_actions,
            text="Save Template",
            style=TTK_BTN_PRIMARY,
            command=debounce_command(self._on_save_wildcard_template, key="selfie_save_wildcard"),
            state=tk.DISABLED,
        )
        self._save_wildcard_btn.pack(side=tk.LEFT, padx=(5, 0))
        ttk.Button(
            wildcard_actions,
            text="Reset Template",
            style=TTK_BTN_DANGER,
            command=debounce_command(self._on_reset_wildcard_template, key="selfie_reset_wildcard"),
        ).pack(side=tk.LEFT, padx=(5, 0))

        # Apply initial prompt mode visibility
        self._apply_prompt_mode_ui()
        self._load_current_slot_into_editor()

        # Settings
        settings_frame = tk.LabelFrame(
            content_frame,
            text="Generation Settings",
            font=(FONT_FAMILY, 9, "bold"),
            bg=COLORS["bg_panel"],
            fg=COLORS["text_light"],
        )
        settings_frame.pack(fill=tk.X, padx=8, pady=4)

        settings_split = tk.Frame(settings_frame, bg=COLORS["bg_panel"])
        settings_split.pack(fill=tk.X, padx=4, pady=4)

        grid = tk.Frame(settings_split, bg=COLORS["bg_panel"])
        grid.pack(side=tk.LEFT, fill=tk.X, expand=True)

        # Face Resemblance (ID Weight)
        tk.Label(
            grid,
            text="Face Resemblance:",
            font=(FONT_FAMILY, 9),
            bg=COLORS["bg_panel"],
            fg=COLORS["text_light"],
        ).grid(row=0, column=0, sticky="w")
        self.id_weight_var = tk.DoubleVar(
            value=self.config.get("selfie_id_weight", 1.0)
        )
        id_scale = tk.Scale(
            grid,
            from_=0.0,
            to=1.0,
            resolution=0.05,
            orient=tk.HORIZONTAL,
            variable=self.id_weight_var,
            bg=COLORS["bg_panel"],
            fg=COLORS["text_light"],
            troughcolor=COLORS["bg_input"],
            highlightthickness=0,
            length=150,
        )
        id_scale.grid(row=0, column=1, padx=5, pady=2, sticky="w")

        # Aspect Ratio (replaces manual Width/Height)
        self._aspect_ratios = {
            "Portrait (3:4)": (896, 1152),
            "Landscape (16:9)": (1280, 720),
            "Square (1:1)": (1024, 1024),
        }
        tk.Label(
            grid,
            text="Aspect Ratio:",
            font=(FONT_FAMILY, 9),
            bg=COLORS["bg_panel"],
            fg=COLORS["text_light"],
        ).grid(row=0, column=2, sticky="w", padx=(12, 0))

        # Determine saved selection from config dimensions
        default_ratio_name = "Portrait (3:4)"
        default_w, default_h = self._aspect_ratios[default_ratio_name]
        try:
            saved_w = int(self.config.get("selfie_width", default_w))
            saved_h = int(self.config.get("selfie_height", default_h))
        except (TypeError, ValueError):
            saved_w, saved_h = default_w, default_h
        dims_to_name = {dims: name for name, dims in self._aspect_ratios.items()}
        saved_ratio = dims_to_name.get((saved_w, saved_h))
        if not saved_ratio:
            custom_ratio = f"Custom ({saved_w}x{saved_h})"
            self._aspect_ratios[custom_ratio] = (saved_w, saved_h)
            saved_ratio = custom_ratio
        self.aspect_var = tk.StringVar(value=saved_ratio)
        aspect_combo = ttk.Combobox(
            grid,
            textvariable=self.aspect_var,
            values=list(self._aspect_ratios.keys()),
            state="readonly",
            width=18,
        )
        aspect_combo.grid(row=0, column=3, padx=5, pady=2, sticky="w")

        # Seed
        tk.Label(
            grid,
            text="Seed:",
            font=(FONT_FAMILY, 9),
            bg=COLORS["bg_panel"],
            fg=COLORS["text_light"],
        ).grid(row=1, column=0, sticky="w")
        self.seed_var = tk.IntVar(value=self.config.get("selfie_seed", -1))
        tk.Entry(
            grid,
            textvariable=self.seed_var,
            width=10,
            bg=COLORS["bg_input"],
            fg=COLORS["text_light"],
            insertbackground=COLORS["text_light"],
            font=(FONT_FAMILY, 9),
        ).grid(row=1, column=1, sticky="w", padx=5, pady=2)

        self.random_seed_var = tk.BooleanVar(
            value=self.config.get("selfie_random_seed", True)
        )
        tk.Checkbutton(
            grid,
            text="Random",
            variable=self.random_seed_var,
            bg=COLORS["bg_panel"],
            fg=COLORS["text_light"],
            selectcolor=COLORS["bg_input"],
            activebackground=COLORS["bg_panel"],
            font=(FONT_FAMILY, 9),
        ).grid(row=1, column=2, columnspan=2, sticky="w", padx=(12, 0))

        save_mode = self.config.get("selfie_output_mode", "")
        if save_mode not in ("source", "custom"):
            save_mode = "source" if self.config.get("use_source_folder", True) else "custom"
        self.output_mode_var = tk.StringVar(value=save_mode)
        self.output_path_var = tk.StringVar(
            value=self.config.get("selfie_output_folder", self.config.get("output_folder", ""))
        )
        tk.Label(
            grid,
            text="Save:",
            font=(FONT_FAMILY, 9),
            bg=COLORS["bg_panel"],
            fg=COLORS["text_light"],
        ).grid(row=2, column=0, sticky="w", pady=(4, 0))
        tk.Radiobutton(
            grid,
            text="Next To Source",
            variable=self.output_mode_var,
            value="source",
            command=self._on_output_mode_changed,
            bg=COLORS["bg_panel"],
            fg=COLORS["text_light"],
            selectcolor=COLORS["bg_input"],
            activebackground=COLORS["bg_panel"],
            font=(FONT_FAMILY, 8),
        ).grid(row=2, column=1, sticky="w", padx=5, pady=(4, 0))
        tk.Radiobutton(
            grid,
            text="Custom Folder",
            variable=self.output_mode_var,
            value="custom",
            command=self._on_output_mode_changed,
            bg=COLORS["bg_panel"],
            fg=COLORS["text_light"],
            selectcolor=COLORS["bg_input"],
            activebackground=COLORS["bg_panel"],
            font=(FONT_FAMILY, 8),
        ).grid(row=2, column=2, columnspan=2, sticky="w", padx=(12, 0), pady=(4, 0))

        # Model selection (moved to right side of Generation Settings)
        models_frame = tk.LabelFrame(
            settings_split,
            text="Step 2 Models",
            font=(FONT_FAMILY, 9, "bold"),
            bg=COLORS["bg_panel"],
            fg=COLORS["text_light"],
        )
        models_frame.pack(side=tk.LEFT, fill=tk.BOTH, padx=(12, 0))
        models_frame.configure(width=360)
        models_frame.pack_propagate(False)

        models_list_container = tk.Frame(models_frame, bg=COLORS["bg_panel"])
        models_list_container.pack(fill=tk.BOTH, expand=True, padx=4, pady=3)
        models_canvas = tk.Canvas(
            models_list_container,
            bg=COLORS["bg_panel"],
            highlightthickness=0,
            borderwidth=0,
            height=66,
        )
        models_scroll = ttk.Scrollbar(
            models_list_container,
            orient=tk.VERTICAL,
            command=models_canvas.yview,
        )
        models_canvas.configure(yscrollcommand=models_scroll.set)
        models_scroll.pack(side=tk.RIGHT, fill=tk.Y)
        models_canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

        models_grid_frame = tk.Frame(models_canvas, bg=COLORS["bg_panel"])
        models_grid_frame.grid_columnconfigure(0, weight=1)
        models_window_id = models_canvas.create_window(
            (0, 0),
            window=models_grid_frame,
            anchor="nw",
        )

        def _on_models_grid_configure(_event):
            bbox = models_canvas.bbox("all")
            models_canvas.configure(scrollregion=bbox)
            if not bbox:
                return
            content_height = bbox[3] - bbox[1]
            viewport_height = models_canvas.winfo_height()
            if content_height <= viewport_height + 2:
                if models_scroll.winfo_ismapped():
                    models_scroll.pack_forget()
            else:
                if not models_scroll.winfo_ismapped():
                    models_scroll.pack(side=tk.RIGHT, fill=tk.Y)

        def _on_models_canvas_configure(event):
            models_canvas.itemconfigure(models_window_id, width=event.width)
            _on_models_grid_configure(None)

        models_grid_frame.bind("<Configure>", _on_models_grid_configure)
        models_canvas.bind("<Configure>", _on_models_canvas_configure)

        saved_models = self.config.get("selfie_selected_models", {})
        for idx, model in enumerate(self._model_options):
            endpoint = model.get("endpoint", "")
            label = model.get("label", endpoint)
            default_checked = (
                endpoint == self.DEFAULT_MODEL_ENDPOINT
                and endpoint not in self.DISABLED_BY_DEFAULT_ENDPOINTS
            )
            checked = bool(saved_models.get(endpoint, default_checked))
            var = tk.BooleanVar(value=checked)
            self._model_vars[endpoint] = var
            tk.Checkbutton(
                models_grid_frame,
                text=label,
                variable=var,
                bg=COLORS["bg_panel"],
                fg=COLORS["text_light"],
                selectcolor=COLORS["bg_input"],
                activebackground=COLORS["bg_panel"],
                font=(FONT_FAMILY, 8),
                anchor="w",
            ).grid(
                row=idx,
                column=0,
                sticky="w",
                padx=(8, 8),
                pady=1,
            )

        self.output_path_row = tk.Frame(content_frame, bg=COLORS["bg_panel"])
        self.output_entry = tk.Entry(
            self.output_path_row,
            textvariable=self.output_path_var,
            bg=COLORS["bg_input"],
            fg=COLORS["text_light"],
            insertbackground=COLORS["text_light"],
            font=(FONT_FAMILY, 9),
            relief=tk.FLAT,
        )
        self.output_entry.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(0, 6))
        self.browse_btn = ttk.Button(
            self.output_path_row,
            text="Browse",
            style=TTK_BTN_SECONDARY,
            command=debounce_command(self._browse_output_folder, key="selfie_browse_output"),
        )
        self.browse_btn.pack(side=tk.LEFT)
        self._update_output_entry_state()

        # Action buttons (btn_frame was packed at top of _build_ui)
        btn_frame = self._btn_frame

        self.generate_btn = create_action_button(
            btn_frame,
            text="Generate Selfie",
            command=debounce_command(self._on_generate, key="selfie_generate"),
            style=TTK_BTN_SUCCESS,
        )
        self.generate_btn.pack(side=tk.LEFT)

        self.save_as_btn = create_action_button(
            btn_frame,
            text="Save As...",
            command=debounce_command(self._on_save_as, key="selfie_save_as"),
            style=TTK_BTN_PRIMARY,
            state=tk.DISABLED,
        )
        self.save_as_btn.pack(side=tk.LEFT, padx=(8, 0))

        self.open_file_btn = create_action_button(
            btn_frame,
            text="Open Image",
            command=debounce_command(self._on_open_result_file, key="selfie_open_file"),
            style=TTK_BTN_SECONDARY,
            state=tk.DISABLED,
        )
        self.open_file_btn.pack(side=tk.LEFT, padx=(6, 0))

        self.open_folder_btn = create_action_button(
            btn_frame,
            text="Open Folder",
            command=debounce_command(self._on_open_result_folder, key="selfie_open_folder"),
            style=TTK_BTN_SECONDARY,
            state=tk.DISABLED,
        )
        self.open_folder_btn.pack(side=tk.LEFT, padx=(6, 0))

        self.send_expand_btn = create_action_button(
            btn_frame,
            text="Send to 2.5 Expand",
            command=debounce_command(self._on_send_to_expand, key="selfie_send_expand"),
            style=TTK_BTN_SUCCESS,
            state=tk.DISABLED,
        )
        self.send_expand_btn.pack(side=tk.LEFT, padx=(6, 0))

        self.status_label = tk.Label(
            btn_frame,
            text="",
            font=(FONT_FAMILY, 9),
            bg=COLORS["bg_panel"],
            fg=COLORS["text_dim"],
            anchor="w",
        )
        self.status_label.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=10)

        # Cancel bar — shown only when generating
        self._cancel_bar = tk.Frame(self, bg=COLORS["bg_panel"])
        # Not packed initially — shown in _set_busy(True)

        self._skip_btn = create_action_button(
            self._cancel_bar,
            text="Skip Model",
            command=debounce_command(self._on_skip_model, key="selfie_skip_model"),
            style=TTK_BTN_PRIMARY,
        )
        self._skip_btn.pack(side=tk.LEFT, padx=(0, 6))

        self._stop_btn = create_action_button(
            self._cancel_bar,
            text="Stop After This",
            command=debounce_command(self._on_stop_after, key="selfie_stop_after"),
            style=TTK_BTN_PRIMARY,
        )
        self._stop_btn.pack(side=tk.LEFT, padx=(0, 6))

        self._abort_btn = create_action_button(
            self._cancel_bar,
            text="Abort All",
            command=debounce_command(self._on_abort_all, key="selfie_abort_all"),
            style=TTK_BTN_DANGER,
        )
        self._abort_btn.pack(side=tk.LEFT)

    def _on_skip_model(self):
        self._cancel_current.set()
        self._skip_btn.config(text="Skipping...", state=tk.DISABLED)

    def _on_stop_after(self):
        self._cancel_all.set()
        self._stop_btn.config(text="Stopping...", state=tk.DISABLED)

    def _on_abort_all(self):
        self._abort_flow.set()
        self._cancel_current.set()  # Also interrupt active model
        self._abort_btn.config(text="Aborting...", state=tk.DISABLED)

    def set_prompt(self, text: str):
        """Set the prompt text (called by Step 1 'Send to Step 2').

        If text is valid JSON matching the template's {json.FIELD} tags,
        resolves the template and shows the result. Otherwise treats
        text as a plain prompt.
        """
        raw_text = (text or "").strip()
        if not raw_text:
            self._handoff_identity_data = None
            self._handoff_resolved = False
            return

        payload = self._extract_handoff_json(raw_text)

        normalized = None
        if payload is not None:
            try:
                from selfie_generator import SelfieGenerator
                # Use template-driven fields instead of hardcoded HANDOFF_JSON_KEYS
                template = self._get_raw_template_text()
                fields = self._extract_json_fields(template)
                normalized = SelfieGenerator.normalize_handoff_identity(
                    payload, required_fields=fields or None
                )
            except Exception:
                normalized = None

        if normalized:
            self._handoff_identity_data = normalized
            self._handoff_resolved = True
            resolved = self._build_handoff_prompt()
            # Show resolved prompt in the template text box
            self.prompt_template_text.config(state=tk.NORMAL)
            self.prompt_template_text.delete("1.0", tk.END)
            self.prompt_template_text.insert("1.0", resolved)
            self.prompt_template_text.config(state=tk.DISABLED)
            self._raw_template = resolved
            self._persist_active_slot_prompt()
            self._save_config_now()
            self._customized_status.config(
                text="Prompt resolved from AI analysis \u2014 ready to generate",
                fg=COLORS["success"],
            )
            return

        # Not valid JSON — treat as plain prompt override
        self._handoff_identity_data = None
        self._handoff_resolved = False
        self.prompt_template_text.config(state=tk.NORMAL)
        self.prompt_template_text.delete("1.0", tk.END)
        self.prompt_template_text.insert("1.0", raw_text)
        self.prompt_template_text.config(state=tk.DISABLED)
        self._raw_template = raw_text
        self._persist_active_slot_prompt()
        self._save_config_now()
        self._customized_status.config(
            text="Plain text prompt loaded (no JSON fields resolved)",
            fg=COLORS["warning"],
        )

    def _extract_handoff_json(self, raw_text: str) -> Optional[dict]:
        """Parse JSON payload even when Step 1 sends wrapper text around it."""
        if not raw_text:
            return None

        candidates = [raw_text]
        first_brace = raw_text.find("{")
        last_brace = raw_text.rfind("}")
        if first_brace != -1 and last_brace != -1 and last_brace > first_brace:
            candidates.append(raw_text[first_brace : last_brace + 1])

        for candidate in candidates:
            try:
                payload = json.loads(candidate.strip())
                if isinstance(payload, dict):
                    return payload
            except Exception:
                continue
        return None

    def _get_selected_dimensions(self) -> tuple:
        """Return (width, height) for the currently selected aspect ratio."""
        return self._aspect_ratios.get(self.aspect_var.get(), self._aspect_ratios["Portrait (3:4)"])

    def _get_prompt_template_text(self) -> str:
        """Return current text box content (may be resolved or raw template)."""
        if not hasattr(self, "prompt_template_text"):
            return ""
        text = self.prompt_template_text.get("1.0", tk.END).strip()
        return text

    def _get_raw_template_text(self) -> str:
        """Return the raw template (with {json.FIELD} tags) from config/default.

        Unlike _get_prompt_template_text(), this always returns the unresolved
        template — even if the text box currently shows a resolved prompt.
        """
        return self._raw_template

    def _update_selfie_slot_button_colors(self):
        active = self._selfie_slot_var.get()
        for i, btn in enumerate(self._slot_buttons, start=1):
            if i == active:
                btn.config(bg=COLORS["accent_blue"], fg=BUTTON_FILLED_TEXT_COLOR)
            else:
                btn.config(bg=COLORS["bg_input"], fg=BUTTON_TEXT_COLOR)

    def _load_current_slot_into_editor(self):
        slot = str(self._selfie_slot_var.get())
        saved_prompts = self.config.setdefault("selfie_saved_prompts", {})
        saved_wildcards = self.config.setdefault("selfie_wildcard_saved_prompts", {})
        saved_titles = self.config.setdefault("selfie_prompt_titles", {})
        prompt_text = str(saved_prompts.get(slot, "") or "")
        wildcard_text = str(saved_wildcards.get(slot, "") or "")
        title_text = str(saved_titles.get(slot, "") or "")
        self._raw_template = prompt_text
        self._slot_title_var.set(title_text)

        self.prompt_template_text.config(state=tk.NORMAL)
        self.prompt_template_text.delete("1.0", tk.END)
        self.prompt_template_text.insert("1.0", prompt_text)
        self.prompt_template_text.config(state=tk.DISABLED)

        self._wildcard_text.config(state=tk.NORMAL)
        self._wildcard_text.delete("1.0", tk.END)
        self._wildcard_text.insert("1.0", wildcard_text)
        self._wildcard_text.config(state=tk.DISABLED)
        self.config["selfie_wildcard_template"] = wildcard_text

        self._update_selfie_slot_button_colors()
        self._customized_status.config(
            text=f"Slot {slot} ready",
            fg=COLORS["text_dim"],
        )

    def _persist_active_slot_prompt(self, persist_title: bool = False):
        slot = str(self._selfie_slot_var.get())
        saved_prompts = self.config.setdefault("selfie_saved_prompts", {})
        saved_wildcards = self.config.setdefault("selfie_wildcard_saved_prompts", {})
        saved_titles = self.config.setdefault("selfie_prompt_titles", {})
        text = self.prompt_template_text.get("1.0", tk.END).strip()
        existing_title = str(saved_titles.get(slot, "") or "").strip()
        title_from_ui = (self._slot_title_var.get() or "").strip()
        if persist_title:
            title = title_from_ui
        else:
            title = existing_title
        saved_prompts[slot] = text
        wildcard_text = ""
        if hasattr(self, "_wildcard_text"):
            wildcard_text = self._wildcard_text.get("1.0", tk.END).strip()
        else:
            wildcard_text = str(saved_wildcards.get(slot, "") or "")
        saved_wildcards[slot] = wildcard_text
        saved_titles[slot] = title
        self._slot_title_var.set(title)
        self.config["selfie_saved_prompts"] = saved_prompts
        self.config["selfie_wildcard_saved_prompts"] = saved_wildcards
        self.config["selfie_prompt_titles"] = saved_titles
        self.config["selfie_wildcard_template"] = wildcard_text
        self.config["selfie_current_prompt_slot"] = int(slot)
        self._raw_template = text

    def _cancel_prompt_template_edit(self):
        self.prompt_template_text.config(state=tk.NORMAL)
        self.prompt_template_text.delete("1.0", tk.END)
        self.prompt_template_text.insert("1.0", self._edit_original_raw_template or self._raw_template)
        self._slot_title_var.set(self._edit_original_slot_title)
        self._set_prompt_template_edit_mode(False)

    def _on_selfie_slot_changed(self, slot: int):
        if self._prompt_template_edit_mode:
            self._cancel_prompt_template_edit()
        if self._wildcard_edit_mode:
            self._cancel_wildcard_template_edit()
        self._selfie_slot_var.set(slot)
        self.config["selfie_current_prompt_slot"] = slot
        self._load_current_slot_into_editor()
        self._save_config_now()
        self.log(f"Step 2 slot changed to {slot}", "info")

    def _set_prompt_template_edit_mode(self, enabled: bool):
        self._prompt_template_edit_mode = enabled
        self.prompt_template_text.config(state=tk.NORMAL if enabled else tk.DISABLED)
        self._slot_title_entry.config(state=tk.NORMAL if enabled else "readonly")
        if enabled:
            self.edit_template_btn.config(
                text="Cancel",
                command=debounce_command(self._cancel_prompt_template_edit, key="selfie_cancel_template"),
            )
        else:
            self.edit_template_btn.config(
                text="Edit Template",
                command=debounce_command(self._on_edit_prompt_template, key="selfie_edit_template"),
            )
        self.save_template_btn.config(state=tk.NORMAL if enabled else tk.DISABLED)

    def _on_edit_prompt_template(self):
        self._edit_original_raw_template = self._raw_template
        self._edit_original_slot_title = self._slot_title_var.get()
        self._set_prompt_template_edit_mode(True)
        # Always show raw template for editing (not resolved text)
        self.prompt_template_text.delete("1.0", tk.END)
        self.prompt_template_text.insert("1.0", self._raw_template)
        self.prompt_template_text.focus_set()
        self.prompt_template_text.mark_set(tk.INSERT, tk.END)

    def _on_save_prompt_template(self):
        text = self._get_prompt_template_text()
        self.prompt_template_text.config(state=tk.NORMAL)
        self.prompt_template_text.delete("1.0", tk.END)
        self.prompt_template_text.insert("1.0", text)
        self._set_prompt_template_edit_mode(False)
        self._persist_active_slot_prompt(persist_title=True)
        self.config["selfie_prompt_template"] = self._raw_template
        self.config["selfie_template_fields"] = self._extract_json_fields(text)
        # Reset handoff state since template changed
        self._handoff_identity_data = None
        self._handoff_resolved = False
        self._customized_status.config(
            text="Template ready \u2014 run AI Analysis in Step 1, then Send to Step 2",
            fg=COLORS["text_dim"],
        )
        self._save_config_now()
        self.log("Selfie prompt template saved", "success")

    def _on_reset_prompt_template(self):
        self.prompt_template_text.config(state=tk.NORMAL)
        self.prompt_template_text.delete("1.0", tk.END)
        self.prompt_template_text.insert("1.0", self.DEFAULT_PROMPT_TEMPLATE)
        self._set_prompt_template_edit_mode(False)
        # Update raw template source of truth
        self._persist_active_slot_prompt(persist_title=True)
        self._handoff_identity_data = None
        self._handoff_resolved = False
        self.config["selfie_prompt_template"] = self._raw_template
        self.config["selfie_template_fields"] = self._extract_json_fields(self.DEFAULT_PROMPT_TEMPLATE)
        self._customized_status.config(
            text="Template ready \u2014 run AI Analysis in Step 1, then Send to Step 2",
            fg=COLORS["text_dim"],
        )
        self._save_config_now()
        self.log("Selfie prompt template reset to default", "info")

    def _on_prompt_mode_changed(self):
        if self._prompt_template_edit_mode:
            self._cancel_prompt_template_edit()
        if self._wildcard_edit_mode:
            self._cancel_wildcard_template_edit()
        self._apply_prompt_mode_ui()
        self._load_current_slot_into_editor()
        mode_label = "Customized (AI Analysis)" if self._prompt_mode_var.get() == "json_handoff" else "Generic (Wildcards)"
        self.log(f"Prompt mode: {mode_label}", "info")

    def _apply_prompt_mode_ui(self):
        """Show/hide widgets based on the active prompt mode."""
        if self._prompt_mode_var.get() == "wildcards":
            self._customized_frame.pack_forget()
            self._wildcard_frame.pack(fill=tk.X, padx=4, pady=(0, 4))
            self._mode_hint_label.config(text="Wildcard template mode active")
        else:
            self._wildcard_frame.pack_forget()
            self._customized_frame.pack(fill=tk.X, padx=4, pady=(0, 4))
            self._mode_hint_label.config(text="AI-analysis template mode active")

    def _set_wildcard_edit_mode(self, enabled: bool):
        self._wildcard_edit_mode = enabled
        self._wildcard_text.config(state=tk.NORMAL if enabled else tk.DISABLED)
        self._slot_title_entry.config(state=tk.NORMAL if enabled else ("readonly" if not self._prompt_template_edit_mode else tk.NORMAL))
        if enabled:
            self._edit_wildcard_btn.config(
                text="Cancel",
                command=debounce_command(self._cancel_wildcard_template_edit, key="selfie_cancel_wildcard"),
            )
        else:
            self._edit_wildcard_btn.config(
                text="Edit Template",
                command=debounce_command(self._on_edit_wildcard_template, key="selfie_edit_wildcard"),
            )
        self._save_wildcard_btn.config(state=tk.NORMAL if enabled else tk.DISABLED)

    def _on_edit_wildcard_template(self):
        self._edit_original_wildcard_template = self._wildcard_text.get("1.0", tk.END).strip()
        self._edit_original_slot_title = self._slot_title_var.get()
        self._set_wildcard_edit_mode(True)
        self._wildcard_text.focus_set()
        self._wildcard_text.mark_set(tk.INSERT, tk.END)

    def _cancel_wildcard_template_edit(self):
        self._wildcard_text.config(state=tk.NORMAL)
        self._wildcard_text.delete("1.0", tk.END)
        self._wildcard_text.insert("1.0", self._edit_original_wildcard_template)
        self._slot_title_var.set(self._edit_original_slot_title)
        self._set_wildcard_edit_mode(False)

    def _on_save_wildcard_template(self):
        text = self._wildcard_text.get("1.0", tk.END).strip()
        self._wildcard_text.config(state=tk.NORMAL)
        self._wildcard_text.delete("1.0", tk.END)
        self._wildcard_text.insert("1.0", text)
        self._set_wildcard_edit_mode(False)
        slot = str(self._selfie_slot_var.get())
        saved_wildcards = self.config.setdefault("selfie_wildcard_saved_prompts", {})
        saved_titles = self.config.setdefault("selfie_prompt_titles", {})
        saved_wildcards[slot] = text
        saved_titles[slot] = (self._slot_title_var.get() or "").strip()
        self.config["selfie_wildcard_saved_prompts"] = saved_wildcards
        self.config["selfie_prompt_titles"] = saved_titles
        self.config["selfie_wildcard_template"] = text
        self._save_config_now()
        self.log("Wildcard template saved", "success")

    def _on_reset_wildcard_template(self):
        self._wildcard_text.config(state=tk.NORMAL)
        self._wildcard_text.delete("1.0", tk.END)
        self._wildcard_text.insert("1.0", self.DEFAULT_WILDCARD_TEMPLATE)
        self._set_wildcard_edit_mode(False)
        slot = str(self._selfie_slot_var.get())
        saved_wildcards = self.config.setdefault("selfie_wildcard_saved_prompts", {})
        saved_wildcards[slot] = self.DEFAULT_WILDCARD_TEMPLATE
        self.config["selfie_wildcard_saved_prompts"] = saved_wildcards
        self.config["selfie_wildcard_template"] = self.DEFAULT_WILDCARD_TEMPLATE
        self._save_config_now()
        self.log("Wildcard template reset to default", "info")

    def _build_handoff_prompt(self) -> str:
        """Resolve {json.FIELD} tags in the template using _handoff_identity_data.

        Leaves {opt1|opt2} wildcard blocks untouched — those resolve per-model at generation.
        """
        if not self._handoff_identity_data:
            return ""
        template = self._get_raw_template_text()

        def _replace_json_tag(match):
            field = match.group(1)
            return self._handoff_identity_data.get(field, f"{{json.{field}}}")

        return re.sub(r"\{json\.([a-zA-Z0-9_]+)\}", _replace_json_tag, template)

    @staticmethod
    def _ref_source_label(source_key: str) -> str:
        labels = {
            "manual_star_ref": "manual ★ Ref",
            "auto_crop": "auto crop fallback",
            "auto_front": "auto front fallback",
            "auto_first_input": "auto first-input fallback",
            "none": "no reference",
        }
        return labels.get(source_key, source_key or "unknown")

    def _on_generate(self):
        if self._busy:
            return
        self._last_batch_result_paths = []
        self._run_ref_path = ""
        self._run_ref_source = "none"

        config = self.get_config()
        api_key = config.get("falai_api_key", "")
        bfl_api_key = config.get("bfl_api_key", "")
        poll_timeout_seconds = config.get("selfie_poll_timeout_seconds", 300)

        # Snapshot run baseline once so all selected models use the same identity reference.
        sim_ref, sim_ref_source = self.image_session.get_effective_similarity_ref()
        image_path = sim_ref.path if sim_ref else self.image_session.active_image_path
        
        if not image_path:
            self.log("No reference image available", "warning")
            return
        self._run_ref_path = image_path
        self._run_ref_source = sim_ref_source if sim_ref else "none"
        ref_name = os.path.basename(image_path)
        self.log(
            f"Step 2 baseline: {ref_name} ({self._ref_source_label(self._run_ref_source)})",
            "info",
        )
        self.log(f"Step 2 baseline path: {image_path}", "debug")

        # For output folder: use original input image's directory when in "source" mode
        # to avoid saving to %TEMP% when intermediates (face crop, polish, outpaint) are active
        if self.output_mode_var.get() == "source":
            ref_entry = self.image_session.reference_entry
            output_source_path = ref_entry.path if ref_entry else image_path
        else:
            output_source_path = image_path

        mode = self._prompt_mode_var.get()
        wildcard_template = None
        if mode == "wildcards":
            raw = self._wildcard_text.get("1.0", tk.END).strip()
            if not raw:
                self.log("Wildcard template is empty", "warning")
                return
            # Save raw template — wildcards will be resolved per-model in _run()
            wildcard_template = raw
            prompt = raw  # placeholder; each model gets a fresh resolution
        elif self._handoff_resolved:
            # Customized mode with {json.FIELD} resolved, wildcards still present
            resolved_text = self._get_prompt_template_text().strip()
            if not resolved_text:
                self.log("Prompt template is empty", "warning")
                return
            wildcard_template = resolved_text  # per-model wildcard resolution
            prompt = resolved_text
        else:
            # Customized mode without handoff — use raw template text as-is
            prompt = self._get_prompt_template_text().strip()
            if not prompt:
                self.log("Prompt is empty", "warning")
                return

        selected_models = self._get_selected_models()
        if not selected_models:
            self.log("Select at least one Step 2 model", "warning")
            return

        # Validate API keys per provider
        needs_fal = any(m.get("provider", "fal") == "fal" for m in selected_models)
        needs_bfl = any(m.get("provider") == "bfl" for m in selected_models)
        if needs_fal and not api_key:
            self.log("fal.ai API key required for selected fal.ai models", "error")
            return
        if needs_bfl and not bfl_api_key:
            self.log("BFL API key required for selected BFL models", "error")
            return

        try:
            output_folder = self._resolve_output_folder(output_source_path)
        except Exception as e:
            self.log(f"Invalid output folder: {e}", "error")
            return

        # Read numeric vars BEFORE setting busy — .get() raises TclError
        # if the entry contains non-numeric text.
        try:
            seed = -1 if self.random_seed_var.get() else self.seed_var.get()
            id_weight = self.id_weight_var.get()
            width, height = self._get_selected_dimensions()
        except (tk.TclError, ValueError):
            self.log("Invalid numeric value in settings", "error")
            return

        self._set_busy(True)

        total_models = len(selected_models)

        def _run():
            try:
                from selfie_generator import SelfieGenerator

                freeimage_key = self.get_config().get("freeimage_api_key")
                gen = SelfieGenerator(api_key, freeimage_key=freeimage_key, bfl_api_key=bfl_api_key)
                gen.set_progress_callback(
                    lambda msg, lvl: self.winfo_toplevel().after(
                        0, lambda m=msg, l=lvl: self.log(m, l)
                    )
                )
                gen.set_cancel_event(self._cancel_current)
                results = []
                failed_models = []
                skipped_models = []

                for idx, model in enumerate(selected_models):
                    # Check abort/stop before each model
                    if self._abort_flow.is_set():
                        self.winfo_toplevel().after(
                            0, lambda: self.log("Generation aborted by user", "warning")
                        )
                        break
                    if self._cancel_all.is_set():
                        self.winfo_toplevel().after(
                            0, lambda: self.log("Stopping after current model (user request)", "warning")
                        )
                        break

                    # Reset per-model cancel event for the new model
                    self._cancel_current.clear()

                    endpoint = model.get("endpoint", "")
                    label = model.get("label", endpoint)

                    # Update status with model progress
                    self.winfo_toplevel().after(
                        0,
                        lambda i=idx, t=total_models, l=label: self.status_label.config(
                            text=f"{i+1}/{t}: {self._truncate_model_label(l)}"
                        ),
                    )

                    # Resolve wildcards per-model for variety
                    if wildcard_template:
                        model_prompt = SelfieGenerator.resolve_wildcards(wildcard_template)
                        self.winfo_toplevel().after(
                            0,
                            lambda l=label, p=model_prompt: self.log(
                                f"[{l}] Resolved prompt: {p[:120]}...", "debug"
                            ),
                        )
                    else:
                        model_prompt = prompt

                    self.winfo_toplevel().after(
                        0,
                        lambda l=label, e=endpoint: self.log(
                            f"Generating with {l} ({e})...", "task"
                        ),
                    )
                    result = gen.generate(
                        image_path=image_path,
                        prompt=model_prompt,
                        output_folder=output_folder,
                        id_weight=id_weight,
                        width=width,
                        height=height,
                        seed=seed,
                        model_endpoint=endpoint,
                        model_label=label,
                        poll_timeout_seconds=poll_timeout_seconds,
                    )
                    if result:
                        results.append((model, result))
                        # Show result immediately in carousel
                        self.winfo_toplevel().after(
                            0,
                            lambda m=model, r=result: self._show_single_result(m, r),
                        )
                    elif self._cancel_current.is_set():
                        # User skipped this model — don't count as failure
                        skipped_models.append(label)
                        self.winfo_toplevel().after(
                            0,
                            lambda l=label: self.log(f"Skipped: {l}", "warning"),
                        )
                    else:
                        failed_models.append(label)

                self.winfo_toplevel().after(
                    0, lambda r=results, fl=failed_models, sk=skipped_models: self._on_complete_batch(r, fl, sk)
                )
            except Exception as e:
                err = str(e)
                self.winfo_toplevel().after(
                    0, lambda: self._on_error(err)
                )

        threading.Thread(target=_run, daemon=True).start()

    def _on_complete(self, result):
        self._set_busy(False)
        if result:
            self._last_result_path = result
            if result not in self._last_batch_result_paths:
                self._last_batch_result_paths.append(result)
            similarity = self._extract_similarity_from_result_path(result)
            self.image_session.add_image(result, "selfie", similarity=similarity)
            message = f"Selfie generated: {os.path.basename(result)}"
            if similarity is not None:
                message = f"Selfie generated (Similarity {similarity}): {os.path.basename(result)}"
            self.log(message, "success")
            self._refresh_result_actions()
        else:
            self.log("Selfie generation failed", "error")

    @staticmethod
    def _truncate_model_label(label: str) -> str:
        text = (label or "").strip()
        if not text:
            return "Model"
        return text[:18]

    @staticmethod
    def _extract_similarity_from_result_path(path: str) -> Optional[str]:
        """Extract similarity token from output filename (e.g. *_sim72_001.png)."""
        if not path:
            return None
        name = os.path.basename(path).lower()
        match = re.search(r"_sim(\d+|na)_\d{3}\.png$", name)
        if not match:
            return None
        token = match.group(1)
        return "n/a" if token == "na" else f"{token}%"

    def _show_single_result(self, model: dict, result: str):
        """Add a single completed result to the carousel immediately."""
        label = model.get("label", model.get("endpoint", "model"))
        similarity = self._extract_similarity_from_result_path(result)
        if self._run_ref_path:
            self.log(
                f"Step 2 compare pair: ref={os.path.basename(self._run_ref_path)} "
                f"target={os.path.basename(result)}",
                "debug",
            )
        self._last_result_path = result
        if result not in self._last_batch_result_paths:
            self._last_batch_result_paths.append(result)
        short_model = self._truncate_model_label(label)
        self.image_session.add_image(
            result,
            "selfie",
            label=short_model,
            similarity=similarity,
        )
        message = f"Selfie generated [{label}]: {os.path.basename(result)}"
        if similarity is not None:
            message = (
                f"Selfie generated [{label}] (Similarity {similarity}): "
                f"{os.path.basename(result)}"
            )
        self.log(message, "success")
        self._refresh_result_actions()

    def _on_complete_batch(self, results, failed_models, skipped_models=None):
        """Final summary after all models have run (results already shown progressively)."""
        self._set_busy(False)
        self._run_ref_path = ""
        self._run_ref_source = "none"
        skipped = skipped_models or []

        if not results and not skipped:
            self.log("Selfie generation failed for all selected models", "error")

        total = len(results) + len(failed_models) + len(skipped)
        if results and (failed_models or skipped):
            self.log(
                f"Batch complete: {len(results)}/{total} succeeded",
                "info",
            )

        if failed_models:
            self.log(
                f"Failed models: {', '.join(failed_models)}",
                "warning",
            )

        if skipped:
            self.log(
                f"Skipped models: {', '.join(skipped)}",
                "info",
            )

    def _on_error(self, error):
        self._set_busy(False)
        self.log(f"Error: {error}", "error")

    def _set_busy(self, busy):
        self._busy = busy
        self.generate_btn.config(
            state=tk.DISABLED if busy else tk.NORMAL,
            text="Generating..." if busy else "Generate Selfie",
        )
        if busy:
            # Clear all cancel events and reset button states
            self._cancel_current.clear()
            self._cancel_all.clear()
            self._abort_flow.clear()
            self._skip_btn.config(text="Skip Model", state=tk.NORMAL)
            self._stop_btn.config(text="Stop After This", state=tk.NORMAL)
            self._abort_btn.config(text="Abort All", state=tk.NORMAL)
            self._cancel_bar.pack(side=tk.BOTTOM, fill=tk.X, padx=8, pady=(0, 4))
        else:
            self._cancel_bar.pack_forget()
        if busy:
            self.save_as_btn.config(state=tk.DISABLED)
            self.open_file_btn.config(state=tk.DISABLED)
            self.open_folder_btn.config(state=tk.DISABLED)
            self.send_expand_btn.config(state=tk.DISABLED)
        else:
            self._refresh_result_actions()
        self.status_label.config(
            text="Processing..." if busy else "",
            fg=COLORS["progress"] if busy else COLORS["text_dim"],
        )

    def _on_output_mode_changed(self):
        self._update_output_entry_state()
        mode_desc = (
            "next to source image"
            if self.output_mode_var.get() == "source"
            else "custom folder"
        )
        self.log(f"Selfie save mode set to {mode_desc}", "info")

    def _update_output_entry_state(self):
        use_custom = self.output_mode_var.get() == "custom"
        if hasattr(self, "output_path_row"):
            if use_custom:
                if not self.output_path_row.winfo_ismapped():
                    self.output_path_row.pack(fill=tk.X, padx=8, pady=(2, 4))
            else:
                if self.output_path_row.winfo_ismapped():
                    self.output_path_row.pack_forget()
        self.output_entry.config(state=tk.NORMAL if use_custom else tk.DISABLED)
        self.browse_btn.config(state=tk.NORMAL if use_custom else tk.DISABLED)

    def _browse_output_folder(self):
        initial_dir = self.output_path_var.get().strip() or os.path.expanduser("~")
        folder = select_directory(
            parent=self.winfo_toplevel(),
            title="Select Selfie Output Folder",
            initialdir=initial_dir,
        )
        if folder:
            self.output_path_var.set(folder)
            self.log(f"Selfie output folder set: {folder}", "success")

    def _resolve_output_folder(self, source_image_path: str) -> str:
        if self.output_mode_var.get() == "custom":
            custom_folder = self.output_path_var.get().strip()
            if not custom_folder:
                raise ValueError("Custom output folder is empty")
            os.makedirs(custom_folder, exist_ok=True)
            return custom_folder
        gen_dir = get_gen_images_folder(source_image_path)
        if not gen_dir:
            raise ValueError("Could not resolve source image folder")
        os.makedirs(gen_dir, exist_ok=True)
        return gen_dir

    def _refresh_result_actions(self):
        has_result = bool(
            self._last_result_path and os.path.isfile(self._last_result_path)
        )
        has_selfies = any(
            entry.source_type == "selfie" and entry.exists
            for entry in self.image_session.images
        )
        self.save_as_btn.config(state=tk.NORMAL if has_result else tk.DISABLED)
        self.open_file_btn.config(state=tk.NORMAL if has_result else tk.DISABLED)
        self.open_folder_btn.config(state=tk.NORMAL if has_result else tk.DISABLED)
        self.send_expand_btn.config(state=tk.NORMAL if has_selfies else tk.DISABLED)

    def _on_send_to_expand(self):
        if not self._on_send_to_expand_cb:
            self.log("Step 2.5 handoff is not configured", "warning")
            return

        selfie_paths = [
            entry.path
            for entry in self.image_session.images
            if entry.source_type == "selfie" and entry.exists
        ]
        if not selfie_paths:
            self.log("No selfie outputs available to send", "warning")
            return

        active_path = self.image_session.active_image_path
        if active_path and active_path not in selfie_paths:
            active_path = self._last_result_path if self._last_result_path in selfie_paths else None

        self._on_send_to_expand_cb(selfie_paths, active_path=active_path)
        self.log(
            f"Sent {len(selfie_paths)} selfie image(s) to Step 2.5 Expand",
            "info",
        )
        if self._notebook_switcher_expand:
            self._notebook_switcher_expand()

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

    def _on_open_result_file(self):
        if not self._last_result_path or not os.path.isfile(self._last_result_path):
            self.log("No generated selfie file to open", "warning")
            return
        self._open_path_in_explorer(self._last_result_path)

    def _on_open_result_folder(self):
        if not self._last_result_path:
            self.log("No generated selfie folder to open", "warning")
            return
        folder = os.path.dirname(self._last_result_path)
        if not folder or not os.path.isdir(folder):
            self.log("Generated selfie folder does not exist", "warning")
            return
        self._open_path_in_explorer(folder)

    def _on_save_as(self):
        if not self._last_result_path or not os.path.isfile(self._last_result_path):
            self.log("No generated selfie to save", "warning")
            return
        initial_dir = os.path.dirname(self._last_result_path)
        initial_file = os.path.basename(self._last_result_path)
        target_path = select_save_file(
            parent=self.winfo_toplevel(),
            title="Save Selfie As",
            initialdir=initial_dir,
            initialfile=initial_file,
            defaultextension=".png",
            filetypes=[
                ("PNG Image", "*.png"),
                ("JPEG Image", "*.jpg;*.jpeg"),
                ("WebP Image", "*.webp"),
                ("All files", "*.*"),
            ],
        )
        if not target_path:
            return
        try:
            if os.path.abspath(target_path) == os.path.abspath(self._last_result_path):
                self.log("Selected path is the same as existing generated file", "info")
                return
            target_dir = os.path.dirname(target_path)
            if target_dir:
                os.makedirs(target_dir, exist_ok=True)
            shutil.copy2(self._last_result_path, target_path)
            self.log(f"Saved copy: {target_path}", "success")
        except Exception as e:
            self.log(f"Save failed: {e}", "error")

    def _load_model_options(self) -> List[dict]:
        try:
            from selfie_generator import SelfieGenerator
            return SelfieGenerator.get_available_models()
        except Exception:
            return [
                {
                    "endpoint": "openai/gpt-image-2/edit",
                    "label": "GPT Image 2 Edit",
                    "api_url": "https://fal.ai/models/openai/gpt-image-2/edit/api",
                },
                {
                    "endpoint": "fal-ai/nano-banana-2/edit",
                    "label": "Nano Banana 2 Edit",
                    "api_url": "https://fal.ai/models/fal-ai/nano-banana-2/edit/api",
                },
            ]

    def _get_selected_models(self) -> List[dict]:
        selected = []
        for model in self._model_options:
            endpoint = model.get("endpoint", "")
            var = self._model_vars.get(endpoint)
            if var and var.get():
                selected.append(model)
        return selected

    def get_config_updates(self) -> dict:
        width, height = self._get_selected_dimensions()
        self._persist_active_slot_prompt()
        # Save the raw template (not the resolved view)
        raw_template = self._get_raw_template_text()
        template_fields = self._extract_json_fields(raw_template)
        return {
            "composer_gender": self.gender_var.get(),
            "composer_camera_style": self.style_var.get(),
            "selfie_id_weight": self.id_weight_var.get(),
            "selfie_width": width,
            "selfie_height": height,
            "selfie_seed": self.seed_var.get(),
            "selfie_random_seed": self.random_seed_var.get(),
            "selfie_output_mode": self.output_mode_var.get(),
            "selfie_output_folder": self.output_path_var.get().strip(),
            "selfie_prompt_template": raw_template,
            "selfie_current_prompt_slot": self._selfie_slot_var.get(),
            "selfie_saved_prompts": self.config.get("selfie_saved_prompts", {}),
            "selfie_wildcard_saved_prompts": self.config.get("selfie_wildcard_saved_prompts", {}),
            "selfie_prompt_titles": self.config.get("selfie_prompt_titles", {}),
            "selfie_template_fields": template_fields,
            "selfie_selected_models": {
                endpoint: bool(var.get())
                for endpoint, var in self._model_vars.items()
            },
            "selfie_prompt_mode": self._prompt_mode_var.get(),
            "selfie_wildcard_template": self._wildcard_text.get("1.0", tk.END).strip(),
        }
