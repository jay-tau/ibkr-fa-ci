"""The hand-built Schedule OS result, its line builders and its output pins,
shared by the Schedule OS output pin tests and the portal writer parity tests.

- ``HAND_BUILT_OS_PINS``: the four format digests of the hand-built result's
  output, keyed by format id.
- ``foreign_dividend_line``: a foreign cash dividend line.
- ``domestic_dividend_line``: an Indian-source cash dividend line, with an
  optional ADR underlying issuer.
- ``other_income_line``: a 1e other-income line of a nature.
- ``hand_built_os_result``: the hand-built ``ScheduleOsComputeResult``.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from ibkr_fa.compute.schedule_os import (
    ScheduleOsComputeResult,
    ScheduleOsDividendLine,
    ScheduleOsDomesticDividendLine,
)
from ibkr_fa.compute.schedule_os_other_income import ScheduleOsOtherIncomeLine
from ibkr_fa.models import DistributionKind
from ibkr_fa.types import Isin, Ticker

HAND_BUILT_OS_PINS = {
    "filing-csv": "d7b239a53cb674e9a5f73451c973f4f5ced4698ba6f4e34b178d4e335e84ed38",
    "filing-xlsx": "b780c7770aabf7a7a921a4ea361a45477f0cd5a6ec003b34316477374403bbb9",
    "review-csv": "bd6b379152134355c9d6f09b9d047f2403b8b1de37cf388657998db976580b5f",
    "xlsx": "7de1284befd7beaebbf747cc5fa25b246ca2021b1d791d2b2504234cfdcf9eb2",
}


def foreign_dividend_line(
    symbol: str,
    isin: str,
    country: str,
    paid: date,
    usd: str,
    rate: str,
    account: int,
) -> ScheduleOsDividendLine:
    return ScheduleOsDividendLine(
        symbol=Ticker(symbol),
        isin=Isin(isin),
        issuer_country_iso2=country,
        payment_date=paid,
        fx_specified_date=date(paid.year, paid.month, 1),
        fx_rate_publication_date=date(paid.year, paid.month, 1),
        fx_rate_inr_per_usd=Decimal(rate),
        amount_gross_usd=Decimal(usd),
        amount_gross_inr=Decimal(usd) * Decimal(rate),
        description=f"{symbol}({isin}) Cash Dividend USD {usd} per Share",
        kind=DistributionKind.CASH_DIVIDEND,
        source_account_index=account,
    )


def domestic_dividend_line(
    symbol: str,
    isin: str,
    paid: date,
    usd: str,
    rate: str,
    account: int,
    *,
    adr_issuer: str | None = None,
) -> ScheduleOsDomesticDividendLine:
    return ScheduleOsDomesticDividendLine(
        symbol=Ticker(symbol),
        isin=Isin(isin),
        issuer_country_iso2="IN",
        payment_date=paid,
        fx_specified_date=date(paid.year, paid.month, 1),
        fx_rate_publication_date=date(paid.year, paid.month, 1),
        fx_rate_inr_per_usd=Decimal(rate),
        amount_gross_usd=Decimal(usd),
        amount_gross_inr=Decimal(usd) * Decimal(rate),
        description=f"{symbol}({isin}) Cash Dividend USD {usd} per Share",
        kind=DistributionKind.CASH_DIVIDEND,
        adr_source_isin=None if adr_issuer is None else Isin(isin),
        adr_underlying_issuer_name=adr_issuer,
        source_account_index=account,
    )


def other_income_line(
    nature: str, paid: date, usd: str, row: int | None
) -> ScheduleOsOtherIncomeLine:
    return ScheduleOsOtherIncomeLine(
        nature=nature,
        payment_date=paid,
        fx_specified_date=date(2026, 3, 31),
        fx_rate_publication_date=date(2026, 3, 27),
        fx_rate_inr_per_usd=Decimal("85.47"),
        amount_gross_usd=Decimal(usd),
        amount_gross_inr=Decimal(usd) * Decimal("85.47"),
        description=f"{nature} {usd}",
        source_row_index=row,
        source_country_iso2=None,
    )


def hand_built_os_result() -> ScheduleOsComputeResult:
    """Two accounts, foreign and Indian-source lines, and two 1e natures.

    The fractional INR amounts leave remainders in every period, so the
    total pass and both Quicko group passes all allocate leftover rupees.
    """
    return ScheduleOsComputeResult(
        lines=[
            foreign_dividend_line(
                "XQBG", "US99995E1082", "US", date(2025, 5, 15), "1.21", "83.17", 0
            ),
            foreign_dividend_line(
                "XQBG", "US99995E1082", "US", date(2026, 3, 5), "1.46", "90.56", 0
            ),
            foreign_dividend_line(
                "XQBA", "US99995C1027", "US", date(2025, 8, 19), "2.63", "83.82", 1
            ),
            foreign_dividend_line(
                "XQBA", "US99995C1027", "US", date(2025, 11, 18), "0.36", "84.58", 1
            ),
            foreign_dividend_line(
                "XQCX", "GB99970P1026", "GB", date(2025, 9, 22), "0.89", "84.89", 1
            ),
            foreign_dividend_line(
                "XQBG", "US99995E1082", "US", date(2026, 3, 20), "0.15", "86.41", 1
            ),
        ],
        domestic_lines=[
            domestic_dividend_line(
                "XQCD",
                "US99972A1034",
                date(2025, 6, 1),
                "0.48",
                "85.33",
                0,
                adr_issuer="SAMPLE ISSUER CD Limited",
            ),
            domestic_dividend_line("XQDK", "IN99970V1094", date(2025, 8, 20), "0.71", "85.21", 1),
        ],
        interest_total_inr_rounded=Decimal("280"),
        other_income_lines=(
            other_income_line(
                "Substitute payment in lieu of dividend", date(2025, 7, 10), "0.27", None
            ),
            other_income_line("Securities lending fee", date(2025, 6, 1), "0.11", 1),
            other_income_line("Securities lending fee", date(2025, 12, 1), "0.06", 2),
        ),
    )
