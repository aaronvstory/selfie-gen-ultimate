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
    TTK_BTN_COMPACT,
    TTK_BTN_DANGER,
    TTK_BTN_PRIMARY,
    TTK_BTN_SECONDARY,
    TTK_BTN_SUCCESS,
    TTK_BTN_WORKFLOW,
    debounce_command,
)
from ..image_state import ImageSession
from log_utils import format_exception_detail
from outpaint_defaults import DEFAULT_OUTPAINT_EXPAND_PERCENT, OUTPAINT_EXPAND_PERCENT_PRESETS
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
        try:
            expand_pct = int(
                config.get("outpaint_expand_percentage", DEFAULT_OUTPAINT_EXPAND_PERCENT)
                or DEFAULT_OUTPAINT_EXPAND_PERCENT
            )
        except (TypeError, ValueError):
            expand_pct = DEFAULT_OUTPAINT_EXPAND_PERCENT
        self._expand_pct_var = tk.IntVar(
            value=expand_pct
        )

        # Runtime state.
        self._busy = False  # read by main_window._on_close — keep it.
        self._abort_event: Optional[threading.Event] = None
        self._before_path: Optional[str] = None
        # Tracks the carousel's active path we last followed, so we only sync
        # the Before pane on a genuine user navigation — not on every session
        # mutation (which would clobber a "Use Result as Input" chain).
        self._last_synced_carousel_path: Optional[str] = None
        self._after_path: Optional[str] = None
        self._after_pil = None
        self._before_pil = None
        self._before_photo = None  # retain to prevent GC
        self._after_photo = None  # retain to prevent GC
        self._last_similarity: Optional[str] = None
        self._auto_added_carousel_path: Optional[str] = None

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
        outer.pack(fill=tk.BOTH, expand=True, padx=8, pady=(4, 6))

        # ── 3-column body: BEFORE | AFTER | OPTIONS ───────────────────
        # Image panes get the bulk of the width (~38% each); the options
        # column is a tight, self-contained vertical stack (~24%). The
        # standalone title row was dropped to reclaim vertical space so the
        # options column never clips its bottom buttons (the tab name "1. AI
        # Studio" already labels this view).
        body = tk.Frame(outer, bg=COLORS["bg_panel"])
        body.pack(fill=tk.BOTH, expand=True)
        # The portrait images don't fill wide panes (lots of dead side space),
        # so give the OPTIONS column more width and let it lay controls out
        # roomily (no vertical clipping). BEFORE 32% | AFTER 32% | OPTIONS 36%.
        body.grid_columnconfigure(0, weight=32, uniform="aistudio")
        body.grid_columnconfigure(2, weight=32, uniform="aistudio")
        body.grid_columnconfigure(4, weight=36, uniform="aistudio")
        body.grid_rowconfigure(0, weight=1)

        # Column 0: BEFORE.
        before_col = tk.Frame(body, bg=COLORS["bg_panel"])
        before_col.grid(row=0, column=0, sticky="nsew")
        tk.Label(
            before_col,
            text="Before (original)",
            bg=COLORS["bg_panel"],
            fg=COLORS["text_dim"],
            font=(FONT_FAMILY, 9, "bold"),
        ).pack(anchor="w")
        self._before_canvas = tk.Canvas(
            before_col, bg=COLORS["bg_input"], highlightthickness=0
        )
        self._before_canvas.pack(fill=tk.BOTH, expand=True)
        self._before_canvas.bind(
            "<Configure>", lambda e: self._rerender_before()
        )

        tk.Frame(body, bg=COLORS["border"], width=2).grid(
            row=0, column=1, sticky="ns", padx=4
        )

        # Column 2: AFTER.
        after_col = tk.Frame(body, bg=COLORS["bg_panel"])
        after_col.grid(row=0, column=2, sticky="nsew")
        self._after_caption = tk.Label(
            after_col,
            text="After (run an edit)",
            bg=COLORS["bg_panel"],
            fg=COLORS["text_dim"],
            font=(FONT_FAMILY, 9, "bold"),
        )
        self._after_caption.pack(anchor="w")
        self._after_canvas = tk.Canvas(
            after_col, bg=COLORS["bg_input"], highlightthickness=0
        )
        self._after_canvas.pack(fill=tk.BOTH, expand=True)
        self._after_canvas.bind("<Configure>", lambda e: self._rerender_after())

        tk.Frame(body, bg=COLORS["border"], width=2).grid(
            row=0, column=3, sticky="ns", padx=4
        )

        # Column 4: OPTIONS (tight vertical stack — must not overflow).
        self._build_options_column(body)

    def _build_options_column(self, body):
        opts = tk.Frame(body, bg=COLORS["bg_panel"])
        opts.grid(row=0, column=4, sticky="nsew")

        # Model.
        tk.Label(
            opts, text="Edit Model:", bg=COLORS["bg_panel"],
            fg=COLORS["text_dim"], font=(FONT_FAMILY, 9),
        ).pack(anchor="w")
        self._model_var = tk.StringVar(
            value=(
                self._model_labels[self._selected_model_index]
                if self._model_labels
                else ""
            )
        )
        self._model_combo = ttk.Combobox(
            opts,
            textvariable=self._model_var,
            values=self._model_labels,
            state="readonly",
            font=(FONT_FAMILY, 9),
        )
        self._model_combo.pack(fill=tk.X, pady=(2, 6))
        self._model_combo.bind("<<ComboboxSelected>>", self._on_model_changed)

        # Prompt (compact; scrollbar handles longer text).
        tk.Label(
            opts, text="Edit Prompt:", bg=COLORS["bg_panel"],
            fg=COLORS["text_dim"], font=(FONT_FAMILY, 9),
        ).pack(anchor="w")
        prompt_wrap = tk.Frame(opts, bg=COLORS["bg_panel"])
        prompt_wrap.pack(fill=tk.X, pady=(2, 6))
        self._prompt_text = tk.Text(
            prompt_wrap,
            height=4,
            wrap=tk.WORD,
            bg=COLORS["bg_input"],
            fg=COLORS["text_light"],
            insertbackground=COLORS["text_light"],
            font=(FONT_FAMILY, 9),
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

        # Presets header + Edit.
        preset_header = tk.Frame(opts, bg=COLORS["bg_panel"])
        preset_header.pack(fill=tk.X)
        tk.Label(
            preset_header, text="Presets:", bg=COLORS["bg_panel"],
            fg=COLORS["text_dim"], font=(FONT_FAMILY, 9),
        ).pack(side=tk.LEFT)
        ttk.Button(
            preset_header,
            text="Edit…",
            style=TTK_BTN_SECONDARY,
            width=6,
            command=debounce_command(
                self._open_presets_editor, key="aistudio_edit_presets"
            ),
        ).pack(side=tk.RIGHT)

        # IMPORTANT (vertical overflow): pack the action rows from the BOTTOM
        # up first so they're pinned to the column bottom and can never be
        # pushed off-screen. With the wider OPTIONS column, buttons pair up
        # into 2-up rows to keep the vertical footprint short. The scrollable
        # presets box then absorbs whatever space is left (expand=True).

        # Row 3 (bottom-most): Zoom Compare, full width.
        self._zoom_btn = ttk.Button(
            opts,
            text="\U0001f50d Zoom Compare",
            style=TTK_BTN_SECONDARY,
            state=tk.DISABLED,
            command=debounce_command(
                self._open_zoom_compare, key="aistudio_zoom"
            ),
        )
        self._zoom_btn.pack(side=tk.BOTTOM, fill=tk.X)

        # Row 2: Add to Carousel + Use Result as Input, side by side.
        result_row = tk.Frame(opts, bg=COLORS["bg_panel"])
        result_row.pack(side=tk.BOTTOM, fill=tk.X, pady=(0, 3))
        self._add_btn = ttk.Button(
            result_row,
            text="Add to Carousel",
            style=TTK_BTN_SUCCESS,
            state=tk.DISABLED,
            command=debounce_command(
                self._on_add_to_carousel, key="aistudio_add"
            ),
        )
        self._add_btn.pack(side=tk.LEFT, fill=tk.X, expand=True)
        self._use_btn = ttk.Button(
            result_row,
            text="Use as Input",
            style=TTK_BTN_PRIMARY,
            state=tk.DISABLED,
            command=debounce_command(self._on_use_as_input, key="aistudio_use"),
        )
        self._use_btn.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(4, 0))

        self._status_label = tk.Label(
            opts,
            text="",
            bg=COLORS["bg_panel"],
            fg=COLORS["text_dim"],
            font=(FONT_FAMILY, 9),
            anchor="w",
            wraplength=300,
        )
        self._status_label.pack(side=tk.BOTTOM, fill=tk.X, pady=(2, 4))

        # Row 1: Run Edit + Abort, side by side (just above the result row).
        run_row = tk.Frame(opts, bg=COLORS["bg_panel"])
        run_row.pack(side=tk.BOTTOM, fill=tk.X, pady=(4, 2))
        self._run_btn = ttk.Button(
            run_row,
            text="Run Edit",
            style=TTK_BTN_WORKFLOW,
            command=debounce_command(self._on_run, key="aistudio_run"),
        )
        self._run_btn.pack(side=tk.LEFT, fill=tk.X, expand=True)
        self._abort_btn = ttk.Button(
            run_row,
            text="Abort",
            style=TTK_BTN_DANGER,
            state=tk.DISABLED,
            command=debounce_command(self._on_abort, key="aistudio_abort"),
        )
        self._abort_btn.pack(side=tk.LEFT, padx=(4, 0))

        # Expand to 3:4 (full-res): the same generative-expand feature as Step 0
        # / Step 2.5, on the current AI Studio image. Keeps the original at native
        # resolution and extends to 3:4 (Bria borders by default). Result flows
        # into the same Before/After + Add-to-Carousel display as an edit.
        expand_row = tk.Frame(opts, bg=COLORS["bg_panel"])
        expand_row.pack(side=tk.BOTTOM, fill=tk.X, pady=(0, 2))
        self._expand_btn = ttk.Button(
            expand_row,
            text="\U0001f5bc Expand to 3:4",
            style=TTK_BTN_SECONDARY,
            command=debounce_command(
                self._on_expand_3x4, key="aistudio_expand"
            ),
        )
        self._expand_btn.pack(side=tk.LEFT, fill=tk.X, expand=True)

        expand_pct_row = tk.Frame(opts, bg=COLORS["bg_panel"])
        expand_pct_row.pack(side=tk.BOTTOM, fill=tk.X, pady=(0, 4))
        tk.Label(
            expand_pct_row,
            text="Zoom:",
            bg=COLORS["bg_panel"],
            fg=COLORS["text_dim"],
            font=(FONT_FAMILY, 9),
        ).pack(side=tk.LEFT)
        for pct in OUTPAINT_EXPAND_PERCENT_PRESETS:
            label = f"{pct}%"
            if pct == DEFAULT_OUTPAINT_EXPAND_PERCENT:
                label = f"{pct}% default"
            ttk.Button(
                expand_pct_row,
                text=label,
                style=TTK_BTN_COMPACT,
                command=lambda p=pct: self._set_expand_percent(p),
            ).pack(side=tk.LEFT, padx=(4, 0))

        # Presets — vertical full-width stack, scrollable, taking the remaining
        # space between the prompt and the bottom-pinned buttons.
        preset_box = tk.Frame(opts, bg=COLORS["bg_panel"])
        preset_box.pack(fill=tk.BOTH, expand=True, pady=(2, 6))
        self._preset_canvas = tk.Canvas(
            preset_box, bg=COLORS["bg_panel"], highlightthickness=0
        )
        preset_scroll = ttk.Scrollbar(
            preset_box, orient="vertical", command=self._preset_canvas.yview
        )
        self._preset_inner = tk.Frame(self._preset_canvas, bg=COLORS["bg_panel"])
        self._preset_inner.bind(
            "<Configure>",
            lambda e: self._preset_canvas.configure(
                scrollregion=self._preset_canvas.bbox("all")
            ),
        )
        self._preset_window_id = self._preset_canvas.create_window(
            (0, 0), window=self._preset_inner, anchor="nw"
        )
        self._preset_canvas.configure(yscrollcommand=preset_scroll.set)
        self._preset_canvas.bind(
            "<Configure>",
            lambda e: self._preset_canvas.itemconfigure(
                self._preset_window_id, width=e.width
            ),
        )
        preset_scroll.pack(side=tk.RIGHT, fill=tk.Y)
        self._preset_canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        self._rebuild_preset_buttons()

    def _rebuild_preset_buttons(self):
        for child in self._preset_inner.winfo_children():
            child.destroy()
        # 2-column grid — uses the wider OPTIONS column to halve the preset
        # block's vertical footprint. Both columns stretch evenly.
        self._preset_inner.grid_columnconfigure(0, weight=1, uniform="preset")
        self._preset_inner.grid_columnconfigure(1, weight=1, uniform="preset")
        for idx, preset in enumerate(self._presets):
            name = preset.get("name", f"Preset {idx + 1}")
            r, c = divmod(idx, 2)
            ttk.Button(
                self._preset_inner,
                text=name,
                style=TTK_BTN_SECONDARY,
                command=debounce_command(
                    lambda i=idx: self._apply_preset(i),
                    key=f"aistudio_preset_{idx}",
                ),
            ).grid(row=r, column=c, sticky="ew", padx=(0, 2), pady=1)

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
        if not (HAS_PIL and path):
            return None
        try:
            # ``with`` + load() closes the source file handle promptly;
            # exif_transpose / convert return new in-memory images so the
            # handle is no longer needed after the block.
            with Image.open(path) as src:
                img = ImageOps.exif_transpose(src)
                if img.mode not in ("RGB", "RGBA"):
                    img = img.convert("RGB")
                img.load()
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
        self._auto_added_carousel_path = None
        self._after_canvas.delete("all")
        self._after_caption.config(text="After (run an edit)")
        self._add_btn.config(state=tk.DISABLED)
        self._use_btn.config(state=tk.DISABLED)
        self._zoom_btn.config(state=tk.DISABLED)

    # ── Session sync ──────────────────────────────────────────────────

    def _on_session_change(self):
        # Never swap the input out from under an in-flight edit.
        if self._busy:
            return
        path = self.image_session.active_image_path
        if not path:
            return
        if path == self._before_path:
            return
        # Only follow the carousel when its ACTIVE image actually changed (the
        # user navigated/selected). _on_session_change fires on every session
        # mutation, including adds from OTHER tabs and our own — without this
        # guard, "Use Result as Input" (which sets _before_path to the edit
        # result WITHOUT touching the carousel) would be silently undone the
        # next time any tab notifies the session. (code-review CRITICAL)
        if path == self._last_synced_carousel_path:
            return
        # Don't pull videos into the editor.
        entry = self.image_session.active_entry
        if entry is not None and getattr(entry, "is_video", False):
            self._last_synced_carousel_path = path
            return
        self._last_synced_carousel_path = path
        self._set_before(path)
        # The Before image changed under a previous result — drop the stale
        # After preview + disable its action buttons so the user can't add or
        # zoom a result that belongs to a different input. (CodeRabbit Major)
        self._clear_after()

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
        except Exception as exc:  # noqa: BLE001 — keep edit result visible if carousel mutation fails
            self.log(f"Could not resolve output folder: {exc}", "error")
            return

        # Derive width/height from the actual input so the edit preserves the
        # image's aspect ratio. generate() feeds these to the model payload
        # (aspect_ratio / image_size); the hardcoded 864x1152 default would
        # force landscape/square inputs into portrait. (code-review HIGH)
        if self._before_pil is not None:
            in_w, in_h = self._before_pil.width, self._before_pil.height
        else:
            in_w, in_h = 864, 1152

        self._save_config_now()
        self._set_busy(True)
        self._abort_event = threading.Event()

        freeimage_key = cfg.get("freeimage_api_key")
        bfl_api_key = cfg.get("bfl_api_key")
        poll_timeout = cfg.get("selfie_poll_timeout_seconds")

        # Resolve the toplevel in the MAIN thread — Tkinter is not thread-safe,
        # so the worker must not call self.winfo_toplevel(). It dispatches all
        # UI work back via this guarded helper instead.
        toplevel = self.winfo_toplevel()

        def _safe_after(func):
            try:
                if toplevel.winfo_exists():
                    toplevel.after(0, func)
            except Exception:
                pass

        def _run():
            try:
                from selfie_generator import SelfieGenerator

                gen = SelfieGenerator(
                    api_key,
                    freeimage_key=freeimage_key,
                    bfl_api_key=bfl_api_key,
                )
                gen.set_progress_callback(
                    lambda msg, lvl: _safe_after(
                        lambda m=msg, level=lvl: self.log(m, level)
                    )
                )
                if self._abort_event is not None:
                    gen.set_cancel_event(self._abort_event)
                result = gen.generate(
                    image_path=image_path,
                    prompt=prompt,
                    output_folder=output_folder,
                    width=in_w,
                    height=in_h,
                    model_endpoint=endpoint,
                    model_label=label,
                    poll_timeout_seconds=poll_timeout,
                )
                _safe_after(lambda: self._on_run_done(result))
            except Exception as e:
                err = format_exception_detail(e)
                _safe_after(lambda: self._on_run_error(err))

        threading.Thread(target=_run, daemon=True).start()

    def _on_expand_3x4(self):
        """Expand the current image to full-res 3:4 (same engine as Step 0/2.5).

        Keeps the original at native resolution; borders come from the resolved
        strategy (Bria by default, edge-extend without a fal key). The result is
        shown in the same Before/After display as an edit.
        """
        if self._busy:
            return
        cfg = self.get_config()
        api_key = (cfg.get("falai_api_key") or "").strip()
        image_path = self._before_path or self.image_session.active_image_path
        if not image_path or not os.path.isfile(image_path):
            self.log("No image selected in carousel.", "warning")
            return
        try:
            output_folder = get_gen_images_folder(image_path)
            os.makedirs(output_folder, exist_ok=True)
        except Exception as exc:  # noqa: BLE001 — manual Add should log, not crash the UI
            self.log(f"Could not resolve output folder: {exc}", "error")
            return

        pct = int(
            cfg.get("outpaint_expand_percentage", DEFAULT_OUTPAINT_EXPAND_PERCENT)
            or DEFAULT_OUTPAINT_EXPAND_PERCENT
        )
        prompt = self.config.get("outpaint_prompt", "") or ""
        self._set_busy(True)
        self._abort_event = threading.Event()
        freeimage_key = cfg.get("freeimage_api_key")
        bfl_api_key = cfg.get("bfl_api_key")
        toplevel = self.winfo_toplevel()

        def _safe_after(func):
            try:
                if toplevel.winfo_exists():
                    toplevel.after(0, func)
            except Exception:
                pass

        def _run():
            try:
                from outpaint_generator import OutpaintGenerator
                from outpaint_geometry import (
                    compute_full_res_expand_plan,
                    compute_provider_caps,
                    resolve_border_strategy,
                )
                from PIL import Image as _PILImg, ImageOps as _PILOps

                use_bfl = bool(bfl_api_key)
                with _PILImg.open(image_path) as _im:
                    iw, ih = _PILOps.exif_transpose(_im).size
                plan = compute_full_res_expand_plan(
                    iw, ih, pct,
                    compute_provider_caps("bfl" if use_bfl else "fal"),
                    (3, 4),
                )
                strategy = resolve_border_strategy(
                    cfg, bool(api_key), "bfl" if use_bfl else "fal"
                )
                gen = OutpaintGenerator(
                    api_key, freeimage_key=freeimage_key,
                    bfl_api_key=bfl_api_key,
                )
                gen.set_progress_callback(
                    lambda msg, lvl: _safe_after(
                        lambda m=msg, level=lvl: self.log(m, level)
                    )
                )
                if self._abort_event is not None:
                    gen.set_cancel_event(self._abort_event)
                self.log(
                    f"Expanding to 3:4 ({strategy}) — original kept at native "
                    f"resolution...", "task",
                )
                result = gen.outpaint(
                    image_path=image_path,
                    output_folder=output_folder,
                    composite_mode="preserve_seamless",
                    prompt=prompt,
                    full_res_plan=plan,
                    border_strategy=strategy,
                )
                _safe_after(lambda: self._on_run_done(result))
            except Exception as e:
                err = format_exception_detail(e)
                _safe_after(lambda: self._on_run_error(err))

        threading.Thread(target=_run, daemon=True).start()

    def _set_expand_percent(self, pct: int):
        self._expand_pct_var.set(int(pct))
        self.config["outpaint_expand_percentage"] = int(pct)
        try:
            live_cfg = self.get_config()
            if isinstance(live_cfg, dict):
                live_cfg["outpaint_expand_percentage"] = int(pct)
        except Exception:
            pass
        self._save_config_now()

    def _on_run_done(self, result_path: Optional[str]):
        self._set_busy(False)
        self._abort_event = None  # run finished — don't let a stale Abort fire
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
        try:
            self._add_result_to_carousel(auto=True)
        except Exception as exc:
            self._auto_added_carousel_path = None
            self._add_btn.config(state=tk.NORMAL)
            self.log(f"Auto-add to carousel failed: {exc}", "warning")

    def _on_run_error(self, error: str):
        self._set_busy(False)
        self._abort_event = None  # run finished — don't let a stale Abort fire
        self.log(f"Edit error: {error}", "error")

    def _on_abort(self):
        if self._abort_event is not None:
            self._abort_event.set()
            self.log("Abort requested — finishing current step…", "warning")

    def _set_busy(self, busy: bool):
        self._busy = busy
        self._run_btn.config(state=tk.DISABLED if busy else tk.NORMAL)
        self._abort_btn.config(state=tk.NORMAL if busy else tk.DISABLED)
        if hasattr(self, "_expand_btn"):
            self._expand_btn.config(state=tk.DISABLED if busy else tk.NORMAL)
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
        try:
            self._add_result_to_carousel(auto=False)
        except Exception as exc:
            self._auto_added_carousel_path = None
            self._add_btn.config(state=tk.NORMAL)
            self.log(f"Add to carousel failed: {exc}", "warning")

    def _add_result_to_carousel(self, auto: bool = False):
        if not (self._after_path and os.path.isfile(self._after_path)):
            self.log("Add to carousel skipped: result file is missing.", "warning")
            return
        if self._auto_added_carousel_path == self._after_path:
            if not auto:
                self.log(
                    f"Already added to carousel: {os.path.basename(self._after_path)}",
                    "info",
                )
            self._add_btn.config(state=tk.DISABLED)
            return
        # add_image(make_active=True) makes the result the active carousel
        # image, which fires _on_session_change. Pre-record it as already-synced
        # so the Before pane doesn't jump to the just-added result.
        self._last_synced_carousel_path = self._after_path
        self.image_session.add_image(
            self._after_path,
            "edit",
            label=os.path.basename(self._after_path),
            make_active=True,
            similarity=self._last_similarity,
        )
        self._auto_added_carousel_path = self._after_path
        self._add_btn.config(state=tk.DISABLED)
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
        # "normalize": when True (default), each image is scaled to fill its OWN
        # pane at scale=1.0, so a Before and After of different resolutions show
        # at the SAME on-screen size and compare cleanly. When False, both share
        # one scale = true 1:1 pixel ratio (a higher-res image looks bigger).
        state = {
            "scale": 1.0, "ox": 0.0, "oy": 0.0, "drag": None,
            "normalize": True,
        }
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
        norm_btn = ttk.Button(toolbar, text="", style=TTK_BTN_SECONDARY, width=22)
        norm_btn.pack(side=tk.LEFT, padx=8, pady=4)

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

        def _fit(img, canvas):
            """Scale to fit one image into one canvas at scale=1.0."""
            cw = max(canvas.winfo_width(), 100)
            ch = max(canvas.winfo_height(), 100)
            return min(cw / max(img.width, 1), ch / max(img.height, 1))

        def _base_scales():
            """Return (base_left, base_right).

            normalize=True  -> each image fits its OWN pane (same display size).
            normalize=False -> both share the smaller fit -> true 1:1 pixels.
            """
            bl = _fit(before_img, left_canvas)
            br = _fit(after_img, right_canvas)
            if state["normalize"]:
                return bl, br
            shared = min(bl, br)
            return shared, shared

        def _redraw(recreate=True):
            # Guard against the window being destroyed (Escape / X / a deferred
            # after-call firing post-close, or a <Configure> after destroy):
            # accessing the canvases / top._zoom_photos then raises TclError.
            try:
                if not top.winfo_exists():
                    return
            except tk.TclError:
                return
            # Panning doesn't change scale, so resizing (LANCZOS) on every
            # mouse-motion event would lag badly on large images. Re-resize
            # only when scale/canvas-size/mode actually change (recreate=True);
            # pan just repositions the cached PhotoImage (recreate=False).
            bl, br = _base_scales()
            eff_l = bl * state["scale"]
            eff_r = br * state["scale"]
            cache_key = (round(eff_l, 4), round(eff_r, 4))
            if recreate or state.get("photo_key") != cache_key or not top._zoom_photos:
                top._zoom_photos = []
                for src, eff in ((before_img, eff_l), (after_img, eff_r)):
                    nw = max(int(src.width * eff), 1)
                    nh = max(int(src.height * eff), 1)
                    resized = src.resize((nw, nh), Image.LANCZOS)
                    top._zoom_photos.append(ImageTk.PhotoImage(resized))
                state["photo_key"] = cache_key
            for canvas, photo in (
                (left_canvas, top._zoom_photos[0]),
                (right_canvas, top._zoom_photos[1]),
            ):
                cw = max(canvas.winfo_width(), 100)
                ch = max(canvas.winfo_height(), 100)
                canvas.delete("all")
                canvas.create_image(
                    cw / 2 + state["ox"],
                    ch / 2 + state["oy"],
                    image=photo,
                    anchor="center",
                )

        def _update_norm_btn():
            norm_btn.config(
                text=("Mode: Fit each ✓" if state["normalize"] else "Mode: 1:1 pixels")
            )

        def _toggle_norm():
            state["normalize"] = not state["normalize"]
            state.update({"scale": 1.0, "ox": 0.0, "oy": 0.0})
            _update_norm_btn()
            _redraw()

        norm_btn.config(command=_toggle_norm)
        _update_norm_btn()

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
            _redraw(recreate=False)  # pan only — reuse cached photos

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
        # Schedule the initial draw on `top` (not self) so the after-callback
        # is auto-cancelled when the Toplevel is destroyed.
        top.after(80, _redraw)

    # ── Presets editor ────────────────────────────────────────────────

    def _open_presets_editor(self):
        top = tk.Toplevel(self)
        top.title("Edit AI Studio Presets")
        top.configure(bg=COLORS["bg_panel"])
        top.geometry("640x460")
        top.transient(self.winfo_toplevel())
        top.grab_set()
        # Closing via the OS window button = Cancel (discard, like the Cancel
        # button) — explicit so it never hangs on the grab and behaves
        # consistently with the carousel browser modal. <Escape> too.
        top.protocol("WM_DELETE_WINDOW", top.destroy)
        top.bind("<Escape>", lambda e: top.destroy())

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
        updates = {
            "ai_studio_model_endpoint": self._selected_model().get(
                "endpoint", "fal-ai/nano-banana-2/edit"
            ),
            "ai_studio_presets": [dict(p) for p in self._presets],
        }
        # Guard the Text read: this can run during window teardown (config is
        # saved on close), after the widget has been destroyed -> TclError.
        try:
            if self._prompt_text.winfo_exists():
                updates["ai_studio_last_custom_prompt"] = self._prompt_text.get(
                    "1.0", tk.END
                ).strip()
        except Exception:
            pass
        return updates

    def _save_config_now(self):
        try:
            self.config.update(self.get_config_updates())
            if self._config_saver:
                self._config_saver()
        except Exception as exc:
            self.log(f"Could not save AI Studio settings: {exc}", "warning")

    def destroy(self):
        """Unregister the session listener before teardown so a later
        _notify() (e.g. from remove_videos/add_image in another tab) doesn't
        invoke this destroyed tab's callback (code-review). Defensive: ignore
        if the session lacks remove_on_change or the listener is already gone.
        """
        try:
            self.image_session.remove_on_change(self._on_session_change)
        except Exception:
            pass
        super().destroy()
