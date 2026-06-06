"""v2.24 polish — guard that pytest is declared in the project's dep manifest.

The pre-commit invariant in CLAUDE.md ("Default Workflow After Feature Work")
is ``pytest tests/ similarity/tests/ -q`` — but pre-v2.24 pytest was NOT
declared in pyproject.toml, requirements.txt, or uv.lock. Every macOS
``uv sync`` therefore UNINSTALLED pytest from the venv, and a fresh-clone
contributor couldn't satisfy the invariant without ``pip install pytest``
out-of-band. (Verified during the v2.21 → v2.23 macOS polish: an initial
``uv sync`` removed pytest 9.0.3, breaking the test run.)

This test pins the contract: pytest must live in ``[project.optional-
dependencies.dev]`` so ``uv sync --extra dev`` is sufficient. Source-text
only so it runs on any OS without needing the env materialized.

Pairs with: tests/test_macos_tkdnd_loads.py (same v2.24 macOS-polish
round), tests/test_uv_lock_imports.py (which proves the locked set
imports cleanly post-sync).
"""

from __future__ import annotations

import sys
import tomllib
from pathlib import Path

import pytest
from packaging.requirements import InvalidRequirement, Requirement

ROOT = Path(__file__).resolve().parent.parent


def _read_optional_extras(pyproject_path: Path) -> dict[str, list[Requirement]]:
    """Parse ``[project.optional-dependencies]`` and return per-extra
    Requirement lists.

    Uses ``tomllib`` (stdlib, Python 3.11+) — replaces the hand-rolled
    line-by-line parser flagged by Gemini in round 2: a custom parser
    silently misses single-line ``[]`` forms, comments inside the body,
    and tomllib-legal multiline strings. The pyproject already requires
    Python >= 3.11 (the floor in ``requires-python``), so tomllib is
    always available in the test env.

    Returns an empty dict if the table is absent — caller asserts presence.
    Invalid Requirement strings are skipped silently; the test still
    fails if a legitimate pytest entry is missing.
    """
    if not pyproject_path.exists():
        return {}
    with pyproject_path.open("rb") as fp:
        data = tomllib.load(fp)
    raw = data.get("project", {}).get("optional-dependencies", {})
    parsed: dict[str, list[Requirement]] = {}
    for extra_name, spec_list in raw.items():
        reqs: list[Requirement] = []
        for spec in spec_list:
            try:
                reqs.append(Requirement(spec))
            except InvalidRequirement:
                continue
        parsed[extra_name] = reqs
    return parsed


def _extra_has_package(reqs: list[Requirement], package: str) -> Requirement | None:
    for r in reqs:
        if r.name.lower() == package.lower():
            return r
    return None


def test_pyproject_declares_dev_extra_with_pytest():
    """Root pyproject.toml must expose a ``dev`` extra with a real pytest spec.

    Without this, ``uv sync`` (the v2.20+ primary path) actively REMOVES
    pytest from the venv on every launch, and the CLAUDE.md pre-commit
    invariant `pytest tests/ similarity/tests/ -q` can only be satisfied
    by ad-hoc out-of-band ``pip install pytest``. A contributor cloning the
    repo on a fresh box hits this immediately.
    """
    extras = _read_optional_extras(ROOT / "pyproject.toml")
    assert "dev" in extras, (
        "pyproject.toml [project.optional-dependencies] does not declare a "
        "`dev` extra. Required by the CLAUDE.md pre-commit invariant — "
        "`uv sync --extra dev` must be sufficient to run "
        "`pytest tests/ similarity/tests/ -q`."
    )
    pytest_req = _extra_has_package(extras["dev"], "pytest")
    assert pytest_req is not None, (
        "`dev` extra exists but does not include pytest. "
        "Required by the CLAUDE.md pre-commit invariant."
    )


def test_distribution_pyproject_mirrors_dev_extra():
    """distribution/pyproject.toml is the packaging-only mirror; it must
    track the dev extra so a release build can re-run the test suite
    against the dist tree if a build-time validation phase ever runs it.

    Round-2 review (L2): we now also assert version-spec EQUALITY between
    root and dist mirrors, not just presence. Drift like root=
    ``pytest>=8,<10`` vs dist= ``pytest>=7,<11`` would silently ship a
    different test floor in the bundle without this check.
    """
    dist_pyproject = ROOT / "distribution" / "pyproject.toml"
    if not dist_pyproject.exists():
        pytest.skip("distribution/pyproject.toml not present")

    dist_extras = _read_optional_extras(dist_pyproject)
    assert "dev" in dist_extras, (
        "distribution/pyproject.toml lacks a `dev` extra. It must mirror "
        "the root pyproject so the packaging tree stays a faithful copy "
        "(an asymmetry surfaces as a missing dep in the built dist)."
    )
    dist_pytest = _extra_has_package(dist_extras["dev"], "pytest")
    assert dist_pytest is not None, (
        "distribution/pyproject.toml `dev` extra does not include pytest."
    )

    # Spec-parity with root.
    root_extras = _read_optional_extras(ROOT / "pyproject.toml")
    root_pytest = _extra_has_package(root_extras.get("dev", []), "pytest")
    assert root_pytest is not None, (
        "internal: root pyproject must have pytest in dev (covered by the "
        "test above; this fails only if BOTH tests are broken)"
    )
    assert str(dist_pytest.specifier) == str(root_pytest.specifier), (
        f"pytest spec drift between root and dist: root="
        f"{str(root_pytest.specifier)!r} vs dist="
        f"{str(dist_pytest.specifier)!r}. The distribution pyproject is a "
        "packaging mirror — keep the spec EQUAL so the bundled dev install "
        "matches the source-of-truth pin."
    )


def test_tomllib_available_in_test_runtime():
    """Guard: tomllib is required by the parser above. If the test env runs
    under Python < 3.11 (where tomllib isn't in stdlib yet), this test will
    fail loudly with a clear message instead of an opaque ImportError when
    the other tests in this file try to collect.

    Per pyproject.toml's ``requires-python``, the floor is already 3.11, so
    this should never fail in practice — but a contributor running tests
    in a stray 3.10 venv would get a clean signal here.
    """
    assert sys.version_info >= (3, 11), (
        f"Test runtime Python {sys.version_info[:2]} is below the pyproject "
        "floor of 3.11. tomllib (stdlib in 3.11+) is required by these "
        "tests. Re-run under .venv-macos / .venv311 (both are 3.11+)."
    )
