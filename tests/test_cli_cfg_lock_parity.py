"""Regression: CLI batch path must pass cfg_scale + lock_end_frame.

Code-reviewer finding, PR #41: the interactive CLI's
process_all_images_concurrent calls dropped cfg_scale and
lock_end_frame entirely, so the CLI silently used the generator
defaults (cfg_scale=None, lock_end_frame=False) while the GUI and
automation/pipeline.py honored the persisted config — a GUI/CLI drift.

KlingAutomationUI._resolve_cfg_and_lock() is the single resolver for
that path. It must mirror automation/pipeline.py exactly:
  * cfg_scale clamped to [0.0, 1.0] (stale/hand-edited out-of-range
    persisted value must not reach the API);
  * lock_end_frame via the canonical _parse_bool with an UNPARSEABLE
    value coercing to True (lock default is True — GUI, pipeline and
    CLI must agree on malformed input; contrast rppg flags where
    bool(None)=False is the deliberate default).
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from kling_automation_ui import KlingAutomationUI  # noqa: E402


def _ui(config):
    ui = KlingAutomationUI.__new__(KlingAutomationUI)
    ui.config = config
    return ui


class TestResolveCfgAndLock:
    def test_defaults_when_keys_absent(self):
        cfg, lock = _ui({})._resolve_cfg_and_lock()
        assert cfg == pytest.approx(0.7)
        assert lock is True  # lock_end_frame default is True

    def test_cfg_scale_clamped_high(self):
        cfg, _ = _ui({"cfg_scale_value": 5.0})._resolve_cfg_and_lock()
        assert cfg == pytest.approx(1.0)

    def test_cfg_scale_clamped_low(self):
        cfg, _ = _ui({"cfg_scale_value": -3})._resolve_cfg_and_lock()
        assert cfg == pytest.approx(0.0)

    def test_cfg_scale_in_range_preserved(self):
        cfg, _ = _ui({"cfg_scale_value": 0.35})._resolve_cfg_and_lock()
        assert cfg == pytest.approx(0.35)

    def test_unparseable_cfg_scale_falls_back_to_default(self):
        cfg, _ = _ui({"cfg_scale_value": "junk"})._resolve_cfg_and_lock()
        assert cfg == pytest.approx(0.7)

    def test_lock_false_when_explicitly_disabled(self):
        _, lock = _ui({"lock_end_frame": False})._resolve_cfg_and_lock()
        assert lock is False

    def test_lock_true_when_explicitly_enabled(self):
        _, lock = _ui({"lock_end_frame": True})._resolve_cfg_and_lock()
        assert lock is True

    def test_unparseable_lock_coerces_to_true(self):
        """_parse_bool returns None for junk; default is True. CLI must
        match GUI/pipeline (NOT bool(None)=False)."""
        _, lock = _ui({"lock_end_frame": "maybe"})._resolve_cfg_and_lock()
        assert lock is True

    def test_string_bools_parsed(self):
        _, lock = _ui({"lock_end_frame": "false"})._resolve_cfg_and_lock()
        assert lock is False
        _, lock2 = _ui({"lock_end_frame": "true"})._resolve_cfg_and_lock()
        assert lock2 is True

    def test_return_shape(self):
        result = _ui({})._resolve_cfg_and_lock()
        assert isinstance(result, tuple) and len(result) == 2
        assert isinstance(result[0], float)
        assert isinstance(result[1], bool)


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
