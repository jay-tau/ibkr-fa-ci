"""Synthetic Schedule FA lots and FX, shared by the Table A2, Table A3, exact
FA sum and filed-value tests.

- ``fa_closed_lot``: a USD lot closed on 15 June of ``year``, opened a year
  earlier, with ``gross_proceeds_money`` as given.
- ``make_open_lot``: a USD lot still open on the 31 Dec 2025 report date.
- ``fx_for``: a ``CalendarPolicy`` with rates for the default lots' open and
  close dates and for 31 Dec 2025.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from ibkr_fa.models import BasisProvenance, ClosedLot, OpenLot, Side
from ibkr_fa.types import CountryCode, Isin, Ticker
from tests._fx_stubs import CalendarPolicy


def fa_closed_lot(year: int, gross_proceeds_money: Decimal | None = None) -> ClosedLot:
    return ClosedLot(
        currency_primary="USD",
        sub_category="COMMON",
        symbol=Ticker("XQAT"),
        description="SAMPLE ISSUER AT INC",
        isin=Isin("US99990G1085"),
        listing_exchange="NASDAQ",
        issuer_country_code=CountryCode("US"),
        quantity=Decimal("4"),
        cost_basis_money=Decimal("300"),
        proceeds_money=Decimal("320"),
        open_date_time=date(year - 1, 6, 15),
        close_date_time=date(year, 6, 15),
        gross_proceeds_money=gross_proceeds_money,
        basis_provenance=BasisProvenance.ENGINE_FIFO,
    )


def make_open_lot(
    *,
    symbol: str = "XQAW",
    description: str = "SAMPLE ISSUER AW INC",
    isin: str = "US99990E1038",
    iso_country: str = "US",
    open_date: date = date(2024, 6, 15),
    quantity: Decimal = Decimal("15"),
    mark_price: Decimal = Decimal("32.70"),
    cost: Decimal = Decimal("386.45"),
) -> OpenLot:
    return OpenLot(
        currency_primary="USD",
        symbol=Ticker(symbol),
        description=description,
        isin=Isin(isin),
        issuer_country_code=CountryCode(iso_country),
        report_date=date(2025, 12, 31),
        quantity=quantity,
        mark_price=mark_price,
        cost_basis_money=cost,
        side=Side.LONG,
        open_date_time=open_date,
    )


def fx_for() -> CalendarPolicy:
    """Default FxPolicy with rates for the standard test dates."""
    return CalendarPolicy(
        {
            date(2024, 6, 15): Decimal("83.50"),
            date(2025, 6, 15): Decimal("84.30"),
            date(2025, 12, 31): Decimal("85.00"),
        }
    )
