"""Layout sanitization helpers for window and sash state."""

import re

_GEOMETRY_RE = re.compile(r"^(\d+)x(\d+)([+-]\d+)?([+-]\d+)?$")


def _clamp_int(value, minimum: int, maximum: int, fallback: int) -> int:
    """Clamp any int-like value to [minimum, maximum] with fallback."""
    if maximum < minimum:
        maximum = minimum
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = fallback
    return max(minimum, min(parsed, maximum))


def sanitize_saved_geometry(saved_geometry: str, min_width: int, min_height: int, max_width: int, max_height: int) -> str:
    """Sanitize Tk geometry string to safe size bounds, preserving position when present."""
    if not isinstance(saved_geometry, str) or not saved_geometry.strip():
        return ""
    match = _GEOMETRY_RE.match(saved_geometry.strip())
    if not match:
        return ""

    width = _clamp_int(match.group(1), min_width, max_width, min_width)
    height = _clamp_int(match.group(2), min_height, max_height, min_height)
    x_part = match.group(3) or ""
    y_part = match.group(4) or ""
    return f"{width}x{height}{x_part}{y_part}"


def sanitize_window_layout(window_config: dict, saved_geometry: str, screen_width: int, screen_height: int) -> tuple[dict, str, bool]:
    """Clamp window sizing config and geometry to monitor-safe ranges."""
    safe_screen_w = max(1024, int(screen_width))
    safe_screen_h = max(720, int(screen_height))

    max_width = max(920, int(safe_screen_w * 0.95))
    max_height = max(620, int(safe_screen_h * 0.90))
    min_width_cap = max(760, int(safe_screen_w * 0.82))
    min_height_cap = max(560, int(safe_screen_h * 0.78))

    width = _clamp_int(window_config.get("width"), 840, max_width, 1100)
    height = _clamp_int(window_config.get("height"), 620, max_height, 900)
    min_width = _clamp_int(window_config.get("min_width"), 700, min_width_cap, 760)
    min_height = _clamp_int(window_config.get("min_height"), 520, min_height_cap, 620)

    width = max(width, min_width)
    height = max(height, min_height)

    sanitized_geometry = sanitize_saved_geometry(saved_geometry, min_width, min_height, max_width, max_height)
    sanitized_window = {
        "width": width,
        "height": height,
        "min_width": min_width,
        "min_height": min_height,
    }

    changed = (
        window_config.get("width") != width
        or window_config.get("height") != height
        or window_config.get("min_width") != min_width
        or window_config.get("min_height") != min_height
        or (saved_geometry or "") != sanitized_geometry
    )
    return sanitized_window, sanitized_geometry, changed


def sanitize_sash_layout(
    sash_dropzone,
    sash_prompt_split,
    sash_queue,
    sash_log,
    sash_log_drop_split,
    root_width: int,
    root_height: int,
) -> tuple[dict, bool]:
    """Clamp sash positions to compact, usable bounds for current window size."""
    safe_w = max(900, int(root_width))
    safe_h = max(620, int(root_height))

    drop_min = 320
    drop_max = max(drop_min, int(safe_h * 0.75))
    drop_default = int(safe_h * 0.58)

    # Left tab panel vs right tools/prompt panel — left panel 52-62% of window
    prompt_min = max(480, int(safe_w * 0.50))
    prompt_max = max(prompt_min, int(safe_w * 0.62))
    prompt_default = int(safe_w * 0.56)

    # Carousel width: 20-30% of window (narrower carousel = more room for log+drop)
    queue_min = max(200, int(safe_w * 0.20))
    queue_max = max(queue_min, int(safe_w * 0.30))
    queue_default = int(safe_w * 0.24)

    log_min = 110
    log_max = max(log_min, int(safe_h * 0.42))
    log_default = int(safe_h * 0.22)

    # Log vs drop zone: clamp relative to the right section width (safe_w - clamped queue)
    # so saved values from a different window size don't blow past the pane boundary.
    clamped_queue = max(queue_min, min(int(sash_queue) if sash_queue else queue_default, queue_max))
    right_section_w = max(400, safe_w - clamped_queue)
    log_drop_min = max(200, int(right_section_w * 0.42))
    log_drop_max = max(log_drop_min, int(right_section_w * 0.62))
    log_drop_default = int(right_section_w * 0.52)

    sanitized = {
        "sash_dropzone": _clamp_int(sash_dropzone, drop_min, drop_max, drop_default),
        "sash_prompt_split": _clamp_int(sash_prompt_split, prompt_min, prompt_max, prompt_default),
        "sash_queue": _clamp_int(sash_queue, queue_min, queue_max, queue_default),
        "sash_log": _clamp_int(sash_log, log_min, log_max, log_default),
        "sash_log_drop_split": _clamp_int(sash_log_drop_split, log_drop_min, log_drop_max, log_drop_default),
    }
    changed = (
        sash_dropzone != sanitized["sash_dropzone"]
        or sash_prompt_split != sanitized["sash_prompt_split"]
        or sash_queue != sanitized["sash_queue"]
        or sash_log != sanitized["sash_log"]
        or sash_log_drop_split != sanitized["sash_log_drop_split"]
    )
    return sanitized, changed
