#!/bin/bash
set -uo pipefail

ROOT_DIR="$(cd "$(dirname "$0")" && pwd)"
TARGET="${ROOT_DIR}/launchers/run_gui.command"

if [[ ! -f "${TARGET}" ]]; then
  printf 'Missing launcher: %s\n' "${TARGET}" >&2
  read -r -p "Press Enter to close..."
  exit 1
fi

if [[ ! -x "${TARGET}" ]]; then
  chmod +x "${TARGET}" || true
fi

exec "${TARGET}"
