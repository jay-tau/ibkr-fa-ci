"""The synthetic IBKR Activity Statement and config of the Form 67, Schedule FSI
and Schedule TR pipeline tests (#1754), and of the Schedule CG and OS tests
(#1764)."""

from __future__ import annotations

import csv
from pathlib import Path

from tests._income_blocks import with_totals

CONFIG_TEMPLATE = """\
[account]
number = "{account_number}"
holder_name = "Test Holder"
opening_date = 2020-01-01
status = "BeneficialOwner"

[fx]
max_carry_forward_days = 4
"""

UNDERLYING_ISSUER_CONFIG_TEMPLATE = """\
[account]
number = "{account_number}"
holder_name = "Test Holder"
opening_date = 2020-01-01
status = "BeneficialOwner"

[fx]
max_carry_forward_days = 4

[os]
schedule_os_indian_adr_routing = "underlying-issuer"
"""

SYNTHETIC_CONFIG = CONFIG_TEMPLATE.format(account_number="U1234567")

AS_STATEMENT_SECTION = """\
Statement,Header,Field Name,Field Value,,,,,,,,,,,,,
Statement,Data,BrokerName,Interactive Brokers LLC,,,,,,,,,,,,,
Statement,Data,Title,Activity Statement,,,,,,,,,,,,,
Statement,Data,Period,"April 1, 2025 - March 31, 2026",,,,,,,,,,,,,
"""
AS_ACCOUNT_INFORMATION = """\
Account Information,Header,Field Name,Field Value,,,,,,,,,,,,,
Account Information,Data,Name,Test Holder,,,,,,,,,,,,,
Account Information,Data,Account,{account},,,,,,,,,,,,,
Account Information,Data,Account Type,Individual,,,,,,,,,,,,,
Account Information,Data,Base Currency,USD,,,,,,,,,,,,,
"""
AS_HEADER = AS_STATEMENT_SECTION + AS_ACCOUNT_INFORMATION
# The long-term round trip's statement starts on January 1, 2023, so it holds
# the 2023-04-01 open that the FIFO match of the FY 2025-26 close needs.
LTCG_AS_HEADER = AS_HEADER.replace(
    '"April 1, 2025 - March 31, 2026"', '"January 1, 2023 - March 31, 2026"'
)

# One round-trip closure within FY 2025-26. XQBA open 2025-06-15 @ 150 x 10
# (basis 1500); close 2025-12-10 @ 180 x 10 (basis -1500, realized PnL +300).
FII_HEADER = (
    "Financial Instrument Information,Header,Asset Category,Symbol,"
    "Description,Conid,Security ID,ISIN,Listing Exch,Multiplier,Type,"
    "Code,,,,,"
)
FII_DATA_XQBA = (
    "Financial Instrument Information,Data,Stocks,XQBA,SAMPLE ISSUER BA INC,"
    "11111,US99995C1027,US99995C1027,NASDAQ,1,COMMON,,,,,,"
)
INCOME_TRADES_HEADER = (
    "Trades,Header,DataDiscriminator,Asset Category,Currency,Symbol,"
    "Date/Time,Quantity,T. Price,C. Price,Proceeds,Comm/Fee,Basis,"
    "Realized P/L,MTM P/L,Code,"
)
TRADE_OPEN_XQBA = (
    'Trades,Data,Order,Stocks,USD,XQBA,"2025-06-15, 10:00:00",10,150,150,-1500,0,1500,0,0,O,'
)
TRADE_CLOSE_XQBA = (
    'Trades,Data,Order,Stocks,USD,XQBA,"2025-12-10, 10:00:00",-10,180,180,1800,0,-1500,300,0,C,'
)
OP_HEADER = (
    "Open Positions,Header,DataDiscriminator,Asset Category,Currency,"
    "Symbol,Quantity,Mult,Cost Price,Cost Basis,Close Price,Value,"
    "Unrealized P/L,Code,,,"
)

# XQAQ's round trips, one per term: its Financial Instrument Information row,
# then its opening and closing trades. Short-term: opened 2025-06-15 @ 150 x 10
# (basis 1500); closed 2025-12-10 @ 180 x 10 (basis -1500, realized PnL +300).
FII_DATA_XQAQ = (
    "Financial Instrument Information,Data,Stocks,XQAQ,SAMPLE ISSUER AQ INC,"
    "999921,US99992A1097,US99992A1097,NASDAQ,1,COMMON,,,,,,"
)
XQAQ_STCG_ROWS = (
    FII_DATA_XQAQ,
    'Trades,Data,Order,Stocks,USD,XQAQ,"2025-06-15, 10:00:00",10,150,150,-1500,0,1500,0,0,O,',
    'Trades,Data,Order,Stocks,USD,XQAQ,"2025-12-10, 10:00:00",-10,180,180,1800,0,-1500,300,0,C,',
)
# Long-term: opened 2023-04-01; closed 2025-12-10, more than 24 months later
# and after 2024-07-23, @ 220 x 10 (basis -1500, realized PnL +700).
XQAQ_LTCG_ROWS = (
    FII_DATA_XQAQ,
    'Trades,Data,Order,Stocks,USD,XQAQ,"2023-04-01, 10:00:00",10,150,150,-1500,0,1500,0,0,O,',
    'Trades,Data,Order,Stocks,USD,XQAQ,"2025-12-10, 10:00:00",-10,220,220,2200,0,-1500,700,0,C,',
)

# XQBA pays a dividend on 2025-09-12 with US withholding.
DIVIDENDS_HEADER = "Dividends,Header,Currency,Date,Description,Amount,,,,,,,,,,,"
DIVIDEND_XQBA = (
    "Dividends,Data,USD,12-09-2025,"
    "XQBA(US99995C1027) Cash Dividend USD 0.24 per Share (Ordinary Dividend),"
    "2.40,,,,,,,,,,,"
)
WHT_HEADER = "Withholding Tax,Header,Currency,Date,Description,Amount,Code,,,,,,,,,,"
WHT_XQBA = (
    "Withholding Tax,Data,USD,12-09-2025,"
    "XQBA(US99995C1027) Cash Dividend USD 0.24 per Share - US Tax,"
    "-0.36,,,,,,,,,,,"
)

# A GB (CBDT code "44") second security with its own dividend and
# withholding, independent of XQBA's values.
FII_DATA_XQCR = (
    "Financial Instrument Information,Data,Stocks,XQCR,SAMPLE ISSUER CR PLC,"
    "12345,GB99970J1065,GB99970J1065,LSE,1,COMMON,,,,,,"
)
DIVIDEND_XQCR = (
    "Dividends,Data,USD,12-09-2025,"
    "XQCR(GB99970J1065) Cash Dividend USD 0.50 per Share (Ordinary Dividend),"
    "5.00,,,,,,,,,,,"
)
WHT_XQCR = (
    "Withholding Tax,Data,USD,12-09-2025,"
    "XQCR(GB99970J1065) Cash Dividend USD 0.50 per Share - GB Tax,"
    "-0.75,,,,,,,,,,,"
)
XQCR_ROWS = (FII_DATA_XQCR, DIVIDEND_XQCR, WHT_XQCR)

# A US-prefix ADR of an Indian issuer, mapped in INDIAN_ADR_MAPPING, with its
# own dividend and US withholding (#225).
FII_DATA_ADR = (
    "Financial Instrument Information,Data,Stocks,INFY,INFOSYS LTD ADR,"
    "12346,US4567881085,US4567881085,NYSE,1,COMMON,,,,,,"
)
DIVIDEND_ADR = (
    "Dividends,Data,USD,12-09-2025,"
    "INFY(US4567881085) Cash Dividend USD 0.50 per Share (Ordinary Dividend),"
    "5.00,,,,,,,,,,,"
)
WHT_ADR = (
    "Withholding Tax,Data,USD,12-09-2025,"
    "INFY(US4567881085) Cash Dividend USD 0.50 per Share - US Tax,"
    "-0.75,,,,,,,,,,,"
)
ADR_ROWS = (FII_DATA_ADR, DIVIDEND_ADR, WHT_ADR)


def write_config(
    tmp_path: Path, *, account_number: str = "U1234567", template: str = CONFIG_TEMPLATE
) -> Path:
    """Write ``template`` for ``account_number`` as ``tmp_path / "user.toml"``."""
    path = tmp_path / "user.toml"
    path.write_text(template.format(account_number=account_number), encoding="utf-8")
    return path


def statement_body(
    *,
    include_dividends: bool = True,
    include_wht: bool = True,
    second: tuple[str, str, str] | None = None,
) -> str:
    """The statement body: a ``{header}`` slot, XQBA's round trip, then its
    dividend and withholding sections when included.

    ``second`` adds another security's Financial Instrument Information,
    dividend and withholding rows (:data:`XQCR_ROWS` or :data:`ADR_ROWS`)
    after XQBA's.
    """
    lines = ["{header}", FII_HEADER, FII_DATA_XQBA]
    if second:
        lines.append(second[0])
    lines.extend([INCOME_TRADES_HEADER, TRADE_OPEN_XQBA, TRADE_CLOSE_XQBA, OP_HEADER])
    if include_dividends:
        lines.extend([DIVIDENDS_HEADER, DIVIDEND_XQBA])
        if second:
            lines.append(second[1])
    if include_wht:
        lines.extend([WHT_HEADER, WHT_XQBA])
        if second:
            lines.append(second[2])
    lines.append("")
    return "\n".join(lines)


def round_trip_body(rows: tuple[str, str, str], *, open_positions: bool = True) -> str:
    """A statement body: a ``{header}`` slot, then the round trip ``rows``
    (:data:`XQAQ_STCG_ROWS` or :data:`XQAQ_LTCG_ROWS`), then an empty Open
    Positions section unless ``open_positions`` is false."""
    lines = ["{header}", FII_HEADER, rows[0], INCOME_TRADES_HEADER, rows[1], rows[2]]
    if open_positions:
        lines.append(OP_HEADER)
    lines.append("")
    return "\n".join(lines)


def write_statement(
    tmp_path: Path,
    *,
    include_dividends: bool = True,
    include_wht: bool = True,
    second: tuple[str, str, str] | None = None,
    account: str = "U1234567",
    header: str = AS_HEADER,
    body: str | None = None,
    name: str = "activity-statement.csv",
) -> Path:
    """Write the statement for ``account`` under ``header``, with IBKR's
    totals, as ``tmp_path / name``.

    ``body`` replaces :func:`statement_body`'s output, and with it the
    include options and ``second``.
    """
    if body is None:
        body = statement_body(
            include_dividends=include_dividends, include_wht=include_wht, second=second
        )
    path = tmp_path / name
    path.write_text(
        with_totals(body.format(header=header.format(account=account))), encoding="utf-8"
    )
    return path


def read_csv_rows(path: Path) -> list[list[str]]:
    """The rows of the CSV file at ``path``, without a leading BOM."""
    with path.open(encoding="utf-8-sig", newline="") as fh:
        return list(csv.reader(fh))
