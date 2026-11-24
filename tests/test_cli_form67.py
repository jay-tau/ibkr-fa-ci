"""Tests for the #37 step 3/N ``form-67`` CLI subcommand."""

from __future__ import annotations

import csv
from dataclasses import replace
from datetime import date
from decimal import Decimal
from functools import partial
from pathlib import Path
from typing import Any

import pytest

from ibkr_fa import cli
from ibkr_fa.cli import main
from ibkr_fa.compute.form67 import Form67Readiness, Form67RefusalReason, Form67RowReadiness
from ibkr_fa.config import ScheduleOsIndianAdrRoutingMode
from ibkr_fa.exceptions import InvariantError
from ibkr_fa.models.form67_entry import Form67SourceOfIncome, Form67TreatyRateStatus
from ibkr_fa.output._cleanup import RunOutputs
from ibkr_fa.parser.format_detect import IbkrCsvFormat
from ibkr_fa.pipeline import FinancialYear
from ibkr_fa.pipeline_form67 import Form67RunReport
from ibkr_fa.pipeline_form67_models import Form67PerEntryRow
from tests._fiscal_cli import install_fake, run_args, write_cli_paths
from tests._ftc_warnings import no_credit_line
from tests._ibkr_user_toml import US_PERSON_USER_TOML

_READY = Form67RowReadiness(Form67Readiness.READY)
_BLANK = Form67RowReadiness(Form67Readiness.BLANK)


def _make_fake_report(**overrides: Any) -> Form67RunReport:
    per_entry_rows = (
        Form67PerEntryRow(
            serial_number=1,
            cbdt_country_code="2",
            country_name_portal="UNITED STATES OF AMERICA",
            iso_alpha2="US",
            source_of_income=Form67SourceOfIncome.DIVIDEND,
            income_inr=Decimal("12345.67"),
            foreign_tax_inr=Decimal("1851.23"),
            foreign_tax_rate_percent=Decimal("15.00"),
            event_count=3,
            tax_payable_normal_inr=None,
            tax_payable_special_inr=None,
            section_90_dtaa_article=None,
            section_90_dtaa_rate_percent=None,
            section_90_credit_inr=None,
            section_91_credit_inr=None,
            total_credit_inr=None,
            readiness=_BLANK,
        ),
        Form67PerEntryRow(
            serial_number=2,
            cbdt_country_code="44",
            country_name_portal="UNITED KINGDOM",
            iso_alpha2="GB",
            source_of_income=Form67SourceOfIncome.DIVIDEND,
            income_inr=Decimal("4112.50"),
            foreign_tax_inr=Decimal("616.88"),
            foreign_tax_rate_percent=Decimal("15.00"),
            event_count=1,
            tax_payable_normal_inr=None,
            tax_payable_special_inr=None,
            section_90_dtaa_article=None,
            section_90_dtaa_rate_percent=None,
            section_90_credit_inr=None,
            section_91_credit_inr=None,
            total_credit_inr=None,
            readiness=_BLANK,
        ),
    )
    defaults: dict[str, Any] = {
        "run_outputs": RunOutputs((), ()),
        "rows_written": 2,
        "per_entry_rows": per_entry_rows,
        "total_foreign_tax_inr_full_precision": Decimal("2468.11"),
        "fsi_rows_consumed": 2,
        "os_dividend_rows_consumed": 2,
        "os_interest_rows_consumed": 0,
        "cg_stcg_rows_consumed": 0,
        "cg_ltcg_rows_consumed": 0,
        "wht_rows_in_fy": 2,
        "excluded_cg_indian_source_count": 0,
        "excluded_cg_out_of_fy_count": 0,
        "excluded_ltcg_pre_cutover_count": 0,
        "excluded_ltcg_indian_source_count": 0,
        "excluded_ltcg_out_of_fy_count": 0,
        "interest_credits_out_of_fy_count": 0,
        "adr_routing_mode": ScheduleOsIndianAdrRoutingMode.DEPOSITARY,
        "output_path": Path("form67.csv"),
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
        "official_output_withheld": False,
        "upload_has_fractional_rate": False,
    }
    defaults.update(overrides)
    return Form67RunReport(**defaults)


@pytest.fixture
def cli_paths(tmp_path: Path) -> tuple[Path, Path, Path]:
    return write_cli_paths(tmp_path, US_PERSON_USER_TOML)


_run_args = partial(run_args, ("form-67",))
_install_fake = partial(install_fake, pipeline="run_form67")


# ---- argparse layer --------------------------------------------------------


def test_form67_help_runs(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exc:
        main(["form-67", "--help"])
    assert exc.value.code == 0
    out = capsys.readouterr().out
    normalised = " ".join(out.lower().split())
    assert "form 67" in normalised
    assert "foreign tax credit" in normalised
    assert "rule 128(9)" in normalised


def test_help_column_count_matches_review_csv(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """#1436: the help's audit-view column count is the review CSV's width."""
    config = tmp_path / "user.toml"
    config.write_text(US_PERSON_USER_TOML, encoding="utf-8")
    out = tmp_path / "form67.csv"
    assert main(_run_args((_WHT_FIXTURE, config, out), "--format", "review-csv")) == 0
    rows = list(csv.reader(out.read_text(encoding="utf-8-sig").splitlines()))
    assert {len(row) for row in rows} == {len(rows[0])}
    capsys.readouterr()

    with pytest.raises(SystemExit):
        main(["form-67", "--help"])
    rendered = " ".join(capsys.readouterr().out.split()).replace("- ", "-")
    assert f"{len(rows[0])}-column audit view" in rendered


def test_form67_requires_input_config_output_fy(
    capsys: pytest.CaptureFixture[str],
) -> None:
    with pytest.raises(SystemExit) as exc:
        main(["form-67"])
    assert exc.value.code == 2
    err = capsys.readouterr().err
    assert "--input" in err or "required" in err.lower()


def test_form67_format_choices_include_review_csv(
    capsys: pytest.CaptureFixture[str],
) -> None:
    with pytest.raises(SystemExit) as exc:
        main(["form-67", "--help"])
    assert exc.value.code == 0
    out = capsys.readouterr().out
    assert "review-csv" in out


def test_form67_rejects_unknown_format(
    capsys: pytest.CaptureFixture[str],
    cli_paths: tuple[Path, Path, Path],
) -> None:
    with pytest.raises(SystemExit) as exc:
        main(_run_args(cli_paths, "--format", "json"))
    assert exc.value.code == 2
    assert "argument --format: invalid value" in capsys.readouterr().err.lower()


def test_form67_passes_args_to_pipeline_with_default_format(
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
    assert recorder["format_id"] == "portal-csv"
    assert recorder["debug_output_path"] is None
    assert recorder["allow_masked_account"] is True
    assert recorder["suppress_cross_ay_warning"] is True
    assert recorder["accept_anonymised_statement"] is True


# ---- Verbose render --------------------------------------------------------


def test_form67_verbose_render_includes_per_entry_breakdown(
    monkeypatch: pytest.MonkeyPatch,
    cli_paths: tuple[Path, Path, Path],
    capsys: pytest.CaptureFixture[str],
) -> None:
    _install_fake(monkeypatch, _make_fake_report())
    rc = main(_run_args(cli_paths))
    assert rc == 0
    out = capsys.readouterr().out
    assert "Form 67" in out
    assert "UNITED STATES OF AMERICA" in out
    assert "UNITED KINGDOM" in out
    # Source-of-income enum VALUE (not member name) — pinned via the
    # enum to catch silent value renames (T4 r0 Nit 1 of PR #228).
    assert Form67SourceOfIncome.DIVIDEND.value in out
    # Two-decimal rate rendering.
    assert "15.00%" in out


def test_form67_verbose_render_includes_total_income_and_tax(
    monkeypatch: pytest.MonkeyPatch,
    cli_paths: tuple[Path, Path, Path],
    capsys: pytest.CaptureFixture[str],
) -> None:
    _install_fake(monkeypatch, _make_fake_report())
    rc = main(_run_args(cli_paths))
    assert rc == 0
    out = capsys.readouterr().out
    # The rows' filed whole rupees, summed: 12346 + 4113 and 1851 + 617 (#898).
    assert "Total income from outside India (portal-displayed): INR 16459" in out
    assert "Total foreign tax paid (portal-displayed): INR 2468" in out
    assert "Total foreign tax paid (full precision): INR 2468.11" in out


def test_form67_report_and_cli_use_filed_values_without_rounding(
    filed_without_rounding,
) -> None:
    report = _make_fake_report()
    filed_without_rounding(
        report.per_entry_rows[0], income_inr=Decimal("10123"), foreign_tax_inr=Decimal("1701")
    )
    assert report.total_income_inr_portal_displayed == Decimal("20246")
    assert report.total_foreign_tax_inr_portal_displayed == Decimal("3402")
    body = cli._render_form67_report(report)
    assert body.count("INR 10123 | INR 1701 |") == 2
    assert "Total income from outside India (portal-displayed): INR 20246" in body
    assert "Total foreign tax paid (portal-displayed): INR 3402" in body
    assert "Total foreign tax paid (full precision): INR 2468.11" in body


def test_form67_verbose_render_relief_blank_when_no_config(
    monkeypatch: pytest.MonkeyPatch,
    cli_paths: tuple[Path, Path, Path],
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The verbose-render summary's relief-columns line mirrors the
    stderr advisory: BLANK variant when no `[form67]` config is
    supplied (issue #261 audit / PR #262 — replaced the legacy
    hardcoded line, whose stale wording the asserts below keep out).
    """
    _install_fake(monkeypatch, _make_fake_report())
    rc = main(_run_args(cli_paths))
    assert rc == 0
    out = capsys.readouterr().out
    assert "Tax payable + DTAA article/rate + FTC credit columns: BLANK" in out
    assert "exact [[form67.tax_payable_overrides]]" in out
    assert "[form67.section_91]" in out
    # Stale wording must NOT reappear.
    assert "NOT COMPUTED" not in out
    assert "v1 skeleton" not in out
    assert "until the #37 step 4/N relief PR ships" not in out


def test_form67_verbose_render_relief_populated_when_config_supplied(
    monkeypatch: pytest.MonkeyPatch,
    cli_paths: tuple[Path, Path, Path],
    capsys: pytest.CaptureFixture[str],
    _fully_populated_row: Form67PerEntryRow,
) -> None:
    """The verbose-render summary's relief-columns line switches to
    the POPULATED variant when any per-entry row has config-
    dependent relief cols (7, 11-13) populated — matches the
    stderr advisory branching. Without this mirror, advisory +
    summary would directly contradict each other (T3 r0 P0 of PR #262).
    """
    _install_fake(
        monkeypatch,
        _make_fake_report(rows_written=1, per_entry_rows=(_fully_populated_row,)),
    )
    rc = main(_run_args(cli_paths))
    assert rc == 0
    out = capsys.readouterr().out
    assert "Tax payable + DTAA article/rate + FTC credit columns: POPULATED" in out
    assert "(cols 7, 9-13)" in out
    assert "col 8 §115JB/JC special-rate still unimplemented" in out
    assert "#235" in out
    # Stale + other-variant wording must NOT appear when populated.
    assert "NOT COMPUTED" not in out
    assert "v1 skeleton" not in out


def test_form67_verbose_render_relief_dtaa_only_when_no_config_but_dtaa_vendored(
    monkeypatch: pytest.MonkeyPatch,
    cli_paths: tuple[Path, Path, Path],
    capsys: pytest.CaptureFixture[str],
    _dtaa_only_row: Form67PerEntryRow,
) -> None:
    """T4 r1 MEDIUM regression test (PR #262): when only cols 9-10
    are populated (DTAA-vendored security + no [form67] config),
    the verbose-render line must emit the DTAA-only variant — NOT
    claim cols 7, 11-13 are populated.
    """
    _install_fake(
        monkeypatch,
        _make_fake_report(rows_written=1, per_entry_rows=(_dtaa_only_row,)),
    )
    rc = main(_run_args(cli_paths))
    assert rc == 0
    out = capsys.readouterr().out
    assert "DTAA article + rate columns: POPULATED (cols 9-10)" in out
    assert "cols 7, 11-13 BLANK" in out
    # Other-variant wording must NOT fire.
    assert "FTC credit columns: POPULATED" not in out
    assert "FTC credit columns: BLANK" not in out
    assert "NOT COMPUTED" not in out
    assert "v1 skeleton" not in out


# ---- Quiet CSV render ------------------------------------------------------


def test_form67_quiet_emits_csv_header_and_one_row_per_entry(
    monkeypatch: pytest.MonkeyPatch,
    cli_paths: tuple[Path, Path, Path],
    capsys: pytest.CaptureFixture[str],
) -> None:
    _install_fake(monkeypatch, _make_fake_report())
    rc = main(_run_args(cli_paths, "--quiet"))
    assert rc == 0
    out = capsys.readouterr().out.strip()
    lines = out.splitlines()
    assert len(lines) == 3  # header + 2 rows
    assert (
        lines[0] == "serial_number,cbdt_country_code,iso_alpha2,country_name_portal,"
        "source_of_income,income_inr,foreign_tax_inr,foreign_tax_rate_percent,event_count"
    )
    # Source-of-income enum VALUE (not member name) referenced via the
    # enum to catch silent value renames (T4 r0 Nit 1 of PR #228).
    dividend = Form67SourceOfIncome.DIVIDEND.value
    # First detail row: serial=1, US, Dividend.
    assert lines[1].startswith(f"1,2,US,UNITED STATES OF AMERICA,{dividend},")
    # Second detail row: serial=2, GB, Dividend.
    assert lines[2].startswith(f"2,44,GB,UNITED KINGDOM,{dividend},")


# ---- Safety warnings -------------------------------------------------------


def test_form67_ay_2027_warning_includes_form_44_renumbering(
    monkeypatch: pytest.MonkeyPatch,
    cli_paths: tuple[Path, Path, Path],
    capsys: pytest.CaptureFixture[str],
) -> None:
    """AY 2027-28+ runs must surface the Form 67 → Form 44 renumbering
    warning per the CBDT Form 44/45 FAQ (deferred to epic #126)."""
    _install_fake(
        monkeypatch,
        _make_fake_report(
            fx_policy_appropriate_for_ay=False,
            wht_fx_policy_appropriate_for_ay=False,
            portal_labels_assessment_year_start=2027,
            portal_labels_used_forward_compat_fallback=True,
        ),
    )
    rc = main(_run_args(cli_paths))
    assert rc == 0
    err = capsys.readouterr().err
    assert "AY 2027-28 or later" in err
    assert "Form 44" in err
    assert "Rule 76" in err
    assert "#126" in err


def test_form67_form_44_warning_fires_even_when_fx_policy_verified(
    monkeypatch: pytest.MonkeyPatch,
    cli_paths: tuple[Path, Path, Path],
    capsys: pytest.CaptureFixture[str],
) -> None:
    """T3 r2 P2 of PR #228: the Form 44 / Rule 76 form-name renumbering
    warning is INDEPENDENT of the cross-AY FX-policy warning. It must
    fire whenever `portal_labels_assessment_year_start >= 2027` AND the
    v1 writer is still emitting Form 67-labelled output, regardless of
    whether `fx_policy_appropriate_for_ay` / `wht_fx_policy_appropriate
    _for_ay` are True (verified) or False (unverified).

    The r2 implementation coupled the two warnings into a single
    conditional, so verifying FX policy or passing --suppress-cross-
    ay-warning would silence Form 44 too — even though Form 44 is
    about the form NAME, not the FX policy. r3 separates them.
    """
    _install_fake(
        monkeypatch,
        _make_fake_report(
            # FX-policy fields VERIFIED (True) — the cross-AY FX warning
            # must NOT fire.
            fx_policy_appropriate_for_ay=True,
            wht_fx_policy_appropriate_for_ay=True,
            # AY 2027-28: Form 67 → Form 44 renumbering applies.
            portal_labels_assessment_year_start=2027,
            portal_labels_used_forward_compat_fallback=True,
        ),
    )
    rc = main(_run_args(cli_paths))
    assert rc == 0
    err = capsys.readouterr().err
    # FX-policy warning is NOT present (FX policy is verified).
    assert "Rule 206(2)(b)(i)" not in err
    # Form 44 / Rule 76 warning IS present (renumbering is a separate concern).
    assert "Form 44" in err
    assert "Rule 76" in err
    assert "renumbering" in err
    assert "#126" in err
    # The advisory explicitly notes it's INDEPENDENT of the FX warning.
    assert "INDEPENDENT" in err


def test_form67_form_44_warning_does_not_fire_for_ay_2026_27(
    monkeypatch: pytest.MonkeyPatch,
    cli_paths: tuple[Path, Path, Path],
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The Form 44 warning fires only when portal_labels_assessment_year
    _start >= 2027. For AY 2026-27 (the current registered snapshot),
    the warning must NOT fire."""
    _install_fake(
        monkeypatch,
        _make_fake_report(
            fx_policy_appropriate_for_ay=True,
            wht_fx_policy_appropriate_for_ay=True,
            portal_labels_assessment_year_start=2026,
            portal_labels_used_forward_compat_fallback=False,
        ),
    )
    rc = main(_run_args(cli_paths))
    assert rc == 0
    err = capsys.readouterr().err
    # Form 44 warning is NOT present for AY 2026-27.
    assert "Form 44" not in err
    assert "Rule 76" not in err


def test_form67_relief_blank_advisory_fires_when_no_config(
    monkeypatch: pytest.MonkeyPatch,
    cli_paths: tuple[Path, Path, Path],
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The fake report's per-entry rows leave relief cols 7 and 9-13
    None (in a real run a DTAA country's cols 9-10 come from the DTAA
    table regardless of config; see ``_dtaa_only_row``), so the
    advisory directs the user to either supply config or fill the
    portal manually. (PR #262 replaced the legacy wording, which the
    asserts below keep out, once the relief PRs had shipped.)
    """
    _install_fake(monkeypatch, _make_fake_report())
    rc = main(_run_args(cli_paths))
    assert rc == 0
    err = capsys.readouterr().err
    # Refreshed wording: relief cols left blank without [form67] config.
    assert "Form 67 relief columns (7-13) left blank" in err
    assert "[[form67.tax_payable_overrides]]" in err
    assert "[form67.section_91]" in err
    # Stale wording must NOT reappear.
    assert "v1 skeleton" not in err
    assert "until the #37 step 4/N relief PR ships" not in err


@pytest.fixture
def _fully_populated_row() -> Form67PerEntryRow:
    """Per-entry row with cols 7, 9-11 and 13 populated (col 12, the
    §91 credit, stays None for a DTAA country) — represents a user
    supplying ``[form67]`` config + holding a DTAA-vendored security
    with exact tax-payable evidence.
    """
    return Form67PerEntryRow(
        serial_number=1,
        cbdt_country_code="2",
        country_name_portal="UNITED STATES OF AMERICA",
        iso_alpha2="US",
        source_of_income=Form67SourceOfIncome.DIVIDEND,
        income_inr=Decimal("12345.67"),
        foreign_tax_inr=Decimal("1851.23"),
        foreign_tax_rate_percent=Decimal("15.00"),
        event_count=3,
        tax_payable_normal_inr=Decimal("3851.85"),
        tax_payable_special_inr=None,
        section_90_dtaa_article="Article 10(2)(b)",
        section_90_dtaa_rate_percent=Decimal("25"),
        section_90_credit_inr=Decimal("1851.23"),
        section_91_credit_inr=None,
        total_credit_inr=Decimal("1851.23"),
        tax_paid_rate_percent=Decimal("15.00"),
        highest_payment_rate_percent=Decimal("15.00"),
        eligible_foreign_tax_inr=Decimal("1851.23"),
        treaty_rate_status=Form67TreatyRateStatus.WITHIN,
        readiness=_READY,
    )


@pytest.fixture
def _dtaa_only_row() -> Form67PerEntryRow:
    """Per-entry row with cols 9-10 populated from DTAA-vendoring
    but cols 7, 11-13 all None — represents the DEFAULT no-config
    state for a user holding DTAA-vendored securities (typical
    case for US/UK/FR/DE/JP IBKR users who haven't set
    ``[form67]`` config in user.toml).

    T4 r1 MEDIUM finding of PR #262 surfaced this — predicate must
    distinguish this DTAA-only intermediate state from the
    fully-populated state, since cols 7, 11-13 require `[form67]`
    config (col 7 directly; cols 11 and 13 through the §90 credit's
    col 7 gate in ``_resolve_section_90_relief``).
    """
    return Form67PerEntryRow(
        serial_number=1,
        cbdt_country_code="2",
        country_name_portal="UNITED STATES OF AMERICA",
        iso_alpha2="US",
        source_of_income=Form67SourceOfIncome.DIVIDEND,
        income_inr=Decimal("12345.67"),
        foreign_tax_inr=Decimal("1851.23"),
        foreign_tax_rate_percent=Decimal("15.00"),
        event_count=3,
        # Cols 9-10 populated from DTAA vendoring (no config needed).
        section_90_dtaa_article="Article 10(2)(b)",
        section_90_dtaa_rate_percent=Decimal("25"),
        # Cols 7, 11-13 require [form67] config — None for this user.
        tax_payable_normal_inr=None,
        tax_payable_special_inr=None,
        section_90_credit_inr=None,
        section_91_credit_inr=None,
        total_credit_inr=None,
        tax_paid_rate_percent=Decimal("15.00"),
        highest_payment_rate_percent=Decimal("15.00"),
        eligible_foreign_tax_inr=Decimal("1851.23"),
        treaty_rate_status=Form67TreatyRateStatus.WITHIN,
        readiness=_BLANK,
    )


def test_form67_relief_populated_advisory_when_config_supplied(
    monkeypatch: pytest.MonkeyPatch,
    cli_paths: tuple[Path, Path, Path],
    capsys: pytest.CaptureFixture[str],
    _fully_populated_row: Form67PerEntryRow,
) -> None:
    """When at least one per-entry row has tax_payable_normal_inr
    (or section_90/91_credit_inr / total_credit_inr) populated —
    the typical "user supplied [form67] config" case — the advisory
    switches to the "fully populated" variant (which also notes
    col 8 is still unimplemented per #235).
    """
    _install_fake(
        monkeypatch,
        _make_fake_report(rows_written=1, per_entry_rows=(_fully_populated_row,)),
    )
    rc = main(_run_args(cli_paths))
    assert rc == 0
    err = capsys.readouterr().err
    assert "Form 67 relief columns (7, 9-13) populated" in err
    assert "Verify the rendered" in err
    # Col 8 deferral is noted.
    assert "Col 8" in err
    assert "#235" in err
    # Other-variant wording must NOT fire when fully populated.
    assert "left blank" not in err
    assert "DTAA article + rate (cols 9-10) populated" not in err
    assert "v1 skeleton" not in err
    # The config-hint snippet must NOT nag a user who already configured.
    assert "replace with YOUR rate" not in err


def test_form67_quiet_mode_still_warns_for_missing_indian_tax(
    monkeypatch: pytest.MonkeyPatch,
    cli_paths: tuple[Path, Path, Path],
    capsys: pytest.CaptureFixture[str],
    _fully_populated_row: Form67PerEntryRow,
) -> None:
    incomplete_row = replace(
        _fully_populated_row,
        tax_payable_normal_inr=None,
        section_90_credit_inr=None,
        total_credit_inr=None,
        readiness=_BLANK,
    )
    _install_fake(
        monkeypatch,
        _make_fake_report(rows_written=1, per_entry_rows=(incomplete_row,)),
    )
    assert main(_run_args(cli_paths, "--quiet")) == 0
    captured = capsys.readouterr()
    assert "INDIAN TAX CALCULATION REQUIRED" in captured.err
    assert "average Indian tax rate" in captured.err


def test_form67_dtaa_only_advisory_when_no_config_but_dtaa_vendored(
    monkeypatch: pytest.MonkeyPatch,
    cli_paths: tuple[Path, Path, Path],
    capsys: pytest.CaptureFixture[str],
    _dtaa_only_row: Form67PerEntryRow,
) -> None:
    """T4 r1 MEDIUM regression test (PR #262): when the user holds
    a DTAA-vendored security (e.g., US dividend) without supplying
    `[form67]` config, cols 9-10 populate from DTAA vendoring but
    cols 7, 11-13 remain None. The advisory MUST distinguish this
    state from "fully populated" — claiming "cols 7, 11-13
    populated" for this user would mislead them.
    """
    _install_fake(
        monkeypatch,
        _make_fake_report(rows_written=1, per_entry_rows=(_dtaa_only_row,)),
    )
    rc = main(_run_args(cli_paths))
    assert rc == 0
    err = capsys.readouterr().err
    # Dtaa-only variant fires.
    assert "DTAA article + rate (cols 9-10) populated" in err
    assert "cols 7 and 11-13 remain blank" in err
    assert "exact Indian tax attributable" in err
    # Other-variant wording must NOT fire.
    assert "Form 67 relief columns (7, 9-13) populated" not in err
    assert "Form 67 relief columns (7-13) left blank" not in err
    assert "v1 skeleton" not in err


# ---- Entries without a credit (#791) ---------------------------------------


def _zero_credit_row(row: Form67PerEntryRow) -> Form67PerEntryRow:
    return replace(
        row,
        tax_payable_normal_inr=Decimal("0"),
        section_90_credit_inr=Decimal("0"),
        total_credit_inr=Decimal("0"),
        readiness=Form67RowReadiness(Form67Readiness.REFUSED, Form67RefusalReason.ZERO_CREDIT),
    )


def _uncredited_row(row: Form67PerEntryRow) -> Form67PerEntryRow:
    """A Canadian entry with col 7 but no credit, REFUSED for its unproven
    source-currency rate. Until #1491 it was the INCOMPLETE fallback, which no
    compute row reaches."""
    return replace(
        row,
        serial_number=2,
        cbdt_country_code="1",
        country_name_portal="CANADA",
        iso_alpha2="CA",
        treaty_rate_status=Form67TreatyRateStatus.UNPROVEN,
        tax_paid_rate_percent=None,
        highest_payment_rate_percent=None,
        eligible_foreign_tax_inr=None,
        section_90_credit_inr=None,
        total_credit_inr=None,
        readiness=Form67RowReadiness(
            Form67Readiness.REFUSED, Form67RefusalReason.SOURCE_RATE_UNPROVEN
        ),
    )


def _blank_gb_row(row: Form67PerEntryRow) -> Form67PerEntryRow:
    return replace(
        row,
        serial_number=3,
        cbdt_country_code="44",
        country_name_portal="UNITED KINGDOM",
        iso_alpha2="GB",
    )


def test_form67_relief_classification_follows_each_row_readiness(
    _fully_populated_row: Form67PerEntryRow, _dtaa_only_row: Form67PerEntryRow
) -> None:
    ready = _fully_populated_row
    zero = _zero_credit_row(ready)
    uncredited = _uncredited_row(ready)
    blank = _blank_gb_row(_dtaa_only_row)
    classify = cli._classify_form67_relief_population
    assert classify((ready, zero)) == "fully_populated"
    assert classify((zero,)) == "fully_populated"
    assert classify((ready, uncredited)) == "partially_populated"
    assert classify((uncredited,)) == "partially_populated"
    assert classify((ready, blank)) == "partially_populated"
    assert classify((zero, blank)) == "partially_populated"
    assert classify((blank,)) == "dtaa_only"
    assert classify(()) == "all_blank"


def test_form67_partial_relief_lists_each_entry_without_a_credit(
    monkeypatch: pytest.MonkeyPatch,
    cli_paths: tuple[Path, Path, Path],
    capsys: pytest.CaptureFixture[str],
    _fully_populated_row: Form67PerEntryRow,
    _dtaa_only_row: Form67PerEntryRow,
) -> None:
    rows = (
        _fully_populated_row,
        _uncredited_row(_fully_populated_row),
        _blank_gb_row(_dtaa_only_row),
    )
    _install_fake(monkeypatch, _make_fake_report(rows_written=3, per_entry_rows=rows))
    assert main(_run_args(cli_paths)) == 0
    captured = capsys.readouterr()
    ca_line = no_credit_line("CA/DIVIDEND", "source-currency rate not established")
    assert (
        "  Tax payable + DTAA article/rate + FTC credit columns: PARTIALLY POPULATED\n"
        f"    {ca_line}\n"
        "    GB/DIVIDEND: Indian tax payable and credit blank; complete both by hand\n"
        "    Indian tax payable and credit are filled for every other entry (col 8 "
        "§115JB/JC special-rate still unimplemented per #235).\n"
        "    The official portal-csv output refuses an entry with Indian tax payable "
        "but no credit, and is withheld while any entry's Indian tax payable and credit "
        "are blank.\n"
    ) in captured.out
    assert (
        "⚠️  Form 67 relief columns are PARTIALLY POPULATED:\n"
        f"     - {ca_line}\n"
        "     - GB/DIVIDEND: Indian tax payable and credit blank; complete both by hand\n"
        "   Indian tax payable and credit are filled for every other entry. Verify them "
        "against your CA's expectations before filing. Col 8 (§115JB/JC special-rate "
        "tax payable) is still unimplemented (#235); fill it manually if applicable.\n"
        "   The official portal-csv output refuses an entry with Indian tax payable "
        "but no credit, and is withheld while any entry's Indian tax payable and credit "
        "are blank.\n"
    ) in captured.err


def test_payment_allocation_unproven_row_prints_its_short_reason(
    monkeypatch: pytest.MonkeyPatch,
    cli_paths: tuple[Path, Path, Path],
    capsys: pytest.CaptureFixture[str],
    _fully_populated_row: Form67PerEntryRow,
) -> None:
    """#890: a standalone review-csv run writes REFUSED rows, so the new
    reason prints its short text rather than raising."""
    row = replace(
        _fully_populated_row,
        treaty_rate_status=Form67TreatyRateStatus.UNPROVEN,
        payment_allocation_unproven=True,
        highest_payment_rate_percent=Decimal("30.04"),
        eligible_foreign_tax_inr=None,
        section_90_credit_inr=None,
        total_credit_inr=None,
        readiness=Form67RowReadiness(
            Form67Readiness.REFUSED, Form67RefusalReason.PAYMENT_ALLOCATION_UNPROVEN
        ),
    )
    _install_fake(monkeypatch, _make_fake_report(rows_written=1, per_entry_rows=(row,)))
    assert main(_run_args(cli_paths, "--format", "review-csv")) == 0
    captured = capsys.readouterr()
    for stream in (captured.out, captured.err):
        assert no_credit_line("US/DIVIDEND", "withholding not attributable per payment") in stream


def test_form67_every_uncredited_refusal_has_a_short_reason() -> None:
    assert set(cli._FORM67_UNCREDITED_REASONS) == set(Form67RefusalReason) - {
        Form67RefusalReason.ZERO_CREDIT
    }


def test_form67_withheld_output_name_does_not_describe_every_entry() -> None:
    """An aggregate BLANK readiness can include READY entries."""
    name = cli._form67_output_name(Form67Readiness.BLANK)
    assert "its Indian tax payable and credit are blank" not in name
    assert "one or more entries lack Indian tax payable and credit" in name


def test_form67_zero_rows_advisory(
    monkeypatch: pytest.MonkeyPatch,
    cli_paths: tuple[Path, Path, Path],
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Zero-row runs surface a "no qualifying Form 67 entries"
    advisory pointing at the upstream schedule-fsi for diagnosis."""
    _install_fake(
        monkeypatch,
        _make_fake_report(
            rows_written=0,
            per_entry_rows=(),
            total_foreign_tax_inr_full_precision=Decimal("0"),
        ),
    )
    rc = main(_run_args(cli_paths))
    assert rc == 0
    err = capsys.readouterr().err
    assert "No qualifying Form 67 entries" in err
    assert "schedule-fsi" in err


def test_form67_underlying_issuer_routing_advisory(
    monkeypatch: pytest.MonkeyPatch,
    cli_paths: tuple[Path, Path, Path],
    capsys: pytest.CaptureFixture[str],
) -> None:
    """UNDERLYING_ISSUER routing mode surfaces the #225 ADR WHT
    mis-routing concern."""
    _install_fake(
        monkeypatch,
        _make_fake_report(adr_routing_mode=ScheduleOsIndianAdrRoutingMode.UNDERLYING_ISSUER),
    )
    rc = main(_run_args(cli_paths))
    assert rc == 0
    err = capsys.readouterr().err
    assert "UNDERLYING_ISSUER" in err
    assert "#225" in err


def test_form67_report_refuses_a_skipped_cross_check_for_a_mask_with_a_line_break() -> None:
    """#854: ``"U***\\n"`` is not IBKR's mask, so it can't justify a
    skipped cross-check."""
    with pytest.raises(InvariantError, match="bypassed without justification"):
        _make_fake_report(account_id="U***\n", account_id_cross_checked=False)


def test_form67_masked_account_advisory(
    monkeypatch: pytest.MonkeyPatch,
    cli_paths: tuple[Path, Path, Path],
    capsys: pytest.CaptureFixture[str],
) -> None:
    """When account_id_cross_checked is False, the masked-account
    warning fires (this is the consequence of --allow-masked-account)."""
    _install_fake(
        monkeypatch,
        _make_fake_report(account_id="U********", account_id_cross_checked=False),
    )
    rc = main(_run_args(cli_paths, "--allow-masked-account"))
    assert rc == 0
    err = capsys.readouterr().err
    assert "Account ID cross-check was SKIPPED" in err


# ---- DTAA over-treaty-cap excess advisory (e.g. 25.40% > 25%) ---------------


def _over_treaty_row(
    *,
    income_inr: Decimal,
    foreign_tax_inr: Decimal,
    foreign_tax_rate_percent: Decimal,
    dtaa_rate_percent: Decimal | None,
    country: str = "UNITED STATES OF AMERICA",
    cbdt_country_code: str = "2",
    iso_alpha2: str = "US",
    serial_number: int = 1,
    section_90_credit_inr: Decimal | None = None,
    tax_payable_normal_inr: Decimal | None = None,
    tax_paid_rate_percent: Decimal | None = None,
    highest_payment_rate_percent: Decimal | None = None,
    eligible_foreign_tax_inr: Decimal | None = None,
) -> Form67PerEntryRow:
    """A per-entry row parameterised on the rate/cap relationship.

    The source rate defaults to the INR ratio, and the highest per-payment
    rate to the source rate. With a DTAA rate, the row gets the status compute
    would record from the highest per-payment rate and, unless given, its
    eligible tax. It is READY with a credit and BLANK without one.
    """
    paid_percent = (
        foreign_tax_rate_percent if tax_paid_rate_percent is None else tax_paid_rate_percent
    )
    highest_percent = (
        paid_percent if highest_payment_rate_percent is None else highest_payment_rate_percent
    )
    status = None
    if dtaa_rate_percent is not None:
        if highest_percent <= dtaa_rate_percent:
            status = Form67TreatyRateStatus.WITHIN
            default_eligible = foreign_tax_inr
        else:
            status = Form67TreatyRateStatus.ABOVE
            default_eligible = min(foreign_tax_inr, dtaa_rate_percent / Decimal("100") * income_inr)
        if eligible_foreign_tax_inr is None:
            eligible_foreign_tax_inr = default_eligible
    return Form67PerEntryRow(
        serial_number=serial_number,
        cbdt_country_code=cbdt_country_code,
        country_name_portal=country,
        iso_alpha2=iso_alpha2,
        source_of_income=Form67SourceOfIncome.DIVIDEND,
        income_inr=income_inr,
        foreign_tax_inr=foreign_tax_inr,
        foreign_tax_rate_percent=foreign_tax_rate_percent,
        event_count=1,
        tax_payable_normal_inr=tax_payable_normal_inr,
        tax_payable_special_inr=None,
        section_90_dtaa_article="Article 10(2)(b)" if dtaa_rate_percent is not None else None,
        section_90_dtaa_rate_percent=dtaa_rate_percent,
        section_90_credit_inr=section_90_credit_inr,
        section_91_credit_inr=None,
        total_credit_inr=section_90_credit_inr,
        tax_paid_rate_percent=paid_percent,
        highest_payment_rate_percent=None if status is None else highest_percent,
        eligible_foreign_tax_inr=eligible_foreign_tax_inr,
        treaty_rate_status=status,
        readiness=_BLANK if section_90_credit_inr is None else _READY,
    )


def test_form67_over_treaty_advisory_fires_when_wht_exceeds_cap(
    monkeypatch: pytest.MonkeyPatch,
    cli_paths: tuple[Path, Path, Path],
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Effective WHT 30% > DTAA cap 25% → advisory fires, lists the
    non-creditable excess and the source-country-refund remedy."""
    row = _over_treaty_row(
        income_inr=Decimal("100"),
        foreign_tax_inr=Decimal("30"),
        foreign_tax_rate_percent=Decimal("30.00"),
        dtaa_rate_percent=Decimal("25"),
    )
    _install_fake(monkeypatch, _make_fake_report(rows_written=1, per_entry_rows=(row,)))
    rc = main(_run_args(cli_paths))
    assert rc == 0
    err = capsys.readouterr().err
    assert "appears to exceed the DTAA treaty cap" in err
    assert "source-currency withholding 30.00% exceeds the DTAA cap 25.00%" in err
    assert "conservative legacy INR cap" in err
    assert "SOURCE-country tax authority" in err
    assert "not creditable" in err.lower()


def test_over_treaty_advisory_renders_the_filed_cap_without_rounding(
    filed_without_rounding,
) -> None:
    row = _over_treaty_row(
        income_inr=Decimal("100"),
        foreign_tax_inr=Decimal("30"),
        foreign_tax_rate_percent=Decimal("30"),
        dtaa_rate_percent=Decimal("25"),
    )
    filed_without_rounding(row, section_90_dtaa_rate_percent=Decimal("25.25"))
    body = "\n".join(cli._form67_over_treaty_excess_lines((row,)))
    assert "withholding 30.00% exceeds the DTAA cap 25.25%" in body


def test_form67_over_treaty_advisory_fires_for_small_source_excess(
    monkeypatch: pytest.MonkeyPatch,
    cli_paths: tuple[Path, Path, Path],
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A genuine small source-rate excess remains visible."""
    row = _over_treaty_row(
        income_inr=Decimal("90.00"),
        foreign_tax_inr=Decimal("22.86"),  # 22.86 / 90.00 = 25.40%
        foreign_tax_rate_percent=Decimal("25.40"),
        dtaa_rate_percent=Decimal("25"),
    )
    _install_fake(monkeypatch, _make_fake_report(rows_written=1, per_entry_rows=(row,)))
    rc = main(_run_args(cli_paths))
    assert rc == 0
    err = capsys.readouterr().err
    assert "source-currency withholding 25.40% exceeds the DTAA cap 25.00%" in err


def test_form67_over_treaty_advisory_ignores_inr_fx_ratio_artifact() -> None:
    row = _over_treaty_row(
        income_inr=Decimal("90.00"),
        foreign_tax_inr=Decimal("22.86"),
        foreign_tax_rate_percent=Decimal("25.40"),
        tax_paid_rate_percent=Decimal("25"),
        dtaa_rate_percent=Decimal("25"),
    )

    assert cli._form67_over_treaty_excess_lines((row,)) == []


def test_form67_over_treaty_advisory_silent_when_rate_equals_cap(
    monkeypatch: pytest.MonkeyPatch,
    cli_paths: tuple[Path, Path, Path],
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Effective WHT exactly at the treaty cap → no over-withholding,
    no advisory."""
    row = _over_treaty_row(
        income_inr=Decimal("100"),
        foreign_tax_inr=Decimal("25"),
        foreign_tax_rate_percent=Decimal("25.00"),
        dtaa_rate_percent=Decimal("25"),
    )
    _install_fake(monkeypatch, _make_fake_report(rows_written=1, per_entry_rows=(row,)))
    rc = main(_run_args(cli_paths))
    assert rc == 0
    err = capsys.readouterr().err
    assert "appears to exceed the DTAA treaty cap" not in err


def test_form67_over_treaty_advisory_silent_when_no_dtaa_cap(
    monkeypatch: pytest.MonkeyPatch,
    cli_paths: tuple[Path, Path, Path],
    capsys: pytest.CaptureFixture[str],
) -> None:
    """No vendored DTAA rate (§91 / non-DTAA path) → no treaty cap to
    breach, so the advisory stays silent even at a high effective rate."""
    row = _over_treaty_row(
        income_inr=Decimal("100"),
        foreign_tax_inr=Decimal("40"),
        foreign_tax_rate_percent=Decimal("40.00"),
        dtaa_rate_percent=None,
    )
    _install_fake(monkeypatch, _make_fake_report(rows_written=1, per_entry_rows=(row,)))
    rc = main(_run_args(cli_paths))
    assert rc == 0
    err = capsys.readouterr().err
    assert "appears to exceed the DTAA treaty cap" not in err


def test_form67_over_treaty_advisory_lists_only_breaching_entries() -> None:
    """With a mix, only the over-cap entry is listed (direct unit test)."""
    over = _over_treaty_row(
        income_inr=Decimal("100"),
        foreign_tax_inr=Decimal("30"),
        foreign_tax_rate_percent=Decimal("30.00"),
        dtaa_rate_percent=Decimal("25"),
        country="UNITED STATES OF AMERICA",
    )
    at_cap = _over_treaty_row(
        income_inr=Decimal("100"),
        foreign_tax_inr=Decimal("15"),
        foreign_tax_rate_percent=Decimal("15.00"),
        dtaa_rate_percent=Decimal("15"),
        country="UNITED KINGDOM",
        cbdt_country_code="44",
        iso_alpha2="GB",
        serial_number=2,
    )
    lines = cli._form67_over_treaty_excess_lines((over, at_cap))
    body = "\n".join(lines)
    assert "UNITED STATES OF AMERICA" in body
    assert "UNITED KINGDOM" not in body


def test_form67_over_treaty_advisory_skips_unproven_rows() -> None:
    """An UNPROVEN row has no automatic credit, so it isn't listed even when
    its INR ratio is above the cap (#767)."""
    above = _over_treaty_row(
        income_inr=Decimal("100"),
        foreign_tax_inr=Decimal("30"),
        foreign_tax_rate_percent=Decimal("30.00"),
        dtaa_rate_percent=Decimal("25"),
    )
    unproven = replace(
        _over_treaty_row(
            income_inr=Decimal("100"),
            foreign_tax_inr=Decimal("40"),
            foreign_tax_rate_percent=Decimal("40.00"),
            dtaa_rate_percent=Decimal("25"),
            country="CANADA",
            cbdt_country_code="1",
            iso_alpha2="CA",
            serial_number=2,
        ),
        tax_paid_rate_percent=None,
        highest_payment_rate_percent=None,
        eligible_foreign_tax_inr=None,
        treaty_rate_status=Form67TreatyRateStatus.UNPROVEN,
    )
    body = "\n".join(cli._form67_over_treaty_excess_lines((above, unproven)))
    assert "UNITED STATES OF AMERICA" in body
    assert "CANADA" not in body


def test_over_treaty_advisory_prints_highest_payment_rate() -> None:
    """#405 E9b (#890): the advisory names the 30.04% payment, not the
    country's 14.33% average."""
    income_inr = Decimal("1665.61")
    foreign_tax_inr = Decimal("233.174")
    row = _over_treaty_row(
        income_inr=income_inr,
        foreign_tax_inr=foreign_tax_inr,
        foreign_tax_rate_percent=(foreign_tax_inr / income_inr) * Decimal("100"),
        tax_paid_rate_percent=(Decimal("2.74") / Decimal("19.12")) * Decimal("100"),
        highest_payment_rate_percent=(Decimal("2.74") / Decimal("9.12")) * Decimal("100"),
        dtaa_rate_percent=Decimal("25"),
        eligible_foreign_tax_inr=Decimal("194.028"),
    )
    body = "\n".join(cli._form67_over_treaty_excess_lines((row,)))
    assert (
        "UNITED STATES OF AMERICA: highest per-payment source-currency withholding 30.04% "
        "exceeds the DTAA cap 25.00%"
    ) in body
    assert "14.33" not in body


@pytest.mark.parametrize(
    ("highest", "cap", "rate_text", "cap_text"),
    [
        pytest.param("25.0000001", "25", "25.0000001", "25.00", id="barely"),
        pytest.param("25.254", "25.25", "25.254", "25.25", id="cap-25.25"),
        pytest.param("25.0049", "25", "25.004", "25.00", id="rounds-down"),
    ],
)
def test_over_treaty_advisory_rate_reads_above_the_cap(
    highest: str, cap: str, rate_text: str, cap_text: str
) -> None:
    """#1467: a payment rate whose two-place text doesn't read above the DTAA
    cap shows the fewest further places, rounded down, that do. The shown rate
    never exceeds the exact rate."""
    row = _over_treaty_row(
        income_inr=Decimal("100"),
        foreign_tax_inr=Decimal("25"),
        foreign_tax_rate_percent=Decimal("25"),
        highest_payment_rate_percent=Decimal(highest),
        dtaa_rate_percent=Decimal(cap),
    )
    body = "\n".join(cli._form67_over_treaty_excess_lines((row,)))
    assert f"withholding {rate_text}% exceeds the DTAA cap {cap_text}%;" in body
    assert Decimal(cap_text) < Decimal(rate_text) <= Decimal(highest)


def test_over_treaty_advisory_keeps_a_two_place_rate_above_the_cap() -> None:
    """#1467 control: 25.006% rounds half up to 25.01%, which already reads
    above the 25.00% cap, so the line prints it as before, not 25.006%."""
    row = _over_treaty_row(
        income_inr=Decimal("100"),
        foreign_tax_inr=Decimal("25"),
        foreign_tax_rate_percent=Decimal("25"),
        highest_payment_rate_percent=Decimal("25.006"),
        dtaa_rate_percent=Decimal("25"),
    )
    body = "\n".join(cli._form67_over_treaty_excess_lines((row,)))
    assert "source-currency withholding 25.01% exceeds the DTAA cap 25.00%;" in body


def test_form67_over_treaty_advisory_empty_when_no_rows() -> None:
    assert cli._form67_over_treaty_excess_lines(()) == []


# ---- Indian-tax-cap FTC-shortfall advisory --------------------------------


def test_form67_indian_cap_advisory_fires_when_indian_tax_is_the_binding_cap(
    monkeypatch: pytest.MonkeyPatch,
    cli_paths: tuple[Path, Path, Path],
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Effective foreign rate == treaty cap (so NO over-treaty excess), but
    the Indian tax payable (credit ₹50) is well below the ₹250 treaty-
    eligible foreign tax → the Indian-tax cap binds and ₹200 is unrelieved."""
    row = _over_treaty_row(
        income_inr=Decimal("1000"),
        foreign_tax_inr=Decimal("250"),
        foreign_tax_rate_percent=Decimal("25.00"),  # == cap → no DTAA-excess advisory
        dtaa_rate_percent=Decimal("25"),
        eligible_foreign_tax_inr=Decimal("250"),
        section_90_credit_inr=Decimal("50"),
        tax_payable_normal_inr=Decimal("50"),
    )
    _install_fake(monkeypatch, _make_fake_report(rows_written=1, per_entry_rows=(row,)))
    rc = main(_run_args(cli_paths))
    assert rc == 0
    err = capsys.readouterr().err
    assert "Indian-tax cap is the binding limit" in err
    assert "₹250.00 treaty-eligible foreign tax" in err
    assert "₹50.00 is creditable" in err
    assert "₹200.00 unrelieved" in err
    assert "exact Indian tax payable" in err
    # This is NOT the over-treaty case (rate == cap), so that advisory stays silent.
    assert "appears to exceed the DTAA treaty cap" not in err


def test_form67_indian_cap_advisory_rounds_amounts_to_paise() -> None:
    row = _over_treaty_row(
        income_inr=Decimal("3599.9176"),
        foreign_tax_inr=Decimal("908.9759"),
        foreign_tax_rate_percent=Decimal("25.2499"),
        tax_paid_rate_percent=Decimal("25"),
        dtaa_rate_percent=Decimal("25"),
        eligible_foreign_tax_inr=Decimal("908.9759"),
        section_90_credit_inr=Decimal("800"),
        tax_payable_normal_inr=Decimal("800"),
    )

    body = "\n".join(cli._form67_indian_cap_shortfall_lines((row,)))

    assert "₹908.98 treaty-eligible foreign tax" in body
    assert "₹108.98 unrelieved" in body


def test_form67_indian_cap_advisory_reads_engine_eligible_tax() -> None:
    """The advisory prints the engine's eligible amount, not a CLI recomputation.

    Re-deriving it from the rate fields would give ₹250 (the INR treaty cap).
    """
    row = _over_treaty_row(
        income_inr=Decimal("1000"),
        foreign_tax_inr=Decimal("250"),
        foreign_tax_rate_percent=Decimal("25.00"),
        tax_paid_rate_percent=Decimal("30"),
        dtaa_rate_percent=Decimal("25"),
        eligible_foreign_tax_inr=Decimal("240"),
        section_90_credit_inr=Decimal("50"),
        tax_payable_normal_inr=Decimal("50"),
    )

    body = "\n".join(cli._form67_indian_cap_shortfall_lines((row,)))

    assert "₹240.00 treaty-eligible foreign tax" in body
    assert "₹190.00 unrelieved" in body


def test_form67_indian_cap_advisory_silent_when_dtaa_cap_binds_only(
    monkeypatch: pytest.MonkeyPatch,
    cli_paths: tuple[Path, Path, Path],
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Typical case: foreign rate 30% > cap 25% (DTAA excess fires), but the
    Indian tax (₹25) equals the eligible foreign tax, so the Indian-tax cap
    does NOT bind → only the over-treaty advisory shows."""
    row = _over_treaty_row(
        income_inr=Decimal("100"),
        foreign_tax_inr=Decimal("30"),
        foreign_tax_rate_percent=Decimal("30.00"),
        dtaa_rate_percent=Decimal("25"),
        eligible_foreign_tax_inr=Decimal("25"),  # min(30, 25% x 100)
        section_90_credit_inr=Decimal("25"),  # == eligible (= dtaa cap 25)
        tax_payable_normal_inr=Decimal("28"),
    )
    _install_fake(monkeypatch, _make_fake_report(rows_written=1, per_entry_rows=(row,)))
    rc = main(_run_args(cli_paths))
    assert rc == 0
    err = capsys.readouterr().err
    assert "appears to exceed the DTAA treaty cap" in err  # DTAA excess fires
    assert "Indian-tax cap is the binding limit" not in err  # this one silent


def test_form67_indian_cap_and_over_treaty_decompose_total_uncredited(
    monkeypatch: pytest.MonkeyPatch,
    cli_paths: tuple[Path, Path, Path],
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Both caps bind: foreign tax ₹300, treaty-eligible ₹250 (excess ₹50 →
    over-treaty advisory), credit ₹50 (Indian cap → shortfall ₹200). Both
    advisories fire and decompose the ₹250 total uncredited."""
    row = _over_treaty_row(
        income_inr=Decimal("1000"),
        foreign_tax_inr=Decimal("300"),
        foreign_tax_rate_percent=Decimal("30.00"),
        dtaa_rate_percent=Decimal("25"),
        eligible_foreign_tax_inr=Decimal("250"),  # min(300, 25% x 1000)
        section_90_credit_inr=Decimal("50"),
        tax_payable_normal_inr=Decimal("50"),
    )
    _install_fake(monkeypatch, _make_fake_report(rows_written=1, per_entry_rows=(row,)))
    rc = main(_run_args(cli_paths))
    assert rc == 0
    err = capsys.readouterr().err
    assert "appears to exceed the DTAA treaty cap" in err  # ₹50 over-treaty
    assert "Indian-tax cap is the binding limit" in err  # ₹200 shortfall
    assert "₹200.00 unrelieved" in err


def test_form67_indian_cap_advisory_silent_without_config() -> None:
    """No [form67] config → section_90_credit_inr is None → no data to
    detect the Indian-tax cap; the advisory stays silent (direct unit)."""
    row = _over_treaty_row(
        income_inr=Decimal("1000"),
        foreign_tax_inr=Decimal("250"),
        foreign_tax_rate_percent=Decimal("25.00"),
        dtaa_rate_percent=Decimal("25"),
        eligible_foreign_tax_inr=Decimal("250"),
        section_90_credit_inr=None,
    )
    assert cli._form67_indian_cap_shortfall_lines((row,)) == []


def test_form67_indian_cap_advisory_silent_sub_paise_shortfall() -> None:
    """A shortfall that rounds below one paisa is treated as negligible."""
    row = _over_treaty_row(
        income_inr=Decimal("100"),
        foreign_tax_inr=Decimal("10"),
        foreign_tax_rate_percent=Decimal("10.00"),
        dtaa_rate_percent=Decimal("10"),
        eligible_foreign_tax_inr=Decimal("10"),  # within the treaty rate: all foreign tax
        section_90_credit_inr=Decimal("9.996"),  # shortfall 0.004 -> < ₹0.01
        tax_payable_normal_inr=Decimal("9.996"),
    )
    assert cli._form67_indian_cap_shortfall_lines((row,)) == []


def test_form67_indian_cap_advisory_empty_when_no_rows() -> None:
    assert cli._form67_indian_cap_shortfall_lines(()) == []


def test_format_form67_inr_2dp_renders_zero_cleanly() -> None:
    assert cli._format_form67_inr_2dp(Decimal("0")) == "₹0.00"
    assert cli._format_form67_inr_2dp(Decimal("200")) == "₹200.00"
    assert cli._format_form67_inr_2dp(Decimal("15.034")) == "₹15.03"


# ---- filed amounts past the CBDT bound (#830) -------------------------------

_WHT_FIXTURE = (
    Path(__file__).parent / "fixtures" / "e2e_activity_statement_schedule_tr_with_wht_fy2025_26.csv"
)
_OVERSIZED_TAX_PAYABLE_TOML = (
    US_PERSON_USER_TOML
    + """
[form67]
[[form67.tax_payable_overrides]]
country = "US"
source = "DIVIDEND"
tax_payable_normal_inr = "100000000000000"
"""
)


def test_tax_payable_past_the_bound_refuses_before_any_writer(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A configured Indian tax payable of ₹1e14 refuses when its entry is built,
    before the review CSV, which has no 14-digit guard of its own, is written."""
    monkeypatch.setattr(
        "ibkr_fa.pipeline_form67.write_output_bundle",
        lambda *_args: pytest.fail("compute must refuse before any writer is built"),
    )
    config = tmp_path / "user.toml"
    config.write_text(_OVERSIZED_TAX_PAYABLE_TOML, encoding="utf-8")
    inputs = sorted(tmp_path.iterdir())

    rc = main(_run_args((_WHT_FIXTURE, config, tmp_path / "form67.csv"), "--format", "review-csv"))

    assert rc == 4
    captured = capsys.readouterr()
    assert (captured.out, captured.err) == (
        "",
        "error: InvariantError: Form67Entry.tax_payable_normal_inr must round to a whole-rupee "
        "amount within ±99,999,999,999,999 (the CBDT 14-digit bound) (context keys: value)\n",
    )
    assert sorted(tmp_path.iterdir()) == inputs
