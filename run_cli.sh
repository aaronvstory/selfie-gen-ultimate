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

# Run the runtime health probe on EVERY launch (v2.17). The previous
# "skip when REQUIREMENTS_STAMP == HEALTH_STAMP" short-circuit meant that
# once the two stamps matched, a venv that broke LATER (numpy 2.x re-pulled
# by an unrelated pip run, an OS update breaking a wheel) was never
# re-detected — the macOS twin of the run_cli.bat unconditional-stamp bug.
# The probe is ~3-5s; matching run_gui.sh which already probes every launch.
# Clear HEALTH_STAMP before repair so a failed repair can't leave a stale
# "healthy" marker.
if [[ -f "${ROOT_DIR}/dependency_health_check.py" ]]; then
  "${PYTHON_BIN}" "${ROOT_DIR}/dependency_health_check.py" --mode check || {
    echo "Runtime dependency health check failed. Attempting auto-repair..." >&2
    rm -f "${HEALTH_STAMP}" 2>/dev/null || true
    "${PYTHON_BIN}" "${ROOT_DIR}/dependency_health_check.py" --mode repair
  }
  cp "${REQUIREMENTS_STAMP}" "${HEALTH_STAMP}" 2>/dev/null || true
fi

# Auto-detect NVIDIA + bootstrap CuPy (same as run_gui.sh — see that
# launcher for the full design rationale + opt-out env var).
if [[ -f "${ROOT_DIR}/scripts/gpu_bootstrap.py" ]]; then
  "${PYTHON_BIN}" "${ROOT_DIR}/scripts/gpu_bootstrap.py" --quiet-if-cached || true
fi

exec "${PYTHON_BIN}" -u "${ROOT_DIR}/kling_automation_ui.py"
