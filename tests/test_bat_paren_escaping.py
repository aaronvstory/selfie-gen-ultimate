r"""Tripwire: an unescaped parenthesis inside a cmd.exe `(...)` block crashes
the batch file at runtime with "<word> was unexpected at this time." (exit 255).

This is the recurring "cmd nested-block parens crash" (project memory:
feedback_macos_windows_bounce_traps, Trap 7). It bit rPPG HARD: in
`rPPG/run_rppg.bat`, line 189 inside an `if exist (...)` block read

    >>"%LOG_FILE%" echo [INFO] ran gpu_bootstrap (CuPy) before rPPG injector

The unescaped `)` in `(CuPy)` closed the `if` block early, so cmd then tried to
execute `before rPPG injector` as a command -> "before was unexpected at this
time." -> exit 255 -> rPPG died BEFORE launching the injector -> every v2.17
Windows run silently fell back to `-NORPPG`. The block runs whenever
`scripts/gpu_bootstrap.py` exists, i.e. every v2.17 install.

A bare `import`/source scan would NOT catch this (the file is syntactically a
fine `.bat`); only modelling cmd's paren-depth parser does. This test is that
model: it walks each tracked `.bat`, tracks `(...)` block depth the way cmd
does, and fails if a line executed INSIDE a block contains an unescaped paren
that cmd would mis-parse.

The rule we enforce (conservative, matches cmd's real behaviour):
* Inside a parenthesised block (depth > 0), any literal `(` or `)` in the
  command text MUST be caret-escaped (`^(` / `^)`), OR appear inside a
  double-quoted string. An unescaped one closes/!opens the block.
* `echo` lines are the overwhelming offender (the text is free-form), so we
  focus the assertion there to keep false-positives ~zero, but the depth
  tracking itself accounts for ALL lines.
"""

from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]

# .bat files anywhere in the tree that git tracks. Globbed live so a new
# launcher is covered automatically.
# Exclude build artifacts (dist/ holds extracted-zip copies that mirror the
# real launchers) and vendored trees -- we lint the SOURCE .bat files.
_EXCLUDED_TOP = {".git", "node_modules", "dist", "build", ".venv", ".venv311", "venv", ".recovery"}
BAT_FILES = sorted(
    p for p in REPO_ROOT.rglob("*.bat")
    if not (_EXCLUDED_TOP & set(p.relative_to(REPO_ROOT).parts))
)


def _strip_quoted(s: str) -> str:
    """Remove double-quoted spans so parens inside quotes don't count (cmd does
    not treat `"(...)"` as block delimiters)."""
    out = []
    in_q = False
    for ch in s:
        if ch == '"':
            in_q = not in_q
            continue
        if not in_q:
            out.append(ch)
    return "".join(out)


def _count_unescaped_unquoted(text: str):
    """(open_count, close_count) of UNescaped, UNquoted parens in `text`."""
    text = _strip_quoted(text)
    opens = closes = 0
    i = 0
    while i < len(text):
        ch = text[i]
        if ch == "^":
            i += 2  # caret escapes the next char (incl. ^( / ^) )
            continue
        if ch == "(":
            opens += 1
        elif ch == ")":
            closes += 1
        i += 1
    return opens, closes


def _structural_delta(line: str):
    """How much this line changes cmd's compound-statement block DEPTH.

    cmd only changes block depth on STRUCTURAL parens, not parens inside an
    `echo`/command's argument text. We model the two structural forms:

    * an opener: a control-flow head (`if ... (`, `for ... (`, `else (`, or a
      bare `(`) where the line ENDS in an unescaped `(`  -> +1.
    * a closer: a line that STARTS with `)` (e.g. `)`, `) else (`)            -> the
      leading `)` is -1, and a trailing `(` on `) else (` re-opens +1.

    Parens that appear only as `echo (text)` arguments do NOT move depth (cmd
    treats them literally once it's parsing the command's args). Returns the net
    structural delta for the line.
    """
    delta = 0
    stripped = line.rstrip()
    # leading structural close
    if stripped.startswith(")"):
        delta -= 1
        # `) else (` style re-open
        if stripped.endswith("(") and not stripped.endswith("^("):
            delta += 1
        return delta
    # trailing structural open (control-flow head or bare `(`), but NOT the
    # `echo(` blank-line idiom. The idiom appears both bare (`echo(`) and after a
    # redirect (`>>"%LOG_FILE%" echo(`), so a line-start check misses the redirect
    # form and inflates the tracked depth. Look at the text AFTER the last `echo`
    # token -- if it's just `(`, the paren belongs to the echo, not a block opener
    # (code-review MEDIUM, PR #70).
    low = stripped.lower()
    if stripped.endswith("(") and not stripped.endswith("^("):
        pos = low.rfind("echo")
        after_echo = low[pos + 4:].strip() if pos != -1 else None
        if after_echo != "(":  # not the echo-blank-line idiom
            delta += 1
    return delta


@pytest.mark.parametrize("bat", BAT_FILES, ids=lambda p: str(p.relative_to(REPO_ROOT)))
def test_no_unescaped_paren_inside_cmd_block(bat: Path):
    """Walk the .bat tracking `(...)` block depth; flag an `echo` line executed
    inside a block (depth>0 at line start) that adds an unescaped paren which
    cmd would mis-parse as a block delimiter."""
    raw = bat.read_text(encoding="utf-8", errors="replace")
    depth = 0
    offenders = []
    for lineno, raw_line in enumerate(raw.splitlines(), start=1):
        line = raw_line.strip()
        if not line:
            continue
        # cmd enters a label (`:foo`) as a GOTO target at block depth 0 -- linear
        # paren counting across label boundaries is wrong (control flow jumps),
        # which otherwise balloons a bogus depth. Reset at every label so we only
        # measure depth WITHIN a straight-line region (which is where the real
        # crash lives: an opener `(` ... an unescaped paren ... before the `)`).
        if line.startswith(":"):
            depth = 0
            continue

        delta = _structural_delta(line)

        # The dangerous case: we are INSIDE a block (depth>0) and this line is an
        # `echo`/log line whose ARGUMENT text carries an unescaped paren that
        # cmd will mis-read as a block delimiter. We only flag a NET-unbalanced
        # paren in the argument (a lone `)` or lone `(` in the echo text), since
        # a fully-balanced `(...)` inside echo args is the same risk but the
        # canonical crash is the unbalanced one. To stay zero-false-positive we
        # require: depth>0, it's an echo/redirect-echo line, it is NOT itself a
        # structural opener/closer line, and its arg text has an unescaped paren.
        lowered = line.lower()
        # `echo(` and `echo.` are the blank-line idioms -- the `(`/`.` is part of
        # the echo token, NOT a block paren. Strip the echo command word and look
        # ONLY at the argument text for stray parens.
        is_echo_cmd = lowered.startswith("echo ") or " echo " in (" " + lowered)
        is_structural = delta != 0 or line.rstrip().startswith(")")
        if depth > 0 and is_echo_cmd and not is_structural:
            # Take the text AFTER the `echo` token (handles `>>file echo ...` too).
            arg = line
            idx = lowered.find("echo ")
            if idx != -1:
                arg = line[idx + len("echo "):]
            opens, closes = _count_unescaped_unquoted(arg)
            # ANY unescaped paren in an echo ARG inside a block is dangerous:
            # the first `)` closes the block early ("X was unexpected..."), and a
            # lone `(` opens an unterminated block. Balanced `(...)` is ALSO unsafe
            # here because cmd's block scanner sees the `)` before echo's args.
            if opens or closes:
                offenders.append((lineno, raw_line.strip()))

        depth += delta
        if depth < 0:
            depth = 0  # forgiving: a stray close just resets to top level

    assert not offenders, (
        f"{bat.relative_to(REPO_ROOT)}: unescaped paren(s) inside a cmd (...) "
        f"block -- cmd will crash with '<word> was unexpected at this time.' "
        f"Escape them as ^( / ^). Offending lines:\n"
        + "\n".join(f"  L{ln}: {txt}" for ln, txt in offenders)
    )


def test_structural_delta_treats_redirect_echo_blank_as_zero():
    """`>>file echo(` is the redirect form of the echo-blank-line idiom; it must
    NOT count as a block opener (would inflate tracked depth -> spurious false
    positives downstream). Code-review MEDIUM, PR #70."""
    assert _structural_delta('>>"%LOG_FILE%" echo(') == 0
    assert _structural_delta("echo(") == 0
    assert _structural_delta("echo (") == 0
    # genuine openers still count
    assert _structural_delta("if exist x (") == 1
    assert _structural_delta('for %%I in ("..") do (') == 1
    # genuine closer
    assert _structural_delta(")") == -1
    # `) else (` nets zero
    assert _structural_delta(") else (") == 0
    # escaped trailing paren is not an opener
    assert _structural_delta("echo done ^(") == 0


def test_run_rppg_bat_gpu_log_line_is_escaped():
    """Direct regression pin for the exact line that crashed rPPG on every
    v2.17 Windows run (the (CuPy) paren)."""
    src = (REPO_ROOT / "rPPG" / "run_rppg.bat").read_text(encoding="utf-8", errors="replace")
    assert "gpu_bootstrap (CuPy)" not in src, (
        "rPPG/run_rppg.bat: the gpu_bootstrap log line has an UNescaped (CuPy) "
        "inside the `if exist (...)` block -> 'before was unexpected at this "
        "time.' -> rPPG -NORPPGs every run. Escape as ^(CuPy^)."
    )
    assert "gpu_bootstrap ^(CuPy^)" in src
