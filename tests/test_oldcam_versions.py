import importlib.util
import builtins
from pathlib import Path
from types import SimpleNamespace
from unittest import mock
import threading
import types
import sys

import numpy as np
import pytest

from kling_gui.queue_manager import QueueItem, QueueManager


ROOT = Path(__file__).resolve().parents[1]


def load_module(path: Path, name: str):
    injected_fake = False
    injected_names = []
    if "mediapipe" not in sys.modules:
        class _FakeImage:
            def __init__(self, *args, **kwargs):
                pass

        fake_mp = types.ModuleType("mediapipe")
        fake_mp.Image = _FakeImage
        fake_mp.ImageFormat = types.SimpleNamespace(SRGB=0)
        fake_mp.__version__ = "0.10.35"
        fake_mp.__file__ = "site-packages/mediapipe/__init__.py"

        fake_tasks = types.ModuleType("mediapipe.tasks")
        fake_tasks_python = types.ModuleType("mediapipe.tasks.python")
        fake_tasks_python.BaseOptions = lambda **kwargs: types.SimpleNamespace(**kwargs)
        fake_tasks_python_vision = types.ModuleType("mediapipe.tasks.python.vision")
        fake_tasks_python_vision.FaceLandmarkerOptions = lambda **kwargs: types.SimpleNamespace(**kwargs)
        fake_tasks_python_vision.FaceLandmarker = types.SimpleNamespace(
            create_from_options=lambda _opts: types.SimpleNamespace(
                detect=lambda _img: types.SimpleNamespace(face_landmarks=[]),
                close=lambda: None,
            )
        )

        sys.modules["mediapipe"] = fake_mp
        sys.modules["mediapipe.tasks"] = fake_tasks
        sys.modules["mediapipe.tasks.python"] = fake_tasks_python
        sys.modules["mediapipe.tasks.python.vision"] = fake_tasks_python_vision
        injected_names = [
            "mediapipe",
            "mediapipe.tasks",
            "mediapipe.tasks.python",
            "mediapipe.tasks.python.vision",
        ]
        injected_fake = True
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(module)
    finally:
        if injected_fake:
            for name in injected_names:
                sys.modules.pop(name, None)
    return module


def make_queue_manager(config):
    logs = []
    manager = QueueManager(
        generator=SimpleNamespace(),
        config_getter=lambda: config,
        log_callback=lambda message, level="info": logs.append((message, level)),
        queue_update_callback=lambda: None,
    )
    return manager, logs


def test_oldcam_version_defaults_to_v9_for_missing_or_invalid_config():
    manager, _ = make_queue_manager({})
    assert manager._get_oldcam_version() == "v9"

    manager, _ = make_queue_manager({"oldcam_version": "invalid"})
    assert manager._get_oldcam_version() == "v9"


def test_oldcam_legacy_explicit_v7_is_preserved():
    manager, _ = make_queue_manager({"oldcam_version": "v7"})
    assert manager._get_oldcam_version() == "v7"


def test_oldcam_empty_versions_list_disables_oldcam():
    manager, _ = make_queue_manager({"oldcam_versions": []})
    assert manager._get_oldcam_versions_to_run() == []


def test_oldcam_versions_list_is_supported():
    manager, _ = make_queue_manager({"oldcam_versions": ["v7", "v10"]})
    assert manager._get_oldcam_versions_to_run() == ["v7", "v10"]


def test_oldcam_output_path_uses_versioned_suffixes():
    manager, _ = make_queue_manager({"oldcam_version": "v7"})
    source = Path("clip_looped.mp4")

    assert manager._build_oldcam_output_path(source, "v7") == Path("clip_looped-oldcam-v7.mp4")
    assert manager._build_oldcam_output_path(source, "v8") == Path("clip_looped-oldcam-v8.mp4")


def test_discover_oldcam_versions_includes_future_version(tmp_path):
    # Create a hypothetical future "v99" folder alongside known versions to confirm
    # the discovery logic is generic (not hardcoded to a fixed set).
    for version in ("7", "8", "9", "99"):
        folder = tmp_path / f"oldcam-v{version}"
        folder.mkdir()
        (folder / "launcher.py").write_text("pass", encoding="utf-8")

    manager, _ = make_queue_manager({"oldcam_version": "all"})
    with mock.patch("kling_gui.queue_manager.get_app_dir", return_value=str(tmp_path)), \
        mock.patch("kling_gui.queue_manager.get_resource_dir", return_value=str(tmp_path)):
        # Also mock __file__-based root to point at tmp_path so only tmp_path is scanned
        import kling_gui.queue_manager as qm_mod
        with mock.patch.object(
            qm_mod, "__file__",
            str(tmp_path / "kling_gui" / "queue_manager.py"),
        ):
            versions = manager._discover_oldcam_versions()

    assert "v9" in versions
    assert "v99" in versions  # future/arbitrary numeric versions are discovered


def test_queue_manager_selects_oldcam_version_folder_and_output(tmp_path):
    input_path = tmp_path / "clip.mp4"
    input_path.write_bytes(b"video")
    output_path = tmp_path / "clip-oldcam-v8.mp4"

    manager, logs = make_queue_manager({"oldcam_version": "v8"})

    with mock.patch.object(manager, "_resolve_oldcam_dir", return_value=tmp_path / "oldcam-v8") as resolve_mock, \
        mock.patch.object(manager, "_ensure_oldcam_dependencies", return_value=True), \
        mock.patch("kling_gui.queue_manager.subprocess.Popen") as popen_mock:
        oldcam_dir = tmp_path / "oldcam-v8"
        oldcam_dir.mkdir()
        (oldcam_dir / "launcher.py").write_text("pass", encoding="utf-8")

        import io
        fake_stdout = io.StringIO("Processing frame 1/10\n")
        fake_proc = SimpleNamespace(stdout=fake_stdout, poll=lambda: 0)
        fake_proc.wait = lambda timeout=None: (output_path.write_bytes(b"done"), 0)[1]
        popen_mock.return_value = fake_proc

        result = manager._oldcam_video(str(input_path), QueueItem(str(input_path)))

    assert result == str(output_path)
    resolve_mock.assert_called_once_with("v8")
    assert any("v8" in message for message, _level in logs)


def test_oldcam_all_mode_runs_all_versions_and_returns_highest():
    manager, _ = make_queue_manager({"oldcam_version": "all"})
    source = Path("clip.mp4")
    expected_paths = {
        "v7": str(source.with_name("clip-oldcam-v7.mp4")),
        "v8": str(source.with_name("clip-oldcam-v8.mp4")),
        "v9": str(source.with_name("clip-oldcam-v9.mp4")),
    }
    calls = []

    def _fake_run(_path, version):
        calls.append(version)
        return expected_paths[version]

    with mock.patch.object(manager, "_get_oldcam_versions_to_run", return_value=["v7", "v8", "v9"]), \
        mock.patch.object(manager, "_get_oldcam_version", return_value="all"), \
        mock.patch.object(manager, "_run_oldcam_version", side_effect=_fake_run):
        result = manager._oldcam_video(str(source), QueueItem(str(source)))

    assert calls == ["v7", "v8", "v9"]
    assert result == expected_paths["v9"]


def test_generation_error_message_prefers_generator_last_error():
    manager, _ = make_queue_manager({})
    manager.generator = SimpleNamespace(last_error_message="Submit failed: HTTP 422 — prompt too long")
    assert manager._get_generation_error_message() == "Submit failed: HTTP 422 — prompt too long"

    manager.generator = SimpleNamespace(last_error_message="")
    assert manager._get_generation_error_message() == "Generation failed"


def test_v7_default_output_path_uses_v7_suffix():
    oldcam_v7 = load_module(ROOT / "oldcam-v7" / "oldcam.py", "oldcam_v7")
    assert oldcam_v7.build_default_output_path("sample.mp4").endswith("sample-oldcam-v7.mp4")


def test_v8_default_output_path_uses_v8_suffix():
    oldcam_v8 = load_module(ROOT / "oldcam-v8" / "oldcam.py", "oldcam_v8")
    assert oldcam_v8.build_default_output_path("sample.mp4").endswith("sample-oldcam-v8.mp4")


def test_v9_default_output_path_uses_v9_suffix():
    oldcam_v9 = load_module(ROOT / "oldcam-v9" / "oldcam.py", "oldcam_v9")
    assert oldcam_v9.build_default_output_path("sample.mp4").endswith("sample-oldcam-v9.mp4")


def test_v10_default_output_path_uses_v10_suffix():
    oldcam_v10 = load_module(ROOT / "oldcam-v10" / "oldcam.py", "oldcam_v10")
    assert oldcam_v10.build_default_output_path("sample.mp4").endswith("sample-oldcam-v10.mp4")


def test_v8_ois_jitter_is_bounded_and_preserves_shape():
    oldcam_v8 = load_module(ROOT / "oldcam-v8" / "oldcam.py", "oldcam_v8_ois")
    image = np.full((24, 24, 3), 127, dtype=np.uint8)
    state = {}
    rng = np.random.default_rng(1)

    processed = oldcam_v8.apply_ois_jitter(image, state, rng)

    assert processed.shape == image.shape
    assert abs(state["ois_x"]) <= 2.0
    assert abs(state["ois_y"]) <= 2.0
    assert "ois_vx" in state
    assert "ois_vy" in state


def test_v8_chroma_noise_changes_channels_independently_in_shadows():
    oldcam_v8 = load_module(ROOT / "oldcam-v8" / "oldcam.py", "oldcam_v8_noise")
    image = np.full((32, 32, 3), 24, dtype=np.uint8)
    rng = np.random.default_rng(2)

    processed = oldcam_v8.apply_organic_sensor_noise(image, grain=8, rng=rng)
    channel_deltas = [
        processed[:, :, channel].astype(np.int16) - image[:, :, channel].astype(np.int16)
        for channel in range(3)
    ]

    assert processed.shape == image.shape
    assert not np.array_equal(channel_deltas[0], channel_deltas[1])
    assert not np.array_equal(channel_deltas[1], channel_deltas[2])


def test_v8_process_frame_does_not_apply_per_frame_jpeg():
    oldcam_v8 = load_module(ROOT / "oldcam-v8" / "oldcam.py", "oldcam_v8_stack")
    image = np.full((24, 24, 3), 127, dtype=np.uint8)
    args = SimpleNamespace(sharpen=0.8, saturation=1.12, grain=1, quality=94)
    lut = oldcam_v8.create_iphone_lut()
    vignette = oldcam_v8.create_vignette_mask(24, 24)

    with mock.patch.object(oldcam_v8, "apply_jpeg_pass", side_effect=AssertionError("JPEG pass called")):
        processed = oldcam_v8.process_frame(image, lut, vignette, args, np.random.default_rng(3), {})

    assert processed.shape == image.shape


def test_oldcam_rerun_only_processes_existing_video(tmp_path):
    source = tmp_path / "clip.mp4"
    source.write_bytes(b"video")
    expected = tmp_path / "clip-oldcam-v7.mp4"
    manager, _ = make_queue_manager(
        {"oldcam_version": "v7", "allow_reprocess": True, "reprocess_mode": "overwrite"}
    )

    done = threading.Event()
    result = {}

    def callback(success, src, output, error):
        result.update(
            {"success": success, "src": src, "output": output, "error": error}
        )
        done.set()

    with mock.patch.object(manager, "_oldcam_video") as oldcam_mock:
        def _run_oldcam(path, _item):
            expected.write_bytes(b"done")
            return str(expected)

        oldcam_mock.side_effect = _run_oldcam
        started = manager.rerun_oldcam_only(str(source), completion_callback=callback)
        assert started is True
        assert done.wait(2)

    assert result["success"] is True
    assert result["src"] == str(source.resolve())
    assert result["output"] == str(expected)


def test_oldcam_rerun_respects_reprocess_disabled_when_same_version_output_exists(tmp_path):
    source = tmp_path / "clip.mp4"
    source.write_bytes(b"video")
    existing = tmp_path / "clip-oldcam-v8.mp4"
    existing.write_bytes(b"existing")
    manager, _ = make_queue_manager(
        {"oldcam_version": "v8", "allow_reprocess": False, "reprocess_mode": "increment"}
    )

    done = threading.Event()
    result = {}

    def callback(success, src, output, error):
        result.update(
            {"success": success, "src": src, "output": output, "error": error}
        )
        done.set()

    with mock.patch.object(manager, "_oldcam_video") as oldcam_mock:
        started = manager.rerun_oldcam_only(str(source), completion_callback=callback)
        assert started is True
        assert done.wait(2)
        oldcam_mock.assert_not_called()

    assert result["success"] is False
    assert "Enable 'Allow reprocessing'" in (result["error"] or "")


def test_oldcam_rerun_increment_mode_creates_versioned_comparison_output(tmp_path):
    source = tmp_path / "clip_looped.mp4"
    source.write_bytes(b"video")
    existing = tmp_path / "clip_looped-oldcam-v7.mp4"
    existing.write_bytes(b"existing")

    manager, _ = make_queue_manager(
        {"oldcam_version": "v7", "allow_reprocess": True, "reprocess_mode": "increment"}
    )

    done = threading.Event()
    result = {}

    def callback(success, src, output, error):
        result.update(
            {"success": success, "src": src, "output": output, "error": error}
        )
        done.set()

    with mock.patch.object(manager, "_oldcam_video") as oldcam_mock:
        def _run_oldcam(path, _item):
            generated = manager._build_oldcam_output_path(Path(path), "v7")
            generated.write_bytes(b"done")
            return str(generated)

        oldcam_mock.side_effect = _run_oldcam
        started = manager.rerun_oldcam_only(str(source), completion_callback=callback)
        assert started is True
        assert done.wait(2)
        manager._oldcam_rerun_thread.join(timeout=2)

    assert result["success"] is True
    assert result["output"].endswith("clip_looped_2-oldcam-v7.mp4")


def test_oldcam_rerun_fails_when_no_versions_selected(tmp_path):
    source = tmp_path / "clip.mp4"
    source.write_bytes(b"video")
    manager, _ = make_queue_manager({"oldcam_versions": []})

    done = threading.Event()
    result = {}

    def callback(success, src, output, error):
        result.update(
            {"success": success, "src": src, "output": output, "error": error}
        )
        done.set()

    started = manager.rerun_oldcam_only(str(source), completion_callback=callback)
    assert started is True
    assert done.wait(2)
    assert result["success"] is False
    assert result["error"] == "Oldcam not selected."


def test_v10_process_frame_skips_spatial_fluctuation_when_face_not_detected():
    oldcam_v10 = load_module(ROOT / "oldcam-v10" / "oldcam.py", "oldcam_v10_gate")
    image = np.full((24, 24, 3), 127, dtype=np.uint8)
    args = SimpleNamespace(sharpen=0.8, saturation=1.02, grain=1.0, quality=94, vignette_strength=0.55)
    vignette = oldcam_v10.create_vignette_mask(24, 24)
    state = {"face_detected": False, "full_face_mask": np.zeros((24, 24, 3), dtype=np.float32)}

    with mock.patch.object(oldcam_v10, "get_dynamic_region_masks", return_value={}), \
        mock.patch.object(
            oldcam_v10,
            "apply_synchronized_spatial_fluctuation",
            side_effect=AssertionError("spatial fluctuation should be skipped"),
        ):
        processed = oldcam_v10.process_frame(
            image,
            oldcam_v10.create_neutral_phone_lut(),
            vignette,
            args,
            np.random.default_rng(4),
            state,
        )

    assert processed.shape == image.shape


def test_oldcam_ui_uses_version_checkboxes_without_master_toggle():
    panel_source = (ROOT / "kling_gui" / "config_panel.py").read_text(encoding="utf-8")
    assert 'text="Oldcam Finish"' not in panel_source
    assert 'text="Oldcam:"' in panel_source
    assert 'for i, version in enumerate(("v7", "v8", "v9", "v10", "v11", "v12", "v13", "v14", "v15"))' in panel_source


def test_oldcam_dependency_preflight_requires_mediapipe_for_v10(tmp_path):
    manager, _ = make_queue_manager({})
    oldcam_dir = tmp_path / "oldcam-v10"
    oldcam_dir.mkdir()
    (oldcam_dir / "requirements.txt").write_text("mediapipe==0.10.35\n", encoding="utf-8")

    real_import = builtins.__import__

    def fake_import(name, *args, **kwargs):
        if name == "mediapipe":
            raise ImportError("No module named mediapipe")
        return real_import(name, *args, **kwargs)

    with mock.patch("builtins.__import__", side_effect=fake_import):
        assert manager._ensure_oldcam_dependencies(oldcam_dir, "v10") is False


def test_oldcam_dependency_preflight_v7_does_not_require_mediapipe(tmp_path):
    manager, _ = make_queue_manager({})
    oldcam_dir = tmp_path / "oldcam-v7"
    oldcam_dir.mkdir()
    (oldcam_dir / "requirements.txt").write_text("numpy\nopencv-python-headless\n", encoding="utf-8")

    real_import = builtins.__import__

    def fake_import(name, *args, **kwargs):
        if name == "mediapipe":
            raise ImportError("No module named mediapipe")
        return real_import(name, *args, **kwargs)

    with mock.patch("builtins.__import__", side_effect=fake_import):
        assert manager._ensure_oldcam_dependencies(oldcam_dir, "v7") is True


def test_oldcam_dependency_preflight_retries_after_missing_dependency(tmp_path):
    manager, _ = make_queue_manager({})
    oldcam_dir = tmp_path / "oldcam-v10"
    oldcam_dir.mkdir()
    (oldcam_dir / "face_landmarker.task").write_bytes(b"x")
    (oldcam_dir / "requirements.txt").write_text("mediapipe==0.10.35\n", encoding="utf-8")

    real_import = builtins.__import__
    missing = {"enabled": True}

    def fake_import(name, *args, **kwargs):
        if name == "mediapipe" and missing["enabled"]:
            raise ImportError("No module named mediapipe")
        if name == "mediapipe":
            return types.SimpleNamespace(
                __file__="site-packages/mediapipe/__init__.py",
                __version__="0.10.35",
            )
        if name == "mediapipe.tasks.python":
            return types.SimpleNamespace(vision=types.SimpleNamespace(FaceLandmarker=object))
        return real_import(name, *args, **kwargs)

    with mock.patch("builtins.__import__", side_effect=fake_import):
        assert manager._ensure_oldcam_dependencies(oldcam_dir, "v10") is False
        missing["enabled"] = False
        assert manager._ensure_oldcam_dependencies(oldcam_dir, "v10") is True


def test_v10_apply_modern_sensor_noise_accepts_3d_fpn_mask():
    oldcam_v10 = load_module(ROOT / "oldcam-v10" / "oldcam.py", "oldcam_v10_fpn")
    image = np.full((20, 20, 3), 64, dtype=np.uint8)
    rng = np.random.default_rng(7)
    fpn = np.zeros((20, 20, 3), dtype=np.float32)
    fpn[:, :, 0] = 0.5
    fpn[:, :, 1] = 0.25
    fpn[:, :, 2] = -0.25
    processed = oldcam_v10.apply_modern_sensor_noise(image, grain=1.0, rng=rng, state={}, fpn_mask=fpn)
    assert processed.shape == image.shape


def test_v9_apply_modern_sensor_noise_accepts_3d_fpn_mask():
    oldcam_v9 = load_module(ROOT / "oldcam-v9" / "oldcam.py", "oldcam_v9_fpn")
    image = np.full((20, 20, 3), 64, dtype=np.uint8)
    rng = np.random.default_rng(8)
    fpn = np.zeros((20, 20, 3), dtype=np.float32)
    fpn[:, :, 0] = 0.4
    fpn[:, :, 1] = 0.2
    fpn[:, :, 2] = -0.3
    processed = oldcam_v9.apply_modern_sensor_noise(image, grain=1.0, rng=rng, state={}, fpn_mask=fpn)
    assert processed.shape == image.shape


def test_validate_mediapipe_tasks_api_missing_facelandmarker_fails(tmp_path, monkeypatch):
    manager, _ = make_queue_manager({})
    # If mediapipe.tasks.python is already cached in sys.modules (e.g. another
    # test imported it), Python skips __import__ and the mock never fires —
    # evict it so the patched importer below actually decides what gets returned.
    for cached in ("mediapipe", "mediapipe.tasks", "mediapipe.tasks.python", "mediapipe.tasks.python.vision"):
        monkeypatch.delitem(sys.modules, cached, raising=False)
    real_import = builtins.__import__
    fake_mp = types.SimpleNamespace(__file__="x/mediapipe.py", __version__="0")

    def fake_import(name, *a, **k):
        if name == "mediapipe":
            return fake_mp
        if name.startswith("mediapipe."):
            raise ImportError(f"mocked: {name} unavailable")
        return real_import(name, *a, **k)

    with mock.patch("builtins.__import__", side_effect=fake_import):
        oldcam_dir = tmp_path / "oldcam-v10"
        oldcam_dir.mkdir()
        ok, diagnostics = manager._validate_mediapipe_tasks_api(oldcam_dir)
    assert ok is False
    assert diagnostics["facelandmarker_import_ok"] == "False"


def test_validate_mediapipe_tasks_api_missing_task_file_fails(tmp_path):
    manager, _ = make_queue_manager({})
    oldcam_dir = tmp_path / "oldcam-v10"
    oldcam_dir.mkdir()
    real_import = builtins.__import__
    fake_mp = types.SimpleNamespace(__file__="site-packages/mediapipe/__init__.py", __version__="0.10.35")

    def fake_import(name, *a, **k):
        if name == "mediapipe":
            return fake_mp
        if name == "mediapipe.tasks.python":
            return types.SimpleNamespace(vision=types.SimpleNamespace(FaceLandmarker=object))
        return real_import(name, *a, **k)

    with mock.patch("builtins.__import__", side_effect=fake_import), \
         mock.patch.object(manager, "_resolve_face_landmarker_task_path", return_value=(None, [])):
        ok, diagnostics = manager._validate_mediapipe_tasks_api(oldcam_dir)
    assert ok is False
    assert diagnostics["task_file_exists"] == "False"


def test_validate_mediapipe_tasks_api_valid_chain_passes(tmp_path):
    manager, _ = make_queue_manager({})
    real_import = builtins.__import__
    oldcam_dir = tmp_path / "oldcam-v10"
    oldcam_dir.mkdir()
    task = oldcam_dir / "face_landmarker.task"
    task.write_bytes(b"x")

    fake_mp = types.SimpleNamespace(
        __file__="site-packages/mediapipe/__init__.py",
        __version__="0.10.35",
    )
    fake_vision = types.SimpleNamespace(FaceLandmarker=object)

    real_import = builtins.__import__

    def fake_import(name, *a, **k):
        if name == "mediapipe":
            return fake_mp
        if name == "mediapipe.tasks.python":
            return types.SimpleNamespace(vision=fake_vision)
        return real_import(name, *a, **k)

    with mock.patch("builtins.__import__", side_effect=fake_import):
        ok, diagnostics = manager._validate_mediapipe_tasks_api(oldcam_dir)
    assert ok is True
    assert diagnostics["facelandmarker_import_ok"] == "True"
    assert diagnostics["task_file_exists"] == "True"


def test_v10_temporal_noise_uses_distinct_state_keys():
    oldcam_v10 = load_module(ROOT / "oldcam-v10" / "oldcam.py", "oldcam_v10_temporal_keys")
    image = np.full((18, 18, 3), 72, dtype=np.uint8)
    state = {}
    _ = oldcam_v10.apply_modern_sensor_noise(image, grain=1.0, rng=np.random.default_rng(11), state=state, fpn_mask=None)
    assert "temporal_noise_luma" in state
    assert "temporal_noise_chroma" in state
    assert "temporal_noise" not in state


def test_v9_temporal_noise_uses_distinct_state_keys():
    oldcam_v9 = load_module(ROOT / "oldcam-v9" / "oldcam.py", "oldcam_v9_temporal_keys")
    image = np.full((18, 18, 3), 72, dtype=np.uint8)
    state = {}
    _ = oldcam_v9.apply_modern_sensor_noise(image, grain=1.0, rng=np.random.default_rng(13), state=state, fpn_mask=None)
    assert "temporal_noise_luma" in state
    assert "temporal_noise_chroma" in state
    assert "temporal_noise" not in state


def test_v11_default_output_path_uses_v11_suffix():
    oldcam_v11 = load_module(ROOT / "oldcam-v11" / "oldcam.py", "oldcam_v11")
    assert oldcam_v11.build_default_output_path("sample.mp4").endswith("sample-oldcam-v11.mp4")


def test_oldcam_dependency_preflight_requires_mediapipe_for_v11(tmp_path):
    manager, _ = make_queue_manager({})
    oldcam_dir = tmp_path / "oldcam-v11"
    oldcam_dir.mkdir()
    (oldcam_dir / "requirements.txt").write_text("mediapipe==0.10.35\n", encoding="utf-8")

    real_import = builtins.__import__

    def fake_import(name, *args, **kwargs):
        if name == "mediapipe":
            raise ImportError("No module named mediapipe")
        return real_import(name, *args, **kwargs)

    with mock.patch("builtins.__import__", side_effect=fake_import):
        assert manager._ensure_oldcam_dependencies(oldcam_dir, "v11") is False


def test_v11_process_frame_applies_awb_drift_after_spatial_fluctuation():
    oldcam_v11 = load_module(ROOT / "oldcam-v11" / "oldcam.py", "oldcam_v11_order")
    call_order = []

    def fake_spatial(image, state, region_masks, target_hz, fps=30.0):
        call_order.append("spatial")
        return image

    def fake_awb(image, state, rng):
        call_order.append("awb")
        return image

    image = np.full((32, 32, 3), 128, dtype=np.uint8)
    state = {
        "face_detected": True,
        "full_face_mask": np.zeros((32, 32, 3), dtype=np.float32),
        "last_masks": {"forehead": np.zeros((32, 32, 3), dtype=np.float32)},
        "fpn": np.zeros((32, 32, 3), dtype=np.float32),
        "adjusted_vignette_mask": np.ones((32, 32, 1), dtype=np.float32),
        "target_hz": 1.2,
        "detect_frame_count": 2,
    }

    import argparse
    args = argparse.Namespace(grain=1, saturation=1.02, ghosting=0.08, vignette_strength=0.55)

    with (
        mock.patch.object(oldcam_v11, "apply_synchronized_spatial_fluctuation", side_effect=fake_spatial),
        mock.patch.object(oldcam_v11, "apply_global_awb_drift", side_effect=fake_awb),
        mock.patch.object(oldcam_v11, "get_dynamic_region_masks", return_value=state["last_masks"]),
        mock.patch.object(oldcam_v11, "synchronize_base_frequency", return_value=1.2),
    ):
        oldcam_v11.process_frame(image, None, None, args, rng=np.random.default_rng(42), state=state)

    assert "spatial" in call_order, "apply_synchronized_spatial_fluctuation was not called"
    assert "awb" in call_order, "apply_global_awb_drift was not called"
    assert call_order.index("spatial") < call_order.index("awb"), (
        "AWB drift must be applied AFTER spatial fluctuation (signal ordering invariant)"
    )


def test_v12_default_output_path_uses_v12_suffix():
    oldcam_v12 = load_module(ROOT / "oldcam-v12" / "oldcam.py", "oldcam_v12")
    assert oldcam_v12.build_default_output_path("sample.mp4").endswith("sample-oldcam-v12.mp4")


def test_oldcam_dependency_preflight_does_not_require_mediapipe_for_v12(tmp_path):
    """V12 is pristine hardware-only — does not call MediaPipe, so missing mediapipe is OK."""
    manager, _ = make_queue_manager({})
    oldcam_dir = tmp_path / "oldcam-v12"
    oldcam_dir.mkdir()
    (oldcam_dir / "requirements.txt").write_text("numpy>=1.24\nopencv-python-headless>=4.8\n", encoding="utf-8")

    real_import = builtins.__import__

    def fake_import(name, *args, **kwargs):
        if name == "mediapipe":
            raise ImportError("No module named mediapipe")
        return real_import(name, *args, **kwargs)

    with mock.patch("builtins.__import__", side_effect=fake_import):
        # Should succeed (returns True) because v12 does not require mediapipe
        assert manager._ensure_oldcam_dependencies(oldcam_dir, "v12") is True


def test_v12_process_frame_skips_rppg_lut_and_tone_mapping():
    """V12 pristine pipeline: no rPPG, no LUT, no dynamic tone mapping."""
    oldcam_v12 = load_module(ROOT / "oldcam-v12" / "oldcam.py", "oldcam_v12_pristine")

    # rPPG functions must be removed entirely from V12.
    assert not hasattr(oldcam_v12, "synchronize_base_frequency"), (
        "V12 must not define synchronize_base_frequency (rPPG removed)"
    )
    assert not hasattr(oldcam_v12, "apply_synchronized_spatial_fluctuation"), (
        "V12 must not define apply_synchronized_spatial_fluctuation (rPPG removed)"
    )

    # process_frame must not call the destructive color filters.
    called = []

    def mark(name):
        def _wrapped(*args, **kwargs):
            called.append(name)
            # Most filters return the image unchanged here; vignette and noise
            # paths just need to not crash. We pass through the first positional arg.
            return args[0]
        return _wrapped

    image = np.full((32, 32, 3), 128, dtype=np.uint8)
    state = {
        "face_detected": False,
        "fpn": np.zeros((32, 32, 3), dtype=np.float32),
        "adjusted_vignette_mask": np.ones((32, 32, 1), dtype=np.float32),
        "last_masks": {},
    }

    import argparse
    args = argparse.Namespace(grain=1, vignette_strength=0.55)

    # V12 should NOT call get_dynamic_region_masks (MediaPipe inference)
    # because nothing downstream consumes the result — that was wasted compute.
    with (
        mock.patch.object(oldcam_v12, "get_dynamic_region_masks", side_effect=mark("mediapipe")),
        mock.patch.object(oldcam_v12, "apply_dynamic_tone_mapping", side_effect=mark("tone")),
        mock.patch.object(oldcam_v12, "create_neutral_phone_lut", side_effect=mark("lut")),
    ):
        oldcam_v12.process_frame(image, None, None, args, rng=np.random.default_rng(0), state=state)

    assert "tone" not in called, "V12 must not call apply_dynamic_tone_mapping"
    assert "lut" not in called, "V12 must not call create_neutral_phone_lut from process_frame"
    assert "mediapipe" not in called, "V12 must not call get_dynamic_region_masks (wasted compute)"


def test_v13_default_output_path_uses_v13_suffix():
    oldcam_v13 = load_module(ROOT / "oldcam-v13" / "oldcam.py", "oldcam_v13")
    assert oldcam_v13.build_default_output_path("sample.mp4").endswith("sample-oldcam-v13.mp4")


def test_oldcam_dependency_preflight_does_not_require_mediapipe_for_v13(tmp_path):
    """V13 High-End Daylight — like V12, no MediaPipe; missing mediapipe must be OK."""
    manager, _ = make_queue_manager({})
    oldcam_dir = tmp_path / "oldcam-v13"
    oldcam_dir.mkdir()
    (oldcam_dir / "requirements.txt").write_text("numpy>=1.24\nopencv-python-headless>=4.8\n", encoding="utf-8")

    real_import = builtins.__import__

    def fake_import(name, *args, **kwargs):
        if name == "mediapipe":
            raise ImportError("No module named mediapipe")
        return real_import(name, *args, **kwargs)

    with mock.patch("builtins.__import__", side_effect=fake_import):
        assert manager._ensure_oldcam_dependencies(oldcam_dir, "v13") is True


def test_v13_process_frame_skips_noise_and_ae_stepping():
    """V13 must not call apply_modern_sensor_noise or apply_ae_stepping in process_frame."""
    oldcam_v13 = load_module(ROOT / "oldcam-v13" / "oldcam.py", "oldcam_v13_daylight")

    called = []

    def mark(name):
        def _wrapped(*args, **kwargs):
            called.append(name)
            return args[0]
        return _wrapped

    image = np.full((32, 32, 3), 128, dtype=np.uint8)
    state = {
        "face_detected": False,
        "fpn": np.zeros((32, 32, 3), dtype=np.float32),
        "adjusted_vignette_mask": np.ones((32, 32, 1), dtype=np.float32),
        "last_masks": {},
    }

    import argparse
    args = argparse.Namespace(vignette_strength=0.55)

    with (
        mock.patch.object(oldcam_v13, "apply_modern_sensor_noise", side_effect=mark("noise")),
        mock.patch.object(oldcam_v13, "apply_ae_stepping", side_effect=mark("ae")),
    ):
        oldcam_v13.process_frame(image, None, None, args, rng=np.random.default_rng(0), state=state)

    assert "noise" not in called, "V13 must not call apply_modern_sensor_noise (pristine daylight)"
    assert "ae" not in called, "V13 must not call apply_ae_stepping (stable daylight assumption)"


def test_v13_naturalize_image_does_not_reference_args_grain(tmp_path):
    """Regression test: removing --grain from V13 parser left args.grain reads in
    naturalize_image / naturalize_video state init, which would AttributeError
    when the CLI parser path (not test-mocked Namespace) was used. Caught by
    Gemini on PR #18.

    This test exercises the actual parser → naturalize_image path with a
    tiny synthetic image to ensure no AttributeError.
    """
    import cv2
    oldcam_v13 = load_module(ROOT / "oldcam-v13" / "oldcam.py", "oldcam_v13_cli_path")

    # Build parser as the real CLI does, parse with only an input path
    src_img = tmp_path / "tiny.png"
    cv2.imwrite(str(src_img), np.full((16, 16, 3), 128, dtype=np.uint8))
    out_img = tmp_path / "out.png"

    parser = oldcam_v13.build_parser()
    args = parser.parse_args([str(src_img)])

    # If args.grain is read anywhere, this raises AttributeError.
    # We're not asserting visual output, just that the call doesn't crash.
    oldcam_v13.naturalize_image(str(src_img), str(out_img), args)
    assert out_img.exists(), "V13 naturalize_image did not produce output"


def test_v13_parser_does_not_expose_grain_arg():
    """V13 parser must not accept --grain (it's dead in the daylight pipeline)."""
    import argparse
    oldcam_v13 = load_module(ROOT / "oldcam-v13" / "oldcam.py", "oldcam_v13_parser_check")
    parser = oldcam_v13.build_parser()
    with pytest.raises(SystemExit):
        # argparse exits with non-zero on unknown args
        parser.parse_args(["dummy.mp4", "--grain", "5"])


# ---------------------------------------------------------------------------
# Re-Run loop wiring (PR #18 follow-up): rerun_oldcam_only() must honor the
# loop_videos config the same way the normal queue path does.
# ---------------------------------------------------------------------------


def test_rerun_oldcam_loop_enabled_re_loops_source_before_oldcam(tmp_path):
    """When loop_videos=True, rerun_oldcam_only must call _loop_video and
    pass the looped path to _oldcam_video (matching normal-queue behavior)."""
    source = tmp_path / "clip.mp4"
    source.write_bytes(b"video")
    looped = tmp_path / "clip_looped.mp4"
    looped.write_bytes(b"looped-video")

    manager, _ = make_queue_manager(
        {
            "oldcam_version": "v13",
            "loop_videos": True,
            "allow_reprocess": True,
            "reprocess_mode": "overwrite",
        }
    )

    done = threading.Event()
    result = {}

    def callback(success, src, output, error):
        result.update({"success": success, "src": src, "output": output, "error": error})
        done.set()

    captured_inputs = []

    def _fake_oldcam(path, _item):
        captured_inputs.append(path)
        expected = manager._build_oldcam_output_path(Path(path), "v13")
        expected.write_bytes(b"done")
        return str(expected)

    with mock.patch.object(manager, "_loop_video", return_value=str(looped)) as loop_mock, \
        mock.patch.object(manager, "_oldcam_video", side_effect=_fake_oldcam):
        started = manager.rerun_oldcam_only(str(source), completion_callback=callback)
        assert started is True
        assert done.wait(2)
        manager._oldcam_rerun_thread.join(timeout=2)

        loop_mock.assert_called_once()
        # _loop_video was called with the un-looped source path string
        args, _ = loop_mock.call_args
        assert args[0] == str(source.resolve())

    assert result["success"] is True
    # The captured Oldcam input must be the looped path (not the un-looped source)
    assert len(captured_inputs) == 1
    assert captured_inputs[0] == str(looped.resolve())
    # Output filename inherits the _looped stem
    assert "_looped-oldcam-v13" in result["output"]


def test_rerun_oldcam_loop_disabled_skips_loop_step(tmp_path):
    """When loop_videos=False, rerun_oldcam_only must NOT call _loop_video
    and must pass the un-looped source straight to _oldcam_video."""
    source = tmp_path / "clip.mp4"
    source.write_bytes(b"video")

    manager, _ = make_queue_manager(
        {
            "oldcam_version": "v13",
            "loop_videos": False,
            "allow_reprocess": True,
            "reprocess_mode": "overwrite",
        }
    )

    done = threading.Event()
    result = {}

    def callback(success, src, output, error):
        result.update({"success": success, "src": src, "output": output, "error": error})
        done.set()

    captured_inputs = []

    def _fake_oldcam(path, _item):
        captured_inputs.append(path)
        expected = manager._build_oldcam_output_path(Path(path), "v13")
        expected.write_bytes(b"done")
        return str(expected)

    with mock.patch.object(manager, "_loop_video") as loop_mock, \
        mock.patch.object(manager, "_oldcam_video", side_effect=_fake_oldcam):
        started = manager.rerun_oldcam_only(str(source), completion_callback=callback)
        assert started is True
        assert done.wait(2)
        manager._oldcam_rerun_thread.join(timeout=2)

        loop_mock.assert_not_called()

    assert result["success"] is True
    assert captured_inputs == [str(source.resolve())]


def test_rerun_oldcam_loop_enabled_skips_when_source_already_looped(tmp_path):
    """If the user picks a _looped source while loop_videos=True, skip the
    loop step so we don't produce ..._looped_looped.mp4."""
    source = tmp_path / "clip_looped.mp4"
    source.write_bytes(b"video")

    manager, logs = make_queue_manager(
        {
            "oldcam_version": "v13",
            "loop_videos": True,
            "allow_reprocess": True,
            "reprocess_mode": "overwrite",
        }
    )

    done = threading.Event()
    result = {}

    def callback(success, src, output, error):
        result.update({"success": success, "src": src, "output": output, "error": error})
        done.set()

    captured_inputs = []

    def _fake_oldcam(path, _item):
        captured_inputs.append(path)
        expected = manager._build_oldcam_output_path(Path(path), "v13")
        expected.write_bytes(b"done")
        return str(expected)

    with mock.patch.object(manager, "_loop_video") as loop_mock, \
        mock.patch.object(manager, "_oldcam_video", side_effect=_fake_oldcam):
        started = manager.rerun_oldcam_only(str(source), completion_callback=callback)
        assert started is True
        assert done.wait(2)
        manager._oldcam_rerun_thread.join(timeout=2)

        loop_mock.assert_not_called()

    assert result["success"] is True
    assert captured_inputs == [str(source.resolve())]
    # A user-visible log line explains the skip
    assert any("already looped" in message for message, _level in logs)


def test_rerun_oldcam_loop_failure_falls_back_to_unlooped_source(tmp_path):
    """If _loop_video returns None, log a warning and run Oldcam on the
    original un-looped source rather than aborting the rerun."""
    source = tmp_path / "clip.mp4"
    source.write_bytes(b"video")

    manager, logs = make_queue_manager(
        {
            "oldcam_version": "v13",
            "loop_videos": True,
            "allow_reprocess": True,
            "reprocess_mode": "overwrite",
        }
    )

    done = threading.Event()
    result = {}

    def callback(success, src, output, error):
        result.update({"success": success, "src": src, "output": output, "error": error})
        done.set()

    captured_inputs = []

    def _fake_oldcam(path, _item):
        captured_inputs.append(path)
        expected = manager._build_oldcam_output_path(Path(path), "v13")
        expected.write_bytes(b"done")
        return str(expected)

    with mock.patch.object(manager, "_loop_video", return_value=None) as loop_mock, \
        mock.patch.object(manager, "_oldcam_video", side_effect=_fake_oldcam):
        started = manager.rerun_oldcam_only(str(source), completion_callback=callback)
        assert started is True
        assert done.wait(2)
        manager._oldcam_rerun_thread.join(timeout=2)

        loop_mock.assert_called_once()

    assert result["success"] is True
    # Fell back to the un-looped source
    assert captured_inputs == [str(source.resolve())]
    # Warning logged so the user knows the loop step didn't run
    assert any(
        level == "warning" and "loop step failed" in message
        for message, level in logs
    )


# ---------------------------------------------------------------------------
# Logging UX: panel-vs-file separation
# ---------------------------------------------------------------------------


def test_is_panel_noise_matches_known_subprocess_noise():
    """Verbose subprocess lines that already have a friendlier panel
    equivalent are routed to file-only ("debug") via _is_panel_noise."""
    from kling_gui.queue_manager import _is_panel_noise

    # Substring-match, case-insensitive, mirrors _TF_NOISE_PATTERNS contract.
    assert _is_panel_noise("Input : F:/Downloads/clip.mp4")
    assert _is_panel_noise("Input: F:/Downloads/clip.mp4")
    assert _is_panel_noise("Output: F:/Downloads/clip-oldcam-v13.mp4")
    assert _is_panel_noise("Saved video to: F:/Downloads/clip-oldcam-v13.mp4")
    assert _is_panel_noise("Video processing complete.")
    assert _is_panel_noise("Finalizing video with FFmpeg codec: h264")

    # Friendly lines that the user SHOULD see in the panel must NOT match.
    assert not _is_panel_noise("[Oldcam] Processing: 50% complete...")
    assert not _is_panel_noise("Oldcam v13 Finish applied: clip-oldcam-v13.mp4")
    assert not _is_panel_noise("Applying Oldcam v13 Finish...")
    assert not _is_panel_noise("")


def test_rerun_oldcam_summary_demoted_to_debug_level(tmp_path):
    """The rerun summary duplicates the _oldcam_video 'Oldcam summary:' line.
    It must be logged at "debug" level so it stays in the file log but
    doesn't reappear in the user-facing panel."""
    source = tmp_path / "clip.mp4"
    source.write_bytes(b"video")

    manager, logs = make_queue_manager(
        {
            "oldcam_version": "v13",
            "loop_videos": False,
            "allow_reprocess": True,
            "reprocess_mode": "overwrite",
        }
    )

    done = threading.Event()

    def callback(success, src, output, error):
        done.set()

    def _fake_oldcam(path, _item):
        expected = manager._build_oldcam_output_path(Path(path), "v13")
        expected.write_bytes(b"done")
        # Populate the summary state that the rerun worker reads.
        manager._last_oldcam_run_summary = {
            "requested_versions": ["v13"],
            "succeeded_versions": ["v13"],
            "failed_versions": [],
            "primary_output": str(expected),
        }
        return str(expected)

    with mock.patch.object(manager, "_oldcam_video", side_effect=_fake_oldcam):
        started = manager.rerun_oldcam_only(str(source), completion_callback=callback)
        assert started is True
        assert done.wait(2)
        manager._oldcam_rerun_thread.join(timeout=2)

    # The "Oldcam-only rerun summary:" line exists, but only at debug level.
    rerun_summary_logs = [
        (message, level) for message, level in logs
        if "Oldcam-only rerun summary:" in message
    ]
    assert len(rerun_summary_logs) >= 1, "Expected the rerun summary to be logged"
    for message, level in rerun_summary_logs:
        assert level == "debug", (
            f"Rerun summary must be at 'debug' (file-only) to avoid duplicating "
            f"the 'Oldcam summary:' panel line; got level={level!r} for: {message}"
        )

    # The basename-only "Oldcam-only rerun complete: <name>" emit must also be
    # debug, since main_window's completion callback emits the friendlier
    # "<src> → <output>" message for the panel.
    rerun_complete_logs = [
        (message, level) for message, level in logs
        if message.startswith("Oldcam-only rerun complete:")
    ]
    for message, level in rerun_complete_logs:
        assert level == "debug", (
            f"Basename-only 'rerun complete' must be debug; main_window emits "
            f"the user-facing arrow form. Got level={level!r} for: {message}"
        )


def test_oldcam_summary_and_selected_lines_demoted_to_debug(tmp_path):
    """Inside _oldcam_video the structured 'Oldcam selected: running ...'
    and 'Oldcam summary:' lines are now debug-level to avoid duplicating
    the per-version 'Oldcam vN Finish applied: ...' panel messages."""
    source = tmp_path / "clip_looped.mp4"
    source.write_bytes(b"video")

    manager, logs = make_queue_manager(
        {
            "oldcam_version": "v13",
            "allow_reprocess": True,
            "reprocess_mode": "overwrite",
        }
    )

    # Stub _run_oldcam_version so _oldcam_video runs end-to-end without
    # spawning a real Oldcam subprocess.
    def _fake_run(_path, version):
        produced = manager._build_oldcam_output_path(Path(_path), version)
        produced.write_bytes(b"done")
        return str(produced)

    with mock.patch.object(manager, "_run_oldcam_version", side_effect=_fake_run):
        result = manager._oldcam_video(str(source), QueueItem(str(source)))

    assert result is not None and result.endswith(".mp4")

    # "Oldcam selected: running v13" -> must be debug now.
    selected_logs = [
        (msg, lvl) for msg, lvl in logs if msg.startswith("Oldcam selected: running")
    ]
    assert selected_logs, "Expected at least one 'Oldcam selected: running' emit"
    for msg, lvl in selected_logs:
        assert lvl == "debug", (
            f"'Oldcam selected: running ...' must be debug (file-only); the "
            f"per-version 'Applying' / 'Finish applied' lines convey the same "
            f"info to the panel. Got level={lvl!r} for: {msg}"
        )

    # "Oldcam summary: ..." -> must also be debug (the success summary).
    summary_logs = [
        (msg, lvl) for msg, lvl in logs if msg.startswith("Oldcam summary: requested versions=")
    ]
    assert summary_logs, "Expected at least one 'Oldcam summary:' emit"
    for msg, lvl in summary_logs:
        assert lvl == "debug", (
            f"'Oldcam summary:' must be debug (file-only); per-version 'Finish "
            f"applied' lines + main_window's final 'rerun complete' arrow line "
            f"cover the user-facing summary. Got level={lvl!r} for: {msg}"
        )


def test_loop_video_wrapper_emits_saved_line_at_debug_level(tmp_path):
    """QueueManager._loop_video should NOT duplicate the looper's
    'Looped video saved: <name> (X.Y MB)' panel success line; its own
    basename-only 'Looped video saved: <name>' emit must be debug."""
    source = tmp_path / "clip.mp4"
    source.write_bytes(b"video")

    manager, logs = make_queue_manager({"loop_videos": True})

    # Mock the looper to return a sentinel path without invoking ffmpeg.
    fake_looped = tmp_path / "clip_looped.mp4"
    fake_looped.write_bytes(b"loop-output")

    with mock.patch(
        "kling_gui.video_looper.create_looped_video",
        return_value=str(fake_looped),
    ):
        result = manager._loop_video(str(source), QueueItem(str(source)))

    assert result == str(fake_looped)

    saved_logs = [
        (msg, lvl) for msg, lvl in logs if msg.startswith("Looped video saved:")
    ]
    # The wrapper emits its own "Looped video saved: <name>" event; verify
    # the wrapper's emit is debug-level (file only). The looper itself
    # emits the panel-facing success line with file size and is not under
    # test here.
    assert saved_logs, "Expected the wrapper to emit a 'Looped video saved' event"
    for msg, lvl in saved_logs:
        assert lvl == "debug", (
            f"Wrapper's 'Looped video saved: <name>' must be debug to avoid "
            f"duplicating the looper's '... (X.Y MB)' panel success line. "
            f"Got level={lvl!r} for: {msg}"
        )


# ---------------------------------------------------------------------------
# Verbose Mode checkbox: when enabled, "debug" lines also appear in the panel.
# ---------------------------------------------------------------------------


def test_verbose_mode_routing_logic_present_in_log_method():
    """`KlingGUIWindow._log` must read `verbose_gui_mode` from config when
    deciding whether to surface a 'debug' line in the panel."""
    from kling_gui.main_window import KlingGUIWindow
    import inspect

    src = inspect.getsource(KlingGUIWindow._log)

    # The method must consult the verbose_gui_mode config to decide whether
    # to render debug emits in the panel.
    assert "verbose_gui_mode" in src, (
        "_log must read config['verbose_gui_mode'] to decide whether to "
        "show debug-level lines in the panel"
    )
    # The default path (verbose OFF) must still suppress debug from the panel.
    assert 'level != "debug"' in src or "level == \"debug\"" in src, (
        "_log must explicitly branch on level == 'debug' so the panel is "
        "clean when Verbose Mode is off"
    )
    # debug must still reach the file logger via logger.debug.
    assert "logger.debug" in src, "debug-level emits must still go to logger.debug for the file log"


def test_verbose_mode_panel_routing_simulated():
    """Simulate the _log routing logic (without booting Tkinter) and verify:
    - verbose OFF: debug lines skip the panel, info lines show
    - verbose ON: both debug and info show in the panel
    File logger always sees both, regardless of verbose setting.
    """
    panel_log: list = []
    file_log: list = []

    def simulate_log(message: str, level: str, *, verbose_gui_mode: bool):
        # Mirror the production routing in KlingGUIWindow._log.
        show_in_panel = level != "debug" or verbose_gui_mode
        if show_in_panel:
            panel_log.append((message, "info" if level == "debug" else level))
        # File logger always sees the line.
        file_log.append((message, level))

    # Default (verbose OFF)
    simulate_log("user-facing info", "info", verbose_gui_mode=False)
    simulate_log("verbose debug detail", "debug", verbose_gui_mode=False)
    assert ("user-facing info", "info") in panel_log
    assert all(m != "verbose debug detail" for m, _ in panel_log)
    assert ("verbose debug detail", "debug") in file_log

    # Verbose ON
    panel_log.clear()
    simulate_log("user-facing info 2", "info", verbose_gui_mode=True)
    simulate_log("verbose debug detail 2", "debug", verbose_gui_mode=True)
    assert ("user-facing info 2", "info") in panel_log
    # debug emit becomes panel-visible but rendered with the "info" tag.
    assert ("verbose debug detail 2", "info") in panel_log


def test_verbose_gui_mode_default_is_false():
    """The clean-panel-by-default contract: the boot-time default for
    verbose_gui_mode must be False so new users get the friendly stream
    out of the box. Verbose Mode is opt-in via the Settings checkbox."""
    panel_source = (ROOT / "kling_gui" / "main_window.py").read_text(encoding="utf-8")
    # The default-config dict in _load_config must declare verbose_gui_mode
    # as False. We match the literal line that defines it.
    assert '"verbose_gui_mode": False' in panel_source, (
        "verbose_gui_mode default must be False (clean panel out of the box). "
        "Verbose stream is opt-in via the Settings checkbox."
    )
    # Belt-and-suspenders: explicitly assert the True default is gone.
    assert '"verbose_gui_mode": True' not in panel_source, (
        "Found a legacy 'verbose_gui_mode: True' default; flip to False so "
        "new installs and migrated configs land on the clean panel."
    )


def test_verbose_gui_mode_legacy_true_migrated_to_false_once():
    """Existing users who saved configs under the pre-v1.7 True default
    get flipped once to False; the migration flag prevents us from
    overriding the user's preference if they later opt back into verbose."""
    from kling_gui.main_window import KlingGUIWindow

    # Legacy config: had True from old default, no migration flag.
    legacy = {"verbose_gui_mode": True}
    KlingGUIWindow._migrate_legacy_defaults(legacy)
    assert legacy["verbose_gui_mode"] is False
    assert legacy["verbose_gui_mode_migrated_v17"] is True

    # User opted into verbose AFTER the migration ran: stays True.
    opted_in = {"verbose_gui_mode": True, "verbose_gui_mode_migrated_v17": True}
    KlingGUIWindow._migrate_legacy_defaults(opted_in)
    assert opted_in["verbose_gui_mode"] is True

    # User had False already (either explicit or fresh install): stays False.
    fresh = {"verbose_gui_mode": False}
    KlingGUIWindow._migrate_legacy_defaults(fresh)
    assert fresh["verbose_gui_mode"] is False
    assert fresh["verbose_gui_mode_migrated_v17"] is True


# ---------------------------------------------------------------------------
# Default-version consistency regression: every layer that picks a default
# Oldcam version must agree on v13 across CLI, GUI, and launcher chains.
# ---------------------------------------------------------------------------


def test_default_oldcam_version_is_v15_across_all_layers():
    """If someone adds a new default-version site and forgets to set v15,
    this test fails. Locks the contract until the next intentional bump.
    (V15 "Temporal Mute" superseded V14 as the default — V14 stays
    selectable but is no longer pre-selected anywhere.)"""
    # CLI / automation default
    from automation.config import merge_automation_defaults
    merged = merge_automation_defaults({})
    assert merged["automation_oldcam_version"] == "v15", (
        f"automation/config.py default must be v15; got "
        f"{merged['automation_oldcam_version']!r}"
    )

    # GUI checkbox default (v15 BooleanVar with value=True)
    config_panel_src = (ROOT / "kling_gui" / "config_panel.py").read_text(encoding="utf-8")
    assert '"v15": tk.BooleanVar(value=True)' in config_panel_src, (
        "GUI must ship with v15 checkbox pre-selected"
    )
    # Every other version in the dict must be value=False (only v15 starts
    # ticked). v14 is now demoted alongside v7-v13.
    for legacy in ("v7", "v8", "v9", "v10", "v11", "v12", "v13", "v14"):
        assert f'"{legacy}": tk.BooleanVar(value=True)' not in config_panel_src, (
            f"Only v15 should be pre-selected; found {legacy} also marked True"
        )

    # Legacy fallback strings inside config_panel must default to v15.
    # (Three call sites: load → save, on-change → save, _resolve fallback.)
    fallback_v15_count = config_panel_src.count('"v15"')
    assert fallback_v15_count >= 3, (
        f"Expected at least 3 'v15' fallback strings in config_panel.py; got {fallback_v15_count}"
    )

    # CLI fallback strings + choice list must include v15 and use it as default.
    cli_src = (ROOT / "kling_automation_ui.py").read_text(encoding="utf-8")
    assert cli_src.count("'v15'") + cli_src.count('"v15"') >= 4, (
        "Expected CLI to default to v15 in ≥3 get(..., 'v15') calls + choice list"
    )
    # The choice list must contain v15 (and still list v14 as a pickable option).
    assert '"v15"' in cli_src or "'v15'" in cli_src
    assert '"v7", "v8", "v9", "v10", "v11", "v12", "v13", "v14", "v15", "all"' in cli_src, (
        "CLI menu choice list must enumerate v7-v15 plus 'all'"
    )

    # Launcher chains: root + windows hub + macOS hub must chain to v15.
    for path in (
        "run_oldcam.bat",
        "launchers/windows/run_oldcam.bat",
    ):
        text = (ROOT / path).read_text(encoding="utf-8")
        assert "run_oldcam_v15.bat" in text, f"{path} must chain to run_oldcam_v15.bat"

    macos_chain = (ROOT / "launchers" / "macos" / "run_oldcam.command").read_text(encoding="utf-8")
    assert "run_oldcam_v15.command" in macos_chain, (
        "launchers/macos/run_oldcam.command must chain to run_oldcam_v15.command"
    )


def test_oldcam_v13_standalone_files_present_for_both_platforms():
    """v13 standalone must exist for Windows + macOS so users can run it
    directly without the GUI. Cross-platform parity is non-negotiable."""
    expected_files = [
        # Windows side
        "oldcam-v13/oldcam.py",
        "oldcam-v13/launcher.py",
        "oldcam-v13/oldcam_launcher.bat",
        "oldcam-v13/requirements.txt",
        # macOS side
        "oldcam-v13/macOS/oldcam.py",
        "oldcam-v13/macOS/oldcam.command",
        "oldcam-v13/macOS/requirements.txt",
        # Hub launchers
        "launchers/windows/run_oldcam_v13.bat",
        "launchers/macos/run_oldcam_v13.command",
        "launchers/run_oldcam_v13.bat",
        "launchers/run_oldcam_v13.command",
    ]
    for rel in expected_files:
        assert (ROOT / rel).exists(), f"Missing v13 standalone file: {rel}"

    # Sanity: macOS .command must use LF endings only (else Bash chokes).
    for cmd_rel in (
        "oldcam-v13/macOS/oldcam.command",
        "launchers/macos/run_oldcam_v13.command",
        "launchers/run_oldcam_v13.command",
    ):
        data = (ROOT / cmd_rel).read_bytes()
        assert b"\r\n" not in data, (
            f"{cmd_rel} must use LF endings only (macOS bash rejects CRLF)"
        )

    # Sanity: Windows .bat must contain CRLF (cmd.exe garbles LF-only batches).
    for bat_rel in (
        "oldcam-v13/oldcam_launcher.bat",
        "launchers/windows/run_oldcam_v13.bat",
        "launchers/run_oldcam_v13.bat",
    ):
        data = (ROOT / bat_rel).read_bytes()
        assert b"\r\n" in data, (
            f"{bat_rel} must use CRLF endings (cmd.exe garbles LF-only .bat files)"
        )


# ---------------------------------------------------------------------------
# V14 "Forensic Daylight" — physics-corrected successor to V13 (new default)
# ---------------------------------------------------------------------------


def test_v14_default_output_path_uses_v14_suffix():
    oldcam_v14 = load_module(ROOT / "oldcam-v14" / "oldcam.py", "oldcam_v14")
    assert oldcam_v14.build_default_output_path("sample.mp4").endswith("sample-oldcam-v14.mp4")


def test_oldcam_dependency_preflight_does_not_require_mediapipe_for_v14(tmp_path):
    """V14 Forensic Daylight — like V13/V12, no MediaPipe; missing mediapipe must be OK."""
    manager, _ = make_queue_manager({})
    oldcam_dir = tmp_path / "oldcam-v14"
    oldcam_dir.mkdir()
    (oldcam_dir / "requirements.txt").write_text("numpy>=1.24\nopencv-python-headless>=4.8\n", encoding="utf-8")

    real_import = builtins.__import__

    def fake_import(name, *args, **kwargs):
        if name == "mediapipe":
            raise ImportError("No module named mediapipe")
        return real_import(name, *args, **kwargs)

    with mock.patch("builtins.__import__", side_effect=fake_import):
        assert manager._ensure_oldcam_dependencies(oldcam_dir, "v14") is True


def test_v14_process_frame_skips_noise_and_ae_stepping():
    """V14 keeps V13's no-AE/no-old-noise contract: the legacy
    apply_modern_sensor_noise / apply_ae_stepping helpers (kept as dead code
    for auditability) must never be called from process_frame."""
    oldcam_v14 = load_module(ROOT / "oldcam-v14" / "oldcam.py", "oldcam_v14_daylight")

    called = []

    def mark(name):
        def _wrapped(*args, **kwargs):
            called.append(name)
            return args[0]
        return _wrapped

    image = np.full((32, 32, 3), 128, dtype=np.uint8)
    state = {
        "face_detected": False,
        "fpn": np.zeros((32, 32, 3), dtype=np.float32),
        "adjusted_vignette_mask": np.ones((32, 32, 1), dtype=np.float32),
        "last_masks": {},
    }

    import argparse
    args = argparse.Namespace(
        vignette_strength=0.55, read_noise=0.22, shot_noise=0.16, chroma_noise_ratio=0.08
    )

    with (
        mock.patch.object(oldcam_v14, "apply_modern_sensor_noise", side_effect=mark("noise")),
        mock.patch.object(oldcam_v14, "apply_ae_stepping", side_effect=mark("ae")),
    ):
        oldcam_v14.process_frame(image, None, None, args, rng=np.random.default_rng(0), state=state)

    assert "noise" not in called, "V14 must not call apply_modern_sensor_noise (uses sensor floor)"
    assert "ae" not in called, "V14 must not call apply_ae_stepping (stable daylight assumption)"


def test_v14_awb_is_multiplicative_not_scalar_add():
    """The core forensic fix: V14 AWB must be a multiplicative color-temperature
    drift (inverse R/B gains), NOT V13's scalar `image_f += drift` luma add."""
    for rel in ("oldcam-v14/oldcam.py", "oldcam-v14/macOS/oldcam.py"):
        src = (ROOT / rel).read_text(encoding="utf-8")
        import ast as _ast
        tree = _ast.parse(src)
        fn = next(
            n for n in _ast.walk(tree)
            if isinstance(n, _ast.FunctionDef) and n.name == "apply_global_awb_drift"
        )
        # Strip the docstring (it intentionally quotes the V13 ``image_f += drift``
        # bug for context) — assert only against the executable statements.
        stmts = fn.body[1:] if (
            fn.body and isinstance(fn.body[0], _ast.Expr)
            and isinstance(getattr(fn.body[0], "value", None), _ast.Constant)
        ) else fn.body
        code = "\n".join(_ast.get_source_segment(src, s) for s in stmts)
        assert "image_f += drift" not in code, f"{rel}: V13 scalar-add AWB bug still present"
        assert "image_f[:, :, 2] *= 1.0 + drift" in code, f"{rel}: Red gain missing"
        assert "image_f[:, :, 0] *= 1.0 - drift" in code, f"{rel}: Blue gain missing"
        assert "awb_temp_drift" in code and "awb_temp_velocity" in code, (
            f"{rel}: mean-reverting drift state keys missing"
        )
        assert "np.rint(np.clip(image_f, 0, 255))" in code, f"{rel}: must round, not truncate"


def test_v14_sensor_floor_is_subperceptual_and_breaks_stasis():
    """V14's sensor floor must perturb every frame (no synthetic stasis) yet
    stay sub-perceptual (small max delta) — the SNR/PAD-defeating property."""
    oldcam_v14 = load_module(ROOT / "oldcam-v14" / "oldcam.py", "oldcam_v14_floor")
    rng = np.random.default_rng(7)
    img = (rng.random((48, 64, 3)) * 255).astype(np.uint8)
    out = oldcam_v14.apply_daylight_sensor_floor(img.copy(), rng)
    assert out.shape == img.shape and out.dtype == np.uint8
    diff = np.abs(out.astype(np.int16) - img.astype(np.int16))
    assert (out != img).any(), "sensor floor did nothing — synthetic stasis not broken"
    assert diff.max() <= 8, f"sensor floor not sub-perceptual: max delta {diff.max()}"
    assert diff.mean() < 1.5, f"sensor floor too strong: mean delta {diff.mean():.3f}"


def test_v14_bloom_is_smoothstep_not_binary_threshold():
    """V14 bloom must use a smoothstep ramp (no cv2.THRESH_BINARY flicker)."""
    for rel in ("oldcam-v14/oldcam.py", "oldcam-v14/macOS/oldcam.py"):
        src = (ROOT / rel).read_text(encoding="utf-8")
        import re
        body = re.search(r"def apply_highlight_blooming.*?(?=\ndef )", src, re.S).group(0)
        assert "cv2.THRESH_BINARY" not in body, f"{rel}: binary-threshold bloom still present"
        assert "3.0 - 2.0 * mask" in body, f"{rel}: smoothstep ramp missing"


def test_v14_uses_lossless_temp_and_copies_audio():
    """V14 must end the V13 double-lossy pipeline: lossless FFV1 temp (with
    MJPG/mp4v fallback) and audio stream-copy (no highpass/lowpass/compressor)."""
    for rel in ("oldcam-v14/oldcam.py", "oldcam-v14/macOS/oldcam.py"):
        src = (ROOT / rel).read_text(encoding="utf-8")
        assert ".tmp_lossless.mkv" in src, f"{rel}: lossless mkv temp path missing"
        assert all(c in src for c in ('"FFV1"', '"MJPG"', '"mp4v"')), (
            f"{rel}: FFV1->MJPG->mp4v fallback chain missing"
        )
        assert "highpass=f=300" not in src, f"{rel}: V13 audio-mangling filter still present"
        assert '"-c:a",\n            "copy",' in src, f"{rel}: audio must be stream-copied"


def test_v14_parser_exposes_sensor_floor_and_crf_args():
    """V14 CLI must expose --read-noise/--shot-noise/--chroma-noise-ratio/--crf."""
    oldcam_v14 = load_module(ROOT / "oldcam-v14" / "oldcam.py", "oldcam_v14_parser")
    parser = oldcam_v14.build_parser()
    ns = parser.parse_args(["clip.mp4", "--read-noise", "9", "--crf", "99"])
    # bare parse holds raw values; main() clamps — assert the args exist + types
    assert hasattr(ns, "read_noise") and hasattr(ns, "shot_noise")
    assert hasattr(ns, "chroma_noise_ratio") and hasattr(ns, "crf")
    # --grain must still be rejected (dead in the daylight pipeline, like V13)
    with pytest.raises(SystemExit):
        parser.parse_args(["dummy.mp4", "--grain", "5"])


def test_v14_naturalize_image_runs_via_real_parser(tmp_path):
    """End-to-end CLI parser -> naturalize_image path with the new args must
    not AttributeError (regression guard mirroring the V13 --grain test)."""
    import cv2
    oldcam_v14 = load_module(ROOT / "oldcam-v14" / "oldcam.py", "oldcam_v14_cli_path")
    src_img = tmp_path / "tiny.png"
    cv2.imwrite(str(src_img), np.full((16, 16, 3), 128, dtype=np.uint8))
    out_img = tmp_path / "out.png"
    parser = oldcam_v14.build_parser()
    args = parser.parse_args([str(src_img)])
    args.read_noise = max(0.0, min(float(args.read_noise), 1.0))
    args.shot_noise = max(0.0, min(float(args.shot_noise), 1.0))
    args.chroma_noise_ratio = max(0.0, min(float(args.chroma_noise_ratio), 0.5))
    args.crf = max(10, min(int(args.crf), 24))
    oldcam_v14.naturalize_image(str(src_img), str(out_img), args)
    assert out_img.exists(), "V14 naturalize_image did not produce output"


def test_oldcam_v14_standalone_files_present_for_both_platforms():
    """v14 standalone must exist for Windows + macOS so users can run it
    directly without the GUI. Cross-platform parity is non-negotiable."""
    expected_files = [
        # Windows side
        "oldcam-v14/oldcam.py",
        "oldcam-v14/launcher.py",
        "oldcam-v14/oldcam_launcher.bat",
        "oldcam-v14/requirements.txt",
        # macOS side
        "oldcam-v14/macOS/oldcam.py",
        "oldcam-v14/macOS/oldcam.command",
        "oldcam-v14/macOS/requirements.txt",
        # Hub launchers
        "launchers/windows/run_oldcam_v14.bat",
        "launchers/macos/run_oldcam_v14.command",
        "launchers/run_oldcam_v14.bat",
        "launchers/run_oldcam_v14.command",
    ]
    for rel in expected_files:
        assert (ROOT / rel).exists(), f"Missing v14 standalone file: {rel}"

    # Sanity: macOS .command must use LF endings only (else Bash chokes).
    for cmd_rel in (
        "oldcam-v14/macOS/oldcam.command",
        "launchers/macos/run_oldcam_v14.command",
        "launchers/run_oldcam_v14.command",
    ):
        data = (ROOT / cmd_rel).read_bytes()
        assert b"\r\n" not in data, (
            f"{cmd_rel} must use LF endings only (macOS bash rejects CRLF)"
        )

    # Sanity: Windows .bat must contain CRLF (cmd.exe garbles LF-only batches).
    for bat_rel in (
        "oldcam-v14/oldcam_launcher.bat",
        "launchers/windows/run_oldcam_v14.bat",
        "launchers/run_oldcam_v14.bat",
    ):
        data = (ROOT / bat_rel).read_bytes()
        assert b"\r\n" in data, (
            f"{bat_rel} must use CRLF endings (cmd.exe garbles LF-only .bat files)"
        )


# ---------------------------------------------------------------------------
# V15 "Temporal Mute" — V14 math/encoding + V13 noise-free + V12 ghosting
# (new default; superseded V14 per Resemble deepfake-API benchmarking)
# ---------------------------------------------------------------------------


def test_v15_default_output_path_uses_v15_suffix():
    oldcam_v15 = load_module(ROOT / "oldcam-v15" / "oldcam.py", "oldcam_v15")
    assert oldcam_v15.build_default_output_path("sample.mp4").endswith("sample-oldcam-v15.mp4")


def test_oldcam_dependency_preflight_does_not_require_mediapipe_for_v15(tmp_path):
    """V15 Temporal Mute — like V14/V13/V12, no MediaPipe; missing mediapipe must be OK."""
    manager, _ = make_queue_manager({})
    oldcam_dir = tmp_path / "oldcam-v15"
    oldcam_dir.mkdir()
    (oldcam_dir / "requirements.txt").write_text("numpy>=1.24\nopencv-python-headless>=4.8\n", encoding="utf-8")

    real_import = builtins.__import__

    def fake_import(name, *args, **kwargs):
        if name == "mediapipe":
            raise ImportError("No module named mediapipe")
        return real_import(name, *args, **kwargs)

    with mock.patch("builtins.__import__", side_effect=fake_import):
        assert manager._ensure_oldcam_dependencies(oldcam_dir, "v15") is True


def test_v15_process_frame_skips_noise_ae_and_sensor_floor():
    """V15 keeps V13/V14's no-AE/no-old-noise contract AND additionally must
    NOT call apply_daylight_sensor_floor (the V14 frequency-detector tell that
    Resemble testing flagged — removed entirely in V15)."""
    oldcam_v15 = load_module(ROOT / "oldcam-v15" / "oldcam.py", "oldcam_v15_mute")

    called = []

    def mark(name):
        def _wrapped(*args, **kwargs):
            called.append(name)
            return args[0]
        return _wrapped

    image = np.full((32, 32, 3), 128, dtype=np.uint8)
    state = {
        "face_detected": False,
        "fpn": np.zeros((32, 32, 3), dtype=np.float32),
        "adjusted_vignette_mask": np.ones((32, 32, 1), dtype=np.float32),
        "last_masks": {},
    }

    import argparse
    args = argparse.Namespace(vignette_strength=0.55, ghosting=0.18)

    # apply_daylight_sensor_floor must not even exist on the v15 module.
    assert not hasattr(oldcam_v15, "apply_daylight_sensor_floor"), (
        "V15 must delete apply_daylight_sensor_floor entirely (it was V14's "
        "frequency-detector tell)"
    )
    with (
        mock.patch.object(oldcam_v15, "apply_modern_sensor_noise", side_effect=mark("noise")),
        mock.patch.object(oldcam_v15, "apply_ae_stepping", side_effect=mark("ae")),
    ):
        oldcam_v15.process_frame(image, None, None, args, rng=np.random.default_rng(0), state=state)

    assert "noise" not in called, "V15 must not call apply_modern_sensor_noise"
    assert "ae" not in called, "V15 must not call apply_ae_stepping"


def test_v15_has_no_sensor_floor_code():
    """V15 must not DEFINE the V14 sensor-floor function nor REGISTER its
    --read/shot/chroma-noise CLI args.

    The static half only asserts the function `def` is gone — the V15 parser
    comment legitimately names the removed knobs to explain the change, so a
    bare substring check would false-positive. The authoritative check that
    the flags are truly unregistered is the runtime parse-rejection below.
    """
    for rel in ("oldcam-v15/oldcam.py", "oldcam-v15/macOS/oldcam.py"):
        src = (ROOT / rel).read_text(encoding="utf-8")
        assert "def apply_daylight_sensor_floor" not in src, (
            f"{rel}: V14 sensor-floor function must be deleted in V15"
        )

    oldcam_v15 = load_module(ROOT / "oldcam-v15" / "oldcam.py", "oldcam_v15_nofloor")
    parser = oldcam_v15.build_parser()
    for bad in ("--read-noise", "--shot-noise", "--chroma-noise-ratio"):
        with pytest.raises(SystemExit):
            parser.parse_args(["dummy.mp4", bad, "0.1"])


def test_v15_restores_ghosting_from_args():
    """V15 must reintroduce V12-style temporal blending: naturalize_video
    passes args.ghosting (NOT the hardcoded 0.0 of V13/V14) to
    blend_with_previous_frame, and the parser defaults --ghosting to 0.18."""
    for rel in ("oldcam-v15/oldcam.py", "oldcam-v15/macOS/oldcam.py"):
        src = (ROOT / rel).read_text(encoding="utf-8")
        import re
        body = re.search(r"def naturalize_video.*?(?=\ndef )", src, re.S).group(0)
        assert "previous_processed, args.ghosting" in body, (
            f"{rel}: naturalize_video must pass args.ghosting (V12 temporal blend restored)"
        )
        assert "previous_processed, 0.0" not in body, (
            f"{rel}: V13/V14 hardcoded 0.0 ghosting must be gone in V15"
        )

    oldcam_v15 = load_module(ROOT / "oldcam-v15" / "oldcam.py", "oldcam_v15_ghost")
    ns = oldcam_v15.build_parser().parse_args(["clip.mp4"])
    assert abs(float(ns.ghosting) - 0.18) < 1e-9, (
        f"V15 --ghosting default must be 0.18; got {ns.ghosting}"
    )


def test_v15_parser_exposes_ghosting_and_crf_but_not_noise_args():
    """V15 CLI: --ghosting + --crf present; the V14 --read-noise/--shot-noise/
    --chroma-noise-ratio and the long-dead --grain must all be rejected."""
    oldcam_v15 = load_module(ROOT / "oldcam-v15" / "oldcam.py", "oldcam_v15_parser")
    parser = oldcam_v15.build_parser()
    ns = parser.parse_args(["clip.mp4", "--ghosting", "0.3", "--crf", "12"])
    assert hasattr(ns, "ghosting") and hasattr(ns, "crf")
    assert abs(float(ns.ghosting) - 0.3) < 1e-9
    for bad in ("--read-noise", "--shot-noise", "--chroma-noise-ratio", "--grain"):
        with pytest.raises(SystemExit):
            parser.parse_args(["dummy.mp4", bad, "5"])
    # bounded_ghosting must still reject out-of-range values
    with pytest.raises(SystemExit):
        parser.parse_args(["dummy.mp4", "--ghosting", "0.9"])


def test_v15_parser_exposes_vignette_strength_with_safe_default():
    """V15 exposes --vignette-strength (process_frame/naturalize_video read
    it via getattr); the default must equal the historic getattr fallback
    (0.55) so adding the knob changes no existing behaviour."""
    oldcam_v15 = load_module(ROOT / "oldcam-v15" / "oldcam.py", "oldcam_v15_vig")
    parser = oldcam_v15.build_parser()
    ns = parser.parse_args(["clip.mp4"])
    assert hasattr(ns, "vignette_strength")
    assert ns.vignette_strength == pytest.approx(0.55), (
        "default must match the existing getattr(..., 0.55) fallback exactly"
    )
    ns2 = parser.parse_args(["clip.mp4", "--vignette-strength", "0.3"])
    assert ns2.vignette_strength == pytest.approx(0.3)


def test_v15_main_clamps_out_of_range_vignette_strength(tmp_path):
    """main() must clamp --vignette-strength into [0.0, 1.0] on the REAL
    code path. Spies on process_input to capture the args object main()
    actually forwards downstream, so the assertion targets the production
    clamp directly (not a re-implemented formula, not fragile image math):
    deleting/breaking the clamp at the production site fails this test.
    """
    oldcam_v15 = load_module(ROOT / "oldcam-v15" / "oldcam.py", "oldcam_v15_vig_clamp")
    src_img = tmp_path / "tiny.png"
    src_img.write_bytes(b"not-real-media")  # process_input is stubbed; never opened

    captured = {}

    def spy_process_input(input_path, output_path, args):
        captured["vignette_strength"] = args.vignette_strength
        captured["crf"] = args.crf

    with mock.patch.object(oldcam_v15, "process_input", side_effect=spy_process_input):
        # Over-range high (5.0) and a separate over-range low (-3.0) run.
        oldcam_v15.main([str(src_img), "-o", str(tmp_path / "o1.png"),
                         "--vignette-strength", "5"])
        assert captured["vignette_strength"] == 1.0, (
            f"main() did not clamp 5.0 -> 1.0 (got {captured['vignette_strength']}); "
            "production clamp at oldcam.py main() missing/broken"
        )
        # crf clamp still applied alongside (regression guard on the shared
        # block). Default is 23 post-Laundromat; ceiling widened to 28.
        assert captured["crf"] == 23 and 10 <= captured["crf"] <= 28

        oldcam_v15.main([str(src_img), "-o", str(tmp_path / "o2.png"),
                         "--vignette-strength", "-3"])
        assert captured["vignette_strength"] == 0.0, (
            f"main() did not clamp -3.0 -> 0.0 (got {captured['vignette_strength']})"
        )


def test_v15_awb_is_multiplicative_not_scalar_add():
    """V15 inherits V14's corrected multiplicative AWB drift verbatim."""
    for rel in ("oldcam-v15/oldcam.py", "oldcam-v15/macOS/oldcam.py"):
        src = (ROOT / rel).read_text(encoding="utf-8")
        import ast as _ast
        tree = _ast.parse(src)
        fn = next(
            n for n in _ast.walk(tree)
            if isinstance(n, _ast.FunctionDef) and n.name == "apply_global_awb_drift"
        )
        stmts = fn.body[1:] if (
            fn.body and isinstance(fn.body[0], _ast.Expr)
            and isinstance(getattr(fn.body[0], "value", None), _ast.Constant)
        ) else fn.body
        code = "\n".join(_ast.get_source_segment(src, s) for s in stmts)
        assert "image_f += drift" not in code, f"{rel}: scalar-add AWB bug present"
        assert "image_f[:, :, 2] *= 1.0 + drift" in code, f"{rel}: Red gain missing"
        assert "image_f[:, :, 0] *= 1.0 - drift" in code, f"{rel}: Blue gain missing"
        assert "np.rint(np.clip(image_f, 0, 255))" in code, f"{rel}: must round, not truncate"


def test_v15_bloom_is_smoothstep_and_temp_is_double_lossy_audio_copied():
    """V15 keeps V14's smoothstep bloom + audio stream-copy, but the
    "Laundromat" hotfix REVERTS the lossless FFV1 temp to a deliberate
    double-lossy mp4v temp (no FFV1/MJPG fallback loop)."""
    for rel in ("oldcam-v15/oldcam.py", "oldcam-v15/macOS/oldcam.py"):
        src = (ROOT / rel).read_text(encoding="utf-8")
        import re
        body = re.search(r"def apply_highlight_blooming.*?(?=\ndef )", src, re.S).group(0)
        assert "cv2.THRESH_BINARY" not in body, f"{rel}: binary-threshold bloom present"
        assert "3.0 - 2.0 * mask" in body, f"{rel}: smoothstep ramp missing"
        # Lossy mp4v temp restored; the FFV1/MJPG *codecs* and the
        # mkv/avi temp *paths* must be gone from the actual pipeline.
        # (The words FFV1/MJPG may still appear in the docstring that
        # explains what the hotfix reverted — that's intentional context,
        # so assert on the code constructs, not bare substrings.)
        assert ".tmp_noaudio.mp4" in src, f"{rel}: lossy mp4v temp path missing"
        assert ".tmp_lossless.mkv" not in src, f"{rel}: stale lossless mkv path present"
        assert ".tmp_mjpg.avi" not in src, f"{rel}: stale mjpg avi path present"
        assert 'fourcc(*"FFV1")' not in src and 'fourcc(*"MJPG")' not in src, (
            f"{rel}: FFV1/MJPG codec writer must be removed"
        )
        assert "temp_candidates" not in src, (
            f"{rel}: the FFV1->MJPG->mp4v fallback loop must be gone"
        )
        assert 'cv2.VideoWriter_fourcc(*"mp4v")' in src, (
            f"{rel}: pure mp4v writer missing"
        )
        assert '"-c:a",\n            "copy",' in src, f"{rel}: audio must be stream-copied"


def test_v15_crf_default_is_23_clamped_to_28(tmp_path):
    """Laundromat hotfix: --crf default 14 -> 23, clamp ceiling 24 -> 28
    (heavier organic web compression to crush the AI signature).

    Asserts the clamp *behaviour* via the real main() path (spying on the
    args main() forwards to process_input) rather than a source-string match,
    so refactoring the clamp doesn't falsely fail this test.
    """
    oldcam_v15 = load_module(ROOT / "oldcam-v15" / "oldcam.py", "oldcam_v15_crf")
    parser = oldcam_v15.build_parser()
    ns = parser.parse_args(["clip.mp4"])
    assert ns.crf == 23, f"V15 --crf default must be 23; got {ns.crf}"

    src_img = tmp_path / "tiny.png"
    src_img.write_bytes(b"not-real-media")  # process_input is stubbed
    captured = {}

    def spy_process_input(input_path, output_path, args):
        captured["crf"] = args.crf

    with mock.patch.object(oldcam_v15, "process_input", side_effect=spy_process_input):
        oldcam_v15.main([str(src_img), "-o", str(tmp_path / "o1.png"), "--crf", "35"])
        assert captured["crf"] == 28, f"CRF 35 must clamp to 28; got {captured['crf']}"
        oldcam_v15.main([str(src_img), "-o", str(tmp_path / "o2.png"), "--crf", "5"])
        assert captured["crf"] == 10, f"CRF 5 must clamp to 10; got {captured['crf']}"
        oldcam_v15.main([str(src_img), "-o", str(tmp_path / "o3.png"), "--crf", "20"])
        assert captured["crf"] == 20, f"in-range CRF 20 must be preserved; got {captured['crf']}"


def test_v15_naturalize_image_runs_via_real_parser(tmp_path):
    """End-to-end CLI parser -> naturalize_image with the V15 arg set must
    not AttributeError (no read_noise/shot_noise/chroma attrs to clamp)."""
    import cv2
    oldcam_v15 = load_module(ROOT / "oldcam-v15" / "oldcam.py", "oldcam_v15_cli_path")
    src_img = tmp_path / "tiny.png"
    cv2.imwrite(str(src_img), np.full((16, 16, 3), 128, dtype=np.uint8))
    out_img = tmp_path / "out.png"
    parser = oldcam_v15.build_parser()
    args = parser.parse_args([str(src_img)])
    args.crf = max(10, min(int(args.crf), 28))  # mirrors production clamp
    oldcam_v15.naturalize_image(str(src_img), str(out_img), args)
    assert out_img.exists(), "V15 naturalize_image did not produce output"


def test_oldcam_v15_standalone_files_present_for_both_platforms():
    """v15 standalone must exist for Windows + macOS so users can run it
    directly without the GUI. Cross-platform parity is non-negotiable."""
    expected_files = [
        # Windows side
        "oldcam-v15/oldcam.py",
        "oldcam-v15/launcher.py",
        "oldcam-v15/oldcam_launcher.bat",
        "oldcam-v15/requirements.txt",
        # macOS side
        "oldcam-v15/macOS/oldcam.py",
        "oldcam-v15/macOS/oldcam.command",
        "oldcam-v15/macOS/requirements.txt",
        # Hub launchers
        "launchers/windows/run_oldcam_v15.bat",
        "launchers/macos/run_oldcam_v15.command",
        "launchers/run_oldcam_v15.bat",
        "launchers/run_oldcam_v15.command",
    ]
    for rel in expected_files:
        assert (ROOT / rel).exists(), f"Missing v15 standalone file: {rel}"

    # macOS .command must use LF endings only (else Bash chokes).
    for cmd_rel in (
        "oldcam-v15/macOS/oldcam.command",
        "launchers/macos/run_oldcam_v15.command",
        "launchers/run_oldcam_v15.command",
    ):
        data = (ROOT / cmd_rel).read_bytes()
        assert b"\r\n" not in data, (
            f"{cmd_rel} must use LF endings only (macOS bash rejects CRLF)"
        )

    # Windows .bat must contain CRLF (cmd.exe garbles LF-only batches).
    for bat_rel in (
        "oldcam-v15/oldcam_launcher.bat",
        "launchers/windows/run_oldcam_v15.bat",
        "launchers/run_oldcam_v15.bat",
    ):
        data = (ROOT / bat_rel).read_bytes()
        assert b"\r\n" in data, (
            f"{bat_rel} must use CRLF endings (cmd.exe garbles LF-only .bat files)"
        )


def test_v15_twins_are_byte_identical():
    """The Windows and macOS v15 oldcam.py must be byte-for-byte identical."""
    win = (ROOT / "oldcam-v15" / "oldcam.py").read_bytes()
    mac = (ROOT / "oldcam-v15" / "macOS" / "oldcam.py").read_bytes()
    assert win == mac, "oldcam-v15 Windows/macOS oldcam.py twins diverged"
