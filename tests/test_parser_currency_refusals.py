"""IBKR currency refusals name no cell (#1027).

A column-shifted row can put the holder's name or account number in a
Currency cell, and a short name such as ``ANN`` looks exactly like a currency
code. So every refusal about a row's currency keeps its section, column and
row, and its message and ``raw_value`` hold no cell text. Every row here is
synthetic.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

import pytest

from ibkr_fa.exceptions import ParseError
from ibkr_fa.parser import parse_closed_lots, parse_open_positions
from ibkr_fa.parser.activity_statement import (
    KnownActivitySection,
    build_dec31_close_prices,
    build_instrument_lookup,
    parse_activity_statement_dividends,
    parse_activity_statement_interest,
    parse_activity_statement_trades,
    parse_activity_statement_withholding_tax,
)
from ibkr_fa.parser.activity_statement.multi_section import Section
from tests._flex_lot_rows import closing_execution_for
from tests._income_blocks import block, closed
from tests._statement_period import ALL_DATES

_USD_ONLY = (
    "the currency isn't USD; v1 supports USD only. Multi-currency support is "
    "tracked at https://github.com/jay-tau/ibkr-fa/issues/52."
)
_INTEREST_SHAPE = (
    "Activity Statement Interest column 'Currency' must be 3 uppercase ASCII letters (ISO 4217)."
)
# Distinct values that must never reach str(error). Short values such as
# 'US' are checked by the exact message instead: 'USD only' contains 'US'.
_MARKERS = ("Synthetic Holder Two", "U1234567", "ANN")
_MALFORMED = ("Synthetic Holder Two", "U1234567", "usd", "US", "USDX", "ÜSD")
_WELL_FORMED = ("ANN", "EUR")

_FLEX_INSTRUMENT = {
    "FXRateToBase": "1",
    "AssetClass": "STK",
    "SubCategory": "COMMON",
    "Symbol": "XQAQ",
    "Description": "SAMPLE ISSUER AQ INC",
    "CUSIP": "99992A109",
    "ISIN": "US99992A1097",
    "ListingExchange": "NASDAQ",
    "IssuerCountryCode": "US",
}


def _flex_open_lot(currency: str) -> int:
    row = {
        **_FLEX_INSTRUMENT,
        "CurrencyPrimary": currency,
        "ReportDate": "20251231",
        "Quantity": "3",
        "MarkPrice": "250",
        "PositionValue": "750",
        "CostBasisPrice": "200",
        "CostBasisMoney": "600",
        "Side": "Long",
        "LevelOfDetail": "LOT",
        "OpenDateTime": "20250602",
        "HoldingPeriodDateTime": "20250602",
    }
    return len(parse_open_positions([row]))


def _flex_closed_lot(currency: str) -> int:
    row = {
        **_FLEX_INSTRUMENT,
        "CurrencyPrimary": currency,
        "ReportDate": "20250910",
        "TradeDate": "20250910",
        "SettleDateTarget": "",
        "TransactionType": "",
        "Quantity": "2",
        "TradePrice": "230",
        "TradeMoney": "",
        "Proceeds": "",
        "Taxes": "",
        "IBCommission": "",
        "IBCommissionCurrency": "",
        "NetCash": "",
        "ClosePrice": "",
        "Open/CloseIndicator": "C",
        "CostBasis": "400",
        "FifoPnlRealized": "60",
        "Buy/Sell": "SELL",
        "LevelOfDetail": "CLOSED_LOT",
        "OpenDateTime": "20250602;093000",
        "HoldingPeriodDateTime": "20250602;093000",
    }
    # The lot first, so a refusal names its row 0 like the other sections' rows.
    return len(parse_closed_lots([row, closing_execution_for(row)]))


# Each income section's parser, returning how many records it parsed.
_INCOME_PARSERS: dict[KnownActivitySection, tuple[Callable[[Section], int], str, str]] = {
    KnownActivitySection.DIVIDENDS: (
        lambda rows: len(parse_activity_statement_dividends(rows, period=ALL_DATES)),
        "XQAQ(US99992A1097) Cash Dividend 0.25 per Share (Ordinary Dividend)",
        "0.75",
    ),
    KnownActivitySection.WITHHOLDING_TAX: (
        lambda rows: len(parse_activity_statement_withholding_tax(rows, period=ALL_DATES)),
        "XQAQ(US99992A1097) Cash Dividend 0.25 per Share - US Tax",
        "-0.19",
    ),
    KnownActivitySection.INTEREST: (
        lambda rows: sum(map(len, parse_activity_statement_interest(rows, period=ALL_DATES))),
        "Credit Interest for Jun-2025",
        "1.10",
    ),
}


def _income(kind: KnownActivitySection, currency: str) -> int:
    parse, description, amount = _INCOME_PARSERS[kind]
    row = {"Currency": currency, "Date": "2025-06-30", "Description": description, "Amount": amount}
    return parse(block(kind, closed([row])))


def _close_price(currency: str) -> int:
    row = {
        "DataDiscriminator": "Summary",
        "Asset Category": "Stocks",
        "Currency": currency,
        "Symbol": "XQAQ",
        "Quantity": "3",
        "Mult": "1",
        "Cost Price": "200",
        "Cost Basis": "600",
        "Close Price": "250",
        "Value": "750",
        "Unrealized P/L": "150",
        "Code": "",
    }
    return len(build_dec31_close_prices([row]))


def _trade(currency: str) -> int:
    instruments = build_instrument_lookup(
        [
            {
                "Asset Category": "Stocks",
                "Symbol": "XQAQ",
                "Description": "SAMPLE ISSUER AQ INC",
                "Conid": "1",
                "Security ID": "US99992A1097",
                "Underlying": "XQAQ",
                "Listing Exch": "NASDAQ",
                "Multiplier": "1",
                "Type": "COMMON",
                "Code": "",
            }
        ]
    )
    row = {
        "DataDiscriminator": "Order",
        "Asset Category": "Stocks",
        "Currency": currency,
        "Symbol": "XQAQ",
        "Date/Time": "2025-06-02, 09:30:00",
        "Quantity": "3",
        "T. Price": "200",
        "C. Price": "201",
        "Proceeds": "-600",
        "Comm/Fee": "-1",
        "Basis": "601",
        "Realized P/L": "0",
        "MTM P/L": "3",
        "Code": "O",
    }
    return len(parse_activity_statement_trades([row], instruments=instruments, period=ALL_DATES))


@dataclass(frozen=True)
class _Site:
    section: str
    column: str
    parse: Callable[[str], int]
    strips: bool
    """Whether the parser strips the cell before its USD check."""


_SITES = {
    "flex-open-positions": _Site("Open Positions (LOT)", "CurrencyPrimary", _flex_open_lot, False),
    "flex-closed-lots": _Site("Trades", "CurrencyPrimary", _flex_closed_lot, False),
    "dividends": _Site(
        "Dividends", "Currency", lambda c: _income(KnownActivitySection.DIVIDENDS, c), True
    ),
    "withholding-tax": _Site(
        "Withholding Tax",
        "Currency",
        lambda c: _income(KnownActivitySection.WITHHOLDING_TAX, c),
        True,
    ),
    "interest-credit": _Site(
        "Interest", "Currency", lambda c: _income(KnownActivitySection.INTEREST, c), True
    ),
    "open-positions-close-price": _Site("Open Positions", "Currency", _close_price, False),
    "trades": _Site("Trades", "Currency", _trade, False),
}


def _refused(name: str, site: _Site) -> tuple[str, ...]:
    # Interest refuses a malformed cell first; see the shape tests below.
    values = _WELL_FORMED if name == "interest-credit" else _MALFORMED + _WELL_FORMED
    return values if site.strips else (*values, "USD\n")


def _refusal(parse: Callable[[], object]) -> ParseError:
    with pytest.raises(ParseError) as exc:
        parse()
    for marker in _MARKERS:
        assert marker not in str(exc.value)
    return exc.value


@pytest.mark.parametrize(
    ("site", "value"),
    [
        pytest.param(site, value, id=f"{name}-{value!r}")
        for name, site in _SITES.items()
        for value in _refused(name, site)
    ],
)
def test_a_currency_other_than_usd_refuses_without_the_cell(site: _Site, value: str) -> None:
    error = _refusal(lambda: site.parse(value))
    assert (error.section, error.column, error.row_index) == (site.section, site.column, 0)
    assert (error.message, error.raw_value) == (_USD_ONLY, "")


@pytest.mark.parametrize(
    ("site", "value"),
    [
        pytest.param(site, value, id=f"{name}-{value!r}")
        for name, site in _SITES.items()
        for value in ("USD", *((" USD\n",) if site.strips else ()))
    ],
)
def test_usd_parses(site: _Site, value: str) -> None:
    assert site.parse(value) == 1


@pytest.mark.parametrize(
    "parse", [_close_price, _trade], ids=["open-positions-close-price", "trades"]
)
def test_a_whitespace_only_trades_currency_refuses_as_empty_without_the_cell(
    parse: Callable[[str], int],
) -> None:
    error = _refusal(lambda: parse("\t "))
    assert (error.column, error.message, error.raw_value) == ("Currency", "must be non-empty", "")


# ---- Interest checks non-footer Currency cells before routing ----------------

_INTEREST_ROWS = {
    "credit": ("Credit Interest for Jun-2025", "1.10"),
    "debit": ("Debit Interest for Jun-2025", "-0.40"),
    "accrual": ("Interest Accrual", "0.05"),
    "correction": ("Credit Interest for May-2025", "-0.20"),
}


@pytest.mark.parametrize("value", _MALFORMED)
@pytest.mark.parametrize("kind", sorted(_INTEREST_ROWS))
def test_a_malformed_interest_currency_refuses_on_every_row(kind: str, value: str) -> None:
    description, amount = _INTEREST_ROWS[kind]
    row = {"Currency": value, "Date": "2025-06-30", "Description": description, "Amount": amount}
    error = _refusal(
        lambda: parse_activity_statement_interest(
            block(KnownActivitySection.INTEREST, closed([row])), period=ALL_DATES
        )
    )
    assert (error.section, error.column, error.row_index) == ("Interest", "Currency", 0)
    assert (error.message, error.raw_value) == (_INTEREST_SHAPE, "")
