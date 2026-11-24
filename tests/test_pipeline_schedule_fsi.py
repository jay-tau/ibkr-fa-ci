"""Tests for :func:`ibkr_fa.pipeline_fsi.run_schedule_fsi` and the report dataclass."""

from __future__ import annotations

import csv
import re
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from ibkr_fa.compute.wht_reversal_pairing import UnprovenWhtReversalError
from ibkr_fa.config import ScheduleOsIndianAdrRoutingMode
from ibkr_fa.exceptions import IbkrFaError, InvariantError, ParseError
from ibkr_fa.models.schedule_fsi_row import HeadOfIncome, ScheduleFsiRow
from ibkr_fa.output._cleanup import RunOutputs
from ibkr_fa.parser.format_detect import IbkrCsvFormat
from ibkr_fa.pipeline import FinancialYear
from ibkr_fa.pipeline_fsi import (
    FsiPerCountryRow,
    ScheduleFsiRunReport,
    run_schedule_fsi,
)
from tests._foreign_tax_refusals import (
    NO_ORIGINAL,
    orphan_wht_message,
    unproven_reversal_message,
)
from tests._ibkr_income_inputs import (
    ADR_ROWS,
    AS_HEADER,
    CONFIG_TEMPLATE,
    DIVIDEND_XQBA,
    DIVIDENDS_HEADER,
    FII_DATA_XQBA,
    FII_HEADER,
    INCOME_TRADES_HEADER,
    OP_HEADER,
    TRADE_CLOSE_XQBA,
    TRADE_OPEN_XQBA,
    UNDERLYING_ISSUER_CONFIG_TEMPLATE,
    WHT_HEADER,
    statement_body,
    write_config,
    write_statement,
)
from tests._income_blocks import with_totals

_FY_2025_26 = FinancialYear(start_year=2025)


def _assert_absent(message: str, *values: str) -> None:
    """``message`` prints none of ``values``, compared case-insensitively (#1047)."""
    for value in values:
        assert value.lower() not in message.lower(), value


# ---------------------------------------------------------------------------
# RunReport invariants
# ---------------------------------------------------------------------------


def _valid_report_kwargs(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "run_outputs": RunOutputs((), ()),
        "rows_written": 0,
        "per_country_rows": (),
        "total_income_inr_rounded": Decimal("0"),
        "total_foreign_tax_inr_rounded": Decimal("0"),
        "os_dividend_rows_consumed": 0,
        "os_interest_rows_consumed": 0,
        "cg_stcg_rows_consumed": 0,
        "cg_ltcg_rows_consumed": 0,
        "wht_rows_in_fy": 0,
        "excluded_cg_indian_source_count": 0,
        "excluded_cg_out_of_fy_count": 0,
        "excluded_ltcg_pre_cutover_count": 0,
        "excluded_ltcg_indian_source_count": 0,
        "excluded_ltcg_out_of_fy_count": 0,
        "interest_credits_out_of_fy_count": 0,
        "adr_routing_mode": ScheduleOsIndianAdrRoutingMode.DEPOSITARY,
        "output_path": Path("out.csv"),
        "account_id": "U1234567",
        "account_id_cross_checked": True,
        "input_format": IbkrCsvFormat.ACTIVITY_STATEMENT,
        "financial_year_start": date(2025, 4, 1),
        "financial_year_end": date(2026, 3, 31),
        "fx_policy_appropriate_for_ay": True,
        "wht_fx_policy_appropriate_for_ay": True,
        "portal_labels_assessment_year_start": 2026,
    }
    base.update(overrides)
    return base


def _negative_field_refusal(field: str) -> str:
    """The whole refusal of the report's own ``field >= 0`` guard, which names
    only ``field``; another guard rejecting the value names a different check."""
    return f"InvariantError: ScheduleFsiRunReport: {field} must be >= 0 (context keys: {field})"


def test_fsi_run_report_rejects_negative_counters() -> None:
    for field in [
        "rows_written",
        "os_dividend_rows_consumed",
        "os_interest_rows_consumed",
        "cg_stcg_rows_consumed",
        "cg_ltcg_rows_consumed",
        "wht_rows_in_fy",
        "excluded_cg_indian_source_count",
        "excluded_cg_out_of_fy_count",
        "excluded_ltcg_pre_cutover_count",
        "excluded_ltcg_indian_source_count",
        "excluded_ltcg_out_of_fy_count",
        "interest_credits_out_of_fy_count",
    ]:
        with pytest.raises(InvariantError) as exc:
            ScheduleFsiRunReport(**_valid_report_kwargs(**{field: -1}))
        assert exc.type is InvariantError
        assert str(exc.value) == _negative_field_refusal(field)


def test_fsi_run_report_rejects_negative_income_or_tax() -> None:
    for field in ["total_income_inr_rounded", "total_foreign_tax_inr_rounded"]:
        with pytest.raises(InvariantError) as exc:
            ScheduleFsiRunReport(**_valid_report_kwargs(**{field: Decimal("-1")}))
        assert exc.type is InvariantError
        assert str(exc.value) == _negative_field_refusal(field)


def test_fsi_run_report_rows_written_must_match_per_country_rows_len() -> None:
    """rows_written and len(per_country_rows) must be consistent: every
    emitted FSI row has exactly one summary entry in the tuple. A
    mismatch indicates a pipeline bug."""
    sample = FsiPerCountryRow(
        cbdt_country_code="2",
        country_name_portal="UNITED STATES OF AMERICA",
        head_of_income=HeadOfIncome.OTHER_SOURCES,
        income_inr_rounded=Decimal("100"),
        foreign_tax_inr_rounded=Decimal("10"),
        event_count=1,
    )
    # rows_written=2 but only 1 per-country tuple entry -> raises
    with pytest.raises(InvariantError):
        ScheduleFsiRunReport(
            **_valid_report_kwargs(
                rows_written=2,
                per_country_rows=(sample,),
            ),
        )


def test_fsi_run_report_account_id_must_be_non_empty() -> None:
    with pytest.raises(InvariantError):
        ScheduleFsiRunReport(**_valid_report_kwargs(account_id=""))


def test_fsi_run_report_fy_end_must_be_correctly_derived() -> None:
    with pytest.raises(InvariantError):
        ScheduleFsiRunReport(
            **_valid_report_kwargs(
                financial_year_start=date(2025, 4, 1),
                financial_year_end=date(2026, 3, 30),
            ),
        )


# ``"U***\\n"`` is not IBKR's mask (#854).
@pytest.mark.parametrize("account_id", ["U12345678", "U***\n"])
def test_fsi_run_report_unjustified_cross_check_skip_rejected(account_id: str) -> None:
    with pytest.raises(InvariantError):
        ScheduleFsiRunReport(
            **_valid_report_kwargs(
                account_id=account_id,
                account_id_cross_checked=False,
            ),
        )


def test_fsi_run_report_accepts_masked_account_without_cross_check() -> None:
    """A masked account (^U\\*+$) is allowed to skip cross-check; the
    masking renders cross-check structurally impossible."""
    report = ScheduleFsiRunReport(
        **_valid_report_kwargs(
            account_id="U**********",
            account_id_cross_checked=False,
        ),
    )
    assert report.account_id_cross_checked is False


def test_fsi_run_report_wht_fx_policy_appropriate_independently_trackable() -> None:
    """v1 evaluates both Rule115 (income) and Rule128 (WHT) against the
    same AY boundary so both bools converge. Locked here so a future
    Rule115/Rule128 verification-status divergence can be modeled."""
    # Both True (AY 2026-27 — default).
    report = ScheduleFsiRunReport(**_valid_report_kwargs())
    assert report.fx_policy_appropriate_for_ay is True
    assert report.wht_fx_policy_appropriate_for_ay is True
    # Both False (AY 2027-28+).
    report2 = ScheduleFsiRunReport(
        **_valid_report_kwargs(
            fx_policy_appropriate_for_ay=False,
            wht_fx_policy_appropriate_for_ay=False,
        ),
    )
    assert report2.fx_policy_appropriate_for_ay is False
    assert report2.wht_fx_policy_appropriate_for_ay is False
    # Divergent (hypothetical future state: Rule 76 verified before Rule 206).
    report3 = ScheduleFsiRunReport(
        **_valid_report_kwargs(
            fx_policy_appropriate_for_ay=False,
            wht_fx_policy_appropriate_for_ay=True,
        ),
    )
    assert report3.fx_policy_appropriate_for_ay is False
    assert report3.wht_fx_policy_appropriate_for_ay is True


def test_fsi_run_report_rejects_missing_taxpayer_id_flag_with_zero_rows() -> None:
    """has_foreign_tax_without_taxpayer_id cannot be True when no FSI
    rows were written — there are no rows that could be missing a TIN."""
    with pytest.raises(InvariantError):
        ScheduleFsiRunReport(
            **_valid_report_kwargs(
                rows_written=0,
                per_country_rows=(),
                has_foreign_tax_without_taxpayer_id=True,
            ),
        )


def test_fsi_run_report_allows_missing_taxpayer_id_flag_with_rows() -> None:
    sample = FsiPerCountryRow(
        cbdt_country_code="2",
        country_name_portal="UNITED STATES OF AMERICA",
        head_of_income=HeadOfIncome.OTHER_SOURCES,
        income_inr_rounded=Decimal("100"),
        foreign_tax_inr_rounded=Decimal("10"),
        event_count=1,
    )
    report = ScheduleFsiRunReport(
        **_valid_report_kwargs(
            rows_written=1,
            per_country_rows=(sample,),
            has_foreign_tax_without_taxpayer_id=True,
        ),
    )
    assert report.has_foreign_tax_without_taxpayer_id is True


# ---------------------------------------------------------------------------
# Section: Integration with an Activity Statement fixture
# ---------------------------------------------------------------------------

_USER_TOML_WITH_FORM67_OVERRIDE = (
    CONFIG_TEMPLATE
    + """
[form67]
[[form67.tax_payable_overrides]]
country = "US"
source = "DIVIDEND"
tax_payable_normal_inr = "9999"
"""
)


def test_run_schedule_fsi_emits_review_csv(tmp_path: Path) -> None:
    """Happy path: an Activity Statement with one STCG closure + one US
    dividend + matching US WHT produces an FSI file with one row per
    (country, head) covering both heads (CAPITAL_GAINS + OTHER_SOURCES)."""
    input_path = write_statement(tmp_path)
    config_path = write_config(tmp_path)
    out = tmp_path / "fsi.csv"

    report = run_schedule_fsi(
        input_path=input_path,
        config_path=config_path,
        output_path=out,
        financial_year=_FY_2025_26,
    )

    assert report.rows_written == 2  # (US, CAPITAL_GAINS) + (US, OTHER_SOURCES)
    assert report.cg_stcg_rows_consumed == 1
    assert report.os_dividend_rows_consumed == 1
    assert report.wht_rows_in_fy == 1
    assert report.account_id_cross_checked is True
    assert report.fx_policy_appropriate_for_ay is True
    assert report.wht_fx_policy_appropriate_for_ay is True
    assert out.exists()

    with out.open(encoding="utf-8-sig", newline="") as fh:
        rows = list(csv.reader(fh))
    # Header + 2 detail rows.
    assert len(rows) == 3
    assert rows[0][0] == "Country Code"
    # Both rows have CBDT code "2" (US).
    assert rows[1][0] == "2"
    assert rows[2][0] == "2"
    # Sorted by (cbdt_country_code, head_of_income.value): CAPITAL_GAINS before OTHER_SOURCES.
    assert rows[1][2] == "Capital Gains"
    assert rows[2][2] == "Other Sources"
    # OTHER_SOURCES row's tax cell is populated from the WHT layer.
    assert int(rows[2][4]) > 0  # foreign tax > 0


def test_report_amounts_are_the_filed_values_and_their_sums(
    tmp_path: Path, filed_without_rounding
) -> None:
    """#900: each report row carries its FSI row's filed whole rupees, and the
    totals add them, with no rounding of the report's own."""
    template = ScheduleFsiRow(
        cbdt_country_code="2",
        country_name_portal="UNITED STATES OF AMERICA",
        iso_alpha2="US",
        head_of_income=HeadOfIncome.OTHER_SOURCES,
        income_inr=Decimal("1"),
        foreign_tax_inr=Decimal("0"),
        event_count=1,
    )
    filed_without_rounding(
        template,
        income_inr=Decimal("7"),
        foreign_tax_inr=Decimal("3"),
        tax_payable_inr=None,
        tax_relief_inr=None,
    )

    report = run_schedule_fsi(
        input_path=write_statement(tmp_path),
        config_path=write_config(tmp_path),
        output_path=tmp_path / "fsi.csv",
        financial_year=_FY_2025_26,
    )

    assert [
        (row.income_inr_rounded, row.foreign_tax_inr_rounded) for row in report.per_country_rows
    ] == [
        (Decimal("7"), Decimal("3")),
        (Decimal("7"), Decimal("3")),
    ]
    assert (report.total_income_inr_rounded, report.total_foreign_tax_inr_rounded) == (
        Decimal("14"),
        Decimal("6"),
    )


def test_exact_dividend_tax_clears_warning_despite_unrelated_capital_gain(
    tmp_path: Path,
) -> None:
    input_path = write_statement(tmp_path)
    config_path = write_config(
        tmp_path,
        template=_USER_TOML_WITH_FORM67_OVERRIDE,
    )

    report = run_schedule_fsi(
        input_path=input_path,
        config_path=config_path,
        output_path=tmp_path / "fsi.csv",
        financial_year=_FY_2025_26,
    )

    assert not report.has_unresolved_indian_tax_payable


def test_run_schedule_fsi_underlying_issuer_without_wht_proceeds(tmp_path: Path) -> None:
    """UNDERLYING_ISSUER mode is OK if WHT is absent — the silent-
    misattribution risk only materialises when WHT is being layered."""
    body = statement_body(include_dividends=True, include_wht=False)
    input_path = write_statement(tmp_path, body=body)
    config_path = write_config(
        tmp_path,
        template=UNDERLYING_ISSUER_CONFIG_TEMPLATE,
    )

    report = run_schedule_fsi(
        input_path=input_path,
        config_path=config_path,
        output_path=tmp_path / "fsi.csv",
        financial_year=_FY_2025_26,
    )
    assert report.adr_routing_mode == ScheduleOsIndianAdrRoutingMode.UNDERLYING_ISSUER
    assert report.wht_rows_in_fy == 0


def test_run_schedule_fsi_rejects_flex_query_input(tmp_path: Path) -> None:
    flex_fixture = Path(__file__).parent / "fixtures" / "e2e_year_end_2025.csv"
    assert flex_fixture.exists()
    config_path = write_config(tmp_path)
    out = tmp_path / "fsi.csv"
    out.write_text("stale\n", encoding="utf-8")  # an earlier run's output
    with pytest.raises(ParseError) as exc:
        run_schedule_fsi(
            input_path=flex_fixture,
            config_path=config_path,
            output_path=out,
            financial_year=_FY_2025_26,
        )
    assert exc.type is ParseError
    assert str(exc.value) == (
        "ParseError in section '<input format>' at row 0, column '<format detection>': "
        "Schedule FSI pipeline requires an IBKR Activity Statement input; got 'flex-query'. "
        "Flex Query inputs are unsupported because Schedule FSI depends on multiple Activity "
        "Statement sections (Dividends, Trades, Interest, Withholding Tax) that Flex doesn't "
        "currently expose in the same shape (#12 / #104)."
    )
    assert list(tmp_path.iterdir()) == [config_path]


def test_run_schedule_fsi_xlsx_format(tmp_path: Path) -> None:
    """Format selection routes through output_binding(Subcommand.FSI, ...)."""
    input_path = write_statement(tmp_path)
    config_path = write_config(tmp_path)
    out = tmp_path / "fsi.xlsx"

    report = run_schedule_fsi(
        input_path=input_path,
        config_path=config_path,
        output_path=out,
        financial_year=_FY_2025_26,
        format_id="xlsx",
    )
    assert out.exists()
    assert report.output_path == out


def test_run_schedule_fsi_empty_account(tmp_path: Path) -> None:
    """An Activity Statement with no Dividends + no WHT (TRADES only)
    produces an FSI with just CG rows. The remaining counters are 0."""
    body = statement_body(include_dividends=False, include_wht=False)
    input_path = write_statement(tmp_path, body=body)
    config_path = write_config(tmp_path)
    out = tmp_path / "fsi.csv"

    report = run_schedule_fsi(
        input_path=input_path,
        config_path=config_path,
        output_path=out,
        financial_year=_FY_2025_26,
    )

    assert report.os_dividend_rows_consumed == 0
    assert report.wht_rows_in_fy == 0
    assert report.cg_stcg_rows_consumed == 1
    assert report.rows_written == 1  # only (US, CAPITAL_GAINS)
    assert report.per_country_rows[0].head_of_income is HeadOfIncome.CAPITAL_GAINS
    assert report.total_foreign_tax_inr_rounded == Decimal("0")


# ---------------------------------------------------------------------------
# T3 r1 P1 + P2 guard tests
# ---------------------------------------------------------------------------


def test_run_schedule_fsi_orphan_wht_per_event_same_country_raises(
    tmp_path: Path,
) -> None:
    """T3 r2 P2 (tightening of T3 r1 P1): even when an in-FY WHT row's
    country DOES have a matching in-FY dividend, the WHT is still
    orphan if its ISIN doesn't match any in-FY dividend's ISIN. This
    catches the per-event misattribution path where, e.g., an in-FY
    XQCA WHT (for a prior-FY XQCA dividend that posted late) is paired
    with an unrelated in-FY XQBA dividend — same country (US), but
    the WHT's underlying event is not in the FY's compute output.
    Without per-ISIN matching, the XQCA WHT would be silently layered
    onto the XQBA row (or any same-country interest row)."""
    # Construct a body with: XQBA dividend in-FY + XQCA WHT in-FY
    # (but NO XQCA dividend). Both ISINs share the US country prefix,
    # so the country-level guard alone would pass; the per-ISIN guard
    # must fire.
    xqca_only_wht = (
        "Withholding Tax,Data,USD,12-09-2025,"
        "XQCA(US99972B1017) Cash Dividend USD 0.75 per Share - US Tax,"
        "-0.11,,,,,,,,,,,"
    )
    body_lines = [
        "{header}",
        FII_HEADER,
        FII_DATA_XQBA,
        INCOME_TRADES_HEADER,
        TRADE_OPEN_XQBA,
        TRADE_CLOSE_XQBA,
        OP_HEADER,
        DIVIDENDS_HEADER,
        DIVIDEND_XQBA,  # XQBA US99995C1027 dividend in-FY
        WHT_HEADER,
        xqca_only_wht,  # XQCA US99972B1017 WHT in-FY — no matching XQCA dividend
        "",
    ]
    body = "\n".join(body_lines)
    input_path = tmp_path / "activity-statement.csv"
    input_path.write_text(
        with_totals(body.format(header=AS_HEADER.format(account="U1234567"))),
        encoding="utf-8",
    )
    config_path = write_config(tmp_path)
    out = tmp_path / "fsi.csv"
    out.write_text("stale\n", encoding="utf-8")  # an earlier run's output

    with pytest.raises(IbkrFaError) as exc:
        run_schedule_fsi(
            input_path=input_path,
            config_path=config_path,
            output_path=out,
            financial_year=_FY_2025_26,
        )
    assert exc.type is IbkrFaError
    msg = str(exc.value)
    # The refusal counts the orphan XQCA event; its ISIN and date are input
    # and aren't printed (#1047).
    assert msg == orphan_wht_message(1)
    _assert_absent(msg, "US99972B1017", "2025-09-12")
    assert sorted(tmp_path.iterdir()) == sorted([input_path, config_path])


def test_run_schedule_fsi_orphan_wht_no_matching_dividend_raises(tmp_path: Path) -> None:
    """T3 r1 P1: an in-FY WHT row whose source country has NO matching
    in-FY foreign dividend in `os_result.lines` is structurally an
    orphan — the FSI WHT aggregator would silently absorb it into
    any same-country broker-interest row. Pipeline hard-fails with
    an actionable error citing §90/§91 + Rule 128(5)."""
    body = statement_body(include_dividends=False, include_wht=True)
    input_path = write_statement(tmp_path, body=body)
    config_path = write_config(tmp_path)
    out = tmp_path / "fsi.csv"
    out.write_text("stale\n", encoding="utf-8")  # an earlier run's output

    with pytest.raises(IbkrFaError) as exc:
        run_schedule_fsi(
            input_path=input_path,
            config_path=config_path,
            output_path=out,
            financial_year=_FY_2025_26,
        )
    assert exc.type is IbkrFaError
    msg = str(exc.value)
    # The refusal counts the orphan XQBA event and cites §90/§91 + Rule
    # 128(5); the event's ISIN and date are input and aren't printed (#1047).
    assert msg == orphan_wht_message(1)
    _assert_absent(msg, "US99995C1027", "2025-09-12")
    assert sorted(tmp_path.iterdir()) == sorted([input_path, config_path])


def test_run_schedule_fsi_partial_wht_refund_on_later_date_does_not_raise(
    tmp_path: Path,
) -> None:
    """T2 r6 + T3 r6 P1: a WHT refund (positive amount) of part of a
    payment's tax that posts on a LATER date than the original WHT must
    NOT trigger a false-positive orphan-WHT error. The refund row's
    payment_date is the REVERSAL booking date, NOT the original
    dividend's date — verified against
    tests/fixtures/...paired_canceling...csv and the WithholdingTax
    model docstring ("for reversals, re-credited"). It reverses one of
    the payment's two withholding rows exactly (#893 refuses a refund
    with no equal-and-opposite original).

    The orphan-WHT guard must skip POSITIVE-amount (reversal) WHT
    rows because reversals reduce the FTC claim — they don't inflate
    it — so they cannot cause misattribution regardless of date."""
    xqba_paid_wht = (
        "Withholding Tax,Data,USD,12-09-2025,"
        "XQBA(US99995C1027) Cash Dividend USD 0.24 per Share - US Tax,"
        "-0.70,,,,,,,,,,,"
    )
    xqba_refunded_wht = (
        "Withholding Tax,Data,USD,12-09-2025,"
        "XQBA(US99995C1027) Cash Dividend USD 0.24 per Share - US Tax,"
        "-0.30,,,,,,,,,,,"
    )
    xqba_partial_refund = (
        "Withholding Tax,Data,USD,15-11-2025,"  # 2 months LATER
        "XQBA(US99995C1027) Cash Dividend USD 0.24 per Share - US Tax,"
        "+0.30,Re,,,,,,,,,,"
    )
    body_lines = [
        "{header}",
        FII_HEADER,
        FII_DATA_XQBA,
        INCOME_TRADES_HEADER,
        TRADE_OPEN_XQBA,
        TRADE_CLOSE_XQBA,
        OP_HEADER,
        DIVIDENDS_HEADER,
        DIVIDEND_XQBA,  # 12-09-2025 dividend
        WHT_HEADER,
        xqba_paid_wht,  # 12-09-2025 paid (matched)
        xqba_refunded_wht,  # 12-09-2025 paid (matched), reversed below
        xqba_partial_refund,  # 15-11-2025 refund (no matching dividend on that date)
        "",
    ]
    body = "\n".join(body_lines)
    input_path = tmp_path / "activity-statement.csv"
    input_path.write_text(
        with_totals(body.format(header=AS_HEADER.format(account="U1234567"))),
        encoding="utf-8",
    )
    config_path = write_config(tmp_path)

    # Per-ISIN net: -0.70 - 0.30 + 0.30 = -0.70 (paid). The refund row's
    # date 2025-11-15 has no matching dividend, but it's positive
    # (reversal), so the orphan check must skip it. Pipeline
    # proceeds; FSI emits a US OTHER_SOURCES row with positive
    # foreign_tax_inr (net-paid INR).
    report = run_schedule_fsi(
        input_path=input_path,
        config_path=config_path,
        output_path=tmp_path / "fsi.csv",
        financial_year=_FY_2025_26,
    )
    assert report.wht_rows_in_fy == 3
    us_os_rows = [
        r
        for r in report.per_country_rows
        if r.cbdt_country_code == "2" and r.head_of_income is HeadOfIncome.OTHER_SOURCES
    ]
    assert len(us_os_rows) == 1
    # Rule 128 converts each row at the SBI TT buying rate of the last day of
    # the month before it: 2025-08-31 is a Sunday, so the Sep rows carry back
    # to 2025-08-30's 87.70; the Nov refund takes 2025-10-31's 88.20.
    # -0.70 * 87.70 - 0.30 * 87.70 + 0.30 * 88.20 = -61.39 - 26.31 + 26.46
    # = -61.24, so ₹61 paid (HALF_UP). Ignoring the refund files ₹88.
    assert us_os_rows[0].foreign_tax_inr_rounded == Decimal("61")


def test_run_schedule_fsi_orphan_wht_within_isin_across_fy_raises(
    tmp_path: Path,
) -> None:
    """T5 r5 P2: a current-FY WHT row that is a late adjustment for a
    PRIOR-FY dividend of an ISIN that ALSO has a current-FY dividend
    should still be flagged as orphan. Without per-(ISIN, payment_date)
    matching, my prior per-ISIN guard would have let it through (the
    ISIN appears in `os_result.lines`), and the WHT would have been
    silently layered onto the unrelated current-FY dividend's
    (country, OTHER_SOURCES) row.

    Construct: XQBA dividend on 12-09-2025 (in-FY) + XQBA WHT on
    20-12-2025 (in-FY, but adjustment for a prior-FY 12-09-2024
    dividend). Same ISIN, different payment_date — must raise."""
    xqba_current_fy_dividend = (
        "Dividends,Data,USD,12-09-2025,"
        "XQBA(US99995C1027) Cash Dividend USD 0.24 per Share (Ordinary Dividend),"
        "2.40,,,,,,,,,,,"
    )
    xqba_late_adjustment_wht = (
        "Withholding Tax,Data,USD,20-12-2025,"
        "XQBA(US99995C1027) Cash Dividend USD 0.20 per Share - US Tax,"
        "-0.30,,,,,,,,,,,"
    )
    body_lines = [
        "{header}",
        FII_HEADER,
        FII_DATA_XQBA,
        INCOME_TRADES_HEADER,
        TRADE_OPEN_XQBA,
        TRADE_CLOSE_XQBA,
        OP_HEADER,
        DIVIDENDS_HEADER,
        xqba_current_fy_dividend,
        WHT_HEADER,
        xqba_late_adjustment_wht,  # in-FY but different payment_date
        "",
    ]
    body = "\n".join(body_lines)
    input_path = tmp_path / "activity-statement.csv"
    input_path.write_text(
        with_totals(body.format(header=AS_HEADER.format(account="U1234567"))),
        encoding="utf-8",
    )
    config_path = write_config(tmp_path)
    out = tmp_path / "fsi.csv"
    out.write_text("stale\n", encoding="utf-8")  # an earlier run's output

    with pytest.raises(IbkrFaError) as exc:
        run_schedule_fsi(
            input_path=input_path,
            config_path=config_path,
            output_path=out,
            financial_year=_FY_2025_26,
        )
    assert exc.type is IbkrFaError
    msg = str(exc.value)
    # The refusal counts the orphan event; its XQBA ISIN and 2025-12-20
    # payment_date are input and aren't printed (#1047).
    assert msg == orphan_wht_message(1)
    _assert_absent(msg, "US99995C1027", "2025-12-20")
    assert sorted(tmp_path.iterdir()) == sorted([input_path, config_path])


def test_run_schedule_fsi_refund_isin_in_paid_country_does_not_raise(
    tmp_path: Path,
) -> None:
    """T4 r3 R3-1: a refunded ISIN (net not paid) within a country
    whose overall net is paid (because another ISIN's paid amount
    dominates) MUST NOT trigger a false-positive orphan-WHT error.
    The refunded ISIN's contribution is not a credit inflation —
    there's no misattribution risk.

    Construct: XQBA WHT -1.00 (paid, matched by an XQBA dividend in
    os_result.lines) + XQCA WHT -0.30 and its +0.30 refund in the same
    FX month (NO matching XQCA dividend in os_result). Country US net
    = -1.00 (paid); but XQCA's own net is zero. The orphan check must
    scope to paid-net ISINs only — XQCA should NOT be flagged."""
    xqba_paid = (
        "Withholding Tax,Data,USD,12-09-2025,"
        "XQBA(US99995C1027) Cash Dividend USD 0.24 per Share - US Tax,"
        "-1.00,,,,,,,,,,,"
    )
    xqca_paid = (
        "Withholding Tax,Data,USD,12-09-2025,"
        "XQCA(US99972B1017) Cash Dividend USD 0.24 per Share - US Tax,"
        "-0.30,,,,,,,,,,,"
    )
    xqca_refund = (
        "Withholding Tax,Data,USD,15-09-2025,"
        "XQCA(US99972B1017) Cash Dividend USD 0.24 per Share - US Tax Refund,"
        "+0.30,,,,,,,,,,,"
    )
    body_lines = [
        "{header}",
        FII_HEADER,
        FII_DATA_XQBA,
        INCOME_TRADES_HEADER,
        TRADE_OPEN_XQBA,
        TRADE_CLOSE_XQBA,
        OP_HEADER,
        DIVIDENDS_HEADER,
        DIVIDEND_XQBA,  # XQBA dividend in-FY (matches XQBA WHT)
        WHT_HEADER,
        xqba_paid,  # paid: matched
        xqca_paid,  # paid: no matching XQCA dividend, refunded below
        xqca_refund,  # refund: XQCA nets to zero
        "",
    ]
    body = "\n".join(body_lines)
    input_path = tmp_path / "activity-statement.csv"
    input_path.write_text(
        with_totals(body.format(header=AS_HEADER.format(account="U1234567"))),
        encoding="utf-8",
    )
    config_path = write_config(tmp_path)

    # Should proceed without raising — the refunded XQCA row doesn't
    # trigger misattribution.
    report = run_schedule_fsi(
        input_path=input_path,
        config_path=config_path,
        output_path=tmp_path / "fsi.csv",
        financial_year=_FY_2025_26,
    )
    assert report.wht_rows_in_fy == 3
    us_os_rows = [
        r
        for r in report.per_country_rows
        if r.cbdt_country_code == "2" and r.head_of_income is HeadOfIncome.OTHER_SOURCES
    ]
    assert len(us_os_rows) == 1
    # Rule 128 converts each row at the SBI TT buying rate of the last day of
    # the month before it: 2025-08-31 is a Sunday, so all three Sep rows carry
    # back to 2025-08-30's 87.70. -1.00 * 87.70 - 0.30 * 87.70 + 0.30 * 87.70
    # = -87.70, so ₹88 paid (HALF_UP). Ignoring the refund files ₹114.
    assert us_os_rows[0].foreign_tax_inr_rounded == Decimal("88")


def test_run_schedule_fsi_orphan_wht_inr_net_paid_despite_usd_refund_raises(
    tmp_path: Path,
) -> None:
    """T3 r3 P2 / T2 r3 P2 — sign-flip pathology test.

    When a paid + refund pair crosses Rule 128 FX months with different
    SBI TTBR rates, the USD net can be zero while the INR net is still
    negative (paid). The compute layer then keeps a positive INR foreign
    tax for the country, so the orphan-WHT guard, which reads INR nets,
    MUST fire: the credit could otherwise be absorbed by a same-country
    row it doesn't belong to. (A refund pairs only with an equal-and-
    opposite paid row (#893), so zero is the only USD net a proven pair
    can have.)

    Construct: XQCA paid -10.00 USD on 2025-10-12 and refunded +10.00 USD
    on 2025-11-15, with no XQCA dividend. Rule 128 takes the SBI TT buying
    rate of the last day of the month before each: 88.35 (2025-09-30) and
    88.20 (2025-10-31). USD net: -10.00 + 10.00 = 0.00. INR net:
    -883.50 + 882.00 = -1.50, so the tax is still paid in INR.
    """
    paid_wht = (
        "Withholding Tax,Data,USD,12-10-2025,"
        "XQCA(US99972B1017) Cash Dividend USD 0.24 per Share - US Tax,"
        "-10.00,,,,,,,,,,,"
    )
    refund_wht = (
        "Withholding Tax,Data,USD,15-11-2025,"
        "XQCA(US99972B1017) Cash Dividend USD 0.24 per Share - US Tax Refund,"
        "+10.00,,,,,,,,,,,"
    )
    body_lines = [
        "{header}",
        FII_HEADER,
        FII_DATA_XQBA,
        INCOME_TRADES_HEADER,
        TRADE_OPEN_XQBA,
        TRADE_CLOSE_XQBA,
        OP_HEADER,
        WHT_HEADER,
        paid_wht,
        refund_wht,
        "",
    ]
    body = "\n".join(body_lines)
    input_path = tmp_path / "activity-statement.csv"
    input_path.write_text(
        with_totals(body.format(header=AS_HEADER.format(account="U1234567"))),
        encoding="utf-8",
    )
    config_path = write_config(tmp_path)
    out = tmp_path / "fsi.csv"
    out.write_text("stale\n", encoding="utf-8")  # an earlier run's output

    # No matching XQCA dividend in-FY → orphan WHT → the guard fires on
    # the INR net (a guard reading the zero USD net would let it pass).
    with pytest.raises(IbkrFaError) as exc:
        run_schedule_fsi(
            input_path=input_path,
            config_path=config_path,
            output_path=out,
            financial_year=_FY_2025_26,
        )
    assert exc.type is IbkrFaError
    # Counted, without the orphan's ISIN or dates (#1047).
    assert str(exc.value) == orphan_wht_message(1)
    _assert_absent(str(exc.value), "US99972B1017", "2025-10-12", "2025-11-15")
    assert sorted(tmp_path.iterdir()) == sorted([input_path, config_path])


def test_run_schedule_fsi_pure_refund_only_wht_refuses(tmp_path: Path) -> None:
    """#893: a refund with no original in the statement cancels tax of
    an unproven year, so netting it, or dropping its country as a net
    refund, would guess. The run refuses.

    Construct: only WHT in the FY is a positive refund row for an
    ISIN with no matching in-FY dividend or withholding."""
    refund_wht = (
        "Withholding Tax,Data,USD,12-09-2025,"
        "XQCA(US99972B1017) Cash Dividend USD 0.24 per Share - US Tax Refund,"
        "+0.36,,,,,,,,,,,"
    )
    body_lines = [
        "{header}",
        FII_HEADER,
        FII_DATA_XQBA,
        INCOME_TRADES_HEADER,
        TRADE_OPEN_XQBA,
        TRADE_CLOSE_XQBA,
        OP_HEADER,
        WHT_HEADER,
        refund_wht,
        "",
    ]
    body = "\n".join(body_lines)
    input_path = tmp_path / "activity-statement.csv"
    input_path.write_text(
        with_totals(body.format(header=AS_HEADER.format(account="U1234567"))),
        encoding="utf-8",
    )
    config_path = write_config(tmp_path)

    with pytest.raises(UnprovenWhtReversalError) as exc:
        run_schedule_fsi(
            input_path=input_path,
            config_path=config_path,
            output_path=tmp_path / "fsi.csv",
            financial_year=_FY_2025_26,
        )
    # Named by why, not by its ISIN, amount, currency or date (#1047).
    assert str(exc.value) == unproven_reversal_message(NO_ORIGINAL)
    _assert_absent(str(exc.value), "US99972B1017", "0.36", "2025-09-12")
    assert re.search(r"(?i)\bUSD\b", str(exc.value)) is None
    assert not (tmp_path / "fsi.csv").exists()


def test_run_schedule_fsi_net_zero_country_does_not_raise(tmp_path: Path) -> None:
    """T5 r2 P2 sister-case: when a country's WHT nets to zero
    (refund exactly cancels paid), compute drops the country and the
    guard must not fail-fast. Construct paired paid+refund WHT rows
    for the SAME ISIN summing to zero."""
    paid_wht = (
        "Withholding Tax,Data,USD,12-09-2025,"
        "XQCA(US99972B1017) Cash Dividend USD 0.24 per Share - US Tax,"
        "-0.36,,,,,,,,,,,"
    )
    refund_wht = (
        "Withholding Tax,Data,USD,15-09-2025,"
        "XQCA(US99972B1017) Cash Dividend USD 0.24 per Share - US Tax Refund,"
        "+0.36,,,,,,,,,,,"
    )
    body_lines = [
        "{header}",
        FII_HEADER,
        FII_DATA_XQBA,
        INCOME_TRADES_HEADER,
        TRADE_OPEN_XQBA,
        TRADE_CLOSE_XQBA,
        OP_HEADER,
        WHT_HEADER,
        paid_wht,
        refund_wht,
        "",
    ]
    body = "\n".join(body_lines)
    input_path = tmp_path / "activity-statement.csv"
    input_path.write_text(
        with_totals(body.format(header=AS_HEADER.format(account="U1234567"))),
        encoding="utf-8",
    )
    config_path = write_config(tmp_path)

    report = run_schedule_fsi(
        input_path=input_path,
        config_path=config_path,
        output_path=tmp_path / "fsi.csv",
        financial_year=_FY_2025_26,
    )
    assert report.wht_rows_in_fy == 2  # both rows ingested in-FY
    assert report.total_foreign_tax_inr_rounded == Decimal("0")  # net zero → no credit


def test_run_schedule_fsi_underlying_issuer_guard_only_fires_on_in_fy_wht(
    tmp_path: Path,
) -> None:
    """The per-ISIN orphan-WHT guard MUST filter WHT to in-FY before
    checking. A multi-year Activity Statement (needed for FIFO / LTCG
    matching) commonly contains prior-FY WHT entries; those don't
    contribute to the current FY's FSI and must not trip the guard.

    Build a statement whose WHT row's payment date is OUTSIDE FY
    2025-26 (set to 2024-09-12 — prior FY). With UNDERLYING_ISSUER
    routing, the per-ISIN orphan guard must NOT fire because the WHT
    is not in-FY.

    Note: pre-#225 this also covered the now-removed broad
    UNDERLYING_ISSUER + WHT guard at the pipeline entry; with the
    structural fix in place, only the per-ISIN orphan guard remains
    in this region of the code and the in-FY filter still applies.
    """
    # The dividend body uses 12-09-2025 (in FY 2025-26); add a prior-FY
    # WHT to assert the guard ignores out-of-FY entries.
    prior_fy_wht = (
        "Withholding Tax,Data,USD,12-09-2024,"
        "XQBA(US99995C1027) Cash Dividend USD 0.24 per Share - US Tax,"
        "-0.36,,,,,,,,,,,"
    )
    # Build body with dividend present (in-FY) and a SINGLE prior-FY
    # WHT row (no current-FY WHT). Use 2024-09-12 dividend so the
    # statement period needs to span both FYs.
    body_lines = [
        "{header}",
        FII_HEADER,
        FII_DATA_XQBA,
        INCOME_TRADES_HEADER,
        TRADE_OPEN_XQBA,
        TRADE_CLOSE_XQBA,
        OP_HEADER,
        DIVIDENDS_HEADER,
        DIVIDEND_XQBA,
        WHT_HEADER,
        prior_fy_wht,
        "",
    ]
    body = "\n".join(body_lines)
    # Override the statement period to span 2024-04-01 → 2026-03-31.
    multi_year_header = AS_HEADER.replace(
        '"April 1, 2025 - March 31, 2026"',
        '"April 1, 2024 - March 31, 2026"',
    )
    input_path = tmp_path / "activity-statement.csv"
    input_path.write_text(
        with_totals(body.format(header=multi_year_header.format(account="U1234567"))),
        encoding="utf-8",
    )
    config_path = write_config(
        tmp_path,
        template=UNDERLYING_ISSUER_CONFIG_TEMPLATE,
    )

    # UNDERLYING_ISSUER mode + ONLY prior-FY WHT → guard should NOT
    # fire (the in-FY WHT subset is empty). The orphan-WHT-cross-
    # source guard also doesn't fire (no in-FY WHT countries).
    report = run_schedule_fsi(
        input_path=input_path,
        config_path=config_path,
        output_path=tmp_path / "fsi.csv",
        financial_year=_FY_2025_26,
    )
    assert report.wht_rows_in_fy == 0
    assert report.adr_routing_mode == ScheduleOsIndianAdrRoutingMode.UNDERLYING_ISSUER


# ============================================================================
# Issue #225: end-to-end Schedule FSI with XQBA + an Indian ADR under UNDERLYING_ISSUER
# ============================================================================


def test_run_schedule_fsi_underlying_issuer_with_xqba_and_infy_succeeds_issue_225(
    tmp_path: Path,
) -> None:
    """Pipeline-level e2e for #225: XQBA + INFY ADR with WHT under
    UNDERLYING_ISSUER routing. Pre-fix: pipeline blocked via the
    broad UNDERLYING_ISSUER+WHT guard. Post-fix: pipeline succeeds;
    INFY WHT is symmetrically skipped, US FSI row reflects XQBA
    only."""
    input_path = write_statement(tmp_path, second=ADR_ROWS)
    config_path = write_config(tmp_path, template=UNDERLYING_ISSUER_CONFIG_TEMPLATE)

    report = run_schedule_fsi(
        input_path=input_path,
        config_path=config_path,
        output_path=tmp_path / "fsi.csv",
        financial_year=_FY_2025_26,
    )

    # Pipeline succeeded — the broad UNDERLYING_ISSUER+WHT guard is gone.
    assert report.adr_routing_mode == ScheduleOsIndianAdrRoutingMode.UNDERLYING_ISSUER
    # wht_rows_in_fy counts ALL ingested in-FY WHT rows (pre-skip,
    # for audit/diagnostics) — both XQBA and INFY.
    assert report.wht_rows_in_fy == 2
    # The structural fix is visible in the per-country rows: the US
    # OTHER_SOURCES row's foreign tax must reflect XQBA only (NOT
    # XQBA + INFY). cbdt_country_code "2" is US.
    us_other_rows = [
        r
        for r in report.per_country_rows
        if r.cbdt_country_code == "2" and r.head_of_income is HeadOfIncome.OTHER_SOURCES
    ]
    assert len(us_other_rows) == 1
    us_row = us_other_rows[0]
    # XQBA's WHT only. Rule 128 takes the SBI TT buying rate of the last day
    # of the month before the deduction: 2025-08-31 is a Sunday, so it
    # carries back to 2025-08-30's 87.70. 0.36 * 87.70 = 31.572, so ₹32
    # (HALF_UP). Absorbing the ADR's 0.75 would give 1.11 * 87.70 = 97.347, ₹97.
    assert us_row.foreign_tax_inr_rounded == Decimal("32")
