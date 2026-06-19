"""Unit tests for the shared post-processing fan-out planner.

Pure, fast, no I/O — exercises enumeration counts, the canonical-order
invariant, sub-option multiply-in, and the preview-line rendering.
"""

import itertools

import pytest

from automation.postproc_plan import (
    CANONICAL_ORDER,
    DEFAULT_MODE,
    Modifier,
    Recipe,
    Step,
    build_plan,
    full_chain_recipe,
    normalize_mode,
    plan_preview_line,
    recipe_expected_suffixes,
)

_ORDER_INDEX = {m: i for i, m in enumerate(CANONICAL_ORDER)}


def _is_canonical(recipe: Recipe) -> bool:
    """Every recipe's modifiers must be a strictly-increasing subsequence of
    CANONICAL_ORDER (never reordered, never duplicated)."""
    idxs = [_ORDER_INDEX[s.modifier] for s in recipe.steps]
    return idxs == sorted(idxs) and len(set(idxs)) == len(idxs)


# Single-option enabled sets for the N=0..4 count tests.
def _single_option_kwargs(n: int) -> dict:
    """Enable the first ``n`` modifier types, each with exactly one sub-option."""
    kwargs = dict(
        rppg_enabled=False,
        loop_enabled=False,
        crush_resolutions=[],
        aa_attacks=[],
        oldcam_versions=[],
    )
    flags = [
        ("rppg_enabled", True),
        ("loop_enabled", True),
        ("crush_resolutions", ["720p"]),
        ("aa_attacks", ["prime"]),
        ("oldcam_versions", ["v13"]),
    ]
    for key, val in flags[:n]:
        kwargs[key] = val
    return kwargs


@pytest.mark.parametrize("n", [0, 1, 2, 3, 4])
def test_powerset_counts(n):
    plan = build_plan(mode="separate_and_combined", **_single_option_kwargs(n))
    expected = (2 ** n) - 1
    assert len(plan.recipes) == expected
    assert len(plan.enabled_modifiers) == n


@pytest.mark.parametrize("n", [0, 1, 2, 3, 4])
def test_combined_only_counts(n):
    plan = build_plan(mode="combined_only", **_single_option_kwargs(n))
    assert len(plan.recipes) == (1 if n >= 1 else 0)


def test_suboption_multiply_in():
    # crush[720,480] + oldcam[v13,v24].
    common = dict(crush_resolutions=["720p", "480p"], oldcam_versions=["v13", "v24"])
    combined = build_plan(mode="combined_only", **common)
    # Only the {crush, oldcam} family: 2 * 2 = 4.
    assert len(combined.recipes) == 4

    powerset = build_plan(mode="separate_and_combined", **common)
    # {crush}=2 + {oldcam}=2 + {crush,oldcam}=4 = 8.
    assert len(powerset.recipes) == 8


def test_canonical_order_invariant_all_modes():
    kwargs = dict(
        rppg_enabled=True,
        loop_enabled=True,
        crush_resolutions=["720p", "480p"],
        aa_attacks=["prime", "scenario1"],
        oldcam_versions=["v13", "v24"],
    )
    for mode in ("combined_only", "separate_and_combined"):
        plan = build_plan(mode=mode, **kwargs)
        assert plan.recipes, "expected at least one recipe"
        for recipe in plan.recipes:
            assert _is_canonical(recipe), f"{mode}: {recipe.modifiers()} not canonical"
            # rPPG, when present, is always first.
            mods = recipe.modifiers()
            if Modifier.RPPG in mods:
                assert mods[0] is Modifier.RPPG
            # Oldcam, when present, is always last.
            if Modifier.OLDCAM in mods:
                assert mods[-1] is Modifier.OLDCAM


def test_loop_and_rppg_are_singletons():
    plan = build_plan(
        mode="separate_and_combined", rppg_enabled=True, loop_enabled=True
    )
    for recipe in plan.recipes:
        for step in recipe.steps:
            if step.modifier in (Modifier.RPPG, Modifier.LOOP):
                assert step.option is None


def test_no_duplicate_recipes():
    plan = build_plan(
        mode="separate_and_combined",
        rppg_enabled=True,
        crush_resolutions=["720p", "480p"],
        aa_attacks=["prime", "scenario1", "scenario3"],
        oldcam_versions=["v13", "v24"],
    )
    keys = [r.prefix_key() for r in plan.recipes]
    assert len(keys) == len(set(keys))


def test_full_chain_recipe_uses_every_enabled_type():
    plan = build_plan(
        mode="separate_and_combined",
        rppg_enabled=True,
        aa_attacks=["prime"],
        oldcam_versions=["v13"],
    )
    headline = full_chain_recipe(plan)
    assert headline is not None
    assert set(headline.modifiers()) == set(plan.enabled_modifiers)


def test_preview_line_empty():
    plan = build_plan(mode="separate_and_combined")
    assert plan_preview_line(plan) == "Kling (no post-processing)"


def test_preview_line_counts_extra_variants():
    plan = build_plan(
        mode="separate_and_combined",
        rppg_enabled=True,
        aa_attacks=["prime"],
        oldcam_versions=["v13"],
    )
    line = plan_preview_line(plan)
    # 7 recipes total -> 6 extra.
    assert "+ 6 more variants" in line
    assert "(powerset)" in line
    assert line.startswith("Kling → ")


def test_preview_line_combined_only_single_variant():
    plan = build_plan(
        mode="combined_only", rppg_enabled=True, oldcam_versions=["v13"]
    )
    line = plan_preview_line(plan)
    assert "more variant" not in line
    assert "(combined only)" in line


def test_recipe_expected_suffixes_order():
    recipe = Recipe(
        steps=(
            Step(Modifier.RPPG),
            Step(Modifier.AA, "prime"),
            Step(Modifier.OLDCAM, "v13"),
        )
    )
    assert recipe_expected_suffixes(recipe) == ["-rppg", "_aa-prime", "-oldcam-v13"]


def test_normalize_mode_defaults():
    assert normalize_mode("bogus") == DEFAULT_MODE
    assert normalize_mode(None) == DEFAULT_MODE
    assert normalize_mode("combined_only") == "combined_only"
    assert normalize_mode("  SEPARATE_AND_COMBINED ") == "separate_and_combined"
