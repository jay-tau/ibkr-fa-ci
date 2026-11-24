"""Parse the IBKR Activity Statement ``Dividends`` section into :class:`Dividend`.

The Dividends section has one row per distribution payment received
during the reporting period. Schema (columns in order, IBKR ~2025):

  DataDiscriminator, Currency, Date, Description, Amount

Example data row (one logical CSV row, wrapped here for readability)::

  Dividends,Data,USD,18-09-2025,
    "XMPL(US99999A1025) Cash Dividend USD 0.25 per Share (Ordinary Dividend)",
    3.25

IBKR prints the section's totals as ``Data`` rows too: a per-currency
``Total`` row (Currency ``"Total"``) after each run of rows and, in
multi-currency accounts, a ``"Total Dividends in <CCY>"`` summary at
the end of the block. The parser validates every such row and checks
its sum when no FX is needed. A mismatch refuses, so a row lost from an
edited or cut-short file can't shrink the income (#908; the rules are in
:mod:`ibkr_fa.parser.activity_statement._totals`). The splitter refuses
a ``SubTotal``/``Total``-type row in this section.

IBKR leaves an empty section out of the export, and the dispatcher reads
a missing Dividends section as no rows. The commands that file income accept
that only when the statement's ``Change in NAV``, if printed, doesn't
contradict it (#1279; see
:mod:`ibkr_fa.parser.activity_statement.change_in_nav_income`).

Per :class:`Dividend`'s docstring: reversals/corrections live in the
``Change in Dividend Accruals`` section, not here, so every amount in
this section is positive.

The ``Description`` column embeds the issuing instrument's ticker
and ISIN in the format ``"<SYMBOL>(<ISIN>) <type> ..."`` where
``<type>`` is the distribution kind (Cash Dividend, Special
Dividend, Choice Dividend, Payment In Lieu Of Dividend, Return Of
Capital, Long-Term Capital Gain, etc.). v1 extracts the symbol +
ISIN and accepts any distribution type as a "gross amount paid /
credited" for Schedule FA Table A3 column 10's purposes — the
column label is "Total gross amount **paid/credited** with respect
to the holding during the period", which is broader than just
ordinary dividends. The distribution type is preserved on
:attr:`Dividend.description` for the audit trail; downstream code
that needs to split dispatches on :attr:`Dividend.kind`, which
:func:`classify_distribution` sets here. Schedule OS takes only the
ordinary kinds, and a Return Of Capital, Long-Term Capital Gain or Cash
in Lieu row refuses Schedule OS in the FY, Schedule CG on any date and
Schedule FA in the calendar year (#1310), so Table A3 column 10 never
sums one.

Tier-1 PR #137 review (correctness Opus 4.7 xhigh, Finding 4):
pre-fix, the regex required the literal "Cash Dividend\\b" prefix
and refused everything else with ParseError. This was a regression
versus the pre-#31 manual-fill path — a single Payment-In-Lieu /
Special Dividend row would block the entire pipeline. Now the
regex matches only the structural SYMBOL(ISIN) prefix and accepts
any descriptive suffix.
"""

from __future__ import annotations

import datetime as _dt
import re
from decimal import Decimal
from typing import Final

from ibkr_fa.exceptions import ParseError
from ibkr_fa.invariants import require
from ibkr_fa.models import DistributionKind, Dividend, StatementPeriod
from ibkr_fa.number_checks import NumberKind, number_limit_breach
from ibkr_fa.parser._cells import parse_decimal_cell, require_columns
from ibkr_fa.parser._scope_guards import require_usd
from ibkr_fa.parser.activity_statement._dates import parse_statement_date
from ibkr_fa.parser.activity_statement._description import (
    parse_symbol_isin_and_type_slice_from_description,
)
from ibkr_fa.parser.activity_statement._totals import PrintedTotals
from ibkr_fa.parser.activity_statement.multi_section import Section

_SECTION: Final[str] = "Dividends"

# Required columns this parser subscripts.
_REQUIRED_DIVIDENDS_ROW_KEYS: Final[tuple[str, ...]] = (
    "Currency",
    "Date",
    "Description",
    "Amount",
)

# IBKR's Dividends section ships dates as either DD-MM-YYYY (IBKR
# India flavor) or YYYY-MM-DD (IBKR LLC / US flavor) — both are
# parsed by the shared ibkr_fa.parser.activity_statement._dates
# helper. This is distinct from Trades' "YYYY-MM-DD, HH:MM:SS"
# datetime; a future maintainer should not reuse _parse_full_date_time
# from trades.py here.

# PR #138 moved the SYMBOL(ISIN) description regex + extraction
# helper to ``ibkr_fa.parser.activity_statement._description`` so the
# sister Withholding Tax parser could reuse it. The shape, charset,
# and ISIN rules are documented in detail there. Pre-#138 they
# lived as a private module constant here; the regression history
# (PR #137 Tier-1 correctness finding 4 + PR #137 Tier-3 P2 symbol
# charset widening for ``RDS-A``/``BF-B``/``BRK-B``) is preserved
# in the shared module's comments.


_KIND_BY_CASEFOLDED_LABEL: Final[dict[str, DistributionKind]] = {
    kind.value.casefold(): kind
    for kind in DistributionKind
    if kind not in {DistributionKind.UNRECOGNISED, DistributionKind.MALFORMED}
}
"""Case-folded lookup map (label → enum) for O(1) classification.

Built once at module-import time so the hot-path classifier does
not re-fold the enum-value labels per dividend. The two parse-time
failure markers (``UNRECOGNISED``, ``MALFORMED``) are excluded so a
user-emitted description that happens to equal ``"<unrecognised>"``
or ``"<malformed>"`` doesn't accidentally classify (defense-in-depth
against a hostile / corrupted statement).
"""


_USD_CURRENCY_MARKER: Final[str] = " USD "
"""The canonical IBKR currency marker that separates the kind keyword
from the per-share amount payload. Pinned as a module constant rather
than inlined so a multi-currency follow-up (#52) can extend the
classifier without grep-archaeology.
"""


_AMOUNT: Final[str] = r"(?P<amount>[0-9]+(?:\.[0-9]+)?)"

_PER_SHARE_PAYLOAD: Final[re.Pattern[str]] = re.compile(
    rf"{_AMOUNT} per Share(?: \(Ordinary Dividend\))?"
)
"""The text after ``" USD "`` for a known kind label: an unsigned ASCII
amount, ``per Share``, and optionally the ``(Ordinary Dividend)``
qualifier. Matched with :meth:`re.Pattern.fullmatch`."""

_CASH_IN_LIEU_PAYLOAD: Final[re.Pattern[str]] = re.compile(_AMOUNT)
"""The one extra payload form: ``Cash in Lieu USD <amount>``, with no
``per Share``."""


def classify_distribution(type_slice: str) -> DistributionKind:
    """Classify an IBKR distribution ``type_slice`` into a :class:`DistributionKind`.

    The ``type_slice`` is the substring AFTER the structural
    ``SYMBOL(ISIN)\\s+`` prefix, as returned by
    :func:`~ibkr_fa.parser.activity_statement._description.parse_symbol_isin_and_type_slice_from_description`.
    This function does NOT re-strip the structural prefix.

    Classification algorithm:

    1. If ``type_slice`` does not contain :data:`_USD_CURRENCY_MARKER`
       (``" USD "``), return :attr:`DistributionKind.MALFORMED`. A
       slice without the currency marker cannot be classified safely
       even if its leading words happen to match an allow-listed kind
       (defense-in-depth against a payload like ``"Cash Dividend"``
       with no amount, which would otherwise route through the
       allow-list).
    2. Extract the kind label as the substring before the first
       ``" USD "``, stripped, and case-fold-match it against
       :data:`_KIND_BY_CASEFOLDED_LABEL`. If it doesn't match, return
       :attr:`DistributionKind.UNRECOGNISED`, whatever follows the
       marker.
    3. For a known label, return its :class:`DistributionKind` only if
       the whole slice matches the supported grammar
       ``<label> USD <amount> per Share[ (Ordinary Dividend)]``, where
       ``<amount>`` is one or more ASCII digits with an optional
       decimal part and no sign. The one extra form is
       ``Cash in Lieu USD <amount>``, with no ``per Share``. Anything
       else (another qualifier, a suffix, a second amount, a sign, a
       footnote, extra spaces) returns
       :attr:`DistributionKind.MALFORMED`. Only the label is matched
       case-insensitively.

    This function NEVER raises. :func:`parse_activity_statement_dividends`
    stores :attr:`~DistributionKind.UNRECOGNISED` /
    :attr:`~DistributionKind.MALFORMED` on the :class:`Dividend` like
    any other kind:

    * Schedule FA Table A3 ignores them (sums gross amount regardless
      of kind).
    * The Schedule OS compute layer raises
      :class:`~ibkr_fa.compute.schedule_os.UnrecognisedDistributionTypeError`
      if an UNRECOGNISED / MALFORMED row reaches the dispatch path.

    Args:
        type_slice: The substring of the IBKR Dividends ``Description``
            cell AFTER the structural ``SYMBOL(ISIN)\\s+`` prefix has
            been stripped. Examples:
            ``"Cash Dividend USD 0.25 per Share (Ordinary Dividend)"``
            → :attr:`~DistributionKind.CASH_DIVIDEND`,
            ``"Return Of Capital USD 0.50 per Share"`` →
            :attr:`~DistributionKind.RETURN_OF_CAPITAL`,
            ``"Mystery Distribution USD 1.00 per Share"`` →
            :attr:`~DistributionKind.UNRECOGNISED`,
            ``"Cash Dividend"`` (no marker) and
            ``"Cash Dividend USD 0.25 per Share - US Tax"`` (a suffix) →
            :attr:`~DistributionKind.MALFORMED`.

    Returns:
        A :class:`DistributionKind` enum member. Never ``None``;
        the failure modes are encoded as
        :attr:`DistributionKind.UNRECOGNISED` and
        :attr:`DistributionKind.MALFORMED`.
    """
    return _classify(type_slice)[0]


def _classify(type_slice: str) -> tuple[DistributionKind, tuple[str, NumberKind] | None]:
    """:func:`classify_distribution`'s kind, with the payload's amount text and its
    number kind when the slice matches the grammar for a known label, else ``None``.

    The ``per Share`` amount is a unit price; the ``Cash in Lieu`` amount, with
    no ``per Share``, is a money amount.
    """
    if _USD_CURRENCY_MARKER not in type_slice:
        return DistributionKind.MALFORMED, None
    label, payload = type_slice.split(_USD_CURRENCY_MARKER, 1)
    kind = _KIND_BY_CASEFOLDED_LABEL.get(label.strip().casefold())
    if kind is None:
        return DistributionKind.UNRECOGNISED, None
    if label == label.strip():
        per_share = _PER_SHARE_PAYLOAD.fullmatch(payload)
        if per_share is not None:
            return kind, (per_share.group("amount"), NumberKind.PRICE)
        cash_in_lieu = _CASH_IN_LIEU_PAYLOAD.fullmatch(payload)
        if kind is DistributionKind.CASH_IN_LIEU and cash_in_lieu is not None:
            return kind, (cash_in_lieu.group("amount"), NumberKind.MONEY)
    return DistributionKind.MALFORMED, None


def _parse_date(raw: str, *, row_index: int, period: StatementPeriod) -> _dt.date:
    """Parse IBKR's Dividends-section date (DD-MM-YYYY or YYYY-MM-DD),
    inside ``period``, via the shared
    :func:`ibkr_fa.parser.activity_statement._dates.parse_statement_date`
    helper."""
    return parse_statement_date(raw, section=_SECTION, row_index=row_index, period=period)


def _parse_amount(raw: str, *, row_index: int) -> Decimal:
    return parse_decimal_cell(
        raw, section=_SECTION, row_index=row_index, column="Amount", kind=NumberKind.MONEY
    )


def parse_activity_statement_dividends(
    block: Section, *, period: StatementPeriod
) -> list[Dividend]:
    """Reconstruct :class:`Dividend` records from one Activity Statement Dividends block.

    Args:
        block: One :class:`KnownActivitySection.DIVIDENDS` block from
            the splitter. A block with no rows (no dividends in the
            reporting period) returns an empty list — common for
            buy-and-hold accounts with no holdings during dividend
            ex-dates.
        period: The statement's parsed Period. Every payment date must
            fall inside it (#1143).

    Returns:
        ``list[Dividend]`` — one per row, in input order (which is
        IBKR's chronological emission order). The compute layer
        aggregates per-symbol; this layer preserves per-payment
        detail for the audit trail and downstream FX conversion.

    Raises:
        ParseError: On any malformed row — missing required column,
            unparseable date (must be DD-MM-YYYY or YYYY-MM-DD), a
            date outside ``period`` (#1143), unparseable or
            non-finite ``Amount``, non-USD ``Currency`` (v1 USD-only;
            #52 tracks multi-currency; the error carries no cell value,
            because a shifted cell can hold the holder's name or
            account number), or a ``Description`` whose
            shape doesn't match the expected ``"SYMBOL(ISIN) ..."``
            prefix format (any distribution type is accepted after
            the prefix — see
            :func:`~ibkr_fa.parser.activity_statement._description.parse_symbol_isin_from_description`),
            or a printed total that doesn't match its rows or isn't
            where or what IBKR prints (see
            :mod:`ibkr_fa.parser.activity_statement._totals`). No
            error carries a cell value, in its message or
            ``raw_value``: a shifted cell can hold the holder's name or
            account number.
    """
    out: list[Dividend] = []
    totals = PrintedTotals(_SECTION, block)
    # Rows other than the printed totals, for the postcondition.
    data_count = 0
    for row_index, (row, file_line) in enumerate(zip(block.rows, block.row_lines, strict=True)):
        totals.before_row(file_line)
        require_columns(
            row,
            _REQUIRED_DIVIDENDS_ROW_KEYS,
            section=_SECTION,
            row_index=row_index,
            message=lambda missing: (
                f"required column(s) {missing!r} missing from the "
                f"Dividends row. IBKR may have changed the section's "
                f"schema."
            ),
        )
        if totals.footer(row, file_line):
            continue
        data_count += 1
        currency = row["Currency"].strip()
        require_usd(currency, section=_SECTION, row_index=row_index, column="Currency")
        payment_date = _parse_date(row["Date"], row_index=row_index, period=period)
        description = row["Description"]
        symbol, isin, type_slice = parse_symbol_isin_and_type_slice_from_description(
            description, section=_SECTION, row_index=row_index
        )
        kind, payload_amount = _classify(type_slice)
        if payload_amount is not None:
            # Matched by shape but never kept: still within its limits (#1276).
            amount_text, amount_kind = payload_amount
            breach = number_limit_breach(Decimal(amount_text), amount_kind)
            if breach is not None:
                raise ParseError(
                    section=_SECTION,
                    row_index=row_index,
                    column="Description",
                    message=breach,
                    raw_value="",
                )
        amount_gross = _parse_amount(row["Amount"], row_index=row_index)
        if amount_gross <= 0:
            # Defensive: Dividends section emits positive amounts;
            # reversals go through Change in Dividend Accruals. A
            # zero/negative here suggests a malformed row or an
            # adjustment that IBKR misclassified — refuse so the
            # downstream sum isn't corrupted.
            raise ParseError(
                section=_SECTION,
                row_index=row_index,
                column="Amount",
                message=(
                    "dividend amount must be positive; reversals are "
                    "emitted in the Change in Dividend Accruals section, "
                    "not here."
                ),
                raw_value="",
            )
        totals.detail(currency, amount_gross, file_line)
        out.append(
            Dividend(
                symbol=symbol,
                isin=isin,
                payment_date=payment_date,
                amount_gross=amount_gross,
                currency=currency,
                description=description,
                kind=kind,
                type_slice=type_slice,
                source_row_index=row_index,
            )
        )
    totals.finish()
    # Postcondition: one Dividend per row other than the printed
    # totals. A silently-dropped real dividend would corrupt
    # downstream Schedule FA column 10 totals.
    require(
        len(out) == data_count,
        what=(
            "parse_activity_statement_dividends post: one Dividend per "
            "non-footer data row; any deviation signals a silently-"
            "dropped row"
        ),
        data_count=data_count,
        output_count=len(out),
    )
    return out
