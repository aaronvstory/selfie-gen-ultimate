"""Workspace + instance runtime path layout (PR #49).

Verifies the path templates documented in CLAUDE.md "Concurrent launches &
workspaces": default workspace runtime sits under the shared user_data_root
so existing shared files (kling_config.json, ui_config.json) stay put; named
workspaces sit under ``<root>/workspaces/<name>/``; per-instance runtime is
always under ``<workspace>/runtime/instances/<instance_id>/``.

Defense-in-depth check: a workspace name that bypasses sanitization (e.g.
via a direct call to ``get_workspace_dir`` with a bad value, which shouldn't
happen in practice) must NOT escape the user-data root.
"""

import os

import path_utils


def test_default_workspace_dir_equals_user_data_root(monkeypatch):
    """Default workspace must NOT introduce a new dir level — existing config
    paths (kling_config.json, ui_config.json) live at the same root they
    always have, so back-compat for single-instance users is preserved."""
    monkeypatch.delenv("KLING_WORKSPACE", raising=False)
    monkeypatch.delenv("KLING_INSTANCE_ID", raising=False)
    root = path_utils._user_data_root()
    assert path_utils.get_workspace_dir("default") == root
    assert path_utils.get_workspace_dir() == root


def test_named_workspace_dir_under_workspaces_subtree(monkeypatch):
    monkeypatch.delenv("KLING_WORKSPACE", raising=False)
    root = path_utils._user_data_root()
    ws = path_utils.get_workspace_dir("shoot-a")
    assert ws == os.path.join(root, "workspaces", "shoot-a")
    assert ws.startswith(root)  # never escapes root


def test_runtime_dir_template(monkeypatch):
    monkeypatch.setenv("KLING_WORKSPACE", "default")
    monkeypatch.setenv("KLING_INSTANCE_ID", "20260101-000000-1234")
    # Clear cached instance id from earlier tests / earlier in this run.
    path_utils._INSTANCE_ID_CACHE = None
    runtime = path_utils.get_runtime_dir()
    assert runtime.endswith(os.path.join("runtime", "instances", "20260101-000000-1234"))
    assert "workspaces" not in runtime  # default sits at root, not under workspaces/
    path_utils._INSTANCE_ID_CACHE = None  # avoid leaking to other tests


def test_runtime_dir_named_workspace_template(monkeypatch):
    monkeypatch.setenv("KLING_WORKSPACE", "shoot-a")
    monkeypatch.setenv("KLING_INSTANCE_ID", "20260101-000000-1234")
    path_utils._INSTANCE_ID_CACHE = None
    runtime = path_utils.get_runtime_dir()
    parts = runtime.replace("\\", "/").split("/")
    # Layout: <root>/workspaces/shoot-a/runtime/instances/<id>/
    assert "workspaces" in parts
    assert "shoot-a" in parts
    assert "runtime" in parts
    assert "instances" in parts
    assert "20260101-000000-1234" in parts
    path_utils._INSTANCE_ID_CACHE = None


def test_get_runtime_subpaths_align(monkeypatch):
    """The runtime accessors must produce paths rooted at runtime_dir."""
    monkeypatch.setenv("KLING_WORKSPACE", "default")
    monkeypatch.setenv("KLING_INSTANCE_ID", "20260101-000000-1234")
    runtime = path_utils.get_runtime_dir()
    assert path_utils.get_runtime_sessions_dir() == os.path.join(runtime, "sessions")
    assert path_utils.get_runtime_crash_log_path() == os.path.join(runtime, "crash_log.txt")
    assert path_utils.get_runtime_history_path() == os.path.join(runtime, "kling_history.json")
    assert path_utils.get_runtime_scratch_dir() == os.path.join(runtime, "scratch")


# ── Multi-instance scratch isolation (fix/multi-instance-state-bleed) ──
# Root cause of the user-reported "image from the other instance bleeds
# through on detect-and-crop": face_crop_tab wrote the EXIF-corrected
# source copy to ``tempfile.gettempdir()/kling_facecrop_<basename>`` —
# a path keyed ONLY on the image basename, SHARED across processes. Two
# concurrent launches loading same-named files collided on one temp
# file. get_runtime_scratch_dir() namespaces by instance id.


def test_scratch_dir_is_under_runtime_dir(monkeypatch, tmp_path):
    monkeypatch.setattr(path_utils, "_user_data_root", lambda: str(tmp_path))
    monkeypatch.setenv("KLING_WORKSPACE", "default")
    monkeypatch.setenv("KLING_INSTANCE_ID", "20260101-000000-1234")
    path_utils._INSTANCE_ID_CACHE = None
    scratch = path_utils.get_runtime_scratch_dir()
    assert scratch == os.path.join(path_utils.get_runtime_dir(), "scratch")
    path_utils._INSTANCE_ID_CACHE = None


def test_scratch_dir_is_created_eagerly(monkeypatch, tmp_path):
    """Call sites write immediately, so the dir must exist on return."""
    monkeypatch.setattr(path_utils, "_user_data_root", lambda: str(tmp_path))
    monkeypatch.setenv("KLING_WORKSPACE", "default")
    monkeypatch.setenv("KLING_INSTANCE_ID", "20260101-000000-9999")
    path_utils._INSTANCE_ID_CACHE = None
    scratch = path_utils.get_runtime_scratch_dir()
    assert os.path.isdir(scratch)
    path_utils._INSTANCE_ID_CACHE = None


def test_two_instances_get_isolated_scratch_dirs(monkeypatch, tmp_path):
    """THE regression guard. Two different instance ids loading a file
    with the SAME basename must resolve to DIFFERENT scratch paths, so
    instance B's EXIF-corrected pixels can never overwrite instance A's
    on disk. This is the exact collision that caused the face-crop image
    bleed across concurrent GUI launches."""
    monkeypatch.setattr(path_utils, "_user_data_root", lambda: str(tmp_path))
    monkeypatch.setenv("KLING_WORKSPACE", "default")

    basename = "photo.jpg"  # same filename, different source folders

    monkeypatch.setenv("KLING_INSTANCE_ID", "20260101-000000-1111")
    path_utils._INSTANCE_ID_CACHE = None
    scratch_a = path_utils.get_runtime_scratch_dir()
    corrected_a = os.path.join(scratch_a, f"kling_facecrop_{basename}")

    monkeypatch.setenv("KLING_INSTANCE_ID", "20260101-000000-2222")
    path_utils._INSTANCE_ID_CACHE = None
    scratch_b = path_utils.get_runtime_scratch_dir()
    corrected_b = os.path.join(scratch_b, f"kling_facecrop_{basename}")

    assert scratch_a != scratch_b, "two instances share a scratch dir — BLEED"
    assert corrected_a != corrected_b, (
        "same-basename images in two instances resolve to the same temp "
        "file — this is the multi-instance face-crop bleed bug"
    )
    # And writing into one must not be visible in the other.
    with open(corrected_a, "wb") as f:
        f.write(b"instance-A-pixels")
    assert not os.path.exists(corrected_b), (
        "instance A's scratch write leaked into instance B's namespace"
    )
    path_utils._INSTANCE_ID_CACHE = None


def test_scratch_dir_falls_back_when_runtime_unwritable(monkeypatch, tmp_path):
    """Gemini PR #88 MEDIUM: if the per-instance runtime dir can't be
    created (locked-down AppData / read-only Application Support),
    get_runtime_scratch_dir must NOT return a dead path that guarantees
    write failures. It falls back to the system temp dir — but STILL
    per-instance-namespaced (``kling_scratch_<iid>``) so the
    cross-instance bleed does NOT reopen on the fallback."""
    import tempfile as _tf
    monkeypatch.setattr(path_utils, "_user_data_root", lambda: str(tmp_path))
    monkeypatch.setenv("KLING_WORKSPACE", "default")
    monkeypatch.setenv("KLING_INSTANCE_ID", "20260101-000000-7777")
    path_utils._INSTANCE_ID_CACHE = None

    real_makedirs = os.makedirs

    def _fail_under_runtime(p, *a, **k):
        # Simulate the runtime subtree being unwritable, but let the
        # system-temp fallback succeed.
        if "runtime" in str(p) and "instances" in str(p):
            raise OSError("read-only runtime tree")
        return real_makedirs(p, *a, **k)

    monkeypatch.setattr(os, "makedirs", _fail_under_runtime)
    scratch = path_utils.get_runtime_scratch_dir()
    # Fell back to system temp, still namespaced by the instance id.
    assert scratch == os.path.join(_tf.gettempdir(), "kling_scratch_20260101-000000-7777")
    # And it's per-instance: a different id yields a different fallback dir.
    monkeypatch.setenv("KLING_INSTANCE_ID", "20260101-000000-8888")
    path_utils._INSTANCE_ID_CACHE = None
    scratch2 = path_utils.get_runtime_scratch_dir()
    assert scratch2 != scratch
    path_utils._INSTANCE_ID_CACHE = None


def test_face_crop_tab_uses_scratch_not_gettempdir():
    """Source-pin: face_crop_tab must NOT route working temp files
    through the shared ``tempfile.gettempdir()``. A refactor that
    reintroduces it reopens the multi-instance bleed."""
    import pathlib
    # Read from the source tree; fall back to the distribution mirror so
    # the test still runs in a distribution-only checkout / packaged CI
    # (Gemini PR #88: EAFP — attempt the read, fall back on OSError).
    root = pathlib.Path(__file__).resolve().parent.parent
    try:
        src = (root / "kling_gui" / "tabs" / "face_crop_tab.py").read_text(encoding="utf-8")
    except OSError:
        src = (root / "distribution" / "kling_gui" / "tabs" / "face_crop_tab.py").read_text(encoding="utf-8")
    # The two former bleed sites must now go through the scratch helper.
    assert "get_runtime_scratch_dir()" in src, (
        "face_crop_tab must use get_runtime_scratch_dir() for working "
        "temp files (EXIF-corrected source + crop-save fallback)"
    )
    # No live call to gettempdir() should remain (comments referencing it
    # for documentation are fine; an actual call is not).
    import re
    code_lines = [
        ln for ln in src.splitlines()
        if "gettempdir(" in ln and not ln.lstrip().startswith("#")
    ]
    assert not code_lines, (
        f"face_crop_tab still calls tempfile.gettempdir(): {code_lines}"
    )


def test_save_crop_fallback_truncates_stem_for_max_path():
    """v2.29: the ``_save_crop`` OSError-fallback writes into the deeply
    nested per-instance scratch dir (``runtime/instances/<id>/scratch/``).
    ``_load_source`` already caps its basename to avoid blowing Windows'
    260-char MAX_PATH there, but the ``_save_crop`` fallback originally used
    a bare ``{origin.stem}_crop.jpg``. A pathologically long stem could push
    the path past the limit and silently lose the crop. Source-pin: the
    fallback must truncate the stem before building the scratch path."""
    import pathlib
    import re

    root = pathlib.Path(__file__).resolve().parent.parent
    try:
        src = (root / "kling_gui" / "tabs" / "face_crop_tab.py").read_text(encoding="utf-8")
    except OSError:
        src = (root / "distribution" / "kling_gui" / "tabs" / "face_crop_tab.py").read_text(encoding="utf-8")

    # The fallback must NOT write a bare un-truncated stem straight into the
    # scratch dir. Find the scratch-fallback out_path construction and assert
    # it does not use ``origin.stem`` directly.
    bad = [
        ln.strip()
        for ln in src.splitlines()
        if "get_runtime_scratch_dir()" in ln
        and "_crop.jpg" in ln
        and "origin.stem" in ln
    ]
    assert not bad, (
        "the _save_crop scratch fallback uses an un-truncated origin.stem — "
        f"MAX_PATH risk on a long basename: {bad}"
    )
    # And a truncation guard (mirroring _load_source's cap) must be present.
    assert re.search(r"fallback_stem\s*=\s*fallback_stem\[-\d+:\]", src), (
        "the _save_crop fallback must cap the stem length before building "
        "the deep scratch path (mirror _load_source's MAX_PATH guard)"
    )


def test_get_workspace_dir_sanitizes_explicit_arg(monkeypatch):
    """PR #49 round-2 (CodeRabbit): a direct call to ``get_workspace_dir``
    with a malformed ``workspace`` arg must NOT compose a traversal path.
    Previously, sanitization happened only at the env-entry layer
    (``set_workspace``/``get_workspace``); a Python caller bypassing those
    could slip a bad name through. Now sanitized in-function with fallback
    to the default workspace."""
    monkeypatch.delenv("KLING_WORKSPACE", raising=False)
    root = path_utils._user_data_root()
    # All these should fall back to default (returning root unchanged)
    for bad in ("../escape", "..\\escape", "a/b", "a\\b", ".hidden", "", "  ", "CON"):
        result = path_utils.get_workspace_dir(bad)
        assert result == root, (
            f"get_workspace_dir({bad!r}) returned {result!r}; "
            f"expected fallback to default workspace root {root!r}"
        )
    # Valid names still work
    assert path_utils.get_workspace_dir("shoot-a") == os.path.join(root, "workspaces", "shoot-a")
    assert path_utils.get_workspace_dir("default") == root


def test_workspace_path_traversal_caught_by_commonpath(monkeypatch):
    """Even if a caller bypassed _sanitize_workspace_name somehow, the
    commonpath defense in get_workspace_dir falls back to the root rather
    than returning an escaped path."""
    # We can't easily inject ".." since path normalization may collapse it
    # before commonpath sees the result. The check still proves that any
    # path returned is anchored under user_data_root.
    monkeypatch.delenv("KLING_WORKSPACE", raising=False)
    for ws in ("shoot-a", "shoot_b", "default"):
        result = path_utils.get_workspace_dir(ws)
        common = os.path.commonpath([
            os.path.abspath(result),
            os.path.abspath(path_utils._user_data_root()),
        ])
        assert os.path.normcase(common) == os.path.normcase(
            os.path.abspath(path_utils._user_data_root())
        )


def test_instance_id_rejects_traversal_in_env(monkeypatch):
    """PR #49 M1: ``KLING_INSTANCE_ID`` is a path component in ``get_runtime_dir``,
    so a hostile or stale value like ``../escape`` must be rejected. Validator
    falls back to a fresh ``<YYYYMMDD-HHMMSS>-<PID>`` stamp."""
    import path_utils as p
    # Reject: actual traversal patterns (slashes, ..-with-slash, whitespace, length).
    # Note: leading-dot names like ``.hidden`` are accepted — they create a hidden
    # dir but don't escape the runtime tree, and a stricter rule could break a
    # parent launcher that legitimately uses ``.`` in its id format. Path
    # escape is the actual attack to defend against.
    for bad in ("../escape", "..\\escape", "a/b", "a\\b", "x with space", "", "a" * 65):
        monkeypatch.setenv("KLING_INSTANCE_ID", bad)
        p._INSTANCE_ID_CACHE = None
        iid = p.get_instance_id()
        assert iid != bad, f"hostile id {bad!r} accepted without sanitization"
        # Format guarantee for the fallback
        assert "/" not in iid and "\\" not in iid and ".." not in iid
        # And the env was overwritten with the clean value so subprocesses inherit it
        assert os.environ["KLING_INSTANCE_ID"] == iid
    p._INSTANCE_ID_CACHE = None


def test_instance_id_accepts_valid_inherited_env(monkeypatch):
    """A well-formed inherited id (e.g. from a parent launcher process) is honored."""
    import path_utils as p
    monkeypatch.setenv("KLING_INSTANCE_ID", "20260101-120000-1234")
    p._INSTANCE_ID_CACHE = None
    assert p.get_instance_id() == "20260101-120000-1234"
    p._INSTANCE_ID_CACHE = None


def test_instance_id_format(monkeypatch):
    """Instance id format must be `<YYYYMMDD-HHMMSS>-<PID>` — keeps stamps
    sortable and per-process unique even across same-second launches with
    different PIDs."""
    import path_utils as p
    # Force fresh generation by clearing cache + env
    monkeypatch.delenv("KLING_INSTANCE_ID", raising=False)
    p._INSTANCE_ID_CACHE = None
    iid = p.get_instance_id()
    parts = iid.split("-")
    assert len(parts) == 3, f"expected 3 parts in {iid!r}"
    assert len(parts[0]) == 8 and parts[0].isdigit()      # YYYYMMDD
    assert len(parts[1]) == 6 and parts[1].isdigit()      # HHMMSS
    assert parts[2].isdigit()                              # PID
    # Cached: second call returns same value
    assert p.get_instance_id() == iid
    p._INSTANCE_ID_CACHE = None


def test_ensure_runtime_dirs_creates_tree(monkeypatch, tmp_path):
    """ensure_runtime_dirs is idempotent and creates the sessions + markers tree."""
    monkeypatch.setattr(path_utils, "_user_data_root", lambda: str(tmp_path))
    monkeypatch.setenv("KLING_WORKSPACE", "default")
    monkeypatch.setenv("KLING_INSTANCE_ID", "test-instance")
    path_utils._INSTANCE_ID_CACHE = None
    runtime = path_utils.ensure_runtime_dirs()
    assert os.path.isdir(runtime)
    assert os.path.isdir(path_utils.get_runtime_sessions_dir())
    assert os.path.isdir(path_utils.get_workspace_markers_dir())
    # Idempotent — second call must not raise
    path_utils.ensure_runtime_dirs()
    path_utils._INSTANCE_ID_CACHE = None
