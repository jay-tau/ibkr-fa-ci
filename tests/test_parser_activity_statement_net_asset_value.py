"""Tests for :mod:`ibkr_fa.parser.activity_statement.net_asset_value`.

The synthetic sections below follow IBKR's Activity Statement NAV layout: the
two-block ``Net Asset Value`` section (asset-class table + Time-Weighted Rate
of Return block), the ``Total`` line as an ordinary ``Data`` row, the trailing
space in ``"Cash "``, and the NAV-total ⇄ Change-in-NAV ``Ending Value``
cross-check.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path

import pytest

from ibkr_fa.exceptions import ParseError
from ibkr_fa.models.net_asset_value import NavRefusalReason
from ibkr_fa.parser.activity_statement.multi_section import (
    KnownActivitySection,
    Section,
    split_activity_statement_sections,
)
from ibkr_fa.parser.activity_statement.net_asset_value import parse_net_asset_value
from tests._activity_statement_blocks import _NAV_COLS
from tests._activity_statement_blocks import change_in_nav_block as _change_block
from tests._activity_statement_blocks import nav_block as _nav_block
from tests._activity_statement_blocks import nav_row as _nav_row
from tests._activity_statement_blocks import twr_block as _twr_block
from tests._crash_reports import assert_no_markers, assert_unchained_refusal, render_without_frames
from tests._number_limits import MONEY

# The two contradictions that stop the run (#1309).
_REMEDY = (
    "The statement contradicts itself, so Schedule FA Table A2's closing balance can't be "
    "filed. Re-download the Activity Statement from IBKR or fix the file, then re-run."
)
_TOTAL_BREAKDOWN_MISMATCH = (
    "the asset-class rows' Current Total amounts don't add up to this Total row's Current "
    f"Total within 0.01, or there are no asset-class rows and the Total isn't zero. {_REMEDY}"
)
_ENDING_VALUE_MISMATCH = (
    "this Ending Value differs by more than 0.01 from the Net Asset Value figure (its Total "
    "row's Current Total or, with no Total row, the sum of its asset-class rows). "
    f"{_REMEDY}"
)


def _breakdown_error(total_row: int) -> ParseError:
    """The Total row at ``total_row`` disagrees with its asset-class rows."""
    return ParseError(
        section="Net Asset Value",
        row_index=total_row,
        column="Current Total",
        message=_TOTAL_BREAKDOWN_MISMATCH,
        raw_value="",
    )


def _ending_value_error(ending_row: int) -> ParseError:
    """The Ending Value row at ``ending_row`` disagrees with the NAV figure."""
    return ParseError(
        section="Change in NAV",
        row_index=ending_row,
        column="Field Value",
        message=_ENDING_VALUE_MISMATCH,
        raw_value="",
    )


def _assert_refuses_with(
    nav: list[Section],
    change: list[Section] | None,
    expected: ParseError,
    markers: tuple[str, ...] = (),
) -> None:
    """``parse_net_asset_value`` raises ``expected``, with no value and no chain."""
    with pytest.raises(ParseError) as exc:
        parse_net_asset_value(nav, change)
    assert_unchained_refusal(
        exc.value,
        expected,
        f"ParseError in section {expected.section!r} at row {expected.row_index}, "
        f"column {expected.column!r}: {expected.message}",
        markers=markers,
    )


# ---- happy path -----------------------------------------------------------


def test_total_row_with_crosscheck() -> None:
    nav = [
        _nav_block(
            [
                _nav_row("Cash ", "42.518346"),
                _nav_row("Stock", "81.20"),
                _nav_row("Total", "123.718346"),
            ]
        ),
        _twr_block(),
    ]
    change = [
        _change_block(
            [
                ("Starting Value", "0"),
                ("Deposits & Withdrawals", "125"),
                ("Commissions", "-1.281654"),
                ("Ending Value", "123.718346"),
            ]
        )
    ]
    result = parse_net_asset_value(nav, change)
    assert result.closing_total == Decimal("123.718346")
    assert result.refusal_reason is None
    assert result.cross_checked is True


def test_asset_class_breakdown_captured_excluding_total_row() -> None:
    """The per-asset-class Current Total is captured (key stripped of IBKR's
    trailing space), EXCLUDING the 'Total' row — for the A2 peak cash
    reference (Rule 114F)."""
    nav = [
        _nav_block(
            [
                _nav_row("Cash ", "42.518346"),
                _nav_row("Stock", "81.20"),
                _nav_row("Total", "123.718346"),
            ]
        ),
        _twr_block(),
    ]
    result = parse_net_asset_value(nav, None)
    assert result.asset_class_current_totals == {
        "Cash": Decimal("42.518346"),
        "Stock": Decimal("81.20"),
    }
    assert "Total" not in result.asset_class_current_totals


def test_asset_class_breakdown_empty_on_refusal() -> None:
    """A refusal (no asset-class block) carries an empty breakdown."""
    result = parse_net_asset_value(None, None)
    assert result.closing_total is None
    assert result.asset_class_current_totals == {}


def test_change_in_nav_component_rows_are_read_but_not_filed() -> None:
    """Multi-nature shape: Dividends + Interest rows are checked, and the
    printed Total, confirmed by the Ending Value, is what's filed."""
    nav = [
        _nav_block(
            [
                _nav_row("Cash", "436.2504187"),
                _nav_row("Stock", "18000"),
                _nav_row("Total", "18436.2504187"),
            ]
        )
    ]
    change = [
        _change_block(
            [
                ("Dividends", "3.2"),
                ("Interest", "0.95"),
                ("Ending Value", "18436.2504187"),
            ]
        )
    ]
    result = parse_net_asset_value(nav, change)
    assert result.closing_total == Decimal("18436.2504187")
    assert result.cross_checked is True


def test_nav_layout_through_the_section_splitter(tmp_path: Path) -> None:
    """Integration: a synthetic CSV in IBKR's NAV layout, run through the
    actual section splitter, parses to the cross-checked closing NAV — proving
    the trailing-space 'Cash ', the Total-as-Data-row, and the two-block NAV
    survive the splitter path (not just hand-built Section objects)."""
    csv_text = (
        "Net Asset Value,Header,Asset Class,Prior Total,Current Long,"
        "Current Short,Current Total,Change\n"
        "Net Asset Value,Data,Cash ,0,42.518346,0,42.518346,42.518346\n"
        "Net Asset Value,Data,Stock,0,81.20,0,81.20,81.20\n"
        "Net Asset Value,Data,Total,0,123.718346,0,123.718346,123.718346\n"
        "Net Asset Value,Header,Time Weighted Rate of Return\n"
        "Net Asset Value,Data,-1.05%\n"
        "Change in NAV,Header,Field Name,Field Value\n"
        "Change in NAV,Data,Starting Value,0\n"
        "Change in NAV,Data,Ending Value,123.718346\n"
    )
    path = tmp_path / "stmt.csv"
    path.write_text(csv_text, encoding="utf-8")
    sections = split_activity_statement_sections(path)
    result = parse_net_asset_value(
        sections.get(KnownActivitySection.NET_ASSET_VALUE),
        sections.get(KnownActivitySection.CHANGE_IN_NAV),
    )
    assert result.closing_total == Decimal("123.718346")
    assert result.cross_checked is True


def test_no_total_row_files_the_printed_ending_value() -> None:
    nav = [_nav_block([_nav_row("Cash ", "30"), _nav_row("Stock", "70")])]
    result = parse_net_asset_value(nav, [_change_block([("Ending Value", "100")])])
    assert result.closing_total == Decimal("100")
    assert result.refusal_reason is None
    assert result.cross_checked is True


def test_no_total_row_files_the_ending_value_not_the_sum() -> None:
    """#906: a sum within tolerance confirms the printed figure; the parser
    files the printed 100, never its own 99.995."""
    nav = [_nav_block([_nav_row("Cash ", "30"), _nav_row("Stock", "69.995")])]
    result = parse_net_asset_value(nav, [_change_block([("Ending Value", "100")])])
    assert result.closing_total == Decimal("100")
    assert result.cross_checked is True


def test_no_total_row_without_an_ending_value_refuses() -> None:
    """#906: without a Total row, the asset-class sum alone is unconfirmed."""
    nav = [_nav_block([_nav_row("Cash ", "30"), _nav_row("Stock", "70")])]
    result = parse_net_asset_value(nav, None)
    assert result.closing_total is None
    assert result.refusal_reason is NavRefusalReason.NO_ENDING_VALUE


def test_no_total_row_with_a_blank_asset_class_amount_refuses() -> None:
    """#906: a blank row would leave the sum partial (Cash 50 alone)."""
    nav = [_nav_block([_nav_row("Cash ", "50"), _nav_row("Stock", "")])]
    result = parse_net_asset_value(nav, [_change_block([("Ending Value", "50")])])
    assert result.closing_total is None
    assert result.refusal_reason is NavRefusalReason.BLANK_ASSET_CLASS_AMOUNT


def test_no_total_row_sum_disagreeing_with_the_ending_value_refuses() -> None:
    """#1309: the rows' sum (100) contradicts the Ending Value (200)."""
    nav = [_nav_block([_nav_row("Cash ", "30"), _nav_row("Stock", "70")])]
    change = [_change_block([("Starting Value", "0"), ("Ending Value", "200")])]
    _assert_refuses_with(nav, change, _ending_value_error(1))


@pytest.mark.parametrize("with_total_row", [True, False], ids=["total-row", "no-total-row"])
@pytest.mark.parametrize("endings", [("200", "100"), ("100", "100")], ids=["unequal", "equal"])
@pytest.mark.parametrize("split", [False, True], ids=["one-block", "two-blocks"])
def test_more_than_one_ending_value_refuses(
    with_total_row: bool, endings: tuple[str, str], split: bool
) -> None:
    """#906: Ending Value rows are counted across every Change in NAV block;
    a second one is ambiguous even when it agrees, with or without a Total row."""
    rows = [_nav_row("Cash ", "30"), _nav_row("Stock", "70")]
    if with_total_row:
        rows.append(_nav_row("Total", "100"))
    pairs = [("Ending Value", value) for value in endings]
    change = (
        [_change_block(pairs[:1]), _change_block(pairs[1:])] if split else [_change_block(pairs)]
    )
    result = parse_net_asset_value([_nav_block(rows)], change)
    assert result.closing_total is None
    assert result.refusal_reason is NavRefusalReason.MULTIPLE_ENDING_VALUES


@pytest.mark.parametrize("with_total_row", [True, False], ids=["total-row", "no-total-row"])
def test_blank_ending_value_refuses(with_total_row: bool) -> None:
    """A printed but blank Ending Value can neither be filed nor confirm a Total."""
    rows = [_nav_row("Cash ", "30"), _nav_row("Stock", "70")]
    if with_total_row:
        rows.append(_nav_row("Total", "100"))
    result = parse_net_asset_value([_nav_block(rows)], [_change_block([("Ending Value", " ")])])
    assert result.closing_total is None
    assert result.refusal_reason is NavRefusalReason.BLANK_ENDING_VALUE


def test_malformed_asset_class_amount_without_total_row_raises() -> None:
    nav = [_nav_block([_nav_row("Cash ", "30"), _nav_row("Stock", "N/A")])]
    with pytest.raises(ParseError) as exc:
        parse_net_asset_value(nav, [_change_block([("Ending Value", "100")])])
    assert exc.value.row_index == 1


def test_malformed_ending_value_in_a_later_block_raises() -> None:
    nav = [_nav_block([_nav_row("Total", "100")])]
    change = [_change_block([("Ending Value", "100")]), _change_block([("Ending Value", "x")])]
    with pytest.raises(ParseError):
        parse_net_asset_value(nav, change)


def test_change_in_nav_absent_single_source() -> None:
    nav = [
        _nav_block(
            [_nav_row("Cash", "23.72"), _nav_row("Stock", "100"), _nav_row("Total", "123.72")]
        )
    ]
    result = parse_net_asset_value(nav, None)
    assert result.closing_total == Decimal("123.72")
    assert result.cross_checked is False


def test_negative_nav_allowed() -> None:
    """A margined account can carry a negative NAV — not refused/clamped."""
    nav = [_nav_block([_nav_row("Cash", "-1234.56"), _nav_row("Total", "-1234.56")])]
    result = parse_net_asset_value(nav, None)
    assert result.closing_total == Decimal("-1234.56")


# ---- the NAV figure against the Ending Value -------------------------------


def test_mismatch_between_nav_total_and_change_ending_refuses() -> None:
    """#1309: the Total (100) contradicts the Ending Value (200)."""
    nav = [_nav_block([_nav_row("Cash", "100.00"), _nav_row("Total", "100.00")])]
    change = [_change_block([("Ending Value", "200.00")])]
    _assert_refuses_with(nav, change, _ending_value_error(0))


def test_within_tolerance_not_refused() -> None:
    nav = [_nav_block([_nav_row("Cash", "100.00"), _nav_row("Total", "100.00")])]
    change = [_change_block([("Ending Value", "100.009")])]  # < 0.01 slack
    result = parse_net_asset_value(nav, change)
    assert result.closing_total == Decimal("100.00")
    assert result.cross_checked is True


# ---- the printed Total against its asset-class rows (#1287) ---------------


def test_total_its_asset_class_rows_dont_add_up_to_refuses() -> None:
    """The Total and the Ending Value agree, but Cash 50000 + Stock 2007.60
    add up to 52007.60, not the printed 2107.60."""
    nav = [
        _nav_block(
            [
                _nav_row("Cash", "50000.00"),
                _nav_row("Stock", "2007.60"),
                _nav_row("Total", "2107.60"),
            ]
        ),
        _twr_block(),
    ]
    _assert_refuses_with(nav, [_change_block([("Ending Value", "2107.60")])], _breakdown_error(2))


@pytest.mark.parametrize(
    ("stock", "agrees"),
    [
        ("2007.60", True),
        ("2007.59", True),
        ("2007.61", True),
        ("2007.589", False),
        ("2007.611", False),
    ],
    ids=["exact", "0.01-under", "0.01-over", "0.011-under", "0.011-over"],
)
@pytest.mark.parametrize(
    "with_ending_value", [True, False], ids=["ending-value", "no-change-in-nav"]
)
def test_total_and_asset_class_rows_agree_within_the_tolerance(
    stock: str, agrees: bool, with_ending_value: bool
) -> None:
    """Rows within 0.01 of the Total file the printed Total, never their sum;
    0.011 apart refuses."""
    nav = [
        _nav_block(
            [_nav_row("Cash", "100.00"), _nav_row("Stock", stock), _nav_row("Total", "2107.60")]
        )
    ]
    change = [_change_block([("Ending Value", "2107.60")])] if with_ending_value else None
    if not agrees:
        _assert_refuses_with(nav, change, _breakdown_error(2))
        return
    result = parse_net_asset_value(nav, change)
    assert result.closing_total == Decimal("2107.60")
    assert result.refusal_reason is None
    assert result.cross_checked is with_ending_value
    assert result.asset_class_current_totals == {
        "Cash": Decimal("100.00"),
        "Stock": Decimal(stock),
    }


@pytest.mark.parametrize(
    ("total", "agrees"),
    [
        ("0", True),
        ("0.00", True),
        ("-0.00", True),
        ("0.01", False),
        ("-0.01", False),
        ("100", False),
    ],
)
def test_a_total_with_no_asset_class_rows_must_be_zero(total: str, agrees: bool) -> None:
    """No asset-class rows add up to zero, with no rounding to allow for."""
    nav = [_nav_block([_nav_row("Total", total)])]
    change = [_change_block([("Ending Value", total)])]
    if not agrees:
        _assert_refuses_with(nav, change, _breakdown_error(0))
        return
    result = parse_net_asset_value(nav, change)
    assert result.closing_total == Decimal(total)
    assert result.refusal_reason is None


def test_total_check_does_not_round_away_a_row() -> None:
    """The rows add up to exactly 0.01000000000000000001, past the tolerance
    from a zero Total. At Decimal's default 28 digits, 1E+14 plus the middle
    row rounds to 1E+14 + 0.01, and the sum to 0.01, within it."""
    nav = [
        _nav_block(
            [
                _nav_row("Cash", "1e14"),
                _nav_row("Stock", "0.01000000000000000001"),
                _nav_row("Options", "-1e14"),
                _nav_row("Total", "0"),
            ]
        )
    ]
    _assert_refuses_with(nav, None, _breakdown_error(3))


@pytest.mark.parametrize(
    ("total_rows", "change", "reason"),
    [
        pytest.param(
            [_nav_row("Total", "100"), _nav_row("Total", "100")],
            [_change_block([("Ending Value", "100")])],
            NavRefusalReason.MULTIPLE_TOTAL_ROWS,
            id="multiple-total-rows-first",
        ),
        pytest.param(
            [_nav_row("Total", "")],
            [_change_block([("Ending Value", "100")])],
            NavRefusalReason.BLANK_TOTAL,
            id="blank-total-first",
        ),
        pytest.param(
            [_nav_row("Total", "100")],
            [_change_block([("Ending Value", "200")])],
            None,
            id="before-ending-value-mismatch",
        ),
        pytest.param(
            [_nav_row("Total", "100")],
            [_change_block([("Ending Value", "100"), ("Ending Value", "100")])],
            None,
            id="before-multiple-ending-values",
        ),
        pytest.param(
            [_nav_row("Total", "100")],
            [_change_block([("Ending Value", "")])],
            None,
            id="before-blank-ending-value",
        ),
    ],
)
def test_total_breakdown_check_order(
    total_rows: list[dict[str, str]], change: list[Section], reason: NavRefusalReason | None
) -> None:
    """The check runs after the Total row's own soft refusals and, raising
    (#1309), before any Ending Value refusal: Cash 30 + Stock 50 never add
    up to 100."""
    nav = [_nav_block([_nav_row("Cash", "30"), _nav_row("Stock", "50"), *total_rows])]
    if reason is None:
        _assert_refuses_with(nav, change, _breakdown_error(2))
        return
    result = parse_net_asset_value(nav, change)
    assert result.closing_total is None
    assert result.refusal_reason is reason


@pytest.mark.parametrize(
    ("nav", "change", "expected", "markers"),
    [
        pytest.param(
            [
                _nav_block(
                    [
                        _nav_row("Cash", "735180246.00"),
                        _nav_row("Stock", "2007.60"),
                        _nav_row("Total", "735180146.00"),
                    ]
                )
            ],
            [_change_block([("Ending Value", "735180146.00")])],
            _breakdown_error(2),
            ("735180246", "735180146", "2007"),
            id="total-breakdown",
        ),
        pytest.param(
            [
                _nav_block(
                    [
                        _nav_row("Cash", "100.00"),
                        _nav_row("Stock", "2007.60"),
                        _nav_row("Total", "2107.60"),
                    ]
                )
            ],
            [_change_block([("Ending Value", "735180346.00")])],
            _ending_value_error(0),
            ("735180346", "2107", "2007"),
            id="ending-value",
        ),
        pytest.param(
            [_nav_block([_nav_row("Cash", "100.00"), _nav_row("Stock", "2007.60")])],
            [
                _change_block([("Starting Value", "0")]),
                _change_block(
                    [("Synthetic Holder Uniform", "0"), ("Ending Value", "735180346.00")]
                ),
            ],
            _ending_value_error(1),
            ("735180346", "2107", "2007", "Synthetic Holder Uniform"),
            id="ending-value-in-a-later-block-without-a-total-row",
        ),
    ],
)
def test_a_contradiction_names_its_cell_and_no_figure(
    nav: list[Section], change: list[Section], expected: ParseError, markers: tuple[str, ...]
) -> None:
    """#1309: each error names the row's index in its block and the column,
    with no figure from the statement and no chained exception."""
    _assert_refuses_with(nav, change, expected, markers)


def test_nav_section_absent_refuses() -> None:
    result = parse_net_asset_value(None, None)
    assert result.closing_total is None
    assert result.refusal_reason is NavRefusalReason.NO_ASSET_CLASS_BLOCK


def test_two_asset_class_blocks_ambiguous_refuses() -> None:
    nav = [
        _nav_block([_nav_row("Total", "100")]),
        _nav_block([_nav_row("Total", "200")]),
    ]
    result = parse_net_asset_value(nav, None)
    assert result.closing_total is None
    assert result.refusal_reason is NavRefusalReason.MULTIPLE_ASSET_CLASS_BLOCKS


def test_only_twr_block_no_asset_class_refuses() -> None:
    result = parse_net_asset_value([_twr_block()], None)
    assert result.closing_total is None
    assert result.refusal_reason is NavRefusalReason.NO_ASSET_CLASS_BLOCK


def test_multiple_total_rows_refuses() -> None:
    nav = [_nav_block([_nav_row("Total", "100"), _nav_row("Total", "100")])]
    result = parse_net_asset_value(nav, None)
    assert result.closing_total is None
    assert result.refusal_reason is NavRefusalReason.MULTIPLE_TOTAL_ROWS


def test_empty_current_total_in_total_row_refuses() -> None:
    nav = [_nav_block([_nav_row("Total", "")])]
    result = parse_net_asset_value(nav, None)
    assert result.closing_total is None
    assert result.refusal_reason is NavRefusalReason.BLANK_TOTAL


def test_asset_class_block_with_no_rows_refuses() -> None:
    """A NAV block that has the right columns but no data rows (and thus no
    Total row to read and nothing to sum) is a soft refusal, not a crash."""
    empty_block = Section(
        kind=KnownActivitySection.NET_ASSET_VALUE, columns=_NAV_COLS, header_line=1, rows=[]
    )
    result = parse_net_asset_value([empty_block], None)
    assert result.closing_total is None
    assert result.refusal_reason is NavRefusalReason.NO_ASSET_CLASS_ROWS


# ---- ParseError on malformed (present-but-non-numeric) figures ------------


def test_malformed_current_total_raises() -> None:
    nav = [_nav_block([_nav_row("Total", "N/A")])]
    with pytest.raises(ParseError):
        parse_net_asset_value(nav, None)


def test_malformed_change_ending_raises() -> None:
    nav = [_nav_block([_nav_row("Total", "100")])]
    change = [_change_block([("Ending Value", "oops")])]
    with pytest.raises(ParseError):
        parse_net_asset_value(nav, change)


def test_infinity_current_total_raises() -> None:
    nav = [_nav_block([_nav_row("Total", "Infinity")])]
    with pytest.raises(ParseError):
        parse_net_asset_value(nav, None)


# ---- refusals instead of skips (#1065) ------------------------------------
#
# Each case pins the exact place and fixed message, an empty raw_value, and
# that its own markers (synthetic stand-ins for a holder's name or a figure a
# shifted row could carry) are absent from str(error).

_NAV = "Net Asset Value"
_CHANGE = "Change in NAV"
_BLANK = "must not be blank"
_DECIMAL = "must be a decimal number"
_FINITE = "must be a finite decimal"
_REPEATED_FIELD_NAME = (
    "repeats an earlier row's Field Name; refusing rather than overwriting the earlier amount"
)
_REPEATED_ASSET_CLASS = (
    "repeats an earlier row's Asset Class; refusing rather than overwriting the earlier "
    "Current Total"
)


def _missing_column(line: int, column: str) -> str:
    return (
        f"the header on line {line} has no {column!r} column, so the block's rows can't be "
        "read; refusing rather than skipping the block. IBKR may have changed the "
        "section's format."
    )


def _block(
    kind: KnownActivitySection, columns: tuple[str, ...], line: int, *cells: tuple[str, ...]
) -> Section:
    return Section(
        kind=kind,
        columns=columns,
        header_line=line,
        rows=[dict(zip(columns, row, strict=True)) for row in cells],
    )


_TOTAL_100 = [
    _nav_block([_nav_row("Cash ", "30"), _nav_row("Stock", "70"), _nav_row("Total", "100")])
]
_ENDING_100 = _change_block([("Ending Value", "100")])


@dataclass(frozen=True)
class _Refusal:
    nav: list[Section]
    change: list[Section]
    section: str
    row_index: int
    column: str
    message: str
    markers: tuple[str, ...]
    limits_stubbed: bool = False
    """Stub the reader's read-time digit limits out (#1276): only an amount
    past them can reach this case's check."""


def _without_digit_limits(monkeypatch: pytest.MonkeyPatch) -> None:
    """Let a NAV or Change in NAV amount past the read-time digit limits
    through (#1276); ``raising=False`` keeps the stub harmless where the
    reader has no limit."""
    monkeypatch.setattr(
        "ibkr_fa.parser._cells.number_limit_breach",
        lambda _value, _kind: None,
        raising=False,
    )


_REFUSALS = {
    "change-block-without-field-value": _Refusal(
        _TOTAL_100,
        [
            _block(
                KnownActivitySection.CHANGE_IN_NAV,
                ("Field Name", "Synthetic Holder Alpha"),
                40,
                ("Ending Value", "1234567.01"),
            )
        ],
        _CHANGE,
        40,
        "Field Value",
        _missing_column(40, "Field Value"),
        ("Synthetic Holder Alpha", "1234567.01"),
    ),
    "change-block-without-field-name-after-a-valid-block": _Refusal(
        _TOTAL_100,
        [
            _ENDING_100,
            _block(
                KnownActivitySection.CHANGE_IN_NAV,
                ("Synthetic Holder Bravo", "Field Value"),
                41,
                ("Ending Value", "1234567.02"),
            ),
        ],
        _CHANGE,
        41,
        "Field Name",
        _missing_column(41, "Field Name"),
        ("Synthetic Holder Bravo", "1234567.02"),
    ),
    "nav-block-without-current-total-beside-a-valid-block": _Refusal(
        [
            *_TOTAL_100,
            _twr_block(),
            _block(
                KnownActivitySection.NET_ASSET_VALUE,
                ("Asset Class", "Prior Total"),
                30,
                ("Synthetic Holder Charlie", "1234567.03"),
            ),
        ],
        [],
        _NAV,
        30,
        "Current Total",
        _missing_column(30, "Current Total"),
        ("Synthetic Holder Charlie", "1234567.03"),
    ),
    "nav-block-without-asset-class-before-a-valid-block": _Refusal(
        [
            _block(
                KnownActivitySection.NET_ASSET_VALUE,
                ("Current Total", "Change"),
                31,
                ("1234567.04", "Synthetic Holder Delta"),
            ),
            *_TOTAL_100,
        ],
        [],
        _NAV,
        31,
        "Asset Class",
        _missing_column(31, "Asset Class"),
        ("Synthetic Holder Delta", "1234567.04"),
    ),
    "change-row-blank-field-name": _Refusal(
        _TOTAL_100,
        [_change_block([("Starting Value", "0"), ("", "1234567.05"), ("Ending Value", "100")])],
        _CHANGE,
        1,
        "Field Name",
        _BLANK,
        ("1234567.05",),
    ),
    "change-row-blank-spacer": _Refusal(
        _TOTAL_100,
        [_change_block([("Synthetic Holder Kilo", "0"), (" ", ""), ("Ending Value", "100")])],
        _CHANGE,
        1,
        "Field Name",
        _BLANK,
        ("Synthetic Holder Kilo",),
    ),
    "change-row-blank-field-value": _Refusal(
        _TOTAL_100,
        [_change_block([("Synthetic Holder Echo", " "), ("Ending Value", "100")])],
        _CHANGE,
        0,
        "Field Value",
        _BLANK,
        ("Synthetic Holder Echo",),
    ),
    "change-row-repeated-field-name": _Refusal(
        _TOTAL_100,
        [
            _change_block(
                [
                    ("Synthetic Holder Foxtrot", "1234567.06"),
                    ("Synthetic Holder Foxtrot", "7654321.06"),
                    ("Ending Value", "100"),
                ]
            )
        ],
        _CHANGE,
        1,
        "Field Name",
        _REPEATED_FIELD_NAME,
        ("Synthetic Holder Foxtrot", "1234567.06", "7654321.06"),
    ),
    "change-row-repeated-field-name-in-a-later-block": _Refusal(
        _TOTAL_100,
        [
            _change_block([("Synthetic Holder Golf", "1234567.07")]),
            _change_block([("Synthetic Holder Golf ", "7654321.07"), ("Ending Value", "100")]),
        ],
        _CHANGE,
        0,
        "Field Name",
        _REPEATED_FIELD_NAME,
        ("Synthetic Holder Golf", "1234567.07", "7654321.07"),
    ),
    "breakdown-blank-asset-class": _Refusal(
        [
            _nav_block(
                [_nav_row("Cash ", "30"), _nav_row(" ", "1234567.08"), _nav_row("Total", "100")]
            )
        ],
        [],
        _NAV,
        1,
        "Asset Class",
        _BLANK,
        ("1234567.08",),
    ),
    "breakdown-blank-asset-class-without-total-row": _Refusal(
        [_nav_block([_nav_row("", "1234567.09"), _nav_row("Stock", "0")])],
        [_change_block([("Ending Value", "1234567.09")])],
        _NAV,
        0,
        "Asset Class",
        _BLANK,
        ("1234567.09",),
    ),
    "breakdown-repeated-asset-class": _Refusal(
        [
            _nav_block(
                [
                    _nav_row("Synthetic Holder Hotel", "30"),
                    _nav_row("Synthetic Holder Hotel", "70"),
                    _nav_row("Total", "100"),
                ]
            )
        ],
        [],
        _NAV,
        1,
        "Asset Class",
        _REPEATED_ASSET_CLASS,
        ("Synthetic Holder Hotel",),
    ),
    "breakdown-repeated-asset-class-without-total-row": _Refusal(
        [
            _nav_block(
                [
                    _nav_row("Synthetic Holder Lima", "0"),
                    _nav_row("Cash ", "30"),
                    _nav_row("Cash", "70"),
                ]
            )
        ],
        [_ENDING_100],
        _NAV,
        2,
        "Asset Class",
        _REPEATED_ASSET_CLASS,
        ("Synthetic Holder Lima",),
    ),
    "breakdown-blank-current-total": _Refusal(
        [_nav_block([_nav_row("Synthetic Holder India", " "), _nav_row("Total", "100")])],
        [],
        _NAV,
        0,
        "Current Total",
        _BLANK,
        ("Synthetic Holder India",),
    ),
    "breakdown-non-numeric-current-total": _Refusal(
        [_nav_block([_nav_row("Stock", "Synthetic Holder Juliett"), _nav_row("Total", "100")])],
        [],
        _NAV,
        0,
        "Current Total",
        _DECIMAL,
        ("Synthetic Holder Juliett",),
    ),
    "breakdown-non-finite-current-total": _Refusal(
        [_nav_block([_nav_row("Stock", "NaN1234567"), _nav_row("Total", "100")])],
        [],
        _NAV,
        0,
        "Current Total",
        _FINITE,
        ("NaN1234567",),
    ),
    "breakdown-checked-before-an-ending-value-refusal": _Refusal(
        [_nav_block([_nav_row("Stock", ""), _nav_row("Total", "100")])],
        [_change_block([("Synthetic Holder Mike", "0"), ("Ending Value", "200")])],
        _NAV,
        0,
        "Current Total",
        _BLANK,
        ("Synthetic Holder Mike",),
    ),
    "blank-amount-does-not-hide-a-later-malformed-amount": _Refusal(
        [_nav_block([_nav_row("Stock", ""), _nav_row("Cash", "Synthetic Holder Romeo")])],
        [_ENDING_100],
        _NAV,
        1,
        "Current Total",
        _DECIMAL,
        ("Synthetic Holder Romeo",),
    ),
    # Past the Decimal exponent range: since #1276 the reader refuses these
    # amounts by their digit limit first
    # (test_an_amount_past_its_digit_limit_refuses_when_read), so the cases
    # stub the limit out to reach the exact sum and cross-check.
    "asset-class-sum-outside-decimal-range": _Refusal(
        [_nav_block([_nav_row("Synthetic Holder Papa", "1e1000000")])],
        [_change_block([("Ending Value", "1e1000000")])],
        _NAV,
        0,
        "Current Total",
        "the asset-class amounts cannot be added exactly",
        ("Synthetic Holder Papa", "1e1000000"),
        limits_stubbed=True,
    ),
    "ending-value-crosscheck-outside-decimal-range": _Refusal(
        [_nav_block([_nav_row("Cash", "9e999999"), _nav_row("Total", "9e999999")])],
        [_change_block([("Synthetic Holder Quebec", "0"), ("Ending Value", "-9e999999")])],
        _CHANGE,
        1,
        "Field Value",
        "the NAV and Ending Value cannot be cross-checked exactly",
        ("Synthetic Holder Quebec", "9e999999"),
        limits_stubbed=True,
    ),
    # #1287: the Total's own check against its asset-class rows.
    "total-breakdown-sum-outside-decimal-range": _Refusal(
        [
            _nav_block(
                [
                    _nav_row("Synthetic Holder Sierra", "9e999999"),
                    _nav_row("Stock", "9e999999"),
                    _nav_row("Total", "9e999999"),
                ]
            )
        ],
        [_change_block([("Ending Value", "9e999999")])],
        _NAV,
        2,
        "Current Total",
        "the Total and the asset-class amounts cannot be compared exactly",
        ("Synthetic Holder Sierra", "9e999999"),
        limits_stubbed=True,
    ),
    "total-breakdown-difference-outside-decimal-range": _Refusal(
        [
            _nav_block(
                [
                    _nav_row("Total", "9e999999"),
                    _nav_row("Synthetic Holder Tango", "-9e999999"),
                ]
            )
        ],
        [],
        _NAV,
        0,
        "Current Total",
        "the Total and the asset-class amounts cannot be compared exactly",
        ("Synthetic Holder Tango", "9e999999"),
        limits_stubbed=True,
    ),
}


@pytest.mark.parametrize("case", _REFUSALS.values(), ids=_REFUSALS.keys())
def test_unreadable_input_refuses_instead_of_being_skipped(
    case: _Refusal, capsys, monkeypatch: pytest.MonkeyPatch
) -> None:
    if case.limits_stubbed:
        _without_digit_limits(monkeypatch)
    with pytest.raises(ParseError) as exc:
        parse_net_asset_value(case.nav, case.change)
    error = exc.value
    assert type(error) is ParseError
    assert (error.section, error.row_index, error.column, error.message) == (
        case.section,
        case.row_index,
        case.column,
        case.message,
    )
    assert error.raw_value == ""
    assert str(error) == (
        f"ParseError in section {case.section!r} at row {case.row_index}, "
        f"column {case.column!r}: {case.message}"
    )
    assert capsys.readouterr() == ("", "")
    rendered = str(error) + repr(error) + render_without_frames(error)
    assert_no_markers(rendered, *case.markers)


@pytest.mark.parametrize(
    ("amounts", "ending", "limits_stubbed"),
    [
        # The rows add up to 0.01000000000000000001, past the tolerance; at
        # Decimal's default 28 digits 1E+14 + that rounds to 1E+14 + 0.01,
        # and the sum to 0.01, within it.
        (("1e14", "0.01000000000000000001", "-1e14"), "0", False),
        # A difference of 0.010000000000000000000000000001 (29 digits) rounds
        # to the tolerance at 28 digits. Amounts within the money limit (20
        # places) never differ by that many digits near 0.01, so the case
        # stubs the reader's read-time digit limit out (#1276); the limit
        # refuses this Ending Value first
        # (test_an_amount_past_its_digit_limit_refuses_when_read).
        (("100",), "100.010000000000000000000000000001", True),
    ],
    ids=["sum-must-not-round-away-a-row", "difference-must-not-round-to-tolerance"],
)
def test_nav_arithmetic_does_not_round_away_a_mismatch(
    amounts, ending, limits_stubbed: bool, monkeypatch: pytest.MonkeyPatch
) -> None:
    if limits_stubbed:
        _without_digit_limits(monkeypatch)
    nav = [
        _nav_block(
            [_nav_row(f"Synthetic Class {index}", amount) for index, amount in enumerate(amounts)]
        )
    ]
    _assert_refuses_with(nav, [_change_block([("Ending Value", ending)])], _ending_value_error(0))


def test_exact_high_precision_sum_confirms_the_printed_ending_value() -> None:
    """The exact sum 1E+14 + 1E-20 (35 digits, each amount within the money
    limit) is exactly the 0.01 tolerance from the printed Ending Value, so it
    confirms it. Rounded to Decimal's default 28 digits, the sum would lose
    its 1E-20 and miss by more than 0.01."""
    nav = [_nav_block([_nav_row("Cash", "1e14"), _nav_row("Stock", "1e-20")])]
    ending = "100000000000000.01000000000000000001"
    result = parse_net_asset_value(nav, [_change_block([("Ending Value", ending)])])
    assert result.closing_total == Decimal(ending)
    assert result.refusal_reason is None
    assert result.cross_checked is True


@pytest.mark.parametrize(
    ("nav", "change", "section", "column"),
    [
        pytest.param(
            [_nav_block([_nav_row("Cash", "7242" + "0" * 25)])],
            [_ENDING_100],
            _NAV,
            "Current Total",
            id="asset-class-29-whole-digits",
        ),
        pytest.param(
            [_nav_block([_nav_row("Cash", "7242e1000000")])],
            [_ENDING_100],
            _NAV,
            "Current Total",
            id="asset-class-past-the-exponent-range",
        ),
        pytest.param(
            [_nav_block([_nav_row("Total", "7242e1000000")])],
            [_ENDING_100],
            _NAV,
            "Current Total",
            id="total-past-the-exponent-range",
        ),
        pytest.param(
            _TOTAL_100,
            [_change_block([("Ending Value", "7242" + "0" * 24 + "1")])],
            _CHANGE,
            "Field Value",
            id="ending-value-29-whole-digits",
        ),
        pytest.param(
            _TOTAL_100,
            [_change_block([("Ending Value", "7242e1000000")])],
            _CHANGE,
            "Field Value",
            id="ending-value-past-the-exponent-range",
        ),
        pytest.param(
            _TOTAL_100,
            [_change_block([("Ending Value", "100.01" + "0" * 24 + "7242")])],
            _CHANGE,
            "Field Value",
            id="ending-value-30-places",
        ),
    ],
)
def test_an_amount_past_its_digit_limit_refuses_when_read(
    nav: list[Section], change: list[Section], section: str, column: str
) -> None:
    """#1276: the amounts the exact-arithmetic tests above once read refuse
    when their row is read, with the money limit and no value, before any
    sum or cross-check."""
    with pytest.raises(ParseError) as exc:
        parse_net_asset_value(nav, change)
    assert_unchained_refusal(
        exc.value,
        ParseError(section=section, row_index=0, column=column, message=MONEY, raw_value=""),
        f"ParseError in section {section!r} at row 0, column {column!r}: {MONEY}",
        markers=("7242",),
    )
