"""Mini Shai-Hulud / TeamPCP IoC self-check.

Runs a battery of local checks for indicators of compromise from the
TeamPCP supply-chain worm campaign that targeted npm + PyPI in 2026.
Exit code 0 = clean, exit code 1 = found something worth investigating.

Usage:
    python scripts/detect_compromise.py
    python scripts/detect_compromise.py --venv .venv  # also scan a specific venv
    python scripts/detect_compromise.py --github      # also check GitHub repos (needs `gh` CLI)
    python scripts/detect_compromise.py --all         # everything

Companion to docs/security/IOC_DETECTION_CHECKLIST.md (the manual runbook).
This script automates the cheap automatable parts so you can run it weekly.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import List, Tuple

# ── IoC tables ──────────────────────────────────────────────────────────

# Known C2 domains as of 2026-05-21. SOURCE: Microsoft, Snyk, Socket
# advisories. Update from the public threat-intel feeds if this script
# is run after that date.
C2_DOMAINS = (
    "t.m-kosche.com",
    "team-pcp.com",
    "duluh-iahs.xyz",
)

# Reversed campaign marker (the description string the worm uses on its
# exfil repos).
REVERSED_MARKER = "niagA oG eW ereH :duluH-iahS"
FORWARD_MARKER = "Shai-Hulud: Here We Go Again"

# Dune-themed repo name patterns (per JFrog + Microsoft reporting).
DUNE_REPO_PATTERNS = (
    re.compile(r"sayyadina[-_]"),
    re.compile(r"atreides[-_]"),
    re.compile(r"bene[-_]gesserit"),
    re.compile(r"\bmelange[-_]"),
    re.compile(r"\bfremen[-_]"),
    re.compile(r"harkonnen"),
    re.compile(r"kwisatz"),
    re.compile(r"ornithopter"),
    re.compile(r"stillsuit"),
)

# Known-compromised PyPI packages from the May 2026 waves. If any of
# these appear in your dependency tree → immediate yank/replace.
COMPROMISED_PYPI = (
    "durabletask",        # Microsoft Durable Task Python client (TeamPCP wave 4)
    "litellm",            # tampered versions; .pth credential stealer
)

# `.pth` file content patterns that indicate executable code (the
# LiteLLM PyPI compromise technique). A normal `.pth` file is one or
# more directory-path strings, one per line.
#
# CRITICAL: re.MULTILINE is mandatory — a payload that puts a benign-
# looking path on line 1 and the malicious `import`/`exec` on line 2
# bypasses non-multiline anchors completely. (Subagent finding on
# 4cc0bb4 — anchors without MULTILINE only match the start of bytes,
# not the start of each line.)
PTH_EXEC_PATTERNS = (
    re.compile(rb"^\s*import\b", re.MULTILINE),
    re.compile(rb"^\s*exec\b", re.MULTILINE),
    re.compile(rb"^\s*os\.", re.MULTILINE),
    re.compile(rb"^\s*subprocess\.", re.MULTILINE),
    re.compile(rb"^\s*eval\b", re.MULTILINE),
    re.compile(rb"\bhttp[s]?://"),  # network IO from a .pth = suspicious
)

# Allowlist: known-legitimate .pth files that ship executable code.
# Match by filename + EXACT content match against a known-good blob.
#
# CRITICAL: prefix-only matching is a bypass. An attacker who replaces
# `distutils-precedence.pth` with a file that starts with the magic
# bytes and appends malicious code (well within 300 bytes) sails past
# the scanner. The allowlist must match the WHOLE content modulo
# whitespace, not just the prefix. (Subagent CRITICAL on 4cc0bb4.)
PTH_ALLOWLIST = {
    # setuptools ships this to transparently shim distutils for
    # older PEP 517 fallbacks. The exact content has been stable
    # across recent setuptools versions; if it changes upstream we'll
    # update this allowlist after auditing the new content.
    "distutils-precedence.pth": {
        "exact_content_stripped": (
            b"import os; var = 'SETUPTOOLS_USE_DISTUTILS'; "
            b"enabled = os.environ.get(var, 'local') == 'local'; "
            b"enabled and __import__('_distutils_hack').add_shim();"
        ),
    },
}


def _pth_is_allowlisted(pth: Path) -> bool:
    """Return True if a .pth file matches a known-legitimate allowlist
    entry by filename + EXACT content match (whitespace-stripped).
    False = run the exec-pattern scan as normal.

    Subagent CRITICAL on 4cc0bb4: prefix-only matching was a bypass
    vector. Whole-content match closes it.
    """
    entry = PTH_ALLOWLIST.get(pth.name)
    if entry is None:
        return False
    try:
        content = pth.read_bytes()
    except OSError:
        return False
    # Strip leading/trailing whitespace + newlines from BOTH sides so
    # editors that add a trailing newline don't fail the match.
    if content.strip() != entry["exact_content_stripped"].strip():
        return False
    return True


# ── Result helpers ──────────────────────────────────────────────────────


class CheckResult:
    """One check's outcome."""

    def __init__(self, name: str, ok: bool, details: List[str]) -> None:
        self.name = name
        self.ok = ok
        self.details = details

    def render(self) -> str:
        status = "[OK]    " if self.ok else "[ALERT] "
        out = [f"{status}{self.name}"]
        for d in self.details:
            out.append(f"        {d}")
        return "\n".join(out)


def _git(*args: str, cwd: Path | None = None) -> Tuple[int, str, str]:
    """Run a git command; return (rc, stdout, stderr). Never raises."""
    try:
        proc = subprocess.run(
            ["git", *args],
            cwd=str(cwd) if cwd else None,
            capture_output=True,
            text=True,
            timeout=30,
        )
        return proc.returncode, proc.stdout, proc.stderr
    except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
        return 1, "", str(exc)


def _gh(*args: str) -> Tuple[int, str, str]:
    """Run a gh CLI command; same return shape as _git."""
    try:
        proc = subprocess.run(
            ["gh", *args],
            capture_output=True,
            text=True,
            timeout=30,
        )
        return proc.returncode, proc.stdout, proc.stderr
    except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
        return 1, "", str(exc)


# ── Checks ──────────────────────────────────────────────────────────────


def check_compromised_pypi_in_deps(repo_root: Path) -> CheckResult:
    """Look for known-bad PyPI packages in any requirements*.txt."""
    name = "Known-compromised PyPI packages in requirements files"
    hits: List[str] = []
    req_files = list(repo_root.rglob("requirements*.txt"))
    # Filter out venvs + recovery dirs + node_modules + .git (false
    # positives, plus the .git tree slows rglob substantially on big
    # repos — Gemini medium on 3fe4154).
    # Match by exact directory-component name, NOT substring — otherwise
    # a legitimate project dir named `my-venv-project` or a file named
    # `requirements-dist.txt` would be silently excluded. (Gemini HIGH
    # on 9a20e14.)
    # Use RELATIVE-TO-REPO parts so a parent directory of the repo
    # named ``dist`` / ``venv`` (e.g. cloning into
    # ``C:\dist\projects\…``) doesn't disable the scan for everything
    # underneath. Gemini security-high on 3fe4154.
    exclude_dirs = {".venv", "venv", "site-packages", ".recovery",
                    "node_modules", ".sandbox-venv", "dist", ".git"}

    def _excluded(p: Path) -> bool:
        try:
            rel_parts = p.relative_to(repo_root).parts
        except ValueError:
            rel_parts = p.parts  # outside repo_root — fall back
        return any(part in exclude_dirs for part in rel_parts)

    req_files = [p for p in req_files if not _excluded(p)]
    for req in req_files:
        try:
            text = req.read_text(encoding="utf-8", errors="replace").lower()
        except OSError:
            continue
        for bad in COMPROMISED_PYPI:
            # Match every legal PEP 508 form for declaring this package:
            #   bare name:                  `litellm`
            #   name with extras:           `litellm[proxy]`
            #   name + version specifier:   `litellm==1.30.0`
            #   name + environment marker:  `litellm; python_version<"3.12"`
            #   direct reference URL:       `litellm @ https://...`
            #   trailing comment:           `litellm  # note`
            # The post-name group accepts ANY of:
            #   - `[` (extras start)
            #   - version operators (=, <, >, !, ~)
            #   - `;` (marker separator)
            #   - `@` (direct reference)
            #   - `#` (trailing comment)
            #   - end of line
            # (Gemini + Codex on 0e16c8d caught the comment + marker
            # + direct-ref bypasses.)
            # PEP 508 allows whitespace between the package name and
            # the extras bracket (``litellm [proxy]`` is legal).
            # Gemini medium on d53c64f.
            if re.search(
                rf"^\s*{re.escape(bad)}\s*(?:\[[^\]]+\])?\s*(?:[=<>!~;@#]|$)",
                text,
                re.MULTILINE,
            ):
                hits.append(f"{req.relative_to(repo_root)} references {bad!r}")
    if hits:
        return CheckResult(name, ok=False, details=hits)
    return CheckResult(name, ok=True, details=[
        f"Scanned {len(req_files)} requirements files, none reference "
        f"the known-compromised packages: {', '.join(COMPROMISED_PYPI)}.",
    ])


def check_pth_files_for_exec_code(venv_paths: List[Path]) -> CheckResult:
    """Scan .pth files in site-packages for executable code.

    Normal .pth files contain only directory paths (one per line).
    A .pth file with `import`, `exec`, `os.`, `subprocess`, etc.
    is the LiteLLM-style attack vector — Python auto-executes it on
    every `import site`.
    """
    name = "`.pth` files in site-packages with executable code"
    hits: List[str] = []
    scanned = 0
    allowlisted = 0
    for venv in venv_paths:
        if not venv.exists():
            continue
        # Walk ALL .pth files under the venv that live anywhere in a
        # site-packages tree, not just directly in site-packages/. A
        # payload that places its .pth in a package sub-directory
        # (`site-packages/<pkg>/attack.pth`) would otherwise evade
        # the scanner. (Subagent HIGH on 4cc0bb4.)
        for pth in venv.rglob("*.pth"):
            # Case-fold via as_posix().lower() so a custom Python
            # install with ``Site-Packages`` capitalisation still
            # matches. Gemini medium on 3fe4154.
            if "site-packages" not in pth.as_posix().lower():
                continue
            scanned += 1
            # Allowlisted setuptools-shipped .pth files are skipped —
            # they ship executable code legitimately. The allowlist
            # is content-fingerprinted so a malicious version that
            # KEEPS the filename but changes content still alerts.
            if _pth_is_allowlisted(pth):
                allowlisted += 1
                continue
            try:
                content = pth.read_bytes()
            except OSError:
                continue
            for pat in PTH_EXEC_PATTERNS:
                if pat.search(content):
                    hits.append(
                        f"{pth}: matches {pat.pattern.decode('utf-8', 'replace')!r}"
                    )
                    break  # one hit per file is enough
    if not venv_paths:
        return CheckResult(name, ok=True, details=["No venv paths provided; skipped."])
    if hits:
        return CheckResult(name, ok=False, details=hits[:20])
    summary = f"Scanned {scanned} .pth files, none contain executable code."
    if allowlisted:
        summary += f" ({allowlisted} allowlisted as known-legitimate setuptools shims.)"
    return CheckResult(name, ok=True, details=[summary])


_REMOTE_URL_AUTHORITY_RE = re.compile(
    r"(?P<scheme>[a-zA-Z][\w+.\-]*://)(?P<auth>[^/@\s]+@)"
)


def _redact_remote_line(line: str) -> str:
    """Strip embedded credentials from a ``git remote -v`` line.

    Tokenised HTTPS remotes look like
    ``origin\thttps://x-access-token:ghp_xxx@github.com/...``. Printing
    them verbatim leaks the bearer secret into logs / CI output. Per
    Codex P1 on 2ced5b6: replace the ``user[:pass]@`` segment with
    ``<REDACTED>@`` before rendering. SSH remotes
    (``git@github.com:owner/repo``) don't carry creds and are passed
    through untouched.
    """
    return _REMOTE_URL_AUTHORITY_RE.sub(r"\g<scheme><REDACTED>@", line)


def check_git_remotes_for_c2(repo_root: Path) -> CheckResult:
    """Check the local git remote URLs for known C2 domains."""
    name = "Git remotes don't reference known C2 domains"
    rc, out, err = _git("remote", "-v", cwd=repo_root)
    if rc != 0:
        return CheckResult(name, ok=True, details=[
            "git remote check skipped (no git or no repo).",
        ])
    hits: List[str] = []
    for line in out.splitlines():
        for c2 in C2_DOMAINS:
            if c2 in line.lower():
                hits.append(
                    f"remote line: {_redact_remote_line(line.strip())}  matches {c2!r}"
                )
    if hits:
        return CheckResult(name, ok=False, details=hits)
    # Clean path: render only redacted lines too. Codex P1 on 2ced5b6.
    redacted_lines = [
        _redact_remote_line(s) for s in (out.strip().splitlines()[:3] if out else [])
    ]
    return CheckResult(name, ok=True, details=[
        f"Checked git remotes: {redacted_lines if redacted_lines else 'none'}",
    ])


def check_workflows_for_suspicious_commits(repo_root: Path) -> CheckResult:
    """Check .github/workflows/ for recently-modified files with curl|bash patterns."""
    name = "GitHub workflows don't have curl-pipe-bash or wget-pipe-sh"
    wf_dir = repo_root / ".github" / "workflows"
    if not wf_dir.is_dir():
        return CheckResult(name, ok=True, details=[
            "No .github/workflows/ directory; skipped.",
        ])
    hits: List[str] = []
    # Patterns must catch the most common pipe-to-shell payload
    # delivery variants. Per Gemini MEDIUM on 0e16c8d:
    #   - Allow an optional ``sudo`` before the shell (sudo-pipe
    #     is the canonical curl|bash form on hostile tutorials).
    #   - Match all three common shells (``sh``, ``bash``, ``zsh``)
    #     not just ``bash`` in the process-substitution variant.
    #   - Catch backtick-form eval ``eval `curl ...``` in addition
    #     to ``eval $(curl ...)``.
    bad_patterns = (
        # Targets: `sh`, `bash`, `zsh`, `python[3][.N][.N]`.
        # Optional path prefix BEFORE sudo: catches ``/usr/bin/sudo bash``
        # and ``/bin/bash``. Optional ``sudo`` prefix THEN optional
        # path prefix BEFORE the shell name: catches ``sudo /bin/bash``.
        # Word boundary `\b` after the shell name prevents matching
        # ``bashfoo`` as a false positive.
        # (Gemini security-medium on 9a20e14 + 03d05e5.)
        # The optional sudo section accepts arbitrary args before
        # the shell name (``sudo -E bash``, ``sudo -u root bash``,
        # ``sudo --preserve-env bash``, etc.) — Gemini medium on
        # d53c64f. The pattern is permissive on what comes between
        # ``sudo`` and the shell (any non-pipe characters), then
        # requires the shell name with a word-boundary. This is
        # slightly broader than strictly correct sudo grammar but
        # the false-positive cost is low (it would require a
        # legitimate workflow line containing ``sudo`` + a shell
        # name + a pipe, which is itself suspicious).
        # Multi-pipe support — attackers obfuscate the curl|bash
        # chain with intermediate commands like ``curl ... | grep
        # ... | bash`` or ``curl ... | tee /tmp/x | sh``. Gemini
        # security-medium on 2ced5b6. Allow zero or more
        # intermediate ``[non-pipe-content]|`` chunks before the
        # final shell target. The ``[^|\n]+`` inside each chunk
        # bounds against the line.
        re.compile(
            r"curl\s+[^|\n]+\|(?:\s*[^|\n]+\|)*\s*"
            r"(?:[\w/.+\-]+/)?(?:sudo(?:\s[^|\n]*?)?\s+)?(?:[\w/.+\-]+/)?"
            r"(sh|bash|zsh|python[\d.]*)\b"
        ),
        # Broader wget detector — Codex P1 on 15bd7bb. Drop the
        # ``-O-`` requirement: ``wget https://evil | sh`` is just as
        # dangerous (wget writes to disk and a follow-up reads it,
        # OR a redirect / process substitution wrapper feeds it
        # through). The ``-O-`` / ``-qO-`` / ``--output-document=-``
        # forms are all subsumed by the generic ``wget <args> |``.
        re.compile(
            r"wget\s+[^|\n]+\|(?:\s*[^|\n]+\|)*\s*"
            r"(?:[\w/.+\-]+/)?(?:sudo(?:\s[^|\n]*?)?\s+)?(?:[\w/.+\-]+/)?"
            r"(sh|bash|zsh|python[\d.]*)\b"
        ),
        re.compile(
            r"(?:[\w/.+\-]+/)?(sh|bash|zsh|python[\d.]*)"
            r"\s*<\(\s*(?:curl|wget)"
        ),
        # Allow optional whitespace inside BOTH subshell forms.
        # `eval $( curl ...)` was Codex-P2 on 9a20e14;
        # `eval `` curl ...`` ` whitespace was Gemini on 03d05e5.
        re.compile(
            r"eval\s*(?:\$\(\s*(?:curl|wget)|`\s*(?:curl|wget))"
        ),
    )
    # GitHub Actions accepts BOTH `.yml` and `.yaml` extensions. A
    # scanner that only checks `.yml` has a false-negative gap on
    # `.yaml`-style workflows. (CodeRabbit major on 3fe4154.)
    workflow_files = list(wf_dir.glob("*.yml")) + list(wf_dir.glob("*.yaml"))
    for wf in workflow_files:
        try:
            text = wf.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        # Collapse POSIX shell line-continuations (``\<newline>``)
        # before scanning. A common evasion is to wrap the payload
        # across lines:
        #     run: |
        #       curl https://evil \
        #         | bash
        # The existing patterns are line-bound (``[^|\n]+``) and miss
        # this. Joining the continuation with a single space restores
        # the logical line so the patterns match. Codex P1 on
        # 15bd7bb.
        text_for_scan = re.sub(r"\\\r?\n\s*", " ", text)
        for pat in bad_patterns:
            m = pat.search(text_for_scan)
            if m:
                hits.append(f"{wf.name}: matches {pat.pattern!r}: {m.group(0)[:80]}")
    if hits:
        return CheckResult(name, ok=False, details=hits)
    return CheckResult(name, ok=True, details=[
        f"Scanned {len(workflow_files)} workflow files; "
        "no curl|bash or wget|sh patterns found.",
    ])


def check_github_repos_for_marker() -> CheckResult:
    """Check the authenticated GitHub account for repos with the reversed
    "Shai-Hulud" marker in their description or with Dune-themed names.

    Requires the `gh` CLI to be installed and authenticated.
    """
    name = "GitHub account has no attacker-created exfil repos"
    # First check whether gh is available + authenticated.
    rc, out, err = _gh("auth", "status")
    if rc != 0:
        return CheckResult(name, ok=True, details=[
            "`gh` CLI not available or not authenticated; skipped.",
            "Install: https://cli.github.com/",
        ])
    # List user's repos. Limit raised to 1000 (Gemini medium on
    # 3fe4154) so accounts with many side projects don't have an
    # un-scanned tail. `gh` will hit pagination automatically.
    rc, out, err = _gh(
        "repo", "list", "--limit", "1000",
        "--json", "name,description,visibility,createdAt",
    )
    if rc != 0:
        return CheckResult(name, ok=False, details=[
            f"gh repo list failed: {err.strip() or 'unknown error'}",
        ])
    try:
        repos = json.loads(out)
    except json.JSONDecodeError:
        return CheckResult(name, ok=False, details=[
            "Could not parse gh repo list output.",
        ])
    hits: List[str] = []
    for repo in repos:
        n = (repo.get("name") or "").lower()
        d = (repo.get("description") or "").lower()
        if REVERSED_MARKER.lower() in d or FORWARD_MARKER.lower() in d:
            hits.append(f"REPO MARKER MATCH: {repo['name']} ({repo.get('description')!r})")
        for pat in DUNE_REPO_PATTERNS:
            if pat.search(n):
                hits.append(f"REPO DUNE-NAME MATCH: {repo['name']} ({repo.get('description')!r})")
                break
    if hits:
        return CheckResult(name, ok=False, details=hits)
    return CheckResult(name, ok=True, details=[
        f"Scanned {len(repos)} GitHub repos; none match the campaign markers "
        "or Dune-themed naming patterns.",
    ])


# ── Main ────────────────────────────────────────────────────────────────


def main(argv: List[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument(
        "--venv",
        action="append",
        default=[],
        help="Venv directory to scan for .pth files. Can be passed multiple times. "
        "Default: scan venv/, .venv/, .venv311/, similarity/.venv/ if present.",
    )
    p.add_argument(
        "--github",
        action="store_true",
        help="Also scan the authenticated GitHub account for exfil repos.",
    )
    p.add_argument(
        "--all",
        action="store_true",
        help="Equivalent to --github and scanning all auto-detected venvs.",
    )
    p.add_argument(
        "--repo-root",
        default=".",
        help="Project root (default: current dir).",
    )
    args = p.parse_args(argv)

    repo_root = Path(args.repo_root).resolve()

    # Auto-detect venvs if none provided. Includes the subproject
    # venvs (similarity/, resemble-score/) AND every oldcam-v* version
    # that may have its own .venv (Gemini medium on 03d05e5 —
    # oldcam subdirs are first-class parts of the project structure
    # and host their own dependency trees; missing them means a
    # poisoned .pth there would evade the scanner).
    venv_paths = [Path(v).resolve() for v in args.venv]
    if not venv_paths or args.all:
        for candidate in (
            "venv", ".venv", ".venv311",
            "similarity/.venv",
            "resemble-score/.venv",
        ):
            p_candidate = repo_root / candidate
            if p_candidate.exists() and p_candidate not in venv_paths:
                venv_paths.append(p_candidate)
        # oldcam-v* venvs (any version × any common venv name).
        # The root auto-detect block above checks for `venv`,
        # `.venv`, and `.venv311`; mirror that here so we catch the
        # poisoned-.pth attack regardless of which naming the user
        # picked when bootstrapping oldcam-v* deps. Gemini medium on
        # d53c64f.
        for p_dir in repo_root.glob("oldcam-v*"):
            if not p_dir.is_dir():
                continue
            for candidate in ("venv", ".venv", ".venv311"):
                p_candidate = p_dir / candidate
                if p_candidate.exists() and p_candidate not in venv_paths:
                    venv_paths.append(p_candidate)

    do_github = args.github or args.all

    print(f"Mini Shai-Hulud IoC self-check — {repo_root}")
    def _disp(p: Path) -> str:
        """Display path relative-to-repo if possible, else absolute.
        Avoids ValueError when --venv points at /opt/shared-venv or
        any path outside the repo. (Subagent LOW on 4cc0bb4.)"""
        try:
            return str(p.relative_to(repo_root))
        except ValueError:
            return str(p)
    print(f"  venvs to scan: {[_disp(v) for v in venv_paths] or 'none'}")
    print(f"  GitHub scan: {'ON' if do_github else 'OFF (use --github to enable)'}")
    print()

    results: List[CheckResult] = []
    results.append(check_compromised_pypi_in_deps(repo_root))
    results.append(check_pth_files_for_exec_code(venv_paths))
    results.append(check_git_remotes_for_c2(repo_root))
    results.append(check_workflows_for_suspicious_commits(repo_root))
    if do_github:
        results.append(check_github_repos_for_marker())

    for r in results:
        print(r.render())
        print()

    alerts = [r for r in results if not r.ok]
    if alerts:
        print(f"=== {len(alerts)} ALERT(S) — see docs/security/IOC_DETECTION_CHECKLIST.md ===")
        return 1
    print("=== All checks passed. Continue running this weekly. ===")
    return 0


if __name__ == "__main__":
    sys.exit(main())
