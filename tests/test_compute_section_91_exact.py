"""The exact Form 67 section 91 credit (#1177).

The credit is the exact rational ``min(foreign_tax, total_tax * income /
total_income)``. It is proven to file, through the existing whole-rupee
HALF_UP rounding, as ``(2n + d) // (2d)`` of ``R = n/d``. The former formula
still runs as a validation-only preflight, so whatever it refused still
refuses with its own fixed message.

All values are synthetic. Section 91 credit cases use the no-treaty Bermuda,
Cayman Islands and British Virgin Islands reference rows, never Hong Kong's.
US rows cover treaty routing and net-zero position controls.
"""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import replace
from datetime import date
from decimal import (
    ROUND_HALF_EVEN,
    Clamped,
    Context,
    Decimal,
    DivisionByZero,
    Inexact,
    InvalidOperation,
    Overflow,
    Rounded,
    Subnormal,
    Underflow,
    getcontext,
    localcontext,
)
from fractions import Fraction

import pytest

from ibkr_fa.compute.form67 import (
    Form67ComputeResult,
    Form67Readiness,
    Form67RefusalReason,
    Form67RowReadiness,
    compute_form67_entries_from_wht,
    require_no_refused_form67_rows,
)
from ibkr_fa.compute.schedule_fsi import (
    ScheduleFsiComputeResult,
    require_fsi_relief_matches_portal,
)
from ibkr_fa.compute.schedule_fsi_relief import enrich_fsi_with_form67_relief
from ibkr_fa.compute.schedule_os import ScheduleOsDividendLine
from ibkr_fa.compute.schedule_tr import ScheduleTrComputeResult, compute_schedule_tr_from_fsi
from ibkr_fa.config import Form67Config, Form67Section91Config, Form67TaxPayableOverride
from ibkr_fa.exceptions import ConfigError, InvariantError
from ibkr_fa.models.distribution_kind import DistributionKind
from ibkr_fa.models.form67_entry import Form67SourceOfIncome
from ibkr_fa.models.schedule_fsi_row import HeadOfIncome, ScheduleFsiRow
from ibkr_fa.models.schedule_tr_row import TaxReliefSection
from ibkr_fa.models.withholding_tax import WithholdingTax
from ibkr_fa.reference.country_codes import lookup_country
from ibkr_fa.types import Ticker
from tests._fx_stubs import AuditedFxStub
from tests._section_91_inputs import (
    _DESCRIPTION,
    _EXACT_OTHER,
    _EXACT_PORTAL,
    _EXACT_ZERO,
    _ISIN_BY_ISO,
    _LONG_GROSS,
    _MARKERS,
    _ON,
    _ORDINARY,
    _PREFLIGHT_OTHER,
    _PREFLIGHT_PORTAL,
    _PREFLIGHT_ZERO,
    _PRODUCT,
    _PROOF,
    _READINESS,
    _SYMBOL,
    _assert_refusal,
    _Country,
    _exact_credit,
    _net_zero_us,
    _oracle,
    _wht,
)

_FY_START = date(2025, 4, 1)
_FY_END = date(2026, 3, 31)


def _dividend(country: _Country, fx: str) -> ScheduleOsDividendLine:
    # Exact INR at 60 digits, so a 28-digit gross keeps every digit.
    with localcontext(Context(prec=60)):
        inr = Decimal(country.gross) * Decimal(fx)
    return ScheduleOsDividendLine(
        symbol=Ticker(_SYMBOL),
        isin=_ISIN_BY_ISO[country.iso],
        issuer_country_iso2=country.iso,
        payment_date=_ON,
        fx_specified_date=_ON,
        fx_rate_publication_date=_ON,
        fx_rate_inr_per_usd=Decimal(fx),
        amount_gross_usd=Decimal(country.gross),
        amount_gross_inr=inr,
        description=_DESCRIPTION,
        kind=DistributionKind.CASH_DIVIDEND,
    )


def _config(countries: Sequence[_Country], tax: str, income: str) -> Form67Config:
    return Form67Config(
        tax_payable_overrides=tuple(
            Form67TaxPayableOverride(
                iso_alpha2=country.iso,
                source_of_income=Form67SourceOfIncome.DIVIDEND,
                tax_payable_normal_inr=Decimal(country.payable),
            )
            for country in countries
            if country.payable is not None
        ),
        section_91=Form67Section91Config(
            total_indian_tax_before_91_relief_inr=Decimal(tax),
            total_income_inr=Decimal(income),
        ),
    )


def _compute(
    countries: Sequence[_Country] = (_ORDINARY,),
    *,
    tax: str = "300000",
    income: str = "2800000",
    fx: str = "70",
    extra_wht: Sequence[WithholdingTax] = (),
) -> Form67ComputeResult:
    return compute_form67_entries_from_wht(
        [*extra_wht, *(_wht(country) for country in countries)],
        os_dividend_lines=[_dividend(country, fx) for country in countries],
        wht_fx_policy=AuditedFxStub(Decimal(fx)),
        financial_year_start=_FY_START,
        financial_year_end=_FY_END,
        form67_config=_config(countries, tax, income),
    )


def _fsi_and_tr(
    result: Form67ComputeResult,
) -> tuple[ScheduleFsiComputeResult, ScheduleTrComputeResult]:
    """Enrich one Other Sources row per entry with the real relief join, then
    aggregate Schedule TR."""
    rows = []
    for entry in result.entries:
        rows.append(_fsi_row(entry.iso_alpha2, entry.income_inr, entry.foreign_tax_inr))
    rows.sort(key=lambda row: (row.cbdt_country_code, row.head_of_income.value))
    fsi = enrich_fsi_with_form67_relief(ScheduleFsiComputeResult(rows=rows), result)
    return fsi, compute_schedule_tr_from_fsi(fsi)


def _fsi_row(
    iso: str,
    income: Decimal,
    tax: Decimal,
) -> ScheduleFsiRow:
    country = lookup_country(iso)
    return ScheduleFsiRow(
        cbdt_country_code=country.cbdt_code,
        country_name_portal=country.country_name_portal,
        iso_alpha2=iso,
        head_of_income=HeadOfIncome.OTHER_SOURCES,
        income_inr=income,
        foreign_tax_inr=tax,
        event_count=1,
    )


def _snapshot(context: Context) -> tuple[object, ...]:
    """Everything a caller can observe of its decimal context."""
    return (
        context.prec,
        context.rounding,
        context.Emin,
        context.Emax,
        context.capitals,
        context.clamp,
        tuple(sorted((signal.__name__, bool(on)) for signal, on in context.traps.items())),
        tuple(sorted((signal.__name__, bool(on)) for signal, on in context.flags.items())),
    )


@contextmanager
def _caller_context(**settings: object) -> Iterator[Context]:
    """A caller's context; its settings and flags must be unchanged on exit."""
    with localcontext(**settings) as context:  # ty: ignore[invalid-argument-type]
        before = _snapshot(context)
        try:
            yield context
        finally:
            assert _snapshot(getcontext()) == before


# --- Public regressions --------------------------------------------------------


def test_ordinary_cent_amounts_file_the_exact_two_rupee_credit() -> None:
    """Fails on 1f885b5: the rounded average rate gave 1.4999… and filed ₹1."""
    result = _compute()

    entry = result.entries[0]
    assert entry.income_inr == Decimal("14.00")
    assert entry.foreign_tax_inr == Decimal("2.10")
    assert str(entry.section_91_credit_inr) == "1.5"
    assert entry.total_credit_inr is entry.section_91_credit_inr
    assert entry.filed.section_91_credit_inr == Decimal("2")
    assert entry.filed.total_credit_inr == Decimal("2")
    assert result.row_readiness == (Form67RowReadiness(Form67Readiness.READY),)
    # The Form 67 filing path (readiness enforced) accepts it.
    require_no_refused_form67_rows(result)


def test_product_correction_files_the_correct_one_rupee() -> None:
    """Fails on 1f885b5, which rounded 0.25 x income up to 1.5000… and filed ₹2.

    Deliberate correction: the exact credit is 1.49999999999999999999999999975,
    whose HALF_UP whole rupee is 1.
    """
    result = _compute((_PRODUCT,), tax="0.25", income="1", fx="1")

    entry = result.entries[0]
    assert str(entry.income_inr) == _LONG_GROSS
    assert entry.foreign_tax_inr == Decimal("3")
    assert str(entry.section_91_credit_inr) == "1.499999999999999999999999999"
    assert str(entry.total_credit_inr) == "1.499999999999999999999999999"
    assert entry.filed.section_91_credit_inr == Decimal("1")
    assert entry.filed.total_credit_inr == Decimal("1")
    assert _oracle(_exact_credit(_PRODUCT, tax="0.25", income="1", fx="1")) == 1
    assert result.row_readiness == (Form67RowReadiness(Form67Readiness.READY),)
    require_no_refused_form67_rows(result)


def test_product_correction_newly_fails_the_fsi_portal_rule() -> None:
    """The corrected ₹1 relief isn't min(filed tax 3, filed payable 2) = 2."""
    result = _compute((_PRODUCT,), tax="0.25", income="1", fx="1")
    fsi, _ = _fsi_and_tr(result)

    with pytest.raises(InvariantError) as raised:
        require_fsi_relief_matches_portal(fsi.rows, result)
    _assert_refusal(raised, 1, _EXACT_PORTAL, _MARKERS)


def test_readiness_correction_files_zero_and_refuses_where_readiness_applies() -> None:
    """Deliberate correction: the exact credit 0.49999999999999999999999999998
    files ₹0, where the former formula's 0.5000… filed ₹1."""
    countries = (_READINESS,)
    result = _compute(countries, tax="0.08333333333333333333333333333", income="1", fx="1")

    entry = result.entries[0]
    assert str(entry.section_91_credit_inr) == "0.4999999999999999999999999999"
    assert entry.filed.section_91_credit_inr == Decimal("0")
    # Status, not a compute error: standalone review formats still write it.
    assert result.row_readiness == (
        Form67RowReadiness(Form67Readiness.REFUSED, Form67RefusalReason.ZERO_CREDIT),
    )
    with pytest.raises(InvariantError) as raised:
        require_no_refused_form67_rows(result)
    _assert_refusal(raised, 1, _EXACT_ZERO, _MARKERS)


def test_zero_credit_control_keeps_the_former_readiness_refusal() -> None:
    """100000/2800000 x 14 filed ₹0 before and refused; exact 0.5 files ₹1."""
    result = _compute(tax="100000")

    entry = result.entries[0]
    assert str(entry.section_91_credit_inr) == "0.5"
    assert entry.filed.section_91_credit_inr == Decimal("1")
    assert result.row_readiness == (Form67RowReadiness(Form67Readiness.READY),)
    with pytest.raises(InvariantError) as raised:
        require_no_refused_form67_rows(result)
    _assert_refusal(raised, 1, _PREFLIGHT_ZERO, _MARKERS)


def test_zero_total_tax_computes_zero_and_keeps_its_readiness_refusal() -> None:
    result = _compute(tax="0")

    entry = result.entries[0]
    assert entry.section_91_credit_inr == 0
    assert entry.total_credit_inr is entry.section_91_credit_inr
    assert result.row_readiness == (
        Form67RowReadiness(Form67Readiness.REFUSED, Form67RefusalReason.ZERO_CREDIT),
    )
    with pytest.raises(InvariantError) as raised:
        require_no_refused_form67_rows(result)
    _assert_refusal(raised, 1, _PREFLIGHT_ZERO, _MARKERS)


def test_ordinary_repro_keeps_its_former_fsi_portal_refusal() -> None:
    """The former ₹1 relief wasn't min(2, 3) = 2; the corrected ₹2 is, but an
    FSI or TR check that refused before still refuses."""
    result = _compute()
    fsi, _ = _fsi_and_tr(result)
    assert fsi.rows[0].filed.tax_relief_inr == Decimal("2")
    assert not fsi.rows[0].filed.portal_overwrites_relief

    with pytest.raises(InvariantError) as raised:
        require_fsi_relief_matches_portal(fsi.rows, result)
    _assert_refusal(raised, 1, _PREFLIGHT_PORTAL, _MARKERS)


# --- The exact oracle -----------------------------------------------------------

_LONG_BELOW_TIE = "9.9999999999999999999999999996"
_LONG_ABOVE_TIE = "10.000000000000000000000000004"

# Each case: its id, country, total tax, total income and FX rate.
_ORACLE_CASES = [
    ("cap-terminating-half-tie", _ORDINARY, "300000", "2800000", "70"),
    ("cap-repeating-thirds", _Country(gross="10", wht="-9"), "1", "3", "1"),
    ("cap-repeating-two-thirds", _Country(gross="10", wht="-9"), "2", "3", "1"),
    ("cap-repeating-sevenths", _Country(gross="10", wht="-9"), "300000", "2800000", "1"),
    ("cap-exact-half-tie", _Country(gross="10", wht="-9"), "25", "100", "1"),
    ("cap-just-below-tie", _Country(gross=_LONG_BELOW_TIE, wht="-9"), "1", "4", "1"),
    ("cap-just-above-tie", _Country(gross=_LONG_ABOVE_TIE, wht="-9"), "1", "4", "1"),
    ("cap-repeating-just-below-tie", _Country(gross="7.4999999", wht="-9"), "1", "3", "1"),
    ("cap-zero", _Country(gross="10", wht="-9"), "0", "3", "1"),
    ("foreign-wins", _Country(gross="10", wht="-1.50"), "1", "1", "1"),
    ("foreign-wins-repeating-rate", _Country(gross="10", wht="-0.60"), "1", "3", "1"),
    ("equal", _Country(gross="10", wht="-1.50"), "15", "100", "1"),
    ("equal-repeating-rate", _Country(gross="9", wht="-3"), "1", "3", "1"),
]


@pytest.mark.parametrize(
    ("country", "tax", "income", "fx"),
    [case[1:] for case in _ORACLE_CASES],
    ids=[case[0] for case in _ORACLE_CASES],
)
def test_every_filed_credit_matches_the_exact_oracle(
    country: _Country, tax: str, income: str, fx: str
) -> None:
    """Form 67 cols 12/13, the FSI relief and Schedule TR's relief are the exact
    credit rounded HALF_UP at their existing rounding points."""
    expected = _oracle(_exact_credit(country, tax=tax, income=income, fx=fx))
    result = _compute((country,), tax=tax, income=income, fx=fx)
    fsi, tr = _fsi_and_tr(result)

    entry = result.entries[0]
    assert entry.filed.section_91_credit_inr == expected
    assert entry.filed.total_credit_inr == expected
    assert entry.section_91_credit_inr is not None
    assert entry.section_91_credit_inr <= entry.foreign_tax_inr
    assert fsi.rows[0].tax_relief_inr is entry.total_credit_inr
    assert fsi.rows[0].filed.tax_relief_inr == expected
    # A credit of exactly ₹0 (cap-zero) names no section, so TR leaves it
    # blank; no command writes it, as #601 refuses its Form 67 row (#1491).
    assert [(row.tax_relief_inr_total, row.section_under_which_claimed) for row in tr.rows] == [
        (expected, TaxReliefSection.SECTION_91) if entry.section_91_credit_inr > 0 else (None, None)
    ]


def test_equal_minimum_keeps_the_foreign_tax_itself() -> None:
    result = _compute((_Country(gross="10", wht="-1.50"),), tax="15", income="100", fx="1")

    assert str(result.entries[0].section_91_credit_inr) == "1.50"


@pytest.mark.parametrize("tax_digits", ["1", "7", "300000", "0.08333333333333333333333333333"])
@pytest.mark.parametrize("income", ["3", "7", "2800000"])
@pytest.mark.parametrize("gross", ["0.20", "6", "14.07", _LONG_GROSS])
@pytest.mark.parametrize("wht", ["-0.03", "-1", "-2.49"])
def test_grid_of_terminating_and_repeating_caps_files_the_oracle(
    tax_digits: str, income: str, gross: str, wht: str
) -> None:
    country = _Country(gross=gross, wht=wht)
    expected = _exact_credit(country, tax=tax_digits, income=income, fx="1")
    result = _compute((country,), tax=tax_digits, income=income, fx="1")

    entry = result.entries[0]
    assert entry.filed.section_91_credit_inr == _oracle(expected)
    assert entry.filed.total_credit_inr == _oracle(expected)
    assert entry.section_91_credit_inr is not None
    assert Fraction(entry.section_91_credit_inr) <= expected
    assert (entry.section_91_credit_inr > 0) == (expected > 0)


def test_schedule_tr_sums_the_filed_relief_cells() -> None:
    """Two synthetic BM relief cells of 1.5 file as ₹2 + ₹2 = ₹4, rather than
    rounding their raw sum of 3 to ₹3. KY contributes its separate ₹2 cell."""
    countries = tuple(
        _Country(iso=iso, gross="10", wht="-2.10", payable="2") for iso in ("BM", "KY")
    )
    result = _compute(countries, tax="3", income="20", fx="1")
    fsi, _ = _fsi_and_tr(result)
    # A synthetic second head shares BM's proven credit to exercise aggregation.
    bm = next(row for row in fsi.rows if row.iso_alpha2 == "BM")
    rows = [*fsi.rows, replace(bm, head_of_income=HeadOfIncome.CAPITAL_GAINS)]
    rows.sort(key=lambda row: (row.cbdt_country_code, row.head_of_income.value))
    require_fsi_relief_matches_portal(rows, result)
    tr = compute_schedule_tr_from_fsi(ScheduleFsiComputeResult(rows=rows))

    by_iso = {row.iso_alpha2: row for row in tr.rows}
    exact = _exact_credit(countries[0], tax="3", income="20", fx="1")
    assert exact == Fraction(3, 2)
    assert by_iso["BM"].tax_relief_inr_total == _oracle(exact) + _oracle(exact) == 4
    assert by_iso["BM"].tax_relief_inr_total != _oracle(exact + exact)
    assert by_iso["KY"].tax_relief_inr_total == _oracle(exact)
    assert by_iso["BM"].event_count == 2


# --- Proof boundary ------------------------------------------------------------


def _credit(
    *,
    tax: str,
    income: str,
    gross: str,
    foreign_tax: str,
    position: int = 4,
) -> tuple[Decimal, Decimal]:
    # Imported here so the rest of this file also runs against 1f885b5's code.
    from ibkr_fa.compute.section_91 import exact_section_91_credit  # noqa: PLC0415

    credit, check = exact_section_91_credit(
        total_tax=Decimal(tax),
        total_income=Decimal(income),
        income_inr=Decimal(gross),
        foreign_tax_inr=Decimal(foreign_tax),
        position=position,
    )
    return credit, check.legacy_credit_inr


def test_operand_beyond_the_conversion_cap_refuses_with_the_proof_message() -> None:
    """A total tax of 1E-1001 computes a former ₹0 credit but can't be converted
    within EXACT_SUM_DIGITS places."""
    with _caller_context(), pytest.raises(InvariantError) as raised:
        _credit(tax="1E-1001", income="1", gross="10", foreign_tax="5")
    _assert_refusal(raised, 4, _PROOF)


def test_underflowing_division_refuses_with_the_proof_message() -> None:
    """With Emin -3 at precision 3 the exact cap 2E-6 rounds down to zero, which
    the proof refuses; the former formula underflowed to a ₹0 credit it allowed."""
    with _caller_context(Emin=-3, prec=3), pytest.raises(InvariantError) as raised:
        _credit(tax="1", income="500000", gross="1", foreign_tax="5")
    _assert_refusal(raised, 4, _PROOF)


def test_reduced_precision_that_moves_the_filed_rupee_refuses() -> None:
    """At precision 2 the cap 12.5 divides down to 12, which files ₹12 while the
    exact credit files ₹13."""
    with _caller_context(prec=2), pytest.raises(InvariantError) as raised:
        _credit(tax="1.25", income="1", gross="10", foreign_tax="20")
    _assert_refusal(raised, 4, _PROOF)


@pytest.mark.parametrize(
    ("tax", "income", "gross", "foreign_tax", "expected"),
    [
        ("300000", "2800000", "14.00", "2.10", "1.5"),
        ("1", "3", "10", "9", "3.333333333333333333333333333"),
        ("0.25", "1", _LONG_GROSS, "3", "1.499999999999999999999999999"),
        ("1", "1", "10", "1.50", "1.50"),
    ],
    ids=["terminating-cap", "repeating-cap", "product-correction", "foreign-wins"],
)
def test_proof_holds_for_ordinary_caps(
    tax: str, income: str, gross: str, foreign_tax: str, expected: str
) -> None:
    with _caller_context():
        credit, _ = _credit(tax=tax, income=income, gross=gross, foreign_tax=foreign_tax)
    assert str(credit) == expected


# --- Inherited flags --------------------------------------------------------------


@pytest.mark.parametrize(
    ("country", "tax", "income", "fx", "expected"),
    [
        (_Country(gross="10", wht="-1.50"), "1", "1", "1", "1.50"),
        (_Country(gross="10", wht="-9"), "1", "3", "1", "3.333333333333333333333333333"),
        (_ORDINARY, "300000", "2800000", "70", "1.5"),
        (_PRODUCT, "0.25", "1", "1", "1.499999999999999999999999999"),
    ],
    ids=["foreign-wins", "repeating-cap", "exact-cap", "product-correction"],
)
def test_inherited_inexact_and_rounded_flags_neither_refuse_nor_change(
    country: _Country, tax: str, income: str, fx: str, expected: str
) -> None:
    with _caller_context(flags=[Inexact, Rounded]) as context:
        result = _compute((country,), tax=tax, income=income, fx=fx)
        assert context.flags[Inexact]
        assert context.flags[Rounded]
    assert str(result.entries[0].section_91_credit_inr) == expected


# --- Raw ceiling and gates --------------------------------------------------------


def test_foreign_tax_winning_credit_keeps_its_raw_value_and_files_two_rupees() -> None:
    result = _compute((_Country(gross="10", wht="-1.50"),), tax="1", income="1", fx="1")

    entry = result.entries[0]
    assert str(entry.section_91_credit_inr) == "1.50"
    assert str(entry.total_credit_inr) == "1.50"
    assert entry.section_91_credit_inr is entry.foreign_tax_inr
    assert entry.filed.section_91_credit_inr == Decimal("2")
    assert entry.filed.total_credit_inr == Decimal("2")


@pytest.mark.parametrize(
    ("tax", "income", "gross"),
    [
        ("1", "3", "10"),  # repeating unused Indian rate
        ("1", "7", "1.0000000000000000000000000003"),  # inexact unused cap product
    ],
    ids=["repeating-rate", "inexact-cap-product"],
)
def test_unused_indian_cap_never_refuses(tax: str, income: str, gross: str) -> None:
    result = _compute((_Country(gross=gross, wht="-0.01"),), tax=tax, income=income, fx="1")

    entry = result.entries[0]
    assert entry.section_91_credit_inr is entry.foreign_tax_inr


def test_pr_233_foreign_rate_round_trip_stays_within_foreign_tax() -> None:
    """PR #233's large amounts: min picks foreign tax, which stays exact."""
    country = _Country(gross="64638133", wht="-7056120", payable="1000000")
    result = _compute((country,), tax="3000000", income="10000000", fx="1")

    entry = result.entries[0]
    assert entry.section_91_credit_inr == Decimal("7056120")
    assert entry.total_credit_inr == Decimal("7056120")


@pytest.mark.parametrize(
    ("countries", "section_91"),
    [
        ((_Country(payable=None),), True),  # no col 7 override
        ((_ORDINARY,), False),  # no [form67.section_91]
    ],
    ids=["no-tax-payable", "no-section-91-config"],
)
def test_unmet_gates_leave_cols_12_and_13_blank(
    countries: tuple[_Country, ...], section_91: bool
) -> None:
    config = _config(countries, "300000", "2800000")
    if not section_91:
        config = Form67Config(tax_payable_overrides=config.tax_payable_overrides)
    result = compute_form67_entries_from_wht(
        [_wht(country) for country in countries],
        os_dividend_lines=[_dividend(country, "70") for country in countries],
        wht_fx_policy=AuditedFxStub(Decimal("70")),
        financial_year_start=_FY_START,
        financial_year_end=_FY_END,
        form67_config=config,
    )

    entry = result.entries[0]
    assert entry.section_91_credit_inr is None
    assert entry.total_credit_inr is None
    assert result._section_91_checks == {}


def test_treaty_country_keeps_its_section_90_route() -> None:
    country = _Country(iso="US", gross="10", wht="-1.50", payable="3")
    result = _compute((country,), tax="1", income="3", fx="1")

    entry = result.entries[0]
    assert entry.section_91_credit_inr is None
    assert entry.section_90_credit_inr is not None
    assert result._section_91_checks == {}


# --- Fail closed: the former formula's refusals stay ---------------------------------


def test_former_formula_trapping_inexact_still_refuses() -> None:
    """A caller trapping Inexact refused the rounded 1/3 rate before; the exact
    division would have refused too, but the preflight names it first."""
    traps = [Inexact, InvalidOperation, DivisionByZero, Overflow]
    with _caller_context(traps=traps), pytest.raises(InvariantError) as raised:
        _credit(tax="1", income="3", gross="10", foreign_tax="9")
    _assert_refusal(raised, 4, _PREFLIGHT_OTHER)


def test_former_credit_trapping_whole_rupee_rounding_still_refuses() -> None:
    """The former 1.0 credit traps Rounded when filed; the exact 1 would not."""
    traps = [Rounded, InvalidOperation, DivisionByZero, Overflow]
    with _caller_context(traps=traps), pytest.raises(InvariantError) as raised:
        _credit(tax="1", income="2", gross="2", foreign_tax="9")
    _assert_refusal(raised, 4, _PREFLIGHT_OTHER)


def test_former_formula_overflowing_rate_still_refuses() -> None:
    """With Emax 6 the former rate 1E+5 / 1E-2 overflowed; the exact cap 1E+4
    would fit, so only the preflight keeps the refusal."""
    with _caller_context(Emax=6), pytest.raises(InvariantError) as raised:
        _credit(tax="1E+5", income="1E-2", gross="1E-3", foreign_tax="99999")
    _assert_refusal(raised, 4, _PREFLIGHT_OTHER)


def test_former_credit_beyond_the_filed_bound_still_refuses() -> None:
    """Foreign tax past the CBDT bound wins the minimum; the former credit failed
    Form67Entry's filed-amount bound before anything else."""
    country = _Country(gross="99999999999999", wht="-100000000000000", payable="1")
    with pytest.raises(InvariantError) as raised:
        _compute((country,), tax="2", income="1", fx="1")
    _assert_refusal(raised, 1, _PREFLIGHT_OTHER, _MARKERS)


def test_exact_credit_reaching_the_filed_bound_refuses() -> None:
    """4/3 x 74999999999999.625 is exactly 99999999999999.5, which can't file
    within the CBDT bound; the former 28-digit rate gave 99999999999999.4999…"""
    with _caller_context(), pytest.raises(InvariantError) as raised:
        _credit(tax="4", income="3", gross="74999999999999.625", foreign_tax="99999999999999.6")
    _assert_refusal(raised, 4, _EXACT_OTHER)


def test_negative_total_tax_refuses_with_the_preflight_message() -> None:
    with _caller_context(), pytest.raises(InvariantError) as raised:
        _credit(tax="-1", income="3", gross="10", foreign_tax="9")
    _assert_refusal(raised, 4, _PREFLIGHT_OTHER)


@pytest.mark.parametrize("signals", [[Clamped], [Subnormal, Underflow]])
def test_unrelated_inherited_flags_are_preserved_on_failure(signals: list[type]) -> None:
    with _caller_context(flags=signals, prec=2), pytest.raises(InvariantError) as raised:
        _credit(tax="1.25", income="1", gross="10", foreign_tax="20")
    _assert_refusal(raised, 4, _PROOF)


def test_caller_rounding_mode_is_used_by_the_preflight_and_kept() -> None:
    with _caller_context(rounding=ROUND_HALF_EVEN, prec=28):
        credit, legacy = _credit(tax="1", income="3", gross="10", foreign_tax="9")
    assert str(credit) == "3.333333333333333333333333333"
    assert str(legacy) == "3.333333333333333333333333333"


# --- Positions --------------------------------------------------------------------


def test_position_counts_a_skipped_country_and_not_the_serial() -> None:
    """Input order US (netted to zero, skipped), BM, KY; KY sorts first, so BM
    is serial 2 but country input row 2, and KY serial 1 but row 3."""
    countries = (_ORDINARY, _Country(iso="KY", gross="10", wht="-9"))
    result = _compute(countries, tax="100000", extra_wht=_net_zero_us())

    assert [entry.iso_alpha2 for entry in result.entries] == ["KY", "BM"]
    assert {iso: check.position for iso, check in result._section_91_checks.items()} == {
        "BM": 2,
        "KY": 3,
    }
    with pytest.raises(InvariantError) as raised:
        require_no_refused_form67_rows(result)
    _assert_refusal(raised, 2, _PREFLIGHT_ZERO, _MARKERS)


def test_several_corrected_failures_name_the_first_country_input_row() -> None:
    """KY then BM both newly file ₹0; KY is input row 1 though it sorts second
    behind VG."""
    countries = (
        _Country(iso="KY", gross="6", wht="-1", payable="1"),
        _Country(iso="BM", gross="6", wht="-1", payable="1"),
        _Country(iso="VG", gross="6", wht="-1", payable="1"),
    )
    result = _compute(countries, tax="0.08333333333333333333333333333", income="1", fx="1")

    assert [entry.iso_alpha2 for entry in result.entries] == ["VG", "KY", "BM"]
    with pytest.raises(InvariantError) as raised:
        require_no_refused_form67_rows(result)
    _assert_refusal(raised, 1, _EXACT_ZERO, _MARKERS)
    fsi, _ = _fsi_and_tr(result)
    with pytest.raises(InvariantError) as raised_portal:
        require_fsi_relief_matches_portal(fsi.rows, result)
    _assert_refusal(raised_portal, 1, _EXACT_PORTAL, _MARKERS)


def test_preflight_refusal_wins_over_a_corrected_one_at_an_earlier_row() -> None:
    """Row 1 (KY) only newly fails the portal rule (₹1 against min(3, 2)); row 2
    (BM) failed it before (₹2 against min(3, 1)). The preflight runs first, so it
    names row 2."""
    countries = (
        _Country(iso="KY", gross=_LONG_GROSS, wht="-3", payable="2"),
        _Country(iso="BM", gross=_LONG_GROSS, wht="-3", payable="1"),
    )
    result = _compute(countries, tax="0.25", income="1", fx="1")
    fsi, _ = _fsi_and_tr(result)

    with pytest.raises(InvariantError) as raised:
        require_fsi_relief_matches_portal(fsi.rows, result)
    _assert_refusal(raised, 2, _PREFLIGHT_PORTAL, _MARKERS)


def test_unrelated_refusal_keeps_its_config_error_before_a_corrected_one() -> None:
    """A treaty row whose Indian tax is ₹0 keeps today's ConfigError, which no
    longer lists the section 91 row; the corrected refusal only follows it."""
    countries = (_READINESS, _Country(iso="US", gross="10", wht="-1.50", payable="0"))
    result = _compute(countries, tax="0.08333333333333333333333333333", income="1", fx="1")

    with pytest.raises(ConfigError) as raised:
        require_no_refused_form67_rows(result)
    message = str(raised.value)
    assert "US/DIVIDEND: the computed credit rounds to ₹0" in message
    assert "BM/DIVIDEND" not in message
