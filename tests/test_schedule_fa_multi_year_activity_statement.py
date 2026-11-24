"""Schedule FA from an Activity Statement that starts before the calendar year (#1288).

Schedule FA rebuilds each holding from its opening trades, so a holding bought
in an earlier year needs a statement that starts before Jan 1. The period must
still end on Dec 31 (Open Positions is its period-end snapshot), and the run
files only the calendar year's lots, dividends, withholding and interest rows.
"""

from __future__ import annotations

import csv
from decimal import Decimal
from pathlib import Path

import pytest

from ibkr_fa.cli import main
from ibkr_fa.exceptions import LotMatchingError, ParseError
from ibkr_fa.pipeline_fa import run_fa_a3
from tests._change_in_nav_checks import assert_no_amount as _assert_no_amount
from tests._change_in_nav_checks import change_in_nav_differs as _differs
from tests._crash_reports import assert_prints_no_marker
from tests._flex_records import with_flex_records
from tests._flex_section_rows import OPEN_POSITIONS_HEADER, TRADES_LOT_DETAIL_HEADER
from tests._ibkr_user_toml import CUSTODIAN_TOML, US_PERSON_USER_TOML
from tests._income_blocks import with_totals

_FIXTURES = Path(__file__).parent / "fixtures"
# Period is exactly FY 2025-26; the run-all golden test's FY input.
_FY_2025_26_INPUT = _FIXTURES / "e2e_activity_statement_schedule_tr_with_wht_fy2025_26.csv"

_CUSTODIAN_TOML = US_PERSON_USER_TOML + CUSTODIAN_TOML

_TWO_YEARS = "January 1, 2024 - December 31, 2025"
_ONE_YEAR = "January 1, 2025 - December 31, 2025"

_FII_HEADER = (
    "Financial Instrument Information,Header,Asset Category,Symbol,Description,Conid,"
    "Security ID,Listing Exch,Multiplier,Type,Code"
)
_FII_ROWS = {
    "XQBB": "Financial Instrument Information,Data,Stocks,XQBB,SAMPLE ISSUER BB INC,11111,"
    "US99995B1044,NASDAQ,1,COMMON,",
    "XQBD": "Financial Instrument Information,Data,Stocks,XQBD,SAMPLE ISSUER BD INC,33333,"
    "US99995A1060,NASDAQ,1,COMMON,",
    "XQBE": "Financial Instrument Information,Data,Stocks,XQBE,SAMPLE ISSUER BE CORP,22222,"
    "US99995D1000,NYSE,1,COMMON,",
}
_TRADES_HEADER = (
    "Trades,Header,DataDiscriminator,Asset Category,Currency,Symbol,Date/Time,Quantity,"
    "T. Price,C. Price,Proceeds,Comm/Fee,Basis,Realized P/L,MTM P/L,Code"
)
_OPEN_POSITIONS_HEADER = (
    "Open Positions,Header,DataDiscriminator,Asset Category,Currency,Symbol,Quantity,Mult,"
    "Cost Price,Cost Basis,Close Price,Value,Unrealized P/L,Code"
)
_NAV = (
    "Net Asset Value,Header,Asset Class,Prior Total,Current Long,Current Short,Current Total,"
    "Change\n"
    "Net Asset Value,Data,Cash,0,100.00,0,100.00,100.00\n"
    "Net Asset Value,Data,Stock,0,2007.60,0,2007.60,2007.60\n"
    "Net Asset Value,Data,Total,0,2107.60,0,2107.60,2107.60\n"
    "Change in NAV,Header,Field Name,Field Value\n"
)
_ENDING_VALUE = "Change in NAV,Data,Ending Value,2107.60\n"


def _change_in_nav_income(income: str) -> str:
    """The ``Change in NAV`` line IBKR prints for each section in ``income``:
    its rows' total over the whole statement period, which ``schedule-fa``
    checks the section against (#1324)."""
    totals: dict[str, Decimal] = {}
    for section, kind, _currency, _date, _description, amount, *_code in csv.reader(
        income.splitlines()
    ):
        if kind == "Data":
            totals[section] = totals.get(section, Decimal(0)) + Decimal(amount)
    return "".join(f"Change in NAV,Data,{section},{total}\n" for section, total in totals.items())


def _statement(
    period: str,
    *,
    symbols: tuple[str, ...],
    trades: tuple[str, ...],
    open_positions: tuple[str, ...],
    income: str = "",
    nav: bool = False,
    nav_income: str | None = None,
) -> str:
    """An Activity Statement with IBKR's totals rows added (:func:`with_totals`).

    With ``nav``, its ``Change in NAV`` prints the totals of ``nav_income``
    (``income`` unless given).
    """
    lines = [
        "Statement,Header,Field Name,Field Value",
        "Statement,Data,BrokerName,Interactive Brokers LLC",
        "Statement,Data,Title,Activity Statement",
        f'Statement,Data,Period,"{period}"',
        'Statement,Data,WhenGenerated,"2026-01-15, 09:00:00 EDT"',
        "Account Information,Header,Field Name,Field Value",
        "Account Information,Data,Name,Sample Holder",
        "Account Information,Data,Account,U12345678",
        "Account Information,Data,Account Type,Individual",
        "Account Information,Data,Customer Type,Individual",
        "Account Information,Data,Account Capabilities,Cash",
        "Account Information,Data,Base Currency,USD",
        _FII_HEADER,
        *(_FII_ROWS[symbol] for symbol in symbols),
        _TRADES_HEADER,
        *trades,
        _OPEN_POSITIONS_HEADER,
        *open_positions,
    ]
    if nav:
        change_in_nav = _change_in_nav_income(income if nav_income is None else nav_income)
        income += _NAV + change_in_nav + _ENDING_VALUE
    return with_totals("\n".join(lines) + "\n" + income)


def _trade(symbol: str, when: str, quantity: str, price: str, proceeds: str, basis: str) -> str:
    code = "O" if not quantity.startswith("-") else "C"
    realized = "0" if code == "O" else f"{float(proceeds) + float(basis):.2f}"
    return (
        f'Trades,Data,Order,Stocks,USD,{symbol},"{when}",{quantity},{price},{price},'
        f"{proceeds},0,{basis},{realized},0,{code}"
    )


def _summary(symbol: str, quantity: str, cost: str, basis: str, close: str, value: str) -> str:
    return (
        f"Open Positions,Data,Summary,Stocks,USD,{symbol},{quantity},1,{cost},{basis},"
        f"{close},{value},0,"
    )


# One holding, 10 shares bought 2024-06-17 and held on Dec 31, 2025 (the issue's repro).
_CARRIED_IN_TRADE = _trade("XQBB", "2024-06-17, 10:30:00", "10", "85.20", "-852.00", "852.00")
_CARRIED_IN_SUMMARY = _summary("XQBB", "10", "85.20", "852.00", "95.40", "954.00")

# The same lot as a Flex Open Positions LOT row, with a header-only Trades section
# (written between header and trailer records, #1313).
_FLEX_SAME_LOT = (
    """\
"ClientAccountID","CurrencyPrimary","Name","AccountType","IBEntity"
"U12345678","USD","Sample Holder","Individual","IBLLC-US"

"""
    + OPEN_POSITIONS_HEADER
    + """
"USD","1","STK","COMMON","XQBB","SAMPLE ISSUER BB INC","99995B104","US99995B1044","NASDAQ","US",\
"20251231","10","95.4","954","85.2","852","Long","LOT","20240617","20240617"

"""
    + TRADES_LOT_DETAIL_HEADER
    + "\n"
)

_COMPARED_COLUMNS = (
    "Date of acquiring the interest",
    "Initial value of the investment",
    "Closing balance",
)


def _carried_in_statement(period: str = _TWO_YEARS) -> str:
    return _statement(
        period,
        symbols=("XQBB",),
        trades=(_CARRIED_IN_TRADE,),
        open_positions=(_CARRIED_IN_SUMMARY,),
    )


def _write(path: Path, text: str) -> Path:
    path.write_text(text, encoding="utf-8")
    return path


def _rows(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8", newline="") as handle:
        return list(csv.DictReader(handle))


def _compared(rows: list[dict[str, str]]) -> list[tuple[str, ...]]:
    return [tuple(row[column] for column in _COMPARED_COLUMNS) for row in rows]


def _schedule_fa_argv(input_path: Path, config: Path, output: Path, fmt: str) -> list[str]:
    return [
        "schedule-fa",
        "--input",
        str(input_path),
        "--config",
        str(config),
        "--output",
        str(output),
        "--calendar-year",
        "2025",
        "--format",
        fmt,
        "--no-fetch-prices",
    ]


@pytest.fixture
def flex_rows(tmp_path: Path) -> dict[str, list[tuple[str, ...]]]:
    """The Flex run's compared A3 cells for the same lot, per CSV format."""
    flex_dir = tmp_path / "flex"
    flex_dir.mkdir()
    flex = _write(flex_dir / "flex.csv", with_flex_records(_FLEX_SAME_LOT))
    config = _write(flex_dir / "user.toml", US_PERSON_USER_TOML)
    rows = {}
    for fmt in ("filing-csv", "review-csv"):
        output = flex_dir / f"{fmt}.csv"
        assert main(_schedule_fa_argv(flex, config, output, fmt)) == 3
        rows[fmt] = _compared(_rows(output))
    assert rows["filing-csv"] == [("2024-06-17", "70844", "85354")]
    return rows


# ---- the carried-in holding files as the Flex lot does ---------------------


def test_run_fa_a3_rebuilds_a_holding_bought_before_the_year(
    tmp_path: Path, flex_rows: dict[str, list[tuple[str, ...]]]
) -> None:
    """#1288 repro: the holding's opening trade is in 2024, so only a statement
    starting before Jan 1, 2025 holds it. Main refused that period."""
    statement = _write(tmp_path / "statement.csv", _carried_in_statement())
    config = _write(tmp_path / "user.toml", US_PERSON_USER_TOML)
    output = tmp_path / "a3.csv"

    report = run_fa_a3(
        input_path=statement,
        config_path=config,
        output_path=output,
        calendar_year=2025,
        format_id="filing-csv",
    )

    assert report.rows_written == 1
    assert _compared(_rows(output)) == flex_rows["filing-csv"]


@pytest.mark.parametrize("fmt", ["filing-csv", "review-csv"])
def test_schedule_fa_cli_rebuilds_a_holding_bought_before_the_year(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    flex_rows: dict[str, list[tuple[str, ...]]],
    fmt: str,
) -> None:
    statement = _write(tmp_path / "statement.csv", _carried_in_statement())
    config = _write(tmp_path / "user.toml", US_PERSON_USER_TOML)
    output = tmp_path / "a3.csv"

    # Exit 3: the peak is left for manual fill without fetched prices.
    assert main(_schedule_fa_argv(statement, config, output, fmt)) == 3

    assert "error" not in capsys.readouterr().err
    assert _compared(_rows(output)) == flex_rows[fmt]


def test_run_all_fa_leg_rebuilds_a_holding_bought_before_the_year(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    flex_rows: dict[str, list[tuple[str, ...]]],
) -> None:
    """``--cy-input`` takes the same statement; main's FA leg refused it, so
    the run exited 4 and removed every output."""
    statement = _write(tmp_path / "statement.csv", _carried_in_statement())
    config = _write(tmp_path / "user.toml", US_PERSON_USER_TOML)
    out = tmp_path / "out"

    code = main(
        [
            "run-all",
            "--fy-input",
            str(_FY_2025_26_INPUT),
            "--cy-input",
            str(statement),
            "--financial-year",
            "2025-26",
            "--format",
            "review-csv",
            "--itr-form",
            "ITR-2",
            "--config",
            str(config),
            "--output-dir",
            str(out),
            "--no-fetch-prices",
        ]
    )

    assert code == 3
    assert "error" not in capsys.readouterr().err
    assert _compared(_rows(out / "schedule-fa.csv")) == flex_rows["review-csv"]


# ---- a prior year changes no filed byte ------------------------------------

# Held on Dec 31, 2025, both bought in 2025.
_CY_TRADES = (
    _trade("XQBB", "2025-01-15, 10:30:00", "4", "80.00", "-320.00", "320.00"),
    _trade("XQBB", "2025-03-20, 14:15:00", "6", "90.00", "-540.00", "540.00"),
    _trade("XQBE", "2025-01-15, 11:00:00", "4", "251.25", "-1005.00", "1005.00"),
)
_CY_SUMMARIES = (
    _summary("XQBB", "10", "86.00", "860.00", "95.40", "954.00"),
    _summary("XQBE", "4", "251.25", "1005.00", "263.40", "1053.60"),
)
# Bought and sold in 2024.
_PRIOR_YEAR_TRADES = (
    _trade("XQBD", "2024-02-01, 10:30:00", "5", "50.00", "-250.00", "250.00"),
    _trade("XQBD", "2024-08-01, 10:30:00", "-5", "60.00", "300.00", "-250.00"),
)
_DIVIDENDS_HEADER = "Dividends,Header,Currency,Date,Description,Amount"
_WITHHOLDING_HEADER = "Withholding Tax,Header,Currency,Date,Description,Amount,Code"
_INTEREST_HEADER = "Interest,Header,Currency,Date,Description,Amount"
_CY_DIVIDENDS = (
    # Paid after the holding was sold in 2024: an orphan dividend that the run
    # reports as never held, as a statement for 2025 alone shows it.
    "Dividends,Data,USD,10-01-2025,XQBD(US99995A1060) Cash Dividend USD 0.10 per Share "
    "(Ordinary Dividend),0.50",
    "Dividends,Data,USD,16-03-2025,XQBB(US99995B1044) Cash Dividend USD 0.21 per Share "
    "(Ordinary Dividend),2.10",
    "Dividends,Data,USD,15-09-2025,XQBB(US99995B1044) Cash Dividend USD 0.21 per Share "
    "(Ordinary Dividend),2.10",
)
_CY_WITHHOLDING = (
    "Withholding Tax,Data,USD,16-03-2025,XQBB(US99995B1044) Cash Dividend USD 0.21 per Share "
    "- US Tax,-0.32,",
    "Withholding Tax,Data,USD,15-09-2025,XQBB(US99995B1044) Cash Dividend USD 0.21 per Share "
    "- US Tax,-0.32,",
)
_CY_INTEREST = (
    "Interest,Data,USD,30-04-2025,USD Credit Interest for Apr-2025,1.23",
    "Interest,Data,USD,31-05-2025,USD Credit Interest for May-2025,1.45",
)
_PRIOR_YEAR_DIVIDENDS = (
    "Dividends,Data,USD,15-05-2024,XQBD(US99995A1060) Cash Dividend USD 0.20 per Share "
    "(Ordinary Dividend),1.00",
)
_PRIOR_YEAR_WITHHOLDING = (
    "Withholding Tax,Data,USD,15-05-2024,XQBD(US99995A1060) Cash Dividend USD 0.20 per Share "
    "- US Tax,-0.15,",
)
# A credit, and an accrual row: unproven, it would leave A2's interest blank if
# it were dated in the year.
_PRIOR_YEAR_INTEREST = (
    "Interest,Data,USD,30-11-2024,USD Credit Interest for Nov-2024,0.88",
    "Interest,Data,USD,31-12-2024,USD Interest Accrual Adjustment,0.05",
)


def _income(
    dividends: tuple[str, ...], withholding: tuple[str, ...], interest: tuple[str, ...]
) -> str:
    blocks = (
        (_DIVIDENDS_HEADER, dividends),
        (_WITHHOLDING_HEADER, withholding),
        (_INTEREST_HEADER, interest),
    )
    return "".join(
        f"{header}\n" + "".join(f"{row}\n" for row in rows) for header, rows in blocks if rows
    )


def _one_year_statement() -> str:
    return _statement(
        _ONE_YEAR,
        symbols=("XQBB", "XQBD", "XQBE"),
        trades=_CY_TRADES,
        open_positions=_CY_SUMMARIES,
        income=_income(_CY_DIVIDENDS, _CY_WITHHOLDING, _CY_INTEREST),
        nav=True,
    )


def _two_year_statement(*, nav_income: str | None = None) -> str:
    return _statement(
        _TWO_YEARS,
        symbols=("XQBB", "XQBD", "XQBE"),
        trades=(*_CY_TRADES[:2], *_PRIOR_YEAR_TRADES, _CY_TRADES[2]),
        open_positions=_CY_SUMMARIES,
        income=_income(
            (*_PRIOR_YEAR_DIVIDENDS, *_CY_DIVIDENDS),
            (*_PRIOR_YEAR_WITHHOLDING, *_CY_WITHHOLDING),
            (*_PRIOR_YEAR_INTEREST, *_CY_INTEREST),
        ),
        nav=True,
        nav_income=nav_income,
    )


def _run_once(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], text: str, fmt: str
) -> tuple[int, bytes, bytes, str, str]:
    """One CLI run on ``text``, always at the same input and output paths, so
    two runs can be compared byte for byte, streams included."""
    statement = _write(tmp_path / "statement.csv", text)
    config = _write(tmp_path / "user.toml", _CUSTODIAN_TOML)
    output = tmp_path / "a3.csv"
    code = main(_schedule_fa_argv(statement, config, output, fmt))
    captured = capsys.readouterr()
    a3 = output.read_bytes()
    a2 = (tmp_path / "a3-a2.csv").read_bytes()
    return code, a3, a2, captured.out, captured.err


_A2_GROSS_COLUMNS = {
    "filing-csv": ("Nature of Amount", "Amount"),
    "review-csv": (
        "Gross amount paid/credited to the account during the period - Nature of amount",
        "Gross amount paid/credited to the account during the period - Amount",
    ),
}
_A2_GROSS = {
    "filing-csv": [("I", "240"), ("D", "421")],
    "review-csv": [("Interest", "240"), ("Dividend", "421")],
}


@pytest.mark.parametrize("fmt", ["filing-csv", "review-csv"])
def test_a_prior_year_in_the_statement_files_the_same_bytes(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], fmt: str
) -> None:
    """For holdings bought in the year, adding 2024 to the statement (a
    dividend and its withholding, interest credit and accrual rows, and a lot
    bought and sold) changes no A2 or A3 byte and no stdout or stderr line:
    every count and advisory, the orphan dividend's included, covers only the
    year."""
    one_year = _run_once(tmp_path, capsys, _one_year_statement(), fmt)
    two_years = _run_once(tmp_path, capsys, _two_year_statement(), fmt)

    assert one_year[0] == 3
    assert two_years == one_year
    assert "XQBD" in one_year[3]
    assert "previously owned" not in one_year[3]
    # A2 files the year's interest (2.68 USD) and dividends (4.70 USD) at the
    # Dec 31 TTBR: the 2024 credit is left out, and the 2024 accrual row
    # doesn't leave the interest for manual fill.
    nature, amount = _A2_GROSS_COLUMNS[fmt]
    a2 = [(row[nature], row[amount]) for row in _rows(tmp_path / "a3-a2.csv")]
    assert a2 == _A2_GROSS[fmt]


def test_prior_year_dividends_alone_leave_the_dividend_cells_for_manual_fill(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The statement has a Dividends section but no dividend paid in 2025. A
    statement for 2025 alone would have no Dividends section and file 0; the
    section's presence can't be limited to the year, so both tables leave the
    dividend for manual fill instead."""
    text = _statement(
        _TWO_YEARS,
        symbols=("XQBB", "XQBD", "XQBE"),
        trades=(*_CY_TRADES[:2], *_PRIOR_YEAR_TRADES, _CY_TRADES[2]),
        open_positions=_CY_SUMMARIES,
        income=_income(_PRIOR_YEAR_DIVIDENDS, _PRIOR_YEAR_WITHHOLDING, ()),
        nav=True,
    )
    code, _a3, _a2, out, _err = _run_once(tmp_path, capsys, text, "filing-csv")

    assert code == 3
    dividend_column = (
        "Total gross amount paid/credited with respect to the holding during the period"
    )
    assert [row[dividend_column] for row in _rows(tmp_path / "a3.csv")] == ["", "", ""]
    a2 = [(row["Nature of Amount"], row["Amount"]) for row in _rows(tmp_path / "a3-a2.csv")]
    assert a2 == [("", "")]
    assert "No dividend activity found" not in out


def test_change_in_nav_is_checked_over_the_whole_statement_period(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """#1324: ``Change in NAV`` covers the statement's whole period, as its
    income sections do, so the check runs before the year's rows are picked:
    a two-year statement whose lines total only 2025's rows refuses."""
    text = _two_year_statement(nav_income=_income(_CY_DIVIDENDS, _CY_WITHHOLDING, _CY_INTEREST))
    statement = _write(tmp_path / "statement.csv", text)
    config = _write(tmp_path / "user.toml", _CUSTODIAN_TOML)
    output = tmp_path / "a3.csv"

    assert main(_schedule_fa_argv(statement, config, output, "filing-csv")) == 4

    message = _differs("Dividends", "the 'Dividends' section")
    captured = capsys.readouterr()
    assert captured == ("", f"error: {message}\n")
    amounts = [row[5] for row in csv.reader(text.splitlines()) if row[:2] == ["Dividends", "Data"]]
    _assert_no_amount(captured.err, "2107.60", *amounts)
    assert list(tmp_path.glob("a3*")) == []


# ---- refusals kept ------------------------------------------------------------

_PERIOD_REFUSAL = (
    "ParseError in section 'Statement' at row 0, column 'Period': the Activity Statement "
    "period must end on Dec 31, 2025 and start on or before Jan 1, 2025. Schedule FA Table "
    "A3 is calendar-year based and rebuilds each holding from its opening trades; re-export "
    "the Activity Statement from Jan 1, 2025, or from your account's first trade if that is "
    "earlier, through Dec 31, 2025."
)
_CLOSE_WITHOUT_OPEN_REFUSAL = (
    "LotMatchingError in section 'Trades' at row 0: the closing trade closes more shares "
    "than the earlier opening trades of its symbol hold, so part of it can't be "
    "FIFO-matched. The FII section may be truncated, or the opening trades pre-date this "
    "Activity Statement's period — check Open Positions reconciliation upstream."
)
_NOT_REBUILT_REFUSAL = (
    "ParseError in section 'Open Positions' at row 0, column 'Symbol': this Summary row's "
    "Symbol has no reconstructed open lots from Trades. Possible causes: the holding was "
    "bought before the statement period (re-export the Activity Statement starting from "
    "your account's first trade), trades section is truncated, or the position came from a "
    "transfer-in (see https://github.com/jay-tau/ibkr-fa/issues/107) rather than a market "
    "trade. Refusing rather than producing partial FA-A3 output."
)
_PART_REBUILT_REFUSAL = (
    "ParseError in section 'Open Positions' at row 0, column 'Quantity': reconciliation "
    "failed: the total quantity reconstructed from Trades for this Summary row's Symbol "
    "differs from its Quantity by more than 0.0001. If part of the holding was bought before "
    "the statement period, re-export the Activity Statement starting from your account's "
    "first trade. Otherwise, refusing output to avoid corrupting Schedule FA — please file "
    "an issue with a redacted statement."
)
_XQBE_BUY = _trade("XQBE", "2025-01-15, 11:00:00", "4", "251.25", "-1005.00", "1005.00")
_XQBE_SUMMARY = _summary("XQBE", "4", "251.25", "1005.00", "263.40", "1053.60")
# The statement's own symbol, issuer and amounts: no refusal prints them.
_HOLDING_MARKERS = ("XQBB", "SAMPLE ISSUER BB", "85.20", "852.00", "95.40", "954.00")

_REFUSALS = [
    pytest.param(
        _carried_in_statement("January 1, 2024 - November 28, 2025"),
        ParseError,
        _PERIOD_REFUSAL,
        ("January 1, 2024", "November 28", "2024-01-01", "2025-11-28", *_HOLDING_MARKERS),
        id="period-ends-before-dec-31",
    ),
    pytest.param(
        _carried_in_statement("February 3, 2025 - December 31, 2025"),
        ParseError,
        _PERIOD_REFUSAL,
        ("February 3", "2025-02-03", *_HOLDING_MARKERS),
        id="period-starts-after-jan-1",
    ),
    pytest.param(
        _carried_in_statement("April 1, 2025 - March 31, 2026"),
        ParseError,
        _PERIOD_REFUSAL,
        ("April 1", "March 31", "2025-04-01", "2026-03-31", *_HOLDING_MARKERS),
        id="financial-year-period",
    ),
    pytest.param(
        # 6 shares bought before 2024 and sold in 2025.
        _statement(
            _TWO_YEARS,
            symbols=("XQBB", "XQBE"),
            trades=(
                _trade("XQBB", "2025-03-10, 10:30:00", "-6", "47.13", "282.78", "-270.00"),
                _XQBE_BUY,
            ),
            open_positions=(_XQBE_SUMMARY,),
        ),
        LotMatchingError,
        _CLOSE_WITHOUT_OPEN_REFUSAL,
        ("XQBB", "SAMPLE ISSUER BB", "47.13", "282.78", "270.00", "2025-03-10"),
        id="close-whose-open-predates-the-statement",
    ),
    pytest.param(
        # Held on Dec 31 with no opening trade in the statement.
        _statement(
            _TWO_YEARS,
            symbols=("XQBB", "XQBE"),
            trades=(_XQBE_BUY,),
            open_positions=(_CARRIED_IN_SUMMARY, _XQBE_SUMMARY),
        ),
        ParseError,
        _NOT_REBUILT_REFUSAL,
        _HOLDING_MARKERS,
        id="holding-without-opening-trades",
    ),
    pytest.param(
        # 15 shares held, the trades open 10.
        _statement(
            _TWO_YEARS,
            symbols=("XQBB",),
            trades=(_CARRIED_IN_TRADE,),
            open_positions=(_summary("XQBB", "15", "85.20", "1278.00", "95.40", "1431.00"),),
        ),
        ParseError,
        _PART_REBUILT_REFUSAL,
        ("XQBB", "SAMPLE ISSUER BB", "1278.00", "1431.00", "2024-06-17"),
        id="holding-partly-rebuilt",
    ),
]


@pytest.mark.parametrize(("text", "error_type", "expected", "markers"), _REFUSALS)
def test_run_fa_a3_still_refuses_a_statement_without_the_evidence(
    tmp_path: Path,
    text: str,
    error_type: type[Exception],
    expected: str,
    markers: tuple[str, ...],
) -> None:
    """Accepting an earlier start keeps every other refusal: a period that
    doesn't end on Dec 31 or starts after Jan 1, and missing trade history.
    The fixed text prints no statement date, symbol or amount, and chains no
    exception."""
    statement = _write(tmp_path / "statement.csv", text)
    config = _write(tmp_path / "user.toml", US_PERSON_USER_TOML)

    with pytest.raises(error_type) as exc:
        run_fa_a3(
            input_path=statement,
            config_path=config,
            output_path=tmp_path / "a3.csv",
            calendar_year=2025,
        )

    assert str(exc.value) == expected
    if isinstance(exc.value, ParseError):
        assert exc.value.raw_value == ""
    assert exc.value.__cause__ is None
    assert exc.value.__context__ is None
    assert_prints_no_marker(exc.value, markers=markers)
    assert list(tmp_path.glob("a3*")) == []


@pytest.mark.parametrize(
    ("text", "expected", "markers"),
    [
        pytest.param(text, expected, markers, id=case.id)
        for case in _REFUSALS
        for text, _error_type, expected, markers in [case.values]
    ],
)
def test_schedule_fa_cli_still_refuses_a_statement_without_the_evidence(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    text: str,
    expected: str,
    markers: tuple[str, ...],
) -> None:
    statement = _write(tmp_path / "statement.csv", text)
    config = _write(tmp_path / "user.toml", US_PERSON_USER_TOML)

    code = main(_schedule_fa_argv(statement, config, tmp_path / "a3.csv", "filing-csv"))

    captured = capsys.readouterr()
    assert code == 4
    assert captured.err == f"error: {expected}\n"
    for marker in markers:
        assert marker not in captured.out
    assert list(tmp_path.glob("a3*")) == []
