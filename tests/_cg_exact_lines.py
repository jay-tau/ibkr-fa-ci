"""Schedule CG lines that carry exact amounts (#1241).

A Robinhood lot's share of a cost or sale split by quantity can be a rational
such as a third. Its line then carries
:class:`~ibkr_fa.models.schedule_cg_row.ScheduleCgExactAmounts`, and each
Decimal field is :func:`~ibkr_fa.compute.exact_inr.exact_decimal` of its
rational in the caller's context. The dates follow #1241's repros: STCG lots
bought on 2025-01-10, LTCG lots on 2022-12-01, each converted at the rate of
the month end before the sale.
"""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal
from fractions import Fraction

from ibkr_fa.compute.exact_inr import exact_decimal
from ibkr_fa.models.schedule_cg_ltcg_row import ScheduleCgLtcgLine
from ibkr_fa.models.schedule_cg_row import ScheduleCgExactAmounts, ScheduleCgStcgLine
from ibkr_fa.types import Isin, Ticker


def exact_amounts(
    cost_usd: Fraction | int, proceeds_usd: Fraction | int, rate: str
) -> ScheduleCgExactAmounts:
    """The exact amounts of a lot costing ``cost_usd`` and sold for ``proceeds_usd`` at ``rate``."""
    cost, proceeds = Fraction(cost_usd), Fraction(proceeds_usd)
    fx = Fraction(Decimal(rate))
    return ScheduleCgExactAmounts(
        cost_basis_usd=cost,
        proceeds_usd=proceeds,
        cost_basis_inr=cost * fx,
        proceeds_inr=proceeds * fx,
        realized_gain_inr=(proceeds - cost) * fx,
    )


def _line[LineT: (ScheduleCgStcgLine, ScheduleCgLtcgLine)](
    model: type[LineT],
    opened: date,
    *,
    cost_usd: Fraction | int,
    proceeds_usd: Fraction | int,
    rate: str,
    sold: date,
    quantity: str,
) -> LineT:
    exact = exact_amounts(cost_usd, proceeds_usd, rate)
    specified = sold.replace(day=1) - timedelta(days=1)
    return model(
        symbol=Ticker("XQAQ"),
        isin=Isin("US99992A1097"),
        issuer_country_iso2="US",
        description="SAMPLE ISSUER AQ INC",
        open_date=opened,
        close_date=sold,
        holding_period_days=(sold - opened).days,
        quantity=Decimal(quantity),
        cost_basis_usd=exact_decimal(exact.cost_basis_usd),
        proceeds_usd=exact_decimal(exact.proceeds_usd),
        fx_specified_date=specified,
        fx_rate_publication_date=specified,
        fx_rate_inr_per_usd=Decimal(rate),
        cost_basis_inr=exact_decimal(exact.cost_basis_inr),
        cost_basis_inr_source="test basis",
        proceeds_inr=exact_decimal(exact.proceeds_inr),
        realized_gain_inr=exact_decimal(exact.realized_gain_inr),
        exact_amounts=exact,
    )


def exact_stcg_line(
    *,
    cost_usd: Fraction | int,
    proceeds_usd: Fraction | int,
    rate: str,
    sold: date,
    quantity: str = "1",
) -> ScheduleCgStcgLine:
    """An STCG line bought on 2025-01-10 with exact amounts."""
    return _line(
        ScheduleCgStcgLine,
        date(2025, 1, 10),
        cost_usd=cost_usd,
        proceeds_usd=proceeds_usd,
        rate=rate,
        sold=sold,
        quantity=quantity,
    )


def exact_ltcg_line(
    *,
    cost_usd: Fraction | int,
    proceeds_usd: Fraction | int,
    rate: str,
    sold: date,
    quantity: str = "1",
) -> ScheduleCgLtcgLine:
    """An LTCG line bought on 2022-12-01 with exact amounts."""
    return _line(
        ScheduleCgLtcgLine,
        date(2022, 12, 1),
        cost_usd=cost_usd,
        proceeds_usd=proceeds_usd,
        rate=rate,
        sold=sold,
        quantity=quantity,
    )
