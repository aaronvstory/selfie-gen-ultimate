from __future__ import annotations

import builtins
import importlib
import json
import os
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch


class _DeepFaceStub:
    @staticmethod
    def build_model(model_name: str):
        return model_name

    @staticmethod
    def extract_faces(**kwargs):
        return [{"face": "face", "facial_area": {"w": 1, "h": 1}}]

    @staticmethod
    def represent(**kwargs):
        return [{"embedding": [1.0, 0.0, 0.0]}]


def _load_cli_module_with_stub():
    deepface_module = types.ModuleType("deepface")
    deepface_module.DeepFace = _DeepFaceStub
    engine_module = types.ModuleType("src.engine")

    class _FaceEngineStub:
        def compare_images(self, *args, **kwargs):
            return {"match": True, "score": 100.0, "error": None}

        def extract_face(self, *args, **kwargs):
            return 0.99

        def initialize_models(self):
            return None

    engine_module.FaceEngine = _FaceEngineStub

    with patch.dict(sys.modules, {"deepface": deepface_module, "src.engine": engine_module}, clear=False):
        sys.modules.pop("src.cli", None)
        cli_module = importlib.import_module("src.cli")
        return importlib.reload(cli_module)


class TestProCLI(unittest.TestCase):
    def setUp(self) -> None:
        self.cli_module = _load_cli_module_with_stub()
        self.addCleanup(lambda: sys.modules.pop("src.cli", None))
        self.addCleanup(lambda: sys.modules.pop("src.engine", None))
        with patch.object(self.cli_module.console, "print"):
            self.cli = self.cli_module.ProCLI()

    def test_import_does_not_require_tkinter(self) -> None:
        original_import = builtins.__import__

        def guarded_import(name, global_vars=None, local_vars=None, fromlist=(), level=0):
            if name == "tkinter":
                raise AssertionError("tkinter should not be imported at module import time")
            return original_import(name, global_vars, local_vars, fromlist, level)

        with patch("builtins.__import__", side_effect=guarded_import):
            reloaded = _load_cli_module_with_stub()

        self.assertTrue(hasattr(reloaded, "ProCLI"))

    def test_load_config_normalizes_invalid_existing_file_mode(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = Path(tmpdir) / "config.json"
            config_path.write_text(
                json.dumps(
                    {
                        "img1_keyword": "alpha",
                        "existing_file_mode": "bogus",
                    }
                ),
                encoding="utf-8",
            )

            self.cli.config_path = str(config_path)
            self.cli.config = {
                "img1_keyword": "extracted",
                "img2_keyword": "selfie",
                "extraction_keyword": "front",
                "padding_ratio": 0.175,
                "existing_file_mode": "index",
            }

            with patch.object(self.cli_module.console, "print") as mock_print:
                self.cli.load_config()

        self.assertEqual(self.cli.config["img1_keyword"], "alpha")
        self.assertEqual(self.cli.config["existing_file_mode"], "index")
        self.assertTrue(any("existing_file_mode" in str(call.args[0]) for call in mock_print.call_args_list))

    def test_apply_runtime_config_rejects_invalid_padding(self) -> None:
        with self.assertRaisesRegex(ValueError, "padding_ratio"):
            self.cli.apply_runtime_config(padding_ratio=2.0)

    def test_apply_runtime_config_anti_spoofing_true_sets_engine_flag(self) -> None:
        self.cli.engine.anti_spoofing = False
        self.cli.apply_runtime_config(anti_spoofing=True)
        self.assertTrue(self.cli.engine.anti_spoofing)
        self.assertTrue(self.cli.config["anti_spoofing"])

    def test_apply_runtime_config_anti_spoofing_false_sets_engine_flag(self) -> None:
        self.cli.engine.anti_spoofing = True
        self.cli.apply_runtime_config(anti_spoofing=False)
        self.assertFalse(self.cli.engine.anti_spoofing)
        self.assertFalse(self.cli.config["anti_spoofing"])

    def test_apply_runtime_config_anti_spoofing_none_leaves_engine_unchanged(self) -> None:
        self.cli.engine.anti_spoofing = True
        self.cli.apply_runtime_config(anti_spoofing=None)
        self.assertTrue(self.cli.engine.anti_spoofing)
        self.assertNotIn("anti_spoofing", self.cli.config)

    def test_validate_padding_ratio_accepts_bounds(self) -> None:
        self.assertEqual(self.cli._validate_padding_ratio(0.0), 0.0)
        self.assertEqual(self.cli._validate_padding_ratio(1.0), 1.0)

    def test_apply_runtime_config_rejects_invalid_existing_file_mode(self) -> None:
        with self.assertRaisesRegex(ValueError, "existing_file_mode"):
            self.cli.apply_runtime_config(existing_file_mode="bogus")

    def test_get_available_path_forces_index_when_source_is_already_extracted(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            source = Path(tmpdir) / "extracted.png"
            source.write_bytes(b"fake")

            self.cli.config["existing_file_mode"] = "skip"
            next_path = self.cli._get_available_path(tmpdir, source.name)

        self.assertEqual(Path(next_path).name, "extracted2.png")

    def test_batch_similarity_processes_deepest_folders_first(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            parent = root / "parent"
            child = parent / "child"
            child.mkdir(parents=True)

            rename_order: list[str] = []

            def walk_stub(_root):
                yield (str(root), ["parent"], [])
                yield (str(parent), ["child"], [])
                yield (str(child), [], [])

            def find_stub(dirpath: str, keyword: str) -> str | None:
                if dirpath in {str(parent), str(child)}:
                    return os.path.join(dirpath, f"{keyword}.jpg")
                return None

            class _ProgressStub:
                def __enter__(self):
                    return self

                def __exit__(self, exc_type, exc, tb):
                    return False

                def add_task(self, *_args, **_kwargs):
                    return 1

                def update(self, *_args, **_kwargs):
                    return None

                def advance(self, *_args, **_kwargs):
                    return None

            with patch.object(self.cli, "_ensure_models_initialized"), \
                patch.object(self.cli, "_log_to_manifest"), \
                patch.object(self.cli, "_find_image_with_keyword", side_effect=find_stub), \
                patch.object(self.cli.engine, "compare_images", return_value={"match": True, "score": 91.0, "error": None}), \
                patch.object(self.cli_module.os, "walk", side_effect=walk_stub), \
                patch.object(self.cli_module.os, "rename", side_effect=lambda src, dst: rename_order.append(src)), \
                patch.object(self.cli_module, "Progress", return_value=_ProgressStub()):
                self.cli.run_batch_similarity(root_dir=str(root), confirm=False)

        self.assertEqual(rename_order, [str(child), str(parent)])

    def test_find_image_with_keyword_matches_fuzzy_front_variant(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            folder = Path(tmpdir)
            (folder / "fronnt_scan.png").write_bytes(b"fake")

            matched = self.cli._find_image_with_keyword(str(folder), "front")

        self.assertIsNotNone(matched)
        self.assertEqual(Path(matched).name, "fronnt_scan.png")

    def test_find_image_with_keyword_matches_fuzzy_extracted_variant(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            folder = Path(tmpdir)
            (folder / "extrcted-selfie.png").write_bytes(b"fake")

            matched = self.cli._find_image_with_keyword(str(folder), "extracted")

        self.assertIsNotNone(matched)
        self.assertEqual(Path(matched).name, "extrcted-selfie.png")

    def test_find_image_with_keyword_handles_invalid_regex(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            folder = Path(tmpdir)
            (folder / "front.png").write_bytes(b"fake")

            matched = self.cli._find_image_with_keyword(str(folder), "[")

        self.assertIsNone(matched)

    def test_get_new_folder_name_replaces_existing_percent_score(self) -> None:
        old_folder = str(Path("C:/tmp") / "DASHER 100% - Jane Doe (123)_front")

        new_folder = self.cli._get_new_folder_name(old_folder, 88.9)

        self.assertEqual(Path(new_folder).name, "DASHER 88 - Jane Doe (123)_front")

    def test_sanitize_folder_name_strips_windows_illegal_chars(self) -> None:
        sanitize = self.cli_module._sanitize_folder_name
        self.assertEqual(sanitize('John: Doe <test>'), "John- Doe _test_")
        self.assertEqual(sanitize('a"b|c?d*e'), "a_b_c_d_e")
        # backslash + forward slash (path separators on Windows) -> '_'
        self.assertEqual(sanitize("Jane\\Doe"), "Jane_Doe")
        self.assertEqual(sanitize("Jane/Doe"), "Jane_Doe")
        # control chars (\r \n \t) dropped, trailing space/dot trimmed
        self.assertEqual(sanitize("line\r\nbreak  "), "linebreak")
        self.assertEqual(sanitize("trailing.dots..."), "trailing.dots")
        # empty / all-trimmed input falls back to the default token
        self.assertEqual(sanitize("   "), "untitled")
        self.assertEqual(sanitize(""), "untitled")
        # unicode that Windows allows is preserved
        self.assertEqual(sanitize("café señor"), "café señor")
        # Windows reserved DOS device names get an underscore prefix (case-insensitive,
        # with or without extension); ordinary names containing them are untouched.
        self.assertEqual(sanitize("CON"), "_CON")
        self.assertEqual(sanitize("nul.txt"), "_nul.txt")
        self.assertEqual(sanitize("COM1"), "_COM1")
        self.assertEqual(sanitize("CONSOLE"), "CONSOLE")

    def test_get_new_folder_name_sanitizes_illegal_chars(self) -> None:
        old_folder = str(Path("/tmp") / "Bad: Name <x>  ")

        new_folder = self.cli._get_new_folder_name(old_folder, 92.0)

        name = Path(new_folder).name
        self.assertNotIn(":", name)
        self.assertFalse(any(c in name for c in '<>"|?*'))
        self.assertFalse(name.endswith((" ", ".")))
        self.assertEqual(name, "Bad- Name _x_ 92")

    def test_get_new_folder_name_is_idempotent_after_sanitize(self) -> None:
        old_folder = str(Path("/tmp") / "Bad: Name <x>  ")
        once = self.cli._get_new_folder_name(old_folder, 92.0)
        twice = self.cli._get_new_folder_name(once, 92.0)
        self.assertEqual(Path(once).name, Path(twice).name)

    def test_get_new_folder_name_spaced_colon_is_idempotent(self) -> None:
        # A colon with surrounding spaces ("Foo : Bar") maps to " - "; sanitizing
        # before the split keeps the rename stable and avoids duplicating the score.
        old_folder = str(Path("/tmp") / "Foo : Bar")
        once = self.cli._get_new_folder_name(old_folder, 88.0)
        twice = self.cli._get_new_folder_name(once, 88.0)
        self.assertEqual(Path(once).name, Path(twice).name)
        # score token appears exactly once (no "88 ... 88" duplication)
        self.assertEqual(Path(once).name.count("88"), 1)
        self.assertNotIn(":", Path(once).name)

    def test_run_dispatches_top_level_sections(self) -> None:
        section_to_handler = {
            "1": "_run_similarity_menu",
            "2": "_run_extraction_menu",
            "3": "_run_settings",
        }

        for section, handler_name in section_to_handler.items():
            with self.subTest(section=section):
                with patch.object(self.cli_module.Prompt, "ask", side_effect=[section, "4"]), \
                    patch.object(self.cli, "_display_current_settings"), \
                    patch.object(self.cli_module.console, "print"), \
                    patch.object(self.cli, handler_name) as handler:
                    self.cli.run()
                handler.assert_called_once_with()

    def test_similarity_menu_dispatches_each_handler(self) -> None:
        option_to_handler = {
            "1": "_run_single_comparison",
            "2": "_run_batch_processing",
        }

        for option, handler_name in option_to_handler.items():
            with self.subTest(option=option):
                with patch.object(self.cli_module.Prompt, "ask", side_effect=[option, "3"]), \
                    patch.object(self.cli_module.console, "print"), \
                    patch.object(self.cli, handler_name) as handler:
                    self.cli._run_similarity_menu()
                handler.assert_called_once_with()

    def test_extraction_menu_dispatches_each_handler(self) -> None:
        option_to_handler = {
            "1": "_run_single_extraction",
            "2": "_run_batch_extraction",
        }

        for option, handler_name in option_to_handler.items():
            with self.subTest(option=option):
                with patch.object(self.cli_module.Prompt, "ask", side_effect=[option, "3"]), \
                    patch.object(self.cli_module.console, "print"), \
                    patch.object(self.cli, handler_name) as handler:
                    self.cli._run_extraction_menu()
                handler.assert_called_once_with()

    def test_similarity_menu_back_returns_without_running_handlers(self) -> None:
        with patch.object(self.cli_module.Prompt, "ask", return_value="3"), \
            patch.object(self.cli_module.console, "print"), \
            patch.object(self.cli, "_run_single_comparison") as single, \
            patch.object(self.cli, "_run_batch_processing") as batch:
            self.cli._run_similarity_menu()
        single.assert_not_called()
        batch.assert_not_called()

    def test_extraction_menu_back_returns_without_running_handlers(self) -> None:
        with patch.object(self.cli_module.Prompt, "ask", return_value="3"), \
            patch.object(self.cli_module.console, "print"), \
            patch.object(self.cli, "_run_single_extraction") as single, \
            patch.object(self.cli, "_run_batch_extraction") as batch:
            self.cli._run_extraction_menu()
        single.assert_not_called()
        batch.assert_not_called()

    def test_run_exit_prints_exit_message(self) -> None:
        with patch.object(self.cli_module.Prompt, "ask", return_value="4"), \
            patch.object(self.cli, "_display_current_settings"), \
            patch.object(self.cli_module.console, "print") as mock_print:
            self.cli.run()
        self.assertTrue(any("[green]Exiting...[/green]" in str(call.args[0]) for call in mock_print.call_args_list if call.args))

    def test_run_main_menu_renders_expected_options(self) -> None:
        with patch.object(self.cli_module.Prompt, "ask", side_effect=["4"]), \
            patch.object(self.cli, "_display_current_settings"), \
            patch.object(self.cli_module.console, "print") as mock_print:
            self.cli.run()

        printed_lines = " ".join(str(call.args[0]) for call in mock_print.call_args_list if call.args)
        self.assertIn("Workflow Sections", printed_lines)
        self.assertIn("Tip: Choose 1 or 2 to access specific workflow submenus.", printed_lines)
        self.assertIn("1. Similarity", printed_lines)
        self.assertIn("2. Extraction", printed_lines)
        self.assertIn("3. Settings", printed_lines)
        self.assertIn("4. Exit", printed_lines)


if __name__ == "__main__":
    unittest.main()
