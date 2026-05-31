"""Static-text guards on the shared Windows Python resolver.

``scripts/win_resolve_python.bat`` is the single source of truth for how the
main Windows launchers (``launchers/windows/run_gui.bat`` and ``run_cli.bat``)
find — or install — a Python interpreter. It was introduced after a
non-technical user installed Python 3.12 but did not tick "Add Python to PATH",
so the old ``where python`` gate failed even though a supported Python was
present. The resolver fixes that by going through the ``py`` launcher
(``py -3.11`` / ``py -3.12``), which selects by version from the registry and
works without PATH, and by silently auto-installing Python 3.12 (winget →
python.org) when nothing is found.

These are pure static-text assertions (no subprocess) so they run fast under
the repo-root pytest and catch resolver-pattern regressions. They mirror the
style of ``tests/test_resemble_score_launcher_resolver.py``.
"""
import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]

RESOLVER = "scripts/win_resolve_python.bat"
GUI = "launchers/windows/run_gui.bat"
CLI = "launchers/windows/run_cli.bat"

# cmd tuples are written without inner spaces: (3,9) <= ... < (3,13)
VERSION_PROBE = r"\(3,9\) <= sys\.version_info\[:2\] < \(3,13\)"


def _read(path: str) -> str:
    return (REPO_ROOT / path).read_text(encoding="utf-8")


# ── The resolver exists and uses the py launcher (the core fix) ────────


def test_resolver_uses_py_launcher_for_311_and_312():
    """The whole point: prefer the ``py`` launcher (3.11 then 3.12) so an
    interpreter the user installed WITHOUT 'Add to PATH' is still found.
    The launcher invocation is parameterised via :pyres_try_py, which runs
    ``py -%~1 ...``."""
    src = _read(RESOLVER)
    assert "py -%~1" in src, (
        "resolver must invoke the py launcher (py -%~1) in :pyres_try_py — "
        "this is the fix for 'installed Python but not on PATH'."
    )
    assert "call :pyres_try_py 3.11" in src and "call :pyres_try_py 3.12" in src, (
        "resolver must probe py -3.11 and py -3.12"
    )
    # 3.11 must be attempted before 3.12 (3.11 is the most-tested target).
    assert src.find("call :pyres_try_py 3.11") < src.find(
        "call :pyres_try_py 3.12"
    ), "py -3.11 should be tried before py -3.12"


def test_resolver_probes_common_install_dirs():
    src = _read(RESOLVER)
    for needle in (
        r"%LocalAppData%\Programs\Python\Python311",
        r"%LocalAppData%\Programs\Python\Python312",
        r"%ProgramFiles%\Python311",
        r"%ProgramFiles%\Python312",
    ):
        assert needle in src, f"resolver missing common install-dir probe: {needle}"


def test_resolver_version_gate_targets_3_9_to_3_12():
    src = _read(RESOLVER)
    assert re.search(VERSION_PROBE, src), (
        "resolver must version-gate every candidate to 3.9-3.12 "
        "(mediapipe==0.10.35 has wheels for that range only)"
    )


def test_resolver_check_subroutine_is_flat_goto():
    """:pyres_check must use flat goto labels, NOT ``if (...) else (...)`` —
    otherwise the (3,9)/(3,13) literals close the block early and cmd crashes
    with `was unexpected at this time` (the resemble-score regression)."""
    src = _read(RESOLVER)
    m = re.search(r"(?m)^:pyres_check$", src)
    assert m, "resolver missing :pyres_check subroutine definition"
    block = src[m.start():]
    assert ") else (" not in block, (
        ":pyres_check reintroduced an if/else paren-block — version-probe "
        "parens will prematurely close it."
    )
    assert ":pyres_check_permissive" in block and ":pyres_check_ok" in block, (
        ":pyres_check must use the flat goto labels"
    )


# ── Auto-install: winget first, python.org fallback, target 3.12 ───────


def test_resolver_auto_installs_via_winget_then_pyorg():
    src = _read(RESOLVER)
    assert "winget install" in src, "resolver must try winget auto-install"
    assert "Python.Python.3.12" in src, (
        "auto-install must target Python 3.12 specifically (mediapipe caps at "
        "3.12; 'latest' would install 3.13+ and fail the gate)"
    )
    assert "python.org/ftp/python/" in src, (
        "resolver must fall back to the python.org silent installer when "
        "winget is unavailable"
    )
    # winget block must come before the python.org download block.
    assert src.find("winget install") < src.find("python.org/ftp/python/"), (
        "winget should be attempted before the python.org download fallback"
    )


def test_resolver_install_uses_silent_path_enabled_flags():
    src = _read(RESOLVER)
    # The python.org installer must run silently with PATH + py launcher on.
    assert "/quiet" in src and "PrependPath=1" in src and "Include_launcher=1" in src, (
        "python.org installer must run /quiet with PrependPath=1 + "
        "Include_launcher=1 so the py launcher is available afterwards"
    )


def test_resolver_reprobe_after_install_uses_py_launcher_not_where_python():
    """PATH edits from the installer do NOT reach the running shell, so the
    post-install re-detection must go through the py launcher / absolute path,
    never a fresh ``where python``."""
    src = _read(RESOLVER)
    # Anchor on the LABEL definition (a line that is exactly
    # ":pyres_install_python"), not the earlier `call :pyres_install_python`
    # dispatch line — otherwise the block wrongly includes the
    # :pyres_try_path_python subroutine (which legitimately uses `where
    # python` for the PATH probe, a different code path).
    m = re.search(r"(?m)^:pyres_install_python$", src)
    assert m, "resolver missing :pyres_install_python label"
    install_block = src[m.start():]
    # Re-probe after install must call :pyres_try_py (py launcher) ...
    assert "call :pyres_try_py 3.12" in install_block, (
        "post-install re-detection must use the py launcher"
    )
    # ... and must NOT rely on `where python` inside the install block.
    assert "where python" not in install_block, (
        "post-install re-detection must not use `where python` — the "
        "installer's PATH edit doesn't apply to the running shell"
    )


# ── Graceful failure ───────────────────────────────────────────────────


def test_resolver_failure_opens_pyorg_and_sets_rc():
    src = _read(RESOLVER)
    assert "start \"\" \"https://www.python.org/downloads/\"" in src, (
        "on total failure the resolver should open python.org in the browser"
    )
    assert 'set "RESOLVE_RC=1"' in src and 'set "RESOLVE_RC=0"' in src, (
        "resolver must report success/failure via RESOLVE_RC"
    )


# ── cmd-parser safety: no unescaped parens in echo lines ───────────────


def test_resolver_no_unescaped_parens_in_echo_lines():
    """Every echo line that contains a literal ( or ) must caret-escape it,
    or cmd's nested-block parser can close an enclosing block early.

    Exception: ``echo(`` (no space) is the *safe* blank-line idiom — its
    ``(`` is part of the command token, never a message paren, so it's
    excluded from the check.
    """
    src = _read(RESOLVER)
    for line in src.splitlines():
        stripped = line.strip()
        if not stripped.lower().startswith("echo"):
            continue
        # `echo(` is the canonical blank-line form; not a paren hazard.
        if stripped == "echo(":
            continue
        # Strip a leading `echo(` prefix so we only scan the message body.
        body = stripped[5:] if stripped.lower().startswith("echo(") else stripped[4:]
        bare = re.search(r"(?<!\^)[()]", body)
        assert bare is None, (
            f"resolver echo line has an unescaped paren -> parser hazard. "
            f"Caret-escape it. Line: {stripped!r}"
        )


def test_resolver_has_no_dev_null():
    """A checkout-local linter has historically rewritten >nul to /dev/null in
    .bat files. Guard against it shipping."""
    assert "/dev/null" not in _read(RESOLVER), (
        "resolver contains POSIX /dev/null — must use Windows >nul"
    )


# ── The main launchers actually call the shared resolver ───────────────


def test_main_launchers_call_shared_resolver():
    for path in (GUI, CLI):
        src = _read(path)
        assert "win_resolve_python.bat" in src, (
            f"{path}: must call the shared resolver"
        )
        # And must no longer carry the brittle inline `where python` gate.
        assert "where python" not in src, (
            f"{path}: still has an inline `where python` gate — detection "
            "should be delegated to the shared resolver"
        )


def test_main_launchers_check_resolve_rc():
    for path in (GUI, CLI):
        src = _read(path)
        assert 'if not "!RESOLVE_RC!"=="0"' in src, (
            f"{path}: must check RESOLVE_RC after calling the resolver"
        )
