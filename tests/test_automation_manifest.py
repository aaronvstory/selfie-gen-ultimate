import json
from pathlib import Path

import pytest

from automation.manifest import AutomationManifest
from automation.config import merge_automation_defaults


def test_manifest_create_update_and_resume(tmp_path: Path):
    manifest_path = tmp_path / "automation_manifest.json"
    root_dir = tmp_path
    snapshot = {"automation_manifest_name": "automation_manifest.json"}

    manifest = AutomationManifest.create_or_load(manifest_path, root_dir, snapshot)
    case = manifest.ensure_case("case/a", tmp_path / "case/a", tmp_path / "case/a/front.png")
    assert case["status"] == "pending"

    out_file = tmp_path / "case/a/front-expanded.png"
    out_file.parent.mkdir(parents=True, exist_ok=True)
    out_file.write_bytes(b"x")

    manifest.update_step(
        "case/a",
        "front_expand",
        "complete",
        output=str(out_file),
        provider="bfl",
        margins={"left": 5, "right": 5, "top": 10, "bottom": 10},
    )

    assert manifest_path.exists()
    reloaded = AutomationManifest.create_or_load(manifest_path, root_dir, snapshot)
    assert reloaded.data["cases"]["case/a"]["steps"]["front_expand"]["status"] == "complete"


def test_manifest_complete_requires_existing_final_output(tmp_path: Path):
    manifest_path = tmp_path / "automation_manifest.json"
    manifest = AutomationManifest.create_or_load(manifest_path, tmp_path, {})
    manifest.ensure_case("case/a", tmp_path / "case/a", tmp_path / "case/a/front.png")
    manifest.data["cases"]["case/a"]["status"] = "complete"
    manifest.data["cases"]["case/a"]["steps"]["video_generate"]["output"] = str(tmp_path / "missing.mp4")
    manifest.save_atomic()

    assert manifest.case_is_complete_and_valid("case/a") is False

    actual = tmp_path / "ok.mp4"
    actual.write_bytes(b"x")
    manifest.data["cases"]["case/a"]["steps"]["video_generate"]["output"] = str(actual)
    manifest.save_atomic()
    assert manifest.case_is_complete_and_valid("case/a") is True


def test_manifest_complete_validates_rppg_output_when_rppg_completed(tmp_path: Path):
    """Regression (Codex P2, PR #39): rPPG is the LAST post-process. When
    the rppg step completed, validation must check the injected file —
    not fall back to the surviving pre-rPPG oldcam/video output — so a
    deleted rPPG deliverable isn't masked as 'complete' (which would make
    skip_completed wrongly skip the case, leaving no rPPG output)."""
    manifest_path = tmp_path / "automation_manifest.json"
    manifest = AutomationManifest.create_or_load(manifest_path, tmp_path, {})
    manifest.ensure_case("case/r", tmp_path / "case/r", tmp_path / "case/r/front.png")
    steps = manifest.data["cases"]["case/r"]["steps"]
    manifest.data["cases"]["case/r"]["status"] = "complete"

    # Pre-rPPG oldcam file survives; the rPPG deliverable was deleted.
    oldcam_file = tmp_path / "clip-oldcam-v24.mp4"
    oldcam_file.write_bytes(b"oldcam")
    steps["video_generate"]["output"] = str(tmp_path / "clip.mp4")
    steps["oldcam"]["status"] = "complete"
    steps["oldcam"]["output"] = str(oldcam_file)
    steps["rppg"]["status"] = "complete"
    steps["rppg"]["output"] = str(tmp_path / "clip-oldcam-v24-rppg - 7.8.mp4")  # missing
    manifest.save_atomic()
    # Must be INVALID — the real final (rPPG) deliverable is gone, even
    # though the pre-rPPG oldcam file still exists.
    assert manifest.case_is_complete_and_valid("case/r") is False

    # Restore the rPPG file -> valid again.
    rppg_file = tmp_path / "clip-oldcam-v24-rppg - 7.8.mp4"
    rppg_file.write_bytes(b"rppg")
    manifest.save_atomic()
    assert manifest.case_is_complete_and_valid("case/r") is True

    # If rppg did NOT complete (e.g. disabled/skipped), behaviour is
    # unchanged: fall back to the oldcam output.
    steps["rppg"]["status"] = "skipped"
    steps["rppg"]["output"] = None
    manifest.save_atomic()
    assert manifest.case_is_complete_and_valid("case/r") is True  # oldcam file exists


def test_manifest_corrupt_file_is_backed_up_and_recreated(tmp_path: Path):
    manifest_path = tmp_path / "automation_manifest.json"
    manifest_path.write_text("{ bad json", encoding="utf-8")

    manifest = AutomationManifest.create_or_load(manifest_path, tmp_path, {})
    assert manifest.data["schema_version"] == 1

    backups = list(tmp_path.glob("automation_manifest.json.corrupt.*"))
    assert backups, "Expected corrupt manifest backup file."


def test_manifest_load_if_exists_returns_none_for_bad_json(tmp_path: Path):
    manifest_path = tmp_path / "automation_manifest.json"
    manifest_path.write_text("{ bad json", encoding="utf-8")
    loaded = AutomationManifest.load_if_exists(manifest_path)
    assert loaded is None


def test_manifest_load_if_exists_backs_up_non_dict_payload(tmp_path: Path):
    manifest_path = tmp_path / "automation_manifest.json"
    manifest_path.write_text(json.dumps(["not", "a", "dict"]), encoding="utf-8")
    loaded = AutomationManifest.load_if_exists(manifest_path)
    assert loaded is None
    backups = list(tmp_path.glob("automation_manifest.json.corrupt.*"))
    assert backups


def test_manifest_load_if_exists_backs_up_wrong_schema(tmp_path: Path):
    manifest_path = tmp_path / "automation_manifest.json"
    manifest_path.write_text(json.dumps({"schema_version": 999, "cases": {}}), encoding="utf-8")
    loaded = AutomationManifest.load_if_exists(manifest_path)
    assert loaded is None
    backups = list(tmp_path.glob("automation_manifest.json.corrupt.*"))
    assert backups


def test_manifest_create_or_load_raises_on_root_mismatch(tmp_path: Path):
    manifest_path = tmp_path / "automation_manifest.json"
    root_a = tmp_path / "root-a"
    root_b = tmp_path / "root-b"
    root_a.mkdir()
    root_b.mkdir()
    snapshot = {"automation_manifest_name": "automation_manifest.json"}
    AutomationManifest.create_or_load(manifest_path, root_a, snapshot)

    with pytest.raises(ValueError, match="Manifest root mismatch"):
        AutomationManifest.create_or_load(manifest_path, root_b, snapshot)


def test_manifest_create_or_load_raises_on_fingerprint_mismatch(tmp_path: Path):
    manifest_path = tmp_path / "automation_manifest.json"
    root = tmp_path / "root"
    root.mkdir()
    snap_a = {
        "automation_manifest_name": "automation_manifest.json",
        "automation_front_expand_mode": "document_3x4",
    }
    snap_b = {
        "automation_manifest_name": "automation_manifest.json",
        "automation_front_expand_mode": "percent",
    }
    AutomationManifest.create_or_load(manifest_path, root, snap_a)

    with pytest.raises(ValueError, match="config fingerprint mismatch"):
        AutomationManifest.create_or_load(manifest_path, root, snap_b)


def test_manifest_additive_default_key_is_backward_compatible(tmp_path: Path):
    """Regression (Codex P1, PR #39): a purely-additive new automation_*
    default key (rPPG introduced default-OFF) must NOT invalidate a
    manifest written before it existed. Old runs are behaviour-identical
    (the feature is off), so resume/run must still work — only a CHANGED
    value of a key the old manifest actually recorded may mismatch."""
    manifest_path = tmp_path / "automation_manifest.json"
    root = tmp_path / "root"
    root.mkdir()
    old_snap = {
        "automation_manifest_name": "automation_manifest.json",
        "automation_front_expand_mode": "document_3x4",
    }
    AutomationManifest.create_or_load(manifest_path, root, old_snap)

    new_snap = {
        "automation_manifest_name": "automation_manifest.json",
        "automation_front_expand_mode": "document_3x4",
        "automation_rppg_enabled": False,
        "automation_rppg_mode": "inject",
        "automation_rppg_required": False,
    }
    # Additive default-off keys must NOT raise — backward compatible.
    reloaded = AutomationManifest.create_or_load(manifest_path, root, new_snap)
    assert reloaded is not None

    # Changing a key the OLD manifest recorded still mismatches.
    changed_snap = dict(new_snap)
    changed_snap["automation_front_expand_mode"] = "percent"
    with pytest.raises(ValueError, match="fingerprint mismatch"):
        AutomationManifest.create_or_load(manifest_path, root, changed_snap)


@pytest.mark.parametrize(
    "changed_key,old_value,new_value",
    [
        ("automation_front_expand_percent", 30, 40),
        ("automation_crop_multiplier", 1.5, 1.8),
        ("automation_similarity_threshold", 80, 81),
        ("automation_selfie_models", ["m1"], ["m1", "m2"]),
        ("automation_oldcam_required", False, True),
    ],
)
def test_manifest_fingerprint_captures_all_automation_keys(tmp_path: Path, changed_key: str, old_value, new_value):
    manifest_path = tmp_path / "automation_manifest.json"
    root = tmp_path / "root"
    root.mkdir()
    snap_a = {
        "automation_manifest_name": "automation_manifest.json",
        "automation_front_expand_percent": 30,
        "automation_crop_multiplier": 1.5,
        "automation_similarity_threshold": 80,
        "automation_selfie_models": ["m1"],
        "automation_oldcam_required": False,
    }
    snap_b = dict(snap_a)
    snap_b[changed_key] = new_value
    snap_a[changed_key] = old_value

    AutomationManifest.create_or_load(manifest_path, root, snap_a)
    with pytest.raises(ValueError, match="config fingerprint mismatch"):
        AutomationManifest.create_or_load(manifest_path, root, snap_b)


def test_manifest_create_or_load_non_dict_payload_backs_up_once(tmp_path: Path):
    manifest_path = tmp_path / "automation_manifest.json"
    manifest_path.write_text(json.dumps(["bad-root"]), encoding="utf-8")

    manifest = AutomationManifest.create_or_load(manifest_path, tmp_path, {})
    assert manifest.data["schema_version"] == 1

    backups = list(tmp_path.glob("automation_manifest.json.corrupt.*"))
    assert len(backups) == 1


def test_manifest_create_or_load_invalid_utf8_backs_up_and_recreates(tmp_path: Path):
    manifest_path = tmp_path / "automation_manifest.json"
    manifest_path.write_bytes(b"\xff\xfe\xfa")

    manifest = AutomationManifest.create_or_load(manifest_path, tmp_path, {})
    assert manifest.data["schema_version"] == 1

    backups = list(tmp_path.glob("automation_manifest.json.corrupt.*"))
    assert len(backups) == 1


def test_automation_defaults_use_percent_and_nano_model():
    merged = merge_automation_defaults({})
    assert merged["automation_front_expand_provider"] == "bfl"
    assert merged["automation_front_expand_mode"] == "percent"
    assert merged["automation_front_expand_composite_mode"] == "preserve_seamless"
    assert merged["automation_front_expand_percent"] == 70
    assert merged["automation_front_expand_passes"] == 2
    assert merged["automation_selfie_expand_provider"] == "bfl"
    assert merged["automation_selfie_expand_mode"] == "percent"
    assert merged["automation_selfie_expand_composite_mode"] == "preserve_seamless"
    assert merged["automation_selfie_expand_percent"] == 30
    assert merged["automation_selfie_models"] == ["fal-ai/nano-banana-2/edit"]
    assert merged["automation_oldcam_version"] == "v24"
    assert merged["automation_oldcam_required"] is True
    assert "parked car" in merged["automation_selfie_prompts"]["1"].lower()
