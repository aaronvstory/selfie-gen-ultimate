"""TDD: true non-interactive --batch mode for the automation CLI.

The "fully automated batch capable" CLI historically had only an interactive
menu (``run_automation_menu`` -> ``while True: input()``) plus an
``input("Approve batch run? [y/N]")`` gate inside ``_run_resume_automation``.
``--auto`` merely dropped into that menu, so the batch path could NOT run
unattended (it blocked on stdin) on either OS.

This module pins the headless contract:

* ``main(["--batch", root, ...])`` runs end-to-end WITHOUT touching
  ``builtins.input`` (any read of stdin is a regression -> the test fails).
* It exits ``0`` on a successful run, non-zero on a missing/invalid root and
  on a preflight failure -- so ``run_auto.bat`` / ``run_auto.command`` can be
  scheduled from cron / Task Scheduler.
* The interactive menu + ``_run_resume_automation`` are left intact (we only
  ADD a path; we do not break the existing one).

Per ``feedback_tdd_real_import_probe_not_just_text``: these tests REAL-INVOKE
the headless method and ``main`` argument parsing -- they do not merely grep
the source text for the flag.
"""

from __future__ import annotations

import pytest

import kling_automation_ui
from kling_automation_ui import KlingAutomationUI


# --------------------------------------------------------------------------- #
# Guard: stdin must never be read on the headless path.                       #
# --------------------------------------------------------------------------- #
def _forbid_input(*_args, **_kwargs):  # pragma: no cover - only hit on regression
    raise AssertionError(
        "headless --batch path read stdin via input(); it must run unattended"
    )


# --------------------------------------------------------------------------- #
# API surface.                                                                #
# --------------------------------------------------------------------------- #
def test_headless_method_exists():
    """A dedicated non-interactive runner exists and is callable."""
    assert hasattr(KlingAutomationUI, "run_automation_headless")
    assert callable(KlingAutomationUI.run_automation_headless)


def test_main_accepts_batch_flag(monkeypatch):
    """``--batch <root>`` is parsed and routed to the headless runner --
    not the interactive menu -- and its int return becomes the process exit
    code."""
    captured = {}

    def _fake_headless(self, root, **kwargs):
        captured["root"] = root
        captured["kwargs"] = kwargs
        return 0

    # Never touch the startup dependency check or key onboarding in unit tests.
    monkeypatch.setenv("KLING_SKIP_PY_STARTUP_DEP_CHECK", "1")
    monkeypatch.setattr(KlingAutomationUI, "run_automation_headless", _fake_headless, raising=True)
    monkeypatch.setattr(KlingAutomationUI, "_run_startup_key_onboarding", lambda self: None, raising=False)
    monkeypatch.setattr("builtins.input", _forbid_input)

    with pytest.raises(SystemExit) as exc:
        kling_automation_ui.main(["--batch", "/some/root", "--limit", "10", "--yes"])

    assert exc.value.code == 0
    assert captured["root"] == "/some/root"
    assert captured["kwargs"].get("auto_approve") is True
    assert captured["kwargs"].get("max_cases_override") == "10"


def test_reprocess_override_not_in_manifest_snapshot_but_applied_after(tmp_path, monkeypatch):
    """--reprocess must (a) NOT be in the config_snapshot passed to the manifest
    (else AutomationManifest rejects the changed automation_* fingerprint and the
    run dies as a load failure -- code-review Codex P1), yet (b) end up applied to
    self.config (with automation_allow_reprocess=True) so the runner honours it
    (code-review Gemini HIGH). I.e. overrides are run policy applied POST-load."""
    monkeypatch.setattr("builtins.input", _forbid_input)

    class _Rec:
        relative_key = "case-a"

    monkeypatch.setattr(kling_automation_ui, "discover_case_folders", lambda *a, **k: [_Rec()])

    captured_snapshot = {}

    class _Manifest:
        manifest_path = tmp_path / "manifest.json"
        data = {"cases": {}}

        @classmethod
        def create_or_load(cls, *, manifest_path, root_dir, config_snapshot):
            captured_snapshot.update(config_snapshot)
            return cls()

    monkeypatch.setattr(kling_automation_ui, "AutomationManifest", _Manifest)

    class _Runner:
        last_case_results = {}

        def __init__(self, **kwargs):
            pass

        def validate_configuration(self):
            # Abort right after the snapshot is captured + overrides applied, so
            # we don't need to mock the whole dashboard run.
            return ["stop-here-after-overrides"]

    monkeypatch.setattr(kling_automation_ui, "AutoPipelineRunner", _Runner)

    ui = _bare_ui(tmp_path)
    monkeypatch.setattr(
        ui,
        "_collect_case_snapshot",
        lambda records, manifest: ([], {"will_run": 1, "discovered": 1, "pending": 1,
                                         "completed_total": 0, "skipped_complete": 0,
                                         "manual_review": 0, "failed": 0}, [_Rec()]),
        raising=False,
    )
    monkeypatch.setattr(ui, "_automation_manifest_path", lambda: tmp_path / "manifest.json", raising=False)

    rc = ui.run_automation_headless(str(tmp_path), auto_approve=True, reprocess_override="overwrite")
    assert rc == 1  # preflight stop
    # (a) the manifest snapshot must NOT carry the override (fingerprint stable):
    assert captured_snapshot.get("automation_reprocess_mode") != "overwrite"
    assert captured_snapshot.get("automation_allow_reprocess") is not True
    # (b) but the live config DID get the override (runner will honour it):
    assert ui.config["automation_reprocess_mode"] == "overwrite"
    assert ui.config["automation_allow_reprocess"] is True


def test_headless_rejects_file_as_root(tmp_path, monkeypatch):
    """A file path passes exists() but is not a valid root -> exit 1 invalid-root,
    not a fall-through into discovery (code-review CodeRabbit Major, PR #69)."""
    monkeypatch.setattr("builtins.input", _forbid_input)
    f = tmp_path / "a_file.txt"
    f.write_text("not a dir")
    called = {"discover": False}
    monkeypatch.setattr(
        kling_automation_ui, "discover_case_folders",
        lambda *a, **k: called.__setitem__("discover", True) or [],
    )
    ui = _bare_ui(tmp_path)
    rc = ui.run_automation_headless(str(f), auto_approve=True)
    assert rc == 1
    assert called["discover"] is False  # never reached discovery


def test_main_batch_rejects_invalid_limit(monkeypatch):
    """--limit only accepts 1/5/10/all; an out-of-set value must error at
    argparse (exit 2) rather than silently falling back to 5."""
    monkeypatch.setenv("KLING_SKIP_PY_STARTUP_DEP_CHECK", "1")
    monkeypatch.setattr("builtins.input", _forbid_input)
    with pytest.raises(SystemExit) as exc:
        kling_automation_ui.main(["--batch", "/r", "--limit", "3"])
    # argparse exits 2 on invalid choice
    assert exc.value.code == 2


def test_main_batch_propagates_nonzero_exit(monkeypatch):
    """A non-zero return from the headless runner must surface as a non-zero
    process exit (cron / Task Scheduler failure signalling)."""
    monkeypatch.setenv("KLING_SKIP_PY_STARTUP_DEP_CHECK", "1")
    monkeypatch.setattr(KlingAutomationUI, "run_automation_headless", lambda self, root, **kw: 3, raising=True)
    monkeypatch.setattr(KlingAutomationUI, "_run_startup_key_onboarding", lambda self: None, raising=False)
    monkeypatch.setattr("builtins.input", _forbid_input)

    with pytest.raises(SystemExit) as exc:
        kling_automation_ui.main(["--batch", "/some/root"])
    assert exc.value.code == 3


# --------------------------------------------------------------------------- #
# Exit-code semantics of the headless runner itself.                          #
# --------------------------------------------------------------------------- #
def _bare_ui(tmp_path) -> KlingAutomationUI:
    """A UI instance with just enough state to drive the headless runner,
    without running __init__ (which touches config files / API keys)."""
    ui = KlingAutomationUI.__new__(KlingAutomationUI)
    ui.legacy_pauses = False
    ui.config = {"automation_front_names": [], "automation_max_cases_per_run": 5}
    ui.automation_root_folder = ""
    return ui


def test_headless_missing_root_returns_one(tmp_path, monkeypatch):
    """Could-not-run conditions return exactly 1 (distinct from the exit-2
    ran-but-needs-attention code)."""
    monkeypatch.setattr("builtins.input", _forbid_input)
    ui = _bare_ui(tmp_path)
    missing = tmp_path / "does-not-exist"
    rc = ui.run_automation_headless(str(missing), auto_approve=True)
    assert rc == 1


def test_headless_auto_approve_false_aborts(tmp_path, monkeypatch):
    """auto_approve=False is not supported in headless mode -- it must abort
    loudly (return 1) rather than silently proceeding (code-review HIGH-2)."""
    monkeypatch.setattr("builtins.input", _forbid_input)
    # Must NOT even reach discovery -- guard is the first thing checked.
    monkeypatch.setattr(
        kling_automation_ui,
        "discover_case_folders",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("should not discover when auto_approve=False")),
    )
    ui = _bare_ui(tmp_path)
    rc = ui.run_automation_headless(str(tmp_path), auto_approve=False)
    assert rc == 1


def test_headless_no_cases_returns_nonzero(tmp_path, monkeypatch):
    """An existing-but-empty root has no case folders -> non-zero (nothing to
    do is a failure for a scheduled batch, not a silent success)."""
    monkeypatch.setattr("builtins.input", _forbid_input)
    monkeypatch.setattr(kling_automation_ui, "discover_case_folders", lambda *a, **k: [])
    ui = _bare_ui(tmp_path)
    rc = ui.run_automation_headless(str(tmp_path), auto_approve=True)
    assert rc != 0


def test_headless_preflight_failure_returns_nonzero(tmp_path, monkeypatch):
    """When AutoPipelineRunner.validate_configuration reports issues, the
    headless runner aborts with a non-zero code and never calls input()."""
    monkeypatch.setattr("builtins.input", _forbid_input)

    class _Rec:
        relative_key = "case-a"

    monkeypatch.setattr(kling_automation_ui, "discover_case_folders", lambda *a, **k: [_Rec()])

    class _Manifest:
        manifest_path = tmp_path / "manifest.json"
        data = {"cases": {}}

        @classmethod
        def create_or_load(cls, **kwargs):
            return cls()

    monkeypatch.setattr(kling_automation_ui, "AutomationManifest", _Manifest)

    class _Runner:
        last_case_results = {}

        def __init__(self, **kwargs):
            pass

        def validate_configuration(self):
            return ["similarity engine unavailable"]

    monkeypatch.setattr(kling_automation_ui, "AutoPipelineRunner", _Runner)

    ui = _bare_ui(tmp_path)
    # Make the snapshot return one runnable case so we reach preflight.
    monkeypatch.setattr(
        ui,
        "_collect_case_snapshot",
        lambda records, manifest: ([], {"will_run": 1, "discovered": 1, "pending": 1,
                                         "completed_total": 0, "skipped_complete": 0,
                                         "manual_review": 0, "failed": 0}, [_Rec()]),
        raising=False,
    )
    rc = ui.run_automation_headless(str(tmp_path), auto_approve=True)
    assert rc != 0


def test_headless_success_returns_zero(tmp_path, monkeypatch):
    """Happy path: a runnable case, clean preflight, a run that reports no
    failures -> exit 0, and input() is never read."""
    monkeypatch.setattr("builtins.input", _forbid_input)

    class _Rec:
        relative_key = "case-a"

    monkeypatch.setattr(kling_automation_ui, "discover_case_folders", lambda *a, **k: [_Rec()])

    class _Manifest:
        manifest_path = tmp_path / "manifest.json"
        data = {"cases": {}}

        @classmethod
        def create_or_load(cls, **kwargs):
            return cls()

    monkeypatch.setattr(kling_automation_ui, "AutomationManifest", _Manifest)

    class _Runner:
        last_case_results = {"case-a": {"status": "complete", "reason": ""}}

        def __init__(self, **kwargs):
            pass

        def validate_configuration(self):
            return []

    monkeypatch.setattr(kling_automation_ui, "AutoPipelineRunner", _Runner)

    ui = _bare_ui(tmp_path)
    # Force the TTY branch so the live-dashboard mock below is used (pytest's
    # captured stdout is non-TTY, which would otherwise take the direct-run path).
    monkeypatch.setattr(kling_automation_ui.sys.stdout, "isatty", lambda: True, raising=False)
    monkeypatch.setattr(
        ui,
        "_collect_case_snapshot",
        lambda records, manifest: ([], {"will_run": 1, "discovered": 1, "pending": 1,
                                         "completed_total": 0, "skipped_complete": 0,
                                         "manual_review": 0, "failed": 0}, [_Rec()]),
        raising=False,
    )
    # Avoid the live Rich dashboard + summary file write in the unit test.
    monkeypatch.setattr(
        ui,
        "_run_with_live_dashboard",
        lambda runner, run_cases, manifest: ({"completed": 1, "failed": 0, "manual_review": 0, "skipped": 0}, None),
        raising=False,
    )
    monkeypatch.setattr(ui, "_write_automation_summary", lambda *a, **k: None, raising=False)
    monkeypatch.setattr(ui, "_get_selected_selfie_prompt", lambda: (3, "prompt", "slot"), raising=False)
    monkeypatch.setattr(ui, "_automation_status_lines", lambda: [], raising=False)
    monkeypatch.setattr(ui, "_automation_manifest_path", lambda: tmp_path / "manifest.json", raising=False)

    rc = ui.run_automation_headless(str(tmp_path), auto_approve=True)
    assert rc == 0


def _run_with_stats(tmp_path, monkeypatch, final_stats):
    """Drive a full headless run that reaches the dashboard, returning the
    final per-run stats dict ``final_stats``. Returns the exit code."""
    monkeypatch.setattr("builtins.input", _forbid_input)

    class _Rec:
        relative_key = "case-a"

    monkeypatch.setattr(kling_automation_ui, "discover_case_folders", lambda *a, **k: [_Rec()])

    class _Manifest:
        manifest_path = tmp_path / "manifest.json"
        data = {"cases": {}}

        @classmethod
        def create_or_load(cls, **kwargs):
            return cls()

    monkeypatch.setattr(kling_automation_ui, "AutomationManifest", _Manifest)

    class _Runner:
        last_case_results = {"case-a": {"status": "x", "reason": "y"}}

        def __init__(self, **kwargs):
            pass

        def validate_configuration(self):
            return []

    monkeypatch.setattr(kling_automation_ui, "AutoPipelineRunner", _Runner)

    ui = _bare_ui(tmp_path)
    # Force the TTY branch so the dashboard mock is used (pytest stdout is non-TTY).
    monkeypatch.setattr(kling_automation_ui.sys.stdout, "isatty", lambda: True, raising=False)
    monkeypatch.setattr(
        ui,
        "_collect_case_snapshot",
        lambda records, manifest: ([], {"will_run": 1, "discovered": 1, "pending": 1,
                                         "completed_total": 0, "skipped_complete": 0,
                                         "manual_review": 0, "failed": 0}, [_Rec()]),
        raising=False,
    )
    monkeypatch.setattr(
        ui,
        "_run_with_live_dashboard",
        lambda runner, run_cases, manifest: (final_stats, None),
        raising=False,
    )
    monkeypatch.setattr(ui, "_write_automation_summary", lambda *a, **k: None, raising=False)
    monkeypatch.setattr(ui, "_get_selected_selfie_prompt", lambda: (3, "prompt", "slot"), raising=False)
    monkeypatch.setattr(ui, "_automation_status_lines", lambda: [], raising=False)
    monkeypatch.setattr(ui, "_automation_manifest_path", lambda: tmp_path / "manifest.json", raising=False)
    return ui.run_automation_headless(str(tmp_path), auto_approve=True)


def test_headless_run_failures_return_two(tmp_path, monkeypatch):
    """Failed cases -> exit 2 (ran-but-needs-attention), NOT 1 (could-not-run)."""
    rc = _run_with_stats(tmp_path, monkeypatch,
                         {"completed": 0, "failed": 1, "manual_review": 0, "skipped": 0})
    assert rc == 2


def test_headless_manual_review_returns_two(tmp_path, monkeypatch):
    """manual_review cases must NOT be silently swallowed as success -- a
    scheduled batch with cases needing human action exits 2 (code-review
    HIGH-1, PR #69)."""
    rc = _run_with_stats(tmp_path, monkeypatch,
                         {"completed": 0, "failed": 0, "manual_review": 2, "skipped": 0})
    assert rc == 2


def test_headless_non_tty_runs_pipeline_directly_no_dashboard(tmp_path, monkeypatch):
    """Under a non-TTY (cron/pipe), the run goes straight through runner.run --
    NOT the Rich live dashboard (no ANSI pollution / polling thread). Code-review
    Gemini MEDIUM, PR #69."""
    monkeypatch.setattr("builtins.input", _forbid_input)
    monkeypatch.setattr(kling_automation_ui.sys.stdout, "isatty", lambda: False, raising=False)

    class _Rec:
        relative_key = "case-a"

    monkeypatch.setattr(kling_automation_ui, "discover_case_folders", lambda *a, **k: [_Rec()])

    class _Manifest:
        manifest_path = tmp_path / "manifest.json"
        data = {"cases": {}}

        @classmethod
        def create_or_load(cls, **kwargs):
            return cls()

    monkeypatch.setattr(kling_automation_ui, "AutomationManifest", _Manifest)

    ran = {"direct": False}

    class _Runner:
        last_case_results = {"case-a": {"status": "complete", "reason": ""}}

        def __init__(self, **kwargs):
            pass

        def validate_configuration(self):
            return []

        def run(self, cases):
            ran["direct"] = True
            return {"completed": 1, "failed": 0, "manual_review": 0, "skipped": 0}

    monkeypatch.setattr(kling_automation_ui, "AutoPipelineRunner", _Runner)

    ui = _bare_ui(tmp_path)
    # If the dashboard is wrongly used in non-TTY, this blows up the test.
    monkeypatch.setattr(
        ui, "_run_with_live_dashboard",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("dashboard used in non-TTY")),
        raising=False,
    )
    monkeypatch.setattr(
        ui, "_collect_case_snapshot",
        lambda records, manifest: ([], {"will_run": 1, "discovered": 1, "pending": 1,
                                         "completed_total": 0, "skipped_complete": 0,
                                         "manual_review": 0, "failed": 0}, [_Rec()]),
        raising=False,
    )
    monkeypatch.setattr(ui, "_write_automation_summary", lambda *a, **k: None, raising=False)
    monkeypatch.setattr(ui, "_get_selected_selfie_prompt", lambda: (3, "prompt", "slot"), raising=False)
    monkeypatch.setattr(ui, "_automation_status_lines", lambda: [], raising=False)
    monkeypatch.setattr(ui, "_automation_manifest_path", lambda: tmp_path / "manifest.json", raising=False)

    rc = ui.run_automation_headless(str(tmp_path), auto_approve=True)
    assert rc == 0
    assert ran["direct"] is True


def _reach_override(tmp_path, monkeypatch, reprocess_override):
    """Drive run_automation_headless past discovery + manifest to the override
    block (the override is applied POST-manifest, so empty discovery would return
    before it). Stops at preflight via a validate_configuration issue."""
    monkeypatch.setattr("builtins.input", _forbid_input)

    class _Rec:
        relative_key = "case-a"

    monkeypatch.setattr(kling_automation_ui, "discover_case_folders", lambda *a, **k: [_Rec()])

    class _Manifest:
        manifest_path = tmp_path / "manifest.json"
        data = {"cases": {}}

        @classmethod
        def create_or_load(cls, **kwargs):
            return cls()

    monkeypatch.setattr(kling_automation_ui, "AutomationManifest", _Manifest)

    class _Runner:
        last_case_results = {}

        def __init__(self, **kwargs):
            pass

        def validate_configuration(self):
            return ["stop-after-overrides"]

    monkeypatch.setattr(kling_automation_ui, "AutoPipelineRunner", _Runner)

    ui = _bare_ui(tmp_path)
    monkeypatch.setattr(
        ui, "_collect_case_snapshot",
        lambda records, manifest: ([], {"will_run": 1, "discovered": 1, "pending": 1,
                                         "completed_total": 0, "skipped_complete": 0,
                                         "manual_review": 0, "failed": 0}, [_Rec()]),
        raising=False,
    )
    monkeypatch.setattr(ui, "_automation_manifest_path", lambda: tmp_path / "manifest.json", raising=False)
    rc = ui.run_automation_headless(str(tmp_path), auto_approve=True, reprocess_override=reprocess_override)
    return ui, rc


def test_reprocess_overwrite_disables_skip_guards(tmp_path, monkeypatch):
    """--reprocess overwrite/increment must drop automation_skip_completed +
    skip_if_*_exists so completed manifest cases actually re-run, else
    _planned_action_for_case returns skip_complete and the batch reports 'no
    runnable cases' (code-review Codex P1, PR #69)."""
    ui, rc = _reach_override(tmp_path, monkeypatch, "overwrite")
    assert ui.config["automation_skip_completed"] is False
    assert ui.config["automation_skip_if_selfie_exists"] is False
    assert ui.config["automation_skip_if_video_exists"] is False
    assert ui.config["automation_allow_reprocess"] is True
    assert ui.config["automation_reprocess_mode"] == "overwrite"


def test_reprocess_skip_leaves_guards_untouched(tmp_path, monkeypatch):
    """--reprocess skip must NOT drop the skip guards (only overwrite/increment do)."""
    ui, rc = _reach_override(tmp_path, monkeypatch, "skip")
    # skip mode sets reprocess_mode + allow_reprocess but must NOT clear the
    # skip-completed guard (skip is the safe default behaviour).
    assert ui.config["automation_reprocess_mode"] == "skip"
    assert ui.config.get("automation_skip_completed", True) is not False
