# Oldcam Version Breakdown

> Reference: Code-level breakdown of what each Oldcam version does, how they differ, and what makes each one distinct.
> Created: 2026-05-12

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

## Side-by-Side Comparison

| Feature | V7 | V8 | V9 | V10 | V11 | V12 |
|---|---|---|---|---|---|---|
| Face detection | None | None | MediaPipe 478 pts | MediaPipe 478 pts | MediaPipe 478 pts | MediaPipe 478 pts |
| Region masks | None | None | 4 regions | 4 regions | 4 regions | Detected, discarded |
| Rolling shutter | Sine arm-sway | Velocity-coupled to OIS | Soft residual | Soft residual | Soft residual | Soft residual |
| OIS model | None | Spring-damper ±2px | Spring-damper ±1.4px | Spring-damper ±1.4px | Spring-damper ±1.4px | Spring-damper ±1.4px |
| AF model | 12-frame hunt 1.5% | 2-frame hunt 0.5% | 6-frame breathing | 6-frame breathing | 6-frame breathing | None (removed) |
| JPEG pass | Yes (quality 94) | No | No | No | No | No |
| Noise type | 2D luma-only | 3D per-channel | Temporal luma+chroma | Temporal luma+chroma | Temporal luma+chroma | Temporal luma+chroma |
| AWB drift | No | Yes | Yes | No | Yes | Yes (luma-only) |
| FFT rPPG sync | No | No | No | Yes | Yes | No |
| Phase-locked oscillations | No | No | No | Yes | Yes | No |
| Global LUT applied | Yes | Yes | Yes | Yes | Yes | **No** |
| Dynamic tone mapping (CLAHE) | Yes | Yes | Yes | Yes | Yes | **No** |
| HSV saturation tweak | Yes | Yes | Yes | Yes | Yes | **No** |
| Output encoding | CRF 18 | baseline + 1500k cap | CRF 18 high | CRF 18 high | CRF 16 slow | CRF 16 slow |

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
| V12 | Pristine hardware-only | Removed rPPG, removed `cv2.LUT()` call, removed CLAHE tone mapping, removed HSV saturation tweak | None yet — V12 is the current optimization for anti-spoofing + color fidelity |

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
- **V12** — **Default ★**. Best for: KYC, liveness-detection, or any pipeline where the video will be analyzed by 3D-CNN anti-spoofing models. Preserves Kling's color fidelity better than V7–V11 since it removes the global LUT and CLAHE.
