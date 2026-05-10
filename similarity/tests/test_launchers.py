from __future__ import annotations

import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]


def _read(path: str) -> str:
    return (REPO_ROOT / path).read_text(encoding="utf-8")


class TestLauncherScripts(unittest.TestCase):
    def test_windows_similarity_venv_priority_prefers_root_before_local(self) -> None:
        for script in ("run_gui.bat", "run_cli.bat"):
            with self.subTest(script=script):
                text = _read(script)
                root_idx = text.index('"%REPO_ROOT%\\venv\\Scripts\\python.exe"')
                local_idx = text.index('".venv\\Scripts\\python.exe"')
                self.assertLess(root_idx, local_idx)

    def test_command_similarity_venv_priority_prefers_root_before_local(self) -> None:
        for script in ("run_gui.command", "run_cli.command"):
            with self.subTest(script=script):
                text = _read(script)
                root_idx = text.index('"$REPO_ROOT/venv/bin/python"')
                local_idx = text.index('".venv/bin/python"')
                self.assertLess(root_idx, local_idx)

    def test_similarity_uses_launcher_state_dependency_stamp(self) -> None:
        for script in ("run_gui.bat", "run_cli.bat", "run_gui.command", "run_cli.command"):
            with self.subTest(script=script):
                text = _read(script)
                self.assertIn(".launcher_state", text)
                self.assertIn("requirements", text)
                self.assertIn("STAMP_FILE", text)

    def test_similarity_python_version_guard_exists(self) -> None:
        for script in ("run_gui.bat", "run_cli.bat", "run_gui.command", "run_cli.command"):
            with self.subTest(script=script):
                text = _read(script)
                self.assertIn("3.9", text)
                self.assertTrue("3.13" in text or "3,13" in text or "3, 13" in text)

    def test_similarity_gui_tkinter_guard_exists(self) -> None:
        for script in ("run_gui.bat", "run_gui.command"):
            with self.subTest(script=script):
                text = _read(script)
                self.assertIn("import tkinter", text)

    def test_similarity_preserves_parent_launch_gating(self) -> None:
        bat = _read("run_cli.bat")
        self.assertIn('if "%SIMILARITY_LAUNCHED_BY_MAIN%"=="" (', bat)

        for script in ("run_gui.command", "run_cli.command"):
            text = _read(script)
            self.assertIn("SIMILARITY_LAUNCHED_BY_MAIN", text)
            self.assertIn("tee -a", text)


if __name__ == "__main__":
    unittest.main()
