"""Tests for the multi-resolution quality-crush feature (2026-06-18).

Covers the single-source-of-truth resolver, the config-migration of the
legacy ``crush_enabled`` boolean, and a real two-tier ffmpeg crush.
"""

import shutil
import subprocess

import pytest

from automation.video_crush import (
    CRUSH_RESOLUTIONS,
    DEFAULT_CRUSH_RESOLUTIONS,
    crush_suffix,
    crush_video,
    normalize_crush_resolutions,
)
from automation.config import merge_automation_defaults


# --------------------------------------------------------------------------
# Resolver
# --------------------------------------------------------------------------
def test_resolver_fresh_default_is_720p():
    assert normalize_crush_resolutions() == ["720p"]
    assert DEFAULT_CRUSH_RESOLUTIONS == ["720p"]


def test_resolver_legacy_true_migrates_to_480p():
    # Legacy crush was a 480p single tier — preserve that exactly.
    assert normalize_crush_resolutions(legacy_enabled=True) == ["480p"]


def test_resolver_legacy_false_is_off():
    assert normalize_crush_resolutions(legacy_enabled=False) == []


def test_resolver_explicit_list_wins_over_legacy():
    # An explicit list is authoritative even when the legacy bool disagrees.
    assert normalize_crush_resolutions(["720p"], legacy_enabled=True) == ["720p"]
    assert normalize_crush_resolutions([], legacy_enabled=True) == []


def test_resolver_orders_highest_first_and_dedups():
    assert normalize_crush_resolutions(["480p", "720p", "480p"]) == ["720p", "480p"]


def test_resolver_accepts_loose_tokens():
    assert normalize_crush_resolutions(["720", 480, "bad", None]) == ["720p", "480p"]


def test_resolver_single_string():
    assert normalize_crush_resolutions("480p") == ["480p"]


def test_crush_suffix_per_tier():
    assert crush_suffix("720p") == "_crush720"
    assert crush_suffix("480p") == "_crush480"
    assert CRUSH_RESOLUTIONS == {"720p": 720, "480p": 480}


# --------------------------------------------------------------------------
# Config migration (merge_automation_defaults)
# --------------------------------------------------------------------------
def test_merge_fresh_config_defaults_720p_on():
    m = merge_automation_defaults({})
    assert m["automation_crush_resolutions"] == ["720p"]
    assert m["automation_crush_enabled"] is True


def test_merge_legacy_enabled_true_preserves_480p():
    m = merge_automation_defaults({"automation_crush_enabled": True})
    assert m["automation_crush_resolutions"] == ["480p"]
    assert m["automation_crush_enabled"] is True


def test_merge_legacy_disabled_stays_off():
    m = merge_automation_defaults({"automation_crush_enabled": False})
    assert m["automation_crush_resolutions"] == []
    assert m["automation_crush_enabled"] is False


def test_merge_explicit_list_round_trips():
    m = merge_automation_defaults({"automation_crush_resolutions": ["720p", "480p"]})
    assert m["automation_crush_resolutions"] == ["720p", "480p"]
    assert m["automation_crush_enabled"] is True


# --------------------------------------------------------------------------
# Real two-tier ffmpeg crush (end-to-end, skipped when ffmpeg absent)
# --------------------------------------------------------------------------
@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not installed")
def test_crush_video_two_tiers_distinct_files(tmp_path):
    src = tmp_path / "clip.mp4"
    subprocess.run(
        [
            "ffmpeg", "-y", "-f", "lavfi",
            "-i", "testsrc=duration=1:size=640x480:rate=10",
            "-pix_fmt", "yuv420p", str(src),
        ],
        check=True,
        capture_output=True,
    )
    out720 = crush_video(str(src), suffix=crush_suffix("720p"), target_height=720)
    out480 = crush_video(str(src), suffix=crush_suffix("480p"), target_height=480)
    assert out720 and out720.endswith("clip_crush720.mp4")
    assert out480 and out480.endswith("clip_crush480.mp4")
    assert out720 != out480
    from pathlib import Path
    assert Path(out720).exists() and Path(out480).exists()
