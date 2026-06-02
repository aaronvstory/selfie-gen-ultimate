"""Prep Tab — Vision analysis of portrait images."""

import tkinter as tk
from tkinter import ttk
import threading
from typing import Callable, Optional

from ..theme import (
    COLORS,
    FONT_FAMILY,
    FONT_MONO,
    TTK_BTN_COMPACT,
    TTK_BTN_DANGER,
    TTK_BTN_PRIMARY,
    TTK_BTN_SECONDARY,
    TTK_BTN_SUCCESS,
    debounce_command,
)
from ..image_state import ImageSession
from log_utils import format_exception_detail


class PrepTab(tk.Frame):
    """Tab 1: Analyze portrait images using vision AI."""

    BUILTIN_MODELS = [
        ("Seed 1.6 Flash", "bytedance-seed/seed-1.6-flash"),
        ("GPT-4o Mini", "openai/gpt-4o-mini"),
        ("Claude 3.5 Haiku", "anthropic/claude-3.5-haiku"),
        ("Gemini 2.0 Flash", "google/gemini-2.0-flash-001"),
    ]
    DEFAULT_VISION_PROMPT = (
        "You are a portrait photo analyzer for AI image generation. "
        "Analyze the provided portrait image and generate a detailed prompt that "
        "describes the person's physical appearance, facial features, expression, "
        "hair, clothing, pose, and lighting for a static portrait photo. "
        "DO NOT mention video, animation, or movement. Focus strictly on physical "
        "identity, expression, and lighting to be used as an image generation prompt. "
        "Return ONLY the prompt text, no explanations or formatting."
    )
    PRIMARY_TEXT_COLOR = "#111111"

    def __init__(
        self,
        parent,
        image_session: ImageSession,
        config: dict,
        config_getter: Callable[[], dict],
        log_callback: Callable[[str, str], None],
        prompt_writer: Callable[[str], None],
        config_saver: Optional[Callable[[], None]] = None,
        selfie_prompt_writer: Optional[Callable[[str], None]] = None,
        **kwargs,
    ):
        """
        Args:
            parent: Parent widget
            image_session: Shared image session state
            config: Initial config dict
            config_getter: Function returning current config
            log_callback: log(message, level)
            prompt_writer: Function to write text into the active prompt slot
            config_saver: Function to persist config to disk immediately
            selfie_prompt_writer: Write composed prompt into Step 2's text box
        """
        super().__init__(parent, bg=COLORS["bg_panel"], **kwargs)
        self.image_session = image_session
        self.config = config
        self.get_config = config_getter
        self.log = log_callback
        self.prompt_writer = prompt_writer
        self._config_saver = config_saver
        self._selfie_prompt_writer = selfie_prompt_writer
        self._selfie_config_getter = None  # set via set_selfie_config_getter
        self._notebook_switcher_selfie = None  # set via set_notebook_switcher_selfie
        self._busy = False
        self._prompt_edit_mode = False
        self._last_result = ""
        self._default_vision_prompt = self._resolve_default_vision_prompt()

        # Build combined model list: built-in + custom from config
        self._custom_models = list(config.get("openrouter_custom_models", []))
        self._all_models = list(self.BUILTIN_MODELS)
        for endpoint in self._custom_models:
            self._all_models.append((endpoint, endpoint))

        self._build_ui()

    def set_selfie_prompt_writer(self, writer: Callable[[str], None]):
        """Set the callback that writes composed prompts into Step 2."""
        self._selfie_prompt_writer = writer

    def set_selfie_config_getter(self, getter: Callable[[], dict]):
        """Set a getter for live Step 2 composer options (gender, style)."""
        self._selfie_config_getter = getter

    def set_notebook_switcher_selfie(self, switcher: Callable[[], None]):
        """Set callback to switch notebook to the Selfie tab."""
        self._notebook_switcher_selfie = switcher

    def _resolve_default_vision_prompt(self) -> str:
        """Resolve shared default prompt from VisionAnalyzer when available."""
        try:
            from vision_analyzer import VisionAnalyzer

            return VisionAnalyzer.DEFAULT_SYSTEM_PROMPT
        except Exception:
            return self.DEFAULT_VISION_PROMPT

    def _build_ui(self):
        # Model selection
        model_frame = tk.Frame(self, bg=COLORS["bg_panel"])
        model_frame.pack(fill=tk.X, padx=10, pady=5)

        tk.Label(
            model_frame,
            text="Vision Model:",
            font=(FONT_FAMILY, 9),
            bg=COLORS["bg_panel"],
            fg=COLORS["text_light"],
        ).pack(side=tk.LEFT)

        self.model_var = tk.StringVar()
        model_names = [m[0] for m in self._all_models]
        self.model_combo = ttk.Combobox(
            model_frame,
            textvariable=self.model_var,
            values=model_names,
            state="readonly",
            width=30,
        )
        saved_model = self.config.get(
            "openrouter_model", "bytedance-seed/seed-1.6-flash"
        )
        for i, (_name, endpoint) in enumerate(self._all_models):
            if endpoint == saved_model:
                self.model_combo.current(i)
                break
        else:
            self.model_combo.current(0)
        self.model_combo.pack(side=tk.LEFT, padx=(5, 0))

        self.remove_model_btn = ttk.Button(
            model_frame,
            text="Remove",
            style=TTK_BTN_DANGER,
            command=debounce_command(self._on_remove_model, key="prep_remove_model"),
        )
        self.remove_model_btn.pack(side=tk.LEFT, padx=(5, 0))

        # Custom model entry
        custom_frame = tk.Frame(self, bg=COLORS["bg_panel"])
        custom_frame.pack(fill=tk.X, padx=10, pady=(0, 5))

        tk.Label(
            custom_frame,
            text="Add Custom:",
            font=(FONT_FAMILY, 9),
            bg=COLORS["bg_panel"],
            fg=COLORS["text_dim"],
        ).pack(side=tk.LEFT)

        self.custom_model_var = tk.StringVar()
        self.custom_model_entry = tk.Entry(
            custom_frame,
            textvariable=self.custom_model_var,
            bg=COLORS["bg_input"],
            fg=COLORS["text_light"],
            insertbackground=COLORS["text_light"],
            font=(FONT_FAMILY, 9),
            width=28,
        )
        self.custom_model_entry.pack(
            side=tk.LEFT, padx=(5, 0), fill=tk.X, expand=True
        )
        self.custom_model_entry.insert(0, "org/model-name")
        self.custom_model_entry.bind(
            "<FocusIn>", self._on_custom_entry_focus
        )

        self.add_model_btn = ttk.Button(
            custom_frame,
            text="Add",
            style=TTK_BTN_PRIMARY,
            command=debounce_command(self._on_add_model, key="prep_add_model"),
        )
        self.add_model_btn.pack(side=tk.LEFT, padx=(5, 0))

        # Editable system prompt (persisted)
        tk.Label(
            self,
            text="Vision Prompt (System):",
            font=(FONT_FAMILY, 9, "bold"),
            bg=COLORS["bg_panel"],
            fg=COLORS["text_light"],
        ).pack(fill=tk.X, padx=10, pady=(2, 2))

        prompt_frame = tk.Frame(self, bg=COLORS["bg_main"])
        prompt_frame.pack(fill=tk.X, padx=10, pady=(0, 5))

        self.system_prompt_text = tk.Text(
            prompt_frame,
            wrap=tk.WORD,
            height=10,
            bg=COLORS["bg_main"],
            fg=COLORS["text_light"],
            # Unified prompt font (user request 2026-05-21): every
            # prompt-text Text widget in the app uses (FONT_FAMILY, 10)
            # to match the video-tab positive + negative prompt
            # editors. Was (FONT_MONO, 9) — visibly smaller + mono.
            font=(FONT_FAMILY, 10),
            insertbackground=COLORS["text_light"],
            padx=5,
            pady=5,
            borderwidth=0,
            highlightthickness=0,
        )
        prompt_scroll = ttk.Scrollbar(
            prompt_frame, command=self.system_prompt_text.yview
        )
        self.system_prompt_text.config(yscrollcommand=prompt_scroll.set)
        prompt_scroll.pack(side=tk.RIGHT, fill=tk.Y)
        self.system_prompt_text.pack(side=tk.LEFT, fill=tk.X, expand=True)

        saved_prompt = self.config.get(
            "openrouter_vision_system_prompt", self._default_vision_prompt
        )
        self.system_prompt_text.insert(
            "1.0", (saved_prompt or self._default_vision_prompt).strip()
        )
        self.system_prompt_text.config(state=tk.DISABLED)

        prompt_actions = tk.Frame(self, bg=COLORS["bg_panel"])
        prompt_actions.pack(fill=tk.X, padx=10, pady=(0, 4))

        self.edit_prompt_btn = ttk.Button(
            prompt_actions,
            text="Edit Prompt",
            style=TTK_BTN_SECONDARY,
            command=debounce_command(self._on_edit_system_prompt, key="prep_edit_prompt"),
        )
        self.edit_prompt_btn.pack(side=tk.LEFT)

        self.save_prompt_btn = ttk.Button(
            prompt_actions,
            text="Save Prompt",
            style=TTK_BTN_PRIMARY,
            command=debounce_command(self._on_save_system_prompt, key="prep_save_prompt"),
            state=tk.DISABLED,
        )
        self.save_prompt_btn.pack(side=tk.LEFT, padx=(5, 0))

        self.reset_prompt_btn = ttk.Button(
            prompt_actions,
            text="Reset Prompt",
            style=TTK_BTN_DANGER,
            command=debounce_command(self._on_reset_system_prompt, key="prep_reset_prompt"),
        )
        self.reset_prompt_btn.pack(side=tk.LEFT)

        # Analyze button
        btn_frame = tk.Frame(self, bg=COLORS["bg_panel"])
        btn_frame.pack(fill=tk.X, padx=10, pady=10)

        self.analyze_btn = ttk.Button(
            btn_frame,
            text="Analyze Image",
            style=TTK_BTN_PRIMARY,
            command=debounce_command(self._on_analyze, key="prep_analyze"),
        )
        self.analyze_btn.pack(anchor="center")

        self.status_label = tk.Label(
            btn_frame,
            text="",
            font=(FONT_FAMILY, 9),
            bg=COLORS["bg_panel"],
            fg=COLORS["text_dim"],
        )
        self.status_label.pack(anchor="center", pady=(4, 0))

        # Write to prompt button (packed BOTTOM first to guarantee space)
        write_frame = tk.Frame(self, bg=COLORS["bg_panel"])
        write_frame.pack(fill=tk.X, padx=10, pady=(0, 10), side=tk.BOTTOM)

        self.write_btn = ttk.Button(
            write_frame,
            text="Send to Selfie Gen \u2192 Custom JSON",
            style=TTK_BTN_SUCCESS,
            command=debounce_command(self._on_send_to_step2, key="prep_send_step2"),
            state=tk.DISABLED,
        )
        self.write_btn.pack(anchor="center")

        # Result display (expands to fill remaining space)
        tk.Label(
            self,
            text="Analysis Result:",
            font=(FONT_FAMILY, 9, "bold"),
            bg=COLORS["bg_panel"],
            fg=COLORS["text_light"],
        ).pack(fill=tk.X, padx=10, pady=(5, 2))

        result_frame = tk.Frame(self, bg=COLORS["bg_main"])
        result_frame.pack(
            fill=tk.BOTH, expand=True, padx=10, pady=(0, 5)
        )

        self.result_text = tk.Text(
            result_frame,
            wrap=tk.WORD,
            height=6,
            bg=COLORS["bg_main"],
            fg=COLORS["text_light"],
            font=(FONT_MONO, 9),
            insertbackground=COLORS["text_light"],
            padx=5,
            pady=5,
            borderwidth=0,
            highlightthickness=0,
        )
        scroll = ttk.Scrollbar(result_frame, command=self.result_text.yview)
        self.result_text.config(yscrollcommand=scroll.set)
        scroll.pack(side=tk.RIGHT, fill=tk.Y)
        self.result_text.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

    def _get_selected_model_endpoint(self) -> str:
        idx = self.model_combo.current()
        if 0 <= idx < len(self._all_models):
            return self._all_models[idx][1]
        return self._all_models[0][1]

    def _save_config_now(self):
        """Update shared config dict and persist to disk."""
        self.config.update(self.get_config_updates())
        if self._config_saver:
            self._config_saver()

    def _refresh_model_combo(self):
        """Rebuild combobox values from _all_models."""
        names = [m[0] for m in self._all_models]
        self.model_combo["values"] = names

    def _on_add_model(self):
        endpoint = self.custom_model_var.get().strip()
        if not endpoint or endpoint == "org/model-name":
            self.log("Enter a model endpoint (e.g. meta-llama/llama-3-70b)", "warning")
            return
        if "/" not in endpoint or not endpoint.isascii() or " " in endpoint:
            self.log("Model endpoint should be org/model format (ASCII, no spaces)", "warning")
            return
        # Check for duplicates
        for _name, ep in self._all_models:
            if ep == endpoint:
                self.log(f"Model already in list: {endpoint}", "warning")
                return
        self._custom_models.append(endpoint)
        self._all_models.append((endpoint, endpoint))
        self._refresh_model_combo()
        # Select the newly added model
        self.model_combo.current(len(self._all_models) - 1)
        self.custom_model_var.set("")
        self._save_config_now()
        self.log(f"Added custom model: {endpoint}", "success")

    def _on_remove_model(self):
        idx = self.model_combo.current()
        if idx < 0:
            return
        _name, endpoint = self._all_models[idx]
        # Only allow removing custom models
        if endpoint not in self._custom_models:
            self.log("Cannot remove built-in models", "warning")
            return
        self._custom_models.remove(endpoint)
        self._all_models.pop(idx)
        self._refresh_model_combo()
        self.model_combo.current(0)
        self._save_config_now()
        self.log(f"Removed custom model: {endpoint}", "info")

    def _on_custom_entry_focus(self, _event):
        if self.custom_model_var.get() == "org/model-name":
            self.custom_model_var.set("")

    def _get_system_prompt_text(self) -> str:
        prompt = self.system_prompt_text.get("1.0", tk.END).strip()
        return prompt if prompt else self._default_vision_prompt

    def _set_prompt_edit_mode(self, enabled: bool):
        self._prompt_edit_mode = enabled
        self.system_prompt_text.config(state=tk.NORMAL if enabled else tk.DISABLED)
        self.edit_prompt_btn.config(state=tk.DISABLED if enabled else tk.NORMAL)
        self.save_prompt_btn.config(state=tk.NORMAL if enabled else tk.DISABLED)

    def _on_edit_system_prompt(self):
        self._set_prompt_edit_mode(True)
        self.system_prompt_text.focus_set()
        self.system_prompt_text.mark_set(tk.INSERT, tk.END)

    def _on_save_system_prompt(self):
        prompt = self._get_system_prompt_text()
        self.system_prompt_text.config(state=tk.NORMAL)
        self.system_prompt_text.delete("1.0", tk.END)
        self.system_prompt_text.insert("1.0", prompt)
        self._save_config_now()
        self._set_prompt_edit_mode(False)
        self.log("Vision prompt saved", "success")

    def _on_reset_system_prompt(self):
        self.system_prompt_text.config(state=tk.NORMAL)
        self.system_prompt_text.delete("1.0", tk.END)
        self.system_prompt_text.insert("1.0", self._default_vision_prompt)
        self._save_config_now()
        self._set_prompt_edit_mode(False)
        self.log("Vision prompt reset to default", "info")

    def _on_analyze(self):
        if self._busy:
            return
        if self._prompt_edit_mode:
            self.log("Save the Vision Prompt before analyzing", "warning")
            return

        api_key = self.get_config().get("openrouter_api_key", "").strip()
        if not api_key:
            self.log("OpenRouter API key required — set it in the bottom bar", "error")
            return

        image_path = self.image_session.active_image_path
        if not image_path:
            self.log("No image selected in carousel", "warning")
            return

        self._set_busy(True)
        self.write_btn.config(state=tk.DISABLED)
        model = self._get_selected_model_endpoint()
        system_prompt = self._get_system_prompt_text()
        self._save_config_now()

        def _run():
            try:
                from vision_analyzer import VisionAnalyzer

                # If the user hasn't customized the system prompt, auto-generate
                # it from the Selfie template's {json.FIELD} tags so the Vision
                # API returns exactly the fields the template needs.
                effective_prompt = system_prompt
                if effective_prompt == self._default_vision_prompt:
                    template_fields = self.get_config().get("selfie_template_fields")
                    if template_fields and isinstance(template_fields, list):
                        effective_prompt = VisionAnalyzer.build_json_system_prompt(
                            template_fields
                        )

                analyzer = VisionAnalyzer(
                    api_key, model, system_prompt=effective_prompt
                )
                analyzer.set_progress_callback(
                    lambda msg, lvl: self.winfo_toplevel().after(
                        0, lambda m=msg, l=lvl: self.log(m, l)
                    )
                )
                result = analyzer.analyze_image(image_path)
                self.winfo_toplevel().after(
                    0, lambda: self._on_analyze_complete(result)
                )
            except Exception as e:
                err = format_exception_detail(e)
                self.winfo_toplevel().after(
                    0, lambda: self._on_analyze_error(err)
                )

        threading.Thread(target=_run, daemon=True).start()

    def _on_analyze_complete(self, result):
        self._set_busy(False)
        if result and result.get("prompt"):
            self._last_result = result["prompt"]
            self.result_text.delete("1.0", tk.END)
            self.result_text.insert("1.0", self._last_result)
            self.write_btn.config(state=tk.NORMAL)
            self.log("Analysis complete — review result below", "success")
        else:
            self._last_result = ""
            self.result_text.delete("1.0", tk.END)
            self.write_btn.config(state=tk.DISABLED)
            self.log("Analysis returned no result", "warning")

    def _on_analyze_error(self, error: str):
        self._set_busy(False)
        self.write_btn.config(state=tk.DISABLED)
        self.log(f"Analysis error: {error}", "error")

    def _on_send_to_step2(self):
        """Send raw Vision output directly to Step 2.

        SelfieTab.set_prompt() handles JSON parsing and template resolution —
        no need to wrap via SelfiePromptComposer.
        """
        description = self.result_text.get("1.0", tk.END).strip()
        if not description:
            return

        if self._selfie_prompt_writer:
            self._selfie_prompt_writer(description)
            self.log("Prompt sent to Selfie Gen", "success")
            if self._notebook_switcher_selfie:
                self._notebook_switcher_selfie()
        else:
            self.prompt_writer(description)
            self.log("Prompt written to active slot", "success")

    def _set_busy(self, busy: bool):
        self._busy = busy
        self.analyze_btn.config(
            state=tk.DISABLED if busy else tk.NORMAL,
            text="Analyzing..." if busy else "Analyze Image",
        )
        self.status_label.config(
            text="Processing..." if busy else "",
            fg=COLORS["progress"] if busy else COLORS["text_dim"],
        )

    def get_config_updates(self) -> dict:
        """Return config values to save (openrouter_api_key managed by bottom bar)."""
        return {
            "openrouter_model": self._get_selected_model_endpoint(),
            "openrouter_custom_models": list(self._custom_models),
            "openrouter_vision_system_prompt": self._get_system_prompt_text(),
        }
