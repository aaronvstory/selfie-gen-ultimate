# Oldcam A/B Scoreboard

Resemble deepfake-API scores. **Lower = more real = better.**
Appended by `run_ab_test.py` (newest run at the bottom).

## Baseline context

- **V15 "Laundromat"** scored **~0.42 mean** on the Resemble API — out of
  the "fake" band. The double-lossy mp4v → CRF 23 chain crushed the
  latent-diffusion noise. Known weakness: the detector still spikes during
  rapid head turns (~5.0–6.9 s in the reference clip).
- **V16 "Dynamic Stress"** is the hypothesis under test: motion-coupled
  rolling-shutter (`×(1 + m·5)`) + OIS jitter (`×(1 + m·8)`) to physically
  blur the AI's worst structural frames during fast motion, before the
  compressor crushes them. Goal: beat V15's ~0.42, especially in the
  head-turn window.

The harness ranks by **frame mean** (Resemble per-frame deepfake
probability) — the discriminating signal. The coarse top-level
`video_metrics.score` (the "~0.42" figure) rounds to Fake for almost any
AI clip, so frame-mean is what we compare.

## Findings

### 2026-05-17 — V16 "Dynamic Stress" — REGRESSION (rejected)

| | Top score | Frame mean | Frame max |
|--|-----------|-----------|-----------|
| **V15** (Laundromat, champion) | 0.4245 | **0.1605** 🏆 | 0.4245 |
| **V16** (motion-coupled, ×8 OIS / ×5 RS) | 0.5283 | 0.1884 | 0.5374 |

V16 ranked **#2, behind V15** (frame-mean +0.028 worse). The motion
coupling *raised* the peak frame score (0.42 → 0.54): the aggressive
geometric warping during the head turn introduced its own detectable
artifacts instead of masking the AI's. Both still crush everything else
(next best, v12, is 0.63). **Decision: V15 stays the production champion.**

### 2026-05-17 — V17 "Dynamic Stress (Gentle)" — ≈V15, marginally worse (rejected)

Same mechanism as V16 with much gentler gains
(`MOTION_OIS_GAIN 8.0 → 2.0`, `MOTION_RS_GAIN 5.0 → 1.5`).

| | Top score | Frame mean | Frame min | Frame max |
|--|-----------|-----------|-----------|-----------|
| **V15** (champion) | 0.4245 | **0.1605** 🏆 | 0.0035 | 0.4245 |
| V16 (×8 / ×5) | 0.5283 | 0.1884 | 0.0034 | 0.5374 |
| **V17 (×2 / ×1.5)** | 0.5439 | 0.1660 | 0.0059 | 0.5597 |

V17 recovered most of V16's regression (0.1884 → 0.1660) but is still
**+0.0055 frame-mean above V15** — and the *peak* frame kept climbing
(0.42 → 0.56) even at gentle gains, while frame-min slightly worsened.

**Verdict: any motion-coupled geometric warp is a dead end.** Even gentle,
shear/translation adds peak artifacts faster than it masks the AI's. The
mean improves only because the still frames dominate the average — the
detector still spikes harder on the head-turn frames. **V15 remains the
production champion.**

### 2026-05-17 — V18 "Dynamic Blur" — WORST so far (rejected)

Clean V15 (baseline un-coupled OIS/RS) + a motion-gated Gaussian blur
(`MOTION_BLUR_MAX_KERNEL = 15`, kernel scales with the same
motion_multiplier; ~no-op when still). Hypothesis: blur masks the AI's
structural failure during the head turn *without* the geometric tearing
that sank V16/V17.

| | Top score | Frame mean | Frame min | Frame max |
|--|-----------|-----------|-----------|-----------|
| **V15** (champion) | 0.4245 | **0.1605** 🏆 | 0.0035 | 0.4245 |
| V17 (gentle warp) | 0.5439 | 0.1660 | 0.0059 | 0.5597 |
| V16 (hard warp) | 0.5283 | 0.1884 | 0.0034 | 0.5374 |
| **V18 (blur k=15)** | 0.5573 | **0.2046** | 0.0038 | 0.5797 |

**V18 is the WORST of all four experiments** (+0.044 vs V15; peak 0.58).
The hypothesis was wrong: the Resemble detector keys on each frame's
*frequency profile*. A Gaussian blur strips high-frequency detail, which
reads as "artificially smeared / low quality" — it deviates from a natural
frequency profile just as much as warp does, only in the other direction,
so it *raises* the score the same way.

### Synthesis — the real lesson (4 experiments, 4 API calls)

| Approach | Frame mean | vs V15 |
|----------|-----------|--------|
| **V15 — uniform double-lossy, every frame identical** | **0.1605** | — (best) |
| V17 — localized gentle warp on motion | 0.1660 | +0.0055 |
| V16 — localized hard warp on motion | 0.1884 | +0.028 |
| V18 — localized blur on motion | 0.2046 | +0.044 |

**Every localized, motion-gated per-frame manipulation lost** — warp *and*
blur, gentle *and* hard. They all create a frame whose frequency profile
deviates locally, which is exactly the anomaly the detector is built to
spot. V15 wins **because it treats every frame identically** (uniform
mp4v→CRF23 crush): there is no localized signature to flag.

### 2026-05-17 — V19 "Uniform Grain Dither" — WORST overall (rejected); refutes the "uniform wins" theory

PR #30 (V15 hotfix) is now **MERGED** as production. V19 was the first
*global/uniform* idea: clean V15 + a static-amplitude integer dither
(`GRAIN_DITHER_AMPLITUDE = 4`, [-4,+4] per pixel) added IDENTICALLY to
every frame (no motion gating), placed last so the mp4v→CRF23 Laundromat
crushes it into macro-blocks. Hypothesis: uniform grain breaks Kling's
residual diffusion flicker with no localized signature.

| | Top score | Frame mean | Frame min | Frame max | Certainty |
|--|-----------|-----------|-----------|-----------|-----------|
| **V15** (champion, merged) | 0.4245 | **0.1605** 🏆 | 0.0035 | 0.4245 | 0.1510 |
| V17 (gentle warp) | 0.5439 | 0.1660 | 0.0059 | 0.5597 | 0.0879 |
| V16 (hard warp) | 0.5283 | 0.1884 | 0.0034 | 0.5374 | 0.0566 |
| V18 (motion blur) | 0.5573 | 0.2046 | 0.0038 | 0.5797 | 0.1146 |
| **V19 (uniform dither)** | 0.6523 | **0.2485** | 0.0076 | 0.6736 | 0.3047 |

**V19 is the WORST of all five experiments** (+0.088 vs V15) and the
detector's *certainty rose* (0.15 → 0.30 — it got MORE sure it's fake).

### Refined synthesis — the real rule (5 experiments, 5 API calls)

V19 disproves "uniform manipulation wins". The actual rule:

> **V15 wins because it is purely DESTRUCTIVE — it only removes
> information (mp4v→CRF23 crush of Kling's artifacts). Any version that
> ADDS a synthetic signal loses — uniform or localized, lossy-crushed or
> not.** Additive integer dither has a flat distribution that does not
> match real sensor photon-shot noise (Poisson, luminance-dependent), so
> even after the CRF crush it leaves a learnable statistic. This is the
> same root cause V14 failed on (it added a sensor floor).

The losing axis is **additive vs subtractive**, not localized vs uniform.
The only thing that has ever helped is *removing* information uniformly
(compression). The "more destructive" prediction was then tested directly
in V20/V21 below — and it was emphatically correct.

### 2026-05-17 — V20 "Double Laundromat" + V21 "Resolution Round-Trip" — BOTH BEAT V15 DECISIVELY 🎯

Both are byte-identical clean V15 (cloned from `origin/main`) + ONE purely
destructive change, no added signal:

- **V20**: after the V15 H.264 file is written, run the whole mp4v→H.264
  CRF 23 Laundromat a SECOND time (4 lossy generations total).
- **V21**: each frame downscaled ×0.5 (INTER_AREA) then upscaled back
  (INTER_LINEAR) — a uniform spatial low-pass — before the V15 encode.

| Rank | Version | Approach | Top score | Frame mean | Frame min | Certainty |
|------|---------|----------|-----------|-----------|-----------|-----------|
| 🏆 1 | **V21** | resolution round-trip | 0.1352 | **0.0249** | 0.0007 | 0.7296 |
| 2 | **V20** | 2× Laundromat | 0.1379 | **0.0405** | 0.0016 | 0.7243 |
| 3 | V15 | production (1× Laundromat) | 0.4245 | 0.1605 | 0.0035 | 0.1510 |
| 4 | V17 | gentle warp (additive) | 0.5439 | 0.1660 | 0.0059 | 0.0879 |
| 5 | V16 | hard warp (additive) | 0.5283 | 0.1884 | 0.0034 | 0.0566 |
| 6 | V18 | motion blur (additive) | 0.5573 | 0.2046 | 0.0038 | 0.1146 |
| 7 | V19 | uniform grain (additive) | 0.6523 | 0.2485 | 0.0076 | 0.3047 |

**V21 is 6.4× better than V15** (0.1605 → 0.0249); **V20 is 4.0× better**
(→ 0.0405). Both also dropped the *top-level* score out of the fake band
(0.42 → 0.135) — not just the frame mean. Every additive experiment
(V16–V19) lost; every destructive one (V20, V21) won big.

### CONCLUSIVE synthesis — the rule, proven both ways (7 experiments)

> **The detector scores Kling's residual diffusion fingerprint. ANY
> processing that destroys more high-frequency information uniformly
> lowers the score; ANY processing that adds a synthetic signal raises
> it.** Subtractive wins, additive loses — confirmed in both directions
> across 7 A/B runs.

Caveat (honest): V20/V21's *certainty* rose to ~0.73 — the detector is
more *confident* about its (now far lower, ~"real") score. Score is the
ranked metric and the win is real (0.025 reads as authentic), but the
detector is not "uncertain", it confidently scores it as more real.

**Recommendation:** Promote the resolution round-trip into production
(supersede V15) via the oldcam-wiring checklist — it is the single best
technique found and a small, low-risk change. Next bench ideas, all
purely destructive: tune `RESOLUTION_SCALE` (0.4 / 0.35 — find the floor
before quality collapses), `V21 + V20` stacked, or a lower bitrate cap.
Stop testing additive ideas.

<!-- run results appended below this line -->

## 2026-05-17 14:28 — `front_crop_nano-banana-2-edit_sim83_001_k25tStd_p4_1.mp4`

| Rank | Video | Frame mean | Frame min | Certainty | Verdict |
|------|-------|-----------|-----------|-----------|---------|
| 1 | front_crop_nano-banana-2-edit_sim83_001_k25tStd_p4_1-oldcam-v15.mp4 | 0.1605 🏆 | 0.0035 | 0.1510 | Neutral/Uncertain |
| 2 | front_crop_nano-banana-2-edit_sim83_001_k25tStd_p4_1-oldcam-v12.mp4 | 0.6262 | 0.0610 | 0.9740 | Fake |
| 3 | front_crop_nano-banana-2-edit_sim83_001_k25tStd_p4_1-oldcam-v13.mp4 | 0.6597 | 0.0481 | 0.9844 | Fake |
| 4 | front_crop_nano-banana-2-edit_sim83_001_k25tStd_p4_1-oldcam-v8.mp4 | 0.8543 | 0.4395 | 1.0000 | Fake |
| 5 | front_crop_nano-banana-2-edit_sim83_001_k25tStd_p4_1-oldcam-v15-v1.mp4 | 0.9892 | 0.8990 | 1.0000 | Fake |
| 6 | front_crop_nano-banana-2-edit_sim83_001_k25tStd_p4_1.mp4 | 0.9936 | 0.9443 | 1.0000 | Fake |
| 7 | front_crop_nano-banana-2-edit_sim83_001_k25tStd_p4_1-oldcam-v14.mp4 | 0.9953 | 0.9639 | 1.0000 | Fake |
| 8 | front_crop_nano-banana-2-edit_sim83_001_k25tStd_p4_1-oldcam-v7.mp4 | 0.9983 | 0.9871 | 1.0000 | Fake |
| 9 | front_crop_nano-banana-2-edit_sim83_001_k25tStd_p4_1-oldcam-v10.mp4 | 0.9993 | 0.9953 | 1.0000 | Fake |
| 10 | front_crop_nano-banana-2-edit_sim83_001_k25tStd_p4_1-oldcam-v9.mp4 | 0.9993 | 0.9926 | 1.0000 | Fake |
| 11 | front_crop_nano-banana-2-edit_sim83_001_k25tStd_p4_1-oldcam-v11.mp4 | 0.9997 | 0.9980 | 1.0000 | Fake |

## 2026-05-17 14:29 — `front_crop_nano-banana-2-edit_sim83_001_k25tStd_p4_1.mp4`

| Rank | Video | Frame mean | Frame min | Certainty | Verdict |
|------|-------|-----------|-----------|-----------|---------|
| 1 | front_crop_nano-banana-2-edit_sim83_001_k25tStd_p4_1-oldcam-v15.mp4 | 0.1605 🏆 | 0.0035 | 0.1510 | Neutral/Uncertain |
| 2 | front_crop_nano-banana-2-edit_sim83_001_k25tStd_p4_1-oldcam-v16.mp4 | 0.1884 | 0.0034 | 0.0566 | Neutral/Uncertain |
| 3 | front_crop_nano-banana-2-edit_sim83_001_k25tStd_p4_1-oldcam-v12.mp4 | 0.6262 | 0.0610 | 0.9740 | Fake |
| 4 | front_crop_nano-banana-2-edit_sim83_001_k25tStd_p4_1-oldcam-v13.mp4 | 0.6597 | 0.0481 | 0.9844 | Fake |
| 5 | front_crop_nano-banana-2-edit_sim83_001_k25tStd_p4_1-oldcam-v8.mp4 | 0.8543 | 0.4395 | 1.0000 | Fake |
| 6 | front_crop_nano-banana-2-edit_sim83_001_k25tStd_p4_1-oldcam-v15-v1.mp4 | 0.9892 | 0.8990 | 1.0000 | Fake |
| 7 | front_crop_nano-banana-2-edit_sim83_001_k25tStd_p4_1.mp4 | 0.9936 | 0.9443 | 1.0000 | Fake |
| 8 | front_crop_nano-banana-2-edit_sim83_001_k25tStd_p4_1-oldcam-v14.mp4 | 0.9953 | 0.9639 | 1.0000 | Fake |
| 9 | front_crop_nano-banana-2-edit_sim83_001_k25tStd_p4_1-oldcam-v7.mp4 | 0.9983 | 0.9871 | 1.0000 | Fake |
| 10 | front_crop_nano-banana-2-edit_sim83_001_k25tStd_p4_1-oldcam-v10.mp4 | 0.9993 | 0.9953 | 1.0000 | Fake |
| 11 | front_crop_nano-banana-2-edit_sim83_001_k25tStd_p4_1-oldcam-v9.mp4 | 0.9993 | 0.9926 | 1.0000 | Fake |
| 12 | front_crop_nano-banana-2-edit_sim83_001_k25tStd_p4_1-oldcam-v11.mp4 | 0.9997 | 0.9980 | 1.0000 | Fake |

## 2026-05-17 14:45 — v17 — `front_crop_nano-banana-2-edit_sim83_001_k25tStd_p4_1.mp4`

| Rank | Video | Frame mean | Frame min | Certainty | Verdict |
|------|-------|-----------|-----------|-----------|---------|
| 1 | front_crop_nano-banana-2-edit_sim83_001_k25tStd_p4_1-oldcam-v15.mp4 | 0.1605 🏆 | 0.0035 | 0.1510 | Neutral/Uncertain |
| 2 | front_crop_nano-banana-2-edit_sim83_001_k25tStd_p4_1-oldcam-v16.mp4 | 0.1884 | 0.0034 | 0.0566 | Neutral/Uncertain |
| 3 | front_crop_nano-banana-2-edit_sim83_001_k25tStd_p4_1-oldcam-v12.mp4 | 0.6262 | 0.0610 | 0.9740 | Fake |
| 4 | front_crop_nano-banana-2-edit_sim83_001_k25tStd_p4_1-oldcam-v13.mp4 | 0.6597 | 0.0481 | 0.9844 | Fake |
| 5 | front_crop_nano-banana-2-edit_sim83_001_k25tStd_p4_1-oldcam-v8.mp4 | 0.8543 | 0.4395 | 1.0000 | Fake |
| 6 | front_crop_nano-banana-2-edit_sim83_001_k25tStd_p4_1-oldcam-v15-v1.mp4 | 0.9892 | 0.8990 | 1.0000 | Fake |
| 7 | front_crop_nano-banana-2-edit_sim83_001_k25tStd_p4_1.mp4 | 0.9936 | 0.9443 | 1.0000 | Fake |
| 8 | front_crop_nano-banana-2-edit_sim83_001_k25tStd_p4_1-oldcam-v14.mp4 | 0.9953 | 0.9639 | 1.0000 | Fake |
| 9 | front_crop_nano-banana-2-edit_sim83_001_k25tStd_p4_1-oldcam-v7.mp4 | 0.9983 | 0.9871 | 1.0000 | Fake |
| 10 | front_crop_nano-banana-2-edit_sim83_001_k25tStd_p4_1-oldcam-v10.mp4 | 0.9993 | 0.9953 | 1.0000 | Fake |
| 11 | front_crop_nano-banana-2-edit_sim83_001_k25tStd_p4_1-oldcam-v9.mp4 | 0.9993 | 0.9926 | 1.0000 | Fake |
| 12 | front_crop_nano-banana-2-edit_sim83_001_k25tStd_p4_1-oldcam-v11.mp4 | 0.9997 | 0.9980 | 1.0000 | Fake |

## 2026-05-17 14:46 — v17 — `front_crop_nano-banana-2-edit_sim83_001_k25tStd_p4_1.mp4`

| Rank | Video | Frame mean | Frame min | Certainty | Verdict |
|------|-------|-----------|-----------|-----------|---------|
| 1 | front_crop_nano-banana-2-edit_sim83_001_k25tStd_p4_1-oldcam-v15.mp4 | 0.1605 🏆 | 0.0035 | 0.1510 | Neutral/Uncertain |
| 2 | front_crop_nano-banana-2-edit_sim83_001_k25tStd_p4_1-oldcam-v17.mp4 | 0.1660 | 0.0059 | 0.0879 | Neutral/Uncertain |
| 3 | front_crop_nano-banana-2-edit_sim83_001_k25tStd_p4_1-oldcam-v16.mp4 | 0.1884 | 0.0034 | 0.0566 | Neutral/Uncertain |
| 4 | front_crop_nano-banana-2-edit_sim83_001_k25tStd_p4_1-oldcam-v12.mp4 | 0.6262 | 0.0610 | 0.9740 | Fake |
| 5 | front_crop_nano-banana-2-edit_sim83_001_k25tStd_p4_1-oldcam-v13.mp4 | 0.6597 | 0.0481 | 0.9844 | Fake |
| 6 | front_crop_nano-banana-2-edit_sim83_001_k25tStd_p4_1-oldcam-v8.mp4 | 0.8543 | 0.4395 | 1.0000 | Fake |
| 7 | front_crop_nano-banana-2-edit_sim83_001_k25tStd_p4_1-oldcam-v15-v1.mp4 | 0.9892 | 0.8990 | 1.0000 | Fake |
| 8 | front_crop_nano-banana-2-edit_sim83_001_k25tStd_p4_1.mp4 | 0.9936 | 0.9443 | 1.0000 | Fake |
| 9 | front_crop_nano-banana-2-edit_sim83_001_k25tStd_p4_1-oldcam-v14.mp4 | 0.9953 | 0.9639 | 1.0000 | Fake |
| 10 | front_crop_nano-banana-2-edit_sim83_001_k25tStd_p4_1-oldcam-v7.mp4 | 0.9983 | 0.9871 | 1.0000 | Fake |
| 11 | front_crop_nano-banana-2-edit_sim83_001_k25tStd_p4_1-oldcam-v10.mp4 | 0.9993 | 0.9953 | 1.0000 | Fake |
| 12 | front_crop_nano-banana-2-edit_sim83_001_k25tStd_p4_1-oldcam-v9.mp4 | 0.9993 | 0.9926 | 1.0000 | Fake |
| 13 | front_crop_nano-banana-2-edit_sim83_001_k25tStd_p4_1-oldcam-v11.mp4 | 0.9997 | 0.9980 | 1.0000 | Fake |

## 2026-05-17 14:58 — v18 — `front_crop_nano-banana-2-edit_sim83_001_k25tStd_p4_1.mp4`

| Rank | Video | Frame mean | Frame min | Certainty | Verdict |
|------|-------|-----------|-----------|-----------|---------|
| 1 | front_crop_nano-banana-2-edit_sim83_001_k25tStd_p4_1-oldcam-v15.mp4 | 0.1605 🏆 | 0.0035 | 0.1510 | Neutral/Uncertain |
| 2 | front_crop_nano-banana-2-edit_sim83_001_k25tStd_p4_1-oldcam-v17.mp4 | 0.1660 | 0.0059 | 0.0879 | Neutral/Uncertain |
| 3 | front_crop_nano-banana-2-edit_sim83_001_k25tStd_p4_1-oldcam-v16.mp4 | 0.1884 | 0.0034 | 0.0566 | Neutral/Uncertain |
| 4 | front_crop_nano-banana-2-edit_sim83_001_k25tStd_p4_1-oldcam-v18.mp4 | 0.2046 | 0.0038 | 0.1146 | Neutral/Uncertain |
| 5 | front_crop_nano-banana-2-edit_sim83_001_k25tStd_p4_1-oldcam-v12.mp4 | 0.6262 | 0.0610 | 0.9740 | Fake |
| 6 | front_crop_nano-banana-2-edit_sim83_001_k25tStd_p4_1-oldcam-v13.mp4 | 0.6597 | 0.0481 | 0.9844 | Fake |
| 7 | front_crop_nano-banana-2-edit_sim83_001_k25tStd_p4_1-oldcam-v8.mp4 | 0.8543 | 0.4395 | 1.0000 | Fake |
| 8 | front_crop_nano-banana-2-edit_sim83_001_k25tStd_p4_1-oldcam-v15-v1.mp4 | 0.9892 | 0.8990 | 1.0000 | Fake |
| 9 | front_crop_nano-banana-2-edit_sim83_001_k25tStd_p4_1.mp4 | 0.9936 | 0.9443 | 1.0000 | Fake |
| 10 | front_crop_nano-banana-2-edit_sim83_001_k25tStd_p4_1-oldcam-v14.mp4 | 0.9953 | 0.9639 | 1.0000 | Fake |
| 11 | front_crop_nano-banana-2-edit_sim83_001_k25tStd_p4_1-oldcam-v7.mp4 | 0.9983 | 0.9871 | 1.0000 | Fake |
| 12 | front_crop_nano-banana-2-edit_sim83_001_k25tStd_p4_1-oldcam-v10.mp4 | 0.9993 | 0.9953 | 1.0000 | Fake |
| 13 | front_crop_nano-banana-2-edit_sim83_001_k25tStd_p4_1-oldcam-v9.mp4 | 0.9993 | 0.9926 | 1.0000 | Fake |
| 14 | front_crop_nano-banana-2-edit_sim83_001_k25tStd_p4_1-oldcam-v11.mp4 | 0.9997 | 0.9980 | 1.0000 | Fake |

## 2026-05-17 15:15 — v19 — `front_crop_nano-banana-2-edit_sim83_001_k25tStd_p4_1.mp4`

| Rank | Video | Frame mean | Frame min | Certainty | Verdict |
|------|-------|-----------|-----------|-----------|---------|
| 1 | front_crop_nano-banana-2-edit_sim83_001_k25tStd_p4_1-oldcam-v15.mp4 | 0.1605 🏆 | 0.0035 | 0.1510 | Neutral/Uncertain |
| 2 | front_crop_nano-banana-2-edit_sim83_001_k25tStd_p4_1-oldcam-v17.mp4 | 0.1660 | 0.0059 | 0.0879 | Neutral/Uncertain |
| 3 | front_crop_nano-banana-2-edit_sim83_001_k25tStd_p4_1-oldcam-v16.mp4 | 0.1884 | 0.0034 | 0.0566 | Neutral/Uncertain |
| 4 | front_crop_nano-banana-2-edit_sim83_001_k25tStd_p4_1-oldcam-v18.mp4 | 0.2046 | 0.0038 | 0.1146 | Neutral/Uncertain |
| 5 | front_crop_nano-banana-2-edit_sim83_001_k25tStd_p4_1-oldcam-v19.mp4 | 0.2485 | 0.0076 | 0.3047 | Likely fake |
| 6 | front_crop_nano-banana-2-edit_sim83_001_k25tStd_p4_1-oldcam-v12.mp4 | 0.6262 | 0.0610 | 0.9740 | Fake |
| 7 | front_crop_nano-banana-2-edit_sim83_001_k25tStd_p4_1-oldcam-v13.mp4 | 0.6597 | 0.0481 | 0.9844 | Fake |
| 8 | front_crop_nano-banana-2-edit_sim83_001_k25tStd_p4_1-oldcam-v8.mp4 | 0.8543 | 0.4395 | 1.0000 | Fake |
| 9 | front_crop_nano-banana-2-edit_sim83_001_k25tStd_p4_1-oldcam-v15-v1.mp4 | 0.9892 | 0.8990 | 1.0000 | Fake |
| 10 | front_crop_nano-banana-2-edit_sim83_001_k25tStd_p4_1.mp4 | 0.9936 | 0.9443 | 1.0000 | Fake |
| 11 | front_crop_nano-banana-2-edit_sim83_001_k25tStd_p4_1-oldcam-v14.mp4 | 0.9953 | 0.9639 | 1.0000 | Fake |
| 12 | front_crop_nano-banana-2-edit_sim83_001_k25tStd_p4_1-oldcam-v7.mp4 | 0.9983 | 0.9871 | 1.0000 | Fake |
| 13 | front_crop_nano-banana-2-edit_sim83_001_k25tStd_p4_1-oldcam-v10.mp4 | 0.9993 | 0.9953 | 1.0000 | Fake |
| 14 | front_crop_nano-banana-2-edit_sim83_001_k25tStd_p4_1-oldcam-v9.mp4 | 0.9993 | 0.9926 | 1.0000 | Fake |
| 15 | front_crop_nano-banana-2-edit_sim83_001_k25tStd_p4_1-oldcam-v11.mp4 | 0.9997 | 0.9980 | 1.0000 | Fake |

## 2026-05-17 15:32 — v20 — `front_crop_nano-banana-2-edit_sim83_001_k25tStd_p4_1.mp4`

| Rank | Video | Frame mean | Frame min | Certainty | Verdict |
|------|-------|-----------|-----------|-----------|---------|
| 1 | front_crop_nano-banana-2-edit_sim83_001_k25tStd_p4_1-oldcam-v20.mp4 | 0.0405 🏆 | 0.0016 | 0.7243 | Real |
| 2 | front_crop_nano-banana-2-edit_sim83_001_k25tStd_p4_1-oldcam-v15.mp4 | 0.1605 | 0.0035 | 0.1510 | Neutral/Uncertain |
| 3 | front_crop_nano-banana-2-edit_sim83_001_k25tStd_p4_1-oldcam-v17.mp4 | 0.1660 | 0.0059 | 0.0879 | Neutral/Uncertain |
| 4 | front_crop_nano-banana-2-edit_sim83_001_k25tStd_p4_1-oldcam-v16.mp4 | 0.1884 | 0.0034 | 0.0566 | Neutral/Uncertain |
| 5 | front_crop_nano-banana-2-edit_sim83_001_k25tStd_p4_1-oldcam-v18.mp4 | 0.2046 | 0.0038 | 0.1146 | Neutral/Uncertain |
| 6 | front_crop_nano-banana-2-edit_sim83_001_k25tStd_p4_1-oldcam-v19.mp4 | 0.2485 | 0.0076 | 0.3047 | Likely fake |
| 7 | front_crop_nano-banana-2-edit_sim83_001_k25tStd_p4_1-oldcam-v12.mp4 | 0.6262 | 0.0610 | 0.9740 | Fake |
| 8 | front_crop_nano-banana-2-edit_sim83_001_k25tStd_p4_1-oldcam-v13.mp4 | 0.6597 | 0.0481 | 0.9844 | Fake |
| 9 | front_crop_nano-banana-2-edit_sim83_001_k25tStd_p4_1-oldcam-v8.mp4 | 0.8543 | 0.4395 | 1.0000 | Fake |
| 10 | front_crop_nano-banana-2-edit_sim83_001_k25tStd_p4_1-oldcam-v15-v1.mp4 | 0.9892 | 0.8990 | 1.0000 | Fake |
| 11 | front_crop_nano-banana-2-edit_sim83_001_k25tStd_p4_1.mp4 | 0.9936 | 0.9443 | 1.0000 | Fake |
| 12 | front_crop_nano-banana-2-edit_sim83_001_k25tStd_p4_1-oldcam-v14.mp4 | 0.9953 | 0.9639 | 1.0000 | Fake |
| 13 | front_crop_nano-banana-2-edit_sim83_001_k25tStd_p4_1-oldcam-v7.mp4 | 0.9983 | 0.9871 | 1.0000 | Fake |
| 14 | front_crop_nano-banana-2-edit_sim83_001_k25tStd_p4_1-oldcam-v10.mp4 | 0.9993 | 0.9953 | 1.0000 | Fake |
| 15 | front_crop_nano-banana-2-edit_sim83_001_k25tStd_p4_1-oldcam-v9.mp4 | 0.9993 | 0.9926 | 1.0000 | Fake |
| 16 | front_crop_nano-banana-2-edit_sim83_001_k25tStd_p4_1-oldcam-v11.mp4 | 0.9997 | 0.9980 | 1.0000 | Fake |

## 2026-05-17 15:33 — v21 — `front_crop_nano-banana-2-edit_sim83_001_k25tStd_p4_1.mp4`

| Rank | Video | Frame mean | Frame min | Certainty | Verdict |
|------|-------|-----------|-----------|-----------|---------|
| 1 | front_crop_nano-banana-2-edit_sim83_001_k25tStd_p4_1-oldcam-v21.mp4 | 0.0249 🏆 | 0.0007 | 0.7296 | Real |
| 2 | front_crop_nano-banana-2-edit_sim83_001_k25tStd_p4_1-oldcam-v20.mp4 | 0.0405 | 0.0016 | 0.7243 | Real |
| 3 | front_crop_nano-banana-2-edit_sim83_001_k25tStd_p4_1-oldcam-v15.mp4 | 0.1605 | 0.0035 | 0.1510 | Neutral/Uncertain |
| 4 | front_crop_nano-banana-2-edit_sim83_001_k25tStd_p4_1-oldcam-v17.mp4 | 0.1660 | 0.0059 | 0.0879 | Neutral/Uncertain |
| 5 | front_crop_nano-banana-2-edit_sim83_001_k25tStd_p4_1-oldcam-v16.mp4 | 0.1884 | 0.0034 | 0.0566 | Neutral/Uncertain |
| 6 | front_crop_nano-banana-2-edit_sim83_001_k25tStd_p4_1-oldcam-v18.mp4 | 0.2046 | 0.0038 | 0.1146 | Neutral/Uncertain |
| 7 | front_crop_nano-banana-2-edit_sim83_001_k25tStd_p4_1-oldcam-v19.mp4 | 0.2485 | 0.0076 | 0.3047 | Likely fake |
| 8 | front_crop_nano-banana-2-edit_sim83_001_k25tStd_p4_1-oldcam-v12.mp4 | 0.6262 | 0.0610 | 0.9740 | Fake |
| 9 | front_crop_nano-banana-2-edit_sim83_001_k25tStd_p4_1-oldcam-v13.mp4 | 0.6597 | 0.0481 | 0.9844 | Fake |
| 10 | front_crop_nano-banana-2-edit_sim83_001_k25tStd_p4_1-oldcam-v8.mp4 | 0.8543 | 0.4395 | 1.0000 | Fake |
| 11 | front_crop_nano-banana-2-edit_sim83_001_k25tStd_p4_1-oldcam-v15-v1.mp4 | 0.9892 | 0.8990 | 1.0000 | Fake |
| 12 | front_crop_nano-banana-2-edit_sim83_001_k25tStd_p4_1.mp4 | 0.9936 | 0.9443 | 1.0000 | Fake |
| 13 | front_crop_nano-banana-2-edit_sim83_001_k25tStd_p4_1-oldcam-v14.mp4 | 0.9953 | 0.9639 | 1.0000 | Fake |
| 14 | front_crop_nano-banana-2-edit_sim83_001_k25tStd_p4_1-oldcam-v7.mp4 | 0.9983 | 0.9871 | 1.0000 | Fake |
| 15 | front_crop_nano-banana-2-edit_sim83_001_k25tStd_p4_1-oldcam-v10.mp4 | 0.9993 | 0.9953 | 1.0000 | Fake |
| 16 | front_crop_nano-banana-2-edit_sim83_001_k25tStd_p4_1-oldcam-v9.mp4 | 0.9993 | 0.9926 | 1.0000 | Fake |
| 17 | front_crop_nano-banana-2-edit_sim83_001_k25tStd_p4_1-oldcam-v11.mp4 | 0.9997 | 0.9980 | 1.0000 | Fake |
