"""Tests for the PR #218 ``schedule-tr`` CLI subcommand."""

from __future__ import annotations

import hashlib
from dataclasses import replace
from datetime import date
from decimal import Decimal
from functools import partial
from pathlib import Path
from typing import Any

import pytest
from openpyxl import load_workbook

from ibkr_fa import cli
from ibkr_fa.cli import main
from ibkr_fa.config import ScheduleOsIndianAdrRoutingMode
from ibkr_fa.exceptions import InvariantError
from ibkr_fa.models.schedule_tr_row import TaxReliefSection
from ibkr_fa.output._cleanup import RunOutputs
from ibkr_fa.parser.format_detect import IbkrCsvFormat
from ibkr_fa.pipeline import FinancialYear
from ibkr_fa.pipeline_tr import ScheduleTrRunReport, TrPerCountryRow
from tests._crash_reports import assert_no_markers
from tests._fiscal_cli import install_fake, run_args, write_cli_paths
from tests._ftc_warnings import MISSING_INDIAN_TAX_WARNING, US_ZERO_CREDIT_REFUSAL
from tests._ibkr_user_toml import US_FOREIGN_TIN_TOML, US_PERSON_USER_TOML
from tests._portal_parity import fix_xlsx_clock

_FIXTURES_DIR = Path(__file__).parent / "fixtures"
_WHT_FIXTURE = _FIXTURES_DIR / "e2e_activity_statement_schedule_tr_with_wht_fy2025_26.csv"


def _make_fake_report(**overrides: Any) -> ScheduleTrRunReport:
    per_country_rows = (
        TrPerCountryRow(
            cbdt_country_code="2",
            country_name_portal="UNITED STATES OF AMERICA",
            iso_alpha2="US",
            foreign_tax_inr_total_portal_displayed=Decimal("1851"),
            foreign_tax_inr_total_full_precision=Decimal("1851.234"),
            tax_relief_inr_total=None,
            section_under_which_claimed=None,
            event_count=3,
        ),
        TrPerCountryRow(
            cbdt_country_code="44",
            country_name_portal="UNITED KINGDOM",
            iso_alpha2="GB",
            foreign_tax_inr_total_portal_displayed=Decimal("1235"),
            foreign_tax_inr_total_full_precision=Decimal("1234.255"),
            tax_relief_inr_total=None,
            section_under_which_claimed=None,
            event_count=1,
        ),
    )
    defaults: dict[str, Any] = {
        "run_outputs": RunOutputs((), ()),
        "rows_written": 2,
        "per_country_rows": per_country_rows,
        "total_foreign_tax_inr_portal_displayed": Decimal("3086"),
        "total_foreign_tax_inr_full_precision": Decimal("3085.489"),
        "fsi_rows_consumed": 4,
        "os_dividend_rows_consumed": 2,
        "os_interest_rows_consumed": 1,
        "cg_stcg_rows_consumed": 1,
        "cg_ltcg_rows_consumed": 0,
        "wht_rows_in_fy": 2,
        "excluded_cg_indian_source_count": 0,
        "excluded_cg_out_of_fy_count": 0,
        "excluded_ltcg_pre_cutover_count": 0,
        "excluded_ltcg_indian_source_count": 0,
        "excluded_ltcg_out_of_fy_count": 0,
        "interest_credits_out_of_fy_count": 0,
        "adr_routing_mode": ScheduleOsIndianAdrRoutingMode.DEPOSITARY,
        "output_path": Path("schedule_tr.csv"),
        "account_id": "U12345678",
        "account_id_cross_checked": True,
        "input_format": IbkrCsvFormat.ACTIVITY_STATEMENT,
        "financial_year_start": date(2025, 4, 1),
        "financial_year_end": date(2026, 3, 31),
        "fx_policy_appropriate_for_ay": True,
        "wht_fx_policy_appropriate_for_ay": True,
        "identity_supplied_from_fallback": frozenset(),
        "portal_labels_assessment_year_start": 2026,
        "portal_labels_used_forward_compat_fallback": False,
    }
    defaults.update(overrides)
    return ScheduleTrRunReport(**defaults)


@pytest.fixture
def cli_paths(tmp_path: Path) -> tuple[Path, Path, Path]:
    return write_cli_paths(tmp_path, US_PERSON_USER_TOML)


def _write_user_toml(tmp_path: Path) -> Path:
    path = tmp_path / "user.toml"
    path.write_text(US_PERSON_USER_TOML, encoding="utf-8")
    return path


_run_args = partial(run_args, ("schedule-tr", "--itr-form", "ITR-2"))
_install_fake = partial(install_fake, pipeline="run_schedule_tr")


def test_schedule_tr_missing_indian_tax_warning_is_in_safety_output() -> None:
    report = _make_fake_report(has_unresolved_indian_tax_payable=True)
    warning = cli._render_schedule_tr_safety_warnings(report)
    assert "INDIAN TAX CALCULATION REQUIRED" in warning
    assert "average Indian tax rate" in warning
    assert "Rule 128(5)(i)" in warning


# ---- argparse layer --------------------------------------------------------


def test_schedule_tr_help_runs(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exc:
        main(["schedule-tr", "--help"])
    assert exc.value.code == 0
    out = capsys.readouterr().out
    normalised = " ".join(out.lower().split())
    assert "schedule tr" in normalised
    assert "summary of tax relief claimed" in normalised
    assert "dual" in normalised and "anchor" in normalised


def test_schedule_tr_requires_input_config_output_fy(
    capsys: pytest.CaptureFixture[str],
) -> None:
    with pytest.raises(SystemExit) as exc:
        main(["schedule-tr"])
    assert exc.value.code == 2
    err = capsys.readouterr().err
    assert "--input" in err or "required" in err.lower()


def test_schedule_tr_format_choices_are_review_csv_and_xlsx(
    capsys: pytest.CaptureFixture[str],
) -> None:
    with pytest.raises(SystemExit) as exc:
        main(["schedule-tr", "--help"])
    assert exc.value.code == 0
    out = capsys.readouterr().out
    assert "review-csv" in out
    assert "xlsx" in out


def test_schedule_tr_rejects_unknown_format(
    capsys: pytest.CaptureFixture[str],
    cli_paths: tuple[Path, Path, Path],
) -> None:
    with pytest.raises(SystemExit) as exc:
        main(_run_args(cli_paths, "--format", "json"))
    assert exc.value.code == 2
    assert "argument --format: invalid value" in capsys.readouterr().err.lower()


def test_schedule_tr_passes_args_to_pipeline_with_default_format(
    monkeypatch: pytest.MonkeyPatch,
    cli_paths: tuple[Path, Path, Path],
) -> None:
    recorder: dict[str, Any] = {}
    _install_fake(monkeypatch, _make_fake_report(), recorder=recorder)
    inp, cfg, out = cli_paths

    rc = main(
        _run_args(
            cli_paths,
            "--allow-masked-account",
            "--suppress-cross-ay-warning",
            "--accept-anonymised-statement",
        )
    )

    assert rc == 0
    assert recorder["input_path"] == inp
    assert recorder["config_path"] == cfg
    assert recorder["output_path"] == out
    assert recorder["financial_year"] == FinancialYear(start_year=2025)
    assert recorder["format_id"] == "review-csv"
    assert recorder["allow_masked_account"] is True
    assert recorder["suppress_cross_ay_warning"] is True
    assert recorder["accept_anonymised_statement"] is True


def test_schedule_tr_accepts_xlsx_format(
    monkeypatch: pytest.MonkeyPatch,
    cli_paths: tuple[Path, Path, Path],
) -> None:
    recorder: dict[str, Any] = {}
    _install_fake(monkeypatch, _make_fake_report(), recorder=recorder)

    rc = main(_run_args(cli_paths, "--format", "xlsx"))

    assert rc == 0
    assert recorder["format_id"] == "xlsx"


# ---- stdout renderers ------------------------------------------------------


def test_schedule_tr_verbose_output_includes_breakdown_and_totals(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    cli_paths: tuple[Path, Path, Path],
) -> None:
    _install_fake(monkeypatch, _make_fake_report())

    rc = main(_run_args(cli_paths))

    assert rc == 0
    captured = capsys.readouterr()
    assert "Schedule TR — Summary of Tax Relief Claimed" in captured.out
    assert "Wrote 2 TR rows" in captured.out
    assert "UNITED STATES OF AMERICA" in captured.out
    assert "UNITED KINGDOM" in captured.out
    assert "Total foreign tax paid (portal-displayed): INR 3086" in captured.out
    assert "Total foreign tax paid (full precision): INR 3085.489" in captured.out
    assert "Tax relief and section under which claimed: LEFT BLANK" in captured.out
    assert "FSI rows consumed: 4" in captured.out
    assert "WHT in FY: 2" in captured.out
    assert "Account: U***5678" in captured.out
    assert "Schedule TR tax relief and section are LEFT BLANK" in captured.err


class _WholeRupees(Decimal):
    """A report total that refuses another rounding pass (#900)."""

    def quantize(self, *_args: object, **_kwargs: object) -> Decimal:
        raise AssertionError("the Schedule TR CLI rounded a whole-rupee total")


def test_schedule_tr_verbose_output_prints_the_whole_rupee_totals_as_they_are(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    cli_paths: tuple[Path, Path, Path],
) -> None:
    report = _make_fake_report()
    (us, gb) = report.per_country_rows
    _install_fake(
        monkeypatch,
        _make_fake_report(
            per_country_rows=(
                replace(us, foreign_tax_inr_total_portal_displayed=_WholeRupees("1851")),
                gb,
            ),
            total_foreign_tax_inr_portal_displayed=_WholeRupees("3086"),
        ),
    )

    assert main(_run_args(cli_paths)) == 0

    out = capsys.readouterr().out
    assert "UNITED STATES OF AMERICA | INR 1851 | 3" in out
    assert "Total foreign tax paid (portal-displayed): INR 3086" in out


def test_schedule_tr_quiet_emits_csv_header_and_rows(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    cli_paths: tuple[Path, Path, Path],
) -> None:
    _install_fake(monkeypatch, _make_fake_report())

    rc = main(_run_args(cli_paths, "--quiet"))

    assert rc == 0
    lines = capsys.readouterr().out.strip().split("\n")
    assert lines[0] == (
        "cbdt_country_code,iso_alpha2,country_name_portal,"
        "foreign_tax_inr_total_portal_displayed,"
        "foreign_tax_inr_total_full_precision,event_count"
    )
    assert lines[1] == "2,US,UNITED STATES OF AMERICA,1851,1851.234,3"
    assert lines[2] == "44,GB,UNITED KINGDOM,1235,1234.255,1"


def test_schedule_tr_quiet_renders_none_full_precision_as_empty_string(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    cli_paths: tuple[Path, Path, Path],
) -> None:
    """T4 r1/r3 #3 regression: a TrPerCountryRow constructed without a
    full-precision value (foreign_tax_inr_total_full_precision=None)
    must render as an empty cell in the quiet CSV, NOT 'None' /
    'null'. Legacy direct constructors and pre-PR-#217 test fixtures
    rely on this fallback (TR pipeline produces non-None for compute
    paths since PR #217)."""
    report = _make_fake_report(
        rows_written=1,
        per_country_rows=(
            TrPerCountryRow(
                cbdt_country_code="2",
                country_name_portal="UNITED STATES OF AMERICA",
                iso_alpha2="US",
                foreign_tax_inr_total_portal_displayed=Decimal("100"),
                foreign_tax_inr_total_full_precision=None,
                tax_relief_inr_total=None,
                section_under_which_claimed=None,
                event_count=1,
            ),
        ),
        total_foreign_tax_inr_portal_displayed=Decimal("100"),
        total_foreign_tax_inr_full_precision=Decimal("0"),
    )
    _install_fake(monkeypatch, report)

    rc = main(_run_args(cli_paths, "--quiet"))

    assert rc == 0
    lines = capsys.readouterr().out.strip().split("\n")
    # 5 commas → 6 fields: full-precision column is the EMPTY string.
    assert lines[1] == "2,US,UNITED STATES OF AMERICA,100,,1"


def test_schedule_tr_quiet_zero_rows_emits_header_only(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    cli_paths: tuple[Path, Path, Path],
) -> None:
    empty_report = _make_fake_report(
        rows_written=0,
        per_country_rows=(),
        total_foreign_tax_inr_portal_displayed=Decimal("0"),
        total_foreign_tax_inr_full_precision=Decimal("0"),
    )
    _install_fake(monkeypatch, empty_report)

    rc = main(_run_args(cli_paths, "--quiet"))

    assert rc == 0
    captured = capsys.readouterr()
    assert captured.out.strip() == (
        "cbdt_country_code,iso_alpha2,country_name_portal,"
        "foreign_tax_inr_total_portal_displayed,"
        "foreign_tax_inr_total_full_precision,event_count"
    )
    # T3 r1 P2 regression on PR #218: the --quiet help promises
    # "audit-bucket counts ALWAYS route to stderr regardless of
    # --quiet". The zero-rows advisory MUST therefore surface on
    # stderr even when --quiet suppresses the verbose stdout render.
    assert "No qualifying TR rows produced" in captured.err
    assert "header only" in captured.err
    # Cross-reference Schedule FSI in the advisory text so the user
    # knows which sister subcommand to consult for the upstream
    # per-(country, head) breakdown.
    assert "schedule-fsi" in captured.err
    # No foreign tax to relieve → the relief advisory is suppressed entirely
    # (it must not claim relief is blank when there are simply no TR rows).
    assert "Schedule TR tax relief" not in captured.err


def test_schedule_tr_quiet_excluded_counters_emit_to_stderr(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    cli_paths: tuple[Path, Path, Path],
) -> None:
    """T3 r1 P2 regression: every nonzero upstream-exclusion counter
    must surface to stderr in --quiet mode, mirroring the FSI sibling
    so the --quiet help text's audit-bucket promise is honored.

    T2/T3 r2 strengthening: also assert each cited sister-audit-block
    MARKER appears exactly once, in the expected advisory, so a
    future copy-paste swap (such as round 2's
    EXCLUDED_LTCG_OUT_OF_FY_MARKER<->EXCLUDED_CG_LTCG_INDIAN_SOURCE_MARKER
    confusion) is caught."""
    from ibkr_fa.output import (  # noqa: PLC0415 — test-local
        EXCLUDED_CG_INDIAN_SOURCE_MARKER,
        EXCLUDED_CG_LTCG_INDIAN_SOURCE_MARKER,
        EXCLUDED_LTCG_OUT_OF_FY_MARKER,
        EXCLUDED_OUT_OF_FY_MARKER,
        EXCLUDED_PRE_CUTOVER_MARKER,
    )

    report_with_exclusions = _make_fake_report(
        excluded_cg_indian_source_count=3,
        excluded_ltcg_indian_source_count=1,
        excluded_cg_out_of_fy_count=4,
        excluded_ltcg_out_of_fy_count=5,
        excluded_ltcg_pre_cutover_count=6,
        interest_credits_out_of_fy_count=7,
    )
    _install_fake(monkeypatch, report_with_exclusions)

    rc = main(_run_args(cli_paths, "--quiet"))

    assert rc == 0
    err = capsys.readouterr().err
    assert "Excluded Indian-source capital gains upstream of TR: 3 STCG + 1 LTCG" in err
    assert "Excluded 4 STCG + 5 LTCG closure" in err
    assert "Excluded 6 LTCG closure" in err
    assert "Excluded 7 broker credit-interest" in err
    # Each marker appears exactly once and on the right advisory
    # block. The Indian-source-LTCG cite must NOT collide with the
    # out-of-FY-LTCG cite.
    assert err.count(EXCLUDED_CG_INDIAN_SOURCE_MARKER) == 1
    assert err.count(EXCLUDED_CG_LTCG_INDIAN_SOURCE_MARKER) == 1
    assert err.count(EXCLUDED_OUT_OF_FY_MARKER) == 1
    assert err.count(EXCLUDED_LTCG_OUT_OF_FY_MARKER) == 1
    assert err.count(EXCLUDED_PRE_CUTOVER_MARKER) == 1


# ---- safety warnings -------------------------------------------------------


def test_schedule_tr_report_refuses_a_skipped_cross_check_for_a_mask_with_a_line_break() -> None:
    """#854: ``"U***\\n"`` is not IBKR's mask, so it can't justify a
    skipped cross-check."""
    with pytest.raises(InvariantError, match="bypassed without justification"):
        _make_fake_report(account_id="U***\n", account_id_cross_checked=False)


def test_schedule_tr_safety_warnings_cover_tr_specific_and_carryover_advisories() -> None:
    report = _make_fake_report(
        account_id="U**********",
        account_id_cross_checked=False,
        fx_policy_appropriate_for_ay=False,
        wht_fx_policy_appropriate_for_ay=False,
        identity_supplied_from_fallback=frozenset({"Account", "Name"}),
        portal_labels_used_forward_compat_fallback=True,
        adr_routing_mode=ScheduleOsIndianAdrRoutingMode.UNDERLYING_ISSUER,
    )

    out = cli._render_schedule_tr_safety_warnings(report)

    assert "cross-check was SKIPPED" in out
    assert "Identity field(s)" in out
    assert "AY 2027-28" in out
    assert "Rule 206" in out
    assert "Rule 76" in out
    assert "Portal labels" in out
    assert "UNDERLYING_ISSUER" in out
    assert "Schedule TR tax relief and section are LEFT BLANK" in out
    assert "ITR" in out


def test_schedule_tr_safety_warnings_emit_foreign_tin_nudge_when_missing() -> None:
    """A TR row (every TR row is a relief context by invariant) with no
    configured foreign TIN must trigger the non-quietable TIN nudge."""
    report = _make_fake_report(has_foreign_tax_without_taxpayer_id=True)

    out = cli._render_schedule_tr_safety_warnings(report)

    assert "Taxpayer Identification Number is BLANK" in out
    assert "[account.foreign_tins]" in out
    assert "90 / 90A / 91" in out


def test_schedule_tr_safety_warnings_omit_foreign_tin_nudge_when_present() -> None:
    """Symmetric contract: when every relief country has a TIN (factory
    default has_foreign_tax_without_taxpayer_id=False), no nudge."""
    report = _make_fake_report()

    out = cli._render_schedule_tr_safety_warnings(report)

    assert "Taxpayer Identification Number is BLANK" not in out


def test_schedule_tr_end_to_end_happy_path_real_fixture(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    cfg = _write_user_toml(tmp_path)
    out = tmp_path / "schedule_tr.csv"

    rc = main(
        [
            "schedule-tr",
            "--itr-form",
            "ITR-2",
            "--input",
            str(_WHT_FIXTURE),
            "--config",
            str(cfg),
            "--output",
            str(out),
            "--financial-year",
            "2025-26",
        ]
    )

    assert rc == 3  # no [account.foreign_tins]: the US row's TIN is blank (#924)
    assert out.exists()
    captured = capsys.readouterr()
    assert "Schedule TR — Summary of Tax Relief Claimed" in captured.out
    assert "Wrote 1 TR rows" in captured.out
    assert "UNITED STATES OF AMERICA" in captured.out
    assert "Total foreign tax paid" in captured.out
    assert "Tax relief and section under which claimed: LEFT BLANK" in captured.out
    assert "INDIAN TAX CALCULATION REQUIRED" in captured.err
    assert "Schedule TR tax relief and section are LEFT BLANK" in captured.err


def test_schedule_tr_invalid_input_file_returns_domain_error(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    cfg = _write_user_toml(tmp_path)
    out = tmp_path / "schedule_tr.csv"

    rc = main(
        [
            "schedule-tr",
            "--itr-form",
            "ITR-2",
            "--input",
            str(tmp_path / "missing.csv"),
            "--config",
            str(cfg),
            "--output",
            str(out),
            "--financial-year",
            "2025-26",
        ]
    )

    assert rc == 4
    captured = capsys.readouterr()
    assert (captured.out, captured.err) == (
        "",
        "error: schedule-tr: a file operation failed (ENOENT)\n",
    )
    assert not out.exists()


def test_schedule_tr_invalid_config_returns_domain_error(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    cfg = tmp_path / "bad.toml"
    cfg.write_text("[account]\nnumber = @\n", encoding="utf-8")
    out = tmp_path / "schedule_tr.csv"

    rc = main(
        [
            "schedule-tr",
            "--itr-form",
            "ITR-2",
            "--input",
            str(_WHT_FIXTURE),
            "--config",
            str(cfg),
            "--output",
            str(out),
            "--financial-year",
            "2025-26",
        ]
    )

    assert rc == 4
    captured = capsys.readouterr()
    assert (captured.out, captured.err) == (
        "",
        "error: ConfigError at field '<file>': invalid TOML at line 2, column 10\n",
    )
    assert not out.exists()


# ---- #36/#37 relief-population messaging (mirrors Form 67's #261/#262 fix) --

_TR_TAX_PAYABLE = """
[form67]
[[form67.tax_payable_overrides]]
country = "US"
source = "DIVIDEND"
tax_payable_normal_inr = "9999"
"""
_TR_RELIEF_CONFIG = US_PERSON_USER_TOML + _TR_TAX_PAYABLE + US_FOREIGN_TIN_TOML


def _tr_row(
    iso: str,
    cbdt: str,
    name: str,
    *,
    relief: str | None,
    section: TaxReliefSection | None,
) -> TrPerCountryRow:
    return TrPerCountryRow(
        cbdt_country_code=cbdt,
        country_name_portal=name,
        iso_alpha2=iso,
        foreign_tax_inr_total_portal_displayed=Decimal("100"),
        foreign_tax_inr_total_full_precision=Decimal("100"),
        tax_relief_inr_total=None if relief is None else Decimal(relief),
        section_under_which_claimed=section,
        event_count=1,
    )


def test_schedule_tr_relief_fully_populated_real_fixture(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """With exact source-level Indian tax, the real fixture's relief is computed,
    so the console must say POPULATED — never the stale 'v1 skeleton'/'fill
    manually' claim that contradicts the written CSV.
    """
    cfg = tmp_path / "user.toml"
    cfg.write_text(_TR_RELIEF_CONFIG, encoding="utf-8")
    out = tmp_path / "schedule_tr.csv"

    rc = main(
        [
            "schedule-tr",
            "--itr-form",
            "ITR-2",
            "--input",
            str(_WHT_FIXTURE),
            "--config",
            str(cfg),
            "--output",
            str(out),
            "--financial-year",
            "2025-26",
        ]
    )

    assert rc == 0
    captured = capsys.readouterr()
    # CSV carries the computed relief + section (cols 4-5).
    data_row = out.read_text(encoding="utf-8").splitlines()[1].split(",")
    assert data_row[3] != ""  # tax relief
    assert data_row[4] == "90"  # section
    assert "Tax relief and section under which claimed: POPULATED" in captured.out
    assert "Schedule TR tax relief + section are POPULATED" in captured.err
    # The stale claims must be gone.
    assert "v1 skeleton" not in (captured.out + captured.err).lower()
    assert "NOT COMPUTED" not in captured.out
    assert "fill in the portal manually" not in captured.err
    assert "fill manually" not in captured.err


def _schedule_tr_on_the_wht_fixture(
    tmp_path: Path, config_text: str, format_id: str, *, statement: Path = _WHT_FIXTURE
) -> tuple[int, Path]:
    """Run ``schedule-tr`` on ``statement`` (default: the WHT fixture) for FY
    2025-26 in ``format_id`` with ``config_text`` as ``user.toml``; return the
    exit code and output."""
    cfg = tmp_path / "user.toml"
    cfg.write_text(config_text, encoding="utf-8")
    out = tmp_path / ("schedule_tr.xlsx" if format_id == "xlsx" else "schedule_tr.csv")

    rc = main(
        [
            "schedule-tr",
            "--itr-form",
            "ITR-2",
            "--input",
            str(statement),
            "--config",
            str(cfg),
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
def test_schedule_tr_exits_3_while_a_relief_row_tin_is_blank(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    format_id: str,
    tins: str,
    expected_rc: int,
) -> None:
    """The US relief row's TIN cell is blank until [account.foreign_tins] has
    US: every format still writes the file and prints the nudge, and exits 3
    (#924). The round trip's US capital-gains row, with no foreign tax, needs
    no Indian tax payable, so it prints no warning and doesn't make the run
    exit 3 (#1328)."""
    rc, out = _schedule_tr_on_the_wht_fixture(
        tmp_path, US_PERSON_USER_TOML + _TR_TAX_PAYABLE + tins, format_id
    )

    assert rc == expected_rc
    assert out.is_file()
    err = capsys.readouterr().err
    nudged = "Taxpayer Identification Number is BLANK" in err
    assert nudged is (expected_rc == 3)
    assert "INDIAN TAX CALCULATION REQUIRED" not in err


# SHA-256 of the file each format writes with the US TIN and no [form67]
# entry, as main wrote it before #1328 (d0966b7); the workbook's under
# fix_xlsx_clock.
_BLANK_INDIAN_TAX_SHA256 = {
    "filing-csv": "d14815c09a0f2b8b98fcd9a74f42b35266ad4c179c41087515e0c74bf55991e5",
    "review-csv": "48cd27e098445c134c29dd1e521ae41d93ac80235f1c828ef549a20e1f890f04",
    "xlsx": "65448d104ed2bea220b8792a30064b98e517d582af0e18060cbfabe3f0fe2486",
}


@pytest.mark.parametrize("format_id", ["filing-csv", "review-csv", "xlsx"])
def test_schedule_tr_exits_3_while_a_relief_row_indian_tax_is_blank(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    format_id: str,
) -> None:
    """With no [form67] entry, the US dividend's Indian tax payable is blank,
    so TR's relief and section are too: every format still writes main's bytes
    and prints the warning, and exits 3 (#1328)."""
    fix_xlsx_clock(monkeypatch)

    rc, out = _schedule_tr_on_the_wht_fixture(
        tmp_path, US_PERSON_USER_TOML + US_FOREIGN_TIN_TOML, format_id
    )

    assert hashlib.sha256(out.read_bytes()).hexdigest() == _BLANK_INDIAN_TAX_SHA256[format_id]
    err = capsys.readouterr().err
    assert MISSING_INDIAN_TAX_WARNING in err
    assert "Taxpayer Identification Number is BLANK" not in err
    assert rc == 3


# ---- A credit that files as ₹0 refuses, as in form-67 (#601, #1359, #1480) ---

# Digit runs in the statement's dividend and withholding amounts; neither may
# reach stdout or stderr.
_DIVIDEND_MARKER = "736251"
_WITHHOLDING_MARKER = "110437"
# A synthetic Bermuda (no DTAA, so section 91) ISIN.
_BM_ISIN = "BM99970Q3001"


def _marked_statement(tmp_path: Path, iso: str) -> Path:
    """The WHT fixture with the marker amounts, its security moved to Bermuda
    when ``iso`` is ``BM``."""
    text = _WHT_FIXTURE.read_text(encoding="utf-8")
    for old, new in (
        ("Share (Ordinary Dividend),2.60,", f"Share (Ordinary Dividend),{_DIVIDEND_MARKER}.00,"),
        ("Total,,,2.60,", f"Total,,,{_DIVIDEND_MARKER}.00,"),
        ("US Tax,-0.39,", f"US Tax,-{_WITHHOLDING_MARKER}.00,"),
        ("Total,,,-0.39,", f"Total,,,-{_WITHHOLDING_MARKER}.00,"),
    ):
        assert text.count(old) == 1
        text = text.replace(old, new)
    if iso == "BM":
        assert text.count("US99995C1027") == 4
        text = text.replace("US99995C1027", _BM_ISIN).replace(" - US Tax", " - BM Tax")
    statement = tmp_path / "activity.csv"
    statement.write_text(text, encoding="utf-8")
    return statement


def _indian_tax_config(iso: str, tax_payable: str) -> str:
    """The TR config with ``iso``'s TIN and its dividend's Indian tax payable;
    a ``BM`` (section 91) run also gets Indian tax of ₹0 before section 91
    relief."""
    config = (
        US_PERSON_USER_TOML
        + US_FOREIGN_TIN_TOML.replace("US =", f"{iso} =")
        + _TR_TAX_PAYABLE.replace('country = "US"', f'country = "{iso}"').replace(
            '"9999"', f'"{tax_payable}"'
        )
    )
    if iso == "BM":
        config += """
[form67.section_91]
total_indian_tax_before_91_relief_inr = "0"
total_income_inr = "2800000"
"""
    return config


def _relief_cells(out: Path, format_id: str) -> tuple[object, object]:
    """The one TR row's relief (d) and section (e) cells, as written."""
    if format_id == "xlsx":
        sheet = load_workbook(out).active
        assert sheet is not None
        assert sheet.max_row == 2
        return sheet.cell(row=2, column=4).value, sheet.cell(row=2, column=5).value
    [_header, row] = out.read_text(encoding="utf-8-sig").splitlines()
    cells = row.split(",")
    return cells[3], cells[4]


# (country, Indian tax payable, the zero-credit refusal form-67 gives).
_ZERO_CREDIT = "the computed credit rounds to ₹0 in whole rupees"
_SECTION_91_ZERO = "the credit as filed today rounds to zero"


@pytest.mark.parametrize("format_id", ["filing-csv", "review-csv", "xlsx"])
@pytest.mark.parametrize(
    ("iso", "tax_payable", "refusal"),
    [("US", "0", _ZERO_CREDIT), ("US", "0.4", _ZERO_CREDIT), ("BM", "0", _SECTION_91_ZERO)],
)
def test_schedule_tr_refuses_a_credit_that_files_as_zero_as_form_67_does(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    format_id: str,
    iso: str,
    tax_payable: str,
    refusal: str,
) -> None:
    """Indian tax of ₹0 makes the dividend's credit, and so its relief,
    exactly ₹0; 0.4 makes it positive but ₹0 in whole rupees. Either way the
    Form 67 row is REFUSED under the owner's #601 policy, so ``schedule-tr``
    refuses it as ``form-67`` does, in every format: exit 4, ``form-67``'s
    stderr, nothing written and no amount marker printed (#1480). From #1359
    until #1480 it filed (d) ``0`` with its section and exited 0."""
    statement = _marked_statement(tmp_path, iso)
    form67_dir = tmp_path / "form-67"
    form67_dir.mkdir()
    form67_config = form67_dir / "user.toml"
    form67_config.write_text(_indian_tax_config(iso, tax_payable), encoding="utf-8")
    form67_rc = main(
        [
            "form-67",
            *("--input", str(statement), "--config", str(form67_config)),
            *("--output", str(form67_dir / "form-67.csv"), "--financial-year", "2025-26"),
        ]
    )
    form67 = capsys.readouterr()
    assert form67_rc == 4
    assert refusal in form67.err
    tr_dir = tmp_path / "schedule-tr"
    tr_dir.mkdir()

    rc, _out = _schedule_tr_on_the_wht_fixture(
        tr_dir, _indian_tax_config(iso, tax_payable), format_id, statement=statement
    )

    captured = capsys.readouterr()
    assert rc == 4
    assert (captured.out, captured.err) == (form67.out, form67.err)
    assert list(tr_dir.iterdir()) == [tr_dir / "user.toml"]
    assert_no_markers(captured.out + captured.err, _DIVIDEND_MARKER, _WITHHOLDING_MARKER)


# SHA-256 of the file each format writes with Indian tax "9999", as main wrote
# it before #1359 (d8658fb); the workbook's under fix_xlsx_clock. Indian tax
# "0.4", a positive relief that rounds to ₹0, refuses since #1480 (above).
_POSITIVE_RELIEF_SHA256 = {
    ("9999", "filing-csv"): "cd32c67f6cd04e8b2bdf319917ca265b33bf2e4b35b834f821dc9c256a012c67",
    ("9999", "review-csv"): "285ec24313241c94b8464a6ef15da3b4464d2d785f19f09032973e3a925babb3",
    ("9999", "xlsx"): "b5411b834d3337e3982c0549587d572bb9b560d7324cb50ba5dacd7709fafed6",
}


@pytest.mark.parametrize("format_id", ["filing-csv", "review-csv", "xlsx"])
@pytest.mark.parametrize(("tax_payable", "relief"), [("9999", 34)])
def test_schedule_tr_positive_relief_files_main_bytes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    format_id: str,
    tax_payable: str,
    relief: int,
) -> None:
    """A relief of at least ₹1 still files its rounded value under section 90
    and main's bytes in every format, exit 0."""
    fix_xlsx_clock(monkeypatch)

    rc, out = _schedule_tr_on_the_wht_fixture(
        tmp_path, _indian_tax_config("US", tax_payable), format_id
    )

    assert rc == 0
    assert _relief_cells(out, format_id) == (
        (relief if format_id == "xlsx" else str(relief)),
        "90",
    )
    assert (
        hashlib.sha256(out.read_bytes()).hexdigest()
        == _POSITIVE_RELIEF_SHA256[tax_payable, format_id]
    )


def test_run_all_still_refuses_an_exactly_zero_relief_before_any_schedule(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """run-all's Form 67 preflight refuses the zero-credit row (#601) before
    any schedule, Schedule TR included, writes: exit 4 with main's one error
    line and an empty output directory. Every source command that writes TR,
    and since #1480 a standalone ``schedule-tr``, runs the same check first."""
    cfg = tmp_path / "user.toml"
    cfg.write_text(_indian_tax_config("US", "0"), encoding="utf-8")
    out_dir = tmp_path / "out"
    out_dir.mkdir()

    rc = main(
        [
            "run-all",
            "--fy-input",
            str(_WHT_FIXTURE),
            "--config",
            str(cfg),
            "--output-dir",
            str(out_dir),
            "--financial-year",
            "2025-26",
            "--itr-form",
            "ITR-2",
            "--no-fetch-prices",
        ]
    )

    captured = capsys.readouterr()
    assert rc == 4
    assert captured.out == ""
    assert captured.err == (
        f"error: run-all: ConfigError at field 'form67': {US_ZERO_CREDIT_REFUSAL}\n"
    )
    assert list(out_dir.iterdir()) == []


def test_schedule_tr_relief_renderer_partially_populated() -> None:
    """One populated + one blank country → PARTIALLY POPULATED, naming the
    blank country's ISO code in both the stdout summary and stderr warning.
    """
    rows = (
        _tr_row(
            "US",
            "2",
            "UNITED STATES OF AMERICA",
            relief="100",
            section=TaxReliefSection.SECTION_90,
        ),
        _tr_row("GB", "44", "UNITED KINGDOM", relief=None, section=None),
    )
    report = _make_fake_report(per_country_rows=rows, rows_written=2)

    summary = cli._render_schedule_tr_report(report)
    warnings = cli._render_schedule_tr_safety_warnings(report)

    assert "PARTIALLY POPULATED (1 of 2 countries; blank: GB)" in summary
    assert "PARTIALLY POPULATED (blank: GB)" in warnings
    assert "v1 skeleton" not in (summary + warnings).lower()


def test_schedule_tr_relief_renderer_fully_populated() -> None:
    rows = (
        _tr_row(
            "US",
            "2",
            "UNITED STATES OF AMERICA",
            relief="100",
            section=TaxReliefSection.SECTION_90,
        ),
        _tr_row(
            "GB",
            "44",
            "UNITED KINGDOM",
            relief="50",
            section=TaxReliefSection.SECTION_91,
        ),
    )
    report = _make_fake_report(per_country_rows=rows, rows_written=2)

    summary = cli._render_schedule_tr_report(report)
    warnings = cli._render_schedule_tr_safety_warnings(report)

    assert "Tax relief and section under which claimed: POPULATED" in summary
    assert "Schedule TR tax relief + section are POPULATED" in warnings
    assert "LEFT BLANK" not in summary
    assert "PARTIALLY" not in summary


def test_classify_tr_relief_population_states() -> None:
    us_pop = _tr_row(
        "US", "2", "UNITED STATES OF AMERICA", relief="100", section=TaxReliefSection.SECTION_90
    )
    gb_blank = _tr_row("GB", "44", "UNITED KINGDOM", relief=None, section=None)

    assert cli._classify_tr_relief_population((us_pop, us_pop)) == "fully_populated"
    assert cli._classify_tr_relief_population((us_pop, gb_blank)) == "partially_populated"
    assert cli._classify_tr_relief_population((gb_blank, gb_blank)) == "all_blank"
    # Empty (no foreign tax) classifies as all_blank; renderers special-case it.
    assert cli._classify_tr_relief_population(()) == "all_blank"


def test_schedule_tr_all_blank_relief_text_lists_only_manual_fill_reasons() -> None:
    """Positive-relief rows on both sections refuse (#787). A country whose
    every relief is computed and exactly zero is a Form 67 credit that files
    as ₹0, which ``schedule-tr`` refuses before writing (#601, #1480). An
    all-blank report blames neither ambiguity nor a lack of positive relief."""
    report = _make_fake_report()

    assert cli._schedule_tr_relief_summary_line(report) == (
        "  Tax relief and section under which claimed: LEFT BLANK — not "
        "auto-populated (no verified DTAA path or incomplete §91 evidence); "
        "provide exact Form 67 evidence where applicable and fill manually "
        "before filing"
    )
    assert cli._schedule_tr_relief_advisory_line(report) == (
        "⚠️  Schedule TR tax relief and section are LEFT BLANK — not "
        "auto-populated (no verified DTAA path or incomplete §91 evidence). "
        "Supply exact Form 67 evidence where applicable and fill manually "
        "before filing."
    )


def test_schedule_tr_help_lists_only_manual_fill_reasons(
    capsys: pytest.CaptureFixture[str],
) -> None:
    with pytest.raises(SystemExit):
        main(["schedule-tr", "--help"])
    normalised = " ".join(capsys.readouterr().out.split())
    assert "ambiguous" not in normalised
    assert "positive relief" not in normalised
    assert (
        "a country is left blank when the [form67] config is absent or a "
        "contributing Schedule FSI row's relief can't be computed. All Schedule "
        "FSI guarantees"
    ) in normalised


@pytest.mark.parametrize("subcommand", ["schedule-tr", "form-67"])
def test_subcommand_help_has_no_stale_skeleton_relief_text(
    subcommand: str,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The relief PRs (#230-#234, #277) shipped — neither subcommand's --help
    (description or --quiet text) may still advertise a 'v1 skeleton' or
    'future PR will populate' relief status.
    """
    with pytest.raises(SystemExit):
        main([subcommand, "--help"])
    help_text = capsys.readouterr().out.lower()
    assert "v1 skeleton" not in help_text
    assert "v1-skeleton" not in help_text
    assert "future pr" not in help_text


# ---- filed amounts past the CBDT bound (#830) -------------------------------


def test_foreign_tax_past_the_bound_refuses_before_any_writer(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """USD 1.5 trillion of US tax withheld is about ₹1.3e14. TR's foreign tax
    paid sums the country's FSI rows, and only the other-sources row carries
    tax, so that row refuses first."""
    monkeypatch.setattr(
        "ibkr_fa.pipeline_tr.output_binding",
        lambda *_args: pytest.fail("compute must refuse before any writer is built"),
    )
    text = _WHT_FIXTURE.read_text(encoding="utf-8")
    # The one withholding row, and the section's Total.
    for old, new in (
        ("US Tax,-0.39,", "US Tax,-1500000000000.00,"),
        ("Total,,,-0.39,", "Total,,,-1500000000000.00,"),
    ):
        assert text.count(old) == 1
        text = text.replace(old, new)
    statement = tmp_path / "activity.csv"
    statement.write_text(text, encoding="utf-8")
    config = _write_user_toml(tmp_path)
    inputs = sorted(tmp_path.iterdir())

    rc = main(_run_args((statement, config, tmp_path / "schedule_tr.csv")))

    assert rc == 4
    captured = capsys.readouterr()
    assert (captured.out, captured.err) == (
        "",
        "error: InvariantError: ScheduleFsiRow.foreign_tax_inr must round to a whole-rupee "
        "amount within ±99,999,999,999,999 (the CBDT 14-digit bound) (context keys: value)\n",
    )
    assert sorted(tmp_path.iterdir()) == inputs
