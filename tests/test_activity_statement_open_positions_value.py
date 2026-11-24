"""Activity Statement Open Positions ``Value`` check (#1315).

The Flex Query reader checks each Open Positions row's ``PositionValue``
against ``Quantity * MarkPrice``. The Activity Statement reader filed each
holding's closing value from its ``Summary`` row's ``Close Price`` alone and
never compared it with the row's printed ``Value``, so a row that
contradicted itself was filed. Each ``Summary`` row's ``Value`` must now be
within 0.01 of its ``Quantity`` times ``Close Price``, as Flex's is, with
the product and the difference keeping every digit. Otherwise every command
that reads Open Positions refuses (exit 4, no output).

Every statement here is synthetic: the module's fixtures, or rows built
here with made-up values.
"""

from __future__ import annotations

from collections.abc import Iterator
from decimal import Context, localcontext
from pathlib import Path
from typing import Final

import pytest

from ibkr_fa.cli import main
from ibkr_fa.exceptions import ParseError
from ibkr_fa.parser.activity_statement import validate_open_positions_values
from tests._activity_statement_rows import open_position_row as _make_op_row
from tests._crash_reports import assert_prints_no_marker, assert_unchained_refusal
from tests._ibkr_user_toml import ACCOUNT_AND_FX_USER_TOML
from tests._number_limits import MONEY
from tests._text_edits import replace_once

_FIXTURES: Final = Path(__file__).parent / "fixtures"
_CY_FIXTURE: Final = _FIXTURES / "e2e_activity_statement_2025.csv"
_FY_FIXTURE: Final = _FIXTURES / "e2e_activity_statement_schedule_cg_ltcg_fy2025_26.csv"
# The fixtures' first Open Positions Summary rows, from Symbol to Unrealized
# P/L: 10 shares at 95.40 are 954.00, and 5 at 210.00 are 1050.00.
_XQBB: Final = "XQBB,10,1,86.10,861.00,95.40,954.00,93.00,"
_SURV: Final = "SURV,5,1,200.00,1000.00,210.00,1050.00,50.00,"
_OP_HEADER: Final = (
    "Open Positions,Header,DataDiscriminator,Asset Category,Currency,Symbol,Quantity,"
    "Mult,Cost Price,Cost Basis,Close Price,Value,Unrealized P/L,Code,,,\n"
)
_USER_TOML: Final = ACCOUNT_AND_FX_USER_TOML
# A neutral digit run, written contiguously into each refused value.
_MARKER: Final = "7362509"
_MISMATCH: Final = "must equal Quantity * Close Price within 0.01"
_REFUSAL: Final = (
    f"error: ParseError in section 'Open Positions' at row 0, column 'Value': {_MISMATCH}\n"
)
# Long-value rows also run at a much narrower precision than the default.
_CONTEXTS: Final = pytest.mark.parametrize(
    "context", [None, Context(prec=3)], ids=["default-context", "prec-3"]
)


@pytest.fixture(autouse=True)
def _no_output(capsys: pytest.CaptureFixture[str]) -> Iterator[None]:
    """Parser calls print nothing, including refused input; CLI tests read
    their streams before this runs."""
    yield
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == ""


def _summary(quantity: str, close_price: str, value: str) -> dict[str, str]:
    """A ``Summary`` row with the given cells."""
    return {
        **_make_op_row(symbol="XQAW", quantity=quantity),
        "Close Price": close_price,
        "Value": value,
    }


def _refusal(column: str, message: str) -> ParseError:
    return ParseError(
        section="Open Positions", row_index=0, column=column, message=message, raw_value=""
    )


def _assert_mismatch(error: ParseError, *markers: str) -> None:
    assert_unchained_refusal(
        error,
        _refusal("Value", _MISMATCH),
        f"ParseError in section 'Open Positions' at row 0, column 'Value': {_MISMATCH}",
        markers=markers,
    )


# ---- the check on its own ----------------------------------------------------


@pytest.mark.parametrize(
    ("quantity", "close_price", "value"),
    [
        pytest.param("10", "105", "1050", id="exact"),
        pytest.param("3", "33.333", "100.00", id="rounded-to-the-cent"),
        pytest.param("-10", "105", "-1050", id="short"),
        pytest.param("0.5", "0.01", "0.00", id="under-a-cent"),
    ],
)
def test_a_summary_row_whose_value_matches_passes(
    quantity: str, close_price: str, value: str
) -> None:
    validate_open_positions_values([_summary(quantity, close_price, value)])


def test_a_lot_row_is_not_checked() -> None:
    """``Lot`` rows supply no price, so only the ``Summary`` row's Value is
    checked; the Lot row's 1 is far from its 10 x 105."""
    lot = {**_summary("10", "105", "1"), "DataDiscriminator": "Lot"}

    validate_open_positions_values([_summary("10", "105", "1050"), lot])


@pytest.mark.parametrize(
    ("quantity", "value"), [("-10", "1050"), ("10", "-1050")], ids=["short", "long"]
)
def test_a_value_of_the_wrong_sign_refuses(quantity: str, value: str) -> None:
    """10 shares at 105 are 1050, and a short position of -10 is -1050: a
    Value of the other sign is 2100 off."""
    with pytest.raises(ParseError) as raised:
        validate_open_positions_values([_summary(quantity, "105", value)])

    _assert_mismatch(raised.value)


def test_the_refusal_names_the_mismatched_row() -> None:
    rows = [_summary("10", "105", "1050"), _summary("10", "105", "1051")]

    with pytest.raises(ParseError) as raised:
        validate_open_positions_values(rows)

    assert raised.value == ParseError(
        section="Open Positions", row_index=1, column="Value", message=_MISMATCH, raw_value=""
    )


@pytest.mark.parametrize(
    ("quantity", "value", "accepted"),
    [
        pytest.param("25", "1127.51", True, id="long-above"),
        pytest.param("25", "1127.51000000000000000001", False, id="long-above-past"),
        pytest.param("25", "1127.49", True, id="long-below"),
        pytest.param("25", "1127.48999999999999999999", False, id="long-below-past"),
        pytest.param("-25", "-1127.49", True, id="short-above"),
        pytest.param("-25", "-1127.48999999999999999999", False, id="short-above-past"),
        pytest.param("-25", "-1127.51", True, id="short-below"),
        pytest.param("-25", "-1127.51000000000000000001", False, id="short-below-past"),
    ],
)
def test_value_refuses_one_unit_past_its_tolerance(
    quantity: str, value: str, accepted: bool
) -> None:
    """25 shares at 45.10 are 1127.50. A Value 0.01 off either way passes,
    and one off by one more unit in the 20th place refuses; so for a short
    position of -25 shares."""
    row = _summary(quantity, "45.10", value)
    if accepted:
        validate_open_positions_values([row])
        return
    with pytest.raises(ParseError) as raised:
        validate_open_positions_values([row])
    _assert_mismatch(raised.value)


# Products past Decimal's default 28 digits, from cells within their
# read-time limits (#1276). A Value has at most 15 whole digits, so a row
# whose Value is near its product can't pair a 32-digit Quantity with a
# 29-digit Close Price: these are two wide-digit pairs that fit.
_LONG_ROWS: Final = pytest.mark.parametrize(
    ("quantity", "close_price", "value", "accepted"),
    [
        pytest.param(
            "100000.00000000000000000001",
            "100000000.00000000000000000006",
            "10000000000000.01000000000100600001",
            False,
            id="26x29-digits-1e-20-past",
        ),
        pytest.param(
            "100000.00000000000000000001",
            "100000000.00000000000000000004",
            "10000000000000.010000000001004",
            True,
            id="26x29-digits-within",
        ),
        pytest.param(
            "100000000000.00000000000000006000",
            "1000.00000000000000000001",
            "100000000000000.01000000100006000001",
            False,
            id="32x24-digits-1e-20-past",
        ),
        pytest.param(
            "100000000000.00000000000000004000",
            "1000.00000000000000000001",
            "100000000000000.01000000100004",
            True,
            id="32x24-digits-within",
        ),
    ],
)


@_CONTEXTS
@_LONG_ROWS
def test_a_product_past_28_digits_gets_the_exact_verdict(
    context: Context | None, quantity: str, close_price: str, value: str, accepted: bool
) -> None:
    """Each row's verdict flips if the product is rounded to 28 digits: the
    refused rows are 0.01 plus about 1e-20 off, which rounding brings within
    0.01, and the accepted rows are just within 0.01, which rounding takes
    past it."""
    row = _summary(quantity, close_price, value)
    with localcontext(context):
        if accepted:
            validate_open_positions_values([row])
            return
        with pytest.raises(ParseError) as raised:
            validate_open_positions_values([row])
    _assert_mismatch(raised.value)


def test_the_mismatch_refusal_prints_no_cell() -> None:
    """Quantity, Close Price and Value each hold the marker."""
    row = _summary(_MARKER, f"0.{_MARKER}", f"{_MARKER}.00")

    with pytest.raises(ParseError) as raised:
        validate_open_positions_values([row])

    _assert_mismatch(raised.value, _MARKER)


@pytest.mark.parametrize(
    ("value", "message"),
    [
        pytest.param(
            None,
            "required column 'Value' missing from Open Positions row; can't reconcile.",
            id="missing-column",
        ),
        pytest.param("", "must be a decimal number", id="blank"),
        pytest.param(f"{_MARKER}x", "must be a decimal number", id="non-decimal"),
        pytest.param("NaN", "must be a finite decimal", id="nan"),
        pytest.param("-Infinity", "must be a finite decimal", id="infinity"),
        pytest.param(
            f"{_MARKER}123456789.00",
            MONEY,
            id="past-the-limit",
        ),
    ],
)
def test_a_summary_row_without_a_readable_value_refuses(value: str | None, message: str) -> None:
    row = _summary("10", "105", "1050")
    if value is None:
        del row["Value"]
    else:
        row["Value"] = value

    with pytest.raises(ParseError) as raised:
        validate_open_positions_values([row])

    assert raised.value == _refusal("Value", message)
    assert str(raised.value) == (
        f"ParseError in section 'Open Positions' at row 0, column 'Value': {message}"
    )
    if value is not None and _MARKER in value:
        assert_prints_no_marker(raised.value, markers=(_MARKER,))


@pytest.mark.parametrize(
    ("row", "message"),
    [
        pytest.param(
            {"Symbol": "XQAW"},
            "required column 'DataDiscriminator' missing from Open Positions row; can't reconcile.",
            id="no-discriminator",
        ),
        pytest.param(
            {"DataDiscriminator": "Total"},
            "must be 'Summary' or 'Lot', the only Open Positions row kinds this tool reads; "
            "another kind could hold a holding no check sees. Refusing rather than skipping "
            "it. If IBKR now emits this, please file an issue at "
            "https://github.com/jay-tau/ibkr-fa/issues naming it.",
            id="other-kind",
        ),
    ],
)
def test_a_row_of_no_known_kind_refuses(row: dict[str, str], message: str) -> None:
    """The pipeline's earlier checks refuse these first; called on its own,
    the check refuses them too rather than raising a bare ``KeyError``."""
    with pytest.raises(ParseError) as raised:
        validate_open_positions_values([row])

    assert raised.value == _refusal("DataDiscriminator", message)


def test_arithmetic_past_the_exponent_limit_refuses_unchained() -> None:
    """Under ``Emax=10``, 7362509 shares at 100000 are 736250900000, which
    overflows. The refusal is fixed text at the row's Value, with no value or
    chain."""
    row = _summary(_MARKER, "100000", f"{_MARKER}00000")
    validate_open_positions_values([row])

    with localcontext(Context(Emax=10)), pytest.raises(ParseError) as raised:
        validate_open_positions_values([row])

    message = "the Value arithmetic exceeds the supported precision"
    assert_unchained_refusal(
        raised.value,
        _refusal("Value", message),
        f"ParseError in section 'Open Positions' at row 0, column 'Value': {message}",
        markers=(_MARKER,),
    )


# ---- through the CLI ------------------------------------------------------------


def _edit(fixture: Path, old: str, new: str) -> str:
    return replace_once(fixture.read_text(encoding="utf-8"), old, new)


def _cy_statement(old: str = _XQBB, new: str = _XQBB) -> str:
    return _edit(_CY_FIXTURE, old, new)


def _write(tmp_path: Path, name: str, text: str) -> Path:
    path = tmp_path / name
    path.write_text(text, encoding="utf-8")
    return path


def _schedule_fa(tmp_path: Path, statement: str) -> tuple[int, Path]:
    """``schedule-fa`` for CY 2025 over ``statement``, with a stale output."""
    out = tmp_path / "review.csv"
    out.write_bytes(b"STALE")
    rc = main(
        [
            "schedule-fa",
            *("--input", str(_write(tmp_path, "statement.csv", statement))),
            *("--config", str(_write(tmp_path, "user.toml", _USER_TOML))),
            *("--output", str(out), "--calendar-year", "2025", "--no-fetch-prices"),
        ]
    )
    return rc, out


_NO_VALUE_COLUMN: Final = (
    _OP_HEADER.replace("Close Price,Value,", "Close Price,"),
    "XQBB,10,1,86.10,861.00,95.40,93.00,",
    "XQBE,4,1,251.35,1005.40,263.40,48.20,",
)


@pytest.mark.parametrize(
    ("edits", "stderr"),
    [
        pytest.param(
            [(_XQBB, _XQBB.replace(",95.40,", ",9.54,"))], _REFUSAL, id="example-1-close-price"
        ),
        pytest.param(
            [(_XQBB, _XQBB.replace(",954.00,", ",1954.00,"))], _REFUSAL, id="example-2-value"
        ),
        pytest.param(
            [(_XQBB, _XQBB.replace(",954.00,", ",,"))],
            "error: ParseError in section 'Open Positions' at row 0, column 'Value': "
            "must be a decimal number\n",
            id="blank-value",
        ),
        pytest.param(
            [
                (_OP_HEADER, _NO_VALUE_COLUMN[0]),
                (_XQBB, _NO_VALUE_COLUMN[1]),
                ("XQBE,4,1,251.35,1005.40,263.40,1053.60,48.20,", _NO_VALUE_COLUMN[2]),
            ],
            "error: ParseError in section 'Open Positions' at row 0, column 'Value': "
            "required column 'Value' missing from Open Positions row; can't reconcile.\n",
            id="no-value-column",
        ),
    ],
)
def test_schedule_fa_refuses_a_summary_row_whose_value_contradicts_it(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    edits: list[tuple[str, str]],
    stderr: str,
) -> None:
    """The issue's examples 1 and 2, and a Value that's blank or has no
    column. Before #1315, example 1 filed XQBB's close as 9.54 and example 2
    filed the unedited statement's output, both exiting 3."""
    text = _CY_FIXTURE.read_text(encoding="utf-8")
    for old, new in edits:
        text = replace_once(text, old, new)

    rc, out = _schedule_fa(tmp_path, text)

    assert rc == 4
    captured = capsys.readouterr()
    assert captured.err == stderr
    assert captured.out == ""
    assert not out.exists()


_FY: Final = ("--financial-year", "2025-26")
_FY_COMMANDS: Final = pytest.mark.parametrize(
    "command",
    [
        pytest.param(("schedule-cg", "--itr-form", "ITR-2", *_FY), id="schedule-cg"),
        pytest.param(("schedule-cg-ltcg", "--itr-form", "ITR-2", *_FY), id="schedule-cg-ltcg"),
        pytest.param(("schedule-fsi", *_FY), id="schedule-fsi"),
        pytest.param(("schedule-tr", "--itr-form", "ITR-2", *_FY), id="schedule-tr"),
        pytest.param(("form-67", *_FY), id="form-67"),
    ],
)


def _fy_command(tmp_path: Path, command: tuple[str, ...], statement: str) -> tuple[int, Path]:
    name, *options = command
    out = tmp_path / "out.csv"
    out.write_bytes(b"STALE")
    rc = main(
        [
            name,
            *("--input", str(_write(tmp_path, "statement.csv", statement))),
            *("--config", str(_write(tmp_path, "user.toml", _USER_TOML))),
            *("--output", str(out), *options),
        ]
    )
    return rc, out


@_FY_COMMANDS
def test_each_command_that_reads_open_positions_refuses(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], command: tuple[str, ...]
) -> None:
    """The issue's example 2 edit of the CG LTCG fixture: SURV's Value
    1050.00 becomes 9050.00. Before #1315 every command filed the unedited
    statement's output."""
    statement = _edit(_FY_FIXTURE, _SURV, _SURV.replace(",1050.00,", ",9050.00,"))

    rc, out = _fy_command(tmp_path, command, statement)

    assert rc == 4
    captured = capsys.readouterr()
    assert captured.err == _REFUSAL
    assert captured.out == ""
    assert not out.exists()


def test_schedule_os_does_not_read_open_positions(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Control: Schedule OS reads no Open Positions, so the same edit
    files the unedited statement's output."""
    command = ("schedule-os", *_FY)
    rc, out = _fy_command(tmp_path, command, _FY_FIXTURE.read_text(encoding="utf-8"))
    assert rc == 0
    unedited = out.read_bytes()

    rc, out = _fy_command(
        tmp_path, command, _edit(_FY_FIXTURE, _SURV, _SURV.replace(",1050.00,", ",9050.00,"))
    )

    assert rc == 0
    assert out.read_bytes() == unedited
    capsys.readouterr()


def test_run_all_refuses_an_fy_input_before_any_command(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A contradicting Summary row in the FY input refuses run-all up front,
    and it writes nothing."""
    out_dir = _run_all(
        tmp_path,
        fy=_edit(_FY_FIXTURE, _SURV, _SURV.replace(",1050.00,", ",9050.00,")),
        cy=_cy_statement(),
    )

    captured = capsys.readouterr()
    assert captured.err == "error: run-all: " + _REFUSAL.removeprefix("error: ")
    assert captured.out == ""
    assert list(out_dir.iterdir()) == []


def test_run_all_refuses_a_cy_input_and_removes_every_output(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """In the CY input, Schedule FA's own run refuses; run-all then removes
    the outputs the FY commands wrote."""
    out_dir = _run_all(
        tmp_path,
        fy=_FY_FIXTURE.read_text(encoding="utf-8"),
        cy=_cy_statement(new=_XQBB.replace(",954.00,", ",1954.00,")),
    )

    err = capsys.readouterr().err
    assert _REFUSAL in err.splitlines(keepends=True)
    assert (
        "                 -> failed (exit 4); no current output — ignore any existing "
        "schedule-fa.csv\n"
    ) in err.splitlines(keepends=True)
    assert list(out_dir.iterdir()) == []


def _run_all(tmp_path: Path, *, fy: str, cy: str) -> Path:
    """``run-all`` over the two statements; it must exit 4. Returns its output folder."""
    out_dir = tmp_path / "out"
    out_dir.mkdir(mode=0o700)

    rc = main(
        [
            "run-all",
            *("--fy-input", str(_write(tmp_path, "fy.csv", fy))),
            *("--cy-input", str(_write(tmp_path, "cy.csv", cy))),
            *("--config", str(_write(tmp_path, "user.toml", _USER_TOML))),
            *("--output-dir", str(out_dir), "--itr-form", "ITR-2", *_FY, "--no-fetch-prices"),
        ]
    )

    assert rc == 4
    return out_dir


def test_the_cli_prints_no_cell(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """XQBB's Close Price and Value each hold the marker."""
    rc, out = _schedule_fa(
        tmp_path,
        _cy_statement(new=_XQBB.replace(",95.40,954.00,", f",95.{_MARKER},{_MARKER}.00,")),
    )

    assert rc == 4
    captured = capsys.readouterr()
    assert captured.err == _REFUSAL
    assert _MARKER not in captured.out
    assert not out.exists()


# ---- earlier refusals keep their order ----------------------------------------------


_XQBE_LINE: Final = (
    "Open Positions,Data,Summary,Stocks,USD,XQBE,4,1,251.35,1005.40,263.40,1053.60,48.20,,,,\n"
)


@pytest.mark.parametrize(
    ("old", "new", "error"),
    [
        pytest.param(
            ",Summary,Stocks,USD,XQBB,",
            ",Summary,Bonds,USD,XQBB,",
            "ParseError in section 'Open Positions' at row 0, column 'Asset Category': "
            "must be 'Stocks': v1 reads only stock rows, and would read any other instrument "
            "as a stock.",
            id="not-a-stock",
        ),
        pytest.param(
            "XQBB,10,1,",
            "XQBB,10,2,",
            "ParseError in section 'Open Positions' at row 0, column 'Mult': must be '1': "
            "a multiplier scales every quantity and value, and v1 reads one share as one unit.",
            id="multiplier",
        ),
        pytest.param(
            "93.00,,,,",
            "93.00,ZZ,,,",
            "ParseError in section 'Open Positions' at row 0, column 'Code': must be '': "
            "v1 reads no codes in this section, so a code's meaning would be lost.",
            id="code",
        ),
        pytest.param(
            ",Summary,Stocks,USD,XQBB,",
            ",Other,Stocks,USD,XQBB,",
            "ParseError in section 'Open Positions' at row 0, column 'DataDiscriminator': "
            "must be 'Summary' or 'Lot', the only Open Positions row kinds this tool reads; "
            "another kind could hold a holding no check sees. Refusing rather than skipping "
            "it. If IBKR now emits this, please file an issue at "
            "https://github.com/jay-tau/ibkr-fa/issues naming it.",
            id="row-kind",
        ),
        pytest.param(
            ",Summary,Stocks,USD,XQBB,",
            ",Summary,Stocks,EUR,XQBB,",
            "ParseError in section 'Open Positions' at row 0, column 'Currency': "
            "the currency isn't USD; v1 supports USD only. Multi-currency support is tracked "
            "at https://github.com/jay-tau/ibkr-fa/issues/52.",
            id="currency",
        ),
        pytest.param(
            _XQBE_LINE,
            _XQBE_LINE + _XQBE_LINE.replace(",1053.60,", ",1053.600,"),
            "ParseError in section 'Open Positions' at row 2, column 'Symbol': "
            "the Symbol appears more than once in Open Positions; refusing to silently "
            "overwrite the earlier close price.",
            id="duplicate-symbol",
        ),
        pytest.param(
            "861.00,95.40,",
            "861.00,abc,",
            "ParseError in section 'Open Positions' at row 0, column 'Close Price': "
            "must be a decimal number",
            id="close-price",
        ),
        pytest.param(
            _XQBE_LINE,
            "",
            "LotMatchingError in section 'Trades' at row 2: the opening trade leaves a "
            "surviving open lot after FIFO matching, but its symbol does not appear in the "
            "Open Positions section, so the Dec 31 close price can't be determined. See "
            "https://github.com/jay-tau/ibkr-fa/issues/133.",
            id="no-close-price",
        ),
        pytest.param(
            "XQBB,10,1,",
            "XQBB,11,1,",
            "ParseError in section 'Open Positions' at row 0, column 'Quantity': "
            "reconciliation failed: the total quantity reconstructed from Trades for this "
            "Summary row's Symbol differs from its Quantity by more than 0.0001. If part of "
            "the holding was bought before the statement period, re-export the Activity "
            "Statement starting from your account's first trade. Otherwise, refusing output "
            "to avoid corrupting Schedule FA — please file an issue with a redacted statement.",
            id="reconciliation",
        ),
        pytest.param(
            "93.00,,,,\n",
            "93.00,,,,\nOpen Positions,Data,Lot,Stocks,USD,XQBB,3,1,86.10,258.30,95.40,286.20,"
            "27.90,,,,\n",
            "ParseError in section 'Open Positions' at row 0, column 'Quantity': "
            "the Lot rows for this Summary row's Symbol don't add up exactly to its "
            "Quantity; can't reconcile.",
            id="lot-rows",
        ),
    ],
)
def test_an_earlier_open_positions_refusal_still_comes_first(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], old: str, new: str, error: str
) -> None:
    """With XQBB's Value also contradicting it, a statement that an earlier
    Open Positions check refuses (or lot matching, for a held Symbol with no
    Summary row) refuses with that check's error, unchanged: the Value check
    sees only rows every earlier check passed."""
    defect = _edit(_CY_FIXTURE, old, new)
    rc, _out = _schedule_fa(tmp_path, defect)
    assert rc == 4
    earlier = capsys.readouterr()
    assert earlier.err == f"error: {error}\n"

    rc, out = _schedule_fa(tmp_path, replace_once(defect, ",954.00,", ",1954.00,"))

    assert rc == 4
    assert capsys.readouterr() == earlier
    assert not out.exists()
