"""The synthetic :class:`Dividend` the Schedule OS and income tests build (#1760)."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from ibkr_fa.models import DistributionKind, Dividend
from ibkr_fa.parser.activity_statement._description import (
    _DESCRIPTION_PATTERN,
    parse_symbol_isin_and_type_slice_from_description,
)
from ibkr_fa.parser.activity_statement.dividends import classify_distribution
from ibkr_fa.types import Isin, Ticker


def make_div(
    *,
    symbol: str = "XQAA",
    isin: str = "US99990A3095",
    payment_date: date = date(2025, 6, 15),
    amount: str = "1.00",
    description: str | None = None,
    source_row_index: int | None = None,
    source_account_index: int | None = None,
) -> Dividend:
    if description is None:
        description = f"{symbol}({isin}) Cash Dividend USD 0.35 per Share"
    # Auto-classify in the test factory so existing tests don't need
    # to thread kind/type_slice through every call. Matches the
    # post-#153 parser behavior: extract type_slice via the
    # structural prefix, then classify the kind. For malformed
    # descriptions that don't match the prefix regex (some tests
    # construct intentionally-bad rows), fall back to defaults
    # (kind=UNRECOGNISED, type_slice="") which the compute layer
    # dispatches to ``violation="missing_kind_prefix"``.
    if _DESCRIPTION_PATTERN.match(description) is not None:
        _, _, type_slice = parse_symbol_isin_and_type_slice_from_description(
            description, section="Dividends", row_index=0
        )
        kind = classify_distribution(type_slice)
    else:
        kind = DistributionKind.UNRECOGNISED
        type_slice = ""
    return Dividend(
        symbol=Ticker(symbol),
        isin=Isin(isin),
        payment_date=payment_date,
        amount_gross=Decimal(amount),
        currency="USD",
        description=description,
        kind=kind,
        type_slice=type_slice,
        source_row_index=source_row_index,
        source_account_index=source_account_index,
    )
