"""Regression tests for AI Studio expand wiring."""

from PIL import Image


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

    class FakeSession:
        active_image_path = str(source)

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
    tab.image_session = FakeSession()
    tab.config = {"outpaint_prompt": "keep scene"}
    tab.get_config = lambda: {
        "falai_api_key": "fal-key",
        "outpaint_expand_percentage": 30,
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
