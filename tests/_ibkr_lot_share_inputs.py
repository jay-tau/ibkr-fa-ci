"""The IBKR lot-share Activity Statement builder and STCG recorder that the
exact lot shares and exact Trades test modules share.

- ``FY_2025_26_PERIOD`` and ``CY_2025_PERIOD``: the statement period texts for
  FY 2025-26 and calendar year 2025.
- ``NAV_SECTIONS``: the Net Asset Value and Change in NAV sections of a
  $100.00 cash account, to follow the Open Positions rows.
- ``xqbb_trade``: one XQBB ``Trades`` row at 10:00 on a day, with no fee or
  MTM P/L.
- ``write_lot_share_inputs``: writes an XQBB Activity Statement for a period,
  holding the given Trades rows, the Open Positions header and any tail, with
  its Trades totals, and the IBKR config, to ``statement.csv`` and
  ``user.toml`` in a folder and returns both paths.
- ``record_stcg_compute``: patches ``ibkr_fa.pipeline_cg`` to record each
  result of the real STCG compute the pipeline calls, and returns the list.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Final

import pytest

from ibkr_fa import pipeline_cg
from ibkr_fa.compute.schedule_cg import ScheduleCgStcgComputeResult
from tests._ibkr_fa_sums_inputs import IBKR_FA_SUMS_CONFIG as _CONFIG
from tests._trades_totals import with_trades_totals

_HEAD = """\
Statement,Header,Field Name,Field Value
Statement,Data,BrokerName,Interactive Brokers LLC
Statement,Data,Title,Activity Statement
Statement,Data,Period,"{period}"
Account Information,Header,Field Name,Field Value
Account Information,Data,Name,Sample Holder
Account Information,Data,Account,U12345678
Account Information,Data,Account Type,Individual
Account Information,Data,Base Currency,USD
Financial Instrument Information,Header,Asset Category,Symbol,Description,Conid,Security ID,Listing Exch,Multiplier,Type,Code
Financial Instrument Information,Data,Stocks,XQBB,SAMPLE ISSUER BB INC,11111,US99995B1044,NASDAQ,1,COMMON,
Trades,Header,DataDiscriminator,Asset Category,Currency,Symbol,Date/Time,Quantity,T. Price,C. Price,Proceeds,Comm/Fee,Basis,Realized P/L,MTM P/L,Code
"""  # noqa: E501
FY_2025_26_PERIOD: Final = "April 1, 2025 - March 31, 2026"
CY_2025_PERIOD: Final = "January 1, 2025 - December 31, 2025"
_OPEN_POSITIONS_HEADER: Final = (
    "Open Positions,Header,DataDiscriminator,Asset Category,Currency,Symbol,Quantity,Mult,"
    "Cost Price,Cost Basis,Close Price,Value,Unrealized P/L,Code\n"
)
NAV_SECTIONS: Final = """\
Net Asset Value,Header,Asset Class,Prior Total,Current Long,Current Short,Current Total,Change
Net Asset Value,Data,Cash,0,100.00,0,100.00,100.00
Net Asset Value,Data,Total,0,100.00,0,100.00,100.00
Change in NAV,Header,Field Name,Field Value
Change in NAV,Data,Ending Value,100.00
"""


def xqbb_trade(
    day: str, quantity: str, price: str, proceeds: str, basis: str, pnl: str, code: str
) -> str:
    """One XQBB ``Trades`` row at 10:00 on ``day``, with no fee or MTM P/L."""
    return (
        f'Trades,Data,Order,Stocks,USD,XQBB,"{day}, 10:00:00",{quantity},{price},{price},'
        f"{proceeds},0,{basis},{pnl},0,{code}\n"
    )


def write_lot_share_inputs(
    tmp_path: Path, period: str, trades: str, tail: str = ""
) -> tuple[Path, Path]:
    """Write an Activity Statement for ``period`` holding ``trades``, and the config."""
    statement = tmp_path / "statement.csv"
    statement.write_text(
        with_trades_totals(_HEAD.format(period=period) + trades + _OPEN_POSITIONS_HEADER + tail),
        encoding="utf-8",
    )
    config = tmp_path / "user.toml"
    config.write_text(_CONFIG, encoding="utf-8")
    return statement, config


def record_stcg_compute(monkeypatch: pytest.MonkeyPatch) -> list[ScheduleCgStcgComputeResult]:
    """Record each result of the real STCG compute the pipeline calls."""
    results: list[ScheduleCgStcgComputeResult] = []
    real = pipeline_cg.compute_schedule_cg_stcg_lines

    def compute(*args: Any, **kwargs: Any) -> ScheduleCgStcgComputeResult:
        result = real(*args, **kwargs)
        results.append(result)
        return result

    monkeypatch.setattr(pipeline_cg, "compute_schedule_cg_stcg_lines", compute)
    return results
