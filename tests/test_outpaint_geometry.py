from outpaint_geometry import (
    BFL_CAPS,
    FAL_CAPS,
    compute_centered_aspect_expand_plan,
    compute_percent_expand_plan,
)
import pytest


def test_percent_expand_30():
    plan = compute_percent_expand_plan(1000, 1200, 30, FAL_CAPS)
    assert plan["left"] > 0
    assert plan["right"] == plan["left"]
    assert plan["canvas_w"] == plan["upload_w"] + plan["left"] + plan["right"]


def test_percent_expand_70_downscales_for_bfl():
    plan = compute_percent_expand_plan(2000, 2000, 70, BFL_CAPS)
    assert plan["upload_w"] < 2000
    assert plan["upload_h"] < 2000
    assert plan["canvas_w"] <= BFL_CAPS.max_canvas_dim
    assert plan["canvas_h"] <= BFL_CAPS.max_canvas_dim


def test_centered_document_3x4():
    plan = compute_centered_aspect_expand_plan(1600, 1000, (3, 4), BFL_CAPS)
    assert plan["canvas_h"] > plan["canvas_w"]
    assert plan["left"] >= 0
    assert plan["top"] >= 0
    assert plan["canvas_w"] <= BFL_CAPS.max_canvas_dim
    assert plan["canvas_h"] <= BFL_CAPS.max_canvas_dim


def test_fal_per_side_cap_applied():
    plan = compute_percent_expand_plan(5000, 4000, 80, FAL_CAPS)
    assert plan["left"] <= FAL_CAPS.max_per_side
    assert plan["top"] <= FAL_CAPS.max_per_side


def test_percent_expand_rejects_non_positive_dimensions():
    with pytest.raises(ValueError):
        compute_percent_expand_plan(0, 100, 30, FAL_CAPS)


def test_centered_plan_rejects_non_positive_dimensions():
    with pytest.raises(ValueError):
        compute_centered_aspect_expand_plan(100, -1, (3, 4), BFL_CAPS)
