"""Tests for :mod:`ibkr_fa.reference.us_market_calendar`."""

from __future__ import annotations

from datetime import date

from ibkr_fa.reference.us_market_calendar import (
    previous_us_market_session,
    supports_calendar_year,
    us_market_session_on_or_before,
    us_market_sessions,
)


def test_cy2025_has_250_sessions() -> None:
    assert len(us_market_sessions(date(2025, 1, 1), date(2025, 12, 31))) == 250


def test_session_before_first_2025_session_skips_new_year() -> None:
    assert previous_us_market_session(date(2025, 1, 2)) == date(2024, 12, 31)


def test_one_off_carter_mourning_closure_is_not_a_session() -> None:
    assert previous_us_market_session(date(2025, 1, 10)) == date(2025, 1, 8)
    assert us_market_session_on_or_before(date(2025, 1, 9)) == date(2025, 1, 8)


def test_only_cy2025_is_supported() -> None:
    assert supports_calendar_year(2025)
    assert not supports_calendar_year(2024)
    assert not supports_calendar_year(2026)
