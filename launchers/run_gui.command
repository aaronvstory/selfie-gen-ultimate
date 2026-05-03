#!/bin/bash
set -uo pipefail

ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
export PATH="/usr/local/bin:/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin:${PATH:-}"
cd "${ROOT_DIR}"
LAUNCH_STARTED_AT="$(date +%s)"

LOG_DIR="${HOME}/Library/Logs/Ultimate-Selfie-Gen"
mkdir -p "${LOG_DIR}"
LOG_FILE="${LOG_DIR}/run_gui.command.log"
exec > >(tee -a "${LOG_FILE}") 2>&1

printf '\n[%s] Starting GUI launcher from %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "${ROOT_DIR}"
printf '[%s] Log file: %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "${LOG_FILE}"

if [[ ! -x "${ROOT_DIR}/run_gui.sh" ]]; then
  printf '[%s] run_gui.sh was not executable; applying chmod +x\n' "$(date '+%Y-%m-%d %H:%M:%S')"
  chmod +x "${ROOT_DIR}/run_gui.sh" || true
fi

if [[ -x "${ROOT_DIR}/setup_macos.sh" ]]; then
  printf '[%s] Running setup_macos.sh preflight\n' "$(date '+%Y-%m-%d %H:%M:%S')"
  "${ROOT_DIR}/setup_macos.sh" || true
fi

if [[ -x "${ROOT_DIR}/.venv-macos/bin/python" && -f "${ROOT_DIR}/dependency_checker.py" ]]; then
  printf '[%s] Running dependency bootstrap preflight\n' "$(date '+%Y-%m-%d %H:%M:%S')"
  "${ROOT_DIR}/.venv-macos/bin/python" "${ROOT_DIR}/dependency_checker.py" --auto || true
fi

set +e
"${ROOT_DIR}/run_gui.sh"
status=$?
set -e

LAUNCH_ELAPSED="$(( $(date +%s) - LAUNCH_STARTED_AT ))"
if [[ ${status} -ne 0 || ${LAUNCH_ELAPSED} -lt 5 ]]; then
  if [[ ${status} -ne 0 ]]; then
    printf '\nGUI launcher failed with exit code %d.\n' "${status}" >&2
  else
    printf '\nGUI launcher exited in %ss, which usually means startup failed before the window opened.\n' "${LAUNCH_ELAPSED}" >&2
  fi
  printf 'Review this log for details: %s\n' "${LOG_FILE}" >&2
  read -r -p "Press Enter to close..."
fi

exit "${status}"
