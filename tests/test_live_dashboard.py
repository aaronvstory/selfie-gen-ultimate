"""Live-dashboard rebuild tests (2026-06-11).

The old dashboard stacked dozens of partial panels: unlocked shared state,
a second Console fighting the app console, and the root logger's
StreamHandler writing raw lines through Rich Live. These tests pin the
fixed building blocks: the pure panel builder, console-logging suppression
(file handlers kept — FileHandler subclasses StreamHandler!), the
thread-safe manifest snapshot, and the pipeline's pause/abort semantics.
"""

import logging
import sys
import threading
from pathlib import Path

from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from automation.config import from_app_config, merge_automation_defaults  # noqa: E402
from automation.discovery import CaseRecord  # noqa: E402
from automation.manifest import AutomationManifest  # noqa: E402
from automation.pipeline import AutoPipelineRunner, PipelineDeps  # noqa: E402
from kling_automation_ui import KlingAutomationUI  # noqa: E402

from tests.test_automation_pipeline import FakeOutpaint, FakeSelfie, FakeVideo  # noqa: E402


# ---------------------------------------------------------------------------
# _build_dashboard_panel (pure renderable)
# ---------------------------------------------------------------------------


def _render_text(panel):
    from rich.console import Console
    import io

    console = Console(file=io.StringIO(), width=120, legacy_windows=False)
    console.print(panel)
    return console.file.getvalue()


def test_dashboard_panel_renders_all_fields():
    panel = KlingAutomationUI._build_dashboard_panel(
        total=5,
        counts={"completed": 2, "failed": 1, "manual_review": 0, "skipped": 0},
        current_case="User_12016",
        current_step="6 kling video",
        similarity="91",
        last_output="Output: video.mp4",
        error_reason="-",
        events=[("01:02:03", "info", "an event"), ("01:02:04", "error", "boom")],
        footer="[p] pause",
    )
    text = _render_text(panel)
    assert "3/5 (60%)" in text
    assert "User_12016" in text
    assert "6 kling video" in text
    assert "91" in text
    assert "an event" in text
    assert "boom" in text
    assert "remaining=2" in text


def test_dashboard_panel_zero_total_is_100_percent():
    panel = KlingAutomationUI._build_dashboard_panel(
        total=0,
        counts={},
        current_case="-",
        current_step="-",
        similarity="-",
        last_output="-",
        error_reason="-",
        events=[],
        footer="",
    )
    assert "0/0 (100%)" in _render_text(panel)


def test_dashboard_panel_round7_queue_next_and_step_progress():
    """Round 7: the panel shows the per-case queue, the NEXT step, the step
    position, and the in-step progress line (fed by progress_update events)."""
    panel = KlingAutomationUI._build_dashboard_panel(
        total=3,
        counts={"completed": 1, "failed": 0, "manual_review": 0, "skipped": 0},
        current_case="User_2",
        current_step="6 kling video",
        similarity="-",
        last_output="-",
        error_reason="-",
        events=[],
        footer="f",
        queue_lines=[
            "[green][ ok ][/green] User_1",
            "[bold cyan][ >> ][/bold cyan] User_2",
            "[dim][    ][/dim] User_3",
        ],
        next_step="7 rppg injection",
        step_pos="step 6/10",
        step_progress="Generating… poll 14 · 73s elapsed",
    )
    text = _render_text(panel)
    assert "User_1" in text and "User_2" in text and "User_3" in text
    assert "7 rppg injection" in text
    assert "step 6/10" in text
    assert "poll 14" in text


def test_build_queue_lines_small_batch_lists_every_folder():
    snap = {"a": {"status": "complete"}, "b": {"status": "running"}, "c": {"status": "pending"}}
    lines = KlingAutomationUI._build_queue_lines(["a", "b", "c"], snap)
    assert len(lines) == 3
    assert " ok " in lines[0] and lines[0].endswith(" a")
    assert " >> " in lines[1] and lines[1].endswith(" b")


def test_build_queue_lines_large_batch_collapses_finished_and_tail():
    keys = [f"c{i:02d}" for i in range(30)]
    snap = {k: {"status": "complete"} for k in keys[:10]}
    snap[keys[10]] = {"status": "running"}
    snap.update({k: {"status": "pending"} for k in keys[11:]})
    lines = KlingAutomationUI._build_queue_lines(keys, snap, max_rows=12)
    assert len(lines) <= 12
    assert any("10 case(s) finished" in line for line in lines)
    assert any("c10" in line for line in lines), "the RUNNING case must stay visible"
    assert any("more pending" in line for line in lines)


# ---------------------------------------------------------------------------
# _suppress_stream_logging
# ---------------------------------------------------------------------------


def test_suppress_stream_logging_removes_console_keeps_file(tmp_path):
    root = logging.getLogger()
    original = list(root.handlers)
    stream_handler = logging.StreamHandler(sys.stdout)
    file_handler = logging.FileHandler(tmp_path / "t.log")
    root.addHandler(stream_handler)
    root.addHandler(file_handler)
    try:
        with KlingAutomationUI._suppress_stream_logging():
            assert stream_handler not in root.handlers, "console handler must be detached"
            assert file_handler in root.handlers, "file handler must be KEPT"
        assert stream_handler in root.handlers, "console handler must be restored"
        assert file_handler in root.handlers
    finally:
        root.removeHandler(stream_handler)
        file_handler.close()
        root.removeHandler(file_handler)
        for h in original:
            if h not in root.handlers:
                root.addHandler(h)


def test_suppress_stream_logging_restrip_catches_late_handlers(tmp_path):
    """Round-8 regression: deepface/TF/absl import LAZILY inside the run and
    install fresh console handlers on the ROOT logger AFTER the initial
    strip — their WARNING lines printed bare through Live and shattered the
    panel. The context manager yields a re-strip callable the dashboard loop
    invokes every tick; it must also catch handlers that are NOT
    StreamHandler subclasses (absl's ABSLHandler isn't)."""

    class FakeAbslHandler(logging.Handler):  # mimics absl.logging.ABSLHandler
        def emit(self, record):  # pragma: no cover - never called
            pass

    root = logging.getLogger()
    file_handler = logging.FileHandler(tmp_path / "t.log")
    root.addHandler(file_handler)
    late_stream = logging.StreamHandler(sys.stderr)
    late_absl = FakeAbslHandler()
    try:
        with KlingAutomationUI._suppress_stream_logging() as restrip:
            # Simulate the mid-run lazy import adding console handlers:
            root.addHandler(late_stream)
            root.addHandler(late_absl)
            restrip()  # what the dashboard loop does every tick
            assert late_stream not in root.handlers, "late StreamHandler must be stripped"
            assert late_absl not in root.handlers, "late non-StreamHandler console handler must be stripped"
            assert file_handler in root.handlers, "file handler must always be KEPT"
        # Everything stripped during the window is restored afterwards.
        assert late_stream in root.handlers
        assert late_absl in root.handlers
        assert file_handler in root.handlers
    finally:
        for h in (late_stream, late_absl):
            root.removeHandler(h)
        file_handler.close()
        root.removeHandler(file_handler)


def test_suppress_stream_logging_neutralizes_lastresort():
    """Round-8 REAL-run finding: when absl/TF import before setup_logging,
    basicConfig is a no-op and the strip can leave root with ZERO handlers —
    Python's logging.lastResort (not in root.handlers, unstrippable) then
    prints every WARNING bare to stderr through Live. The CM must keep at
    least a guard handler on root so lastResort can never fire."""
    root = logging.getLogger()
    saved = list(root.handlers)
    for h in saved:
        root.removeHandler(h)
    try:
        with KlingAutomationUI._suppress_stream_logging():
            assert root.handlers, (
                "root must keep a guard handler during the Live window — "
                "an empty root.handlers re-enables logging.lastResort"
            )
        assert not root.handlers, "the guard must be removed on exit"
    finally:
        for h in saved:
            root.addHandler(h)


def test_suppress_stream_logging_restores_on_exception(tmp_path):
    root = logging.getLogger()
    stream_handler = logging.StreamHandler(sys.stderr)
    root.addHandler(stream_handler)
    try:
        try:
            with KlingAutomationUI._suppress_stream_logging():
                raise RuntimeError("boom")
        except RuntimeError:
            pass
        assert stream_handler in root.handlers
    finally:
        root.removeHandler(stream_handler)


# ---------------------------------------------------------------------------
# AutomationManifest.snapshot_statuses
# ---------------------------------------------------------------------------


def test_snapshot_statuses_returns_copies(tmp_path):
    manifest = AutomationManifest.create_or_load(tmp_path / "m.json", tmp_path, {})
    manifest.ensure_case("c1", tmp_path / "c1", tmp_path / "c1" / "front.jpg")
    manifest.update_step("c1", "similarity_gate", "complete", meta={"score": 91})
    snap = manifest.snapshot_statuses(["c1", "missing"])
    assert snap["c1"]["status"] == "pending"
    assert snap["c1"]["similarity"] == 91
    assert snap["missing"]["status"] == "pending"
    # Mutating the snapshot must not touch the manifest.
    snap["c1"]["status"] = "hacked"
    assert manifest.data["cases"]["c1"]["status"] != "hacked"


def test_manifest_update_step_is_thread_safe_under_snapshot_reads(tmp_path):
    manifest = AutomationManifest.create_or_load(tmp_path / "m.json", tmp_path, {})
    keys = [f"c{i}" for i in range(8)]
    for key in keys:
        manifest.ensure_case(key, tmp_path / key, tmp_path / key / "front.jpg")
    errors = []

    def writer():
        try:
            for _ in range(40):
                for key in keys:
                    manifest.update_step(key, "front_expand", "complete", output="x.png")
        except Exception as exc:  # pragma: no cover
            errors.append(exc)

    def reader():
        try:
            for _ in range(300):
                manifest.snapshot_statuses(keys)
        except Exception as exc:  # pragma: no cover
            errors.append(exc)

    threads = [threading.Thread(target=writer), threading.Thread(target=reader)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors


# ---------------------------------------------------------------------------
# Pipeline pause/abort
# ---------------------------------------------------------------------------


def _build_runner(tmp_path, monkeypatch, n_cases=2):
    records = []
    for i in range(n_cases):
        case_dir = tmp_path / f"case-{i}"
        case_dir.mkdir()
        front = case_dir / "front.png"
        Image.new("RGB", (64, 64), (1, 1, 1)).save(front)
        records.append(CaseRecord(case_dir=case_dir, front_path=front, relative_key=f"case-{i}"))

    config = merge_automation_defaults({
        "falai_api_key": "x",
        "bfl_api_key": "bfl-token",
        "automation_oldcam_required": False,
        "saved_prompts": {"1": "prompt"},
        "current_prompt_slot": 1,
    })
    manifest = AutomationManifest.create_or_load(tmp_path / "automation_manifest.json", tmp_path, {})
    for record in records:
        manifest.ensure_case(record.relative_key, record.case_dir, record.front_path)

    monkeypatch.setattr(
        "automation.pipeline.extract_portrait_crop",
        lambda **kwargs: {"confidence": 0.9, "crop_box": [0, 0, 10, 10], "extractor": "mock"},
    )
    monkeypatch.setattr(
        "automation.pipeline.compute_face_similarity_details",
        lambda *args, **kwargs: {"score": 90, "pass": True, "error": None, "match": True},
    )
    monkeypatch.setattr("automation.pipeline.run_oldcam_all", lambda **kwargs: [])

    runner = AutoPipelineRunner(
        config=config,
        automation_config=from_app_config(config),
        manifest=manifest,
        progress_cb=lambda msg, level="info": None,
        deps=PipelineDeps(
            outpaint_factory=lambda: FakeOutpaint(),
            selfie_factory=lambda: FakeSelfie(),
            video_factory=lambda: FakeVideo(),
        ),
    )
    return runner, records, manifest


def test_pause_event_stops_between_cases(tmp_path, monkeypatch):
    runner, records, manifest = _build_runner(tmp_path, monkeypatch, n_cases=2)
    runner.pause_event.set()  # set before the run: nothing should start
    stats = runner.run(records)
    assert stats == {"completed": 0, "failed": 0, "manual_review": 0, "skipped": 0}
    assert runner.stopped_reason == "paused"
    assert manifest.data["cases"]["case-0"]["status"] == "pending"


def test_abort_mid_case_reverts_to_pending_and_keeps_step_progress(tmp_path, monkeypatch):
    runner, records, manifest = _build_runner(tmp_path, monkeypatch, n_cases=1)

    # Trip the abort as a side effect of the SELFIE generator running —
    # the next _set_active_step transition must raise and stop the case.
    class AbortingSelfie(FakeSelfie):
        def generate(self, *args, **kwargs):
            runner.abort_event.set()
            return super().generate(*args, **kwargs)

    runner.deps = PipelineDeps(
        outpaint_factory=lambda: FakeOutpaint(),
        selfie_factory=lambda: AbortingSelfie(),
        video_factory=lambda: FakeVideo(),
    )
    stats = runner.run(records)
    assert runner.stopped_reason == "aborted"
    assert stats["completed"] == 0 and stats["failed"] == 0
    case = manifest.data["cases"]["case-0"]
    # Case is resumable, not failed/lost:
    assert case["status"] == "pending"
    assert case["active_step"] is None
    # Completed steps BEFORE the abort point keep their manifest state.
    assert case["steps"]["front_expand"]["status"] == "complete"
    assert case["steps"]["selfie_generate"]["status"] in {"complete", "running"}
    # The not-yet-reached post steps are untouched.
    assert case["steps"]["oldcam"]["status"] == "pending"


def test_abort_then_resume_completes_the_case(tmp_path, monkeypatch):
    runner, records, manifest = _build_runner(tmp_path, monkeypatch, n_cases=1)
    runner.abort_event.set()
    # Abort fires at the FIRST step transition -> nothing ran.
    runner.run(records)
    assert manifest.data["cases"]["case-0"]["status"] == "pending"

    # Fresh runner (same manifest) resumes and completes.
    resumed = AutoPipelineRunner(
        config=runner.config,
        automation_config=runner.automation,
        manifest=manifest,
        progress_cb=lambda msg, level="info": None,
        deps=PipelineDeps(
            outpaint_factory=lambda: FakeOutpaint(),
            selfie_factory=lambda: FakeSelfie(),
            video_factory=lambda: FakeVideo(),
        ),
    )
    stats = resumed.run(records)
    assert stats["completed"] == 1
    assert manifest.data["cases"]["case-0"]["status"] == "complete"
