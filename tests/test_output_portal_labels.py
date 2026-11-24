"""Tests for :mod:`ibkr_fa.output.portal_labels`."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from ibkr_fa.exceptions import InvariantError
from ibkr_fa.output.portal_labels import (
    PORTAL_COLUMN_HEADERS_FA_A3,
    PORTAL_DATE_FORMAT,
    format_amount_for_portal,
    format_country_for_portal,
    format_date_for_portal,
)
from ibkr_fa.reference.country_codes import CountryCodeNotFoundError
from tests._crash_reports import assert_prints_no_marker

# ---- Column headers --------------------------------------------------------


def test_portal_column_headers_count_is_11() -> None:
    """Schedule FA Table A3 has 11 portal-facing columns (verified
    against the official ITR-2 utility v1.0, sheet TR_FA rows 50-51,
    columns E-O). The auto-generated Sl. No. is NOT included here."""
    assert len(PORTAL_COLUMN_HEADERS_FA_A3) == 11


def test_portal_column_headers_order_matches_portal() -> None:
    """Order is verified against the utility — country first,
    monetary fields last. Regression-guarded so accidental
    re-ordering breaks the test."""
    expected_ordered = (
        "Country Name and Code",
        "Name of entity",
        "Address of entity",
        "ZIP Code",
        "Nature of entity",
        "Date of acquiring the interest",
        "Initial value of the investment",
        "Peak value of investment during the Period",
        "Closing balance",
        "Total gross amount paid/credited with respect to the holding during the period",
        "Total gross proceeds from sale or redemption of investment during the period",
    )
    assert expected_ordered == PORTAL_COLUMN_HEADERS_FA_A3


def test_portal_column_headers_is_a_tuple() -> None:
    """The headers must be immutable so a writer can't accidentally
    mutate the list and reorder columns for downstream rows."""
    assert isinstance(PORTAL_COLUMN_HEADERS_FA_A3, tuple)


def test_closing_label_is_balance_not_value() -> None:
    """Regression: the portal column is literally 'Closing balance'
    even though our model field is closing_value_inr. Confirm we use
    the portal label (NOT the model field name) in the header."""
    assert "Closing balance" in PORTAL_COLUMN_HEADERS_FA_A3
    assert "Closing value" not in PORTAL_COLUMN_HEADERS_FA_A3


# ---- Country formatting ----------------------------------------------------


def test_format_country_for_us() -> None:
    assert format_country_for_portal("US") == "2-UNITED STATES OF AMERICA"


def test_format_country_for_uk_uses_full_name() -> None:
    assert (
        format_country_for_portal("GB") == "44-UNITED KINGDOM OF GREAT BRITAIN AND NORTHERN IRELAND"
    )


@pytest.mark.parametrize(
    "looked_up", ["ZZ", "Synthetic Holder Two", "QZ731942"], ids=["code", "holder-name", "marker"]
)
def test_format_country_raises_on_unknown_iso(looked_up: str) -> None:
    """The error neither prints nor keeps the looked-up code (#1047, #1368)."""
    with pytest.raises(CountryCodeNotFoundError) as exc:
        format_country_for_portal(looked_up)
    assert str(exc.value) == (
        "no CBDT country code mapped for a looked-up ISO alpha-2 code; add a row to "
        "src/ibkr_fa/reference/itr_country_codes.csv after verifying the code against "
        "the official ITR-2 utility (see the .source file there for the upstream URL)."
    )
    assert_prints_no_marker(exc.value, markers=(looked_up,))


# ---- Date formatting -------------------------------------------------------


def test_format_date_uses_dd_mm_yyyy() -> None:
    """The portal expects DD/MM/YYYY (Indian convention). Verified
    via cell J52:J57 number-format `@` and textLength=10 in the
    ITR-2 utility."""
    assert format_date_for_portal(date(2025, 12, 31)) == "31/12/2025"


def test_format_date_zero_pads_single_digit_components() -> None:
    """E.g., 5 May 2025 must render as '05/05/2025', not '5/5/2025'."""
    assert format_date_for_portal(date(2025, 5, 5)) == "05/05/2025"


def test_format_date_handles_dec_31_year_end() -> None:
    """Common case: FA closing dates are typically Dec 31 of CY."""
    assert format_date_for_portal(date(2026, 12, 31)) == "31/12/2026"


def test_portal_date_format_constant() -> None:
    assert PORTAL_DATE_FORMAT == "%d/%m/%Y"


# ---- Amount formatting -----------------------------------------------------


def test_format_amount_none_renders_as_empty_cell() -> None:
    """v1 missing-column convention: None -> empty cell. The CLI
    warns about which columns need manual entry; the empty cell
    signals 'fill me in' visually and the portal's own validation
    rejects blanks as a safety net."""
    assert format_amount_for_portal(None, field="Schedule FA A3 peak value") == ""


@pytest.mark.parametrize(
    ("value", "rendered"),
    [
        ("64218", "64218"),
        ("0", "0"),
        ("12345678900", "12345678900"),
        ("-501", "-501"),
        ("99999999999999", "99999999999999"),
    ],
)
def test_format_amount_renders_a_whole_rupee_amount_as_its_digits(
    value: str, rendered: str
) -> None:
    """The models round filed amounts (``FaA3Row.filed``, ``FaA2Row.filed``);
    the helper renders a whole-rupee value as it is, never in exponent form.
    A negative A2 peak or closing balance keeps its sign."""
    assert format_amount_for_portal(Decimal(value), field="Schedule FA A3 initial value") == (
        rendered
    )


def test_format_amount_refuses_a_fraction_without_printing_it() -> None:
    """The helper never rounds: a fraction is refused, and neither the value
    nor its rounded digits appear in the refusal."""
    with pytest.raises(InvariantError) as exc:
        format_amount_for_portal(Decimal("64217.50"), field="Schedule FA A3 initial value")
    assert str(exc.value) == (
        "InvariantError: Schedule FA A3 initial value must be a whole-rupee amount "
        "(no fraction, exponent or -0) (context keys: value)"
    )
    assert "64217" not in str(exc.value)


@pytest.mark.parametrize("value", ["5E+2", "-0", "500.00", "100.4", "-500.5"])
def test_format_amount_refuses_what_require_whole_rupees_refuses(value: str) -> None:
    """An exponent form, ``-0``, trailing zeros or any fraction is refused."""
    with pytest.raises(InvariantError) as exc:
        format_amount_for_portal(Decimal(value), field="Schedule FA A2 closing balance")
    assert exc.value.what == (
        "Schedule FA A2 closing balance must be a whole-rupee amount (no fraction, exponent or -0)"
    )
