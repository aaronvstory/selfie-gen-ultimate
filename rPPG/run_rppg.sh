#!/usr/bin/env bash
# rPPG injector launcher — macOS / Linux sibling of run_rppg.bat.
#
# Resolves the shared repo Python (3.9-3.12) and invokes
# rppg_injector.py with whatever args were passed. Phase D + Phase E of
# polish/v2.3 (2026-05-22): the rPPG/ folder is now committed in-tree,
# so a macOS clone gets this launcher alongside the .bat.
#
# Python resolver chain matches the macOS launcher policy in CLAUDE.md
# rule 9 (.venv311 first per rule 6, with version validation on every
# candidate). Honours $SELFIEGEN_PYTHON override.

set -uo pipefail

SCRIPT_DIR="$( cd -- "$( dirname -- "${BASH_SOURCE[0]}" )" &> /dev/null && pwd )"
REPO_ROOT="$( cd -- "${SCRIPT_DIR}/.." &> /dev/null && pwd )"
STATE_DIR="${REPO_ROOT}/.launcher_state"
mkdir -p "${STATE_DIR}"
LOG_FILE="${STATE_DIR}/rppg.log"

# Per CLAUDE.md rule 9: every venv candidate must be version-validated
# BEFORE being accepted. A stale .venv pointing at Python 3.13+ would
# otherwise be selected and trip the post-resolve gate later.
_python_supported() {
  "$1" -c 'import sys; raise SystemExit(0 if (3, 9) <= sys.version_info[:2] < (3, 13) else 2)' >/dev/null 2>&1
}

PYTHON_BIN=""
ENV_KIND=""

if [ -n "${SELFIEGEN_PYTHON:-}" ] && _python_supported "${SELFIEGEN_PYTHON}"; then
  PYTHON_BIN="${SELFIEGEN_PYTHON}"
  ENV_KIND="SELFIEGEN_PYTHON override"
fi

# Resolver order: .venv311 first (canonical macOS venv per rule 6),
# then .venv / venv as fallbacks.
for entry in \
  ".venv311/bin/python|shared root .venv311" \
  ".venv/bin/python|shared root .venv" \
  "venv/bin/python|shared root venv" \
; do
  if [ -n "${PYTHON_BIN}" ]; then break; fi
  cand_path="${REPO_ROOT}/${entry%%|*}"
  cand_kind="${entry#*|}"
  if [ -x "${cand_path}" ] && _python_supported "${cand_path}"; then
    PYTHON_BIN="${cand_path}"
    ENV_KIND="${cand_kind}"
  fi
done

# System Python fallback (python3.11 preferred per CLAUDE.md rule 6).
if [ -z "${PYTHON_BIN}" ]; then
  for cand in python3.11 python3.12 python3 python; do
    if command -v "${cand}" >/dev/null 2>&1 && _python_supported "${cand}"; then
      PYTHON_BIN="${cand}"
      ENV_KIND="system ${cand}"
      break
    fi
  done
fi

if [ -z "${PYTHON_BIN}" ]; then
  echo "  ERROR: No supported Python (3.9-3.12) found." >&2
  echo "  Create one: python3.11 -m venv ${REPO_ROOT}/.venv311 && \\" >&2
  echo "             ${REPO_ROOT}/.venv311/bin/pip install -r ${REPO_ROOT}/requirements.txt" >&2
  echo "[ERROR] No supported Python found." >> "${LOG_FILE}"
  exit 1
fi

echo "  Python: ${ENV_KIND} -- ${PYTHON_BIN}"
echo "[INFO] Using ${ENV_KIND}: ${PYTHON_BIN}" >> "${LOG_FILE}"

# Mirror the .bat's mediapipe model env var (if a sibling-of-repo copy
# exists) and force matplotlib's headless backend so the injector's
# visualize_analysis() doesn't block on a GUI window.
if [ -f "${REPO_ROOT}/face_landmarker.task" ]; then
  export MEDIAPIPE_FACE_LANDMARKER_MODEL="${REPO_ROOT}/face_landmarker.task"
fi
export MPLBACKEND="Agg"

cd "${SCRIPT_DIR}"
echo "  Launching rppg_injector.py $*"
echo "[INFO] Launching rppg_injector.py $*" >> "${LOG_FILE}"
"${PYTHON_BIN}" rppg_injector.py "$@"
EXIT_CODE=$?
echo "  Finished with code ${EXIT_CODE}."
echo "[INFO] Finished with code ${EXIT_CODE}." >> "${LOG_FILE}"
exit "${EXIT_CODE}"
