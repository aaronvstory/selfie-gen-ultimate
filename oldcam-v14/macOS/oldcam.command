#!/usr/bin/env bash
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
find_repo_root() {
  local cur="$SCRIPT_DIR"
  while [ -n "$cur" ] && [ "$cur" != "/" ]; do
    if [ -f "$cur/kling_automation_ui.py" ] && [ -f "$cur/requirements.txt" ] && [ -d "$cur/oldcam-v14" ]; then
      printf '%s\n' "$cur"
      return 0
    fi
    cur="$(dirname "$cur")"
  done
  return 1
}
REPO_ROOT="$(find_repo_root 2>/dev/null || true)"
if [ -n "$REPO_ROOT" ]; then STATE_DIR="$REPO_ROOT/.launcher_state"; else STATE_DIR="$SCRIPT_DIR/.launcher_state"; fi
mkdir -p "$STATE_DIR"

# Validate that an interpreter is in the supported range (3.9 <= ver < 3.13).
# Used per-candidate so a stale wrong-version venv (e.g. python3.14 .venv from
# a prior session) falls through instead of being silently returned. This is
# the canonical Rule 9 pattern from similarity/run_gui.command:38-40.
_python_supported() {
  "$1" -c 'import sys; raise SystemExit(0 if (3, 9) <= sys.version_info[:2] < (3, 13) else 2)' >/dev/null 2>&1
}

resolve_py() {
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
  if [ -x "$SCRIPT_DIR/.venv311/bin/python" ] && _python_supported "$SCRIPT_DIR/.venv311/bin/python"; then echo "$SCRIPT_DIR/.venv311/bin/python|local module .venv311"; return 0; fi
  if [ -x "$SCRIPT_DIR/.venv/bin/python" ] && _python_supported "$SCRIPT_DIR/.venv/bin/python"; then echo "$SCRIPT_DIR/.venv/bin/python|local module .venv"; return 0; fi
  # macOS Python — prefer python3.11 first per CLAUDE.md Rule 6.
  pybin="$(command -v python3.11 || command -v python3.12 || command -v python3 || command -v python || true)"
  [ -n "$pybin" ] || return 1
  _python_supported "$pybin" || { echo "[ERROR] No supported Python (3.9-3.12) on PATH (found: $pybin). Install python3.11 (brew install python@3.11) and retry." >&2; return 1; }
  if [ -n "$REPO_ROOT" ]; then
    "$pybin" -m venv "$REPO_ROOT/venv" >/dev/null 2>&1 || return 1
    [ -x "$REPO_ROOT/venv/bin/python" ] || return 1
    echo "$REPO_ROOT/venv/bin/python|created shared root venv"; return 0
  fi
  # Prefer .venv311 per CLAUDE.md Rule 6 — canonical macOS venv name.
  if "$pybin" -m venv "$SCRIPT_DIR/.venv311" >/dev/null 2>&1 && [ -x "$SCRIPT_DIR/.venv311/bin/python" ]; then
    echo "$SCRIPT_DIR/.venv311/bin/python|created local module .venv311"; return 0
  fi
  "$pybin" -m venv "$SCRIPT_DIR/.venv" >/dev/null 2>&1 || return 1
  [ -x "$SCRIPT_DIR/.venv/bin/python" ] || return 1
  echo "$SCRIPT_DIR/.venv/bin/python|created local module .venv"
}

resolved="$(resolve_py)" || true
if [ -z "${resolved:-}" ]; then
  echo "[ERROR] No usable Python environment found. Install python3.11 (brew install python@3.11) and retry."
  exit 1
fi
PYTHON_CMD="${resolved%%|*}"
ENV_KIND="${resolved#*|}"

# Defense-in-depth post-resolve gate (catches the case where an override
# points at an unsupported interpreter).
if ! "$PYTHON_CMD" -c 'import sys; raise SystemExit(0 if (3, 9) <= sys.version_info[:2] < (3, 13) else 2)' >/dev/null 2>&1; then
  PY_ACTUAL="$("$PYTHON_CMD" -c 'import sys; print(".".join(map(str, sys.version_info[:3])))' 2>/dev/null || echo unknown)"
  if [ -n "${SELFIEGEN_PYTHON:-}" ] || [ -n "${SELFIEGEN_VENV_DIR:-}" ]; then
    echo "[ERROR] Your SELFIEGEN_PYTHON / SELFIEGEN_VENV_DIR override points at Python ${PY_ACTUAL}, but Oldcam v14 requires 3.9-3.12. Unset the override or point it at a supported interpreter."
  else
    echo "[ERROR] Resolved Python is ${PY_ACTUAL}, outside the supported range 3.9-3.12 (resolver bug — please file an issue with this log)."
  fi
  exit 1
fi

echo "[oldcam-v14] Using $ENV_KIND"
echo "[oldcam-v14] Python: $PYTHON_CMD"

# V14 is forensic daylight — no MediaPipe / face_landmarker.task required.
REQ_HASH="$(shasum -a 256 "$SCRIPT_DIR/requirements.txt" 2>/dev/null | awk '{print $1}')"
[ -n "$REQ_HASH" ] || REQ_HASH="missing"
PY_ID="$("$PYTHON_CMD" -c 'import sys; print(f"{sys.version_info[0]}.{sys.version_info[1]}")' 2>/dev/null || echo unknown)"
STAMP="$STATE_DIR/oldcam_v14_${REQ_HASH}_${PY_ID}.ok"
if [ ! -f "$STAMP" ] || ! "$PYTHON_CMD" -c "import cv2, numpy" >/dev/null 2>&1; then
  "$PYTHON_CMD" -m pip install -r "$SCRIPT_DIR/requirements.txt" || {
    echo "Failed to install Oldcam v14 dependencies."
    echo "Close running Python processes and retry. If still failing, recreate venv."
    exit 1
  }
  rm -f "$STATE_DIR"/oldcam_v14_*.ok
  echo ok > "$STAMP"
fi
pick_files() {
  osascript <<'APPLESCRIPT'
set selectedFiles to choose file with prompt "Select one or more media files for Oldcam V14" with multiple selections allowed
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
