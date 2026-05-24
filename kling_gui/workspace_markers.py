"""Liveness markers for concurrent GUI instances (PR #49).

Each running GUI window registers a small JSON file under
``<workspace_dir>/runtime/.markers/<instance_id>.json`` so other instances
(and post-mortem debugging) can enumerate "what's running in workspace X".
The marker is deleted on clean exit and also via :func:`cleanup_stale_markers`
on launch (catches kill -9 and crashes that bypass ``_on_close``).

Round-2 (review finding H-2): liveness is determined by **PID probe**, not
``mtime``. The earlier 24h-mtime cutoff would delete still-active markers
from sessions running >24h — this app legitimately has overnight oldcam /
rPPG batches that exceed that window. The PID probe correctly classifies
a process as alive iff its PID is reachable via ``os.kill(pid, 0)``
(POSIX) / ``OpenProcess`` (Windows). A very-old mtime is now an ADDITIONAL
fallback only when the marker has no usable pid (corrupt/legacy).

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

# Fallback mtime cutoff for markers MISSING a usable pid (corrupt JSON, legacy
# pre-PID-probe markers from older releases, etc.). Made very generous —
# 30 days — because the primary liveness signal is now PID, not age.
_FALLBACK_STALE_SECONDS = 30 * 24 * 60 * 60


def _pid_is_alive(pid: int) -> bool:
    """Return True iff ``pid`` corresponds to a running process.

    Cross-platform: ``os.kill(pid, 0)`` works on POSIX (sends signal 0,
    which is a no-op but raises if the process doesn't exist).
    On Windows, ``os.kill`` with signal 0 raises ``OSError`` for both
    "no such process" and "permission denied" — we need ``OpenProcess``
    via ctypes to disambiguate. PID 0 / negative pids are never alive.

    Conservative on failure: returns True (don't sweep a marker we can't
    classify; let the fallback mtime path handle truly old ones).

    Known edge case (PID recycling): on a long-running system, an OS may
    recycle an exited process's PID for an unrelated new process before
    the old marker is swept. This function then classifies the marker as
    still alive (the PID exists, just for a different process) and
    suppresses cleanup. Note: ``_marker_is_alive`` early-returns on this
    function's result when the marker JSON carries a usable pid, so the
    ``_FALLBACK_STALE_SECONDS`` mtime path does NOT catch the recycled-PID
    case — only no-pid / corrupt markers fall through to mtime. A stuck
    marker would persist until the workspace's markers dir is manually
    cleared, or until the recycled process itself exits. In practice this
    is rare (PID space is wide on macOS+Linux; Windows PID recycling is
    real but typically slow). Fixing it precisely would require a
    stable per-launch token (timestamp+nonce) in the marker JSON, which
    is more bookkeeping than the failure mode warrants.
    """
    if pid <= 0:
        return False
    if os.name == "nt":
        try:
            import ctypes
            PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
            kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
            handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
            if not handle:
                # Could be ERROR_INVALID_PARAMETER (no such process) or
                # ERROR_ACCESS_DENIED (process exists but we lack rights).
                # GetLastError disambiguates: 5 = access denied (alive),
                # 87 = invalid parameter (dead).
                err = kernel32.GetLastError()
                if err == 5:  # access denied → process exists
                    return True
                return False
            # Got a handle — process is alive. Close it.
            kernel32.CloseHandle(handle)
            return True
        except Exception as exc:
            logger.debug("PID probe failed for pid=%s: %s", pid, exc)
            return True  # conservative: don't sweep
    # POSIX path
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        # Process exists but we lack permission to signal it (different uid).
        return True
    except OSError as exc:
        logger.debug("os.kill probe failed for pid=%s: %s", pid, exc)
        return True


def _marker_is_alive(marker_path: str, marker_data: Optional[dict] = None) -> bool:
    """Return True iff the process described by the marker is still running.

    Reads the marker JSON if not provided. Falls back to mtime check
    (``_FALLBACK_STALE_SECONDS``) when the marker has no usable pid —
    corrupt/legacy markers eventually get swept regardless.
    """
    if marker_data is None:
        try:
            with open(marker_path, "r", encoding="utf-8") as f:
                marker_data = json.load(f)
        except (OSError, json.JSONDecodeError):
            marker_data = None
    pid = None
    if isinstance(marker_data, dict):
        raw_pid = marker_data.get("pid")
        try:
            pid = int(raw_pid)
        except (TypeError, ValueError):
            pid = None
    if pid is not None:
        return _pid_is_alive(pid)
    # No usable pid — fall back to the generous mtime cutoff for sweeping.
    try:
        mtime = os.path.getmtime(marker_path)
        return (time.time() - mtime) < _FALLBACK_STALE_SECONDS
    except OSError:
        return False


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
    """Return the contents of every still-alive marker for ``workspace``.

    Round-2 H-2: liveness is determined by PID probe (``_marker_is_alive``),
    NOT mtime — this app's legitimate workloads (overnight oldcam batches,
    long rPPG queues) routinely exceed any reasonable mtime window. A
    sibling whose PID is still running is alive regardless of marker age.

    Each returned dict has at least the keys written by ``register_instance``
    (``instance_id``, ``workspace``, ``pid``, ``started_at``, ``cwd``,
    ``runtime_dir``). Markers for dead processes are excluded but NOT
    deleted here — use ``cleanup_stale_markers`` for that.
    """
    out: List[dict] = []
    try:
        markers_dir = path_utils.get_workspace_markers_dir(workspace)
        if not os.path.isdir(markers_dir):
            return out
        for entry in os.scandir(markers_dir):
            if not entry.is_file() or not entry.name.endswith(".json"):
                continue
            try:
                with open(entry.path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                if not isinstance(data, dict):
                    continue
                if _marker_is_alive(entry.path, data):
                    out.append(data)
            except (OSError, json.JSONDecodeError) as exc:
                logger.debug("Skipping bad marker %s: %s", entry.path, exc)
    except Exception as exc:
        logger.debug("workspace_markers.list_active_instances failed: %s", exc)
    return out


# Filenames we expect to find inside an orphan ``runtime/instances/<id>/`` dir
# and feel safe deleting. ``crash_log.txt`` / ``kling_history.json`` / ``sessions``
# are the GUI's own per-instance writes. The remaining names are OS-junk created
# by Finder / Explorer the moment a user browses the folder — round-3 review M1
# called out that without these, a single visit by macOS Finder permanently
# blocks orphan cleanup (a ``.DS_Store`` appears and ``_safe_rmtree_orphan_runtime``
# aborts as "manual user data").
_ORPHAN_EXPECTED_NAMES = {
    "crash_log.txt",
    "kling_history.json",
    "sessions",
    ".DS_Store",      # macOS Finder
    "Thumbs.db",      # Windows Explorer thumbnail cache
    "desktop.ini",    # Windows folder metadata
}


def _is_safe_orphan_runtime_path(runtime_dir: str, workspace: str) -> bool:
    """Return True iff ``runtime_dir`` is structurally a per-instance runtime
    dir under ``workspace``'s ``runtime/instances/`` tree.

    Round-3 review finding (C1): the marker JSON's ``runtime_dir`` field is
    attacker-controlled in the cross-machine sense — a marker rsynced from
    another box, or a hand-edited / corrupted marker, can point at any path.
    Without this check, ``_safe_rmtree_orphan_runtime`` would happily rmtree
    e.g. ``C:\\Users\\d0nbxx\\Desktop\\sessions`` if that path coincidentally
    contained only files matching ``_ORPHAN_EXPECTED_NAMES``.

    Two gates: (1) ``commonpath`` containment under the expected
    ``runtime/instances`` root, AND (2) the basename matches the instance-id
    regex. Both must pass — defense-in-depth against symlinks pointing
    inside the tree.
    """
    try:
        expected_root = os.path.join(
            path_utils.get_workspace_dir(workspace), "runtime", "instances"
        )
        candidate = os.path.realpath(runtime_dir)
        anchor = os.path.realpath(expected_root)
        common = os.path.commonpath([candidate, anchor])
        if os.path.normcase(common) != os.path.normcase(anchor):
            return False
        # Basename of the candidate must look like an instance id
        # (`<YYYYMMDD-HHMMSS>-<PID>`). Re-use the regex defined in path_utils.
        basename = os.path.basename(candidate.rstrip(os.sep))
        if not path_utils._INSTANCE_ID_RE.match(basename):
            return False
        return True
    except (ValueError, OSError) as exc:
        # ValueError: commonpath across drives (Windows). OSError: realpath
        # on a broken symlink. Conservative: classify as unsafe so we never
        # rmtree on uncertainty.
        logger.debug(
            "orphan-runtime safety check failed for %s under %s: %s",
            runtime_dir, workspace, exc,
        )
        return False


def _safe_rmtree_orphan_runtime(runtime_dir: str, workspace: str) -> None:
    """Remove an orphaned per-instance runtime dir, but only if it's safe.

    Round-2 review finding (Gemini): the original cleanup_stale_markers
    deleted marker files but left orphan ``runtime/instances/<id>/`` dirs
    forever — each abandoned instance leaks a few KB to MB of carousel
    autosave + history + crash log on disk.

    Round-3 review finding (C1): added a structural containment check —
    ``runtime_dir`` MUST resolve under the workspace's ``runtime/instances/``
    tree AND its basename must match the instance-id regex. Without this,
    a cross-machine marker file (rsync'd, cloud-synced) could point at an
    arbitrary path on this host that happens to contain only the expected
    ephemerals, and we'd rmtree the wrong dir.

    Safety rules (all must pass for rmtree):
      1. Path containment via ``_is_safe_orphan_runtime_path``
      2. Directory contents are entirely in ``_ORPHAN_EXPECTED_NAMES``
         (the GUI's own files plus OS-junk like ``.DS_Store`` so a single
         Finder visit doesn't permanently block cleanup)
    Best-effort on all filesystem errors.
    """
    if not _is_safe_orphan_runtime_path(runtime_dir, workspace):
        logger.debug(
            "Skipping orphan rmtree of %s: not under workspace %r runtime/instances/",
            runtime_dir, workspace,
        )
        return
    try:
        if not os.path.isdir(runtime_dir):
            return
        entries = list(os.scandir(runtime_dir))
        for entry in entries:
            if entry.name not in _ORPHAN_EXPECTED_NAMES:
                logger.debug(
                    "Skipping rmtree of %s: unexpected entry %r — manual user data?",
                    runtime_dir, entry.name,
                )
                return
        # All entries are expected ephemerals — safe to rmtree.
        import shutil
        shutil.rmtree(runtime_dir, ignore_errors=True)
        logger.debug("Removed orphan runtime dir: %s", runtime_dir)
    except OSError as exc:
        logger.debug("Failed orphan rmtree on %s: %s", runtime_dir, exc)


def cleanup_stale_markers(workspace: str) -> int:
    """Delete markers whose process is dead, plus their orphan runtime dirs.

    Returns the count of markers removed (orphan dir removal is best-effort
    and not counted separately — a marker successfully removed but a dir
    skipped for safety still increments the count).

    Round-2 H-2: switched from a 24h mtime cutoff to a per-marker PID probe.
    The old logic would delete the marker of any sibling session running
    >24h (overnight oldcam batches, multi-hour rPPG queues) — making the
    new sibling's heads-up log silent and breaking the "is anyone else
    running" contract. PID probe correctly classifies an active long-running
    window as alive regardless of marker age.

    Round-2 (Gemini review): also rmtree the orphan
    ``runtime/instances/<id>/`` dir if it contains only the expected
    ephemeral files (crash log, history, sessions subtree). See
    :func:`_safe_rmtree_orphan_runtime` for the safety check.

    Called from gui_launcher early in startup so a kill-9'd predecessor
    doesn't pollute the active-instance count indefinitely.
    """
    removed = 0
    try:
        markers_dir = path_utils.get_workspace_markers_dir(workspace)
        if not os.path.isdir(markers_dir):
            return 0
        for entry in os.scandir(markers_dir):
            if not entry.is_file() or not entry.name.endswith(".json"):
                continue
            try:
                if _marker_is_alive(entry.path):
                    continue
                # Read the marker to find its runtime_dir BEFORE deleting it.
                runtime_dir = None
                try:
                    with open(entry.path, "r", encoding="utf-8") as f:
                        data = json.load(f)
                    if isinstance(data, dict):
                        runtime_dir = data.get("runtime_dir")
                except (OSError, json.JSONDecodeError):
                    pass
                os.remove(entry.path)
                removed += 1
                if isinstance(runtime_dir, str) and runtime_dir:
                    _safe_rmtree_orphan_runtime(runtime_dir, workspace)
            except OSError as exc:
                logger.debug("Failed removing stale marker %s: %s", entry.path, exc)
    except Exception as exc:
        logger.debug("workspace_markers.cleanup_stale_markers failed: %s", exc)
    return removed
