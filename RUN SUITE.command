#!/usr/bin/env bash
set -uo pipefail

# RUN SUITE - unified Selfie Gen Ultimate launcher (macOS).
# One front door: GUI / CLI / dependency check / system info.
# Delegates to the canonical launcher chain in launchers/macos (which owns
# Python resolution, venv bootstrap and ALL dependency installs - this file
# must never pip install anything itself).

ROOT_DIR="$(cd "$(dirname "$0")" && pwd)"

APP_VER="$(sed -n 's/^RELEASE_VERSION[[:space:]]*=[[:space:]]*"\(.*\)".*/\1/p' \
  "${ROOT_DIR}/app_version.py" 2>/dev/null | head -1 || true)"
[[ -n "${APP_VER}" ]] || APP_VER="unknown"

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
  cat <<'EOF'

  ███████ ███████ ██      ███████ ██ ███████      ██████  ███████ ███    ██
  ██      ██      ██      ██      ██ ██          ██       ██      ████   ██
  ███████ █████   ██      █████   ██ █████       ██   ███ █████   ██ ██  ██
       ██ ██      ██      ██      ██ ██          ██    ██ ██      ██  ██ ██
  ███████ ███████ ███████ ██      ██ ███████      ██████  ███████ ██   ████

EOF
  printf '            ULTIMATE  %s  ·  Front → Selfie → Similarity → Video → Oldcam\n' "${APP_VER}"
  printf '  ==========================================================================\n'
  printf '    Host: %s\n' "$(sysctl -n machdep.cpu.brand_string 2>/dev/null || echo 'unknown CPU')"
  printf '    Note: the CUDA GPU check is Windows-only; rPPG runs on CPU on macOS.\n'
}

while true; do
  banner
  printf '\n'
  printf '    [1]  Launch GUI          (Tkinter manual lab)\n'
  printf '    [2]  Launch CLI          (automation pipeline)\n'
  printf '    [3]  Dependency check / repair\n'
  printf '    [q]  Quit\n'
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
