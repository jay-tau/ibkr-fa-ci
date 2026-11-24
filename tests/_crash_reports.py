"""Shared checks for the CLI's fixed crash and filesystem lines (#1049).

A crash prints its class and innermost ``ibkr_fa`` source line, never its
message, cause or context; an ``OSError`` prints fixed text and its errno name,
never its text or file names.
"""

from __future__ import annotations

import dataclasses
import re
import traceback
from collections.abc import Callable
from pathlib import Path
from typing import NoReturn

import pytest

from ibkr_fa import cli
from ibkr_fa.exceptions import IbkrFaError, ReplacedCrashError

# Distinct markers for the crash's message, cause and context; ``ANN`` is
# checked as a case-insensitive whole token, since it sits inside many words.
MESSAGE = "Synthetic Holder Two"
CAUSE = "U1234567"
CONTEXT = "Synthetic Context Marker"
MARKERS = (MESSAGE, CAUSE, CONTEXT, "ANN")


def crash_pattern(exc_type: type[BaseException]) -> str:
    """A pattern for a crash site, such as ``KeyError at ibkr_fa/cli.py:123``."""
    return rf"{exc_type.__name__} at ibkr_fa/[\w/]+\.py:\d+"


def internal_error_line(exc_type: type[BaseException]) -> str:
    """A pattern for the line :func:`ibkr_fa.cli.main` prints for a crash."""
    return rf"error: internal error \({crash_pattern(exc_type)}\)\n"


def cli_site(exc_type: type[BaseException], code: str) -> str:
    """The crash site of ``exc_type`` raised by the call on the one ``cli.py``
    line that contains ``code``."""
    lines = Path(cli.__file__).read_text(encoding="utf-8").splitlines()
    [number] = [index for index, line in enumerate(lines, start=1) if code in line]
    return f"{exc_type.__name__} at ibkr_fa/cli.py:{number}"


def assert_no_markers(text: str, *markers: str) -> None:
    """``text`` holds none of ``markers`` (default :data:`MARKERS`),
    compared case-insensitively; ``ANN`` only as a whole token."""
    for marker in markers or MARKERS:
        if marker == "ANN":
            assert not re.search(r"\bann\b", text, re.IGNORECASE), marker
        else:
            assert marker.lower() not in text.lower(), marker


def render_without_frames(error: BaseException) -> str:
    """``error`` as :func:`traceback.format_exception` prints it, less every
    traceback header and frame (#2013).

    It keeps each exception's type and message line and its notes, the
    causes, contexts and exception-group members that the full traceback
    prints, and the lines between them. A frame's file, line, source and caret
    lines include file paths and code, so a short marker can match them by
    chance."""
    rendered = traceback.TracebackException.from_exception(error, compact=True)
    pending = [rendered]
    while pending:
        current = pending.pop()
        current.stack = traceback.StackSummary()
        links = (current.__cause__, current.__context__, *(current.exceptions or ()))
        pending += [link for link in links if link is not None]
    return "".join(rendered.format())


# Attributes where exceptions keep input: a decode error's bytes, a TOML
# error's document (Python 3.14), a subprocess's output, a transport error's
# URL and reason, an HTTP error's headers, a partial read's bytes, a bad status
# line, and a filesystem error's text and file names.
_CONTENT_ATTRIBUTES = (
    "object",
    "doc",
    "output",
    "stdout",
    "stderr",
    "url",
    "reason",
    "headers",
    "partial",
    "line",
    "strerror",
    "filename",
    "filename2",
)


def assert_prints_no_marker(error: BaseException, *, markers: tuple[str, ...]) -> None:
    """No input marker is in ``error``'s fields, ``str``, ``repr``, args, content
    attributes, other instance attributes (``__dict__``, such as an HTTP error's
    message or a JSON error's notes) or rendering (:func:`render_without_frames`),
    nor in any ``__cause__`` or ``__context__`` it chains to, suppressed or not."""
    seen: set[int] = set()
    pending: list[BaseException | None] = [error]
    while pending:
        current = pending.pop()
        if current is None or id(current) in seen:
            continue
        seen.add(id(current))
        texts = [
            str(current),
            repr(current),
            repr(current.args),
            render_without_frames(current),
        ]
        if dataclasses.is_dataclass(current):
            texts += [repr(getattr(current, f.name)) for f in dataclasses.fields(current)]
        values = [getattr(current, name, None) for name in _CONTENT_ATTRIBUTES]
        values += getattr(current, "__dict__", {}).values()
        # ``str`` as well: an HTTP error's headers print their text only so.
        texts += [text for value in values for text in (repr(value), str(value))]
        for text in texts:
            assert_no_markers(text, *markers)
        pending += [current.__cause__, current.__context__]


def assert_unchained_refusal(
    error: BaseException,
    expected: IbkrFaError,
    text: str,
    *,
    markers: tuple[str, ...],
    crash: type[BaseException] | None = None,
) -> None:
    """``error`` is ``expected`` (class and every field) and prints ``text``; no
    marker is anywhere in its exception graph, and it chains no exception, or
    with ``crash`` only the stand-in for it (:func:`assert_replaced_unchained`).

    The checks run in that order, so a chained error that names a marker fails
    on the marker before the structural check.
    """
    assert type(error) is type(expected)
    assert error == expected
    assert str(error) == text
    assert_replaced_unchained(error, crash, markers=markers)


def assert_replaced_unchained(
    error: BaseException,
    crash: type[BaseException] | None = None,
    *,
    markers: tuple[str, ...] = (),
) -> None:
    """``error`` replaced another exception and keeps none of it (#1211).

    No marker is anywhere in its exception graph (:func:`assert_prints_no_marker`).
    It has no context, and no cause unless the replaced exception or active
    handler was or chained a crash of class ``crash``: then its cause is a
    :class:`ReplacedCrashError` naming only that class and a line, with no cause
    or context of its own.
    """
    if markers:
        assert_prints_no_marker(error, markers=markers)
    assert error.__context__ is None
    stand_in = error.__cause__
    if crash is None:
        assert stand_in is None
        return
    assert type(stand_in) is ReplacedCrashError
    assert re.fullmatch(
        rf"{crash.__name__} at (ibkr_fa/[\w/]+\.py:\d+|an unknown line)", str(stand_in)
    ), str(stand_in)
    assert stand_in.__cause__ is None
    assert stand_in.__context__ is None


def assert_plain_refusal(
    error: BaseException,
    expected_type: type[Exception],
    text: str,
    *,
    crash: type[BaseException] | None,
    markers: tuple[str, ...],
) -> None:
    """``error`` is exactly ``expected_type(text)``, a plain exception with no
    other state, and it replaced the error it caught: no marker anywhere in its
    exception graph, no context, and a stand-in cause only for a ``crash``
    (:func:`assert_replaced_unchained`)."""
    assert type(error) is expected_type
    assert error.args == (text,)
    assert str(error) == text
    assert vars(error) == {}
    assert_replaced_unchained(error, crash, markers=markers)


def assert_fixed_refusal(
    error: BaseException,
    expected: type[BaseException],
    message: str,
    capsys: pytest.CaptureFixture[str],
    *,
    markers: tuple[str, ...] = (),
) -> None:
    """``error`` is exactly ``expected`` with the complete fixed ``message``, no
    cause or context and ``__suppress_context__`` false; nothing was printed;
    and no marker is in its rendering (:func:`render_without_frames`) or its
    ``repr``."""
    assert type(error) is expected
    assert str(error) == message
    assert error.__cause__ is None
    assert error.__context__ is None
    assert error.__suppress_context__ is False
    rendered = render_without_frames(error) + repr(error)
    assert capsys.readouterr() == ("", "")
    for marker in markers:
        assert marker not in rendered, marker


def chained_crash(
    exc_type: type[Exception] = RuntimeError, message: str = MESSAGE
) -> Callable[..., NoReturn]:
    """A callable that raises ``exc_type(message)`` from a cause holding
    :data:`CAUSE`, while handling a context holding :data:`CONTEXT`."""

    def crash(*_args: object, **_kwargs: object) -> NoReturn:
        try:
            raise_context()
        except ValueError:
            raise exc_type(message) from KeyError(CAUSE)

    return crash


def raise_context() -> NoReturn:
    """Raise the context a chained crash is raised while handling."""
    raise ValueError(CONTEXT)
