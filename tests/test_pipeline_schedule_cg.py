"""Tests for :func:`ibkr_fa.pipeline_cg.run_schedule_cg` and friends."""

from __future__ import annotations

import csv
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from ibkr_fa.compute.schedule_cg import ScheduleCgStcgComputeResult
from ibkr_fa.exceptions import ConfigError, InvariantError, ParseError
from ibkr_fa.output import UnknownOutputFormatError
from ibkr_fa.output.portal_labels import (
    PORTAL_FIELD_LABEL_CG_FOREIGN_STCG,
    PORTAL_FIELD_LABEL_CG_FOREIGN_STCG_BY_ITR_FORM,
)
from ibkr_fa.pipeline import FinancialYear
from ibkr_fa.pipeline_cg import ScheduleCgRunReport, run_schedule_cg
from tests._cg_lines import stcg_line, stcg_result
from tests._ibkr_account_refusals import ACCOUNT_MISMATCH_REFUSAL
from tests._ibkr_income_inputs import (
    AS_HEADER,
    XQAQ_STCG_ROWS,
    round_trip_body,
    write_config,
    write_statement,
)
from tests._run_report_kwargs import cg_report_kwargs

_FY_2025_26 = FinancialYear(start_year=2025)
# Rule 115 converts the XQAQ close of 2025-12-10 at the SBI TT buying rate of
# the last day of the month before: 2025-11-30 is a Sunday, so it carries back
# to 2025-11-29's 88.95. Consideration 1800 * 88.95 = ₹160,110 less cost
# 1500 * 88.95 = ₹133,425 files a gain of ₹26,685.
_FILED_GAIN = Decimal("26685")


def test_cg_run_report_rejects_negative_counters() -> None:
    for field in [
        "stcg_rows_written",
        "excluded_ltcg_deferred_count",
        "excluded_indian_source_count",
        "excluded_out_of_fy_count",
    ]:
        with pytest.raises(InvariantError):
            ScheduleCgRunReport(**cg_report_kwargs(**{field: -1}))


def test_cg_run_report_accepts_negative_total_gain() -> None:
    """Unlike Schedule OS, CG can have net losses (Section 74 carry-forward)."""
    report = ScheduleCgRunReport(
        **cg_report_kwargs(total_realized_gain_inr_rounded=Decimal("-50000")),
    )
    assert report.total_realized_gain_inr_rounded == Decimal("-50000")


def test_cg_run_report_account_id_must_be_non_empty() -> None:
    with pytest.raises(InvariantError):
        ScheduleCgRunReport(**cg_report_kwargs(account_id=""))


def test_cg_run_report_fy_end_must_be_correctly_derived() -> None:
    with pytest.raises(InvariantError):
        ScheduleCgRunReport(
            **cg_report_kwargs(
                financial_year_start=date(2025, 4, 1),
                financial_year_end=date(2026, 3, 30),
            ),
        )


# ``"U***\\n"`` is not IBKR's mask (#854).
@pytest.mark.parametrize("account_id", ["U12345678", "U***\n"])
def test_cg_run_report_unjustified_cross_check_skip_rejected(account_id: str) -> None:
    with pytest.raises(InvariantError):
        ScheduleCgRunReport(
            **cg_report_kwargs(
                account_id=account_id,
                account_id_cross_checked=False,
            ),
        )


# ---------------------------------------------------------------------------
# Section: run_schedule_cg integration with real Activity Statement
# ---------------------------------------------------------------------------

# A minimal Activity Statement with one round-trip closure within FY 2025-26:
# XQAQ's short-term round trip.
_AS_FIXTURE_ONE_STCG = round_trip_body(XQAQ_STCG_ROWS)

# #162: identical to _AS_FIXTURE_ONE_STCG except the Open Positions
# header is absent — exercises the closure-only AS path where IBKR
# omits the section entirely because no positions survived the FY.
_AS_FIXTURE_ONE_STCG_NO_OPEN_POSITIONS_SECTION = round_trip_body(
    XQAQ_STCG_ROWS, open_positions=False
)


def test_run_schedule_cg_emits_stcg_review_csv(tmp_path: Path) -> None:
    input_path = write_statement(tmp_path, body=_AS_FIXTURE_ONE_STCG)
    config_path = write_config(tmp_path)
    out = tmp_path / "cg.csv"

    report = run_schedule_cg(
        input_path=input_path,
        config_path=config_path,
        output_path=out,
        financial_year=_FY_2025_26,
        itr_form="ITR-3",
    )

    assert report.stcg_rows_written == 1
    assert report.excluded_ltcg_deferred_count == 0
    assert report.excluded_indian_source_count == 0
    assert report.total_realized_gain_inr_rounded == _FILED_GAIN
    assert report.account_id_cross_checked is True
    assert report.fx_policy_appropriate_for_ay is True
    assert out.exists()

    with out.open(encoding="utf-8-sig", newline="") as fh:
        rows = list(csv.reader(fh))
    assert rows[0][0] == "Symbol"
    assert rows[1][0] == "XQAQ"
    assert rows[-1][0] == PORTAL_FIELD_LABEL_CG_FOREIGN_STCG_BY_ITR_FORM["ITR-3"]


def test_run_schedule_cg_rejects_flex_query_input(tmp_path: Path) -> None:
    """T2 P2 fix: use a REAL Flex Query fixture so the Schedule-CG-
    specific Flex-rejection branch in run_schedule_cg actually fires
    (the earlier malformed-csv test triggered detect_ibkr_csv_format
    upstream of the rejection branch and accidentally substring-
    matched its 'Activity Statement' enum entry — the branch this
    test claims to cover was uncovered).
    """
    flex_fixture = Path(__file__).parent / "fixtures" / "e2e_year_end_2025.csv"
    assert flex_fixture.exists(), "shared Flex Query fixture missing — repo state is broken"
    config_path = write_config(tmp_path)
    out = tmp_path / "cg.csv"
    out.write_text("stale\n", encoding="utf-8")  # an earlier run's output
    with pytest.raises(ParseError) as exc:
        run_schedule_cg(
            input_path=flex_fixture,
            config_path=config_path,
            output_path=out,
            financial_year=_FY_2025_26,
        )
    assert exc.type is ParseError
    # The whole refusal of the CG-specific Flex-rejection branch (NOT the
    # upstream format-detection error, which would only contain the
    # 'Activity Statement' substring as part of its "Expected one of:"
    # enumeration).
    refusal = exc.value
    assert (refusal.section, refusal.row_index, refusal.column, refusal.message) == (
        "<input format>",
        0,
        "<format detection>",
        "Schedule CG pipeline requires an IBKR Activity Statement input; got "
        "'flex-query'. Flex Query inputs are unsupported because the Schedule CG "
        "compute layer depends on the Trades-section FIFO-matched ClosedLot stream "
        "that Flex doesn't currently produce in the same shape (see #104).",
    )
    # The input is a repository fixture, so only the config is left.
    assert list(tmp_path.iterdir()) == [config_path]


def test_run_schedule_cg_partial_fy_error_attributes_to_cg(tmp_path: Path) -> None:
    """T2 P2 fix + T3 P1 follow-up: when the CG path rejects a too-narrow
    Activity Statement export, the message must name 'Schedule CG'
    (not the validator's generic / OS-flavoured default).

    Build a statement whose Period ends inside the FY (covers only the
    first quarter), which trips the new
    require_financial_year_contained_in_period validator's
    "end before FY end" branch.
    """
    statement = write_statement(
        tmp_path,
        header=AS_HEADER.replace("April 1, 2025 - March 31, 2026", "April 1, 2025 - June 30, 2025"),
        body=_AS_FIXTURE_ONE_STCG,
        name="activity.csv",
    )
    config_path = write_config(tmp_path)
    out = tmp_path / "cg.csv"
    out.write_text("stale\n", encoding="utf-8")  # an earlier run's output
    with pytest.raises(ParseError) as exc:
        run_schedule_cg(
            input_path=statement,
            config_path=config_path,
            output_path=out,
            financial_year=_FY_2025_26,
        )
    assert exc.type is ParseError
    # Names Schedule CG, and not Schedule OS — the user didn't run that.
    refusal = exc.value
    assert (refusal.section, refusal.row_index, refusal.column, refusal.message) == (
        "Statement",
        0,
        "Period",
        "the Activity Statement period does not fully contain the Indian financial "
        "year 2025-26 (2025-04-01 to 2026-03-31). Schedule CG capital-gains reporting "
        "requires the export to cover at least Apr 1, 2025 through Mar 31, 2026 and "
        "typically a much wider window — re-export the Activity Statement from your "
        "account's first trade, or from Apr 1, 2025 if that is earlier (a multi-year "
        "export is normal for Schedule CG since the FIFO match needs the opening trade "
        "of every lot held during the FY, including lots sold in it).",
    )
    assert sorted(tmp_path.iterdir()) == sorted([statement, config_path])


def test_run_schedule_cg_accepts_wider_multi_year_export(tmp_path: Path) -> None:
    """T3 P1 fix: a multi-year Activity Statement (start before FY,
    end after FY) is the canonical CG export shape — it carries the
    historical opens needed for FIFO matching of pre-FY-acquired lots.
    The new require_financial_year_contained_in_period validator must
    accept it.
    """
    statement = write_statement(
        tmp_path,
        header=AS_HEADER.replace(
            "April 1, 2025 - March 31, 2026", "January 1, 2020 - December 31, 2026"
        ),
        body=_AS_FIXTURE_ONE_STCG,
        name="activity.csv",
    )
    config_path = write_config(tmp_path)
    out = tmp_path / "cg.csv"
    report = run_schedule_cg(
        input_path=statement,
        config_path=config_path,
        output_path=out,
        financial_year=_FY_2025_26,
    )
    # The fixture's STCG row's close_date (2025-12-10) is in-FY → 1 STCG.
    assert report.stcg_rows_written == 1
    assert out.exists()


def test_run_schedule_cg_account_id_mismatch_raises_config_error(tmp_path: Path) -> None:
    input_path = write_statement(tmp_path, account="U1234567", body=_AS_FIXTURE_ONE_STCG)
    config_path = write_config(tmp_path, account_number="U7777777")
    out = tmp_path / "cg.csv"
    out.write_text("stale\n", encoding="utf-8")  # an earlier run's output
    with pytest.raises(ConfigError) as exc:
        run_schedule_cg(
            input_path=input_path,
            config_path=config_path,
            output_path=out,
            financial_year=_FY_2025_26,
        )
    assert exc.type is ConfigError
    assert exc.value.field == "account.number"
    assert exc.value.message == ACCOUNT_MISMATCH_REFUSAL
    assert sorted(tmp_path.iterdir()) == sorted([input_path, config_path])


def test_run_schedule_cg_unknown_format_id_raises(tmp_path: Path) -> None:
    input_path = write_statement(tmp_path, body=_AS_FIXTURE_ONE_STCG)
    config_path = write_config(tmp_path)
    out = tmp_path / "cg.csv"
    out.write_text("stale\n", encoding="utf-8")  # an earlier run's output
    with pytest.raises(UnknownOutputFormatError) as exc:
        run_schedule_cg(
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


def test_run_schedule_cg_closure_only_no_open_positions_section_succeeds(
    tmp_path: Path,
) -> None:
    """#162: an Activity Statement where every position opened in the FY
    was also fully closed in the FY may legitimately omit the Open
    Positions section (IBKR doesn't emit empty sections). The CG
    pipeline must accept this case: closures are the entire point of
    Schedule CG, and the FIFO matcher emits ClosedLot records from the
    Trades section without needing the Open Positions section at all.

    Uses the same single-O+single-C fixture as the happy-path test but
    with the Open Positions header removed entirely
    (:data:`_AS_FIXTURE_ONE_STCG_NO_OPEN_POSITIONS_SECTION`), so the
    section is absent from the input file (not merely empty under a
    header row).
    """
    input_path = write_statement(tmp_path, body=_AS_FIXTURE_ONE_STCG_NO_OPEN_POSITIONS_SECTION)
    config_path = write_config(tmp_path)
    out = tmp_path / "cg.csv"

    report = run_schedule_cg(
        input_path=input_path,
        config_path=config_path,
        output_path=out,
        financial_year=_FY_2025_26,
    )

    assert report.stcg_rows_written == 1
    assert report.total_realized_gain_inr_rounded == _FILED_GAIN
    assert out.exists()

    with out.open(encoding="utf-8-sig", newline="") as fh:
        rows = list(csv.reader(fh))
    assert rows[0][0] == "Symbol"
    assert rows[1][0] == "XQAQ"
    assert rows[-1][0] == PORTAL_FIELD_LABEL_CG_FOREIGN_STCG


@pytest.mark.parametrize(
    ("proceeds", "cost", "filed_total"),
    [("1000.4", "1200.6", "-201"), ("100.4", "100.6", "-1")],
)
def test_run_schedule_cg_reports_the_component_total(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, proceeds: str, cost: str, filed_total: str
) -> None:
    """#615: the RunReport and the portal-label cell carry the Policy A total."""
    monkeypatch.setattr(
        "ibkr_fa.pipeline_cg.compute_schedule_cg_stcg_lines",
        lambda *_args, **_kwargs: stcg_result(proceeds, cost),
    )
    out = tmp_path / "cg.csv"

    report = run_schedule_cg(
        input_path=write_statement(tmp_path, body=_AS_FIXTURE_ONE_STCG),
        config_path=write_config(tmp_path),
        output_path=out,
        financial_year=_FY_2025_26,
    )

    assert report.total_realized_gain_inr_rounded == Decimal(filed_total)
    with out.open(encoding="utf-8-sig", newline="") as fh:
        assert list(csv.reader(fh))[-1][-1] == f"'{filed_total}"


def test_run_schedule_cg_files_the_exact_consideration(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#1166: six proceeds totalling exactly ₹1.50 file ₹2. The 28-digit
    context kept only the first, 1.499...998, and filed ₹1."""
    lines = [
        stcg_line(amount, "0") for amount in ("1.499999999999999999999999998", *("4E-28",) * 5)
    ]
    monkeypatch.setattr(
        "ibkr_fa.pipeline_cg.compute_schedule_cg_stcg_lines",
        lambda *_args, **_kwargs: ScheduleCgStcgComputeResult(lines, [], [], []),
    )
    out = tmp_path / "cg.csv"

    report = run_schedule_cg(
        input_path=write_statement(tmp_path, body=_AS_FIXTURE_ONE_STCG),
        config_path=write_config(tmp_path),
        output_path=out,
        financial_year=_FY_2025_26,
    )

    assert report.total_realized_gain_inr_rounded == 2
    with out.open(encoding="utf-8-sig", newline="") as fh:
        assert list(csv.reader(fh))[-1][-1] == "2"


def test_run_schedule_cg_refusal_leaves_no_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#615 owner decision 2: zero consideration with a cost is refused."""
    monkeypatch.setattr(
        "ibkr_fa.pipeline_cg.compute_schedule_cg_stcg_lines",
        lambda *_args, **_kwargs: stcg_result("0.40", "0.60"),
    )
    out = tmp_path / "cg.csv"

    with pytest.raises(InvariantError) as excinfo:
        run_schedule_cg(
            input_path=write_statement(tmp_path, body=_AS_FIXTURE_ONE_STCG),
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
