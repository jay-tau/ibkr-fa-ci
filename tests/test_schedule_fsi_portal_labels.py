"""Tests for ITR-2 Schedule FSI portal labels."""

from __future__ import annotations

import inspect
import typing

from ibkr_fa.output.portal_labels import PORTAL_COLUMN_HEADERS_FSI, _ay_2026_27

# Schedule FSI has outer Country/TIN fields followed by six inner
# columns (a)-(f), flattened to 8 writer columns. See
# docs/portal/schedule-fsi.md. Pinned here so a
# future column add/remove in `_ay_2026_27.py` fails loudly until this
# test is also updated.
_EXPECTED_COLUMN_COUNT = 8


def test_column_headers_is_non_empty_tuple_of_str() -> None:
    assert isinstance(PORTAL_COLUMN_HEADERS_FSI, tuple)
    assert PORTAL_COLUMN_HEADERS_FSI != ()
    for header in PORTAL_COLUMN_HEADERS_FSI:
        assert isinstance(header, str)
        assert header != ""


def test_column_headers_has_expected_count() -> None:
    assert len(PORTAL_COLUMN_HEADERS_FSI) == _EXPECTED_COLUMN_COUNT


def test_column_headers_match_ay2026_27_itr2_and_itr3_utilities() -> None:
    assert PORTAL_COLUMN_HEADERS_FSI == (
        "Country Code",
        "Taxpayer Identification number",
        "Head of Income",
        "Income from outside India (included in PART B-TI)",
        "Tax paid outside India",
        "Tax payable on such income under normal provisions in India",
        "Tax relief available in India (e)= (c) or (d) whichever is lower",
        "Relevant article of DTAA if relief claimed u/s 90 or 90A",
    )


def test_column_headers_first_is_country_code() -> None:
    """The first column is the outer source-country identifier."""
    assert PORTAL_COLUMN_HEADERS_FSI[0] == "Country Code"


def test_column_headers_contains_taxpayer_identification_number() -> None:
    assert "Taxpayer Identification number" in PORTAL_COLUMN_HEADERS_FSI


def test_column_headers_contains_head_of_income() -> None:
    assert "Head of Income" in PORTAL_COLUMN_HEADERS_FSI


def test_column_headers_contains_verbatim_relief_formula() -> None:
    assert (
        "Tax relief available in India (e)= (c) or (d) whichever is lower"
        in PORTAL_COLUMN_HEADERS_FSI
    )


def test_column_headers_no_synthetic_country_name() -> None:
    """Schedule FSI does NOT have a "Country Name" portal column —
    only "Country Code" (CBDT 1-4-digit numeric). The vendored
    country_name_portal lookup lives in the audit block of the
    writer, NOT in the portal-column block. Regression-locks the
    T3 / T5 r1 P2 fix from PR #212 — re-introducing a synthetic
    "Country Name" portal cell would silently break copy-paste into
    the e-filing utility's FSI table (column-shifted)."""
    assert "Country Name" not in PORTAL_COLUMN_HEADERS_FSI


def test_column_headers_is_annotated_final_tuple_in_canonical_snapshot() -> None:
    """The labels' CANONICAL home is the per-AY snapshot module
    (issue #65). :mod:`ibkr_fa.output.portal_labels` re-exports them
    from the current-AY snapshot. The
    ``Final[tuple[str, ...]]`` annotation must live on the
    snapshot's definition, not the re-export sites — that's what
    this test pins."""
    hints = typing.get_type_hints(_ay_2026_27, include_extras=True)
    assert "PORTAL_COLUMN_HEADERS_FSI" in hints
    source = inspect.getsource(_ay_2026_27)
    assert "PORTAL_COLUMN_HEADERS_FSI: Final[tuple[str, ...]]" in source


def test_column_headers_have_no_leading_or_trailing_whitespace() -> None:
    for header in PORTAL_COLUMN_HEADERS_FSI:
        assert header.strip() == header, f"header {header!r} has surrounding whitespace"


def test_column_headers_are_ascii_clean() -> None:
    """Non-ASCII characters in a portal column header would silently
    corrupt the FSI review-CSV output (encoding mis-detection by the
    e-filing utility). Mirrors the sister
    `test_schedule_os_portal_labels.test_label_is_ascii_clean` for
    cross-shim consistency."""
    for header in PORTAL_COLUMN_HEADERS_FSI:
        assert header.isascii(), f"header {header!r} contains non-ASCII character(s)"
