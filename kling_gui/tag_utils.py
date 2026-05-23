"""Pipeline tag derivation and filename generation from structured ops."""

import os
import re
from pathlib import Path
from path_utils import sanitize_stem, sanitize_filename

_OP_ORDER = ["pol", "ups", "exp"]

# For backward compat: parse old filenames as fallback
_OLD_OP_MAP = {"polished": "pol", "upscaled": "ups", "outpaint": "exp"}
_OLD_OP_RE = re.compile(r"_(polished|upscaled|outpaint)(?:_\d+)?(?=_|$)")


def derive_display_tag(entry) -> tuple:
    """Return (tag_string, color_key) from entry.ops (structured source of truth).

    Falls back to legacy filename parsing for old sessions with empty ops.
    Returns color_key as string; caller maps to actual color.
    """
    ops = getattr(entry, "ops", None) or {}

    # Fallback: if ops empty but source_type is polish/upscale/outpaint,
    # try legacy filename parsing
    if not ops and entry.source_type in ("polish", "upscale", "outpaint"):
        ops = _parse_legacy_filename(entry.filename)

    if entry.source_type == "input":
        return "[ORIGINAL]", "accent_blue"
    if entry.source_type == "selfie":
        return "[GENERATED]", "success"
    if entry.source_type == "video":
        return "[VIDEO]", "warning_light"

    if not ops:
        # Final fallback for typed entries without ops
        fallback = {"polish": "POL", "upscale": "UPS", "outpaint": "EXP"}
        tag = "[" + fallback.get(entry.source_type, entry.source_type.upper()) + "]"
        return tag, "text_dim"

    parts = []
    for op in _OP_ORDER:
        c = ops.get(op, 0)
        if c == 0:
            continue
        label = op.upper()
        if c > 1:
            label = f"{c}{label}"
        parts.append(label)

    tag = "[" + "+".join(parts) + "]"

    # Color based on source_type (last operation)
    color_map = {"polish": "success", "upscale": "accent_blue", "outpaint": "warning"}
    return tag, color_map.get(entry.source_type, "text_dim")


def build_ops_filename(base_stem: str, ops: dict, ext: str = ".png") -> str:
    """Build abbreviated filename from base stem + ops dict.

    Examples:
        ("1_crop", {"pol": 1})                      -> "1_crop_pol.png"
        ("1_crop", {"pol": 2, "ups": 1})             -> "1_crop_2-pol_ups.png"
        ("1_crop", {"pol": 1, "ups": 1, "exp": 1})  -> "1_crop_pol_ups_exp.png"
    """
    parts = [sanitize_stem(base_stem, default="image")]
    for op in _OP_ORDER:
        c = ops.get(op, 0)
        if c == 0:
            continue
        elif c == 1:
            parts.append(op)
        else:
            parts.append(f"{c}-{op}")
    return sanitize_filename("_".join(parts) + ext, default_stem="image")


def increment_ops(current_ops: dict, operation: str) -> dict:
    """Return a new ops dict with the given operation incremented."""
    new_ops = dict(current_ops or {})
    new_ops[operation] = new_ops.get(operation, 0) + 1
    return new_ops


# `build_expand_filenames` was moved to `path_utils.py` so the CLI
# automation pipeline (`automation/pipeline.py`) and Step 2.5
# (`expand_tab.py`) can use the same helper that Step 0 uses. The CLI
# must NOT depend on `kling_gui.*`; `path_utils` is shared root-level.
# Re-export here so existing call sites keep working.
from path_utils import build_expand_filenames  # noqa: F401


def _parse_legacy_filename(filename: str) -> dict:
    """Fallback: parse old-format filenames for backward compat."""
    stem = os.path.splitext(filename)[0].lower()
    ops = {}
    for m in _OLD_OP_RE.finditer(stem):
        op = _OLD_OP_MAP[m.group(1)]
        ops[op] = ops.get(op, 0) + 1
    return ops
