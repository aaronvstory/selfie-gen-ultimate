"""CLI batch approval — prominent max-cases + folder confirmation step.

Covers the approval-screen UX added so the operator can SEE and TRUST the
max-cases cap before any API call (user report: "set max to 5 but it still
shows 1 case"; the cap was correct — only 1 folder existed — but nothing on
screen made that legible).

Three behaviours are pinned here:
  1. max-cases now accepts ANY positive integer (or "all"), not just {1,5,10}.
  2. the approval menu carries a one-tap "🔢 Max cases per run: N · change" item.
  3. an after-approval confirmation partitions discovered folders into
     process / deferred-over-cap / skipped(+reason) and returns run/back/cancel.
"""
from __future__ import annotations

from kling_automation_ui import KlingAutomationUI


def _bare_ui(config: dict | None = None) -> KlingAutomationUI:
    ui = KlingAutomationUI.__new__(KlingAutomationUI)
    ui.config = dict(config or {})
    ui.automation_root_folder = ui.config.get("automation_root_folder", "")
    return ui


class _Rec:
    """Minimal stand-in for a discovery CaseRecord."""

    def __init__(self, relative_key: str, case_dir: str | None = None):
        self.relative_key = relative_key
        self.case_dir = case_dir or f"/root/{relative_key}"


# ---------------------------------------------------------------------------
# 1. max-cases normalization — custom numbers allowed
# ---------------------------------------------------------------------------

def test_normalize_max_cases_accepts_any_positive_int() -> None:
    ui = _bare_ui()
    assert ui._normalize_max_cases("20") == 20
    assert ui._normalize_max_cases("3") == 3
    assert ui._normalize_max_cases("1") == 1
    assert ui._normalize_max_cases("all") is None


def test_normalize_max_cases_rejects_garbage_to_default() -> None:
    ui = _bare_ui()
    assert ui._normalize_max_cases("0") == 5       # not positive -> default
    assert ui._normalize_max_cases("-4") == 5      # not a digit string
    assert ui._normalize_max_cases("abc") == 5
    assert ui._normalize_max_cases("") == 5


def test_read_max_cases_setting_normalizes_string() -> None:
    assert _bare_ui({"automation_max_cases_per_run": "05"})._read_max_cases_setting() == "5"
    assert _bare_ui({"automation_max_cases_per_run": "all"})._read_max_cases_setting() == "all"
    assert _bare_ui({"automation_max_cases_per_run": 20})._read_max_cases_setting() == "20"
    assert _bare_ui({"automation_max_cases_per_run": "junk"})._read_max_cases_setting() == "5"
    assert _bare_ui({})._read_max_cases_setting() == "5"  # unset -> default


# ---------------------------------------------------------------------------
# 2. friendly folder name ("." -> root basename)
# ---------------------------------------------------------------------------

def test_case_display_name_root_self_uses_basename() -> None:
    rec = _Rec(".", case_dir="/data/Katherine Rhoads (398569)_front")
    assert KlingAutomationUI._case_display_name(rec) == "Katherine Rhoads (398569)_front"


def test_case_display_name_subfolder_uses_relative_key() -> None:
    rec = _Rec("subject-A")
    assert KlingAutomationUI._case_display_name(rec) == "subject-A"


# ---------------------------------------------------------------------------
# 3. partition logic: process / deferred-over-cap / skipped
# ---------------------------------------------------------------------------

def _rows() -> "list[dict]":
    return [
        {"case": "a", "planned": "run_pending"},
        {"case": "b", "planned": "run_pending"},
        {"case": "c", "planned": "run_front_changed"},
        {"case": "done1", "planned": "skip_complete"},
        {"case": "bad1", "planned": "failed"},
        {"case": "mr1", "planned": "manual_review"},
    ]


def test_partition_deferred_over_cap() -> None:
    # 3 runnable, cap=2 -> 1 deferred; 3 skipped of distinct reasons.
    deferred, skipped = KlingAutomationUI._partition_batch_rows(_rows(), 2)
    assert [r["case"] for r in deferred] == ["c"]
    assert {r["case"] for r in skipped} == {"done1", "bad1", "mr1"}


def test_partition_no_deferred_when_cap_covers_all() -> None:
    deferred, skipped = KlingAutomationUI._partition_batch_rows(_rows(), 3)
    assert deferred == []
    assert len(skipped) == 3


def test_cap_math_holds_for_non_preset_value() -> None:
    # 25 runnable, cap=20 -> 5 deferred (the user-chosen custom cap is honored).
    rows = [{"case": f"c{i}", "planned": "run_pending"} for i in range(25)]
    deferred, skipped = KlingAutomationUI._partition_batch_rows(rows, 20)
    assert len(deferred) == 5
    assert skipped == []


# ---------------------------------------------------------------------------
# 4. confirmation flow returns run / back / cancel
# ---------------------------------------------------------------------------

def _confirm_ui(selection: "str | None") -> KlingAutomationUI:
    ui = _bare_ui({"automation_max_cases_per_run": "2"})
    ui._use_legacy_prompt_ui = lambda: False  # type: ignore[assignment]
    ui.display_header = lambda *a, **k: None   # type: ignore[assignment]
    ui._q_select = lambda *a, **k: selection   # type: ignore[assignment]
    return ui


def test_confirm_batch_folders_proceed() -> None:
    ui = _confirm_ui("run")
    out = ui._confirm_batch_folders(_rows(), [_Rec("a"), _Rec("b")], "/root")
    assert out == "run"


def test_confirm_batch_folders_back() -> None:
    ui = _confirm_ui("back")
    out = ui._confirm_batch_folders(_rows(), [_Rec("a"), _Rec("b")], "/root")
    assert out == "back"


def test_confirm_batch_folders_cancel_on_none() -> None:
    ui = _confirm_ui(None)  # Esc/Ctrl-C -> treated as cancel
    out = ui._confirm_batch_folders(_rows(), [_Rec("a"), _Rec("b")], "/root")
    assert out == "cancel"


def test_confirm_batch_folders_nothing_to_run_returns_back() -> None:
    ui = _confirm_ui("run")
    pauses = []
    ui.print_yellow = lambda msg: pauses.append(msg)   # type: ignore[assignment]
    ui.pause_review = lambda *a, **k: pauses.append("paused")  # type: ignore[assignment]
    out = ui._confirm_batch_folders(_rows(), [], "/root")
    assert out == "back"          # no runnable cases -> bounce back, never run
    assert pauses                 # user was told why


def test_confirm_batch_folders_legacy_yes_runs() -> None:
    ui = _bare_ui({"automation_max_cases_per_run": "2"})
    ui._use_legacy_prompt_ui = lambda: True    # type: ignore[assignment]
    ui._confirm = lambda *a, **k: True         # type: ignore[assignment]
    out = ui._confirm_batch_folders(_rows(), [_Rec("a")], "/root")
    assert out == "run"


def test_confirm_batch_folders_legacy_no_cancels() -> None:
    ui = _bare_ui({"automation_max_cases_per_run": "2"})
    ui._use_legacy_prompt_ui = lambda: True    # type: ignore[assignment]
    ui._confirm = lambda *a, **k: False        # type: ignore[assignment]
    out = ui._confirm_batch_folders(_rows(), [_Rec("a")], "/root")
    assert out == "cancel"


# ---------------------------------------------------------------------------
# 5. quick-edit menu still wires the max-cases field
# ---------------------------------------------------------------------------

def test_quick_edit_pairs_include_batch_max() -> None:
    # Distinct dict objects per key — dict.fromkeys(..., {}) would alias one
    # shared dict and could mask a real mutation regression (CodeRabbit).
    ui = _bare_ui({k: {} for k in
                   ("automation_selfie_models", "automation_selfie_prompts", "saved_prompts")})
    ui.automation_root_folder = "/root"
    pairs = ui._quick_edit_choice_pairs()
    values = {v for _label, v in pairs}
    assert "batch_max" in values


# ---------------------------------------------------------------------------
# 6. free-text editor (legacy/non-TTY path) accepts custom numbers
# ---------------------------------------------------------------------------

def test_is_valid_max_cases() -> None:
    f = KlingAutomationUI._is_valid_max_cases
    assert f("20") and f("1") and f("all") and f("ALL")
    assert not f("0") and not f("-2") and not f("abc") and not f("")


def test_prompt_max_cases_legacy_accepts_custom(monkeypatch) -> None:
    ui = _bare_ui({"automation_max_cases_per_run": "5"})
    ui._use_legacy_prompt_ui = lambda: True   # type: ignore[assignment]
    monkeypatch.setattr("builtins.input", lambda *a, **k: "20")
    ui._prompt_max_cases()
    assert ui.config["automation_max_cases_per_run"] == "20"


def test_prompt_max_cases_legacy_rejects_invalid(monkeypatch) -> None:
    ui = _bare_ui({"automation_max_cases_per_run": "5"})
    ui._use_legacy_prompt_ui = lambda: True   # type: ignore[assignment]
    monkeypatch.setattr("builtins.input", lambda *a, **k: "0")
    ui._prompt_max_cases()
    assert ui.config["automation_max_cases_per_run"] == "5"  # unchanged


def test_prompt_max_cases_legacy_blank_keeps(monkeypatch) -> None:
    ui = _bare_ui({"automation_max_cases_per_run": "7"})
    ui._use_legacy_prompt_ui = lambda: True   # type: ignore[assignment]
    monkeypatch.setattr("builtins.input", lambda *a, **k: "")
    ui._prompt_max_cases()
    assert ui.config["automation_max_cases_per_run"] == "7"


def test_prompt_max_cases_legacy_invalid_warns(monkeypatch) -> None:
    # Invalid (non-empty) input must warn, not silently keep (Gemini review).
    ui = _bare_ui({"automation_max_cases_per_run": "5"})
    ui._use_legacy_prompt_ui = lambda: True   # type: ignore[assignment]
    warned = []
    ui.print_red = lambda msg: warned.append(msg)   # type: ignore[assignment]
    monkeypatch.setattr("builtins.input", lambda *a, **k: "abc")
    ui._prompt_max_cases()
    assert ui.config["automation_max_cases_per_run"] == "5"  # unchanged
    assert warned                                            # user was warned


def test_case_display_name_uses_root_when_case_dir_missing() -> None:
    # "." with a genuinely-absent case_dir falls back to the root basename
    # (Sourcery). Build a bare record so _Rec's default path doesn't apply.
    rec = type("R", (), {"relative_key": ".", "case_dir": None})()
    assert KlingAutomationUI._case_display_name(rec, root="/data/rootdir") == "rootdir"


def test_case_display_name_resolve_oserror_falls_back(monkeypatch) -> None:
    # .resolve() can raise OSError on restricted filesystems — fall back to the
    # unresolved basename instead of crawling the batch (Gemini round 2).
    import kling_automation_ui as kui

    def _boom(self, *a, **k):
        raise OSError("restricted fs")

    monkeypatch.setattr(kui.Path, "resolve", _boom)
    rec = _Rec(".", case_dir="/data/Subject_front")
    assert KlingAutomationUI._case_display_name(rec) == "Subject_front"


def test_recommended_defaults_preserve_custom_max_cases() -> None:
    # The ⭐ recommended-defaults reset must NOT clobber a custom cap. Drive the
    # REAL baseline writer (not just the validity gate) so a clobber-to-"1"
    # regression actually fails here (Gemini + CodeRabbit).
    ui = _bare_ui({"automation_max_cases_per_run": "20"})
    ui._write_recommended_automation_baseline()
    assert ui.config["automation_max_cases_per_run"] == "20"


def test_recommended_defaults_reset_invalid_max_cases_to_one() -> None:
    # An invalid/missing prior cap is reset to "1" by the baseline writer.
    ui = _bare_ui({"automation_max_cases_per_run": "garbage"})
    ui._write_recommended_automation_baseline()
    assert ui.config["automation_max_cases_per_run"] == "1"
