# Supply Chain Threat Model

> **Last updated:** 2026-05-21
> **Status:** Active — Mini Shai-Hulud (TeamPCP) campaign still expanding to PyPI; toolkit was open-sourced via BreachForums in early May 2026, so copycat waves are expected.

## Purpose

This document is the project's mental model for **which links in our dependency
supply chain can be poisoned, how an attacker would deliver the payload to
this Python codebase, and what we lose if we get hit**.

The goal is **prevention**, not response — once a malicious package executes
in your venv with your credentials in the environment, it's already over.
The defenses below are layered to fail closed before that point.

## 1. Current threat landscape (relevant to us)

### 1.1 Mini Shai-Hulud (TeamPCP)

- **Active since:** April 2026, escalating through May 2026.
- **Latest waves:**
  - 2026-05-11: TanStack, Mistral AI, Guardrails AI, UiPath, OpenSearch — 84
    artifacts across 42 npm packages compromised in a single 22-minute burst
    via OIDC token hijack of TanStack's release pipeline.
  - 2026-05-19: AntV `@antv/*` family, echarts-for-react, timeago.js,
    size-sensor — 300+ malicious versions across 323 npm packages in a
    22-minute automated burst, via compromise of the `atool` maintainer
    account.
- **Cross-ecosystem footprint:** Worm is **wormable, autonomous, and
  cross-ecosystem** (npm, PyPI, Composer). Microsoft's `durabletask` Python
  SDK was confirmed compromised; LiteLLM PyPI saw tampered versions installing
  multi-stage credential stealers via the `.pth` file technique.
- **Toolkit:** Open-sourced via BreachForums as a "supply chain attack
  contest." Copycat waves are the default forecast.

### 1.2 Attack chain

1. **Initial access** — attacker compromises a maintainer account (phishing,
   PAT theft, OIDC token hijack via poisoned GitHub Actions cache).
2. **Malicious publish** — attacker pushes a tampered version of a real
   package, often with valid SLSA Build Level 3 provenance attestations
   (TanStack wave demonstrated this — provenance is not a defense).
3. **Install-time hook** — for npm, `preinstall`/`postinstall` (often
   Bun-based). For Python, the equivalents are:
   - `setup.py` execution on `sdist` install
   - `.pth` files in site-packages auto-executed on every `import site`
   - `pyproject.toml` build hooks (PEP 517 backends)
   - Top-level `__init__.py` code that runs on first import
   - `entry_points` console scripts executed by the user
4. **Payload** — harvests AWS/GCP creds, Kubernetes tokens, Vault secrets,
   GitHub PATs, npm/PyPI tokens, SSH keys, password manager vaults
   (1Password, Bitwarden, `pass`).
5. **Exfiltration** — public GitHub repos created under the victim's account
   (description reversed: `niagA oG eW ereH :duluH-iahS` → "Shai-Hulud: Here
   We Go Again"), Dune-themed repo names like `sayyadina-stillsuit-852`,
   plus C2 over HTTPS to attacker-controlled domains
   (`t.m-kosche[.]com` and rotating others).
6. **Self-propagation** — stolen npm/PyPI tokens used to publish malicious
   versions of OTHER packages the compromised account can touch — the worm
   spreads across the dependency graph.

### 1.3 What we lose if it lands here

- `fal.ai` API key (lifetime spend on attacker's video generations)
- `BFL` API key (same)
- `OpenRouter` API key (vision-LLM bills)
- `Freeimage.host` API key (uploads, minor)
- Any GitHub PAT in the dev environment → branch poisoning → recursive supply
  chain attack on downstream users of THIS repo
- SSH keys for any remote the dev pushes to
- Any browser-saved cloud creds the dev's user has access to

The dev machine running this app is a credential-rich target. Treat every
`pip install` and every CI run with the seriousness that implies.

## 2. Our Python-specific attack surface

### 2.1 Direct dependencies (`requirements.txt`)

The main `requirements.txt` declares ~12 top-level deps. The transitive graph
is much larger. Highest-risk packages by surface area:

| Package | Why high-risk | Mitigation in place |
|---|---|---|
| `deepface==0.0.92` | Lazy-imports `tf-keras`, `torch`, `retina-face` at runtime; pulls in TensorFlow's full graph. Any of those can ship a `.pth` payload that runs on `import site`. | Pinned exact version. Audit on update. |
| `tf-keras==2.16.0` | Pulled by DeepFace. Large attack surface (TF ecosystem has had compromises before). | Pinned exact version. |
| `retina-face==0.0.17` | Small package, single maintainer. Attractive target. | Pinned exact version. |
| `torch>=2.2,<3` | PyTorch — large team, but the install runs platform-specific binary wheel downloads. | Range pin (CPU-only). Sandbox install in venv. |
| `mediapipe==0.10.35` | Google maintainer, but exact pin still important. | `--no-deps` install (sibling deps blocked) — see `launchers/windows/run_gui.bat`. |
| `fal-client>=0.7,<1` | Direct API surface; bug there = data exfil to attacker-controlled fal endpoints. | Range pin. Monitor. |
| `selenium>=4.15,<5` + `webdriver-manager>=4.0,<5` | Downloads + executes a Chromium driver binary on first use. | Optional dep; off by default. |

### 2.2 Setuptools / build hooks

- **No `setup.py` at the repo root** — main project installs as a script,
  not a package. ✅
- **`similarity/pyproject.toml`** uses `setuptools` build-backend. A
  poisoned setuptools COULD execute code during `pip install -e similarity`,
  which CI does. Mitigated by pinning setuptools and preferring wheels.
- **`oldcam-v*/requirements.txt`** — each oldcam version has its own
  requirements file installed by launchers on first run. Surface is mediapipe
  + opencv + numpy. Same dep set as main; same mitigations.

### 2.3 Auto-executed code paths

- **`__init__.py` files** — `kling_gui/__init__.py` and submodules. A poisoned
  dependency could plant a malicious top-level package OR shadow our own
  module names if installed with `pip install` (sys.path order). Our launchers
  use the local repo path FIRST so this is mitigated, but a `pip install`
  done by a dev directly would not have that protection.
- **`.pth` files in site-packages** — auto-executed by Python's `site.py`
  on every interpreter start. LiteLLM's PyPI compromise used this exact
  technique. Detection: any `.pth` file in venv `Lib/site-packages/` that
  contains executable code (not just a path string) is suspicious.

### 2.4 GitHub Actions workflows

- **`.github/workflows/`** — currently runs on every PR. CodeRabbit's review
  cited that GitHub Actions billing is currently locked on this account
  (separate issue) so the workflows aren't running. **When they're
  re-enabled, this is a high-value attack surface** — any malicious workflow
  edit could exfiltrate the repo's GITHUB_TOKEN to an attacker C2.

## 3. Defensive posture (this codebase)

### 3.1 What we have

- Direct deps version-pinned (exact or narrow range)
- `mediapipe` installed with `--no-deps` so sibling deps don't drift
- No `setup.py` at root (no sdist build hook for the main project)
- Sandbox: every launcher creates a project-local venv (not the user's global)
- API keys live in `kling_config.json`, NOT in environment variables, so a
  payload reading `os.environ` doesn't capture them. (They CAN still be read
  if the payload reads the config file directly, but the bar is higher.)

### 3.2 What we're adding in this PR

| Defense | File | Why |
|---|---|---|
| Hash-pinned `requirements-hashed.txt` | new | Generated via `pip-tools compile --generate-hashes`. Detects post-publish tampering: a malicious republish under the same version fails the SHA-256 verify on install. `sandbox_install.{sh,bat}` use `--require-hashes` when this file is present. |
| `pip-audit` in CI + `scripts/audit_deps.{sh,bat}` | new | Cross-checks every dep against PyPA + OSV advisory DBs on every PR + on-demand for devs. |
| Installed-venv audit job | CI | Belt-and-suspenders for the requirements-file audits — catches transitive CVEs via an actual installed tree, in case the pip-audit resolver behaves differently on range-pinned input than expected. Each manifest (main + similarity + resemble-score + oldcam-v*) gets its OWN ephemeral venv so a later install's pin can't overwrite an earlier vulnerable one (Codex P1, 0e16c8d). |
| OSV-Scanner in CI | new | Second-source vulnerability DB (Google) — covers gaps in PyPA. |
| `detect_compromise.py` IoC self-check | new | One-command scan: known-compromised PyPI packages, `.pth` files with executable code (`re.MULTILINE` + whole-content allowlist matching to prevent prefix-only bypass), git remotes against C2 domains, workflow files for `curl \| bash` patterns, optional GitHub account scan for exfil repos. |
| `.github/dependabot.yml` | new | Automated security updates with weekly cadence + grouping. |
| `SECURITY.md` | new | Disclosure policy + threat model link. |
| `scripts/sandbox_install.{sh,bat}` | new | One-command isolated install (separate venv, `--only-binary :all:`, pip-audit on the result). |
| `--only-binary :all:` documented in install workflow | docs update | Avoid sdist execution path for top-level deps. |
| `docs/security/SINGLE_DEV_CHECKLIST.md` | new | Pragmatic single-developer hardening that doesn't require full VM isolation: 1Password CLI lockdown, npm `ignore-scripts`, backups, FDE, egress monitoring, scoped tokens, install discipline. |

### 3.3 Deferred to a follow-up PR (NOT in this commit)

| Item | Why deferred | Tracked in |
|---|---|---|
| CVE remediation of the 20 known advisories pip-audit surfaces (Pillow 11.3 → 12.2, torch 2.12 PYSEC-2025 chain, keras, markdown, joblib) | Mixing remediation with hardening would make the PR hard to review. Hardening infrastructure must land first; CVE bumps tested individually. | `docs/security/HARDENING.md` §8 |

### 3.4 What we're NOT doing (out of scope or accepting tradeoff)

- **Vendoring all deps into the repo** — would prevent supply chain attacks
  entirely but cost more than the protection's worth (Tensorflow alone is
  500MB+, freezes us at one version forever).
- **Air-gapped CI** — runs every install in a fresh Linux VM with no creds —
  would require GitHub Actions account unlock + significant infra. Tracked
  for v3.0.
- **Reproducible builds** — proves nothing was changed during build, but
  Python wheel reproducibility is unsolved at the ecosystem level.

## 4. Cross-reference

- `docs/security/HARDENING.md` — operational guide (commands, CI setup)
- `docs/security/IOC_DETECTION_CHECKLIST.md` — what to look for if you suspect
  compromise
- `scripts/detect_compromise.py` — automated IoC scanner
- `SECURITY.md` — top-level policy + disclosure

## 5. Sources

- [Snyk: TanStack npm Packages Hit by Mini Shai-Hulud](https://snyk.io/blog/tanstack-npm-packages-compromised/)
- [Snyk: Mini Shai-Hulud Hits AntV — 300+ Malicious npm Packages](https://snyk.io/blog/mini-shai-hulud-antv-npm-supply-chain-attack/)
- [Microsoft Security Blog: Shai-Hulud 2.0 Guidance](https://www.microsoft.com/en-us/security/blog/2025/12/09/shai-hulud-2-0-guidance-for-detecting-investigating-and-defending-against-the-supply-chain-attack/)
- [Hackread: TeamPCP Mini Shai-Hulud npm + PyPI](https://hackread.com/teampcp-mini-shai-hulud-worm-npm-pypi-packages/)
- [JFrog Research: Shai-Hulud Here We Go Again](https://research.jfrog.com/post/shai-hulud-here-we-go-again/)
- [SafeDep: 317 npm Packages Compromised](https://safedep.io/mini-shai-hulud-strikes-again-314-npm-packages-compromised/)
- [Cybernews: 600+ npm Packages Compromised](https://cybersecuritynews.com/600-npm-packages-compromised/)
- [Wiz: TanStack + more Compromised](https://www.wiz.io/blog/mini-shai-hulud-strikes-again-tanstack-more-npm-packages-compromised)
- [Phoenix Security: TeamPCP Wave Four — durabletask PyPI Worm](https://phoenix.security/teampcp-github-breach-durabletask-pypi-supply-chain-wave-four-2026/)
- [Bernat Tech: Defense in Depth — Python Supply Chain](https://bernat.tech/posts/securing-python-supply-chain/)
- [pypa/pip-audit GitHub](https://github.com/pypa/pip-audit)
- [PyPI Security Best Practices (lirantal)](https://github.com/lirantal/pypi-security-best-practices)
