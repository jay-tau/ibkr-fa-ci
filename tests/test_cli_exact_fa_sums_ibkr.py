"""Exact Schedule FA dividend, interest and proceeds totals through IBKR
``schedule-fa`` and ``run-all`` (#1168).

Owner decision (2026-11-10, on #1166, applied to #1168): these sums never
round. They add exactly and refuse only a nonfinite amount, a decimal
arithmetic failure or an addition that would still round at
``EXACT_SUM_DIGITS``. Every statement here is a synthetic Activity Statement
built inline. The parser reads the operands straight from its cells (the
printed ``Total`` rows are exact sums). The Dec-31 TTBR is patched only for the
₹1/$ repro. Since #1276 the readers refuse a number past its read-time digit
limit when read, and no number within those limits can reach the 1,000-digit
cap, so the cap tests stub the limit out of the readers their numbers pass
through; the read-limit tests pin the readers' refusal of the same statements.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Final

import pytest

from ibkr_fa import cli
from ibkr_fa.exceptions import InvariantError, ParseError
from ibkr_fa.fx.policy_fa import FaExactDatePolicy
from ibkr_fa.pipeline_fa import run_fa_a3
from tests._crash_reports import assert_unchained_refusal
from tests._fa_exact_sums import (
    DESCRIPTION,
    HUGE,
    ISIN,
    MARKED_DATE,
    MARKERS,
    MINUTE,
    NUMERIC,
    SOURCE,
    SYMBOL,
    assert_refusal,
    message,
)
from tests._ibkr_fa_sums_inputs import A2_AMOUNT_COLUMN as _A2_AMOUNT
from tests._ibkr_fa_sums_inputs import A2_NATURE_COLUMN as _A2_NATURE
from tests._ibkr_fa_sums_inputs import A3_GROSS_COLUMN as _A3_GROSS
from tests._ibkr_fa_sums_inputs import XQBB_DIVIDEND as _XQBB_DIVIDEND
from tests._ibkr_fa_sums_inputs import ibkr_fa_sums_statement as _statement
from tests._ibkr_fa_sums_inputs import read_csv_dict_rows as _rows
from tests._ibkr_fa_sums_inputs import run_schedule_fa_cy2025 as _schedule_fa
from tests._ibkr_fa_sums_inputs import write_fa_sums_inputs as _write_inputs
from tests._ibkr_flex_cli_inputs import earlier_schedule_fa_outputs
from tests._ibkr_run_all import FY_2025_26_INPUT as _FY_2025_26_INPUT
from tests._ibkr_run_all import run_all_both as _run_all_both
from tests._ibkr_run_all import seed_every_target as _seed_every_target
from tests._number_limits import MONEY, PRICE

_XQCE_DIVIDEND = "XQCE(US99996E1007) Cash Dividend USD 0.25 per Share (Ordinary Dividend)"
# The marked symbol holds no lot, so its dividends reach no Table A3 row.
_MARKED_DIVIDEND = f"{SYMBOL}({ISIN}) {DESCRIPTION} {SOURCE}"


def _close_trades(symbol: str, close_day: date, price: str) -> str:
    """Buy one share of ``symbol`` and sell it on ``close_day`` at ``price``.

    Only ``T. Price`` feeds the gross proceeds, so the proceeds and P/L the
    Trades totals check stay ordinary.
    """
    return (
        f'Trades,Data,Order,Stocks,USD,{symbol},"2025-02-03, 10:00:00",1,100,100,-100,0,100,'
        "0,0,O\n"
        f'Trades,Data,Order,Stocks,USD,{symbol},"{close_day.isoformat()}, 10:00:00",-1,'
        f"{price},120,120,0,-100,20,0,C\n"
    )


# A closed lot carries no source or reason text.
_NO_SOURCE_MARKERS = tuple(marker for marker in MARKERS if marker != SOURCE)

# One payment per row, so each case reaches its sum through the parser.
_A3_DIVIDENDS: Final = _statement(
    dividends=(
        (date(2025, 3, 14), _XQBB_DIVIDEND, NUMERIC),
        (MARKED_DATE, _MARKED_DIVIDEND, HUGE),
        (date(2025, 9, 15), _MARKED_DIVIDEND, MINUTE),
    ),
)
_A2_INTEREST: Final = _statement(
    dividends=((MARKED_DATE, _MARKED_DIVIDEND, "1.00"),),
    # A credit needs IBKR's exact wording (#1280), so the markers sit on the
    # dividend row.
    interest=(
        (date(2025, 1, 31), "USD Credit Interest for Jan-2025", NUMERIC),
        (MARKED_DATE, "USD Credit Interest for Jun-2025", HUGE),
        (date(2025, 9, 30), "USD Credit Interest for Sep-2025", MINUTE),
    ),
)
_A2_PROCEEDS: Final = _statement(
    instruments=(
        "Financial Instrument Information,Data,Stocks,XQCC,SAMPLE ISSUER CC INC,"
        "33333,US99996C1009,NASDAQ,1,COMMON,\n"
        f"Financial Instrument Information,Data,Stocks,{SYMBOL},{DESCRIPTION},"
        f"44444,{ISIN},NASDAQ,1,COMMON,\n"
        "Financial Instrument Information,Data,Stocks,XQCD,SAMPLE ISSUER CD INC,"
        "55555,US99996D1008,NASDAQ,1,COMMON,\n"
    ),
    trades=(
        _close_trades("XQCC", date(2025, 3, 3), NUMERIC)
        + _close_trades(SYMBOL, MARKED_DATE, HUGE)
        + _close_trades("XQCD", date(2025, 9, 15), MINUTE)
    ),
)
_A2_DIVIDENDS: Final = _statement(
    dividends=(
        (date(2025, 3, 14), _XQBB_DIVIDEND, NUMERIC),
        (MARKED_DATE, _MARKED_DIVIDEND, HUGE),
        (date(2025, 9, 15), _XQCE_DIVIDEND, MINUTE),
    ),
)

# The modules under ``parser`` whose read-time digit limits each statement's
# absurd numbers meet: an income section's detail rows, its printed ``Total``
# and its ``Change in NAV`` line, or the Trades ``T. Price`` (which the Trades
# totals check never reads). ``_cells`` parses the readers' decimal cells; the
# Dividends reader also checks its per-share amount itself.
_DIVIDEND_READERS: Final = ("activity_statement.dividends", "activity_statement._totals", "_cells")
_INTEREST_READERS: Final = ("activity_statement._totals", "_cells")
_TRADE_PRICE_READERS: Final = ("_cells",)


def _without_read_limits(monkeypatch: pytest.MonkeyPatch, readers: tuple[str, ...]) -> None:
    """Stub the read-time digit limit (#1276) out of each module under
    ``ibkr_fa.parser`` in ``readers``. ``NUMERIC`` has 28 decimal places and
    ``MINUTE`` 995, past these readers' 20-place limit, so without this the
    reader refuses the first of them before any sum."""
    for reader in readers:
        monkeypatch.setattr(
            f"ibkr_fa.parser.{reader}.number_limit_breach",
            lambda _value, _kind: None,
            raising=False,
        )


_REFUSALS = pytest.mark.parametrize(
    ("statement", "readers", "expected", "markers"),
    [
        pytest.param(
            _A3_DIVIDENDS,
            _DIVIDEND_READERS,
            message(
                "Schedule FA per-symbol dividends, input row 3, column amount_gross",
                source_row_index=2,
            ),
            MARKERS,
            id="a3-per-symbol-dividends",
        ),
        pytest.param(
            _A2_INTEREST,
            _INTEREST_READERS,
            message(
                "Schedule FA A2 gross interest, input row 3, column amount_gross",
                source_row_index=2,
            ),
            MARKERS,
            id="a2-gross-interest",
        ),
        pytest.param(
            _A2_PROCEEDS,
            _TRADE_PRICE_READERS,
            message("Schedule FA A2 gross proceeds, input lot 3, field gross_proceeds_inr"),
            _NO_SOURCE_MARKERS,
            id="a2-gross-proceeds",
        ),
        pytest.param(
            _A2_DIVIDENDS,
            _DIVIDEND_READERS,
            message("Schedule FA A2 gross dividends, contribution 3, field dividend_total_inr"),
            MARKERS,
            id="a2-gross-dividends",
        ),
    ],
)

# Unstubbed, each kind of statement refuses at its first number past its
# read-time limit: ``NUMERIC``, the first row's amount or closing price.
_DIVIDEND_AMOUNT_TEXT: Final = (
    f"ParseError in section 'Dividends' at row 0, column 'Amount': {MONEY}"
)
_READ_REFUSALS = pytest.mark.parametrize(
    ("statement", "expected", "text", "markers"),
    [
        pytest.param(
            _A3_DIVIDENDS,
            ParseError(
                section="Dividends", row_index=0, column="Amount", message=MONEY, raw_value=""
            ),
            _DIVIDEND_AMOUNT_TEXT,
            MARKERS,
            id="dividend-amount",
        ),
        pytest.param(
            _A2_INTEREST,
            ParseError(
                section="Interest", row_index=0, column="Amount", message=MONEY, raw_value=""
            ),
            f"ParseError in section 'Interest' at row 0, column 'Amount': {MONEY}",
            MARKERS,
            id="interest-amount",
        ),
        pytest.param(
            _A2_PROCEEDS,
            ParseError(
                section="Trades", row_index=2, column="T. Price", message=PRICE, raw_value=""
            ),
            f"ParseError in section 'Trades' at row 2, column 'T. Price': {PRICE}",
            _NO_SOURCE_MARKERS,
            id="trade-price",
        ),
    ],
)

_FORMATS = pytest.mark.parametrize(
    ("format_id", "suffix", "extra_args"),
    [
        ("review-csv", ".csv", ()),
        ("filing-csv", ".csv", ()),
        ("xlsx", ".xlsx", ()),
        ("cbdt-json-fragment", ".json", ("--itr-form", "ITR-2")),
    ],
)
_STALE = pytest.mark.parametrize("stale", [False, True], ids=["fresh", "stale"])


@_REFUSALS
@_FORMATS
@_STALE
def test_schedule_fa_sum_past_the_exact_cap_refuses_and_clears_the_output(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    statement: str,
    readers: tuple[str, ...],
    expected: str,
    markers: tuple[str, ...],
    format_id: str,
    suffix: str,
    extra_args: tuple[str, ...],
    stale: bool,
) -> None:
    """#1168: each Schedule FA sum on the IBKR path refuses ``HUGE`` beside
    ``MINUTE`` (more digits than an exact sum may hold) with exit 4 and the
    located message alone, in every format, and leaves no output, removing an
    earlier run's. Base 3751eab5 rounded the sum and filed (exit 3). Since
    #1276 the readers' digit limits are stubbed out to reach the sum (the
    read-limit test below pins their refusal)."""
    _without_read_limits(monkeypatch, readers)
    statement_path, config = _write_inputs(tmp_path, statement)
    out = tmp_path / f"schedule-fa{suffix}"
    a2 = tmp_path / f"schedule-fa-a2{suffix}"
    if stale:
        earlier_schedule_fa_outputs(out, format_id)

    rc = _schedule_fa(statement_path, config, out, "--format", format_id, *extra_args)

    captured = capsys.readouterr()
    assert rc == 4
    assert captured.err == f"error: {expected}\n"
    assert captured.out == ""
    for marker in markers:
        assert marker in statement, marker
        assert marker not in captured.err, marker
    assert not out.exists()
    assert not a2.exists()


@_REFUSALS
def test_schedule_fa_pipeline_refusal_chains_nothing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    statement: str,
    readers: tuple[str, ...],
    expected: str,
    markers: tuple[str, ...],
) -> None:
    """#1168: the pipeline raises the located refusal itself, with no
    chained exception and no input value in the rendered chain. Since #1276
    the readers' digit limits are stubbed out to reach the sum."""
    _without_read_limits(monkeypatch, readers)
    statement_path, config = _write_inputs(tmp_path, statement)

    with pytest.raises(InvariantError) as raised:
        run_fa_a3(
            input_path=statement_path,
            config_path=config,
            output_path=tmp_path / "schedule-fa.csv",
            calendar_year=2025,
        )

    for marker in markers:
        assert marker in statement, marker
    assert_refusal(raised.value, expected, markers=markers)


@_READ_REFUSALS
@_FORMATS
@_STALE
def test_schedule_fa_number_past_its_read_limit_refuses_and_clears_the_output(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    statement: str,
    expected: ParseError,
    text: str,
    markers: tuple[str, ...],
    format_id: str,
    suffix: str,
    extra_args: tuple[str, ...],
    stale: bool,
) -> None:
    """#1276: the cap tests' statements, unstubbed, refuse when read, at the
    first number past its digit limit (``NUMERIC``'s 28 decimal places in the
    first income row's amount or closing trade's price): exit 4 and the fixed
    refusal alone, in every format, no marker printed, and no output, an
    earlier run's removed."""
    statement_path, config = _write_inputs(tmp_path, statement)
    out = tmp_path / f"schedule-fa{suffix}"
    a2 = tmp_path / f"schedule-fa-a2{suffix}"
    if stale:
        earlier_schedule_fa_outputs(out, format_id)

    rc = _schedule_fa(statement_path, config, out, "--format", format_id, *extra_args)

    captured = capsys.readouterr()
    assert rc == 4
    assert captured.err == f"error: {text}\n"
    assert captured.out == ""
    for marker in markers:
        assert marker in statement, marker
        assert marker not in captured.err, marker
    assert not out.exists()
    assert not a2.exists()


@_READ_REFUSALS
def test_schedule_fa_read_limit_refusal_chains_nothing(
    tmp_path: Path, statement: str, expected: ParseError, text: str, markers: tuple[str, ...]
) -> None:
    """#1276: the pipeline raises the reader's refusal unchained, every field
    fixed, with no input value anywhere in its exception graph."""
    statement_path, config = _write_inputs(tmp_path, statement)

    with pytest.raises(ParseError) as raised:
        run_fa_a3(
            input_path=statement_path,
            config_path=config,
            output_path=tmp_path / "schedule-fa.csv",
            calendar_year=2025,
        )

    for marker in markers:
        assert marker in statement, marker
    assert_unchained_refusal(raised.value, expected, text, markers=markers)


# ---- exact values (₹1/$) ---------------------------------------------------------

# ``tests._fa_exact_sums.SIX`` with 9 whole digits (#1276): its 1.499…998 and
# 4E-28 have 27 and 28 decimal places, past the money read limit of 20. Here
# ``_LONG`` has 9 whole digits and 19 places, still 28 digits, and ``_TINY``
# sits one place past it at 20, so the six still total exactly a half
# (USD 100,000,000.50) and the 28-digit context still drops each ``_TINY``
# added after ``_LONG``.
_LONG: Final = "100000000.4999999999999999998"
_TINY: Final = "4E-20"
_SIX: Final = (_LONG, *(_TINY,) * 5)


def _one_rupee_dec31_rate(monkeypatch: pytest.MonkeyPatch) -> None:
    """Serve ₹1/$ on Dec 31, 2025 and the vendored TTBR on every other date.

    The vendored SBI TTBR for that day is 89.47, at which 100000000.50 and
    the rounded 100000000.4999999999999999998 file the same whole rupee, and
    the statement has no way to choose its rate.
    """
    vendored = FaExactDatePolicy.rate_for

    def rate_for(self: FaExactDatePolicy, event_date: date) -> Decimal:
        if event_date == date(2025, 12, 31):
            return Decimal(1)
        return vendored(self, event_date)

    monkeypatch.setattr(FaExactDatePolicy, "rate_for", rate_for)


_A2_COMPONENTS = "_audit: Gross credited components (per nature)"


@pytest.mark.parametrize("amounts", [_SIX, _SIX[::-1]], ids=["long-first", "long-last"])
def test_six_dividend_payments_file_two_rupees(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    amounts: tuple[str, ...],
) -> None:
    """#1168: six XQBB payments totalling exactly USD 100,000,000.50 at ₹1/$
    file ₹100,000,001 in the holding's Table A3 row and in Table A2's dividend
    row. Long first, base 3751eab5 kept 100000000.4999999999999999998 and filed
    ₹100,000,000 in both. Long last, the 28-digit context drops only a zero
    and base also filed ₹100,000,001: a preservation pin that the exact sum
    keeps it rather than refusing. Until #1276 the payments were ``SIX``,
    totalling USD 1.50 and filing ₹2 (base ₹1); its places are now past the
    money read limit, so the name keeps the old two rupees."""
    _one_rupee_dec31_rate(monkeypatch)
    days = [date(2025, month, 15) for month in range(2, 8)]
    statement, config = _write_inputs(
        tmp_path,
        _statement(
            dividends=[
                (day, _XQBB_DIVIDEND, amount) for day, amount in zip(days, amounts, strict=True)
            ]
        ),
    )
    out = tmp_path / "schedule-fa.csv"

    rc = _schedule_fa(statement, config, out)

    capsys.readouterr()
    assert rc == 3
    [a3] = _rows(out)
    assert a3["_audit: Source ticker"] == "XQBB"
    assert a3[_A3_GROSS] == "100000001"
    [a2] = _rows(tmp_path / "schedule-fa-a2.csv")
    assert (a2[_A2_NATURE], a2[_A2_AMOUNT]) == ("Dividend", "100000001")
    nature, amount = a2[_A2_COMPONENTS].split("=")
    assert nature == "Dividend"
    assert Decimal(amount) == Decimal("100000000.50")


def test_six_interest_credits_file_two_rupees(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """#1168: six interest credits totalling exactly USD 100,000,000.50 at
    ₹1/$ file ₹100,000,001 in Table A2's interest row. Base 3751eab5 kept
    100000000.4999999999999999998 and filed ₹100,000,000. Until #1276 the
    credits were ``SIX``, totalling USD 1.50 and filing ₹2 (base ₹1); its
    places are now past the money read limit, so the name keeps the old two
    rupees."""
    _one_rupee_dec31_rate(monkeypatch)
    months = ("Feb", "Mar", "Apr", "May", "Jun", "Jul")
    statement, config = _write_inputs(
        tmp_path,
        _statement(
            interest=[
                (date(2025, number, 28), f"USD Credit Interest for {month}-2025", amount)
                for number, month, amount in zip(range(2, 8), months, _SIX, strict=True)
            ]
        ),
    )
    out = tmp_path / "schedule-fa.csv"

    rc = _schedule_fa(statement, config, out)

    capsys.readouterr()
    assert rc == 3
    [a2] = _rows(tmp_path / "schedule-fa-a2.csv")
    assert (a2[_A2_NATURE], a2[_A2_AMOUNT]) == ("Interest", "100000001")
    nature, amount = a2[_A2_COMPONENTS].split("=")
    assert nature == "Interest"
    assert Decimal(amount) == Decimal("100000000.50")


# ---- run-all ---------------------------------------------------------------------


def test_run_all_fa_leg_past_the_exact_cap_fails_only_schedule_fa(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """#1168: the run-all FA leg reaches the per-symbol dividend check on the
    CY statement and refuses it as standalone schedule-fa does. As for any
    refused leg (#927, #1012), only schedule-fa is in error and the run
    removes every target, an earlier run's included. Base 3751eab5 rounded
    the sum and the run exited 3. Since #1276 the Dividends readers' digit
    limits are stubbed out to reach the sum (the next test pins their
    refusal)."""
    _without_read_limits(monkeypatch, _DIVIDEND_READERS)
    cy_text = _A3_DIVIDENDS
    cy_input = tmp_path / "cy.csv"
    cy_input.write_text(cy_text, encoding="utf-8")
    out = tmp_path / "out"
    _seed_every_target(out)

    assert _run_all_both(tmp_path, _FY_2025_26_INPUT, cy_input) == 4

    assert list(out.iterdir()) == []
    captured = capsys.readouterr()
    expected = message(
        "Schedule FA per-symbol dividends, input row 3, column amount_gross", source_row_index=2
    )
    assert f"error: {expected}" in captured.err.splitlines()
    error_label = cli._RUN_ALL_STATUS_LABEL["error"]
    errors = [
        line.split()[1]
        for line in captured.err.splitlines()
        if line.lstrip().startswith(error_label)
    ]
    assert errors == ["schedule-fa"]
    assert captured.err.count("-> output removed because the run failed") == 6
    for marker in MARKERS:
        assert marker in cy_text, marker
        assert marker not in captured.out, marker
        assert marker not in captured.err, marker


def test_run_all_fa_leg_past_its_read_limit_fails_only_schedule_fa(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """#1276: the same CY statement, unstubbed, refuses when the FA leg reads
    it, at the first dividend's ``NUMERIC`` amount, as standalone schedule-fa
    does. Only schedule-fa is in error, the run removes every target, an
    earlier run's included, and no marker is printed."""
    cy_input = tmp_path / "cy.csv"
    cy_input.write_text(_A3_DIVIDENDS, encoding="utf-8")
    out = tmp_path / "out"
    _seed_every_target(out)

    assert _run_all_both(tmp_path, _FY_2025_26_INPUT, cy_input) == 4

    assert list(out.iterdir()) == []
    captured = capsys.readouterr()
    assert f"error: {_DIVIDEND_AMOUNT_TEXT}" in captured.err.splitlines()
    error_label = cli._RUN_ALL_STATUS_LABEL["error"]
    errors = [
        line.split()[1]
        for line in captured.err.splitlines()
        if line.lstrip().startswith(error_label)
    ]
    assert errors == ["schedule-fa"]
    assert captured.err.count("-> output removed because the run failed") == 6
    for marker in MARKERS:
        assert marker in _A3_DIVIDENDS, marker
        assert marker not in captured.out, marker
        assert marker not in captured.err, marker
