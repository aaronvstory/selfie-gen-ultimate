#!/usr/bin/env bash
set -uo pipefail

ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
export PATH="/usr/local/bin:/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin:${PATH:-}"
cd "${ROOT_DIR}"

if [[ ! -x "${ROOT_DIR}/run_cli.sh" ]]; then
  chmod +x "${ROOT_DIR}/run_cli.sh" || true
fi

if [[ -x "${ROOT_DIR}/setup_macos.sh" ]]; then
  "${ROOT_DIR}/setup_macos.sh" || true
fi

if [[ -x "${ROOT_DIR}/.venv-macos/bin/python" && -f "${ROOT_DIR}/dependency_checker.py" ]]; then
  "${ROOT_DIR}/.venv-macos/bin/python" "${ROOT_DIR}/dependency_checker.py" --auto || true
fi

set +e
"${ROOT_DIR}/run_cli.sh"
status=$?
set -e

if [[ ${status} -ne 0 ]]; then
  printf '\nCLI launcher failed with exit code %d.\n' "${status}" >&2
  read -r -p "Press Enter to close..."
fi

exit "${status}"
