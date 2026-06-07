"""Regression tests for the video-poll progress cap math (v2.29 fix).

The Kling video poll loop in ``kling_generator_falai`` uses tiered backoff:
5s for the first 24 attempts, 10s for the next 36, 15s thereafter. The old
v2.28 progress row computed ``elapsed_s = attempt * base_delay`` and
``cap_s = max_attempts * base_delay`` — a FLAT 5s/attempt assumption. That
under-counted elapsed time and capped at a false 1200s (20 min) when the real
budget at ``max_attempts=240`` is ~3180s (~53 min), so the Step 3 row rendered
``(1205s / 1200s)`` (ratio > 1, reads as "the job is stuck/overdue") for any
job that polled past ~20 min.

``_cumulative_poll_seconds`` is the backoff-aware single source of truth these
tests pin. The key invariant: for every attempt in [0, max_attempts], the
displayed elapsed never exceeds the displayed cap (ratio <= 1).
"""

from __future__ import annotations

from kling_generator_falai import (
    _cumulative_poll_seconds,
    _POLL_TIER_FAST_LIMIT,
    _POLL_TIER_MID_LIMIT,
)

# Mirrors ``max_attempts`` in FalAIKlingGenerator's poll loop.
MAX_ATTEMPTS = 240


def test_zero_and_negative_attempts_are_zero():
    assert _cumulative_poll_seconds(0) == 0
    assert _cumulative_poll_seconds(-5) == 0


def test_fast_tier_is_five_seconds_each():
    # First 24 attempts: 5s each.
    assert _cumulative_poll_seconds(1) == 5
    assert _cumulative_poll_seconds(_POLL_TIER_FAST_LIMIT) == 24 * 5  # 120


def test_mid_tier_is_ten_seconds_each():
    # Attempts 24..59 add 10s each on top of the 120s fast tier.
    assert _cumulative_poll_seconds(_POLL_TIER_MID_LIMIT) == 24 * 5 + 36 * 10  # 480
    assert _cumulative_poll_seconds(30) == 120 + 6 * 10  # 180


def test_slow_tier_is_fifteen_seconds_each():
    # Attempts 60+ add 15s each.
    assert _cumulative_poll_seconds(61) == 480 + 15
    # The full budget that motivated the fix: ~53 min, NOT the old false 20 min.
    assert _cumulative_poll_seconds(MAX_ATTEMPTS) == 480 + 180 * 15  # 3180


def test_full_budget_is_about_53_minutes_not_20():
    cap = _cumulative_poll_seconds(MAX_ATTEMPTS)
    assert cap == 3180
    # The old flat math produced 240 * 5 = 1200s (20 min) — guard against a
    # regression back to it.
    assert cap != MAX_ATTEMPTS * 5
    assert cap // 60 == 53


def test_monotonic_non_decreasing():
    prev = -1
    for attempt in range(MAX_ATTEMPTS + 1):
        cur = _cumulative_poll_seconds(attempt)
        assert cur >= prev, f"non-monotonic at attempt {attempt}"
        prev = cur


def test_elapsed_never_exceeds_cap_the_ratio_invariant():
    # The bug was a progress row that showed (elapsed / cap) with elapsed > cap.
    # With the backoff-aware helper, elapsed at any attempt <= the cap computed
    # from max_attempts, so the displayed ratio is always <= 1.
    cap = _cumulative_poll_seconds(MAX_ATTEMPTS)
    for attempt in range(MAX_ATTEMPTS + 1):
        assert _cumulative_poll_seconds(attempt) <= cap


def test_tier_boundaries_are_continuous():
    # No jump/gap at the tier transitions: the value just before a boundary
    # plus the next tier's per-attempt cost equals the value just after.
    fast_end = _cumulative_poll_seconds(_POLL_TIER_FAST_LIMIT)
    assert _cumulative_poll_seconds(_POLL_TIER_FAST_LIMIT + 1) == fast_end + 10
    mid_end = _cumulative_poll_seconds(_POLL_TIER_MID_LIMIT)
    assert _cumulative_poll_seconds(_POLL_TIER_MID_LIMIT + 1) == mid_end + 15


if __name__ == "__main__":
    import pytest

    pytest.main([__file__, "-v"])
