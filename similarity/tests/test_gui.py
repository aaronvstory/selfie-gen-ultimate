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

    def get(self):
        # Stubbed for CTkProgressBar.get() — used by the determinate-mode tick.
        return self.value if isinstance(self.value, (int, float)) else 0.0

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
        # In real Tk, after() schedules; in tests we executed synchronously.
        # That broke when the standalone GUI's progress tick reschedules itself
        # — synchronous execution = infinite recursion. Queue callbacks instead;
        # tests that need to drive the loop can drain via _drain_after_calls().
        self._after_queue = getattr(self, "_after_queue", [])
        self._after_queue.append((callback, args))

    def _drain_after_calls(self, max_iterations: int = 5):
        """Run pending after() callbacks up to max_iterations times.

        Simulates Tk's event loop just enough for tests to observe one round
        of scheduled work without falling into reschedule recursion.
        """
        for _ in range(max_iterations):
            queue = getattr(self, "_after_queue", [])
            if not queue:
                return
            self._after_queue = []
            for cb, args in queue:
                try:
                    cb(*args)
                except Exception:
                    pass

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
        self.assertEqual(app.btn_run.kwargs.get("text"), "RUN COMPARISON")

    def test_on_models_ready_re_enables_controls(self) -> None:
        app = self.gui_module.ModernGUI()
        app._on_models_ready()
        self.assertEqual(app.btn_upload1.state, "normal")
        self.assertEqual(app.btn_upload2.state, "normal")
        self.assertEqual(app.btn_run.state, "normal")
        self.assertEqual(app.btn_upload_extract.state, "normal")
        self.assertEqual(app.btn_run_extract.state, "normal")
        # v4: sim_result_label is now a small status line (always has a
        # placeholder " " when idle so the row reserves height) — never blank.
        self.assertEqual(app.sim_result_label.text, " ")
        self.assertEqual(app.ext_result_label.text, "")
        # v4: hero card resets to idle state on models-ready.
        self.assertEqual(app.hero_headline.text, "R E A D Y")
        self.assertEqual(app.hero_score.text, "—")
        # sim_progressbar stays gridded post-init (layout-stability fix in v1.8
        # follow-up). It just resets to value=0 and the status label clears.
        self.assertFalse(app.sim_progressbar.grid_hidden)
        self.assertEqual(app.sim_progressbar.value, 0)
        # Extraction bar still hides until used.
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
        self.assertEqual(app.img1_display.image.kwargs["size"], (220, 220))

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
        self.assertEqual(app.img2_display.image.kwargs["size"], (220, 220))

    def test_upload_image_button_preserves_aspect_ratio(self) -> None:
        app = self.gui_module.ModernGUI()
        app._on_models_ready()
        with patch("src.gui.filedialog.askopenfilename", return_value="C:/tmp/wide.jpg"), patch(
            "src.gui.os.path.isfile", return_value=True
        ), patch.object(self.gui_module.Image, "open", return_value=_ImageOpenStub(size=(1200, 600))):
            app.upload_image(1)
        # v4: SIMILARITY_PREVIEW_MAX_SIZE shrunk 250→220 to free width for
        # the new center hero verdict column. 1200x600 → 220x110 (2:1 ratio).
        self.assertEqual(app.img1_display.image.kwargs["size"], (220, 110))

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
        # v1.8 follow-up: result label is cleared on start (was "Processing…");
        # progress feedback now lives in the dedicated sim_status_label which
        # the phase-milestone timer drives — assert it shows the starting beat.
        self.assertIn("0%", app.sim_status_label.text)

    def test_on_comparison_complete_renders_expected_success_text(self) -> None:
        # v4: similarity verdict + score live in the hero card, not in
        # sim_result_label (which is now a small status line that stays empty
        # for the success path).
        app = self.gui_module.ModernGUI()
        app._on_comparison_complete({"match": True, "score": 98.7, "error": None})
        # Hero card should show MATCH + green
        self.assertEqual(app.hero_headline.text, "M A T C H")
        self.assertEqual(app.hero_score.text, "98.7%")
        self.assertEqual(app.hero_icon.text, "✓")
        # Status line stays clean — hero is the source of truth.
        self.assertEqual(app.sim_result_label.text, " ")

    def test_on_comparison_complete_renders_no_match(self) -> None:
        # v4: complement to the match test — verifies the no-match branch
        # renders correctly in the hero card.
        app = self.gui_module.ModernGUI()
        app._on_comparison_complete({"match": False, "score": 32.4, "error": None})
        self.assertEqual(app.hero_headline.text, "N O   M A T C H")
        self.assertEqual(app.hero_score.text, "32.4%")
        self.assertEqual(app.hero_icon.text, "✖")

    def test_on_comparison_complete_renders_error(self) -> None:
        # v4: error state goes to the hero card, not sim_result_label.
        app = self.gui_module.ModernGUI()
        app._on_comparison_complete({"match": False, "score": 0, "error": "bad input"})
        self.assertEqual(app.hero_headline.text, "E R R O R")
        self.assertEqual(app.hero_icon.text, "!")
        self.assertIn("bad input", app.hero_score.text)
        self.assertEqual(app.sim_result_label.text, " ")

    def test_compare_thread_converts_engine_exception_to_error_result(self) -> None:
        app = self.gui_module.ModernGUI()
        with patch.object(app.engine, "compare_images", side_effect=ValueError("compare failed")):
            app._compare_thread("img1.png", "img2.png")
        # _compare_thread calls self.after(0, _on_comparison_complete, result) —
        # the queueing after() stub now collects callbacks; drain to run them.
        app._drain_after_calls()
        # v4: error surfaces on the hero card.
        self.assertEqual(app.hero_headline.text, "E R R O R")
        self.assertIn("compare failed", app.hero_score.text)

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
        # Layout-stability fix: sim_progressbar stays gridded; bar resets to 0
        # and status text clears. Extraction bar still hides until used.
        self.assertFalse(app.sim_progressbar.grid_hidden)
        self.assertEqual(app.sim_progressbar.value, 0)
        self.assertTrue(app.ext_progressbar.grid_hidden)
        # v4: init-error story moved off sim_result_label onto the hero card.
        self.assertEqual(app.hero_headline.text, "E R R O R")
        self.assertIn("init failed", app.hero_score.text)
        self.assertEqual(app.sim_result_label.text, " ")
        self.assertIn("Initialization Error", app.ext_result_label.text)


    # ── v4 hero verdict + per-image FAS badge tests ─────────────────────

    def test_hero_verdict_idle_state(self) -> None:
        app = self.gui_module.ModernGUI()
        app._update_hero_verdict(state="idle")
        self.assertEqual(app.hero_icon.text, "—")
        self.assertEqual(app.hero_headline.text, "R E A D Y")
        self.assertEqual(app.hero_score.text, "—")

    def test_hero_verdict_match_state(self) -> None:
        app = self.gui_module.ModernGUI()
        app._update_hero_verdict(state="match", score=87.3)
        self.assertEqual(app.hero_icon.text, "✓")
        self.assertEqual(app.hero_headline.text, "M A T C H")
        self.assertEqual(app.hero_score.text, "87.3%")
        # Threshold bar fills proportionally to the score.
        self.assertAlmostEqual(app.hero_threshold_bar.value, 0.873, places=3)

    def test_hero_verdict_no_match_state(self) -> None:
        app = self.gui_module.ModernGUI()
        app._update_hero_verdict(state="no_match", score=42.1)
        self.assertEqual(app.hero_icon.text, "✖")
        self.assertEqual(app.hero_headline.text, "N O   M A T C H")
        self.assertEqual(app.hero_score.text, "42.1%")
        self.assertAlmostEqual(app.hero_threshold_bar.value, 0.421, places=3)

    def test_hero_verdict_error_state(self) -> None:
        app = self.gui_module.ModernGUI()
        app._update_hero_verdict(state="error", error_msg="model load failed")
        self.assertEqual(app.hero_icon.text, "!")
        self.assertEqual(app.hero_headline.text, "E R R O R")
        self.assertIn("model load failed", app.hero_score.text)

    def test_per_image_fas_badge_real(self) -> None:
        # Engine says is_real=True with high real_conf → green REAL badge.
        app = self.gui_module.ModernGUI()
        app._set_per_image_fas_badge(
            app.zone1_fas_label, is_real=True, real_conf=0.97, status="ok",
        )
        self.assertIn("REAL", app.zone1_fas_label.text)
        self.assertIn("97.0%", app.zone1_fas_label.text)
        self.assertIn("✓", app.zone1_fas_label.text)

    def test_per_image_fas_badge_spoof(self) -> None:
        # Engine says is_real=False with high real_conf=0.0001 (i.e., 99.99%
        # confident SPOOF) → red SPOOF badge with the SPOOF confidence shown.
        # This is the regression case for the Driver's License bug.
        app = self.gui_module.ModernGUI()
        app._set_per_image_fas_badge(
            app.zone2_fas_label, is_real=False, real_conf=0.0001, status="ok",
        )
        self.assertIn("SPOOF", app.zone2_fas_label.text)
        # 1 - 0.0001 = 0.9999 → 99.99% confident SPOOF
        self.assertIn("99.99%", app.zone2_fas_label.text)
        self.assertIn("✖", app.zone2_fas_label.text)

    def test_per_image_fas_badge_no_face(self) -> None:
        app = self.gui_module.ModernGUI()
        app._set_per_image_fas_badge(
            app.zone1_fas_label, is_real=None, real_conf=None, status="no_face",
        )
        self.assertIn("no face", app.zone1_fas_label.text.lower())

    def test_per_image_fas_badge_disabled(self) -> None:
        app = self.gui_module.ModernGUI()
        app._set_per_image_fas_badge(
            app.zone1_fas_label, is_real=None, real_conf=None, status="not_active",
        )
        self.assertIn("liveness off", app.zone1_fas_label.text.lower())

    def test_set_ui_state_disables_anti_spoof_checkbox(self) -> None:
        # Bot finding fix (coderabbit, similarity/src/gui.py:210): the
        # anti_spoof_checkbox must be disabled mid-run so users can't toggle
        # it and end up with mismatched state.
        app = self.gui_module.ModernGUI()
        app.set_ui_state("disabled")
        self.assertEqual(app.anti_spoof_checkbox.state, "disabled")
        self.assertEqual(app.show_face_box_checkbox.state, "disabled")
        app.set_ui_state("normal")
        self.assertEqual(app.anti_spoof_checkbox.state, "normal")
        self.assertEqual(app.show_face_box_checkbox.state, "normal")


if __name__ == "__main__":
    unittest.main()
