"""Pytest fixtures for the kling-ui test suite.

PR #49 round-2 M-3: an autouse fixture resets ``path_utils._INSTANCE_ID_CACHE``
after every test. Without this, tests that set ``KLING_INSTANCE_ID`` env or
generate fresh ids leak the cached value into subsequent tests in the same
pytest session, causing test-order-dependent flakes (especially for runtime
path tests where the cache must reflect a freshly-set env value).

Individual tests are still free to set the cache to None at the top of the
test if they need a known starting point — the autouse fixture only acts
after the test completes, so it does not interfere.
"""

import pytest


@pytest.fixture(autouse=True)
def _reset_instance_id_cache():
    """Clear ``path_utils._INSTANCE_ID_CACHE`` after each test."""
    yield
    try:
        import path_utils
        path_utils._INSTANCE_ID_CACHE = None
    except Exception:
        pass
