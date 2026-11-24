"""Tests for the ``schedule-fsi`` CLI subcommand."""

from __future__ import annotations

import csv
import hashlib
from datetime import date
from decimal import Decimal
from functools import partial
from pathlib import Path
from typing import Any

import pytest

from ibkr_fa import cli
from ibkr_fa.cli import main
from ibkr_fa.compute.schedule_cg import ScheduleCgStcgComputeResult
from ibkr_fa.config import ScheduleOsIndianAdrRoutingMode
from ibkr_fa.models.schedule_fsi_row import HeadOfIncome
from ibkr_fa.output._cleanup import RunOutputs
from ibkr_fa.parser.format_detect import IbkrCsvFormat
from ibkr_fa.pipeline_fsi import FsiPerCountryRow, ScheduleFsiRunReport
from tests._cg_cli_runs import (
    GAIN_PAST_THE_CAP,
    LONG_TERM_OPEN,
    PAST_THE_CAP,
    SHORT_TERM_OPEN,
    assert_private_refusal,
    compute_on,
    marked_lot,
)
from tests._cg_lines import MARKERS, marked, stcg_line
from tests._exact_refusals import INEXACT_SUM
from tests._fiscal_cli import install_fake, write_cli_paths
from tests._ftc_warnings import MISSING_INDIAN_TAX_WARNING
from tests._ibkr_user_toml import US_FOREIGN_TIN_TOML
from tests._portal_parity import fix_xlsx_clock
from tests._trades_totals import with_trades_totals


def _make_fake_report(**overrides: Any) -> ScheduleFsiRunReport:
    defaults: dict[str, Any] = {
        "run_outputs": RunOutputs((), ()),
        "rows_written": 2,
        "per_country_rows": (
            FsiPerCountryRow(
                cbdt_country_code="2",
                country_name_portal="UNITED STATES OF AMERICA",
                head_of_income=HeadOfIncome.CAPITAL_GAINS,
                income_inr_rounded=Decimal("12345"),
                foreign_tax_inr_rounded=Decimal("0"),
                event_count=1,
            ),
            FsiPerCountryRow(
                cbdt_country_code="2",
                country_name_portal="UNITED STATES OF AMERICA",
                head_of_income=HeadOfIncome.OTHER_SOURCES,
                income_inr_rounded=Decimal("6789"),
                foreign_tax_inr_rounded=Decimal("1234"),
                event_count=3,
            ),
        ),
        "total_income_inr_rounded": Decimal("19134"),
        "total_foreign_tax_inr_rounded": Decimal("1234"),
        "os_dividend_rows_consumed": 2,
        "os_interest_rows_consumed": 1,
        "cg_stcg_rows_consumed": 1,
        "cg_ltcg_rows_consumed": 0,
        "wht_rows_in_fy": 1,
        "excluded_cg_indian_source_count": 0,
        "excluded_cg_out_of_fy_count": 0,
        "excluded_ltcg_pre_cutover_count": 0,
        "excluded_ltcg_indian_source_count": 0,
        "excluded_ltcg_out_of_fy_count": 0,
        "interest_credits_out_of_fy_count": 0,
        "adr_routing_mode": ScheduleOsIndianAdrRoutingMode.DEPOSITARY,
        "output_path": Path("/tmp/out.csv"),
        "account_id": "U1234567",
        "account_id_cross_checked": True,
        "input_format": IbkrCsvFormat.ACTIVITY_STATEMENT,
        "financial_year_start": date(2025, 4, 1),
        "financial_year_end": date(2026, 3, 31),
        "fx_policy_appropriate_for_ay": True,
        "wht_fx_policy_appropriate_for_ay": True,
        "portal_labels_assessment_year_start": 2026,
    }
    defaults.update(overrides)
    return ScheduleFsiRunReport(**defaults)


@pytest.fixture
def cli_paths(tmp_path: Path) -> tuple[Path, Path, Path]:
    return write_cli_paths(tmp_path, "placeholder")


_install_fake = partial(install_fake, pipeline="run_schedule_fsi")


# ---------------------------------------------------------------------------
# Argparse: required arguments
# ---------------------------------------------------------------------------


def test_schedule_fsi_requires_input_config_output_fy(
    capsys: pytest.CaptureFixture[str],
) -> None:
    with pytest.raises(SystemExit) as exc:
        main(["schedule-fsi"])
    assert exc.value.code == 2  # argparse usage error
    err = capsys.readouterr().err
    assert "--input" in err or "required" in err.lower()


def test_schedule_fsi_format_choices_are_review_csv_and_xlsx(
    capsys: pytest.CaptureFixture[str],
) -> None:
    with pytest.raises(SystemExit):
        main(["schedule-fsi", "--help"])
    out = capsys.readouterr().out
    assert "review-csv" in out
    assert "xlsx" in out


def test_schedule_fsi_missing_indian_tax_warning_is_in_safety_output() -> None:
    report = _make_fake_report(has_unresolved_indian_tax_payable=True)
    warning = cli._render_schedule_fsi_safety_warnings(report)
    assert "INDIAN TAX CALCULATION REQUIRED" in warning
    assert "average Indian tax rate" in warning
    assert "Rule 128(5)(i)" in warning


def test_schedule_fsi_rejects_unknown_format(
    capsys: pytest.CaptureFixture[str],
    cli_paths: tuple[Path, Path, Path],
) -> None:
    inp, cfg, out = cli_paths
    with pytest.raises(SystemExit) as exc:
        main(
            [
                "schedule-fsi",
                "--input",
                str(inp),
                "--config",
                str(cfg),
                "--output",
                str(out),
                "--format",
                "json",  # not registered for FSI
                "--financial-year",
                "2025-26",
            ],
        )
    assert exc.value.code == 2
    err = capsys.readouterr().err
    assert "argument --format: invalid value" in err


# ---------------------------------------------------------------------------
# Verbose output
# ---------------------------------------------------------------------------


def test_schedule_fsi_verbose_output_includes_per_country_breakdown(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    cli_paths: tuple[Path, Path, Path],
) -> None:
    inp, cfg, out = cli_paths
    _install_fake(monkeypatch, _make_fake_report())

    rc = main(
        [
            "schedule-fsi",
            "--input",
            str(inp),
            "--config",
            str(cfg),
            "--output",
            str(out),
            "--financial-year",
            "2025-26",
        ],
    )
    assert rc == 0
    stdout = capsys.readouterr().out
    assert "Wrote Schedule FSI" in stdout
    assert "UNITED STATES OF AMERICA" in stdout
    assert "CBDT 2" in stdout
    assert "Capital Gains" in stdout
    assert "Other Sources" in stdout
    # Per-source consumption breakdown:
    assert "1 STCG row(s)" in stdout
    assert "1 in-FY WHT row(s)" in stdout


# ---------------------------------------------------------------------------
# --quiet output: CSV-with-header
# ---------------------------------------------------------------------------


def test_schedule_fsi_quiet_emits_csv_header_and_rows(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    cli_paths: tuple[Path, Path, Path],
) -> None:
    inp, cfg, out = cli_paths
    _install_fake(monkeypatch, _make_fake_report())

    rc = main(
        [
            "schedule-fsi",
            "--input",
            str(inp),
            "--config",
            str(cfg),
            "--output",
            str(out),
            "--financial-year",
            "2025-26",
            "--quiet",
        ],
    )
    assert rc == 0
    stdout = capsys.readouterr().out
    # CSV header. We use lineterminator='\n' so no \r residue.
    lines = stdout.strip().split("\n")
    assert lines[0] == "cbdt_country_code,head_of_income,income_inr_rounded,foreign_tax_inr_rounded"
    # 2 data rows.
    assert len(lines) == 3
    # csv.writer uses QUOTE_MINIMAL — spaces don't trigger quoting.
    # 2 data rows in canonical comma-separated form.
    assert lines[1] == "2,Capital Gains,12345,0"
    assert lines[2] == "2,Other Sources,6789,1234"


def test_schedule_fsi_quiet_zero_rows_emits_header_only(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    cli_paths: tuple[Path, Path, Path],
) -> None:
    inp, cfg, out = cli_paths
    empty_report = _make_fake_report(rows_written=0, per_country_rows=())
    _install_fake(monkeypatch, empty_report)

    rc = main(
        [
            "schedule-fsi",
            "--input",
            str(inp),
            "--config",
            str(cfg),
            "--output",
            str(out),
            "--financial-year",
            "2025-26",
            "--quiet",
        ],
    )
    assert rc == 0
    stdout = capsys.readouterr().out
    assert (
        stdout.strip()
        == "cbdt_country_code,head_of_income,income_inr_rounded,foreign_tax_inr_rounded"
    )


# ---------------------------------------------------------------------------
# Exit codes
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Safety warnings
# ---------------------------------------------------------------------------


def test_schedule_fsi_emits_cross_ay_warning_when_appropriate_flag_false(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    cli_paths: tuple[Path, Path, Path],
) -> None:
    inp, cfg, out = cli_paths
    report = _make_fake_report(
        fx_policy_appropriate_for_ay=False,
        wht_fx_policy_appropriate_for_ay=False,
    )
    _install_fake(monkeypatch, report)

    rc = main(
        [
            "schedule-fsi",
            "--input",
            str(inp),
            "--config",
            str(cfg),
            "--output",
            str(out),
            "--financial-year",
            "2025-26",
        ],
    )
    assert rc == 0
    err = capsys.readouterr().err
    assert "AY 2027-28" in err
    assert "Rule 206" in err
    assert "Rule 76" in err


def test_schedule_fsi_emits_underlying_issuer_advisory_when_routing_set(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    cli_paths: tuple[Path, Path, Path],
) -> None:
    inp, cfg, out = cli_paths
    report = _make_fake_report(
        adr_routing_mode=ScheduleOsIndianAdrRoutingMode.UNDERLYING_ISSUER,
        wht_rows_in_fy=0,  # only legitimate combo per the pipeline guard
    )
    _install_fake(monkeypatch, report)

    rc = main(
        [
            "schedule-fsi",
            "--input",
            str(inp),
            "--config",
            str(cfg),
            "--output",
            str(out),
            "--financial-year",
            "2025-26",
        ],
    )
    assert rc == 0
    err = capsys.readouterr().err
    assert "UNDERLYING_ISSUER" in err


def test_schedule_fsi_depositary_advisory_does_not_claim_wht_combo_is_blocked(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    cli_paths: tuple[Path, Path, Path],
) -> None:
    """The default-mode (DEPOSITARY) advisory previously claimed
    that opting in to UNDERLYING_ISSUER + WHT was "BLOCKED in v1".
    PR #251 (issue #225) removed that block via symmetric WHT
    skipping; the advisory must therefore NOT still claim the
    combination is blocked. (issue #261 audit / PR #262 fix.)
    """
    inp, cfg, out = cli_paths
    # Default DEPOSITARY mode (the legacy-advisory branch).
    report = _make_fake_report(adr_routing_mode=ScheduleOsIndianAdrRoutingMode.DEPOSITARY)
    _install_fake(monkeypatch, report)
    rc = main(
        [
            "schedule-fsi",
            "--input",
            str(inp),
            "--config",
            str(cfg),
            "--output",
            str(out),
            "--financial-year",
            "2025-26",
        ],
    )
    assert rc == 0
    err = capsys.readouterr().err
    # The DEPOSITARY advisory IS emitted (its informational header).
    assert "asset-as-held" in err
    assert "'depositary' mode" in err
    # But the stale "BLOCKED in v1" claim MUST be gone.
    assert "BLOCKED" not in err
    # The refreshed wording explicitly references #225's symmetric exclusion.
    assert "symmetrically excludes the mapped Indian-ADR WHT" in err
    assert "issue #225" in err


def test_schedule_fsi_passes_args_to_pipeline(
    monkeypatch: pytest.MonkeyPatch,
    cli_paths: tuple[Path, Path, Path],
) -> None:
    """The CLI thread should forward every flag to run_schedule_fsi."""
    inp, cfg, out = cli_paths
    recorder: dict[str, Any] = {}
    _install_fake(monkeypatch, _make_fake_report(), recorder=recorder)

    main(
        [
            "schedule-fsi",
            "--input",
            str(inp),
            "--config",
            str(cfg),
            "--output",
            str(out),
            "--format",
            "xlsx",
            "--financial-year",
            "2025-26",
            "--allow-masked-account",
            "--suppress-cross-ay-warning",
        ],
    )
    assert recorder["input_path"] == inp
    assert recorder["config_path"] == cfg
    assert recorder["output_path"] == out
    assert recorder["format_id"] == "xlsx"
    assert recorder["allow_masked_account"] is True
    assert recorder["suppress_cross_ay_warning"] is True
    assert recorder["accept_anonymised_statement"] is False


def test_schedule_fsi_emits_foreign_tin_nudge_when_missing(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    cli_paths: tuple[Path, Path, Path],
) -> None:
    """When a relief-claiming country has no configured foreign TIN, the
    FSI safety block must nudge the user (the TIN is mandatory for
    §90/90A/91 relief) and exit 3 rather than silently file a blank
    mandatory cell (#924)."""
    inp, cfg, out = cli_paths
    report = _make_fake_report(has_foreign_tax_without_taxpayer_id=True)
    _install_fake(monkeypatch, report)

    rc = main(
        [
            "schedule-fsi",
            "--input",
            str(inp),
            "--config",
            str(cfg),
            "--output",
            str(out),
            "--financial-year",
            "2025-26",
        ],
    )
    assert rc == 3
    err = capsys.readouterr().err
    assert "Taxpayer Identification Number is BLANK" in err
    assert "[account.foreign_tins]" in err
    assert "90 / 90A / 91" in err


def test_schedule_fsi_omits_foreign_tin_nudge_when_present(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    cli_paths: tuple[Path, Path, Path],
) -> None:
    """Symmetric contract: when every relief country has a TIN (the
    factory default has_foreign_tax_without_taxpayer_id=False), the
    nudge MUST be absent."""
    inp, cfg, out = cli_paths
    _install_fake(monkeypatch, _make_fake_report())

    rc = main(
        [
            "schedule-fsi",
            "--input",
            str(inp),
            "--config",
            str(cfg),
            "--output",
            str(out),
            "--financial-year",
            "2025-26",
        ],
    )
    assert rc == 0
    err = capsys.readouterr().err
    assert "Taxpayer Identification Number is BLANK" not in err


# ---- filed amounts past the CBDT bound (#830) -------------------------------

_CG_FIXTURE = (
    Path(__file__).parent / "fixtures" / "e2e_activity_statement_schedule_cg_stcg_fy2025_26.csv"
)
_USER_TOML = """\
[account]
number = "U12345678"
holder_name = "Sample Holder"
opening_date = 2020-01-01
status = "BeneficialOwner"

[fx]
max_carry_forward_days = 4
"""


def test_capital_gains_income_past_the_bound_refuses_before_any_writer(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The fixture's STCG and LTCG closes each gain about USD 0.7 trillion
    (₹6e13), in range, but the US capital-gains row's income isn't."""
    monkeypatch.setattr(
        "ibkr_fa.pipeline_fsi.output_binding",
        lambda *_args: pytest.fail("compute must refuse before any writer is built"),
    )
    text = _CG_FIXTURE.read_text(encoding="utf-8")
    stcg_close, ltcg_close = ",-1500.00,300.00,", ",-1000.00,800.00,"
    assert text.count(stcg_close) == text.count(ltcg_close) == 1
    statement = tmp_path / "activity.csv"
    statement.write_text(
        with_trades_totals(
            text.replace(stcg_close, ",-1500.00,700000000000.00,").replace(
                ltcg_close, ",-1000.00,700000000000.00,"
            )
        ),
        encoding="utf-8",
    )
    config = tmp_path / "user.toml"
    config.write_text(_USER_TOML, encoding="utf-8")
    inputs = sorted(tmp_path.iterdir())

    rc = main(
        [
            "schedule-fsi",
            "--input",
            str(statement),
            "--config",
            str(config),
            "--output",
            str(tmp_path / "schedule-fsi.csv"),
            "--financial-year",
            "2025-26",
        ]
    )

    assert rc == 4
    # The model invariant names its key but not the income it refused (#1046).
    captured = capsys.readouterr()
    assert (captured.out, captured.err) == (
        "",
        "error: InvariantError: ScheduleFsiRow.income_inr must round to a whole-rupee amount "
        "within ±99,999,999,999,999 (the CBDT 14-digit bound) (context keys: value)\n",
    )
    assert sorted(tmp_path.iterdir()) == inputs


# ---- blank foreign TIN on a relief row (#924) ------------------------------

# One USD 2.60 US dividend with USD 0.39 US tax withheld, and a round trip in one
# US stock.
_WHT_FIXTURE = (
    Path(__file__).parent / "fixtures" / "e2e_activity_statement_schedule_tr_with_wht_fy2025_26.csv"
)
_US_DIVIDEND_TAX_PAYABLE = """
[form67]
[[form67.tax_payable_overrides]]
country = "US"
source = "DIVIDEND"
tax_payable_normal_inr = "9999"
"""


def _schedule_fsi_on_the_wht_fixture(
    tmp_path: Path, config_text: str, format_id: str
) -> tuple[int, Path]:
    """Run ``schedule-fsi`` on the WHT fixture for FY 2025-26 in ``format_id``
    with ``config_text`` as ``user.toml``; return the exit code and output."""
    config = tmp_path / "user.toml"
    config.write_text(config_text, encoding="utf-8")
    out = tmp_path / ("schedule-fsi.xlsx" if format_id == "xlsx" else "schedule-fsi.csv")

    rc = main(
        [
            "schedule-fsi",
            "--input",
            str(_WHT_FIXTURE),
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
    return rc, out


@pytest.mark.parametrize("format_id", ["filing-csv", "review-csv", "xlsx"])
@pytest.mark.parametrize(("tins", "expected_rc"), [("", 3), (US_FOREIGN_TIN_TOML, 0)])
def test_schedule_fsi_exits_3_while_a_relief_row_tin_is_blank(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    format_id: str,
    tins: str,
    expected_rc: int,
) -> None:
    """The US relief row's TIN cell is blank until [account.foreign_tins] has
    US: every format still writes the file and prints the nudge, and exits 3.
    The round trip's capital-gains row, with no foreign tax, needs no Indian
    tax payable, so it prints no warning and doesn't make the run exit 3."""
    rc, out = _schedule_fsi_on_the_wht_fixture(
        tmp_path, _USER_TOML + _US_DIVIDEND_TAX_PAYABLE + tins, format_id
    )

    assert rc == expected_rc
    assert out.is_file()
    err = capsys.readouterr().err
    nudged = "Taxpayer Identification Number is BLANK" in err
    assert nudged is (expected_rc == 3)
    assert "INDIAN TAX CALCULATION REQUIRED" not in err


# ---- blank Indian tax payable on a relief row (#1328) ------------------------

# SHA-256 of the file each format writes with the US TIN and no [form67]
# entry, as main wrote it before #1328 (d0966b7); the workbook's under
# fix_xlsx_clock. #1427: the filing CSV's "Manual fill guidance" cell ends with
# the new override remedy; nothing else changes. #1446: that cell drops its
# lending-fee by-hand clause; nothing else changes.
_BLANK_INDIAN_TAX_SHA256 = {
    "filing-csv": "cafaae6f41cb478452ea2f162556b01c89025612aa265160868ccb07918c294c",
    "review-csv": "5877bf537663dc3b137dfba8b776e09cea90dfde23c26fb0528c113e5a08fe8c",
    "xlsx": "39777d288a3f98a8d14a5f045c8039b4f9ae51a771147f4c725efb2b87845c58",
}


@pytest.mark.parametrize("format_id", ["filing-csv", "review-csv", "xlsx"])
def test_schedule_fsi_exits_3_while_a_relief_row_indian_tax_is_blank(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    format_id: str,
) -> None:
    """With no [form67] entry, the US dividend row's Indian tax payable (d) is
    blank: every format still writes main's bytes and prints the warning, and
    exits 3."""
    fix_xlsx_clock(monkeypatch)

    rc, out = _schedule_fsi_on_the_wht_fixture(
        tmp_path, _USER_TOML + US_FOREIGN_TIN_TOML, format_id
    )

    assert hashlib.sha256(out.read_bytes()).hexdigest() == _BLANK_INDIAN_TAX_SHA256[format_id]
    err = capsys.readouterr().err
    assert MISSING_INDIAN_TAX_WARNING in err
    assert "Taxpayer Identification Number is BLANK" not in err
    assert rc == 3


# ---- exact sums (#1166) -------------------------------------------------------


@pytest.mark.parametrize(
    ("format_id", "suffix"),
    [("filing-csv", ".csv"), ("review-csv", ".csv"), ("xlsx", ".xlsx")],
)
@pytest.mark.parametrize("stale", [False, True], ids=["fresh", "stale"])
def test_capital_gains_net_past_the_exact_cap_refuses_and_clears_the_output(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    format_id: str,
    suffix: str,
    stale: bool,
) -> None:
    """#1166 (owner decision 2026-11-10: exact, never round): STCG gains of
    ₹1E10 and -₹1E-995 need more digits than an exact sum may hold. The
    first line carries every privacy marker; none is printed."""
    lines = [marked(stcg_line("1", "0")), stcg_line("1E+10", "0"), stcg_line("0", "1E-995")]
    monkeypatch.setattr(
        "ibkr_fa.compute.income_engine.compute_schedule_cg_stcg_lines",
        lambda *_args, **_kwargs: ScheduleCgStcgComputeResult(lines, [], [], []),
    )
    config = tmp_path / "user.toml"
    config.write_text(_USER_TOML, encoding="utf-8")
    out = tmp_path / f"schedule-fsi{suffix}"
    if stale:
        out.write_text("stale", encoding="utf-8")

    rc = main(
        [
            "schedule-fsi",
            "--input",
            str(_CG_FIXTURE),
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
        "error: InvariantError: Schedule FSI capital gains STCG net, input row 3, "
        f"column realized_gain_inr: {INEXACT_SUM}\n"
    )
    assert captured.out == ""
    for marker in MARKERS:
        assert marker not in captured.err, marker
    assert not out.exists()


# ---- exact line conversion (#1184) --------------------------------------------


@pytest.mark.parametrize(
    ("format_id", "suffix"),
    [("filing-csv", ".csv"), ("review-csv", ".csv"), ("xlsx", ".xlsx")],
)
@pytest.mark.parametrize("stale", [False, True], ids=["fresh", "stale"])
@pytest.mark.parametrize(
    ("schedule", "opened"), [("STCG", SHORT_TERM_OPEN), ("LTCG", LONG_TERM_OPEN)]
)
def test_capital_gain_past_the_exact_cap_refuses_at_its_line_and_clears_the_output(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    format_id: str,
    suffix: str,
    stale: bool,
    schedule: str,
    opened: date,
) -> None:
    """#1184: the income engine's CG line refuses at its exact gain, earlier
    than main's refusal at FSI's checked capital-gains net (``input row 1``).
    STCG's compute skips the long-term lot, so LTCG's refuses it."""
    raised = compute_on(
        monkeypatch,
        [marked_lot(PAST_THE_CAP, opened=opened)],
        "ibkr_fa.compute.income_engine.compute_schedule_cg_stcg_lines",
        "ibkr_fa.compute.income_engine.compute_schedule_cg_ltcg_lines",
        rate="1",
    )
    config = tmp_path / "user.toml"
    config.write_text(_USER_TOML, encoding="utf-8")
    inputs = sorted(tmp_path.iterdir())
    out = tmp_path / f"schedule-fsi{suffix}"
    if stale:
        out.write_text("stale", encoding="utf-8")

    rc = main(
        [
            "schedule-fsi",
            "--input",
            str(_CG_FIXTURE),
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
    assert_private_refusal(
        capsys, raised, GAIN_PAST_THE_CAP.format(schedule=schedule), PAST_THE_CAP
    )
    assert sorted(tmp_path.iterdir()) == inputs


# ---- Other Sources income adds up to Schedule OS (#1319) ---------------------

_INCOME_LABEL = "Income from outside India (included in PART B-TI)"


def _dividend_and_interest(tmp_path: Path, dividend: str, interest: str) -> Path:
    """The withholding fixture with its US dividend set to ``dividend``, its
    withholding dropped, and one USD credit-interest row of ``interest``."""
    text = _WHT_FIXTURE.read_text(encoding="utf-8")
    # The dividend is in its row and in the section's Total row.
    assert text.count(",2.60,") == 2
    lines = [
        line
        for line in text.replace(",2.60,", f",{dividend},").splitlines()
        if not line.startswith("Withholding Tax,")
    ]
    lines += [
        "Interest,Header,Currency,Date,Description,Amount,,,,,,,,,,,",
        f"Interest,Data,USD,30-09-2025,USD Credit Interest for Sep-2025,{interest},,,,,,,,,,,",
        f"Interest,Data,Total,,,{interest},,,,,,,,,,,",
    ]
    statement = tmp_path / "statement.csv"
    statement.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return statement


def _filing_fields(path: Path) -> dict[str, str]:
    with path.open(encoding="utf-8-sig", newline="") as handle:
        return {row[0]: row[1] for row in csv.reader(handle) if len(row) > 1 and row[0]}


def _other_sources_income(path: Path) -> list[str]:
    """Each Other Sources block's income in a vertical FSI filing CSV."""
    with path.open(encoding="utf-8-sig", newline="") as handle:
        rows = [row for row in csv.reader(handle) if row]
    return [
        rows[index + 1][1]
        for index, row in enumerate(rows)
        if row[:2] == ["Head of Income", "Other Sources"]
    ]


@pytest.mark.parametrize(
    ("dividend", "interest", "os_fields", "fsi_income"),
    [
        # The issue's example: HALF_UP of the US row's exact income was ₹24.
        ("0.13", "0.13", ("11", "12"), "23"),
        # Control: the two agreed already.
        ("0.17", "0.17", ("15", "16"), "31"),
    ],
    ids=["moved", "already-equal"],
)
def test_other_sources_income_is_the_schedule_os_fields_it_comes_from(
    tmp_path: Path,
    dividend: str,
    interest: str,
    os_fields: tuple[str, str],
    fsi_income: str,
) -> None:
    """Schedule FSI files the US Other Sources income Schedule OS files in
    1a(i) and 1b(ix); ITR-3 rule 894 refuses an upload where FSI's is more."""
    statement = _dividend_and_interest(tmp_path, dividend, interest)
    config = tmp_path / "user.toml"
    config.write_text(_USER_TOML, encoding="utf-8")
    outputs = {}
    for command in ("schedule-os", "schedule-fsi"):
        outputs[command] = tmp_path / f"{command}.csv"
        rc = main(
            [
                command,
                "--input",
                str(statement),
                "--config",
                str(config),
                "--output",
                str(outputs[command]),
                "--format",
                "filing-csv",
                "--financial-year",
                "2025-26",
            ]
        )
        assert rc == 0

    os_filed = _filing_fields(outputs["schedule-os"])
    assert (
        os_filed["1ai. Dividend income [other than (ii) and (iii)]"],
        os_filed["1b(ix). Others including interest from Companies, NBFCs & HFCs"],
    ) == os_fields
    assert _other_sources_income(outputs["schedule-fsi"]) == [fsi_income]


def test_run_all_files_the_schedule_os_rupees_in_schedule_fsi(tmp_path: Path) -> None:
    statement = _dividend_and_interest(tmp_path, "0.13", "0.13")
    config = tmp_path / "user.toml"
    config.write_text(_USER_TOML, encoding="utf-8")
    out = tmp_path / "out"
    out.mkdir()

    rc = main(
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
            "--format",
            "review-csv",
            "--itr-form",
            "ITR-2",
        ]
    )

    assert rc == 0
    with (out / "schedule-fsi.csv").open(encoding="utf-8-sig", newline="") as handle:
        rows = list(csv.DictReader(handle))
    assert [row[_INCOME_LABEL] for row in rows if row["Head of Income"] == "Other Sources"] == [
        "23"
    ]
