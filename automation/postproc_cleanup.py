"""Post-processing fan-out cleanup helpers (I/O side — kept OUT of the pure
``postproc_plan`` planner).

The powerset fan-out caches every intermediate step output so sibling recipes
can share a common prefix (e.g. ``crush`` feeds both ``crush`` and
``crush->oldcam``). After the fan-out completes, cached files that no recipe
claimed as a final deliverable are pure stepping-stones — safe to delete to
reclaim disk + ease memory pressure. The real deliverables, the raw Kling base,
and the rPPG seed are always preserved by the caller via ``keep``.

A real macOS run was SIGKILL'd by jetsam right after a 14-variant fan-out; this
trimming is part of lowering that peak footprint (the launcher now also reports
such kills honestly instead of "GUI startup failed").
"""
from __future__ import annotations

from pathlib import Path
from typing import Callable, Iterable, List, Optional


def _norm_key(pathlike) -> str:
    """Canonical comparison key for a path, robust across OSes.

    The GUI caches paths as ``str`` (possibly with forward slashes) while the
    CLI caches ``Path`` (backslashes on Windows). Comparing raw ``str(k)`` would
    miss a match between the two forms and could delete a real deliverable.
    ``resolve()`` normalizes separators, case-folds on case-insensitive
    filesystems, and reconciles relative/absolute forms; ``absolute()`` is the
    fallback when the path can't be resolved (restricted FS)."""
    try:
        return str(Path(pathlike).resolve())
    except OSError:
        return str(Path(pathlike).absolute())


def prune_strict_intermediates(
    cache_values: Iterable,
    keep: Iterable,
    *,
    on_pruned: Optional[Callable[[str], None]] = None,
) -> List[str]:
    """Delete cached step outputs not present in ``keep``.

    Accepts ``str`` or ``Path`` for both arguments (the GUI caches ``str``, the
    CLI caches ``Path``); both are normalized to a canonical key before
    comparison so a forward-slash string and a backslash ``Path`` to the SAME
    file still match — a real deliverable is never deleted on a separator/case
    mismatch. Best-effort + EAFP: a file already gone or locked is skipped.
    Returns the basenames actually deleted.

    In full powerset mode every cached prefix is itself a recipe's final output,
    so ``keep`` covers everything and this is a no-op; it only reclaims true
    stepping-stone files that appear in reduced configurations.
    """
    keep_keys = {_norm_key(k) for k in keep}
    pruned: List[str] = []
    seen: set = set()
    for value in cache_values:
        key = _norm_key(value)
        if key in keep_keys or key in seen:
            continue
        seen.add(key)
        p = Path(value)
        try:
            p.unlink()  # EAFP: don't pre-check exists() (TOCTOU + restricted FS)
        except OSError:
            continue
        pruned.append(p.name)
        if on_pruned is not None:
            on_pruned(p.name)
    return pruned
