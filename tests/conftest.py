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

import uuid

import pytest


# Module-scoped (one value per pytest invocation) so the workspace name
# is stable across every test in this run, but uuid4-suffixed so it can
# never collide with a real workspace dir on disk. CodeRabbit PR #53
# round 13 caught the fixed sentinel "_pytest_isolated" — if anyone
# ever materialized that workspace locally (manually or via a future
# launcher option), the autouse fixture's isolation would silently
# break. Per-test uniqueness adds no extra isolation benefit (each test
# already has its own monkeypatch fixture instance + temp dirs).
_PYTEST_WORKSPACE_NAME = f"_pytest_isolated_{uuid.uuid4().hex}"


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


@pytest.fixture(autouse=True)
def _isolate_kling_workspace(monkeypatch):
    """Force tests into an unused workspace so ``_iter_extra_sessions_dirs``
    never resolves to the developer's real per-instance autosave tree.

    Without this, ``session_manager.list_sessions`` /
    ``find_dead_sessions`` / ``prune_dead_sessions`` aggregate the user's
    live ``~/Library/Application Support/.../workspaces/default/runtime/
    instances/*/sessions/`` rolling autosaves into the test result — making
    assertions on returned record counts flake on dev machines that have
    real GUI history. CI is clean so the bug only surfaces locally.

    Setting an unused workspace name makes ``get_workspace_dir`` resolve to
    a directory that doesn't exist; ``_iter_extra_sessions_dirs``'s
    ``os.path.isdir`` guard then returns ``[]``. Tests that explicitly want
    a different workspace value override this baseline via their own
    ``monkeypatch.setenv`` / ``monkeypatch.delenv`` — pytest's monkeypatch
    handles the override + unwind correctly.

    The workspace name is uuid4-suffixed at module load (see
    ``_PYTEST_WORKSPACE_NAME`` above) so it can never collide with a real
    on-disk workspace, no matter what the user has previously created.
    """
    monkeypatch.setenv("KLING_WORKSPACE", _PYTEST_WORKSPACE_NAME)
