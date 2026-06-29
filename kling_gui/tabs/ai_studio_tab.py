"""AI Studio Tab — natural-language image editing.

Takes the current carousel image, runs an edit prompt through the already-wired
fal.ai editing models (Nano Banana 2 Edit / Flux Pro Kontext Max / GPT Image 2
Edit), and shows a large before/after side-by-side with a synced zoom+pan
close-up for pixel inspection. Curated, editable preset prompts plus a custom
prompt box. Results can be added to the carousel (source_type "edit") or used as
the new input for iterative editing.

The editing backend is reused verbatim from ``selfie_generator.SelfieGenerator``
— this tab is UI + orchestration only.
"""

import os
import re
import threading
import tkinter as tk
from tkinter import ttk
from typing import Callable, Optional

from ..theme import (
    COLORS,
    FONT_FAMILY,
    TTK_BTN_DANGER,
    TTK_BTN_PRIMARY,
    TTK_BTN_SECONDARY,
    TTK_BTN_SUCCESS,
    TTK_BTN_WORKFLOW,
    debounce_command,
)
from ..image_state import ImageSession
from log_utils import format_exception_detail
from path_utils import get_gen_images_folder

# PIL for canvas thumbnails (mirrors face_crop_tab/compare_panel guarded import).
try:
    from PIL import Image, ImageOps, ImageTk

    HAS_PIL = True
except ImportError:  # pragma: no cover - environment without Pillow
    HAS_PIL = False


#: Curated default edit presets. Single source of truth — the config seeder in
#: ``main_window`` imports this so fresh + existing installs get the same set.
DEFAULT_AI_STUDIO_PRESETS = [
    {
        "name": "Remove glasses",
        "prompt": (
            "Remove the eyeglasses from the subject's face. Preserve every other "
            "feature exactly: identity, eye shape and color, skin texture, hair, "
            "expression, pose, lighting, and background. Naturally reconstruct the "
            "eyes and surrounding skin. Change nothing else."
        ),
    },
    {
        "name": "More authentic / candid",
        "prompt": (
            "Make this look like a real, candid, un-staged photo of the same "
            "person. Keep their identity and features identical. Add natural, "
            "slightly imperfect realism and remove any over-polished, studio, or "
            "AI-generated look."
        ),
    },
    {
        "name": "Improve lighting & contrast",
        "prompt": (
            "Improve the lighting and contrast for a flattering, natural result: "
            "even out harsh shadows, balance exposure, keep skin tones realistic. "
            "Do not change the person's identity, features, pose, or background."
        ),
    },
    {
        "name": "Cheap phone / older iPhone candid",
        "prompt": (
            "Re-render as if shot casually on an older, cheaper smartphone (early "
            "iPhone): slightly soft focus, mild noise and grain, flatter dynamic "
            "range, on-camera-flash feel, candid framing. Keep the same person and "
            "scene; only change the camera look."
        ),
    },
    {
        "name": "Change clothing",
        "prompt": (
            "Change the subject's clothing to [DESCRIBE OUTFIT HERE]. Keep the "
            "person's face, identity, hair, pose, and background unchanged."
        ),
    },
    {
        "name": "Clean up background",
        "prompt": (
            "Replace the background with a clean, neutral, natural-looking setting. "
            "Keep the subject, their pose, the lighting on their face, and all "
            "features unchanged."
        ),
    },
    {
        "name": "Subtle skin cleanup",
        "prompt": (
            "Lightly clean up the skin: reduce temporary blemishes and harsh shine "
            "while keeping natural skin texture and pores and the person's real "
            "features. Do not smooth into a plastic look or change identity."
        ),
    },
]


class AIStudioTab(tk.Frame):
    """Tab 1: edit the active carousel image with a natural-language prompt."""

    def __init__(
        self,
        parent,
        image_session: ImageSession,
        config: dict,
        config_getter: Callable[[], dict],
        log_callback: Callable[[str, str], None],
        config_saver: Optional[Callable[[], None]] = None,
        **kwargs,
    ):
        """
        Args:
            parent: Parent widget (the notebook).
            image_session: Shared image session state (carousel).
            config: Initial config dict.
            config_getter: Function returning the current (live) config.
            log_callback: log(message, level).
            config_saver: Function persisting config to disk immediately.
        """
        super().__init__(parent, bg=COLORS["bg_panel"], **kwargs)
        self.image_session = image_session
        self.config = config
        self.get_config = config_getter
        self.log = log_callback
        self._config_saver = config_saver

        # Runtime state.
        self._busy = False  # read by main_window._on_close — keep it.
        self._abort_event: Optional[threading.Event] = None
        self._before_path: Optional[str] = None
        self._after_path: Optional[str] = None
        self._after_pil = None
        self._before_pil = None
        self._before_photo = None  # retain to prevent GC
        self._after_photo = None  # retain to prevent GC
        self._last_similarity: Optional[str] = None

        # Models (the 3 editing endpoints live in models.json / AVAILABLE_MODELS).
        from selfie_generator import SelfieGenerator

        self._models = SelfieGenerator.get_available_models()
        self._model_labels = [
            self._format_model_label(m) for m in self._models
        ]
        default_ep = config.get(
            "ai_studio_model_endpoint", "fal-ai/nano-banana-2/edit"
        )
        self._selected_model_index = self._index_for_endpoint(default_ep)

        # Presets (curated default if config has none).
        cfg_presets = config.get("ai_studio_presets")
        if isinstance(cfg_presets, list) and cfg_presets:
            self._presets = [dict(p) for p in cfg_presets if isinstance(p, dict)]
        else:
            self._presets = [dict(p) for p in DEFAULT_AI_STUDIO_PRESETS]

        self._build_ui()

        # Seed the Before pane from the current carousel image + subscribe.
        self.image_session.add_on_change(self._on_session_change)
        self.after(120, self._on_session_change)

    # ── Model helpers ─────────────────────────────────────────────────

    @staticmethod
    def _format_model_label(model: dict) -> str:
        label = model.get("label", model.get("endpoint", "model"))
        price = model.get("price_note", "")
        return f"{label}  ({price})" if price else label

    def _index_for_endpoint(self, endpoint: str) -> int:
        for i, m in enumerate(self._models):
            if m.get("endpoint") == endpoint:
                return i
        return 0

    def _selected_model(self) -> dict:
        if 0 <= self._selected_model_index < len(self._models):
            return self._models[self._selected_model_index]
        return self._models[0] if self._models else {}

    # ── UI ────────────────────────────────────────────────────────────

    def _build_ui(self):
        outer = tk.Frame(self, bg=COLORS["bg_panel"])
        outer.pack(fill=tk.BOTH, expand=True, padx=8, pady=6)

        # Title.
        title_row = tk.Frame(outer, bg=COLORS["bg_panel"])
        title_row.pack(fill=tk.X, pady=(0, 4))
        tk.Label(
            title_row,
            text="AI STUDIO",
            bg=COLORS["bg_panel"],
            fg=COLORS["text_light"],
            font=(FONT_FAMILY, 13, "bold"),
        ).pack(side=tk.LEFT)
        tk.Label(
            title_row,
            text="  Edit the current carousel image with a prompt",
            bg=COLORS["bg_panel"],
            fg=COLORS["text_dim"],
            font=(FONT_FAMILY, 9),
        ).pack(side=tk.LEFT)

        # ── Controls ──────────────────────────────────────────────────
        controls = tk.Frame(outer, bg=COLORS["bg_panel"])
        controls.pack(fill=tk.X, pady=(0, 4))

        model_row = tk.Frame(controls, bg=COLORS["bg_panel"])
        model_row.pack(fill=tk.X, pady=(0, 4))
        tk.Label(
            model_row,
            text="Edit Model:",
            bg=COLORS["bg_panel"],
            fg=COLORS["text_dim"],
            font=(FONT_FAMILY, 9),
        ).pack(side=tk.LEFT, padx=(0, 6))
        self._model_var = tk.StringVar(
            value=(
                self._model_labels[self._selected_model_index]
                if self._model_labels
                else ""
            )
        )
        self._model_combo = ttk.Combobox(
            model_row,
            textvariable=self._model_var,
            values=self._model_labels,
            state="readonly",
            font=(FONT_FAMILY, 9),
            width=40,
        )
        self._model_combo.pack(side=tk.LEFT)
        self._model_combo.bind("<<ComboboxSelected>>", self._on_model_changed)

        # Prompt entry.
        tk.Label(
            controls,
            text="Edit Prompt:",
            bg=COLORS["bg_panel"],
            fg=COLORS["text_dim"],
            font=(FONT_FAMILY, 9),
        ).pack(anchor="w")
        prompt_wrap = tk.Frame(controls, bg=COLORS["bg_panel"])
        prompt_wrap.pack(fill=tk.X, pady=(2, 4))
        self._prompt_text = tk.Text(
            prompt_wrap,
            height=4,
            wrap=tk.WORD,
            bg=COLORS["bg_input"],
            fg=COLORS["text_light"],
            insertbackground=COLORS["text_light"],
            font=(FONT_FAMILY, 10),
            relief="flat",
            highlightthickness=1,
            highlightbackground=COLORS["border"],
        )
        self._prompt_text.pack(side=tk.LEFT, fill=tk.X, expand=True)
        prompt_scroll = ttk.Scrollbar(
            prompt_wrap, orient="vertical", command=self._prompt_text.yview
        )
        prompt_scroll.pack(side=tk.RIGHT, fill=tk.Y)
        self._prompt_text.config(yscrollcommand=prompt_scroll.set)
        last_prompt = self.config.get("ai_studio_last_custom_prompt", "")
        if last_prompt:
            self._prompt_text.insert("1.0", last_prompt)

        # Preset buttons row (rebuilt on edit).
        preset_label_row = tk.Frame(controls, bg=COLORS["bg_panel"])
        preset_label_row.pack(fill=tk.X)
        tk.Label(
            preset_label_row,
            text="Presets:",
            bg=COLORS["bg_panel"],
            fg=COLORS["text_dim"],
            font=(FONT_FAMILY, 9),
        ).pack(side=tk.LEFT, padx=(0, 6))
        ttk.Button(
            preset_label_row,
            text="Edit Presets…",
            style=TTK_BTN_SECONDARY,
            command=debounce_command(
                self._open_presets_editor, key="aistudio_edit_presets"
            ),
        ).pack(side=tk.RIGHT)
        self._preset_row = tk.Frame(controls, bg=COLORS["bg_panel"])
        self._preset_row.pack(fill=tk.X, pady=(2, 4))
        self._rebuild_preset_buttons()

        # Action row.
        action_row = tk.Frame(controls, bg=COLORS["bg_panel"])
        action_row.pack(fill=tk.X, pady=(2, 2))
        self._run_btn = ttk.Button(
            action_row,
            text="Run Edit",
            style=TTK_BTN_WORKFLOW,
            command=debounce_command(self._on_run, key="aistudio_run"),
        )
        self._run_btn.pack(side=tk.LEFT)
        self._abort_btn = ttk.Button(
            action_row,
            text="Abort",
            style=TTK_BTN_DANGER,
            state=tk.DISABLED,
            command=debounce_command(self._on_abort, key="aistudio_abort"),
        )
        self._abort_btn.pack(side=tk.LEFT, padx=(6, 0))
        self._status_label = tk.Label(
            action_row,
            text="",
            bg=COLORS["bg_panel"],
            fg=COLORS["text_dim"],
            font=(FONT_FAMILY, 9),
        )
        self._status_label.pack(side=tk.LEFT, padx=(10, 0))

        # Result-actions row.
        result_row = tk.Frame(controls, bg=COLORS["bg_panel"])
        result_row.pack(fill=tk.X, pady=(2, 0))
        self._add_btn = ttk.Button(
            result_row,
            text="Add to Carousel",
            style=TTK_BTN_SUCCESS,
            state=tk.DISABLED,
            command=debounce_command(
                self._on_add_to_carousel, key="aistudio_add"
            ),
        )
        self._add_btn.pack(side=tk.LEFT)
        self._use_btn = ttk.Button(
            result_row,
            text="Use Result as Input",
            style=TTK_BTN_PRIMARY,
            state=tk.DISABLED,
            command=debounce_command(self._on_use_as_input, key="aistudio_use"),
        )
        self._use_btn.pack(side=tk.LEFT, padx=(6, 0))
        self._zoom_btn = ttk.Button(
            result_row,
            text="\U0001f50d Zoom Compare",
            style=TTK_BTN_SECONDARY,
            state=tk.DISABLED,
            command=debounce_command(
                self._open_zoom_compare, key="aistudio_zoom"
            ),
        )
        self._zoom_btn.pack(side=tk.LEFT, padx=(6, 0))

        # ── Before / After viewer ─────────────────────────────────────
        viewer = tk.Frame(outer, bg=COLORS["bg_panel"])
        viewer.pack(fill=tk.BOTH, expand=True, pady=(6, 0))

        left = tk.Frame(viewer, bg=COLORS["bg_panel"])
        left.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        tk.Label(
            left,
            text="Before (original)",
            bg=COLORS["bg_panel"],
            fg=COLORS["text_dim"],
            font=(FONT_FAMILY, 9, "bold"),
        ).pack(anchor="w")
        self._before_canvas = tk.Canvas(
            left, bg=COLORS["bg_input"], highlightthickness=0
        )
        self._before_canvas.pack(fill=tk.BOTH, expand=True)
        self._before_canvas.bind(
            "<Configure>", lambda e: self._rerender_before()
        )

        tk.Frame(viewer, bg=COLORS["border"], width=2).pack(
            side=tk.LEFT, fill=tk.Y, padx=4
        )

        right = tk.Frame(viewer, bg=COLORS["bg_panel"])
        right.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        self._after_caption = tk.Label(
            right,
            text="After (run an edit)",
            bg=COLORS["bg_panel"],
            fg=COLORS["text_dim"],
            font=(FONT_FAMILY, 9, "bold"),
        )
        self._after_caption.pack(anchor="w")
        self._after_canvas = tk.Canvas(
            right, bg=COLORS["bg_input"], highlightthickness=0
        )
        self._after_canvas.pack(fill=tk.BOTH, expand=True)
        self._after_canvas.bind("<Configure>", lambda e: self._rerender_after())

    def _rebuild_preset_buttons(self):
        for child in self._preset_row.winfo_children():
            child.destroy()
        for idx, preset in enumerate(self._presets):
            name = preset.get("name", f"Preset {idx + 1}")
            ttk.Button(
                self._preset_row,
                text=name,
                style=TTK_BTN_SECONDARY,
                command=debounce_command(
                    lambda i=idx: self._apply_preset(i),
                    key=f"aistudio_preset_{idx}",
                ),
            ).pack(side=tk.LEFT, padx=(0, 4), pady=2)

    def _apply_preset(self, index: int):
        if not (0 <= index < len(self._presets)):
            return
        prompt = self._presets[index].get("prompt", "")
        self._prompt_text.delete("1.0", tk.END)
        self._prompt_text.insert("1.0", prompt)

    # ── Canvas rendering ──────────────────────────────────────────────

    def _show_on_canvas(self, canvas: tk.Canvas, pil_img):
        """Fit a PIL image into a canvas, centered. Returns the PhotoImage."""
        canvas.update_idletasks()
        cw = max(canvas.winfo_width(), 100)
        ch = max(canvas.winfo_height(), 100)
        img = pil_img.copy()
        img.thumbnail((cw, ch))
        photo = ImageTk.PhotoImage(img)
        canvas.delete("all")
        canvas.create_image(cw // 2, ch // 2, image=photo, anchor="center")
        return photo

    def _load_pil(self, path: str):
        """EXIF-safe load to RGB PIL image, or None on failure."""
        if not (HAS_PIL and path and os.path.isfile(path)):
            return None
        try:
            img = Image.open(path)
            img = ImageOps.exif_transpose(img)
            if img.mode not in ("RGB", "RGBA"):
                img = img.convert("RGB")
            return img
        except Exception:
            return None

    def _rerender_before(self):
        if self._before_pil is not None:
            self._before_photo = self._show_on_canvas(
                self._before_canvas, self._before_pil
            )

    def _rerender_after(self):
        if self._after_pil is not None:
            self._after_photo = self._show_on_canvas(
                self._after_canvas, self._after_pil
            )

    def _set_before(self, path: str):
        self._before_path = path
        self._before_pil = self._load_pil(path)
        self._rerender_before()

    def _clear_after(self):
        self._after_path = None
        self._after_pil = None
        self._after_photo = None
        self._after_canvas.delete("all")
        self._after_caption.config(text="After (run an edit)")
        self._add_btn.config(state=tk.DISABLED)
        self._use_btn.config(state=tk.DISABLED)
        self._zoom_btn.config(state=tk.DISABLED)

    # ── Session sync ──────────────────────────────────────────────────

    def _on_session_change(self):
        path = self.image_session.active_image_path
        if not path:
            return
        if path == self._before_path:
            return
        # Don't pull videos into the editor.
        entry = self.image_session.active_entry
        if entry is not None and getattr(entry, "is_video", False):
            return
        self._set_before(path)

    # ── Run edit ──────────────────────────────────────────────────────

    def _on_model_changed(self, event=None):
        self._selected_model_index = self._model_combo.current()
        self._save_config_now()

    def _on_run(self):
        if self._busy:
            return
        cfg = self.get_config()
        api_key = (cfg.get("falai_api_key") or "").strip()
        if not api_key:
            self.log(
                "fal.ai API key required — set it in the bottom bar.", "error"
            )
            return
        image_path = self._before_path or self.image_session.active_image_path
        if not image_path or not os.path.isfile(image_path):
            self.log("No image selected in carousel.", "warning")
            return
        prompt = self._prompt_text.get("1.0", tk.END).strip()
        if not prompt:
            self.log("Enter an edit prompt (or pick a preset).", "warning")
            return

        model = self._selected_model()
        endpoint = model.get("endpoint", "")
        label = model.get("label", endpoint)

        try:
            output_folder = get_gen_images_folder(image_path)
            os.makedirs(output_folder, exist_ok=True)
        except Exception as exc:
            self.log(f"Could not resolve output folder: {exc}", "error")
            return

        self._save_config_now()
        self._set_busy(True)
        self._abort_event = threading.Event()

        freeimage_key = cfg.get("freeimage_api_key")
        bfl_api_key = cfg.get("bfl_api_key")
        poll_timeout = cfg.get("selfie_poll_timeout_seconds")

        def _run():
            try:
                from selfie_generator import SelfieGenerator

                gen = SelfieGenerator(
                    api_key,
                    freeimage_key=freeimage_key,
                    bfl_api_key=bfl_api_key,
                )
                gen.set_progress_callback(
                    lambda msg, lvl: self.winfo_toplevel().after(
                        0, lambda m=msg, l=lvl: self.log(m, l)
                    )
                )
                if self._abort_event is not None:
                    gen.set_cancel_event(self._abort_event)
                result = gen.generate(
                    image_path=image_path,
                    prompt=prompt,
                    output_folder=output_folder,
                    model_endpoint=endpoint,
                    model_label=label,
                    poll_timeout_seconds=poll_timeout,
                )
                self.winfo_toplevel().after(
                    0, lambda: self._on_run_done(result)
                )
            except Exception as e:
                err = format_exception_detail(e)
                self.winfo_toplevel().after(
                    0, lambda: self._on_run_error(err)
                )

        threading.Thread(target=_run, daemon=True).start()

    def _on_run_done(self, result_path: Optional[str]):
        self._set_busy(False)
        if not result_path or not os.path.isfile(result_path):
            self.log("Edit failed — see the reason logged above.", "error")
            return
        self._after_path = result_path
        self._after_pil = self._load_pil(result_path)
        self._rerender_after()
        self._last_similarity = self._extract_similarity_from_result_path(
            result_path
        )
        if self._last_similarity:
            self._after_caption.config(
                text=f"After  —  Identity match: {self._last_similarity}"
            )
        else:
            self._after_caption.config(text="After")
        self._add_btn.config(state=tk.NORMAL)
        self._use_btn.config(state=tk.NORMAL)
        self._zoom_btn.config(state=tk.NORMAL)
        self.log(
            f"Edit complete: {os.path.basename(result_path)}", "success"
        )

    def _on_run_error(self, error: str):
        self._set_busy(False)
        self.log(f"Edit error: {error}", "error")

    def _on_abort(self):
        if self._abort_event is not None:
            self._abort_event.set()
            self.log("Abort requested — finishing current step…", "warning")

    def _set_busy(self, busy: bool):
        self._busy = busy
        self._run_btn.config(state=tk.DISABLED if busy else tk.NORMAL)
        self._abort_btn.config(state=tk.NORMAL if busy else tk.DISABLED)
        self._status_label.config(
            text="Editing…" if busy else "",
            fg=COLORS.get("progress", COLORS["text_dim"])
            if busy
            else COLORS["text_dim"],
        )

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

    # ── Result actions ────────────────────────────────────────────────

    def _on_add_to_carousel(self):
        if not (self._after_path and os.path.isfile(self._after_path)):
            return
        self.image_session.add_image(
            self._after_path,
            "edit",
            label=os.path.basename(self._after_path),
            make_active=True,
            similarity=self._last_similarity,
        )
        self.log(
            f"Added to carousel: {os.path.basename(self._after_path)}",
            "success",
        )

    def _on_use_as_input(self):
        if not (self._after_path and os.path.isfile(self._after_path)):
            return
        new_input = self._after_path
        self._set_before(new_input)
        self._clear_after()
        self.log("Using edited image as the new input.", "info")

    # ── Zoom compare (synced zoom + pan) ──────────────────────────────

    def _open_zoom_compare(self):
        if not (self._before_pil is not None and self._after_pil is not None):
            self.log("Run an edit first to compare.", "warning")
            return

        before_img = self._before_pil
        after_img = self._after_pil

        top = tk.Toplevel(self)
        top.title("Zoom Compare — Before / After")
        top.configure(bg=COLORS["bg_main"])
        top.geometry("1200x720")
        top.transient(self.winfo_toplevel())

        # Shared zoom/pan state drives BOTH canvases in lockstep.
        state = {"scale": 1.0, "ox": 0.0, "oy": 0.0, "drag": None}
        top._zoom_photos = []  # retain refs to prevent GC

        toolbar = tk.Frame(top, bg=COLORS["bg_panel"])
        toolbar.pack(fill=tk.X)
        tk.Label(
            toolbar,
            text="Scroll = zoom (both)   •   Drag = pan (both)",
            bg=COLORS["bg_panel"],
            fg=COLORS["text_dim"],
            font=(FONT_FAMILY, 9),
        ).pack(side=tk.LEFT, padx=8, pady=4)

        body = tk.Frame(top, bg=COLORS["bg_main"])
        body.pack(fill=tk.BOTH, expand=True)

        left = tk.Frame(body, bg=COLORS["bg_main"])
        left.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        tk.Label(
            left,
            text="Before",
            bg=COLORS["bg_main"],
            fg=COLORS["text_dim"],
            font=(FONT_FAMILY, 9, "bold"),
        ).pack(anchor="w")
        left_canvas = tk.Canvas(left, bg=COLORS["bg_input"], highlightthickness=0)
        left_canvas.pack(fill=tk.BOTH, expand=True)

        tk.Frame(body, bg=COLORS["border"], width=2).pack(
            side=tk.LEFT, fill=tk.Y, padx=2
        )

        right = tk.Frame(body, bg=COLORS["bg_main"])
        right.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        tk.Label(
            right,
            text="After",
            bg=COLORS["bg_main"],
            fg=COLORS["text_dim"],
            font=(FONT_FAMILY, 9, "bold"),
        ).pack(anchor="w")
        right_canvas = tk.Canvas(
            right, bg=COLORS["bg_input"], highlightthickness=0
        )
        right_canvas.pack(fill=tk.BOTH, expand=True)

        def _fit_base_scale():
            """Base scale so each image fits its canvas at scale=1.0."""
            cw = max(left_canvas.winfo_width(), 100)
            ch = max(left_canvas.winfo_height(), 100)
            iw = max(before_img.width, after_img.width, 1)
            ih = max(before_img.height, after_img.height, 1)
            return min(cw / iw, ch / ih)

        def _redraw():
            top._zoom_photos = []
            base = _fit_base_scale()
            eff = base * state["scale"]
            for canvas, src in (
                (left_canvas, before_img),
                (right_canvas, after_img),
            ):
                cw = max(canvas.winfo_width(), 100)
                ch = max(canvas.winfo_height(), 100)
                nw = max(int(src.width * eff), 1)
                nh = max(int(src.height * eff), 1)
                resized = src.resize((nw, nh), Image.LANCZOS)
                photo = ImageTk.PhotoImage(resized)
                top._zoom_photos.append(photo)
                canvas.delete("all")
                canvas.create_image(
                    cw / 2 + state["ox"],
                    ch / 2 + state["oy"],
                    image=photo,
                    anchor="center",
                )

        def _on_wheel(event):
            # Normalize wheel delta across platforms.
            if getattr(event, "num", None) == 4:
                factor = 1.1
            elif getattr(event, "num", None) == 5:
                factor = 1 / 1.1
            else:
                factor = 1.1 if event.delta > 0 else 1 / 1.1
            new_scale = max(0.1, min(20.0, state["scale"] * factor))
            # Zoom toward the cursor: keep the point under the pointer stable.
            cw = max(event.widget.winfo_width(), 100)
            ch = max(event.widget.winfo_height(), 100)
            px = event.x - (cw / 2 + state["ox"])
            py = event.y - (ch / 2 + state["oy"])
            ratio = new_scale / state["scale"] if state["scale"] else 1.0
            state["ox"] -= px * (ratio - 1)
            state["oy"] -= py * (ratio - 1)
            state["scale"] = new_scale
            _redraw()

        def _on_press(event):
            state["drag"] = (event.x, event.y)

        def _on_motion(event):
            if state["drag"] is None:
                return
            dx = event.x - state["drag"][0]
            dy = event.y - state["drag"][1]
            state["drag"] = (event.x, event.y)
            state["ox"] += dx
            state["oy"] += dy
            _redraw()

        def _on_release(event):
            state["drag"] = None

        def _reset():
            state.update({"scale": 1.0, "ox": 0.0, "oy": 0.0, "drag": None})
            _redraw()

        ttk.Button(
            toolbar, text="Reset", style=TTK_BTN_SECONDARY, command=_reset
        ).pack(side=tk.RIGHT, padx=8, pady=4)

        for canvas in (left_canvas, right_canvas):
            canvas.bind("<MouseWheel>", _on_wheel)
            canvas.bind("<Button-4>", _on_wheel)
            canvas.bind("<Button-5>", _on_wheel)
            canvas.bind("<ButtonPress-1>", _on_press)
            canvas.bind("<B1-Motion>", _on_motion)
            canvas.bind("<ButtonRelease-1>", _on_release)
            canvas.bind("<Configure>", lambda e: _redraw())

        top.bind("<Escape>", lambda e: top.destroy())
        self.after(80, _redraw)

    # ── Presets editor ────────────────────────────────────────────────

    def _open_presets_editor(self):
        top = tk.Toplevel(self)
        top.title("Edit AI Studio Presets")
        top.configure(bg=COLORS["bg_panel"])
        top.geometry("640x460")
        top.transient(self.winfo_toplevel())
        top.grab_set()

        # Working copy so Cancel discards.
        working = [dict(p) for p in self._presets]

        body = tk.Frame(top, bg=COLORS["bg_panel"])
        body.pack(fill=tk.BOTH, expand=True, padx=8, pady=8)

        left = tk.Frame(body, bg=COLORS["bg_panel"])
        left.pack(side=tk.LEFT, fill=tk.Y)
        tk.Label(
            left,
            text="Presets",
            bg=COLORS["bg_panel"],
            fg=COLORS["text_dim"],
            font=(FONT_FAMILY, 9, "bold"),
        ).pack(anchor="w")
        listbox = tk.Listbox(
            left,
            width=26,
            height=16,
            bg=COLORS["bg_input"],
            fg=COLORS["text_light"],
            selectbackground=COLORS["accent_blue"],
            highlightthickness=0,
            font=(FONT_FAMILY, 9),
            exportselection=False,
        )
        listbox.pack(fill=tk.Y, expand=True, pady=(2, 0))

        right = tk.Frame(body, bg=COLORS["bg_panel"])
        right.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, padx=(8, 0))
        tk.Label(
            right,
            text="Name",
            bg=COLORS["bg_panel"],
            fg=COLORS["text_dim"],
            font=(FONT_FAMILY, 9),
        ).pack(anchor="w")
        name_var = tk.StringVar()
        name_entry = tk.Entry(
            right,
            textvariable=name_var,
            bg=COLORS["bg_input"],
            fg=COLORS["text_light"],
            insertbackground=COLORS["text_light"],
            font=(FONT_FAMILY, 10),
            relief="flat",
            highlightthickness=1,
            highlightbackground=COLORS["border"],
        )
        name_entry.pack(fill=tk.X, pady=(2, 6))
        tk.Label(
            right,
            text="Prompt",
            bg=COLORS["bg_panel"],
            fg=COLORS["text_dim"],
            font=(FONT_FAMILY, 9),
        ).pack(anchor="w")
        prompt_box = tk.Text(
            right,
            height=10,
            wrap=tk.WORD,
            bg=COLORS["bg_input"],
            fg=COLORS["text_light"],
            insertbackground=COLORS["text_light"],
            font=(FONT_FAMILY, 10),
            relief="flat",
            highlightthickness=1,
            highlightbackground=COLORS["border"],
        )
        prompt_box.pack(fill=tk.BOTH, expand=True, pady=(2, 6))

        state = {"selected": None}

        def _refresh_list(select: Optional[int] = None):
            listbox.delete(0, tk.END)
            for p in working:
                listbox.insert(tk.END, p.get("name", "(unnamed)"))
            if select is not None and 0 <= select < len(working):
                listbox.selection_clear(0, tk.END)
                listbox.selection_set(select)
                state["selected"] = select
                _load_into_fields(select)

        def _load_into_fields(idx: int):
            if not (0 <= idx < len(working)):
                return
            name_var.set(working[idx].get("name", ""))
            prompt_box.delete("1.0", tk.END)
            prompt_box.insert("1.0", working[idx].get("prompt", ""))

        def _on_select(event=None):
            sel = listbox.curselection()
            if sel:
                state["selected"] = sel[0]
                _load_into_fields(sel[0])

        listbox.bind("<<ListboxSelect>>", _on_select)

        def _add():
            working.append({"name": "New preset", "prompt": ""})
            _refresh_list(select=len(working) - 1)

        def _update():
            idx = state["selected"]
            if idx is None or not (0 <= idx < len(working)):
                return
            working[idx] = {
                "name": name_var.get().strip() or f"Preset {idx + 1}",
                "prompt": prompt_box.get("1.0", tk.END).strip(),
            }
            _refresh_list(select=idx)

        def _delete():
            idx = state["selected"]
            if idx is None or not (0 <= idx < len(working)):
                return
            working.pop(idx)
            state["selected"] = None
            name_var.set("")
            prompt_box.delete("1.0", tk.END)
            _refresh_list()

        def _reset_defaults():
            working.clear()
            working.extend(dict(p) for p in DEFAULT_AI_STUDIO_PRESETS)
            _refresh_list(select=0)

        def _save():
            # Capture in-flight edits to the selected row if the user didn't
            # press Update.
            idx = state["selected"]
            if idx is not None and 0 <= idx < len(working):
                working[idx] = {
                    "name": name_var.get().strip() or f"Preset {idx + 1}",
                    "prompt": prompt_box.get("1.0", tk.END).strip(),
                }
            self._presets = [
                p for p in working if p.get("name") or p.get("prompt")
            ]
            self._rebuild_preset_buttons()
            self._save_config_now()
            self.log("Presets saved.", "success")
            top.destroy()

        btn_row = tk.Frame(right, bg=COLORS["bg_panel"])
        btn_row.pack(fill=tk.X)
        ttk.Button(
            btn_row, text="Add", style=TTK_BTN_SECONDARY, command=_add
        ).pack(side=tk.LEFT)
        ttk.Button(
            btn_row, text="Update", style=TTK_BTN_PRIMARY, command=_update
        ).pack(side=tk.LEFT, padx=(4, 0))
        ttk.Button(
            btn_row, text="Delete", style=TTK_BTN_DANGER, command=_delete
        ).pack(side=tk.LEFT, padx=(4, 0))

        bottom_row = tk.Frame(top, bg=COLORS["bg_panel"])
        bottom_row.pack(fill=tk.X, padx=8, pady=(0, 8))
        ttk.Button(
            bottom_row,
            text="Reset to defaults",
            style=TTK_BTN_SECONDARY,
            command=_reset_defaults,
        ).pack(side=tk.LEFT)
        ttk.Button(
            bottom_row, text="Save", style=TTK_BTN_SUCCESS, command=_save
        ).pack(side=tk.RIGHT)
        ttk.Button(
            bottom_row,
            text="Cancel",
            style=TTK_BTN_SECONDARY,
            command=top.destroy,
        ).pack(side=tk.RIGHT, padx=(0, 4))

        _refresh_list(select=0 if working else None)

    # ── Persistence ───────────────────────────────────────────────────

    def get_config_updates(self) -> dict:
        """Return AI Studio config values to persist."""
        return {
            "ai_studio_model_endpoint": self._selected_model().get(
                "endpoint", "fal-ai/nano-banana-2/edit"
            ),
            "ai_studio_presets": [dict(p) for p in self._presets],
            "ai_studio_last_custom_prompt": self._prompt_text.get(
                "1.0", tk.END
            ).strip(),
        }

    def _save_config_now(self):
        try:
            self.config.update(self.get_config_updates())
            if self._config_saver:
                self._config_saver()
        except Exception as exc:
            self.log(f"Could not save AI Studio settings: {exc}", "warning")
