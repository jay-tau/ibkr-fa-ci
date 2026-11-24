"""CLI tests for the ``schedule-cg-ltcg`` subcommand."""

from __future__ import annotations

import csv
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from openpyxl import load_workbook

from ibkr_fa.cli import (
    _render_schedule_cg_ltcg_report,
    _render_schedule_cg_ltcg_report_quiet,
    _render_schedule_cg_ltcg_safety_warnings,
    main,
)
from ibkr_fa.compute.schedule_cg_ltcg import ScheduleCgLtcgComputeResult
from ibkr_fa.output.portal_labels import PORTAL_FIELD_LABEL_CG_FOREIGN_LTCG
from ibkr_fa.pipeline_cg_ltcg import ScheduleCgLtcgRunReport
from tests._cg_cli_runs import (
    BELOW_HALF,
    GAIN_PAST_THE_CAP,
    LONG_TERM_OPEN,
    PAST_THE_CAP,
    assert_private_refusal,
    compute_on,
    filed_cell,
    marked_lot,
    run_cg,
)
from tests._cg_lines import MARKERS, ltcg_line, ltcg_result, marked
from tests._exact_refusals import INEXACT_PRODUCT, INEXACT_SUM
from tests._fiscal_cli import install_fake
from tests._ibkr_account_refusals import ACCOUNT_MISMATCH_REFUSAL, cli_account_refusal
from tests._ibkr_income_inputs import (
    CONFIG_TEMPLATE,
    LTCG_AS_HEADER,
    XQAQ_LTCG_ROWS,
    round_trip_body,
    write_config,
    write_statement,
)
from tests._run_report_kwargs import ltcg_report_kwargs
from tests._trades_totals import with_trades_totals


def _make_report(**overrides: Any) -> ScheduleCgLtcgRunReport:
    """A report with two rows, a ₹12,345 total and ``/tmp/cg-ltcg.csv``, then
    ``overrides``."""
    return ScheduleCgLtcgRunReport(
        **ltcg_report_kwargs(
            ltcg_rows_written=2,
            total_realized_gain_inr_rounded=Decimal("12345"),
            output_path=Path("/tmp/cg-ltcg.csv"),
        )
        | overrides
    )


def test_render_full_report_includes_total_and_path() -> None:
    report = _make_report()
    out = _render_schedule_cg_ltcg_report(report)
    assert "Schedule CG LTCG" in out
    assert "  TOTAL realized gain (rounded ₹): 12345" in out.splitlines()
    assert "/tmp/cg-ltcg.csv" in out
    assert "2025-04-01" in out


def test_render_quiet_returns_only_total() -> None:
    report = _make_report(total_realized_gain_inr_rounded=Decimal("12345"))
    out = _render_schedule_cg_ltcg_report_quiet(report)
    assert out == "12345"


def test_render_quiet_supports_negative_loss() -> None:
    report = _make_report(total_realized_gain_inr_rounded=Decimal("-7777"))
    out = _render_schedule_cg_ltcg_report_quiet(report)
    assert out == "-7777"


def test_render_report_lists_stcg_bucket_when_non_zero() -> None:
    report = _make_report(excluded_stcg_count=3)
    out = _render_schedule_cg_ltcg_report(report)
    assert (
        "  Excluded (STCG, handled by schedule-cg): 3 — see _excluded_stcg: audit block in the CSV"
    ) in out.splitlines()


def test_render_report_lists_pre_cutover_bucket_when_non_zero() -> None:
    report = _make_report(excluded_pre_2024_07_23_count=5)
    out = _render_schedule_cg_ltcg_report(report)
    assert (
        "  Excluded (pre-2024-07-23 disposals, old regime needs indexation): 5 — "
        "see _excluded_pre_2024_07_23: audit block in the CSV"
    ) in out.splitlines()


def test_render_report_omits_buckets_when_zero() -> None:
    report = _make_report()
    out = _render_schedule_cg_ltcg_report(report)
    assert "Excluded" not in out


def test_safety_warnings_lossy_total_triggers_loss_advisory() -> None:
    report = _make_report(total_realized_gain_inr_rounded=Decimal("-5000"))
    out = _render_schedule_cg_ltcg_safety_warnings(report)
    assert "long-term capital LOSS" in out
    assert "Section 74" in out
    assert "long-term gains only" in out


def test_safety_warnings_zero_rows_triggers_zero_advisory() -> None:
    report = _make_report(ltcg_rows_written=0)
    out = _render_schedule_cg_ltcg_safety_warnings(report)
    assert "No in-FY foreign-source long-term capital gains" in out


def test_safety_warnings_stcg_bucket_triggers_29_advisory() -> None:
    report = _make_report(excluded_stcg_count=2)
    out = _render_schedule_cg_ltcg_safety_warnings(report)
    assert "schedule-cg" in out


def test_safety_warnings_pre_cutover_triggers_manual_indexation_advisory() -> None:
    report = _make_report(excluded_pre_2024_07_23_count=1)
    out = _render_schedule_cg_ltcg_safety_warnings(report)
    assert "indexation" in out
    assert "manually add them" in out


def test_safety_warnings_indian_source_triggers_advisory() -> None:
    report = _make_report(excluded_indian_source_count=1)
    out = _render_schedule_cg_ltcg_safety_warnings(report)
    assert "Indian-source closure" in out


def test_safety_warnings_out_of_fy_triggers_advisory() -> None:
    report = _make_report(excluded_out_of_fy_count=2)
    out = _render_schedule_cg_ltcg_safety_warnings(report)
    assert "outside the requested FY window" in out


def test_safety_warnings_cross_ay_triggers_2027_advisory() -> None:
    report = _make_report(fx_policy_appropriate_for_ay=False)
    out = _render_schedule_cg_ltcg_safety_warnings(report)
    assert "AY 2027-28" in out
    assert "issues/126" in out


def test_safety_warnings_masked_account_triggers_advisory() -> None:
    report = _make_report(account_id="U**********", account_id_cross_checked=False)
    out = _render_schedule_cg_ltcg_safety_warnings(report)
    assert "Account ID cross-check was SKIPPED" in out


def test_safety_warnings_contain_only_section_112_advisory_when_clean() -> None:
    """A clean LTCG run (no excluded buckets, no cross-AY mismatch,
    no masked account, no losses) still surfaces the Section 112
    vs 112A advisory because it fires on every run with ≥ 1 LTCG
    row (so the user cannot miss the §112A ₹1.25L exemption
    mis-classification risk). #62.
    """
    report = _make_report()
    out = _render_schedule_cg_ltcg_safety_warnings(report)
    assert "Section 112" in out
    assert "NOT Section 112A" in out
    assert "₹1.25 lakh exemption" in out
    # No other warnings should appear in a clean run.
    assert "Account ID cross-check was SKIPPED" not in out
    assert "AY 2027-28" not in out
    assert "Excluded" not in out
    assert "Net LTCG TOTAL is negative" not in out


# ---------------------------------------------------------------------------
# End-to-end main() invocation
# ---------------------------------------------------------------------------


def _write_inputs(tmp_path: Path) -> tuple[Path, Path]:
    config = write_config(tmp_path)
    statement = write_statement(
        tmp_path, header=LTCG_AS_HEADER, body=round_trip_body(XQAQ_LTCG_ROWS), name="activity.csv"
    )
    return statement, config


def test_main_runs_schedule_cg_ltcg_end_to_end(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    statement, config = _write_inputs(tmp_path)
    out = tmp_path / "cg-ltcg.csv"
    rc = main(
        [
            "schedule-cg-ltcg",
            "--itr-form",
            "ITR-2",
            "--input",
            str(statement),
            "--config",
            str(config),
            "--output",
            str(out),
            "--financial-year",
            "2025-26",
        ]
    )
    assert rc == 0
    captured = capsys.readouterr()
    assert "Schedule CG LTCG" in captured.out
    # #62 T4-r1: lock the advisory→stderr wiring via main() dispatch.
    assert "NOT Section 112A" in captured.err
    assert out.exists()
    with out.open(encoding="utf-8-sig", newline="") as fh:
        rows = list(csv.reader(fh))
    assert rows[0][0] == "Symbol"
    assert rows[1][0] == "XQAQ"


def test_main_quiet_prints_only_total(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    statement, config = _write_inputs(tmp_path)
    out = tmp_path / "cg-ltcg.csv"
    rc = main(
        [
            "schedule-cg-ltcg",
            "--itr-form",
            "ITR-2",
            "--quiet",
            "--input",
            str(statement),
            "--config",
            str(config),
            "--output",
            str(out),
            "--financial-year",
            "2025-26",
        ]
    )
    assert rc == 0
    captured = capsys.readouterr()
    # --quiet stdout is exactly the rounded total, on one line. Rule 115
    # converts the 2025-12-10 close at the SBI TT buying rate of the last day
    # of the month before: 2025-11-30 is a Sunday, so it carries back to
    # 2025-11-29's 88.95. Consideration 2200 * 88.95 = ₹195,690 less cost
    # 1500 * 88.95 = ₹133,425 files ₹62,265.
    assert captured.out == "62265\n"
    # #62: tax-safety advisory must survive --quiet (stderr only).
    assert "NOT Section 112A" in captured.err


def test_main_returns_4_on_domain_error(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    statement, config = _write_inputs(tmp_path)
    wrong_config = tmp_path / "wrong.toml"
    wrong_config.write_text(CONFIG_TEMPLATE.format(account_number="U9999999"), encoding="utf-8")
    out = tmp_path / "cg-ltcg.csv"
    out.write_text("stale\n", encoding="utf-8")  # an earlier run's output
    rc = main(
        [
            "schedule-cg-ltcg",
            "--itr-form",
            "ITR-2",
            "--input",
            str(statement),
            "--config",
            str(wrong_config),
            "--output",
            str(out),
            "--financial-year",
            "2025-26",
        ]
    )
    assert rc == 4
    captured = capsys.readouterr()
    assert captured.err == cli_account_refusal(ACCOUNT_MISMATCH_REFUSAL)
    assert captured.out == ""
    assert sorted(tmp_path.iterdir()) == sorted([statement, config, wrong_config])


def test_safety_warnings_includes_identity_fallback_block_for_anonymised_input() -> None:
    """#141 / T2 PR #166 P3: when identity_supplied_from_fallback is
    non-empty on the LTCG report, the safety-warnings block surfaces
    the user-attested-identity warning."""
    report = _make_report(identity_supplied_from_fallback=frozenset({"Name", "Account"}))
    out = _render_schedule_cg_ltcg_safety_warnings(report)
    assert "Identity field(s)" in out
    assert "'Account'" in out
    assert "'Name'" in out
    assert "user-attested" in out


def test_schedule_cg_ltcg_format_xlsx_produces_valid_workbook(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """End-to-end: ``--format xlsx`` writes a load-able workbook
    whose last row matches the portal-label contract."""
    statement, config = _write_inputs(tmp_path)
    out = tmp_path / "cg-ltcg.xlsx"
    rc = main(
        [
            "schedule-cg-ltcg",
            "--itr-form",
            "ITR-2",
            "--input",
            str(statement),
            "--config",
            str(config),
            "--output",
            str(out),
            "--financial-year",
            "2025-26",
            "--format",
            "xlsx",
        ]
    )
    capsys.readouterr()
    assert rc == 0
    assert out.exists()
    workbook = load_workbook(out)
    worksheet = workbook.active
    assert worksheet is not None
    last_row = worksheet.max_row
    assert worksheet.cell(row=last_row, column=1).value == PORTAL_FIELD_LABEL_CG_FOREIGN_LTCG


def test_schedule_cg_ltcg_format_invalid_rejected(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Argparse validates ``--format`` against
    ``available_formats(Subcommand.CG_LTCG)`` — unknown choices exit with
    code 2 (usage error) before pipeline dispatch."""
    statement, config = _write_inputs(tmp_path)
    out = tmp_path / "cg-ltcg.xlsx"
    with pytest.raises(SystemExit) as excinfo:
        main(
            [
                "schedule-cg-ltcg",
                "--itr-form",
                "ITR-2",
                "--input",
                str(statement),
                "--config",
                str(config),
                "--output",
                str(out),
                "--financial-year",
                "2025-26",
                "--format",
                "tex",
            ]
        )
    assert excinfo.value.code == 2
    captured = capsys.readouterr()
    assert "argument --format: invalid value" in captured.err


def test_schedule_cg_ltcg_default_format_is_review_csv_threaded_to_pipeline(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Without ``--format``, the dispatcher MUST thread
    ``format_id="review-csv"`` as a keyword argument to
    :func:`run_schedule_cg_ltcg` (defends against a future regression
    where the dispatcher silently drops the format_id kwarg and
    relies on the pipeline default)."""
    statement, config = _write_inputs(tmp_path)
    out = tmp_path / "cg-ltcg.csv"
    recorder: dict[str, object] = {}
    install_fake(
        monkeypatch,
        _make_report(output_path=out),
        pipeline="run_schedule_cg_ltcg",
        recorder=recorder,
    )
    rc = main(
        [
            "schedule-cg-ltcg",
            "--itr-form",
            "ITR-2",
            "--input",
            str(statement),
            "--config",
            str(config),
            "--output",
            str(out),
            "--financial-year",
            "2025-26",
        ]
    )
    capsys.readouterr()
    assert rc == 0
    assert recorder["format_id"] == "review-csv"


@pytest.mark.parametrize(
    ("proceeds", "cost", "total"), [("1000.4", "1200.6", "-201"), ("100.4", "100.6", "-1")]
)
def test_main_quiet_total_matches_the_written_portal_cell(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    proceeds: str,
    cost: str,
    total: str,
) -> None:
    """#615: --quiet prints the same Policy A total the file files."""
    monkeypatch.setattr(
        "ibkr_fa.pipeline_cg_ltcg.compute_schedule_cg_ltcg_lines",
        lambda *_args, **_kwargs: ltcg_result(proceeds, cost),
    )
    statement, config = _write_inputs(tmp_path)
    out = tmp_path / "cg-ltcg.csv"
    rc = main(
        [
            "schedule-cg-ltcg",
            "--itr-form",
            "ITR-2",
            "--quiet",
            "--input",
            str(statement),
            "--config",
            str(config),
            "--output",
            str(out),
            "--financial-year",
            "2025-26",
        ]
    )

    assert rc == 0
    assert capsys.readouterr().out.strip() == total
    with out.open(encoding="utf-8-sig", newline="") as fh:
        assert list(csv.reader(fh))[-1][-1] == f"'{total}"


# ---- filed amounts past the CBDT bound (#830) -------------------------------

_LTCG_FIXTURE = (
    Path(__file__).parent / "fixtures" / "e2e_activity_statement_schedule_cg_ltcg_fy2025_26.csv"
)


def test_consideration_total_past_the_bound_refuses_before_any_writer(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """With its short-term lot bought in 2023, the fixture's two closes are LTCG
    lines of about USD 0.7 trillion (₹6e13) each, in range, but the
    consideration total they file isn't."""
    monkeypatch.setattr(
        "ibkr_fa.pipeline_cg_ltcg.output_binding",
        lambda *_args: pytest.fail("compute must refuse before any writer is built"),
    )
    text = _LTCG_FIXTURE.read_text(encoding="utf-8")
    replacements = {
        '"2025-05-15, 10:30:00"': '"2023-05-15, 10:30:00"',
        ",-1500.00,700.00,": ",-1500.00,700000000000.00,",
        ",-1500.00,300.00,": ",-1500.00,700000000000.00,",
    }
    for old, new in replacements.items():
        assert text.count(old) == 1
        text = text.replace(old, new)
    statement = tmp_path / "activity.csv"
    statement.write_text(with_trades_totals(text), encoding="utf-8")
    config = write_config(tmp_path, account_number="U12345678")
    inputs = sorted(tmp_path.iterdir())

    rc = main(
        [
            "schedule-cg-ltcg",
            "--itr-form",
            "ITR-2",
            "--input",
            str(statement),
            "--config",
            str(config),
            "--output",
            str(tmp_path / "cg-ltcg.csv"),
            "--financial-year",
            "2025-26",
        ]
    )

    assert rc == 4
    assert "InvariantError: Schedule CG LTCG consideration must round" in capsys.readouterr().err
    assert sorted(tmp_path.iterdir()) == inputs


# ---- exact sums (#1166) -------------------------------------------------------


@pytest.mark.parametrize(("format_id", "suffix"), [("review-csv", ".csv"), ("xlsx", ".xlsx")])
@pytest.mark.parametrize("stale", [False, True], ids=["fresh", "stale"])
def test_consideration_sum_past_the_exact_cap_refuses_and_clears_the_output(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    format_id: str,
    suffix: str,
    stale: bool,
) -> None:
    """#1166 (owner decision 2026-11-10: exact, never round): proceeds of
    ₹1E10 and ₹1E-995 need more digits than an exact sum may hold. The first
    line carries every privacy marker; none is printed."""
    lines = [marked(ltcg_line("1", "0")), ltcg_line("1E+10", "0"), ltcg_line("1E-995", "0")]
    monkeypatch.setattr(
        "ibkr_fa.pipeline_cg_ltcg.compute_schedule_cg_ltcg_lines",
        lambda *_args, **_kwargs: ScheduleCgLtcgComputeResult(lines, [], [], [], []),
    )
    statement, config = _write_inputs(tmp_path)
    out = tmp_path / f"cg-ltcg{suffix}"
    if stale:
        out.write_text("stale", encoding="utf-8")

    rc = main(
        [
            "schedule-cg-ltcg",
            "--itr-form",
            "ITR-2",
            "--input",
            str(statement),
            "--config",
            str(config),
            "--output",
            str(out),
            "--format",
            format_id,
            "--financial-year",
            "2025-26",
        ]
    )

    assert rc == 4
    captured = capsys.readouterr()
    assert captured.err == (
        "error: InvariantError: Schedule CG LTCG, input row 3, column proceeds_inr: "
        f"{INEXACT_SUM}\n"
    )
    assert captured.out == ""
    for marker in MARKERS:
        assert marker not in captured.err, marker
    assert not out.exists()


# ---- exact line conversion (#1184) --------------------------------------------

_LTCG_COMPUTE = "ibkr_fa.pipeline_cg_ltcg.compute_schedule_cg_ltcg_lines"


@pytest.mark.parametrize(("format_id", "suffix"), [("review-csv", ".csv"), ("xlsx", ".xlsx")])
@pytest.mark.parametrize("stale", [False, True], ids=["fresh", "stale"])
def test_gain_past_the_exact_cap_refuses_at_its_line_and_clears_the_output(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    format_id: str,
    suffix: str,
    stale: bool,
) -> None:
    """#1184: the line's gain refuses at its exact subtraction, earlier than
    main's refusal at the checked consideration total (``input row 1``)."""
    lots = [marked_lot(PAST_THE_CAP, opened=LONG_TERM_OPEN)]
    raised = compute_on(monkeypatch, lots, _LTCG_COMPUTE, rate="1")
    statement, config = _write_inputs(tmp_path)
    inputs = sorted(tmp_path.iterdir())
    out = tmp_path / f"cg-ltcg{suffix}"
    if stale:
        out.write_text("stale", encoding="utf-8")

    rc = run_cg("schedule-cg-ltcg", statement, config, out, "--format", format_id)

    assert rc == 4
    assert_private_refusal(capsys, raised, GAIN_PAST_THE_CAP.format(schedule="LTCG"), PAST_THE_CAP)
    assert sorted(tmp_path.iterdir()) == inputs


def test_cost_conversion_past_the_exponent_limit_names_the_input_position(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """#1184: a $9E+999999 cost at ₹86.20/$ overflows the exponent limit, so
    the cost conversion refuses; the position counts the short-term lot LTCG
    skips. Main printed only the CLI's generic Overflow message."""
    lots = [marked_lot("1"), marked_lot("1", cost="9E+999999", opened=LONG_TERM_OPEN)]
    raised = compute_on(monkeypatch, lots, _LTCG_COMPUTE, rate="86.20")
    statement, config = _write_inputs(tmp_path)
    out = tmp_path / "cg-ltcg.csv"

    rc = run_cg("schedule-cg-ltcg", statement, config, out)

    assert rc == 4
    assert_private_refusal(
        capsys,
        raised,
        f"InvariantError: Schedule CG LTCG, input lot 2, column cost_basis_inr: {INEXACT_PRODUCT}",
        "9E+999999",
    )
    assert not out.exists()


@pytest.mark.parametrize(("format_id", "suffix"), [("review-csv", ".csv"), ("xlsx", ".xlsx")])
def test_just_below_half_proceeds_file_one_rupee(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    format_id: str,
    suffix: str,
) -> None:
    """#1184: $1.4999999999999999999999999999 at ₹1/$ against no cost files
    ₹1; main rounded the product to ₹1.500000000000000000000000000 and filed ₹2."""
    lots = [marked_lot(BELOW_HALF, opened=LONG_TERM_OPEN)]
    compute_on(monkeypatch, lots, _LTCG_COMPUTE, rate="1")
    statement, config = _write_inputs(tmp_path)
    out = tmp_path / f"cg-ltcg{suffix}"

    rc = run_cg("schedule-cg-ltcg", statement, config, out, "--quiet", "--format", format_id)

    assert rc == 0
    assert capsys.readouterr().out == "1\n"
    assert filed_cell(out) == "1"
