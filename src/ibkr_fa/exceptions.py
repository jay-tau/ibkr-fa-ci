"""Exception hierarchy for ibkr-fa.

All exceptions raised by ibkr-fa code descend from ``IbkrFaError``. This
gives callers (CLI, future library users) one base class to catch and
distinguishes our errors from arbitrary stdlib exceptions in tracebacks.

Concrete subclasses carry structured context (file row, config field
path) so the CLI can render actionable error messages without parsing
strings.
"""

from __future__ import annotations

import errno
import sys
import traceback
from dataclasses import dataclass
from datetime import date
from decimal import DecimalException
from pathlib import Path
from typing import Final, NoReturn

_PACKAGE_DIR: Final[Path] = Path(__file__).parent


class IbkrFaError(Exception):
    """Base class for every error raised by the ``ibkr_fa`` package.

    Subclasses are :func:`dataclass`-based and carry structured context.
    Catch this class to handle any expected ibkr-fa failure; let
    everything else propagate as a programming bug.
    """


def errno_name(exc: OSError) -> str:
    """``exc``'s errno as its symbolic name, such as ``EACCES``, or "unknown error".

    Never its ``strerror``, ``str()`` or file names: code can raise ``OSError``
    with any text, and its file names may not be paths the user typed.
    """
    code = exc.errno
    return (errno.errorcode.get(code) if isinstance(code, int) else None) or "unknown error"


def crash_site(exc: BaseException) -> str:
    """``exc``'s class and its innermost ``ibkr_fa`` frame as a package-relative
    file and line, such as ``KeyError at ibkr_fa/cli.py:123``; never its message."""
    site = "an unknown line"
    for frame, line in traceback.walk_tb(exc.__traceback__):
        path = Path(frame.f_code.co_filename)
        if path.is_relative_to(_PACKAGE_DIR):
            site = f"{path.relative_to(_PACKAGE_DIR.parent).as_posix()}:{line}"
    return f"{type(exc).__name__} at {site}"


def relayed_failure(exc: BaseException, operation: str, path: Path | None = None) -> str:
    """The text a relay prints for ``exc`` (#1044).

    An :class:`IbkrFaError`'s own text; an ``OSError`` as ``operation`` (fixed
    text), ``path`` and its errno name; anything else as ``internal error (…)``
    naming its class and innermost ``ibkr_fa`` source line
    (:func:`crash_site`). Never another exception's text, nor the text of its
    cause or context. ``path`` is one the user typed, or a fixed name joined to
    one.
    """
    if isinstance(exc, IbkrFaError):
        return str(exc)
    if isinstance(exc, OSError):
        where = "" if path is None else f" {path}"
        return f"{operation}{where} ({errno_name(exc)})"
    return f"internal error ({crash_site(exc)})"


# What every command refuses with exit 4 (#832): a documented domain error, a
# filesystem error, or a value too large or too precise to compute (a
# ``decimal`` exception from arithmetic or rounding on a parsed value). Anything
# else is a crash.
DOMAIN_ERRORS: Final = (IbkrFaError, OSError, DecimalException)


class ReplacedCrashError(Exception):
    """Stands in for a crash that an ibkr-fa error replaced (see
    :func:`raise_replacing`): its text is the crash's class and innermost
    ``ibkr_fa`` line (:func:`crash_site`), never its message, cause or context.

    Never raised. It is not one of :data:`DOMAIN_ERRORS`, so the CLI still
    treats the error chained to it as one holding a crash.
    """


def first_crash(*links: BaseException | None) -> BaseException | None:
    """The first of ``links``, or of every ``__cause__`` and ``__context__``
    they reach, suppressed or not, that is not one of :data:`DOMAIN_ERRORS`;
    ``None`` if there is none."""
    seen: set[int] = set()
    pending = list(reversed(links))
    while pending:
        link = pending.pop()
        if link is None or id(link) in seen:
            continue
        seen.add(id(link))
        if not isinstance(link, DOMAIN_ERRORS):
            return link
        pending.extend((link.__context__, link.__cause__))
    return None


def raise_replacing(error: IbkrFaError, replaced: BaseException) -> NoReturn:
    """Raise ``error`` in place of ``replaced``, chained to nothing that could
    hold input.

    ``raise … from None`` only hides the exception being handled: it stays
    attached as ``__context__``. A raise in a ``with`` block's exit, or under a
    caller's ``except``, attaches the block's or the caller's exception the same
    way. So ``error`` leaves with no context, and its only link is a
    :class:`ReplacedCrashError` cause when ``replaced`` or the active handler
    is or chains a crash (:func:`first_crash`), so the CLI keeps its crash
    classification.
    """
    crash = first_crash(replaced, sys.exception())
    if crash is not None and not isinstance(crash, ReplacedCrashError):
        crash = ReplacedCrashError(crash_site(crash))
    try:
        raise error from crash
    finally:
        error.__context__ = None


@dataclass(frozen=True, slots=True)
class ParseError(IbkrFaError):
    """Raised when a Flex Query CSV row cannot be parsed.

    Attributes carry enough context to point the user at the exact row.

    Args:
        section: The Flex CSV section name where the error occurred
            (e.g., ``"Open Positions"``).
        row_index: Zero-based index of the row within the section.
        column: Column name (header) of the offending field, or
            :data:`None` if the error spans the whole row.
        message: Human-readable description of what's wrong.
        raw_value: Production callers pass ``""``. ``str()`` never prints
            this field, so a value here can't reach the message.
    """

    section: str
    row_index: int
    column: str | None
    message: str
    raw_value: str

    def __str__(self) -> str:
        loc = f"row {self.row_index}"
        if self.column is not None:
            loc = f"{loc}, column {self.column!r}"
        return f"ParseError in section {self.section!r} at {loc}: {self.message}"


@dataclass(frozen=True, slots=True)
class LotMatchingError(IbkrFaError):
    """Raised when trade executions cannot be FIFO-matched into lots.

    Args:
        row_index: Zero-based index of the Trades row the refusal is about.
        message: Human-readable description of what's wrong.
    """

    row_index: int
    message: str

    def __str__(self) -> str:
        return f"LotMatchingError in section 'Trades' at row {self.row_index}: {self.message}"


@dataclass(frozen=True, slots=True)
class MissingFxRateError(IbkrFaError):
    """Raised when an FX rate cannot be sourced for the requested date.

    Args:
        currency: ISO 4217 currency code (e.g., ``"USD"``).
        last_available: The most recent date before the requested date
            for which the bundled archive has a rate, a public archive
            date, or :data:`None` if it has none (e.g., the requested date
            predates the entire archive).
        max_carry_forward_days: The carry-forward limit configured on
            the FX policy that raised this error.
        policy_kind: Optional short discriminator describing the FX
            policy semantic that failed (e.g., ``"Rule 115(1), Explanation (2)(e)
            (preceding-month-end for a dividend)"``), or the fixed step for a
            policy that serves several event kinds. When set, it is rendered
            into :meth:`__str__` so users can distinguish which policy raised
            the error. Defaults to :data:`None` for backward compatibility
            with existing callers.

    :meth:`__str__` prints the currency, the policy and the latest available
    rate's date. The error keeps no requested or event date, nor its gap to
    that rate: the event date is input (#1047, #1368).
    """

    currency: str
    last_available: date | None
    max_carry_forward_days: int
    policy_kind: str | None = None

    def __str__(self) -> str:
        if self.last_available is None:
            avail = "no prior rate available in the bundled archive"
        else:
            avail = (
                f"latest available prior rate is {self.last_available.isoformat()}, "
                f"beyond the carry-forward limit of {self.max_carry_forward_days} days"
            )
        discriminator = f" [{self.policy_kind}]" if self.policy_kind else ""
        return (
            f"MissingFxRateError: no {self.currency} rate available"
            f"{discriminator}; {avail}. "
            "The bundled TTBR archive may need updating; see "
            "https://github.com/jay-tau/ibkr-fa for the latest snapshot."
        )


@dataclass(frozen=True, slots=True)
class ConfigError(IbkrFaError):
    """Raised when ``user.toml`` is missing required fields or fails validation.

    Args:
        field: Dotted path to the offending field
            (e.g., ``"account.opening_date"``).
        message: Human-readable description of what's wrong.
    """

    field: str
    message: str

    def __str__(self) -> str:
        return f"ConfigError at field {self.field!r}: {self.message}"


@dataclass(frozen=True, slots=True)
class PathRefusalError(IbkrFaError):
    """Raised when a path the user gave can't be used, such as a missing input.

    Args:
        operation: Fixed text saying what is wrong with ``path``.
        path: The path as the user gave it.
    """

    operation: str
    path: Path

    def __str__(self) -> str:
        return f"{self.operation}: {self.path}"


@dataclass(frozen=True, slots=True)
class InvariantError(IbkrFaError):
    """Raised when a runtime invariant is violated.

    Distinct from :class:`ParseError` (which is for **expected** user-
    facing input problems carrying section/row/column context) and
    :class:`ConfigError` (for ``user.toml`` problems). An
    ``InvariantError`` represents a condition the code believes
    should never hold — if it fires, either there's a parser /
    compute bug, or the input shape violated an assumption we didn't
    catch at the boundary.

    Raised via :func:`ibkr_fa.invariants.require`, and built by
    :func:`ibkr_fa.invariants.invariant_error`, which keeps no value but a
    position. Caught by the CLI's :class:`IbkrFaError` mapper -> exit 4,
    same as other documented errors.

    Args:
        what: Short, human-readable description of the violated
            invariant (e.g., ``"OpenLot.quantity must match Side"``).
        context: The context keys in call order, each paired with the
            ``int`` value of a position key (such as ``line``) or
            ``None``. No other value is kept, because it can be input.
            Stored as a tuple (not a dict) so the dataclass can stay
            frozen and hashable. :meth:`__str__` prints each key, and
            ``key=value`` for a kept position.
    """

    what: str
    context: tuple[tuple[str, int | None], ...]

    def __str__(self) -> str:
        if not self.context:
            return f"InvariantError: {self.what}"
        rendered = ", ".join(k if v is None else f"{k}={v}" for k, v in self.context)
        return f"InvariantError: {self.what} (context keys: {rendered})"
