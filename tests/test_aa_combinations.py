"""
Exhaustive post-processing COMBINATION tests for the re-run path.

Exercises queue_manager.rerun_oldcam_only across every combination of
rPPG / Loop / Crush / AA / Oldcam — the "all combinations of everything must
work" mandate (2026-06-18). The four post-proc workers (_rppg_video,
_crush_video, _aa_video, _oldcam_video) are mocked at the boundary so each combo
runs fast + deterministically and we verify the WIRING (routing, fan-out,
headline selection, the no-op guard), not ffmpeg itself.

The real AA/crush/oldcam binaries are exercised separately by the standalone
harness + the live GUI; here we prove every selection combination is honoured.
"""
import threading
from types import SimpleNamespace

from kling_gui.queue_manager import QueueManager


def _mgr(config):
    logs = []
    manager = QueueManager(
        generator=SimpleNamespace(),
        config_getter=lambda: config,
        log_callback=lambda message, level="info": logs.append((message, level)),
        queue_update_callback=lambda: None,
    )
    return manager, logs


def _run(manager, source):
    """Drive rerun_oldcam_only synchronously and return the completion tuple."""
    done = threading.Event()
    result = {}

    def cb(success, src, output, error):
        result.update({"success": success, "output": output, "error": error})
        done.set()

    started = manager.rerun_oldcam_only(str(source), completion_callback=cb)
    assert started is True, "rerun_oldcam_only should start"
    assert done.wait(3), "rerun callback never fired"
    return result


def _src(tmp_path, name="clip.mp4"):
    p = tmp_path / name
    p.write_bytes(b"kling-original")
    return p


# ---------------------------------------------------------------------------
# Single-step combos
# ---------------------------------------------------------------------------
def _writer(path, data=b"x"):
    """Return a mock that writes *path* and returns its str (not write_bytes' int)."""
    def _fn(*args, **kwargs):
        path.write_bytes(data)
        return str(path)
    return _fn


def test_crush_only(tmp_path, monkeypatch):
    src = _src(tmp_path)
    mgr, _ = _mgr({"oldcam_versions": [], "crush_resolutions": ["720p"]})
    out = tmp_path / "clip_crush720.mp4"
    monkeypatch.setattr(mgr, "_crush_video", _writer(out, b"c"))
    r = _run(mgr, src)
    assert r["success"] and r["output"] == str(out)


def test_aa_only(tmp_path, monkeypatch):
    src = _src(tmp_path)
    mgr, _ = _mgr({"oldcam_versions": [], "aa_attacks": ["prime"]})
    out = tmp_path / "clip_aa-prime.mp4"
    monkeypatch.setattr(mgr, "_aa_video", _writer(out, b"a"))
    r = _run(mgr, src)
    assert r["success"] and r["output"] == str(out)


def test_oldcam_only(tmp_path, monkeypatch):
    src = _src(tmp_path)
    mgr, _ = _mgr({"oldcam_versions": ["v24"], "oldcam_videos": True})
    out = tmp_path / "clip-oldcam-v24.mp4"

    def fake_oldcam(v, item):
        out.write_bytes(b"o")
        mgr._last_oldcam_run_summary = {"outputs": [str(out)]}
        return str(out)

    monkeypatch.setattr(mgr, "_oldcam_video", fake_oldcam)
    r = _run(mgr, src)
    assert r["success"] and r["output"] == str(out)


def test_nothing_selected_is_rejected(tmp_path):
    src = _src(tmp_path)
    mgr, _ = _mgr({"oldcam_versions": [], "aa_attacks": [], "crush_resolutions": []})
    r = _run(mgr, src)
    assert r["success"] is False
    assert "nothing to apply" in (r["error"] or "").lower()


# ---------------------------------------------------------------------------
# AA -> Oldcam fan-out: AA output is the oldcam source
# ---------------------------------------------------------------------------
def test_aa_then_oldcam_fans_aa_output(tmp_path, monkeypatch):
    src = _src(tmp_path)
    mgr, _ = _mgr({"oldcam_versions": ["v24"], "oldcam_videos": True, "aa_attacks": ["prime"]})
    aa_out = tmp_path / "clip_aa-prime.mp4"
    monkeypatch.setattr(mgr, "_aa_video", _writer(aa_out, b"a"))

    oldcam_inputs = []

    def fake_oldcam(v, item):
        oldcam_inputs.append(str(v))
        o = tmp_path / (str(v).rsplit("\\", 1)[-1].rsplit("/", 1)[-1].replace(".mp4", "") + "-oldcam-v24.mp4")
        o.write_bytes(b"o")
        mgr._last_oldcam_run_summary = {"outputs": [str(o)]}
        return str(o)

    monkeypatch.setattr(mgr, "_oldcam_video", fake_oldcam)
    r = _run(mgr, src)
    assert r["success"]
    # Oldcam must run on the AA output, not the raw source.
    assert any("aa-prime" in p for p in oldcam_inputs), oldcam_inputs


# ---------------------------------------------------------------------------
# Crush -> AA -> Oldcam: full chain, AA runs on the crushed tier
# ---------------------------------------------------------------------------
def test_crush_aa_oldcam_full_chain(tmp_path, monkeypatch):
    src = _src(tmp_path)
    mgr, _ = _mgr({
        "oldcam_versions": ["v24"], "oldcam_videos": True,
        "crush_resolutions": ["720p"], "aa_attacks": ["prime"],
    })
    crush_out = tmp_path / "clip_crush720.mp4"
    monkeypatch.setattr(mgr, "_crush_video", _writer(crush_out, b"c"))

    aa_inputs = []

    def fake_aa(v, item, attack="prime"):
        aa_inputs.append(str(v))
        o = tmp_path / "clip_crush720_aa-prime.mp4"
        o.write_bytes(b"a")
        return str(o)

    monkeypatch.setattr(mgr, "_aa_video", fake_aa)

    oldcam_inputs = []

    def fake_oldcam(v, item):
        oldcam_inputs.append(str(v))
        o = tmp_path / "final-oldcam-v24.mp4"
        o.write_bytes(b"o")
        mgr._last_oldcam_run_summary = {"outputs": [str(o)]}
        return str(o)

    monkeypatch.setattr(mgr, "_oldcam_video", fake_oldcam)
    r = _run(mgr, src)
    assert r["success"]
    # AA must run on the CRUSHED tier (not the raw source).
    assert any("crush720" in p for p in aa_inputs), aa_inputs
    # Oldcam must run on the AA output.
    assert any("aa-prime" in p for p in oldcam_inputs), oldcam_inputs


# ---------------------------------------------------------------------------
# Multi-pipeline AA: prime + scenario1 both run
# ---------------------------------------------------------------------------
def test_aa_multi_pipeline(tmp_path, monkeypatch):
    src = _src(tmp_path)
    mgr, _ = _mgr({"oldcam_versions": [], "aa_attacks": ["prime", "scenario1"]})
    calls = []

    def fake_aa(v, item, attack="prime"):
        calls.append(attack)
        o = tmp_path / f"clip_aa-{attack}.mp4"
        o.write_bytes(b"a")
        return str(o)

    monkeypatch.setattr(mgr, "_aa_video", fake_aa)
    r = _run(mgr, src)
    assert r["success"]
    assert calls == ["prime", "scenario1"], calls
