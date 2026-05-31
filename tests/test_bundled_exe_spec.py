"""Static guards on the bundled-exe build (spec + bridge + first-run seed).

The bundled Windows .exe ships WITHOUT the heavy ML stack (torch/TF/mediapipe/
deepface/retinaface/opencv) — those install lazily into a side venv on first
face-crop/similarity use and run as a subprocess. These tests pin that contract
so a future edit can't silently re-bundle the 4-8GB ML stack or break the
first-run config seeding.

Pure static text / import checks — no PyInstaller run, no subprocess.
"""
import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]

SPEC = REPO_ROOT / "distribution" / "kling_gui_bundled.spec"
BUILDER = REPO_ROOT / "distribution" / "build_bundled_exe.py"
BRIDGE = REPO_ROOT / "ml_subprocess_bridge.py"
LAUNCHER = REPO_ROOT / "gui_launcher.py"

ML_PACKAGES = ("torch", "tensorflow", "mediapipe", "deepface", "retinaface", "cv2")


def _read(p: Path) -> str:
    return p.read_text(encoding="utf-8")


# ── Spec excludes the ML stack, bundles the runtime install machinery ───


def test_spec_exists():
    assert SPEC.exists(), "bundled spec missing"


def test_spec_excludes_ml_stack():
    src = _read(SPEC)
    # Find the excludes=[...] block and assert each heavy package is named there.
    m = re.search(r"excludes=\[(.*?)\]", src, re.DOTALL)
    assert m, "spec has no excludes list"
    excludes = m.group(1)
    for pkg in ("torch", "tensorflow", "mediapipe", "deepface", "cv2"):
        assert pkg in excludes, (
            f"{pkg} must be in the spec's excludes (it installs to the first-run "
            f"side venv, NOT the bundle — else the exe balloons to multi-GB)"
        )


def test_spec_does_not_collect_ml_submodules():
    src = _read(SPEC)
    # The developer spec used collect_submodules('deepface') / ('retinaface').
    # The bundled spec must NOT — that would drag the ML stack back in.
    for pkg in ("deepface", "retinaface", "torch"):
        assert f"collect_submodules('{pkg}')" not in src, (
            f"bundled spec must not collect_submodules('{pkg}')"
        )


def test_spec_bundles_resolver_and_requirements():
    src = _read(SPEC)
    assert "win_resolve_python.bat" in src, (
        "spec must bundle the shared resolver so first-run ML install can run"
    )
    assert "requirements.txt" in src, (
        "spec must bundle requirements.txt for the side-venv install"
    )
    assert "ml_subprocess_bridge" in src, (
        "spec must include the ml_subprocess_bridge hidden import"
    )


def test_spec_bundles_personal_seed_config_reference():
    src = _read(SPEC)
    assert "personal_seed_config.json" in src, (
        "spec must reference the personal seed config (bundled when present)"
    )


# ── Builder blanks keys + has a size guard ─────────────────────────────


def test_builder_blanks_api_keys_in_seed():
    src = _read(BUILDER)
    for k in ("falai_api_key", "freeimage_api_key", "bfl_api_key", "openrouter_api_key"):
        assert k in src, f"builder must verify {k} is blanked in the seed"
    assert "refusing to bundle a key" in src, (
        "builder must abort if a key survives into the seed config"
    )


def test_builder_has_bundle_size_guard():
    src = _read(BUILDER)
    assert "SIZE_MAX_MB" in src and "leaked past" in src.lower().replace("`", ""), (
        "builder must guard against the ML stack leaking into the bundle"
    )


# ── Bridge contract ────────────────────────────────────────────────────


def test_bridge_imports_cleanly_without_ml():
    """The bridge must be import-safe with NO heavy deps present (it runs
    inside the frozen exe which has no torch/TF)."""
    import importlib
    import sys as _sys
    # Import by file to avoid polluting; it should not import torch/TF/cv2.
    spec_mod = importlib.util.spec_from_file_location("ml_subprocess_bridge", BRIDGE)
    mod = importlib.util.module_from_spec(spec_mod)
    spec_mod.loader.exec_module(mod)
    assert hasattr(mod, "ensure_ml_stack")
    assert hasattr(mod, "run_in_ml_venv")
    assert hasattr(mod, "ml_stack_ready")
    # None of the heavy ML modules should have been imported as a side effect.
    for pkg in ("torch", "tensorflow", "deepface"):
        assert pkg not in _sys.modules, (
            f"ml_subprocess_bridge import pulled in {pkg} — it must stay lazy"
        )


def test_bridge_targets_python_3_12_for_autoinstall():
    src = _read(BRIDGE)
    # The auto-install re-probe must prefer 3.12 (mediapipe cap), matching the
    # resolver. And it must NOT use a fresh `where python` after install.
    assert '"3.12"' in src or "3.12" in src
    assert "where python" not in src, (
        "bridge must not rely on `where python` (installer PATH edit doesn't "
        "reach the running process)"
    )


# ── First-run config seed in the launcher ──────────────────────────────


def test_launcher_seeds_config_only_when_frozen_and_absent():
    src = _read(LAUNCHER)
    assert "_seed_config_if_frozen_first_run" in src, (
        "gui_launcher must define the first-run seed function"
    )
    assert "is_frozen()" in src, "seed must be gated on is_frozen()"
    assert "personal_seed_config.json" in src, "seed must read the bundled seed file"
    # Must NOT overwrite an existing config.
    assert "if os.path.exists(target)" in src and "return" in src, (
        "seed must no-op when a config already exists (never clobber keys)"
    )
    # And it must actually be called from main().
    assert re.search(r"def main\(\):.*_seed_config_if_frozen_first_run\(\)", src, re.DOTALL), (
        "main() must call _seed_config_if_frozen_first_run() before launching the GUI"
    )
