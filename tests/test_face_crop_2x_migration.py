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


def test_template_pre_stamps_migration_markers():
    """Fresh installs ship with both v2 + v3 markers already set so
    the migration is a no-op on first launch of a fresh bundle.
    """
    import json
    from pathlib import Path

    repo_root = Path(__file__).resolve().parent.parent
    template_path = repo_root / "default_config_template.json"
    template = json.loads(template_path.read_text(encoding="utf-8"))
    # v2 (PR fix/step0-composite-and-rppg-v2.5), v3 (round 10), and v4
    # (PR #81, "session-only" contract) must all be True so a fresh
    # install doesn't trigger any migration on first launch.
    assert template.get("outpaint_2x_default_reset_v2") is True
    assert template.get("outpaint_2x_default_reset_v3") is True
    assert template.get("outpaint_2x_session_only_v4") is True
    # v4 contract: outpaint_double_expand must NOT appear in the template
    # — Run 2x is session-only state now, never persisted. The user
    # explicitly asked: "for all versions all future dists never should
    # 'run 2x' be checked by default" (2026-06-06). Stripping the key
    # from the template removes the on-disk surface where a stale value
    # could ever set Run 2x checked at launch.
    assert "outpaint_double_expand" not in template, (
        "Run 2x is session-only as of PR #81 (v4 contract); "
        "the template MUST NOT carry outpaint_double_expand."
    )


def test_release_prep_pre_stamps_migration_markers():
    """The dist bundle preparer forces both markers even if the dev
    machine's kling_config.json doesn't carry them yet — otherwise
    the bundle would still ship with the migration "armed" and fire
    on first launch on the user's machine (harmless but undesired).
    """
    from pathlib import Path

    repo_root = Path(__file__).resolve().parent.parent
    release_prep_src = (repo_root / "distribution" / "release_prep.py").read_text(
        encoding="utf-8"
    )
    # All three migration markers (v2, v3, v4) are named here AND all
    # overrides are unconditional.
    assert "outpaint_2x_default_reset_v2" in release_prep_src
    assert "outpaint_2x_default_reset_v3" in release_prep_src
    assert "outpaint_2x_session_only_v4" in release_prep_src
    # The override lines should use template.get(...) with True default,
    # mirroring the template-driven contract.
    assert "template.get(\"outpaint_2x_default_reset_v2\", True)" in release_prep_src
    assert "template.get(\"outpaint_2x_default_reset_v3\", True)" in release_prep_src
    assert "template.get(\"outpaint_2x_session_only_v4\", True)" in release_prep_src
    # v4 contract: the bundle config MUST NOT carry outpaint_double_expand
    # (Run 2x is session-only). Verify release_prep STRIPS it via
    # `config.pop(...)`.
    assert 'config.pop("outpaint_double_expand"' in release_prep_src, (
        "PR #81 v4 contract: release_prep must strip outpaint_double_expand "
        "from the bundle config so Run 2x is never persisted."
    )


def test_v3_migration_logic_fires_when_only_v2_present():
    """PR #53 round 10: user manual smoke caught configs that have
    `outpaint_2x_default_reset_v2=true` AND `outpaint_double_expand=
    true` simultaneously — v2 stamped but value not reset (likely a
    write race when v2 first introduced). v3 forces one more reset
    for those configs.

    This test exercises the bool-parsing migration GUARD via the
    same `_parse_bool` semantics the GUI uses (we can't easily
    instantiate FaceCropTab in CI — see module docstring). It
    asserts the OUTCOMES the GUI code uses to decide whether to
    fire the migration:

    - v3 absent → migration fires (parsed is None/False)
    - v3 present (bool True) → migration skipped (parsed is True)
    - v3 present (string "false") → migration fires (parsed is False)
    - v3 present (garbage) → migration skipped (parsed is None,
      treated as "marker present" for safety, M3 from round 5)
    """
    def _migration_already_done(marker_value):
        parsed = _parse_bool(marker_value)
        # Same semantics as kling_gui/tabs/face_crop_tab.py round 10.
        return parsed is True or parsed is None

    # v3 absent → migration fires.
    assert _migration_already_done(False) is False
    # v3 stamped as True → skip.
    assert _migration_already_done(True) is True
    # v3 stamped as "true" string → skip.
    assert _migration_already_done("true") is True
    # v3 stamped as "false" string → fire (parsed False, not None).
    assert _migration_already_done("false") is False
    # v3 as uncoercible garbage → skip (conservative, M3 round 5).
    assert _migration_already_done({"weird": True}) is True
    assert _migration_already_done([1, 2, 3]) is True
