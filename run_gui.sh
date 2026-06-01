#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PYTHON_BIN="${ROOT_DIR}/.venv-macos/bin/python"
REQUIREMENTS_STAMP="${ROOT_DIR}/.venv-macos/.requirements.sha256"
HEALTH_STAMP="${ROOT_DIR}/.venv-macos/.health.sha256"

# Release version banner. Parsed from app_version.py text (no Python needed,
# so it prints even on a fresh Mac before the venv exists). Single source of
# truth: app_version.RELEASE_VERSION — the same constant the GUI chip, the
# Windows launcher banner, and the release-zip name all read. `|| true`
# keeps a parse miss from tripping `set -e`.
APP_VER="$(sed -n 's/^RELEASE_VERSION[[:space:]]*=[[:space:]]*"\(.*\)".*/\1/p' \
  "${ROOT_DIR}/app_version.py" 2>/dev/null | head -1 || true)"
[[ -n "${APP_VER:-}" ]] || APP_VER="unknown"
printf '\n'
printf '  ============================================================\n'
printf '   Ultimate-Selfie-Gen  %s  --  GUI Launcher\n' "${APP_VER}"
printf '  ============================================================\n\n'

# ---------------------------------------------------------------------------
# PR #49: bootstrap mutex. Two concurrent launches must not run setup_macos.sh
# (venv create + pip install) at the same time — they corrupt the shared venv.
# Held only across the dep-setup block below; released BEFORE exec'ing the
# GUI, so multiple GUI processes can run concurrently after bootstrap.
# ---------------------------------------------------------------------------
LOCK_DIR="${ROOT_DIR}/.launcher_state"
LOCK_PATH="${LOCK_DIR}/setup.lock"
LOCK_STALE_SECONDS=600   # 10 min — conservative; most dep installs <3 min
mkdir -p "${LOCK_DIR}"

_lock_acquired=0
_lock_waited=0
while [[ ${_lock_acquired} -eq 0 ]]; do
  if mkdir "${LOCK_PATH}" 2>/dev/null; then
    _lock_acquired=1
    break
  fi
  # Check for stale lock (kill -9'd sibling or crashed dep install)
  if [[ -d "${LOCK_PATH}" ]]; then
    if command -v stat >/dev/null 2>&1; then
      _now="$(date +%s)"
      # macOS stat: -f %m; GNU stat: -c %Y. Try both.
      _mtime="$(stat -f %m "${LOCK_PATH}" 2>/dev/null || stat -c %Y "${LOCK_PATH}" 2>/dev/null || echo "${_now}")"
      _age=$(( _now - _mtime ))
      if (( _age > LOCK_STALE_SECONDS )); then
        printf '[setup-lock] removing stale lock (age=%ss)\n' "${_age}" >&2
        rmdir "${LOCK_PATH}" 2>/dev/null || true
        continue
      fi
    fi
  fi
  if [[ ${_lock_waited} -eq 0 ]]; then
    printf '[setup-lock] another launcher is running dependency setup; waiting...\n' >&2
    _lock_waited=1
  fi
  sleep 2
done
trap 'rmdir "${LOCK_PATH}" 2>/dev/null || true' EXIT

# Heavy-install user banner — only shown when setup is about to do a full
# install (no REQUIREMENTS_STAMP yet). Mirrors the same banner pattern in
# the Windows BAT. Sets expectations so users don't kill the process
# thinking it's frozen during the multi-GB CUDA torch download.
if [[ ! -f "${REQUIREMENTS_STAMP}" ]]; then
  printf '\n'
  printf '  ============================================================\n'
  printf '   FIRST-RUN DEP INSTALL -- expect 5 to 15 minutes\n'
  printf '  ============================================================\n'
  printf '   - torch wheels: ~2GB (CPU build on macOS; no CUDA)\n'
  printf '   - tensorflow + mediapipe + opencv: ~1-2GB more\n'
  printf '   - subsequent launches skip this entire block\n'
  printf '   - pip will print progress below; if 60+ sec of silence,\n'
  printf '     check your network or Ctrl+C and re-run.\n'
  printf '  ============================================================\n\n'
  if [[ -d "${LOCK_DIR}" ]]; then
    printf '[%s] dep-install banner shown (first-run path)\n' \
      "$(date '+%Y-%m-%d %H:%M:%S')" >> "${LOCK_DIR}/launch.log"
  fi
fi

# setup_macos.sh installs deps and writes REQUIREMENTS_STAMP when they change
"${ROOT_DIR}/setup_macos.sh"
export KLING_SKIP_PY_STARTUP_DEP_CHECK=1

# Per-launch diagnostic snapshot — writes Python / pip / OS info to the
# launch log so users have something to attach when reporting issues.
# Mirrors the equivalent block in launchers/windows/run_gui.bat. macOS
# omits GPU because Apple Silicon / Intel Macs don't ship with
# nvidia-smi; CUDA isn't a concern on this platform.
if [[ -d "${LOCK_DIR}" && -x "${PYTHON_BIN}" ]]; then
  DIAG_PY="$("${PYTHON_BIN}" -V 2>&1 || echo unknown)"
  DIAG_PIP="$("${PYTHON_BIN}" -m pip --version 2>&1 | head -1 || echo unknown)"
  DIAG_OS="$(uname -a 2>&1 || echo unknown)"
  DIAG_TS="$(date '+%Y-%m-%d %H:%M:%S')"
  {
    printf '[%s] diag-py %s\n' "${DIAG_TS}" "${DIAG_PY}"
    printf '[%s] diag-pip %s\n' "${DIAG_TS}" "${DIAG_PIP}"
    printf '[%s] diag-os %s\n' "${DIAG_TS}" "${DIAG_OS}"
  } >> "${LOCK_DIR}/launch.log" 2>/dev/null || true
fi

# Runtime dependency health check.
#
# Behavior contract (PR fix/windows-tf-health-check-and-error-msg):
#   - Probe runtime health on EVERY launch (~3-5s overhead). The previous
#     shape — skip when REQUIREMENTS_STAMP == HEALTH_STAMP — could lock in
#     a broken install: once the stamps match, a subsequent failure (e.g.
#     a TF DLL the OS quarantined, a venv corrupted by `rm -rf` mid-pip)
#     was never detected. Cheap continuous validation beats stamp-based
#     skipping for a stack this fragile.
#   - On failure: ALWAYS clear the HEALTH_STAMP first so a repair-then-
#     fail leaves a "needs re-check" signal for the next launch. The
#     previous shape rewrote the stamp unconditionally, masking failures.
#   - On repair failure: emit an actionable, copy-pasteable recovery
#     hint, write the diagnostic to .launcher_state/last_health.log,
#     and exit non-zero. Do not silently launch the GUI into a state
#     that will produce "Run run_gui.bat for automatic dependency
#     repair" toasts in the in-GUI log — that toast (face_crop_tab.py
#     ~L1330) creates an infinite re-run loop with no recovery action.
LAUNCH_DIAG_LOG="${LOCK_DIR}/launch.log"
LAUNCH_TS="$(date '+%Y-%m-%d %H:%M:%S')"
HEALTH_OUTPUT_LOG="${LOCK_DIR}/last_health.log"

# Tk availability guard MUST run BEFORE the dependency health probe.
# Codex PR #55 round-2 P2 (×2): on macOS/Homebrew Python builds without
# `_tkinter`, `dependency_health_check.py --mode check` reaches
# `_default_retinaface_runtime_probe()` which imports
# `kling_gui.tabs.face_crop_tab` → which top-level imports `tkinter` →
# ImportError. The launcher then runs futile face-stack repair, exits
# through the generic recovery branch, and the user never sees the
# actionable `brew install python-tk@X.Y` message that the Tk check
# below produces. Move the Tk probe ahead of the health block.
if ! "${PYTHON_BIN}" -c 'import tkinter' >/dev/null 2>&1; then
  VERSION="$("${PYTHON_BIN}" -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')"
  printf '[%s] tk-probe FAIL (no _tkinter); exiting before health check\n' "${LAUNCH_TS}" >> "${LAUNCH_DIAG_LOG}" 2>/dev/null || true
  printf 'GUI launch blocked: this Python environment does not provide Tk support.\n\n' >&2
  printf 'If you are using Homebrew Python, install the matching Tk package and recreate the virtual environment:\n' >&2
  printf '  brew install python-tk@%s\n\n' "${VERSION}" >&2
  printf 'Then rerun ./run_gui.sh.\n' >&2
  rmdir "${LOCK_PATH}" 2>/dev/null || true
  trap - EXIT
  exit 1
fi

# Auto-detect NVIDIA + bootstrap CuPy. On Darwin nvidia-smi is absent
# so the script short-circuits to "no_nvidia" instantly and stamps it
# (CuPy has no Metal backend — Apple Silicon stays CPU). On a CUDA
# Linux host it installs cupy-cuda12x[ctk] / cupy-cuda13x[ctk] once.
# Idempotent + cached via .launcher_state/gpu_status.json. Never
# blocks launch on failure (script always exits 0). User opt-out:
#     export KLING_SKIP_GPU_BOOTSTRAP=1
if [[ -f "${ROOT_DIR}/scripts/gpu_bootstrap.py" ]]; then
  "${PYTHON_BIN}" "${ROOT_DIR}/scripts/gpu_bootstrap.py" --quiet-if-cached || true
fi

if [[ -f "${ROOT_DIR}/dependency_health_check.py" ]]; then
  printf '[%s] health-probe START\n' "${LAUNCH_TS}" >> "${LAUNCH_DIAG_LOG}"
  printf '[%s] Validating runtime dependency health...\n' "${LAUNCH_TS}"
  if "${PYTHON_BIN}" "${ROOT_DIR}/dependency_health_check.py" --mode check \
        > "${HEALTH_OUTPUT_LOG}" 2>&1; then
    printf '[%s] Runtime health: OK\n' "${LAUNCH_TS}"
    printf '[%s] health-probe OK\n' "${LAUNCH_TS}" >> "${LAUNCH_DIAG_LOG}"
    # Refresh the HEALTH_STAMP only on success so it actually reflects truth.
    [[ -f "${REQUIREMENTS_STAMP}" ]] \
      && cp "${REQUIREMENTS_STAMP}" "${HEALTH_STAMP}" 2>/dev/null || true
  else
    printf '\n[%s] Runtime health probe FAILED. Recent output:\n' "${LAUNCH_TS}" >&2
    cat "${HEALTH_OUTPUT_LOG}" >&2
    printf '\n[%s] Attempting auto-repair...\n' "${LAUNCH_TS}" >&2
    printf '[%s] health-probe FAIL; attempting repair\n' "${LAUNCH_TS}" >> "${LAUNCH_DIAG_LOG}"
    # Always invalidate the health stamp BEFORE we attempt repair so a
    # crash/kill during repair leaves a re-check signal for next launch.
    rm -f "${HEALTH_STAMP}"
    if "${PYTHON_BIN}" "${ROOT_DIR}/dependency_health_check.py" --mode repair; then
      printf '[%s] Repair succeeded.\n' "${LAUNCH_TS}" >&2
      printf '[%s] health-repair OK\n' "${LAUNCH_TS}" >> "${LAUNCH_DIAG_LOG}"
      [[ -f "${REQUIREMENTS_STAMP}" ]] \
        && cp "${REQUIREMENTS_STAMP}" "${HEALTH_STAMP}" 2>/dev/null || true
    else
      printf '\n============================================================\n' >&2
      printf 'ERROR: Automatic dependency repair FAILED.\n' >&2
      printf '============================================================\n' >&2
      printf 'The runtime health probe failed AND auto-repair did not fix\n' >&2
      printf 'it. Re-running %s alone will not help — the next run will\n' "$(basename "${BASH_SOURCE[0]}")" >&2
      printf 'just re-probe and re-fail. You need to recover manually:\n\n' >&2
      printf '  1. Delete the venv and re-bootstrap:\n' >&2
      printf '       rm -rf "%s" && bash "%s"\n\n' "${ROOT_DIR}/.venv-macos" "${BASH_SOURCE[0]}" >&2
      printf '  2. Force-reinstall the face stack (mirrors REPAIR_PACKAGES\n' >&2
      printf '     in dependency_health_check.py):\n' >&2
      printf '       "%s" -m pip install --force-reinstall \\\n' "${PYTHON_BIN}" >&2
      printf '         --no-cache-dir numpy==1.26.4 tensorflow==2.16.2 \\\n' >&2
      printf '         protobuf==4.25.3 tf-keras==2.16.0 \\\n' >&2
      printf '         retina-face==0.0.17 deepface==0.0.92\n\n' >&2
      printf '  3. Inspect the diagnostic log:\n' >&2
      printf '       %s\n\n' "${HEALTH_OUTPUT_LOG}" >&2
      printf '  4. Inspect the launch log:\n' >&2
      printf '       %s\n\n' "${LAUNCH_DIAG_LOG}" >&2
      printf '[%s] health-repair FAIL; exiting\n' "${LAUNCH_TS}" >> "${LAUNCH_DIAG_LOG}"
      rmdir "${LOCK_PATH}" 2>/dev/null || true
      trap - EXIT
      exit 1
    fi
  fi
fi

# Release the bootstrap lock BEFORE exec'ing the GUI so siblings can launch.
# Tk availability was already checked above (before the health probe) per
# Codex PR #55 round-2 P2 — moved up so Tk-less Pythons get the actionable
# `brew install python-tk@X.Y` message instead of running futile face-stack
# repair. Don't reintroduce a duplicate Tk check here.
rmdir "${LOCK_PATH}" 2>/dev/null || true
trap - EXIT

exec "${PYTHON_BIN}" -u "${ROOT_DIR}/gui_launcher.py" "$@"
