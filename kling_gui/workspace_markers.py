"""Liveness markers for concurrent GUI instances (PR #49).

Each running GUI window registers a small JSON file under
``<workspace_dir>/runtime/.markers/<instance_id>.json`` so other instances
(and post-mortem debugging) can enumerate "what's running in workspace X".
The marker is deleted on clean exit and also via :func:`cleanup_stale_markers`
on launch (catches kill -9 and crashes that bypass ``_on_close``).

All filesystem ops are wrapped in broad try/except — a marker failure must
never break a GUI launch. The markers are diagnostic and best-effort.
"""

from __future__ import annotations

import json
import logging
import os
import time
from datetime import datetime
from typing import List, Optional

import path_utils

logger = logging.getLogger(__name__)

# Markers older than this are presumed orphaned (process crashed / kill -9
# without _on_close). Conservative — a real GUI session can last a full day.
_STALE_SECONDS = 24 * 60 * 60


def _marker_path(workspace: str, instance_id: str) -> str:
    return os.path.join(path_utils.get_workspace_markers_dir(workspace), f"{instance_id}.json")


def register_instance(
    workspace: str,
    instance_id: str,
    runtime_dir: str,
) -> Optional[str]:
    """Write the liveness marker for this process. Returns marker path or None.

    Best-effort: any failure (permission denied, disk full, dir missing) logs
    a debug line and returns ``None``. Caller treats ``None`` as "no marker
    available for release" and proceeds normally.
    """
    try:
        markers_dir = path_utils.get_workspace_markers_dir(workspace)
        os.makedirs(markers_dir, exist_ok=True)
        path = _marker_path(workspace, instance_id)
        payload = {
            "instance_id": instance_id,
            "workspace": workspace,
            "pid": os.getpid(),
            "started_at": datetime.now().isoformat(timespec="seconds"),
            "cwd": os.getcwd(),
            "runtime_dir": runtime_dir,
        }
        # Plain write (not atomic): markers are small + best-effort; a torn
        # marker on crash is harmless — cleanup_stale_markers will sweep it.
        with open(path, "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2)
        return path
    except Exception as exc:
        logger.debug("workspace_markers.register_instance failed: %s", exc)
        return None


def release_instance(marker_path: Optional[str]) -> None:
    """Delete the marker file written by ``register_instance``.

    No-op when ``marker_path`` is None (registration failed) or the file is
    already gone (kill -9 sequence raced ``atexit`` against the OS).
    """
    if not marker_path:
        return
    try:
        os.remove(marker_path)
    except FileNotFoundError:
        pass
    except OSError as exc:
        logger.debug("workspace_markers.release_instance(%s) failed: %s", marker_path, exc)


def list_active_instances(workspace: str) -> List[dict]:
    """Return the contents of every non-stale marker for ``workspace``.

    Each dict has at least the keys written by ``register_instance``
    (``instance_id``, ``workspace``, ``pid``, ``started_at``, ``cwd``,
    ``runtime_dir``). A corrupt/unreadable marker is skipped (logged at
    debug). Stale markers (mtime > 24h) are excluded but NOT deleted here —
    use ``cleanup_stale_markers`` for that.
    """
    out: List[dict] = []
    try:
        markers_dir = path_utils.get_workspace_markers_dir(workspace)
        if not os.path.isdir(markers_dir):
            return out
        cutoff = time.time() - _STALE_SECONDS
        for entry in os.scandir(markers_dir):
            if not entry.is_file() or not entry.name.endswith(".json"):
                continue
            try:
                if entry.stat().st_mtime < cutoff:
                    continue
                with open(entry.path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                if isinstance(data, dict):
                    out.append(data)
            except (OSError, json.JSONDecodeError) as exc:
                logger.debug("Skipping bad marker %s: %s", entry.path, exc)
    except Exception as exc:
        logger.debug("workspace_markers.list_active_instances failed: %s", exc)
    return out


def cleanup_stale_markers(workspace: str) -> int:
    """Delete markers older than the stale cutoff. Returns the count removed.

    Called from gui_launcher early in startup so a kill-9'd predecessor doesn't
    pollute the active-instance count indefinitely. Conservative cutoff — only
    sweeps after 24h, longer than any plausible single GUI session.
    """
    removed = 0
    try:
        markers_dir = path_utils.get_workspace_markers_dir(workspace)
        if not os.path.isdir(markers_dir):
            return 0
        cutoff = time.time() - _STALE_SECONDS
        for entry in os.scandir(markers_dir):
            if not entry.is_file() or not entry.name.endswith(".json"):
                continue
            try:
                if entry.stat().st_mtime < cutoff:
                    os.remove(entry.path)
                    removed += 1
            except OSError as exc:
                logger.debug("Failed removing stale marker %s: %s", entry.path, exc)
    except Exception as exc:
        logger.debug("workspace_markers.cleanup_stale_markers failed: %s", exc)
    return removed
