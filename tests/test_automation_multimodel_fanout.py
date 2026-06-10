"""Multi-selfie-model fan-out tests (2026-06-11).

With N>1 selected selfie models, EVERY model whose best candidate passes the
similarity threshold becomes a branch: its own selfie -> expand -> video ->
post chain. The overall-best candidate stays the PRIMARY (owns the manifest
step statuses — single-model behavior is byte-identical); extra branches are
recorded in the video_generate step's meta["branches"] and never fail the
case.
"""

import re
from pathlib import Path

from PIL import Image

from automation.config import from_app_config, merge_automation_defaults
from automation.discovery import CaseRecord
from automation.manifest import AutomationManifest
from automation.pipeline import AutoPipelineRunner, PipelineDeps

NANO = "fal-ai/nano-banana-2/edit"
GPT = "openai/gpt-image-2/edit"


class ModelAwareSelfie:
    """Fake selfie generator that embeds the model slug + a per-model score
    in the output filename (mirrors the real ``..._{slug}_sim{NN}_...``
    naming contract)."""

    def __init__(self, scores):
        self.scores = scores  # endpoint -> sim score
        self.calls = []

    def set_progress_callback(self, _cb):
        return None

    def generate(self, image_path, prompt, output_folder, model_endpoint="", **kwargs):
        del prompt, kwargs
        self.calls.append(model_endpoint)
        slug = "-".join(p for p in model_endpoint.split("/") if p)[-40:].replace("/", "-")
        slug = re.sub(r"[^a-z0-9\-]+", "-", slug.lower()).strip("-")
        score = self.scores[model_endpoint]
        out = Path(output_folder) / f"{Path(image_path).stem}_{slug}_sim{score}_001.png"
        out.parent.mkdir(parents=True, exist_ok=True)
        Image.new("RGB", (64, 64), (110, 110, 110)).save(out)
        return str(out)


class StemEchoVideo:
    """Fake video generator producing one mp4 per input still (named after
    the still's stem) so branch videos are distinguishable."""

    def __init__(self):
        self.stills = []

    def set_progress_callback(self, _cb):
        return None

    def create_kling_generation(self, character_image_path, output_folder=None, **kwargs):
        del kwargs
        self.stills.append(character_image_path)
        out = Path(output_folder or ".") / f"{Path(character_image_path).stem}_k.mp4"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(b"mp4")
        return str(out)


class FakeOutpaint:
    def __init__(self):
        self.invocations = []

    def set_progress_callback(self, _cb):
        return None

    def outpaint(self, image_path, output_folder, output_path=None, **kwargs):
        del kwargs
        self.invocations.append(image_path)
        out_path = Path(output_path or (Path(output_folder) / f"{Path(image_path).stem}-expanded.png"))
        out_path.parent.mkdir(parents=True, exist_ok=True)
        Image.new("RGB", (64, 64), (120, 120, 120)).save(out_path)
        return str(out_path)


def _sim_from_filename(_ref, target, report_cb=None):
    del report_cb
    match = re.search(r"_sim(\d+)_", Path(target).name)
    score = int(match.group(1)) if match else 90
    return {"score": score, "pass": True, "error": None, "match": True}


def _build(tmp_path, monkeypatch, *, models, scores, extra_config=None, oldcam_capture=None):
    case_dir = tmp_path / "case-a"
    case_dir.mkdir()
    front = case_dir / "front.png"
    Image.new("RGB", (64, 64), (1, 1, 1)).save(front)
    record = CaseRecord(case_dir=case_dir, front_path=front, relative_key="case-a")

    config = merge_automation_defaults({
        "falai_api_key": "x",
        "bfl_api_key": "bfl-token",
        "automation_oldcam_required": False,
        "automation_selfie_models": list(models),
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
    monkeypatch.setattr("automation.pipeline.compute_face_similarity_details", _sim_from_filename)

    def fake_oldcam_all(**kwargs):
        if oldcam_capture is not None:
            oldcam_capture.append(str(kwargs["video_path"]))
        return []

    monkeypatch.setattr("automation.pipeline.run_oldcam_all", fake_oldcam_all)

    selfie = ModelAwareSelfie(scores)
    video = StemEchoVideo()
    runner = AutoPipelineRunner(
        config=config,
        automation_config=from_app_config(config),
        manifest=manifest,
        progress_cb=lambda msg, level="info": None,
        deps=PipelineDeps(
            outpaint_factory=lambda: FakeOutpaint(),
            selfie_factory=lambda: selfie,
            video_factory=lambda: video,
        ),
    )
    return runner, record, manifest, selfie, video


def test_two_passing_models_fan_out_to_two_videos(tmp_path, monkeypatch):
    runner, record, manifest, selfie, video = _build(
        tmp_path, monkeypatch, models=[NANO, GPT], scores={NANO: 92, GPT: 88},
    )
    stats = runner.run([record])

    assert stats["completed"] == 1
    # Both models generated.
    assert set(selfie.calls) == {NANO, GPT}
    # Two videos: primary (nano, higher score) + the gpt branch.
    assert len(video.stills) == 2
    case = manifest.data["cases"]["case-a"]
    assert case["status"] == "complete"
    branches = case["steps"]["video_generate"]["meta"].get("branches")
    assert branches and len(branches) == 1
    branch = branches[0]
    assert branch["endpoint"] == GPT
    assert branch["status"] == "complete"
    assert branch["video"].endswith("_k.mp4")
    assert "-expanded" in branch["expanded"]
    # Primary step output stays the nano chain (single-status-per-step
    # schema unchanged).
    assert "sim92" in case["steps"]["similarity_gate"]["output"]


def test_below_threshold_model_does_not_branch(tmp_path, monkeypatch):
    runner, record, manifest, selfie, video = _build(
        tmp_path, monkeypatch, models=[NANO, GPT], scores={NANO: 92, GPT: 50},
    )
    stats = runner.run([record])

    assert stats["completed"] == 1
    assert set(selfie.calls) == {NANO, GPT}  # both still generated
    assert len(video.stills) == 1  # but only the primary got a video
    branches = manifest.data["cases"]["case-a"]["steps"]["video_generate"]["meta"].get("branches")
    assert not branches


def test_single_model_has_no_branches_key(tmp_path, monkeypatch):
    runner, record, manifest, selfie, video = _build(
        tmp_path, monkeypatch, models=[NANO], scores={NANO: 92},
    )
    stats = runner.run([record])

    assert stats["completed"] == 1
    assert len(video.stills) == 1
    meta = manifest.data["cases"]["case-a"]["steps"]["video_generate"]["meta"]
    assert "branches" not in meta


def test_fanout_overrides_first_pass_cross_model_exit(tmp_path, monkeypatch):
    """first_pass historically stopped at the first model passing the
    threshold; in fan-out mode every selected model must still generate."""
    runner, record, manifest, selfie, video = _build(
        tmp_path,
        monkeypatch,
        models=[NANO, GPT],
        scores={NANO: 95, GPT: 90},
        extra_config={"automation_selfie_model_policy": "first_pass"},
    )
    stats = runner.run([record])

    assert stats["completed"] == 1
    assert set(selfie.calls) == {NANO, GPT}
    assert len(video.stills) == 2


def test_branch_oldcam_runs_on_branch_video(tmp_path, monkeypatch):
    oldcam_capture = []
    runner, record, manifest, selfie, video = _build(
        tmp_path,
        monkeypatch,
        models=[NANO, GPT],
        scores={NANO: 92, GPT: 88},
        oldcam_capture=oldcam_capture,
    )
    stats = runner.run([record])

    assert stats["completed"] == 1
    # Oldcam ran once for the primary video and once for the branch video.
    assert len(oldcam_capture) == 2
    assert any("sim88" in p for p in oldcam_capture)
    assert any("sim92" in p for p in oldcam_capture)


def test_branch_marks_failed_when_required_oldcam_produces_nothing(tmp_path, monkeypatch):
    """Codex P2 (PR #96): with automation_oldcam_required=True and the
    branch's oldcam fan-out producing ZERO outputs, the branch record must
    say "failed" — not report success with an empty oldcam_outputs list.
    The CASE still completes (primary chain owns the case verdict)."""
    runner, record, manifest, selfie, video = _build(
        tmp_path,
        monkeypatch,
        models=[NANO, GPT],
        scores={NANO: 92, GPT: 88},
        extra_config={"automation_oldcam_required": True},
    )

    def selective_oldcam(**kwargs):
        video_path = Path(kwargs["video_path"])
        if "sim88" in video_path.stem:  # the GPT branch: oldcam produces nothing
            return []
        out = video_path.with_name(f"{video_path.stem}-oldcam-v13.mp4")
        out.write_bytes(b"oldcam")
        return [("v13", out)]

    monkeypatch.setattr("automation.pipeline.run_oldcam_all", selective_oldcam)
    stats = runner.run([record])

    assert stats["completed"] == 1  # primary oldcam succeeded -> case OK
    branches = manifest.data["cases"]["case-a"]["steps"]["video_generate"]["meta"]["branches"]
    assert branches[0]["status"] == "failed"
    assert "required oldcam" in branches[0]["error"]
    assert branches[0]["oldcam_outputs"] == []


def test_abort_skips_remaining_branches(tmp_path, monkeypatch):
    """Codex P2 + Gemini HIGH/MED (PR #96): [a] abort must stop fan-out at
    the next branch-stage checkpoint, the partial branch records must STILL
    be persisted (the finally — losing them costs paid regeneration on
    resume), and the case reverts to pending for resume."""
    THIRD = "vendor-x/third-model/edit"
    runner, record, manifest, selfie, video = _build(
        tmp_path,
        monkeypatch,
        models=[NANO, GPT, THIRD],
        scores={NANO: 95, GPT: 90, THIRD: 85},
    )

    original = StemEchoVideo.create_kling_generation

    def abort_during_first_branch(self, character_image_path, output_folder=None, **kwargs):
        if "sim90" in Path(character_image_path).stem:
            runner.abort_event.set()  # fires DURING branch 1's video gen
        return original(self, character_image_path, output_folder=output_folder, **kwargs)

    monkeypatch.setattr(StemEchoVideo, "create_kling_generation", abort_during_first_branch)
    stats = runner.run([record])

    # The abort propagates from the branch chain's next stage checkpoint
    # (oldcam), so the run stops and the case reverts to "pending" — every
    # completed step (the ENTIRE primary chain) is preserved for resume.
    assert runner.stopped_reason == "aborted"
    assert stats["completed"] == 0
    case = manifest.data["cases"]["case-a"]
    assert case["status"] == "pending"
    assert case["steps"]["video_generate"]["status"] == "complete"  # primary work kept
    # The finally persisted the partial branch records despite the abort.
    branches = case["steps"]["video_generate"]["meta"]["branches"]
    by_endpoint = {b["endpoint"]: b for b in branches}
    assert by_endpoint[GPT]["status"] == "skipped"
    assert "aborted" in by_endpoint[GPT]["error"]
    assert THIRD not in by_endpoint  # never started; rebuilt on resume
    # Branch 1's paid video DID land on disk before the abort -> reusable,
    # AND the partial record carried on the exception preserved its path
    # (round-3 review must-fix: a bare skipped record lost it).
    assert by_endpoint[GPT].get("video", "").endswith("_k.mp4")
    assert list((tmp_path / "case-a" / "gen-videos").glob("*sim90*_k.mp4"))


def test_branch_failure_never_fails_the_case(tmp_path, monkeypatch):
    runner, record, manifest, selfie, video = _build(
        tmp_path, monkeypatch, models=[NANO, GPT], scores={NANO: 92, GPT: 88},
    )

    original = StemEchoVideo.create_kling_generation

    def failing_for_branch(self, character_image_path, output_folder=None, **kwargs):
        if "sim88" in Path(character_image_path).stem:
            return None  # the branch generation fails
        return original(self, character_image_path, output_folder=output_folder, **kwargs)

    monkeypatch.setattr(StemEchoVideo, "create_kling_generation", failing_for_branch)
    stats = runner.run([record])

    assert stats["completed"] == 1  # primary deliverable exists -> case OK
    case = manifest.data["cases"]["case-a"]
    assert case["status"] == "complete"
    branches = case["steps"]["video_generate"]["meta"]["branches"]
    assert branches[0]["status"] == "failed"
    assert "video" not in branches[0] or branches[0].get("video") is None
