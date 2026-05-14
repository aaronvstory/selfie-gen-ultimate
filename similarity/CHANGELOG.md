# Changelog

All notable changes to this project will be documented in this file.

## [Unreleased]
### Added
- **Hero Verdict Card** (standalone GUI): The similarity result is now the central visual element — a dedicated card between the two image zones shows a large status glyph (✓ / ✖), MATCH / NO MATCH headline, and the similarity score in big mono font, with a 0–100% bar marked at the 80% threshold so users can see how close to the cutoff a match landed.
- **Per-image FAS badges** (standalone GUI + CLI): Liveness verdicts now render as compact `✓ REAL · 99.7%` (green) / `✖ SPOOF · 99.99%` (red) badges directly under each image instead of a wide center text line, making it obvious at a glance which image scored what.
- **Brutalist palette** (standalone GUI): Replaced the default CTk dark-blue with a deliberate industrial grayscale palette (`similarity/src/theme.py`) — near-black surfaces, hairline borders, no rounded panels (only the hero card and interactive controls get small radii), mono numbers, sans body. Minimal and professional.
- **`is_real`-aware engine helpers**: `FaceEngine._side_real_confidence` and `_side_is_real` derive a single unambiguous "real-confidence on [0,1]" number from the per-face `(is_real, antispoof_score)` pair so renderers no longer need to know about the score-meaning flip.
- **Separated CLI Workflow Menus**: Interactive CLI now starts with dedicated `Similarity` and `Extraction` sections, each with its own submenu and explicit `Back` navigation.
- **Separated GUI Workflow Tabs**: GUI now provides dedicated `Similarity` and `Extraction` tabs with independent action controls and status messaging.

### Changed
- **GUI window 920×880 → 880×720**: Vertical real-estate is now better used by the 3-column layout; window fits 1080p screens with room for the taskbar.
- **Image preview max 250×250 → 220×220**: Frees width for the new center hero verdict column.
- **Anti-spoof checkbox is now disabled mid-run** (bot fix from coderabbit on PR #19): users can no longer flip the toggle while a comparison is running and end up with state that doesn't match the result.
- Updated user documentation to reflect sectioned CLI navigation and the tabbed GUI workflow model.

### Fixed
- **Critical: FAS score inversion** (`similarity_engine.py`, `similarity/src/{gui,cli}.py`): the DeepFace anti-spoofing engine returns `{is_real: bool, antispoof_score: float}` where the score's MEANING flips with the boolean (`is_real=True` → real confidence, `is_real=False` → spoof confidence). Renderers historically forwarded the score raw and displayed it as "% real" unconditionally, so a Driver's License flagged `is_real=False, antispoof_score=0.999997` ("99.9999% confident SPOOF") was rendered as "✓ Liveness: 99.9% real". Engine layer now folds the boolean into a derived `real_conf` so renderers consume one already-interpreted number.
- **Removed redundant center FAS line**: prior versions showed both a wide "Liveness (anti-spoof): possible synthetic on ref" text below the controls AND per-image readouts — confusing duplication. Per-image badges are now the single source of truth.

## [1.1.0] - 2026-04-10
### Added
- **Batch Processing CLI**: A major upgrade to the CLI mode allowing users to select a root directory and recursively scan subfolders.
- **Interactive Menu**: The CLI now runs an interactive `rich` menu instead of relying strictly on argparse flags.
- **Dynamic Image Settings**: CLI users can change the regex/keywords used to locate the two face images inside subdirectories (defaulting to "extracted" and "selfie").
- **Automated Folder Renaming**: Batch processing will automatically insert the rounded similarity score into the subfolder's name (e.g., `FAILED PERSONA - Morgan` -> `FAILED PERSONA 81 - Morgan`).
- **Comprehensive Documentation**: Added `README.md`, `CHANGELOG.md`, `agents.md`, and `claude.md`.

### Fixed
- Replaced the mathematical scoring formula in `src/engine.py` to map ArcFace's official 0.68 cosine distance threshold dynamically to an 80% score curve, resolving an issue where the app falsely outputted "59%" for matched photos.

## [1.0.0] - 2026-04-10
### Added
- Initial release.
- FaceEngine using DeepFace, RetinaFace, and ArcFace.
- Modern GUI built with `customtkinter`.
- Professional CLI wrapper using `rich`.
- Cross-platform launchers (`.bat` and `.command`) with automated virtual environment creation and pip dependency resolution.
