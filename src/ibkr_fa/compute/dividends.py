"""Aggregate per-symbol Schedule FA dividend totals over a calendar year.

Schedule FA Table A3 column 10 ("Total gross amount paid/credited
with respect to the holding during the period") is the per-holding
sum of all dividends paid during the calendar year, converted to INR
at the SBI TTBR on the calendar-year closing date (December 31).

This module is the bridge between the per-payment :class:`Dividend`
records the parser produces and the per-symbol INR figure the
:class:`~ibkr_fa.models.fa_a3_row.FaA3Row` writer needs.

Algorithm:

1. Filter dividends to the target calendar year (Jan 1 - Dec 31 inclusive).
2. Sum foreign-currency amounts by :attr:`Dividend.symbol`, exactly
   (:func:`~ibkr_fa.compute.exact_inr.checked_addition`, #1168).
3. Convert each symbol total via
   ``FxPolicy.rate_for(date(calendar_year, 12, 31))``, exactly
   (:func:`~ibkr_fa.compute.exact_inr.checked_product`, #1312).
4. Preserve **full :class:`~decimal.Decimal` precision**, matching the convention of
   :mod:`ibkr_fa.compute.initial_value` and
   :mod:`ibkr_fa.compute.closing_value` (which both leave rupee
   rounding to the model).
   :attr:`FaA3Row.filed <ibkr_fa.models.fa_a3_row.FaA3Row.filed>`
   rounds the row's amount half-up to the whole rupees every writer
   renders.

v1 scope: aggregation keys on :attr:`Dividend.symbol` (ticker) ONLY,
not ``(symbol, isin)``. This matches the FA-A3 row key (also ticker
in :func:`ibkr_fa.compute.fa_a3.build_fa_a3_rows`). In the rare
edge case of a ticker being reassigned to a different ISIN mid-year
(e.g., reverse-merger ticker reuse), payments on the new-ISIN
security would be summed into the same per-ticker total as the
old-ISIN lot. The :class:`Dividend` model preserves
:attr:`Dividend.isin` so a future enhancement can switch to
``(symbol, isin)`` keying without a model change.

It also counts the US-source dividends and net US withholding tax that
feed the FA run's US-WHT advisory (#145, #626).
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import date
from decimal import Decimal

from ibkr_fa.compute.exact_inr import checked_addition, checked_product
from ibkr_fa.fx.policy import FxPolicy
from ibkr_fa.invariants import require
from ibkr_fa.models import Dividend, WithholdingTax
from ibkr_fa.types import Ticker


def aggregate_dividends_by_symbol_inr(
    dividends: Iterable[Dividend],
    *,
    calendar_year: int,
    fx_policy: FxPolicy,
) -> dict[Ticker, Decimal]:
    """Aggregate per-symbol total INR dividends paid during the CY.

    Args:
        dividends: All :class:`Dividend` records parsed from the
            Activity Statement's Dividends section. Dividends with
            ``payment_date`` outside the target CY are silently
            ignored — a multi-period statement may emit dividends
            from before Jan 1 or after Dec 31.
        calendar_year: The CY being reported (e.g., ``2025`` for AY
            2026-27). Drives the date-window filter.
        fx_policy: The :class:`FxPolicy` used to resolve the December
            31 SBI TTBR. The Schedule FA disclosure conversion is
            separate from the Rule 115 conversion used for taxable
            dividend income in Schedule OS.

    Returns:
        ``{symbol -> total INR}``. Each value is the exact
        :class:`~decimal.Decimal` product of the calendar-year
        foreign-currency total and the December 31 TTBR (#1312); the row's
        ``filed`` value rounds it HALF_UP. Symbols with no in-CY dividends
        are omitted from the dict; the caller should treat a missing key as
        the literal :class:`Decimal` 0.

    Raises:
        InvariantError: When a symbol's total can't be added exactly, the
            refusal names the payment's 1-based position in ``dividends``
            and its ``source_row_index`` when it has one. When a total can't
            be converted exactly, it names the symbol total's 1-based
            position in the result.
        MissingFxRateError: Propagated from
            :meth:`FxPolicy.rate_for` when at least one in-CY payment
            exists but the December 31 rate cannot be sourced.
    """
    cy_start = date(calendar_year, 1, 1)
    cy_end = date(calendar_year, 12, 31)
    totals: dict[Ticker, Decimal] = {}
    in_cy_count = 0
    for position, div in enumerate(dividends, start=1):
        if not (cy_start <= div.payment_date <= cy_end):
            continue
        in_cy_count += 1
        totals[div.symbol] = checked_addition(
            totals.get(div.symbol, Decimal("0")),
            div.amount_gross,
            at=f"Schedule FA per-symbol dividends, input row {position}, column amount_gross",
            source_row_index=div.source_row_index,
        )
    if not totals:
        return {}

    rate = fx_policy.rate_for(cy_end)
    out = {
        symbol: checked_product(
            total, rate, at=f"Schedule FA per-symbol dividends, symbol total {position}, INR"
        )
        for position, (symbol, total) in enumerate(totals.items(), start=1)
    }
    # Postcondition: every output value is strictly positive
    # (full-precision product of strictly-positive Decimals).
    # No risk of zero entries because the compute layer does not quantize.
    for symbol, total in out.items():
        require(
            total > 0,
            what=(
                "aggregate_dividends_by_symbol_inr post: every per-symbol "
                "total must be positive (positive calendar-year foreign-currency "
                "total converted at the December 31 TTBR)"
            ),
            symbol=symbol,
            total=total,
        )
    # Postcondition: output keys form a subset of in-CY dividend
    # symbols. Equivalent to "no symbol appears in output unless it
    # had at least one in-CY dividend payment".
    require(
        len(out) <= in_cy_count,
        what=(
            "aggregate_dividends_by_symbol_inr post: per-symbol output "
            "count cannot exceed per-payment in-CY input count"
        ),
        output_count=len(out),
        in_cy_count=in_cy_count,
    )
    return out


def us_source_dividend_count(dividends: Iterable[Dividend]) -> int:
    """Count dividends with a US ISIN (ISO 6166: the first two characters
    are the issuing country).

    A heuristic: an ADR carries a US ISIN, so it counts as US-source even
    when the issuer is foreign. Feeds the CLI advisory only (#145).
    """
    return sum(1 for d in dividends if d.isin.startswith("US"))


def us_source_withholding_tax_net_paid(withholding_tax: Iterable[WithholdingTax]) -> Decimal:
    """Net US-source withholding tax paid in the period, floored at zero.

    Keyed by the US ISIN prefix, as :func:`us_source_dividend_count`.
    :attr:`WithholdingTax.amount` is signed — negative for tax paid,
    positive for reversals/refunds — so summing handles three cases
    uniformly:

    (a) all-paid rows: the signed sum is negative, so the net paid is
        positive.
    (b) all-reversal rows: the signed sum is positive, so the floor gives 0
        (PR #152 Codex Tier-5 P1: a refund-only period must not look like
        withholding occurred).
    (c) a paid + reversal pair that cancels: the signed sum is zero, so 0
        (PR #152 Codex Tier-3 P2: in-period self-correction is not WHT).

    The CLI advisory gates on a positive result: did the user actually pay
    US WHT this period that they need to credit?
    """
    signed_net = sum((w.amount for w in withholding_tax if w.isin.startswith("US")), Decimal("0"))
    return max(-signed_net, Decimal("0"))
