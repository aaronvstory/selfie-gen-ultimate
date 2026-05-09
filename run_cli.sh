#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PYTHON_BIN="${ROOT_DIR}/.venv-macos/bin/python"

"${ROOT_DIR}/setup_macos.sh"
export KLING_SKIP_PY_STARTUP_DEP_CHECK=1

if [[ -f "${ROOT_DIR}/dependency_health_check.py" ]]; then
  "${PYTHON_BIN}" "${ROOT_DIR}/dependency_health_check.py" --mode check || {
    echo "Runtime dependency health check failed. Attempting auto-repair..." >&2
    "${PYTHON_BIN}" "${ROOT_DIR}/dependency_health_check.py" --mode repair
  }
fi

exec "${PYTHON_BIN}" -u "${ROOT_DIR}/kling_automation_ui.py"
