# Temporal Forensics — the discovery, the model, the calibration

This documents *why* `src/forensics.py` exists and how its thresholds
were derived, so future tuning is evidence-based, not guesswork.

## The discovery (the core insight)

oldcam V24 ("Crush Laundromat" = V15 + a uniform resolution round-trip)
beats the Resemble deepfake detector by **destroying the spatial
high-frequency diffusion fingerprint** that AI renders carry. It does
nothing to the temporal dimension.

Three clips with **known** Resemble outcomes through the *identical* V24
code told the whole story:

| Clip | Original | After V24 | Outcome |
|------|---------:|----------:|---------|
| GISELLE `front_crop…sim83` | 0.99 | **0.018** | huge win |
| sim86 `face_crop…sim86` (maroz-face) | 1.00 | **0.45** | partial win |
| signal `signal-2026-05-17-142926` | 1.00 | **1.00** | total failure |

"Saturated original (1.00)" was **not** the explanation — sim86 *and*
signal both started at a flat 1.00, yet V24 cut one in half and did
*nothing* to the other. The difference was in the videos themselves.

Whole-clip motion analysis (no model, just frame-to-frame deltas):

| Metric | GISELLE (win) | sim86 (win) | signal (fail) |
|--------|--------------:|------------:|--------------:|
| smoothness (motion std/mean) | 0.34 | 0.34 | **0.90** |
| burst % (>2.5× median motion) | 0 | 0 | **16** |
| freeze % (<0.25× median) | 0 | 0 | **18** |
| jerk (‖2nd-deriv motion‖/median) | 0.20 | 0.18 | **0.76** |

The two clips V24 helped are **temporally smooth**. The clip V24 could
not touch has a **broken cadence** — bursts of fast motion interleaved
with near-frozen frames, jerky acceleration: the signature of a
low-fps-upsampled or generation-unstable AI clip, or a messenger
re-encode (this one came through Signal). V24 destroys spatial pixels;
this temporal tell survives intact → score doesn't move.

**Conclusion:** the detector keys on two independent tells. oldcam can
only attack the spatial one. A clip's *temporal cadence* predicts, for
free, whether scoring it is worth the money.

## The model

`temporal_instability_score = smoothness + jerk + burst%/10 + freeze%/10`

Equal-ish weights, no fitted coefficients — at n=3 the signal margin was
~10× (0.5 vs 5.0), far too strong to need a regression. Each term is a
ratio on a comparable scale; burst/freeze percentages are /10 so a 16%
burst contributes ~1.6 (matching the empirical separation). Revisit the
weighting only if labelled data shows the simple sum mis-ranks.

Verdict bands (`TEMPORAL_THRESHOLDS`):

- `composite ≤ spatial_max (0.80)` → **spatial** (score it, expect a win)
- `composite ≥ temporal_min (1.30)` → **temporal** (don't; re-generate)
- between → **uncertain** (score to find out)

## The calibration (181-clip corpus)

Ran the analyzer offline (0 API calls) over 181 Kling/nano-banana/
gpt-image originals from the omnapayments corpus. 0 read failures.

| Statistic | composite |
|-----------|----------:|
| min | 0.33 |
| p25 | 0.52 |
| **median** | **0.63** |
| p75 | 0.83 |
| p95 | 1.26 |
| max | **2.16** |

Distribution under the calibrated thresholds:

- **spatial ~65%** (≤0.80) — clean renders, V24 candidates
- **uncertain ~23%** (0.80–1.30) — score to find out
- **temporal ~4%** (≥1.30) — broken cadence, skip the API call;
  these clustered on `gpt-image` / `selfie-expanded` / `looped` pipelines

Key calibration facts that anchor the thresholds:

- Both **proven V24 wins** (GISELLE 0.585, sim86 0.517) sit safely under
  `spatial_max=0.80`, inside the tight corpus bulk.
- The **proven V24 failure** (signal, composite 4.97) is FAR above the
  entire corpus (max 2.16) — it is a genuine extreme outlier (a
  Signal-relayed messenger re-encode), not representative of normal
  generation. The corpus's own worst cases (1.6–2.2) are the realistic
  "hard" band.
- `temporal_min=1.30` sits above ~96% of the corpus → only the genuinely
  broken minority is flagged "don't bother", which is the bias we want
  (a false "skip" wastes a usable source; a false "score" only wastes
  one API call).

## Limitations / when to re-tune

- The score is intrinsic-cadence only. It predicts "is the tell spatial
  (V24-fixable) or temporal (not)" — it does **not** predict the exact
  post-V24 score, only whether V24 has a mechanism to help at all.
- Calibrated on 184 clips total (181 corpus + 3 ground-truth). Only 3
  have *confirmed API outcomes*; the 181 are distribution-only. As more
  clips get scored, record (composite, actual outcome) pairs and tighten
  the bands. The oldcam-testing `SCOREBOARD.md` / `RESUME.md` are the
  system of record for confirmed outcomes.
- A "temporal" verdict is a recommendation, not a guarantee — the one
  untested oldcam-domain lever for temporally-broken clips is a *uniform
  temporal-resmoothing* pass (the temporal analog of V24's spatial
  crush). Unproven; would be a future "V25" bench experiment.
