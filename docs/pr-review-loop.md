# Default PR + Review Loop (MANDATORY) — autonomous workflow after feature work

> **Relocated from `CLAUDE.md` (2026-06-06) to reduce always-loaded context.**
> Still binding. Agents working on this repo run this loop end-to-end
> WITHOUT pausing to ask permission for intermediate steps. The CLAUDE.md
> entry is now a one-paragraph summary + a pointer to this file; the full
> contract lives here.

User directive (2026-05-21): "i want u to always push commit into pr on
branch... never work on main, then run code reviewer ur own while bots
work, then when done implementing subagent findings, should be enough
time for checking bot comments". This is the **default loop** whenever
feature/fix work reaches a runnable state — don't skip steps unless the
user explicitly says "skip review" or "just push".

## RUN THIS LOOP AUTONOMOUSLY — do NOT ask permission at each step (2026-06-01)

Reinforced user directives (2026-06-01): "commit everything always into
pr... your workflow automatically should always be work on branch -> into
PR -> poll review bots and run codereviewer subagent automatically as u wait
for bots -> fix -> prep for merge, etc. i need u to be doing this
autonomously." And: "u do PR then trigger bots as soon as they are triggered
u run code-reviewer subagent in the meantime, then u address all findings and
then u prep to merge (u dont merge till i tell u to) and u autonomously do
this for as many rounds as possible to be reasonably clean unless i say
otherwise.. and u never wait asking me whether u should commit.. and u shall
not be lazy."

See the `feedback_autonomous_pr_review_loop` memory for the agent-private
version. The loop below is the **unconditional default** — execute it
end-to-end WITHOUT pausing to ask "should I open a PR?", "should I trigger
bots?", "should I run the subagent?", "should I commit?", or "want me to fix
these?". Just do it:

1. Work reaches a runnable state → branch (if on `main`) → commit → push.
   Never wait to ask whether to commit.
2. **Immediately + automatically**: open/refresh the PR, trigger the bots,
   AND (in the meantime, while bots run) spawn the code-reviewer subagent on
   the full branch diff — all in the same turn, no permission ask. Run the
   subagent on EVERY PR with reviewable content, including SUBSTANTIVE
   docs-only ones (workflow/contract/instruction changes like this very
   section — it catches contradictions and ambiguity, not just code bugs).
   Pure typo / whitespace / single-word docs fixes have nothing cross-cutting
   to review and fall under the "Skip conditions" list at the end of this
   doc — don't spin up a subagent for those.
3. Address subagent findings, then bot findings, per the triage rubric —
   **fix everything reasonable, don't defer** (see
   `feedback_dont_defer_fix_everything`). Commit + push each fix batch,
   which re-triggers bots.
4. Loop for **as many rounds as possible to be reasonably clean** — keep
   re-triggering bots + re-running the subagent after each fix batch until
   no real findings remain (or the user says otherwise), then prep for merge.

The ONLY things that still require an explicit user check are: the **final
merge itself**, **outward-facing/irreversible actions**, and **genuine
branch-choice ambiguity** (per Step 1 below — "If unsure which branch to use,
ask"). Everything else up to "PR is green and merge-ready" runs autonomously.
Asking permission for the intermediate steps is the lazy/slow pattern the
user has corrected repeatedly — opening the PR, committing, engaging
reviewers, running the subagent, and applying fixes are NOT decisions to
surface; they're the job. When a turn ends with work pushed, the
PR/bots/subagent should already be in flight, not waiting on a question.
(The "Skip conditions" list at the end of this doc still applies —
autonomy is the default, not a removal of those exits.)

## 1. Never work on `main`

All work happens on a feature branch tied to a PR. If `main` is checked
out, create or switch to the right branch BEFORE editing. Use
`git rev-parse --abbrev-ref HEAD` to confirm. If unsure which branch to
use, ask the user rather than guessing.

## 2. Commit + push to the PR branch when work reaches a runnable state

"Runnable state" = tests pass + portability gate green + the change
doesn't leave the GUI/CLI in a broken intermediate. **Push every such
commit to the remote PR branch** — the user works on multiple machines
(macOS + Windows) and pulls from the remote, so unpushed local work is
invisible. Commit-message contract:

* Type prefix: `feat:` / `fix:` / `perf:` / `chore:` / `refactor:` / `docs:`
* One-line subject explaining the user-visible outcome
* Body explains the WHY + cites the finding/bot/issue source if any
* Last line: `Co-Authored-By: Claude Opus <noreply@anthropic.com>`

Pre-commit invariants — fail-fast if any violated:

- `bash scripts/check_macos_portability.sh` exits 0
- `.venv311/bin/python -m pytest tests/ similarity/tests/ -q` (on macOS)
  or equivalent on Windows passes
- EOL invariants preserved on every edited file
  (`git ls-files --eol` left column == right column, CR counts match
  HEAD on CRLF files)
- No `nul` file in tree (`rm -f nul`)

## 3. Trigger bot reviews + spawn code-reviewer subagent in PARALLEL

Right after `git push`, do BOTH of these in the same turn (parallel
tool calls, not sequential):

**a) Trigger PR bots on the PR.** Single comment listing all active
   bot mentions:

   ```text
   Branch head: <sha> — <one-line summary>. Bot pass please.

   @coderabbitai review
   @codex review
   @gemini-code-assist review
   ```

   Include `@sourcery-ai` — it was silent across PR #43 (hence the prior
   "skip" advice) but resumed responding with actionable findings on PR #49
   round 1. Drop the skip until it goes silent on a fresh PR.

**b) Spawn the code-reviewer subagent on the ENTIRE branch diff.** Use
   the `general-purpose` agent type with a prompt that:
   * Points it at the FULL branch diff (`git diff main...HEAD`), NOT
     the latest commit range. User directive 2026-05-21: "i think the
     subagent codereview should better be ran on the entire branch
     diff, not just the commit individuals." Reasoning: the subagent's
     unique value over the bots is cross-cutting analysis — it catches
     bugs in code that LOOKED safe at commit-time but interacts badly
     with adjacent code added in earlier commits on the same branch.
     Bots already do the per-commit review well; the subagent should
     do what bots are bad at.
   * Names the project context (macOS + Windows Tk app + the
     wiring-doc cross-references for similarity / oldcam / rPPG).
   * Asks for severity-tagged findings (CRITICAL/HIGH/MEDIUM/LOW).
   * Caps the response length (1500-2000 words).
   * Includes a list of findings ALREADY ADDRESSED in earlier commits
     on the same branch with their commit SHAs — tells the subagent
     NOT to re-flag them. Without this list, the subagent re-discovers
     the same bugs every round and the report becomes noise.
   * If branch is large (>1500 LOC of net diff) and would exceed the
     subagent's effective review window, focus on the high-traffic
     files (touched in 3+ commits) and the new files. Explicitly say
     so in the prompt — don't silently sample.

The subagent typically returns in 4-7 minutes; bots typically respond
in 5-30 minutes. Running them in parallel cuts wall-clock by ~40%.

## 4. Address subagent findings first

The subagent returns before the bots usually do. Triage its findings
using a SINGLE rubric (the prior draft had two conflicting thresholds —
fixed here per the code-review M5 finding on 4ddb0252):

- **CRITICAL / HIGH**: fix in this round, write a regression test if
  the bug was non-obvious, commit + push to the PR branch.
- **MEDIUM**: fix in this round UNLESS the work is genuinely large
  (subjectively: ">2 hours of focused work" or "requires an API
  change in code the user explicitly asked not to touch"). Otherwise
  do it now. Do NOT preemptively defer mediums as "V2 work" — the
  user has called this out as a lazy pattern. The shipping cost of
  carrying a known medium into the next round is always higher than
  the cost of fixing it now.
- **LOW**: defer to a cleanup pass at PR-close.

## 5. Check bot comments

By the time subagent fixes are pushed, bots should have responded.
Pull the new comments using whichever snippet matches the current
shell. Both forms produce identical filtered output; the difference
is purely quoting (M6 code-review on 4ddb0252 — the prior single
bash snippet was unusable from the L3 Windows machine's PowerShell):

**bash / zsh / git-bash** (macOS, WSL, git-bash on Windows):
```bash
SINCE="<timestamp-of-trigger>"
gh api "repos/<owner>/<repo>/pulls/<n>/comments?per_page=50" \
  --paginate --jq '.[] | select(.created_at > "'"$SINCE"'" \
  and .user.login != "<your-gh-username>")'
```

**PowerShell** (native Windows shell):
```powershell
$SINCE = "<timestamp-of-trigger>"
$Q = '.[] | select(.created_at > "' + $SINCE + '" and .user.login != "<your-gh-username>")'
gh api "repos/<owner>/<repo>/pulls/<n>/comments?per_page=50" `
  --paginate --jq $Q
```

Per-bot disposition (per PR #43 retro, evidence-driven):

- **Codex** (chatgpt-codex-connector): highest signal-to-noise. P1/P2
  badges. Catches semantic contract violations and cross-cutting bugs.
  Address every P1 same round; P2 same round if tractable.
- **Gemini** (gemini-code-assist): broadest coverage. HIGH/Medium/Low.
  Catches widget patterns, perf nits, threading. Address HIGH same
  round; triage mediums per the V2 rule above.
- **CodeRabbit** (coderabbitai): thorough but noisy. Major/Minor +
  inline lint rules. Address Major same round; batch Minors at PR-close.
  Skip its "Analysis chain" issue-level comments — those are
  verification scripts CR ran, not findings to address.
- **Sourcery**: was silent across PR #43; resumed on PR #49. Concise,
  contract-level findings (docstring drift, no-raise guarantees, etc.).
  Address Major same round; defer LOW.

## 6. Address bot findings + reply inline

For each finding:

1. Fix the code if Step 4's triage rules say "this round."
2. Reply inline on the comment with `gh api -X POST .../comments/<id>/replies`
   pointing at the fix commit SHA.
3. If declining/deferring, post a real rationale (NOT "V2 work").
4. Commit + push the fix batch as ONE commit with a message that
   itemizes all the addressed findings.

## 7. Post a round wrap-up comment

After pushing the fix batch, post a PR comment summarizing the round:
a table of "finding → fix commit / disposition", final test count,
portability gate result, branch head SHA. This is the user's entry
point when they pull the branch on another machine.

## 8. Loop if findings keep landing

If the bot pass produces findings that warrant a new fix commit, the
next push triggers a fresh bot round. Repeat from step 3. Two rounds
is normal; three rounds is acceptable; four rounds usually means a
CRITICAL was missed in the original implementation and we should
pause to do a wider audit before continuing.

## 9. After merge: refresh the SSD + rebuild distributable (macOS only)

This step is macOS-only — the SSD bootstrap setup at
`/Volumes/st7Private/code/selfie-gen-ultimate/` doesn't exist on the
Windows machine. **On Windows, skip step 9 entirely**; the merge itself
is the end of the loop.

On macOS: once the PR squash-merges to `main`, immediately do the
post-merge refresh: `git pull` on the SSD source repo, refresh the
`_user_state/app_support/` snapshot from the live Application Support
dir, build a fresh `dist/SelfieGenUltimate-{vX.Y}.zip` and drop it on
the SSD root. Full playbook + verification commands in
[`ssd-and-distributables.md`](ssd-and-distributables.md).

Skip the SSD refresh when `/Volumes/st7Private/` isn't mounted; in
that case, explicitly tell the user the SSD copy is now stale instead
of silently ignoring it. Only rebuild the SSD's `venv-macos.tar` if
the merged PR touched `requirements.txt` or `requirements-hashed.txt`.

## Documenting cross-PR work (added v2.24)

When the diff in your branch documents, follows up on, or depends on a
code change that lives in a SIBLING PR (e.g. a docs PR that explains a
feature whose code shipped in a feature PR), **cite the source PR + the
specific commit SHA explicitly** in every doc / CHANGELOG entry that
touches the cross-PR surface. Use language like:

> **Source of the code change:** the `dev` extra landed via
> [PR #79](https://github.com/.../pull/79) (`feat/macos-polish-post-v2.21`),
> commit `de161c04`, in the v2.24 release round.

Why this rule exists: PR #80 round 2 of the v2.24 audit shipped three docs
describing the `dev` extra contract — but the extra itself was in the
sibling PR #79. A reader checking out PR #80 alone would have read a
contract pointing at code that wasn't in their tree. Without the
explicit "Source of the code change" callout pointing at PR #79 + the
SHA, the doc becomes a checklist failure the next time someone runs it.

Three places this rule applies in practice:
- **CHANGELOG entries** that span multiple PRs in the same release
  round: split the `Added` / `Changed` / `Fixed` sub-sections per PR
  with explicit headers like `### Added (PR #79 — code)` /
  `### Added (PR #80 — docs)` so a future reader can attribute each
  line correctly.
- **Doc cross-references** (e.g. `docs/uv-migration.md` describing a
  contract whose implementation lives elsewhere): open the section
  with a blockquote citing the source PR + commit SHA, and use
  past-tense "landed via PR #X" rather than in-flight "ships in
  PR #X" so the wording stays accurate after merge.
- **Code comments** referring to a fix from another branch (e.g. a
  comment in `kling_gui/queue_manager.py` pointing at a docs entry
  for a related fix in a different PR): cite both the doc path AND
  the originating PR/commit so the breadcrumb survives a doc
  relocation.

The corollary: **never write a doc that depends on uncited sibling-PR
code.** If your docs PR can't merge cleanly without a sibling PR
landing first, say so in the PR description AND in every doc that
touches the dependency. The reviewer + the future-you reading the
merged main both need that pointer.

## Skip conditions (don't run the full loop)

- The user explicitly says "skip review" / "just push" / "WIP"
- The commit is purely typo / whitespace / single-word fix (NOT
  workflow / contract / instruction doc changes — those still get the
  subagent per step 2's carve-out, even when they touch only
  `*.md` files)
- The branch is mid-experiment and not ready for review (push without
  trigger; resume the loop when work stabilizes)
- The user says "don't engage the bots yet"

## Why this is committed to the repo (not just a local memory)

The user works on multiple machines and pulls from the remote branch.
Local-only memories live in `~/.claude/projects/...` on a single
machine; this file ships with the repo so the same workflow runs on
Windows after `git pull`. If you find yourself wanting to add a
workflow rule that should apply on both OSes, put it HERE (or in
`CLAUDE.md` for the always-loaded summary), not in a local memory.
