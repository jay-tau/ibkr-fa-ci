"""Load the vendored SBI TTBR CSV into an in-memory ``date -> Decimal`` map.

This module owns the parse step. The :class:`~ibkr_fa.fx.policy.FxPolicy`
implementations consume the parsed map via :func:`load_ttbr` and apply
their own carry-forward / preceding-month / preceding-day rules on top.

Vendor-time transformations applied to the upstream CSV (documented in
``src/ibkr_fa/reference/sbi_ttbr_*.csv.source``):

1. Dedup by calendar date — keep the LAST published rate of the day.
2. Sort by date ascending.

This module then applies the runtime rules:

3. Check every row, zero-rate rows included (see :func:`parse_ttbr_row`).
4. Skip rows where the parsed ``TT BUY`` value equals 0 (the upstream
   uses both the string ``"0"`` and ``"0.00"`` for unpublished days).
   Use numeric comparison; do NOT match on the string literal.

Load-time integrity invariants (see :func:`load_ttbr`) verify the file
and the returned archive are well-formed: strict CSV, the exact
:data:`TTBR_HEADER`, ten whole, printable cells per row with a real
``DATE`` and a plain-number ``TT BUY``, strictly monotonic dates, no
duplicates, every rate within the SANE_RATE_BOUNDS window. A breach
raises :class:`~ibkr_fa.exceptions.InvariantError`.
"""

from __future__ import annotations

import csv
import io
import re
from datetime import date, datetime
from decimal import Decimal
from functools import cache
from importlib import resources
from typing import TYPE_CHECKING, Final

from ibkr_fa.exceptions import ReplacedCrashError, crash_site
from ibkr_fa.invariants import invariant_error, require
from ibkr_fa.number_checks import NumberKind, number_limit_breach

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence


_REFERENCE_PACKAGE = "ibkr_fa.reference"
_TTBR_CSV_TEMPLATE = "sbi_ttbr_{currency_lower}_inr.csv"
_ZERO = Decimal("0")

# SBI's column names, in order. ``scripts/refresh_ttbr.py`` imports this
# too, so the refresh and the loader share the header and cell rules.
TTBR_HEADER: Final[tuple[str, ...]] = (
    "DATE",
    "PDF FILE",
    "TT BUY",
    "TT SELL",
    "BILL BUY",
    "BILL SELL",
    "FOREX TRAVEL CARD BUY",
    "FOREX TRAVEL CARD SELL",
    "CN BUY",
    "CN SELL",
)
_DATE_INDEX: Final[int] = TTBR_HEADER.index("DATE")
_RATE_INDEX: Final[int] = TTBR_HEADER.index("TT BUY")
_DATE_SHAPE: Final = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2} [0-9]{2}:[0-9]{2}")
_RATE_SHAPE: Final = re.compile(r"[0-9]+(\.[0-9]+)?")

# Sane TTBR bounds for SBI's USD/INR archive. Historical USD/INR has
# ranged from ~40 (early 2000s) to ~90 (recent). Setting the upper
# bound at 200 gives wide safety margin while still catching gross
# data-integrity problems (e.g., a misparsed cell with extra digits).
# When multi-currency support lands (tracker #53), the bounds widen
# and may need to be per-currency.
SANE_RATE_LOWER: Final[Decimal] = Decimal("1")
SANE_RATE_UPPER: Final[Decimal] = Decimal("200")


def parse_ttbr_row(cells: Sequence[str]) -> tuple[date, Decimal]:
    """Check one data row and return its calendar date and ``TT BUY`` rate.

    The row must have one cell per :data:`TTBR_HEADER` column, and every
    cell must be printable. ``DATE`` must be a real ``YYYY-MM-DD HH:MM``
    and ``TT BUY`` plain digits with an optional fraction, within an FX
    rate's read-time digit limits (:mod:`ibkr_fa.number_checks`). The other
    columns aren't parsed. A zero rate is returned like any other; the
    caller decides what it means.

    ``scripts/refresh_ttbr.py`` calls this too, sharing the loader's
    whole-cell checks before its dedup.

    Raises:
        ValueError: Naming the first rule the row breaks, in fixed text that
            quotes no cell.
    """
    if len(cells) != len(TTBR_HEADER):
        msg = f"row must have exactly {len(TTBR_HEADER)} cells"
        raise ValueError(msg)
    for column, cell in zip(TTBR_HEADER, cells, strict=True):
        if not cell.isprintable():
            msg = f"{column} cell has a non-printable character"
            raise ValueError(msg)
    raw_date = cells[_DATE_INDEX]
    raw_rate = cells[_RATE_INDEX]
    if _DATE_SHAPE.fullmatch(raw_date) is None:
        msg = "DATE is not YYYY-MM-DD HH:MM"
        raise ValueError(msg)
    try:
        # ``strptime``, not ``fromisoformat``: Python 3.14's
        # ``fromisoformat`` reads ``24:00`` as midnight of the next day.
        stamp = datetime.strptime(raw_date, "%Y-%m-%d %H:%M")
    except ValueError:
        msg = "DATE is not a real date and time"
        raise ValueError(msg) from None
    if _RATE_SHAPE.fullmatch(raw_rate) is None:
        msg = "TT BUY is not plain digits with an optional fraction"
        raise ValueError(msg)
    rate = Decimal(raw_rate)
    breach = number_limit_breach(rate, NumberKind.FX_RATE)
    if breach is not None:
        raise ValueError(breach)
    return stamp.date(), rate


def _read_pairs(text: str, *, csv_name: str) -> list[tuple[date, Decimal]]:
    """Return the non-zero ``(date, rate)`` pairs of a TTBR CSV, in file order.

    Parses strictly, then checks the header and every row, zero-rate rows
    included, before it skips the zero rates.

    Raises:
        InvariantError: On malformed CSV, a header other than
            :data:`TTBR_HEADER`, or a row :func:`parse_ttbr_row` refuses. A
            row refusal keeps the row error's fixed text but not the error,
            whose suppressed context can quote the DATE cell: it has no context
            of its own, and its cause is a content-free
            :class:`~ibkr_fa.exceptions.ReplacedCrashError` (#1225).
    """
    reader = csv.reader(io.StringIO(text, newline=""), strict=True)
    try:
        records = [(reader.line_num, cells) for cells in reader]
    except csv.Error as exc:
        raise invariant_error(
            what=(
                f"{csv_name}: malformed CSV (text after a closing quote, a quote left open "
                "or a cell over the csv field size limit)"
            ),
            line=reader.line_num,
        ) from exc
    header = tuple(records[0][1]) if records else ()
    require(
        header == TTBR_HEADER,
        what=f"{csv_name}: header must be exactly {TTBR_HEADER!r}",
        header=header,
    )
    pairs: list[tuple[date, Decimal]] = []
    for line, cells in records[1:]:
        try:
            day, rate = parse_ttbr_row(cells)
        except ValueError as exc:
            # The row error's context can hold the DATE cell (``strptime``'s
            # error). The refusal is frozen, so ``raise_replacing`` can't clear
            # its context: keep the fixed text and a content-free stand-in, and
            # raise after the handler.
            problem, stand_in = str(exc), ReplacedCrashError(crash_site(exc))
        else:
            if rate != _ZERO:
                pairs.append((day, rate))
            continue
        raise invariant_error(what=f"{csv_name}: {problem}", line=line) from stand_in
    return pairs


@cache
def load_ttbr(currency: str) -> Mapping[date, Decimal]:
    """Return ``{event_date: ttbr_rate}`` for the requested ``currency``.

    The mapping is sorted by date ascending (insertion order is
    preserved by :class:`dict` since Python 3.7) and contains only
    dates where SBI actually published a non-zero TT-Buying rate.

    The result is memoised — first call parses the CSV; subsequent
    calls return the same mapping. Safe because the CSV is bundled
    immutable data.

    Load-time invariants enforced on the file (issue #941):

    * The file is strict CSV (:class:`csv.Error` on, e.g., a stray quote
      in a cell or a quote left open at the end of the file).
    * The first row is exactly :data:`TTBR_HEADER`.
    * Every later row passes :func:`parse_ttbr_row`, zero-rate rows
      included: ten printable cells, a real ``YYYY-MM-DD HH:MM`` date
      and a plain-number ``TT BUY``. A blank line is a row with no
      cells, so it refuses too.

    Load-time invariants enforced on the parsed archive (see PR #123):

    * Every rate is within ``(SANE_RATE_LOWER, SANE_RATE_UPPER]`` — a
      USD/INR rate outside this range signals a parser bug or a
      corrupted upstream CSV.
    * Dates are strictly monotonically increasing (which also implies
      no duplicates) — the dedup-by-date vendor-time step should
      have already ensured this; verifying at runtime catches any
      future vendor-time mistake before it propagates into
      downstream date lookups.

    Args:
        currency: ISO 4217 code (uppercase). v1 supports only
            ``"USD"``; passing anything else raises
            :class:`FileNotFoundError` from :mod:`importlib.resources`.

    Returns:
        Mapping from calendar date to the TT-Buying rate as a
        :class:`~decimal.Decimal`. Read-only intent (the underlying
        type is :class:`dict` for performance, but callers should not
        mutate).

    Raises:
        FileNotFoundError: If the bundled CSV for ``currency`` does not
            exist (i.e., currency not yet vendored).
        InvariantError: If the file or the parsed archive violates any
            of the load-time integrity invariants (malformed CSV, wrong
            header, a refused row, rate outside sane bounds,
            non-monotonic dates, or duplicates). CSV, header and row
            errors name the file; malformed CSV and refused data rows
            also have line context.
    """
    csv_name = _TTBR_CSV_TEMPLATE.format(currency_lower=currency.lower())
    csv_resource = resources.files(_REFERENCE_PACKAGE).joinpath(csv_name)
    # ``Traversable.open`` doesn't accept ``newline``; ``as_file`` materialises
    # a real ``pathlib.Path`` so we can use the standard csv module conventions.
    with (
        resources.as_file(csv_resource) as csv_path,
        csv_path.open(newline="", encoding="utf-8") as f,
    ):
        text = f.read()
    # Parse after the ``with``: ``as_file`` is a ``@contextmanager``, and
    # a frozen ``InvariantError`` raised through one becomes a
    # ``TypeError``. Keep a list, not a dict, so the duplicate-date
    # check below can actually see duplicates. Codex P2 on PR #123:
    # going straight to ``dict(...)`` silently dropped duplicate keys
    # (later wins), letting exactly the vendor-time-dedup corruption
    # this load-time check is meant to detect slip through unnoticed.
    pairs = _read_pairs(text, csv_name=csv_name)

    # Load-time integrity checks. Every entry in the bundled CSV is
    # subject to the same invariants; verifying once at load time
    # (cached forever) is cheaper than per-lookup.
    prior_date: date | None = None
    for d, r in pairs:
        require(
            SANE_RATE_LOWER < r <= SANE_RATE_UPPER,
            what=(
                f"load_ttbr({currency!r}): rate must be in ({SANE_RATE_LOWER}, {SANE_RATE_UPPER}]"
            ),
            date=d,
            rate=r,
        )
        if prior_date is not None:
            require(
                d > prior_date,
                what=(
                    f"load_ttbr({currency!r}): dates must be strictly "
                    "monotonically increasing (no duplicates, no reverse order)"
                ),
                prior=prior_date,
                current=d,
            )
        prior_date = d

    return dict(pairs)
