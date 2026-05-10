#!/usr/bin/env bash

cd "$(dirname "$0")" || exit 1

LOG_FILE="$(pwd)/launcher_runtime.log"
{
  echo
  echo "==============================================================================="
  echo "[INFO] [$(date '+%Y-%m-%d %H:%M:%S')] Starting run_gui.command in $(pwd)"
} >> "${LOG_FILE}"
if [ -n "${SIMILARITY_LAUNCHED_BY_MAIN:-}" ]; then
    exec >> "${LOG_FILE}" 2>&1
else
    exec > >(tee -a "${LOG_FILE}") 2>&1
fi

export TF_USE_LEGACY_KERAS=1
export KERAS_BACKEND=tensorflow
export PYTHONNOUSERSITE=1
unset PYTHONPATH
unset PYTHONHOME

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

resolve_python() {
  if [ -n "${SELFIEGEN_PYTHON:-}" ] && [ -x "${SELFIEGEN_PYTHON}" ]; then
    if "${SELFIEGEN_PYTHON}" -V >/dev/null 2>&1; then echo "${SELFIEGEN_PYTHON}|SELFIEGEN_PYTHON override"; return 0; fi
  fi
  if [ -n "${SELFIEGEN_VENV_DIR:-}" ] && [ -x "${SELFIEGEN_VENV_DIR}/bin/python" ]; then
    if "${SELFIEGEN_VENV_DIR}/bin/python" -V >/dev/null 2>&1; then echo "${SELFIEGEN_VENV_DIR}/bin/python|SELFIEGEN_VENV_DIR override"; return 0; fi
  fi
  if [ -n "$REPO_ROOT" ] && [ -x "$REPO_ROOT/venv/bin/python" ]; then echo "$REPO_ROOT/venv/bin/python|shared root venv"; return 0; fi
  if [ -n "$REPO_ROOT" ] && [ -x "$REPO_ROOT/.venv/bin/python" ]; then echo "$REPO_ROOT/.venv/bin/python|shared root .venv"; return 0; fi
  if [ -x ".venv/bin/python" ]; then echo "$(pwd)/.venv/bin/python|local module .venv fallback"; return 0; fi
  if [ -n "$REPO_ROOT" ]; then
    pybin="$(command -v python3.12 || command -v python3.11 || command -v python3 || command -v python || true)"
    [ -n "$pybin" ] || return 1
    "$pybin" -m venv "$REPO_ROOT/venv" || return 1
    [ -x "$REPO_ROOT/venv/bin/python" ] || return 1
    echo "$REPO_ROOT/venv/bin/python|created shared root venv"; return 0
  fi
  pybin="$(command -v python3.12 || command -v python3.11 || command -v python3 || command -v python || true)"
  [ -n "$pybin" ] || return 1
  "$pybin" -m venv .venv || return 1
  [ -x ".venv/bin/python" ] || return 1
  echo "$(pwd)/.venv/bin/python|created local module .venv fallback"
}

resolved="$(resolve_python)"
if [ -z "$resolved" ]; then
  echo "[ERROR] No usable Python environment found."
  [ -z "${SIMILARITY_LAUNCHED_BY_MAIN:-}" ] && read -r -p "Press Enter to exit..."
  exit 1
fi
PYTHON_BIN="${resolved%%|*}"
ENV_KIND="${resolved#*|}"

echo "[1/5] Locating repository root..."
if [ -n "$REPO_ROOT" ]; then echo "      Found: $REPO_ROOT"; else echo "      Standalone mode: no repo root found."; fi
echo "[2/5] Selecting Python environment..."
echo "      Using $ENV_KIND"
echo "      $PYTHON_BIN"

REQ_HASH="$(shasum -a 256 requirements.txt 2>/dev/null | awk '{print $1}')"
[ -n "$REQ_HASH" ] || REQ_HASH="missing"
PY_ID="$("$PYTHON_BIN" -c 'import sys; print(f"{sys.version_info[0]}.{sys.version_info[1]}")' 2>/dev/null || echo unknown)"
STAMP_FILE="$STATE_DIR/similarity_gui_${REQ_HASH}_${PY_ID}.ok"

echo "[3/5] Checking dependency state..."
NEED_PIP=1
if [ -f "$STAMP_FILE" ] && "$PYTHON_BIN" -c 'import cv2, numpy; from PIL import Image; import tkinter' >/dev/null 2>&1; then
  NEED_PIP=0
fi
if [ "$NEED_PIP" -eq 0 ]; then
  echo "      Requirements unchanged. Skipping pip install."
else
  echo "      Dependencies stale or missing. Installing..."
  "$PYTHON_BIN" -m pip install -r requirements.txt
  if [ $? -ne 0 ]; then
    echo "[ERROR] Failed to synchronize dependencies from requirements.txt."
    [ -z "${SIMILARITY_LAUNCHED_BY_MAIN:-}" ] && read -r -p "Press Enter to exit..."
    exit 1
  fi
  rm -f "$STATE_DIR"/similarity_gui_*.ok
  echo "ok" > "$STAMP_FILE"
fi

echo "[4/5] Launching Face Similarity GUI..."
echo "      Runtime log: $LOG_FILE"
echo "      Crash log: $(pwd)/crash.log"
"$PYTHON_BIN" main.py
EXIT_CODE=$?

echo "[5/5] Running..."
if [ $EXIT_CODE -ne 0 ]; then
  echo "[ERROR] Application exited with an error (code=$EXIT_CODE)."
  [ -z "${SIMILARITY_LAUNCHED_BY_MAIN:-}" ] && read -r -p "Press Enter to exit..."
fi

echo "[INFO] run_gui.command exiting with code $EXIT_CODE"
exit $EXIT_CODE
