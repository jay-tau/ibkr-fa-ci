"""Tests for :func:`ibkr_fa.compute.schedule_fsi_relief.enrich_fsi_with_form67_relief`.

Issues #36/#37: FSI rows inherit the §90/§91 FTC from the matching Form 67
DIVIDEND entry only where it is provably correct. Mixed interest is supported
only with an independently computed exact INR total.
"""

from __future__ import annotations

import dataclasses
from decimal import Decimal

import pytest

import ibkr_fa.compute.schedule_fsi_relief as schedule_fsi_relief_mod
from ibkr_fa.compute.form67 import Form67ComputeResult
from ibkr_fa.compute.schedule_fsi import ScheduleFsiComputeResult
from ibkr_fa.compute.schedule_fsi_relief import (
    enrich_fsi_with_form67_relief,
    relief_row_lacks_taxpayer_id,
    require_form67_credit_reconciles,
)
from ibkr_fa.compute.schedule_tr import compute_schedule_tr_from_fsi
from ibkr_fa.config import Form67Config, Form67TaxPayableOverride
from ibkr_fa.exceptions import InvariantError
from ibkr_fa.models.form67_entry import Form67Entry, Form67SourceOfIncome, Form67TreatyRateStatus
from ibkr_fa.models.schedule_fsi_row import HeadOfIncome, ScheduleFsiRow
from ibkr_fa.models.schedule_tr_row import TaxReliefSection
from tests._ftc_warnings import FORM67_RELIEF_REFUSAL as _RELIEF_REFUSAL
from tests._ftc_warnings import unvendored_dtaa_gap, wont_clear


def _fsi(
    *,
    head: HeadOfIncome = HeadOfIncome.OTHER_SOURCES,
    income: str = "68.4372",
    tax: str = "17.3833",
    cbdt: str = "2",
    iso: str = "US",
) -> ScheduleFsiRow:
    return ScheduleFsiRow(
        cbdt_country_code=cbdt,
        country_name_portal="UNITED STATES OF AMERICA",
        iso_alpha2=iso,
        head_of_income=head,
        income_inr=Decimal(income),
        foreign_tax_inr=Decimal(tax),
        event_count=1,
    )


def _div_entry(
    *,
    income: str = "68.4372",
    tax: str = "17.3833",
    tax_payable: str | None = "20.53116",
    credit: str | None = "17.1093",
    article: str | None = "Article 10(2)(b)",
    section_90_credit: str | None = "17.1093",
    section_91_credit: str | None = None,
    cbdt: str = "2",
    iso: str = "US",
) -> Form67Entry:
    return Form67Entry(
        serial_number=1,
        cbdt_country_code=cbdt,
        country_name_portal="UNITED STATES OF AMERICA",
        iso_alpha2=iso,
        source_of_income=Form67SourceOfIncome.DIVIDEND,
        income_inr=Decimal(income),
        foreign_tax_inr=Decimal(tax),
        foreign_tax_rate_percent=(Decimal(tax) / Decimal(income)) * Decimal("100"),
        event_count=1,
        tax_payable_normal_inr=None if tax_payable is None else Decimal(tax_payable),
        section_90_dtaa_article=article,
        section_90_dtaa_rate_percent=Decimal("25.00") if article else None,
        tax_paid_rate_percent=Decimal("25") if article else None,
        highest_payment_rate_percent=Decimal("25") if article else None,
        treaty_rate_status=Form67TreatyRateStatus.WITHIN if article else None,
        eligible_foreign_tax_inr=Decimal(tax) if article else None,
        section_90_credit_inr=None if section_90_credit is None else Decimal(section_90_credit),
        section_91_credit_inr=None if section_91_credit is None else Decimal(section_91_credit),
        total_credit_inr=None if credit is None else Decimal(credit),
    )


def _enrich(
    fsi_rows: list[ScheduleFsiRow],
    entries: list[Form67Entry],
    *,
    interest_income: dict[str, Decimal] | None = None,
    other_income: dict[str, Decimal] | None = None,
    config: Form67Config | None = None,
) -> list[ScheduleFsiRow]:
    result = enrich_fsi_with_form67_relief(
        ScheduleFsiComputeResult(rows=fsi_rows),
        Form67ComputeResult(entries=tuple(entries)),
        interest_income_inr_by_iso=interest_income,
        other_income_inr_by_iso=other_income,
        form67_config=config,
    )
    return result.rows


def test_matching_dividend_row_inherits_section_90_credit() -> None:
    [row] = _enrich([_fsi()], [_div_entry()])
    assert row.tax_payable_inr == Decimal("20.53116")
    assert row.tax_relief_inr == Decimal("17.1093")
    assert row.dtaa_article == "Article 25(2)(a)"


def test_section_90_credit_requires_vendored_relief_article(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(schedule_fsi_relief_mod, "lookup_dtaa_dividend", lambda _iso: None)
    with pytest.raises(InvariantError, match="missing DTAA relief article"):
        _enrich([_fsi()], [_div_entry()])


def test_section_91_credit_populates_relief_with_no_dtaa_article() -> None:
    entry = _div_entry(article=None, section_90_credit=None, section_91_credit="17.1093")
    [row] = _enrich([_fsi()], [entry])
    assert row.tax_relief_inr == Decimal("17.1093")
    assert row.dtaa_article is None  # §91 unilateral relief has no DTAA article
    assert row.tax_payable_inr == Decimal("20.53116")


def test_hong_kong_section_90_credit_cites_article_23_2_a_in_fsi_and_tr() -> None:
    """#1146: a Hong Kong section 90 credit cites the India-Hong Kong
    DTAA's relief article, and Schedule TR claims it under section 90."""
    hk_fsi = dataclasses.replace(
        _fsi(income="200", tax="10", cbdt="852", iso="HK"), country_name_portal="HONG KONG"
    )
    hk_entry = dataclasses.replace(
        _div_entry(
            income="200",
            tax="10",
            tax_payable="60",
            credit="10",
            article="Article 10(2)",
            section_90_credit="10",
            cbdt="852",
            iso="HK",
        ),
        country_name_portal="HONG KONG",
        section_90_dtaa_rate_percent=Decimal("5"),
        tax_paid_rate_percent=Decimal("5"),
        highest_payment_rate_percent=Decimal("5"),
    )
    [fsi_row] = _enrich([hk_fsi], [hk_entry])
    assert fsi_row.dtaa_article == "Article 23(2)(a)"
    assert fsi_row.tax_relief_inr == Decimal("10")
    assert fsi_row.tax_payable_inr == Decimal("60")

    [tr_row] = compute_schedule_tr_from_fsi(ScheduleFsiComputeResult(rows=[fsi_row])).rows
    assert tr_row.iso_alpha2 == "HK"
    assert tr_row.tax_relief_inr_total == Decimal("10")
    assert tr_row.section_under_which_claimed is TaxReliefSection.SECTION_90


def test_zero_foreign_tax_row_gets_zero_relief_any_head() -> None:
    # A Capital-Gains row with no foreign tax: relief is provably ₹0; the
    # special-rate tax-payable + article stay manual-fill.
    [row] = _enrich([_fsi(head=HeadOfIncome.CAPITAL_GAINS, income="500", tax="0")], [])
    assert row.tax_relief_inr == Decimal("0")
    assert row.tax_payable_inr is None
    assert row.dtaa_article is None


def test_capital_gains_row_with_foreign_tax_stays_manual_fill() -> None:
    [row] = _enrich([_fsi(head=HeadOfIncome.CAPITAL_GAINS, income="500", tax="40")], [_div_entry()])
    assert row.tax_payable_inr is None
    assert row.tax_relief_inr is None
    assert row.dtaa_article is None


def test_income_mismatch_leaves_row_manual_fill() -> None:
    # Excess Other-Sources income without a proven interest total stays unknown.
    [row] = _enrich([_fsi(income="120.00", tax="17.3833")], [_div_entry()])
    assert row.tax_relief_inr is None
    assert row.tax_payable_inr is None


def test_proven_mixed_interest_inherits_credit_and_combined_exact_tax() -> None:
    [row] = _enrich(
        [_fsi(income="120.00")],
        [_div_entry()],
        interest_income={"US": Decimal("51.5628")},
        config=Form67Config(
            tax_payable_overrides=(
                Form67TaxPayableOverride(
                    iso_alpha2="US",
                    source_of_income=Form67SourceOfIncome.INTEREST_INCOME,
                    tax_payable_normal_inr=Decimal("15.46884"),
                ),
            ),
        ),
    )
    assert row.tax_payable_inr == Decimal("36.00000")
    assert row.tax_relief_inr == Decimal("17.1093")
    assert row.dtaa_article == "Article 25(2)(a)"


def test_mixed_interest_uses_per_source_override() -> None:
    [row] = _enrich(
        [_fsi(income="120.00")],
        [_div_entry(tax_payable="20")],
        interest_income={"US": Decimal("51.5628")},
        config=Form67Config(
            tax_payable_overrides=(
                Form67TaxPayableOverride(
                    iso_alpha2="US",
                    source_of_income=Form67SourceOfIncome.INTEREST_INCOME,
                    tax_payable_normal_inr=Decimal("7"),
                ),
            ),
        ),
    )
    assert row.tax_payable_inr == Decimal("27")
    assert row.tax_relief_inr == Decimal("17.1093")


def test_reviewed_fee_income_uses_exact_override_for_combined_tax() -> None:
    [row] = _enrich(
        [_fsi(income="120.00")],
        [_div_entry()],
        other_income={"US": Decimal("51.5628")},
        config=Form67Config(
            tax_payable_overrides=(
                Form67TaxPayableOverride(
                    iso_alpha2="US",
                    source_of_income=Form67SourceOfIncome.OTHERS,
                    tax_payable_normal_inr=Decimal("15.46884"),
                ),
            ),
        ),
    )
    assert row.tax_payable_inr == Decimal("36.00000")
    assert row.tax_relief_inr == Decimal("17.1093")


def test_mixed_interest_without_tax_config_stays_manual_in_fsi_and_tr() -> None:
    [fsi_row] = _enrich(
        [_fsi(income="120.00")],
        [_div_entry()],
        interest_income={"US": Decimal("51.5628")},
    )
    assert fsi_row.tax_payable_inr is None
    assert fsi_row.tax_relief_inr is None
    assert fsi_row.dtaa_article is None

    [tr_row] = compute_schedule_tr_from_fsi(ScheduleFsiComputeResult(rows=[fsi_row])).rows
    assert tr_row.tax_relief_inr_total is None
    assert tr_row.section_under_which_claimed is None


def test_wrong_interest_total_does_not_relax_exact_match() -> None:
    [row] = _enrich(
        [_fsi(income="120.00")],
        [_div_entry()],
        interest_income={"US": Decimal("44.00")},
        config=Form67Config(
            tax_payable_overrides=(
                Form67TaxPayableOverride(
                    iso_alpha2="US",
                    source_of_income=Form67SourceOfIncome.INTEREST_INCOME,
                    tax_payable_normal_inr=Decimal("13.2"),
                ),
            ),
        ),
    )
    assert row.tax_payable_inr is None
    assert row.tax_relief_inr is None


def test_cross_form_invariant_rejects_partial_exact_mixed_income_evidence() -> None:
    fsi_result = ScheduleFsiComputeResult(rows=[_fsi(income="120.00")])
    form67_result = Form67ComputeResult(entries=(_div_entry(),))
    with pytest.raises(InvariantError) as caught:
        require_form67_credit_reconciles(fsi_result, form67_result)
    assert str(caught.value) == _RELIEF_REFUSAL


def test_foreign_tax_mismatch_leaves_row_manual_fill() -> None:
    [row] = _enrich([_fsi(income="68.4372", tax="25.00")], [_div_entry()])
    assert row.tax_relief_inr is None


def test_no_credit_computed_leaves_row_manual_fill() -> None:
    # Form 67 produced no credit (e.g. no exact Indian tax) → nothing to inherit.
    entry = _div_entry(tax_payable=None, credit=None, section_90_credit=None)
    [row] = _enrich([_fsi()], [entry])
    assert row.tax_relief_inr is None
    assert row.tax_payable_inr is None


def test_missing_exact_indian_tax_stays_manual_in_fsi_and_tr() -> None:
    # Without col 7 the compute leaves the credit blank too (#1491: a credit
    # without col 7 has no producer and fails the readiness invariant).
    entry = _div_entry(tax_payable=None, credit=None, section_90_credit=None)
    [fsi_row] = _enrich([_fsi()], [entry])
    assert fsi_row.tax_payable_inr is None
    assert fsi_row.tax_relief_inr is None
    assert fsi_row.dtaa_article is None

    [tr_row] = compute_schedule_tr_from_fsi(ScheduleFsiComputeResult(rows=[fsi_row])).rows
    assert tr_row.tax_relief_inr_total is None
    assert tr_row.section_under_which_claimed is None


def test_no_matching_country_entry_leaves_row_manual_fill() -> None:
    [row] = _enrich([_fsi()], [])  # empty Form 67
    assert row.tax_relief_inr is None


def test_inherited_relief_never_exceeds_foreign_tax_invariant() -> None:
    # The model invariant already forbids relief > foreign_tax; confirm the
    # join respects it for a realistic DTAA-capped credit.
    [row] = _enrich([_fsi()], [_div_entry()])
    assert row.tax_relief_inr is not None
    assert row.tax_relief_inr <= row.foreign_tax_inr


# ---- stamp_taxpayer_ids ----------------------------------------------------


def test_stamp_taxpayer_ids_sets_per_country_tin() -> None:
    from ibkr_fa.compute.schedule_fsi_relief import stamp_taxpayer_ids  # noqa: PLC0415

    rows = [
        _fsi(cbdt="2", iso="US"),
        _fsi(cbdt="44", iso="GB", income="100", tax="10"),
    ]
    result = stamp_taxpayer_ids(ScheduleFsiComputeResult(rows=rows), {"US": "123456789"})
    by_iso = {r.iso_alpha2: r for r in result.rows}
    assert by_iso["US"].taxpayer_id_number == "123456789"
    assert by_iso["GB"].taxpayer_id_number is None  # not in the map → manual-fill


def test_stamp_taxpayer_ids_empty_map_is_noop() -> None:
    from ibkr_fa.compute.schedule_fsi_relief import stamp_taxpayer_ids  # noqa: PLC0415

    original = ScheduleFsiComputeResult(rows=[_fsi()])
    result = stamp_taxpayer_ids(original, {})
    assert result is original
    assert result.rows[0].taxpayer_id_number is None


def test_enrich_and_stamp_preserve_cross_fy_reversal_report() -> None:
    """Both transforms must carry the #224 cross-FY WHT-reversal report through
    so the schedule-fsi / schedule-tr CLI advisories ("prior FY return may need
    amendment") are not silently suppressed.
    """
    from ibkr_fa.compute.schedule_fsi_relief import stamp_taxpayer_ids  # noqa: PLC0415
    from ibkr_fa.compute.wht_reversal_pairing import (  # noqa: PLC0415
        WhtCrossFyReversalReport,
    )

    report = WhtCrossFyReversalReport(cross_fy_excluded_count=2, orphan_reversals_count=1)
    source = ScheduleFsiComputeResult(rows=[_fsi()], cross_fy_reversal_report=report)

    enriched = enrich_fsi_with_form67_relief(
        source,
        Form67ComputeResult(entries=()),
    )
    assert enriched.cross_fy_reversal_report == report

    stamped = stamp_taxpayer_ids(source, {"US": "123456789"})
    assert stamped.cross_fy_reversal_report == report


# ---- relief_row_lacks_taxpayer_id (#1084) ----------------------------------


def _with_tin(row: ScheduleFsiRow, tin: str | None) -> ScheduleFsiRow:
    return dataclasses.replace(row, taxpayer_id_number=tin)


_GB_RELIEF = _fsi(cbdt="44", iso="GB", income="100", tax="10")


@pytest.mark.parametrize(
    ("rows", "expected"),
    [
        pytest.param([_fsi()], True, id="us-relief-row-without-tin"),
        pytest.param(
            [_with_tin(_fsi(), "999-00-0001"), _GB_RELIEF],
            True,
            id="non-us-relief-row-without-tin-next-to-us-row-with-one",
        ),
        pytest.param(
            [_with_tin(_fsi(), "999-00-0001"), _with_tin(_GB_RELIEF, "999-00-0002")],
            False,
            id="every-relief-row-has-a-tin",
        ),
        pytest.param([_fsi(tax="0")], False, id="row-without-foreign-tax-or-tin"),
    ],
)
def test_relief_row_lacks_taxpayer_id(rows: list[ScheduleFsiRow], expected: bool) -> None:
    assert relief_row_lacks_taxpayer_id(rows) is expected


def test_blank_dividend_row_advice_reaches_only_its_other_sources_row() -> None:
    """#1469: an Other Sources row left manual carries its country's BLANK
    Form 67 DIVIDEND row's advice; that country's Capital Gains row and a row
    whose DIVIDEND row an override alone clears carry none."""
    ch = _div_entry(
        tax_payable=None, credit=None, article=None, section_90_credit=None, cbdt="41", iso="CH"
    )
    us = _div_entry(tax_payable=None, credit=None, section_90_credit=None)
    rows = [
        _fsi(),
        _fsi(head=HeadOfIncome.CAPITAL_GAINS, cbdt="41", iso="CH"),
        _fsi(cbdt="41", iso="CH"),
    ]
    us_row, ch_gains, ch_dividends = _enrich(rows, [us, dataclasses.replace(ch, serial_number=2)])
    assert us_row.override_advice == ()
    assert ch_gains.override_advice == ()
    assert ch_dividends.override_advice == (wont_clear(unvendored_dtaa_gap("CH"), "CH/DIVIDEND"),)
