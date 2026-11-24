"""The StockPlan FA pipeline tests' marked inputs, held-lot cases and offline
runners, which the ``stockplan-all`` tests share.

- :data:`MISMATCH`: the refusal text when the cumulative vests don't reconcile
  to the Holdings Report's Number of Shares and Acquisition Value.
- The #1176 marked inputs: :data:`SP_TITLE`, :data:`SP_ORDER`,
  :data:`SP_EMPLOYEE` and :data:`SP_PLAN` (the title, order and Holdings
  identity markers), :data:`SP_FUTURE_VEST` (a vest the day after the
  Releases Report's run date), :data:`SP_MARKERS` (the markers no refusal may
  echo), :data:`FUTURE_VEST_REFUSAL` (that vest's refusal, with a ``{row}``
  field), :data:`SP_MARKED_VESTS` (the vest table with the first vest's order
  marked) and :func:`setup_marked_exports` (writes the exports and config with
  the markers in the titles and Holdings identity).
- The #1165 market-session case: :data:`_DEC30` (the marked-close date,
  2025-12-30), :data:`STOCKPLAN_DEC31` (2025-12-31), :data:`_ISIN_LINE`
  (the config's own ISIN line, reused by the ticker override),
  :data:`MARKED_CONFIG` (the config with the description marker as the
  entity name and a ticker override that fetches under the symbol marker),
  :data:`MARKERS_IN_INPUT` (that run's input markers),
  :func:`marked_dec30_fetcher` (returns a fetcher of the stub series with a
  marked Dec-30 close and its Dec-31 bar kept or dropped),
  :func:`prior_session_only_fetcher` (that series without the Dec-31 bar;
  it asserts that the fetch symbol is the symbol marker) and
  :data:`PRIOR_SESSION_ONLY_PIPELINE` (``run_stockplan_fa`` with that
  fetcher at the fetch clock).
- The #1314 exact-cost case: :data:`LONG_COST_VESTS` (each example's vest
  date, Price and Net Share Proceeds), :data:`INITIAL_VALUE` and
  :data:`AUDIT_COST` (two Table A3 column names), :func:`setup_one_vest`
  (writes the exports and config with one vest and its exact Holdings
  totals) and :func:`a3_row` (reads the one Table A3 row of an output).
- :func:`offline_pipeline`: seeds an input folder's default price cache with
  the stub series and returns ``run_stockplan_fa`` at that fetch time.
- The #1290 left-the-account case: :data:`VEST_A` (the vest that left) and
  :data:`VEST_B` (a later vest of the same size), :data:`LEFT_VESTS`
  (the two-vest table), :data:`LEFT_HOLDINGS` (the Holdings totals) and
  :data:`LEFT_MARKERS` (the input markers used by the refusal tests).
"""

from __future__ import annotations

import csv
import datetime as _dt
import functools
from dataclasses import replace
from decimal import Context, Decimal, localcontext
from pathlib import Path
from typing import Any

import ibkr_fa.pipeline_stockplan_fa as fa_mod
from ibkr_fa.pipeline_stockplan_fa import run_stockplan_fa
from ibkr_fa.types import Ticker
from tests._dec31_close import MARKER_DESCRIPTION, MARKER_PRICE, MARKER_SOURCE_DATE, MARKER_SYMBOL
from tests._stockplan_fa_exports import STOCKPLAN_FA_CONFIG as CONFIG
from tests._stockplan_fa_exports import STOCKPLAN_PRICE_NOW as _PRICE_NOW
from tests._stockplan_fa_exports import STOCKPLAN_VESTS as _VESTS
from tests._stockplan_fa_exports import setup_stockplan_exports as _setup
from tests._stockplan_fa_exports import stub_fetcher
from tests._stockplan_fa_exports import write_stockplan_holdings as _write_holdings

MISMATCH = (
    "cumulative vests do not reconcile to the Holdings Report's Number of Shares and "
    "Acquisition Value; a vest lot may be missing, or there are unaccounted sales/transfers "
    "— refusing to file an unverified Schedule FA holding"
)


# Synthetic markers of the #1176 inputs: titles, order, Holdings identity, the
# future vest's figure and date. None may reach a refusal.
SP_TITLE = "Synthetic Title SP01"
SP_ORDER = "SYNTH-ORDER-SP01"
SP_EMPLOYEE = "SYNTH-EMPLOYEE-SP01"
SP_PLAN = "Synthetic Plan SP01"
# A vest the day after the Releases Report's 22-Jun-2026 run date.
SP_FUTURE_VEST = ("2026-06-23", SP_ORDER, 137.2468, 2.0, 1.0)
SP_MARKERS = (
    SP_TITLE,
    SP_ORDER,
    SP_EMPLOYEE,
    SP_PLAN,
    "137.2468",
    "2026-06-23",
    "23-Jun-2026",
    "2026-06-22",
    "22-Jun-2026",
)
FUTURE_VEST_REFUSAL = (
    "ParseError in section 'Releases Report' at row {row}, column 'Vest Date': "
    "completed vest date must not be after this sheet's report run date"
)
SP_MARKED_VESTS = [(_VESTS[0][0], SP_ORDER, *_VESTS[0][2:]), *_VESTS[1:]]


def setup_marked_exports(
    tmp_path: Path,
    *,
    vests: list[tuple[str, str, float, float, float]] = SP_MARKED_VESTS,
    sales: list[list[object]] | None = None,
    **holdings: Any,
) -> tuple[Path, Path, Path]:
    """By default, exports satisfying #1144 and #1290 (both reports run on 22-Jun-2026, the
    Holdings Report's Selected Date), with ``vests``, ``sales`` and the #1176
    markers in the titles and Holdings identity."""
    input_dir, cfg, out = _setup(tmp_path, vests=vests, sales=sales, releases_prefix=SP_TITLE)
    _write_holdings(
        input_dir / "Vested Share Holdings.xlsx",
        title_prefix=SP_TITLE,
        employee=SP_EMPLOYEE,
        plan=SP_PLAN,
        **holdings,
    )
    return input_dir, cfg, out


_DEC30 = _dt.date(2025, 12, 30)
STOCKPLAN_DEC31 = _dt.date(2025, 12, 31)
# The issuer must stay GOOG, the only verified StockPlan issuer, so the symbol
# marker is the Yahoo symbol GOOG is fetched under; the entity name carries the
# description marker. The override reuses CONFIG's own isin line.
_ISIN_LINE = next(line for line in CONFIG.splitlines() if line.startswith("isin = "))
MARKED_CONFIG = CONFIG.replace("Alphabet Inc.", MARKER_DESCRIPTION) + (
    f'\n[[prices.ticker_overrides]]\nibkr_ticker = "GOOG"\n{_ISIN_LINE}\n'
    f'yahoo_symbol = "{MARKER_SYMBOL}"\n'
)
# The run's input markers: the Dec-30 bar's date and close, and MARKED_CONFIG's.
MARKERS_IN_INPUT = (MARKER_SYMBOL, MARKER_DESCRIPTION, MARKER_PRICE, MARKER_SOURCE_DATE)


def marked_dec30_fetcher(*, dec31_bar: bool):
    """The stub series with its 2025-12-30 close set to ``MARKER_PRICE``, below
    every other close, and its 2025-12-31 bar kept or dropped."""

    def fetcher(ticker: Ticker, *, start, end, now, fetch_symbol=None):
        series = stub_fetcher(ticker, start=start, end=end, now=now)
        closes = {**series.closes, _DEC30: Decimal(MARKER_PRICE)}
        highs = dict(series.highs)
        if not dec31_bar:
            del closes[STOCKPLAN_DEC31], highs[STOCKPLAN_DEC31]
        return replace(series, closes=closes, highs=highs)

    return fetcher


def prior_session_only_fetcher(ticker: Ticker, *, start, end, now, fetch_symbol=None):
    # MARKED_CONFIG's ticker override sends the symbol marker to the price source.
    assert fetch_symbol == MARKER_SYMBOL
    return marked_dec30_fetcher(dec31_bar=False)(ticker, start=start, end=end, now=now)


# Runs the real pipeline over a series with no Dec-31 bar, for the CLI tests.
PRIOR_SESSION_ONLY_PIPELINE = functools.partial(
    run_stockplan_fa, price_fetcher=prior_session_only_fetcher, price_now=_PRICE_NOW
)


# (vest date, Price, Net Share Proceeds) of one vest. Main multiplied the two
# at 28 digits, then converted that cost exactly.
LONG_COST_VESTS = {
    # The cost is 246913587.81285878300803673938; at the TTBR of 87.1 that is
    # 21506173498.499999999999999999998, so ₹21,506,173,498. Main's 28-digit
    # cost 246913587.8128587830080367394 filed ₹21,506,173,499.
    "a": ("2025-08-14", "123456793.90642939150401836969", "2"),
    # (a)'s price cut to 28 digits: its cost fits, and it files ₹21,506,173,499.
    "a_control": ("2025-08-14", "123456793.9064293915040183697", "2"),
    # x 87.1 is 99999999999999.500000000000000362, past the CBDT bound once
    # rounded. Main's 28-digit cost gave 99999999999999.4999999999999812.
    "b": ("2025-08-14", "574052812.85878013777267508611", "2000"),
    # x 85.55 (the TTBR of 2025-07-15) is 99999999999999.4999999999999888860,
    # within the bound. Main's gave 99999999999999.50000000000002995 and refused.
    "c": ("2025-07-15", "584453535.94388953828170660426", "2000"),
}
INITIAL_VALUE = "Initial value of the investment"
AUDIT_COST = "_audit: Cost basis (USD)"


def setup_one_vest(
    tmp_path: Path, vest_date: str, price: str, shares: str
) -> tuple[Path, Path, Path]:
    """:func:`_setup` with one vest of ``shares`` at ``price``; the Holdings
    totals are text cells of its shares and exact cost."""
    input_dir, cfg, out = _setup(tmp_path, vests=[(vest_date, "V1", price, shares, shares)])
    with localcontext(Context(prec=100)):
        cost = str(Decimal(price) * Decimal(shares))
    _write_holdings(input_dir / "Vested Share Holdings.xlsx", shares=shares, acq=cost)
    return input_dir, cfg, out


def a3_row(out: Path) -> dict[str, str]:
    """The one Table A3 row in ``out``."""
    with out.open(encoding="utf-8-sig") as handle:
        [row] = csv.DictReader(handle)
    return row


def offline_pipeline(input_dir: Path) -> functools.partial[Any]:
    """Seed ``input_dir``'s default price cache with :func:`stub_fetcher`'s
    series, and return ``run_stockplan_fa`` at that fetch time, so a
    ``--no-fetch-prices`` run reaches the year-end close."""
    now = _dt.datetime(2026, 7, 15, tzinfo=_dt.UTC)
    fa_mod.load_price_series(
        {Ticker("GOOG"): (_dt.date(2025, 1, 1), _dt.date(2025, 12, 31))},
        cache_dir=input_dir / ".ibkr-fa-price-cache",
        fetcher=stub_fetcher,
        now=now,
    )
    return functools.partial(run_stockplan_fa, price_now=now)


# #1290: vest A left the account in December; vest B, after Dec-31 and before
# the Selected Date, has the same shares and value, so A alone matched the
# Holdings totals and was filed as held. No report shows the transfer.
VEST_A = ("2025-07-15", SP_ORDER, 100.0, 1.0, 1.0)
VEST_B = ("2026-01-14", "SYNTH-ORDER-SP02", 100.0, 1.0, 1.0)
LEFT_VESTS = [VEST_A, VEST_B]
LEFT_HOLDINGS: dict[str, Any] = {"shares": Decimal(1), "acq": Decimal(100)}
LEFT_MARKERS = (*SP_MARKERS[:4], VEST_B[1], VEST_A[0], VEST_B[0])
