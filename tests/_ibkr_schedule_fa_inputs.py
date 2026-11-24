"""IBKR Schedule FA inputs that several test modules share (#1867).

- ``CY_2025_NAV_INPUT``: the path of the synthetic calendar-year 2025 Activity
  Statement with positions and NAV;
- ``SCHEDULE_FA_CONFIG``: a user config with every Schedule FA value the CBDT JSON
  fragment needs;
- ``SCHEDULE_FA_CONFIG_NO_PEAK``: the same config without ``[schedule_fa]``, so the
  A2 peak is unsupplied;
- ``flat_close_fetcher``: a price-fetcher double that returns a flat close on every
  US market session.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

from ibkr_fa.prices.series import PriceSeries
from ibkr_fa.reference.us_market_calendar import us_market_sessions
from tests._ibkr_user_toml import CUSTODIAN_TOML

CY_2025_NAV_INPUT = Path(__file__).parent / "fixtures" / "e2e_activity_statement_with_nav_2025.csv"

SCHEDULE_FA_CONFIG = (
    """\
[account]
number = "U12345678"
holder_name = "Sample Holder"
opening_date = 2020-01-01
status = "BeneficialOwner"
"""
    + CUSTODIAN_TOML
    + """
[schedule_fa]
manual_values_account_number = "U12345678"
[[schedule_fa.manual_values]]
calendar_year = 2025
a2_peak_balance_inr = 250000

[[fa.entities]]
isin = "US99995B1044"
address = "1 Sample Issuer Street, Sample City, XX"
zip = "00001"
nature_of_entity = "Company"
[[fa.entities]]
isin = "US99995D1000"
address = "2 Sample Issuer Street, Sample City, XX"
zip = "00002"
nature_of_entity = "Company"
"""
)

# Same as SCHEDULE_FA_CONFIG but without [schedule_fa] (so the A2 peak is unsupplied).
SCHEDULE_FA_CONFIG_NO_PEAK = (
    """\
[account]
number = "U12345678"
holder_name = "Sample Holder"
opening_date = 2020-01-01
status = "BeneficialOwner"
"""
    + CUSTODIAN_TOML
    + """
[[fa.entities]]
isin = "US99995B1044"
address = "1 Sample Issuer Street, Sample City, XX"
zip = "00001"
nature_of_entity = "Company"
[[fa.entities]]
isin = "US99995D1000"
address = "2 Sample Issuer Street, Sample City, XX"
zip = "00002"
nature_of_entity = "Company"
"""
)


def flat_close_fetcher(price: str = "100"):
    """Price-fetcher double returning a flat close on every US market session.

    A close on every session in the requested span (the loader's lookback
    seeds January 1) means the peak compute never hits a staleness gap. With
    the close flat, only the SBI TTBR moves a lot's value from day to day.
    """

    def _fetch(ticker, *, start, end, now, fetch_symbol=None):
        closes = {day: Decimal(price) for day in us_market_sessions(start, end)}
        return PriceSeries(
            ticker=ticker,
            currency="USD",
            closes=closes,
            has_split_in_range=False,
            source="test-double",
            fetched_at=datetime(2026, 6, 1, tzinfo=UTC),
        )

    return _fetch
