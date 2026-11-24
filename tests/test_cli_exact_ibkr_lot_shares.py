"""Exact IBKR FIFO lot shares through the real CLI (#1273).

Owner rule (#1166): exact, never round. These are the issue's regressions
through the real IBKR ``schedule-cg`` and ``schedule-fa`` commands, on
synthetic Activity Statements built inline. The parser takes an open's cost
as ``|Basis|`` and a close's net proceeds as ``|Basis| + Realized P/L``.
Nothing upstream is patched: every rate is the vendored SBI TTBR, and Rule
115 converts a transfer at the preceding month-end's, 30 Sep 2025's ₹88.35.
The one exception is the digit cap: since #1276 the Trades readers refuse a
quantity or Basis with more than 20 decimal places when read, and no number
within those limits can reach the 1,000-digit cap, so the cap tests stub that
limit out of them, and the read-limit tests pin their refusal of the same
statements.
"""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path
from typing import Any, Final

import pytest

from ibkr_fa import pipeline_dispatch
from ibkr_fa.exceptions import IbkrFaError, LotMatchingError
from tests._cg_cli_runs import filed_cell, run_cg
from tests._crash_reports import assert_no_markers, render_without_frames
from tests._exact_refusals import TOO_MANY_DIGITS
from tests._ibkr_fa_sums_inputs import read_csv_dict_rows as _rows
from tests._ibkr_fa_sums_inputs import run_schedule_fa_cy2025 as _schedule_fa
from tests._ibkr_lot_share_inputs import CY_2025_PERIOD as _CY_2025
from tests._ibkr_lot_share_inputs import FY_2025_26_PERIOD as _FY_2025_26
from tests._ibkr_lot_share_inputs import NAV_SECTIONS as _NAV
from tests._ibkr_lot_share_inputs import record_stcg_compute as _record_stcg_compute
from tests._ibkr_lot_share_inputs import write_lot_share_inputs as _write_inputs
from tests._ibkr_lot_share_inputs import xqbb_trade as _trade
from tests._number_limits import MONEY, QUANTITY

# Neutral digit runs, each written contiguously in the input. A quantity or
# Basis this small needs more than 1,000 places, so no exact share holds it.
_QUANTITY_MARKER: Final = "7362509"
_BASIS_MARKER: Final = "7362519"
_TINY_QUANTITY: Final = f"{_QUANTITY_MARKER}E-1008"
_TINY_BASIS: Final = f"{_BASIS_MARKER}E-1008"
_SHARE_PAST_THE_CAP: Final = (
    "LotMatchingError in section 'Trades' at row {row}: a lot's cost or proceeds share "
    + TOO_MANY_DIGITS
)

# The modules under ``parser`` whose digit limit a quantity or Basis meets: the
# Trades totals check, which runs first, and ``_cells``, which parses the Trades
# parser's decimal cells.
_TRADES_READERS: Final = ("activity_statement._trades_totals", "_cells")

_A3_INITIAL: Final = "Initial value of the investment"
_A3_PROCEEDS: Final = "Total gross proceeds from sale or redemption of investment during the period"


def _record_match_refusals(monkeypatch: pytest.MonkeyPatch) -> list[IbkrFaError]:
    """Record each refusal as it leaves the real ``match_fifo``."""
    raised: list[IbkrFaError] = []
    real = pipeline_dispatch.match_fifo

    def match(*args: Any, **kwargs: Any) -> Any:
        try:
            return real(*args, **kwargs)
        except IbkrFaError as error:
            raised.append(error)
            raise

    monkeypatch.setattr(pipeline_dispatch, "match_fifo", match)
    return raised


def _assert_share_refusal(
    capsys: pytest.CaptureFixture[str], raised: list[IbkrFaError], *, row: int, marker: str
) -> None:
    """Stderr is exactly the share refusal at ``row``, raised with no cause or
    context, and ``marker`` is neither printed nor on the exception."""
    expected = _SHARE_PAST_THE_CAP.format(row=row)
    captured = capsys.readouterr()
    assert captured.err == f"error: {expected}\n"
    assert captured.out == ""
    (error,) = raised
    assert type(error) is LotMatchingError
    assert str(error) == expected
    assert error.__cause__ is None
    assert error.__context__ is None
    rendered = render_without_frames(error)
    assert_no_markers(
        captured.out + captured.err + rendered + repr(error) + repr(error.args), marker
    )


# ---- Example 1: schedule-cg -------------------------------------------------------


@pytest.mark.parametrize(("format_id", "suffix"), [("review-csv", ".csv"), ("xlsx", ".xlsx")])
def test_whole_sale_files_its_exact_proceeds(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    format_id: str,
    suffix: str,
) -> None:
    """Fails on 0d3547b: 3 shares bought for $9.00 and sold for net $10.00
    came out $9.999…9 of proceeds, so consideration was ₹883.49…, filed ₹883,
    and the filed STCG ₹88. Exact, consideration is ₹883.50: ₹884 and ₹89."""
    results = _record_stcg_compute(monkeypatch)
    statement, config = _write_inputs(
        tmp_path,
        _FY_2025_26,
        _trade("2025-06-16", "3", "3", "-9.00", "9.00", "0", "O")
        + _trade("2025-10-15", "-3", "3.3333", "10.00", "-9.00", "1.00", "C"),
    )
    out = tmp_path / f"cg{suffix}"

    rc = run_cg("schedule-cg", statement, config, out, "--format", format_id)

    assert rc == 0
    stdout = [line.strip() for line in capsys.readouterr().out.splitlines()]
    assert "TOTAL realized gain (rounded ₹): 89" in stdout
    assert filed_cell(out) == "89"
    (result,) = results
    amounts = result.portal_amounts
    assert amounts.consideration_inr == Decimal(884)
    assert amounts.cost_of_acquisition_inr == Decimal(795)


# ---- Example 2: schedule-fa -------------------------------------------------------


@pytest.mark.parametrize("args", [(), ("--format", "filing-csv")], ids=["default", "filing-csv"])
def test_surviving_lot_files_its_exact_initial_value(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], args: tuple[str, ...]
) -> None:
    """Fails on 0d3547b: of 3 shares bought for $10.00 and 2 sold, the
    surviving lot cost $3.333333333333333333333333333, so its A3 initial value
    was ₹294.49…, filed ₹294. Exact, it is ₹294.50: ₹295. The closed lot's
    ₹589 is a control in the same run."""
    statement, config = _write_inputs(
        tmp_path,
        _CY_2025,
        _trade("2025-09-30", "3", "3.3333", "-10.00", "10.00", "0", "O")
        + _trade("2025-11-10", "-2", "4", "8.00", "-6.67", "1.33", "C"),
        "Open Positions,Data,Summary,Stocks,USD,XQBB,1,1,3.3333,3.33,4,4,0.67,\n" + _NAV,
    )
    out = tmp_path / "schedule-fa.csv"

    rc = _schedule_fa(statement, config, out, *args)

    capsys.readouterr()
    # Peak and nature of entity are left for manual fill.
    assert rc == 3
    # The open lot sells nothing; the closed lot sells 2 shares at $4.
    initial_values = {row[_A3_PROCEEDS]: row[_A3_INITIAL] for row in _rows(out)}
    assert initial_values == {"0": "295", "716": "589"}


# ---- the digit cap ----------------------------------------------------------------


def _without_read_limits(monkeypatch: pytest.MonkeyPatch) -> None:
    """Stub the read-time digit limit (#1276) out of the Trades readers, so a
    tiny quantity or Basis reaches lot matching."""
    for reader in _TRADES_READERS:
        monkeypatch.setattr(
            f"ibkr_fa.parser.{reader}.number_limit_breach",
            lambda _value, _kind: None,
            raising=False,
        )


# A whole sale of a lot whose quantity, or whose Basis, is tiny.
_TINY_QUANTITY_SLICE: Final = _trade(
    "2025-06-16", _TINY_QUANTITY, "3", "-9.00", "9.00", "0", "O"
) + _trade("2025-10-15", f"-{_TINY_QUANTITY}", "3.3333", "10.00", "-9.00", "1.00", "C")
_TINY_BASIS_SLICE: Final = _trade("2025-06-16", "3", "3", "-9.00", _TINY_BASIS, "0", "O") + _trade(
    "2025-10-15", "-3", "3.3333", "10.00", f"-{_TINY_BASIS}", "0", "C"
)
# The open lot: two shares, then one at the tiny cost, then a sale of two.
_OPEN_LOT_TRADES: Final = (
    _trade("2025-09-30", "2", "3", "-6.00", "6.00", "0", "O")
    + _trade("2025-10-01", "1", "3", "-3.00", _TINY_BASIS, "0", "O")
    + _trade("2025-11-10", "-2", "4", "8.00", "-6.00", "2.00", "C")
)
_OPEN_LOT_TAIL: Final = "Open Positions,Data,Summary,Stocks,USD,XQBB,1,1,3,3.00,4,4,1.00,\n" + _NAV


@pytest.mark.parametrize(
    ("trades", "marker"),
    [
        pytest.param(_TINY_QUANTITY_SLICE, _QUANTITY_MARKER, id="quantity"),
        pytest.param(_TINY_BASIS_SLICE, _BASIS_MARKER, id="basis"),
    ],
)
@pytest.mark.parametrize(("format_id", "suffix"), [("review-csv", ".csv"), ("xlsx", ".xlsx")])
@pytest.mark.parametrize("stale", [False, True], ids=["fresh", "stale"])
def test_slice_share_past_the_cap_refuses_at_the_close(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    trades: str,
    marker: str,
    format_id: str,
    suffix: str,
    stale: bool,
) -> None:
    """Fails on 0d3547b, which divided at 28 digits: the review CSV filed
    (exit 0), ₹88 STCG from the quantity case's $9.999…97 proceeds share and
    ₹0 from the basis case, and the workbook refused the tiny cell with a
    WriterContractError. Exact, the closed slice's share needs more than
    1,000 digits, so it refuses at the closing trade and leaves no output.
    Since #1276 the Trades readers' digit limits are stubbed out to reach the
    share (the next test pins their refusal)."""
    _without_read_limits(monkeypatch)
    raised = _record_match_refusals(monkeypatch)
    statement, config = _write_inputs(tmp_path, _FY_2025_26, trades)
    assert marker in statement.read_text(encoding="utf-8")
    out = tmp_path / f"cg{suffix}"
    inputs = sorted(tmp_path.iterdir())
    if stale:
        out.write_text("stale", encoding="utf-8")

    rc = run_cg("schedule-cg", statement, config, out, "--format", format_id)

    assert rc == 4
    _assert_share_refusal(capsys, raised, row=1, marker=marker)
    assert sorted(tmp_path.iterdir()) == inputs


def _assert_read_refusal(capsys: pytest.CaptureFixture[str], expected: str, *, marker: str) -> None:
    """Stderr is exactly the reader's refusal ``expected``, and ``marker`` is
    printed nowhere."""
    captured = capsys.readouterr()
    assert captured.err == f"error: {expected}\n"
    assert captured.out == ""
    assert_no_markers(captured.out + captured.err, marker)


@pytest.mark.parametrize(
    ("trades", "marker", "expected"),
    [
        pytest.param(
            _TINY_QUANTITY_SLICE,
            _QUANTITY_MARKER,
            f"ParseError in section 'Trades' at row 13, column 'Quantity': {QUANTITY}",
            id="quantity",
        ),
        pytest.param(
            _TINY_BASIS_SLICE,
            _BASIS_MARKER,
            f"ParseError in section 'Trades' at row 13, column 'Basis': {MONEY}",
            id="basis",
        ),
    ],
)
@pytest.mark.parametrize(("format_id", "suffix"), [("review-csv", ".csv"), ("xlsx", ".xlsx")])
@pytest.mark.parametrize("stale", [False, True], ids=["fresh", "stale"])
def test_slice_number_past_its_read_limit_refuses_when_read(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    trades: str,
    marker: str,
    expected: str,
    format_id: str,
    suffix: str,
    stale: bool,
) -> None:
    """#1276: the same statements, unstubbed, refuse when read: the Trades
    totals check meets the opening trade's tiny quantity or Basis (1,008
    decimal places) on file line 13, before any matching. Exit 4, the fixed
    refusal alone, no marker printed, and no output, an earlier run's
    removed."""
    statement, config = _write_inputs(tmp_path, _FY_2025_26, trades)
    assert marker in statement.read_text(encoding="utf-8")
    out = tmp_path / f"cg{suffix}"
    inputs = sorted(tmp_path.iterdir())
    if stale:
        out.write_text("stale", encoding="utf-8")

    rc = run_cg("schedule-cg", statement, config, out, "--format", format_id)

    assert rc == 4
    _assert_read_refusal(capsys, expected, marker=marker)
    assert sorted(tmp_path.iterdir()) == inputs


@pytest.mark.parametrize("stale", [False, True], ids=["fresh", "stale"])
def test_open_lot_share_past_the_cap_refuses_at_the_open(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch, stale: bool
) -> None:
    """Fails on 0d3547b, which filed the surviving lot's tiny cost as a ₹0
    initial value (exit 3). Exact, the lot's cost share needs more than 1,000
    digits, so it refuses at its opening trade (row 1, not the close at row
    2) and leaves no output. Since #1276 the Trades readers' digit limits are
    stubbed out to reach the share (the next test pins their refusal)."""
    _without_read_limits(monkeypatch)
    raised = _record_match_refusals(monkeypatch)
    statement, config = _write_inputs(tmp_path, _CY_2025, _OPEN_LOT_TRADES, _OPEN_LOT_TAIL)
    assert _BASIS_MARKER in statement.read_text(encoding="utf-8")
    out = tmp_path / "schedule-fa.csv"
    a2 = tmp_path / "schedule-fa-a2.csv"
    inputs = sorted(tmp_path.iterdir())
    if stale:
        out.write_text("stale", encoding="utf-8")
        a2.write_text("stale", encoding="utf-8")

    rc = _schedule_fa(statement, config, out)

    assert rc == 4
    _assert_share_refusal(capsys, raised, row=1, marker=_BASIS_MARKER)
    assert sorted(tmp_path.iterdir()) == inputs


@pytest.mark.parametrize("stale", [False, True], ids=["fresh", "stale"])
def test_open_lot_number_past_its_read_limit_refuses_when_read(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], stale: bool
) -> None:
    """#1276: the same statement, unstubbed, refuses when read: the Trades
    totals check meets the second opening trade's tiny Basis (1,008 decimal
    places) on file line 14, before any matching. Exit 4, the fixed refusal
    alone, no marker printed, and no output, an earlier run's removed."""
    statement, config = _write_inputs(tmp_path, _CY_2025, _OPEN_LOT_TRADES, _OPEN_LOT_TAIL)
    assert _BASIS_MARKER in statement.read_text(encoding="utf-8")
    out = tmp_path / "schedule-fa.csv"
    a2 = tmp_path / "schedule-fa-a2.csv"
    inputs = sorted(tmp_path.iterdir())
    if stale:
        out.write_text("stale", encoding="utf-8")
        a2.write_text("stale", encoding="utf-8")

    rc = _schedule_fa(statement, config, out)

    assert rc == 4
    _assert_read_refusal(
        capsys,
        f"ParseError in section 'Trades' at row 14, column 'Basis': {MONEY}",
        marker=_BASIS_MARKER,
    )
    assert sorted(tmp_path.iterdir()) == inputs
