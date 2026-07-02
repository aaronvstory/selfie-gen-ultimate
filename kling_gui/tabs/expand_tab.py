"""Step 2.5 Expand tab - gated expansion between selfie generation and video."""

import os
import threading
import tkinter as tk
from pathlib import Path
from tkinter import messagebox, ttk
from typing import Callable, List, Optional

from path_utils import get_gen_images_folder
from automation.config import get_outpaint_fal_timeout_seconds

from ..image_state import ImageSession, SIMILARITY_PASS_THRESHOLD, parse_similarity_score
from ..theme import (
    COLORS,
    FONT_FAMILY,
    TTK_BTN_SECONDARY,
    TTK_BTN_SUCCESS,
    TTK_BTN_WORKFLOW,
    debounce_command,
    macos_widget_pad,
)


class ExpandTab(tk.Frame):
    """Tab 2.5: expand selfie outputs with similarity gate and Step 3 handoff."""

    def __init__(
        self,
        parent,
        image_session: ImageSession,
        config: dict,
        config_getter: Callable[[], dict],
        log_callback: Callable[[str, str], None],
        on_send_to_video: Optional[Callable[[List[str]], None]] = None,
        notebook_switcher_video: Optional[Callable[[], None]] = None,
        config_saver: Optional[Callable[[], None]] = None,
        **kwargs,
    ):
        super().__init__(parent, bg=COLORS["bg_panel"], **kwargs)
        self.image_session = image_session
        self.config = config
        self.get_config = config_getter
        self.log = log_callback
        self._on_send_to_video = on_send_to_video
        self._notebook_switcher_video = notebook_switcher_video
        self._config_saver = config_saver

        self._busy = False
        self._candidate_entries = []
        self._expanded_paths: List[str] = []

        self._auto_switch_var = tk.BooleanVar(
            value=self.config.get("expand25_auto_switch", True)
        )
        self._expand_mode_var = tk.StringVar(
            value=self.config.get("outpaint_expand_mode", "percentage")
        )
        self._pct_var = tk.IntVar(
            value=self.config.get("outpaint_expand_percentage", 30)
        )
        self._top_var = tk.IntVar(value=self.config.get("outpaint_expand_top", 140))
        self._bottom_var = tk.IntVar(value=self.config.get("outpaint_expand_bottom", 140))
        self._left_var = tk.IntVar(value=self.config.get("outpaint_expand_left", 140))
        self._right_var = tk.IntVar(value=self.config.get("outpaint_expand_right", 140))
        self._format_var = tk.StringVar(value=self.config.get("outpaint_format", "png"))
        self._composite_mode_var = tk.StringVar(
            # Default "none" (raw AI output) for Step 2.5 expand —
            # user-requested ship default. Reads ONLY from the section-
            # specific key. The previous back-compat read fallback to
            # the shared ``outpaint_composite_mode`` was the other half
            # of the bug where Step 0's user-chosen "preserve_seamless"
            # silently became "none" — Step 2.5 inherited Step 0's
            # value, then wrote it back, then on the next session
            # Step 0 saw the inherited "none" and showed it.
            value=self.config.get(
                "automation_selfie_expand_composite_mode", "none",
            ),
        )
        # Default = "fal" everywhere (user direction 2026-05-22 final).
        # The Phase A revert that restored the BFL-if-key-present default
        # was over-broad: the user only wanted the macOS composite/feather
        # changes reverted (the LANCZOS + 16px tolerance edits in
        # outpaint_generator.py, already rolled back by d48bbc8). The
        # provider default itself stays "fal" — switching between
        # providers is a one-click dropdown change.
        self._provider_var = tk.StringVar(
            value=self.config.get("outpaint_provider", "fal")
        )

        self._build_ui()
        self.refresh_candidates(select_all_default=True)

    def _build_ui(self):
        candidate_frame = tk.LabelFrame(
            self,
            text="Step 2 Selfie Candidates",
            font=(FONT_FAMILY, 9, "bold"),
            bg=COLORS["bg_panel"],
            fg=COLORS["text_light"],
        )
        candidate_frame.pack(fill=tk.BOTH, expand=True, padx=10, pady=(8, 4))

        self._candidate_list = tk.Listbox(
            candidate_frame,
            # SINGLE select: clicking a row sets the active
            # carousel image (and that's what the Expand button
            # operates on). Multi-select made no sense — we only
            # ever expand one selfie at a time (user request,
            # PR #41).
            selectmode=tk.SINGLE,
            bg=COLORS["bg_input"],
            fg=COLORS["text_light"],
            font=(FONT_FAMILY, 9),
            relief=tk.FLAT,
            height=6,
            exportselection=False,
        )
        self._candidate_list.bind(
            "<<ListboxSelect>>", self._on_candidate_clicked
        )
        candidate_scroll = ttk.Scrollbar(
            candidate_frame, orient=tk.VERTICAL, command=self._candidate_list.yview
        )
        self._candidate_list.configure(yscrollcommand=candidate_scroll.set)
        self._candidate_list.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, padx=(6, 0), pady=6)
        candidate_scroll.pack(side=tk.RIGHT, fill=tk.Y, padx=(0, 6), pady=6)

        candidate_actions = tk.Frame(self, bg=COLORS["bg_panel"])
        candidate_actions.pack(fill=tk.X, padx=10, pady=(0, 4))
        ttk.Button(
            candidate_actions,
            text="Refresh Selfies",
            style=TTK_BTN_SECONDARY,
            command=debounce_command(lambda: self.refresh_candidates(select_all_default=True), key="expand_refresh_candidates"),
        ).pack(side=tk.LEFT)
        self._candidate_meta = tk.Label(
            candidate_actions,
            text="",
            font=(FONT_FAMILY, 9),
            bg=COLORS["bg_panel"],
            fg=COLORS["text_dim"],
            anchor="w",
        )
        self._candidate_meta.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=8)

        # Prompt editor — Phase G of polish/v2.3 (2026-05-22) gave
        # this section its own config key (``selfie_expand_prompt``)
        # so Step 2.5 can carry a scene-matching prompt without
        # bleeding into Step 0 face-crop expand or the standalone
        # Outpaint tab. Fallback: when ``selfie_expand_prompt`` is
        # empty / missing, read the legacy shared ``outpaint_prompt``
        # so users with old configs see their saved prompt populated
        # on first launch.
        prompt_frame = tk.LabelFrame(
            self,
            text="Step 2.5 Expand Prompt (sent to fal.ai / BFL)",
            font=(FONT_FAMILY, 9, "bold"),
            bg=COLORS["bg_panel"],
            fg=COLORS["text_light"],
        )
        prompt_frame.pack(fill=tk.X, padx=10, pady=(0, 4))
        self._prompt_text = tk.Text(
            prompt_frame,
            height=3,
            wrap=tk.WORD,
            bg=COLORS["bg_input"],
            fg=COLORS["text_light"],
            # Unified prompt font (user request 2026-05-21): every
            # prompt-text Text widget in the app uses (FONT_FAMILY, 10)
            # to match the video-tab positive + negative prompt
            # editors. Was (FONT_FAMILY, 9) — visibly smaller.
            font=(FONT_FAMILY, 10),
            relief=tk.FLAT,
            insertbackground=COLORS["text_light"],
        )
        # Phase G fallback chain: section-specific key first, then
        # legacy shared key, then empty string. Codex P1 on 0967564
        # (2026-05-22): key-presence semantics, NOT truthiness. An
        # explicitly-saved empty ``selfie_expand_prompt`` is a valid
        # intentional value (user cleared the prompt) and must NOT
        # be silently replaced by the legacy shared prompt.
        _section_prompt = self.config.get("selfie_expand_prompt")
        if isinstance(_section_prompt, str):
            _initial_prompt = _section_prompt
        else:
            _initial_prompt = str(
                self.config.get("outpaint_prompt", "") or ""
            )
        self._prompt_text.insert("1.0", _initial_prompt)
        self._prompt_text.pack(fill=tk.X, padx=6, pady=6)

        settings_frame = tk.LabelFrame(
            self,
            text="Expand Settings",
            font=(FONT_FAMILY, 9, "bold"),
            bg=COLORS["bg_panel"],
            fg=COLORS["text_light"],
        )
        settings_frame.pack(fill=tk.X, padx=10, pady=4)

        mode_row = tk.Frame(settings_frame, bg=COLORS["bg_panel"])
        mode_row.pack(fill=tk.X, padx=6, pady=(4, 2))
        tk.Radiobutton(
            mode_row,
            text="Percentage",
            variable=self._expand_mode_var,
            value="percentage",
            command=self._apply_mode_ui,
            bg=COLORS["bg_panel"],
            fg=COLORS["text_light"],
            selectcolor=COLORS["bg_input"],
            activebackground=COLORS["bg_panel"],
            font=(FONT_FAMILY, 9),
            **macos_widget_pad(),
        ).pack(side=tk.LEFT)
        tk.Radiobutton(
            mode_row,
            text="Pixels",
            variable=self._expand_mode_var,
            value="pixels",
            command=self._apply_mode_ui,
            bg=COLORS["bg_panel"],
            fg=COLORS["text_light"],
            selectcolor=COLORS["bg_input"],
            activebackground=COLORS["bg_panel"],
            font=(FONT_FAMILY, 9),
            **macos_widget_pad(),
        ).pack(side=tk.LEFT, padx=(12, 0))
        # Full-res modes keep the original at native resolution (borders
        # upscaled). They reuse the Percentage % field as the zoom-out amount.
        tk.Radiobutton(
            mode_row,
            text="% Full-res",
            variable=self._expand_mode_var,
            value="percentage_fullres",
            command=self._apply_mode_ui,
            bg=COLORS["bg_panel"],
            fg=COLORS["text_light"],
            selectcolor=COLORS["bg_input"],
            activebackground=COLORS["bg_panel"],
            font=(FONT_FAMILY, 9),
            **macos_widget_pad(),
        ).pack(side=tk.LEFT, padx=(12, 0))
        tk.Radiobutton(
            mode_row,
            text="3:4 Full-res",
            variable=self._expand_mode_var,
            value="three_four_fullres",
            command=self._apply_mode_ui,
            bg=COLORS["bg_panel"],
            fg=COLORS["text_light"],
            selectcolor=COLORS["bg_input"],
            activebackground=COLORS["bg_panel"],
            font=(FONT_FAMILY, 9),
            **macos_widget_pad(),
        ).pack(side=tk.LEFT, padx=(12, 0))

        self._pct_frame = tk.Frame(settings_frame, bg=COLORS["bg_panel"])
        self._pct_frame.pack(fill=tk.X, padx=6, pady=(0, 4))
        tk.Label(
            self._pct_frame,
            text="Expand all sides:",
            font=(FONT_FAMILY, 9),
            bg=COLORS["bg_panel"],
            fg=COLORS["text_light"],
        ).pack(side=tk.LEFT)
        tk.Scale(
            self._pct_frame,
            from_=5,
            to=100,
            resolution=5,
            orient=tk.HORIZONTAL,
            variable=self._pct_var,
            bg=COLORS["bg_panel"],
            fg=COLORS["text_light"],
            troughcolor=COLORS["bg_input"],
            highlightthickness=0,
            length=200,
            font=(FONT_FAMILY, 9),
        ).pack(side=tk.LEFT, padx=(6, 3))
        tk.Label(
            self._pct_frame,
            text="%",
            font=(FONT_FAMILY, 9),
            bg=COLORS["bg_panel"],
            fg=COLORS["text_light"],
        ).pack(side=tk.LEFT)

        self._px_frame = tk.Frame(settings_frame, bg=COLORS["bg_panel"])
        self._px_frame.pack(fill=tk.X, padx=6, pady=(0, 6))
        for label, var in (
            ("Top", self._top_var),
            ("Bottom", self._bottom_var),
            ("Left", self._left_var),
            ("Right", self._right_var),
        ):
            tk.Label(
                self._px_frame,
                text=f"{label}:",
                font=(FONT_FAMILY, 9),
                bg=COLORS["bg_panel"],
                fg=COLORS["text_light"],
            ).pack(side=tk.LEFT, padx=(0, 3))
            tk.Entry(
                self._px_frame,
                textvariable=var,
                width=5,
                bg=COLORS["bg_input"],
                fg=COLORS["text_light"],
                insertbackground=COLORS["text_light"],
                font=(FONT_FAMILY, 9),
            ).pack(side=tk.LEFT, padx=(0, 8))

        io_row = tk.Frame(settings_frame, bg=COLORS["bg_panel"])
        io_row.pack(fill=tk.X, padx=6, pady=(0, 6))
        tk.Label(
            io_row,
            text="Provider:",
            font=(FONT_FAMILY, 9),
            bg=COLORS["bg_panel"],
            fg=COLORS["text_light"],
        ).pack(side=tk.LEFT)
        provider_combo = ttk.Combobox(
            io_row,
            state="readonly",
            width=12,
            values=["bfl", "fal"],
        )
        provider_combo.set(self._provider_var.get())

        def _on_provider_change(_event=None):
            self._provider_var.set(provider_combo.get().strip())

        provider_combo.bind("<<ComboboxSelected>>", _on_provider_change)
        provider_combo.pack(side=tk.LEFT, padx=(5, 10))

        tk.Label(
            io_row,
            text="Output:",
            font=(FONT_FAMILY, 9),
            bg=COLORS["bg_panel"],
            fg=COLORS["text_light"],
        ).pack(side=tk.LEFT)
        ttk.Combobox(
            io_row,
            textvariable=self._format_var,
            values=["png", "jpg"],
            state="readonly",
            width=6,
        ).pack(side=tk.LEFT, padx=5)
        tk.Label(
            io_row,
            text="Composite:",
            font=(FONT_FAMILY, 9),
            bg=COLORS["bg_panel"],
            fg=COLORS["text_light"],
        ).pack(side=tk.LEFT, padx=(8, 0))
        self._composite_mode_labels = {
            "preserve_seamless": "Preserve Seamless",
            "feathered": "Feathered",
            "hard": "Hard",
            "black_fill": "Black Fill (no AI)",
            "none": "None",
        }
        composite_value = self._composite_mode_var.get().strip()
        if composite_value not in self._composite_mode_labels:
            composite_value = "none"
            self._composite_mode_var.set(composite_value)
        self._composite_mode_label_var = tk.StringVar(
            value=self._composite_mode_labels[composite_value]
        )

        composite_btn = tk.Menubutton(
            io_row,
            textvariable=self._composite_mode_label_var,
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

        def _set_composite_mode(mode_key: str) -> None:
            self._composite_mode_var.set(mode_key)
            self._composite_mode_label_var.set(self._composite_mode_labels[mode_key])

        for mode_key, mode_label in self._composite_mode_labels.items():
            composite_menu.add_command(
                label=mode_label,
                command=lambda m=mode_key: _set_composite_mode(m),
            )

        composite_btn.configure(menu=composite_menu)
        composite_btn.pack(side=tk.LEFT, padx=5)
        self._composite_mode_btn = composite_btn

        run_frame = tk.Frame(self, bg=COLORS["bg_panel"])
        run_frame.pack(fill=tk.X, padx=10, pady=(4, 4))
        # Workflow-primary on Step 2.5.
        self._expand_btn = ttk.Button(
            run_frame,
            text="Expand Active Image",
            style=TTK_BTN_WORKFLOW,
            command=debounce_command(self._on_expand_selected, key="expand_run"),
        )
        self._expand_btn.pack(side=tk.LEFT)
        self._status_label = tk.Label(
            run_frame,
            text="",
            font=(FONT_FAMILY, 9),
            bg=COLORS["bg_panel"],
            fg=COLORS["text_dim"],
            anchor="w",
        )
        self._status_label.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=10)

        expanded_frame = tk.LabelFrame(
            self,
            text="Expanded Outputs",
            font=(FONT_FAMILY, 9, "bold"),
            bg=COLORS["bg_panel"],
            fg=COLORS["text_light"],
        )
        expanded_frame.pack(fill=tk.BOTH, expand=True, padx=10, pady=(0, 6))

        self._expanded_list = tk.Listbox(
            expanded_frame,
            selectmode=tk.EXTENDED,
            bg=COLORS["bg_input"],
            fg=COLORS["text_light"],
            font=(FONT_FAMILY, 9),
            relief=tk.FLAT,
            height=5,
            exportselection=False,
        )
        expanded_scroll = ttk.Scrollbar(
            expanded_frame, orient=tk.VERTICAL, command=self._expanded_list.yview
        )
        self._expanded_list.configure(yscrollcommand=expanded_scroll.set)
        self._expanded_list.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, padx=(6, 0), pady=6)
        expanded_scroll.pack(side=tk.RIGHT, fill=tk.Y, padx=(0, 6), pady=6)

        send_row = tk.Frame(self, bg=COLORS["bg_panel"])
        send_row.pack(fill=tk.X, padx=10, pady=(0, 8))
        tk.Checkbutton(
            send_row,
            text="Auto-switch to Step 3 after send",
            variable=self._auto_switch_var,
            bg=COLORS["bg_panel"],
            fg=COLORS["text_light"],
            selectcolor=COLORS["bg_input"],
            activebackground=COLORS["bg_panel"],
            activeforeground=COLORS["text_light"],
            font=(FONT_FAMILY, 9),
            **macos_widget_pad(),
        ).pack(side=tk.LEFT)
        self._send_btn = ttk.Button(
            send_row,
            text="Send Selected Expanded to 3 (Video)",
            style=TTK_BTN_SUCCESS,
            command=debounce_command(self._on_send_to_video_clicked, key="expand_send_video"),
        )
        self._send_btn.pack(side=tk.RIGHT)

        self._apply_mode_ui()

    def _apply_mode_ui(self):
        # Full-res modes reuse the percentage % field as the zoom-out amount.
        if self._expand_mode_var.get() in (
            "percentage", "percentage_fullres", "three_four_fullres",
        ):
            self._px_frame.pack_forget()
            self._pct_frame.pack(fill=tk.X, padx=6, pady=(0, 4))
        else:
            self._pct_frame.pack_forget()
            self._px_frame.pack(fill=tk.X, padx=6, pady=(0, 6))

    @staticmethod
    def _format_candidate(entry) -> str:
        score = entry.similarity_score
        if score is None:
            score = parse_similarity_score(entry.similarity)

        if score is None:
            sim_text = "n/a"
            gate_text = "BLOCKED"
        else:
            sim_text = f"{score}%"
            if score >= SIMILARITY_PASS_THRESHOLD:
                gate_text = "PASS"
            elif entry.similarity_override:
                gate_text = "OVERRIDE"
            else:
                gate_text = "BLOCKED"
        return f"{entry.filename}  |  Sim {sim_text}  |  {gate_text}"

    def _get_all_selfie_entries(self):
        return [
            entry
            for entry in self.image_session.images
            if entry.source_type == "selfie" and entry.exists
        ]

    @staticmethod
    def _dedupe_entries_by_path(entries: List) -> List:
        deduped = []
        seen = set()
        for entry in entries:
            path = getattr(entry, "path", None)
            if not path:
                continue
            key = os.path.abspath(path)
            if key in seen:
                continue
            seen.add(key)
            deduped.append(entry)
        return deduped

    @classmethod
    def compose_candidate_entries(cls, selfie_entries: List, active_entry) -> List:
        """Build Step 2.5 candidates: selfies plus active non-selfie (deduped)."""
        entries = list(selfie_entries or [])
        if active_entry and getattr(active_entry, "exists", False):
            if getattr(active_entry, "source_type", "") != "selfie":
                entries.append(active_entry)
        return cls._dedupe_entries_by_path(entries)

    def _get_candidate_entries(self, active_path: Optional[str] = None) -> List:
        entries = self._get_all_selfie_entries()
        active_candidate = None
        active_abs = os.path.abspath(active_path) if active_path else None
        if active_abs:
            for entry in self.image_session.images:
                if not entry.exists:
                    continue
                if os.path.abspath(entry.path) == active_abs:
                    active_candidate = entry
                    break
        return self.compose_candidate_entries(entries, active_candidate)

    def _fallback_active_path(self, entries: List) -> Optional[str]:
        if not entries:
            return None
        active_entry = self.image_session.active_entry
        if active_entry and active_entry.exists:
            return active_entry.path
        return entries[-1].path

    def refresh_candidates(
        self,
        preselect_paths: Optional[List[str]] = None,
        active_path: Optional[str] = None,
        select_all_default: bool = False,
    ):
        entries = self._get_candidate_entries(active_path=active_path)
        self._candidate_entries = entries
        self._candidate_list.delete(0, tk.END)

        for entry in entries:
            self._candidate_list.insert(tk.END, self._format_candidate(entry))

        self._candidate_list.selection_clear(0, tk.END)
        preselect_set = {os.path.abspath(p) for p in preselect_paths or []}
        if entries:
            for idx, entry in enumerate(entries):
                if preselect_set and os.path.abspath(entry.path) in preselect_set:
                    self._candidate_list.selection_set(idx)
                elif select_all_default and not preselect_set:
                    self._candidate_list.selection_set(idx)

        if active_path:
            active_abs = os.path.abspath(active_path)
            for idx, entry in enumerate(entries):
                if os.path.abspath(entry.path) == active_abs:
                    self._candidate_list.activate(idx)
                    self._candidate_list.see(idx)
                    break

        pass_count = 0
        for entry in entries:
            score = entry.similarity_score
            if score is None:
                score = parse_similarity_score(entry.similarity)
            if score is not None and score >= SIMILARITY_PASS_THRESHOLD:
                pass_count += 1
        selfie_count = sum(1 for entry in entries if entry.source_type == "selfie")
        extra_count = len(entries) - selfie_count
        extra_text = f", +{extra_count} active non-selfie" if extra_count else ""
        self._candidate_meta.config(
            text=(
                f"{len(entries)} candidate images ({selfie_count} selfie{extra_text}) - "
                f"{pass_count} passing (>= {SIMILARITY_PASS_THRESHOLD})"
            )
        )

    def receive_from_step2(self, paths: List[str], active_path: Optional[str] = None):
        """Receive Step 2 handoff; default behavior keeps all session selfies visible."""
        selfies = [entry.path for entry in self._get_all_selfie_entries()]
        if not selfies:
            self.refresh_candidates(preselect_paths=paths, active_path=active_path)
            return
        self.refresh_candidates(
            preselect_paths=selfies,
            active_path=active_path,
            select_all_default=True,
        )
        self.log(
            f"Step 2 -> 2.5 handoff: {len(selfies)} session selfie image(s) ready",
            "info",
        )

    def _on_candidate_clicked(self, _event=None):
        """Click a row -> make that candidate the active carousel
        image. The existing _on_image_session_changed wiring then
        keeps the listbox preselection in sync (one-way flow back
        from carousel-active to listbox is the prior behavior;
        this adds the forward flow listbox-click -> carousel so
        the two views stay coupled, and the Expand button — which
        operates on the active carousel image — always targets
        what the user clicked (user request, PR #41)."""
        sel = self._candidate_list.curselection()
        if not sel:
            return
        idx = sel[0]
        if not (0 <= idx < len(self._candidate_entries)):
            return
        clicked_path = self._candidate_entries[idx].path
        try:
            clicked_abs = os.path.abspath(clicked_path)
            for i, e in enumerate(self.image_session.images):
                if os.path.abspath(e.path) == clicked_abs:
                    if self.image_session.current_index != i:
                        self.image_session.navigate_to(i)
                    break
        except Exception:
            # Lifecycle / path-resolution races are non-fatal —
            # the click just becomes a no-op rather than crashing
            # the GUI.
            pass

    def _get_selected_candidate_entries(self):
        selected = []
        for idx in self._candidate_list.curselection():
            if 0 <= idx < len(self._candidate_entries):
                selected.append(self._candidate_entries[idx])
        return selected

    def _approve_override_if_needed(self, entry) -> bool:
        score = entry.similarity_score
        if score is None:
            score = parse_similarity_score(entry.similarity)
            if score is not None:
                entry.update_similarity(score)

        if score is not None and score >= SIMILARITY_PASS_THRESHOLD:
            entry.set_similarity_override(False)
            return True

        if entry.similarity_override:
            return True

        score_text = "n/a" if score is None else f"{score}%"
        accepted = messagebox.askyesno(
            "Low Similarity Override",
            (
                f"{entry.filename}\n\n"
                f"Similarity: {score_text}\n"
                f"Required pass: >= {SIMILARITY_PASS_THRESHOLD}%\n\n"
                "This image is below threshold. Expand anyway?"
            ),
            parent=self.winfo_toplevel(),
        )
        if accepted:
            entry.set_similarity_override(True, note="manual override in Step 2.5")
            self.image_session._notify()
            return True
        return False

    @staticmethod
    def _calc_expand_pixels(image_path: str, pct: int, max_per_side: int) -> tuple:
        from PIL import Image, ImageOps

        with Image.open(image_path) as img:
            img = ImageOps.exif_transpose(img)
            width, height = img.size
        pct_frac = pct / 100.0
        left = right = min(max_per_side, int(width * pct_frac))
        top = bottom = min(max_per_side, int(height * pct_frac))
        return left, right, top, bottom

    @classmethod
    def _build_expand_margins(
        cls,
        image_path: str,
        mode: str,
        pct_value: int,
        pixel_values: tuple,
        max_per_side: int,
    ) -> Optional[tuple]:
        if mode == "percentage":
            try:
                return cls._calc_expand_pixels(image_path, pct_value, max_per_side)
            except Exception as exc:
                raise ValueError(f"Could not read image dimensions: {exc}") from exc
        left, right, top, bottom = pixel_values
        return left, right, top, bottom

    def _get_similarity_reference(self) -> Optional[str]:
        ref = self.image_session.effective_similarity_ref_entry
        if ref and ref.exists:
            return ref.path
        return None

    def refresh_from_active_carousel(self):
        """Default Step 2.5 entry behavior: include and preselect active carousel image."""
        active_path = self.image_session.active_image_path
        entries = self._get_candidate_entries(active_path=active_path)
        if active_path:
            active_abs = os.path.abspath(active_path)
            entry_paths = {os.path.abspath(entry.path) for entry in entries}
            if active_abs not in entry_paths:
                active_path = self._fallback_active_path(entries)
        else:
            active_path = self._fallback_active_path(entries)

        preselect = [active_path] if active_path else None
        self.refresh_candidates(
            preselect_paths=preselect,
            active_path=active_path,
            select_all_default=not bool(preselect),
        )

    def _set_busy(self, busy: bool):
        self._busy = busy
        self._expand_btn.config(
            state=tk.DISABLED if busy else tk.NORMAL,
            text="Expanding..." if busy else "Expand Active Image",
        )
        self._send_btn.config(state=tk.DISABLED if busy else tk.NORMAL)
        self._status_label.config(
            text="Processing..." if busy else "",
            fg=COLORS["progress"] if busy else COLORS["text_dim"],
        )

    def _on_expand_selected(self):
        if self._busy:
            return

        # Active-carousel-image-driven (user request, PR #41).
        # Clicking a candidate row already navigates the carousel
        # to that image (see _on_candidate_clicked), so the active
        # image IS the user's selection — no multi-target loop.
        active_path = self.image_session.active_image_path
        if not active_path:
            self.log("No active image in carousel to expand", "warning")
            return

        # Resolve back to a candidate entry (for threshold-override
        # approval + similarity-score logging). If the active image
        # isn't in the current candidate list (e.g. a non-selfie),
        # fabricate a minimal entry so we can still expand it.
        active_abs = os.path.abspath(active_path)
        entry = None
        for e in self._candidate_entries:
            if os.path.abspath(e.path) == active_abs:
                entry = e
                break
        if entry is None:
            # Fallback: the active image isn't in the current
            # selfie-candidate list (e.g. it's a crop / face_crop or
            # an original input the user navigated to in the carousel).
            # Use the live ImageEntry from the session directly — it
            # has the full API (.update_similarity / .set_similarity_override
            # / .similarity_override) that _approve_override_if_needed
            # requires. A fabricated namedtuple is missing those
            # methods and would raise AttributeError (Codex P1, PR #41).
            entry = self.image_session.active_entry
            if entry is None:
                self.log(
                    "Active image not resolvable to a session entry",
                    "warning",
                )
                return
        if not self._approve_override_if_needed(entry):
            self.log(
                f"Skipped (gate not overridden): {entry.filename}",
                "warning",
            )
            return
        approved = [entry]

        cfg = self.get_config()
        api_key = cfg.get("falai_api_key", "")
        if not api_key:
            self.log("fal.ai API key required", "error")
            return

        provider = self._provider_var.get().strip().lower()
        if provider not in {"fal", "bfl"}:
            provider = "fal"
        use_bfl = provider == "bfl"
        bfl_key = cfg.get("bfl_api_key", "") if use_bfl else ""
        if use_bfl and not bfl_key:
            self.log("BFL key missing - switch provider to fal or set BFL API key", "error")
            return

        max_per_side = 2048 if use_bfl else 700
        output_format = self._format_var.get()
        # Live editor value — falls back to the section-specific
        # persisted config key (Phase G of polish/v2.3), then to the
        # legacy shared ``outpaint_prompt`` key, if the widget is
        # missing (defensive, mirrors the outpaint_tab pattern).
        # Codex P2 on 6080445 (2026-05-22): route through the named
        # helper instead of inline ``or`` truthiness, otherwise an
        # explicitly-saved empty ``selfie_expand_prompt`` is silently
        # replaced by ``outpaint_prompt`` in this defensive path —
        # the same regression R4 fixed on the happy path.
        try:
            prompt = self._prompt_text.get("1.0", "end-1c")
        except Exception:
            prompt = self._fallback_selfie_expand_prompt()
        composite_mode = self._composite_mode_var.get().strip() or "preserve_seamless"
        freeimage_key = cfg.get("freeimage_api_key")
        ref_path = self._get_similarity_reference()
        if ref_path:
            self.log(
                f"Step 2.5 similarity reference: {ref_path}",
                "info",
            )
        else:
            self.log("Step 2.5 similarity reference missing: no extracted/front/input image", "error")
            return
        mode = self._expand_mode_var.get()
        fullres_mode = mode in ("percentage_fullres", "three_four_fullres")
        fullres_aspect = (3, 4) if mode == "three_four_fullres" else None
        try:
            if mode in ("percentage", "percentage_fullres", "three_four_fullres"):
                pct_value = int(self._pct_var.get())
                pixel_values = (0, 0, 0, 0)
            else:
                pct_value = 0
                pixel_values = (
                    int(self._left_var.get()),
                    int(self._right_var.get()),
                    int(self._top_var.get()),
                    int(self._bottom_var.get()),
                )
        except (tk.TclError, ValueError):
            self.log("Invalid expand settings", "error")
            return

        self._set_busy(True)
        self.log(f"Expanding {len(approved)} image(s) in Step 2.5...", "task")

        def _worker():
            from outpaint_generator import OutpaintGenerator
            from ..tag_utils import increment_ops
            from path_utils import build_expand_filenames
            from face_similarity import compute_face_similarity_details

            gen = OutpaintGenerator(
                api_key,
                freeimage_key=freeimage_key,
                bfl_api_key=bfl_key if use_bfl else None,
            )
            gen.set_progress_callback(
                lambda msg, lvl: self.winfo_toplevel().after(
                    0, lambda m=msg, l=lvl: self.log(m, l)
                )
            )

            completed = 0
            for entry in approved:
                _fr_kwargs = {}
                left = right = top = bottom = 0
                try:
                    if fullres_mode:
                        from outpaint_geometry import (
                            compute_full_res_expand_plan,
                            compute_provider_caps,
                            resolve_border_strategy,
                        )
                        from PIL import Image as _PILImg, ImageOps as _PILOps
                        with _PILImg.open(entry.path) as _im:
                            _iw, _ih = _PILOps.exif_transpose(_im).size
                        _fr_kwargs["full_res_plan"] = compute_full_res_expand_plan(
                            _iw, _ih, pct_value,
                            compute_provider_caps("bfl" if use_bfl else "fal"),
                            fullres_aspect,
                        )
                        _fr_kwargs["border_strategy"] = resolve_border_strategy(
                            cfg, bool(api_key)
                        )
                    else:
                        left, right, top, bottom = self._build_expand_margins(
                            entry.path, mode, pct_value, pixel_values, max_per_side
                        )
                except Exception as exc:
                    self.winfo_toplevel().after(
                        0,
                        lambda e=entry, err=exc: self.log(
                            f"Skipped [{e.filename}]: {err}", "error"
                        ),
                    )
                    continue

                output_dir = get_gen_images_folder(entry.path)
                os.makedirs(output_dir, exist_ok=True)

                # Step 2.5 expand-output naming unified with Step 0 per
                # PR #48 round 4 user feedback. The previous ops-tag
                # scheme produced names like `<stem>_exp.png` / `_2-exp`
                # which conflicted with Step 0's `-expanded` /
                # `-expanded-2x` form; the user saw two conventions
                # side-by-side and rightly called it inconsistent.
                # Ops accounting still happens in ``new_ops`` for the
                # carousel display tag; only the filename changed.
                # Collision suffix logic via ``_vN`` (no more
                # hacky ``_v{counter}{ext}`` injection into
                # ``build_ops_filename``).
                new_ops = increment_ops(entry.ops if entry.ops else {}, "exp")
                stem = Path(entry.path).stem
                pass1_path, _ = build_expand_filenames(
                    base_stem=stem,
                    ext=output_format,
                    gen_dir=output_dir,
                    do_2x=False,
                )
                output_path = str(pass1_path)

                result = None
                try:
                    result = gen.outpaint(
                        image_path=entry.path,
                        output_folder=output_dir,
                        expand_left=left,
                        expand_right=right,
                        expand_top=top,
                        expand_bottom=bottom,
                        prompt=prompt,
                        output_format=output_format,
                        composite_mode=composite_mode,
                        output_path=output_path,
                        poll_timeout_seconds=get_outpaint_fal_timeout_seconds(cfg),
                        **_fr_kwargs,
                    )
                except Exception as exc:
                    self.winfo_toplevel().after(
                        0,
                        lambda e=entry, err=exc: self.log(
                            f"Expand failed [{e.filename}]: {err}", "error"
                        ),
                    )
                    continue

                score = None
                passed = None
                if result and ref_path:
                    self.log(
                        f"Step 2.5 compare pair: ref={ref_path} target={result}",
                        "debug",
                    )
                    details = compute_face_similarity_details(
                        ref_path, result, report_cb=self.log
                    )
                    if not details.get("error"):
                        score = details.get("score")
                        passed = details.get("pass")
                    else:
                        self.log(
                            f"Step 2.5 similarity unavailable for {os.path.basename(result)}: {details.get('error')}",
                            "warning",
                        )

                completed += 1
                self.winfo_toplevel().after(
                    0,
                    lambda src=entry, path=result, scr=score, ok=passed, ops=new_ops: self._on_single_expand_complete(
                        src, path, scr, ok, ops
                    ),
                )

            self.winfo_toplevel().after(0, lambda: self._on_expand_batch_complete(completed))

        threading.Thread(target=_worker, daemon=True).start()

    def _on_single_expand_complete(self, source_entry, result_path, score, passed, ops):
        if not result_path:
            return
        similarity = f"{score}%" if score is not None else "n/a"
        basename = os.path.basename(result_path)
        self.image_session.add_image(
            result_path,
            "outpaint",
            label=basename,
            similarity=similarity,
            similarity_score=score,
            similarity_pass=passed,
            similarity_override=False,
            ops=ops,
        )
        if result_path not in self._expanded_paths:
            self._expanded_paths.append(result_path)
        self._refresh_expanded_list()
        self.log(f"Expanded: {basename}", "success")

    def _on_expand_batch_complete(self, completed: int):
        self._set_busy(False)
        self.refresh_candidates(select_all_default=False)
        if completed > 0:
            self.log(f"Step 2.5 expand complete: {completed} image(s)", "success")
        else:
            self.log("Step 2.5 expand completed with no outputs", "warning")

    def _refresh_expanded_list(self):
        self._expanded_list.delete(0, tk.END)
        valid_paths = []
        for path in self._expanded_paths:
            if os.path.isfile(path):
                valid_paths.append(path)
                self._expanded_list.insert(tk.END, os.path.basename(path))
        self._expanded_paths = valid_paths

    def _on_send_to_video_clicked(self):
        if self._busy:
            return
        if not self._on_send_to_video:
            self.log("No Step 3 queue handler available", "warning")
            return
        indexes = list(self._expanded_list.curselection())
        if not indexes:
            self.log("Select expanded output(s) to send to Step 3", "warning")
            return
        paths = []
        for idx in indexes:
            if 0 <= idx < len(self._expanded_paths):
                p = self._expanded_paths[idx]
                if os.path.isfile(p):
                    paths.append(p)
        if not paths:
            self.log("Selected expanded output(s) are missing on disk", "warning")
            return

        self._on_send_to_video(paths)
        self.log(f"Sent {len(paths)} expanded image(s) to Step 3 queue", "info")
        if self._auto_switch_var.get() and self._notebook_switcher_video:
            self._notebook_switcher_video()

    def _fallback_selfie_expand_prompt(self) -> str:
        """Defensive fallback when ``_prompt_text`` widget is missing.

        Codex P1 on 0967564 (2026-05-22): use key-presence semantics
        so an explicitly-saved empty ``selfie_expand_prompt`` is
        preserved (an empty string is a valid intentional value).
        Only fall through to legacy ``outpaint_prompt`` when the
        section-specific key is absent or non-str.
        """
        cfg = getattr(self, "config", None)
        if not isinstance(cfg, dict):
            return ""
        section = cfg.get("selfie_expand_prompt")
        if isinstance(section, str):
            return section
        legacy = cfg.get("outpaint_prompt")
        return legacy if isinstance(legacy, str) else ""

    def get_config_updates(self) -> dict:
        return {
            "expand25_auto_switch": self._auto_switch_var.get(),
            "outpaint_expand_mode": self._expand_mode_var.get(),
            "outpaint_expand_percentage": self._pct_var.get(),
            "outpaint_expand_top": self._top_var.get(),
            "outpaint_expand_bottom": self._bottom_var.get(),
            "outpaint_expand_left": self._left_var.get(),
            "outpaint_expand_right": self._right_var.get(),
            "outpaint_format": self._format_var.get(),
            # NEVER write to the SHARED key here. Step 2.5's factory default
            # is "none" (raw AI output) while Step 0's is "preserve_seamless".
            # Previously Step 2.5 wrote both keys at session save, which meant
            # Step 0's user-chosen "preserve_seamless" got silently overwritten
            # to "none" every time the user opened Step 2.5. Section-specific
            # key only; Step 0 manages the shared key on its own.
            "automation_selfie_expand_composite_mode": self._composite_mode_var.get(),
            "outpaint_provider": self._provider_var.get(),
            # Phase G of polish/v2.3 (2026-05-22): writes go to the
            # section-specific key. Reads still fall back to the legacy
            # shared key for back-compat (see _build_ui), but writes
            # never touch the shared key so a Step 2.5 prompt edit can
            # NOT silently override Step 0 or Outpaint-tab prompts.
            # Codex P1 on 0967564: defensive fallback also uses
            # key-presence semantics (not truthiness) so an
            # explicit empty ``selfie_expand_prompt`` survives.
            "selfie_expand_prompt": (
                self._prompt_text.get("1.0", "end-1c")
                if hasattr(self, "_prompt_text")
                else self._fallback_selfie_expand_prompt()
            ),
        }
