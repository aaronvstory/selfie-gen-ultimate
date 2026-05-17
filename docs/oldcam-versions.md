# Oldcam Version Breakdown

> Reference: Code-level breakdown of what each Oldcam version does, how they differ, and what makes each one distinct.
> Created: 2026-05-12 · Updated: 2026-05-17 (added V15 "Temporal Mute", now default; V14 demoted; V15 "Laundromat" hotfix: lossless FFV1 → double-lossy mp4v + CRF 23)

---

## Shared Foundation (All Versions)

Every version shares a common post-processing chain built on top of raw video frames:

| Effect | What it does |
|---|---|
| **Banding** | Adds horizontal luminance bands (∼±4 luma) simulating CCD scan-line inconsistency |
| **AE stepping** | Exposure walks a random ±1.5% per frame, mimicking slow auto-exposure drift |
| **Tone mapping** | S-curve applied to all channels to compress highlights and lift shadows |
| **Blooming** | Gaussian blur added back at 12% weight in bright regions — halation glow |
| **Chromatic aberration** | Red channel shifted +2px right, blue −2px left |
| **Ghosting** | 8% of previous frame blended in (motion smear) |
| **LUT** | 3D color LUT applied (warm/cool film response) |
| **Vignette** | Radial darkening toward corners |
| **FFmpeg H.264 encode** | All versions write `.mp4` via FFmpeg subprocess |

The differences between versions are entirely in what gets **added on top of** or **changed within** this foundation.

---

## V7 — "Modern Imperfection"

**File:** `oldcam-v7/oldcam.py` (536 lines)  
**Output encoding:** CRF 18 (high quality, no bitrate cap)

### What's unique

**Rolling shutter** — sine arm-sway model:
- Phase advances `0.05` radians per frame
- Shear coefficient: `0.0005–0.002` (random per clip)
- Each row is shifted horizontally by `sin(phase + row * freq) * shear * row`
- Produces a gentle lateral wobble, like handheld camera arm movement

**AF hunting** — 12-frame autofocus cycle:
- 1.5% chance per frame of triggering a focus hunt
- Sinusoidal blur radius rises then falls over 12 frames (sigma 0 → 1.8 → 0)
- Simulates a camera "searching" before locking focus

**JPEG pass** — quality 94:
- Each frame is JPEG-encoded in-memory at quality 94, then decoded back
- Introduces gentle DCT compression artifacts (block boundaries, luma smear)
- The only version that does this

**Noise model** — 2D luma-only:
- Random field at half resolution `(h//2, w//2)`, upsampled to full
- Applied to luma channel only (Y in YCrCb)
- Clean, film-grain character

### Character
V7 feels like a modern phone with mild imperfections — slight focus drift, subtle wobble, clean grain. The JPEG pass keeps it feeling "processed" rather than raw.

---

## V8 — "Temporal Smartphone"

**File:** `oldcam-v8/oldcam.py` (590 lines)  
**Output encoding:** `baseline` profile + `1500k maxrate 2000k` (intentional compression artifacts)

### What's unique

**OIS jitter** — spring-damper physics model:
- Simulates optical image stabilization fighting real hand tremor
- State: `(ois_x, ois_y, ois_vx, ois_vy)` — position + velocity
- Damping `0.72`, restoring force `0.10`, max travel `±2px`
- When hitting the wall, velocity inverted with `35%` energy loss
- Each frame the OIS position is applied as a sub-pixel frame translation

**Rolling shutter** — velocity-coupled to OIS:
- `shear = rs_velocity + ois_vx * 0.00055 + sign(ois_vx) * ois_speed * 0.00018`
- Rolling shutter wobble is directly coupled to how fast the OIS is moving
- Fast stabilization movements produce proportionally more line skew

**3D chroma noise** — per-channel:
- Noise field at `(h//4, w//4, 3)` — one independent noise plane per channel
- This adds color noise (not just luma grain), giving it a sensor-noise character
- Upsampled to full resolution before adding

**AF** — short 2-frame hunt, 0.5% chance per frame (shorter and rarer than V7)

**No JPEG pass** — V8 relies on the encoder's `baseline` profile to create compression artifacts instead of in-process JPEG encoding

**Output encoding** — deliberate bitrate cap:
- `baseline` H.264 profile (no CABAC, no B-frames) + `1500k maxrate 2000k`
- This is intentional: the goal is compression-artifact aesthetic on motion
- The bitrate cap crushes fine detail during movement exactly like an old phone

### Character
V8 feels like a mid-2010s Android phone — the OIS makes it feel stabilized but not perfectly steady. The bitrate cap creates authentic motion blur during fast movement. Most "smartphone" of the four.

---

## V9 — "Dynamic Mesh"

**File:** `oldcam-v9/oldcam.py` (829 lines)  
**Output encoding:** CRF 18 + `profile:v high`  
**Requires:** `mediapipe==0.10.35`, `face_landmarker.task` model

### What's unique

**MediaPipe FaceLandmarker integration:**
- Detects 478 face landmarks per frame using the Tasks FaceLandmarker API
- `REGION_INDICES` maps 4 face regions to specific landmark indices:
  - `forehead`: 23 landmark points
  - `left_cheek`: 22 points
  - `right_cheek`: 22 points
  - `chin`: 22 points

**Per-region dynamic masks** (`get_dynamic_region_masks`):
- For each region: extract landmark pixel coords → compute convex hull → draw filled polygon → Gaussian blur the mask
- **Temporal smoothing**: `mask = prev_mask * 0.65 + new_mask * 0.35` (inertia prevents flicker)
- **5-frame persistence**: if FaceLandmarker misses a frame, the last known masks stay active for up to 5 frames
- **Ellipse fallback**: if no face ever detected, falls back to a generic center-frame ellipse

**AWB color drift** (`apply_global_awb_drift`):
- `drift += random.normal(0, 0.05)` each frame, clamped `±2.0`
- Applied as: `red_channel += drift * 0.35`, `green_channel += drift * 0.15`
- Simulates auto white balance wandering on a warm/cool scene

**Background texture softening** (`apply_soft_background_texture`):
- Outside the face mask region: downsample 50% → upsample back → blend 18% into original
- Gives background a slight soft-focus / CCD-smoothed look

**Temporal noise field:**
- `field = previous_field * 0.85 + fresh_noise * 0.15`
- Separate luma and chroma noise fields, both temporally correlated
- Noise is persistent frame-to-frame rather than fresh each frame

**Softer camera movement (vs V8):**
- OIS: damping `0.82` (vs 0.72), max travel `±1.4px` (vs ±2px) — more stable
- Rolling shutter: sigma `0.00006` (much softer), clamp `±0.0018` — subtle line wobble

**AF breathing pulse:**
- 6-frame sinusoidal breathing cycle (sigma 0.45 → 0.95 → 0.45)
- Slower and more rhythmic than V7/V8's random hunt

### Character
V9 is where face-awareness enters. The face regions have different tonal properties than the background, AWB drift gives a cinematic color wandering feel, and the soft background blur makes subjects "pop." This is the closest to a professional-but-vintage cinema camera.

---

## V10 — "Spatial Sync"

**File:** `oldcam-v10/oldcam.py` (939 lines)  
**Output encoding:** CRF 18 + `profile:v high`  
**Requires:** `mediapipe==0.10.35`, `face_landmarker.task` model

### What's unique (everything V9 has, plus:)

**FFT-based base frequency detection** (`synchronize_base_frequency`):
- Tracks mean green-channel luminance within the detected face region, every frame
- Accumulates a rolling 3-second history buffer
- Every 30 frames, runs `np.fft.rfft` on the history → finds peak frequency in `0.7–4.0 Hz` range
- This is the "heartbeat / micromovement" frequency of the subject's face
- Defaults to `1.2 Hz` if no strong peak found or clip too short

**Phase-locked per-region oscillations** (`apply_synchronized_spatial_fluctuation`):
- Each region gets its own spatial phase offset:
  ```python
  SPATIAL_PHASE_OFFSETS = {
      "forehead": 0.0,
      "left_cheek": 0.15,
      "right_cheek": 0.15,
      "chin": 0.25
  }
  ```
- Per frame: `phase = t * 2π * target_hz + offset`
- `shift = sin(phase) * envelope * 2.0`
- Applied to color channels weighted by a **warm skin tone mask** (YCrCb range: Cr 133–173, Cb 77–127):
  - Green: `+1.0 * shift`
  - Red: `+0.45 * shift`
  - Blue: `-0.10 * shift`
- Result: the face's color subtly pulses at its own detected frequency, with forehead leading and chin slightly lagging — like subsurface vascular light variation

**Dynamic relighting** (`apply_dynamic_relighting`):
- `light_shift = (x_grid * -ois_x + y_grid * -ois_y) * 1.5`
- As the camera moves (OIS position), a synthetic light source appears to shift in the opposite direction
- Applied only in highlight regions: weight = `(blur_frame / 255)^2` — only bright areas rerelight
- Simulates a nearby reflective surface (window, lamp) creating directional bounce as the camera moves

**Graceful degradation:**
- If the clip is too short for FFT analysis, V10 falls back to V9 behavior (no frequency sync)
- Short clips still get all V9 effects; only the Spatial Sync layer is skipped

### Character
V10 is the most technically ambitious. The FFT sync means the color oscillations are tied to the actual content of the video — a face that's slightly moving will have its detected movement frequency reflected back in the color pulsing. Combined with the motion-coupled relighting, it produces a "the camera is alive to this person" feeling that's hard to articulate but visible.

---

## V11 — "Spatial Sync + AWB Drift" (2026-05-12)

V11 is the synthesis of V9's hardware realism and V10's biological realism. V10 had removed AWB drift because global channel shifts contaminate the green channel history buffer that the FFT reads to detect the 0.8–1.8 Hz heartbeat/breathing frequency. V11 solves this with strict execution ordering.

### The Signal Integrity Problem

V10's FFT analysis reads the **mean green channel** of the face region across the last 3 seconds of frames to detect the dominant low-frequency fluctuation. If AWB drift (which modifies global channel intensity) runs before this read, the FFT interprets camera hardware noise as a biological signal — frequency lock breaks down.

### The V11 Solution: Operation Order

```text
1. get_dynamic_region_masks()                  — face detection (per-frame; the every-other-frame skip from v10 was removed in v1.5 to fix motion stutter)
2. synchronize_base_frequency()                — FFT reads CLEAN green channel, no drift yet
3. apply_synchronized_spatial_fluctuation()    — biological pulse baked into pixels
4. apply_global_awb_drift()                    — hardware color drift applied AFTER FFT
5. apply_soft_ois_jitter()
6. apply_soft_rolling_shutter()
7. apply_subtle_af_breathing()
8. apply_ae_stepping()
9. apply_dynamic_tone_mapping()
10. apply_highlight_blooming()
11. HSV saturation / LUT
12. apply_modern_sensor_noise()
13. apply_radial_chromatic_aberration()
14. vignette
```

Steps 1–3 read and write `g_history` (the green channel buffer) before any AWB channel modification. Step 4 then applies camera-hardware white-balance wandering on top of the biological signal already baked in. The two systems are mathematically independent.

### What V11 Adds vs V10

- **`apply_global_awb_drift()`** is called again (V10 had the function defined but not called). Drift magnitude: ±2.0, step sigma 0.05 per frame. Red channel: +drift×0.35, Green: +drift×0.15.

### What V11 Preserves from V10

All V10 improvements are unchanged:
- ~~Every-other-frame face detection~~ (removed in v1.5: caused motion stutter; MediaPipe now runs 1:1 on every frame)
- Pre-computed vignette mask (no per-frame recalculation)
- `tight_mask = focus_mask * focus_mask` (squared boundary, eliminates bleed onto face)
- FFT range narrowed to [0.8, 1.8] Hz (realistic heartbeat band)
- `shift_intensity` amplitude at 0.45 (eliminated siren-like strobing)
- `apply_soft_background_texture` commented out (flat-sensor webcam model)
- `apply_dynamic_relighting` commented out (flat focal plane, no depth separation)

### Character

V11 is the "best of all worlds" version. V9 simulates a physical camera sensor adjusting to ambient light. V10 simulates human skin microcirculation. V11 does both by calculating the biological signal before applying global hardware color shifts. The AWB drift adds a subtle warmth/coolness oscillation that's independent of (and layered on top of) the face-region biological pulsing — the result is a video that reads as both "phone footage" and "living person" simultaneously.

---

## V12 — "Pristine Hardware-Only" (2026-05-13)

V12 is a deliberate step back from V11's stack. It assumes the video will be evaluated by modern Presentation Attack Detection (PAD) systems and wants to minimize anything those systems classify as a "synthetic liveness signal" — while also restoring color fidelity that V7–V11 were degrading.

### The Anti-Spoofing Problem

Modern liveness detectors (KYC, face-unlock, fintech onboarding) use 3D-CNN architectures that track how blood propagates across the **geometry** of the face over time, not just whether a color channel oscillates. A 2D mask of green-channel oscillation applied uniformly across face regions (V10/V11's rPPG) lacks sub-surface scattering and the spatial propagation pattern of real tissue. Detectors flag this as synthetic.

### The Color Degradation Problem

Two effects in V7–V11 were globally manipulating color and contrast in ways that didn't survive scrutiny:

- **`create_neutral_phone_lut()` + `cv2.LUT()`** — a 1D LUT with `red_curve[i] = min(255, i * 1.05 + 5)` and lifted blacks. Applied globally, this pushes the entire frame warm → a sepia tint that betrays the synthetic origin.
- **`apply_dynamic_tone_mapping()`** — uses CLAHE (Contrast Limited Adaptive Histogram Equalization) which mathematically forces high local contrast, crushing the subtle shadow/highlight detail that Kling renders well.

### What V12 Removes

- `synchronize_base_frequency()` and `apply_synchronized_spatial_fluctuation()` — rPPG entirely
- `cv2.LUT()` call and `create_neutral_phone_lut()` application from `process_frame()`
- `apply_dynamic_tone_mapping()` — no CLAHE
- HSV saturation tweak — preserves Kling's source color science

### What V12 Keeps

The "hardware emulation" stack only:
- `apply_soft_ois_jitter()` — spring-damper hand stabilization residual
- `apply_soft_rolling_shutter()` — CMOS scan-line warp
- `apply_ae_stepping()` — auto-exposure walks
- `apply_highlight_blooming()` — sensor halation in bright regions
- `apply_global_awb_drift()` — white-balance hunting (luma drift, no biased channels)
- `apply_modern_sensor_noise()` — fixed-pattern + temporal noise
- `apply_radial_chromatic_aberration()` — lens fringing
- Vignette

Face detection still runs (`get_dynamic_region_masks()`) so the FPN mask, vignette mask, and other state survive — but the region masks are discarded since no rPPG layer consumes them.

### Character

V12 looks like a raw sensor feed with imperfect optics, not a color-graded clip. The output preserves Kling's source contrast and color fidelity, while still gaining the physical-camera fingerprint (OIS micro-movement, sensor noise, rolling shutter). It's the right pick when the downstream consumer is a liveness model rather than a human viewer.

---

## V13 — "High-End Daylight" (2026-05-14)

**File:** `oldcam-v13/oldcam.py` (~830 lines, cloned from V12)
**Output encoding:** CRF 12 / preset slow / profile high (near-lossless H.264)

### The Sensor Noise Problem

V12 kept `apply_modern_sensor_noise()` — FPN (fixed pattern noise) baked into the sensor plus a temporal grain pass per frame. In stable bright daylight, a flagship phone's ISP cleans those signals away before the encoded frame ever reaches you. So injecting grain into Kling's already-pristine daylight footage was *adding* a synthetic-looking signal, not removing one.

V13's premise: when the source already looks like flagship-daylight output, don't degrade it. Simulate only the hardware artifacts a real CMOS *can't* fully hide — the geometric and optical fingerprints.

### What V13 Removes (vs V12)

- **`apply_modern_sensor_noise`** — no FPN, no temporal noise, no grain (largest perf win)
- **`apply_ae_stepping`** — no auto-exposure walk; V13 assumes stable daylight
- **Ghosting blend forced to `0.0`** — razor-sharp frames, no inter-frame smear
- **`--grain` CLI arg** — dead code since the noise call is gone

### What V13 Keeps

- `apply_soft_ois_jitter` — sub-pixel hand motion residual
- `apply_soft_rolling_shutter` — CMOS scan-line warp
- `apply_highlight_blooming(threshold=232, strength=0.055)` — photons scattering through glass
- `apply_global_awb_drift` — AWB algorithm recalculating in microscale
- `apply_radial_chromatic_aberration(scale=0.0006)` — corner fringing
- Vignette

### Why V13 Renders Faster

`apply_modern_sensor_noise` was the heaviest per-frame pass — it generated a temporal noise field, blended it with the FPN mask, and clipped per channel. Removing it shaves a measurable chunk off render time, especially on long clips. V13 is the fastest version that produces a release-quality output.

### Character

V13 looks like a high-end smartphone's daylight output: razor-sharp, no visible grain, no exposure hunting, no temporal smear. To a human viewer it looks pristine. To a KYC anti-spoofing algorithm reading raw data, it still carries the geometric (OIS sub-pixel jitter, rolling shutter scan-warp) and optical (blooming, chromatic aberration, vignette) signatures of a real physical device.

---

## V24 — "Crush Laundromat" (2026-05-17) — **Default ★**

**File:** `oldcam-v24/oldcam.py` (cloned from V15 + one destructive change)
**Output encoding:** uniform resolution round-trip (×0.40 downscale → Lanczos upscale + light unsharp) per frame, then V15's lossy **mp4v** temp → H.264 CRF 23, original audio stream-copied

> App version numbers jump V15 → V24. V16–V23 were **rejected
> oldcam-testing bench experiments**, never app versions. Full A/B
> history and the conclusive "destructive wins / additive loses" rule:
> `oldcam-testing/SCOREBOARD.md`; future-work guide:
> `oldcam-testing/RESUME.md`.

V24 is the bench winner. The Resemble deepfake-API sweep established:
the detector scores Kling's residual diffusion fingerprint —
*destroying* high-frequency information **uniformly** lowers the score;
*adding* any synthetic signal (warp/blur/grain) raises it. V15's
"Laundromat" was already destructive (frame_mean 0.16). V24 destroys
more, the same way on every frame, and adds nothing:

- **Resolution round-trip** before the V15 encode: each frame
  downscaled ×0.40 (`cv2.INTER_AREA`) then upscaled back
  (`cv2.INTER_LANCZOS4`). Annihilates the AI fingerprint's
  high-frequency band, identically every frame, no added signal.
- **Light unsharp-mask** restores *perceived* crispness — it amplifies
  the real structure that survived; it cannot reinvent the destroyed
  fingerprint. Real phone ISPs sharpen post-readout, so this is
  physically faithful.

Bench result on the reference clip: **frame_mean 0.018, ~9× better than
V15's 0.16**, while staying visually sharp. On a fully-saturated source
(where V15 couldn't move the score off 1.0000 at all) V24 still pulled
it to 0.45 — it improves clips V15 was useless on.

`process_frame` is V15's with the round-trip as the only change. Two
review-bot safety fixes vs the bench file: `--output` == input is
refused (no in-place source overwrite); the ffmpeg encode timeout is
configurable (`--ffmpeg-timeout`, default 600). Windows + macOS twins
byte-identical; Rule 9/10-compliant launchers.

---

## V15 — "Temporal Mute" (2026-05-17) — superseded as default by V24

**File:** `oldcam-v15/oldcam.py` (~1000 lines, cloned from V14)
**Output encoding:** lossy **mp4v** temp → single H.264 final at CRF 23 (clamped 10–28), original audio stream-copied

V15 is a synthesis profile, driven by **Resemble deepfake-API benchmarking** rather than a forensic-math review. The benchmark verdict: V12 and V13 vastly outperformed every other version. V12's temporal blending (ghosting) hid the AI's frame-to-frame flicker; V13's total absence of synthetic noise avoided spatial/frequency detectors. **V14 regressed** — its sub-perceptual sensor floor, *perfectly preserved* by the lossless FFV1 intermediate, is itself a stationary signal a frequency detector locks onto. V15 keeps V14's corrected per-frame optics but takes the two winning traits and discards the ones that backfired (sensor floor *and* lossless preservation).

### Hotfix — "Laundromat" double-lossy encoding (2026-05-17)

The first Resemble run of V15 scored poorly. Root cause: the **lossless FFV1 temp was a trap**. Losslessly preserving the frames also perfectly preserves Kling's high-frequency latent-diffusion artifacts, and makes the 0.18 temporal ghosting read as a mathematical opacity overlay rather than natural motion blur. The fix reverts to V13's **"double-lossy"** chain: OpenCV `mp4v` temp followed by a heavier FFmpeg H.264 encode (`--crf` default **14 → 23**, clamp ceiling **24 → 28**). The crude double compression acts as a digital washing machine — it organically crushes the AI artifacts and bakes the ghosting into the H.264 macro-blocks, so a frequency-domain detector reads natural compression blocks ("real") instead of pristine math ("fake"). Per-frame optics (`process_frame`) and the 0.18 ghosting are **unchanged**.

### What V15 Keeps from V14 (verbatim)

- **True multiplicative AWB color-temperature drift** (`apply_global_awb_drift`).
- **Smoothstep highlight bloom** (`threshold=232, strength=0.055`).
- **Original audio stream-copied** (no highpass/lowpass/compressor).
- **`np.rint()` casts** (no truncation darkening) + **cached vignette mask**.

### What the Hotfix Reverts from V14

- **Lossless FFV1 MKV temp + MJPG/mp4v fallback loop → pure lossy `mp4v` temp** (`.tmp_noaudio.mp4`). The FFV1/MJPG fallback chain is gone; the deliberate mp4v → H.264 double-lossy pass is the point. `--crf` default raised 14 → 23 (clamp widened 10–24 → 10–28) for WhatsApp/Telegram-grade web compression.

### What V15 Restores from V12

- **`--ghosting` is a real knob again** — default **0.18**, validated by `bounded_ghosting` (clamps 0.0–0.5). V13 and V14 hardcoded ghosting to `0.0` for razor-sharp frames; Resemble testing showed that frame-to-frame perfection is itself an AI tell. `naturalize_video` now passes `args.ghosting` to `blend_with_previous_frame`, bleeding 18% of the previous frame to defeat temporal-consistency detectors.

### What V15 Removes from V14

- **`apply_daylight_sensor_floor()` deleted entirely**, along with its `--read-noise` / `--shot-noise` / `--chroma-noise-ratio` CLI args and their `main()` clamps. The preserved floor was V14's frequency-detector tell. V15 applies **no sensor noise of any kind** (V13's noise-free philosophy restored).

### Pipeline Order (`process_frame`)

```text
1. apply_soft_ois_jitter            — physical hand-motion residual
2. apply_soft_rolling_shutter       — CMOS scan-line warp
3. apply_highlight_blooming         — smoothstep bloom (kept from V14)
4. apply_radial_chromatic_aberration — lens fringing
5. apply_global_awb_drift           — true multiplicative AWB drift (kept from V14)
6. vignette                         — lens falloff (mask cached, np.rint)
   (NO sensor floor — deleted)
— then, in naturalize_video, per-frame temporal blend at args.ghosting (0.18)
```

No face detection, no AE stepping, no rPPG. The only behavioural difference from V14 inside `process_frame` is the *absence* of the final sensor-floor pass; the temporal blend is applied one level up, in `naturalize_video`.

### Character

V15 looks like V13/V14 to a human but reads differently to a detector: there is no stationary noise floor for a frequency analyzer to lock onto, the 18% previous-frame bleed smears the AI's tell-tale per-frame consistency without being visible as motion blur on real footage, and the double-lossy mp4v → H.264 (CRF 23) pass crushes Kling's diffusion artifacts into ordinary web-compression macro-blocks. It is the version tuned to what the Resemble API actually rewards.

---

## V14 — "Forensic Daylight" (2026-05-16)

**File:** `oldcam-v14/oldcam.py` (~1060 lines, cloned from V13)
**Output encoding:** lossless **FFV1** MKV temp → single H.264 final at CRF 14 (clamped 10–24), original audio stream-copied

V14 is a physics-corrected successor to V13. A forensic review found V13's optics were mathematically wrong in several places — wrong enough that a PAD/SNR detector reading the raw signal could flag them. V14 fixes the math while keeping V13's design philosophy unchanged: **no rPPG, no face tracking, no biological-liveness signals — camera optics and sensor physics only.**

### The Problems V14 Fixes (vs V13)

**1. AWB was exposure drift, not white balance.** V13's `apply_global_awb_drift` did `image_f += drift` — a flat scalar added to all BGR channels. That is a luma/exposure shift, not a color-temperature change. A real AWB algorithm moves the red/blue channel *gains* inversely while green stays anchored.

**2. Synthetic pixel stasis.** V13's pixels were perfectly static between OIS micro-jitters. Real sensors never produce two identical frames — the absence of any read/shot noise floor is a forensic dead-giveaway for SNR/PAD detectors.

**3. Double-lossy encode.** V13 wrote the temp video with OpenCV `mp4v` (lossy) then re-encoded to H.264 — destroying the sub-perceptual effects before FFmpeg ever saw them.

**4. Flickering highlight bloom.** V13's binary `cv2.threshold` bloom mask shimmered as highlights crossed the boundary frame-to-frame.

**5. Truncation darkening.** V13's `astype(np.uint8)` truncated instead of rounding, biasing the whole clip progressively darker.

**6. Pointless audio mangling.** V13 ran `highpass/lowpass/volume/acompressor` on the audio for no camera-realism reason.

**7. V13 vignette-cache bug.** V13 recomputed the adjusted vignette mask every frame but never stored it in `state`.

### What V14 Adds / Changes

- **`apply_global_awb_drift` rewritten as true multiplicative color-temperature drift** — Red and Blue gains move inversely (warm↔cool) around a green anchor, driven by a mean-reverting stochastic walk (not a perfect sine, which would itself be a periodic tell).
- **`apply_daylight_sensor_floor()` — new.** A sub-perceptual, luma-dominant, signal-dependent read+shot noise floor (max ≤ ~2/255, mean < 0.2/255). Invisible to humans, survives H.264 without shattering, and breaks the artificial cleanliness. Runs **last** in `process_frame` so bloom/CA/vignette don't smooth it away.
- **Lossless FFV1 MKV temp** (graceful **MJPG → mp4v** fallback for limited OpenCV builds) so the sensor floor and AWB survive to the single final H.264 encode.
- **Smoothstep bloom ramp** — `apply_highlight_blooming(threshold=232, strength=0.055)` ramps continuously from threshold to white (no binary-edge flicker).
- **`np.rint()` before every uint8 cast** — eliminates truncation darkening bias.
- **Audio stream-copied** verbatim.
- **Vignette mask cached in `state`** (V13 bug fixed, carried into V14).
- **New CLI knobs:** `--read-noise` (0.22), `--shot-noise` (0.16), `--chroma-noise-ratio` (0.08), `--crf` (14, clamped 10–24).

### Pipeline Order (`process_frame`)

```text
1. apply_soft_ois_jitter            — physical hand-motion residual
2. apply_soft_rolling_shutter       — CMOS scan-line warp
3. apply_highlight_blooming         — smoothstep bloom (no flicker)
4. apply_radial_chromatic_aberration — lens fringing (kept verbatim from V13)
5. apply_global_awb_drift           — TRUE multiplicative AWB temperature drift
6. vignette                         — lens falloff (mask cached, np.rint)
7. apply_daylight_sensor_floor      — sub-perceptual sensor noise floor, LAST
```

No face detection, no AE stepping, no ghosting, no rPPG — same red-team-physics-only stance as V13, but with the optics done correctly.

### Character

V14 looks identical to V13 to a human viewer — pristine flagship daylight. The difference is entirely in the raw signal a forensic detector reads: V14's AWB wanders as a real color-temperature drift, the sensor floor gives every frame the micro-variation a real CMOS produces, and nothing is destroyed by a lossy intermediate encode. It is the "looks the same, measures correctly" version.

---

## Side-by-Side Comparison

| Feature | V7 | V8 | V9 | V10 | V11 | V12 | V13 | V14 | V15 |
|---|---|---|---|---|---|---|---|---|---|
| Face detection | None | None | MediaPipe 478 pts | MediaPipe 478 pts | MediaPipe 478 pts | None (lazy import only) | None | None | **None** |
| Region masks | None | None | 4 regions | 4 regions | 4 regions | Discarded | Not computed | Not computed | **Not computed** |
| Rolling shutter | Sine arm-sway | Velocity-coupled to OIS | Soft residual | Soft residual | Soft residual | Soft residual | Soft residual | Soft residual | Soft residual |
| OIS model | None | Spring-damper ±2px | Spring-damper ±1.4px | Spring-damper ±1.4px | Spring-damper ±1.4px | Spring-damper ±1.4px | Spring-damper ±1.4px | Spring-damper ±1.4px | Spring-damper ±1.4px |
| AF model | 12-frame hunt 1.5% | 2-frame hunt 0.5% | 6-frame breathing | 6-frame breathing | 6-frame breathing | None (removed) | None | None | None |
| AE stepping | Yes | Yes | Yes | Yes | Yes | Yes | No | No | **No** |
| JPEG pass | Yes (quality 94) | No | No | No | No | No | No | No | No |
| Sensor noise | 2D luma-only | 3D per-channel | Temporal luma+chroma | Temporal luma+chroma | Temporal luma+chroma | Temporal luma+chroma | None | Sub-perceptual floor | **None** |
| Ghosting blend | `--ghosting` arg | `--ghosting` arg | `--ghosting` arg | `--ghosting` arg | `--ghosting` arg | `--ghosting` arg | Forced 0.0 | Forced 0.0 | **`--ghosting` 0.18** |
| AWB drift | No | Yes | Yes | No | Yes | Yes (luma-only) | Yes (luma-only ✗) | Yes (true multiplicative) | **Yes (true multiplicative)** |
| FFT rPPG sync | No | No | No | Yes | Yes | No | No | No | No |
| Phase-locked oscillations | No | No | No | Yes | Yes | No | No | No | No |
| Global LUT applied | Yes | Yes | Yes | Yes | Yes | **No** | No | No | No |
| Dynamic tone mapping (CLAHE) | Yes | Yes | Yes | Yes | Yes | **No** | No | No | No |
| HSV saturation tweak | Yes | Yes | Yes | Yes | Yes | **No** | No | No | No |
| Highlight bloom | Binary threshold | Binary threshold | Binary threshold | Binary threshold | Binary threshold | Binary threshold | Binary threshold | Smoothstep ramp | **Smoothstep ramp** |
| uint8 cast | Truncate | Truncate | Truncate | Truncate | Truncate | Truncate | Truncate | np.rint() | **np.rint()** |
| Temp encode | mp4v (lossy) | mp4v (lossy) | mp4v (lossy) | mp4v (lossy) | mp4v (lossy) | mp4v (lossy) | mp4v (lossy ✗) | FFV1 lossless | **mp4v (lossy ★)** |
| Audio | Filtered | Filtered | Filtered | Filtered | Filtered | Filtered | Filtered ✗ | Stream-copied | **Stream-copied** |
| Output encoding | CRF 12 slow | baseline + 1500k cap | CRF 12 slow | CRF 12 slow | CRF 12 slow | CRF 12 slow | CRF 12 slow | CRF 14 slow (clamp 10–24) | **CRF 23 slow (clamp 10–28)** |

---

## Version History — Theme & Trade-Off

Each version had a guiding theme and a limitation that motivated the next one. This is the why behind the chain, not just the what:

| Version | Theme | Key Additions | Trade-Off That Drove the Next Version |
|---|---|---|---|
| V7 | Modern phone imperfection | JPEG quality-94 cycle, sine arm-sway rolling shutter, AF hunting | Too subtle — output looked nearly identical to the source frame |
| V8 | Hardware physics upgrade | Spring-damper OIS, 3D per-channel sensor noise, AWB drift, hard bitrate cap (1500k baseline) | Bitrate cap over-compressed the final file, losing detail |
| V9 | Face-aware portrait pass | MediaPipe FaceLandmarker (478 pts), 4-region masks, AWB color drift, soft background blur (later disabled) | Background softening read as "computational photography depth-of-field," not raw webcam |
| V10 | rPPG biological sync | FFT on green-channel face mean, phase-locked spatial oscillation in 4 face regions, dynamic relighting | Visible color "siren" on the face; needed to remove AWB drift so FFT could read clean signal |
| V11 | Best-of-all combination | Re-enabled `apply_global_awb_drift()` AFTER FFT read; relighting kept disabled | Modern PAD detectors flag the 2D rPPG color pulse; global LUT crushes contrast and tints the frame sepia |
| V12 | Pristine hardware-only | Removed rPPG, removed `cv2.LUT()` call, removed CLAHE tone mapping, removed HSV saturation tweak | Sensor noise and AE stepping still applied per-frame — degradation signals flagship daylight footage doesn't carry |
| V13 | High-end daylight | Removed `apply_modern_sensor_noise`, removed `apply_ae_stepping`, hardcoded ghosting to 0.0, dropped `--grain` CLI arg | Optics were mathematically wrong: AWB was exposure drift not white balance; perfectly-static pixels; double-lossy mp4v→H.264; flickering binary bloom; truncation darkening |
| V14 | Forensic daylight (physics-corrected) | True multiplicative AWB temperature drift, `apply_daylight_sensor_floor` (sub-perceptual), lossless FFV1 temp, smoothstep bloom, `np.rint` casts, audio stream-copy, vignette-cache fix | Resemble API showed the preserved sensor floor is itself a stationary frequency-detector tell; V12/V13 (ghosting + zero noise) outperformed it |
| V15 | Temporal Mute (Resemble-tuned synthesis) | Kept V14 AWB/bloom/audio/np.rint; **removed** `apply_daylight_sensor_floor` + its CLI knobs; **restored** `--ghosting` 0.18 (V12 temporal blend); **hotfix:** reverted lossless FFV1 → double-lossy mp4v + CRF 23 (Resemble flagged lossless preservation as the new tell) | None yet — current default |

Notes on accuracy of this table vs the codebase:
- "Digital over-sharpening" was a `--sharpen` CLI parameter present in every version (default 0.8), not a V11 addition. The actual destructive effects in V11 are the global LUT and CLAHE — both inherited from earlier versions, exposed by V11's higher fidelity.
- V9's background blur logic is present (`oldcam-v9/oldcam.py:342` GaussianBlur) but gated by face mask; the "fake depth-of-field" critique applies when the mask boundary becomes visible.
- V10's dynamic relighting was added then disabled in the same release (commented out in `process_frame`); it never shipped enabled.

---

## Version Selection Guide

- **V7** — Best for: general-purpose vintage look, clean grain, subtle imperfections. No MediaPipe dependency.
- **V8** — Best for: smartphone/social-media aesthetic, authentic motion compression. Bitrate cap is a feature, not a bug.
- **V9** — Best for: face-forward footage where you want subject/background separation and cinematic color drift. Requires MediaPipe.
- **V10** — Best for: portrait subjects where biological realism is the priority; AWB drift removed to preserve FFT signal integrity.
- **V11** — Best for: human viewers who value the "phone footage of a living person" feel.
- **V12** — Best for: low-light realism, KYC pipelines that expect visible sensor noise, scenes where AE walk reads as authentic. Preserves Kling's color fidelity better than V7–V11.
- **V13** — Best for: bright daylight footage where you want V13's exact look and don't need the physics corrections. Superseded — selectable but no longer pre-selected.
- **V14** — Best for: cases where you specifically want the sub-perceptual sensor floor (it defeats *naive* SNR-stasis checks). Superseded by V15 after Resemble benchmarking — the preserved floor turned out to be a frequency-detector tell. Selectable but no longer pre-selected.
- **V15** — **Default ★**. Best for: footage scored by the Resemble deepfake API (the benchmark it was tuned against) and consistency/frequency detectors generally. Keeps V14's correct per-frame optics, drops the sensor floor entirely, restores V12's `--ghosting` (0.18) so per-frame AI consistency is smeared without visible motion blur, and (Laundromat hotfix) runs a deliberate double-lossy mp4v → H.264 CRF 23 pass so Kling's diffusion artifacts are crushed into ordinary web-compression blocks. The empirically-best version on the metric that matters. Same MediaPipe-free dependency story.
