"""Tests for the headless --batch override flags, glob-based front discovery,
and the questionary non-TTY fallbacks added in feat/cli-batch-pipeline-questionary.

These complement the existing mocked automation suites — no real fal.ai calls.
"""
from __future__ import annotations

from pathlib import Path

from automation.config import AUTOMATION_DEFAULTS, AutomationConfig
from automation.discovery import discover_case_folders
from kling_automation_ui import KlingAutomationUI, _derive_model_display_name


# --------------------------------------------------------------------------
# discover_case_folders: glob support
# --------------------------------------------------------------------------

def _make_case(root: Path, name: str, front_name: str) -> Path:
    case = root / name
    case.mkdir(parents=True)
    (case / front_name).write_bytes(b"x")
    return case


def test_discovery_exact_names_unchanged_when_no_globs(tmp_path):
    _make_case(tmp_path, "a", "front.jpg")
    _make_case(tmp_path, "b", "other.jpg")  # not a front name -> not a case
    cases = discover_case_folders(tmp_path, ["front.jpg"])
    keys = sorted(c.relative_key for c in cases)
    assert keys == ["a"]


def test_discovery_glob_matches_nonstandard_front(tmp_path):
    _make_case(tmp_path, "a", "menopausequestionnaire-12016-id_photo-x.jpg")
    cases = discover_case_folders(tmp_path, ["front.jpg"], front_globs=["*id_photo*.jpg"])
    keys = sorted(c.relative_key for c in cases)
    assert keys == ["a"]
    # The matched file is the non-standard one.
    assert "id_photo" in cases[0].front_path.name


def test_discovery_glob_is_additive_to_exact_names(tmp_path):
    _make_case(tmp_path, "a", "front.jpg")
    _make_case(tmp_path, "b", "scan-id_photo-1.jpg")
    cases = discover_case_folders(tmp_path, ["front.jpg"], front_globs=["*id_photo*.jpg"])
    assert sorted(c.relative_key for c in cases) == ["a", "b"]


def test_discovery_glob_case_insensitive(tmp_path):
    _make_case(tmp_path, "a", "PHOTO-ID_PHOTO.JPG")
    cases = discover_case_folders(tmp_path, [], front_globs=["*id_photo*.jpg"])
    assert [c.relative_key for c in cases] == ["a"]


def test_discovery_warns_on_multiple_matches(tmp_path):
    case = tmp_path / "a"
    case.mkdir()
    (case / "a-id_photo.jpg").write_bytes(b"x")
    (case / "b-id_photo.jpg").write_bytes(b"x")
    warnings: list[str] = []
    cases = discover_case_folders(
        tmp_path, [], front_globs=["*id_photo*.jpg"], warn_cb=warnings.append
    )
    assert len(cases) == 1  # first sorted wins
    assert cases[0].front_path.name == "a-id_photo.jpg"
    assert warnings and "match" in warnings[0].lower()


def test_discovery_empty_names_and_globs_finds_nothing(tmp_path):
    _make_case(tmp_path, "a", "front.jpg")
    assert discover_case_folders(tmp_path, [], front_globs=[]) == []


def test_discovery_glob_skips_non_image_files(tmp_path):
    """A loose glob ('*front*') must not pick a sidecar/.txt/.mp4 that sorts
    before the real image (Codex MEDIUM)."""
    case = tmp_path / "a"
    case.mkdir()
    (case / "front_notes.txt").write_text("x")  # sorts before front.jpg
    (case / "front.mp4").write_bytes(b"x")
    (case / "front.jpg").write_bytes(b"x")
    cases = discover_case_folders(tmp_path, [], front_globs=["*front*"])
    assert len(cases) == 1
    assert cases[0].front_path.name == "front.jpg"


# --------------------------------------------------------------------------
# config: automation_front_globs default + property
# --------------------------------------------------------------------------

def test_front_globs_default_is_empty_list():
    assert AUTOMATION_DEFAULTS["automation_front_globs"] == []


def test_automation_config_front_globs_property_normalizes():
    ac = AutomationConfig({"automation_front_globs": ["*ID*.JPG", "", "  ", "*x*"]})
    assert ac.front_globs == ["*id*.jpg", "*x*"]


# --------------------------------------------------------------------------
# _derive_model_display_name
# --------------------------------------------------------------------------

def test_derive_model_display_name_known_endpoint():
    name = _derive_model_display_name(
        "fal-ai/kling-video/v2.5-turbo/standard/image-to-video"
    )
    # Resolves via models.json to a friendly Kling 2.5 standard name.
    assert "Kling" in name and "Standard" in name


def test_derive_model_display_name_unknown_endpoint_prettifies():
    name = _derive_model_display_name("fal-ai/some-new-model/v9/image-to-video")
    assert name == "Some New Model V9"


def test_derive_model_display_name_empty():
    assert _derive_model_display_name("") == ""
    assert _derive_model_display_name("   ") == ""


# --------------------------------------------------------------------------
# run_automation_headless: override threading + fingerprint sequencing
# --------------------------------------------------------------------------

def _build_app(tmp_path, monkeypatch):
    """A KlingAutomationUI wired with a clean config + a captured runner so no
    real fal.ai work happens. Returns (app, captured) where captured records the
    effective config the runner saw."""
    app = KlingAutomationUI.__new__(KlingAutomationUI)
    app.config = dict(AUTOMATION_DEFAULTS)
    app.config["falai_api_key"] = "test-fal-key"
    app.config["current_model"] = "fal-ai/kling-video/v2.5-turbo/pro/image-to-video"
    app.config["model_display_name"] = "Kling 2.5 Turbo Pro"
    app.config["automation_front_expand_provider"] = "bfl"
    app.config["automation_selfie_expand_provider"] = "bfl"
    app.automation_root_folder = ""
    app.print_red = lambda *_a, **_k: None
    app.print_yellow = lambda *_a, **_k: None
    app._automation_status_lines = lambda: []
    app._get_selected_selfie_prompt = lambda: ("1", "prompt", "slot:1")
    app._write_automation_summary = lambda *a, **k: None
    app._automation_manifest_path = lambda: tmp_path / "automation_manifest.json"

    captured: dict = {}

    import kling_automation_ui as kmod

    class FakeRunner:
        def __init__(self, config, automation_config, manifest, progress_cb=None):
            self.last_case_results = []
            captured["config"] = dict(config)
            self.progress_cb = progress_cb

        def validate_configuration(self):
            return []

        def run(self, cases):
            captured["n_cases"] = len(cases)
            return {"completed": len(cases), "failed": 0, "manual_review": 0, "skipped": 0}

    monkeypatch.setattr(kmod, "AutoPipelineRunner", FakeRunner)
    return app, captured


def _seed_two_cases(tmp_path):
    _make_case(tmp_path, "u1", "front.jpg")
    _make_case(tmp_path, "u2", "front.jpg")


def test_headless_overrides_land_in_runner_config(tmp_path, monkeypatch):
    _seed_two_cases(tmp_path)
    app, captured = _build_app(tmp_path, monkeypatch)
    rc = app.run_automation_headless(
        str(tmp_path),
        model_override="fal-ai/kling-video/v2.5-turbo/standard/image-to-video",
        model_name_override="Kling 2.5 Turbo Standard",
        oldcam_version_override="v13",
        rppg_override=True,
        provider_override="fal",
        outpaint_timeout_override="240",
        max_cases_override="all",
        reprocess_override="overwrite",
    )
    assert rc == 0
    cfg = captured["config"]
    assert cfg["current_model"] == "fal-ai/kling-video/v2.5-turbo/standard/image-to-video"
    assert cfg["model_display_name"] == "Kling 2.5 Turbo Standard"
    assert cfg["automation_oldcam_version"] == ["v13"]
    assert cfg["automation_rppg_enabled"] is True
    assert cfg["automation_front_expand_provider"] == "fal"
    assert cfg["automation_selfie_expand_provider"] == "fal"
    assert cfg["outpaint_fal_timeout_seconds"] == 240
    assert captured["n_cases"] == 2


def test_headless_fingerprinted_overrides_applied_before_manifest(tmp_path, monkeypatch):
    """oldcam-version / rppg / provider are automation_* keys -> they must be on
    the manifest fingerprint, NOT applied after load. Verify the persisted
    manifest fingerprint reflects the overridden values."""
    _seed_two_cases(tmp_path)
    app, _captured = _build_app(tmp_path, monkeypatch)
    rc = app.run_automation_headless(
        str(tmp_path),
        oldcam_version_override="v13",
        rppg_override=True,
        provider_override="fal",
        max_cases_override="all",
        reprocess_override="overwrite",
    )
    assert rc == 0
    import json

    manifest = json.loads((tmp_path / "automation_manifest.json").read_text(encoding="utf-8"))
    # The manifest persists the full automation_* config_snapshot, which is what
    # the fingerprint is computed from on load. Overrides applied BEFORE
    # create_or_load are baked into this snapshot; applied after, they would be
    # absent here and silently no-op on a stale manifest.
    snap = manifest.get("config_snapshot", {})
    assert snap.get("automation_oldcam_version") == ["v13"]
    assert snap.get("automation_rppg_enabled") is True
    assert snap.get("automation_front_expand_provider") == "fal"


def test_headless_identity_override_recreates_stale_manifest(tmp_path, monkeypatch):
    """A stale manifest (old oldcam version) + an explicit --oldcam-version
    override must RECREATE a fresh manifest and run (rc 0), not exit 1 on a
    fingerprint mismatch (Codex HIGH). The old manifest is backed up."""
    _seed_two_cases(tmp_path)
    # Seed an existing manifest fingerprinted for oldcam v24.
    from automation.manifest import AutomationManifest

    manifest_path = tmp_path / "automation_manifest.json"
    AutomationManifest.create_or_load(
        manifest_path=manifest_path,
        root_dir=tmp_path,
        config_snapshot={"automation_oldcam_version": "v24"},
    )
    assert manifest_path.exists()

    app, captured = _build_app(tmp_path, monkeypatch)
    rc = app.run_automation_headless(
        str(tmp_path),
        oldcam_version_override="v13",  # identity change vs the v24 manifest
        max_cases_override="all",
        reprocess_override="overwrite",
    )
    assert rc == 0  # recreated + ran, NOT exit 1
    assert captured["config"]["automation_oldcam_version"] == ["v13"]
    # Old manifest backed up aside.
    assert list(tmp_path.glob("automation_manifest.json.superseded.*"))


def test_headless_oldcam_comma_list_override(tmp_path, monkeypatch):
    """--oldcam-version accepts a comma list; it lands as the canonical
    normalized list (deduped, version-sorted)."""
    _seed_two_cases(tmp_path)
    app, captured = _build_app(tmp_path, monkeypatch)
    rc = app.run_automation_headless(
        str(tmp_path),
        oldcam_version_override="v24,v13",
        max_cases_override="all",
        reprocess_override="overwrite",
    )
    assert rc == 0
    assert captured["config"]["automation_oldcam_version"] == ["v13", "v24"]


def test_headless_oldcam_none_override_skips_oldcam(tmp_path, monkeypatch):
    """--oldcam-version none = explicit empty selection: oldcam step off for
    this run, and the required flag is dropped so validation doesn't reject
    the run as contradictory."""
    _seed_two_cases(tmp_path)
    app, captured = _build_app(tmp_path, monkeypatch)
    app.config["automation_oldcam_required"] = True
    rc = app.run_automation_headless(
        str(tmp_path),
        oldcam_version_override="none",
        max_cases_override="all",
        reprocess_override="overwrite",
    )
    assert rc == 0
    assert captured["config"]["automation_oldcam_version"] == []
    assert captured["config"]["automation_oldcam_required"] is False


def test_legacy_string_manifest_fingerprint_does_not_churn_on_list_form(tmp_path):
    """Representation-only change (old manifest "v13" string vs new ["v13"]
    list) must NOT invalidate the manifest — normalize-on-compare in
    _build_config_fingerprint keeps it loading cleanly. A REAL selection
    change still mismatches."""
    import pytest as _pytest

    from automation.manifest import AutomationManifest

    manifest_path = tmp_path / "automation_manifest.json"
    legacy_snapshot = {
        "automation_oldcam_version": "v13",
        "automation_root_folder": str(tmp_path),
    }
    AutomationManifest.create_or_load(
        manifest_path=manifest_path, root_dir=tmp_path, config_snapshot=legacy_snapshot
    )

    # Same selection, new list representation: loads cleanly (no churn).
    loaded = AutomationManifest.create_or_load(
        manifest_path=manifest_path,
        root_dir=tmp_path,
        config_snapshot={
            "automation_oldcam_version": ["v13"],
            "automation_root_folder": str(tmp_path),
        },
    )
    assert loaded.data["config_snapshot"]["automation_oldcam_version"] == "v13"
    assert not list(tmp_path.glob("automation_manifest.json.superseded.*"))

    # A REAL selection change is still a fingerprint mismatch.
    with _pytest.raises(ValueError, match="fingerprint mismatch"):
        AutomationManifest.create_or_load(
            manifest_path=manifest_path,
            root_dir=tmp_path,
            config_snapshot={
                "automation_oldcam_version": ["v13", "v24"],
                "automation_root_folder": str(tmp_path),
            },
        )


def test_headless_empty_front_globs_override_recreates_stale_manifest(tmp_path, monkeypatch):
    """Clearing globs (front_globs_override=[]) against a manifest fingerprinted
    with non-empty globs is an identity change and must recreate fresh, not exit
    1 (CodeRabbit: the identity check must use `is not None`, not truthiness)."""
    _seed_two_cases(tmp_path)
    from automation.manifest import AutomationManifest

    manifest_path = tmp_path / "automation_manifest.json"
    AutomationManifest.create_or_load(
        manifest_path=manifest_path,
        root_dir=tmp_path,
        config_snapshot={"automation_front_globs": ["*id_photo*.jpg"]},
    )
    app, captured = _build_app(tmp_path, monkeypatch)
    rc = app.run_automation_headless(
        str(tmp_path),
        front_globs_override=[],  # explicit clear -> identity change
        max_cases_override="all",
        reprocess_override="overwrite",
    )
    assert rc == 0
    assert captured["config"]["automation_front_globs"] == []
    assert list(tmp_path.glob("automation_manifest.json.superseded.*"))


def test_headless_invalid_provider_exits_1(tmp_path, monkeypatch):
    _seed_two_cases(tmp_path)
    app, _ = _build_app(tmp_path, monkeypatch)
    rc = app.run_automation_headless(str(tmp_path), provider_override="banana")
    assert rc == 1


def test_headless_invalid_outpaint_timeout_exits_1(tmp_path, monkeypatch):
    _seed_two_cases(tmp_path)
    app, _ = _build_app(tmp_path, monkeypatch)
    rc = app.run_automation_headless(str(tmp_path), outpaint_timeout_override="abc")
    assert rc == 1


def test_headless_outpaint_timeout_out_of_range_is_clamped(tmp_path, monkeypatch):
    _seed_two_cases(tmp_path)
    app, captured = _build_app(tmp_path, monkeypatch)
    rc = app.run_automation_headless(
        str(tmp_path),
        outpaint_timeout_override="5",  # below the [30, 300] floor
        max_cases_override="all",
        reprocess_override="overwrite",
    )
    assert rc == 0
    # Clamped at WRITE time so the stored value matches the effective behavior.
    assert captured["config"]["outpaint_fal_timeout_seconds"] == 30


def test_headless_empty_front_globs_override_clears_saved(tmp_path, monkeypatch):
    _seed_two_cases(tmp_path)
    app, captured = _build_app(tmp_path, monkeypatch)
    app.config["automation_front_globs"] = ["*old*.jpg"]
    rc = app.run_automation_headless(
        str(tmp_path),
        front_globs_override=[],  # explicit empty = clear
        max_cases_override="all",
        reprocess_override="overwrite",
    )
    assert rc == 0
    assert captured["config"]["automation_front_globs"] == []


def test_headless_front_glob_discovers_nonstandard_names(tmp_path, monkeypatch):
    _make_case(tmp_path, "u1", "scan-id_photo-1.jpg")
    _make_case(tmp_path, "u2", "scan-id_photo-2.jpg")
    app, captured = _build_app(tmp_path, monkeypatch)
    rc = app.run_automation_headless(
        str(tmp_path),
        front_globs_override=["*id_photo*.jpg"],
        max_cases_override="all",
        reprocess_override="overwrite",
    )
    assert rc == 0
    assert captured["n_cases"] == 2


# --------------------------------------------------------------------------
# questionary non-TTY fallbacks
# --------------------------------------------------------------------------

def test_confirm_non_tty_uses_input_fallback(monkeypatch):
    app = KlingAutomationUI.__new__(KlingAutomationUI)
    app.config = {}
    monkeypatch.setattr("builtins.input", lambda *_a, **_k: "y")
    assert app._confirm("Proceed?", default=False) is True
    monkeypatch.setattr("builtins.input", lambda *_a, **_k: "")
    assert app._confirm("Proceed?", default=True) is True
    assert app._confirm("Proceed?", default=False) is False


def test_confirm_non_tty_eof_returns_default(monkeypatch):
    app = KlingAutomationUI.__new__(KlingAutomationUI)
    app.config = {}

    def _raise(*_a, **_k):
        raise EOFError()

    monkeypatch.setattr("builtins.input", _raise)
    assert app._confirm("Proceed?", default=True) is True
    assert app._confirm("Proceed?", default=False) is False


def test_automation_menu_choice_non_tty_uses_numeric_input(monkeypatch):
    app = KlingAutomationUI.__new__(KlingAutomationUI)
    app.config = {}
    app._display_automation_menu = lambda: None
    monkeypatch.setattr("builtins.input", lambda *_a, **_k: "6")
    assert app._automation_menu_choice() == "6"


def test_model_presets_shared_constant_includes_default():
    """Both the questionary and legacy model pickers must read _MODEL_PRESETS
    so the default Kling 2.5 Turbo Standard is selectable from either."""
    endpoints = [e for _name, e, _dur in KlingAutomationUI._MODEL_PRESETS]
    assert "fal-ai/kling-video/v2.5-turbo/standard/image-to-video" in endpoints
    # Default is first so it maps to legacy numbered choice "1".
    assert KlingAutomationUI._MODEL_PRESETS[0][1] == (
        "fal-ai/kling-video/v2.5-turbo/standard/image-to-video"
    )


def test_safe_input_returns_default_on_eof(monkeypatch):
    def _raise(*_a, **_k):
        raise EOFError()

    monkeypatch.setattr("builtins.input", _raise)
    assert KlingAutomationUI._safe_input("x", default="fallback") == "fallback"
    assert KlingAutomationUI._safe_input("x") == ""


def test_automation_menu_choice_eof_returns_back(monkeypatch):
    """A closed/piped stdin (EOFError on input) must return '0' (Back), not
    crash the menu loop."""
    app = KlingAutomationUI.__new__(KlingAutomationUI)
    app.config = {}
    app._display_automation_menu = lambda: None

    def _raise(*_a, **_k):
        raise EOFError()

    monkeypatch.setattr("builtins.input", _raise)
    assert app._automation_menu_choice() == "0"


def test_use_legacy_prompt_ui_handles_stdin_without_isatty(monkeypatch):
    """The shared gate must not crash when sys.stdin is None or a custom stream
    lacking isatty() (Windows background service / GUI wrappers / test runners)."""
    import kling_automation_ui as kmod

    monkeypatch.setattr(kmod.sys, "stdin", None, raising=False)
    assert KlingAutomationUI._use_legacy_prompt_ui() is True  # None -> legacy

    class _NoIsatty:
        pass

    monkeypatch.setattr(kmod.sys, "stdin", _NoIsatty(), raising=False)
    # Must not raise AttributeError; absence of isatty -> not a TTY -> legacy.
    assert KlingAutomationUI._use_legacy_prompt_ui() is True


def test_discovery_glob_does_not_match_path_separators(tmp_path):
    """fnmatchcase keeps '*' from matching across path-like names predictably;
    a folder-name-shaped pattern shouldn't accidentally match via os.normcase
    slash rewriting (the reason we use fnmatchcase, not fnmatch)."""
    case = tmp_path / "a"
    case.mkdir()
    (case / "front.jpg").write_bytes(b"x")
    # A pattern with a backslash must not match a plain filename on any OS.
    cases = discover_case_folders(tmp_path, [], front_globs=[r"*\front.jpg"])
    assert cases == []
