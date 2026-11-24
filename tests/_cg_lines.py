"""Shared Schedule CG line and result factories with chosen INR amounts (#615)."""

from __future__ import annotations

from dataclasses import replace
from datetime import date, timedelta
from decimal import Context, Decimal, localcontext
from typing import Final

from ibkr_fa.compute.exact_inr import EXACT_SUM_DIGITS
from ibkr_fa.compute.schedule_cg import ScheduleCgStcgComputeResult
from ibkr_fa.compute.schedule_cg_ltcg import ScheduleCgLtcgComputeResult
from ibkr_fa.models.schedule_cg_ltcg_row import ScheduleCgLtcgLine
from ibkr_fa.models.schedule_cg_row import ScheduleCgStcgLine
from ibkr_fa.types import Isin, Ticker


def _exact_gain(proceeds: Decimal, cost: Decimal) -> Decimal:
    """``proceeds - cost`` with every digit, as the line models check it (#1184)."""
    with localcontext(Context(prec=EXACT_SUM_DIGITS)):
        return proceeds - cost


def stcg_line(proceeds_inr: str, cost_basis_inr: str) -> ScheduleCgStcgLine:
    """A valid FY 2025-26 STCG line whose INR proceeds and cost are as given.

    The rate is ₹1/$, so the USD proceeds equal the INR proceeds (#937).
    """
    proceeds, cost = Decimal(proceeds_inr), Decimal(cost_basis_inr)
    return ScheduleCgStcgLine(
        symbol=Ticker("XQCA"),
        isin=Isin("US99972B1017"),
        issuer_country_iso2="US",
        description="SAMPLE ISSUER CA CORP",
        open_date=date(2025, 5, 1),
        close_date=date(2025, 6, 10),
        holding_period_days=40,
        quantity=Decimal("1"),
        cost_basis_usd=Decimal("1"),
        proceeds_usd=proceeds,
        fx_specified_date=date(2025, 5, 31),
        fx_rate_publication_date=date(2025, 5, 31),
        fx_rate_inr_per_usd=Decimal("1"),
        cost_basis_inr=cost,
        cost_basis_inr_source="test basis",
        proceeds_inr=proceeds,
        realized_gain_inr=_exact_gain(proceeds, cost),
    )


def ltcg_line(proceeds_inr: str, cost_basis_inr: str) -> ScheduleCgLtcgLine:
    """A valid FY 2025-26 LTCG line whose INR proceeds and cost are as given.

    The rate is ₹1/$, so the USD proceeds equal the INR proceeds (#937).
    """
    proceeds, cost = Decimal(proceeds_inr), Decimal(cost_basis_inr)
    return ScheduleCgLtcgLine(
        symbol=Ticker("XQAQ"),
        isin=Isin("US99992A1097"),
        issuer_country_iso2="US",
        description="SAMPLE ISSUER AQ INC",
        open_date=date(2023, 4, 1),
        close_date=date(2025, 5, 1),
        holding_period_days=(date(2025, 5, 1) - date(2023, 4, 1)).days,
        quantity=Decimal("1"),
        cost_basis_usd=Decimal("1"),
        proceeds_usd=proceeds,
        fx_specified_date=date(2025, 4, 30),
        fx_rate_publication_date=date(2025, 4, 30),
        fx_rate_inr_per_usd=Decimal("1"),
        cost_basis_inr=cost,
        cost_basis_inr_source="test basis",
        proceeds_inr=proceeds,
        realized_gain_inr=_exact_gain(proceeds, cost),
    )


def stcg_result(proceeds_inr: str, cost_basis_inr: str) -> ScheduleCgStcgComputeResult:
    """An STCG compute result holding one :func:`stcg_line`."""
    return ScheduleCgStcgComputeResult([stcg_line(proceeds_inr, cost_basis_inr)], [], [], [])


def ltcg_result(proceeds_inr: str, cost_basis_inr: str) -> ScheduleCgLtcgComputeResult:
    """An LTCG compute result holding one :func:`ltcg_line`."""
    return ScheduleCgLtcgComputeResult([ltcg_line(proceeds_inr, cost_basis_inr)], [], [], [], [])


def sold_on[LineT: (ScheduleCgStcgLine, ScheduleCgLtcgLine)](line: LineT, sold: date) -> LineT:
    """``line`` sold on ``sold``, its Rule 115 date the day before (#1361)."""
    specified = sold - timedelta(days=1)
    return replace(
        line,
        close_date=sold,
        holding_period_days=(sold - line.open_date).days,
        fx_specified_date=specified,
        fx_rate_publication_date=specified,
    )


# Table F bands (i), (iii) and (iv): ₹100 gains beside a ₹50 loss, netting
# ₹150, so the Table F row is blank for a loss across bands (#1361).
_LOSS_ACROSS_BANDS: Final = (
    (date(2025, 6, 10), "200", "100"),
    (date(2025, 10, 15), "50", "100"),
    (date(2026, 1, 20), "200", "100"),
)


def loss_across_bands_stcg_result() -> ScheduleCgStcgComputeResult:
    """An STCG result whose Table F row 3 is blank for a loss across bands."""
    return ScheduleCgStcgComputeResult(
        [sold_on(stcg_line(proceeds, cost), sold) for sold, proceeds, cost in _LOSS_ACROSS_BANDS],
        [],
        [],
        [],
    )


def loss_across_bands_ltcg_result() -> ScheduleCgLtcgComputeResult:
    """An LTCG result whose Table F row 5 is blank for a loss across bands."""
    return ScheduleCgLtcgComputeResult(
        [sold_on(ltcg_line(proceeds, cost), sold) for sold, proceeds, cost in _LOSS_ACROSS_BANDS],
        [],
        [],
        [],
        [],
    )


# #1166's privacy markers: a symbol, an ISIN-shaped identifier, a description,
# a basis source, a sale date and a 29-digit amount. :func:`marked` puts each
# on a line; no refusal or captured output may print one.
MARKED_AMOUNT: Final = "1.2345678901234567890123456789"
MARKED_DESCRIPTION: Final = "SYNTHDESC2A"
MARKERS: Final = (
    "SYNTHCG2A",
    "US99999Q2001",
    MARKED_DESCRIPTION,
    "SYNTHSOURCE2A",
    "2025-06-17",
    MARKED_AMOUNT,
)


def marked[LineT: (ScheduleCgStcgLine, ScheduleCgLtcgLine)](line: LineT) -> LineT:
    """``line`` with every one of :data:`MARKERS`: sold on 2025-06-17 for
    :data:`MARKED_AMOUNT` (₹1/$) against no cost.

    Built at 60 digits so the derived-field checks keep all 29 digits.
    """
    proceeds, sold = Decimal(MARKED_AMOUNT), date(2025, 6, 17)
    with localcontext(Context(prec=60)):
        return replace(
            line,
            symbol=Ticker("SYNTHCG2A"),
            isin=Isin("US99999Q2001"),
            description=MARKED_DESCRIPTION,
            close_date=sold,
            holding_period_days=(sold - line.open_date).days,
            cost_basis_usd=Decimal("0"),
            cost_basis_inr=Decimal("0"),
            cost_basis_inr_source="SYNTHSOURCE2A",
            proceeds_usd=proceeds,
            proceeds_inr=proceeds,
            realized_gain_inr=proceeds,
        )
