#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PYTHON_BIN="${ROOT_DIR}/.venv-macos/bin/python"
REQUIREMENTS_STAMP="${ROOT_DIR}/.venv-macos/.requirements.sha256"
HEALTH_STAMP="${ROOT_DIR}/.venv-macos/.health.sha256"

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
    cp "${REQUIREMENTS_STAMP}" "${HEALTH_STAMP}" 2>/dev/null || true
  fi
fi

# Auto-detect NVIDIA + bootstrap CuPy (same as run_gui.sh — see that
# launcher for the full design rationale + opt-out env var).
if [[ -f "${ROOT_DIR}/scripts/gpu_bootstrap.py" ]]; then
  "${PYTHON_BIN}" "${ROOT_DIR}/scripts/gpu_bootstrap.py" --quiet-if-cached || true
fi

exec "${PYTHON_BIN}" -u "${ROOT_DIR}/kling_automation_ui.py"
