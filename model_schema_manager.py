"""
Model Schema Manager - Queries and caches fal.ai model capabilities
"""

import requests
import logging
import time
import threading
import sys
from typing import Dict, List, Optional, Set, Any
from dataclasses import dataclass, field
import json
from pathlib import Path

from path_utils import get_user_data_dir

logger = logging.getLogger(__name__)

# Default cache TTL in seconds (24 hours)
DEFAULT_CACHE_TTL = 86400


@dataclass
class ModelParameter:
    """Represents a single model parameter"""

    name: str
    type: str
    required: bool
    description: str
    default: Any = None
    enum: Optional[List[Any]] = None
    minimum: Optional[float] = None
    maximum: Optional[float] = None


@dataclass
class CachedSchema:
    """Represents a cached schema with timestamp for TTL management"""

    parameters: Dict[str, ModelParameter] = field(default_factory=dict)
    timestamp: float = 0.0

    def is_expired(self, ttl: int = DEFAULT_CACHE_TTL) -> bool:
        """Check if the cache has expired based on TTL."""
        if self.timestamp == 0:
            return True
        return (time.time() - self.timestamp) > ttl


@dataclass
class APIProbeResult:
    """Represents the outcome of a lightweight fal.ai connectivity check."""

    state: str
    message: str
    status_code: Optional[int] = None
    retry_after_seconds: Optional[int] = None


class ModelSchemaManager:
    """Manages model schemas and parameter detection with TTL-based caching"""

    def __init__(
        self,
        api_key: str,
        cache_dir: Optional[str] = None,
        cache_ttl: int = DEFAULT_CACHE_TTL,
    ):
        """
        Initialize the ModelSchemaManager.

        Args:
            api_key: fal.ai API key
            cache_dir: Directory for disk cache
            cache_ttl: Cache time-to-live in seconds
        """
        self.api_key = api_key
        self.cache_ttl = cache_ttl
        self.cache_dir = Path(cache_dir) if cache_dir else self._default_cache_dir()
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self._memory_cache: Dict[str, CachedSchema] = {}
        self._cache_lock = threading.Lock()

    @staticmethod
    def _default_cache_dir() -> Path:
        """Return the model-cache location, using macOS Application Support when applicable."""
        if sys.platform == "darwin":
            return Path(get_user_data_dir()) / "model_cache"
        return Path.home() / ".kling-ui" / "model_cache"

    @staticmethod
    def _parse_retry_after(response: Optional[requests.Response]) -> Optional[int]:
        """Return Retry-After as seconds when the API provides it."""
        if response is None:
            return None
        retry_after = response.headers.get("Retry-After", "").strip()
        if retry_after.isdigit():
            return max(1, int(retry_after))
        return None

    def probe_api_access(self, timeout: float = 10.0) -> APIProbeResult:
        """Perform one lightweight live request to classify API availability."""
        headers = {"Authorization": f"Key {self.api_key}"}
        try:
            response = requests.get(
                "https://api.fal.ai/v1/models",
                params={"limit": 1},
                headers=headers,
                timeout=timeout,
            )
        except requests.RequestException as exc:
            return APIProbeResult(
                state="network_error",
                message=f"Could not reach fal.ai right now: {exc}",
            )

        status_code = response.status_code
        if status_code == 200:
            return APIProbeResult("ok", "fal.ai connection verified.", status_code=200)
        if status_code in (401, 403):
            return APIProbeResult(
                "auth_error",
                "The saved fal.ai API key was rejected. Update it before generating.",
                status_code=status_code,
            )
        if status_code == 429:
            retry_after = self._parse_retry_after(response)
            wait_text = f" Wait about {retry_after}s and try again." if retry_after else ""
            return APIProbeResult(
                "rate_limited",
                f"fal.ai rate limited live model checks.{wait_text}",
                status_code=status_code,
                retry_after_seconds=retry_after,
            )
        if status_code >= 500:
            return APIProbeResult(
                "server_error",
                f"fal.ai is temporarily unavailable (HTTP {status_code}).",
                status_code=status_code,
            )
        return APIProbeResult(
            "unexpected_error",
            f"fal.ai returned HTTP {status_code} while checking API access.",
            status_code=status_code,
        )

    def get_model_schema(
        self,
        model_endpoint: str,
        use_cache: bool = True,
        force_refresh: bool = False,
        allow_network: bool = True,
    ) -> Dict[str, ModelParameter]:
        """
        Fetch OpenAPI schema for a model endpoint.

        Args:
            model_endpoint: e.g., "fal-ai/kling-video/v2.1/pro/image-to-video"
            use_cache: Whether to use cached schema
            force_refresh: Force refresh even if cache is valid (bypasses TTL)
            allow_network: False keeps startup/cache-only paths from making live calls

        Returns:
            Dictionary of parameter name -> ModelParameter
        """
        # Check memory cache first (unless force refresh)
        with self._cache_lock:
            if not force_refresh and model_endpoint in self._memory_cache:
                cached = self._memory_cache[model_endpoint]
                if not cached.is_expired(self.cache_ttl):
                    logger.debug(f"Using memory-cached schema for {model_endpoint}")
                    return cached.parameters
                else:
                    logger.debug(f"Memory cache expired for {model_endpoint}")

        # Check disk cache
        cache_file = self.cache_dir / f"{model_endpoint.replace('/', '_')}.json"
        if use_cache and not force_refresh and cache_file.exists():
            try:
                with open(cache_file, "r") as f:
                    cached_data = json.load(f)

                # Check disk cache timestamp
                cache_timestamp = cached_data.get("_cache_timestamp", 0)
                if (
                    cache_timestamp
                    and (time.time() - cache_timestamp) <= self.cache_ttl
                ):
                    parameters = self._parse_cached_schema(cached_data)
                    # Store in memory cache
                    with self._cache_lock:
                        self._memory_cache[model_endpoint] = CachedSchema(
                            parameters=parameters, timestamp=cache_timestamp
                        )
                    logger.info(f"Loaded disk-cached schema for {model_endpoint}")
                    return parameters
                else:
                    logger.debug(f"Disk cache expired for {model_endpoint}")
            except Exception as e:
                logger.warning(f"Failed to load cached schema: {e}")

        if not allow_network:
            logger.debug("Using offline schema fallback for %s (network disabled)", model_endpoint)
            return {}

        # Fetch from API
        try:
            url = "https://api.fal.ai/v1/models"
            params = {"endpoint_id": model_endpoint, "expand": "openapi-3.0"}
            headers = {"Authorization": f"Key {self.api_key}"}

            logger.info(f"Fetching schema for {model_endpoint}...")
            response = requests.get(url, params=params, headers=headers, timeout=30)

            if response.status_code != 200:
                logger.warning(
                    "Schema lookup for %s returned HTTP %s",
                    model_endpoint,
                    response.status_code,
                )
                return self._get_fallback_schema()

            data = response.json()

            # Add cache timestamp to data before saving
            current_time = time.time()
            data["_cache_timestamp"] = current_time

            # Save to disk cache
            with open(cache_file, "w") as f:
                json.dump(data, f, indent=2)

            # Parse schema
            parameters = self._parse_schema_response(data)

            # Store in memory cache
            with self._cache_lock:
                self._memory_cache[model_endpoint] = CachedSchema(
                    parameters=parameters, timestamp=current_time
                )

            logger.info(f"Fetched schema with {len(parameters)} parameters")
            return parameters

        except Exception as e:
            logger.error(f"Error fetching schema: {e}")
            return self._get_fallback_schema()

    def _parse_schema_response(self, data: dict) -> Dict[str, ModelParameter]:
        """Parse OpenAPI schema from API response"""
        parameters = {}

        try:
            # The API returns a list of models - we want the first one
            models_list = data.get("models", [])
            if not models_list:
                logger.warning("No models found in API response")
                return parameters

            model_data = models_list[0]

            # Navigate to the OpenAPI schema
            openapi = model_data.get("openapi", {})
            components = openapi.get("components", {})
            schemas = components.get("schemas", {})

            # Find the Input schema - it could be named "Input" or model-specific like
            # "KlingVideoV21ProImageToVideoInput"
            input_schema = None

            # First try "Input"
            if "Input" in schemas:
                input_schema = schemas["Input"]
            else:
                # Look for a schema that ends with "Input" and contains "properties"
                for schema_name, schema_data in schemas.items():
                    if schema_name.endswith("Input") and "properties" in schema_data:
                        input_schema = schema_data
                        logger.debug(f"Found input schema: {schema_name}")
                        break

            if not input_schema:
                logger.warning("Could not find Input schema in response")
                return parameters

            properties = input_schema.get("properties", {})
            required_fields = set(input_schema.get("required", []))

            for param_name, param_spec in properties.items():
                param = ModelParameter(
                    name=param_name,
                    type=param_spec.get("type", "string"),
                    required=param_name in required_fields,
                    description=param_spec.get("description", ""),
                    default=param_spec.get("default"),
                    enum=param_spec.get("enum"),
                    minimum=param_spec.get("minimum"),
                    maximum=param_spec.get("maximum"),
                )
                parameters[param_name] = param

            logger.info(f"Parsed {len(parameters)} parameters from schema")

        except Exception as e:
            logger.error(f"Error parsing schema: {e}")

        return parameters

    def _parse_cached_schema(self, data: dict) -> Dict[str, ModelParameter]:
        """Parse cached schema data"""
        return self._parse_schema_response(data)

    # Sentinel attribute that validate_parameters checks to skip
    # destructive filtering when the schema is a non-authoritative
    # fallback (Codex P1, PR #41).
    _FALLBACK_SENTINEL = "__fallback_schema__"

    def _get_fallback_schema(self) -> Dict[str, ModelParameter]:
        """Return fallback schema with common parameters.

        IMPORTANT: this schema is NON-AUTHORITATIVE -- it's what we
        return when fal.ai schema discovery fails (429/5xx/network)
        or returns an empty schema. The set of params here is the
        UNION of params used across the live roster (image_url AND
        start_image_url, both end variants, neg/cfg) so that even
        if validate_parameters somehow filters against this dict
        it won't drop a v3-family required start_image_url
        (Codex P1, PR #41). Tagged with _FALLBACK_SENTINEL so
        validate_parameters can detect it and skip filtering
        entirely -- the live API is the real authority.
        """
        schema = {
            # Start-image synonyms -- both names different model
            # families use (image_url for v2.5/o1, start_image_url
            # for v3 / v2.6 / o1-new).
            "image_url": ModelParameter("image_url", "string", True, "Input image URL"),
            "start_image_url": ModelParameter("start_image_url", "string", False, "Input start image URL (v3/o1 family)"),
            # End-frame synonyms.
            "end_image_url": ModelParameter("end_image_url", "string", False, "Input end image URL"),
            "tail_image_url": ModelParameter("tail_image_url", "string", False, "Input tail image URL (v2.5-turbo-pro)"),
            "prompt": ModelParameter("prompt", "string", True, "Generation prompt"),
            "negative_prompt": ModelParameter("negative_prompt", "string", False, "Negative prompt"),
            "cfg_scale": ModelParameter("cfg_scale", "number", False, "CFG scale"),
            "duration": ModelParameter(
                "duration", "integer", False, "Video duration", default=10
            ),
            "aspect_ratio": ModelParameter(
                "aspect_ratio", "string", False, "Aspect ratio", default="16:9"
            ),
        }
        # Tag the dict so validate_parameters() can detect it.
        try:
            schema[self._FALLBACK_SENTINEL] = True  # type: ignore[assignment]
        except Exception:
            pass
        return schema  # type: ignore[return-value]

    def supports_parameter(self, model_endpoint: str, param_name: str) -> bool:
        """Check if a model supports a specific parameter"""
        schema = self.get_model_schema(model_endpoint)
        return param_name in schema

    def get_supported_parameters(self, model_endpoint: str) -> Set[str]:
        """Get set of all supported parameter names"""
        schema = self.get_model_schema(model_endpoint)
        return set(schema.keys())

    def get_parameter_info(
        self, model_endpoint: str, param_name: str
    ) -> Optional[ModelParameter]:
        """Get detailed info about a specific parameter"""
        schema = self.get_model_schema(model_endpoint)
        return schema.get(param_name)

    def validate_parameters(
        self, model_endpoint: str, params: Dict[str, Any]
    ) -> Dict[str, Any]:
        """
        Filter parameters to only those supported by the model.

        Args:
            model_endpoint: Model endpoint identifier
            params: Dictionary of parameters to validate

        Returns:
            Filtered dictionary with only supported parameters
        """
        # NB: get_model_schema may return our non-authoritative
        # fallback dict (fal.ai 429/5xx/network). Detect that via
        # the sentinel and send the payload unfiltered -- the live
        # API is the only true authority on model params, and
        # destructively filtering against the fallback's static
        # union would silently drop e.g. v3's start_image_url
        # (Codex P1, PR #41).
        schema = self.get_model_schema(model_endpoint)
        if schema.get(self._FALLBACK_SENTINEL):
            logger.warning(
                f"Schema for {model_endpoint} is the non-authoritative fallback; "
                f"sending unfiltered payload to fal.ai"
            )
            return dict(params)
        supported = set(schema.keys())

        # If schema discovery fails or is incomplete, avoid dropping required payload fields.
        # In this case we trust API-side validation and send all parameters as-is.
        if not supported:
            logger.warning(
                f"No schema parameters available for {model_endpoint}; sending unfiltered payload"
            )
            return dict(params)

        if (
            ("image_url" not in supported and "start_image_url" not in supported)
            or "prompt" not in supported
        ):
            logger.warning(
                f"Schema for {model_endpoint} missing core fields; sending unfiltered payload"
            )
            return dict(params)

        validated = {}

        for key, value in params.items():
            if key in supported:
                validated[key] = value
            else:
                logger.warning(
                    f"Parameter '{key}' not supported by {model_endpoint}, skipping"
                )

        return validated

    def get_model_pricing(self, endpoints: List[str]) -> Dict[str, dict]:
        """Batch-fetch pricing from fal.ai pricing API.

        Returns {endpoint: {unit_price, unit, currency}} for each endpoint.
        Returns {} on failure (never blocks UI).
        """
        if not endpoints:
            return {}

        # Check disk cache first
        pricing_cache_file = self.cache_dir.parent / "pricing_cache.json"
        cached_pricing = {}
        if pricing_cache_file.exists():
            try:
                with open(pricing_cache_file, "r") as f:
                    cached_data = json.load(f)
                cache_ts = cached_data.get("_cache_timestamp", 0)
                if cache_ts and (time.time() - cache_ts) <= self.cache_ttl:
                    cached_pricing = cached_data.get("pricing", {})
                    # If all requested endpoints are cached, return immediately
                    if all(ep in cached_pricing for ep in endpoints):
                        return {ep: cached_pricing[ep] for ep in endpoints if ep in cached_pricing}
            except Exception:
                pass

        try:
            headers = {"Authorization": f"Key {self.api_key}"}
            # Fetch pricing for all endpoints
            params = {"endpoint_id": ",".join(endpoints)}
            response = requests.get(
                "https://api.fal.ai/v1/models/pricing",
                params=params,
                headers=headers,
                timeout=15,
            )
            if response.status_code != 200:
                logger.warning(f"Pricing API returned {response.status_code}")
                return cached_pricing

            data = response.json()
            result = {}

            # Parse response — API returns {"prices": [...]} but handle variants
            pricing_list = data if isinstance(data, list) else data.get("prices", data.get("models", data.get("pricing", [])))

            def _normalize_unit(raw_unit: str) -> str:
                """Normalize plural units from API (seconds→second, videos→video)."""
                u = raw_unit.lower().rstrip("s") if raw_unit else "video"
                return u if u else "video"

            if isinstance(pricing_list, list):
                for item in pricing_list:
                    ep = item.get("endpoint_id", item.get("endpoint", ""))
                    if ep and any(k in item for k in ("unit_price", "price")):
                        result[ep] = {
                            "unit_price": item.get("unit_price", item.get("price", 0)),
                            "unit": _normalize_unit(item.get("unit", "video")),
                            "currency": item.get("currency", "USD"),
                        }
            elif isinstance(pricing_list, dict):
                # Dict keyed by endpoint
                for ep, info in pricing_list.items():
                    if isinstance(info, dict):
                        result[ep] = {
                            "unit_price": info.get("unit_price", info.get("price", 0)),
                            "unit": _normalize_unit(info.get("unit", "video")),
                            "currency": info.get("currency", "USD"),
                        }

            # Merge with cached and save
            cached_pricing.update(result)
            try:
                pricing_cache_file.parent.mkdir(parents=True, exist_ok=True)
                with open(pricing_cache_file, "w") as f:
                    json.dump({"_cache_timestamp": time.time(), "pricing": cached_pricing}, f, indent=2)
            except Exception as e:
                logger.debug(f"Could not write pricing cache: {e}")

            return {ep: cached_pricing[ep] for ep in endpoints if ep in cached_pricing}

        except Exception as e:
            logger.warning(f"Failed to fetch pricing: {e}")
            return {ep: cached_pricing[ep] for ep in endpoints if ep in cached_pricing}

    def extract_capabilities(self, model_endpoint: str) -> dict:
        """Read cached schema and return structured dict of model capabilities.

        Returns dict with keys like duration_options, aspect_ratios,
        supports_audio, etc. based on what the schema reports.
        """
        schema = self.get_model_schema(model_endpoint)

        caps = {
            "duration_options": [],
            "duration_default": None,
            "aspect_ratios": [],
            "aspect_ratio_default": None,
            "supports_audio": False,
            "supports_camera_fixed": False,
            "supports_seed": False,
            "supports_resolution": False,
            "supports_negative_prompt": False,
            "resolution_options": [],
        }

        # Duration
        dur = schema.get("duration")
        if dur:
            if dur.enum:
                # Drop non-numeric enum values (e.g. Seedance 2.0's "auto")
                # via EAFP -- more robust than isdigit() for signed/odd formats.
                _numeric = []
                for _d in dur.enum:
                    try:
                        _numeric.append(int(_d))
                    except (TypeError, ValueError):
                        continue
                caps["duration_options"] = sorted(_numeric)
            if dur.default is not None:
                try:
                    caps["duration_default"] = int(dur.default)
                except (ValueError, TypeError):
                    pass

        # Aspect ratio
        ar = schema.get("aspect_ratio")
        if ar:
            if ar.enum:
                caps["aspect_ratios"] = list(ar.enum)
            if ar.default is not None:
                caps["aspect_ratio_default"] = str(ar.default)

        # Resolution
        res = schema.get("resolution")
        if res:
            caps["supports_resolution"] = True
            if res.enum:
                caps["resolution_options"] = list(res.enum)

        # Boolean feature flags
        caps["supports_audio"] = "generate_audio" in schema
        caps["supports_camera_fixed"] = "camera_fixed" in schema
        caps["supports_seed"] = "seed" in schema
        caps["supports_negative_prompt"] = "negative_prompt" in schema

        return caps

    def get_model_metadata(self, model_endpoint: str) -> dict:
        """Return {display_name, description, date, category, status} from cached schema.

        Reads from disk cache (the full API response includes a metadata block).
        Returns {} if no cached data is available.
        """
        cache_file = self.cache_dir / f"{model_endpoint.replace('/', '_')}.json"
        if not cache_file.exists():
            # Try to fetch it (will populate cache)
            self.get_model_schema(model_endpoint)
            if not cache_file.exists():
                return {}

        try:
            with open(cache_file, "r") as f:
                data = json.load(f)

            models_list = data.get("models", [])
            if not models_list:
                return {}

            model_data = models_list[0]
            metadata = model_data.get("metadata", {})
            return {
                "display_name": metadata.get("display_name", ""),
                "description": metadata.get("description", ""),
                "date": metadata.get("date", ""),
                "category": metadata.get("category", ""),
                "status": metadata.get("status", ""),
            }
        except Exception as e:
            logger.debug(f"Could not read metadata for {model_endpoint}: {e}")
            return {}

    def list_models(self, category: str = "image-to-video", status: str = "active") -> list:
        """Fetch available models from fal.ai catalog.

        Returns list of dicts: {endpoint, display_name, description, date, category, status}.
        Results are cached to disk with standard TTL. Returns [] on failure.
        """
        cache_file = self.cache_dir.parent / f"catalog_{category}_{status}.json"

        # Check disk cache
        if cache_file.exists():
            try:
                with open(cache_file, "r") as f:
                    cached = json.load(f)
                ts = cached.get("_cache_timestamp", 0)
                if ts and (time.time() - ts) <= self.cache_ttl:
                    return cached.get("models", [])
            except Exception as e:
                logger.debug(f"Could not read catalog cache: {e}")

        try:
            headers = {"Authorization": f"Key {self.api_key}"}
            results = []
            cursor = None
            seen_cursors: set[str] = set()

            while True:
                params = {"category": category, "status": status, "limit": 500}
                if cursor:
                    params["cursor"] = cursor

                response = requests.get(
                    "https://api.fal.ai/v1/models",
                    params=params,
                    headers=headers,
                    timeout=30,
                )
                if response.status_code != 200:
                    body = response.text[:200] if response.text else "(empty)"
                    logger.warning(f"Catalog API returned {response.status_code}: {body}")
                    return []

                data = response.json()
                models_list = data.get("models", data if isinstance(data, list) else [])

                for item in models_list:
                    endpoint = item.get("endpoint_id", item.get("endpoint", ""))
                    if not endpoint:
                        continue
                    metadata = item.get("metadata", {})
                    results.append({
                        "endpoint": endpoint,
                        "display_name": metadata.get("display_name", item.get("display_name", "")),
                        "description": metadata.get("description", item.get("description", "")),
                        "date": metadata.get("date", item.get("date", "")),
                        "category": metadata.get("category", category),
                        "status": metadata.get("status", status),
                    })

                next_cursor = data.get("next_cursor") if isinstance(data, dict) else None
                if not (isinstance(data, dict) and data.get("has_more") and next_cursor):
                    break
                if next_cursor in seen_cursors:
                    logger.warning("Catalog pagination returned repeated cursor; stopping at %s", next_cursor)
                    break
                seen_cursors.add(next_cursor)
                cursor = next_cursor

            # Save to disk cache
            try:
                cache_file.parent.mkdir(parents=True, exist_ok=True)
                with open(cache_file, "w") as f:
                    json.dump({"_cache_timestamp": time.time(), "models": results}, f, indent=2)
            except Exception as e:
                logger.debug(f"Could not write catalog cache: {e}")

            return results

        except Exception as e:
            logger.warning(f"Failed to fetch model catalog: {e}")
            return []

    def clear_cache(self, model_endpoint: Optional[str] = None):
        """
        Clear cached schemas.

        Args:
            model_endpoint: Specific model to clear, or None to clear all
        """
        if model_endpoint:
            # Clear specific model
            with self._cache_lock:
                if model_endpoint in self._memory_cache:
                    del self._memory_cache[model_endpoint]
            cache_file = self.cache_dir / f"{model_endpoint.replace('/', '_')}.json"
            if cache_file.exists():
                cache_file.unlink()
            logger.info(f"Cleared cache for {model_endpoint}")
        else:
            # Clear all caches
            with self._cache_lock:
                self._memory_cache.clear()
            for cache_file in self.cache_dir.glob("*.json"):
                cache_file.unlink()
            logger.info("Cleared all schema caches")

    def get_cache_info(self, model_endpoint: str) -> Dict[str, Any]:
        """
        Get cache status information for a model.

        Args:
            model_endpoint: Model endpoint to check

        Returns:
            Dictionary with cache status info
        """
        info = {
            "model": model_endpoint,
            "in_memory": model_endpoint in self._memory_cache,
            "on_disk": False,
            "expired": True,
            "age_seconds": None,
            "ttl_remaining": None,
        }

        # Check memory cache
        with self._cache_lock:
            if model_endpoint in self._memory_cache:
                cached = self._memory_cache[model_endpoint]
                age = time.time() - cached.timestamp
                info["age_seconds"] = int(age)
                info["expired"] = cached.is_expired(self.cache_ttl)
                info["ttl_remaining"] = max(0, int(self.cache_ttl - age))

        # Check disk cache
        cache_file = self.cache_dir / f"{model_endpoint.replace('/', '_')}.json"
        if cache_file.exists():
            info["on_disk"] = True
            try:
                with open(cache_file, "r") as f:
                    data = json.load(f)
                    timestamp = data.get("_cache_timestamp", 0)
                    if timestamp:
                        age = time.time() - timestamp
                        info["disk_age_seconds"] = int(age)
            except Exception:
                pass

        return info

    def refresh_schema(self, model_endpoint: str) -> Dict[str, ModelParameter]:
        """
        Force refresh schema from API, bypassing cache.

        Args:
            model_endpoint: Model endpoint to refresh

        Returns:
            Fresh schema parameters
        """
        return self.get_model_schema(model_endpoint, use_cache=True, force_refresh=True)


def check_endpoint_health(api_key: str, endpoint: str, timeout: float = 5.0) -> bool:
    """Check if a fal.ai model endpoint is alive.

    Sends an empty POST to the sync endpoint (fal.run). Alive models return
    422 (validation error — they tried to parse input). Dead/removed models
    return 404. This is free (no inference, no credits).

    Returns True if alive, False if dead/unreachable.
    """
    url = f"https://fal.run/{endpoint}"
    headers = {"Authorization": f"Key {api_key}", "Content-Type": "application/json"}
    try:
        resp = requests.post(url, headers=headers, json={}, timeout=timeout)
        # 422 = validation error = model exists and tried to process
        # 404 = "Path ... not found" = model removed
        return resp.status_code != 404
    except Exception:
        return True  # network error — assume alive, don't block the user


def check_all_endpoints(api_key: str, models: list, timeout: float = 5.0) -> Dict[str, bool]:
    """Check health of all model endpoints in parallel.

    Args:
        api_key: fal.ai API key
        models: List of model dicts with "endpoint" key
        timeout: Per-request timeout

    Returns:
        Dict mapping endpoint -> is_alive (True/False)
    """
    from concurrent.futures import ThreadPoolExecutor, as_completed

    results: Dict[str, bool] = {}
    endpoints = [m["endpoint"] for m in models if m.get("endpoint")]

    def _check(ep):
        return ep, check_endpoint_health(api_key, ep, timeout)

    with ThreadPoolExecutor(max_workers=min(8, len(endpoints))) as pool:
        futures = {pool.submit(_check, ep): ep for ep in endpoints}
        for future in as_completed(futures):
            try:
                ep, alive = future.result()
                results[ep] = alive
            except Exception:
                results[futures[future]] = True  # assume alive on error

    dead = [ep for ep, alive in results.items() if not alive]
    if dead:
        logger.warning(f"Dead/unavailable model endpoints: {dead}")

    return results
