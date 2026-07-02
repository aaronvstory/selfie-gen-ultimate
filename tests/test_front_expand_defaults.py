"""Front-expand defaults: 2 passes + preserve_seamless, with pass 2 consuming
pass 1's output as its input (chain expand twice).

These defaults are user-mandated and have regressed before (preserve_seamless
misbehaving on the 2x pass), so they are locked here:
  - automation_front_expand_passes == 2
  - automation_front_expand_composite_mode == "preserve_seamless"
  - pass 2's input image == pass 1's output (output -> input chaining)
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


class _ChainOutpaint:
    """Records every outpaint invocation so the 2-pass output->input chain can
    be asserted. Each pass writes a distinct file so the chain is observable."""

    def __init__(self):
        self.invocations = []

    def set_progress_callback(self, _cb):
        return None

    def outpaint(self, image_path, output_folder, output_path=None, **kwargs):
        self.invocations.append(
            {
                "image_path": image_path,
                "output_path": output_path,
                "composite_mode": kwargs.get("composite_mode"),
            }
        )
        out_path = Path(
            output_path or (Path(output_folder) / f"{Path(image_path).stem}-expanded.png")
        )
        out_path.parent.mkdir(parents=True, exist_ok=True)
        Image.new("RGB", (864, 1152), (120, 120, 120)).save(out_path)
        return str(out_path)


class _FakeSelfie:
    def set_progress_callback(self, _cb):
        return None

    def generate(self, image_path, prompt, output_folder, model_endpoint="", **kwargs):
        del prompt, model_endpoint, kwargs
        out = Path(output_folder) / f"{Path(image_path).stem}_sim85_001.png"
        out.parent.mkdir(parents=True, exist_ok=True)
        Image.new("RGB", (864, 1152), (110, 110, 110)).save(out)
        return str(out)


class _FakeVideo:
    def set_progress_callback(self, _cb):
        return None

    def create_kling_generation(self, character_image_path, output_folder=None, **kwargs):
        del character_image_path, kwargs
        out = Path(output_folder or ".") / "video.mp4"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(b"mp4")
        return str(out)


def test_front_expand_defaults_are_2x_preserve_seamless():
    assert AUTOMATION_DEFAULTS["automation_front_expand_passes"] == 2
    assert AUTOMATION_DEFAULTS["automation_front_expand_composite_mode"] == "preserve_seamless"


def test_front_expand_runs_twice_chaining_output_into_input(tmp_path, monkeypatch):
    case_dir = tmp_path / "case-a"
    case_dir.mkdir()
    front = case_dir / "front.png"
    Image.new("RGB", (864, 1152), (1, 1, 1)).save(front)
    record = CaseRecord(case_dir=case_dir, front_path=front, relative_key="case-a")

    config = merge_automation_defaults(
        {
            "falai_api_key": "x",
            "bfl_api_key": "bfl-token",
            "automation_oldcam_required": False,
            "saved_prompts": {"1": "prompt"},
            "current_prompt_slot": 1,
            # 2-pass chaining is a PERCENT-mode feature; the default is now
            # three_four_fullres (single pass), so pin percent for this test.
            "automation_front_expand_mode": "percent",
        }
    )
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

    outpaint = _ChainOutpaint()
    runner = AutoPipelineRunner(
        config=config,
        automation_config=from_app_config(config),
        manifest=manifest,
        progress_cb=lambda msg, level="info": None,
        deps=PipelineDeps(
            outpaint_factory=lambda: outpaint,
            selfie_factory=lambda: _FakeSelfie(),
            video_factory=lambda: _FakeVideo(),
        ),
    )
    runner.run([record])

    # Front-expand invocations come first; isolate them by the source front image
    # / stage1 intermediate (selfie-expand operates on the selfie file).
    front_passes = [
        inv
        for inv in outpaint.invocations
        if Path(inv["image_path"]).name in {"front.png"}
        or "stage1" in Path(inv["image_path"]).name
    ]
    assert len(front_passes) == 2, f"expected 2 front passes, got {front_passes}"

    # Pass 1 reads the source front; pass 2 reads pass 1's output (chaining).
    assert Path(front_passes[0]["image_path"]).name == "front.png"
    pass1_output = Path(front_passes[0]["output_path"])
    assert "stage1" in pass1_output.name, "pass 1 must write a distinct stage1 file"
    assert Path(front_passes[1]["image_path"]) == pass1_output, (
        "pass 2 input must be pass 1 output (output -> input chaining)"
    )

    # Both front passes use preserve_seamless (the 2x composite must not regress).
    assert all(inv["composite_mode"] == "preserve_seamless" for inv in front_passes)
