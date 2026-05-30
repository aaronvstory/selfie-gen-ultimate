"""Argv shape tests for automation.rppg.run_rppg.

These tests capture the exact ``subprocess.Popen`` argv the rPPG runner
constructs. They were added in PR #43 to lock in:

* Iterative mode is the production default (friend confirmed mandatory).
* ``--iterate-from-baseline`` rides with ``--iterative``.
* ``--skip-diagnosis`` rides with ``--iterative`` (avoids the Claude-
  API "clod diagnostics" postscript).
* ``--skip-kinematic-gate`` is preserved as a tier-1 default.
* Order matches ``rPPG/rppg.bat`` (the friend's canonical launcher).
* Per-call kwargs override the defaults for back-to-back testing.

These pair with the GUI queue-manager cmd in
``kling_gui/queue_manager.py::_rppg_video`` — the same flag set is
duplicated there because the GUI runs the .bat directly (no pipeline).
A future refactor could collapse both call sites into a single helper.
"""

from __future__ import annotations

from pathlib import Path
from unittest import mock

import pytest

from automation import rppg as rppg_module


@pytest.fixture
def _stub_launcher(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Force resolve_rppg_launcher to return a real (empty) file so
    run_rppg gets past the existence check and reaches the cmd build."""
    fake_launcher = tmp_path / "rPPG" / "run_rppg.bat"
    fake_launcher.parent.mkdir()
    fake_launcher.write_text("@echo off\n", encoding="utf-8")
    monkeypatch.setattr(
        rppg_module,
        "resolve_rppg_launcher",
        lambda root: fake_launcher,
    )
    return fake_launcher


@pytest.fixture
def _stub_subprocess(monkeypatch: pytest.MonkeyPatch) -> list:
    """Capture the argv list passed to stream_subprocess_with_timeout.
    Return a list that the test reads after run_rppg returns."""
    captured: list = []

    def _fake_stream(
        cmd, *, cwd, timeout_seconds, on_line, deadline_extender=None,
        on_heartbeat=None, heartbeat_interval_seconds=60.0,
        heartbeat_silence_predicate=None,
    ):
        # Swallow the v2.7 heartbeat kwargs added by the streamer; the
        # cmd-shape tests only care about argv content, not progress
        # signalling.
        del (
            cwd, timeout_seconds, on_line, deadline_extender,
            on_heartbeat, heartbeat_interval_seconds,
            heartbeat_silence_predicate,
        )
        captured.append(list(cmd))
        return (0, ["fake-stdout"])

    monkeypatch.setattr(
        rppg_module, "stream_subprocess_with_timeout", _fake_stream
    )
    # resolve_produced_output expects a real file; stub it so the
    # post-run "did the injector write the file?" check passes.
    # PR fix/step0-composite-and-rppg-v2.5 added a keyword-only
    # progress_cb parameter — accept and ignore it here.
    monkeypatch.setattr(
        rppg_module, "resolve_produced_output", lambda p, **_kw: p,
    )
    monkeypatch.setattr(
        rppg_module,
        "finalize_rppg_output",
        lambda produced, requested, **kw: produced,
    )
    return captured


def _make_input(tmp_path: Path) -> Path:
    """Touch a real input file so the run_rppg existence guard passes."""
    p = tmp_path / "case-clip.mp4"
    p.write_bytes(b"\x00\x00\x00\x00mp4")
    return p


def test_default_cmd_is_iterative_mode(tmp_path, _stub_launcher, _stub_subprocess):
    """Default invocation: --iterative + --iterate-from-baseline +
    --skip-diagnosis + --skip-kinematic-gate. Order mirrors rPPG/rppg.bat.
    """
    inp = _make_input(tmp_path)
    rppg_module.run_rppg(video_path=inp, repo_root=tmp_path)
    assert len(_stub_subprocess) == 1
    cmd = _stub_subprocess[0]
    assert "--inject" in cmd
    assert "--iterative" in cmd
    assert "--iterate-from-baseline" in cmd
    assert "--skip-diagnosis" in cmd
    assert "--skip-kinematic-gate" in cmd
    # The .bat orders flags as: --inject ... --iterative
    # --iterate-from-baseline --skip-diagnosis (then we add
    # --skip-kinematic-gate). Verify --iterative precedes the
    # companion flags so a strict-mode CLI wouldn't reject the order.
    iter_idx = cmd.index("--iterative")
    assert cmd.index("--iterate-from-baseline") > iter_idx
    assert cmd.index("--skip-diagnosis") > iter_idx


def test_one_shot_mode_drops_iterative_companions(
    tmp_path, _stub_launcher, _stub_subprocess,
):
    """iterative=False disables --iterative AND its companion flags
    (--iterate-from-baseline, --skip-diagnosis). --skip-kinematic-gate
    stays (it's not iterative-scoped). For back-to-back testing.
    """
    inp = _make_input(tmp_path)
    rppg_module.run_rppg(video_path=inp, repo_root=tmp_path, iterative=False)
    cmd = _stub_subprocess[0]
    assert "--inject" in cmd
    assert "--iterative" not in cmd
    assert "--iterate-from-baseline" not in cmd
    assert "--skip-diagnosis" not in cmd
    # Kinematic gate is independent of iterative mode.
    assert "--skip-kinematic-gate" in cmd


def test_iterate_from_baseline_can_be_disabled_individually(
    tmp_path, _stub_launcher, _stub_subprocess,
):
    """iterative=True + iterate_from_baseline=False = cumulative
    iteration mode (each iter builds on the prior output). Used for
    A/B against the baseline-reset behaviour."""
    inp = _make_input(tmp_path)
    rppg_module.run_rppg(
        video_path=inp, repo_root=tmp_path, iterate_from_baseline=False,
    )
    cmd = _stub_subprocess[0]
    assert "--iterative" in cmd
    assert "--iterate-from-baseline" not in cmd
    # skip_diagnosis is independent of iterate_from_baseline.
    assert "--skip-diagnosis" in cmd


def test_skip_diagnosis_can_be_disabled_individually(
    tmp_path, _stub_launcher, _stub_subprocess,
):
    """User can opt back in to the Claude-API diagnosis if they want
    the postscript (requires ANTHROPIC_API_KEY + API spend)."""
    inp = _make_input(tmp_path)
    rppg_module.run_rppg(
        video_path=inp, repo_root=tmp_path, skip_diagnosis=False,
    )
    cmd = _stub_subprocess[0]
    assert "--iterative" in cmd
    assert "--iterate-from-baseline" in cmd
    assert "--skip-diagnosis" not in cmd


def test_skip_kinematic_gate_can_be_disabled(
    tmp_path, _stub_launcher, _stub_subprocess,
):
    """The v8 kinematic preflight is README-marked untested but can
    be re-enabled per-call."""
    inp = _make_input(tmp_path)
    rppg_module.run_rppg(
        video_path=inp, repo_root=tmp_path, skip_kinematic_gate=False,
    )
    cmd = _stub_subprocess[0]
    assert "--skip-kinematic-gate" not in cmd


def test_landmark_stride_default_omits_flag(
    tmp_path, _stub_launcher, _stub_subprocess,
):
    """Default landmark_stride=1 (reverted from 3 in
    PR fix/step0-composite-and-rppg-v2.5 after PR #52's snapshot-race
    regression). Stride 1 == injector's own default, so the wrapper
    must NOT emit ``--landmark-stride 1`` (keeps the cmd visually
    identical to rPPG/rppg.bat). Users can opt back into stride>1
    via ``automation_rppg_landmark_stride``.
    """
    inp = _make_input(tmp_path)
    rppg_module.run_rppg(video_path=inp, repo_root=tmp_path)
    cmd = _stub_subprocess[0]
    assert "--landmark-stride" not in cmd


def test_landmark_stride_explicit_three_emits_flag(
    tmp_path, _stub_launcher, _stub_subprocess,
):
    """When the caller (config/GUI) explicitly opts into stride=3,
    the wrapper materialises ``--landmark-stride 3`` so the injector
    actually applies the speedup."""
    inp = _make_input(tmp_path)
    rppg_module.run_rppg(video_path=inp, repo_root=tmp_path, landmark_stride=3)
    cmd = _stub_subprocess[0]
    assert "--landmark-stride" in cmd
    idx = cmd.index("--landmark-stride")
    assert cmd[idx + 1] == "3"


def test_landmark_stride_one_omits_flag(
    tmp_path, _stub_launcher, _stub_subprocess,
):
    """Stride 1 == "detect every frame", the injector's own default.
    The wrapper should NOT emit the flag in this case (keeps the cmd
    visually identical to the canonical rPPG/rppg.bat reference and
    makes A/B comparisons against the stride=1 baseline trivial)."""
    inp = _make_input(tmp_path)
    rppg_module.run_rppg(video_path=inp, repo_root=tmp_path, landmark_stride=1)
    cmd = _stub_subprocess[0]
    assert "--landmark-stride" not in cmd


def test_landmark_stride_invalid_input_falls_back_to_signature_default(
    tmp_path, _stub_launcher, _stub_subprocess,
):
    """The pipeline reads landmark_stride from a JSON config that may
    contain user typos (None, "", "fast"). run_rppg must coerce-or-
    default rather than crash the queue worker on a bad value.

    Under PR fix/step0-composite-and-rppg-v2.5 the safety fallback
    matches the signature default (1) — previously it was hardcoded
    to 3 which silently re-enabled the speedup path that caused the
    user's unplayable output. Stride 1 means the wrapper omits the
    flag entirely (== injector default)."""
    inp = _make_input(tmp_path)
    rppg_module.run_rppg(video_path=inp, repo_root=tmp_path, landmark_stride="bad")  # type: ignore[arg-type]
    cmd = _stub_subprocess[0]
    assert "--landmark-stride" not in cmd


def test_landmark_stride_negative_int_floors_to_one(
    tmp_path, _stub_launcher, _stub_subprocess,
):
    """A negative stride is meaningless (`max(1, int(-5)) == 1`). The
    wrapper must NOT emit the flag in that case (stride 1 == injector
    default; emitting it would just be confusing log noise).
    Subagent MEDIUM on PR #52 round 3 — locks in the behaviour so a
    future refactor that adds a different floor doesn't silently
    regress to passing a nonsense negative."""
    inp = _make_input(tmp_path)
    rppg_module.run_rppg(video_path=inp, repo_root=tmp_path, landmark_stride=-5)
    cmd = _stub_subprocess[0]
    assert "--landmark-stride" not in cmd


def _make_extender_capturing_stub(monkeypatch: pytest.MonkeyPatch) -> dict:
    """Captures deadline_extender (in addition to cmd) so tests can assert on it."""
    captured: dict = {}

    def _fake_stream(
        cmd, *, cwd, timeout_seconds, on_line, deadline_extender=None,
        on_heartbeat=None, heartbeat_interval_seconds=60.0,
        heartbeat_silence_predicate=None,
    ):
        del (
            cwd, on_line, on_heartbeat, heartbeat_interval_seconds,
            heartbeat_silence_predicate,
        )
        captured["cmd"] = list(cmd)
        captured["timeout"] = timeout_seconds
        captured["extender"] = deadline_extender
        return (0, ["fake-stdout"])

    monkeypatch.setattr(rppg_module, "stream_subprocess_with_timeout", _fake_stream)
    monkeypatch.setattr(rppg_module, "resolve_produced_output", lambda p, **_kw: p)
    monkeypatch.setattr(
        rppg_module, "finalize_rppg_output",
        lambda produced, requested, **kw: produced,
    )
    return captured


def test_timeout_zero_disables_adaptive_extender(tmp_path, _stub_launcher, monkeypatch):
    """Codex P2 (PR #43, bot pass on 2a32f938): timeout_seconds=0 is the
    documented "hard deadline, no adaptive extension" contract. Before the
    fix, the iterative path wired the deadline_extender unconditionally —
    a caller asking for 0 seconds (e.g. tests, fast-fail) still got many
    extra minutes because each Iteration line added ~90s.
    """
    captured = _make_extender_capturing_stub(monkeypatch)
    inp = _make_input(tmp_path)
    rppg_module.run_rppg(
        video_path=inp, repo_root=tmp_path, timeout_seconds=0, iterative=True,
    )
    assert captured["timeout"] == 0
    assert captured["extender"] is None, (
        "timeout_seconds=0 must disable the deadline extender even when "
        "iterative=True (Codex P2 regression)."
    )


def test_positive_timeout_with_iterative_enables_extender(
    tmp_path, _stub_launcher, monkeypatch,
):
    """Belt-test for the Codex P2 fix: the extender DOES wire when
    iterative=True AND timeout_seconds > 0. Without this we'd be tempted
    to over-tighten the fix to never wire the extender."""
    captured = _make_extender_capturing_stub(monkeypatch)
    inp = _make_input(tmp_path)
    rppg_module.run_rppg(
        video_path=inp, repo_root=tmp_path, timeout_seconds=600, iterative=True,
    )
    assert captured["extender"] is not None
    assert callable(captured["extender"])


def test_timeout_none_falls_through_to_default_and_keeps_extender(
    tmp_path, _stub_launcher, monkeypatch,
):
    """When timeout_seconds=None, run_rppg defaults to 1800s (iterative)
    or 600s (one-shot). With iterative=True the resulting 1800s is
    positive so the extender IS wired — the Codex P2 fix only
    short-circuits the hard-zero contract."""
    captured = _make_extender_capturing_stub(monkeypatch)
    inp = _make_input(tmp_path)
    rppg_module.run_rppg(
        video_path=inp, repo_root=tmp_path, timeout_seconds=None, iterative=True,
    )
    # timeout defaulted to 1800 (iterative)
    assert captured["timeout"] == 1800
    assert captured["extender"] is not None


def test_non_iterative_never_wires_extender(
    tmp_path, _stub_launcher, monkeypatch,
):
    """The extender is iterative-mode-only — its purpose is to ratchet
    the wall clock per Iteration N/M line, and one-shot has no
    iterations to track."""
    captured = _make_extender_capturing_stub(monkeypatch)
    inp = _make_input(tmp_path)
    rppg_module.run_rppg(
        video_path=inp, repo_root=tmp_path, timeout_seconds=300, iterative=False,
    )
    assert captured["extender"] is None


def test_cmd_order_matches_rppg_bat(tmp_path, _stub_launcher, _stub_subprocess):
    """Stronger order assertion: the flag sequence matches the
    canonical launcher (rPPG/rppg.bat line 12) so a strict-mode CLI
    parser couldn't reject the ordering."""
    inp = _make_input(tmp_path)
    rppg_module.run_rppg(video_path=inp, repo_root=tmp_path)
    cmd = _stub_subprocess[0]
    # Find the positions of the canonical flags in the actual cmd.
    positions = {
        flag: cmd.index(flag)
        for flag in [
            "--inject",
            "--iterative",
            "--iterate-from-baseline",
            "--skip-diagnosis",
            "--skip-kinematic-gate",
        ]
    }
    # rPPG/rppg.bat order: --inject ... --iterative --iterate-from-base
    # --skip-diagnosis. We add --skip-kinematic-gate after.
    assert positions["--inject"] < positions["--iterative"]
    assert positions["--iterative"] < positions["--iterate-from-baseline"]
    assert (
        positions["--iterate-from-baseline"] < positions["--skip-diagnosis"]
    )
    assert (
        positions["--skip-diagnosis"] < positions["--skip-kinematic-gate"]
    )
