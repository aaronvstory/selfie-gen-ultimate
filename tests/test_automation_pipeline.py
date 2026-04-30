from pathlib import Path

from PIL import Image

from automation.config import from_app_config, merge_automation_defaults
from automation.discovery import CaseRecord
from automation.manifest import AutomationManifest
from automation.pipeline import AutoPipelineRunner, PipelineDeps


class FakeOutpaint:
    def __init__(self):
        self.calls = []

    def set_progress_callback(self, _cb):
        return None

    def outpaint(self, image_path, output_folder, output_path=None, **kwargs):
        self.calls.append(kwargs)
        out_path = Path(output_path or (Path(output_folder) / f"{Path(image_path).stem}-expanded.png"))
        out_path.parent.mkdir(parents=True, exist_ok=True)
        Image.new("RGB", (64, 64), (120, 120, 120)).save(out_path)
        return str(out_path)


class FakeSelfie:
    def __init__(self):
        self.calls = 0

    def set_progress_callback(self, _cb):
        return None

    def generate(self, image_path, prompt, output_folder, model_endpoint="", **kwargs):
        del prompt, model_endpoint, kwargs
        self.calls += 1
        out = Path(output_folder) / f"{Path(image_path).stem}_sim85_001.png"
        out.parent.mkdir(parents=True, exist_ok=True)
        Image.new("RGB", (64, 64), (110, 110, 110)).save(out)
        return str(out)


class FakeVideo:
    def set_progress_callback(self, _cb):
        return None

    def create_kling_generation(self, character_image_path, output_folder=None, **kwargs):
        del character_image_path, kwargs
        out = Path(output_folder or ".") / "video.mp4"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(b"mp4")
        return str(out)


def test_pipeline_success_case(tmp_path: Path, monkeypatch):
    case_dir = tmp_path / "case-a"
    case_dir.mkdir()
    front = case_dir / "front.png"
    front.write_bytes(b"front")
    record = CaseRecord(case_dir=case_dir, front_path=front, relative_key="case-a")

    config = merge_automation_defaults({"falai_api_key": "x", "saved_prompts": {"1": "prompt"}, "current_prompt_slot": 1})
    manifest = AutomationManifest.create_or_load(tmp_path / "automation_manifest.json", tmp_path, {})
    manifest.ensure_case(record.relative_key, record.case_dir, record.front_path)

    monkeypatch.setattr("automation.pipeline.extract_portrait_crop", lambda **kwargs: {"confidence": 0.9, "crop_box": [0, 0, 10, 10], "extractor": "mock"})
    monkeypatch.setattr("automation.pipeline.compute_face_similarity_details", lambda *args, **kwargs: {"score": 90, "pass": True, "error": None, "match": True})
    monkeypatch.setattr("automation.pipeline.run_oldcam", lambda **kwargs: None)

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
    stats = runner.run([record])
    assert stats["completed"] == 1
    assert manifest.data["cases"]["case-a"]["status"] == "complete"


def test_pipeline_similarity_manual_review(tmp_path: Path, monkeypatch):
    case_dir = tmp_path / "case-b"
    case_dir.mkdir()
    front = case_dir / "front.jpeg"
    front.write_bytes(b"front")
    record = CaseRecord(case_dir=case_dir, front_path=front, relative_key="case-b")

    config = merge_automation_defaults({"falai_api_key": "x", "saved_prompts": {"1": "prompt"}, "current_prompt_slot": 1})
    manifest = AutomationManifest.create_or_load(tmp_path / "automation_manifest.json", tmp_path, {})
    manifest.ensure_case(record.relative_key, record.case_dir, record.front_path)

    monkeypatch.setattr("automation.pipeline.extract_portrait_crop", lambda **kwargs: {"confidence": 0.9, "crop_box": [0, 0, 10, 10], "extractor": "mock"})
    monkeypatch.setattr("automation.pipeline.compute_face_similarity_details", lambda *args, **kwargs: {"score": 40, "pass": False, "error": None, "match": False})
    monkeypatch.setattr("automation.pipeline.run_oldcam", lambda **kwargs: None)

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
    stats = runner.run([record])
    assert stats["manual_review"] == 1
    assert manifest.data["cases"]["case-b"]["status"] == "manual_review"


def test_pipeline_skips_video_when_existing(tmp_path: Path, monkeypatch):
    case_dir = tmp_path / "case-c"
    case_dir.mkdir()
    front = case_dir / "front.png"
    front.write_bytes(b"front")
    (case_dir / "gen-videos").mkdir()
    existing_video = case_dir / "gen-videos" / "existing.mp4"
    existing_video.write_bytes(b"video")
    record = CaseRecord(case_dir=case_dir, front_path=front, relative_key="case-c")

    config = merge_automation_defaults({"falai_api_key": "x", "saved_prompts": {"1": "prompt"}, "current_prompt_slot": 1})
    manifest = AutomationManifest.create_or_load(tmp_path / "automation_manifest.json", tmp_path, {})
    manifest.ensure_case(record.relative_key, record.case_dir, record.front_path)

    monkeypatch.setattr("automation.pipeline.extract_portrait_crop", lambda **kwargs: {"confidence": 0.9, "crop_box": [0, 0, 10, 10], "extractor": "mock"})
    monkeypatch.setattr("automation.pipeline.compute_face_similarity_details", lambda *args, **kwargs: {"score": 90, "pass": True, "error": None, "match": True})
    monkeypatch.setattr("automation.pipeline.run_oldcam", lambda **kwargs: None)

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
    stats = runner.run([record])
    assert stats["completed"] == 1
    step = manifest.data["cases"]["case-c"]["steps"]["video_generate"]
    assert step["status"] == "skipped"
    assert str(existing_video) in str(step["output"])


def test_pipeline_oldcam_required_failure_marks_case_failed(tmp_path: Path, monkeypatch):
    case_dir = tmp_path / "case-d"
    case_dir.mkdir()
    front = case_dir / "front.png"
    front.write_bytes(b"front")
    record = CaseRecord(case_dir=case_dir, front_path=front, relative_key="case-d")

    config = merge_automation_defaults(
        {
            "falai_api_key": "x",
            "saved_prompts": {"1": "prompt"},
            "current_prompt_slot": 1,
            "automation_oldcam_required": True,
        }
    )
    manifest = AutomationManifest.create_or_load(tmp_path / "automation_manifest.json", tmp_path, {})
    manifest.ensure_case(record.relative_key, record.case_dir, record.front_path)
    monkeypatch.setattr("automation.pipeline.extract_portrait_crop", lambda **kwargs: {"confidence": 0.9, "crop_box": [0, 0, 10, 10], "extractor": "mock"})
    monkeypatch.setattr("automation.pipeline.compute_face_similarity_details", lambda *args, **kwargs: {"score": 90, "pass": True, "error": None, "match": True})
    monkeypatch.setattr("automation.pipeline.run_oldcam", lambda **kwargs: None)

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
    stats = runner.run([record])
    assert stats["failed"] == 1
    assert manifest.data["cases"]["case-d"]["steps"]["oldcam"]["status"] == "failed"


def test_pipeline_increment_mode_generates_incremented_files(tmp_path: Path, monkeypatch):
    case_dir = tmp_path / "case-e"
    case_dir.mkdir()
    front = case_dir / "front.png"
    front.write_bytes(b"front")
    record = CaseRecord(case_dir=case_dir, front_path=front, relative_key="case-e")

    config = merge_automation_defaults(
        {
            "falai_api_key": "x",
            "saved_prompts": {"1": "prompt"},
            "current_prompt_slot": 1,
            "automation_allow_reprocess": True,
            "automation_reprocess_mode": "increment",
        }
    )
    manifest = AutomationManifest.create_or_load(tmp_path / "automation_manifest.json", tmp_path, {})
    manifest.ensure_case(record.relative_key, record.case_dir, record.front_path)
    monkeypatch.setattr("automation.pipeline.extract_portrait_crop", lambda **kwargs: {"confidence": 0.9, "crop_box": [0, 0, 10, 10], "extractor": "mock"})
    monkeypatch.setattr("automation.pipeline.compute_face_similarity_details", lambda *args, **kwargs: {"score": 90, "pass": True, "error": None, "match": True})
    monkeypatch.setattr("automation.pipeline.run_oldcam", lambda **kwargs: None)

    # Pre-create expected default outputs so increment path is exercised.
    (case_dir / "front-expanded.png").write_bytes(b"x")
    (case_dir / "extracted.png").write_bytes(b"x")
    (case_dir / "gen-images").mkdir(exist_ok=True)
    (case_dir / "gen-images" / "extracted_sim85_001-expanded.png").write_bytes(b"x")

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
    stats = runner.run([record])
    assert stats["completed"] == 1
    front_out = Path(manifest.data["cases"]["case-e"]["steps"]["front_expand"]["output"])
    assert "_v" in front_out.stem


def test_pipeline_overwrite_mode_reuses_base_output_name(tmp_path: Path, monkeypatch):
    case_dir = tmp_path / "case-f"
    case_dir.mkdir()
    front = case_dir / "front.png"
    front.write_bytes(b"front")
    record = CaseRecord(case_dir=case_dir, front_path=front, relative_key="case-f")

    config = merge_automation_defaults(
        {
            "falai_api_key": "x",
            "saved_prompts": {"1": "prompt"},
            "current_prompt_slot": 1,
            "automation_allow_reprocess": True,
            "automation_reprocess_mode": "overwrite",
        }
    )
    manifest = AutomationManifest.create_or_load(tmp_path / "automation_manifest.json", tmp_path, {})
    manifest.ensure_case(record.relative_key, record.case_dir, record.front_path)
    monkeypatch.setattr("automation.pipeline.extract_portrait_crop", lambda **kwargs: {"confidence": 0.9, "crop_box": [0, 0, 10, 10], "extractor": "mock"})
    monkeypatch.setattr("automation.pipeline.compute_face_similarity_details", lambda *args, **kwargs: {"score": 90, "pass": True, "error": None, "match": True})
    monkeypatch.setattr("automation.pipeline.run_oldcam", lambda **kwargs: None)

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
    stats = runner.run([record])
    assert stats["completed"] == 1
    front_out = Path(manifest.data["cases"]["case-f"]["steps"]["front_expand"]["output"])
    assert "_v" not in front_out.stem


def test_pipeline_validation_fails_on_oldcam_required_without_enable(tmp_path: Path):
    config = merge_automation_defaults(
        {
            "falai_api_key": "x",
            "automation_oldcam_enabled": False,
            "automation_oldcam_required": True,
        }
    )
    manifest = AutomationManifest.create_or_load(tmp_path / "automation_manifest.json", tmp_path, {})
    runner = AutoPipelineRunner(
        config=config,
        automation_config=from_app_config(config),
        manifest=manifest,
        deps=PipelineDeps(
            outpaint_factory=lambda: FakeOutpaint(),
            selfie_factory=lambda: FakeSelfie(),
            video_factory=lambda: FakeVideo(),
        ),
    )
    issues = runner.validate_configuration()
    assert any("requires" in issue for issue in issues)


def test_pipeline_manual_review_when_selfie_disabled(tmp_path: Path, monkeypatch):
    case_dir = tmp_path / "case-g"
    case_dir.mkdir()
    front = case_dir / "front.png"
    front.write_bytes(b"front")
    record = CaseRecord(case_dir=case_dir, front_path=front, relative_key="case-g")

    config = merge_automation_defaults(
        {
            "falai_api_key": "x",
            "automation_selfie_enabled": False,
        }
    )
    manifest = AutomationManifest.create_or_load(tmp_path / "automation_manifest.json", tmp_path, {})
    manifest.ensure_case(record.relative_key, record.case_dir, record.front_path)
    monkeypatch.setattr("automation.pipeline.extract_portrait_crop", lambda **kwargs: {"confidence": 0.9, "crop_box": [0, 0, 10, 10], "extractor": "mock"})
    monkeypatch.setattr("automation.pipeline.run_oldcam", lambda **kwargs: None)

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
    stats = runner.run([record])
    assert stats["manual_review"] == 1
    assert manifest.data["cases"]["case-g"]["steps"]["selfie_generate"]["status"] == "manual_review"


def test_pipeline_honors_selfie_max_attempts(tmp_path: Path, monkeypatch):
    case_dir = tmp_path / "case-h"
    case_dir.mkdir()
    front = case_dir / "front.png"
    front.write_bytes(b"front")
    record = CaseRecord(case_dir=case_dir, front_path=front, relative_key="case-h")

    config = merge_automation_defaults(
        {
            "falai_api_key": "x",
            "saved_prompts": {"1": "prompt"},
            "current_prompt_slot": 1,
            "automation_selfie_models": ["m1", "m2"],
            "automation_selfie_max_attempts_per_model": 2,
        }
    )
    manifest = AutomationManifest.create_or_load(tmp_path / "automation_manifest.json", tmp_path, {})
    manifest.ensure_case(record.relative_key, record.case_dir, record.front_path)
    monkeypatch.setattr("automation.pipeline.extract_portrait_crop", lambda **kwargs: {"confidence": 0.9, "crop_box": [0, 0, 10, 10], "extractor": "mock"})
    monkeypatch.setattr("automation.pipeline.run_oldcam", lambda **kwargs: None)
    monkeypatch.setattr("automation.pipeline.compute_face_similarity_details", lambda *args, **kwargs: {"score": 10, "pass": False, "error": None, "match": False})

    selfie = FakeSelfie()
    runner = AutoPipelineRunner(
        config=config,
        automation_config=from_app_config(config),
        manifest=manifest,
        progress_cb=lambda msg, level="info": None,
        deps=PipelineDeps(
            outpaint_factory=lambda: FakeOutpaint(),
            selfie_factory=lambda: selfie,
            video_factory=lambda: FakeVideo(),
        ),
    )
    runner.run([record])
    assert selfie.calls == 4


def test_pipeline_existing_video_still_runs_oldcam_when_enabled(tmp_path: Path, monkeypatch):
    case_dir = tmp_path / "case-i"
    case_dir.mkdir()
    front = case_dir / "front.png"
    front.write_bytes(b"front")
    (case_dir / "gen-videos").mkdir()
    existing_video = case_dir / "gen-videos" / "existing.mp4"
    existing_video.write_bytes(b"video")
    record = CaseRecord(case_dir=case_dir, front_path=front, relative_key="case-i")

    config = merge_automation_defaults({"falai_api_key": "x"})
    manifest = AutomationManifest.create_or_load(tmp_path / "automation_manifest.json", tmp_path, {})
    manifest.ensure_case(record.relative_key, record.case_dir, record.front_path)
    monkeypatch.setattr("automation.pipeline.extract_portrait_crop", lambda **kwargs: {"confidence": 0.9, "crop_box": [0, 0, 10, 10], "extractor": "mock"})
    monkeypatch.setattr("automation.pipeline.compute_face_similarity_details", lambda *args, **kwargs: {"score": 90, "pass": True, "error": None, "match": True})

    oldcam_called = {"value": False}

    def _run_oldcam(**kwargs):
        oldcam_called["value"] = True
        return None

    monkeypatch.setattr("automation.pipeline.run_oldcam", _run_oldcam)

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
    stats = runner.run([record])
    assert stats["completed"] == 1
    assert oldcam_called["value"] is True


def test_pipeline_extract_disabled_stays_skipped_when_file_exists(tmp_path: Path, monkeypatch):
    case_dir = tmp_path / "case-j"
    case_dir.mkdir()
    front = case_dir / "front.png"
    front.write_bytes(b"front")
    extracted = case_dir / "extracted.png"
    Image.new("RGB", (32, 32), (10, 10, 10)).save(extracted)
    record = CaseRecord(case_dir=case_dir, front_path=front, relative_key="case-j")

    config = merge_automation_defaults(
        {
            "falai_api_key": "x",
            "automation_extract_enabled": False,
        }
    )
    manifest = AutomationManifest.create_or_load(tmp_path / "automation_manifest.json", tmp_path, {})
    manifest.ensure_case(record.relative_key, record.case_dir, record.front_path)
    monkeypatch.setattr("automation.pipeline.compute_face_similarity_details", lambda *args, **kwargs: {"score": 90, "pass": True, "error": None, "match": True})
    monkeypatch.setattr("automation.pipeline.run_oldcam", lambda **kwargs: None)

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
    stats = runner.run([record])
    assert stats["completed"] == 1
    assert manifest.data["cases"]["case-j"]["steps"]["extract_portrait"]["status"] == "skipped"


def test_pipeline_extract_disabled_missing_file_marks_manual_review(tmp_path: Path, monkeypatch):
    case_dir = tmp_path / "case-k"
    case_dir.mkdir()
    front = case_dir / "front.png"
    front.write_bytes(b"front")
    record = CaseRecord(case_dir=case_dir, front_path=front, relative_key="case-k")

    config = merge_automation_defaults(
        {
            "falai_api_key": "x",
            "automation_extract_enabled": False,
        }
    )
    manifest = AutomationManifest.create_or_load(tmp_path / "automation_manifest.json", tmp_path, {})
    manifest.ensure_case(record.relative_key, record.case_dir, record.front_path)
    monkeypatch.setattr("automation.pipeline.run_oldcam", lambda **kwargs: None)

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
    stats = runner.run([record])
    assert stats["manual_review"] == 1
    assert manifest.data["cases"]["case-k"]["status"] == "manual_review"


def test_pipeline_video_disabled_skips_oldcam_without_video(tmp_path: Path, monkeypatch):
    case_dir = tmp_path / "case-l"
    case_dir.mkdir()
    front = case_dir / "front.png"
    front.write_bytes(b"front")
    record = CaseRecord(case_dir=case_dir, front_path=front, relative_key="case-l")

    config = merge_automation_defaults(
        {
            "falai_api_key": "x",
            "automation_video_enabled": False,
            "automation_oldcam_enabled": True,
        }
    )
    manifest = AutomationManifest.create_or_load(tmp_path / "automation_manifest.json", tmp_path, {})
    manifest.ensure_case(record.relative_key, record.case_dir, record.front_path)
    monkeypatch.setattr("automation.pipeline.extract_portrait_crop", lambda **kwargs: {"confidence": 0.9, "crop_box": [0, 0, 10, 10], "extractor": "mock"})
    monkeypatch.setattr("automation.pipeline.compute_face_similarity_details", lambda *args, **kwargs: {"score": 90, "pass": True, "error": None, "match": True})

    oldcam_called = {"value": False}

    def _run_oldcam(**kwargs):
        oldcam_called["value"] = True
        return None

    monkeypatch.setattr("automation.pipeline.run_oldcam", _run_oldcam)

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
    stats = runner.run([record])
    assert stats["completed"] == 1
    assert oldcam_called["value"] is False
    assert manifest.data["cases"]["case-l"]["steps"]["oldcam"]["status"] == "skipped"


def test_pipeline_video_disabled_oldcam_required_fails(tmp_path: Path, monkeypatch):
    case_dir = tmp_path / "case-m"
    case_dir.mkdir()
    front = case_dir / "front.png"
    front.write_bytes(b"front")
    record = CaseRecord(case_dir=case_dir, front_path=front, relative_key="case-m")

    config = merge_automation_defaults(
        {
            "falai_api_key": "x",
            "automation_video_enabled": False,
            "automation_oldcam_enabled": True,
            "automation_oldcam_required": True,
        }
    )
    manifest = AutomationManifest.create_or_load(tmp_path / "automation_manifest.json", tmp_path, {})
    manifest.ensure_case(record.relative_key, record.case_dir, record.front_path)
    monkeypatch.setattr("automation.pipeline.extract_portrait_crop", lambda **kwargs: {"confidence": 0.9, "crop_box": [0, 0, 10, 10], "extractor": "mock"})
    monkeypatch.setattr("automation.pipeline.compute_face_similarity_details", lambda *args, **kwargs: {"score": 90, "pass": True, "error": None, "match": True})
    monkeypatch.setattr("automation.pipeline.run_oldcam", lambda **kwargs: None)

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
    stats = runner.run([record])
    assert stats["failed"] == 1
    assert manifest.data["cases"]["case-m"]["status"] == "failed"


def test_pipeline_resolves_auto_provider_to_bfl_for_caps_and_outpaint(tmp_path: Path, monkeypatch):
    case_dir = tmp_path / "case-n"
    case_dir.mkdir()
    front = case_dir / "front.png"
    Image.new("RGB", (1000, 1000), (1, 2, 3)).save(front)
    record = CaseRecord(case_dir=case_dir, front_path=front, relative_key="case-n")

    config = merge_automation_defaults({"falai_api_key": "x", "bfl_api_key": "bfl-token"})
    manifest = AutomationManifest.create_or_load(tmp_path / "automation_manifest.json", tmp_path, {})
    manifest.ensure_case(record.relative_key, record.case_dir, record.front_path)
    monkeypatch.setattr("automation.pipeline.extract_portrait_crop", lambda **kwargs: {"confidence": 0.9, "crop_box": [0, 0, 10, 10], "extractor": "mock"})
    monkeypatch.setattr("automation.pipeline.compute_face_similarity_details", lambda *args, **kwargs: {"score": 90, "pass": True, "error": None, "match": True})
    monkeypatch.setattr("automation.pipeline.run_oldcam", lambda **kwargs: None)

    outpaint = FakeOutpaint()
    runner = AutoPipelineRunner(
        config=config,
        automation_config=from_app_config(config),
        manifest=manifest,
        progress_cb=lambda msg, level="info": None,
        deps=PipelineDeps(
            outpaint_factory=lambda: outpaint,
            selfie_factory=lambda: FakeSelfie(),
            video_factory=lambda: FakeVideo(),
        ),
    )
    stats = runner.run([record])
    assert stats["completed"] == 1
    assert outpaint.calls
    assert all(call.get("provider") == "bfl" for call in outpaint.calls)


def test_pipeline_selfie_expand_reuse_skips_outpaint_call(tmp_path: Path, monkeypatch):
    case_dir = tmp_path / "case-o"
    case_dir.mkdir()
    front = case_dir / "front.png"
    Image.new("RGB", (128, 128), (11, 11, 11)).save(front)
    existing_expanded = case_dir / "gen-images" / "already-expanded.png"
    existing_expanded.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (128, 128), (22, 22, 22)).save(existing_expanded)
    record = CaseRecord(case_dir=case_dir, front_path=front, relative_key="case-o")

    config = merge_automation_defaults({"falai_api_key": "x"})
    manifest = AutomationManifest.create_or_load(tmp_path / "automation_manifest.json", tmp_path, {})
    manifest.ensure_case(record.relative_key, record.case_dir, record.front_path)
    manifest.update_step(record.relative_key, "selfie_expand", "complete", output=str(existing_expanded))
    monkeypatch.setattr("automation.pipeline.extract_portrait_crop", lambda **kwargs: {"confidence": 0.9, "crop_box": [0, 0, 10, 10], "extractor": "mock"})
    monkeypatch.setattr("automation.pipeline.compute_face_similarity_details", lambda *args, **kwargs: {"score": 90, "pass": True, "error": None, "match": True})
    monkeypatch.setattr("automation.pipeline.run_oldcam", lambda **kwargs: None)

    outpaint = FakeOutpaint()
    runner = AutoPipelineRunner(
        config=config,
        automation_config=from_app_config(config),
        manifest=manifest,
        progress_cb=lambda msg, level="info": None,
        deps=PipelineDeps(
            outpaint_factory=lambda: outpaint,
            selfie_factory=lambda: FakeSelfie(),
            video_factory=lambda: FakeVideo(),
        ),
    )
    stats = runner.run([record])
    assert stats["completed"] == 1
    selfie_expand_step = manifest.get_step(record.relative_key, "selfie_expand")
    assert selfie_expand_step["status"] == "complete"
    assert selfie_expand_step["output"] == str(existing_expanded)
    assert selfie_expand_step["meta"]["reused_existing"] is True
    assert len(outpaint.calls) == 1


def test_pipeline_marks_active_selfie_step_failed_on_exception(tmp_path: Path, monkeypatch):
    case_dir = tmp_path / "case-p"
    case_dir.mkdir()
    front = case_dir / "front.png"
    Image.new("RGB", (64, 64), (1, 1, 1)).save(front)
    existing_selfie = case_dir / "gen-images" / "candidate.png"
    existing_selfie.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (64, 64), (2, 2, 2)).save(existing_selfie)
    record = CaseRecord(case_dir=case_dir, front_path=front, relative_key="case-p")

    config = merge_automation_defaults({"falai_api_key": "x"})
    manifest = AutomationManifest.create_or_load(tmp_path / "automation_manifest.json", tmp_path, {})
    manifest.ensure_case(record.relative_key, record.case_dir, record.front_path)
    monkeypatch.setattr("automation.pipeline.extract_portrait_crop", lambda **kwargs: {"confidence": 0.9, "crop_box": [0, 0, 10, 10], "extractor": "mock"})
    monkeypatch.setattr(
        "automation.pipeline.compute_face_similarity_details",
        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("score boom")),
    )
    monkeypatch.setattr("automation.pipeline.run_oldcam", lambda **kwargs: None)

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
    stats = runner.run([record])
    assert stats["failed"] == 1
    step = manifest.get_step(record.relative_key, "selfie_generate")
    assert step["status"] == "failed"
    assert "score boom" in (step.get("error") or "")
    assert manifest.data["cases"][record.relative_key].get("active_step") is None
