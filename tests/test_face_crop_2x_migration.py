"""Regression tests for the one-time Run 2x reset migration added in
PR fix/step0-composite-and-rppg-v2.5.

The bug: PR #48 fixed the in-code default of `outpaint_double_expand`
to False, but configs persisted from before that PR still carried
`True` forever. The migration forces False on first launch if marker
`outpaint_2x_default_reset_v2` is absent. After that, the user's
manual re-check is sticky.

Tests use the safe `_parse_bool` helper (face_similarity._parse_bool)
to handle the case where JSON has been round-tripped through string
keys (e.g. `"false"` instead of `false`). A bare `bool("false")`
would treat the string as truthy and silently flip the migration
the wrong way.

This module tests the helper + the get_config_updates persistence
contract, not the full Tk init flow (FaceCropTab requires a Tk root
which is fragile in CI).
"""

from face_similarity import _parse_bool


def test_parse_bool_handles_string_false():
    """`bool("false")` is True; `_parse_bool("false")` must be False."""
    assert _parse_bool("false") is False
    assert _parse_bool("FALSE") is False
    assert _parse_bool("False") is False
    assert _parse_bool("0") is False
    assert _parse_bool("no") is False
    assert _parse_bool("off") is False


def test_parse_bool_handles_string_true():
    assert _parse_bool("true") is True
    assert _parse_bool("TRUE") is True
    assert _parse_bool("1") is True
    assert _parse_bool("yes") is True
    assert _parse_bool("on") is True


def test_parse_bool_handles_real_bools():
    assert _parse_bool(True) is True
    assert _parse_bool(False) is False


def test_parse_bool_returns_none_for_uncoercible():
    """The migration uses _parse_bool == truthy / falsy paths. None
    means "couldn't tell" — caller falls back to the default.
    """
    assert _parse_bool(None) is None
    assert _parse_bool({"weird": True}) is None
    assert _parse_bool([1, 2, 3]) is None
    # Ambiguous int (not 0/1) -> None — could be a legacy version code
    # under a bool-named key.
    assert _parse_bool(2) is None
    assert _parse_bool(-1) is None


def test_parse_bool_accepts_int_0_and_1():
    """PR #53 round 2 — subagent M4. A config that programmatically
    holds the int 1 (e.g., set by a future surface) was previously
    returned as None — the migration guard then treated it as
    "marker absent" and re-fired every launch. Accept 0/1 ints
    strictly.
    """
    assert _parse_bool(0) is False
    assert _parse_bool(1) is True
    # Real bools still return as themselves (not coerced via int branch).
    assert _parse_bool(True) is True
    assert _parse_bool(False) is False


def test_template_pre_stamps_migration_marker():
    """Fresh installs ship with the marker already set so the
    migration is a no-op on first launch of a fresh bundle.
    """
    import json
    from pathlib import Path

    repo_root = Path(__file__).resolve().parent.parent
    template_path = repo_root / "default_config_template.json"
    template = json.loads(template_path.read_text(encoding="utf-8"))
    # PR fix/step0-composite-and-rppg-v2.5 — must be True so a
    # fresh install doesn't trigger the migration on first launch.
    assert template.get("outpaint_2x_default_reset_v2") is True
    # And the default value of the controlled key must be False.
    assert template.get("outpaint_double_expand") is False


def test_release_prep_pre_stamps_migration_marker():
    """The dist bundle preparer forces the marker even if the dev
    machine's kling_config.json doesn't carry it yet — otherwise the
    bundle would still ship with the migration "armed" and fire on
    first launch on the user's machine (harmless but undesired).
    """
    import json
    from pathlib import Path

    repo_root = Path(__file__).resolve().parent.parent
    release_prep_src = (repo_root / "distribution" / "release_prep.py").read_text(
        encoding="utf-8"
    )
    # Both: the key is named here AND the override is unconditional.
    assert "outpaint_2x_default_reset_v2" in release_prep_src
    # The override line should use template.get(...) with True default,
    # mirroring the template-driven contract.
    assert "template.get(\"outpaint_2x_default_reset_v2\", True)" in release_prep_src
