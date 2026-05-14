from __future__ import annotations

import importlib
import os
import sys
import types
import unittest
from typing import ClassVar
from unittest.mock import patch


class _DeepFaceStub:
    @staticmethod
    def build_model(model_name: str):
        return model_name

    @staticmethod
    def extract_faces(**kwargs):
        return [{"face": "face", "facial_area": {"w": 1, "h": 1}}]

    @staticmethod
    def represent(**kwargs):
        return [{"embedding": [1.0, 0.0, 0.0]}]


class _WidgetStub:
    def __init__(self, *args, **kwargs):
        self.kwargs = kwargs
        self.state = kwargs.get("state")
        self.text = kwargs.get("text", "")
        self.text_color = kwargs.get("text_color")
        self.image = kwargs.get("image")
        self.grid_hidden = False
        self.value = None
        self.dnd_targets = ()
        self.dnd_handlers = {}

    def grid(self, *args, **kwargs):
        self.grid_hidden = False

    def grid_remove(self):
        self.grid_hidden = True

    def grid_rowconfigure(self, *args, **kwargs):
        return None

    def grid_columnconfigure(self, *args, **kwargs):
        return None

    def configure(self, **kwargs):
        self.kwargs.update(kwargs)
        if "state" in kwargs:
            self.state = kwargs["state"]
        if "text" in kwargs:
            self.text = kwargs["text"]
        if "text_color" in kwargs:
            self.text_color = kwargs["text_color"]
        if "image" in kwargs:
            self.image = kwargs["image"]

    def cget(self, key):
        if key == "state":
            return self.state
        return self.kwargs.get(key)

    def set(self, value):
        self.value = value

    def start(self):
        return None

    def stop(self):
        return None

    def drop_target_register(self, *targets):
        self.dnd_targets = targets

    def dnd_bind(self, event_name, handler):
        self.dnd_handlers[event_name] = handler


class _CTkStub(_WidgetStub):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.tk = types.SimpleNamespace(splitlist=self._splitlist)

    @staticmethod
    def _splitlist(value: str):
        if value.startswith("{") and value.endswith("}"):
            return (value[1:-1],)
        return tuple(value.split())

    def title(self, *_args, **_kwargs):
        return None

    def geometry(self, *_args, **_kwargs):
        return None

    def minsize(self, *_args, **_kwargs):
        return None

    def after(self, _delay, callback, *args):
        callback(*args)

    def mainloop(self):
        return None


class _CTkImageStub:
    def __init__(self, *args, **kwargs):
        self.kwargs = kwargs


class _CTkTabViewStub(_WidgetStub):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.tabs = {}

    def add(self, name: str):
        tab = _WidgetStub()
        self.tabs[name] = tab
        return tab


class _CTkModuleStub(types.ModuleType):
    def __init__(self):
        super().__init__("customtkinter")
        self.CTk = _CTkStub
        self.CTkTabview = _CTkTabViewStub
        self.CTkFrame = _WidgetStub
        self.CTkLabel = _WidgetStub
        self.CTkButton = _WidgetStub
        self.CTkProgressBar = _WidgetStub
        self.CTkCheckBox = _WidgetStub
        self.CTkImage = _CTkImageStub
        self.CTkFont = lambda *args, **kwargs: {"args": args, "kwargs": kwargs}
        self.set_appearance_mode = lambda *args, **kwargs: None
        self.set_default_color_theme = lambda *args, **kwargs: None


class _TkinterDnDModuleStub(types.ModuleType):
    class _DnDWrapper:
        pass

    def __init__(self):
        super().__init__("tkinterdnd2")
        self.DND_FILES = "DND_Files"
        self.TkinterDnD = types.SimpleNamespace(
            DnDWrapper=self._DnDWrapper,
            _require=lambda _root: "stub",
        )


class _EngineStub:
    def initialize_models(self):
        return None

    def compare_images(self, _path1: str, _path2: str):
        return {"match": True, "score": 92.5, "error": None}

    def extract_face(self, _src: str, _out: str, padding: float = 0.175):
        return 0.81

    @staticmethod
    def summarize_fas_pair(_diag):
        # Mirror the canonical helper's "no diag" return so render code paths
        # don't crash. Real verdict logic is unit-tested in test_similarity_engine.
        return {
            "verdict": "unavailable",
            "color_hint": "muted",
            "message": "",
            "ref_status": "missing",
            "target_status": "missing",
        }


class _ThreadCaptureBase:
    instances: ClassVar[list["_ThreadCaptureBase"]] = []


class _ImageOpenStub:
    def __init__(self, size=(1000, 1000)):
        self.size = size

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def copy(self):
        return types.SimpleNamespace(size=self.size)


class TestModernGUI(unittest.TestCase):
    def setUp(self) -> None:
        self.thread_instances: list[_ThreadCaptureBase] = []
        self._original_gui_module = sys.modules.pop("src.gui", None)
        self.addCleanup(self._restore_gui_module)

        deepface_module = types.ModuleType("deepface")
        deepface_module.DeepFace = _DeepFaceStub
        engine_module = types.ModuleType("src.engine")
        engine_module.FaceEngine = _EngineStub
        tkinter_module = types.ModuleType("tkinter")

        class _TclError(Exception):
            pass

        filedialog_module = types.ModuleType("tkinter.filedialog")
        filedialog_module.askopenfilename = lambda *args, **kwargs: ""
        tkinter_module.TclError = _TclError
        tkinter_module.filedialog = filedialog_module

        class _BooleanVarStub:
            """Minimal tk.BooleanVar replacement for headless GUI tests."""
            def __init__(self, value=False):
                self._value = bool(value)
            def get(self):
                return self._value
            def set(self, value):
                self._value = bool(value)

        tkinter_module.BooleanVar = _BooleanVarStub

        parent = self

        class _ThreadCapture(_ThreadCaptureBase):
            def __init__(self, target=None, args=(), daemon=None, **kwargs):
                self.target = target
                self.args = args
                self.daemon = daemon
                self.started = False
                parent.thread_instances.append(self)

            def start(self):
                self.started = True

        self.thread_capture_class = _ThreadCapture

        patcher = patch.dict(
            sys.modules,
            {
                "customtkinter": _CTkModuleStub(),
                "deepface": deepface_module,
                "src.engine": engine_module,
                "tkinter": tkinter_module,
                "tkinter.filedialog": filedialog_module,
                "tkinterdnd2": _TkinterDnDModuleStub(),
            },
        )
        patcher.start()
        self.addCleanup(patcher.stop)

        self.gui_module = importlib.import_module("src.gui")
        self.gui_module = importlib.reload(self.gui_module)

        self.thread_patcher = patch.object(self.gui_module.threading, "Thread", self.thread_capture_class)
        self.thread_patcher.start()
        self.addCleanup(self.thread_patcher.stop)

        self.engine_patcher = patch.object(self.gui_module, "FaceEngine", _EngineStub)
        self.engine_patcher.start()
        self.addCleanup(self.engine_patcher.stop)

    def _restore_gui_module(self) -> None:
        sys.modules.pop("src.gui", None)
        if self._original_gui_module is not None:
            sys.modules["src.gui"] = self._original_gui_module

    def test_init_starts_model_warmup_on_daemon_thread(self) -> None:
        app = self.gui_module.ModernGUI()
        self.assertEqual(len(self.thread_instances), 1)
        thread = self.thread_instances[0]
        self.assertEqual(thread.target.__name__, "_init_models_thread")
        self.assertTrue(thread.daemon)
        self.assertTrue(thread.started)
        self.assertEqual(app.btn_run.state, "disabled")
        self.assertEqual(app.btn_run.kwargs.get("text"), "Run Similarity Comparison")

    def test_on_models_ready_re_enables_controls(self) -> None:
        app = self.gui_module.ModernGUI()
        app._on_models_ready()
        self.assertEqual(app.btn_upload1.state, "normal")
        self.assertEqual(app.btn_upload2.state, "normal")
        self.assertEqual(app.btn_run.state, "normal")
        self.assertEqual(app.btn_upload_extract.state, "normal")
        self.assertEqual(app.btn_run_extract.state, "normal")
        self.assertEqual(app.sim_result_label.text, "")
        self.assertEqual(app.ext_result_label.text, "")
        self.assertTrue(app.sim_progressbar.grid_hidden)
        self.assertTrue(app.ext_progressbar.grid_hidden)
        self.assertIn("<<Drop>>", app.zone1_dropzone.dnd_handlers)
        self.assertIn("<<Drop>>", app.zone2_dropzone.dnd_handlers)
        self.assertIn("<<Drop>>", app.ext_dropzone.dnd_handlers)

    def test_drop_similarity_image_updates_zone_state(self) -> None:
        app = self.gui_module.ModernGUI()
        app._on_models_ready()
        event = types.SimpleNamespace(data="{C:/tmp/photo one.jpg}")
        with patch("src.gui.os.path.isfile", return_value=True), patch.object(
            self.gui_module.Image, "open", return_value=_ImageOpenStub()
        ):
            app._on_drop_similarity_image1(event)
        self.assertEqual(app.img1_path, os.path.normpath("C:/tmp/photo one.jpg"))
        self.assertEqual(app.img1_display.text, "")
        self.assertIsNotNone(app.img1_display.image)
        self.assertEqual(app.img1_display.image.kwargs["size"], (250, 250))

    def test_drop_extraction_image_updates_source_and_output(self) -> None:
        app = self.gui_module.ModernGUI()
        app._on_models_ready()
        event = types.SimpleNamespace(data="C:/tmp/front.png")
        with patch("src.gui.os.path.isfile", return_value=True), patch(
            "src.gui.os.path.exists", return_value=False
        ), patch.object(self.gui_module.Image, "open", return_value=_ImageOpenStub()):
            app._on_drop_extraction_source(event)
        self.assertEqual(app.extraction_src_path, os.path.normpath("C:/tmp/front.png"))
        self.assertIn("Output: extracted.png", app.ext_output_label.text)

    def test_drop_similarity_rejects_unsupported_file_types(self) -> None:
        app = self.gui_module.ModernGUI()
        app._on_models_ready()
        event = types.SimpleNamespace(data="C:/tmp/not-image.txt")
        with patch("src.gui.os.path.isfile", return_value=True):
            app._on_drop_similarity_image2(event)
        self.assertIn("unsupported file type", app.sim_result_label.text.lower())

    def test_upload_image_button_still_sets_selected_path(self) -> None:
        app = self.gui_module.ModernGUI()
        app._on_models_ready()
        with patch("src.gui.filedialog.askopenfilename", return_value="C:/tmp/picked.webp"), patch(
            "src.gui.os.path.isfile", return_value=True
        ), patch.object(self.gui_module.Image, "open", return_value=_ImageOpenStub()):
            app.upload_image(2)
        self.assertEqual(app.img2_path, "C:/tmp/picked.webp")
        self.assertIsNotNone(app.img2_display.image)
        self.assertEqual(app.img2_display.image.kwargs["size"], (250, 250))

    def test_upload_image_button_preserves_aspect_ratio(self) -> None:
        app = self.gui_module.ModernGUI()
        app._on_models_ready()
        with patch("src.gui.filedialog.askopenfilename", return_value="C:/tmp/wide.jpg"), patch(
            "src.gui.os.path.isfile", return_value=True
        ), patch.object(self.gui_module.Image, "open", return_value=_ImageOpenStub(size=(1200, 600))):
            app.upload_image(1)
        self.assertEqual(app.img1_display.image.kwargs["size"], (250, 125))

    def test_drop_extraction_image_preserves_aspect_ratio(self) -> None:
        app = self.gui_module.ModernGUI()
        app._on_models_ready()
        event = types.SimpleNamespace(data="C:/tmp/tall.png")
        with patch("src.gui.os.path.isfile", return_value=True), patch(
            "src.gui.os.path.exists", return_value=False
        ), patch.object(self.gui_module.Image, "open", return_value=_ImageOpenStub(size=(600, 1200))):
            app._on_drop_extraction_source(event)
        self.assertEqual(app.ext_display.image.kwargs["size"], (150, 300))

    def test_similarity_error_clears_stale_selected_image(self) -> None:
        app = self.gui_module.ModernGUI()
        app._on_models_ready()
        app.img1_path = "C:/tmp/old.png"
        app.img1_display.configure(text="", image=object())
        app.img1_display.image = object()
        with patch("src.gui.os.path.isfile", return_value=False):
            app._load_similarity_image("C:/tmp/missing.png", 1)
        self.assertIsNone(app.img1_path)
        self.assertEqual(app.img1_display.text, "No Image Selected")
        self.assertIsNone(app.img1_display.image)

    def test_extraction_error_clears_stale_selected_image(self) -> None:
        app = self.gui_module.ModernGUI()
        app._on_models_ready()
        app.extraction_src_path = "C:/tmp/old.png"
        app.extraction_out_path = "C:/tmp/extracted.png"
        app.ext_display.configure(text="", image=object())
        app.ext_display.image = object()
        with patch("src.gui.os.path.isfile", return_value=False):
            app._load_extraction_source_image("C:/tmp/missing.png")
        self.assertIsNone(app.extraction_src_path)
        self.assertIsNone(app.extraction_out_path)
        self.assertEqual(app.ext_display.text, "No Source Image Selected")
        self.assertIsNone(app.ext_display.image)

    def test_drop_is_blocked_while_ui_disabled(self) -> None:
        app = self.gui_module.ModernGUI()
        event = types.SimpleNamespace(data="C:/tmp/new.png")
        app.img1_path = "C:/tmp/current.png"
        with patch("src.gui.os.path.isfile", return_value=True), patch.object(
            self.gui_module.Image, "open", return_value=_ImageOpenStub()
        ):
            app._on_drop_similarity_image1(event)
        self.assertEqual(app.img1_path, "C:/tmp/current.png")
        self.assertIn("wait for the current task", app.sim_result_label.text.lower())

    def test_drop_works_after_ui_enabled(self) -> None:
        app = self.gui_module.ModernGUI()
        app._on_models_ready()
        event = types.SimpleNamespace(data="C:/tmp/new.png")
        with patch("src.gui.os.path.isfile", return_value=True), patch.object(
            self.gui_module.Image, "open", return_value=_ImageOpenStub()
        ):
            app._on_drop_similarity_image1(event)
        self.assertEqual(app.img1_path, os.path.normpath("C:/tmp/new.png"))

    def test_start_comparison_spawns_daemon_worker_and_updates_status(self) -> None:
        app = self.gui_module.ModernGUI()
        app.img1_path = "img1.png"
        app.img2_path = "img2.png"
        self.thread_instances = []

        app.start_comparison()

        self.assertEqual(len(self.thread_instances), 1)
        thread = self.thread_instances[0]
        self.assertEqual(thread.target.__name__, "_compare_thread")
        self.assertEqual(thread.args, ("img1.png", "img2.png"))
        self.assertTrue(thread.daemon)
        self.assertEqual(app.btn_run.state, "disabled")
        self.assertIn("Processing...", app.sim_result_label.text)

    def test_on_comparison_complete_renders_expected_success_text(self) -> None:
        app = self.gui_module.ModernGUI()
        app._on_comparison_complete({"match": True, "score": 98.7, "error": None})
        self.assertIn("Face similarity ratio: 98.7%", app.sim_result_label.text)
        self.assertIn("are the same person", app.sim_result_label.text)
        self.assertEqual(app.sim_result_label.text_color, "#00FF00")

    def test_on_comparison_complete_renders_error(self) -> None:
        app = self.gui_module.ModernGUI()
        app._on_comparison_complete({"match": False, "score": 0, "error": "bad input"})
        self.assertEqual(app.sim_result_label.text, "Error: bad input")
        self.assertEqual(app.sim_result_label.text_color, "red")

    def test_compare_thread_converts_engine_exception_to_error_result(self) -> None:
        app = self.gui_module.ModernGUI()
        with patch.object(app.engine, "compare_images", side_effect=ValueError("compare failed")):
            app._compare_thread("img1.png", "img2.png")
        self.assertEqual(app.sim_result_label.text, "Error: compare failed")
        self.assertEqual(app.sim_result_label.text_color, "red")

    def test_start_extraction_spawns_daemon_worker_and_updates_status(self) -> None:
        app = self.gui_module.ModernGUI()
        app.extraction_src_path = "src.png"
        app.extraction_out_path = "extracted.png"
        self.thread_instances = []

        with patch("src.gui.os.path.exists", return_value=False):
            app.start_extraction()

        self.assertEqual(len(self.thread_instances), 1)
        thread = self.thread_instances[0]
        self.assertEqual(thread.target.__name__, "_extract_thread")
        self.assertEqual(thread.args, ("src.png", "extracted.png"))
        self.assertTrue(thread.daemon)
        self.assertEqual(app.btn_run_extract.state, "disabled")
        self.assertIn("Processing...", app.ext_result_label.text)

    def test_start_extraction_respects_skip_mode_and_existing_target(self) -> None:
        app = self.gui_module.ModernGUI()
        app.extraction_src_path = "C:/tmp/front.png"
        app.config["existing_file_mode"] = "skip"
        with patch("src.gui.os.path.exists", return_value=True):
            app.start_extraction()
        self.assertIn("Extraction skipped", app.ext_result_label.text)

    def test_extract_thread_uses_configured_padding_ratio(self) -> None:
        app = self.gui_module.ModernGUI()
        app.config["padding_ratio"] = 0.33
        with patch.object(app.engine, "extract_face", return_value=0.77) as extract_face:
            app._extract_thread("src.png", "out.png")
        extract_face.assert_called_once_with("src.png", "out.png", padding=0.33)

    def test_on_extraction_complete_renders_success_text(self) -> None:
        app = self.gui_module.ModernGUI()
        app._on_extraction_complete({"ok": True, "confidence": 0.91, "output": "C:/tmp/extracted.png"})
        self.assertIn("Extraction complete: extracted.png", app.ext_result_label.text)
        self.assertIn("Confidence: 91.0%", app.ext_result_label.text)
        self.assertEqual(app.ext_result_label.text_color, "#00FF00")

    def test_on_extraction_complete_renders_error(self) -> None:
        app = self.gui_module.ModernGUI()
        app._on_extraction_complete({"ok": False, "error": "face missing"})
        self.assertEqual(app.ext_result_label.text, "Error: face missing")
        self.assertEqual(app.ext_result_label.text_color, "red")

    def test_on_init_error_hides_both_progress_bars(self) -> None:
        app = self.gui_module.ModernGUI()
        app._on_init_error("init failed")
        self.assertTrue(app.sim_progressbar.grid_hidden)
        self.assertTrue(app.ext_progressbar.grid_hidden)
        self.assertIn("Initialization Error", app.sim_result_label.text)
        self.assertIn("Initialization Error", app.ext_result_label.text)


if __name__ == "__main__":
    unittest.main()
