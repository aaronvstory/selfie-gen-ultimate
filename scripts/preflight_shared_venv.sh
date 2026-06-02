#!/usr/bin/env bash
# =============================================================================
#  preflight_shared_venv.sh  (v2.17)
#  Shared preflight for macOS sub-project launchers (oldcam / similarity /
#  resemble .command files). SOURCE it (`. preflight_shared_venv.sh`) or call
#  the function after the launcher resolves PYTHON_BIN against the shared venv.
#
#  Contract (review feedback 2026-06-02, "Gipps"): a sub-launcher must NOT
#  trust a shared root venv blindly + must NOT install its own divergent
#  subset over a half-complete venv. Before the sub-launcher does its own
#  minimal install, this runs the CANONICAL full-set health probe against the
#  shared venv and repairs it if incomplete -- so a missing
#  scipy/absl/mediapipe/torch surfaces here as one canonical repair, not later
#  as a weird ImportError deep in oldcam/similarity.
#
#  Usage:  selfiegen_preflight_shared_venv "<python_bin>" "<repo_root>"
#  Best-effort: NEVER returns non-zero to the caller (the sub-launcher's own
#  minimal import-gate is the final safety net).
#  Opt-out: export SELFIEGEN_SKIP_PREFLIGHT=1
# =============================================================================

selfiegen_preflight_shared_venv() {
  local py="$1"
  local repo_root="$2"

  [[ "${SELFIEGEN_SKIP_PREFLIGHT:-}" == "1" ]] && return 0
  [[ -z "${py}" || -z "${repo_root}" ]] && return 0
  local health="${repo_root}/dependency_health_check.py"
  [[ -f "${health}" ]] || return 0

  local state="${repo_root}/.launcher_state"
  mkdir -p "${state}" 2>/dev/null || true

  if "${py}" "${health}" --mode check >"${state}/preflight_health.log" 2>&1; then
    return 0
  fi

  echo "  [preflight] shared venv incomplete/broken -- running canonical repair..." >&2
  echo "  [preflight] see ${state}/preflight_health.log" >&2
  if ! "${py}" "${health}" --mode repair; then
    echo "  [preflight] WARNING: canonical repair did not fully succeed." >&2
    echo "  [preflight] The sub-app may still launch on its own minimal deps;" >&2
    echo "  [preflight] if it fails, inspect ${state}/preflight_health.log or" >&2
    echo "  [preflight] delete ${repo_root}/.venv-macos and relaunch the MAIN app first." >&2
  fi
  return 0
}
