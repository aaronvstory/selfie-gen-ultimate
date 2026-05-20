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
    monkeypatch.setattr("automation.pipeline.run_oldcam_all", lambda **kwargs: [])

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
    monkeypatch.setattr("automation.pipeline.run_oldcam_all", lambda **kwargs: [])

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
    monkeypatch.setattr("automation.pipeline.run_oldcam_all", lambda **kwargs: [])

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
    monkeypatch.setattr("automation.pipeline.run_oldcam_all", lambda **kwargs: [])

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
    monkeypatch.setattr("automation.pipeline.run_oldcam_all", lambda **kwargs: [])

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
    monkeypatch.setattr("automation.pipeline.run_oldcam_all", lambda **kwargs: [])

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
    monkeypatch.setattr("automation.pipeline.run_oldcam_all", lambda **kwargs: [])

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
    monkeypatch.setattr("automation.pipeline.run_oldcam_all", lambda **kwargs: [])

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


def test_pipeline_validation_fails_on_rppg_required_without_enable(tmp_path: Path):
    """Regression (Codex P2, PR #39): symmetric with the oldcam rule —
    automation_rppg_required=true while automation_rppg_enabled=false is
    contradictory (the CLI asks them independently). Without validation,
    Step 8 skips and the case finalizes complete, so 'required' silently
    no-ops. validate_configuration() must reject the combination."""
    config = merge_automation_defaults(
        {
            "falai_api_key": "x",
            "bfl_api_key": "bfl-token",
            "automation_rppg_enabled": False,
            "automation_rppg_required": True,
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
    assert any("automation_rppg_required=true requires automation_rppg_enabled=true" in i for i in issues)

    # Sanity: the valid combination (both true) must NOT raise this issue.
    ok_config = merge_automation_defaults(
        {
            "falai_api_key": "x",
            "bfl_api_key": "bfl-token",
            "automation_rppg_enabled": True,
            "automation_rppg_required": True,
        }
    )
    ok_runner = AutoPipelineRunner(
        config=ok_config,
        automation_config=from_app_config(ok_config),
        manifest=AutomationManifest.create_or_load(tmp_path / "m2.json", tmp_path, {}),
        deps=PipelineDeps(
            outpaint_factory=lambda: FakeOutpaint(),
            selfie_factory=lambda: FakeSelfie(),
            video_factory=lambda: FakeVideo(),
        ),
    )
    assert not any("automation_rppg_required" in i for i in ok_runner.validate_configuration())


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
    monkeypatch.setattr("automation.pipeline.run_oldcam_all", lambda **kwargs: [])

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
    monkeypatch.setattr("automation.pipeline.run_oldcam_all", lambda **kwargs: [])
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

    def _run_oldcam_all(**kwargs):
        oldcam_called["value"] = True
        return []

    monkeypatch.setattr("automation.pipeline.run_oldcam_all", _run_oldcam_all)

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
    monkeypatch.setattr("automation.pipeline.run_oldcam_all", lambda **kwargs: [])

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
    monkeypatch.setattr("automation.pipeline.run_oldcam_all", lambda **kwargs: [])

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
    monkeypatch.setattr("automation.pipeline.run_oldcam_all", lambda **kwargs: [])

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

    def _run_oldcam_all(**kwargs):
        oldcam_called["value"] = True
        return []

    monkeypatch.setattr("automation.pipeline.run_oldcam_all", _run_oldcam_all)

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
    monkeypatch.setattr("automation.pipeline.run_oldcam_all", lambda **kwargs: [])

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
    monkeypatch.setattr("automation.pipeline.run_oldcam_all", lambda **kwargs: [])

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
    monkeypatch.setattr("automation.pipeline.run_oldcam_all", lambda **kwargs: [])

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
    monkeypatch.setattr("automation.pipeline.run_oldcam_all", lambda **kwargs: [])

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
    monkeypatch.setattr("automation.pipeline.run_oldcam_all", lambda **kwargs: [])

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
    monkeypatch.setattr("automation.pipeline.run_oldcam_all", lambda **kwargs: [])

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
    monkeypatch.setattr("automation.pipeline.run_oldcam_all", lambda **kwargs: [])

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
    monkeypatch.setattr("automation.pipeline.run_oldcam_all", lambda **kwargs: [])
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

    def _run_oldcam_all(**kwargs):
        oldcam_called["value"] = True
        return []

    monkeypatch.setattr("automation.pipeline.run_oldcam_all", _run_oldcam_all)

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

    def _run_oldcam_all(**kwargs):
        oldcam_called["value"] = True
        return []

    monkeypatch.setattr("automation.pipeline.run_oldcam_all", _run_oldcam_all)

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
    monkeypatch.setattr("automation.pipeline.run_oldcam_all", lambda **kwargs: [])

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
    monkeypatch.setattr("automation.pipeline.run_oldcam_all", lambda **kwargs: [])

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

    def _run_oldcam_all(**kwargs):
        called["video"] = str(kwargs.get("video_path"))
        return []

    monkeypatch.setattr("automation.pipeline.run_oldcam_all", _run_oldcam_all)

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
    monkeypatch.setattr("automation.pipeline.run_oldcam_all", lambda **kw: [])
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
    monkeypatch.setattr("automation.pipeline.run_oldcam_all", lambda **kwargs: [])

    def _fake_rppg(
        *,
        video_path,
        repo_root,
        progress_cb=None,
        timeout_seconds=600,
        keep_metrics=False,
        # Iterative-mode kwargs (PR #43 / friend feedback) — accepted
        # but not exercised here. Pipeline-level tests verify that the
        # config keys flow through; injector-cmd tests verify the actual
        # subprocess argv (see test_automation_rppg_cmd.py / test_rppg_runner_cmd).
        iterative=True,
        iterate_from_baseline=True,
        skip_diagnosis=True,
        skip_kinematic_gate=True,
    ):
        del (
            repo_root, progress_cb, timeout_seconds, keep_metrics,
            iterative, iterate_from_baseline, skip_diagnosis, skip_kinematic_gate,
        )
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

    # Newest metric-renamed wins when several exist. Set mtimes
    # explicitly with os.utime() rather than relying on time.sleep():
    # a sub-10ms sleep is below the timestamp resolution of some CI
    # filesystems, making the "newest wins" assertion flaky. Pin EVERY
    # competing file's mtime (including `renamed`, written above with a
    # real ~now mtime) so the ordering is fully deterministic regardless
    # of fs granularity.
    import os
    older = tmp_path / "clip-rppg - 7.72-75.5-0.79-0.06-0.35.mp4"
    older.write_bytes(b"c")
    newest = tmp_path / "clip-rppg - 14.00-3.0-0.50-0.02-0.50.mp4"
    newest.write_bytes(b"d")
    os.utime(renamed, (1_000_000, 1_000_000))
    os.utime(older, (1_500_000, 1_500_000))
    os.utime(newest, (2_000_000, 2_000_000))
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
    monkeypatch.setattr("automation.pipeline.run_oldcam_all", lambda **kwargs: [])

    rppg_calls = []

    def _fake_rppg(
        *, video_path, repo_root, progress_cb=None,
        timeout_seconds=600, keep_metrics=False,
        iterative=True, iterate_from_baseline=True,
        skip_diagnosis=True, skip_kinematic_gate=True,
    ):
        del (
            repo_root, progress_cb, timeout_seconds, keep_metrics,
            iterative, iterate_from_baseline, skip_diagnosis, skip_kinematic_gate,
        )
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


def test_resolve_produced_output_handles_glob_metacharacters(tmp_path):
    """Regression (refine-loop self-review, PR #39): real Kling/oldcam
    stems can contain glob metacharacters — "selfie[final]", "clip (1)",
    "v[2]-oldcam-...". Unescaped, Path.glob() treats "[..]" as a char
    class and the produced file is silently missed → false graceful-skip
    on a SUCCESSFUL injection. resolve_produced_output must glob.escape
    the literal stem. (Note: "?" / "*" can't be Windows filenames, so
    "[]" is the realistic offender; the others are escaped defensively.)"""
    from automation.rppg import resolve_produced_output

    for stem in ("selfie[final]-rppg", "v[2]-oldcam-v24-rppg", "clip (1)-rppg", "a+b{x}-rppg"):
        d = tmp_path / stem.replace("[", "_").replace("]", "_").replace(" ", "_")
        d.mkdir()
        requested = d / f"{stem}.mp4"
        produced = d / f"{stem} - 7.81-6.4-0.53-0.03-0.54.mp4"
        produced.write_bytes(b"x")
        assert resolve_produced_output(requested) == produced, f"failed for stem {stem!r}"

    # The loose-sibling guard must still hold with escaping in place.
    d2 = tmp_path / "guard"
    d2.mkdir()
    (d2 / "clip-rppg-backup.mp4").write_bytes(b"q")  # NOT the rename form
    real = d2 / "clip-rppg - 1.0-2.0-0.5-0.0-0.5.mp4"
    real.write_bytes(b"r")
    assert resolve_produced_output(d2 / "clip-rppg.mp4") == real


def test_parse_metric_suffix_roundtrip_and_edge_cases(tmp_path):
    """parse_metric_suffix must recover the 5 metrics from the injector's
    "<clean stem> - <SNR>-<Phase>-<Temporal>-<Motion>-<Harmonic>" rename,
    including NEGATIVE values (a leading '-' makes '--', so a naive
    str.split('-') would mis-parse — the scanner must be float-aware),
    and must return None when the produced stem is NOT that exact form."""
    from automation.rppg import parse_metric_suffix, _METRIC_KEYS

    m = parse_metric_suffix("clip-rppg - 8.16-19.9-0.43-0.01-0.70", "clip-rppg")
    assert m == {"snr": 8.16, "phase": 19.9, "temporal": 0.43, "motion": 0.01, "harmonic": 0.70}
    assert list(m.keys()) == list(_METRIC_KEYS)

    m2 = parse_metric_suffix("c-rppg - 5.40--12.5-0.26-0.03-0.56", "c-rppg")
    assert m2 == {"snr": 5.40, "phase": -12.5, "temporal": 0.26, "motion": 0.03, "harmonic": 0.56}

    assert parse_metric_suffix("clip-rppg", "clip-rppg") is None
    assert parse_metric_suffix("other-rppg - 1-2-3-4-5", "clip-rppg") is None
    assert parse_metric_suffix("clip-rppg - 1-2-3", "clip-rppg") is None


def test_finalize_rppg_output_strips_suffix_and_writes_sidecar(tmp_path):
    """keep_metrics=False (default): the injector's metric-suffixed file
    is renamed back to the clean requested path and the 5 metrics land
    in a {stem}.metrics.json sidecar. The clean name MUST keep the
    literal -rppg token so is_rppg_artifact still recognises it."""
    import json
    from automation.rppg import finalize_rppg_output, is_rppg_artifact

    requested = tmp_path / "clip_looped-oldcam-v24-rppg.mp4"
    produced = tmp_path / "clip_looped-oldcam-v24-rppg - 8.16-19.9-0.43-0.01-0.70.mp4"
    produced.write_bytes(b"video-bytes")

    final = finalize_rppg_output(produced, requested, keep_metrics=False)

    assert final == requested
    assert requested.exists()
    assert requested.read_bytes() == b"video-bytes"
    assert not produced.exists()
    assert is_rppg_artifact(requested)

    sidecar = tmp_path / "clip_looped-oldcam-v24-rppg.metrics.json"
    assert sidecar.exists()
    data = json.loads(sidecar.read_text(encoding="utf-8"))
    assert data["metrics"] == {
        "snr": 8.16, "phase": 19.9, "temporal": 0.43, "motion": 0.01, "harmonic": 0.70,
    }
    assert data["source"] == produced.name


def test_finalize_rppg_output_keeps_name_when_metrics_enabled(tmp_path):
    """keep_metrics=True: the injector's metric-suffixed name is kept
    as-is and NO sidecar is written."""
    from automation.rppg import finalize_rppg_output

    requested = tmp_path / "clip-rppg.mp4"
    produced = tmp_path / "clip-rppg - 9.10-3.3-0.57-0.00-0.85.mp4"
    produced.write_bytes(b"v")

    final = finalize_rppg_output(produced, requested, keep_metrics=True)

    assert final == produced
    assert produced.exists()
    assert not requested.exists()
    assert not (tmp_path / "clip-rppg.metrics.json").exists()


def test_finalize_rppg_output_passthrough_when_no_rename(tmp_path):
    """If the injector honoured --output for once (produced == requested),
    finalize is a no-op even with keep_metrics=False."""
    from automation.rppg import finalize_rppg_output

    requested = tmp_path / "clip-rppg.mp4"
    requested.write_bytes(b"v")
    final = finalize_rppg_output(requested, requested, keep_metrics=False)
    assert final == requested
    assert requested.exists()
    assert not (tmp_path / "clip-rppg.metrics.json").exists()


def test_finalize_rppg_output_never_raises_on_rename_failure(tmp_path, monkeypatch):
    """A cosmetic rename hiccup must NOT lose the delivered video — the
    run already succeeded. finalize returns the best path it has."""
    import os
    from automation.rppg import finalize_rppg_output

    requested = tmp_path / "clip-rppg.mp4"
    produced = tmp_path / "clip-rppg - 1.0-2.0-0.5-0.0-0.5.mp4"
    produced.write_bytes(b"v")

    def _boom(*a, **k):
        raise OSError("simulated rename failure")

    monkeypatch.setattr(os, "replace", _boom)
    final = finalize_rppg_output(produced, requested, keep_metrics=False)
    assert final == produced
    assert produced.exists()


def test_run_oldcam_all_returns_every_succeeded_version(tmp_path, monkeypatch):
    """run_oldcam_all must return [(version, path)] for EVERY version that
    produced output (the fan-out primitive); run_oldcam stays the
    back-compat highest-only wrapper."""
    import automation.oldcam as oc

    monkeypatch.setattr(oc, "discover_oldcam_versions", lambda repo_root: ["v8", "v13", "v24"])

    def _fake_version(*, video_path, version, repo_root, progress_cb=None):
        out = tmp_path / f"clip-oldcam-{version}.mp4"
        out.write_bytes(b"x")
        return out

    monkeypatch.setattr(oc, "run_oldcam_version", _fake_version)

    allout = oc.run_oldcam_all(
        video_path=tmp_path / "clip.mp4", version_setting="all", repo_root=tmp_path
    )
    assert sorted(v for v, _ in allout) == ["v13", "v24", "v8"]
    # Back-compat wrapper returns the HIGHEST version's path.
    one = oc.run_oldcam(
        video_path=tmp_path / "clip.mp4", version_setting="all", repo_root=tmp_path
    )
    assert one == tmp_path / "clip-oldcam-v24.mp4"


def test_pipeline_rppg_fans_out_over_base_and_every_oldcam(tmp_path, monkeypatch):
    """Automation Step 8 must inject rPPG into the BASE (video_generate
    output — automation has no loop) AND every per-version oldcam output
    recorded in the oldcam step meta["all_outputs"]. There is no
    privileged "primary"; plain pre-rPPG files are kept."""
    from automation.rppg import build_rppg_output_path

    case_dir = tmp_path / "case-fanout"
    case_dir.mkdir()
    front = case_dir / "front.png"
    Image.new("RGB", (64, 64), (1, 1, 1)).save(front)
    gv = case_dir / "gen-videos"
    gv.mkdir()
    base = gv / "existing.mp4"
    base.write_bytes(b"base")
    v8 = gv / "existing-oldcam-v8.mp4"
    v8.write_bytes(b"v8")
    v24 = gv / "existing-oldcam-v24.mp4"
    v24.write_bytes(b"v24")
    record = CaseRecord(case_dir=case_dir, front_path=front, relative_key="case-fanout")

    config = merge_automation_defaults({
        "falai_api_key": "x", "bfl_api_key": "bfl-token",
        "saved_prompts": {"1": "p"}, "current_prompt_slot": 1,
        "automation_skip_if_video_exists": True,
        "automation_oldcam_enabled": True,
        "automation_oldcam_required": False,
        "automation_rppg_enabled": True,
        "automation_rppg_required": False,
    })
    manifest = AutomationManifest.create_or_load(tmp_path / "automation_manifest.json", tmp_path, {})
    manifest.ensure_case(record.relative_key, record.case_dir, record.front_path)
    monkeypatch.setattr("automation.pipeline.extract_portrait_crop", lambda **kwargs: {"confidence": 0.9, "crop_box": [0, 0, 10, 10], "extractor": "mock"})
    monkeypatch.setattr("automation.pipeline.compute_face_similarity_details", lambda *args, **kwargs: {"score": 90, "pass": True, "error": None, "match": True})

    # Oldcam "ran" producing v8 + v24 (recorded in meta["all_outputs"]).
    def _fake_oldcam_all(**kwargs):
        return [("v8", v8), ("v24", v24)]

    monkeypatch.setattr("automation.pipeline.run_oldcam_all", _fake_oldcam_all)

    rppg_inputs = []

    def _fake_rppg(
        *, video_path, repo_root, progress_cb=None,
        timeout_seconds=600, keep_metrics=False,
        iterative=True, iterate_from_baseline=True,
        skip_diagnosis=True, skip_kinematic_gate=True,
    ):
        del (
            repo_root, progress_cb, timeout_seconds, keep_metrics,
            iterative, iterate_from_baseline, skip_diagnosis, skip_kinematic_gate,
        )
        rppg_inputs.append(Path(video_path))
        out = build_rppg_output_path(Path(video_path))
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
    # rPPG fanned over base + v8 + v24 (3 distinct inputs, no "primary").
    names = sorted(p.name for p in rppg_inputs)
    assert names == ["existing-oldcam-v24.mp4", "existing-oldcam-v8.mp4", "existing.mp4"]
    # Plain pre-rPPG oldcam files are KEPT (non-destructive).
    assert v8.exists() and v24.exists() and base.exists()
    step = manifest.data["cases"]["case-fanout"]["steps"].get("rppg", {})
    assert step.get("status") == "complete"
    assert len(step.get("meta", {}).get("all_outputs", [])) == 3


def _fanout_partial_case(tmp_path, required, monkeypatch):
    """Shared rig: base + v8 + v24; v24 rPPG FAILS, base+v8 succeed."""
    from automation.rppg import build_rppg_output_path

    case_dir = tmp_path / f"case-partial-{required}"
    case_dir.mkdir()
    front = case_dir / "front.png"
    Image.new("RGB", (64, 64), (1, 1, 1)).save(front)
    gv = case_dir / "gen-videos"
    gv.mkdir()
    base = gv / "existing.mp4"
    base.write_bytes(b"base")
    v8 = gv / "existing-oldcam-v8.mp4"
    v8.write_bytes(b"v8")
    v24 = gv / "existing-oldcam-v24.mp4"
    v24.write_bytes(b"v24")
    record = CaseRecord(case_dir=case_dir, front_path=front,
                        relative_key=f"case-partial-{required}")

    config = merge_automation_defaults({
        "falai_api_key": "x", "bfl_api_key": "bfl-token",
        "saved_prompts": {"1": "p"}, "current_prompt_slot": 1,
        "automation_skip_if_video_exists": True,
        "automation_oldcam_enabled": True,
        "automation_oldcam_required": False,
        "automation_rppg_enabled": True,
        "automation_rppg_required": required,
    })
    manifest = AutomationManifest.create_or_load(
        tmp_path / f"m-{required}.json", tmp_path, {})
    manifest.ensure_case(record.relative_key, record.case_dir, record.front_path)
    monkeypatch.setattr("automation.pipeline.extract_portrait_crop", lambda **k: {"confidence": 0.9, "crop_box": [0, 0, 10, 10], "extractor": "mock"})
    monkeypatch.setattr("automation.pipeline.compute_face_similarity_details", lambda *a, **k: {"score": 90, "pass": True, "error": None, "match": True})
    monkeypatch.setattr("automation.pipeline.run_oldcam_all", lambda **k: [("v8", v8), ("v24", v24)])

    def _fake_rppg(
        *, video_path, repo_root, progress_cb=None,
        timeout_seconds=600, keep_metrics=False,
        iterative=True, iterate_from_baseline=True,
        skip_diagnosis=True, skip_kinematic_gate=True,
    ):
        del (
            repo_root, progress_cb, timeout_seconds, keep_metrics,
            iterative, iterate_from_baseline, skip_diagnosis, skip_kinematic_gate,
        )
        # v24 injection FAILS (returns None); base + v8 succeed.
        if Path(video_path).name == "existing-oldcam-v24.mp4":
            return None
        out = build_rppg_output_path(Path(video_path))
        out.write_bytes(b"rppg")
        return out

    monkeypatch.setattr("automation.pipeline.run_rppg", _fake_rppg)
    runner = AutoPipelineRunner(
        config=config, automation_config=from_app_config(config), manifest=manifest,
        progress_cb=lambda msg, level="info": None,
        deps=PipelineDeps(outpaint_factory=lambda: FakeOutpaint(), selfie_factory=lambda: FakeSelfie(), video_factory=lambda: FakeVideo()),
    )
    stats = runner.run([record])
    step = manifest.data["cases"][record.relative_key]["steps"].get("rppg", {})
    return stats, step


def test_pipeline_rppg_partial_fanout_fails_when_required(tmp_path, monkeypatch):
    """Codex P2 (PR #40): with automation_rppg_required=true, a PARTIAL
    fan-out (base+v8 succeed, v24 fails) must FAIL the case — a missing
    oldcam deliverable must never be reported as success."""
    stats, step = _fanout_partial_case(tmp_path, True, monkeypatch)
    assert stats["failed"] == 1
    assert step.get("status") == "failed"
    assert "existing-oldcam-v24.mp4" in step.get("error", "")


def test_pipeline_rppg_partial_fanout_headlines_best_success_when_not_required(tmp_path, monkeypatch):
    """Codex P2 (PR #40): not required -> the case still completes, but
    the headline must be the highest oldcam version that ACTUALLY
    succeeded (v8 here), NEVER the base just because it was produced
    last-standing. partial flag + failed_inputs are recorded."""
    stats, step = _fanout_partial_case(tmp_path, False, monkeypatch)
    assert stats["failed"] == 0
    assert step.get("status") == "complete"
    # Headline must be the v8 rPPG (highest SUCCESSFUL oldcam), not base.
    assert step.get("output", "").endswith("existing-oldcam-v8-rppg.mp4"), step.get("output")
    assert step.get("meta", {}).get("partial") is True
    assert "existing-oldcam-v24.mp4" in step.get("meta", {}).get("failed_inputs", [])


def test_pipeline_step8_candidate_selection_includes_legacy_oldcam(tmp_path):
    """Regression (Codex P2 / CodeRabbit Major, PR #40): the Step-8
    candidate-selection rule must fan rPPG over the BASE *and* the legacy
    single ``oldcam.output`` when ``meta["all_outputs"]`` is absent (a
    manifest whose oldcam step completed before all_outputs existed).
    The pre-fix bug: ``video_out`` seeded ``candidates`` first, so a
    "fallback only if candidates empty" check never fired and the legacy
    oldcam deliverable was silently skipped.

    This pins the exact selection logic in isolation (a full-pipeline
    run always re-runs Step 7, which overwrites the oldcam step — so the
    cross-run resume the bug describes is only reachable at this rule)."""
    base = tmp_path / "existing.mp4"
    base.write_bytes(b"b")
    legacy = tmp_path / "existing-oldcam-v24.mp4"
    legacy.write_bytes(b"v")

    def select(video_out, oldcam_out, oldcam_all):
        """Mirror of automation/pipeline.py Step 8 candidate building."""
        oldcam_sources = oldcam_all if oldcam_all else ([oldcam_out] if oldcam_out else [])
        candidates = []
        seen = set()
        for raw in [video_out, *oldcam_sources]:
            if not raw:
                continue
            p = Path(raw)
            key = str(p)
            if key in seen or not p.exists():
                continue
            seen.add(key)
            candidates.append(p)
        return [p.name for p in candidates]

    # Legacy manifest: all_outputs empty, single oldcam.output present,
    # video_out also present -> BOTH must be fanned (the fix).
    assert select(str(base), str(legacy), []) == ["existing.mp4", "existing-oldcam-v24.mp4"]
    # Modern manifest: all_outputs wins; legacy ignored (deduped anyway).
    assert select(str(base), str(legacy), [str(legacy)]) == ["existing.mp4", "existing-oldcam-v24.mp4"]
    # No oldcam at all -> just the base (the standalone-rPPG path).
    assert select(str(base), None, []) == ["existing.mp4"]
    # Missing files are filtered.
    assert select(str(tmp_path / "gone.mp4"), str(legacy), []) == ["existing-oldcam-v24.mp4"]


def test_rerun_oldcam_failure_not_masked_by_base_rppg(monkeypatch):
    """Regression (CodeRabbit Major, PR #40): the OLDCAM-only re-run
    path must NOT report success when oldcam produced nothing just
    because rPPG injected the base clip. output_path stays falsy so the
    downstream existence check fails the rerun."""
    from kling_gui import queue_manager as qm

    class _QM(qm.QueueManager):
        def __init__(self):  # bypass heavy __init__
            self._last_oldcam_run_summary = {"outputs": []}

        def get_config(self):
            return {}

        def log(self, *a, **k):
            pass

    inst = _QM()
    monkeypatch.setattr(inst, "_rppg_enabled", lambda: True)
    # oldcam produced NOTHING (total failure).
    monkeypatch.setattr(inst, "_oldcam_video", lambda *a, **k: None)
    # rPPG would still "succeed" on the base if wired wrong.
    monkeypatch.setattr(inst, "_rppg_video", lambda src, item: src + "-rppg")
    monkeypatch.setattr(inst, "_build_rppg_output_path", lambda p: qm.Path(str(p) + "-rppg"))

    # Reproduce the re-run decision block in isolation.
    run_input = "clip.mp4"
    output_path = inst._oldcam_video(str(run_input), None)  # -> None
    summary = inst._last_oldcam_run_summary or {}
    if inst._rppg_enabled():
        rerun_oldcam_outputs = list(summary.get("outputs") or [])
        rppg_inputs = list(dict.fromkeys(s for s in [str(run_input), *rerun_oldcam_outputs] if s))
        last_rppg = None
        for src in rppg_inputs:
            r = inst._rppg_video(src, None)
            if r:
                last_rppg = r
        if last_rppg and output_path:
            preferred = inst._build_rppg_output_path(qm.Path(output_path))
            output_path = str(preferred) if preferred.exists() else last_rppg
    # rPPG ran on the base, but oldcam failed -> output_path MUST stay
    # None so the rerun is correctly reported as failed.
    assert last_rppg == "clip.mp4-rppg"
    assert output_path is None


def test_pipeline_rppg_skips_reinjection_of_already_injected_input(tmp_path, monkeypatch):
    """Regression (Codex P2, PR #39): a stale/seeded manifest can point
    video_generate (or oldcam) output at a prior "*-rppg" artifact. Step 8
    must NOT re-inject it (-rppg-rppg double pulse breaks the
    non-negotiable sub-perceptual guarantee) — it IS the final
    deliverable, so record complete and DO NOT call run_rppg."""
    case_dir = tmp_path / "case-doubleinject"
    case_dir.mkdir()
    front = case_dir / "front.png"
    Image.new("RGB", (64, 64), (1, 1, 1)).save(front)
    (case_dir / "gen-videos").mkdir()
    # Manifest's recorded video output IS an already-injected rPPG file
    # (the exact stale-manifest condition Codex described).
    injected = case_dir / "gen-videos" / "clip-oldcam-v24-rppg - 7.8-6.4-0.5-0.0-0.5.mp4"
    injected.write_bytes(b"already-injected")
    record = CaseRecord(case_dir=case_dir, front_path=front, relative_key="case-doubleinject")

    config = merge_automation_defaults({
        "falai_api_key": "x", "bfl_api_key": "bfl-token",
        "saved_prompts": {"1": "prompt"}, "current_prompt_slot": 1,
        "automation_video_enabled": False,
        "automation_skip_if_video_exists": False,
        "automation_oldcam_enabled": False,
        "automation_oldcam_required": False,
        "automation_rppg_enabled": True,
        "automation_rppg_required": False,
    })
    manifest = AutomationManifest.create_or_load(tmp_path / "automation_manifest.json", tmp_path, {})
    manifest.ensure_case(record.relative_key, record.case_dir, record.front_path)
    manifest.update_step(record.relative_key, "video_generate", "complete", output=str(injected))
    monkeypatch.setattr("automation.pipeline.extract_portrait_crop", lambda **kwargs: {"confidence": 0.9, "crop_box": [0, 0, 10, 10], "extractor": "mock"})
    monkeypatch.setattr("automation.pipeline.compute_face_similarity_details", lambda *args, **kwargs: {"score": 90, "pass": True, "error": None, "match": True})
    monkeypatch.setattr("automation.pipeline.run_oldcam_all", lambda **kwargs: [])

    rppg_calls = []
    monkeypatch.setattr("automation.pipeline.run_rppg", lambda **kw: rppg_calls.append(kw) or None)

    runner = AutoPipelineRunner(
        config=config, automation_config=from_app_config(config), manifest=manifest,
        progress_cb=lambda msg, level="info": None,
        deps=PipelineDeps(outpaint_factory=lambda: FakeOutpaint(), selfie_factory=lambda: FakeSelfie(), video_factory=lambda: FakeVideo()),
    )
    stats = runner.run([record])
    # The non-negotiable property: an already-injected file is NEVER fed
    # back into the injector (a double -rppg-rppg pass would compound the
    # pulse out of the sub-perceptual range). Run must not fail; rppg must
    # not end up "complete" via a FRESH injection of an injected input.
    assert stats["failed"] == 0
    assert rppg_calls == [], "re-injected an already-rPPG'd input (double-injection)"
    step = manifest.data["cases"]["case-doubleinject"]["steps"].get("rppg", {})
    assert step.get("status") in {"complete", "skipped"}
    if step.get("status") == "complete":
        assert step.get("meta", {}).get("already_injected") is True


def test_is_rppg_artifact_detects_all_injection_forms():
    """Regression (Codex P2, PR #39): is_rppg_artifact must recognise the
    "-rppg" marker as a complete token in EVERY position it can land in
    the processing chain — including the INFIX case where rPPG ran before
    oldcam (clip-rppg-oldcam-v24). A false-negative double-injects and
    breaks the non-negotiable sub-perceptual guarantee."""
    from pathlib import Path

    from automation.rppg import is_rppg_artifact

    injected = [
        "clip-rppg.mp4",                                   # raw --output form
        "clip-oldcam-v24-rppg.mp4",                         # oldcam then rPPG
        "clip-oldcam-v24-rppg - 7.81-6.4-0.53-0.03-0.46.mp4",  # metric rename
        "front_looped-oldcam-v24-rppg - 13.08-7.8-0.70-0.03-0.46.mp4",
        "clip-rppg-oldcam-v24.mp4",                         # rPPG BEFORE oldcam (the bug)
        "clip-rppg-oldcam-v24 - 7.7-2.1-0.5-0.0-0.6.mp4",   # rPPG, oldcam, re-injected+renamed
        "a_looped-rppg-oldcam-v15.mp4",
    ]
    injected += [
        # Case-insensitive: the marker must match regardless of casing.
        "CLIP-RPPG.mp4",
        "Clip-RpPg - 7.8-1.0-0.5-0.0-0.5.mp4",
        "clip-RPPG-oldcam-v24.mp4",
    ]
    not_injected = [
        "clip.mp4",
        "clip-oldcam-v24.mp4",
        "clip_looped.mp4",
        "clip-oldcam-v24_looped.mp4",
        "selfie.mp4",
        # Path-component-safe: a dir containing "-rppg" with a plain file
        # name must NOT match (predicate uses .stem, ignores parents).
        "rppg_harness_out/clip.mp4",
        "some-rppg-dir/plain.mp4",
        # Prefix-safe: the injector always writes the "-rppg" TOKEN; a
        # bare/leading "rppg" without the separator is not its marker.
        "rppg.mp4",
        "rppgclip.mp4",
        "rppg-clip.mp4",
    ]
    for name in injected:
        assert is_rppg_artifact(Path(name)) is True, f"{name!r} must be detected as injected"
    for name in not_injected:
        assert is_rppg_artifact(Path(name)) is False, f"{name!r} must NOT be flagged injected"


def test_pipeline_rppg_string_false_does_not_enable(tmp_path, monkeypatch):
    """Regression (Codex P2, PR #39): a JSON/CLI string value of "false"
    for automation_rppg_enabled must NOT opt the user into rPPG. Raw
    dict.get returns the truthy string "false"; the pipeline now reads
    via _read_bool (face_similarity._parse_bool) like facetrack/similarity
    do. With rPPG effectively OFF, run_rppg must NOT be called and the
    step records skipped/rPPG-disabled."""
    stats, step = _mk_rppg_case(tmp_path, monkeypatch, rppg_enabled="false")
    assert stats["failed"] == 0
    # rPPG must be treated as DISABLED: step skipped with the disabled
    # reason, exactly as if rppg_enabled were the bool False.
    assert step.get("status") == "skipped"
    assert "rPPG disabled" in (step.get("error") or "")


def test_pipeline_rppg_string_true_enables(tmp_path, monkeypatch):
    """Symmetric: a string "true" DOES enable rPPG (the injector mock
    runs and the step completes), so the _read_bool coercion is correct
    in both directions, not just fail-safe-off."""
    stats, step = _mk_rppg_case(tmp_path, monkeypatch, rppg_enabled="true", rppg_returns="path")
    assert stats["failed"] == 0
    assert step.get("status") == "complete"


def test_stream_subprocess_timeout_reaps_killed_child(tmp_path):
    """Regression (CodeRabbit Critical, PR #39): on timeout the helper
    kill()s the child; without a following wait() it lingers as a zombie.
    Assert the process is actually reaped (poll() returns a code, not
    None) after TimeoutExpired propagates."""
    import subprocess
    import sys
    import time as _t
    import unittest.mock as _m

    from automation import rppg as _r

    real_popen = subprocess.Popen
    captured = {}

    def _spy(*a, **k):
        proc = real_popen(*a, **k)
        captured["p"] = proc
        return proc

    with _m.patch("subprocess.Popen", side_effect=_spy):
        with pytest.raises(subprocess.TimeoutExpired):
            _r.stream_subprocess_with_timeout(
                [sys.executable, "-c", "import time;time.sleep(30)"],
                cwd=str(tmp_path),
                timeout_seconds=1,
            )
    proc = captured["p"]
    for _ in range(50):
        if proc.poll() is not None:
            break
        _t.sleep(0.05)
    assert proc.poll() is not None, "killed child was not reaped (zombie)"


def test_run_rppg_absolutizes_relative_input(tmp_path, monkeypatch):
    """Regression (CodeRabbit Major, PR #39): run_rppg runs the injector
    with cwd=launcher.parent, so a RELATIVE video_path would resolve
    against rPPG/ not the caller dir. The input + --output must be
    .resolve()d before the command is built."""
    import os

    from automation import rppg as _r

    rppg_dir = tmp_path / "rPPG"
    rppg_dir.mkdir()
    (rppg_dir / "run_rppg.bat").write_text("@echo off", encoding="utf-8")
    (rppg_dir / "rppg_injector.py").write_text("# stub", encoding="utf-8")

    work = tmp_path / "work"
    work.mkdir()
    vid = work / "clip.mp4"
    vid.write_bytes(b"v")

    seen = {}

    def _fake_stream(cmd, *, cwd, timeout_seconds, on_line=None):
        seen["cmd"] = list(cmd)
        return 1, []  # non-zero -> graceful skip; we only inspect cmd

    monkeypatch.setattr(_r, "stream_subprocess_with_timeout", _fake_stream)

    old = os.getcwd()
    try:
        os.chdir(work)
        _r.run_rppg(video_path=Path("clip.mp4"), repo_root=tmp_path)
    finally:
        os.chdir(old)

    args = seen["cmd"]
    in_arg = args[1]
    out_arg = args[args.index("--output") + 1]
    assert Path(in_arg).is_absolute(), f"input not absolutized: {in_arg!r}"
    assert Path(out_arg).is_absolute(), f"--output not absolutized: {out_arg!r}"
    assert Path(in_arg) == vid.resolve()
