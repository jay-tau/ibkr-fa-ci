"""Parse the IBKR Activity Statement ``Interest`` section into
:class:`~ibkr_fa.models.InterestCredit` and
:class:`~ibkr_fa.models.InterestNonCredit` records.

The Interest section emits one row per cash-balance interest posting
to the user's account during the reporting period. Sister
parser to :mod:`ibkr_fa.parser.activity_statement.dividends` and
:mod:`ibkr_fa.parser.activity_statement.withholding_tax` — same
section-tagged Activity Statement format, same printed-total checks.
Schema (columns in order, IBKR ~2025-2026)::

  DataDiscriminator, Currency, Date, Description, Amount

Example data rows (one logical CSV row each)::

  Interest,Data,USD,30-04-2025,USD Credit Interest for Apr-2025,1.23
  Interest,Data,USD,2025-12-03,USD Credit Interest for Nov-2025,0.73

The Date column ships in either DD-MM-YYYY or YYYY-MM-DD form
depending on the account's IBKR date-format preference; this parser
accepts both via the shared
:mod:`ibkr_fa.parser.activity_statement._dates` helper. The Dividends
and Withholding Tax sister parsers were back-ported to the same
shared helper, so YYYY-MM-DD-format accounts (e.g., the IBKR LLC
flavor) now parse end-to-end through the whole pipeline, not just the
Interest section.

IBKR prints the section's totals as ``Data`` rows too: a per-currency
``Total`` row (Currency ``"Total"``) after each run of rows and, in
multi-currency accounts, a ``"Total Interest in <CCY>"`` summary at the
end of the block. As in the sister Dividends parser, every such row is
validated and its sum checked when no FX is needed. A mismatch refuses
(#908; the rules are in :mod:`ibkr_fa.parser.activity_statement._totals`). Here a
``Total`` adds up every row of its run (credits, debit interest and
unproven rows alike), not only the credits the parser keeps; a run of
non-USD debit interest still needs its own ``Total``, and makes a
``Total Interest in USD`` summary uncheckable without FX. The splitter
refuses a ``SubTotal``/``Total``-type row in this section.

IBKR leaves an empty section out of the export, and the dispatcher reads
a missing Interest section as no rows. The commands that file income accept
that only when the statement's ``Change in NAV``, if printed, doesn't
contradict it (#1279; see
:mod:`ibkr_fa.parser.activity_statement.change_in_nav_income`).

Unlike the Dividends / Withholding Tax sections, the Interest
section's ``Description`` is FREE-FORM TEXT (e.g.,
``"USD Credit Interest for Apr-2025"``) — it does NOT embed a
``SYMBOL(ISIN)`` prefix because cash-balance interest is paid on
the cash balance itself, not on a specific security. The parser
therefore does not reuse
:func:`~ibkr_fa.parser.activity_statement._description.parse_symbol_isin_from_description`;
it simply preserves the description verbatim on
:attr:`InterestCredit.description` for the audit trail.

**Sign convention**: IBKR's ``Interest`` section emits BOTH credit
interest (POSITIVE amounts — money entering the cash balance) AND
debit interest (NEGATIVE amounts — margin interest the user PAID,
on margin-enabled accounts). The official IBKR docs at
https://ibkrguides.com/reportingreference/reportguide/interest_default.htm
confirm both sign classes can ship in the same section, plus
interest accruals and accrual adjustments.

**Classification (#905, #1280).** After the shape checks, which include a
Currency of three uppercase ASCII letters on every non-footer row
(#1027), every non-footer row lands in exactly one group; the first match wins:

1. **unproven**: the description contains ``"accrual"``
   (case-insensitive), whatever the sign. Treating an accrual as a
   realized credit would double-count interest once the cash credit
   posts, and dropping one could lose a credit.
2. **credit**: a positive amount whose description reads exactly
   ``<CCY> Credit Interest for <Mon>-<YYYY>``, IBKR's credit-interest
   wording: ``<CCY>`` is the row's Currency, ``<Mon>`` an English
   three-letter month (``Jan`` to ``Dec``) and ``<YYYY>`` four ASCII
   digits, as in ``USD Credit Interest for Apr-2025``. Returned as an
   :class:`~ibkr_fa.models.InterestCredit`.
3. **unproven**: any other positive amount, e.g. a refund of debit
   interest or a label the parser has never seen. Filing it as interest
   would guess what it is.
4. **debit interest**: a negative amount whose description contains
   ``"debit interest"`` (case-insensitive). Margin interest the user
   paid isn't income.
5. **unproven**: anything else, e.g. a negative correction to a
   credit, or a zero row.

A positive row in groups 2 and 3 must be in USD (v1 is USD-only,
multi-currency is #52), so the parser refuses a non-USD one whatever its
description, as it did before #1280. Groups 1, 3, 4 and 5 come back as
:class:`~ibkr_fa.models.InterestNonCredit` records carrying their
group and file line. Past that USD check the parser raises nothing for
them, so ``schedule-cg``, ``schedule-cg-ltcg`` and Table A3, which don't
use interest, still run on a margin or accrual-heavy statement (the
reason PR #186 skipped these rows). The commands that use interest
decide instead: ``schedule-os``, ``schedule-fsi``, ``schedule-tr`` and
``form-67`` refuse any unproven row
(:func:`~ibkr_fa.compute.schedule_os_interest.require_no_unproven_interest`),
and Table A2 leaves its Interest nature for manual fill. Debit
interest stays out of income and A2.

A postcondition checks ``len(interest_credits) + len(non_credits) ==
data_count``, so a silently dropped row raises
:class:`~ibkr_fa.exceptions.InvariantError`.
"""

from __future__ import annotations

import datetime as _dt
import re
from decimal import Decimal
from typing import Final

from ibkr_fa.exceptions import ParseError
from ibkr_fa.invariants import require
from ibkr_fa.models import InterestCredit, InterestNonCredit, InterestRowGroup, StatementPeriod
from ibkr_fa.models.account_info import _CURRENCY_PATTERN
from ibkr_fa.number_checks import NumberKind
from ibkr_fa.parser._cells import parse_decimal_cell, require_columns
from ibkr_fa.parser._scope_guards import require_usd
from ibkr_fa.parser.activity_statement._dates import parse_statement_date
from ibkr_fa.parser.activity_statement._totals import PrintedTotals
from ibkr_fa.parser.activity_statement.multi_section import Section

_SECTION: Final[str] = "Interest"

# IBKR's credit-interest wording, e.g. "USD Credit Interest for Apr-2025" (#1280).
# The parser also requires the leading code to equal the row's Currency.
_CREDIT_INTEREST_DESCRIPTION: Final[re.Pattern[str]] = re.compile(
    r"([A-Z]{3}) Credit Interest for "
    r"(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)-[0-9]{4}"
)

_REQUIRED_INTEREST_ROW_KEYS: Final[tuple[str, ...]] = (
    "Currency",
    "Date",
    "Description",
    "Amount",
)


def _parse_date(raw: str, *, row_index: int, period: StatementPeriod) -> _dt.date:
    """Parse IBKR's Interest-section date (DD-MM-YYYY or YYYY-MM-DD),
    inside ``period``, via the shared
    :func:`ibkr_fa.parser.activity_statement._dates.parse_statement_date`
    helper. Both shapes are observed in real IBKR exports (the
    per-account date-format preference varies)."""
    return parse_statement_date(raw, section=_SECTION, row_index=row_index, period=period)


def _parse_amount(raw: str, *, row_index: int) -> Decimal:
    return parse_decimal_cell(
        raw, section=_SECTION, row_index=row_index, column="Amount", kind=NumberKind.MONEY
    )


def parse_activity_statement_interest(
    block: Section,
    *,
    period: StatementPeriod,
) -> tuple[list[InterestCredit], list[InterestNonCredit]]:
    """Classify one Activity Statement Interest block's rows.

    Args:
        block: One
            :class:`~ibkr_fa.parser.activity_statement.multi_section.KnownActivitySection.INTEREST`
            block from the splitter. Empty rows (no interest in the
            reporting period) return two empty lists — common for
            accounts whose cash balance never exceeded IBKR's
            minimum-balance threshold for credit interest.
        period: The statement's parsed Period. Every row's date, credit
            or not, must fall inside it (#1143).

    Returns:
        ``(credits, non_credits)``, each in input order (IBKR's
        chronological emission order). ``credits`` holds one
        :class:`~ibkr_fa.models.InterestCredit` per positive row whose
        description reads exactly ``<CCY> Credit Interest for
        <Mon>-<YYYY>``; ``non_credits`` holds
        one :class:`~ibkr_fa.models.InterestNonCredit` per other
        non-footer row, with its group and file line (see the module
        docstring for the order of the checks).

    Raises:
        ParseError: On any malformed row — missing required column;
            unparseable date (must be DD-MM-YYYY or YYYY-MM-DD,
            both formats supported); a date outside ``period``, on any
            row (#1143); unparseable or non-finite
            ``Amount``; empty ``Description``; a ``Currency`` that
            isn't three uppercase ASCII letters, on a non-footer row; or a
            non-USD ``Currency`` on a positive row whose description
            doesn't mention an accrual, credit or unproven (v1 USD-only,
            multi-currency tracked in #52). Neither currency error
            carries the cell's value, because a shifted cell can hold
            the holder's name or account number (#1027). **All shape
            validation runs BEFORE the
            classification**, so a malformed row (bad date / empty
            description / non-finite amount) raises whatever group it
            would land in (T3 codex-emulation P2 finding on PR #186
            round 2). Unproven rows don't raise here; the commands
            that use interest refuse them. Also on a printed total
            that doesn't match its rows or isn't where or what IBKR
            prints (see :mod:`ibkr_fa.parser.activity_statement._totals`).
            No error carries a cell value, in its message or
            ``raw_value``: a shifted cell can hold the holder's name or
            account number.
    """
    interest_credits: list[InterestCredit] = []
    non_credits: list[InterestNonCredit] = []
    totals = PrintedTotals(_SECTION, block)
    # Rows other than the printed totals, for the postcondition.
    data_count = 0
    for row_index, (row, file_line) in enumerate(zip(block.rows, block.row_lines, strict=True)):
        totals.before_row(file_line)
        require_columns(
            row,
            _REQUIRED_INTEREST_ROW_KEYS,
            section=_SECTION,
            row_index=row_index,
            message=lambda missing: (
                f"required column(s) {missing!r} missing from the "
                f"Interest row. IBKR may have changed the section's "
                f"schema."
            ),
        )
        if totals.footer(row, file_line):
            continue
        data_count += 1
        currency = row["Currency"].strip()
        # Echo no value: a shifted cell can put the holder's name or account here.
        if _CURRENCY_PATTERN.fullmatch(currency) is None:
            raise ParseError(
                section=_SECTION,
                row_index=row_index,
                column="Currency",
                message=(
                    "Activity Statement Interest column 'Currency' must be 3 "
                    "uppercase ASCII letters (ISO 4217)."
                ),
                raw_value="",
            )
        # SHAPE VALIDATION FIRST. T3 codex-emulation P2 finding on
        # PR #186 round 2: validate finite Amount, valid Date, and
        # non-empty Description before classifying, so a shape bug
        # surfaces loudly whatever group the row would land in.
        amount = _parse_amount(row["Amount"], row_index=row_index)
        row_date = _parse_date(row["Date"], row_index=row_index, period=period)
        description = row["Description"]
        if description == "":
            raise ParseError(
                section=_SECTION,
                row_index=row_index,
                column="Description",
                message=(
                    "Interest row Description must be non-empty "
                    "(needed for audit trail; IBKR emits a human-"
                    "readable label such as 'USD Credit Interest "
                    "for Apr-2025')."
                ),
                raw_value="",
            )
        totals.detail(currency, amount, file_line)
        # Classification (#905, #1280); the first match wins. IBKR's
        # reporting guide documents accrual and accrual-change rows
        # alongside realized credits, so an "accrual" description is
        # unproven whatever its sign. A positive row is a credit only in
        # IBKR's credit-interest wording; any other is unproven.
        folded = description.casefold()
        group: InterestRowGroup | None
        if "accrual" in folded:
            group = InterestRowGroup.UNPROVEN_ACCRUAL
        elif amount > 0:
            # Every positive row is USD-only, as before #1280.
            require_usd(currency, section=_SECTION, row_index=row_index, column="Currency")
            wording = _CREDIT_INTEREST_DESCRIPTION.fullmatch(description)
            group = (
                None
                if wording is not None and wording.group(1) == currency
                else InterestRowGroup.UNPROVEN_POSITIVE
            )
        elif amount < 0 and "debit interest" in folded:
            group = InterestRowGroup.DEBIT_INTEREST
        else:
            group = InterestRowGroup.UNPROVEN_NON_CREDIT
        if group is not None:
            non_credits.append(
                InterestNonCredit(
                    group=group,
                    amount=amount,
                    currency=currency,
                    row_date=row_date,
                    description=description,
                    file_line=file_line,
                )
            )
            continue
        interest_credits.append(
            InterestCredit(
                credit_date=row_date,
                amount_gross=amount,
                currency=currency,
                description=description,
                source_row_index=row_index,
            )
        )
    totals.finish()
    # Postcondition: every row other than the printed totals
    # produced exactly one record. Any deviation signals a silently
    # dropped row that would corrupt downstream interest totals.
    require(
        len(interest_credits) + len(non_credits) == data_count,
        what=(
            "parse_activity_statement_interest post: every non-footer "
            "data row produced an InterestCredit or an InterestNonCredit; "
            "any deviation signals a silently-dropped row"
        ),
        data_count=data_count,
        credit_count=len(interest_credits),
        non_credit_count=len(non_credits),
    )
    return interest_credits, non_credits
