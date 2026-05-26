"""Unit tests for scripts/gpu_bootstrap.py.

These tests cover the pure logic (stamp parsing, package-name mapping,
KLING_SKIP_GPU_BOOTSTRAP opt-out, TTL behaviour for the no_nvidia
stamp). They DO NOT exercise actual nvidia-smi or pip install — those
are integration concerns we verify manually on the user's CUDA host.

The module is intentionally importable as a library so the launcher
can shell to ``python scripts/gpu_bootstrap.py`` for the production
flow AND the tests can drive ``bootstrap()`` directly with monkey-
patched detection.
"""
from __future__ import annotations

import datetime as _dt
import json
import sys
from pathlib import Path

import pytest


# scripts/ isn't a package — add the dir to sys.path so we can import.
_REPO_ROOT = Path(__file__).resolve().parent.parent
_SCRIPTS_DIR = _REPO_ROOT / "scripts"
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

import gpu_bootstrap  # noqa: E402


@pytest.fixture(autouse=True)
def _isolated_stamp_dir(tmp_path, monkeypatch):
    """Redirect STATE_DIR / STAMP_PATH / LOCK_PATH to tmp_path so each
    test starts with a clean slate. The production paths under
    .launcher_state/ are shared across the test runner and the live
    launcher — never poison them from a test."""
    state = tmp_path / "state"
    state.mkdir()
    monkeypatch.setattr(gpu_bootstrap, "STATE_DIR", state)
    monkeypatch.setattr(gpu_bootstrap, "STAMP_PATH", state / "gpu_status.json")
    monkeypatch.setattr(gpu_bootstrap, "LOCK_PATH", state / "gpu_bootstrap.lock")
    yield


def test_cuda_to_cupy_map_has_only_supported_majors():
    """CuPy 14+ wheels exist for CUDA 12.x and 13.x. CUDA 11.x is not
    in the current-stable map; the launcher correctly falls back to
    CPU on 11.x rather than pinning an old CuPy. If/when CuPy 15
    drops the 12.x wheel this test will catch the change so we update
    the doc + the no-NVIDIA explanation."""
    assert set(gpu_bootstrap._CUDA_TO_CUPY) == {12, 13}
    assert gpu_bootstrap._CUDA_TO_CUPY[12].startswith("cupy-cuda12x")
    assert gpu_bootstrap._CUDA_TO_CUPY[13].startswith("cupy-cuda13x")
    # [ctk] extra pulls CUDA component wheels from PyPI so the install
    # works on driver-only hosts (no system CUDA toolkit needed).
    assert "[ctk]" in gpu_bootstrap._CUDA_TO_CUPY[12]
    assert "[ctk]" in gpu_bootstrap._CUDA_TO_CUPY[13]


def test_skip_env_var_short_circuits(monkeypatch):
    """KLING_SKIP_GPU_BOOTSTRAP=1 must skip everything — no detection,
    no install attempt, no stamp write."""
    monkeypatch.setenv("KLING_SKIP_GPU_BOOTSTRAP", "1")
    # If detect_nvidia ran, it would write a stamp. Spy on it.
    monkeypatch.setattr(
        gpu_bootstrap, "detect_nvidia",
        lambda: pytest.fail("detect_nvidia called despite KLING_SKIP_GPU_BOOTSTRAP=1"),
    )
    result = gpu_bootstrap.bootstrap("python_unused")
    assert result == "skipped"
    assert not gpu_bootstrap.STAMP_PATH.exists()


def test_no_nvidia_writes_stamp(monkeypatch):
    """When nvidia-smi is absent (return None), the stamp records
    no_nvidia and the result is no_nvidia (NOT cached_no_nvidia which
    is for the cache hit on subsequent calls)."""
    monkeypatch.setattr(gpu_bootstrap, "detect_nvidia", lambda: None)
    result = gpu_bootstrap.bootstrap("python_unused")
    assert result == "no_nvidia"
    payload = json.loads(gpu_bootstrap.STAMP_PATH.read_text())
    assert payload["result"] == "no_nvidia"
    assert payload["driver_version"] is None
    assert payload["cuda_major"] is None


def test_cached_no_nvidia_short_circuits_within_ttl(monkeypatch):
    """A recent no_nvidia stamp must not re-run detection — the user
    swapping GPUs is rare and the TTL handles it."""
    now = _dt.datetime.now(_dt.timezone.utc)
    gpu_bootstrap._write_stamp({
        "result": "no_nvidia",
        "checked_at": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "driver_version": None, "cuda_major": None,
        "cupy_package": None, "cupy_version": None,
    })
    called = []
    monkeypatch.setattr(gpu_bootstrap, "detect_nvidia", lambda: called.append(1) or None)
    result = gpu_bootstrap.bootstrap("python_unused", quiet_if_cached=True)
    assert result == "cached_no_nvidia"
    assert called == [], "detection should NOT run on a recent no_nvidia stamp"


def test_expired_no_nvidia_stamp_triggers_recheck(monkeypatch):
    """After TTL (30 days) the stamp is stale and we re-detect — a
    user who installed a GPU since last check gets picked up."""
    stale = _dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(days=31)
    gpu_bootstrap._write_stamp({
        "result": "no_nvidia",
        "checked_at": stale.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "driver_version": None, "cuda_major": None,
        "cupy_package": None, "cupy_version": None,
    })
    called = []
    monkeypatch.setattr(gpu_bootstrap, "detect_nvidia",
                        lambda: called.append(1) or None)
    gpu_bootstrap.bootstrap("python_unused", quiet_if_cached=True)
    assert called == [1], "expired stamp must trigger fresh detection"


def test_install_failed_retry_cap(monkeypatch):
    """install_failed with attempts >= 3 must NOT retry — the user has
    to clear the stamp to break the loop. Avoids hammering pip every
    launch when the issue is something the script can't fix (mismatched
    CUDA toolkit, network down, etc.)."""
    gpu_bootstrap._write_stamp({
        "result": "install_failed",
        "checked_at": _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "driver_version": "555.52", "cuda_major": 12,
        "cupy_package": "cupy-cuda12x[ctk]", "cupy_version": None,
        "attempts": 3, "last_error": "wheel not found",
    })
    monkeypatch.setattr(
        gpu_bootstrap, "detect_nvidia",
        lambda: pytest.fail("detect_nvidia called despite install_failed cap"),
    )
    result = gpu_bootstrap.bootstrap("python_unused")
    assert result == "install_failed"


def test_install_failed_below_cap_retries(monkeypatch):
    """Attempts < cap means we DO retry. On the retry the stamp gets
    overwritten by the fresh install attempt (success or new failure
    with attempts++)."""
    gpu_bootstrap._write_stamp({
        "result": "install_failed",
        "checked_at": _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "driver_version": "555.52", "cuda_major": 12,
        "cupy_package": "cupy-cuda12x[ctk]", "cupy_version": None,
        "attempts": 1, "last_error": "transient network",
    })
    monkeypatch.setattr(
        gpu_bootstrap, "detect_nvidia",
        lambda: {"driver_version": "555.52", "cuda_major": 12},
    )
    # Force the install to fail again so we can check attempts increments.
    monkeypatch.setattr(
        gpu_bootstrap, "install_cupy",
        lambda *a, **kw: (False, "still failing"),
    )
    result = gpu_bootstrap.bootstrap("python_unused")
    assert result == "install_failed"
    payload = json.loads(gpu_bootstrap.STAMP_PATH.read_text())
    assert payload["attempts"] == 2, "attempts must increment on retry"
    assert payload["last_error"] == "still failing"


def test_successful_install_writes_gpu_ready_stamp(monkeypatch):
    """A successful install path → gpu_ready stamp with cupy_version."""
    monkeypatch.setattr(
        gpu_bootstrap, "detect_nvidia",
        lambda: {"driver_version": "555.52", "cuda_major": 12},
    )
    monkeypatch.setattr(
        gpu_bootstrap, "install_cupy",
        lambda *a, **kw: (True, "13.3.0"),
    )
    result = gpu_bootstrap.bootstrap("python_unused")
    assert result == "gpu_installed_now"
    payload = json.loads(gpu_bootstrap.STAMP_PATH.read_text())
    assert payload["result"] == "gpu_ready"
    assert payload["cupy_version"] == "13.3.0"
    assert payload["cupy_package"] == "cupy-cuda12x[ctk]"
    assert payload["attempts"] == 0


def test_gpu_ready_cache_revalidates_via_probe(monkeypatch):
    """A cached gpu_ready stamp re-runs the import probe before
    declaring ready. If the venv was wiped between launches (probe
    fails), we fall through to detection + reinstall."""
    gpu_bootstrap._write_stamp({
        "result": "gpu_ready",
        "checked_at": _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "driver_version": "555.52", "cuda_major": 12,
        "cupy_package": "cupy-cuda12x[ctk]", "cupy_version": "13.3.0",
        "attempts": 0,
    })
    probe_calls = []
    monkeypatch.setattr(
        gpu_bootstrap, "probe_cupy",
        lambda exe: probe_calls.append(exe) or "13.3.0",
    )
    result = gpu_bootstrap.bootstrap("python_used", quiet_if_cached=True)
    assert result == "gpu_ready"
    assert probe_calls == ["python_used"], (
        "gpu_ready cache must re-probe to catch a wiped venv between launches"
    )


def test_unsupported_cuda_major_short_circuits_to_no_nvidia(monkeypatch):
    """Subagent MEDIUM on PR #54 round 1: CUDA 11.x / 10.x / 14+ has no
    current-stable CuPy wheel. Before the fix, install_cupy returned
    False three launches in a row before the install_failed cap fired,
    polluting the log with three "CuPy install failed" messages on
    every launch for a CUDA 11 user. The fix: short-circuit BEFORE
    acquiring the lock, write a permanent no_nvidia stamp with
    descriptive cuda_major + last_error so a future debugger sees
    "GPU was there, just unsupported by current CuPy."
    """
    monkeypatch.setattr(
        gpu_bootstrap, "detect_nvidia",
        lambda: {"driver_version": "470.82", "cuda_major": 11},
    )
    install_called = []
    monkeypatch.setattr(
        gpu_bootstrap, "install_cupy",
        lambda exe, major: install_called.append((exe, major)) or (False, "should not run"),
    )
    result = gpu_bootstrap.bootstrap("python_unused")
    assert result == "no_nvidia"
    assert install_called == [], (
        "unsupported CUDA major must short-circuit BEFORE the lock + "
        "install path"
    )
    payload = json.loads(gpu_bootstrap.STAMP_PATH.read_text())
    assert payload["result"] == "no_nvidia"
    assert payload["cuda_major"] == 11, (
        "the stamp must preserve cuda_major so a debugger can see "
        "GPU detection worked but CuPy didn't have a matching wheel"
    )
    assert payload["driver_version"] == "470.82"
    assert "CUDA 11" in (payload.get("last_error") or ""), (
        "stamp must carry a human-readable reason"
    )


def test_install_failed_concurrent_attempts_increment_monotonically(monkeypatch):
    """Subagent HIGH on PR #54 round 1: simulate the race where two
    launchers both load the stamp pre-lock, both pass the cap check,
    and serialize on the GPU bootstrap lock. The second launcher must
    re-read the stamp INSIDE the lock so its attempts increment from
    the post-first-launcher state, not from its own stale in-memory
    snapshot — otherwise the second launcher clobbers the first's
    attempts=2 with another attempts=2 and the cap is never reached.

    We can't truly run two processes inside a unit test, but we can
    verify the fix by simulating the on-disk state transition: write
    attempts=1, then run bootstrap() with the in-memory stamp at
    attempts=1 but the on-disk stamp at attempts=2 (the state another
    process would have just written), and assert the new stamp has
    attempts=3 (not 2).
    """
    # Initial stamp on disk: attempts=1 (matches what we load).
    gpu_bootstrap._write_stamp({
        "result": "install_failed",
        "checked_at": _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "driver_version": "555.52", "cuda_major": 12,
        "cupy_package": "cupy-cuda12x[ctk]", "cupy_version": None,
        "attempts": 1, "last_error": "first failure",
    })
    monkeypatch.setattr(
        gpu_bootstrap, "detect_nvidia",
        lambda: {"driver_version": "555.52", "cuda_major": 12},
    )
    # Force install to fail so we hit the increment path.
    monkeypatch.setattr(
        gpu_bootstrap, "install_cupy",
        lambda *a, **kw: (False, "still failing"),
    )

    # Simulate process A writing attempts=2 between THIS process's
    # initial stamp read and the in-lock re-read. We patch _acquire_lock
    # to do this side-effect for us, mimicking the race.
    def _spy_acquire_lock(quiet=False):
        # Mid-flight write to disk — what process A would have done.
        gpu_bootstrap._write_stamp({
            "result": "install_failed",
            "checked_at": _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "driver_version": "555.52", "cuda_major": 12,
            "cupy_package": "cupy-cuda12x[ctk]", "cupy_version": None,
            "attempts": 2, "last_error": "process A second failure",
        })
        return True
    monkeypatch.setattr(gpu_bootstrap, "_acquire_lock", _spy_acquire_lock)
    monkeypatch.setattr(gpu_bootstrap, "_release_lock", lambda: None)

    gpu_bootstrap.bootstrap("python_unused")
    payload = json.loads(gpu_bootstrap.STAMP_PATH.read_text())
    assert payload["attempts"] == 3, (
        f"expected attempts=3 (process A wrote 2 inside the lock, this "
        f"process must re-read + write 3), got {payload['attempts']} — "
        f"the H1 lock re-read regression has reappeared"
    )


def test_gpu_ready_recheck_after_lock_short_circuits_install(monkeypatch):
    """CodeRabbit major (PR #54 round 1): after acquiring the lock,
    re-check the stamp for a FRESH gpu_ready before doing any install
    work. A sibling launcher may have just installed CuPy while we
    were waiting on the lock — running another pip install would be
    redundant.

    Simulate: pre-lock stamp is install_failed (so we enter the lock),
    but DURING lock acquisition a sibling writes a gpu_ready stamp.
    The current process must short-circuit on the in-lock fresh stamp
    read, NOT proceed to install_cupy."""
    gpu_bootstrap._write_stamp({
        "result": "install_failed",
        "checked_at": _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "driver_version": "555.52", "cuda_major": 12,
        "cupy_package": "cupy-cuda12x[ctk]", "cupy_version": None,
        "attempts": 1, "last_error": "transient",
    })
    monkeypatch.setattr(
        gpu_bootstrap, "detect_nvidia",
        lambda: {"driver_version": "555.52", "cuda_major": 12},
    )
    install_called = []
    monkeypatch.setattr(
        gpu_bootstrap, "install_cupy",
        lambda exe, major: install_called.append((exe, major)) or (True, "13.3.0"),
    )
    # Probe returns success — pretending the sibling's install IS valid.
    monkeypatch.setattr(gpu_bootstrap, "probe_cupy", lambda exe: "13.3.0")

    def _spy_acquire_lock(quiet=False):
        # Simulate the sibling completing its install while we waited.
        gpu_bootstrap._write_stamp({
            "result": "gpu_ready",
            "checked_at": _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "driver_version": "555.52", "cuda_major": 12,
            "cupy_package": "cupy-cuda12x[ctk]", "cupy_version": "13.3.0",
            "attempts": 0,
        })
        return True
    monkeypatch.setattr(gpu_bootstrap, "_acquire_lock", _spy_acquire_lock)
    monkeypatch.setattr(gpu_bootstrap, "_release_lock", lambda: None)

    result = gpu_bootstrap.bootstrap("python_unused")
    assert result == "gpu_ready", (
        "post-lock fresh-stamp gpu_ready re-check must short-circuit "
        "BEFORE install_cupy runs"
    )
    assert install_called == [], (
        "install_cupy must NOT be called when a sibling already wrote "
        "gpu_ready while we were waiting on the lock"
    )


def test_write_stamp_swallows_oserror(monkeypatch):
    """Gemini medium (PR #54 round 1): _write_stamp must degrade
    gracefully when the state dir is read-only / disk is full / a
    permission error fires — the launcher chain treats a non-zero
    exit from gpu_bootstrap as fatal, so an unhandled OSError here
    would block legitimate GUI launches."""
    def _raise(*args, **kwargs):
        raise PermissionError("simulated read-only mount")
    monkeypatch.setattr(gpu_bootstrap.Path, "write_text", _raise)
    # Must NOT raise.
    gpu_bootstrap._write_stamp({"result": "no_nvidia"})


def test_acquire_lock_returns_false_on_state_dir_oserror(monkeypatch):
    """Gemini medium (PR #54 round 1): _acquire_lock degrades to
    "False" (which the caller treats as "fall back to CPU this
    launch") when STATE_DIR.mkdir raises a non-FileExistsError
    OSError — restricted filesystems must NOT crash the bootstrap."""
    real_mkdir = gpu_bootstrap.Path.mkdir
    def _selective_mkdir(self, *args, **kwargs):
        # First call (STATE_DIR) raises, subsequent calls work — but
        # we should never get there because the function returns
        # immediately on the first failure.
        raise PermissionError("simulated permission denied")
    monkeypatch.setattr(gpu_bootstrap.Path, "mkdir", _selective_mkdir)
    result = gpu_bootstrap._acquire_lock()
    assert result is False, (
        "lock acquisition must return False (degrade to CPU) when "
        "the state dir cannot be created, not raise"
    )


def test_pip_install_timeout_constant_is_strictly_less_than_lock_stale():
    """CodeRabbit major + Sourcery (PR #54 round 1): the
    lock-staleness window MUST be larger than the pip-install
    timeout, otherwise a slow but legitimate first-time install
    could trip the stale-lock check and a second launcher would
    rmdir the live lock and kick off a parallel pip install into
    the shared venv."""
    assert (
        gpu_bootstrap.LOCK_STALE_SECONDS > gpu_bootstrap.PIP_INSTALL_TIMEOUT_SECONDS
    ), (
        f"LOCK_STALE_SECONDS ({gpu_bootstrap.LOCK_STALE_SECONDS}) must be "
        f"strictly greater than PIP_INSTALL_TIMEOUT_SECONDS "
        f"({gpu_bootstrap.PIP_INSTALL_TIMEOUT_SECONDS}) or a slow valid "
        f"pip install could be misclassified as stale"
    )


def test_gpu_ready_cache_falls_through_when_probe_fails(monkeypatch):
    """If the probe returns None (venv broken) we MUST NOT return
    gpu_ready off the cache — fall through to fresh detection +
    reinstall. Otherwise a broken cupy would silently stay cached."""
    gpu_bootstrap._write_stamp({
        "result": "gpu_ready",
        "checked_at": _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "driver_version": "555.52", "cuda_major": 12,
        "cupy_package": "cupy-cuda12x[ctk]", "cupy_version": "13.3.0",
        "attempts": 0,
    })
    monkeypatch.setattr(gpu_bootstrap, "probe_cupy", lambda exe: None)
    monkeypatch.setattr(
        gpu_bootstrap, "detect_nvidia",
        lambda: {"driver_version": "555.52", "cuda_major": 12},
    )
    install_calls = []
    monkeypatch.setattr(
        gpu_bootstrap, "install_cupy",
        lambda exe, major: install_calls.append((exe, major)) or (True, "13.3.0"),
    )
    result = gpu_bootstrap.bootstrap("python_used")
    assert result == "gpu_installed_now"
    assert install_calls == [("python_used", 12)]
