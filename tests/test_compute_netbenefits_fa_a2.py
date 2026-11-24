"""Unit tests for the NetBenefits Table A2 cash-zero proof + row builder (#I11)."""

from __future__ import annotations

from dataclasses import replace
from datetime import date
from decimal import Decimal

import pytest

from ibkr_fa.compute.fa_a2 import FaA2AttestedPeak, FaA2StatusError
from ibkr_fa.compute.netbenefits_fa_a2 import (
    NetBenefitsA2Error,
    build_netbenefits_a2_row,
    names_fidelity_stock_plan_services,
    prove_dec31_cash_zero,
)
from ibkr_fa.exceptions import InvariantError
from ibkr_fa.models.account_identity import TaxpayerStatus
from ibkr_fa.models.fa_a2_row import FaA2NatureOfAmount, FaA2Row, FaA2Status
from ibkr_fa.normalize.netbenefits.statement_label import (
    ALL_REPORTS_ORDER,
    statement_among_all_reports,
)
from ibkr_fa.parser.netbenefits.confirmation_pdf import EspPurchase
from ibkr_fa.parser.netbenefits.statement_pdf import (
    ReportType,
    StatementCoreHolding,
    StatementData,
    StatementDividend,
    StatementMmktBuy,
    StatementWithholding,
)
from tests._crash_reports import assert_prints_no_marker
from tests._exact_refusals import INEXACT_PRODUCT
from tests._netbenefits import (
    ALL_REPORTS,
    a2_credit_refusal,
    a2_sweep_refusal,
    esp_purchase,
    statement_data,
    table_row,
)
from tests._netbenefits import statement_label as _statement
from tests._netbenefits_a2_identity import netbenefits_a2_identity as _identity


def _purchase(
    *,
    shares: str = "3.482",
    price: str = "192.12",
    contribution: str = "668.96",
    day: date = date(2025, 12, 31),
) -> EspPurchase:
    return esp_purchase(
        purchase_date=day,
        settlement_date=day,
        offering_period_end=day,
        shares=Decimal(shares),
        purchase_price_per_share_usd=Decimal(price),
        contribution_usd=Decimal(contribution),
    )


def _stmt(
    period_start: date,
    period_end: date,
    *,
    dividends: tuple[StatementDividend, ...] = (),
    withholdings: tuple[StatementWithholding, ...] = (),
    mmkt_buys: tuple[StatementMmktBuy, ...] = (),
    core_holding: StatementCoreHolding | None = None,
) -> StatementData:
    return statement_data(
        period_start,
        period_end,
        dividends=dividends,
        withholdings=withholdings,
        mmkt_buys=mmkt_buys,
        core_holding=core_holding,
    )


# --- cash-zero proof ----------------------------------------------------------

# #1042: refusals name a statement by its place among every report the proof
# gets (year-end reports included), an income event by its row in its statement
# table, and coverage or the opening date by the config field; never a value.
_WINDOW = (
    "the window from the later of Jan-1 and netbenefits.account.account_opening_date through Dec-31"
)
_TABLES = {
    "MMKT sweep": "Core Fund Activity",
    "dividend": "Dividends, Interest & Other Income",
    "withholding": "Taxes Withheld",
}


def _income_refusal(kind: str, *, statement: int, row: int) -> str:
    """The refusal for the in-window ``kind`` at ``row`` of ``statement``."""
    where = table_row(statement, row, _TABLES[kind])
    if kind == "MMKT sweep":
        return a2_sweep_refusal(where)
    return a2_credit_refusal(kind, where)


def _opening_refusal(calendar_year: int) -> str:
    return (
        f"netbenefits.account.account_opening_date is outside the FA year {calendar_year}; "
        "the Dec-31 cash can't be proven zero (a prior-year opening could carry cash in, a "
        f"later one contradicts a {calendar_year} holding) — file A2 manually"
    )


def _priced_refusal(statement: int) -> str:
    return (
        f"{_statement(statement)} is a PRICED Dec-31 statement (it states an account NAV); its "
        "stated account value is the authoritative A2 closing balance — file Table A2 from it "
        "manually (this pipeline derives A2 only from the unpriced year-end case)"
    )


def _assert_absent(message: str, *values: str) -> None:
    """None of ``values`` (input the old messages printed) is in ``message``."""
    for value in values:
        assert value.casefold() not in message.casefold(), value


def _year_end(
    period_start: date = date(2025, 1, 1),
    period_end: date = date(2025, 12, 31),
    *,
    account_nav_usd: Decimal | None = None,
) -> StatementData:
    """A Year-End Report, unpriced unless ``account_nav_usd`` is given."""
    return replace(
        _stmt(period_start, period_end),
        report_type=ReportType.YEAR_END,
        account_nav_usd=account_nav_usd,
    )


def test_statement_label_is_the_shared_expected_text() -> None:
    """The label the refusals name a statement by equals the text the tests write
    out, so they compare the code's text with an independent copy (#1685)."""
    assert ALL_REPORTS_ORDER == ALL_REPORTS
    assert statement_among_all_reports(2) == _statement(2)


def test_cash_zero_proof_passes_for_covered_income_free_window() -> None:
    prove_dec31_cash_zero(
        purchases=[_purchase()],
        statements=[_stmt(date(2025, 12, 1), date(2025, 12, 31))],
        account_opening_date=date(2025, 12, 31),
        calendar_year=2025,
    )


def test_cash_zero_proof_refuses_account_opened_before_cy() -> None:
    # #1042: names the config field and the CLI's year, never the configured date.
    with pytest.raises(NetBenefitsA2Error) as caught:
        prove_dec31_cash_zero(
            purchases=[_purchase()],
            statements=[_stmt(date(2024, 1, 1), date(2025, 12, 31))],
            account_opening_date=date(2024, 11, 14),
            calendar_year=2025,
        )
    assert str(caught.value) == _opening_refusal(2025)
    _assert_absent(str(caught.value), "2024-11-14")


def test_cash_zero_proof_refuses_account_opened_after_cy_end() -> None:
    # A future opening date would invert the window and silently skip the income
    # scan — refuse instead (an account opened after Dec-31 can't hold a CY lot).
    with pytest.raises(NetBenefitsA2Error) as caught:
        prove_dec31_cash_zero(
            purchases=[_purchase()],
            statements=[_stmt(date(2025, 12, 1), date(2025, 12, 31))],
            account_opening_date=date(2026, 11, 14),
            calendar_year=2025,
        )
    assert str(caught.value) == _opening_refusal(2025)
    _assert_absent(str(caught.value), "2026-11-14")


def test_cash_zero_proof_refuses_income_before_configured_opening() -> None:
    # A dividend earlier in the month than a (mis)configured opening date must NOT
    # be hidden: the income scan is not bounded below by the opening date.
    div = StatementDividend(date(2025, 12, 17), "EXAMPLE CORP", "99999E104", Decimal("1.00"))
    with pytest.raises(NetBenefitsA2Error) as caught:
        prove_dec31_cash_zero(
            purchases=[_purchase()],
            statements=[_stmt(date(2025, 12, 1), date(2025, 12, 31), dividends=(div,))],
            account_opening_date=date(2025, 12, 31),  # later than the 12-17 dividend
            calendar_year=2025,
        )
    assert str(caught.value) == _income_refusal("dividend", statement=1, row=1)
    _assert_absent(str(caught.value), "2025-12-17")


def test_cash_zero_proof_refuses_priced_dec31_statement() -> None:
    # A priced Dec-31 statement (stated account NAV) is out of scope — refuse and
    # tell the user to file A2 from it manually (the pipeline derives the unpriced case).
    # #1042: the unpriced year-end report before it counts, so the priced one is
    # statement 2; its NAV is never printed.
    priced = replace(
        _stmt(date(2025, 12, 1), date(2025, 12, 31)), account_nav_usd=Decimal("1234567.89")
    )
    with pytest.raises(NetBenefitsA2Error) as caught:
        prove_dec31_cash_zero(
            purchases=[_purchase()],
            statements=[_year_end(), priced],
            account_opening_date=date(2025, 12, 1),
            calendar_year=2025,
        )
    assert str(caught.value) == _priced_refusal(2)
    _assert_absent(str(caught.value), "1234567.89", "1,234,567.89")


def test_cash_zero_proof_refuses_contribution_residual() -> None:
    # contribution != round(shares*price) → unspent payroll cash can't be proven 0.
    # #1042: a backstop in fixed text; never the purchase date, the residual
    # (|3.482 x 192.12 - 900.00| = 231.03816) or the tolerance (0.101060).
    with pytest.raises(NetBenefitsA2Error) as caught:
        prove_dec31_cash_zero(
            purchases=[_purchase(contribution="900.00", day=date(2025, 11, 14))],
            statements=[_stmt(date(2025, 12, 1), date(2025, 12, 31))],
            account_opening_date=date(2025, 12, 31),
            calendar_year=2025,
        )
    assert str(caught.value) == (
        "a purchase confirmation's shares x purchase price differs from its Accumulated "
        "Contributions by more than the rounding tolerance; residual cash can't be proven zero"
    )
    _assert_absent(str(caught.value), "2025-11-14", "231.038", "0.10106")


def test_cash_zero_proof_refuses_statement_coverage_gap() -> None:
    # #1042: names the window by its config field, never the gap's dates.
    with pytest.raises(NetBenefitsA2Error) as caught:
        prove_dec31_cash_zero(
            purchases=[_purchase()],
            statements=[
                _stmt(date(2025, 8, 18), date(2025, 9, 30)),
                _stmt(date(2025, 11, 1), date(2025, 12, 31)),  # Oct missing
            ],
            account_opening_date=date(2025, 8, 18),
            calendar_year=2025,
        )
    assert str(caught.value) == (
        f"the monthly SPS statements leave a gap in {_WINDOW}; can't prove the account held no "
        "cash-generating activity there — supply the missing monthly statement"
    )
    _assert_absent(str(caught.value), "2025-10-01", "2025-10-31")


def test_cash_zero_proof_refuses_short_coverage() -> None:
    # #1042: names the window by its config field, never the last covered day or
    # the window's dates.
    with pytest.raises(NetBenefitsA2Error) as caught:
        prove_dec31_cash_zero(
            purchases=[_purchase()],
            statements=[_stmt(date(2025, 11, 14), date(2025, 12, 30))],  # stops a day short
            account_opening_date=date(2025, 11, 14),
            calendar_year=2025,
        )
    assert str(caught.value) == (
        f"the monthly SPS statements end before Dec-31, so they don't cover {_WINDOW}; supply "
        "the missing month(s)"
    )
    _assert_absent(str(caught.value), "2025-12-30", "2025-11-14", "2025-12-31")


def test_cash_zero_proof_refuses_dividend_in_window() -> None:
    div = StatementDividend(date(2025, 12, 31), "EXAMPLE CORP", "99999E104", Decimal("1.00"))
    with pytest.raises(NetBenefitsA2Error) as caught:
        prove_dec31_cash_zero(
            purchases=[_purchase()],
            statements=[_stmt(date(2025, 12, 1), date(2025, 12, 31), dividends=(div,))],
            account_opening_date=date(2025, 12, 31),
            calendar_year=2025,
        )
    assert str(caught.value) == _income_refusal("dividend", statement=1, row=1)
    _assert_absent(str(caught.value), "2025-12-31")


def test_cash_zero_proof_refuses_mmkt_sweep_in_window() -> None:
    mmkt = StatementMmktBuy(
        date(2025, 12, 17), "SAMPLE ISSUER CP", Decimal("1.090"), Decimal("1"), Decimal("1.09")
    )
    with pytest.raises(NetBenefitsA2Error) as caught:
        prove_dec31_cash_zero(
            purchases=[_purchase()],
            statements=[_stmt(date(2025, 12, 1), date(2025, 12, 31), mmkt_buys=(mmkt,))],
            account_opening_date=date(2025, 12, 1),
            calendar_year=2025,
        )
    assert str(caught.value) == _income_refusal("MMKT sweep", statement=1, row=1)
    _assert_absent(str(caught.value), "2025-12-17")


def test_cash_zero_proof_refuses_carried_core_holding_without_sweep() -> None:
    core = StatementCoreHolding(
        description="SAMPLE ISSUER CP FUND CL A",
        symbol="XQCP",
        quantity=Decimal("1.09"),
        price_per_unit_usd=Decimal("1.00"),
        ending_market_value_usd=Decimal("1.09"),
        eai_usd=Decimal("0.06"),
    )
    with pytest.raises(NetBenefitsA2Error) as caught:
        prove_dec31_cash_zero(
            purchases=[_purchase()],
            statements=[
                replace(
                    _stmt(date(2024, 12, 1), date(2024, 12, 31)),
                    report_type=ReportType.YEAR_END,
                ),
                _stmt(
                    date(2025, 12, 1),
                    date(2025, 12, 31),
                    core_holding=core,
                ),
            ],
            account_opening_date=date(2025, 12, 1),
            calendar_year=2025,
        )
    # The earlier year-end report counts in the carried holding's statement label.
    assert str(caught.value) == (
        f"{_statement(2)} lists a Core Account "
        "money-market fund holding in the FA year; it is a separate "
        "foreign asset this tool does not yet auto-disclose — file the fund's own Schedule FA "
        "Table A3 holding and include its value in the A2 account balances manually"
    )


def test_cash_zero_proof_surfaces_mmkt_guidance_when_dividend_is_swept() -> None:
    # A dividend swept into the fund: the MMKT-fund refusal
    # (which names the deferred Table A3 holding) must win over the dividend leg.
    # #1042: it names the sweep's row in Core Fund Activity, not the dividend's.
    div = StatementDividend(date(2025, 12, 17), "EXAMPLE CORP", "99999E104", Decimal("1.09"))
    mmkt = StatementMmktBuy(
        date(2025, 12, 17), "SAMPLE ISSUER CP", Decimal("1.09"), Decimal("1"), Decimal("1.09")
    )
    with pytest.raises(NetBenefitsA2Error) as caught:
        prove_dec31_cash_zero(
            purchases=[_purchase()],
            statements=[
                _stmt(date(2025, 12, 1), date(2025, 12, 31), dividends=(div,), mmkt_buys=(mmkt,))
            ],
            account_opening_date=date(2025, 12, 1),
            calendar_year=2025,
        )
    assert str(caught.value) == _income_refusal("MMKT sweep", statement=1, row=1)
    assert "row 1 of its Core Fund Activity table" in str(caught.value)
    assert _TABLES["dividend"] not in str(caught.value)
    _assert_absent(str(caught.value), "2025-12-17")


def test_cash_zero_proof_refuses_withholding_in_window() -> None:
    wht = StatementWithholding(date(2025, 12, 20), "EXAMPLE CORP", Decimal("-0.30"))
    with pytest.raises(NetBenefitsA2Error) as caught:
        prove_dec31_cash_zero(
            purchases=[_purchase()],
            statements=[_stmt(date(2025, 12, 1), date(2025, 12, 31), withholdings=(wht,))],
            account_opening_date=date(2025, 12, 1),
            calendar_year=2025,
        )
    assert str(caught.value) == _income_refusal("withholding", statement=1, row=1)
    _assert_absent(str(caught.value), "2025-12-20")


def _sweep(day: date) -> StatementMmktBuy:
    return StatementMmktBuy(day, "SAMPLE ISSUER CP", Decimal("1.09"), Decimal("1"), Decimal("1.09"))


def _dividend(day: date) -> StatementDividend:
    return StatementDividend(day, "EXAMPLE CORP", "99999E104", Decimal("1.00"))


def _withholding(day: date) -> StatementWithholding:
    return StatementWithholding(day, "EXAMPLE CORP", Decimal("-0.30"))


@pytest.mark.parametrize("kind", list(_TABLES), ids=["mmkt-sweep", "dividend", "withholding"])
def test_cash_zero_proof_names_the_income_row_and_statement_number(kind: str) -> None:
    """#1042: the refusal names the statement by its place among every report
    (the year-end report first counts) and the event by its row in its table:
    row 1 is dated after year-end and skipped, so row 2 refuses. Never the date."""
    after_year_end, in_window = date(2026, 1, 14), date(2025, 11, 14)
    period = (date(2025, 11, 1), date(2026, 1, 31))
    statement = {
        "MMKT sweep": _stmt(*period, mmkt_buys=(_sweep(after_year_end), _sweep(in_window))),
        "dividend": _stmt(*period, dividends=(_dividend(after_year_end), _dividend(in_window))),
        "withholding": _stmt(
            *period, withholdings=(_withholding(after_year_end), _withholding(in_window))
        ),
    }[kind]
    with pytest.raises(NetBenefitsA2Error) as caught:
        prove_dec31_cash_zero(
            purchases=[_purchase()],
            statements=[
                _year_end(),  # statement 1
                _stmt(date(2025, 10, 1), date(2025, 10, 31)),  # statement 2, no income
                statement,  # statement 3
            ],
            account_opening_date=date(2025, 10, 1),
            calendar_year=2025,
        )
    assert str(caught.value) == _income_refusal(kind, statement=3, row=2)
    _assert_absent(str(caught.value), "2025-11-14")


def test_cash_zero_proof_scans_statements_after_coverage_is_satisfied() -> None:
    # A clean Dec statement satisfies coverage, but a SECOND overlapping Dec
    # statement carrying a dividend must still be scanned and refuse (the income
    # scan is independent of the coverage early-return).
    div = StatementDividend(date(2025, 12, 20), "EXAMPLE CORP", "99999E104", Decimal("1.00"))
    with pytest.raises(NetBenefitsA2Error) as caught:
        prove_dec31_cash_zero(
            purchases=[_purchase()],
            statements=[
                _stmt(date(2025, 12, 1), date(2025, 12, 31)),  # clean, covers cy_end
                _stmt(date(2025, 12, 1), date(2025, 12, 31), dividends=(div,)),  # later, has income
            ],
            account_opening_date=date(2025, 12, 1),
            calendar_year=2025,
        )
    assert str(caught.value) == _income_refusal("dividend", statement=2, row=1)
    _assert_absent(str(caught.value), "2025-12-20")


def test_cash_zero_proof_refuses_priced_year_end_report() -> None:
    # A priced YEAR_END report (Jan-1..Dec-31) carries the authoritative Dec-31 NAV
    # — it must be refused too, not skipped by the SPS-only income scan.
    # #1042: the earlier December statement makes it statement 2; its NAV is never
    # printed.
    priced_ye = _year_end(account_nav_usd=Decimal("1234567.89"))
    with pytest.raises(NetBenefitsA2Error) as caught:
        prove_dec31_cash_zero(
            purchases=[_purchase()],
            statements=[
                _stmt(date(2024, 12, 1), date(2024, 12, 31)),
                priced_ye,
                _stmt(date(2025, 12, 1), date(2025, 12, 31)),
            ],
            account_opening_date=date(2025, 12, 1),
            calendar_year=2025,
        )
    assert str(caught.value) == _priced_refusal(2)
    _assert_absent(str(caught.value), "1234567.89", "1,234,567.89")


# --- year-end income rows (#1249) -----------------------------------------------

# Privacy markers on a year-end report's income rows: the name, the amounts and the day.
_YE_DAY = date(2025, 12, 19)
_YE_MARKERS = ("SYNTHQYEDIV", "2025-12-19")
_YE_ROWS = {
    "MMKT sweep": {
        "mmkt_buys": (
            StatementMmktBuy(
                _YE_DAY, "SYNTHQYEDIV", Decimal("7.40"), Decimal("1"), Decimal("7.40")
            ),
        )
    },
    "dividend": {
        "dividends": (StatementDividend(_YE_DAY, "SYNTHQYEDIV", "99999E104", Decimal("9.87")),)
    },
    "withholding": {
        "withholdings": (StatementWithholding(_YE_DAY, "SYNTHQYEDIV", Decimal("-2.47")),)
    },
}


def _prove_dec31(*statements: StatementData, opening: date = date(2025, 12, 1)) -> None:
    prove_dec31_cash_zero(
        purchases=[_purchase()],
        statements=list(statements),
        account_opening_date=opening,
        calendar_year=2025,
    )


@pytest.mark.parametrize(
    ("kind", "amount"),
    [("MMKT sweep", "7.40"), ("dividend", "9.87"), ("withholding", "2.47")],
    ids=["mmkt-sweep", "dividend", "withholding"],
)
def test_cash_zero_proof_refuses_year_end_income_on_or_before_dec31(kind: str, amount: str) -> None:
    """#1249: a year-end report's silence proves nothing, but an income row it
    prints is evidence, so one dated on or before Dec-31 refuses with the SPS
    statement's refusal. The year-end report starts first, so it is statement 1."""
    year_end = replace(_year_end(), **_YE_ROWS[kind])
    with pytest.raises(NetBenefitsA2Error) as caught:
        _prove_dec31(year_end, _stmt(date(2025, 12, 1), date(2025, 12, 31)))
    assert str(caught.value) == _income_refusal(kind, statement=1, row=1)
    assert_prints_no_marker(caught.value, markers=(*_YE_MARKERS, amount))


def test_cash_zero_proof_ignores_year_end_income_after_dec31() -> None:
    """#1249: the next year's year-end report prints income after Dec-31, which the
    Dec-31 cash proof doesn't scan, as on an SPS statement."""
    after = date(2026, 3, 5)
    rows = {
        "dividends": (StatementDividend(after, "SYNTHQYEDIV", "99999E104", Decimal("9.87")),),
        "withholdings": (StatementWithholding(after, "SYNTHQYEDIV", Decimal("-2.47")),),
        "mmkt_buys": (
            StatementMmktBuy(after, "SYNTHQYEDIV", Decimal("7.40"), Decimal("1"), Decimal("7.40")),
        ),
    }
    _prove_dec31(
        _stmt(date(2025, 12, 1), date(2025, 12, 31)),
        replace(_year_end(date(2026, 1, 1), date(2026, 12, 31)), **rows),
    )


@pytest.mark.parametrize(
    ("statements", "opening", "message"),
    [
        pytest.param(
            (_stmt(date(2025, 11, 1), date(2025, 11, 30)),),
            date(2025, 11, 1),
            f"the monthly SPS statements end before Dec-31, so they don't cover {_WINDOW}; "
            "supply the missing month(s)",
            id="short-coverage",
        ),
        pytest.param(
            (
                _stmt(date(2025, 10, 1), date(2025, 10, 31)),
                _stmt(date(2025, 12, 1), date(2025, 12, 31)),
            ),
            date(2025, 10, 1),
            f"the monthly SPS statements leave a gap in {_WINDOW}; can't prove the account held "
            "no cash-generating activity there — supply the missing monthly statement",
            id="coverage-gap",
        ),
        pytest.param(
            (
                _stmt(
                    date(2025, 12, 1),
                    date(2025, 12, 31),
                    withholdings=(_withholding(date(2025, 12, 20)),),
                ),
            ),
            date(2025, 12, 1),
            _income_refusal("withholding", statement=2, row=1),
            id="sps-income",
        ),
    ],
)
def test_cash_zero_proof_scans_year_end_income_after_the_existing_checks(
    statements: tuple[StatementData, ...], opening: date, message: str
) -> None:
    """#1249: an input that refuses today keeps its refusal: the year-end scan runs
    after the coverage walk and the SPS income scan, though the year-end report
    (statement 1) starts first."""
    year_end = replace(_year_end(), **_YE_ROWS["MMKT sweep"], **_YE_ROWS["dividend"])
    with pytest.raises(NetBenefitsA2Error) as caught:
        _prove_dec31(year_end, *statements, opening=opening)
    assert str(caught.value) == message
    assert_prints_no_marker(caught.value, markers=(*_YE_MARKERS, "9.87", "7.40"))


# --- A2 row builder -----------------------------------------------------------


def test_build_a2_row_identity_and_closing() -> None:
    result = build_netbenefits_a2_row(
        identity=_identity(),
        securities_inr=Decimal("743.30") * Decimal("89.47"),
        securities_usd=Decimal("743.30"),
        ttbr_dec31=Decimal("89.47"),
    )
    # Closing = NAV x TTBR (equal to Σ A3 INR), proven-zero cash and gross, peak
    # manual-fill.
    assert result.row == FaA2Row(
        country_code="2",
        country_name="UNITED STATES OF AMERICA",
        institution_name="Fidelity Stock Plan Services LLC",
        institution_address="245 Summer Street, Boston, MA",
        institution_zip="02210",
        account_number="I99999999",
        status=FaA2Status.BENEFICIAL_OWNER,
        account_opening_date=date(2025, 12, 31),
        peak_balance_inr=None,
        closing_balance_inr=Decimal("743.30") * Decimal("89.47"),
        nature_of_amount=FaA2NatureOfAmount.NO_AMOUNT,
        gross_amount_inr=Decimal("0"),
        base_currency="USD",
        audit_closing_balance_base_ccy=Decimal("743.30"),
        audit_ttbr_dec31=Decimal("89.47"),
        audit_account_number_from_config=False,
        audit_nav_cross_checked=False,
        audit_gross_components={},
        audit_period_end_cash_base_ccy=Decimal("0"),
    )


def test_build_a2_row_files_an_attested_peak() -> None:
    result = build_netbenefits_a2_row(
        identity=_identity(),
        securities_inr=Decimal("743.30") * Decimal("89.47"),
        securities_usd=Decimal("743.30"),
        ttbr_dec31=Decimal("89.47"),
        peak=FaA2AttestedPeak(peak_inr=Decimal("66503"), source="config", reason="supplied"),
    )
    assert result.row.peak_balance_inr == Decimal("66503")
    assert result.row.audit_peak_override_source == "config"


def test_build_a2_row_refuses_signing_authority_status() -> None:
    with pytest.raises(FaA2StatusError, match="signing-authority accounts aren't supported"):
        build_netbenefits_a2_row(
            identity=_identity(status=TaxpayerStatus.SIGNING_AUTHORITY),
            securities_inr=Decimal("743.30") * Decimal("89.47"),
            securities_usd=Decimal("743.30"),
            ttbr_dec31=Decimal("89.47"),
        )


def test_build_a2_row_refuses_a3_closing_that_differs_from_nav_x_ttbr() -> None:
    # Exact: one in the last digit is still a disagreement, never a tolerance.
    with pytest.raises(InvariantError) as excinfo:
        build_netbenefits_a2_row(
            identity=_identity(),
            securities_inr=Decimal("66503.0509"),
            securities_usd=Decimal("743.30"),
            ttbr_dec31=Decimal("89.47"),
        )
    assert str(excinfo.value) == (
        "InvariantError: NetBenefits A2 closing: Σ A3 closing INR differs from securities USD "
        "x Dec-31 TTBR (context keys: a3_closing_inr, nav_closing_inr)"
    )
    assert excinfo.value.context == (("a3_closing_inr", None), ("nav_closing_inr", None))


# #1250: a 33-digit securities total at ₹89.47/$ is ₹66503.4999… to every digit,
# which files ₹66503. A 28-digit product rounds it to ₹66503.50, which files ₹66504.
_LONG_USD = Decimal("743.305018441935844417123058008270")
_LONG_INR = "66503.49999999999999999999999999991690"
_ROUNDED_INR = "66503.50000000000000000000000"


def test_build_a2_row_files_the_exact_securities_closing() -> None:
    """#1250: Σ A3 closing equal to the exact securities USD x TTBR passes and
    the row files that exact closing, ₹66503. Main compared it with its 28-digit
    product and refused."""
    result = build_netbenefits_a2_row(
        identity=_identity(),
        securities_inr=Decimal(_LONG_INR),
        securities_usd=_LONG_USD,
        ttbr_dec31=Decimal("89.47"),
    )
    closing = result.row.closing_balance_inr
    assert closing is not None
    assert closing.compare_total(Decimal(_LONG_INR)) == 0
    assert result.row.filed.closing_balance_inr == Decimal("66503")


def test_build_a2_row_refuses_a3_closing_equal_to_the_rounded_product() -> None:
    """#1250: a Σ A3 closing equal to the 28-digit rounded product now differs
    from the exact product, so it refuses. Main's rounded product matched it."""
    with pytest.raises(InvariantError) as excinfo:
        build_netbenefits_a2_row(
            identity=_identity(),
            securities_inr=Decimal(_ROUNDED_INR),
            securities_usd=_LONG_USD,
            ttbr_dec31=Decimal("89.47"),
        )
    assert str(excinfo.value) == (
        "InvariantError: NetBenefits A2 closing: Σ A3 closing INR differs from securities USD "
        "x Dec-31 TTBR (context keys: a3_closing_inr, nav_closing_inr)"
    )
    assert excinfo.value.context == (("a3_closing_inr", None), ("nav_closing_inr", None))


@pytest.mark.parametrize(
    "securities_usd",
    ["9E+999999", "1" * 999],
    ids=["past-the-largest-exponent", "past-1000-digits"],
)
def test_build_a2_row_refuses_a_securities_product_that_cannot_be_exact(
    securities_usd: str,
) -> None:
    """#1250: past the exponent or the 1000-digit cap securities USD x TTBR
    refuses, naming no value. Main raised a raw Overflow, or refused the rounded
    product as differing from Σ A3 closing."""
    with pytest.raises(InvariantError) as excinfo:
        build_netbenefits_a2_row(
            identity=_identity(),
            securities_inr=Decimal("0"),
            securities_usd=Decimal(securities_usd),
            ttbr_dec31=Decimal("89.47"),
        )
    assert str(excinfo.value) == (
        f"InvariantError: NetBenefits A2 closing, securities INR: {INEXACT_PRODUCT}"
    )
    assert excinfo.value.context == ()
    assert excinfo.value.__cause__ is None
    assert excinfo.value.__context__ is None


_NOT_FINITE = (None, Decimal("NaN"), Decimal("sNaN"), Decimal("Infinity"), Decimal("-Infinity"))


@pytest.mark.parametrize(
    ("field", "value", "what"),
    [
        pytest.param(field, value, what, id=f"{field}={value}")
        for field, what, values in (
            ("securities_inr", "finite non-negative Σ A3 closing INR", (Decimal("-0.01"),)),
            ("securities_usd", "finite non-negative securities USD total", (Decimal("-0.01"),)),
            ("ttbr_dec31", "finite positive December 31 TTBR", (Decimal("0"), Decimal("-89.47"))),
        )
        for value in (*_NOT_FINITE, *values)
    ],
)
def test_build_a2_row_refuses_a_missing_non_finite_or_wrong_sign_total(
    field: str, value: Decimal | None, what: str
) -> None:
    inputs: dict[str, object] = {
        "securities_inr": Decimal("743.30") * Decimal("89.47"),
        "securities_usd": Decimal("743.30"),
        "ttbr_dec31": Decimal("89.47"),
        field: value,
    }
    with pytest.raises(InvariantError) as excinfo:
        build_netbenefits_a2_row(identity=_identity(), **inputs)  # ty: ignore[invalid-argument-type]
    assert (
        str(excinfo.value)
        == f"InvariantError: NetBenefits A2 needs a {what} (context keys: {field})"
    )
    assert excinfo.value.context == ((field, None),)


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("Fidelity Stock Plan Services LLC", True),
        ("Fidelity Stock Plan Services, LLC", True),
        ("Fidelity Stock Plan Services", True),
        ("FIDELITY STOCK PLAN SERVICES L.L.C.", True),
        ("  fidelity-stock-plan-services llc ", True),
        ("National Financial Services LLC", False),
        ("Example Custodian LLC", False),
        ("Fidelity Stock Plan Services Inc", False),
        ("Fidelity Brokerage Services LLC", False),
    ],
)
def test_names_fidelity_stock_plan_services(name: str, *, expected: bool) -> None:
    assert names_fidelity_stock_plan_services(name) is expected
