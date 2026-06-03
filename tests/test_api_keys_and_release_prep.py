import json
import zipfile
from pathlib import Path

from api_keys import (
    API_KEY_SPECS,
    apply_env_key_fallback,
    ensure_key_fields,
    required_missing_specs,
)
from distribution.build_release import refresh_extracted_bundle
from distribution.release_prep import (
    LATEST_ALIAS_ZIP_NAME,
    RELEASE_VERSION,
    VERSIONED_ZIP_NAME,
    build_sanitized_config,
    bundle_release,
    copy_sanitized_tree,
)
from kling_automation_ui import KlingAutomationUI

EXPECTED_PROMPT_KEYS = {
    "saved_prompts",
    "negative_prompts",
    "prompt_titles",
    "automation_selfie_prompts",
    "automation_selfie_prompt_slot",
    "automation_selfie_prompt_mode",
    "selfie_saved_prompts",
    "selfie_prompt_titles",
    "selfie_prompt_template",
    "selfie_wildcard_saved_prompts",
    "selfie_wildcard_template",
    "outpaint_prompt",
    "face_crop_polish_prompt",
    "openrouter_vision_system_prompt",
}


def test_ensure_key_fields_adds_all_keys():
    config = {}
    changed = ensure_key_fields(config)
    assert changed is True
    for spec in API_KEY_SPECS:
        assert spec.config_key in config
        assert config[spec.config_key] == ""


def test_nothing_is_required_at_start():
    """User direction 2026-06-04: NO key is required at startup — a user may
    only want rPPG/Oldcam (no key) or only fal.ai. required_missing_specs (the
    required_at_start gate) must therefore be empty even with all keys blank."""
    config = {spec.config_key: "" for spec in API_KEY_SPECS}
    assert required_missing_specs(config) == []
    assert all(spec.required_at_start is False for spec in API_KEY_SPECS)


def test_every_key_has_an_env_var_mapping():
    """All four keys must map to a fallback env var so the app can auto-prefill
    from the environment (FAL_KEY / BFL_API_KEY / OPENROUTER_API_KEY /
    FREEIMAGE_API_KEY)."""
    by_key = {spec.config_key: spec.env_var for spec in API_KEY_SPECS}
    assert by_key["falai_api_key"] == "FAL_KEY"
    assert by_key["bfl_api_key"] == "BFL_API_KEY"
    assert by_key["openrouter_api_key"] == "OPENROUTER_API_KEY"
    assert by_key["freeimage_api_key"] == "FREEIMAGE_API_KEY"


def test_falai_accepts_fal_api_key_alias(monkeypatch):
    """User direction 2026-06-04: the user stores their fal.ai key under
    FAL_API_KEY, not FAL_KEY — auto-detect must accept BOTH (first non-empty
    wins). This was the 'fal.ai key still not detected from env' bug."""
    for name in ("FAL_KEY", "FAL_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("FAL_API_KEY", "fal-from-api-key-alias")
    config = {"falai_api_key": ""}
    filled = apply_env_key_fallback(config)
    assert config["falai_api_key"] == "fal-from-api-key-alias"
    assert "falai_api_key" in filled


def test_falai_prefers_fal_key_over_fal_api_key(monkeypatch):
    """When BOTH are set, the first alias (FAL_KEY, fal's native name) wins."""
    monkeypatch.setenv("FAL_KEY", "native")
    monkeypatch.setenv("FAL_API_KEY", "suffix-form")
    config = {"falai_api_key": ""}
    apply_env_key_fallback(config)
    assert config["falai_api_key"] == "native"


def test_every_key_has_at_least_one_env_alias():
    """Every key must map to >=1 env var alias; fal.ai must accept both names."""
    by_key = {spec.config_key: spec.env_vars for spec in API_KEY_SPECS}
    assert "FAL_KEY" in by_key["falai_api_key"]
    assert "FAL_API_KEY" in by_key["falai_api_key"]
    assert "BFL_API_KEY" in by_key["bfl_api_key"]
    assert "OPENROUTER_API_KEY" in by_key["openrouter_api_key"]
    assert "FREEIMAGE_API_KEY" in by_key["freeimage_api_key"]
    assert all(spec.env_vars for spec in API_KEY_SPECS)


def test_apply_env_key_fallback_fills_empty_keys(monkeypatch):
    """An empty key is silently prefilled from its env var."""
    monkeypatch.setenv("FAL_KEY", "fal-from-env")
    monkeypatch.setenv("BFL_API_KEY", "bfl-from-env")
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    monkeypatch.delenv("FREEIMAGE_API_KEY", raising=False)
    config = {spec.config_key: "" for spec in API_KEY_SPECS}
    filled = apply_env_key_fallback(config)
    assert config["falai_api_key"] == "fal-from-env"
    assert config["bfl_api_key"] == "bfl-from-env"
    assert config["openrouter_api_key"] == ""  # no env -> stays empty
    assert set(filled) == {"falai_api_key", "bfl_api_key"}


def test_apply_env_key_fallback_user_value_overrides_env(monkeypatch):
    """A non-empty saved config value ALWAYS wins over the env var."""
    monkeypatch.setenv("FAL_KEY", "fal-from-env")
    config = {"falai_api_key": "user-saved-key"}
    filled = apply_env_key_fallback(config)
    assert config["falai_api_key"] == "user-saved-key"
    assert "falai_api_key" not in filled


def test_apply_env_key_fallback_strips_whitespace(monkeypatch):
    """Whitespace-only config is treated as empty; env value is trimmed."""
    monkeypatch.setenv("FAL_KEY", "  spaced-key  ")
    config = {"falai_api_key": "   "}
    apply_env_key_fallback(config)
    assert config["falai_api_key"] == "spaced-key"


def test_apply_env_key_fallback_noop_when_no_env(monkeypatch):
    """No env vars set -> nothing filled, keys stay empty, returns []."""
    for spec in API_KEY_SPECS:
        if spec.env_var:
            monkeypatch.delenv(spec.env_var, raising=False)
    config = {spec.config_key: "" for spec in API_KEY_SPECS}
    assert apply_env_key_fallback(config) == []
    assert all(config[spec.config_key] == "" for spec in API_KEY_SPECS)


def test_cli_save_config_excludes_env_prefilled_keys(tmp_path: Path):
    """code-review CRITICAL: an env-prefilled key must NEVER be written to disk
    (env stays the source of truth; a shared config must not carry the secret).
    save_config strips keys listed in _env_prefilled_keys."""
    ui = KlingAutomationUI.__new__(KlingAutomationUI)
    ui.config_file = str(tmp_path / "kling_config.json")
    ui.verbose_logging = False
    ui.config = {"falai_api_key": "fal-from-env", "bfl_api_key": "", "other": "x"}
    ui._env_prefilled_keys = ["falai_api_key"]
    ui.save_config()
    on_disk = json.loads((tmp_path / "kling_config.json").read_text())
    assert "falai_api_key" not in on_disk, "env-sourced key must not persist"
    assert on_disk["other"] == "x", "non-key config still persists"
    # In-memory config is untouched (the app still has the usable key this run).
    assert ui.config["falai_api_key"] == "fal-from-env"


def test_cli_clear_env_prefill_marker_makes_key_persist(tmp_path: Path):
    """After the user explicitly sets a key, clearing the env-prefill marker
    makes save_config persist it (their explicit value wins + is durable)."""
    ui = KlingAutomationUI.__new__(KlingAutomationUI)
    ui.config_file = str(tmp_path / "kling_config.json")
    ui.verbose_logging = False
    ui.config = {"falai_api_key": "fal-from-env"}
    ui._env_prefilled_keys = ["falai_api_key"]
    # User explicitly re-enters the key:
    ui.config["falai_api_key"] = "user-typed-key"
    ui._clear_env_prefill_marker("falai_api_key")
    ui.save_config()
    on_disk = json.loads((tmp_path / "kling_config.json").read_text())
    assert on_disk["falai_api_key"] == "user-typed-key", (
        "an explicitly-entered key must persist after clearing the env marker"
    )


def test_cli_save_config_no_env_marker_persists_everything(tmp_path: Path):
    """Back-compat: with no _env_prefilled_keys attr, save_config writes the
    whole config unchanged (the common case for a user with saved keys)."""
    ui = KlingAutomationUI.__new__(KlingAutomationUI)
    ui.config_file = str(tmp_path / "kling_config.json")
    ui.verbose_logging = False
    ui.config = {"falai_api_key": "saved-key", "x": 1}
    ui.save_config()
    on_disk = json.loads((tmp_path / "kling_config.json").read_text())
    assert on_disk == {"falai_api_key": "saved-key", "x": 1}


def test_build_sanitized_config_clears_keys_and_paths(tmp_path: Path):
    template = tmp_path / "default_config_template.json"
    template.write_text(
        json.dumps(
            {
                "falai_api_key": "secret",
                "bfl_api_key": "secret",
                "openrouter_api_key": "secret",
                "freeimage_api_key": "secret",
                "output_folder": "C:/private",
                "automation_root_folder": "C:/private/root",
                "selfie_output_folder": "C:/private/selfie",
                # v2.7 audit caught these two "last_*" path fields leaking
                # subject names + dev machine paths into the shipped zip
                # (CLAUDE.md Trap 4).
                "oldcam_last_source_video": (
                    "C:/Users/dev/Downloads/subject_video.mp4"
                ),
                "video_inspector_last_folder": (
                    "F:/Downloads/organized/SUBJECT-FULL-NAME"
                ),
                "selfie_prompt_template": "keep me",
                "outpaint_prompt": "expand bg",
                "saved_prompts": {"1": "prompt one"},
            }
        ),
        encoding="utf-8",
    )
    sanitized = build_sanitized_config(template)
    for spec in API_KEY_SPECS:
        assert sanitized[spec.config_key] == ""
    assert sanitized["output_folder"] == ""
    assert sanitized["automation_root_folder"] == ""
    assert sanitized["selfie_output_folder"] == ""
    # New blank-list members added 2026-05-28 (v2.7 audit).
    assert sanitized["oldcam_last_source_video"] == ""
    assert sanitized["video_inspector_last_folder"] == ""
    # And the non-secret prompt/setting content must still come through
    # untouched (the "ship everything except secrets" contract).
    assert sanitized["selfie_prompt_template"] == "keep me"
    assert sanitized["outpaint_prompt"] == "expand bg"
    assert sanitized["saved_prompts"]["1"] == "prompt one"


def test_release_forces_active_slot_prompt_and_overrides_cfg(tmp_path: Path):
    """PR #41 (user request): the v2.1 bundle ships
    current_prompt_slot=3 with slot 3 carrying its OWN distinct
    "enhanced for kling 2.5 pro" prompt + negative, while slot 1 keeps
    the proven minimal-motion fallback. Each forced slot must take its
    OWN template text (NOT slot-1 stamped onto every slot), the active
    slot's title must ship, current_prompt_slot must be pinned, and a
    stale dev cfg_scale must be OVERRIDDEN to the template value."""
    template = tmp_path / "default_config_template.json"
    template.write_text(
        json.dumps(
            {
                "current_prompt_slot": 3,
                "saved_prompts": {
                    "1": "MINIMAL motion fallback",
                    "3": "ENHANCED for kling 2.5 pro",
                },
                "negative_prompts": {
                    "1": "NEG one",
                    "3": "NEG three",
                },
                "prompt_titles": {"3": "enhanced for kling 2.5 pro"},
                "cfg_scale_value": 0.7,
            }
        ),
        encoding="utf-8",
    )
    live = tmp_path / "kling_config.json"
    live.write_text(
        json.dumps(
            {
                # Dev machine carries STALE slot text + old cfg.
                "current_prompt_slot": 1,
                "saved_prompts": {"1": "old s1", "3": "OLD stale slot3"},
                "negative_prompts": {"3": "old neg3"},
                "prompt_titles": {"3": "old title"},
                "cfg_scale_value": 0.5,
            }
        ),
        encoding="utf-8",
    )
    cfg = build_sanitized_config(template, live)
    # Slot 1 keeps ITS own text; slot 3 (active) keeps ITS own
    # distinct enhanced text — NOT slot-1 stamped onto both.
    assert cfg["saved_prompts"]["1"] == "MINIMAL motion fallback"
    assert cfg["saved_prompts"]["3"] == "ENHANCED for kling 2.5 pro"
    assert cfg["negative_prompts"]["1"] == "NEG one"
    assert cfg["negative_prompts"]["3"] == "NEG three"
    # The active slot's title ships so the GUI shows the right label.
    assert cfg["prompt_titles"]["3"] == "enhanced for kling 2.5 pro"
    # current_prompt_slot pinned to the template value (3), the dev's
    # stale 1 must not survive.
    assert cfg["current_prompt_slot"] == 3
    # Stale 0.5 OVERRIDDEN, not preserved.
    assert cfg["cfg_scale_value"] == 0.7
    # Other forced defaults still hold.
    assert cfg["current_model"] == "fal-ai/kling-video/v2.5-turbo/pro/image-to-video"
    assert cfg["lock_end_frame"] is True


def test_default_config_template_contains_prompt_families():
    template = Path("default_config_template.json")
    loaded = json.loads(template.read_text(encoding="utf-8"))
    for key in EXPECTED_PROMPT_KEYS:
        assert key in loaded


def test_copy_sanitized_tree_skips_personal_files(tmp_path: Path):
    src = tmp_path / "src"
    dst = tmp_path / "dst"
    src.mkdir()
    (src / "kling_config.json").write_text('{"falai_api_key":"secret"}', encoding="utf-8")
    (src / "kling_gui.log").write_text("private", encoding="utf-8")
    (src / "run_gui.sh").write_text("#!/usr/bin/env bash", encoding="utf-8")
    (src / "launchers").mkdir()
    (src / "launchers" / "run_gui.command").write_text("echo hi", encoding="utf-8")
    copy_sanitized_tree(src, dst)
    assert not (dst / "kling_config.json").exists()
    assert not (dst / "kling_gui.log").exists()
    assert (dst / "run_gui.sh").exists()
    assert (dst / "launchers" / "run_gui.command").exists()


def test_copy_sanitized_tree_prunes_excluded_directories(tmp_path: Path):
    src = tmp_path / "src"
    dst = tmp_path / "dst"
    (src / ".git" / "nested").mkdir(parents=True)
    (src / ".git" / "nested" / "secret.txt").write_text("secret", encoding="utf-8")
    (src / "normal").mkdir(parents=True)
    (src / "normal" / "file.txt").write_text("ok", encoding="utf-8")

    copy_sanitized_tree(src, dst)

    assert not (dst / ".git").exists()
    assert (dst / "normal" / "file.txt").exists()


def test_copy_sanitized_tree_excludes_tests_and_scratch(tmp_path: Path):
    src = tmp_path / "src"
    dst = tmp_path / "dst"
    (src / "tests").mkdir(parents=True)
    (src / "tests" / "test_a.py").write_text("x=1", encoding="utf-8")
    (src / "reviews").mkdir(parents=True)
    (src / "reviews" / "notes.md").write_text("private", encoding="utf-8")
    (src / ".recovery").mkdir(parents=True)
    (src / ".recovery" / "secret.txt").write_text("private", encoding="utf-8")
    (src / ".tmp_pytest").mkdir(parents=True)
    (src / ".tmp_pytest" / "temp.txt").write_text("private", encoding="utf-8")
    (src / "map-codebase-session-abc.md").write_text("private", encoding="utf-8")
    (src / "session-ses_123.md").write_text("private", encoding="utf-8")
    (src / "normal.py").write_text("ok", encoding="utf-8")

    copy_sanitized_tree(src, dst)

    assert not (dst / "tests").exists()
    assert not (dst / "reviews").exists()
    assert not (dst / ".recovery").exists()
    assert not (dst / ".tmp_pytest").exists()
    assert not (dst / "map-codebase-session-abc.md").exists()
    assert not (dst / "session-ses_123.md").exists()
    assert (dst / "normal.py").exists()


def test_copy_sanitized_tree_excludes_all_venv_variants(tmp_path: Path):
    # Regression guard for the build-bloat bug where `.venv311` was
    # MISSING from EXCLUDED_DIRS, causing the local Python 3.11 venv to
    # be bundled and ballooning the release zip from ~10MB to 532MB.
    # Every venv flavor a contributor might plausibly create — canonical,
    # platform-suffixed, version-suffixed across the Python lifecycle,
    # dotted AND undotted forms — must be pruned.
    #
    # Two-pronged guard:
    #   1. Derive variants from EXCLUDED_DIRS so any future addition is
    #      automatically exercised (no test/impl drift — Sourcery round 1).
    #   2. Assert the explicit minimum set is present so a silent removal
    #      from EXCLUDED_DIRS makes the test fail loudly (anti-circularity
    #      — Gemini round 2). The derive-only form would silently shrink
    #      its check set in step with the implementation regression.
    from distribution.release_prep import EXCLUDED_DIRS

    EXPECTED_MINIMUM = {
        "venv", ".venv", ".venv-macos",
        ".venv311", ".venv312", ".venv313", ".venv314",
        "venv311", "venv312", "venv313", "venv314",
    }
    src = tmp_path / "src"
    dst = tmp_path / "dst"
    venv_variants = tuple(sorted(
        name for name in EXCLUDED_DIRS
        if name.startswith("venv") or name.startswith(".venv")
    ))
    missing = EXPECTED_MINIMUM - set(venv_variants)
    assert not missing, (
        f"regression: EXCLUDED_DIRS no longer covers expected venv "
        f"variants: {sorted(missing)} — original .venv311 build-bloat "
        f"bug class"
    )
    for v in venv_variants:
        (src / v / "bin").mkdir(parents=True)
        (src / v / "bin" / "python").write_text("#!/fake", encoding="utf-8")
    (src / "kept.py").write_text("ok", encoding="utf-8")

    copy_sanitized_tree(src, dst)

    for v in venv_variants:
        assert not (dst / v).exists(), f"venv variant {v!r} leaked into release bundle"
    assert (dst / "kept.py").exists()


def test_copy_sanitized_tree_excludes_local_only_research_dirs(tmp_path: Path):
    """Regression guard for the Windows-side dist-bloat bug discovered after
    PR #50 merged: a contributor's working tree contains several gitignored
    research/A-B-testing dirs that ``release_prep.copy_sanitized_tree`` was
    not pruning. The build script sweeps the working tree (not git
    ls-files), so being in ``.gitignore`` alone doesn't save them — they
    must also be in ``EXCLUDED_DIRS``.

    Observed before fix: 182 MB release zip (134 MB from oldcam-testing
    .mp4 fixtures, 35 MB from test-material/, plus oldcam_reference_bundle
    and analysis_frames). After fix: 9.85 MB. Same bug class as the
    .venv311 miss in PR #50.

    PR #51 round-1 code review additionally caught:
      - CRITICAL: ``sourav_facetrack_results.json`` /
        ``sourav_kinematic_results.json`` shipped to every release with
        78+40 SSN-format identifiers (real PII leak).
      - HIGH: stray ``*.zip`` siblings (oldcam_reference_bundle.zip,
        oldcam-v13.zip, rppg_injector-v8.zip) all gitignored but shipping.
      - HIGH: ``oldcam-testing/reports/`` (12 A/B HTML reports) shipping.
    All three classes are now guarded below.
    """
    # Round-2 review (subagent M1): derive the expected set from the source of
    # truth in release_prep.py — same two-pronged pattern as PR #50's
    # venv-variants test. Anti-circularity (EXPECTED_MINIMUM) catches silent
    # removals; derive (LOCAL_ONLY_RESEARCH_DIRS) catches silent renames.
    from distribution.release_prep import (
        EXCLUDED_DIRS,
        EXCLUDED_FILES,
        LOCAL_ONLY_RESEARCH_DIRS,
        PII_EXCLUDED_FILES,
    )

    EXPECTED_MINIMUM_DIRS = {
        "oldcam_reference_bundle",
        "analysis_frames",
        "test-material",
        "rppg_harness_out",
        "_friend_logs",  # PII: friend debug logs (Codex P1 PR #72) — must stay excluded
    }
    missing_minimum = EXPECTED_MINIMUM_DIRS - LOCAL_ONLY_RESEARCH_DIRS
    assert not missing_minimum, (
        f"regression: LOCAL_ONLY_RESEARCH_DIRS no longer covers the local-only "
        f"research dirs that bloated the Windows dist zip: "
        f"{sorted(missing_minimum)}"
    )
    # And every name in the constant must actually be in EXCLUDED_DIRS (the
    # `EXCLUDED_DIRS |= LOCAL_ONLY_RESEARCH_DIRS` merge must hold).
    assert LOCAL_ONLY_RESEARCH_DIRS <= EXCLUDED_DIRS, (
        f"regression: LOCAL_ONLY_RESEARCH_DIRS is not merged into EXCLUDED_DIRS — "
        f"the dir-name match in _should_skip won't fire. "
        f"Missing: {sorted(LOCAL_ONLY_RESEARCH_DIRS - EXCLUDED_DIRS)}"
    )

    # PR #51 round-1 CRITICAL: PII-bearing corpus measurement outputs
    EXPECTED_MINIMUM_PII = {"sourav_facetrack_results.json", "sourav_kinematic_results.json"}
    missing_pii_minimum = EXPECTED_MINIMUM_PII - PII_EXCLUDED_FILES
    assert not missing_pii_minimum, (
        f"PII regression: PII_EXCLUDED_FILES no longer covers the corpus "
        f"measurement outputs containing SSN-format identifiers: "
        f"{sorted(missing_pii_minimum)}"
    )
    assert PII_EXCLUDED_FILES <= EXCLUDED_FILES, (
        f"regression: PII_EXCLUDED_FILES is not merged into EXCLUDED_FILES — "
        f"the file-name match in _should_skip won't fire. "
        f"Missing: {sorted(PII_EXCLUDED_FILES - EXCLUDED_FILES)}"
    )

    expected_excluded = LOCAL_ONLY_RESEARCH_DIRS  # alias for the rest of the test below

    src = tmp_path / "src"
    dst = tmp_path / "dst"
    # All dirs in expected_excluded are pruned by dir-name match in EXCLUDED_DIRS.
    # CodeRabbit round-1 finding: ``rppg_harness_out`` is the one case where the
    # actual leak path is nested (``oldcam-testing/rppg_harness_out/``), not at
    # repo root — exercise it that way so the fixture mirrors the real bug.
    for d in expected_excluded - {"rppg_harness_out"}:
        (src / d).mkdir(parents=True)
        (src / d / "fixture.bin").write_bytes(b"x" * 1024)
    # The oldcam-testing/ dir itself ships (frozen A/B test scripts), but
    # the *.mp4 byproducts inside it must be pruned by the path-aware filter.
    # Cover BOTH top-level and nested (`oldcam-testing/sub/inner.mp4`) cases.
    (src / "oldcam-testing").mkdir()
    # rppg_harness_out at the production leak location (nested under oldcam-testing/)
    (src / "oldcam-testing" / "rppg_harness_out").mkdir()
    (src / "oldcam-testing" / "rppg_harness_out" / "fixture.bin").write_bytes(b"x" * 1024)
    (src / "oldcam-testing" / "oldcam_v24.py").write_text("# frozen", encoding="utf-8")
    (src / "oldcam-testing" / "fixture-video.mp4").write_bytes(b"VID" * 1024)
    (src / "oldcam-testing" / "subdir").mkdir()
    (src / "oldcam-testing" / "subdir" / "nested.mp4").write_bytes(b"NESTED" * 256)
    # PR #51 round-1 HIGH: gitignored A/B HTML reports must be pruned too
    (src / "oldcam-testing" / "reports").mkdir()
    (src / "oldcam-testing" / "reports" / "v24_report.html").write_text("<html/>", encoding="utf-8")
    # PR #51 round-1 HIGH: stray *.zip siblings (.gitignore: *.zip)
    (src / "oldcam_reference_bundle.zip").write_bytes(b"ZIP" * 1024)
    (src / "rPPG").mkdir()
    (src / "rPPG" / "rppg_injector-v8.zip").write_bytes(b"ZIP" * 1024)
    # PR #51 round-1 CRITICAL: PII files in docs/analysis/
    (src / "docs" / "analysis").mkdir(parents=True)
    (src / "docs" / "analysis" / "sourav_facetrack_results.json").write_text(
        '[{"persona": "DUPE - 108-62-9880"}]', encoding="utf-8",
    )
    (src / "docs" / "analysis" / "sourav_kinematic_results.json").write_text(
        '[{"persona": "DUPE - 108-62-9880"}]', encoding="utf-8",
    )
    (src / "docs" / "analysis" / "harmless_keeper.py").write_text("# ok", encoding="utf-8")
    (src / "kept.py").write_text("ok", encoding="utf-8")

    copy_sanitized_tree(src, dst)

    # The big bloat dirs are gone entirely. rppg_harness_out is checked at
    # its real nested location (oldcam-testing/rppg_harness_out/) per the
    # CodeRabbit round-1 finding.
    for d in expected_excluded - {"rppg_harness_out"}:
        assert not (dst / d).exists(), f"local-only dir {d!r} leaked into release bundle"
    assert not (dst / "oldcam-testing" / "rppg_harness_out").exists(), (
        "oldcam-testing/rppg_harness_out/ leaked — dir-name EXCLUDED_DIRS regression"
    )
    # oldcam-testing/ survives but only its .py scripts ship
    assert (dst / "oldcam-testing" / "oldcam_v24.py").exists()
    assert not (dst / "oldcam-testing" / "fixture-video.mp4").exists(), (
        "oldcam-testing/*.mp4 fixture leaked — path-aware extension filter "
        "regression"
    )
    # Nested mp4 case: path.parts walks the full relative path so the filter
    # catches `oldcam-testing/subdir/nested.mp4` too. A refactor that scoped
    # the check to ``path.parent.name == "oldcam-testing"`` would break this
    # and the subagent flagged the risk in PR #51 round-1.
    assert not (dst / "oldcam-testing" / "subdir" / "nested.mp4").exists(), (
        "oldcam-testing/subdir/*.mp4 leaked — nested .mp4 filter regression"
    )
    # PR #51 round-1: oldcam-testing/reports/ pruned
    assert not (dst / "oldcam-testing" / "reports").exists(), (
        "oldcam-testing/reports/ A/B HTML reports leaked into release zip"
    )
    # PR #51 round-1: stray *.zip artifacts pruned
    assert not (dst / "oldcam_reference_bundle.zip").exists(), (
        "stray *.zip artifact leaked into release zip — the SAME confidential "
        "content as the oldcam_reference_bundle/ dir we exclude"
    )
    assert not (dst / "rPPG" / "rppg_injector-v8.zip").exists(), (
        "rPPG/*.zip artifact leaked"
    )
    # PR #51 round-1: PII files pruned. PR #61 release-hardening: docs/analysis/
    # is now pruned WHOLESALE (it is a local-only research dir, gitignored as
    # `docs/analysis/`), so the whole dir — including the formerly-kept
    # harmless_keeper.py — must NOT ship. The dedicated leak-guard test
    # test_copy_sanitized_tree_excludes_local_analysis_artifacts below proves
    # the anchored-prefix prune doesn't over-reach into real docs/ files.
    assert not (dst / "docs" / "analysis").exists(), (
        "docs/analysis/ (local-only research dir, contains the PII JSONs) "
        "leaked into release bundle"
    )
    assert (dst / "kept.py").exists()


def test_copy_sanitized_tree_excludes_local_analysis_artifacts(tmp_path: Path):
    """Regression guard for the PR #61 release-hardening pass: local-only
    analysis artifacts were added to .gitignore but NOT to the release sweep,
    so they leaked into the friend-facing personal zip. Same bug class as the
    PR #51 research-dir leak — ``copy_sanitized_tree`` walks the WORKING TREE,
    not git ls-files, so .gitignore alone doesn't shield them.

    Classes guarded (all gitignored, all confirmed present + SHIP-ing before
    the fix):
      - Top-level briefs: OLDCAM_DECISION_BRIEF.md, OLDCAM_GUIDE.md (~130 KB
        of internal A/B decision notes).
      - docs/analysis/ : committed-but-gitignored A/B study scripts/frames/JSON.
      - rPPG/iteration_history/ : per-run rPPG iteration byproducts.
      - rPPG/temp_iteration_N.mp4 + rPPG/best_iteration_snapshot[_N].mp4 :
        injector iteration scratch files (rppg-wiring.md gotcha #2).
    """
    from distribution.release_prep import (
        EXCLUDED_FILES,
        LOCAL_ANALYSIS_DIR_PREFIXES,
        LOCAL_ANALYSIS_FILES,
    )

    # Anti-drift: the named briefs must be merged into EXCLUDED_FILES so the
    # file-name match in _should_skip fires (mirrors the PII/LOCAL_ONLY pattern).
    assert LOCAL_ANALYSIS_FILES <= EXCLUDED_FILES, (
        f"regression: LOCAL_ANALYSIS_FILES not merged into EXCLUDED_FILES — "
        f"missing {sorted(LOCAL_ANALYSIS_FILES - EXCLUDED_FILES)}"
    )
    assert ("docs", "analysis") in LOCAL_ANALYSIS_DIR_PREFIXES
    assert ("rPPG", "iteration_history") in LOCAL_ANALYSIS_DIR_PREFIXES

    src = tmp_path / "src"
    dst = tmp_path / "dst"
    layout = {
        "OLDCAM_DECISION_BRIEF.md": "brief",          # SKIP
        "OLDCAM_GUIDE.md": "guide",                   # SKIP
        "docs/analysis/study.py": "s",                # SKIP (dir prefix)
        "docs/analysis/frames/f.png": "p",            # SKIP (nested)
        "docs/macos-portability.md": "real doc",      # KEEP (real docs file)
        "rPPG/iteration_history/run.json": "j",       # SKIP (dir prefix)
        "rPPG/temp_iteration_3.mp4": "v",             # SKIP (scratch mp4)
        "rPPG/best_iteration_snapshot.mp4": "v",      # SKIP (scratch mp4)
        "rPPG/best_iteration_snapshot_2.mp4": "v",    # SKIP (numbered scratch)
        "rPPG/rppg_injector.py": "code",              # KEEP (real injector code)
        "kept.py": "ok",                              # KEEP
    }
    for rel, content in layout.items():
        p = src / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")

    copy_sanitized_tree(src, dst)

    # Pruned: every gitignored analysis artifact class.
    assert not (dst / "OLDCAM_DECISION_BRIEF.md").exists()
    assert not (dst / "OLDCAM_GUIDE.md").exists()
    assert not (dst / "docs" / "analysis").exists()
    assert not (dst / "rPPG" / "iteration_history").exists()
    assert not (dst / "rPPG" / "temp_iteration_3.mp4").exists()
    assert not (dst / "rPPG" / "best_iteration_snapshot.mp4").exists()
    assert not (dst / "rPPG" / "best_iteration_snapshot_2.mp4").exists()
    # Kept: real code + real docs (the anchored prefix must NOT over-prune).
    assert (dst / "kept.py").exists()
    assert (dst / "docs" / "macos-portability.md").exists()
    assert (dst / "rPPG" / "rppg_injector.py").exists()


def test_standalone_windows_launchers_use_stable_python_probes():
    similarity_gui = Path("similarity/run_gui.bat").read_text(encoding="utf-8")
    similarity_cli = Path("similarity/run_cli.bat").read_text(encoding="utf-8")
    oldcam_v8 = Path("oldcam-v8/oldcam_launcher.bat").read_text(encoding="utf-8")

    assert "%REPO_ROOT%\\venv\\Scripts\\python.exe" in similarity_gui
    assert ".launcher_state" in similarity_gui

    assert "%REPO_ROOT%\\venv\\Scripts\\python.exe" in similarity_cli
    assert ".launcher_state" in similarity_cli

    assert "%REPO_ROOT%\\venv\\Scripts\\python.exe" in oldcam_v8
    assert ".launcher_state" in oldcam_v8


def test_refresh_extracted_bundle_replaces_stale_files(tmp_path: Path):
    dist = tmp_path / "dist"
    stale = dist / "SelfieGenUltimate" / "selfie-gen-ultimate" / "similarity"
    stale.mkdir(parents=True, exist_ok=True)
    (stale / "run_gui.bat").write_text("@echo off\r\npython3.12 -V\r\n", encoding="utf-8")
    (stale / "old.txt").write_text("stale", encoding="utf-8")

    zip_path = dist / "SelfieGenUltimate.zip"
    src = tmp_path / "zip_src" / "selfie-gen-ultimate" / "similarity"
    src.mkdir(parents=True, exist_ok=True)
    (src / "run_gui.bat").write_text("@echo off\r\nfor %%V in (3.12 3.11) do py -%%V -V\r\n", encoding="utf-8")
    (src / "run_cli.bat").write_text("@echo off\r\nfor %%V in (3.12 3.11) do py -%%V -V\r\n", encoding="utf-8")
    oldcam = tmp_path / "zip_src" / "selfie-gen-ultimate" / "oldcam-v8"
    oldcam.mkdir(parents=True, exist_ok=True)
    (oldcam / "oldcam_launcher.bat").write_text("@echo off\r\nfor %%V in (3.12 3.11) do py -%%V -V\r\n", encoding="utf-8")
    with zipfile.ZipFile(zip_path, "w") as zf:
        zf.write(src / "run_gui.bat", arcname="selfie-gen-ultimate/similarity/run_gui.bat")
        zf.write(src / "run_cli.bat", arcname="selfie-gen-ultimate/similarity/run_cli.bat")
        zf.write(oldcam / "oldcam_launcher.bat", arcname="selfie-gen-ultimate/oldcam-v8/oldcam_launcher.bat")

    extracted_root = refresh_extracted_bundle(zip_path, dist)
    assert extracted_root == dist / "SelfieGenUltimate"
    refreshed_gui = (extracted_root / "selfie-gen-ultimate" / "similarity" / "run_gui.bat").read_text(encoding="utf-8")
    assert "py -%%V" in refreshed_gui
    assert "python3.12 -V" not in refreshed_gui
    assert not (extracted_root / "selfie-gen-ultimate" / "similarity" / "old.txt").exists()


def test_bundle_release_creates_universal_zip_with_top_level_launchers(tmp_path: Path):
    repo = tmp_path / "repo"
    dist = tmp_path / "dist"
    repo.mkdir()
    (repo / "default_config_template.json").write_text(
        json.dumps(
            {
                "falai_api_key": "secret",
                "bfl_api_key": "secret",
                "openrouter_api_key": "secret",
                "freeimage_api_key": "secret",
                "output_folder": "C:/private",
                "automation_root_folder": "C:/private/root",
                "selfie_output_folder": "C:/private/selfie",
                "window_geometry": "100x100+0+0",
                "saved_prompts": {"1": "kling prompt"},
                "negative_prompts": {"1": "bad prompt"},
                "prompt_titles": {"1": "title one"},
                "automation_selfie_prompts": {"1": "selfie auto prompt"},
                "automation_selfie_prompt_slot": 1,
                "automation_selfie_prompt_mode": "wildcards",
                "selfie_saved_prompts": {"1": "selfie prompt"},
                "selfie_prompt_titles": {"1": "selfie title"},
                "selfie_prompt_template": "selfie template",
                "selfie_wildcard_saved_prompts": {"1": "wildcard prompt"},
                "selfie_wildcard_template": "wildcard template",
                "outpaint_prompt": "outpaint it",
                "face_crop_polish_prompt": "polish it",
                "openrouter_vision_system_prompt": "vision system",
            }
        ),
        encoding="utf-8",
    )
    (repo / "run_gui.sh").write_text("#!/usr/bin/env bash\necho gui\n", encoding="utf-8")
    (repo / "run_cli.sh").write_text("#!/usr/bin/env bash\necho cli\n", encoding="utf-8")
    (repo / "run_gui.command").write_text("#!/usr/bin/env bash\necho gui\n", encoding="utf-8")
    (repo / "run_cli.command").write_text("#!/usr/bin/env bash\necho cli\n", encoding="utf-8")
    (repo / "setup_macos.sh").write_text("#!/usr/bin/env bash\necho setup\n", encoding="utf-8")
    (repo / "face_landmarker.task").write_bytes(b"task-model")
    # v2.11: the project-wide pip constraints file must be packaged (launchers
    # pass it via -c; in-app repair reads it). Assert release_prep includes it.
    (repo / "constraints.txt").write_text("numpy>=1.26,<2\n", encoding="utf-8")
    (repo / "launchers").mkdir()
    (repo / "launchers" / "windows").mkdir()
    (repo / "launchers" / "macos").mkdir()
    (repo / "launchers" / "windows" / "run_gui.bat").write_text("@echo off\r\necho gui\r\n", encoding="utf-8")
    (repo / "launchers" / "windows" / "run_cli.bat").write_text("@echo off\r\necho cli\r\n", encoding="utf-8")
    (repo / "launchers" / "windows" / "run_similarity_gui.bat").write_text("@echo off\r\ncall ..\\..\\similarity\\run_gui.bat\r\n", encoding="utf-8")
    (repo / "launchers" / "windows" / "run_similarity_cli.bat").write_text("@echo off\r\ncall ..\\..\\similarity\\run_cli.bat\r\n", encoding="utf-8")
    (repo / "launchers" / "windows" / "run_oldcam_v8.bat").write_text("@echo off\r\ncall ..\\..\\oldcam-v8\\oldcam_launcher.bat\r\n", encoding="utf-8")
    (repo / "launchers" / "windows" / "run_oldcam_v7.bat").write_text("@echo off\r\ncall ..\\..\\oldcam-v7\\oldcam_launcher.bat\r\n", encoding="utf-8")
    (repo / "launchers" / "macos" / "run_similarity_gui.command").write_text("#!/usr/bin/env bash\nexec ../../similarity/run_gui.command\n", encoding="utf-8")
    (repo / "launchers" / "macos" / "run_similarity_cli.command").write_text("#!/usr/bin/env bash\nexec ../../similarity/run_cli.command\n", encoding="utf-8")
    (repo / "launchers" / "macos" / "run_oldcam_v8.command").write_text("#!/usr/bin/env bash\nexec ../../oldcam-v8/macOS/oldcam.command\n", encoding="utf-8")
    (repo / "launchers" / "macos" / "run_oldcam_v7.command").write_text("#!/usr/bin/env bash\nexec ../../oldcam-v7/macOS/oldcam.command\n", encoding="utf-8")
    (repo / "launchers" / "run_similarity_gui.bat").write_text("@echo off\r\ncall windows\\run_similarity_gui.bat\r\n", encoding="utf-8")
    (repo / "launchers" / "run_similarity_cli.bat").write_text("@echo off\r\ncall windows\\run_similarity_cli.bat\r\n", encoding="utf-8")
    (repo / "launchers" / "run_oldcam_v8.bat").write_text("@echo off\r\ncall windows\\run_oldcam_v8.bat\r\n", encoding="utf-8")
    (repo / "launchers" / "run_oldcam_v7.bat").write_text("@echo off\r\ncall windows\\run_oldcam_v7.bat\r\n", encoding="utf-8")
    (repo / "launchers" / "run_similarity_gui.command").write_text("#!/usr/bin/env bash\nexec ./macos/run_similarity_gui.command\n", encoding="utf-8")
    (repo / "launchers" / "run_similarity_cli.command").write_text("#!/usr/bin/env bash\nexec ./macos/run_similarity_cli.command\n", encoding="utf-8")
    (repo / "launchers" / "run_oldcam_v8.command").write_text("#!/usr/bin/env bash\nexec ./macos/run_oldcam_v8.command\n", encoding="utf-8")
    (repo / "launchers" / "run_oldcam_v7.command").write_text("#!/usr/bin/env bash\nexec ./macos/run_oldcam_v7.command\n", encoding="utf-8")
    (repo / "similarity").mkdir()
    (repo / "similarity" / "run_gui.bat").write_text(
        "@echo off\r\nset PYTHON_BIN=py -3.12\r\nif exist .venv\\Scripts\\python.exe echo ok\r\n",
        encoding="utf-8",
    )
    (repo / "similarity" / "run_cli.bat").write_text(
        "@echo off\r\nset PYTHON_BIN=py -3.12\r\nif exist .venv\\Scripts\\python.exe echo ok\r\n",
        encoding="utf-8",
    )
    (repo / "oldcam-v8").mkdir()
    (repo / "oldcam-v8" / "oldcam_launcher.bat").write_text(
        "@echo off\r\nset PYTHON_CMD=py -3.12\r\nif exist .venv\\Scripts\\python.exe echo ok\r\n",
        encoding="utf-8",
    )
    (repo / "kling_gui.log").write_text("private", encoding="utf-8")
    (repo / "kling_config.json").write_text("private", encoding="utf-8")
    (repo / "tests").mkdir()
    (repo / "tests" / "test_x.py").write_text("x=1", encoding="utf-8")

    created = list(bundle_release(repo, dist))
    names = sorted(path.name for path in created)
    assert names == sorted([LATEST_ALIAS_ZIP_NAME, VERSIONED_ZIP_NAME])
    assert RELEASE_VERSION.startswith("v")
    versioned_zip = dist / VERSIONED_ZIP_NAME
    latest_zip = dist / LATEST_ALIAS_ZIP_NAME
    assert versioned_zip.exists()
    assert latest_zip.exists()

    with zipfile.ZipFile(versioned_zip) as zf:
        names = zf.namelist()
        # FLAT layout (user direction 2026-06-04): the app must sit at the ZIP
        # ROOT, NOT under a nested ``selfie-gen-ultimate/`` folder (that made an
        # ugly ``…-personal/selfie-gen-ultimate/<app>`` double nest on extract).
        assert not any(name.startswith("selfie-gen-ultimate/") for name in names), (
            "release zip must be FLAT — no nested selfie-gen-ultimate/ wrapper dir"
        )
        assert "Start GUI.bat" in names, "Start GUI.bat must be at the zip root"
        assert any(name.startswith("launchers/") for name in names), (
            "launchers/ must be at the zip root (flat layout)"
        )
        assert any(name.endswith("Start GUI.bat") for name in names)
        assert any(name.endswith("Start CLI.bat") for name in names)
        assert any(name.endswith("Start GUI.command") for name in names)
        assert any(name.endswith("Start CLI.command") for name in names)
        assert any(name.endswith("launchers/windows/run_similarity_gui.bat") for name in names)
        assert any(name.endswith("launchers/windows/run_similarity_cli.bat") for name in names)
        assert any(name.endswith("launchers/windows/run_oldcam_v8.bat") for name in names)
        assert any(name.endswith("launchers/windows/run_oldcam_v7.bat") for name in names)
        assert any(name.endswith("launchers/macos/run_similarity_gui.command") for name in names)
        assert any(name.endswith("launchers/macos/run_similarity_cli.command") for name in names)
        assert any(name.endswith("launchers/macos/run_oldcam_v8.command") for name in names)
        assert any(name.endswith("launchers/macos/run_oldcam_v7.command") for name in names)
        assert any(name.endswith("face_landmarker.task") for name in names)
        # v2.11 numpy-2 guard: the project-wide pip constraints file MUST ship —
        # the launchers pass it via `-c` and dependency_health_check reads it
        # during in-app repair. Without it in the zip, a fresh install can pull
        # numpy 2.x and break Face Crop (the v2.10 bug this release fixes).
        assert any(name.endswith("constraints.txt") for name in names), (
            "constraints.txt missing from release zip — numpy<2 cap would not ship"
        )
        similarity_gui_name = next(name for name in names if name.endswith("similarity/run_gui.bat"))
        similarity_gui = zf.read(similarity_gui_name).decode("utf-8")
        assert "set PYTHON_BIN=py -3.12" in similarity_gui
        assert ".venv\\Scripts\\python.exe" in similarity_gui
        similarity_cli_name = next(name for name in names if name.endswith("similarity/run_cli.bat"))
        similarity_cli = zf.read(similarity_cli_name).decode("utf-8")
        assert "set PYTHON_BIN=py -3.12" in similarity_cli
        oldcam_launcher_name = next(name for name in names if name.endswith("oldcam-v8/oldcam_launcher.bat"))
        oldcam_launcher = zf.read(oldcam_launcher_name).decode("utf-8")
        assert "set PYTHON_CMD=py -3.12" in oldcam_launcher
        assert not any(name.endswith("tests/test_x.py") for name in names)
        assert not any(name.endswith("kling_gui.log") for name in names)
        assert not any(name.endswith("distribution/build_release.py") for name in names)
        assert not any("/.launcher_state/" in name for name in names)
        assert not any("/.recovery/" in name for name in names)
        assert not any("/.tmp_pytest/" in name for name in names)
        assert not any("/venv/" in name for name in names)
        assert not any("/.venv/" in name for name in names)
        gui_launcher_name = next(name for name in names if name.endswith("Start GUI.command"))
        gui_launcher = zf.read(gui_launcher_name).decode("utf-8")
        assert "if [[ -f ./run_gui.command ]]; then" in gui_launcher
        assert "exec /bin/bash ./run_gui.command" in gui_launcher
        assert "exec /bin/bash ./run_gui.sh" in gui_launcher
        windows_gui_launcher_name = next(name for name in names if name.endswith("Start GUI.bat"))
        windows_gui_launcher = zf.read(windows_gui_launcher_name).decode("utf-8")
        assert "call launchers\\windows\\run_gui.bat" in windows_gui_launcher
        cli_launcher_name = next(name for name in names if name.endswith("Start CLI.command"))
        cli_launcher = zf.read(cli_launcher_name).decode("utf-8")
        assert "if [[ -f ./run_cli.command ]]; then" in cli_launcher
        assert "exec /bin/bash ./run_cli.command" in cli_launcher
        assert "exec /bin/bash ./run_cli.sh" in cli_launcher
        cfg_name = next(name for name in names if name.endswith("kling_config.json"))
        cfg = json.loads(zf.read(cfg_name).decode("utf-8"))
        for key in ("falai_api_key", "bfl_api_key", "openrouter_api_key", "freeimage_api_key"):
            assert cfg[key] == ""
        # window_geometry is NO LONGER blanked (user 2026-05-19: ship
        # the dev's window sizing too — everything except API keys).
        for key in ("output_folder", "automation_root_folder", "selfie_output_folder"):
            assert cfg[key] == ""
        assert cfg.get("window_geometry") == "100x100+0+0"  # preserved
        for key in EXPECTED_PROMPT_KEYS:
            assert key in cfg
        # saved_prompts["1"] is FORCED from the template slot 1 (the new
        # minimal-motion default ships even if the dev's live slot was
        # stale); current_model + lock_end_frame are forced project
        # defaults too.
        assert cfg["saved_prompts"]["1"] == "kling prompt"
        assert cfg["current_model"] == "fal-ai/kling-video/v2.5-turbo/pro/image-to-video"
        assert cfg["lock_end_frame"] is True
        assert cfg["automation_selfie_prompts"]["1"] == "selfie auto prompt"
        assert cfg["selfie_prompt_template"] == "selfie template"
        assert cfg["outpaint_prompt"] == "outpaint it"
        assert cfg["face_crop_polish_prompt"] == "polish it"
        assert cfg["openrouter_vision_system_prompt"] == "vision system"
        assert any(name.endswith("run_cli.command") for name in names)
        readme_name = next(name for name in names if name.endswith("README_FIRST_RUN.txt"))
        readme_text = zf.read(readme_name).decode("utf-8")
        assert 'Windows: double-click "Start GUI.bat" or "Start CLI.bat"' in readme_text
        assert 'macOS: double-click "Start GUI.command" or "Start CLI.command"' in readme_text
        assert "right-click -> Open once" in readme_text
        assert "All prompts are stored in kling_config.json" in readme_text

    with zipfile.ZipFile(latest_zip) as zf_latest:
        with zipfile.ZipFile(versioned_zip) as zf_versioned:
            assert zf_latest.namelist() == zf_versioned.namelist()

    staging_cfg = dist / "_staging" / "universal" / "selfie-gen-ultimate" / "kling_config.json"
    assert not staging_cfg.exists()


def test_cli_startup_requires_fal_and_bfl_for_default_automation():
    ui = KlingAutomationUI.__new__(KlingAutomationUI)
    ui.config = {
        "automation_front_expand_enabled": True,
        "automation_front_expand_provider": "bfl",
        "automation_selfie_expand_enabled": True,
        "automation_selfie_expand_provider": "bfl",
        "automation_selfie_enabled": True,
        "automation_selfie_models": ["fal-ai/nano-banana-2/edit"],
        "outpaint_provider": "fal",
    }
    required_keys = {spec.config_key for spec, _reason in ui._startup_required_key_specs()}
    assert "falai_api_key" in required_keys
    assert "bfl_api_key" in required_keys


def test_cli_startup_requires_only_fal_when_bfl_not_selected():
    ui = KlingAutomationUI.__new__(KlingAutomationUI)
    ui.config = {
        "automation_front_expand_enabled": True,
        "automation_front_expand_provider": "fal",
        "automation_selfie_expand_enabled": True,
        "automation_selfie_expand_provider": "fal",
        "automation_selfie_enabled": True,
        "automation_selfie_models": ["fal-ai/nano-banana-2/edit"],
        "outpaint_provider": "fal",
    }
    required_keys = {spec.config_key for spec, _reason in ui._startup_required_key_specs()}
    assert "falai_api_key" in required_keys
    assert "bfl_api_key" not in required_keys


def test_release_prep_force_overrides_blank_phase_g_prompts(tmp_path: Path):
    """Subagent HIGH on 286613c (R5, 2026-05-22): a dev
    kling_config.json carrying empty-string Phase G per-section
    prompts (face_crop_expand_prompt, selfie_expand_prompt,
    outpaint_tab_prompt) would ship a bundle with blank expand
    prompts, because the prior setdefault merge doesn't overwrite
    existing "" values. R5 fix: replace blank/whitespace-only
    values with the template text after the merge, so the bundle
    always ships populated Phase G prompts."""
    template = tmp_path / "default_config_template.json"
    template.write_text(
        json.dumps(
            {
                "face_crop_expand_prompt": "TEMPLATE face crop bg text",
                "selfie_expand_prompt": "TEMPLATE selfie expand bg text",
                "outpaint_tab_prompt": "TEMPLATE outpaint tab bg text",
                "outpaint_prompt": "TEMPLATE legacy shared bg text",
            }
        ),
        encoding="utf-8",
    )
    live = tmp_path / "kling_config.json"
    live.write_text(
        json.dumps(
            {
                # Dev cleared all three Phase G keys to ""; without the
                # R5 fix these blanks would survive into the bundle.
                "face_crop_expand_prompt": "",
                "selfie_expand_prompt": "   ",  # whitespace also treated as blank
                "outpaint_tab_prompt": "",
                # Legacy outpaint_prompt set explicitly — must survive
                # because it's not in the Phase G override list.
                "outpaint_prompt": "DEV custom legacy",
            }
        ),
        encoding="utf-8",
    )
    cfg = build_sanitized_config(template, live)
    assert cfg["face_crop_expand_prompt"] == "TEMPLATE face crop bg text"
    assert cfg["selfie_expand_prompt"] == "TEMPLATE selfie expand bg text"
    assert cfg["outpaint_tab_prompt"] == "TEMPLATE outpaint tab bg text"
    # Legacy outpaint_prompt is NOT in the Phase G force-override
    # list — a dev's custom legacy prompt must survive into the bundle.
    assert cfg["outpaint_prompt"] == "DEV custom legacy"


def test_release_prep_preserves_non_blank_phase_g_prompts(tmp_path: Path):
    """Companion to the above: when a dev has SET intentional
    non-blank Phase G prompts in their kling_config.json, those
    values must survive into the bundle. The R5 fix MUST only
    replace blanks, not overwrite intentional dev customisation."""
    template = tmp_path / "default_config_template.json"
    template.write_text(
        json.dumps(
            {
                "face_crop_expand_prompt": "TEMPLATE face crop",
                "selfie_expand_prompt": "TEMPLATE selfie expand",
                "outpaint_tab_prompt": "TEMPLATE outpaint tab",
            }
        ),
        encoding="utf-8",
    )
    live = tmp_path / "kling_config.json"
    live.write_text(
        json.dumps(
            {
                "face_crop_expand_prompt": "dev's custom face crop",
                "selfie_expand_prompt": "dev's custom selfie expand",
                "outpaint_tab_prompt": "dev's custom outpaint tab",
            }
        ),
        encoding="utf-8",
    )
    cfg = build_sanitized_config(template, live)
    assert cfg["face_crop_expand_prompt"] == "dev's custom face crop"
    assert cfg["selfie_expand_prompt"] == "dev's custom selfie expand"
    assert cfg["outpaint_tab_prompt"] == "dev's custom outpaint tab"
