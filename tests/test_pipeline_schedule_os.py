"""Tests for :func:`ibkr_fa.pipeline_os.run_schedule_os` and friends.

Covers:

* :class:`ScheduleOsRunReport` invariant enforcement.
* :class:`FinancialYear` date / AY / label derivations.
* :func:`run_schedule_os` end-to-end wiring across the happy path,
  the Indian-source partitioning path, the non-ordinary distribution
  refusal (#1310), the rejection paths (Flex Query, wrong period,
  account mismatch), and the cross-AY flag behaviour for FY 2025-26
  vs FY 2026-27.
"""

from __future__ import annotations

import csv
import dataclasses
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from ibkr_fa import pipeline_os
from ibkr_fa.cli import main
from ibkr_fa.compute.schedule_os import (
    NonOrdinaryDistributionError,
    UnrecognisedDistributionTypeError,
)
from ibkr_fa.exceptions import ConfigError, InvariantError, ParseError
from ibkr_fa.models import DistributionKind, InterestCredit
from ibkr_fa.pipeline import FinancialYear, IbkrCsvFormat
from ibkr_fa.pipeline_cg import ScheduleCgRunReport
from ibkr_fa.pipeline_cg_ltcg import ScheduleCgLtcgRunReport
from ibkr_fa.pipeline_fa_models import RunReport
from ibkr_fa.pipeline_form67_models import Form67RunReport
from ibkr_fa.pipeline_fsi_models import ScheduleFsiRunReport
from ibkr_fa.pipeline_os import run_schedule_os
from ibkr_fa.pipeline_os_models import ScheduleOsRunReport
from ibkr_fa.pipeline_tr_models import ScheduleTrRunReport
from tests._crash_reports import assert_prints_no_marker
from tests._exact_refusals import INEXACT_SUM
from tests._fx_stubs import AuditedFxStub
from tests._ibkr_account_refusals import ACCOUNT_MISMATCH_REFUSAL, MASKED_ACCOUNT_REFUSAL
from tests._ibkr_income_inputs import (
    AS_ACCOUNT_INFORMATION,
    AS_STATEMENT_SECTION,
    DIVIDENDS_HEADER,
    write_config,
    write_statement,
)
from tests._run_report_kwargs import os_report_kwargs
from tests._schedule_os_refusals import (
    malformed_type_slice_message,
    missing_kind_prefix_message,
    missing_usd_marker_message,
    unsupported_kind_message,
)
from tests._schedule_os_totals import total_rows

# ---------------------------------------------------------------------------
# Fixture builders
# ---------------------------------------------------------------------------


def _write_activity_statement(
    tmp_path: Path,
    *,
    account: str = "U1234567",
    statement_section: str = AS_STATEMENT_SECTION,
    dividend_rows: tuple[str, ...] = (),
) -> Path:
    """Build a minimal Activity Statement CSV in ``tmp_path``.

    Only includes Statement, Account Information, and (optionally) a
    Dividends section — the OS pipeline does not require FII /
    Trades / Open Positions. The statement section and the rows go through
    :func:`write_statement`'s ``str.format``, so they hold no braces.
    """
    parts = ["{header}"]
    if dividend_rows:
        parts.append(DIVIDENDS_HEADER + "\n")
        for row in dividend_rows:
            parts.append(row if row.endswith("\n") else row + "\n")
    return write_statement(
        tmp_path,
        account=account,
        header=statement_section + AS_ACCOUNT_INFORMATION,
        body="".join(parts),
    )


_FY_2025_26 = FinancialYear(start_year=2025)
_FY_2026_27 = FinancialYear(start_year=2026)


# ---------------------------------------------------------------------------
# Section A: FinancialYear + ScheduleOsRunReport invariants
# ---------------------------------------------------------------------------


def test_run_report_rows_written_must_be_non_negative() -> None:
    with pytest.raises(InvariantError):
        ScheduleOsRunReport(**os_report_kwargs(ordinary_dividend_rows_written=-1))


def test_run_report_indian_source_rows_must_be_non_negative() -> None:
    with pytest.raises(InvariantError):
        ScheduleOsRunReport(**os_report_kwargs(indian_source_dividend_rows_written=-1))


def test_run_report_total_gross_inr_must_be_non_negative() -> None:
    with pytest.raises(InvariantError):
        ScheduleOsRunReport(**os_report_kwargs(ordinary_dividend_total_inr_rounded=Decimal("-1")))


def test_run_report_indian_source_count_cannot_exceed_total() -> None:
    with pytest.raises(InvariantError):
        ScheduleOsRunReport(
            **os_report_kwargs(
                ordinary_dividend_rows_written=1,
                indian_source_dividend_rows_written=2,
            )
        )


def test_run_report_account_id_must_be_non_empty() -> None:
    with pytest.raises(InvariantError):
        ScheduleOsRunReport(**os_report_kwargs(account_id=""))


def test_run_report_fy_end_must_equal_fy_start_plus_1y_minus_1d() -> None:
    with pytest.raises(InvariantError):
        ScheduleOsRunReport(
            **os_report_kwargs(
                financial_year_start=date(2025, 4, 1),
                financial_year_end=date(2026, 3, 30),
            )
        )
    # Happy-path version constructs without raising.
    ScheduleOsRunReport(
        **os_report_kwargs(
            financial_year_start=date(2025, 4, 1),
            financial_year_end=date(2026, 3, 31),
        )
    )


def test_run_report_skipped_check_with_unmasked_account_rejected() -> None:
    """T1-r10 quality P3: mirror of RunReport's safety-net invariant.

    ScheduleOsRunReport with cross_checked=False AND a real (unmasked)
    account_id means the cross-check was bypassed without
    justification. The __post_init__ refuses to construct such a state
    so an internal-construction bug can't silently produce a report
    whose account_id wasn't validated against ``user.toml``.
    """
    with pytest.raises(InvariantError) as exc:
        ScheduleOsRunReport(
            **os_report_kwargs(
                account_id="U12345678",  # NOT masked
                account_id_cross_checked=False,
            )
        )
    assert "safety net was bypassed without justification" in exc.value.what
    # The InvariantError context must not embed the raw account ID
    # (mirror of the RunReport masking fix in round 9). And conversely
    # must keep the `account_id` key so a future regression that
    # drops the `account_id=` context entirely (while still passing
    # the raw-negative assertion) fails fast — preserving the
    # debuggability of the failure. The message prints neither (#1046).
    rendered = str(exc.value)
    assert "U12345678" not in rendered
    assert ("account_id", None) in exc.value.context
    assert "U***5678" not in rendered


def test_run_report_skipped_check_with_line_break_after_mask_rejected() -> None:
    """#854: ``"U***\\n"`` is not IBKR's mask, so it can't justify a
    skipped cross-check."""
    with pytest.raises(InvariantError) as exc:
        ScheduleOsRunReport(**os_report_kwargs(account_id="U***\n", account_id_cross_checked=False))
    assert "safety net was bypassed without justification" in exc.value.what


def test_run_report_skipped_check_with_masked_account_accepted() -> None:
    """The inverse of the above: a masked ID legitimately pairs with
    cross_checked=False (the user passed allow_masked_account=True
    after re-export with full ID was not possible)."""
    report = ScheduleOsRunReport(
        **os_report_kwargs(
            account_id="U**********",
            account_id_cross_checked=False,
        )
    )
    assert report.account_id_cross_checked is False
    assert report.account_id == "U**********"


# Issue #65: portal labels AY tracking on the RunReport.


@pytest.mark.parametrize(
    "report_cls",
    [
        ScheduleCgRunReport,
        ScheduleCgLtcgRunReport,
        RunReport,
        Form67RunReport,
        ScheduleFsiRunReport,
        ScheduleOsRunReport,
        ScheduleTrRunReport,
    ],
)
def test_run_report_portal_labels_ay_is_required_keyword(report_cls: type) -> None:
    """Issue #604: no report defaults the AY, so a constructor that
    forgets it fails instead of silently recording the current AY."""
    (ay_field,) = (
        f for f in dataclasses.fields(report_cls) if f.name == "portal_labels_assessment_year_start"
    )
    assert ay_field.default is dataclasses.MISSING
    assert ay_field.default_factory is dataclasses.MISSING
    assert ay_field.kw_only is True


def test_run_report_without_portal_labels_ay_raises_type_error() -> None:
    kwargs = os_report_kwargs()
    del kwargs["portal_labels_assessment_year_start"]
    with pytest.raises(TypeError, match="portal_labels_assessment_year_start"):
        ScheduleOsRunReport(**kwargs)


def test_run_report_portal_labels_ay_must_be_plausible() -> None:
    """A 4-digit AY start year < 1900 trips the
    _PLAUSIBLE_MIN_AY_START invariant (catches integer-typo bugs
    like passing the calendar-year-end-day count by mistake)."""
    with pytest.raises(InvariantError):
        ScheduleOsRunReport(**os_report_kwargs(portal_labels_assessment_year_start=42))


def test_run_report_can_record_forward_compat_fallback() -> None:
    """Pipeline populates the fallback flag from
    :func:`is_forward_compat_fallback`; CLI uses this to surface the
    advisory."""
    report = ScheduleOsRunReport(
        **os_report_kwargs(
            portal_labels_assessment_year_start=2027,
            portal_labels_used_forward_compat_fallback=True,
        )
    )
    assert report.portal_labels_assessment_year_start == 2027
    assert report.portal_labels_used_forward_compat_fallback is True


# ---------------------------------------------------------------------------
# Section B: run_schedule_os end-to-end
# ---------------------------------------------------------------------------


def test_happy_path_zero_dividends(tmp_path: Path) -> None:
    """An Activity Statement with no Dividends section produces a
    zero-total CSV and a report with rows_written=0."""
    as_csv = _write_activity_statement(tmp_path)
    cfg = write_config(tmp_path)
    out = tmp_path / "schedule-os.csv"
    report = run_schedule_os(
        input_path=as_csv,
        config_path=cfg,
        output_path=out,
        financial_year=_FY_2025_26,
    )
    assert report.ordinary_dividend_rows_written == 0
    assert report.indian_source_dividend_rows_written == 0
    assert report.ordinary_dividend_total_inr_rounded == Decimal("0")
    assert report.account_id == "U1234567"
    assert report.account_id_cross_checked is True
    assert report.input_format is IbkrCsvFormat.ACTIVITY_STATEMENT
    assert report.financial_year_start == date(2025, 4, 1)
    assert report.financial_year_end == date(2026, 3, 31)
    assert report.fx_policy_appropriate_for_ay is True
    assert out.exists()


# Rule 115 converts a 2025-09-16 payment at the SBI TT buying rate of the last
# day of the month before: 2025-08-31 is a Sunday, so it carries back to
# 2025-08-30's 87.70. A 2.50 dividend is 2.50 * 87.70 = 219.25, filed as ₹219
# (HALF_UP).
_FILED_DIVIDEND_INR = Decimal("219")


def test_happy_path_one_us_ordinary_dividend(tmp_path: Path) -> None:
    div = (
        "Dividends,Data,USD,16-09-2025,"
        "XQAQ(US99992A1097) Cash Dividend USD 0.25 per Share (Ordinary Dividend),"
        "2.50,,,,,,,,,,,"
    )
    as_csv = _write_activity_statement(tmp_path, dividend_rows=(div,))
    cfg = write_config(tmp_path)
    out = tmp_path / "schedule-os.csv"
    report = run_schedule_os(
        input_path=as_csv,
        config_path=cfg,
        output_path=out,
        financial_year=_FY_2025_26,
    )
    assert report.ordinary_dividend_rows_written == 1
    assert report.indian_source_dividend_rows_written == 0
    assert report.ordinary_dividend_total_inr_rounded == _FILED_DIVIDEND_INR
    assert out.exists()

    with out.open(encoding="utf-8", newline="") as fh:
        rows = list(csv.reader(fh))
    # Find the TOTAL row by scanning for the portal label.
    total_rows(rows)


def test_indian_source_dividend_routed_to_domestic_lines_for_1a_i(tmp_path: Path) -> None:
    """issue #158: an INE-prefixed (Indian-issuer direct-share)
    ordinary dividend now produces a first-class
    :class:`ScheduleOsDomesticDividendLine` routed to Schedule OS
    row 1a(i) (with Rule 115 FX conversion). Pre-#158 this was
    carved into ``excluded_indian_source_count`` /
    zero ordinary-dividend total.

    Note: INE-prefix ISINs are the IBKR-rare direct-Indian-share
    case (foreign broker with DMA to NSE/BSE). The common Indian-
    company ADR case carries a US-prefix ISIN and remains
    foreign-source; see issue #183 for the opt-in underlying-issuer
    attribution.
    """
    div = (
        "Dividends,Data,USD,16-09-2025,"
        "XQCD(IN99970W1077) Cash Dividend USD 0.25 per Share (Ordinary Dividend),"
        "2.50,,,,,,,,,,,"
    )
    as_csv = _write_activity_statement(tmp_path, dividend_rows=(div,))
    cfg = write_config(tmp_path)
    out = tmp_path / "schedule-os.csv"
    report = run_schedule_os(
        input_path=as_csv,
        config_path=cfg,
        output_path=out,
        financial_year=_FY_2025_26,
    )
    assert report.ordinary_dividend_rows_written == 1
    assert report.indian_source_dividend_rows_written == 1
    assert report.ordinary_dividend_total_inr_rounded == _FILED_DIVIDEND_INR


def test_non_ordinary_distribution_refuses(tmp_path: Path) -> None:
    div = (
        "Dividends,Data,USD,16-09-2025,"
        "XQAQ(US99992A1097) Return Of Capital USD 0.25 per Share,"
        "2.50,,,,,,,,,,,"
    )
    as_csv = _write_activity_statement(tmp_path, dividend_rows=(div,))
    cfg = write_config(tmp_path)
    out = tmp_path / "schedule-os.csv"
    # #1310: an in-FY Return Of Capital refuses, rather than going to the
    # excluded audit block, and nothing is written.
    with pytest.raises(NonOrdinaryDistributionError) as exc:
        run_schedule_os(
            input_path=as_csv,
            config_path=cfg,
            output_path=out,
            financial_year=_FY_2025_26,
        )
    assert exc.value.kind is DistributionKind.RETURN_OF_CAPITAL
    assert exc.value.source_row_index == 0
    assert not out.exists()


def test_flex_query_input_rejected(tmp_path: Path) -> None:
    """Schedule OS pipeline only accepts Activity Statement inputs."""
    flex = tmp_path / "flex.csv"
    flex.write_text(
        '"ClientAccountID","CurrencyPrimary","Name","AccountType","IBEntity"\n'
        '"U1234567","USD","Test Holder","Individual","IBLLC-US"\n',
        encoding="utf-8",
    )
    cfg = write_config(tmp_path)
    out = tmp_path / "out.csv"
    out.write_text("stale\n", encoding="utf-8")  # an earlier run's output
    with pytest.raises(ParseError) as exc:
        run_schedule_os(
            input_path=flex,
            config_path=cfg,
            output_path=out,
            financial_year=_FY_2025_26,
        )
    assert exc.type is ParseError
    refusal = exc.value
    assert (refusal.section, refusal.row_index, refusal.column, refusal.message) == (
        "<input format>",
        0,
        "<format detection>",
        "Schedule OS pipeline requires an IBKR Activity Statement input; got "
        "'flex-query'. Flex Query inputs are unsupported because the Schedule OS "
        "compute layer depends on the Dividends section that Flex doesn't currently "
        "expose (sister gap to #138).",
    )
    assert sorted(tmp_path.iterdir()) == sorted([flex, cfg])


@pytest.mark.parametrize(
    "period",
    [
        pytest.param("January 1, 2025 - December 31, 2025", id="calendar-year"),
        pytest.param("April 2, 2025 - March 31, 2026", id="starts-one-day-late"),
        pytest.param("April 1, 2025 - March 30, 2026", id="ends-one-day-early"),
    ],
)
def test_partial_fy_period_rejected(tmp_path: Path, period: str) -> None:
    """A period that doesn't contain the FY still refuses, now with the
    containment wording (#1284): it names Schedule OS, and not Schedule
    CG's lot-history advice."""
    statement = f"""\
Statement,Header,Field Name,Field Value,,,,,,,,,,,,,
Statement,Data,Period,"{period}",,,,,,,,,,,,,
"""
    as_csv = _write_activity_statement(tmp_path, statement_section=statement)
    cfg = write_config(tmp_path)
    with pytest.raises(ParseError) as exc:
        run_schedule_os(
            input_path=as_csv,
            config_path=cfg,
            output_path=tmp_path / "out.csv",
            financial_year=_FY_2025_26,
        )
    # T2 PR #163 review: lock the partial-FY error's report-name
    # attribution so the Schedule OS path's customised label
    # ("Schedule OS dividend reporting") propagates correctly through
    # the parametrised require_financial_year_contained_in_period validator.
    assert exc.value.message == (
        "the Activity Statement period does not fully contain the Indian financial "
        "year 2025-26 (2025-04-01 to 2026-03-31). Schedule OS dividend reporting "
        "requires the export to cover at least Apr 1, 2025 through Mar 31, 2026; "
        "re-export the Activity Statement with date range Apr 1, 2025 - Mar 31, 2026, "
        "or a wider one."
    )
    assert "Schedule CG" not in exc.value.message
    assert not (tmp_path / "out.csv").exists()


def test_account_id_mismatch_raises_config_error(tmp_path: Path) -> None:
    as_csv = _write_activity_statement(tmp_path, account="U7654321")
    cfg = write_config(tmp_path, account_number="U1234567")
    out = tmp_path / "out.csv"
    out.write_text("stale\n", encoding="utf-8")  # an earlier run's output
    with pytest.raises(ConfigError) as exc:
        run_schedule_os(
            input_path=as_csv,
            config_path=cfg,
            output_path=out,
            financial_year=_FY_2025_26,
        )
    assert exc.type is ConfigError
    # T1-r9 quality lane PR #159 P2, parallel to the schedule-fa mismatch
    # regression in tests/test_pipeline.py: the ConfigError is fixed text
    # that prints neither account ID, raw or masked (#1047).
    assert exc.value.field == "account.number"
    assert exc.value.message == ACCOUNT_MISMATCH_REFUSAL
    msg = str(exc.value).lower()
    for account in ("U7654321", "U1234567", "U***4321", "U***4567"):
        assert account.lower() not in msg, account
    assert sorted(tmp_path.iterdir()) == sorted([as_csv, cfg])


def test_masked_account_id_requires_opt_in(tmp_path: Path) -> None:
    as_csv = _write_activity_statement(tmp_path, account="U**********")
    cfg = write_config(tmp_path, account_number="U1234567")
    out = tmp_path / "out.csv"
    out.write_text("stale\n", encoding="utf-8")  # an earlier run's output
    with pytest.raises(ConfigError) as exc:
        run_schedule_os(
            input_path=as_csv,
            config_path=cfg,
            output_path=out,
            financial_year=_FY_2025_26,
        )
    assert exc.type is ConfigError
    # T1-r9 quality lane PR #159 P2: the masked-account ConfigError prints
    # neither the statement's mask nor user.toml's number, raw or masked
    # (#1047).
    assert exc.value.field == "account.number"
    assert exc.value.message == MASKED_ACCOUNT_REFUSAL
    msg = str(exc.value).lower()
    for account in ("U1234567", "U***4567", "U***"):
        assert account.lower() not in msg, account
    assert sorted(tmp_path.iterdir()) == sorted([as_csv, cfg])
    report = run_schedule_os(
        input_path=as_csv,
        config_path=cfg,
        output_path=out,
        financial_year=_FY_2025_26,
        allow_masked_account=True,
    )
    assert report.account_id_cross_checked is False
    assert report.account_id == "U**********"


def test_cross_ay_warning_flag_default_false_pre_ay_2027(tmp_path: Path) -> None:
    as_csv = _write_activity_statement(tmp_path)
    cfg = write_config(tmp_path)
    report = run_schedule_os(
        input_path=as_csv,
        config_path=cfg,
        output_path=tmp_path / "out.csv",
        financial_year=_FY_2025_26,
    )
    assert report.fx_policy_appropriate_for_ay is True

    # Setting suppress_cross_ay_warning is a no-op for pre-AY-2027 runs.
    report_suppressed = run_schedule_os(
        input_path=as_csv,
        config_path=cfg,
        output_path=tmp_path / "out2.csv",
        financial_year=_FY_2025_26,
        suppress_cross_ay_warning=True,
    )
    assert report_suppressed.fx_policy_appropriate_for_ay is True


def test_cross_ay_warning_flag_default_false_ay_2027_plus(tmp_path: Path) -> None:
    fy_2026_27_statement = """\
Statement,Header,Field Name,Field Value,,,,,,,,,,,,,
Statement,Data,Period,"April 1, 2026 - March 31, 2027",,,,,,,,,,,,,
"""
    as_csv = _write_activity_statement(tmp_path, statement_section=fy_2026_27_statement)
    cfg = write_config(tmp_path)
    report = run_schedule_os(
        input_path=as_csv,
        config_path=cfg,
        output_path=tmp_path / "out.csv",
        financial_year=_FY_2026_27,
    )
    assert report.fx_policy_appropriate_for_ay is False

    report_attested = run_schedule_os(
        input_path=as_csv,
        config_path=cfg,
        output_path=tmp_path / "out2.csv",
        financial_year=_FY_2026_27,
        suppress_cross_ay_warning=True,
    )
    assert report_attested.fx_policy_appropriate_for_ay is True


def test_unrecognised_distribution_type_raises(tmp_path: Path) -> None:
    """A distribution type matching neither allow nor reject list must fail
    loud, naming the row by its index and none of its cells (#1047)."""
    div = (
        "Dividends,Data,USD,16-09-2025,"
        "XQAQ(US99992A1097) Mystery Distribution USD 0.25 per Share,"
        "2.50,,,,,,,,,,,"
    )
    as_csv = _write_activity_statement(tmp_path, dividend_rows=(div,))
    cfg = write_config(tmp_path)
    out = tmp_path / "out.csv"
    out.write_text("stale\n", encoding="utf-8")  # an earlier run's output
    with pytest.raises(UnrecognisedDistributionTypeError) as exc:
        run_schedule_os(
            input_path=as_csv,
            config_path=cfg,
            output_path=out,
            financial_year=_FY_2025_26,
        )
    assert exc.type is UnrecognisedDistributionTypeError
    assert exc.value.violation == "unsupported_kind"
    assert exc.value.source_row_index == 0
    msg = str(exc.value)
    assert msg == unsupported_kind_message(0)
    for value in ("XQAQ", "US99992A1097", "Mystery", "0.25", "2.50", "2025-09-16", "16-09-2025"):
        assert value.lower() not in msg.lower(), value
    assert sorted(tmp_path.iterdir()) == sorted([as_csv, cfg])


# ---- #1285: the error keeps nothing from the row ----------------------------

_ROW_SYMBOL = "XQBA"
_ROW_ISIN = "US99995C1027"
_ROW_WORD = "Synthqkind"
_ROW_PER_SHARE = "7.31"
_ROW_DATE = "12-09-2025"
_ROW_PREFIX = f"{_ROW_SYMBOL}({_ROW_ISIN})"
_UNSUPPORTED = f"{_ROW_PREFIX} {_ROW_WORD} Payout USD {_ROW_PER_SHARE} per Share"
# The row's values, plus its payment date as ISO text and as a date's repr.
_ROW_MARKERS = (
    _ROW_SYMBOL,
    _ROW_ISIN,
    _ROW_WORD,
    _ROW_PER_SHARE,
    _ROW_DATE,
    "2025-09-12",
    "2025, 9, 12",
)

# violation → (the row's Description, its fixed message). No parser builds a
# ``missing_kind_prefix`` row, so that run clears the parsed row's kind and
# type-slice (:func:`_refusing_run`).
_UNCLASSIFIABLE_ROWS = {
    "unsupported_kind": (_UNSUPPORTED, unsupported_kind_message),
    "missing_usd_marker": (
        f"{_ROW_PREFIX} {_ROW_WORD} Payout {_ROW_PER_SHARE} per Share",
        missing_usd_marker_message,
    ),
    "malformed_type_slice": (
        f"{_ROW_PREFIX} Cash Dividend USD {_ROW_PER_SHARE} per Share ({_ROW_WORD} Payout)",
        malformed_type_slice_message,
    ),
    "missing_kind_prefix": (_UNSUPPORTED, missing_kind_prefix_message),
}


def _refusing_run(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, violation: str) -> Path:
    """Write the statement and config for ``violation``'s one Dividends row
    (gross 73.10) and return the statement."""
    description, _ = _UNCLASSIFIABLE_ROWS[violation]
    row = f"Dividends,Data,USD,{_ROW_DATE},{description},73.10,,,,,,,,,,,"
    as_csv = _write_activity_statement(tmp_path, dividend_rows=(row,))
    for marker in _ROW_MARKERS[:5]:
        assert marker in row, marker
    write_config(tmp_path)
    if violation == "missing_kind_prefix":
        load = pipeline_os._load_ibkr_activity_input

        def load_unclassified(**kwargs: Any) -> Any:
            loaded = load(**kwargs)
            dividends = [
                dataclasses.replace(div, kind=DistributionKind.UNRECOGNISED, type_slice="")
                for div in loaded.extract.dividends
            ]
            extract = dataclasses.replace(loaded.extract, dividends=dividends)
            return dataclasses.replace(loaded, extract=extract)

        monkeypatch.setattr(pipeline_os, "_load_ibkr_activity_input", load_unclassified)
    return as_csv


@pytest.mark.parametrize("violation", sorted(_UNCLASSIFIABLE_ROWS))
def test_unrecognised_distribution_type_error_keeps_no_row_content(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, violation: str
) -> None:
    """#1285: each violation refuses ``run_schedule_os`` with its fixed text,
    and the error keeps only the violation and the row index. No symbol,
    ISIN, kind word, per-share amount or payment date is in its fields,
    repr, args, cause or context."""
    as_csv = _refusing_run(tmp_path, monkeypatch, violation)
    out = tmp_path / "out.csv"
    out.write_text("stale\n", encoding="utf-8")  # an earlier run's output
    with pytest.raises(UnrecognisedDistributionTypeError) as exc:
        run_schedule_os(
            input_path=as_csv,
            config_path=tmp_path / "user.toml",
            output_path=out,
            financial_year=_FY_2025_26,
        )
    assert exc.type is UnrecognisedDistributionTypeError
    err = exc.value
    assert str(err) == _UNCLASSIFIABLE_ROWS[violation][1](0)
    assert err.args == ()
    assert_prints_no_marker(err, markers=_ROW_MARKERS)
    assert dataclasses.astuple(err) == (violation, 0)
    assert sorted(tmp_path.iterdir()) == sorted([as_csv, tmp_path / "user.toml"])


@pytest.mark.parametrize("violation", sorted(_UNCLASSIFIABLE_ROWS))
def test_schedule_os_refuses_an_unclassifiable_row_with_fixed_text(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    violation: str,
) -> None:
    """#1285 control: ``schedule-os`` still exits 4 with the fixed text for
    each violation and removes the stale output."""
    as_csv = _refusing_run(tmp_path, monkeypatch, violation)
    out = tmp_path / "schedule-os.csv"
    out.write_text("stale", encoding="utf-8")

    code = main(
        [
            "schedule-os",
            "--input",
            str(as_csv),
            "--config",
            str(tmp_path / "user.toml"),
            "--output",
            str(out),
            "--financial-year",
            "2025-26",
        ]
    )

    assert code == 4
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == f"error: {_UNCLASSIFIABLE_ROWS[violation][1](0)}\n"
    assert not out.exists()


@pytest.mark.parametrize(
    "type_slice",
    [
        "Cash Dividend USD nonsense",
        "Cash Dividend USD -5 per Share (New Adjustment)",
        "Cash Dividend USD 0.25 per Share (Return of Capital)",
        "Cash Dividend USD 0.25 per Share - US Tax",
        "Special Dividend USD  per Share",
        "Cash Dividend USD 0.25 USD 9 per Share",
        "Cash Dividend USD -5 per Share",
        "Cash Dividend USD +5 per Share",
        "Cash Dividend USD 0.25 per Share (Synthetic Holder Two)",
        "Cash Dividend USD 0.25 per Share Synthetic Marker Q",
    ],
)
def test_known_kind_outside_the_grammar_refuses_schedule_os(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], type_slice: str
) -> None:
    """#1096: a known kind label whose description doesn't match the
    supported grammar refuses ``schedule-os`` with exit 4, the fixed
    text for its row, no payload text and no stale output."""
    rows = (
        "Dividends,Data,USD,15-09-2025,"
        "XQAA(US99990A3095) Cash Dividend USD 0.25 per Share (Ordinary Dividend),"
        "2.50,,,,,,,,,,,",
        f"Dividends,Data,USD,16-09-2025,XQAQ(US99992A1097) {type_slice},2.50,,,,,,,,,,,",
    )
    as_csv = _write_activity_statement(tmp_path, dividend_rows=rows)
    cfg = write_config(tmp_path)
    out = tmp_path / "schedule-os.csv"
    out.write_text("stale", encoding="utf-8")

    code = main(
        [
            "schedule-os",
            "--input",
            str(as_csv),
            "--config",
            str(cfg),
            "--output",
            str(out),
            "--financial-year",
            "2025-26",
        ]
    )

    assert code == 4
    captured = capsys.readouterr()
    assert captured.err == f"error: {malformed_type_slice_message(1)}\n"
    assert captured.out == ""
    assert not out.exists()
    for value in ("Synthetic Holder Two", "Synthetic Marker Q", "0.25", "XQAQ", "16-09-2025"):
        if any(value in row for row in rows):
            assert value not in captured.err, value


_INTEREST_DESCRIPTION = "SYNTHDESC-2290"


def _schedule_os_with_interest_credits(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, amounts: list[str]
) -> tuple[int, Path]:
    """Run ``schedule-os`` over a stale output, with one interest credit per amount
    in place of the statement's and every credit converted at ₹80/$."""
    interest_credits = [
        InterestCredit(
            credit_date=date(2025, 6, 2),
            amount_gross=Decimal(amount),
            currency="USD",
            description=_INTEREST_DESCRIPTION,
            source_row_index=row,
        )
        for row, amount in enumerate(amounts)
    ]
    load = pipeline_os._load_ibkr_activity_input

    def load_with_credits(**kwargs: Any) -> Any:
        loaded = load(**kwargs)
        extract = dataclasses.replace(loaded.extract, interest_credits=interest_credits)
        return dataclasses.replace(loaded, extract=extract)

    monkeypatch.setattr(pipeline_os, "_load_ibkr_activity_input", load_with_credits)
    monkeypatch.setattr(
        "ibkr_fa.fx.context.Rule115OtherSourcesPolicy",
        lambda *, previous_year_end, **_: AuditedFxStub(
            Decimal("80"), specified_date=previous_year_end
        ),
    )
    as_csv = _write_activity_statement(tmp_path)
    cfg = write_config(tmp_path)
    out = tmp_path / "schedule-os.csv"
    out.write_text("stale", encoding="utf-8")

    code = main(
        [
            "schedule-os",
            "--input",
            str(as_csv),
            "--config",
            str(cfg),
            "--output",
            str(out),
            "--financial-year",
            "2025-26",
        ]
    )
    return code, out


def test_interest_total_keeps_every_digit_in_schedule_os(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#1312: six credits whose products are exact and whose 1b(ix) sum needs 29
    digits total exactly ₹1.50, so ``schedule-os`` files ₹2; main refused them
    with exit 4. All values are synthetic."""
    amounts = ["0.018749999999999999999999999975", *["5E-30"] * 5]

    code, out = _schedule_os_with_interest_credits(tmp_path, monkeypatch, amounts)

    assert code == 0
    with out.open(encoding="utf-8", newline="") as fh:
        rows = list(csv.reader(fh))
    _, _, interest = total_rows(rows)
    assert rows[interest][-1] == "2"


def test_interest_total_past_the_digit_cap_refuses_schedule_os(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """#1136, #1312: credits whose 1b(ix) sum needs 1,001 digits still refuse
    ``schedule-os`` with exit 4, the fixed text and no output. All values are
    synthetic."""
    code, out = _schedule_os_with_interest_credits(tmp_path, monkeypatch, ["1", "1E-1000"])

    assert code == 4
    captured = capsys.readouterr()
    for value in (_INTEREST_DESCRIPTION, "1E-1000", "80"):
        assert value not in captured.err, value
    assert captured.err == (
        f"error: InvariantError: Schedule OS 1b(ix) interest INR {INEXACT_SUM}\n"
    )
    assert captured.out == ""
    assert not out.exists()
