"""Tests for :mod:`ibkr_fa.compute.source_coverage` (#625).

Schedule FA needs an export that ends on Dec 31 of the calendar year and
starts on or before Jan 1 (or Flex lots reported at Dec 31), and Schedule
OS, CG and FSI an export that contains the Indian FY (Apr 1 - Mar 31).
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import date
from decimal import Decimal

import pytest

from ibkr_fa.compute.lot_matching import match_fifo
from ibkr_fa.compute.source_coverage import (
    CoverageGap,
    first_coverage_gap,
    require_calendar_year_period,
    require_financial_year_contained_in_period,
    require_open_lots_at_calendar_year_end,
)
from ibkr_fa.exceptions import LotMatchingError, ParseError
from ibkr_fa.models import FinancialYear, OpenLot, Side, StatementPeriod
from ibkr_fa.types import CountryCode, Isin, Ticker
from tests._lot_matching_inputs import MatchInstrument as _Instrument
from tests._lot_matching_inputs import close_execution as _close
from tests._lot_matching_inputs import open_execution as _open


def _fy(start_year: int) -> FinancialYear:
    """Build an Indian FY: Apr 1, YYYY -- Mar 31, YYYY+1."""
    return FinancialYear(start_year=start_year)


# ---------------------------------------------------------------------------
# require_financial_year_contained_in_period (Schedule OS, CG and FSI)
# T3 (Codex CLI) PR #163 P1: CG needs a wider window than the exact FY
# because FIFO matching requires the original opening trades that may
# pre-date the reporting FY.
# ---------------------------------------------------------------------------


def test_contained_validator_accepts_exact_fy_match() -> None:
    period = StatementPeriod(date(2025, 4, 1), date(2026, 3, 31))
    require_financial_year_contained_in_period(period, financial_year=_fy(2025))


def test_contained_validator_accepts_wider_multi_year_export() -> None:
    """A 2020-2026 export legitimately covers FY 2025-26 with extra
    history needed for FIFO matching of pre-FY-acquired lots."""
    period = StatementPeriod(date(2020, 1, 1), date(2026, 12, 31))
    require_financial_year_contained_in_period(period, financial_year=_fy(2025))


def test_contained_validator_accepts_extended_start() -> None:
    """Pre-FY start (covers older acquisitions) is fine."""
    period = StatementPeriod(date(2023, 1, 1), date(2026, 3, 31))
    require_financial_year_contained_in_period(period, financial_year=_fy(2025))


def test_contained_validator_accepts_extended_end() -> None:
    """Post-FY end is fine."""
    period = StatementPeriod(date(2025, 4, 1), date(2027, 12, 31))
    require_financial_year_contained_in_period(period, financial_year=_fy(2025))


def test_contained_validator_rejects_start_after_fy_start() -> None:
    """Export start inside the FY → missing earlier opens → reject."""
    period = StatementPeriod(date(2025, 5, 1), date(2026, 3, 31))
    with pytest.raises(ParseError) as exc:
        require_financial_year_contained_in_period(period, financial_year=_fy(2025))
    assert "does not fully contain" in exc.value.message
    assert "FIFO" in exc.value.message or "lot history" in exc.value.message


def test_contained_validator_rejects_end_before_fy_end() -> None:
    """Export end before FY end → missing late-FY closures → reject."""
    period = StatementPeriod(date(2025, 4, 1), date(2025, 12, 31))
    with pytest.raises(ParseError) as exc:
        require_financial_year_contained_in_period(period, financial_year=_fy(2025))
    assert "does not fully contain" in exc.value.message


def test_contained_validator_rejects_cy_aligned_export() -> None:
    """Most likely user mistake — Jan-Dec export when CG needs Apr-Mar+."""
    period = StatementPeriod(date(2025, 1, 1), date(2025, 12, 31))
    with pytest.raises(ParseError) as exc:
        require_financial_year_contained_in_period(period, financial_year=_fy(2025))
    assert "Apr 1, 2025" in exc.value.message
    assert "Mar 31, 2026" in exc.value.message


@pytest.mark.parametrize(
    ("start", "end"),
    [
        pytest.param(date(2025, 4, 2), date(2026, 3, 31), id="starts-one-day-late"),
        pytest.param(date(2025, 4, 1), date(2026, 3, 30), id="ends-one-day-early"),
        pytest.param(date(2024, 4, 1), date(2025, 3, 31), id="previous-fy"),
    ],
)
def test_contained_validator_rejects_a_period_one_day_short_or_another_fy(
    start: date, end: date
) -> None:
    with pytest.raises(ParseError) as exc:
        require_financial_year_contained_in_period(
            StatementPeriod(start, end), financial_year=_fy(2025)
        )
    assert "does not fully contain the Indian financial year 2025-26" in exc.value.message


def test_contained_validator_accepts_another_fy_positive_control() -> None:
    period = StatementPeriod(date(2024, 4, 1), date(2025, 3, 31))
    require_financial_year_contained_in_period(period, financial_year=_fy(2024))


def test_contained_validator_report_name_kwarg_propagates() -> None:
    period = StatementPeriod(date(2025, 5, 1), date(2025, 12, 31))
    with pytest.raises(ParseError) as exc:
        require_financial_year_contained_in_period(
            period,
            financial_year=_fy(2025),
            report_name="My custom report",
        )
    assert "My custom report" in exc.value.message


# ---- CY enforcement ------------------------------------------------------


def test_require_cy_period_accepts_exact_match() -> None:
    period = StatementPeriod(date(2025, 1, 1), date(2025, 12, 31))
    require_calendar_year_period(period, calendar_year=2025)


def test_require_cy_period_rejects_partial_year() -> None:
    period = StatementPeriod(date(2025, 6, 1), date(2025, 12, 31))
    with pytest.raises(ParseError) as exc:
        require_calendar_year_period(period, calendar_year=2025)
    assert str(exc.value) == _CY_2025_REFUSAL


def test_require_cy_period_rejects_a_start_one_day_into_the_year() -> None:
    period = StatementPeriod(date(2025, 1, 2), date(2025, 12, 31))
    with pytest.raises(ParseError) as exc:
        require_calendar_year_period(period, calendar_year=2025)
    assert str(exc.value) == _CY_2025_REFUSAL


def test_require_cy_period_rejects_fy_aligned_period() -> None:
    """Apr-Mar FY statement is the most likely user mistake (Indian
    users default to FY thinking). Error must point them at the
    fix -- re-export with calendar-year date range."""
    period = StatementPeriod(date(2025, 4, 1), date(2026, 3, 31))
    with pytest.raises(ParseError) as exc:
        require_calendar_year_period(period, calendar_year=2025)
    assert "re-export" in exc.value.message.lower()


@pytest.mark.parametrize(
    "start",
    [date(2024, 1, 1), date(2024, 12, 31), date(2011, 7, 19)],
    ids=["one-year-earlier", "the-day-before", "years-earlier"],
)
def test_require_cy_period_accepts_multi_year(start: date) -> None:
    """#1288: a statement that starts before Jan 1 can hold the opening trades
    of holdings bought in earlier years; it must still end on Dec 31."""
    require_calendar_year_period(StatementPeriod(start, date(2025, 12, 31)), calendar_year=2025)


@pytest.mark.parametrize(
    "end",
    [date(2025, 12, 30), date(2026, 1, 1), date(2026, 12, 31), date(2024, 12, 31)],
    ids=["a-day-early", "a-day-late", "a-year-late", "a-year-early"],
)
def test_require_cy_period_rejects_a_multi_year_period_not_ending_dec_31(end: date) -> None:
    """Open Positions is the period-end snapshot, so a period starting before
    Jan 1 must still end on Dec 31 of the calendar year."""
    with pytest.raises(ParseError) as exc:
        require_calendar_year_period(StatementPeriod(date(2023, 1, 1), end), calendar_year=2025)
    assert str(exc.value) == _CY_2025_REFUSAL


def test_require_cy_period_wrong_year() -> None:
    """Statement for CY 2024 passed to a CY 2025 invocation."""
    period = StatementPeriod(date(2024, 1, 1), date(2024, 12, 31))
    with pytest.raises(ParseError) as exc:
        require_calendar_year_period(period, calendar_year=2025)
    assert "2025" in exc.value.message


# ---- the period refusals never print the statement's dates (#1047) -------

_PERIOD_LOCATION = "ParseError in section 'Statement' at row 0, column 'Period': "
_CY_2025_REFUSAL = (
    f"{_PERIOD_LOCATION}the Activity Statement period must end on Dec 31, 2025 and start "
    "on or before Jan 1, 2025. Schedule FA Table A3 is calendar-year based and rebuilds "
    "each holding from its opening trades; re-export the Activity Statement from Jan 1, "
    "2025, or from your account's first trade if that is earlier, through Dec 31, 2025."
)
_FY_2025_CONTAINED_REFUSAL = (
    f"{_PERIOD_LOCATION}the Activity Statement period does not fully contain the "
    "Indian financial year 2025-26 (2025-04-01 to 2026-03-31). Indian "
    "financial-year-based reports that need historical trade context (Schedule CG) "
    "requires the export to cover at least Apr 1, 2025 through Mar 31, 2026 and "
    "typically a much wider window — re-export the Activity Statement from your "
    "account's first trade, or from Apr 1, 2025 if that is earlier (a multi-year export "
    "is normal for Schedule CG since the FIFO match needs the opening trade of every lot "
    "held during the FY, including lots sold in it)."
)
_FY_2025_CONTAINED_NO_LOT_REFUSAL = (
    f"{_PERIOD_LOCATION}the Activity Statement period does not fully contain the "
    "Indian financial year 2025-26 (2025-04-01 to 2026-03-31). Schedule OS dividend "
    "reporting requires the export to cover at least Apr 1, 2025 through Mar 31, 2026; "
    "re-export the Activity Statement with date range Apr 1, 2025 - Mar 31, 2026, or a "
    "wider one."
)


@pytest.mark.parametrize(
    ("check", "expected"),
    [
        pytest.param(
            lambda period: require_calendar_year_period(period, calendar_year=2025),
            _CY_2025_REFUSAL,
            id="calendar-year",
        ),
        pytest.param(
            lambda period: require_financial_year_contained_in_period(
                period, financial_year=_fy(2025)
            ),
            _FY_2025_CONTAINED_REFUSAL,
            id="contains-financial-year",
        ),
        pytest.param(
            lambda period: require_financial_year_contained_in_period(
                period,
                financial_year=_fy(2025),
                report_name="Schedule OS dividend reporting",
                needs_lot_history=False,
            ),
            _FY_2025_CONTAINED_NO_LOT_REFUSAL,
            id="contains-financial-year-without-lot-history",
        ),
    ],
)
def test_period_refusals_print_only_the_cli_derived_bounds(
    check: Callable[[StatementPeriod], None], expected: str
) -> None:
    """Each period refusal is fixed text plus the CY / FY bounds derived
    from the CLI's year flag. The statement's own dates, none of which
    is a bound here, are input, so neither the message nor
    ``raw_value`` carries them (#1047)."""
    with pytest.raises(ParseError) as exc:
        check(StatementPeriod(date(2025, 3, 1), date(2026, 2, 27)))
    assert str(exc.value) == expected
    assert exc.value.raw_value == ""
    for value in ("2025-03-01", "2026-02-27", "20250301", "20260227"):
        assert value not in str(exc.value)


def test_calendar_year_refusal_of_an_earlier_start_prints_no_statement_date() -> None:
    """A period that starts years before Jan 1 but ends on another day than
    Dec 31 refuses with the fixed text; neither of its dates is printed."""
    with pytest.raises(ParseError) as exc:
        require_calendar_year_period(
            StatementPeriod(date(2019, 3, 7), date(2025, 11, 28)), calendar_year=2025
        )
    assert str(exc.value) == _CY_2025_REFUSAL
    assert exc.value.raw_value == ""
    for value in ("2019", "03-07", "11-28", "0307", "1128", "March", "November"):
        assert value not in str(exc.value)


# ---- following the period refusals' remedies clears them (#1425) ----------


def test_cy_refusal_remedy_from_jan_1_when_first_trade_is_later() -> None:
    """An account whose first trade is on 2025-06-02: a statement from that
    trade still starts after Jan 1 and refuses; the remedy's statement from
    Jan 1, the earlier date, passes."""
    with pytest.raises(ParseError) as exc:
        require_calendar_year_period(
            StatementPeriod(date(2025, 6, 2), date(2025, 12, 31)), calendar_year=2025
        )
    assert str(exc.value) == _CY_2025_REFUSAL
    require_calendar_year_period(
        StatementPeriod(date(2025, 1, 1), date(2025, 12, 31)), calendar_year=2025
    )


def test_cg_remedy_from_first_trade_covers_a_lot_sold_in_the_fy() -> None:
    """The XQAB lot opened on 2024-06-03 is still open; the XQAA lot, opened
    on 2023-06-01, the account's first trade, was sold on 2025-06-02. A
    statement from the oldest still-open lot passes the FY check, but FIFO
    can't match the sale; one from the first trade holds both openings."""
    xqaa = _Instrument("XQAA", "US99995A1054")
    xqab = _Instrument("XQAB", "US99995B1044")
    xqaa_open = _open(0, "2023-06-01 10:00:00", "1", "10", instrument=xqaa)
    xqab_open = _open(1, "2024-06-03 10:00:00", "1", "10", instrument=xqab)
    xqaa_close = _close(2, "2025-06-02 10:00:00", "1", "12", trade_price="12", instrument=xqaa)
    closes = {Ticker("XQAB"): Decimal("11")}
    fy = _fy(2025)

    require_financial_year_contained_in_period(
        StatementPeriod(date(2024, 6, 3), date(2026, 3, 31)), financial_year=fy
    )
    with pytest.raises(LotMatchingError) as exc:
        match_fifo([xqab_open, xqaa_close], dec31_close_prices=closes, report_date=fy.end_date)
    assert exc.value.row_index == xqaa_close.row_index

    open_lots, closed_lots = match_fifo(
        [xqaa_open, xqab_open, xqaa_close], dec31_close_prices=closes, report_date=fy.end_date
    )
    assert [lot.symbol for lot in closed_lots] == [Ticker("XQAA")]
    assert [lot.symbol for lot in open_lots] == [Ticker("XQAB")]
    require_financial_year_contained_in_period(
        StatementPeriod(date(2023, 6, 1), date(2026, 3, 31)),
        financial_year=fy,
        needs_lot_history=True,
    )


# ---- Flex ReportDate (moved from the Flex parser, #625) ------------------


def _lot(report_date: date, *, open_date: date) -> OpenLot:
    return OpenLot(
        currency_primary="USD",
        symbol=Ticker("XQAW"),
        description="SAMPLE ISSUER AW INC",
        isin=Isin("US99990E1038"),
        issuer_country_code=CountryCode("US"),
        report_date=report_date,
        quantity=Decimal("15"),
        mark_price=Decimal("32.70"),
        cost_basis_money=Decimal("386.45"),
        side=Side.LONG,
        open_date_time=open_date,
    )


def test_open_lots_at_calendar_year_end_accepts_dec_31() -> None:
    require_open_lots_at_calendar_year_end(
        [_lot(date(2026, 12, 31), open_date=date(2026, 3, 18))], calendar_year=2026
    )


def test_open_lots_at_calendar_year_end_accepts_no_lots() -> None:
    require_open_lots_at_calendar_year_end([], calendar_year=2026)


def _report_date_refusal(row_index: int) -> str:
    return (
        f"ParseError in section 'Open Positions (LOT)' at row {row_index}, column "
        "'ReportDate': v1 requires the Flex Query ReportDate to equal Dec 31 of the "
        "target calendar year (2026-12-31); this lot's ReportDate is another date. "
        "Re-run your Flex Query with ReportDate set to 2026-12-31."
    )


@pytest.mark.parametrize(
    ("report_date", "open_date"),
    [(date(2026, 7, 14), date(2026, 3, 18)), (date(2025, 12, 31), date(2025, 3, 18))],
    ids=["mid-year", "wrong-year"],
)
def test_open_lots_at_calendar_year_end_refuses_without_the_report_date(
    report_date: date, open_date: date
) -> None:
    """The refusal names the LOT row and the Dec 31 the CY needs, never
    the lot's own ReportDate, in the message or ``raw_value`` (#1047)."""
    with pytest.raises(ParseError) as exc:
        require_open_lots_at_calendar_year_end(
            [_lot(report_date, open_date=open_date)], calendar_year=2026
        )
    assert str(exc.value) == _report_date_refusal(0)
    assert exc.value.raw_value == ""
    for value in (report_date.isoformat(), f"{report_date:%Y%m%d}", open_date.isoformat()):
        assert value not in str(exc.value)


def test_open_lots_at_calendar_year_end_points_at_the_offending_lot() -> None:
    lots = [
        _lot(date(2026, 12, 31), open_date=date(2026, 3, 18)),
        _lot(date(2026, 7, 14), open_date=date(2026, 3, 18)),
    ]
    with pytest.raises(ParseError) as exc:
        require_open_lots_at_calendar_year_end(lots, calendar_year=2026)
    assert exc.value.row_index == 1
    assert str(exc.value) == _report_date_refusal(1)
    assert exc.value.raw_value == ""
    assert "20260714" not in str(exc.value)


# ---------------------------------------------------------------------------
# first_coverage_gap (#646)
# ---------------------------------------------------------------------------

_JAN = (date(2025, 1, 1), date(2025, 1, 31))
_FEB = (date(2025, 2, 1), date(2025, 2, 28))
_MAR = (date(2025, 3, 1), date(2025, 3, 31))


@pytest.mark.parametrize(
    ("periods", "expected"),
    [
        ([], CoverageGap(date(2025, 1, 1), date(2025, 3, 31), short_end=True)),
        ([_FEB, _MAR], CoverageGap(date(2025, 1, 1), date(2025, 1, 31), short_end=False)),
        ([_JAN, _MAR], CoverageGap(date(2025, 2, 1), date(2025, 2, 28), short_end=False)),
        ([_JAN, _FEB], CoverageGap(date(2025, 3, 1), date(2025, 3, 31), short_end=True)),
        ([_JAN, (date(2025, 1, 15), date(2025, 3, 31))], None),
        ([_MAR, _JAN, _FEB], None),
        ([(date(2024, 12, 1), date(2025, 4, 30))], None),
    ],
    ids=[
        "no-periods",
        "gap-at-start",
        "internal-gap",
        "short-end",
        "overlapping",
        "exact-fit-unsorted",
        "window-inside-one-period",
    ],
)
def test_first_coverage_gap(periods: list[tuple[date, date]], expected: CoverageGap | None) -> None:
    gap = first_coverage_gap(periods, window_start=date(2025, 1, 1), window_end=date(2025, 3, 31))

    assert gap == expected


def test_first_coverage_gap_ignores_periods_after_the_window_is_covered() -> None:
    later = (date(2025, 6, 1), date(2025, 6, 30))

    assert (
        first_coverage_gap(
            [_JAN, _FEB, _MAR, later], window_start=date(2025, 1, 1), window_end=date(2025, 3, 31)
        )
        is None
    )
