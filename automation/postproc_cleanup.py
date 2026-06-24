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


def prune_strict_intermediates(
    cache_values: Iterable,
    keep: Iterable,
    *,
    on_pruned: Optional[Callable[[str], None]] = None,
) -> List[str]:
    """Delete cached step outputs not present in ``keep``.

    Accepts ``str`` or ``Path`` for both arguments (the GUI caches ``str``, the
    CLI caches ``Path``). Best-effort: never raises — a file already gone or
    locked is skipped. Returns the basenames actually deleted.

    In full powerset mode every cached prefix is itself a recipe's final output,
    so ``keep`` covers everything and this is a no-op; it only reclaims true
    stepping-stone files that appear in reduced configurations.
    """
    keep_resolved = {str(k) for k in keep}
    pruned: List[str] = []
    for value in {str(v) for v in cache_values}:
        if value in keep_resolved:
            continue
        try:
            p = Path(value)
            if p.exists():
                p.unlink()
                pruned.append(p.name)
                if on_pruned is not None:
                    on_pruned(p.name)
        except OSError:
            pass
    return pruned
