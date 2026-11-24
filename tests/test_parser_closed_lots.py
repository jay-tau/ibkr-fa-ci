"""Tests for :mod:`ibkr_fa.parser.closed_lots`."""

from __future__ import annotations

import csv
from collections.abc import Callable
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from ibkr_fa.compute.lot_matching import BrokerBasisError, require_indian_basis
from ibkr_fa.exceptions import ParseError
from ibkr_fa.models import BasisProvenance, ClosedLot
from ibkr_fa.parser import KnownSection, parse_closed_lots, split_sections
from tests._crash_reports import assert_prints_no_marker, assert_replaced_unchained
from tests._flex_lot_rows import closing_execution_for, make_closed_lot_row, with_closing_executions
from tests._flex_section_rows import (
    FLEX_SECTION_KINDS,
    OPEN_POSITIONS_HEADER,
    OPEN_POSITIONS_XQAW,
    TRADES_CLOSED_LOT_XQAT,
    TRADES_LOT_DETAIL_HEADER,
)
from tests._lot_matching_inputs import BROKER_BASIS_MESSAGE
from tests._no_echo import HOLDER, assert_no_markers
from tests._private_checks import private_test
from tests._private_data import private_dataset


def make_execution_row(**overrides: str) -> dict[str, str]:
    """Synthetic EXECUTION row: an XQAW opening buy of 25 shares at 38.75
    (TradeMoney 968.75), with a 1.00 commission (NetCash -969.75)."""
    base = {
        "CurrencyPrimary": "USD",
        "FXRateToBase": "1",
        "AssetClass": "STK",
        "SubCategory": "COMMON",
        "Symbol": "XQAW",
        "Description": "SAMPLE ISSUER AW INC",
        "CUSIP": "99990E103",
        "ISIN": "US99990E1038",
        "ListingExchange": "NASDAQ",
        "IssuerCountryCode": "US",
        "ReportDate": "20260616",
        "TradeDate": "20260616",
        "SettleDateTarget": "20260617",
        "TransactionType": "ExchTrade",
        "Quantity": "25",
        "TradePrice": "38.75",
        "TradeMoney": "968.75",
        "Proceeds": "-968.75",
        "Taxes": "0",
        "IBCommission": "-1",
        "IBCommissionCurrency": "USD",
        "NetCash": "-969.75",
        "ClosePrice": "39.60",
        "Open/CloseIndicator": "O",
        "CostBasis": "969.75",
        "FifoPnlRealized": "0",
        "Buy/Sell": "BUY",
        "LevelOfDetail": "EXECUTION",
        "OpenDateTime": "",
        "HoldingPeriodDateTime": "",
    }
    base.update(overrides)
    return base


# ---- happy paths -----------------------------------------------------------


def test_parses_a_closed_lot_row() -> None:
    [lot] = parse_closed_lots(with_closing_executions(make_closed_lot_row()))
    assert lot.currency_primary == "USD"
    assert lot.symbol == "XQAT"
    assert lot.isin == "US99990G1085"
    assert lot.quantity == Decimal("12")
    assert lot.cost_basis_money == Decimal("496.20")
    assert lot.proceeds_money - lot.cost_basis_money == Decimal("-23.40")
    # Derived: cost_basis + pnl = 496.20 + (-23.40) = 472.80
    assert lot.proceeds_money == Decimal("472.80")
    assert lot.open_date_time == date(2026, 7, 21)
    assert lot.close_date_time == date(2026, 7, 21)


def test_proceeds_derivation_uses_full_decimal_precision() -> None:
    """The derivation must preserve the full precision of ``CostBasis +
    FifoPnlRealized`` — no float rounding in the path."""
    row = make_closed_lot_row(
        CostBasis="1234.567890123456789", FifoPnlRealized="-12.345678901234567"
    )
    [lot] = parse_closed_lots(with_closing_executions(row))
    assert lot.proceeds_money == Decimal("1222.222211222222222")


def test_silently_drops_execution_rows() -> None:
    """EXECUTION rows are not closed lots; they yield none — callers
    explicitly want only the closed-lot subset."""
    lots = parse_closed_lots(
        [
            make_execution_row(),
            *with_closing_executions(make_closed_lot_row()),
            make_execution_row(),
        ]
    )
    assert len(lots) == 1
    assert lots[0].symbol == "XQAT"


def test_empty_input_returns_empty_list() -> None:
    assert parse_closed_lots([]) == []


def test_only_executions_returns_empty_list() -> None:
    """Trades section with no closed lots (e.g., user only opened
    positions during the year) must produce an empty list, not raise."""
    assert parse_closed_lots([make_execution_row(), make_execution_row()]) == []


def test_preserves_input_order() -> None:
    """Multiple CLOSED_LOT rows are returned in the order they appear
    in the input."""
    lots = parse_closed_lots(
        with_closing_executions(
            make_closed_lot_row(Symbol="XQAT"),
            make_closed_lot_row(
                Symbol="XQAK",
                Description="SAMPLE ISSUER AK CORP",
                CUSIP="99990H106",
                ISIN="US99990H1068",
            ),
            make_closed_lot_row(
                Symbol="XQAD",
                Description="SAMPLE ISSUER AD NV",
                CUSIP="",
                ISIN="NL99971G1013",
                IssuerCountryCode="NL",
            ),
        )
    )
    assert [lot.symbol for lot in lots] == ["XQAT", "XQAK", "XQAD"]
    assert lots[2].issuer_country_code == "NL"


# ---- LOT-detail column requirements ----------------------------------------


@pytest.mark.parametrize(
    "missing_column",
    ["LevelOfDetail", "OpenDateTime", "HoldingPeriodDateTime"],
)
def test_rejects_legacy_27_col_trades_rows(missing_column: str) -> None:
    """If the caller passes rows from the legacy 27-col Trades schema
    (Closed Lots not enabled in the Flex Query), every required
    LOT-detail column will be absent. Reject with a pointer to the
    Flex Query configuration."""
    row = make_closed_lot_row()
    del row[missing_column]
    with pytest.raises(ParseError) as exc:
        parse_closed_lots([row])
    assert exc.value.column == missing_column
    assert "Closed Lots enabled" in exc.value.message
    assert exc.value.row_index == 0


# ---- LevelOfDetail validation ----------------------------------------------


def test_rejects_unknown_level_of_detail() -> None:
    """Unknown LevelOfDetail values (e.g., a future IBKR addition or
    a typo) must fail loudly. Silently dropping them would hide
    schema drift."""
    with pytest.raises(ParseError) as exc:
        parse_closed_lots([make_closed_lot_row(LevelOfDetail="ORDER")])
    assert exc.value.column == "LevelOfDetail"
    assert "ORDER" not in str(exc.value)
    assert exc.value.raw_value == ""
    assert "schema drift" in exc.value.message


def test_rejects_empty_level_of_detail() -> None:
    """An empty LevelOfDetail is a malformed row; not the same as
    EXECUTION (which has explicit 'EXECUTION'). Reject loudly."""
    with pytest.raises(ParseError) as exc:
        parse_closed_lots([make_closed_lot_row(LevelOfDetail="")])
    assert exc.value.column == "LevelOfDetail"


_KIND_MARKER = "Synthetic Marker Q"


@pytest.mark.parametrize("asset_class", ["NEW_ASSET", "OPT", "", HOLDER])
@pytest.mark.parametrize(
    "make_lot_detail_row",
    [make_closed_lot_row, make_execution_row],
    ids=["closed-lot", "execution"],
)
def test_rejects_a_row_that_is_not_a_stock(
    make_lot_detail_row: Callable[..., dict[str, str]], asset_class: str
) -> None:
    """#1093: the parser never read AssetClass, so another instrument's
    closed lot was read as a stock's, and an EXECUTION row was skipped
    unread. The neighbouring cells carry markers, and a closed lot's
    currency would refuse if it were read first."""
    row = make_lot_detail_row(
        AssetClass=asset_class, SubCategory=HOLDER, CurrencyPrimary=_KIND_MARKER
    )
    with pytest.raises(ParseError) as exc:
        parse_closed_lots([make_closed_lot_row(), row])
    error = exc.value
    assert (error.section, error.row_index, error.column, error.message, error.raw_value) == (
        "Trades",
        1,
        "AssetClass",
        "must be 'STK': v1 reads only stock rows, and would read any other instrument as a stock.",
        "",
    )
    assert_no_markers(str(error), HOLDER, _KIND_MARKER)


# ---- per-CLOSED_LOT-row validations ----------------------------------------


def test_rejects_non_usd_currency() -> None:
    with pytest.raises(ParseError) as exc:
        parse_closed_lots([make_closed_lot_row(CurrencyPrimary="EUR")])
    assert exc.value.column == "CurrencyPrimary"
    assert "USD only" in exc.value.message
    assert "issues/52" in exc.value.message


def test_rejects_pre_2020_open_date() -> None:
    with pytest.raises(ParseError) as exc:
        parse_closed_lots(
            [
                make_closed_lot_row(
                    OpenDateTime="20191231;000000",
                    HoldingPeriodDateTime="20191231;000000",
                )
            ]
        )
    assert exc.value.column == "OpenDateTime"
    assert "pre-2020" in exc.value.message.lower() or "2020-01-06" in exc.value.message


def test_rejects_open_date_one_day_before_archive_start() -> None:
    with pytest.raises(ParseError) as exc:
        parse_closed_lots(
            [
                make_closed_lot_row(
                    OpenDateTime="20200105;093000",
                    HoldingPeriodDateTime="20200105;093000",
                )
            ]
        )
    assert exc.value.column == "OpenDateTime"


def test_accepts_open_date_on_archive_start() -> None:
    [lot] = parse_closed_lots(
        with_closing_executions(
            make_closed_lot_row(
                OpenDateTime="20200106;093000",
                HoldingPeriodDateTime="20200106;093000",
            )
        )
    )
    assert lot.open_date_time == date(2020, 1, 6)


@pytest.mark.parametrize(
    ("holding", "holding_iso"),
    [("20260526;110000", "2026-05-26"), ("20260722;101500", "2026-07-22")],
)
def test_rejects_holding_period_date_differing_from_open_date(
    holding: str, holding_iso: str
) -> None:
    """#599: IBKR moves HoldingPeriodDateTime for US wash sales, and
    the lot's CostBasis may then carry the US disallowed-loss add-back.
    Flex can't show the actual cost, so the lot is refused in either
    direction: one synthetic holding date falls before the open date, the
    other after it. Since #640 the parser tags the lot BROKER_MATCHED with
    its holding date, and the engine refuses it in fixed text, without the
    row's Symbol, ISIN or dates (#1047)."""
    lots = parse_closed_lots(
        with_closing_executions(
            make_closed_lot_row(),
            make_closed_lot_row(OpenDateTime="20260721;101500", HoldingPeriodDateTime=holding),
        )
    )
    assert lots[1].basis_provenance is BasisProvenance.BROKER_MATCHED
    assert lots[1].holding_period_date_time == date.fromisoformat(holding_iso)
    with pytest.raises(BrokerBasisError) as exc:
        require_indian_basis(lots)
    message = str(exc.value)
    assert message == BROKER_BASIS_MESSAGE
    for value in ("XQAT", "US99990G1085", "2026-07-21", holding_iso, "20260721", holding[:8]):
        assert value.lower() not in message.lower()


def test_accepts_holding_period_on_open_date_with_different_time() -> None:
    """#599 compares calendar dates only; the ;HHMMSS time is ignored."""
    [lot] = parse_closed_lots(
        with_closing_executions(
            make_closed_lot_row(
                OpenDateTime="20260721;101500",
                HoldingPeriodDateTime="20260721;160000",
            )
        )
    )
    assert lot.open_date_time == date(2026, 7, 21)
    assert lot.holding_period_date_time == date(2026, 7, 21)
    require_indian_basis([lot])


@pytest.mark.parametrize(
    "field",
    [
        "ISIN",
        "Symbol",
        "OpenDateTime",
        "HoldingPeriodDateTime",
        "TradeDate",
        "CostBasis",
        "FifoPnlRealized",
        "Quantity",
    ],
)
def test_rejects_blank_required_field(field: str) -> None:
    with pytest.raises(ParseError) as exc:
        parse_closed_lots([make_closed_lot_row(**{field: ""})])
    assert exc.value.column == field


def test_rejects_malformed_open_date_format() -> None:
    """OpenDateTime is YYYYMMDD;HHMMSS — bare YYYYMMDD must be rejected."""
    with pytest.raises(ParseError) as exc:
        parse_closed_lots([make_closed_lot_row(OpenDateTime="20260721")])
    assert exc.value.column == "OpenDateTime"


def test_rejects_malformed_close_date_format() -> None:
    """TradeDate is bare YYYYMMDD — YYYYMMDD;HHMMSS must be rejected."""
    with pytest.raises(ParseError) as exc:
        parse_closed_lots([make_closed_lot_row(TradeDate="20260721;120000")])
    assert exc.value.column == "TradeDate"


def test_rejects_non_decimal_cost_basis() -> None:
    with pytest.raises(ParseError) as exc:
        parse_closed_lots([make_closed_lot_row(CostBasis="not-a-number")])
    assert exc.value.column == "CostBasis"


def test_rejects_non_decimal_fifo_pnl() -> None:
    with pytest.raises(ParseError) as exc:
        parse_closed_lots([make_closed_lot_row(FifoPnlRealized="bad")])
    assert exc.value.column == "FifoPnlRealized"


def test_rejects_malformed_isin_shape() -> None:
    """#150: a malformed ISIN (wrong length, bad characters) is
    rejected at parse time via
    :func:`ibkr_fa.parser._isin.validate_isin_or_raise`, with row
    context — previously slipped past the empty-check into
    ``ClosedLot.__post_init__``'s contextless InvariantError.
    """
    with pytest.raises(ParseError) as exc:
        parse_closed_lots([make_closed_lot_row(ISIN="US-78331005")])
    assert exc.value.section == "Trades"
    assert exc.value.column == "ISIN"
    assert "ISO 6166" in exc.value.message
    assert "issues/150" in exc.value.message


def test_normalises_lowercase_isin_to_uppercase() -> None:
    """#150 (PR #146 Tier-5 P2 pattern): a lowercase ISIN cell is
    case-normalised at parse time, so the resulting ClosedLot has the
    canonical uppercase value — pre-empts silent mismatch at
    downstream aggregation time.
    """
    [lot] = parse_closed_lots(
        with_closing_executions(
            make_closed_lot_row(
                Symbol="XQAQ",
                Description="SAMPLE ISSUER AQ INC",
                CUSIP="99992A109",
                ISIN="us99992a1097",
            )
        )
    )
    assert lot.isin == "US99992A1097"


@pytest.mark.parametrize("bad_value", ["NaN", "Infinity", "-Infinity", "sNaN"])
def test_rejects_non_finite_decimal_cost_basis(bad_value: str) -> None:
    """Tier-1 PR #136 r2 review: Decimal accepts NaN/Infinity as
    valid syntax but those values escape the ParseError contract
    via raw InvalidOperation in downstream arithmetic. _decimal()
    in this parser now rejects non-finite values up front (parallel
    guard to open_positions.py and activity_statement/trades.py)."""
    with pytest.raises(ParseError) as exc:
        parse_closed_lots([make_closed_lot_row(CostBasis=bad_value)])
    assert exc.value.column == "CostBasis"
    assert "finite" in exc.value.message.lower()


def test_first_offending_row_aborts_batch() -> None:
    """Even if a later row is also bad, the FIRST one wins — fail-fast
    semantic so users fix one problem at a time."""
    with pytest.raises(ParseError) as exc:
        parse_closed_lots(
            [
                make_closed_lot_row(),
                make_closed_lot_row(CurrencyPrimary="EUR"),
                make_closed_lot_row(ISIN=""),
            ]
        )
    assert exc.value.row_index == 1
    assert exc.value.column == "CurrencyPrimary"


# ---- Buy/Sell side filtering -----------------------------------------------


def test_rejects_short_cover_buy_closed_lot() -> None:
    """A CLOSED_LOT row with Buy/Sell='BUY' represents covering a
    short position. Short positions are liabilities, not Schedule FA
    Table A3 assets; the parser rejects them so the compute layer
    can assume every ClosedLot it sees is a long-side closure."""
    with pytest.raises(ParseError) as exc:
        parse_closed_lots([make_closed_lot_row(**{"Buy/Sell": "BUY"})])
    assert exc.value.column == "Buy/Sell"
    assert "short cover" in exc.value.message.lower()
    assert exc.value.raw_value == ""


def test_rejects_unknown_buy_sell_value() -> None:
    """Anything other than SELL/BUY indicates Flex Query schema drift."""
    with pytest.raises(ParseError) as exc:
        parse_closed_lots([make_closed_lot_row(**{"Buy/Sell": "WHATEVER"})])
    assert exc.value.column == "Buy/Sell"
    assert "WHATEVER" not in str(exc.value)
    assert exc.value.raw_value == ""


def test_execution_buy_rows_still_silently_dropped() -> None:
    """The Buy/Sell check only fires on CLOSED_LOT rows.
    EXECUTION rows with Buy/Sell='BUY' are normal (every long open is
    a BUY execution) and must continue to yield no closed lot."""
    lots = parse_closed_lots(
        [
            make_execution_row(**{"Buy/Sell": "BUY"}),
            *with_closing_executions(make_closed_lot_row()),
            make_execution_row(**{"Buy/Sell": "SELL"}),
        ]
    )
    assert len(lots) == 1


# ---- end-to-end against a Flex Query CSV dataset -------------------------

# A record whose first cell is one of these starts a new section.
_SECTION_FIRST_CELLS = frozenset({"CurrencyPrimary", "ClientAccountID"})


def _csv_closed_lot_rows(path: Path) -> list[dict[str, str]]:
    """The CLOSED_LOT rows of the file's LOT-detail Trades section, read
    with ``csv`` rather than the splitter: the non-blank records after
    the header with ``make_closed_lot_row``'s columns, up to the next known
    header in any column order or record whose first cell starts a section."""
    with path.open(newline="", encoding="utf-8-sig") as f:
        records = [r for r in csv.reader(f) if any(cell.strip() for cell in r)]
    columns = frozenset(make_closed_lot_row())
    starts = [i for i, record in enumerate(records) if frozenset(record) == columns]
    assert len(starts) <= 1  # the splitter refuses a repeated section
    rows: list[dict[str, str]] = []
    for record in records[starts[0] + 1 :] if starts else []:
        if record[0] in _SECTION_FIRST_CELLS or frozenset(record) in FLEX_SECTION_KINDS:
            break
        rows.append(dict(zip(records[starts[0]], record, strict=True)))
    return [row for row in rows if row["LevelOfDetail"] == "CLOSED_LOT"]


def test_csv_closed_lot_reader_stops_at_reordered_next_header(tmp_path: Path) -> None:
    header, row = csv.reader([TRADES_LOT_DETAIL_HEADER, TRADES_CLOSED_LOT_XQAT])
    next_header, next_row = csv.reader([OPEN_POSITIONS_HEADER, OPEN_POSITIONS_XQAW])
    index = next_header.index("ISIN")
    path = tmp_path / "sections.csv"
    with path.open("w", newline="", encoding="utf-8") as target:
        csv.writer(target).writerows(
            [
                header,
                row,
                next_header[index:] + next_header[:index],
                next_row[index:] + next_row[:index],
            ]
        )
    assert set(split_sections(path)) == {KnownSection.TRADES, KnownSection.OPEN_POSITIONS_LOT}
    assert _csv_closed_lot_rows(path) == [dict(zip(header, row, strict=True))]


def _flex_date(cell: str) -> date:
    """The calendar date of a ``YYYYMMDD`` or ``YYYYMMDD;HHMMSS`` cell."""
    return date(int(cell[:4]), int(cell[4:6]), int(cell[6:8]))


def _lot_fields(lot: ClosedLot) -> tuple[object, ...]:
    return (
        lot.currency_primary,
        lot.sub_category,
        lot.symbol,
        lot.description,
        lot.isin,
        lot.listing_exchange,
        lot.issuer_country_code,
        lot.quantity,
        lot.cost_basis_money,
        lot.proceeds_money,
        lot.open_date_time,
        lot.holding_period_date_time,
        lot.close_date_time,
    )


def _row_fields(row: dict[str, str]) -> tuple[object, ...]:
    return (
        row["CurrencyPrimary"],
        row["SubCategory"],
        row["Symbol"],
        row["Description"],
        row["ISIN"].strip().upper(),
        row["ListingExchange"],
        row["IssuerCountryCode"].strip().upper(),
        Decimal(row["Quantity"]),
        Decimal(row["CostBasis"]),
        Decimal(row["CostBasis"]) + Decimal(row["FifoPnlRealized"]),
        _flex_date(row["OpenDateTime"]),
        _flex_date(row["HoldingPeriodDateTime"]),
        _flex_date(row["TradeDate"]),
    )


@private_test
def test_works_against_flex_query_closed_lots_csv() -> None:
    """End-to-end against ``ibkr/flex-query-closed-lots.csv`` (skips when
    absent), checking only properties relative to the file itself. The
    file either splits and parses or refuses with a ParseError. When it
    parses, each lot equals its CLOSED_LOT row read with ``csv``, and the
    #599 engine check refuses exactly when one of those rows has a
    HoldingPeriodDateTime date other than its OpenDateTime date. The
    synthetic test_rejects_holding_period_date_differing_from_open_date
    pins that refusal."""
    [sample] = private_dataset("ibkr/flex-query-closed-lots.csv")
    try:
        section = split_sections(sample).get(KnownSection.TRADES)
        lots = parse_closed_lots([] if section is None else section.rows)
    except ParseError:
        return
    rows = _csv_closed_lot_rows(sample)
    assert [_lot_fields(lot) for lot in lots] == [_row_fields(row) for row in rows]
    if any(
        _flex_date(row["HoldingPeriodDateTime"]) != _flex_date(row["OpenDateTime"]) for row in rows
    ):
        with pytest.raises(BrokerBasisError, match="HoldingPeriodDateTime"):
            require_indian_basis(lots)
    else:
        require_indian_basis(lots)


# ---- #147 IN-prefix rejection + parse-time lookup_country defence -------


def test_rejects_indian_issuer_country_code() -> None:
    """#147: an IN-prefixed IssuerCountryCode on a CLOSED_LOT row is
    refused at parse time. The engine wouldn't refuse it: IN is in the
    CBDT table, so CG would only set the lot aside as Indian-source and
    FA A3 would give it a row.
    """
    row = make_closed_lot_row(IssuerCountryCode="IN")
    with pytest.raises(ParseError) as exc:
        parse_closed_lots([row])
    assert exc.value.section == "Trades"
    assert exc.value.column == "IssuerCountryCode"
    assert "'IN' (India)" in exc.value.message
    assert "FOREIGN" in exc.value.message
    assert exc.value.raw_value == ""


def test_rejects_unmapped_issuer_country_code() -> None:
    """#147: an ISO-valid country code not in the bundled CBDT table
    is rejected at parse time with the correct actionable hint."""
    row = make_closed_lot_row(IssuerCountryCode="ZZ")
    with pytest.raises(ParseError) as exc:
        parse_closed_lots([row])
    assert exc.value.column == "IssuerCountryCode"
    assert "ZZ" not in str(exc.value)
    assert "itr_country_codes.csv" in exc.value.message


def test_accepts_valid_non_us_issuer_country_code() -> None:
    """#147 sanity inverse: a mapped non-US country (CA) parses
    cleanly through the new guard."""
    row = make_closed_lot_row(
        Symbol="XQAL",
        Description="SAMPLE ISSUER AL INC",
        CUSIP="99991A100",
        IssuerCountryCode="CA",
        ISIN="CA99991A1003",
    )
    [lot] = parse_closed_lots(with_closing_executions(row))
    assert lot.issuer_country_code == "CA"


def test_rejects_lowercase_indian_issuer_country_code() -> None:
    """T1-correctness PR #169 P3-1: lowercase 'in' is normalised to
    uppercase before the IN-guard."""
    row = make_closed_lot_row(IssuerCountryCode="in")
    with pytest.raises(ParseError) as exc:
        parse_closed_lots([row])
    assert exc.value.column == "IssuerCountryCode"
    assert "'IN' (India)" in exc.value.message


def test_rejects_empty_issuer_country_code() -> None:
    """T1-correctness PR #169 P3-2: empty IssuerCountryCode caught by
    the _non_empty guard, not mis-routed through unmapped-country."""
    row = make_closed_lot_row(IssuerCountryCode="")
    with pytest.raises(ParseError) as exc:
        parse_closed_lots([row])
    assert exc.value.column == "IssuerCountryCode"
    assert "non-empty" in exc.value.message


def test_rejects_whitespace_only_issuer_country_code() -> None:
    """T2 PR #169 P3: whitespace-only IssuerCountryCode caught by
    the non-empty check after normalisation."""
    row = make_closed_lot_row(IssuerCountryCode="   ")
    with pytest.raises(ParseError) as exc:
        parse_closed_lots([row])
    assert exc.value.column == "IssuerCountryCode"
    assert "non-empty" in exc.value.message


def test_rejects_padded_indian_issuer_country_code() -> None:
    """T2 PR #169 P3: symmetric coverage with open_positions for
    padded IN value."""
    row = make_closed_lot_row(IssuerCountryCode="  IN  ")
    with pytest.raises(ParseError) as exc:
        parse_closed_lots([row])
    assert exc.value.column == "IssuerCountryCode"
    assert "'IN' (India)" in exc.value.message


# ---- #794 IssuerCountryCode must equal the ISIN prefix -------------------


@pytest.mark.parametrize("issuer_country", ["CA", "ca", "  CA  "])
def test_refuses_issuer_country_differing_from_isin_prefix(issuer_country: str) -> None:
    """#794: the CLOSED_LOT twin of the Open Positions refusal. The bad
    row follows an EXECUTION row and a valid CLOSED_LOT row, so the error
    names its index in the Trades section."""
    rows = [
        make_execution_row(),
        make_closed_lot_row(),
        make_closed_lot_row(
            Symbol="XQAQ",
            Description="SAMPLE ISSUER AQ INC",
            CUSIP="99992A109",
            ISIN="US99992A1097",
            IssuerCountryCode=issuer_country,
        ),
    ]
    with pytest.raises(ParseError) as exc:
        parse_closed_lots(rows)
    assert exc.value.section == "Trades"
    assert exc.value.row_index == 2
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
    [lot] = parse_closed_lots(
        with_closing_executions(
            make_closed_lot_row(
                Symbol="XQAQ",
                Description="SAMPLE ISSUER AQ INC",
                CUSIP="99992A109",
                ISIN=isin,
                IssuerCountryCode=issuer_country,
            )
        )
    )
    assert lot.isin == "US99992A1097"
    assert lot.issuer_country_code == "US"


# ---- #1286: the closed lots account for every closing execution ----------

# Neutral markers for the sold security's symbol, description and ISIN.
_MARKERS = ("QZ86SOLD", "QZ86 SOLD CORP", "US99986Q1022")
_MARKED = {
    "Symbol": "QZ86SOLD",
    "Description": "QZ86 SOLD CORP",
    "CUSIP": "99986Q102",
    "ISIN": "US99986Q1022",
}
_UNMATCHED = (
    "the CLOSED_LOT quantities for this row's security don't add up to its closing "
    "EXECUTION quantities, so a holding sold during the year can't be checked against "
    "its closed lots. Re-run the Flex Query over the whole calendar year with the "
    "Closed Lots checkbox in the Trades section turned on."
)
_INDICATOR = (
    "unrecognised Open/CloseIndicator on an EXECUTION row; expected 'O' (opening) or 'C' (closing)."
)


def _sold(**overrides: str) -> dict[str, str]:
    """A CLOSED_LOT row of the marked security (12 shares unless overridden)."""
    return make_closed_lot_row(**{**_MARKED, **overrides})


def _sale(quantity: str, **overrides: str) -> dict[str, str]:
    """A closing EXECUTION row of the marked security for ``quantity`` shares."""
    return closing_execution_for(_sold(Quantity=quantity, **overrides))


def _assert_refusal(
    rows: list[dict[str, str]],
    row_index: int,
    column: str,
    message: str,
    *markers: str,
) -> ParseError:
    """``rows`` refuse with exactly this error, with no marker in its graph."""
    with pytest.raises(ParseError) as raised:
        parse_closed_lots(rows)
    error = raised.value
    assert error == ParseError(
        section="Trades", row_index=row_index, column=column, message=message, raw_value=""
    )
    assert str(error) == (
        f"ParseError in section 'Trades' at row {row_index}, column {column!r}: {message}"
    )
    assert_prints_no_marker(error, markers=(*_MARKERS, *markers))
    return error


# 99999999999.99999999999999999999 + 0.00000000000000000002 is
# 100000000000.00000000000000000001: 32 digits, which the default 28-digit
# decimal context would round to the sale's 100000000000.
_BIG = "99999999999.99999999999999999999"
_TINY = "0.00000000000000000002"


@pytest.mark.parametrize(
    ("rows", "row_index"),
    [
        pytest.param([make_execution_row(), _sale("12")], 1, id="sale-without-closed-lot"),
        pytest.param([make_execution_row(), _sold()], 1, id="closed-lot-without-sale"),
        pytest.param([_sale("12"), _sold(), _sold(Quantity="1")], 0, id="closed-lots-past-sale"),
        pytest.param([_sale("12"), _sold(Quantity="11")], 0, id="closed-lots-short-of-sale"),
        pytest.param(
            [*with_closing_executions(make_closed_lot_row()), _sale("12")],
            2,
            id="after-a-matched-security",
        ),
        pytest.param(
            [_sale("12", **{"Buy/Sell": "BUY"}) | {"Quantity": "12"}],
            0,
            id="short-cover-without-closed-lot",
        ),
        pytest.param(
            [_sale("100000000000"), _sold(Quantity=_BIG), _sold(Quantity=_TINY)],
            0,
            id="past-28-digits",
        ),
    ],
)
def test_refuses_closed_lots_that_dont_add_up_to_the_closing_executions(
    rows: list[dict[str, str]], row_index: int
) -> None:
    """#1286: Table A3 lists a holding sold during the year only through its
    CLOSED_LOT rows, so each ISIN's closed-lot quantities must add up exactly
    to its closing executions' quantities without their signs. A mismatch
    either way refuses at the ISIN's first closing EXECUTION or CLOSED_LOT
    row with fixed text."""
    assert_replaced_unchained(_assert_refusal(rows, row_index, "Quantity", _UNMATCHED))


@pytest.mark.parametrize(
    "rows",
    [
        pytest.param([_sale("12"), _sold(Quantity="5"), _sold(Quantity="7")], id="split-lots"),
        pytest.param([_sale("5"), _sale("7"), _sold()], id="split-sales"),
        pytest.param([_sold(), _sale("12")], id="closed-lot-first"),
        pytest.param([_sale("12", ISIN="us99986q1022"), _sold()], id="lowercase-isin"),
        pytest.param(
            [_sale(f"100000000000.{'0' * 19}1"), _sold(Quantity=_BIG), _sold(Quantity=_TINY)],
            id="past-28-digits",
        ),
        pytest.param(
            [make_execution_row(ISIN="", Quantity="QZ86QTY"), *with_closing_executions(_sold())],
            id="opening-cells-unread",
        ),
    ],
)
def test_accepts_closed_lots_that_add_up_to_the_closing_executions(
    rows: list[dict[str, str]],
) -> None:
    """#1286 inverse: the totals compare per ISIN, whatever the split or order
    of the rows; an opening execution's ISIN and Quantity are never read."""
    lots = parse_closed_lots(rows)
    closed = [row for row in rows if row["LevelOfDetail"] == "CLOSED_LOT"]
    assert [lot.quantity for lot in lots] == [Decimal(row["Quantity"]) for row in closed]
    assert {lot.isin for lot in lots} == {"US99986Q1022"}


@pytest.mark.parametrize("indicator", ["", "c", "o", "C;O", "O;C", " C", "QZ86IND"])
def test_refuses_an_execution_indicator_other_than_o_or_c(indicator: str) -> None:
    """#1286: an EXECUTION row's Open/CloseIndicator must be exactly 'O' or
    'C'; any other value, a combined form included, refuses without it."""
    rows = [
        *with_closing_executions(_sold()),
        make_execution_row(**_MARKED, **{"Open/CloseIndicator": indicator}),
    ]
    markers = ("QZ86IND",) if indicator == "QZ86IND" else ()
    assert_replaced_unchained(_assert_refusal(rows, 2, "Open/CloseIndicator", _INDICATOR, *markers))


@pytest.mark.parametrize(
    ("cells", "column", "message"),
    [
        pytest.param(
            {"ISIN": ""},
            "ISIN",
            "ISIN column 'ISIN' is empty; expected a 12-character ISO 6166 ISIN (2 uppercase "
            "letters + 9 alphanumerics + 1 digit, e.g., 'US9999011011').",
            id="blank-isin",
        ),
        pytest.param({"Quantity": ""}, "Quantity", "must be non-empty", id="blank-quantity"),
        pytest.param({"Quantity": "QZ86QTY"}, "Quantity", "must be a decimal number", id="text"),
        pytest.param({"Quantity": "-NaN"}, "Quantity", "must be a finite decimal", id="nan"),
    ],
)
def test_refuses_a_closing_execution_it_cant_read(
    cells: dict[str, str], column: str, message: str
) -> None:
    """#1286: a closing execution's ISIN and Quantity are read, so a cell that
    can't be read refuses with the parser's fixed text and no value anywhere,
    as a CLOSED_LOT cell does."""
    rows = [*with_closing_executions(make_closed_lot_row()), {**_sale("12"), **cells}, _sold()]
    markers = ("QZ86QTY",) if cells.get("Quantity") == "QZ86QTY" else ()
    _assert_refusal(rows, 2, column, message, *markers)


@pytest.mark.parametrize(
    ("rows", "column"),
    [
        pytest.param(
            [make_execution_row(**{"Open/CloseIndicator": "C;O"}), _sold(CurrencyPrimary="EUR")],
            "CurrencyPrimary",
            id="currency-after-bad-indicator",
        ),
        pytest.param(
            [_sale("12", ISIN=""), _sold(**{"Buy/Sell": "BUY"})],
            "Buy/Sell",
            id="short-cover-after-unreadable-sale",
        ),
        pytest.param(
            [_sold(), make_execution_row(AssetClass="OPT")],
            "AssetClass",
            id="asset-class-after-unmatched-lot",
        ),
        pytest.param(
            [_sale("12"), {k: v for k, v in _sold().items() if k != "OpenDateTime"}],
            "OpenDateTime",
            id="legacy-row-after-unmatched-sale",
        ),
    ],
)
def test_existing_refusals_still_come_first(rows: list[dict[str, str]], column: str) -> None:
    """#1286 runs its checks only after every row has passed the per-row
    checks, so an input that refused before still refuses at the same row
    and column, even when it also breaks a #1286 rule earlier in the file."""
    with pytest.raises(ParseError) as raised:
        parse_closed_lots(rows)
    assert (raised.value.row_index, raised.value.column) == (1, column)
