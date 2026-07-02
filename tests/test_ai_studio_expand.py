"""Regression tests for AI Studio expand wiring."""

import tkinter as tk

from PIL import Image


class _FakeButton:
    def __init__(self):
        self.state = None

    def config(self, **kwargs):
        if "state" in kwargs:
            self.state = kwargs["state"]


class _FakeCaption:
    def __init__(self):
        self.text = None

    def config(self, **kwargs):
        if "text" in kwargs:
            self.text = kwargs["text"]


class _FakeImageSession:
    def __init__(self, active_image_path=""):
        self.active_image_path = active_image_path
        self.added = []

    def add_image(self, path, source_type, label="", make_active=True, similarity=None, **kwargs):
        self.added.append(
            {
                "path": path,
                "source_type": source_type,
                "label": label,
                "make_active": make_active,
                "similarity": similarity,
            }
        )


def _build_done_tab(source_path):
    from kling_gui.tabs.ai_studio_tab import AIStudioTab

    tab = AIStudioTab.__new__(AIStudioTab)
    tab.image_session = _FakeImageSession(str(source_path))
    tab._abort_event = object()
    tab._after_path = None
    tab._after_pil = None
    tab._last_similarity = None
    tab._last_synced_carousel_path = ""
    tab._auto_added_carousel_path = None
    tab._add_btn = _FakeButton()
    tab._use_btn = _FakeButton()
    tab._zoom_btn = _FakeButton()
    tab._after_caption = _FakeCaption()
    tab._set_busy = lambda busy: None
    tab._load_pil = lambda path: path
    tab._rerender_after = lambda: None
    tab._extract_similarity_from_result_path = lambda path: "98%"
    tab.log_messages = []
    tab.log = lambda message, level="info": tab.log_messages.append((message, level))
    return tab


def test_ai_studio_run_done_auto_adds_result_once(tmp_path):
    source = tmp_path / "front_crop.jpg"
    result = tmp_path / "front_crop_nano-banana-2-edit_sim98_001.png"
    Image.new("RGB", (60, 40), "white").save(source)
    Image.new("RGB", (60, 40), "blue").save(result)

    tab = _build_done_tab(source)

    tab._on_run_done(str(result))
    tab._on_add_to_carousel()

    assert len(tab.image_session.added) == 1
    added = tab.image_session.added[0]
    assert added["path"] == str(result)
    assert added["source_type"] == "edit"
    assert added["label"] == result.name
    assert added["make_active"] is True
    assert added["similarity"] == "98%"
    assert tab._last_synced_carousel_path == str(result)
    assert tab._auto_added_carousel_path == str(result)
    assert tab._add_btn.state == tk.DISABLED
    assert any("Added to carousel" in msg for msg, _level in tab.log_messages)
    assert any("Already added" in msg for msg, _level in tab.log_messages)


def test_ai_studio_auto_add_failure_keeps_manual_add_enabled(tmp_path):
    source = tmp_path / "front_crop.jpg"
    result = tmp_path / "front_crop_edit.png"
    Image.new("RGB", (60, 40), "white").save(source)
    Image.new("RGB", (60, 40), "blue").save(result)

    tab = _build_done_tab(source)

    def fail_add(*args, **kwargs):
        raise RuntimeError("session add failed")

    tab.image_session.add_image = fail_add

    tab._on_run_done(str(result))

    assert tab._after_path == str(result)
    assert tab._auto_added_carousel_path is None
    assert tab._add_btn.state == tk.NORMAL
    assert any("Auto-add to carousel failed" in msg for msg, _level in tab.log_messages)


def test_ai_studio_manual_add_failure_logs_without_raising(tmp_path):
    source = tmp_path / "front_crop.jpg"
    result = tmp_path / "front_crop_edit.png"
    Image.new("RGB", (60, 40), "white").save(source)
    Image.new("RGB", (60, 40), "blue").save(result)

    tab = _build_done_tab(source)
    tab._after_path = str(result)

    def fail_add(*args, **kwargs):
        raise RuntimeError("session add failed")

    tab.image_session.add_image = fail_add

    tab._on_add_to_carousel()

    assert tab._auto_added_carousel_path is None
    assert tab._add_btn.state == tk.NORMAL
    assert any("Add to carousel failed" in msg for msg, _level in tab.log_messages)


def test_ai_studio_add_missing_result_logs_warning(tmp_path):
    source = tmp_path / "front_crop.jpg"
    missing = tmp_path / "missing_edit.png"
    Image.new("RGB", (60, 40), "white").save(source)

    tab = _build_done_tab(source)
    tab._after_path = str(missing)

    tab._on_add_to_carousel()

    assert tab.image_session.added == []
    assert any("result file is missing" in msg for msg, _level in tab.log_messages)


def test_ai_studio_expand_to_3x4_runs_outpaint_with_cancel_event(monkeypatch, tmp_path):
    from kling_gui.tabs import ai_studio_tab
    from kling_gui.tabs.ai_studio_tab import AIStudioTab
    import outpaint_generator

    source = tmp_path / "front_crop.jpg"
    Image.new("RGB", (60, 40), "white").save(source)
    output = tmp_path / "front_crop-expanded.png"
    calls = {}

    class FakeTop:
        def winfo_exists(self):
            return True

        def after(self, _delay, func):
            func()

    class SyncThread:
        def __init__(self, target, daemon=False):
            self.target = target
            self.daemon = daemon

        def start(self):
            self.target()

    class FakeOutpaintGenerator:
        def __init__(self, api_key, freeimage_key=None, bfl_api_key=None):
            calls["init"] = (api_key, freeimage_key, bfl_api_key)

        def set_progress_callback(self, callback):
            calls["progress_callback"] = callback

        def set_cancel_event(self, event):
            calls["cancel_event"] = event

        def outpaint(self, **kwargs):
            calls["outpaint"] = kwargs
            return str(output)

    monkeypatch.setattr(ai_studio_tab.threading, "Thread", SyncThread)
    monkeypatch.setattr(outpaint_generator, "OutpaintGenerator", FakeOutpaintGenerator)

    tab = AIStudioTab.__new__(AIStudioTab)
    tab._busy = False
    tab._before_path = str(source)
    tab.image_session = _FakeImageSession(str(source))
    tab.config = {"outpaint_prompt": "keep scene"}
    tab.get_config = lambda: {
        "falai_api_key": "fal-key",
        "freeimage_api_key": "free-key",
        "bfl_api_key": "",
    }
    tab.log = lambda *args, **kwargs: None
    tab.winfo_toplevel = lambda: FakeTop()
    tab._set_busy = lambda busy: calls.setdefault("busy", []).append(busy)
    tab._on_run_done = lambda result: calls.setdefault("done", result)
    tab._on_run_error = lambda err: calls.setdefault("error", err)

    tab._on_expand_3x4()

    assert "error" not in calls
    assert calls["done"] == str(output)
    assert calls["cancel_event"] is tab._abort_event
    assert calls["outpaint"]["image_path"] == str(source)
    assert calls["outpaint"]["composite_mode"] == "preserve_seamless"
    assert calls["outpaint"]["full_res_plan"]["full_canvas_w"] > 0
    assert calls["outpaint"]["border_strategy"] == "bria"

    # No saved percentage now means the widened 35% default is used.
    plan = calls["outpaint"]["full_res_plan"]
    assert plan["full_canvas_w"] == 102
    assert plan["full_canvas_h"] == 136
