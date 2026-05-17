"""Session persistence — save / load / list / delete session snapshots."""

import os
import json
import re
import hashlib
import logging
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


def _get_sessions_dir(app_dir: str) -> str:
    d = os.path.join(app_dir, "sessions")
    os.makedirs(d, exist_ok=True)
    return d


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


def get_rolling_autosave_path(app_dir: str, project_key: str) -> str:
    """Return the single deterministic autosave path for a project.

    One rolling file per project (``{project_key}_autosave.json``), overwritten
    in place — no timestamp, no accumulation. The no-timestamp stem is still
    matched by ``_infer_project_key``'s regex (the timestamp group is optional).
    """
    sessions_dir = _get_sessions_dir(app_dir)
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


def _purge_legacy_autosaves(app_dir: str, project_key: str, keep_path: str) -> int:
    """Delete every autosave for ``project_key`` except the rolling file.

    Filename-only matching (no JSON parsing) so this stays cheap on the
    autosave hot path even when the directory holds many manual sessions.
    Case-insensitive path compare (Windows) so the rolling file is never
    deleted as its own legacy sibling. Manual saves are untouched.
    """
    removed = 0
    sessions_dir = _get_sessions_dir(app_dir)
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
                    "source_type": "input",
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


def list_sessions(app_dir: str) -> List[SessionRecord]:
    """Return all saved sessions, sorted newest-first."""
    sessions_dir = _get_sessions_dir(app_dir)
    records = []
    for fname in os.listdir(sessions_dir):
        if not fname.endswith(".json"):
            continue
        fpath = os.path.join(sessions_dir, fname)
        try:
            with open(fpath, "r", encoding="utf-8") as f:
                data = json.load(f)
            kind = _normalize_session_kind(data.get("session_kind"), fname)
            project_key = _infer_project_key(data, fname)
            mtime_iso = _file_mtime_iso(fpath)
            created_at = str(data.get("created_at") or data.get("timestamp") or mtime_iso)
            updated_at = str(data.get("updated_at") or data.get("timestamp") or mtime_iso)
            records.append(SessionRecord(
                name=data.get("name", fname),
                path=fpath,
                timestamp=updated_at,
                created_at=created_at,
                updated_at=updated_at,
                session_kind=kind,
                project_key=project_key,
                image_count=len(data.get("session", {}).get("images", [])),
            ))
        except Exception:
            logger.warning("Skipping corrupt session file: %s", fname)
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
    sessions_dir = _get_sessions_dir(app_dir)
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
            fpath = get_rolling_autosave_path(app_dir, effective_project_key)
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

    with open(fpath, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)

    if kind == SESSION_KIND_AUTOSAVE:
        # Single rolling file now; keep the legacy timestamped pile from this
        # project tidy (purge anything that isn't the rolling file).
        _purge_legacy_autosaves(app_dir, effective_project_key, fpath)

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
