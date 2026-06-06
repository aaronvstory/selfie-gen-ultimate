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
    # The `[ctk]` extra is GONE — it was a no-op on cupy 13.6.0 (pulled NO
    # nvidia wheels). The CUDA component wheels now ship explicitly via
    # _CUDA_TO_NVIDIA_WHEELS, asserted below.
    assert "[ctk]" not in gpu_bootstrap._CUDA_TO_CUPY[12]
    assert "[ctk]" not in gpu_bootstrap._CUDA_TO_CUPY[13]


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


def test_detect_nvidia_returns_none_when_smi_missing_macos(monkeypatch):
    """macOS contract: nvidia-smi is not installed on Darwin, so the
    real subprocess.run call raises FileNotFoundError. detect_nvidia
    MUST swallow that and return None — the bootstrap then short-
    circuits to no_nvidia with NO install attempt (CuPy has no Metal
    backend, so the M1/M2 path stays CPU forever). This test
    simulates the macOS-without-NVIDIA case on any platform.

    User question 2026-05-27 ("make sure this keeps working fine on
    mac"): yes — this is the path."""
    import subprocess as _sp
    original_run = _sp.run

    def _fake_run(*args, **kwargs):
        raise FileNotFoundError("nvidia-smi not found (simulated macOS)")

    monkeypatch.setattr(gpu_bootstrap.subprocess, "run", _fake_run)
    try:
        assert gpu_bootstrap.detect_nvidia() is None, (
            "detect_nvidia must swallow FileNotFoundError and return None "
            "on hosts without nvidia-smi (macOS, Linux-without-NVIDIA, etc.)"
        )
    finally:
        monkeypatch.setattr(gpu_bootstrap.subprocess, "run", original_run)


def test_resolve_nvidia_smi_prefers_path(monkeypatch):
    """When nvidia-smi is on PATH, _resolve_nvidia_smi returns shutil.which's
    result verbatim and does no Windows-dir guessing."""
    monkeypatch.setattr(gpu_bootstrap.shutil, "which", lambda name: "/usr/bin/nvidia-smi")
    assert gpu_bootstrap._resolve_nvidia_smi() == "/usr/bin/nvidia-smi"


def test_resolve_nvidia_smi_none_on_posix_without_path(monkeypatch):
    """POSIX + not-on-PATH → None (no Windows fallback dirs apply)."""
    monkeypatch.setattr(gpu_bootstrap.shutil, "which", lambda name: None)
    monkeypatch.setattr(gpu_bootstrap, "_is_windows", lambda: False)
    assert gpu_bootstrap._resolve_nvidia_smi() is None


def test_resolve_nvidia_smi_finds_windows_system32(monkeypatch, tmp_path):
    """Code-review HIGH (PR #54): on Windows the driver may not put
    nvidia-smi on PATH. _resolve_nvidia_smi must still find it in the
    canonical System32 location."""
    sysroot = tmp_path / "Windows"
    smi = sysroot / "System32" / "nvidia-smi.exe"
    smi.parent.mkdir(parents=True)
    smi.write_text("")
    monkeypatch.setattr(gpu_bootstrap.shutil, "which", lambda name: None)
    monkeypatch.setattr(gpu_bootstrap, "_is_windows", lambda: True)
    monkeypatch.setenv("SystemRoot", str(sysroot))
    assert gpu_bootstrap._resolve_nvidia_smi() == str(smi)


# --- detect_nvidia header-format robustness (driver 610+ regression) ---------
#
# Real nvidia-smi no-flag headers, captured verbatim. The NEW one (driver 610.x,
# 2026) DROPPED the legacy "Driver Version:" / "CUDA Version:" strings entirely,
# replacing them with "NVIDIA-SMI 610.47", "KMD Version:" and "CUDA UMD Version:
# 13.3". The old regex matched neither -> detect_nvidia returned None -> a real
# RTX 4090 silently ran rPPG on CPU (verified 2026-06-04). These tests pin BOTH
# layouts so a future header tweak can't reintroduce the regression.

_SMI_HEADER_LEGACY = (
    "Tue May 13 10:00:00 2025\n"
    "+-----------------------------------------------------------------------------+\n"
    "| NVIDIA-SMI 555.52       Driver Version: 555.52       CUDA Version: 12.6      |\n"
    "|-------------------------------+----------------------+----------------------+\n"
)

_SMI_HEADER_NEW_610 = (
    "Thu Jun  4 02:47:53 2026\n"
    "+-----------------------------------------------------------------------------------------+\n"
    "| NVIDIA-SMI 610.47                 KMD Version: 610.47        CUDA UMD Version: 13.3     |\n"
    "+-----------------------------------------+------------------------+----------------------+\n"
)


_DEFAULT_GPU_NAME = "NVIDIA GeForce RTX 4090 Laptop GPU"


def _fake_smi(monkeypatch, *, driver_query, header, gpu_name=_DEFAULT_GPU_NAME):
    """Monkeypatch subprocess.run so detect_nvidia sees a fixed nvidia-smi.

    ``driver_query`` is the DRIVER VERSION the ``--query-gpu=driver_version,name``
    probe should report (None = the query fails / returns nothing). The fake
    builds the real ``"<driver>, <name>"`` CSV row so the name-parse path is
    exercised. ``header`` is the no-flag header stdout (for the CUDA-major parse).
    """

    class _Proc:
        def __init__(self, stdout, rc=0):
            self.stdout = stdout
            self.returncode = rc

    def _run(cmd, *a, **k):
        # cmd[0] is the resolved nvidia-smi exe; a "--query-gpu=..." arg means
        # the stable driver+name probe, otherwise it's the free-form header call.
        if any(isinstance(c, str) and c.startswith("--query-gpu") for c in cmd):
            if driver_query is None:
                return _Proc("", rc=1)
            drv = driver_query.strip()
            row = f"{drv}, {gpu_name}\n" if gpu_name else f"{drv}\n"
            return _Proc(row, rc=0)
        return _Proc(header, rc=0)

    monkeypatch.setattr(gpu_bootstrap, "_resolve_nvidia_smi", lambda: "nvidia-smi")
    monkeypatch.setattr(gpu_bootstrap.subprocess, "run", _run)


def test_detect_nvidia_parses_legacy_header(monkeypatch):
    """Legacy driver header (Driver Version: / CUDA Version:) still parses."""
    _fake_smi(monkeypatch, driver_query="555.52\n", header=_SMI_HEADER_LEGACY)
    got = gpu_bootstrap.detect_nvidia()
    assert got == {"driver_version": "555.52", "cuda_major": 12, "gpu_name": "NVIDIA GeForce RTX 4090 Laptop GPU"}


def test_detect_nvidia_parses_new_610_header(monkeypatch):
    """Driver 610+ header (CUDA UMD Version:, no legacy strings) — THE bug.

    Before the fix detect_nvidia returned None here and the RTX 4090 ran on CPU.
    """
    _fake_smi(monkeypatch, driver_query="610.47\n", header=_SMI_HEADER_NEW_610)
    got = gpu_bootstrap.detect_nvidia()
    assert got == {"driver_version": "610.47", "cuda_major": 13, "gpu_name": "NVIDIA GeForce RTX 4090 Laptop GPU"}


def test_detect_nvidia_falls_back_to_driver_branch_when_no_cuda_field(monkeypatch):
    """GPU present but header has NEITHER CUDA field -> driver-branch fallback
    picks the CUDA major rather than silently dropping a visible GPU to CPU."""
    headerless = (
        "Thu Jun  4 02:47:53 2026\n"
        "| NVIDIA-SMI 612.00   KMD Version: 612.00   (no cuda field at all) |\n"
    )
    _fake_smi(monkeypatch, driver_query="612.00\n", header=headerless)
    got = gpu_bootstrap.detect_nvidia()
    # 612 >= 580 -> CUDA 13 by the driver-branch table.
    assert got == {"driver_version": "612.00", "cuda_major": 13, "gpu_name": "NVIDIA GeForce RTX 4090 Laptop GPU"}


def test_detect_nvidia_none_when_query_driver_fails(monkeypatch):
    """If the stable --query-gpu driver probe yields nothing, there's no usable
    GPU -> None (even if a stale header string is somehow present)."""
    _fake_smi(monkeypatch, driver_query=None, header=_SMI_HEADER_NEW_610)
    assert gpu_bootstrap.detect_nvidia() is None


def test_detect_nvidia_gpu_present_unknown_cuda_returns_none_major(monkeypatch):
    """GPU present, no CUDA field, and a driver branch BELOW the fallback floor
    -> cuda_major None (resolve_torch_mode then treats it as CPU). Never None
    for the whole dict — we still record the driver so the GPU isn't 'lost'."""
    old_driver = (
        "Tue May 13 10:00:00 2020\n"
        "| NVIDIA-SMI 440.33   (ancient driver, no cuda string)            |\n"
    )
    _fake_smi(monkeypatch, driver_query="440.33\n", header=old_driver)
    got = gpu_bootstrap.detect_nvidia()
    assert got == {"driver_version": "440.33", "cuda_major": None, "gpu_name": "NVIDIA GeForce RTX 4090 Laptop GPU"}


def test_parse_smi_header_cuda_major_prefers_umd_over_legacy():
    """If BOTH fields somehow appear, the NEW 'CUDA UMD Version:' wins (it's the
    authoritative runtime field on the new layout)."""
    both = "CUDA Version: 12.4   CUDA UMD Version: 13.3"
    assert gpu_bootstrap._parse_smi_header_cuda_major(both) == 13


def test_install_cupy_surfaces_pip_error_lines(monkeypatch):
    """Code-review MEDIUM (PR #54): a failed pip install must report pip's
    own ERROR: line, not a blind tail that often captures only the generic
    hint block."""
    class _Proc:
        returncode = 1
        stdout = (
            "Collecting cupy-cuda12x\n"
            "ERROR: Could not find a version that satisfies the requirement "
            "cupy-cuda12x\n"
            "ERROR: No matching distribution found for cupy-cuda12x\n"
            "\n[hint] try upgrading pip and rerun the command shown above\n"
        )
        stderr = ""

    # v2.17: install_cupy now runs through _run_pip_with_heartbeat (Popen-based,
    # for the elapsed-time heartbeat on multi-GB CUDA-wheel downloads), so mock
    # THAT rather than subprocess.run. It returns a _PipResult with the same
    # returncode/stdout/stderr attrs the failure-detail extractor reads.
    monkeypatch.setattr(
        gpu_bootstrap, "_run_pip_with_heartbeat", lambda *a, **k: _Proc()
    )
    ok, msg = gpu_bootstrap.install_cupy("python", 12)
    assert ok is False
    assert "Could not find a version" in msg
    assert "No matching distribution" in msg


def test_load_stamp_rejects_non_dict_json():
    """gemini MEDIUM (PR #54): a corrupted/hand-edited stamp that is valid
    JSON but not a dict (e.g. a list or string) must be treated as 'no
    stamp' — returning it would crash later .get(...) callers."""
    for payload in ("[]", '"oops"', "42", "null"):
        gpu_bootstrap.STAMP_PATH.write_text(payload, encoding="utf-8")
        assert gpu_bootstrap._load_stamp() is None, (
            f"non-dict stamp {payload!r} must load as None"
        )
    # Sanity: a real dict still round-trips.
    gpu_bootstrap.STAMP_PATH.write_text('{"result": "no_nvidia"}', encoding="utf-8")
    assert gpu_bootstrap._load_stamp() == {"result": "no_nvidia"}


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
    # v2.17: pinned to the CuPy 13.x line (14.x needs numpy>=2, conflicts with
    # our numpy<2 face stack). Assert the package family + the pin, not a bare
    # exact string, so the version cap is locked too.
    assert payload["cupy_package"] == gpu_bootstrap._CUDA_TO_CUPY[12]
    assert payload["cupy_package"].startswith("cupy-cuda12x")
    assert "[ctk]" not in payload["cupy_package"]
    assert ">=13.6,<14" in payload["cupy_package"]
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


def test_stale_gpu_ready_with_broken_nvrtc_re_enters_install(monkeypatch):
    """THE friend-fix idempotence guard (Plan-agent §1).

    The friend has a STALE ``gpu_ready`` stamp from his broken v2.17 install
    (his OLD probe passed because it never compiled a kernel — nvrtc was never
    there). The ONLY thing that dislodges that stale stamp and reinstalls the
    now-explicit nvidia wheels is the HONEST re-probe returning None. Prove
    that a gpu_ready stamp + a failing probe re-enters detect_nvidia +
    install_cupy rather than trusting the stamp. Without this, the fix would
    NEVER fire for the friend (he'd stay on CPU forever).
    """
    gpu_bootstrap._write_stamp({
        "result": "gpu_ready",
        "checked_at": _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "driver_version": "576.80", "cuda_major": 12,
        "cupy_package": "cupy-cuda12x>=13.6,<14", "cupy_version": "13.6.0",
        "attempts": 0,
    })
    # Honest probe FAILS (nvrtc unloadable on his broken install).
    monkeypatch.setattr(gpu_bootstrap, "probe_cupy", lambda exe: None)
    monkeypatch.setattr(
        gpu_bootstrap, "detect_nvidia",
        lambda: {"driver_version": "576.80", "cuda_major": 12},
    )
    install_called = []
    monkeypatch.setattr(
        gpu_bootstrap, "install_cupy",
        lambda exe, major: install_called.append((exe, major)) or (True, "13.6.0"),
    )
    result = gpu_bootstrap.bootstrap("python_friend")
    assert result == "gpu_installed_now", (
        "stale gpu_ready + failing probe MUST reinstall, not trust the stamp"
    )
    assert install_called == [("python_friend", 12)], (
        "the nvidia-wheel reinstall must actually fire for the friend"
    )


def test_capped_install_failed_from_older_installer_re_attempts(monkeypatch):
    """Codex P2 PR #72: a user who exhausted the 3-attempt install_failed cap on
    an OLDER installer (e.g. the broken `[ctk]` one) must get a fresh set of
    attempts after the installer is fixed — otherwise bootstrap() returns at the
    capped-stamp check and the fix never runs for the stranded-upgrade case."""
    # Write the stale stamp DIRECTLY to disk (not via _write_stamp, which now
    # stamps the CURRENT installer_version) — this is what an old-installer stamp
    # actually looks like on the friend's box.
    gpu_bootstrap.STAMP_PATH.write_text(json.dumps({
        "result": "install_failed",
        "checked_at": _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "driver_version": "576.80", "cuda_major": 12,
        "cupy_package": "cupy-cuda12x[ctk]>=13.6,<14", "cupy_version": None,
        "attempts": 3, "installer_version": "2.17.0",  # the OLD broken installer
    }), encoding="utf-8")
    # Current installer is newer (2.17.1) — the cap must be ignored.
    monkeypatch.setattr(gpu_bootstrap, "INSTALLER_VERSION", "2.17.1")
    monkeypatch.setattr(
        gpu_bootstrap, "detect_nvidia",
        lambda: {"driver_version": "576.80", "cuda_major": 12},
    )
    install_called = []
    monkeypatch.setattr(
        gpu_bootstrap, "install_cupy",
        lambda exe, major: install_called.append((exe, major)) or (True, "13.6.0"),
    )
    result = gpu_bootstrap.bootstrap("python_friend")
    assert result == "gpu_installed_now", (
        "a capped failure from an OLDER installer must re-attempt, not stay capped"
    )
    assert install_called == [("python_friend", 12)]


def test_capped_install_failed_same_installer_stays_capped(monkeypatch):
    """The cap MUST still hold when the failures came from the CURRENT installer
    (don't turn the cap into a no-op — that would re-introduce the 3x-per-launch
    log spam the cap was added to prevent)."""
    # Monkeypatch the constant BEFORE _write_stamp — the stamp writer forcibly
    # overwrites installer_version with the live INSTALLER_VERSION (gpu_bootstrap
    # ~line 339), so the stamp only ends up "SAME as current" if the constant is
    # already lowered when the stamp is written. (Reordering this is what keeps
    # the test robust against future INSTALLER_VERSION bumps.)
    monkeypatch.setattr(gpu_bootstrap, "INSTALLER_VERSION", "2.17.1")
    gpu_bootstrap._write_stamp({
        "result": "install_failed",
        "checked_at": _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "driver_version": "576.80", "cuda_major": 12,
        "cupy_package": "cupy-cuda12x>=13.6,<14", "cupy_version": None,
        "attempts": 3,  # installer_version auto-stamped to the (patched) current
    })
    install_called = []
    monkeypatch.setattr(
        gpu_bootstrap, "install_cupy",
        lambda exe, major: install_called.append((exe, major)) or (True, "x"),
    )
    result = gpu_bootstrap.bootstrap("python_unused")
    assert result == "install_failed"
    assert install_called == [], "cap must hold for same-installer failures"


def test_write_stamp_records_installer_version():
    """Every stamp must carry installer_version so the capped-failure reset
    (Codex P2 PR #72) can tell which installer produced it."""
    gpu_bootstrap._write_stamp({"result": "no_nvidia", "cuda_major": None})
    payload = json.loads(gpu_bootstrap.STAMP_PATH.read_text())
    assert payload["installer_version"] == gpu_bootstrap.INSTALLER_VERSION


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


def test_write_stamp_is_atomic_and_leaves_no_temp(_isolated_stamp_dir):
    """Gemini MEDIUM (PR #54 round 14b): the stamp write must be atomic
    (temp + os.replace) so concurrent launchers on a non-NVIDIA host —
    which write the stamp OUTSIDE the lock — can't leave a torn/corrupt
    JSON. After a successful write the final path holds valid JSON AND no
    ``.tmp.*`` orphan is left in the state dir."""
    gpu_bootstrap._write_stamp({"result": "cuda_ready", "attempts": 1})
    payload = json.loads(gpu_bootstrap.STAMP_PATH.read_text(encoding="utf-8"))
    assert payload["result"] == "cuda_ready"
    leftovers = list(gpu_bootstrap.STATE_DIR.glob("gpu_status.tmp.*"))
    assert leftovers == [], (
        f"atomic write must not leave a temp orphan; found {leftovers}"
    )


def test_write_stamp_replace_failure_cleans_temp(monkeypatch, _isolated_stamp_dir):
    """If os.replace fails (e.g. the dir goes read-only between the temp
    write and the rename), _write_stamp must NOT raise AND must not leave
    the PID-temp behind for the next launch to trip over."""
    def _boom(src, dst):
        raise PermissionError("simulated replace failure")
    monkeypatch.setattr(gpu_bootstrap.os, "replace", _boom)
    # Must not raise.
    gpu_bootstrap._write_stamp({"result": "no_nvidia"})
    leftovers = list(gpu_bootstrap.STATE_DIR.glob("gpu_status.tmp.*"))
    assert leftovers == [], (
        f"a failed replace must still clean its temp; found {leftovers}"
    )


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


def test_probe_cupy_parses_sentinel_line_despite_trailing_noise(monkeypatch):
    """Code-review MEDIUM (PR #54): probe_cupy must read the CUPYVER=
    sentinel line, not blindly take the last stdout line. CuPy's first
    device init (or any import-chain library) can print notices AFTER the
    version, which the old ``splitlines()[-1]`` would have stamped as the
    'version'. Simulate a trailing warning and assert the real version
    is still extracted."""
    import types

    def _fake_run(*args, **kwargs):
        return types.SimpleNamespace(
            returncode=0,
            stdout="CUPYVER=13.3.0\nUserWarning: cuDNN JIT cache warming...\n",
            stderr="",
        )

    monkeypatch.setattr(gpu_bootstrap.subprocess, "run", _fake_run)
    assert gpu_bootstrap.probe_cupy("python_unused") == "13.3.0"


def test_probe_cupy_returns_none_when_sentinel_absent(monkeypatch):
    """If the probe somehow emits no CUPYVER= line (crash before print,
    truncated output), probe_cupy returns None rather than a junk string,
    so the caller correctly treats the install as not-verified."""
    import types

    def _fake_run(*args, **kwargs):
        return types.SimpleNamespace(
            returncode=0, stdout="some unexpected chatter\n", stderr="")

    monkeypatch.setattr(gpu_bootstrap.subprocess, "run", _fake_run)
    assert gpu_bootstrap.probe_cupy("python_unused") is None


# ---------------------------------------------------------------------------
# v2.17: torch hardware selection (resolve_torch_mode is pure -> easy to test)
# ---------------------------------------------------------------------------
def test_resolve_torch_mode_macos_never_cuda():
    """macOS must NEVER select CUDA, even if a (bogus) nvidia dict is passed —
    resolve_torch_mode hard-returns mac_default before looking at nvidia."""
    d = gpu_bootstrap.resolve_torch_mode(
        platform_is_darwin=True,
        nvidia={"cuda_major": 12, "driver_version": "555"},
    )
    assert d["mode"] == "mac_default"
    assert d["index_url"] is None


def test_resolve_torch_mode_nvidia_12_selects_cuda():
    d = gpu_bootstrap.resolve_torch_mode(
        platform_is_darwin=False,
        nvidia={"cuda_major": 12, "driver_version": "555"},
    )
    assert d["mode"] == "cuda"
    assert d["index_url"] == gpu_bootstrap._TORCH_CUDA_INDEX[12]
    assert d["extra_index_url"] == gpu_bootstrap._PYPI_INDEX_URL


def test_resolve_torch_mode_nvidia_13_selects_cuda():
    d = gpu_bootstrap.resolve_torch_mode(
        platform_is_darwin=False,
        nvidia={"cuda_major": 13, "driver_version": "580"},
    )
    assert d["mode"] == "cuda"
    assert d["index_url"] == gpu_bootstrap._TORCH_CUDA_INDEX[13]


def test_resolve_torch_mode_unsupported_cuda_major_falls_back_to_cpu():
    """An NVIDIA box with a CUDA major we don't ship a torch index for (e.g.
    11.x or a future 14.x) must fall back to CPU torch, not crash."""
    d = gpu_bootstrap.resolve_torch_mode(
        platform_is_darwin=False,
        nvidia={"cuda_major": 11, "driver_version": "470"},
    )
    assert d["mode"] == "cpu"
    assert d["index_url"] == gpu_bootstrap._TORCH_CPU_INDEX_URL


def test_resolve_torch_mode_no_nvidia_selects_cpu():
    d = gpu_bootstrap.resolve_torch_mode(platform_is_darwin=False, nvidia=None)
    assert d["mode"] == "cpu"
    assert d["index_url"] == gpu_bootstrap._TORCH_CPU_INDEX_URL


def test_torch_cuda_index_keys_match_cupy_keys():
    """Drift guard (review feedback 2026-06-02): torch + CuPy must agree on
    which CUDA majors are GPU-supported. If one map gains/loses a major
    without the other, GPU detection becomes inconsistent."""
    assert set(gpu_bootstrap._TORCH_CUDA_INDEX) == set(gpu_bootstrap._CUDA_TO_CUPY)


def test_torch_cuda_index_urls_are_well_formed_pytorch_tags():
    """Drift guard: each value must be a real pytorch.org/whl/cuNNN index URL,
    NOT a tag inferred arithmetically from the CUDA major. A typo / bad tag
    fails CI here instead of silently producing a 404 install at runtime."""
    import re

    pat = re.compile(r"^https://download\.pytorch\.org/whl/cu\d{3}$")
    for major, url in gpu_bootstrap._TORCH_CUDA_INDEX.items():
        assert pat.match(url), f"CUDA {major} -> {url!r} is not a whl/cuNNN URL"


def test_compute_stamp_token_deterministic_and_mode_sensitive(monkeypatch, tmp_path):
    """The stamp token must be deterministic for a fixed env and must change
    when the resolved torch mode changes (so adding/removing a GPU invalidates
    the dep stamp).

    Pinned to ``sys.platform = "win32"`` so the test runs identically on every
    OS. On darwin, ``resolve_torch_mode`` short-circuits to ``mac_default``
    before looking at ``nvidia`` (Apple Silicon has no CUDA path), so without
    the pin the t1==t3 case would equal on a Mac dev box — the assertion
    `t3 != t1` would fail spuriously even though the production-Windows
    behaviour is correct. The pin keeps this a Windows-semantics test that
    runs everywhere.
    """
    constraints = tmp_path / "constraints.txt"
    constraints.write_text("numpy>=1.26,<2\n", encoding="utf-8")

    # Patch sys.platform via its dotted path rather than via the module's
    # `gpu_bootstrap.sys` attribute — the former is decoupled from the
    # module's internal import style. If gpu_bootstrap.py is ever
    # refactored to `from sys import platform`, the attribute-based form
    # silently fails or AttributeErrors; the dotted-path form keeps working.
    # (Gemini round-3 review on PR #79.)
    monkeypatch.setattr("sys.platform", "win32")
    # Force a no-cache path so it uses detect_nvidia, then monkeypatch that.
    monkeypatch.setattr(gpu_bootstrap, "_load_stamp", lambda: None)
    monkeypatch.setattr(gpu_bootstrap, "detect_nvidia", lambda: None)
    t1 = gpu_bootstrap.compute_stamp_token(str(constraints))
    t2 = gpu_bootstrap.compute_stamp_token(str(constraints))
    assert t1 == t2  # deterministic

    monkeypatch.setattr(
        gpu_bootstrap, "detect_nvidia",
        lambda: {"cuda_major": 12, "driver_version": "555"},
    )
    t3 = gpu_bootstrap.compute_stamp_token(str(constraints))
    assert t3 != t1  # GPU appeared -> token changes -> stamp invalidates
    assert gpu_bootstrap.INSTALLER_VERSION in t1


def test_compute_stamp_token_prefers_cached_gpu_status(monkeypatch, tmp_path):
    """On the hot path the token reads cuda_major from the cached gpu_status
    stamp (cheap) instead of running nvidia-smi every launch.

    Pinned to ``sys.platform = "win32"`` — same reasoning as the test above:
    on darwin ``resolve_torch_mode`` short-circuits to ``mac_default`` and
    drops the cached cuda_major, so the "cuda" / "13" tokens would never
    appear on a Mac dev box. The pin keeps the test a Windows-semantics
    invariant that runs portably.
    """
    constraints = tmp_path / "constraints.txt"
    constraints.write_text("x\n", encoding="utf-8")

    monkeypatch.setattr("sys.platform", "win32")  # dotted-path: see test above.
    monkeypatch.setattr(
        gpu_bootstrap, "_load_stamp",
        lambda: {"result": "gpu_ready", "cuda_major": 13},
    )

    def _boom():
        raise AssertionError("detect_nvidia must NOT run when stamp is cached")

    monkeypatch.setattr(gpu_bootstrap, "detect_nvidia", _boom)
    token = gpu_bootstrap.compute_stamp_token(str(constraints))
    assert "cuda" in token and "13" in token


def test_compute_stamp_token_on_darwin_always_mac_default(monkeypatch, tmp_path):
    """Darwin-specific token guard: on macOS the token must ALWAYS encode
    ``mac_default`` and ``None`` for cuda_major in its fixed-shape positions,
    regardless of cached gpu_status or live detect_nvidia. So a Mac dev box
    that touched a Windows venv's gpu_status.json can't accidentally pick
    up a CUDA dep stamp.

    The token shape is ``INSTALLER_VERSION-sys.platform-mode-cuda_major-sha12``
    (see scripts/gpu_bootstrap.py::compute_stamp_token). We assert on the
    PARSED parts (positions 2 and 3) rather than substring search — a naive
    ``"cuda" not in token`` check was too broad (an installer version of
    13.x or a constraints sha containing "13" would fail it spuriously
    even though production behaviour is correct — code review M3,
    2026-06-06).

    Mirrors the existing ``test_resolve_torch_mode_macos_never_cuda`` rule
    one layer up the call stack.
    """
    constraints = tmp_path / "constraints.txt"
    constraints.write_text("numpy>=1.26,<2\n", encoding="utf-8")

    monkeypatch.setattr("sys.platform", "darwin")
    # Even if a CUDA-13 cache exists (e.g. from a synced .venv on a Windows
    # box), darwin must never roll it into the token.
    monkeypatch.setattr(
        gpu_bootstrap, "_load_stamp",
        lambda: {"result": "gpu_ready", "cuda_major": 13},
    )
    monkeypatch.setattr(
        gpu_bootstrap, "detect_nvidia",
        lambda: {"cuda_major": 13, "driver_version": "555"},
    )
    token = gpu_bootstrap.compute_stamp_token(str(constraints))
    parts = token.split("-")
    # Shape: [INSTALLER_VERSION, sys.platform, mode, cuda_major, sha12]
    assert len(parts) == 5, (
        f"darwin token has unexpected shape (expected 5 dash-parts): {token!r}"
    )
    assert parts[1] == "darwin", f"darwin token sys.platform wrong: {token!r}"
    assert parts[2] == "mac_default", (
        f"darwin token mode is {parts[2]!r}, expected 'mac_default': {token!r}"
    )
    assert parts[3] == "None", (
        f"darwin token cuda_major is {parts[3]!r}, expected 'None': {token!r}"
    )


def test_run_pip_with_heartbeat_returns_pipresult_and_no_early_beat(capsys):
    """v2.17: a FAST command must complete cleanly via the heartbeat wrapper
    and NOT print a heartbeat line (first beat is at 20s). Returns a
    _PipResult with the captured stdout."""
    import sys as _sys

    r = gpu_bootstrap._run_pip_with_heartbeat(
        [_sys.executable, "-c", "print('hello-heartbeat')"],
        timeout=30,
        label="unit",
    )
    assert r.returncode == 0
    assert "hello-heartbeat" in r.stdout
    out = capsys.readouterr().out
    assert "still running" not in out, "fast command must not emit a heartbeat"


def test_run_pip_with_heartbeat_handles_bad_command():
    """A non-existent executable must return a _PipResult(returncode!=0), not
    raise — the launcher must never crash on a pip subprocess error."""
    r = gpu_bootstrap._run_pip_with_heartbeat(
        ["this_executable_does_not_exist_xyz"], timeout=10, label="bad"
    )
    assert r.returncode != 0


def test_cupy_pinned_to_numpy1_compatible_line():
    """v2.17 (verified 2026-06-03): CuPy 14.x requires numpy>=2.0 and FAILS to
    import under our numpy<2 face-stack pin. Both CuPy specs MUST pin the 13.x
    line (>=13.6,<14) — the last numpy-1.x-compatible release. If this drifts to
    14.x, rPPG GPU silently breaks (cupy import fails -> CPU fallback)."""
    for major in (12, 13):
        spec = gpu_bootstrap._CUDA_TO_CUPY[major]
        assert "<14" in spec, f"CUDA {major}: CuPy must be pinned <14 (numpy<2): {spec!r}"
        assert ">=13.6" in spec, f"CUDA {major}: expected >=13.6 floor: {spec!r}"
        # `[ctk]` is GONE (no-op on 13.6.0) — the nvidia component wheels ship
        # explicitly now (see test_nvidia_component_wheels_*).
        assert "[ctk]" not in spec, f"CUDA {major}: [ctk] is a no-op, must be dropped: {spec!r}"


def test_pip_install_timeout_covers_large_cuda_download():
    """The CuPy + nvidia component download is ~1.5-2.5GB; the old 900s cap
    timed out on a real box -> install_failed -> CPU. Timeout must be generous,
    and LOCK_STALE must still exceed it (so a live install isn't force-broken)."""
    assert gpu_bootstrap.PIP_INSTALL_TIMEOUT_SECONDS >= 1800, "too short for 1.5-2.5GB CUDA set"
    assert gpu_bootstrap.LOCK_STALE_SECONDS > gpu_bootstrap.PIP_INSTALL_TIMEOUT_SECONDS


def test_nvidia_component_wheels_present_per_cuda_major():
    """The explicit nvidia-* component wheels (the REAL replacement for the
    no-op [ctk]) must exist for both CUDA majors: 12 (-cu12 suffixed) and 13
    (un-suffixed). Without nvrtc, cupy imports but can't compile a kernel ->
    rPPG silently runs on CPU (the friend's 20-min/iter bug)."""
    wheels = gpu_bootstrap._CUDA_TO_NVIDIA_WHEELS
    assert set(wheels) == {12, 13}
    # nvrtc is the load-bearing one — assert it's present in both.
    assert any("nvidia-cuda-nvrtc" in w for w in wheels[12])
    assert any("nvidia-cuda-nvrtc" in w for w in wheels[13])
    # cu12 set is ALL -cu12 suffixed; cu13 set is NONE -cu12 suffixed.
    assert all("-cu12" in w for w in wheels[12]), wheels[12]
    assert all("-cu12" not in w for w in wheels[13]), wheels[13]
    # Both sets cover the 8 components cupy dispatches to.
    for major in (12, 13):
        names = " ".join(wheels[major])
        for comp in ("nvrtc", "runtime", "cublas", "cufft",
                     "curand", "cusolver", "cusparse", "nvjitlink"):
            assert comp in names, f"CUDA {major} missing nvidia-*{comp}*"


def test_nvidia_wheel_specs_parity_with_pyproject_extras():
    """The pip-path nvidia wheel specs MUST equal the uv pyproject.toml
    cu121/cu128 extras (modulo the `; sys_platform` marker) so the two install
    paths can't silently drift. Skips gracefully if pyproject.toml is absent
    (this test ships on the pip-only main branch where it lives at repo root
    only on the uv branch)."""
    import os

    repo_root = os.path.dirname(os.path.dirname(os.path.abspath(gpu_bootstrap.__file__)))
    pyproject = os.path.join(repo_root, "pyproject.toml")
    if not os.path.isfile(pyproject):
        import pytest
        pytest.skip("pyproject.toml absent (pip-only branch) — parity checked on uv branch")

    # Parse with tomllib (stdlib >=3.11, which this project requires) rather than
    # a hand-rolled regex — the regex form was fragile to formatting/whitespace
    # drift in the extras block (code-review MEDIUM, PR #73).
    import tomllib
    with open(pyproject, "rb") as fh:
        data = tomllib.load(fh)
    extras = data.get("project", {}).get("optional-dependencies", {})

    def _extra_nvidia(extra_name):
        deps = extras.get(extra_name)
        assert deps, f"extra {extra_name} not found in pyproject.toml [project.optional-dependencies]"
        specs = set()
        for dep in deps:
            s = dep.split(";")[0].strip()  # drop the ; sys_platform marker
            if s.startswith("nvidia-"):
                specs.add(s)
        assert specs, f"no nvidia-* specs in extra {extra_name}"
        return specs

    # cu121 extra <-> CUDA 12; cu128 extra <-> CUDA 13.
    assert _extra_nvidia("cu121") == set(gpu_bootstrap._CUDA_TO_NVIDIA_WHEELS[12])
    assert _extra_nvidia("cu128") == set(gpu_bootstrap._CUDA_TO_NVIDIA_WHEELS[13])


def test_log_survives_none_stdout_pythonw(monkeypatch):
    """Under pythonw.exe (GUI, no console) sys.stdout is None. gpu_bootstrap._log
    must NOT raise — a bare print() would AttributeError and silently crash the
    GPU bootstrap daemon thread → no GPU init ever (gemini HIGH, PR #73)."""
    import io
    monkeypatch.setattr(sys, "stdout", None)
    gpu_bootstrap._log("msg under pythonw")  # must not raise
    # And with a real stdout it still writes.
    buf = io.StringIO()
    monkeypatch.setattr(sys, "stdout", buf)
    gpu_bootstrap._log("hello")
    assert "GPU: hello" in buf.getvalue()
