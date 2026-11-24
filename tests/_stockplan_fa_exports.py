"""The StockPlan FA pipeline tests' config, vest table, offline price
fetcher, export writers and ``stockplan-fa`` runner that several test modules
share.

- :func:`stockplan_fa_config`: a full StockPlan ``user.toml`` with
  ``[account]``, ``[fx]`` and ``[stockplan.security]`` tables, an optional
  ``[stockplan].opening_date``; the security's ISIN is the one the StockPlan
  issuer gate accepts.
- :data:`STOCKPLAN_FA_CONFIG`: that config with both opening dates set.
- :data:`STOCKPLAN_VESTS`: the vest table (vest date, order, FMV, units
  vested, units deposited), all held on Dec-31.
- :data:`STOCKPLAN_HELD_SHARES` and :data:`STOCKPLAN_HELD_ACQUISITION`: the
  Holdings report's share and acquisition value totals for those vests.
- :func:`stub_fetcher`: an offline price fetcher whose price steps up every
  weekday; each US market session gets a close and a high.
- :func:`write_stockplan_cg_report`: writes a synthetic capital gain/loss
  export .xlsx (a Releases Report and a Capital Gains Report sheet, openpyxl).
- :func:`write_stockplan_holdings`: writes a synthetic Holdings Report .xlsx
  with its ``Quote History`` sheet.
- :func:`setup_stockplan_exports`: writes the three exports (``cg_kwargs``
  go to the capital gain/loss writer; the dividend report is
  ``write_dividend_report``'s, with one Dec-10 dividend and its withholding)
  and a config under one tmp path, and returns the input folder, the config
  and the Schedule FA output path.
- :data:`STOCKPLAN_PRICE_NOW`: the fetch clock for runs that fetch prices.
- :func:`run_stockplan_fa_cli`: runs ``stockplan-fa`` (calendar year 2025, no
  price fetch) through ``ibkr_fa.cli.main`` and returns its exit code.
"""

from __future__ import annotations

import datetime as _dt
from decimal import Decimal
from pathlib import Path

import openpyxl

from ibkr_fa.cli import main
from ibkr_fa.prices.series import PriceSeries
from ibkr_fa.reference.us_market_calendar import is_us_market_session
from ibkr_fa.types import Ticker
from tests._stockplan_holdings import (
    HOLDINGS_FOOTER,
    HOLDINGS_HEADER,
    add_quote_history,
    market_value,
    quote_history_rows,
)
from tests._stockplan_income import write_dividend_report
from tests._stockplan_report_writers import (
    CAPITAL_GAINS_HEADER,
    CAPITAL_GAINS_SECTION_ROW,
    RELEASES_HEADER,
    STOCKPLAN_USD_FOOTER,
)


def stockplan_fa_config(*, account_opening: str, stockplan_opening: str | None) -> str:
    """A full StockPlan config with ``[account].opening_date = account_opening``.

    ``stockplan_opening`` sets ``[stockplan].opening_date``, or omits it when
    None. In a mixed config ``[account]`` is the separate IBKR account. (#600)
    """
    stockplan = (
        "" if stockplan_opening is None else f"[stockplan]\nopening_date = {stockplan_opening}\n\n"
    )
    return f"""\
[account]
number = "MS-1"
holder_name = "Sample Holder"
opening_date = {account_opening}
status = "BeneficialOwner"
us_person = false

[fx]
max_carry_forward_days = 14

{stockplan}[stockplan.security]
symbol = "GOOG"
isin = "US02079K1079"
entity_name = "Alphabet Inc."
entity_address = "1600 Amphitheatre Parkway, Mountain View, CA"
entity_zip = "94043"
listing_exchange = "NASDAQ"
issuer_country_code = "US"
nature_of_entity = "Listed company shares"
"""


STOCKPLAN_FA_CONFIG = stockplan_fa_config(
    account_opening="2025-07-14", stockplan_opening="2025-07-14"
)

# (vest_date, order, fmv, gsus, deposited). All four vests are held on Dec-31.
STOCKPLAN_VESTS = [
    ("2025-08-14", "V1", 196.42, 9.012, 6.214),
    ("2025-09-15", "V2", 203.18, 4.506, 3.107),
    ("2025-10-14", "V3", 211.95, 5.408, 3.729),
    ("2025-11-14", "V4", 224.63, 4.506, 3.108),
]
# Holdings anchor = cumulative of the 4 held vests (no later vests in the fixture).
STOCKPLAN_HELD_SHARES = Decimal("16.158")
STOCKPLAN_HELD_ACQUISITION = sum(
    (Decimal(str(f)) * Decimal(str(d)) for _, _, f, _, d in STOCKPLAN_VESTS), Decimal("0")
)


def stub_fetcher(ticker: Ticker, *, start, end, now, fetch_symbol=None):
    closes: dict[_dt.date, Decimal] = {}
    highs: dict[_dt.date, Decimal] = {}
    d = start
    price = Decimal("200")
    while d <= end:
        if d.weekday() < 5:
            # The price walks every weekday; a listed closure gets no bar.
            if is_us_market_session(d):
                closes[d] = price
                highs[d] = price + Decimal("3")
            price += Decimal("0.25")
        d += _dt.timedelta(days=1)
    return PriceSeries(
        ticker=ticker,
        currency="USD",
        closes=closes,
        highs=highs,
        has_split_in_range=False,
        source="stub",
        fetched_at=now,
    )


def write_stockplan_cg_report(
    path: Path,
    *,
    vests=STOCKPLAN_VESTS,
    sales: list[list[object]] | None = None,
    releases_run_on: str = "22-Jun-2026",
    releases_prefix: str = "Alphabet, Inc.",
    cg_run_on: str = "22-Jun-2026",
    period: str = "02-Jun-2025 to 04-May-2026",
) -> None:
    wb = openpyxl.Workbook()
    rel = wb.active
    rel.title = "Releases Report"
    rel.append([f"{releases_prefix} - Releases Report (report run on {releases_run_on})"])
    rel.append(RELEASES_HEADER)
    for date, order, fmv, gsus, dep in vests:
        rel.append(
            [
                _dt.datetime.fromisoformat(date),
                order,
                "GOOG",
                "Release",
                "Complete",
                fmv,
                gsus,
                0.0,
                dep,
                "Fractional Shares",
            ]
        )
    rel.append([STOCKPLAN_USD_FOOTER])
    cg = wb.create_sheet("Capital Gains Report")
    cg.append(
        [
            f"Alphabet, Inc. - Capital Gains Report (report run on {cg_run_on} "
            f"for reporting period {period})"
        ]
    )
    cg.append(CAPITAL_GAINS_SECTION_ROW)
    cg.append(CAPITAL_GAINS_HEADER)
    for row in sales if sales is not None else [["No records found"]]:
        cg.append(row)
    cg.append([STOCKPLAN_USD_FOOTER])
    wb.save(path)


def write_stockplan_holdings(
    path: Path,
    *,
    shares=STOCKPLAN_HELD_SHARES,
    acq=STOCKPLAN_HELD_ACQUISITION,
    run_on: str = "22-Jun-2026",
    selected_date: str | None = None,
    title_prefix: str = "Alphabet, Inc.",
    employee: str = "7000001",
    plan: str = "Plan",
) -> None:
    """A Holdings workbook run on ``run_on``, the same day as the Releases
    Report by default, as of ``selected_date`` (default: ``run_on``). A string
    ``shares`` or ``acq`` is a text cell, which keeps every digit."""
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Holdings Report"
    ws.append([f"{title_prefix} - Holdings Report (report run on {run_on})"])
    ws.append(HOLDINGS_HEADER)
    ws.append([employee, plan, "Cash - USD", 4.18, 4.18, 4.18])
    totals = (v if isinstance(v, str) else float(v) for v in (shares, acq))
    ws.append([employee, plan, "GOOG - NASDAQ", *totals, market_value(shares)])
    ws.append([HOLDINGS_FOOTER])
    quote_date = _dt.datetime.strptime(selected_date or run_on, "%d-%b-%Y").date()
    add_quote_history(wb, quote_history_rows("GOOG - NASDAQ", quote_date=quote_date))
    wb.save(path)


def setup_stockplan_exports(
    tmp_path: Path, *, config: str = STOCKPLAN_FA_CONFIG, **cg_kwargs
) -> tuple[Path, Path, Path]:
    input_dir = tmp_path / "in"
    input_dir.mkdir()
    write_stockplan_cg_report(input_dir / "IN - Capital Gain_Loss Report.xlsx", **cg_kwargs)
    write_stockplan_holdings(input_dir / "Vested Share Holdings.xlsx")
    write_dividend_report(input_dir, pairs=(("2025-12-10", 2.60, -0.65),))
    cfg = tmp_path / "user.toml"
    cfg.write_text(config)
    return input_dir, cfg, tmp_path / "schedule-fa.csv"


STOCKPLAN_PRICE_NOW = _dt.datetime(2026, 7, 15, tzinfo=_dt.UTC)


def run_stockplan_fa_cli(input_dir: Path, cfg: Path, out: Path) -> int:
    return main(
        [
            "stockplan-fa",
            *("--input", str(input_dir), "--config", str(cfg)),
            *("--output", str(out), "--calendar-year", "2025", "--no-fetch-prices"),
        ]
    )
