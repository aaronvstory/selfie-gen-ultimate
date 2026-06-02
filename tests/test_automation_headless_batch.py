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


def test_headless_missing_root_returns_nonzero(tmp_path, monkeypatch):
    monkeypatch.setattr("builtins.input", _forbid_input)
    ui = _bare_ui(tmp_path)
    missing = tmp_path / "does-not-exist"
    rc = ui.run_automation_headless(str(missing), auto_approve=True)
    assert rc != 0


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


def test_headless_run_failures_return_nonzero(tmp_path, monkeypatch):
    """If the pipeline reports failed cases, the batch exits non-zero so a
    scheduler treats it as a failed job."""
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
        last_case_results = {"case-a": {"status": "failed", "reason": "x"}}

        def __init__(self, **kwargs):
            pass

        def validate_configuration(self):
            return []

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
    monkeypatch.setattr(
        ui,
        "_run_with_live_dashboard",
        lambda runner, run_cases, manifest: ({"completed": 0, "failed": 1, "manual_review": 0, "skipped": 0}, None),
        raising=False,
    )
    monkeypatch.setattr(ui, "_write_automation_summary", lambda *a, **k: None, raising=False)
    monkeypatch.setattr(ui, "_get_selected_selfie_prompt", lambda: (3, "prompt", "slot"), raising=False)
    monkeypatch.setattr(ui, "_automation_status_lines", lambda: [], raising=False)
    monkeypatch.setattr(ui, "_automation_manifest_path", lambda: tmp_path / "manifest.json", raising=False)

    rc = ui.run_automation_headless(str(tmp_path), auto_approve=True)
    assert rc != 0
