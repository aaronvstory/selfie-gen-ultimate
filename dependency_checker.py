"""
Dependency Checker - Verify and install all required dependencies for Kling UI.

Checks:
- Python packages (pip)
- External tools (FFmpeg)
- Optional enhancements (tkinterdnd2 for drag-drop)
"""

import subprocess
import sys
import shutil
from typing import Dict, List, Tuple, Optional
from dataclasses import dataclass
import importlib.metadata
from pathlib import Path


@dataclass
class Dependency:
    """Represents a dependency with its status."""
    name: str
    import_name: str  # The actual import name (may differ from package name)
    pip_name: str     # The pip install name
    required: bool    # True = required, False = optional
    description: str
    installed: bool = False
    version: Optional[str] = None
    runtime_issue: Optional[str] = None


# Define all dependencies
PYTHON_DEPENDENCIES = [
    Dependency(
        name="Requests",
        import_name="requests",
        pip_name="requests",
        required=True,
        description="HTTP library for API calls to fal.ai"
    ),
    Dependency(
        name="Pillow (PIL)",
        import_name="PIL",
        pip_name="Pillow",
        required=True,
        description="Image processing library"
    ),
    Dependency(
        name="TkinterDnD2",
        import_name="tkinterdnd2",
        pip_name="tkinterdnd2",
        required=False,
        description="Drag-and-drop support for GUI mode"
    ),
    Dependency(
        name="Selenium",
        import_name="selenium",
        pip_name="selenium",
        required=False,
        description="Browser automation (for balance checker)"
    ),
    Dependency(
        name="Webdriver Manager",
        import_name="webdriver_manager",
        pip_name="webdriver-manager",
        required=False,
        description="Automatic ChromeDriver management"
    ),
    Dependency(
        name="DeepFace",
        import_name="deepface",
        pip_name="deepface",
        required=False,
        description="Face embedding and verification backend (ArcFace)"
    ),
    Dependency(
        name="TF-Keras",
        import_name="tf_keras",
        pip_name="tf-keras",
        required=False,
        description="TensorFlow compatibility backend required by DeepFace"
    ),
    Dependency(
        name="TensorFlow",
        import_name="tensorflow",
        pip_name="tensorflow",
        required=False,
        description="Core runtime backend for DeepFace/RetinaFace stack"
    ),
    Dependency(
        name="RetinaFace",
        import_name="retinaface",
        pip_name="retina-face",
        required=False,
        description="Face detector used by crop tab and similarity engine"
    ),
    Dependency(
        name="OpenCV",
        import_name="cv2",
        pip_name="opencv-python-headless",
        required=False,
        description="Image loading for InsightFace face analysis"
    ),
    Dependency(
        name="PyTorch",
        import_name="torch",
        pip_name="torch>=2.2,<3",
        required=False,
        description="Required by DeepFace anti-spoofing (MiniFASNetV2). Without it FAS fails."
    ),
]


@dataclass
class ExternalTool:
    """Represents an external tool dependency."""
    name: str
    command: str
    args: List[str]
    required: bool
    description: str
    install_hint: str
    installed: bool = False
    version: Optional[str] = None


EXTERNAL_TOOLS = [
    ExternalTool(
        name="FFmpeg",
        command="ffmpeg",
        args=["-version"],
        required=False,
        description="Video processing for Loop Video feature",
        install_hint="Install via your package manager (macOS: brew install ffmpeg, Windows: winget install FFmpeg) or download from https://ffmpeg.org/download.html"
    ),
]

REPAIRABLE_RUNTIME_IMPORTS = {"tensorflow", "tf_keras", "deepface", "retinaface", "torch"}


class DependencyChecker:
    """Check and install dependencies for Kling UI."""

    # ANSI color codes
    GREEN = "\033[92m"
    RED = "\033[91m"
    YELLOW = "\033[93m"
    CYAN = "\033[96m"
    MAGENTA = "\033[95m"
    WHITE = "\033[97m"
    GRAY = "\033[90m"
    RESET = "\033[0m"

    def __init__(self):
        self.python_deps = [Dependency(**d.__dict__) for d in PYTHON_DEPENDENCIES]
        self.external_tools = [ExternalTool(**t.__dict__) for t in EXTERNAL_TOOLS]
        self.tkinter_available = False
        self.tkinter_error: Optional[str] = None

    def _get_tkinter_install_hint(self) -> str:
        """Return a platform-specific hint for enabling Tk support."""
        if sys.platform == "darwin":
            version = f"{sys.version_info.major}.{sys.version_info.minor}"
            return (
                "GUI mode needs a Python build with Tk support. "
                "For Homebrew Python, install the matching package and recreate the virtual environment: "
                f"brew install python-tk@{version}"
            )
        return "GUI mode needs Python with Tk support enabled."

    def check_tkinter_support(self) -> bool:
        """Check whether the active Python runtime can import tkinter."""
        try:
            import tkinter  # noqa: F401
        except ModuleNotFoundError as exc:
            self.tkinter_available = False
            self.tkinter_error = self._get_tkinter_install_hint() if exc.name == "_tkinter" else str(exc)
            return False
        except Exception as exc:
            self.tkinter_available = False
            self.tkinter_error = str(exc)
            return False

        self.tkinter_available = True
        self.tkinter_error = None
        return True

    @staticmethod
    def _get_installed_package_version(pip_name: str) -> Optional[str]:
        """Return installed metadata version, or None when the package is absent."""
        try:
            return importlib.metadata.version(pip_name)
        except importlib.metadata.PackageNotFoundError:
            return None

    def print_header(self, text: str):
        """Print a header line."""
        print(f"\n{self.MAGENTA}{'═' * 79}{self.RESET}")
        print(f"{self.MAGENTA}  {text}{self.RESET}")
        print(f"{self.MAGENTA}{'═' * 79}{self.RESET}\n")

    def print_section(self, text: str):
        """Print a section header."""
        print(f"\n{self.CYAN}{'─' * 79}{self.RESET}")
        print(f"{self.CYAN}  {text}{self.RESET}")
        print(f"{self.CYAN}{'─' * 79}{self.RESET}\n")

    def check_python_package(self, dep: Dependency) -> bool:
        """Check if a Python package is installed."""
        try:
            module = __import__(dep.import_name)
            dep.installed = True
            dep.runtime_issue = None
            # Try to get version
            try:
                dep.version = getattr(module, '__version__', None)
                if dep.version is None:
                    dep.version = importlib.metadata.version(dep.pip_name)
            except Exception:
                dep.version = "unknown"
            return True
        except ImportError as exc:
            dep.version = self._get_installed_package_version(dep.pip_name)
            dep.installed = dep.version is not None
            dep.runtime_issue = str(exc) if dep.installed else None
            return False
        except Exception as exc:
            dep.version = self._get_installed_package_version(dep.pip_name)
            dep.installed = dep.version is not None
            dep.runtime_issue = str(exc) if dep.installed else None
            return False

    def check_external_tool(self, tool: ExternalTool) -> bool:
        """Check if an external tool is available."""
        try:
            result = subprocess.run(
                [tool.command] + tool.args,
                capture_output=True,
                text=True,
                timeout=10
            )
            if result.returncode == 0:
                tool.installed = True
                # Extract version from first line
                first_line = result.stdout.split('\n')[0] if result.stdout else ""
                tool.version = first_line[:60] if first_line else "installed"
                return True
        except FileNotFoundError:
            tool.installed = False
        except subprocess.TimeoutExpired:
            tool.installed = False
        except Exception:
            tool.installed = False
        return False

    def check_all(self) -> Tuple[int, int, int, int]:
        """
        Check all dependencies.

        Returns:
            (required_ok, required_missing, optional_ok, optional_missing)
        """
        required_ok = 0
        required_missing = 0
        optional_ok = 0
        optional_missing = 0

        self.check_tkinter_support()

        # Check Python packages
        for dep in self.python_deps:
            installed = self.check_python_package(dep)
            if dep.required:
                if installed:
                    required_ok += 1
                else:
                    required_missing += 1
            else:
                if installed:
                    optional_ok += 1
                else:
                    optional_missing += 1

        # Check external tools
        for tool in self.external_tools:
            installed = self.check_external_tool(tool)
            if tool.required:
                if installed:
                    required_ok += 1
                else:
                    required_missing += 1
            else:
                if installed:
                    optional_ok += 1
                else:
                    optional_missing += 1

        return required_ok, required_missing, optional_ok, optional_missing

    def display_status(self):
        """Display the status of all dependencies."""
        self.print_header("DEPENDENCY CHECK")

        self.print_section("Python Runtime")
        if self.tkinter_available:
            print(f"  {self.GREEN}✓{self.RESET} tkinter runtime available {self.GRAY}[GUI]{self.RESET}")
        else:
            print(f"  {self.RED}✗{self.RESET} tkinter runtime unavailable {self.RED}[GUI]{self.RESET}")
            if self.tkinter_error:
                print(f"    {self.YELLOW}{self.tkinter_error}{self.RESET}")
        print()

        # Python packages
        self.print_section("Python Packages")

        for dep in self.python_deps:
            req_label = f"{self.RED}[REQUIRED]{self.RESET}" if dep.required else f"{self.GRAY}[optional]{self.RESET}"

            if dep.installed and not dep.runtime_issue:
                version_str = f" v{dep.version}" if dep.version and dep.version != "unknown" else ""
                print(f"  {self.GREEN}✓{self.RESET} {dep.name}{version_str} {req_label}")
                print(f"    {self.GRAY}{dep.description}{self.RESET}")
            elif dep.installed and dep.runtime_issue:
                version_str = f" v{dep.version}" if dep.version and dep.version != "unknown" else ""
                print(f"  {self.YELLOW}!{self.RESET} {dep.name}{version_str} {req_label}")
                print(f"    {self.GRAY}{dep.description}{self.RESET}")
                print(f"    {self.YELLOW}Installed but failing at runtime: {dep.runtime_issue}{self.RESET}")
                print(f"    {self.YELLOW}Repair: pip install --force-reinstall {dep.pip_name}{self.RESET}")
            else:
                print(f"  {self.RED}✗{self.RESET} {dep.name} {req_label}")
                print(f"    {self.GRAY}{dep.description}{self.RESET}")
                print(f"    {self.YELLOW}Install: pip install {dep.pip_name}{self.RESET}")
            print()

        # External tools
        self.print_section("External Tools")

        for tool in self.external_tools:
            req_label = f"{self.RED}[REQUIRED]{self.RESET}" if tool.required else f"{self.GRAY}[optional]{self.RESET}"

            if tool.installed:
                print(f"  {self.GREEN}✓{self.RESET} {tool.name} {req_label}")
                print(f"    {self.GRAY}{tool.description}{self.RESET}")
                if tool.version:
                    print(f"    {self.GRAY}Version: {tool.version}{self.RESET}")
            else:
                print(f"  {self.RED}✗{self.RESET} {tool.name} {req_label}")
                print(f"    {self.GRAY}{tool.description}{self.RESET}")
                print(f"    {self.YELLOW}{tool.install_hint}{self.RESET}")
            print()

    def display_summary(self, required_ok: int, required_missing: int,
                       optional_ok: int, optional_missing: int):
        """Display summary of dependency check."""
        self.print_section("SUMMARY")

        total_required = required_ok + required_missing
        total_optional = optional_ok + optional_missing

        # Required dependencies
        if required_missing == 0:
            print(f"  {self.GREEN}✓ All required dependencies installed ({required_ok}/{total_required}){self.RESET}")
        else:
            print(f"  {self.RED}✗ Missing required dependencies: {required_missing}/{total_required}{self.RESET}")

        # Optional dependencies
        if optional_missing == 0:
            print(f"  {self.GREEN}✓ All optional dependencies installed ({optional_ok}/{total_optional}){self.RESET}")
        else:
            print(f"  {self.YELLOW}⚠ Missing optional dependencies: {optional_missing}/{total_optional}{self.RESET}")

        print()

        return required_missing == 0

    def get_missing_pip_packages(self) -> List[Dependency]:
        """Get list of missing or runtime-broken pip packages."""
        return [dep for dep in self.python_deps if not dep.installed or dep.runtime_issue]

    def install_pip_package(self, dep: Dependency) -> bool:
        """Install a single pip package."""
        try:
            print(f"  {self.CYAN}Installing {dep.name}...{self.RESET}")
            result = subprocess.run(
                [sys.executable, "-m", "pip", "install", dep.pip_name],
                capture_output=True,
                text=True,
                timeout=120
            )
            if result.returncode == 0:
                print(f"  {self.GREEN}✓ {dep.name} installed successfully{self.RESET}")
                return True
            else:
                print(f"  {self.RED}✗ Failed to install {dep.name}{self.RESET}")
                if result.stderr:
                    print(f"    {self.GRAY}{result.stderr[-200:]}{self.RESET}")
                return False
        except subprocess.TimeoutExpired:
            print(f"  {self.RED}✗ Installation timed out for {dep.name}{self.RESET}")
            return False
        except Exception as e:
            print(f"  {self.RED}✗ Error installing {dep.name}: {e}{self.RESET}")
            return False

    def install_all_missing(self, include_optional: bool = True) -> Tuple[int, int]:
        """
        Install all missing pip packages.

        Args:
            include_optional: Whether to install optional packages too

        Returns:
            (success_count, failure_count)
        """
        missing = self.get_missing_pip_packages()
        if not include_optional:
            missing = [dep for dep in missing if dep.required]

        if not missing:
            print(f"  {self.GREEN}No packages to install!{self.RESET}")
            return 0, 0

        self.print_section(f"Installing {len(missing)} package(s)")

        success = 0
        failed = 0

        for dep in missing:
            if self.install_pip_package(dep):
                success += 1
            else:
                failed += 1
            print()

        return success, failed

    def _brew_is_available(self) -> bool:
        """Return True when Homebrew is available on PATH."""
        return shutil.which("brew") is not None

    @staticmethod
    def _command_available(command: str) -> bool:
        return shutil.which(command) is not None

    def install_external_tool(self, tool: ExternalTool) -> bool:
        """Attempt to install a missing external tool automatically."""
        if tool.name.lower() != "ffmpeg":
            print(f"  {self.YELLOW}Auto-install not implemented for {tool.name}.{self.RESET}")
            return False

        attempted_cmds = []
        if sys.platform == "darwin":
            if not self._brew_is_available():
                print(
                    f"  {self.YELLOW}Homebrew not found; cannot auto-install FFmpeg on macOS.{self.RESET}"
                )
                return False
            install_cmds = [["brew", "install", "ffmpeg"]]
        elif sys.platform == "win32":
            install_cmds = []
            if self._command_available("winget"):
                install_cmds.append(
                    [
                        "winget",
                        "install",
                        "--id",
                        "Gyan.FFmpeg",
                        "--exact",
                        "--silent",
                        "--accept-source-agreements",
                        "--accept-package-agreements",
                    ]
                )
            if self._command_available("choco"):
                install_cmds.append(["choco", "install", "ffmpeg", "-y"])
            if not install_cmds:
                print(
                    f"  {self.YELLOW}Neither winget nor choco was found; cannot auto-install FFmpeg on Windows.{self.RESET}"
                )
                print(
                    f"  {self.YELLOW}Install one package manager, then relaunch to auto-install, or install FFmpeg manually.{self.RESET}"
                )
                return False
        else:
            print(
                f"  {self.YELLOW}Auto-install for FFmpeg is currently supported on macOS and Windows only.{self.RESET}"
            )
            return False

        for cmd in install_cmds:
            attempted_cmds.append(" ".join(cmd))
            try:
                print(f"  {self.CYAN}Installing {tool.name} via {' '.join(cmd)}...{self.RESET}")
                result = subprocess.run(cmd, capture_output=True, text=True, timeout=900)
                if result.returncode == 0:
                    print(f"  {self.GREEN}✓ {tool.name} installed successfully{self.RESET}")
                    return True
                print(f"  {self.RED}✗ Failed via {' '.join(cmd)}{self.RESET}")
                if result.stderr:
                    print(f"    {self.GRAY}{result.stderr[-400:]}{self.RESET}")
            except subprocess.TimeoutExpired:
                print(f"  {self.RED}✗ Installation timed out for {' '.join(cmd)}{self.RESET}")
            except Exception as exc:
                print(f"  {self.RED}✗ Error installing via {' '.join(cmd)}: {exc}{self.RESET}")

        if attempted_cmds:
            print(
                f"  {self.YELLOW}Tried these commands: {', '.join(attempted_cmds)}{self.RESET}"
            )
        return False

    def install_missing_external_tools(self, include_optional: bool = True) -> Tuple[int, int]:
        """Install all missing external tools when supported."""
        missing = [tool for tool in self.external_tools if not tool.installed]
        if not include_optional:
            missing = [tool for tool in missing if tool.required]

        if not missing:
            return 0, 0

        self.print_section(f"Installing {len(missing)} external tool(s)")
        success = 0
        failed = 0
        for tool in missing:
            if self.install_external_tool(tool):
                success += 1
            else:
                failed += 1
            print()
        return success, failed


def run_dependency_check(
    auto_mode: bool = False,
    install_external_tools: bool = True,
    enforce_all: bool = False,
) -> bool:
    """
    Run the full dependency check workflow.

    Args:
        auto_mode: If True, automatically install without prompting
        install_external_tools: If True, try to install missing external tools
        enforce_all: If True, treat optional Python package dependencies as required

    Returns:
        True if required dependencies are satisfied, plus optional Python packages
        when enforce_all=True. External tools are reported/handled separately.
    """
    checker = DependencyChecker()

    # Initial check
    req_ok, req_missing, opt_ok, opt_missing = checker.check_all()

    def _repairable_runtime_deps() -> List[Dependency]:
        repairable: List[Dependency] = []
        for dep in checker.python_deps:
            if dep.runtime_issue and dep.import_name in REPAIRABLE_RUNTIME_IMPORTS:
                repairable.append(dep)
        return repairable

    def _run_runtime_repair() -> bool:
        try:
            from path_utils import get_app_dir, is_frozen
        except Exception:
            get_app_dir = None
            is_frozen = None
        if callable(is_frozen) and is_frozen() and callable(get_app_dir):
            health_script = Path(get_app_dir()) / "dependency_health_check.py"
        else:
            health_script = Path(__file__).resolve().with_name("dependency_health_check.py")
        if not health_script.exists():
            return False
        print(f"\n{checker.CYAN}{'─' * 79}{checker.RESET}")
        print(f"  {checker.YELLOW}Runtime import issues detected; running deterministic repair...{checker.RESET}")
        cmd = [sys.executable, str(health_script), "--mode", "repair"]
        repaired = subprocess.run(cmd, capture_output=False, text=True, check=False)
        return repaired.returncode == 0

    repaired_runtime_stack = False
    if enforce_all and _repairable_runtime_deps():
        should_repair = auto_mode
        if not auto_mode:
            answer = input(f"  {checker.GREEN}Run runtime stack repair now? (y/N): {checker.RESET}").strip().lower()
            should_repair = answer == "y"
        if should_repair:
            repaired_runtime_stack = _run_runtime_repair()
        checker = DependencyChecker()
        req_ok, req_missing, opt_ok, opt_missing = checker.check_all()

    # Display status
    checker.display_status()

    # Display summary
    all_required_ok = checker.display_summary(req_ok, req_missing, opt_ok, opt_missing)
    all_optional_ok = opt_missing == 0
    # Strict mode applies to Python dependency categories (required + optional).
    # In strict mode we also require all external tools to be present.
    all_tools_ok = all(tool.installed for tool in checker.external_tools)
    all_ok = all_required_ok and (all_optional_ok if enforce_all else True) and (all_tools_ok if enforce_all else True)

    # If nothing missing, we're done
    missing_pip = checker.get_missing_pip_packages()
    missing_tools = [t for t in checker.external_tools if not t.installed]
    if not missing_pip and not (install_external_tools and missing_tools):
        print(f"{checker.GREEN}All Python packages are installed!{checker.RESET}")
        if missing_tools:
            print(f"\n{checker.YELLOW}Note: Some external tools are not installed.{checker.RESET}")
            print(f"{checker.GRAY}These must be installed manually (see instructions above).{checker.RESET}")
        return all_ok

    # Offer to install missing Python packages
    if missing_pip:
        print(f"\n{checker.CYAN}{'─' * 79}{checker.RESET}")

        missing_required = [d for d in missing_pip if d.required]
        missing_optional = [d for d in missing_pip if not d.required]

        print(f"\n  Missing packages:")
        if missing_required:
            print(f"    {checker.RED}Required: {', '.join(d.pip_name for d in missing_required)}{checker.RESET}")
        if missing_optional:
            print(f"    {checker.YELLOW}Optional: {', '.join(d.pip_name for d in missing_optional)}{checker.RESET}")

        print()
        print(f"  {checker.WHITE}Options:{checker.RESET}")
        print(f"    {checker.CYAN}1{checker.RESET}  Install all missing packages (required + optional)")
        print(f"    {checker.CYAN}2{checker.RESET}  Install required packages only")
        print(f"    {checker.CYAN}3{checker.RESET}  Skip installation")
        print()

        if auto_mode:
            choice = "1"
            print(f"  {checker.GRAY}Auto-mode: Installing all packages...{checker.RESET}")
        else:
            choice = input(f"  {checker.GREEN}Enter choice (1/2/3): {checker.RESET}").strip()

        if choice == "1":
            checker.install_all_missing(include_optional=True)
        elif choice == "2":
            checker.install_all_missing(include_optional=False)
        else:
            print(f"\n  {checker.YELLOW}Installation skipped.{checker.RESET}")
            return all_ok

    if install_external_tools:
        missing_tools = [tool for tool in checker.external_tools if not tool.installed]
        if missing_tools:
            if auto_mode:
                checker.install_missing_external_tools(include_optional=True)
            else:
                print(f"\n{checker.CYAN}{'─' * 79}{checker.RESET}")
                print("\n  Missing external tools:")
                print(f"    {checker.YELLOW}{', '.join(t.name for t in missing_tools)}{checker.RESET}")
                print()
                print(f"  {checker.WHITE}Install missing external tools automatically?{checker.RESET}")
                print(f"    {checker.CYAN}1{checker.RESET}  Yes")
                print(f"    {checker.CYAN}2{checker.RESET}  No")
                print()
                tool_choice = input(f"  {checker.GREEN}Enter choice (1/2): {checker.RESET}").strip()
                if tool_choice == "1":
                    checker.install_missing_external_tools(include_optional=True)

    # Re-run check to verify
    print(f"\n{checker.MAGENTA}{'═' * 79}{checker.RESET}")
    print(f"{checker.MAGENTA}  VERIFICATION - Re-checking dependencies...{checker.RESET}")
    print(f"{checker.MAGENTA}{'═' * 79}{checker.RESET}")

    # Reset and recheck
    checker = DependencyChecker()
    req_ok, req_missing, opt_ok, opt_missing = checker.check_all()
    checker.display_status()
    all_required_ok = checker.display_summary(req_ok, req_missing, opt_ok, opt_missing)
    all_optional_ok = opt_missing == 0
    all_tools_ok = all(tool.installed for tool in checker.external_tools)
    all_ok = all_required_ok and (all_optional_ok if enforce_all else True) and (all_tools_ok if enforce_all else True)

    if all_ok:
        print(f"\n{checker.GREEN}✓ All dependencies are now installed!{checker.RESET}")
    else:
        if enforce_all and all_required_ok and all_optional_ok and not all_tools_ok:
            missing_names = ", ".join(tool.name for tool in checker.external_tools if not tool.installed) or "external tools"
            print(f"\n{checker.RED}✗ Missing external runtime tool(s): {missing_names}.{checker.RESET}")
            print(f"{checker.YELLOW}Launcher strict mode requires these tools. Install and retry.{checker.RESET}")
        elif enforce_all and all_required_ok and not all_optional_ok:
            print(f"\n{checker.RED}✗ Some runtime dependencies marked optional are still missing.{checker.RESET}")
            print(f"{checker.YELLOW}Launcher strict mode requires them. Install and retry.{checker.RESET}")
        else:
            print(f"\n{checker.RED}✗ Some required dependencies are still missing.{checker.RESET}")
            print(f"{checker.YELLOW}Please install them manually and try again.{checker.RESET}")
        if enforce_all and not all_ok and not repaired_runtime_stack and _repairable_runtime_deps():
            print(f"{checker.YELLOW}Hint: runtime stack repair is available via dependency_health_check.py --mode repair{checker.RESET}")

    return all_ok


# Allow running directly
if __name__ == "__main__":
    import sys
    auto = "--auto" in sys.argv
    skip_external = "--skip-external-tools" in sys.argv
    enforce_all = "--enforce-all" in sys.argv
    success = run_dependency_check(
        auto_mode=auto,
        install_external_tools=not skip_external,
        enforce_all=enforce_all,
    )
    sys.exit(0 if success else 1)
