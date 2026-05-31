"""Build the bundled Windows .exe distributable.

Pipeline:
  1. Verify we're bumping a real version (reads app_version.RELEASE_VERSION).
  2. Build distribution/personal_seed_config.json from the user's LIVE config
     using the same blanking logic as build_release_personal (all prompts/slots/
     defaults preserved; 4 API keys + 3 machine paths blanked). This file is
     picked up by kling_gui_bundled.spec and seeded to %LocalAppData% on first
     run only.
  3. Run PyInstaller with distribution/kling_gui_bundled.spec — produces
     dist/SelfieGenUltimate/SelfieGenUltimate.exe (one-folder, ML stack
     EXCLUDED; installed lazily to a side venv on first face-crop/similarity
     use via ml_subprocess_bridge + scripts/win_resolve_python.bat).
  4. Sanity-check bundle size: a healthy bundle is ~150-300MB. If it's GBs,
     the ML stack leaked into the bundle — check the spec's `excludes`.

Run from the repo root:
    python distribution/build_bundled_exe.py

This is a build tool, not shipped at runtime. It is Windows-only (the bundled
exe target is Windows); on other OSes it exits early with a clear message.
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DIST_DIR = ROOT / "distribution"
for p in (str(ROOT), str(DIST_DIR)):
    if p not in sys.path:
        sys.path.insert(0, p)

SEED_PATH = DIST_DIR / "personal_seed_config.json"
SPEC_PATH = DIST_DIR / "kling_gui_bundled.spec"
LIVE_CONFIG = ROOT / "kling_config.json"
TEMPLATE = ROOT / "default_config_template.json"

# Bundle-size sanity bounds (MB). Lower bound catches an empty/broken build;
# upper bound catches the ML stack leaking past the spec's `excludes`.
SIZE_MIN_MB = 60
SIZE_MAX_MB = 600


def build_seed_config() -> None:
    """Write distribution/personal_seed_config.json (blanked personal config)."""
    try:
        import build_release_personal as brp  # reuses the blanking logic
    except ImportError as exc:
        raise SystemExit(
            "ABORT: distribution/build_release_personal.py is required to build the "
            f"personal seed config but could not be imported ({exc}). It ships with "
            "the repo; ensure you're running from a full checkout."
        )

    cfg = brp._personal_build_config(TEMPLATE, LIVE_CONFIG if LIVE_CONFIG.exists() else None)
    SEED_PATH.write_text(json.dumps(cfg, indent=2), encoding="utf-8")

    # Verify the share-safety contract before we bake it into an exe.
    blanked_keys = ("falai_api_key", "freeimage_api_key", "bfl_api_key", "openrouter_api_key")
    for k in blanked_keys:
        if cfg.get(k, "") not in ("", None):
            raise SystemExit(f"ABORT: seed config did not blank {k!r} — refusing to bundle a key.")
    print(f"  Seed config written: {SEED_PATH}  ({len(cfg)} keys, API keys blanked)")


def run_pyinstaller() -> None:
    print("  Running PyInstaller (bundled spec)...")
    rc = subprocess.run(
        [sys.executable, "-m", "PyInstaller", str(SPEC_PATH), "--noconfirm"],
        cwd=str(ROOT),
    ).returncode
    if rc != 0:
        raise SystemExit(f"ABORT: PyInstaller failed (exit {rc}).")


def check_bundle_size() -> None:
    out_dir = ROOT / "dist" / "SelfieGenUltimate"
    if not out_dir.exists():
        raise SystemExit(f"ABORT: expected build output missing: {out_dir}")
    total = sum(f.stat().st_size for f in out_dir.rglob("*") if f.is_file())
    mb = total / 1024 / 1024
    print(f"  Bundle size: {mb:.0f} MB")
    if mb < SIZE_MIN_MB:
        raise SystemExit(f"ABORT: bundle is only {mb:.0f}MB (< {SIZE_MIN_MB}) — likely broken.")
    if mb > SIZE_MAX_MB:
        raise SystemExit(
            f"ABORT: bundle is {mb:.0f}MB (> {SIZE_MAX_MB}). The ML stack probably "
            "leaked past `excludes` in kling_gui_bundled.spec — torch/TF/mediapipe/"
            "deepface must NOT be in the bundle (they install to the first-run side venv)."
        )


def main() -> int:
    if sys.platform != "win32":
        print("build_bundled_exe.py targets Windows only (the bundled .exe is a "
              "Windows artifact). Run this on the Windows machine.")
        return 0

    from app_version import RELEASE_VERSION
    print(f"=== Building SelfieGenUltimate bundled .exe  ({RELEASE_VERSION}) ===")

    if not SPEC_PATH.exists():
        raise SystemExit(f"ABORT: spec missing: {SPEC_PATH}")

    build_seed_config()
    run_pyinstaller()
    check_bundle_size()

    print()
    print("  Build complete: dist/SelfieGenUltimate/SelfieGenUltimate.exe")
    print("  Next (manual, needs a clean Windows VM):")
    print("   * double-click the exe with NO Python installed -> GUI opens,")
    print("     version chip shows the right version, cloud gen works.")
    print("   * Face Crop -> one-time ML side-venv install runs, then works.")
    print("   * confirm seeded config has prompts/slots, blank API keys.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
