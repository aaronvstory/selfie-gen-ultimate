"""$0 full-pipeline E2E harness — canned artifacts for the PAID steps only.

User mandate (PR #96 round 8): ~70-80% of the pipeline is free/local and must
be tested FOR REAL — portrait extraction, similarity gate, rPPG (GPU), oldcam,
looping, manifest + Live-dashboard flow. Only four steps cost money (front
expand, selfie gen, selfie expand, Kling video). This harness patches the
three generator classes that ``PipelineDeps``'s default factories instantiate
(``automation.pipeline.OutpaintGenerator/SelfieGenerator/FalAIKlingGenerator``)
with replay fakes that copy previously-produced artifacts into the expected
output locations — "we know the API wiring works, don't keep re-calling the
same API calls."

Canned artifacts live in the gitignored ``test-material/canned-pipeline/``:
    front-expanded.png   — a real front-expand output
    selfie.jpg           — a real selfie-gen output (same identity as front.jpg!)
    selfie-expanded.png  — a real selfie-expand output
    kling.mp4            — a real Kling generation
    front.jpg            — the source front image the above were produced from

Usage:
    venv\\Scripts\\python.exe scripts\\dev_local_e2e.py [--keep-root]

Runs the INTERACTIVE path (_execute_automation_run: preflight table -> Live
dashboard -> completion tables) on a fresh temp root with one case, console
swapped for a force_terminal recording console; greps the capture for panel-
shatter symptoms and prints the per-step manifest verdict. Exit 0 = clean.
"""
from __future__ import annotations

import argparse
import shutil
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

CANNED = REPO / "test-material" / "canned-pipeline"
CAPTURE = REPO / ".claude" / "local_e2e_capture.txt"


class _CannedBase:
    """Common shape for the replay fakes: accept any ctor kwargs (the real
    factories pass api keys), accept a progress callback, and emit a couple
    of progress_update events so the dashboard's Step-prog line is exercised."""

    def __init__(self, *args, **kwargs):
        self._cb = None

    def set_progress_callback(self, cb):
        self._cb = cb

    def _progress(self, msg: str) -> None:
        if self._cb:
            self._cb(msg, "progress_update")

    @staticmethod
    def _deliver(src: Path, output_folder: str, output_path: str | None, default_name: str) -> str:
        dest = Path(output_path) if output_path else Path(output_folder) / default_name
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dest)
        return str(dest)


# Subclass the REAL generators so every class attribute / classmethod the
# pipeline references (sanitize_poll_timeout_seconds, MAX_POLL_TIMEOUT_SECONDS,
# …) keeps working; only __init__ (no api keys needed), the progress hook and
# the PAID methods are overridden.
from outpaint_generator import OutpaintGenerator as _RealOutpaint  # noqa: E402
from selfie_generator import SelfieGenerator as _RealSelfie  # noqa: E402
from kling_generator_falai import FalAIKlingGenerator as _RealVideo  # noqa: E402


class CannedOutpaint(_CannedBase, _RealOutpaint):
    def outpaint(self, image_path: str, output_folder: str, output_path: str | None = None, **kwargs) -> str:
        # Front expand feeds the case front (front.jpg / pass-N output);
        # selfie expand feeds the generated selfie (model slug in the name).
        is_selfie = "sim" in Path(image_path).name.lower() or "banana" in Path(image_path).name.lower()
        src = CANNED / ("selfie-expanded.png" if is_selfie else "front-expanded.png")
        self._progress(f"[canned] outpaint replay ({'selfie' if is_selfie else 'front'}) 100%")
        return self._deliver(src, output_folder, output_path, src.name)


class CannedSelfie(_CannedBase, _RealSelfie):
    def generate(self, image_path: str, prompt: str, output_folder: str,
                 model_endpoint: str, **kwargs) -> str:
        self._progress("[canned] selfie replay 100%")
        slug = model_endpoint.replace("/", "-")
        return self._deliver(CANNED / "selfie.jpg", output_folder, None, f"extracted_{slug}_canned_001.jpg")


class CannedVideo(_CannedBase, _RealVideo):
    def __init__(self, *args, **kwargs):
        _CannedBase.__init__(self)
        self.prompt_slot = kwargs.get("prompt_slot")
        self.model_display_name = kwargs.get("model_display_name")

    def create_kling_generation(self, character_image_path: str, output_folder: str, **kwargs) -> str:
        self._progress("[canned] kling replay — poll 1 · 0s elapsed")
        self._progress("[canned] kling replay — poll 2 · 1s elapsed")
        stem = Path(character_image_path).stem
        return self._deliver(CANNED / "kling.mp4", output_folder, None, f"{stem}_kling_canned_p0.mp4")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="$0 full-pipeline E2E with canned paid-step artifacts.")
    parser.add_argument("--keep-root", action="store_true", help="keep the temp root for inspection")
    args = parser.parse_args(argv)

    missing = [n for n in ("front.jpg", "front-expanded.png", "selfie.jpg", "selfie-expanded.png", "kling.mp4")
               if not (CANNED / n).is_file()]
    if missing:
        print(f"[local-e2e] missing canned artifacts in {CANNED}: {missing}")
        print("[local-e2e] harvest them from a real run first (see docstring).")
        return 2

    from rich.console import Console
    import kling_automation_ui as kmod

    CAPTURE.parent.mkdir(parents=True, exist_ok=True)
    capture_file = open(CAPTURE, "w", encoding="utf-8")
    kmod._RICH_CONSOLE = Console(force_terminal=True, width=110, file=capture_file)

    from kling_automation_ui import KlingAutomationUI
    import automation.pipeline as pmod
    from automation.discovery import discover_case_folders
    from automation.manifest import AutomationManifest

    # Patch the classes the default PipelineDeps factories instantiate.
    pmod.OutpaintGenerator = CannedOutpaint
    pmod.SelfieGenerator = CannedSelfie
    pmod.FalAIKlingGenerator = CannedVideo

    root = Path(tempfile.mkdtemp(prefix="sgu_local_e2e_"))
    case_dir = root / "Canned_Case"
    case_dir.mkdir()
    shutil.copy2(CANNED / "front.jpg", case_dir / "front.jpg")
    print(f"[local-e2e] root: {root}", flush=True)

    ui = KlingAutomationUI.__new__(KlingAutomationUI)
    ui.config_file = str(REPO / "kling_config.json")
    ui.config = ui.load_config()
    ui.config["automation_max_cases_per_run"] = "1"
    ui.config["automation_reprocess_mode"] = "skip"
    ui.automation_root_folder = str(root)
    ui.clear_screen = lambda: None
    ui.pause_review = lambda *a, **k: None
    ui.pause_continue = lambda *a, **k: None
    ui.save_config = lambda: None  # never touch the real config
    ui.fetch_model_pricing = lambda *a, **k: None  # no pricing HTTP in the harness
    ui._use_legacy_prompt_ui = lambda: False  # force the interactive render path

    records = discover_case_folders(root, ui.config.get("automation_front_names", []),
                                    front_globs=ui.config.get("automation_front_globs", []))
    snapshot = {k: v for k, v in ui.config.items() if str(k).startswith("automation_")}
    manifest = AutomationManifest.create_or_load(
        manifest_path=root / "automation_manifest.json", root_dir=root, config_snapshot=snapshot)

    ui._execute_automation_run(manifest, records, records)
    capture_file.flush()
    capture_file.close()

    text = CAPTURE.read_text(encoding="utf-8", errors="replace")
    bad = [s for s in ("Anti-spoofing flagged", "Pricing API returned") if s in text]
    import json
    case = json.loads((root / "automation_manifest.json").read_text(encoding="utf-8"))["cases"]["Canned_Case"]
    print(f"[local-e2e] capture: {CAPTURE} ({len(text)} bytes)", flush=True)
    print(f"[local-e2e] shatter lines: {bad if bad else 'NONE (clean)'}", flush=True)
    print(f"[local-e2e] case status: {case['status']}", flush=True)
    failed_steps = []
    for step, st in case["steps"].items():
        status = st.get("status")
        print(f"[local-e2e]   {step}: {status}", flush=True)
        if status == "failed":
            failed_steps.append(step)
    if not args.keep_root:
        shutil.rmtree(root, ignore_errors=True)
    ok = (not bad) and case["status"] == "complete" and not failed_steps
    print(f"[local-e2e] VERDICT: {'PASS' if ok else 'FAIL'}", flush=True)
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
