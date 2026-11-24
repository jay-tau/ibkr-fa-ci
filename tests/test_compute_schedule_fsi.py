"""Tests for :mod:`ibkr_fa.compute.schedule_fsi`."""

from __future__ import annotations

from dataclasses import fields, replace
from datetime import date, timedelta
from decimal import ROUND_HALF_UP, Decimal
from typing import override

import pytest

from ibkr_fa.compute.schedule_cg import (
    ScheduleCgStcgComputeResult,
    ScheduleCgStcgLine,
)
from ibkr_fa.compute.schedule_cg_ltcg import (
    ScheduleCgLtcgComputeResult,
    ScheduleCgLtcgLine,
)
from ibkr_fa.compute.schedule_fsi import (
    InterestPayerCountryError,
    ScheduleFsiComputeResult,
    ScheduleFsiWhtContribution,
    _merge_results,
    compute_schedule_fsi,
    compute_schedule_fsi_from_business_income,
    compute_schedule_fsi_from_capital_gains,
    compute_schedule_fsi_from_dividends,
    compute_schedule_fsi_from_interest,
    compute_schedule_fsi_from_other_income,
    compute_schedule_fsi_from_withholding_tax,
    require_fsi_relief_matches_portal,
)
from ibkr_fa.compute.schedule_os import (
    ScheduleOsComputeResult,
    ScheduleOsDividendLine,
    ScheduleOsDomesticDividendLine,
)
from ibkr_fa.compute.schedule_os_interest import (
    ScheduleOsInterestComputeResult,
    ScheduleOsInterestLine,
)
from ibkr_fa.compute.schedule_os_other_income import (
    ScheduleOsOtherIncomeComputeResult,
    ScheduleOsOtherIncomeLine,
)
from ibkr_fa.compute.schedule_os_substitute_dividend import (
    ScheduleOsSubstituteDividendComputeResult,
    ScheduleOsSubstituteDividendLine,
)
from ibkr_fa.compute.wht_reversal_pairing import UnprovenWhtReversalError
from ibkr_fa.config import ScheduleOsIndianAdrRoutingMode
from ibkr_fa.fx.policy import FxPolicy
from ibkr_fa.invariants import InvariantError
from ibkr_fa.models import DistributionKind, HeadOfIncome, ScheduleFsiRow, WithholdingTax
from ibkr_fa.models.money import to_whole_rupees
from ibkr_fa.reference.country_codes import CountryCodeNotFoundError
from ibkr_fa.types import CountryCode, Isin, Ticker
from tests._foreign_tax_refusals import (
    NO_ORIGINAL,
    TWO_FY_CANDIDATES,
    assert_no_wht_echo,
    unproven_reversal_message,
)
from tests._ftc_warnings import PORTAL_RELIEF_REFUSAL

_US = CountryCode("US")

# ---- shared fixtures -----------------------------------------------------


def _foreign_div_line(
    *,
    symbol: str,
    isin: str,
    issuer_country_iso2: str,
    payment_date: date,
    amount_usd: Decimal,
    rate: Decimal,
) -> ScheduleOsDividendLine:
    """Construct a valid foreign-source dividend line for tests."""
    if payment_date.month == 1:
        fx_specified_date = date(payment_date.year - 1, 12, 31)
    else:
        fx_specified_date = date(payment_date.year, payment_date.month, 1) - timedelta(days=1)
    description = f"{symbol}({isin}) Cash Dividend USD {amount_usd} per Share (Ordinary Dividend)"
    return ScheduleOsDividendLine(
        symbol=Ticker(symbol),
        isin=Isin(isin),
        issuer_country_iso2=issuer_country_iso2,
        payment_date=payment_date,
        fx_specified_date=fx_specified_date,
        fx_rate_publication_date=fx_specified_date,
        fx_rate_inr_per_usd=rate,
        amount_gross_usd=amount_usd,
        amount_gross_inr=amount_usd * rate,
        description=description,
        kind=DistributionKind.CASH_DIVIDEND,
    )


def _empty_os_result() -> ScheduleOsComputeResult:
    return ScheduleOsComputeResult(lines=[], domestic_lines=[])


def _other_income_line(
    amount_inr: str,
    *,
    source_country_iso2: CountryCode | None = _US,
) -> ScheduleOsOtherIncomeLine:
    return ScheduleOsOtherIncomeLine(
        nature="Securities lending fee",
        payment_date=date(2025, 6, 15),
        fx_specified_date=date(2026, 3, 31),
        fx_rate_publication_date=date(2026, 3, 31),
        fx_rate_inr_per_usd=Decimal("80"),
        amount_gross_usd=Decimal(amount_inr) / Decimal("80"),
        amount_gross_inr=Decimal(amount_inr),
        description="Stock Lending",
        source_row_index=1,
        source_country_iso2=source_country_iso2,
    )


# ---- compute_schedule_fsi_from_dividends — single country ----------------


def test_no_foreign_dividends_yields_empty_result() -> None:
    """An OS compute result with zero foreign-source dividend
    lines (e.g., purely Indian-source) must produce zero FSI rows —
    NOT a row with income_inr=0 (which would violate the model
    invariant). An in-FY return of capital used to be another way
    to get none; it has refused at the OS layer since #1310."""
    result = compute_schedule_fsi_from_dividends(_empty_os_result())
    assert result.rows == []


def test_positive_business_income_materialises_business_profession_row() -> None:
    result = compute_schedule_fsi_from_business_income(
        Decimal("8500.85"),
        source_country_iso2="US",
        event_count=6,
    )

    assert len(result.rows) == 1
    row = result.rows[0]
    assert row.head_of_income is HeadOfIncome.BUSINESS_PROFESSION
    assert row.income_inr == Decimal("8500.85")
    assert row.foreign_tax_inr == Decimal()
    assert row.event_count == 6


def test_non_positive_business_income_creates_no_fsi_row() -> None:
    result = compute_schedule_fsi_from_business_income(
        Decimal("-1"),
        source_country_iso2="US",
        event_count=1,
    )

    assert result.rows == []


def test_single_us_dividend_aggregates_one_row() -> None:
    line = _foreign_div_line(
        symbol="XQAW",
        isin="US99990E1038",
        issuer_country_iso2="US",
        payment_date=date(2025, 5, 5),
        amount_usd=Decimal("100.00"),
        rate=Decimal("84.50"),
    )
    os_result = ScheduleOsComputeResult(lines=[line], domestic_lines=[])

    result = compute_schedule_fsi_from_dividends(os_result)

    assert len(result.rows) == 1
    row = result.rows[0]
    assert row.cbdt_country_code == "2"  # US per ITR-2 vendor table
    assert row.country_name_portal == "UNITED STATES OF AMERICA"
    assert row.iso_alpha2 == "US"
    assert row.head_of_income is HeadOfIncome.OTHER_SOURCES
    assert row.income_inr == Decimal("8450.00")
    assert row.foreign_tax_inr == Decimal("0")  # WHT is layered on at merge, not here
    assert row.event_count == 1


def test_multiple_us_dividends_sum_into_one_row() -> None:
    """Two dividends from the same source country must aggregate
    into ONE row (not two) — the at-most-one-row-per-(country,
    head) invariant is enforced at the ScheduleFsiComputeResult
    level too."""
    line1 = _foreign_div_line(
        symbol="XQAW",
        isin="US99990E1038",
        issuer_country_iso2="US",
        payment_date=date(2025, 5, 5),
        amount_usd=Decimal("100.00"),
        rate=Decimal("84.50"),
    )
    line2 = _foreign_div_line(
        symbol="XQAK",
        isin="US99990H1068",
        issuer_country_iso2="US",
        payment_date=date(2025, 8, 15),
        amount_usd=Decimal("50.00"),
        rate=Decimal("85.00"),
    )
    os_result = ScheduleOsComputeResult(lines=[line1, line2], domestic_lines=[])

    result = compute_schedule_fsi_from_dividends(os_result)

    assert len(result.rows) == 1
    row = result.rows[0]
    # Income: 100*84.50 + 50*85.00 = 8450 + 4250 = 12700
    assert row.income_inr == Decimal("12700.00")
    assert row.event_count == 2


# ---- compute_schedule_fsi_from_dividends — multi-country -----------------


def test_multi_country_dividends_produce_one_row_per_country() -> None:
    """Three dividends from three distinct source countries must
    produce three FSI rows, sorted by (cbdt_country_code,
    head_of_income.value) ascending."""
    us_line = _foreign_div_line(
        symbol="XQAW",
        isin="US99990E1038",
        issuer_country_iso2="US",
        payment_date=date(2025, 5, 5),
        amount_usd=Decimal("100.00"),
        rate=Decimal("84.50"),
    )
    gb_line = _foreign_div_line(
        symbol="XQCQ",
        isin="GB99970N1077",
        issuer_country_iso2="GB",
        payment_date=date(2025, 6, 10),
        amount_usd=Decimal("75.00"),
        rate=Decimal("84.80"),
    )
    de_line = _foreign_div_line(
        symbol="XQCS",
        isin="DE99970G1028",
        issuer_country_iso2="DE",
        payment_date=date(2025, 7, 20),
        amount_usd=Decimal("200.00"),
        rate=Decimal("84.90"),
    )
    os_result = ScheduleOsComputeResult(
        lines=[gb_line, us_line, de_line],  # unsorted input
        domestic_lines=[],
    )

    result = compute_schedule_fsi_from_dividends(os_result)

    assert len(result.rows) == 3
    # CBDT codes: US=2, GB=44, DE=49. Lexicographic sort of "2", "44", "49"
    # is ["2", "44", "49"] — so US first, then GB, then DE.
    assert [r.iso_alpha2 for r in result.rows] == ["US", "GB", "DE"]
    assert [r.cbdt_country_code for r in result.rows] == ["2", "44", "49"]


def test_multi_country_sort_order_is_cbdt_code_lexicographic() -> None:
    """Pin the sort order against the actual CBDT codes vendored
    in the table. CBDT codes are STRINGS so the sort is
    lexicographic, not numeric: "1268" sorts before "2", etc."""
    us_line = _foreign_div_line(
        symbol="XQAW",
        isin="US99990E1038",
        issuer_country_iso2="US",
        payment_date=date(2025, 5, 5),
        amount_usd=Decimal("100.00"),
        rate=Decimal("84.50"),
    )
    ag_line = _foreign_div_line(  # Antigua and Barbuda — CBDT code "1268"
        symbol="XQJB",
        isin="AG99970A1053",
        issuer_country_iso2="AG",
        payment_date=date(2025, 6, 1),
        amount_usd=Decimal("10.00"),
        rate=Decimal("84.00"),
    )
    os_result = ScheduleOsComputeResult(lines=[us_line, ag_line], domestic_lines=[])

    result = compute_schedule_fsi_from_dividends(os_result)

    # CBDT codes: US=2, AG=1268. Lexicographic: "1268" < "2".
    assert [r.cbdt_country_code for r in result.rows] == ["1268", "2"]


# ---- excludes the domestic bucket ----------------------------------------


def test_indian_source_dividends_excluded_from_fsi() -> None:
    """Schedule FSI is "income from OUTSIDE India" by definition.
    Indian-source dividends (Schedule OS row 1a(i)) must NEVER
    surface to FSI even if domestic_lines is populated."""
    domestic = ScheduleOsDomesticDividendLine(
        symbol=Ticker("XQCD"),
        isin=Isin("IN99970W1077"),
        issuer_country_iso2="IN",
        payment_date=date(2025, 6, 15),
        fx_specified_date=date(2025, 5, 31),
        fx_rate_publication_date=date(2025, 5, 31),
        fx_rate_inr_per_usd=Decimal("84.30"),
        amount_gross_usd=Decimal("50.00"),
        amount_gross_inr=Decimal("4215.00"),
        description="XQCD(IN99970W1077) Cash Dividend USD 50.00 per Share (Ordinary Dividend)",
        kind=DistributionKind.CASH_DIVIDEND,
    )
    foreign = _foreign_div_line(
        symbol="XQAW",
        isin="US99990E1038",
        issuer_country_iso2="US",
        payment_date=date(2025, 5, 5),
        amount_usd=Decimal("100.00"),
        rate=Decimal("84.50"),
    )
    os_result = ScheduleOsComputeResult(
        lines=[foreign],
        domestic_lines=[domestic],
    )

    result = compute_schedule_fsi_from_dividends(os_result)

    # Only the foreign US line surfaces; domestic IN line is excluded.
    assert len(result.rows) == 1
    assert result.rows[0].iso_alpha2 == "US"


# ---- error path: unknown country -----------------------------------------


def test_unknown_iso_alpha2_raises() -> None:
    """If a dividend line carries an ISO alpha-2 not in the
    vendored CBDT table, the FSI compute fails fast — the portal
    output cell can't be filled, so silent drop would mis-state
    the FSI total."""
    # "ZZ" is the ISO 3166 reserved "user-assigned" code; the
    # vendored table won't have it. The OS layer already validates
    # alpha-2 SHAPE, so we can't go through _foreign_div_line which
    # would also reject ZZ if it were structurally invalid. ZZ is
    # structurally valid alpha-2 (2 uppercase letters) so OS accepts
    # it; only FSI's country-code lookup will reject.
    line = ScheduleOsDividendLine(
        symbol=Ticker("XX"),
        isin=Isin("ZZ0000000005"),
        issuer_country_iso2="ZZ",
        payment_date=date(2025, 5, 5),
        fx_specified_date=date(2025, 4, 30),
        fx_rate_publication_date=date(2025, 4, 30),
        fx_rate_inr_per_usd=Decimal("84.50"),
        amount_gross_usd=Decimal("100.00"),
        amount_gross_inr=Decimal("8450.00"),
        description="XX(ZZ0000000005) Cash Dividend USD 100.00 per Share",
        kind=DistributionKind.CASH_DIVIDEND,
    )
    os_result = ScheduleOsComputeResult(lines=[line], domestic_lines=[])

    with pytest.raises(CountryCodeNotFoundError):
        compute_schedule_fsi_from_dividends(os_result)


# ---- ScheduleFsiComputeResult invariants ---------------------------------


def test_compute_result_rejects_duplicate_country_head_pairs() -> None:
    """Direct construction with a duplicate (country, head) pair
    must raise — the public compute function aggregates so this
    branch is only reachable from direct constructors / tests,
    but the invariant catches aggregation bugs in future
    extensions."""
    row_a = ScheduleFsiRow(
        cbdt_country_code="2",
        country_name_portal="UNITED STATES OF AMERICA",
        iso_alpha2="US",
        head_of_income=HeadOfIncome.OTHER_SOURCES,
        income_inr=Decimal("100"),
        foreign_tax_inr=Decimal("0"),
        event_count=1,
    )
    row_b = ScheduleFsiRow(
        cbdt_country_code="2",
        country_name_portal="UNITED STATES OF AMERICA",
        iso_alpha2="US",
        head_of_income=HeadOfIncome.OTHER_SOURCES,
        income_inr=Decimal("200"),
        foreign_tax_inr=Decimal("0"),
        event_count=1,
    )
    with pytest.raises(InvariantError):
        ScheduleFsiComputeResult(rows=[row_a, row_b])


def test_compute_result_rejects_unsorted_rows() -> None:
    """Direct construction with rows out of canonical order must
    raise — the compute function sorts before constructing so
    this branch is unreachable from production, but the invariant
    catches future writer-side bugs that might reorder rows."""
    row_us = ScheduleFsiRow(
        cbdt_country_code="2",
        country_name_portal="UNITED STATES OF AMERICA",
        iso_alpha2="US",
        head_of_income=HeadOfIncome.OTHER_SOURCES,
        income_inr=Decimal("100"),
        foreign_tax_inr=Decimal("0"),
        event_count=1,
    )
    row_gb = ScheduleFsiRow(
        cbdt_country_code="44",
        country_name_portal="UNITED KINGDOM OF GREAT BRITAIN AND NORTHERN IRELAND",
        iso_alpha2="GB",
        head_of_income=HeadOfIncome.OTHER_SOURCES,
        income_inr=Decimal("100"),
        foreign_tax_inr=Decimal("0"),
        event_count=1,
    )
    # Correct order: "2" < "44" lexicographically. Reverse is invalid.
    with pytest.raises(InvariantError):
        ScheduleFsiComputeResult(rows=[row_gb, row_us])


def test_compute_result_accepts_same_country_different_heads() -> None:
    """Two rows with the SAME country but DIFFERENT heads of
    income are NOT duplicates — both must be allowed (a US
    investor has both OS dividends AND CG closes from US
    stocks)."""
    row_os = ScheduleFsiRow(
        cbdt_country_code="2",
        country_name_portal="UNITED STATES OF AMERICA",
        iso_alpha2="US",
        head_of_income=HeadOfIncome.OTHER_SOURCES,
        income_inr=Decimal("100"),
        foreign_tax_inr=Decimal("0"),
        event_count=1,
    )
    row_cg = ScheduleFsiRow(
        cbdt_country_code="2",
        country_name_portal="UNITED STATES OF AMERICA",
        iso_alpha2="US",
        head_of_income=HeadOfIncome.CAPITAL_GAINS,
        income_inr=Decimal("500"),
        foreign_tax_inr=Decimal("0"),
        event_count=1,
    )
    # Sort comparison: ("2", "Capital Gains") < ("2", "Other Sources").
    result = ScheduleFsiComputeResult(rows=[row_cg, row_os])
    assert len(result.rows) == 2
    assert result.rows[0].head_of_income is HeadOfIncome.CAPITAL_GAINS
    assert result.rows[1].head_of_income is HeadOfIncome.OTHER_SOURCES


# ---- top-level compute_schedule_fsi — dividends only ---------------------


def test_top_level_compute_dispatches_to_dividends_aggregator() -> None:
    """``compute_schedule_fsi`` with only ``os_result`` (CG kwargs
    default to ``None``) reproduces the dividend-only aggregator
    output — the CG kwargs are backwards-compatible additions."""
    line = _foreign_div_line(
        symbol="XQAW",
        isin="US99990E1038",
        issuer_country_iso2="US",
        payment_date=date(2025, 5, 5),
        amount_usd=Decimal("100.00"),
        rate=Decimal("84.50"),
    )
    os_result = ScheduleOsComputeResult(lines=[line], domestic_lines=[])

    result = compute_schedule_fsi(os_result=os_result, broker_domicile_iso2=None)

    assert len(result.rows) == 1
    assert result.rows[0].iso_alpha2 == "US"
    assert result.rows[0].income_inr == Decimal("8450.00")


# ---- shared CG fixtures --------------------------------------------------


def _stcg_line(
    *,
    symbol: str = "XQAQ",
    isin: str = "US99992A1097",
    issuer_country_iso2: str = "US",
    open_date: date = date(2025, 1, 10),
    close_date: date = date(2025, 6, 10),
    cost_basis_usd: Decimal = Decimal("100.00"),
    proceeds_usd: Decimal = Decimal("150.00"),
    rate: Decimal = Decimal("84.50"),
    quantity: Decimal = Decimal("1"),
) -> ScheduleCgStcgLine:
    """Construct a valid foreign-source STCG line for tests."""
    if close_date.month == 1:
        fx_specified_date = date(close_date.year - 1, 12, 31)
    else:
        fx_specified_date = date(close_date.year, close_date.month, 1) - timedelta(days=1)
    cost_basis_inr = cost_basis_usd * rate
    proceeds_inr = proceeds_usd * rate
    return ScheduleCgStcgLine(
        symbol=Ticker(symbol),
        isin=Isin(isin),
        issuer_country_iso2=issuer_country_iso2,
        description=f"{symbol} disposal",
        open_date=open_date,
        close_date=close_date,
        holding_period_days=(close_date - open_date).days,
        quantity=quantity,
        cost_basis_usd=cost_basis_usd,
        proceeds_usd=proceeds_usd,
        fx_specified_date=fx_specified_date,
        fx_rate_publication_date=fx_specified_date,
        fx_rate_inr_per_usd=rate,
        cost_basis_inr=cost_basis_inr,
        cost_basis_inr_source="test basis",
        proceeds_inr=proceeds_inr,
        realized_gain_inr=proceeds_inr - cost_basis_inr,
    )


def _ltcg_line(
    *,
    symbol: str = "XQCA",
    isin: str = "US99972B1017",
    issuer_country_iso2: str = "US",
    open_date: date = date(2022, 1, 10),
    close_date: date = date(2025, 6, 10),
    cost_basis_usd: Decimal = Decimal("100.00"),
    proceeds_usd: Decimal = Decimal("200.00"),
    rate: Decimal = Decimal("84.50"),
    quantity: Decimal = Decimal("1"),
) -> ScheduleCgLtcgLine:
    """Construct a valid foreign-source LTCG line (post-2024-07-23) for tests."""
    if close_date.month == 1:
        fx_specified_date = date(close_date.year - 1, 12, 31)
    else:
        fx_specified_date = date(close_date.year, close_date.month, 1) - timedelta(days=1)
    cost_basis_inr = cost_basis_usd * rate
    proceeds_inr = proceeds_usd * rate
    return ScheduleCgLtcgLine(
        symbol=Ticker(symbol),
        isin=Isin(isin),
        issuer_country_iso2=issuer_country_iso2,
        description=f"{symbol} disposal",
        open_date=open_date,
        close_date=close_date,
        holding_period_days=(close_date - open_date).days,
        quantity=quantity,
        cost_basis_usd=cost_basis_usd,
        proceeds_usd=proceeds_usd,
        fx_specified_date=fx_specified_date,
        fx_rate_publication_date=fx_specified_date,
        fx_rate_inr_per_usd=rate,
        cost_basis_inr=cost_basis_inr,
        cost_basis_inr_source="test basis",
        proceeds_inr=proceeds_inr,
        realized_gain_inr=proceeds_inr - cost_basis_inr,
    )


def _stcg_result(lines: list[ScheduleCgStcgLine]) -> ScheduleCgStcgComputeResult:
    return ScheduleCgStcgComputeResult(
        stcg_lines=lines,
        excluded_ltcg_deferred=[],
        excluded_indian_source=[],
        excluded_out_of_fy=[],
    )


def _ltcg_result(lines: list[ScheduleCgLtcgLine]) -> ScheduleCgLtcgComputeResult:
    return ScheduleCgLtcgComputeResult(
        ltcg_lines=lines,
        excluded_stcg=[],
        excluded_pre_2024_07_23=[],
        excluded_indian_source=[],
        excluded_out_of_fy=[],
    )


# ---- compute_schedule_fsi_from_capital_gains — happy path ----------------


def test_no_cg_inputs_yields_empty_result() -> None:
    """Both ``None`` defaults must yield empty rows (and no
    InvariantError on the empty result)."""
    result = compute_schedule_fsi_from_capital_gains(cg_stcg_result=None, cg_ltcg_result=None)
    assert result.rows == []


def test_only_stcg_with_single_us_gain() -> None:
    """A single US STCG line yields a single (US, CAPITAL_GAINS)
    FSI row. Income = proceeds_inr - cost_basis_inr (50 USD *
    84.50 = 4225)."""
    line = _stcg_line(
        cost_basis_usd=Decimal("100.00"),
        proceeds_usd=Decimal("150.00"),
        rate=Decimal("84.50"),
    )
    result = compute_schedule_fsi_from_capital_gains(cg_stcg_result=_stcg_result([line]))

    assert len(result.rows) == 1
    row = result.rows[0]
    assert row.iso_alpha2 == "US"
    assert row.cbdt_country_code == "2"
    assert row.head_of_income is HeadOfIncome.CAPITAL_GAINS
    assert row.income_inr == Decimal("4225.00")
    assert row.foreign_tax_inr == Decimal("0")  # WHT fragments are Other Sources only
    assert row.event_count == 1


def test_only_ltcg_with_single_us_gain() -> None:
    """A single US LTCG line yields a single (US, CAPITAL_GAINS)
    FSI row. Income = (200 - 100) USD * 84.50 = 8450."""
    line = _ltcg_line(
        cost_basis_usd=Decimal("100.00"),
        proceeds_usd=Decimal("200.00"),
        rate=Decimal("84.50"),
    )
    result = compute_schedule_fsi_from_capital_gains(cg_ltcg_result=_ltcg_result([line]))

    assert len(result.rows) == 1
    row = result.rows[0]
    assert row.iso_alpha2 == "US"
    assert row.head_of_income is HeadOfIncome.CAPITAL_GAINS
    assert row.income_inr == Decimal("8450.00")
    assert row.event_count == 1


def test_stcg_and_ltcg_same_country_aggregate_to_single_row() -> None:
    """STCG and LTCG from the same source country share the
    single CAPITAL_GAINS bucket on Schedule FSI (no per-rate
    split on FSI; that's Schedule SI). One row with summed
    income and combined event_count."""
    stcg = _stcg_line(
        cost_basis_usd=Decimal("100.00"),
        proceeds_usd=Decimal("150.00"),
        rate=Decimal("84.50"),
    )
    ltcg = _ltcg_line(
        cost_basis_usd=Decimal("100.00"),
        proceeds_usd=Decimal("200.00"),
        rate=Decimal("84.50"),
    )
    result = compute_schedule_fsi_from_capital_gains(
        cg_stcg_result=_stcg_result([stcg]),
        cg_ltcg_result=_ltcg_result([ltcg]),
    )

    assert len(result.rows) == 1
    row = result.rows[0]
    assert row.iso_alpha2 == "US"
    assert row.head_of_income is HeadOfIncome.CAPITAL_GAINS
    # 4225 + 8450 = 12675.
    assert row.income_inr == Decimal("12675.00")
    assert row.event_count == 2


def test_multiple_stcg_same_country_sum() -> None:
    """Two STCG closes for the same country aggregate to one
    row with summed gains and event_count=2."""
    line1 = _stcg_line(
        symbol="XQAQ",
        isin="US99992A1097",
        cost_basis_usd=Decimal("100.00"),
        proceeds_usd=Decimal("130.00"),
        rate=Decimal("84.50"),
        close_date=date(2025, 6, 10),
    )
    line2 = _stcg_line(
        symbol="XQCA",
        isin="US99972B1017",
        cost_basis_usd=Decimal("200.00"),
        proceeds_usd=Decimal("250.00"),
        rate=Decimal("85.00"),
        close_date=date(2025, 9, 12),
    )
    result = compute_schedule_fsi_from_capital_gains(cg_stcg_result=_stcg_result([line1, line2]))

    assert len(result.rows) == 1
    row = result.rows[0]
    # (130-100)*84.50 + (250-200)*85.00 = 2535 + 4250 = 6785.
    assert row.income_inr == Decimal("6785.00")
    assert row.event_count == 2


def test_multi_country_cg_one_row_per_country_sorted() -> None:
    """STCG from US + UK produces two rows, sorted by
    (cbdt_country_code, head_of_income.value)."""
    us = _stcg_line(
        symbol="XQAQ",
        isin="US99992A1097",
        issuer_country_iso2="US",
        cost_basis_usd=Decimal("100.00"),
        proceeds_usd=Decimal("150.00"),
        rate=Decimal("84.50"),
    )
    gb = _stcg_line(
        symbol="XQCQ",
        isin="GB99970N1077",
        issuer_country_iso2="GB",
        cost_basis_usd=Decimal("50.00"),
        proceeds_usd=Decimal("75.00"),
        rate=Decimal("84.80"),
        close_date=date(2025, 7, 15),
    )
    result = compute_schedule_fsi_from_capital_gains(cg_stcg_result=_stcg_result([gb, us]))

    assert len(result.rows) == 2
    # CBDT US=2, GB=44; lexicographic "2" < "44".
    assert [r.iso_alpha2 for r in result.rows] == ["US", "GB"]
    assert all(r.head_of_income is HeadOfIncome.CAPITAL_GAINS for r in result.rows)


# ---- CG net-loss / net-zero handling -------------------------------------


def test_country_with_only_loss_lines_produces_no_row() -> None:
    """A country whose CG lines all realised LOSSES produces NO
    FSI row — Schedule FSI reports POSITIVE foreign income;
    losses are tracked upstream on Schedule CG itself (and
    available for set-off / carry-forward on Schedule CFL)."""
    loss = _stcg_line(
        cost_basis_usd=Decimal("200.00"),
        proceeds_usd=Decimal("150.00"),  # 50 USD loss
        rate=Decimal("84.50"),
    )
    result = compute_schedule_fsi_from_capital_gains(cg_stcg_result=_stcg_result([loss]))

    assert result.rows == []


def test_country_with_wins_and_losses_uses_net_when_positive() -> None:
    """A country with mixed wins and losses produces ONE row
    with the NET positive income (FSI reports the country-
    level aggregate, not per-event)."""
    win = _stcg_line(
        symbol="XQAQ",
        isin="US99992A1097",
        cost_basis_usd=Decimal("100.00"),
        proceeds_usd=Decimal("200.00"),  # +100 USD
        rate=Decimal("84.50"),
        close_date=date(2025, 6, 10),
    )
    loss = _stcg_line(
        symbol="XQCA",
        isin="US99972B1017",
        cost_basis_usd=Decimal("200.00"),
        proceeds_usd=Decimal("160.00"),  # -40 USD
        rate=Decimal("84.50"),
        close_date=date(2025, 8, 5),
    )
    result = compute_schedule_fsi_from_capital_gains(cg_stcg_result=_stcg_result([win, loss]))

    assert len(result.rows) == 1
    # Net: (200-100)*84.50 + (160-200)*84.50 = 8450 - 3380 = 5070.
    assert result.rows[0].income_inr == Decimal("5070.00")
    assert result.rows[0].event_count == 2  # both events counted even though one was a loss


def test_country_with_exactly_offsetting_wins_and_losses_produces_no_row() -> None:
    """Net-zero must be excluded — the ``income_inr > 0``
    invariant would reject a zero-income row, and a country
    with zero net foreign income has nothing to report on FSI."""
    win = _stcg_line(
        symbol="XQAQ",
        isin="US99992A1097",
        cost_basis_usd=Decimal("100.00"),
        proceeds_usd=Decimal("150.00"),
        rate=Decimal("84.50"),
        close_date=date(2025, 6, 10),
    )
    loss = _stcg_line(
        symbol="XQCA",
        isin="US99972B1017",
        cost_basis_usd=Decimal("150.00"),
        proceeds_usd=Decimal("100.00"),
        rate=Decimal("84.50"),
        close_date=date(2025, 8, 5),
    )
    result = compute_schedule_fsi_from_capital_gains(cg_stcg_result=_stcg_result([win, loss]))

    assert result.rows == []


def test_country_with_stcg_loss_offset_by_ltcg_win_produces_single_net_row() -> None:
    """A country with a STCG LOSS and a larger LTCG GAIN
    produces one row at the net positive — Section 70(2) allows
    STCL to be set off against LTCG (the legal direction of
    set-off), so STCG and LTCG combine into a single
    CAPITAL_GAINS bucket on FSI."""
    stcg_loss = _stcg_line(
        cost_basis_usd=Decimal("100.00"),
        proceeds_usd=Decimal("80.00"),
        rate=Decimal("84.50"),
    )
    ltcg_win = _ltcg_line(
        cost_basis_usd=Decimal("100.00"),
        proceeds_usd=Decimal("200.00"),
        rate=Decimal("84.50"),
    )
    result = compute_schedule_fsi_from_capital_gains(
        cg_stcg_result=_stcg_result([stcg_loss]),
        cg_ltcg_result=_ltcg_result([ltcg_win]),
    )

    assert len(result.rows) == 1
    # Section 70(2): STCL -1690 + LTCG +8450 → net +6760.
    assert result.rows[0].income_inr == Decimal("6760.00")
    assert result.rows[0].event_count == 2


# ---- Section 70(2) / 70(3) per-country set-off asymmetry -----------------


def test_country_with_ltcg_loss_does_not_offset_stcg_gain() -> None:
    """**Section 70(3) regression** — long-term capital LOSS
    cannot be set off against short-term capital GAIN. With
    STCG +500 and LTCL -1000 for the same country, the STCG
    +500 remains taxable as foreign income and the LTCG loss
    is CARRIED FORWARD via Section 74 (Schedule CFL), NOT
    netted against STCG on this year's FSI.

    Pins fix for PR #209 T3 r1 / T2 r1 P2 finding (naive sum
    would silently drop the country with fsi_income = -500)."""
    stcg_win = _stcg_line(
        symbol="XQAQ",
        isin="US99992A1097",
        cost_basis_usd=Decimal("100.00"),
        proceeds_usd=Decimal("105.00"),  # +5 USD STCG
        rate=Decimal("100.00"),  # +500 INR
        close_date=date(2025, 6, 10),
    )
    ltcg_loss = _ltcg_line(
        symbol="XQCA",
        isin="US99972B1017",
        cost_basis_usd=Decimal("100.00"),
        proceeds_usd=Decimal("90.00"),  # -10 USD LTCG loss
        rate=Decimal("100.00"),  # -1000 INR
    )
    result = compute_schedule_fsi_from_capital_gains(
        cg_stcg_result=_stcg_result([stcg_win]),
        cg_ltcg_result=_ltcg_result([ltcg_loss]),
    )

    assert len(result.rows) == 1
    row = result.rows[0]
    assert row.iso_alpha2 == "US"
    assert row.head_of_income == HeadOfIncome.CAPITAL_GAINS
    # Section 70(3): LTCL does NOT offset STCG. Only the STCG
    # +500 contributes to fsi_income; the LTCL is excluded
    # (carried forward, not on this year's FSI).
    assert row.income_inr == Decimal("500.00")
    # Only the STCG event is counted; the LTCG loss event is
    # carried forward and represented on Schedule CG/CFL, not
    # on FSI for this year.
    assert row.event_count == 1


def test_country_with_only_ltcg_loss_and_no_stcg_produces_no_row() -> None:
    """A country with only LTCG losses (no STCG) produces NO
    FSI row — there is no positive foreign income to report;
    the LTCL is carried forward via Section 74."""
    ltcg_loss = _ltcg_line(
        cost_basis_usd=Decimal("200.00"),
        proceeds_usd=Decimal("150.00"),
        rate=Decimal("84.50"),
    )
    result = compute_schedule_fsi_from_capital_gains(
        cg_ltcg_result=_ltcg_result([ltcg_loss]),
    )

    assert result.rows == []


def test_country_with_both_stcg_loss_and_ltcg_loss_produces_no_row() -> None:
    """A country with losses in BOTH STCG and LTCG produces
    NO FSI row — neither loss can offset the other (STCL has
    nothing positive in this country to offset, LTCL is
    barred from offsetting STCL anyway), and each is carried
    forward separately."""
    stcg_loss = _stcg_line(
        symbol="XQAQ",
        isin="US99992A1097",
        cost_basis_usd=Decimal("100.00"),
        proceeds_usd=Decimal("80.00"),
        rate=Decimal("84.50"),
    )
    ltcg_loss = _ltcg_line(
        symbol="XQCA",
        isin="US99972B1017",
        cost_basis_usd=Decimal("100.00"),
        proceeds_usd=Decimal("70.00"),
        rate=Decimal("84.50"),
    )
    result = compute_schedule_fsi_from_capital_gains(
        cg_stcg_result=_stcg_result([stcg_loss]),
        cg_ltcg_result=_ltcg_result([ltcg_loss]),
    )

    assert result.rows == []


def test_country_with_ltcg_loss_smaller_than_stcg_gain_keeps_full_stcg() -> None:
    """**Section 70(3) edge** — LTCL of -300 + STCG +1000:
    the LTCL still cannot offset the STCG. fsi_income equals
    the full STCG (1000), NOT the naive sum (700). The LTCL
    is carried forward."""
    stcg_win = _stcg_line(
        symbol="XQAQ",
        isin="US99992A1097",
        cost_basis_usd=Decimal("100.00"),
        proceeds_usd=Decimal("110.00"),  # +10 USD
        rate=Decimal("100.00"),  # +1000 INR
        close_date=date(2025, 6, 10),
    )
    ltcg_loss = _ltcg_line(
        symbol="XQCA",
        isin="US99972B1017",
        cost_basis_usd=Decimal("100.00"),
        proceeds_usd=Decimal("97.00"),  # -3 USD
        rate=Decimal("100.00"),  # -300 INR
    )
    result = compute_schedule_fsi_from_capital_gains(
        cg_stcg_result=_stcg_result([stcg_win]),
        cg_ltcg_result=_ltcg_result([ltcg_loss]),
    )

    assert len(result.rows) == 1
    # Section 70(3): full STCG remains; LTCL carried forward.
    assert result.rows[0].income_inr == Decimal("1000.00")
    assert result.rows[0].event_count == 1


def test_country_with_stcg_loss_offsets_ltcg_partial_positive_remainder() -> None:
    """**Section 70(2)** — STCL of -200 + LTCG +500: the
    STCL is allowed to offset LTCG (legal direction). Net
    fsi_income = 300; both events contribute to the row."""
    stcg_loss = _stcg_line(
        symbol="XQAQ",
        isin="US99992A1097",
        cost_basis_usd=Decimal("100.00"),
        proceeds_usd=Decimal("98.00"),  # -2 USD STCG
        rate=Decimal("100.00"),  # -200 INR
        close_date=date(2025, 6, 10),
    )
    ltcg_win = _ltcg_line(
        symbol="XQCA",
        isin="US99972B1017",
        cost_basis_usd=Decimal("100.00"),
        proceeds_usd=Decimal("105.00"),  # +5 USD LTCG
        rate=Decimal("100.00"),  # +500 INR
    )
    result = compute_schedule_fsi_from_capital_gains(
        cg_stcg_result=_stcg_result([stcg_loss]),
        cg_ltcg_result=_ltcg_result([ltcg_win]),
    )

    assert len(result.rows) == 1
    # Section 70(2): -200 + 500 = +300.
    assert result.rows[0].income_inr == Decimal("300.00")
    assert result.rows[0].event_count == 2


def test_country_with_stcg_loss_larger_than_ltcg_gain_produces_no_row() -> None:
    """**Section 70(2) edge** — STCL of -500 + LTCG +200:
    STCL is allowed to offset LTCG. Net = -300 ≤ 0, so no
    FSI row (the residual STCL after offsetting LTCG would
    be carried forward via Section 74)."""
    stcg_loss = _stcg_line(
        symbol="XQAQ",
        isin="US99992A1097",
        cost_basis_usd=Decimal("100.00"),
        proceeds_usd=Decimal("95.00"),  # -5 USD
        rate=Decimal("100.00"),  # -500 INR
        close_date=date(2025, 6, 10),
    )
    ltcg_win = _ltcg_line(
        symbol="XQCA",
        isin="US99972B1017",
        cost_basis_usd=Decimal("100.00"),
        proceeds_usd=Decimal("102.00"),  # +2 USD
        rate=Decimal("100.00"),  # +200 INR
    )
    result = compute_schedule_fsi_from_capital_gains(
        cg_stcg_result=_stcg_result([stcg_loss]),
        cg_ltcg_result=_ltcg_result([ltcg_win]),
    )

    assert result.rows == []


def test_us_stcl_does_not_offset_uk_ltcg_across_countries() -> None:
    """**T3 r2 P2 follow-up** — pins the CBDT-canonical
    per-country FTC contract: cross-country Section 70(2)
    set-off is NOT applied on Schedule FSI. With US STCL
    -500 and UK LTCG +800, Schedule FSI must report UK
    Capital Gains at GROSS +800 (not the aggregate +300
    that Section 70 would yield at FY level on Schedule
    CG); the US STCL is dropped (carried forward via Sec
    74). Authority: Rule 128(5)(i) (per-country/per-source
    FTC cap) + Rule 128(8)(i) (Form 67 per-country
    statement requirement) + CBDT Notification 9/2017
    (Form 67 filing procedure) + ITR-2 Schedule FSI
    instructions.

    A T3 r2 reviewer questioned this — citing the FY-
    aggregate scope of Section 70 — but applying
    cross-country set-off on FSI would understate the
    per-country FTC cap (since per-Rule-128(5)(i) the FTC
    is computed on per-country foreign income, and
    "losses from one country cannot reduce FTC-eligible
    income of another"). This test pins the CBDT-correct
    behavior."""
    us_stcl = _stcg_line(
        symbol="XQAQ",
        isin="US99992A1097",
        issuer_country_iso2="US",
        cost_basis_usd=Decimal("100.00"),
        proceeds_usd=Decimal("95.00"),  # -5 USD
        rate=Decimal("100.00"),  # -500 INR
        close_date=date(2025, 6, 10),
    )
    uk_ltcg = _ltcg_line(
        symbol="XQCQ",
        isin="GB99970N1077",
        issuer_country_iso2="GB",
        cost_basis_usd=Decimal("100.00"),
        proceeds_usd=Decimal("108.00"),  # +8 USD
        rate=Decimal("100.00"),  # +800 INR
    )
    result = compute_schedule_fsi_from_capital_gains(
        cg_stcg_result=_stcg_result([us_stcl]),
        cg_ltcg_result=_ltcg_result([uk_ltcg]),
    )

    # Only the UK row appears, at GROSS +800 (NOT +300 net
    # of the US STCL — cross-country set-off is barred per
    # CBDT FTC guidance).
    assert len(result.rows) == 1
    row = result.rows[0]
    assert row.iso_alpha2 == "GB"
    assert row.head_of_income == HeadOfIncome.CAPITAL_GAINS
    assert row.income_inr == Decimal("800.00")  # GROSS, not +300 net
    assert row.event_count == 1  # only the UK LTCG event


def test_uk_ltcg_loss_does_not_offset_us_stcg_across_countries() -> None:
    """Symmetric cross-country case: UK LTCL is barred
    from offsetting US STCG even at the within-country
    level (Section 70(3)) AND at the cross-country level
    (CBDT FTC contract). US row reports STCG +500 gross;
    UK row dropped (LTCL carried forward via Sec 74)."""
    uk_ltcl = _ltcg_line(
        symbol="XQCQ",
        isin="GB99970N1077",
        issuer_country_iso2="GB",
        cost_basis_usd=Decimal("100.00"),
        proceeds_usd=Decimal("92.00"),  # -8 USD
        rate=Decimal("100.00"),  # -800 INR
    )
    us_stcg = _stcg_line(
        symbol="XQAQ",
        isin="US99992A1097",
        issuer_country_iso2="US",
        cost_basis_usd=Decimal("100.00"),
        proceeds_usd=Decimal("105.00"),  # +5 USD
        rate=Decimal("100.00"),  # +500 INR
        close_date=date(2025, 6, 10),
    )
    result = compute_schedule_fsi_from_capital_gains(
        cg_stcg_result=_stcg_result([us_stcg]),
        cg_ltcg_result=_ltcg_result([uk_ltcl]),
    )

    assert len(result.rows) == 1
    row = result.rows[0]
    assert row.iso_alpha2 == "US"
    assert row.income_inr == Decimal("500.00")  # full US STCG
    assert row.event_count == 1


def test_us_stcl_does_not_offset_gb_stcg_across_countries() -> None:
    """**T5 r2 P1 follow-up** — pins the CBDT-canonical
    per-country FTC contract for the same-head cross-
    country case. US STCG -1000 + GB STCG +1000: at FY-
    aggregate level Section 70 would net these to zero
    (same source, same head), but Schedule FSI is per
    country with no cross-country set-off — GB reports
    GROSS +1000, US is dropped (STCL carried forward via
    Section 74). The FY-aggregate Section 70 set-off is
    applied on Schedule CG and Schedule CFL, not on
    Schedule FSI. Authority: Rule 128(5)(i) (per-country/
    per-source FTC cap) + Rule 128(8)(i) (Form 67
    per-country statement requirement) + CBDT
    Notification 9/2017 (Form 67 filing procedure) +
    ITR-2 Schedule FSI instructions."""
    us_stcl = _stcg_line(
        symbol="XQAQ",
        isin="US99992A1097",
        issuer_country_iso2="US",
        cost_basis_usd=Decimal("200.00"),
        proceeds_usd=Decimal("100.00"),  # -100 USD
        rate=Decimal("10.00"),  # -1000 INR
        close_date=date(2025, 6, 10),
    )
    gb_stcg = _stcg_line(
        symbol="XQCQ",
        isin="GB99970N1077",
        issuer_country_iso2="GB",
        cost_basis_usd=Decimal("100.00"),
        proceeds_usd=Decimal("200.00"),  # +100 USD
        rate=Decimal("10.00"),  # +1000 INR
        close_date=date(2025, 7, 15),
    )
    result = compute_schedule_fsi_from_capital_gains(
        cg_stcg_result=_stcg_result([us_stcl, gb_stcg]),
    )

    # Only GB row; US dropped. GB at GROSS +1000 (NOT 0
    # net of cross-country set-off).
    assert len(result.rows) == 1
    row = result.rows[0]
    assert row.iso_alpha2 == "GB"
    assert row.income_inr == Decimal("1000.00")
    assert row.event_count == 1


def test_us_ltcl_does_not_offset_gb_ltcg_across_countries() -> None:
    """**T4 r3 low-pri** — completes the LTCL-vs-LTCG
    cross-country coverage matrix. US LTCL -1000 + GB
    LTCG +1000: at FY-aggregate level Section 70(3) would
    allow these to offset (same head, both long-term);
    Schedule FSI per the CBDT FTC contract reports GB at
    GROSS +1000 and drops US (LTCL carried forward via
    Sec 74). Authority: Rule 128(5)(i) + Rule 128(8)(i)
    + CBDT Notification 9/2017 + ITR-2 Schedule FSI
    instructions."""
    us_ltcl = _ltcg_line(
        symbol="XQAQ",
        isin="US99992A1097",
        issuer_country_iso2="US",
        cost_basis_usd=Decimal("200.00"),
        proceeds_usd=Decimal("100.00"),  # -100 USD
        rate=Decimal("10.00"),  # -1000 INR
    )
    gb_ltcg = _ltcg_line(
        symbol="XQCQ",
        isin="GB99970N1077",
        issuer_country_iso2="GB",
        cost_basis_usd=Decimal("100.00"),
        proceeds_usd=Decimal("200.00"),  # +100 USD
        rate=Decimal("10.00"),  # +1000 INR
    )
    result = compute_schedule_fsi_from_capital_gains(
        cg_ltcg_result=_ltcg_result([us_ltcl, gb_ltcg]),
    )

    assert len(result.rows) == 1
    row = result.rows[0]
    assert row.iso_alpha2 == "GB"
    assert row.income_inr == Decimal("1000.00")
    assert row.event_count == 1


def test_country_with_offsetting_stcg_wins_losses_and_positive_ltcg_uses_ltcg_only() -> None:
    """**T4 r2 Nit-2** — when STCG wins and STCG losses
    exactly cancel (stcg_net = 0) for a country while
    LTCG is positive, branch 1 fires and the FSI row
    income equals the LTCG amount. All three events
    contribute to event_count (each one is part of the
    audit reconciliation back to Schedule CG)."""
    stcg_win = _stcg_line(
        symbol="XQAQ",
        isin="US99992A1097",
        cost_basis_usd=Decimal("100.00"),
        proceeds_usd=Decimal("105.00"),  # +5 USD
        rate=Decimal("100.00"),  # +500 INR
        close_date=date(2025, 6, 10),
    )
    stcg_loss = _stcg_line(
        symbol="XQCA",
        isin="US99972B1017",
        cost_basis_usd=Decimal("100.00"),
        proceeds_usd=Decimal("95.00"),  # -5 USD
        rate=Decimal("100.00"),  # -500 INR
        close_date=date(2025, 8, 5),
    )
    ltcg_win = _ltcg_line(
        cost_basis_usd=Decimal("100.00"),
        proceeds_usd=Decimal("103.00"),  # +3 USD
        rate=Decimal("100.00"),  # +300 INR
    )
    result = compute_schedule_fsi_from_capital_gains(
        cg_stcg_result=_stcg_result([stcg_win, stcg_loss]),
        cg_ltcg_result=_ltcg_result([ltcg_win]),
    )

    assert len(result.rows) == 1
    row = result.rows[0]
    assert row.iso_alpha2 == "US"
    # stcg_net = 0, ltcg_net = +300, branch 1 → fsi_income = 300.
    assert row.income_inr == Decimal("300.00")
    # All three events count (audit reconciliation).
    assert row.event_count == 3


def test_one_country_loss_another_country_gain_drops_only_loss_country() -> None:
    """Per-country net evaluation: US net loss + UK net gain
    → only UK row appears."""
    us_loss = _stcg_line(
        symbol="XQAQ",
        isin="US99992A1097",
        issuer_country_iso2="US",
        cost_basis_usd=Decimal("200.00"),
        proceeds_usd=Decimal("100.00"),
        rate=Decimal("84.50"),
    )
    gb_win = _stcg_line(
        symbol="XQCQ",
        isin="GB99970N1077",
        issuer_country_iso2="GB",
        cost_basis_usd=Decimal("50.00"),
        proceeds_usd=Decimal("100.00"),
        rate=Decimal("84.80"),
        close_date=date(2025, 7, 15),
    )
    result = compute_schedule_fsi_from_capital_gains(cg_stcg_result=_stcg_result([us_loss, gb_win]))

    assert len(result.rows) == 1
    assert result.rows[0].iso_alpha2 == "GB"


# ---- CG error path: unknown country --------------------------------------


def test_cg_unknown_iso_alpha2_raises() -> None:
    """A CG line carrying an ISO alpha-2 not in the vendored
    table raises CountryCodeNotFoundError — same fail-fast
    contract as the dividend aggregator."""
    line = _stcg_line(issuer_country_iso2="ZZ", isin="ZZ0000000005")
    with pytest.raises(CountryCodeNotFoundError):
        compute_schedule_fsi_from_capital_gains(cg_stcg_result=_stcg_result([line]))


def test_cg_unknown_iso_alpha2_raises_even_for_net_loss_country() -> None:
    """**T4 r1 F1 regression** — fail-fast country validation
    applies BEFORE the net-loss filter. A loss-only country
    with an unmapped ISO must raise CountryCodeNotFoundError
    at ingestion, not silently pass. Without this guard, the
    same data later turning net-positive would crash with
    no upstream signal."""
    loss_line = _stcg_line(
        issuer_country_iso2="ZZ",
        isin="ZZ0000000005",
        cost_basis_usd=Decimal("200.00"),
        proceeds_usd=Decimal("100.00"),  # -100 USD loss
        rate=Decimal("84.50"),
    )
    with pytest.raises(CountryCodeNotFoundError):
        compute_schedule_fsi_from_capital_gains(cg_stcg_result=_stcg_result([loss_line]))


def test_cg_unknown_iso_alpha2_in_ltcg_raises_even_for_net_loss_country() -> None:
    """Same fail-fast contract for the LTCG side: a loss-only
    LTCG line with an unmapped ISO must surface immediately
    rather than wait until the next year's data turns it
    positive."""
    loss_line = _ltcg_line(
        issuer_country_iso2="ZZ",
        isin="ZZ0000000005",
        cost_basis_usd=Decimal("200.00"),
        proceeds_usd=Decimal("100.00"),
        rate=Decimal("84.50"),
    )
    with pytest.raises(CountryCodeNotFoundError):
        compute_schedule_fsi_from_capital_gains(cg_ltcg_result=_ltcg_result([loss_line]))


# ---- top-level compute_schedule_fsi merging ------------------------------


def test_dispatcher_merges_dividends_and_cg_for_same_country_different_heads() -> None:
    """A US investor with both foreign dividends and foreign CG
    produces TWO rows for US (one OTHER_SOURCES + one
    CAPITAL_GAINS), sorted alphabetically by head value
    ('Capital Gains' < 'Other Sources')."""
    div = _foreign_div_line(
        symbol="XQAW",
        isin="US99990E1038",
        issuer_country_iso2="US",
        payment_date=date(2025, 5, 5),
        amount_usd=Decimal("100.00"),
        rate=Decimal("84.50"),
    )
    cg = _stcg_line(
        cost_basis_usd=Decimal("100.00"),
        proceeds_usd=Decimal("150.00"),
        rate=Decimal("84.50"),
    )
    os_result = ScheduleOsComputeResult(lines=[div], domestic_lines=[])

    result = compute_schedule_fsi(
        os_result=os_result, cg_stcg_result=_stcg_result([cg]), broker_domicile_iso2=None
    )

    assert len(result.rows) == 2
    # Same CBDT code "2", sort tiebreaker on head.value:
    # "Capital Gains" < "Other Sources" alphabetically.
    assert [r.head_of_income for r in result.rows] == [
        HeadOfIncome.CAPITAL_GAINS,
        HeadOfIncome.OTHER_SOURCES,
    ]
    assert result.rows[0].income_inr == Decimal("4225.00")  # CG
    assert result.rows[1].income_inr == Decimal("8450.00")  # Dividend


def test_dispatcher_only_cg_no_dividends() -> None:
    """Dispatcher with empty OS + populated CG yields only CG
    rows — no dividend row materialises from empty OS."""
    cg = _stcg_line(
        cost_basis_usd=Decimal("100.00"),
        proceeds_usd=Decimal("150.00"),
        rate=Decimal("84.50"),
    )
    result = compute_schedule_fsi(
        os_result=ScheduleOsComputeResult(lines=[], domestic_lines=[]),
        cg_stcg_result=_stcg_result([cg]),
        broker_domicile_iso2=None,
    )

    assert len(result.rows) == 1
    assert result.rows[0].head_of_income is HeadOfIncome.CAPITAL_GAINS


def test_dispatcher_only_dividends_no_cg() -> None:
    """Dispatcher with populated OS + None CG kwargs reproduces
    the dividend-only output (backwards compat with PR #208)."""
    div = _foreign_div_line(
        symbol="XQAW",
        isin="US99990E1038",
        issuer_country_iso2="US",
        payment_date=date(2025, 5, 5),
        amount_usd=Decimal("100.00"),
        rate=Decimal("84.50"),
    )
    result = compute_schedule_fsi(
        os_result=ScheduleOsComputeResult(lines=[div], domestic_lines=[]),
        broker_domicile_iso2=None,
    )

    assert len(result.rows) == 1
    assert result.rows[0].head_of_income is HeadOfIncome.OTHER_SOURCES


def test_dispatcher_cg_loss_country_does_not_create_phantom_row() -> None:
    """A country with positive dividends but net CG LOSS produces
    only the dividend row — the loss country has no CAPITAL_GAINS
    row to merge with the OTHER_SOURCES row."""
    div = _foreign_div_line(
        symbol="XQAW",
        isin="US99990E1038",
        issuer_country_iso2="US",
        payment_date=date(2025, 5, 5),
        amount_usd=Decimal("100.00"),
        rate=Decimal("84.50"),
    )
    cg_loss = _stcg_line(
        cost_basis_usd=Decimal("200.00"),
        proceeds_usd=Decimal("100.00"),
        rate=Decimal("84.50"),
    )
    result = compute_schedule_fsi(
        os_result=ScheduleOsComputeResult(lines=[div], domestic_lines=[]),
        cg_stcg_result=_stcg_result([cg_loss]),
        broker_domicile_iso2=None,
    )

    assert len(result.rows) == 1
    assert result.rows[0].head_of_income is HeadOfIncome.OTHER_SOURCES
    assert result.rows[0].income_inr == Decimal("8450.00")


# ---- merge contract — exercised via the dispatcher -----------------------


def test_merge_sums_same_country_same_head_across_aggregators() -> None:
    """When two aggregators emit overlapping (country, head)
    keys, the dispatcher MUST sum income_inr, foreign_tax_inr,
    and event_count (per the documented merge contract). This
    test exercises the contract directly via the private
    ``_merge_results`` helper, with hand-built rows that carry
    foreign tax."""

    row_a = ScheduleFsiRow(
        cbdt_country_code="2",
        country_name_portal="UNITED STATES OF AMERICA",
        iso_alpha2="US",
        head_of_income=HeadOfIncome.OTHER_SOURCES,
        income_inr=Decimal("100"),
        foreign_tax_inr=Decimal("0"),
        event_count=1,
    )
    row_b = ScheduleFsiRow(
        cbdt_country_code="2",
        country_name_portal="UNITED STATES OF AMERICA",
        iso_alpha2="US",
        head_of_income=HeadOfIncome.OTHER_SOURCES,
        income_inr=Decimal("250"),
        foreign_tax_inr=Decimal("15"),
        event_count=3,
    )
    a = ScheduleFsiComputeResult(rows=[row_a])
    b = ScheduleFsiComputeResult(rows=[row_b])

    merged = _merge_results(a, b)

    assert len(merged.rows) == 1
    row = merged.rows[0]
    assert row.income_inr == Decimal("350")
    assert row.foreign_tax_inr == Decimal("15")
    assert row.event_count == 4


def test_merge_preserves_distinct_keys() -> None:
    """Aggregator-pairs with distinct (country, head) keys must
    be preserved as separate rows, sorted by the canonical
    order."""

    us_div = ScheduleFsiRow(
        cbdt_country_code="2",
        country_name_portal="UNITED STATES OF AMERICA",
        iso_alpha2="US",
        head_of_income=HeadOfIncome.OTHER_SOURCES,
        income_inr=Decimal("100"),
        foreign_tax_inr=Decimal("0"),
        event_count=1,
    )
    gb_cg = ScheduleFsiRow(
        cbdt_country_code="44",
        country_name_portal="UNITED KINGDOM OF GREAT BRITAIN AND NORTHERN IRELAND",
        iso_alpha2="GB",
        head_of_income=HeadOfIncome.CAPITAL_GAINS,
        income_inr=Decimal("500"),
        foreign_tax_inr=Decimal("0"),
        event_count=1,
    )
    merged = _merge_results(
        ScheduleFsiComputeResult(rows=[us_div]),
        ScheduleFsiComputeResult(rows=[gb_cg]),
    )

    assert len(merged.rows) == 2
    # CBDT US=2 < GB=44 lexicographic.
    assert [r.iso_alpha2 for r in merged.rows] == ["US", "GB"]


def test_merge_rejects_drifting_iso_alpha2_for_same_cbdt_code() -> None:
    """If two aggregators emit different ``iso_alpha2`` for the
    same CBDT code (aggregator bug), the merge must fail
    fast — the country-codes lookup is the single source of
    truth for the iso/CBDT mapping."""

    row_a = ScheduleFsiRow(
        cbdt_country_code="2",
        country_name_portal="UNITED STATES OF AMERICA",
        iso_alpha2="US",
        head_of_income=HeadOfIncome.OTHER_SOURCES,
        income_inr=Decimal("100"),
        foreign_tax_inr=Decimal("0"),
        event_count=1,
    )
    row_b = ScheduleFsiRow(
        cbdt_country_code="2",
        country_name_portal="UNITED STATES OF AMERICA",
        iso_alpha2="GB",  # drift!
        head_of_income=HeadOfIncome.OTHER_SOURCES,
        income_inr=Decimal("200"),
        foreign_tax_inr=Decimal("0"),
        event_count=1,
    )
    with pytest.raises(InvariantError):
        _merge_results(
            ScheduleFsiComputeResult(rows=[row_a]),
            ScheduleFsiComputeResult(rows=[row_b]),
        )


def test_merge_rejects_drifting_country_name_for_same_cbdt_code() -> None:
    """Country-name drift must also fail fast (same
    aggregator-bug detection)."""

    row_a = ScheduleFsiRow(
        cbdt_country_code="2",
        country_name_portal="UNITED STATES OF AMERICA",
        iso_alpha2="US",
        head_of_income=HeadOfIncome.OTHER_SOURCES,
        income_inr=Decimal("100"),
        foreign_tax_inr=Decimal("0"),
        event_count=1,
    )
    row_b = ScheduleFsiRow(
        cbdt_country_code="2",
        country_name_portal="UNITED STATES",  # drift!
        iso_alpha2="US",
        head_of_income=HeadOfIncome.OTHER_SOURCES,
        income_inr=Decimal("200"),
        foreign_tax_inr=Decimal("0"),
        event_count=1,
    )
    with pytest.raises(InvariantError):
        _merge_results(
            ScheduleFsiComputeResult(rows=[row_a]),
            ScheduleFsiComputeResult(rows=[row_b]),
        )


# ---- shared interest fixtures --------------------------------------------


def _interest_line(
    *,
    credit_date: date = date(2025, 4, 30),
    amount_usd: Decimal = Decimal("1.23"),
    rate: Decimal = Decimal("85.50"),
    description: str = "USD Credit Interest for Apr-2025",
) -> ScheduleOsInterestLine:
    """Construct a valid Rule 115(1), Explanation (2)(c) interest line for tests.

    The interest aggregator only reads ``amount_gross_inr`` from the
    line; the other fields are set to plausible values so the line's
    own invariants pass.
    """
    return ScheduleOsInterestLine(
        credit_date=credit_date,
        fx_specified_date=date(2025, 3, 31),
        fx_rate_publication_date=date(2025, 3, 31),
        fx_rate_inr_per_usd=rate,
        amount_gross_usd=amount_usd,
        amount_gross_inr=amount_usd * rate,
        description=description,
    )


def _interest_result(lines: list[ScheduleOsInterestLine]) -> ScheduleOsInterestComputeResult:
    """Wrap interest lines into a compute result with consistent totals.

    ``total_inr_rounded`` quantises with ``ROUND_HALF_UP`` to match
    the production ``compute_schedule_os_interest_lines`` rounding
    mode (CBDT-FAQ HALF_UP). The FSI aggregator under test re-sums
    from per-line ``amount_gross_inr`` and ignores
    ``total_inr_rounded`` entirely, but keeping the helper's
    rounding semantics aligned with production prevents subtle
    drift if a future test ever asserts on the wrapped total.
    """
    total = sum((ln.amount_gross_inr for ln in lines), start=Decimal("0"))
    return ScheduleOsInterestComputeResult(
        lines=lines,
        total_inr_full=total,
        total_inr_rounded=total.quantize(Decimal("1"), rounding=ROUND_HALF_UP),
        excluded_out_of_fy_count=0,
    )


# ---- compute_schedule_fsi_from_interest — happy path ---------------------


def test_other_income_requires_a_reviewed_source_country() -> None:
    with pytest.raises(InvariantError, match="reviewed source country"):
        compute_schedule_fsi_from_other_income(
            ScheduleOsOtherIncomeComputeResult(
                lines=(_other_income_line("80", source_country_iso2=None),)
            )
        )


def test_dispatcher_counts_fee_and_substitute_once_in_same_country() -> None:
    fee = _other_income_line("80")
    substitute = ScheduleOsSubstituteDividendLine(
        symbol=Ticker("TEST"),
        isin=Isin("BM99970C1019"),
        issuer_country_iso2=CountryCode("BM"),
        source_country_iso2=CountryCode("US"),
        payment_date=date(2025, 6, 15),
        fx_specified_date=date(2026, 3, 31),
        fx_rate_publication_date=date(2026, 3, 31),
        fx_rate_inr_per_usd=Decimal("80"),
        amount_gross_usd=Decimal("2"),
        amount_gross_inr=Decimal("160"),
        description="Manufactured Div.",
    )
    os_result = ScheduleOsComputeResult(
        lines=[],
        domestic_lines=[],
        other_income_lines=(
            ScheduleOsOtherIncomeLine(
                nature="Substitute payment in lieu of dividend",
                payment_date=substitute.payment_date,
                fx_specified_date=substitute.fx_specified_date,
                fx_rate_publication_date=substitute.fx_rate_publication_date,
                fx_rate_inr_per_usd=substitute.fx_rate_inr_per_usd,
                amount_gross_usd=substitute.amount_gross_usd,
                amount_gross_inr=substitute.amount_gross_inr,
                description=substitute.description,
                source_row_index=None,
                source_country_iso2=substitute.source_country_iso2,
            ),
            fee,
        ),
    )

    result = compute_schedule_fsi(
        os_result=os_result,
        substitute_dividend_result=ScheduleOsSubstituteDividendComputeResult(
            lines=(substitute,),
        ),
        other_income_result=ScheduleOsOtherIncomeComputeResult(lines=(fee,)),
        broker_domicile_iso2=None,
    )

    [row] = result.rows
    assert row.iso_alpha2 == "US"
    assert row.income_inr == Decimal("240")
    assert row.event_count == 2


def test_substitute_dividend_result_has_no_unread_totals() -> None:
    """Schedule OS 1e totals its lines; the result carries no totals of its own (#1137)."""
    assert {field.name for field in fields(ScheduleOsSubstituteDividendComputeResult)} == {
        "lines",
        "excluded_out_of_fy_count",
    }


def test_interest_none_yields_empty_result() -> None:
    """``None`` input means no interest pipeline has been run; it must
    yield an empty result, not raise."""
    result = compute_schedule_fsi_from_interest(None, broker_domicile_iso2=None)
    assert result.rows == []


def test_interest_empty_lines_yield_empty_result() -> None:
    """A compute result with zero lines (e.g. all credits filtered
    out as out-of-FY) yields no FSI row — same as the None case."""
    empty = ScheduleOsInterestComputeResult(
        lines=[],
        total_inr_full=Decimal("0"),
        total_inr_rounded=Decimal("0"),
        excluded_out_of_fy_count=5,
    )
    result = compute_schedule_fsi_from_interest(empty, broker_domicile_iso2="US")
    assert result.rows == []


def test_interest_single_line_us_broker_domicile() -> None:
    """A single positive credit yields one (US, OTHER_SOURCES) row
    whose income equals the credit's INR amount."""
    ln = _interest_line(amount_usd=Decimal("10.00"), rate=Decimal("84.00"))
    result = compute_schedule_fsi_from_interest(_interest_result([ln]), broker_domicile_iso2="US")

    assert len(result.rows) == 1
    row = result.rows[0]
    assert row.iso_alpha2 == "US"
    assert row.cbdt_country_code == "2"  # CBDT code for USA
    assert row.head_of_income is HeadOfIncome.OTHER_SOURCES
    assert row.income_inr == Decimal("840.00")
    assert row.foreign_tax_inr == Decimal("0")
    assert row.event_count == 1


def test_interest_multiple_lines_sum_into_single_row() -> None:
    """All in-FY credits collapse into ONE row whose income is the
    sum of per-line ``amount_gross_inr`` and whose ``event_count``
    matches the number of credits — this is the central
    broker-domicile aggregation invariant."""
    lines = [
        _interest_line(
            credit_date=date(2025, 4, 30),
            amount_usd=Decimal("10.00"),
            rate=Decimal("84.00"),
        ),
        _interest_line(
            credit_date=date(2025, 5, 31),
            amount_usd=Decimal("12.50"),
            rate=Decimal("84.00"),
            description="USD Credit Interest for May-2025",
        ),
        _interest_line(
            credit_date=date(2025, 6, 30),
            amount_usd=Decimal("8.25"),
            rate=Decimal("84.00"),
            description="USD Credit Interest for Jun-2025",
        ),
    ]
    result = compute_schedule_fsi_from_interest(_interest_result(lines), broker_domicile_iso2="US")

    assert len(result.rows) == 1
    row = result.rows[0]
    assert row.iso_alpha2 == "US"
    assert row.head_of_income is HeadOfIncome.OTHER_SOURCES
    # 10 + 12.5 + 8.25 = 30.75 * 84.00 = 2583.00
    assert row.income_inr == Decimal("2583.00")
    assert row.event_count == 3


def test_interest_override_broker_domicile_to_ireland() -> None:
    """An IBKR Ireland account holder passes ``"IE"`` and the row
    is attributed to Ireland's CBDT code, not US."""
    ln = _interest_line(amount_usd=Decimal("5.00"), rate=Decimal("90.00"))
    result = compute_schedule_fsi_from_interest(
        _interest_result([ln]),
        broker_domicile_iso2="IE",
    )

    assert len(result.rows) == 1
    row = result.rows[0]
    assert row.iso_alpha2 == "IE"
    # Ireland's CBDT code is non-empty and distinct from US's "2".
    assert row.cbdt_country_code and row.cbdt_country_code != "2"
    assert row.head_of_income is HeadOfIncome.OTHER_SOURCES
    assert row.income_inr == Decimal("450.00")
    assert row.event_count == 1


def test_interest_broker_domicile_lowercase_is_normalised_to_uppercase() -> None:
    """The CLI / config layer may pass ``"us"`` or ``"ie"`` from
    user-supplied input. The aggregator normalises to uppercase
    before the country lookup so a case mismatch never produces a
    spurious :class:`CountryCodeNotFoundError`. The emitted row's
    ``iso_alpha2`` is also the normalised form so it merges
    cleanly with the dividend aggregator (which emits uppercase
    via the typed ``ScheduleOsDividendLine``)."""
    ln = _interest_line(amount_usd=Decimal("10.00"), rate=Decimal("84.00"))

    lower_result = compute_schedule_fsi_from_interest(
        _interest_result([ln]),
        broker_domicile_iso2="us",
    )
    mixed_result = compute_schedule_fsi_from_interest(
        _interest_result([ln]),
        broker_domicile_iso2="Us",
    )
    upper_result = compute_schedule_fsi_from_interest(
        _interest_result([ln]),
        broker_domicile_iso2="US",
    )

    assert lower_result == mixed_result == upper_result
    assert lower_result.rows[0].iso_alpha2 == "US"


# ---- compute_schedule_fsi_from_interest — error path ---------------------


def test_interest_unknown_broker_domicile_raises_even_with_credits() -> None:
    """A non-mapped broker domicile must surface immediately via
    :class:`CountryCodeNotFoundError` (fail-fast), not silently
    accept the credits and produce a misattributed row."""
    ln = _interest_line(amount_usd=Decimal("5.00"), rate=Decimal("90.00"))
    with pytest.raises(CountryCodeNotFoundError):
        compute_schedule_fsi_from_interest(
            _interest_result([ln]),
            broker_domicile_iso2="ZZ",
        )


_EMPTY_INTEREST = ScheduleOsInterestComputeResult(
    lines=[],
    total_inr_full=Decimal("0"),
    total_inr_rounded=Decimal("0"),
    excluded_out_of_fy_count=0,
)


def _interest_fsi(
    function: str, interest: ScheduleOsInterestComputeResult | None, iso: object
) -> ScheduleFsiComputeResult:
    """Call ``function`` with ``interest`` and the payer country ``iso``."""
    if function == "from_interest":
        return compute_schedule_fsi_from_interest(
            interest,
            broker_domicile_iso2=iso,  # ty: ignore[invalid-argument-type]
        )
    return compute_schedule_fsi(
        os_result=_empty_os_result(),
        interest_result=interest,
        broker_domicile_iso2=iso,  # ty: ignore[invalid-argument-type]
    )


_LOWER_FUNCTIONS = pytest.mark.parametrize("function", ["from_interest", "dispatcher"])
_NO_INTEREST = pytest.mark.parametrize("interest", [None, _EMPTY_INTEREST], ids=["absent", "empty"])


@_LOWER_FUNCTIONS
@_NO_INTEREST
def test_interest_none_payer_without_interest_yields_no_row(
    function: str, interest: ScheduleOsInterestComputeResult | None
) -> None:
    assert _interest_fsi(function, interest, None).rows == []


@_LOWER_FUNCTIONS
def test_interest_none_payer_with_interest_lines_refuses(function: str) -> None:
    ln = _interest_line(amount_usd=Decimal("5.00"), rate=Decimal("90.00"))
    with pytest.raises(
        InterestPayerCountryError,
        match=r"^interest income has no stated payer country; the source must state it$",
    ):
        _interest_fsi(function, _interest_result([ln]), None)


_ANY_INTEREST = pytest.mark.parametrize(
    "interest",
    [
        None,
        _EMPTY_INTEREST,
        _interest_result([_interest_line(amount_usd=Decimal("5.00"), rate=Decimal("90.00"))]),
    ],
    ids=["absent", "empty", "lines"],
)


@_LOWER_FUNCTIONS
@_ANY_INTEREST
@pytest.mark.parametrize("iso", [False, []], ids=repr)
def test_interest_non_string_payer_refuses_with_or_without_interest(
    function: str, interest: ScheduleOsInterestComputeResult | None, iso: object
) -> None:
    with pytest.raises(InterestPayerCountryError) as exc:
        _interest_fsi(function, interest, iso)
    assert str(exc.value) == (
        f"interest payer country must be an ISO 3166-1 alpha-2 string, got {iso!r}"
    )


@_LOWER_FUNCTIONS
@_ANY_INTEREST
@pytest.mark.parametrize("iso", ["", "ZZ"])
def test_interest_unmapped_payer_refuses_with_or_without_interest(
    function: str, interest: ScheduleOsInterestComputeResult | None, iso: str
) -> None:
    """An unmapped country fails before the empty-interest return, so no
    caller learns of a bad country only once interest appears."""
    with pytest.raises(CountryCodeNotFoundError):
        _interest_fsi(function, interest, iso)


@_LOWER_FUNCTIONS
@_NO_INTEREST
@pytest.mark.parametrize("iso", ["US", "IE", "ie"])
def test_interest_valid_payer_without_interest_yields_no_row(
    function: str, interest: ScheduleOsInterestComputeResult | None, iso: str
) -> None:
    assert _interest_fsi(function, interest, iso).rows == []


@_ANY_INTEREST
def test_interest_payer_argument_is_required(
    interest: ScheduleOsInterestComputeResult | None,
) -> None:
    with pytest.raises(TypeError, match="broker_domicile_iso2"):
        compute_schedule_fsi_from_interest(interest)  # ty: ignore[missing-argument]
    with pytest.raises(TypeError, match="broker_domicile_iso2"):
        compute_schedule_fsi(  # ty: ignore[missing-argument]
            os_result=_empty_os_result(), interest_result=interest
        )


# ---- dispatcher — interest contribution ----------------------------------


def test_dispatcher_only_interest_no_dividends_no_cg() -> None:
    """Interest-only pipeline yields a single (US, OTHER_SOURCES)
    row from the dispatcher."""
    ln = _interest_line(amount_usd=Decimal("10.00"), rate=Decimal("84.00"))
    result = compute_schedule_fsi(
        os_result=ScheduleOsComputeResult(lines=[], domestic_lines=[]),
        interest_result=_interest_result([ln]),
        broker_domicile_iso2="US",
    )

    assert len(result.rows) == 1
    row = result.rows[0]
    assert row.iso_alpha2 == "US"
    assert row.head_of_income is HeadOfIncome.OTHER_SOURCES
    assert row.income_inr == Decimal("840.00")
    assert row.event_count == 1


def test_dispatcher_dividends_and_interest_same_country_merge_into_single_row() -> None:
    """US dividends + US broker interest collapse into a SINGLE
    (US, OTHER_SOURCES) row whose ``income_inr`` is the sum of both
    contributions and whose ``event_count`` is the sum of both
    contributors' event counts. This is the canonical merge case
    documented in the dispatcher's merge-contract docstring."""
    div = _foreign_div_line(
        symbol="XQAW",
        isin="US99990E1038",
        issuer_country_iso2="US",
        payment_date=date(2025, 5, 5),
        amount_usd=Decimal("100.00"),
        rate=Decimal("84.50"),
    )
    interest = _interest_line(amount_usd=Decimal("10.00"), rate=Decimal("84.00"))

    result = compute_schedule_fsi(
        os_result=ScheduleOsComputeResult(lines=[div], domestic_lines=[]),
        interest_result=_interest_result([interest]),
        broker_domicile_iso2="US",
    )

    assert len(result.rows) == 1
    row = result.rows[0]
    assert row.iso_alpha2 == "US"
    assert row.head_of_income is HeadOfIncome.OTHER_SOURCES
    # 100 * 84.50 (dividend) + 10 * 84.00 (interest) = 8450 + 840 = 9290
    assert row.income_inr == Decimal("9290.00")
    assert row.event_count == 2


def test_dispatcher_us_dividends_plus_ireland_interest_yields_two_rows() -> None:
    """US dividends (issuer country) + IBKR-Ireland broker interest
    yield TWO distinct rows because the broker-domicile attribution
    is independent of issuer attribution."""
    div = _foreign_div_line(
        symbol="XQAW",
        isin="US99990E1038",
        issuer_country_iso2="US",
        payment_date=date(2025, 5, 5),
        amount_usd=Decimal("100.00"),
        rate=Decimal("84.50"),
    )
    interest = _interest_line(amount_usd=Decimal("10.00"), rate=Decimal("90.00"))

    result = compute_schedule_fsi(
        os_result=ScheduleOsComputeResult(lines=[div], domestic_lines=[]),
        interest_result=_interest_result([interest]),
        broker_domicile_iso2="IE",
    )

    assert len(result.rows) == 2
    iso_to_row = {r.iso_alpha2: r for r in result.rows}
    assert set(iso_to_row.keys()) == {"US", "IE"}
    assert iso_to_row["US"].head_of_income is HeadOfIncome.OTHER_SOURCES
    assert iso_to_row["US"].income_inr == Decimal("8450.00")
    assert iso_to_row["US"].event_count == 1
    assert iso_to_row["IE"].head_of_income is HeadOfIncome.OTHER_SOURCES
    assert iso_to_row["IE"].income_inr == Decimal("900.00")
    assert iso_to_row["IE"].event_count == 1


def test_dispatcher_us_cg_plus_us_interest_yields_two_rows_different_heads() -> None:
    """US CG (CAPITAL_GAINS) + US broker interest (OTHER_SOURCES)
    yield two rows for the same country under distinct heads — the
    merge primitive only collapses on full ``(country, head)`` key
    matches."""
    cg = _stcg_line(
        cost_basis_usd=Decimal("100.00"),
        proceeds_usd=Decimal("150.00"),
        rate=Decimal("84.50"),
    )
    interest = _interest_line(amount_usd=Decimal("10.00"), rate=Decimal("84.00"))

    result = compute_schedule_fsi(
        os_result=ScheduleOsComputeResult(lines=[], domestic_lines=[]),
        cg_stcg_result=_stcg_result([cg]),
        interest_result=_interest_result([interest]),
        broker_domicile_iso2="US",
    )

    assert len(result.rows) == 2
    head_to_row = {r.head_of_income: r for r in result.rows}
    assert head_to_row[HeadOfIncome.CAPITAL_GAINS].income_inr == Decimal("4225.00")
    assert head_to_row[HeadOfIncome.CAPITAL_GAINS].event_count == 1
    assert head_to_row[HeadOfIncome.OTHER_SOURCES].income_inr == Decimal("840.00")
    assert head_to_row[HeadOfIncome.OTHER_SOURCES].event_count == 1


def test_dispatcher_omitting_interest_kwarg_matches_pre_interest_behavior() -> None:
    """Omitting ``interest_result`` (or passing ``None``) reproduces
    the pre-interest dispatcher behaviour — backwards compatibility
    with PRs #208 and #209 callers."""
    div = _foreign_div_line(
        symbol="XQAW",
        isin="US99990E1038",
        issuer_country_iso2="US",
        payment_date=date(2025, 5, 5),
        amount_usd=Decimal("100.00"),
        rate=Decimal("84.50"),
    )
    cg = _stcg_line(
        cost_basis_usd=Decimal("100.00"),
        proceeds_usd=Decimal("150.00"),
        rate=Decimal("84.50"),
    )
    os_result = ScheduleOsComputeResult(lines=[div], domestic_lines=[])

    without_kwarg = compute_schedule_fsi(
        os_result=os_result,
        cg_stcg_result=_stcg_result([cg]),
        broker_domicile_iso2=None,
    )
    with_none = compute_schedule_fsi(
        os_result=os_result,
        cg_stcg_result=_stcg_result([cg]),
        interest_result=None,
        broker_domicile_iso2=None,
    )

    assert without_kwarg == with_none


# ---- shared WHT fixtures -------------------------------------------------


class _FlatWhtFxPolicy(FxPolicy):
    """Deterministic FX policy for WHT tests.

    Returns a per-date rate from the supplied map, or a fallback
    constant. Modeled on the per-test `_FlatFxPolicy` in
    `tests/test_compute_schedule_cg.py`. The WHT aggregator
    requires `currency == "USD"`.
    """

    currency = "USD"

    def __init__(
        self,
        rates: dict[date, Decimal] | None = None,
        default: Decimal = Decimal("84.00"),
    ) -> None:
        self._rates = rates or {}
        self._default = default

    @override
    def rate_for(self, event_date: date) -> Decimal:
        return self._rates.get(event_date, self._default)


class _NonUsdPolicy(FxPolicy):
    """A non-USD FX policy used to exercise the strict-USD guard."""

    currency = "EUR"

    @override
    def rate_for(self, event_date: date) -> Decimal:
        return Decimal("90.00")


_WHT_FY_START = date(2025, 4, 1)
_WHT_FY_END = date(2026, 3, 31)


def _wht(
    *,
    symbol: str = "XQAA",
    isin: str = "US99990A3095",
    payment_date: date = date(2025, 5, 16),
    amount: Decimal = Decimal("-1.50"),
    currency: str = "USD",
    description: str = "XQAA(US99990A3095) Cash Dividend USD 0.35 per Share - US Tax",
) -> WithholdingTax:
    """Construct one valid WithholdingTax row for tests."""
    return WithholdingTax(
        symbol=Ticker(symbol),
        isin=Isin(isin),
        payment_date=payment_date,
        amount=amount,
        currency=currency,
        description=description,
    )


def _us_dividend_for_wht_merge(
    *,
    payment_date: date = date(2025, 5, 5),
    amount_usd: Decimal = Decimal("100.00"),
    rate: Decimal = Decimal("84.50"),
) -> ScheduleOsDividendLine:
    """Convenience: a US dividend the WHT layer can attach tax to."""
    return _foreign_div_line(
        symbol="XQAA",
        isin="US99990A3095",
        issuer_country_iso2="US",
        payment_date=payment_date,
        amount_usd=amount_usd,
        rate=rate,
    )


# ---- compute_schedule_fsi_from_withholding_tax — happy path --------------


def test_wht_empty_iterable_yields_no_contributions() -> None:
    """No WHT rows at all → empty contribution list, NO country
    lookup attempted (so a stub fx_policy whose `currency` is USD
    but whose `rate_for` would raise is never invoked)."""
    contributions = compute_schedule_fsi_from_withholding_tax(
        [],
        fx_policy=_FlatWhtFxPolicy(),
        financial_year_start=_WHT_FY_START,
        financial_year_end=_WHT_FY_END,
    ).contributions
    assert contributions == []


def test_wht_single_negative_row_emits_positive_contribution() -> None:
    """Net tax paid in INR is the negation of the signed sum (which
    is itself negative). One row at -$1.50 * 84 = -126 → +126 INR."""
    row = _wht(amount=Decimal("-1.50"), payment_date=date(2025, 5, 16))
    contributions = compute_schedule_fsi_from_withholding_tax(
        [row],
        fx_policy=_FlatWhtFxPolicy(default=Decimal("84.00")),
        financial_year_start=_WHT_FY_START,
        financial_year_end=_WHT_FY_END,
    ).contributions

    assert len(contributions) == 1
    c = contributions[0]
    assert c.cbdt_country_code == "2"
    assert c.iso_alpha2 == "US"
    assert c.head_of_income is HeadOfIncome.OTHER_SOURCES
    assert c.foreign_tax_inr == Decimal("126.00")
    assert c.event_count == 1


def test_wht_multiple_negative_rows_same_country_sum() -> None:
    """Multiple in-FY WHT rows for the same country produce a single
    contribution whose foreign_tax_inr is the (negated) signed sum
    and whose event_count is the row count."""
    rows = [
        _wht(amount=Decimal("-1.00"), payment_date=date(2025, 5, 16)),
        _wht(amount=Decimal("-2.50"), payment_date=date(2025, 8, 16)),
        _wht(amount=Decimal("-0.75"), payment_date=date(2025, 11, 16)),
    ]
    contributions = compute_schedule_fsi_from_withholding_tax(
        rows,
        fx_policy=_FlatWhtFxPolicy(default=Decimal("84.00")),
        financial_year_start=_WHT_FY_START,
        financial_year_end=_WHT_FY_END,
    ).contributions

    assert len(contributions) == 1
    c = contributions[0]
    assert c.iso_alpha2 == "US"
    # Per-event FX (Rule 128(5)(ii)): each row converted on its own
    # payment_date, then summed in INR. With a flat rate this matches
    # (1.00 + 2.50 + 0.75) * 84 = 357.00; with real per-event rates
    # the rates would differ per row.
    assert c.foreign_tax_inr == Decimal("357.00")
    assert c.event_count == 3


def test_wht_multiple_countries_one_contribution_each_sorted() -> None:
    """Two countries → two contributions, sorted by cbdt_country_code
    for deterministic output (same convention as the dividend / CG
    aggregators)."""
    rows = [
        _wht(
            symbol="XQCQ",
            isin="GB99970N1077",  # GB prefix
            amount=Decimal("-0.80"),
            payment_date=date(2025, 6, 16),
        ),
        _wht(
            symbol="XQAA",
            isin="US99990A3095",  # US prefix
            amount=Decimal("-1.50"),
            payment_date=date(2025, 5, 16),
        ),
    ]
    contributions = compute_schedule_fsi_from_withholding_tax(
        rows,
        fx_policy=_FlatWhtFxPolicy(default=Decimal("100.00")),
        financial_year_start=_WHT_FY_START,
        financial_year_end=_WHT_FY_END,
    ).contributions

    assert len(contributions) == 2
    by_iso = {c.iso_alpha2: c for c in contributions}
    assert by_iso["US"].foreign_tax_inr == Decimal("150.00")
    assert by_iso["GB"].foreign_tax_inr == Decimal("80.00")
    # Sorted by CBDT country code:
    expected_order = sorted(
        contributions, key=lambda c: (c.cbdt_country_code, c.head_of_income.value)
    )
    assert contributions == expected_order


def test_wht_within_country_reversal_nets_against_tax_paid() -> None:
    """IBKR sign convention: NEGATIVE = tax paid, POSITIVE = reversal.
    Reversals net against tax paid for the same source country."""
    rows = [
        _wht(amount=Decimal("-2.00"), payment_date=date(2025, 5, 16)),  # tax paid
        _wht(amount=Decimal("-1.00"), payment_date=date(2025, 5, 16)),  # tax paid
        _wht(amount=Decimal("+1.00"), payment_date=date(2025, 6, 16)),  # reversal
    ]
    contributions = compute_schedule_fsi_from_withholding_tax(
        rows,
        fx_policy=_FlatWhtFxPolicy(default=Decimal("84.00")),
        financial_year_start=_WHT_FY_START,
        financial_year_end=_WHT_FY_END,
    ).contributions

    assert len(contributions) == 1
    # Per-event FX (Rule 128(5)(ii)): tax-paid rows contribute
    # (-3)*84 = -252 INR; reversal row contributes (+1)*84 = +84 INR;
    # per-country signed sum is -168 INR; foreign_tax_inr = -(-168) = 168.
    assert contributions[0].foreign_tax_inr == Decimal("168.00")
    # event_count includes the tax-paid rows AND the reversal row.
    assert contributions[0].event_count == 3


def test_wht_reversal_exceeding_tax_paid_drops_country() -> None:
    """Net-refund country (reversals > tax paid in the FY, in INR) is
    dropped: Schedule FSI has no representation for a negative tax credit
    and the user wouldn't claim FTC on a net refund."""
    rows = [
        _wht(amount=Decimal("-1.00"), payment_date=date(2025, 5, 16)),
        _wht(amount=Decimal("+1.00"), payment_date=date(2025, 6, 16)),  # at a higher rate
    ]
    contributions = compute_schedule_fsi_from_withholding_tax(
        rows,
        fx_policy=_FlatWhtFxPolicy({date(2025, 6, 16): Decimal("85.00")}),
        financial_year_start=_WHT_FY_START,
        financial_year_end=_WHT_FY_END,
    ).contributions

    assert contributions == []


@pytest.mark.parametrize(
    ("rows", "unproven"),
    [
        pytest.param(
            [
                _wht(amount=Decimal("-5.00"), payment_date=date(2025, 3, 15)),  # prior FY
                _wht(amount=Decimal("-5.00"), payment_date=date(2025, 6, 15)),
                _wht(amount=Decimal("+5.00"), payment_date=date(2025, 8, 1)),
            ],
            TWO_FY_CANDIDATES,
            id="ambiguous-across-fys",
        ),
        pytest.param(
            [
                _wht(amount=Decimal("-3.00"), payment_date=date(2025, 6, 15)),
                _wht(amount=Decimal("+5.00"), payment_date=date(2025, 8, 1)),
            ],
            NO_ORIGINAL,
            id="orphan",
        ),
    ],
)
def test_wht_reversal_of_an_unproven_year_refuses(
    rows: list[WithholdingTax], unproven: str
) -> None:
    """#893: netting either refund guesses which year's tax it cancels. The
    refusal names why, not the refund's ISIN, amount or dates (#1047)."""
    with pytest.raises(UnprovenWhtReversalError) as excinfo:
        compute_schedule_fsi_from_withholding_tax(
            rows,
            fx_policy=_FlatWhtFxPolicy(),
            financial_year_start=_WHT_FY_START,
            financial_year_end=_WHT_FY_END,
        )
    assert str(excinfo.value) == unproven_reversal_message(
        unproven, fy_start=_WHT_FY_START, fy_end=_WHT_FY_END
    )
    assert_no_wht_echo(str(excinfo.value), rows)


def test_wht_exact_offset_drops_country() -> None:
    """Net == 0 also drops (no positive tax credit to claim)."""
    rows = [
        _wht(amount=Decimal("-1.50"), payment_date=date(2025, 5, 16)),
        _wht(amount=Decimal("+1.50"), payment_date=date(2025, 6, 16)),
    ]
    contributions = compute_schedule_fsi_from_withholding_tax(
        rows,
        fx_policy=_FlatWhtFxPolicy(default=Decimal("84.00")),
        financial_year_start=_WHT_FY_START,
        financial_year_end=_WHT_FY_END,
    ).contributions
    assert contributions == []


def test_wht_out_of_fy_rows_silently_skipped() -> None:
    """Rows whose payment_date is outside the FY window are silently
    filtered before any conversion / lookup (so an out-of-FY row's
    ISIN does NOT need to be in the country map)."""
    pre_fy = _wht(amount=Decimal("-1.00"), payment_date=date(2025, 3, 31))
    in_fy = _wht(amount=Decimal("-2.00"), payment_date=date(2025, 5, 16))
    post_fy = _wht(amount=Decimal("-3.00"), payment_date=date(2026, 4, 1))
    rows = [pre_fy, in_fy, post_fy]
    contributions = compute_schedule_fsi_from_withholding_tax(
        rows,
        fx_policy=_FlatWhtFxPolicy(default=Decimal("84.00")),
        financial_year_start=_WHT_FY_START,
        financial_year_end=_WHT_FY_END,
    ).contributions

    assert len(contributions) == 1
    # Only the May-16 row contributes: -(-2) * 84 = 168
    assert contributions[0].foreign_tax_inr == Decimal("168.00")
    assert contributions[0].event_count == 1


def test_wht_fy_start_date_inclusive_apr_1() -> None:
    """A WHT row whose payment_date is exactly the FY start (Apr 1)
    is INCLUDED, not filtered. Mirrors the half-open vs closed-interval
    contract: ``payment_date >= financial_year_start`` (inclusive)."""
    row = _wht(amount=Decimal("-1.00"), payment_date=_WHT_FY_START)
    contributions = compute_schedule_fsi_from_withholding_tax(
        [row],
        fx_policy=_FlatWhtFxPolicy(default=Decimal("84.00")),
        financial_year_start=_WHT_FY_START,
        financial_year_end=_WHT_FY_END,
    ).contributions

    assert len(contributions) == 1
    assert contributions[0].foreign_tax_inr == Decimal("84.00")
    assert contributions[0].event_count == 1


def test_wht_fy_end_date_inclusive_mar_31() -> None:
    """A WHT row whose payment_date is exactly the FY end (Mar 31)
    is INCLUDED, not filtered. Mirrors the half-open vs closed-interval
    contract: ``payment_date <= financial_year_end`` (inclusive)."""
    row = _wht(amount=Decimal("-1.00"), payment_date=_WHT_FY_END)
    contributions = compute_schedule_fsi_from_withholding_tax(
        [row],
        fx_policy=_FlatWhtFxPolicy(default=Decimal("84.00")),
        financial_year_start=_WHT_FY_START,
        financial_year_end=_WHT_FY_END,
    ).contributions

    assert len(contributions) == 1
    assert contributions[0].foreign_tax_inr == Decimal("84.00")
    assert contributions[0].event_count == 1


def test_wht_per_event_fx_rate_used_for_each_row() -> None:
    """Each row's FX is computed on its own payment_date (Rule 128(5)(ii)
    is per-event). Rates from different months produce different INR."""
    rows = [
        _wht(amount=Decimal("-1.00"), payment_date=date(2025, 5, 16)),
        _wht(amount=Decimal("-1.00"), payment_date=date(2025, 11, 16)),
    ]
    policy = _FlatWhtFxPolicy(
        rates={
            date(2025, 5, 16): Decimal("84.00"),
            date(2025, 11, 16): Decimal("90.00"),
        },
        default=Decimal("999.00"),  # should never be consulted
    )
    contributions = compute_schedule_fsi_from_withholding_tax(
        rows,
        fx_policy=policy,
        financial_year_start=_WHT_FY_START,
        financial_year_end=_WHT_FY_END,
    ).contributions

    assert len(contributions) == 1
    # -(-1) * 84 + -(-1) * 90 = 84 + 90 = 174
    assert contributions[0].foreign_tax_inr == Decimal("174.00")


# ---- compute_schedule_fsi_from_withholding_tax — error paths -------------


def test_wht_non_usd_fx_policy_raises() -> None:
    """v1 strict-USD guard: a non-USD FX policy is rejected before
    any rows are processed."""
    with pytest.raises(InvariantError):
        compute_schedule_fsi_from_withholding_tax(
            [],
            fx_policy=_NonUsdPolicy(),
            financial_year_start=_WHT_FY_START,
            financial_year_end=_WHT_FY_END,
        )


def test_wht_non_usd_row_with_usd_policy_raises() -> None:
    """A WHT row whose currency mismatches the policy's currency
    surfaces immediately."""
    eur_row = _wht(
        amount=Decimal("-1.00"),
        currency="EUR",
        description="XQCQ(GB99970N1077) Cash Dividend EUR 0.10 per Share - UK Tax",
    )
    with pytest.raises(InvariantError):
        compute_schedule_fsi_from_withholding_tax(
            [eur_row],
            fx_policy=_FlatWhtFxPolicy(),
            financial_year_start=_WHT_FY_START,
            financial_year_end=_WHT_FY_END,
        )


def test_wht_unknown_isin_country_prefix_raises() -> None:
    """A WHT row whose ISIN's first two letters are not in the
    vendored country map raises CountryCodeNotFoundError (fail-fast
    pre-pass, same pattern as the other aggregators)."""
    bad_row = _wht(
        isin="ZZ99990A3095",  # ZZ is not a real ISO 3166-1 alpha-2
        amount=Decimal("-1.00"),
        payment_date=date(2025, 5, 16),
    )
    with pytest.raises(CountryCodeNotFoundError):
        compute_schedule_fsi_from_withholding_tax(
            [bad_row],
            fx_policy=_FlatWhtFxPolicy(),
            financial_year_start=_WHT_FY_START,
            financial_year_end=_WHT_FY_END,
        )


# ---- dispatcher — WHT layered onto matching income row -------------------


def test_dispatcher_us_dividend_plus_us_wht_layers_tax_onto_row() -> None:
    """The canonical case: a foreign US dividend + matching US WHT
    yields a single (US, OTHER_SOURCES) row whose income_inr is the
    dividend INR and whose foreign_tax_inr is the WHT INR."""
    div = _us_dividend_for_wht_merge(
        payment_date=date(2025, 5, 5),
        amount_usd=Decimal("100.00"),
        rate=Decimal("84.50"),
    )
    wht_row = _wht(
        amount=Decimal("-15.00"),
        payment_date=date(2025, 5, 16),
    )

    result = compute_schedule_fsi(
        os_result=ScheduleOsComputeResult(lines=[div], domestic_lines=[]),
        withholding_tax=[wht_row],
        wht_fx_policy=_FlatWhtFxPolicy(default=Decimal("84.00")),
        financial_year_start=_WHT_FY_START,
        financial_year_end=_WHT_FY_END,
        broker_domicile_iso2=None,
    )

    assert len(result.rows) == 1
    row = result.rows[0]
    assert row.iso_alpha2 == "US"
    assert row.head_of_income is HeadOfIncome.OTHER_SOURCES
    assert row.income_inr == Decimal("8450.00")
    # WHT INR = 15 USD times 84 INR/USD = 1260 INR
    assert row.foreign_tax_inr == Decimal("1260.00")
    # event_count counts INCOME events only; WHT does NOT inflate it.
    assert row.event_count == 1


def test_dispatcher_omitting_wht_kwarg_matches_pre_wht_behavior() -> None:
    """Omitting `withholding_tax` (or passing None) reproduces the
    pre-WHT dispatcher behaviour — backwards compat with PRs #208 /
    #209 / #210."""
    div = _us_dividend_for_wht_merge()
    os_result = ScheduleOsComputeResult(lines=[div], domestic_lines=[])

    without_kwarg = compute_schedule_fsi(os_result=os_result, broker_domicile_iso2=None)
    with_none = compute_schedule_fsi(
        os_result=os_result,
        withholding_tax=None,
        broker_domicile_iso2=None,
    )

    assert without_kwarg == with_none


def test_dispatcher_wht_without_fx_policy_raises() -> None:
    """When `withholding_tax` is provided, `wht_fx_policy`,
    `financial_year_start`, and `financial_year_end` are all required."""
    div = _us_dividend_for_wht_merge()
    wht_row = _wht(amount=Decimal("-1.00"), payment_date=date(2025, 5, 16))

    with pytest.raises(InvariantError):
        compute_schedule_fsi(
            os_result=ScheduleOsComputeResult(lines=[div], domestic_lines=[]),
            withholding_tax=[wht_row],
            # wht_fx_policy omitted!
            financial_year_start=_WHT_FY_START,
            financial_year_end=_WHT_FY_END,
            broker_domicile_iso2=None,
        )


def test_dispatcher_orphan_wht_raises_invariant_error() -> None:
    """WHT for a country with no matching income row raises:
    Indian FTC (Rule 128(5)(i)) requires the foreign income to be
    declared before a credit can be claimed."""
    # No US dividend in the OS result, but a US WHT row exists.
    wht_row = _wht(
        amount=Decimal("-15.00"),
        payment_date=date(2025, 5, 16),
    )

    with pytest.raises(InvariantError):
        compute_schedule_fsi(
            os_result=ScheduleOsComputeResult(lines=[], domestic_lines=[]),
            withholding_tax=[wht_row],
            wht_fx_policy=_FlatWhtFxPolicy(),
            financial_year_start=_WHT_FY_START,
            financial_year_end=_WHT_FY_END,
            broker_domicile_iso2=None,
        )


def test_dispatcher_wht_for_capital_gains_head_does_not_merge_with_other_sources() -> None:
    """v1 WHT aggregator emits OTHER_SOURCES only (Dividends-section
    WHT). A construction of a WHT fragment with CAPITAL_GAINS head
    would not collide with an OTHER_SOURCES income row even if the
    country matches — different `(cbdt, head)` keys are independent.

    This test pins the contract by directly invoking `_merge_results`
    with a hand-built CAPITAL_GAINS WHT fragment and verifying the
    orphan-WHT guard fires."""
    div = _us_dividend_for_wht_merge()
    fragment = ScheduleFsiWhtContribution(
        cbdt_country_code="2",  # US
        country_name_portal="UNITED STATES OF AMERICA",
        iso_alpha2="US",
        head_of_income=HeadOfIncome.CAPITAL_GAINS,  # mismatched head!
        foreign_tax_inr=Decimal("100"),
        event_count=1,
    )

    div_result = compute_schedule_fsi_from_dividends(
        ScheduleOsComputeResult(lines=[div], domestic_lines=[])
    )

    with pytest.raises(InvariantError):
        _merge_results(div_result, wht_contributions=[fragment])


def test_dispatcher_us_wht_with_us_dividend_and_us_interest_layers_onto_merged_row() -> None:
    """The full canonical merge: US dividend + US broker interest +
    US WHT all collapse to a single (US, OTHER_SOURCES) row whose
    income is summed across the two income sources and whose
    foreign_tax_inr is the WHT alone."""
    div = _us_dividend_for_wht_merge(
        payment_date=date(2025, 5, 5),
        amount_usd=Decimal("100.00"),
        rate=Decimal("84.50"),
    )
    interest = _interest_line(amount_usd=Decimal("10.00"), rate=Decimal("84.00"))
    wht_row = _wht(amount=Decimal("-15.00"), payment_date=date(2025, 5, 16))

    result = compute_schedule_fsi(
        os_result=ScheduleOsComputeResult(lines=[div], domestic_lines=[]),
        interest_result=_interest_result([interest]),
        withholding_tax=[wht_row],
        wht_fx_policy=_FlatWhtFxPolicy(default=Decimal("84.00")),
        financial_year_start=_WHT_FY_START,
        financial_year_end=_WHT_FY_END,
        broker_domicile_iso2="US",
    )

    assert len(result.rows) == 1
    row = result.rows[0]
    # 100 * 84.50 (dividend) + 10 * 84.00 (interest) = 8450 + 840 = 9290
    assert row.income_inr == Decimal("9290.00")
    # WHT INR = 15 USD times 84 INR/USD = 1260 INR
    assert row.foreign_tax_inr == Decimal("1260.00")
    # event_count = 1 (dividend) + 1 (interest) = 2; WHT does NOT contribute.
    assert row.event_count == 2


def test_dispatcher_multi_country_wht_each_layered_onto_its_matching_row() -> None:
    """A US dividend + a GB dividend + corresponding WHT rows for
    each → two rows, each with its own WHT layered on."""
    us_div = _foreign_div_line(
        symbol="XQAA",
        isin="US99990A3095",
        issuer_country_iso2="US",
        payment_date=date(2025, 5, 5),
        amount_usd=Decimal("100.00"),
        rate=Decimal("84.50"),
    )
    gb_div = _foreign_div_line(
        symbol="XQCQ",
        isin="GB99970N1077",
        issuer_country_iso2="GB",
        payment_date=date(2025, 6, 5),
        amount_usd=Decimal("50.00"),
        rate=Decimal("85.00"),
    )
    us_wht = _wht(
        symbol="XQAA",
        isin="US99990A3095",
        amount=Decimal("-15.00"),
        payment_date=date(2025, 5, 16),
    )
    gb_wht = _wht(
        symbol="XQCQ",
        isin="GB99970N1077",
        amount=Decimal("-7.50"),
        payment_date=date(2025, 6, 16),
    )

    result = compute_schedule_fsi(
        os_result=ScheduleOsComputeResult(lines=[us_div, gb_div], domestic_lines=[]),
        withholding_tax=[us_wht, gb_wht],
        wht_fx_policy=_FlatWhtFxPolicy(default=Decimal("84.00")),
        financial_year_start=_WHT_FY_START,
        financial_year_end=_WHT_FY_END,
        broker_domicile_iso2=None,
    )

    assert len(result.rows) == 2
    by_iso = {r.iso_alpha2: r for r in result.rows}
    assert by_iso["US"].income_inr == Decimal("8450.00")
    assert by_iso["US"].foreign_tax_inr == Decimal("1260.00")  # 15 * 84
    assert by_iso["GB"].income_inr == Decimal("4250.00")
    assert by_iso["GB"].foreign_tax_inr == Decimal("630.00")  # 7.50 * 84


# ---- ScheduleFsiWhtContribution model invariants -------------------------


def test_wht_contribution_rejects_zero_or_negative_foreign_tax() -> None:
    """A non-positive foreign_tax_inr is rejected at construction
    (the aggregator drops net-refund countries before construction;
    direct callers shouldn't bypass that)."""
    with pytest.raises(InvariantError):
        ScheduleFsiWhtContribution(
            cbdt_country_code="2",
            country_name_portal="UNITED STATES OF AMERICA",
            iso_alpha2="US",
            head_of_income=HeadOfIncome.OTHER_SOURCES,
            foreign_tax_inr=Decimal("0"),
            event_count=1,
        )
    with pytest.raises(InvariantError):
        ScheduleFsiWhtContribution(
            cbdt_country_code="2",
            country_name_portal="UNITED STATES OF AMERICA",
            iso_alpha2="US",
            head_of_income=HeadOfIncome.OTHER_SOURCES,
            foreign_tax_inr=Decimal("-100"),
            event_count=1,
        )


def test_wht_contribution_rejects_zero_event_count() -> None:
    """event_count == 0 makes no sense for a positive-tax contribution."""
    with pytest.raises(InvariantError):
        ScheduleFsiWhtContribution(
            cbdt_country_code="2",
            country_name_portal="UNITED STATES OF AMERICA",
            iso_alpha2="US",
            head_of_income=HeadOfIncome.OTHER_SOURCES,
            foreign_tax_inr=Decimal("100"),
            event_count=0,
        )


def test_wht_contribution_rejects_lowercase_iso() -> None:
    """ISO must be uppercase 2-letter alphabetic."""
    with pytest.raises(InvariantError):
        ScheduleFsiWhtContribution(
            cbdt_country_code="2",
            country_name_portal="UNITED STATES OF AMERICA",
            iso_alpha2="us",
            head_of_income=HeadOfIncome.OTHER_SOURCES,
            foreign_tax_inr=Decimal("100"),
            event_count=1,
        )


def test_wht_contribution_rejects_non_ascii_capital_in_iso() -> None:
    """#854: ``isupper`` and ``isalpha`` accept ``"Ü"``; ISO 3166-1 is ASCII."""
    with pytest.raises(InvariantError) as exc:
        ScheduleFsiWhtContribution(
            cbdt_country_code="2",
            country_name_portal="UNITED STATES OF AMERICA",
            iso_alpha2="ÜS",
            head_of_income=HeadOfIncome.OTHER_SOURCES,
            foreign_tax_inr=Decimal("100"),
            event_count=1,
        )
    assert exc.value.what == (
        "ScheduleFsiWhtContribution.iso_alpha2 must be a 2-letter uppercase ISO 3166-1 code"
    )
    assert exc.value.context == (("iso_alpha2", None),)


def test_wht_contribution_rejects_empty_cbdt_code() -> None:
    with pytest.raises(InvariantError):
        ScheduleFsiWhtContribution(
            cbdt_country_code="",
            country_name_portal="UNITED STATES OF AMERICA",
            iso_alpha2="US",
            head_of_income=HeadOfIncome.OTHER_SOURCES,
            foreign_tax_inr=Decimal("100"),
            event_count=1,
        )


def test_wht_contribution_rejects_empty_country_name() -> None:
    with pytest.raises(InvariantError):
        ScheduleFsiWhtContribution(
            cbdt_country_code="2",
            country_name_portal="",
            iso_alpha2="US",
            head_of_income=HeadOfIncome.OTHER_SOURCES,
            foreign_tax_inr=Decimal("100"),
            event_count=1,
        )


# ---- _merge_results — WHT-fragment specific edge cases -------------------


def test_merge_wht_fragment_iso_drift_against_income_row_raises() -> None:
    """A fragment whose iso_alpha2 mismatches the existing income
    row for the same CBDT code raises (aggregator-bug indicator)."""
    div = _us_dividend_for_wht_merge()
    div_result = compute_schedule_fsi_from_dividends(
        ScheduleOsComputeResult(lines=[div], domestic_lines=[])
    )
    bogus = ScheduleFsiWhtContribution(
        cbdt_country_code="2",  # matches US
        country_name_portal="UNITED STATES OF AMERICA",
        iso_alpha2="ZZ",  # but iso drifts!
        head_of_income=HeadOfIncome.OTHER_SOURCES,
        foreign_tax_inr=Decimal("100"),
        event_count=1,
    )
    with pytest.raises(InvariantError):
        _merge_results(div_result, wht_contributions=[bogus])


def test_merge_wht_fragment_country_name_drift_raises() -> None:
    """A fragment whose country_name_portal mismatches raises."""
    div = _us_dividend_for_wht_merge()
    div_result = compute_schedule_fsi_from_dividends(
        ScheduleOsComputeResult(lines=[div], domestic_lines=[])
    )
    bogus = ScheduleFsiWhtContribution(
        cbdt_country_code="2",
        country_name_portal="UNITED STATES",  # drift!
        iso_alpha2="US",
        head_of_income=HeadOfIncome.OTHER_SOURCES,
        foreign_tax_inr=Decimal("100"),
        event_count=1,
    )
    with pytest.raises(InvariantError):
        _merge_results(div_result, wht_contributions=[bogus])


def test_merge_wht_contributions_none_is_no_op() -> None:
    """`wht_contributions=None` is the default and a no-op (no
    fragment loop runs)."""
    div = _us_dividend_for_wht_merge()
    div_result = compute_schedule_fsi_from_dividends(
        ScheduleOsComputeResult(lines=[div], domestic_lines=[])
    )
    merged = _merge_results(div_result, wht_contributions=None)
    assert merged == _merge_results(div_result)


def test_merge_multiple_wht_fragments_for_same_key_sum_into_row() -> None:
    """If two WHT fragments somehow target the same (cbdt, head)
    key (e.g. a future aggregator splits per-source-type), they are
    layered in sequence — each contributes its foreign_tax_inr to
    the running row total."""
    div = _us_dividend_for_wht_merge()
    div_result = compute_schedule_fsi_from_dividends(
        ScheduleOsComputeResult(lines=[div], domestic_lines=[])
    )
    f1 = ScheduleFsiWhtContribution(
        cbdt_country_code="2",
        country_name_portal="UNITED STATES OF AMERICA",
        iso_alpha2="US",
        head_of_income=HeadOfIncome.OTHER_SOURCES,
        foreign_tax_inr=Decimal("100"),
        event_count=1,
    )
    f2 = ScheduleFsiWhtContribution(
        cbdt_country_code="2",
        country_name_portal="UNITED STATES OF AMERICA",
        iso_alpha2="US",
        head_of_income=HeadOfIncome.OTHER_SOURCES,
        foreign_tax_inr=Decimal("250"),
        event_count=1,
    )
    merged = _merge_results(div_result, wht_contributions=[f1, f2])

    assert len(merged.rows) == 1
    assert merged.rows[0].foreign_tax_inr == Decimal("350")


# ============================================================================
# Issue #225: ADR WHT skipped under UNDERLYING_ISSUER routing.
# ============================================================================
#
# Symmetric with the Form 67 fix: when the holder is in UNDERLYING_ISSUER
# mode, Schedule OS reroutes mapped Indian-ADR dividends to the domestic
# bucket. The matching WHT (depositary-prefix ISIN) must NOT contribute to
# the depositary country's Schedule FSI WHT total. Test scope here is the
# compute_schedule_fsi_from_withholding_tax aggregator; the per-ISIN
# orphan-guard interaction is exercised at pipeline level.

_INFY_ADR_ISIN_FSI: Isin = Isin("US4567881085")


def test_wht_indian_adr_skipped_under_underlying_issuer_mode() -> None:
    """INFY (Indian ADR) WHT must be skipped when UNDERLYING_ISSUER
    is in effect — even when the holder also has unrelated US WHT
    (XQAA). Only XQAA's WHT survives into the US contribution."""
    rows = [
        _wht(amount=Decimal("-1.50")),  # XQAA — US foreign
        _wht(
            symbol="INFY",
            isin=str(_INFY_ADR_ISIN_FSI),
            amount=Decimal("-3.00"),
            description="INFY(US4567881085) Cash Dividend - US Tax",
        ),
    ]
    contributions = compute_schedule_fsi_from_withholding_tax(
        rows,
        fx_policy=_FlatWhtFxPolicy(default=Decimal("84.00")),
        financial_year_start=_WHT_FY_START,
        financial_year_end=_WHT_FY_END,
        adr_routing_mode=ScheduleOsIndianAdrRoutingMode.UNDERLYING_ISSUER,
    ).contributions
    # One contribution: US with XQAA's 1.50 * 84 = 126 INR only.
    # INFY's 3.00 * 84 = 252 INR must NOT be present.
    assert len(contributions) == 1
    c = contributions[0]
    assert c.iso_alpha2 == "US"
    assert c.foreign_tax_inr == Decimal("126.00")
    assert c.event_count == 1


def test_wht_indian_adr_default_depositary_mode_preserves_legacy() -> None:
    """Default DEPOSITARY mode (kwarg omitted) keeps the legacy
    behavior: both XQAA and INFY WHT sum into the US bucket."""
    rows = [
        _wht(amount=Decimal("-1.50")),
        _wht(
            symbol="INFY",
            isin=str(_INFY_ADR_ISIN_FSI),
            amount=Decimal("-3.00"),
            description="INFY(US4567881085) Cash Dividend - US Tax",
        ),
    ]
    contributions = compute_schedule_fsi_from_withholding_tax(
        rows,
        fx_policy=_FlatWhtFxPolicy(default=Decimal("84.00")),
        financial_year_start=_WHT_FY_START,
        financial_year_end=_WHT_FY_END,
    ).contributions
    assert len(contributions) == 1
    c = contributions[0]
    assert c.iso_alpha2 == "US"
    # Both rows: (1.50 + 3.00) * 84 = 378 INR
    assert c.foreign_tax_inr == Decimal("378.00")
    assert c.event_count == 2


def test_wht_indian_adr_only_under_underlying_issuer_yields_no_contribution() -> None:
    """INFY-only WHT under UNDERLYING_ISSUER: skipped → no contributions
    at all. The per-ISIN orphan guard at the higher pipeline layer
    won't fire because there's nothing to orphan."""
    rows = [
        _wht(
            symbol="INFY",
            isin=str(_INFY_ADR_ISIN_FSI),
            amount=Decimal("-3.00"),
            description="INFY(US4567881085) Cash Dividend - US Tax",
        ),
    ]
    contributions = compute_schedule_fsi_from_withholding_tax(
        rows,
        fx_policy=_FlatWhtFxPolicy(default=Decimal("84.00")),
        financial_year_start=_WHT_FY_START,
        financial_year_end=_WHT_FY_END,
        adr_routing_mode=ScheduleOsIndianAdrRoutingMode.UNDERLYING_ISSUER,
    ).contributions
    assert contributions == []


# ---------------------------------------------------------------------------
# Relief the portal's read-only cell would overwrite (#900)
# ---------------------------------------------------------------------------


def _relief_row(
    *,
    foreign_tax_inr: Decimal = Decimal("300"),
    tax_payable_inr: Decimal | None = Decimal("400"),
    tax_relief_inr: Decimal | None = Decimal("250"),
    head: HeadOfIncome = HeadOfIncome.OTHER_SOURCES,
) -> ScheduleFsiRow:
    return ScheduleFsiRow(
        cbdt_country_code="2",
        country_name_portal="UNITED STATES OF AMERICA",
        iso_alpha2="US",
        head_of_income=head,
        income_inr=Decimal("1000"),
        foreign_tax_inr=foreign_tax_inr,
        event_count=1,
        tax_payable_inr=tax_payable_inr,
        tax_relief_inr=tax_relief_inr,
    )


def test_relief_check_refuses_relief_the_portal_overwrites() -> None:
    """The issue's example: ₹250 of treaty-capped relief against the portal's
    min(₹300, ₹400) = ₹300, named by the row's compute flag."""
    blank_payable = _relief_row(
        tax_payable_inr=None, tax_relief_inr=Decimal("0"), head=HeadOfIncome.CAPITAL_GAINS
    )
    rows = [blank_payable, _relief_row()]

    with pytest.raises(InvariantError, match="read-only relief") as excinfo:
        require_fsi_relief_matches_portal(rows)

    # Names the row by its CBDT-table country and head, not its amounts (#1047).
    assert str(excinfo.value) == PORTAL_RELIEF_REFUSAL
    assert excinfo.value.context == (
        ("country", None),
        ("head", None),
        ("computed_relief", None),
        ("portal_relief", None),
    )


def test_relief_check_names_the_refused_rows_own_country_and_head() -> None:
    """A UK Capital Gains row is named as such, not as a fixed label (#1047)."""
    row = replace(
        _relief_row(head=HeadOfIncome.CAPITAL_GAINS),
        cbdt_country_code="44",
        country_name_portal="UNITED KINGDOM OF GREAT BRITAIN AND NORTHERN IRELAND",
        iso_alpha2="GB",
    )

    with pytest.raises(InvariantError) as excinfo:
        require_fsi_relief_matches_portal([row])

    assert str(excinfo.value) == (
        "InvariantError: Schedule FSI filing would diverge from the portal's "
        "read-only relief (min of tax paid and Indian tax payable) on the GB "
        "Capital Gains row; resolve the filing amounts manually before filing "
        "(context keys: country, head, computed_relief, portal_relief)"
    )


def test_relief_check_accepts_relief_that_matches_in_whole_rupees() -> None:
    # Files as tax paid ₹644 and tax payable ₹1187; the portal's
    # min(644, 1187) = 644 equals the relief's ₹644.
    row = _relief_row(
        foreign_tax_inr=Decimal("643.8275"),
        tax_payable_inr=Decimal("1187.2486135"),
        tax_relief_inr=Decimal("643.8275"),
    )

    require_fsi_relief_matches_portal([row])


@pytest.mark.parametrize(
    ("tax_payable", "tax_relief"),
    [(Decimal("400"), None), (None, Decimal("250"))],
    ids=["blank-relief", "blank-payable"],
)
def test_relief_check_skips_rows_with_a_blank_cell(
    tax_payable: Decimal | None, tax_relief: Decimal | None
) -> None:
    require_fsi_relief_matches_portal(
        [_relief_row(tax_payable_inr=tax_payable, tax_relief_inr=tax_relief)]
    )


# ---- Other Sources income filed from Schedule OS (#1319) ---------------------


def _domestic_line(rate: Decimal) -> ScheduleOsDomesticDividendLine:
    """A USD 1.00 Indian dividend at ``rate``."""
    return ScheduleOsDomesticDividendLine(
        symbol=Ticker("XQCD"),
        isin=Isin("IN99970W1077"),
        issuer_country_iso2="IN",
        payment_date=date(2025, 5, 5),
        fx_specified_date=date(2025, 4, 30),
        fx_rate_publication_date=date(2025, 4, 30),
        fx_rate_inr_per_usd=rate,
        amount_gross_usd=Decimal("1.00"),
        amount_gross_inr=Decimal("1.00") * rate,
        description="XQCD(IN99970W1077) Cash Dividend USD 1.00 per Share (Ordinary Dividend)",
        kind=DistributionKind.CASH_DIVIDEND,
    )


def _other_sources_filed(result: ScheduleFsiComputeResult) -> dict[str, Decimal]:
    return {
        row.iso_alpha2: row.filed.income_inr
        for row in result.rows
        if row.head_of_income is HeadOfIncome.OTHER_SOURCES
    }


def _as_os_other_income(line: ScheduleOsSubstituteDividendLine) -> ScheduleOsOtherIncomeLine:
    """The Schedule OS 1e line the income engine makes of ``line``."""
    return ScheduleOsOtherIncomeLine(
        nature="Substitute payment in lieu of dividend",
        payment_date=line.payment_date,
        fx_specified_date=line.fx_specified_date,
        fx_rate_publication_date=line.fx_rate_publication_date,
        fx_rate_inr_per_usd=line.fx_rate_inr_per_usd,
        amount_gross_usd=line.amount_gross_usd,
        amount_gross_inr=line.amount_gross_inr,
        description=line.description,
        source_row_index=None,
        source_country_iso2=line.source_country_iso2,
    )


def _substitute(amount_inr: str) -> ScheduleOsSubstituteDividendLine:
    return ScheduleOsSubstituteDividendLine(
        symbol=Ticker("XQAW"),
        isin=Isin("US99990E1038"),
        issuer_country_iso2=_US,
        source_country_iso2=_US,
        payment_date=date(2025, 6, 15),
        fx_specified_date=date(2026, 3, 31),
        fx_rate_publication_date=date(2026, 3, 31),
        fx_rate_inr_per_usd=Decimal("50"),
        amount_gross_usd=Decimal(amount_inr) / Decimal("50"),
        amount_gross_inr=Decimal(amount_inr),
        description="Manufactured Div.",
    )


def test_two_country_rows_add_up_to_1ai_less_its_indian_share() -> None:
    """US ₹100.55, Canadian ₹100.50 and Indian ₹100.40 file 1a(i) ₹301. Each
    row's own HALF_UP filed US and Canada ₹101 each, ₹1 more than the ₹201
    left after India's share; the one rupee left after the floors goes to
    the US's larger remainder."""
    os_result = ScheduleOsComputeResult(
        lines=[
            _foreign_div_line(
                symbol=symbol,
                isin=isin,
                issuer_country_iso2=iso,
                payment_date=date(2025, 5, 5),
                amount_usd=Decimal("1.00"),
                rate=Decimal(rate),
            )
            for symbol, isin, iso, rate in (
                ("XQAW", "US99990E1038", "US", "100.55"),
                ("XQAM", "CA99991A1003", "CA", "100.50"),
            )
        ],
        domestic_lines=[_domestic_line(Decimal("100.40"))],
    )

    result = compute_schedule_fsi(os_result=os_result, broker_domicile_iso2=None)

    assert os_result.ordinary_dividend_total_inr_rounded == Decimal("301")
    assert _other_sources_filed(result) == {"CA": Decimal("100"), "US": Decimal("101")}
    # The exact income, which relief and the audit columns read, is unchanged.
    assert {row.iso_alpha2: row.income_inr for row in result.rows} == {
        "CA": Decimal("100.50"),
        "US": Decimal("100.55"),
    }


def test_a_1ai_tie_goes_to_the_earlier_country() -> None:
    """US and Canadian ₹100.50 file 1a(i) ₹201, ₹1 less than each row's own
    HALF_UP; the tied leftover rupee goes to Canada, first in ISO order."""
    os_result = ScheduleOsComputeResult(
        lines=[
            _foreign_div_line(
                symbol=symbol,
                isin=isin,
                issuer_country_iso2=iso,
                payment_date=date(2025, 5, 5),
                amount_usd=Decimal("1.00"),
                rate=Decimal("100.50"),
            )
            for symbol, isin, iso in (
                ("XQAW", "US99990E1038", "US"),
                ("XQAM", "CA99991A1003", "CA"),
            )
        ],
        domestic_lines=[],
    )

    result = compute_schedule_fsi(os_result=os_result, broker_domicile_iso2=None)

    assert _other_sources_filed(result) == {"CA": Decimal("101"), "US": Decimal("100")}


def test_a_1ai_tie_with_india_goes_to_the_foreign_country() -> None:
    """US and Indian ₹100.50 file 1a(i) ₹201; India's dividends split last,
    so the tied leftover rupee stays with the US row, as its own HALF_UP."""
    os_result = ScheduleOsComputeResult(
        lines=[
            _foreign_div_line(
                symbol="XQAW",
                isin="US99990E1038",
                issuer_country_iso2="US",
                payment_date=date(2025, 5, 5),
                amount_usd=Decimal("1.00"),
                rate=Decimal("100.50"),
            )
        ],
        domestic_lines=[_domestic_line(Decimal("100.50"))],
    )

    result = compute_schedule_fsi(os_result=os_result, broker_domicile_iso2=None)

    assert _other_sources_filed(result) == {"US": Decimal("101")}


def test_dividend_and_interest_row_files_1ai_plus_1bix() -> None:
    """The issue's example: USD 0.13 of dividend at ₹87.70 files 1a(i) ₹11 and
    USD 0.13 of interest at ₹93.15 files 1b(ix) ₹12; the US row's exact
    income rounds to ₹24."""
    os_result = ScheduleOsComputeResult(
        lines=[
            _foreign_div_line(
                symbol="XQBA",
                isin="US99995C1027",
                issuer_country_iso2="US",
                payment_date=date(2025, 9, 12),
                amount_usd=Decimal("0.13"),
                rate=Decimal("87.70"),
            )
        ],
        domestic_lines=[],
    )
    interest = _interest_result(
        [
            _interest_line(
                credit_date=date(2025, 9, 30), amount_usd=Decimal("0.13"), rate=Decimal("93.15")
            )
        ]
    )

    # The payer country is case-normalised, as the interest aggregator does.
    result = compute_schedule_fsi(
        os_result=os_result, interest_result=interest, broker_domicile_iso2="us"
    )

    assert (os_result.ordinary_dividend_total_inr_rounded, interest.total_inr_rounded) == (
        Decimal("11"),
        Decimal("12"),
    )
    [row] = result.rows
    assert row.filed.income_inr == Decimal("23")
    assert to_whole_rupees(row.income_inr) == Decimal("24")


def test_each_1e_nature_files_its_own_rupees() -> None:
    """A ₹0.50 lending fee and a ₹0.50 substitute payment each file 1e ₹1, so
    the US row files ₹2, not the ₹1 of its exact ₹1.00."""
    fee = _other_income_line("0.50")
    substitute = _substitute("0.50")
    os_result = replace(
        _empty_os_result(), other_income_lines=(_as_os_other_income(substitute), fee)
    )

    result = compute_schedule_fsi(
        os_result=os_result,
        substitute_dividend_result=ScheduleOsSubstituteDividendComputeResult(lines=(substitute,)),
        other_income_result=ScheduleOsOtherIncomeComputeResult(lines=(fee,)),
        broker_domicile_iso2=None,
    )

    assert [whole for _nature, _exact, whole in os_result.other_income_totals] == [
        Decimal("1"),
        Decimal("1"),
    ]
    assert _other_sources_filed(result) == {"US": Decimal("2")}


def test_a_1e_nature_splits_across_its_source_countries() -> None:
    """Two ₹0.60 fees, British and US, file 1e ₹1; the tie goes to the
    earlier country, so the US row files ₹0 rather than each filing ₹1."""
    fees = (
        _other_income_line("0.60"),
        _other_income_line("0.60", source_country_iso2=CountryCode("GB")),
    )
    os_result = replace(_empty_os_result(), other_income_lines=fees)

    result = compute_schedule_fsi(
        os_result=os_result,
        other_income_result=ScheduleOsOtherIncomeComputeResult(lines=fees),
        broker_domicile_iso2=None,
    )

    assert _other_sources_filed(result) == {"GB": Decimal("1"), "US": Decimal("0")}


def test_rows_that_agree_with_schedule_os_are_unchanged() -> None:
    """₹100.20 of dividend and ₹50.20 of interest file 1a(i) ₹100 and 1b(ix)
    ₹50: the ₹150 the row's exact ₹150.40 rounds to."""
    os_result = ScheduleOsComputeResult(
        lines=[
            _foreign_div_line(
                symbol="XQAW",
                isin="US99990E1038",
                issuer_country_iso2="US",
                payment_date=date(2025, 5, 5),
                amount_usd=Decimal("1.00"),
                rate=Decimal("100.20"),
            )
        ],
        domestic_lines=[],
    )
    interest = _interest_result(
        [_interest_line(amount_usd=Decimal("0.50"), rate=Decimal("100.40"))]
    )

    [row] = compute_schedule_fsi(
        os_result=os_result, interest_result=interest, broker_domicile_iso2="US"
    ).rows

    assert row.filed.income_inr == to_whole_rupees(row.income_inr) == Decimal("150")


def test_other_heads_keep_their_own_rounding() -> None:
    """Capital gains and business income come from no Schedule OS field."""
    result = compute_schedule_fsi(
        os_result=_empty_os_result(),
        cg_stcg_result=_stcg_result([_stcg_line(proceeds_usd=Decimal("150.01"))]),
        broker_domicile_iso2=None,
        additional_income_results=(
            compute_schedule_fsi_from_business_income(
                Decimal("10.5"), source_country_iso2="US", event_count=1
            ),
        ),
    )

    assert [(row.head_of_income, row.filed_income_inr) for row in result.rows] == [
        (HeadOfIncome.BUSINESS_PROFESSION, None),
        (HeadOfIncome.CAPITAL_GAINS, None),
    ]
    assert [row.filed.income_inr for row in result.rows] == [Decimal("11"), Decimal("4226")]


_NOT_FROM_SCHEDULE_OS = (
    "Schedule FSI Other Sources income must be exactly its Schedule OS fields' lines"
)


def test_other_sources_income_from_outside_schedule_os_refuses() -> None:
    """An adapter's Other Sources row has no Schedule OS field to file from."""
    extra = ScheduleFsiComputeResult(
        rows=[
            ScheduleFsiRow(
                cbdt_country_code="2",
                country_name_portal="UNITED STATES OF AMERICA",
                iso_alpha2="US",
                head_of_income=HeadOfIncome.OTHER_SOURCES,
                income_inr=Decimal("10"),
                foreign_tax_inr=Decimal("0"),
                event_count=1,
            )
        ]
    )

    with pytest.raises(InvariantError) as exc:
        compute_schedule_fsi(
            os_result=_empty_os_result(),
            broker_domicile_iso2=None,
            additional_income_results=(extra,),
        )
    assert exc.value.what == _NOT_FROM_SCHEDULE_OS


@pytest.mark.parametrize("with_dividend", [False, True], ids=["missing-country", "wrong-amount"])
def test_1e_lines_missing_from_schedule_os_refuse(with_dividend: bool) -> None:
    fee = _other_income_line("80")
    os_result = _empty_os_result()
    if with_dividend:
        os_result = replace(
            os_result,
            lines=[
                _foreign_div_line(
                    symbol="XQAW",
                    isin="US99990E1038",
                    issuer_country_iso2="US",
                    payment_date=date(2025, 5, 5),
                    amount_usd=Decimal("1.00"),
                    rate=Decimal("80"),
                )
            ],
        )

    with pytest.raises(InvariantError) as exc:
        compute_schedule_fsi(
            os_result=os_result,
            other_income_result=ScheduleOsOtherIncomeComputeResult(lines=(fee,)),
            broker_domicile_iso2=None,
        )
    assert exc.value.what == _NOT_FROM_SCHEDULE_OS
