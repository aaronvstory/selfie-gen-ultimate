#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")/../.." && pwd)"
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

set +e
# PR #49: forward args ("--workspace NAME", etc.) through to gui_launcher.py.
"${ROOT_DIR}/run_gui.sh" "$@"
status=$?
set -e

LAUNCH_ELAPSED="$(( $(date +%s) - LAUNCH_STARTED_AT ))"
if [[ ${status} -ne 0 || ${LAUNCH_ELAPSED} -lt 5 ]]; then
  # Exit 137 = 128+9 (SIGKILL), 143 = 128+15 (SIGTERM). When the GUI had
  # already been running for a while (>=5s) and is then killed by a signal,
  # it was almost certainly terminated by the OS under memory pressure
  # (macOS jetsam) DURING a run -- NOT a startup failure. Diagnose honestly
  # so the user gets an actionable message instead of a misleading
  # "GUI startup failed".
  if [[ ${status} -ne 0 && ${LAUNCH_ELAPSED} -ge 5 && ( ${status} -eq 137 || ${status} -eq 143 ) ]]; then
    printf '\nThe GUI ran for %ss and was then terminated by the system (exit %d).\n' "${LAUNCH_ELAPSED}" "${status}" >&2
    printf 'This is almost always the OS reclaiming memory during heavy post-processing.\n' >&2
    printf 'To avoid it: trim the powerset fan-out (fewer Oldcam versions / AA attacks /\n' >&2
    printf 'crush tiers) or close other memory-heavy apps, then re-run. Paused/aborted\n' >&2
    printf 'runs resume from the menu where they left off.\n' >&2
    printf 'Review this log for details: %s\n' "${LOG_FILE}" >&2
  elif [[ ${status} -ne 0 ]]; then
    printf '\nGUI startup failed (exit %d).\n' "${status}" >&2
    printf 'Set KLING_VERBOSE_STARTUP=1 and retry for full startup diagnostics.\n' >&2
    printf 'Review this log for details: %s\n' "${LOG_FILE}" >&2
  else
    printf '\nGUI launcher exited in %ss, which usually means startup failed before the window opened.\n' "${LAUNCH_ELAPSED}" >&2
    printf 'Set KLING_VERBOSE_STARTUP=1 and retry for full startup diagnostics.\n' >&2
    printf 'Review this log for details: %s\n' "${LOG_FILE}" >&2
  fi
  read -r -p "Press Enter to close..."
fi

exit "${status}"

