"""sessions_dir_override kwarg threading (PR #49 — concurrent workspaces).

Verifies that ``session_manager.save_session(..., sessions_dir_override=X)``
writes to X instead of ``<app_dir>/sessions/``, and that the rolling-autosave
helpers and legacy-purge respect the override too. Without this guarantee,
the per-instance runtime isolation collapses and two concurrent windows
overwrite each other's autosave (the bleed bug this PR fixes).
"""

import json
import os
from unittest.mock import MagicMock

from kling_gui import session_manager as sm


def test_save_autosave_with_override_writes_to_override_dir(tmp_path):
    """When sessions_dir_override is set, rolling autosave lands there.

    Critically, the legacy ``<app_dir>/sessions/`` must remain untouched —
    that's the contract: per-instance autosaves are fully isolated.
    """
    app_dir = tmp_path / "app"
    override = tmp_path / "instance_runtime" / "sessions"
    app_dir.mkdir()

    fake_session = MagicMock()
    fake_session.count = 1
    fake_session.to_dict.return_value = {"images": [], "current_index": 0,
                                          "reference_index": 0, "similarity_ref_index": -1}
    fake_session.input_images = []
    fake_session.reference_entry = None
    fake_session.images = []

    path = sm.save_session(
        str(app_dir),
        fake_session,
        {},
        session_kind=sm.SESSION_KIND_AUTOSAVE,
        project_key="myproj",
        sessions_dir_override=str(override),
    )
    assert path is not None
    # The autosave landed under the override, not under app_dir/sessions/
    assert os.path.normcase(str(override)) in os.path.normcase(path)
    assert os.path.isfile(path)
    # Legacy <app_dir>/sessions/ either doesn't exist or is empty for this project
    legacy_sessions = app_dir / "sessions"
    if legacy_sessions.is_dir():
        legacy_files = [p.name for p in legacy_sessions.iterdir()]
        assert not any("myproj_autosave" in n for n in legacy_files), (
            f"legacy dir still contains a project autosave: {legacy_files}"
        )


def test_get_rolling_autosave_path_respects_override(tmp_path):
    """The autosave path helper must redirect under the override too."""
    override = tmp_path / "instance_runtime" / "sessions"
    path = sm.get_rolling_autosave_path(
        str(tmp_path / "app"),
        "myproj",
        sessions_dir_override=str(override),
    )
    assert path.endswith(os.path.join("sessions", "myproj_autosave.json"))
    assert os.path.normcase(str(override)) in os.path.normcase(path)


def test_purge_legacy_autosaves_respects_override(tmp_path):
    """_purge_legacy_autosaves must scope its sweep to the override dir
    only — sweeping the shared dir would delete other instances' rolling
    autosaves, which would re-introduce the bleed bug in reverse."""
    override = tmp_path / "instance_runtime" / "sessions"
    override.mkdir(parents=True)
    # The rolling file we want to keep
    rolling = override / "myproj_autosave.json"
    rolling.write_text("{}", encoding="utf-8")
    # A timestamped legacy autosave for the same project in the SAME (override) dir
    legacy = override / "myproj_autosave_20260101_120000.json"
    legacy.write_text("{}", encoding="utf-8")
    # An unrelated manual save — must NOT be touched
    manual = override / "myproj_other.json"
    manual.write_text("{}", encoding="utf-8")

    removed = sm._purge_legacy_autosaves(
        str(tmp_path / "app"),
        "myproj",
        str(rolling),
        sessions_dir_override=str(override),
    )
    assert removed == 1
    assert rolling.exists()       # kept
    assert not legacy.exists()    # purged
    assert manual.exists()        # untouched


def test_list_sessions_aggregates_legacy_and_instance_dirs(tmp_path, monkeypatch):
    """The Session Manager dialog uses list_sessions; it must see autosaves
    saved by ANY instance under the workspace, not just the legacy shared
    dir. Otherwise users would think their autosaves vanished after PR #49."""
    # Build a fake "workspace_dir/runtime/instances/<id>/sessions/" tree
    legacy_dir = tmp_path / "app" / "sessions"
    legacy_dir.mkdir(parents=True)
    inst_a = tmp_path / "app" / "runtime" / "instances" / "iid-a" / "sessions"
    inst_a.mkdir(parents=True)
    inst_b = tmp_path / "app" / "runtime" / "instances" / "iid-b" / "sessions"
    inst_b.mkdir(parents=True)

    def _write_session(path, name, kind="autosave"):
        data = {
            "name": name,
            "timestamp": "2026-01-01T00:00:00",
            "session_kind": kind,
            "project_key": name.split("_")[0],
            "session": {"images": []},
        }
        path.write_text(json.dumps(data), encoding="utf-8")

    _write_session(legacy_dir / "manualA.json", "manualA", kind="manual")
    _write_session(inst_a / "projA_autosave.json", "projA_autosave")
    _write_session(inst_b / "projB_autosave.json", "projB_autosave")

    # Point _user_data_root at our tmp tree
    import path_utils
    monkeypatch.setattr(path_utils, "_user_data_root", lambda: str(tmp_path / "app"))
    monkeypatch.setenv("KLING_WORKSPACE", "default")

    recs = sm.list_sessions(str(tmp_path / "app"))
    names = {r.name for r in recs}
    assert "manualA" in names
    # PR #49 M2: per-instance autosaves get an instance-id tag appended to
    # the displayed name so the user can disambiguate two siblings on the
    # same project_key. Legacy autosaves stay tag-less.
    assert "projA_autosave  [iid-a]" in names
    assert "projB_autosave  [iid-b]" in names


def test_list_sessions_disambiguates_two_instances_on_same_project_key(tmp_path, monkeypatch):
    """PR #49 M2 (review finding): the same project_key autosave file from
    two different per-instance dirs MUST both appear in the listing — the
    earlier (kind, fname) de-dupe silently hid one, so a user with two
    "untitled" windows would load the wrong window's state."""
    import json
    inst_a = tmp_path / "app" / "runtime" / "instances" / "iid-a" / "sessions"
    inst_b = tmp_path / "app" / "runtime" / "instances" / "iid-b" / "sessions"
    inst_a.mkdir(parents=True)
    inst_b.mkdir(parents=True)

    # Both windows are on the "untitled" default project_key
    for d in (inst_a, inst_b):
        (d / "untitled_autosave.json").write_text(json.dumps({
            "name": "untitled_autosave",
            "timestamp": "2026-01-01T00:00:00",
            "session_kind": "autosave",
            "project_key": "untitled",
            "session": {"images": []},
        }), encoding="utf-8")

    import path_utils
    monkeypatch.setattr(path_utils, "_user_data_root", lambda: str(tmp_path / "app"))
    monkeypatch.setenv("KLING_WORKSPACE", "default")

    recs = sm.list_sessions(str(tmp_path / "app"))
    autosaves = [r for r in recs if r.session_kind == "autosave"]
    # Both must appear, distinguishable via the tagged name.
    assert len(autosaves) == 2, (
        f"M2 regression: only {len(autosaves)} autosave(s) returned; "
        f"sibling was silently hidden by (kind, fname) de-dupe"
    )
    names = {r.name for r in autosaves}
    assert "untitled_autosave  [iid-a]" in names
    assert "untitled_autosave  [iid-b]" in names


def test_save_session_default_path_unchanged_without_override(tmp_path):
    """Back-compat: when sessions_dir_override is None (the default), saves
    land in the legacy app_dir/sessions/ — preserving every existing caller
    that doesn't know about PR #49."""
    app_dir = tmp_path / "app"
    app_dir.mkdir()

    fake_session = MagicMock()
    fake_session.count = 1
    fake_session.to_dict.return_value = {"images": [], "current_index": 0,
                                          "reference_index": 0, "similarity_ref_index": -1}
    fake_session.input_images = []
    fake_session.reference_entry = None
    fake_session.images = []

    path = sm.save_session(
        str(app_dir), fake_session, {},
        session_kind=sm.SESSION_KIND_AUTOSAVE,
        project_key="legacy",
    )
    assert path is not None
    assert os.path.normcase(str(app_dir / "sessions")) in os.path.normcase(path)
