"""GUI cross-thread safety regression tests.

A worker thread calling ``root.after()`` directly raises
``RuntimeError: main thread is not in main loop`` and silently aborts, so
the tree never populates ("picked a folder, no files appeared"). These
tests drive the real threaded discovery/scoring paths through the
queue-pump and assert the UI actually updates.

Skipped automatically when no display / Tk is available (headless CI).
"""

from __future__ import annotations

import time

import pytest

tk = pytest.importorskip("tkinter")


@pytest.fixture
def root():
    try:
        r = tk.Tk()
    except tk.TclError:
        pytest.skip("no display / Tk unavailable")
    r.withdraw()
    yield r
    try:
        r.destroy()
    except tk.TclError:
        pass


def _pump_until(root, predicate, timeout=8.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        root.update()
        if predicate():
            return True
        time.sleep(0.02)
    return False


def test_threaded_discovery_populates_tree(root, tmp_path):
    """The reported bug: pick folder -> worker thread -> tree must fill."""
    from src.gui import ResembleScoreGUI

    (tmp_path / "front_kling.mp4").write_bytes(b"x")
    (tmp_path / "front-oldcam-v9.mp4").write_bytes(b"x")
    (tmp_path / "front-oldcam-v14.mp4").write_bytes(b"x")

    g = ResembleScoreGUI(root)
    g.folder = tmp_path
    g.recursive.set(True)
    g._start_discovery()  # exactly what _pick_folder does

    ok = _pump_until(root, lambda: len(g._rows) > 0)
    assert ok, "tree never populated — worker→UI hand-off broke"
    assert len(g._rows) == 3
    assert "3 video(s) found" in g.status_lbl.cget("text")
    # Grouped: Original + Oldcam v9 + Oldcam v14 = 3 top-level group rows.
    assert len(g.tree.get_children()) == 3


def test_recursive_toggle_rescans(root, tmp_path):
    from src.gui import ResembleScoreGUI

    (tmp_path / "top_kling.mp4").write_bytes(b"x")
    sub = tmp_path / "sub"
    sub.mkdir()
    (sub / "deep-oldcam-v9.mp4").write_bytes(b"x")

    g = ResembleScoreGUI(root)
    g.folder = tmp_path

    g.recursive.set(False)
    g._on_recursive_toggle()
    assert _pump_until(root, lambda: g.status_lbl.cget("text").startswith("1"))
    assert len(g._rows) == 1  # only top-level

    g.recursive.set(True)
    g._on_recursive_toggle()
    assert _pump_until(root, lambda: len(g._rows) == 2)
    assert len(g._rows) == 2  # now includes nested


def test_discovery_error_surfaces_without_crashing(root, tmp_path, monkeypatch):
    from src.gui import ResembleScoreGUI

    g = ResembleScoreGUI(root)
    g.folder = tmp_path / "does-not-exist"

    shown = {}
    monkeypatch.setattr(
        "tkinter.messagebox.showerror",
        lambda title, msg: shown.update(title=title, msg=msg),
    )
    g._start_discovery()
    assert _pump_until(root, lambda: "title" in shown)
    assert "Discovery failed" in shown["title"]
    # Controls must be re-enabled after the error (not stuck "Scanning…").
    # ttk cget("state") returns a Tcl object — coerce to str to compare.
    assert str(g.pick_btn.cget("state")) == "normal"


def test_post_runs_on_main_thread_via_queue(root):
    """_post() must not touch Tk from the calling thread; the pump runs it."""
    import threading

    from src.gui import ResembleScoreGUI

    g = ResembleScoreGUI(root)
    seen = {}

    def worker():
        # Simulate a worker thread marshalling a UI update.
        g._post(lambda: seen.update(done=True))

    t = threading.Thread(target=worker)
    t.start()
    t.join()
    assert _pump_until(root, lambda: seen.get("done") is True)
