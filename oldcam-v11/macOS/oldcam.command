#!/usr/bin/env bash
set -u
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
find_repo_root() {
  local cur="$SCRIPT_DIR"
  while [ -n "$cur" ] && [ "$cur" != "/" ]; do
    if [ -f "$cur/kling_automation_ui.py" ] && [ -f "$cur/requirements.txt" ] && [ -d "$cur/oldcam-v11" ]; then
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
# Bash ARRAY (not a scalar string): a scalar `-c ${REPO_ROOT}/constraints.txt`
# word-splits when REPO_ROOT contains a space (e.g. /Users/John Smith/...),
# breaking pip for the non-technical Mac users this targets. The array +
# "${CONSTRAINTS_ARG[@]+"${CONSTRAINTS_ARG[@]}"}" expansion below keeps the path as one argument.
CONSTRAINTS_ARG=()
if [ -n "${REPO_ROOT}" ] && [ -f "${REPO_ROOT}/constraints.txt" ]; then
  CONSTRAINTS_ARG=(-c "${REPO_ROOT}/constraints.txt")
fi
if [ -n "$REPO_ROOT" ]; then STATE_DIR="$REPO_ROOT/.launcher_state"; else STATE_DIR="$SCRIPT_DIR/.launcher_state"; fi
mkdir -p "$STATE_DIR"
MP_VALIDATE_CMD="import sys, mediapipe as mp; from mediapipe.tasks.python import vision; cls=getattr(vision,'FaceLandmarker',None); sys.exit(0 if cls is not None else 1)"
MP_DIAG_CMD="import sys, os, mediapipe as mp; from mediapipe.tasks.python import vision; cls=getattr(vision,'FaceLandmarker',None); print('python='+sys.executable); print('mediapipe_file='+str(getattr(mp,'__file__','unknown'))); print('mediapipe_version='+str(getattr(mp,'__version__','unknown'))); print('facelandmarker_import_ok='+str(cls is not None)); print('task_file_path='+os.environ.get('OLDCAM_FACE_LANDMARKER_TASK','')); print('task_file_exists='+str(os.path.exists(os.environ.get('OLDCAM_FACE_LANDMARKER_TASK','')))); print('sys_path_0='+(sys.path[0] if sys.path else ''))"
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
TASK_MODEL_PATH=""
if [ -n "${OLDCAM_FACE_LANDMARKER_TASK:-}" ] && [ -f "$OLDCAM_FACE_LANDMARKER_TASK" ]; then
  TASK_MODEL_PATH="$OLDCAM_FACE_LANDMARKER_TASK"
elif [ -f "$SCRIPT_DIR/face_landmarker.task" ]; then
  TASK_MODEL_PATH="$SCRIPT_DIR/face_landmarker.task"
elif [ -n "$REPO_ROOT" ] && [ -f "$REPO_ROOT/face_landmarker.task" ]; then
  TASK_MODEL_PATH="$REPO_ROOT/face_landmarker.task"
elif [ -n "$REPO_ROOT" ] && [ -f "$REPO_ROOT/../face_landmarker.task" ]; then
  TASK_MODEL_PATH="$REPO_ROOT/../face_landmarker.task"
elif [ -f "$(pwd)/face_landmarker.task" ]; then
  TASK_MODEL_PATH="$(pwd)/face_landmarker.task"
fi
if [ -z "$TASK_MODEL_PATH" ]; then
  echo "FaceLandmarker task model missing. Expected face_landmarker.task. Oldcam v11 cannot run."
  echo "Searched: $SCRIPT_DIR/face_landmarker.task ; $REPO_ROOT/face_landmarker.task ; $REPO_ROOT/../face_landmarker.task ; $(pwd)/face_landmarker.task"
  exit 1
fi
export OLDCAM_FACE_LANDMARKER_TASK="$TASK_MODEL_PATH"
# v2.17: canonical shared-venv preflight (full-set health check + repair)
# BEFORE our own minimal install, so a partial shared venv is repaired
# canonically rather than launching oldcam into a weird ImportError.
# Best-effort; the helper never fails the launcher.
if [ -n "$REPO_ROOT" ] && [ -f "$REPO_ROOT/scripts/preflight_shared_venv.sh" ]; then
  . "$REPO_ROOT/scripts/preflight_shared_venv.sh"
  selfiegen_preflight_shared_venv "$PYTHON_CMD" "$REPO_ROOT"
fi

REQ_HASH="$(shasum -a 256 "$SCRIPT_DIR/requirements.txt" 2>/dev/null | awk '{print $1}')"
[ -n "$REQ_HASH" ] || REQ_HASH="missing"
PY_ID="$("$PYTHON_CMD" -c 'import sys; print(f"{sys.version_info[0]}.{sys.version_info[1]}")' 2>/dev/null || echo unknown)"
STAMP="$STATE_DIR/oldcam_v11_${REQ_HASH}_${PY_ID}.ok"
if [ ! -f "$STAMP" ] || ! "$PYTHON_CMD" -c "import cv2, numpy" >/dev/null 2>&1 || ! "$PYTHON_CMD" -c "$MP_VALIDATE_CMD" >/dev/null 2>&1; then
  FILTERED_REQ="$STATE_DIR/oldcam_v11_requirements.filtered.txt"
  grep -E -vi '^[[:space:]]*mediapipe($|[[:space:]]|==|>=|<=|~=|!=)' "$SCRIPT_DIR/requirements.txt" > "$FILTERED_REQ" || true
  "$PYTHON_CMD" -m pip install "${CONSTRAINTS_ARG[@]+"${CONSTRAINTS_ARG[@]}"}" -r "$FILTERED_REQ" || {
    rm -f "$FILTERED_REQ" || true
    echo "Failed to install Oldcam v11 dependencies."
    echo "MediaPipe is required for Oldcam v11."
    echo "Close running Python processes and retry. If still failing, recreate venv."
    exit 1
  }
  "$PYTHON_CMD" -m pip install --force-reinstall --no-deps "${CONSTRAINTS_ARG[@]+"${CONSTRAINTS_ARG[@]}"}" "mediapipe==0.10.35" || {
    rm -f "$FILTERED_REQ" || true
    echo "Failed to install MediaPipe required by Oldcam v11."
    echo "Close running Python processes and retry. If still failing, recreate venv."
    exit 1
  }
  # v2.17: mediapipe --no-deps leaves matplotlib (imported by
  # mediapipe.tasks at load) + opencv-contrib/sounddevice absent, so
  # MP_VALIDATE_CMD below would crash. Install them (numpy<2 pinned).
  "$PYTHON_CMD" -m pip install "${CONSTRAINTS_ARG[@]+"${CONSTRAINTS_ARG[@]}"}" \
    matplotlib "opencv-contrib-python<4.12" sounddevice "numpy>=1.26,<2" || true
  if ! "$PYTHON_CMD" -c "$MP_VALIDATE_CMD" >/dev/null 2>&1; then
    echo "MediaPipe installed but Tasks FaceLandmarker API unavailable. Oldcam v11 cannot run."
    echo "Close Python/GUI processes, delete/rebuild venv, and retry."
    echo "Python executable: $PYTHON_CMD"
    echo "Validation command: \"$PYTHON_CMD\" -c \"$MP_VALIDATE_CMD\""
    "$PYTHON_CMD" -c "$MP_DIAG_CMD" || true
    rm -f "$FILTERED_REQ" || true
    exit 1
  fi
  rm -f "$FILTERED_REQ" || true
  rm -f "$STATE_DIR"/oldcam_v11_*.ok
  echo ok > "$STAMP"
fi
pick_files() {
  osascript <<'APPLESCRIPT'
set selectedFiles to choose file with prompt "Select one or more media files for Oldcam V11" with multiple selections allowed
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
