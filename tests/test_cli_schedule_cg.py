"""CLI tests for the ``schedule-cg`` subcommand."""

from __future__ import annotations

import csv
import re
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any, Final

import pytest
from openpyxl import load_workbook

from ibkr_fa.cli import (
    _render_schedule_cg_report,
    _render_schedule_cg_report_quiet,
    _render_schedule_cg_safety_warnings,
    main,
)
from ibkr_fa.compute.schedule_cg import ScheduleCgStcgComputeResult
from ibkr_fa.output import EXCLUDED_LTCG_DEFERRED_MARKER
from ibkr_fa.pipeline_cg import ScheduleCgRunReport
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
from tests._cg_lines import MARKERS, marked, stcg_line, stcg_result
from tests._exact_refusals import INEXACT_PRODUCT, INEXACT_SUM
from tests._fiscal_cli import install_fake
from tests._ibkr_account_refusals import ACCOUNT_MISMATCH_REFUSAL, cli_account_refusal
from tests._ibkr_income_inputs import (
    CONFIG_TEMPLATE,
    XQAQ_STCG_ROWS,
    round_trip_body,
    write_config,
    write_statement,
)
from tests._run_report_kwargs import cg_report_kwargs
from tests._trades_totals import with_trades_totals


def _make_report(**overrides: Any) -> ScheduleCgRunReport:
    """A report with two rows, a ₹12,345 total and ``/tmp/cg.csv``, then
    ``overrides``."""
    return ScheduleCgRunReport(
        **cg_report_kwargs(
            stcg_rows_written=2,
            total_realized_gain_inr_rounded=Decimal("12345"),
            output_path=Path("/tmp/cg.csv"),
        )
        | overrides
    )


def test_render_full_report_includes_total_and_path() -> None:
    report = _make_report()
    out = _render_schedule_cg_report(report)
    assert "Schedule CG STCG" in out
    assert "  TOTAL realized gain (rounded ₹): 12345" in out.splitlines()
    assert "/tmp/cg.csv" in out
    assert "2025-04-01" in out


def test_render_quiet_returns_only_total() -> None:
    report = _make_report(total_realized_gain_inr_rounded=Decimal("12345"))
    out = _render_schedule_cg_report_quiet(report)
    assert out == "12345"


def test_render_quiet_supports_negative_loss() -> None:
    report = _make_report(total_realized_gain_inr_rounded=Decimal("-7777"))
    out = _render_schedule_cg_report_quiet(report)
    assert out == "-7777"


def test_render_report_lists_ltcg_deferred_bucket_when_non_zero() -> None:
    report = _make_report(excluded_ltcg_deferred_count=3)
    out = _render_schedule_cg_report(report)
    assert "Excluded (LTCG)" in out
    assert "schedule-cg-ltcg" in out
    assert EXCLUDED_LTCG_DEFERRED_MARKER in out


def test_render_report_omits_buckets_when_zero() -> None:
    report = _make_report()
    out = _render_schedule_cg_report(report)
    assert "Excluded" not in out


def test_safety_warnings_lossy_total_triggers_loss_advisory() -> None:
    report = _make_report(total_realized_gain_inr_rounded=Decimal("-5000"))
    out = _render_schedule_cg_safety_warnings(report)
    assert "short-term capital LOSS" in out
    assert "Section 74" in out


def test_safety_warnings_zero_rows_triggers_zero_advisory() -> None:
    report = _make_report(stcg_rows_written=0, total_realized_gain_inr_rounded=Decimal("0"))
    out = _render_schedule_cg_safety_warnings(report)
    assert "No in-FY foreign-source short-term capital gains" in out


def test_safety_warnings_ltcg_deferred_triggers_schedule_cg_ltcg_advisory() -> None:
    """T2 PR #164 review: post-#30 (schedule-cg-ltcg shipped), the
    LTCG advisory now directs users at the new subcommand rather
    than the now-closed tracker issue."""
    report = _make_report(excluded_ltcg_deferred_count=2)
    out = _render_schedule_cg_safety_warnings(report)
    assert "long-term closure" in out
    assert "schedule-cg-ltcg" in out


def test_safety_warnings_indian_source_triggers_advisory() -> None:
    report = _make_report(excluded_indian_source_count=1)
    out = _render_schedule_cg_safety_warnings(report)
    assert "Indian-source closure" in out


def test_safety_warnings_out_of_fy_triggers_advisory() -> None:
    report = _make_report(excluded_out_of_fy_count=2)
    out = _render_schedule_cg_safety_warnings(report)
    assert "outside the requested FY window" in out


def test_safety_warnings_cross_ay_triggers_2027_advisory() -> None:
    report = _make_report(fx_policy_appropriate_for_ay=False)
    out = _render_schedule_cg_safety_warnings(report)
    assert "AY 2027-28" in out
    assert "issues/126" in out


def test_safety_warnings_contain_only_slab_vs_111a_advisory_when_clean() -> None:
    """A clean STCG run (no excluded buckets, no cross-AY mismatch,
    no masked account, no losses) still surfaces the slab-rate
    vs §111A advisory because it fires on every run with ≥ 1 STCG
    row (so the user cannot miss the §111A mis-classification
    risk). #62.
    """
    report = _make_report()
    out = _render_schedule_cg_safety_warnings(report)
    # Positive attribution: slab-rate under Chapter IV-D, not a
    # special-rate section.
    assert "slab" in out
    assert "NOT Section 111A" in out
    # Anti-regression: must NOT claim foreign STCG is taxed
    # under Section 111 (which is RPF-balances per the
    # Income-tax Act, 1961) — earlier draft drift fixed per
    # T5-r2 on PR #176.

    assert not re.search(r"\bunder Section 111\b(?!A)", out)
    # No other warnings should appear in a clean run.
    assert "Account ID cross-check was SKIPPED" not in out
    assert "AY 2027-28" not in out
    assert "Excluded" not in out
    assert "Net STCG TOTAL is negative" not in out


def test_safety_warnings_masked_account_triggers_advisory() -> None:
    report = _make_report(
        account_id="U**********",
        account_id_cross_checked=False,
    )
    out = _render_schedule_cg_safety_warnings(report)
    assert "Account ID cross-check was SKIPPED" in out


# ---------------------------------------------------------------------------
# End-to-end main() invocation
# ---------------------------------------------------------------------------


def _write_inputs(tmp_path: Path) -> tuple[Path, Path]:
    config = write_config(tmp_path)
    statement = write_statement(tmp_path, body=round_trip_body(XQAQ_STCG_ROWS), name="activity.csv")
    return statement, config


def test_main_runs_schedule_cg_end_to_end(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    statement, config = _write_inputs(tmp_path)
    out = tmp_path / "cg.csv"
    rc = main(
        [
            "schedule-cg",
            "--itr-form",
            "ITR-3",
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
    assert "Schedule CG STCG" in captured.out
    # #62 T4-r1: lock the wiring from advisory constant to stderr
    # output of main(). If the `print(warnings, file=sys.stderr)`
    # call in _run_schedule_cg ever regresses, this assertion
    # catches it (the unit tests against
    # _render_schedule_cg_safety_warnings only check the renderer,
    # not the dispatch path).
    assert "NOT Section 111A" in captured.err
    assert out.exists()
    with out.open(encoding="utf-8-sig", newline="") as fh:
        rows = list(csv.reader(fh))
    assert rows[0][0] == "Symbol"
    assert rows[1][0] == "XQAQ"
    assert rows[-1][0].endswith("(A6g)")


def test_main_quiet_prints_only_total(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    statement, config = _write_inputs(tmp_path)
    out = tmp_path / "cg.csv"
    rc = main(
        [
            "schedule-cg",
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
    # 2025-11-29's 88.95. Consideration 1800 * 88.95 = ₹160,110 less cost
    # 1500 * 88.95 = ₹133,425 files ₹26,685.
    assert captured.out == "26685\n"
    # #62: tax-safety advisory must survive --quiet (stderr only).
    assert "NOT Section 111A" in captured.err


def test_main_returns_4_on_domain_error(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    # Wrong account in config triggers ConfigError → exit 4
    statement, config = _write_inputs(tmp_path)
    wrong_config = tmp_path / "wrong.toml"
    wrong_config.write_text(CONFIG_TEMPLATE.format(account_number="U9999999"), encoding="utf-8")
    out = tmp_path / "cg.csv"
    out.write_text("stale\n", encoding="utf-8")  # an earlier run's output
    rc = main(
        [
            "schedule-cg",
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
    non-empty on the STCG report, the safety-warnings block surfaces
    the user-attested-identity warning."""
    report = _make_report(identity_supplied_from_fallback=frozenset({"Name", "Account"}))
    out = _render_schedule_cg_safety_warnings(report)
    assert "Identity field(s)" in out
    assert "'Account'" in out
    assert "'Name'" in out
    assert "user-attested" in out


# ---- --format wiring -------------------------------------------------------


def test_schedule_cg_format_xlsx_produces_valid_workbook(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """End-to-end CLI: ``schedule-cg --format xlsx`` produces a
    valid Excel workbook that ``openpyxl.load_workbook`` can read."""
    statement, config = _write_inputs(tmp_path)
    out = tmp_path / "cg.xlsx"
    rc = main(
        [
            "schedule-cg",
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
    assert rc == 0
    capsys.readouterr()
    assert out.exists()
    workbook = load_workbook(out)
    worksheet = workbook.active
    assert worksheet is not None
    assert worksheet.cell(row=1, column=1).value == "Symbol"
    # Last row's first cell is the portal-TOTAL label.
    rows = list(worksheet.iter_rows(values_only=True))
    assert rows[-1][0] is not None
    assert "STCG" in str(rows[-1][0]) or "1A1a" in str(rows[-1][0])


def test_schedule_cg_format_invalid_rejected(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """argparse must reject an unknown ``--format`` value (exit 2),
    NOT route to the pipeline domain-error code 4."""
    statement, config = _write_inputs(tmp_path)
    out = tmp_path / "cg.csv"
    with pytest.raises(SystemExit) as exc:
        main(
            [
                "schedule-cg",
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
                "not-a-format",
            ]
        )
    assert exc.value.code == 2
    assert "argument --format: invalid value" in capsys.readouterr().err.lower()


def test_schedule_cg_default_format_is_review_csv_threaded_to_pipeline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When ``--format`` is omitted, the dispatcher must thread
    ``format_id="review-csv"`` as a kwarg to ``run_schedule_cg``.
    Without this assertion a future drop of the kwarg from
    ``_run_schedule_cg`` could silently regress."""
    recorder: dict[str, object] = {}
    install_fake(monkeypatch, _make_report(), pipeline="run_schedule_cg", recorder=recorder)
    statement, config = _write_inputs(tmp_path)
    out = tmp_path / "cg.csv"
    rc = main(
        [
            "schedule-cg",
            "--itr-form",
            "ITR-3",
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
    assert recorder["format_id"] == "review-csv"
    assert recorder["itr_form"] == "ITR-3"


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
        "ibkr_fa.pipeline_cg.compute_schedule_cg_stcg_lines",
        lambda *_args, **_kwargs: stcg_result(proceeds, cost),
    )
    statement, config = _write_inputs(tmp_path)
    out = tmp_path / "cg.csv"
    rc = main(
        [
            "schedule-cg",
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

_STCG_FIXTURE = (
    Path(__file__).parent / "fixtures" / "e2e_activity_statement_schedule_cg_stcg_fy2025_26.csv"
)


def _write_two_stcg_closes(tmp_path: Path, *, first_pnl: str, second_pnl: str) -> tuple[Path, Path]:
    """The STCG fixture with its long-term lot bought in 2025, so both its
    closes are STCG lines, and each close's Realized P/L (USD) replaced."""
    text = _STCG_FIXTURE.read_text(encoding="utf-8")
    replacements = {
        '"2023-01-10, 11:00:00"': '"2025-01-10, 11:00:00"',
        ",-1500.00,300.00,": f",-1500.00,{first_pnl},",
        ",-1000.00,800.00,": f",-1000.00,{second_pnl},",
    }
    for old, new in replacements.items():
        assert text.count(old) == 1
        text = text.replace(old, new)
    statement = tmp_path / "activity.csv"
    statement.write_text(with_trades_totals(text), encoding="utf-8")
    return statement, write_config(tmp_path, account_number="U12345678")


def test_consideration_total_past_the_bound_refuses_before_any_writer(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two closes of about USD 0.7 trillion are about ₹6e13 each, in range, but
    the consideration total they file isn't."""
    monkeypatch.setattr(
        "ibkr_fa.pipeline_cg.output_binding",
        lambda *_args: pytest.fail("compute must refuse before any writer is built"),
    )
    statement, config = _write_two_stcg_closes(
        tmp_path, first_pnl="700000000000.00", second_pnl="700000000000.00"
    )
    inputs = sorted(tmp_path.iterdir())

    rc = run_cg("schedule-cg", statement, config, tmp_path / "cg.csv")

    assert rc == 4
    assert "InvariantError: Schedule CG STCG consideration must round" in capsys.readouterr().err
    assert sorted(tmp_path.iterdir()) == inputs


@pytest.mark.parametrize(("format_id", "suffix"), [("review-csv", ".csv"), ("xlsx", ".xlsx")])
def test_line_past_the_bound_files_while_the_totals_stay_in_range(
    tmp_path: Path, format_id: str, suffix: str
) -> None:
    """A line's proceeds and gain are review detail: a USD 1.4 trillion close
    (about ₹1.2e14) and a USD -0.7 trillion one file a consideration of about
    ₹6.2e13, which is in range."""
    statement, config = _write_two_stcg_closes(
        tmp_path, first_pnl="1400000000000.00", second_pnl="-700000000000.00"
    )
    out = tmp_path / f"cg{suffix}"

    rc = run_cg("schedule-cg", statement, config, out, "--format", format_id)

    assert rc == 0
    assert out.exists()


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
    lines = [marked(stcg_line("1", "0")), stcg_line("1E+10", "0"), stcg_line("1E-995", "0")]
    monkeypatch.setattr(
        "ibkr_fa.pipeline_cg.compute_schedule_cg_stcg_lines",
        lambda *_args, **_kwargs: ScheduleCgStcgComputeResult(lines, [], [], []),
    )
    statement, config = _write_inputs(tmp_path)
    out = tmp_path / f"cg{suffix}"
    if stale:
        out.write_text("stale", encoding="utf-8")

    rc = run_cg("schedule-cg", statement, config, out, "--format", format_id)

    assert rc == 4
    captured = capsys.readouterr()
    assert captured.err == (
        "error: InvariantError: Schedule CG STCG, input row 3, column proceeds_inr: "
        f"{INEXACT_SUM}\n"
    )
    assert captured.out == ""
    for marker in MARKERS:
        assert marker not in captured.err, marker
    assert not out.exists()


# ---- exact line conversion (#1184) --------------------------------------------

_STCG_COMPUTE: Final = "ibkr_fa.pipeline_cg.compute_schedule_cg_stcg_lines"


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
    raised = compute_on(monkeypatch, [marked_lot(PAST_THE_CAP)], _STCG_COMPUTE, rate="1")
    statement, config = _write_inputs(tmp_path)
    inputs = sorted(tmp_path.iterdir())
    out = tmp_path / f"cg{suffix}"
    if stale:
        out.write_text("stale", encoding="utf-8")

    rc = run_cg("schedule-cg", statement, config, out, "--format", format_id)

    assert rc == 4
    assert_private_refusal(capsys, raised, GAIN_PAST_THE_CAP.format(schedule="STCG"), PAST_THE_CAP)
    assert sorted(tmp_path.iterdir()) == inputs


def test_proceeds_conversion_past_the_exponent_limit_names_the_input_position(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """#1184: $9E+999999 at ₹86.20/$ overflows the exponent limit, so the
    proceeds conversion refuses; the position counts the out-of-FY and
    long-term lots STCG skips. Main printed only the CLI's generic Overflow
    message."""
    lots = [
        marked_lot("1", opened=date(2024, 5, 1), closed=date(2024, 6, 10)),
        marked_lot("1", opened=LONG_TERM_OPEN),
        marked_lot("9E+999999"),
    ]
    raised = compute_on(monkeypatch, lots, _STCG_COMPUTE, rate="86.20")
    statement, config = _write_inputs(tmp_path)
    out = tmp_path / "cg.csv"

    rc = run_cg("schedule-cg", statement, config, out)

    assert rc == 4
    assert_private_refusal(
        capsys,
        raised,
        f"InvariantError: Schedule CG STCG, input lot 3, column proceeds_inr: {INEXACT_PRODUCT}",
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
    compute_on(monkeypatch, [marked_lot(BELOW_HALF)], _STCG_COMPUTE, rate="1")
    statement, config = _write_inputs(tmp_path)
    out = tmp_path / f"cg{suffix}"

    rc = run_cg("schedule-cg", statement, config, out, "--quiet", "--format", format_id)

    assert rc == 0
    assert capsys.readouterr().out == "1\n"
    assert filed_cell(out) == "1"
