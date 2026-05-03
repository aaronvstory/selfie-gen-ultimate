# Changelog

All notable changes to this project are documented here.

## 2026-05-04

### Added

- Documented end-to-end CLI automation pipeline flow and run/resume semantics in `README.md`.
- Added reusable test-folder guidance for repeatable batch validation:
  - `test_root/case_a/front.jpg|png`
  - `test_root/case_b/front.jpg|png`
- Added PR bot triage workflow documentation for fresh actionable feedback on latest commit range.
- Added this `CHANGELOG.md`.

### Changed

- Automation defaults:
  - Front expansion recommended/default percent changed from `30` to `70`.
  - Selfie expansion remains `30`.
- Similarity/runtime hardening:
  - Added fallback path in `similarity_engine.py` for TensorFlow/Keras runtime mismatch during face extraction.
  - Early ML backend environment bootstrap added for CLI path.
- Automation retry behavior:
  - Cases in `manual_review` because of `similarity unavailable` are now rerunnable in case planning.
- Manifest robustness:
  - `AutomationManifest.create_or_load()` now quarantines corrupt/invalid payloads and recreates a fresh manifest payload in the same invocation.
- Runnable-case selection:
  - Existing selfie/video cases are kept runnable for downstream continuation checks instead of being excluded from batch execution.

### Verified

- Offline regression suite passed for targeted automation/manifest/CLI smoke/pipeline tests.
- Two paid live end-to-end verification runs completed with strict oldcam requirement:
  - Resume path on an existing prior-failure case.
  - Clean path on a fresh root.
- Latest successful runs confirmed:
  - No current-run `KerasTensor` similarity failure.
  - Complete per-step outputs through `oldcam`.

### Docs

- Updated `README.md` for CLI automation, reusable retesting workflow, macOS compatibility constraints, and GitHub review loop.
- Updated `AGENTS.md` to reflect active pytest usage, manifest semantics, reusable test-folder practice, and macOS guardrails.
- Updated `CLAUDE.md` for current testing reality, CLI automated pipeline internals, and fresh PR bot triage workflow.
