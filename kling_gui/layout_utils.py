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


def parse_geometry_size(geometry: str, fallback_width: int, fallback_height: int) -> tuple[int, int]:
    """Return ``(width, height)`` parsed from a Tk geometry string.

    Falls back to the provided defaults when the input is empty/malformed.
    Used by ``main_window`` to derive the ACTUAL window size the root is
    about to open at (e.g. "1331x950+97+52" -> 1331, 950) so the pre-sash
    clamp doesn't shrink saved sash positions to fit the smaller ui_config
    default. See main_window._init_window_layout for the full bug story.
    """
    if not isinstance(geometry, str):
        return int(fallback_width), int(fallback_height)
    match = _GEOMETRY_RE.match(geometry.strip())
    if not match:
        return int(fallback_width), int(fallback_height)
    try:
        return int(match.group(1)), int(match.group(2))
    except (TypeError, ValueError):
        return int(fallback_width), int(fallback_height)


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
    """Clamp sash positions to PHYSICALLY usable bounds for current window size.

    Range philosophy (user feedback 2026-05-21: "I asked for this
    to be fixed like 1000x"):

    The clamp used to enforce AESTHETIC percentages (e.g. carousel
    must be ≥ 22% of window) which silently bumped user-saved
    values BACK UP toward the defaults on every launch. User drags
    the drop zone narrower → it saves → next launch the clamp
    promotes it back to 22% → user's preference is lost.

    Now: minimums are PHYSICAL usability floors (~200px) and
    maximums are the actual pane boundary. A user who wants a tiny
    50px log pane gets 200px (still readable), not a "looks-good
    25%" reset. Defaults stay the same for fresh installs but
    saved values are honoured aggressively.
    """
    safe_w = max(900, int(root_width))
    safe_h = max(620, int(root_height))

    # Drop zone height: usable floor 200px; ceiling stays at 75% so
    # a user can't accidentally hide the entire bottom row.
    drop_min = 200
    drop_max = max(drop_min, int(safe_h * 0.85))
    drop_default = int(safe_h * 0.58)

    # Prompt-split (left tab panel vs right tools/prompt). Minimum
    # 400px so the leftmost tab labels stay readable; max 80% so
    # the right panel never fully disappears.
    #
    # Default bumped from 60% → 72% (user feedback 2026-05-22): on
    # Step 3 (Video) the left tab's horizontal controls (model +
    # output + Oldcam/rPPG checkboxes + Re-Run buttons) consume
    # significant width, and at 60% the trailing Re-Run column
    # was getting visually clipped against the right prompt panel.
    # The right prompt panel itself only needs ~28-30% to comfortably
    # show slot picker + title + positive/negative prompt previews.
    prompt_min = 400
    prompt_max = max(prompt_min, int(safe_w * 0.82))
    prompt_default = int(safe_w * 0.72)

    # Carousel width (bottom left). Floor 200px (smallest where
    # thumbnails + nav arrows still fit); ceiling 50% so the right
    # log+queue section keeps room. Default stays at 25% for
    # fresh installs.
    queue_min = 200
    queue_max = max(queue_min, int(safe_w * 0.50))
    queue_default = int(safe_w * 0.25)

    # Log pane height. Floor 80px (~3 lines); ceiling 60%.
    log_min = 80
    log_max = max(log_min, int(safe_h * 0.60))
    log_default = int(safe_h * 0.22)

    # Log vs drop zone: sash_log_drop_split is the X coordinate of
    # the sash measured from the LEFT edge of the right-section
    # paned widget. log_panel is .add()ed FIRST (left), drop_zone
    # SECOND, so this value IS the log panel's width. Floor 150px
    # (a narrow log column is still useful for status lines);
    # ceiling is right_section_w - 150 (drop zone never goes below
    # 150px either).
    # Use _clamp_int here too — a corrupted persisted value (e.g.
    # the config got an "abc" string from a hand-edit, or a None
    # from a partial migration) would crash startup-layout-restore
    # via int("abc"). _clamp_int handles type errors as "fall back
    # to default" rather than raising. (GPT audit on b4ed739.)
    clamped_queue = _clamp_int(sash_queue, queue_min, queue_max, queue_default)
    right_section_w = max(400, safe_w - clamped_queue)
    # sash_log_drop_split is the LOG panel's width (drop zone = the remainder).
    # The drop zone is a small fixed-ish square — bound it to a narrow band so
    # it can NEVER hog width again (recurring user complaint). We size the LOG
    # to fill everything except a ~190px drop column, and clamp the drop column
    # to 170–230px (i.e. log width is pinned to right_section_w - [170,230]).
    # This means even a stale persisted value can't widen the drop zone past
    # ~230px, and the log always gets the lion's share.
    _DROP_ZONE_TARGET = 190
    _DROP_ZONE_MIN = 170
    _DROP_ZONE_MAX = 230
    log_drop_min = max(150, right_section_w - _DROP_ZONE_MAX)
    log_drop_max = max(log_drop_min, right_section_w - _DROP_ZONE_MIN)
    log_drop_default = max(log_drop_min, right_section_w - _DROP_ZONE_TARGET)

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
