"""Session persistence — save / load / list / delete session snapshots."""

import os
import json
import re
import hashlib
import logging
import tempfile
from datetime import datetime
from typing import List, Optional, NamedTuple

from path_utils import _walk_up_past_gen_folders, VALID_EXTENSIONS

logger = logging.getLogger(__name__)
SESSION_VERSION = 2
SESSION_KIND_MANUAL = "manual"
SESSION_KIND_AUTOSAVE = "autosave"
AUTOSAVE_RETENTION_DEFAULT = 10


class SessionRecord(NamedTuple):
    name: str
    path: str
    timestamp: str  # ISO format (legacy alias for updated_at)
    created_at: str
    updated_at: str
    session_kind: str
    project_key: str
    image_count: int


def _get_sessions_dir(app_dir: str, *, sessions_dir_override: Optional[str] = None) -> str:
    """Return the sessions dir, optionally overridden.

    When ``sessions_dir_override`` is provided, it is used verbatim — bypasses
    the ``app_dir/sessions`` convention. Used by per-instance autosaves which
    live under ``runtime/instances/<id>/sessions/`` so two concurrent windows
    don't overwrite each other's rolling autosave (PR #49).
    """
    d = sessions_dir_override if sessions_dir_override else os.path.join(app_dir, "sessions")
    os.makedirs(d, exist_ok=True)
    return d


def _iter_extra_sessions_dirs(app_dir: str) -> List[str]:
    """Return per-instance sessions dirs under the **active** workspace.

    Scans ``<workspace_dir>/runtime/instances/*/sessions/`` so the Session
    Manager dialog can aggregate autosaves saved by any sibling instance
    in the SAME workspace as this process. Two instances both running in
    the ``default`` workspace see each other's autosaves; two instances in
    different named workspaces stay separate (the workspace boundary is
    intentional — that's the entire point of named workspaces).

    The ``app_dir`` parameter is unused here — the workspace identity is
    read from the env (``KLING_WORKSPACE``) via ``get_workspace()``, since
    a single legacy ``<app_dir>/sessions/`` can serve multiple workspaces
    in some edge cases. Kept in the signature for symmetry with other
    ``session_manager`` helpers that DO take ``app_dir``.

    Returns absolute paths. Silently returns ``[]`` on any filesystem error
    — the caller (``list_sessions``) treats this dir set as best-effort.
    """
    del app_dir  # unused — workspace identity comes from env
    dirs: List[str] = []
    try:
        from path_utils import get_workspace_dir, get_workspace
        ws = get_workspace()
        instances_root = os.path.join(get_workspace_dir(ws), "runtime", "instances")
        if not os.path.isdir(instances_root):
            return dirs
        for entry in os.scandir(instances_root):
            if not entry.is_dir():
                continue
            sessions = os.path.join(entry.path, "sessions")
            if os.path.isdir(sessions):
                dirs.append(sessions)
    except Exception as exc:
        logger.debug("Failed enumerating per-instance sessions dirs: %s", exc)
    return dirs


def _atomic_write_json(path: str, data: dict) -> None:
    """Write ``data`` as JSON to ``path`` atomically.

    Serialize to a temp file in the same directory, flush+fsync, then
    ``os.replace`` onto the target — atomic on both Windows and POSIX, so a
    crash mid-write can never leave the destination (the only rolling
    autosave) truncated. On any failure the temp file is removed and the
    pre-existing target is left untouched.
    """
    directory = os.path.dirname(path) or "."
    fd, tmp_path = tempfile.mkstemp(
        prefix=".tmp_", suffix=".json", dir=directory
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, path)
    except Exception:
        try:
            os.remove(tmp_path)
        except OSError:
            pass
        raise


def _sanitize_name(name: str) -> str:
    return re.sub(r"[^a-zA-Z0-9_\-]", "_", name).strip("_")[:80] or "session"


def _resolve_session_folder(image_session) -> str:
    """Get the real project folder name from session images, walking up past gen-images/gen-videos."""
    ref = image_session.reference_entry
    if ref:
        path = ref.path
    else:
        inputs = image_session.input_images
        if inputs:
            path = inputs[0][1].path
        else:
            images = image_session.images
            path = images[0].path if images else None
    if not path:
        return "untitled"
    project_dir = _walk_up_past_gen_folders(path)
    return os.path.basename(project_dir)


def _now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _file_mtime_iso(path: str) -> str:
    try:
        return datetime.fromtimestamp(os.path.getmtime(path)).isoformat(timespec="seconds")
    except Exception:
        return _now_iso()


def _normalize_session_kind(raw_kind: Optional[str], filename: str) -> str:
    kind = str(raw_kind or "").strip().lower()
    if kind in {SESSION_KIND_MANUAL, SESSION_KIND_AUTOSAVE}:
        return kind
    stem = os.path.splitext(filename)[0].lower()
    return SESSION_KIND_AUTOSAVE if "_autosave" in stem else SESSION_KIND_MANUAL


def _infer_project_key(data: dict, filename: str) -> str:
    project_key = _sanitize_name(str(data.get("project_key", "")).strip())
    if project_key and project_key != "session":
        return project_key
    stem = os.path.splitext(filename)[0]
    match = re.match(r"^(?P<project>.+?)_autosave(?:_\d{8}_\d{6}(?:_\d+)?)?$", stem)
    if match:
        inferred = _sanitize_name(match.group("project"))
        if inferred:
            return inferred
    fallback_name = _sanitize_name(str(data.get("name", "")).strip())
    return fallback_name if fallback_name else "untitled"


def get_project_key(image_session) -> str:
    """Return sanitized project key for this working image session."""
    return _sanitize_name(_resolve_session_folder(image_session))


def _build_autosave_name(project_key: str) -> str:
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    return f"{project_key}_autosave_{stamp}"


def get_autosave_path(app_dir: str, image_session) -> str:
    """DEPRECATED: timestamped autosave path (one file per save).

    Superseded by :func:`get_rolling_autosave_path` (one rolling file per
    project). Retained only so any external caller keeps working.
    """
    sessions_dir = _get_sessions_dir(app_dir)
    project_key = get_project_key(image_session)
    base = _build_autosave_name(project_key)
    path = os.path.join(sessions_dir, f"{base}.json")
    counter = 2
    while os.path.exists(path):
        path = os.path.join(sessions_dir, f"{base}_{counter}.json")
        counter += 1
    return path


def get_rolling_autosave_path(
    app_dir: str,
    project_key: str,
    *,
    sessions_dir_override: Optional[str] = None,
) -> str:
    """Return the single deterministic autosave path for a project.

    One rolling file per project (``{project_key}_autosave.json``), overwritten
    in place — no timestamp, no accumulation. The no-timestamp stem is still
    matched by ``_infer_project_key``'s regex (the timestamp group is optional).

    When ``sessions_dir_override`` is set, the autosave goes into that dir
    instead of ``app_dir/sessions/`` — used by per-instance runtime isolation
    (PR #49) so two concurrent windows working on the same project_key don't
    overwrite each other's rolling autosave.
    """
    sessions_dir = _get_sessions_dir(app_dir, sessions_dir_override=sessions_dir_override)
    return os.path.join(sessions_dir, f"{_sanitize_name(project_key)}_autosave.json")


def compute_session_fingerprint(image_session) -> str:
    """Stable hash of the session's content (ignores wall-clock save times).

    Used to skip autosave writes when nothing meaningful changed. Built from
    ``image_session.to_dict()`` (paths, source types, labels, ops, similarity
    state, indices) — none of which is a save timestamp, so an idle timer tick
    or a debounced no-op produces the same digest as the prior save.
    """
    try:
        payload = json.dumps(image_session.to_dict(), sort_keys=True, ensure_ascii=False)
    except Exception:
        # Never let fingerprinting break a save; fall back to a unique value
        # so the caller treats it as "changed" and writes.
        return _now_iso()
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _legacy_autosave_pattern(project_key: str) -> "re.Pattern":
    """Filename regex for a project's autosave files (rolling + timestamped).

    Matches ``{key}_autosave.json`` and ``{key}_autosave_YYYYMMDD_HHMMSS.json``
    (and the ``_N`` collision-counter variant) without opening any file.
    """
    return re.compile(
        rf"^{re.escape(_sanitize_name(project_key))}_autosave"
        r"(?:_\d{8}_\d{6}(?:_\d+)?)?\.json$",
        re.IGNORECASE,
    )


def _purge_legacy_autosaves(
    app_dir: str,
    project_key: str,
    keep_path: str,
    *,
    sessions_dir_override: Optional[str] = None,
) -> int:
    """Delete every autosave for ``project_key`` except the rolling file.

    Filename-only matching (no JSON parsing) so this stays cheap on the
    autosave hot path even when the directory holds many manual sessions.
    Case-insensitive path compare (Windows) so the rolling file is never
    deleted as its own legacy sibling. Manual saves are untouched.

    When ``sessions_dir_override`` is set, purges within that dir instead
    of ``app_dir/sessions/`` (PR #49). The per-instance rolling file lives
    under the override; its legacy siblings (timestamped autosaves from a
    pre-PR #49 release) live in the legacy shared dir and are left alone.
    """
    removed = 0
    sessions_dir = _get_sessions_dir(app_dir, sessions_dir_override=sessions_dir_override)
    keep_norm = os.path.normcase(os.path.abspath(keep_path))
    pattern = _legacy_autosave_pattern(project_key)
    try:
        entries = list(os.scandir(sessions_dir))
    except OSError as exc:
        logger.warning("Failed scanning sessions dir %s: %s", sessions_dir, exc)
        return 0
    for entry in entries:
        if not entry.is_file() or not pattern.match(entry.name):
            continue
        if os.path.normcase(os.path.abspath(entry.path)) == keep_norm:
            continue
        try:
            os.remove(entry.path)
            removed += 1
        except Exception as exc:
            logger.warning("Failed purging legacy autosave %s: %s", entry.path, exc)
    return removed


def collapse_legacy_autosaves(app_dir: str) -> int:
    """One-shot migration: collapse each project's autosave pile to one file.

    For every project that still has timestamped autosaves, keep the newest,
    rewrite it to the deterministic rolling path, and delete the rest.
    Idempotent — safe to call repeatedly. Returns the number of files removed.

    One ``list_sessions`` scan up front (this runs once, on first dialog open,
    so parsing is acceptable); the per-project purge is filename-only, so the
    overall cost is O(N), not O(N²).
    """
    removed = 0
    autosaves = [r for r in list_sessions(app_dir) if r.session_kind == SESSION_KIND_AUTOSAVE]
    by_project: dict = {}
    for rec in autosaves:
        by_project.setdefault(rec.project_key, []).append(rec)

    for project_key, recs in by_project.items():
        # list_sessions is already newest-first; recs preserves that order.
        rolling_path = get_rolling_autosave_path(app_dir, project_key)
        rolling_norm = os.path.normcase(os.path.abspath(rolling_path))
        newest = recs[0]
        if os.path.normcase(os.path.abspath(newest.path)) != rolling_norm:
            try:
                with open(newest.path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                data["project_key"] = _sanitize_name(project_key)
                data["session_kind"] = SESSION_KIND_AUTOSAVE
                with open(rolling_path, "w", encoding="utf-8") as f:
                    json.dump(data, f, indent=2, ensure_ascii=False)
            except Exception as exc:
                logger.warning("Failed collapsing autosave for %s: %s", project_key, exc)
                continue
        removed += _purge_legacy_autosaves(app_dir, project_key, rolling_path)
    if removed:
        logger.info("Collapsed %d legacy autosave file(s)", removed)
    return removed


_GEN_FOLDER_SEGMENTS = {"gen-images", "gen-videos"}
# Expansion / outpaint markers (checked before the generic selfie bucket).
_OUTPAINT_MARKERS = ("_exp", "_outpaint", "_expanded", "-expanded", " - exp")


def _classify_source_type(path: str) -> str:
    """Infer an ImageEntry source_type for a scanned file.

    The designed project layout puts every *generated* artifact under a
    ``gen-images/`` (or ``gen-videos/``) subfolder; true source inputs
    (e.g. ``front.jpg``) sit at the project root. Hard-coding everything
    to ``"input"`` (the old behaviour) made the similarity recalc find
    zero targets after a folder-load, because the target filter is
    ``source_type != "input"``.

    Rules:
      - Anything NOT inside a gen-* folder  -> ``"input"`` (source img).
      - The extracted crop (``*_crop`` with no further gen suffix), even
        inside gen-images/, stays ``"input"`` so it can serve as the
        auto similarity reference (``get_effective_similarity_ref``
        prefers a ``_crop`` input).
      - Expanded / outpaint artifacts        -> ``"outpaint"``.
      - Every other gen-folder artifact      -> ``"selfie"``.
    """
    norm = path.replace("\\", "/").lower()
    parts = norm.split("/")
    in_gen_folder = any(seg in _GEN_FOLDER_SEGMENTS for seg in parts[:-1])
    if not in_gen_folder:
        return "input"
    stem = os.path.splitext(os.path.basename(norm))[0]
    # The extracted crop is the reference, not a generated target. Treat
    # a bare "*_crop" / "*-crop" (no later generation suffix) as input.
    if stem.endswith("_crop") or stem.endswith("-crop") or stem == "crop":
        return "input"
    if any(m in stem for m in _OUTPAINT_MARKERS):
        return "outpaint"
    return "selfie"


def build_session_from_folder(folder: str, max_images: int = 500) -> Optional[dict]:
    """Scan ``folder`` recursively for recognized images → ad-hoc session dict.

    Shaped exactly like a loaded session file so the existing restore path
    consumes it unchanged. Primary use: recover a project whose folder was
    renamed (its saved session's absolute paths are now dead). Returns ``None``
    when no recognized images are found.
    """
    files: List[str] = []
    truncated = False
    # Stop walking as soon as the cap is hit — a user who points this at a
    # huge tree (or a drive root) shouldn't hang the UI enumerating it all.
    for root, _dirs, names in os.walk(folder):
        for n in names:
            if os.path.splitext(n)[1].lower() in VALID_EXTENSIONS:
                files.append(os.path.join(root, n))
                if len(files) >= max_images:
                    truncated = True
                    break
        if truncated:
            break
    files.sort(key=lambda p: p.lower())
    if not files:
        return None
    now_iso = _now_iso()
    return {
        "name": os.path.basename(os.path.normpath(folder)) or "folder",
        "timestamp": now_iso,
        "created_at": now_iso,
        "updated_at": now_iso,
        "session_kind": SESSION_KIND_MANUAL,
        "project_key": _sanitize_name(os.path.basename(os.path.normpath(folder))),
        "session_version": SESSION_VERSION,
        "similarity_engine_version": "1.8",
        "_folder_scan_truncated": truncated,
        "session": {
            "images": [
                {
                    "path": p,
                    "source_type": _classify_source_type(p),
                    "label": os.path.basename(p),
                    "ops": {},
                }
                for p in files
            ],
            "current_index": 0,
            "reference_index": 0,
            "similarity_ref_index": -1,
        },
    }


def _derive_session_name(image_session) -> str:
    """Auto-derive a name from the session's source folder + timestamp."""
    folder_name = _resolve_session_folder(image_session)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    return f"{_sanitize_name(folder_name)}_{ts}"


def _instance_tag_for_dir(directory: str) -> str:
    """Return a short instance-id tag for a session dir, or "" for legacy.

    Per-instance sessions dirs follow the layout
    ``<workspace>/runtime/instances/<instance_id>/sessions/`` — pull the
    ``<instance_id>`` segment so the Session Manager listing can disambiguate
    two siblings' autosaves on the same ``project_key`` (PR #49 M2). Returns
    ``""`` for the legacy ``<app_dir>/sessions/`` so back-compat rows render
    unchanged.
    """
    try:
        normalized = directory.replace("\\", "/").rstrip("/")
        parts = normalized.split("/")
        if len(parts) >= 4 and parts[-1] == "sessions" and parts[-3] == "instances":
            return parts[-2]
    except Exception:
        pass
    return ""


def list_sessions(app_dir: str) -> List[SessionRecord]:
    """Return all saved sessions, sorted newest-first.

    Aggregates two sources (PR #49 — concurrent workspaces):
      1. The legacy shared sessions dir (``<app_dir>/sessions/``) — holds
         manual saves + any pre-PR #49 autosaves.
      2. Every per-instance runtime sessions dir
         (``<workspace_dir>/runtime/instances/*/sessions/``) — holds
         rolling autosaves saved by THIS instance and any sibling instance
         that's still alive or that exited cleanly without sweeping its dir.

    Both sources merged so the Session Manager dialog can browse autosaves
    saved by any window. On duplicate filenames (e.g. same project_key
    autosave file present in two instance dirs), keep the newest-modified.

    Sort design (two intentionally different timestamps):
      * De-dupe tiebreak (above) uses filesystem **mtime** — the ground
        truth for "which file was actually written most recently on this
        machine". Resistant to stale JSON from backup restores or hand
        edits.
      * Final display sort (below) uses the JSON **updated_at** — the
        user-provided save timestamp, preserving the "I saved this at
        time X, show it ordered by X" contract that the Session Manager
        relies on.

    In the rare case these disagree (e.g. backup-restored file whose
    mtime is fresh but ``updated_at`` is months old), the display
    timestamp shows the original save time and the file still loads
    correctly — acceptable trade-off for honoring user intent on sort.
    """
    sessions_dir = _get_sessions_dir(app_dir)
    seen: dict = {}

    def _ingest(directory: str) -> None:
        try:
            entries = os.listdir(directory)
        except OSError as exc:
            logger.debug("list_sessions: scandir(%s) failed: %s", directory, exc)
            return
        for fname in entries:
            if not fname.endswith(".json"):
                continue
            fpath = os.path.join(directory, fname)
            try:
                with open(fpath, "r", encoding="utf-8") as f:
                    data = json.load(f)
                kind = _normalize_session_kind(data.get("session_kind"), fname)
                project_key = _infer_project_key(data, fname)
                mtime_iso = _file_mtime_iso(fpath)
                created_at = str(data.get("created_at") or data.get("timestamp") or mtime_iso)
                updated_at = str(data.get("updated_at") or data.get("timestamp") or mtime_iso)
                # PR #49 M2: if this autosave came from a per-instance runtime
                # dir, tag the displayed name with the instance id so the user
                # can tell two siblings apart in the Session Manager listing.
                # The legacy <app_dir>/sessions/ dir produces no tag (back-compat).
                display_name = data.get("name", fname)
                instance_tag = _instance_tag_for_dir(directory)
                if instance_tag and kind == SESSION_KIND_AUTOSAVE:
                    display_name = f"{display_name}  [{instance_tag}]"
                rec = SessionRecord(
                    name=display_name,
                    path=fpath,
                    timestamp=updated_at,
                    created_at=created_at,
                    updated_at=updated_at,
                    session_kind=kind,
                    project_key=project_key,
                    image_count=len(data.get("session", {}).get("images", [])),
                )
                # De-dupe key: include the directory so two instances on the
                # same project_key both surface (PR #49 M2 — earlier the
                # (kind, fname) key silently hid the older sibling when both
                # windows worked on e.g. "untitled" and produced
                # "untitled_autosave.json" in their respective per-instance
                # dirs, causing the user to load the wrong window's state).
                # Dirname-aware de-dupe keeps each instance's autosave visible
                # while still collapsing genuine in-dir duplicates (which can
                # only happen if a fresh autosave races a stale-file scan).
                key = (kind, fname, os.path.normcase(os.path.abspath(directory)))
                existing = seen.get(key)
                # Round-2 review (CodeRabbit): use filesystem mtime, not the
                # JSON-stored ``updated_at``, for the de-dupe tiebreak. The
                # JSON value can be stale (e.g. a backup tool restored an old
                # file whose mtime is fresh but updated_at is months old) or
                # manually edited. Filesystem mtime is the ground truth for
                # "which file was written most recently on this machine".
                if existing is None:
                    seen[key] = rec
                else:
                    try:
                        new_mtime = os.path.getmtime(fpath)
                        existing_mtime = os.path.getmtime(existing.path)
                    except OSError:
                        # If either file disappeared between scan and stat,
                        # prefer the new record (it definitely existed during
                        # the open() call above).
                        seen[key] = rec
                        continue
                    if new_mtime > existing_mtime:
                        seen[key] = rec
            except Exception:
                logger.warning("Skipping corrupt session file: %s", fname)

    _ingest(sessions_dir)
    for extra in _iter_extra_sessions_dirs(app_dir):
        if os.path.normcase(os.path.abspath(extra)) == os.path.normcase(os.path.abspath(sessions_dir)):
            continue  # Don't double-scan the legacy dir if it happens to equal an instance dir
        _ingest(extra)

    records = list(seen.values())
    records.sort(key=lambda r: r.updated_at, reverse=True)
    return records


def _build_config_snapshot(config: dict) -> dict:
    return {
        k: config.get(k)
        for k in (
            "selfie_selected_models",
            "selfie_prompt_template",
            "selfie_scene_templates",
            "selfie_prompt_mode",
            "selfie_wildcard_template",
            "selfie_id_weight",
            "selfie_width",
            "selfie_height",
        )
        if config.get(k) is not None
    }


def prune_autosaves(app_dir: str, project_key: str, keep: int = AUTOSAVE_RETENTION_DEFAULT) -> int:
    """Delete oldest autosave snapshots for one project beyond the keep limit."""
    if keep < 1:
        keep = 1
    removed = 0
    autosaves = [
        rec for rec in list_sessions(app_dir)
        if rec.session_kind == SESSION_KIND_AUTOSAVE and rec.project_key == project_key
    ]
    for rec in autosaves[keep:]:
        try:
            os.remove(rec.path)
            removed += 1
        except Exception as exc:
            logger.warning("Failed pruning autosave %s: %s", rec.path, exc)
    return removed


def delete_project_sessions(app_dir: str, project_key: str) -> int:
    """Delete all saved sessions (manual + autosave) for one project key."""
    removed = 0
    for rec in list_sessions(app_dir):
        if rec.project_key != project_key:
            continue
        try:
            os.remove(rec.path)
            removed += 1
        except Exception as exc:
            logger.warning("Failed deleting session %s: %s", rec.path, exc)
    return removed


def save_session(
    app_dir: str,
    image_session,
    config: dict,
    name: Optional[str] = None,
    overwrite_path: Optional[str] = None,
    session_kind: str = SESSION_KIND_MANUAL,
    project_key: Optional[str] = None,
    autosave_retention: int = AUTOSAVE_RETENTION_DEFAULT,
    skip_if_unchanged: bool = False,
    *,
    sessions_dir_override: Optional[str] = None,
) -> Optional[str]:
    """Save session to JSON. Returns the saved file path, or None if skipped.

    If overwrite_path is given, overwrites that file in place.
    Autosaves write a single rolling file per project (overwritten in place).
    Manual saves create a new file (auto-increments on collision).

    When ``skip_if_unchanged`` is set (autosave only), the write is skipped and
    ``None`` returned if the rolling file's stored content fingerprint already
    matches the current session — so idle timer ticks / debounced no-ops cost
    nothing and don't churn the file.

    ``autosave_retention`` is retained for backward compatibility with existing
    callers; autosaves are now a single rolling file so there is nothing to
    retain past it (legacy timestamped piles are purged instead).
    """
    _ = autosave_retention  # accepted for back-compat; see docstring
    sessions_dir = _get_sessions_dir(app_dir, sessions_dir_override=sessions_dir_override)
    kind = _normalize_session_kind(session_kind, "")
    effective_project_key = _sanitize_name(project_key or get_project_key(image_session))
    now_iso = _now_iso()
    session_name = name or (
        _build_autosave_name(effective_project_key)
        if kind == SESSION_KIND_AUTOSAVE
        else _derive_session_name(image_session)
    )
    created_at = now_iso
    existing_data = None
    fingerprint = compute_session_fingerprint(image_session)

    if overwrite_path and os.path.isfile(overwrite_path):
        fpath = overwrite_path
        # Preserve original name from existing file
        try:
            with open(fpath, "r", encoding="utf-8") as f:
                existing_data = json.load(f)
            session_name = existing_data.get("name", session_name)
            created_at = str(existing_data.get("created_at") or existing_data.get("timestamp") or now_iso)
        except Exception:
            pass
    else:
        if kind == SESSION_KIND_AUTOSAVE:
            fpath = get_rolling_autosave_path(
                app_dir,
                effective_project_key,
                sessions_dir_override=sessions_dir_override,
            )
            session_name = os.path.splitext(os.path.basename(fpath))[0]
            # Preserve created_at and short-circuit on unchanged content.
            # A corrupt rolling file self-heals: the read fails, we fall
            # through and overwrite it fresh.
            if os.path.isfile(fpath):
                try:
                    with open(fpath, "r", encoding="utf-8") as f:
                        existing_data = json.load(f)
                    created_at = str(
                        existing_data.get("created_at")
                        or existing_data.get("timestamp")
                        or now_iso
                    )
                    if (
                        skip_if_unchanged
                        and existing_data.get("content_fingerprint") == fingerprint
                    ):
                        logger.debug("Autosave skipped (no change): %s", fpath)
                        return None
                except Exception as exc:
                    # Corrupt/unreadable rolling file: surface it, then fall
                    # through and overwrite with a fresh snapshot.
                    logger.warning(
                        "Autosave read failed, rewriting rolling file %s: %s",
                        fpath,
                        exc,
                    )
        else:
            safe = _sanitize_name(session_name)
            fpath = os.path.join(sessions_dir, f"{safe}.json")
            counter = 2
            while os.path.exists(fpath):
                fpath = os.path.join(sessions_dir, f"{safe}_{counter}.json")
                counter += 1

    data = {
        "name": session_name,
        "timestamp": now_iso,
        "created_at": created_at,
        "updated_at": now_iso,
        "session_kind": kind,
        "project_key": effective_project_key,
        "session_version": SESSION_VERSION,
        "content_fingerprint": fingerprint,
        # Stamp the engine version so loaders can detect & invalidate stale (pre-v1.8) scores.
        "similarity_engine_version": "1.8",
        "session": image_session.to_dict(),
        "config_snapshot": _build_config_snapshot(config),
    }

    # Atomic write: serialize to a temp file in the same directory, then
    # os.replace() onto the target. The single rolling autosave is now the
    # only safety net — an in-place write interrupted by a crash would leave
    # it truncated and unrecoverable. os.replace is atomic on Win + POSIX.
    _atomic_write_json(fpath, data)

    if kind == SESSION_KIND_AUTOSAVE:
        # Single rolling file now; keep the legacy timestamped pile from this
        # project tidy (purge anything that isn't the rolling file). Done only
        # after the replace above succeeded, so a failed write never destroys
        # the previous good autosave.
        _purge_legacy_autosaves(
            app_dir,
            effective_project_key,
            fpath,
            sessions_dir_override=sessions_dir_override,
        )

    logger.info("Session saved: %s", fpath)
    return fpath


def load_session(path: str) -> dict:
    """Load a session file, returns the raw dict."""
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def delete_session(path: str) -> None:
    """Delete a session file."""
    if os.path.isfile(path):
        os.remove(path)
        logger.info("Session deleted: %s", path)


# Folder-rescan vocabulary. Imported from the single sources of truth
# (path_utils.VALID_EXTENSIONS for images, image_state._VIDEO_EXTENSIONS
# for videos) so adding a new extension on one side automatically
# updates the liveness classifier. Replaces the prior copy-paste pair
# that was a future-bomb (code-review C2 on 4ddb0252).
from path_utils import VALID_EXTENSIONS as _LIVENESS_IMAGE_EXTS  # noqa: E402
from .image_state import _VIDEO_EXTENSIONS as _LIVENESS_VIDEO_EXTS  # noqa: E402

# Foreign-OS path detection. The user works on a 3-machine mesh
# (DMBP14 macOS, DMBP16 macOS, L3 Windows). Sessions saved on one host
# carry that host's path syntax in the JSON; opened on another, the
# stored paths can't resolve via os.path.isfile. Without this guard,
# a Windows session opened on macOS would classify dead and Prune
# would silently delete it (code-review C1 on 4ddb0252 — silent data
# loss in the worst case).
_WINDOWS_DRIVE_RE = re.compile(r"^[A-Za-z]:[\\/]")
_UNC_PREFIX_RE = re.compile(r"^\\\\[^\\/]+[\\/]")


def _is_foreign_path(path: str) -> bool:
    """Return True if ``path`` was saved by a different OS than the host.

    Windows paths on POSIX: drive-letter prefix (C:\\...), UNC prefix
    (\\\\server\\share\\...), or any backslash in the body (POSIX never
    produces backslashes in a saved path).

    POSIX paths on Windows: leading ``/`` that is NOT a UNC share. The
    earlier ``os.path.isabs`` guard was wrong — on Windows,
    ``ntpath.isabs("/Users/alice/x")`` returns True, so the predicate
    ``startswith("/") and not isabs`` was always False and POSIX paths
    were never flagged as foreign on a Windows host (CodeRabbit
    critical on 253a9b4). Detect explicitly: ``/`` start but not
    ``//`` (which is reserved for UNC). Backslashes are a POSIX
    impossibility, so on Windows we additionally treat their absence
    as a hint — but the ``/`` prefix is sufficient.
    """
    if not path:
        return False
    if os.name == "nt":
        # Windows host — spot POSIX-shaped absolute paths.
        # POSIX absolute paths start with a single forward slash and
        # contain no backslashes. Reject UNC-style ``\\server\share``
        # AND drive-relative ``\Users\…`` (both are Windows-native;
        # POSIX paths can never contain a backslash). Code-review on
        # 706466f caught the drive-relative case.
        if "\\" in path:
            return False
        return path.startswith("/") and not path.startswith("//")
    # POSIX host — spot Windows-shaped paths.
    if _WINDOWS_DRIVE_RE.match(path):
        return True
    if _UNC_PREFIX_RE.match(path):
        return True
    if "\\" in path:
        return True
    return False


def session_liveness(record_path: str) -> dict:
    """Inspect a session's on-disk state without loading it.

    Returns a dict::

        {
          "live": bool,        # True iff at least one path / rescan-able file exists
          "saved_images": int, # count of image entries in the JSON
          "missing": int,      # how many of those have no file on disk
          "rescan_imgs": int,  # extra images discoverable via folder rescan
          "rescan_vids": int,  # extra videos discoverable via folder rescan
        }

    A session is "dead" when ``live`` is False — every saved image is
    missing AND no surveyed folder has any image/video the rescan path
    could surface. Used by the Session Manager to flag prune candidates.

    Cross-platform: only uses os.path.isfile / isdir / listdir / splitext,
    so saved paths work on whichever OS this runs on (Win sessions opened
    on macOS will read as "dead" iff the macOS-mounted equivalents don't
    exist, which is the right answer).
    """
    result = {"live": False, "saved_images": 0, "missing": 0,
              "rescan_imgs": 0, "rescan_vids": 0, "foreign_os": False}
    try:
        with open(record_path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except Exception as exc:
        # Unreadable / corrupt JSON — NOT dead, just broken. Let
        # list_sessions' existing skip-warning handle it; we return
        # "live" so the user doesn't accidentally lose a fixable file.
        # H4 (code-review): log at debug so post-mortem after a confused
        # user report ("my live sessions got pruned") is possible.
        # Gemini medium PR #43 (3277077726): include exception type so
        # post-mortem distinguishes PermissionError / OSError (disk-side,
        # transient) from json.JSONDecodeError (file actually corrupt
        # and should be quarantined rather than retried).
        logger.debug(
            "session_liveness read failed for %s: %s: %s",
            record_path, type(exc).__name__, exc,
        )
        result["live"] = True
        return result

    images = data.get("session", {}).get("images", [])
    result["saved_images"] = len(images)

    # C1: if EVERY stored path looks foreign-OS, classify live. The
    # session was saved on a different host and can't be evaluated
    # here — prune would lose work that would still be valid back
    # on its home OS.
    if images and all(_is_foreign_path(img.get("path", "")) for img in images):
        result["live"] = True
        result["foreign_os"] = True
        return result

    folders: set = set()
    any_saved_alive = False
    for img in images:
        path = img.get("path", "")
        if not path:
            continue
        folders.add(os.path.dirname(path))
        if os.path.isfile(path):
            any_saved_alive = True
        else:
            result["missing"] += 1

    # Folder rescan vocabulary mirrors the load-path: count any image
    # or video the rescan WOULD pick up. Treat ANY hit as evidence of
    # life — a folder with only videos but no saved-image survivors
    # is still loadable by the rescan path and should not be pruned.
    #
    # M1 (code-review): broken-symlink / unmounted-drive detection.
    # ``os.path.isdir`` returns False both for a permanently-gone
    # folder AND for a symlink whose target is currently unreachable
    # (sleeping external drive, dropped network mount). Distinguish
    # via ``os.path.lexists``: True + isdir False = link/mount
    # exists but target is currently inaccessible — treat as live
    # so a re-plug brings the session back.
    for folder in folders:
        if not folder:
            continue
        if not os.path.isdir(folder):
            if os.path.lexists(folder):
                # Folder reference is alive but target is unreachable.
                # Classify session live so it survives a re-plug.
                result["live"] = True
                logger.debug(
                    "session_liveness: %s folder %s is dangling link/"
                    "unreachable mount — classifying live",
                    record_path, folder,
                )
                return result
            continue
        try:
            entries = os.listdir(folder)
        except OSError as exc:
            logger.debug(
                "session_liveness: listdir(%s) failed: %s", folder, exc,
            )
            continue
        for fname in entries:
            full = os.path.join(folder, fname)
            if not os.path.isfile(full):
                continue
            ext = os.path.splitext(fname)[1].lower()
            if ext in _LIVENESS_IMAGE_EXTS:
                result["rescan_imgs"] += 1
            elif ext in _LIVENESS_VIDEO_EXTS:
                result["rescan_vids"] += 1

    result["live"] = bool(
        any_saved_alive or result["rescan_imgs"] > 0 or result["rescan_vids"] > 0
    )
    return result


def find_dead_sessions(app_dir: str) -> List[SessionRecord]:
    """Return the subset of saved sessions whose source data is gone.

    "Dead" = ``session_liveness(...)["live"] is False`` — the saved
    image paths all point at missing files AND no surveyed folder has
    any image/video the rescan path could surface. Safe for prune.
    """
    dead: List[SessionRecord] = []
    for rec in list_sessions(app_dir):
        try:
            if not session_liveness(rec.path)["live"]:
                dead.append(rec)
        except Exception:
            # Defensive: any unexpected error means we keep the record
            # rather than risk a false-positive prune.
            logger.warning("session_liveness raised for %s", rec.path, exc_info=True)
    return dead


def prune_dead_sessions(
    app_dir: str,
    paths: Optional[List[str]] = None,
) -> List[str]:
    """Delete sessions classified dead by ``find_dead_sessions``.

    When ``paths`` is None, behaves as before — calls
    ``find_dead_sessions`` and prunes whatever comes back. When given,
    deletes exactly those file paths instead (no rescan). Use the
    explicit-paths form when the caller has already computed the dead
    set and is showing it to the user (Session Manager dialog's
    _refresh_list does this) — prevents the case where a folder
    becomes inaccessible between dialog-open and prune-click and a
    formerly-live session silently flips dead and gets swept along
    (code-review H2 on 4ddb0252).

    Returns the list of deleted file paths so the caller can show a
    confirmation log. Individual delete failures are logged + skipped
    so one stuck file can't block the whole sweep.
    """
    if paths is None:
        targets = [rec.path for rec in find_dead_sessions(app_dir)]
    else:
        targets = list(paths)
    deleted: List[str] = []
    for path in targets:
        try:
            delete_session(path)
            deleted.append(path)
        except OSError as exc:
            logger.warning("Failed to prune dead session %s: %s", path, exc)
    return deleted
