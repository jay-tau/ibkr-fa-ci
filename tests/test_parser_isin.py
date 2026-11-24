"""Tests for :mod:`ibkr_fa.parser._isin` shared ISIN validator.

Issue #150: centralised the ISO 6166 shape check used by every
parser construction site. These tests exercise the helper
directly; per-parser integration coverage lives in
``tests/test_parser_activity_statement_trades.py``,
``tests/test_parser_open_positions.py``, and
``tests/test_parser_closed_lots.py``.
"""

from __future__ import annotations

import pytest

from ibkr_fa.exceptions import ParseError
from ibkr_fa.parser._isin import validate_isin_or_raise


def test_valid_us_isin_passes_unchanged() -> None:
    """A synthetic US ISIN (US-prefixed, valid Luhn) is returned
    unchanged. Sanity test pinning the happy-path identity behaviour."""
    out = validate_isin_or_raise("US99992A1097", section="Trades", row_index=0, column="ISIN")
    assert out == "US99992A1097"


def test_valid_canadian_isin_passes_unchanged() -> None:
    """Non-US ISIN (CA-prefixed) — sanity that the validator doesn't
    accidentally hard-code US."""
    out = validate_isin_or_raise("CA99991A1003", section="Trades", row_index=0, column="ISIN")
    assert out == "CA99991A1003"


def test_lowercase_isin_normalised_to_uppercase() -> None:
    """#150: per PR #146 Tier-5 P2 pattern, the helper case-normalises
    so a lowercase IBKR cell is accepted and emitted as canonical
    uppercase. This pre-empts the silent-mismatch bug where lowercase
    ISINs would fail to join against other parsers' uppercase ISINs
    at downstream aggregation time."""
    out = validate_isin_or_raise("us99992a1097", section="Trades", row_index=0, column="ISIN")
    assert out == "US99992A1097"


def test_surrounding_whitespace_stripped() -> None:
    """Defensive: trim surrounding whitespace before validation. IBKR
    is not observed to emit padded ISINs, but the strip makes the
    helper robust to whitespace-bearing CSV exports."""
    out = validate_isin_or_raise("  US99992A1097  ", section="Trades", row_index=0, column="ISIN")
    assert out == "US99992A1097"


def test_empty_raises_parse_error_with_context() -> None:
    """Empty cell raises with section/row/column context (rather than
    a bare ValueError or InvariantError)."""
    with pytest.raises(ParseError) as exc:
        validate_isin_or_raise("", section="Trades", row_index=5, column="ISIN")
    assert exc.value.section == "Trades"
    assert exc.value.row_index == 5
    assert exc.value.column == "ISIN"
    assert "is empty" in exc.value.message
    assert "ISO 6166" in exc.value.message


def test_whitespace_only_treated_as_empty() -> None:
    """A whitespace-only cell is functionally empty — same error path."""
    with pytest.raises(ParseError) as exc:
        validate_isin_or_raise("   ", section="Trades", row_index=0, column="ISIN")
    assert "is empty" in exc.value.message
    assert exc.value.raw_value == ""


@pytest.mark.parametrize(
    "bad_isin",
    [
        pytest.param("US99992A109", id="too-short-11-chars"),
        pytest.param("US99992A10977", id="too-long-13-chars"),
        pytest.param("1S99992A1097", id="prefix-leading-digit"),
        pytest.param("U199992A1097", id="prefix-second-digit"),
        pytest.param("US99992A109X", id="trailing-letter-not-digit"),
        pytest.param("US99992A109!", id="trailing-special-char"),
        pytest.param("US-9992A1097", id="hyphen-in-payload"),
        pytest.param("US 9992A1097", id="internal-space"),
    ],
)
def test_malformed_shape_raises_parse_error_with_context(bad_isin: str) -> None:
    """Every malformed shape raises with section, row and column, and
    without the cell (#1038): a shifted cell can hold the holder's name."""
    with pytest.raises(ParseError) as exc:
        validate_isin_or_raise(bad_isin, section="Open Positions (LOT)", row_index=3, column="ISIN")
    assert exc.value.section == "Open Positions (LOT)"
    assert exc.value.row_index == 3
    assert exc.value.column == "ISIN"
    assert "ISO 6166" in exc.value.message
    assert "issues/150" in exc.value.message
    assert exc.value.raw_value == ""
    assert bad_isin.casefold() not in str(exc.value).casefold()


def test_error_message_omits_raw_value() -> None:
    """#1038: the error names the cell's place, not its value."""
    with pytest.raises(ParseError) as exc:
        validate_isin_or_raise("BADISIN", section="Trades", row_index=0, column="ISIN")
    assert "badisin" not in str(exc.value).casefold()


def test_column_name_embedded_in_empty_message() -> None:
    """The empty-cell error message names the column to disambiguate
    Security ID vs ISIN (the column name varies between sections)."""
    with pytest.raises(ParseError) as exc:
        validate_isin_or_raise(
            "", section="Financial Instrument Information", row_index=2, column="Security ID"
        )
    assert "'Security ID'" in exc.value.message
