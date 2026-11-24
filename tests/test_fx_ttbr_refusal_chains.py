"""A TTBR row whose DATE isn't a real date and time stays out of the refusal's
chain (#1225).

``parse_ttbr_row`` refuses such a cell in fixed text, over ``strptime``'s error,
which quotes the cell, or for a day out of range names nothing (Python 3.12 and
3.13) or the year (3.14). ``load_ttbr`` refuses the row with a frozen ``InvariantError`` naming
the file, the fixed text and the line. It raises it after its handler, from a
content-free stand-in naming only the row error's class and line: the refusal
has no context (except a caller's own handled exception), and
``IBKR_FA_TRACEBACK=1`` still re-raises it as an error that replaced a crash.
Zero-rate rows, ``24:00``, the first-error order and the other row rules'
fixed messages are unchanged.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from importlib import resources
from pathlib import Path
from types import SimpleNamespace
from typing import NamedTuple

import pytest

from ibkr_fa import cli
from ibkr_fa.exceptions import InvariantError, ReplacedCrashError
from ibkr_fa.fx import ttbr_loader
from ibkr_fa.invariants import invariant_error
from tests._crash_reports import (
    assert_prints_no_marker,
    crash_pattern,
    raise_context,
)
from tests._schedule_os_e2e import run_schedule_os_on_dividends as _run_schedule_os
from tests._schedule_os_e2e import schedule_os_with_earlier_output as _schedule_os
from tests._ttbr_rows import SBI_CSV_HEADER as _HEADER
from tests._ttbr_rows import USD_TTBR_CSV as _USD_CSV
from tests._ttbr_rows import assert_ttbr_row_refusal as _assert_refusal
from tests._ttbr_rows import load_ttbr_lines as _load_lines
from tests._ttbr_rows import ttbr_row as _row


class _BadDate(NamedTuple):
    """A DATE cell of the ``YYYY-MM-DD HH:MM`` shape that ``strptime`` refuses,
    and its year, the marker it carries."""

    cell: str
    marker: str


# A distinct year from 9125 up per case, so no traceback line number holds it.
_HOUR_24 = _BadDate("9125-01-01 24:00", "9125")
_MONTH_13 = _BadDate("9126-13-01 09:00", "9126")
# Python 3.12 and 3.13 say only that the day is out of range; 3.14 names the year.
_FEBRUARY_30 = _BadDate("9127-02-30 09:00", "9127")
_BAD_DATES = pytest.mark.parametrize(
    "bad",
    [
        pytest.param(_HOUR_24, id="hour-24"),
        pytest.param(_MONTH_13, id="month-13"),
        pytest.param(_FEBRUARY_30, id="february-30"),
    ],
)
_NOT_REAL = "DATE is not a real date and time"
_RATE_SHAPE = "TT BUY is not plain digits with an optional fraction"

# Markers for the cells the other row rules refuse.
_COUNT_MARKER = "QZ25TCOUNT"
_NONPRINTABLE_MARKER = "QZ25TNPRNT"
_SHAPE_MARKER = "QZ25TSHAPE"
_RATE_MARKER = "QZ25TRATE"


# --- The loader ------------------------------------------------------------------


@pytest.mark.parametrize("rate", ["89.47", "0"], ids=["rate", "zero-rate"])
@_BAD_DATES
def test_a_date_that_is_not_real_stays_out_of_the_refusal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, bad: _BadDate, rate: str
) -> None:
    """A zero-rate row is checked too, before it is skipped; ``24:00`` is
    refused, not read as the next day's midnight."""
    lines = [_HEADER, _row(), _row(bad.cell, rate)]

    with pytest.raises(InvariantError) as refusal:
        _load_lines(lines, tmp_path, monkeypatch)

    _assert_refusal(refusal.value, _NOT_REAL, line=3, markers=(bad.marker,))


@pytest.mark.parametrize(
    ("first", "second", "problem", "marker"),
    [
        pytest.param(
            _row(_HOUR_24.cell),
            _row(_MONTH_13.cell),
            _NOT_REAL,
            _HOUR_24.marker,
            id="hour-24-first",
        ),
        pytest.param(
            _row(_MONTH_13.cell),
            _row(_HOUR_24.cell),
            _NOT_REAL,
            _MONTH_13.marker,
            id="month-13-first",
        ),
        pytest.param(
            _row(rate=_RATE_MARKER),
            _row(_FEBRUARY_30.cell),
            _RATE_SHAPE,
            _RATE_MARKER,
            id="rate-shape-first",
        ),
    ],
)
def test_the_first_refused_row_is_the_one_reported(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    first: str,
    second: str,
    problem: str,
    marker: str,
) -> None:
    """Two refused rows: the earlier one refuses, and only its cell's
    ``marker`` is checked, since the later row is never parsed."""
    with pytest.raises(InvariantError) as refusal:
        _load_lines([_HEADER, first, second], tmp_path, monkeypatch)

    _assert_refusal(refusal.value, problem, line=2, markers=(marker,))


@pytest.mark.parametrize(
    ("line_text", "problem", "marker"),
    [
        pytest.param(
            f"2025-12-31 09:00,{_COUNT_MARKER},89.47",
            "row must have exactly 10 cells",
            _COUNT_MARKER,
            id="cell-count",
        ),
        pytest.param(
            _row(pdf=f"{_NONPRINTABLE_MARKER}\t"),
            "PDF FILE cell has a non-printable character",
            _NONPRINTABLE_MARKER,
            id="non-printable-cell",
        ),
        pytest.param(
            _row(_SHAPE_MARKER), "DATE is not YYYY-MM-DD HH:MM", _SHAPE_MARKER, id="date-shape"
        ),
        pytest.param(_row(rate=_RATE_MARKER), _RATE_SHAPE, _RATE_MARKER, id="rate-shape"),
    ],
)
def test_other_row_rules_keep_their_fixed_message_and_only_the_stand_in(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, line_text: str, problem: str, marker: str
) -> None:
    with pytest.raises(InvariantError) as refusal:
        _load_lines([_HEADER, line_text], tmp_path, monkeypatch)

    _assert_refusal(refusal.value, problem, line=2, markers=(marker,))


@_BAD_DATES
def test_refusal_under_a_caller_handler_has_only_that_exception_as_context(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, bad: _BadDate
) -> None:
    """The refusal is raised after the loader's own handler, so a caller's
    handled exception is its context, as for any raise in a handler; the row
    error is still only the stand-in."""
    lines = [_HEADER, _row(bad.cell)]

    try:
        raise_context()
    except ValueError as caller:
        handled = caller
        with pytest.raises(InvariantError) as refusal:
            _load_lines(lines, tmp_path, monkeypatch)

    error = refusal.value
    assert type(error) is InvariantError
    assert error == invariant_error(what=f"sbi_ttbr_xxx_inr.csv: {_NOT_REAL}", line=2)
    assert_prints_no_marker(error, markers=(bad.marker,))
    assert error.__context__ is handled
    stand_in = error.__cause__
    assert type(stand_in) is ReplacedCrashError
    assert re.fullmatch(crash_pattern(ValueError), str(stand_in)), str(stand_in)
    assert stand_in.__cause__ is None
    assert stand_in.__context__ is None


# --- The CLI -------------------------------------------------------------------


@pytest.fixture
def bad_usd_ttbr(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """The loader reads a synthetic USD file whose second data row has month
    13 in place of the bundled one. Its cache is cleared before and after, so
    the bundled map isn't reused here and no other test sees this file."""
    fake = tmp_path / "reference" / _USD_CSV
    fake.parent.mkdir()
    fake.write_text("\n".join([_HEADER, _row(), _row(_MONTH_13.cell)]) + "\n", encoding="utf-8")

    class _Reference:
        def joinpath(self, name: str) -> Path:
            assert name == _USD_CSV
            return fake

    def files(package: str) -> _Reference:
        assert package == "ibkr_fa.reference"
        return _Reference()

    # Only the loader's view of ``importlib.resources``: other reference data
    # still reads the bundled files.
    monkeypatch.setattr(
        ttbr_loader, "resources", SimpleNamespace(files=files, as_file=resources.as_file)
    )
    ttbr_loader.load_ttbr.cache_clear()
    yield
    ttbr_loader.load_ttbr.cache_clear()


@pytest.mark.usefixtures("bad_usd_ttbr")
def test_schedule_os_refuses_a_date_that_is_not_real(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Converting the statement's USD dividends loads the file, which refuses:
    exit 4 with the fixed message only, and the earlier output removed."""
    out = _schedule_os(tmp_path)

    assert _run_schedule_os(tmp_path) == 4

    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == (
        f"error: InvariantError: {_USD_CSV}: {_NOT_REAL} (context keys: line=3)\n"
    )
    assert list(out.iterdir()) == []


@pytest.mark.usefixtures("bad_usd_ttbr")
def test_traceback_mode_still_reraises_the_refusal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """``IBKR_FA_TRACEBACK=1`` re-raises the refusal, after cleanup, as a
    domain error that replaced a crash, with nothing of the row error but its
    stand-in."""
    out = _schedule_os(tmp_path)
    monkeypatch.setenv("IBKR_FA_TRACEBACK", "1")

    with pytest.raises(InvariantError) as refusal:
        _run_schedule_os(tmp_path)

    assert cli._holds_crash(refusal.value)
    _assert_refusal(
        refusal.value, _NOT_REAL, line=3, markers=(_MONTH_13.marker,), csv_name=_USD_CSV
    )
    assert capsys.readouterr().out == ""
    assert list(out.iterdir()) == []
