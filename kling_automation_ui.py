import argparse
import contextlib
import faulthandler
import io
import os
import signal
import sys
import json
import time
import threading
import traceback
from pathlib import Path
from typing import Dict, Any, Optional, List, Tuple
import logging
from rich.console import Console, Group
from rich.table import Table
from rich.live import Live
from rich.panel import Panel
from rich.progress import BarColumn, Progress, SpinnerColumn, TextColumn

try:
    from kling_gui.ml_backend_env import ensure_ml_backend_env
except ModuleNotFoundError:
    def ensure_ml_backend_env() -> None:
        os.environ["TF_USE_LEGACY_KERAS"] = "1"
        os.environ["KERAS_BACKEND"] = "tensorflow"

ensure_ml_backend_env()

# Import path utilities for frozen exe compatibility
from path_utils import (
    get_config_path,
    get_crash_log_path,
    get_app_dir,
    VALID_EXTENSIONS,
)

# Import the fal.ai KlingBatchGenerator
from kling_generator_falai import FalAIKlingGenerator
from automation.config import merge_automation_defaults, from_app_config
from automation.discovery import discover_case_folders, detect_existing_outputs
from automation.logger import key_status, resolve_automation_log_path
from automation.manifest import AutomationManifest
from automation.pipeline import AutoPipelineRunner
from automation.oldcam import discover_oldcam_versions, ensure_oldcam_dependencies
from selfie_generator import SelfieGenerator
from tk_dialogs import select_directory, select_directory_cli_safe, select_open_file

RECOMMENDED_DEFAULTS_VERSION = 1
RECOMMENDED_KLING_PROMPT_SLOT_1 = (
    "Generate a lifelike video animation from the provided image. The subject must rotate only their head in an exceptionally "
    "slow, smooth, and biologically realistic motion: start by gently turning the head left, up to 30 degrees from center, with "
    "absolutely no movement in the shoulders, neck, or upper body, which must stay perfectly upright and still. Hold a brief, "
    "natural pause at the leftmost 30 degree position, then gently turn the head all the way to the right 30 degree facing side , "
    "maintaining the same extremely slow and continuous, lifelike pace. Head motion must appear completely natural, never robotic, "
    "mechanical, stiff, or artificial—mimic genuine human motion with soft micro-adjustments. Eyes stay focused on the camera lens "
    "through both turns. Facial expression remains strictly neutral and relaxed throughout. Lighting on the face and background "
    "must stay natural, matching the original image, with no added highlights, shadows, flicker, or artificial lighting. The "
    "camera is fixed and stationary. Only the head moves; the rest of the body remains motionless."
)


def _enable_cli_crash_capture() -> Optional[str]:
    """Enable faulthandler logging for fatal native crashes."""
    crash_path = Path(get_app_dir()) / "kling_automation_crash.log"
    crash_file = None
    try:
        crash_file = open(crash_path, "a", encoding="utf-8")
        crash_file.write(f"\n\n=== Crash capture initialized at {time.strftime('%Y-%m-%d %H:%M:%S')} ===\n")
        crash_file.flush()
        faulthandler.enable(file=crash_file, all_threads=True)
        for sig_name in ("SIGSEGV", "SIGABRT", "SIGBUS", "SIGILL"):
            sig = getattr(signal, sig_name, None)
            if sig is not None:
                try:
                    faulthandler.register(sig, file=crash_file, all_threads=True)
                except Exception:
                    pass
        return str(crash_path)
    except Exception:
        if crash_file is not None:
            try:
                crash_file.close()
            except Exception:
                pass
        return None


class KlingAutomationUI:
    legacy_pauses: bool = False

    def __init__(self, legacy_pauses: bool = False):
        self.config_file = get_config_path("kling_config.json")
        self.config = merge_automation_defaults(self.load_config())
        self.automation_root_folder = self.config.get("automation_root_folder", "")
        self.verbose_logging = self.config.get("verbose_logging", False)
        self.legacy_pauses = legacy_pauses
        self._last_scan_records: List[Any] = []
        self.setup_logging()

    def pause_continue(self, message: str = "Press Enter to continue..."):
        """Pause only when legacy pause mode is enabled."""
        if self.legacy_pauses:
            input(message)

    def pause_review(self, message: str = "Press Enter to continue..."):
        """Pause for explicit review screens or actionable error surfaces."""
        input(message)

    def load_config(self) -> Dict[str, Any]:
        """Load configuration from file or create default"""
        # Default prompt slot 1 - basic head turn
        prompt_slot_1 = (
            "Turn head to the right slowly then all the way to the left slowly then to the right slowly, and to the left slowly. "
            "Make sure the body is kept still while doing this - ONLY turn THE HEAD NOT THE BODY. The subject should perform smooth, "
            "natural head movements with no body movement whatsoever. Keep shoulders, neck, and torso completely stationary. "
            "Head movements should be slow, deliberate, and realistic. Eyes can follow natural movement patterns. "
            "Maintain neutral facial expression throughout. Camera remains fixed and stationary. "
            "Generate in maximum resolution and professional quality with no blur, pixelation, or quality degradation."
        )

        default_config = {
            "output_folder": "",  # Empty by default - user picks their own
            "use_source_folder": True,  # Default: save videos alongside source images
            "falai_api_key": "",  # Will prompt user on first run
            "verbose_logging": True,
            "duplicate_detection": True,
            "delay_between_generations": 1,
            # Prompt slot system - recommended defaults use slot 1
            "current_prompt_slot": 1,
            "saved_prompts": {
                "1": RECOMMENDED_KLING_PROMPT_SLOT_1,
                "2": prompt_slot_1,
                "3": None,
                "4": None,
                "5": None,
                "6": None,
                "7": None,
                "8": None,
                "9": None,
                "10": None,
            },
            "negative_prompts": {
                "1": None,
                "2": None,
                "3": None,
                "4": None,
                "5": None,
                "6": None,
                "7": None,
                "8": None,
                "9": None,
                "10": None,
            },
            # Model configuration - Kling 2.5 Turbo Standard
            "current_model": "fal-ai/kling-video/v2.5-turbo/standard/image-to-video",
            "model_display_name": "Kling 2.5 Turbo Standard",
            # Generation parameters
            "video_duration": 10,
            "aspect_ratio": "9:16",
            "resolution": "720p",
            "seed": -1,  # -1 = random
            "camera_fixed": False,
            "generate_audio": False,
            "automation_recommended_defaults_version": RECOMMENDED_DEFAULTS_VERSION,
        }

        try:
            if Path(self.config_file).exists():
                with open(self.config_file, "r") as f:
                    loaded_config = json.load(f)
                    # Merge with defaults, ensuring new fields exist
                    merged = {**default_config, **loaded_config}
                    # Ensure saved_prompts has all slots (1-10)
                    if "saved_prompts" not in merged:
                        merged["saved_prompts"] = default_config["saved_prompts"]
                    else:
                        for slot in [str(i) for i in range(1, 11)]:
                            if slot not in merged["saved_prompts"] or merged["saved_prompts"][slot] is None:
                                merged["saved_prompts"][slot] = ""

                    # Ensure negative_prompts has all slots (1-10)
                    if "negative_prompts" not in merged:
                        merged["negative_prompts"] = default_config["negative_prompts"]
                    else:
                        for slot in [str(i) for i in range(1, 11)]:
                            if slot not in merged["negative_prompts"] or merged["negative_prompts"][slot] is None:
                                merged["negative_prompts"][slot] = ""

                    return merged
        except Exception:
            pass
        return default_config

    def get_current_prompt(self) -> str:
        """Get the current prompt from the active slot"""
        slot = str(self.config.get("current_prompt_slot", 1))
        saved = self.config.get("saved_prompts", {})
        prompt = saved.get(slot)
        if prompt:
            return prompt
        # Fallback to default
        return self.get_default_prompt()

    def get_current_negative_prompt(self) -> Optional[str]:
        """Get the current negative prompt from the active slot"""
        slot = str(self.config.get("current_prompt_slot", 1))
        saved = self.config.get("negative_prompts", {})
        return saved.get(slot)

    def get_default_prompt(self) -> str:
        """Get the default head movement prompt"""
        return (
            "Turn head to the right slowly then all the way to the left slowly then to the right slowly, and to the left slowly. "
            "Make sure the body is kept still while doing this - ONLY turn THE HEAD NOT THE BODY. The subject should perform smooth, "
            "natural head movements with no body movement whatsoever. Keep shoulders, neck, and torso completely stationary. "
            "Head movements should be slow, deliberate, and realistic. Eyes can follow natural movement patterns. "
            "Maintain neutral facial expression throughout. Camera remains fixed and stationary. "
            "Generate in maximum resolution and professional quality with no blur, pixelation, or quality degradation."
        )

    def fetch_model_pricing(self, model_endpoint: str) -> Optional[float]:
        """Fetch pricing for a model from fal.ai API"""
        try:
            import requests

            headers = {"Authorization": f"Key {self.config['falai_api_key']}"}
            response = requests.get(
                f"https://api.fal.ai/v1/models/pricing?endpoint_id={model_endpoint}",
                headers=headers,
                timeout=10,
            )
            if response.status_code == 200:
                data = response.json()
                prices = data.get("prices", [])
                if prices:
                    return prices[0].get("unit_price")
        except Exception:
            pass
        return None

    def fetch_available_models(self) -> list:
        """Fetch available video models from fal.ai Platform API with pagination"""
        try:
            import requests

            headers = {"Authorization": f"Key {self.config['falai_api_key']}"}
            all_models = []
            cursor = None

            # Paginate through all results
            while True:
                params = {"category": "image-to-video", "status": "active", "limit": 50}
                if cursor:
                    params["cursor"] = cursor

                response = requests.get(
                    "https://api.fal.ai/v1/models",
                    params=params,
                    headers=headers,
                    timeout=15,
                )

                if response.status_code != 200:
                    if self.verbose_logging:
                        print(
                            f"\033[91mAPI returned status {response.status_code}\033[0m"
                        )
                    break

                data = response.json()
                for m in data.get("models", []):
                    endpoint_id = m.get("endpoint_id", "")
                    metadata = m.get("metadata", {})
                    description = metadata.get("description", "")
                    # Keep up to 200 chars for wrapping (3 lines of ~65 chars)
                    if len(description) > 200:
                        description = description[:197] + "..."
                    all_models.append(
                        {
                            "name": metadata.get("display_name", endpoint_id),
                            "endpoint_id": endpoint_id,
                            "description": description,
                            "duration": metadata.get("duration_estimate", 10),
                        }
                    )

                # Check for more pages
                if data.get("has_more") and data.get("next_cursor"):
                    cursor = data["next_cursor"]
                else:
                    break

            # Batch fetch pricing for all models (up to 50 at a time)
            if all_models:
                endpoint_ids = [m["endpoint_id"] for m in all_models]
                prices = self.fetch_batch_pricing(endpoint_ids)
                for model in all_models:
                    model["price"] = prices.get(model["endpoint_id"])

            if all_models:
                return all_models

        except Exception as e:
            if self.verbose_logging:
                print(f"\033[91mError fetching models: {e}\033[0m")

        # Fallback to centralized model metadata
        from model_metadata import MODEL_METADATA

        # Convert to CLI format (endpoint_id instead of endpoint)
        return [
            {
                "name": m["name"],
                "endpoint_id": m["endpoint"],
                "duration_options": m["duration_options"],
                "duration_default": m["duration_default"],
                "description": m["description"],
            }
            for m in MODEL_METADATA
        ]

    def fetch_batch_pricing(self, endpoint_ids: list) -> dict:
        """Fetch pricing for multiple models at once (max 50)"""
        prices = {}
        try:
            import requests

            headers = {"Authorization": f"Key {self.config['falai_api_key']}"}

            # Process in batches of 50
            for i in range(0, len(endpoint_ids), 50):
                batch = endpoint_ids[i : i + 50]
                response = requests.get(
                    "https://api.fal.ai/v1/models/pricing",
                    params={"endpoint_id": batch},
                    headers=headers,
                    timeout=15,
                )
                if response.status_code == 200:
                    data = response.json()
                    for p in data.get("prices", []):
                        endpoint = p.get("endpoint_id", "")
                        unit_price = p.get("unit_price")
                        unit = p.get("unit", "")
                        prices[endpoint] = {"price": unit_price, "unit": unit}
        except Exception:
            pass
        return prices

    def save_config(self):
        """Save current configuration to file"""
        try:
            with open(self.config_file, "w") as f:
                json.dump(self.config, f, indent=2)
        except Exception as e:
            if self.verbose_logging:
                print(f"Error saving config: {e}")

    def setup_logging(self):
        """Setup logging based on verbose setting"""
        if self.verbose_logging:
            logging.basicConfig(
                level=logging.INFO,
                format="%(asctime)s - %(levelname)s - %(message)s",
                handlers=[
                    logging.FileHandler("kling_automation.log"),
                    logging.StreamHandler(),
                ],
            )
        else:
            logging.basicConfig(
                level=logging.ERROR,
                format="%(asctime)s - %(levelname)s - %(message)s",
                handlers=[logging.FileHandler("kling_automation.log")],
            )
            logging.getLogger().setLevel(logging.CRITICAL)

    def configure_api_provider_settings(self):
        """Provider-aware API key/editor for automation and manual tools."""
        self.clear_screen_simple()
        print("\n" + "=" * 72)
        print("  API SETUP / PROVIDER SETTINGS")
        print("=" * 72)
        print("\nFal.ai key: required for Kling video and fal-backed image/selfie providers.")
        print("BFL key: optional, used for outpainting when BFL is selected or auto-resolved.")
        print("Other providers: configured through existing app/provider settings.")
        print("\nCurrent key status:")
        fal_status = "set" if str(self.config.get("falai_api_key", "")).strip() else "missing"
        bfl_status = "set" if str(self.config.get("bfl_api_key", "")).strip() else "missing"
        print(f"  - falai_api_key: {fal_status}")
        print(f"  - bfl_api_key: {bfl_status}")
        print("\nOptions:")
        print("  1) Set/update falai_api_key")
        print("  2) Set/update bfl_api_key")
        print("  3) Clear falai_api_key")
        print("  4) Clear bfl_api_key")
        print("  0) Back")
        print()
        choice = input("Select option: ").strip()
        if choice == "1":
            value = input("Enter fal.ai API key: ").strip()
            if value:
                self.config["falai_api_key"] = value
                self.save_config()
                print("Saved falai_api_key.")
        elif choice == "2":
            value = input("Enter BFL API key: ").strip()
            if value:
                self.config["bfl_api_key"] = value
                self.save_config()
                print("Saved bfl_api_key.")
        elif choice == "3":
            self.config["falai_api_key"] = ""
            self.save_config()
            print("Cleared falai_api_key.")
        elif choice == "4":
            self.config["bfl_api_key"] = ""
            self.save_config()
            print("Cleared bfl_api_key.")
        self.pause_continue("\nPress Enter to continue...")

    def clear_screen_simple(self):
        """Clear screen without dependencies"""
        os.system("cls" if os.name == "nt" else "clear")

    def clear_screen(self):
        """Clear terminal screen"""
        os.system("cls" if os.name == "nt" else "clear")

    def print_cyan(self, text):
        """Print text in cyan color"""
        print(f"\033[96m{text}\033[0m")

    def print_light_purple(self, text):
        """Print text in light purple color"""
        print(f"\033[94m{text}\033[0m")

    def print_magenta(self, text):
        """Print text in magenta color"""
        print(f"\033[95m{text}\033[0m")

    def print_green(self, text):
        """Print text in green color"""
        print(f"\033[92m{text}\033[0m", end="")

    def print_yellow(self, text):
        """Print text in yellow color"""
        print(f"\033[93m{text}\033[0m")

    def print_red(self, text):
        """Print text in red color"""
        print(f"\033[91m{text}\033[0m")

    def display_header(self):
        """Display the primary Selfie Gen Ultimate header."""
        self.clear_screen()

        model_name = self.config.get("model_display_name", "Kling 2.1 Professional")
        duration = self.config.get("video_duration", 10)

        # Fetch pricing (cached after first call)
        if not hasattr(self, "_cached_price"):
            self._cached_price = self.fetch_model_pricing(
                self.config.get("current_model", "")
            )
        price = self._cached_price
        price_str = f"${price:.2f}/sec" if price else "Check fal.ai"

        # Beautiful header with horizontal-only borders
        print("\033[38;5;27m" + "═" * 79 + "\033[0m")
        print()

        # ASCII art title
        title_art = "SELFIE GEN ULTIMATE"
        padding = (79 - len(title_art)) // 2
        print(f"\033[1;97m{' ' * padding}{title_art}\033[0m")
        subtitle = "Front DL -> Selfie -> Similarity -> Video -> Oldcam"
        subtitle_padding = (79 - len(subtitle)) // 2
        print(f"\033[90m{' ' * subtitle_padding}{subtitle}\033[0m")

        print()
        print("\033[38;5;27m" + "─" * 79 + "\033[0m")

        # Model info row
        print(f"  Model: \033[95m{model_name}\033[0m")

        # Config row
        print(f"  Duration: \033[92m{duration}s\033[0m   ·   Price: \033[93m{price_str}\033[0m")

        # Balance link row
        print("  Workflow: \033[96mAutomation first, manual Kling tools available\033[0m")

        print()
        print("\033[38;5;27m" + "═" * 79 + "\033[0m")
        print()

    def display_configuration_menu(self):
        """Display top-level Selfie Gen Ultimate menu."""
        self.print_magenta("═" * 79)
        self.print_magenta("                         SELFIE GEN ULTIMATE")
        self.print_magenta("═" * 79)
        print()
        root_value = self.automation_root_folder or "(not set)"
        print(f"  Automation root: \033[97m{root_value}\033[0m")
        for line in self._automation_status_lines():
            print(f"  {line}")
        print()
        print("  \033[93m1\033[0m   End-to-End Auto Pipeline")
        print("  \033[93m2\033[0m   Scan automation root / preview cases")
        print("  \033[93m3\033[0m   Run/resume automation batch")
        print("  \033[93m4\033[0m   Automation settings")
        print("  \033[93m5\033[0m   Manual Kling video tools")
        print("  \033[93m6\033[0m   Launch GUI manual lab")
        print("  \033[93m7\033[0m   API keys / provider settings")
        print("  \033[93m8\033[0m   Dependency check")
        print("  \033[93m9\033[0m   Advanced video/model settings")
        print()
        print("  \033[91mq\033[0m   Quit")
        print()
        print(
            "\033[92m➤ Choose a workflow or paste automation root folder path (case folders need front.png/front.jpg/front.jpeg):\033[0m ",
            end="",
            flush=True,
        )

    def select_folder_gui(self):
        """Open GUI folder selection dialog"""
        return select_directory(title="Select Input Folder")

    def select_file_gui(self):
        """Open GUI file selection dialog"""
        return select_open_file(
            title="Select Single Input Image",
            filetypes=[
                ("Image files", "*.jpg *.jpeg *.png *.bmp *.gif *.webp *.tiff *.tif"),
                ("All files", "*.*"),
            ],
        )

    def launch_gui(self):
        """Launch the Tkinter GUI mode for drag-and-drop processing."""
        try:
            from kling_gui.main_window import KlingGUIWindow

            print("\nLaunching GUI mode...")
            gui = KlingGUIWindow(config_path=self.config_file)
            gui.run()
        except ImportError as e:
            self.print_red(f"\nGUI module not found: {e}")
            self.print_yellow("Make sure kling_gui package is in the same directory.")
            self.pause_review("Press Enter to continue...")
        except Exception as e:
            self.print_red(f"\nError launching GUI: {e}")
            self.pause_review("Press Enter to continue...")

    def check_dependencies(self):
        """Check and optionally install all required dependencies."""
        try:
            from dependency_checker import run_dependency_check

            print()
            run_dependency_check(auto_mode=False)
            print()
            self.pause_review("Press Enter to continue...")
        except ImportError as e:
            self.print_red(f"\nDependency checker module not found: {e}")
            self.print_yellow(
                "Make sure dependency_checker.py is in the same directory."
            )
            self.pause_review("Press Enter to continue...")
        except Exception as e:
            self.print_red(f"\nError running dependency check: {e}")
            self.pause_review("Press Enter to continue...")

    def toggle_verbose_logging(self):
        """Toggle verbose logging on/off"""
        self.verbose_logging = not self.verbose_logging
        self.config["verbose_logging"] = self.verbose_logging
        self.save_config()
        self.setup_logging()

        status = "enabled" if self.verbose_logging else "disabled"
        print(f"\nVerbose logging {status}")
        time.sleep(1)

    def change_output_mode(self):
        """Change output mode between source folder and custom folder"""
        print()
        use_source = self.config.get("use_source_folder", True)

        print("\033[96m" + "─" * 60 + "\033[0m")
        print("\033[95m OUTPUT MODE SETTINGS\033[0m")
        print("\033[96m" + "─" * 60 + "\033[0m")
        print()

        if use_source:
            print(f"  \033[92m✓ Current: SAME FOLDER AS SOURCE IMAGES\033[0m")
            print(f"     Videos are saved alongside each input image")
        else:
            print(f"  \033[93m✓ Current: CUSTOM FOLDER\033[0m")
            print(f"     All videos go to: {self.config['output_folder']}")
        print()

        print("\033[93mOptions:\033[0m")
        print(
            f"  \033[96m1\033[0m   Use source folder (save video next to input image)"
        )
        print(f"  \033[96m2\033[0m   Use custom folder (all videos to one location)")
        print(f"  \033[91m0\033[0m   Cancel")
        print()

        choice = input("\033[92mSelect option: \033[0m").strip()

        if choice == "1":
            self.config["use_source_folder"] = True
            self.save_config()
            print("\n\033[92m✓ Output mode: SAME FOLDER AS SOURCE IMAGES\033[0m")
            print("  Videos will be saved alongside each input image")
            time.sleep(1.5)
        elif choice == "2":
            self.config["use_source_folder"] = False
            print(
                f"\n\033[93mCurrent custom folder:\033[0m {self.config['output_folder']}"
            )
            new_path = input(
                "\033[92mEnter new folder path (or Enter to keep current):\033[0m "
            ).strip()

            if new_path and (
                (new_path.startswith('"') and new_path.endswith('"'))
                or (new_path.startswith("'") and new_path.endswith("'"))
            ):
                new_path = new_path[1:-1]

            if new_path:
                try:
                    Path(new_path).mkdir(parents=True, exist_ok=True)
                    self.config["output_folder"] = new_path
                    print(f"\033[92m✓ Custom folder set to: {new_path}\033[0m")
                except Exception as e:
                    self.print_red(f"Error creating folder: {e}")
                    time.sleep(1.5)
                    return

            self.save_config()
            print(f"\n\033[92m✓ Output mode: CUSTOM FOLDER\033[0m")
            print(f"  All videos will go to: {self.config['output_folder']}")
            time.sleep(1.5)
        else:
            print("\033[90mCancelled\033[0m")
            time.sleep(0.5)

    def configure_advanced_video_settings(self):
        """Configure advanced video generation settings"""
        while True:
            print()
            print("\033[96m" + "─" * 60 + "\033[0m")
            print("\033[95m ADVANCED VIDEO SETTINGS\033[0m")
            print("\033[96m" + "─" * 60 + "\033[0m")
            print()

            # Show current settings
            aspect_ratio = self.config.get("aspect_ratio", "9:16")
            resolution = self.config.get("resolution", "720p")
            seed = self.config.get("seed", -1)
            camera_fixed = self.config.get("camera_fixed", False)
            generate_audio = self.config.get("generate_audio", False)

            seed_display = "Random" if seed == -1 else str(seed)
            camera_status = (
                "\033[92mON\033[0m" if camera_fixed else "\033[91mOFF\033[0m"
            )
            audio_status = (
                "\033[92mON\033[0m" if generate_audio else "\033[91mOFF\033[0m"
            )

            print(
                f"  \033[93m1\033[0m   Aspect Ratio    : \033[97m{aspect_ratio}\033[0m"
            )
            print(f"  \033[93m2\033[0m   Resolution      : \033[97m{resolution}\033[0m")
            print(
                f"  \033[93m3\033[0m   Seed            : \033[97m{seed_display}\033[0m"
            )
            print(f"  \033[93m4\033[0m   Camera Fixed    : {camera_status}")
            print(f"  \033[93m5\033[0m   Generate Audio  : {audio_status}")
            print()
            print(f"  \033[91m0\033[0m   Back to main menu")
            print()

            choice = input("\033[92mSelect option: \033[0m").strip()

            if choice == "0" or choice.lower() == "q":
                break
            elif choice == "1":
                self._set_aspect_ratio()
            elif choice == "2":
                self._set_resolution()
            elif choice == "3":
                self._set_seed()
            elif choice == "4":
                self.config["camera_fixed"] = not self.config.get("camera_fixed", False)
                self.save_config()
                status = "enabled" if self.config["camera_fixed"] else "disabled"
                print(f"\n\033[92m✓ Camera fixed {status}\033[0m")
                time.sleep(0.8)
            elif choice == "5":
                self.config["generate_audio"] = not self.config.get(
                    "generate_audio", False
                )
                self.save_config()
                status = "enabled" if self.config["generate_audio"] else "disabled"
                print(f"\n\033[92m✓ Generate audio {status}\033[0m")
                time.sleep(0.8)
            else:
                print("\033[91mInvalid option\033[0m")
                time.sleep(0.5)

    def _set_aspect_ratio(self):
        """Set video aspect ratio"""
        print()
        print("\033[95mSelect Aspect Ratio:\033[0m")
        ratios = ["21:9", "16:9", "4:3", "1:1", "3:4", "9:16"]
        for i, ratio in enumerate(ratios, 1):
            current = (
                " (current)" if ratio == self.config.get("aspect_ratio", "9:16") else ""
            )
            print(f"  \033[96m{i}\033[0m   {ratio}{current}")
        print(f"  \033[91m0\033[0m   Cancel")
        print()

        choice = input("\033[92mSelect: \033[0m").strip()
        if choice in ["1", "2", "3", "4", "5", "6"]:
            selected = ratios[int(choice) - 1]
            self.config["aspect_ratio"] = selected
            self.save_config()
            print(f"\n\033[92m✓ Aspect ratio set to {selected}\033[0m")
            time.sleep(0.8)
        elif choice != "0":
            print("\033[91mInvalid option\033[0m")
            time.sleep(0.5)

    def _set_resolution(self):
        """Set video resolution"""
        print()
        print("\033[95mSelect Resolution:\033[0m")
        resolutions = ["480p", "720p"]
        for i, res in enumerate(resolutions, 1):
            current = (
                " (current)" if res == self.config.get("resolution", "720p") else ""
            )
            print(f"  \033[96m{i}\033[0m   {res}{current}")
        print(f"  \033[91m0\033[0m   Cancel")
        print()

        choice = input("\033[92mSelect: \033[0m").strip()
        if choice == "1":
            self.config["resolution"] = "480p"
            self.save_config()
            print(f"\n\033[92m✓ Resolution set to 480p\033[0m")
            time.sleep(0.8)
        elif choice == "2":
            self.config["resolution"] = "720p"
            self.save_config()
            print(f"\n\033[92m✓ Resolution set to 720p\033[0m")
            time.sleep(0.8)
        elif choice != "0":
            print("\033[91mInvalid option\033[0m")
            time.sleep(0.5)

    def _set_seed(self):
        """Set generation seed"""
        print()
        current_seed = self.config.get("seed", -1)
        seed_display = "Random" if current_seed == -1 else str(current_seed)
        print(f"\033[95mCurrent seed:\033[0m {seed_display}")
        print()
        print("Enter a seed number (integer) or 'r' for random")
        print()

        choice = input("\033[92mSeed: \033[0m").strip().lower()
        if choice == "r" or choice == "random" or choice == "-1":
            self.config["seed"] = -1
            self.save_config()
            print(f"\n\033[92m✓ Seed set to random\033[0m")
            time.sleep(0.8)
        elif choice:
            try:
                seed_val = int(choice)
                self.config["seed"] = seed_val
                self.save_config()
                print(f"\n\033[92m✓ Seed set to {seed_val}\033[0m")
                time.sleep(0.8)
            except ValueError:
                print("\033[91mInvalid seed value (must be integer)\033[0m")
                time.sleep(1)

    def inspect_model_capabilities(self):
        """Show detailed capabilities of a model via OpenAPI schema inspection"""
        from model_schema_manager import ModelSchemaManager

        self.clear_screen()
        print("\033[96m" + "═" * 79 + "\033[0m")
        self.print_magenta("                       MODEL CAPABILITY INSPECTOR")
        print("\033[96m" + "═" * 79 + "\033[0m")
        print()

        api_key = os.getenv("FAL_KEY")
        if not api_key:
            self.print_red("FAL_KEY environment variable not set")
            self.pause_continue("\nPress Enter to continue...")
            return

        # Available models to inspect
        models = {
            "1": ("fal-ai/kling-video/v2.1/pro/image-to-video", "Kling 2.1 Pro"),
            "2": ("fal-ai/kling-video/v2.5/pro/image-to-video", "Kling 2.5 Pro"),
            "3": (
                "fal-ai/kling-video/v2.5-turbo/pro/image-to-video",
                "Kling 2.5 Turbo Pro",
            ),
            "4": ("fal-ai/wan/v2.1/image-to-video", "Wan 2.1"),
            "5": ("fal-ai/veo3", "Veo 3"),
            "6": (
                "fal-ai/bytedance/seedance/v1.5/pro/image-to-video",
                "Seedance 1.5 Pro",
            ),
        }

        # Add current model if not in list
        current_model = self.config.get(
            "current_model", "fal-ai/kling-video/v2.1/pro/image-to-video"
        )
        if current_model not in [m[0] for m in models.values()]:
            models["c"] = (current_model, f"Current: {current_model.split('/')[-1]}")

        print("\033[93mSelect a model to inspect:\033[0m")
        print()
        for key, (model_id, name) in models.items():
            marker = " \033[92m(current)\033[0m" if model_id == current_model else ""
            print(f"  \033[93m{key}\033[0m  {name}{marker}")
            print(f"      \033[90m{model_id}\033[0m")
        print()
        print(f"  \033[91mq\033[0m  Back to menu")
        print()

        choice = input("\033[92m➤ Select model: \033[0m").strip().lower()

        if choice == "q" or choice not in models:
            return

        model_id, model_name = models[choice]

        print()
        print(f"\033[96mFetching schema for {model_name}...\033[0m")
        print()

        try:
            schema_manager = ModelSchemaManager(api_key)
            schema = schema_manager.get_model_schema(model_id)

            if not schema:
                self.print_yellow(f"No schema found for {model_id}")
                self.print_yellow(
                    "This model may not be available or the API returned no data."
                )
                self.pause_continue("\nPress Enter to continue...")
                return

            # schema is Dict[str, ModelParameter]
            # Separate required and optional
            required = [p for p in schema.values() if p.required]
            optional = [p for p in schema.values() if not p.required]

            print("\033[96m" + "─" * 79 + "\033[0m")
            print(f"\033[97m{model_name}\033[0m")
            print(f"\033[90m{model_id}\033[0m")
            print("\033[96m" + "─" * 79 + "\033[0m")
            print()

            # Required parameters
            print(f"\033[92mREQUIRED PARAMETERS ({len(required)}):\033[0m")
            if required:
                for p in sorted(required, key=lambda x: x.name):
                    ptype = p.type
                    desc = p.description[:60] if p.description else ""
                    print(f"  \033[97m{p.name}\033[0m \033[90m({ptype})\033[0m")
                    if desc:
                        print(f"    {desc}")
            else:
                print("  \033[90m(none)\033[0m")
            print()

            # Optional parameters
            print(f"\033[93mOPTIONAL PARAMETERS ({len(optional)}):\033[0m")
            if optional:
                for p in sorted(optional, key=lambda x: x.name):
                    ptype = p.type
                    default = p.default
                    enum_vals = p.enum
                    desc = p.description[:50] if p.description else ""

                    default_str = ""
                    if default is not None:
                        default_str = f" = \033[95m{default}\033[0m"

                    print(
                        f"  \033[97m{p.name}\033[0m \033[90m({ptype}){default_str}\033[0m"
                    )

                    if enum_vals:
                        enum_preview = ", ".join(str(v) for v in enum_vals[:5])
                        if len(enum_vals) > 5:
                            enum_preview += f", ... (+{len(enum_vals) - 5})"
                        print(f"    \033[90mAllowed: [{enum_preview}]\033[0m")

                    if desc:
                        print(f"    {desc}")
            else:
                print("  \033[90m(none)\033[0m")

            print()
            print("\033[96m" + "─" * 79 + "\033[0m")

            # Show specific parameter support for key features
            key_params = [
                "seed",
                "aspect_ratio",
                "duration",
                "cfg_scale",
                "negative_prompt",
            ]
            print("\033[97mKEY FEATURE SUPPORT:\033[0m")
            for param in key_params:
                supported = schema_manager.supports_parameter(model_id, param)
                status = "\033[92m✓\033[0m" if supported else "\033[91m✗\033[0m"
                print(f"  {status} {param}")

            print()

        except Exception as e:
            self.print_red(f"Error fetching schema: {e}")

        self.pause_continue("\nPress Enter to continue...")

    def edit_prompt(self):
        """Edit or view the Kling generation prompt (full editor with slot support)"""
        self.clear_screen()

        current_slot = str(self.config.get("current_prompt_slot", 1))
        current_prompt = self.get_current_prompt()
        default_prompt = self.get_default_prompt()

        print("\033[96m" + "═" * 79 + "\033[0m")
        self.print_magenta("                           KLING PROMPT EDITOR")
        print("\033[96m" + "═" * 79 + "\033[0m")
        print()

        # Show all slots
        print("\033[93mSaved Prompts:\033[0m")
        saved_prompts = self.config.get("saved_prompts", {})
        for i in range(1, 11):
            slot_key = str(i)
            prompt = saved_prompts.get(slot_key)
            active = " \033[92m(ACTIVE)\033[0m" if slot_key == current_slot else ""
            if prompt:
                preview = prompt[:50] + "..." if len(prompt) > 50 else prompt
                print(f"  [{i}] {preview}{active}")
            else:
                print(f"  [{i}] \033[90m(empty){active}\033[0m")
        print()

        # Show current prompt in full
        print("\033[93mCurrent Prompt (Slot {}):\033[0m".format(current_slot))
        print("\033[97m" + "─" * 79 + "\033[0m")
        words = current_prompt.split()
        line = ""
        for word in words:
            if len(line) + len(word) + 1 <= 75:
                line += word + " "
            else:
                print(f"  {line}")
                line = word + " "
        if line:
            print(f"  {line}")
        print("\033[97m" + "─" * 79 + "\033[0m")

        # Show negative prompt if exists
        neg_prompt = self.config.get("negative_prompts", {}).get(current_slot)
        if neg_prompt:
            print(f"\033[91mNegative Prompt:\033[0m {neg_prompt}")
            print("\033[97m" + "─" * 79 + "\033[0m")
        print()

        print("\033[92mOptions:\033[0m")
        print("  \033[93m1\033[0m - Reset to default prompt (head movement)")
        print("  \033[93m2\033[0m - Enter custom prompt for current slot")
        print("  \033[93m3\033[0m - Edit NEGATIVE prompt for current slot")
        print("  \033[93m4\033[0m - Clear current slot (make empty)")
        print("  \033[93m5\033[0m - Return without changes")
        print()

        choice = input("\033[92mSelect option (1-5): \033[0m").strip()

        if choice == "1":
            self.config["saved_prompts"][current_slot] = default_prompt
            self.save_config()
            print("\n\033[92mReset to default head movement prompt\033[0m")
            time.sleep(1.5)
        elif choice == "2":
            print()
            print(
                "\033[93mEnter your custom prompt (press Enter twice when done):\033[0m"
            )
            print("\033[90m(Tip: You can paste multi-line text)\033[0m")
            print()

            lines = []
            empty_count = 0
            while True:
                try:
                    line = input()
                    if line:
                        lines.append(line)
                        empty_count = 0
                    else:
                        empty_count += 1
                        if empty_count >= 2:
                            break
                except EOFError:
                    break

            if lines:
                custom_prompt = " ".join(lines).strip()
                self.config["saved_prompts"][current_slot] = custom_prompt
                self.save_config()
                print(
                    "\n\033[92mCustom prompt saved to Slot {}!\033[0m".format(
                        current_slot
                    )
                )
                time.sleep(1.5)
            else:
                print("\n\033[91mNo prompt entered, keeping current\033[0m")
                time.sleep(1.5)
        elif choice == "3":
            print()
            print(
                "\033[93mEnter NEGATIVE prompt (what to avoid - e.g. 'blur, bokeh'):\033[0m"
            )
            neg_prompt = input("\033[92m➤ \033[0m").strip()

            if neg_prompt:
                self.config["negative_prompts"][current_slot] = neg_prompt
                self.save_config()
                print(
                    "\n\033[92mNegative prompt saved to Slot {}!\033[0m".format(
                        current_slot
                    )
                )
                time.sleep(1.5)
            else:
                print("\n\033[90mCancelled\033[0m")
                time.sleep(0.5)
        elif choice == "4":
            self.config["saved_prompts"][current_slot] = ""
            self.config["negative_prompts"][current_slot] = ""
            self.save_config()
            print("\n\033[93mSlot {} cleared\033[0m".format(current_slot))
            time.sleep(1.5)

    def quick_edit_prompt(self):
        """Quick inline prompt editor - single line input"""
        print()
        print(
            "\033[93mQuick Edit - Enter new prompt (single line, or press Enter to cancel):\033[0m"
        )
        new_prompt = input("\033[92m➤ \033[0m").strip()

        if new_prompt:
            current_slot = str(self.config.get("current_prompt_slot", 1))
            self.config["saved_prompts"][current_slot] = new_prompt
            self.save_config()
            print("\033[92m✓ Prompt saved to Slot {}\033[0m".format(current_slot))
            time.sleep(1)
        else:
            print("\033[90mCancelled\033[0m")
            time.sleep(0.5)

    def swap_prompt_slot(self):
        """Swap between prompt slots 1, 2, 3"""
        print()
        saved_prompts = self.config.get("saved_prompts", {})
        current_slot = self.config.get("current_prompt_slot", 1)

        print("\033[93mSaved Prompts:\033[0m")
        for i in range(1, 11):
            slot_key = str(i)
            prompt = saved_prompts.get(slot_key)
            active = " \033[92m◄ ACTIVE\033[0m" if slot_key == str(current_slot) else ""
            if prompt:
                preview = prompt[:60] + "..." if len(prompt) > 60 else prompt
                print(f"  [\033[96m{i}\033[0m] {preview}{active}")
            else:
                print(f"  [\033[90m{i}\033[0m] \033[90m(empty)\033[0m{active}")
        print()

        choice = input("\033[92mSelect slot (1-10) or Enter to cancel: \033[0m").strip()
        if choice.isdigit() and 1 <= int(choice) <= 10:
            self.config["current_prompt_slot"] = int(choice)
            self.save_config()
            prompt = saved_prompts.get(choice)
            if prompt:
                print(f"\033[92m✓ Switched to Slot {choice}\033[0m")
            else:
                print(
                    f"\033[93m⚠ Switched to Slot {choice} (empty - will use default)\033[0m"
                )
            time.sleep(1)
        else:
            print("\033[90mCancelled\033[0m")
            time.sleep(0.5)

    def select_model(self):
        """Select AI model from presets or enter custom endpoint"""
        self.clear_screen()

        print("\033[96m" + "═" * 79 + "\033[0m")
        self.print_magenta("                           MODEL SELECTION")
        print("\033[96m" + "═" * 79 + "\033[0m")
        print()

        current_model = self.config.get("current_model", "")
        current_name = self.config.get("model_display_name", "Unknown")
        print(f"\033[95mCurrent model:\033[0m {current_name}")
        print(f"\033[90m  Endpoint: {current_model}\033[0m")
        print()

        # Preset models
        presets = [
            (
                "Kling 2.1 Professional",
                "fal-ai/kling-video/v2.1/pro/image-to-video",
                10,
            ),
            (
                "Kling 2.5 Turbo Pro",
                "fal-ai/kling-video/v2.5-turbo/pro/image-to-video",
                10,
            ),
            ("Wan 2.5", "fal-ai/wan-25-preview/image-to-video", 5),
            ("Veo 3", "fal-ai/veo3/image-to-video", 8),
            ("Ovi", "fal-ai/ovi/image-to-video", 5),
        ]

        print("\033[93mPreset Models:\033[0m")
        for idx, (name, endpoint, duration) in enumerate(presets, 1):
            # Fetch pricing
            price = self.fetch_model_pricing(endpoint)
            price_str = f"${price:.2f}/sec" if price else "check fal.ai"
            active = " \033[92m◄\033[0m" if endpoint == current_model else ""
            print(f"  \033[96m{idx}\033[0m   {name} ({price_str}){active}")

        print()
        print(f"  \033[93m6\033[0m   Enter custom endpoint")
        print(f"  \033[93m7\033[0m   Fetch all models from fal.ai")
        print(f"  \033[91m0\033[0m   Cancel")
        print()
        print(f"  \033[90mSee all: https://fal.ai/models?category=video\033[0m")
        print()

        choice = input("\033[92mSelect option: \033[0m").strip()

        if choice == "0":
            return
        elif choice == "6":
            # Custom endpoint
            print()
            print(
                "\033[93mEnter fal.ai endpoint ID (e.g., fal-ai/kling-video/v2.1/pro/image-to-video):\033[0m"
            )
            endpoint = input("\033[92m➤ \033[0m").strip()
            if endpoint:
                name = (
                    input("\033[92mDisplay name for this model: \033[0m").strip()
                    or endpoint
                )
                # Duration prompt with common options
                print("\033[93mCommon durations: 5s (most models), 10s (most models), 15s (some models)\033[0m")
                duration_input = input(
                    "\033[92mVideo duration in seconds (5, 10, 15, default 10): \033[0m"
                ).strip()

                # Parse and validate duration
                if duration_input.isdigit():
                    duration = int(duration_input)
                    # Warn about uncommon durations but allow them
                    if duration not in [2, 3, 4, 5, 6, 7, 8, 10, 15]:
                        print(f"\033[93m⚠ Uncommon duration {duration}s - verify model supports this\033[0m")
                else:
                    duration = 10

                self.config["current_model"] = endpoint
                self.config["model_display_name"] = name
                self.config["video_duration"] = duration
                self._cached_price = None  # Clear cache
                self.save_config()
                print(f"\033[92m✓ Model set to: {name}\033[0m")
                time.sleep(1.5)
        elif choice == "7":
            # Show all available models with pagination
            print("\n\033[93mFetching all image-to-video models from fal.ai...\033[0m")
            models = self.fetch_available_models()
            current_model = self.config.get("current_model", "")
            page_size = 40  # Show up to 40 per page
            page = 0
            total_pages = (len(models) + page_size - 1) // page_size

            print(f"\033[92mFound {len(models)} models total\033[0m")

            while True:
                start_idx = page * page_size
                end_idx = min(start_idx + page_size, len(models))
                page_models = models[start_idx:end_idx]

                print(f"\n\033[92m{'═' * 60}\033[0m")
                print(
                    f"\033[92m  Image-to-Video Models  ·  Page {page + 1}/{total_pages}  ·  Showing {start_idx + 1}-{end_idx} of {len(models)}\033[0m"
                )
                print(f"\033[92m{'═' * 60}\033[0m\n")
                for idx, m in enumerate(page_models, start_idx + 1):
                    endpoint = m.get("endpoint_id", "")
                    name = m.get("name", endpoint)
                    duration = m.get("duration", 10)
                    description = m.get("description", "")
                    price_info = m.get("price")
                    if price_info:
                        price_str = f"${price_info['price']:.3f}/{price_info['unit']}"
                    else:
                        price_str = "pricing unavailable"
                    active = (
                        "  \033[92m◄ CURRENT\033[0m"
                        if endpoint == current_model
                        else ""
                    )

                    print(f"  \033[96m{idx:2d}\033[0m  \033[1;97m{name}\033[0m{active}")
                    print(f"       Price: \033[93m{price_str}\033[0m")
                    if description:
                        # Wrap description to ~65 chars per line, max 3 lines
                        words = description.split()
                        lines = []
                        current_line = ""
                        for word in words:
                            if len(current_line) + len(word) + 1 <= 65:
                                current_line += (" " if current_line else "") + word
                            else:
                                if current_line:
                                    lines.append(current_line)
                                current_line = word
                            if len(lines) >= 3:
                                break
                        if current_line and len(lines) < 3:
                            lines.append(current_line)
                        for line in lines[:3]:
                            print(f"       \033[90m{line}\033[0m")
                    print(f"       \033[36m{endpoint}\033[0m")
                    print()  # Blank line between entries

                print()
                nav_hint = []
                if page > 0:
                    nav_hint.append("p=prev")
                if page < total_pages - 1:
                    nav_hint.append("n=next")
                nav_str = f" ({', '.join(nav_hint)})" if nav_hint else ""

                sel = (
                    input(
                        f"\033[92mEnter number to select{nav_str}, or Enter to cancel: \033[0m"
                    )
                    .strip()
                    .lower()
                )

                if sel == "n" and page < total_pages - 1:
                    page += 1
                    continue
                elif sel == "p" and page > 0:
                    page -= 1
                    continue
                elif sel == "" or sel == "q":
                    break
                elif sel.isdigit() and 1 <= int(sel) <= len(models):
                    selected = models[int(sel) - 1]
                    self.config["current_model"] = selected.get("endpoint_id")
                    self.config["model_display_name"] = selected.get(
                        "name", selected.get("endpoint_id")
                    )
                    self.config["video_duration"] = selected.get("duration", 10)
                    self._cached_price = None
                    self.save_config()
                    print(f"\033[92m✓ Model set to: {selected.get('name')}\033[0m")
                    time.sleep(1.5)
                    break
                else:
                    print("\033[91mInvalid selection\033[0m")
                    time.sleep(1)
        elif choice.isdigit() and 1 <= int(choice) <= len(presets):
            name, endpoint, duration = presets[int(choice) - 1]
            self.config["current_model"] = endpoint
            self.config["model_display_name"] = name
            self.config["video_duration"] = duration
            self._cached_price = None  # Clear price cache
            self.save_config()
            print(f"\033[92m✓ Model set to: {name}\033[0m")
            time.sleep(1.5)

    def run_configuration_menu(self):
        """Main top-level menu loop."""
        while True:
            self.display_header()
            self.display_configuration_menu()
            choice = input().strip()
            if choice.startswith('"') and choice.endswith('"'):
                choice = choice[1:-1]
            elif choice.startswith("'") and choice.endswith("'"):
                choice = choice[1:-1]
            choice_lower = choice.lower()
            if choice_lower == "q":
                print("\nGoodbye!")
                sys.exit(0)
            if choice_lower == "1":
                self.run_automation_menu()
            elif choice_lower == "2":
                self._scan_automation_cases()
            elif choice_lower == "3":
                self._run_resume_automation()
            elif choice_lower == "4":
                self._edit_automation_settings()
            elif choice_lower == "5":
                selected_path = self._run_manual_kling_menu()
                if selected_path:
                    return selected_path
            elif choice_lower == "6":
                self.launch_gui()
            elif choice_lower == "7":
                self.configure_api_provider_settings()
            elif choice_lower == "8":
                self.check_dependencies()
            elif choice_lower == "9":
                self.configure_advanced_video_settings()
            elif choice and Path(choice).exists():
                selected_root = Path(choice)
                if selected_root.is_dir():
                    self.automation_root_folder = str(selected_root)
                    self.config["automation_root_folder"] = self.automation_root_folder
                    self.save_config()
                    self._scan_automation_cases()
                else:
                    self.print_red(f"Path is not a folder: {choice}")
                    self.pause_continue("Press Enter to continue...")
            elif choice:
                self.print_red(f"Path not found: {choice}")
                self.pause_continue("Press Enter to continue...")
            else:
                self.print_yellow("Please enter a valid path or select an option")
                time.sleep(1)

    def _automation_manifest_path(self) -> Optional[Path]:
        if not self.automation_root_folder:
            return None
        raw_manifest_name = str(self.config.get("automation_manifest_name", "automation_manifest.json") or "").strip()
        safe_manifest_name = Path(raw_manifest_name).name if raw_manifest_name else "automation_manifest.json"
        if not safe_manifest_name.endswith(".json"):
            safe_manifest_name = "automation_manifest.json"
        return Path(self.automation_root_folder) / safe_manifest_name

    def _resolve_provider(self, configured_provider: str) -> str:
        normalized = str(configured_provider or "auto").strip().lower()
        if normalized in {"bfl", "fal"}:
            return normalized
        if str(self.config.get("bfl_api_key", "")).strip():
            return "bfl"
        return "fal"

    def _selfie_model_label_map(self) -> Dict[str, str]:
        return {item.get("endpoint", ""): item.get("label", item.get("endpoint", "")) for item in SelfieGenerator.get_available_models()}

    def _ensure_selfie_prompt_slots(self) -> None:
        prompts = self.config.get("automation_selfie_prompts")
        if not isinstance(prompts, dict):
            prompts = {}
        for i in range(1, 11):
            prompts.setdefault(str(i), "")
        if not prompts.get("1"):
            prompts["1"] = merge_automation_defaults({}).get("automation_selfie_prompts", {}).get("1", "")
        self.config["automation_selfie_prompts"] = prompts
        slot = int(self.config.get("automation_selfie_prompt_slot", 1))
        if slot < 1 or slot > 10:
            slot = 1
        self.config["automation_selfie_prompt_slot"] = slot

    def _get_selected_selfie_prompt(self) -> Tuple[str, str, str]:
        self._ensure_selfie_prompt_slots()
        slot = str(self.config.get("automation_selfie_prompt_slot", 1))
        prompt = str(self.config.get("automation_selfie_prompts", {}).get(slot, "") or "").strip()
        if prompt:
            return slot, prompt, f"slot:{slot}"
        default_prompt = merge_automation_defaults({}).get("automation_selfie_prompts", {}).get("1", "")
        return slot, default_prompt, "default_seeded_prompt"

    def _oldcam_readiness_status(self) -> str:
        repo_root = Path(__file__).resolve().parent
        versions = discover_oldcam_versions(repo_root)
        deps_ok, _deps_err = ensure_oldcam_dependencies()
        if not versions:
            return "unavailable(no version)"
        if not deps_ok:
            return "unavailable(deps)"
        return f"ready({','.join(versions)})"

    def _automation_status_lines(self) -> List[str]:
        model_labels = self._selfie_model_label_map()
        selfie_models = [model_labels.get(x, x) for x in list(self.config.get("automation_selfie_models", []))]
        selfie_slot, _selfie_prompt, selfie_prompt_source = self._get_selected_selfie_prompt()
        front_configured = str(self.config.get("automation_front_expand_provider", "auto"))
        selfie_configured = str(self.config.get("automation_selfie_expand_provider", "auto"))
        lines = [
            f"root={self.automation_root_folder or '(not set)'} max_cases={self._read_max_cases_setting()}",
            f"keys fal={key_status(self.config.get('falai_api_key'))} bfl={key_status(self.config.get('bfl_api_key'))}",
            f"front mode={self.config.get('automation_front_expand_mode')} pct={self.config.get('automation_front_expand_percent', 70)} passes={self.config.get('automation_front_expand_passes', 2)} provider={front_configured}->{self._resolve_provider(front_configured)}",
            f"selfie expand mode={self.config.get('automation_selfie_expand_mode')} pct={self.config.get('automation_selfie_expand_percent', 30)} provider={selfie_configured}->{self._resolve_provider(selfie_configured)}",
            f"selfie models={', '.join(selfie_models) if selfie_models else '(none)'} prompt_slot={selfie_slot} prompt_source={selfie_prompt_source}",
            f"similarity_threshold={self.config.get('automation_similarity_threshold', 80)} video_model={self.config.get('model_display_name') or self.config.get('current_model')} kling_prompt_slot={self.config.get('current_prompt_slot', 1)}",
            f"oldcam version={self.config.get('automation_oldcam_version', 'all')} required={self.config.get('automation_oldcam_required', False)} readiness={self._oldcam_readiness_status()}",
            f"recommended_defaults_version={self.config.get('automation_recommended_defaults_version', 0)} target={RECOMMENDED_DEFAULTS_VERSION}",
            f"automation_verbose_logging={bool(self.config.get('automation_verbose_logging', self.config.get('verbose_logging', True)))} log_path={resolve_automation_log_path(self.config, self.automation_root_folder)}",
        ]
        return lines

    def _apply_recommended_automation_defaults(self) -> None:
        before = {
            "front": (
                self.config.get("automation_front_expand_provider"),
                self.config.get("automation_front_expand_mode"),
                self.config.get("automation_front_expand_percent"),
                self.config.get("automation_front_expand_passes"),
            ),
            "selfie_expand": (
                self.config.get("automation_selfie_expand_provider"),
                self.config.get("automation_selfie_expand_mode"),
                self.config.get("automation_selfie_expand_percent"),
            ),
            "selfie_models": list(self.config.get("automation_selfie_models", [])),
            "video_model": self.config.get("model_display_name") or self.config.get("current_model"),
            "selfie_prompt_slot": self.config.get("automation_selfie_prompt_slot", 1),
            "kling_prompt_slot": self.config.get("current_prompt_slot", 1),
            "oldcam": (self.config.get("automation_oldcam_version", "v8"), self.config.get("automation_oldcam_required", False)),
            "max_cases": self._read_max_cases_setting(),
        }

        valid_max_cases = {"1", "5", "10", "all"}
        current_max_cases = str(self.config.get("automation_max_cases_per_run", "")).strip().lower()
        if current_max_cases in valid_max_cases:
            max_cases_status = f"preserved ({current_max_cases})"
        else:
            self.config["automation_max_cases_per_run"] = "1"
            max_cases_status = "set to 1 (invalid/missing previous value)"

        self.config["automation_front_expand_provider"] = "bfl"
        self.config["automation_front_expand_mode"] = "percent"
        self.config["automation_front_expand_percent"] = 70
        self.config["automation_front_expand_passes"] = 2
        self.config["automation_front_edge_seal_enabled"] = False
        self.config["automation_selfie_expand_provider"] = "bfl"
        self.config["automation_selfie_expand_mode"] = "percent"
        self.config["automation_selfie_expand_percent"] = 30
        self.config["automation_selfie_expand_edge_seal_enabled"] = False
        self.config["automation_selfie_models"] = ["fal-ai/nano-banana-2/edit"]
        self.config["automation_selfie_prompt_slot"] = 1
        self._ensure_selfie_prompt_slots()
        self.config["automation_selfie_prompts"]["1"] = merge_automation_defaults({}).get("automation_selfie_prompts", {}).get("1", "")
        self.config["current_model"] = "fal-ai/kling-video/v2.5-turbo/standard/image-to-video"
        self.config["model_display_name"] = "Kling 2.5 Turbo Standard"
        self.config["current_prompt_slot"] = 1
        saved_prompts = self.config.get("saved_prompts")
        if not isinstance(saved_prompts, dict):
            saved_prompts = {}
        saved_prompts["1"] = RECOMMENDED_KLING_PROMPT_SLOT_1
        self.config["saved_prompts"] = saved_prompts
        self.config["automation_similarity_threshold"] = 80
        self.config["automation_video_enabled"] = True
        self.config["automation_oldcam_enabled"] = True
        self.config["automation_oldcam_version"] = "all"
        self.config["automation_oldcam_required"] = True
        self.config["automation_recommended_defaults_version"] = RECOMMENDED_DEFAULTS_VERSION
        self.save_config()

        print("\nApplied recommended automation defaults.")
        print("Before -> After")
        print(f"  front expand: {before['front'][0]} / {before['front'][1]} / {before['front'][2]} -> bfl / percent / 70")
        print(f"  selfie expand: {before['selfie_expand'][0]} / {before['selfie_expand'][1]} / {before['selfie_expand'][2]} -> bfl / percent / 30")
        print(f"  selfie model: {before['selfie_models']} -> Nano Banana 2 Edit")
        print(f"  video model: {before['video_model']} -> Kling 2.5 Turbo Standard")
        print(f"  selfie prompt slot: {before['selfie_prompt_slot']} -> 1")
        print(f"  Kling prompt slot: {before['kling_prompt_slot']} -> 1")
        print(f"  oldcam: {before['oldcam'][0]} / {'required' if before['oldcam'][1] else 'optional'} -> all / required")
        print(f"  max cases per run: {before['max_cases']} -> {self._read_max_cases_setting()} ({max_cases_status})")
        print("\nCurrent recommended state:")
        print("  front expand: bfl / percent / 70")
        print("  selfie expand: bfl / percent / 30")
        print("  selfie model: Nano Banana 2 Edit")
        print("  video model: Kling 2.5 Turbo Standard")
        print("  selfie prompt slot: 1")
        print("  Kling prompt slot: 1")
        print("  oldcam: all / required")
        print(f"  max cases per run: {self._read_max_cases_setting()}")
        self.pause_continue("\nPress Enter to continue...")

    def _display_automation_menu(self):
        self.display_header()
        self.print_magenta("═" * 79)
        self.print_magenta("                     END-TO-END AUTO PIPELINE")
        self.print_magenta("═" * 79)
        print()
        current_root = self.automation_root_folder or "(not set)"
        print(f"  Root folder: \033[97m{current_root}\033[0m")
        for line in self._automation_status_lines():
            print(f"  {line}")
        current_version = int(self.config.get("automation_recommended_defaults_version", 0) or 0)
        if current_version < RECOMMENDED_DEFAULTS_VERSION:
            print(f"  \033[93mRecommendation:\033[0m apply recommended defaults (target version {RECOMMENDED_DEFAULTS_VERSION}).")
        print()
        print("  \033[93m1\033[0m   Select automation root folder")
        print("  \033[93m2\033[0m   Scan / preview cases")
        print("  \033[93m3\033[0m   Apply recommended automation defaults")
        print("  \033[93m4\033[0m   Edit automation settings")
        print("  \033[93m5\033[0m   Dry run")
        print("  \033[93m6\033[0m   Run / resume automation")
        print("  \033[93m7\033[0m   Print manifest path")
        print("  \033[93m0\033[0m   Back")
        print()
        print("\033[92m➤ Select option:\033[0m ", end="", flush=True)

    def _select_automation_root(self):
        logging.info("automation_root_select_start")
        print("\nSelect automation root:")
        print("  1) Browse for folder (recommended)")
        print("  2) Type folder path")
        choice = input("Choose option [1/2, default 1]: ").strip()
        selected_path: Optional[str] = None
        use_browse = choice in {"", "1"}
        logging.info("automation_root_select_mode use_browse=%s choice=%s", use_browse, choice or "<default>")
        if use_browse:
            logging.info("automation_root_picker_browse_attempt")
            try:
                logging.info(
                    "automation_root_picker_backend backend=%s",
                    "osascript" if sys.platform == "darwin" else "tk",
                )
                selected_path = select_directory_cli_safe(title="Select Automation Root Folder")
            except Exception as exc:
                self.print_yellow(f"Folder picker unavailable ({exc}). Falling back to typed path.")
                logging.warning("automation_root_picker_browse_error error=%s", exc, exc_info=True)
                selected_path = None
            if selected_path is None:
                self.print_yellow("Folder picker canceled or unavailable. Enter a path manually.")
                logging.info("automation_root_picker_browse_canceled_or_unavailable")
            if not selected_path:
                logging.info("automation_root_typed_fallback_prompt")
                raw = input("Enter automation root folder path (leave blank to cancel): ").strip()
                if not raw:
                    self.print_yellow("Automation root selection canceled.")
                    logging.info("automation_root_select_canceled")
                    return
                if raw.startswith('"') and raw.endswith('"'):
                    raw = raw[1:-1]
                elif raw.startswith("'") and raw.endswith("'"):
                    raw = raw[1:-1]
                selected_path = raw
        else:
            logging.info("automation_root_typed_primary_prompt")
            raw = input("Enter automation root folder path (leave blank to cancel): ").strip()
            if not raw:
                self.print_yellow("Automation root selection canceled.")
                logging.info("automation_root_select_canceled")
                return
            if raw.startswith('"') and raw.endswith('"'):
                raw = raw[1:-1]
            elif raw.startswith("'") and raw.endswith("'"):
                raw = raw[1:-1]
            selected_path = raw

        selected = Path(selected_path)
        if not selected.exists() or not selected.is_dir():
            self.print_red("Invalid folder path.")
            logging.warning("automation_root_select_invalid path=%s", selected)
            self.pause_continue("Press Enter to continue...")
            return
        self.automation_root_folder = str(selected)
        logging.info("automation_root_select_success path=%s", self.automation_root_folder)
        self.config["automation_root_folder"] = self.automation_root_folder
        self.save_config()
        self.print_yellow(f"Automation root set: {self.automation_root_folder}")
        self._scan_automation_cases()

    def _normalize_max_cases(self, value: Any) -> Optional[int]:
        raw = str(value).strip().lower()
        if raw == "all":
            return None
        if raw.isdigit():
            parsed = int(raw)
            if parsed in {1, 5, 10}:
                return parsed
        return 5

    def _read_max_cases_setting(self) -> str:
        raw = str(self.config.get("automation_max_cases_per_run", 5)).strip().lower()
        if raw in {"1", "5", "10", "all"}:
            return raw
        return "5"

    def _planned_action_for_case(self, case_entry: Dict[str, Any], existing: Any, is_complete: bool) -> str:
        status = str(case_entry.get("status", "pending"))
        if is_complete and self.config.get("automation_skip_completed", True):
            return "skip_complete"
        if status == "manual_review":
            gate_error = str(case_entry.get("steps", {}).get("similarity_gate", {}).get("error", "") or "")
            if "similarity unavailable" in gate_error.lower():
                return "run_pending"
            return "manual_review"
        if status == "failed":
            return "failed"
        if self.config.get("automation_skip_if_video_exists", True) and existing.video_candidate:
            return "skip_video_exists"
        if self.config.get("automation_skip_if_selfie_exists", True) and existing.selfie_candidate:
            return "skip_selfie_exists"
        return "run_pending"

    def _collect_case_snapshot(
        self,
        records: List[Any],
        manifest: Optional[AutomationManifest],
    ) -> Tuple[List[Dict[str, Any]], Dict[str, int], List[Any]]:
        rows: List[Dict[str, Any]] = []
        counts = {
            "discovered": len(records),
            "completed_total": 0,
            "skipped_complete": 0,
            "pending": 0,
            "manual_review": 0,
            "failed": 0,
            "existing_videos_selfies": 0,
            "will_run": 0,
        }
        runnable: List[Any] = []
        for record in records:
            case_entry = manifest.data.get("cases", {}).get(record.relative_key, {}) if manifest else {}
            existing = detect_existing_outputs(record.case_dir)
            is_complete = bool(
                manifest
                and case_entry.get("status") == "complete"
                and manifest.case_is_complete_and_valid(record.relative_key)
            )
            if existing.video_candidate or existing.selfie_candidate:
                counts["existing_videos_selfies"] += 1
            planned = self._planned_action_for_case(case_entry, existing, is_complete)
            if is_complete:
                counts["completed_total"] += 1
            if planned == "skip_complete":
                counts["skipped_complete"] += 1
            elif planned == "manual_review":
                counts["manual_review"] += 1
            elif planned == "failed":
                counts["failed"] += 1
            elif planned in {"run_pending", "skip_video_exists", "skip_selfie_exists"}:
                counts["pending"] += 1
                runnable.append(record)
            row = {
                "case": record.relative_key,
                "front": record.front_path.name,
                "front_expanded": "yes" if existing.front_expanded else "-",
                "extracted": "yes" if existing.extracted else "-",
                "selfie": "yes" if existing.selfie_candidate else "-",
                "video": "yes" if existing.video_candidate else "-",
                "manifest_status": str(case_entry.get("status", "pending")),
                "planned": planned,
            }
            rows.append(row)

        max_cases = self._normalize_max_cases(self._read_max_cases_setting())
        capped = runnable[:max_cases] if max_cases is not None else runnable
        counts["will_run"] = len(capped)
        return rows, counts, capped

    def _scan_automation_cases(self):
        if not self.automation_root_folder:
            self.print_red("Set automation root folder first.")
            self.pause_continue("Press Enter to continue...")
            return
        root = Path(self.automation_root_folder)
        if not root.exists():
            self.print_red("Automation root path does not exist.")
            self.pause_continue("Press Enter to continue...")
            return
        records = discover_case_folders(root, self.config.get("automation_front_names", []))
        manifest = AutomationManifest.load_if_exists(self._automation_manifest_path())
        rows, counts, _ = self._collect_case_snapshot(records, manifest)
        table = Table(title="Automation Scan Preview")
        table.add_column("Case")
        table.add_column("Front")
        table.add_column("front-expanded")
        table.add_column("extracted")
        table.add_column("selfie")
        table.add_column("video")
        table.add_column("manifest status")
        table.add_column("planned action")
        for row in rows[:60]:
            table.add_row(
                row["case"],
                row["front"],
                row["front_expanded"],
                row["extracted"],
                row["selfie"],
                row["video"],
                row["manifest_status"],
                row["planned"],
            )
        console = Console()
        console.print(table)
        if len(records) > 60:
            print(f"\nShowing first 60/{len(records)} cases.")
        else:
            print(f"\nDiscovered {len(records)} case folders.")
        print("\nTotals:")
        print(f"  discovered: {counts['discovered']}")
        print(f"  completed total: {counts['completed_total']}")
        print(f"  skipped complete: {counts['skipped_complete']}")
        print(f"  pending/runnable: {counts['pending']}")
        print(f"  will run this batch: {counts['will_run']}")
        print(f"  manual review: {counts['manual_review']}")
        print(f"  failed: {counts['failed']}")
        print(f"  existing videos/selfies: {counts['existing_videos_selfies']}")
        print(f"  max cases per run: {self._read_max_cases_setting()}")
        self.pause_continue("\nPress Enter to continue...")

    def _edit_automation_settings(self):
        def _ask(prompt: str, key: str, cast_fn, validator=None):
            current = self.config.get(key)
            raw = input(f"{prompt} (current: {current}) [Enter keep]: ").strip()
            if not raw:
                return
            try:
                value = cast_fn(raw)
                if validator and not validator(value):
                    raise ValueError("validation failed")
                self.config[key] = value
            except Exception:
                self.print_red(f"Invalid value for {key}. Keeping previous value.")

        def _ask_choice(prompt: str, key: str, choices: list):
            current = str(self.config.get(key))
            raw = input(f"{prompt} {choices} (current: {current}) [Enter keep]: ").strip().lower()
            if not raw:
                return
            if raw not in choices:
                self.print_red(f"Invalid choice for {key}.")
                return
            self.config[key] = raw

        def _ask_bool(prompt: str, key: str):
            current = bool(self.config.get(key, False))
            raw = input(f"{prompt} [y/n] (current: {'y' if current else 'n'}) [Enter keep]: ").strip().lower()
            if not raw:
                return
            if raw in {"y", "yes", "1", "true"}:
                self.config[key] = True
            elif raw in {"n", "no", "0", "false"}:
                self.config[key] = False
            else:
                self.print_red(f"Invalid boolean for {key}.")

        print("\nAutomation Settings Editor (Grouped)")
        print("Press Enter on any prompt to keep current value.\n")

        print("[Discovery]")
        raw_root = input(f"Automation root path (current: {self.automation_root_folder or '(not set)'}) [Enter keep]: ").strip()
        if raw_root:
            root_path = Path(raw_root)
            if root_path.exists() and root_path.is_dir():
                self.automation_root_folder = str(root_path)
                self.config["automation_root_folder"] = self.automation_root_folder
            else:
                self.print_red("Root path invalid; keeping previous value.")
        _ask(
            "Manifest filename",
            "automation_manifest_name",
            str,
            lambda v: len(v) > 0 and v.endswith(".json") and Path(v).name == v,
        )
        max_cases_raw = input(
            f"Max cases per run [1/5/10/all] (current: {self._read_max_cases_setting()}) [Enter keep]: "
        ).strip().lower()
        if max_cases_raw:
            if max_cases_raw in {"1", "5", "10", "all"}:
                self.config["automation_max_cases_per_run"] = max_cases_raw
            else:
                self.print_red("Invalid max cases value. Keeping previous value.")

        print("\n[Discovery Flags]")
        _ask_bool("Skip completed", "automation_skip_completed")
        _ask_bool("Skip if selfie exists", "automation_skip_if_selfie_exists")
        _ask_bool("Skip if video exists", "automation_skip_if_video_exists")
        _ask_bool("Allow reprocess", "automation_allow_reprocess")
        _ask_choice("Reprocess mode", "automation_reprocess_mode", ["skip", "overwrite", "increment"])

        print("\n[Front Expansion]")
        _ask_bool("Front expand enabled", "automation_front_expand_enabled")
        _ask_choice("Front expand provider", "automation_front_expand_provider", ["auto", "bfl", "fal"])
        _ask_choice("Front expand mode", "automation_front_expand_mode", ["document_3x4", "percent"])
        _ask("Front expand percent", "automation_front_expand_percent", int, lambda v: v >= 0)
        _ask("Front expand passes [1|2]", "automation_front_expand_passes", int, lambda v: v in {1, 2})
        _ask_bool("Front edge seal enabled", "automation_front_edge_seal_enabled")
        _ask("Front edge seal px", "automation_front_edge_seal_px", int, lambda v: v >= 0)
        _ask("Front output name", "automation_front_output_name", str, lambda v: len(v) > 0)

        print("\n[Portrait Extraction / Selfie / Similarity]")
        _ask_bool("Portrait extraction enabled", "automation_extract_enabled")
        _ask("Extract output name", "automation_extract_output_name", str, lambda v: len(v) > 0)
        _ask("Crop multiplier", "automation_crop_multiplier", float, lambda v: v > 0)
        _ask_bool("Selfie generation enabled", "automation_selfie_enabled")
        current_models = list(self.config.get("automation_selfie_models", []))
        print("Selfie model selection:")
        print("  1) Nano Banana 2 Edit")
        print("  2) GPT Image 2 Edit")
        print("  3) Both")
        print("  4) Custom endpoints")
        model_choice = input(f"Choose model set (current: {current_models}) [Enter keep]: ").strip()
        if model_choice == "1":
            self.config["automation_selfie_models"] = ["fal-ai/nano-banana-2/edit"]
        elif model_choice == "2":
            self.config["automation_selfie_models"] = ["openai/gpt-image-2/edit"]
        elif model_choice == "3":
            self.config["automation_selfie_models"] = ["fal-ai/nano-banana-2/edit", "openai/gpt-image-2/edit"]
        elif model_choice == "4":
            models_raw = input("Custom selfie model endpoints comma-separated: ").strip()
            models = [m.strip() for m in models_raw.split(",") if m.strip()]
            if models:
                self.config["automation_selfie_models"] = models
        _ask_choice("Selfie model policy", "automation_selfie_model_policy", ["first_pass", "all"])
        _ask("Max attempts per model", "automation_selfie_max_attempts_per_model", int, lambda v: v > 0)
        _ask("Similarity threshold", "automation_similarity_threshold", int, lambda v: 0 <= v <= 100)
        self._ensure_selfie_prompt_slots()
        current_slot = int(self.config.get("automation_selfie_prompt_slot", 1))
        current_prompt = str(self.config.get("automation_selfie_prompts", {}).get(str(current_slot), "") or "")
        print(f"Selfie prompt slot: {current_slot}")
        print(f"Current selfie prompt preview: {(current_prompt[:120] + '...') if len(current_prompt) > 120 else current_prompt}")
        slot_raw = input("Switch selfie prompt slot [1-10, Enter keep]: ").strip()
        if slot_raw.isdigit() and 1 <= int(slot_raw) <= 10:
            self.config["automation_selfie_prompt_slot"] = int(slot_raw)
            current_slot = int(slot_raw)
        edit_current = input("Edit active selfie prompt now? [y/N]: ").strip().lower()
        if edit_current in {"y", "yes"}:
            new_prompt = input("Enter selfie prompt text: ").strip()
            if new_prompt:
                self.config["automation_selfie_prompts"][str(current_slot)] = new_prompt
        reset_current = input("Reset active selfie slot to default prompt? [y/N]: ").strip().lower()
        if reset_current in {"y", "yes"}:
            self.config["automation_selfie_prompts"][str(current_slot)] = merge_automation_defaults({}).get("automation_selfie_prompts", {}).get("1", "")

        print("\n[Selfie Expansion / Video / Loop-Oldcam]")
        _ask_bool("Selfie expansion enabled", "automation_selfie_expand_enabled")
        _ask_choice("Selfie expand provider", "automation_selfie_expand_provider", ["auto", "bfl", "fal"])
        _ask_choice("Selfie expand mode", "automation_selfie_expand_mode", ["percent", "centered_3x4"])
        _ask("Selfie expand percent", "automation_selfie_expand_percent", int, lambda v: v >= 0)
        _ask_bool("Video generation enabled", "automation_video_enabled")
        _ask("Video aspect ratio", "automation_video_aspect_ratio", str, lambda v: ":" in v)
        _ask_bool("Use existing video prompt", "automation_video_use_existing_prompt")
        _ask_bool("Oldcam enabled", "automation_oldcam_enabled")
        _ask_choice("Oldcam version", "automation_oldcam_version", ["v7", "v8", "all"])
        _ask_bool("Oldcam required", "automation_oldcam_required")
        _ask_bool("Automation verbose logging", "automation_verbose_logging")
        _ask("Automation log max bytes", "automation_log_max_bytes", int, lambda v: v > 0)
        _ask("Automation log backup count", "automation_log_backup_count", int, lambda v: v >= 1)

        self.save_config()
        self.pause_review("Settings saved. Press Enter to continue...")

    def _edit_automation_settings_quick(self):
        """Backwards-compatible alias for older tests/callers."""
        self._edit_automation_settings()

    def _dry_run_automation(self):
        if not self.automation_root_folder:
            self.print_red("Set automation root folder first.")
            self.pause_continue("Press Enter to continue...")
            return
        root = Path(self.automation_root_folder)
        if not root.exists():
            self.print_red("Automation root path does not exist.")
            self.pause_continue("Press Enter to continue...")
            return
        records = discover_case_folders(root, self.config.get("automation_front_names", []))
        manifest_path = self._automation_manifest_path()
        had_manifest = manifest_path.exists()
        manifest = AutomationManifest.load_if_exists(manifest_path)
        manifest_warning = ""
        if manifest is None and had_manifest:
            manifest_warning = "Warning: existing manifest unreadable or schema-mismatched; dry-run ignoring manifest state."
        _rows, counts, runnable_cases = self._collect_case_snapshot(records, manifest)

        print("\nDry run summary")
        if manifest_warning:
            print(f"  {manifest_warning}")
        print(f"  discovered cases: {counts['discovered']}")
        print(f"  completed total: {counts['completed_total']}")
        print(f"  skipped complete: {counts['skipped_complete']}")
        print(f"  pending: {counts['pending']}")
        print(f"  failed/manual_review: {counts['failed'] + counts['manual_review']}")
        print(f"  will run this batch: {len(runnable_cases)}")
        print("  planned steps: front_expand -> extract -> selfie -> similarity -> selfie_expand -> video -> oldcam")
        self.pause_continue("\nPress Enter to continue...")

    def _run_resume_automation(self):
        if not self.automation_root_folder:
            self.print_red("Set automation root folder first.")
            self.pause_continue("Press Enter to continue...")
            return

        root = Path(self.automation_root_folder)
        if not root.exists():
            self.print_red("Automation root path does not exist.")
            self.pause_continue("Press Enter to continue...")
            return
        records = discover_case_folders(root, self.config.get("automation_front_names", []))
        if not records:
            self.print_yellow("No case folders found.")
            self.pause_continue("Press Enter to continue...")
            return

        try:
            manifest = AutomationManifest.create_or_load(
                manifest_path=self._automation_manifest_path(),
                root_dir=root,
                config_snapshot={k: v for k, v in self.config.items() if str(k).startswith("automation_")},
            )
        except Exception as exc:
            self.print_red(f"Failed to load manifest: {exc}")
            self.pause_continue("Press Enter to continue...")
            return
        rows, counts, runnable_cases = self._collect_case_snapshot(records, manifest)
        print("\nRun preview:")
        print(f"  discovered: {counts['discovered']}")
        print(f"  completed total: {counts['completed_total']}")
        print(f"  skipped complete: {counts['skipped_complete']}")
        print(f"  pending/runnable: {counts['pending']}")
        print(f"  will run this batch: {counts['will_run']}")
        print(f"  manual review: {counts['manual_review']}")
        print(f"  failed: {counts['failed']}")
        if not runnable_cases:
            self.print_yellow("No runnable cases for this batch.")
            self.pause_continue("Press Enter to continue...")
            return
        approve = input("Approve batch run? [y/N]: ").strip().lower()
        if approve not in {"y", "yes"}:
            print("Run cancelled.")
            self.pause_continue("Press Enter to continue...")
            return

        self.config["automation_root_folder"] = self.automation_root_folder
        runner = AutoPipelineRunner(
            config=self.config,
            automation_config=from_app_config(self.config),
            manifest=manifest,
            progress_cb=None,
        )
        issues = runner.validate_configuration()
        if issues:
            print("\nAutomation preflight failed:")
            for issue in issues:
                print(f"  - {issue}")
            self.pause_continue("\nPress Enter to continue...")
            return

        print("\nAutomation preflight:")
        print(f"  cases discovered: {len(records)}")
        print(f"  running this batch: {len(runnable_cases)}")
        print(f"  reprocess mode: {self.config.get('automation_reprocess_mode', 'skip')}")
        print(f"  skip selfie/video existing: {self.config.get('automation_skip_if_selfie_exists', True)} / {self.config.get('automation_skip_if_video_exists', True)}")
        for line in self._automation_status_lines():
            print(f"  {line}")
        selfie_slot, selfie_prompt, selfie_source = self._get_selected_selfie_prompt()
        prompt_preview = selfie_prompt if len(selfie_prompt) <= 160 else f"{selfie_prompt[:160]}..."
        print(f"  selfie prompt slot/source: {selfie_slot} / {selfie_source}")
        print(f"  selfie prompt preview: {prompt_preview}")
        stats, run_error = self._run_with_live_dashboard(runner, runnable_cases, manifest)
        if run_error:
            self.print_red(f"Automation run failed: {run_error}")
            self.pause_continue("\nPress Enter to continue...")
            return
        print("\nAutomation run complete.")
        print(f"  completed: {stats.get('completed', 0)}")
        print(f"  failed: {stats.get('failed', 0)}")
        print(f"  manual_review: {stats.get('manual_review', 0)}")
        print(f"  skipped: {stats.get('skipped', 0)}")
        table = Table(title="Per-Case Summary")
        table.add_column("Case")
        table.add_column("Status")
        table.add_column("Reason")
        for key, result in sorted(runner.last_case_results.items(), key=lambda item: item[0].lower()):
            table.add_row(key, str(result.get("status", "")), str(result.get("reason", "")))
        Console().print(table)
        self._write_automation_summary(manifest, runner.last_case_results, stats)
        self.pause_continue("\nPress Enter to continue...")

    def _run_with_live_dashboard(
        self,
        runner: AutoPipelineRunner,
        run_cases: List[Any],
        manifest: AutomationManifest,
    ) -> Tuple[Dict[str, int], Optional[str]]:
        state: Dict[str, Any] = {
            "message": "",
            "level": "info",
            "last_output": "-",
            "error_reason": "-",
            "similarity": "-",
            "current_case": "-",
            "current_step": "-",
        }
        run_result: Dict[str, Any] = {"stats": None, "error": None}

        def _cb(message: str, level: str = "info"):
            state["message"] = message
            state["level"] = level
            lowered = message.lower()
            if ".mp4" in lowered or "output:" in lowered:
                state["last_output"] = message

        runner.progress_cb = _cb

        def _worker():
            try:
                run_result["stats"] = runner.run(run_cases)
            except Exception as exc:
                run_result["error"] = str(exc)

        worker = threading.Thread(target=_worker, daemon=True)
        worker.start()

        total_cases = len(run_cases)
        steps = {
            "front_expand": "1 front expand",
            "extract_portrait": "2 extract portrait",
            "selfie_generate": "3 generate selfie",
            "similarity_gate": "4 similarity gate",
            "selfie_expand": "5 selfie expand",
            "video_generate": "6 kling video",
            "oldcam": "7 loop/oldcam",
        }

        with Live(console=Console(), refresh_per_second=4, transient=True) as live:
            while worker.is_alive():
                cases = manifest.data.get("cases", {})
                completed = 0
                failed = 0
                manual_review = 0
                skipped = 0
                active_step = "-"
                active_case = "-"
                for case_key in [case.relative_key for case in run_cases]:
                    status = str(cases.get(case_key, {}).get("status", "pending"))
                    if status == "complete":
                        completed += 1
                    elif status == "failed":
                        failed += 1
                    elif status == "manual_review":
                        manual_review += 1
                    elif status == "skipped":
                        skipped += 1
                    if status == "running":
                        active_case = case_key
                        step_name = str(cases.get(case_key, {}).get("active_step", "") or "")
                        active_step = steps.get(step_name, step_name or "-")
                        sim = cases.get(case_key, {}).get("steps", {}).get("similarity_gate", {}).get("meta", {}).get("score")
                        if sim is not None:
                            state["similarity"] = str(sim)
                done = completed + failed + manual_review + skipped
                state["current_case"] = active_case
                state["current_step"] = active_step
                remaining = max(0, len(run_cases) - done)
                if state["level"] in {"error", "warning"}:
                    state["error_reason"] = state["message"]
                progress_pct = int((done / total_cases) * 100) if total_cases else 100
                dashboard = "\n".join(
                    [
                        f"Progress: {done}/{total_cases} ({progress_pct}%)",
                        f"Current case: {state['current_case']}",
                        f"Current step: {state['current_step']}",
                        f"Similarity: {state['similarity']}",
                        f"Last output: {state['last_output']}",
                        f"Errors/manual review reason: {state['error_reason']}",
                        f"completed={completed} failed={failed} manual_review={manual_review} skipped={skipped} remaining={remaining}",
                        f"Event: [{state['level']}] {state['message']}",
                    ]
                )
                panel = Panel(dashboard, title="Automation Live Progress")
                live.update(panel)
                time.sleep(0.2)
            worker.join()

        return run_result.get("stats") or {"completed": 0, "failed": 0, "manual_review": 0, "skipped": 0}, run_result.get("error")

    def _write_automation_summary(
        self,
        manifest: AutomationManifest,
        last_case_results: Dict[str, Dict[str, str]],
        stats: Dict[str, int],
    ) -> None:
        summary_lines = [
            "# Automation Run Summary",
            "",
            f"- completed: {stats.get('completed', 0)}",
            f"- failed: {stats.get('failed', 0)}",
            f"- manual_review: {stats.get('manual_review', 0)}",
            f"- skipped: {stats.get('skipped', 0)}",
            f"- manifest: {manifest.manifest_path}",
            "",
            "## Per-case outputs",
        ]
        for case_key, result in sorted(last_case_results.items(), key=lambda item: item[0].lower()):
            case_entry = manifest.data.get("cases", {}).get(case_key, {})
            video_out = case_entry.get("steps", {}).get("video_generate", {}).get("output") or "-"
            oldcam_out = case_entry.get("steps", {}).get("oldcam", {}).get("output") or "-"
            summary_lines.append(
                f"- `{case_key}`: status={result.get('status', '')}, video={video_out}, oldcam={oldcam_out}, reason={result.get('reason', '')}"
            )
        summary_path = manifest.manifest_path.parent / "automation_run_summary.md"
        summary_path.write_text("\n".join(summary_lines) + "\n", encoding="utf-8")
        print(f"\nSummary written: {summary_path}")

    def run_automation_menu(self):
        while True:
            self._display_automation_menu()
            choice = input().strip().lower()
            if choice == "0":
                return
            if choice == "1":
                self._select_automation_root()
            elif choice == "2":
                self._scan_automation_cases()
            elif choice == "3":
                self._apply_recommended_automation_defaults()
            elif choice == "4":
                self._edit_automation_settings()
            elif choice == "5":
                self._dry_run_automation()
            elif choice == "6":
                self._run_resume_automation()
            elif choice == "7":
                manifest_path = self._automation_manifest_path()
                print(f"\nManifest path: {manifest_path if manifest_path else '(set root first)'}")
                self.pause_continue("\nPress Enter to continue...")
            else:
                self.print_red("Unknown option.")
                time.sleep(1)

    def _run_manual_kling_menu(self) -> Optional[str]:
        """Legacy Kling-first tools grouped under a manual menu."""
        while True:
            self.display_header()
            print("Manual Kling Video Tools")
            print("  1) Change output mode")
            print("  2) Edit/view Kling prompt")
            print("  3) Toggle verbose logging")
            print("  4) Select input folder (GUI)")
            print("  5) Select single image (GUI)")
            print("  6) Inspect model capabilities")
            print("  7) Change model")
            print("  8) Swap prompt slot")
            print("  e) Quick edit prompt")
            print("  0) Back")
            choice = input("\nSelect option: ").strip().lower()
            if choice == "0":
                return None
            if choice == "1":
                self.change_output_mode()
            elif choice == "2":
                self.edit_prompt()
            elif choice == "3":
                self.toggle_verbose_logging()
            elif choice == "4":
                selected_path = self.select_folder_gui()
                if selected_path:
                    return selected_path
            elif choice == "5":
                selected_path = self.select_file_gui()
                if selected_path:
                    return selected_path
            elif choice == "6":
                self.inspect_model_capabilities()
            elif choice == "7":
                self.select_model()
            elif choice == "8":
                self.swap_prompt_slot()
            elif choice == "e":
                self.quick_edit_prompt()
            else:
                self.print_red("Unknown option.")
                time.sleep(1)

    def count_genx_files(self, root_directory: str) -> int:
        """Count total genx files to process"""
        count = 0

        try:
            for folder_path in Path(root_directory).iterdir():
                if folder_path.is_dir():
                    for file_path in folder_path.iterdir():
                        if (
                            file_path.is_file()
                            and file_path.suffix.lower() in VALID_EXTENSIONS
                            and "genx" in file_path.name.lower()
                        ):
                            count += 1
        except Exception:
            pass
        return count

    def get_all_folders(self, root_directory: str):
        """Get all folders that contain genx images"""
        folders = []
        try:
            if self.get_genx_files_in_folder(root_directory):
                folders.append(root_directory)

            for folder_path in Path(root_directory).iterdir():
                if folder_path.is_dir():
                    if self.get_genx_files_in_folder(str(folder_path)):
                        folders.append(str(folder_path))
        except Exception:
            pass
        return folders

    def get_genx_files_in_folder(self, folder_path: str):
        """Get genx files in a specific folder"""
        genx_files = []

        try:
            for file_path in Path(folder_path).iterdir():
                if (
                    file_path.is_file()
                    and file_path.suffix.lower() in VALID_EXTENSIONS
                    and "genx" in file_path.name.lower()
                ):
                    genx_files.append(str(file_path))
        except Exception:
            pass
        return genx_files

    def start_processing(self, input_folder: str):
        """Start the video generation process with Rich UI"""
        from rich.console import Console
        from rich.progress import (
            Progress,
            SpinnerColumn,
            TextColumn,
            BarColumn,
            MofNCompleteColumn,
            TimeElapsedColumn,
        )
        from rich.panel import Panel
        from rich.text import Text
        from rich.table import Table
        from rich.align import Align
        from rich.spinner import Spinner
        from rich.live import Live
        from rich.console import Group

        console = Console(force_terminal=True, width=120)
        self.clear_screen()

        # Header panel - show configured model
        model_name = self.config.get("model_display_name", "Kling 2.1 Professional")
        header_text = Text()
        header_text.append(
            f"🚀 {model_name.upper()} BATCH VIDEO GENERATOR 🚀", style="bold cyan"
        )

        header_panel = Panel(
            Align.center(header_text), style="bright_blue", padding=(0, 1)
        )

        console.print(header_panel)

        # Create loading spinner
        def create_loading_spinner(message):
            return Spinner("dots", text=message, style="green bold")

        with Live(
            create_loading_spinner("Analyzing input..."),
            console=console,
            refresh_per_second=10,
        ) as loading_live:
            # Use fal.ai API with configurable model
            generator = FalAIKlingGenerator(
                api_key=self.config["falai_api_key"],
                verbose=self.verbose_logging,
                model_endpoint=self.config.get("current_model"),
                model_display_name=self.config.get("model_display_name"),
                prompt_slot=self.config.get("current_prompt_slot", 1),
            )

            # Gate negative_prompt by model capability (like GUI does)
            # This prevents API errors for models that don't support negative prompts
            model_endpoint = self.config.get("current_model", "")
            negative_prompt = self.get_current_negative_prompt()
            if negative_prompt:
                if not generator.schema_manager.supports_parameter(
                    model_endpoint, "negative_prompt"
                ):
                    negative_prompt = None
                    if self.verbose_logging:
                        print(
                            f"Note: {self.config.get('model_display_name', 'Selected model')} does not support negative prompts - ignoring"
                        )

            # Get use_source_folder setting early for consistent use throughout
            use_source = self.config.get("use_source_folder", True)

            input_path = Path(input_folder)
            if input_path.is_file():
                genx_count = 1
                folders = [
                    input_folder
                ]  # Treat file as single item list for processing logic
                total_files = 1
                loading_live.update(
                    create_loading_spinner(f"Prepared single file: {input_path.name}")
                )
            else:
                loading_live.update(
                    create_loading_spinner(
                        "Analyzing folders and checking for duplicates..."
                    )
                )
                genx_count = self.count_genx_files(input_folder)
                folders = self.get_all_folders(input_folder)

                loading_live.update(
                    create_loading_spinner("Filtering out duplicates...")
                )

                total_files = 0
                for folder in folders:
                    genx_images = generator.get_genx_image_files(
                        folder, use_source, self.config["output_folder"]
                    )
                    total_files += len(genx_images)

        # Clear screen
        console.clear()
        os.system("cls" if os.name == "nt" else "clear")
        time.sleep(0.1)

        console.print(header_panel)

        # Balance tracking removed - use fal.ai dashboard instead
        # Dashboard link shown in header

        try:
            if not self.verbose_logging:
                # Configuration panel
                config_table = Table.grid(padding=0)
                config_table.add_column(
                    style="cyan", justify="left", width=18
                )  # Increased width for longer labels
                config_table.add_column(style="white", justify="left")

                if Path(input_folder).is_file():
                    config_table.add_row(
                        "Input:", f"Single File: {Path(input_folder).name}"
                    )
                else:
                    config_table.add_row("Files Amt:", f"{total_files} GenX files")

                model_name = self.config.get(
                    "model_display_name", "Kling 2.1 Professional"
                )
                duration = self.config.get("video_duration", 10)
                price = self.fetch_model_pricing(self.config.get("current_model", ""))
                price_str = f"${price:.2f}/sec" if price else "Check fal.ai"

                config_table.add_row("Provider:", "fal.ai API")
                config_table.add_row("Model:", model_name)
                config_table.add_row("Duration:", f"{duration} seconds")
                config_table.add_row("Cost:", price_str)
                # Show output mode
                use_source = self.config.get("use_source_folder", True)
                if use_source:
                    config_table.add_row("Output:", "📂 Same folder as source images")
                else:
                    config_table.add_row("Output folder:", self.config["output_folder"])
                config_table.add_row("Verbose mode:", "Hidden")

                config_panel = Panel(
                    config_table,
                    title="Configuration",
                    border_style="green",
                    title_align="left",
                    padding=(0, 1),
                )
                console.print(config_panel)
                print()  # Blank line after panel

                # Progress bar
                with Progress(
                    SpinnerColumn(style="bright_cyan"),
                    TextColumn("[progress.description]{task.description}"),
                    BarColumn(bar_width=None),
                    MofNCompleteColumn(),
                    TextColumn("•"),
                    TimeElapsedColumn(),
                    console=console,
                ) as progress:
                    if Path(input_folder).is_file():
                        main_task = progress.add_task(
                            "📊 [cyan]0% complete[/cyan] • 🎬 Processing Single File... 🚀",
                            total=total_files,
                        )
                    else:
                        main_task = progress.add_task(
                            "📊 [cyan]0% complete[/cyan] • 🎬 Processing GenX files... 🚀",
                            total=total_files,
                        )

                    active_generations = []  # Track currently processing files
                    recent_status = ""
                    processed = 0
                    videos_completed = 0  # Track successful completions for cost
                    all_files = []  # Track ALL files for "Next" display

                    # Collect all files upfront for Next display
                    if Path(input_folder).is_file():
                        all_files.append(Path(input_folder).stem)
                    else:
                        for folder in folders:
                            genx_images = generator.get_genx_image_files(
                                folder, use_source, self.config["output_folder"]
                            )
                            for img in genx_images:
                                folder_name = Path(folder).name
                                all_files.append(folder_name)

                    def create_colorful_spinners():
                        activity_text = Text()
                        activity_text.append("🔥 Activity: ", style="bright_green bold")

                        if active_generations:
                            # Show only 2 names to avoid overflow
                            activity_text.append(
                                f"{len(active_generations)} concurrent • ",
                                style="bright_cyan",
                            )
                            display_names = [
                                Path(f).stem[:15] for f in active_generations[:2]
                            ]  # Only 2 names, shorter
                            activity_text.append(
                                ", ".join(display_names), style="white"
                            )
                            if len(active_generations) > 2:
                                activity_text.append(
                                    f" (+{len(active_generations) - 2} more)",
                                    style="bright_yellow",
                                )
                        elif recent_status:
                            if "Completed:" in recent_status:
                                filename = recent_status.replace("Completed: ", "")
                                activity_text.append(
                                    "✅ Completed: ", style="bright_green"
                                )
                                activity_text.append(
                                    filename[:30], style="white"
                                )  # Limit length
                            elif "Failed:" in recent_status:
                                filename = recent_status.replace("Failed: ", "")
                                activity_text.append("❌ Failed: ", style="bright_red")
                                activity_text.append(
                                    filename[:30], style="white"
                                )  # Limit length
                            else:
                                activity_text.append(recent_status, style="bright_cyan")
                        else:
                            activity_text.append("Initializing...", style="bright_cyan")
                        activity_spinner = Spinner(
                            "dots", text=activity_text, style="bright_green"
                        )

                        # Action spinner (balance tracking removed - check fal.ai dashboard)
                        action_text = Text()
                        action_text.append("⚡ Action: ", style="bright_blue bold")
                        action_text.append(
                            "💰 Balance: fal.ai/dashboard • ", style="bright_yellow"
                        )
                        action_text.append(
                            "Monitoring for Interrupts...", style="bright_white"
                        )
                        action_spinner = Spinner(
                            "dots", text=action_text, style="bright_blue"
                        )

                        next_text = Text()
                        next_text.append("🔮 Next: ", style="bright_magenta bold")

                        # Calculate remaining (not yet started)
                        total_in_progress = processed + len(active_generations)
                        remaining_to_start = len(all_files) - total_in_progress

                        # Show next folder names (not yet processed or in progress)
                        if remaining_to_start > 0:
                            upcoming = all_files[
                                total_in_progress : total_in_progress + 3
                            ]  # Next 3 folders

                            # Get unique folder names
                            unique_folders = []
                            seen = set()
                            for folder_name in upcoming:
                                if folder_name not in seen:
                                    unique_folders.append(folder_name)
                                    seen.add(folder_name)

                            if unique_folders:
                                display = ", ".join(unique_folders[:3])
                                if remaining_to_start > 3:
                                    display += f" (+{remaining_to_start - 3} more)"
                                next_text.append(display, style="bright_yellow")
                            else:
                                next_text.append(
                                    f"{remaining_to_start} videos remaining in queue",
                                    style="bright_yellow",
                                )
                        else:
                            next_text.append(
                                "All generations complete", style="bright_green"
                            )
                        next_spinner = Spinner(
                            "dots", text=next_text, style="bright_magenta"
                        )

                        return Group(activity_spinner, action_spinner, next_spinner)

                    with Live(
                        create_colorful_spinners(),
                        console=console,
                        refresh_per_second=10,
                    ) as live:

                        def update_progress(completed, total, new_status):
                            nonlocal recent_status, processed, active_generations
                            recent_status = new_status
                            processed = completed

                            # Update active generations list
                            if "Generating:" in new_status:
                                filename = new_status.replace("Generating: ", "")
                                if filename not in active_generations:
                                    active_generations.append(filename)
                            elif "Completed:" in new_status or "Failed:" in new_status:
                                filename = new_status.replace(
                                    "Completed: ", ""
                                ).replace("Failed: ", "")
                                if filename in active_generations:
                                    active_generations.remove(filename)

                            current_pct = (
                                int((completed / total) * 100) if total > 0 else 0
                            )
                            progress.update(
                                main_task,
                                completed=completed,
                                description=f"📊 [cyan]{current_pct}% complete[/cyan] • 🚀",
                            )
                            live.update(create_colorful_spinners())

                        # Use concurrent processing with 5 workers (Kling API max)
                        use_source = self.config.get("use_source_folder", True)
                        generator.process_all_images_concurrent(
                            target_directory=input_folder,
                            output_directory=self.config["output_folder"],
                            max_workers=5,
                            custom_prompt=self.get_current_prompt(),
                            negative_prompt=negative_prompt,  # Uses gated value from line 1525
                            progress_callback=update_progress,
                            use_source_folder=use_source,
                            duration=self.config.get("video_duration", 10),
                            aspect_ratio=self.config.get("aspect_ratio", "9:16"),
                            resolution=self.config.get("resolution", "720p"),
                            seed=self.config.get("seed", -1),
                            camera_fixed=self.config.get("camera_fixed", False),
                            generate_audio=self.config.get("generate_audio", False),
                        )

                        if total_files > 0:
                            progress.update(
                                main_task,
                                completed=total_files,
                                description="📊 [cyan]100% complete[/cyan] • 🎉 All files processed!",
                            )
                            recent_status = "Processing complete!"
                            active_generations.clear()
                            live.update(create_colorful_spinners())

                        time.sleep(2)

            else:
                # Verbose processing with concurrent execution
                print("Processing started with verbose logging...")
                print("Using 5 concurrent workers for faster processing...")
                use_source = self.config.get("use_source_folder", True)
                if use_source:
                    print("Output mode: Videos saved alongside source images")
                else:
                    print(f"Output folder: {self.config['output_folder']}")
                print("All detailed logs will be displayed below:")
                print()

                generator.process_all_images_concurrent(
                    target_directory=input_folder,
                    output_directory=self.config["output_folder"],
                    max_workers=5,
                    custom_prompt=self.get_current_prompt(),
                    negative_prompt=negative_prompt,  # Uses gated value from line 1525
                    use_source_folder=use_source,
                    duration=self.config.get("video_duration", 10),
                    aspect_ratio=self.config.get("aspect_ratio", "9:16"),
                    resolution=self.config.get("resolution", "720p"),
                    seed=self.config.get("seed", -1),
                    camera_fixed=self.config.get("camera_fixed", False),
                    generate_audio=self.config.get("generate_audio", False),
                )

        except Exception as e:
            print(f"\nError during processing: {e}")
            if self.verbose_logging:
                import traceback

                print(f"{traceback.format_exc()}")

        print("\nProcessing complete!")
        use_source = self.config.get("use_source_folder", True)
        if use_source:
            print("Videos saved alongside source images in their respective folders")
        else:
            print(f"Check your videos in: {self.config['output_folder']}")
        self.pause_review("\nPress Enter to return to main menu...")

    def run(self):
        """Main application loop"""
        while True:
            input_folder = self.run_configuration_menu()
            if input_folder:
                self.start_processing(input_folder)

    def run_auto_mode(self):
        """Direct launch into automation flow."""
        self.run_automation_menu()

    def run_manual_video_mode(self):
        """Direct launch into legacy manual Kling tools."""
        while True:
            selected = self._run_manual_kling_menu()
            if selected:
                self.start_processing(selected)
            else:
                return


def main(argv=None):
    """Entry point"""
    try:
        crash_log_path = _enable_cli_crash_capture()
        if crash_log_path:
            print(f"Native crash capture enabled: {crash_log_path}")

        parser = argparse.ArgumentParser(add_help=True)
        parser.add_argument("--auto", action="store_true", help="Launch directly into automation workflow")
        parser.add_argument("--manual-video", action="store_true", help="Launch legacy manual Kling tools")
        parser.add_argument("--gui", action="store_true", help="Launch GUI manual lab directly")
        parser.add_argument("--verbose-startup", action="store_true", help="Show full startup dependency diagnostics")
        parser.add_argument("--legacy-pauses", action="store_true", help="Restore legacy 'Press Enter to continue' pauses")
        args = parser.parse_args(argv)
        verbose_startup = args.verbose_startup or os.getenv("KLING_VERBOSE_STARTUP", "0") == "1"
        legacy_pauses = args.legacy_pauses or os.getenv("KLING_LEGACY_PAUSES", "0") == "1"

        if os.name == "nt":
            os.system("color")

        # Optional Python-side dependency check for direct python launches.
        if os.getenv("KLING_SKIP_PY_STARTUP_DEP_CHECK", "0") != "1":
            try:
                from dependency_checker import run_dependency_check

                if verbose_startup:
                    ok = run_dependency_check(auto_mode=True, enforce_all=True, install_external_tools=False)
                else:
                    print("Checking startup dependencies...")
                    dep_buffer = io.StringIO()
                    with contextlib.redirect_stdout(dep_buffer), contextlib.redirect_stderr(dep_buffer):
                        ok = run_dependency_check(auto_mode=True, enforce_all=True, install_external_tools=False)
                    if ok:
                        print("Startup dependency check: OK")
                    else:
                        print("Startup dependency check failed. Details below.")
                        print(dep_buffer.getvalue())
                if not ok:
                    sys.exit(1)
            except Exception as e:
                print(f"Warning: Startup dependency check failed: {e}")

        if sys.platform == "win32":
            try:
                import codecs

                sys.stdout = codecs.getwriter("utf-8")(sys.stdout.buffer, "strict")
                sys.stderr = codecs.getwriter("utf-8")(sys.stderr.buffer, "strict")
            except:
                pass

        app = KlingAutomationUI(legacy_pauses=legacy_pauses)
        if args.gui:
            app.launch_gui()
            return
        if args.auto:
            app.run_auto_mode()
            return
        if args.manual_video:
            app.run_manual_video_mode()
            return
        app.run()
    except KeyboardInterrupt:
        print("\n\nGoodbye!")
        sys.exit(0)
    except Exception as e:
        logging.error("Fatal error: %s", e)
        logging.error("Fatal traceback:\n%s", traceback.format_exc())
        print(f"Fatal error: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
