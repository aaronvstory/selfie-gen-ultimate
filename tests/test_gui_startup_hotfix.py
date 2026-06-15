import builtins
import importlib
import io
import os
import sys
import tempfile
import unittest
from unittest import mock


class GuiStartupHotfixTests(unittest.TestCase):
    def test_ml_backend_env_forces_deterministic_values(self):
        module = importlib.import_module("kling_gui.ml_backend_env")
        with mock.patch.dict(os.environ, {}, clear=True):
            module.ensure_ml_backend_env()
            self.assertEqual(os.environ.get("TF_USE_LEGACY_KERAS"), "1")
            self.assertEqual(os.environ.get("KERAS_BACKEND"), "tensorflow")

        with mock.patch.dict(
            os.environ,
            {"TF_USE_LEGACY_KERAS": "0", "KERAS_BACKEND": "jax"},
            clear=True,
        ):
            module.ensure_ml_backend_env()
            self.assertEqual(os.environ.get("TF_USE_LEGACY_KERAS"), "1")
            self.assertEqual(os.environ.get("KERAS_BACKEND"), "tensorflow")

    def test_main_window_import_survives_retinaface_init_exception(self):
        real_import = builtins.__import__

        def fake_import(name, globals=None, locals=None, fromlist=(), level=0):
            if name == "retinaface":
                raise AttributeError("module tensorflow has no attribute __version__")
            return real_import(name, globals, locals, fromlist, level)

        with mock.patch("builtins.__import__", side_effect=fake_import):
            for module_name in [
                "kling_gui.main_window",
                "kling_gui.tabs",
                "kling_gui.tabs.face_crop_tab",
            ]:
                sys.modules.pop(module_name, None)

            module = importlib.import_module("kling_gui.main_window")
            self.assertTrue(hasattr(module, "KlingGUIWindow"))

    def test_face_crop_runtime_loader_catches_non_importerror(self):
        module = importlib.import_module("kling_gui.tabs.face_crop_tab")
        module._RETINAFACE_CLASS = None

        real_import = builtins.__import__

        def fake_import(name, globals=None, locals=None, fromlist=(), level=0):
            if name == "retinaface":
                raise RuntimeError("tensorflow runtime broken")
            return real_import(name, globals, locals, fromlist, level)

        with mock.patch.object(module, "HAS_FACE_DEPS", True), \
            mock.patch.object(module, "FACE_DEPS_ERROR", ""), \
            mock.patch("builtins.__import__", side_effect=fake_import):
            retinaface_cls, retinaface_error = module._load_retinaface()

        self.assertIsNone(retinaface_cls)
        self.assertIn("RuntimeError", retinaface_error)

    def test_face_crop_loader_bootstraps_ml_env_before_import(self):
        module = importlib.import_module("kling_gui.tabs.face_crop_tab")
        module._RETINAFACE_CLASS = None

        real_import = builtins.__import__

        def fake_import(name, globals=None, locals=None, fromlist=(), level=0):
            if name == "retinaface":
                raise RuntimeError("tensorflow runtime broken")
            return real_import(name, globals, locals, fromlist, level)

        with mock.patch.object(module, "HAS_FACE_DEPS", True), \
            mock.patch.object(module, "FACE_DEPS_ERROR", ""), \
            mock.patch.object(module, "ensure_ml_backend_env") as ensure_mock, \
            mock.patch("builtins.__import__", side_effect=fake_import):
            retinaface_cls, retinaface_error = module._load_retinaface()

        self.assertIsNone(retinaface_cls)
        self.assertIn("RuntimeError", retinaface_error)
        ensure_mock.assert_called_once_with()


class GuiLauncherBatchModeTests(unittest.TestCase):
    def test_launcher_bootstraps_ml_env_before_dependency_check(self):
        module = importlib.import_module("gui_launcher")
        call_order = []

        with mock.patch.object(module, "ensure_ml_backend_env", side_effect=lambda: call_order.append("env")), \
            mock.patch.object(module, "_run_dependency_bootstrap", side_effect=lambda: call_order.append("deps")), \
            mock.patch.object(module, "_load_gui_window", return_value=(None, "ImportError: test", "tb")), \
            mock.patch.object(module, "show_critical_error"), \
            mock.patch.object(module, "PATH_UTILS_AVAILABLE", False):
            with self.assertRaises(SystemExit):
                module.main()

        self.assertEqual(call_order[:2], ["env", "deps"])

    def test_batch_mode_import_failure_is_console_only(self):
        module = importlib.import_module("gui_launcher")

        with tempfile.TemporaryDirectory() as tmpdir:
            stderr_buffer = io.StringIO()
            with mock.patch.object(module, "CLI_ERROR_MODE", True), \
                mock.patch.object(module, "PATH_UTILS_AVAILABLE", False), \
                mock.patch.object(module, "_app_dir", tmpdir), \
                mock.patch.object(
                    module,
                    "_load_gui_window",
                    return_value=(None, "AttributeError: broken tensorflow", "traceback text"),
                ), \
                mock.patch.object(module, "show_critical_error") as mocked_popup, \
                mock.patch("sys.stderr", stderr_buffer):
                with self.assertRaises(SystemExit) as exit_ctx:
                    module.main()

            self.assertEqual(exit_ctx.exception.code, 1)
            mocked_popup.assert_not_called()

            stderr_text = stderr_buffer.getvalue()
            self.assertIn("Import Error:", stderr_text)
            self.assertIn("AttributeError: broken tensorflow", stderr_text)

            crash_log = os.path.join(tmpdir, "crash_log.txt")
            self.assertTrue(os.path.exists(crash_log))
            with open(crash_log, "r", encoding="utf-8") as handle:
                content = handle.read()
            self.assertIn("Kling UI Initialization Failure", content)


class GuiStartupKeyPromptTests(unittest.TestCase):
    """First-launch onboarding is PURELY INFORMATIONAL (Codex P2, PR #73 +
    user direction 2026-06-04 "nothing is required at startup"). The old
    blocking ``simpledialog.askstring`` per-missing-key loop was removed — it
    contradicted the "nothing required" contract by nagging for a Fal.ai key it
    then called optional. These tests lock the new contract: show the info box,
    NEVER prompt, NEVER save, never close the app."""

    def test_first_launch_is_informational_only_no_prompt(self):
        module = importlib.import_module("kling_gui.main_window")
        window = module.KlingGUIWindow.__new__(module.KlingGUIWindow)
        window.config = {"falai_api_key": "", "bfl_api_key": ""}
        window.root = object()
        logs = []
        saved = {"count": 0}
        window._log = lambda message, level="info": logs.append((message, level))
        window._save_config = lambda: saved.__setitem__("count", saved["count"] + 1)
        window._update_api_badge = lambda _key: None
        window._on_close = mock.Mock()

        with mock.patch.object(module.messagebox, "showinfo") as info_mock, \
            mock.patch.object(module.simpledialog, "askstring") as ask_mock:
            window._prompt_startup_provider_keys_on_first_run()

        # Informational box shown, but NO blocking key prompt and NO save.
        info_mock.assert_called()
        ask_mock.assert_not_called()
        self.assertEqual(saved["count"], 0)
        window._on_close.assert_not_called()
        # A missing Fal.ai key produces an informational "add via badge" line.
        self.assertTrue(any("badge" in message.lower() for message, _ in logs))

    def test_first_launch_does_not_persist_keys(self):
        module = importlib.import_module("kling_gui.main_window")
        window = module.KlingGUIWindow.__new__(module.KlingGUIWindow)
        window.config = {"falai_api_key": "", "bfl_api_key": ""}
        window.root = object()
        window._log = lambda *_args, **_kwargs: None
        window._on_close = mock.Mock()
        saved = {"count": 0}
        updated = []
        window._save_config = lambda: saved.__setitem__("count", saved["count"] + 1)
        window._update_api_badge = lambda key: updated.append(key)

        with mock.patch.object(module.messagebox, "showinfo"), \
            mock.patch.object(module.simpledialog, "askstring") as ask_mock:
            window._prompt_startup_provider_keys_on_first_run()

        # No key is written or persisted on first launch (badge-driven entry only).
        self.assertEqual(window.config["falai_api_key"], "")
        self.assertEqual(window.config["bfl_api_key"], "")
        self.assertEqual(saved["count"], 0)
        ask_mock.assert_not_called()
        self.assertEqual(updated, [])
        window._on_close.assert_not_called()

    def test_established_install_never_shows_key_onboarding(self):
        """An EXISTING config file at launch = established install: the startup
        key boxes must NOT fire, even if THIS load came up with empty keys (a
        read racing a concurrent instance's save). Otherwise the GUI nags for
        keys (incl. OpenRouter) the user already has, every such launch."""
        module = importlib.import_module("kling_gui.main_window")
        window = module.KlingGUIWindow.__new__(module.KlingGUIWindow)
        # Empty keys (what a transient failed config read leaves behind)...
        window.config = {"falai_api_key": "", "bfl_api_key": "", "openrouter_api_key": ""}
        window.root = object()
        window._config_existed_at_startup = True  # ...but the install is established
        window._log = lambda *_a, **_k: None
        window._save_config = lambda: None

        with mock.patch.object(module.messagebox, "showinfo") as info_mock, \
            mock.patch.object(module.simpledialog, "askstring") as ask_mock:
            window._prompt_startup_provider_keys_on_first_run()

        # No info box, no optional-keys box, no prompt: the badges are the entry.
        info_mock.assert_not_called()
        ask_mock.assert_not_called()

    def test_genuine_first_run_still_shows_onboarding(self):
        """A true fresh install (no prior config file) still gets the
        informational onboarding box — the gate must not suppress that."""
        module = importlib.import_module("kling_gui.main_window")
        window = module.KlingGUIWindow.__new__(module.KlingGUIWindow)
        window.config = {"falai_api_key": "", "bfl_api_key": ""}
        window.root = object()
        window._config_existed_at_startup = False  # genuine first run
        window._log = lambda *_a, **_k: None
        window._save_config = lambda: None
        window._update_api_badge = lambda _k: None

        with mock.patch.object(module.messagebox, "showinfo") as info_mock, \
            mock.patch.object(module.simpledialog, "askstring"):
            window._prompt_startup_provider_keys_on_first_run()

        info_mock.assert_called()


if __name__ == "__main__":
    unittest.main()
