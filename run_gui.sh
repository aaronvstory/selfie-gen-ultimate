#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PYTHON_BIN="${ROOT_DIR}/.venv-macos/bin/python"
REQUIREMENTS_STAMP="${ROOT_DIR}/.venv-macos/.requirements.sha256"
HEALTH_STAMP="${ROOT_DIR}/.venv-macos/.health.sha256"

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

# setup_macos.sh installs deps and writes REQUIREMENTS_STAMP when they change
"${ROOT_DIR}/setup_macos.sh"
export KLING_SKIP_PY_STARTUP_DEP_CHECK=1

# Only run dep_health_check when requirements changed since last health check
if [[ -f "${ROOT_DIR}/dependency_health_check.py" ]]; then
  _run_health=1
  if [[ -f "${REQUIREMENTS_STAMP}" && -f "${HEALTH_STAMP}" ]]; then
    if [[ "$(<"${REQUIREMENTS_STAMP}")" == "$(<"${HEALTH_STAMP}")" ]]; then
      _run_health=0
    fi
  fi
  if [[ "${_run_health}" -eq 1 ]]; then
    "${PYTHON_BIN}" "${ROOT_DIR}/dependency_health_check.py" --mode check || {
      echo "Runtime dependency health check failed. Attempting auto-repair..." >&2
      "${PYTHON_BIN}" "${ROOT_DIR}/dependency_health_check.py" --mode repair
    }
    # Record that health was checked against this requirements version
    cp "${REQUIREMENTS_STAMP}" "${HEALTH_STAMP}" 2>/dev/null || true
  fi
fi

# Release the bootstrap lock BEFORE exec'ing the GUI so siblings can launch.
rmdir "${LOCK_PATH}" 2>/dev/null || true
trap - EXIT

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

if ! "${PYTHON_BIN}" -c 'import tkinter' >/dev/null 2>&1; then
  VERSION="$("${PYTHON_BIN}" -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')"
  printf 'GUI launch blocked: this Python environment does not provide Tk support.\n\n' >&2
  printf 'If you are using Homebrew Python, install the matching Tk package and recreate the virtual environment:\n' >&2
  printf '  brew install python-tk@%s\n\n' "${VERSION}" >&2
  printf 'Then rerun ./run_gui.sh.\n' >&2
  exit 1
fi

exec "${PYTHON_BIN}" -u "${ROOT_DIR}/gui_launcher.py" "$@"
