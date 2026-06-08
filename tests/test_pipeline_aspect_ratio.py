"""Aspect-ratio defaults: the automation pipeline must drive a 3:4 chain.

Root cause of historical 9:16 output: the pipeline called
SelfieGenerator.generate() WITHOUT width/height, so it fell back to the
generator's 720x1280 (9:16) default. The percent expand is ratio-preserving and
Kling follows the input image's aspect ratio, so a 9:16 selfie -> 9:16 video.
Generating the selfie at an exact 3:4 (864x1152) keeps the whole chain 3:4.
"""
from pathlib import Path

from PIL import Image

from automation.config import (
    AUTOMATION_DEFAULTS,
    from_app_config,
    merge_automation_defaults,
)
from automation.discovery import CaseRecord
from automation.manifest import AutomationManifest
from automation.pipeline import AutoPipelineRunner, PipelineDeps


class _CapturingSelfie:
    """Records the width/height the pipeline passes to generate()."""

    def __init__(self):
        self.gen_kwargs = []

    def set_progress_callback(self, _cb):
        return None

    def generate(self, image_path, prompt, output_folder, model_endpoint="", **kwargs):
        del model_endpoint
        self.gen_kwargs.append(dict(kwargs))
        out = Path(output_folder) / f"{Path(image_path).stem}_sim85_001.png"
        out.parent.mkdir(parents=True, exist_ok=True)
        Image.new("RGB", (864, 1152), (110, 110, 110)).save(out)
        return str(out)


class _FakeOutpaint:
    def set_progress_callback(self, _cb):
        return None

    def outpaint(self, image_path, output_folder, output_path=None, **kwargs):
        out_path = Path(
            output_path or (Path(output_folder) / f"{Path(image_path).stem}-expanded.png")
        )
        out_path.parent.mkdir(parents=True, exist_ok=True)
        Image.new("RGB", (864, 1152), (120, 120, 120)).save(out_path)
        return str(out_path)


class _FakeVideo:
    def set_progress_callback(self, _cb):
        return None

    def create_kling_generation(self, character_image_path, output_folder=None, **kwargs):
        del character_image_path, kwargs
        out = Path(output_folder or ".") / "video.mp4"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(b"mp4")
        return str(out)


def _run(tmp_path, monkeypatch, config):
    case_dir = tmp_path / "case-a"
    case_dir.mkdir()
    front = case_dir / "front.png"
    Image.new("RGB", (864, 1152), (1, 1, 1)).save(front)
    record = CaseRecord(case_dir=case_dir, front_path=front, relative_key="case-a")

    manifest = AutomationManifest.create_or_load(
        tmp_path / "automation_manifest.json", tmp_path, {}
    )
    manifest.ensure_case(record.relative_key, record.case_dir, record.front_path)

    monkeypatch.setattr(
        "automation.pipeline.extract_portrait_crop",
        lambda **kwargs: {"confidence": 0.9, "crop_box": [0, 0, 10, 10], "extractor": "mock"},
    )
    monkeypatch.setattr(
        "automation.pipeline.compute_face_similarity_details",
        lambda *a, **k: {"score": 90, "pass": True, "error": None, "match": True},
    )
    monkeypatch.setattr("automation.pipeline.run_oldcam_all", lambda **kwargs: [])

    selfie = _CapturingSelfie()
    runner = AutoPipelineRunner(
        config=config,
        automation_config=from_app_config(config),
        manifest=manifest,
        progress_cb=lambda msg, level="info": None,
        deps=PipelineDeps(
            outpaint_factory=lambda: _FakeOutpaint(),
            selfie_factory=lambda: selfie,
            video_factory=lambda: _FakeVideo(),
        ),
    )
    runner.run([record])
    return selfie


def _base_config(**overrides):
    config = merge_automation_defaults(
        {
            "falai_api_key": "x",
            "bfl_api_key": "bfl-token",
            "automation_oldcam_required": False,
            "saved_prompts": {"1": "prompt"},
            "current_prompt_slot": 1,
        }
    )
    config.update(overrides)
    return config


def test_default_selfie_dimensions_are_exact_3x4():
    assert AUTOMATION_DEFAULTS["automation_selfie_width"] == 864
    assert AUTOMATION_DEFAULTS["automation_selfie_height"] == 1152
    # 864/1152 == 0.75 exactly == 3:4
    assert 864 / 1152 == 0.75


def test_pipeline_passes_3x4_dimensions_to_selfie_generate(tmp_path, monkeypatch):
    selfie = _run(tmp_path, monkeypatch, _base_config())
    assert selfie.gen_kwargs, "selfie.generate was never called"
    kw = selfie.gen_kwargs[0]
    assert kw.get("width") == 864
    assert kw.get("height") == 1152
    # Ratio fed to the generator is exact 3:4.
    assert kw["width"] / kw["height"] == 0.75


def test_pipeline_respects_overridden_selfie_dimensions(tmp_path, monkeypatch):
    config = _base_config(
        automation_selfie_width=960, automation_selfie_height=1280
    )
    selfie = _run(tmp_path, monkeypatch, config)
    kw = selfie.gen_kwargs[0]
    assert kw["width"] == 960
    assert kw["height"] == 1280


def test_default_video_aspect_ratio_is_3x4():
    assert AUTOMATION_DEFAULTS["automation_video_aspect_ratio"] == "3:4"
