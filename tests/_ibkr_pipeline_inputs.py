"""The synthetic IBKR Flex Query and Activity Statement inputs and their
writers that the pipeline tests and the IBKR number limits tests share. The CLI
tests and the IBKR parser refusal chain tests also read the Flex ``Account
Information`` section and the Flex and ``user.toml`` writers. The Flex period
tests read that section and the ``user.toml`` writer.

- ``PIPELINE_ACCOUNT_INFO_SECTION``: a Flex ``Account Information`` section, a
  header and one USD account row.
- ``PIPELINE_OPEN_POSITIONS_SECTION``: a Flex ``Open Positions`` section, a
  header and one USD stock lot.
- ``PIPELINE_TRADES_WITH_CLOSED_LOT``: a Flex ``Trades`` section, a header, one
  closing execution and its ``CLOSED_LOT`` row.
- ``write_pipeline_flex_csv``: writes the given sections, joined with a newline
  and wrapped in Flex header and trailer records for a calendar year, to
  ``flex.csv`` in a folder and returns its path.
- ``write_pipeline_user_toml``: writes a ``user.toml`` with an ``[account]``
  table for an account number and an ``[fx]`` table to a folder and returns its
  path.
- ``PIPELINE_AS_STATEMENT_SECTION``, ``PIPELINE_AS_ACCOUNT_INFORMATION_SECTION``,
  ``PIPELINE_AS_FINANCIAL_INSTRUMENT_INFO_SECTION``,
  ``PIPELINE_AS_TRADES_SECTION`` and ``PIPELINE_AS_OPEN_POSITIONS_SECTION``: an
  Activity Statement's ``Statement`` section for calendar year 2025, its
  ``Account Information`` section (a template with an ``{account}`` field), and
  its ``Financial Instrument Information``, ``Trades`` and ``Open Positions``
  sections for one USD stock bought in December and held at year end.
- ``write_activity_statement_csv``: writes an Activity Statement built from
  those sections, with a configurable account number, three optional section
  overrides and optional section omissions, through ``with_trades_totals``, to
  ``activity-statement.csv`` in a folder and returns its path.
"""

from __future__ import annotations

from pathlib import Path

from tests._flex_records import with_flex_records
from tests._flex_section_rows import OPEN_POSITIONS_HEADER, TRADES_LOT_DETAIL_HEADER
from tests._trades_totals import with_trades_totals

# Synthetic Flex Query sections: Account Information, one open lot, and a
# Trades section with one closing execution and its CLOSED_LOT row, with
# made-up securities and values.

PIPELINE_ACCOUNT_INFO_SECTION = """\
"ClientAccountID","CurrencyPrimary","Name","AccountType","IBEntity"
"U12345678","USD","Test Holder","Individual","IBLLC-US"
"""

PIPELINE_OPEN_POSITIONS_SECTION = (
    OPEN_POSITIONS_HEADER
    + """
"USD","1","STK","COMMON","XQBB","SAMPLE ISSUER BB INC","99995B104","US99995B1044","NASDAQ","US","20251231","10","95.4","954","85.2","852","Long","LOT","20250429","20250429"
"""  # noqa: E501
)

PIPELINE_TRADES_WITH_CLOSED_LOT = (
    TRADES_LOT_DETAIL_HEADER
    + """
"USD","1","STK","COMMON","XQBD","SAMPLE ISSUER BD INC","99995A106","US99995A1060","NASDAQ","US","20250428","20250428","20250429","ExchTrade","-4","61.15","-244.6","244.6","0","0","USD","244.6","61.15","C","-260","-15.4","SELL","EXECUTION","",""
"USD","1","STK","COMMON","XQBD","SAMPLE ISSUER BD INC","99995A106","US99995A1060","NASDAQ","US","20250428","20250428","","","4","65","","","","","","","","C","260","-15.4","SELL","CLOSED_LOT","20250428;101500","20250428;101500"
"""  # noqa: E501
)

_USER_TOML_TEMPLATE = """\
[account]
number = "{account_number}"
holder_name = "Test Holder"
opening_date = 2020-01-01
status = "BeneficialOwner"

[fx]
max_carry_forward_days = 14
"""


def write_pipeline_flex_csv(
    tmp_path: Path, *sections: str, calendar_year: int = 2025, start: str | None = None
) -> Path:
    """Write a Flex CSV containing the given section blocks.

    Sections are separated by blank lines, matching IBKR's emitter, between
    header and trailer records covering ``calendar_year`` from ``start``
    (#1313).
    """
    p = tmp_path / "flex.csv"
    p.write_text(
        with_flex_records("\n".join(sections), calendar_year=calendar_year, start=start),
        encoding="utf-8",
    )
    return p


def write_pipeline_user_toml(tmp_path: Path, *, account_number: str = "U12345678") -> Path:
    p = tmp_path / "user.toml"
    p.write_text(_USER_TOML_TEMPLATE.format(account_number=account_number), encoding="utf-8")
    return p


PIPELINE_AS_STATEMENT_SECTION = """\
Statement,Header,Field Name,Field Value,,,,,,,,,,,,,
Statement,Data,BrokerName,Interactive Brokers LLC,,,,,,,,,,,,,
Statement,Data,Title,Activity Statement,,,,,,,,,,,,,
Statement,Data,Period,"January 1, 2025 - December 31, 2025",,,,,,,,,,,,,
"""

PIPELINE_AS_ACCOUNT_INFORMATION_SECTION = """\
Account Information,Header,Field Name,Field Value,,,,,,,,,,,,,
Account Information,Data,Name,TAX PAYER,,,,,,,,,,,,,
Account Information,Data,Account,{account},,,,,,,,,,,,,
Account Information,Data,Account Type,Individual,,,,,,,,,,,,,
Account Information,Data,Base Currency,USD,,,,,,,,,,,,,
"""

PIPELINE_AS_FINANCIAL_INSTRUMENT_INFO_SECTION = """\
Financial Instrument Information,Header,Asset Category,Symbol,Description,Conid,Security ID,Listing Exch,Multiplier,Type,Code,,,,,,
Financial Instrument Information,Data,Stocks,XQBK,SAMPLE ISSUER BK INC-CL A,55555,US99995L1026,NASDAQ,1,COMMON,,,,,,,
"""  # noqa: E501

PIPELINE_AS_TRADES_SECTION = """\
Trades,Header,DataDiscriminator,Asset Category,Currency,Symbol,Date/Time,Quantity,T. Price,C. Price,Proceeds,Comm/Fee,Basis,Realized P/L,MTM P/L,Code
Trades,Data,Order,Stocks,USD,XQBK,"2025-12-29, 09:45:00",1,212,212.60,-212,-0.35,212.35,0,0.60,O
"""  # noqa: E501

PIPELINE_AS_OPEN_POSITIONS_SECTION = """\
Open Positions,Header,DataDiscriminator,Asset Category,Currency,Symbol,Quantity,Mult,Cost Price,Cost Basis,Close Price,Value,Unrealized P/L,Code,,,
Open Positions,Data,Summary,Stocks,USD,XQBK,1,1,212.35,212.35,212.60,212.60,0.25,,,,
"""  # noqa: E501


def write_activity_statement_csv(
    tmp_path: Path,
    *,
    account: str = "U12345678",
    statement_section: str | None = None,
    trades_section: str | None = None,
    open_positions_section: str | None = None,
    omit_section: str | tuple[str, ...] | None = None,
) -> Path:
    """Write a minimal but format-valid Activity Statement CSV.

    ``omit_section`` drops one section by name, or several when given a tuple.
    """
    sections: dict[str, str] = {
        "Statement": statement_section or PIPELINE_AS_STATEMENT_SECTION,
        "Account Information": PIPELINE_AS_ACCOUNT_INFORMATION_SECTION.format(account=account),
        "Financial Instrument Information": PIPELINE_AS_FINANCIAL_INSTRUMENT_INFO_SECTION,
        "Trades": trades_section or PIPELINE_AS_TRADES_SECTION,
        "Open Positions": open_positions_section or PIPELINE_AS_OPEN_POSITIONS_SECTION,
    }
    for name in (omit_section,) if isinstance(omit_section, str) else omit_section or ():
        sections.pop(name)
    p = tmp_path / "activity-statement.csv"
    p.write_text(with_trades_totals("".join(sections.values())), encoding="utf-8")
    return p
