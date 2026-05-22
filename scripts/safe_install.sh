#!/usr/bin/env bash
# safe_install.sh — wrap `pip install` / `npm install` with a post-install
# Shai-Hulud / TeamPCP audit.
#
# This is where the real attack moment is: a compromised package executes its
# postinstall script (npm) or its setup.py (pip sdist) the moment it lands on
# disk. Pre-commit hooks don't help here; this wrapper runs the project audit
# AFTER the install finishes, while the bad code is still freshly written.
#
# Usage:
#   ./scripts/safe_install.sh pip install <args>
#   ./scripts/safe_install.sh npm install <args>
#   ./scripts/safe_install.sh pnpm install <args>
#   ./scripts/safe_install.sh yarn add <args>
#
# Or shell aliases (add to ~/.bashrc):
#   alias pipi='~/path/to/safe_install.sh pip install'
#   alias npmi='~/path/to/safe_install.sh npm install'

set -u

REPO_ROOT="$(git rev-parse --show-toplevel 2>/dev/null || pwd)"
RED='\033[31m'; GREEN='\033[32m'; YELLOW='\033[33m'; RESET='\033[0m'

if [[ $# -lt 1 ]]; then
    echo "usage: safe_install.sh <package-manager> <args...>" >&2
    exit 2
fi

echo -e "${YELLOW}[safe-install] running: $*${RESET}"
"$@"
install_code=$?

if [[ $install_code -ne 0 ]]; then
    echo -e "${RED}[safe-install] install failed (exit $install_code) — skipping audit${RESET}" >&2
    exit $install_code
fi

PROJECT_SCRIPT="$REPO_ROOT/scripts/detect_compromise.py"
if [[ ! -f "$PROJECT_SCRIPT" ]]; then
    echo -e "${YELLOW}[safe-install] no detect_compromise.py — audit skipped${RESET}" >&2
    exit 0
fi

PY="python3"
command -v python3 >/dev/null 2>&1 || PY="python"

echo -e "${YELLOW}[safe-install] auditing project after install...${RESET}"
"$PY" "$PROJECT_SCRIPT" --root "$REPO_ROOT"
audit_code=$?

if [[ $audit_code -ge 2 ]]; then
    echo -e "${RED}[safe-install] AUDIT FOUND ALERTS — review immediately${RESET}" >&2
    echo "Recommended next steps:" >&2
    echo "  1. Do NOT run anything in this venv/node_modules" >&2
    echo "  2. Check docs/security/IOC_DETECTION_CHECKLIST.md" >&2
    echo "  3. Run '/hulud-kit quick' for a full machine scan" >&2
    exit $audit_code
elif [[ $audit_code -eq 1 ]]; then
    echo -e "${YELLOW}[safe-install] audit has warnings (install completed)${RESET}" >&2
else
    echo -e "${GREEN}[safe-install] install + audit clean${RESET}"
fi
exit 0
