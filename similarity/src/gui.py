import os
import threading
import json
import logging
import tkinter as tk
from tkinter import filedialog
from typing import List, Optional, Tuple

import customtkinter as ctk
from PIL import Image
from tkinterdnd2 import DND_FILES, TkinterDnD

from src.engine import FaceEngine
from src import theme

IMAGE_FILETYPES = [("Image Files", "*.png *.jpg *.jpeg *.bmp *.webp")]
SUPPORTED_IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".bmp", ".webp"}
# v4: image previews shrunk from 250x250 → 220x220 to free width for the
# new center hero verdict column. Ratio-preserved fit logic is unchanged.
SIMILARITY_PREVIEW_MAX_SIZE = (220, 220)
EXTRACTION_PREVIEW_MAX_SIZE = (300, 300)


class DnDCTk(TkinterDnD.DnDWrapper, ctk.CTk):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.TkdndVersion = TkinterDnD._require(self)


class ModernGUI(DnDCTk):
    """
    Modern Graphical User Interface using CustomTkinter.
    Provides separate Similarity and Extraction workflows while running
    heavy ML operations in background daemon threads.
    """

    def __init__(self):
        super().__init__()

        self.engine = FaceEngine()
        self.config_path = os.path.join(os.path.dirname(os.path.dirname(__file__)), "config.json")
        self.config = {
            "padding_ratio": 0.175,
            "existing_file_mode": "index",
        }
        self._load_config()
        self.img1_path: Optional[str] = None
        self.img2_path: Optional[str] = None
        self.extraction_src_path: Optional[str] = None
        self.extraction_out_path: Optional[str] = None
        # Cached face bboxes from the most recent comparison — drives the
        # "Show face detection boxes" overlay toggle without re-running compare.
        self._last_ref_bbox: Optional[dict] = None
        self._last_target_bbox: Optional[dict] = None

        self.title("Face Similarity Pro")
        # v4: 920x880 → 880x720 (-4% W, -18% H). Tighter 3-col layout uses
        # vertical real estate better; window now fits 1080p screens with
        # room for the taskbar + window chrome.
        self.geometry("880x720")
        self.minsize(800, 680)
        ctk.set_appearance_mode("dark")
        ctk.set_default_color_theme("blue")
        # Apply brutalist palette to the window itself; per-widget overrides
        # in _build_* methods extend it to all children.
        self.configure(fg_color=theme.BG_DEEP)

        self.grid_rowconfigure(1, weight=1)
        self.grid_columnconfigure(0, weight=1)

        self.header_label = ctk.CTkLabel(
            self,
            text="FACE SIMILARITY",
            font=theme.sans_font(size=18, weight="bold"),
            text_color=theme.TEXT_DIM,
        )
        self.header_label.grid(row=0, column=0, pady=(18, 8))

        self.tabview = ctk.CTkTabview(
            self,
            fg_color=theme.BG_DEEP,
            segmented_button_fg_color=theme.BG_PANEL,
            segmented_button_selected_color=theme.BG_PANEL_HI,
            segmented_button_selected_hover_color=theme.BG_PANEL_HI,
            segmented_button_unselected_color=theme.BG_PANEL,
            segmented_button_unselected_hover_color=theme.BG_PANEL_HI,
            text_color=theme.TEXT,
            border_color=theme.BORDER,
            border_width=1,
        )
        self.tabview.grid(row=1, column=0, sticky="nsew", padx=18, pady=8)
        self.similarity_tab = self.tabview.add("Similarity")
        self.extraction_tab = self.tabview.add("Extraction")

        self._build_similarity_tab()
        self._build_extraction_tab()

        self.set_ui_state("disabled")
        # Idle hero card while models initialize. Sub-status line carries the
        # "loading models" copy in muted form so the hero can stay neutral.
        self._update_hero_verdict(state="idle")
        self.sim_result_label.configure(text="initializing ML models…", text_color=theme.INFO)
        self.ext_result_label.configure(text="Initializing ML Models... Please wait.", text_color="yellow")
        # Switch sim_progressbar to indeterminate mode for the startup pulse
        # (it goes back to determinate when a comparison starts).
        self.sim_progressbar.configure(mode="indeterminate")
        self.sim_progressbar.grid()
        self.ext_progressbar.grid()
        self.sim_progressbar.start()
        self.ext_progressbar.start()

        threading.Thread(target=self._init_models_thread, daemon=True).start()

    def _load_config(self):
        if not os.path.exists(self.config_path):
            return
        try:
            with open(self.config_path, "r", encoding="utf-8") as f:
                loaded = json.load(f)
            if not isinstance(loaded, dict):
                return

            if "padding_ratio" in loaded:
                value = float(loaded["padding_ratio"])
                if 0.0 <= value <= 1.0:
                    self.config["padding_ratio"] = value

            mode = loaded.get("existing_file_mode")
            if mode in {"index", "skip", "overwrite"}:
                self.config["existing_file_mode"] = mode
        except Exception:
            # GUI should still launch even with invalid config.json.
            return

    def _build_similarity_tab(self):
        # 3-column layout: (zone1)(verdict)(zone2). Image zones flank the
        # central HERO VERDICT card — the hallmark feature gets center stage.
        # Column weights: zones get 3 each, hero gets 2 — total 8 units.
        self.similarity_tab.configure(fg_color=theme.BG_DEEP)
        self.similarity_tab.grid_columnconfigure(0, weight=3)
        self.similarity_tab.grid_columnconfigure(1, weight=2, minsize=240)
        self.similarity_tab.grid_columnconfigure(2, weight=3)
        self.similarity_tab.grid_rowconfigure(1, weight=1)

        self._build_zone(side=1, column=0, label="REFERENCE", pad=(0, 6))
        self._build_hero_verdict()  # column=1
        self._build_zone(side=2, column=2, label="TARGET", pad=(6, 0))

        self._bind_drop_target(self.zone1_dropzone, self._on_drop_similarity_image1)
        self._bind_drop_target(self.zone1_drop_hint, self._on_drop_similarity_image1)
        self._bind_drop_target(self.img1_display, self._on_drop_similarity_image1)
        self._bind_drop_target(self.zone2_dropzone, self._on_drop_similarity_image2)
        self._bind_drop_target(self.zone2_drop_hint, self._on_drop_similarity_image2)
        self._bind_drop_target(self.img2_display, self._on_drop_similarity_image2)

        self.sim_controls = ctk.CTkFrame(self.similarity_tab, fg_color="transparent")
        self.sim_controls.grid(row=2, column=0, columnspan=3, sticky="ew", pady=(8, 14))
        self.sim_controls.grid_columnconfigure(0, weight=1)

        # Anti-spoof toggle. Default ON to match KYC-grade defaults.
        # Toggling re-runs the comparison if both images are loaded.
        self.anti_spoof_var = tk.BooleanVar(value=getattr(self.engine, "anti_spoofing", True))
        self.anti_spoof_checkbox = ctk.CTkCheckBox(
            self.sim_controls,
            text="Anti-spoof (liveness)",
            variable=self.anti_spoof_var,
            onvalue=True,
            offvalue=False,
            command=self._on_anti_spoof_toggle,
            font=theme.sans_font(size=11),
            text_color=theme.TEXT_DIM,
            fg_color=theme.ACCENT,
            hover_color=theme.BG_PANEL_HI,
            border_color=theme.BORDER_HI,
            border_width=1,
            checkmark_color=theme.BG_DEEP,
        )
        self.anti_spoof_checkbox.grid(row=0, column=0, pady=(4, 6))

        # Show face boxes overlay toggle. OFF by default — opt-in diagnostic.
        # Pure UI redraw on toggle, no recompute. Reads cached bboxes from the
        # last comparison's diagnostics (saved on _on_comparison_complete).
        self.show_face_box_var = tk.BooleanVar(value=False)
        self.show_face_box_checkbox = ctk.CTkCheckBox(
            self.sim_controls,
            text="Detection boxes",
            variable=self.show_face_box_var,
            onvalue=True,
            offvalue=False,
            command=self._on_show_face_box_toggle,
            font=theme.sans_font(size=11),
            text_color=theme.TEXT_DIM,
            fg_color=theme.ACCENT,
            hover_color=theme.BG_PANEL_HI,
            border_color=theme.BORDER_HI,
            border_width=1,
            checkmark_color=theme.BG_DEEP,
        )
        self.show_face_box_checkbox.grid(row=0, column=1, padx=(20, 0), pady=(4, 6))
        self.sim_controls.grid_columnconfigure(1, weight=1)

        # Industrial control-panel button — flat accent fill, no chunky radius.
        self.btn_run = ctk.CTkButton(
            self.sim_controls,
            text="RUN COMPARISON",
            font=theme.sans_font(size=13, weight="bold"),
            command=self.start_comparison,
            height=38,
            corner_radius=4,
            fg_color=theme.ACCENT,
            hover_color=theme.ACCENT_HI,
            text_color=theme.BG_DEEP,
        )
        self.btn_run.grid(row=1, column=0, columnspan=2, pady=(8, 4))

        # Determinate progress bar — drives 0-100% from a phase-milestone timer
        # while the comparison thread runs. Snaps to 100% when the thread returns.
        # ALWAYS gridded so the layout doesn't shift when a comparison starts;
        # we just toggle the fill (set 0 → set value) and the status text below.
        self.sim_progressbar = ctk.CTkProgressBar(
            self.sim_controls, mode="determinate", height=6, corner_radius=3,
            fg_color=theme.BG_PANEL, progress_color=theme.ACCENT, border_color=theme.BORDER, border_width=0,
        )
        self.sim_progressbar.grid(row=2, column=0, columnspan=2, pady=(10, 2), sticky="ew", padx=24)
        self.sim_progressbar.set(0)

        # Live transient status text. Pre-reserved fixed-height widget with a
        # placeholder space so the GUI doesn't shift when text appears mid-run.
        # When idle, the placeholder is invisible; phase milestones overwrite it.
        self.sim_status_label = ctk.CTkLabel(
            self.sim_controls,
            text=" ",  # non-empty placeholder so layout reserves the line
            font=theme.mono_font(size=10),
            text_color=theme.TEXT_MUTE,
            height=18,
        )
        self.sim_status_label.grid(row=3, column=0, columnspan=2, pady=(0, 4), sticky="ew")

        # v4: the prior wide sim_result_label / fas_result_label that lived
        # at the bottom of the window are GONE — the hero verdict card now
        # carries the similarity verdict and per-image FAS badges carry
        # liveness state. sim_result_label is repurposed here as a small
        # transient STATUS line (for loading errors, init errors, etc.) —
        # it never carries verdict copy.
        self.sim_result_label = ctk.CTkLabel(
            self.sim_controls,
            text=" ",
            font=theme.mono_font(size=10),
            text_color=theme.TEXT_MUTE,
            wraplength=720,
            height=18,
        )
        self.sim_result_label.grid(row=4, column=0, columnspan=2, pady=(2, 0), sticky="ew")
        # fas_result_label is no longer created — all references to it were
        # removed in the same commit. Per-image badges are the source of truth.

    def _build_zone(self, *, side: int, column: int, label: str, pad):
        """Build one of the two image zones (reference or target).

        Each zone is a vertical stack: header label, dropzone (with hint +
        image preview), per-image FAS badge, select-file button. The badge
        sits IMMEDIATELY under the image so the user can read each side's
        liveness verdict at a glance — no scanning required.
        """
        frame = ctk.CTkFrame(
            self.similarity_tab,
            fg_color=theme.BG_PANEL,
            border_color=theme.BORDER,
            border_width=1,
            corner_radius=0,  # brutalist hairline edge
        )
        frame.grid(row=0, column=column, rowspan=2, sticky="nsew", padx=pad, pady=4)
        frame.grid_rowconfigure(1, weight=1)
        frame.grid_columnconfigure(0, weight=1)

        header = ctk.CTkLabel(
            frame,
            text=label,
            font=theme.sans_font(size=11, weight="bold"),
            text_color=theme.TEXT_DIM,
        )
        header.grid(row=0, column=0, pady=(8, 4))

        # Dropzone — flat panel with hairline border. Brutalist: no rounded
        # corners on the panel itself, only on the inner button.
        dropzone = ctk.CTkFrame(
            frame,
            fg_color=theme.BG_DEEP,
            border_width=1,
            border_color=theme.BORDER_HI,
            corner_radius=0,
        )
        dropzone.grid(row=1, column=0, pady=2, padx=6, sticky="nsew")
        dropzone.grid_rowconfigure(1, weight=1)
        dropzone.grid_columnconfigure(0, weight=1)
        drop_hint = ctk.CTkLabel(
            dropzone,
            text="drop image here",
            text_color=theme.TEXT_MUTE,
            font=theme.mono_font(size=9),
        )
        drop_hint.grid(row=0, column=0, pady=(4, 2), padx=8)
        img_display = ctk.CTkLabel(
            dropzone,
            text="(no image)",
            text_color=theme.TEXT_MUTE,
            font=theme.mono_font(size=9),
        )
        img_display.grid(row=1, column=0, pady=(2, 4), padx=8)

        # Per-image FAS badge — pre-allocated so it doesn't shift the layout
        # when populated post-comparison. Mono font for tech-spec readout feel.
        fas_label = ctk.CTkLabel(
            frame,
            text=" ",
            font=theme.mono_font(size=11, weight="bold"),
            text_color=theme.TEXT_MUTE,
            height=20,
        )
        fas_label.grid(row=2, column=0, pady=(4, 2), sticky="ew")

        btn = ctk.CTkButton(
            frame,
            text="select file",
            command=lambda s=side: self.upload_image(s),
            height=28,
            corner_radius=4,
            font=theme.sans_font(size=11),
            fg_color=theme.BG_PANEL_HI,
            hover_color=theme.BORDER_HI,
            text_color=theme.TEXT,
            border_color=theme.BORDER_HI,
            border_width=1,
        )
        btn.grid(row=3, column=0, pady=(4, 8))

        # Bind to instance attrs so existing code paths keep working.
        if side == 1:
            self.zone1_frame = frame
            self.zone1_label = header
            self.zone1_dropzone = dropzone
            self.zone1_drop_hint = drop_hint
            self.img1_display = img_display
            self.zone1_fas_label = fas_label
            self.btn_upload1 = btn
        else:
            self.zone2_frame = frame
            self.zone2_label = header
            self.zone2_dropzone = dropzone
            self.zone2_drop_hint = drop_hint
            self.img2_display = img_display
            self.zone2_fas_label = fas_label
            self.btn_upload2 = btn

    def _build_hero_verdict(self):
        """Build the central HERO VERDICT card — the hallmark feature.

        Three vertical stripes:
          1. status icon strip (top): single large glyph (✓ ✖ —)
          2. headline (middle): MATCH / NO MATCH / READY (28pt, bold, tracked)
          3. score block (bottom): big mono score % + caption + threshold bar

        Idle state shows "—" + READY in TEXT_DIM. Match shows "✓" + MATCH in
        OK green. No-match shows "✖" + NO MATCH in FAIL red.
        """
        card = ctk.CTkFrame(
            self.similarity_tab,
            fg_color=theme.BG_PANEL,
            border_color=theme.BORDER,
            border_width=1,
            corner_radius=6,  # the only rounded panel — earns the eye
        )
        card.grid(row=0, column=1, rowspan=2, sticky="nsew", padx=4, pady=4)
        card.grid_columnconfigure(0, weight=1)
        # 5 rows: top spacer, icon, headline, score, bottom spacer
        card.grid_rowconfigure(0, weight=1)
        card.grid_rowconfigure(4, weight=1)

        self.hero_card = card

        self.hero_icon = ctk.CTkLabel(
            card,
            text="—",
            font=theme.sans_font(size=42, weight="bold"),
            text_color=theme.TEXT_MUTE,
            height=56,
        )
        self.hero_icon.grid(row=1, column=0, pady=(0, 2))

        # Tracked-out via spaces — CTk has no letter-spacing API, so
        # widening the headline manually gives it the brutalist tech feel.
        self.hero_headline = ctk.CTkLabel(
            card,
            text="R E A D Y",
            font=theme.sans_font(size=18, weight="bold"),
            text_color=theme.TEXT_MUTE,
            height=24,
        )
        self.hero_headline.grid(row=2, column=0, pady=(2, 8))

        # Score subframe so the score number + caption + threshold bar all
        # share a tight vertical block separate from the headline.
        score_frame = ctk.CTkFrame(card, fg_color="transparent")
        score_frame.grid(row=3, column=0, sticky="ew", padx=18, pady=(0, 12))
        score_frame.grid_columnconfigure(0, weight=1)

        self.hero_score = ctk.CTkLabel(
            score_frame,
            text="—",  # idle placeholder
            font=theme.mono_font(size=30, weight="bold"),
            text_color=theme.TEXT_MUTE,
            height=36,
        )
        self.hero_score.grid(row=0, column=0)

        self.hero_score_caption = ctk.CTkLabel(
            score_frame,
            text="similarity score",
            font=theme.sans_font(size=9),
            text_color=theme.TEXT_MUTE,
        )
        self.hero_score_caption.grid(row=1, column=0, pady=(0, 6))

        # Threshold-marked progress bar: visualizes the score on the 0-100
        # axis with a tick mark at the 80% threshold so the user can see HOW
        # close to the cutoff a match landed. Always present; just zeroed
        # when idle.
        self.hero_threshold_bar = ctk.CTkProgressBar(
            score_frame, mode="determinate", height=4, corner_radius=2,
            fg_color=theme.BG_DEEP, progress_color=theme.TEXT_MUTE,
            border_color=theme.BORDER, border_width=0,
        )
        self.hero_threshold_bar.grid(row=2, column=0, sticky="ew", padx=4)
        self.hero_threshold_bar.set(0)

        # Tick mark at 80% threshold — a tiny vertical hairline below the bar.
        self.hero_threshold_tick = ctk.CTkLabel(
            score_frame,
            text="          ▲ 80%",  # 80% point text marker. Spaces approximate position.
            font=theme.mono_font(size=8),
            text_color=theme.TEXT_MUTE,
        )
        self.hero_threshold_tick.grid(row=3, column=0, pady=(2, 0))

    def _update_hero_verdict(self, *, state: str, score: Optional[float] = None,
                             error_msg: Optional[str] = None):
        """Drive the hero card's visual state.

        states:
          - "idle":     "—" + READY (muted)
          - "running":  "…" + COMPUTING (info)
          - "match":    "✓" + MATCH (OK green) + score
          - "no_match": "✖" + NO MATCH (FAIL red) + score
          - "error":    "!" + ERROR (WARN amber) + tooltip with msg
        """
        if state == "idle":
            self.hero_icon.configure(text="—", text_color=theme.TEXT_MUTE)
            self.hero_headline.configure(text="R E A D Y", text_color=theme.TEXT_MUTE)
            self.hero_score.configure(text="—", text_color=theme.TEXT_MUTE)
            self.hero_threshold_bar.set(0)
        elif state == "running":
            self.hero_icon.configure(text="…", text_color=theme.INFO)
            self.hero_headline.configure(text="C O M P U T I N G", text_color=theme.INFO)
            self.hero_score.configure(text="—", text_color=theme.TEXT_MUTE)
            self.hero_threshold_bar.set(0)
        elif state == "match":
            self.hero_icon.configure(text="✓", text_color=theme.OK)
            self.hero_headline.configure(text="M A T C H", text_color=theme.OK)
            if score is not None:
                self.hero_score.configure(text=f"{score:.1f}%", text_color=theme.OK)
                self.hero_threshold_bar.configure(progress_color=theme.OK)
                self.hero_threshold_bar.set(max(0.0, min(1.0, score / 100.0)))
        elif state == "no_match":
            self.hero_icon.configure(text="✖", text_color=theme.FAIL)
            self.hero_headline.configure(text="N O   M A T C H", text_color=theme.FAIL)
            if score is not None:
                self.hero_score.configure(text=f"{score:.1f}%", text_color=theme.FAIL)
                self.hero_threshold_bar.configure(progress_color=theme.FAIL)
                self.hero_threshold_bar.set(max(0.0, min(1.0, score / 100.0)))
        elif state == "error":
            self.hero_icon.configure(text="!", text_color=theme.WARN)
            self.hero_headline.configure(text="E R R O R", text_color=theme.WARN)
            short = (error_msg or "")[:48]
            self.hero_score.configure(text=short, text_color=theme.WARN, font=theme.mono_font(size=10))
            self.hero_threshold_bar.set(0)

    def _build_extraction_tab(self):
        self.extraction_tab.grid_columnconfigure(0, weight=1)

        self.ext_frame = ctk.CTkFrame(self.extraction_tab)
        self.ext_frame.grid(row=0, column=0, sticky="nsew", padx=10, pady=12)
        self.ext_frame.grid_columnconfigure(0, weight=1)
        self.ext_frame.grid_rowconfigure(1, weight=1)

        self.ext_label = ctk.CTkLabel(self.ext_frame, text="Select Source Image", font=ctk.CTkFont(size=16))
        self.ext_label.grid(row=0, column=0, pady=(12, 8))

        self.ext_dropzone = ctk.CTkFrame(self.ext_frame, fg_color="transparent", border_width=2, border_color="#1f6aa5")
        self.ext_dropzone.grid(row=1, column=0, pady=(2, 10), padx=8, sticky="nsew")
        self.ext_dropzone.grid_rowconfigure(1, weight=1)
        self.ext_dropzone.grid_columnconfigure(0, weight=1)
        self.ext_drop_hint = ctk.CTkLabel(self.ext_dropzone, text="Drag and drop source image here")
        self.ext_drop_hint.grid(row=0, column=0, pady=(10, 6), padx=12)
        self.ext_display = ctk.CTkLabel(self.ext_dropzone, text="No Source Image Selected")
        self.ext_display.grid(row=1, column=0, pady=(4, 10), padx=12)

        self.btn_upload_extract = ctk.CTkButton(
            self.ext_frame,
            text="Select Source File...",
            command=self.upload_extraction_image,
        )
        self.btn_upload_extract.grid(row=2, column=0, pady=8)

        self._bind_drop_target(self.ext_dropzone, self._on_drop_extraction_source)
        self._bind_drop_target(self.ext_drop_hint, self._on_drop_extraction_source)
        self._bind_drop_target(self.ext_display, self._on_drop_extraction_source)

        self.ext_output_label = ctk.CTkLabel(self.ext_frame, text="Output: (not selected yet)", wraplength=760)
        self.ext_output_label.grid(row=3, column=0, pady=(4, 10))

        self.btn_run_extract = ctk.CTkButton(
            self.ext_frame,
            text="Run Face Extraction",
            font=ctk.CTkFont(size=16, weight="bold"),
            command=self.start_extraction,
            height=40,
        )
        self.btn_run_extract.grid(row=4, column=0, pady=8)

        self.ext_progressbar = ctk.CTkProgressBar(self.ext_frame, mode="indeterminate")
        self.ext_progressbar.grid(row=5, column=0, pady=(0, 10), sticky="ew")
        self.ext_progressbar.set(0)
        self.ext_progressbar.grid_remove()

        self.ext_result_label = ctk.CTkLabel(
            self.ext_frame,
            text="",
            font=ctk.CTkFont(size=18),
            wraplength=760,
        )
        self.ext_result_label.grid(row=6, column=0, pady=(0, 12))

    def _init_models_thread(self):
        try:
            self.engine.initialize_models()
            self.after(0, self._on_models_ready)
        except Exception as e:
            self.after(0, self._on_init_error, str(e))

    def _on_models_ready(self):
        # Stop the indeterminate startup pulse and switch the bar back to
        # determinate mode so the comparison driver can use it. Bar stays
        # gridded — layout-stability is more important than hiding pixels.
        self.sim_progressbar.stop()
        self.ext_progressbar.stop()
        self.sim_progressbar.configure(mode="determinate")
        self.sim_progressbar.set(0)
        self.ext_progressbar.grid_remove()  # extraction bar stays hidden until used
        self.sim_status_label.configure(text=" ", text_color=theme.TEXT_MUTE)
        self.sim_result_label.configure(text=" ", text_color=theme.TEXT_MUTE)
        self.ext_result_label.configure(text="", text_color="white")
        self._update_hero_verdict(state="idle")
        self.set_ui_state("normal")

    def _on_init_error(self, error_msg: str):
        self.sim_progressbar.stop()
        self.ext_progressbar.stop()
        self.sim_progressbar.configure(mode="determinate")
        self.sim_progressbar.set(0)
        self.ext_progressbar.grid_remove()
        self.sim_status_label.configure(text=" ", text_color=theme.TEXT_MUTE)
        # Hero card carries the init-error story; status line stays clean.
        self._update_hero_verdict(state="error", error_msg=error_msg)
        self.sim_result_label.configure(text=" ", text_color=theme.TEXT_MUTE)
        self.ext_result_label.configure(text=f"Initialization Error: {error_msg}", text_color="red")

    def set_ui_state(self, state: str):
        # v4 bot fix (coderabbit, similarity/src/gui.py:210): include the
        # anti_spoof_checkbox + show_face_box_checkbox so users can't toggle
        # them mid-run and end up with checkbox state that doesn't match the
        # result.
        self.btn_upload1.configure(state=state)
        self.btn_upload2.configure(state=state)
        self.anti_spoof_checkbox.configure(state=state)
        self.show_face_box_checkbox.configure(state=state)
        self.btn_run.configure(state=state)
        self.btn_upload_extract.configure(state=state)
        self.btn_run_extract.configure(state=state)

    def _bind_drop_target(self, widget, handler):
        widget.drop_target_register(DND_FILES)
        widget.dnd_bind("<<Drop>>", handler)

    def _extract_drop_paths(self, data: str) -> List[str]:
        if not data:
            return []

        try:
            raw_paths = self.tk.splitlist(data)
        except Exception:
            raw_paths = (data,)

        paths: List[str] = []
        for raw_path in raw_paths:
            path = str(raw_path).strip()
            if path.startswith("{") and path.endswith("}"):
                path = path[1:-1]
            path = path.strip().strip('"')
            if path:
                paths.append(os.path.normpath(path))
        return paths

    def _is_ui_enabled(self) -> bool:
        try:
            return self.btn_upload1.cget("state") == "normal"
        except Exception:
            return False

    def _is_supported_image_file(self, file_path: str) -> bool:
        return os.path.splitext(file_path)[1].lower() in SUPPORTED_IMAGE_EXTENSIONS

    def _clear_similarity_image_zone(self, zone: int):
        if zone == 1:
            self.img1_path = None
            self._last_ref_bbox = None
            self.img1_display.configure(image=None, text="No Image Selected")
            self.img1_display.image = None
            if hasattr(self, "zone1_fas_label"):
                self.zone1_fas_label.configure(text=" ", text_color="#6B7280")
            return
        self.img2_path = None
        self._last_target_bbox = None
        self.img2_display.configure(image=None, text="No Image Selected")
        self.img2_display.image = None
        if hasattr(self, "zone2_fas_label"):
            self.zone2_fas_label.configure(text=" ", text_color="#6B7280")

    def _clear_extraction_source(self):
        self.extraction_src_path = None
        self.extraction_out_path = None
        self.ext_display.configure(image=None, text="No Source Image Selected")
        self.ext_display.image = None

    def _fit_preview_size(self, width: int, height: int, max_width: int, max_height: int) -> Tuple[int, int]:
        if width <= 0 or height <= 0:
            return max_width, max_height
        scale = min(max_width / width, max_height / height, 1.0)
        fitted_width = max(1, min(max_width, int(round(width * scale))))
        fitted_height = max(1, min(max_height, int(round(height * scale))))
        return fitted_width, fitted_height

    def _build_preview_image(self, img: Image.Image, max_width: int, max_height: int) -> ctk.CTkImage:
        fitted_size = self._fit_preview_size(img.size[0], img.size[1], max_width, max_height)
        return ctk.CTkImage(light_image=img, dark_image=img, size=fitted_size)

    def _load_similarity_image(self, file_path: str, zone: int):
        if not os.path.isfile(file_path):
            self._clear_similarity_image_zone(zone)
            self.sim_result_label.configure(text=f"file not found: {file_path}", text_color=theme.FAIL)
            return
        if not self._is_supported_image_file(file_path):
            self._clear_similarity_image_zone(zone)
            self.sim_result_label.configure(
                text="unsupported file type — use PNG, JPG, JPEG, BMP, or WEBP",
                text_color=theme.FAIL,
            )
            return

        try:
            with Image.open(file_path) as opened:
                img = opened.copy()
            ctk_image = self._build_preview_image(
                img, max_width=SIMILARITY_PREVIEW_MAX_SIZE[0], max_height=SIMILARITY_PREVIEW_MAX_SIZE[1]
            )

            if zone == 1:
                self.img1_path = file_path
                self.img1_display.configure(image=ctk_image, text="")
                self.img1_display.image = ctk_image
            else:
                self.img2_path = file_path
                self.img2_display.configure(image=ctk_image, text="")
                self.img2_display.image = ctk_image
            self.sim_result_label.configure(text=" ", text_color=theme.TEXT_MUTE)
            # Reset hero card to idle so the user knows results are stale until
            # they re-run the comparison with the new image.
            self._update_hero_verdict(state="idle")
            self._reset_per_image_fas_labels()
        except Exception as e:
            self._clear_similarity_image_zone(zone)
            self.sim_result_label.configure(text=f"error loading image: {e}", text_color=theme.FAIL)

    def _load_extraction_source_image(self, file_path: str):
        if not os.path.isfile(file_path):
            self._clear_extraction_source()
            self.ext_result_label.configure(text=f"Error loading image: file not found ({file_path})", text_color="red")
            return
        if not self._is_supported_image_file(file_path):
            self._clear_extraction_source()
            self.ext_result_label.configure(
                text="Error loading image: unsupported file type. Use PNG, JPG, JPEG, BMP, or WEBP.",
                text_color="red",
            )
            return

        try:
            with Image.open(file_path) as opened:
                img = opened.copy()
            ctk_image = self._build_preview_image(
                img, max_width=EXTRACTION_PREVIEW_MAX_SIZE[0], max_height=EXTRACTION_PREVIEW_MAX_SIZE[1]
            )
            self.extraction_src_path = file_path
            self.extraction_out_path = self._resolve_extracted_output_path(file_path)
            self.ext_display.configure(image=ctk_image, text="")
            self.ext_display.image = ctk_image
            if self.extraction_out_path:
                self.ext_output_label.configure(text=f"Output: {os.path.basename(self.extraction_out_path)}")
            else:
                self.ext_output_label.configure(
                    text="Output: skipped by existing_file_mode='skip' (existing extracted file found)"
                )
            self.ext_result_label.configure(text="", text_color="white")
        except Exception as e:
            self._clear_extraction_source()
            self.ext_result_label.configure(text=f"Error loading image: {e}", text_color="red")

    def _handle_similarity_drop(self, data: str, zone: int):
        if not self._is_ui_enabled():
            self.sim_result_label.configure(text="wait for the current task to finish", text_color=theme.WARN)
            return
        for file_path in self._extract_drop_paths(data):
            self._load_similarity_image(file_path, zone)
            return
        self.sim_result_label.configure(text="error loading image: no files were dropped", text_color=theme.FAIL)

    def _handle_extraction_drop(self, data: str):
        if not self._is_ui_enabled():
            self.ext_result_label.configure(text="Please wait for the current task to finish.", text_color="yellow")
            return
        for file_path in self._extract_drop_paths(data):
            self._load_extraction_source_image(file_path)
            return
        self.ext_result_label.configure(text="Error loading image: no files were dropped.", text_color="red")

    def _on_drop_similarity_image1(self, event):
        self._handle_similarity_drop(event.data, 1)
        return "break"

    def _on_drop_similarity_image2(self, event):
        self._handle_similarity_drop(event.data, 2)
        return "break"

    def _on_drop_extraction_source(self, event):
        self._handle_extraction_drop(event.data)
        return "break"

    def upload_image(self, zone: int):
        file_path = filedialog.askopenfilename(
            title=f"Select Image {zone}",
            filetypes=IMAGE_FILETYPES,
        )
        if not file_path:
            return

        self._load_similarity_image(file_path, zone)

    def _next_extracted_path(self, source_path: str) -> str:
        directory = os.path.dirname(source_path)
        ext = os.path.splitext(source_path)[1] or ".png"
        source_norm = os.path.normcase(os.path.normpath(source_path))
        first = os.path.join(directory, f"extracted{ext}")
        first_norm = os.path.normcase(os.path.normpath(first))
        if first_norm != source_norm and not os.path.exists(first):
            return first

        idx = 2
        while True:
            candidate = os.path.join(directory, f"extracted{idx}{ext}")
            candidate_norm = os.path.normcase(os.path.normpath(candidate))
            if candidate_norm != source_norm and not os.path.exists(candidate):
                return candidate
            idx += 1

    def _resolve_extracted_output_path(self, source_path: str) -> Optional[str]:
        directory = os.path.dirname(source_path)
        ext = os.path.splitext(source_path)[1] or ".png"
        target = os.path.join(directory, f"extracted{ext}")

        mode = self.config.get("existing_file_mode", "index")
        if mode not in {"index", "skip", "overwrite"}:
            mode = "index"

        source_norm = os.path.normcase(os.path.normpath(source_path))
        target_norm = os.path.normcase(os.path.normpath(target))
        force_index = source_norm == target_norm

        if not force_index and not os.path.exists(target):
            return target
        if mode == "skip" and not force_index:
            return None
        if mode == "overwrite" and not force_index:
            return target
        return self._next_extracted_path(source_path)

    def upload_extraction_image(self):
        file_path = filedialog.askopenfilename(
            title="Select Image for Extraction",
            filetypes=IMAGE_FILETYPES,
        )
        if not file_path:
            return

        self._load_extraction_source_image(file_path)

    # Phase milestones for the determinate progress bar — chosen so the bar moves
    # at roughly the rate the underlying DeepFace pipeline finishes each step.
    # (Pure UX — the engine doesn't emit progress events; this is a believable
    # cadence that matches typical 600ms-1.5s comparison durations.)
    _PROGRESS_PHASES = [
        (0.05, "Loading models…"),
        (0.15, "Detecting faces (RetinaFace)…"),
        (0.30, "Cropping & aligning…"),
        (0.50, "Computing ArcFace embedding…"),
        (0.70, "Computing Facenet512 embedding (ensemble)…"),
        (0.85, "Anti-spoof liveness check…"),
        (0.95, "Computing similarity score…"),
    ]

    def start_comparison(self):
        if not self.img1_path or not self.img2_path:
            self.sim_result_label.configure(
                text="upload both images before running comparison",
                text_color=theme.WARN,
            )
            return

        self.set_ui_state("disabled")
        # Clear status line + per-image FAS labels + hero card so the user
        # sees only fresh in-progress UI.
        self.sim_result_label.configure(text=" ", text_color=theme.TEXT_MUTE)
        self._reset_per_image_fas_labels()
        self._update_hero_verdict(state="running")
        # Stop any leftover indeterminate animation (from startup) and switch
        # to determinate mode for the phase-milestone driver. The widgets are
        # ALWAYS gridded — we just reset state, not visibility, so the GUI
        # layout never shifts when a comparison starts.
        try:
            self.sim_progressbar.stop()
        except Exception:
            pass
        self.sim_progressbar.configure(mode="determinate")
        self.sim_progressbar.set(0)
        self.sim_status_label.configure(text="starting…  0%", text_color=theme.TEXT_MUTE)
        # Phase-milestone driver state.
        self._progress_done = False
        self._progress_phase_idx = 0
        # Kick off the milestone timer; advances the bar smoothly through phases
        # while the comparison thread runs. Cancelled when the result lands.
        self.after(60, self._tick_progress)

        threading.Thread(
            target=self._compare_thread,
            args=(self.img1_path, self.img2_path),
            daemon=True,
        ).start()

    def _tick_progress(self):
        """Advance the determinate progress bar through phase milestones.

        Pure UX — runs entirely on the Tk thread. Phase pacing is calibrated
        for typical 600ms–1.5s comparison runs; if the real comparison
        finishes faster (cached models) or slower (cold start), the bar
        either jumps to 100% (fast path) or holds at 95% (slow path) until
        _on_comparison_complete snaps it home.
        """
        if self._progress_done:
            return
        target_pct, label = self._PROGRESS_PHASES[
            min(self._progress_phase_idx, len(self._PROGRESS_PHASES) - 1)
        ]
        # Smoothly walk the bar toward the next phase target so the motion is
        # visually pleasant rather than steppy.
        try:
            current = float(self.sim_progressbar.get())
        except Exception:
            current = 0.0
        if current < target_pct:
            current = min(target_pct, current + 0.015)
            self.sim_progressbar.set(current)
            self.sim_status_label.configure(text=f"{label}  {int(current * 100)}%")
        else:
            # Reached this phase's milestone — advance to next on the next tick.
            if self._progress_phase_idx < len(self._PROGRESS_PHASES) - 1:
                self._progress_phase_idx += 1
        self.after(60, self._tick_progress)

    def _compare_thread(self, path1: str, path2: str):
        try:
            result = self.engine.compare_images(path1, path2)
        except Exception as e:
            logging.exception("Comparison thread failed")
            result = {"match": False, "score": 0.0, "error": str(e)}
        self.after(0, self._on_comparison_complete, result)

    def _on_comparison_complete(self, result: dict):
        # Halt the phase-milestone timer and snap the bar to 100% so the user
        # gets a definitive "done" beat before the bar fades.
        self._progress_done = True
        self.sim_progressbar.set(1.0)
        if result.get("error"):
            self.sim_status_label.configure(text="failed", text_color=theme.FAIL)
        else:
            self.sim_status_label.configure(text="done  100%", text_color=theme.OK)
        # Remove the bar + status after a short beat so the result reads cleanly.
        self.after(450, self._hide_progress_ui)
        self.set_ui_state("normal")

        # Cache per-side bboxes from this comparison so the "Show face boxes"
        # toggle can re-render the displayed images without re-running compare.
        diag = result.get("diagnostics") if isinstance(result.get("diagnostics"), dict) else {}
        boxes = (diag or {}).get("selected_face_boxes") or {}
        self._last_ref_bbox = boxes.get("ref") if isinstance(boxes, dict) else None
        self._last_target_bbox = boxes.get("target") if isinstance(boxes, dict) else None
        # If the toggle is currently on, redraw both images with the new bboxes.
        if self.show_face_box_var.get():
            self._redraw_images_with_bbox()

        if result.get("error"):
            # Hero card carries the error visually; status line stays clean.
            self._update_hero_verdict(state="error", error_msg=str(result["error"]))
            self.sim_result_label.configure(text=" ", text_color=theme.TEXT_MUTE)
            self._reset_per_image_fas_labels()
            return

        # Drive the hero card with the verdict + score. The wide bottom-of-window
        # text label is gone — hero is the single source of truth for the
        # similarity story.
        try:
            score_value = float(result["score"])
        except (TypeError, ValueError):
            score_value = 0.0
        is_match = bool(result.get("match"))
        self._update_hero_verdict(
            state="match" if is_match else "no_match",
            score=score_value,
        )
        # Status line stays empty — hero already tells the story.
        self.sim_result_label.configure(text=" ", text_color=theme.TEXT_MUTE)
        self._render_fas(result)

    def _hide_progress_ui(self):
        """Reset progress UI to idle after a comparison completes.

        Widgets stay gridded (so the layout doesn't shift); we just zero the
        bar and clear the status text. The placeholder space keeps the row
        height stable.
        """
        try:
            self.sim_progressbar.set(0)
            self.sim_status_label.configure(text=" ", text_color="#6B7280")
        except Exception:
            pass

    def _on_show_face_box_toggle(self):
        """Toggle the face-bbox overlay on both image displays. Pure UI redraw."""
        if self.show_face_box_var.get():
            self._redraw_images_with_bbox()
        else:
            # Toggle OFF — reload the original images without overlay.
            if getattr(self, "img1_path", None):
                self._refresh_similarity_image(1, draw_bbox=False)
            if getattr(self, "img2_path", None):
                self._refresh_similarity_image(2, draw_bbox=False)

    def _redraw_images_with_bbox(self):
        """Re-render both image zones with their cached face bboxes overlaid."""
        if getattr(self, "img1_path", None):
            self._refresh_similarity_image(1, draw_bbox=True)
        if getattr(self, "img2_path", None):
            self._refresh_similarity_image(2, draw_bbox=True)

    def _refresh_similarity_image(self, zone: int, *, draw_bbox: bool):
        """Reload a zone's image, optionally overlaying its cached face bbox."""
        path = self.img1_path if zone == 1 else self.img2_path
        bbox = self._last_ref_bbox if zone == 1 else self._last_target_bbox
        if not path or not os.path.isfile(path):
            return
        try:
            with Image.open(path) as opened:
                img = opened.copy()
            if draw_bbox and isinstance(bbox, dict):
                img = self._overlay_face_bbox(img, bbox)
            ctk_image = self._build_preview_image(
                img, max_width=SIMILARITY_PREVIEW_MAX_SIZE[0], max_height=SIMILARITY_PREVIEW_MAX_SIZE[1]
            )
            display = self.img1_display if zone == 1 else self.img2_display
            display.configure(image=ctk_image, text="")
            display.image = ctk_image
        except Exception as exc:
            logging.exception("Failed to refresh image with bbox overlay: %s", exc)

    @staticmethod
    def _overlay_face_bbox(img: Image.Image, bbox: dict) -> Image.Image:
        """Return a copy of `img` with a green rectangle drawn at `bbox`.

        bbox = {"x", "y", "w", "h"} in original-image coordinates (the same
        coordinate space DeepFace returned). Draws a 3px green outline that's
        proportionate to image size so it stays visible at any preview scale.
        """
        from PIL import ImageDraw
        try:
            x = int(bbox.get("x", 0))
            y = int(bbox.get("y", 0))
            w = int(bbox.get("w", 0))
            h = int(bbox.get("h", 0))
        except (TypeError, ValueError):
            return img
        if w <= 0 or h <= 0:
            return img
        out = img.copy().convert("RGB")
        draw = ImageDraw.Draw(out)
        # Stroke width scales with image size so the box is visible at preview scale.
        # Bumped from 0.005 -> 0.008 for better visibility per user feedback.
        stroke = max(3, int(min(out.width, out.height) * 0.008))
        # Brighter green for better contrast against dark backgrounds + portrait skin.
        draw.rectangle((x, y, x + w, y + h), outline=(72, 219, 122), width=stroke)
        return out

    def _render_fas(self, result: dict):
        """Render the LIVENESS check from a comparison result diagnostics block.

        v4: per-image badges under each image are the SOLE liveness surface
        in the standalone GUI. The wide center FAS line is gone — the hero
        verdict card now owns the center. Each badge switches on the engine's
        is_real boolean (not score magnitude) so a Driver's License flagged
        is_real=False with antispoof_score=0.9999 displays as "✖ SPOOF · 99.99%"
        in red, not "✓ Liveness: 99.9% real" in green (the prior bug).
        """
        try:
            from src.engine import FaceEngine
        except ImportError:
            from similarity_engine import FaceEngine  # fallback for direct invocation
        diag = result.get("diagnostics") if isinstance(result.get("diagnostics"), dict) else {}
        summary = FaceEngine.summarize_fas_pair(diag)
        # Each side renders independently — no shared verdict needed at this
        # layer. The badge format encodes both the verdict (REAL / SPOOF) and
        # the engine's confidence in a single line.
        self._set_per_image_fas_badge(
            self.zone1_fas_label,
            is_real=summary.get("ref_is_real"),
            real_conf=summary.get("ref_real_conf"),
            status=summary.get("ref_status", "missing"),
        )
        self._set_per_image_fas_badge(
            self.zone2_fas_label,
            is_real=summary.get("target_is_real"),
            real_conf=summary.get("target_real_conf"),
            status=summary.get("target_status", "missing"),
        )

    @staticmethod
    def _set_per_image_fas_badge(label, *, is_real, real_conf, status: str):
        """Populate one per-image liveness badge.

        Switches on the engine's is_real boolean — NOT score magnitude — so
        the spoof verdict is presented correctly regardless of how confident
        the model was. The displayed % is the engine's confidence in its
        verdict (always the higher of real_conf vs 1-real_conf, since the
        engine is always confident IN ONE DIRECTION).

        Badge formats:
          is_real=True,  status=ok  →  "✓ REAL · 99.7%"   (green)
          is_real=False, status=ok  →  "✖ SPOOF · 99.99%" (red)
          status=no_face            →  "· no face"        (muted)
          status=not_active         →  "· liveness off"   (muted)
          missing / other           →  empty placeholder  (muted)
        """
        if status == "ok" and isinstance(is_real, bool) and isinstance(real_conf, (int, float)):
            real_conf = max(0.0, min(1.0, float(real_conf)))
            if is_real:
                # Engine says real — show real_conf as the % real.
                pct = real_conf * 100
                text = f"✓ REAL · {ModernGUI._format_conf_pct(pct)}"
                color = theme.OK
            else:
                # Engine says spoof — show (1 - real_conf) as the % spoof
                # confidence (which is what the antispoof_score is internally
                # when is_real=False).
                spoof_conf = (1.0 - real_conf) * 100
                text = f"✖ SPOOF · {ModernGUI._format_conf_pct(spoof_conf)}"
                color = theme.FAIL
        elif status == "no_face":
            text = "· no face"
            color = theme.TEXT_MUTE
        elif status == "not_active":
            text = "· liveness off"
            color = theme.TEXT_MUTE
        else:
            text = " "  # placeholder — keeps row height stable
            color = theme.TEXT_MUTE
        label.configure(text=text, text_color=color)

    def _reset_per_image_fas_labels(self):
        """Clear the per-image liveness badges so a new comparison starts clean."""
        for lbl in (getattr(self, "zone1_fas_label", None), getattr(self, "zone2_fas_label", None)):
            if lbl is not None:
                lbl.configure(text=" ", text_color=theme.TEXT_MUTE)

    @staticmethod
    def _format_conf_pct(pct: float) -> str:
        """Format a confidence percentage with adaptive precision.

        At very high confidence (≥99.9%) the model is essentially certain, so
        use 2 decimals to preserve the meaningful "99.99% confident SPOOF"
        signal — formatting as "100.0%" loses information and undersells how
        decisive the model was. Below 99.9% one decimal is plenty.
        """
        if pct >= 99.9:
            return f"{pct:.2f}%"
        return f"{pct:.1f}%"

    def _on_anti_spoof_toggle(self):
        """Apply checkbox state to the engine; re-run comparison if both images are loaded."""
        new_value = bool(self.anti_spoof_var.get())
        self.engine.anti_spoofing = new_value
        # Re-run comparison if we have a previous result on screen and both images are still loaded.
        if (
            getattr(self, "img1_path", None)
            and getattr(self, "img2_path", None)
            and self.btn_run.cget("state") != "disabled"
        ):
            self.start_comparison()

    def start_extraction(self):
        if not self.extraction_src_path:
            self.ext_result_label.configure(
                text="Please select a source image before running extraction.",
                text_color="yellow",
            )
            return
        self.extraction_out_path = self._resolve_extracted_output_path(self.extraction_src_path)
        if not self.extraction_out_path:
            self.ext_result_label.configure(
                text="Extraction skipped because existing_file_mode is 'skip' and an extracted file already exists.",
                text_color="yellow",
            )
            return
        self.ext_output_label.configure(text=f"Output: {os.path.basename(self.extraction_out_path)}")

        self.set_ui_state("disabled")
        self.ext_result_label.configure(text="Processing... Detecting face and extracting crop...", text_color="cyan")
        self.ext_progressbar.grid()
        self.ext_progressbar.start()

        threading.Thread(
            target=self._extract_thread,
            args=(self.extraction_src_path, self.extraction_out_path),
            daemon=True,
        ).start()

    def _extract_thread(self, src_path: str, out_path: str):
        try:
            confidence = self.engine.extract_face(src_path, out_path, padding=self.config["padding_ratio"])
            result = {"ok": True, "confidence": confidence, "output": out_path}
        except Exception as e:
            result = {"ok": False, "error": str(e)}
        self.after(0, self._on_extraction_complete, result)

    def _on_extraction_complete(self, result: dict):
        self.ext_progressbar.stop()
        self.ext_progressbar.grid_remove()
        self.set_ui_state("normal")

        if not result.get("ok"):
            self.ext_result_label.configure(text=f"Error: {result['error']}", text_color="red")
            return

        self.ext_result_label.configure(
            text=(
                f"Extraction complete: {os.path.basename(result['output'])} "
                f"(Confidence: {result['confidence']:.1%})"
            ),
            text_color="#00FF00",
        )


def run_gui():
    app = ModernGUI()
    app.mainloop()


if __name__ == "__main__":
    run_gui()
