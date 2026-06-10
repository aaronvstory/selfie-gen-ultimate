"""Pipeline loop-step tests (Phase E order: Kling -> rPPG -> Loop -> Oldcam).

The ping-pong loop became a first-class automation step on 2026-06-11
(automation_loop_enabled, default OFF). These tests pin:
- loop disabled  -> step "skipped", ffmpeg wrapper never called
- loop enabled   -> create_looped_video runs on the Kling output, manifest
                    records "complete", and Oldcam consumes the LOOPED file
- loop failure   -> graceful skip (case continues unlooped, never fails)
- rPPG + loop    -> loop input is the rPPG-injected base (order proof)
"""

from pathlib import Path

from PIL import Image

from automation.config import from_app_config, merge_automation_defaults
from automation.discovery import CaseRecord
from automation.manifest import AutomationManifest
from automation.pipeline import AutoPipelineRunner, PipelineDeps

from tests.test_automation_pipeline import FakeOutpaint, FakeSelfie, FakeVideo


def _build_runner(tmp_path: Path, monkeypatch, extra_config=None, oldcam_capture=None):
    case_dir = tmp_path / "case-a"
    case_dir.mkdir()
    front = case_dir / "front.png"
    Image.new("RGB", (64, 64), (1, 1, 1)).save(front)
    record = CaseRecord(case_dir=case_dir, front_path=front, relative_key="case-a")

    config = merge_automation_defaults({
        "falai_api_key": "x",
        "bfl_api_key": "bfl-token",
        "automation_oldcam_required": False,
        "saved_prompts": {"1": "prompt"},
        "current_prompt_slot": 1,
        **(extra_config or {}),
    })
    manifest = AutomationManifest.create_or_load(tmp_path / "automation_manifest.json", tmp_path, {})
    manifest.ensure_case(record.relative_key, record.case_dir, record.front_path)

    monkeypatch.setattr(
        "automation.pipeline.extract_portrait_crop",
        lambda **kwargs: {"confidence": 0.9, "crop_box": [0, 0, 10, 10], "extractor": "mock"},
    )
    monkeypatch.setattr(
        "automation.pipeline.compute_face_similarity_details",
        lambda *args, **kwargs: {"score": 90, "pass": True, "error": None, "match": True},
    )

    def fake_oldcam_all(**kwargs):
        if oldcam_capture is not None:
            oldcam_capture.append(kwargs)
        return []

    monkeypatch.setattr("automation.pipeline.run_oldcam_all", fake_oldcam_all)

    runner = AutoPipelineRunner(
        config=config,
        automation_config=from_app_config(config),
        manifest=manifest,
        progress_cb=lambda msg, level="info": None,
        deps=PipelineDeps(
            outpaint_factory=lambda: FakeOutpaint(),
            selfie_factory=lambda: FakeSelfie(),
            video_factory=lambda: FakeVideo(),
        ),
    )
    return runner, record, manifest


def test_loop_disabled_skips_step_and_never_calls_ffmpeg(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(
        "automation.pipeline.create_looped_video",
        lambda *a, **k: calls.append((a, k)) or None,
    )
    runner, record, manifest = _build_runner(tmp_path, monkeypatch)

    stats = runner.run([record])

    assert stats["completed"] == 1
    assert calls == []
    loop_step = manifest.data["cases"]["case-a"]["steps"]["loop"]
    assert loop_step["status"] == "skipped"
    assert loop_step["error"] == "loop disabled"


def test_loop_enabled_loops_kling_output_and_feeds_oldcam(tmp_path, monkeypatch):
    looped_calls = []

    def fake_loop(input_path, suffix="_looped", overwrite=True, log_callback=None, **kwargs):
        looped_calls.append(input_path)
        out = Path(input_path).with_name(Path(input_path).stem + "_looped.mp4")
        out.write_bytes(b"looped-mp4")
        return str(out)

    monkeypatch.setattr("automation.pipeline.create_looped_video", fake_loop)
    oldcam_capture = []
    runner, record, manifest = _build_runner(
        tmp_path,
        monkeypatch,
        extra_config={"automation_loop_enabled": True},
        oldcam_capture=oldcam_capture,
    )

    stats = runner.run([record])

    assert stats["completed"] == 1
    assert len(looped_calls) == 1
    assert looped_calls[0].endswith("video.mp4")
    loop_step = manifest.data["cases"]["case-a"]["steps"]["loop"]
    assert loop_step["status"] == "complete"
    assert loop_step["output"].endswith("video_looped.mp4")
    # Oldcam must consume the LOOPED file (Phase E order).
    assert len(oldcam_capture) == 1
    assert str(oldcam_capture[0]["video_path"]).endswith("video_looped.mp4")


def test_loop_failure_is_graceful_skip_and_case_completes(tmp_path, monkeypatch):
    monkeypatch.setattr("automation.pipeline.create_looped_video", lambda *a, **k: None)
    oldcam_capture = []
    runner, record, manifest = _build_runner(
        tmp_path,
        monkeypatch,
        extra_config={"automation_loop_enabled": True},
        oldcam_capture=oldcam_capture,
    )

    stats = runner.run([record])

    assert stats["completed"] == 1
    loop_step = manifest.data["cases"]["case-a"]["steps"]["loop"]
    assert loop_step["status"] == "skipped"
    assert "loop failed" in (loop_step["error"] or "")
    # Oldcam falls back to the unlooped Kling output.
    assert len(oldcam_capture) == 1
    assert str(oldcam_capture[0]["video_path"]).endswith("video.mp4")


def test_rppg_prepass_reuses_existing_sibling_on_resume(tmp_path, monkeypatch):
    """Codex P2 (round 3): the rPPG pre-pass used to be untracked — on a
    resume after an abort, the minutes-long GPU injection re-ran from
    scratch even though the clean-named ``{stem}-rppg{ext}`` sibling sat on
    disk. With skip semantics the sibling is now reused and run_rppg is
    NEVER invoked."""
    def must_not_run(**kwargs):
        raise AssertionError("run_rppg must not be called when the -rppg sibling exists")

    monkeypatch.setattr("automation.pipeline.run_rppg", must_not_run)
    oldcam_capture = []
    runner, record, manifest = _build_runner(
        tmp_path,
        monkeypatch,
        extra_config={"automation_rppg_enabled": True},
        oldcam_capture=oldcam_capture,
    )

    # Pre-seed the rPPG sibling next to where the fake Kling output lands.
    video_dir = record.case_dir / "gen-videos"
    video_dir.mkdir(exist_ok=True)
    (video_dir / "video-rppg.mp4").write_bytes(b"rppg-mp4")

    stats = runner.run([record])

    assert stats["completed"] == 1
    # Oldcam consumed the REUSED rPPG base, not the raw video.
    assert len(oldcam_capture) == 1
    assert str(oldcam_capture[0]["video_path"]).endswith("video-rppg.mp4")


def test_rppg_then_loop_order(tmp_path, monkeypatch):
    """With rPPG AND loop enabled, the loop input must be the rPPG-injected
    base (Kling -> rPPG -> Loop -> Oldcam), and Oldcam gets the looped file."""
    looped_inputs = []

    def fake_loop(input_path, suffix="_looped", overwrite=True, log_callback=None, **kwargs):
        looped_inputs.append(input_path)
        out = Path(input_path).with_name(Path(input_path).stem + "_looped.mp4")
        out.write_bytes(b"looped-mp4")
        return str(out)

    def fake_rppg(*, video_path, **kwargs):
        injected = video_path.with_name(video_path.stem + "-rppg.mp4")
        injected.write_bytes(b"rppg-mp4")
        return injected

    monkeypatch.setattr("automation.pipeline.create_looped_video", fake_loop)
    monkeypatch.setattr("automation.pipeline.run_rppg", fake_rppg)
    oldcam_capture = []
    runner, record, manifest = _build_runner(
        tmp_path,
        monkeypatch,
        extra_config={
            "automation_loop_enabled": True,
            "automation_rppg_enabled": True,
        },
        oldcam_capture=oldcam_capture,
    )

    stats = runner.run([record])

    assert stats["completed"] == 1
    assert len(looped_inputs) == 1
    assert looped_inputs[0].endswith("video-rppg.mp4")
    assert len(oldcam_capture) == 1
    assert str(oldcam_capture[0]["video_path"]).endswith("video-rppg_looped.mp4")
