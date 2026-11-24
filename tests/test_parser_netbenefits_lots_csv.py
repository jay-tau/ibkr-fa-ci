"""Tests for the Fidelity NetBenefits open/closed-lots CSV parser.

The value-refusal helper pins the exact location and fixed message and an empty
``raw_value``. Distinctive input markers are checked absent from the rendered
refusal (#1041). A column-shifted row can put the holder's name or account number
in any cell, so the markers below go into the cells that reach each refusal.
"""

from __future__ import annotations

import re
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from ibkr_fa.exceptions import ParseError
from ibkr_fa.parser.format_detect import detect_ibkr_csv_format
from ibkr_fa.parser.netbenefits.lots_csv import (
    EspClosedLot,
    EspLot,
    FidelityLotsKind,
    MarkupNearMiss,
    NearMissLotsHeader,
    detect_fidelity_lots_csv,
    detect_fidelity_lots_kind,
    parse_closed_lots,
    parse_open_lots,
    read_closed_lots,
    read_open_lots,
)
from tests._malformed_csv import (
    AFTER_QUOTE,
    HOLDER,
    MARKER,
    UNCLOSED,
    assert_malformed,
    oversized_cell,
)
from tests._netbenefits import CLOSED_HEADER as _CLOSED_HEADER
from tests._netbenefits import CLOSED_WITH_ROW_CSV, OPEN_CSV
from tests._netbenefits import assert_refusal as _assert_refusal
from tests._text_edits import replace_once as _replace_once

# Closed export, header only (no disposals) — with the export's HTML <span> wart
# whose inline CSS commas would split the header cell without tag-stripping.
CLOSED_EMPTY_CSV = (
    '\ufeffDate acquired,Quantity,<span style="color: rgb(0, 0, 51); '
    'background-color: rgb(255, 255, 255);">Date sold or transferred</span>,'
    "Proceeds,Cost basis,Gain/loss,Term\n"
    ",\n"
    "The values are displayed in USD\n"
)

# Privacy markers (#1041): none may appear in a refusal, compared case-insensitively.
_NAME = "Synthetic Holder Two"
_ACCOUNT = "U1234567"
_AMOUNT = "987654.321"

# The parsers' default sections, used when no ``source`` is passed.
_OPEN = "Fidelity open lots"
_CLOSED = "Fidelity closed lots"
# The bundle's labels for a nested lots CSV: its path relative to --input.
_OPEN_SOURCE = "open lots first/View open lots.csv"
_CLOSED_SOURCE = "closed lots first/View closed lots.csv"

_NOT_SP = "Share source is not 'SP' (ESPP); only ESPP lots are supported"
_NOT_A_DATE = "not a Fidelity date (expected e.g. 'Mar-31-2026')"


def _open_refusal(text: str) -> ParseError:
    with pytest.raises(ParseError) as caught:
        parse_open_lots(text)
    return caught.value


def _closed_refusal(text: str) -> ParseError:
    with pytest.raises(ParseError) as caught:
        parse_closed_lots(text)
    return caught.value


def test_parse_open_lots_export_shape() -> None:
    lots = parse_open_lots(OPEN_CSV)
    assert lots == (
        EspLot(
            acquire_date=date(2026, 3, 31),
            quantity=Decimal("4.6315"),
            contribution_usd=Decimal("755.31"),
            cost_per_share_paid=Decimal("163.08"),
            fidelity_term="Short",
        ),
        EspLot(
            acquire_date=date(2025, 12, 31),
            quantity=Decimal("3.4820"),
            contribution_usd=Decimal("668.96"),
            cost_per_share_paid=Decimal("192.12"),
            fidelity_term="Short",
        ),
    )


def test_parse_closed_lots_empty() -> None:
    assert parse_closed_lots(CLOSED_EMPTY_CSV) == ()


def test_parse_closed_lots_one_row() -> None:
    assert parse_closed_lots(CLOSED_WITH_ROW_CSV) == (
        EspClosedLot(
            acquire_date=date(2026, 3, 31),
            sold_or_transferred_date=date(2026, 6, 15),
            quantity=Decimal("2.0000"),
            proceeds_usd=Decimal("900.00"),
            contribution_usd=Decimal("755.31"),
            fidelity_term="Short",
        ),
    )


@pytest.mark.parametrize("share_source", ["RS", _NAME])
def test_open_lots_reject_non_sp_share_source(share_source: str) -> None:
    # #1041: "RS" collides with fixed text ("parse"), so the marker carries the
    # absence check.
    bad = OPEN_CSV.replace(",SP,Short", f",{share_source},Short", 1)
    _assert_refusal(
        _open_refusal(bad),
        section=_OPEN,
        row_index=1,
        column="Share source",
        message=_NOT_SP,
        absent=(_NAME,) if share_source == _NAME else (),
    )


def test_open_lots_missing_usd_footer_rejected() -> None:
    bad = OPEN_CSV.replace("The values are displayed in USD\n", "")
    with pytest.raises(ParseError) as exc:
        parse_open_lots(bad)
    assert "USD" in exc.value.message


def test_open_lots_localized_footer_rejected() -> None:
    # Padded with empty cells to the header's 11 columns, as a USD footer may be, so
    # the row-width check passes and the missing USD footer is what refuses.
    bad = OPEN_CSV.replace(
        "The values are displayed in USD", "The values are displayed in EUR" + "," * 10
    )
    with pytest.raises(ParseError, match="missing the 'The values are displayed in USD' footer"):
        parse_open_lots(bad)


@pytest.mark.parametrize(
    ("old", "new"),
    [
        pytest.param("Holding period", "Holding term", id="renamed"),
        pytest.param("Grant date,Share source", f"{_NAME},{_ACCOUNT}", id="markers"),
    ],
)
def test_open_lots_wrong_header_rejected(old: str, new: str) -> None:
    # #1041: names the expected columns (code constants), never the file's cells.
    _assert_refusal(
        _open_refusal(_replace_once(OPEN_CSV, old, new)),
        section=_OPEN,
        row_index=0,
        column=None,
        message=f"unexpected header; expected columns {_OPEN_HEADER}",
        absent=tuple(new.split(",")),
    )


@pytest.mark.parametrize("cell", ["2026-03-31", _NAME])
def test_open_lots_bad_date_rejected(cell: str) -> None:
    _assert_refusal(
        _open_refusal(_replace_once(OPEN_CSV, "Mar-31-2026", cell)),
        section=_OPEN,
        row_index=1,
        column="Date acquired",
        message=_NOT_A_DATE,
        absent=(cell,),
    )


def test_open_lots_invalid_calendar_date_rejected() -> None:
    # #1041: neither the cell nor the date error's text is printed.
    _assert_refusal(
        _open_refusal(_replace_once(OPEN_CSV, "Mar-31-2026", "Feb-30-2026")),
        section=_OPEN,
        row_index=1,
        column="Date acquired",
        message="not a valid calendar date",
        absent=("Feb-30-2026", "day is out of range"),
    )


@pytest.mark.parametrize("cell", ["4.x315", _ACCOUNT])
def test_open_lots_bad_decimal_rejected(cell: str) -> None:
    _assert_refusal(
        _open_refusal(_replace_once(OPEN_CSV, ",4.6315,", f",{cell},")),
        section=_OPEN,
        row_index=1,
        column="Quantity",
        message="not a valid number",
        absent=(cell,),
    )


def test_open_lots_non_finite_rejected() -> None:
    # #1041: a NaN with a payload parses as a Decimal, so it reaches the
    # finiteness check; its text is never printed.
    _assert_refusal(
        _open_refusal(_replace_once(OPEN_CSV, ",4.6315,", ",NaN1234567,")),
        section=_OPEN,
        row_index=1,
        column="Quantity",
        message="must be a finite number",
        absent=("NaN1234567",),
    )


def test_open_lots_zero_quantity_rejected() -> None:
    # A zero collides with fixed text ("> 0"), so the exact message covers it.
    bad = OPEN_CSV.replace(
        "Mar-31-2026,4.6315,755.31,163.08,832.98,77.67",
        "Mar-31-2026,0,755.31,163.08,832.98,77.67",
    )
    assert bad != OPEN_CSV
    _assert_refusal(
        _open_refusal(bad),
        section=_OPEN,
        row_index=1,
        column="Quantity",
        message="Quantity must be > 0",
    )


_GAIN_CHECK = "Value - Cost basis should equal Gain/loss"
_PER_SHARE_CHECK = "Cost basis/share x Quantity should equal Cost basis"


@pytest.mark.parametrize(
    ("old", "new", "column", "check", "absent"),
    [
        # Value - Cost basis (832.98 - 755.31 = 77.67) no longer equals Gain/loss;
        # the gap 27.67 used to be printed.
        pytest.param(
            ",832.98,77.67,",
            ",832.98,50.00,",
            "Gain/loss",
            _GAIN_CHECK,
            ("27.67",),
            id="gain-loss",
        ),
        # ... and here the gap is the marker.
        pytest.param(
            ",832.98,77.67,",
            ",832.98,987731.991,",
            "Gain/loss",
            _GAIN_CHECK,
            (_AMOUNT, "987731.991"),
            id="gain-loss-marker",
        ),
        # 500.00 x 4.6315 is 1560.44 off Cost basis.
        pytest.param(
            ",755.31,163.08,",
            ",755.31,500.00,",
            "Cost basis/share",
            _PER_SHARE_CHECK,
            ("1560.44",),
            id="cost-basis-per-share",
        ),
        # 988409.631 x 1 is the marker off Cost basis.
        pytest.param(
            "Mar-31-2026,4.6315,755.31,163.08,",
            "Mar-31-2026,1,755.31,988409.631,",
            "Cost basis/share",
            _PER_SHARE_CHECK,
            (_AMOUNT, "988409.631"),
            id="cost-basis-per-share-marker",
        ),
    ],
)
def test_open_lots_inconsistent_columns_rejected(
    old: str, new: str, column: str, check: str, absent: tuple[str, ...]
) -> None:
    # #1041: the gap between the columns used to be printed; now the refusal
    # names the cross-checked column and the rule only.
    _assert_refusal(
        _open_refusal(_replace_once(OPEN_CSV, old, new)),
        section=_OPEN,
        row_index=1,
        column=column,
        message=f"{check}, but they differ by more than the rounding tolerance",
        absent=absent,
    )


def test_open_lots_wrong_column_count_rejected() -> None:
    bad = OPEN_CSV.replace(",SP,Short\n", ",SP\n", 1)
    _assert_refusal(
        _open_refusal(bad),
        section=_OPEN,
        row_index=1,
        column=None,
        message="row has 10 columns, expected 11 (possible column shift / corruption)",
    )


def test_open_lots_column_shifted_row_prints_no_cell() -> None:
    # #1041: an extra name cell shifts the row; it used to be printed whole.
    shifted = f"Mar-31-2026,{_NAME},4.6315,755.31,163.08,832.98,77.67,{_ACCOUNT},-,Jan-05-2026,SP"
    bad = _replace_once(
        OPEN_CSV, "Mar-31-2026,4.6315,755.31,163.08,832.98,77.67,-,-,Jan-05-2026,SP", shifted
    )
    _assert_refusal(
        _open_refusal(bad),
        section=_OPEN,
        row_index=1,
        column=None,
        message="row has 12 columns, expected 11 (possible column shift / corruption)",
        absent=(_NAME, _ACCOUNT, "4.6315", "755.31", "Jan-05-2026"),
    )


def test_open_lots_row_after_footer_rejected() -> None:
    bad = OPEN_CSV + f"Apr-01-2026,{_AMOUNT},100.00,100.00,110.00,10.00,{_ACCOUNT},-,{_NAME},SP\n"
    # Rows: header 0, data 1-2, the blank "," 3, the footer 4, the stray row 5.
    _assert_refusal(
        _open_refusal(bad),
        section=_OPEN,
        row_index=5,
        column=None,
        message="unexpected row after the 'The values are displayed in USD' footer",
        absent=(_NAME, _ACCOUNT, _AMOUNT, "Apr-01-2026"),
    )


def test_open_lots_empty_text_rejected() -> None:
    with pytest.raises(ParseError) as exc:
        parse_open_lots("")
    assert "empty" in exc.value.message


def test_closed_lots_disposal_before_acquisition_rejected() -> None:
    # #1041: neither date is printed.
    _assert_refusal(
        _closed_refusal(_replace_once(CLOSED_WITH_ROW_CSV, "Jun-15-2026", "Jan-01-2026")),
        section=_CLOSED,
        row_index=1,
        column="Date sold or transferred",
        message="the disposal date precedes the acquisition date",
        absent=("Jan-01-2026", "Mar-31-2026", "2026-01-01", "2026-03-31"),
    )


def test_closed_lots_inconsistent_gain_rejected() -> None:
    # #1041: Proceeds - Cost basis is 144.69, 987654.321 off the Gain/loss cell;
    # the gap used to be printed.
    bad = _replace_once(CLOSED_WITH_ROW_CSV, ",144.69,", ",987799.011,")
    _assert_refusal(
        _closed_refusal(bad),
        section=_CLOSED,
        row_index=1,
        column="Gain/loss",
        message=(
            "Proceeds - Cost basis should equal Gain/loss, but they differ by more than the "
            "rounding tolerance"
        ),
        absent=(_AMOUNT, "987799.011"),
    )


def test_detect_kind_open_closed_and_none() -> None:
    assert detect_fidelity_lots_kind(list(_OPEN_HEADER)) is FidelityLotsKind.OPEN
    assert detect_fidelity_lots_kind(list(_CLOSED_HEADER)) is FidelityLotsKind.CLOSED
    assert detect_fidelity_lots_kind(["Statement", "Header"]) is None
    assert detect_fidelity_lots_kind(["CurrencyPrimary", "Symbol"]) is None


def _near_miss(columns: list[str]) -> NearMissLotsHeader:
    kind = detect_fidelity_lots_kind(columns)
    assert isinstance(kind, NearMissLotsHeader)
    return kind


def _swap(header: tuple[str, ...], old: str, new: str) -> list[str]:
    assert old in header
    return [new if column == old else column for column in header]


def test_detect_kind_near_miss_open_lots_columns_only() -> None:
    # #911: case and spacing changes, a dropped column and a reorder keep every
    # column an open-lots one.
    for columns in (
        _swap(_OPEN_HEADER, "Holding period", "Holding  Period"),
        [c for c in _OPEN_HEADER if c != "Grant date"],
        [*_OPEN_HEADER[1:], _OPEN_HEADER[0]],
    ):
        assert _near_miss(columns).open_lots_only


def test_detect_kind_near_miss_that_may_be_closed_lots() -> None:
    for columns in (
        _swap(_CLOSED_HEADER, "Proceeds", "Proceeds (USD)"),
        # Starts with "Date acquired"; nothing else is a lots column.
        ["Date acquired (ET)", "Symbol"],
        # Only columns both exports have.
        ["Date acquired", "Quantity", "Cost basis", "Gain/loss"],
        # A renamed column might be a closed-lots one.
        _swap(_OPEN_HEADER, "Value", "Value (USD)"),
        # A column only a lots export has, not first.
        ["Symbol", "Date sold or transferred"],
    ):
        assert not _near_miss(columns).open_lots_only


@pytest.mark.parametrize(
    "columns",
    [
        ["foo", "bar"],
        ["Symbol", "Quantity", "Cost basis", "Proceeds", "Gain/loss", "Term", "Value"],
        # Schedule CG review output: its holding period is "(days)".
        ["Symbol", "Open date (DD/MM/YYYY)", "Holding period (days)", "Quantity"],
        [],
    ],
)
def test_detect_kind_ignores_a_csv_without_lots_columns(columns: list[str]) -> None:
    assert detect_fidelity_lots_kind(columns) is None


def test_near_miss_difference_names_the_expected_columns_and_counts_the_rest() -> None:
    # #1041: the expected columns are code constants and may be named; the
    # file's own header cells are only counted.
    renamed = _near_miss(_swap(_CLOSED_HEADER, "Proceeds", "Proceeds (USD)"))
    assert renamed.difference() == (
        f"compared with the closed-lots header {_CLOSED_HEADER}, missing: 'Proceeds'; "
        "unexpected: 1 column(s)"
    )
    assert "Proceeds (USD)" not in renamed.difference()
    dropped = _near_miss([c for c in _OPEN_HEADER if c != "Grant date"])
    assert dropped.difference() == (
        f"compared with the open-lots header {_OPEN_HEADER}, missing: 'Grant date'; "
        "unexpected: 0 column(s)"
    )
    duplicated = _near_miss([*_CLOSED_HEADER, "Term"])
    assert duplicated.difference() == (
        f"compared with the closed-lots header {_CLOSED_HEADER}, missing: none; "
        "unexpected: 1 column(s)"
    )
    reordered = (_CLOSED_HEADER[1], _CLOSED_HEADER[0], *_CLOSED_HEADER[2:])
    assert _near_miss(list(reordered)).difference() == (
        f"it has the closed-lots columns, but not in the expected order {_CLOSED_HEADER}"
    )


def test_near_miss_difference_prints_no_header_cell() -> None:
    # #1041: header cells are file content; a marker or an ``ANN`` cell is
    # counted, never printed.
    closed = _near_miss(_swap(tuple(_swap(_CLOSED_HEADER, "Proceeds", _NAME)), "Term", "ANN"))
    assert closed.difference() == (
        f"compared with the closed-lots header {_CLOSED_HEADER}, missing: 'Proceeds', 'Term'; "
        "unexpected: 2 column(s)"
    )
    opened = _near_miss(_swap(_OPEN_HEADER, "Grant date", _ACCOUNT))
    assert opened.difference() == (
        f"compared with the open-lots header {_OPEN_HEADER}, missing: 'Grant date'; "
        "unexpected: 1 column(s)"
    )
    for difference in (closed.difference(), opened.difference()):
        assert _NAME.casefold() not in difference.casefold()
        assert _ACCOUNT.casefold() not in difference.casefold()
        assert re.search(r"(?i)\bANN\b", difference) is None


def test_detect_near_miss_file_through_the_html_wart(tmp_path: Path) -> None:
    near_miss = tmp_path / "closed.csv"
    near_miss.write_text(CLOSED_EMPTY_CSV.replace("Proceeds,", "Proceeds (USD),"), encoding="utf-8")
    kind = detect_fidelity_lots_csv(near_miss, source="closed.csv")
    assert isinstance(kind, NearMissLotsHeader)
    assert kind.columns == tuple(_swap(_CLOSED_HEADER, "Proceeds", "Proceeds (USD)"))


def test_detect_fidelity_lots_csv_file(tmp_path: Path) -> None:
    op = tmp_path / "open.csv"
    op.write_text(OPEN_CSV, encoding="utf-8")
    assert detect_fidelity_lots_csv(op, source="open.csv") is FidelityLotsKind.OPEN
    cl = tmp_path / "closed.csv"
    cl.write_text(CLOSED_EMPTY_CSV, encoding="utf-8")
    assert detect_fidelity_lots_csv(cl, source="closed.csv") is FidelityLotsKind.CLOSED


def test_ibkr_csv_not_detected_as_fidelity(tmp_path: Path) -> None:
    ibkr = tmp_path / "ibkr.csv"
    ibkr.write_text("Statement,Header,BrokerName,Field Value\n", encoding="utf-8")
    assert detect_fidelity_lots_csv(ibkr, source="ibkr.csv") is None


@pytest.mark.parametrize(
    ("read", "source"),
    [
        pytest.param(read_open_lots, _OPEN_SOURCE, id="read-open"),
        pytest.param(read_closed_lots, _CLOSED_SOURCE, id="read-closed"),
        pytest.param(detect_fidelity_lots_csv, "first/View open lots.csv", id="detect"),
    ],
)
def test_non_utf8_csv_refuses_cleanly(tmp_path: Path, read, source: str) -> None:
    # A wrong-encoding export (e.g. re-saved as UTF-16) must refuse as a
    # ParseError, not crash the caller with a raw UnicodeDecodeError. #1041: the
    # refusal names the label it was given, never the path or the decode error.
    bad = tmp_path / _NAME / f"{_ACCOUNT}.csv"
    bad.parent.mkdir()
    bad.write_bytes(OPEN_CSV.encode("utf-16"))
    with pytest.raises(ParseError) as caught:
        read(bad, source=source)
    _assert_refusal(
        caught.value,
        section=source,
        row_index=0,
        column=None,
        message="the file is not UTF-8 text",
        absent=(str(tmp_path), _NAME, _ACCOUNT, "codec", "0xff"),
    )
    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None


def test_detect_unreadable_csv_default_label(tmp_path: Path) -> None:
    # #1041: without a label the sniff names the file kind, not its path.
    bad = tmp_path / _NAME / "open.csv"
    bad.parent.mkdir()
    bad.write_bytes(OPEN_CSV.encode("utf-16"))
    with pytest.raises(ParseError) as caught:
        detect_fidelity_lots_csv(bad)
    _assert_refusal(
        caught.value,
        section="Fidelity lots CSV",
        row_index=0,
        column=None,
        message="the file is not UTF-8 text",
        absent=(str(tmp_path), _NAME),
    )


def test_unreadable_csv_names_the_errno_not_the_path(tmp_path: Path) -> None:
    # #1041: an OSError names its errno, never its text or file name. The
    # bundle tests cover EACCES and the unknown-errno cases.
    folder = tmp_path / _NAME / "View open lots.csv"
    folder.mkdir(parents=True)
    with pytest.raises(ParseError) as caught:
        read_open_lots(folder, source=_OPEN_SOURCE)
    _assert_refusal(
        caught.value,
        section=_OPEN_SOURCE,
        row_index=0,
        column=None,
        message="could not read the file (EISDIR)",
        absent=(str(tmp_path), _NAME, "Is a directory"),
    )
    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None


@pytest.mark.parametrize(
    ("parse", "read", "text", "source", "column", "message"),
    [
        pytest.param(
            parse_open_lots,
            read_open_lots,
            OPEN_CSV.replace(",SP,Short", f",{_NAME},Short", 1),
            _OPEN_SOURCE,
            "Share source",
            _NOT_SP,
            id="open",
        ),
        pytest.param(
            parse_closed_lots,
            read_closed_lots,
            CLOSED_WITH_ROW_CSV.replace("Jun-15-2026", "Jan-01-2026"),
            _CLOSED_SOURCE,
            "Date sold or transferred",
            "the disposal date precedes the acquisition date",
            id="closed",
        ),
    ],
)
def test_source_labels_the_refusal(
    tmp_path: Path, parse, read, text: str, source: str, column: str, message: str
) -> None:
    # #1041: the bundle labels each file by its path relative to --input, and
    # that label is the section whether the parser gets the text or the file.
    path = tmp_path / _NAME / "lots.csv"
    path.parent.mkdir()
    path.write_text(text, encoding="utf-8")
    for refuse in (lambda: parse(text, source=source), lambda: read(path, source=source)):
        with pytest.raises(ParseError) as caught:
            refuse()
        _assert_refusal(
            caught.value,
            section=source,
            row_index=1,
            column=column,
            message=message,
            absent=(_NAME, str(tmp_path)),
        )


def test_fidelity_csv_not_detected_as_ibkr(tmp_path: Path) -> None:
    fid = tmp_path / "open.csv"
    fid.write_text(OPEN_CSV, encoding="utf-8")
    with pytest.raises(ParseError):
        detect_ibkr_csv_format(fid)


def test_read_open_lots_from_disk(tmp_path: Path) -> None:
    op = tmp_path / "open.csv"
    op.write_text(OPEN_CSV, encoding="utf-8")
    assert len(read_open_lots(op, source=_OPEN_SOURCE)) == 2


# The open-lots header; the closed-lots one is the shared ``CLOSED_HEADER``.
_OPEN_HEADER = (
    "Date acquired",
    "Quantity",
    "Cost basis",
    "Cost basis/share",
    "Value",
    "Gain/loss",
    "Sale availability date",
    "Transfer availability date",
    "Grant date",
    "Share source",
    "Holding period",
)


def test_read_closed_lots_from_disk(tmp_path: Path) -> None:
    cl = tmp_path / "closed.csv"
    cl.write_text(CLOSED_EMPTY_CSV, encoding="utf-8")
    assert read_closed_lots(cl, source=_CLOSED_SOURCE) == ()


def test_detect_blank_file_is_none(tmp_path: Path) -> None:
    blank = tmp_path / "blank.csv"
    blank.write_text("\n   \n,\n", encoding="utf-8")
    assert detect_fidelity_lots_csv(blank, source="blank.csv") is None


def test_open_lots_full_width_footer_accepted() -> None:
    padded = OPEN_CSV.replace(
        "The values are displayed in USD\n",
        "The values are displayed in USD,,,,,,,,,,\n",
    )
    assert len(parse_open_lots(padded)) == 2


def test_open_lots_footer_with_data_not_swallowed() -> None:
    bad = OPEN_CSV.replace(
        "The values are displayed in USD\n",
        "The values are displayed in USD,unexpected,,,,,,,,,\n",
    )
    with pytest.raises(ParseError):
        parse_open_lots(bad)


def test_closed_lots_full_width_footer_accepted() -> None:
    padded = CLOSED_EMPTY_CSV.replace(
        "The values are displayed in USD\n",
        "The values are displayed in USD,,,,,,\n",
    )
    assert parse_closed_lots(padded) == ()


@pytest.mark.parametrize(
    ("frm", "column", "message"),
    [
        pytest.param(",4.6315,", "Quantity", "Quantity must be > 0", id="quantity"),
        pytest.param(",755.31,", "Cost basis", "Cost basis must be > 0", id="cost-basis"),
        pytest.param(
            ",163.08,", "Cost basis/share", "Cost basis/share must be > 0", id="cost-per-share"
        ),
        pytest.param(",832.98,", "Value", "Value must be >= 0", id="value"),
    ],
)
def test_open_lots_negative_money_rejected(frm: str, column: str, message: str) -> None:
    # #1041: _require_positive / _require_non_negative used to print the amount.
    _assert_refusal(
        _open_refusal(_replace_once(OPEN_CSV, frm, f",-{_AMOUNT},")),
        section=_OPEN,
        row_index=1,
        column=column,
        message=message,
        absent=(_AMOUNT,),
    )


@pytest.mark.parametrize(
    ("frm", "column", "message"),
    [
        pytest.param(",2.0000,", "Quantity", "Quantity must be > 0", id="quantity"),
        pytest.param(",755.31,", "Cost basis", "Cost basis must be > 0", id="cost-basis"),
        pytest.param(",900.00,", "Proceeds", "Proceeds must be >= 0", id="proceeds"),
    ],
)
def test_closed_lots_negative_money_rejected(frm: str, column: str, message: str) -> None:
    _assert_refusal(
        _closed_refusal(_replace_once(CLOSED_WITH_ROW_CSV, frm, f",-{_AMOUNT},")),
        section=_CLOSED,
        row_index=1,
        column=column,
        message=message,
        absent=(_AMOUNT,),
    )


def test_cell_error_reports_physical_row_index() -> None:
    # Header is physical row 0; the two data rows are 1 and 2. A bad date on the
    # SECOND data row must report row_index 2 (not the 0-based data-list index 1).
    _assert_refusal(
        _open_refusal(_replace_once(OPEN_CSV, "Dec-31-2025", "Xxx-31-2025")),
        section=_OPEN,
        row_index=2,
        column="Date acquired",
        message=_NOT_A_DATE,
        absent=("Xxx-31-2025",),
    )


def test_open_lots_single_digit_day_accepted() -> None:
    lots = parse_open_lots(OPEN_CSV.replace("Mar-31-2026", "Mar-3-2026"))
    assert lots[0].acquire_date == date(2026, 3, 3)


# #1097: markup. A second privacy marker; the refusals print neither marker.
_MARKER = "Synthetic Marker Q"
_MARKED_COMMENT = f"<!-- {_NAME} {_MARKER} -->"
_HEADER_MARKUP = (
    "the header carries markup other than the export's documented <span> around "
    "'Date sold or transferred'"
)
_CELL_MARKUP = (
    "the cell holds markup ('<' or '>'); only the closed-lots 'Date sold or transferred' "
    "header may carry the export's documented <span> decoration"
)
_OPEN_ROW = "Mar-31-2026,4.6315,755.31,163.08,832.98,77.67,-,-,Jan-05-2026,SP,Short"
_CLOSED_ROW = "Mar-31-2026,2.0000,Jun-15-2026,900.00,755.31,144.69,Short"
_DECORATED_DATE_SOLD = '<span style="color: rgb(0, 0, 51);">Date sold or transferred</span>'
# The issue's examples: a Quantity of 1<span>0</span> with amounts that agree
# with 10 shares, and a comment from the end of one sale's row to a line after
# the next sale.
_OPEN_SPAN_IN_QUANTITY_CSV = _replace_once(
    OPEN_CSV,
    _OPEN_ROW,
    f"Mar-31-2026,1<span>0</span>,1630.80,163.08,1750.00,119.20,"
    f"{_MARKED_COMMENT},-,Jan-05-2026,SP,Short",
)
_CLOSED_COMMENT_ACROSS_SALES_CSV = _replace_once(
    CLOSED_WITH_ROW_CSV,
    f"{_CLOSED_ROW}\n",
    f"{_CLOSED_ROW}<!-- {_NAME}\n"
    f"Dec-31-2025,1.0000,Jun-15-2026,450.00,300.00,150.00,Short\n{_MARKER} -->\n",
)


@pytest.mark.parametrize(
    ("parse", "section", "text", "row_index", "column"),
    [
        pytest.param(
            parse_open_lots, _OPEN, _OPEN_SPAN_IN_QUANTITY_CSV, 1, "Quantity", id="span-in-quantity"
        ),
        pytest.param(
            parse_closed_lots,
            _CLOSED,
            _CLOSED_COMMENT_ACROSS_SALES_CSV,
            1,
            "Term",
            id="comment-across-sales",
        ),
        pytest.param(
            parse_open_lots,
            _OPEN,
            _replace_once(OPEN_CSV, "77.67,-,", f"77.67,<i>{_NAME} {_MARKER}</i>,"),
            1,
            "Sale availability date",
            id="ignored-open-cell",
        ),
        pytest.param(
            parse_open_lots,
            _OPEN,
            _replace_once(OPEN_CSV, "SP,Short\n,", f"SP,Short{_MARKED_COMMENT}\n,"),
            2,
            "Holding period",
            id="holding-period",
        ),
        pytest.param(
            parse_closed_lots,
            _CLOSED,
            _replace_once(CLOSED_WITH_ROW_CSV, ",Short\n", f",<b>{_NAME} {_MARKER}</b>\n"),
            1,
            "Term",
            id="term",
        ),
        pytest.param(
            parse_open_lots,
            _OPEN,
            _replace_once(
                OPEN_CSV,
                "The values are displayed in USD\n",
                f"The values are displayed in USD,{_MARKED_COMMENT}\n",
            ),
            4,
            "Quantity",
            id="footer-row",
        ),
        pytest.param(
            parse_open_lots,
            _OPEN,
            f"{OPEN_CSV}{_MARKED_COMMENT}\n",
            5,
            "Date acquired",
            id="row-after-footer",
        ),
        pytest.param(
            parse_open_lots,
            _OPEN,
            _replace_once(OPEN_CSV, _OPEN_ROW, f"{_OPEN_ROW},<b>{_NAME} {_MARKER}</b>"),
            1,
            None,
            id="past-the-header-width",
        ),
        pytest.param(
            parse_open_lots,
            _OPEN,
            _replace_once(OPEN_CSV, "Nov-03-2025", f"<{_NAME} {_MARKER}"),
            2,
            "Grant date",
            id="unmatched-open-bracket",
        ),
        pytest.param(
            parse_closed_lots,
            _CLOSED,
            _replace_once(CLOSED_WITH_ROW_CSV, ",900.00,", f",{_NAME} {_MARKER}>,"),
            1,
            "Proceeds",
            id="unmatched-close-bracket",
        ),
        pytest.param(
            parse_closed_lots,
            _CLOSED,
            _replace_once(
                CLOSED_WITH_ROW_CSV, ",Short\n", f",{_DECORATED_DATE_SOLD} {_NAME} {_MARKER}\n"
            ),
            1,
            "Term",
            id="decoration-off-the-header-line",
        ),
    ],
)
def test_markup_in_a_cell_refused(
    parse, section: str, text: str, row_index: int, column: str | None
) -> None:
    with pytest.raises(ParseError) as caught:
        parse(text)
    _assert_refusal(
        caught.value,
        section=section,
        row_index=row_index,
        column=column,
        message=_CELL_MARKUP,
        absent=(_NAME, _MARKER),
    )


@pytest.mark.parametrize(
    ("read", "source", "text", "column"),
    [
        pytest.param(
            read_open_lots, _OPEN_SOURCE, _OPEN_SPAN_IN_QUANTITY_CSV, "Quantity", id="read-open"
        ),
        pytest.param(
            read_closed_lots,
            _CLOSED_SOURCE,
            _CLOSED_COMMENT_ACROSS_SALES_CSV,
            "Term",
            id="read-closed",
        ),
    ],
)
def test_reading_a_file_with_markup_refuses(
    tmp_path: Path, read, source: str, text: str, column: str
) -> None:
    path = tmp_path / "lots.csv"
    path.write_text(text, encoding="utf-8")
    with pytest.raises(ParseError) as caught:
        read(path, source=source)
    _assert_refusal(
        caught.value,
        section=source,
        row_index=1,
        column=column,
        message=_CELL_MARKUP,
        absent=(_NAME, _MARKER),
    )


@pytest.mark.parametrize(
    ("parse", "section", "text"),
    [
        pytest.param(
            parse_closed_lots,
            _CLOSED,
            _replace_once(
                CLOSED_WITH_ROW_CSV,
                ",Proceeds,",
                f',<span style="color: rgb(0, 0, 51);">{_NAME} {_MARKER}</span>,',
            ),
            id="second-span",
        ),
        pytest.param(
            parse_closed_lots,
            _CLOSED,
            _replace_once(
                CLOSED_WITH_ROW_CSV,
                _DECORATED_DATE_SOLD,
                f"<b>Date sold or transferred</b>{_MARKED_COMMENT}",
            ),
            id="bold-date-sold",
        ),
        pytest.param(
            parse_closed_lots,
            _CLOSED,
            _replace_once(CLOSED_WITH_ROW_CSV, ",Term\n", f",Term {_NAME} {_MARKER}>\n"),
            id="stray-close-bracket",
        ),
        pytest.param(
            parse_closed_lots,
            _CLOSED,
            _replace_once(
                CLOSED_WITH_ROW_CSV,
                _DECORATED_DATE_SOLD,
                _DECORATED_DATE_SOLD.replace("rgb(0, 0, 51)", "rgb(0, 0, 52)") + _MARKED_COMMENT,
            ),
            id="undocumented-style",
        ),
        pytest.param(
            parse_open_lots,
            _OPEN,
            f"<!-- {_NAME} {_MARKER}\n" + OPEN_CSV.lstrip("\ufeff"),
            id="comment-before-the-header",
        ),
    ],
)
def test_markup_in_the_header_refused(parse, section: str, text: str) -> None:
    with pytest.raises(ParseError) as caught:
        parse(text)
    _assert_refusal(
        caught.value,
        section=section,
        row_index=0,
        column=None,
        message=_HEADER_MARKUP,
        absent=(_NAME, _MARKER),
    )


def test_decoration_removed_after_leading_blank_lines(tmp_path: Path) -> None:
    # A line of only commas and whitespace is blank, so the decorated header
    # below these is the first non-blank line.
    text = ",,\n \n" + CLOSED_WITH_ROW_CSV.lstrip("\ufeff")
    assert parse_closed_lots(text) == parse_closed_lots(CLOSED_WITH_ROW_CSV)
    path = tmp_path / "closed.csv"
    path.write_text(text, encoding="utf-8")
    assert detect_fidelity_lots_csv(path, source="closed.csv") is FidelityLotsKind.CLOSED


_OPEN_HEADER_LINE = ",".join(_OPEN_HEADER)
_CLOSED_HEADER_LINE = ",".join(_CLOSED_HEADER)


@pytest.mark.parametrize(
    ("text", "open_lots_only"),
    [
        pytest.param(
            f"<!-- {_NAME}\n{_OPEN_HEADER_LINE}\n--> {_MARKER}\n", True, id="open-header-in-comment"
        ),
        pytest.param(_OPEN_SPAN_IN_QUANTITY_CSV, True, id="span-in-open-lots-cell"),
        pytest.param(
            f"<!-- {_NAME}\n{','.join(_swap(_OPEN_HEADER, 'Holding period', 'Holding  Period'))}\n"
            f"--> {_MARKER}\n",
            True,
            id="open-only-near-miss-in-comment",
        ),
        pytest.param(
            f"<!-- {_NAME}\n{_OPEN_HEADER_LINE}\n{_CLOSED_HEADER_LINE}\n--> {_MARKER}\n",
            False,
            id="open-and-closed-headers-in-comment",
        ),
        pytest.param(f"<!--{_NAME},{_OPEN_HEADER_LINE}-->\n", False, id="wrapped-open-header"),
        pytest.param(f"foo,<{_NAME}>\n", False, id="no-header-candidate"),
        pytest.param(_CLOSED_COMMENT_ACROSS_SALES_CSV, False, id="comment-across-sales"),
        pytest.param(
            _replace_once(CLOSED_WITH_ROW_CSV, "rgb(0, 0, 51)", "rgb(0, 0, 52)"),
            False,
            id="undocumented-style",
        ),
    ],
)
def test_detect_a_csv_with_markup_as_a_markup_near_miss(
    tmp_path: Path, text: str, open_lots_only: bool
) -> None:
    path = tmp_path / "lots.csv"
    path.write_text(text, encoding="utf-8")
    assert detect_fidelity_lots_csv(path, source="lots.csv") == MarkupNearMiss(
        open_lots_only=open_lots_only
    )


# ---- Malformed CSV records (#1098) -------------------------------------------

_REEXPORT = "re-export it from NetBenefits."
_OPEN_ROW_1 = "Mar-31-2026,4.6315,755.31,163.08,832.98,77.67,-,-,Jan-05-2026,SP,Short\n"
_OPEN_ROW_2 = "Dec-31-2025,3.4820,668.96,192.12,626.24,-42.72,-,-,Nov-03-2025,SP,Short\n"
# Row 1 with its Sale availability date spanning two lines (lines 1-2).
_OPEN_TWO_LINE_ROW_1 = _OPEN_ROW_1.replace(",-,-,", ',"-\n-",-,', 1)
_TWO_LINE_AFTER_QUOTE = f'"{HOLDER}\n{MARKER}"x'
_MALFORMED_CELLS = [
    pytest.param(UNCLOSED, id="quote-left-open"),
    pytest.param(AFTER_QUOTE, id="text-after-quote"),
    pytest.param(oversized_cell(), id="oversized-cell"),
]


def _open_with_bad_row(cell: str, *, row: str = _OPEN_ROW_1, text: str = OPEN_CSV) -> str:
    """``text`` with ``row``'s Holding period replaced by ``cell``."""
    return _replace_once(text, row, row.replace(",SP,Short\n", f",SP,{cell}\n"))


@pytest.mark.parametrize("cell", _MALFORMED_CELLS)
@pytest.mark.parametrize(
    ("parse", "text", "old"),
    [
        pytest.param(parse_open_lots, OPEN_CSV, ",SP,Short\nDec", id="open"),
        pytest.param(parse_closed_lots, CLOSED_WITH_ROW_CSV, ",144.69,Short\n", id="closed"),
    ],
)
def test_reader_refuses_a_malformed_record(parse, text: str, old: str, cell: str) -> None:
    """#1098: the reader used to repair bad quoting, and a cell over the CSV
    field-size limit raised a raw ``csv.Error``. Each now refuses at the
    0-based line the bad record starts on, under the source label."""
    new = old.replace("Short", cell)
    with pytest.raises(ParseError) as caught:
        parse(_replace_once(text, old, new), source=_OPEN_SOURCE)
    assert_malformed(caught.value, section=_OPEN_SOURCE, row_index=1, line=2, reexport=_REEXPORT)


@pytest.mark.parametrize(
    ("text", "bad_row", "row_index"),
    [
        pytest.param(
            _replace_once(OPEN_CSV, "\ufeff", "\ufeff\n  \n,,\n"),
            _OPEN_ROW_1,
            4,
            id="after-blank-lines",
        ),
        pytest.param(
            _replace_once(OPEN_CSV, _OPEN_ROW_1, _OPEN_TWO_LINE_ROW_1),
            _OPEN_ROW_2,
            3,
            id="after-a-two-line-cell",
        ),
    ],
)
@pytest.mark.parametrize(
    "cell",
    [
        pytest.param(UNCLOSED, id="quote-left-open"),
        pytest.param(_TWO_LINE_AFTER_QUOTE, id="text-after-a-two-line-quote"),
    ],
)
def test_reader_names_the_line_a_malformed_record_starts_on(
    text: str, bad_row: str, row_index: int, cell: str
) -> None:
    """#1098: the refusal names the bad record's first line, counting blank
    lines and every line an earlier quoted cell spans, though the reader has
    read past it, to the end of the file for a quote left open."""
    with pytest.raises(ParseError) as caught:
        parse_open_lots(_open_with_bad_row(cell, row=bad_row, text=text))
    assert_malformed(
        caught.value, section=_OPEN, row_index=row_index, line=row_index + 1, reexport=_REEXPORT
    )


@pytest.mark.parametrize(
    ("row_2", "column", "message"),
    [
        pytest.param(_OPEN_ROW_2.replace(",SP,", ",RS,"), "Share source", _NOT_SP, id="cell"),
        pytest.param(
            _OPEN_ROW_2.replace(",Short\n", "\n"),
            None,
            "row has 10 columns, expected 11 (possible column shift / corruption)",
            id="row",
        ),
    ],
)
def test_reader_names_the_line_a_row_starts_on(
    row_2: str, column: str | None, message: str
) -> None:
    """#1098: a refusal names the 0-based line its row starts on, not the
    row's count, which falls one short after row 1's two-line cell."""
    text = _replace_once(OPEN_CSV, _OPEN_ROW_1 + _OPEN_ROW_2, _OPEN_TWO_LINE_ROW_1 + row_2)
    _assert_refusal(_open_refusal(text), section=_OPEN, row_index=3, column=column, message=message)


@pytest.mark.parametrize("cell", _MALFORMED_CELLS)
def test_detector_refuses_a_malformed_first_record(tmp_path: Path, cell: str) -> None:
    """#1098: a CSV whose first record can't be read can't be ruled out as a
    lots export, so the detector refuses it under the source label."""
    path = tmp_path / "unrelated.csv"
    path.write_text(f"Date acquired,{cell},Cost basis\n{_OPEN_ROW_1}", encoding="utf-8")
    with pytest.raises(ParseError) as caught:
        detect_fidelity_lots_csv(path, source=_OPEN_SOURCE)
    assert_malformed(caught.value, section=_OPEN_SOURCE, row_index=0, line=1, reexport=_REEXPORT)


@pytest.mark.parametrize(
    ("before", "row_index"),
    [
        pytest.param("\n  \n,,\n", 3, id="after-blank-lines"),
        pytest.param('" \n "\n', 2, id="after-a-two-line-cell"),
    ],
)
@pytest.mark.parametrize(
    "cell",
    [
        pytest.param(UNCLOSED, id="quote-left-open"),
        pytest.param(_TWO_LINE_AFTER_QUOTE, id="text-after-a-two-line-quote"),
    ],
)
def test_detector_names_the_line_a_malformed_record_starts_on(
    tmp_path: Path, before: str, row_index: int, cell: str
) -> None:
    """#1098: the refusal names the first non-blank record's first line,
    counting blank lines and every line a blank quoted cell spans, though the
    reader has read past it. Without a source label it names the file kind."""
    path = tmp_path / "unrelated.csv"
    path.write_text(f"{before}Date acquired,{cell}\n{_OPEN_ROW_1}", encoding="utf-8")
    with pytest.raises(ParseError) as caught:
        detect_fidelity_lots_csv(path)
    assert_malformed(
        caught.value,
        section="Fidelity lots CSV",
        row_index=row_index,
        line=row_index + 1,
        reexport=_REEXPORT,
    )


def test_detector_reads_every_record_of_a_markup_file_strictly(tmp_path: Path) -> None:
    """#1098, #1097: a file with markup has every record read, so a malformed
    record past the header refuses, at the line it starts on (row 1's cell
    spans two lines)."""
    text = _replace_once(OPEN_CSV, _OPEN_ROW_1, _OPEN_TWO_LINE_ROW_1)
    path = tmp_path / "open.csv"
    path.write_text(
        _replace_once(text, _OPEN_ROW_2, _OPEN_ROW_2.replace(",SP,Short", f",<b>,{AFTER_QUOTE}")),
        encoding="utf-8",
    )
    with pytest.raises(ParseError) as caught:
        detect_fidelity_lots_csv(path, source=_OPEN_SOURCE)
    assert_malformed(caught.value, section=_OPEN_SOURCE, row_index=3, line=4, reexport=_REEXPORT)
