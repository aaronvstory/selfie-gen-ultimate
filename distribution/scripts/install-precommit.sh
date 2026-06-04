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

# Subagent MEDIUM on b807560 (2026-05-22): the original guard was
# ``[ ! -d "$REPO_ROOT/.git" ]`` which returns fatal for git worktrees
# (where ``.git`` is a FILE pointing at the real gitdir, not a dir).
# Fix: ask git itself for the gitdir — handles plain repos, worktrees,
# and submodules uniformly. We CD into the repo before invoking git
# (rather than ``git -C``) because Git for Windows doesn't accept
# cygwin-style ``/cygdrive/F/...`` paths via ``-C`` but does accept
# them as a cwd via the shell's cd builtin.
if ! GITDIR="$(cd "$REPO_ROOT" && git rev-parse --git-dir 2>/dev/null)"; then
    echo "FATAL: not a git repo at $REPO_ROOT" >&2
    exit 1
fi
# rev-parse --git-dir returns a relative path when run from the repo
# root; resolve to absolute so the hooks/ path below is unambiguous.
case "$GITDIR" in
    /*) ;;                                          # already absolute (rare)
    [A-Za-z]:[/\\]*) ;;                             # absolute Windows path (C:\..., D:/...)
    *) GITDIR="${REPO_ROOT}/${GITDIR}" ;;
esac

# Codex P2 on 49702c0 (2026-05-22): honour ``core.hooksPath`` if set.
# Git lets users redirect hook lookup with ``git config core.hooksPath``
# (repo-local OR global). When set, the hook at ``<gitdir>/hooks/X``
# is IGNORED and git looks at ``$core.hooksPath/X`` instead. Without
# this branch the installer would report success while writing a hook
# git never reads, leaving commits unaudited in environments that use
# lefthook / husky / pre-commit.com or any custom hooksPath.
HOOKS_DIR="$(cd "$REPO_ROOT" && git config --get core.hooksPath 2>/dev/null || true)"
if [ -n "$HOOKS_DIR" ]; then
    # ``git config core.hooksPath`` may return a path relative to the
    # repo's working tree (per git docs: "It is resolved relative to
    # the directory where the git command is run"). Normalise to
    # absolute so the cp below is unambiguous.
    case "$HOOKS_DIR" in
        /*|~*) ;;                                       # absolute / home-relative
        [A-Za-z]:[/\\]*) ;;                             # Windows absolute
        *) HOOKS_DIR="${REPO_ROOT}/${HOOKS_DIR}" ;;
    esac
    # Expand ~ if present (cd handles this naturally).
    case "$HOOKS_DIR" in
        ~*) HOOKS_DIR="$(eval echo "$HOOKS_DIR")" ;;
    esac
    echo "NOTE: core.hooksPath is set — installing to ${HOOKS_DIR}/pre-commit"
    echo "      (not ${GITDIR}/hooks/pre-commit, which git would ignore)"
else
    HOOKS_DIR="${GITDIR}/hooks"
fi
HOOK_DST="${HOOKS_DIR}/pre-commit"

mkdir -p "${HOOKS_DIR}"

# Subagent MEDIUM on b807560 (2026-05-22): the original code backed up
# to ``.bak`` (single slot). Re-running the installer overwrites that
# backup — a developer who had a custom pre-commit (lefthook, husky,
# etc.) and runs the installer twice would lose their original
# permanently because the second run's .bak holds OUR hook, not theirs.
# Fix: timestamp the backup so every run preserves the prior state.
if [ -f "$HOOK_DST" ] && ! cmp -s "$HOOK_SRC" "$HOOK_DST"; then
    backup_ts="$(date -u +%Y%m%dT%H%M%SZ)"
    backup_path="${HOOK_DST}.bak.${backup_ts}"
    echo "NOTE: existing .git/hooks/pre-commit differs from tracked version"
    echo "      backing up to ${backup_path} before overwriting"
    cp "$HOOK_DST" "$backup_path"
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
