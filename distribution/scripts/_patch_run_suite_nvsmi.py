"""One-shot byte-level patcher: RUN_SUITE.bat nvidia-smi resolution.

Gemini HIGH (PR #96 round 10): nvidia-smi is not always on PATH (some
driver installs only carry %ProgramFiles%\\NVIDIA Corporation\\NVSMI).
Resolve NVSMI_EXE once at startup with install-dir fallbacks and use the
resolved path in BOTH GPU sections. Byte-level CRLF-preserving patch per
the Windows-launcher rules (never Write/Edit a .bat). Committed for
reproducibility per the repo's scripts/_gen_* / _patch_* convention.
"""
from __future__ import annotations

from pathlib import Path

PATH = Path(__file__).resolve().parents[1] / "RUN_SUITE.bat"


def rep(data: bytes, old: str, new: str) -> bytes:
    ob = old.replace("\n", "\r\n").encode("ascii")
    nb = new.replace("\n", "\r\n").encode("ascii")
    if data.count(ob) != 1:
        raise SystemExit(f"anchor not unique ({data.count(ob)}x): {old[:60]!r}")
    return data.replace(ob, nb)


def main() -> None:
    data = PATH.read_bytes()
    assert b"\r\n" in data, "RUN_SUITE.bat must be CRLF"

    data = rep(
        data,
        'set "ROOT_DIR=%~dp0"\n\nrem --- App version',
        'set "ROOT_DIR=%~dp0"\n'
        "\n"
        "rem --- nvidia-smi resolution (PATH first, then standard install dirs) ---------\n"
        "rem Driver packages put nvidia-smi.exe in System32 (usually on PATH), but some\n"
        "rem setups only carry ProgramFiles\\NVIDIA Corporation\\NVSMI. Resolve ONCE here\n"
        "rem and use the resolved path everywhere (gemini HIGH, PR #96 round 10).\n"
        'set "NVSMI_EXE="\n'
        "where nvidia-smi >nul 2>&1\n"
        'if not errorlevel 1 set "NVSMI_EXE=nvidia-smi"\n'
        'if not defined NVSMI_EXE if exist "%SystemRoot%\\System32\\nvidia-smi.exe" set "NVSMI_EXE=%SystemRoot%\\System32\\nvidia-smi.exe"\n'
        'if not defined NVSMI_EXE if exist "%ProgramFiles%\\NVIDIA Corporation\\NVSMI\\nvidia-smi.exe" set "NVSMI_EXE=%ProgramFiles%\\NVIDIA Corporation\\NVSMI\\nvidia-smi.exe"\n'
        "\n"
        "rem --- App version",
    )

    data = rep(
        data,
        ":gpu_brief\n"
        'set "GPU_NAME="\n'
        'set "GPU_DRV="\n'
        "where nvidia-smi >nul 2>&1\n"
        "if errorlevel 1 goto gpu_brief_none\n"
        "for /f \"tokens=1,2 delims=,\" %%A in ('cmd /c \"nvidia-smi --query-gpu=name,driver_version --format=csv,noheader\" 2^>nul') do set \"GPU_NAME=%%A\" & set \"GPU_DRV=%%B\"",
        ":gpu_brief\n"
        'set "GPU_NAME="\n'
        'set "GPU_DRV="\n'
        "if not defined NVSMI_EXE goto gpu_brief_none\n"
        "for /f \"tokens=1,2 delims=,\" %%A in ('cmd /c \"\"%NVSMI_EXE%\" --query-gpu=name,driver_version --format=csv,noheader\" 2^>nul') do set \"GPU_NAME=%%A\" & set \"GPU_DRV=%%B\"",
    )

    data = rep(
        data,
        ":gpu_full\n"
        "echo(\n"
        "where nvidia-smi >nul 2>&1\n"
        "if errorlevel 1 goto gpu_full_none\n"
        "nvidia-smi\n"
        "echo(\n"
        "echo   %CLRC%--- Per-GPU summary -------------------------------------------------------%CLR0%\n"
        "nvidia-smi --query-gpu=name,driver_version,memory.total,memory.used --format=csv",
        ":gpu_full\n"
        "echo(\n"
        "if not defined NVSMI_EXE goto gpu_full_none\n"
        '"%NVSMI_EXE%"\n'
        "echo(\n"
        "echo   %CLRC%--- Per-GPU summary -------------------------------------------------------%CLR0%\n"
        '"%NVSMI_EXE%" --query-gpu=name,driver_version,memory.total,memory.used --format=csv',
    )

    PATH.write_bytes(data)
    assert data.count(b"\n") == data.count(b"\r\n"), "LF-only line leaked"
    data.decode("ascii")
    print("RUN_SUITE.bat patched - CRLF + ASCII verified")


if __name__ == "__main__":
    main()
