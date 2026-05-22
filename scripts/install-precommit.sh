#!/usr/bin/env bash
# install-precommit.sh — install the supply-chain audit pre-commit hook.
#
# Copies scripts/git-hooks/pre-commit to .git/hooks/pre-commit and sets
# the exec bit. Idempotent — safe to re-run.
#
# Why a separate installer: .git/hooks/ is NOT versioned (git design),
# so a `git pull` on a fresh clone (e.g. your macOS box after cloning)
# does NOT install the hook automatically. This script bridges that
# gap. Run it once after every fresh clone, or whenever you want to
# refresh the hook body to the latest tracked version.
#
# Usage:
#   bash scripts/install-precommit.sh
#
# Or from any platform with bash (macOS, Linux, Git Bash on Windows).
# On a fresh Windows clone, run from Git Bash. There is no .bat
# variant because Windows users get the hook via the SAME bash file —
# git on Windows uses MSYS bash for hooks regardless of host shell.

set -euo pipefail

SCRIPT_DIR="$( cd -- "$( dirname -- "${BASH_SOURCE[0]}" )" &> /dev/null && pwd )"
REPO_ROOT="$( cd -- "${SCRIPT_DIR}/.." &> /dev/null && pwd )"
cd "${REPO_ROOT}"

HOOK_SRC="${REPO_ROOT}/scripts/git-hooks/pre-commit"
HOOK_DST="${REPO_ROOT}/.git/hooks/pre-commit"

if [ ! -f "$HOOK_SRC" ]; then
    echo "FATAL: $HOOK_SRC not found — are you running this from the repo root?" >&2
    exit 1
fi

if [ ! -d "${REPO_ROOT}/.git" ]; then
    echo "FATAL: not a git repo (no .git/ at $REPO_ROOT)" >&2
    exit 1
fi

mkdir -p "${REPO_ROOT}/.git/hooks"

# If a hook already exists and differs from our tracked version, warn
# but proceed (the user can always restore their own version from git).
if [ -f "$HOOK_DST" ] && ! cmp -s "$HOOK_SRC" "$HOOK_DST"; then
    echo "NOTE: replacing existing .git/hooks/pre-commit (current version backed up to .git/hooks/pre-commit.bak)"
    cp "$HOOK_DST" "${HOOK_DST}.bak"
fi

cp "$HOOK_SRC" "$HOOK_DST"
chmod +x "$HOOK_DST"

echo "Installed pre-commit hook -> $HOOK_DST"
echo "Exec bit: $(ls -la "$HOOK_DST" | awk '{print $1}')"
echo ""
echo "What it does (fast, ~1-2s on typical commits):"
echo "  - Skips the scan entirely unless your commit touches a dep manifest,"
echo "    lockfile, .github/workflows/*.yml, .pth file, or .claude/*.{js,mjs,json}"
echo "  - When dep files change, runs scripts/detect_compromise.py (stdlib-only,"
echo "    no network) which checks PEP-508 deps, .pth payloads, workflow tamper,"
echo "    git remotes, campaign markers, and more"
echo ""
echo "To skip in an emergency: git commit --no-verify"
echo "To force a full scan even on non-dep commits: SHAI_HULUD_FORCE=1 git commit ..."
echo "To re-enable the slow machine-wide audit: SHAI_HULUD_MACHINE_AUDIT=1 git commit ..."
