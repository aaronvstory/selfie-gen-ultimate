"""Insert the uv fast-path CALL block into a Windows launcher .bat, byte-level,
preserving CRLF. Idempotent: re-running is a no-op if the block is present.

Usage: python scripts/_patch_uv_into_launcher.py <launcher.bat>

The block is inserted right after the per-launch diagnostic snapshot (the
`diag-gpu` log line) and before the stamp-key build, so:
  * the diagnostic GPU log still captures hardware info, then
  * the uv fast-path runs; on success it releases the setup lock and jumps to
    :launch, skipping the entire legacy stamp + pip-sync block, which stays
    intact as the fallback (uv_sync_deps returns 3 -> UV_SYNCED empty -> fall
    through to pip).
"""
import sys
from pathlib import Path

# GUI launcher: anchor after the diag-gpu log line; releases the setup lock.
GUI_ANCHOR = b'>>"%LOG_FILE%" echo [%LAUNCH_TS%] diag-gpu %DIAG_GPU%\r\n'
# CLI launcher: anchor before the stamp-key build; no setup lock to release.
CLI_ANCHOR = (
    b"rem --- Build stamp key from req file dates+sizes "
    b"---------------------------\r\n"
)

_HEADER = [
    b"",
    b"rem --- v2.20 uv fast-path -------------------------------------------------",
    b"rem  Try the uv-native dependency sync FIRST (one `uv sync` resolves the",
    b"rem  whole locked set: full face stack + GPU-aware torch/CuPy, no subset, no",
    b"rem  --no-deps gap, no constraints threading -- the lock IS the constraint).",
    b"rem  On success we skip the entire legacy stamp + pip block below and launch",
    b"rem  directly. On any uv problem the helper leaves UV_SYNCED empty and we",
    b"rem  fall through to the proven pip path (set KLING_USE_PIP=1 to force pip).",
    b'call "%ROOT_DIR%\\scripts\\win_uv_sync.bat" "%VENV_PYTHON%" "%ROOT_DIR%"',
    b'if defined UV_SYNCED (',
    b"    echo   [%LAUNCH_TS%] Dependencies ready via uv; skipping pip sync.",
    b'    >>"%LOG_FILE%" echo [%LAUNCH_TS%] uv-sync OK; skipping pip path',
]
_FOOTER = [
    b"    goto :launch",
    b")",
    b'>>"%LOG_FILE%" echo [%LAUNCH_TS%] uv-sync not used; continuing on pip path',
    b"",
]
# GUI block releases the bootstrap mutex before jumping to :launch.
GUI_BLOCK_LINES = _HEADER + [b"    call :release_setup_lock"] + _FOOTER
# CLI block has no setup lock — just jump.
CLI_BLOCK_LINES = _HEADER + _FOOTER


def main() -> int:
    if len(sys.argv) != 3 or sys.argv[1] not in ("gui", "cli"):
        print(
            "usage: _patch_uv_into_launcher.py <gui|cli> <launcher.bat>",
            file=sys.stderr,
        )
        return 2
    kind, target = sys.argv[1], sys.argv[2]
    anchor = GUI_ANCHOR if kind == "gui" else CLI_ANCHOR
    block_lines = GUI_BLOCK_LINES if kind == "gui" else CLI_BLOCK_LINES
    # CLI inserts BEFORE the anchor (the stamp-key build); GUI inserts AFTER.
    path = Path(target)
    data = path.read_bytes()
    if b"win_uv_sync.bat" in data:
        print(f"{path}: already patched, no-op")
        return 0
    if anchor not in data:
        print(f"{path}: ANCHOR not found; not patching", file=sys.stderr)
        return 1
    block = b"\r\n".join(block_lines) + b"\r\n"
    if kind == "gui":
        data = data.replace(anchor, anchor + block, 1)
    else:
        data = data.replace(anchor, block + anchor, 1)
    path.write_bytes(data)
    print(f"{path}: patched ({len(block)} bytes inserted)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
