#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")/../.." && pwd)"
export PATH="/usr/local/bin:/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin:${PATH:-}"
cd "${ROOT_DIR}"

if [[ ! -x "${ROOT_DIR}/run_cli.sh" ]]; then
  chmod +x "${ROOT_DIR}/run_cli.sh" || true
fi

set +e
"${ROOT_DIR}/run_cli.sh" "$@"
status=$?
set -e

if [[ ${status} -ne 0 ]]; then
  printf '\nCLI startup failed (exit %d).\n' "${status}" >&2
  printf 'Set KLING_VERBOSE_STARTUP=1 and retry for full startup diagnostics.\n' >&2
  # Skip the keypress wait in non-interactive/batch mode (cron / launchd would
  # hang forever). run_auto.command sets KLING_NONINTERACTIVE=1.
  [[ -n "${KLING_NONINTERACTIVE:-}" ]] || read -r -p "Press Enter to close..."
fi

exit "${status}"

