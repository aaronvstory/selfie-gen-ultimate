from pathlib import Path

from PIL import Image

from automation.config import from_app_config, merge_automation_defaults
from automation.discovery import CaseRecord
from automation.manifest import AutomationManifest
from automation.pipeline import AutoPipelineRunner, PipelineDeps


class FakeOutpaint:
    def set_progress_callback(self, _cb):
        return None

    def outpaint(self, image_path, output_folder, output_path=None, **kwargs):
        del kwargs
        out_path = Path(output_path or (Path(output_folder) / f"{Path(image_path).stem}-expanded.png"))
        out_path.parent.mkdir(parents=True, exist_ok=True)
        Image.new("RGB", (64, 64), (120, 120, 120)).save(out_path)
        return str(out_path)


class FakeSelfie:
    def set_progress_callback(self, _cb):
        return None

    def generate(self, image_path, prompt, output_folder, model_endpoint="", **kwargs):
        del prompt, model_endpoint, kwargs
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
