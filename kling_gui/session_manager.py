"""Session persistence — save / load / list / delete session snapshots."""

import os
import json
import re
import logging
from datetime import datetime
from typing import List, Optional, NamedTuple

from path_utils import _walk_up_past_gen_folders

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
    """Return a timestamped autosave path for the provided session."""
    sessions_dir = _get_sessions_dir(app_dir)
    project_key = get_project_key(image_session)
    base = _build_autosave_name(project_key)
    path = os.path.join(sessions_dir, f"{base}.json")
    counter = 2
    while os.path.exists(path):
        path = os.path.join(sessions_dir, f"{base}_{counter}.json")
        counter += 1
    return path


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
) -> str:
    """Save session to JSON. Returns the saved file path.

    If overwrite_path is given, overwrites that file in place.
    Otherwise creates a new file (auto-increments on collision).
    """
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
            fpath = get_autosave_path(app_dir, image_session)
            session_name = os.path.splitext(os.path.basename(fpath))[0]
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
        # Stamp the engine version so loaders can detect & invalidate stale (pre-v1.8) scores.
        "similarity_engine_version": "1.8",
        "session": image_session.to_dict(),
        "config_snapshot": _build_config_snapshot(config),
    }

    with open(fpath, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)

    if kind == SESSION_KIND_AUTOSAVE:
        prune_autosaves(app_dir, effective_project_key, keep=autosave_retention)

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
