#!/usr/bin/env bash
# AA (adversarial-attack) video toolkit launcher — macOS / Linux.
#
# UNLIKE rPPG/oldcam (which share the main repo venv), this subproject runs in
# its OWN ISOLATED uv venv (aa-video/.venv) because its deps (numpy 2.x,
# opencv 4.x, optional torch) irreconcilably conflict with the main repo
# invariant (numpy<2 / opencv<4.12). The launcher OWNS that venv: it ensures
# uv, syncs aa-video/.venv from this dir's pyproject.toml + uv.lock, then runs
# main.py with whatever args were passed.
#
# macOS rule: the default lock is CPU-only (no torch). NEVER pass a `cu*`
# extra here — the optional `gpu` extra's [tool.uv.sources] marker already
# excludes darwin, so even an opt-in GPU sync resolves to the MPS/CPU wheel.
#
# Standalone-runnable: `./aa_launcher.sh --input clip.mp4 --attack prime`.

set -euo pipefail

SCRIPT_DIR="$( cd -- "$( dirname -- "${BASH_SOURCE[0]}" )" &> /dev/null && pwd )"
REPO_ROOT="$( cd -- "${SCRIPT_DIR}/.." &> /dev/null && pwd )"
STATE_DIR="${REPO_ROOT}/.launcher_state"
mkdir -p "${STATE_DIR}"
LOG_FILE="${STATE_DIR}/aa.log"

# Force unbuffered stdout + headless matplotlib so the GUI subprocess streamer
# sees per-frame progress in real time and nothing blocks on a GUI window.
export PYTHONUNBUFFERED=1
export MPLBACKEND="Agg"

# --- Resolve uv (stdlib-only bootstrap; runs before any venv exists) --------
UV_BIN=""
if command -v uv >/dev/null 2>&1; then
  UV_BIN="$(command -v uv)"
elif [ -x "${HOME}/.local/bin/uv" ]; then
  UV_BIN="${HOME}/.local/bin/uv"
else
  # ensure_uv.py is stdlib-only and prints the resolved uv path on stdout.
  for cand in python3.11 python3.12 python3 python; do
    if command -v "${cand}" >/dev/null 2>&1; then
      if UV_BIN="$( "${cand}" "${REPO_ROOT}/scripts/ensure_uv.py" --print-path 2>>"${LOG_FILE}" )"; then
        break
      fi
      UV_BIN=""
    fi
  done
fi

if [ -z "${UV_BIN}" ] || [ ! -x "${UV_BIN}" ]; then
  echo "  ERROR: uv not found and could not be bootstrapped." >&2
  echo "  Install uv: https://docs.astral.sh/uv/  (or run scripts/ensure_uv.py)" >&2
  echo "[ERROR] uv unavailable; cannot provision aa-video venv." >> "${LOG_FILE}"
  exit 1
fi
echo "  uv: ${UV_BIN}"
echo "[INFO] using uv: ${UV_BIN}" >> "${LOG_FILE}"

cd "${SCRIPT_DIR}"

# --- Sync the isolated venv (CPU-only default lock) -------------------------
# uv sync is idempotent: a no-op when the venv already matches the lock, a
# one-time install on a fresh checkout. Scoped to this dir so it provisions
# aa-video/.venv, NOT the main repo venv.
echo "  Syncing aa-video venv (uv sync)..."
echo "[INFO] uv sync starting" >> "${LOG_FILE}"
if ! "${UV_BIN}" sync >> "${LOG_FILE}" 2>&1; then
  echo "  ERROR: uv sync failed (see ${LOG_FILE})." >&2
  echo "[ERROR] uv sync failed." >> "${LOG_FILE}"
  exit 1
fi
echo "  OK: aa-video venv ready."

# --- Run the tool -----------------------------------------------------------
echo "  Launching aa-video main.py $*"
echo "[INFO] Launching main.py $*" >> "${LOG_FILE}"
set +e
"${UV_BIN}" run --no-sync python main.py "$@"
EXIT_CODE=$?
set -e
echo "  Finished with code ${EXIT_CODE}."
echo "[INFO] Finished with code ${EXIT_CODE}." >> "${LOG_FILE}"
exit "${EXIT_CODE}"
