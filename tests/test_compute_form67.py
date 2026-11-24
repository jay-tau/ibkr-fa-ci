"""Tests for :mod:`ibkr_fa.compute.form67`."""

from __future__ import annotations

from dataclasses import replace
from datetime import date
from decimal import Context, Decimal, localcontext

import pytest

from ibkr_fa.compute.form67 import (
    Form67ComputeResult,
    Form67Readiness,
    Form67RefusalReason,
    Form67RowReadiness,
    compute_form67_entries_from_wht,
    form67_row_readiness,
    require_no_refused_form67_rows,
)
from ibkr_fa.compute.schedule_fsi import compute_schedule_fsi_from_withholding_tax
from ibkr_fa.compute.schedule_os import ScheduleOsDividendLine
from ibkr_fa.compute.wht_reversal_pairing import (
    UnprovenWhtReversalError,
    WhtCrossFyReversalReport,
)
from ibkr_fa.config import (
    Form67Config,
    Form67Section91Config,
    Form67TaxPayableOverride,
    ScheduleOsIndianAdrRoutingMode,
)
from ibkr_fa.exceptions import ConfigError, InvariantError
from ibkr_fa.fx.policy import AuditedFxPolicy
from ibkr_fa.models.form67_entry import (
    Form67Entry,
    Form67SourceOfIncome,
    Form67TreatyRateStatus,
)
from ibkr_fa.models.withholding_tax import WithholdingTax
from ibkr_fa.types import Isin, Ticker
from tests._foreign_tax_refusals import (
    NO_ORIGINAL,
    TWO_FY_CANDIDATES,
    assert_no_wht_echo,
    unproven_reversal_message,
)
from tests._form67_compute_inputs import DEFAULT_ISIN_US as _DEFAULT_ISIN_US
from tests._form67_compute_inputs import DEFAULT_PAYMENT_DATE as _DEFAULT_PAYMENT_DATE
from tests._form67_compute_inputs import DEFAULT_TICKER_XQBN as _DEFAULT_TICKER_XQBN
from tests._form67_compute_inputs import E9_GROSS as _E9_GROSS
from tests._form67_compute_inputs import E9_TAX as _E9_TAX
from tests._form67_compute_inputs import E9_WHT as _E9_WHT
from tests._form67_compute_inputs import E9A_LINE as _E9A_LINE
from tests._form67_compute_inputs import FEB_12 as _FEB_12
from tests._form67_compute_inputs import FORM67_FX_RATE
from tests._form67_compute_inputs import FY_END as _FY_END
from tests._form67_compute_inputs import FY_START as _FY_START
from tests._form67_compute_inputs import JUN_12 as _JUN_12
from tests._form67_compute_inputs import JUN_RATE as _JUN_RATE
from tests._form67_compute_inputs import MAR_12 as _MAR_12
from tests._form67_compute_inputs import (
    REFUND_BESIDE_ABOVE_RATE_PAYMENTS as _REFUND_BESIDE_ABOVE_RATE_PAYMENTS,
)
from tests._form67_compute_inputs import UNPROVEN_SOURCE_RATE_CASES as _UNPROVEN_SOURCE_RATE_CASES
from tests._form67_compute_inputs import os_dividend_line as _os_line
from tests._form67_compute_inputs import withholding_row as _wht
from tests._ftc_warnings import (
    SAME_DAY_GAP_CAUSE,
    SOURCE_RATE_GAP_CAUSE,
    without_override,
    zero_credit_text,
)
from tests._fx_stubs import AuditedFxStub, DateAwareRatePolicy, FixedRatePolicy

_DEFAULT_ISIN_GB: Isin = Isin("GB99970J1065")
_DEFAULT_TICKER_XQCR: Ticker = Ticker("XQCR")


# ---- happy path -----------------------------------------------------------


def test_empty_inputs_yield_empty_result() -> None:
    result = compute_form67_entries_from_wht(
        [],
        os_dividend_lines=[],
        wht_fx_policy=FixedRatePolicy(FORM67_FX_RATE),
        financial_year_start=_FY_START,
        financial_year_end=_FY_END,
    )
    assert result.entries == ()


def test_single_country_aggregates_to_one_entry() -> None:
    """The headline behaviour: 4 WHT events from the US aggregate to
    ONE Form 67 row (NOT four rows per the closed PR #220 design)."""
    wht_list = [
        _wht(payment_date=date(2025, 6, 15), amount=Decimal("-1.50")),
        _wht(payment_date=date(2025, 9, 15), amount=Decimal("-2.00")),
        _wht(payment_date=date(2025, 12, 15), amount=Decimal("-2.50")),
        _wht(payment_date=date(2026, 3, 15), amount=Decimal("-1.80")),
    ]
    os_list = [
        _os_line(payment_date=date(2025, 6, 15), amount_gross_inr=Decimal("500")),
        _os_line(payment_date=date(2025, 9, 15), amount_gross_inr=Decimal("700")),
        _os_line(payment_date=date(2025, 12, 15), amount_gross_inr=Decimal("900")),
        _os_line(payment_date=date(2026, 3, 15), amount_gross_inr=Decimal("600")),
    ]
    result = compute_form67_entries_from_wht(
        wht_list,
        os_dividend_lines=os_list,
        wht_fx_policy=FixedRatePolicy(FORM67_FX_RATE),
        financial_year_start=_FY_START,
        financial_year_end=_FY_END,
    )
    assert len(result.entries) == 1
    e = result.entries[0]
    assert e.serial_number == 1
    assert e.cbdt_country_code == "2"  # US
    assert e.iso_alpha2 == "US"
    assert e.source_of_income is Form67SourceOfIncome.DIVIDEND
    # income aggregated: 500 + 700 + 900 + 600 = 2700
    assert e.income_inr == Decimal("2700")
    # foreign_tax aggregated: (1.50 + 2.00 + 2.50 + 1.80) * FORM67_FX_RATE
    expected_tax = Decimal("7.80") * FORM67_FX_RATE
    assert e.foreign_tax_inr == expected_tax
    # event_count records the aggregation cardinality (audit cell).
    assert e.event_count == 4


def test_multi_country_yields_one_entry_per_country() -> None:
    """Two countries (US + GB) → two entries, sorted by CBDT code."""
    wht_list = [
        _wht(
            isin=_DEFAULT_ISIN_GB,
            symbol=_DEFAULT_TICKER_XQCR,
            description="XQCR - UK Tax",
            payment_date=date(2025, 12, 15),
            amount=Decimal("-0.75"),
        ),
        _wht(payment_date=date(2025, 12, 15)),
    ]
    os_list = [
        _os_line(),
        _os_line(
            isin=_DEFAULT_ISIN_GB,
            symbol=_DEFAULT_TICKER_XQCR,
            description="XQCR",
            issuer_country_iso2="GB",
            payment_date=date(2025, 12, 15),
            amount_gross_inr=Decimal("500"),
        ),
    ]
    result = compute_form67_entries_from_wht(
        wht_list,
        os_dividend_lines=os_list,
        wht_fx_policy=FixedRatePolicy(FORM67_FX_RATE),
        financial_year_start=_FY_START,
        financial_year_end=_FY_END,
    )
    assert len(result.entries) == 2
    # Lexicographic CBDT sort: "2" (US) < "44" (GB).
    assert result.entries[0].cbdt_country_code == "2"
    assert result.entries[0].iso_alpha2 == "US"
    assert result.entries[1].cbdt_country_code == "44"
    assert result.entries[1].iso_alpha2 == "GB"
    assert [e.serial_number for e in result.entries] == [1, 2]


def test_effective_rate_percent_derived_correctly() -> None:
    """The INR audit ratio and source-currency withholding rate are separate."""
    result = compute_form67_entries_from_wht(
        [_wht(amount=Decimal("-3.00"))],  # USD 3.00 tax
        os_dividend_lines=[
            _os_line(amount_gross_inr=Decimal("1000"))  # INR 1000 income
        ],
        wht_fx_policy=FixedRatePolicy(rate=Decimal("100")),  # 1 USD = 100 INR
        financial_year_start=_FY_START,
        financial_year_end=_FY_END,
    )
    e = result.entries[0]
    # tax_inr = 3.00 * 100 = 300; income = 1000; rate = 30%
    assert e.foreign_tax_inr == Decimal("300")
    assert e.income_inr == Decimal("1000")
    assert e.foreign_tax_rate_percent == Decimal("30")
    assert e.tax_paid_rate_percent == Decimal("25")


def test_source_cent_rounding_recovers_evidenced_treaty_rate() -> None:
    result = compute_form67_entries_from_wht(
        [_wht(amount=Decimal("-0.37"))],
        os_dividend_lines=[
            _os_line(
                amount_gross_usd=Decimal("1.46"),
                amount_gross_inr=Decimal("132.2176"),
                fx_rate_inr_per_usd=Decimal("90.56"),
            )
        ],
        wht_fx_policy=FixedRatePolicy(rate=Decimal("90.56")),
        financial_year_start=_FY_START,
        financial_year_end=_FY_END,
    )
    entry = result.entries[0]
    assert entry.foreign_tax_rate_percent > Decimal("25")
    assert entry.tax_paid_rate_percent == Decimal("25")


# ---- FY filtering ---------------------------------------------------------


def test_out_of_fy_wht_silently_skipped() -> None:
    result = compute_form67_entries_from_wht(
        [_wht(payment_date=date(2025, 3, 31))],  # before FY start
        os_dividend_lines=[],
        wht_fx_policy=FixedRatePolicy(FORM67_FX_RATE),
        financial_year_start=_FY_START,
        financial_year_end=_FY_END,
    )
    assert result.entries == ()


def test_fy_start_boundary_inclusive() -> None:
    result = compute_form67_entries_from_wht(
        [_wht(payment_date=_FY_START)],
        os_dividend_lines=[_os_line(payment_date=_FY_START)],
        wht_fx_policy=FixedRatePolicy(FORM67_FX_RATE),
        financial_year_start=_FY_START,
        financial_year_end=_FY_END,
    )
    assert len(result.entries) == 1


def test_fy_end_boundary_inclusive() -> None:
    result = compute_form67_entries_from_wht(
        [_wht(payment_date=_FY_END)],
        os_dividend_lines=[_os_line(payment_date=_FY_END)],
        wht_fx_policy=FixedRatePolicy(FORM67_FX_RATE),
        financial_year_start=_FY_START,
        financial_year_end=_FY_END,
    )
    assert len(result.entries) == 1


# ---- refund / reversal netting (per-country) ----------------------------


@pytest.mark.parametrize("col_7", [None, "100000"])
@pytest.mark.parametrize(
    "refund",
    [
        pytest.param(Decimal("0.50"), id="partial-refund"),
        pytest.param(Decimal("2.00"), id="refund-above-paid"),
    ],
)
def test_refund_without_an_equal_original_refuses(refund: Decimal, col_7: str | None) -> None:
    """A refund unequal to the -1.00 paid has no proven original, so it
    refuses before any entry, with or without col 7 (#893). The refusal
    doesn't print the refund's ISIN, amount, currency or date (#1047)."""
    rows = [
        _wht(amount=Decimal("-1.00"), payment_date=_JUN_12),
        _wht(amount=refund, payment_date=date(2025, 9, 15)),
    ]
    with pytest.raises(UnprovenWhtReversalError) as excinfo:
        _compute_payments(
            rows,
            [_os_line(payment_date=_JUN_12)],
            fx_policy=FixedRatePolicy(FORM67_FX_RATE),
            col_7=col_7,
        )
    assert str(excinfo.value) == unproven_reversal_message(NO_ORIGINAL)
    assert_no_wht_echo(str(excinfo.value), rows)


# ---- orphan WHT (HARD FAIL per T5 codex r4 P1 of closed PR #220) ---------


def test_paid_wht_with_no_matching_os_income_hard_fails() -> None:
    """T5 codex r4 P1 of closed PR #220: silently emitting a Form 67
    entry with zero income would under-report the claim and force a
    manual portal edit. Must hard-fail so the user investigates."""
    with pytest.raises(InvariantError, match="ZERO matching foreign-source dividend"):
        compute_form67_entries_from_wht(
            [_wht()],
            os_dividend_lines=[],  # NO matching dividend
            wht_fx_policy=FixedRatePolicy(FORM67_FX_RATE),
            financial_year_start=_FY_START,
            financial_year_end=_FY_END,
        )


def test_paid_wht_country_with_only_refund_dividends_hard_fails() -> None:
    """A country with positive WHT but no OS dividend line (here the
    only line is another country's) has zero income — hard-fail. An
    in-FY return of capital used to be filtered out at the OS layer
    this way; it has refused there since #1310."""
    with pytest.raises(InvariantError, match="ZERO matching foreign-source dividend"):
        compute_form67_entries_from_wht(
            [_wht()],
            # OS line for a DIFFERENT country (no US income).
            os_dividend_lines=[
                _os_line(
                    isin=_DEFAULT_ISIN_GB,
                    issuer_country_iso2="GB",
                    symbol=_DEFAULT_TICKER_XQCR,
                    description="XQCR",
                )
            ],
            wht_fx_policy=FixedRatePolicy(FORM67_FX_RATE),
            financial_year_start=_FY_START,
            financial_year_end=_FY_END,
        )


# ---- currency invariants -------------------------------------------------


def test_non_usd_policy_rejected_in_v1() -> None:
    """T5 codex r4 P2 of closed PR #220: the policy itself must be
    USD-denominated. An EUR policy + EUR WHT would otherwise bypass
    the per-row currency match check."""

    class _EurPolicy:
        currency = "EUR"

        def rate_for(self, event_date: date) -> Decimal:
            del event_date
            return Decimal("90")

    with pytest.raises(InvariantError, match="USD-denominated FX policy"):
        compute_form67_entries_from_wht(
            [],
            os_dividend_lines=[],
            wht_fx_policy=_EurPolicy(),
            financial_year_start=_FY_START,
            financial_year_end=_FY_END,
        )


def test_wht_row_currency_must_match_policy_currency() -> None:
    with pytest.raises(InvariantError, match="multi-currency"):
        compute_form67_entries_from_wht(
            [_wht(currency="EUR")],  # EUR row + USD policy
            os_dividend_lines=[_os_line()],
            wht_fx_policy=FixedRatePolicy(FORM67_FX_RATE),
            financial_year_start=_FY_START,
            financial_year_end=_FY_END,
        )


# ---- audited FX policy --------------------------------------------------


def test_audited_policy_uses_carry_back_aware_rate() -> None:
    """An audited policy, as production passes, converts through
    ``rate_for``. Production's Rule128Policy inherits
    ``Rule115Policy.rate_for``, which returns
    ``rate_for_audited(...).rate``, so the rate stays carry-back-aware.

    The isinstance pin keeps the stub an AuditedFxPolicy like
    production's; the stub returns the same rate from both methods."""
    audited_policy = AuditedFxStub(Decimal("90"))
    assert isinstance(audited_policy, AuditedFxPolicy)
    result = compute_form67_entries_from_wht(
        [_wht(amount=Decimal("-1.00"))],
        os_dividend_lines=[_os_line(amount_gross_inr=Decimal("100"))],
        wht_fx_policy=audited_policy,
        financial_year_start=_FY_START,
        financial_year_end=_FY_END,
    )
    assert len(result.entries) == 1
    # tax = 1.00 * 90 = 90; income = 100; rate = 90%
    assert result.entries[0].foreign_tax_inr == Decimal("90")
    assert result.entries[0].foreign_tax_rate_percent == Decimal("90")


# ---- result invariants --------------------------------------------------


def test_result_rejects_non_sequential_serial_numbers() -> None:
    e1 = Form67Entry(
        serial_number=1,
        cbdt_country_code="2",
        country_name_portal="UNITED STATES OF AMERICA",
        iso_alpha2="US",
        source_of_income=Form67SourceOfIncome.DIVIDEND,
        income_inr=Decimal("1000"),
        foreign_tax_inr=Decimal("250"),
        foreign_tax_rate_percent=Decimal("25"),
        event_count=1,
    )
    e2 = Form67Entry(
        serial_number=3,  # skips 2
        cbdt_country_code="44",
        country_name_portal="UNITED KINGDOM",
        iso_alpha2="GB",
        source_of_income=Form67SourceOfIncome.DIVIDEND,
        income_inr=Decimal("500"),
        foreign_tax_inr=Decimal("75"),
        foreign_tax_rate_percent=Decimal("15"),
        event_count=1,
    )
    with pytest.raises(InvariantError, match="serial_number"):
        Form67ComputeResult(entries=(e1, e2))


def test_result_rejects_unsorted_entries() -> None:
    e_gb = Form67Entry(
        serial_number=1,
        cbdt_country_code="44",
        country_name_portal="UNITED KINGDOM",
        iso_alpha2="GB",
        source_of_income=Form67SourceOfIncome.DIVIDEND,
        income_inr=Decimal("500"),
        foreign_tax_inr=Decimal("75"),
        foreign_tax_rate_percent=Decimal("15"),
        event_count=1,
    )
    e_us = Form67Entry(
        serial_number=2,
        cbdt_country_code="2",
        country_name_portal="UNITED STATES OF AMERICA",
        iso_alpha2="US",
        source_of_income=Form67SourceOfIncome.DIVIDEND,
        income_inr=Decimal("1000"),
        foreign_tax_inr=Decimal("250"),
        foreign_tax_rate_percent=Decimal("25"),
        event_count=1,
    )
    with pytest.raises(InvariantError, match="sorted"):
        Form67ComputeResult(entries=(e_gb, e_us))  # GB before US violates lexicographic


# ---- regression: T3 r1 P3 duplicate aggregate keys (PR #221) ------------


def test_result_rejects_duplicate_aggregate_keys() -> None:
    """T3 r1 P3 of PR #221: directly-constructed result with two
    entries sharing the same (cbdt_country_code, source_of_income)
    must fail — Form 67 is per-aggregate by definition."""
    e1 = Form67Entry(
        serial_number=1,
        cbdt_country_code="2",
        country_name_portal="UNITED STATES OF AMERICA",
        iso_alpha2="US",
        source_of_income=Form67SourceOfIncome.DIVIDEND,
        income_inr=Decimal("1000"),
        foreign_tax_inr=Decimal("250"),
        foreign_tax_rate_percent=Decimal("25"),
        event_count=1,
    )
    e2 = Form67Entry(
        serial_number=2,
        cbdt_country_code="2",  # SAME country
        country_name_portal="UNITED STATES OF AMERICA",
        iso_alpha2="US",
        source_of_income=Form67SourceOfIncome.DIVIDEND,  # SAME source
        income_inr=Decimal("500"),
        foreign_tax_inr=Decimal("125"),
        foreign_tax_rate_percent=Decimal("25"),
        event_count=1,
    )
    with pytest.raises(InvariantError, match=r"unique on \(cbdt_country_code, source_of_income\)"):
        Form67ComputeResult(entries=(e1, e2))


# ---- regression: T4 r1 P2 income keys by issuer_country_iso2 (PR #221) ----


def test_income_aggregation_uses_issuer_country_iso2_not_isin_prefix() -> None:
    """T4 r1 P2 of PR #221: Form 67 income aggregation must use
    ScheduleOsDividendLine.issuer_country_iso2 (matching Schedule
    FSI's income key) not isin[:2]. For an ADR where the depositary
    differs from the underlying issuer (e.g., UK ADR on US exchange
    has isin[:2]='US' but issuer_country_iso2='GB'), the income
    should attribute to the underlying jurisdiction. WHT continues
    to use isin[:2] (depositary) since that's where withholding
    occurred."""
    # OS line: GB-issuer ADR on US exchange (ISIN US-prefix, issuer GB).
    # When Form 67 uses issuer_country_iso2 for income, this dividend
    # attributes to GB. The WHT (US-prefix ISIN) attributes to US.
    # Result: US has WHT but no matching income → orphan WHT hard-fail
    # (this is the correct behaviour — Form 67 should NOT silently
    # mis-attribute the income to US just because the depositary is US).
    os_lines = [
        _os_line(
            isin=Isin("US99971U1003"),  # US-prefix depositary
            issuer_country_iso2="GB",  # underlying GB issuer
            amount_gross_inr=Decimal("1000"),
        )
    ]
    wht_list = [
        _wht(
            isin=Isin("US99971U1003"),
            amount=Decimal("-1.50"),
        )
    ]
    with pytest.raises(InvariantError, match="ZERO matching foreign-source dividend"):
        compute_form67_entries_from_wht(
            wht_list,
            os_dividend_lines=os_lines,
            wht_fx_policy=FixedRatePolicy(FORM67_FX_RATE),
            financial_year_start=_FY_START,
            financial_year_end=_FY_END,
        )


def test_orphan_wht_error_message_mentions_structural_225_handling() -> None:
    """When the orphan-WHT hard-fail trips, the error message must:

    - Cite Rule 128(5)(i) (the FTC-eligibility ground) so users
      understand WHY the guard exists.
    - Enumerate the three common production causes (IBKR
      mis-classification of the underlying instrument, downstream
      OS-row filtering, source statement missing the Dividends
      section) so the user has concrete diagnostic vectors.
    - Note that issue #225 (Indian-ADR mis-routing under
      ``adr_routing_mode=UNDERLYING_ISSUER``) is now handled
      STRUCTURALLY upstream — the orphan branch can no longer be
      reached by that production path. This prevents users from
      chasing the obsolete "revert to DEPOSITARY" workaround.

    Note: this orphan can still be exercised at the form67 layer by
    constructing a foreign ``ScheduleOsDividendLine`` with a
    deliberately non-matching ``issuer_country_iso2`` (e.g., for
    other future ADR mis-classification cases not covered by the
    ``INDIAN_ADR_MAPPING``). The message itself is what we pin here.
    """
    os_lines = [
        _os_line(
            isin=Isin("US99971U1003"),
            issuer_country_iso2="GB",
            amount_gross_inr=Decimal("1000"),
        )
    ]
    wht_list = [
        _wht(
            isin=Isin("US99971U1003"),
            amount=Decimal("-1.50"),
        )
    ]
    with pytest.raises(InvariantError) as exc_info:
        compute_form67_entries_from_wht(
            wht_list,
            os_dividend_lines=os_lines,
            wht_fx_policy=FixedRatePolicy(FORM67_FX_RATE),
            financial_year_start=_FY_START,
            financial_year_end=_FY_END,
        )
    msg = str(exc_info.value)
    # Rule citation present so users know WHY the guard fires.
    assert "Rule 128(5)(i)" in msg
    # The three common causes (a)/(b)/(c) enumerated for diagnosis.
    assert "IBKR mis-classification" in msg
    assert "OS-row filtering" in msg
    assert "Dividends section" in msg
    # #1374: a non-ordinary distribution refuses before Form 67 (#1310).
    assert "non-ordinary" not in msg
    # Issue #225 is now structural — note in the message that
    # the ADR-mis-routing branch cannot reach this guard. This
    # steers users away from the obsolete DEPOSITARY revert advice.
    assert "#225" in msg
    assert "UNDERLYING_ISSUER" in msg
    assert "structurally" in msg or "skipped" in msg


def test_non_adr_income_keys_match_isin_prefix_unchanged() -> None:
    """For typical non-ADR securities (US issuer on US exchange,
    issuer_country_iso2 == isin[:2] == 'US'), the income aggregation
    is unchanged by the T4 r1 P2 fix."""
    result = compute_form67_entries_from_wht(
        [_wht()],
        os_dividend_lines=[_os_line()],  # issuer_country_iso2="US", isin[:2]="US"
        wht_fx_policy=FixedRatePolicy(FORM67_FX_RATE),
        financial_year_start=_FY_START,
        financial_year_end=_FY_END,
    )
    assert len(result.entries) == 1
    assert result.entries[0].iso_alpha2 == "US"
    assert result.entries[0].income_inr == Decimal("1000.00")


# ============================================================================
# Step 4b/N of #37: DTAA dividend reference wiring (cols 9 + 10)
# ============================================================================
#
# The compute layer populates Form67Entry.section_90_dtaa_article (col 9) and
# Form67Entry.section_90_dtaa_rate_percent (col 10) from the vendored DTAA
# reference table at ibkr_fa.reference.dtaa_dividend per the tri-state
# lookup contract:
#
#   * DTAA-vendored country (e.g. US): both fields populate.
#   * NO_DTAA-vendored country (e.g. KY): both fields stay None.
#   * Unvendored country (e.g. AU, which is in country_codes.csv but not
#     yet vendored in dtaa_dividend_rates.csv): both fields stay None
#     (the explicit "unknown" branch — must NOT default to §91).
#
# Tests here that supply no [form67] config also see cols 7, 11, 12 and
# 13 stay None: col 7 needs an exact [[form67.tax_payable_overrides]]
# entry, and every credit needs col 7. Col 8 is never computed.


def test_dtaa_known_country_populates_cols_9_and_10() -> None:
    """US is vendored as DTAA with Article 10(2)(b) (withholding) +
    Article 25(2)(a) (relief) + 25% portfolio rate (per the IRS-
    hosted India treaty PDF verbatim verification at PR #230).
    Cols 9 (WITHHOLDING article) and 10 (treaty cap rate) must
    populate verbatim from the vendored entry.

    Col 9 carries the WITHHOLDING / distributive article (Article
    10(2)(b)), NOT the relief article (Article 25(2)(a)). Col 9 sits
    immediately beside col 10 "Rate of tax as per DTAA (%)", and only
    a distributive article states a rate: Article 10(2)(b) caps
    portfolio dividends at 25%, while Article 25 (Relief from Double
    Taxation) fixes no percentage at all. The form's own structure
    confirms it — the parallel "Relief claimed under section 91" block
    has a rate column but NO article column, because there is no
    treaty to cite. CBDT's ITR guidance points the same way,
    instructing filers to cite the article for the INCOME TYPE
    (Article 11 for interest, hence Article 10 for dividends).

    Beware the India-US coincidence that hid this: the relief article
    number (25) equals the portfolio dividend rate (25%), so citing
    the wrong article still "looks right" beside the rate."""
    result = compute_form67_entries_from_wht(
        [_wht()],
        os_dividend_lines=[_os_line()],
        wht_fx_policy=FixedRatePolicy(FORM67_FX_RATE),
        financial_year_start=_FY_START,
        financial_year_end=_FY_END,
    )
    assert len(result.entries) == 1
    e = result.entries[0]
    assert e.iso_alpha2 == "US"
    # Col 9: WITHHOLDING article per India-US DTAA Article 10
    # (Dividends), paragraph (2)(b) — the clause that supplies the
    # 25% cap in col 10.
    assert e.section_90_dtaa_article == "Article 10(2)(b)"
    # Col 10: portfolio dividend cap rate per India-US DTAA Article
    # 10(2)(b). 25 (not 15) — the 15% rate in 10(2)(a) applies only
    # to corporate beneficial owners holding >= 10% voting stock,
    # never to portfolio Indian individuals via IBKR.
    assert e.section_90_dtaa_rate_percent == Decimal("25")


def test_dtaa_known_country_leaves_indian_tax_and_credit_blank_without_override() -> None:
    result = compute_form67_entries_from_wht(
        [_wht()],
        os_dividend_lines=[_os_line()],
        wht_fx_policy=FixedRatePolicy(FORM67_FX_RATE),
        financial_year_start=_FY_START,
        financial_year_end=_FY_END,
    )
    e = result.entries[0]
    assert e.tax_payable_normal_inr is None
    assert e.tax_payable_special_inr is None
    assert e.treaty_rate_status is Form67TreatyRateStatus.WITHIN
    assert e.eligible_foreign_tax_inr == e.foreign_tax_inr
    assert e.section_90_credit_inr is None
    assert e.section_91_credit_inr is None
    assert e.total_credit_inr is None


def test_genuine_over_withholding_stays_blank_without_exact_indian_tax() -> None:
    """Rule 128(5)(i) proviso: foreign tax above the DTAA rate is ignored.

    USD 4.00 withheld on USD 12.00 is 33.3%, far outside the half-cent
    rounding tolerance around the 25% US treaty rate, so the excess is a real
    over-levy and must not be credited.
    """
    result = compute_form67_entries_from_wht(
        [_wht(amount=Decimal("-4.00"))],
        os_dividend_lines=[_os_line()],
        wht_fx_policy=FixedRatePolicy(FORM67_FX_RATE),
        financial_year_start=_FY_START,
        financial_year_end=_FY_END,
    )
    entry = result.entries[0]
    assert entry.tax_paid_rate_percent is not None
    assert entry.section_90_dtaa_rate_percent is not None
    assert entry.tax_paid_rate_percent > entry.section_90_dtaa_rate_percent
    assert entry.treaty_rate_status is Form67TreatyRateStatus.ABOVE
    assert entry.tax_payable_normal_inr is None
    assert entry.eligible_foreign_tax_inr == min(
        entry.foreign_tax_inr,
        (entry.section_90_dtaa_rate_percent / Decimal("100")) * entry.income_inr,
    )
    assert entry.section_90_credit_inr is None
    assert entry.total_credit_inr is None


def test_genuine_over_withholding_with_exact_tax_retains_legacy_cap() -> None:
    result = compute_form67_entries_from_wht(
        [_wht(amount=Decimal("-4.00"))],
        os_dividend_lines=[_os_line()],
        wht_fx_policy=FixedRatePolicy(FORM67_FX_RATE),
        financial_year_start=_FY_START,
        financial_year_end=_FY_END,
        form67_config=Form67Config(
            tax_payable_overrides=(
                Form67TaxPayableOverride(
                    iso_alpha2="US",
                    source_of_income=Form67SourceOfIncome.DIVIDEND,
                    tax_payable_normal_inr=Decimal("9999"),
                ),
            ),
        ),
    )

    entry = result.entries[0]
    assert entry.section_90_dtaa_rate_percent is not None
    treaty_cap = (entry.section_90_dtaa_rate_percent / Decimal("100")) * entry.income_inr
    assert entry.foreign_tax_inr > treaty_cap
    assert entry.treaty_rate_status is Form67TreatyRateStatus.ABOVE
    assert entry.eligible_foreign_tax_inr == treaty_cap
    _assert_credit_caps_eligible_tax(entry)
    assert entry.section_90_credit_inr == treaty_cap
    assert entry.total_credit_inr == treaty_cap


def test_cent_rounded_treaty_withholding_uses_rule128_converted_tax() -> None:
    """The source-currency treaty-rate classification survives differing FX dates."""
    result = compute_form67_entries_from_wht(
        # 25% of USD 1.46 is 0.365; the agent withheld 0.37 (half a cent up).
        [_wht(amount=Decimal("-0.37"))],
        os_dividend_lines=[
            _os_line(
                amount_gross_usd=Decimal("1.46"),
                amount_gross_inr=Decimal("116.80"),
            )
        ],
        wht_fx_policy=FixedRatePolicy(rate=Decimal("90.56")),
        financial_year_start=_FY_START,
        financial_year_end=_FY_END,
        form67_config=_form67_cfg(overrides=[("US", Form67SourceOfIncome.DIVIDEND, "9999")]),
    )
    entry = result.entries[0]
    rule115_inr_cap = (Decimal("25") / Decimal("100")) * entry.income_inr
    assert entry.foreign_tax_inr > rule115_inr_cap
    assert entry.tax_paid_rate_percent == Decimal("25")  # reported as nominal
    assert entry.treaty_rate_status is Form67TreatyRateStatus.WITHIN
    assert entry.eligible_foreign_tax_inr == entry.foreign_tax_inr
    _assert_credit_caps_eligible_tax(entry)
    assert entry.section_90_credit_inr == entry.foreign_tax_inr


def test_treaty_rate_snapping_stops_above_half_cent_per_event() -> None:
    result = compute_form67_entries_from_wht(
        [_wht(amount=Decimal("-0.3701"))],
        os_dividend_lines=[
            _os_line(
                amount_gross_usd=Decimal("1.46"),
                amount_gross_inr=Decimal("116.80"),
            )
        ],
        wht_fx_policy=FixedRatePolicy(rate=Decimal("90.56")),
        financial_year_start=_FY_START,
        financial_year_end=_FY_END,
        form67_config=_form67_cfg(overrides=[("US", Form67SourceOfIncome.DIVIDEND, "9999")]),
    )
    entry = result.entries[0]
    treaty_cap = Decimal("0.25") * entry.income_inr
    assert entry.tax_paid_rate_percent is not None
    assert entry.tax_paid_rate_percent > Decimal("25")
    assert entry.treaty_rate_status is Form67TreatyRateStatus.ABOVE
    assert entry.foreign_tax_inr > treaty_cap
    assert entry.eligible_foreign_tax_inr == treaty_cap
    _assert_credit_caps_eligible_tax(entry)
    assert entry.section_90_credit_inr == treaty_cap


def test_no_dtaa_country_leaves_cols_9_and_10_none() -> None:
    """KY is vendored as NO_DTAA (TIEA-only). Cols 9 + 10 MUST stay
    None — the Form67Entry mutex forbids DTAA fields beside a §91
    credit in col 12, which is the only credit a NO_DTAA country can
    get."""
    ky_isin = Isin("KY99970Q1000")  # synthetic KY ISIN
    result = compute_form67_entries_from_wht(
        [
            _wht(
                isin=ky_isin,
                symbol=Ticker("KYDIV"),
                description="KYDIV - KY Tax",
                payment_date=date(2025, 12, 15),
                amount=Decimal("-0.50"),
            )
        ],
        os_dividend_lines=[
            _os_line(
                isin=ky_isin,
                symbol=Ticker("KYDIV"),
                description="KYDIV",
                issuer_country_iso2="KY",
                amount_gross_inr=Decimal("300"),
            )
        ],
        wht_fx_policy=FixedRatePolicy(FORM67_FX_RATE),
        financial_year_start=_FY_START,
        financial_year_end=_FY_END,
    )
    assert len(result.entries) == 1
    e = result.entries[0]
    assert e.iso_alpha2 == "KY"
    # Tri-state: KY is vendored NO_DTAA → cols 9 + 10 stay None.
    assert e.section_90_dtaa_article is None
    assert e.section_90_dtaa_rate_percent is None
    assert e.eligible_foreign_tax_inr is None
    # And cols 7, 8, 11, 12, 13 too: no [form67] config is supplied,
    # so col 7 stays blank and no credit can be computed; col 8 is
    # never computed.
    assert e.tax_payable_normal_inr is None
    assert e.tax_payable_special_inr is None
    assert e.section_90_credit_inr is None
    assert e.section_91_credit_inr is None
    assert e.total_credit_inr is None


def test_unvendored_dtaa_country_leaves_cols_9_and_10_none() -> None:
    """Australia is a real CBDT country (code 61) so country_codes
    resolves it cleanly, but Australia is NOT yet vendored in
    dtaa_dividend_rates.csv. The tri-state contract requires cols
    9 + 10 to stay None for unvendored countries — silently
    defaulting to either §90 or §91 would mis-attribute relief.
    Verifies the blocking design point flagged by the step-4/N
    planning rubber-duck pass."""
    au_isin = Isin("AU99970B1031")  # synthetic AU ISIN
    result = compute_form67_entries_from_wht(
        [
            _wht(
                isin=au_isin,
                symbol=Ticker("AUDIV"),
                description="AUDIV - AU Tax",
                payment_date=date(2025, 12, 15),
                amount=Decimal("-0.60"),
            )
        ],
        os_dividend_lines=[
            _os_line(
                isin=au_isin,
                symbol=Ticker("AUDIV"),
                description="AUDIV",
                issuer_country_iso2="AU",
                amount_gross_inr=Decimal("400"),
            )
        ],
        wht_fx_policy=FixedRatePolicy(FORM67_FX_RATE),
        financial_year_start=_FY_START,
        financial_year_end=_FY_END,
    )
    assert len(result.entries) == 1
    e = result.entries[0]
    assert e.iso_alpha2 == "AU"
    # Tri-state: AU is unvendored (lookup_dtaa_dividend returns None)
    # → cols 9 + 10 stay None. This is the explicit "unknown" branch
    # that protects against mis-attribution for any DTAA country we
    # have not yet vendored. Adding AU to dtaa_dividend_rates.csv in
    # a follow-up sub-PR would flip this test to populated values.
    assert e.section_90_dtaa_article is None
    assert e.section_90_dtaa_rate_percent is None
    assert e.eligible_foreign_tax_inr is None


def test_singapore_routes_to_dtaa_with_article_10_2_b_and_15_percent() -> None:
    """SG was vendored as DTAA in PR #236 (this PR). Cols 9 + 10 must
    now populate with India-Singapore Article 10(2)(b) (withholding) +
    Decimal('15') (Article 10(2)(b) portfolio cap), verbatim from the
    vendored entry. Distinct from the AU "unvendored" branch
    immediately above to make the tri-state distinction explicit."""
    sg_isin = Isin("SG99971H1095")
    result = compute_form67_entries_from_wht(
        [
            _wht(
                isin=sg_isin,
                symbol=Ticker("SGDIV"),
                description="SGDIV - SG Tax",
                payment_date=date(2025, 12, 15),
                amount=Decimal("-0.60"),
            )
        ],
        os_dividend_lines=[
            _os_line(
                isin=sg_isin,
                symbol=Ticker("SGDIV"),
                description="SGDIV",
                issuer_country_iso2="SG",
                amount_gross_inr=Decimal("400"),
            )
        ],
        wht_fx_policy=FixedRatePolicy(FORM67_FX_RATE),
        financial_year_start=_FY_START,
        financial_year_end=_FY_END,
    )
    assert len(result.entries) == 1
    e = result.entries[0]
    assert e.iso_alpha2 == "SG"
    # Col 9: WITHHOLDING article per India-Singapore DTAA Article 10
    # (Avoidance of Double Taxation), paragraph 2. Note India-SG's
    # Singapore's dividends article is also 10(2)(b), but its cap is
    # 15% (not the US's 25%), so this also guards against the rate and
    # the article being sourced from the same hardcoded country.
    assert e.section_90_dtaa_article == "Article 10(2)(b)"
    # Col 10: portfolio cap per Article 10(2)(b). 15 (not 10) — 10%
    # in 10(2)(a) applies only to corporate beneficial owners
    # owning >= 25% of the shares of the Singapore distributor.
    assert e.section_90_dtaa_rate_percent == Decimal("15")


def test_mixed_dtaa_and_unvendored_countries_route_correctly() -> None:
    """End-to-end tri-state exercise: a single compute call with US
    (DTAA-vendored), KY (NO_DTAA-vendored), and AU (unvendored) must
    route each country to its correct DTAA-field state."""
    ky_isin = Isin("KY99970Q1000")
    au_isin = Isin("AU99970B1031")
    wht_list = [
        _wht(payment_date=date(2025, 12, 15)),  # US
        _wht(
            isin=ky_isin,
            symbol=Ticker("KYDIV"),
            description="KYDIV - KY Tax",
            payment_date=date(2025, 12, 15),
            amount=Decimal("-0.50"),
        ),
        _wht(
            isin=au_isin,
            symbol=Ticker("AUDIV"),
            description="AUDIV - AU Tax",
            payment_date=date(2025, 12, 15),
            amount=Decimal("-0.60"),
        ),
    ]
    os_list = [
        _os_line(),  # US
        _os_line(
            isin=ky_isin,
            symbol=Ticker("KYDIV"),
            description="KYDIV",
            issuer_country_iso2="KY",
            amount_gross_inr=Decimal("300"),
        ),
        _os_line(
            isin=au_isin,
            symbol=Ticker("AUDIV"),
            description="AUDIV",
            issuer_country_iso2="AU",
            amount_gross_inr=Decimal("400"),
        ),
    ]
    result = compute_form67_entries_from_wht(
        wht_list,
        os_dividend_lines=os_list,
        wht_fx_policy=FixedRatePolicy(FORM67_FX_RATE),
        financial_year_start=_FY_START,
        financial_year_end=_FY_END,
    )
    by_iso = {e.iso_alpha2: e for e in result.entries}
    assert set(by_iso) == {"US", "KY", "AU"}

    # US: DTAA-vendored → cols 9 + 10 populate with the WITHHOLDING
    # article (Article 10(2)(b)) — the clause that supplies col 10's
    # 25% cap. Article 25 (relief) states no rate.
    assert by_iso["US"].section_90_dtaa_article == "Article 10(2)(b)"
    assert by_iso["US"].section_90_dtaa_rate_percent == Decimal("25")
    assert by_iso["US"].treaty_rate_status is Form67TreatyRateStatus.WITHIN  # 15% source rate

    # KY: NO_DTAA-vendored → cols 9 + 10 None, and no treaty-rate status.
    assert by_iso["KY"].section_90_dtaa_article is None
    assert by_iso["KY"].section_90_dtaa_rate_percent is None
    assert by_iso["KY"].treaty_rate_status is None

    # AU: unvendored → cols 9 + 10 None (the "unknown" branch).
    assert by_iso["AU"].section_90_dtaa_article is None
    assert by_iso["AU"].section_90_dtaa_rate_percent is None
    assert by_iso["AU"].treaty_rate_status is None


def test_dtaa_fields_round_trip_through_sort_reassignment() -> None:
    """Regression: the post-sort serial-number reassignment loop
    rebuilds Form67Entry instances from scratch (see compute/form67.py
    sort + reassign block). It MUST preserve the DTAA fields
    populated above; a silent drop in the rebuild step would emit
    None for cols 9 + 10 despite the per-iso lookup having populated
    them."""
    # This US multi-event fixture verifies that the DTAA fields survive
    # serial-number reassignment.
    wht_list = [
        _wht(payment_date=date(2025, 6, 15), amount=Decimal("-1.50")),
        _wht(payment_date=date(2025, 9, 15), amount=Decimal("-2.00")),
    ]
    os_list = [
        _os_line(payment_date=date(2025, 6, 15), amount_gross_inr=Decimal("500")),
        _os_line(payment_date=date(2025, 9, 15), amount_gross_inr=Decimal("700")),
    ]
    result = compute_form67_entries_from_wht(
        wht_list,
        os_dividend_lines=os_list,
        wht_fx_policy=FixedRatePolicy(FORM67_FX_RATE),
        financial_year_start=_FY_START,
        financial_year_end=_FY_END,
    )
    e = result.entries[0]
    assert e.serial_number == 1  # sort-reassigned
    assert e.section_90_dtaa_article == "Article 10(2)(b)"
    assert e.section_90_dtaa_rate_percent == Decimal("25")


# ============================================================================
# Step 4c/N of #37: §90 credit + per-taxpayer tax-payable config (cols 7, 11, 13)
# ============================================================================
#
# Wires Form67Config (TOML [form67] table) into the compute layer. The
# resolution is gated by the DTAA tri-state:
#
#   * applicability=DTAA (US): cols 7 + 11 + 13 ALL populate when
#     config supplies a tax-payable for the (country, source) and the
#     treaty-rate status is WITHIN or ABOVE; §90 credit =
#     min(tax_payable, eligible foreign tax), where eligible tax is
#     all the foreign tax for within-rate payments and the legacy INR
#     treaty cap for above-rate payments (see _decide_treaty_rate in
#     compute/form67.py).
#   * applicability=NO_DTAA (KY): col 7 populates if config supplies;
#     col 11 stays None (no §90 credit without a DTAA), and cols 12 +
#     13 stay None unless [form67.section_91] is also supplied (the
#     §91 tests below).
#   * unvendored (AU): col 7 populates if config supplies; cols 11 + 13
#     stay None (unknown branch).


def _form67_cfg(
    *,
    overrides: list[tuple[str, Form67SourceOfIncome, str]] | None = None,
) -> Form67Config:
    """Build a Form67Config with concise call-site syntax for tests."""
    return Form67Config(
        tax_payable_overrides=tuple(
            Form67TaxPayableOverride(
                iso_alpha2=iso,
                source_of_income=src,
                tax_payable_normal_inr=Decimal(payable),
            )
            for iso, src, payable in (overrides or [])
        ),
    )


def _assert_credit_caps_eligible_tax(entry: Form67Entry) -> None:
    """Col 11 is ``min(col 7, eligible foreign tax)`` from the stored amount."""
    assert entry.tax_payable_normal_inr is not None
    assert entry.eligible_foreign_tax_inr is not None
    assert entry.section_90_credit_inr == min(
        entry.tax_payable_normal_inr, entry.eligible_foreign_tax_inr
    )


def _compute_unproven(case: str, *, tax_payable_normal_inr: str | None) -> Form67Entry:
    wht, os_line, rates = _UNPROVEN_SOURCE_RATE_CASES[case]
    overrides = (
        []
        if tax_payable_normal_inr is None
        else [("US", Form67SourceOfIncome.DIVIDEND, tax_payable_normal_inr)]
    )
    (entry,) = compute_form67_entries_from_wht(
        wht,
        os_dividend_lines=[os_line],
        wht_fx_policy=DateAwareRatePolicy(rates),
        financial_year_start=_FY_START,
        financial_year_end=_FY_END,
        form67_config=_form67_cfg(overrides=overrides),
    ).entries
    return entry


@pytest.mark.parametrize("case", sorted(_UNPROVEN_SOURCE_RATE_CASES))
def test_unproven_source_rate_with_col_7_leaves_credit_blank_and_refuses(case: str) -> None:
    """No source rate: no INR-cap fallback, and a col 7 override is REFUSED.

    The override is large enough that the legacy INR cap would file a
    positive credit.
    """
    entry = _compute_unproven(case, tax_payable_normal_inr="9999")
    assert entry.foreign_tax_inr > 0
    assert entry.foreign_tax_rate_percent <= Decimal("100")
    assert entry.section_90_dtaa_rate_percent == Decimal("25")
    assert entry.tax_paid_rate_percent is None
    assert entry.tax_payable_normal_inr == Decimal("9999")
    assert entry.eligible_foreign_tax_inr is None
    assert entry.section_90_credit_inr is None
    assert entry.total_credit_inr is None
    assert entry.treaty_rate_status is Form67TreatyRateStatus.UNPROVEN
    assert form67_row_readiness(entry) == Form67RowReadiness(
        Form67Readiness.REFUSED, Form67RefusalReason.SOURCE_RATE_UNPROVEN
    )
    with pytest.raises(ConfigError) as caught:
        require_no_refused_form67_rows(Form67ComputeResult(entries=(entry,)))
    message = str(caught.value)
    assert "US/DIVIDEND: the source-currency tax rate can't be established" in message
    assert "remove the [[form67.tax_payable_overrides]] entry for US/DIVIDEND" in message


@pytest.mark.parametrize("case", sorted(_UNPROVEN_SOURCE_RATE_CASES))
def test_unproven_source_rate_without_col_7_is_blank(case: str) -> None:
    entry = _compute_unproven(case, tax_payable_normal_inr=None)
    assert entry.treaty_rate_status is Form67TreatyRateStatus.UNPROVEN
    assert entry.eligible_foreign_tax_inr is None
    assert form67_row_readiness(entry) == Form67RowReadiness(Form67Readiness.BLANK)


# ---- per-payment treaty-rate test (#890) ---------------------------------

_DEC_11 = date(2025, 12, 11)
# #405 E9b adds a USD 10.00 dividend with no withholding.
_E9B_UNWITHHELD_LINE = _os_line(
    payment_date=_DEC_11,
    amount_gross_usd=Decimal("10.00"),
    amount_gross_inr=Decimal("889.50"),
    fx_rate_inr_per_usd=Decimal("88.95"),
)
_E9_PAYMENT_RATE = (_E9_TAX / _E9_GROSS) * Decimal("100")
_E9_JUNE_CAP = Decimal("0.25") * (_E9_GROSS * _JUN_RATE)  # ₹194.028


def _compute_payments(
    wht: list[WithholdingTax],
    os_lines: list[ScheduleOsDividendLine],
    *,
    fx_policy: FixedRatePolicy | DateAwareRatePolicy | None = None,
    col_7: str | None = None,
) -> Form67ComputeResult:
    overrides = [] if col_7 is None else [("US", Form67SourceOfIncome.DIVIDEND, col_7)]
    return compute_form67_entries_from_wht(
        wht,
        os_dividend_lines=os_lines,
        wht_fx_policy=DateAwareRatePolicy({_JUN_12: _JUN_RATE}) if fx_policy is None else fx_policy,
        financial_year_start=_FY_START,
        financial_year_end=_FY_END,
        form67_config=_form67_cfg(overrides=overrides),
    )


def _account[T: (WithholdingTax, ScheduleOsDividendLine)](item: T, index: int) -> T:
    return replace(item, source_account_index=index)


def _assert_payment_allocation_unproven(entry: Form67Entry) -> None:
    """UNPROVEN with the country rate kept and every credit blank; col 7 refuses."""
    assert entry.treaty_rate_status is Form67TreatyRateStatus.UNPROVEN
    assert entry.tax_payable_normal_inr is not None
    assert entry.eligible_foreign_tax_inr is None
    assert entry.section_90_credit_inr is None
    assert entry.total_credit_inr is None
    assert form67_row_readiness(entry) == Form67RowReadiness(
        Form67Readiness.REFUSED, Form67RefusalReason.PAYMENT_ALLOCATION_UNPROVEN
    )
    assert entry.payment_allocation_unproven
    assert entry.tax_paid_rate_percent is not None


def test_above_rate_payment_is_not_averaged_with_unwithheld_payment() -> None:
    """#405 E9b: the country averages about 14.38%, but the June payment is about 30.15%,
    so only its treaty-rate part is eligible (#890)."""
    (entry,) = _compute_payments(
        [_E9_WHT], [_E9A_LINE, _E9B_UNWITHHELD_LINE], col_7="100000"
    ).entries
    assert entry.treaty_rate_status is Form67TreatyRateStatus.ABOVE
    assert entry.foreign_tax_inr == _E9_TAX * _JUN_RATE  # ₹234.025
    assert entry.eligible_foreign_tax_inr == _E9_JUNE_CAP
    _assert_credit_caps_eligible_tax(entry)
    assert entry.section_90_credit_inr == _E9_JUNE_CAP
    # The country rate is kept; the highest payment rate is recorded beside it.
    assert entry.tax_paid_rate_percent == (_E9_TAX / Decimal("19.12")) * Decimal("100")
    assert entry.highest_payment_rate_percent == _E9_PAYMENT_RATE
    assert round(_E9_PAYMENT_RATE, 2) == Decimal("30.15")


def test_within_rate_payment_keeps_full_rule128_tax_beside_above_rate_payment() -> None:
    """Only the above-rate payment is capped; the cent-rounded 25% payment
    keeps its full Rule-128 tax, which is above its own INR cap."""
    rate = Decimal("90.56")
    above_gross_inr = _E9_GROSS * rate
    result = _compute_payments(
        [_wht(amount=Decimal("-0.37")), _wht(payment_date=_JUN_12, amount=-_E9_TAX)],
        [
            _os_line(amount_gross_usd=Decimal("1.46"), amount_gross_inr=Decimal("116.80")),
            _os_line(
                payment_date=_JUN_12, amount_gross_usd=_E9_GROSS, amount_gross_inr=above_gross_inr
            ),
        ],
        fx_policy=FixedRatePolicy(rate=rate),
        col_7="9999",
    )
    (entry,) = result.entries
    within_tax = Decimal("0.37") * rate
    assert within_tax == Decimal("33.5072")  # 0.37 x 90.56
    assert within_tax > Decimal("0.25") * Decimal("116.80")  # its ₹29.20 INR cap
    assert entry.treaty_rate_status is Form67TreatyRateStatus.ABOVE
    assert entry.eligible_foreign_tax_inr == within_tax + Decimal("0.25") * above_gross_inr
    _assert_credit_caps_eligible_tax(entry)
    assert entry.highest_payment_rate_percent == _E9_PAYMENT_RATE


def test_same_key_withholding_rows_keep_per_row_rounding_tolerance() -> None:
    """Two separately rounded rows on one dividend line: 0.74 on 2.92 is
    within two half-cents of 25%."""
    line = _os_line(amount_gross_usd=Decimal("2.92"), amount_gross_inr=Decimal("233.60"))
    (entry,) = _compute_payments(
        [_wht(amount=Decimal("-0.37")), _wht(amount=Decimal("-0.37"))],
        [line],
        fx_policy=FixedRatePolicy(rate=Decimal("90.56")),
    ).entries
    assert entry.treaty_rate_status is Form67TreatyRateStatus.WITHIN
    assert entry.tax_paid_rate_percent == Decimal("25")
    assert entry.eligible_foreign_tax_inr == entry.foreign_tax_inr


@pytest.mark.parametrize("case", sorted(_REFUND_BESIDE_ABOVE_RATE_PAYMENTS))
def test_above_rate_payment_with_refund_in_country_is_unproven(case: str) -> None:
    """Form 67 leaves allocation UNPROVEN when an above-rate payment
    and any refund share a country, even with an exact pair (#890)."""
    wht, os_lines, rates = _REFUND_BESIDE_ABOVE_RATE_PAYMENTS[case]
    (entry,) = _compute_payments(
        wht, os_lines, fx_policy=DateAwareRatePolicy(rates), col_7="100000"
    ).entries
    assert entry.foreign_tax_inr > 0
    assert entry.foreign_tax_rate_percent > 0
    _assert_payment_allocation_unproven(entry)
    assert entry.highest_payment_rate_percent == _E9_PAYMENT_RATE


def test_above_rate_payment_with_excluded_prior_fy_refund_stays_above() -> None:
    """A refund that cross-FY pairing excludes isn't a refund in the country."""
    result = _compute_payments(
        [
            _wht(payment_date=date(2025, 3, 12), amount=Decimal("-1.00")),
            _E9_WHT,
            _wht(payment_date=date(2025, 7, 15), amount=Decimal("1.00")),
        ],
        [_E9A_LINE],
    )
    assert result.cross_fy_reversal_report.cross_fy_excluded_count == 1
    (entry,) = result.entries
    assert entry.treaty_rate_status is Form67TreatyRateStatus.ABOVE
    assert entry.eligible_foreign_tax_inr == _E9_JUNE_CAP
    assert entry.highest_payment_rate_percent == _E9_PAYMENT_RATE


def test_payments_are_keyed_by_source_account() -> None:
    """Same ISIN and payment date in two accounts are two payments: the 30%
    one isn't averaged with the unwithheld one to 15%."""
    line = _os_line(amount_gross_usd=Decimal("100.00"), amount_gross_inr=Decimal("8500"))
    (entry,) = _compute_payments(
        [_account(_wht(amount=Decimal("-30.00")), 0)],
        [_account(line, 0), _account(line, 1)],
        fx_policy=FixedRatePolicy(rate=Decimal("85")),
    ).entries
    assert entry.tax_paid_rate_percent == Decimal("15")
    assert entry.treaty_rate_status is Form67TreatyRateStatus.ABOVE
    assert entry.eligible_foreign_tax_inr == Decimal("0.25") * Decimal("8500")
    assert entry.highest_payment_rate_percent == Decimal("30")


def test_same_day_payments_from_exact_pairing_source_are_unproven() -> None:
    """Two account-0 dividends of one ISIN on one date: the adapter paired the
    USD 30 to one of them, but the shared models can't tell which."""
    line = _account(
        _os_line(amount_gross_usd=Decimal("100.00"), amount_gross_inr=Decimal("8500")), 0
    )
    (entry,) = _compute_payments(
        [_account(_wht(amount=Decimal("-30.00")), 0)],
        [line, line],
        fx_policy=FixedRatePolicy(rate=Decimal("85")),
        col_7="100000",
    ).entries
    assert entry.tax_paid_rate_percent == Decimal("15")
    _assert_payment_allocation_unproven(entry)
    assert entry.highest_payment_rate_percent is None


def test_same_day_payments_without_an_account_index_are_unproven() -> None:
    """#1283: IBKR, NetBenefits, StockPlan and Fidelity CMA leave the account
    index unset. Two such dividends of one ISIN on one date, with USD 30 on one
    of them, can't be told apart either, so they aren't pooled into 15%."""
    line = _os_line(amount_gross_usd=Decimal("100.00"), amount_gross_inr=Decimal("8500"))
    assert line.source_account_index is None
    (entry,) = _compute_payments(
        [_wht(amount=Decimal("-30.00"))],
        [line, line],
        fx_policy=FixedRatePolicy(rate=Decimal("85")),
        col_7="100000",
    ).entries
    assert entry.tax_paid_rate_percent == Decimal("15")
    _assert_payment_allocation_unproven(entry)
    assert entry.highest_payment_rate_percent is None


@pytest.mark.parametrize("account_index", [None, 0])
def test_same_day_lines_without_withholding_keep_their_result(account_index: int | None) -> None:
    """#1283 control: several dividend lines on a key with no paid withholding
    have nothing to allocate; the withheld payment beside them decides alone."""
    unwithheld = _os_line(
        payment_date=_DEC_11,
        amount_gross_usd=Decimal("100.00"),
        amount_gross_inr=Decimal("8500"),
        fx_rate_inr_per_usd=Decimal("85"),
    )
    if account_index is not None:
        unwithheld = _account(unwithheld, account_index)
    (entry,) = _compute_payments(
        [_wht(amount=Decimal("-15.00"))],
        [
            _os_line(amount_gross_usd=Decimal("100.00"), amount_gross_inr=Decimal("8500")),
            unwithheld,
            unwithheld,
        ],
        fx_policy=FixedRatePolicy(rate=Decimal("85")),
        col_7="100000",
    ).entries
    assert entry.treaty_rate_status is Form67TreatyRateStatus.WITHIN
    assert not entry.payment_allocation_unproven
    assert entry.tax_paid_rate_percent == Decimal("5")
    assert entry.highest_payment_rate_percent == Decimal("15")
    assert entry.eligible_foreign_tax_inr == entry.foreign_tax_inr == Decimal("1275")
    _assert_credit_caps_eligible_tax(entry)
    assert entry.total_credit_inr == Decimal("1275")
    assert form67_row_readiness(entry) == Form67RowReadiness(Form67Readiness.READY)


_XQCA: Isin = Isin("US99972B1017")
_UNATTRIBUTABLE_WITHHOLDING = {
    # Paid XQCA tax with no XQCA dividend on its date, beside its own refund.
    "no-dividend-on-its-date": [
        _wht(isin=_XQCA, payment_date=date(2025, 7, 10), amount=Decimal("-1.00")),
        _wht(isin=_XQCA, payment_date=date(2025, 8, 10), amount=Decimal("1.00")),
    ],
    # USD 12 withheld on a USD 10 dividend.
    "withholding-above-its-dividend": [_wht(payment_date=_JUN_12, amount=Decimal("-12.00"))],
}


@pytest.mark.parametrize("case", sorted(_UNATTRIBUTABLE_WITHHOLDING))
def test_withholding_that_cannot_be_tested_per_payment_is_unproven(case: str) -> None:
    """A payment whose withholding has no dividend, or exceeds it, has no
    rate to test, so its withholding can't be attributed (#890)."""
    (entry,) = _compute_payments(
        [_wht(amount=Decimal("-1.50")), *_UNATTRIBUTABLE_WITHHOLDING[case]],
        [
            _os_line(amount_gross_usd=Decimal("10.00"), amount_gross_inr=Decimal("850")),
            _os_line(
                payment_date=_JUN_12,
                amount_gross_usd=Decimal("10.00"),
                amount_gross_inr=Decimal("850"),
            ),
        ],
        fx_policy=FixedRatePolicy(rate=Decimal("85")),
        col_7="100000",
    ).entries
    _assert_payment_allocation_unproven(entry)
    assert entry.highest_payment_rate_percent is None


# ---- column 6 snaps only without an above-rate payment (#1448) ---------------

_SEP_12 = date(2025, 9, 12)
_SEP_26 = date(2025, 9, 26)
_AUG_RATE = Decimal("87.70")  # the 2025-08-31 TTBR, for both Rule 128 and Rule 115


def _september_line(gross: str, payment_date: date = _SEP_12) -> ScheduleOsDividendLine:
    return _os_line(
        payment_date=payment_date,
        amount_gross_usd=Decimal(gross),
        amount_gross_inr=Decimal(gross) * _AUG_RATE,
        fx_rate_inr_per_usd=_AUG_RATE,
    )


@pytest.mark.parametrize(
    ("first", "second", "rate", "filed", "highest"),
    [
        pytest.param(
            ("100.00", "25.01"), ("100.00", "25.00"), "25.005", "25.01", "25.01", id="example"
        ),
        pytest.param(("0.04", "0.02"), ("0.04", "0.01"), "37.5", "37.50", "50", id="sub-dollar"),
        # The unsnapped rate can still file as the treaty rate.
        pytest.param(
            ("1000.00", "250.01"),
            ("1000.00", "250.00"),
            "25.0005",
            "25.00",
            "25.001",
            id="rounds-to-it",
        ),
    ],
)
def test_payment_above_the_treaty_rate_stops_column_6_snapping(
    first: tuple[str, str], second: tuple[str, str], rate: str, filed: str, highest: str
) -> None:
    """#1448: (gross, tax) USD dividends on 12 and 26 September 2025. The
    country's tax is within two half-cents of 25% of its income, but the first
    payment is above its own half-cent, so column 6 keeps the country's rate.
    Main snapped each to 25. Eligible tax still caps the first payment (#890)."""
    payments = ((_SEP_12, first), (_SEP_26, second))
    (entry,) = _compute_payments(
        [_wht(payment_date=day, amount=-Decimal(tax)) for day, (_, tax) in payments],
        [_september_line(gross, day) for day, (gross, _) in payments],
        fx_policy=FixedRatePolicy(rate=_AUG_RATE),
        col_7="4000",
    ).entries
    assert entry.treaty_rate_status is Form67TreatyRateStatus.ABOVE
    assert entry.tax_paid_rate_percent == Decimal(rate)
    assert entry.filed.tax_paid_rate_percent == Decimal(filed)
    assert entry.highest_payment_rate_percent == Decimal(highest)
    cap = Decimal("0.25") * Decimal(first[0]) * _AUG_RATE
    assert entry.eligible_foreign_tax_inr == cap + Decimal(second[1]) * _AUG_RATE
    _assert_credit_caps_eligible_tax(entry)


def test_unproven_refund_beside_an_above_rate_payment_keeps_the_country_rate() -> None:
    """#1448: USD 25.01 and 0.01 withheld on a USD 100.00 dividend, then the
    0.01 reversed on 15 October. Net USD 25.01 is within three half-cents of
    25%, but the payment paid 25.02%, so column 6 files 25.01; main filed 25.00."""
    (entry,) = _compute_payments(
        [
            _wht(payment_date=_SEP_12, amount=Decimal("-25.01")),
            _wht(payment_date=_SEP_12, amount=Decimal("-0.01")),
            _wht(payment_date=date(2025, 10, 15), amount=Decimal("0.01")),
        ],
        [_september_line("100.00")],
        fx_policy=FixedRatePolicy(rate=_AUG_RATE),
        col_7="4000",
    ).entries
    _assert_payment_allocation_unproven(entry)
    assert entry.highest_payment_rate_percent == Decimal("25.02")
    assert entry.tax_paid_rate_percent == Decimal("25.01")
    assert entry.filed.tax_paid_rate_percent == Decimal("25.01")


def test_column_6_still_snaps_where_payments_cannot_be_tested() -> None:
    """#1448 control: two same-day dividend lines can't be tested apart, so
    no payment is found above the rate, and USD 50.01 on USD 200.00 still
    snaps to 25, as on main."""
    line = _september_line("100.00")
    (entry,) = _compute_payments(
        [_wht(payment_date=_SEP_12, amount=Decimal(tax)) for tax in ("-25.01", "-25.00")],
        [line, line],
        fx_policy=FixedRatePolicy(rate=_AUG_RATE),
        col_7="4000",
    ).entries
    _assert_payment_allocation_unproven(entry)
    assert entry.highest_payment_rate_percent is None
    assert entry.tax_paid_rate_percent == Decimal("25")


# ---- exact treaty-rate test, treaty cap and column 6 rates (#1312) -----------

# At ₹0.2/$ a USD 4E14 gross is ₹8E13, within the CBDT 14-digit bound.
_EDGE_RATE = Decimal("0.2")


def _exact_product(*factors: str | Decimal) -> Decimal:
    """The product of ``factors`` with every digit."""
    with localcontext(Context(prec=1200)):
        product = Decimal(1)
        for factor in factors:
            product *= Decimal(factor)
    return product


def _one_payment(
    gross_usd: str,
    paid_usd: str,
    *,
    rate: Decimal,
    gross_inr: Decimal | None = None,
    col_7: str | None = None,
) -> Form67Entry:
    """A US dividend of ``gross_usd`` and one paid row of ``paid_usd``, both at
    ``rate``, with the dividend's INR ``gross_usd * rate`` unless given."""
    inr = _exact_product(gross_usd, rate) if gross_inr is None else gross_inr
    (entry,) = _compute_payments(
        [_wht(amount=Decimal(paid_usd).copy_negate())],
        [
            _os_line(
                amount_gross_usd=Decimal(gross_usd),
                amount_gross_inr=inr,
                fx_rate_inr_per_usd=rate,
            )
        ],
        fx_policy=FixedRatePolicy(rate=rate),
        col_7=col_7,
    ).entries
    return entry


def test_excess_1e_20_past_the_tolerance_is_above_the_treaty_rate() -> None:
    """#1312: the paid tax exceeds 25% of the 35-digit gross by exactly
    0.005 + 1E-20, past one row's half cent, so the payment is above the rate
    and its eligible tax is the exact cap. Column 6 isn't snapped either. Main
    rounded the treaty tax up at 28 digits, saw an excess of
    0.00499999999995000002, and filed WITHIN with column 6 at 25."""
    gross, paid = "400000000000000.00000000000020000004", "100000000000000.00500000000005000002"
    entry = _one_payment(gross, paid, rate=_EDGE_RATE)
    assert entry.treaty_rate_status is Form67TreatyRateStatus.ABOVE
    assert entry.eligible_foreign_tax_inr is not None
    assert (
        entry.eligible_foreign_tax_inr.compare_total(_exact_product("0.25", gross, _EDGE_RATE)) == 0
    )
    assert entry.eligible_foreign_tax_inr < entry.foreign_tax_inr
    unsnapped = Decimal("25.00000000000000125000000000")
    assert unsnapped > Decimal("25")
    assert entry.highest_payment_rate_percent == unsnapped
    assert entry.tax_paid_rate_percent == unsnapped


def test_excess_exactly_at_the_tolerance_is_within_the_treaty_rate() -> None:
    """#1312: the paid tax exceeds 25% of the 29-digit gross by exactly 0.005,
    one row's half cent, so the payment is within the rate and both its rate
    and column 6 snap to 25. Main rounded the treaty tax down at 28 digits, saw
    an excess of 0.00500000000004, and filed ABOVE with neither snapped."""
    entry = _one_payment(
        "400000000000000.00000000000016", "100000000000000.00500000000004", rate=_EDGE_RATE
    )
    assert entry.treaty_rate_status is Form67TreatyRateStatus.WITHIN
    assert entry.eligible_foreign_tax_inr == entry.foreign_tax_inr
    assert entry.section_90_dtaa_rate_percent == Decimal("25")
    assert entry.highest_payment_rate_percent == Decimal("25")
    assert entry.tax_paid_rate_percent == Decimal("25")


def test_treaty_cap_keeps_every_digit() -> None:
    """#1312: 25% of the ₹40002157.99999999999999999998 gross is exactly
    ₹10000539.4999999999999999999950, so the eligible tax and the section 90
    and total credits file ₹10000539. Main's 28-digit cap,
    ₹10000539.50000000000000000000, filed ₹10000540."""
    gross_inr = Decimal("40002157.99999999999999999998")
    entry = _one_payment(
        "456124.9486887115165336374",
        "136837.49",
        rate=Decimal("87.70"),
        gross_inr=gross_inr,
        col_7="99999999",
    )
    cap = Decimal("10000539.4999999999999999999950")
    assert entry.treaty_rate_status is Form67TreatyRateStatus.ABOVE
    assert entry.eligible_foreign_tax_inr is not None
    assert entry.eligible_foreign_tax_inr.compare_total(cap) == 0
    assert entry.section_90_credit_inr == entry.total_credit_inr == cap
    assert entry.filed.section_90_credit_inr == entry.filed.total_credit_inr == Decimal("10000539")


# The rate is 12.345% when the tax ends in ...12345, and just below it when it
# ends in ...12344; both quotients round to 12.345 at 28 digits.
_RATE_INCOME = "1000000000.000000000000001"
_RATE_CASES = pytest.mark.parametrize(
    ("tax", "filed"),
    [("123450000.00000000000000012344", "12.34"), ("123450000.00000000000000012345", "12.35")],
    ids=["just-below-a-half-hundredth", "on-a-half-hundredth"],
)


@_RATE_CASES
def test_column_6_source_rate_files_the_exact_rate_hundredths(tax: str, filed: str) -> None:
    """#1312: column 6 files the exact USD rate's hundredths. Main's 28-digit
    quotient carried the rate just below 12.345% up onto it and filed 12.35."""
    entry = _one_payment(_RATE_INCOME, tax, rate=Decimal("87.70"))
    assert entry.treaty_rate_status is Form67TreatyRateStatus.WITHIN
    assert entry.filed.tax_paid_rate_percent == Decimal(filed)


@_RATE_CASES
def test_column_6_inr_rate_files_the_exact_rate_hundredths(tax: str, filed: str) -> None:
    """#1312: without a source rate (USD tax above the USD income), column 6
    files the INR rate, also the exact rate's hundredths. Main filed 12.35."""
    entry = _one_payment("100000000.00", tax, rate=Decimal("1"), gross_inr=Decimal(_RATE_INCOME))
    assert entry.tax_paid_rate_percent is None
    assert entry.treaty_rate_status is Form67TreatyRateStatus.UNPROVEN
    assert entry.filed.tax_paid_rate_percent == Decimal(filed)


def test_ordinary_rates_keep_the_28_digit_quotient() -> None:
    """#1312 changes no ordinary rate: #405 E9b's repeating USD 2.75 on USD
    19.12 and its INR and per-payment rates keep main's quotient, exponent
    included."""
    (entry,) = _compute_payments(
        [_E9_WHT], [_E9A_LINE, _E9B_UNWITHHELD_LINE], col_7="100000"
    ).entries
    assert entry.tax_paid_rate_percent is not None
    assert entry.highest_payment_rate_percent is not None
    for rate, quotient in (
        (entry.tax_paid_rate_percent, Decimal("14.38284518828451882845188285")),
        (entry.highest_payment_rate_percent, Decimal("30.15350877192982456140350877")),
        (entry.foreign_tax_rate_percent, Decimal("14.05039108748015744363032927")),
    ):
        assert rate.as_tuple() == quotient.as_tuple()


def test_no_form67_config_leaves_indian_tax_and_credit_blank() -> None:
    result = compute_form67_entries_from_wht(
        [_wht()],
        os_dividend_lines=[_os_line()],
        wht_fx_policy=FixedRatePolicy(FORM67_FX_RATE),
        financial_year_start=_FY_START,
        financial_year_end=_FY_END,
    )
    e = result.entries[0]
    assert e.tax_payable_normal_inr is None
    assert e.section_90_credit_inr is None
    assert e.total_credit_inr is None


def test_empty_form67_config_also_leaves_indian_tax_and_credit_blank() -> None:
    result = compute_form67_entries_from_wht(
        [_wht()],
        os_dividend_lines=[_os_line()],
        wht_fx_policy=FixedRatePolicy(FORM67_FX_RATE),
        financial_year_start=_FY_START,
        financial_year_end=_FY_END,
        form67_config=_form67_cfg(),
    )
    e = result.entries[0]
    assert e.tax_payable_normal_inr is None
    assert e.section_90_credit_inr is None
    assert e.total_credit_inr is None


def test_exact_override_populates_indian_tax() -> None:
    result = compute_form67_entries_from_wht(
        [_wht()],
        os_dividend_lines=[_os_line(amount_gross_inr=Decimal("4000"))],
        wht_fx_policy=FixedRatePolicy(FORM67_FX_RATE),
        financial_year_start=_FY_START,
        financial_year_end=_FY_END,
        form67_config=_form67_cfg(overrides=[("US", Form67SourceOfIncome.DIVIDEND, "999.99")]),
    )
    e = result.entries[0]
    assert e.tax_payable_normal_inr == Decimal("999.99")
    _assert_credit_caps_eligible_tax(e)


def test_section_90_credit_indian_tax_cap_binds() -> None:
    """§90 formula corner: Indian tax cap is the tighter constraint.

    Setup: foreign_tax = 100 (≈ 20% of income); DTAA cap = 25% * 500 =
    125 (looser); tax_payable = 50 (tighter).
    Expected: eligible = min(100, 125) = 100; credit = min(50, 100) = 50.
    """
    # rate=100 INR/USD lets us hit exact integer foreign_tax_inr values.
    wht_list = [_wht(amount=Decimal("-1"))]  # 1 * 100 = 100 INR
    os_list = [_os_line(amount_gross_inr=Decimal("500"))]  # 500 INR income
    result = compute_form67_entries_from_wht(
        wht_list,
        os_dividend_lines=os_list,
        wht_fx_policy=FixedRatePolicy(rate=Decimal("100")),
        financial_year_start=_FY_START,
        financial_year_end=_FY_END,
        form67_config=_form67_cfg(
            overrides=[("US", Form67SourceOfIncome.DIVIDEND, "50")],
        ),
    )
    e = result.entries[0]
    assert e.foreign_tax_inr == Decimal("100")
    assert e.tax_payable_normal_inr == Decimal("50")
    # DTAA cap (25% * 500 = 125) > foreign_tax (100) → eligible = 100.
    # Indian cap (50) < eligible (100) → credit = 50.
    assert e.eligible_foreign_tax_inr == Decimal("100")
    _assert_credit_caps_eligible_tax(e)
    assert e.section_90_credit_inr == Decimal("50")
    assert e.total_credit_inr == Decimal("50")


def test_section_90_credit_does_not_infer_excess_from_different_fx_bases() -> None:
    """INR ratios cannot create a treaty excess absent a source-currency excess."""
    wht_list = [_wht(amount=Decimal("-2"))]  # 2 * 100 = 200 INR
    os_list = [_os_line(amount_gross_inr=Decimal("500"))]
    result = compute_form67_entries_from_wht(
        wht_list,
        os_dividend_lines=os_list,
        wht_fx_policy=FixedRatePolicy(rate=Decimal("100")),
        financial_year_start=_FY_START,
        financial_year_end=_FY_END,
        form67_config=_form67_cfg(
            overrides=[("US", Form67SourceOfIncome.DIVIDEND, "200")],
        ),
    )
    e = result.entries[0]
    assert e.foreign_tax_inr == Decimal("200")
    assert e.tax_payable_normal_inr == Decimal("200")
    assert e.tax_paid_rate_percent is not None
    assert e.section_90_dtaa_rate_percent is not None
    assert e.tax_paid_rate_percent < e.section_90_dtaa_rate_percent
    # The INR ratio (200 / 500 = 40%) is above the treaty rate, but the
    # source-currency rate is not, so all Rule-128-converted tax is eligible.
    assert e.foreign_tax_inr > (e.section_90_dtaa_rate_percent / Decimal("100")) * e.income_inr
    assert e.eligible_foreign_tax_inr == e.foreign_tax_inr
    _assert_credit_caps_eligible_tax(e)
    assert e.section_90_credit_inr == Decimal("200")
    assert e.total_credit_inr == Decimal("200")


def test_section_90_credit_foreign_tax_already_below_dtaa_cap() -> None:
    """Common case: foreign WHT (US default 15% via W-8BEN) is below
    the treaty cap (25%) and below the Indian tax. Credit equals the
    full foreign tax.

    Setup: foreign_tax = 75 (15% * 500); DTAA cap = 125; tax_payable
    = 150.
    Expected: eligible = 75 (within the treaty rate, so all of it);
    credit = min(150, 75) = 75.
    """
    wht_list = [_wht(amount=Decimal("-0.75"))]  # 0.75 * 100 = 75 INR
    os_list = [_os_line(amount_gross_inr=Decimal("500"))]
    result = compute_form67_entries_from_wht(
        wht_list,
        os_dividend_lines=os_list,
        wht_fx_policy=FixedRatePolicy(rate=Decimal("100")),
        financial_year_start=_FY_START,
        financial_year_end=_FY_END,
        form67_config=_form67_cfg(
            overrides=[("US", Form67SourceOfIncome.DIVIDEND, "150")],
        ),
    )
    e = result.entries[0]
    assert e.foreign_tax_inr == Decimal("75.00")
    assert e.eligible_foreign_tax_inr == Decimal("75.00")
    _assert_credit_caps_eligible_tax(e)
    assert e.section_90_credit_inr == Decimal("75.00")


def test_section_90_credit_zero_tax_payable_is_legitimate() -> None:
    """Zero tax_payable_normal_inr is a legitimate edge case (foreign
    income wholly absorbed by carry-forward losses, or taxpayer's
    total income below slab threshold). Credit = min(0, eligible) = 0.
    Form67Entry's section_90_credit_inr >= 0 invariant accepts this."""
    result = compute_form67_entries_from_wht(
        [_wht()],
        os_dividend_lines=[_os_line(amount_gross_inr=Decimal("500"))],
        wht_fx_policy=FixedRatePolicy(FORM67_FX_RATE),
        financial_year_start=_FY_START,
        financial_year_end=_FY_END,
        form67_config=_form67_cfg(
            overrides=[("US", Form67SourceOfIncome.DIVIDEND, "0")],
        ),
    )
    e = result.entries[0]
    assert e.tax_payable_normal_inr == Decimal("0")
    assert e.eligible_foreign_tax_inr == e.foreign_tax_inr
    _assert_credit_caps_eligible_tax(e)
    assert e.section_90_credit_inr == Decimal("0")
    assert e.total_credit_inr == Decimal("0")


def test_positive_credit_that_files_as_zero_is_a_zero_credit() -> None:
    """#898's example: a $1 dividend with $0.25 withheld at ₹80/$ and ₹0.10 of
    Indian tax. The ₹0.10 credit files as 0, so the row is refused like a zero
    credit, while the entry keeps the unrounded amounts for audit."""
    result = compute_form67_entries_from_wht(
        [_wht(amount=Decimal("-0.25"))],
        os_dividend_lines=[
            _os_line(
                amount_gross_usd=Decimal("1.00"),
                amount_gross_inr=Decimal("80"),
                fx_rate_inr_per_usd=Decimal("80"),
            )
        ],
        wht_fx_policy=FixedRatePolicy(rate=Decimal("80")),
        financial_year_start=_FY_START,
        financial_year_end=_FY_END,
        form67_config=_form67_cfg(overrides=[("US", Form67SourceOfIncome.DIVIDEND, "0.10")]),
    )
    (entry,) = result.entries
    assert entry.treaty_rate_status is Form67TreatyRateStatus.WITHIN
    assert entry.section_90_credit_inr == entry.total_credit_inr == Decimal("0.10")
    filed = entry.filed
    assert (filed.income_inr, filed.foreign_tax_inr) == (Decimal("80"), Decimal("20"))
    assert (filed.tax_paid_rate_percent, filed.section_90_dtaa_rate_percent) == (
        Decimal("25.00"),
        Decimal("25.00"),
    )
    assert filed.tax_payable_normal_inr == Decimal("0")
    assert filed.section_90_credit_inr == filed.total_credit_inr == Decimal("0")
    assert filed.section_91_credit_inr is None
    assert result.row_readiness == (
        Form67RowReadiness(Form67Readiness.REFUSED, Form67RefusalReason.ZERO_CREDIT),
    )
    with pytest.raises(ConfigError, match=r"US/DIVIDEND: the computed credit rounds to ₹0"):
        require_no_refused_form67_rows(result)


def test_no_dtaa_country_populates_col_7_only_not_credit() -> None:
    """KY is NO_DTAA. With a tax-payable override, col 7 populates
    (Indian tax payable on the foreign income exists regardless of
    relief pathway) but cols 11 + 13 stay None (§90 pathway gated on
    DTAA applicability; no §91 credit because the fixture supplies no
    [form67.section_91])."""
    ky_isin = Isin("KY99970Q1000")
    result = compute_form67_entries_from_wht(
        [
            _wht(
                isin=ky_isin,
                symbol=Ticker("KYDIV"),
                description="KYDIV - KY Tax",
                amount=Decimal("-0.50"),
            )
        ],
        os_dividend_lines=[
            _os_line(
                isin=ky_isin,
                symbol=Ticker("KYDIV"),
                description="KYDIV",
                issuer_country_iso2="KY",
                amount_gross_inr=Decimal("300"),
            )
        ],
        wht_fx_policy=FixedRatePolicy(FORM67_FX_RATE),
        financial_year_start=_FY_START,
        financial_year_end=_FY_END,
        form67_config=_form67_cfg(
            overrides=[("KY", Form67SourceOfIncome.DIVIDEND, "75")],
        ),
    )
    e = result.entries[0]
    assert e.iso_alpha2 == "KY"
    assert e.tax_payable_normal_inr == Decimal("75")
    # NO_DTAA → no §90 credit (DTAA gate); no §91 credit without
    # [form67.section_91].
    assert e.section_90_credit_inr is None
    assert e.total_credit_inr is None


def test_unvendored_country_populates_col_7_only_not_credit() -> None:
    """AU is unvendored in dtaa_dividend_rates.csv. Same behaviour as
    NO_DTAA: col 7 populates, cols 11 + 13 stay None (user fills via
    portal until AU is vendored in a follow-up sub-PR)."""
    au_isin = Isin("AU99970B1031")
    result = compute_form67_entries_from_wht(
        [
            _wht(
                isin=au_isin,
                symbol=Ticker("AUDIV"),
                description="AUDIV - AU Tax",
                amount=Decimal("-0.60"),
            )
        ],
        os_dividend_lines=[
            _os_line(
                isin=au_isin,
                symbol=Ticker("AUDIV"),
                description="AUDIV",
                issuer_country_iso2="AU",
                amount_gross_inr=Decimal("400"),
            )
        ],
        wht_fx_policy=FixedRatePolicy(FORM67_FX_RATE),
        financial_year_start=_FY_START,
        financial_year_end=_FY_END,
        form67_config=_form67_cfg(
            overrides=[("AU", Form67SourceOfIncome.DIVIDEND, "100")],
        ),
    )
    e = result.entries[0]
    assert e.iso_alpha2 == "AU"
    assert e.tax_payable_normal_inr == Decimal("100")
    assert e.section_90_credit_inr is None
    assert e.total_credit_inr is None


def test_override_for_different_source_does_not_apply() -> None:
    """Override for (US, INTEREST_INCOME) must NOT apply to a US
    DIVIDEND aggregate. The (iso, source) tuple is the key — not iso
    alone."""
    result = compute_form67_entries_from_wht(
        [_wht()],
        os_dividend_lines=[_os_line(amount_gross_inr=Decimal("4000"))],
        wht_fx_policy=FixedRatePolicy(FORM67_FX_RATE),
        financial_year_start=_FY_START,
        financial_year_end=_FY_END,
        form67_config=_form67_cfg(
            overrides=[("US", Form67SourceOfIncome.INTEREST_INCOME, "999.99")],
        ),
    )
    e = result.entries[0]
    assert e.source_of_income is Form67SourceOfIncome.DIVIDEND
    assert e.tax_payable_normal_inr is None
    assert e.section_90_credit_inr is None
    assert e.total_credit_inr is None


def test_override_for_unmatched_country_silently_unused() -> None:
    """Override for ("GB", DIVIDEND) when only US dividends are
    present is silently dropped. The compute layer doesn't surface
    a warning here — the CLI layer is the right place for unused-
    override advisories (out of scope for 4c/N)."""
    result = compute_form67_entries_from_wht(
        [_wht()],
        os_dividend_lines=[_os_line()],
        wht_fx_policy=FixedRatePolicy(FORM67_FX_RATE),
        financial_year_start=_FY_START,
        financial_year_end=_FY_END,
        form67_config=_form67_cfg(
            overrides=[("GB", Form67SourceOfIncome.DIVIDEND, "5000")],
        ),
    )
    # Only US entry emitted; GB override goes unused.
    assert len(result.entries) == 1
    assert result.entries[0].iso_alpha2 == "US"
    assert result.entries[0].tax_payable_normal_inr is None
    assert result.entries[0].section_90_credit_inr is None
    assert result.entries[0].total_credit_inr is None


# ============================================================================
# Step 4d/N of #37: §91 unilateral relief for NO_DTAA countries (cols 12, 13)
# ============================================================================
#
# §91 of the Income-tax Act, 1961 grants relief for income from a country
# with which India has NO DTAA. The formula uses the LOWER of the Indian
# average rate of tax and the foreign rate of tax. §91 and §90 are
# mutually exclusive — gated by the DTAA tri-state:
#
#   * applicability=DTAA (US): §91 does NOT apply (§90 pathway exclusive);
#     col 12 stays None even with section_91 config.
#   * applicability=NO_DTAA (KY): §91 populates cols 12 + 13 when section_91
#     config AND tax_payable_normal_inr are both supplied.
#   * unvendored (AU): §91 does NOT apply (unknown branch is conservative;
#     would mis-attribute relief for any DTAA country not yet vendored).


def _section_91_cfg(*, total_tax: str, total_income: str) -> Form67Section91Config:
    return Form67Section91Config(
        total_indian_tax_before_91_relief_inr=Decimal(total_tax),
        total_income_inr=Decimal(total_income),
    )


def _form67_cfg_with_section_91(
    *,
    overrides: list[tuple[str, Form67SourceOfIncome, str]] | None = None,
    section_91: Form67Section91Config | None = None,
) -> Form67Config:
    return Form67Config(
        tax_payable_overrides=tuple(
            Form67TaxPayableOverride(
                iso_alpha2=iso,
                source_of_income=src,
                tax_payable_normal_inr=Decimal(payable),
            )
            for iso, src, payable in (overrides or [])
        ),
        section_91=section_91,
    )


def test_no_dtaa_country_with_section_91_config_populates_cols_12_and_13() -> None:
    """KY (NO_DTAA) + section_91 config + tax_payable override → cols 7,
    12, 13 all populate. Cols 9, 10, 11 stay None (those are the §90
    pathway).

    Setup:
      foreign_tax = 50 (= 25% * 200 income); tax_payable = 60;
      indian_avg_rate = 30000/100000 = 0.30 = 30%;
      foreign_rate = 50/200 = 0.25 = 25%.
    Expected:
      §91 relief = income * min(0.30, 0.25) = 200 * 0.25 = 50.
    """
    ky_isin = Isin("KY99970Q1000")
    result = compute_form67_entries_from_wht(
        [
            _wht(
                isin=ky_isin,
                symbol=Ticker("KYDIV"),
                description="KYDIV - KY Tax",
                amount=Decimal("-0.50"),  # 0.50 * 100 = 50 INR foreign tax
            )
        ],
        os_dividend_lines=[
            _os_line(
                isin=ky_isin,
                symbol=Ticker("KYDIV"),
                description="KYDIV",
                issuer_country_iso2="KY",
                amount_gross_inr=Decimal("200"),
            )
        ],
        wht_fx_policy=FixedRatePolicy(rate=Decimal("100")),
        financial_year_start=_FY_START,
        financial_year_end=_FY_END,
        form67_config=_form67_cfg_with_section_91(
            overrides=[("KY", Form67SourceOfIncome.DIVIDEND, "60")],
            section_91=_section_91_cfg(total_tax="30000", total_income="100000"),
        ),
    )
    e = result.entries[0]
    assert e.iso_alpha2 == "KY"
    # Col 7: tax_payable from override
    assert e.tax_payable_normal_inr == Decimal("60")
    # Cols 9, 10, 11 stay None (no §90 pathway for NO_DTAA)
    assert e.section_90_dtaa_article is None
    assert e.section_90_dtaa_rate_percent is None
    assert e.section_90_credit_inr is None
    # Col 12: §91 credit = income * min(indian_rate, foreign_rate)
    # = 200 * min(0.30, 0.25) = 200 * 0.25 = 50
    assert e.section_91_credit_inr == Decimal("50")
    # Col 13: total = §91 credit (mutex)
    assert e.total_credit_inr == Decimal("50")


def test_section_91_indian_rate_lower_than_foreign_rate() -> None:
    """§91 corner: Indian average rate is the binding constraint.

    Setup:
      foreign_tax = 80 (40% of 200 income); indian_avg = 10%.
    Expected:
      §91 relief = 200 * min(0.10, 0.40) = 200 * 0.10 = 20.
    """
    ky_isin = Isin("KY99970Q1000")
    result = compute_form67_entries_from_wht(
        [
            _wht(
                isin=ky_isin,
                symbol=Ticker("KYDIV"),
                description="KYDIV - KY Tax",
                amount=Decimal("-0.80"),  # 80 INR
            )
        ],
        os_dividend_lines=[
            _os_line(
                isin=ky_isin,
                symbol=Ticker("KYDIV"),
                description="KYDIV",
                issuer_country_iso2="KY",
                amount_gross_inr=Decimal("200"),
            )
        ],
        wht_fx_policy=FixedRatePolicy(rate=Decimal("100")),
        financial_year_start=_FY_START,
        financial_year_end=_FY_END,
        form67_config=_form67_cfg_with_section_91(
            overrides=[("KY", Form67SourceOfIncome.DIVIDEND, "60")],
            section_91=_section_91_cfg(total_tax="10000", total_income="100000"),
        ),
    )
    e = result.entries[0]
    assert e.section_91_credit_inr == Decimal("20")
    assert e.total_credit_inr == Decimal("20")


def test_section_91_zero_total_tax_yields_zero_relief() -> None:
    """Edge case: total Indian tax = 0 (income below basic exemption).
    indian_avg_rate = 0; §91 relief = income * 0 = 0. Form67Entry's
    section_91_credit_inr >= 0 invariant accepts zero."""
    ky_isin = Isin("KY99970Q1000")
    result = compute_form67_entries_from_wht(
        [
            _wht(
                isin=ky_isin,
                symbol=Ticker("KYDIV"),
                description="KYDIV - KY Tax",
                amount=Decimal("-0.50"),
            )
        ],
        os_dividend_lines=[
            _os_line(
                isin=ky_isin,
                symbol=Ticker("KYDIV"),
                description="KYDIV",
                issuer_country_iso2="KY",
                amount_gross_inr=Decimal("200"),
            )
        ],
        wht_fx_policy=FixedRatePolicy(rate=Decimal("100")),
        financial_year_start=_FY_START,
        financial_year_end=_FY_END,
        form67_config=_form67_cfg_with_section_91(
            overrides=[("KY", Form67SourceOfIncome.DIVIDEND, "60")],
            section_91=_section_91_cfg(total_tax="0", total_income="100000"),
        ),
    )
    e = result.entries[0]
    assert e.section_91_credit_inr == Decimal("0")
    assert e.total_credit_inr == Decimal("0")


def test_no_dtaa_country_without_section_91_config_leaves_cols_12_13_none() -> None:
    """KY (NO_DTAA) + tax_payable override but NO section_91 config →
    col 7 populates (independent of relief pathway) but cols 11, 12,
    13 stay None, so users who haven't filled the §91 sub-table fill
    the §91 columns by hand."""
    ky_isin = Isin("KY99970Q1000")
    result = compute_form67_entries_from_wht(
        [
            _wht(
                isin=ky_isin,
                symbol=Ticker("KYDIV"),
                description="KYDIV - KY Tax",
                amount=Decimal("-0.50"),
            )
        ],
        os_dividend_lines=[
            _os_line(
                isin=ky_isin,
                symbol=Ticker("KYDIV"),
                description="KYDIV",
                issuer_country_iso2="KY",
                amount_gross_inr=Decimal("200"),
            )
        ],
        wht_fx_policy=FixedRatePolicy(rate=Decimal("100")),
        financial_year_start=_FY_START,
        financial_year_end=_FY_END,
        form67_config=_form67_cfg_with_section_91(
            overrides=[("KY", Form67SourceOfIncome.DIVIDEND, "60")],
            section_91=None,
        ),
    )
    e = result.entries[0]
    assert e.tax_payable_normal_inr == Decimal("60")
    assert e.section_91_credit_inr is None
    assert e.total_credit_inr is None


def test_no_dtaa_country_without_tax_payable_leaves_section_91_none() -> None:
    """KY + section_91 config but NO tax_payable resolved (no
    exact override) → §91 credit stays None per the
    third gate (audit-consistency with §90 pathway requires col 7
    accompany a populated col 12)."""
    ky_isin = Isin("KY99970Q1000")
    result = compute_form67_entries_from_wht(
        [
            _wht(
                isin=ky_isin,
                symbol=Ticker("KYDIV"),
                description="KYDIV - KY Tax",
                amount=Decimal("-0.50"),
            )
        ],
        os_dividend_lines=[
            _os_line(
                isin=ky_isin,
                symbol=Ticker("KYDIV"),
                description="KYDIV",
                issuer_country_iso2="KY",
                amount_gross_inr=Decimal("200"),
            )
        ],
        wht_fx_policy=FixedRatePolicy(rate=Decimal("100")),
        financial_year_start=_FY_START,
        financial_year_end=_FY_END,
        form67_config=_form67_cfg_with_section_91(
            section_91=_section_91_cfg(total_tax="30000", total_income="100000"),
        ),
    )
    e = result.entries[0]
    assert e.tax_payable_normal_inr is None
    assert e.section_91_credit_inr is None
    assert e.total_credit_inr is None


def test_dtaa_country_with_section_91_config_uses_section_90_not_91() -> None:
    """US (DTAA) + section_91 config + tax_payable override → §90
    pathway populates cols 9, 10, 11, 13; col 12 stays None even
    though section_91 config is supplied. The DTAA-applicability
    gate routes US to the §90 branch exclusively (the Form67Entry
    invariant rejects §91 credit paired with DTAA fields)."""
    result = compute_form67_entries_from_wht(
        [_wht(amount=Decimal("-0.75"))],  # 75 INR foreign tax
        os_dividend_lines=[_os_line(amount_gross_inr=Decimal("500"))],
        wht_fx_policy=FixedRatePolicy(rate=Decimal("100")),
        financial_year_start=_FY_START,
        financial_year_end=_FY_END,
        form67_config=_form67_cfg_with_section_91(
            overrides=[("US", Form67SourceOfIncome.DIVIDEND, "150")],
            section_91=_section_91_cfg(total_tax="30000", total_income="100000"),
        ),
    )
    e = result.entries[0]
    assert e.iso_alpha2 == "US"
    # §90 pathway populates cols 9, 10, 11, 13
    assert e.section_90_dtaa_article == "Article 10(2)(b)"
    assert e.section_90_dtaa_rate_percent == Decimal("25")
    _assert_credit_caps_eligible_tax(e)
    assert e.section_90_credit_inr == Decimal("75")
    # §91 NOT activated — col 12 stays None
    assert e.section_91_credit_inr is None
    # total = §90 credit (mutex)
    assert e.total_credit_inr == Decimal("75")


def test_unvendored_country_with_section_91_config_leaves_all_relief_none() -> None:
    """AU (unvendored / unknown) + section_91 config + tax_payable
    override → col 7 populates; cols 11, 12, 13 ALL stay None. The
    "unknown" branch is conservatively excluded from §91 — silently
    attributing §91 relief to a country that turns out to have a
    DTAA we haven't yet vendored would mis-file the return. The
    rubber-duck blocking design point flagged for step 4a/N applies
    transitively to the §91 wiring."""
    au_isin = Isin("AU99970B1031")
    result = compute_form67_entries_from_wht(
        [
            _wht(
                isin=au_isin,
                symbol=Ticker("AUDIV"),
                description="AUDIV - AU Tax",
                amount=Decimal("-0.50"),
            )
        ],
        os_dividend_lines=[
            _os_line(
                isin=au_isin,
                symbol=Ticker("AUDIV"),
                description="AUDIV",
                issuer_country_iso2="AU",
                amount_gross_inr=Decimal("200"),
            )
        ],
        wht_fx_policy=FixedRatePolicy(rate=Decimal("100")),
        financial_year_start=_FY_START,
        financial_year_end=_FY_END,
        form67_config=_form67_cfg_with_section_91(
            overrides=[("AU", Form67SourceOfIncome.DIVIDEND, "60")],
            section_91=_section_91_cfg(total_tax="30000", total_income="100000"),
        ),
    )
    e = result.entries[0]
    assert e.iso_alpha2 == "AU"
    assert e.tax_payable_normal_inr == Decimal("60")
    assert e.section_90_credit_inr is None
    # CRITICAL: unvendored country MUST NOT get §91 attribution
    assert e.section_91_credit_inr is None
    assert e.total_credit_inr is None


def test_mixed_dtaa_and_no_dtaa_use_separate_relief_pathways() -> None:
    """End-to-end: a single compute call with US (DTAA-vendored) +
    KY (NO_DTAA-vendored) must route US through §90 and KY through
    §91 simultaneously, with each populating its own credit column."""
    ky_isin = Isin("KY99970Q1000")
    wht_list = [_wht(amount=Decimal("-0.75"))]  # US: 75 INR
    wht_list.append(
        _wht(
            isin=ky_isin,
            symbol=Ticker("KYDIV"),
            description="KYDIV - KY Tax",
            amount=Decimal("-0.50"),  # KY: 50 INR
        )
    )
    os_list = [_os_line(amount_gross_inr=Decimal("500"))]
    os_list.append(
        _os_line(
            isin=ky_isin,
            symbol=Ticker("KYDIV"),
            description="KYDIV",
            issuer_country_iso2="KY",
            amount_gross_inr=Decimal("200"),
        )
    )
    result = compute_form67_entries_from_wht(
        wht_list,
        os_dividend_lines=os_list,
        wht_fx_policy=FixedRatePolicy(rate=Decimal("100")),
        financial_year_start=_FY_START,
        financial_year_end=_FY_END,
        form67_config=_form67_cfg_with_section_91(
            overrides=[
                ("US", Form67SourceOfIncome.DIVIDEND, "150"),
                ("KY", Form67SourceOfIncome.DIVIDEND, "60"),
            ],
            section_91=_section_91_cfg(total_tax="30000", total_income="100000"),
        ),
    )
    by_iso = {e.iso_alpha2: e for e in result.entries}

    # US: §90 pathway
    _assert_credit_caps_eligible_tax(by_iso["US"])
    assert by_iso["US"].section_90_credit_inr == Decimal("75")
    assert by_iso["US"].section_91_credit_inr is None
    assert by_iso["US"].total_credit_inr == Decimal("75")

    # KY: §91 pathway. indian_avg = 30000/100000 = 0.30;
    # foreign_rate = 50/200 = 0.25; credit = 200 * 0.25 = 50.
    assert by_iso["KY"].section_90_credit_inr is None
    assert by_iso["KY"].section_91_credit_inr == Decimal("50")
    assert by_iso["KY"].total_credit_inr == Decimal("50")


def test_section_91_decimal_precision_does_not_exceed_foreign_tax() -> None:
    """T3 r0 P1 regression: an algebraically-equivalent formula
    ``income * min(indian_avg, foreign_rate)`` round-trips through
    ``income * (foreign_tax / income)`` whenever min picks
    foreign_rate, and that round-trip can grow the result by ~1 ULP
    at the default Decimal context precision (28 sig digits,
    ROUND_HALF_EVEN) for ratios that aren't exactly representable.
    The grown value would then trip the Form67Entry invariant
    ``section_91_credit_inr <= foreign_tax_inr``.

    The fixed formula ``min(foreign_tax, indian_avg * income)`` caps
    with the raw foreign_tax and is invariant-safe by construction.

    This test uses T3's verbatim reproducer (large realistic INR
    amounts where foreign_rate is just inexact enough to expose the
    bug) and constructs the resulting Form67Entry through the
    pipeline so the model invariant fires if the bug regresses.
    """
    # T3 reproducer: foreign_tax = 7056120 INR, income = 64638133 INR;
    # foreign_rate = 7056120/64638133 ≈ 0.1091... (not exact in Decimal);
    # indian_avg = 3000000/10000000 = 0.30 > foreign_rate → min picks
    # foreign_rate → naive formula computes income * (foreign_tax/income)
    # which round-trips to 7056120.000000000000000000001 > 7056120.
    #
    # The fix's formula computes min(7056120, 0.30 * 64638133) =
    # min(7056120, 19391439.9) = 7056120 exactly.
    #
    # Hit the bug via the compute layer (not just the helper) so the
    # whole pipeline including Form67Entry's __post_init__ runs.
    #
    # Building a synthetic large-amount fixture requires WHT and OS
    # rows that net to the exact INR amounts. The simplest path:
    # craft a single WHT + matching OS pair whose Rule 128(5)(ii) FX
    # conversion yields the exact integer INR values, by using a
    # stub policy returning rate=1 INR/USD and supplying the WHT
    # amount in INR directly (since stub uses no fx).
    ky_isin = Isin("KY99970Q1000")
    # rate=1 means USD amount == INR amount.
    result = compute_form67_entries_from_wht(
        [
            _wht(
                isin=ky_isin,
                symbol=Ticker("KYDIV"),
                description="KYDIV - KY Tax",
                amount=Decimal("-7056120"),  # 7056120 INR foreign tax (with rate=1)
            )
        ],
        os_dividend_lines=[
            _os_line(
                isin=ky_isin,
                symbol=Ticker("KYDIV"),
                description="KYDIV",
                issuer_country_iso2="KY",
                amount_gross_inr=Decimal("64638133"),
            )
        ],
        wht_fx_policy=FixedRatePolicy(rate=Decimal("1")),
        financial_year_start=_FY_START,
        financial_year_end=_FY_END,
        form67_config=_form67_cfg_with_section_91(
            overrides=[("KY", Form67SourceOfIncome.DIVIDEND, "1000000")],
            section_91=_section_91_cfg(total_tax="3000000", total_income="10000000"),
        ),
    )
    e = result.entries[0]
    assert e.foreign_tax_inr == Decimal("7056120")
    # With the fix: credit equals foreign_tax exactly.
    # Before the fix: credit = 7056120.000000000000000000001 (>1 ULP)
    # would have tripped Form67Entry's section_91_credit_inr <=
    # foreign_tax_inr invariant during construction.
    assert e.section_91_credit_inr == Decimal("7056120")
    assert e.section_91_credit_inr is not None
    assert e.section_91_credit_inr <= e.foreign_tax_inr
    assert e.total_credit_inr == Decimal("7056120")


# ============================================================================
# #1146: Hong Kong has a DTAA with India (Article 10(2), 5%), so its dividends
# take the section 90 route, with that route's per-payment proofs.
# ============================================================================

_HK_ISIN: Isin = Isin("HK99970Q4401")
_HK_TICKER: Ticker = Ticker("XQCV")
_HK_DESCRIPTION = "SYNTHDESC-6612"
# Privacy markers in the inputs; a refusal message must not echo them.
_HK_MARKERS = (_HK_DESCRIPTION, "HK99970Q4401")
_WITHOUT_HK_OVERRIDE = without_override("HK/DIVIDEND")
# A 30% Indian average rate: main's section 91 credit was positive with it.
_HK_SECTION_91 = _section_91_cfg(total_tax="30000", total_income="100000")


def _hk_wht(amount: str, *, payment_date: date = _DEFAULT_PAYMENT_DATE) -> WithholdingTax:
    return _wht(
        isin=_HK_ISIN,
        symbol=_HK_TICKER,
        description=_HK_DESCRIPTION,
        payment_date=payment_date,
        amount=Decimal(amount),
    )


def _hk_line(
    *,
    gross_usd: str = "12.00",
    gross_inr: str = "200",
    payment_date: date = _DEFAULT_PAYMENT_DATE,
    fx: str = str(FORM67_FX_RATE),
) -> ScheduleOsDividendLine:
    return _os_line(
        isin=_HK_ISIN,
        symbol=_HK_TICKER,
        description=_HK_DESCRIPTION,
        issuer_country_iso2="HK",
        payment_date=payment_date,
        amount_gross_usd=Decimal(gross_usd),
        amount_gross_inr=Decimal(gross_inr),
        fx_rate_inr_per_usd=Decimal(fx),
    )


def _us_and_hk(hk_tax_usd: str, *, section_91: Form67Section91Config | None) -> Form67ComputeResult:
    """A US dividend beside a USD 12 (₹200) Hong Kong one, with withholding
    converted at ₹100/$ and ₹150 / ₹60 of Indian tax."""
    return compute_form67_entries_from_wht(
        [_wht(amount=Decimal("-0.75")), _hk_wht(f"-{hk_tax_usd}")],
        os_dividend_lines=[_os_line(amount_gross_inr=Decimal("500")), _hk_line()],
        wht_fx_policy=FixedRatePolicy(rate=Decimal("100")),
        financial_year_start=_FY_START,
        financial_year_end=_FY_END,
        form67_config=_form67_cfg_with_section_91(
            overrides=[
                ("US", Form67SourceOfIncome.DIVIDEND, "150"),
                ("HK", Form67SourceOfIncome.DIVIDEND, "60"),
            ],
            section_91=section_91,
        ),
    )


def _assert_hk_section_90_credit(
    result: Form67ComputeResult, *, status: Form67TreatyRateStatus, credit: str
) -> None:
    by_iso = {e.iso_alpha2: e for e in result.entries}
    assert by_iso["US"].section_90_credit_inr == Decimal("75")
    hk = by_iso["HK"]
    assert hk.section_90_dtaa_article == "Article 10(2)"
    assert hk.section_90_dtaa_rate_percent == Decimal("5")
    assert hk.treaty_rate_status is status
    assert hk.eligible_foreign_tax_inr == Decimal(credit)
    _assert_credit_caps_eligible_tax(hk)
    assert hk.section_90_credit_inr == hk.total_credit_inr == Decimal(credit)
    assert hk.section_91_credit_inr is None
    assert result.row_readiness == (
        Form67RowReadiness(Form67Readiness.READY),
        Form67RowReadiness(Form67Readiness.READY),
    )


def test_hong_kong_tax_above_the_treaty_rate_gets_a_capped_section_90_credit() -> None:
    """USD 1.20 on USD 12 is 10%, above the 5% cap, so only 5% of ₹200 is
    eligible: ₹10. Main filed a ₹60 section 91 credit here."""
    result = _us_and_hk("1.20", section_91=_HK_SECTION_91)
    _assert_hk_section_90_credit(result, status=Form67TreatyRateStatus.ABOVE, credit="10")


def test_hong_kong_tax_within_the_treaty_rate_is_all_eligible() -> None:
    """USD 0.50 on USD 12 is within 5%: the ₹50 credit is the same as main's
    section 91 one, but it is filed under section 90."""
    result = _us_and_hk("0.50", section_91=_HK_SECTION_91)
    _assert_hk_section_90_credit(result, status=Form67TreatyRateStatus.WITHIN, credit="50")


@pytest.mark.parametrize(
    "section_91",
    [
        pytest.param(None, id="no-section-91-inputs"),
        pytest.param(_section_91_cfg(total_tax="0", total_income="100000"), id="zero-average-rate"),
        # 0.1% of ₹200 is a ₹0.20 section 91 credit, which files as ₹0.
        pytest.param(
            _section_91_cfg(total_tax="100", total_income="100000"),
            id="average-rate-credit-rounds-to-zero",
        ),
    ],
)
def test_hong_kong_credit_needs_no_section_91_inputs(
    section_91: Form67Section91Config | None,
) -> None:
    """Owner decision (#1146): Hong Kong claims that main refused only
    because of the section 91 route pass, since the section 90 route
    doesn't read [form67.section_91]. Main refused these with
    SECTION_91_INPUTS_MISSING, then ZERO_CREDIT twice."""
    result = _us_and_hk("1.20", section_91=section_91)
    _assert_hk_section_90_credit(result, status=Form67TreatyRateStatus.ABOVE, credit="10")


def _compute_hk(
    wht: list[WithholdingTax],
    os_lines: list[ScheduleOsDividendLine],
    *,
    fx_policy: FixedRatePolicy | DateAwareRatePolicy | None = None,
    col_7: str = "60",
) -> Form67ComputeResult:
    """Hong Kong alone, with the 30% [form67.section_91] inputs that made
    main's section 91 credit positive."""
    return compute_form67_entries_from_wht(
        wht,
        os_dividend_lines=os_lines,
        wht_fx_policy=FixedRatePolicy(rate=Decimal("100")) if fx_policy is None else fx_policy,
        financial_year_start=_FY_START,
        financial_year_end=_FY_END,
        form67_config=_form67_cfg_with_section_91(
            overrides=[("HK", Form67SourceOfIncome.DIVIDEND, col_7)],
            section_91=_HK_SECTION_91,
        ),
    )


def _assert_hk_refused(
    result: Form67ComputeResult, reason: Form67RefusalReason, row_text: str
) -> None:
    (entry,) = result.entries
    assert entry.iso_alpha2 == "HK"
    assert entry.section_91_credit_inr is None
    assert result.row_readiness == (Form67RowReadiness(Form67Readiness.REFUSED, reason),)
    with pytest.raises(ConfigError) as caught:
        require_no_refused_form67_rows(result)
    assert caught.value.message == f"Form 67 cannot be produced: HK/DIVIDEND: {row_text}."
    for marker in _HK_MARKERS:
        assert marker not in str(caught.value)


def test_hong_kong_withholding_on_another_date_than_its_dividend_is_unproven() -> None:
    """The withholding's payment has no dividend to test against the rate."""
    result = _compute_hk(
        [_hk_wht("-0.50", payment_date=date(2025, 12, 16))],
        [_hk_line(payment_date=date(2025, 12, 15))],
    )
    (entry,) = result.entries
    assert entry.payment_allocation_unproven
    _assert_hk_refused(
        result,
        Form67RefusalReason.PAYMENT_ALLOCATION_UNPROVEN,
        f"{SAME_DAY_GAP_CAUSE} {_WITHOUT_HK_OVERRIDE}",
    )


def test_hong_kong_treaty_capped_credit_that_files_as_zero_refuses() -> None:
    """USD 0.02 on USD 0.10 at ₹86/$: 5% of the ₹8.60 gross is ₹0.43, which
    files as ₹0. Main's section 91 credit was ₹1.72."""
    result = _compute_hk(
        [_hk_wht("-0.02")],
        [_hk_line(gross_usd="0.10", gross_inr="8.60", fx="86")],
        fx_policy=FixedRatePolicy(rate=Decimal("86")),
    )
    (entry,) = result.entries
    assert entry.section_90_credit_inr == Decimal("0.43")
    _assert_hk_refused(
        result,
        Form67RefusalReason.ZERO_CREDIT,
        zero_credit_text("HK/DIVIDEND"),
    )


_HK_UNPROVEN_SOURCE_RATE_CASES = {
    # USD 25 withheld at ₹90 and refunded at ₹80 nets to USD 0 but ₹250.
    "same-year-refund": (
        [_hk_wht("-25.00", payment_date=_FEB_12), _hk_wht("25.00", payment_date=_MAR_12)],
        _hk_line(payment_date=_FEB_12, gross_usd="100.00", gross_inr="9000.00", fx="90"),
        {_FEB_12: Decimal("90"), _MAR_12: Decimal("80")},
    ),
    # USD 10.05 tax on USD 10.00 income.
    "source-tax-above-income": (
        [_hk_wht("-10.05", payment_date=_MAR_12)],
        _hk_line(payment_date=_MAR_12, gross_usd="10.00", gross_inr="900.00", fx="90"),
        {_MAR_12: Decimal("80")},
    ),
}


@pytest.mark.parametrize("case", sorted(_HK_UNPROVEN_SOURCE_RATE_CASES))
def test_hong_kong_without_a_source_currency_rate_refuses(case: str) -> None:
    wht, os_line, rates = _HK_UNPROVEN_SOURCE_RATE_CASES[case]
    result = _compute_hk(wht, [os_line], fx_policy=DateAwareRatePolicy(rates), col_7="9999")
    (entry,) = result.entries
    assert entry.foreign_tax_inr > 0
    assert entry.treaty_rate_status is Form67TreatyRateStatus.UNPROVEN
    _assert_hk_refused(
        result,
        Form67RefusalReason.SOURCE_RATE_UNPROVEN,
        f"{SOURCE_RATE_GAP_CAUSE} {_WITHOUT_HK_OVERRIDE}",
    )


# ============================================================================
# Issue #225: ADR WHT absorbed by unrelated same-country foreign income under
# UNDERLYING_ISSUER routing — structural fix at form67 compute layer.
# ============================================================================
#
# Issue #225 bug scenario: holder owns XQBN (US foreign issuer) + a mapped
# Indian ADR (US-prefix ADR for an Indian-underlying issuer). With
# adr_routing_mode=UNDERLYING_ISSUER (issue #183), Schedule OS reroutes the
# ADR dividend to ScheduleOsDomesticDividendLine (issuer_country='IN') so
# it does NOT appear in os_dividend_lines here. The matching ADR WHT row
# (US-prefix ISIN, depositary 'US') is THEN silently absorbed into the
# US Form 67 bucket alongside the legitimate XQBN WHT — over-reporting
# foreign_tax against income that does not include the ADR.
#
# Fix (this PR): in UNDERLYING_ISSUER mode, the WHT loop skips rows whose
# ISIN is mapped in INDIAN_ADR_MAPPING — symmetric with Schedule OS's
# rerouting. Default DEPOSITARY mode keeps the legacy behavior (no skip).

_INFY_ADR_ISIN: Isin = Isin("US4567881085")
_INFY_TICKER: Ticker = Ticker("INFY")


def _xqbn_us_dividend(*, amount_gross_inr: Decimal = Decimal("1000")) -> ScheduleOsDividendLine:
    """An XQBN (US-issuer) dividend that legitimately remains in
    os_dividend_lines under both DEPOSITARY and UNDERLYING_ISSUER routing
    (it is NOT an Indian ADR)."""
    return _os_line(
        isin=_DEFAULT_ISIN_US,
        symbol=_DEFAULT_TICKER_XQBN,
        issuer_country_iso2="US",
        amount_gross_inr=amount_gross_inr,
    )


def _xqbn_us_wht(*, amount: Decimal = Decimal("-1.80")) -> WithholdingTax:
    return _wht(isin=_DEFAULT_ISIN_US, symbol=_DEFAULT_TICKER_XQBN, amount=amount)


def _infy_adr_wht(*, amount: Decimal = Decimal("-3.00")) -> WithholdingTax:
    """INFY WHT — depositary is US (ISIN prefix US), but the underlying
    issuer is Indian (Infosys Ltd). Mapped in INDIAN_ADR_MAPPING."""
    return _wht(
        isin=_INFY_ADR_ISIN,
        symbol=_INFY_TICKER,
        amount=amount,
        description="INFY(US4567881085) Cash Dividend - US Tax",
    )


def test_issue_225_indian_adr_wht_skipped_under_underlying_issuer_mode() -> None:
    """In UNDERLYING_ISSUER mode, INFY WHT must NOT contribute to the US
    Form 67 bucket. The US entry must reflect XQBN WHT only.

    Pre-fix: both WHTs were summed into US, producing
    foreign_tax_inr = (1.80 + 3.00) * FORM67_FX_RATE against
    income = XQBN only — over-reporting the FTC claim.

    Post-fix: INFY WHT skipped at the WHT loop; US entry =
    foreign_tax_inr = 1.80 * FORM67_FX_RATE only.
    """
    result = compute_form67_entries_from_wht(
        [_xqbn_us_wht(), _infy_adr_wht()],
        os_dividend_lines=[_xqbn_us_dividend()],
        wht_fx_policy=FixedRatePolicy(FORM67_FX_RATE),
        financial_year_start=_FY_START,
        financial_year_end=_FY_END,
        adr_routing_mode=ScheduleOsIndianAdrRoutingMode.UNDERLYING_ISSUER,
    )
    assert len(result.entries) == 1
    e = result.entries[0]
    assert e.iso_alpha2 == "US"
    # XQBN only: 1.80 * FORM67_FX_RATE — INFY WHT silently skipped because
    # INFY's dividend was rerouted to the domestic bucket by Schedule OS.
    assert e.foreign_tax_inr == Decimal("1.80") * FORM67_FX_RATE
    assert e.income_inr == Decimal("1000")
    # event_count audits the aggregation cardinality — must reflect
    # ONE retained WHT row (XQBN), not two.
    assert e.event_count == 1


def test_issue_225_default_depositary_mode_preserves_legacy_absorption() -> None:
    """In default DEPOSITARY mode, no skip occurs — both INFY and XQBN WHT
    flow into the US bucket. This is the PRE-#225 behavior, preserved as
    the default so the fix is opt-in via the adr_routing_mode parameter.

    NOTE: in DEPOSITARY mode, INFY's dividend ALSO stays in
    os_dividend_lines (issuer_country='US' per depositary), so the
    income side balances. This test exercises the simpler structural
    invariant (the absorption itself) by passing INFY's dividend
    explicitly with US issuer_country_iso2.
    """
    infy_div = _os_line(
        isin=_INFY_ADR_ISIN,
        symbol=_INFY_TICKER,
        issuer_country_iso2="US",
        amount_gross_inr=Decimal("2000"),
    )
    result = compute_form67_entries_from_wht(
        [_xqbn_us_wht(), _infy_adr_wht()],
        os_dividend_lines=[_xqbn_us_dividend(), infy_div],
        wht_fx_policy=FixedRatePolicy(FORM67_FX_RATE),
        financial_year_start=_FY_START,
        financial_year_end=_FY_END,
        # Default is DEPOSITARY; pass explicitly for clarity.
        adr_routing_mode=ScheduleOsIndianAdrRoutingMode.DEPOSITARY,
    )
    assert len(result.entries) == 1
    e = result.entries[0]
    assert e.iso_alpha2 == "US"
    # Both retained: (1.80 + 3.00) * FORM67_FX_RATE
    assert e.foreign_tax_inr == Decimal("4.80") * FORM67_FX_RATE
    assert e.event_count == 2


def test_issue_225_default_kwarg_omitted_matches_depositary_behavior() -> None:
    """Backward-compat: callers that DON'T pass adr_routing_mode must
    see the legacy behavior unchanged. Same scenario as the explicit
    DEPOSITARY test above, but with the kwarg omitted entirely."""
    infy_div = _os_line(
        isin=_INFY_ADR_ISIN,
        symbol=_INFY_TICKER,
        issuer_country_iso2="US",
        amount_gross_inr=Decimal("2000"),
    )
    result = compute_form67_entries_from_wht(
        [_xqbn_us_wht(), _infy_adr_wht()],
        os_dividend_lines=[_xqbn_us_dividend(), infy_div],
        wht_fx_policy=FixedRatePolicy(FORM67_FX_RATE),
        financial_year_start=_FY_START,
        financial_year_end=_FY_END,
    )
    assert len(result.entries) == 1
    assert result.entries[0].event_count == 2


def test_issue_225_indian_adr_only_under_underlying_issuer_produces_no_entry() -> None:
    """User holds ONLY Indian ADRs (INFY) in UNDERLYING_ISSUER mode:
    INFY dividend reroutes to domestic (not in os_dividend_lines),
    INFY WHT is skipped here, so the result must have zero entries —
    NOT an orphan-WHT error. Pre-#225 the orphan guard fired with the
    misleading 'revert to DEPOSITARY' message; post-fix the user
    correctly recovers WHT via the domestic §90 pathway."""
    result = compute_form67_entries_from_wht(
        [_infy_adr_wht()],
        os_dividend_lines=[],
        wht_fx_policy=FixedRatePolicy(FORM67_FX_RATE),
        financial_year_start=_FY_START,
        financial_year_end=_FY_END,
        adr_routing_mode=ScheduleOsIndianAdrRoutingMode.UNDERLYING_ISSUER,
    )
    assert result.entries == ()


def test_issue_225_non_indian_adr_orphan_still_raises_under_underlying_issuer() -> None:
    """Regression: the per-ISIN orphan guard must still fire for ADR
    mis-classifications NOT in INDIAN_ADR_MAPPING (e.g., a foreign-
    underlying ADR with depositary≠issuer that IBKR mis-flags). Only
    the INFY-family mapped ADRs get the symmetric skip; everything
    else must continue to surface as an orphan so users can
    investigate."""
    os_lines = [
        _os_line(
            # Unmapped ADR ISIN with non-matching issuer country
            # (UK underlying, US depositary). Not in
            # INDIAN_ADR_MAPPING, so no skip — orphan guard fires.
            isin=Isin("US99971U1003"),
            issuer_country_iso2="GB",
            amount_gross_inr=Decimal("1000"),
        )
    ]
    wht_list = [_wht(isin=Isin("US99971U1003"), amount=Decimal("-1.50"))]
    with pytest.raises(InvariantError):
        compute_form67_entries_from_wht(
            wht_list,
            os_dividend_lines=os_lines,
            wht_fx_policy=FixedRatePolicy(FORM67_FX_RATE),
            financial_year_start=_FY_START,
            financial_year_end=_FY_END,
            adr_routing_mode=ScheduleOsIndianAdrRoutingMode.UNDERLYING_ISSUER,
        )


def test_issue_225_indian_adr_wht_refund_also_skipped_under_underlying_issuer() -> None:
    """A reversal/refund of an Indian-ADR WHT (positive amount) must
    ALSO be skipped — netting it against unrelated same-country
    foreign WHT would still mis-attribute the refund cross-position.

    Scenario: XQBN has -$1.80 paid, INFY has +$0.50 refund (reversal
    of a prior payment). Under UNDERLYING_ISSUER, only XQBN's tax
    should reach the US bucket; the INFY refund must not net down
    XQBN's claim."""
    result = compute_form67_entries_from_wht(
        [_xqbn_us_wht(), _infy_adr_wht(amount=Decimal("0.50"))],
        os_dividend_lines=[_xqbn_us_dividend()],
        wht_fx_policy=FixedRatePolicy(FORM67_FX_RATE),
        financial_year_start=_FY_START,
        financial_year_end=_FY_END,
        adr_routing_mode=ScheduleOsIndianAdrRoutingMode.UNDERLYING_ISSUER,
    )
    assert len(result.entries) == 1
    e = result.entries[0]
    # XQBN only, INFY refund skipped: 1.80 * FORM67_FX_RATE (no +0.50 net-down)
    assert e.foreign_tax_inr == Decimal("1.80") * FORM67_FX_RATE
    assert e.event_count == 1


# ============================================================================
# Issue #224 — cross-FY WHT reversal pairing (form67 compute layer)
# ============================================================================
#
# The compute layer applies a heuristic ISIN + equal-and-opposite +
# LIFO pairing pass (via compute.wht_reversal_pairing.pair_wht_reversals)
# BEFORE the FY-scope filter; reversals in the current FY whose paired
# original was in a prior FY are EXCLUDED from per-country totals to
# fix sub-cases (a2) + (b) of the cross-FY taxonomy documented at
# compute/form67.py:649. The pairing report is surfaced on
# Form67ComputeResult.cross_fy_reversal_report so pipelines / RunReports
# / CLI advisories can surface user warnings.


def test_issue_224_cross_fy_reversal_excluded_from_current_fy() -> None:
    """Issue #224 reproducer (a2): prior-FY original + current-FY
    reversal. The reversal MUST be excluded from current FY totals.

    Setup: FY 2024-25 has prior original -$1.00 (out of FY scope
    for current FY 2025-26). FY 2025-26 has unrelated paid -$2.00
    + cross-FY refund +$1.00. Pre-fix: refund nets against unrelated
    WHT, current FY claim is $1.00 (-$2 + $1). Post-fix: refund
    excluded, current FY claim is $2.00.
    """
    rows = [
        _wht(amount=Decimal("-1.00"), payment_date=date(2025, 3, 15)),  # prior FY
        _wht(amount=Decimal("-2.00"), payment_date=date(2025, 6, 15)),  # current FY
        _wht(amount=Decimal("1.00"), payment_date=date(2025, 8, 1)),  # cross-FY refund
    ]
    result = compute_form67_entries_from_wht(
        rows,
        os_dividend_lines=[_os_line()],
        wht_fx_policy=FixedRatePolicy(FORM67_FX_RATE),
        financial_year_start=_FY_START,
        financial_year_end=_FY_END,
    )
    assert len(result.entries) == 1
    e = result.entries[0]
    # Only the -$2.00 row contributes: 2.00 * FORM67_FX_RATE.
    # Pre-#224 the refund (+1.00) would have netted, giving 1.00 * FORM67_FX_RATE.
    assert e.foreign_tax_inr == Decimal("2.00") * FORM67_FX_RATE
    # event_count counts contributing rows post-exclusion: only the paid row.
    assert e.event_count == 1
    # Report surfaces the exclusion + foreign-currency total.
    rep = result.cross_fy_reversal_report
    assert rep.cross_fy_excluded_count == 1
    assert rep.cross_fy_excluded_amount_foreign == {"USD": Decimal("1.00")}
    assert rep.orphan_reversals_count == 0
    assert rep.ambiguous_cross_fy_count == 0


def test_issue_224_same_fy_pair_preserves_legacy_netting() -> None:
    """Same-FY original + reversal: NOT excluded; netting is the
    intended same-FY behaviour (the user's net claim correctly
    reflects what they retained after the refund).
    """
    rows = [
        _wht(amount=Decimal("-2.00"), payment_date=date(2025, 6, 15)),
        _wht(amount=Decimal("-1.00"), payment_date=date(2025, 7, 15)),
        _wht(amount=Decimal("1.00"), payment_date=date(2025, 8, 1)),  # reverses July
    ]
    result = compute_form67_entries_from_wht(
        rows,
        os_dividend_lines=[_os_line()],
        wht_fx_policy=FixedRatePolicy(FORM67_FX_RATE),
        financial_year_start=_FY_START,
        financial_year_end=_FY_END,
    )
    assert len(result.entries) == 1
    e = result.entries[0]
    # Net: -$2 - $1 + $1 = -$2 (i.e., $2.00 retained tax paid).
    assert e.foreign_tax_inr == Decimal("2.00") * FORM67_FX_RATE
    assert e.event_count == 3
    assert result.cross_fy_reversal_report == WhtCrossFyReversalReport()


@pytest.mark.parametrize(
    ("rows", "unproven"),
    [
        pytest.param(
            [
                _wht(amount=Decimal("-5.00"), payment_date=date(2025, 3, 15)),  # prior FY
                _wht(amount=Decimal("-5.00"), payment_date=date(2025, 6, 15)),
                _wht(amount=Decimal("5.00"), payment_date=date(2025, 8, 1)),
            ],
            TWO_FY_CANDIDATES,
            id="ambiguous-across-fys",
        ),
        pytest.param(
            [
                _wht(amount=Decimal("-3.00"), payment_date=date(2025, 6, 15)),
                _wht(amount=Decimal("5.00"), payment_date=date(2025, 8, 1)),
            ],
            NO_ORIGINAL,
            id="orphan",
        ),
    ],
)
def test_issue_893_reversal_of_an_unproven_year_refuses_form67(
    rows: list[WithholdingTax], unproven: str
) -> None:
    """LIFO would pair the first refund with June and net this FY's tax to
    zero; the second would erase it. Either guesses the refund's year. The
    refusal names why, not the refund's ISIN, amount or dates (#1047)."""
    with pytest.raises(UnprovenWhtReversalError) as excinfo:
        compute_form67_entries_from_wht(
            rows,
            os_dividend_lines=[_os_line()],
            wht_fx_policy=FixedRatePolicy(FORM67_FX_RATE),
            financial_year_start=_FY_START,
            financial_year_end=_FY_END,
        )
    assert str(excinfo.value) == unproven_reversal_message(unproven)
    assert_no_wht_echo(str(excinfo.value), rows)


def test_issue_224_default_no_wht_yields_zero_report() -> None:
    """No WHT input → empty report (all zeros)."""
    result = compute_form67_entries_from_wht(
        [],
        os_dividend_lines=[],
        wht_fx_policy=FixedRatePolicy(FORM67_FX_RATE),
        financial_year_start=_FY_START,
        financial_year_end=_FY_END,
    )
    rep = result.cross_fy_reversal_report
    assert rep.cross_fy_excluded_count == 0
    assert rep.cross_fy_excluded_amount_foreign == {}
    assert rep.orphan_reversals_count == 0
    assert rep.ambiguous_cross_fy_count == 0
    assert rep.ambiguous_same_fy_count == 0


# ---- Form 67 ⇄ Schedule FSI parity ---------------------------------------


class _MonthlyStubFxPolicy:
    """USD stub whose rate is 80 + the event month."""

    currency = "USD"

    def rate_for(self, event_date: date) -> Decimal:
        return Decimal(80 + event_date.month)


@pytest.mark.parametrize("adr_routing_mode", list(ScheduleOsIndianAdrRoutingMode))
def test_form67_foreign_tax_matches_schedule_fsi_per_country(
    adr_routing_mode: ScheduleOsIndianAdrRoutingMode,
) -> None:
    """Without a tax-payable override every ``total_credit_inr`` is None,
    so ``require_form67_credit_reconciles`` checks nothing and only this
    test keeps Form 67 col 5 equal to Schedule FSI's foreign tax."""
    xqca = Isin("US99972B1017")
    xqcs = Isin("DE99970G1028")
    wht_rows = [
        _wht(amount=Decimal("-1.80"), payment_date=date(2025, 6, 10)),
        _wht(amount=Decimal("1.80"), payment_date=date(2025, 7, 10)),
        _wht(amount=Decimal("-2.00"), payment_date=date(2025, 12, 15)),
        _wht(amount=Decimal("-5.00"), payment_date=date(2025, 3, 10)),
        _wht(amount=Decimal("5.00"), payment_date=date(2025, 5, 10)),
        _wht(isin=xqca, amount=Decimal("-0.30"), payment_date=date(2025, 7, 1)),
        _wht(isin=xqca, amount=Decimal("0.30"), payment_date=date(2025, 8, 1)),
        _infy_adr_wht(amount=Decimal("-3.00")),
        _wht(isin=_DEFAULT_ISIN_GB, amount=Decimal("-3.00"), payment_date=date(2025, 9, 1)),
        _wht(isin=xqcs, amount=Decimal("-1.00"), payment_date=date(2025, 9, 1)),
        _wht(isin=xqcs, amount=Decimal("1.00"), payment_date=date(2025, 10, 1)),
    ]
    fx_policy = _MonthlyStubFxPolicy()

    fsi = compute_schedule_fsi_from_withholding_tax(
        wht_rows,
        fx_policy=fx_policy,
        financial_year_start=_FY_START,
        financial_year_end=_FY_END,
        adr_routing_mode=adr_routing_mode,
    )
    form67 = compute_form67_entries_from_wht(
        wht_rows,
        os_dividend_lines=[
            _xqbn_us_dividend(),
            _os_line(isin=_DEFAULT_ISIN_GB, issuer_country_iso2="GB", symbol=_DEFAULT_TICKER_XQCR),
        ],
        wht_fx_policy=fx_policy,
        financial_year_start=_FY_START,
        financial_year_end=_FY_END,
        adr_routing_mode=adr_routing_mode,
    )

    fsi_tax = {c.iso_alpha2: (c.foreign_tax_inr, c.event_count) for c in fsi.contributions}
    form67_tax = {e.iso_alpha2: (e.foreign_tax_inr, e.event_count) for e in form67.entries}
    assert fsi_tax == form67_tax
    assert set(form67_tax) == {"US", "GB"}
    assert all(e.total_credit_inr is None for e in form67.entries)
    assert fsi.cross_fy_reversal_report == form67.cross_fy_reversal_report
    assert fsi.cross_fy_reversal_report.cross_fy_excluded_count == 1
