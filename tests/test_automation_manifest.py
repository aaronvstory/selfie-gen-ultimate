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
    assert merged["automation_oldcam_version"] == "v8"
    assert merged["automation_oldcam_required"] is True
    assert "parked car" in merged["automation_selfie_prompts"]["1"].lower()
