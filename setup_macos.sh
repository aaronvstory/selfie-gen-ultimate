#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV_DIR="${ROOT_DIR}/.venv-macos"
REQUIREMENTS_FILE="${ROOT_DIR}/requirements.txt"
REQUIREMENTS_STAMP="${VENV_DIR}/.requirements.sha256"
export PATH="/usr/local/bin:/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin:${PATH:-}"
VERBOSE_STARTUP="${KLING_VERBOSE_STARTUP:-0}"

is_supported_python() {
  local python_bin="$1"
  "${python_bin}" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 11) else 1)' >/dev/null 2>&1
}

has_tk_support() {
  local python_bin="$1"
  "${python_bin}" -c 'import tkinter' >/dev/null 2>&1
}

python_base_prefix() {
  local python_bin="$1"
  "${python_bin}" -c 'import sys; print(sys.base_prefix)' 2>/dev/null
}

requirements_hash() {
  local python_bin="$1"
  "${python_bin}" - "${REQUIREMENTS_FILE}" <<'PY'
import hashlib
import pathlib
import sys

print(hashlib.sha256(pathlib.Path(sys.argv[1]).read_bytes()).hexdigest())
PY
}

ensure_homebrew_tk() {
  local python_bin="$1"
  local version formula

  if ! command -v brew >/dev/null 2>&1; then
    return 1
  fi

  version="$("${python_bin}" -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')"
  formula="python-tk@${version}"

  if ! brew info "${formula}" >/dev/null 2>&1; then
    return 1
  fi

  if brew list --versions "${formula}" >/dev/null 2>&1; then
    return 0
  fi

  printf 'Installing %s to enable Tk support for %s\n' "${formula}" "${python_bin}"
  brew install "${formula}"
}

pick_python() {
  local candidate resolved fallback=""
  local -a candidates=()

  if [[ -n "${KLING_PYTHON:-}" ]]; then
    if command -v "${KLING_PYTHON}" >/dev/null 2>&1; then
      candidates+=("${KLING_PYTHON}")
    elif [[ -x "${KLING_PYTHON}" ]]; then
      candidates+=("${KLING_PYTHON}")
    else
      printf 'Configured interpreter not found: %s\n' "${KLING_PYTHON}" >&2
      return 1
    fi
  fi

  candidates+=(
    /usr/local/bin/python3.11
    /usr/local/bin/python3.12
    /usr/local/bin/python3.13
    /usr/local/bin/python3.14
    /opt/homebrew/bin/python3.11
    /opt/homebrew/bin/python3.12
    /opt/homebrew/bin/python3.13
    /opt/homebrew/bin/python3.14
    python3.11
    python3.12
    python3.13
    python3.14
    python3
  )

  for candidate in "${candidates[@]}"; do
    if ! resolved="$(command -v "${candidate}" 2>/dev/null)"; then
      if [[ -x "${candidate}" ]]; then
        resolved="${candidate}"
      else
        continue
      fi
    fi

    if ! is_supported_python "${resolved}"; then
      continue
    fi

    if has_tk_support "${resolved}"; then
      echo "${resolved}"
      return 0
    fi

    if [[ -z "${fallback}" ]]; then
      fallback="${resolved}"
    fi
  done

  if [[ -n "${fallback}" ]] && ensure_homebrew_tk "${fallback}" && has_tk_support "${fallback}"; then
    echo "${fallback}"
    return 0
  fi

  if [[ -n "${fallback}" ]]; then
    echo "${fallback}"
    return 0
  fi

  return 1
}

PYTHON_BIN="$(pick_python)" || {
  printf 'Python 3.11+ is required but was not found on PATH.\n' >&2
  exit 1
}

if ! has_tk_support "${PYTHON_BIN}"; then
  printf 'Warning: selected interpreter lacks Tk support, so GUI launchers will not work until Tk is installed for %s.\n' "${PYTHON_BIN}" >&2
fi

SELECTED_BASE="$(python_base_prefix "${PYTHON_BIN}")"
REBUILD_VENV=0

if [[ ! -x "${VENV_DIR}/bin/python" ]]; then
  REBUILD_VENV=1
else
  EXISTING_BASE="$(python_base_prefix "${VENV_DIR}/bin/python")"
  if [[ -z "${EXISTING_BASE}" || "${EXISTING_BASE}" != "${SELECTED_BASE}" ]]; then
    REBUILD_VENV=1
  fi
fi

if [[ "${REBUILD_VENV}" -eq 1 ]]; then
  printf 'Creating macOS virtual environment with %s\n' "${PYTHON_BIN}"
  "${PYTHON_BIN}" -m venv --clear "${VENV_DIR}"
fi

CURRENT_REQUIREMENTS_HASH="$(requirements_hash "${PYTHON_BIN}")"
SYNC_REQUIREMENTS=0

if [[ "${REBUILD_VENV}" -eq 1 || ! -f "${REQUIREMENTS_STAMP}" ]]; then
  SYNC_REQUIREMENTS=1
else
  INSTALLED_REQUIREMENTS_HASH="$(<"${REQUIREMENTS_STAMP}")"
  if [[ "${INSTALLED_REQUIREMENTS_HASH}" != "${CURRENT_REQUIREMENTS_HASH}" ]]; then
    SYNC_REQUIREMENTS=1
  fi
fi

if [[ "${KLING_FORCE_SETUP:-0}" == "1" ]]; then
  SYNC_REQUIREMENTS=1
fi

if [[ "${SYNC_REQUIREMENTS}" -eq 1 ]]; then
  printf 'Syncing Python dependencies\n'
  "${VENV_DIR}/bin/python" -m pip install --disable-pip-version-check --upgrade pip
  "${VENV_DIR}/bin/python" -m pip install --disable-pip-version-check -r "${REQUIREMENTS_FILE}"
  printf '%s\n' "${CURRENT_REQUIREMENTS_HASH}" > "${REQUIREMENTS_STAMP}"
fi

if [[ -f "${ROOT_DIR}/dependency_checker.py" ]]; then
  run_dep_check() {
    "${VENV_DIR}/bin/python" "${ROOT_DIR}/dependency_checker.py" --auto --enforce-all
  }

  if [[ "${VERBOSE_STARTUP}" == "1" ]]; then
    printf 'Verifying runtime dependency stack (strict mode)\n'
    if ! run_dep_check; then
      if [[ -f "${ROOT_DIR}/dependency_health_check.py" ]]; then
        printf 'Strict check failed. Attempting runtime dependency auto-repair...\n'
        "${VENV_DIR}/bin/python" "${ROOT_DIR}/dependency_health_check.py" --mode repair
        printf 'Re-running strict dependency check...\n'
        run_dep_check
      else
        exit 1
      fi
    fi
  else
    DEP_LOG="$(mktemp -t kling_depcheck.XXXXXX.log)"
    if run_dep_check >"${DEP_LOG}" 2>&1; then
      printf 'Runtime dependency check: OK\n'
      rm -f "${DEP_LOG}" || true
    else
      if [[ -f "${ROOT_DIR}/dependency_health_check.py" ]]; then
        printf 'Runtime dependency check failed. Attempting auto-repair...\n' >&2
        "${VENV_DIR}/bin/python" "${ROOT_DIR}/dependency_health_check.py" --mode repair || {
          printf 'Runtime dependency check failed. Details below.\n' >&2
          printf 'Tip: use KLING_VERBOSE_STARTUP=1 for live diagnostic output.\n' >&2
          cat "${DEP_LOG}" >&2 || true
          rm -f "${DEP_LOG}" || true
          exit 1
        }
        if run_dep_check >"${DEP_LOG}" 2>&1; then
          printf 'Runtime dependency check: OK (after repair)\n'
          rm -f "${DEP_LOG}" || true
        else
          printf 'Runtime dependency check failed after repair. Details below.\n' >&2
          cat "${DEP_LOG}" >&2 || true
          rm -f "${DEP_LOG}" || true
          exit 1
        fi
      else
        printf 'Runtime dependency check failed. Details below.\n' >&2
        printf 'Tip: use KLING_VERBOSE_STARTUP=1 for live diagnostic output.\n' >&2
        cat "${DEP_LOG}" >&2 || true
        rm -f "${DEP_LOG}" || true
        exit 1
      fi
    fi
  fi
fi

printf '\nEnvironment ready:\n'
printf '  Source: %s\n' "${PYTHON_BIN}"
printf '  Python: %s\n' "${VENV_DIR}/bin/python"
printf '  Venv:   %s\n' "${VENV_DIR}"
