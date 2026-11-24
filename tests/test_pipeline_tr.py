"""Tests for :func:`ibkr_fa.pipeline_tr.run_schedule_tr`."""

from __future__ import annotations

from dataclasses import replace
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest
from openpyxl import load_workbook

from ibkr_fa.compute.form67 import Form67ComputeResult
from ibkr_fa.compute.schedule_fsi import ScheduleFsiComputeResult
from ibkr_fa.exceptions import ConfigError, IbkrFaError, InvariantError
from ibkr_fa.models.schedule_fsi_row import HeadOfIncome, ScheduleFsiRow
from ibkr_fa.output._cleanup import RunOutputs
from ibkr_fa.pipeline import FinancialYear
from ibkr_fa.pipeline_tr import run_schedule_tr
from tests._foreign_tax_refusals import orphan_wht_message
from tests._form67_overrides import FORM67_READY_OVERRIDE
from tests._ibkr_income_inputs import (
    XQCR_ROWS,
    read_csv_rows,
    statement_body,
    write_config,
    write_statement,
)
from tests._ibkr_user_toml import US_PERSON_USER_TOML

_FY_2025_26 = FinancialYear(start_year=2025)
_FIXTURES_DIR = Path(__file__).parent / "fixtures"
_STCG_FIXTURE = _FIXTURES_DIR / "e2e_activity_statement_schedule_cg_stcg_fy2025_26.csv"


def _write_file_fixture_user_toml(tmp_path: Path) -> Path:
    path = tmp_path / "user.toml"
    path.write_text(US_PERSON_USER_TOML, encoding="utf-8")
    return path


def test_run_schedule_tr_fixture_statement_is_deterministic(tmp_path: Path) -> None:
    config_path = _write_file_fixture_user_toml(tmp_path)
    out1 = tmp_path / "schedule_tr_1.csv"
    out2 = tmp_path / "schedule_tr_2.csv"

    report1 = run_schedule_tr(
        input_path=_STCG_FIXTURE,
        config_path=config_path,
        output_path=out1,
        financial_year=_FY_2025_26,
    )
    report2 = run_schedule_tr(
        input_path=_STCG_FIXTURE,
        config_path=config_path,
        output_path=out2,
        financial_year=_FY_2025_26,
    )

    assert out1.exists()
    assert out2.exists()
    assert out1.read_bytes() == out2.read_bytes()
    assert report1 == replace(report2, output_path=out1, run_outputs=RunOutputs((out1,), ()))
    assert report1.rows_written == len(report1.per_country_rows)
    portal_total = sum(
        (row.foreign_tax_inr_total_portal_displayed for row in report1.per_country_rows),
        Decimal("0"),
    )
    full_precision_total = sum(
        (
            row.foreign_tax_inr_total_full_precision
            for row in report1.per_country_rows
            if row.foreign_tax_inr_total_full_precision is not None
        ),
        Decimal("0"),
    )
    assert report1.total_foreign_tax_inr_portal_displayed == portal_total
    assert report1.total_foreign_tax_inr_full_precision == full_precision_total
    assert report1.fsi_rows_consumed > 0


def test_run_schedule_tr_aggregates_us_dividend_wht(tmp_path: Path) -> None:
    input_path = write_statement(tmp_path)
    config_path = write_config(tmp_path)
    out = tmp_path / "schedule_tr.csv"

    report = run_schedule_tr(
        input_path=input_path,
        config_path=config_path,
        output_path=out,
        financial_year=_FY_2025_26,
    )

    assert out.exists()
    assert report.rows_written == 1
    row = report.per_country_rows[0]
    assert row.cbdt_country_code == "2"
    assert row.iso_alpha2 == "US"
    # Rule 128 takes the SBI TT buying rate of the last day of the month
    # before the 2025-09-12 deduction: 2025-08-31 is a Sunday, so it carries
    # back to 2025-08-30's 87.70. 0.36 * 87.70 = 31.572, filed as ₹32 (HALF_UP).
    assert row.foreign_tax_inr_total_portal_displayed == Decimal("32")
    assert row.foreign_tax_inr_total_full_precision == Decimal("31.572")
    assert row.tax_relief_inr_total is None
    assert row.section_under_which_claimed is None

    rows = read_csv_rows(out)
    assert len(rows) == 2
    header, data = rows
    assert data[header.index("Country Code")] == "2"
    portal_col = (
        "Total taxes paid outside India (total of (c) of Schedule FSI in respect of each country)"
    )
    assert data[header.index(portal_col)] == "32"


def test_run_schedule_tr_refuses_an_unproven_source_rate_row_with_col_7(tmp_path: Path) -> None:
    """A same-year refund nets the USD tax to zero but leaves INR tax, so no
    source-currency rate exists. Even with col 7, Form 67 computes no credit
    (#767), so the row is REFUSED, and TR refuses it as run-all does, writing
    nothing (#1480). Until #1480, TR wrote that country's relief blank."""
    config_path = tmp_path / "user.toml"
    config_path.write_text(
        US_PERSON_USER_TOML + FORM67_READY_OVERRIDE,
        encoding="utf-8",
    )
    with pytest.raises(ConfigError, match="US/DIVIDEND: the source-currency tax rate can't"):
        run_schedule_tr(
            input_path=_FIXTURES_DIR
            / "e2e_activity_statement_form67_same_year_wht_refund_fy2025_26.csv",
            config_path=config_path,
            output_path=tmp_path / "schedule_tr.csv",
            financial_year=_FY_2025_26,
        )
    assert list(tmp_path.iterdir()) == [config_path]


def test_run_schedule_tr_filing_refuses_relief_fsi_will_overwrite(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fsi_result = ScheduleFsiComputeResult(
        rows=[
            ScheduleFsiRow(
                cbdt_country_code="2",
                country_name_portal="UNITED STATES OF AMERICA",
                iso_alpha2="US",
                head_of_income=HeadOfIncome.OTHER_SOURCES,
                income_inr=Decimal("1000"),
                foreign_tax_inr=Decimal("400"),
                event_count=1,
                tax_payable_inr=Decimal("300"),
                tax_relief_inr=Decimal("250"),
            )
        ]
    )
    monkeypatch.setattr(
        "ibkr_fa.pipeline_tr._compute_schedule_fsi_for_pipeline",
        lambda **_kwargs: SimpleNamespace(
            fsi_result=fsi_result, form67_result=Form67ComputeResult(entries=())
        ),
    )
    output = tmp_path / "schedule-tr.csv"

    with pytest.raises(InvariantError, match="read-only relief"):
        run_schedule_tr(
            input_path=tmp_path / "unused.csv",
            config_path=tmp_path / "unused.toml",
            output_path=output,
            financial_year=_FY_2025_26,
            format_id="filing-csv",
        )

    assert not output.exists()


def test_run_schedule_tr_xlsx_format(tmp_path: Path) -> None:
    input_path = write_statement(tmp_path)
    config_path = write_config(tmp_path)
    out = tmp_path / "schedule_tr.xlsx"

    report = run_schedule_tr(
        input_path=input_path,
        config_path=config_path,
        output_path=out,
        financial_year=_FY_2025_26,
        format_id="xlsx",
    )

    assert out.exists()
    assert report.output_path == out
    workbook = load_workbook(out)
    assert workbook.sheetnames == ["Schedule TR"]


def test_run_schedule_tr_orphan_wht_guard_raises(tmp_path: Path) -> None:
    input_path = write_statement(
        tmp_path,
        include_dividends=False,
        include_wht=True,
    )
    config_path = write_config(tmp_path)
    out = tmp_path / "schedule_tr.csv"
    out.write_text("stale\n", encoding="utf-8")  # an earlier run's output

    with pytest.raises(IbkrFaError) as exc:
        run_schedule_tr(
            input_path=input_path,
            config_path=config_path,
            output_path=out,
            financial_year=_FY_2025_26,
        )

    assert exc.type is IbkrFaError
    msg = str(exc.value)
    # The refusal counts the orphan event; its ISIN and payment date are
    # input and aren't printed (#1047).
    assert msg == orphan_wht_message(1)
    for value in ("US99995C1027", "2025-09-12"):
        assert value.lower() not in msg.lower(), value
    assert sorted(tmp_path.iterdir()) == sorted([input_path, config_path])


def test_run_schedule_tr_aggregates_two_countries_independently(tmp_path: Path) -> None:
    """T2 r1 P3 follow-up on PR #217: with TWO foreign-source ISINs
    from different countries (US XQBA + GB XQCR), each surfaces as its
    own TR row and the report totals equal the sum of per-country
    portal cells WITHOUT re-rounding the aggregate. Pins the
    'portal totals NOT re-rounded' property which is invisible in
    single-row tests."""
    input_path = write_statement(tmp_path, second=XQCR_ROWS)
    config_path = write_config(tmp_path)
    out = tmp_path / "schedule_tr.csv"

    report = run_schedule_tr(
        input_path=input_path,
        config_path=config_path,
        output_path=out,
        financial_year=_FY_2025_26,
    )

    assert out.exists()
    assert report.rows_written == 2
    by_cbdt = {row.cbdt_country_code: row for row in report.per_country_rows}
    assert {"2", "44"} == set(by_cbdt)  # US + GB
    assert by_cbdt["2"].iso_alpha2 == "US"
    assert by_cbdt["44"].iso_alpha2 == "GB"
    # Each country's foreign tax comes from its OWN WHT row. Rule 128 takes
    # the SBI TT buying rate of the last day of the month before the
    # 2025-09-12 deductions: 2025-08-31 is a Sunday, so both carry back to
    # 2025-08-30's 87.70.
    # US: 0.36 * 87.70 = 31.572, filed as ₹32 (HALF_UP).
    # GB: 0.75 * 87.70 = 65.775, filed as ₹66 (HALF_UP).
    assert by_cbdt["2"].foreign_tax_inr_total_portal_displayed == Decimal("32")
    assert by_cbdt["2"].foreign_tax_inr_total_full_precision == Decimal("31.572")
    assert by_cbdt["44"].foreign_tax_inr_total_portal_displayed == Decimal("66")
    assert by_cbdt["44"].foreign_tax_inr_total_full_precision == Decimal("65.775")
    # Totals add the per-country cells: 32 + 66 = 98, where re-rounding the
    # full-precision 31.572 + 65.775 = 97.347 would give 97.
    assert report.total_foreign_tax_inr_portal_displayed == Decimal("98")
    assert report.total_foreign_tax_inr_full_precision == Decimal("97.347")


def test_run_schedule_tr_empty_when_no_foreign_tax(tmp_path: Path) -> None:
    """T2 r1 P4 follow-up on PR #217: when the Activity Statement has
    no withholding tax (e.g., STCG-only or trades-only fixtures), FSI
    emits rows with foreign_tax_inr=0 and compute_schedule_tr_from_fsi
    drops them. The TR pipeline must surface this as
    ``rows_written == 0`` with both totals at ``Decimal("0")`` and
    still produce a (header-only) CSV at output_path."""
    input_path = write_statement(
        tmp_path,
        include_dividends=False,
        include_wht=False,
    )
    config_path = write_config(tmp_path)
    out = tmp_path / "schedule_tr.csv"

    report = run_schedule_tr(
        input_path=input_path,
        config_path=config_path,
        output_path=out,
        financial_year=_FY_2025_26,
    )

    assert out.exists()
    assert report.rows_written == 0
    assert report.per_country_rows == ()
    assert report.total_foreign_tax_inr_portal_displayed == Decimal("0")
    assert report.total_foreign_tax_inr_full_precision == Decimal("0")
    # FSI still consumed the STCG closed lot, so this is the "FSI
    # populated but no foreign tax" path — distinct from a fully
    # empty FSI.
    assert report.fsi_rows_consumed >= 1
    # CSV is header-only (1 row, no data rows).
    rows = read_csv_rows(out)
    assert len(rows) == 1


def test_run_schedule_tr_empty_when_dividends_present_but_no_wht(tmp_path: Path) -> None:
    """T4 r2 #7 follow-up on PR #217: distinct from the all-empty
    case — this fixture has foreign-source dividends BUT no
    withholding tax. FSI emits a non-zero-income row, the row's
    foreign_tax_inr is zero, and compute_schedule_tr_from_fsi drops
    countries with zero foreign tax. The TR pipeline must surface
    ``rows_written == 0`` and both totals at ``Decimal("0")`` in
    this case too, with FSI showing at least the dividend row."""
    input_path = write_statement(
        tmp_path,
        include_dividends=True,
        include_wht=False,
    )
    config_path = write_config(tmp_path)
    out = tmp_path / "schedule_tr.csv"

    report = run_schedule_tr(
        input_path=input_path,
        config_path=config_path,
        output_path=out,
        financial_year=_FY_2025_26,
    )

    assert out.exists()
    assert report.rows_written == 0
    assert report.per_country_rows == ()
    assert report.total_foreign_tax_inr_portal_displayed == Decimal("0")
    assert report.total_foreign_tax_inr_full_precision == Decimal("0")
    # FSI consumed the XQBA dividend (income > 0 but tax == 0).
    assert report.os_dividend_rows_consumed >= 1
    assert report.fsi_rows_consumed >= 1
    rows = read_csv_rows(out)
    assert len(rows) == 1  # header-only


def test_sub_rupee_foreign_tax_dropped_from_tr_does_not_set_unresolved_flag(
    tmp_path: Path,
) -> None:
    body = statement_body(
        include_dividends=True,
        include_wht=True,
    ).replace("-0.36", "-0.004")
    input_path = write_statement(tmp_path, body=body)

    report = run_schedule_tr(
        input_path=input_path,
        config_path=write_config(tmp_path),
        output_path=tmp_path / "schedule_tr.csv",
        financial_year=_FY_2025_26,
    )

    assert report.rows_written == 0
    assert not report.has_unresolved_indian_tax_payable


def test_tr_per_country_row_rejects_iso_alpha2_with_digits() -> None:
    """T4 r4 medium (codex-flagged) regression on PR #217: TrPerCountryRow
    must reject ``iso_alpha2='U1'`` and similar digit-bearing strings.
    Round 3 originally added the invariant but missed ``isalpha()``,
    so 'U1' passed via ``isupper()`` alone (digits are technically
    'uppercase' under Python's rules). Mirrors ScheduleTrRow at
    src/ibkr_fa/models/schedule_tr_row.py:294-297."""
    from ibkr_fa.exceptions import InvariantError  # noqa: PLC0415 — test-local
    from ibkr_fa.pipeline_tr_models import TrPerCountryRow  # noqa: PLC0415

    with pytest.raises(InvariantError, match="iso_alpha2"):
        TrPerCountryRow(
            cbdt_country_code="2",
            country_name_portal="UNITED STATES OF AMERICA",
            iso_alpha2="U1",  # digit-bearing — must be rejected
            foreign_tax_inr_total_portal_displayed=Decimal("100"),
            foreign_tax_inr_total_full_precision=Decimal("100"),
            tax_relief_inr_total=None,
            section_under_which_claimed=None,
            event_count=1,
        )


def test_tr_per_country_row_rejects_negative_tax_relief_inr_total() -> None:
    """``TrPerCountryRow.tax_relief_inr_total`` must be >= 0 when
    populated. The compute layer only produces non-negative relief
    (a sum of rounded FSI relief), so the invariant is defensive;
    this test exercises its branch in ``pipeline_tr_models.py``.
    """
    from decimal import Decimal  # noqa: PLC0415 — test-local

    import pytest  # noqa: PLC0415

    from ibkr_fa.exceptions import InvariantError  # noqa: PLC0415
    from ibkr_fa.models.schedule_tr_row import TaxReliefSection  # noqa: PLC0415
    from ibkr_fa.pipeline_tr_models import TrPerCountryRow  # noqa: PLC0415

    with pytest.raises(InvariantError, match="tax_relief_inr_total"):
        TrPerCountryRow(
            cbdt_country_code="2",
            country_name_portal="UNITED STATES OF AMERICA",
            iso_alpha2="US",
            foreign_tax_inr_total_portal_displayed=Decimal("100"),
            foreign_tax_inr_total_full_precision=Decimal("100"),
            tax_relief_inr_total=Decimal("-1"),
            section_under_which_claimed=TaxReliefSection.SECTION_90,
            event_count=1,
        )
