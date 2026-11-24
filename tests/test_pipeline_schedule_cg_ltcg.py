"""Tests for :func:`ibkr_fa.pipeline_cg_ltcg.run_schedule_cg_ltcg` and friends."""

from __future__ import annotations

import csv
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from ibkr_fa.exceptions import ConfigError, InvariantError, ParseError
from ibkr_fa.output import UnknownOutputFormatError
from ibkr_fa.output.portal_labels import PORTAL_FIELD_LABEL_CG_FOREIGN_LTCG
from ibkr_fa.pipeline import FinancialYear
from ibkr_fa.pipeline_cg_ltcg import ScheduleCgLtcgRunReport, run_schedule_cg_ltcg
from tests._cg_lines import ltcg_result
from tests._ibkr_account_refusals import ACCOUNT_MISMATCH_REFUSAL
from tests._ibkr_income_inputs import (
    LTCG_AS_HEADER,
    XQAQ_LTCG_ROWS,
    round_trip_body,
    write_config,
    write_statement,
)
from tests._run_report_kwargs import ltcg_report_kwargs

_FY_2025_26 = FinancialYear(start_year=2025)
# Rule 115 converts the XQAQ close of 2025-12-10 at the SBI TT buying rate of
# the last day of the month before: 2025-11-30 is a Sunday, so it carries back
# to 2025-11-29's 88.95. Consideration 2200 * 88.95 = ₹195,690 less cost
# 1500 * 88.95 = ₹133,425 files a gain of ₹62,265.
_FILED_GAIN = Decimal("62265")


def test_ltcg_run_report_rejects_negative_counters() -> None:
    for field in [
        "ltcg_rows_written",
        "excluded_stcg_count",
        "excluded_pre_2024_07_23_count",
        "excluded_indian_source_count",
        "excluded_out_of_fy_count",
    ]:
        with pytest.raises(InvariantError):
            ScheduleCgLtcgRunReport(**ltcg_report_kwargs(**{field: -1}))


def test_ltcg_run_report_accepts_negative_total_gain() -> None:
    """LTCG losses are valid (Section 74 8-AY carry-forward applies to
    long-term losses too; they can only set off against long-term gains)."""
    report = ScheduleCgLtcgRunReport(
        **ltcg_report_kwargs(total_realized_gain_inr_rounded=Decimal("-50000")),
    )
    assert report.total_realized_gain_inr_rounded == Decimal("-50000")


def test_ltcg_run_report_account_id_must_be_non_empty() -> None:
    with pytest.raises(InvariantError):
        ScheduleCgLtcgRunReport(**ltcg_report_kwargs(account_id=""))


def test_ltcg_run_report_fy_end_must_be_correctly_derived() -> None:
    with pytest.raises(InvariantError):
        ScheduleCgLtcgRunReport(
            **ltcg_report_kwargs(
                financial_year_start=date(2025, 4, 1),
                financial_year_end=date(2026, 3, 30),
            ),
        )


# ``"U***\\n"`` is not IBKR's mask (#854).
@pytest.mark.parametrize("account_id", ["U12345678", "U***\n"])
def test_ltcg_run_report_unjustified_cross_check_skip_rejected(account_id: str) -> None:
    with pytest.raises(InvariantError):
        ScheduleCgLtcgRunReport(
            **ltcg_report_kwargs(
                account_id=account_id,
                account_id_cross_checked=False,
            ),
        )


# ---------------------------------------------------------------------------
# End-to-end run_schedule_cg_ltcg integration tests
# ---------------------------------------------------------------------------

# XQAQ's long-term round trip, under LTCG_AS_HEADER's multi-year period.
_AS_FIXTURE_ONE_LTCG = round_trip_body(XQAQ_LTCG_ROWS)

# #162: identical to _AS_FIXTURE_ONE_LTCG except the Open Positions
# header is absent — exercises the closure-only AS path where IBKR
# omits the section entirely because no positions survived the FY.
_AS_FIXTURE_ONE_LTCG_NO_OPEN_POSITIONS_SECTION = round_trip_body(
    XQAQ_LTCG_ROWS, open_positions=False
)


def test_run_schedule_cg_ltcg_emits_review_csv(tmp_path: Path) -> None:
    input_path = write_statement(tmp_path, header=LTCG_AS_HEADER, body=_AS_FIXTURE_ONE_LTCG)
    config_path = write_config(tmp_path)
    out = tmp_path / "cg-ltcg.csv"
    report = run_schedule_cg_ltcg(
        input_path=input_path,
        config_path=config_path,
        output_path=out,
        financial_year=_FY_2025_26,
    )
    assert report.ltcg_rows_written == 1
    assert report.excluded_stcg_count == 0
    assert report.excluded_pre_2024_07_23_count == 0
    assert report.total_realized_gain_inr_rounded == _FILED_GAIN
    assert report.account_id_cross_checked is True
    assert out.exists()
    with out.open(encoding="utf-8-sig", newline="") as fh:
        rows = list(csv.reader(fh))
    assert rows[0][0] == "Symbol"
    assert rows[1][0] == "XQAQ"
    assert rows[-1][0] == PORTAL_FIELD_LABEL_CG_FOREIGN_LTCG


def test_run_schedule_cg_ltcg_rejects_flex_query_input(tmp_path: Path) -> None:
    flex_fixture = Path(__file__).parent / "fixtures" / "e2e_year_end_2025.csv"
    assert flex_fixture.exists()
    config_path = write_config(tmp_path)
    out = tmp_path / "cg-ltcg.csv"
    out.write_text("stale\n", encoding="utf-8")  # an earlier run's output
    with pytest.raises(ParseError) as exc:
        run_schedule_cg_ltcg(
            input_path=flex_fixture,
            config_path=config_path,
            output_path=out,
            financial_year=_FY_2025_26,
        )
    assert exc.type is ParseError
    refusal = exc.value
    assert (refusal.section, refusal.row_index, refusal.column, refusal.message) == (
        "<input format>",
        0,
        "<format detection>",
        "Schedule CG LTCG pipeline requires an IBKR Activity Statement input; got "
        "'flex-query'. Flex Query inputs are unsupported because the CG pathway "
        "depends on the Trades-section FIFO-matched ClosedLot stream that Flex "
        "doesn't currently produce in the same shape (see #104).",
    )
    # The input is a repository fixture, so only the config is left.
    assert list(tmp_path.iterdir()) == [config_path]


def test_run_schedule_cg_ltcg_partial_fy_error_attributes_to_cg(tmp_path: Path) -> None:
    statement = write_statement(
        tmp_path,
        header=LTCG_AS_HEADER.replace(
            "January 1, 2023 - March 31, 2026", "April 1, 2025 - June 30, 2025"
        ),
        body=_AS_FIXTURE_ONE_LTCG,
        name="activity.csv",
    )
    config_path = write_config(tmp_path)
    out = tmp_path / "cg-ltcg.csv"
    out.write_text("stale\n", encoding="utf-8")  # an earlier run's output
    with pytest.raises(ParseError) as exc:
        run_schedule_cg_ltcg(
            input_path=statement,
            config_path=config_path,
            output_path=out,
            financial_year=_FY_2025_26,
        )
    assert exc.type is ParseError
    refusal = exc.value
    assert (refusal.section, refusal.row_index, refusal.column, refusal.message) == (
        "Statement",
        0,
        "Period",
        "the Activity Statement period does not fully contain the Indian financial "
        "year 2025-26 (2025-04-01 to 2026-03-31). Schedule CG capital-gains reporting "
        "(LTCG) requires the export to cover at least Apr 1, 2025 through Mar 31, 2026 "
        "and typically a much wider window — re-export the Activity Statement from "
        "your account's first trade, or from Apr 1, 2025 if that is earlier (a "
        "multi-year export is normal for Schedule CG since the FIFO match needs the "
        "opening trade of every lot held during the FY, including lots sold in it).",
    )
    assert sorted(tmp_path.iterdir()) == sorted([statement, config_path])


def test_run_schedule_cg_ltcg_account_id_mismatch_raises_config_error(tmp_path: Path) -> None:
    input_path = write_statement(
        tmp_path, account="U1234567", header=LTCG_AS_HEADER, body=_AS_FIXTURE_ONE_LTCG
    )
    config_path = write_config(tmp_path, account_number="U7777777")
    out = tmp_path / "cg-ltcg.csv"
    out.write_text("stale\n", encoding="utf-8")  # an earlier run's output
    with pytest.raises(ConfigError) as exc:
        run_schedule_cg_ltcg(
            input_path=input_path,
            config_path=config_path,
            output_path=out,
            financial_year=_FY_2025_26,
        )
    assert exc.type is ConfigError
    assert exc.value.field == "account.number"
    assert exc.value.message == ACCOUNT_MISMATCH_REFUSAL
    assert sorted(tmp_path.iterdir()) == sorted([input_path, config_path])


def test_run_schedule_cg_ltcg_unknown_format_id_raises(tmp_path: Path) -> None:
    input_path = write_statement(tmp_path, header=LTCG_AS_HEADER, body=_AS_FIXTURE_ONE_LTCG)
    config_path = write_config(tmp_path)
    out = tmp_path / "cg-ltcg.csv"
    out.write_text("stale\n", encoding="utf-8")  # an earlier run's output
    with pytest.raises(UnknownOutputFormatError) as exc:
        run_schedule_cg_ltcg(
            input_path=input_path,
            config_path=config_path,
            output_path=out,
            financial_year=_FY_2025_26,
            format_id="nope-csv",
        )
    assert exc.type is UnknownOutputFormatError
    assert str(exc.value) == (
        "unknown output format 'nope-csv'; available formats: 'review-csv', 'xlsx'"
    )
    assert sorted(tmp_path.iterdir()) == sorted([input_path, config_path])


def test_run_schedule_cg_ltcg_closure_only_no_open_positions_section_succeeds(
    tmp_path: Path,
) -> None:
    """#162 LTCG-symmetry counterpart to
    :func:`tests.test_pipeline_schedule_cg.test_run_schedule_cg_closure_only_no_open_positions_section_succeeds`.

    The LTCG pipeline shares :func:`_dispatch_activity_statement` with
    the STCG pipeline, so the closure-only behaviour is structurally
    identical; this test asserts the symmetry holds (a future
    LTCG-only divergence in the dispatcher would break this case
    without breaking the STCG test).

    Uses :data:`_AS_FIXTURE_ONE_LTCG_NO_OPEN_POSITIONS_SECTION` — the
    same > 24-month XQAQ O+C pair as the happy-path fixture but with
    the Open Positions header absent from the file entirely.
    """
    input_path = write_statement(
        tmp_path, header=LTCG_AS_HEADER, body=_AS_FIXTURE_ONE_LTCG_NO_OPEN_POSITIONS_SECTION
    )
    config_path = write_config(tmp_path)
    out = tmp_path / "cg-ltcg.csv"

    report = run_schedule_cg_ltcg(
        input_path=input_path,
        config_path=config_path,
        output_path=out,
        financial_year=_FY_2025_26,
    )

    assert report.ltcg_rows_written == 1
    assert report.excluded_stcg_count == 0
    assert report.excluded_pre_2024_07_23_count == 0
    assert report.total_realized_gain_inr_rounded == _FILED_GAIN
    assert out.exists()

    with out.open(encoding="utf-8-sig", newline="") as fh:
        rows = list(csv.reader(fh))
    assert rows[0][0] == "Symbol"
    assert rows[1][0] == "XQAQ"
    assert rows[-1][0] == PORTAL_FIELD_LABEL_CG_FOREIGN_LTCG


def test_run_schedule_cg_ltcg_reports_the_component_total(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#615: the RunReport and the portal-label cell carry the Policy A total."""
    monkeypatch.setattr(
        "ibkr_fa.pipeline_cg_ltcg.compute_schedule_cg_ltcg_lines",
        lambda *_args, **_kwargs: ltcg_result("1000.4", "1200.6"),
    )
    out = tmp_path / "cg-ltcg.csv"

    report = run_schedule_cg_ltcg(
        input_path=write_statement(tmp_path, header=LTCG_AS_HEADER, body=_AS_FIXTURE_ONE_LTCG),
        config_path=write_config(tmp_path),
        output_path=out,
        financial_year=_FY_2025_26,
    )

    assert report.total_realized_gain_inr_rounded == Decimal("-201")
    with out.open(encoding="utf-8-sig", newline="") as fh:
        assert list(csv.reader(fh))[-1][-1] == "'-201"


def test_run_schedule_cg_ltcg_refusal_leaves_no_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#615 owner decision 2: zero consideration with a cost is refused."""
    monkeypatch.setattr(
        "ibkr_fa.pipeline_cg_ltcg.compute_schedule_cg_ltcg_lines",
        lambda *_args, **_kwargs: ltcg_result("0.40", "0.60"),
    )
    out = tmp_path / "cg-ltcg.csv"

    with pytest.raises(InvariantError) as excinfo:
        run_schedule_cg_ltcg(
            input_path=write_statement(tmp_path, header=LTCG_AS_HEADER, body=_AS_FIXTURE_ONE_LTCG),
            config_path=write_config(tmp_path),
            output_path=out,
            financial_year=_FY_2025_26,
        )

    assert dict(excinfo.value.context) == {
        "schedule": None,
        "consideration_inr": None,
        "cost_of_acquisition_inr": None,
    }
    assert not out.exists()
