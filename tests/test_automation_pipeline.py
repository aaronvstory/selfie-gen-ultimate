from pathlib import Path

import pytest
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
        self.last_prompt = None

    def set_progress_callback(self, _cb):
        return None

    def generate(self, image_path, prompt, output_folder, model_endpoint="", **kwargs):
        del model_endpoint, kwargs
        self.calls += 1
        self.last_prompt = prompt
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
    Image.new("RGB", (64, 64), (1, 1, 1)).save(front)
    record = CaseRecord(case_dir=case_dir, front_path=front, relative_key="case-a")

    config = merge_automation_defaults({"falai_api_key": "x",
            "bfl_api_key": "bfl-token",
            "automation_oldcam_required": False, "saved_prompts": {"1": "prompt"}, "current_prompt_slot": 1})
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


def test_pipeline_fas_strict_gate_routes_to_manual_review(tmp_path: Path, monkeypatch):
    """When automation_similarity_require_fas_pass=true and FAS flags a spoof,
    the pipeline must route to manual_review even with a passing similarity score."""
    case_dir = tmp_path / "case-fas"
    case_dir.mkdir()
    front = case_dir / "front.jpeg"
    Image.new("RGB", (64, 64), (2, 2, 2)).save(front)
    record = CaseRecord(case_dir=case_dir, front_path=front, relative_key="case-fas")

    config = merge_automation_defaults({
        "falai_api_key": "x",
        "bfl_api_key": "bfl-token",
        "automation_oldcam_required": False,
        "automation_similarity_require_fas_pass": True,  # KEY UNDER TEST
        "saved_prompts": {"1": "prompt"},
        "current_prompt_slot": 1,
    })
    manifest = AutomationManifest.create_or_load(tmp_path / "automation_manifest.json", tmp_path, {})
    manifest.ensure_case(record.relative_key, record.case_dir, record.front_path)

    # Score is 95 (passing), but target image is flagged as spoofed.
    monkeypatch.setattr("automation.pipeline.extract_portrait_crop",
                        lambda **kwargs: {"confidence": 0.9, "crop_box": [0, 0, 10, 10], "extractor": "mock"})
    monkeypatch.setattr(
        "automation.pipeline.compute_face_similarity_details",
        lambda *a, **kw: {
            "score": 95,
            "pass": True,
            "error": None,
            "match": True,
            "diagnostics": {
                "mode": "normalized_crop",
                "anti_spoofing": {
                    "ref": {"status": "ok", "spoof_detected": False, "faces": [{"is_real": True, "antispoof_score": 0.91}]},
                    # Realistic DeepFace shape: spoof verdicts come back with
                    # HIGH confidence numbers, not low ones. The score is the
                    # model's certainty in is_real, not real-ness.
                    "target": {"status": "ok", "spoof_detected": True, "faces": [{"is_real": False, "antispoof_score": 0.99}]},
                },
            },
        },
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
    assert stats["manual_review"] == 1
    assert manifest.data["cases"]["case-fas"]["status"] == "manual_review"


def test_pipeline_fas_log_only_does_not_block_when_require_fas_pass_false(tmp_path: Path, monkeypatch):
    """When automation_similarity_require_fas_pass=false (default), FAS spoof_detected
    is log-only and a passing similarity score still routes the case to selfie_expand."""
    case_dir = tmp_path / "case-fas-log"
    case_dir.mkdir()
    front = case_dir / "front.jpeg"
    Image.new("RGB", (64, 64), (3, 3, 3)).save(front)
    record = CaseRecord(case_dir=case_dir, front_path=front, relative_key="case-fas-log")

    config = merge_automation_defaults({
        "falai_api_key": "x",
        "bfl_api_key": "bfl-token",
        "automation_oldcam_required": False,
        "automation_similarity_require_fas_pass": False,  # default
        "saved_prompts": {"1": "prompt"},
        "current_prompt_slot": 1,
    })
    manifest = AutomationManifest.create_or_load(tmp_path / "automation_manifest.json", tmp_path, {})
    manifest.ensure_case(record.relative_key, record.case_dir, record.front_path)

    monkeypatch.setattr("automation.pipeline.extract_portrait_crop",
                        lambda **kwargs: {"confidence": 0.9, "crop_box": [0, 0, 10, 10], "extractor": "mock"})
    monkeypatch.setattr(
        "automation.pipeline.compute_face_similarity_details",
        lambda *a, **kw: {
            "score": 95,
            "pass": True,
            "error": None,
            "match": True,
            "diagnostics": {
                "mode": "normalized_crop",
                "anti_spoofing": {
                    "ref": {"status": "ok", "spoof_detected": False, "faces": []},
                    "target": {"status": "ok", "spoof_detected": True, "faces": [{"is_real": False, "antispoof_score": 0.97}]},
                },
            },
        },
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
    # Spoof was detected but require_fas_pass=False, so case did NOT get gated by FAS.
    # It will likely complete or hit a downstream stub (expand/video); the key assertion
    # is that manual_review is NOT 1 due to FAS — we proved log-only behavior.
    assert stats.get("manual_review", 0) == 0


def test_pipeline_similarity_manual_review(tmp_path: Path, monkeypatch):
    case_dir = tmp_path / "case-b"
    case_dir.mkdir()
    front = case_dir / "front.jpeg"
    Image.new("RGB", (64, 64), (1, 1, 1)).save(front)
    record = CaseRecord(case_dir=case_dir, front_path=front, relative_key="case-b")

    config = merge_automation_defaults({"falai_api_key": "x",
            "bfl_api_key": "bfl-token",
            "automation_oldcam_required": False, "saved_prompts": {"1": "prompt"}, "current_prompt_slot": 1})
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
    Image.new("RGB", (64, 64), (1, 1, 1)).save(front)
    (case_dir / "gen-videos").mkdir()
    existing_video = case_dir / "gen-videos" / "existing.mp4"
    existing_video.write_bytes(b"video")
    record = CaseRecord(case_dir=case_dir, front_path=front, relative_key="case-c")

    config = merge_automation_defaults({"falai_api_key": "x",
            "bfl_api_key": "bfl-token",
            "automation_oldcam_required": False, "saved_prompts": {"1": "prompt"}, "current_prompt_slot": 1})
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
    Image.new("RGB", (64, 64), (1, 1, 1)).save(front)
    record = CaseRecord(case_dir=case_dir, front_path=front, relative_key="case-d")

    config = merge_automation_defaults(
        {
            "falai_api_key": "x",
            "bfl_api_key": "bfl-token",
            "automation_oldcam_required": False,
            "bfl_api_key": "bfl-token",
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
    Image.new("RGB", (64, 64), (1, 1, 1)).save(front)
    record = CaseRecord(case_dir=case_dir, front_path=front, relative_key="case-e")

    config = merge_automation_defaults(
        {
            "falai_api_key": "x",
            "bfl_api_key": "bfl-token",
            "automation_oldcam_required": False,
            "bfl_api_key": "bfl-token",
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
    Image.new("RGB", (64, 64), (1, 1, 1)).save(front)
    record = CaseRecord(case_dir=case_dir, front_path=front, relative_key="case-f")

    config = merge_automation_defaults(
        {
            "falai_api_key": "x",
            "bfl_api_key": "bfl-token",
            "automation_oldcam_required": False,
            "bfl_api_key": "bfl-token",
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
            "bfl_api_key": "bfl-token",
            "automation_oldcam_required": False,
            "bfl_api_key": "bfl-token",
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


def test_pipeline_validation_fails_when_bfl_provider_missing_bfl_key(tmp_path: Path):
    config = merge_automation_defaults(
        {
            "falai_api_key": "x",
            "bfl_api_key": "bfl-token",
            "automation_oldcam_required": False,
            "bfl_api_key": "bfl-token",
            "bfl_api_key": "",
            "automation_front_expand_provider": "bfl",
            "automation_selfie_expand_provider": "bfl",
            "automation_oldcam_required": False,
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
    assert any("front expand provider=bfl" in issue for issue in issues)
    assert any("selfie expand provider=bfl" in issue for issue in issues)


def test_pipeline_validation_passes_when_bfl_provider_has_bfl_key(tmp_path: Path, monkeypatch):
    config = merge_automation_defaults(
        {
            "falai_api_key": "x",
            "bfl_api_key": "bfl-token",
            "automation_oldcam_required": False,
            "bfl_api_key": "bfl-token",
            "bfl_api_key": "bfl-token",
            "automation_front_expand_provider": "bfl",
            "automation_selfie_expand_provider": "bfl",
            "automation_oldcam_required": False,
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
    assert not any("provider=bfl" in issue for issue in issues)


def test_pipeline_validation_fails_when_oldcam_required_and_not_ready(tmp_path: Path, monkeypatch):
    config = merge_automation_defaults(
        {
            "falai_api_key": "x",
            "bfl_api_key": "bfl-token",
            "automation_oldcam_required": False,
            "bfl_api_key": "bfl-token",
            "bfl_api_key": "bfl-token",
            "automation_oldcam_required": True,
            "automation_oldcam_enabled": True,
            "automation_oldcam_version": "all",
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
    monkeypatch.setattr("automation.pipeline.discover_oldcam_versions", lambda _repo_root: ["v8"])
    monkeypatch.setattr("automation.pipeline.ensure_oldcam_dependencies", lambda: (False, "missing deps"))
    issues = runner.validate_configuration()
    assert any("dependencies are not ready" in issue for issue in issues)


def test_pipeline_validation_oldcam_all_required_accepts_single_discovered_version(tmp_path: Path, monkeypatch):
    config = merge_automation_defaults(
        {
            "falai_api_key": "x",
            "bfl_api_key": "bfl-token",
            "automation_oldcam_required": True,
            "automation_oldcam_enabled": True,
            "automation_oldcam_version": "all",
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
    monkeypatch.setattr("automation.pipeline.discover_oldcam_versions", lambda _repo_root: ["v8"])
    monkeypatch.setattr("automation.pipeline.ensure_oldcam_dependencies", lambda: (True, ""))
    issues = runner.validate_configuration()
    assert not any("Oldcam required with version=all" in issue for issue in issues)


def test_pipeline_validation_collects_numeric_coercion_issues(tmp_path: Path):
    config = merge_automation_defaults(
        {
            "falai_api_key": "x",
            "bfl_api_key": "bfl-token",
            "automation_similarity_threshold": "abc",
            "automation_front_expand_percent": "bad",
            "automation_selfie_expand_percent": "-1",
            "automation_crop_multiplier": "nanx",
            "automation_selfie_max_attempts_per_model": "0",
            "automation_front_expand_passes": "3",
            "automation_oldcam_required": False,
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
    assert any("automation_similarity_threshold must be an integer." in issue for issue in issues)
    assert any("automation_front_expand_percent must be an integer." in issue for issue in issues)
    assert any("automation_crop_multiplier must be a number." in issue for issue in issues)
    assert any("automation_front_expand_passes must be 1 or 2." in issue for issue in issues)


def test_pipeline_validation_rejects_non_finite_crop_multiplier(tmp_path: Path):
    config = merge_automation_defaults(
        {
            "falai_api_key": "x",
            "bfl_api_key": "bfl-token",
            "automation_crop_multiplier": "nan",
            "automation_oldcam_required": False,
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
    assert any("automation_crop_multiplier must be a finite number." in issue for issue in issues)


def test_pipeline_validation_skips_front_percent_check_in_document_mode(tmp_path: Path):
    config = merge_automation_defaults(
        {
            "falai_api_key": "x",
            "bfl_api_key": "bfl-token",
            "automation_front_expand_mode": "document_3x4",
            "automation_front_expand_percent": "not-a-number",
            "automation_oldcam_required": False,
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
    assert not any("automation_front_expand_percent must be an integer." in issue for issue in issues)


def test_pipeline_manual_review_when_selfie_disabled(tmp_path: Path, monkeypatch):
    case_dir = tmp_path / "case-g"
    case_dir.mkdir()
    front = case_dir / "front.png"
    Image.new("RGB", (64, 64), (1, 1, 1)).save(front)
    record = CaseRecord(case_dir=case_dir, front_path=front, relative_key="case-g")

    config = merge_automation_defaults(
        {
            "falai_api_key": "x",
            "bfl_api_key": "bfl-token",
            "automation_oldcam_required": False,
            "bfl_api_key": "bfl-token",
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
    Image.new("RGB", (64, 64), (1, 1, 1)).save(front)
    record = CaseRecord(case_dir=case_dir, front_path=front, relative_key="case-h")

    config = merge_automation_defaults(
        {
            "falai_api_key": "x",
            "bfl_api_key": "bfl-token",
            "automation_oldcam_required": False,
            "bfl_api_key": "bfl-token",
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
    Image.new("RGB", (64, 64), (1, 1, 1)).save(front)
    (case_dir / "gen-videos").mkdir()
    existing_video = case_dir / "gen-videos" / "existing.mp4"
    existing_video.write_bytes(b"video")
    record = CaseRecord(case_dir=case_dir, front_path=front, relative_key="case-i")

    config = merge_automation_defaults({"falai_api_key": "x",
            "bfl_api_key": "bfl-token",
            "automation_oldcam_required": False,
            "automation_selfie_expand_composite_mode": "none"})
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
    Image.new("RGB", (64, 64), (1, 1, 1)).save(front)
    extracted = case_dir / "extracted.png"
    Image.new("RGB", (32, 32), (10, 10, 10)).save(extracted)
    record = CaseRecord(case_dir=case_dir, front_path=front, relative_key="case-j")

    config = merge_automation_defaults(
        {
            "falai_api_key": "x",
            "bfl_api_key": "bfl-token",
            "automation_oldcam_required": False,
            "bfl_api_key": "bfl-token",
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
    Image.new("RGB", (64, 64), (1, 1, 1)).save(front)
    record = CaseRecord(case_dir=case_dir, front_path=front, relative_key="case-k")

    config = merge_automation_defaults(
        {
            "falai_api_key": "x",
            "bfl_api_key": "bfl-token",
            "automation_oldcam_required": False,
            "bfl_api_key": "bfl-token",
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


def test_pipeline_extract_disabled_reuses_manifest_output(tmp_path: Path, monkeypatch):
    case_dir = tmp_path / "case-k2"
    case_dir.mkdir()
    front = case_dir / "front.png"
    Image.new("RGB", (64, 64), (1, 1, 1)).save(front)
    extracted = case_dir / "custom-extracted.png"
    Image.new("RGB", (32, 32), (10, 10, 10)).save(extracted)
    record = CaseRecord(case_dir=case_dir, front_path=front, relative_key="case-k2")

    config = merge_automation_defaults(
        {
            "falai_api_key": "x",
            "bfl_api_key": "bfl-token",
            "automation_oldcam_required": False,
            "automation_extract_enabled": False,
        }
    )
    manifest = AutomationManifest.create_or_load(tmp_path / "automation_manifest.json", tmp_path, {})
    manifest.ensure_case(record.relative_key, record.case_dir, record.front_path)
    manifest.update_step(record.relative_key, "extract_portrait", "complete", output=str(extracted))
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
    assert manifest.data["cases"]["case-k2"]["steps"]["extract_portrait"]["output"] == str(extracted)


def test_pipeline_video_disabled_skips_oldcam_without_video(tmp_path: Path, monkeypatch):
    case_dir = tmp_path / "case-l"
    case_dir.mkdir()
    front = case_dir / "front.png"
    Image.new("RGB", (64, 64), (1, 1, 1)).save(front)
    record = CaseRecord(case_dir=case_dir, front_path=front, relative_key="case-l")

    config = merge_automation_defaults(
        {
            "falai_api_key": "x",
            "bfl_api_key": "bfl-token",
            "automation_oldcam_required": False,
            "bfl_api_key": "bfl-token",
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
    Image.new("RGB", (64, 64), (1, 1, 1)).save(front)
    record = CaseRecord(case_dir=case_dir, front_path=front, relative_key="case-m")

    config = merge_automation_defaults(
        {
            "falai_api_key": "x",
            "bfl_api_key": "bfl-token",
            "automation_oldcam_required": False,
            "bfl_api_key": "bfl-token",
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

    config = merge_automation_defaults({"falai_api_key": "x",
            "bfl_api_key": "bfl-token",
            "automation_oldcam_required": False, "bfl_api_key": "bfl-token"})
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


def test_pipeline_front_expand_runs_two_passes_when_configured(tmp_path: Path, monkeypatch):
    case_dir = tmp_path / "case-n2"
    case_dir.mkdir()
    front = case_dir / "front.png"
    Image.new("RGB", (800, 600), (1, 2, 3)).save(front)
    record = CaseRecord(case_dir=case_dir, front_path=front, relative_key="case-n2")

    config = merge_automation_defaults(
        {
            "falai_api_key": "x",
            "bfl_api_key": "bfl-token",
            "automation_oldcam_required": False,
            "automation_front_expand_passes": 2,
            "automation_front_expand_composite_mode": "hard",
            "automation_selfie_expand_composite_mode": "feathered",
        }
    )
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
    assert len(outpaint.calls) == 3
    assert outpaint.calls[0]["composite_mode"] == "hard"
    assert outpaint.calls[1]["composite_mode"] == "hard"
    assert outpaint.calls[2]["composite_mode"] == "feathered"
    front_step = manifest.get_step(record.relative_key, "front_expand")
    assert front_step["meta"]["configured_passes"] == 2
    assert front_step["meta"]["executed_passes"] == 2
    assert front_step["meta"]["composite_mode"] == "hard"
    selfie_step = manifest.get_step(record.relative_key, "selfie_expand")
    assert selfie_step["meta"]["composite_mode"] == "feathered"


def test_pipeline_front_expand_runs_single_pass_when_configured(tmp_path: Path, monkeypatch):
    case_dir = tmp_path / "case-n3"
    case_dir.mkdir()
    front = case_dir / "front.png"
    Image.new("RGB", (800, 600), (1, 2, 3)).save(front)
    record = CaseRecord(case_dir=case_dir, front_path=front, relative_key="case-n3")

    config = merge_automation_defaults(
        {"falai_api_key": "x", "bfl_api_key": "bfl-token", "automation_oldcam_required": False, "automation_front_expand_passes": 1}
    )
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
    assert len(outpaint.calls) == 2
    front_step = manifest.get_step(record.relative_key, "front_expand")
    assert front_step["meta"]["configured_passes"] == 1
    assert front_step["meta"]["executed_passes"] == 1


def test_pipeline_selfie_expand_reuse_skips_outpaint_call(tmp_path: Path, monkeypatch):
    case_dir = tmp_path / "case-o"
    case_dir.mkdir()
    front = case_dir / "front.png"
    Image.new("RGB", (128, 128), (11, 11, 11)).save(front)
    existing_expanded = case_dir / "gen-images" / "already-expanded.png"
    existing_expanded.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (128, 128), (22, 22, 22)).save(existing_expanded)
    record = CaseRecord(case_dir=case_dir, front_path=front, relative_key="case-o")

    config = merge_automation_defaults({"falai_api_key": "x",
            "bfl_api_key": "bfl-token",
            "automation_oldcam_required": False,
            "automation_selfie_expand_composite_mode": "none"})
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
    assert selfie_expand_step["meta"]["composite_mode"] == "none"
    assert len(outpaint.calls) == 2


def test_pipeline_marks_active_selfie_step_failed_on_exception(tmp_path: Path, monkeypatch):
    case_dir = tmp_path / "case-p"
    case_dir.mkdir()
    front = case_dir / "front.png"
    Image.new("RGB", (64, 64), (1, 1, 1)).save(front)
    existing_selfie = case_dir / "gen-images" / "candidate.png"
    existing_selfie.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (64, 64), (2, 2, 2)).save(existing_selfie)
    record = CaseRecord(case_dir=case_dir, front_path=front, relative_key="case-p")

    config = merge_automation_defaults({"falai_api_key": "x",
            "bfl_api_key": "bfl-token",
            "automation_oldcam_required": False})
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


def test_pipeline_similarity_backend_error_marks_manual_review_unavailable(tmp_path: Path, monkeypatch):
    case_dir = tmp_path / "case-sim-error"
    case_dir.mkdir()
    front = case_dir / "front.png"
    Image.new("RGB", (64, 64), (1, 1, 1)).save(front)
    record = CaseRecord(case_dir=case_dir, front_path=front, relative_key="case-sim-error")

    config = merge_automation_defaults({"falai_api_key": "x",
            "bfl_api_key": "bfl-token",
            "automation_oldcam_required": False})
    manifest = AutomationManifest.create_or_load(tmp_path / "automation_manifest.json", tmp_path, {})
    manifest.ensure_case(record.relative_key, record.case_dir, record.front_path)
    monkeypatch.setattr("automation.pipeline.extract_portrait_crop", lambda **kwargs: {"confidence": 0.9, "crop_box": [0, 0, 10, 10], "extractor": "mock"})
    monkeypatch.setattr(
        "automation.pipeline.compute_face_similarity_details",
        lambda *args, **kwargs: {"score": 0, "pass": False, "error": "backend unavailable", "match": False},
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
    assert stats["manual_review"] == 1
    gate = manifest.data["cases"][record.relative_key]["steps"]["similarity_gate"]
    assert gate["status"] == "manual_review"
    assert "similarity unavailable: backend unavailable" == gate["error"]
    assert gate["meta"]["error"] == "backend unavailable"


def test_pipeline_passes_selected_selfie_prompt_slot(tmp_path: Path, monkeypatch):
    case_dir = tmp_path / "case-selfie-slot"
    case_dir.mkdir()
    front = case_dir / "front.png"
    Image.new("RGB", (64, 64), (1, 1, 1)).save(front)
    record = CaseRecord(case_dir=case_dir, front_path=front, relative_key="case-selfie-slot")

    config = merge_automation_defaults(
        {
            "falai_api_key": "x",
            "bfl_api_key": "bfl-token",
            "automation_oldcam_required": False,
            "bfl_api_key": "bfl-token",
            "automation_selfie_prompt_slot": 2,
            "automation_selfie_prompts": {"1": "slot1", "2": "slot2 prompt identity preserve"},
        }
    )
    manifest = AutomationManifest.create_or_load(tmp_path / "automation_manifest.json", tmp_path, {})
    manifest.ensure_case(record.relative_key, record.case_dir, record.front_path)
    monkeypatch.setattr("automation.pipeline.extract_portrait_crop", lambda **kwargs: {"confidence": 0.9, "crop_box": [0, 0, 10, 10], "extractor": "mock"})
    monkeypatch.setattr("automation.pipeline.compute_face_similarity_details", lambda *args, **kwargs: {"score": 90, "pass": True, "error": None, "match": True})
    monkeypatch.setattr("automation.pipeline.run_oldcam", lambda **kwargs: None)
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
    stats = runner.run([record])
    assert stats["completed"] == 1
    assert selfie.last_prompt == "slot2 prompt identity preserve"


def test_pipeline_missing_manifest_video_skips_optional_oldcam_without_call(tmp_path: Path, monkeypatch):
    case_dir = tmp_path / "case-q"
    case_dir.mkdir()
    front = case_dir / "front.png"
    Image.new("RGB", (64, 64), (8, 8, 8)).save(front)
    record = CaseRecord(case_dir=case_dir, front_path=front, relative_key="case-q")

    config = merge_automation_defaults(
        {"falai_api_key": "x",
            "bfl_api_key": "bfl-token",
            "automation_oldcam_required": False, "automation_skip_if_video_exists": False, "automation_video_enabled": False}
    )
    manifest = AutomationManifest.create_or_load(tmp_path / "automation_manifest.json", tmp_path, {})
    manifest.ensure_case(record.relative_key, record.case_dir, record.front_path)
    missing_video = case_dir / "gen-videos" / "missing.mp4"
    manifest.update_step(record.relative_key, "video_generate", "complete", output=str(missing_video))
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
    oldcam_step = manifest.data["cases"][record.relative_key]["steps"]["oldcam"]
    assert oldcam_step["status"] == "skipped"
    assert "missing or non-mp4" in (oldcam_step.get("error") or "")


def test_pipeline_missing_manifest_video_fails_required_oldcam_without_call(tmp_path: Path, monkeypatch):
    case_dir = tmp_path / "case-r"
    case_dir.mkdir()
    front = case_dir / "front.png"
    Image.new("RGB", (64, 64), (9, 9, 9)).save(front)
    record = CaseRecord(case_dir=case_dir, front_path=front, relative_key="case-r")

    config = merge_automation_defaults(
        {
            "falai_api_key": "x",
            "bfl_api_key": "bfl-token",
            "automation_oldcam_required": False,
            "bfl_api_key": "bfl-token",
            "automation_oldcam_required": True,
            "automation_skip_if_video_exists": False,
            "automation_video_enabled": False,
        }
    )
    manifest = AutomationManifest.create_or_load(tmp_path / "automation_manifest.json", tmp_path, {})
    manifest.ensure_case(record.relative_key, record.case_dir, record.front_path)
    missing_video = case_dir / "gen-videos" / "missing.mp4"
    manifest.update_step(record.relative_key, "video_generate", "complete", output=str(missing_video))
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
    assert stats["failed"] == 1
    assert oldcam_called["value"] is False
    oldcam_step = manifest.data["cases"][record.relative_key]["steps"]["oldcam"]
    assert oldcam_step["status"] == "failed"
    assert "missing or non-mp4" in (oldcam_step.get("error") or "")


def test_pipeline_selfie_expand_failure_is_terminal(tmp_path: Path, monkeypatch):
    case_dir = tmp_path / "case-s"
    case_dir.mkdir()
    front = case_dir / "front.png"
    Image.new("RGB", (64, 64), (1, 1, 1)).save(front)
    record = CaseRecord(case_dir=case_dir, front_path=front, relative_key="case-s")

    config = merge_automation_defaults({"falai_api_key": "x",
            "bfl_api_key": "bfl-token",
            "automation_oldcam_required": False, "automation_selfie_expand_enabled": True})
    manifest = AutomationManifest.create_or_load(tmp_path / "automation_manifest.json", tmp_path, {})
    manifest.ensure_case(record.relative_key, record.case_dir, record.front_path)
    monkeypatch.setattr("automation.pipeline.extract_portrait_crop", lambda **kwargs: {"confidence": 0.9, "crop_box": [0, 0, 10, 10], "extractor": "mock"})
    monkeypatch.setattr("automation.pipeline.compute_face_similarity_details", lambda *args, **kwargs: {"score": 99, "pass": True, "error": None, "match": True})
    monkeypatch.setattr("automation.pipeline.run_oldcam", lambda **kwargs: None)

    class FailingSelfieExpand(FakeOutpaint):
        def outpaint(self, image_path, output_folder, output_path=None, **kwargs):
            if "expand_left" in kwargs and "extracted_sim85" in str(image_path):
                return None
            return super().outpaint(image_path, output_folder, output_path=output_path, **kwargs)

    video_called = {"value": False}

    class SpyVideo(FakeVideo):
        def create_kling_generation(self, *args, **kwargs):
            video_called["value"] = True
            return super().create_kling_generation(*args, **kwargs)

    runner = AutoPipelineRunner(
        config=config,
        automation_config=from_app_config(config),
        manifest=manifest,
        progress_cb=lambda msg, level="info": None,
        deps=PipelineDeps(
            outpaint_factory=lambda: FailingSelfieExpand(),
            selfie_factory=lambda: FakeSelfie(),
            video_factory=lambda: SpyVideo(),
        ),
    )
    stats = runner.run([record])
    assert stats["failed"] == 1
    assert video_called["value"] is False
    assert manifest.data["cases"][record.relative_key]["steps"]["selfie_expand"]["status"] == "failed"


def test_pipeline_extract_reuse_meta_keeps_reused_existing_true(tmp_path: Path, monkeypatch):
    case_dir = tmp_path / "case-t"
    case_dir.mkdir()
    front = case_dir / "front.png"
    Image.new("RGB", (64, 64), (2, 2, 2)).save(front)
    extracted = case_dir / "extracted.png"
    Image.new("RGB", (64, 64), (3, 3, 3)).save(extracted)
    record = CaseRecord(case_dir=case_dir, front_path=front, relative_key="case-t")

    config = merge_automation_defaults({"falai_api_key": "x",
            "bfl_api_key": "bfl-token",
            "automation_oldcam_required": False})
    manifest = AutomationManifest.create_or_load(tmp_path / "automation_manifest.json", tmp_path, {})
    manifest.ensure_case(record.relative_key, record.case_dir, record.front_path)
    manifest.update_step(record.relative_key, "extract_portrait", "complete", output=str(extracted), meta={"extractor": "cached"})
    monkeypatch.setattr("automation.pipeline.compute_face_similarity_details", lambda *args, **kwargs: {"score": 99, "pass": True, "error": None, "match": True})
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
    meta = manifest.data["cases"][record.relative_key]["steps"]["extract_portrait"]["meta"]
    assert meta["reused_existing"] is True


def test_pipeline_oldcam_falls_back_to_existing_video_when_manifest_video_is_stale(tmp_path: Path, monkeypatch):
    case_dir = tmp_path / "case-u"
    case_dir.mkdir()
    front = case_dir / "front.png"
    Image.new("RGB", (64, 64), (4, 4, 4)).save(front)
    video_dir = case_dir / "gen-videos"
    video_dir.mkdir()
    existing_video = video_dir / "existing.mp4"
    existing_video.write_bytes(b"video")
    record = CaseRecord(case_dir=case_dir, front_path=front, relative_key="case-u")

    config = merge_automation_defaults(
        {
            "falai_api_key": "x",
            "bfl_api_key": "bfl-token",
            "automation_oldcam_required": False,
            "automation_video_enabled": False,
        }
    )
    manifest = AutomationManifest.create_or_load(tmp_path / "automation_manifest.json", tmp_path, {})
    manifest.ensure_case(record.relative_key, record.case_dir, record.front_path)
    manifest.update_step(
        record.relative_key,
        "video_generate",
        "complete",
        output=str(video_dir / "stale.mp4"),
    )
    monkeypatch.setattr("automation.pipeline.extract_portrait_crop", lambda **kwargs: {"confidence": 0.9, "crop_box": [0, 0, 10, 10], "extractor": "mock"})
    monkeypatch.setattr("automation.pipeline.compute_face_similarity_details", lambda *args, **kwargs: {"score": 95, "pass": True, "error": None, "match": True})

    called = {"video": None}

    def _run_oldcam(**kwargs):
        called["video"] = str(kwargs.get("video_path"))
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
    assert called["video"] == str(existing_video)


def _make_runner_for_bool_test(automation_overrides: dict, tmp_path: Path) -> AutoPipelineRunner:
    """Helper: build a minimal AutoPipelineRunner for direct _read_bool tests."""
    config = merge_automation_defaults({
        "falai_api_key": "x",
        "bfl_api_key": "bfl-token",
        "saved_prompts": {"1": "prompt"},
        "current_prompt_slot": 1,
        **automation_overrides,
    })
    manifest = AutomationManifest.create_or_load(tmp_path / "manifest.json", tmp_path, {})
    return AutoPipelineRunner(
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


def test_read_bool_treats_string_false_as_false(tmp_path: Path):
    """Coderabbit Major finding on PR #19: bool('false') is True in Python.
    The prior bool(self.automation.get(...)) at automation/pipeline.py:663
    would silently enable strict spoof gating whenever a user wrote "false"
    (string) in their config — opposite of the intended behavior. _read_bool
    routes through face_similarity._parse_bool to handle string inputs
    correctly."""
    runner = _make_runner_for_bool_test({
        "automation_similarity_require_fas_pass": "false",
    }, tmp_path)
    assert runner._read_bool("automation_similarity_require_fas_pass", True) is False


def test_read_bool_treats_string_true_as_true(tmp_path: Path):
    runner = _make_runner_for_bool_test({
        "automation_similarity_require_fas_pass": "true",
    }, tmp_path)
    assert runner._read_bool("automation_similarity_require_fas_pass", False) is True


def test_read_bool_returns_default_for_unknown_string(tmp_path: Path):
    runner = _make_runner_for_bool_test({
        "automation_similarity_require_fas_pass": "garbage",
    }, tmp_path)
    # _parse_bool returns None for unparseable strings → fall back to default.
    assert runner._read_bool("automation_similarity_require_fas_pass", True) is True
    assert runner._read_bool("automation_similarity_require_fas_pass", False) is False


def test_read_bool_passes_through_actual_booleans(tmp_path: Path):
    runner = _make_runner_for_bool_test({
        "automation_similarity_require_fas_pass": True,
    }, tmp_path)
    assert runner._read_bool("automation_similarity_require_fas_pass", False) is True

    runner2 = _make_runner_for_bool_test({
        "automation_similarity_require_fas_pass": False,
    }, tmp_path)
    assert runner2._read_bool("automation_similarity_require_fas_pass", True) is False


# --- Face-track gate (Step 6.5) -------------------------------------------
# Empirical basis: docs/analysis/versailles_fail_vs_pass.md. The gate runs
# after video_generate, before oldcam, on the Kling source. It must:
#  (a) degrade to a non-blocking skip when cv2/mediapipe unavailable,
#  (b) route to manual_review when sub-threshold + not required,
#  (c) hard-fail when sub-threshold + required,
#  (d) skip cleanly when disabled,
#  (e) let the case complete when the source tracks above threshold.

from automation.face_track_gate import FaceTrackResult


def _ft_runner(tmp_path: Path, monkeypatch, overrides: dict):
    case_dir = tmp_path / "case-ft"
    case_dir.mkdir()
    front = case_dir / "front.png"
    Image.new("RGB", (64, 64), (1, 1, 1)).save(front)
    record = CaseRecord(case_dir=case_dir, front_path=front, relative_key="case-ft")
    config = merge_automation_defaults({
        "falai_api_key": "x", "bfl_api_key": "bfl-token",
        "automation_oldcam_required": False,
        "saved_prompts": {"1": "prompt"}, "current_prompt_slot": 1,
        # Face-track gate now defaults OFF (large-corpus negative, see
        # docs/analysis/versailles_fail_vs_pass.md). These tests exercise
        # the RETAINED opt-in gate code, so enable it by default here;
        # a test can still override it back off via overrides.
        "automation_facetrack_enabled": True,
        **overrides,
    })
    manifest = AutomationManifest.create_or_load(
        tmp_path / "automation_manifest.json", tmp_path, {})
    manifest.ensure_case(record.relative_key, record.case_dir, record.front_path)
    monkeypatch.setattr("automation.pipeline.extract_portrait_crop",
                        lambda **kw: {"confidence": 0.9, "crop_box": [0, 0, 10, 10], "extractor": "mock"})
    monkeypatch.setattr("automation.pipeline.compute_face_similarity_details",
                        lambda *a, **k: {"score": 90, "pass": True, "error": None, "match": True})
    monkeypatch.setattr("automation.pipeline.run_oldcam", lambda **kw: None)
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


def test_facetrack_gate_degrades_when_unavailable(tmp_path: Path, monkeypatch):
    """Tooling unavailable -> gate skips, case still completes (non-blocking)."""
    monkeypatch.setattr(
        "automation.face_track_gate.measure_face_track",
        lambda *a, **k: FaceTrackResult(False, reason="cv2/mediapipe unavailable"),
    )
    runner, record, manifest = _ft_runner(tmp_path, monkeypatch, {})
    stats = runner.run([record])
    assert stats["completed"] == 1
    assert manifest.data["cases"]["case-ft"]["steps"]["facetrack_gate"]["status"] == "skipped"


def test_facetrack_gate_manual_review_when_sub_threshold(tmp_path: Path, monkeypatch):
    """Sub-threshold + not required -> manual_review (advisory default)."""
    monkeypatch.setattr(
        "automation.face_track_gate.measure_face_track",
        lambda *a, **k: FaceTrackResult(
            True, track_pct=70.0, sampled=80, with_face=56,
            passed=False, reason="face-track 70.0% < 96.0% threshold"),
    )
    runner, record, manifest = _ft_runner(
        tmp_path, monkeypatch, {"automation_facetrack_required": False})
    stats = runner.run([record])
    assert stats["manual_review"] == 1
    assert manifest.data["cases"]["case-ft"]["steps"]["facetrack_gate"]["status"] == "manual_review"


def test_facetrack_gate_hard_fail_when_required(tmp_path: Path, monkeypatch):
    """Sub-threshold + required=true -> failed."""
    monkeypatch.setattr(
        "automation.face_track_gate.measure_face_track",
        lambda *a, **k: FaceTrackResult(
            True, track_pct=70.0, sampled=80, with_face=56,
            passed=False, reason="face-track 70.0% < 96.0% threshold"),
    )
    runner, record, manifest = _ft_runner(
        tmp_path, monkeypatch, {"automation_facetrack_required": True})
    stats = runner.run([record])
    assert stats["failed"] == 1
    assert manifest.data["cases"]["case-ft"]["steps"]["facetrack_gate"]["status"] == "failed"


def test_facetrack_gate_passes_above_threshold(tmp_path: Path, monkeypatch):
    """Above threshold -> gate complete, case completes."""
    monkeypatch.setattr(
        "automation.face_track_gate.measure_face_track",
        lambda *a, **k: FaceTrackResult(
            True, track_pct=100.0, sampled=80, with_face=80, passed=True),
    )
    runner, record, manifest = _ft_runner(tmp_path, monkeypatch, {})
    stats = runner.run([record])
    assert stats["completed"] == 1
    assert manifest.data["cases"]["case-ft"]["steps"]["facetrack_gate"]["status"] == "complete"


def test_facetrack_gate_skips_when_disabled(tmp_path: Path, monkeypatch):
    """Disabled -> skipped without invoking the measurer at all."""
    def _boom(*a, **k):
        raise AssertionError("measure_face_track must not run when gate disabled")
    monkeypatch.setattr("automation.face_track_gate.measure_face_track", _boom)
    runner, record, manifest = _ft_runner(
        tmp_path, monkeypatch, {"automation_facetrack_enabled": False})
    stats = runner.run([record])
    assert stats["completed"] == 1
    assert manifest.data["cases"]["case-ft"]["steps"]["facetrack_gate"]["status"] == "skipped"


def test_facetrack_gate_tolerates_invalid_config(tmp_path: Path, monkeypatch):
    """Garbage / out-of-range min_pct + fps must fall back to the validated
    defaults (96.0 / 8.0) via _read_float clamping — never crash a run."""
    seen = {}

    def _capture(video_path, repo_root, *, sample_fps, min_track_pct, **kw):
        seen["fps"] = sample_fps
        seen["min"] = min_track_pct
        return FaceTrackResult(
            True, track_pct=100.0, sampled=80, with_face=80, passed=True)

    monkeypatch.setattr(
        "automation.face_track_gate.measure_face_track", _capture)
    runner, record, manifest = _ft_runner(tmp_path, monkeypatch, {
        "automation_facetrack_min_pct": "not-a-number",
        "automation_facetrack_sample_fps": 999,  # out of [1,30]
    })
    stats = runner.run([record])
    assert stats["completed"] == 1
    # Bad string -> 96.0 default; out-of-range fps -> 8.0 default.
    assert seen["min"] == 96.0
    assert seen["fps"] == 8.0
    assert manifest.data["cases"]["case-ft"]["steps"]["facetrack_gate"]["status"] == "complete"


def test_measure_face_track_rejects_bad_args():
    """measure_face_track must surface invalid sample_fps/min_track_pct
    explicitly (available=False) rather than letting the division throw
    and silently degrade to a non-blocking pass (CodeRabbit PR #37)."""
    from automation.face_track_gate import measure_face_track

    repo = Path(__file__).resolve().parent.parent
    for bad in (0, -5, float("nan")):
        r = measure_face_track("x.mp4", repo, sample_fps=bad)
        assert r.available is False
        assert "invalid" in r.reason.lower()
    r2 = measure_face_track("x.mp4", repo, min_track_pct=150)
    assert r2.available is False and "invalid" in r2.reason.lower()
    r3 = measure_face_track("x.mp4", repo, min_track_pct=-1)
    assert r3.available is False
    # Non-numeric must not raise either.
    r4 = measure_face_track("x.mp4", repo, sample_fps="fast")  # type: ignore[arg-type]
    assert r4.available is False


def _mk_rppg_case(tmp_path, monkeypatch, *, rppg_enabled, rppg_required=False, rppg_returns="path"):
    """Shared rPPG-step pipeline harness. rppg_returns: "path" => mock
    run_rppg writes & returns <input>-rppg.mp4; "none" => returns None
    (graceful-skip path)."""
    case_dir = tmp_path / "rppg-case"
    case_dir.mkdir()
    front = case_dir / "front.png"
    Image.new("RGB", (64, 64), (1, 1, 1)).save(front)
    record = CaseRecord(case_dir=case_dir, front_path=front, relative_key="rppg-case")

    config = merge_automation_defaults(
        {
            "falai_api_key": "x",
            "bfl_api_key": "bfl-token",
            "saved_prompts": {"1": "prompt"},
            "current_prompt_slot": 1,
            # Oldcam off so rPPG runs on the video_generate output directly,
            # isolating the rPPG step under test.
            "automation_oldcam_enabled": False,
            "automation_oldcam_required": False,
            "automation_rppg_enabled": rppg_enabled,
            "automation_rppg_required": rppg_required,
        }
    )
    manifest = AutomationManifest.create_or_load(tmp_path / "automation_manifest.json", tmp_path, {})
    manifest.ensure_case(record.relative_key, record.case_dir, record.front_path)
    monkeypatch.setattr("automation.pipeline.extract_portrait_crop", lambda **kwargs: {"confidence": 0.9, "crop_box": [0, 0, 10, 10], "extractor": "mock"})
    monkeypatch.setattr("automation.pipeline.compute_face_similarity_details", lambda *args, **kwargs: {"score": 90, "pass": True, "error": None, "match": True})
    monkeypatch.setattr("automation.pipeline.run_oldcam", lambda **kwargs: None)

    def _fake_rppg(*, video_path, repo_root, progress_cb=None, timeout_seconds=600):
        del repo_root, progress_cb, timeout_seconds
        if rppg_returns == "none":
            return None
        out = Path(video_path).with_name(Path(video_path).stem + "-rppg" + Path(video_path).suffix)
        out.write_bytes(b"rppg")
        return out

    monkeypatch.setattr("automation.pipeline.run_rppg", _fake_rppg)

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
    step = manifest.data["cases"]["rppg-case"]["steps"].get("rppg", {})
    return stats, step


def test_pipeline_rppg_skipped_when_disabled_by_default(tmp_path: Path, monkeypatch):
    """rPPG defaults OFF: the step records 'skipped' with the disabled
    reason and the case still completes."""
    stats, step = _mk_rppg_case(tmp_path, monkeypatch, rppg_enabled=False)
    assert stats["failed"] == 0
    assert step.get("status") == "skipped"
    assert "rPPG disabled" in (step.get("error") or "")


def test_pipeline_rppg_runs_and_completes_when_enabled(tmp_path: Path, monkeypatch):
    """When enabled and the (mocked) injector yields output, the rppg step
    is 'complete' and points at the injected file."""
    stats, step = _mk_rppg_case(tmp_path, monkeypatch, rppg_enabled=True, rppg_returns="path")
    assert stats["failed"] == 0
    assert step.get("status") == "complete"
    assert str(step.get("output", "")).endswith("-rppg.mp4")


def test_pipeline_rppg_graceful_skip_when_no_output_and_not_required(tmp_path: Path, monkeypatch):
    """Enabled but injector returns nothing and not required => 'skipped',
    run still completes (never hard-fails). Mirrors the facetrack
    non-required precedent."""
    stats, step = _mk_rppg_case(tmp_path, monkeypatch, rppg_enabled=True, rppg_required=False, rppg_returns="none")
    assert stats["failed"] == 0
    assert step.get("status") == "skipped"


def test_pipeline_rppg_required_failure_marks_case_failed(tmp_path: Path, monkeypatch):
    """Enabled + required + injector yields nothing => case fails (opt-in
    strictness, parallel to oldcam_required)."""
    stats, step = _mk_rppg_case(tmp_path, monkeypatch, rppg_enabled=True, rppg_required=True, rppg_returns="none")
    assert stats["failed"] == 1
    assert step.get("status") == "failed"


def test_resolve_produced_output_handles_metric_suffix_rename(tmp_path):
    """The real injector renames our --output {stem}-rppg{ext} to
    {stem}-rppg - <snr>-<phase>-<temporal>-<motion>-<harmonic>{ext}
    regardless of --output (verified via oldcam-testing/rppg_harness.py
    against the real tool). resolve_produced_output must find the renamed
    file, not insist on the exact requested path."""
    from automation.rppg import resolve_produced_output

    requested = tmp_path / "clip-rppg.mp4"
    # Exact path present -> returned as-is.
    requested.write_bytes(b"a")
    assert resolve_produced_output(requested) == requested
    requested.unlink()

    # Only the metric-renamed sibling exists -> that is returned.
    renamed = tmp_path / "clip-rppg - 13.08-7.8-0.70-0.03-0.46.mp4"
    renamed.write_bytes(b"b")
    assert resolve_produced_output(requested) == renamed

    # Newest metric-renamed wins when several exist.
    import time
    older = tmp_path / "clip-rppg - 7.72-75.5-0.79-0.06-0.35.mp4"
    older.write_bytes(b"c")
    time.sleep(0.01)
    newest = tmp_path / "clip-rppg - 14.00-3.0-0.50-0.02-0.50.mp4"
    newest.write_bytes(b"d")
    assert resolve_produced_output(requested) == newest

    # Nothing matching -> None (graceful-skip path).
    empty = tmp_path / "sub"
    empty.mkdir()
    assert resolve_produced_output(empty / "x-rppg.mp4") is None


def test_resolve_produced_output_ignores_loose_siblings(tmp_path):
    """The resolver must match ONLY the injector's exact rename form
    '<stem> - <metrics><ext>' (space-hyphen-space), never a loose
    '<stem>-<anything><ext>' sibling or the input itself. Locks the
    self-review hardening (a greedy '<stem>*<ext>' glob could return the
    un-injected input on a re-run)."""
    from automation.rppg import resolve_produced_output

    requested = tmp_path / "clip-rppg.mp4"
    # A loose sibling that is NOT the metric-rename form must be ignored.
    (tmp_path / "clip-rppg-backup.mp4").write_bytes(b"x")
    (tmp_path / "clip-rppgX.mp4").write_bytes(b"y")
    assert resolve_produced_output(requested) is None

    # The real metric-rename form is picked.
    real = tmp_path / "clip-rppg - 13.08-7.8-0.70-0.03-0.46.mp4"
    real.write_bytes(b"z")
    assert resolve_produced_output(requested) == real


def test_pipeline_rppg_runs_on_reused_video_when_oldcam_disabled(tmp_path, monkeypatch):
    """Regression (Codex P2, PR #39): when skip_if_video_exists reuses an
    existing gen-videos output AND oldcam is disabled, the case used to
    short-circuit to 'completed' before Step 8 — so opt-in rPPG was
    silently skipped on the common resume path. With rPPG enabled it must
    now fall through and inject on the reused video."""
    case_dir = tmp_path / "case-reuse-rppg"
    case_dir.mkdir()
    front = case_dir / "front.png"
    Image.new("RGB", (64, 64), (1, 1, 1)).save(front)
    (case_dir / "gen-videos").mkdir()
    existing_video = case_dir / "gen-videos" / "existing.mp4"
    existing_video.write_bytes(b"video")
    record = CaseRecord(case_dir=case_dir, front_path=front, relative_key="case-reuse-rppg")

    config = merge_automation_defaults({
        "falai_api_key": "x", "bfl_api_key": "bfl-token",
        "saved_prompts": {"1": "prompt"}, "current_prompt_slot": 1,
        "automation_skip_if_video_exists": True,
        "automation_oldcam_enabled": False,
        "automation_oldcam_required": False,
        "automation_rppg_enabled": True,
        "automation_rppg_required": False,
    })
    manifest = AutomationManifest.create_or_load(tmp_path / "automation_manifest.json", tmp_path, {})
    manifest.ensure_case(record.relative_key, record.case_dir, record.front_path)
    monkeypatch.setattr("automation.pipeline.extract_portrait_crop", lambda **kwargs: {"confidence": 0.9, "crop_box": [0, 0, 10, 10], "extractor": "mock"})
    monkeypatch.setattr("automation.pipeline.compute_face_similarity_details", lambda *args, **kwargs: {"score": 90, "pass": True, "error": None, "match": True})
    monkeypatch.setattr("automation.pipeline.run_oldcam", lambda **kwargs: None)

    rppg_calls = []

    def _fake_rppg(*, video_path, repo_root, progress_cb=None, timeout_seconds=600):
        del repo_root, progress_cb, timeout_seconds
        rppg_calls.append(Path(video_path))
        out = Path(video_path).with_name(Path(video_path).stem + "-rppg" + Path(video_path).suffix)
        out.write_bytes(b"rppg")
        return out

    monkeypatch.setattr("automation.pipeline.run_rppg", _fake_rppg)

    runner = AutoPipelineRunner(
        config=config, automation_config=from_app_config(config), manifest=manifest,
        progress_cb=lambda msg, level="info": None,
        deps=PipelineDeps(outpaint_factory=lambda: FakeOutpaint(), selfie_factory=lambda: FakeSelfie(), video_factory=lambda: FakeVideo()),
    )
    stats = runner.run([record])
    assert stats["failed"] == 0
    # rPPG MUST have been invoked on the reused video.
    assert len(rppg_calls) == 1, "rPPG was skipped on the reuse path (the bug)"
    assert existing_video.name in rppg_calls[0].name or str(existing_video) in str(rppg_calls[0])
    step = manifest.data["cases"]["case-reuse-rppg"]["steps"].get("rppg", {})
    assert step.get("status") == "complete"


def test_stream_subprocess_with_timeout_edge_cases(tmp_path):
    """Permanent regression for the shared rPPG subprocess streamer
    (Codex P2, PR #39). The graceful-skip guarantee depends entirely on
    this: a child that stalls — including MID-LINE with no trailing
    newline — must still be killed on the wall clock, not block until
    EOF. Covers the failure mode the bare readline() loop had."""
    import subprocess
    import sys
    import time

    from automation.rppg import stream_subprocess_with_timeout

    def run(argv, timeout):
        return stream_subprocess_with_timeout(
            [sys.executable, "-c", argv], cwd=str(tmp_path), timeout_seconds=timeout
        )

    # Normal multi-line completion -> rc 0, all lines captured.
    rc, lines = run("print('a');print('b');print('c')", 10)
    assert rc == 0 and lines == ["a", "b", "c"]

    # Non-zero exit returns rc (NOT an exception).
    rc, lines = run("import sys;print('x');sys.exit(3)", 10)
    assert rc == 3 and lines == ["x"]

    # Silent hang: no output, no exit -> TimeoutExpired, killed fast.
    t0 = time.monotonic()
    with pytest.raises(subprocess.TimeoutExpired):
        run("import time;time.sleep(30)", 2)
    assert time.monotonic() - t0 < 8, "silent hang not killed near the deadline"

    # Mid-line stall: writes WITHOUT a newline then hangs forever. This
    # is the exact scenario a bare readline() loop could not time out.
    t0 = time.monotonic()
    with pytest.raises(subprocess.TimeoutExpired):
        run(
            "import sys,time;sys.stdout.write('partial');sys.stdout.flush();time.sleep(30)",
            2,
        )
    assert time.monotonic() - t0 < 8, "mid-line stall not killed near the deadline"

    # Rapid output then clean exit -> reader thread keeps up.
    rc, lines = run("[print(i) for i in range(300)]", 10)
    assert rc == 0 and len(lines) == 300
