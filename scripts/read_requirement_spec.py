#!/usr/bin/env python3
"""Read a single package's requirement line out of a requirements file — safely.

Usage:
    python scripts/read_requirement_spec.py <pkgname> <requirements.txt> [fallback]

Prints EXACTLY ONE line to stdout: the first non-comment requirement line whose
package name is exactly <pkgname> (e.g. ``mediapipe==0.10.35``), or <fallback>
if none is found. All diagnostics go to stderr so the stdout capture stays clean
for a Windows ``for /f`` / POSIX ``$(...)`` consumer.

Why this exists (v2.13): rPPG/run_rppg.bat used to grep the mediapipe spec from
requirements.txt with ``findstr /R "^[ ]*mediapipe"``. Inside the batch
``for /f`` backtick context the ``^`` anchor carets got mangled, so the pattern
matched the FIRST line containing "mediapipe" — which was a COMMENT line
(``# mediapipe (matplotlib drawing_utils); pin both ...``). pip then tried to
install that comment, hit the ``;`` as an environment marker, and crashed with
``InvalidMarker``, failing rPPG. A real parser that ignores comments fixes the
class outright.
"""

from __future__ import annotations

import re
import sys


def find_spec(pkgname: str, path: str, fallback: str) -> str:
    """Return the first matching requirement line, else ``fallback``."""
    # Package name boundary: name followed by a version/marker/extra delimiter
    # or end-of-string. Case-insensitive (pip normalizes names case-insensitively).
    pattern = re.compile(
        r"^" + re.escape(pkgname) + r"(\s|==|>=|<=|!=|~=|<|>|;|\[|$)",
        re.IGNORECASE,
    )
    try:
        with open(path, encoding="utf-8") as fh:
            for raw in fh:
                line = raw.strip()
                if not line or line.startswith("#"):
                    continue
                # Strip a trailing inline comment (" #..."). A leading-# line is
                # already skipped above; this only trims real inline comments.
                line = line.split(" #", 1)[0].strip()
                if not line:
                    continue
                if pattern.match(line):
                    return line
    except OSError as exc:  # file missing / unreadable → fall back
        print(f"[read_requirement_spec] {type(exc).__name__}: {exc}", file=sys.stderr)
    return fallback


def main(argv: list[str]) -> int:
    if len(argv) < 3:
        print(
            "usage: read_requirement_spec.py <pkgname> <requirements.txt> [fallback]",
            file=sys.stderr,
        )
        return 2
    pkgname = argv[1]
    path = argv[2]
    fallback = argv[3] if len(argv) > 3 else pkgname
    spec = find_spec(pkgname, path, fallback)
    # The ONLY thing on stdout — clean for `for /f` / `$(...)` capture.
    print(spec)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
