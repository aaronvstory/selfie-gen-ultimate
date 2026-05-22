"""Pipeline tag derivation and filename generation from structured ops."""

import os
import re
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


def build_expand_filenames(base_stem: str, ext: str, gen_dir, do_2x: bool):
    """Plan deterministic output paths for a Step 0 Generative Expand run.

    Returns ``(pass1_path, pass2_path_or_None)`` as ``pathlib.Path`` objects.

    Naming: pass 1 -> ``<stem>-expanded<ext>``; pass 2 (only when
    ``do_2x``) -> ``<stem>-expanded-2x<ext>``. Collision suffixes are
    PAIRED in 2x mode — pass 1 and pass 2 share the same ``_vN`` index so
    a re-run's outputs stay semantically linked on disk (per code-review
    M2 on subagent ae2dd01f). Without pairing, pass 1 could land at
    ``_v2`` while pass 2 lands at ``_v3`` (or vice-versa) and the
    "this 2x belongs to that 1x" relationship is lost in the gen dir.

    Single-pass mode resolves the one path independently as before.
    """
    from pathlib import Path

    gen_dir = Path(gen_dir)
    stem = sanitize_stem(base_stem, default="image")
    if not ext.startswith("."):
        ext = "." + ext

    def _name(base: str, n: int) -> Path:
        if n == 1:
            return gen_dir / f"{base}{ext}"
        return gen_dir / f"{base}_v{n}{ext}"

    base1 = f"{stem}-expanded"
    base2 = f"{stem}-expanded-2x"

    if not do_2x:
        n = 1
        while _name(base1, n).exists():
            n += 1
        return _name(base1, n), None

    # Paired resolution: smallest n where BOTH targets are free.
    n = 1
    while _name(base1, n).exists() or _name(base2, n).exists():
        n += 1
    return _name(base1, n), _name(base2, n)


def _parse_legacy_filename(filename: str) -> dict:
    """Fallback: parse old-format filenames for backward compat."""
    stem = os.path.splitext(filename)[0].lower()
    ops = {}
    for m in _OLD_OP_RE.finditer(stem):
        op = _OLD_OP_MAP[m.group(1)]
        ops[op] = ops.get(op, 0) + 1
    return ops
