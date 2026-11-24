"""Tests for parse_open_positions."""

from __future__ import annotations

import csv
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from ibkr_fa.exceptions import InvariantError, ParseError
from ibkr_fa.models import OpenLot, Side
from ibkr_fa.parser import KnownSection, parse_open_positions, split_sections
from ibkr_fa.parser.open_positions import _SECTION
from ibkr_fa.types import CountryCode
from tests._flex_section_rows import (
    FLEX_SECTION_KINDS,
    OPEN_POSITIONS_HEADER,
    OPEN_POSITIONS_XQAW,
    TRADES_EXECUTION_XQAW,
    TRADES_LOT_DETAIL_HEADER,
)
from tests._flex_section_rows import make_open_position_row as make_row
from tests._no_echo import HOLDER, assert_no_markers
from tests._private_checks import private_test
from tests._private_data import private_dataset


def test_section_constant() -> None:
    """Confirm the section name constant matches the public KnownSection."""
    assert KnownSection.OPEN_POSITIONS_LOT.value == _SECTION


def test_parses_valid_xqaw_row() -> None:
    [lot] = parse_open_positions([make_row()])
    assert lot.symbol == "XQAW"
    assert lot.isin == "US99990E1038"
    assert lot.quantity == Decimal("25")
    assert lot.cost_basis_money == Decimal("969.75")
    assert lot.side is Side.LONG
    assert lot.open_date_time == date(2026, 6, 16)
    assert lot.report_date == date(2026, 12, 31)


def test_parses_decimal_full_precision() -> None:
    [lot] = parse_open_positions([make_row(CostBasisMoney="969.750000")])
    assert str(lot.cost_basis_money) == "969.750000"


def test_parses_multiple_rows_in_input_order() -> None:
    rows = [
        make_row(Symbol="XQAW", ISIN="US99990E1038"),
        make_row(
            Symbol="XQAK",
            Description="SAMPLE ISSUER AK CORP",
            CUSIP="99990H106",
            ISIN="US99990H1068",
        ),
        make_row(
            Symbol="XQAD",
            Description="SAMPLE ISSUER AD NV",
            ISIN="NL99971G1013",
            IssuerCountryCode="NL",
            CUSIP="",
        ),
    ]
    lots = parse_open_positions(rows)
    assert [lot.symbol for lot in lots] == ["XQAW", "XQAK", "XQAD"]
    assert lots[2].issuer_country_code == "NL"


def test_empty_input_yields_empty_list() -> None:
    assert parse_open_positions([]) == []


def test_rejects_non_usd_lot() -> None:
    with pytest.raises(ParseError) as exc:
        parse_open_positions([make_row(CurrencyPrimary="EUR")])
    assert exc.value.section == _SECTION
    assert exc.value.column == "CurrencyPrimary"
    assert "USD only" in exc.value.message
    assert "issues/52" in exc.value.message


def test_report_date_policy_is_left_to_the_caller() -> None:
    """#625: the parser accepts any ReportDate; the Dec-31 check lives in
    compute.source_coverage and runs after the whole LOT section parses."""
    [lot] = parse_open_positions([make_row(ReportDate="20260617", OpenDateTime="20260616")])
    assert lot.report_date == date(2026, 6, 17)


def test_open_date_after_report_date_raises_invariant_error() -> None:
    """#625: a lot opened after its ReportDate now fails OpenLot's own
    invariant at parse time, before any ReportDate policy check runs."""
    with pytest.raises(InvariantError, match="open_date_time must be on or before report_date"):
        parse_open_positions([make_row(ReportDate="20251231")])


def test_rejects_pre_2020_acquisition() -> None:
    with pytest.raises(ParseError) as exc:
        parse_open_positions(
            [make_row(OpenDateTime="20191231", HoldingPeriodDateTime="20191231")],
        )
    assert exc.value.column == "OpenDateTime"
    assert "pre-2020" in exc.value.message.lower() or "2020-01-06" in exc.value.message


def test_accepts_acquisition_on_archive_start_date() -> None:
    [lot] = parse_open_positions(
        [make_row(HoldingPeriodDateTime="20200106", OpenDateTime="20200106")],
    )
    assert lot.open_date_time == date(2020, 1, 6)


def test_rejects_acquisition_one_day_before_archive_start() -> None:
    with pytest.raises(ParseError) as exc:
        parse_open_positions(
            [make_row(OpenDateTime="20200105", HoldingPeriodDateTime="20200105")],
        )
    assert exc.value.column == "OpenDateTime"


def test_rejects_pre_2020_open_even_if_holding_period_is_post_2020() -> None:
    # Wash-sale rules can leave HoldingPeriodDateTime earlier than
    # OpenDateTime, but Schedule FA initial value is FX-converted on the
    # cash-outlay date (OpenDateTime). Reject if OpenDateTime predates
    # the TTBR archive even when HoldingPeriodDateTime would pass on its
    # own; otherwise the FX layer would later raise MissingFxRateError
    # and break the parser's fail-fast contract.
    with pytest.raises(ParseError) as exc:
        parse_open_positions(
            [make_row(OpenDateTime="20191231", HoldingPeriodDateTime="20200601")],
        )
    assert exc.value.column == "OpenDateTime"


@pytest.mark.parametrize(
    ("holding", "holding_iso"),
    [("20260504", "2026-05-04"), ("20260617", "2026-06-17")],
)
def test_rejects_holding_period_date_differing_from_open_date(
    holding: str, holding_iso: str
) -> None:
    """#599: IBKR moves HoldingPeriodDateTime for US wash sales, and
    the lot's CostBasisMoney may then carry the US disallowed-loss
    add-back. Flex can't show the actual cost, so the row is refused
    in either direction: one holding date falls before the open date,
    the other after it. #1038: the error names neither the lot nor
    its dates."""
    with pytest.raises(ParseError) as exc:
        parse_open_positions(
            [make_row(), make_row(OpenDateTime="20260616", HoldingPeriodDateTime=holding)],
        )
    assert exc.value.section == "Open Positions (LOT)"
    assert exc.value.column == "HoldingPeriodDateTime"
    assert exc.value.row_index == 1
    assert exc.value.raw_value == ""
    for part in ("HoldingPeriodDateTime differs from OpenDateTime", "issues/265"):
        assert part in exc.value.message
    for part in ("XQAW", "US99990E1038", "2026-06-16", "20260616", holding_iso, holding):
        assert part not in str(exc.value)


def test_rejects_empty_isin() -> None:
    """#150: empty ISIN is rejected with the new
    :func:`ibkr_fa.parser._isin.validate_isin_or_raise` error
    message (mentioning ISO 6166 shape + expected example)."""
    with pytest.raises(ParseError) as exc:
        parse_open_positions([make_row(ISIN="")])
    assert exc.value.column == "ISIN"
    assert "is empty" in exc.value.message
    assert "ISO 6166" in exc.value.message


def test_rejects_malformed_isin_shape() -> None:
    """#150: a malformed ISIN (wrong length, bad characters) is
    rejected at parse time with row context — previously slipped past
    the empty-check into ``OpenLot.__post_init__``'s contextless
    InvariantError.
    """
    with pytest.raises(ParseError) as exc:
        parse_open_positions([make_row(ISIN="US-78331005")])
    assert exc.value.section == "Open Positions (LOT)"
    assert exc.value.column == "ISIN"
    assert "ISO 6166" in exc.value.message
    assert "issues/150" in exc.value.message


def test_normalises_lowercase_isin_to_uppercase() -> None:
    """#150 (PR #146 Tier-5 P2 pattern): a lowercase ISIN cell is
    case-normalised at parse time, so the resulting OpenLot has the
    canonical uppercase value.
    """
    [lot] = parse_open_positions(
        [
            make_row(
                Symbol="XQAQ",
                Description="SAMPLE ISSUER AQ INC",
                CUSIP="99992A109",
                ISIN="us99992a1097",
            )
        ]
    )
    assert lot.isin == "US99992A1097"


def test_rejects_unknown_side() -> None:
    with pytest.raises(ParseError) as exc:
        parse_open_positions([make_row(Side="Sideways")])
    assert exc.value.column == "Side"
    assert "Long" in exc.value.message
    assert "Short" in exc.value.message


def test_rejects_invalid_yyyymmdd_format() -> None:
    """A non-8-digit ReportDate (e.g., '2026-12-31') is rejected before
    the year-end check, with a clear format error."""
    with pytest.raises(ParseError) as exc:
        parse_open_positions([make_row(ReportDate="2026-12-31")])
    assert exc.value.column == "ReportDate"
    assert "8 digits" in exc.value.message


def test_rejects_invalid_calendar_date() -> None:
    """A syntactically-correct YYYYMMDD that's not a real date (Feb 30)."""
    with pytest.raises(ParseError) as exc:
        parse_open_positions([make_row(OpenDateTime="20260230")])
    assert exc.value.column == "OpenDateTime"
    assert "calendar date" in exc.value.message


def test_rejects_non_decimal_quantity() -> None:
    with pytest.raises(ParseError) as exc:
        parse_open_positions([make_row(Quantity="ten")])
    assert exc.value.column == "Quantity"


@pytest.mark.parametrize("bad_value", ["NaN", "Infinity", "-Infinity", "sNaN"])
def test_rejects_non_finite_decimal_quantity(bad_value: str) -> None:
    """Tier-1 PR #136 r2 review: Decimal accepts NaN/Infinity as
    valid syntax. NaN would raise raw InvalidOperation past OpenLot's
    `quantity > 0` invariant check; Infinity would propagate into
    the PositionValue cross-check. _decimal() now rejects non-finite
    values up front (same guard as the Activity Statement parser)."""
    with pytest.raises(ParseError) as exc:
        parse_open_positions([make_row(Quantity=bad_value)])
    assert exc.value.column == "Quantity"
    assert "finite" in exc.value.message.lower()


# Quantity * MarkPrice is 25 * 45.10 = 1127.50: the first list sits within
# 0.01 of it, the second just outside (and far off).
@pytest.mark.parametrize("position_value", ["1127.49", "1127.5", "1127.51"])
def test_accepts_position_value_within_a_cent_of_quantity_times_mark(position_value: str) -> None:
    [lot] = parse_open_positions([make_row(PositionValue=position_value)])
    assert lot.quantity * lot.mark_price == Decimal("1127.50")


@pytest.mark.parametrize("position_value", ["1127.48", "1127.52", "999.99"])
def test_rejects_position_value_off_quantity_times_mark(position_value: str) -> None:
    """#629: the lot keeps no PositionValue, but a row whose PositionValue
    is more than 0.01 from Quantity * MarkPrice is still refused, with
    the row's context."""
    with pytest.raises(ParseError) as exc:
        parse_open_positions([make_row(), make_row(PositionValue=position_value)])
    assert exc.value.section == _SECTION
    assert exc.value.row_index == 1
    assert exc.value.column == "PositionValue"
    assert exc.value.raw_value == ""
    assert exc.value.message == "must equal Quantity * MarkPrice within 0.01"


@pytest.mark.parametrize("cost_basis_price", ["0", "38.79"])
def test_accepts_non_negative_cost_basis_price(cost_basis_price: str) -> None:
    [lot] = parse_open_positions([make_row(CostBasisPrice=cost_basis_price)])
    assert lot.cost_basis_money == Decimal("969.75")


def test_rejects_negative_cost_basis_price() -> None:
    """#629: the lot keeps no CostBasisPrice, but a negative one is still
    refused, with the row's context."""
    with pytest.raises(ParseError) as exc:
        parse_open_positions([make_row(), make_row(CostBasisPrice="-0.01")])
    assert exc.value.section == _SECTION
    assert exc.value.row_index == 1
    assert exc.value.column == "CostBasisPrice"
    assert exc.value.raw_value == ""
    assert exc.value.message == "must be non-negative"


@pytest.mark.parametrize("column", ["PositionValue", "CostBasisPrice"])
@pytest.mark.parametrize("bad_value", ["abc", "NaN", "-Infinity"])
def test_rejects_non_decimal_position_value_and_cost_basis_price(
    column: str, bad_value: str
) -> None:
    with pytest.raises(ParseError) as exc:
        parse_open_positions([make_row(**{column: bad_value})])
    assert exc.value.column == column
    assert exc.value.raw_value == ""
    assert bad_value.casefold() not in str(exc.value).casefold()


def test_short_side_round_trips() -> None:
    """v1 doesn't filter SHORT lots at parse time — that happens in compute."""
    [lot] = parse_open_positions([make_row(Side="Short")])
    assert lot.side is Side.SHORT


def test_first_offending_row_aborts_whole_batch() -> None:
    """Mixed valid + invalid: the first invalid row wins; later rows
    aren't silently skipped."""
    rows = [make_row(), make_row(CurrencyPrimary="EUR"), make_row()]
    with pytest.raises(ParseError) as exc:
        parse_open_positions(rows)
    # row_index 1 = the EUR row.
    assert exc.value.row_index == 1
    assert exc.value.column == "CurrencyPrimary"


def test_row_index_is_zero_based_within_input() -> None:
    rows = [make_row(), make_row(), make_row(ISIN="")]
    with pytest.raises(ParseError) as exc:
        parse_open_positions(rows)
    assert exc.value.row_index == 2


_KIND_MARKER = "Synthetic Marker Q"
_LOT_ONLY = (
    "must be 'LOT': each Open Positions row is read as one lot, so a SUMMARY or other row "
    "would be counted as another holding. Set the Flex Query's Open Positions Level of "
    "Detail to Lot."
)
_STOCKS_ONLY = (
    "must be 'STK': v1 reads only stock rows, and would read any other instrument as a stock."
)


def _refusal(row: dict[str, str]) -> ParseError:
    """The refusal of ``row``, parsed after a valid row."""
    with pytest.raises(ParseError) as exc:
        parse_open_positions([make_row(), row])
    return exc.value


@pytest.mark.parametrize(
    ("column", "value"),
    [
        *(("AssetClass", value) for value in ("NEW_ASSET", "OPT", "FUND", "BOND", "", HOLDER)),
        *(("LevelOfDetail", value) for value in ("NEW_SUMMARY", "SUMMARY", "", HOLDER)),
    ],
)
def test_refuses_a_row_that_is_not_one_stock_lot(column: str, value: str) -> None:
    """#1093: the parser read neither column, so another instrument, or a
    row that isn't one lot, was read as a stock lot. The neighbouring cells
    carry markers, and the currency would refuse if it were read first."""
    error = _refusal(
        make_row(**{column: value, "SubCategory": HOLDER, "CurrencyPrimary": _KIND_MARKER})
    )
    assert (error.section, error.row_index, error.column, error.message, error.raw_value) == (
        _SECTION,
        1,
        column,
        {"AssetClass": _STOCKS_ONLY, "LevelOfDetail": _LOT_ONLY}[column],
        "",
    )
    assert_no_markers(str(error), HOLDER, _KIND_MARKER)


@pytest.mark.parametrize(
    "cells",
    [{"OpenDateTime": "", "HoldingPeriodDateTime": ""}, {"AssetClass": "OPT"}],
    ids=["no-open-date", "not-a-stock"],
)
def test_a_summary_row_refuses_on_its_level_of_detail(cells: dict[str, str]) -> None:
    """#1093: a SUMMARY row with no open date refused with the date message.
    LevelOfDetail must win over the date and asset-class checks."""
    error = _refusal(
        make_row(LevelOfDetail="SUMMARY", SubCategory=HOLDER, Description=_KIND_MARKER, **cells)
    )
    assert (error.column, error.message) == ("LevelOfDetail", _LOT_ONLY)
    assert_no_markers(str(error), HOLDER, _KIND_MARKER)


# A record whose first cell is one of these starts a new section.
_SECTION_FIRST_CELLS = frozenset({"CurrencyPrimary", "ClientAccountID"})


def _open_positions_records(records: list[list[str]]) -> tuple[list[str], list[list[str]]]:
    """The Open Positions (LOT) header and data records among ``records``,
    found without the splitter: the header holds ``make_row``'s columns,
    and the data records are the non-blank records after it, up to the
    next known header in any column order or record whose first cell starts
    a section. The data records are
    the lists in ``records``, so editing one edits ``records``."""
    columns = frozenset(make_row())
    starts = [i for i, record in enumerate(records) if frozenset(record) == columns]
    assert len(starts) <= 1  # the splitter refuses a repeated section
    if not starts:
        return [], []
    data: list[list[str]] = []
    for record in records[starts[0] + 1 :]:
        if record and (
            record[0] in _SECTION_FIRST_CELLS or frozenset(record) in FLEX_SECTION_KINDS
        ):
            break
        if any(cell.strip() for cell in record):
            data.append(record)
    return records[starts[0]], data


def test_open_position_reader_stops_at_reordered_next_header(tmp_path: Path) -> None:
    header, row = csv.reader([OPEN_POSITIONS_HEADER, OPEN_POSITIONS_XQAW])
    next_header, next_row = csv.reader([TRADES_LOT_DETAIL_HEADER, TRADES_EXECUTION_XQAW])
    index = next_header.index("ISIN")
    records = [
        header,
        row,
        next_header[index:] + next_header[:index],
        next_row[index:] + next_row[:index],
    ]
    path = tmp_path / "sections.csv"
    with path.open("w", newline="", encoding="utf-8") as target:
        csv.writer(target).writerows(records)
    assert set(split_sections(path)) == {KnownSection.OPEN_POSITIONS_LOT, KnownSection.TRADES}
    assert _open_positions_records(records) == (header, [row])


def _flex_date(cell: str) -> date:
    return date(int(cell[:4]), int(cell[4:6]), int(cell[6:8]))


def _lot_fields(lot: OpenLot) -> tuple[object, ...]:
    return (
        lot.currency_primary,
        lot.symbol,
        lot.description,
        lot.isin,
        lot.issuer_country_code,
        lot.report_date,
        lot.quantity,
        lot.mark_price,
        lot.cost_basis_money,
        lot.side.value,
        lot.open_date_time,
    )


def _row_fields(row: dict[str, str]) -> tuple[object, ...]:
    return (
        row["CurrencyPrimary"],
        row["Symbol"],
        row["Description"],
        row["ISIN"].strip().upper(),
        row["IssuerCountryCode"].strip().upper(),
        _flex_date(row["ReportDate"]),
        Decimal(row["Quantity"]),
        Decimal(row["MarkPrice"]),
        Decimal(row["CostBasisMoney"]),
        row["Side"],
        _flex_date(row["OpenDateTime"]),
    )


@private_test
def test_works_against_multi_section_splitter_output(tmp_path: Path) -> None:
    """End-to-end against ``ibkr/flex-query.csv`` (skips when absent),
    checking only properties relative to the file itself.

    v1 reads year-end snapshots, so each Open Positions (LOT) ReportDate
    cell, read with ``csv``, first moves to Dec 31 of its own year. The
    rewritten file then goes through split_sections and
    parse_open_positions, and each lot must equal its rewritten row read
    with ``csv``, so a rewrite that missed the file fails.
    """
    [sample] = private_dataset("ibkr/flex-query.csv")
    with sample.open(newline="", encoding="utf-8-sig") as f:
        records = list(csv.reader(f))
    header, data = _open_positions_records(records)
    for record in data:
        cell = header.index("ReportDate")
        record[cell] = f"{record[cell][:4]}1231"
    fixture_path = tmp_path / "year_end.csv"
    with fixture_path.open("w", newline="", encoding="utf-8") as f:
        csv.writer(f, quoting=csv.QUOTE_ALL, lineterminator="\n").writerows(records)
    section = split_sections(fixture_path).get(KnownSection.OPEN_POSITIONS_LOT)
    lots = parse_open_positions([] if section is None else section.rows)
    rows = [dict(zip(header, record, strict=True)) for record in data]
    assert [_lot_fields(lot) for lot in lots] == [_row_fields(row) for row in rows]
    assert all((lot.report_date.month, lot.report_date.day) == (12, 31) for lot in lots)


# ---- #147 IN-prefix rejection + parse-time lookup_country defence -------


def test_rejects_indian_issuer_country_code() -> None:
    """#147: an IN-prefixed IssuerCountryCode is refused at parse time
    (mirrors the AS-side #143 fix). Schedule FA reports FOREIGN assets;
    Indian-issued securities don't belong on it regardless of broker.
    The engine wouldn't refuse it: IN is in the CBDT table, so FA A3
    would give the lot a row and CG would only set it aside as
    Indian-source.
    """
    row = make_row(IssuerCountryCode="IN")
    with pytest.raises(ParseError) as exc:
        parse_open_positions([row])
    assert exc.value.section == "Open Positions (LOT)"
    assert exc.value.column == "IssuerCountryCode"
    assert "'IN' (India)" in exc.value.message
    assert "FOREIGN" in exc.value.message
    assert exc.value.raw_value == ""


def test_rejects_unmapped_issuer_country_code() -> None:
    """#147: an ISO-valid country code that's not in the bundled CBDT
    country-code table is rejected at parse time with the right
    actionable hint (extend itr_country_codes.csv from the ITR-2
    utility) — previously would crash at compute time with a
    contextless CountryCodeNotFoundError."""
    row = make_row(IssuerCountryCode="ZZ")
    with pytest.raises(ParseError) as exc:
        parse_open_positions([row])
    assert exc.value.column == "IssuerCountryCode"
    assert "ZZ" not in str(exc.value)
    assert "itr_country_codes.csv" in exc.value.message


def test_accepts_valid_non_us_issuer_country_code() -> None:
    """#147 sanity inverse: a mapped non-US country (CA) still parses
    cleanly. Locks the IN+unmapped defences against an over-eager
    regression that would accidentally reject legitimate
    non-US-incorporated US-listed foreign issuers.
    """
    row = make_row(
        Symbol="XQAL",
        Description="SAMPLE ISSUER AL INC",
        CUSIP="99991A100",
        IssuerCountryCode="CA",
        ISIN="CA99991A1003",  # synthetic Canadian issuer listed in the US
    )
    [lot] = parse_open_positions([row])
    assert lot.issuer_country_code == CountryCode("CA")


def test_rejects_lowercase_indian_issuer_country_code() -> None:
    """T1-correctness PR #169 P3-1: a lowercase 'in' IssuerCountryCode
    in a hypothetical Flex export is normalised via .strip().upper()
    before the IN-guard, so it's caught by the IN-rejection path (not
    mis-routed through the unmapped-country branch with a misleading
    'add to itr_country_codes.csv' hint).
    """
    row = make_row(IssuerCountryCode="in")
    with pytest.raises(ParseError) as exc:
        parse_open_positions([row])
    assert exc.value.column == "IssuerCountryCode"
    assert "'IN' (India)" in exc.value.message


def test_rejects_padded_indian_issuer_country_code() -> None:
    """T1-correctness PR #169 P3-1: surrounding whitespace stripped
    before the IN-guard."""
    row = make_row(IssuerCountryCode="  IN  ")
    with pytest.raises(ParseError) as exc:
        parse_open_positions([row])
    assert exc.value.column == "IssuerCountryCode"
    assert "'IN' (India)" in exc.value.message


def test_rejects_empty_issuer_country_code() -> None:
    """T1-correctness PR #169 P3-2: an empty IssuerCountryCode is
    refused with a clear 'missing column' message via _non_empty,
    rather than mis-routing through the unmapped-country branch's
    'add to itr_country_codes.csv' hint (which would mis-instruct
    the user to add the empty string to the CSV)."""
    row = make_row(IssuerCountryCode="")
    with pytest.raises(ParseError) as exc:
        parse_open_positions([row])
    assert exc.value.column == "IssuerCountryCode"
    assert "non-empty" in exc.value.message


def test_rejects_whitespace_only_issuer_country_code() -> None:
    """T2 PR #169 P3: a whitespace-only IssuerCountryCode is caught
    by the non-empty check AFTER normalisation (.strip().upper() →
    '' → _non_empty raises). Previously (T1-fix-only) the order was
    inverted — _non_empty saw the truthy '   ' first, passed, then
    normalisation produced '' which mis-routed through the
    unmapped-country branch with the misleading 'add to
    itr_country_codes.csv' hint.
    """
    row = make_row(IssuerCountryCode="   ")
    with pytest.raises(ParseError) as exc:
        parse_open_positions([row])
    assert exc.value.column == "IssuerCountryCode"
    assert "non-empty" in exc.value.message


# ---- #794 IssuerCountryCode must equal the ISIN prefix -------------------


@pytest.mark.parametrize("issuer_country", ["CA", "ca", "  CA  "])
def test_refuses_issuer_country_differing_from_isin_prefix(issuer_country: str) -> None:
    """#794: which country A3 reports when IssuerCountryCode differs from
    the ISIN prefix is unresolved, so the lot is refused, naming its row.
    The comparison runs after the strip + upper normalisation."""
    rows = [
        make_row(),
        make_row(
            Symbol="XQAQ",
            Description="SAMPLE ISSUER AQ INC",
            CUSIP="99992A109",
            ISIN="US99992A1097",
            IssuerCountryCode=issuer_country,
        ),
    ]
    with pytest.raises(ParseError) as exc:
        parse_open_positions(rows)
    assert exc.value.section == _SECTION
    assert exc.value.row_index == 1
    assert exc.value.column == "IssuerCountryCode"
    assert exc.value.raw_value == ""
    assert "unresolved" in exc.value.message
    assert "issues/794" in exc.value.message


@pytest.mark.parametrize(
    ("isin", "issuer_country"),
    [("US99992A1097", "US"), ("us99992a1097", "us"), ("  US99992A1097  ", "  us  ")],
)
def test_accepts_issuer_country_equal_to_isin_prefix(isin: str, issuer_country: str) -> None:
    """#794 inverse: agreeing values still parse after normalisation."""
    [lot] = parse_open_positions(
        [
            make_row(
                Symbol="XQAQ",
                Description="SAMPLE ISSUER AQ INC",
                CUSIP="99992A109",
                ISIN=isin,
                IssuerCountryCode=issuer_country,
            )
        ]
    )
    assert lot.isin == "US99992A1097"
    assert lot.issuer_country_code == CountryCode("US")
