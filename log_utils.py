"""Shared logging helpers — uniform, diagnosable failure messages.

The app's tabs and generators historically reported caught exceptions with a
bare ``str(e)``. For many exception types that produces an empty or
near-useless message (e.g. ``KeyError`` → ``'foo'`` with no class, ``TimeoutError``
→ ``''``), so a non-technical user staring at the main log sees "Error: " with no
clue what broke. These helpers give every failure path a consistent, readable
shape — the exception *type* plus its message — and an optional traceback for
the file log / verbose mode.

Usage::

    from log_utils import format_exception_detail
    except Exception as exc:
        self.log(f"Selfie generation failed — {format_exception_detail(exc)}", "error")

Keep this module dependency-free (stdlib only) so every layer — tabs,
generators, the queue worker, the automation pipeline — can import it without
pulling in GUI or third-party deps.
"""

from __future__ import annotations

import traceback


def format_exception_detail(exc: BaseException) -> str:
    """Return ``"<ExceptionType>: <message>"`` — never an empty string.

    A bare ``str(exc)`` is empty for several common exception types
    (``TimeoutError``, ``KeyboardInterrupt``, some ``OSError`` subclasses with no
    args), which produces unactionable log lines. Prefixing the class name
    guarantees the user always learns *what kind* of failure occurred even when
    the message is blank, and disambiguates same-message errors from different
    sources.
    """
    type_name = type(exc).__name__
    message = str(exc).strip()
    if message:
        return f"{type_name}: {message}"
    return type_name


def format_exception_traceback(exc: BaseException) -> str:
    """Return the full formatted traceback for ``exc`` as a single string.

    Intended for ``level="debug"`` routing (file log + verbose mode) so the
    panel stays readable while the full stack is still recoverable. Falls back
    to :func:`format_exception_detail` if the traceback can't be formatted
    (e.g. an exception with no ``__traceback__``).
    """
    try:
        tb = "".join(
            traceback.format_exception(type(exc), exc, exc.__traceback__)
        ).strip()
        return tb or format_exception_detail(exc)
    except Exception:  # noqa: BLE001 — formatting a traceback must never raise
        return format_exception_detail(exc)
