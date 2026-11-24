"""Exact IBKR Trades values through the real CLI (#1311).

Owner rule (#1166): exact, never round. The Activity Statement Trades parser
keeps every digit of each trade's quantity, ``|Basis|`` and net proceeds, and
the Open Positions reconciliation adds and compares exactly, so FIFO matching
and the filed figures see the statement's own values. The long-value
regressions use cells within the read-time digit limits (#1276) whose
arithmetic needs more than Decimal's default 28 digits; supporting cells
use ordinary values. Before #1311 the parser rounded the long values.
Every observer calls the real function; every rate is the vendored SBI TTBR,
and Rule 115 converts a transfer at the preceding month-end's, 30 Sep 2025's ₹88.35.
"""

from __future__ import annotations

from collections.abc import Callable
from fractions import Fraction
from pathlib import Path
from typing import Any, Final

import pytest

from ibkr_fa import pipeline_dispatch
from ibkr_fa.models import ClosedLot, OpenLot
from tests._cg_cli_runs import filed_cell, run_cg
from tests._crash_reports import assert_replaced_unchained
from tests._ibkr_fa_sums_inputs import read_csv_dict_rows as _rows
from tests._ibkr_fa_sums_inputs import run_schedule_fa_cy2025 as _schedule_fa
from tests._ibkr_lot_share_inputs import CY_2025_PERIOD as _CY_2025
from tests._ibkr_lot_share_inputs import FY_2025_26_PERIOD as _FY_2025_26
from tests._ibkr_lot_share_inputs import NAV_SECTIONS as _NAV
from tests._ibkr_lot_share_inputs import record_stcg_compute as _record_stcg_compute
from tests._ibkr_lot_share_inputs import write_lot_share_inputs as _write_inputs
from tests._ibkr_lot_share_inputs import xqbb_trade as _trade

# A neutral digit run, written contiguously into each refused value.
_MARKER: Final = "7362509"

_OVERSELL: Final = (
    "error: LotMatchingError in section 'Trades' at row 1: the closing trade closes more "
    "shares than the earlier opening trades of its symbol hold, so part of it can't be "
    "FIFO-matched. The FII section may be truncated, or the opening trades pre-date this "
    "Activity Statement's period — check Open Positions reconciliation upstream.\n"
)
_NO_OPEN_POSITION: Final = (
    "error: LotMatchingError in section 'Trades' at row 0: the opening trade leaves a "
    "surviving open lot after FIFO matching, but its symbol does not appear in the Open "
    "Positions section, so the Dec 31 close price can't be determined. See "
    "https://github.com/jay-tau/ibkr-fa/issues/133.\n"
)
_RECONCILIATION: Final = (
    "error: ParseError in section 'Open Positions' at row 0, column 'Quantity': "
    "reconciliation failed: the total quantity reconstructed from Trades for this Summary "
    "row's Symbol differs from its Quantity by more than 0.0001. If part of the holding was "
    "bought before the statement period, re-export the Activity Statement starting from "
    "your account's first trade. Otherwise, refusing output to avoid corrupting Schedule "
    "FA — please file an issue with a redacted statement.\n"
)


def _summary(quantity: str, close_price: str, value: str) -> str:
    """An XQBB Open Positions Summary row, then the NAV sections."""
    return (
        f"Open Positions,Data,Summary,Stocks,USD,XQBB,{quantity},1,1,0.00,{close_price},"
        f"{value},0.00,\n" + _NAV
    )


def _record(monkeypatch: pytest.MonkeyPatch, name: str) -> tuple[list[Any], list[BaseException]]:
    """Record each result of, and each exception leaving, ``pipeline_dispatch.<name>``."""
    results: list[Any] = []
    raised: list[BaseException] = []
    real: Callable[..., Any] = getattr(pipeline_dispatch, name)

    def record(*args: Any, **kwargs: Any) -> Any:
        try:
            result = real(*args, **kwargs)
        except Exception as error:
            raised.append(error)
            raise
        results.append(result)
        return result

    monkeypatch.setattr(pipeline_dispatch, name, record)
    return results, raised


def _assert_refusal(
    capsys: pytest.CaptureFixture[str],
    raised: list[BaseException],
    stderr: str,
    *markers: str,
) -> None:
    """Stderr is exactly ``stderr``, from the one recorded refusal, and no
    marker is printed or on that refusal."""
    captured = capsys.readouterr()
    assert captured.err == stderr
    assert captured.out == ""
    (error,) = raised
    assert f"error: {error}\n" == stderr
    assert_replaced_unchained(error, markers=markers)


# ---- schedule-cg: net proceeds past 28 digits ---------------------------------


def test_closing_basis_past_28_digits_files_its_exact_gain(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The issue's item 1. Fails on aa7ed98: the 29-digit closing Basis
    rounded to net proceeds of 123456789.5246179966044142615, a consideration
    of ₹10,907,407,354.5000…035, and a filed gain of ₹10,907,319,005. Exact,
    the consideration is ₹10,907,407,354.4999…991 and the gain ₹10,907,319,004.

    The closed lot carries the exact proceeds as a rational. Its review
    ``_audit: Proceeds USD`` cell is lot matching's Decimal of that rational
    at 28 digits, cut toward zero (#1273): that display division is outside
    #1311, so the cell can't print the 29th digit."""
    results = _record_stcg_compute(monkeypatch)
    matched, _raised = _record(monkeypatch, "match_fifo")
    basis = "123456789.52461799660441426146"
    statement, config = _write_inputs(
        tmp_path,
        _FY_2025_26,
        _trade("2025-06-16", "1", "1000", "-1000.00", "1000.00", "0", "O")
        + _trade("2025-10-15", "-1", "123456789.52", "123456789.52", f"-{basis}", "0", "C"),
    )
    out = tmp_path / "cg.csv"

    rc = run_cg("schedule-cg", statement, config, out, "--format", "review-csv")

    assert rc == 0
    stdout = [line.strip() for line in capsys.readouterr().out.splitlines()]
    assert "TOTAL realized gain (rounded ₹): 10907319004" in stdout
    assert filed_cell(out) == "10907319004"
    (result,) = results
    assert str(result.portal_amounts.consideration_inr) == "10907407354"
    ((_open_lots, closed_lots),) = matched
    (lot,) = closed_lots
    assert isinstance(lot, ClosedLot)
    assert lot.proceeds_money_exact == Fraction(basis)
    assert _rows(out)[0]["_audit: Proceeds USD"] == "123456789.5246179966044142614"


# ---- FIFO quantities past 28 digits ----------------------------------------------


def _remainder_statement(
    tmp_path: Path,
    tail: str,
    bought: str = "100000000.00000000000000000001",
    sold: str = "-100000000",
) -> tuple[Path, Path]:
    """Buy ``bought`` XQBB shares, then sell ``sold`` of them."""
    return _write_inputs(
        tmp_path,
        _CY_2025,
        _trade("2025-09-30", bought, "1", "-100.00", "100.00", "0", "O")
        + _trade("2025-11-10", sold, "1", "100.00", "-100.00", "0", "C"),
        tail,
    )


def test_a_fifo_remainder_open_positions_lists_runs(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Fails on aa7ed98: the buy of 100000000.00000000000000000001 shares
    rounded to 100000000, so the sale closed it all, and the Summary row for
    the 1E-20 share left over refused as having no open lots. Exact, that
    share stays open and the statement runs (exit 3: fields left to fill)."""
    matched, _raised = _record(monkeypatch, "match_fifo")
    statement, config = _remainder_statement(
        tmp_path, _summary("0.00000000000000000001", "4", "0.00")
    )

    rc = _schedule_fa(statement, config, tmp_path / "fa.csv")

    assert rc == 3
    assert not any(line.startswith("error") for line in capsys.readouterr().err.splitlines())
    ((open_lots, _closed_lots),) = matched
    (lot,) = open_lots
    assert isinstance(lot, OpenLot)
    assert str(lot.quantity) == "1E-20"


# Each pair leaves 1E-20 share open. The second's whole digits hold the marker.
@pytest.mark.parametrize(
    ("bought", "sold"),
    [
        ("100000000.00000000000000000001", "-100000000"),
        (f"{_MARKER}0000.00000000000000000001", f"-{_MARKER}0000"),
    ],
    ids=["issue", "marker"],
)
def test_a_fifo_remainder_open_positions_omits_refuses(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    bought: str,
    sold: str,
) -> None:
    """Fails on aa7ed98, which rounded the remainder away and reported no
    open holding (exit 3). Exact, the surviving lot has no Open Positions row,
    so the run refuses at its opening trade with no value printed."""
    _matched, raised = _record(monkeypatch, "match_fifo")
    statement, config = _remainder_statement(tmp_path, _NAV, bought, sold)

    rc = _schedule_fa(statement, config, tmp_path / "fa.csv")

    assert rc == 4
    _assert_refusal(capsys, raised, _NO_OPEN_POSITION, *((_MARKER,) if _MARKER in bought else ()))


# Each sale is 1E-20 share more than its buy. The second's whole digits hold
# the marker.
@pytest.mark.parametrize(
    ("bought", "sold"),
    [
        ("100000000", "-100000000.00000000000000000001"),
        (f"{_MARKER}0000", f"-{_MARKER}0000.00000000000000000001"),
    ],
    ids=["issue", "marker"],
)
def test_a_sale_past_its_buys_only_past_28_digits_refuses(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    bought: str,
    sold: str,
) -> None:
    """Fails on aa7ed98: the sale rounded to the shares bought and filed
    (exit 0). Exact, it closes more than was bought and refuses at the
    closing trade with no value printed."""
    _matched, raised = _record(monkeypatch, "match_fifo")
    statement, config = _write_inputs(
        tmp_path,
        _FY_2025_26,
        _trade("2025-06-16", bought, "1", "-100.00", "100.00", "0", "O")
        + _trade("2025-10-15", sold, "1", "100.00", "-100.00", "0", "C"),
    )

    rc = run_cg("schedule-cg", statement, config, tmp_path / "cg.csv", "--format", "review-csv")

    assert rc == 4
    _assert_refusal(capsys, raised, _OVERSELL, *((_MARKER,) if _MARKER in sold else ()))


def test_a_sale_of_exactly_its_buys_files(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Fails on aa7ed98: each buy of 100000000.00000000000000000004 rounded
    down and the sale of both rounded up, so it refused as closing more than
    was bought. Exact, it closes both lots, and the review CSV prints each
    lot's quantity with every digit."""
    statement, config = _write_inputs(
        tmp_path,
        _FY_2025_26,
        _trade("2025-06-16", "100000000.00000000000000000004", "1", "-100.00", "100.00", "0", "O")
        + _trade("2025-06-17", "100000000.00000000000000000004", "1", "-100.00", "100.00", "0", "O")
        + _trade(
            "2025-10-15", "-200000000.00000000000000000008", "1", "200.00", "-200.00", "0", "C"
        ),
    )
    out = tmp_path / "cg.csv"

    rc = run_cg("schedule-cg", statement, config, out, "--format", "review-csv")

    assert rc == 0
    stdout = [line.strip() for line in capsys.readouterr().out.splitlines()]
    assert "TOTAL realized gain (rounded ₹): 0" in stdout
    quantities = [row["Quantity"] for row in _rows(out) if row["Symbol"] == "XQBB"]
    assert quantities == ["100000000.00000000000000000004"] * 2


# ---- schedule-fa: the Open Positions reconciliation -------------------------------


# A Summary Quantity, and the first of two buys that leave it
# 0.00010000000000000001 short. The second's whole digits hold the marker.
@pytest.mark.parametrize("quantity", ["100000000000", f"{_MARKER}0000"], ids=["issue", "marker"])
def test_a_reconciliation_past_its_tolerance_only_past_28_digits_refuses(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    quantity: str,
) -> None:
    """The issue's item 2, first example. Fails on aa7ed98: the lots' sum
    rounded to within 0.0001 of the Summary row and the run reported (exit
    3). Exact, the difference is past the tolerance, so it refuses with no
    value printed."""
    _results, raised = _record(monkeypatch, "validate_reconciliation")
    statement, config = _write_inputs(
        tmp_path,
        _CY_2025,
        _trade("2025-09-30", quantity, "1", "-100.00", "100.00", "0", "O")
        + _trade("2025-10-01", "0.00010000000000000001", "1", "-100.00", "100.00", "0", "O"),
        _summary(quantity, "1", quantity),
    )

    rc = _schedule_fa(statement, config, tmp_path / "fa.csv")

    assert rc == 4
    _assert_refusal(capsys, raised, _RECONCILIATION, *((_MARKER,) if _MARKER in quantity else ()))


def test_a_reconciliation_within_its_tolerance_only_exactly_runs(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The issue's item 2, second example. Fails on aa7ed98: each running
    sum rounded up, past 0.0001 from the Summary row, and it refused. Exact,
    the difference is 0.00009999999999999999 and the run reports (exit 3)."""
    statement, config = _write_inputs(
        tmp_path,
        _CY_2025,
        _trade("2025-09-29", "100000000000", "1", "-100.00", "100.00", "0", "O")
        + _trade("2025-09-30", "0.00003333333333335000", "1", "-100.00", "100.00", "0", "O")
        + _trade("2025-10-01", "0.00003333333333335000", "1", "-100.00", "100.00", "0", "O")
        + _trade("2025-10-02", "0.00003333333333329999", "1", "-100.00", "100.00", "0", "O"),
        _summary("100000000000", "1", "100000000000"),
    )

    rc = _schedule_fa(statement, config, tmp_path / "fa.csv")

    assert rc == 3
    assert not any(line.startswith("error") for line in capsys.readouterr().err.splitlines())
