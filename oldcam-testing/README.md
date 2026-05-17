# oldcam-testing — V16 "Dynamic Stress" bench

Throwaway experiment area. **Not wired into the app** (no launcher in
`launchers/`, no `_discover_oldcam_versions` entry, no `config_panel`
checkbox, not in the main pytest suite). It exists to answer one question
before we invest in integrating V16 everywhere:

> Does V16's motion-coupled optical stress score better than V15 on the
> Resemble deepfake API?

## What's here

| File | Purpose |
|------|---------|
| `oldcam_v16.py` | Standalone V16. = V15 (mp4v + CRF 23 + ghosting 0.18 + zero noise) **plus** motion-coupled rolling-shutter (`shear × (1 + m·5)`) and OIS jitter (`offset × (1 + m·8)`), where `m` is a 0–1 frame-to-frame motion velocity. At `m == 0` (subject still) it is byte-behaviour-identical to V15. |
| `run_ab_test.py` | Make V16 → score ONLY it → rank vs the existing scored corpus → emit HTML. |
| `run_ab_test.bat` | Windows wrapper (uses the repo venv). |
| `SCOREBOARD.md` | Findings + auto-appended ranked tables (newest at bottom). |
| `reports/` | Generated standalone HTML reports (gitignored). |

## The V16 idea

V15's double-lossy "Laundromat" compression scored ~0.42 (out of the "fake"
band) by crushing latent-diffusion noise. But the detector still spikes
during rapid head turns — AI struggles with structural consistency across
fast perspective shifts. V16 measures per-frame pixel velocity (grayscale
`absdiff` → threshold → normalized pixel count) and **dynamically spikes**
rolling-shutter shear and OIS jitter only during high-motion moments,
physically blurring the AI's worst structural frames right before the mp4v
compressor crushes them. When the subject is still it behaves exactly like
V15, so the only thing under test is the motion reaction.

## Money-safe by design

The expensive part is the Resemble API call, and the team already has the
original Kling render + v7..v15 scored (sidecar `*.mp4.json` files sitting
in the corpus folder). So the harness **only ever scores the new V16
clip** (one API call) and **reuses every other clip's existing sidecar
JSON from disk** — no re-scoring the original or v15. It imports
`resemble-score`'s own `client` / `discovery` / `scoring` modules so the
parsing + ranking is byte-identical to that tool's GUI/CLI.

## Quick loop

```bat
:: Windows — make V16 from the default GISELLE clip, score it, rank vs the
:: corpus, write the HTML report + scoreboard row
oldcam-testing\run_ab_test.bat
```

```bash
# Default clip + corpus (the GISELLE gen-images folder):
python oldcam-testing/run_ab_test.py

# A different source clip / corpus:
python oldcam-testing/run_ab_test.py --source "F:/path/clip.mp4" \
    --corpus "F:/path/to/scored/folder"

# Make the V16 clip but don't spend an API call yet:
python oldcam-testing/run_ab_test.py --no-score

# Re-rank + rebuild the HTML from sidecars already on disk (zero API):
python oldcam-testing/run_ab_test.py --report-only
```

Each run:

1. Runs V16 on the source → `…-oldcam-v16.mp4` written **into the corpus
   folder** (next to the already-scored clips).
2. One Resemble API call scores just that V16 file; its `…-v16.mp4.json`
   sidecar is written (same shape resemble-score writes).
3. Loads every other clip's existing sidecar (no API), ranks all by
   **frame mean** (lower = more authentic), and writes:
   - `oldcam-testing/reports/v16_report_<ts>.html` — a self-contained
     report mirroring resemble-score's breakdown (per-clip frame
     mean/min/max, chunk mean, certainty, raw verdict, V16-vs-V15 delta,
     winner highlight). Open it in any browser.
   - a ranked table appended to `SCOREBOARD.md`.

## Requirements

- `RESEMBLE_API_KEY` discoverable by resemble-score — see
  `resemble-score/README.md` (`.env`, `C:\claude\Resemble\resemble\.env`,
  or env var). The harness raises a clean message if it's missing.
- `ffmpeg` on `PATH` (the oldcam final H.264 encode needs it).
- The repo Python env with `opencv-python` + `numpy` + resemble-score's
  deps (`requests`).

## Reading the result

`SCOREBOARD.md` carries a **Findings** section (human conclusions) plus the
raw ranked tables. Ranking is by **frame mean** — the per-frame Resemble
score that actually discriminates variants (the coarse top-level score
rounds to ~Fake for almost everything, so it can't compare). If the V16
row ranks **above** the good `…-oldcam-v15.mp4`, the motion idea helped →
consider promoting V16 into the app proper via the oldcam-wiring
checklist. If not, tune the `×5` / `×8` motion gains in `oldcam_v16.py`
and rerun — zero app churn.

> **First result (2026-05-17): V16 lost.** frame mean 0.1884 vs V15's
> 0.1605 — the `×8`/`×5` motion warp *raised* the head-turn peak instead
> of masking it. V15 stays the production champion. See SCOREBOARD.md.
