# Claude / Anthropic Context (claude.md)

This file provides specific context for Claude.

### Project Origins
This project was initiated as an "Enterprise-Grade Local Face Similarity Application". The goal was to overcome the limitations of cloud-based KYC and facial similarity checks by building a robust, offline desktop application.

### Key Mathematical Decision (Cosine Distance Mapping)
ArcFace utilizes Cosine Distance. The threshold for ArcFace is officially `0.68`. Any distance <= 0.68 is considered the same person.

End-users requested an easy-to-understand 0-100% UI where 80% represents the
cutoff. `similarity_engine._score_from_distance` maps `distance 0.0 -> 0.68`
to `score 100% -> 80%` via a polynomial easing curve.

**v1.9 calibration (current):** `PASS_CURVE_EXPONENT = 0.5` (square root).
This was chosen so AI-generated selfies (which typically land at cosine
distance 0.05-0.15 because modern edit models preserve identity strongly)
read as 91-95% rather than 99-100%. The 99% pegged read in v1.8 (exponent
2.5) was indistinguishable from a degenerate fallback and obscured real
variance. Reference points listed in the source comment above the constant.

Do **not** replace the curve with a standard `(1 - distance) * 100`
formula, as that linearization fails mathematically accurate matches at
the 0.40-0.68 distance band (true matches scoring as 32-60% would be
discarded). Tune the exponent if calibration intent changes; do not
swap the formula.

### Directory Structure Requirements
- `main.py` serves as the sole entry point, passing control to `cli.py` or `gui.py`.
- Automated bash/batch scripts exist at the root level to circumvent python setup friction for non-technical users. 
