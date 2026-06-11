#!/usr/bin/env bash
set -uo pipefail

# RUN SUITE - unified Selfie Gen Ultimate launcher (macOS).
# One front door: GUI / CLI / dependency check / system info.
# Delegates to the canonical launcher chain in launchers/macos (which owns
# Python resolution, venv bootstrap and ALL dependency installs - this file
# must never pip install anything itself).
# Colors only when stdout is a TTY; plain otherwise (pipes, logs).

ROOT_DIR="$(cd "$(dirname "$0")" && pwd)"

APP_VER="$(sed -n 's/^RELEASE_VERSION[[:space:]]*=[[:space:]]*"\(.*\)".*/\1/p' \
  "${ROOT_DIR}/app_version.py" 2>/dev/null | head -1 || true)"
[[ -n "${APP_VER}" ]] || APP_VER="unknown"

if [[ -t 1 ]]; then
  C0=$'\033[0m'    # reset
  CB=$'\033[1;97m' # bold white
  CC=$'\033[96m'   # cyan
  CY=$'\033[93m'   # yellow
  CG=$'\033[92m'   # green
  CR=$'\033[91m'   # red
  CD=$'\033[90m'   # dim
else
  C0=''; CB=''; CC=''; CY=''; CG=''; CR=''; CD=''
fi

resolve_python() {
  local cand
  for cand in "${ROOT_DIR}/.venv311/bin/python" "${ROOT_DIR}/venv/bin/python"; do
    if [[ -x "${cand}" ]]; then
      printf '%s' "${cand}"
      return 0
    fi
  done
  for cand in python3.11 python3; do
    if command -v "${cand}" >/dev/null 2>&1; then
      printf '%s' "${cand}"
      return 0
    fi
  done
  return 1
}

banner() {
  clear
  printf '\n'
  printf '%s' "${CC}"
  cat <<'EOF'
  ███████ ███████ ██      ███████ ██ ███████      ██████  ███████ ███    ██
  ██      ██      ██      ██      ██ ██          ██       ██      ████   ██
  ███████ █████   ██      █████   ██ █████       ██   ███ █████   ██ ██  ██
       ██ ██      ██      ██      ██ ██          ██    ██ ██      ██  ██ ██
  ███████ ███████ ███████ ██      ██ ███████      ██████  ███████ ██   ████
EOF
  printf '%s\n' "${C0}"
  printf '            %sULTIMATE  %s%s  %s·  Front → Selfie → Similarity → Video → Oldcam%s\n' \
    "${CB}" "${APP_VER}" "${C0}" "${CD}" "${C0}"
  printf '  %s==========================================================================%s\n' "${CD}" "${C0}"
  printf '    %sHost:%s %s\n' "${CG}" "${C0}" "$(sysctl -n machdep.cpu.brand_string 2>/dev/null || echo 'unknown CPU')"
  printf '    %sNote: the CUDA GPU check is Windows-only; rPPG runs on CPU on macOS.%s\n' "${CD}" "${C0}"
}

while true; do
  banner
  printf '\n'
  printf '    %s[1]%s  %sLaunch GUI%s          %s(Tkinter manual lab)%s\n' "${CY}" "${C0}" "${CB}" "${C0}" "${CD}" "${C0}"
  printf '    %s[2]%s  %sLaunch CLI%s          %s(automation pipeline)%s\n' "${CY}" "${C0}" "${CB}" "${C0}" "${CD}" "${C0}"
  printf '    %s[3]%s  Dependency check / repair\n' "${CY}" "${C0}"
  printf '    %s[q]%s  Quit\n' "${CR}" "${C0}"
  printf '\n'
  read -r -p '   Select an option: ' choice || exit 0
  case "${choice}" in
    1)
      "${ROOT_DIR}/launchers/macos/run_gui.command" || true
      ;;
    2)
      "${ROOT_DIR}/launchers/macos/run_cli.command" || true
      ;;
    3)
      if PY="$(resolve_python)"; then
        "${PY}" "${ROOT_DIR}/dependency_checker.py" || true
      else
        printf 'No Python found yet. Launch the GUI or CLI once first - the launcher bootstraps Python and the venv.\n'
      fi
      read -r -p 'Press Enter to continue...' _ || true
      ;;
    q|Q)
      exit 0
      ;;
    *)
      ;;
  esac
done
