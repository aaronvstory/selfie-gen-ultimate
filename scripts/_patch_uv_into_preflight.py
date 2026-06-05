"""Insert a uv fast-path block into scripts/win_preflight_shared_venv.bat,
byte-level, preserving CRLF. Idempotent."""
from pathlib import Path

TARGET = Path(__file__).resolve().parent / "win_preflight_shared_venv.bat"

# Anchor: the line right before the health probe. We insert the uv attempt
# AFTER the state-dir mkdir and BEFORE the canonical health check, so a
# sub-launcher provisions the full shared env via uv first.
ANCHOR = (
    b'rem Quick probe of the FULL runtime set against the shared venv.\r\n'
)

BLOCK = [
    b"rem --- v2.20 uv fast-path: provision the FULL shared env via one uv sync",
    b"rem  (oldcam/similarity launched before the main app still install no",
    b"rem  subset -- they funnel through the same canonical uv sync). On any uv",
    b"rem  problem this no-ops + we fall through to the health probe/repair below.",
    b"rem  SEQUENTIAL if/goto (NOT nested-if + paren block): a nested",
    b'rem  `if not "X"=="1" if exist "Y" ( ... )` crashes cmd on Windows 11 25H2',
    b'rem  with ". was unexpected at this time" (cross-os-bounce-traps Trap 7,',
    b"rem  reproduced on this hardware in PR #55). Keep this flat.",
    b'if "%KLING_USE_PIP%"=="1" goto :_pf_uv_skip',
    b'if not exist "%_PF_ROOT%\\scripts\\win_uv_sync.bat" goto :_pf_uv_skip',
    b'call "%_PF_ROOT%\\scripts\\win_uv_sync.bat" "%_PF_PY%" "%_PF_ROOT%"',
    b"if defined UV_SYNCED (",
    b"    echo   [preflight] shared env ready via uv.",
    b"    goto :_pf_done",
    b")",
    b":_pf_uv_skip",
]


def main() -> int:
    data = TARGET.read_bytes()
    if b"win_uv_sync.bat" in data:
        print(f"{TARGET}: already patched, no-op")
        return 0
    if ANCHOR not in data:
        print(f"{TARGET}: ANCHOR not found", flush=True)
        return 1
    block = b"\r\n".join(BLOCK) + b"\r\n"
    data = data.replace(ANCHOR, block + ANCHOR, 1)
    TARGET.write_bytes(data)
    print(f"{TARGET}: patched ({len(block)} bytes)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
