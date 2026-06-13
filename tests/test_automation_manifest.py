import json
from pathlib import Path

import pytest

from automation.manifest import AutomationManifest, SCHEMA_VERSION
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


def test_manifest_corrupted_reads_degrade_not_crash(tmp_path: Path):
    """Gemini HIGH (PR #96 rounds 10-11): a corrupted/hand-edited manifest —
    non-dict "cases", case entry, or "steps" — must make
    case_is_complete_and_valid return False and snapshot_statuses degrade
    to pending placeholders, never AttributeError."""
    manifest_path = tmp_path / "automation_manifest.json"
    manifest = AutomationManifest.create_or_load(manifest_path, tmp_path, {})
    manifest.ensure_case("case/a", tmp_path / "case/a", tmp_path / "case/a/front.png")

    # Corrupted "steps" (string instead of dict) on a complete case.
    manifest.data["cases"]["case/a"]["status"] = "complete"
    manifest.data["cases"]["case/a"]["steps"] = "corrupted"
    assert manifest.case_is_complete_and_valid("case/a") is False

    # Corrupted per-stage entry (null instead of dict) must also degrade.
    manifest.data["cases"]["case/a"]["steps"] = {"oldcam": None, "loop": "bad", "rppg": 7}
    assert manifest.case_is_complete_and_valid("case/a") is False

    # Corrupted case entry (string instead of dict).
    manifest.data["cases"]["case/a"] = "corrupted"
    assert manifest.case_is_complete_and_valid("case/a") is False
    assert manifest.snapshot_statuses(["case/a"])["case/a"]["status"] == "pending"

    # Corrupted "cases" root (list instead of dict).
    manifest.data["cases"] = ["corrupted"]
    assert manifest.case_is_complete_and_valid("case/a") is False
    assert manifest.snapshot_statuses(["case/a"])["case/a"]["status"] == "pending"


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


def test_case_validation_uses_most_recently_finished_stage(tmp_path: Path):
    """Codex P1 (PR #96 round 4): under the Phase E order (Kling -> rPPG
    base -> Loop -> Oldcam) the OLDCAM output is the final deliverable; the
    rppg step's output is the pre-oldcam base. With real finished_at
    timestamps (update_step always stamps them), a deleted oldcam output
    must invalidate the case even though the rppg base still exists."""
    manifest_path = tmp_path / "automation_manifest.json"
    manifest = AutomationManifest.create_or_load(manifest_path, tmp_path, {})
    manifest.ensure_case("case/e", tmp_path / "case/e", tmp_path / "case/e/front.png")
    manifest.data["cases"]["case/e"]["status"] = "complete"

    rppg_base = tmp_path / "clip-rppg.mp4"
    rppg_base.write_bytes(b"rppg-base")
    oldcam_out = tmp_path / "clip-rppg-oldcam-v13.mp4"  # NOT created yet

    # Phase E order via real timestamps: rppg (base) finished BEFORE oldcam.
    steps = manifest.data["cases"]["case/e"]["steps"]
    steps["rppg"].update(
        status="complete", output=str(rppg_base), finished_at="2026-06-11T01:00:00+00:00"
    )
    steps["oldcam"].update(
        status="complete", output=str(oldcam_out), finished_at="2026-06-11T01:05:00+00:00"
    )
    manifest.save_atomic()

    # Oldcam (the most recently finished stage) output is missing -> the
    # case must NOT be treated complete, despite the surviving rppg base.
    assert manifest.case_is_complete_and_valid("case/e") is False

    oldcam_out.write_bytes(b"oldcam")
    assert manifest.case_is_complete_and_valid("case/e") is True


def test_backup_paths_never_collide_within_a_second(tmp_path: Path):
    """Codex P2 (PR #96 round 4): two create_fresh calls in the same second
    used identical second-resolution backup names — os.replace silently
    overwrote the first backup (data loss)."""
    manifest_path = tmp_path / "automation_manifest.json"
    AutomationManifest.create_or_load(manifest_path, tmp_path, {"automation_x": 1})
    AutomationManifest.create_fresh(manifest_path, tmp_path, {"automation_x": 2})
    AutomationManifest.create_fresh(manifest_path, tmp_path, {"automation_x": 3})
    backups = list(tmp_path.glob("automation_manifest.json.superseded.*"))
    assert len(backups) == 2, f"both backups must survive, got {[b.name for b in backups]}"


def test_load_if_exists_read_only_does_not_rename_corrupt_manifest(tmp_path: Path):
    """Codex P2 (PR #96 round 4): preview surfaces (scan/dry-run) promise
    non-mutation — read_only=True must NOT rename a corrupt manifest aside."""
    manifest_path = tmp_path / "automation_manifest.json"
    manifest_path.write_text("{ bad json", encoding="utf-8")

    assert AutomationManifest.load_if_exists(manifest_path, read_only=True) is None
    assert manifest_path.exists(), "read-only load must leave the corrupt file in place"
    assert not list(tmp_path.glob("*.corrupt.*"))

    # The default (mutating) load still backs it up for recovery.
    assert AutomationManifest.load_if_exists(manifest_path) is None
    assert not manifest_path.exists()
    assert list(tmp_path.glob("*.corrupt.*"))


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


def test_manifest_rebases_foreign_os_root_when_in_place(tmp_path: Path):
    """A manifest carrying a Windows-style root_dir must still load when the
    file physically lives in the requested (POSIX) folder — resume across
    OSes / after a folder move. The stale root_dir is rebased, not rejected.
    """
    root = tmp_path / "Batch_04"
    root.mkdir()
    manifest_path = root / "automation_manifest.json"
    snapshot = {"automation_manifest_name": "automation_manifest.json"}

    # Simulate a manifest authored on Windows: backslash drive-letter root,
    # with a completed case whose recorded paths are ALSO Windows absolutes.
    win_root = r"F:\Downloads\Telegram Desktop\DLs\Pandia\Batch_04"
    manifest_path.write_text(
        json.dumps(
            {
                "schema_version": SCHEMA_VERSION,
                "root_dir": win_root,
                "created_at": "2026-01-01T00:00:00+00:00",
                "updated_at": "2026-01-01T00:00:00+00:00",
                "config_snapshot": snapshot,
                "cases": {
                    "case/a": {
                        "status": "complete",
                        "case_dir": win_root + r"\case\a",
                        "front_path": win_root + r"\case\a\front.png",
                        "steps": {
                            "video_generate": {
                                "status": "complete",
                                "output": win_root + r"\case\a\out.mp4",
                            }
                        },
                        "outputs": {"video_generate": win_root + r"\case\a\out.mp4"},
                    }
                },
            }
        ),
        encoding="utf-8",
    )

    loaded = AutomationManifest.create_or_load(manifest_path, root, snapshot)

    # Loaded without raising, case data preserved, and EVERY path field
    # rebased to the live POSIX root (root_dir + case_dir + front_path +
    # step output + outputs mirror) AND persisted to disk.
    case = loaded.data["cases"]["case/a"]
    assert case["status"] == "complete"
    assert loaded.data["root_dir"] == str(root.resolve())
    assert case["case_dir"] == str(root / "case" / "a")
    assert case["front_path"] == str(root / "case" / "a" / "front.png")
    assert case["steps"]["video_generate"]["output"] == str(root / "case" / "a" / "out.mp4")
    assert case["outputs"]["video_generate"] == str(root / "case" / "a" / "out.mp4")
    on_disk = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert on_disk["root_dir"] == str(root.resolve())
    assert on_disk["cases"]["case/a"]["steps"]["video_generate"]["output"] == str(
        root / "case" / "a" / "out.mp4"
    )


def test_manifest_cross_os_resume_skips_when_output_present(tmp_path: Path):
    """The real point of the path rebase: after a cross-OS load, a completed
    case is skipped IFF its (now-rebased) output actually exists on this
    machine, and reprocessed otherwise — instead of always reprocessing.
    """
    root = tmp_path / "Batch_07"
    root.mkdir()
    manifest_path = root / "automation_manifest.json"
    snapshot = {"automation_manifest_name": "automation_manifest.json"}
    win_root = r"D:\shoots\Batch_07"

    def _case(rel: str, fname: str) -> dict:
        return {
            "status": "complete",
            "steps": {
                "video_generate": {
                    "status": "complete",
                    "output": win_root + rf"\{rel}\{fname}",
                    "finished_at": "2026-01-01T00:00:00+00:00",
                }
            },
        }

    manifest_path.write_text(
        json.dumps(
            {
                "schema_version": SCHEMA_VERSION,
                "root_dir": win_root,
                "created_at": "2026-01-01T00:00:00+00:00",
                "updated_at": "2026-01-01T00:00:00+00:00",
                "config_snapshot": snapshot,
                "cases": {"present": _case("present", "out.mp4"), "absent": _case("absent", "out.mp4")},
            }
        ),
        encoding="utf-8",
    )
    # Only the "present" case's output was actually copied to this machine.
    (root / "present").mkdir()
    (root / "present" / "out.mp4").write_bytes(b"video")

    loaded = AutomationManifest.create_or_load(manifest_path, root, snapshot)

    assert loaded.case_is_complete_and_valid("present") is True   # output exists -> skip
    assert loaded.case_is_complete_and_valid("absent") is False   # output missing -> reprocess


def test_manifest_rebase_respects_path_boundary_and_substrings(tmp_path: Path):
    """The rebase must only rewrite values that sit UNDER the old root with a
    path boundary — a sibling folder sharing a name prefix (Batch_040 vs
    Batch_04) and a free-text value that merely contains the root substring
    mid-string must be left untouched.
    """
    root = tmp_path / "Batch_04"
    root.mkdir()
    manifest_path = root / "automation_manifest.json"
    snapshot = {"automation_manifest_name": "automation_manifest.json"}
    win_root = r"D:\shoots\Batch_04"

    sibling = r"D:\shoots\Batch_040\case\x\out.mp4"  # NOT under Batch_04
    midstring = r"copied from D:\shoots\Batch_04\case to backup"  # not a path prefix
    nested_under_root = r"D:\shoots\Batch_04\case\a\extracted.png"  # genuinely under root

    manifest_path.write_text(
        json.dumps(
            {
                "schema_version": SCHEMA_VERSION,
                "root_dir": win_root,
                "created_at": "2026-01-01T00:00:00+00:00",
                "updated_at": "2026-01-01T00:00:00+00:00",
                "config_snapshot": snapshot,
                "cases": {
                    "case/a": {
                        "status": "complete",
                        "steps": {
                            "extract_portrait": {"status": "complete", "output": nested_under_root},
                            # path buried in nested meta list -> deep-walk must reach it
                            "similarity_gate": {
                                "status": "complete",
                                "meta": {
                                    "note": midstring,
                                    "diagnostics": {
                                        "mode_results": [{"diagnostics": {"ref_path": nested_under_root}}]
                                    },
                                },
                            },
                            "sibling": {"status": "complete", "output": sibling},
                        },
                    }
                },
            }
        ),
        encoding="utf-8",
    )

    loaded = AutomationManifest.create_or_load(manifest_path, root, snapshot)
    steps = loaded.data["cases"]["case/a"]["steps"]

    # Under-root paths rebased (including the nested meta list path).
    assert steps["extract_portrait"]["output"] == str(root / "case" / "a" / "extracted.png")
    assert steps["similarity_gate"]["meta"]["diagnostics"]["mode_results"][0]["diagnostics"][
        "ref_path"
    ] == str(root / "case" / "a" / "extracted.png")
    # Sibling prefix and mid-string text left exactly as-is.
    assert steps["sibling"]["output"] == sibling
    assert steps["similarity_gate"]["meta"]["note"] == midstring


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
        # Default was "inject" in PR #39; flipped to "iterative" in PR
        # #43 (friend confirmed iterative is mandatory for production).
        # The test sends the CURRENT default so the manifest treats
        # the key as default-equal and reconciles cleanly.
        "automation_rppg_mode": "iterative",
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


def test_manifest_explicit_optin_of_new_key_forces_reprocess(tmp_path: Path):
    """Regression (Codex P2, PR #39): tolerating a missing additive key
    is only valid when the requested value is the DEFAULT. If the user
    EXPLICITLY opts a new feature in (automation_rppg_enabled=True) on a
    corpus whose manifest predates rPPG, the fingerprint MUST mismatch so
    the case reprocesses and the opted-in step actually runs — otherwise
    skip_completed would skip it as 'complete' on the stale pre-rPPG
    output and rPPG silently never executes."""
    manifest_path = tmp_path / "automation_manifest.json"
    root = tmp_path / "root"
    root.mkdir()
    old_snap = {
        "automation_manifest_name": "automation_manifest.json",
        "automation_front_expand_mode": "document_3x4",
    }
    AutomationManifest.create_or_load(manifest_path, root, old_snap)

    # Default-valued additive key -> tolerated (behaviour-preserving).
    ok_snap = dict(old_snap)
    ok_snap["automation_rppg_enabled"] = False  # the documented default
    assert AutomationManifest.create_or_load(manifest_path, root, ok_snap) is not None

    # EXPLICIT opt-in (non-default) -> must force a reprocess (mismatch).
    optin_snap = dict(old_snap)
    optin_snap["automation_rppg_enabled"] = True
    with pytest.raises(ValueError, match="fingerprint mismatch"):
        AutomationManifest.create_or_load(manifest_path, root, optin_snap)


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


@pytest.mark.parametrize(
    "scope_key,old_value,new_value",
    [
        ("automation_max_cases_per_run", "5", "1"),
        ("automation_reprocess_mode", "skip", "overwrite"),
        ("automation_allow_reprocess", False, True),
        ("automation_verbose_logging", True, False),
        ("automation_recommended_defaults_version", 6, 7),
        ("automation_front_globs", [], ["*id_photo*.jpg"]),
        ("automation_front_names", ["front.jpg"], ["front.jpg", "front.png"]),
    ],
)
def test_manifest_run_scope_keys_never_invalidate(tmp_path: Path, scope_key: str, old_value, new_value):
    """Run-scope / metadata keys are EXCLUDED from the fingerprint: changing
    how much of the batch runs, discovery scope, or bookkeeping stamps must
    never demand a manifest rebuild (PR #96 round 6 — the user got the
    back-up-and-recreate prompt every time max-cases changed). The first
    create stores the key in the snapshot, so the reload also proves the
    exclusion applies to manifests that RECORDED these keys."""
    manifest_path = tmp_path / "automation_manifest.json"
    root = tmp_path / "root"
    root.mkdir()
    snap_a = {"automation_front_expand_percent": 30, scope_key: old_value}
    snap_b = {"automation_front_expand_percent": 30, scope_key: new_value}
    AutomationManifest.create_or_load(manifest_path, root, snap_a)
    # Must NOT raise — the change is run-scope, outputs are unaffected.
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
    # CodeRabbit Major on 36b5e0b (2026-05-22): both automation
    # expand providers default to "fal" per user direction "fal-
    # first defaults everywhere". The GUI Phase A revert only
    # touched the GUI tabs; the automation CLI keys had stayed
    # "bfl" and silently overrode the user's GUI choice. R3
    # aligns all three (GUI + 2 automation) to fal.
    assert merged["automation_front_expand_provider"] == "fal"
    assert merged["automation_front_expand_mode"] == "percent"
    assert merged["automation_front_expand_composite_mode"] == "preserve_seamless"
    assert merged["automation_front_expand_percent"] == 70
    assert merged["automation_front_expand_passes"] == 2
    assert merged["automation_selfie_expand_provider"] == "fal"
    assert merged["automation_selfie_expand_mode"] == "percent"
    # Step 2.5 selfie expand ships raw AI output by default (composite
    # "none") per user request, PR #41 — front expand stays
    # preserve_seamless (asserted above); the two are independent.
    assert merged["automation_selfie_expand_composite_mode"] == "none"
    assert merged["automation_selfie_expand_percent"] == 30
    assert merged["automation_selfie_models"] == ["fal-ai/nano-banana-2/edit"]
    # Multi-select canonical list form; CLI default v13 per user mandate
    # 2026-06-11 (GUI default stays v24 — intentionally divergent).
    assert merged["automation_oldcam_version"] == ["v13"]
    assert merged["automation_oldcam_required"] is True
    assert "parked car" in merged["automation_selfie_prompts"]["1"].lower()
