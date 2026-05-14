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

IMAGE_FILETYPES = [("Image Files", "*.png *.jpg *.jpeg *.bmp *.webp")]
SUPPORTED_IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".bmp", ".webp"}
SIMILARITY_PREVIEW_MAX_SIZE = (250, 250)
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

        self.title("Face Similarity Pro")
        self.geometry("900x680")
        self.minsize(780, 620)
        ctk.set_appearance_mode("dark")
        ctk.set_default_color_theme("blue")

        self.grid_rowconfigure(1, weight=1)
        self.grid_columnconfigure(0, weight=1)

        self.header_label = ctk.CTkLabel(
            self,
            text="Enterprise Face Similarity Analysis",
            font=ctk.CTkFont(size=24, weight="bold"),
        )
        self.header_label.grid(row=0, column=0, pady=(20, 10))

        self.tabview = ctk.CTkTabview(self)
        self.tabview.grid(row=1, column=0, sticky="nsew", padx=20, pady=10)
        self.similarity_tab = self.tabview.add("Similarity")
        self.extraction_tab = self.tabview.add("Extraction")

        self._build_similarity_tab()
        self._build_extraction_tab()

        self.set_ui_state("disabled")
        self.sim_result_label.configure(text="Initializing ML Models... Please wait.", text_color="yellow")
        self.ext_result_label.configure(text="Initializing ML Models... Please wait.", text_color="yellow")
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
        self.similarity_tab.grid_columnconfigure(0, weight=1)
        self.similarity_tab.grid_columnconfigure(1, weight=1)
        self.similarity_tab.grid_rowconfigure(1, weight=1)

        self.zone1_frame = ctk.CTkFrame(self.similarity_tab)
        self.zone1_frame.grid(row=0, column=0, rowspan=2, sticky="nsew", padx=(0, 8), pady=10)
        self.zone1_frame.grid_rowconfigure(1, weight=1)
        self.zone1_frame.grid_columnconfigure(0, weight=1)

        self.zone1_label = ctk.CTkLabel(self.zone1_frame, text="Upload Image 1", font=ctk.CTkFont(size=16))
        self.zone1_label.grid(row=0, column=0, pady=10)
        self.zone1_dropzone = ctk.CTkFrame(
            self.zone1_frame, fg_color="transparent", border_width=2, border_color="#1f6aa5"
        )
        self.zone1_dropzone.grid(row=1, column=0, pady=10, padx=8, sticky="nsew")
        self.zone1_dropzone.grid_rowconfigure(1, weight=1)
        self.zone1_dropzone.grid_columnconfigure(0, weight=1)
        self.zone1_drop_hint = ctk.CTkLabel(self.zone1_dropzone, text="Drag and drop image here")
        self.zone1_drop_hint.grid(row=0, column=0, pady=(10, 6), padx=12)
        self.img1_display = ctk.CTkLabel(self.zone1_dropzone, text="No Image Selected")
        self.img1_display.grid(row=1, column=0, pady=(4, 10), padx=12)
        self.btn_upload1 = ctk.CTkButton(self.zone1_frame, text="Select File...", command=lambda: self.upload_image(1))
        self.btn_upload1.grid(row=2, column=0, pady=20)

        self.zone2_frame = ctk.CTkFrame(self.similarity_tab)
        self.zone2_frame.grid(row=0, column=1, rowspan=2, sticky="nsew", padx=(8, 0), pady=10)
        self.zone2_frame.grid_rowconfigure(1, weight=1)
        self.zone2_frame.grid_columnconfigure(0, weight=1)

        self.zone2_label = ctk.CTkLabel(self.zone2_frame, text="Upload Image 2", font=ctk.CTkFont(size=16))
        self.zone2_label.grid(row=0, column=0, pady=10)
        self.zone2_dropzone = ctk.CTkFrame(
            self.zone2_frame, fg_color="transparent", border_width=2, border_color="#1f6aa5"
        )
        self.zone2_dropzone.grid(row=1, column=0, pady=10, padx=8, sticky="nsew")
        self.zone2_dropzone.grid_rowconfigure(1, weight=1)
        self.zone2_dropzone.grid_columnconfigure(0, weight=1)
        self.zone2_drop_hint = ctk.CTkLabel(self.zone2_dropzone, text="Drag and drop image here")
        self.zone2_drop_hint.grid(row=0, column=0, pady=(10, 6), padx=12)
        self.img2_display = ctk.CTkLabel(self.zone2_dropzone, text="No Image Selected")
        self.img2_display.grid(row=1, column=0, pady=(4, 10), padx=12)
        self.btn_upload2 = ctk.CTkButton(self.zone2_frame, text="Select File...", command=lambda: self.upload_image(2))
        self.btn_upload2.grid(row=2, column=0, pady=20)

        self._bind_drop_target(self.zone1_dropzone, self._on_drop_similarity_image1)
        self._bind_drop_target(self.zone1_drop_hint, self._on_drop_similarity_image1)
        self._bind_drop_target(self.img1_display, self._on_drop_similarity_image1)
        self._bind_drop_target(self.zone2_dropzone, self._on_drop_similarity_image2)
        self._bind_drop_target(self.zone2_drop_hint, self._on_drop_similarity_image2)
        self._bind_drop_target(self.img2_display, self._on_drop_similarity_image2)

        self.sim_controls = ctk.CTkFrame(self.similarity_tab, fg_color="transparent")
        self.sim_controls.grid(row=2, column=0, columnspan=2, sticky="ew", pady=(6, 16))
        self.sim_controls.grid_columnconfigure(0, weight=1)

        # Anti-spoof toggle. Default ON to match KYC-grade defaults.
        # Toggling re-runs the comparison if both images are loaded.
        self.anti_spoof_var = tk.BooleanVar(value=getattr(self.engine, "anti_spoofing", True))
        self.anti_spoof_checkbox = ctk.CTkCheckBox(
            self.sim_controls,
            text="Anti-spoof (face liveness)",
            variable=self.anti_spoof_var,
            onvalue=True,
            offvalue=False,
            command=self._on_anti_spoof_toggle,
        )
        self.anti_spoof_checkbox.grid(row=0, column=0, pady=(4, 6))

        self.btn_run = ctk.CTkButton(
            self.sim_controls,
            text="Run Similarity Comparison",
            font=ctk.CTkFont(size=16, weight="bold"),
            command=self.start_comparison,
            height=40,
        )
        self.btn_run.grid(row=1, column=0, pady=8)

        self.sim_progressbar = ctk.CTkProgressBar(self.sim_controls, mode="indeterminate")
        self.sim_progressbar.grid(row=2, column=0, pady=(0, 10), sticky="ew")
        self.sim_progressbar.set(0)
        self.sim_progressbar.grid_remove()

        self.sim_result_label = ctk.CTkLabel(
            self.sim_controls,
            text="",
            font=ctk.CTkFont(size=18),
            wraplength=760,
        )
        self.sim_result_label.grid(row=3, column=0)

        self.fas_result_label = ctk.CTkLabel(
            self.sim_controls,
            text="",
            font=ctk.CTkFont(size=14, weight="bold"),
            wraplength=760,
        )
        self.fas_result_label.grid(row=4, column=0, pady=(4, 0))

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
        self.sim_progressbar.stop()
        self.ext_progressbar.stop()
        self.sim_progressbar.grid_remove()
        self.ext_progressbar.grid_remove()
        self.sim_result_label.configure(text="", text_color="white")
        self.ext_result_label.configure(text="", text_color="white")
        self.set_ui_state("normal")

    def _on_init_error(self, error_msg: str):
        self.sim_progressbar.stop()
        self.ext_progressbar.stop()
        self.sim_progressbar.grid_remove()
        self.ext_progressbar.grid_remove()
        self.sim_result_label.configure(text=f"Initialization Error: {error_msg}", text_color="red")
        self.ext_result_label.configure(text=f"Initialization Error: {error_msg}", text_color="red")

    def set_ui_state(self, state: str):
        self.btn_upload1.configure(state=state)
        self.btn_upload2.configure(state=state)
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
            self.img1_display.configure(image=None, text="No Image Selected")
            self.img1_display.image = None
            return
        self.img2_path = None
        self.img2_display.configure(image=None, text="No Image Selected")
        self.img2_display.image = None

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
            self.sim_result_label.configure(text=f"Error loading image: file not found ({file_path})", text_color="red")
            return
        if not self._is_supported_image_file(file_path):
            self._clear_similarity_image_zone(zone)
            self.sim_result_label.configure(
                text="Error loading image: unsupported file type. Use PNG, JPG, JPEG, BMP, or WEBP.",
                text_color="red",
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
            self.sim_result_label.configure(text="", text_color="white")
        except Exception as e:
            self._clear_similarity_image_zone(zone)
            self.sim_result_label.configure(text=f"Error loading image: {e}", text_color="red")

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
            self.sim_result_label.configure(text="Please wait for the current task to finish.", text_color="yellow")
            return
        for file_path in self._extract_drop_paths(data):
            self._load_similarity_image(file_path, zone)
            return
        self.sim_result_label.configure(text="Error loading image: no files were dropped.", text_color="red")

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

    def start_comparison(self):
        if not self.img1_path or not self.img2_path:
            self.sim_result_label.configure(
                text="Please upload both images before running comparison.",
                text_color="yellow",
            )
            return

        self.set_ui_state("disabled")
        self.sim_result_label.configure(text="Processing... Detecting and comparing faces...", text_color="cyan")
        self.sim_progressbar.grid()
        self.sim_progressbar.start()

        threading.Thread(
            target=self._compare_thread,
            args=(self.img1_path, self.img2_path),
            daemon=True,
        ).start()

    def _compare_thread(self, path1: str, path2: str):
        try:
            result = self.engine.compare_images(path1, path2)
        except Exception as e:
            logging.exception("Comparison thread failed")
            result = {"match": False, "score": 0.0, "error": str(e)}
        self.after(0, self._on_comparison_complete, result)

    def _on_comparison_complete(self, result: dict):
        self.sim_progressbar.stop()
        self.sim_progressbar.grid_remove()
        self.set_ui_state("normal")

        if result.get("error"):
            self.sim_result_label.configure(text=f"Error: {result['error']}", text_color="red")
            self.fas_result_label.configure(text="", text_color="#888888")
            return

        # Engine returns score as a float (e.g. 97.118112...). Round to 1 decimal
        # for display — anything beyond is noise and visually ugly.
        try:
            score_display = f"{float(result['score']):.1f}"
        except (TypeError, ValueError):
            score_display = str(result.get("score", "?"))
        is_match = result["match"]
        if is_match:
            status_text = "are"
            color = "#00FF00"
        else:
            status_text = "are not"
            color = "#FF4444"

        output_msg = (
            f"Face similarity ratio: {score_display}%, "
            f"The two photos {status_text} the same person."
        )
        self.sim_result_label.configure(text=output_msg, text_color=color)
        self._render_fas(result)

    def _render_fas(self, result: dict):
        """Render the LIVENESS check from a comparison result diagnostics block.

        FAS is ADVISORY only — it does not gate the similarity verdict. A spoof
        flag means "this image looks possibly synthetic to the FAS classifier";
        false positives are common (passport photos, low-light selfies, printed
        IDs). Render as an info-level note in amber rather than a hard red FAIL
        so it doesn't visually contradict a passing similarity match.
        """
        diag = result.get("diagnostics") if isinstance(result.get("diagnostics"), dict) else {}
        fas = diag.get("anti_spoofing") if isinstance(diag, dict) else None
        if not isinstance(fas, dict):
            self.fas_result_label.configure(text="", text_color="#888888")
            return
        ref = fas.get("ref") if isinstance(fas.get("ref"), dict) else None
        tgt = fas.get("target") if isinstance(fas.get("target"), dict) else None
        if ref is None and tgt is None:
            self.fas_result_label.configure(text="", text_color="#888888")
            return
        ref_spoof = (ref or {}).get("spoof_detected")
        tgt_spoof = (tgt or {}).get("spoof_detected")
        if ref_spoof or tgt_spoof:
            # Amber, not red. Wording emphasizes "possible" + "advisory".
            flagged = []
            if ref_spoof:
                flagged.append("ref")
            if tgt_spoof:
                flagged.append("target")
            self.fas_result_label.configure(
                text=f"Liveness (anti-spoof): possible synthetic input on {' & '.join(flagged)} (advisory only)",
                text_color="#FFC107",
            )
        else:
            self.fas_result_label.configure(
                text="Liveness (anti-spoof): both images look real",
                text_color="#00FF00",
            )

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
