"""Tests for :mod:`ibkr_fa.parser.activity_statement._description`.

The shared helper is exercised indirectly through the Dividends and
Withholding Tax parser tests, but a small focused suite here guards
against regression on the section-agnostic contract: the helper
must thread the caller's ``section`` and ``row_index`` into the
:class:`ParseError` payload regardless of which sister parser
invoked it.
"""

from __future__ import annotations

import pytest

from ibkr_fa.exceptions import ParseError
from ibkr_fa.parser.activity_statement._description import (
    _DESCRIPTION_PATTERN,
    _DESCRIPTION_PREFIX_PATTERN,
    parse_symbol_isin_from_description,
)
from ibkr_fa.types import Isin, Ticker


def test_parses_canonical_dividend_description() -> None:
    symbol, isin = parse_symbol_isin_from_description(
        "XQAA(US99990A3095) Cash Dividend USD 0.35 per Share (Ordinary Dividend)",
        section="Dividends",
        row_index=0,
    )
    assert symbol == Ticker("XQAA")
    assert isin == Isin("US99990A3095")


def test_parses_canonical_withholding_tax_description() -> None:
    """The WHT section uses the same structural prefix but appends
    a ``- US Tax`` suffix to the originating dividend description."""
    symbol, isin = parse_symbol_isin_from_description(
        "XQAA(US99990A3095) Cash Dividend USD 0.35 per Share - US Tax",
        section="Withholding Tax",
        row_index=0,
    )
    assert symbol == Ticker("XQAA")
    assert isin == Isin("US99990A3095")


@pytest.mark.parametrize(
    "description",
    [
        "XQCY-A(US99972D1072) Cash Dividend USD 0.94 per Share - US Tax",
        "XQCZ-B(US99971S1050) Cash Dividend USD 0.23 per Share",
        "XQDA-B(US99971Q1094) Cash Dividend USD 0.10 per Share",
        "XQDA.B(US99971Q1094) Cash Dividend USD 0.10 per Share",
    ],
)
def test_hyphen_and_dot_symbols_accepted(description: str) -> None:
    """PR #137 Tier-3 widened the symbol charset to accept hyphens.
    These made-up share-class tickers also exercise the supported dot form.
    A regression here would break Dividends and Withholding Tax
    parsing simultaneously."""
    symbol, _ = parse_symbol_isin_from_description(description, section="Dividends", row_index=0)
    assert "-" in str(symbol) or "." in str(symbol)


def test_section_name_threaded_into_parse_error() -> None:
    """Each caller passes its own ``section`` string; the helper
    must use that verbatim in any raised ParseError. Without this,
    the WHT parser's malformed-row errors would mis-attribute to
    'Dividends'."""
    with pytest.raises(ParseError) as exc:
        parse_symbol_isin_from_description(
            "free-text not matching the prefix",
            section="Withholding Tax",
            row_index=7,
        )
    assert exc.value.section == "Withholding Tax"
    assert exc.value.row_index == 7
    assert exc.value.column == "Description"


def test_empty_description_rejected() -> None:
    with pytest.raises(ParseError) as exc:
        parse_symbol_isin_from_description("", section="Dividends", row_index=0)
    assert exc.value.column == "Description"
    assert "non-empty" in exc.value.message


def test_whitespace_only_description_rejected() -> None:
    with pytest.raises(ParseError) as exc:
        parse_symbol_isin_from_description("   ", section="Dividends", row_index=0)
    assert exc.value.column == "Description"


def test_lowercase_symbol_rejected() -> None:
    """The uppercase-leading invariant is shared with trade-side
    tickers — a lowercase 'symbol' would silently mismatch at
    FA-A3 row-assembly time."""
    with pytest.raises(ParseError) as exc:
        parse_symbol_isin_from_description(
            "xqaa(US99990A3095) Cash Dividend USD 0.35 per Share",
            section="Dividends",
            row_index=0,
        )
    assert exc.value.column == "Description"


def test_bad_isin_check_digit_position_rejected() -> None:
    """The ISIN regex requires the final character to be a digit
    (ISO 6166 check-digit position). A letter in the check-digit
    slot is structurally malformed."""
    with pytest.raises(ParseError):
        parse_symbol_isin_from_description(
            "XQAA(US99990A309Z) Cash Dividend USD 0.35 per Share",
            section="Dividends",
            row_index=0,
        )


def test_optional_whitespace_between_symbol_and_isin() -> None:
    """Defense-in-depth: the regex tolerates whitespace between
    ticker and the ``(`` opening the ISIN group, in case IBKR
    reformats with a separator space later."""
    symbol, isin = parse_symbol_isin_from_description(
        "XQAA (US99990A3095) Cash Dividend USD 0.35 per Share",
        section="Dividends",
        row_index=0,
    )
    assert symbol == Ticker("XQAA")
    assert isin == Isin("US99990A3095")


def test_prefix_regex_stays_in_sync_with_description_regex() -> None:
    """``_DESCRIPTION_PREFIX_PATTERN``, which locates the end of the
    structural prefix for the type-slice, MUST accept every shape that
    ``_DESCRIPTION_PATTERN`` accepts. If a future contributor widens
    one regex (e.g., to accept new symbol charsets) without the other,
    a parsed description would fail the type-slice extraction
    (T5-r20 P2 root cause). This test pins the two regex bodies
    together so any drift is caught at test time.
    """
    # Both must accept all the shapes IBKR / hand-normalised exports
    # are known to emit.
    sample_descriptions = (
        "XQAA(US99990A3095) Cash Dividend USD 0.35 per Share",
        "XQAA (US99990A3095) Cash Dividend USD 0.35 per Share",
        "XQAA\t(US99990A3095) Cash Dividend USD 0.35 per Share",
        "XQAA   (US99990A3095) Cash Dividend USD 0.35 per Share",
        "XQCY-A(US99972D1072) Cash Dividend USD 0.94 per Share",
        "XQDA-B(US99971Q1094) Special Dividend USD 1.00 per Share",
    )
    for desc in sample_descriptions:
        assert _DESCRIPTION_PATTERN.match(desc) is not None, (
            f"description regex rejects {desc!r} (unexpected — it is the source of truth)"
        )
        assert _DESCRIPTION_PREFIX_PATTERN.match(desc) is not None, (
            f"prefix regex rejects {desc!r} — drift from the description "
            "regex; widen the prefix pattern to match"
        )
