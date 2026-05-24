"""Pytest fixtures for the kling-ui test suite.

PR #49 round-2 M-3: an autouse fixture resets ``path_utils._INSTANCE_ID_CACHE``
between every test. Without this, tests that set ``KLING_INSTANCE_ID`` env or
generate fresh ids leak the cached value into subsequent tests in the same
pytest session, causing test-order-dependent flakes (especially for runtime
path tests where the cache must reflect a freshly-set env value).

Round-3 review (L3): clear the cache BEFORE the test runs too, so a new
test author doesn't need to remember the ``path_utils._INSTANCE_ID_CACHE
= None`` boilerplate at the top of their test.

Round-3 review (H2): narrowed the ``except`` to ``AttributeError`` — the
only legitimate reason ``path_utils._INSTANCE_ID_CACHE`` would fail to
assign is a rename of the attribute. Any other failure (``ImportError``,
real bugs) should propagate so the test suite catches them loudly
instead of silently leaking state.
"""

import pytest


def _clear_instance_id_cache() -> None:
    import path_utils
    try:
        path_utils._INSTANCE_ID_CACHE = None
    except AttributeError:
        # Attribute renamed/removed in a future refactor. Tests that rely on
        # the old name will fail on their own; the fixture just no-ops.
        pass


@pytest.fixture(autouse=True)
def _reset_instance_id_cache():
    """Clear ``path_utils._INSTANCE_ID_CACHE`` before and after each test."""
    _clear_instance_id_cache()
    yield
    _clear_instance_id_cache()
