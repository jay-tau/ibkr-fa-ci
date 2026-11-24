"""Parse the IBKR Activity Statement ``Withholding Tax`` section
into :class:`~ibkr_fa.models.WithholdingTax`.

The Withholding Tax section emits one row per per-payment WHT debit
that pairs with a corresponding ``Dividends`` row (sister parser at
:mod:`ibkr_fa.parser.activity_statement.dividends`). Schema (columns
in order, IBKR ~2025-2026, verified against a private FY 25-26 statement)::

  DataDiscriminator, Currency, Date, Description, Amount, Code

Example data row (one logical CSV row, wrapped here for readability)::

  Withholding Tax,Data,USD,18-09-2025,
    "XMPL(US99999A1025) Cash Dividend USD 0.25 per Share - US Tax",
    -0.81,

IBKR prints the section's totals as ``Data`` rows too: a per-currency
``Total`` row (Currency ``"Total"``) after each run of rows and, in
multi-currency accounts, a ``"Total Withholding Tax in <CCY>"`` summary
at the end of the block. As in the sister Dividends parser, every such
row is validated and its sum checked when no FX is needed. A mismatch refuses
(#908; the rules are in :mod:`ibkr_fa.parser.activity_statement._totals`).
The splitter refuses a ``SubTotal``/``Total``-type row in this section.

IBKR leaves an empty section out of the export, and the dispatcher reads
a missing Withholding Tax section as no rows. The commands that file
income accept that only when the statement's ``Change in NAV``, if
printed, doesn't contradict it (#1279; see
:mod:`ibkr_fa.parser.activity_statement.change_in_nav_income`).

**Sign convention**: IBKR emits withholding tax as a NEGATIVE
amount (money leaving the cash balance). When IBKR reverses a
withholding (e.g., treaty-rate correction, refund), the same
section carries a POSITIVE amount. The parser accepts BOTH signs
and rejects only zero — the compute layer is responsible for
pairing reversals to their originating WHT row (per Paasa
methodology: "Reversals (positive amounts) excluded from tax paid
totals"). See #138's "Out of scope" note for the parser/compute
split, and :class:`~ibkr_fa.models.WithholdingTax`'s docstring for
the field-level contract.

The ``Code`` cell is blank, or ``Re`` on a reversal (a positive
amount). The parser reads a row by its amount's sign, so any other
code refuses (#913).

A description ending exactly ``" - XX Tax"`` (two uppercase letters) states
the country that took the tax; the parser keeps it as
:attr:`~ibkr_fa.models.WithholdingTax.tax_country_iso2`, with the row's
file line. Any other ending, such as ``- nl Tax`` or ``- USA Tax``, states
none. After the mapped Indian-ADR skip under ``underlying-issuer`` routing,
foreign-tax netting refuses a remaining row whose stated country differs
from the ISIN's (#1282; see :mod:`ibkr_fa.compute.foreign_tax`).

The ``Description`` column reuses the same ``SYMBOL(ISIN)`` prefix
as the Dividends section — the regex and extraction helper are
shared via
:mod:`ibkr_fa.parser.activity_statement._description` so both
parsers stay in sync if IBKR changes the symbol charset (e.g., the
``RDS-A`` / ``BF-B`` / ``BRK-B`` widening from PR #137 Tier-3).

This parser populates Schedule OS net dividends (#31), Schedule
FSI per-country foreign tax (#35), Schedule TR (#36), and Form 67
FTC (#37). The compute layer that drives those tables is tracked
under #33 — out of scope for this module.
"""

from __future__ import annotations

import datetime as _dt
import re
from decimal import Decimal
from typing import Final

from ibkr_fa.exceptions import ParseError
from ibkr_fa.invariants import require
from ibkr_fa.models import StatementPeriod, WithholdingTax
from ibkr_fa.number_checks import NumberKind
from ibkr_fa.parser._cells import parse_decimal_cell, require_columns
from ibkr_fa.parser._scope_guards import require_usd
from ibkr_fa.parser.activity_statement._dates import parse_statement_date
from ibkr_fa.parser.activity_statement._description import (
    parse_symbol_isin_from_description,
)
from ibkr_fa.parser.activity_statement._totals import PrintedTotals
from ibkr_fa.parser.activity_statement.multi_section import Section

_SECTION: Final[str] = "Withholding Tax"

# IBKR's code for a reversal row (#913); the only code a row may carry.
_REVERSAL_CODE: Final[str] = "Re"

# The trailing " - XX Tax" naming the country that took the tax (#1282).
_TAX_COUNTRY_SUFFIX: Final[re.Pattern[str]] = re.compile(r" - ([A-Z]{2}) Tax\Z")

_REQUIRED_WHT_ROW_KEYS: Final[tuple[str, ...]] = (
    "Currency",
    "Date",
    "Description",
    "Amount",
)

# IBKR's Withholding Tax section ships dates as either DD-MM-YYYY
# (IBKR India flavor) or YYYY-MM-DD (IBKR LLC / US flavor), same as
# the Dividends section. Both are parsed by the shared
# ibkr_fa.parser.activity_statement._dates helper (lifted there once
# the Interest parser (#139) became the third copy, per its docstring).


def _parse_date(raw: str, *, row_index: int, period: StatementPeriod) -> _dt.date:
    """Parse IBKR's Withholding-Tax-section date (DD-MM-YYYY or
    YYYY-MM-DD, same as the Dividends section), inside ``period``, via
    the shared
    :func:`ibkr_fa.parser.activity_statement._dates.parse_statement_date`
    helper."""
    return parse_statement_date(raw, section=_SECTION, row_index=row_index, period=period)


def _parse_amount(raw: str, *, row_index: int) -> Decimal:
    return parse_decimal_cell(
        raw, section=_SECTION, row_index=row_index, column="Amount", kind=NumberKind.MONEY
    )


def _check_code(code: str, amount: Decimal, *, row_index: int) -> None:
    """Refuse a ``Code`` other than blank or ``Re`` on a positive amount (#913).

    ``Re`` marks a reversal, which refunds tax, so it must be positive.
    The parser classifies a row by its amount's sign, so any other code
    could mean something it doesn't read.
    """
    if code == "" or (code == _REVERSAL_CODE and amount > 0):
        return
    raise ParseError(
        section=_SECTION,
        row_index=row_index,
        column="Code",
        message=(
            f"must be blank, or {_REVERSAL_CODE!r} on a positive amount (a "
            f"reversal refunds tax). The parser reads a row by its amount's "
            f"sign, so any other code could change the row's meaning unread."
        ),
        raw_value="",
    )


def parse_activity_statement_withholding_tax(
    block: Section,
    *,
    period: StatementPeriod,
) -> list[WithholdingTax]:
    """Reconstruct :class:`~ibkr_fa.models.WithholdingTax` records
    from one Activity Statement Withholding Tax block.

    Args:
        block: One
            :class:`~ibkr_fa.parser.activity_statement.multi_section.KnownActivitySection.WITHHOLDING_TAX`
            block from the splitter. A block with no rows (no WHT in
            the reporting period) returns an empty list — common for US-citizen accounts
            with a W-9 on file (0% WHT), or for buy-and-hold
            accounts with no foreign-distribution-bearing holdings.
        period: The statement's parsed Period. Every payment date must
            fall inside it (#1143).

    Returns:
        ``list[WithholdingTax]`` — one per row, in input order
        (which is IBKR's chronological emission order). The
        compute layer aggregates per-symbol and per-source-country;
        this layer preserves per-payment detail for the audit
        trail, FX conversion, and reversal pairing. Each row carries
        its file line and the country its ``" - XX Tax"`` suffix states,
        if any (#1282).

    Raises:
        ParseError: On any malformed row — missing required column;
            unparseable date (must be DD-MM-YYYY or YYYY-MM-DD); a
            date outside ``period`` (#1143); unparseable or
            non-finite ``Amount``; ZERO ``Amount`` (IBKR doesn't
            emit zero-tax rows; both negative-paid and
            positive-reversal are accepted); non-USD ``Currency``
            (v1 USD-only — multi-currency tracked in #52; the error
            carries no cell value, because a shifted cell can hold the
            holder's name or account number); a
            ``Description`` whose shape doesn't match the expected
            ``"SYMBOL(ISIN) ..."`` prefix format (any free-form
            suffix is accepted after the prefix — see
            :func:`~ibkr_fa.parser.activity_statement._description.parse_symbol_isin_from_description`);
            a ``Code`` other than blank, or ``Re`` on a positive
            amount (#913); or a printed total that doesn't match its rows or isn't
            where or what IBKR prints (see
            :mod:`ibkr_fa.parser.activity_statement._totals`). No error
            carries a cell value, in its message or ``raw_value``: a
            shifted cell can hold the holder's name or account number.
    """
    out: list[WithholdingTax] = []
    totals = PrintedTotals(_SECTION, block)
    # Rows other than the printed totals, for the postcondition.
    data_count = 0
    for row_index, (row, file_line) in enumerate(zip(block.rows, block.row_lines, strict=True)):
        totals.before_row(file_line)
        require_columns(
            row,
            _REQUIRED_WHT_ROW_KEYS,
            section=_SECTION,
            row_index=row_index,
            message=lambda missing: (
                f"required column(s) {missing!r} missing from the "
                f"Withholding Tax row. IBKR may have changed the "
                f"section's schema."
            ),
        )
        if totals.footer(row, file_line):
            continue
        data_count += 1
        currency = row["Currency"].strip()
        require_usd(currency, section=_SECTION, row_index=row_index, column="Currency")
        payment_date = _parse_date(row["Date"], row_index=row_index, period=period)
        description = row["Description"]
        symbol, isin = parse_symbol_isin_from_description(
            description, section=_SECTION, row_index=row_index
        )
        stated_country = _TAX_COUNTRY_SUFFIX.search(description)
        amount = _parse_amount(row["Amount"], row_index=row_index)
        if amount == 0:
            # Defensive: IBKR doesn't emit zero-tax rows. A zero
            # here suggests a malformed/truncated row that would
            # corrupt downstream sums (and the model itself
            # rejects zero at __post_init__, so this catches it
            # earlier with row context).
            raise ParseError(
                section=_SECTION,
                row_index=row_index,
                column="Amount",
                message=(
                    "withholding-tax amount must be non-zero (got "
                    "0). IBKR does not emit zero-tax rows; this "
                    "suggests a malformed or truncated row. "
                    "Negative amounts represent tax paid (normal); "
                    "positive amounts represent reversals/refunds "
                    "(both accepted)."
                ),
                raw_value="",
            )
        _check_code(row.get("Code", ""), amount, row_index=row_index)
        totals.detail(currency, amount, file_line)
        out.append(
            WithholdingTax(
                symbol=symbol,
                isin=isin,
                payment_date=payment_date,
                amount=amount,
                currency=currency,
                description=description,
                tax_country_iso2=None if stated_country is None else stated_country.group(1),
                file_line=file_line,
            )
        )
    totals.finish()
    # Postcondition: one WithholdingTax per row other than the printed
    # totals. A silently-dropped real WHT row would corrupt downstream
    # Schedule OS/FSI/TR/Form 67 totals.
    require(
        len(out) == data_count,
        what=(
            "parse_activity_statement_withholding_tax post: one "
            "WithholdingTax per non-footer data row; any deviation "
            "signals a silently-dropped row"
        ),
        data_count=data_count,
        output_count=len(out),
    )
    return out
