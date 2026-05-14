import argparse
import os
import sys
import traceback


def _write_crash_log(exc: Exception) -> str:
    """Persist a fatal crash traceback to similarity/crash.log."""
    crash_path = os.path.join(os.path.dirname(__file__), "crash.log")
    try:
        with open(crash_path, "a", encoding="utf-8") as f:
            f.write("=" * 80 + "\n")
            f.write(f"Fatal error: {exc}\n")
            f.write(traceback.format_exc())
            f.write("\n")
    except Exception as log_exc:
        raise RuntimeError(f"Could not write crash log at {crash_path}: {log_exc}") from log_exc
    return crash_path


def _run_with_crash_logging() -> None:
    """Run app entrypoint and preserve original failure if crash logging also fails."""
    try:
        main()
    except Exception as exc:
        try:
            crash_log_path = _write_crash_log(exc)
            print(
                f"[FATAL] Similarity app crashed: {exc}. Traceback saved to: {crash_log_path}",
                file=sys.stderr,
            )
        except Exception as log_exc:
            print(
                f"[FATAL] Similarity app crashed: {exc}. Crash logging failed: {log_exc}",
                file=sys.stderr,
            )
        raise

def main():
    """
    Entry point for the Face Similarity Application.
    Routes to either the GUI (default) or the CLI based on arguments.
    """
    parser = argparse.ArgumentParser(
        description="Enterprise-Grade Local Face Similarity Application",
        epilog="By default, launches the modern dark-mode GUI."
    )
    parser.add_argument(
        "--cli", 
        action="store_true", 
        help="Run in Command Line Interface mode."
    )
    parser.add_argument(
        "--img1", 
        type=str, 
        help="Path to the first image (CLI mode only)."
    )
    parser.add_argument(
        "--img2", 
        type=str, 
        help="Path to the second image (CLI mode only)."
    )
    parser.add_argument(
        "--mode",
        choices=["gui", "interactive", "compare", "similarity", "extract"],
        help="Launch mode for the application."
    )
    parser.add_argument("--root", type=str, help="Root directory for batch workflows.")
    parser.add_argument("--yes", action="store_true", help="Skip confirmation prompts in batch mode.")
    parser.add_argument("--img1-keyword", type=str, help="Keyword or regex for similarity image 1.")
    parser.add_argument("--img2-keyword", type=str, help="Keyword or regex for similarity image 2.")
    parser.add_argument("--extraction-keyword", type=str, help="Keyword or regex for extraction discovery.")
    parser.add_argument("--padding-ratio", type=float, help="Face extraction padding ratio.")
    parser.add_argument(
        "--existing-file-mode",
        choices=["index", "skip", "overwrite"],
        help="Existing-file handling for extraction."
    )
    parser.add_argument(
        "--anti-spoof",
        dest="anti_spoof",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="Enable / disable face anti-spoofing on the engine. Use --anti-spoof or --no-anti-spoof.",
    )

    args = parser.parse_args()

    cli_requested = bool(
        args.cli
        or args.mode in {"interactive", "compare", "similarity", "extract"}
        or args.img1
        or args.img2
        or args.root
        or args.img1_keyword
        or args.img2_keyword
        or args.extraction_keyword
        or args.padding_ratio is not None
        or args.existing_file_mode
        or args.anti_spoof is not None
    )

    if not cli_requested:
        try:
            from src.gui import run_gui
            run_gui()
        except ImportError as e:
            print(f"Failed to load GUI components. Ensure all dependencies are installed: {e}")
            sys.exit(1)
        return

    try:
        from src.cli import ProCLI
    except ImportError as e:
        print(f"Failed to load CLI components. Ensure all dependencies are installed: {e}")
        sys.exit(1)

    cli = ProCLI()
    try:
        cli.apply_runtime_config(
            img1_keyword=args.img1_keyword,
            img2_keyword=args.img2_keyword,
            extraction_keyword=args.extraction_keyword,
            padding_ratio=args.padding_ratio,
            existing_file_mode=args.existing_file_mode,
            anti_spoofing=args.anti_spoof,
        )
    except ValueError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(2)

    mode = args.mode
    if mode == "gui":
        from src.gui import run_gui
        run_gui()
        return

    if mode == "similarity":
        if not args.root:
            print("Batch similarity mode requires --root.")
            sys.exit(2)
        cli.run_batch_similarity(root_dir=args.root, confirm=not args.yes)
        return

    if mode == "extract":
        if not args.root:
            print("Batch extraction mode requires --root.")
            sys.exit(2)
        cli.run_batch_extraction(root_dir=args.root, confirm=not args.yes)
        return

    if mode == "compare" or args.img1 or args.img2:
        if mode == "compare" and (not args.img1 or not args.img2):
            print("Compare mode requires both --img1 and --img2.")
            sys.exit(2)
        try:
            cli.run(img1_path=args.img1, img2_path=args.img2)
        except Exception as e:
            print(f"CLI comparison failed: {e}")
            sys.exit(1)
        return

    cli.run()

if __name__ == "__main__":
    _run_with_crash_logging()
