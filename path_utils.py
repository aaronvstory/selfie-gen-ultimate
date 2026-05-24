"""
Path utilities for PyInstaller compatibility.
Provides functions to get correct paths whether running as script or frozen exe.
"""

import os
import re
import sys
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple


APP_NAME = "selfie-gen-ultimate"

# ---------------------------------------------------------------------------
# Concurrent-launch workspace + instance identity (PR #49)
#
# Two launches of this app must not bleed runtime state (carousel, session
# autosave, video history, crash log) into each other. We use two axes:
#
#   workspace name  → namespace dir under user_data_dir.
#                     "default" maps to the existing root so shared files
#                     (kling_config.json, ui_config.json, kling_gui.log) stay
#                     exactly where they are. Named workspaces go under
#                     <root>/workspaces/<name>/.
#
#   instance id     → per-process child dir under <workspace>/runtime/instances/.
#                     Format "<YYYYMMDD-HHMMSS>-<PID>". Always set; even two
#                     default launches each get their own isolated instance dir.
#
# Resolution order: explicit CLI arg → KLING_WORKSPACE env → "default". The env
# vars KLING_WORKSPACE and KLING_INSTANCE_ID are set early in gui_launcher.main
# so any subprocess inherits the same identity.
# ---------------------------------------------------------------------------

WORKSPACE_DEFAULT = "default"
_WORKSPACE_NAME_RE = re.compile(r"^[A-Za-z0-9._-]+$")
# Instance id format: "<YYYYMMDD-HHMMSS>-<PID>". The regex matches that and
# its component chars only — no slashes/backslashes/dots-as-traversal — so a
# malicious or stale ``KLING_INSTANCE_ID`` env value can never escape the
# runtime tree via ``get_runtime_dir`` (which composes it into a path).
# Code-review M1 on PR #49.
_INSTANCE_ID_RE = re.compile(r"^[A-Za-z0-9._-]{1,64}$")
_INSTANCE_ID_CACHE: Optional[str] = None
_WINDOWS_RESERVED_FOR_WORKSPACE = {
    "CON", "PRN", "AUX", "NUL",
    "COM1", "COM2", "COM3", "COM4", "COM5", "COM6", "COM7", "COM8", "COM9",
    "LPT1", "LPT2", "LPT3", "LPT4", "LPT5", "LPT6", "LPT7", "LPT8", "LPT9",
}


def _sanitize_workspace_name(name: str) -> str:
    """Return the canonical workspace name or raise ValueError.

    Accepts: letters, digits, ``.``, ``_``, ``-``. Length 1-64.
    Rejects: empty, whitespace, ``..``, leading dot, slashes/backslashes,
    Windows reserved device names. The leading-dot ban keeps workspace dirs
    out of dotfile-hidden status on macOS/Linux.
    """
    raw = (name or "").strip()
    if not raw:
        raise ValueError("workspace name is empty")
    if len(raw) > 64:
        raise ValueError("workspace name exceeds 64 chars")
    if raw == "." or raw == ".." or raw.startswith("."):
        raise ValueError(f"workspace name starts with '.' or is '..': {raw!r}")
    if not _WORKSPACE_NAME_RE.match(raw):
        raise ValueError(
            f"workspace name has invalid chars (allowed: A-Z a-z 0-9 . _ -): {raw!r}"
        )
    if raw.upper() in _WINDOWS_RESERVED_FOR_WORKSPACE:
        raise ValueError(f"workspace name is a Windows reserved device name: {raw!r}")
    return raw


def set_workspace(name: str) -> str:
    """Sanitize ``name``, set ``KLING_WORKSPACE`` env, return canonical name.

    Raises ``ValueError`` on invalid input — caller is expected to log and
    fall back to :data:`WORKSPACE_DEFAULT`.
    """
    canonical = _sanitize_workspace_name(name)
    os.environ["KLING_WORKSPACE"] = canonical
    return canonical


def get_workspace() -> str:
    """Return the current workspace name (env-or-default, sanitized).

    Never raises — falls back to :data:`WORKSPACE_DEFAULT` if the env var
    holds a malformed value (e.g. a stale env from a different app).
    """
    raw = os.environ.get("KLING_WORKSPACE", WORKSPACE_DEFAULT) or WORKSPACE_DEFAULT
    try:
        return _sanitize_workspace_name(raw)
    except ValueError:
        return WORKSPACE_DEFAULT


def get_instance_id() -> str:
    """Return the current process's instance id.

    Reads ``KLING_INSTANCE_ID`` if set; otherwise generates
    ``"<YYYYMMDD-HHMMSS>-<PID>"``, caches it in this module, exports it
    via env so subprocesses inherit. Subsequent calls in the same process
    return the cached value — instance id is process-stable.
    """
    global _INSTANCE_ID_CACHE
    if _INSTANCE_ID_CACHE is not None:
        return _INSTANCE_ID_CACHE
    env_val = os.environ.get("KLING_INSTANCE_ID", "").strip()
    if env_val and _INSTANCE_ID_RE.match(env_val):
        # Trust an inherited id from a parent launcher process — but only
        # after validation. Without this, a stale or hostile env value like
        # "../escape" would land in ``get_runtime_dir()`` as a path component
        # and write outside the runtime tree. Code-review M1 on PR #49.
        _INSTANCE_ID_CACHE = env_val
        return env_val
    # Either no env value or it failed validation — generate fresh and
    # overwrite the env so any subprocess inherits the clean value.
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    fresh = f"{stamp}-{os.getpid()}"
    _INSTANCE_ID_CACHE = fresh
    os.environ["KLING_INSTANCE_ID"] = fresh
    return fresh


def _user_data_root() -> str:
    """Return the same root that ``get_config_path`` / ``get_log_path`` use.

    macOS: ``~/Library/Application Support/<APP_NAME>``.
    Windows/Linux: ``get_app_dir()`` (preserves the portable Windows workflow
    documented in CLAUDE.md — kling_config.json stays next to the exe).
    """
    return get_user_data_dir() if sys.platform == "darwin" else get_app_dir()


def get_workspace_dir(workspace: Optional[str] = None) -> str:
    """Return the parent dir for the given workspace's namespaced state.

    For the default workspace this returns the SAME root as today's
    ``get_config_path`` / ``get_log_path`` — so ``kling_config.json``,
    ``ui_config.json``, ``kling_gui.log`` stay exactly where they have
    always lived. Only the new ``runtime/`` subtree is new.

    For named workspaces this returns ``<root>/workspaces/<name>/``.
    Defense-in-depth: after constructing the path, verifies it stays within
    ``_user_data_root()``; if not (would only happen if sanitization were
    bypassed somehow) falls back to the default workspace.
    """
    ws = workspace or get_workspace()
    root = _user_data_root()
    if ws == WORKSPACE_DEFAULT:
        return root
    candidate = os.path.join(root, "workspaces", ws)
    try:
        common = os.path.commonpath([os.path.abspath(candidate), os.path.abspath(root)])
        if os.path.normcase(common) != os.path.normcase(os.path.abspath(root)):
            return root
    except ValueError:
        return root
    return candidate


def get_runtime_dir(
    workspace: Optional[str] = None,
    instance_id: Optional[str] = None,
) -> str:
    """Return this process's runtime dir under the given workspace.

    Layout: ``<workspace_dir>/runtime/instances/<instance_id>/``. The
    per-instance dir holds carousel autosave, video history, crash log —
    everything that two concurrent processes would otherwise clobber.

    Pure path computation; call :func:`ensure_runtime_dirs` to materialize.
    """
    base = get_workspace_dir(workspace)
    iid = instance_id or get_instance_id()
    return os.path.join(base, "runtime", "instances", iid)


def get_runtime_sessions_dir(
    workspace: Optional[str] = None,
    instance_id: Optional[str] = None,
) -> str:
    """Return ``<runtime_dir>/sessions/`` — per-instance autosave home."""
    return os.path.join(get_runtime_dir(workspace, instance_id), "sessions")


def get_runtime_crash_log_path(
    workspace: Optional[str] = None,
    instance_id: Optional[str] = None,
) -> str:
    """Return ``<runtime_dir>/crash_log.txt`` — per-instance crash sink."""
    return os.path.join(get_runtime_dir(workspace, instance_id), "crash_log.txt")


def get_runtime_history_path(
    workspace: Optional[str] = None,
    instance_id: Optional[str] = None,
) -> str:
    """Return ``<runtime_dir>/kling_history.json`` — per-instance video history."""
    return os.path.join(get_runtime_dir(workspace, instance_id), "kling_history.json")


def get_workspace_markers_dir(workspace: Optional[str] = None) -> str:
    """Return ``<workspace_dir>/runtime/.markers/`` — liveness markers root.

    One small JSON per active instance. Survives orderly close (deleted) but
    is also subject to a 24h stale-cleanup on launch (catches kill -9).
    """
    return os.path.join(get_workspace_dir(workspace), "runtime", ".markers")


def ensure_runtime_dirs(
    workspace: Optional[str] = None,
    instance_id: Optional[str] = None,
) -> str:
    """Create the per-instance runtime tree and return the runtime dir.

    Idempotent (``makedirs(exist_ok=True)``). Creates the sessions and
    markers dirs as well so first-write call sites don't have to.
    """
    runtime = get_runtime_dir(workspace, instance_id)
    for d in (
        runtime,
        get_runtime_sessions_dir(workspace, instance_id),
        get_workspace_markers_dir(workspace),
    ):
        try:
            os.makedirs(d, exist_ok=True)
        except OSError:
            # Per-instance dirs are best-effort at this layer; the GUI's
            # autosave layer already tolerates missing sessions dir by
            # creating it on demand. Re-raising would break the launch
            # for a write-permission edge case that isn't actionable here.
            pass
    return runtime


def build_expand_filenames(
    base_stem: str,
    ext: str,
    gen_dir: "Path | str",
    do_2x: bool,
) -> "Tuple[Path, Optional[Path]]":
    """Plan deterministic output paths for a Generative Expand run.

    Used by Step 0 (face_crop_tab.py), Step 2.5 (expand_tab.py), AND
    the CLI automation pipeline (automation/pipeline.py) so all three
    surfaces produce identical filenames for the same conceptual op.

    Returns ``(pass1_path, pass2_path_or_None)`` as ``pathlib.Path``
    objects.

    Naming:
        * pass 1  ->  ``<stem>-expanded<ext>``
        * pass 2  ->  ``<stem>-expanded-2x<ext>`` (only when ``do_2x``)

    Collision suffixes are PAIRED in 2x mode — pass 1 and pass 2 share
    the same ``_vN`` index so a re-run's outputs stay semantically
    linked on disk. Without pairing, pass 1 could land at ``_v2``
    while pass 2 lands at ``_v3`` (or vice-versa) and the
    "this 2x belongs to that 1x" relationship is lost. Single-pass
    mode resolves the one path independently.

    NOTE: ``sanitize_stem`` is called locally inside this function (no
    import of kling_gui.* — this module is shared between the CLI
    pipeline and the GUI, and the CLI must not depend on kling_gui).
    """
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

# Valid image extensions for processing
VALID_EXTENSIONS = {'.jpg', '.jpeg', '.png', '.webp', '.bmp', '.gif', '.tiff', '.tif'}


def get_app_dir() -> str:
    """
    Get the directory where the application is located.
    
    When running as a script: Returns the directory containing the main .py file
    When running as frozen exe: Returns the directory containing the .exe
    
    Returns:
        str: Absolute path to the application directory
    """
    if getattr(sys, 'frozen', False):
        # Running as compiled executable (PyInstaller)
        # sys.executable points to the .exe file
        return os.path.dirname(sys.executable)
    else:
        # Running as a Python script
        # Use the directory of the main module
        return os.path.dirname(os.path.abspath(sys.argv[0]))


def get_resource_dir() -> str:
    """
    Get the directory where bundled resources are located.
    
    When running as a script: Same as get_app_dir()
    When running as frozen exe: Returns the _MEIPASS temp directory
    
    This is for read-only bundled resources, not user data.
    
    Returns:
        str: Absolute path to the resource directory
    """
    if getattr(sys, 'frozen', False):
        # Running as compiled executable
        # _MEIPASS contains extracted bundled files
        return getattr(sys, '_MEIPASS', os.path.dirname(sys.executable))
    else:
        # Running as a Python script
        return os.path.dirname(os.path.abspath(sys.argv[0]))


def get_config_path(filename: str = "kling_config.json") -> str:
    """
    Get the full path for a configuration file.
    On macOS, config files are stored in Application Support. On Windows,
    keep the app-local path to preserve the tested portable workflow.
    
    Args:
        filename: Name of the config file
        
    Returns:
        str: Full path to the config file
    """
    base_dir = get_user_data_dir() if sys.platform == "darwin" else get_app_dir()
    return os.path.join(base_dir, filename)


def get_log_path(filename: str = "kling_gui.log") -> str:
    """
    Get the full path for a log file.
    On macOS, log files are stored in Application Support. On Windows,
    keep the app-local path to preserve the tested portable workflow.
    
    Args:
        filename: Name of the log file
        
    Returns:
        str: Full path to the log file
    """
    base_dir = get_user_data_dir() if sys.platform == "darwin" else get_app_dir()
    return os.path.join(base_dir, filename)


def get_crash_log_path() -> str:
    """
    Get the full path for the crash log file.
    
    Returns:
        str: Full path to crash_log.txt
    """
    base_dir = get_user_data_dir() if sys.platform == "darwin" else get_app_dir()
    return os.path.join(base_dir, "crash_log.txt")


def get_user_data_dir(app_name: str = APP_NAME) -> str:
    """
    Get the directory for storing user data such as config, logs, caches, and sessions.

    Platform conventions:
    - macOS: ~/Library/Application Support/<app_name>
    - Windows: %APPDATA%/<app_name>
    - Linux: ~/.local/share/<app_name>
    """
    if sys.platform == "darwin":
        base = os.path.expanduser("~/Library/Application Support")
    elif sys.platform == "win32":
        base = os.environ.get("APPDATA", os.path.expanduser("~\\AppData\\Roaming"))
    else:
        base = os.environ.get("XDG_DATA_HOME", os.path.expanduser("~/.local/share"))

    path = os.path.join(base, app_name)
    try:
        if os.path.exists(path) and not os.path.isdir(path):
            return get_app_dir()
        os.makedirs(path, exist_ok=True)
    except OSError:
        return get_app_dir()
    return path


def is_frozen() -> bool:
    """
    Check if running as a frozen executable.

    Returns:
        bool: True if running as exe, False if running as script
    """
    return getattr(sys, 'frozen', False)


_GEN_FOLDER_NAMES = {"gen-images", "gen-videos"}
_WINDOWS_RESERVED_NAMES = {
    "CON",
    "PRN",
    "AUX",
    "NUL",
    "COM1",
    "COM2",
    "COM3",
    "COM4",
    "COM5",
    "COM6",
    "COM7",
    "COM8",
    "COM9",
    "LPT1",
    "LPT2",
    "LPT3",
    "LPT4",
    "LPT5",
    "LPT6",
    "LPT7",
    "LPT8",
    "LPT9",
}
_INVALID_FILENAME_CHARS_RE = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_REPEATED_UNDERSCORE_RE = re.compile(r"_{2,}")


def _walk_up_past_gen_folders(source_path: str) -> str:
    """Walk up from *source_path*'s parent dir past any gen-images/gen-videos nesting."""
    current = os.path.dirname(os.path.abspath(source_path))
    while os.path.basename(current) in _GEN_FOLDER_NAMES:
        current = os.path.dirname(current)
    return current


def get_gen_images_folder(source_path: str) -> str:
    """Return the gen-images subfolder path next to the given source file.

    Walks up past any existing gen-images/gen-videos directories to prevent
    nesting when piping output through multiple tabs.

    Pure path computation — does NOT call os.makedirs.
    Each caller is responsible for creating it before writing.
    """
    return os.path.join(_walk_up_past_gen_folders(source_path), "gen-images")


def get_gen_videos_folder(source_path: str) -> str:
    """Return the gen-videos subfolder path next to the given source file.

    Same anti-nesting logic as get_gen_images_folder() but for video output.

    Pure path computation — does NOT call os.makedirs.
    Each caller is responsible for creating it before writing.
    """
    return os.path.join(_walk_up_past_gen_folders(source_path), "gen-videos")


def sanitize_stem(name: str, default: str = "untitled") -> str:
    """Sanitize a path stem for cross-platform compatibility."""
    raw = str(name or "")
    sanitized = _INVALID_FILENAME_CHARS_RE.sub("_", raw)
    sanitized = sanitized.replace("\n", " ").replace("\r", " ").replace("\t", " ")
    sanitized = _REPEATED_UNDERSCORE_RE.sub("_", sanitized)
    sanitized = sanitized.strip(" .")
    if not sanitized:
        sanitized = default
    if sanitized.upper() in _WINDOWS_RESERVED_NAMES:
        sanitized = f"{sanitized}_file"
    return sanitized[:180]


def _sanitize_reasons(current_name: str, desired_name: str) -> str:
    """Return a compact reason summary for a sanitize rename."""
    reasons: List[str] = []
    if _INVALID_FILENAME_CHARS_RE.search(current_name):
        reasons.append("invalid_characters")
    if any(ch in current_name for ch in ("\n", "\r", "\t")):
        reasons.append("control_whitespace")
    if current_name != current_name.strip(" ."):
        reasons.append("edge_spaces_or_dots")
    if "__" in current_name and "__" not in desired_name:
        reasons.append("repeated_underscores")
    if current_name.upper() in _WINDOWS_RESERVED_NAMES:
        reasons.append("windows_reserved_name")
    if not reasons:
        reasons.append("normalized")
    return ",".join(reasons)


def sanitize_filename(name: str, default_stem: str = "untitled") -> str:
    """Sanitize filename while preserving extension when possible."""
    raw = str(name or "").strip()
    stem_raw, ext_raw = os.path.splitext(raw)
    stem = sanitize_stem(stem_raw or raw, default=default_stem)
    ext = _INVALID_FILENAME_CHARS_RE.sub("", ext_raw or "")
    ext = ext.replace(" ", "")
    if ext and not ext.startswith("."):
        ext = f".{ext}"
    if len(ext) > 20:
        ext = ext[:20]
    if ext == ".":
        ext = ""
    return f"{stem}{ext}"


def preflight_image_path(path: str, allowed_exts: Optional[Set[str]] = None) -> Tuple[bool, str]:
    """Perform a lightweight image path validation for GUI ingest flows."""
    if not path:
        return False, "empty path"
    if not os.path.isfile(path):
        return False, "file not found"
    ext = os.path.splitext(path)[1].lower()
    valid_exts = allowed_exts or VALID_EXTENSIONS
    if ext not in valid_exts:
        return False, f"unsupported extension: {ext or '(none)'}"
    try:
        from PIL import Image, ImageOps

        with Image.open(path) as img:
            img.load()
            # Keep this tolerant: only verify this call does not hard-fail.
            ImageOps.exif_transpose(img)
    except Exception as exc:
        return False, f"{type(exc).__name__}: {exc}"
    return True, "ok"


def make_unique_name(parent_dir: str, candidate_name: str) -> str:
    """Return a non-colliding filename in *parent_dir* using numeric suffixes."""
    candidate_path = os.path.join(parent_dir, candidate_name)
    if not os.path.exists(candidate_path):
        return candidate_name

    stem, ext = os.path.splitext(candidate_name)
    counter = 2
    while True:
        next_name = f"{stem}_{counter}{ext}"
        next_path = os.path.join(parent_dir, next_name)
        if not os.path.exists(next_path):
            return next_name
        counter += 1


def sanitize_path_name(path: str) -> Tuple[str, bool]:
    """Rename one path to a cross-platform-safe name when needed."""
    parent = os.path.dirname(path)
    current_name = os.path.basename(path)
    if not parent or not current_name:
        return path, False

    if os.path.isdir(path):
        desired = sanitize_stem(current_name, default="untitled")
    else:
        desired = sanitize_filename(current_name, default_stem="untitled")

    if desired == current_name:
        return path, False

    desired = make_unique_name(parent, desired)
    new_path = os.path.join(parent, desired)
    os.rename(path, new_path)
    return new_path, True


def sanitize_tree_names(root_path: str, rename_root: bool = True) -> Tuple[str, List[Tuple[str, str]]]:
    """Recursively rename files/folders under *root_path* to safe names.

    Returns:
        (new_root_path, renames) where renames are (old_path, new_path).
    """
    new_root, renames, _failures, _changes = sanitize_tree_names_report(
        root_path=root_path,
        rename_root=rename_root,
    )
    return new_root, renames


def sanitize_tree_names_report(
    root_path: str, rename_root: bool = True
) -> Tuple[str, List[Tuple[str, str]], List[Dict[str, str]], List[Dict[str, str]]]:
    """Recursively rename files/folders under *root_path* and report failures.

    Returns:
        (new_root_path, renames, failures, changes)
        - renames: list[(old_path, new_path)]
        - failures: list[{
              "path", "desired_path", "error_type", "error_message"
          }]
        - changes: list[{
              "old_path", "new_path", "old_name", "new_name", "reason"
          }]
    """
    if not os.path.isdir(root_path):
        return root_path, [], [], []

    renames: List[Tuple[str, str]] = []
    failures: List[Dict[str, str]] = []
    changes: List[Dict[str, str]] = []

    def _attempt_rename(old_path: str):
        parent = os.path.dirname(old_path)
        current_name = os.path.basename(old_path)
        if not parent or not current_name:
            return

        if os.path.isdir(old_path):
            desired = sanitize_stem(current_name, default="untitled")
        else:
            desired = sanitize_filename(current_name, default_stem="untitled")

        if desired == current_name:
            return

        desired = make_unique_name(parent, desired)
        desired_path = os.path.join(parent, desired)
        reason = _sanitize_reasons(current_name=current_name, desired_name=desired)
        try:
            os.rename(old_path, desired_path)
            renames.append((old_path, desired_path))
            changes.append(
                {
                    "old_path": old_path,
                    "new_path": desired_path,
                    "old_name": current_name,
                    "new_name": desired,
                    "reason": reason,
                }
            )
        except OSError as exc:
            failures.append(
                {
                    "path": old_path,
                    "desired_path": desired_path,
                    "error_type": type(exc).__name__,
                    "error_message": str(exc),
                }
            )

    for current_dir, dirs, files in os.walk(root_path, topdown=False):
        for filename in sorted(files):
            old_path = os.path.join(current_dir, filename)
            if not os.path.exists(old_path):
                continue
            _attempt_rename(old_path)
        for dirname in sorted(dirs):
            old_path = os.path.join(current_dir, dirname)
            if not os.path.isdir(old_path):
                continue
            _attempt_rename(old_path)

    new_root = root_path
    if rename_root:
        old_root = root_path
        _attempt_rename(old_root)
        if renames and renames[-1][0] == old_root:
            new_root = renames[-1][1]

    return new_root, renames, failures, changes


def sanitize_portable_stem(name: str, default: str = "untitled") -> str:
    """Strict portable stem sanitizer for folder-tree compatibility fixes."""
    raw = str(name or "")
    sanitized = raw.replace("\n", " ").replace("\r", " ").replace("\t", " ")
    sanitized = _INVALID_FILENAME_CHARS_RE.sub("_", sanitized)
    sanitized = sanitized.rstrip(" .")
    if not sanitized:
        sanitized = default
    if sanitized.upper() in _WINDOWS_RESERVED_NAMES:
        sanitized = f"{sanitized}_file"
    return sanitized[:180]


def _portable_reasons(current_name: str) -> str:
    """Reason summary for strict portable sanitize mode."""
    reasons: List[str] = []
    if _INVALID_FILENAME_CHARS_RE.search(current_name):
        reasons.append("invalid_characters")
    if any(ch in current_name for ch in ("\n", "\r", "\t")):
        reasons.append("control_whitespace")
    if current_name != current_name.rstrip(" ."):
        reasons.append("trailing_spaces_or_dots")
    if current_name.upper() in _WINDOWS_RESERVED_NAMES:
        reasons.append("windows_reserved_name")
    if len(current_name) > 180:
        reasons.append("length_limit")
    if not reasons:
        reasons.append("normalized")
    return ",".join(reasons)


def sanitize_portable_filename(name: str, default_stem: str = "untitled") -> str:
    """Strict portable filename sanitizer preserving valid leading dots/underscores."""
    raw = str(name or "")
    if not raw:
        return default_stem
    stem_raw, ext_raw = os.path.splitext(raw)
    if not stem_raw:
        stem_raw, ext_raw = raw, ""
    stem = sanitize_portable_stem(stem_raw or raw, default=default_stem)
    ext = _INVALID_FILENAME_CHARS_RE.sub("", ext_raw or "")
    ext = ext.replace(" ", "")
    if ext and not ext.startswith("."):
        ext = f".{ext}"
    if len(ext) > 20:
        ext = ext[:20]
    if ext == ".":
        ext = ""
    return f"{stem}{ext}"


def sanitize_tree_names_portable_report(
    root_path: str, rename_root: bool = True
) -> Tuple[str, List[Tuple[str, str]], List[Dict[str, str]], List[Dict[str, str]]]:
    """Strict tree sanitizer used by Sanitize Folder feature path."""
    if not os.path.isdir(root_path):
        return root_path, [], [], []

    renames: List[Tuple[str, str]] = []
    failures: List[Dict[str, str]] = []
    changes: List[Dict[str, str]] = []

    def _attempt_rename(old_path: str):
        parent = os.path.dirname(old_path)
        current_name = os.path.basename(old_path)
        if not parent or not current_name:
            return

        if os.path.isdir(old_path):
            desired = sanitize_portable_stem(current_name, default="untitled")
        else:
            desired = sanitize_portable_filename(current_name, default_stem="untitled")

        if desired == current_name:
            return

        desired = make_unique_name(parent, desired)
        desired_path = os.path.join(parent, desired)
        reason = _portable_reasons(current_name=current_name)
        try:
            os.rename(old_path, desired_path)
            renames.append((old_path, desired_path))
            changes.append(
                {
                    "old_path": old_path,
                    "new_path": desired_path,
                    "old_name": current_name,
                    "new_name": desired,
                    "reason": reason,
                }
            )
        except OSError as exc:
            failures.append(
                {
                    "path": old_path,
                    "desired_path": desired_path,
                    "error_type": type(exc).__name__,
                    "error_message": str(exc),
                }
            )

    for current_dir, dirs, files in os.walk(root_path, topdown=False):
        for filename in sorted(files):
            old_path = os.path.join(current_dir, filename)
            if not os.path.exists(old_path):
                continue
            _attempt_rename(old_path)
        for dirname in sorted(dirs):
            old_path = os.path.join(current_dir, dirname)
            if not os.path.isdir(old_path):
                continue
            _attempt_rename(old_path)

    new_root = root_path
    if rename_root:
        old_root = root_path
        _attempt_rename(old_root)
        if renames and renames[-1][0] == old_root:
            new_root = renames[-1][1]

    return new_root, renames, failures, changes
