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
printf '   Ultimate-Selfie-Gen  %s  --  CLI Launcher\n' "${APP_VER}"
printf '  ============================================================\n\n'

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
