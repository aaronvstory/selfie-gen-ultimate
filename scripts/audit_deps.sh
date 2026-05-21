#!/usr/bin/env bash
# Audit Python dependencies for known CVEs + supply-chain advisories.
# Cross-checks PyPA Advisory DB + OSV.dev via pip-audit.
#
# Exit code 0 = clean. Non-zero = at least one finding.
# Run on every dependency update. CI runs this on every PR.
set -euo pipefail

SCRIPT_DIR="$( cd -- "$( dirname -- "${BASH_SOURCE[0]}" )" &> /dev/null && pwd )"
REPO_ROOT="$( cd -- "${SCRIPT_DIR}/.." &> /dev/null && pwd )"
cd "${REPO_ROOT}"

# Resolve a Python (project venv > .venv311 > python3.11 > python3).
PY=""
for cand in venv/bin/python .venv311/bin/python; do
  if [ -x "${REPO_ROOT}/${cand}" ]; then
    PY="${REPO_ROOT}/${cand}"
    break
  fi
done
if [ -z "$PY" ]; then
  for cand in python3.11 python3.12 python3 python; do
    if command -v "$cand" >/dev/null 2>&1; then
      PY="$cand"
      break
    fi
  done
fi
if [ -z "$PY" ]; then
  echo "FATAL: no Python found (looked for venv, .venv311, python3.{11,12}, python3, python)"
  exit 2
fi

echo "Using Python: $PY ($(${PY} --version 2>&1))"

# Make sure pip-audit is available; if not, install it into the current Python.
if ! "${PY}" -m pip_audit --version >/dev/null 2>&1; then
  echo "Installing pip-audit..."
  "${PY}" -m pip install --quiet --upgrade pip-audit
fi

FAILED=0

# Audit each requirements file independently. --disable-pip skips the
# resolver (we trust the pinned versions); --strict treats EVERY advisory
# as an error, including ones without a fix available.
for req in \
  requirements.txt \
  similarity/requirements.txt \
  similarity/requirements-test.txt \
; do
  if [ ! -f "$req" ]; then
    continue
  fi
  echo
  echo "=== pip-audit: $req ==="
  if ! "${PY}" -m pip_audit -r "$req" --strict --no-deps --progress-spinner off; then
    FAILED=$((FAILED + 1))
  fi
done

# Audit each oldcam-v* requirements file (skip macOS-specific subdirs to
# avoid double-scan of the same content).
for req in oldcam-v*/requirements.txt; do
  [ -f "$req" ] || continue
  echo
  echo "=== pip-audit: $req ==="
  if ! "${PY}" -m pip_audit -r "$req" --strict --no-deps --progress-spinner off; then
    FAILED=$((FAILED + 1))
  fi
done

echo
if [ "$FAILED" -gt 0 ]; then
  echo "=== FAILED: $FAILED requirements file(s) have findings ==="
  echo "See docs/security/HARDENING.md §8 for remediation."
  exit 1
fi
echo "=== All requirements files passed pip-audit. ==="
exit 0
