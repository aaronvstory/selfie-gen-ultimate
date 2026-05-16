"""Vendored Resemble AI deepfake-detection client.

Ported from the standalone ``C:\\claude\\Resemble\\resemble\\detect.py`` so this
subproject has **no runtime dependency** on that external path (it must work on
macOS / other machines / frozen builds where that path does not exist).

The submit/trim logic is a verbatim port; only env handling changed:
``load_env`` (single file) is replaced by ``load_env_chain`` (own .env →
optional external .env → process env) plus ``resolve_api_key``.
"""

from __future__ import annotations

import mimetypes
import os
from pathlib import Path
from typing import Any

import requests

BASE_URL = "https://app.resemble.ai/api/v2"
DETECT_URL = f"{BASE_URL}/detect"
SECURE_UPLOAD_URL = f"{BASE_URL}/secure_uploads"

IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".gif", ".webp"}
# .m4v added vs. detect.py — the oldcam pipeline accepts/produces it.
VIDEO_EXTS = {".mp4", ".mov", ".avi", ".mkv", ".webm", ".m4v"}
MEDIA_EXTS = IMAGE_EXTS | VIDEO_EXTS

DIRECT_UPLOAD_LIMIT = 150 * 1024 * 1024  # 150 MB per API constraints

API_KEY_ENV = "RESEMBLE_API_KEY"
# Point this at an extra .env to read as a best-effort key source (e.g. a
# shared standalone client's .env). os.pathsep-separated for multiple paths.
EXTERNAL_ENV_OVERRIDE = "RESEMBLE_EXTERNAL_ENV"

# Default best-effort convenience locations (the original standalone Resemble
# client keeps its key here on the dev box). These are read ONLY if they
# exist; absence is never an error, so they are harmless on other machines.
# Override entirely via the RESEMBLE_EXTERNAL_ENV env var.
_DEFAULT_EXTERNAL_ENV_PATHS = (
    Path(r"C:\claude\Resemble\resemble\.env"),
    Path(r"F:\claude\Resemble\resemble\.env"),  # C:/F: junction on the dev box
)


def _external_env_paths() -> tuple[Path, ...]:
    """Resolve the external .env search paths.

    ``RESEMBLE_EXTERNAL_ENV`` (os.pathsep-separated) fully replaces the
    built-in defaults when set, so the tool is portable: other machines just
    set the var (or rely on their own ``resemble-score/.env``) instead of
    inheriting dev-box-specific paths.
    """
    override = os.environ.get(EXTERNAL_ENV_OVERRIDE, "")
    parsed = tuple(
        Path(p.strip())
        for p in override.split(os.pathsep)
        if p.strip()
    )
    # "set but only blank segments" is treated like "not set" → defaults,
    # so a stray empty var never silently disables the convenience paths.
    return parsed or _DEFAULT_EXTERNAL_ENV_PATHS


def _apply_env_file(path: Path) -> None:
    """Apply KEY=VALUE lines from ``path`` via ``os.environ.setdefault``.

    Exact line-parser ported from detect.py:load_env. Existing environment
    values always win (setdefault), so an explicitly-exported key is never
    overwritten by a file.

    Best-effort: a path that is missing, a directory, or unreadable is
    skipped silently. A bad ``RESEMBLE_EXTERNAL_ENV`` (e.g. pointing at a
    folder) must NOT crash startup — it should fall through to the clean
    "missing API key" error instead of an uncaught OSError.
    """
    try:
        if not path.is_file():
            return
        text = path.read_text(encoding="utf-8")
    except OSError:
        return
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def load_env_chain() -> None:
    """Populate the environment from, in priority order:

    1. ``resemble-score/.env`` (gitignored; the canonical place for the key)
    2. external ``.env`` path(s) from :func:`_external_env_paths` if any
       exist (``RESEMBLE_EXTERNAL_ENV`` override, else built-in defaults)
    3. whatever is already in the process environment (untouched — wins via
       ``setdefault``)
    """
    own_env = Path(__file__).resolve().parent.parent / ".env"
    _apply_env_file(own_env)
    for ext in _external_env_paths():
        _apply_env_file(ext)


def resolve_api_key() -> str:
    """Return the Resemble API key, or raise a clean ``RuntimeError``.

    The message lists every source checked so a user can fix it without a
    stack trace. Callers surface ``str(e)`` (CLI exit / GUI messagebox).
    """
    load_env_chain()
    key = os.environ.get(API_KEY_ENV, "").strip()
    if key:
        return key
    raise RuntimeError(
        "RESEMBLE_API_KEY is not set. Provide it via one of:\n"
        "  1. resemble-score/.env  ->  RESEMBLE_API_KEY=your-key\n"
        "  2. C:\\claude\\Resemble\\resemble\\.env (Windows convenience)\n"
        "  3. the RESEMBLE_API_KEY environment variable"
    )


def auth_header(api_key: str) -> dict:
    return {"Authorization": f"Bearer {api_key}"}


def secure_upload(path: Path, api_key: str) -> str:
    with path.open("rb") as f:
        mime = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        resp = requests.post(
            SECURE_UPLOAD_URL,
            headers=auth_header(api_key),
            files={"file": (path.name, f, mime)},
            timeout=600,
        )
    resp.raise_for_status()
    token = resp.json().get("token")
    if not token:
        raise RuntimeError(f"No token in secure upload response: {resp.text}")
    return token


def submit_detect(path: Path, api_key: str) -> dict:
    """Submit a single file. Direct upload when <=150 MB, else secure upload."""
    headers = auth_header(api_key) | {"Prefer": "wait"}
    size = path.stat().st_size

    if size <= DIRECT_UPLOAD_LIMIT:
        with path.open("rb") as f:
            mime = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
            resp = requests.post(
                DETECT_URL,
                headers=headers,
                files={"file": (path.name, f, mime)},
                data={"intelligence": "true"},
                timeout=1800,
            )
    else:
        token = secure_upload(path, api_key)
        resp = requests.post(
            DETECT_URL,
            headers=headers | {"Content-Type": "application/json"},
            json={"media_token": token, "intelligence": True},
            timeout=1800,
        )

    resp.raise_for_status()
    return resp.json()


def _flatten_video_children(children: Any) -> list[dict]:
    """Walk the nested video_metrics.children tree and emit one flat entry per
    node, keeping only the four fields the project cares about."""
    out: list[dict] = []
    if not isinstance(children, list):
        return out
    # O(1) pop()/extend() with reversed lists preserves the original
    # breadth-then-depth ordering of the pop(0)+(nested+stack) form while
    # avoiding the O(N) list-head pop and per-iteration list rebuild.
    stack = list(reversed(children))
    while stack:
        node = stack.pop()
        if not isinstance(node, dict):
            continue
        out.append(
            {
                "type": node.get("type"),
                "conclusion": node.get("conclusion"),
                "score": node.get("score"),
                "certainty": node.get("certainty"),
            }
        )
        nested = node.get("children")
        if isinstance(nested, list):
            stack.extend(reversed(nested))
    return out


def trim_response(raw: dict) -> dict:
    """Reduce the raw API payload to the trimmed shape detect.py persists."""
    item = raw.get("item") or {}
    media_type = item.get("media_type")
    intelligence = item.get("intelligence")

    trimmed_item: dict[str, Any] = {
        "created_at": item.get("created_at"),
        "media_type": media_type,
        "filename": item.get("filename"),
        "intelligence": (
            {"description": intelligence.get("description")}
            if isinstance(intelligence, dict)
            else None
        ),
    }

    if media_type == "image":
        im = item.get("image_metrics") or {}
        trimmed_item["image_metrics"] = {
            "image": im.get("image"),
            "score": im.get("score"),
        }
    elif media_type == "video":
        trimmed_item["metrics"] = item.get("metrics")
        vm = item.get("video_metrics") or {}
        trimmed_item["video_metrics"] = {
            "score": vm.get("score"),
            "certainty": vm.get("certainty"),
            "children": _flatten_video_children(vm.get("children")),
        }

    return {"success": True, "item": trimmed_item}


def detect_video(path: Path, api_key: str) -> dict:
    """Submit one video and return the trimmed result.

    Raises ``RuntimeError`` if the API reports ``success=false`` (mirrors
    detect.py:process) or propagates ``requests`` errors for the caller to
    record per-item.
    """
    raw = submit_detect(path, api_key)
    if not raw.get("success"):
        raise RuntimeError(f"API returned success=false: {raw}")
    return trim_response(raw)
