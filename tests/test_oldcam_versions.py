import importlib.util
import builtins
from pathlib import Path
from types import SimpleNamespace
from unittest import mock
import threading
import types
import sys

import numpy as np

from kling_gui.queue_manager import QueueItem, QueueManager


ROOT = Path(__file__).resolve().parents[1]


def load_module(path: Path, name: str):
    injected_fake = False
    if "mediapipe" not in sys.modules:
        class _FakeFaceMesh:
            def __init__(self, *args, **kwargs):
                pass

            def process(self, _img):
                return SimpleNamespace(multi_face_landmarks=None)

        fake_mp = types.SimpleNamespace(
            solutions=types.SimpleNamespace(
                face_mesh=types.SimpleNamespace(FaceMesh=_FakeFaceMesh)
            )
        )
        sys.modules["mediapipe"] = fake_mp
        injected_fake = True
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(module)
    finally:
        if injected_fake:
            sys.modules.pop("mediapipe", None)
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
    for version in ("7", "8", "9"):
        folder = tmp_path / f"oldcam-v{version}"
        folder.mkdir()
        (folder / "launcher.py").write_text("pass", encoding="utf-8")

    manager, _ = make_queue_manager({"oldcam_version": "all"})
    with mock.patch("kling_gui.queue_manager.get_app_dir", return_value=str(tmp_path)), \
        mock.patch("kling_gui.queue_manager.get_resource_dir", return_value=str(tmp_path)):
        versions = manager._discover_oldcam_versions()

    assert "v9" in versions
    assert versions[-1] in {"v9", "v10"}


def test_queue_manager_selects_oldcam_version_folder_and_output(tmp_path):
    input_path = tmp_path / "clip.mp4"
    input_path.write_bytes(b"video")
    output_path = tmp_path / "clip-oldcam-v8.mp4"

    manager, logs = make_queue_manager({"oldcam_version": "v8"})

    with mock.patch.object(manager, "_resolve_oldcam_dir", return_value=tmp_path / "oldcam-v8") as resolve_mock, \
        mock.patch.object(manager, "_ensure_oldcam_dependencies", return_value=True), \
        mock.patch("kling_gui.queue_manager.subprocess.run") as run_mock:
        oldcam_dir = tmp_path / "oldcam-v8"
        oldcam_dir.mkdir()
        (oldcam_dir / "launcher.py").write_text("pass", encoding="utf-8")
        run_mock.return_value = SimpleNamespace(returncode=0, stderr="", stdout="")

        def create_output(*_args, **_kwargs):
            output_path.write_bytes(b"done")
            return run_mock.return_value

        run_mock.side_effect = create_output

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
    assert 'for version in ("v7", "v8", "v9", "v10")' in panel_source


def test_oldcam_dependency_preflight_requires_mediapipe_for_v10(tmp_path):
    manager, _ = make_queue_manager({})
    oldcam_dir = tmp_path / "oldcam-v10"
    oldcam_dir.mkdir()
    (oldcam_dir / "requirements.txt").write_text("mediapipe>=0.10.14\n", encoding="utf-8")

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
    (oldcam_dir / "requirements.txt").write_text("mediapipe>=0.10.14\n", encoding="utf-8")

    real_import = builtins.__import__
    missing = {"enabled": True}

    def fake_import(name, *args, **kwargs):
        if name == "mediapipe" and missing["enabled"]:
            raise ImportError("No module named mediapipe")
        if name == "mediapipe":
            return types.SimpleNamespace()
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
