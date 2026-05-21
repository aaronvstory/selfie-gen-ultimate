# IoC Detection Checklist — Mini Shai-Hulud / TeamPCP

> If you suspect your dev machine, GitHub account, or CI infrastructure has
> been touched by Mini Shai-Hulud (TeamPCP) or a copycat: work through this
> checklist top to bottom. Do NOT skip ahead. Each step builds context for
> the next.

## 0. Triage — am I actually compromised?

**Yes-or-no signals** (any ONE of these → assume compromise + go to §1):

- [ ] A new public GitHub repo appeared under your account with a description
      containing `niagA oG eW ereH :duluH-iahS` (reversed
      "Shai-Hulud: Here We Go Again") or any Dune-themed name like
      `sayyadina-stillsuit-852` / `atreides-ornithopter-112` /
      `bene-gesserit-vault-N` that YOU did not create.
- [ ] A package YOU maintain on npm/PyPI has a new version YOU did not
      publish (check the publish history).
- [ ] Unexpected outbound HTTPS traffic from your dev machine or CI runners
      to: `t.m-kosche[.]com`, `*.duluh-iahs[.]xyz`, `team-pcp[.]com`,
      or any newly-registered domain not on your usual whitelist.
- [ ] New `.github/workflows/*.yml` files in repos YOU maintain that YOU
      did not commit, OR an existing workflow file modified to inject
      `curl ... | bash` / `wget ... -O- | sh` / `pip install <foo>` from
      an unfamiliar source.
- [ ] Suspicious `.pth` files in your venv `site-packages/`:
      ```bash
      grep -RIl "^import\|^exec\|^os\." */Lib/site-packages/*.pth */lib/python*/site-packages/*.pth 2>/dev/null
      ```
      (Path-only `.pth` files are normal. Files containing executable
      Python are the attack vector LiteLLM was hit with.)
- [ ] Recent cloud-credential access from an IP you don't recognize:
      AWS CloudTrail / GCP Audit Logs / Azure Sign-in Logs.

If NONE of the above match → run `scripts/detect_compromise.py` weekly as
a hygiene check. If ANY match → go to §1 immediately.

## 1. First 5 minutes — contain

1. **Pull the network cable / disconnect Wi-Fi.** Don't shut down — preserve
   memory state for forensics.
2. **Do NOT log into anything new** from the suspect machine. The payload
   may have a keylogger.
3. **Find a CLEAN machine.** Phone is fine for the next steps.

## 2. Rotate credentials (in this order)

From the clean machine:

1. **GitHub** — https://github.com/settings/security
   - Sign out everywhere
   - Reset password
   - Revoke ALL Personal Access Tokens (PATs):
     https://github.com/settings/tokens
   - Revoke ALL OAuth tokens:
     https://github.com/settings/applications
   - Re-enable 2FA (regenerate codes — the attacker may have copied them)
2. **npm / PyPI publish tokens**
   - npm: `npm token revoke <token-id>` for every token under your account
   - PyPI: https://pypi.org/manage/account/token/ — delete all + recreate
3. **Cloud creds**
   - AWS: IAM → rotate access keys, revoke active sessions, check
     CloudTrail for unauthorized actions
   - GCP: IAM → rotate service account keys, check Cloud Audit Logs
   - Azure: same pattern
4. **fal.ai / BFL / OpenRouter** — regenerate keys from each provider's
   dashboard. Update `kling_config.json` on a CLEAN machine before next
   use.
5. **Password manager master password**
   - 1Password, Bitwarden, `pass`, Keychain, etc.
6. **SSH keys**
   - Generate fresh keys on the clean machine
   - Update GitHub/GitLab/Bitbucket with the new public key
   - Delete old keys from the providers
7. **Browser-stored creds** — the payload reads these. Assume every saved
   password is compromised. Mass-reset.

## 3. Audit your GitHub presence

From the clean machine, signed into the rotated account:

1. **List your public repos** — look for ANY repo you don't recognize.
   Pay special attention to:
   - Repos with the reversed "Shai-Hulud" string in the description
   - Dune-themed names (sayyadina, atreides, ornithopter, stillsuit,
     melange, bene-gesserit, fremen, harkonnen, kwisatz)
   - Repos with a single dump file (often `.txt`, `.json`, or `.env`)
     containing what looks like your secrets
2. **Delete the attacker repos** — but FIRST screenshot them and save the
   names. You'll need this for the disclosure to the providers whose
   creds were leaked.
3. **Audit recent commits** to YOUR legitimate repos:
   ```bash
   git log --all --since="30 days ago" --pretty=format:"%H %an %ae %s" \
     | grep -v "<your-email>" | grep -v "noreply@github.com"
   ```
   Any commit from an unfamiliar author/email = unauthorized push.
4. **Check the Actions tab** on every repo for workflow runs you didn't
   trigger. Look at the workflow YAML history — was a file added or modified
   recently that you didn't write?
5. **Check Settings → Webhooks** on every repo for unfamiliar URLs.

## 4. Audit your published packages

1. **PyPI** — https://pypi.org/manage/projects/
   - For every project you maintain: open the "Releases" tab and check
     EVERY recent version. If a version exists that you don't remember
     publishing → immediate yank + advisory.
   - Yank: `python -m twine upload --skip-existing` is for adding; to yank
     a malicious version use the PyPI UI:
     https://pypi.org/manage/project/<name>/release/<version>/
   - File a security advisory:
     https://pypi.org/security/
2. **npm** — `npm whoami` → `npm owner ls <package>` for each
   - For every package, check the publish history:
     `npm view <package> time --json`
   - If a malicious version is present: `npm unpublish <pkg>@<version>`
     (within 72h) or contact security@npmjs.com.

## 5. Audit cloud usage

1. **AWS CloudTrail**: filter the last 7 days for `CreateAccessKey`,
   `PutUserPolicy`, `AssumeRole`, `StartInstances` (cryptominer attacker
   pattern). Anything you didn't initiate = breach.
2. **GCP Cloud Audit Logs**: same — filter for IAM changes, key creations,
   VM starts.
3. **Stripe / billing dashboards** — has anyone added a new payment method
   or made unusual charges?
4. **fal.ai dashboard** — recent generations + spend. The attacker WILL
   try to burn your credit on their own video generations.

## 6. Audit your dev machine

After credentials are rotated and the machine is air-gapped:

1. **Search for `.pth` files with executable code**:
   ```bash
   find / -name "*.pth" -exec grep -lE "^(import|exec|os\.|subprocess|eval)" {} \; 2>/dev/null
   ```
2. **Search for unexpected processes** with recent network activity:
   ```bash
   # macOS / Linux
   ps auxe | grep -iE "(python|node|bun|curl|wget|nc)"
   netstat -an | grep ESTABLISHED
   ```
3. **Check your shell history** for commands you don't recognize:
   ```bash
   cat ~/.bash_history ~/.zsh_history 2>/dev/null | tail -200
   ```
4. **Check launch agents** (macOS) / **scheduled tasks** (Windows) for
   persistence:
   - macOS: `ls ~/Library/LaunchAgents/ /Library/LaunchAgents/`
   - Windows: `schtasks /query /fo list /v`
5. **Re-image the machine** if you find anything. The cost of "thoroughly
   cleaning" a developer machine is always higher than the cost of
   re-imaging.

## 7. Disclose

1. **GitHub Security Advisory** for any repo you maintain that shipped
   malicious code via this vector.
2. **PyPI / npm Security** — see emails above.
3. **Affected users** — if you maintain a popular package and it shipped
   compromised, write a public post-mortem (helps the ecosystem catch
   the next wave faster).
4. **Microsoft Security Response Center** — they're cataloging Mini
   Shai-Hulud waves; reporting helps. https://msrc.microsoft.com/

## 8. Lessons-learned

Before you forget:

- [ ] Was a long-lived PAT involved? → Replace with OIDC where possible.
- [ ] Did the install bypass `--require-hashes`? → Update CI to enforce.
- [ ] Was the compromise detected by Dependabot/pip-audit before the
      payload ran? → If yes, those defenses worked. If no, what tool would
      have caught it?
- [ ] Update this checklist with anything you learned the hard way.

## 9. Known IoC reference

**Reversed campaign markers** (search GitHub for these):
- `niagA oG eW ereH :duluH-iahS` ("Shai-Hulud: Here We Go Again")
- Repo names: `sayyadina-stillsuit-*`, `atreides-ornithopter-*`,
  `bene-gesserit-vault-*`, `melange-*`, `fremen-cache-*`

**C2 domains** (block at network egress if you can):
- `t.m-kosche[.]com`
- `*.duluh-iahs[.]xyz`
- `team-pcp[.]com`
- (rotating — check Socket.dev / Snyk advisories for fresh lookups)

**Indicator files** in `site-packages/`:
- Any `.pth` containing `import`, `exec`, `os.system`, `subprocess`, or `eval`
- Files named `__pycache__/__init__.cpython-*.pyc` modified after the
  parent package's expected install time
- `.dist-info/RECORD` entries that don't match the on-disk file hashes

## 10. Sources

- [Microsoft Security Blog: Shai-Hulud 2.0 Detection Guidance](https://www.microsoft.com/en-us/security/blog/2025/12/09/shai-hulud-2-0-guidance-for-detecting-investigating-and-defending-against-the-supply-chain-attack/)
- [Snyk: Mini Shai-Hulud TanStack Compromise](https://snyk.io/blog/tanstack-npm-packages-compromised/)
- [SafeDep: 317 Packages Compromised](https://safedep.io/mini-shai-hulud-strikes-again-314-npm-packages-compromised/)
- [SOC Prime: Shai-Hulud Here We Go Again — TeamPCP](https://socprime.com/active-threats/shai-hulud-here-we-go-again-worm-by-teampcp-hits-npm-and-pypi/)
- [Phoenix Security: durabletask PyPI Wave Four](https://phoenix.security/teampcp-github-breach-durabletask-pypi-supply-chain-wave-four-2026/)
