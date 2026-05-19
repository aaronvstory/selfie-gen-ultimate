import os
import os
import time
from pathlib import Path
from typing import List, Optional, Callable, Dict, Any
import requests
import logging
from PIL import Image, ImageOps
import io
import base64
from concurrent.futures import ThreadPoolExecutor, as_completed
import threading
import traceback
from datetime import datetime

from model_schema_manager import ModelSchemaManager
from model_metadata import get_prompt_limit, get_model_capabilities
from path_utils import sanitize_stem

# Setup logging
logging.basicConfig(
    level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger(__name__)


class FalAIKlingGenerator:
    def __init__(
        self,
        api_key: str,
        verbose: bool = True,
        model_endpoint: Optional[str] = None,
        model_display_name: Optional[str] = None,
        prompt_slot: int = 1,
        freeimage_key: Optional[str] = None,
    ):
        self.api_key = api_key
        self.verbose = verbose

        # Initialize schema manager for dynamic parameter detection
        self.schema_manager = ModelSchemaManager(api_key)

        # Store model display name for logging
        self.model_display_name = model_display_name or "Kling 2.1 Professional"

        # Store prompt slot for filename generation
        self.prompt_slot = prompt_slot

        # Configurable model endpoint - defaults to Kling 2.1 Professional
        if model_endpoint:
            self.base_url = f"https://queue.fal.run/{model_endpoint}"
            self.model_endpoint = model_endpoint
        else:
            self.base_url = (
                "https://queue.fal.run/fal-ai/kling-video/v2.1/pro/image-to-video"
            )
            self.model_endpoint = "fal-ai/kling-video/v2.1/pro/image-to-video"

        # Freeimage.host API key (GUI config overrides env, guest key as final fallback)
        configured_freeimage_key = (freeimage_key or "").strip()
        self.freeimage_key = (
            configured_freeimage_key
            or os.getenv("FREEIMAGE_API_KEY", "").strip()
            or "6d207e02198a847aa98d0a2a901485a5"
        )

        # Default prompt for head movement
        self.default_prompt = (
            "Turn head to the right slowly then all the way to the left slowly then to the right slowly, and to the left slowly. "
            "Make sure the body is kept still while doing this - ONLY turn THE HEAD NOT THE BODY. The subject should perform smooth, "
            "natural head movements with no body movement whatsoever. Keep shoulders, neck, and torso completely stationary. "
            "Head movements should be slow, deliberate, and realistic. Eyes can follow natural movement patterns. "
            "Maintain neutral facial expression throughout. Camera remains fixed and stationary. "
            "Generate in maximum resolution and professional quality with no blur, pixelation, or quality degradation."
        )

        # Downloads folder
        self.downloads_folder = os.path.join(os.path.expanduser("~"), "Downloads")

        # Progress callback for GUI verbose mode
        self._progress_callback = None

        # Last generation error for UI/reporting
        self.last_error_message: Optional[str] = None

    def set_progress_callback(self, callback):
        """Set a callback for progress updates (used by GUI verbose mode)."""
        self._progress_callback = callback

    def _set_last_error(self, message: str):
        """Store the last generation error for queue/UI reporting."""
        self.last_error_message = message

    @staticmethod
    def _extract_response_error_detail(response: requests.Response) -> str:
        """Extract readable detail text from fal API error response."""
        try:
            data = response.json()
            if isinstance(data, dict):
                detail = data.get("detail")
                if isinstance(detail, list):
                    return "; ".join(str(item.get("msg", item)) for item in detail)
                if detail:
                    return str(detail)
                if data.get("error"):
                    return str(data["error"])
                if data.get("message"):
                    return str(data["message"])
            if data:
                return str(data)
        except Exception:
            pass
        return (response.text or "").strip()

    @staticmethod
    def _is_balance_lock_error(message: str) -> bool:
        """True when backend message indicates exhausted/locked account state."""
        lowered = str(message or "").lower()
        markers = (
            "exhausted balance",
            "user is locked",
            "insufficient credits",
            "insufficient balance",
            "quota",
            "payment required",
        )
        return any(marker in lowered for marker in markers)

    @staticmethod
    def _extract_video_url(data: dict) -> Optional[str]:
        """Extract video URL from all known fal.ai response structures.

        Checks: video.url, video[0].url, output.video.url, data.video.url,
        response.video.url — handles both dict and list video fields.
        """
        if not isinstance(data, dict):
            return None

        # Helper to pull .url from a video field that may be dict or list
        def _url_from_video(video_field):
            if isinstance(video_field, dict):
                return video_field.get("url")
            if isinstance(video_field, list) and video_field:
                first = video_field[0]
                if isinstance(first, dict):
                    return first.get("url")
                if isinstance(first, str):
                    return first
            if isinstance(video_field, str):
                return video_field
            return None

        # Direct: video.url / video[0].url
        if "video" in data:
            url = _url_from_video(data["video"])
            if url:
                return url

        # Nested wrappers: output.video, data.video, response.video
        for wrapper_key in ("output", "data", "response"):
            wrapper = data.get(wrapper_key)
            if isinstance(wrapper, dict) and "video" in wrapper:
                url = _url_from_video(wrapper["video"])
                if url:
                    return url

        return None

    def update_model(self, model_endpoint: str, model_display_name: str):
        """Update the model endpoint and display name.

        Called when user changes model in GUI settings.
        """
        self.model_display_name = model_display_name or "Kling 2.1 Professional"
        if model_endpoint:
            self.base_url = f"https://queue.fal.run/{model_endpoint}"
            self.model_endpoint = model_endpoint
        else:
            self.base_url = (
                "https://queue.fal.run/fal-ai/kling-video/v2.1/pro/image-to-video"
            )
            self.model_endpoint = "fal-ai/kling-video/v2.1/pro/image-to-video"

    def update_freeimage_key(self, freeimage_key: Optional[str]):
        """Update Freeimage API key from UI config (fallback to env, then guest key)."""
        configured_freeimage_key = (freeimage_key or "").strip()
        self.freeimage_key = (
            configured_freeimage_key
            or os.getenv("FREEIMAGE_API_KEY", "").strip()
            or "6d207e02198a847aa98d0a2a901485a5"
        )

    def update_prompt_slot(self, slot: int):
        """Update the current prompt slot.

        Called when user changes prompt slot in GUI settings.
        """
        self.prompt_slot = slot

    def get_model_short_name(self) -> str:
        """Get a short identifier for the current model based on the endpoint.

        Used in video filenames to indicate which model was used.
        Each model variant (pro/standard/master) gets a distinct name.
        Examples:
            fal-ai/kling-video/v2.5-turbo/pro/image-to-video -> k25tPro
            fal-ai/kling-video/v2.5-turbo/standard/image-to-video -> k25tStd
            fal-ai/kling-video/v3/pro/image-to-video -> k30pro
        """
        endpoint = self.model_endpoint.lower()

        # Tier suffix helper — appends Pro/Std/Master based on endpoint path
        def _tier(endpoint: str) -> str:
            if "/master/" in endpoint or endpoint.endswith("/master"):
                return "Master"
            if "/standard/" in endpoint or endpoint.endswith("/standard"):
                return "Std"
            return "Pro"  # default for /pro/ or unspecified

        # Common model mappings
        if "kling" in endpoint:
            # Extract version info — ordered most-specific to least-specific
            if "/v3/" in endpoint or "/v3-" in endpoint or endpoint.endswith("/v3"):
                return "k30pro" if "pro" in endpoint else "k30std"
            elif (
                "v2.5-turbo" in endpoint
                or "v2.5/turbo" in endpoint
                or "v2.5turbo" in endpoint
            ):
                return f"k25t{_tier(endpoint)}"
            elif "v2.6" in endpoint:
                return f"k26{_tier(endpoint).lower()}"
            elif "v2.5" in endpoint:
                return "k25"
            elif "v2.1" in endpoint:
                return f"k21{_tier(endpoint).lower()}"
            elif "v2/" in endpoint or endpoint.endswith("/v2"):
                return f"k20{_tier(endpoint).lower()}"
            elif "v1.6" in endpoint:
                return f"k16{_tier(endpoint).lower()}"
            elif "v1.5" in endpoint:
                return f"k15{_tier(endpoint).lower()}"
            elif "o1" in endpoint:
                return "kO1"
            else:
                return "kling"
        elif "wan" in endpoint:
            if "25" in endpoint:
                return "wan25"
            return "wan"
        elif "veo3" in endpoint:
            return "veo3"
        elif "veo" in endpoint:
            return "veo"
        elif "ovi" in endpoint:
            return "ovi"
        elif "ltx" in endpoint:
            return "ltx2"
        elif "pixverse" in endpoint:
            if "v5" in endpoint:
                return "pix5"
            return "pixverse"
        elif "hunyuan" in endpoint:
            return "hunyuan"
        elif "minimax" in endpoint:
            return "minimax"
        else:
            # Fallback: extract last meaningful segment
            parts = endpoint.replace("/image-to-video", "").split("/")
            for part in reversed(parts):
                if part and part != "fal-ai":
                    # Clean up and truncate
                    clean = part.replace("-", "").replace("_", "")[:8]
                    return clean
            return "video"

    def sanitize_prompt_description(self, prompt_text: str, max_length: int = 35) -> str:
        """Extract a clean filename-safe description from prompt text.

        Takes first 3-5 meaningful words, removes special chars,
        converts spaces to underscores.

        Args:
            prompt_text: The full prompt text to extract from
            max_length: Maximum length for the resulting description (default 35)

        Returns:
            Cleaned filename-safe string like "Generate_lifelike_video_animation"

        Examples:
            "Generate a lifelike video animation..." → "Generate_lifelike_video_animation"
            "Turn head to the right slowly..." → "Turn_head_right_slowly"
            "Framing: medium shot, head and upper torso visible" → "medium_shot_head_upper_torso"
        """
        if not prompt_text:
            return "prompt"

        # Take first sentence or first 100 chars
        text = prompt_text[:100].split('.')[0].strip()

        # Remove common formatting prefixes
        prefixes_to_remove = [
            "Framing:", "Camera:", "Subject:", "Action:", "Movement:",
            "Generate", "Create", "Make", "The subject should", "This should"
        ]
        for prefix in prefixes_to_remove:
            if text.startswith(prefix):
                text = text[len(prefix):].strip()

        # Split into words
        words = text.split()

        # Filter out common filler words
        filler_words = {'a', 'an', 'the', 'with', 'from', 'to', 'and', 'or', 'of', 'in', 'on', 'at', 'by'}
        meaningful_words = [w for w in words if w.lower() not in filler_words]

        # Take first 3-5 meaningful words (or all if less)
        selected_words = meaningful_words[:5] if len(meaningful_words) >= 3 else words[:5]

        # Join with underscores
        description = '_'.join(selected_words)

        # Remove special characters except underscores and dashes
        description = ''.join(c if c.isalnum() or c in ('_', '-') else '' for c in description)

        # Truncate to max length
        if len(description) > max_length:
            description = description[:max_length].rstrip('_-')

        return description if description else "prompt"

    def get_output_filename(
        self,
        image_stem: str,
        output_folder: Optional[str] = None,
        *_legacy_args,
        **_ignored,
    ) -> str:
        """Generate output filename: {stem}_{model_short}_{index}.mp4

        Index is determined by scanning output_folder for existing files with the
        same stem and model so that each generation gets a unique sequential number.

        Args:
            image_stem: Image filename without extension (e.g. 'selfie')
            output_folder: Folder to scan for existing files (index starts at 1 if None)

        Returns:
            Filename like: selfie_k30pro_1.mp4, selfie_k30pro_2.mp4, …
        """
        import glob as _glob

        # Backward compatibility: older callers passed config dict/timestamp
        # positionally as args #2/#3. Ignore those legacy values.
        if isinstance(output_folder, dict):
            output_folder = None

        model_short = self.get_model_short_name()
        safe_image_stem = sanitize_stem(image_stem, default="image")
        try:
            slot = max(1, int(getattr(self, "prompt_slot", 1)))
        except (TypeError, ValueError):
            slot = 1
        prefix = f"{safe_image_stem}_{model_short}_p{slot}_"

        max_index = 0
        if output_folder:
            pattern = str(Path(output_folder) / f"{prefix}*.mp4")
            for fpath in _glob.glob(pattern):
                remainder = Path(fpath).stem[len(prefix):]
                if remainder.isdigit():
                    max_index = max(max_index, int(remainder))

        return f"{prefix}{max_index + 1}.mp4"

    def _report_progress(self, message: str, level: str = "info"):
        """Report progress to callback if set."""
        if self._progress_callback:
            self._progress_callback(message, level)

    def upload_to_freeimage(self, image_path: str) -> Optional[str]:
        """Upload image via freeimage.host."""
        from fal_utils import upload_reference_image
        try:
            image_url, _, provider = upload_reference_image(
                image_path=image_path,
                fal_api_key=self.api_key,
                progress_cb=self._report_progress,
                freeimage_api_key=self.freeimage_key,
            )
            if image_url and provider:
                self._report_progress(f"Reference upload provider: {provider}", "upload")
            return image_url

        except Exception as e:
            logger.error(f"Upload error: {e}")
            return None

    def check_duplicate_exists(
        self, image_path: str, output_folder: Optional[str] = None
    ) -> bool:
        """Check if any video already exists for this image+model combination."""
        import glob as _glob
        try:
            char_name = Path(image_path).stem
            target_folder = output_folder if output_folder else self.downloads_folder
            model_short = self.get_model_short_name()
            try:
                slot = max(1, int(getattr(self, "prompt_slot", 1)))
            except (TypeError, ValueError):
                slot = 1

            slot_pattern = str(
                Path(target_folder) / f"{char_name}_{model_short}_p{slot}_*.mp4"
            )
            slot_matches = _glob.glob(slot_pattern)
            if slot_matches:
                return True

            # Backward compatibility: treat legacy (pre-slot) underscore files as slot 1 duplicates.
            if slot == 1:
                legacy_pattern = str(Path(target_folder) / f"{char_name}_{model_short}_*.mp4")
                legacy_matches = [
                    path
                    for path in _glob.glob(legacy_pattern)
                    if f"_{model_short}_p" not in Path(path).stem
                ]
                if legacy_matches:
                    return True

            # Backward compatibility: check older hyphenated-format filenames (e.g.
            # "Selfie-Kling_V2.5_Turbo_Pro-*-p3-2026-01-17.mp4") for any slot.
            # Pattern anchors on "-Kling_" immediately after the stem to prevent
            # false positives from prefixed filenames and non-Kling legacy files.
            hyphen_pattern = str(Path(target_folder) / f"{_glob.escape(char_name)}-Kling_*-p{slot}-*.mp4")
            if _glob.glob(hyphen_pattern):
                return True

            return False
        except Exception:
            return False

    def get_genx_image_files(
        self,
        folder_path: str,
        use_source_folder: bool = False,
        fallback_output_folder: Optional[str] = None,
    ) -> List[str]:
        """Get GenX images excluding duplicates

        Args:
            folder_path: Path to scan for images
            use_source_folder: If True, check for duplicates in source folder (image's parent)
            fallback_output_folder: Output folder to check for duplicates when use_source_folder=False
        """
        image_extensions = {
            ".jpg",
            ".jpeg",
            ".png",
            ".bmp",
            ".gif",
            ".webp",
            ".tiff",
            ".tif",
        }
        genx_images = []

        try:
            for file_path in Path(folder_path).iterdir():
                if (
                    file_path.is_file()
                    and file_path.suffix.lower() in image_extensions
                    and "genx" in file_path.name.lower()
                ):
                    # Determine output folder for duplicate check
                    if use_source_folder:
                        check_folder = str(file_path.parent)
                    else:
                        check_folder = fallback_output_folder

                    if not self.check_duplicate_exists(str(file_path), check_folder):
                        genx_images.append(str(file_path))
                    elif self.verbose:
                        logger.info(f"Skipping duplicate: {file_path.name}")
        except Exception as e:
            if self.verbose:
                logger.error(f"Error getting GenX files: {e}")

        return genx_images

    def create_kling_generation(
        self,
        character_image_path: str,
        output_folder: Optional[str] = None,
        custom_prompt: Optional[str] = None,
        negative_prompt: Optional[str] = None,
        use_source_folder: bool = False,
        skip_duplicate_check: bool = False,
        duration: int = 10,
        aspect_ratio: str = "9:16",
        resolution: str = "720p",
        seed: int = -1,
        camera_fixed: bool = False,
        generate_audio: bool = False,
        end_image_url: Optional[str] = None,
        cfg_scale: Optional[float] = None,
        lock_end_frame: bool = False,
        config: Optional[Dict[str, Any]] = None,
        timestamp: Optional[datetime] = None,
    ) -> Optional[str]:
        """Create Kling video via fal.ai

        Args:
            character_image_path: Path to source image
            output_folder: Fallback output folder (used when use_source_folder=False)
            custom_prompt: Custom generation prompt
            negative_prompt: Negative prompt for content to avoid (model-dependent)
            use_source_folder: If True, save video in same folder as source image
            skip_duplicate_check: If True, skip duplicate detection (for reprocessing)
            duration: Video duration in seconds (default 10, some models support 5 or 6)
            aspect_ratio: Aspect ratio string (21:9, 16:9, 4:3, 1:1, 3:4, 9:16)
            resolution: Resolution string (480p, 720p)
            seed: Random seed (-1 for random)
            camera_fixed: Whether camera should be fixed/stationary
            generate_audio: Whether to generate audio
            end_image_url: Optional explicit last-frame image URL (model-dependent)
            cfg_scale: Optional prompt-adherence strength (model-dependent, 0-1)
            lock_end_frame: If True and the model has an end-frame param,
                set it to the SAME url as the start image so the clip
                mechanically returns to the opening pose
            config: Config dict containing prompt_titles and saved_prompts (for enhanced filenames)
            timestamp: Generation start time for consistent filenames (defaults to now)
        """
        self.last_error_message = None

        try:
            # Determine actual output folder — prefer gen-videos/ subfolder
            if use_source_folder:
                from path_utils import get_gen_videos_folder
                actual_output_folder = get_gen_videos_folder(character_image_path)
                os.makedirs(actual_output_folder, exist_ok=True)
            elif output_folder is not None:
                actual_output_folder = output_folder
            else:
                actual_output_folder = self.downloads_folder

            if not skip_duplicate_check and self.check_duplicate_exists(
                character_image_path, actual_output_folder
            ):
                if self.verbose:
                    logger.info(
                        f"Skipping {Path(character_image_path).name} - already exists"
                    )
                return None

            # Upload image
            image_url = self.upload_to_freeimage(character_image_path)
            if not image_url:
                error_msg = "Failed to upload image"
                self._set_last_error(error_msg)
                logger.error(error_msg)
                return None

            # Prepare prompt
            prompt = custom_prompt if custom_prompt else self.default_prompt

            # Warn if prompt exceeds model-specific API limit (never truncate silently)
            prompt_limit = get_prompt_limit(self.model_endpoint)
            if prompt_limit and len(prompt) > prompt_limit:
                self._report_progress(
                    f"⚠ Prompt is {len(prompt)} chars — model limit is {prompt_limit}. "
                    f"API may reject this request.", "warning"
                )

            # fal.ai API request
            headers = {
                "Authorization": f"Key {self.api_key}",
                "Content-Type": "application/json",
            }

            # Status check headers (no Content-Type for GET requests)
            status_headers = {"Authorization": f"Key {self.api_key}"}

            # Per-model API capability flags (single source of truth in
            # models.json via model_metadata.get_model_capabilities — the
            # GUI reads the SAME flags so payload + UI never diverge).
            caps = get_model_capabilities(self.model_endpoint)
            start_param = caps["start_image_param"] or "image_url"
            end_param = caps["end_image_param"]  # str or None

            # Validate the start image URL is present BEFORE building the
            # payload — a blank/missing url silently produces a useless
            # request the API rejects opaquely much later.
            if not image_url or not str(image_url).strip():
                error_msg = "No start image URL — cannot submit generation"
                self._set_last_error(error_msg)
                logger.error(error_msg)
                return None

            # Build full payload with ALL possible parameters. The start
            # image goes under the model's OWN param name (Kling v3 uses
            # start_image_url; v2.5-turbo / o3 / seedance use image_url).
            payload_full: Dict[str, Any] = {
                start_param: image_url,
                "prompt": prompt,
                "duration": duration,  # fal.ai expects integer, not string
            }

            if aspect_ratio:
                payload_full["aspect_ratio"] = aspect_ratio

            # Add optional parameters if they differ from defaults or are explicitly set
            if resolution:
                payload_full["resolution"] = resolution

            if seed >= 0:
                payload_full["seed"] = seed

            if camera_fixed:
                payload_full["camera_fixed"] = True

            if generate_audio:
                payload_full["generate_audio"] = True

            # End-frame: an explicit end_image_url wins; otherwise, when
            # lock_end_frame is requested AND the model exposes an
            # end-frame param, point it at the SAME url as the start so
            # the clip mechanically returns to the opening pose. Use the
            # model's OWN end param name (tail_image_url for v2.5-turbo
            # pro; end_image_url for v3 / o3 / seedance). Models with no
            # end-frame support (end_param is None) get nothing.
            if end_param:
                if end_image_url:
                    payload_full[end_param] = end_image_url
                elif lock_end_frame:
                    payload_full[end_param] = image_url

            # negative_prompt / cfg_scale only when the model supports
            # them (o3 / seedance dropped both in 2026) AND a value was
            # provided. schema_manager.validate_parameters below is a
            # second defensive filter against the live fal.ai schema.
            if negative_prompt and caps["supports_negative_prompt"]:
                payload_full["negative_prompt"] = negative_prompt

            if cfg_scale is not None and caps["supports_cfg_scale"]:
                payload_full["cfg_scale"] = cfg_scale

            # Validate and filter parameters based on model schema (dynamic detection)
            payload = self.schema_manager.validate_parameters(
                self.model_endpoint,
                payload_full
            )

            if self.verbose:
                # Show which parameters were filtered out
                removed = set(payload_full.keys()) - set(payload.keys())
                if removed:
                    logger.info(f"Filtered unsupported parameters: {removed}")
                logger.info(f"Sending parameters: {list(payload.keys())}")

            if self.verbose:
                logger.info(f"Creating {self.model_display_name} video via fal.ai...")
                logger.info(f"Image: {Path(character_image_path).name}")
                logger.info(f"Duration: {duration} seconds")
                logger.info(f"Endpoint: {self.base_url}")

            # Submit request with retry logic
            max_submit_retries = 3
            request_id = None
            status_url = None
            response_url = None  # URL to fetch actual result (video data)

            for submit_attempt in range(max_submit_retries):
                try:
                    response = requests.post(
                        self.base_url, headers=headers, json=payload, timeout=30
                    )

                    if response.status_code == 429:
                        logger.warning(
                            f"⚠ Rate limited - waiting before retry ({submit_attempt + 1}/{max_submit_retries})"
                        )
                        time.sleep(30)
                        continue

                    elif response.status_code == 503:
                        logger.warning(
                            f"⚠ Service unavailable - retrying ({submit_attempt + 1}/{max_submit_retries})"
                        )
                        time.sleep(10)
                        continue

                    elif response.status_code == 402:
                        detail = self._extract_response_error_detail(response)
                        error_msg = "Payment required - insufficient credits in your fal.ai account"
                        if detail:
                            error_msg = f"{error_msg}: {detail}"
                        self._set_last_error(error_msg)
                        logger.error(f"💳 {error_msg}")
                        return None

                    elif response.status_code != 200:
                        detail = self._extract_response_error_detail(response)
                        error_msg = f"Request failed: HTTP {response.status_code}"
                        if detail:
                            error_msg = f"{error_msg} — {detail}"
                        self._set_last_error(error_msg)
                        logger.error(f"❌ {error_msg}")
                        if detail:
                            logger.error(f"Response detail: {detail}")

                        # Account lock/balance issues will not resolve by retrying.
                        if response.status_code == 403 and self._is_balance_lock_error(detail):
                            logger.error(
                                "💳 Account locked due to exhausted balance - top up at fal.ai/dashboard/billing"
                            )
                            return None

                        if submit_attempt < max_submit_retries - 1:
                            logger.info(
                                f"Retrying... ({submit_attempt + 1}/{max_submit_retries})"
                            )
                            time.sleep(5)
                            continue
                        return None

                    result = response.json()
                    request_id = result.get("request_id")
                    status_url = result.get("status_url")
                    response_url = result.get("response_url")  # URL to fetch actual result

                    if not request_id or not status_url:
                        logger.error("✗ No request ID or status URL returned")
                        if submit_attempt < max_submit_retries - 1:
                            time.sleep(5)
                            continue
                        return None

                    # Success!
                    break

                except requests.exceptions.Timeout:
                    logger.warning(
                        f"⚠ Request timeout ({submit_attempt + 1}/{max_submit_retries})"
                    )
                    if submit_attempt < max_submit_retries - 1:
                        time.sleep(10)
                        continue
                    logger.error("✗ Failed to submit request after timeouts")
                    return None

                except requests.exceptions.ConnectionError as e:
                    logger.warning(
                        f"⚠ Connection error: {e} ({submit_attempt + 1}/{max_submit_retries})"
                    )
                    if submit_attempt < max_submit_retries - 1:
                        time.sleep(10)
                        continue
                    logger.error("✗ Failed to submit request due to connection errors")
                    return None

                except Exception as e:
                    logger.error(f"✗ Unexpected error during submission: {e}")
                    if submit_attempt < max_submit_retries - 1:
                        time.sleep(5)
                        continue
                    return None

            if not request_id:
                error_msg = "Failed to get request ID after retries"
                self._set_last_error(error_msg)
                logger.error(f"✗ {error_msg}")
                return None

            if not status_url:
                error_msg = "Failed to get status URL after retries"
                self._set_last_error(error_msg)
                logger.error(f"✗ {error_msg}")
                return None

            if self.verbose:
                logger.info(f"✓ Task created: {request_id}")
                logger.info("Waiting for video generation...")
            self._report_progress(f"✓ Task created: {request_id}", "task")
            self._report_progress("Waiting for video generation...", "progress")

            # Poll for completion with exponential backoff
            # Use the status_url provided by fal.ai (already authenticated)
            max_attempts = 240  # 20 minutes total (increased from 15)
            attempt = 0
            consecutive_errors = 0
            max_consecutive_errors = 10

            # Exponential backoff settings
            base_delay = 5
            max_delay = 30

            logger.debug(f" Starting polling loop. Max attempts: {max_attempts}, status_url: {status_url}")

            while attempt < max_attempts:
                # Calculate delay with exponential backoff
                if attempt < 24:  # First 2 minutes: 5 second polls
                    delay = base_delay
                elif attempt < 60:  # Next 3 minutes: 10 second polls
                    delay = 10
                else:  # After 5 minutes: 15 second polls
                    delay = 15

                time.sleep(delay)
                attempt += 1

                logger.debug(f" Polling attempt {attempt}/{max_attempts}, delay: {delay}s")

                # Show progress every minute
                if attempt % (60 // base_delay) == 0:
                    elapsed_mins = (attempt * base_delay) // 60
                    if self.verbose:
                        logger.info(
                            f"⏳ Still waiting... {elapsed_mins} min elapsed (attempt {attempt}/{max_attempts})"
                        )
                    self._report_progress(
                        f"⏳ Still waiting... {elapsed_mins} min elapsed", "progress"
                    )

                try:
                    # fal.ai status endpoint requires Authorization header
                    status_response = requests.get(
                        status_url, headers=status_headers, timeout=30
                    )

                    # Handle different HTTP status codes
                    if status_response.status_code == 404:
                        logger.error(
                            f"✗ Job not found (404) - request may have expired"
                        )
                        return None
                    elif status_response.status_code == 429:
                        if self.verbose:
                            logger.warning(
                                "⚠ Rate limited - waiting longer before retry"
                            )
                        time.sleep(30)
                        continue
                    elif status_response.status_code == 503:
                        if self.verbose:
                            logger.warning(
                                "⚠ Service unavailable - fal.ai may be overloaded"
                            )
                        consecutive_errors += 1
                        if consecutive_errors >= max_consecutive_errors:
                            logger.error(
                                f"✗ Too many service errors ({consecutive_errors}) - giving up"
                            )
                            return None
                        time.sleep(10)
                        continue
                    elif status_response.status_code not in [200, 202]:
                        if self.verbose:
                            logger.warning(
                                f"⚠ Unexpected status code: {status_response.status_code}"
                            )
                        consecutive_errors += 1
                        if consecutive_errors >= max_consecutive_errors:
                            logger.error(
                                f"✗ Too many errors ({consecutive_errors}) - giving up"
                            )
                            return None
                        continue

                    # Reset error counter on success
                    consecutive_errors = 0

                    status_result = status_response.json()
                    status = status_result.get("status")

                    # Log queue position if available
                    queue_position = status_result.get("queue_position")
                    if queue_position and self.verbose and attempt % 6 == 0:
                        logger.info(f"📊 Queue position: {queue_position}")

                    if status == "IN_QUEUE":
                        if self.verbose and attempt % 12 == 0:
                            logger.info("⏳ Job is in queue, waiting...")
                        continue

                    elif status == "IN_PROGRESS":
                        if self.verbose and attempt % 12 == 0:
                            logger.info("🎬 Video is being generated...")
                        continue

                    elif status == "COMPLETED":
                        self._report_progress("Status: COMPLETED — fetching result", "progress")
                        video_url = None

                        # Step 1: Determine result URL
                        # Priority: submit-time response_url > status response_url > constructed
                        result_url = response_url  # from submit response
                        if not result_url:
                            result_url = status_result.get("response_url")
                        if not result_url:
                            # Construct from convention (full model endpoint path)
                            result_url = f"https://queue.fal.run/{self.model_endpoint}/requests/{request_id}"

                        # Step 2: Fetch actual result — try multiple URL strategies
                        result_data = None
                        fetch_urls = [result_url]

                        # If result_url doesn't include the full endpoint, also try constructed URL
                        constructed_url = f"https://queue.fal.run/{self.model_endpoint}/requests/{request_id}"
                        if constructed_url != result_url:
                            fetch_urls.append(constructed_url)

                        for try_url in fetch_urls:
                            try:
                                self._report_progress(f"Fetching result from: {try_url}", "api")
                                result_response = requests.get(
                                    try_url, headers=status_headers, timeout=30
                                )
                                self._report_progress(
                                    f"Result fetch HTTP {result_response.status_code}", "debug"
                                )

                                if result_response.status_code == 200:
                                    result_data = result_response.json()
                                    self._report_progress(
                                        f"Result keys: {list(result_data.keys())}", "debug"
                                    )

                                    # Check for API errors in result
                                    if "error" in result_data:
                                        error_detail = result_data["error"]
                                        if isinstance(error_detail, dict):
                                            error_msg = error_detail.get("message") or error_detail.get("detail") or str(error_detail)
                                        else:
                                            error_msg = str(error_detail)
                                        error_msg = f"API error in result: {error_msg}"
                                        self._set_last_error(error_msg)
                                        self._report_progress(f"❌ {error_msg}", "error")
                                        return None

                                    if "detail" in result_data:
                                        detail = result_data["detail"]
                                        if isinstance(detail, list):
                                            parts = []
                                            for err in detail:
                                                msg = err.get("msg", str(err))
                                                loc = err.get("loc", [])
                                                parts.append(f"{msg} (field: {loc})")
                                            error_msg = f"Validation error: {'; '.join(parts)}"
                                        else:
                                            error_msg = f"API detail: {detail}"
                                        self._set_last_error(error_msg)
                                        self._report_progress(f"❌ {error_msg}", "error")
                                        return None

                                    # Extract video URL from result data
                                    video_url = self._extract_video_url(result_data)
                                    if video_url:
                                        self._report_progress("Found video URL in result data", "debug")
                                        break
                                    else:
                                        self._report_progress(
                                            f"No video URL in result. Keys: {list(result_data.keys())}. "
                                            f"Full: {str(result_data)[:300]}", "warning"
                                        )
                                else:
                                    resp_text = result_response.text[:500]
                                    self._report_progress(
                                        f"Result fetch HTTP {result_response.status_code}: {resp_text}",
                                        "warning",
                                    )
                                    # 422 = validation error (e.g. prompt too long for this model)
                                    if result_response.status_code == 422:
                                        try:
                                            err_data = result_response.json()
                                            details = err_data.get("detail", [])
                                            if isinstance(details, list):
                                                msgs = [d.get("msg", str(d)) for d in details]
                                                error_msg = f"Validation error: {'; '.join(msgs)}"
                                            else:
                                                error_msg = f"Validation error: {details}"
                                        except Exception:
                                            error_msg = f"Validation error (HTTP 422): {resp_text}"
                                        self._set_last_error(error_msg)
                                        self._report_progress(f"❌ {error_msg}", "error")
                                        return None
                                    # Detect model removal/deprecation
                                    if result_response.status_code == 404 and "not found" in resp_text.lower():
                                        error_msg = (
                                            f"Model endpoint unavailable on fal.ai: {self.model_endpoint}. "
                                            f"This model may have been removed or renamed. Try a different model."
                                        )
                                        self._set_last_error(error_msg)
                                        self._report_progress(f"❌ {error_msg}", "error")
                                        return None
                            except Exception as e:
                                self._report_progress(
                                    f"Failed to fetch result from {try_url}: {e}", "warning"
                                )

                        # Step 3: Fallback — check status response body (some models inline results)
                        if not video_url:
                            video_url = self._extract_video_url(status_result)
                            if video_url:
                                self._report_progress("Found video URL in status response (inline)", "debug")

                        # Step 4: Give up if no URL found
                        if not video_url:
                            diag_keys = list(result_data.keys()) if result_data else list(status_result.keys())
                            diag_body = str(result_data or status_result)[:500]
                            error_msg = f"No video URL returned by API (keys: {diag_keys})"
                            self._set_last_error(error_msg)
                            self._report_progress(f"✗ {error_msg}", "error")
                            self._report_progress(f"✗ Response: {diag_body}", "error")
                            return None

                        if self.verbose:
                            logger.info(f"✓ Video ready: {video_url}")
                        self._report_progress(
                            f"✓ Video ready - downloading...", "download"
                        )

                        # Download video with retry logic
                        max_download_retries = 3
                        for download_attempt in range(max_download_retries):
                            try:
                                video_response = requests.get(video_url, timeout=120)
                                if video_response.status_code != 200:
                                    if download_attempt < max_download_retries - 1:
                                        logger.warning(
                                            f"⚠ Download failed (attempt {download_attempt + 1}/{max_download_retries})"
                                        )
                                        time.sleep(5)
                                        continue
                                    else:
                                        logger.error(
                                            f"✗ Failed to download after {max_download_retries} attempts: {video_response.status_code}"
                                        )
                                        return None

                                # Save video with enhanced filename (model, prompt, date)
                                char_name = Path(character_image_path).stem

                                # DEBUG: Log parameters before filename generation
                                logger.debug(f" About to generate filename:")
                                logger.info(f"  char_name: {char_name}")
                                logger.info(f"  model: {self.model_display_name}")

                                try:
                                    output_filename = self.get_output_filename(
                                        char_name, actual_output_folder
                                    )
                                    logger.debug(f" Generated filename: {output_filename}")
                                except Exception as e:
                                    logger.error(f"✗ Filename generation failed: {e}")
                                    logger.error(f"✗ Traceback:\n{traceback.format_exc()}")
                                    raise
                                output_path = (
                                    Path(actual_output_folder) / output_filename
                                )

                                with open(output_path, "wb") as f:
                                    f.write(video_response.content)

                                file_size = output_path.stat().st_size / (1024 * 1024)
                                total_time = attempt * base_delay
                                if self.verbose:
                                    logger.info(f"✓ Video saved: {output_path}")
                                    logger.info(f"✓ File size: {file_size:.2f} MB")
                                    logger.info(
                                        f"✓ Total generation time: {total_time // 60}m {total_time % 60}s"
                                    )
                                self._report_progress(
                                    f"✓ File size: {file_size:.2f} MB", "download"
                                )
                                self._report_progress(
                                    f"✓ Total generation time: {total_time // 60}m {total_time % 60}s",
                                    "success",
                                )

                                return str(output_path)

                            except Exception as download_error:
                                if download_attempt < max_download_retries - 1:
                                    logger.warning(
                                        f"⚠ Download error: {download_error} (retry {download_attempt + 1}/{max_download_retries})"
                                    )
                                    time.sleep(5)
                                else:
                                    logger.error(
                                        f"✗ Download failed after {max_download_retries} attempts: {download_error}"
                                    )
                                    return None

                    elif status in ["FAILED", "ERROR"]:
                        raw_error = status_result.get("error")
                        if isinstance(raw_error, dict):
                            error_msg = raw_error.get("message") or raw_error.get("detail") or str(raw_error)
                        else:
                            error_msg = raw_error

                        if not error_msg:
                            detail = status_result.get("detail")
                            if isinstance(detail, list):
                                error_msg = "; ".join(str(x) for x in detail)
                            elif detail:
                                error_msg = str(detail)

                        if not error_msg:
                            error_msg = status_result.get("message")

                        if not error_msg:
                            error_msg = f"Unknown error ({status_result})"

                        final_error = f"Generation failed: {error_msg}"
                        self._set_last_error(final_error)
                        logger.error(f"✗ {final_error}")

                        # Check if it's a retriable/account error
                        lowered = error_msg.lower()
                        if "quota" in lowered or "credit" in lowered:
                            logger.error(
                                "💳 Insufficient credits - please add more credits to your fal.ai account"
                            )
                        elif "timeout" in lowered:
                            logger.error(
                                "⏱️ Server timeout - fal.ai may be overloaded, try again later"
                            )

                        return None

                    elif status == "CANCELLED":
                        logger.error("✗ Job was cancelled")
                        return None

                except requests.exceptions.Timeout:
                    if self.verbose:
                        logger.warning(f"⚠ Request timeout (attempt {attempt})")
                    consecutive_errors += 1
                    if consecutive_errors >= max_consecutive_errors:
                        logger.error(f"✗ Too many timeouts - giving up")
                        return None
                    continue

                except requests.exceptions.ConnectionError:
                    if self.verbose:
                        logger.warning(f"⚠ Connection error - retrying")
                    consecutive_errors += 1
                    if consecutive_errors >= max_consecutive_errors:
                        logger.error(f"✗ Too many connection errors - giving up")
                        return None
                    time.sleep(10)
                    continue

                except Exception as e:
                    if self.verbose and attempt % 12 == 0:
                        logger.warning(f"⚠ Status check error: {e}")
                    consecutive_errors += 1
                    if consecutive_errors >= max_consecutive_errors:
                        logger.error(f"✗ Too many errors - giving up")
                        return None
                    continue

            # Timeout reached
            error_msg = f"Timeout after {max_attempts * base_delay // 60} minutes"
            self._set_last_error(error_msg)
            logger.error(f"✗ {error_msg}")
            logger.error("💡 Possible causes:")
            logger.error(
                "   - fal.ai servers are overloaded (try again during off-peak hours)"
            )
            logger.error("   - Job stuck in queue (check fal.ai dashboard)")
            logger.error("   - Network connectivity issues")
            return None

        except Exception as e:
            error_msg = str(e)
            self._set_last_error(error_msg)
            logger.error(f"✗ Error: {error_msg}")
            logger.error(f"✗ Full traceback:\n{traceback.format_exc()}")
            return None

    def process_all_images_concurrent(
        self,
        target_directory: str,
        output_directory: Optional[str] = None,
        max_workers: int = 5,
        custom_prompt: Optional[str] = None,
        negative_prompt: Optional[str] = None,
        progress_callback: Optional[Callable] = None,
        use_source_folder: bool = False,
        duration: int = 10,
        aspect_ratio: str = "9:16",
        resolution: str = "720p",
        seed: int = -1,
        camera_fixed: bool = False,
        generate_audio: bool = False,
    ):
        """Process all GenX images concurrently

        Args:
            target_directory: Input directory or file path
            output_directory: Fallback output directory (used when use_source_folder=False)
            max_workers: Maximum concurrent workers
            custom_prompt: Custom generation prompt
            negative_prompt: Negative prompt for content to avoid
            progress_callback: Progress callback function
            use_source_folder: If True, save each video alongside its source image
            duration: Video duration in seconds
            aspect_ratio: Aspect ratio string
            resolution: Resolution string (480p, 720p)
            seed: Random seed (-1 for random)
            camera_fixed: Whether camera should be fixed
            generate_audio: Whether to generate audio
        """
        if output_directory is None:
            output_directory = self.downloads_folder

        if self.verbose:
            logger.info(f"Scanning: {target_directory}")
            logger.info(f"Max workers: {max_workers}")
            if use_source_folder:
                logger.info("Output mode: Videos saved alongside source images")
            else:
                logger.info(f"Output folder: {output_directory}")

        # Collect all images
        all_images = []

        target_path = Path(target_directory)
        if target_path.is_file():
            # Single file mode - check for duplicate in appropriate location
            if use_source_folder:
                check_folder = str(target_path.parent)
            else:
                check_folder = output_directory
            if not self.check_duplicate_exists(str(target_path), check_folder):
                all_images.append(str(target_path))
            elif self.verbose:
                logger.info(f"Skipping duplicate: {target_path.name}")
        else:
            # Root directory
            genx_images = self.get_genx_image_files(
                target_directory, use_source_folder, output_directory
            )
            all_images.extend(genx_images)

            # Subdirectories
            try:
                for folder_path in Path(target_directory).iterdir():
                    if folder_path.is_dir():
                        genx_images = self.get_genx_image_files(
                            str(folder_path), use_source_folder, output_directory
                        )
                        all_images.extend(genx_images)
            except Exception as e:
                if self.verbose:
                    logger.error(f"Error: {e}")

        total_images = len(all_images)
        if total_images == 0:
            if self.verbose:
                logger.info("No GenX images found")
            return

        if self.verbose:
            logger.info(f"Found {total_images} GenX images")
            logger.info("Starting concurrent processing...")

        videos_created = 0
        videos_skipped = 0
        lock = threading.Lock()

        def process_image(image_path):
            nonlocal videos_created, videos_skipped

            image_name = Path(image_path).name

            if progress_callback:
                with lock:
                    progress_callback(
                        videos_created + videos_skipped,
                        total_images,
                        f"Generating: {image_name}",
                    )

            try:
                result = self.create_kling_generation(
                    character_image_path=image_path,
                    output_folder=output_directory,
                    custom_prompt=custom_prompt,
                    negative_prompt=negative_prompt,
                    use_source_folder=use_source_folder,
                    duration=duration,
                    aspect_ratio=aspect_ratio,
                    resolution=resolution,
                    seed=seed,
                    camera_fixed=camera_fixed,
                    generate_audio=generate_audio,
                )

                with lock:
                    if result:
                        videos_created += 1
                        if progress_callback:
                            progress_callback(
                                videos_created + videos_skipped,
                                total_images,
                                f"Completed: {image_name}",
                            )
                    else:
                        videos_skipped += 1
                        if progress_callback:
                            progress_callback(
                                videos_created + videos_skipped,
                                total_images,
                                f"Failed: {image_name}",
                            )

                return result
            except Exception as e:
                with lock:
                    videos_skipped += 1
                    if self.verbose:
                        logger.error(f"Error processing {image_name}: {e}")
                    if progress_callback:
                        progress_callback(
                            videos_created + videos_skipped,
                            total_images,
                            f"Failed: {image_name}",
                        )
                return None

        # Process concurrently
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            futures = {executor.submit(process_image, img): img for img in all_images}

            for future in as_completed(futures):
                try:
                    future.result()
                except Exception as e:
                    if self.verbose:
                        logger.error(f"Unexpected error: {e}")

        # Summary
        if self.verbose:
            logger.info("\n" + "=" * 80)
            logger.info("PROCESSING COMPLETE")
            logger.info(f"Videos created: {videos_created}")
            logger.info(f"Videos skipped: {videos_skipped}")
            logger.info(f"Total processed: {videos_created + videos_skipped}")
            logger.info("=" * 80)
