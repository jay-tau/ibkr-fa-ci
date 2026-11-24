"""Prove that the Dec-31 purchase is the only ESPP lot held in the calendar year (#1151).

``netbenefits-fa`` rebuilds the Dec-31 lots from the open- and closed-lots exports
(:mod:`ibkr_fa.compute.espp_positions`). Neither export carries a download date, so
a lot bought and sold during the CY can be missing from both. After the lots are
matched to their confirmations, :func:`prove_dec31_purchase_is_the_only_lot` checks
the statements and every confirmation in the bundle instead. Two dates drive it:
*S*, the Dec-31 purchase's settlement date, and *T*, Dec-31 plus
:data:`~ibkr_fa.compute.source_coverage.SETTLEMENT_BOUND`. It assumes every purchase
settles within that bound, so a purchase traded on or before Dec-31 settles on or
before *T*. In this order, it refuses:

* a confirmation dated before Dec-31: a lot bought before or during the CY;
* *S* after *T*: the bound can't be relied on;
* Stock Plan Services reports that don't cover, with no gap, from the later of Jan-1
  and ``netbenefits.account.account_opening_date`` through *T*;
* two overlapping Stock Plan Services reports that list different purchase rows
  or counts on shared days through *T*, or a year-end report that prints a row
  more times than a Stock Plan Services report covering that day. A year-end
  report's silence is no omission; two year-end reports aren't compared;
* a purchase row settling on or before *T*, in any report, that is neither the
  Dec-31 purchase's own row nor a later purchase's row matching its own confirmation.
  Matching is one to one, and a row printed by overlapping reports counts once;
* no own row for the Dec-31 purchase;
* a report ending on or before *T* that holds another security, or whose shares
  differ from those of the matched purchases settled by its end beyond the
  Holdings rounding tolerance.

Refusals name a statement by its place among all reports by period start, as the
Table A2 proof does, and a row by its 1-based place in its table. They print no
value from a file.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterator, Mapping, Sequence
from datetime import date
from decimal import MAX_EMAX, MAX_PREC, MIN_EMIN, Context, Decimal, localcontext
from typing import Final

from ibkr_fa.compute.espp_positions import NetBenefitsFaError, confirmation_label
from ibkr_fa.compute.source_coverage import SETTLEMENT_BOUND, first_coverage_gap
from ibkr_fa.config import NetBenefitsConfig
from ibkr_fa.invariants import require
from ibkr_fa.normalize.netbenefits.statement_label import ALL_REPORTS_ORDER
from ibkr_fa.normalize.netbenefits.statement_label import statement_among_all_reports as _statement
from ibkr_fa.parser.netbenefits.confirmation_pdf import EspPurchase
from ibkr_fa.parser.netbenefits.statement_pdf import ReportType, StatementData, StatementPurchase

# Every difference and sum below is exact, so no rounding can hide a mismatch.
_EXACT: Final = Context(prec=MAX_PREC, Emax=MAX_EMAX, Emin=MIN_EMIN)
_BOUGHT_TABLE: Final = "Securities Bought & Sold"


def prove_dec31_purchase_is_the_only_lot(
    statements: Sequence[StatementData],
    confirmations_by_date: Mapping[date, EspPurchase],
    *,
    dec31_purchase: EspPurchase,
    cfg: NetBenefitsConfig,
    calendar_year: int,
) -> None:
    """Refuse unless no ESPP lot other than ``dec31_purchase`` was held in the CY.

    ``statements`` are every report, year-end ones included, sorted by period
    start; ``confirmations_by_date`` are every confirmation in the bundle. See the
    module docstring for the checks and their order.

    Raises:
        NetBenefitsFaError: A check fails.
    """
    cy_end = date(calendar_year, 12, 31)
    require(
        dec31_purchase.purchase_date == cy_end,
        what="the lot proof needs the CY's Dec-31 purchase",
    )
    earliest = min(confirmations_by_date)
    if earliest < cy_end:
        raise NetBenefitsFaError(
            f"{confirmation_label(earliest, confirmations_by_date)}, purchase date: dated "
            f"before Dec-31, so a lot was bought before or during CY{calendar_year}; "
            "netbenefits-fa supports only an account whose lots were all bought on Dec-31"
        )
    bound = cy_end + SETTLEMENT_BOUND
    if dec31_purchase.settlement_date > bound:
        raise NetBenefitsFaError(
            f"{confirmation_label(cy_end, confirmations_by_date)}, settlement date: the Dec-31 "
            "purchase settles more than ten days after Dec-31, so the statements can't be "
            "relied on to show every purchase made on or before Dec-31"
        )
    gap = first_coverage_gap(
        (
            (s.period_start, s.period_end)
            for s in statements
            if s.report_type is ReportType.STOCK_PLAN_SERVICES
        ),
        window_start=max(date(calendar_year, 1, 1), cfg.account.account_opening_date),
        window_end=bound,
    )
    if gap is not None:
        raise NetBenefitsFaError(
            "the Stock Plan Services reports don't cover the window from the later of Jan-1 "
            "and netbenefits.account.account_opening_date through ten days after Dec-31, so "
            "they can't show every purchase made on or before Dec-31; supply the missing report"
        )
    numbered = list(enumerate(statements, start=1))
    _require_overlapping_reports_agree(numbered, bound)
    matched = _match_purchase_rows(
        numbered,
        confirmations_by_date,
        cusip=cfg.security.cusip,
        bound=bound,
        calendar_year=calendar_year,
    )
    if dec31_purchase not in matched:
        raise NetBenefitsFaError(
            "no Stock Plan Services report shows the Dec-31 purchase's row with its "
            "confirmation's settlement date, identifier, price, quantity and amount; the "
            "purchase can't be tied to the statements"
        )
    _require_holdings_explained(
        numbered,
        matched,
        symbol=cfg.security.instrument.symbol,
        bound=bound,
        calendar_year=calendar_year,
    )


def _rows_by(s: StatementData, bound: date) -> Iterator[tuple[int, StatementPurchase]]:
    """Each of ``s``'s purchase rows settling on or before ``bound``, with its
    1-based place in its table."""
    for row, purchase in enumerate(s.purchases, start=1):
        if purchase.settlement_date <= bound:
            yield row, purchase


def _require_overlapping_reports_agree(
    numbered: list[tuple[int, StatementData]], bound: date
) -> None:
    """Refuse two reports that list different purchase rows for the days they share,
    on or before ``bound``. Two Stock Plan Services reports must list the same rows;
    a year-end report's rows must each be in the Stock Plan Services report, as many
    times as the year-end report prints them. Two year-end reports aren't compared."""
    for index, (first, a) in enumerate(numbered):
        for second, b in numbered[index + 1 :]:
            start = max(a.period_start, b.period_start)
            end = min(a.period_end, b.period_end, bound)
            if start > end:
                continue
            rows_a, rows_b = (
                Counter(p for _, p in _rows_by(s, end) if p.settlement_date >= start)
                for s in (a, b)
            )
            if a.report_type is ReportType.STOCK_PLAN_SERVICES:
                agree = rows_a == rows_b if b.report_type is a.report_type else rows_b <= rows_a
            elif b.report_type is ReportType.STOCK_PLAN_SERVICES:
                agree = rows_a <= rows_b
            else:
                continue
            if not agree:
                raise NetBenefitsFaError(
                    f"statements {first} and {second} ({ALL_REPORTS_ORDER}) list different ESPP "
                    "purchase rows for the days they share"
                )


def _distinct_rows(
    numbered: list[tuple[int, StatementData]], bound: date
) -> Iterator[tuple[str, StatementPurchase]]:
    """Each distinct purchase row settling on or before ``bound``, named by the first
    report printing it and its place there, in report then table order. A row
    printed identically by overlapping reports counts once: as many times as the
    report printing it most often prints it."""
    counted: Counter[StatementPurchase] = Counter()
    for number, s in numbered:
        printed: Counter[StatementPurchase] = Counter()
        for row, purchase in _rows_by(s, bound):
            printed[purchase] += 1
            if printed[purchase] > counted[purchase]:
                counted[purchase] += 1
                yield f"{_statement(number)}, row {row} of its {_BOUGHT_TABLE} table", purchase


def _half_unit(value: Decimal) -> Decimal:
    """Half a unit in ``value``'s last decimal place, such as ``0.0005`` for
    ``3.482``."""
    with localcontext(_EXACT):
        return Decimal(5).scaleb(int(value.as_tuple().exponent) - 1)


def _first_difference(row: StatementPurchase, purchase: EspPurchase) -> str | None:
    """The column and refusal for the first of price, quantity and amount on which
    ``row`` differs from ``purchase``, or ``None``.

    The price must equal the confirmation's at the coarser of the two documents'
    decimals: within half a unit of the coarser last place. The quantity must be
    within half a unit of the row's last place (3 dp) of the confirmation's shares,
    and the amount must be exactly the contribution as an outflow.
    """
    price = purchase.purchase_price_per_share_usd
    with localcontext(_EXACT):
        if (row.price_usd - price).copy_abs() > max(_half_unit(row.price_usd), _half_unit(price)):
            return (
                "Price column: an ESPP purchase's price differs from its confirmation's "
                "purchase price"
            )
        if (row.quantity - purchase.shares).copy_abs() > _half_unit(row.quantity):
            return (
                "Quantity column: an ESPP purchase's quantity differs from its confirmation's "
                "shares"
            )
    if row.amount_usd != purchase.contribution_usd.copy_negate():
        return (
            "Amount column: an ESPP purchase's amount differs from its confirmation's contribution"
        )
    return None


def _match_purchase_rows(
    numbered: list[tuple[int, StatementData]],
    confirmations_by_date: Mapping[date, EspPurchase],
    *,
    cusip: str,
    bound: date,
    calendar_year: int,
) -> list[EspPurchase]:
    """Match each purchase row settling on or before ``bound`` to its own
    confirmation, one to one, and return the confirmations matched.

    Each row, in report then table order, must be for ``cusip`` and match exactly
    one still-unmatched confirmation in settlement date, identifier, price, quantity
    and amount. A row matching more than one refuses as ambiguous. A row matching
    none refuses with its first differing column when its settlement date and
    identifier pick exactly one still-unmatched confirmation, and as unexplained
    otherwise.
    """
    unmatched = dict(confirmations_by_date)
    matched: list[EspPurchase] = []
    for where, row in _distinct_rows(numbered, bound):
        if row.identifier != cusip:
            raise NetBenefitsFaError(
                f"{where}, Symbol/CUSIP column: an ESPP purchase is for a security other than "
                "netbenefits.security.cusip"
            )
        candidates = [
            purchase_date
            for purchase_date, purchase in unmatched.items()
            if (purchase.settlement_date, purchase.cusip) == (row.settlement_date, row.identifier)
        ]
        full = [d for d in candidates if _first_difference(row, unmatched[d]) is None]
        if len(full) > 1:
            raise NetBenefitsFaError(
                f"{where}, Settlement Date column: an ESPP purchase matches more than one "
                "confirmation in every field, so it can't be tied to one purchase"
            )
        if full:
            matched.append(unmatched.pop(full[0]))
            continue
        if len(candidates) == 1:
            raise NetBenefitsFaError(f"{where}, {_first_difference(row, unmatched[candidates[0]])}")
        raise NetBenefitsFaError(
            f"{where}, Settlement Date column: an ESPP purchase settling within ten days after "
            "Dec-31 or earlier is neither the Dec-31 purchase nor a later purchase with a "
            f"confirmation of its own, so a lot may have been held during CY{calendar_year}; "
            "netbenefits-fa supports only lots bought on Dec-31"
        )
    return matched


def _require_holdings_explained(
    numbered: list[tuple[int, StatementData]],
    matched: list[EspPurchase],
    *,
    symbol: str,
    bound: date,
    calendar_year: int,
) -> None:
    """Refuse a report ending on or before ``bound`` whose holdings the matched
    purchases settled by its end don't explain.

    Every Holdings row must be of ``symbol``. Their quantities must sum to those
    purchases' shares at full precision, within half a unit of the last place per
    Holdings row. A report with no Holdings table (unpriced), or a priced one with
    no row of ``symbol``, refuses unless no matched purchase had settled.
    """
    for number, s in numbered:
        if s.period_end > bound:
            continue
        with localcontext(_EXACT):
            expected = sum(
                (p.shares for p in matched if p.settlement_date <= s.period_end), Decimal(0)
            )
        for row, holding in enumerate(s.holdings, start=1):
            if holding.symbol != symbol:
                raise NetBenefitsFaError(
                    f"{_statement(number)}, row {row} of its Holdings table, Description column: "
                    "a holding of a security other than netbenefits.security.symbol isn't "
                    "explained by the matched purchases"
                )
        if s.account_nav_usd is None:
            if expected:
                raise NetBenefitsFaError(
                    f"{_statement(number)} prints no Holdings table, so the shares held at its "
                    "period end can't be tied to the matched purchases settled by then; the "
                    "whole statement was checked"
                )
            continue
        if not s.holdings:
            if expected:
                raise NetBenefitsFaError(
                    f"{_statement(number)} lists no holding of netbenefits.security.symbol, "
                    "though matched purchases had settled by its period end; the whole "
                    "statement was checked"
                )
            continue
        with localcontext(_EXACT):
            held = sum((h.quantity for h in s.holdings), Decimal(0))
            tolerance = sum((_half_unit(h.quantity) for h in s.holdings), Decimal(0))
            explained = (held - expected).copy_abs() <= tolerance
        if not explained:
            raise NetBenefitsFaError(
                f"{_statement(number)}, row 1 of its Holdings table, Quantity column: the shares "
                "held at the period end aren't explained by the Dec-31 purchase and the "
                "confirmed later purchases, so netbenefits-fa can't prove no lot was held during "
                f"CY{calendar_year}; it supports only lots bought on Dec-31"
            )
