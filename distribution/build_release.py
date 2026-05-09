from __future__ import annotations

import sys
from pathlib import Path
import shutil
import zipfile


def refresh_extracted_bundle(zip_path: Path, extract_root: Path) -> Path:
    """Refresh extracted shareable bundle folder from a release zip."""
    target = extract_root / "SelfieGenUltimate"
    if target.exists():
        shutil.rmtree(target)
    target.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(zip_path) as zf:
        zf.extractall(target)
    return target


def main() -> int:
    repo_root = Path(__file__).resolve().parent.parent
    if str(repo_root) not in sys.path:
        sys.path.insert(0, str(repo_root))
    from distribution.release_prep import bundle_release

    dist_root = repo_root / "dist"
    created = list(bundle_release(repo_root, dist_root))
    extracted_root = None
    for path in created:
        if path.name == "SelfieGenUltimate.zip":
            extracted_root = refresh_extracted_bundle(path, dist_root)
            break
    print("Created release bundle:")
    for path in created:
        print(f"- {path}")
    if extracted_root is not None:
        print(f"Refreshed extracted bundle folder: {extracted_root}")
    print(
        "Safety: Use freshly recreated dist/SelfieGenUltimate/... or unzip latest zip; "
        "old extracted folders can contain stale launchers."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
