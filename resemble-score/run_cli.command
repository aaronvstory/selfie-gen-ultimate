#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")" || exit 1

LOG_FILE="$(pwd)/launcher_runtime.log"
{
  echo
  echo "==============================================================================="
  echo "[INFO] [$(date '+%Y-%m-%d %H:%M:%S')] Starting run_cli.command in $(pwd)"
} >> "${LOG_FILE}"
if [ -n "${RESEMBLE_LAUNCHED_BY_MAIN:-}" ]; then
    exec >> "${LOG_FILE}" 2>&1
else
    exec > >(tee -a "${LOG_FILE}") 2>&1
fi

export PYTHONNOUSERSITE=1
unset PYTHONPATH || true
unset PYTHONHOME || true

if [ -f "../requirements.txt" ] && [ -f "../kling_automation_ui.py" ]; then
  REPO_ROOT="$(cd .. && pwd)"
else
  REPO_ROOT=""
fi
if [ -n "$REPO_ROOT" ]; then
  STATE_DIR="$REPO_ROOT/.launcher_state"
else
  STATE_DIR="$(pwd)/.launcher_state"
fi
mkdir -p "$STATE_DIR"

# Validate that an interpreter is in the supported range (3.9 <= ver < 3.13).
# Per-candidate guard: a stale wrong-version venv falls through instead of
# being returned only to trip the post-resolve gate with a misleading error.
_python_supported() {
  "$1" -c 'import sys; raise SystemExit(0 if (3, 9) <= sys.version_info[:2] < (3, 13) else 2)' >/dev/null 2>&1
}

resolve_python() {
  # Overrides stay permissive at resolve time — the post-resolve gate gives a
  # tailored error if they point at an unsupported interpreter.
  if [ -n "${SELFIEGEN_PYTHON:-}" ] && [ -x "${SELFIEGEN_PYTHON}" ]; then
    if "${SELFIEGEN_PYTHON}" -V >/dev/null 2>&1; then echo "${SELFIEGEN_PYTHON}|SELFIEGEN_PYTHON override"; return 0; fi
  fi
  if [ -n "${SELFIEGEN_VENV_DIR:-}" ] && [ -x "${SELFIEGEN_VENV_DIR}/bin/python" ]; then
    if "${SELFIEGEN_VENV_DIR}/bin/python" -V >/dev/null 2>&1; then echo "${SELFIEGEN_VENV_DIR}/bin/python|SELFIEGEN_VENV_DIR override"; return 0; fi
  fi
  if [ -n "$REPO_ROOT" ] && [ -x "$REPO_ROOT/venv/bin/python" ] && _python_supported "$REPO_ROOT/venv/bin/python"; then echo "$REPO_ROOT/venv/bin/python|shared root venv"; return 0; fi
  if [ -n "$REPO_ROOT" ] && [ -x "$REPO_ROOT/.venv311/bin/python" ] && _python_supported "$REPO_ROOT/.venv311/bin/python"; then echo "$REPO_ROOT/.venv311/bin/python|shared root .venv311"; return 0; fi
  if [ -n "$REPO_ROOT" ] && [ -x "$REPO_ROOT/.venv/bin/python" ] && _python_supported "$REPO_ROOT/.venv/bin/python"; then echo "$REPO_ROOT/.venv/bin/python|shared root .venv"; return 0; fi
  if [ -x ".venv311/bin/python" ] && _python_supported "$(pwd)/.venv311/bin/python"; then echo "$(pwd)/.venv311/bin/python|local module .venv311 fallback"; return 0; fi
  if [ -x ".venv/bin/python" ] && _python_supported "$(pwd)/.venv/bin/python"; then echo "$(pwd)/.venv/bin/python|local module .venv fallback"; return 0; fi
  # macOS Python — prefer python3.11 first per CLAUDE.md Rule 6 (only Homebrew
  # Python that ships with bundled _tkinter).
  if [ -n "$REPO_ROOT" ]; then
    pybin="$(command -v python3.11 || command -v python3.12 || command -v python3 || command -v python || true)"
    [ -n "$pybin" ] || return 1
    _python_supported "$pybin" || { echo "[ERROR] No supported Python (3.9-3.12) on PATH (found: $pybin). Install python3.11 (brew install python@3.11) and retry." >&2; return 1; }
    "$pybin" -m venv "$REPO_ROOT/venv" || return 1
    [ -x "$REPO_ROOT/venv/bin/python" ] || return 1
    echo "$REPO_ROOT/venv/bin/python|created shared root venv"; return 0
  fi
  pybin="$(command -v python3.11 || command -v python3.12 || command -v python3 || command -v python || true)"
  [ -n "$pybin" ] || return 1
  _python_supported "$pybin" || { echo "[ERROR] No supported Python (3.9-3.12) on PATH (found: $pybin). Install python3.11 (brew install python@3.11) and retry." >&2; return 1; }
  # Prefer .venv311 per CLAUDE.md Rule 6 — canonical macOS venv name.
  if "$pybin" -m venv .venv311 && [ -x ".venv311/bin/python" ]; then
    echo "$(pwd)/.venv311/bin/python|created local module .venv311 fallback"; return 0
  fi
  "$pybin" -m venv .venv || return 1
  [ -x ".venv/bin/python" ] || return 1
  echo "$(pwd)/.venv/bin/python|created local module .venv fallback"
}

resolved="$(resolve_python || true)"
if [ -z "$resolved" ]; then
  echo "[ERROR] No usable Python environment found."
  [ -z "${RESEMBLE_LAUNCHED_BY_MAIN:-}" ] && read -r -p "Press Enter to exit..."
  exit 1
fi
PYTHON_BIN="${resolved%%|*}"
ENV_KIND="${resolved#*|}"

echo "[INFO] Using $ENV_KIND: $PYTHON_BIN"
if ! "$PYTHON_BIN" -c 'import sys; raise SystemExit(0 if (3, 9) <= sys.version_info[:2] < (3, 13) else 2)' >/dev/null 2>&1; then
  PY_ACTUAL="$("$PYTHON_BIN" -c 'import sys; print(".".join(map(str, sys.version_info[:3])))' 2>/dev/null || echo unknown)"
  if [ -n "${SELFIEGEN_PYTHON:-}" ] || [ -n "${SELFIEGEN_VENV_DIR:-}" ]; then
    echo "[ERROR] Your SELFIEGEN_PYTHON / SELFIEGEN_VENV_DIR override points at Python ${PY_ACTUAL}, but resemble-score requires 3.9-3.12. Unset the override or point it at a supported interpreter."
  else
    echo "[ERROR] Resolved Python is ${PY_ACTUAL}, outside the supported range 3.9-3.12 (resolver bug — please file an issue with this log)."
  fi
  [ -z "${RESEMBLE_LAUNCHED_BY_MAIN:-}" ] && read -r -p "Press Enter to exit..."
  exit 1
fi

REQ_HASH="$(shasum -a 256 requirements.txt 2>/dev/null | awk '{print $1}')"
[ -n "$REQ_HASH" ] || REQ_HASH="missing"
PY_ID="$("$PYTHON_BIN" -c 'import sys; print(f"{sys.version_info[0]}.{sys.version_info[1]}")' 2>/dev/null || echo unknown)"
STAMP_FILE="$STATE_DIR/resemble_cli_${REQ_HASH}_${PY_ID}.ok"

NEED_PIP=1
if [ -f "$STAMP_FILE" ] && "$PYTHON_BIN" -c 'import requests, rich' >/dev/null 2>&1; then
  NEED_PIP=0
fi
if [ "$NEED_PIP" -eq 1 ]; then
  echo "[INFO] Synchronizing dependencies from requirements.txt..."
  if ! "$PYTHON_BIN" -m pip install -r requirements.txt; then
    echo "[ERROR] Failed to synchronize dependencies from requirements.txt."
    [ -z "${RESEMBLE_LAUNCHED_BY_MAIN:-}" ] && read -r -p "Press Enter to exit..."
    exit 1
  fi
  rm -f "$STATE_DIR"/resemble_cli_*.ok
  echo "ok" > "$STAMP_FILE"
else
  echo "[INFO] Requirements unchanged. Skipping pip install."
fi

echo "[INFO] Launching resemble-score CLI..."
set +e
if [ -z "${RESEMBLE_LAUNCHED_BY_MAIN:-}" ]; then
  "$PYTHON_BIN" main.py --cli
else
  "$PYTHON_BIN" main.py --cli >> "${LOG_FILE}" 2>&1
fi
EXIT_CODE=$?
set -e

echo "[INFO] Application finished with code $EXIT_CODE"
if [ -z "${RESEMBLE_LAUNCHED_BY_MAIN:-}" ]; then
  read -r -p "Press Enter to exit..."
fi

exit $EXIT_CODE
