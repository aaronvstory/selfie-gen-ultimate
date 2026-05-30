"""Static-text guards: release version is surfaced in the CLI + every launcher.

User mandate (2026-05-30): the release version (``app_version.RELEASE_VERSION``)
must be visible next to the "Ultimate-Selfie-Gen" branding in the GUI **and**
the CLI, and the launchers should print it too. The GUI chip is covered by the
GUI; this file guards the CLI banner and the four shell/batch launchers so a
future edit can't silently drop the version display on one surface.

Single source of truth: ``app_version.RELEASE_VERSION``. None of these surfaces
may hardcode the literal version string — they must read/parse the constant so
every dist build auto-updates. These are static-text assertions (no subprocess),
mirroring ``test_launcher_arg_forwarding.py``.
"""

from pathlib import Path

import app_version

REPO_ROOT = Path(__file__).resolve().parents[1]


def _read_text(path: str) -> str:
    """Read with universal-newline normalization so CRLF batch files work."""
    return (REPO_ROOT / path).read_text(encoding="utf-8")


def test_app_version_constant_present():
    """The single source of truth exists and looks like a release tag."""
    assert isinstance(app_version.RELEASE_VERSION, str)
    assert app_version.RELEASE_VERSION.strip(), "RELEASE_VERSION is empty"


def test_cli_imports_release_version():
    """kling_automation_ui.py must import the constant (not hardcode a version)."""
    src = _read_text("kling_automation_ui.py")
    assert "from app_version import RELEASE_VERSION" in src, (
        "CLI must import RELEASE_VERSION as the single source of truth."
    )


def test_cli_header_renders_release_version():
    """display_header() must interpolate RELEASE_VERSION into the ASCII title."""
    src = _read_text("kling_automation_ui.py")
    assert 'f"SELFIE GEN ULTIMATE  {RELEASE_VERSION}"' in src, (
        "CLI display_header() dropped the version from the ASCII title."
    )


def test_cli_config_menu_renders_release_version():
    """display_configuration_menu() must interpolate RELEASE_VERSION into its header."""
    src = _read_text("kling_automation_ui.py")
    assert "SELFIE GEN ULTIMATE  {RELEASE_VERSION}" in src, (
        "CLI config menu header dropped the version."
    )


def test_cli_does_not_hardcode_version():
    """The CLI must not embed the literal current version (would drift on rebuild)."""
    src = _read_text("kling_automation_ui.py")
    literal = f'"SELFIE GEN ULTIMATE  {app_version.RELEASE_VERSION}"'
    assert literal not in src, (
        "CLI hardcodes the literal version string; it must read RELEASE_VERSION."
    )


def test_windows_gui_bat_parses_and_prints_version():
    """launchers/windows/run_gui.bat must parse app_version.py and print APP_VER."""
    src = _read_text("launchers/windows/run_gui.bat")
    assert 'findstr /b /c:"RELEASE_VERSION"' in src, (
        "run_gui.bat dropped the app_version.py parse for the banner."
    )
    assert "Ultimate-Selfie-Gen  %APP_VER%" in src, (
        "run_gui.bat banner no longer prints the parsed version."
    )


def test_windows_cli_bat_parses_and_prints_version():
    """launchers/windows/run_cli.bat must parse app_version.py and print APP_VER."""
    src = _read_text("launchers/windows/run_cli.bat")
    assert 'findstr /b /c:"RELEASE_VERSION"' in src, (
        "run_cli.bat dropped the app_version.py parse for the banner."
    )
    assert "Ultimate-Selfie-Gen  %APP_VER%" in src, (
        "run_cli.bat banner no longer prints the parsed version."
    )


def test_macos_gui_sh_parses_and_prints_version():
    """run_gui.sh must sed-parse RELEASE_VERSION and print it in the banner."""
    src = _read_text("run_gui.sh")
    assert "RELEASE_VERSION" in src and "app_version.py" in src, (
        "run_gui.sh dropped the app_version.py parse for the banner."
    )
    assert "Ultimate-Selfie-Gen  %s  --  GUI Launcher" in src, (
        "run_gui.sh banner no longer prints the parsed version."
    )


def test_macos_cli_sh_parses_and_prints_version():
    """run_cli.sh must sed-parse RELEASE_VERSION and print it in the banner."""
    src = _read_text("run_cli.sh")
    assert "RELEASE_VERSION" in src and "app_version.py" in src, (
        "run_cli.sh dropped the app_version.py parse for the banner."
    )
    assert "Ultimate-Selfie-Gen  %s  --  CLI Launcher" in src, (
        "run_cli.sh banner no longer prints the parsed version."
    )


def test_launchers_do_not_hardcode_version():
    """No launcher may hardcode the literal version (must parse it dynamically)."""
    version = app_version.RELEASE_VERSION
    for path in (
        "launchers/windows/run_gui.bat",
        "launchers/windows/run_cli.bat",
        "run_gui.sh",
        "run_cli.sh",
    ):
        src = _read_text(path)
        assert f"Ultimate-Selfie-Gen  {version}" not in src, (
            f"{path} hardcodes the literal version {version!r}; "
            "it must parse app_version.py so rebuilds auto-update."
        )
