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
    from distribution.release_prep import RELEASE_BASENAME, RELEASE_VERSION, bundle_release

    dist_root = repo_root / "dist"
    created = list(bundle_release(repo_root, dist_root))
    extracted_root = None
    preferred = dist_root / f"{RELEASE_BASENAME}-v{RELEASE_VERSION}.zip"
    for path in created:
        if path == preferred:
            extracted_root = refresh_extracted_bundle(path, dist_root)
            break
    if extracted_root is None and preferred.exists():
        extracted_root = refresh_extracted_bundle(preferred, dist_root)
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
