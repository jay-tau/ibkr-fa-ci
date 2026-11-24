"""Tests for :func:`ibkr_fa.pipeline_form67.run_form67`."""

from __future__ import annotations

from dataclasses import replace
from decimal import Decimal
from pathlib import Path
from typing import Any, ClassVar

import pytest
from openpyxl import load_workbook

from ibkr_fa import cli, output
from ibkr_fa.cli import main
from ibkr_fa.compute.form67 import Form67Readiness, Form67RefusalReason, Form67RowReadiness
from ibkr_fa.exceptions import IbkrFaError, InvariantError, ParseError
from ibkr_fa.models.form67_entry import Form67SourceOfIncome, Form67TreatyRateStatus
from ibkr_fa.output.base import OutputBinding, Subcommand
from ibkr_fa.pipeline import FinancialYear
from ibkr_fa.pipeline_form67 import run_form67 as _run_form67
from ibkr_fa.pipeline_form67_models import Form67PerEntryRow, Form67RunReport
from tests._form67_overrides import FORM67_READY_OVERRIDE
from tests._ftc_warnings import no_credit_line, refused_override, same_day_gap
from tests._ibkr_format_refusals import no_sentinel_refusal as _no_sentinel
from tests._ibkr_income_inputs import (
    ADR_ROWS,
    AS_HEADER,
    DIVIDEND_ADR,
    DIVIDEND_XQBA,
    DIVIDEND_XQCR,
    DIVIDENDS_HEADER,
    FII_DATA_ADR,
    FII_DATA_XQBA,
    FII_DATA_XQCR,
    FII_HEADER,
    INCOME_TRADES_HEADER,
    OP_HEADER,
    SYNTHETIC_CONFIG,
    TRADE_CLOSE_XQBA,
    TRADE_OPEN_XQBA,
    UNDERLYING_ISSUER_CONFIG_TEMPLATE,
    WHT_ADR,
    WHT_HEADER,
    WHT_XQBA,
    WHT_XQCR,
    XQCR_ROWS,
    read_csv_rows,
    write_config,
    write_statement,
)
from tests._ibkr_user_toml import US_PERSON_USER_TOML
from tests._income_blocks import with_totals
from tests._writer_stubs import StubWriter

_FY_2025_26 = FinancialYear(start_year=2025)


def run_form67(
    *,
    input_path: Path,
    config_path: Path,
    output_path: Path,
    financial_year: FinancialYear,
    format_id: str = "review-csv",
    debug_output_path: Path | None = None,
    allow_masked_account: bool = False,
    suppress_cross_ay_warning: bool = False,
    accept_anonymised_statement: bool = False,
) -> Form67RunReport:
    """Existing pipeline tests exercise the incomplete review/debug path."""
    return _run_form67(
        input_path=input_path,
        config_path=config_path,
        output_path=output_path,
        financial_year=financial_year,
        format_id=format_id,
        debug_output_path=debug_output_path,
        allow_masked_account=allow_masked_account,
        suppress_cross_ay_warning=suppress_cross_ay_warning,
        accept_anonymised_statement=accept_anonymised_statement,
    )


# ---- End-to-end smoke ----------------------------------------------------


def test_run_form67_produces_review_csv_for_single_us_dividend(tmp_path: Path) -> None:
    """Smoke test: XQBA dividend + WHT → one US (DIVIDEND) Form 67 entry."""
    input_path = write_statement(tmp_path)
    config_path = write_config(tmp_path)
    output_path = tmp_path / "form67.csv"

    report = run_form67(
        input_path=input_path,
        config_path=config_path,
        output_path=output_path,
        financial_year=_FY_2025_26,
    )

    assert isinstance(report, Form67RunReport)
    assert report.rows_written == 1
    assert len(report.per_entry_rows) == 1

    row = report.per_entry_rows[0]
    assert row.serial_number == 1
    assert row.iso_alpha2 == "US"
    assert row.cbdt_country_code == "2"
    assert row.country_name_portal == "UNITED STATES OF AMERICA"
    assert row.source_of_income == Form67SourceOfIncome.DIVIDEND
    assert row.income_inr > 0
    assert row.foreign_tax_inr > 0
    assert row.event_count == 1

    # Cols 9 + 10 populate from the vendored DTAA reference table
    # (step 4b/N of #37); for US this is the WITHHOLDING article
    # (Article 10(2)(b) — the clause that supplies col 10's rate;
    # Article 25 "Relief from Double Taxation" states no rate) and
    # the portfolio cap rate (25%) per the India-US DTAA verbatim
    # verification at PR #230.
    assert row.tax_payable_normal_inr is None
    assert row.tax_payable_special_inr is None
    assert row.section_90_dtaa_article == "Article 10(2)(b)"
    assert row.section_90_dtaa_rate_percent == Decimal("25")
    assert row.section_90_credit_inr is None
    assert row.section_91_credit_inr is None
    assert row.total_credit_inr is None

    # Output file exists and starts with the portal header row.
    assert output_path.exists()
    rows = read_csv_rows(output_path)
    assert len(rows) == 2  # header + 1 detail
    assert rows[0][0] == "Sl. No."  # CBDT verbatim col 1


# ---- Multi-country aggregation ------------------------------------------


def test_run_form67_emits_one_entry_per_country_with_sort_order(tmp_path: Path) -> None:
    """XQBA (US) + XQCR (GB) → 2 entries; compute layer sorts by
    cbdt_country_code lexicographically with 1-indexed serial preserved."""
    input_path = write_statement(tmp_path, second=XQCR_ROWS)
    config_path = write_config(tmp_path)
    output_path = tmp_path / "form67.csv"

    report = run_form67(
        input_path=input_path,
        config_path=config_path,
        output_path=output_path,
        financial_year=_FY_2025_26,
    )

    assert report.rows_written == 2
    # US cbdt_country_code "2" sorts BEFORE GB "44" lexicographically (str.sort: "2" < "4").
    assert [r.iso_alpha2 for r in report.per_entry_rows] == ["US", "GB"]
    cbdt_codes = [r.cbdt_country_code for r in report.per_entry_rows]
    assert cbdt_codes == sorted(cbdt_codes)
    # 1-indexed serial preserved in tuple order.
    assert [r.serial_number for r in report.per_entry_rows] == [1, 2]


# ---- Zero-WHT case (no Form 67 entries) ----------------------------------


def test_run_form67_with_no_wht_section_emits_empty_review_csv(tmp_path: Path) -> None:
    """Statement without WHT section → zero Form 67 entries, header-only CSV.

    Pinned because Form 67 only surfaces (country, source) aggregates with
    positive netted foreign tax paid; the CLI safety-warnings block has a
    zero-rows advisory that references this exact contract.
    """
    input_path = write_statement(tmp_path, include_wht=False)
    config_path = write_config(tmp_path)
    output_path = tmp_path / "form67.csv"

    report = run_form67(
        input_path=input_path,
        config_path=config_path,
        output_path=output_path,
        financial_year=_FY_2025_26,
    )

    assert report.rows_written == 0
    assert report.per_entry_rows == ()
    assert report.total_foreign_tax_inr_portal_displayed == Decimal("0")
    assert report.total_income_inr_portal_displayed == Decimal("0")

    rows = read_csv_rows(output_path)
    assert len(rows) == 1  # header only


# ---- RunReport sanity ----------------------------------------------------


def test_run_report_carries_upstream_consumption_counts(tmp_path: Path) -> None:
    """The report exposes the upstream-consumption counts from the
    shared Schedule FSI compute seam — these power the CLI
    audit-bucket stderr advisory."""
    input_path = write_statement(tmp_path)
    config_path = write_config(tmp_path)
    output_path = tmp_path / "form67.csv"

    report = run_form67(
        input_path=input_path,
        config_path=config_path,
        output_path=output_path,
        financial_year=_FY_2025_26,
    )

    # Schedule FSI consumed exactly one OS dividend row from XQBA.
    assert report.os_dividend_rows_consumed == 1
    assert report.wht_rows_in_fy == 1
    # No exclusions in the happy-path fixture.
    assert report.excluded_cg_indian_source_count == 0
    assert report.excluded_cg_out_of_fy_count == 0
    assert report.excluded_ltcg_pre_cutover_count == 0
    assert report.excluded_ltcg_indian_source_count == 0
    assert report.excluded_ltcg_out_of_fy_count == 0
    assert report.interest_credits_out_of_fy_count == 0


def test_run_report_portal_labels_assessment_year_start_matches_fy(tmp_path: Path) -> None:
    """The report records the AY start year derived from the FY input —
    used by the CLI verbose render's "AY policy" block."""
    input_path = write_statement(tmp_path)
    config_path = write_config(tmp_path)
    output_path = tmp_path / "form67.csv"

    report = run_form67(
        input_path=input_path,
        config_path=config_path,
        output_path=output_path,
        financial_year=_FY_2025_26,
    )

    # FY 2025-26 → AY 2026-27 → assessment_year_start = 2026.
    assert report.portal_labels_assessment_year_start == 2026
    # AY 2026-27 IS the registered snapshot today, so no forward-compat fallback.
    assert report.portal_labels_used_forward_compat_fallback is False


def test_dual_anchor_totals_differ_under_rounding_noise(tmp_path: Path) -> None:
    """T3 r0 P2 / T4 r0 P1 of PR #228 regression test: the dual-anchor
    contract documented in pipeline_form67_models.py says
    ``total_foreign_tax_inr_portal_displayed`` (sum of HALF_UP per-
    entry display values) and ``total_foreign_tax_inr_full_precision``
    (sum of un-rounded values) may differ by up to ~0.5 INR * n
    entries due to rounding noise. The r0 implementation collapsed
    them; r1 separates them per the PR #216 Schedule TR convention.

    Verifies that BOTH totals are correctly derived from the per-
    entry rows: portal-displayed equals the sum of per-entry HALF_UP
    rounded values, full-precision equals the sum of un-rounded
    values, and same for income.
    """
    input_path = write_statement(tmp_path, second=XQCR_ROWS)
    config_path = write_config(tmp_path)
    output_path = tmp_path / "form67.csv"

    report = run_form67(
        input_path=input_path,
        config_path=config_path,
        output_path=output_path,
        financial_year=_FY_2025_26,
    )

    from decimal import ROUND_HALF_UP  # noqa: PLC0415 — scoped import for the regression assertion

    expected_portal_displayed = sum(
        row.foreign_tax_inr.quantize(Decimal("1"), rounding=ROUND_HALF_UP)
        for row in report.per_entry_rows
    )
    expected_full_precision = sum(row.foreign_tax_inr for row in report.per_entry_rows)
    assert report.total_foreign_tax_inr_portal_displayed == expected_portal_displayed
    assert report.total_foreign_tax_inr_full_precision == expected_full_precision

    expected_income_portal_displayed = sum(
        row.income_inr.quantize(Decimal("1"), rounding=ROUND_HALF_UP)
        for row in report.per_entry_rows
    )
    assert report.total_income_inr_portal_displayed == expected_income_portal_displayed

    # The portal-displayed total has no fractional component (unit constraint).
    assert (
        report.total_foreign_tax_inr_portal_displayed
        == report.total_foreign_tax_inr_portal_displayed.quantize(Decimal("1"))
    )


def test_dual_anchor_totals_distinguish_round_then_sum_from_sum_then_round(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """T3 r2 P2 + T2/T3/T4 r3 P2 of PR #228 regression test. This
    test constructs an explicit 3-entry fixture where the per-entry
    HALF_UP sum and the round-of-full-precision sum differ: three entries with
    foreign_tax_inr = 1.50 INR each yield per-entry HALF_UP sum =
    2 + 2 + 2 = 6, but round-of-sum = round(4.50) = 5 under HALF_UP.

    Per T2/T3/T4 r3 (all three reviewers flagged): the r3 first cut
    of this test constructed Form67ComputeResult directly and never
    invoked run_form67, so a regression to sum-then-round would
    silently pass. The test monkeypatches
    income_engine.compute_form67_entries_from_wht to return the
    synthetic 3-entry result, then invokes run_form67. The rounded
    totals are Form67RunReport properties (pipeline_form67_models.py)
    that sum each row's filed whole-rupee value; pipeline_form67.py
    computes only the unrounded audit total. The report MUST show
    total_foreign_tax_inr_portal_displayed = 6, not 5.
    """
    from ibkr_fa.compute import income_engine  # noqa: PLC0415 — scoped import for monkeypatching
    from ibkr_fa.compute.form67 import (  # noqa: PLC0415 — scoped import for the regression test
        Form67ComputeResult,
        Form67Entry,
    )

    # Build three Form67Entry instances with foreign_tax_inr = 1.50,
    # which round HALF_UP to 2 each. Per-entry sum = 6; round-of-sum = 5.
    entries = tuple(
        Form67Entry(
            serial_number=i,
            cbdt_country_code=str(code),
            country_name_portal=name,
            iso_alpha2=iso,
            source_of_income=Form67SourceOfIncome.DIVIDEND,
            income_inr=Decimal("10000"),
            foreign_tax_inr=Decimal("1.50"),
            foreign_tax_rate_percent=(Decimal("1.50") / Decimal("10000")) * Decimal("100"),
            event_count=1,
        )
        for i, (code, name, iso) in enumerate(
            [
                (2, "UNITED STATES OF AMERICA", "US"),
                (28, "SOUTH AFRICA", "ZA"),
                (44, "UNITED KINGDOM", "GB"),
            ],
            start=1,
        )
    )
    synthetic_result = Form67ComputeResult(entries=entries)

    # Monkeypatch the compute function so run_form67 sees our 3-entry
    # synthetic result instead of the real (single-country) one from
    # the XQBA synthetic Activity Statement fixture. Form 67 is now
    # computed once inside the shared FSI context (#36/#37), so we patch
    # it there; run_form67 reads the result back via context.form67_result.
    # The report's real totals then run against entries with
    # foreign_tax_inr=1.50 INR — exercising the per-entry-HALF_UP-then-sum
    # properties on Form67RunReport (pipeline_form67_models.py).
    def fake_compute_form67(**_kwargs: object) -> Form67ComputeResult:
        return synthetic_result

    monkeypatch.setattr(income_engine, "compute_form67_entries_from_wht", fake_compute_form67)

    input_path = write_statement(tmp_path)
    config_path = write_config(tmp_path)
    output_path = tmp_path / "form67.csv"

    report = run_form67(
        input_path=input_path,
        config_path=config_path,
        output_path=output_path,
        financial_year=_FY_2025_26,
    )

    # The pipeline's actual computation path:
    # - portal_displayed sums per-entry HALF_UP (2 + 2 + 2 = 6 INR).
    # - full_precision sums un-rounded (1.50 + 1.50 + 1.50 = 4.50 INR).
    # - income portal_displayed sums per-entry HALF_UP
    #   (10000 + 10000 + 10000 = 30000 INR).
    # If the pipeline were sum-then-round, foreign_tax_inr_portal_displayed
    # would be round(4.50) = 5 INR under HALF_UP, NOT 6. This assertion
    # discriminates the two paths.
    assert report.total_foreign_tax_inr_portal_displayed == Decimal("6")
    assert report.total_foreign_tax_inr_full_precision == Decimal("4.50")
    assert report.total_income_inr_portal_displayed == Decimal("30000")
    # Explicit anti-regression: sum-then-round would produce 5 here.
    assert report.total_foreign_tax_inr_portal_displayed != Decimal("5")


def test_run_form67_invalid_input_raises(tmp_path: Path) -> None:
    """A statement in no known IBKR format refuses with the typed parse error
    and leaves no output, removing an earlier run's."""
    bogus_input = tmp_path / "not-a-statement.csv"
    bogus_input.write_text("not,a,valid,statement\n", encoding="utf-8")
    config_path = write_config(tmp_path)
    output_path = tmp_path / "form67.csv"
    output_path.write_text("stale\n", encoding="utf-8")  # an earlier run's output

    with pytest.raises(ParseError) as exc:
        run_form67(
            input_path=bogus_input,
            config_path=config_path,
            output_path=output_path,
            financial_year=_FY_2025_26,
        )
    assert exc.type is ParseError
    refusal = exc.value
    assert (refusal.section, refusal.row_index, refusal.column, refusal.message) == _no_sentinel(
        0, bogus_input
    )
    assert sorted(tmp_path.iterdir()) == sorted([bogus_input, config_path])


def test_debug_output_is_separate_review_sidecar(tmp_path: Path) -> None:
    input_path = write_statement(tmp_path)
    config_path = tmp_path / "user.toml"
    config_path.write_text(_USER_TOML_WITH_FORM67_OVERRIDE, encoding="utf-8")
    output_path = tmp_path / "form67.csv"
    debug_path = tmp_path / "form67-debug.csv"

    _run_form67(
        input_path=input_path,
        config_path=config_path,
        output_path=output_path,
        debug_output_path=debug_path,
        financial_year=_FY_2025_26,
    )

    assert len(read_csv_rows(output_path)[0]) == 15
    assert len(read_csv_rows(debug_path)[0]) == 17


def test_debug_write_failure_removes_official_and_debug_outputs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    input_path = write_statement(tmp_path)
    config_path = tmp_path / "user.toml"
    config_path.write_text(_USER_TOML_WITH_FORM67_OVERRIDE, encoding="utf-8")
    output_path = tmp_path / "form67.csv"
    debug_path = tmp_path / "form67-debug.csv"

    class Boom(StubWriter):
        format_id: ClassVar[str] = "review-csv"

        def write(self, _value: object, _path: Path) -> None:
            raise OSError("debug disk failure")

    slot = (Subcommand.FORM67, "review-csv")
    key = output.output_binding(*slot).key
    monkeypatch.setitem(output._REGISTRY, key, Boom)
    monkeypatch.setitem(output._CATALOG, slot, OutputBinding(key, Boom))
    with pytest.raises(OSError, match="debug disk failure"):
        _run_form67(
            input_path=input_path,
            config_path=config_path,
            output_path=output_path,
            debug_output_path=debug_path,
            financial_year=_FY_2025_26,
        )
    assert not output_path.exists()
    assert not debug_path.exists()


# ---- Form67PerEntryRow invariants ----------------------------------------

_READY = Form67RowReadiness(Form67Readiness.READY)
_BLANK = Form67RowReadiness(Form67Readiness.BLANK)


def test_per_entry_row_rejects_zero_serial_number() -> None:
    """1-indexed serial_number invariant matches the upstream Form67Entry contract."""
    with pytest.raises(IbkrFaError):
        Form67PerEntryRow(
            serial_number=0,
            cbdt_country_code="2",
            country_name_portal="UNITED STATES OF AMERICA",
            iso_alpha2="US",
            source_of_income=Form67SourceOfIncome.DIVIDEND,
            income_inr=Decimal("10000"),
            foreign_tax_inr=Decimal("1500"),
            foreign_tax_rate_percent=Decimal("15"),
            event_count=1,
            tax_payable_normal_inr=None,
            tax_payable_special_inr=None,
            section_90_dtaa_article=None,
            section_90_dtaa_rate_percent=None,
            section_90_credit_inr=None,
            section_91_credit_inr=None,
            total_credit_inr=None,
            readiness=_BLANK,
        )


def test_per_entry_row_rejects_zero_income() -> None:
    """Rule 128(5)(i): Form 67 cannot claim relief on zero foreign income."""
    with pytest.raises(IbkrFaError):
        Form67PerEntryRow(
            serial_number=1,
            cbdt_country_code="2",
            country_name_portal="UNITED STATES OF AMERICA",
            iso_alpha2="US",
            source_of_income=Form67SourceOfIncome.DIVIDEND,
            income_inr=Decimal("0"),
            foreign_tax_inr=Decimal("1500"),
            foreign_tax_rate_percent=Decimal("15"),
            event_count=1,
            tax_payable_normal_inr=None,
            tax_payable_special_inr=None,
            section_90_dtaa_article=None,
            section_90_dtaa_rate_percent=None,
            section_90_credit_inr=None,
            section_91_credit_inr=None,
            total_credit_inr=None,
            readiness=_BLANK,
        )


def _refused(reason: Form67RefusalReason) -> Form67RowReadiness:
    return Form67RowReadiness(Form67Readiness.REFUSED, reason)


def _report_row(**overrides: Any) -> Form67PerEntryRow:
    """A READY section 90 US row, with ``overrides`` applied."""
    fields: dict[str, Any] = {
        "serial_number": 1,
        "cbdt_country_code": "2",
        "country_name_portal": "UNITED STATES OF AMERICA",
        "iso_alpha2": "US",
        "source_of_income": Form67SourceOfIncome.DIVIDEND,
        "income_inr": Decimal("1000"),
        "foreign_tax_inr": Decimal("250"),
        "foreign_tax_rate_percent": Decimal("25"),
        "event_count": 1,
        "tax_payable_normal_inr": Decimal("300"),
        "tax_payable_special_inr": None,
        "section_90_dtaa_article": "Article 10(2)(b)",
        "section_90_dtaa_rate_percent": Decimal("25"),
        "section_90_credit_inr": Decimal("250"),
        "section_91_credit_inr": None,
        "total_credit_inr": Decimal("250"),
        "tax_paid_rate_percent": Decimal("25"),
        "highest_payment_rate_percent": Decimal("25"),
        "eligible_foreign_tax_inr": Decimal("250"),
        "treaty_rate_status": Form67TreatyRateStatus.WITHIN,
        "readiness": _READY,
    }
    return Form67PerEntryRow(**(fields | overrides))


# Fields that turn the READY section 90 row into each other shape.
_NO_TREATY: dict[str, Any] = {
    "section_90_dtaa_article": None,
    "section_90_dtaa_rate_percent": None,
    "treaty_rate_status": None,
    "highest_payment_rate_percent": None,
    "eligible_foreign_tax_inr": None,
}
_SECTION_91: dict[str, Any] = {
    **_NO_TREATY,
    "section_90_credit_inr": None,
    "section_91_credit_inr": Decimal("100"),
    "total_credit_inr": Decimal("100"),
}
_NO_CREDIT: dict[str, Any] = {"section_90_credit_inr": None, "total_credit_inr": None}
_BLANK_ROW: dict[str, Any] = {**_NO_CREDIT, "tax_payable_normal_inr": None, "readiness": _BLANK}
_ZERO_CREDIT = _refused(Form67RefusalReason.ZERO_CREDIT)

_VALID_REPORT_ROWS: dict[str, dict[str, Any]] = {
    "ready-section-90": {},
    "ready-section-91": _SECTION_91,
    "blank": _BLANK_ROW,
    "zero-credit-section-90": {
        "tax_payable_normal_inr": Decimal("0"),
        "section_90_credit_inr": Decimal("0"),
        "total_credit_inr": Decimal("0"),
        "readiness": _ZERO_CREDIT,
    },
    "zero-credit-section-91": {
        **_SECTION_91,
        "section_91_credit_inr": Decimal("0"),
        "total_credit_inr": Decimal("0"),
        "readiness": _ZERO_CREDIT,
    },
    # #898: readiness reads the filed whole-rupee credit.
    "credit-rounding-to-zero-section-90": {
        "section_90_credit_inr": Decimal("0.49"),
        "total_credit_inr": Decimal("0.49"),
        "readiness": _ZERO_CREDIT,
    },
    "credit-rounding-to-zero-section-91": {
        **_SECTION_91,
        "section_91_credit_inr": Decimal("0.49"),
        "total_credit_inr": Decimal("0.49"),
        "readiness": _ZERO_CREDIT,
    },
    "credit-rounding-to-one-section-90": {
        "section_90_credit_inr": Decimal("0.50"),
        "total_credit_inr": Decimal("0.50"),
    },
    "section-91-inputs-missing": {
        **_NO_TREATY,
        **_NO_CREDIT,
        "readiness": _refused(Form67RefusalReason.SECTION_91_INPUTS_MISSING),
    },
    "no-vendored-dtaa": {
        **_NO_TREATY,
        **_NO_CREDIT,
        "readiness": _refused(Form67RefusalReason.NO_VENDORED_DTAA),
    },
    "source-rate-unproven": {
        **_NO_CREDIT,
        "treaty_rate_status": Form67TreatyRateStatus.UNPROVEN,
        "tax_paid_rate_percent": None,
        "highest_payment_rate_percent": None,
        "eligible_foreign_tax_inr": None,
        "readiness": _refused(Form67RefusalReason.SOURCE_RATE_UNPROVEN),
    },
    "payment-allocation-unproven": {
        **_NO_CREDIT,
        "treaty_rate_status": Form67TreatyRateStatus.UNPROVEN,
        "payment_allocation_unproven": True,
        "highest_payment_rate_percent": Decimal("30"),
        "eligible_foreign_tax_inr": None,
        "readiness": _refused(Form67RefusalReason.PAYMENT_ALLOCATION_UNPROVEN),
    },
}

_READINESS_ERROR = "Form67PerEntryRow.readiness must be a Form67RowReadiness"
_MISMATCH_ERROR = "Form67PerEntryRow.readiness doesn't match"
_SECTION_90_ERROR = "Form67PerEntryRow: a section 90 credit needs"
# Each broken row, and the start of the message that must refuse it.
_BROKEN_REPORT_ROWS: dict[str, tuple[dict[str, Any], str]] = {
    "not-a-row-readiness": ({"readiness": Form67Readiness.READY}, _READINESS_ERROR),
    "unknown-state": (
        {"readiness": Form67RowReadiness("unexpected")},  # ty: ignore[invalid-argument-type]
        _READINESS_ERROR,
    ),
    "unknown-reason": (
        {
            **_NO_CREDIT,
            "readiness": Form67RowReadiness(
                Form67Readiness.REFUSED,
                "unexpected",  # ty: ignore[invalid-argument-type]
            ),
        },
        _READINESS_ERROR,
    ),
    "ready-with-blank-credit": (_NO_CREDIT, _MISMATCH_ERROR),
    "ready-with-blank-col-7": ({"tax_payable_normal_inr": None}, _MISMATCH_ERROR),
    "ready-with-zero-credit": (
        {**_VALID_REPORT_ROWS["zero-credit-section-90"], "readiness": _READY},
        _MISMATCH_ERROR,
    ),
    "blank-with-col-7": ({**_NO_CREDIT, "readiness": _BLANK}, _MISMATCH_ERROR),
    "blank-with-credit": ({"readiness": _BLANK}, _MISMATCH_ERROR),
    "zero-credit-with-positive-credit": ({"readiness": _ZERO_CREDIT}, _MISMATCH_ERROR),
    "zero-credit-with-negative-section-90-credit": (
        {
            "section_90_credit_inr": Decimal("-0.49"),
            "total_credit_inr": Decimal("-0.49"),
            "readiness": _ZERO_CREDIT,
        },
        _MISMATCH_ERROR,
    ),
    "zero-credit-with-negative-section-91-credit": (
        {
            **_SECTION_91,
            "section_91_credit_inr": Decimal("-0.49"),
            "total_credit_inr": Decimal("-0.49"),
            "readiness": _ZERO_CREDIT,
        },
        _MISMATCH_ERROR,
    ),
    "ready-with-credit-rounding-to-zero": (
        {"section_90_credit_inr": Decimal("0.49"), "total_credit_inr": Decimal("0.49")},
        _MISMATCH_ERROR,
    ),
    "zero-credit-with-credit-rounding-to-one": (
        {
            "section_90_credit_inr": Decimal("0.50"),
            "total_credit_inr": Decimal("0.50"),
            "readiness": _ZERO_CREDIT,
        },
        _MISMATCH_ERROR,
    ),
    "zero-credit-with-blank-credit": ({**_NO_CREDIT, "readiness": _ZERO_CREDIT}, _MISMATCH_ERROR),
    "refused-on-a-complete-row": (
        {"readiness": _refused(Form67RefusalReason.NO_VENDORED_DTAA)},
        _MISMATCH_ERROR,
    ),
    "refused-without-col-7": (
        {**_BLANK_ROW, "readiness": _refused(Form67RefusalReason.NO_VENDORED_DTAA)},
        _MISMATCH_ERROR,
    ),
    "both-credits": (
        {"section_91_credit_inr": Decimal("0")},
        "Form67PerEntryRow: at most one of section_90_credit_inr",
    ),
    "section-90-without-article": ({"section_90_dtaa_article": None}, _SECTION_90_ERROR),
    "section-90-without-rate": (
        {
            "section_90_dtaa_rate_percent": None,
            "treaty_rate_status": None,
            "highest_payment_rate_percent": None,
            "eligible_foreign_tax_inr": None,
        },
        _SECTION_90_ERROR,
    ),
    "section-91-with-article": (
        {**_SECTION_91, "section_90_dtaa_article": "Article 10(2)(b)"},
        "Form67PerEntryRow: a section 91 credit needs",
    ),
    "section-91-with-rate": (
        {
            **_SECTION_91,
            "section_90_dtaa_rate_percent": Decimal("25"),
            "treaty_rate_status": Form67TreatyRateStatus.WITHIN,
            "highest_payment_rate_percent": Decimal("25"),
            "eligible_foreign_tax_inr": Decimal("250"),
        },
        "Form67PerEntryRow: a section 91 credit needs",
    ),
    "string-credit": (
        {"section_90_credit_inr": "250", "total_credit_inr": "250"},
        "Form67PerEntryRow: col 7, the section 90 and 91 credits and col 13",
    ),
    "nan-credit": (
        {"section_90_credit_inr": Decimal("NaN")},
        "Form67PerEntryRow: col 7, the section 90 and 91 credits and col 13",
    ),
    "negative-col-7": (
        {"tax_payable_normal_inr": Decimal("-1")},
        "Form67PerEntryRow.tax_payable_normal_inr must be >= 0",
    ),
    "empty-article": ({"section_90_dtaa_article": ""}, _SECTION_90_ERROR),
    "whitespace-article": ({"section_90_dtaa_article": "   "}, _SECTION_90_ERROR),
    "non-string-article": ({"section_90_dtaa_article": 10}, _SECTION_90_ERROR),
    "col-13-differs-from-credit": (
        {"total_credit_inr": Decimal("200")},
        "Form67PerEntryRow.total_credit_inr must equal",
    ),
}


@pytest.mark.parametrize("case", sorted(_VALID_REPORT_ROWS))
def test_report_row_accepts_readiness_that_matches_its_columns(case: str) -> None:
    overrides = _VALID_REPORT_ROWS[case]
    assert _report_row(**overrides).readiness == overrides.get("readiness", _READY)


@pytest.mark.parametrize("case", sorted(_BROKEN_REPORT_ROWS))
def test_report_row_refuses_readiness_that_contradicts_its_columns(case: str) -> None:
    overrides, message = _BROKEN_REPORT_ROWS[case]
    with pytest.raises(InvariantError) as excinfo:
        _report_row(**overrides)
    assert excinfo.value.what.startswith(message)


def test_report_row_readiness_refusal_wins_over_the_citation_shape() -> None:
    """#1079: the citation shape is checked last, so this BLANK row with a
    credit gets the readiness refusal, and no part of the citation is shown."""
    with pytest.raises(InvariantError) as excinfo:
        _report_row(section_90_dtaa_article="Article 7(Synthetic Holder Two)x", readiness=_BLANK)
    assert str(excinfo.value) == (
        "InvariantError: Form67PerEntryRow.readiness doesn't match col 7 and the filed "
        "claimed credit: READY needs both and a credit of at least ₹1, BLANK neither, "
        "ZERO_CREDIT both and a credit that rounds to ₹0, and any other refusal col 7 alone "
        "(context keys: readiness, tax_payable_normal_inr, claimed_credit, filed_claimed_credit)"
    )


@pytest.mark.parametrize(
    ("readiness", "forced_reason", "shape"),
    [
        (_READY, Form67RefusalReason.ZERO_CREDIT, {}),
        (_BLANK, Form67RefusalReason.ZERO_CREDIT, _BLANK_ROW),
        (_refused(Form67RefusalReason.NO_VENDORED_DTAA), None, _NO_CREDIT),
    ],
    ids=["ready-with-reason", "blank-with-reason", "refused-without-reason"],
)
def test_report_row_refuses_a_tampered_readiness(
    readiness: Form67RowReadiness,
    forced_reason: Form67RefusalReason | None,
    shape: dict[str, Any],
) -> None:
    """Form67RowReadiness can't be built this way, so the row is the only guard."""
    tampered = replace(readiness)
    object.__setattr__(tampered, "reason", forced_reason)
    with pytest.raises(InvariantError) as excinfo:
        _report_row(**(shape | {"readiness": tampered}))
    assert excinfo.value.what.startswith(_READINESS_ERROR)


# ============================================================================
# Step 4c/N of #37: end-to-end §90 credit + tax-payable config
# ============================================================================


_USER_TOML_WITH_FORM67_OVERRIDE = """\
[account]
number = "U1234567"
holder_name = "Test Holder"
opening_date = 2020-01-01
status = "BeneficialOwner"

[fx]
max_carry_forward_days = 4

[form67]

[[form67.tax_payable_overrides]]
country = "US"
source = "DIVIDEND"
tax_payable_normal_inr = "999.99"
"""


def test_run_form67_no_form67_config_leaves_indian_tax_and_credit_blank(
    tmp_path: Path,
) -> None:
    """DTAA metadata remains available, but tax and credit need exact input."""
    input_path = write_statement(tmp_path)
    config_path = write_config(tmp_path)  # no [form67]
    output_path = tmp_path / "form67.csv"

    report = run_form67(
        input_path=input_path,
        config_path=config_path,
        output_path=output_path,
        financial_year=_FY_2025_26,
    )

    row = report.per_entry_rows[0]
    assert row.iso_alpha2 == "US"
    # Cols 9 + 10 populate from DTAA reference (no config dependency).
    assert row.section_90_dtaa_article == "Article 10(2)(b)"
    assert row.section_90_dtaa_rate_percent == Decimal("25")
    assert row.tax_payable_normal_inr is None
    assert row.section_90_credit_inr is None
    assert row.total_credit_inr is None
    assert report.has_unresolved_indian_tax_payable


def test_run_form67_with_form67_override_takes_precedence(
    tmp_path: Path,
) -> None:
    """An explicit (US, DIVIDEND) override supplies the exact Indian
    tax payable. End-to-end through the pipeline."""
    input_path = write_statement(tmp_path)
    config_path = tmp_path / "user.toml"
    config_path.write_text(_USER_TOML_WITH_FORM67_OVERRIDE, encoding="utf-8")
    output_path = tmp_path / "form67.csv"

    report = run_form67(
        input_path=input_path,
        config_path=config_path,
        output_path=output_path,
        financial_year=_FY_2025_26,
    )

    row = report.per_entry_rows[0]
    assert row.iso_alpha2 == "US"
    assert row.tax_payable_normal_inr == Decimal("999.99")
    # §90 credit = min(999.99, min(foreign_tax, dtaa_cap)). foreign_tax
    # is small (one WHT row at -0.36 USD), well below both 999.99 and
    # the 25% DTAA cap, so credit equals foreign_tax_inr.
    assert row.section_90_credit_inr is not None
    assert row.section_90_credit_inr == row.foreign_tax_inr
    assert row.total_credit_inr == row.section_90_credit_inr
    entry = next(
        e
        for e in _compute_ctx(input_path, config_path).form67_result.entries
        if e.iso_alpha2 == "US"
    )
    assert entry.eligible_foreign_tax_inr is not None
    assert row.eligible_foreign_tax_inr == entry.eligible_foreign_tax_inr
    assert not report.has_unresolved_indian_tax_payable


# ============================================================================
# Step 4d/N of #37: end-to-end §91 config threading
# ============================================================================


_USER_TOML_WITH_FORM67_SECTION_91 = """\
[account]
number = "U1234567"
holder_name = "Test Holder"
opening_date = 2020-01-01
status = "BeneficialOwner"

[fx]
max_carry_forward_days = 4

[form67]

[[form67.tax_payable_overrides]]
country = "US"
source = "DIVIDEND"
tax_payable_normal_inr = "150"

[form67.section_91]
total_indian_tax_before_91_relief_inr = "30000"
total_income_inr = "100000"
"""


def test_run_form67_with_section_91_config_loads_cleanly(tmp_path: Path) -> None:
    """End-to-end pipeline test: a user.toml with [form67.section_91]
    fully populated loads + runs to completion without error. Verifies
    the section_91 config threading through pipeline_fsi_compute →
    pipeline_form67 → compute_form67_entries_from_wht."""
    input_path = write_statement(tmp_path)
    config_path = tmp_path / "user.toml"
    config_path.write_text(_USER_TOML_WITH_FORM67_SECTION_91, encoding="utf-8")
    output_path = tmp_path / "form67.csv"

    report = run_form67(
        input_path=input_path,
        config_path=config_path,
        output_path=output_path,
        financial_year=_FY_2025_26,
    )

    # Synthetic fixture has US data only (a DTAA-vendored country),
    # so §91 config is supplied but doesn't apply — US routes through
    # the §90 pathway exclusively. Cols 9, 10, 11, 13 populate; col 12
    # stays None even with section_91 config.
    row = report.per_entry_rows[0]
    assert row.iso_alpha2 == "US"
    assert row.section_90_dtaa_article == "Article 10(2)(b)"
    assert row.section_90_dtaa_rate_percent == Decimal("25")
    assert row.section_90_credit_inr is not None
    # §91 NOT activated for DTAA country (Form67Entry mutex rejects
    # §91 paired with DTAA fields, and the compute-layer gate routes
    # US to §90 exclusively).
    assert row.section_91_credit_inr is None
    # total = §90 credit (mutex)
    assert row.total_credit_inr == row.section_90_credit_inr


# ============================================================================
# Issue #225: end-to-end pipeline with XQBA + an Indian ADR under UNDERLYING_ISSUER
# ============================================================================
#
# Reproduces the bug scenario from issue #225 at the pipeline level:
# XQBA (US foreign issuer) + a mapped Indian ADR (US-prefix, Indian underlying)
# both with WHT. Pre-fix: the pipeline either raised at the broad
# UNDERLYING_ISSUER + WHT guard, or silently over-reported US foreign tax
# by absorbing the ADR's WHT alongside XQBA's. Post-fix: the pipeline must
# succeed AND emit a Form 67 entry for US that contains XQBA's WHT only.


def test_issue_225_e2e_underlying_issuer_with_xqba_and_infy_succeeds(
    tmp_path: Path,
) -> None:
    """End-to-end pipeline: XQBA + INFY ADR with WHT under
    UNDERLYING_ISSUER routing.

    Pre-#225: pipeline raised IbkrFaError at the broad guard
    (UNDERLYING_ISSUER + any WHT was rejected) OR (after broad guard
    removal) silently absorbed INFY's WHT into US bucket, over-reporting
    the FTC claim.

    Post-#225: pipeline succeeds. The Form 67 US entry's foreign_tax
    reflects XQBA's $0.36 WHT only — INFY's $0.75 WHT is excluded
    symmetrically with Schedule OS's dividend rerouting.
    """
    input_path = write_statement(tmp_path, second=ADR_ROWS)
    config_path = write_config(tmp_path, template=UNDERLYING_ISSUER_CONFIG_TEMPLATE)
    output_path = tmp_path / "form67.csv"

    report = run_form67(
        input_path=input_path,
        config_path=config_path,
        output_path=output_path,
        financial_year=_FY_2025_26,
    )

    # Pipeline succeeded — broad UNDERLYING_ISSUER+WHT guard is gone.
    assert output_path.exists()
    # Exactly one entry: US/DIVIDEND. INFY did NOT produce an IN entry
    # (INFY dividend reroutes to domestic and INFY WHT is skipped).
    assert len(report.per_entry_rows) == 1
    row = report.per_entry_rows[0]
    assert row.iso_alpha2 == "US"
    assert row.source_of_income is Form67SourceOfIncome.DIVIDEND
    # foreign_tax_inr: XQBA's $0.36 only — NOT (0.36 + 0.75) = $1.11. Rule 128
    # takes the SBI TT buying rate of the last day of the month before the
    # 2025-09-12 deduction: 2025-08-31 is a Sunday, so it carries back to
    # 2025-08-30's 87.70. 0.36 * 87.70 = 31.572 (with the ADR's 0.75: 97.347).
    assert row.foreign_tax_inr == Decimal("31.572")
    # event_count audits the aggregation cardinality: 1 (XQBA), not 2.
    assert row.event_count == 1


def test_issue_225_e2e_underlying_issuer_infy_only_produces_no_form67_row(
    tmp_path: Path,
) -> None:
    """INFY-only (no XQBA): INFY dividend reroutes to domestic, INFY WHT
    is skipped → no Form 67 entry produced, AND the pipeline does NOT
    raise the orphan-WHT guard (because there are no remaining WHTs to
    orphan). User should recover via domestic §90 pathway."""
    lines = [
        "{header}",
        FII_HEADER,
        FII_DATA_XQBA,
        FII_DATA_ADR,
        INCOME_TRADES_HEADER,
        TRADE_OPEN_XQBA,
        TRADE_CLOSE_XQBA,
        OP_HEADER,
        DIVIDENDS_HEADER,
        DIVIDEND_ADR,
        WHT_HEADER,
        WHT_ADR,
        "",
    ]
    body = "\n".join(lines).format(header=AS_HEADER.format(account="U1234567"))
    input_path = tmp_path / "activity-statement.csv"
    input_path.write_text(with_totals(body), encoding="utf-8")
    config_path = write_config(tmp_path, template=UNDERLYING_ISSUER_CONFIG_TEMPLATE)
    output_path = tmp_path / "form67.csv"

    report = run_form67(
        input_path=input_path,
        config_path=config_path,
        output_path=output_path,
        financial_year=_FY_2025_26,
    )

    assert output_path.exists()
    # No Form 67 entries — INFY routed entirely out of the foreign
    # bucket (dividend AND WHT both skipped from the foreign
    # aggregator). No orphan-WHT error raised.
    assert report.per_entry_rows == ()


# ---- #36/#37: FSI / TR / Form 67 relief consistency ------------------------


def _compute_ctx(input_path: Path, config_path: Path):
    from ibkr_fa.pipeline_fsi_compute import (  # noqa: PLC0415 — scoped private import
        _compute_schedule_fsi_for_pipeline,
    )

    return _compute_schedule_fsi_for_pipeline(
        input_path=input_path,
        config_path=config_path,
        financial_year=_FY_2025_26,
        allow_masked_account=False,
        suppress_cross_ay_warning=False,
        accept_anonymised_statement=False,
        report_name="test",
    )


def test_fsi_tr_form67_all_leave_relief_blank_without_exact_indian_tax(
    tmp_path: Path,
) -> None:
    from ibkr_fa.compute.schedule_tr import compute_schedule_tr_from_fsi  # noqa: PLC0415

    input_path = write_statement(tmp_path)
    config_path = write_config(tmp_path)

    ctx = _compute_ctx(input_path, config_path)
    fsi_us = next(
        r
        for r in ctx.fsi_result.rows
        if r.iso_alpha2 == "US" and r.head_of_income.value == "Other Sources"
    )
    f67_us = next(e for e in ctx.form67_result.entries if e.iso_alpha2 == "US")

    assert fsi_us.tax_relief_inr is None
    assert fsi_us.tax_payable_inr is None
    assert fsi_us.dtaa_article is None
    assert f67_us.total_credit_inr is None
    assert f67_us.tax_payable_normal_inr is None
    assert f67_us.section_90_dtaa_article == "Article 10(2)(b)"
    tr_us = next(
        r for r in compute_schedule_tr_from_fsi(ctx.fsi_result).rows if r.iso_alpha2 == "US"
    )
    assert tr_us.tax_relief_inr_total is None
    assert tr_us.section_under_which_claimed is None


def test_fsi_tr_relief_stays_blank_without_form67_config(tmp_path: Path) -> None:
    from ibkr_fa.compute.schedule_tr import compute_schedule_tr_from_fsi  # noqa: PLC0415

    input_path = write_statement(tmp_path)
    config_path = write_config(tmp_path)  # no [form67] table

    ctx = _compute_ctx(input_path, config_path)
    for fsi_row in ctx.fsi_result.rows:
        if fsi_row.foreign_tax_inr > 0:
            assert fsi_row.tax_relief_inr is None
            assert fsi_row.tax_payable_inr is None
            assert fsi_row.dtaa_article is None
        else:
            # Zero foreign tax → relief is provably ₹0 regardless of rate.
            assert fsi_row.tax_relief_inr == Decimal("0")
    for tr_row in compute_schedule_tr_from_fsi(ctx.fsi_result).rows:
        assert tr_row.tax_relief_inr_total is None
        assert tr_row.section_under_which_claimed is None


def test_foreign_tin_flows_from_config_to_fsi_and_tr(tmp_path: Path) -> None:
    """An [account.foreign_tins] entry is stamped onto the FSI rows and
    copied to the TR rows for the matching country."""
    from ibkr_fa.compute.schedule_tr import compute_schedule_tr_from_fsi  # noqa: PLC0415

    input_path = write_statement(tmp_path)
    config_path = tmp_path / "user.toml"
    config_path.write_text(
        SYNTHETIC_CONFIG.rstrip() + '\n\n[account.foreign_tins]\nUS = "123456789"\n',
        encoding="utf-8",
    )

    ctx = _compute_ctx(input_path, config_path)
    us_fsi = [r for r in ctx.fsi_result.rows if r.iso_alpha2 == "US"]
    assert us_fsi  # the synthetic XQBA fixture is US-source
    assert all(r.taxpayer_id_number == "123456789" for r in us_fsi)

    tr_us = [r for r in compute_schedule_tr_from_fsi(ctx.fsi_result).rows if r.iso_alpha2 == "US"]
    assert tr_us and all(r.taxpayer_id_number == "123456789" for r in tr_us)


def test_foreign_tin_absent_leaves_fsi_tr_tin_none(tmp_path: Path) -> None:
    from ibkr_fa.compute.schedule_tr import compute_schedule_tr_from_fsi  # noqa: PLC0415

    input_path = write_statement(tmp_path)
    config_path = write_config(tmp_path)  # no foreign_tins

    ctx = _compute_ctx(input_path, config_path)
    # The synthetic XQBA fixture's STCG and dividend are US-source.
    fsi_rows = ctx.fsi_result.rows
    assert [(r.iso_alpha2, r.head_of_income.value) for r in fsi_rows] == [
        ("US", "Capital Gains"),
        ("US", "Other Sources"),
    ]
    assert all(r.taxpayer_id_number is None for r in fsi_rows)
    tr_rows = compute_schedule_tr_from_fsi(ctx.fsi_result).rows
    assert [r.iso_alpha2 for r in tr_rows] == ["US"]
    assert all(r.taxpayer_id_number is None for r in tr_rows)


# ---- Form 67 readiness (#601) --------------------------------------------

_FII_DATA_XQCT = (
    "Financial Instrument Information,Data,Stocks,XQCT,SAMPLE ISSUER CT SA-REG,"
    "1234567,CH99970E1074,CH99970E1074,EBS,1,COMMON,,,,,,"
)
_DIVIDEND_XQCT = (
    "Dividends,Data,USD,12-09-2025,"
    "XQCT(CH99970E1074) Cash Dividend USD 3.00 per Share (Ordinary Dividend),"
    "30.00,,,,,,,,,,,"
)
_WHT_XQCT = (
    "Withholding Tax,Data,USD,12-09-2025,"
    "XQCT(CH99970E1074) Cash Dividend USD 3.00 per Share - CH Tax,"
    "-10.50,,,,,,,,,,,"
)


def _override_toml(tmp_path: Path, **amounts: str) -> Path:
    """A config with an exact dividend col 7 override per ISO code."""
    path = tmp_path / "user.toml"
    path.write_text(
        SYNTHETIC_CONFIG
        + "\n[form67]\n"
        + "".join(
            "[[form67.tax_payable_overrides]]\n"
            f'country = "{country}"\nsource = "DIVIDEND"\ntax_payable_normal_inr = "{amount}"\n'
            for country, amount in amounts.items()
        ),
        encoding="utf-8",
    )
    return path


def _write_ch_activity_statement(tmp_path: Path) -> Path:
    lines = [
        AS_HEADER.format(account="U1234567"),
        FII_HEADER,
        FII_DATA_XQBA,
        _FII_DATA_XQCT,
        INCOME_TRADES_HEADER,
        TRADE_OPEN_XQBA,
        TRADE_CLOSE_XQBA,
        OP_HEADER,
        DIVIDENDS_HEADER,
        _DIVIDEND_XQCT,
        WHT_HEADER,
        _WHT_XQCT,
        "",
    ]
    path = tmp_path / "activity-statement.csv"
    path.write_text(with_totals("\n".join(lines)), encoding="utf-8")
    return path


def _refuse_every_write(monkeypatch: pytest.MonkeyPatch) -> None:
    def refuse(*_args: object) -> None:
        raise AssertionError("a normal output write was attempted")

    monkeypatch.setattr(OutputBinding, "writer", refuse)


def test_blank_row_withholds_official_csv_and_writes_debug(tmp_path: Path) -> None:
    output_path = tmp_path / "form-67.csv"
    _run_form67(
        input_path=write_statement(tmp_path),
        config_path=write_config(tmp_path),
        output_path=output_path,
        financial_year=_FY_2025_26,
    )
    assert not output_path.exists()
    assert read_csv_rows(tmp_path / "form-67-debug.csv")[1][1] == "2-UNITED STATES OF AMERICA"


def test_cli_blank_row_is_manual_fill(tmp_path: Path) -> None:
    code = main(
        [
            "form-67",
            "--input",
            str(write_statement(tmp_path)),
            "--config",
            str(write_config(tmp_path)),
            "--output",
            str(tmp_path / "form-67.csv"),
            "--financial-year",
            "2025-26",
        ]
    )
    assert code == 3


def test_zero_credit_override_refuses_before_any_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _refuse_every_write(monkeypatch)
    with pytest.raises(IbkrFaError, match=r"US/DIVIDEND: the computed credit rounds to ₹0"):
        _run_form67(
            input_path=write_statement(tmp_path),
            config_path=_override_toml(tmp_path, US="0"),
            output_path=tmp_path / "form-67.csv",
            financial_year=_FY_2025_26,
        )
    assert sorted(path.name for path in tmp_path.iterdir()) == [
        "activity-statement.csv",
        "user.toml",
    ]


def test_unvendored_country_override_refuses_naming_the_gap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _refuse_every_write(monkeypatch)
    with pytest.raises(
        IbkrFaError, match=r"CH/DIVIDEND: CH has no row in the vendored DTAA dividend table"
    ):
        _run_form67(
            input_path=_write_ch_activity_statement(tmp_path),
            config_path=_override_toml(tmp_path, CH="500"),
            output_path=tmp_path / "form-67.csv",
            financial_year=_FY_2025_26,
        )


@pytest.mark.parametrize("format_id", ["review-csv", "xlsx"])
def test_explicit_diagnostic_formats_still_write_refused_rows(
    tmp_path: Path, format_id: str
) -> None:
    output_path = tmp_path / f"form-67.{'xlsx' if format_id == 'xlsx' else 'csv'}"
    report = _run_form67(
        input_path=_write_ch_activity_statement(tmp_path),
        config_path=_override_toml(tmp_path, CH="500"),
        output_path=output_path,
        financial_year=_FY_2025_26,
        format_id=format_id,
    )
    assert report.rows_written == 1
    assert output_path.exists()


# ---- Treaty-rate status (#767) ---------------------------------------------

_FII_DATA_XQCS = (
    "Financial Instrument Information,Data,Stocks,XQCS,SAMPLE ISSUER CS SE,"
    "12347,DE99970G1028,DE99970G1028,IBIS,1,COMMON,,,,,,"
)
_DIVIDEND_XQCS = (
    "Dividends,Data,USD,12-09-2025,"
    "XQCS(DE99970G1028) Cash Dividend USD 2.00 per Share (Ordinary Dividend),"
    "10.00,,,,,,,,,,,"
)
# 26.4% German withholding, above the 10% India-Germany treaty rate.
_WHT_XQCS = (
    "Withholding Tax,Data,USD,12-09-2025,"
    "XQCS(DE99970G1028) Cash Dividend USD 2.00 per Share - DE Tax,"
    "-2.64,,,,,,,,,,,"
)


def test_run_form67_rows_carry_each_entry_treaty_rate_status(tmp_path: Path) -> None:
    """US is within its treaty rate, DE above it, and GB has no vendored rate.

    Only the ABOVE row reaches the over-treaty advisory, with today's text.
    """
    input_path = tmp_path / "activity-statement.csv"
    input_path.write_text(
        with_totals(
            "\n".join(
                [
                    AS_HEADER.format(account="U1234567"),
                    FII_HEADER,
                    FII_DATA_XQBA,
                    FII_DATA_XQCR,
                    _FII_DATA_XQCS,
                    INCOME_TRADES_HEADER,
                    TRADE_OPEN_XQBA,
                    TRADE_CLOSE_XQBA,
                    OP_HEADER,
                    DIVIDENDS_HEADER,
                    DIVIDEND_XQBA,
                    DIVIDEND_XQCR,
                    _DIVIDEND_XQCS,
                    WHT_HEADER,
                    WHT_XQBA,
                    WHT_XQCR,
                    _WHT_XQCS,
                    "",
                ]
            )
        ),
        encoding="utf-8",
    )
    report = run_form67(
        input_path=input_path,
        config_path=write_config(tmp_path),
        output_path=tmp_path / "form-67.csv",
        financial_year=_FY_2025_26,
    )
    assert {row.iso_alpha2: row.treaty_rate_status for row in report.per_entry_rows} == {
        "US": Form67TreatyRateStatus.WITHIN,
        "DE": Form67TreatyRateStatus.ABOVE,
        "GB": None,
    }
    lines = cli._form67_over_treaty_excess_lines(report.per_entry_rows)
    assert lines[1:-1] == [
        "     - GERMANY: highest per-payment source-currency withholding 26.40% exceeds "
        "the DTAA cap 10.00%; automatic credit applies the conservative legacy INR cap "
        "to each payment above it, pending per-event eligible-tax conversion."
    ]


def test_per_entry_row_carries_highest_payment_rate(tmp_path: Path) -> None:
    """#890: USD 0.37 withheld on USD 1.46 is about 25.34%, within half a cent of the
    25% treaty rate. The report row carries the snapped rate, and its invariant
    checks the status against it."""
    assert DIVIDEND_XQBA.count(",2.40,") == 1
    assert WHT_XQBA.count(",-0.36,") == 1
    cent_rounded_leg = (
        DIVIDEND_XQBA.replace(",2.40,", ",1.46,"),
        WHT_XQBA.replace(",-0.36,", ",-0.37,"),
    )
    report = run_form67(
        input_path=_dividend_statement(tmp_path, cent_rounded_leg),
        config_path=write_config(tmp_path),
        output_path=tmp_path / "form-67.csv",
        financial_year=_FY_2025_26,
    )
    (row,) = report.per_entry_rows
    assert row.treaty_rate_status is Form67TreatyRateStatus.WITHIN
    assert row.highest_payment_rate_percent == Decimal("25")
    raw_rate = (Decimal("0.37") / Decimal("1.46")) * Decimal("100")
    with pytest.raises(InvariantError, match=r"Form67PerEntryRow\.treaty_rate_status WITHIN"):
        replace(row, highest_payment_rate_percent=raw_rate)


_FIXTURES = Path(__file__).parent / "fixtures"
# Two DTAA entries with positive INR tax and income but no source-currency
# rate: a same-year refund that nets the USD tax to zero, and USD tax above
# USD income at an INR ratio under 100%. With a col 7 override, c38027b filed
# an INR-capped credit for each.
_UNPROVEN_SOURCE_RATE_INPUTS = [
    pytest.param(
        _FIXTURES / "e2e_activity_statement_form67_same_year_wht_refund_fy2025_26.csv",
        id="same-year-refund",
    ),
    pytest.param(
        _FIXTURES / "e2e_activity_statement_form67_tax_above_income_fy2025_26.csv",
        id="source-tax-above-income",
    ),
]


def _fixture_config(tmp_path: Path, *, col_7: bool) -> Path:
    path = tmp_path / "user.toml"
    path.write_text(
        US_PERSON_USER_TOML + (FORM67_READY_OVERRIDE if col_7 else ""), encoding="utf-8"
    )
    return path


@pytest.mark.parametrize("input_path", _UNPROVEN_SOURCE_RATE_INPUTS)
def test_unproven_source_rate_with_col_7_refuses_before_any_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, input_path: Path
) -> None:
    _refuse_every_write(monkeypatch)
    with pytest.raises(
        IbkrFaError, match=r"US/DIVIDEND: the source-currency tax rate can't be established"
    ):
        _run_form67(
            input_path=input_path,
            config_path=_fixture_config(tmp_path, col_7=True),
            output_path=tmp_path / "form-67.csv",
            financial_year=_FY_2025_26,
        )
    assert sorted(path.name for path in tmp_path.iterdir()) == ["user.toml"]


@pytest.mark.parametrize("input_path", _UNPROVEN_SOURCE_RATE_INPUTS)
def test_cli_unproven_source_rate_with_col_7_exits_4_without_output(
    tmp_path: Path, input_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code = main(
        [
            "form-67",
            "--input",
            str(input_path),
            "--config",
            str(_fixture_config(tmp_path, col_7=True)),
            "--output",
            str(tmp_path / "form-67.csv"),
            "--financial-year",
            "2025-26",
        ]
    )
    assert code == 4
    assert sorted(path.name for path in tmp_path.iterdir()) == ["user.toml"]
    err = capsys.readouterr().err
    assert "US/DIVIDEND: the source-currency tax rate can't be established" in err
    assert "remove the [[form67.tax_payable_overrides]] entry for US/DIVIDEND" in err


@pytest.mark.parametrize("input_path", _UNPROVEN_SOURCE_RATE_INPUTS)
def test_unproven_source_rate_without_col_7_is_blank(tmp_path: Path, input_path: Path) -> None:
    output_path = tmp_path / "form-67.csv"
    report = _run_form67(
        input_path=input_path,
        config_path=_fixture_config(tmp_path, col_7=False),
        output_path=output_path,
        financial_year=_FY_2025_26,
    )
    assert report.official_output_withheld
    assert not output_path.exists()
    assert report.per_entry_rows[0].treaty_rate_status is Form67TreatyRateStatus.UNPROVEN


@pytest.mark.parametrize("format_id", ["review-csv", "xlsx"])
@pytest.mark.parametrize("input_path", _UNPROVEN_SOURCE_RATE_INPUTS)
def test_unproven_source_rate_diagnostic_formats_leave_credit_columns_blank(
    tmp_path: Path, input_path: Path, format_id: str
) -> None:
    output_path = tmp_path / f"form-67.{'xlsx' if format_id == 'xlsx' else 'csv'}"
    report = _run_form67(
        input_path=input_path,
        config_path=_fixture_config(tmp_path, col_7=True),
        output_path=output_path,
        financial_year=_FY_2025_26,
        format_id=format_id,
    )
    if format_id == "xlsx":
        sheet = load_workbook(filename=output_path).active
        assert sheet is not None
        data = [
            "" if cell is None else str(cell)
            for cell in next(sheet.iter_rows(min_row=2, values_only=True))
        ]
    else:
        data = read_csv_rows(output_path)[1]
    # Col 7 is the override; cols 11 (section 90), 12 (section 91) and 13 (total) are blank.
    assert data[6] == "1000"
    assert data[10:13] == ["", "", ""]
    (row,) = report.per_entry_rows
    assert row.treaty_rate_status is Form67TreatyRateStatus.UNPROVEN
    assert row.tax_payable_normal_inr == Decimal("1000")


# ---- Report text for entries without a credit (#791) -----------------------

_FII_DATA_XQCU = (
    "Financial Instrument Information,Data,Stocks,XQCU,SAMPLE ISSUER CU LTD,"
    "7654321,KY99970R1085,KY99970R1085,SEHK,1,COMMON,,,,,,"
)
_DIVIDEND_XQCU = (
    "Dividends,Data,USD,12-09-2025,"
    "XQCU(KY99970R1085) Cash Dividend USD 0.20 per Share (Ordinary Dividend),"
    "20.00,,,,,,,,,,,"
)
_WHT_XQCU = (
    "Withholding Tax,Data,USD,12-09-2025,"
    "XQCU(KY99970R1085) Cash Dividend USD 0.20 per Share - KY Tax,"
    "-2.00,,,,,,,,,,,"
)
_US_LEG = (DIVIDEND_XQBA, WHT_XQBA)
_GB_LEG = (DIVIDEND_XQCR, WHT_XQCR)
_CH_LEG = (_DIVIDEND_XQCT, _WHT_XQCT)
_KY_LEG = (_DIVIDEND_XQCU, _WHT_XQCU)
# The fully-populated texts, which an entry without a credit must not get.
_POPULATED_TEXTS = ("POPULATED (cols 7, 9-13)", "populated from exact overrides")
_OTHERS_FILLED = "Indian tax payable and credit are filled for every other entry"


def _dividend_statement(tmp_path: Path, *legs: tuple[str, str]) -> Path:
    """The XQBA round trip, plus one ``(dividend, WHT)`` pair per leg."""
    path = tmp_path / "activity-statement.csv"
    path.write_text(
        with_totals(
            "\n".join(
                [
                    AS_HEADER.format(account="U1234567"),
                    FII_HEADER,
                    FII_DATA_XQBA,
                    FII_DATA_XQCR,
                    _FII_DATA_XQCT,
                    _FII_DATA_XQCU,
                    INCOME_TRADES_HEADER,
                    TRADE_OPEN_XQBA,
                    TRADE_CLOSE_XQBA,
                    OP_HEADER,
                    DIVIDENDS_HEADER,
                    *(dividend for dividend, _ in legs),
                    WHT_HEADER,
                    *(wht for _, wht in legs),
                    "",
                ]
            )
        ),
        encoding="utf-8",
    )
    return path


def _form67_cli(input_path: Path, config_path: Path, output_path: Path, *extra: str) -> int:
    return main(
        [
            "form-67",
            "--input",
            str(input_path),
            "--config",
            str(config_path),
            "--output",
            str(output_path),
            "--financial-year",
            "2025-26",
            *extra,
        ]
    )


def test_review_csv_names_a_no_dtaa_entry_without_section_91_inputs(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Col 7 without a credit isn't a filled entry, on either stream."""
    code = _form67_cli(
        _dividend_statement(tmp_path, _KY_LEG),
        _override_toml(tmp_path, KY="500"),
        tmp_path / "form-67.csv",
        "--format",
        "review-csv",
    )
    assert code == 0
    captured = capsys.readouterr()
    for stream in (captured.out, captured.err):
        assert not any(text in stream for text in _POPULATED_TEXTS)
        assert no_credit_line("KY/DIVIDEND", "no [form67.section_91] inputs") in stream
        assert _OTHERS_FILLED not in stream


# ---- Hong Kong takes the section 90 route (#1146) ---------------------------

# Privacy markers: the refusal must not echo the account, ISIN or description.
_HK_MARKERS = ("U9988776", "HK99970Q4401", "SYNTHDESC-6612")
_HK_PAYMENTS = (("06-12-2025", "-1.50"), ("09-11-2025", "-12.00"))


def _hong_kong_statement(tmp_path: Path) -> Path:
    """Two USD 10 Hong Kong dividends, each with withholding on its own date:
    USD 1.50 (15%) and USD 12, more than its dividend."""
    lines = [
        AS_HEADER.format(account="U9988776"),
        FII_HEADER,
        FII_DATA_XQBA,
        "Financial Instrument Information,Data,Stocks,XQCV,SYNTHDESC-6612,"
        "7654322,HK99970Q4401,HK99970Q4401,SEHK,1,COMMON,,,,,,",
        INCOME_TRADES_HEADER,
        TRADE_OPEN_XQBA,
        TRADE_CLOSE_XQBA,
        OP_HEADER,
        DIVIDENDS_HEADER,
        *(
            f"Dividends,Data,USD,{day},XQCV(HK99970Q4401) Cash Dividend USD 0.10 per Share "
            "(Ordinary Dividend),10.00,,,,,,,,,,,"
            for day, _tax in _HK_PAYMENTS
        ),
        WHT_HEADER,
        *(
            f"Withholding Tax,Data,USD,{day},XQCV(HK99970Q4401) Cash Dividend USD 0.10 per "
            f"Share - HK Tax,{tax},,,,,,,,,,,"
            for day, tax in _HK_PAYMENTS
        ),
        "",
    ]
    path = tmp_path / "activity-statement.csv"
    path.write_text(with_totals("\n".join(lines)), encoding="utf-8")
    return path


def test_hong_kong_withholding_above_its_dividend_refuses_on_the_treaty_route(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The USD 12 payment has no rate to test against the 5% cap, so the
    treaty-eligible tax can't be computed. Main's section 91 route, which
    needs no per-payment rate, produced the file."""
    config_path = tmp_path / "user.toml"
    config_path.write_text(
        SYNTHETIC_CONFIG.replace('"U1234567"', '"U9988776"')
        + "\n[form67]\n[[form67.tax_payable_overrides]]\n"
        + 'country = "HK"\nsource = "DIVIDEND"\ntax_payable_normal_inr = "500"\n'
        + "\n[form67.section_91]\n"
        + 'total_indian_tax_before_91_relief_inr = "30000"\ntotal_income_inr = "100000"\n',
        encoding="utf-8",
    )
    input_path = _hong_kong_statement(tmp_path)
    code = _form67_cli(input_path, config_path, tmp_path / "form-67.csv")
    assert code == 4
    assert sorted(path.name for path in tmp_path.iterdir()) == [
        "activity-statement.csv",
        "user.toml",
    ]
    err = capsys.readouterr().err
    assert refused_override(same_day_gap("HK/DIVIDEND"), "HK/DIVIDEND") in err
    for marker in _HK_MARKERS:
        assert marker not in err


def test_review_csv_names_only_the_entry_without_a_credit(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """US gets a section 90 credit; CH has col 7 but no vendored DTAA row."""
    code = _form67_cli(
        _dividend_statement(tmp_path, _US_LEG, _CH_LEG),
        _override_toml(tmp_path, US="999.99", CH="500"),
        tmp_path / "form-67.csv",
        "--format",
        "review-csv",
    )
    assert code == 0
    captured = capsys.readouterr()
    for stream in (captured.out, captured.err):
        assert not any(text in stream for text in _POPULATED_TEXTS)
        assert no_credit_line("CH/DIVIDEND", "not in the vendored DTAA table") in stream
        assert "US/DIVIDEND" not in stream
        assert _OTHERS_FILLED in stream


@pytest.mark.parametrize("input_path", _UNPROVEN_SOURCE_RATE_INPUTS)
def test_review_csv_names_an_unproven_entry_with_col_7(
    tmp_path: Path, input_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code = _form67_cli(
        input_path,
        _fixture_config(tmp_path, col_7=True),
        tmp_path / "form-67.csv",
        "--format",
        "review-csv",
    )
    assert code == 0
    captured = capsys.readouterr()
    for stream in (captured.out, captured.err):
        assert no_credit_line("US/DIVIDEND", "source-currency rate not established") in stream


def test_portal_csv_names_the_blank_entry_beside_a_complete_one(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The official file is withheld as before; the report no longer says the
    credit columns are populated, or that every entry is blank."""
    output_path = tmp_path / "form-67.csv"
    code = _form67_cli(
        _dividend_statement(tmp_path, _US_LEG, _GB_LEG),
        _override_toml(tmp_path, US="999.99"),
        output_path,
    )
    assert code == 3
    assert not output_path.exists()
    assert (tmp_path / "form-67-debug.csv").is_file()
    captured = capsys.readouterr()
    for stream in (captured.out, captured.err):
        assert not any(text in stream for text in _POPULATED_TEXTS)
        assert "GB/DIVIDEND: Indian tax payable and credit blank; complete both by hand" in stream
        assert "US/DIVIDEND" not in stream
    assert "its Indian tax payable and credit are blank" not in captured.out
    assert (
        "Official Form 67 CSV withheld: one or more entries lack Indian tax payable and credit."
    ) in captured.out


@pytest.mark.parametrize("tax_payable", ["0", "0.10"])
@pytest.mark.parametrize("format_id", ["review-csv", "xlsx"])
def test_zero_credit_row_is_written_and_counts_as_filled(
    tmp_path: Path, format_id: str, tax_payable: str
) -> None:
    """A credit of ₹0.10 files as 0 (#898), so it is shown like a zero credit."""
    output_path = tmp_path / f"form-67.{'xlsx' if format_id == 'xlsx' else 'csv'}"
    report = run_form67(
        input_path=_dividend_statement(tmp_path, _US_LEG),
        config_path=_override_toml(tmp_path, US=tax_payable),
        output_path=output_path,
        financial_year=_FY_2025_26,
        format_id=format_id,
    )
    assert output_path.is_file()
    (row,) = report.per_entry_rows
    assert row.readiness == _ZERO_CREDIT
    assert row.total_credit_inr == Decimal(tax_payable)
    assert row.filed.total_credit_inr == Decimal("0")
    assert "POPULATED (cols 7, 9-13)" in cli._render_form67_report(report)
    if format_id == "xlsx":
        sheet = load_workbook(filename=output_path).active
        assert sheet is not None
        detail = [cell.value for cell in next(sheet.iter_rows(min_row=2, max_row=2))]
    else:
        detail = read_csv_rows(output_path)[1]
    zero = 0 if format_id == "xlsx" else "0"
    blank = None if format_id == "xlsx" else ""
    # Cols 7, 11, 12 and 13: Indian tax, section 90 credit, section 91 credit, total.
    assert [detail[6], detail[10], detail[11], detail[12]] == [zero, zero, blank, zero]


def test_credit_rounding_to_zero_refuses_portal_csv_and_removes_stale_outputs(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """#898's ₹0.10 credit: the official CSV refuses it (exit 4) before writing,
    and removes the previous run's official and debug files."""
    output_path = tmp_path / "form-67.csv"
    for stale in (output_path, tmp_path / "form-67-debug.csv"):
        stale.write_text("stale", encoding="utf-8")
    code = _form67_cli(
        _dividend_statement(tmp_path, _US_LEG), _override_toml(tmp_path, US="0.10"), output_path
    )
    assert code == 4
    assert sorted(path.name for path in tmp_path.iterdir()) == [
        "activity-statement.csv",
        "user.toml",
    ]
    assert (
        "US/DIVIDEND: the computed credit rounds to ₹0 in whole rupees" in capsys.readouterr().err
    )


# ---- Same-day dividend lines without an account index (#1283) ---------------

# The issue's repro: a USD 100 Cash Dividend and a USD 100 Special Dividend of
# one ISIN on 2025-09-12, with USD 30 withheld against the Cash Dividend only.
# An Activity Statement sets no account index, so both lines share one payment
# key, and the withholding row can't say which of them it taxed.
_SAME_DAY_DIVIDENDS = (
    "Dividends,Data,USD,12-09-2025,"
    "XQBA(US99995C1027) Cash Dividend USD 10.00 per Share (Ordinary Dividend),"
    "100.00,,,,,,,,,,,",
    "Dividends,Data,USD,12-09-2025,"
    "XQBA(US99995C1027) Special Dividend USD 10.00 per Share,"
    "100.00,,,,,,,,,,,",
)
_SAME_DAY_WHT = (
    "Withholding Tax,Data,USD,12-09-2025,"
    "XQBA(US99995C1027) Cash Dividend USD 10.00 per Share - US Tax,"
    "-30.00,,,,,,,,,,,"
)
_ALLOCATION_REFUSAL = "US/DIVIDEND: the withholding can't be matched to a single dividend payment"


def _same_day_statement(tmp_path: Path) -> Path:
    path = tmp_path / "activity-statement.csv"
    path.write_text(
        with_totals(
            "\n".join(
                [
                    AS_HEADER.format(account="U1234567"),
                    FII_HEADER,
                    FII_DATA_XQBA,
                    INCOME_TRADES_HEADER,
                    TRADE_OPEN_XQBA,
                    TRADE_CLOSE_XQBA,
                    OP_HEADER,
                    DIVIDENDS_HEADER,
                    *_SAME_DAY_DIVIDENDS,
                    WHT_HEADER,
                    _SAME_DAY_WHT,
                    "",
                ]
            )
        ),
        encoding="utf-8",
    )
    return path


def test_same_day_dividend_lines_leave_form67_fsi_and_tr_credit_blank(tmp_path: Path) -> None:
    """#1283: with col 7 set, the pair was pooled into one 15% payment and a
    full credit was filed in Form 67, Schedule FSI and Schedule TR, although
    the taxed payment alone is withheld at 30%. Now every credit is blank."""
    from ibkr_fa.compute.schedule_tr import compute_schedule_tr_from_fsi  # noqa: PLC0415

    ctx = _compute_ctx(_same_day_statement(tmp_path), _override_toml(tmp_path, US="10000"))

    (entry,) = ctx.form67_result.entries
    assert entry.treaty_rate_status is Form67TreatyRateStatus.UNPROVEN
    assert entry.payment_allocation_unproven
    assert entry.highest_payment_rate_percent is None
    assert entry.tax_paid_rate_percent == Decimal("15")
    assert entry.tax_payable_normal_inr == Decimal("10000")
    assert entry.eligible_foreign_tax_inr is None
    assert entry.section_90_credit_inr is None
    assert entry.total_credit_inr is None
    fsi_us = next(
        r
        for r in ctx.fsi_result.rows
        if r.iso_alpha2 == "US" and r.head_of_income.value == "Other Sources"
    )
    assert fsi_us.foreign_tax_inr > 0
    assert fsi_us.tax_relief_inr is None
    tr_us = next(
        r for r in compute_schedule_tr_from_fsi(ctx.fsi_result).rows if r.iso_alpha2 == "US"
    )
    assert tr_us.tax_relief_inr_total is None
    assert tr_us.section_under_which_claimed is None


@pytest.mark.parametrize("command", ["run-all", "form-67"])
def test_same_day_dividend_lines_with_col_7_refuse_before_any_output(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], command: str
) -> None:
    """#1283: with col 7 set, run-all and standalone form-67 exit with the
    existing allocation refusal and write no output, as for Robinhood."""
    statement = _same_day_statement(tmp_path)
    config = _override_toml(tmp_path, US="10000")
    out = tmp_path / "out"
    out.mkdir()
    if command == "run-all":
        code = main(
            [
                "run-all",
                "--fy-input",
                str(statement),
                "--config",
                str(config),
                "--output-dir",
                str(out),
                "--financial-year",
                "2025-26",
                "--itr-form",
                "ITR-2",
                "--format",
                "review-csv",
            ]
        )
    else:
        code = _form67_cli(statement, config, out / "form-67.csv")

    assert code == 4
    assert list(out.iterdir()) == []
    assert _ALLOCATION_REFUSAL in capsys.readouterr().err
