"""Tests for :mod:`ibkr_fa.models.tax_year` (#603)."""

from __future__ import annotations

from datetime import date

import pytest

import ibkr_fa.models
import ibkr_fa.pipeline
from ibkr_fa.exceptions import InvariantError
from ibkr_fa.models.tax_year import FinancialYear, starting_ay_for_calendar_year


def test_financial_year_dataclass() -> None:
    fy = FinancialYear(start_year=2025)
    assert fy.end_year == 2026
    assert fy.end_year_2digit == 26
    assert fy.start_date == date(2025, 4, 1)
    assert fy.end_date == date(2026, 3, 31)
    assert fy.assessment_year_start == 2026
    assert str(fy) == "2025-26"

    turn_of_century = FinancialYear(start_year=2099)
    assert turn_of_century.end_year == 2100
    assert turn_of_century.end_year_2digit == 0
    assert str(turn_of_century) == "2099-00"


def test_financial_year_start_year_lower_bound() -> None:
    with pytest.raises(InvariantError):
        FinancialYear(start_year=1899)


def test_financial_year_start_year_upper_bound() -> None:
    boundary = FinancialYear(start_year=9998)
    assert boundary.end_year == 9999
    assert boundary.end_date == date(9999, 3, 31)

    with pytest.raises(InvariantError):
        FinancialYear(start_year=9999)


def test_starting_ay_for_calendar_year() -> None:
    """CY 2025 is filed in AY 2026-27."""
    assert starting_ay_for_calendar_year(2025) == 2026


def test_pipeline_re_exports_are_the_moved_objects() -> None:
    """Callers that still import from ``ibkr_fa.pipeline`` get the same objects."""
    assert ibkr_fa.pipeline.FinancialYear is ibkr_fa.models.FinancialYear
    assert ibkr_fa.models.FinancialYear is FinancialYear
    assert ibkr_fa.pipeline._starting_ay_for_calendar_year is starting_ay_for_calendar_year
    assert ibkr_fa.models.starting_ay_for_calendar_year is starting_ay_for_calendar_year
