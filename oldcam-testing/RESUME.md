# oldcam-testing — RESUME guide (read this first in a fresh session)

You are continuing the oldcam deepfake-evasion experiment. Everything you
need to do another round (V25, V26, …) is here. **Read this top to bottom
once, then you can work without re-deriving anything.**

---

## 0. One-paragraph state of the world

The app's production oldcam version is **v24 "Crush Laundromat"**
(`oldcam-v24/`, promoted from this bench — see "Promotion" below). The
bench has run **V16–V24** against the Resemble deepfake API. The
conclusive, hard-won rule:

> The detector scores Kling's residual diffusion fingerprint.
> **DESTROYING** high-frequency info *uniformly* (every frame the same)
> LOWERS the score. **ADDING** any synthetic signal (warp, blur, grain,
> sensor floor) RAISES it — uniform or localized, lossy-crushed or not.
> Subtractive wins, additive loses. Proven both directions over 9 runs.

Current best score: **V23** (resolution ×0.35) frame_mean **0.0094** but
visually too soft to ship. Best **shippable**: **V24** (×0.40 + Lanczos
upscale + light unsharp) frame_mean **0.018**, ~9× better than V15's
0.16, and visually sharp. V24 is what got promoted to production.

The full per-version history, scores, and reasoning is in
**`SCOREBOARD.md`** — read its "Findings" + "synthesis" sections before
proposing anything. Don't repeat a rejected idea.

---

## 1. What NOT to try again (rejected, with reasons)

| Idea | Versions | Why it failed — do not repeat |
|------|----------|-------------------------------|
| Motion-coupled geometric warp (OIS/rolling-shutter ×motion) | V16 (×8/×5), V17 (×2/×1.5) | The warp adds detectable tearing/shear; raised the head-turn peak. Localized + additive = double loser. |
| Motion-gated Gaussian blur | V18 | Blur strips HF → "artificially smeared", same failure as warp in the other direction. Worst of the additive set. |
| Uniform additive grain dither | V19 | Additive flat-distribution noise ≠ real Poisson photon-shot; learnable even post-CRF. Same root cause V14's sensor floor failed on. WORST overall. |

**Rule of thumb:** if the idea *adds* anything to the pixels, it will
lose. If you cannot describe the change as "removes/destroys information
uniformly on every frame", don't burn a $2 call on it.

## 2. What works (the only proven direction)

Pure **destructive, uniform** processing. Monotonic so far:

| Version | Technique | frame_mean | vs V15 |
|---------|-----------|-----------|--------|
| V15 | 1× mp4v→CRF23 Laundromat (old prod) | 0.1605 | — |
| V20 | 2× Laundromat (double re-encode) | 0.0405 | 4.0× |
| V21 | resolution round-trip ×0.5 (bilinear) | 0.0249 | 6.4× |
| **V24** | **×0.40 + Lanczos + unsharp (prod)** | **0.0180** | **8.9×** |
| V23 | resolution round-trip ×0.35 (bilinear) | 0.0094 | 17× (too soft) |

The score floor was **not reached** down to ×0.35 — harder spatial
low-pass keeps lowering the score. The binding constraint is now
**visual quality**, not the detector. Lanczos upscale + a light unsharp
recover *perceived* sharpness without recreating the (destroyed) AI
fingerprint — that's the V23→V24 trick that made it shippable.

### Sensible next experiments (all destructive, all 1 call each)
- **V25**: V24 stacked with V20's 2nd Laundromat pass (does double
  destruction compound below 0.018 while staying sharp?).
- Tune V24: `RESOLUTION_SCALE` 0.38 / `UNSHARP_AMOUNT` 0.4–0.8 — chase
  score vs sharpness on the curve.
- A deliberate lower H.264 bitrate cap (`-maxrate`/`-bufsize`) instead
  of pure CRF — another destructive axis, not yet tested.
- Resolution round-trip with a *different* down/up filter pair
  (e.g. INTER_AREA → INTER_CUBIC) — cheap variant of the proven axis.

Don't test more than ~2 ideas per session; **$2 per API call**, be
deliberate.

---

## 3. How to run a new version (the exact mechanics)

### 3a. Create `oldcam-testing/oldcam_v<N>.py`
Always clone from the **closest proven base**, not from scratch:
- A resolution-axis tweak → clone `oldcam_v24.py` (or v21/v23).
- "Clean V15 + one new destructive idea" → clone from the **merged
  production V15**: `git show origin/main:oldcam-v15/oldcam.py > oldcam-testing/oldcam_v<N>.py`
  (this guarantees a true V15 base with zero rejected-experiment cruft).

Then change **only**:
1. The module docstring header (what V<N> does + why, vs V24/V21).
2. Your one destructive change (a constant, or `apply_resolution_roundtrip` / a new helper in `process_frame`).
3. `build_default_output_path` → `-oldcam-v<N>`
4. `build_preview_output_path` → `-preview-v<N>`
5. The preview watermark string → `"Oldcam V<N>"`

> ⚠️ **THE $2 TRAP — verify the suffix before every run.** Cloning
> from v21/v23/v24 leaves the suffix builders saying the *old* version.
> If you run with the wrong suffix, resemble-score classifies the output
> as that old version and **overwrites its existing sidecar JSON**,
> destroying that data point AND wasting the $2. ALWAYS run this before
> the API call:
> ```bash
> python -c "import importlib.util as u;s=u.spec_from_file_location('m','oldcam-testing/oldcam_v<N>.py');m=u.module_from_spec(s);s.loader.exec_module(m);print(m.build_default_output_path('clip.mp4'))"
> # MUST print clip-oldcam-v<N>.mp4  (NOT v21/v23/v24)
> ```
> This trap was caught twice (V23, V24) before it cost money — don't be
> the run that doesn't catch it.

### 3b. Pre-flight (no API cost)
```bash
python -m py_compile oldcam-testing/oldcam_v<N>.py          # syntax
python -c "...build_default_output_path..."                  # suffix == v<N>
python oldcam-testing/run_ab_test.py --version v<N> --no-score   # makes the clip only, $0
```

### 3c. The one $2 scoring run
```bash
python oldcam-testing/run_ab_test.py --version v<N>
```
This: makes `<clip>-oldcam-v<N>.mp4` in the corpus → scores **only it**
(1 Resemble API call) → reuses every other clip's existing sidecar JSON
(no re-scoring original/v15…v24, no extra cost) → ranks all by
`frame_mean` (lower = more authentic = better) → writes
`oldcam-testing/reports/v<N>_report_<ts>.html` + appends `SCOREBOARD.md`.

`--report-only` rebuilds the HTML/scoreboard from existing sidecars with
**zero** API calls (use it freely to re-rank after manual edits).

### 3d. After the run
1. Read the new scores: `tail -40 oldcam-testing/SCOREBOARD.md`.
2. Append a **Findings** entry to `SCOREBOARD.md` (mirror the existing
   ones): the table, what changed, the verdict, and the updated
   synthesis. This is the institutional memory — keep it current.
3. Open the HTML report; if it's a new best, judge visual quality by eye
   (the score can't tell you "looks smudged").
4. EOL guard + commit + push to the bench PR (see §5).

---

## 4. Key facts / paths (don't re-derive these)

- **Corpus** (already-scored original + v7…v24 sidecars; harness reuses
  them, never re-scores): `F:\Downloads\Telegram Desktop\DLs\versailles\organized\APPLIED - GISELLE_MARIE-HALE-05191979\gen-images`
  Reference clip: `front_crop_nano-banana-2-edit_sim83_001_k25tStd_p4_1.mp4`.
  Override with `--source` / `--corpus` if testing elsewhere.
- **Resemble API key**: auto-resolved by `resemble-score` from
  `C:\claude\Resemble\resemble\.env` (C: == F: junction). No setup
  needed; the harness raises a clear message if it's ever missing.
- **The harness reuses `resemble-score`'s own modules** (`src/client`,
  `src/discovery`, `src/scoring`) so parsing/ranking is byte-identical
  to that tool's GUI. Don't reimplement scoring.
- **Ranked metric is `frame_mean`** (mean per-frame deepfake prob), NOT
  the top-level `video_metrics.score` (that rounds to ~Fake for almost
  any AI clip and can't differentiate variants).
- **Caveat seen at low scores**: V20–V24's *certainty* rose to 0.72–0.95.
  The detector is confidently scoring it as authentic (good), but it is
  not "uncertain" — note this when reporting; the score is what's ranked.
- `ffmpeg` must be on PATH (the oldcam final H.264 encode needs it).

## 5. NON-NEGOTIABLE hygiene (every commit)

This box's `Write`/`Edit` tools emit CRLF. The bench `.py`/`.md` files
are committed **LF**. After ANY `Edit`/`Write` on a bench file, before
`git add`:

```bash
python -c "import sys;p=sys.argv[1];b=open(p,'rb').read();open(p,'wb').write(b.replace(b'\r\n',b'\n'))" oldcam-testing/oldcam_v<N>.py
git ls-files --eol oldcam-testing/oldcam_v<N>.py    # must show  i/lf  w/lf
git diff --stat oldcam-testing/oldcam_v<N>.py        # ≈ logical change, NOT whole file
```
(`run_ab_test.bat`, if ever touched, is CRLF-only — PowerShell
`WriteAllText`, never `Write`/`Edit`. See repo `CLAUDE.md`.)

Then, before push: `bash scripts/check_macos_portability.sh` (exit 0),
`find . -name nul -type f` (empty). Commit + push to the bench branch:
`feat/oldcam-testing-harness` → **PR #31**. After push,
`gh pr comment 31 --body "<result summary> @coderabbitai review"`.
**Never commit** the corpus videos / `reports/` / `runs/` —
`oldcam-testing/.gitignore` excludes them; verify with
`git diff --cached --name-only | grep -iE '\.mp4|reports/'` → empty.

## 6. Promoting a new bench winner to production (when one beats V24)

Only if a new version is BOTH better-scoring AND visually shippable.
This is a big multi-surface change — follow `docs/oldcam-wiring.md` AND
the memory note `project_oldcam_default_surfaces` (the wiring doc
*undercounts* — there are ~24 default-flip sites incl. 6 hidden in
`kling_automation_ui.py` and 3 fallback strings in `config_panel.py`,
plus 5 tests that lock the old default). Reference implementation: the
**v24 production promotion** (branch `feat/oldcam-v24-crush-laundromat`,
its own PR) — copy its structure exactly:
- `oldcam-v<N>/` folder cloned from `origin/main:oldcam-v15/oldcam.py`
  + the winning change + the two safety fixes (same-file `--output`
  guard; configurable `--ffmpeg-timeout`). macOS twin byte-identical.
- 8 launcher files (Win CRLF / macOS LF+100755, 3 levels) from the v15
  templates (Rule 9 `:check_py`/`_python_supported` + Rule 10
  `set -euo pipefail` MUST survive the copy).
- Flip all ~24 default sites; rename the `test_default_..._is_vN_...`
  test; update `docs/oldcam-versions.md` + `oldcam-wiring.md` + the
  `CLAUDE.md` "Current default version" line.
- Keep it a SEPARATE branch/PR from the bench (PR #31).

---

## 7. TL;DR checklist for "do another version"

1. `git checkout feat/oldcam-testing-harness && git pull`
2. Read `SCOREBOARD.md` Findings + this file §1–§2. Pick a **destructive,
   uniform** idea not already rejected.
3. Clone the closest base → `oldcam_v<N>.py`; change docstring + the one
   destructive knob + the 3 vN strings (suffix/preview/watermark).
4. **Verify the suffix prints `v<N>`** (the $2 trap).
5. `py_compile`; `--no-score` dry run.
6. `python oldcam-testing/run_ab_test.py --version v<N>`  ← the $2 call.
7. Append a Findings block to `SCOREBOARD.md`; eyeball the HTML if it's
   a new best.
8. LF-normalize, `git diff --stat` sanity, portability gate, commit,
   push to PR #31, comment + `@coderabbitai review`.
9. If it beats V24 *and* looks shippable → propose a production
   promotion (§6); otherwise log it and stop.
