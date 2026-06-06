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

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent


def _parse_optional_extras(pyproject_text: str) -> dict[str, str]:
    """Return {extra_name: extra_body} for [project.optional-dependencies].

    Toy parser — we only need to detect named extras and grep their bodies.
    Reaching for ``tomllib`` would also work, but staying string-only keeps
    this test fast and Python-version-independent (3.10 has no tomllib;
    pyproject still expects 3.10 per the pyrightconfig).
    """
    extras: dict[str, str] = {}
    in_section = False
    current_name: str | None = None
    current_body: list[str] = []
    for line in pyproject_text.splitlines():
        stripped = line.strip()
        if stripped == "[project.optional-dependencies]":
            in_section = True
            continue
        if in_section and stripped.startswith("[") and stripped.endswith("]"):
            # Next top-level table starts — flush.
            if current_name is not None:
                extras[current_name] = "\n".join(current_body)
            in_section = False
            current_name = None
            current_body = []
            continue
        if not in_section:
            continue
        # Inside the section: detect "name = [".
        m = re.match(r"^([A-Za-z_][A-Za-z0-9_-]*)\s*=\s*\[", stripped)
        if m:
            if current_name is not None:
                extras[current_name] = "\n".join(current_body)
            current_name = m.group(1)
            current_body = []
            continue
        if current_name is not None:
            current_body.append(stripped)
    if current_name is not None:
        extras[current_name] = "\n".join(current_body)
    return extras


def test_pyproject_declares_dev_extra_with_pytest():
    """Root pyproject.toml must expose a ``dev`` extra with a real pytest spec.

    Without this, ``uv sync`` (the v2.20+ primary path) actively REMOVES
    pytest from the venv on every launch, and the CLAUDE.md pre-commit
    invariant `pytest tests/ similarity/tests/ -q` can only be satisfied
    by ad-hoc out-of-band ``pip install pytest``. A contributor cloning the
    repo on a fresh box hits this immediately.
    """
    pyproject = ROOT / "pyproject.toml"
    text = pyproject.read_text(encoding="utf-8")
    extras = _parse_optional_extras(text)

    assert "dev" in extras, (
        "pyproject.toml [project.optional-dependencies] does not declare a "
        "`dev` extra. Required by the CLAUDE.md pre-commit invariant — "
        "`uv sync --extra dev` must be sufficient to run "
        "`pytest tests/ similarity/tests/ -q`."
    )

    body = extras["dev"]
    assert re.search(r"['\"]pytest\b", body), (
        f"`dev` extra exists but does not include pytest. Body:\n{body}\n"
        "Required by the CLAUDE.md pre-commit invariant."
    )


def test_distribution_pyproject_mirrors_dev_extra():
    """distribution/pyproject.toml is the packaging-only mirror; it must
    track the dev extra so a release build can re-run the test suite
    against the dist tree if a build-time validation phase ever runs it.
    """
    dist_pyproject = ROOT / "distribution" / "pyproject.toml"
    if not dist_pyproject.exists():
        pytest.skip("distribution/pyproject.toml not present")

    text = dist_pyproject.read_text(encoding="utf-8")
    extras = _parse_optional_extras(text)

    assert "dev" in extras, (
        "distribution/pyproject.toml lacks a `dev` extra. It must mirror "
        "the root pyproject so the packaging tree stays a faithful copy "
        "(an asymmetry surfaces as a missing dep in the built dist)."
    )
    assert re.search(r"['\"]pytest\b", extras["dev"]), (
        "distribution/pyproject.toml `dev` extra does not include pytest."
    )
