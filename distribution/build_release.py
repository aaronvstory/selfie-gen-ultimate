from __future__ import annotations

import sys
from pathlib import Path


def main() -> int:
    repo_root = Path(__file__).resolve().parent.parent
    if str(repo_root) not in sys.path:
        sys.path.insert(0, str(repo_root))
    from distribution.release_prep import bundle_release

    dist_root = repo_root / "dist"
    created = list(bundle_release(repo_root, dist_root))
    print("Created release bundles:")
    for path in created:
        print(f"- {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
