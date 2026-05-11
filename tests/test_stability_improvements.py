import os
import inspect
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from PIL import Image

from fal_utils import fal_queue_poll, fal_queue_submit, upload_to_freeimage, upload_reference_image
from selfie_generator import SelfieGenerator
from outpaint_generator import OutpaintGenerator
from kling_generator_falai import FalAIKlingGenerator
from kling_gui.main_window import KlingGUIWindow
from kling_gui import theme
from kling_gui.carousel_widget import ImageCarousel
from kling_gui.config_panel import ConfigPanel
from kling_gui.queue_manager import QueueItem
from kling_gui.tabs.selfie_tab import SelfieTab
from kling_gui.tabs.video_tab import VideoTab
from kling_gui.drop_zone import DropZone


class FalPollCancellationTests(unittest.TestCase):
    def test_cancel_interrupts_poll_sleep(self):
        cancel_event = threading.Event()

        def _set_cancel():
            time.sleep(0.1)
            cancel_event.set()

        worker = threading.Thread(target=_set_cancel, daemon=True)
        worker.start()

        start = time.monotonic()
        with mock.patch("fal_utils._get_with_auth_fallback", side_effect=AssertionError("should not reach HTTP call")):
            result = fal_queue_poll(
                api_key="dummy",
                status_url="https://example.com/status",
                progress_cb=None,
                max_wait_seconds=30,
                cancel_event=cancel_event,
            )
        elapsed = time.monotonic() - start

        self.assertIsNone(result)
        self.assertLess(elapsed, 1.0)

    def test_timeout_reports_provider_endpoint_request_reason(self):
        logs = []

        def _cb(message, level):
            logs.append((message, level))

        result = fal_queue_poll(
            api_key="dummy",
            status_url="https://example.com/status",
            progress_cb=_cb,
            max_wait_seconds=0,
            cancel_event=None,
            provider="fal",
            endpoint="fal-ai/image-apps-v2/outpaint",
            request_id="12345678-abcdef00",
            operation_name="Outpaint",
        )
        self.assertIsNone(result)
        timeout_msgs = [m for m, lvl in logs if lvl == "error"]
        self.assertTrue(any("reason=provider_timeout" in m for m in timeout_msgs))
        self.assertTrue(any("Outpaint timeout" in m for m in timeout_msgs))
        self.assertTrue(any("endpoint=fal-ai/image-apps-v2/outpaint" in m for m in timeout_msgs))
        self.assertTrue(any("req=*abcdef00" in m for m in timeout_msgs))

    def test_cancel_reports_reason_endpoint_request_fragment(self):
        logs = []

        def _cb(message, level):
            logs.append((message, level))

        cancel_event = threading.Event()
        cancel_event.set()
        result = fal_queue_poll(
            api_key="dummy",
            status_url="https://example.com/status",
            progress_cb=_cb,
            max_wait_seconds=30,
            cancel_event=cancel_event,
            provider="fal",
            endpoint="fal-ai/image-apps-v2/outpaint",
            request_id="12345678-abcdef00",
            operation_name="Outpaint",
        )
        self.assertIsNone(result)
        warn_msgs = [m for m, lvl in logs if lvl == "warning"]
        self.assertTrue(any("reason=user_aborted" in m for m in warn_msgs))
        self.assertTrue(any("endpoint=fal-ai/image-apps-v2/outpaint" in m for m in warn_msgs))
        self.assertTrue(any("req=*abcdef00" in m for m in warn_msgs))


class UploadHandleTests(unittest.TestCase):
    def test_upload_error_releases_source_file_handle(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            image_path = os.path.join(tmpdir, "source.png")
            Image.new("RGB", (64, 64), (255, 0, 0)).save(image_path)

            with mock.patch("fal_utils.requests.post", side_effect=RuntimeError("network down")):
                url, _img = upload_to_freeimage(
                    image_path=image_path,
                    api_key="dummy",
                    progress_cb=None,
                )
            self.assertIsNone(url)

            renamed_path = os.path.join(tmpdir, "renamed.png")
            os.rename(image_path, renamed_path)
            self.assertTrue(os.path.exists(renamed_path))

    def test_upload_reference_image_prefers_fal_cdn(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            image_path = os.path.join(tmpdir, "source.png")
            Image.new("RGB", (64, 64), (255, 0, 0)).save(image_path)

            with mock.patch("fal_utils._fal_upload_jpeg_bytes", return_value="https://v3.fal.media/files/test.jpg"), \
                mock.patch("fal_utils.upload_to_freeimage") as freeimage_mock:
                url, processed, provider = upload_reference_image(
                    image_path=image_path,
                    fal_api_key="fal-key",
                    freeimage_api_key="free-key",
                    progress_cb=None,
                )

            self.assertEqual(provider, "fal_cdn")
            self.assertEqual(url, "https://v3.fal.media/files/test.jpg")
            self.assertIsNotNone(processed)
            freeimage_mock.assert_not_called()

    def test_upload_reference_image_falls_back_to_freeimage_on_transport_failure(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            image_path = os.path.join(tmpdir, "source.png")
            Image.new("RGB", (64, 64), (255, 0, 0)).save(image_path)
            fake_img = Image.new("RGB", (32, 32), (0, 0, 0))

            with mock.patch("fal_utils._fal_upload_jpeg_bytes", side_effect=RuntimeError("network down")), \
                mock.patch("fal_utils.upload_to_freeimage", return_value=("https://freeimage.host/img.jpg", fake_img)) as freeimage_mock:
                url, processed, provider = upload_reference_image(
                    image_path=image_path,
                    fal_api_key="fal-key",
                    freeimage_api_key="free-key",
                    progress_cb=None,
                )

            self.assertEqual(provider, "freeimage")
            self.assertEqual(url, "https://freeimage.host/img.jpg")
            self.assertIs(processed, fake_img)
            freeimage_mock.assert_called_once()

    def test_upload_reference_image_does_not_fallback_when_balance_locked(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            image_path = os.path.join(tmpdir, "source.png")
            Image.new("RGB", (64, 64), (255, 0, 0)).save(image_path)

            with mock.patch(
                "fal_utils._fal_upload_jpeg_bytes",
                side_effect=RuntimeError("User is locked. Reason: Exhausted balance."),
            ), mock.patch("fal_utils.upload_to_freeimage") as freeimage_mock:
                url, processed, provider = upload_reference_image(
                    image_path=image_path,
                    fal_api_key="fal-key",
                    freeimage_api_key="free-key",
                    progress_cb=None,
                )

            self.assertIsNone(url)
            self.assertIsNone(processed)
            self.assertIsNone(provider)
            freeimage_mock.assert_not_called()


class FalSubmitRetryBehaviorTests(unittest.TestCase):
    class _FakeResponse:
        def __init__(self, status_code=200, payload=None, text=""):
            self.status_code = status_code
            self._payload = payload if payload is not None else {}
            self.text = text

        def json(self):
            return self._payload

    def test_submit_uses_post_auth_fallback_before_failing(self):
        responses = [
            self._FakeResponse(
                status_code=403,
                payload={"detail": "User is locked. Reason: Exhausted balance."},
                text='{"detail":"User is locked. Reason: Exhausted balance."}',
            ),
            self._FakeResponse(
                status_code=200,
                payload={"request_id": "req-1", "status_url": "https://example.com/status"},
                text='{"request_id":"req-1","status_url":"https://example.com/status"}',
            ),
        ]
        progress = []

        with mock.patch("fal_utils.requests.post", side_effect=responses) as post_mock:
            result = fal_queue_submit(
                api_key="test-key",
                endpoint="fal-ai/test-model",
                payload={"prompt": "hello"},
                progress_cb=lambda message, level: progress.append((level, message)),
            )

        self.assertIsNotNone(result)
        self.assertEqual(result.get("request_id"), "req-1")
        self.assertEqual(post_mock.call_count, 2)
        self.assertFalse(any(level == "error" and "Submit failed" in message for level, message in progress))

    def test_submit_retry_logs_warning_until_terminal_failure(self):
        responses = [
            self._FakeResponse(status_code=500, payload={"detail": "temporary backend failure"}),
            self._FakeResponse(
                status_code=200,
                payload={"request_id": "req-2", "status_url": "https://example.com/status2"},
            ),
        ]
        progress = []

        with mock.patch("fal_utils.requests.post", side_effect=responses), \
            mock.patch("fal_utils.time.sleep", return_value=None):
            result = fal_queue_submit(
                api_key="test-key",
                endpoint="fal-ai/test-model",
                payload={"prompt": "hello"},
                progress_cb=lambda message, level: progress.append((level, message)),
            )

        self.assertIsNotNone(result)
        self.assertTrue(any(level == "warning" and "Submit retrying" in message for level, message in progress))
        self.assertFalse(any(level == "error" and "Submit failed" in message for level, message in progress))


class SubmitErrorDetailTests(unittest.TestCase):
    class _FakeResponse:
        def __init__(self, status_code=200, payload=None, text=""):
            self.status_code = status_code
            self._payload = payload
            self.text = text

        def json(self):
            if self._payload is None:
                raise ValueError("no payload")
            return self._payload

    def test_submit_403_balance_lock_sets_detailed_last_error_and_skips_retries(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            image_path = os.path.join(tmpdir, "source.png")
            Image.new("RGB", (64, 64), (255, 0, 0)).save(image_path)

            generator = FalAIKlingGenerator(api_key="fal-key", verbose=False)
            generator.schema_manager.validate_parameters = lambda _endpoint, payload: payload

            response = self._FakeResponse(
                status_code=403,
                payload={"detail": "User is locked. Reason: Exhausted balance. Top up your balance."},
                text='{"detail":"User is locked. Reason: Exhausted balance. Top up your balance."}',
            )

            with mock.patch.object(generator, "upload_to_freeimage", return_value="https://v3.fal.media/files/test.jpg"), \
                mock.patch("kling_generator_falai.requests.post", return_value=response) as post_mock, \
                mock.patch("kling_generator_falai.time.sleep", return_value=None):
                result = generator.create_kling_generation(
                    character_image_path=image_path,
                    output_folder=tmpdir,
                    custom_prompt="test",
                    skip_duplicate_check=True,
                )

            self.assertIsNone(result)
            self.assertIn("HTTP 403", generator.last_error_message or "")
            self.assertIn("Exhausted balance", generator.last_error_message or "")
            self.assertEqual(post_mock.call_count, 1)


class DeepMergeTests(unittest.TestCase):
    def test_deep_merge_dict_preserves_nested_defaults(self):
        window = KlingGUIWindow.__new__(KlingGUIWindow)
        base = {
            "saved_prompts": {"1": "a", "2": "b"},
            "selfie_selected_models": {"m1": True, "m2": False},
            "simple": 1,
        }
        updates = {
            "saved_prompts": {"1": "override"},
            "selfie_selected_models": {"m2": True},
            "simple": 3,
        }
        merged = window._deep_merge_dict(base, updates)
        self.assertEqual(merged["saved_prompts"]["1"], "override")
        self.assertEqual(merged["saved_prompts"]["2"], "b")
        self.assertEqual(merged["selfie_selected_models"]["m1"], True)
        self.assertEqual(merged["selfie_selected_models"]["m2"], True)
        self.assertEqual(merged["simple"], 3)


class SelfieModelLoadingTests(unittest.TestCase):
    def test_selfie_models_loaded_from_models_json(self):
        payload = {
            "selfie_models": [
                {
                    "endpoint": "fal-ai/test-model/edit",
                    "label": "Test Model",
                    "slug": "test-model",
                    "provider": "fal",
                    "api_url": "https://fal.ai/models/fal-ai/test-model/edit/api",
                }
            ]
        }
        with tempfile.TemporaryDirectory() as tmpdir:
            models_path = Path(tmpdir) / "models.json"
            models_path.write_text(__import__("json").dumps(payload), encoding="utf-8")
            with mock.patch.object(SelfieGenerator, "_SELFIE_MODELS_FILE", models_path):
                SelfieGenerator._refresh_available_models()
                models = SelfieGenerator.get_available_models()
        self.assertEqual(models[0]["endpoint"], "fal-ai/test-model/edit")
        self.assertEqual(models[0]["label"], "Test Model")


class MacButtonFactoryTests(unittest.TestCase):
    def test_macos_button_factory_uses_tk_button_styling(self):
        root = __import__("tkinter").Tk()
        root.withdraw()
        try:
            with mock.patch.object(theme, "IS_MACOS", True):
                btn = theme.create_action_button(root, text="X", command=lambda: None)
            self.assertEqual(str(btn.cget("highlightbackground")), theme.COLORS["bg_main"])
            self.assertGreaterEqual(int(btn.cget("padx")), 12)
            self.assertGreaterEqual(int(btn.cget("pady")), 8)
        finally:
            root.destroy()


class SelfieSlotStateTests(unittest.TestCase):
    class _FakeVar:
        def __init__(self):
            self.value = None
        def set(self, value):
            self.value = value
        def get(self):
            return self.value

    class _FakeText:
        def __init__(self, text=""):
            self.text = text
            self.state = "normal"
        def get(self, *_args):
            return self.text
        def delete(self, *_args):
            self.text = ""
        def insert(self, *_args):
            self.text = _args[-1]
        def config(self, **kwargs):
            if "state" in kwargs:
                self.state = kwargs["state"]

    class _FakeEntry:
        def __init__(self):
            self.state = None
        def config(self, **kwargs):
            if "state" in kwargs:
                self.state = kwargs["state"]

    class _FakeButton:
        def config(self, **_kwargs):
            return None

    def test_slot_defaults_and_migration(self):
        tab = SelfieTab.__new__(SelfieTab)
        tab.SLOT_COUNT = 10
        tab.DEFAULT_PROMPT_TEMPLATE = "DEFAULT"
        tab.config = {"selfie_prompt_template": "LEGACY TEMPLATE"}
        tab._selfie_slot_var = self._FakeVar()
        tab._init_selfie_prompt_slots()

        self.assertEqual(tab.config["selfie_current_prompt_slot"], 3)
        self.assertEqual(tab.config["selfie_saved_prompts"]["1"], "LEGACY TEMPLATE")
        self.assertEqual(tab.config["selfie_prompt_titles"]["1"], "")
        self.assertEqual(tab._selfie_slot_var.get(), 3)

    def test_slot_current_clamped(self):
        tab = SelfieTab.__new__(SelfieTab)
        tab.SLOT_COUNT = 10
        tab.DEFAULT_PROMPT_TEMPLATE = "DEFAULT"
        tab.config = {"selfie_current_prompt_slot": 99, "selfie_saved_prompts": {}, "selfie_prompt_titles": {}}
        tab._selfie_slot_var = self._FakeVar()
        tab._init_selfie_prompt_slots()
        self.assertEqual(tab.config["selfie_current_prompt_slot"], 3)

    def test_stale_gpt_only_selection_migrates_to_nano_banana_only(self):
        tab = SelfieTab.__new__(SelfieTab)
        tab.DEFAULT_MODEL_ENDPOINT = "fal-ai/nano-banana-2/edit"
        tab._supported_model_endpoints = {
            "fal-ai/nano-banana-2/edit",
            "openai/gpt-image-2/edit",
            "fal-ai/flux-pulid",
        }
        tab.config = {"selfie_selected_models": {"openai/gpt-image-2/edit": True}}
        tab._migrate_selected_models_config()
        merged = tab.config["selfie_selected_models"]
        self.assertTrue(merged["fal-ai/nano-banana-2/edit"])
        self.assertFalse(merged["openai/gpt-image-2/edit"])
        self.assertFalse(merged["fal-ai/flux-pulid"])

    def test_legacy_list_selection_migrates_to_dict(self):
        tab = SelfieTab.__new__(SelfieTab)
        tab.DEFAULT_MODEL_ENDPOINT = "fal-ai/nano-banana-2/edit"
        tab._supported_model_endpoints = {
            "fal-ai/nano-banana-2/edit",
            "openai/gpt-image-2/edit",
            "fal-ai/flux-pulid",
        }
        tab.config = {"selfie_selected_models": ["fal-ai/flux-pulid"]}
        tab._migrate_selected_models_config()
        merged = tab.config["selfie_selected_models"]
        self.assertTrue(merged["fal-ai/flux-pulid"])
        self.assertFalse(merged["openai/gpt-image-2/edit"])

    def test_valid_multi_model_selection_is_preserved(self):
        tab = SelfieTab.__new__(SelfieTab)
        tab.DEFAULT_MODEL_ENDPOINT = "fal-ai/nano-banana-2/edit"
        tab._supported_model_endpoints = {
            "fal-ai/nano-banana-2/edit",
            "openai/gpt-image-2/edit",
            "fal-ai/flux-pulid",
        }
        tab.config = {
            "selfie_selected_models": {
                "fal-ai/nano-banana-2/edit": True,
                "fal-ai/flux-pulid": True,
            }
        }
        tab._migrate_selected_models_config()
        merged = tab.config["selfie_selected_models"]
        self.assertTrue(merged["fal-ai/nano-banana-2/edit"])
        self.assertTrue(merged["fal-ai/flux-pulid"])


class OutpaintAbortLoggingTests(unittest.TestCase):
    class _FakeVar:
        def __init__(self, value=None):
            self.value = value

        def get(self):
            return self.value

        def set(self, value):
            self.value = value

    class _FakeText:
        def __init__(self, text):
            self.text = text

        def get(self, _start, _end):
            return self.text

        def config(self, **_kwargs):
            return None

    class _FakeEntry:
        def __init__(self):
            self.state = None

        def config(self, **kwargs):
            if "state" in kwargs:
                self.state = kwargs["state"]

    class _FakeButton:
        def config(self, **_kwargs):
            return None

    def test_user_abort_does_not_emit_failed_or_timed_out_error(self):
        generator = OutpaintGenerator(api_key="x")
        logs = []
        generator.set_progress_callback(lambda msg, lvl: logs.append((msg, lvl)))

        with tempfile.TemporaryDirectory() as tmpdir:
            image_path = str(Path(tmpdir) / "input.png")
            Image.new("RGB", (64, 64), (20, 30, 40)).save(image_path)
            with mock.patch("fal_utils.fal_queue_submit", return_value={"request_id": "req-1234", "status_url": "https://example/status"}), \
                mock.patch("fal_utils.upload_reference_image", return_value=("https://img", Image.new("RGB", (16, 16)), "fal_cdn")), \
                mock.patch("fal_utils.fal_queue_poll", return_value=None), \
                mock.patch("outpaint_generator.os.path.exists", return_value=False):
                cancel = threading.Event()
                cancel.set()
                result = generator.outpaint(
                    image_path=image_path,
                    output_folder=tmpdir,
                    cancel_event=cancel,
                )
        self.assertIsNone(result)
        self.assertFalse(any("Outpaint failed or timed out" in m for m, lvl in logs if lvl == "error"))

    def test_persist_does_not_overwrite_custom_title_without_explicit_title_save(self):
        tab = SelfieTab.__new__(SelfieTab)
        tab.SLOT_COUNT = 10
        tab.DEFAULT_PROMPT_TEMPLATE = "DEFAULT"
        tab._selfie_slot_var = self._FakeVar()
        tab._selfie_slot_var.set(1)
        tab._slot_title_var = self._FakeVar()
        tab._slot_title_var.set("Unsaved UI Title")
        tab.prompt_template_text = self._FakeText("New prompt text")
        tab.config = {
            "selfie_saved_prompts": {"1": "Old prompt"},
            "selfie_prompt_titles": {"1": "Custom Saved Title"},
        }
        tab._persist_active_slot_prompt(persist_title=False)
        self.assertEqual(tab.config["selfie_saved_prompts"]["1"], "New prompt text")
        self.assertEqual(tab.config["selfie_prompt_titles"]["1"], "Custom Saved Title")

    def test_persist_with_title_save_updates_custom_title(self):
        tab = SelfieTab.__new__(SelfieTab)
        tab.SLOT_COUNT = 10
        tab.DEFAULT_PROMPT_TEMPLATE = "DEFAULT"
        tab._selfie_slot_var = self._FakeVar()
        tab._selfie_slot_var.set(2)
        tab._slot_title_var = self._FakeVar()
        tab._slot_title_var.set("My Slot Two")
        tab.prompt_template_text = self._FakeText("Prompt two")
        tab.config = {
            "selfie_saved_prompts": {"2": "Old"},
            "selfie_prompt_titles": {"2": "Prompt 2"},
        }
        tab._persist_active_slot_prompt(persist_title=True)
        self.assertEqual(tab.config["selfie_prompt_titles"]["2"], "My Slot Two")
        self.assertEqual(tab.config["selfie_current_prompt_slot"], 2)

    def test_template_edit_mode_uses_readonly_outside_edit(self):
        tab = SelfieTab.__new__(SelfieTab)
        tab.prompt_template_text = self._FakeText("")
        tab._slot_title_entry = self._FakeEntry()
        tab.edit_template_btn = self._FakeButton()
        tab.save_template_btn = self._FakeButton()
        tab._set_prompt_template_edit_mode(True)
        self.assertEqual(tab._slot_title_entry.state, "normal")
        tab._set_prompt_template_edit_mode(False)
        self.assertEqual(tab._slot_title_entry.state, "readonly")

    def test_step2_layout_no_extra_slot_title_rows(self):
        src = inspect.getsource(SelfieTab._build_ui)
        self.assertNotIn("slot_row =", src)
        self.assertNotIn("title_row =", src)


class DropZoneWindowsRenderTests(unittest.TestCase):
    def test_compact_windows_uses_canvas_title(self):
        try:
            root = __import__("tkinter").Tk()
        except Exception as exc:
            self.skipTest(f"Tk unavailable in this environment: {exc}")
        root.withdraw()
        try:
            with mock.patch("kling_gui.drop_zone.sys.platform", "win32"), mock.patch("kling_gui.drop_zone.HAS_DND", False):
                zone = DropZone(root, on_files_dropped=lambda *_: None, compact=True)
            self.assertIsNotNone(zone._main_text_canvas)
        finally:
            root.destroy()


class OldcamRerunFlowTests(unittest.TestCase):
    def test_config_panel_contains_oldcam_rerun_icon_next_to_version_selector(self):
        src = inspect.getsource(ConfigPanel._setup_ui)
        version_pos = src.find("self.oldcam_version_vars")
        rerun_pos = src.find("self.oldcam_rerun_btn")
        self.assertTrue(version_pos >= 0 and rerun_pos > version_pos)

    def test_config_panel_oldcam_versions_include_v7_v8_v9_v10(self):
        src = inspect.getsource(ConfigPanel._setup_ui)
        self.assertIn('self.oldcam_version_vars = {', src)
        self.assertIn('"v7"', src)
        self.assertIn('"v8"', src)
        self.assertIn('"v9"', src)
        self.assertIn('"v10"', src)

    def test_resolve_oldcam_rerun_source_prefers_base_video_when_available(self):
        window = KlingGUIWindow.__new__(KlingGUIWindow)
        window._log = mock.Mock()
        with tempfile.TemporaryDirectory() as tmpdir:
            base = Path(tmpdir) / "clip_looped.mp4"
            oldcam = Path(tmpdir) / "clip_looped-oldcam-v8.mp4"
            base.write_bytes(b"base")
            oldcam.write_bytes(b"oldcam")
            resolved = window._resolve_oldcam_rerun_source(str(oldcam))
        self.assertEqual(resolved, str(base))
        window._log.assert_not_called()

    def test_resolve_oldcam_rerun_source_supports_future_version_suffix(self):
        window = KlingGUIWindow.__new__(KlingGUIWindow)
        window._log = mock.Mock()
        with tempfile.TemporaryDirectory() as tmpdir:
            base = Path(tmpdir) / "clip_looped.mp4"
            oldcam = Path(tmpdir) / "clip_looped-oldcam-v9.mp4"
            base.write_bytes(b"base")
            oldcam.write_bytes(b"oldcam")
            resolved = window._resolve_oldcam_rerun_source(str(oldcam))
        self.assertEqual(resolved, str(base))
        window._log.assert_not_called()

    def test_oldcam_rerun_uses_selected_history_output_first(self):
        window = KlingGUIWindow.__new__(KlingGUIWindow)
        window._log = mock.Mock()
        window.config = {"oldcam_version": "v7"}
        window.history = []
        window.queue_manager = mock.Mock()
        window.queue_manager.rerun_oldcam_only.return_value = True
        window._get_latest_completed_history = lambda: {"output": "unused"}
        with tempfile.TemporaryDirectory() as tmpdir:
            selected_path = Path(tmpdir) / "picked.mp4"
            selected_path.write_bytes(b"video")
            window._get_selected_history = lambda: {"output": str(selected_path)}
            window._on_oldcam_rerun_requested()

        args, kwargs = window.queue_manager.rerun_oldcam_only.call_args
        self.assertEqual(args[0], str(selected_path))
        self.assertIn("completion_callback", kwargs)

    def test_oldcam_rerun_falls_back_to_latest_completed_when_no_selection(self):
        window = KlingGUIWindow.__new__(KlingGUIWindow)
        window._log = mock.Mock()
        window.config = {"oldcam_version": "v8"}
        window.history = []
        window.queue_manager = mock.Mock()
        window.queue_manager.rerun_oldcam_only.return_value = True
        window._get_selected_history = lambda: None
        with tempfile.TemporaryDirectory() as tmpdir:
            latest = Path(tmpdir) / "latest.mp4"
            latest.write_bytes(b"video")
            window._get_latest_completed_history = lambda: {"output": str(latest)}
            window._on_oldcam_rerun_requested()

        args, _kwargs = window.queue_manager.rerun_oldcam_only.call_args
        self.assertEqual(args[0], str(latest))

    def test_on_item_complete_does_not_emit_duplicate_failed_log(self):
        window = KlingGUIWindow.__new__(KlingGUIWindow)
        logs = []
        window._log = lambda message, level="info": logs.append((message, level))
        window.history = []
        window._save_history = lambda: None
        window._refresh_history_view = lambda: None
        window.queue_manager = None
        window.generator = None
        window.config = {}
        item = QueueItem(path="C:/tmp/input.png", status="failed", error_message="Request failed: HTTP 422")

        window._on_item_complete(item)

        self.assertFalse(any(message.startswith("Failed ") for message, _level in logs))


class CarouselFolderButtonTests(unittest.TestCase):
    class _FakeSession:
        def __init__(self, entry):
            self.active_entry = entry

    def test_open_active_folder_opens_parent_dir(self):
        carousel = ImageCarousel.__new__(ImageCarousel)
        with tempfile.TemporaryDirectory() as tmpdir:
            image_path = str(Path(tmpdir) / "active.png")
            Path(image_path).write_bytes(b"x")
            carousel.image_session = self._FakeSession(SimpleNamespace(path=image_path))
            carousel.log = mock.Mock()
            carousel._open_path_in_explorer = mock.Mock()

            carousel._on_open_active_image_folder()

            carousel._open_path_in_explorer.assert_called_once_with(tmpdir)
            carousel.log.assert_not_called()

    def test_open_active_folder_warns_when_no_active_entry(self):
        carousel = ImageCarousel.__new__(ImageCarousel)
        carousel.image_session = self._FakeSession(None)
        carousel.log = mock.Mock()
        carousel._open_path_in_explorer = mock.Mock()

        carousel._on_open_active_image_folder()

        carousel._open_path_in_explorer.assert_not_called()
        carousel.log.assert_called_once_with(
            "No active carousel image to open folder for",
            "warning",
        )

    def test_open_active_folder_warns_when_parent_folder_missing(self):
        carousel = ImageCarousel.__new__(ImageCarousel)
        missing_path = str(Path("Z:/path/that/does/not/exist/image.png"))
        carousel.image_session = self._FakeSession(SimpleNamespace(path=missing_path))
        carousel.log = mock.Mock()
        carousel._open_path_in_explorer = mock.Mock()

        carousel._on_open_active_image_folder()

        carousel._open_path_in_explorer.assert_not_called()
        carousel.log.assert_called_once_with(
            "No folder to open for active carousel image",
            "warning",
        )

    def test_similarity_row_button_order_includes_folder_icon_before_auto(self):
        src = inspect.getsource(ImageCarousel._build_panel)
        ref_pos = src.find("self._ref_btn")
        compare_pos = src.find("self.compare_btn")
        folder_pos = src.find("self.open_active_folder_btn")
        auto_pos = src.find("self._auto_chk")
        self.assertTrue(0 <= ref_pos < compare_pos < folder_pos < auto_pos)

    def test_similarity_row_buttons_use_compact_style(self):
        src = inspect.getsource(ImageCarousel._build_panel)
        # Ref/Compare use tk.Button for custom legibility styling.
        self.assertIn("self._ref_btn = tk.Button(", src)
        self.assertIn("self.compare_btn = tk.Button(", src)
        self.assertIn("self.open_active_folder_btn = ttk.Button(", src)
        self.assertGreaterEqual(src.count("style=TTK_BTN_COMPACT"), 3)


class Step3UiTighteningTests(unittest.TestCase):
    def test_setup_ui_keeps_queue_panel_hidden(self):
        src = inspect.getsource(KlingGUIWindow._setup_ui)
        self.assertIn("self._queue_panel_visible = False", src)
        self.assertIn("if self._queue_panel_visible:", src)

    def test_queue_controls_are_disabled_in_setup(self):
        src = inspect.getsource(KlingGUIWindow._setup_controls)
        self.assertIn("self._set_queue_controls_enabled(False)", src)

    def test_update_queue_display_safe_when_widgets_missing(self):
        window = KlingGUIWindow.__new__(KlingGUIWindow)
        window._update_queue_display()

    def test_queue_backend_clear_still_callable(self):
        window = KlingGUIWindow.__new__(KlingGUIWindow)
        window.queue_manager = mock.Mock()
        window._clear_queue()
        window.queue_manager.clear_queue.assert_called_once()

    def test_video_tab_helper_text_no_queue_wording(self):
        src = inspect.getsource(VideoTab.__init__)
        self.assertIn("Use active carousel image for video generation", src)
        self.assertNotIn("video queue", src)


if __name__ == "__main__":
    unittest.main()
