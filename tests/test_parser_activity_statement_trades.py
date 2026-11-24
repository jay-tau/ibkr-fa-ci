"""Tests for :mod:`ibkr_fa.parser.activity_statement.trades`."""

from __future__ import annotations

import csv
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path

import pytest

from ibkr_fa.compute.lot_matching import match_fifo
from ibkr_fa.exceptions import ParseError
from ibkr_fa.models import ExecutionEvent, ExecutionKind, OpenLot, Side
from ibkr_fa.parser.activity_statement import (
    FinancialInstrument,
    KnownActivitySection,
    build_dec31_close_prices,
    build_instrument_lookup,
    parse_activity_statement_trades,
    parse_statement_period,
    split_activity_statement_sections,
    validate_reconciliation,
)
from ibkr_fa.types import CountryCode, Isin, Ticker
from tests._activity_statement_rows import RECONCILIATION_REFUSAL as _RECONCILIATION
from tests._activity_statement_rows import ZERO_QUANTITY_REFUSAL as _ZERO_QUANTITY
from tests._activity_statement_rows import fifo_open_lots as _open_lots
from tests._activity_statement_rows import make_fii_row, make_trade_row
from tests._activity_statement_rows import open_position_close_row as _make_op_summary_row
from tests._activity_statement_rows import open_position_lot_row as _lot
from tests._activity_statement_rows import open_position_row as _make_op_row
from tests._crash_reports import assert_unchained_refusal
from tests._no_echo import ACCOUNT, CUSIP, HOLDER, MARKERS, TICKER, assert_no_markers
from tests._number_limits import QUANTITY
from tests._private_checks import private_test
from tests._private_data import private_dataset
from tests._statement_period import ALL_DATES

# ---- Financial Instrument lookup ------------------------------------------


def test_build_instrument_lookup_basic() -> None:
    rows = [make_fii_row()]
    lookup = build_instrument_lookup(rows)
    assert len(lookup) == 1
    inst = lookup[Ticker("XQAW")]
    assert isinstance(inst, FinancialInstrument)
    assert inst.symbol == Ticker("XQAW")
    assert inst.description == "SAMPLE ISSUER AW INC"
    assert inst.isin == Isin("US99990E1038")
    assert inst.listing_exchange == "NASDAQ"
    assert inst.asset_class == "Stocks"


def test_build_instrument_lookup_uppercases_lowercase_isin() -> None:
    """#150: ``build_instrument_lookup`` canonicalises a lowercase Security ID
    to an uppercase ISIN at parse time via
    :func:`ibkr_fa.parser._isin.validate_isin_or_raise`.
    """
    rows = [
        make_fii_row(symbol="XQAL", description="SAMPLE ISSUER AL INC", security_id="ca99991a1003")
    ]
    lookup = build_instrument_lookup(rows)
    inst = lookup[Ticker("XQAL")]
    assert inst.isin == Isin("CA99991A1003")


def test_build_instrument_lookup_rejects_empty_symbol() -> None:
    rows = [make_fii_row(symbol="")]
    with pytest.raises(ParseError) as exc:
        build_instrument_lookup(rows)
    assert exc.value.column == "Symbol"


def test_build_instrument_lookup_rejects_empty_security_id() -> None:
    """ISIN is required for the FA-A3 output, so a missing
    Security ID at this step must fail loudly rather than
    propagate as an empty Isin into FaA3Row."""
    rows = [make_fii_row(security_id="")]
    with pytest.raises(ParseError) as exc:
        build_instrument_lookup(rows)
    assert exc.value.column == "Security ID"


def test_build_instrument_lookup_rejects_duplicate_symbol() -> None:
    """Two Financial Instrument rows for the same Symbol with
    different ISINs would silently overwrite. Refuse — same class
    of bug as the duplicate-field check in account_info.py."""
    rows = [
        make_fii_row(symbol="XQAW", security_id="US99990E1038"),
        make_fii_row(symbol="XQAW", security_id="US9999999999"),
    ]
    with pytest.raises(ParseError) as exc:
        build_instrument_lookup(rows)
    assert "more than once" in exc.value.message
    assert exc.value.column == "Symbol"


def test_build_instrument_lookup_rejects_missing_required_column() -> None:
    """Defence against IBKR schema drift — a row missing a column
    we need must raise with that column name."""
    rows = [{k: v for k, v in make_fii_row().items() if k != "Security ID"}]
    with pytest.raises(ParseError) as exc:
        build_instrument_lookup(rows)
    assert exc.value.column == "Security ID"


def test_build_instrument_lookup_handles_empty_input() -> None:
    assert build_instrument_lookup([]) == {}


# ---- Code validation ------------------------------------------------------


@pytest.mark.parametrize(
    "code",
    [
        "O",
        "O;RP",
        "FP;O",
        "O;FP",
        "O;RPA",
        "O;P",
        "O;IPO",
        "O;PI",
        "O;M",
        "O;SI;SF",
        "IA;O",  # RPL-tracked wash-sale venue code; basis is actual cost (#265)
        "IM;O",  # mixed-venue RPL-tracked wash sale; basis is actual cost (#265)
    ],
)
def test_code_allow_list_accepts_known_lot_neutral_combinations(code: str) -> None:
    """Every token in the documented allow-list must pass."""
    instruments = build_instrument_lookup([make_fii_row()])
    [event] = parse_activity_statement_trades(
        [make_trade_row(code=code)],
        instruments=instruments,
        period=ALL_DATES,
    )
    assert event.kind is ExecutionKind.OPEN


def test_empty_code_rejected_as_no_direction_token() -> None:
    """Post-#104: every Trades row must carry exactly one direction
    token (``O`` or ``C``). An empty Code cell carries zero, which
    is malformed input — the parser refuses rather than guess
    whether the row opens or closes."""
    instruments = build_instrument_lookup([make_fii_row()])
    with pytest.raises(ParseError) as exc:
        parse_activity_statement_trades(
            [make_trade_row(code="")],
            instruments=instruments,
            period=ALL_DATES,
        )
    assert exc.value.column == "Code"
    assert "direction token" in exc.value.message


def test_closure_negative_t_price_rejected() -> None:
    """#348: a closing trade with NEGATIVE T.Price is malformed (a sale
    price cannot be negative); refuse. Zero is allowed (see the
    worthless-removal test) — only a negative price is refused."""
    instruments = build_instrument_lookup([make_fii_row()])
    open_row = make_trade_row(quantity="10", basis="412.50")
    close_row = make_trade_row(
        date_time="2025-09-15, 09:30:00",
        quantity="-10",
        trade_price="-1",
        basis="-412.50",
        code="C",
    )
    close_row["Realized P/L"] = "-412.50"
    with pytest.raises(ParseError, match=r"T\. Price"):
        parse_activity_statement_trades(
            [open_row, close_row],
            instruments=instruments,
            period=ALL_DATES,
        )


def test_code_with_both_o_and_c_tokens_rejected_as_malformed() -> None:
    """A single row carrying both O and C direction tokens is
    malformed (a single execution cannot simultaneously open and
    close a position)."""
    instruments = build_instrument_lookup([make_fii_row()])
    with pytest.raises(ParseError) as exc:
        parse_activity_statement_trades(
            [make_trade_row(code="O;C")],
            instruments=instruments,
            period=ALL_DATES,
        )
    assert exc.value.column == "Code"
    assert "direction token" in exc.value.message


@pytest.mark.parametrize(
    ("code", "expected_issue"),
    [
        ("Adj", "issues/106"),  # corporate action
        ("De", "issues/106"),
        ("Ex", "issues/106"),
        ("XCH", "issues/106"),
        ("ETF", "issues/106"),
        ("T", "issues/107"),  # transfer
        ("I", "issues/107"),
        ("HC", "issues/107"),  # tax-basis election
        ("LI", "issues/107"),
        ("SL", "issues/107"),
        ("UNKNOWN-FUTURE", "issues/107"),  # generic unknown
    ],
)
def test_disallowed_codes_rejected_with_actionable_message(code: str, expected_issue: str) -> None:
    """Every non-allow-listed code raises with a message pointing
    at the relevant tracking issue so the user knows what's
    blocking them.

    NB: ``LD`` (wash-sale loss-disallowed) is NOT in this list — it is
    refused (India needs actual cost, not IBKR's US wash-sale-adjusted
    basis). See ``test_ld_wash_sale_code_refused_as_us_adjusted_basis``
    and #265."""
    instruments = build_instrument_lookup([make_fii_row()])
    with pytest.raises(ParseError) as exc:
        parse_activity_statement_trades(
            [make_trade_row(code=code)],
            instruments=instruments,
            period=ALL_DATES,
        )
    assert exc.value.column == "Code"
    # Fixed text: a Code cell can hold a shifted name (#1039).
    assert exc.value.message == _UNRECOGNISED_TOKEN
    assert exc.value.raw_value == ""
    # The error links all five issues (#101, #104, #106, #107, #265) so the
    # user can find the right one regardless of which specific code they hit.
    assert expected_issue in exc.value.message


# ---- Event construction ---------------------------------------------------


def test_open_event_field_mapping() -> None:
    """Every ExecutionEvent field must be populated from the right
    Activity Statement column."""
    instruments = build_instrument_lookup([make_fii_row()])
    [event] = parse_activity_statement_trades(
        [
            make_trade_row(
                symbol="XQAW",
                date_time="2025-06-15, 10:20:35",
                quantity="10",
                trade_price="41.25",
                close_price="45.60",
                basis="412.50",
            )
        ],
        instruments=instruments,
        period=ALL_DATES,
    )
    assert event == ExecutionEvent(
        currency_primary="USD",
        symbol=Ticker("XQAW"),
        description="SAMPLE ISSUER AW INC",
        isin=Isin("US99990E1038"),
        listing_exchange="NASDAQ",
        issuer_country_code=CountryCode("US"),
        kind=ExecutionKind.OPEN,
        side=Side.LONG,
        # The full Date/Time, so the matcher can order same-day trades.
        trade_date_time=datetime(2025, 6, 15, 10, 20, 35),
        row_index=0,
        quantity=Decimal("10"),
        acquisition_cost=Decimal("412.50"),
        net_proceeds=None,
        trade_price=None,
    )


def test_closing_row_emits_close_event_with_net_proceeds() -> None:
    """A ``C`` row carries net proceeds ``|Basis| + Realized P/L`` and
    its ``T. Price``, but no cost: the broker's close-side ``Basis``
    (206.25 here) never reaches the matcher, which costs each slice from
    the matched open instead."""
    instruments = build_instrument_lookup([make_fii_row()])
    close_row = make_trade_row(
        date_time="2025-09-15, 09:30:00",
        quantity="-5",
        trade_price="44.80",
        basis="-206.25",  # 5 * 41.25
        code="C",
    )
    close_row["Realized P/L"] = "17.75"  # 5 * 44.80 - 206.25 = 224.00 - 206.25
    [event] = parse_activity_statement_trades(
        [close_row], instruments=instruments, period=ALL_DATES
    )
    assert event.kind is ExecutionKind.CLOSE
    assert event.side is Side.LONG
    assert event.trade_date_time == datetime(2025, 9, 15, 9, 30)
    assert event.quantity == Decimal("5")
    assert event.acquisition_cost is None
    assert event.net_proceeds == Decimal("224.00")  # 206.25 + 17.75
    assert event.trade_price == Decimal("44.80")


def test_fractional_quantity_handled() -> None:
    """IBKR fractional shares come through as Decimal quantities
    like '0.35'; no truncation to integer."""
    instruments = build_instrument_lookup([make_fii_row()])
    [event] = parse_activity_statement_trades(
        [make_trade_row(quantity="0.35", basis="14.4375")],  # 0.35 * 41.25
        instruments=instruments,
        period=ALL_DATES,
    )
    assert event.quantity == Decimal("0.35")


def test_input_order_preserved() -> None:
    """Events come out in the same order as input rows, each with
    its row index; the matcher's ordering relies on both."""
    instruments = build_instrument_lookup(
        [
            make_fii_row(symbol="AAA", security_id="US99971J1050"),
            make_fii_row(symbol="BBB", security_id="US99971J1051"),
            make_fii_row(symbol="CCC", security_id="US99971J1052"),
        ]
    )
    events = parse_activity_statement_trades(
        [
            make_trade_row(symbol="CCC", date_time="2025-01-01, 09:00:00"),
            make_trade_row(symbol="AAA", date_time="2025-02-01, 09:00:00"),
            make_trade_row(symbol="BBB", date_time="2025-03-01, 09:00:00"),
        ],
        instruments=instruments,
        period=ALL_DATES,
    )
    assert [(str(event.symbol), event.row_index) for event in events] == [
        ("CCC", 0),
        ("AAA", 1),
        ("BBB", 2),
    ]


def test_empty_input_returns_empty_list() -> None:
    instruments = build_instrument_lookup([])
    assert parse_activity_statement_trades([], instruments=instruments, period=ALL_DATES) == []


# ---- ISIN join via Financial Instrument Information -----------------------


def test_symbol_not_in_instrument_lookup_raises() -> None:
    """Without the FII row, we can't get ISIN. Refuse loudly."""
    instruments: dict[Ticker, FinancialInstrument] = {}
    with pytest.raises(ParseError) as exc:
        parse_activity_statement_trades(
            [make_trade_row()],
            instruments=instruments,
            period=ALL_DATES,
        )
    assert exc.value.column == "Symbol"
    assert "Financial Instrument Information" in exc.value.message


# ---- Date/Time parsing ----------------------------------------------------


def test_date_time_with_comma_space_separator() -> None:
    """Activity Statement uses 'YYYY-MM-DD, HH:MM:SS' (with comma
    AND space). Both must be required exactly."""
    instruments = build_instrument_lookup([make_fii_row()])
    [event] = parse_activity_statement_trades(
        [make_trade_row(date_time="2026-02-10, 14:25:50")],
        instruments=instruments,
        period=ALL_DATES,
    )
    assert event.trade_date_time == datetime(2026, 2, 10, 14, 25, 50)


@pytest.mark.parametrize(
    "bad_date_time",
    [
        "2025-06-15",  # missing time entirely
        "2025-06-15 09:30:00",  # space, no comma
        "2025-06-15,09:30:00",  # comma, no space
        "2025/06/15, 09:30:00",  # wrong date separator
        "06/15/2025, 09:30:00",  # US-style date
        "",  # empty
        "garbage",
        # #915: loose ISO shapes and non-ASCII digits must fail the shape check.
        "2025-06-15, 09:30:00+00:00",  # timezone offset
        "2025-06-15, 09:30:00Z",  # UTC suffix
        "2025-06-15, 09:30",  # no seconds
        "2025-06-15, 0930",  # basic-format time
        "2025-06-15, 09:30:00.5",  # fractional seconds
        "20250615, 09:30:00",  # basic-format date
        "2025-06-15, ٠٩:30:00",  # Arabic-Indic digits for the hour
    ],
)
def test_invalid_date_time_format_raises(bad_date_time: str) -> None:
    instruments = build_instrument_lookup([make_fii_row()])
    with pytest.raises(ParseError) as exc:
        parse_activity_statement_trades(
            [make_trade_row(date_time=bad_date_time)],
            instruments=instruments,
            period=ALL_DATES,
        )
    assert exc.value.column == "Date/Time"
    # The shape check refuses before the calendar-date and clock-time checks.
    assert "'YYYY-MM-DD, HH:MM:SS' format" in exc.value.message


def test_aware_open_and_plain_close_raise_parse_error_not_type_error() -> None:
    """#915: an opening trade with a timezone suffix used to parse as an aware
    datetime, and FIFO matching then raised ``TypeError`` comparing it with the
    plain closing trade."""
    instruments = build_instrument_lookup([make_fii_row()])
    rows = [
        make_trade_row(date_time="2025-06-15, 09:30:00+00:00"),
        make_trade_row(
            date_time="2025-09-15, 10:00:00",
            quantity="-5",
            trade_price="44.80",
            basis="-206.25",
            code="C",
        ),
    ]
    with pytest.raises(ParseError) as exc:
        _open_lots(rows, instruments=instruments)
    assert (exc.value.row_index, exc.value.column) == (0, "Date/Time")


def test_real_date_rejects_feb_30() -> None:
    """Syntactically valid YYYY-MM-DD but Feb 30 isn't real."""
    instruments = build_instrument_lookup([make_fii_row()])
    with pytest.raises(ParseError) as exc:
        parse_activity_statement_trades(
            [make_trade_row(date_time="2025-02-30, 09:30:00")],
            instruments=instruments,
            period=ALL_DATES,
        )
    assert "calendar date" in exc.value.message


def test_real_time_rejects_25_99_99() -> None:
    """The time component is also validated."""
    instruments = build_instrument_lookup([make_fii_row()])
    with pytest.raises(ParseError) as exc:
        parse_activity_statement_trades(
            [make_trade_row(date_time="2025-06-15, 25:99:99")],
            instruments=instruments,
            period=ALL_DATES,
        )
    assert "wall-clock time" in exc.value.message


# ---- malformed numeric input ----------------------------------------------


@pytest.mark.parametrize(
    "field",
    ["Quantity", "T. Price", "Basis"],
)
def test_non_decimal_numeric_raises(field: str) -> None:
    """Non-numeric value in any *used* decimal column raises with
    that column's name. C. Price is intentionally NOT validated here
    (issue #133 fix): the parser doesn't use its value — mark_price
    comes from Open Positions instead — so silently accepting a
    malformed C. Price is preferable to failing on an unused field."""
    instruments = build_instrument_lookup([make_fii_row()])
    row = make_trade_row()
    row[field] = "not-a-number"
    with pytest.raises(ParseError) as exc:
        parse_activity_statement_trades(
            [row],
            instruments=instruments,
            period=ALL_DATES,
        )
    assert exc.value.column == field


# ---- DataDiscriminator guard ----------------------------------------------


def test_missing_required_column_raises_clean_parse_error() -> None:
    """Codex P2 on PR #111: an Activity Statement whose Trades section
    header is missing a required column (e.g., ``Basis``) would have
    raised ``KeyError`` from the parser's direct subscripts. That
    KeyError escapes the CLI's documented-error catch and becomes
    an unhandled traceback / exit code 1, instead of the documented
    :class:`ParseError` / exit code 4.

    The fix validates required keys at the top of the loop. This
    test verifies the validation fires for a missing column."""
    instruments = build_instrument_lookup([make_fii_row()])
    row = make_trade_row()
    del row["Basis"]
    with pytest.raises(ParseError) as exc:
        parse_activity_statement_trades(
            [row],
            instruments=instruments,
            period=ALL_DATES,
        )
    assert exc.value.column == "Basis"
    assert "missing from the" in exc.value.message
    assert "Trades row" in exc.value.message


def test_unexpected_data_discriminator_raises() -> None:
    """The splitter checks SubTotal/Total rows and never passes them on
    (#1090). If we see anything other than 'Order' here, the splitter's
    contract has been violated — refuse loudly."""
    instruments = build_instrument_lookup([make_fii_row()])
    row = make_trade_row()
    row["DataDiscriminator"] = "WeirdNewKind"
    with pytest.raises(ParseError) as exc:
        parse_activity_statement_trades(
            [row],
            instruments=instruments,
            period=ALL_DATES,
        )
    assert exc.value.column == "DataDiscriminator"


# ---- USD / US-only v1 enforcement -----------------------------------------


def test_non_usd_trade_currency_rejected() -> None:
    """Codex P1 on PR #108: a EUR/GBP-denominated trade would carry
    its currency into OpenLot but the FA compute layer is USD-only.
    Reject up front matching the same check in the Flex parser."""
    instruments = build_instrument_lookup([make_fii_row()])
    row = make_trade_row()
    row["Currency"] = "EUR"
    with pytest.raises(ParseError) as exc:
        parse_activity_statement_trades(
            [row],
            instruments=instruments,
            period=ALL_DATES,
        )
    assert exc.value.column == "Currency"
    assert "USD only" in exc.value.message
    assert "issues/52" in exc.value.message


@pytest.mark.parametrize(
    ("symbol", "description", "isin", "expected_country"),
    [
        ("XQAV", "SAMPLE ISSUER AV PLC", "GB99970M1094", "GB"),  # United Kingdom
        ("XQAL", "SAMPLE ISSUER AL INC", "CA99991A1003", "CA"),  # Canada
        ("XQAC", "SAMPLE ISSUER AC LTD", "KY99971C1008", "KY"),  # Cayman Islands
        ("XQAF", "SAMPLE ISSUER AF PLC", "IE99970U1011", "IE"),  # Ireland
        ("XQAD", "SAMPLE ISSUER AD NV", "NL99971G1013", "NL"),  # Netherlands
        ("XQAN", "SAMPLE ISSUER AN LTD", "VG99972K1019", "VG"),  # British Virgin Islands
    ],
)
def test_non_us_issuer_isin_accepted_for_usd_traded_holdings(
    symbol: str, description: str, isin: str, expected_country: str
) -> None:
    """Issue #143: securities of foreign-incorporated issuers traded in
    USD on US exchanges are accepted as of #143. The country code is
    derived from ``ISIN[:2]`` and propagated to
    ``ExecutionEvent.issuer_country_code`` for correct Schedule FA col 1
    attribution.

    Pre-#143 the parser hard-rejected any non-US ISIN, blocking users
    with ADRs, Canadian dual-listed stocks, Cayman tech IPOs, Irish
    UCITS USD ETFs, etc. The synthetic securities below cover six
    non-US issuer country codes: CA, KY, IE, NL, VG and GB."""
    instruments = build_instrument_lookup(
        [make_fii_row(symbol=symbol, description=description, security_id=isin)]
    )
    [event] = parse_activity_statement_trades(
        [make_trade_row(symbol=symbol)],
        instruments=instruments,
        period=ALL_DATES,
    )
    assert event.issuer_country_code == CountryCode(expected_country)


def test_in_prefix_isin_rejected_as_not_foreign_asset() -> None:
    """Issue #143 follow-up: an IN-prefixed ISIN must be rejected.
    Schedule FA reports FOREIGN assets for an Indian tax resident;
    Indian-issued securities (IN[:2]) are domestic, not foreign,
    regardless of which broker holds them.

    This case is extremely unlikely on IBKR's US platform (Indian
    shares aren't directly tradeable on NYSE/NASDAQ — what users
    actually hold are US-issued ADRs, not the underlying Indian
    shares). But for defence-in-depth, the
    parser refuses with a clear message."""
    instruments = build_instrument_lookup([make_fii_row(symbol="XQIN", security_id="IN99990E1034")])
    with pytest.raises(ParseError) as exc:
        parse_activity_statement_trades(
            [make_trade_row(symbol="XQIN")],
            instruments=instruments,
            period=ALL_DATES,
        )
    assert exc.value.column == "Symbol"
    assert "'IN'" in exc.value.message
    assert "India" in exc.value.message
    assert "foreign" in exc.value.message.lower()
    # #1038: the refusal names the row and column, not the symbol or ISIN.
    assert exc.value.raw_value == ""
    for value in ("XQIN", "IN99990E1034"):
        assert value not in str(exc.value)


def test_unmapped_country_code_rejected_at_parse_time() -> None:
    """PR #146 Tier-1 r1 correctness P3-3: an ISO-valid country
    code that isn't in the bundled ITR country-code table (probed
    here with the user-assigned ZZ prefix) must be
    rejected at PARSE time with a clear message pointing to the
    CSV extension, not later at the compute layer with no
    row_index/raw_value context.

    Pre-#146 r1 P3-3 fix: BR would pass parse (since != IN), then
    raise CountryCodeNotFoundError deep in build_fa_a3_rows() with
    no parser context. Post-fix: rejected at parse time with the
    same actionable CSV-extension hint."""
    # ZZ is reserved in ISO 3166-1 for "user-assigned" codes — it
    # will never appear in the CBDT portal dropdown, so this test
    # remains a stable "unmapped country" probe even after PR #74
    # expanded itr_country_codes.csv to the full 249-country CBDT
    # annexure. (Pre-#74 the test used "BR" because Brazil was
    # genuinely missing from the 26-row subset; with the full
    # annexure BR maps to CBDT 55 and the missing-country code
    # path can only be exercised with a synthetic non-assigned
    # ISO prefix.) PR #146 Tier-4 P2 (Claude review): use a
    # digit-terminated ISIN (12th char must be a decimal digit per
    # ISO 6166); ``ZZ1234567890`` passes ``OpenLot._ISIN_PATTERN``
    # so the test exercises the lookup_country guard rather than
    # the ISIN-shape guard.
    instruments = build_instrument_lookup(
        [make_fii_row(symbol="ZZTEST", security_id="ZZ1234567890")]
    )
    with pytest.raises(ParseError) as exc:
        parse_activity_statement_trades(
            [make_trade_row(symbol="ZZTEST")],
            instruments=instruments,
            period=ALL_DATES,
        )
    assert exc.value.column == "Symbol"
    assert "ZZ" not in str(exc.value)
    assert exc.value.raw_value == ""
    assert "ITR country-code table" in exc.value.message
    assert "itr_country_codes.csv" in exc.value.message


def test_lowercase_isin_normalized_to_uppercase_in_event() -> None:
    """PR #146 Tier-5 P2 (Codex GitHub bot): a malformed lowercase
    Security ID like ``ca99991a1003`` (otherwise structurally valid)
    must be normalized to uppercase at the parser boundary BEFORE
    OpenLot construction. Pre-Tier-5, only the 2-char country prefix
    was upper()'d; the full ``instrument.isin`` was passed raw to
    OpenLot, which would fail ``_ISIN_PATTERN`` (requires uppercase
    letters) with an ``InvariantError`` that loses section/row/column
    context — a regression vs pre-#143 where the country-prefix
    rejection raised ParseError at the parser layer.

    IBKR always emits uppercase ISINs in real exports, but a
    corrupted statement (or future malformed shape) should still
    parse cleanly via the normalization, OR fail with a proper
    ParseError carrying row context — never with a raw model-level
    InvariantError."""
    instruments = build_instrument_lookup(
        [
            make_fii_row(
                symbol="XQAL", description="SAMPLE ISSUER AL INC", security_id="ca99991a1003"
            )
        ]
    )
    [event] = parse_activity_statement_trades(
        [make_trade_row(symbol="XQAL")],
        instruments=instruments,
        period=ALL_DATES,
    )
    # ISIN normalized end-to-end to canonical uppercase form.
    assert event.isin == "CA99991A1003"
    assert event.issuer_country_code == CountryCode("CA")


@pytest.mark.parametrize(
    "malformed_isin",
    [
        pytest.param("CA12345", id="too-short-7-chars-expected-12"),
        pytest.param("CA99970D10951", id="too-long-13-chars"),
        pytest.param("CA99991A100X", id="trailing-X-instead-of-digit"),
        pytest.param("CA99991A10!1", id="special-char-in-payload"),
    ],
)
def test_malformed_isin_shape_rejected_at_parse_time(malformed_isin: str) -> None:
    """#150: ``build_instrument_lookup`` validates ISO 6166 ISIN shape at
    parse time via :func:`ibkr_fa.parser._isin.validate_isin_or_raise`,
    raising ``ParseError`` with section/row/column context before the
    bad ISIN can flow into ``parse_activity_statement_trades`` and
    eventually trip ``OpenLot.__post_init__``'s contextless
    ``InvariantError``. A malformed ISIN whose 2-char prefix is a
    MAPPED non-IN country (e.g., ``CA12345``) used to survive the IN
    check and the ``lookup_country()`` defence; the explicit shape
    check now catches it at the earliest possible site (FII section
    parse), preserving fail-fast posture with row context."""
    with pytest.raises(ParseError) as exc:
        build_instrument_lookup([make_fii_row(symbol="TEST", security_id=malformed_isin)])
    assert exc.value.section == "Financial Instrument Information"
    assert exc.value.column == "Security ID"
    assert "ISO 6166" in exc.value.message
    assert "issues/150" in exc.value.message


def test_us_isin_passes_country_check() -> None:
    """Sanity inverse: the default US ISIN passes the new check."""
    instruments = build_instrument_lookup([make_fii_row()])
    [event] = parse_activity_statement_trades(
        [make_trade_row()],
        instruments=instruments,
        period=ALL_DATES,
    )
    assert event.issuer_country_code == CountryCode("US")


def test_resolve_instrument_defence_in_depth_isin_shape_check_fires(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#150 / T2 PR #168 P3: the trades-resolver's defence-in-depth
    ISO 6166 re-check is now unreachable through ``build_instrument_lookup``
    (which validates at parse time), so this test constructs a
    :class:`FinancialInstrument` directly with a malformed ISIN and
    feeds it into the trades parser to exercise the resolver-time
    re-check at ``trades.py:_resolve_instrument``. Locks the
    defence-in-depth contract against future bugs that might
    construct ``FinancialInstrument`` outside the parser-side
    helper.

    We have to construct the malformed instrument carefully: the
    NewType ``Isin`` has no runtime validation, but ``ClosedLot``
    and ``OpenLot`` model ``__post_init__``s validate shape — so a
    malformed ISIN won't survive into a built lot. The
    ``ParseError`` from the resolver's re-check fires BEFORE any
    lot is constructed, so the test is consistent.
    """
    bad_instrument = FinancialInstrument(
        symbol=Ticker("TEST"),
        description="malformed-isin-test",
        isin=Isin("US-78331005"),  # 11 chars, with a hyphen in the payload
        listing_exchange="NASDAQ",
        asset_class="Stocks",
    )
    instruments: dict[Ticker, FinancialInstrument] = {Ticker("TEST"): bad_instrument}
    with pytest.raises(ParseError) as exc:
        parse_activity_statement_trades(
            [make_trade_row(symbol="TEST")],
            instruments=instruments,
            period=ALL_DATES,
        )
    assert exc.value.section == "Trades"
    assert exc.value.column == "Symbol"
    assert "ISO 6166" in exc.value.message
    assert "defence-in-depth" in exc.value.message
    assert "US-78331005" not in str(exc.value)
    assert exc.value.raw_value == ""


def test_negative_quantity_opening_short_creates_short_event() -> None:
    """#109 implementation: an opening short sale arrives with
    Code=O and NEGATIVE Quantity. v2 of the parser (#109) now
    classifies these as Side.SHORT events rather than rejecting.

    Stored attributes follow OpenLot's "absolute value + Side enum"
    convention: quantity is the unsigned share count, cost is
    unsigned, side records the direction. Downstream
    overlapping_lots_for_cy filters Side.SHORT out of Schedule FA
    output (short positions are liabilities, not foreign assets).

    Pre-#109 behaviour: rejected with ParseError pointing at #109."""
    instruments = build_instrument_lookup([make_fii_row()])
    row = make_trade_row()
    row["Quantity"] = "-5"
    # IBKR reports Basis with the same sign as Quantity for shorts
    # (shares * per-share cost). default basis is 412.50 for the
    # default qty of 10; flip the sign to mirror IBKR's emission.
    row["Basis"] = "-206.25"  # = -5 * 41.25
    [event] = parse_activity_statement_trades(
        [row],
        instruments=instruments,
        period=ALL_DATES,
    )
    assert event.kind is ExecutionKind.OPEN
    assert event.side == Side.SHORT
    assert event.quantity == Decimal("5")  # absolute value
    assert event.acquisition_cost == Decimal("206.25")  # absolute value


def test_zero_quantity_opening_trade_rejected() -> None:
    """#109 defensive: an O trade with Quantity=0 moves no shares
    and would create a degenerate OpenLot (cost_basis_price would
    require dividing by zero). Refuse rather than construct."""
    instruments = build_instrument_lookup([make_fii_row()])
    row = make_trade_row()
    row["Quantity"] = "0"
    with pytest.raises(ParseError) as exc:
        parse_activity_statement_trades(
            [row],
            instruments=instruments,
            period=ALL_DATES,
        )
    assert exc.value.column == "Quantity"
    assert exc.value.message == _ZERO_QUANTITY


def test_short_lot_signed_reconciliation_matches_negative_open_positions_qty() -> None:
    """#109: Open Positions Summary reports a short position's
    Quantity as NEGATIVE. validate_reconciliation now sums the
    reconstructed lots' SIGNED quantities (positive for LONG,
    negative for SHORT) so the sum equals IBKR's signed Quantity.

    Pre-#109 sum used absolute values, which would have made a
    short fail reconciliation against a negative Open Positions
    Quantity."""
    instruments = build_instrument_lookup([make_fii_row()])
    row = make_trade_row()
    row["Quantity"] = "-5"
    row["Basis"] = "-206.25"
    lots = _open_lots(
        [row],
        instruments=instruments,
    )
    # Must not raise: signed sum (-5) matches Open Positions
    # Summary's signed Quantity (-5).
    validate_reconciliation(lots, [_make_op_row(symbol="XQAW", quantity="-5")])


def test_short_lot_reconciliation_fails_on_quantity_mismatch() -> None:
    """Sign-aware reconciliation must still catch mismatches. If
    the parser reconstructs a -5 short but Open Positions Summary
    reports -10, that's a real bug and must raise."""
    instruments = build_instrument_lookup([make_fii_row()])
    row = make_trade_row()
    row["Quantity"] = "-5"
    row["Basis"] = "-206.25"
    lots = _open_lots(
        [row],
        instruments=instruments,
    )
    with pytest.raises(ParseError) as exc:
        validate_reconciliation(lots, [_make_op_row(symbol="XQAW", quantity="-10")])
    assert "reconciliation failed" in exc.value.message
    assert "XQAW" not in str(exc.value)


def test_short_lot_reconciliation_fails_when_sign_disagrees() -> None:
    """If the parser reconstructs a +5 long but Open Positions
    Summary reports -5 (short), the signed sum (+5) doesn't match
    (-5) so reconciliation must fail. Pre-#109 (absolute-value
    comparison) would have erroneously accepted this case."""
    instruments = build_instrument_lookup([make_fii_row()])
    # Default make_trade_row() builds a LONG trade with qty=10
    lots = _open_lots(
        [make_trade_row(quantity="5", basis="206.25")],
        instruments=instruments,
    )
    with pytest.raises(ParseError) as exc:
        validate_reconciliation(lots, [_make_op_row(symbol="XQAW", quantity="-5")])
    assert "reconciliation failed" in exc.value.message


@pytest.mark.parametrize(
    "bad_value",
    ["NaN", "Infinity", "-Infinity", "sNaN"],
)
def test_decimal_helper_rejects_non_finite_values(bad_value: str) -> None:
    """Tier-1 PR #136 review (GPT-5.5 P3): the Decimal constructor
    accepts 'NaN' and 'Infinity' as valid syntax. A non-finite value
    reaching sign comparisons (e.g., `quantity_signed < 0`) raises
    raw decimal.InvalidOperation, which escapes the parser's
    ParseError contract and surfaces as a traceback / exit code 1.
    The new finite-check in `_decimal` rejects them up front."""
    instruments = build_instrument_lookup([make_fii_row()])
    row = make_trade_row()
    row["Quantity"] = bad_value
    with pytest.raises(ParseError) as exc:
        parse_activity_statement_trades(
            [row],
            instruments=instruments,
            period=ALL_DATES,
        )
    assert exc.value.column == "Quantity"
    assert "finite" in exc.value.message.lower()


def test_basis_sign_mismatch_with_long_quantity_rejected() -> None:
    """GitHub Codex (PR #136 review): IBKR emits Basis with the
    same sign as Quantity for opening trades. A long row with a
    NEGATIVE Basis is malformed/corrupted input — pre-Codex-fix
    the parser would silently `abs()` both and construct an
    apparently-valid OpenLot with the wrong economics."""
    instruments = build_instrument_lookup([make_fii_row()])
    row = make_trade_row()
    # default quantity=10 (long), but inject a negative Basis
    row["Basis"] = "-412.50"
    with pytest.raises(ParseError) as exc:
        parse_activity_statement_trades(
            [row],
            instruments=instruments,
            period=ALL_DATES,
        )
    assert exc.value.column == "Basis"
    assert "sign mismatch" in exc.value.message.lower() or "same sign" in exc.value.message


def test_basis_sign_mismatch_with_short_quantity_rejected() -> None:
    """Symmetric to the long case: a SHORT row (negative Quantity)
    with a POSITIVE Basis is also malformed. The Codex finding's
    fix catches BOTH directions in one sign-consistency check."""
    instruments = build_instrument_lookup([make_fii_row()])
    row = make_trade_row()
    row["Quantity"] = "-5"
    row["Basis"] = "206.25"  # positive Basis on a short — wrong
    with pytest.raises(ParseError) as exc:
        parse_activity_statement_trades(
            [row],
            instruments=instruments,
            period=ALL_DATES,
        )
    assert exc.value.column == "Basis"


def test_basis_zero_with_nonzero_quantity_accepted() -> None:
    """Edge case: Basis=0 is the only value where the sign-equality
    check is vacuously satisfied (0 isn't negative). IBKR doesn't
    typically emit Basis=0 for a real O trade, but if it does (e.g.,
    a free-grant degenerate case) the parser should not reject. The
    OpenLot's downstream `cost_basis_money >= 0` invariant catches
    semantically-zero cost separately."""
    instruments = build_instrument_lookup([make_fii_row()])
    row = make_trade_row()
    row["Basis"] = "0"
    [event] = parse_activity_statement_trades(
        [row],
        instruments=instruments,
        period=ALL_DATES,
    )
    assert event.acquisition_cost == Decimal("0")


def test_issuer_country_derived_from_instrument_isin() -> None:
    """Regression: ExecutionEvent.issuer_country_code must come from the
    instrument's ISIN prefix, not a hard-coded default. (The
    original implementation had issuer_country_code='US' as a
    function-level default param; Codex flagged that it would
    misattribute non-US lots. The new behaviour derives per-
    instrument, and the non-US case rejects loudly. This test
    verifies the per-instrument derivation on the happy path.)"""
    instruments = build_instrument_lookup([make_fii_row(symbol="XQAW", security_id="US99990E1038")])
    [event] = parse_activity_statement_trades(
        [make_trade_row()],
        instruments=instruments,
        period=ALL_DATES,
    )
    # The derived value comes from the ISIN's first 2 chars, NOT
    # from any function-level default. If someone reintroduces a
    # default, this test still passes for a US ISIN — but the
    # paired tests above
    # (``test_non_us_issuer_isin_accepted_for_usd_traded_holdings``
    # which checks CA/KY/IE/NL/VG/GB ISINs map to their correct
    # country codes, and ``test_in_prefix_isin_rejected_as_not_foreign_asset``
    # which checks IN-prefix is rejected) catch any regression
    # that hard-codes US or swallows the per-ISIN derivation.
    assert event.issuer_country_code == CountryCode("US")


# ---- Open-Positions reconciliation ---------------------------------------


def test_reconciliation_passes_when_quantities_match_exactly() -> None:
    """Two reconstructed lots of 5 shares + 5 shares = 10 shares;
    Open Positions Summary says 10. Pass."""
    instruments = build_instrument_lookup([make_fii_row()])
    lots = _open_lots(
        [
            make_trade_row(quantity="5", basis="206.25"),
            make_trade_row(quantity="5", trade_price="42.00", basis="210.00"),
        ],
        instruments=instruments,
    )
    validate_reconciliation(lots, [_make_op_row(symbol="XQAW", quantity="10")])


def test_reconciliation_passes_lot_rows_that_add_up_to_their_summary() -> None:
    """Flattened Open Positions blocks may include Lot rows.

    Reconciliation compares reconstructed aggregate Trades quantity to
    IBKR's Summary rows only; per-lot rows are not independent account
    totals and must not create false mismatches. Lot rows of 4 and 6
    split the Summary row's 10 (#1124).
    """
    instruments = build_instrument_lookup([make_fii_row()])
    lots = _open_lots(
        [
            make_trade_row(quantity="5", basis="206.25"),
            make_trade_row(quantity="5", trade_price="42.00", basis="210.00"),
        ],
        instruments=instruments,
    )
    validate_reconciliation(
        lots,
        [
            _lot(symbol="XQAW", quantity="4"),
            _make_op_row(symbol="XQAW", quantity="10"),
            _lot(symbol="XQAW", quantity="6"),
        ],
    )


@pytest.mark.parametrize(
    ("lot_rows", "position"),
    [
        pytest.param(
            [_lot(symbol="XQAW", quantity="10"), _lot(symbol="XQAW", quantity="10")],
            (2, "Quantity"),
            id="lots-add-up-to-twice-the-summary",
        ),
        pytest.param(
            [_lot(symbol="XQAX", quantity="4"), _lot(symbol="XQAX", quantity="6")],
            (0, "Symbol"),
            id="lots-without-a-summary-row",
        ),
    ],
)
def test_reconciliation_refuses_lot_rows_their_summary_does_not_account_for(
    lot_rows: list[dict[str, str]], position: tuple[int, str]
) -> None:
    """#1124: both readers skipped Lot rows unread, so lots that don't add
    up to their Summary row, or that have none, passed."""
    instruments = build_instrument_lookup([make_fii_row()])
    lots = _open_lots([make_trade_row()], instruments=instruments)
    with pytest.raises(ParseError) as exc:
        validate_reconciliation(lots, [*lot_rows, _make_op_row(symbol="XQAW", quantity="10")])
    assert (exc.value.section, exc.value.row_index, exc.value.column) == (
        "Open Positions",
        *position,
    )


def test_reconciliation_passes_within_tolerance() -> None:
    """0.0001 share difference is fractional rounding noise and
    accepted. This is the EPSILON value documented in the parser."""
    instruments = build_instrument_lookup([make_fii_row()])
    lots = _open_lots(
        [make_trade_row(quantity="9.9999", basis="412.495875")],  # 9.9999 * 41.25
        instruments=instruments,
    )
    validate_reconciliation(lots, [_make_op_row(symbol="XQAW", quantity="10.0000")])


def test_reconciliation_fails_beyond_tolerance() -> None:
    """A 5-share difference (5 vs 10) far exceeds tolerance — refuse output."""
    instruments = build_instrument_lookup([make_fii_row()])
    lots = _open_lots(
        [make_trade_row(quantity="5", basis="206.25")],
        instruments=instruments,
    )
    with pytest.raises(ParseError) as exc:
        validate_reconciliation(lots, [_make_op_row(symbol="XQAW", quantity="10")])
    # Neither the symbol nor either quantity appears (#1039).
    assert exc.value.message == _RECONCILIATION
    assert exc.value.raw_value == ""


def test_reconciliation_fails_when_symbol_in_op_has_no_lots() -> None:
    """Open Positions reports a symbol we don't have any
    reconstructed lots for. Refuse — Schedule FA would underreport."""
    lots: list[OpenLot] = []
    with pytest.raises(ParseError) as exc:
        validate_reconciliation(lots, [_make_op_row(symbol="XQAW", quantity="10")])
    assert "XQAW" not in str(exc.value)
    # The message should hint at the most likely cause.
    assert "truncated" in exc.value.message or "transfer-in" in exc.value.message


def test_reconciliation_fails_when_lots_have_symbol_not_in_op() -> None:
    """A reconstructed open lot for a symbol not in Open Positions
    is a parser bug — defence-in-depth check."""
    instruments = build_instrument_lookup([make_fii_row()])
    lots = _open_lots(
        [make_trade_row()],
        instruments=instruments,
    )
    # Pass an EMPTY open_positions_rows. The function returns
    # without raising the per-row reconciliation error... but the
    # extras-check at the end catches XQAW-in-lots-but-not-in-OP.
    with pytest.raises(ParseError) as exc:
        validate_reconciliation(lots, [])
    # One past the last of no rows; no symbol named (#1039).
    assert (exc.value.row_index, exc.value.column) == (0, None)
    assert exc.value.message == _missing_summary_rows(1)


def test_reconciliation_handles_empty_inputs() -> None:
    """Both empty = nothing to compare = pass."""
    validate_reconciliation([], [])


def test_reconciliation_rejects_malformed_op_row() -> None:
    """Defence: OP row missing Symbol or Quantity column raises
    with that column's name."""
    instruments = build_instrument_lookup([make_fii_row()])
    lots = _open_lots(
        [make_trade_row()],
        instruments=instruments,
    )
    bad_op = {"DataDiscriminator": "Summary", "Symbol": "XQAW"}  # no Quantity
    with pytest.raises(ParseError) as exc:
        validate_reconciliation(lots, [bad_op])
    assert exc.value.column == "Quantity"


def test_reconciliation_rejects_non_decimal_quantity() -> None:
    instruments = build_instrument_lookup([make_fii_row()])
    lots = _open_lots(
        [make_trade_row()],
        instruments=instruments,
    )
    with pytest.raises(ParseError) as exc:
        validate_reconciliation(lots, [_make_op_row(symbol="XQAW", quantity="not-a-number")])
    assert exc.value.column == "Quantity"


@pytest.mark.parametrize("bad_value", ["NaN", "Infinity", "-Infinity", "sNaN"])
def test_reconciliation_rejects_non_finite_quantity(bad_value: str) -> None:
    """Tier-2 PR #136 review (Claude Code Opus 4.7 max): the inline
    `Decimal(qty_str)` call in validate_reconciliation bypasses the
    `is_finite()` guard added to `_decimal()`. A NaN/Infinity in
    Open Positions Quantity would reach the tolerance comparison
    and raise raw InvalidOperation, escaping the ParseError
    contract. Now rejected up front."""
    instruments = build_instrument_lookup([make_fii_row()])
    lots = _open_lots(
        [make_trade_row()],
        instruments=instruments,
    )
    with pytest.raises(ParseError) as exc:
        validate_reconciliation(lots, [_make_op_row(symbol="XQAW", quantity=bad_value)])
    assert exc.value.column == "Quantity"
    assert "finite" in exc.value.message.lower()


# ---- end-to-end against private statements --------------------------------
#
# These read files kept outside git (``tests/_private_data.py``) and skip
# when they are absent. They check only relations inside each file.


def _check_private_statement_trades(sample: Path) -> list[OpenLot]:
    """Run the pipeline's Trades steps on ``sample``, check them against
    the file's own rows and return the open lots.

    As in the pipeline, each section's blocks are flattened in file order
    and the lots are stamped with the end of the file's own Statement
    period. Each event's quantity is its row's ``Quantity`` without the
    sign, and its side follows that sign (an opening row with a negative
    quantity, or a closing row with a positive one, is SHORT). Each open
    lot keeps the side of an opening event of its symbol and date, and
    the lots reconcile with the file's Open Positions."""
    sections = split_activity_statement_sections(sample)

    def rows(kind: KnownActivitySection) -> list[dict[str, str]]:
        return [row for block in sections.get(kind, []) for row in block.rows]

    period = parse_statement_period(rows(KnownActivitySection.STATEMENT))
    instruments = build_instrument_lookup(
        rows(KnownActivitySection.FINANCIAL_INSTRUMENT_INFORMATION)
    )
    trade_rows = rows(KnownActivitySection.TRADES)
    open_positions_rows = rows(KnownActivitySection.OPEN_POSITIONS)
    events = parse_activity_statement_trades(trade_rows, instruments=instruments, period=period)
    for event in events:
        signed = Decimal(trade_rows[event.row_index]["Quantity"])
        short = signed < 0 if event.kind is ExecutionKind.OPEN else signed > 0
        assert event.quantity == abs(signed)
        assert event.side is (Side.SHORT if short else Side.LONG)
    lots, _closed_lots = match_fifo(
        events,
        dec31_close_prices=build_dec31_close_prices(open_positions_rows),
        report_date=period.end_date,
    )
    opening = {
        (event.symbol, event.trade_date_time.date(), event.side)
        for event in events
        if event.kind is ExecutionKind.OPEN
    }
    assert all((lot.symbol, lot.open_date_time, lot.side) in opening for lot in lots)
    validate_reconciliation(lots, open_positions_rows)
    return lots


def _csv_open_positions_close_prices(sample: Path) -> dict[str, Decimal]:
    """Each Open Positions ``Summary`` row's ``Close Price`` by ``Symbol``,
    read with ``csv`` straight from the file rather than the splitter."""
    out: dict[str, Decimal] = {}
    header: list[str] = []
    with sample.open(newline="", encoding="utf-8-sig") as source:
        for row in csv.reader(source):
            if len(row) < 2 or row[0] != "Open Positions":
                continue
            if row[1] == "Header":
                header = row[2:]
            elif row[1] == "Data":
                cells = dict(zip(header, row[2:], strict=False))
                if cells["DataDiscriminator"] == "Summary":
                    out[cells["Symbol"]] = Decimal(cells["Close Price"])
    return out


@private_test
def test_works_against_private_fy2025_26_statement() -> None:
    """End-to-end against the private FY 2025-26 statement: every step from
    splitter through instrument lookup, trade reconstruction and
    reconciliation succeeds, checked against the file's own rows."""
    [sample] = private_dataset("ibkr/activity-statement-fy2025-26.csv")
    _check_private_statement_trades(sample)


@private_test
def test_works_against_private_cy2025_statement() -> None:
    """End-to-end against the private CY 2025 statement, as above."""
    [sample] = private_dataset("ibkr/activity-statement-cy2025.csv")
    _check_private_statement_trades(sample)


# ============================================================================
# Parser postconditions (PR #124, issue #118)
# ============================================================================


def test_build_instrument_lookup_empty_input_yields_empty_dict() -> None:
    """The post-loop invariant `bool(out) == bool(input)` is satisfied
    on empty input: no rows -> empty dict."""
    assert build_instrument_lookup([]) == {}


def test_build_instrument_lookup_non_empty_input_yields_non_empty_dict() -> None:
    """Inverse: input with rows must produce a non-empty dict (the
    happy-path assertion that locks the invariant)."""
    out = build_instrument_lookup([make_fii_row()])
    assert len(out) == 1
    assert "XQAW" in out


def test_parse_trades_post_one_event_per_input_row() -> None:
    """The post-loop assertion in parse_activity_statement_trades
    verifies len(out) == len(input). A silently-dropped row would
    fire the invariant. The happy path is covered here."""
    instruments = build_instrument_lookup(
        [
            make_fii_row(symbol="A", security_id="US99971J1050"),
            make_fii_row(symbol="B", security_id="US99971J1051"),
        ]
    )
    rows = [make_trade_row(symbol="A"), make_trade_row(symbol="B")]
    out = parse_activity_statement_trades(
        rows,
        instruments=instruments,
        period=ALL_DATES,
    )
    assert len(out) == 2


# ============================================================================
# Dec 31 close-price wiring (issue #133)
# ============================================================================
#
# The Activity Statement parser used to populate OpenLot.mark_price from
# the trade row's C. Price (= close on the TRADE day). For any lot
# acquired before Dec 31 and still held at year-end, that over- or
# under-stated Schedule FA's Closing Balance, which must use the
# Dec 31 close.
#
# Fix: mark_price comes from dec31_close_prices (derived from Open
# Positions Summary). C. Price is no longer parsed at all — column
# presence is still required (via _REQUIRED_TRADES_ROW_KEYS), but
# the value is ignored, so a malformed C. Price doesn't reject an
# otherwise-valid statement.


def test_build_dec31_close_prices_basic() -> None:
    """The happy path: two symbols, each with a Close Price → dict."""
    rows = [
        _make_op_summary_row(symbol="XQAA", close_price="128.40"),
        _make_op_summary_row(symbol="XQAY", close_price="57.15"),
    ]
    out = build_dec31_close_prices(rows)
    assert out == {Ticker("XQAA"): Decimal("128.40"), Ticker("XQAY"): Decimal("57.15")}


def test_build_dec31_close_prices_empty_input() -> None:
    """Empty Open Positions → empty dict (no positions held at Dec 31)."""
    assert build_dec31_close_prices([]) == {}


def test_build_dec31_close_prices_rejects_missing_close_price_column() -> None:
    row = _make_op_summary_row(symbol="XQAW", close_price="100")
    del row["Close Price"]
    with pytest.raises(ParseError) as exc:
        build_dec31_close_prices([row])
    assert exc.value.column == "Close Price"
    assert "missing" in exc.value.message.lower()


def test_build_dec31_close_prices_rejects_missing_symbol_column() -> None:
    row = _make_op_summary_row(symbol="XQAW", close_price="100")
    del row["Symbol"]
    with pytest.raises(ParseError) as exc:
        build_dec31_close_prices([row])
    assert exc.value.column == "Symbol"


def test_build_dec31_close_prices_rejects_duplicate_symbol() -> None:
    """Two Summary rows with the same Symbol would silently overwrite
    each other in a naive dict build; refuse loudly so the user can
    investigate a malformed statement."""
    rows = [
        _make_op_summary_row(symbol="XQAW", close_price="100"),
        _make_op_summary_row(symbol="XQAW", close_price="200"),
    ]
    with pytest.raises(ParseError) as exc:
        build_dec31_close_prices(rows)
    assert exc.value.column == "Symbol"
    assert "more than once" in exc.value.message


def test_build_dec31_close_prices_rejects_non_decimal_close_price() -> None:
    rows = [_make_op_summary_row(symbol="XQAW", close_price="not-a-number")]
    with pytest.raises(ParseError) as exc:
        build_dec31_close_prices(rows)
    assert exc.value.column == "Close Price"
    assert exc.value.message == _DECIMAL


@pytest.mark.parametrize("bad_value", ["NaN", "Infinity", "-Infinity", "sNaN"])
def test_build_dec31_close_prices_rejects_non_finite_close_price(bad_value: str) -> None:
    """Tier-2 PR #136 review (Claude Code Opus 4.7 max): the inline
    `Decimal(close_price_raw)` call in build_dec31_close_prices
    bypasses the `is_finite()` guard added to `_decimal()`. A
    NaN/Infinity Close Price would propagate into OpenLot.mark_price
    and raise raw InvalidOperation from the >= 0 invariant check,
    escaping the ParseError contract. Now rejected up front."""
    rows = [_make_op_summary_row(symbol="XQAW", close_price=bad_value)]
    with pytest.raises(ParseError) as exc:
        build_dec31_close_prices(rows)
    assert exc.value.column == "Close Price"
    assert "finite" in exc.value.message.lower()


@private_test
def test_private_cy2025_lots_are_marked_at_their_open_positions_close_price() -> None:
    """#133 end-to-end against the private CY 2025 statement: every open
    lot is marked at the ``Close Price`` of its symbol's Open Positions
    ``Summary`` row, read with ``csv`` straight from the file, not at
    its trade row's ``C. Price``."""
    [sample] = private_dataset("ibkr/activity-statement-cy2025.csv")
    close_prices = _csv_open_positions_close_prices(sample)
    for lot in _check_private_statement_trades(sample):
        assert lot.mark_price == close_prices[str(lot.symbol)]


def test_build_dec31_close_prices_skips_lot_rows() -> None:
    """Tier-1 PR #134 review: the function reads only rows whose
    DataDiscriminator is 'Summary'. The per-lot 'Lot' rows of
    lot-detail mode are skipped so they don't trigger a duplicate-symbol
    error; any other kind refuses (#1093).

    Important contract: a future IBKR schema change that adds Lot
    rows alongside Summary rows must NOT break the function — it
    should continue to extract the same Summary-row close prices."""
    summary_row = _make_op_summary_row(symbol="XQAW", close_price="100")
    non_summary_row = {**summary_row, "DataDiscriminator": "Lot"}
    out = build_dec31_close_prices([non_summary_row, summary_row, non_summary_row])
    # Only the Summary row contributed.
    assert out == {Ticker("XQAW"): Decimal("100")}


def test_build_dec31_close_prices_all_lot_rows_returns_empty() -> None:
    """If every input row is a Lot row, the function returns {}
    (same as an empty input). The postcondition counts Summary rows,
    not raw input length, so this is consistent."""
    row = {**_make_op_summary_row(symbol="X", close_price="1"), "DataDiscriminator": "Lot"}
    assert build_dec31_close_prices([row, row]) == {}


def test_build_dec31_close_prices_rejects_missing_discriminator_column() -> None:
    """Tier-1 r2 review: missing the DataDiscriminator column entirely
    is a schema-drift signal, not a silently-skip case. Using `.get()`
    + None-check would silently drop the row; the parser now requires
    the column's presence and raises ParseError if absent."""
    row = _make_op_summary_row(symbol="XQAW", close_price="100")
    del row["DataDiscriminator"]
    with pytest.raises(ParseError) as exc:
        build_dec31_close_prices([row])
    assert exc.value.column == "DataDiscriminator"
    assert "missing" in exc.value.message.lower()
    assert "schema" in exc.value.message.lower()


def test_build_dec31_close_prices_rejects_non_usd_currency() -> None:
    """Tier-3 r1 review (Codex CLI GPT-5.5 high): the parser's USD-only
    check on Trades doesn't cover Open Positions. Without this guard,
    a non-USD Summary row would silently feed its Close Price into an
    OpenLot whose currency_primary is forced to USD by the Trades
    parser — producing a wrong-currency Schedule FA closing balance.

    Mirrors the existing USD-only check on parse_activity_statement_trades."""
    row = _make_op_summary_row(symbol="XQAW", close_price="100")
    row["Currency"] = "EUR"
    with pytest.raises(ParseError) as exc:
        build_dec31_close_prices([row])
    assert exc.value.column == "Currency"
    assert "USD" in exc.value.message
    assert "issues/52" in exc.value.message


def test_build_dec31_close_prices_rejects_missing_currency_column() -> None:
    """Companion to the USD-only check: a Summary row that's missing
    the Currency column entirely is also schema drift, not a silent-
    skip case. Same defensive pattern as the missing-DataDiscriminator
    test."""
    row = _make_op_summary_row(symbol="XQAW", close_price="100")
    del row["Currency"]
    with pytest.raises(ParseError) as exc:
        build_dec31_close_prices([row])
    assert exc.value.column == "Currency"


def test_build_dec31_close_prices_rejects_empty_currency() -> None:
    """Empty Currency cell behaves like a missing currency assignment —
    refuse via _non_empty rather than fall through to the USD-equality
    check (which would let an empty string through with the wrong
    error)."""
    row = _make_op_summary_row(symbol="XQAW", close_price="100")
    row["Currency"] = ""
    with pytest.raises(ParseError) as exc:
        build_dec31_close_prices([row])
    assert exc.value.column == "Currency"


def test_parse_trades_tolerates_malformed_c_price() -> None:
    """Tier-1 PR #134 review: C. Price is no longer parsed — its
    value can be 'not-a-number' or empty and the parser still
    succeeds, because the matcher takes mark_price from
    dec31_close_prices.
    Locks the post-#133 contract change so a future "tighten this"
    refactor that re-introduces C. Price validation will fail this
    test and require an explicit decision."""
    instruments = build_instrument_lookup([make_fii_row()])
    row = make_trade_row(close_price="not-a-number")
    [event] = parse_activity_statement_trades(
        [row],
        instruments=instruments,
        period=ALL_DATES,
    )
    assert event.kind is ExecutionKind.OPEN


# ============================================================================
# Closing rows (issue #104); FIFO matching is tested in
# tests/test_compute_lot_matching.py
# ============================================================================


def test_closing_buy_passes_through_as_a_short_close() -> None:
    """A positive-quantity C-row is a closing BUY ("buy-to-cover"). The
    parser passes its sign through as a SHORT close, and the matcher
    refuses it (#109; see ``tests/test_compute_lot_matching.py``).
    """
    instruments = build_instrument_lookup([make_fii_row()])
    rows = [
        make_trade_row(quantity="-10", trade_price="100", basis="-1000", code="O"),
        make_trade_row(
            date_time="2025-09-15, 09:30:00", quantity="5", trade_price="100", basis="500", code="C"
        ),
    ]
    _short_open, cover = parse_activity_statement_trades(
        rows, instruments=instruments, period=ALL_DATES
    )
    assert (cover.kind, cover.side, cover.quantity) == (
        ExecutionKind.CLOSE,
        Side.SHORT,
        Decimal("5"),
    )


def test_disallowed_codes_error_uses_full_issue_urls() -> None:
    """Tier-1 PR #161 docs review (Claude Opus 4.6 1M): the
    _validate_code error must use full GitHub URLs (clickable in
    terminals) for every tracker reference, consistent with the
    rest of the parser's user-facing errors. Uses ``Adj`` (corporate
    action, #106) as a stand-in for any non-allow-listed token since
    ``LD`` was promoted to the allow-list in issue #105."""
    instruments = build_instrument_lookup([make_fii_row()])
    with pytest.raises(ParseError) as exc:
        parse_activity_statement_trades(
            [make_trade_row(code="Adj")],
            instruments=instruments,
            period=ALL_DATES,
        )
    msg = exc.value.message
    for slug in (
        "https://github.com/jay-tau/ibkr-fa/issues/101",
        "https://github.com/jay-tau/ibkr-fa/issues/104",
        "https://github.com/jay-tau/ibkr-fa/issues/106",
        "https://github.com/jay-tau/ibkr-fa/issues/107",
    ):
        assert slug in msg, f"missing tracker URL: {slug}"
    # #105 is now in the allow-list, so the LD wash-sale URL should
    # NOT appear in the error message; only the still-unhandled
    # trackers (#106, #107) should be cited.
    assert "issues/105" not in msg


# ---- LD (wash-sale loss disallowed) acceptance (#105) -------------------


def test_ld_wash_sale_code_refused_as_us_adjusted_basis() -> None:
    """India has no wash-sale rule, so the Indian return must use the
    ACTUAL acquisition cost. IBKR's ``LD`` (loss-disallowed) opening row
    reports the US wash-sale-ADJUSTED ``Basis`` (the disallowed loss is
    rolled into it — here 100 sh @ 20.00 reported as Basis 2100, $100
    over the actual 2000 cost), which we cannot reliably reduce to the
    actual cost from the Activity Statement alone. So ``LD`` rows are
    refused rather than emit a US-tax basis on an Indian return (#265)."""
    instruments = build_instrument_lookup([make_fii_row()])
    with pytest.raises(ParseError) as exc:
        parse_activity_statement_trades(
            [
                make_trade_row(
                    symbol="XQAW",
                    date_time="2025-06-10, 10:00:00",
                    quantity="100",
                    trade_price="20.00",
                    close_price="45.60",
                    basis="2100.00",  # US wash-sale-adjusted (actual cost is 2000)
                    code="O;LD",
                )
            ],
            instruments=instruments,
            period=ALL_DATES,
        )
    assert exc.value.column == "Code"
    assert "LD" in exc.value.message
    assert "issues/265" in exc.value.message


def test_ld_token_refused_regardless_of_direction() -> None:
    """Any ``LD`` (wash-sale loss-disallowed) marker is refused — IBKR's
    ``LD`` basis is the US wash-sale-adjusted figure, which India can't
    use (it needs the actual acquisition cost). This holds for ``O;LD``,
    ``C;LD``, and a bare ``LD`` (also direction-ambiguous). See #265."""
    instruments = build_instrument_lookup([make_fii_row()])
    with pytest.raises(ParseError) as exc:
        parse_activity_statement_trades(
            [make_trade_row(code="LD")],
            instruments=instruments,
            period=ALL_DATES,
        )
    assert exc.value.column == "Code"
    assert "issues/265" in exc.value.message


def test_im_ia_wash_sale_codes_accepted_with_actual_cost_basis() -> None:
    """``IM`` / ``IA`` are RPL-tracked wash-sale venue codes. Unlike ``LD``
    (which rewrites ``Basis`` in place to ADD the US disallowed loss —
    refused), an ``IM`` / ``IA`` opening row reports the ACTUAL acquisition
    cost in ``Basis`` (the disallowed loss sits in the row's Realized P/L,
    which we don't fold into basis), so it is India-correct verbatim and
    must be ACCEPTED (#265).

    Locks in the distinction for ``IM;O`` / ``IA;O`` lots: ``Basis`` ==
    ``T. Price`` x ``Quantity`` + commission, Realized P/L == 0, i.e. no
    disallowed-loss add-back. A future refactor of the code-token handling
    must not sweep these into the ``LD`` basis-adjustment refusal (that
    would wrongly reject correct rows) nor drop them from the allow-list."""
    instruments = build_instrument_lookup([make_fii_row()])
    # 11 sh @ 41.25 = 453.75 actual cost; Basis carries exactly that (no
    # wash-sale add-back), unlike an LD row which would inflate it.
    [event] = parse_activity_statement_trades(
        [
            make_trade_row(
                quantity="11",
                trade_price="41.25",
                basis="453.75",
                code="IM;O;P",
            )
        ],
        instruments=instruments,
        period=ALL_DATES,
    )
    assert event.acquisition_cost == Decimal("453.75")
    assert event.acquisition_cost == event.quantity * Decimal("41.25")


def test_ld_on_closing_trade_refused_proceeds_recomputation_gap() -> None:
    """T5 codex P2 finding on PR #196: ``C;LD`` (wash-sale on a
    closing trade) is REFUSED because the parser derives per-closure
    proceeds as ``|Basis| + Realized P/L``, which produces a wrong
    figure when the realized P/L is artificially zeroed by the
    wash-sale disallowance.

    Concrete failure mode: a $2,000 lot sold for $1,200 with an
    $800 disallowed loss has reported Realized P/L = $0 (loss
    zeroed by disallowance). The parser would derive proceeds as
    $2,000 + $0 = $2,000 instead of the actual $1,200, silently
    over-reporting Schedule CG STCG/LTCG income by $800.

    Fix tracked as a future-#105 follow-up: read the ``Proceeds``
    column directly for ``C;LD`` rows."""
    instruments = build_instrument_lookup([make_fii_row()])
    rows = [
        make_trade_row(
            symbol="XQAW",
            date_time="2025-06-15, 09:30:00",
            quantity="100",
            trade_price="20.00",
            close_price="45.60",
            basis="2000.00",
            code="O",
        ),
        # Sale for $1,200 ($12/share * 100), $800 loss disallowed.
        make_trade_row(
            symbol="XQAW",
            date_time="2025-12-15, 14:00:00",
            quantity="-100",
            trade_price="12.00",
            close_price="45.60",
            basis="-2000.00",
            code="C;LD",
        ),
    ]
    rows[1]["Realized P/L"] = "0"  # disallowed: net realized = 0
    with pytest.raises(ParseError) as exc:
        parse_activity_statement_trades(
            rows,
            instruments=instruments,
            period=ALL_DATES,
        )
    assert exc.value.column == "Code"
    assert exc.value.message == _WASH_SALE
    assert "C;LD" not in str(exc.value)


# ---- stocks only (#913) -----------------------------------------------------


def test_instrument_lookup_accepts_rows_without_underlying_isin_or_code() -> None:
    """``Underlying``, ``ISIN`` and ``Code`` are checked only when the
    export has them; ``ISIN`` must then match ``Security ID``."""
    row = make_fii_row()
    del row["Underlying"], row["Code"]
    assert set(build_instrument_lookup([row])) == {Ticker("XQAW")}
    with_isin = {**make_fii_row(), "ISIN": "US99990E1038"}
    assert set(build_instrument_lookup([with_isin])) == {Ticker("XQAW")}


@pytest.mark.parametrize(
    ("column", "value", "rule"),
    [
        ("Asset Category", "Bonds", "must be 'Stocks'"),
        ("Asset Category", "Equity and Index Options", "must be 'Stocks'"),
        ("Multiplier", "100", "must be '1'"),
        ("Multiplier", "", "must be '1'"),
        ("Code", "X", "must be ''"),
        ("Underlying", "XQAW 250117C00010000", "must match the row's Symbol"),
        ("Underlying", "", "must match the row's Symbol"),
        ("ISIN", "US99992A1097", "must match the row's Security ID"),
        ("ISIN", "", "must match the row's Security ID"),
    ],
)
def test_instrument_lookup_refuses_a_row_that_is_not_a_plain_stock(
    column: str, value: str, rule: str
) -> None:
    """#913: v1 reads every Financial Instrument Information row as a
    stock. Another asset category, a multiplier other than 1, a code, an
    ``Underlying`` other than the row's own symbol or an ``ISIN`` that
    disagrees with ``Security ID`` would be read as a stock anyway."""
    row = {**make_fii_row(), column: value}
    with pytest.raises(ParseError) as exc:
        build_instrument_lookup([row])
    error = exc.value
    assert (error.section, error.row_index, error.column, error.raw_value) == (
        "Financial Instrument Information",
        0,
        column,
        "",
    )
    assert error.message.startswith(f"{rule}: ")


def test_instrument_lookup_refuses_a_row_without_a_multiplier() -> None:
    row = make_fii_row()
    del row["Multiplier"]
    with pytest.raises(ParseError) as exc:
        build_instrument_lookup([row])
    assert exc.value.column == "Multiplier"
    assert exc.value.message.startswith("required column 'Multiplier' missing from the row")


@pytest.mark.parametrize("discriminator", ["Summary", "Lot"])
@pytest.mark.parametrize(
    ("column", "value", "expected"),
    [
        ("Asset Category", "Equity and Index Options", "Stocks"),
        ("Mult", "100", "1"),
        ("Code", "X", ""),
    ],
)
def test_open_positions_refuse_a_row_that_is_not_a_plain_stock(
    discriminator: str, column: str, value: str, expected: str
) -> None:
    """#913: every Open Positions row, Summary or per-lot, is a stock
    with multiplier 1 and no code; anything else would be read as one."""
    row = {
        **_make_op_summary_row(symbol="XQAW", close_price="100"),
        "DataDiscriminator": discriminator,
        column: value,
    }
    with pytest.raises(ParseError) as exc:
        build_dec31_close_prices([row])
    error = exc.value
    assert (error.section, error.row_index, error.column, error.raw_value) == (
        "Open Positions",
        0,
        column,
        "",
    )
    assert error.message.startswith(f"must be {expected!r}: ")


@pytest.mark.parametrize("column", ["Asset Category", "Mult"])
def test_open_positions_refuse_a_row_without_its_category_or_multiplier(column: str) -> None:
    row = _make_op_summary_row(symbol="XQAW", close_price="100")
    del row[column]
    with pytest.raises(ParseError) as exc:
        build_dec31_close_prices([row])
    assert exc.value.column == column
    assert exc.value.message.startswith(f"required column {column!r} missing from the row")


def test_open_positions_accept_a_row_without_a_code_column() -> None:
    row = _make_op_summary_row(symbol="XQAW", close_price="100")
    del row["Code"]
    assert build_dec31_close_prices([row]) == {Ticker("XQAW"): Decimal("100")}


def test_trades_refuse_a_row_that_is_not_a_stock() -> None:
    """#913: a Trades row of another asset category would be read as a
    stock lot."""
    instruments = build_instrument_lookup([make_fii_row()])
    row = {**make_trade_row(), "Asset Category": "Bonds"}
    with pytest.raises(ParseError) as exc:
        parse_activity_statement_trades([row], instruments=instruments, period=ALL_DATES)
    error = exc.value
    assert (error.section, error.row_index, error.column, error.raw_value) == (
        "Trades",
        0,
        "Asset Category",
        "",
    )
    assert error.message.startswith("must be 'Stocks': ")


# ---- refusals echo no cell (#1039) -------------------------------------------
#
# A column-shifted or malformed row can put the holder's name or account
# number in any cell, and errors end up in screenshots and pasted issues. So
# every refusal here names its section and row, the column when one is at fault,
# and the rule broken, with fixed text and an empty ``raw_value``. Each case pins
# the exact message and checks the markers and its listed extra values are absent.

_DECIMAL = "must be a decimal number"
_FINITE = "must be a finite decimal"
_UNRECOGNISED_TOKEN = (
    "unrecognised trade code token. v1 of the Activity Statement parser only handles "
    "Open (https://github.com/jay-tau/ibkr-fa/issues/101) and Close "
    "(https://github.com/jay-tau/ibkr-fa/issues/104) trades with execution-routing "
    "metadata. Tracking issues for each unhandled case: "
    "https://github.com/jay-tau/ibkr-fa/issues/106 (corporate actions), "
    "https://github.com/jay-tau/ibkr-fa/issues/107 (transfers + tax-basis elections), "
    "https://github.com/jay-tau/ibkr-fa/issues/265 (``LD`` wash-sale). Please file a new "
    "issue with a redacted statement if the token isn't listed in any of those."
)
_WASH_SALE = (
    "wash-sale loss-disallowed marker ``LD`` is not supported. India has no wash-sale "
    "rule, so the Indian return must use the ACTUAL acquisition cost, but IBKR's ``LD`` "
    "basis is the US wash-sale-adjusted figure (on an opening lot the disallowed loss is "
    "rolled into ``Basis``; on a closing lot the realized P/L is zeroed) and cannot be "
    "reliably reduced to the actual cost from the Activity Statement alone. Refusing "
    "rather than emit a US-tax basis on an Indian return. See "
    "https://github.com/jay-tau/ibkr-fa/issues/265."
)
_DUPLICATE_OP_SYMBOL = (
    "the Symbol appears more than once in Open Positions; refusing to silently overwrite the "
    "earlier close price."
)
_NO_SUMMARY_FOR_LOT = (
    "this Lot row's Symbol has no Open Positions Summary row; refusing rather than dropping a "
    "holding that only the lot detail shows."
)
_LOT_CURRENCY = "this Lot row's Currency differs from its Summary row's Currency; can't reconcile."
_LOT_SIGN = (
    "this Lot row's Quantity has a different sign from its Summary row's Quantity; can't reconcile."
)
_LOT_SUM = (
    "the Lot rows for this Summary row's Symbol don't add up exactly to its Quantity; can't "
    "reconcile."
)


def _direction_tokens(count: int) -> str:
    return (
        "row code must contain exactly one direction token (one of ['C', 'O']); got "
        f"{count}. A Trades row that neither opens nor closes a position (or that claims "
        "to do both) is malformed input — refusing to guess."
    )


def _missing_op_column(column: str) -> str:
    return f"required column {column!r} missing from Open Positions row; can't reconcile."


def _missing_summary_rows(count: int) -> str:
    return (
        f"the Open Positions section has no Summary row for {count} symbol(s) that the "
        "Trades section leaves open; each needs one. No existing row is at fault, so this "
        "points one past the last row. This shouldn't happen — file an issue with a "
        "redacted statement."
    )


def _instruments(*, holder: bool = True) -> dict[Ticker, FinancialInstrument]:
    """``XQAW`` and, unless ``holder`` is false, a stock whose Symbol is a name."""
    rows = [make_fii_row()]
    if holder:
        rows.append(make_fii_row(symbol=HOLDER, security_id="US99990G1085"))
    return build_instrument_lookup(rows)


def _trade(cells: dict[str, str], *, holder: bool = True) -> Callable[[], object]:
    """Parse a valid Trades row, then a row with ``cells`` (row 1)."""
    rows = [make_trade_row(), {**make_trade_row(), **cells}]
    return lambda: parse_activity_statement_trades(
        rows, instruments=_instruments(holder=holder), period=ALL_DATES
    )


def _instrument(row: dict[str, str]) -> Callable[[], object]:
    """Build the lookup from a valid ``XQAW`` row, then ``row`` (row 1)."""
    return lambda: build_instrument_lookup([make_fii_row(), row])


def _close_prices(first: dict[str, str], second: dict[str, str]) -> Callable[[], object]:
    return lambda: build_dec31_close_prices([first, second])


def _summary(symbol: str, **cells: str) -> dict[str, str]:
    return {**_make_op_summary_row(symbol=symbol, close_price="45.60"), **cells}


def _holder_lots(*symbols: str, quantity: str = "10") -> list[OpenLot]:
    """One open lot of ``quantity`` shares per symbol, short if negative."""
    basis = str(Decimal(quantity) * Decimal("41.25"))
    rows = [make_trade_row(symbol=symbol, quantity=quantity, basis=basis) for symbol in symbols]
    open_lots, _closed = match_fifo(
        parse_activity_statement_trades(rows, instruments=_instruments(), period=ALL_DATES),
        dec31_close_prices={Ticker(symbol): Decimal("45.60") for symbol in symbols},
        report_date=date(2025, 12, 31),
    )
    return open_lots


def _reconcile(
    rows: list[dict[str, str]], *symbols: str, quantity: str = "10"
) -> Callable[[], object]:
    """Reconcile one lot of ``quantity`` shares per symbol in ``symbols`` against ``rows``."""
    return lambda: validate_reconciliation(_holder_lots(*symbols, quantity=quantity), rows)


_LOT_QUANTITY = "7.123457"
_HUGE_QUANTITY = "10000000000000000000000000000"
# A 12-digit whole number and the smallest step within the quantity limit
# (12 digits before the point, 20 after it, #1276).
_LARGE_QUANTITY = "100000000000"
_SMALLEST_QUANTITY = "0.00000000000000000001"


def _marked_summary(quantity: str = "10", **cells: str) -> dict[str, str]:
    """A Summary row whose Symbol is a name and whose Currency is an account number."""
    return {**_make_op_row(symbol=HOLDER, quantity=quantity), "Currency": ACCOUNT, **cells}


def _marked_lot(quantity: str = _LOT_QUANTITY, **cells: str) -> dict[str, str]:
    """A Lot row of :func:`_marked_summary`'s Symbol and Currency."""
    return _marked_summary(quantity, DataDiscriminator="Lot", **cells)


def _without(row: dict[str, str], column: str) -> dict[str, str]:
    return {key: value for key, value in row.items() if key != column}


def _after_summary(lot_row: dict[str, str]) -> Callable[[], object]:
    """Reconcile a valid ``XQAW`` Summary row, then ``lot_row`` (row 1)."""
    return _reconcile([_make_op_row(symbol="XQAW", quantity="10"), lot_row], "XQAW")


def _without_digit_limits(raises: Callable[[], object]) -> Callable[[], object]:
    """``raises`` with the reader's read-time digit limits stubbed out (#1276),
    to reach a check that only a number past them can trip.

    ``raising=False`` keeps the stub harmless where the reader has no limit.
    The stub is undone in ``finally``, not by ``MonkeyPatch.context()``: a
    generator context manager can't re-raise a frozen ``ParseError``.
    """

    def call() -> object:
        patch = pytest.MonkeyPatch()
        patch.setattr(
            "ibkr_fa.parser._cells.number_limit_breach",
            lambda _value, _kind: None,
            raising=False,
        )
        try:
            return raises()
        finally:
            patch.undo()

    return call


def _bad_isin() -> None:
    """A lookup built directly, with an account ID as an ISIN (the backstop)."""
    instruments = {
        **_instruments(holder=False),
        Ticker(HOLDER): FinancialInstrument(
            symbol=Ticker(HOLDER),
            description="SAMPLE ISSUER AT INC",
            isin=Isin(ACCOUNT),
            listing_exchange="NASDAQ",
            asset_class="Stocks",
        ),
    }
    parse_activity_statement_trades(
        [make_trade_row(), make_trade_row(symbol=HOLDER)], instruments=instruments, period=ALL_DATES
    )


@dataclass(frozen=True)
class _Case:
    raises: Callable[[], object]
    section: str
    column: str | None
    message: str
    absent: tuple[str, ...] = ()
    """Values beyond the markers that must not appear."""
    row_index: int = 1


_TRADES = "Trades"
_FII = "Financial Instrument Information"
_OP = "Open Positions"
_CASES = {
    # Marker cases: a name or account number reaches the site.
    "decimal-marker": _Case(_trade({"Quantity": HOLDER}), _TRADES, "Quantity", _DECIMAL),
    "date-time-marker": _Case(
        _trade({"Date/Time": HOLDER}),
        _TRADES,
        "Date/Time",
        "must be in 'YYYY-MM-DD, HH:MM:SS' format (with the comma and space)",
    ),
    "close-price-marker": _Case(
        _close_prices(_summary("XQAW"), _summary("XQAX", **{"Close Price": HOLDER})),
        _OP,
        "Close Price",
        _DECIMAL,
    ),
    "open-positions-quantity-marker": _Case(
        _reconcile(
            [
                _make_op_row(symbol="XQAW", quantity="10"),
                _make_op_row(symbol=HOLDER, quantity=HOLDER),
            ],
            "XQAW",
            HOLDER,
        ),
        _OP,
        "Quantity",
        _DECIMAL,
    ),
    "data-discriminator-marker": _Case(
        _trade({"DataDiscriminator": HOLDER}),
        _TRADES,
        "DataDiscriminator",
        "unexpected DataDiscriminator; v1 only handles 'Order' rows. The splitter checks the "
        "'SubTotal' and 'Total' rows and never passes them on.",
    ),
    "code-marker": _Case(
        _trade({"Code": f"O;{HOLDER}"}), _TRADES, "Code", _UNRECOGNISED_TOKEN, (f"O;{HOLDER}",)
    ),
    "underlying-marker": _Case(
        _instrument(
            {**make_fii_row(symbol=TICKER, security_id="US99990G1085"), "Underlying": HOLDER}
        ),
        _FII,
        "Underlying",
        "must match the row's Symbol: a plain stock's Underlying is its own Symbol.",
        ("US99990G1085",),
    ),
    "isin-marker": _Case(
        _instrument({**make_fii_row(symbol=TICKER, security_id="US99990G1085"), "ISIN": HOLDER}),
        _FII,
        "ISIN",
        "must match the row's Security ID: the instrument is identified by its Security ID.",
        ("US99990G1085",),
    ),
    "instrument-duplicate-symbol-marker": _Case(
        lambda: build_instrument_lookup([make_fii_row(symbol=HOLDER), make_fii_row(symbol=HOLDER)]),
        _FII,
        "Symbol",
        "the Symbol appears more than once; refusing to silently overwrite the earlier "
        "entry. This signals either a malformed export or a multi-account statement.",
    ),
    "open-positions-duplicate-symbol-marker": _Case(
        _close_prices(_summary(HOLDER), _summary(HOLDER)), _OP, "Symbol", _DUPLICATE_OP_SYMBOL
    ),
    "symbol-not-in-instruments-marker": _Case(
        _trade({"Symbol": HOLDER}, holder=False),
        _TRADES,
        "Symbol",
        "the Symbol isn't in the Financial Instrument Information section; can't look up "
        "ISIN. Re-export the statement with the Financial Instrument Information section "
        "enabled.",
    ),
    "open-positions-symbol-without-lots-marker": _Case(
        _reconcile(
            [
                _make_op_row(symbol="XQAW", quantity="10"),
                _make_op_row(symbol=HOLDER, quantity="10"),
            ],
            "XQAW",
        ),
        _OP,
        "Symbol",
        "this Summary row's Symbol has no reconstructed open lots from Trades. Possible "
        "causes: the holding was bought before the statement period (re-export the "
        "Activity Statement starting from your account's first trade), trades section is "
        "truncated, or the position came from a transfer-in (see "
        "https://github.com/jay-tau/ibkr-fa/issues/107) rather than a market trade. "
        "Refusing rather than producing partial FA-A3 output.",
    ),
    # Rule cases: a marker would fail an earlier check, so the value breaks the rule.
    "decimal-nan": _Case(
        _trade({"Quantity": "NaN1234567"}), _TRADES, "Quantity", _FINITE, ("NaN1234567", "1234567")
    ),
    "close-price-nan": _Case(
        _close_prices(_summary("XQAW"), _summary(HOLDER, **{"Close Price": "NaN1234567"})),
        _OP,
        "Close Price",
        _FINITE,
        ("NaN1234567", "1234567"),
    ),
    "open-positions-quantity-nan": _Case(
        _reconcile(
            [
                _make_op_row(symbol="XQAW", quantity="10"),
                _make_op_row(symbol=HOLDER, quantity="NaN1234567"),
            ],
            "XQAW",
            HOLDER,
        ),
        _OP,
        "Quantity",
        _FINITE,
        ("NaN1234567", "1234567"),
    ),
    "date-not-a-day": _Case(
        _trade({"Date/Time": "2025-02-30, 09:30:00"}),
        _TRADES,
        "Date/Time",
        "must be a real calendar date",
        ("2025-02-30", "2025", "09:30:00"),
    ),
    "time-not-a-time": _Case(
        _trade({"Date/Time": "2025-03-01, 25:00:00"}),
        _TRADES,
        "Date/Time",
        "must have a real wall-clock time",
        ("2025-03-01", "2025", "25:00:00"),
    ),
    "code-wash-sale": _Case(_trade({"Code": "O;LD"}), _TRADES, "Code", _WASH_SALE, ("O;LD",)),
    "code-two-directions": _Case(
        _trade({"Code": "O;C"}), _TRADES, "Code", _direction_tokens(2), ("O;C",)
    ),
    "code-no-direction": _Case(_trade({"Code": "FP"}), _TRADES, "Code", _direction_tokens(0)),
    "zero-quantity": _Case(
        _trade({"Symbol": HOLDER, "Quantity": "0"}), _TRADES, "Quantity", _ZERO_QUANTITY
    ),
    "basis-sign": _Case(
        _trade({"Symbol": HOLDER, "Quantity": "987654.321", "Basis": "-123456.789"}),
        _TRADES,
        "Basis",
        "has the opposite sign to Quantity — IBKR emits Basis with the same sign as "
        "Quantity (shares * per-share cost for opens; relieved basis for closes). A sign "
        "mismatch indicates a malformed statement.",
        ("987654.321", "123456.789"),
    ),
    "negative-closing-t-price": _Case(
        _trade(
            {
                "Symbol": HOLDER,
                "Code": "C",
                "Quantity": "-10",
                "Basis": "-412.50",
                "T. Price": "-987654.321",
            }
        ),
        _TRADES,
        "T. Price",
        "is negative on a closing trade — a sale price cannot be negative (FA-A3 gross "
        "proceeds is quantity * T.Price, issue #348). A zero price (worthless-security "
        "removal / delisting) is allowed and files gross proceeds 0; a blank price is "
        "already rejected as a malformed decimal.",
        ("987654.321", "412.50"),
    ),
    "reconciliation-mismatch": _Case(
        _reconcile(
            [
                _make_op_row(symbol="XQAW", quantity="10"),
                _make_op_row(symbol=HOLDER, quantity="987654.321"),
            ],
            "XQAW",
            HOLDER,
        ),
        _OP,
        "Quantity",
        _RECONCILIATION,
        ("987654.321", "987644.321"),
    ),
    # Lot rows (#1124). The Symbol is a name, the Currency an account number,
    # and the quantities are distinctive; the field under test keeps its value.
    **{
        f"lot-{column.lower()}-missing": _Case(
            _after_summary(_without(_marked_lot(), column)),
            _OP,
            column,
            _missing_op_column(column),
            () if column == "Quantity" else (_LOT_QUANTITY,),
        )
        for column in ("Symbol", "Quantity", "Currency")
    },
    "lot-symbol-blank": _Case(
        _after_summary(_marked_lot(Symbol="")), _OP, "Symbol", "must be non-empty", (_LOT_QUANTITY,)
    ),
    "lot-currency-blank": _Case(
        _after_summary(_marked_lot(Currency="")),
        _OP,
        "Currency",
        "must be non-empty",
        (_LOT_QUANTITY,),
    ),
    "lot-quantity-malformed": _Case(
        _after_summary(_marked_lot("7,123.457")),
        _OP,
        "Quantity",
        _DECIMAL,
        ("7,123.457", "123.457"),
    ),
    "lot-quantity-nan": _Case(
        _after_summary(_marked_lot("NaN1234567")),
        _OP,
        "Quantity",
        _FINITE,
        ("NaN1234567", "1234567"),
    ),
    "lot-quantity-infinity": _Case(
        _after_summary(_marked_lot("Infinity")), _OP, "Quantity", _FINITE, ("Infinity",)
    ),
    "lot-quantity-minus-infinity": _Case(
        _after_summary(_marked_lot("-Infinity")), _OP, "Quantity", _FINITE, ("Infinity",)
    ),
    "lot-quantity-zero": _Case(_after_summary(_marked_lot("0")), _OP, "Quantity", _ZERO_QUANTITY),
    "lot-without-summary": _Case(
        _after_summary(_marked_lot()), _OP, "Symbol", _NO_SUMMARY_FOR_LOT, (_LOT_QUANTITY,)
    ),
    "lot-currency-mismatch": _Case(
        _reconcile([_marked_summary(), _marked_lot(Currency=CUSIP)], HOLDER),
        _OP,
        "Currency",
        _LOT_CURRENCY,
        (_LOT_QUANTITY,),
    ),
    "lot-sign-mismatch": _Case(
        _reconcile([_marked_summary(), _marked_lot("-7.123457"), _marked_lot("-2.876543")], HOLDER),
        _OP,
        "Quantity",
        _LOT_SIGN,
        ("7.123457", "2.876543"),
    ),
    "lot-of-zero-summary": _Case(
        _reconcile([_marked_summary("0"), _marked_lot()], HOLDER, quantity="0.0001"),
        _OP,
        "Quantity",
        _LOT_SIGN,
        (_LOT_QUANTITY, "0.0001"),
    ),
    "lots-short": _Case(
        _reconcile([_marked_summary(), _marked_lot()], HOLDER),
        _OP,
        "Quantity",
        _LOT_SUM,
        (_LOT_QUANTITY,),
        row_index=0,
    ),
    "lots-over": _Case(
        _reconcile([_marked_summary(), _marked_lot(), _marked_lot()], HOLDER),
        _OP,
        "Quantity",
        _LOT_SUM,
        (_LOT_QUANTITY,),
        row_index=0,
    ),
    # A sum rounded to 28 digits would equal the Summary Quantity: 1E+11 +
    # 1E-20 has 32 digits, each quantity within the quantity limit.
    "lots-beyond-default-precision": _Case(
        _reconcile(
            [
                _marked_summary(_LARGE_QUANTITY),
                _marked_lot(_LARGE_QUANTITY),
                _marked_lot(_SMALLEST_QUANTITY),
            ],
            HOLDER,
            quantity=_LARGE_QUANTITY,
        ),
        _OP,
        "Quantity",
        _LOT_SUM,
        (_LARGE_QUANTITY, _SMALLEST_QUANTITY),
        row_index=0,
    ),
    # Past the Decimal exponent range: since #1276 the Lot row's read-time
    # digit limit refuses it first, as
    # test_a_quantity_past_its_digit_limit_refuses_before_reconciling pins, so
    # the case stubs the limit out to reach the sum.
    "lots-too-large-to-add": _Case(
        _without_digit_limits(_reconcile([_marked_summary(), _marked_lot("1E+1000000")], HOLDER)),
        _OP,
        "Quantity",
        "the Lot rows for this Summary row's Symbol can't be added up exactly, so they can't "
        "be checked against its Quantity.",
        ("1E+1000000", "1000000"),
        row_index=0,
    ),
    # Backstops: reached only by calling the function directly.
    # The pipeline's price reader refuses a second Summary row and a
    # Currency that isn't USD before reconciliation runs.
    "lot-summary-twice": _Case(
        _reconcile([_marked_summary(), _marked_summary(), _marked_lot()], HOLDER),
        _OP,
        "Symbol",
        _DUPLICATE_OP_SYMBOL,
        (_LOT_QUANTITY,),
    ),
    "lot-summary-currency-missing": _Case(
        _reconcile([_without(_marked_summary(), "Currency"), _marked_lot()], HOLDER),
        _OP,
        "Currency",
        _missing_op_column("Currency"),
        (_LOT_QUANTITY,),
        row_index=0,
    ),
    "lot-summary-currency-blank": _Case(
        _reconcile([_marked_summary(Currency=""), _marked_lot()], HOLDER),
        _OP,
        "Currency",
        "must be non-empty",
        (_LOT_QUANTITY,),
        row_index=0,
    ),
    "lots-without-open-positions-row": _Case(
        _reconcile(
            [
                _make_op_row(symbol="XQAW", quantity="10"),
                {**_make_op_row(symbol="XQAW", quantity="10"), "DataDiscriminator": "Lot"},
            ],
            "XQAW",
            HOLDER,
        ),
        _OP,
        None,
        _missing_summary_rows(1),
        row_index=2,
    ),
    "isin-shape-backstop": _Case(
        _bad_isin,
        _TRADES,
        "Symbol",
        "the instrument's ISIN doesn't match ISO 6166 shape (12 chars: 2 uppercase "
        "letters + 9 alphanumerics + 1 check digit). IBKR may have changed the Financial "
        "Instrument Information schema, or the Security ID column is truncated. "
        "build_instrument_lookup validates this at parse time via validate_isin_or_raise "
        "(#150); reaching this defence-in-depth check means the FinancialInstrument was "
        "constructed directly (bypassing the parser).",
    ),
}


@pytest.mark.parametrize("case", _CASES.values(), ids=_CASES.keys())
def test_refusal_names_the_cell_without_its_value(case: _Case) -> None:
    with pytest.raises(ParseError) as exc:
        case.raises()
    error = exc.value
    assert (error.section, error.row_index, error.column) == (
        case.section,
        case.row_index,
        case.column,
    )
    assert error.message == case.message
    assert error.raw_value == ""
    assert_no_markers(str(error), *MARKERS, *case.absent)


@pytest.mark.parametrize(
    ("rows", "row_index"),
    [
        pytest.param(
            [_marked_summary(f"{_HUGE_QUANTITY}.7242"), _marked_lot()],
            0,
            id="summary-29-whole-digits",
        ),
        pytest.param(
            [_marked_summary(), _marked_lot(f"{_HUGE_QUANTITY}.7242")],
            1,
            id="lot-29-whole-digits",
        ),
        pytest.param(
            [_marked_summary(), _marked_lot("7242E+1000000")], 1, id="lot-past-the-exponent-range"
        ),
    ],
)
def test_a_quantity_past_its_digit_limit_refuses_before_reconciling(
    rows: list[dict[str, str]], row_index: int
) -> None:
    """#1276: the Open Positions quantities the lots-beyond-default-precision
    and lots-too-large-to-add cases once read refuse when their row is read,
    with the quantity limit and no value, before any reconciliation."""
    with pytest.raises(ParseError) as exc:
        _reconcile(rows, HOLDER)()
    assert_unchained_refusal(
        exc.value,
        ParseError(
            section=_OP, row_index=row_index, column="Quantity", message=QUANTITY, raw_value=""
        ),
        f"ParseError in section {_OP!r} at row {row_index}, column 'Quantity': {QUANTITY}",
        markers=("7242",),
    )


def test_the_no_echo_cases_start_from_rows_that_parse() -> None:
    """Every case's row 0, and the name-shaped Symbol on its own, are valid."""
    assert (
        len(
            parse_activity_statement_trades(
                [make_trade_row(), make_trade_row(symbol=HOLDER)],
                instruments=_instruments(),
                period=ALL_DATES,
            )
        )
        == 2
    )
    assert build_dec31_close_prices([_summary("XQAW"), _summary(HOLDER)]) == {
        Ticker("XQAW"): Decimal("45.60"),
        Ticker(HOLDER): Decimal("45.60"),
    }
    validate_reconciliation(
        _holder_lots("XQAW", HOLDER),
        [_make_op_row(symbol="XQAW", quantity="10"), _make_op_row(symbol=HOLDER, quantity="10")],
    )
    validate_reconciliation(
        _holder_lots(HOLDER), [_marked_summary(), _marked_lot(), _marked_lot("2.876543")]
    )


# ---- Open Positions row kinds (#1093) ----------------------------------------

_KIND_MARKER = "Synthetic Marker Q"
_UNKNOWN_KIND = (
    "must be 'Summary' or 'Lot', the only Open Positions row kinds this tool reads; another "
    "kind could hold a holding no check sees. Refusing rather than skipping it. If IBKR now "
    "emits this, please file an issue at https://github.com/jay-tau/ibkr-fa/issues naming it."
)


@pytest.mark.parametrize("discriminator", ["NewSummary", "", HOLDER])
@pytest.mark.parametrize("reader", ["close prices", "reconciliation"])
def test_open_positions_refuse_a_row_of_an_unknown_kind(discriminator: str, reader: str) -> None:
    """#1093: both readers skipped every row that wasn't a Summary row, so a
    holding in a row of another kind passed no check. Only Lot rows are
    skipped now. The row's Symbol and Currency carry markers."""
    row = {
        **_make_op_summary_row(symbol=HOLDER, close_price="45.60"),
        "DataDiscriminator": discriminator,
        "Currency": _KIND_MARKER,
    }
    if reader == "close prices":
        raises = _close_prices(_summary("XQAW"), row)
    else:
        raises = _reconcile([_make_op_row(symbol="XQAW", quantity="10"), row], "XQAW")
    with pytest.raises(ParseError) as exc:
        raises()
    error = exc.value
    assert (error.section, error.row_index, error.column, error.message, error.raw_value) == (
        "Open Positions",
        1,
        "DataDiscriminator",
        _UNKNOWN_KIND,
        "",
    )
    assert_no_markers(str(error), HOLDER, _KIND_MARKER, *filter(None, [discriminator]))


# ---- Open Positions Lot rows (#1124) -----------------------------------------


@pytest.mark.parametrize(
    ("rows", "symbols", "quantity"),
    [
        pytest.param(
            [
                _lot(symbol="XQAW", quantity="4"),
                _make_op_row(symbol="XQAW", quantity="10"),
                _lot(symbol="XQAW", quantity="6"),
            ],
            ("XQAW",),
            "10",
            id="long-lots-before-and-after-their-summary",
        ),
        pytest.param(
            [
                _make_op_row(symbol="XQAW", quantity="-10"),
                _lot(symbol="XQAW", quantity="-4"),
                _lot(symbol="XQAW", quantity="-6"),
            ],
            ("XQAW",),
            "-10",
            id="short",
        ),
        pytest.param(
            [
                _lot(symbol="XQAW", quantity="4.25"),
                _lot(symbol="XQAW", quantity="6.25"),
                _make_op_row(symbol="XQAW", quantity="10.5"),
            ],
            ("XQAW",),
            "10.5",
            id="fractional",
        ),
        pytest.param(
            [
                _make_op_row(symbol="XQAW", quantity="10"),
                _lot(symbol=HOLDER, quantity="3"),
                _lot(symbol="XQAW", quantity="10"),
                _make_op_row(symbol=HOLDER, quantity="10"),
                _lot(symbol=HOLDER, quantity="7"),
            ],
            ("XQAW", HOLDER),
            "10",
            id="several-symbols",
        ),
        pytest.param(
            [
                _make_op_row(symbol="XQAW", quantity="10"),
                _make_op_row(symbol=HOLDER, quantity="10"),
                _lot(symbol=HOLDER, quantity="3"),
                _lot(symbol=HOLDER, quantity="7"),
            ],
            ("XQAW", HOLDER),
            "10",
            id="a-summary-row-without-lots",
        ),
    ],
)
def test_lot_rows_that_split_their_summary_rows_change_nothing(
    rows: list[dict[str, str]], symbols: tuple[str, ...], quantity: str
) -> None:
    """Reconciliation passes, and the close prices are the Summary rows' alone."""
    summary_rows = [row for row in rows if row["DataDiscriminator"] == "Summary"]
    validate_reconciliation(_holder_lots(*symbols, quantity=quantity), rows)
    assert build_dec31_close_prices(rows) == build_dec31_close_prices(summary_rows)


@pytest.mark.parametrize(
    ("rows", "symbols", "position", "message"),
    [
        # Every earlier refusal wins over a Lot row with a blank Symbol.
        pytest.param(
            [_marked_lot(Symbol=""), _make_op_row(symbol="XQAW", quantity="7")],
            ("XQAW",),
            (1, "Quantity"),
            _RECONCILIATION,
            id="summary-quantity-mismatch",
        ),
        pytest.param(
            [
                _make_op_row(symbol="XQAW", quantity="10"),
                _marked_lot(Symbol=""),
                {**_make_op_row(symbol="XQAW", quantity="10"), "DataDiscriminator": "NewSummary"},
            ],
            ("XQAW",),
            (2, "DataDiscriminator"),
            _UNKNOWN_KIND,
            id="later-unknown-kind",
        ),
        pytest.param(
            [_marked_lot(Symbol="")],
            ("XQAW",),
            (1, None),
            _missing_summary_rows(1),
            id="missing-summary-row",
        ),
        # Each pass wins over a later pass's fault at an earlier row.
        pytest.param(
            [_marked_summary(), _marked_summary(), _marked_lot(), _marked_lot("0")],
            (HOLDER,),
            (3, "Quantity"),
            _ZERO_QUANTITY,
            id="pass-1-before-pass-2",
        ),
        pytest.param(
            [_lot(symbol="XQAX", quantity="4"), _marked_summary(Currency=""), _marked_lot()],
            (HOLDER,),
            (1, "Currency"),
            "must be non-empty",
            id="pass-2-before-pass-3",
        ),
        pytest.param(
            [
                _marked_summary(),
                _marked_lot(),
                _make_op_row(symbol="XQAW", quantity="10"),
                {**_lot(symbol="XQAW", quantity="10"), "Currency": CUSIP},
            ],
            (HOLDER, "XQAW"),
            (3, "Currency"),
            _LOT_CURRENCY,
            id="pass-3-before-pass-4",
        ),
    ],
)
def test_lot_checks_run_last_and_in_pass_order(
    rows: list[dict[str, str]],
    symbols: tuple[str, ...],
    position: tuple[int, str | None],
    message: str,
) -> None:
    with pytest.raises(ParseError) as exc:
        validate_reconciliation(_holder_lots(*symbols), rows)
    error = exc.value
    assert (error.section, error.row_index, error.column, error.message, error.raw_value) == (
        "Open Positions",
        *position,
        message,
        "",
    )
