"""Tests for the ``schedule-os`` CLI subcommand."""

from __future__ import annotations

import argparse
from decimal import Decimal
from functools import partial
from pathlib import Path
from typing import Any

import pytest

from ibkr_fa import cli
from ibkr_fa.cli import _parse_financial_year, main
from ibkr_fa.config import ScheduleOsIndianAdrRoutingMode
from ibkr_fa.exceptions import ParseError
from ibkr_fa.pipeline import FinancialYear
from ibkr_fa.pipeline_os_models import ScheduleOsRunReport
from tests._fiscal_cli import install_fake, run_args, write_cli_paths
from tests._run_report_kwargs import os_report_kwargs


def _make_fake_report(**overrides: Any) -> ScheduleOsRunReport:
    """A report with one row, a ₹1,000 total and ``/tmp/out.csv``, then
    ``overrides``."""
    return ScheduleOsRunReport(
        **os_report_kwargs(
            ordinary_dividend_rows_written=1,
            ordinary_dividend_total_inr_rounded=Decimal("1000"),
            output_path=Path("/tmp/out.csv"),
        )
        | overrides
    )


@pytest.fixture
def cli_paths(tmp_path: Path) -> tuple[Path, Path, Path]:
    return write_cli_paths(tmp_path, "placeholder")


_install_fake = partial(install_fake, pipeline="run_schedule_os")


# ---- argparse layer --------------------------------------------------------


def test_top_level_help_lists_both_subcommands(
    capsys: pytest.CaptureFixture[str],
) -> None:
    with pytest.raises(SystemExit) as exc:
        main(["--help"])
    assert exc.value.code == 0
    out = capsys.readouterr().out
    assert "schedule-fa" in out
    assert "schedule-os" in out


def test_schedule_os_help_runs(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exc:
        main(["schedule-os", "--help"])
    assert exc.value.code == 0
    out = capsys.readouterr().out
    assert "Schedule OS" in out
    assert "1a(i)" in out
    assert "1b(ix)" in out
    assert "Rule 115" in out


def test_schedule_os_requires_input(capsys: pytest.CaptureFixture[str], tmp_path: Path) -> None:
    with pytest.raises(SystemExit) as exc:
        main(
            [
                "schedule-os",
                "--config",
                str(tmp_path / "u.toml"),
                "--output",
                str(tmp_path / "o.csv"),
                "--financial-year",
                "2025-26",
            ]
        )
    assert exc.value.code == 2


def test_schedule_os_requires_financial_year(
    capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    with pytest.raises(SystemExit) as exc:
        main(
            [
                "schedule-os",
                "--input",
                str(tmp_path / "i.csv"),
                "--config",
                str(tmp_path / "u.toml"),
                "--output",
                str(tmp_path / "o.csv"),
            ]
        )
    assert exc.value.code == 2


def test_schedule_os_format_review_csv_accepted(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    cli_paths: tuple[Path, Path, Path],
) -> None:
    """``schedule-os --format review-csv`` is the default and must
    not be rejected when passed explicitly."""
    _install_fake(monkeypatch, _make_fake_report())
    code = main(_run_args(cli_paths, "--format", "review-csv"))
    assert code == 0
    capsys.readouterr()


def test_schedule_os_format_xlsx_accepted(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    cli_paths: tuple[Path, Path, Path],
) -> None:
    """``schedule-os --format xlsx`` selects the XLSX writer added
    by the OS XLSX PR (sister to the FA XLSX writer in #42)."""
    recorder: dict[str, Any] = {}
    _install_fake(monkeypatch, _make_fake_report(), recorder=recorder)
    code = main(_run_args(cli_paths, "--format", "xlsx"))
    assert code == 0
    assert recorder["format_id"] == "xlsx"
    capsys.readouterr()


def test_schedule_os_format_invalid_rejected(
    capsys: pytest.CaptureFixture[str],
    cli_paths: tuple[Path, Path, Path],
) -> None:
    """An unknown ``--format`` value is rejected by argparse with
    exit code 2 (usage error), NOT the pipeline domain-error code
    4. This matches the exit-code policy split shared with
    ``schedule-fa``."""
    with pytest.raises(SystemExit) as exc:
        main(_run_args(cli_paths, "--format", "not-a-format"))
    assert exc.value.code == 2
    assert "argument --format: invalid value" in capsys.readouterr().err.lower()


# ---- _parse_financial_year --------------------------------------------------


def test_financial_year_short_form() -> None:
    fy = _parse_financial_year("2025-26")
    assert fy == FinancialYear(start_year=2025)


def test_financial_year_long_form() -> None:
    fy = _parse_financial_year("2025-2026")
    assert fy == FinancialYear(start_year=2025)


def test_financial_year_short_form_off_by_one_rejected() -> None:

    with pytest.raises(argparse.ArgumentTypeError):
        _parse_financial_year("2025-27")


def test_financial_year_long_form_off_by_one_rejected() -> None:

    with pytest.raises(argparse.ArgumentTypeError):
        _parse_financial_year("2025-2027")


@pytest.mark.parametrize("spec", ["FY2025-26", "2025/26", "2025", "abc"])
def test_financial_year_malformed_rejected(spec: str) -> None:

    with pytest.raises(argparse.ArgumentTypeError):
        _parse_financial_year(spec)


def test_financial_year_too_old_rejected() -> None:

    with pytest.raises(argparse.ArgumentTypeError):
        _parse_financial_year("1899-00")


def test_financial_year_too_far_in_future_rejected() -> None:
    with pytest.raises(argparse.ArgumentTypeError):
        _parse_financial_year("9999-00")


def test_financial_year_century_rollover() -> None:
    fy = _parse_financial_year("2099-00")
    assert fy == FinancialYear(start_year=2099)


# ---- Dispatcher / rendering ------------------------------------------------


_run_args = partial(run_args, ("schedule-os",))


def test_happy_path_zero_dividends(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    cli_paths: tuple[Path, Path, Path],
) -> None:
    _install_fake(
        monkeypatch,
        _make_fake_report(
            ordinary_dividend_rows_written=0,
            ordinary_dividend_total_inr_rounded=Decimal("0"),
        ),
    )
    code = main(_run_args(cli_paths))
    assert code == 0
    captured = capsys.readouterr()
    assert "0 qualifying ordinary dividend" in captured.out
    assert "TOTAL 1a(i) ordinary dividends (rounded ₹): 0" in captured.out
    assert "1a(i)" in captured.err


def test_happy_path_with_dividends(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    cli_paths: tuple[Path, Path, Path],
) -> None:
    _install_fake(
        monkeypatch,
        _make_fake_report(
            ordinary_dividend_rows_written=3,
            ordinary_dividend_total_inr_rounded=Decimal("12345"),
        ),
    )
    code = main(_run_args(cli_paths))
    assert code == 0
    captured = capsys.readouterr()
    out_lines = captured.out.splitlines()
    assert (
        "Wrote Schedule OS review to /tmp/out.csv with 3 qualifying ordinary dividend "
        "payment(s) (3 foreign-source, 0 Indian-source)"
    ) in out_lines
    assert "  TOTAL 1a(i) ordinary dividends (rounded ₹): 12345" in out_lines
    # T1-r9 quality lane PR #159 P2: schedule-os stdout must mask the
    # account ID (positive: masked form is present; negative: raw form
    # never appears). The fake report's account_id is "U1234567"
    # (7 chars), which display_account_id masks to "U***4567".
    assert "U***4567" in captured.out
    assert "U1234567" not in captured.out
    # #63: BMA disclosure advisory always fires on stderr now.
    # Other safety warnings should still be absent.
    assert "Black Money Act disclosure reminder" in captured.err
    assert "Account ID cross-check was SKIPPED" not in captured.err
    assert "AY 2027-28" not in captured.err
    assert "Excluded" not in captured.err


def test_quiet_mode_prints_only_total(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    cli_paths: tuple[Path, Path, Path],
) -> None:
    """Quiet stdout emits 1a(i) dividends then 1b(ix) interest."""
    _install_fake(
        monkeypatch,
        _make_fake_report(
            ordinary_dividend_rows_written=3,
            indian_source_dividend_rows_written=1,
            ordinary_dividend_total_inr_rounded=Decimal("13023"),
        ),
    )
    code = main(_run_args(cli_paths, "--quiet"))
    assert code == 0
    captured = capsys.readouterr()
    assert captured.out == "13023 0\n"
    # #63: BMA disclosure advisory survives --quiet (tax-safety
    # diagnostics are non-quietable by design).
    assert "Black Money Act disclosure reminder" in captured.err


def test_quiet_mode_emits_interest_total_when_present(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    cli_paths: tuple[Path, Path, Path],
) -> None:
    """Quiet mode keeps the 1b(ix) total in field two."""
    _install_fake(
        monkeypatch,
        _make_fake_report(
            ordinary_dividend_rows_written=2,
            ordinary_dividend_total_inr_rounded=Decimal("500"),
            interest_total_inr_rounded=Decimal("369"),
            interest_lines_count=3,
        ),
    )
    code = main(_run_args(cli_paths, "--quiet"))
    assert code == 0
    captured = capsys.readouterr()
    assert captured.out == "500 369\n"


def test_interest_credits_out_of_fy_advisory_renders(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    cli_paths: tuple[Path, Path, Path],
) -> None:
    """T3 codex-emulation P2 finding on PR #188: the
    interest_credits_out_of_fy_count field was documented as
    CLI-surfaced but no renderer emitted it. Without this advisory
    a user whose Activity Statement is a calendar-year export
    spanning multiple FYs would silently drop the out-of-FY
    interest credits from their 1b(ix) aggregate."""
    _install_fake(
        monkeypatch,
        _make_fake_report(
            ordinary_dividend_rows_written=1,
            ordinary_dividend_total_inr_rounded=Decimal("100"),
            interest_lines_count=3,
            interest_total_inr_rounded=Decimal("250"),
            interest_credits_out_of_fy_count=2,
        ),
    )
    code = main(_run_args(cli_paths))
    assert code == 0
    err = capsys.readouterr().err
    assert "Silently filtered 2 InterestCredit" in err
    assert "outside FY" in err
    assert "calendar-year export" in err


def test_quiet_mode_still_emits_safety_warnings_to_stderr(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    cli_paths: tuple[Path, Path, Path],
) -> None:
    _install_fake(
        monkeypatch,
        _make_fake_report(
            ordinary_dividend_rows_written=0,
            ordinary_dividend_total_inr_rounded=Decimal("0"),
        ),
    )
    code = main(_run_args(cli_paths, "--quiet"))
    assert code == 0
    captured = capsys.readouterr()
    assert captured.out == "0 0\n"
    # Stderr no-qualifying advisory still fires (non-quietable).
    assert "No qualifying" in captured.err


def test_cross_ay_warning_renders_on_stderr(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    cli_paths: tuple[Path, Path, Path],
) -> None:
    _install_fake(
        monkeypatch,
        _make_fake_report(fx_policy_appropriate_for_ay=False),
    )
    code = main(_run_args(cli_paths))
    assert code == 0
    err = capsys.readouterr().err
    assert "AY 2027-28 or later" in err
    assert "Rule 206(2)(b)(i)" in err
    assert "#126" in err or "issues/126" in err


def test_masked_account_warning_renders_on_stderr(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    cli_paths: tuple[Path, Path, Path],
) -> None:
    _install_fake(
        monkeypatch,
        # cross_checked=False is only invariant-legal when the source
        # statement's account ID was masked (T1-r10 quality P3:
        # ScheduleOsRunReport mirrors RunReport's safety-net invariant).
        _make_fake_report(
            account_id="U**********",
            account_id_cross_checked=False,
        ),
    )
    code = main(_run_args(cli_paths))
    assert code == 0
    err = capsys.readouterr().err
    assert "Account ID cross-check was SKIPPED" in err


def test_source_counts_and_total_render_on_stdout(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    cli_paths: tuple[Path, Path, Path],
) -> None:
    """Source-country counts and the combined total remain visible."""
    _install_fake(
        monkeypatch,
        _make_fake_report(
            ordinary_dividend_rows_written=2,
            indian_source_dividend_rows_written=1,
            ordinary_dividend_total_inr_rounded=Decimal("250"),
        ),
    )
    code = main(_run_args(cli_paths))
    assert code == 0
    out = capsys.readouterr().out
    assert "  TOTAL 1a(i) ordinary dividends (rounded ₹): 250" in out.splitlines()


def test_source_count_breakdown_surfaces_on_stdout(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    cli_paths: tuple[Path, Path, Path],
) -> None:
    _install_fake(
        monkeypatch,
        _make_fake_report(
            ordinary_dividend_rows_written=3,
            indian_source_dividend_rows_written=2,
            ordinary_dividend_total_inr_rounded=Decimal("12000"),
        ),
    )
    code = main(_run_args(cli_paths))
    assert code == 0
    out = capsys.readouterr().out
    assert "(1 foreign-source, 2 Indian-source)" in out


def test_adr_source_attribution_advisory_always_fires(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    cli_paths: tuple[Path, Path, Path],
) -> None:
    _install_fake(monkeypatch, _make_fake_report())
    code = main(_run_args(cli_paths))
    assert code == 0
    err = capsys.readouterr().err
    assert "ADR / GDR source attribution" in err
    assert "INFY" in err
    assert "underlying-issuer" in err
    assert "1a(i) total unchanged" in err


def test_adr_limitation_advisory_survives_quiet_mode(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    cli_paths: tuple[Path, Path, Path],
) -> None:
    """The ADR-limitation advisory is part of the non-quietable
    safety-warning block; --quiet must not suppress it (same design
    principle as the BMA and ITR-form advisories).
    """
    _install_fake(monkeypatch, _make_fake_report())
    code = main(_run_args(cli_paths, "--quiet"))
    assert code == 0
    err = capsys.readouterr().err
    assert "ADR / GDR source attribution" in err
    assert "INFY" in err


def test_underlying_issuer_mode_zero_reroutes_shows_opt_in_advisory(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    cli_paths: tuple[Path, Path, Path],
) -> None:
    """PR #184 T3 codex P3 anti-regression: a user who opted in to
    ``underlying-issuer`` mode but happens to have zero mapped ADRs
    in this FY must still see the opt-in (positive) advisory — NOT
    the depositary-mode advisory that tells them to opt in (which
    they already did). The CLI advisory dispatches on
    ``ScheduleOsRunReport.adr_routing_mode``, not on
    ``adr_reroutes_count``, so the mode-vs-count cases stay
    distinguishable.
    """
    _install_fake(
        monkeypatch,
        _make_fake_report(
            adr_reroutes_count=0,
            adr_routing_mode=ScheduleOsIndianAdrRoutingMode.UNDERLYING_ISSUER,
        ),
    )
    code = main(_run_args(cli_paths))
    assert code == 0
    err = capsys.readouterr().err
    # The opt-in (positive) advisory keys:
    assert "0 dividend row(s) treated as Indian-source" in err
    assert "underlying-issuer" in err
    assert "remain foreign-source" not in err


def test_domain_error_exits_four(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    cli_paths: tuple[Path, Path, Path],
) -> None:
    _install_fake(
        monkeypatch,
        raises=ParseError(
            section="Dividends",
            row_index=0,
            column=None,
            message="bad",
            raw_value="",
        ),
    )
    code = main(_run_args(cli_paths))
    assert code == 4
    err = capsys.readouterr().err
    assert err.startswith("error: ")


def test_flex_query_input_rejection_routes_through_domain_error(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    cli_paths: tuple[Path, Path, Path],
) -> None:
    _install_fake(
        monkeypatch,
        raises=ParseError(
            section="Statement",
            row_index=0,
            column=None,
            message=(
                "Schedule OS does not support Flex Query inputs; "
                "see https://github.com/jay-tau/ibkr-fa/issues/12"
            ),
            raw_value="",
        ),
    )
    code = main(_run_args(cli_paths))
    assert code == 4
    err = capsys.readouterr().err
    assert "error: " in err
    assert "Flex Query" in err


def test_passes_all_args_to_pipeline(
    monkeypatch: pytest.MonkeyPatch,
    cli_paths: tuple[Path, Path, Path],
) -> None:
    recorder: dict[str, Any] = {}
    _install_fake(monkeypatch, _make_fake_report(), recorder=recorder)
    inp, cfg, out = cli_paths
    code = main(
        _run_args(
            cli_paths,
            "--allow-masked-account",
            "--suppress-cross-ay-warning",
            "--accept-anonymised-statement",
        )
    )
    assert code == 0
    assert recorder["input_path"] == inp
    assert recorder["config_path"] == cfg
    assert recorder["output_path"] == out
    assert recorder["financial_year"] == FinancialYear(start_year=2025)
    assert recorder["allow_masked_account"] is True
    assert recorder["suppress_cross_ay_warning"] is True
    assert recorder["accept_anonymised_statement"] is True
    # --format defaults to review-csv when not passed; threaded as kwarg.
    assert recorder["format_id"] == "review-csv"


def test_safety_warnings_includes_identity_fallback_block_for_anonymised_input() -> None:
    """#141 / T2 PR #166 P3: when identity_supplied_from_fallback is
    non-empty on the OS report, the safety-warnings block surfaces
    the user-attested-identity warning."""
    report = _make_fake_report(identity_supplied_from_fallback=frozenset({"Name", "Account"}))
    out = cli._render_schedule_os_safety_warnings(report)
    assert "Identity field(s)" in out
    assert "'Account'" in out
    assert "'Name'" in out
    assert "user-attested" in out


# ---- filed amounts past the CBDT bound (#830) -------------------------------

_DIVIDENDS_FIXTURE = (
    Path(__file__).parent
    / "fixtures"
    / "e2e_activity_statement_schedule_os_with_dividends_fy2025_26.csv"
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


def test_dividend_total_past_the_bound_refuses_before_any_writer(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two USD 0.7 trillion dividends are about ₹6e13 each, in range and in
    different section 10 periods, but the 1a(i) total isn't."""
    monkeypatch.setattr(
        "ibkr_fa.pipeline_os.output_binding",
        lambda *_args: pytest.fail("compute must refuse before any writer is built"),
    )
    text = _DIVIDENDS_FIXTURE.read_text(encoding="utf-8")
    assert text.count(",8.30,") == text.count(",5.00,") == text.count(",15.40,") == 1
    statement = tmp_path / "activity.csv"
    statement.write_text(
        text.replace(",8.30,", ",700000000000.00,")
        .replace(",5.00,", ",700000000000.00,")
        .replace(",15.40,", ",1400000000002.10,"),  # the section's Total
        encoding="utf-8",
    )
    config = tmp_path / "user.toml"
    config.write_text(_USER_TOML, encoding="utf-8")
    inputs = sorted(tmp_path.iterdir())

    code = main(_run_args((statement, config, tmp_path / "schedule-os.csv")))

    assert code == 4
    assert "InvariantError: Schedule OS 1a(i) dividend total must round" in capsys.readouterr().err
    assert sorted(tmp_path.iterdir()) == inputs
