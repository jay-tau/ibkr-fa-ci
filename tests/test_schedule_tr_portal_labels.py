"""Tests for the ITR-2 and ITR-3 Schedule TR portal labels."""

from __future__ import annotations

import inspect
import typing

from ibkr_fa.output.portal_labels import (
    PORTAL_COLUMN_HEADERS_TR,
    PORTAL_COLUMN_HEADERS_TR_BY_ITR_FORM,
    _ay_2026_27,
)

# The AY 2026-27 utilities' Schedule TR columns (a)-(e), in order. ITR-3 keeps
# ITR-2's first four and renames (e). Columns (c) and (d) name the Schedule FSI
# column they total, and (e) lists the sections (PR #216).
_ITR2_HEADERS = (
    "Country Code",
    "Taxpayer Identification Number",
    "Total taxes paid outside India (total of (c) of Schedule FSI in respect of each country)",
    "Total tax relief available (total of (e) of Schedule FSI in respect of each country)",
    "Tax Relief Claimed under section (specify 90, 90A or 91)",
)
_ITR3_HEADERS = (
    "Country Code",
    "Taxpayer Identification Number",
    "Total taxes paid outside India (total of (c) of Schedule FSI in respect of each country)",
    "Total tax relief available (total of (e) of Schedule FSI in respect of each country)",
    "Section under which relief claimed (specify 90, 90A or 91)",
)


def test_column_headers_is_non_empty_tuple_of_str() -> None:
    assert isinstance(PORTAL_COLUMN_HEADERS_TR, tuple)
    assert PORTAL_COLUMN_HEADERS_TR != ()
    for header in PORTAL_COLUMN_HEADERS_TR:
        assert isinstance(header, str)
        assert header != ""


def test_column_headers_match_ay2026_27_itr2_and_itr3_utilities() -> None:
    assert PORTAL_COLUMN_HEADERS_TR == _ITR2_HEADERS
    assert PORTAL_COLUMN_HEADERS_TR_BY_ITR_FORM == {"ITR-2": _ITR2_HEADERS, "ITR-3": _ITR3_HEADERS}


def test_column_headers_no_synthetic_country_name() -> None:
    """Schedule TR does NOT have a "Country Name" portal column —
    only "Country Code" (CBDT 1-4-digit numeric). Same constraint as
    Schedule FSI (regression-locks the analogous PR #212 T3/T5 r1 P2
    fix on the TR side). The vendored country_name_portal lookup
    lives in the audit block of the writer, NOT in the portal-column
    block."""
    assert "Country Name" not in PORTAL_COLUMN_HEADERS_TR


def test_column_headers_is_annotated_final_tuple_in_canonical_snapshot() -> None:
    """The labels' CANONICAL home is the per-AY snapshot module
    (issue #65). :mod:`ibkr_fa.output.portal_labels` re-exports them
    from the current-AY snapshot. The
    ``Final[tuple[str, ...]]`` annotation must live on the
    snapshot's definition, not the re-export sites — that's what
    this test pins."""
    hints = typing.get_type_hints(_ay_2026_27, include_extras=True)
    assert "PORTAL_COLUMN_HEADERS_TR" in hints
    source = inspect.getsource(_ay_2026_27)
    assert "PORTAL_COLUMN_HEADERS_TR: Final[tuple[str, ...]]" in source


def test_column_headers_have_no_leading_or_trailing_whitespace() -> None:
    for header in PORTAL_COLUMN_HEADERS_TR:
        assert header.strip() == header, f"header {header!r} has surrounding whitespace"


def test_column_headers_are_ascii_clean() -> None:
    """Non-ASCII characters in a portal column header would silently
    corrupt the TR review-CSV output (encoding mis-detection by the
    e-filing utility). Mirrors the sister
    `test_schedule_fsi_portal_labels.test_column_headers_are_ascii_clean`
    for cross-shim consistency."""
    for header in PORTAL_COLUMN_HEADERS_TR:
        assert header.isascii(), f"header {header!r} contains non-ASCII character(s)"
