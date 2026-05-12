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

## Side-by-Side Comparison

| Feature | V7 | V8 | V9 | V10 |
|---|---|---|---|---|
| Face detection | None | None | MediaPipe 478 pts | MediaPipe 478 pts |
| Region masks | None | None | 4 regions (forehead, cheeks, chin) | 4 regions |
| Rolling shutter | Sine arm-sway | Velocity-coupled to OIS | Soft residual | Soft residual |
| OIS model | None | Spring-damper ±2px | Spring-damper ±1.4px | Spring-damper ±1.4px |
| AF model | 12-frame hunt 1.5% | 2-frame hunt 0.5% | 6-frame breathing | 6-frame breathing |
| JPEG pass | Yes (quality 94) | No | No | No |
| Noise type | 2D luma-only | 3D per-channel | Temporal luma+chroma | Temporal luma+chroma |
| AWB drift | No | No | Yes | Yes |
| Background softening | No | No | Yes | Yes |
| FFT frequency sync | No | No | No | Yes |
| Phase-locked oscillations | No | No | No | Yes |
| Dynamic relighting | No | No | No | Yes |
| Output encoding | CRF 18 | baseline + 1500k cap | CRF 18 high | CRF 18 high |
| Lines of code | 536 | 590 | 829 | 939 |

---

## Version Selection Guide

- **V7** — Best for: general-purpose vintage look, clean grain, subtle imperfections. No MediaPipe dependency.
- **V8** — Best for: smartphone/social-media aesthetic, authentic motion compression. Bitrate cap is a feature, not a bug.
- **V9** — Best for: face-forward footage where you want subject/background separation and cinematic color drift. Requires MediaPipe.
- **V10** — Best for: portrait subjects with visible motion (talking, slight movement). The FFT sync and relighting reward good source material.
