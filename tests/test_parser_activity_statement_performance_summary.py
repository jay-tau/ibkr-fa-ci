"""Tests for :mod:`ibkr_fa.parser.activity_statement.performance_summary` (#913)."""

from __future__ import annotations

import pytest

from ibkr_fa.exceptions import ParseError
from ibkr_fa.parser.activity_statement.performance_summary import check_cost_adjustments
from tests._activity_statement_blocks import _SECTION
from tests._activity_statement_blocks import performance_summary_block as _block


def test_blank_or_zero_cost_adjustments_pass() -> None:
    check_cost_adjustments([_block("", "0", "0.00", "-0"), _block("0E+3")])
    check_cost_adjustments([])


@pytest.mark.parametrize("value", ["5", "-0.01", "1E-30", "abc", "NaN", "sNaN", "Infinity", " "])
def test_any_other_cost_adjustment_refuses(value: str) -> None:
    """A cost adjustment changes a holding's basis, which the lots built
    from Trades may not reflect; a value that isn't a decimal can't be
    shown to be zero either. The error names the file line, not the cell
    (#1040)."""
    with pytest.raises(ParseError) as exc:
        check_cost_adjustments([_block("0"), _block("0", value)])
    error = exc.value
    assert (error.section, error.row_index, error.column, error.raw_value) == (
        _SECTION.value,
        6,
        "Cost Adj.",
        "",
    )
    assert error.message.startswith(
        "line 6 has a 'Cost Adj.' cell that isn't blank or a zero decimal."
    )


def test_a_row_without_the_cost_adjustment_column_refuses() -> None:
    columns = ("Asset Category", "Symbol")
    with pytest.raises(ParseError) as exc:
        check_cost_adjustments([_block("", columns=columns)])
    error = exc.value
    assert (error.section, error.row_index, error.column) == (_SECTION.value, 5, "Cost Adj.")
    assert error.message.startswith("line 5 has no 'Cost Adj.' column")
