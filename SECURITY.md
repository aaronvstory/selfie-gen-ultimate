# Security Policy

## Reporting a vulnerability

Please report security issues privately via **GitHub Security Advisories**:

  https://github.com/aaronvstory/selfie-gen-ultimate/security/advisories/new

Or email the maintainer (see commit history for the active address).

**Do not file public GitHub issues for security reports.** We'll respond
within 7 days; coordinated disclosure preferred.

## Threat model & hardening

This project is preventively hardened against the supply chain attack class
exemplified by the **Mini Shai-Hulud (TeamPCP)** campaign active through
2026. For background, defenses in place, and operational guidance:

- `docs/security/SUPPLY_CHAIN_THREAT_MODEL.md` — what we're defending against
  and why
- `docs/security/HARDENING.md` — operational guide (tools, commands, CI)
- `docs/security/IOC_DETECTION_CHECKLIST.md` — incident response runbook
- `scripts/detect_compromise.py` — automated IoC self-check (run weekly)
- `scripts/audit_deps.sh` / `scripts/audit_deps.bat` — local CVE audit
- `.github/workflows/supply-chain-audit.yml` — CI runs pip-audit +
  OSV-Scanner + IoC check on every PR + nightly
- `.github/dependabot.yml` — automated security update PRs

## Quick start (for contributors)

Before submitting a PR that changes dependencies:

```bash
# Linux/macOS
bash scripts/audit_deps.sh

# Windows
scripts\audit_deps.bat
```

Once a week (or anytime you suspect something is off):

```bash
python scripts/detect_compromise.py --all
```

## Supported versions

Active support is on `main`. Security fixes will be backported to the
latest distributable tag (v2.x) when the fix is non-breaking. Older
distributable tags receive no fixes — upgrade.
