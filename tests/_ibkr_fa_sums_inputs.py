"""The IBKR Schedule FA statement builder, config, ``schedule-fa`` runner and
CSV reader that several test modules share.

- ``IBKR_FA_SUMS_CONFIG``: a ``user.toml`` text with an IBKR custodian, so
  ``schedule-fa`` builds Table A2.
- ``XQBB_DIVIDEND``: the description of an XQBB cash dividend; every statement
  holds XQBB over Dec 31, so its dividends land in its Table A3 row.
- ``IncomeRow``: a dividend or interest row's date, description and amount.
- ``ibkr_fa_sums_statement``: a full-CY 2025 Activity Statement that holds
  XQBB, with a cash NAV, the given dividend and interest rows and their exact
  printed totals, and any extra instrument and trade rows.
- ``write_fa_sums_inputs``: writes a statement and the config to
  ``statement.csv`` and ``user.toml`` in a folder and returns both paths.
- ``run_schedule_fa_cy2025``: runs ``schedule-fa`` for calendar year 2025
  without fetching prices through ``ibkr_fa.cli.main`` and returns its exit
  code.
- ``read_csv_dict_rows``: reads a CSV output's rows as dicts keyed by its
  header.
- ``A3_GROSS_COLUMN``, ``A2_NATURE_COLUMN`` and ``A2_AMOUNT_COLUMN``: the
  Table A3 gross amount and the Table A2 nature and amount column names.
"""

from __future__ import annotations

import csv
from collections.abc import Sequence
from datetime import date
from decimal import Context, Decimal, localcontext
from pathlib import Path

from ibkr_fa.cli import main
from tests._ibkr_user_toml import CUSTODIAN_TOML
from tests._trades_totals import with_trades_totals

_HEAD = """\
Statement,Header,Field Name,Field Value
Statement,Data,BrokerName,Interactive Brokers LLC
Statement,Data,Title,Activity Statement
Statement,Data,Period,"January 1, 2025 - December 31, 2025"
Account Information,Header,Field Name,Field Value
Account Information,Data,Name,Sample Holder
Account Information,Data,Account,U12345678
Account Information,Data,Account Type,Individual
Account Information,Data,Customer Type,Individual
Account Information,Data,Account Capabilities,Cash
Account Information,Data,Base Currency,USD
Financial Instrument Information,Header,Asset Category,Symbol,Description,Conid,Security ID,Listing Exch,Multiplier,Type,Code
Financial Instrument Information,Data,Stocks,XQBB,SAMPLE ISSUER BB INC,11111,US99995B1044,NASDAQ,1,COMMON,
"""  # noqa: E501
_TRADES_HEADER = (
    "Trades,Header,DataDiscriminator,Asset Category,Currency,Symbol,Date/Time,Quantity,"
    "T. Price,C. Price,Proceeds,Comm/Fee,Basis,Realized P/L,MTM P/L,Code\n"
)
# XQBB is held over Dec 31, so its dividends land in its Table A3 row.
_XQBB_BUY = (
    'Trades,Data,Order,Stocks,USD,XQBB,"2025-01-15, 10:30:00",10,85.20,95.40,-852.00,0,'
    "852.00,0,102.00,O\n"
)
_TAIL = """\
Open Positions,Header,DataDiscriminator,Asset Category,Currency,Symbol,Quantity,Mult,Cost Price,Cost Basis,Close Price,Value,Unrealized P/L,Code
Open Positions,Data,Summary,Stocks,USD,XQBB,10,1,85.20,852.00,95.40,954.00,102.00,
Net Asset Value,Header,Asset Class,Prior Total,Current Long,Current Short,Current Total,Change
Net Asset Value,Data,Cash,0,100.00,0,100.00,100.00
Net Asset Value,Data,Total,0,100.00,0,100.00,100.00
Change in NAV,Header,Field Name,Field Value
"""  # noqa: E501
_ENDING_VALUE = "Change in NAV,Data,Ending Value,100.00\n"

# Table A2 is built only for a configured custodian.
IBKR_FA_SUMS_CONFIG = (
    """\
[account]
number = "U12345678"
holder_name = "Sample Holder"
opening_date = 2020-01-01
status = "BeneficialOwner"
"""
    + CUSTODIAN_TOML
)

XQBB_DIVIDEND = "XQBB(US99995B1044) Cash Dividend USD 0.25 per Share (Ordinary Dividend)"

type IncomeRow = tuple[date, str, str]


def _total(rows: Sequence[IncomeRow]) -> Decimal:
    """The exact sum of ``rows``' amounts."""
    with localcontext(Context(prec=1200)):
        return sum((Decimal(amount) for _, _, amount in rows), Decimal(0))


def _income_block(section: str, rows: Sequence[IncomeRow]) -> str:
    """A USD ``section`` block of ``rows`` and its printed ``Total``, summed exactly."""
    if not rows:
        return ""
    return "".join(
        (
            f"{section},Header,Currency,Date,Description,Amount\n",
            *(
                f"{section},Data,USD,{day.isoformat()},{text},{amount}\n"
                for day, text, amount in rows
            ),
            f"{section},Data,Total,,,{_total(rows)}\n",
        )
    )


def _change_in_nav_line(section: str, rows: Sequence[IncomeRow]) -> str:
    """The ``Change in NAV`` line IBKR prints for ``section``: its rows' total,
    which ``schedule-fa`` checks the section against (#1324)."""
    return f"Change in NAV,Data,{section},{_total(rows)}\n" if rows else ""


def ibkr_fa_sums_statement(
    *,
    dividends: Sequence[IncomeRow] = (),
    interest: Sequence[IncomeRow] = (),
    instruments: str = "",
    trades: str = "",
) -> str:
    """A full-CY 2025 Activity Statement holding XQBB, with a cash NAV."""
    return with_trades_totals(
        "".join(
            (
                _HEAD,
                instruments,
                _TRADES_HEADER,
                _XQBB_BUY,
                trades,
                _income_block("Dividends", dividends),
                _income_block("Interest", interest),
                _TAIL,
                _change_in_nav_line("Dividends", dividends),
                _change_in_nav_line("Interest", interest),
                _ENDING_VALUE,
            )
        )
    )


def write_fa_sums_inputs(tmp_path: Path, statement: str) -> tuple[Path, Path]:
    statement_path = tmp_path / "statement.csv"
    statement_path.write_text(statement, encoding="utf-8")
    config = tmp_path / "user.toml"
    config.write_text(IBKR_FA_SUMS_CONFIG, encoding="utf-8")
    return statement_path, config


def run_schedule_fa_cy2025(statement: Path, config: Path, out: Path, *args: str) -> int:
    return main(
        [
            "schedule-fa",
            "--input",
            str(statement),
            "--config",
            str(config),
            "--output",
            str(out),
            "--calendar-year",
            "2025",
            "--no-fetch-prices",
            *args,
        ]
    )


def read_csv_dict_rows(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


A3_GROSS_COLUMN = "Total gross amount paid/credited with respect to the holding during the period"
A2_NATURE_COLUMN = "Gross amount paid/credited to the account during the period - Nature of amount"
A2_AMOUNT_COLUMN = "Gross amount paid/credited to the account during the period - Amount"
