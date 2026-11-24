"""Portal labels for the Schedule FA cells a run leaves for manual fill.

Compute reports the gaps through
(:class:`~ibkr_fa.compute.fa_a3.FaA3ManualFillGaps`,
:class:`~ibkr_fa.compute.fa_a2.FaA2ManualFillGaps`); this module names
them, a blank A2 or A3 ZIP Code and a whitespace-only A3 Name of entity
included, the way the CLI's manual-fill warning shows them (#626, #1358,
#1369). It also words
each :class:`~ibkr_fa.models.FaA3PeakBlankReason` once, for the IBKR CLI hint,
CBDT JSON gap and StockPlan summary (#962), and each
:class:`~ibkr_fa.models.fa_a2_row.FaA2ClosingBlankReason` once, for the IBKR
run report and CBDT JSON gap (#1013).
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from types import MappingProxyType
from typing import Final

from ibkr_fa.compute.fa_a2 import FaA2ManualFillGaps
from ibkr_fa.compute.fa_a3 import FaA3ManualFillGaps
from ibkr_fa.models import FaA3PeakBlankReason
from ibkr_fa.models.fa_a2_row import FaA2ClosingBlankReason
from ibkr_fa.types import Isin, Ticker

FA_A3_NAME_OF_ENTITY_COLUMN: Final[str] = "Name of entity"
FA_A3_ADDRESS_OF_ENTITY_COLUMN: Final[str] = "Address of entity"
FA_A3_ZIP_CODE_COLUMN: Final[str] = "ZIP Code"
FA_A3_NATURE_OF_ENTITY_COLUMN: Final[str] = "Nature of entity"
FA_A3_PEAK_VALUE_COLUMN: Final[str] = "Peak value of investment during the Period"
_DIVIDEND_MANUAL_FILL_COLUMN: Final[str] = (
    "Total gross amount paid/credited with respect to the holding during the period"
)
_GROSS_PROCEEDS_MANUAL_FILL_COLUMN: Final[str] = (
    "Total gross proceeds from sale or redemption of investment during the period"
)
# Full generic-IBKR A3 manual-fill list, in portal order. Imported by tests.
MANUAL_FILL_PORTAL_COLUMNS: Final[tuple[str, ...]] = (
    FA_A3_NAME_OF_ENTITY_COLUMN,
    FA_A3_ADDRESS_OF_ENTITY_COLUMN,
    FA_A3_ZIP_CODE_COLUMN,
    FA_A3_NATURE_OF_ENTITY_COLUMN,
    FA_A3_PEAK_VALUE_COLUMN,
    _DIVIDEND_MANUAL_FILL_COLUMN,
    _GROSS_PROCEEDS_MANUAL_FILL_COLUMN,
)

FA_A2_ZIP_CODE_LABEL: Final[str] = "ZIP Code"
FA_A2_PEAK_LABEL: Final[str] = "Peak Balance During the Period"
FA_A2_CLOSING_LABEL: Final[str] = "Closing balance"
_FA_A2_GROSS_LABEL: Final[str] = (
    "Gross amount paid/credited to the account during the period (Nature + Amount)"
)
# Fidelity CMA's own, shorter wording for the A2 gross column.
FIDELITY_CMA_A2_GROSS_LABEL: Final[str] = "Gross amount paid/credited (Nature + Amount)"


_BY_HAND: Final[str] = "Enter it by hand; a ticker override or a re-fetch won't help."
# (cause, remedy) for each blank-peak reason. The cause completes "the peak
# is blank because ..."; the remedy is one or more sentences.
FA_A3_PEAK_BLANK_TEXT: Final[Mapping[FaA3PeakBlankReason, tuple[str, str]]] = MappingProxyType(
    {
        FaA3PeakBlankReason.PRICES_NOT_FETCHED: (
            "price fetching was off (--no-fetch-prices) and no complete price cache was found",
            "Re-run without --no-fetch-prices to compute it.",
        ),
        FaA3PeakBlankReason.NO_USD_SERIES: (
            "the price source returned no USD daily series for the ticker: an unknown, "
            "renamed or non-USD ticker (e.g. a security your broker lists under a different "
            "symbol than the price source), or a failed fetch",
            "If the fetch failed, re-run. If you can VERIFY a USD price-source symbol for "
            "the SAME security (match the ISIN), map it with [[prices.ticker_overrides]] in "
            "user.toml so the peak is computed; a wrong mapping can produce a wrong figure, "
            "so confirm the security first.",
        ),
        FaA3PeakBlankReason.SPLIT: (
            "a stock split falls inside the holding window, or the price series isn't "
            "adjusted to per-date prices, so one lot quantity can't be valued across it",
            _BY_HAND,
        ),
        FaA3PeakBlankReason.SESSION_WITHOUT_CLOSE: (
            "a US market session in the holding window has no close: a dropped bar, a "
            "trading halt or delisting, or, in a year whose US market closures the tool "
            "doesn't list yet, a market holiday",
            "The peak is never guessed: enter it by hand. If the session is a market "
            "holiday the tool doesn't list, adding that year's US market closures lets a "
            "later run compute it. A ticker override or a re-fetch won't help.",
        ),
        FaA3PeakBlankReason.STALE_CLOSE: (
            "the price series has no close within the allowed carry limit for a day in the "
            "holding window (for example, it starts after the window opens or ended before "
            "it: a delisting, a trading halt, or a ticker that names a different security)",
            "If the ticker names a different security, map the right USD price-source "
            "symbol with [[prices.ticker_overrides]] in user.toml; otherwise enter it by hand.",
        ),
        FaA3PeakBlankReason.NO_TTBR: (
            "no SBI TT buying rate can be sourced for a day the peak needs",
            _BY_HAND,
        ),
        FaA3PeakBlankReason.EMPTY_WINDOW: (
            "the lot wasn't held at the end of any day in the year (bought and sold on the "
            "same day, or sold on 1 January)",
            _BY_HAND,
        ),
        FaA3PeakBlankReason.DISPOSAL_DAY_PEAK: (
            "the peak falls on the disposal day, and the Schedule FA text doesn't settle "
            "whether that day counts",
            _BY_HAND,
        ),
        FaA3PeakBlankReason.SESSION_WITHOUT_HIGH: (
            'peak_basis = "high" needs a valid intraday high for every US market session in '
            "the holding window, and one is missing",
            # A matching cache is reused before any fetch, highs or not (#1474).
            "Re-run with price fetching and an empty --price-cache-dir (or first move each such "
            "holding's cache file, and any .lock beside it, out of the price cache directory: a "
            'cached series is reused even without highs), set [schedule_fa] peak_basis = "close", '
            "or enter it by hand. A ticker override won't help.",
        ),
        FaA3PeakBlankReason.HIGH_BELOW_CLOSING: (
            "peak_basis = \"high\" found the price series' peak below the statement's "
            "closing value, so the two sources disagree",
            _BY_HAND,
        ),
    }
)


_A2_BY_HAND: Final[str] = (
    "enter it by hand in the review CSV or XLSX, or in the portal: the account's Net "
    "Asset Value on 31 December of the requested calendar year, in its base currency, "
    "converted to INR at that date's SBI TTBR."
)
_A2_REEXPORT: Final[str] = (
    "If the file was edited or cut short, re-export the Activity Statement from IBKR "
    f"and re-run. Otherwise, {_A2_BY_HAND}"
)
# (cause, remedy) for each blank-closing reason. The cause completes "the
# closing balance is blank because ..."; the remedy is one or more sentences.
# No config value can supply an A2 closing balance, so no remedy names one.
FA_A2_CLOSING_BLANK_TEXT: Final[Mapping[FaA2ClosingBlankReason, tuple[str, str]]] = (
    MappingProxyType(
        {
            FaA2ClosingBlankReason.NO_ASSET_CLASS_BLOCK: (
                "the statement has no Net Asset Value section, or that section has no "
                "asset-class table with a 'Current Total' column",
                "Re-export the Activity Statement from IBKR with its Net Asset Value "
                f"section and re-run. Otherwise, {_A2_BY_HAND}",
            ),
            FaA2ClosingBlankReason.MULTIPLE_ASSET_CLASS_BLOCKS: (
                "the Net Asset Value section has more than one asset-class table with a "
                "'Current Total' column, so the closing figure is ambiguous",
                _A2_REEXPORT,
            ),
            FaA2ClosingBlankReason.MULTIPLE_TOTAL_ROWS: (
                "the Net Asset Value table has more than one 'Total' row, so the closing "
                "figure is ambiguous",
                _A2_REEXPORT,
            ),
            FaA2ClosingBlankReason.BLANK_TOTAL: (
                "the Net Asset Value table's 'Total' row has a blank 'Current Total'",
                _A2_REEXPORT,
            ),
            FaA2ClosingBlankReason.NO_ASSET_CLASS_ROWS: (
                "the Net Asset Value table has no 'Total' row and no asset-class rows",
                _A2_REEXPORT,
            ),
            FaA2ClosingBlankReason.BLANK_ASSET_CLASS_AMOUNT: (
                "the Net Asset Value table has no 'Total' row, and an asset-class row has "
                "a blank 'Current Total', so the rows can't confirm the 'Change in NAV' "
                "'Ending Value'",
                _A2_REEXPORT,
            ),
            FaA2ClosingBlankReason.MULTIPLE_ENDING_VALUES: (
                "the 'Change in NAV' section has more than one 'Ending Value', so the "
                "closing figure is ambiguous",
                _A2_REEXPORT,
            ),
            FaA2ClosingBlankReason.NO_ENDING_VALUE: (
                "the Net Asset Value table has no 'Total' row, and there is no 'Change in "
                "NAV' 'Ending Value' to confirm the asset-class rows' sum",
                "Re-export the Activity Statement from IBKR with its Change in NAV "
                f"section and re-run. Otherwise, {_A2_BY_HAND}",
            ),
            FaA2ClosingBlankReason.BLANK_ENDING_VALUE: (
                "the 'Change in NAV' section's 'Ending Value' is blank",
                _A2_REEXPORT,
            ),
            FaA2ClosingBlankReason.FLEX_QUERY: (
                "the input is a Flex Query, whose Net Asset Value isn't read",
                "Re-run with the calendar year's Activity Statement instead of the Flex "
                f"Query. Otherwise, {_A2_BY_HAND}",
            ),
            FaA2ClosingBlankReason.NON_USD_BASE: (
                "the account's base currency isn't USD",
                f"Only a USD base is converted, so {_A2_BY_HAND}",
            ),
        }
    )
)


def fa_a3_manual_fill_columns(gaps: FaA3ManualFillGaps) -> tuple[str, ...]:
    """The A3 portal columns ``gaps`` leaves blank, in portal order."""
    blank = (
        (FA_A3_NAME_OF_ENTITY_COLUMN, bool(gaps.name_of_entity)),
        (FA_A3_ADDRESS_OF_ENTITY_COLUMN, bool(gaps.address_of_entity)),
        (FA_A3_ZIP_CODE_COLUMN, bool(gaps.zip_code)),
        (FA_A3_NATURE_OF_ENTITY_COLUMN, bool(gaps.nature_of_entity)),
        (FA_A3_PEAK_VALUE_COLUMN, bool(gaps.peak) or gaps.no_rows),
        (_DIVIDEND_MANUAL_FILL_COLUMN, gaps.dividends),
        (_GROSS_PROCEEDS_MANUAL_FILL_COLUMN, bool(gaps.gross_proceeds)),
    )
    return tuple(label for label, is_blank in blank if is_blank)


def manual_fill_holdings(holdings: Iterable[tuple[Ticker, Isin]]) -> tuple[str, ...]:
    """Unique ``SYMBOL (ISIN)`` names for ``holdings``, sorted."""
    return tuple(sorted({f"{ticker} ({isin})" for ticker, isin in holdings}))


def peak_manual_fill_holdings(
    peak: Iterable[tuple[Ticker, Isin, FaA3PeakBlankReason]],
) -> tuple[tuple[FaA3PeakBlankReason, tuple[str, ...]], ...]:
    """:func:`manual_fill_holdings` per blank-peak reason, in reason order."""
    blank = tuple(peak)
    return tuple(
        (reason, names)
        for reason in FaA3PeakBlankReason
        if (
            names := manual_fill_holdings(
                (ticker, isin) for ticker, isin, why in blank if why is reason
            )
        )
    )


def fa_a2_manual_fill_columns(gaps: FaA2ManualFillGaps) -> tuple[str, ...]:
    """The A2 portal columns ``gaps`` leaves blank, in portal order."""
    blank = (
        (FA_A2_ZIP_CODE_LABEL, gaps.zip_code),
        (FA_A2_PEAK_LABEL, gaps.peak),
        (FA_A2_CLOSING_LABEL, gaps.closing),
        (_FA_A2_GROSS_LABEL, gaps.gross),
    )
    return tuple(label for label, is_blank in blank if is_blank)
