import unittest
from types import SimpleNamespace
from unittest import mock

from kling_gui.tabs import face_crop_tab as face_crop_tab_module
from kling_gui.tabs.face_crop_tab import FaceCropTab


class _FakeWidget:
    def __init__(self):
        self.pack_calls = []
        self.pack_forget_calls = 0
        self.config_calls = []

    def pack(self, **kwargs):
        self.pack_calls.append(kwargs)

    def pack_forget(self):
        self.pack_forget_calls += 1

    def config(self, **kwargs):
        self.config_calls.append(kwargs)


class _FakeSized:
    def __init__(self, width):
        self._width = width

    def winfo_width(self):
        return self._width


class _FakeVar:
    def __init__(self):
        self.value = None

    def set(self, value):
        self.value = value


class _FakeGetVar:
    def __init__(self, value):
        self._value = value

    def get(self):
        return self._value


class FaceCropLayoutTests(unittest.TestCase):
    def test_browse_row_reflow_noop_without_secondary_button(self):
        tab = FaceCropTab.__new__(FaceCropTab)
        tab._source_frame = _FakeSized(1000)
        tab._refresh_browse_row_layout()

    def test_status_wraplength_updates(self):
        tab = FaceCropTab.__new__(FaceCropTab)
        tab._source_frame = _FakeSized(900)
        tab._status_label = _FakeWidget()
        tab._outpaint_status = _FakeWidget()
        tab._upscale_status = _FakeWidget()
        tab.winfo_width = lambda: 1200

        tab._refresh_status_wraplengths()

        self.assertEqual(tab._status_label.config_calls[-1]["wraplength"], 820)
        self.assertEqual(tab._outpaint_status.config_calls[-1]["wraplength"], 1100)
        self.assertEqual(tab._upscale_status.config_calls[-1]["wraplength"], 1100)

    def test_detect_uses_active_carousel_when_source_not_loaded(self):
        tab = FaceCropTab.__new__(FaceCropTab)
        tab._busy = False
        tab._source_path = None
        tab._original_path = None
        tab._path_var = _FakeVar()
        tab._status_label = _FakeWidget()
        tab._detect_btn = _FakeWidget()
        tab.log = mock.Mock()
        tab.image_session = SimpleNamespace(active_image_path="/tmp/active.jpg")
        tab._load_source = mock.Mock(side_effect=lambda _p: setattr(tab, "_source_path", "/tmp/corrected.jpg"))
        tab._detect_worker = mock.Mock()

        with mock.patch.object(face_crop_tab_module, "HAS_FACE_DEPS", True), \
            mock.patch.object(face_crop_tab_module, "_load_retinaface", return_value=(SimpleNamespace(), "")), \
            mock.patch.object(face_crop_tab_module.threading, "Thread") as thread_mock:
            tab._detect_face()

        self.assertEqual(tab._path_var.value, "/tmp/active.jpg")
        tab._load_source.assert_called_once_with("/tmp/active.jpg")
        thread_mock.assert_called_once()

    def test_detect_warns_when_no_source_and_no_active_carousel(self):
        tab = FaceCropTab.__new__(FaceCropTab)
        tab._busy = False
        tab._source_path = None
        tab._path_var = _FakeVar()
        tab._status_label = _FakeWidget()
        tab.image_session = SimpleNamespace(active_image_path=None)

        with mock.patch.object(face_crop_tab_module, "HAS_FACE_DEPS", True):
            tab._detect_face()

        self.assertEqual(tab._status_label.config_calls[-1]["text"], "No image in carousel")

    def test_active_carousel_change_refreshes_preview_silently(self):
        tab = FaceCropTab.__new__(FaceCropTab)
        tab._path_var = _FakeVar()
        tab._original_path = "/tmp/old.jpg"
        tab.image_session = SimpleNamespace(active_image_path="/tmp/new.jpg")
        tab._load_source = mock.Mock()
        with mock.patch.object(face_crop_tab_module.os.path, "isfile", return_value=True):
            tab._on_image_session_change()
        self.assertEqual(tab._path_var.value, "/tmp/new.jpg")
        tab._load_source.assert_called_once_with("/tmp/new.jpg", silent=True)

    def test_detect_worker_falls_back_when_retinaface_kerastensor_error(self):
        tab = FaceCropTab.__new__(FaceCropTab)
        tab._source_path = "/tmp/source.jpg"
        tab._after_detect = mock.Mock()
        tab.log = mock.Mock()
        fake_img = object()
        tab._detect_face_with_opencv_fallback = mock.Mock(return_value=(1, 2, 30, 40))

        class _Retina:
            @staticmethod
            def detect_faces(_source):
                raise RuntimeError("A KerasTensor cannot be used as input to a TensorFlow function")

        with mock.patch.object(face_crop_tab_module.cv2, "imread", return_value=fake_img):
            tab._detect_worker(_Retina)

        tab._detect_face_with_opencv_fallback.assert_called_once_with(fake_img)
        tab._after_detect.assert_called_once_with(fake_img, (1, 2, 30, 40), None)

    def test_get_config_updates_includes_outpaint_double_expand(self):
        tab = FaceCropTab.__new__(FaceCropTab)
        tab._multiplier_var = _FakeGetVar(1.5)
        tab._auto_switch_var = _FakeGetVar(True)
        tab._polish_provider_var = _FakeGetVar("provider")
        tab._polish_strength_var = _FakeGetVar(0.2)
        tab._upscale_provider_var = _FakeGetVar("Crystal (Portraits)")
        tab._upscale_scale_var = _FakeGetVar("2x")
        tab._upscale_creativity_var = _FakeGetVar(0.0)
        tab._upscale_resemblance_var = _FakeGetVar(0.9)
        tab._expand_mode_var = _FakeGetVar("percentage")
        tab._pct_var = _FakeGetVar(30)
        tab._expand_left_var = _FakeGetVar(100)
        tab._expand_right_var = _FakeGetVar(100)
        tab._expand_top_var = _FakeGetVar(120)
        tab._expand_bottom_var = _FakeGetVar(120)
        tab._outpaint_format_var = _FakeGetVar("png")
        tab._outpaint_composite_var = _FakeGetVar("feathered")
        tab._outpaint_provider_var = _FakeGetVar("bfl")
        tab._outpaint_double_expand_var = _FakeGetVar(True)
        tab._expanded_sections = []
        tab._outpaint_prompt_str = "prompt"
        tab.config = {"face_crop_polish_prompt": "x"}

        updates = tab.get_config_updates()
        self.assertTrue(updates["outpaint_double_expand"])


if __name__ == "__main__":
    unittest.main()
