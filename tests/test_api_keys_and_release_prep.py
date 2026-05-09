import json
import zipfile
from pathlib import Path

from api_keys import API_KEY_SPECS, ensure_key_fields, required_missing_specs
from distribution.release_prep import build_sanitized_config, bundle_release, copy_sanitized_tree
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


def test_required_missing_specs_flags_falai_only():
    config = {"falai_api_key": "", "bfl_api_key": "x", "openrouter_api_key": "x", "freeimage_api_key": "x"}
    missing = required_missing_specs(config)
    assert len(missing) == 1
    assert missing[0].config_key == "falai_api_key"


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
    assert sanitized["selfie_prompt_template"] == "keep me"
    assert sanitized["outpaint_prompt"] == "expand bg"
    assert sanitized["saved_prompts"]["1"] == "prompt one"


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
    (src / "map-codebase-session-abc.md").write_text("private", encoding="utf-8")
    (src / "session-ses_123.md").write_text("private", encoding="utf-8")
    (src / "normal.py").write_text("ok", encoding="utf-8")

    copy_sanitized_tree(src, dst)

    assert not (dst / "tests").exists()
    assert not (dst / "reviews").exists()
    assert not (dst / "map-codebase-session-abc.md").exists()
    assert not (dst / "session-ses_123.md").exists()
    assert (dst / "normal.py").exists()


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
                "automation_selfie_prompt_mode": "existing_config",
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
    (repo / "launchers").mkdir()
    (repo / "launchers" / "run_gui.bat").write_text("@echo off\r\necho gui\r\n", encoding="utf-8")
    (repo / "launchers" / "run_cli.bat").write_text("@echo off\r\necho cli\r\n", encoding="utf-8")
    (repo / "kling_gui.log").write_text("private", encoding="utf-8")
    (repo / "kling_config.json").write_text("private", encoding="utf-8")
    (repo / "tests").mkdir()
    (repo / "tests" / "test_x.py").write_text("x=1", encoding="utf-8")

    created = list(bundle_release(repo, dist))
    names = sorted(path.name for path in created)
    assert names == ["SelfieGenUltimate.zip"]

    universal_zip = dist / "SelfieGenUltimate.zip"
    assert universal_zip.exists()

    with zipfile.ZipFile(universal_zip) as zf:
        names = zf.namelist()
        assert any(name.endswith("selfie-gen-ultimate/Start GUI.bat") for name in names)
        assert any(name.endswith("selfie-gen-ultimate/Start CLI.bat") for name in names)
        assert any(name.endswith("selfie-gen-ultimate/Start GUI.command") for name in names)
        assert any(name.endswith("selfie-gen-ultimate/Start CLI.command") for name in names)
        assert not any(name.endswith("selfie-gen-ultimate/tests/test_x.py") for name in names)
        assert not any(name.endswith("selfie-gen-ultimate/kling_gui.log") for name in names)
        assert not any(name.endswith("selfie-gen-ultimate/distribution/build_release.py") for name in names)
        cfg_name = next(name for name in names if name.endswith("selfie-gen-ultimate/kling_config.json"))
        cfg = json.loads(zf.read(cfg_name).decode("utf-8"))
        for key in ("falai_api_key", "bfl_api_key", "openrouter_api_key", "freeimage_api_key"):
            assert cfg[key] == ""
        for key in ("output_folder", "automation_root_folder", "selfie_output_folder", "window_geometry"):
            assert cfg[key] == ""
        for key in EXPECTED_PROMPT_KEYS:
            assert key in cfg
        assert cfg["saved_prompts"]["1"] == "kling prompt"
        assert cfg["automation_selfie_prompts"]["1"] == "selfie auto prompt"
        assert cfg["selfie_prompt_template"] == "selfie template"
        assert cfg["outpaint_prompt"] == "outpaint it"
        assert cfg["face_crop_polish_prompt"] == "polish it"
        assert cfg["openrouter_vision_system_prompt"] == "vision system"
        assert any(name.endswith("selfie-gen-ultimate/run_cli.command") for name in names)
        readme_name = next(name for name in names if name.endswith("selfie-gen-ultimate/README_FIRST_RUN.txt"))
        readme_text = zf.read(readme_name).decode("utf-8")
        assert 'Windows: double-click "Start GUI.bat" or "Start CLI.bat"' in readme_text
        assert 'macOS: double-click "Start GUI.command" or "Start CLI.command"' in readme_text
        assert "right-click -> Open once" in readme_text
        assert "All prompts are stored in kling_config.json" in readme_text

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
