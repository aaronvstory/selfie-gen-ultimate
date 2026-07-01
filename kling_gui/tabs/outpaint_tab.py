"""Outpaint Tab — Expand images using fal.ai outpaint."""

import tkinter as tk
from tkinter import ttk
import threading
import os
from typing import Callable

from ..theme import (
    COLORS,
    FONT_FAMILY,
    FONT_MONO,
    TTK_BTN_COMPACT,
    TTK_BTN_WORKFLOW,
)
from ..image_state import ImageSession
from path_utils import get_gen_images_folder
from log_utils import format_exception_detail
from automation.config import get_outpaint_fal_timeout_seconds


class OutpaintTab(tk.Frame):
    """Tab 3: Expand (outpaint) images."""

    def __init__(
        self,
        parent,
        image_session: ImageSession,
        config: dict,
        config_getter: Callable[[], dict],
        log_callback: Callable[[str, str], None],
        **kwargs,
    ):
        super().__init__(parent, bg=COLORS["bg_panel"], **kwargs)
        self.image_session = image_session
        self.config = config
        self.get_config = config_getter
        self.log = log_callback
        self._busy = False
        self._composite_mode_var = tk.StringVar(
            value=self.config.get("outpaint_composite_mode", "preserve_seamless")
        )

        self._build_ui()

    def _build_ui(self):
        # ── Expand Mode Toggle ──────────────────────────────────────────
        mode_frame = tk.LabelFrame(
            self,
            text="Expand Mode",
            font=(FONT_FAMILY, 9, "bold"),
            bg=COLORS["bg_panel"],
            fg=COLORS["text_light"],
        )
        mode_frame.pack(fill=tk.X, padx=10, pady=(10, 5))

        mode_row = tk.Frame(mode_frame, bg=COLORS["bg_panel"])
        mode_row.pack(fill=tk.X, padx=5, pady=5)

        self._expand_mode_var = tk.StringVar(
            value=self.config.get("outpaint_expand_mode", "percentage")
        )
        tk.Radiobutton(
            mode_row,
            text="Percentage (simple)",
            variable=self._expand_mode_var,
            value="percentage",
            command=self._on_mode_changed,
            bg=COLORS["bg_panel"],
            fg=COLORS["text_light"],
            selectcolor=COLORS["bg_input"],
            activebackground=COLORS["bg_panel"],
            font=(FONT_FAMILY, 9),
        ).pack(side=tk.LEFT)
        tk.Radiobutton(
            mode_row,
            text="Pixels (manual L/R/T/B)",
            variable=self._expand_mode_var,
            value="pixels",
            command=self._on_mode_changed,
            bg=COLORS["bg_panel"],
            fg=COLORS["text_light"],
            selectcolor=COLORS["bg_input"],
            activebackground=COLORS["bg_panel"],
            font=(FONT_FAMILY, 9),
        ).pack(side=tk.LEFT, padx=(15, 0))
        # Full-res modes keep the original at native resolution (only the
        # generated borders are upscaled). They reuse the % field as the
        # zoom-out amount; "3:4 Full-res" lands on an exact 3:4 canvas.
        tk.Radiobutton(
            mode_row,
            text="% Full-res",
            variable=self._expand_mode_var,
            value="percentage_fullres",
            command=self._on_mode_changed,
            bg=COLORS["bg_panel"],
            fg=COLORS["text_light"],
            selectcolor=COLORS["bg_input"],
            activebackground=COLORS["bg_panel"],
            font=(FONT_FAMILY, 9),
        ).pack(side=tk.LEFT, padx=(15, 0))
        tk.Radiobutton(
            mode_row,
            text="3:4 Full-res",
            variable=self._expand_mode_var,
            value="three_four_fullres",
            command=self._on_mode_changed,
            bg=COLORS["bg_panel"],
            fg=COLORS["text_light"],
            selectcolor=COLORS["bg_input"],
            activebackground=COLORS["bg_panel"],
            font=(FONT_FAMILY, 9),
        ).pack(side=tk.LEFT, padx=(15, 0))

        # Expand cost — shown clearly so the user knows the per-image charge
        # before running (2026-06-18). The generator uses BFL Flux Expand when
        # a BFL key is set (allows larger expands) and falls back to the fal
        # outpaint endpoint otherwise; surface both rates.
        tk.Label(
            mode_frame,
            text="\U0001F4B2  Cost: ~$0.035 / megapixel (fal outpaint)  ·  "
                 "$0.05 / image (BFL Flux Expand, used when a BFL key is set)",
            font=(FONT_FAMILY, 8),
            bg=COLORS["bg_panel"],
            fg=COLORS["text_dim"],
            anchor="w",
            justify=tk.LEFT,
        ).pack(fill=tk.X, padx=5, pady=(0, 4))

        # ── Percentage Controls ─────────────────────────────────────────
        self._pct_frame = tk.Frame(self, bg=COLORS["bg_panel"])

        pct_row = tk.Frame(self._pct_frame, bg=COLORS["bg_panel"])
        pct_row.pack(fill=tk.X, padx=10, pady=(2, 0))

        tk.Label(
            pct_row,
            text="Expand all sides by:",
            font=(FONT_FAMILY, 9),
            bg=COLORS["bg_panel"],
            fg=COLORS["text_light"],
        ).pack(side=tk.LEFT)

        self._pct_var = tk.IntVar(
            value=self.config.get("outpaint_expand_percentage", 30)
        )
        self._pct_scale = tk.Scale(
            pct_row,
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
        )
        self._pct_scale.pack(side=tk.LEFT, padx=(5, 0))

        tk.Label(
            pct_row,
            text="%",
            font=(FONT_FAMILY, 9),
            bg=COLORS["bg_panel"],
            fg=COLORS["text_light"],
        ).pack(side=tk.LEFT)

        # Quick preset buttons for percentage
        pct_presets = tk.Frame(self._pct_frame, bg=COLORS["bg_panel"])
        pct_presets.pack(fill=tk.X, padx=10, pady=(2, 5))

        tk.Label(
            pct_presets,
            text="Presets:",
            font=(FONT_FAMILY, 9),
            bg=COLORS["bg_panel"],
            fg=COLORS["text_dim"],
        ).pack(side=tk.LEFT, padx=(0, 4))

        for pct in [10, 20, 30, 50, 75]:
            label = f"{pct}%"
            if pct == 30:
                label = "30% (default)"
            ttk.Button(
                pct_presets,
                text=label,
                style=TTK_BTN_COMPACT,
                command=lambda p=pct: self._pct_var.set(p),
            ).pack(side=tk.LEFT, padx=2)

        # ── Pixels Controls ─────────────────────────────────────────────
        self._px_frame = tk.LabelFrame(
            self,
            text="Expansion (pixels per side)",
            font=(FONT_FAMILY, 9, "bold"),
            bg=COLORS["bg_panel"],
            fg=COLORS["text_light"],
        )

        grid = tk.Frame(self._px_frame, bg=COLORS["bg_panel"])
        grid.pack(fill=tk.X, padx=5, pady=5)

        # Top
        tk.Label(
            grid, text="Top:", font=(FONT_FAMILY, 9),
            bg=COLORS["bg_panel"], fg=COLORS["text_light"],
        ).grid(row=0, column=0, sticky="w")
        self.top_var = tk.IntVar(
            value=self.config.get("outpaint_expand_top", 140)
        )
        tk.Entry(
            grid, textvariable=self.top_var, width=6,
            bg=COLORS["bg_input"], fg=COLORS["text_light"],
            insertbackground=COLORS["text_light"], font=(FONT_FAMILY, 9),
        ).grid(row=0, column=1, padx=5, pady=2)

        # Bottom
        tk.Label(
            grid, text="Bottom:", font=(FONT_FAMILY, 9),
            bg=COLORS["bg_panel"], fg=COLORS["text_light"],
        ).grid(row=0, column=2, sticky="w")
        self.bottom_var = tk.IntVar(
            value=self.config.get("outpaint_expand_bottom", 140)
        )
        tk.Entry(
            grid, textvariable=self.bottom_var, width=6,
            bg=COLORS["bg_input"], fg=COLORS["text_light"],
            insertbackground=COLORS["text_light"], font=(FONT_FAMILY, 9),
        ).grid(row=0, column=3, padx=5, pady=2)

        # Left
        tk.Label(
            grid, text="Left:", font=(FONT_FAMILY, 9),
            bg=COLORS["bg_panel"], fg=COLORS["text_light"],
        ).grid(row=1, column=0, sticky="w")
        self.left_var = tk.IntVar(
            value=self.config.get("outpaint_expand_left", 140)
        )
        tk.Entry(
            grid, textvariable=self.left_var, width=6,
            bg=COLORS["bg_input"], fg=COLORS["text_light"],
            insertbackground=COLORS["text_light"], font=(FONT_FAMILY, 9),
        ).grid(row=1, column=1, padx=5, pady=2)

        # Right
        tk.Label(
            grid, text="Right:", font=(FONT_FAMILY, 9),
            bg=COLORS["bg_panel"], fg=COLORS["text_light"],
        ).grid(row=1, column=2, sticky="w")
        self.right_var = tk.IntVar(
            value=self.config.get("outpaint_expand_right", 140)
        )
        tk.Entry(
            grid, textvariable=self.right_var, width=6,
            bg=COLORS["bg_input"], fg=COLORS["text_light"],
            insertbackground=COLORS["text_light"], font=(FONT_FAMILY, 9),
        ).grid(row=1, column=3, padx=5, pady=2)

        # Pixel preset buttons
        uniform_frame = tk.Frame(self._px_frame, bg=COLORS["bg_panel"])
        uniform_frame.pack(fill=tk.X, padx=5, pady=(0, 5))

        for px, text in [(70, "70px"), (140, "140px"), (280, "280px"), (500, "500px")]:
            ttk.Button(
                uniform_frame, text=text, style=TTK_BTN_COMPACT,
                command=lambda p=px: self._set_uniform(p),
            ).pack(side=tk.LEFT, padx=2)

        # ── Prompt (optional) ───────────────────────────────────────────
        prompt_frame = tk.LabelFrame(
            self,
            text="Guidance Prompt (optional)",
            font=(FONT_FAMILY, 9, "bold"),
            bg=COLORS["bg_panel"],
            fg=COLORS["text_light"],
        )
        prompt_frame.pack(fill=tk.X, padx=10, pady=5)

        self.prompt_text = tk.Text(
            prompt_frame,
            height=3,
            wrap=tk.WORD,
            bg=COLORS["bg_input"],
            fg=COLORS["text_light"],
            # Unified prompt font (user request 2026-05-21): every
            # prompt-text Text widget in the app uses (FONT_FAMILY, 10)
            # to match the video-tab positive + negative prompt
            # editors. Was (FONT_MONO, 9) — visibly smaller + mono.
            font=(FONT_FAMILY, 10),
            insertbackground=COLORS["text_light"],
            padx=5,
            pady=5,
        )
        self.prompt_text.pack(fill=tk.X, padx=5, pady=5)
        # Phase G of polish/v2.3 (2026-05-22): section-specific
        # key ``outpaint_tab_prompt``, with the legacy shared
        # ``outpaint_prompt`` as a back-compat fallback so users
        # with old configs see their saved prompt on first launch.
        # Codex P1 on 0967564 (2026-05-22): key-presence
        # semantics, NOT truthiness. An explicitly-saved empty
        # ``outpaint_tab_prompt`` is a valid intentional value
        # (user cleared the prompt) and must NOT be silently
        # replaced by the legacy shared prompt.
        _section_prompt = self.config.get("outpaint_tab_prompt")
        if isinstance(_section_prompt, str):
            _initial_prompt = _section_prompt
        else:
            _legacy = self.config.get("outpaint_prompt")
            _initial_prompt = _legacy if isinstance(_legacy, str) else ""
        self.prompt_text.insert("1.0", _initial_prompt)

        # ── Output format ───────────────────────────────────────────────
        fmt_frame = tk.Frame(self, bg=COLORS["bg_panel"])
        fmt_frame.pack(fill=tk.X, padx=10, pady=5)

        tk.Label(
            fmt_frame, text="Output Format:",
            font=(FONT_FAMILY, 9),
            bg=COLORS["bg_panel"], fg=COLORS["text_light"],
        ).pack(side=tk.LEFT)
        self.format_var = tk.StringVar(
            value=self.config.get("outpaint_format", "png")
        )
        ttk.Combobox(
            fmt_frame, textvariable=self.format_var,
            values=["png", "jpg"], state="readonly", width=6,
        ).pack(side=tk.LEFT, padx=5)
        tk.Label(
            fmt_frame, text="Composite:",
            font=(FONT_FAMILY, 9),
            bg=COLORS["bg_panel"], fg=COLORS["text_light"],
        ).pack(side=tk.LEFT, padx=(8, 0))
        ttk.Combobox(
            fmt_frame, textvariable=self._composite_mode_var,
            values=["preserve_seamless", "feathered", "hard", "black_fill", "none"], state="readonly", width=18,
        ).pack(side=tk.LEFT, padx=5)

        # ── Expand button ───────────────────────────────────────────────
        btn_frame = tk.Frame(self, bg=COLORS["bg_panel"])
        btn_frame.pack(fill=tk.X, padx=10, pady=10)

        # Workflow-primary on the standalone Outpaint tab.
        self.expand_btn = ttk.Button(
            btn_frame,
            text="Expand Image",
            style=TTK_BTN_WORKFLOW,
            command=self._on_expand,
        )
        self.expand_btn.pack(side=tk.LEFT)

        self.status_label = tk.Label(
            btn_frame, text="",
            font=(FONT_FAMILY, 9),
            bg=COLORS["bg_panel"], fg=COLORS["text_dim"],
        )
        self.status_label.pack(side=tk.LEFT, padx=10)

        # Apply initial mode visibility
        self._apply_mode_ui()

    # ── Mode switching ──────────────────────────────────────────────────

    def _on_mode_changed(self):
        self._apply_mode_ui()
        mode = self._expand_mode_var.get()
        label = "Percentage" if mode == "percentage" else "Pixels"
        self.log(f"Expand mode: {label}", "info")

    def _apply_mode_ui(self):
        # Full-res modes reuse the percentage % field as the zoom-out amount.
        if self._expand_mode_var.get() in (
            "percentage", "percentage_fullres", "three_four_fullres",
        ):
            self._px_frame.pack_forget()
            self._pct_frame.pack(fill=tk.X, padx=0, pady=0)
        else:
            self._pct_frame.pack_forget()
            self._px_frame.pack(fill=tk.X, padx=10, pady=(0, 5))

    def _set_uniform(self, px: int):
        """Set all expansion values uniformly."""
        for var in [self.top_var, self.bottom_var, self.left_var, self.right_var]:
            var.set(px)

    # ── Percentage → pixel calculation ──────────────────────────────────

    @staticmethod
    def _calculate_expand_pixels(
        image_path: str, percentage: int, max_per_side: int = 700,
    ) -> tuple:
        """Calculate L/R/T/B pixel expansion from image dimensions and %.

        Args:
            max_per_side: Cap per-side expansion (700 for fal.ai, 2048 for BFL).
        """
        from PIL import Image, ImageOps

        with Image.open(image_path) as img:
            img = ImageOps.exif_transpose(img)
            width, height = img.size

        pct = percentage / 100.0
        lr = min(max_per_side, int(width * pct))
        tb = min(max_per_side, int(height * pct))
        return lr, lr, tb, tb  # left, right, top, bottom

    # ── Generation ──────────────────────────────────────────────────────

    def _on_expand(self):
        if self._busy:
            return

        config = self.get_config()
        api_key = config.get("falai_api_key", "")
        if not api_key:
            self.log("fal.ai API key required (set in Video tab)", "error")
            return

        image_path = self.image_session.active_image_path
        if not image_path:
            self.log("No image selected in carousel", "warning")
            return

        output_folder = config.get("output_folder", "")
        if not output_folder or not os.path.isdir(output_folder):
            ref = self.image_session.reference_entry
            ref_path = ref.path if ref else image_path
            output_folder = get_gen_images_folder(ref_path)
        os.makedirs(output_folder, exist_ok=True)

        prompt = self.prompt_text.get("1.0", tk.END).strip()
        output_format = self.format_var.get()
        composite_mode = self._composite_mode_var.get()
        mode = self._expand_mode_var.get()
        has_bfl = bool(self.get_config().get("bfl_api_key"))
        expand_left = expand_right = expand_top = expand_bottom = 0
        full_res_plan = None

        if mode in ("percentage_fullres", "three_four_fullres"):
            try:
                pct = self._pct_var.get()
            except (tk.TclError, ValueError):
                self.log("Invalid percentage value", "error")
                return
            try:
                from outpaint_geometry import (
                    compute_full_res_expand_plan,
                    compute_provider_caps,
                )
                from PIL import Image as _PILImg, ImageOps as _PILOps
                with _PILImg.open(image_path) as _im:
                    _iw, _ih = _PILOps.exif_transpose(_im).size
                full_res_plan = compute_full_res_expand_plan(
                    _iw, _ih, pct,
                    compute_provider_caps("bfl" if has_bfl else "fal"),
                    (3, 4) if mode == "three_four_fullres" else None,
                )
            except Exception as e:
                self.log(f"Could not plan full-res expand: {e}", "error")
                return
            self.log(
                f"Full-res expand ({mode}) zoom-out {pct}% — original kept at "
                f"native resolution → {full_res_plan['full_canvas_w']}x"
                f"{full_res_plan['full_canvas_h']}",
                "info",
            )
        elif mode == "percentage":
            try:
                pct = self._pct_var.get()
            except (tk.TclError, ValueError):
                self.log("Invalid percentage value", "error")
                return
            max_per_side = 2048 if has_bfl else 700
            try:
                expand_left, expand_right, expand_top, expand_bottom = (
                    self._calculate_expand_pixels(image_path, pct, max_per_side)
                )
            except Exception as e:
                self.log(f"Could not read image dimensions: {e}", "error")
                return
            self.log(
                f"Expanding {pct}% → L={expand_left} R={expand_right} "
                f"T={expand_top} B={expand_bottom} px",
                "debug",
            )
        else:
            try:
                expand_left = self.left_var.get()
                expand_right = self.right_var.get()
                expand_top = self.top_var.get()
                expand_bottom = self.bottom_var.get()
            except (tk.TclError, ValueError):
                self.log("Invalid numeric value in expand settings", "error")
                return

        self._set_busy(True)

        # Capture reference path before spawning thread (thread-safety)
        ref = self.image_session.reference_entry
        ref_path = ref.path if ref and ref.exists else None

        def _run():
            try:
                from outpaint_generator import OutpaintGenerator

                freeimage_key = self.get_config().get("freeimage_api_key")
                bfl_key = self.get_config().get("bfl_api_key")
                gen = OutpaintGenerator(
                    api_key, freeimage_key=freeimage_key, bfl_api_key=bfl_key,
                )
                self.outpaint_generator = gen
                gen.set_progress_callback(
                    lambda msg, lvl: self.winfo_toplevel().after(
                        0, lambda m=msg, l=lvl: self.log(m, l)
                    )
                )
                result = gen.outpaint(
                    image_path=image_path,
                    output_folder=output_folder,
                    expand_left=expand_left,
                    expand_right=expand_right,
                    expand_top=expand_top,
                    expand_bottom=expand_bottom,
                    prompt=prompt,
                    output_format=output_format,
                    composite_mode=composite_mode,
                    poll_timeout_seconds=get_outpaint_fal_timeout_seconds(self.get_config()),
                    full_res_plan=full_res_plan,
                )

                # Compute face similarity against original portrait
                similarity = None
                if ref_path and result:
                    try:
                        from face_similarity import compute_face_similarity
                        similarity = compute_face_similarity(ref_path, result, report_cb=self.log)
                    except Exception as exc:
                        self.winfo_toplevel().after(
                            0, lambda e=exc: self.log(f"Sim: failed — {type(e).__name__}: {e!r}", "warning")
                        )

                self.winfo_toplevel().after(
                    0, lambda: self._on_complete(result, similarity)
                )
            except Exception as e:
                err = format_exception_detail(e)
                self.winfo_toplevel().after(
                    0, lambda: self._on_error(err)
                )

        threading.Thread(target=_run, daemon=True).start()

    def _on_complete(self, result, similarity=None):
        self._set_busy(False)
        if result:
            sim_str = f"{similarity}%" if similarity is not None else None
            self.image_session.add_image(result, "outpaint", similarity=sim_str)
            sim_msg = f" (Similarity: {similarity}%)" if similarity is not None else ""
            self.log(
                f"Outpaint complete{sim_msg}: {os.path.basename(result)}", "success"
            )
        else:
            detail = ""
            gen = getattr(self, "outpaint_generator", None)
            if gen is not None and hasattr(gen, "get_last_outpaint_error_detail"):
                detail = gen.get_last_outpaint_error_detail() or ""
            from outpaint_generator import OutpaintGenerator
            msg = OutpaintGenerator.format_error_detail(detail)
            self.log(msg, "error")

    def _on_error(self, error):
        self._set_busy(False)
        self.log(f"Error: {error}", "error")

    def _set_busy(self, busy):
        self._busy = busy
        self.expand_btn.config(
            state=tk.DISABLED if busy else tk.NORMAL,
            text="Expanding..." if busy else "Expand Image",
        )
        self.status_label.config(
            text="Processing..." if busy else "",
            fg=COLORS["progress"] if busy else COLORS["text_dim"],
        )

    def get_config_updates(self) -> dict:
        return {
            "outpaint_expand_mode": self._expand_mode_var.get(),
            "outpaint_expand_percentage": self._pct_var.get(),
            "outpaint_expand_left": self.left_var.get(),
            "outpaint_expand_right": self.right_var.get(),
            "outpaint_expand_top": self.top_var.get(),
            "outpaint_expand_bottom": self.bottom_var.get(),
            # Phase G: writes go to the section-specific key.
            "outpaint_tab_prompt": self.prompt_text.get("1.0", tk.END).strip(),
            "outpaint_format": self.format_var.get(),
            "outpaint_composite_mode": self._composite_mode_var.get(),
        }
