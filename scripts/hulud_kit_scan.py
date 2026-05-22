#!/usr/bin/env python3
"""
detect_compromise.py — project-level supply chain audit (Python).

v1.1 — adds Claude Code persistence detection, SARIF output, expanded IOC list.

Nine checks, one exit code:
  1. Compromised PyPI packages in any requirements*.txt (PEP 508 regex)
  2. Compromised npm packages in package.json / package-lock.json
  3. .pth files in site-packages with executable code (persistence)
  4. GitHub workflow files with curl|bash / pipe-to-shell / token exfil
  5. Git remote URLs against known C2 domains (with token redaction)
  6. Claude Code / VSCode persistence files (.claude/execution.js, etc.)
  7. Spoofed git commit authors (claude@..., dependabot[bot]@...)
  8. Campaign string markers in any file (LongLiveTheResistance..., etc.)
  9. Reversed-marker self-check

Exits 0 (clean), 1 (warnings), 2 (alerts).
No external dependencies. Stdlib only. Python 3.8+. Cross-platform.

Usage:
    python scripts/detect_compromise.py
    python scripts/detect_compromise.py --json
    python scripts/detect_compromise.py --sarif results.sarif
    python scripts/detect_compromise.py --root /path/to/repo

IOC data is sourced primarily from copyleftdev/mini-shai-hulud-dragnet
(CC-BY-4.0) plus OSV.dev advisories. Update the constants below as new
waves are disclosed.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import sys
from dataclasses import dataclass, asdict, field
from pathlib import Path
from typing import Iterable, Optional

# --- Marker (spelled in reverse to prevent AI auto-fixers from removing it) ---
# This file scans for the IoC string "Shai-Hulud: Here We Go Again". Storing
# it reversed prevents the scanner from flagging itself, and also defends
# against an automated linter or AI auto-fixer that thinks the literal is
# malicious and "cleans" it. Don't paraphrase the comment beyond what's
# needed — the test suite checks for the exact reversed string below.
MARKER_REVERSED = "niagA oG eW ereH :duluH-iahS"


# ============================================================================
# IOC data — updated from copyleftdev/mini-shai-hulud-dragnet (CC-BY-4.0)
# Last updated: 2026-05-21
# ============================================================================

# PyPI packages confirmed malicious (May 2026 wave).
COMPROMISED_PYPI: frozenset[str] = frozenset({
    "durabletask",
    "fast-agent-mcp",
})

# npm packages — name -> set of compromised versions.
# Empty set means "all versions until further notice".
COMPROMISED_NPM: dict[str, frozenset[str]] = {
    "mbt": frozenset({"1.2.48"}),
    "@cap-js/sqlite": frozenset({"2.2.2"}),
    "@cap-js/postgres": frozenset({"2.2.2"}),
    "@cap-js/db-service": frozenset({"2.10.1"}),
    "@bitwarden/cli": frozenset({"2026.4.0"}),
    # AntV wave (May 19, 2026) — broad coverage
    "@antv/g2": frozenset(), "@antv/g6": frozenset(), "@antv/x6": frozenset(),
    "@antv/l7": frozenset(), "@antv/s2": frozenset(), "@antv/f2": frozenset(),
    "@antv/g": frozenset(), "@antv/g2plot": frozenset(),
    "@antv/graphin": frozenset(), "@antv/data-set": frozenset(),
    "@antv/scale": frozenset(),
    # TanStack wave (May 11, 2026)
    "@tanstack/react-query": frozenset(), "@tanstack/vue-query": frozenset(),
    "@tanstack/query-core": frozenset(), "@tanstack/react-table": frozenset(),
    "@tanstack/table-core": frozenset(), "@tanstack/react-virtual": frozenset(),
    "@tanstack/virtual-core": frozenset(), "@tanstack/react-router": frozenset(),
    "@tanstack/router-core": frozenset(), "@tanstack/react-form": frozenset(),
    "@tanstack/form-core": frozenset(), "@tanstack/store": frozenset(),
    # Others
    "echarts-for-react": frozenset(), "timeago.js": frozenset(),
    "size-sensor": frozenset(), "canvas-nest.js": frozenset(),
    "@mistralai/mistralai": frozenset(), "@squawk/squawk": frozenset(),
}

C2_DOMAINS: frozenset[str] = frozenset({
    "t.m-kosche.com",
    "audit.checkmarx.cx",
    "checkmarx.cx",            # apex of typosquat
    "npm.componentjs.com",
    "registry.npmjs.cx",
    "duluh-iahs.xyz",
    "team-pcp.com",
})

C2_IPS: frozenset[str] = frozenset({
    "94.154.172.43",  # AS209101 IP Vendetta Inc. — offshore bulletproof
})

# Known-malicious file SHA256s. detect_compromise.py only checks these for
# files dropped at the persistence paths below (it doesn't hash every file
# in the repo — that's the deep audit script's job).
MALICIOUS_SHA256: frozenset[str] = frozenset({
    "4066781fa830224c8bbcc3aa005a396657f9c8f9016f9a64ad44a9d7f5f45e34",  # setup.mjs loader
    "80a3d2877813968ef847ae73b5eeeb70b9435254e74d7f07d8cf4057f0a710ac",  # mbt execution.js
    "6f933d00b7d05678eb43c90963a80b8947c4ae6830182f89df31da9f568fea95",  # @cap-js/sqlite execution.js
    "18f784b3bc9a0bcdcb1a8d7f51bc5f54323fc40cbd874119354ab609bef6e4cb",  # variant
    "8605e365edf11160aad517c7d79a3b26b62290e5072ef97b102a01ddbb343f14",  # variant
    "167ce57ef59a32a6a0ef4137785828077879092d7f83ddbc1755d6e69116e0ad",  # variant
    "2a6a35f06118ff7d61bfd36a5788557b695095e7c9a609b4a01956883f146f50",  # kics elf
    "24680027afadea90c7c713821e214b15cb6c922e67ac01109fb1edb3ee4741d9",  # mcpAddon.js
})

# Persistence file paths — relative to repo root.
# Format: (path, level, description, optional content_keyword)
# If content_keyword is set, the file is only flagged if it contains that
# string. This is to avoid false-positives on legitimate .claude/settings.json
# and .vscode/tasks.json files.
PERSISTENCE_PATHS: list[tuple[str, str, str, Optional[str]]] = [
    # Filename alone is the IoC — these names are not used in legitimate setups.
    (".claude/execution.js", "ALERT", "Claude Code persistence payload (TeamPCP)", None),
    (".claude/setup.mjs", "ALERT", "Claude Code persistence loader (TeamPCP)", None),
    (".vscode/setup.mjs", "ALERT", "VSCode persistence loader (TeamPCP)", None),
    # File is legitimate, but specific properties indicate compromise.
    (".claude/settings.json", "WARN", "Claude Code hook config", "SessionStart_hook"),
    (".vscode/tasks.json", "WARN", "VSCode task config", "runOn"),
    # Workflow injection
    (".github/workflows/format-check.yml", "WARN", "Suspicious workflow filename (TeamPCP signature)", None),
]

# Strings whose presence in any tracked file is a strong IoC.
CAMPAIGN_MARKERS: list[tuple[str, str]] = [
    ("LongLiveTheResistanceAgainstMachines", "TeamPCP persistence marker"),
    ("A Mini Shai-Hulud has Appeared", "TeamPCP dropbox marker"),
    ("Exiting as russian language detected!", "TeamPCP geo-evasion string"),
    ("beautifulcastle", "TeamPCP persistence commit prefix"),
    ("__DAEMONIZED", "TeamPCP runtime flag"),
    # The reversed marker would self-match, so we don't include the forward form.
]

# Git author emails used to spoof legitimate commits.
SPOOFED_AUTHORS: frozenset[str] = frozenset({
    "claude@users.noreply.github.com",
    "dependabot[bot]@users.noreply.github.com",
})


# ============================================================================
# Regex
# ============================================================================

def compromised_pypi_pattern(bad: str) -> re.Pattern[str]:
    """PEP 508 name match — covers bare, extras, versioned, env-marker,
    direct-ref, comment forms.

    Retained for backwards compatibility; the live check uses PEP 503
    canonicalization (see ``canonicalize_name``) so that ``fast_agent_mcp``
    and ``fast.agent.mcp`` match the ``fast-agent-mcp`` IOC entry.
    """
    return re.compile(
        rf"^\s*{re.escape(bad)}\s*(?:\[[^\]]+\])?\s*(?:[=<>!~;@#]|$)",
        re.IGNORECASE,
    )


# PEP 503 name normalization: lowercase + collapse runs of [-_.] to single '-'.
# PyPI treats `fast-agent-mcp`, `fast_agent_mcp`, and `fast.agent.mcp` as the
# same project, so the matcher must too.
# See https://packaging.python.org/en/latest/specifications/name-normalization/
_CANONICALIZE_RE = re.compile(r"[-_.]+")


def canonicalize_name(name: str) -> str:
    return _CANONICALIZE_RE.sub("-", name).lower()


# Pre-canonicalize the IOC set once at import time.
COMPROMISED_PYPI_CANONICAL: frozenset[str] = frozenset(
    canonicalize_name(n) for n in COMPROMISED_PYPI
)


def canonicalize_npm_name(name: str) -> str:
    """npm package names are case-insensitive on the registry — lowercase
    is the canonical form. (Unlike PyPI, npm does not collapse '-_.' runs;
    those characters are independently meaningful in the name.)"""
    return name.lower()


# Pre-canonicalize the npm IOC set so a lookup is O(1) even on lowercased
# names from a package-lock that uppercases the scope or org segment.
COMPROMISED_NPM_CANONICAL: dict[str, frozenset[str]] = {
    canonicalize_npm_name(k): v for k, v in COMPROMISED_NPM.items()
}

# Match a PEP 508 requirement line and capture the project name only.
# Group 1 = name. The remainder (extras, version spec, marker, direct ref,
# trailing comment) is intentionally not captured — once the name is in hand
# we only need to canonicalize and compare.
# Tolerates empty `[]` extras (bypass observed during review) and whitespace
# anywhere between name / extras / spec.
# Name pattern matches PEP 508: must START and END with alphanumeric, with
# `.`/`_`/`-` allowed in the middle. A single-character name is also valid.
_REQ_NAME_RE = re.compile(
    r"^\s*([A-Za-z0-9](?:[A-Za-z0-9._\-]*[A-Za-z0-9])?)\s*"
    r"(?:\[[^\]]*\])?\s*"
    r"(?:[=<>!~;@#].*|\#.*)?$"
)


WORKFLOW_PATTERNS: list[re.Pattern[str]] = [
    re.compile(
        r"curl\s+[^|\n]+\|(?:\s*[^|\n]+\|)*\s*"
        r"(?:[\w/.+\-]+/)?(?:sudo(?:\s[^|\n]*?)?\s+)?(?:[\w/.+\-]+/)?"
        r"(sh|bash|zsh|python[\d.]*)\b"
    ),
    re.compile(
        r"wget\s+[^|\n]+-O-?\s*\|(?:\s*[^|\n]+\|)*\s*"
        r"(?:[\w/.+\-]+/)?(?:sudo(?:\s[^|\n]*?)?\s+)?(?:[\w/.+\-]+/)?"
        r"(sh|bash|zsh|python[\d.]*)\b"
    ),
    re.compile(r"base64\s+(-d|--decode)"),
    re.compile(r"eval\s+(\`|\$\()"),
    re.compile(r"ACTIONS_RUNTIME_TOKEN|ACTIONS_CACHE_URL"),
]

PTH_EXEC_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r"^\s*import\s+", re.MULTILINE),
    re.compile(r"^\s*exec\s*\(", re.MULTILINE),
    re.compile(r"^\s*subprocess", re.MULTILINE),
    re.compile(r"^\s*eval\s*\(", re.MULTILINE),
    re.compile(r"^\s*compile\s*\(", re.MULTILINE),
    re.compile(r"^\s*__import__\s*\(", re.MULTILINE),
]

PTH_ALLOWLIST_EXACT: frozenset[str] = frozenset({
    "import sys; sys.__plen = len(sys.path)",
    "",
})

# SHA256 of stripped() bytes of known-good .pth shims. Real
# distutils-precedence.pth contains an `import os; ...
# __import__('_distutils_hack').add_shim()` shim that trips
# PTH_EXEC_PATTERNS but is legitimate. Hashes vary across setuptools
# versions, so we keep a frozenset; extend it with:
#   python -c "import hashlib,sys;print(hashlib.sha256(open(sys.argv[1],'rb').read().decode('utf-8').strip().encode('utf-8')).hexdigest())" path/to/x.pth
PTH_ALLOWLIST_SHA256: frozenset[str] = frozenset({
    # setuptools distutils-precedence.pth, observed forms
    "2f70c2fa9227e9db9348215d9c7b246d2786aac7516f86d71a5952c7c225aa16",
    # virtualenv 20.x _virtualenv.pth — content is literally `import _virtualenv`
    "69ac3d8f27e679c81b94ab30b3b56e9cd138219b1ba94a1fa3606d5a76a1433d",
})

# Filenames that are categorically legitimate shims when matched against
# the SHA256 or content-pattern allowlist. Defense in depth — even if an
# attacker dropped a file with a colliding hash, an unexpected filename
# still alerts. Keep this list narrow: only add a name when at least one
# hash or content pattern actually exists for it.
PTH_ALLOWLIST_NAMES: frozenset[str] = frozenset({
    "distutils-precedence.pth",
    "_virtualenv.pth",
})

# Content-pattern allowlist — durable fallback for files whose SHA256 isn't
# yet in PTH_ALLOWLIST_SHA256 but whose content matches a canonical legit
# shim shape. Each pattern must be tight enough that an attacker can't
# trivially mimic it. Currently: the setuptools distutils-precedence shim,
# matched against the full stripped content (anchored). Whitespace tolerated.
PTH_ALLOWLIST_PATTERNS: list[re.Pattern[str]] = [
    # setuptools distutils-precedence.pth (any version 68.x..80.x observed)
    re.compile(
        r"\Aimport\s+os\s*;\s*"
        r"var\s*=\s*['\"]SETUPTOOLS_USE_DISTUTILS['\"]\s*;\s*"
        r"enabled\s*=\s*os\.environ\.get\(\s*var\s*,\s*['\"]local['\"]\s*\)"
        r"(?:\s*\.lower\(\)\s*in\s*\([^)]+\)|\s*==\s*['\"]local['\"])\s*;\s*"
        r"enabled\s+and\s+__import__\(\s*['\"]_distutils_hack['\"]\s*\)\.add_shim\(\s*\)\s*;?\s*\Z",
        re.MULTILINE,
    ),
]

_REMOTE_URL_AUTHORITY_RE = re.compile(
    r"(?P<scheme>[a-zA-Z][\w+.\-]*://)(?P<auth>[^/@\s]+@)"
)


def _redact_remote_line(line: str) -> str:
    return _REMOTE_URL_AUTHORITY_RE.sub(r"\g<scheme><REDACTED>@", line)


# ============================================================================
# Finding model
# ============================================================================

@dataclass
class Finding:
    section: str
    type: str
    level: str  # ALERT | WARN | INFO
    message: str
    details: dict = field(default_factory=dict)

    def as_dict(self) -> dict:
        return asdict(self)


# ============================================================================
# Path walkers
# ============================================================================

SKIP_PARTS = {
    "site-packages", ".venv", "venv", ".venv311", "env", ".tox",
    "node_modules", ".sandbox-venv", "__pycache__", ".git",
}


def walk_requirements(root: Path) -> Iterable[Path]:
    for path in root.rglob("requirements*.txt"):
        if any(p in SKIP_PARTS for p in path.parts):
            continue
        yield path


def walk_package_jsons(root: Path) -> Iterable[Path]:
    """Yield package.json and package-lock.json files outside node_modules."""
    for name in ("package.json", "package-lock.json"):
        for path in root.rglob(name):
            if any(p in SKIP_PARTS for p in path.parts):
                continue
            yield path


def walk_text_files(root: Path, max_size: int = 2_000_000) -> Iterable[Path]:
    """Yield small text files for content-marker scanning."""
    text_exts = {".py", ".js", ".ts", ".jsx", ".tsx", ".mjs", ".cjs",
                 ".json", ".yml", ".yaml", ".sh", ".ps1", ".bat", ".md",
                 ".txt", ".env", ".toml", ".cfg", ".ini"}
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        if path.suffix.lower() not in text_exts:
            continue
        if any(p in SKIP_PARTS for p in path.parts):
            continue
        try:
            if path.stat().st_size > max_size:
                continue
        except OSError:
            continue
        yield path


# ============================================================================
# Check 1: compromised PyPI packages
# ============================================================================

def check_compromised_pypi(root: Path) -> list[Finding]:
    findings: list[Finding] = []
    for req in walk_requirements(root):
        try:
            content = req.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for lineno, line in enumerate(content.splitlines(), 1):
            stripped = line.strip()
            # Skip blank lines, comments, and pip directives (-r, -c, --index-url …)
            if not stripped or stripped.startswith("#") or stripped.startswith("-"):
                continue
            m = _REQ_NAME_RE.match(line)
            if not m:
                continue
            raw_name = m.group(1)
            canonical = canonicalize_name(raw_name)
            if canonical not in COMPROMISED_PYPI_CANONICAL:
                continue
            findings.append(Finding(
                section="pypi", type="compromised_dependency",
                level="ALERT",
                message=(
                    f"compromised package '{raw_name}' (canonical "
                    f"'{canonical}') in {req}:{lineno}"
                ),
                details={"package": raw_name, "canonical": canonical,
                         "file": str(req), "line": lineno,
                         "raw": stripped},
            ))
    return findings


# ============================================================================
# Check 2: compromised npm packages
# ============================================================================

def check_compromised_npm(root: Path) -> list[Finding]:
    """Scan package.json and package-lock.json for known-compromised packages."""
    findings: list[Finding] = []
    for path in walk_package_jsons(root):
        try:
            data = json.loads(path.read_text(encoding="utf-8", errors="replace"))
        except (OSError, json.JSONDecodeError):
            continue

        # package.json: scan dependencies, devDependencies, peerDependencies
        if path.name == "package.json":
            for dep_section in ("dependencies", "devDependencies",
                                "peerDependencies", "optionalDependencies"):
                deps = data.get(dep_section, {})
                if not isinstance(deps, dict):
                    continue
                for pkg, ver_spec in deps.items():
                    canonical = canonicalize_npm_name(pkg)
                    if canonical in COMPROMISED_NPM_CANONICAL:
                        bad_versions = COMPROMISED_NPM_CANONICAL[canonical]
                        # If specific versions are tracked, check the spec
                        if bad_versions:
                            ver_str = str(ver_spec).strip().lstrip("^~>=<")
                            if ver_str in bad_versions:
                                _add_npm_finding(findings, pkg, ver_str, path,
                                                 dep_section, "exact_version_match")
                            else:
                                # Range may include a bad version
                                _add_npm_finding(findings, pkg, str(ver_spec), path,
                                                 dep_section, "range_includes_bad",
                                                 level="WARN")
                        else:
                            # All versions are bad
                            _add_npm_finding(findings, pkg, str(ver_spec), path,
                                             dep_section, "package_compromised")

        # package-lock.json: scan resolved package@version entries
        elif path.name == "package-lock.json":
            packages = data.get("packages", {})
            for key, pkg_info in packages.items():
                if not isinstance(pkg_info, dict):
                    continue
                name = pkg_info.get("name") or _name_from_lock_key(key)
                if not name:
                    continue
                canonical = canonicalize_npm_name(name)
                if canonical not in COMPROMISED_NPM_CANONICAL:
                    continue
                version = pkg_info.get("version", "")
                bad_versions = COMPROMISED_NPM_CANONICAL[canonical]
                if not bad_versions or version in bad_versions:
                    _add_npm_finding(findings, name, version, path,
                                     "package-lock", "locked_to_bad_version")
    return findings


def _name_from_lock_key(key: str) -> str:
    """Extract package name from a package-lock.json key like
    'node_modules/@cap-js/sqlite'."""
    if "node_modules/" not in key:
        return ""
    # Last node_modules/ segment carries the name
    tail = key.rsplit("node_modules/", 1)[-1]
    return tail


def _add_npm_finding(findings: list[Finding], name: str, version: str,
                     path: Path, section_name: str, why: str,
                     level: str = "ALERT") -> None:
    findings.append(Finding(
        section="npm", type="compromised_dependency", level=level,
        message=f"compromised npm package '{name}@{version}' in {path} ({section_name}, {why})",
        details={"package": name, "version": version, "file": str(path),
                 "section": section_name, "reason": why},
    ))


# ============================================================================
# Check 3: .pth files with executable code
# ============================================================================

def check_pth_files(root: Path) -> list[Finding]:
    findings: list[Finding] = []
    site_packages_dirs: set[Path] = set()
    for sp in root.rglob("site-packages"):
        if sp.is_dir():
            site_packages_dirs.add(sp)
    for spd in site_packages_dirs:
        for pth in spd.rglob("*.pth"):
            try:
                content = pth.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            stripped = content.strip()
            if stripped in PTH_ALLOWLIST_EXACT:
                continue
            # SHA256 + filename guardrail: legitimate shim with both a
            # known-good hash AND a recognized filename short-circuits.
            sha = hashlib.sha256(stripped.encode("utf-8")).hexdigest()
            if sha in PTH_ALLOWLIST_SHA256 and pth.name in PTH_ALLOWLIST_NAMES:
                continue
            # Content-pattern allowlist: durable fallback for shims whose
            # exact hash isn't tracked yet but whose structure matches a
            # canonical legit form (e.g., setuptools across versions).
            # Still requires the recognized filename to defend against
            # someone dropping a "looks-like-shim" file under an evil name.
            if pth.name in PTH_ALLOWLIST_NAMES and any(
                pat.search(stripped) for pat in PTH_ALLOWLIST_PATTERNS
            ):
                continue
            for pat in PTH_EXEC_PATTERNS:
                if pat.search(content):
                    findings.append(Finding(
                        section="pth_files", type="executable_pth",
                        level="ALERT",
                        message=f"executable code in .pth file: {pth}",
                        details={"file": str(pth), "pattern": pat.pattern,
                                 "preview": content[:200]},
                    ))
                    break
    return findings


# ============================================================================
# Check 4: GitHub workflow files
# ============================================================================

def check_workflows(root: Path) -> list[Finding]:
    findings: list[Finding] = []
    workflow_dir = root / ".github" / "workflows"
    if not workflow_dir.is_dir():
        return findings
    for wf in list(workflow_dir.glob("*.yml")) + list(workflow_dir.glob("*.yaml")):
        try:
            content = wf.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for pat in WORKFLOW_PATTERNS:
            m = pat.search(content)
            if m:
                line_no = content[:m.start()].count("\n") + 1
                findings.append(Finding(
                    section="workflows", type="suspicious_workflow",
                    level="ALERT",
                    message=f"suspicious pattern in {wf}:{line_no}",
                    details={"file": str(wf), "line": line_no,
                             "pattern": pat.pattern[:80]},
                ))
                break
    return findings


# ============================================================================
# Check 5: git remotes vs C2 domains
# ============================================================================

def check_git_remotes(root: Path) -> list[Finding]:
    findings: list[Finding] = []
    if not (root / ".git").is_dir():
        return findings
    try:
        result = subprocess.run(
            ["git", "-C", str(root), "remote", "-v"],
            capture_output=True, text=True, timeout=10, check=False,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return findings
    for raw_line in result.stdout.splitlines():
        safe_line = _redact_remote_line(raw_line)
        lower = raw_line.lower()
        for domain in C2_DOMAINS:
            if domain in lower:
                findings.append(Finding(
                    section="git_remotes", type="c2_remote",
                    level="ALERT",
                    message=f"git remote points at C2 domain: {safe_line}",
                    details={"domain": domain, "remote_line": safe_line},
                ))
                break
        for ip in C2_IPS:
            if ip in raw_line:
                findings.append(Finding(
                    section="git_remotes", type="c2_remote_ip",
                    level="ALERT",
                    message=f"git remote points at C2 IP: {safe_line}",
                    details={"ip": ip, "remote_line": safe_line},
                ))
                break
    return findings


# ============================================================================
# Check 6: Claude Code / VSCode persistence files (NEW in v1.1)
# ============================================================================

_MAX_HASH_FILE_BYTES = 5_000_000


def _hash_persistence_file(target: Path) -> Optional[Finding]:
    """If ``target`` exists, is under the size cap, and its SHA256 is in
    MALICIOUS_SHA256, return a finding. Otherwise None.

    Composed with the path-based check in ``check_persistence_paths``: the
    constant's docstring intentionally scopes hash matching to persistence
    paths (broader scanning belongs to ``shai-hulud-audit.{ps1,sh}``).
    """
    try:
        size = target.stat().st_size
    except OSError:
        return None
    if size > _MAX_HASH_FILE_BYTES:
        return None
    try:
        data = target.read_bytes()
    except OSError:
        return None
    digest = hashlib.sha256(data).hexdigest()
    if digest not in MALICIOUS_SHA256:
        return None
    return Finding(
        section="persistence", type="known_malicious_hash", level="ALERT",
        message=(
            f"file at {target} matches known TeamPCP payload hash "
            f"{digest[:12]}…"
        ),
        details={"file": str(target), "sha256": digest,
                 "note": "exact match against MALICIOUS_SHA256 IOC list"},
    )


def check_persistence_paths(root: Path) -> list[Finding]:
    """Detect TeamPCP persistence via Claude Code / VSCode config + payload files.

    Some of these paths legitimately exist in many projects (.claude/settings.json,
    .vscode/tasks.json), so we only ALERT on suspicious filenames. For legitimate
    config files, we WARN only if the file content matches an IoC keyword.

    Additionally, for every file that matches a persistence path entry, the
    file's SHA256 is checked against ``MALICIOUS_SHA256``. A hash match emits
    a separate ``known_malicious_hash`` ALERT in addition to the path-based
    finding so reviewers see both signals.
    """
    findings: list[Finding] = []
    for rel_path, level, description, content_keyword in PERSISTENCE_PATHS:
        target = root / rel_path
        if not target.exists():
            continue
        # Hash-based check runs independently of the keyword filter so a
        # known-bad payload dropped at one of the keyword-guarded paths
        # (.claude/settings.json, .vscode/tasks.json) is still caught when
        # the file lacks the legacy trigger keyword.
        hash_finding = _hash_persistence_file(target)
        if hash_finding is not None:
            findings.append(hash_finding)
        if content_keyword is not None:
            try:
                content = target.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            if content_keyword not in content:
                continue
            findings.append(Finding(
                section="persistence", type="suspicious_config",
                level=level,
                message=f"{description} contains '{content_keyword}': {target}",
                details={"file": str(target), "keyword": content_keyword,
                         "description": description},
            ))
        else:
            findings.append(Finding(
                section="persistence", type="suspicious_file",
                level=level,
                message=f"{description}: {target}",
                details={"file": str(target), "description": description},
            ))
    return findings


# ============================================================================
# Check 7: Spoofed git commit authors (NEW in v1.1)
# ============================================================================

def check_spoofed_commits(root: Path) -> list[Finding]:
    """Detect commits authored by emails TeamPCP is known to spoof.

    Note: legitimate commits from Claude Code or Dependabot will also match.
    We surface as WARN, not ALERT — the user has to verify.
    """
    findings: list[Finding] = []
    if not (root / ".git").is_dir():
        return findings
    try:
        result = subprocess.run(
            ["git", "-C", str(root), "log",
             "--format=%H|%ae|%s", "--all", "-n", "200"],
            capture_output=True, text=True, timeout=15, check=False,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return findings
    for line in result.stdout.splitlines():
        parts = line.split("|", 2)
        if len(parts) < 3:
            continue
        sha, email, subject = parts
        if email.lower() in SPOOFED_AUTHORS:
            findings.append(Finding(
                section="git_commits", type="potentially_spoofed_author",
                level="WARN",
                message=f"commit by '{email}' at {sha[:8]}: {subject[:60]}",
                details={"sha": sha, "email": email,
                         "subject": subject,
                         "note": "TeamPCP is known to spoof this author email; "
                                 "verify against expected commit history"},
            ))
    return findings


# ============================================================================
# Check 8: Campaign string markers in any file (NEW in v1.1)
# ============================================================================

def check_campaign_markers(root: Path) -> list[Finding]:
    """Search text files for known TeamPCP / Shai-Hulud campaign strings."""
    # Files that legitimately contain IOC strings as part of their own
    # detection logic or documentation — skip to avoid scanner-scans-itself
    # false positives.
    SCANNER_FILENAMES = {
        # Scanner code + tests
        "detect_compromise.py", "test_detect_compromise.py",
        "hulud_kit_scan.py",
        "shai-hulud-audit.ps1", "shai-hulud-audit.sh",
        "shai-hulud-audit.bat",
        # Kit docs that document IOC strings by name
        "THREAT_MODEL.md", "HARDENING.md", "IOC_CHECKLIST.md",
        "SINGLE_DEV_CHECKLIST.md", "INTEGRATION.md",
        "SUPPLY_CHAIN_THREAT_MODEL.md", "IOC_DETECTION_CHECKLIST.md",
    }
    findings: list[Finding] = []
    for path in walk_text_files(root):
        if path.name in SCANNER_FILENAMES:
            continue
        try:
            content = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for marker, description in CAMPAIGN_MARKERS:
            if marker in content:
                # Find the line number
                line_no = content.find(marker)
                line_no = content[:line_no].count("\n") + 1
                findings.append(Finding(
                    section="campaign_marker", type="ioc_string",
                    level="ALERT",
                    message=f"campaign marker '{marker}' in {path}:{line_no}",
                    details={"file": str(path), "line": line_no,
                             "marker": marker, "description": description},
                ))
                break  # one finding per file is enough
    return findings


# ============================================================================
# Check 9: marker self-check
# ============================================================================

def check_marker_self() -> list[Finding]:
    expected = "niagA oG eW ereH :duluH-iahS"
    if MARKER_REVERSED != expected:
        return [Finding(
            section="self", type="marker_tampered", level="WARN",
            message="reversed marker in detect_compromise.py was modified",
            details={"expected": expected, "actual": MARKER_REVERSED},
        )]
    return []


# ============================================================================
# Orchestration
# ============================================================================

def run_all_checks(root: Path) -> list[Finding]:
    findings: list[Finding] = []
    findings.extend(check_compromised_pypi(root))
    findings.extend(check_compromised_npm(root))
    findings.extend(check_pth_files(root))
    findings.extend(check_workflows(root))
    findings.extend(check_git_remotes(root))
    findings.extend(check_persistence_paths(root))
    findings.extend(check_spoofed_commits(root))
    findings.extend(check_campaign_markers(root))
    findings.extend(check_marker_self())
    return findings


def summarize(findings: list[Finding]) -> tuple[int, int, int]:
    alerts = sum(1 for f in findings if f.level == "ALERT")
    warnings = sum(1 for f in findings if f.level == "WARN")
    if alerts > 0:
        exit_code = 2
    elif warnings > 0:
        exit_code = 1
    else:
        exit_code = 0
    return alerts, warnings, exit_code


# ============================================================================
# SARIF output (NEW in v1.1)
# ============================================================================

def to_sarif(findings: list[Finding], root: Path) -> dict:
    """Convert findings to SARIF 2.1.0 format for GitHub Security tab."""
    rules: dict[str, dict] = {}
    results: list[dict] = []

    for f in findings:
        rule_id = f"{f.section}/{f.type}"
        if rule_id not in rules:
            rules[rule_id] = {
                "id": rule_id,
                "name": f.type,
                "shortDescription": {"text": f.type.replace("_", " ")},
                "fullDescription": {
                    "text": f"Supply chain audit finding: {rule_id}. "
                            f"Detected by detect_compromise.py."
                },
                "defaultConfiguration": {
                    "level": _sarif_level(f.level)
                },
                "helpUri": "https://github.com/copyleftdev/mini-shai-hulud-dragnet",
            }

        # Build location. Make path relative to root for GitHub UI linking.
        file_path = f.details.get("file", str(root))
        try:
            rel_path = str(Path(file_path).resolve().relative_to(root.resolve()))
        except (ValueError, OSError):
            rel_path = file_path

        location = {
            "physicalLocation": {
                "artifactLocation": {"uri": rel_path}
            }
        }
        if "line" in f.details:
            location["physicalLocation"]["region"] = {
                "startLine": int(f.details["line"])
            }

        results.append({
            "ruleId": rule_id,
            "level": _sarif_level(f.level),
            "message": {"text": f.message},
            "locations": [location],
        })

    return {
        "version": "2.1.0",
        "$schema": "https://docs.oasis-open.org/sarif/sarif/v2.1.0/cos02/schemas/sarif-schema-2.1.0.json",
        "runs": [{
            "tool": {
                "driver": {
                    "name": "detect_compromise",
                    "version": "1.1",
                    "informationUri": "https://github.com/copyleftdev/mini-shai-hulud-dragnet",
                    "rules": list(rules.values()),
                }
            },
            "results": results,
        }]
    }


def _sarif_level(level: str) -> str:
    return {"ALERT": "error", "WARN": "warning", "INFO": "note"}.get(level, "none")


# ============================================================================
# CLI
# ============================================================================

def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Project supply-chain audit.")
    parser.add_argument("--root", type=Path, default=Path.cwd(),
                        help="Repo root to scan (default: cwd)")
    parser.add_argument("--json", action="store_true",
                        help="Emit JSON to stdout instead of human output")
    parser.add_argument("--sarif", type=Path, default=None,
                        help="Write SARIF 2.1.0 output to this path")
    parser.add_argument("--fail-on-level", choices=["1", "2"], default="2",
                        help="Exit non-zero on warning (1) or alert (2). Default 2.")
    args = parser.parse_args(argv)

    root = args.root.resolve()
    if not root.is_dir():
        print(f"Root not a directory: {root}", file=sys.stderr)
        return 3

    findings = run_all_checks(root)
    alerts, warnings, exit_code = summarize(findings)

    if int(args.fail_on_level) == 2 and exit_code == 1:
        exit_code = 0

    result = {
        "tool": "detect_compromise",
        "version": "1.1",
        "root": str(root),
        "summary": {
            "alerts": alerts,
            "warnings": warnings,
            "exit_code": exit_code,
        },
        "findings": [f.as_dict() for f in findings],
    }

    if args.sarif:
        sarif = to_sarif(findings, root)
        try:
            args.sarif.parent.mkdir(parents=True, exist_ok=True)
            args.sarif.write_text(json.dumps(sarif, indent=2), encoding="utf-8")
            print(f"SARIF written to {args.sarif}", file=sys.stderr)
        except OSError as e:
            print(f"Failed to write SARIF: {e}", file=sys.stderr)
            return 3

    if args.json:
        print(json.dumps(result, indent=2))
    else:
        print(f"Scanned: {root}")
        if not findings:
            print("OK No findings.")
        else:
            for f in findings:
                marker = {"ALERT": "X", "WARN": "!", "INFO": "."}.get(f.level, "?")
                print(f"  [{marker}] {f.section}/{f.type}: {f.message}")
        print(f"Summary: {alerts} alerts, {warnings} warnings, exit {exit_code}")

    return exit_code


if __name__ == "__main__":
    sys.exit(main())
