"""The Nasdaq price reply builder, fetcher, valid row and refusal text that the
Nasdaq price tests and the TTBR and price limit tests share. Every value is
synthetic, and the fetcher touches no network.
"""

from __future__ import annotations

import json
from datetime import UTC, date, datetime

from ibkr_fa.prices.nasdaq import fetch_nasdaq_mutual_fund_daily_closes_usd
from ibkr_fa.types import Ticker

_DEFAULT_ROWS: tuple[dict[str, object], ...] = (
    {"date": "12/31/2025", "close": "1", "high": "1"},
    {"date": "01/02/2025", "close": "$1.0000", "high": "$1.0000"},
)


def nasdaq_payload(
    *, symbol: str = "XQCN", rows: tuple[dict[str, object], ...] = _DEFAULT_ROWS
) -> bytes:
    return json.dumps(
        {
            "data": {"symbol": symbol, "tradesTable": {"rows": list(rows)}},
            "status": {"rCode": 200},
        }
    ).encode()


def fetch_nasdaq_rows(*rows: dict[str, object]):
    return fetch_nasdaq_mutual_fund_daily_closes_usd(
        Ticker("XQCN"),
        start=date(2025, 1, 1),
        end=date(2025, 12, 31),
        now=datetime(2026, 1, 1, tzinfo=UTC),
        url_opener=lambda _url, _timeout: nasdaq_payload(rows=rows),
    )


NASDAQ_PRICE_FORMAT = (
    "the value isn't a price in Nasdaq's format ($, digits with comma thousands, "
    "optional decimals, above zero)"
)
NASDAQ_VALID_ROW: dict[str, object] = {"date": "06/16/2025", "close": "$4.8213"}


def nasdaq_row_message(row: int, column: str, problem: str = NASDAQ_PRICE_FORMAT) -> str:
    return f"Nasdaq price response, tradesTable row {row}, column {column}: {problem}"
