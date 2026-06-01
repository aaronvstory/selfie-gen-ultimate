#!/usr/bin/env bash
set -u
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
find_repo_root() {
  local cur="$SCRIPT_DIR"
  while [ -n "$cur" ] && [ "$cur" != "/" ]; do
    if [ -f "$cur/kling_automation_ui.py" ] && [ -f "$cur/requirements.txt" ] && [ -d "$cur/oldcam-v7" ] && [ -d "$cur/oldcam-v8" ]; then
      printf '%s\n' "$cur"
      return 0
    fi
    cur="$(dirname "$cur")"
  done
  return 1
}
REPO_ROOT="$(find_repo_root 2>/dev/null || true)"
# v2.11 numpy-2 guard: thread the project-wide constraints file into pip
# so a transitive resolve can't upgrade numpy past 1.x (mirrors the
# Windows oldcam launchers). Guarded: only added when the file exists,
# since find_repo_root can return empty on an unusual layout.
CONSTRAINTS_ARG=""
if [ -n "${REPO_ROOT}" ] && [ -f "${REPO_ROOT}/constraints.txt" ]; then
  CONSTRAINTS_ARG="-c ${REPO_ROOT}/constraints.txt"
fi
if [ -n "$REPO_ROOT" ]; then STATE_DIR="$REPO_ROOT/.launcher_state"; else STATE_DIR="$SCRIPT_DIR/.launcher_state"; fi
mkdir -p "$STATE_DIR"
resolve_py(){
  if [ -n "${SELFIEGEN_PYTHON:-}" ] && [ -x "$SELFIEGEN_PYTHON" ]; then echo "$SELFIEGEN_PYTHON"; return; fi
  if [ -n "${SELFIEGEN_VENV_DIR:-}" ] && [ -x "$SELFIEGEN_VENV_DIR/bin/python" ]; then echo "$SELFIEGEN_VENV_DIR/bin/python"; return; fi
  [ -n "$REPO_ROOT" ] && [ -x "$REPO_ROOT/venv/bin/python" ] && { echo "$REPO_ROOT/venv/bin/python"; return; }
  [ -n "$REPO_ROOT" ] && [ -x "$REPO_ROOT/.venv/bin/python" ] && { echo "$REPO_ROOT/.venv/bin/python"; return; }
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
  "$PYTHON_CMD" -m pip install ${CONSTRAINTS_ARG} -r "$SCRIPT_DIR/requirements.txt" || exit 1
  rm -f "$STATE_DIR"/oldcam_v7_*.ok
  echo ok > "$STAMP"
fi
pick_files() {
  osascript <<'APPLESCRIPT'
set selectedFiles to choose file with prompt "Select one or more media files for oldcam" with multiple selections allowed
set outputText to ""
repeat with oneFile in selectedFiles
  set outputText to outputText & POSIX path of oneFile & linefeed
end repeat
return outputText
APPLESCRIPT
}

if [ "$#" -eq 0 ]; then
  if ! SELECTED_FILES="$(pick_files)"; then
    echo "No files selected."
    exit 0
  fi
  HAD_ERRORS=0
  while IFS= read -r file_path; do
    [ -z "$file_path" ] && continue
    "$PYTHON_CMD" "$SCRIPT_DIR/oldcam.py" "$file_path" || HAD_ERRORS=1
  done <<< "$SELECTED_FILES"
  exit "$HAD_ERRORS"
fi

exec "$PYTHON_CMD" "$SCRIPT_DIR/oldcam.py" "$@"
