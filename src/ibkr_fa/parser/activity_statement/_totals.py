"""Check the printed totals of one Activity Statement income block (#908).

IBKR prints the Dividends, Withholding Tax and Interest sections' totals
as ``Data`` rows, so the splitter hands them to the section parsers
along with the detail rows. Within one header-led block:

* Detail rows come in runs, and each run ends with a per-currency
  ``Total`` row: Currency ``Total``, the run's sum in Amount, and every
  other cell empty. The run's rows share one currency, and their sum
  must equal the ``Total`` exactly.
* After the last ``Total`` the block may end with one
  ``Total <Section> in <CCY>`` summary row: Currency ``<CCY>``, exactly
  that Description, an Amount, and every other cell empty. When every
  run is in ``<CCY>`` the summary must equal the sum of the ``Total``
  rows. When a run is in another currency that sum needs FX, so the
  summary's value is accepted unchecked; each run is still checked
  against its own ``Total``.

Anything else raises :class:`~ibkr_fa.exceptions.ParseError` naming the
file line: a mismatch, a run without a ``Total``, a ``Total`` with no
rows above it, a malformed total or summary, a second summary, any row
after the summary, or a sum that can't be represented exactly within
the decimal range or available memory. So a row lost
when the file was edited or cut short can't shrink the income every
schedule files. These errors carry the file line as
:attr:`~ibkr_fa.exceptions.ParseError.row_index`, as the splitter's do,
because a total's check spans rows; the section parsers' per-row errors
keep the row's position in its block. No refusal prints a cell, an
amount or a sum: a shifted row can put the holder's name or account
number in any cell (#1027, #1040). A refusal names the lines it
checked and the rule broken.

Its sibling, :mod:`~ibkr_fa.parser.activity_statement._trades_totals`,
checks the Trades section's ``SubTotal`` and ``Total`` rows inside the
splitter (#1090), sharing this module's exact addition and re-export
sentence.
"""

from __future__ import annotations

import operator
import re
from collections.abc import Mapping
from decimal import Decimal, InvalidOperation
from functools import partial
from typing import TYPE_CHECKING

from ibkr_fa.exceptions import ParseError
from ibkr_fa.invariants import require
from ibkr_fa.number_checks import NumberKind, number_limit_breach
from ibkr_fa.parser._exact import exactly

if TYPE_CHECKING:
    # Only annotated here: the splitter imports this module's sibling.
    from ibkr_fa.parser.activity_statement.multi_section import Section

_RE_EXPORT = "The file may have been edited or cut short; re-export the Activity Statement."


def add_exactly(
    augend: Decimal, addend: Decimal, *, section: str, line: int, column: str
) -> Decimal:
    """Add exactly: a sum rounded to the context's precision could hide a lost row.

    Refuse decimal range or allocation failures rather than round the sum,
    naming ``line`` and ``column`` in ``section``
    (:func:`~ibkr_fa.parser._exact.exactly`).
    """
    return exactly(
        operator.add,
        augend,
        addend,
        refusal=partial(_sum_refusal, section=section, line=line, column=column),
    )


def _sum_refusal(out_of_memory: bool, *, section: str, line: int, column: str) -> ParseError:
    return ParseError(
        section=section,
        row_index=line,
        column=column,
        message=(
            f"there isn't enough memory to add the amounts up to line "
            f"{line} exactly, so the printed total can't be checked."
            if out_of_memory
            else f"the amounts up to line {line} are too large or too small "
            f"to add up exactly, so the printed total can't be checked. "
            f"{_RE_EXPORT}"
        ),
        raw_value="",
    )


class PrintedTotals:
    """Check one block's printed totals against its rows, in file order.

    Call :meth:`before_row` before the parser's per-row checks, then
    :meth:`footer` after its required-column check. When it returns
    ``False`` the row is a detail row: pass its currency and parsed
    amount to :meth:`detail`. Call :meth:`finish` after the last row.
    """

    def __init__(self, section: str, block: Section) -> None:
        require(
            len(block.row_lines) == len(block.rows),
            what="PrintedTotals pre: every row in the block needs its file line",
            section=section,
            rows=len(block.rows),
            row_lines=len(block.row_lines),
        )
        self._section = section
        self._block = block
        self._summary_prefix = f"total {section.casefold()} in"
        self._summary_label = re.compile(rf"Total {re.escape(section)} in ([A-Z]{{3}})")
        self._run_currencies: set[str] = set()
        self._run_sum = Decimal(0)
        self._run_lines: list[int] = []
        # Each ``Total`` row's run currency, amount and file line.
        self._totals: list[tuple[str, Decimal, int]] = []
        self._summary_line: int | None = None

    def footer(self, row: Mapping[str, str], line: int) -> bool:
        """Check ``row`` if it is a total or summary row; return whether it was.

        A Currency cell starting with ``Total`` marks a per-currency
        total, and a Description starting with ``Total <Section> in``
        marks a summary (both ignoring case and surrounding spaces).
        Either is then checked in full, so a near-miss refuses rather
        than passing as a detail row.
        """
        if row["Currency"].strip().casefold().startswith("total"):
            self._check_total(row, line)
            return True
        if row["Description"].strip().casefold().startswith(self._summary_prefix):
            self._check_summary(row, line)
            return True
        return False

    def detail(self, currency: str, amount: Decimal, line: int) -> None:
        """Add a detail row on ``line`` to the current run."""
        self._run_currencies.add(currency)
        self._run_sum = add_exactly(
            self._run_sum, amount, section=self._section, line=line, column="Amount"
        )
        self._run_lines.append(line)

    def finish(self) -> None:
        """Refuse a block whose last rows have no ``Total`` after them."""
        if self._run_lines:
            raise self._error(
                self._run_lines[-1],
                None,
                f"the block whose header is on line {self._block.header_line} "
                f"ends without a 'Total' row after its last row (line "
                f"{self._run_lines[-1]}), so its rows can't be checked against "
                f"IBKR's total. {_RE_EXPORT}",
            )

    def _check_total(self, row: Mapping[str, str], line: int) -> None:
        if row["Currency"] != "Total":
            raise self._error(
                line,
                "Currency",
                f"line {line} looks like a total row, but its Currency cell "
                f"isn't exactly 'Total'. {_RE_EXPORT}",
            )
        self._require_empty_except(row, line, ("Currency", "Amount"), "'Total' row")
        total = self._amount(row, line, "'Total' row")
        if not self._run_lines:
            raise self._error(
                line,
                None,
                f"the 'Total' row on line {line} has no rows above it since "
                f"the previous 'Total' row or the block's header. {_RE_EXPORT}",
            )
        rows = self._run_span()
        if len(self._run_currencies) != 1:
            raise self._error(
                line,
                None,
                f"the rows on {rows}, which the 'Total' row on "
                f"line {line} adds up, are in more than one currency. IBKR "
                f"prints one 'Total' per currency. {_RE_EXPORT}",
            )
        if self._run_sum != total:
            raise self._error(
                line,
                "Amount",
                f"the 'Total' row on line {line} doesn't equal the sum of the "
                f"rows on {rows}. A row may be missing or changed. {_RE_EXPORT}",
            )
        [currency] = self._run_currencies
        self._totals.append((currency, total, line))
        self._run_currencies = set()
        self._run_sum = Decimal(0)
        self._run_lines = []

    def _check_summary(self, row: Mapping[str, str], line: int) -> None:
        label = self._summary_label.fullmatch(row["Description"])
        if label is None:
            raise self._error(
                line,
                "Description",
                f"line {line} looks like a summary row, but its Description "
                f"isn't 'Total {self._section} in <CCY>' with a three-letter "
                f"currency code. {_RE_EXPORT}",
            )
        currency = label.group(1)
        if row["Currency"] != currency:
            raise self._error(
                line,
                "Currency",
                f"the summary row on line {line} has a Currency cell that "
                f"differs from the currency in its Description. {_RE_EXPORT}",
            )
        self._require_empty_except(row, line, ("Currency", "Description", "Amount"), "summary row")
        summary = self._amount(row, line, "summary row")
        if self._run_lines:
            raise self._error(
                line,
                None,
                f"the summary row on line {line} comes before a 'Total' row "
                f"for the rows on {self._run_span()}. {_RE_EXPORT}",
            )
        if not self._totals:
            raise self._error(
                line,
                None,
                f"the summary row on line {line} has no 'Total' row above it. {_RE_EXPORT}",
            )
        if all(total_currency == currency for total_currency, _, _ in self._totals):
            totals_sum = Decimal(0)
            for _, total, _ in self._totals:
                totals_sum = add_exactly(
                    totals_sum, total, section=self._section, line=line, column="Amount"
                )
            if totals_sum != summary:
                total_lines = ", ".join(str(total_line) for _, _, total_line in self._totals)
                raise self._error(
                    line,
                    "Amount",
                    f"the summary row on line {line} doesn't equal the sum of the "
                    f"'Total' rows on line(s) {total_lines}. {_RE_EXPORT}",
                )
        self._summary_line = line

    def _run_span(self) -> str:
        first, last = self._run_lines[0], self._run_lines[-1]
        return f"line {first}" if first == last else f"lines {first}-{last}"

    def before_row(self, line: int) -> None:
        """Refuse a row after the summary before any per-row validation."""
        if self._summary_line is not None:
            raise self._error(
                line,
                None,
                f"line {line} comes after the summary row on line "
                f"{self._summary_line}, which must be the block's last row. "
                f"{_RE_EXPORT}",
            )

    def _require_empty_except(
        self,
        row: Mapping[str, str],
        line: int,
        kept: tuple[str, ...],
        what: str,
    ) -> None:
        for column in self._block.columns:
            if column not in row:
                raise self._error(
                    line,
                    column,
                    f"the {what} on line {line} is missing its {column!r} cell.",
                )
            if column not in kept and row[column] != "":
                raise self._error(
                    line,
                    column,
                    f"the {what} on line {line} has a value in its {column!r} "
                    f"cell, which IBKR leaves empty. {_RE_EXPORT}",
                )

    def _amount(self, row: Mapping[str, str], line: int, what: str) -> Decimal:
        try:
            value = Decimal(row["Amount"])
        except InvalidOperation:
            value = Decimal("NaN")
        if not value.is_finite():
            raise self._error(
                line,
                "Amount",
                f"the {what} on line {line} needs a finite decimal Amount.",
            )
        breach = number_limit_breach(value, NumberKind.MONEY)
        if breach is not None:
            raise self._error(line, "Amount", breach)
        return value

    def _error(self, line: int, column: str | None, message: str) -> ParseError:
        return ParseError(
            section=self._section,
            row_index=line,
            column=column,
            message=message,
            raw_value="",
        )
