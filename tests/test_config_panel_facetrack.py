"""ConfigPanel face-track gate control — construction + config round-trip.

The face-track gate is wired into the CLI automation pipeline; this
covers its Tkinter surface in the video tab's ConfigPanel: the
"Gate enabled" / "Block oldcam if below threshold" checkboxes + the
min-% entry must construct, persist to the automation_* config keys
the pipeline reads, and reload from them.

Skips cleanly when no display / Tk is unavailable (CI headless boxes).
"""
import pytest

tk = pytest.importorskip("tkinter")


@pytest.fixture()
def _root():
    try:
        r = tk.Tk()
    except Exception as exc:  # pragma: no cover - headless CI
        pytest.skip(f"Tk unavailable: {exc}")
    r.withdraw()
    yield r
    try:
        r.destroy()
    except Exception:  # pragma: no cover - teardown best effort
        pass


def _panel(root, config):
    from kling_gui.config_panel import ConfigPanel

    return ConfigPanel(
        root,
        config,
        on_config_changed=lambda *_a, **_k: None,
        build_prompt=lambda *_a, **_k: "",
    )


def test_facetrack_widgets_construct_with_defaults(_root):
    panel = _panel(_root, {})
    assert panel.facetrack_enabled_var.get() is True
    assert panel.facetrack_required_var.get() is False
    assert panel.facetrack_min_var.get() in ("96", "96.0")
    # Indicator reflects advisory (enabled, not blocking).
    assert "advisory" in panel.facetrack_status_label.cget("text")


def test_facetrack_loads_existing_config(_root):
    panel = _panel(_root, {
        "automation_facetrack_enabled": True,
        "automation_facetrack_required": True,
        "automation_facetrack_min_pct": 92.5,
    })
    assert panel.facetrack_required_var.get() is True
    assert panel.facetrack_min_var.get() == "92.5"
    assert "blocking" in panel.facetrack_status_label.cget("text")


def test_facetrack_toggle_persists_to_automation_keys(_root):
    panel = _panel(_root, {})
    panel.facetrack_required_var.set(True)
    panel.facetrack_min_var.set("88")
    panel._on_facetrack_changed()
    assert panel.config["automation_facetrack_enabled"] is True
    assert panel.config["automation_facetrack_required"] is True
    assert panel.config["automation_facetrack_min_pct"] == 88.0
    assert "blocking" in panel.facetrack_status_label.cget("text")


def test_facetrack_disabled_indicator(_root):
    panel = _panel(_root, {})
    panel.facetrack_enabled_var.set(False)
    panel._on_facetrack_changed()
    assert panel.config["automation_facetrack_enabled"] is False
    assert "off" in panel.facetrack_status_label.cget("text")


def test_facetrack_invalid_threshold_snaps_back(_root):
    panel = _panel(_root, {"automation_facetrack_min_pct": 96.0})
    panel.facetrack_min_var.set("not-a-number")
    panel._on_facetrack_changed()
    # Bad input must snap back to the stored value, never crash.
    assert panel.config["automation_facetrack_min_pct"] == 96.0
    assert panel.facetrack_min_var.get() == "96"

    panel.facetrack_min_var.set("150")  # out of [0,100]
    panel._on_facetrack_changed()
    assert panel.config["automation_facetrack_min_pct"] == 96.0
