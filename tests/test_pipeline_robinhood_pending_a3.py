"""A Dec-31 pending Robinhood trade through robinhood-fa (#1246).

Each run reconciles a synthetic two-account bundle and runs the real option,
price-coverage, source-income, Schedule FA and writer steps. The first account
trades on 2025-12-31 for settlement on 2026-01-02: its CY 2025 statement shows
the trade only as a Pending Trades row, with the position and cash before it,
and its Q1 2026 statement carries the trade's Account Activity row and the
settled position and cash. Only the bundle load, the Schedule FA TTBR table
(80 every day of CY 2025) and the Yahoo closes ($15 every session by default) are stubbed,
and one refusal forges the anchors A3 reads.
"""

from __future__ import annotations

import csv
from dataclasses import replace
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

import ibkr_fa.pipeline_robinhood as pipeline
from ibkr_fa.compute.robinhood_fa import RobinhoodFaBlockerKind
from ibkr_fa.normalize.robinhood.bundle import RobinhoodReconciliationError
from ibkr_fa.parser.robinhood import RobinhoodEvent
from ibkr_fa.pipeline_robinhood import RobinhoodPipelineError, run_robinhood_fa
from tests._crash_reports import assert_prints_no_marker
from tests._no_echo import assert_no_markers
from tests._robinhood_cli import pipeline_run_kwargs_to_out as _run_kwargs
from tests._robinhood_cli import robinhood_main as _main
from tests._robinhood_cli import stale_fa_outputs as _stale_fa_outputs
from tests._robinhood_fa_inputs import fa_buy as _buy
from tests._robinhood_fa_inputs import fa_sale as _sale
from tests._robinhood_pending import CLOSING_BLOCKED as _BLOCKED
from tests._robinhood_pending import DEC31_REPRO as _REPRO
from tests._robinhood_pending import (
    MARKERS,
    SYMBOL,
    dec31_trade,
    forge_settlement_anchors,
)
from tests._robinhood_pending import load_dec31_accounts as _load
from tests._robinhood_refusals import PENDING_CASH_REFUSAL as _UNRECONCILED

# Each case is the trades settled in CY 2025 and the Dec-31 trade, as _REPRO
# is. 3 shares bought for $30 in January, and 1 sold for $15 on Dec 31.
_SALE = (
    (_buy("3"),),
    dec31_trade(replace(_sale(), price_usd=Decimal(15), amount_usd=Decimal(15)), 5),
)


def _a3_rows(tmp_path: Path) -> list[tuple[str, str, str]]:
    """Each A3 row's initial value, peak value and closing balance."""
    with (tmp_path / "out" / "schedule-fa-a3.csv").open(encoding="utf-8", newline="") as handle:
        return [
            (
                row["Initial value of the investment"],
                row["Peak value of investment during the Period"],
                row["Closing balance"],
            )
            for row in csv.DictReader(handle)
        ]


@pytest.mark.parametrize(
    ("trades", "code", "rows"),
    [
        # 2 shares worth $30 scale to $30 * 3/2 = $45 at the trade-date 3
        # shares: the January lot's 2/3 is $30, ₹2,400 at 80, and the Dec-31
        # lot's 1/3 is $15, ₹1,200, its peak too. Nothing is left to fill by
        # hand, so the run exits 0.
        pytest.param(_REPRO, 0, [("1600", "2400", "2400"), ("800", "1200", "1200")], id="purchase"),
        # 3 shares worth $45 scale to $45 * 2/3 = $30 at the trade-date 2
        # shares, ₹2,400; the sold share's fragment closes at 0 as before. Its
        # sale Amount files as A2 and A3 proceeds (#1383), so the run exits 0.
        pytest.param(_SALE, 0, [("1600", "2400", "2400"), ("800", "1200", "0")], id="sale"),
    ],
)
def test_robinhood_fa_values_a_dec31_pending_trade_at_its_trade_date_quantity(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    trades: tuple[tuple[RobinhoodEvent, ...], RobinhoodEvent],
    code: int,
    rows: list[tuple[str, str, str]],
) -> None:
    """#1246: a Dec-31 trade settling in January files A3 at the trade-date
    quantity its Pending Trades row proves, valued at the December statement's
    total scaled to that quantity and split by quantity. Main refused both for
    its A3 closing blocker."""
    _load(monkeypatch, tmp_path, trades)

    assert _main("robinhood-fa", tmp_path, "--no-fetch-prices", "--format", "filing-csv") == code

    assert capsys.readouterr().err == ""
    assert _a3_rows(tmp_path) == rows


# Each case: its trades, its other load changes, the trade-date quantity a
# forged anchor gives A3, the markers in its input, and the refusal.
_REFUSALS = pytest.mark.parametrize(
    ("trades", "changes", "forged", "markers", "error", "text"),
    [
        # The repro without its Pending Trades row: the straddling trade has
        # no pending evidence, and the row's description is not in the input.
        pytest.param(
            _REPRO,
            {"pending": False},
            None,
            (SYMBOL,),
            RobinhoodReconciliationError,
            _UNRECONCILED,
            id="no-pending-row",
        ),
        # Every share is bought on Dec 31: the statement shows none to scale,
        # and the lot's 1 share is 1 off its 0.
        pytest.param(
            ((), _REPRO[1]),
            {},
            None,
            MARKERS,
            RobinhoodPipelineError,
            _BLOCKED.format(lots="0:5"),
            id="all-bought-on-dec31",
        ),
        # The 3 lot shares are 1 off a forged trade-date quantity of 4.
        pytest.param(
            _REPRO,
            {},
            Decimal(4),
            MARKERS,
            RobinhoodPipelineError,
            _BLOCKED.format(lots="0:2, 0:5"),
            id="fragments-beyond-tolerance",
        ),
        # A $20 Dec-31 close values the 3 trade-date shares at $60, a third
        # above the scaled $45.
        pytest.param(
            _REPRO,
            {"dec31_close": Decimal(20)},
            None,
            MARKERS,
            RobinhoodPipelineError,
            _BLOCKED.format(lots="0:2, 0:5"),
            id="yahoo-close-off",
        ),
    ],
)


def _refusing(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    trades: tuple[tuple[RobinhoodEvent, ...], RobinhoodEvent],
    changes: dict[str, Any],
    forged: Decimal | None,
    markers: tuple[str, ...],
) -> None:
    """Load the refusing case, after checking ``markers`` are its input's."""
    assert _load(monkeypatch, tmp_path, trades, **changes) == markers
    if forged is not None:
        forge_settlement_anchors(monkeypatch, trade_date_quantity=forged)


@_REFUSALS
def test_robinhood_fa_still_refuses_and_leaves_no_output(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    trades: tuple[tuple[RobinhoodEvent, ...], RobinhoodEvent],
    changes: dict[str, Any],
    forged: Decimal | None,
    markers: tuple[str, ...],
    error: type[Exception],
    text: str,
) -> None:
    """#1246: a pending trade without its row, or with no shares on the
    statement, fragments beyond tolerance or a Yahoo close off the statement
    still refuses robinhood-fa with exit 4 and its message alone: the A3
    closing refusals name their lots (#1550). No FA output remains, an earlier
    run's included, and no marker of the run's input is printed."""
    _refusing(monkeypatch, tmp_path, trades, changes, forged, markers)
    _stale_fa_outputs(tmp_path / "out")

    assert _main("robinhood-fa", tmp_path, "--no-fetch-prices") == 4

    captured = capsys.readouterr()
    assert captured.err == f"error: {text}\n"
    assert captured.out == ""
    assert list((tmp_path / "out").iterdir()) == []
    assert_no_markers(captured.out + captured.err, *markers)


@_REFUSALS
def test_run_robinhood_fa_still_raises_without_the_input(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    trades: tuple[tuple[RobinhoodEvent, ...], RobinhoodEvent],
    changes: dict[str, Any],
    forged: Decimal | None,
    markers: tuple[str, ...],
    error: type[Exception],
    text: str,
) -> None:
    """#1246: run_robinhood_fa raises the existing refusal, after Schedule FA's
    A3 closing blocker where a 31 December close decides it; one that needs no
    price stops the run before the prices, with no Schedule FA compute (#1574).
    Its text, repr, args and chain, suppressed context included, hold no
    marker of the run's input."""
    _refusing(monkeypatch, tmp_path, trades, changes, forged, markers)
    results: list[Any] = []
    compute = pipeline._compute_fa

    def recorded(*args: Any, **kwargs: Any) -> Any:
        results.append(compute(*args, **kwargs))
        return results[-1]

    monkeypatch.setattr(pipeline, "_compute_fa", recorded)

    with pytest.raises(error) as raised:
        run_robinhood_fa(**_run_kwargs(tmp_path), fetch_prices=False)

    assert type(raised.value) is error
    assert str(raised.value) == text
    # An unreconciled bundle never reaches Schedule FA, and only the close off
    # the statement needs the prices first.
    blocked = [{RobinhoodFaBlockerKind.A3_CLOSING}] if "dec31_close" in changes else []
    assert [{blocker.kind for blocker in result.blockers} for result in results] == blocked
    assert_prints_no_marker(raised.value, markers=markers)
