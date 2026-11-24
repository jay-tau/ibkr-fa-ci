"""Flex and shared IBKR parser refusals echo no cell (#1038).

A column-shifted or malformed row can put the holder's name or account
number in any cell, and errors end up in screenshots and pasted issues. So
cell refusals here name their section, column and row and the rule the cell
broke. Whole-header and unknown-format refusals locate the record with
``column=None``. Messages are fixed text with an empty ``raw_value``. Each
case pins the exact message and checks that identifying values don't reach
``str(error)``, except permitted code constants.
Every row here is synthetic.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path

import pytest

from ibkr_fa.exceptions import ParseError
from ibkr_fa.parser import parse_closed_lots, parse_open_positions, split_sections
from ibkr_fa.parser.format_detect import detect_ibkr_csv_format
from tests._flex_lot_rows import FLEX_CLOSED_LOT_XQAT as _CLOSED_LOT
from tests._flex_lot_rows import FLEX_OPEN_LOT_XQAW as _OPEN_LOT
from tests._flex_lot_rows import with_closing_executions
from tests._issuer_country_refusal import UNMAPPED_COUNTRY_RULE as _UNMAPPED

_MARKERS = ("Synthetic Holder Two", "U1234567", "ANN")

# The cells that identify the lot or carry its figures and dates.
_OPEN_LOT_VALUES = tuple(
    _OPEN_LOT[column]
    for column in (
        "Symbol",
        "Description",
        "CUSIP",
        "ISIN",
        "ReportDate",
        "Quantity",
        "MarkPrice",
        "PositionValue",
        "CostBasisPrice",
        "CostBasisMoney",
        "OpenDateTime",
    )
)
_CLOSED_LOT_VALUES = tuple(
    _CLOSED_LOT[column]
    for column in (
        "Symbol",
        "Description",
        "CUSIP",
        "ISIN",
        "ReportDate",
        "Quantity",
        "TradePrice",
        "CostBasis",
        "FifoPnlRealized",
        "OpenDateTime",
    )
)

_DECIMAL = "must be a decimal number"
_FINITE = "must be a finite decimal"


def _assert_no_echo(text: str, values: Iterable[str]) -> None:
    """Assert no marker or test value is in ``text``, ignoring case.

    A value of three characters or fewer is matched as a whole token, so
    fixed text such as "cannot" doesn't trip ``ANN``.
    """
    for value in (*_MARKERS, *values):
        if len(value) <= 3:
            token = rf"(?<![0-9A-Za-z]){re.escape(value)}(?![0-9A-Za-z])"
            assert re.search(token, text, re.IGNORECASE) is None, value
        else:
            assert value.casefold() not in text.casefold(), value


def _open_lot(**cells: str) -> None:
    parse_open_positions([_OPEN_LOT, {**_OPEN_LOT, **cells}])


def _closed_lot(**cells: str) -> None:
    parse_closed_lots([_CLOSED_LOT, {**_CLOSED_LOT, **cells}])


@dataclass(frozen=True)
class _Row:
    parse: Callable[..., None]
    section: str
    values: tuple[str, ...]


_OPEN = _Row(_open_lot, "Open Positions (LOT)", _OPEN_LOT_VALUES)
_CLOSED = _Row(_closed_lot, "Trades", _CLOSED_LOT_VALUES)


@dataclass(frozen=True)
class _Case:
    row: _Row
    cells: dict[str, str]
    column: str
    message: str
    absent: tuple[str, ...] = ()
    """More values that must not appear, such as a computed product."""
    named: str = ""
    """A cell value the message names because it is the code's own constant."""


_CASES = {
    # Marker cases: a name or account number reaches the site.
    "open-quantity-marker": _Case(
        _OPEN, {"Quantity": "Synthetic Holder Two"}, "Quantity", _DECIMAL
    ),
    "open-side-marker": _Case(
        _OPEN, {"Side": "Synthetic Holder Two"}, "Side", "must be one of: Long, Short"
    ),
    "closed-cost-basis-marker": _Case(
        _CLOSED, {"CostBasis": "Synthetic Holder Two"}, "CostBasis", _DECIMAL
    ),
    "closed-level-of-detail-marker": _Case(
        _CLOSED,
        {"LevelOfDetail": "Synthetic Holder Two"},
        "LevelOfDetail",
        "unrecognised LevelOfDetail; expected one of 'CLOSED_LOT' or 'EXECUTION'. An empty "
        "value or unknown row type indicates Flex Query schema drift; please open an issue "
        "with a redacted sample row.",
    ),
    "closed-buy-sell-marker": _Case(
        _CLOSED,
        {"Buy/Sell": "Synthetic Holder Two"},
        "Buy/Sell",
        "unrecognised Buy/Sell on CLOSED_LOT row; expected 'SELL' (long-side close).",
    ),
    "open-report-date-marker": _Case(
        _OPEN,
        {"ReportDate": "Synthetic Holder Two"},
        "ReportDate",
        "must be exactly 8 digits in YYYYMMDD format",
    ),
    "closed-open-date-time-marker": _Case(
        _CLOSED,
        {"OpenDateTime": "Synthetic Holder Two"},
        "OpenDateTime",
        "must be in YYYYMMDD;HHMMSS format (e.g. '20250610;143015')",
    ),
    "open-isin-marker": _Case(
        _OPEN,
        {"ISIN": "Synthetic Holder Two"},
        "ISIN",
        "the ISIN doesn't match ISO 6166 shape (12 chars: 2 uppercase letters + 9 "
        "alphanumerics + 1 digit). IBKR may have changed the schema, or the cell is "
        "corrupted. See https://github.com/jay-tau/ibkr-fa/issues/150 for the validation "
        "contract.",
    ),
    "open-issuer-country-marker": _Case(
        _OPEN, {"IssuerCountryCode": "Synthetic Holder Two"}, "IssuerCountryCode", _UNMAPPED
    ),
    "open-issuer-country-short-name": _Case(
        _OPEN, {"IssuerCountryCode": "ANN"}, "IssuerCountryCode", _UNMAPPED
    ),
    "open-shifted-holding-period-marker-symbol": _Case(
        _OPEN,
        {"Symbol": "Synthetic Holder Two", "HoldingPeriodDateTime": "20250313"},
        "HoldingPeriodDateTime",
        "HoldingPeriodDateTime differs from OpenDateTime. IBKR documents a shifted "
        "holding-period date for US wash sales, so CostBasisMoney may include a US "
        "wash-sale adjustment that the Indian return does not recognise. Re-run "
        "schedule-fa with an IBKR Activity Statement that covers this lot's acquisition; "
        "it uses the actual opening-trade cost. LD-coded (wash-sale) Activity Statement "
        "rows are still refused there (https://github.com/jay-tau/ibkr-fa/issues/265).",
        ("20250313", "2025-03-13", "2025-03-12"),
    ),
    # Rule cases: a marker would fail an earlier check, so the value breaks the rule.
    "open-quantity-nan": _Case(
        _OPEN, {"Quantity": "NaN1234567"}, "Quantity", _FINITE, ("NaN1234567", "1234567")
    ),
    "open-quantity-negative-infinity": _Case(
        _OPEN, {"Quantity": "-Infinity"}, "Quantity", _FINITE, ("-Infinity",)
    ),
    "closed-cost-basis-nan": _Case(
        _CLOSED, {"CostBasis": "NaN1234567"}, "CostBasis", _FINITE, ("NaN1234567", "1234567")
    ),
    "closed-cost-basis-negative-infinity": _Case(
        _CLOSED, {"CostBasis": "-Infinity"}, "CostBasis", _FINITE, ("-Infinity",)
    ),
    "open-position-value-off": _Case(
        _OPEN,
        {"Quantity": "3", "MarkPrice": "7", "PositionValue": "1234567.89"},
        "PositionValue",
        "must equal Quantity * MarkPrice within 0.01",
        ("3", "7", "21", "1234567.89"),
    ),
    "open-cost-basis-price-negative": _Case(
        _OPEN,
        {"CostBasisPrice": "-1234567.89"},
        "CostBasisPrice",
        "must be non-negative",
        ("1234567.89",),
    ),
    "open-report-date-not-a-day": _Case(
        _OPEN,
        {"ReportDate": "20250230"},
        "ReportDate",
        "must be a real calendar date",
        ("20250230", "2025-02-30", "2025"),
    ),
    "closed-open-date-time-not-a-time": _Case(
        _CLOSED,
        {"OpenDateTime": "20250610;250000"},
        "OpenDateTime",
        "must have a real wall-clock time in HHMMSS",
        ("20250610", "250000", "2025-06-10"),
    ),
    "open-issuer-country-india": _Case(
        _OPEN,
        {"ISIN": "US9999999993", "IssuerCountryCode": "IN"},
        "IssuerCountryCode",
        "the issuer country is 'IN' (India). ibkr-fa v1 handles foreign-issued securities "
        "only: Schedule FA reports FOREIGN assets for an Indian tax resident, and "
        "Indian-issued securities do not belong on it regardless of which broker holds "
        "them. If you reached this via a non-IBKR-US export, please file an issue at "
        "https://github.com/jay-tau/ibkr-fa/issues.",
        ("US9999999993",),
        named="IN",
    ),
    "open-issuer-country-not-isin-prefix": _Case(
        _OPEN,
        {"ISIN": "US9999999993", "IssuerCountryCode": "CA"},
        "IssuerCountryCode",
        "the issuer country differs from the ISIN's country prefix. Which of the two "
        "Schedule FA Table A3 should report is unresolved, so ibkr-fa refuses the lot; see "
        "https://github.com/jay-tau/ibkr-fa/issues/794.",
        ("US9999999993", "CA", "US"),
    ),
    "open-acquired-before-ttbr-archive": _Case(
        _OPEN,
        {"OpenDateTime": "20191231", "HoldingPeriodDateTime": "20191231"},
        "OpenDateTime",
        "lot acquired before 2020-01-06 — the bundled SBI TTBR archive does not cover this "
        "date. Pre-2020 acquisitions are unsupported in v1; see "
        "https://github.com/jay-tau/ibkr-fa/issues/52 for FX-history extension.",
        ("20191231", "2019-12-31", "2019"),
    ),
}


@pytest.mark.parametrize("case", _CASES.values(), ids=_CASES.keys())
def test_lot_refusal_names_the_cell_without_its_value(case: _Case) -> None:
    with pytest.raises(ParseError) as exc:
        case.row.parse(**case.cells)
    error = exc.value
    assert (error.section, error.row_index, error.column) == (case.row.section, 1, case.column)
    assert error.message == case.message
    assert error.raw_value == ""
    cells = [value for value in case.cells.values() if value != case.named]
    _assert_no_echo(str(error), (*case.row.values, *cells, *case.absent))


def test_valid_lots_still_parse() -> None:
    assert len(parse_open_positions([_OPEN_LOT, _OPEN_LOT])) == 2
    assert len(parse_closed_lots(with_closing_executions(_CLOSED_LOT, _CLOSED_LOT))) == 2


@pytest.mark.parametrize(
    "first_cell",
    [
        pytest.param("Synthetic Holder Two", id="name"),
        pytest.param("U1234567", id="account"),
        pytest.param("ANN", id="short-name"),
    ],
)
@pytest.mark.parametrize(
    ("prefix", "record_index"),
    [
        pytest.param("", 0, id="first-record"),
        pytest.param("\n\n", 2, id="after-two-blank-records"),
    ],
)
def test_unknown_format_names_the_record_without_its_cells(
    tmp_path: Path, first_cell: str, prefix: str, record_index: int
) -> None:
    """The refusal keeps the typed path and the sentinels, and reports the
    zero-based index of the first non-blank record it checked. Not even a
    masked account ID's last four digits appear."""
    path = tmp_path / "export.csv"
    path.write_text(f"{prefix}{first_cell},Sample Holder,Individual\n", encoding="utf-8")
    with pytest.raises(ParseError) as exc:
        detect_ibkr_csv_format(path)
    error = exc.value
    assert (error.section, error.row_index, error.column) == ("<file>", record_index, None)
    assert error.message == (
        f"input CSV {path}'s first non-blank record doesn't match a known IBKR export "
        "format. Expected one of:\n"
        "  * Activity Statement: first two cells ('Statement', 'Header'); or\n"
        "  * Flex Query: first cell in ['BOF', 'ClientAccountID', 'CurrencyPrimary']."
    )
    assert error.raw_value == ""
    _assert_no_echo(str(error).replace(str(path), ""), ("Sample Holder", "Individual", "4567"))


_OPEN_POSITIONS_HEADER = ",".join(_OPEN_LOT)


@pytest.mark.parametrize(
    ("header", "message"),
    [
        pytest.param(
            f"{_OPEN_POSITIONS_HEADER},Synthetic Holder Two",
            "header row looks like a changed 'Open Positions (LOT)' section header but "
            "matches none of its supported column sets; against the nearest one it has "
            "extra 1 column(s) not in any known section and lacks column(s) []. Fix those "
            "columns in the Flex Query, or, if this is a different section, remove it from "
            "the query or report it",
            id="near-miss",
        ),
        pytest.param(
            f"{_OPEN_POSITIONS_HEADER},Synthetic Holder Two,Synthetic Holder Two",
            "header row looks like the 'Open Positions (LOT)' section but repeats 1 column(s) "
            "not in any known section; re-export the Flex Query with each column once",
            id="repeated-column",
        ),
        pytest.param(
            f"{_OPEN_POSITIONS_HEADER},Symbol",
            "header row looks like the 'Open Positions (LOT)' section but repeats column(s) "
            "['Symbol']; re-export the Flex Query with each column once",
            id="repeated-known-column",
        ),
    ],
)
def test_header_refusal_counts_unknown_columns(tmp_path: Path, header: str, message: str) -> None:
    """A whole-header refusal names only columns from the signature tables."""
    path = tmp_path / "flex.csv"
    path.write_text(f"{header}\n", encoding="utf-8")
    with pytest.raises(ParseError) as exc:
        split_sections(path)
    error = exc.value
    assert (error.section, error.row_index, error.column) == ("<file>", 0, None)
    assert error.message == message
    assert error.raw_value == ""
    _assert_no_echo(str(error), ())
