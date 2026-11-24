"""Tests for :mod:`ibkr_fa.output.quicko_foreign_tax`."""

from __future__ import annotations

import csv
from dataclasses import replace
from decimal import Decimal
from pathlib import Path

import pytest

from ibkr_fa.compute.form67 import Form67ComputeResult
from ibkr_fa.compute.schedule_tr import ScheduleTrComputeResult
from ibkr_fa.models.form67_entry import Form67Entry, Form67SourceOfIncome, Form67TreatyRateStatus
from ibkr_fa.models.schedule_tr_row import ScheduleTrRow, TaxReliefSection
from ibkr_fa.output import output_binding
from ibkr_fa.output.base import RenderContext, Subcommand
from ibkr_fa.output.quicko_foreign_tax import (
    QuickoForeignTaxInput,
    has_quicko_foreign_tax_entries,
    quicko_foreign_tax_entries,
)

_US = Form67Entry(
    serial_number=1,
    cbdt_country_code="2",
    country_name_portal="UNITED STATES OF AMERICA",
    iso_alpha2="US",
    source_of_income=Form67SourceOfIncome.DIVIDEND,
    income_inr=Decimal("100"),
    foreign_tax_inr=Decimal("25"),
    foreign_tax_rate_percent=Decimal("25"),
    event_count=1,
    tax_payable_normal_inr=Decimal("31.2"),
    section_90_dtaa_article="Article 10(2)(b)",
    section_90_dtaa_rate_percent=Decimal("25"),
    tax_paid_rate_percent=Decimal("25"),
    highest_payment_rate_percent=Decimal("25"),
    treaty_rate_status=Form67TreatyRateStatus.WITHIN,
    eligible_foreign_tax_inr=Decimal("25"),
    section_90_credit_inr=Decimal("25"),
    total_credit_inr=Decimal("25"),
)


def _read(path: Path) -> list[list[str]]:
    with path.open(encoding="utf-8-sig", newline="") as fh:
        return list(csv.reader(fh))


def test_quicko_entries_skip_only_the_country_missing_normal_tax() -> None:
    incomplete_gb = replace(
        _US,
        serial_number=2,
        cbdt_country_code="44",
        country_name_portal="UNITED KINGDOM OF GREAT BRITAIN AND NORTHERN IRELAND",
        iso_alpha2="GB",
        tax_payable_normal_inr=None,
        section_90_dtaa_article="Article 11(2)",
        section_90_dtaa_rate_percent=Decimal("15"),
        treaty_rate_status=Form67TreatyRateStatus.ABOVE,
    )

    entries = quicko_foreign_tax_entries(
        Form67ComputeResult(entries=(_US, incomplete_gb)),
        deductor_name="National Financial Services LLC",
        foreign_tins={"US": "123456789"},
    )

    assert [entry.country_name for entry in entries] == ["United States of America"]


@pytest.mark.parametrize(
    ("entries", "expected"),
    [
        ((), False),
        ((_US,), True),
        ((replace(_US, tax_payable_normal_inr=None),), False),
        ((replace(_US, section_90_credit_inr=None, total_credit_inr=Decimal("0")),), False),
        (
            (
                replace(_US, tax_payable_normal_inr=None),
                replace(
                    _US, serial_number=2, source_of_income=Form67SourceOfIncome.INTEREST_INCOME
                ),
            ),
            True,
        ),
    ],
    ids=["no-entry", "section-90", "no-normal-tax", "no-credit", "one-of-two"],
)
def test_has_quicko_entries_matches_the_kept_entries(
    entries: tuple[Form67Entry, ...], expected: bool
) -> None:
    form67 = Form67ComputeResult(entries=entries)
    kept = quicko_foreign_tax_entries(form67, deductor_name="Agent", foreign_tins={})

    assert has_quicko_foreign_tax_entries(form67) is expected
    assert bool(kept) is expected


def test_quicko_amounts_are_the_entry_filed_values(filed_without_rounding) -> None:
    """#898: the block writes compute's whole-rupee income and foreign tax, and
    #1079 the filed article number."""
    entry = replace(
        _US,
        income_inr=Decimal("100.5"),
        foreign_tax_inr=Decimal("25.49"),
        foreign_tax_rate_percent=Decimal("25.49") / Decimal("100.5") * 100,
        eligible_foreign_tax_inr=Decimal("25.49"),
    )
    filed = filed_without_rounding(
        entry,
        income_inr=Decimal("102"),
        foreign_tax_inr=Decimal("26"),
        section_90_dtaa_article="Article 99(9)",
    )

    (quicko,) = quicko_foreign_tax_entries(
        Form67ComputeResult(entries=(entry,)), deductor_name="Agent", foreign_tins={}
    )

    assert (quicko.credit_amount_inr, quicko.tax_withheld_inr) == (
        filed.income_inr,
        filed.foreign_tax_inr,
    )
    assert (str(quicko.credit_amount_inr), str(quicko.tax_withheld_inr)) == ("102", "26")
    assert quicko.article == "99"


def test_filing_csv_leaves_quicko_article_blank_for_section_91(tmp_path: Path) -> None:
    path = tmp_path / "tr-filing.csv"
    bermuda = Form67Entry(
        serial_number=1,
        cbdt_country_code="1441",
        country_name_portal="BERMUDA",
        iso_alpha2="BM",
        source_of_income=Form67SourceOfIncome.DIVIDEND,
        income_inr=Decimal("100"),
        foreign_tax_inr=Decimal("10"),
        foreign_tax_rate_percent=Decimal("10"),
        event_count=1,
        tax_payable_normal_inr=Decimal("31.2"),
        section_91_credit_inr=Decimal("10"),
        total_credit_inr=Decimal("10"),
    )
    ctx = RenderContext(
        assessment_year_start=2026,
        quicko_foreign_tax=QuickoForeignTaxInput(
            form67=Form67ComputeResult(entries=(bermuda,)),
            deductor_name="Example Withholding Agent LLC",
            foreign_tins={},
        ),
    )

    output_binding(Subcommand.TR, "filing-csv").writer(ctx).write(
        ScheduleTrComputeResult(rows=[]), path
    )

    rows = _read(path)
    assert ["Bermuda", "Dividend", "", "", ""] in rows
    assert ["91", "", "", "", ""] in rows


def test_filing_csv_leaves_the_deductor_blank_without_a_name(tmp_path: Path) -> None:
    """#1327: with no withholding agent, the form keeps its deductor row with
    a blank value."""
    form67 = Form67ComputeResult(entries=(_US,))
    (entry,) = quicko_foreign_tax_entries(form67, deductor_name=None, foreign_tins={})
    assert entry.deductor_name == ""
    path = tmp_path / "tr-filing.csv"
    ctx = RenderContext(
        assessment_year_start=2026,
        quicko_foreign_tax=QuickoForeignTaxInput(
            form67=form67, deductor_name=None, foreign_tins={}
        ),
    )

    output_binding(Subcommand.TR, "filing-csv").writer(ctx).write(
        ScheduleTrComputeResult(rows=[]), path
    )

    rows = _read(path)
    assert ["United States of America", "Dividend", "", "", ""] in rows
    assert rows.count(["Name of the deductor", "", "", "", ""]) == 1


def test_filing_csv_without_quicko_inputs_writes_only_tr_rows(tmp_path: Path) -> None:
    path = tmp_path / "tr-filing.csv"
    row = ScheduleTrRow(
        cbdt_country_code="2",
        country_name_portal="UNITED STATES OF AMERICA",
        iso_alpha2="US",
        foreign_tax_inr_total=Decimal("1851"),
        event_count=3,
        tax_relief_inr_total=Decimal("750"),
        section_under_which_claimed=TaxReliefSection.SECTION_90,
        taxpayer_id_number="123456789",
    )

    writer = output_binding(Subcommand.TR, "filing-csv").writer(
        RenderContext(assessment_year_start=2026)
    )
    writer.write(ScheduleTrComputeResult(rows=[row]), path)

    assert _read(path)[1:] == [["2-UNITED STATES OF AMERICA", "123456789", "1851", "750", "90"]]
