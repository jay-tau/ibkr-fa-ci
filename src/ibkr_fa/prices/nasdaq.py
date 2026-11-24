"""Fetch mutual-fund daily NAV history from Nasdaq's public JSON endpoint.

Price values (#1138): a row's ``close`` and ``high`` are read only as JSON
strings in Nasdaq's whole price format, checked as received: an optional
leading ``$``, ASCII digits either ungrouped or grouped by commas in threes
(``1,234``), and an optional ``.`` with one or more digits. No sign,
exponent, underscore, space or other character, and the value must be above
zero. The exact text ``N/A``, in any case, means no value: no close, or no
high, for that date. An absent ``high`` key means no high. A present high is
checked even when the close is ``N/A``, and a high below its close refuses.
Any other value, including a JSON number or ``null``, refuses with
:class:`NasdaqPriceFormatError`.
"""

from __future__ import annotations

import json
import re
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from typing import Final, Literal, cast

from ibkr_fa.exceptions import IbkrFaError
from ibkr_fa.number_checks import NumberKind, number_limit_breach
from ibkr_fa.prices.series import PriceSeries, PriceSourceContradictionError
from ibkr_fa.types import Ticker

_URL: Final[str] = "https://api.nasdaq.com/api/quote/{symbol}/historical"
_USER_AGENT: Final[str] = "Mozilla/5.0"
_TIMEOUT_SECONDS: Final[float] = 20.0
_HTTP_OK: Final[int] = 200
# Nasdaq's whole price format, as the module docstring states.
_PRICE_TEXT: Final[re.Pattern[str]] = re.compile(
    r"\$?(?:[0-9]+|[0-9]{1,3}(?:,[0-9]{3})+)(?:\.[0-9]+)?"
)
# Nasdaq's marker for a date with no value in that column.
_NO_VALUE: Final[frozenset[str]] = frozenset({"N/A", "N/a", "n/A", "n/a"})
UrlOpener = Callable[[str, float], bytes | None]


@dataclass(frozen=True, slots=True)
class NasdaqPriceFormatError(IbkrFaError):
    """A Nasdaq ``close`` or ``high`` isn't a usable price (#1138).

    The message names the row's 1-based position in ``data.tradesTable.rows``
    and the column, but not the symbol, the date or the value, which are
    input (#1047).

    Args:
        row: The row's 1-based position in ``data.tradesTable.rows``.
        column: ``"close"`` or ``"high"``.
        below_close: The high is a price but below the row's close.
        too_many_digits: The value is a price in Nasdaq's format but past a
            unit price's read-time digit limits (#1276).
    """

    row: int
    column: Literal["close", "high"]
    below_close: bool = False
    too_many_digits: bool = False

    def __str__(self) -> str:
        if self.below_close:
            problem = "the high is below the close"
        elif self.too_many_digits:
            problem = NumberKind.PRICE.refusal
        else:
            problem = (
                "the value isn't a price in Nasdaq's format ($, digits with comma "
                "thousands, optional decimals, above zero)"
            )
        return f"Nasdaq price response, tradesTable row {self.row}, column {self.column}: {problem}"


def _open(url: str, timeout: float) -> bytes | None:
    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": _USER_AGENT,
            "Accept": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.read()
    except (urllib.error.URLError, TimeoutError, OSError):
        return None


def _amount(raw: object, *, row: int, column: Literal["close", "high"]) -> Decimal | None:
    """Read one ``close`` or ``high`` value of tradesTable row ``row`` in the
    price format the module docstring states; ``None`` for ``N/A``.

    Raises:
        NasdaqPriceFormatError: For any other value, including a JSON number
            or ``null``.
    """
    if isinstance(raw, str):
        if raw in _NO_VALUE:
            return None
        if _PRICE_TEXT.fullmatch(raw) is not None:
            value = Decimal(raw.removeprefix("$").replace(",", ""))
            if value > 0:
                if number_limit_breach(value, NumberKind.PRICE) is not None:
                    raise NasdaqPriceFormatError(row, column, too_many_digits=True)
                return value
    raise NasdaqPriceFormatError(row, column)


def fetch_nasdaq_mutual_fund_daily_closes_usd(  # noqa: PLR0911, PLR0912
    ticker: Ticker,
    *,
    start: date,
    end: date,
    now: datetime,
    fetch_symbol: str | None = None,
    timeout: float = _TIMEOUT_SECONDS,
    url_opener: UrlOpener | None = None,
) -> PriceSeries | None:
    """Fetch a USD mutual-fund NAV series, refusing malformed or redirected data.

    Every row's date and ``close`` key are read first; a missing or
    unreadable one returns ``None``. Rows outside ``[start, end]`` are then
    skipped with their prices unread. An in-window row is checked for a
    repeated date, then its close and any present high are read as the
    module docstring states.

    Raises:
        PriceSourceContradictionError: If two rows within ``[start, end]``
            carry the same date, whether or not either has a usable close
            (#942).
        NasdaqPriceFormatError: If an in-window row's close or present high
            isn't in Nasdaq's price format, or its high is below its close
            (#1138).
    """
    requested = fetch_symbol or str(ticker)
    query = urllib.parse.urlencode(
        {
            "assetclass": "mutualfunds",
            "fromdate": start.isoformat(),
            "todate": end.isoformat(),
            "limit": "5000",
        }
    )
    url = f"{_URL.format(symbol=requested)}?{query}"
    raw = (url_opener or _open)(url, timeout)
    if raw is None:
        return None
    try:
        payload = json.loads(raw)
        data = payload["data"]
        if (
            payload["status"]["rCode"] != _HTTP_OK
            or str(data["symbol"]).upper() != requested.upper()
        ):
            return None
        rows = data["tradesTable"]["rows"]
    except (KeyError, TypeError, ValueError):
        return None
    if not isinstance(rows, list):
        return None

    closes: dict[date, Decimal] = {}
    highs: dict[date, Decimal] = {}
    days: set[date] = set()
    for position, item in enumerate(rows, start=1):
        if not isinstance(item, dict):
            return None
        row = cast("dict[str, object]", item)
        try:
            day = datetime.strptime(str(row["date"]), "%m/%d/%Y").date()
            raw_close = row["close"]
        except (KeyError, ValueError):
            return None
        if not start <= day <= end:
            continue
        if day in days:
            raise PriceSourceContradictionError("Nasdaq")
        days.add(day)
        close = _amount(raw_close, row=position, column="close")
        high = _amount(row["high"], row=position, column="high") if "high" in row else None
        if close is None:
            continue
        closes[day] = close
        if high is not None:
            if high < close:
                raise NasdaqPriceFormatError(position, "high", below_close=True)
            highs[day] = high
    if not closes:
        return None
    return PriceSeries(
        ticker=ticker,
        currency="USD",
        closes=closes,
        highs=highs,
        has_split_in_range=False,
        source=url,
        fetched_at=now,
    )
