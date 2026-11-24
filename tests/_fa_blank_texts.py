"""The cause and remedy each blank Schedule FA Table A3 peak and Table A2
closing balance gets, as several test modules check them, written out here
rather than imported so the pins stay independent. Each caller keeps its own
prefix, row list and layout.
"""

from collections.abc import Mapping
from typing import Final

from ibkr_fa.models import FaA3PeakBlankReason
from ibkr_fa.models.fa_a2_row import FaA2ClosingBlankReason

# The remedy several blank-peak reasons share.
PEAK_BY_HAND: Final = "Enter it by hand; a ticker override or a re-fetch won't help."
# The by-hand route every blank-closing remedy ends with.
A2_BY_HAND: Final = (
    "enter it by hand in the review CSV or XLSX, or in the portal: the account's Net "
    "Asset Value on 31 December of the requested calendar year, in its base currency, "
    "converted to INR at that date's SBI TTBR."
)
# The remedy most blank-closing reasons share.
A2_REEXPORT: Final = (
    "If the file was edited or cut short, re-export the Activity Statement from IBKR "
    f"and re-run. Otherwise, {A2_BY_HAND}"
)

# (cause, remedy) for each blank-peak reason.
A3_PEAK_BLANK_TEXT: Final[Mapping[FaA3PeakBlankReason, tuple[str, str]]] = {
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
        PEAK_BY_HAND,
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
        PEAK_BY_HAND,
    ),
    FaA3PeakBlankReason.EMPTY_WINDOW: (
        "the lot wasn't held at the end of any day in the year (bought and sold on the "
        "same day, or sold on 1 January)",
        PEAK_BY_HAND,
    ),
    FaA3PeakBlankReason.DISPOSAL_DAY_PEAK: (
        "the peak falls on the disposal day, and the Schedule FA text doesn't settle "
        "whether that day counts",
        PEAK_BY_HAND,
    ),
    FaA3PeakBlankReason.SESSION_WITHOUT_HIGH: (
        'peak_basis = "high" needs a valid intraday high for every US market session in '
        "the holding window, and one is missing",
        "Re-run with price fetching and an empty --price-cache-dir (or first move each such "
        "holding's cache file, and any .lock beside it, out of the price cache directory: a "
        'cached series is reused even without highs), set [schedule_fa] peak_basis = "close", '
        "or enter it by hand. A ticker override won't help.",
    ),
    FaA3PeakBlankReason.HIGH_BELOW_CLOSING: (
        "peak_basis = \"high\" found the price series' peak below the statement's "
        "closing value, so the two sources disagree",
        PEAK_BY_HAND,
    ),
}

# (cause, remedy) for each blank-closing reason.
A2_CLOSING_BLANK_TEXT: Final[Mapping[FaA2ClosingBlankReason, tuple[str, str]]] = {
    FaA2ClosingBlankReason.NO_ASSET_CLASS_BLOCK: (
        "the statement has no Net Asset Value section, or that section has no "
        "asset-class table with a 'Current Total' column",
        "Re-export the Activity Statement from IBKR with its Net Asset Value "
        f"section and re-run. Otherwise, {A2_BY_HAND}",
    ),
    FaA2ClosingBlankReason.MULTIPLE_ASSET_CLASS_BLOCKS: (
        "the Net Asset Value section has more than one asset-class table with a "
        "'Current Total' column, so the closing figure is ambiguous",
        A2_REEXPORT,
    ),
    FaA2ClosingBlankReason.MULTIPLE_TOTAL_ROWS: (
        "the Net Asset Value table has more than one 'Total' row, so the closing "
        "figure is ambiguous",
        A2_REEXPORT,
    ),
    FaA2ClosingBlankReason.BLANK_TOTAL: (
        "the Net Asset Value table's 'Total' row has a blank 'Current Total'",
        A2_REEXPORT,
    ),
    FaA2ClosingBlankReason.NO_ASSET_CLASS_ROWS: (
        "the Net Asset Value table has no 'Total' row and no asset-class rows",
        A2_REEXPORT,
    ),
    FaA2ClosingBlankReason.BLANK_ASSET_CLASS_AMOUNT: (
        "the Net Asset Value table has no 'Total' row, and an asset-class row has "
        "a blank 'Current Total', so the rows can't confirm the 'Change in NAV' "
        "'Ending Value'",
        A2_REEXPORT,
    ),
    FaA2ClosingBlankReason.MULTIPLE_ENDING_VALUES: (
        "the 'Change in NAV' section has more than one 'Ending Value', so the "
        "closing figure is ambiguous",
        A2_REEXPORT,
    ),
    FaA2ClosingBlankReason.NO_ENDING_VALUE: (
        "the Net Asset Value table has no 'Total' row, and there is no 'Change in "
        "NAV' 'Ending Value' to confirm the asset-class rows' sum",
        "Re-export the Activity Statement from IBKR with its Change in NAV "
        f"section and re-run. Otherwise, {A2_BY_HAND}",
    ),
    FaA2ClosingBlankReason.BLANK_ENDING_VALUE: (
        "the 'Change in NAV' section's 'Ending Value' is blank",
        A2_REEXPORT,
    ),
    FaA2ClosingBlankReason.FLEX_QUERY: (
        "the input is a Flex Query, whose Net Asset Value isn't read",
        "Re-run with the calendar year's Activity Statement instead of the Flex "
        f"Query. Otherwise, {A2_BY_HAND}",
    ),
    FaA2ClosingBlankReason.NON_USD_BASE: (
        "the account's base currency isn't USD",
        f"Only a USD base is converted, so {A2_BY_HAND}",
    ),
}
