#!/usr/bin/env bash
set -u
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd 2>/dev/null || true)"
if [ -n "$REPO_ROOT" ]; then STATE_DIR="$REPO_ROOT/.launcher_state"; else STATE_DIR="$SCRIPT_DIR/.launcher_state"; fi
mkdir -p "$STATE_DIR"
resolve_py(){
  if [ -n "${SELFIEGEN_PYTHON:-}" ] && [ -x "$SELFIEGEN_PYTHON" ]; then echo "$SELFIEGEN_PYTHON"; return; fi
  if [ -n "${SELFIEGEN_VENV_DIR:-}" ] && [ -x "$SELFIEGEN_VENV_DIR/bin/python" ]; then echo "$SELFIEGEN_VENV_DIR/bin/python"; return; fi
  [ -x "$REPO_ROOT/venv/bin/python" ] && { echo "$REPO_ROOT/venv/bin/python"; return; }
  [ -x "$REPO_ROOT/.venv/bin/python" ] && { echo "$REPO_ROOT/.venv/bin/python"; return; }
  [ -x "$SCRIPT_DIR/.venv/bin/python" ] && { echo "$SCRIPT_DIR/.venv/bin/python"; return; }
  pybin="$(command -v python3.12 || command -v python3.11 || command -v python3 || command -v python || true)"
  [ -z "$pybin" ] && return
  if [ -d "$REPO_ROOT" ] && [ -f "$REPO_ROOT/requirements.txt" ]; then "$pybin" -m venv "$REPO_ROOT/venv" >/dev/null 2>&1 && echo "$REPO_ROOT/venv/bin/python" && return; fi
  "$pybin" -m venv "$SCRIPT_DIR/.venv" >/dev/null 2>&1 && echo "$SCRIPT_DIR/.venv/bin/python"
}
PYTHON_CMD="$(resolve_py)"
[ -n "$PYTHON_CMD" ] || { echo "No python"; exit 1; }
REQ_HASH="$(shasum -a 256 "$SCRIPT_DIR/requirements.txt" 2>/dev/null | awk '{print $1}')"
[ -n "$REQ_HASH" ] || REQ_HASH="missing"
PY_ID="$("$PYTHON_CMD" -c 'import sys; print(f"{sys.version_info[0]}.{sys.version_info[1]}")' 2>/dev/null || echo unknown)"
STAMP="$STATE_DIR/oldcam_v7_${REQ_HASH}_${PY_ID}.ok"
if [ ! -f "$STAMP" ] || ! "$PYTHON_CMD" -c "import cv2, numpy" >/dev/null 2>&1; then
  "$PYTHON_CMD" -m pip install -r "$SCRIPT_DIR/requirements.txt" || exit 1
  rm -f "$STATE_DIR"/oldcam_v7_*.ok
  echo ok > "$STAMP"
fi
exec "$PYTHON_CMD" "$SCRIPT_DIR/oldcam.py" "$@"
