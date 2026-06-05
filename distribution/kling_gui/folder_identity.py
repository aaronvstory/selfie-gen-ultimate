"""Per-folder stable identity markers.

Single source of truth for the ``.selfie_session_id.json`` marker that lets the
app track a *working folder* by an embedded, rename-immune ID instead of by its
name. The marker travels with the folder when the user renames it (their normal
workflow — rename a folder when a shoot is finished), so the session keyed by
that ID stays linked. The same folder opened on macOS or Windows (e.g. on a
shared/synced drive) yields the same ID, so it maps to one Session Manager entry.

Pure stdlib so it can be imported from ``path_utils`` and ``session_manager``
without an import cycle. Every public function is best-effort: a read-only or
unreachable folder must never crash a generate — failures degrade to ``None`` /
an empty result and are logged at debug.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
import uuid
from datetime import datetime
from typing import Dict, Iterable, Optional

logger = logging.getLogger(__name__)

#: Filename of the in-folder identity marker. Hidden (leading dot) on both OSes,
#: tiny, and NOT a recognized media extension so the app's own image scan ignores
#: it.
MARKER_NAME = ".selfie_session_id.json"

#: Prefix on the generated id so a project_key derived from it is visually
#: distinguishable from a legacy name-based key in logs and filenames.
_ID_PREFIX = "sg-"


def _marker_path(folder: str) -> str:
    return os.path.join(folder, MARKER_NAME)


def _new_id() -> str:
    return _ID_PREFIX + uuid.uuid4().hex


def read_folder_id(folder: str) -> Optional[str]:
    """Return the embedded folder ID, or ``None``.

    Never writes. Tolerant of a missing or corrupt marker — both return
    ``None`` (a corrupt marker is healed lazily by the next
    :func:`ensure_folder_id`, not here, so read paths stay side-effect-free).
    """
    if not folder:
        return None
    path = _marker_path(folder)
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as exc:
        logger.debug("read_folder_id: unreadable marker %s: %s", path, exc)
        return None
    fid = data.get("id") if isinstance(data, dict) else None
    if isinstance(fid, str) and fid.strip().startswith(_ID_PREFIX):
        return fid.strip()
    return None


def ensure_folder_id(folder: str, *, seed_name: Optional[str] = None) -> Optional[str]:
    """Return the folder's ID, writing a fresh marker if none exists.

    Idempotent: an existing valid marker is returned untouched. A missing
    (or corrupt) marker is replaced with a new ``uuid4``-based ID via an atomic
    write (temp file + ``os.replace`` — atomic on Windows and POSIX). Returns
    ``None`` on any OS error (e.g. the folder is read-only or on an unreachable
    network mount); callers treat that as "no stable id available" and fall back
    to name-based keying, never crashing.

    ``seed_name`` is stored for human debuggability only — identity is the uuid.
    """
    if not folder or not os.path.isdir(folder):
        return None
    existing = read_folder_id(folder)
    if existing:
        return existing
    fid = _new_id()
    payload = {
        "id": fid,
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "seed_name": seed_name or os.path.basename(os.path.normpath(folder)),
    }
    try:
        _atomic_write_marker(folder, payload)
    except OSError as exc:
        logger.debug("ensure_folder_id: could not write marker in %s: %s", folder, exc)
        return None
    return fid


def _atomic_write_marker(folder: str, payload: dict) -> None:
    """Atomically write the marker into ``folder`` (temp + os.replace)."""
    fd, tmp = tempfile.mkstemp(prefix=".sg_id_", suffix=".tmp", dir=folder)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, indent=2, ensure_ascii=False)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, _marker_path(folder))
    except Exception:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise


def index_live_folder_ids(roots: Iterable[str]) -> Dict[str, str]:
    """Map ``embedded-id -> folder_path`` for every folder under *roots*.

    Scans each path in ``roots`` and its immediate child directories for a
    marker, building a reverse index used to locate a *renamed* folder by its
    ID. Best-effort: unreadable directories are skipped. On a duplicate ID
    (two folders carrying the same marker — e.g. a folder was copied), the
    first one found wins; this is rare and either folder is a valid re-link
    target.

    Only descends one level (``roots`` + their direct children) to bound cost —
    the caller passes the parent dirs of a session's known folders, so the
    renamed sibling sits exactly one level down.
    """
    index: Dict[str, str] = {}
    seen_dirs: set = set()

    def _consider(folder: str) -> None:
        try:
            real = os.path.normcase(os.path.abspath(folder))
        except (OSError, ValueError):
            return
        if real in seen_dirs:
            return
        seen_dirs.add(real)
        fid = read_folder_id(folder)
        if fid and fid not in index:
            index[fid] = folder

    for root in roots:
        if not root:
            continue
        _consider(root)
        # EAFP (Gemini MED, PR #75): scandir is already guarded, so skip the
        # redundant isdir() pre-stat — a non-dir/missing root just raises here
        # and is logged + skipped.
        try:
            entries = list(os.scandir(root))
        except OSError as exc:
            logger.debug("index_live_folder_ids: scandir(%s) failed: %s", root, exc)
            continue
        for entry in entries:
            try:
                if entry.is_dir():
                    _consider(entry.path)
            except OSError:
                continue
    return index
