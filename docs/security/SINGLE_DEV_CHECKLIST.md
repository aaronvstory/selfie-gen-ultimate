# Single-Dev Security Checklist

> **Audience:** Solo developer on a single machine. No corporate IT, no dedicated security team, no easy access to spin up isolated VMs/containers for every install.
>
> **Premise:** Full isolation (Docker dev containers, VMs, separate macOS/Linux user accounts) is the structural fix to "malicious npm/PyPI install reads your credentials." It's also a major workflow disruption. This checklist is the **non-isolation pragmatic stack** — things that genuinely reduce risk while keeping the single-dev workflow intact.
>
> See `SUPPLY_CHAIN_THREAT_MODEL.md` for the WHY and `HARDENING.md` for the operational HOW. This file is the **personal-hygiene layer** that sits on top of both.

## Threat reframe

The threat is NOT "1Password gets hacked." It's **"your dev machine runs a malicious install, then the malware reads everything it can reach."**

**Defense = reduce what a compromised install can reach.** Without going full-VM, you do this through:

1. Making fresh installs less common + more deliberate
2. Removing always-on credential sessions
3. Hash-pinning so a tampered version of an existing dep fails install instead of running its payload
4. Catching CVEs in CI before they merge
5. Detecting compromise fast if it does land

## Tier 1 — Do today (~30 min)

### ☐ 1.1  Lock down 1Password CLI

- **If you don't actively use `op` CLI**: uninstall it. The desktop app alone is a much smaller attack surface than the CLI which stores session tokens accessible to any process running as your user.
- **If you do use it**:
  - Run `op signout` whenever you finish a session. The default 30-day session token is way too long.
  - **Never** store the master password or service-account tokens in `.env`, shell scripts, or env vars.
  - Clear shell history of past signins: `history -c && history -w` (bash) or edit `~/.zsh_history` / `~/.bash_history` directly.
  - Enable biometric unlock so the master password isn't typed often (key-logger resistance).

### ☐ 1.2  Audit your GitHub account for active compromise

- Visit `https://github.com/<your-username>?tab=repositories`. Look for ANY public repo you didn't create, especially with:
  - Description containing `niagA oG eW ereH :duluH-iahS` (reversed "Shai-Hulud: Here We Go Again")
  - Dune-themed names: `sayyadina-*`, `atreides-*`, `bene-gesserit-*`, `melange-*`, `fremen-*`, `harkonnen-*`, `kwisatz-*`, `ornithopter-*`, `stillsuit-*`
- Check `.github/workflows/` in YOUR repos for files you didn't add or modifications you didn't make.
- Review tokens at `https://github.com/settings/tokens` — delete any you don't recognize OR haven't used recently.
- Review OAuth app permissions at `https://github.com/settings/applications` — revoke any you don't use.

### ☐ 1.3  Disable npm install scripts globally (if you touch npm at all)

Even if you only run `npm install` once a year in someone else's project:

```bash
npm config set ignore-scripts true
```

This single line blocks the install-time payload execution vector — the `preinstall`/`postinstall` hooks that Mini Shai-Hulud uses. When you need scripts (esbuild, sharp, etc. building native deps), re-enable per-install after vetting:

```bash
npm install --foreground-scripts <package>
```

Tradeoff: ~5% of packages won't install correctly without scripts. You'll find out at first use and can vet-then-allow.

### ☐ 1.4  Audit this project's deps for known CVEs

From the repo root:

```bash
# Linux/macOS
bash scripts/audit_deps.sh

# Windows
scripts\audit_deps.bat
```

If anything FAILs, see `HARDENING.md` §8 for remediation.

### ☐ 1.5  Run the IoC self-check

```bash
python scripts/detect_compromise.py --all
```

Note: `--all` requires the `gh` CLI to scan your GitHub account for exfil repos. If you don't have `gh`, omit `--all` and check GitHub manually (see §1.2).

## Tier 2 — Do this week (~2 hours)

### ☐ 2.1  Hash-pin Python deps

A version pin alone doesn't catch tampering — a compromised package republished under the same version still installs. SHA-256 hash verification catches this at install time.

Run from the repo root:

```bash
python -m pip install --upgrade pip pip-tools
python -m piptools compile --generate-hashes \
  --output-file=requirements-hashed.txt requirements.txt

# Now use the hashed file for installs:
python -m pip install --require-hashes -r requirements-hashed.txt
```

The `requirements-hashed.txt` is committed to this repo (see `requirements-hashed.txt` at repo root). Regenerate it whenever you bump a dep. **Never hand-edit it.**

### ☐ 2.2  Backups: 3-2-1 rule with at least one immutable copy

Once disk-wipers are in supply-chain payloads (durabletask PyPI compromise had one), backups become non-negotiable. Aim for:

- **3 copies** of important data
- **2 different media** (local SSD + cloud, or local SSD + external HDD)
- **1 offsite** AND **at least one of the three IMMUTABLE** — meaning the wiper running as you cannot delete or overwrite it

Pick your stack:

| Solution | Immutability | Cost | Notes |
|---|---|---|---|
| Backblaze Personal | ✅ (versioning + 30-day retention; pay for longer) | $9/mo | Best single-dev option |
| Arq + cloud target (S3/B2 with Object Lock) | ✅ (object lock) | ~$5/mo + storage | More setup but bulletproof |
| `restic` to S3 with Object Lock | ✅ (object lock) | ~$5/mo + storage | CLI; for the comfortable |
| Time Machine to external drive | ⚠️ (only if you DISCONNECT the drive after every backup) | $0 | Free, but only immutable if you unplug |
| iCloud / Google Drive sync | ❌ NOT immutable — wiper deletes synced files | Free tier | Sync ≠ backup |

**Test restore at least once.** Untested backups have a way of not working when you need them.

### ☐ 2.3  Verify full-disk encryption is on

- **macOS**: System Settings → Privacy & Security → FileVault → ON
- **Windows**: Settings → Privacy & Security → Device encryption (BitLocker) → ON
- **Linux**: `lsblk -f` should show your home partition as `crypto_LUKS`

Doesn't prevent wipe, but prevents data extraction from a stolen disk.

### ☐ 2.4  Set up dependency monitoring

You already have these in PR #44 (auto-enabled when this branch merges):

- ✅ GitHub Dependabot alerts (`.github/dependabot.yml`)
- ✅ pip-audit + OSV-Scanner in CI (`.github/workflows/supply-chain-audit.yml`)
- ✅ Nightly run at 07:00 UTC so post-PR advisories light up the next morning

**Free additions worth considering:**
- **Socket.dev** free tier — 6.7-minute median detection time on Mini Shai-Hulud waves. Best-in-class.
- **Snyk** free tier — broad CVE coverage, decent CLI.

## Tier 3 — Ongoing behavior changes

### ☐ 3.1  Pause auto-updates

- Don't run `npm update`, `pip install -U <pkg>`, or accept Dependabot PRs without reviewing the lockfile diff.
- Wait 24-48h after a release before pulling unless it fixes a CVE you're affected by. Most supply-chain attacks are detected within the first 24h.

### ☐ 3.2  Don't install fresh packages with active cloud sessions

Before `pip install something-new`:

```bash
# Lock 1Password
op signout

# Log out of AWS CLI
aws sso logout

# Close browser tabs with sensitive sessions
# (Optional — high friction, lower priority than the above two)
```

Reduces blast radius if the new package is malicious. The credential the payload sees is the empty set, not your full keychain.

### ☐ 3.3  Use scoped, short-lived tokens

- **GitHub PATs**: fine-grained, scoped to specific repos, with expiration dates. Avoid classic PATs with `repo` scope.
- **AWS**: SSO with short sessions instead of long-lived access keys (`aws configure sso`).
- **npm**: if you publish, use **granular access tokens** scoped to specific packages, NOT classic tokens.
- **fal.ai / BFL / OpenRouter**: rotate quarterly even if no incident. Set a calendar reminder.

### ☐ 3.4  Egress monitoring (catch exfil before it completes)

The disk-wiper variant gives you maybe seconds. Most other variants (credential stealer + slow exfil) give you minutes. A network egress monitor can flag unusual outbound connections from `python` or `node` processes:

- **macOS**: [Little Snitch](https://www.obdev.at/products/littlesnitch/) ($45 one-time) — best UX
- **Linux**: [OpenSnitch](https://github.com/evilsocket/opensnitch) (free, open source)
- **Windows**: [SimpleWall](https://www.henrypp.org/product/simplewall) (free) or [GlassWire](https://www.glasswire.com/) (paid)

Configure it to **alert on outbound connections from `python`, `node`, `bun` to non-allowlisted destinations**. The first time it complains about a connection to `t.m-kosche.com` from your `pip install`, you'll know.

### ☐ 3.5  Pull/install discipline

When pulling THIS project on a new machine OR a fresh shell:

1. `git fetch --all && git log --oneline -10` — read the commit log; anything you don't recognize?
2. `git diff main..HEAD` — read the diff on a branch before checking it out
3. `cat requirements.txt requirements-hashed.txt` — compare the dep lists; anything new?
4. ONLY THEN: `pip install --require-hashes -r requirements-hashed.txt`
5. Run `python scripts/detect_compromise.py` once after install completes.

## What this checklist does NOT cover

- **Full VM isolation** — covered in `HARDENING.md` §5 (sandboxed installs are halfway there)
- **GitHub Actions OIDC + workflow hardening** — covered in `HARDENING.md` §4
- **PyPI publishing protection** — only matters if you publish packages; out of scope for this app
- **Mobile device security** — your phone isn't running `pip install`

## What's the minimum if I can only do 5 things?

If you're picking just five from this list, do these:

1. ☐ 1.3 (npm `ignore-scripts true`)
2. ☐ 2.1 (hash-pin Python deps with `pip-tools`)
3. ☐ 2.2 (3-2-1 backup with one immutable copy)
4. ☐ 3.2 (lock 1Password + AWS before fresh installs)
5. ☐ 3.4 (egress monitor flagging python/node outbound)

That stack defends against >90% of the realistic threat without VM-level isolation.
