"""Tests for :mod:`ibkr_fa.cli`.

CLI tests cover argument parsing, exit codes, and stdout/stderr
rendering. The underlying pipeline is already exhaustively tested
in tests/test_pipeline.py, so these tests use the same synthetic
fixtures and focus on the CLI-specific contract.
"""

from __future__ import annotations

import argparse
import csv
import dataclasses
from datetime import date
from decimal import Decimal
from io import StringIO
from pathlib import Path

import pytest

from ibkr_fa.cli import _build_parser, _render_run_report, main
from ibkr_fa.exceptions import InvariantError, MissingFxRateError
from ibkr_fa.fx.policy_fa import FaExactDatePolicy
from ibkr_fa.fx.ttbr_loader import load_ttbr
from ibkr_fa.models.fa_a2_row import (
    FaA2ClosingBlankReason,
    FaA2NatureOfAmount,
    FaA2Row,
    FaA2Status,
)
from ibkr_fa.models.interest_credit import InterestNonCredit, InterestRowGroup
from ibkr_fa.output._cleanup import RunOutputs
from ibkr_fa.pipeline import IbkrCsvFormat
from ibkr_fa.pipeline_fa_models import OrphanDividendInfo, RunReport
from ibkr_fa.types import Ticker
from tests._config_toml import NON_UTF8_TOML
from tests._fa_blank_texts import A2_CLOSING_BLANK_TEXT
from tests._flex_records import with_flex_records
from tests._flex_section_rows import OPEN_POSITIONS_HEADER, TRADES_LOT_DETAIL_HEADER
from tests._ibkr_account_refusals import (
    ACCOUNT_MISMATCH_REFUSAL,
    MASKED_ACCOUNT_REFUSAL,
    cli_account_refusal,
)
from tests._ibkr_flex_cli_inputs import CLI_OPEN_POSITIONS_SECTION as _OPEN_POSITIONS_SECTION
from tests._ibkr_flex_cli_inputs import earlier_schedule_fa_outputs as _earlier_schedule_fa_outputs
from tests._ibkr_pipeline_inputs import PIPELINE_ACCOUNT_INFO_SECTION as _ACCOUNT_INFO_SECTION
from tests._ibkr_pipeline_inputs import write_pipeline_flex_csv as write_flex_csv
from tests._ibkr_pipeline_inputs import write_pipeline_user_toml as write_user_toml
from tests._ibkr_user_toml import CUSTODIAN_TOML
from tests._trades_totals import with_trades_totals

# A Flex Trades section with a header and no rows: nothing was traded (#1286).
_NO_TRADES_SECTION = TRADES_LOT_DETAIL_HEADER + "\n"


@pytest.fixture
def standard_run(tmp_path: Path) -> tuple[Path, Path, Path]:
    """Pre-built flex CSV + user.toml + intended output path."""
    flex = write_flex_csv(
        tmp_path, _ACCOUNT_INFO_SECTION, _OPEN_POSITIONS_SECTION, _NO_TRADES_SECTION
    )
    cfg = write_user_toml(tmp_path)
    out = tmp_path / "review.csv"
    return flex, cfg, out


# Restore the writer store and catalog so per-test format injections
# (e.g., test_unknown_format) don't bleed across tests.
pytestmark = pytest.mark.usefixtures("isolated_output_registry")


# ---- Fidelity CMA statement discovery -------------------------------------

_CMA_CONFIG = """\
[account]
number = "U1234567"
holder_name = "Tax Payer"
opening_date = 2020-01-01
status = "BeneficialOwner"

[fidelity_cma]
account_id = "Z99-999999"
spaxx_isin = "US31617H1023"
status = "BeneficialOwner"
"""


@pytest.mark.parametrize(
    ("command", "extra_args"),
    [
        (
            "fidelity-cma-income",
            (
                *("--tax-forms", "{tmp}/tax-forms", "--output-dir", "{tmp}/out"),
                *("--financial-year", "2025-26", "--itr-form", "ITR-2"),
            ),
        ),
        (
            "fidelity-cma-cg",
            ("--output-dir", "{tmp}/out", "--financial-year", "2025-26", "--itr-form", "ITR-2"),
        ),
        ("fidelity-cma-fa", ("--output", "{tmp}/out/schedule-fa.csv", "--calendar-year", "2025")),
    ],
)
def test_cma_discovery_refusal_exits_4_with_its_text(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    command: str,
    extra_args: tuple[str, ...],
) -> None:
    """Every CMA command prints a discovery refusal as plain text, exit 4 (#630)."""
    statements = tmp_path / "statements.pdf"
    statements.write_bytes(b"%PDF placeholder")
    config = tmp_path / "user.toml"
    config.write_text(_CMA_CONFIG, encoding="utf-8")
    (tmp_path / "out").mkdir()
    code = main(
        [
            command,
            "--input",
            str(statements),
            "--config",
            str(config),
            *(arg.format(tmp=tmp_path) for arg in extra_args),
        ]
    )
    assert code == 4
    assert capsys.readouterr().err == "error: monthly-statement input must be a directory\n"


# ---- argparse contracts ----------------------------------------------------


@pytest.mark.parametrize(
    ("command", "extra_args"),
    [
        ("netbenefits-income", ()),
        ("fidelity-cma-income", ("--tax-forms", "tax-forms")),
        ("stockplan-income", ()),
        ("netbenefits-all", ()),
        ("stockplan-all", ()),
    ],
)
def test_legacy_debug_form67_alias_maps_to_unified_debug(
    command: str, extra_args: tuple[str, ...]
) -> None:
    args = _build_parser().parse_args(
        [
            command,
            "--input",
            "input",
            "--config",
            "user.toml",
            "--output-dir",
            "output",
            "--financial-year",
            "2025-26",
            "--itr-form",
            "ITR-2",
            *extra_args,
            "--debug-form67",
        ]
    )
    assert args.debug is True
    assert not hasattr(args, "debug_form67")


def test_help_exits_zero(capsys: pytest.CaptureFixture[str]) -> None:
    """`ibkr-fa --help` should print usage and exit 0 (argparse convention)."""
    with pytest.raises(SystemExit) as exc:
        main(["--help"])
    assert exc.value.code == 0
    captured = capsys.readouterr()
    assert "Produce Indian-tax artefacts" in captured.out
    assert "schedule-fa" in captured.out


def test_help_mentions_compliance_advisories() -> None:
    """`ibkr-fa --help` epilog must mention both the BMA disclosure
    advisory (PR #178) and the ITR-form selection advisory (PR #179)
    so a user discovering the tool via --help knows these compliance
    surfaces exist before running any subcommand. Locked here so a
    future maintainer cannot silently drop the epilog or its key
    statutory citations.

    We assert against the raw ``parser.epilog`` (and a normalised-
    whitespace projection of the rendered ``--help`` output) rather
    than the raw rendered output, because argparse's default
    ``HelpFormatter`` wraps long lines on word boundaries and would
    otherwise break multi-word substring assertions.
    """
    parser = _build_parser()
    epilog = parser.epilog or ""
    buf = StringIO()
    parser.print_help(file=buf)
    # Normalise so wrap-induced single newlines don't break
    # multi-word substring matches.
    rendered = " ".join(buf.getvalue().split())

    # Sanity: every assertion must hold for both the source-of-truth
    # epilog string AND the rendered help (after whitespace
    # normalisation). The dual check guards against (a) the epilog
    # being silently dropped from the parser, and (b) the parser
    # being constructed without an epilog.
    for required in (
        # BMA advisory hooks
        "Black Money Act",
        "§43",
        "§50",
        # PR #181 T5 P2: epilog must surface the ₹20 lakh aggregate-
        # foreign-asset carve-out (Finance (No.2) Act, 2024) so the
        # epilog is not materially misleading for users with aggregate
        # foreign holdings below the threshold.
        "₹20 lakh",
        "Finance (No.2) Act, 2024",
        # PR #181 T1-r2 correctness P2: §50 carve-out is enacted by
        # Finance Act, 2026 (Act 4 of 2026, Presidential assent
        # 30 March 2026), NOT "proposed in Finance Bill, 2026 ...
        # not yet enacted" — the Bill received Presidential assent
        # two months before the PR commit date.
        "Finance Act, 2026",
        "Act 4 of 2026",
        "30 March 2026",
        # ITR-form selection hooks
        "ITR-form selection",
        "ITR-2",
        "ITR-3",
        # Lock the "never applicable" forms (PR #181 T1 P3 hardening)
        "ITR-1",
        "ITR-4",
        # Lock routing claims (PR #181 T1 P3 hardening): FA → stdout,
        # OS/CG/CG-LTCG → stderr
        "schedule-fa",
        "stdout",
        # Non-quietable contract
        "--quiet",
        "stderr",
    ):
        assert required in epilog, f"missing from parser.epilog (source of truth): {required!r}"
        assert required in rendered, f"missing from rendered --help (post-wrap): {required!r}"

    # PR #181 T1-r2 correctness P2 anti-regression: must NOT cite
    # the obsolete "Finance Bill, 2026 ... not yet enacted" phrasing.
    for forbidden in ("Finance Bill, 2026", "not yet enacted", "pending enactment"):
        assert forbidden not in epilog, f"stale BMA citation in parser.epilog: {forbidden!r}"


def test_help_names_commands_without_advisories() -> None:
    """#1436: only the IBKR and robinhood-* subcommands print both advisories;
    merge prints the first only, and the other sources and init print neither."""
    parser = _build_parser()
    buf = StringIO()
    parser.print_help(file=buf)
    for text in (parser.epilog or "", " ".join(buf.getvalue().split())):
        assert "every subcommand" not in text
        assert "merge prints the first only" in text
        assert "the NetBenefits, Fidelity CMA, StockPlan and init subcommands print neither" in text


def test_subcommand_help_exits_zero(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exc:
        main(["schedule-fa", "--help"])
    assert exc.value.code == 0
    captured = capsys.readouterr()
    assert "--input" in captured.out
    assert "--calendar-year" in captured.out
    rendered = " ".join(captured.out.split()).replace("- ", "-")
    assert "proven pre-fee consideration" in rendered
    assert "31 December SBI TT-Buying rate" in rendered
    assert "commission-reduced net cash remain manual-fill" in rendered
    split_window = "the earlier of its disposal date and December 31"
    assert split_window in rendered
    assert "disposal date" not in rendered.replace(split_window, "")


def test_schedule_fa_help_names_the_a2_sibling(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """#1436: --output's help names the -a2 sibling that every non-JSON run
    clears, as a run without [account.custodian] does to a file already there."""
    statement = Path(__file__).parent / "fixtures" / "e2e_activity_statement_2025.csv"
    out = tmp_path / "review.csv"
    sibling = tmp_path / "review-a2.csv"
    sibling.write_text("a file already there\n", encoding="utf-8")
    rc = main(
        [
            "schedule-fa",
            "--input",
            str(statement),
            "--config",
            str(write_user_toml(tmp_path)),
            "--output",
            str(out),
            "--calendar-year",
            "2025",
            "--no-fetch-prices",
            "--price-cache-dir",
            str(tmp_path / "price-cache"),
        ]
    )
    assert rc == 3
    assert out.exists()
    assert not sibling.exists()
    capsys.readouterr()

    with pytest.raises(SystemExit):
        main(["schedule-fa", "--help"])
    rendered = " ".join(capsys.readouterr().out.split()).replace("- ", "-")
    assert "Table A2 to the <name>-a2.<ext> sibling (review.csv → review-a2.csv)" in rendered
    assert "first deletes any file already there, even when the run writes no A2" in rendered


def test_price_cache_dir_help_never_says_to_delete_or_empty_the_cache() -> None:
    """#1558: no --price-cache-dir help tells the filer to delete or empty the
    cache; schedule-fa's gives the refusals' move-aside route (#1528, #1530)."""
    subparsers = next(
        action
        for action in _build_parser()._actions
        if isinstance(action, argparse._SubParsersAction)
    )
    helps = {
        command: " ".join(str(action.help).split())
        for command, parser in subparsers.choices.items()
        for action in parser._actions
        if "--price-cache-dir" in action.option_strings
    }
    assert "schedule-fa" in helps
    assert [
        command
        for command, text in helps.items()
        if "delete" in text.casefold() or "empty" in text.casefold()
    ] == []
    assert helps["schedule-fa"].endswith(
        "next to the input statement. To refetch a holding's prices, move its cache file, "
        "and any .lock beside it, out of this directory and re-run with price fetching "
        "(without --no-fetch-prices)."
    )


def test_netbenefits_fa_help_names_cache_and_high_basis_manual_fill(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """#929: the peak isn't always filled, and a price cache can stand in for an override."""
    with pytest.raises(SystemExit) as exc:
        main(["netbenefits-fa", "--help"])
    assert exc.value.code == 0
    rendered = " ".join(capsys.readouterr().out.split()).replace("- ", "-")
    assert "filled either way" not in rendered
    assert "needs a price fetch" not in rendered
    assert (
        "a complete, integrity-checked price cache already on disk or from a "
        "[[netbenefits.price_overrides]] config entry"
    ) in rendered
    assert (
        "When the peak can't be established, the CSV outputs and the Quicko workbook "
        "leave it blank for manual fill, and no CBDT JSON fragment is written."
    ) in rendered


def test_no_subcommand_exits_nonzero(capsys: pytest.CaptureFixture[str]) -> None:
    """Calling `ibkr-fa` without a subcommand is a usage error."""
    with pytest.raises(SystemExit) as exc:
        main([])
    assert exc.value.code != 0
    captured = capsys.readouterr()
    assert "required" in captured.err.lower() or "command" in captured.err.lower()


def test_missing_required_arg_exits_nonzero(
    capsys: pytest.CaptureFixture[str],
) -> None:
    with pytest.raises(SystemExit) as exc:
        main(["schedule-fa", "--input", "/tmp/x.csv"])
    assert exc.value.code != 0
    err = capsys.readouterr().err.lower()
    assert "required" in err or "config" in err


def test_format_choice_validated_by_argparse(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Argparse should reject an unknown --format before the
    pipeline ever runs. This catches typos early."""
    with pytest.raises(SystemExit) as exc:
        main(
            [
                "schedule-fa",
                "--input",
                "/tmp/x.csv",
                "--config",
                "/tmp/u.toml",
                "--output",
                "/tmp/o.csv",
                "--calendar-year",
                "2025",
                "--format",
                "not-a-format",
            ]
        )
    assert exc.value.code != 0
    assert "argument --format: invalid value" in capsys.readouterr().err.lower()


def test_argparse_usage_errors_exit_2_not_4(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Regression for the exit-code policy split: argparse usage
    errors (missing required flag, bad subcommand, invalid choice)
    must exit with code 2 — NOT 4. Code 4 is reserved for pipeline
    domain errors so wrappers can distinguish "wrong command line"
    from "wrong data". Without this split, a missing --input and a
    bad account number would return the same code, defeating the
    point of structured exit codes.
    """
    # Missing required flag.
    with pytest.raises(SystemExit) as exc1:
        main(["schedule-fa"])
    assert exc1.value.code == 2

    # Invalid --format choice.
    with pytest.raises(SystemExit) as exc2:
        main(
            [
                "schedule-fa",
                "--input",
                "/tmp/x.csv",
                "--config",
                "/tmp/u.toml",
                "--output",
                "/tmp/o.csv",
                "--calendar-year",
                "2025",
                "--format",
                "bogus",
            ]
        )
    assert exc2.value.code == 2

    # Drain captured argparse output so other tests don't see it.
    capsys.readouterr()


# ---- happy path ------------------------------------------------------------


def test_successful_run_writes_csv_and_exits_3_for_manual_fill(
    standard_run: tuple[Path, Path, Path],
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Exit 3: Nature of entity, peak (blank under conftest.py's offline
    price fetcher) and gross amount paid stay manual-fill, and without
    [account.custodian] no Table A2 is written. Verifies stdout summary
    AND the manual-fill warning content."""
    flex, cfg, out = standard_run
    rc = main(
        [
            "schedule-fa",
            "--input",
            str(flex),
            "--config",
            str(cfg),
            "--output",
            str(out),
            "--calendar-year",
            "2025",
        ]
    )
    assert rc == 3
    assert out.exists()
    captured = capsys.readouterr()
    assert "Wrote 1 Schedule FA" in captured.out
    assert "Account: U***5678" in captured.out
    assert "U12345678" not in captured.out
    # The v1-incomplete columns each appear in the warning block.
    # This fixture has only an open lot, whose gross proceeds are known to be 0.
    assert "Peak value" in captured.out
    assert "gross amount paid" in captured.out.lower()
    assert "gross proceeds" not in captured.out.lower()
    assert "Exit status: 3" in captured.out


def test_successful_run_with_explicit_format(
    standard_run: tuple[Path, Path, Path],
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The default --format is review-csv, but passing it explicitly
    must also work."""
    flex, cfg, out = standard_run
    rc = main(
        [
            "schedule-fa",
            "--input",
            str(flex),
            "--config",
            str(cfg),
            "--output",
            str(out),
            "--calendar-year",
            "2025",
            "--format",
            "review-csv",
        ]
    )
    assert rc == 3
    assert out.exists()


# ---- domain-error rendering -----------------------------------------------


def test_config_error_renders_and_exits_4(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Account-number mismatch raises ConfigError; CLI should render
    it to stderr and exit 4 (domain error — distinct from argparse's
    exit code 2). The refusal prints neither account number, masked
    or not (#1047)."""
    flex = write_flex_csv(
        tmp_path, _ACCOUNT_INFO_SECTION, _OPEN_POSITIONS_SECTION, _NO_TRADES_SECTION
    )
    cfg = write_user_toml(tmp_path, account_number="U99999999")
    out = tmp_path / "review.csv"
    rc = main(
        [
            "schedule-fa",
            "--input",
            str(flex),
            "--config",
            str(cfg),
            "--output",
            str(out),
            "--calendar-year",
            "2025",
        ]
    )
    assert rc == 4
    assert not out.exists()
    captured = capsys.readouterr()
    assert captured.err == cli_account_refusal(ACCOUNT_MISMATCH_REFUSAL)
    for account in ("U99999999", "U12345678", "U***9999", "U***5678"):
        assert account.lower() not in captured.err.lower(), account


def test_non_utf8_config_refuses_with_exit_4(
    standard_run: tuple[Path, Path, Path], capsys: pytest.CaptureFixture[str]
) -> None:
    """A user.toml saved in a legacy encoding is a refused input, not a
    traceback (#934)."""
    flex, cfg, out = standard_run
    cfg.write_bytes(NON_UTF8_TOML)
    rc = main(
        [
            "schedule-fa",
            "--input",
            str(flex),
            "--config",
            str(cfg),
            "--output",
            str(out),
            "--calendar-year",
            "2025",
        ]
    )
    assert rc == 4
    assert not out.exists()
    err = capsys.readouterr().err
    assert "error: ConfigError at field '<file>': must be UTF-8 text" in err
    assert "Traceback" not in err


def test_config_refusal_does_not_print_the_configured_value(
    standard_run: tuple[Path, Path, Path], capsys: pytest.CaptureFixture[str]
) -> None:
    """A value in the wrong user.toml field is refused by its field path, never
    printed (#1045)."""
    flex, cfg, out = standard_run
    text = cfg.read_text(encoding="utf-8")
    assert text.count("opening_date = 2020-01-01\n") == 1
    cfg.write_text(
        text.replace("opening_date = 2020-01-01\n", 'opening_date = "Synthetic Holder Two"\n'),
        encoding="utf-8",
    )
    rc = main(
        [
            "schedule-fa",
            "--input",
            str(flex),
            "--config",
            str(cfg),
            "--output",
            str(out),
            "--calendar-year",
            "2025",
        ]
    )
    assert rc == 4
    assert not out.exists()
    err = capsys.readouterr().err
    assert (
        "error: ConfigError at field 'account.opening_date': must be an ISO date (YYYY-MM-DD)"
    ) in err
    assert "Synthetic Holder Two" not in err


def test_parse_error_renders_and_exits_4(
    standard_run: tuple[Path, Path, Path],
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Wrong --calendar-year against the CSV's ReportDate raises
    ParseError; CLI renders + exits 4 (domain error)."""
    flex, cfg, out = standard_run
    rc = main(
        [
            "schedule-fa",
            "--input",
            str(flex),
            "--config",
            str(cfg),
            "--output",
            str(out),
            "--calendar-year",
            "2024",  # CSV ReportDate is 20251231
        ]
    )
    assert rc == 4
    captured = capsys.readouterr()
    assert "error:" in captured.err
    assert "ReportDate" in captured.err


def test_near_miss_trades_header_exits_4_and_writes_nothing(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """#904: a Flex Trades header with one unsupported column added to
    the simple Trades column set refuses, instead of filing Table A3 without the
    year's sold lots."""
    trades_header = (
        '"CurrencyPrimary","FXRateToBase","AssetClass","SubCategory","Symbol",'
        '"Description","CUSIP","ISIN","ListingExchange","IssuerCountryCode",'
        '"ReportDate","TradeDate","SettleDateTarget","TransactionType","Quantity",'
        '"TradePrice","TradeMoney","Proceeds","Taxes","IBCommission",'
        '"IBCommissionCurrency","NetCash","ClosePrice","Open/CloseIndicator",'
        '"CostBasis","FifoPnlRealized","Buy/Sell","OrderTime"\n'
    )
    flex = write_flex_csv(tmp_path, _ACCOUNT_INFO_SECTION, _OPEN_POSITIONS_SECTION, trades_header)
    cfg = write_user_toml(tmp_path)
    out = tmp_path / "review.csv"
    rc = main(
        [
            "schedule-fa",
            "--input",
            str(flex),
            "--config",
            str(cfg),
            "--output",
            str(out),
            "--calendar-year",
            "2025",
        ]
    )
    assert rc == 4
    assert sorted(path.name for path in tmp_path.iterdir()) == ["flex.csv", "user.toml"]
    captured = capsys.readouterr()
    assert "changed 'Trades' section header" in captured.err
    assert "extra 1 column(s) not in any known section" in captured.err
    assert "OrderTime" not in captured.err


def test_missing_input_file_renders_and_exits_4(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    cfg = write_user_toml(tmp_path)
    rc = main(
        [
            "schedule-fa",
            "--input",
            str(tmp_path / "missing.csv"),
            "--config",
            str(cfg),
            "--output",
            str(tmp_path / "out.csv"),
            "--calendar-year",
            "2025",
        ]
    )
    assert rc == 4
    captured = capsys.readouterr()
    assert "error:" in captured.err


def test_pipeline_failures_do_not_create_output_file(
    standard_run: tuple[Path, Path, Path],
) -> None:
    """If the pipeline raises, the output CSV must NOT be left in a
    partial state. The writer is the last step, so an upstream
    error means it never ran."""
    flex, cfg, out = standard_run
    main(
        [
            "schedule-fa",
            "--input",
            str(flex),
            "--config",
            str(cfg),
            "--output",
            str(out),
            "--calendar-year",
            "2024",  # ReportDate mismatch -> ParseError
        ]
    )
    assert not out.exists()


_CASH_ONLY_STATEMENT_WITH_NAV = """\
Statement,Header,Field Name,Field Value
Statement,Data,BrokerName,Interactive Brokers LLC
Statement,Data,Title,Activity Statement
Statement,Data,Period,"January 1, 2025 - December 31, 2025"
Account Information,Header,Field Name,Field Value
Account Information,Data,Name,Test Holder
Account Information,Data,Account,U12345678
Account Information,Data,Account Type,Individual
Account Information,Data,Customer Type,Individual
Account Information,Data,Account Capabilities,Cash
Account Information,Data,Base Currency,USD
Financial Instrument Information,Header,Asset Category,Symbol,Description,Conid,Security ID,Listing Exch,Multiplier,Type,Code
Trades,Header,DataDiscriminator,Asset Category,Currency,Symbol,Date/Time,Quantity,T. Price,C. Price,Proceeds,Comm/Fee,Basis,Realized P/L,MTM P/L,Code
Net Asset Value,Header,Asset Class,Prior Total,Current Long,Current Short,Current Total,Change
Net Asset Value,Data,Cash,0,100.00,0,100.00,100.00
Net Asset Value,Data,Total,0,100.00,0,100.00,100.00
Change in NAV,Header,Field Name,Field Value
Change in NAV,Data,Ending Value,100.00
"""  # noqa: E501


def _refuse_dec31_2025_rate(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make the FA FX policy refuse December 31, 2025 and serve every other date."""
    real_rate_for = FaExactDatePolicy.rate_for

    def _rate_for(self: FaExactDatePolicy, event_date: date) -> Decimal:
        if event_date == date(2025, 12, 31):
            raise MissingFxRateError(
                currency="USD",
                last_available=None,
                max_carry_forward_days=self.max_carry_forward_days,
            )
        return real_rate_for(self, event_date)

    monkeypatch.setattr(FaExactDatePolicy, "rate_for", _rate_for)


@pytest.mark.parametrize(
    ("format_id", "suffix", "extra_args"),
    [
        ("review-csv", ".csv", ()),
        ("filing-csv", ".csv", ()),
        ("xlsx", ".xlsx", ()),
        ("cbdt-json-fragment", ".json", ("--itr-form", "ITR-2")),
    ],
)
def test_missing_dec31_rate_for_a2_exits_4_without_outputs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    format_id: str,
    suffix: str,
    extra_args: tuple[str, ...],
) -> None:
    """#699: a cash-only account's A2 closing balance needs the Dec-31 TTBR,
    so a missing rate refuses the run in every output format."""
    statement = tmp_path / "statement.csv"
    statement.write_text(_CASH_ONLY_STATEMENT_WITH_NAV, encoding="utf-8")
    cfg = write_user_toml(tmp_path)
    cfg.write_text(cfg.read_text(encoding="utf-8") + CUSTODIAN_TOML, encoding="utf-8")
    out = tmp_path / f"schedule-fa{suffix}"
    _refuse_dec31_2025_rate(monkeypatch)
    rc = main(
        [
            "schedule-fa",
            "--input",
            str(statement),
            "--config",
            str(cfg),
            "--output",
            str(out),
            "--calendar-year",
            "2025",
            "--format",
            format_id,
            *extra_args,
        ]
    )
    assert rc == 4
    assert "MissingFxRateError" in capsys.readouterr().err
    assert not out.exists()
    assert not out.with_name(f"schedule-fa-a2{suffix}").exists()


# #906: no Total row, a blank Stock amount and no Change in NAV. The parser
# used to file Cash alone (100) as the closing NAV.
_PARTIAL_NAV_STATEMENT = _CASH_ONLY_STATEMENT_WITH_NAV.replace(
    "Net Asset Value,Data,Total,0,100.00,0,100.00,100.00\n"
    "Change in NAV,Header,Field Name,Field Value\n"
    "Change in NAV,Data,Ending Value,100.00\n",
    "Net Asset Value,Data,Stock,0,,0,,\n",
)


def _edit_statement(old: str, new: str) -> str:
    """``_CASH_ONLY_STATEMENT_WITH_NAV`` with ``old`` replaced by ``new``."""
    assert old in _CASH_ONLY_STATEMENT_WITH_NAV
    return _CASH_ONLY_STATEMENT_WITH_NAV.replace(old, new)


_NAV_SECTION = (
    "Net Asset Value,Header,Asset Class,Prior Total,Current Long,Current Short,Current Total,"
    "Change\n"
    "Net Asset Value,Data,Cash,0,100.00,0,100.00,100.00\n"
    "Net Asset Value,Data,Total,0,100.00,0,100.00,100.00\n"
)
_ENDING_VALUE_ROW = "Change in NAV,Data,Ending Value,100.00\n"

# #1013: each statement the pipelines build that leaves the A2 closing balance
# blank, with the reason the run must name.
_A2_CLOSING_BLANK_RUNS = pytest.mark.parametrize(
    ("statement_text", "reason"),
    [
        pytest.param(
            _edit_statement(_NAV_SECTION, ""),
            FaA2ClosingBlankReason.NO_ASSET_CLASS_BLOCK,
            id="no-nav-section",
        ),
        pytest.param(
            _edit_statement(_ENDING_VALUE_ROW, _ENDING_VALUE_ROW * 2),
            FaA2ClosingBlankReason.MULTIPLE_ENDING_VALUES,
            id="two-ending-values",
        ),
        pytest.param(
            _edit_statement(
                "Net Asset Value,Data,Total,0,100.00,0,100.00,100.00\n"
                "Change in NAV,Header,Field Name,Field Value\n"
                f"{_ENDING_VALUE_ROW}",
                "",
            ),
            FaA2ClosingBlankReason.NO_ENDING_VALUE,
            id="no-total-row-no-change-in-nav",
        ),
        pytest.param(
            _PARTIAL_NAV_STATEMENT,
            FaA2ClosingBlankReason.BLANK_ASSET_CLASS_AMOUNT,
            id="blank-asset-class-amount",
        ),
        pytest.param(
            _edit_statement(_ENDING_VALUE_ROW, "Change in NAV,Data,Ending Value,\n"),
            FaA2ClosingBlankReason.BLANK_ENDING_VALUE,
            id="blank-ending-value",
        ),
        pytest.param(
            with_flex_records(
                "\n".join((_ACCOUNT_INFO_SECTION, _OPEN_POSITIONS_SECTION, _NO_TRADES_SECTION))
            ),
            FaA2ClosingBlankReason.FLEX_QUERY,
            id="flex-query",
        ),
        pytest.param(
            _edit_statement(
                "Account Information,Data,Base Currency,USD\n",
                "Account Information,Data,Base Currency,EUR\n",
            ),
            FaA2ClosingBlankReason.NON_USD_BASE,
            id="eur-base",
        ),
    ],
)


def _schedule_fa_with_custodian(tmp_path: Path, statement_text: str, *args: str) -> int:
    statement = tmp_path / "statement.csv"
    statement.write_text(with_trades_totals(statement_text), encoding="utf-8")
    cfg = write_user_toml(tmp_path)
    cfg.write_text(cfg.read_text(encoding="utf-8") + CUSTODIAN_TOML, encoding="utf-8")
    return main(
        [
            "schedule-fa",
            "--input",
            str(statement),
            "--config",
            str(cfg),
            "--calendar-year",
            "2025",
            *args,
        ]
    )


def _other_reasons_absent(text: str, reason: FaA2ClosingBlankReason) -> None:
    for other, (cause, _remedy) in A2_CLOSING_BLANK_TEXT.items():
        if other is not reason:
            assert cause not in text, other


@_A2_CLOSING_BLANK_RUNS
def test_blank_a2_closing_cell_names_its_reason(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    statement_text: str,
    reason: FaA2ClosingBlankReason,
) -> None:
    out = tmp_path / "schedule-fa.csv"
    assert _schedule_fa_with_custodian(tmp_path, statement_text, "--output", str(out)) == 3
    captured = capsys.readouterr()
    cause, remedy = A2_CLOSING_BLANK_TEXT[reason]
    assert (
        "    - Closing balance\n"
        f"      The closing balance is blank because {cause}.\n"
        f"      {remedy}\n"
    ) in captured.out
    _other_reasons_absent(captured.out + captured.err, reason)
    with out.with_name("schedule-fa-a2.csv").open(encoding="utf-8-sig", newline="") as fh:
        [row] = list(csv.DictReader(fh))
    assert row["Closing balance"] == ""
    assert row["_audit: Closing balance (base currency, full precision)"] == ""


@_A2_CLOSING_BLANK_RUNS
def test_blank_a2_closing_json_gap_names_its_reason(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    statement_text: str,
    reason: FaA2ClosingBlankReason,
) -> None:
    out = tmp_path / "schedule-fa.json"
    rc = _schedule_fa_with_custodian(
        tmp_path,
        statement_text,
        "--output",
        str(out),
        "--format",
        "cbdt-json-fragment",
        "--itr-form",
        "ITR-2",
    )
    assert rc == 3
    captured = capsys.readouterr()
    cause, remedy = A2_CLOSING_BLANK_TEXT[reason]
    assert (
        f"  - A2 ClosingBalance: the closing balance is blank because {cause}. {remedy} No "
        "config value can supply an A2 closing balance, so to enter it by hand use the "
        "review CSV or xlsx output instead\n"
    ) in captured.err
    _other_reasons_absent(captured.out + captured.err, reason)
    assert not out.exists()


# #1309: a statement whose NAV figures contradict each other stops the run,
# naming the cell and no figure. These used to leave the closing balance blank.
_CONTRADICTION_REMEDY = (
    "The statement contradicts itself, so Schedule FA Table A2's closing balance can't be "
    "filed. Re-download the Activity Statement from IBKR or fix the file, then re-run."
)
_TOTAL_BREAKDOWN_ERROR = (
    "error: ParseError in section 'Net Asset Value' at row 1, column 'Current Total': the "
    "asset-class rows' Current Total amounts don't add up to this Total row's Current Total "
    "within 0.01, or there are no asset-class rows and the Total isn't zero. "
    f"{_CONTRADICTION_REMEDY}\n"
)
_ENDING_VALUE_ERROR = (
    "error: ParseError in section 'Change in NAV' at row 0, column 'Field Value': this "
    "Ending Value differs by more than 0.01 from the Net Asset Value figure (its Total "
    "row's Current Total or, with no Total row, the sum of its asset-class rows). "
    f"{_CONTRADICTION_REMEDY}\n"
)
_CASH_ROW = "Net Asset Value,Data,Cash,0,100.00,0,100.00,100.00\n"
_CASH_50000 = _edit_statement(_CASH_ROW, _CASH_ROW.replace("100.00", "50000.00"))
_NAV_CONTRADICTIONS = pytest.mark.parametrize(
    ("statement_text", "error"),
    [
        pytest.param(
            _edit_statement(
                "Net Asset Value,Data,Cash,0,100.00,0,100.00,100.00\n"
                "Net Asset Value,Data,Total,0,100.00,0,100.00,100.00\n",
                "Net Asset Value,Data,Cash,0,150.00,0,150.00,150.00\n"
                "Net Asset Value,Data,Total,0,150.00,0,150.00,150.00\n",
            ),
            _ENDING_VALUE_ERROR,
            id="total-disagrees-with-ending-value",
        ),
        pytest.param(
            _edit_statement(_CASH_ROW, _CASH_ROW.replace("100.00", "150.00")),
            _TOTAL_BREAKDOWN_ERROR,
            id="total-disagrees-with-asset-class-rows",
        ),
    ],
)
_SCHEDULE_FA_FORMATS = pytest.mark.parametrize(
    ("format_id", "suffix", "extra_args"),
    [
        ("review-csv", ".csv", ()),
        ("filing-csv", ".csv", ()),
        ("xlsx", ".xlsx", ()),
        ("cbdt-json-fragment", ".json", ("--itr-form", "ITR-2")),
    ],
)


@_NAV_CONTRADICTIONS
@_SCHEDULE_FA_FORMATS
def test_nav_figures_that_disagree_exit_4_and_remove_earlier_outputs(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    statement_text: str,
    error: str,
    format_id: str,
    suffix: str,
    extra_args: tuple[str, ...],
) -> None:
    out = tmp_path / f"schedule-fa{suffix}"
    earlier = _earlier_schedule_fa_outputs(out, format_id)
    rc = _schedule_fa_with_custodian(
        tmp_path, statement_text, "--output", str(out), "--format", format_id, *extra_args
    )
    assert rc == 4
    assert capsys.readouterr() == ("", error)
    assert not any(path.exists() for path in earlier)
    assert not out.with_name(f"schedule-fa-a2{suffix}").exists()


@pytest.mark.parametrize(
    ("format_id", "suffix", "extra_args"),
    [
        ("filing-csv", ".csv", ()),
        ("cbdt-json-fragment", ".json", ("--itr-form", "ITR-2")),
    ],
)
def test_nav_figures_that_disagree_refuse_before_a_missing_dec31_rate(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    format_id: str,
    suffix: str,
    extra_args: tuple[str, ...],
) -> None:
    """#1309: PR #1302's blank closing balance needed no Dec-31 TTBR, so this
    statement exited 3 where the printed one exits 4 with
    MissingFxRateError. It exits 4 again, at the NAV."""
    _refuse_dec31_2025_rate(monkeypatch)
    out = tmp_path / f"schedule-fa{suffix}"
    rc = _schedule_fa_with_custodian(
        tmp_path, _CASH_50000, "--output", str(out), "--format", format_id, *extra_args
    )
    assert rc == 4
    assert capsys.readouterr() == ("", _TOTAL_BREAKDOWN_ERROR)
    assert list(tmp_path.glob("schedule-fa*")) == []


def test_nav_figures_that_disagree_refuse_before_the_account_number_check(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """#1309: the NAV is parsed at dispatch, before the account-number check
    that used to refuse this run."""
    statement = tmp_path / "statement.csv"
    statement.write_text(_CASH_50000, encoding="utf-8")
    cfg = write_user_toml(tmp_path, account_number="U99999999")
    cfg.write_text(cfg.read_text(encoding="utf-8") + CUSTODIAN_TOML, encoding="utf-8")
    out = tmp_path / "schedule-fa.csv"
    rc = main(
        [
            "schedule-fa",
            *("--input", str(statement), "--config", str(cfg), "--output", str(out)),
            *("--calendar-year", "2025"),
        ]
    )
    assert rc == 4
    assert capsys.readouterr() == ("", _TOTAL_BREAKDOWN_ERROR)
    assert not out.exists()


def test_a_dispatcher_refusal_still_comes_before_nav_figures_that_disagree(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """#1309: the NAV is parsed last at dispatch, so an earlier refusal keeps
    its place."""
    statement_text = _CASH_50000.replace(
        "Account Information,Data,Base Currency,USD\n",
        "Account Information,Data,Base Currency,Synthetic Holder Two\n",
    )
    out = tmp_path / "schedule-fa.csv"
    assert _schedule_fa_with_custodian(tmp_path, statement_text, "--output", str(out)) == 4
    err = capsys.readouterr().err
    assert err.startswith(
        "error: ParseError in section 'Account Information' at row 0, column 'Base Currency'"
    )
    assert "Net Asset Value" not in err
    assert not out.exists()


@pytest.mark.parametrize(
    ("old", "new", "extra_args", "section"),
    [
        pytest.param(
            "Net Asset Value,Data,Total,0,100.00,0,100.00,100.00\n",
            "Net Asset Value,Data,Total,0,100.00,0,100.00,100.00\n"
            "Net Asset Value,Total,Total,0,150.00,0,150.00,150.00\n",
            (),
            "Net Asset Value",
            id="second-nav-total",
        ),
        pytest.param(
            "Account Information,Data,Name,Test Holder\n",
            "Account Information,Total,Name,Someone Else\n",
            ("--accept-anonymised-statement",),
            "Account Information",
            id="holder-name",
        ),
        pytest.param(
            "Account Information,Data,Account,U12345678\n",
            "Account Information,Total,Account,U99999999\n",
            ("--accept-anonymised-statement",),
            "Account Information",
            id="another-account",
        ),
    ],
)
def test_total_typed_row_in_a_section_that_defines_none_exits_4(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    old: str,
    new: str,
    extra_args: tuple[str, ...],
    section: str,
) -> None:
    """#1012: the splitter used to drop these rows. The second NAV total then
    left the first one filed in A2. Under the opt-in, the configured holder
    name or account number stood in for the dropped row, so another
    account's statement filed under this one. Each now refuses."""
    assert _CASH_ONLY_STATEMENT_WITH_NAV.count(old) == 1
    statement = tmp_path / "statement.csv"
    statement.write_text(_CASH_ONLY_STATEMENT_WITH_NAV.replace(old, new), encoding="utf-8")
    cfg = write_user_toml(tmp_path)
    cfg.write_text(cfg.read_text(encoding="utf-8") + CUSTODIAN_TOML, encoding="utf-8")
    out = tmp_path / "schedule-fa.csv"
    rc = main(
        [
            "schedule-fa",
            "--input",
            str(statement),
            "--config",
            str(cfg),
            "--output",
            str(out),
            "--calendar-year",
            "2025",
            "--format",
            "review-csv",
            *extra_args,
        ]
    )
    assert rc == 4
    err = capsys.readouterr().err
    assert f"ParseError in section {section!r}" in err
    assert f"is a 'Total' row, but the {section!r} section defines no 'SubTotal'" in err
    assert "U99999999" not in err
    assert not out.exists()
    assert not out.with_name("schedule-fa-a2.csv").exists()


def test_duplicate_name_row_exits_4_without_either_name(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """#1017, #1040: a repeated Account Information field refuses naming the
    row and the Field Name column, without echoing either row's cells."""
    statement_text = _edit_statement(
        "Account Information,Data,Name,Test Holder\n",
        "Account Information,Data,Name,Synthetic Holder One\n"
        "Account Information,Data,Name,Synthetic Holder Two\n",
    )
    out = tmp_path / "schedule-fa.csv"
    assert _schedule_fa_with_custodian(tmp_path, statement_text, "--output", str(out)) == 4
    captured = capsys.readouterr()
    assert "ParseError in section 'Account Information'" in captured.err
    assert "column 'Field Name'" in captured.err
    assert "row repeats an earlier row's Field Name" in captured.err
    assert "'Name'" not in captured.err
    assert "Synthetic Holder" not in captured.out + captured.err
    assert not out.exists()


def test_name_shaped_base_currency_exits_4_without_the_value(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """#1021: a malformed Base Currency refuses as an input error that names
    the field, without echoing the cell."""
    statement_text = _edit_statement(
        "Account Information,Data,Base Currency,USD\n",
        "Account Information,Data,Base Currency,Synthetic Holder Two\n",
    )
    out = tmp_path / "schedule-fa.csv"
    assert _schedule_fa_with_custodian(tmp_path, statement_text, "--output", str(out)) == 4
    captured = capsys.readouterr()
    assert "ParseError in section 'Account Information' at row 0, column 'Base Currency'" in (
        captured.err
    )
    assert "Synthetic Holder Two" not in captured.out + captured.err
    assert not out.exists()


_FLEX_LOT_IN_A_NAME = (
    _ACCOUNT_INFO_SECTION
    + OPEN_POSITIONS_HEADER
    + "\n"
    + '"Synthetic Holder Two","1","STK","COMMON","XQAQ","SAMPLE ISSUER AQ INC","99992A109",'
    '"US99992A1097","NASDAQ","US","20251231","3","250","750","200","600","Long","LOT",'
    '"20250602","20250602"\n'
)
_INSTRUMENT_AND_TRADES_HEADERS = (
    "Financial Instrument Information,Header,Asset Category,Symbol,Description,Conid,"
    "Security ID,Listing Exch,Multiplier,Type,Code\n"
    "Trades,Header,DataDiscriminator,Asset Category,Currency,Symbol,Date/Time,Quantity,"
    "T. Price,C. Price,Proceeds,Comm/Fee,Basis,Realized P/L,MTM P/L,Code\n"
)
_TRADE_IN_A_NAME = _edit_statement(
    _INSTRUMENT_AND_TRADES_HEADERS,
    _INSTRUMENT_AND_TRADES_HEADERS.replace(
        "Trades,Header",
        "Financial Instrument Information,Data,Stocks,XQAQ,SAMPLE ISSUER AQ INC,1,US99992A1097,"
        "NASDAQ,1,COMMON,\nTrades,Header",
    )
    + 'Trades,Data,Order,Stocks,Synthetic Holder Two,XQAQ,"2025-06-02, 09:30:00",3,200,201,'
    "-600,-1,601,0,3,O\n",
)


@pytest.mark.parametrize(
    ("statement_text", "location"),
    [
        pytest.param(
            _FLEX_LOT_IN_A_NAME,
            "ParseError in section 'Open Positions (LOT)' at row 0, column 'CurrencyPrimary'",
            id="flex",
        ),
        pytest.param(
            _TRADE_IN_A_NAME,
            "ParseError in section 'Trades' at row 0, column 'Currency'",
            id="activity-statement",
        ),
    ],
)
def test_name_shaped_lot_currency_exits_4_without_the_value(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], statement_text: str, location: str
) -> None:
    """#1027: a name-shaped currency in a lot or trade row refuses as an
    input error that names the cell's place, without echoing the cell."""
    out = tmp_path / "schedule-fa.csv"
    assert _schedule_fa_with_custodian(tmp_path, statement_text, "--output", str(out)) == 4
    captured = capsys.readouterr()
    assert location in captured.err
    assert "v1 supports USD only" in captured.err
    assert "Synthetic Holder Two" not in captured.out + captured.err
    assert not out.exists()


def test_name_shaped_flex_quantity_exits_4_without_the_value(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """#1038: a name in a Flex lot's Quantity refuses as an input error that
    names the cell's place and rule, without echoing the cell."""
    statement_text = _FLEX_LOT_IN_A_NAME.replace('"Synthetic Holder Two","1"', '"USD","1"').replace(
        '"20251231","3"', '"20251231","Synthetic Holder Two"'
    )
    out = tmp_path / "schedule-fa.csv"
    assert _schedule_fa_with_custodian(tmp_path, statement_text, "--output", str(out)) == 4
    captured = capsys.readouterr()
    assert (
        "ParseError in section 'Open Positions (LOT)' at row 0, column 'Quantity': "
        "must be a decimal number"
    ) in captured.err
    assert "synthetic holder two" not in (captured.out + captured.err).casefold()
    assert not out.exists()


def test_name_shaped_trade_code_exits_4_without_the_value(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """#1039: a name in an Activity Statement Trades row's Code refuses as an
    input error that names the cell's place and rule, without echoing the cell."""
    statement_text = _TRADE_IN_A_NAME.replace(
        "Stocks,Synthetic Holder Two,XQAQ", "Stocks,USD,XQAQ"
    ).replace(",0,3,O\n", ",0,3,O;Synthetic Holder Two\n")
    out = tmp_path / "schedule-fa.csv"
    assert _schedule_fa_with_custodian(tmp_path, statement_text, "--output", str(out)) == 4
    captured = capsys.readouterr()
    assert (
        "ParseError in section 'Trades' at row 0, column 'Code': unrecognised trade code token. "
    ) in captured.err
    assert "isn't listed in any of those.\n" in captured.err
    assert "synthetic holder two" not in (captured.out + captured.err).casefold()
    assert not out.exists()


def test_name_shaped_dividend_date_exits_4_without_the_value(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """#1040: a name in an Activity Statement Dividends Date refuses as an
    input error that names the cell's place and rule, without echoing the
    cell."""
    statement_text = (
        _CASH_ONLY_STATEMENT_WITH_NAV
        + "Dividends,Header,Currency,Date,Description,Amount\n"
        + 'Dividends,Data,USD,Synthetic Holder Two,"XQAQ(US99992A1097) Cash Dividend USD '
        + '1.00 per Share",10.00\n'
        + "Dividends,Data,Total,,,10.00\n"
    )
    out = tmp_path / "schedule-fa.csv"
    assert _schedule_fa_with_custodian(tmp_path, statement_text, "--output", str(out)) == 4
    captured = capsys.readouterr()
    assert (
        "ParseError in section 'Dividends' at row 0, column 'Date': "
        "must match DD-MM-YYYY or YYYY-MM-DD"
    ) in captured.err
    assert "synthetic holder two" not in (captured.out + captured.err).casefold()
    assert not out.exists()


# One CY 2025 input of each format, for the --calendar-year range checks (#928).
_CY2025_STATEMENTS = pytest.mark.parametrize(
    "statement_text",
    [
        pytest.param(_CASH_ONLY_STATEMENT_WITH_NAV, id="activity-statement"),
        pytest.param("\n".join((_ACCOUNT_INFO_SECTION, _OPEN_POSITIONS_SECTION)), id="flex-query"),
    ],
)


def _schedule_fa_for_year(tmp_path: Path, statement_text: str, year: str) -> int:
    statement = tmp_path / "statement.csv"
    statement.write_text(statement_text, encoding="utf-8")
    cfg = write_user_toml(tmp_path)
    out = tmp_path / "schedule-fa.csv"
    return main(
        [
            "schedule-fa",
            "--input",
            str(statement),
            "--config",
            str(cfg),
            "--output",
            str(out),
            "--calendar-year",
            year,
        ]
    )


@_CY2025_STATEMENTS
@pytest.mark.parametrize("year", ["0", "10000", "1989", "2100"])
def test_out_of_range_calendar_year_exits_4(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], statement_text: str, year: str
) -> None:
    """#928: a year outside 1990-2099 refuses with exit 4, not a date() traceback."""
    assert _schedule_fa_for_year(tmp_path, statement_text, year) == 4
    assert (
        f"error: calendar year {year} is out of the supported range [1990, 2099]"
        in capsys.readouterr().err
    )
    assert not (tmp_path / "schedule-fa.csv").exists()


@_CY2025_STATEMENTS
@pytest.mark.parametrize("year", ["1990", "2099"])
def test_boundary_calendar_year_reaches_the_period_refusal(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], statement_text: str, year: str
) -> None:
    """#928: 1990 and 2099 pass the range check; the CY 2025 input refuses on its period."""
    assert _schedule_fa_for_year(tmp_path, statement_text, year) == 4
    err = capsys.readouterr().err
    assert "ParseError" in err
    # The Activity Statement refusal names "Dec 31, <year>", the Flex one "<year>-12-31".
    assert f"must end on Dec 31, {year} and start" in err or f"({year}-12-31)" in err
    assert "supported range" not in err


@pytest.mark.parametrize(
    ("format_id", "suffix", "extra_args"),
    [
        ("review-csv", ".csv", ()),
        ("filing-csv", ".csv", ()),
        ("xlsx", ".xlsx", ()),
        ("cbdt-json-fragment", ".json", ("--itr-form", "ITR-2")),
    ],
)
def test_out_of_range_calendar_year_removes_earlier_outputs(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    format_id: str,
    suffix: str,
    extra_args: tuple[str, ...],
) -> None:
    """#928: the year refusal still removes the earlier outputs in every format: the
    A3 file and its A2 sibling, or a CBDT JSON fragment ibkr-fa wrote."""
    statement = tmp_path / "statement.csv"
    statement.write_text(_CASH_ONLY_STATEMENT_WITH_NAV, encoding="utf-8")
    cfg = write_user_toml(tmp_path)
    out = tmp_path / f"schedule-fa{suffix}"
    earlier = _earlier_schedule_fa_outputs(out, format_id)
    rc = main(
        [
            "schedule-fa",
            "--input",
            str(statement),
            "--config",
            str(cfg),
            "--output",
            str(out),
            "--calendar-year",
            "0",
            "--format",
            format_id,
            *extra_args,
        ]
    )
    assert rc == 4
    assert "calendar year 0 is out of the supported range" in capsys.readouterr().err
    assert not any(path.exists() for path in earlier)


_ZERO_PROCEEDS_STATEMENT = """\
Statement,Header,Field Name,Field Value
Statement,Data,BrokerName,Interactive Brokers LLC
Statement,Data,Title,Activity Statement
Statement,Data,Period,"January 1, 2025 - December 31, 2025"
Account Information,Header,Field Name,Field Value
Account Information,Data,Name,Test Holder
Account Information,Data,Account,U12345678
Account Information,Data,Account Type,Individual
Account Information,Data,Customer Type,Individual
Account Information,Data,Account Capabilities,Cash
Account Information,Data,Base Currency,USD
Financial Instrument Information,Header,Asset Category,Symbol,Description,Conid,Security ID,Listing Exch,Multiplier,Type,Code
Financial Instrument Information,Data,Stocks,XQAW,SAMPLE ISSUER AW INC,11111,US99990E1038,NASDAQ,1,COMMON,
Trades,Header,DataDiscriminator,Asset Category,Currency,Symbol,Date/Time,Quantity,T. Price,C. Price,Proceeds,Comm/Fee,Basis,Realized P/L,MTM P/L,Code
Trades,Data,Order,Stocks,USD,XQAW,"2025-01-15, 10:30:00",4,80.00,80.00,-320.00,-0.50,320.50,0,0,O
Trades,Data,Order,Stocks,USD,XQAW,"2025-06-16, 10:30:00",-4,0,0,0,0,-320.50,-320.50,0,C
"""  # noqa: E501


def test_zero_proceeds_closed_lot_needs_dec31_rate(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """#735: a lot closed out at a price of 0 still needs the Dec-31 TTBR for
    its proven gross proceeds, so a missing rate refuses the run. The
    refusal doesn't print the date it needed a rate for (#1047)."""
    statement = tmp_path / "statement.csv"
    statement.write_text(with_trades_totals(_ZERO_PROCEEDS_STATEMENT), encoding="utf-8")
    cfg = write_user_toml(tmp_path)
    cfg.write_text(cfg.read_text(encoding="utf-8") + CUSTODIAN_TOML, encoding="utf-8")
    price_cache = tmp_path / "price-cache"
    price_cache.mkdir()
    out = tmp_path / "schedule-fa.csv"
    _refuse_dec31_2025_rate(monkeypatch)
    rc = main(
        [
            "schedule-fa",
            "--input",
            str(statement),
            "--config",
            str(cfg),
            "--output",
            str(out),
            "--calendar-year",
            "2025",
            "--format",
            "review-csv",
            "--no-fetch-prices",
            "--price-cache-dir",
            str(price_cache),
        ]
    )
    assert rc == 4
    err = capsys.readouterr().err
    assert err == (
        "error: MissingFxRateError: no USD rate available; no prior rate "
        "available in the bundled archive. The bundled TTBR archive may need "
        "updating; see https://github.com/jay-tau/ibkr-fa for the latest snapshot.\n"
    )
    assert "2025-12-31" not in err
    assert not out.exists()
    assert not out.with_name("schedule-fa-a2.csv").exists()


# ---- module entry point ----------------------------------------------------


def test_main_callable_with_no_args_uses_sys_argv(
    monkeypatch: pytest.MonkeyPatch,
    standard_run: tuple[Path, Path, Path],
) -> None:
    """When ``argv`` is :data:`None`, argparse reads from ``sys.argv``."""
    flex, cfg, out = standard_run
    monkeypatch.setattr(
        "sys.argv",
        [
            "ibkr-fa",
            "schedule-fa",
            "--input",
            str(flex),
            "--config",
            str(cfg),
            "--output",
            str(out),
            "--calendar-year",
            "2025",
        ],
    )
    rc = main()
    assert rc == 3
    assert out.exists()


# ============================================================================
# Activity Statement format support (issue #110)
# ============================================================================
# These tests focus on the CLI-specific contract: that the new flag
# parses, that the report's input-format and account-cross-check
# status surface in stdout, and that the masked-account warning
# renders prominently.

_AS_HAPPY_PATH = """\
Statement,Header,Field Name,Field Value,,,,,,,,,,,,,
Statement,Data,BrokerName,Interactive Brokers LLC,,,,,,,,,,,,,
Statement,Data,Title,Activity Statement,,,,,,,,,,,,,
Statement,Data,Period,"January 1, 2025 - December 31, 2025",,,,,,,,,,,,,
Account Information,Header,Field Name,Field Value,,,,,,,,,,,,,
Account Information,Data,Name,TAX PAYER,,,,,,,,,,,,,
Account Information,Data,Account,{account},,,,,,,,,,,,,
Account Information,Data,Account Type,Individual,,,,,,,,,,,,,
Account Information,Data,Base Currency,USD,,,,,,,,,,,,,
Financial Instrument Information,Header,Asset Category,Symbol,Description,Conid,Security ID,Listing Exch,Multiplier,Type,Code,,,,,,
Financial Instrument Information,Data,Stocks,XQAA,SAMPLE ISSUER AA INC-CL A,33333,US99990A3095,NASDAQ,1,COMMON,,,,,,,
Trades,Header,DataDiscriminator,Asset Category,Currency,Symbol,Date/Time,Quantity,T. Price,C. Price,Proceeds,Comm/Fee,Basis,Realized P/L,MTM P/L,Code
Trades,Data,Order,Stocks,USD,XQAA,"2025-11-12, 10:15:00",2,128.40,131.25,-256.80,-0.50,257.30,0,5.70,O
Open Positions,Header,DataDiscriminator,Asset Category,Currency,Symbol,Quantity,Mult,Cost Price,Cost Basis,Close Price,Value,Unrealized P/L,Code,,,
Open Positions,Data,Summary,Stocks,USD,XQAA,2,1,128.65,257.30,133.10,266.20,8.90,,,,
"""  # noqa: E501


def write_activity_statement(tmp_path: Path, *, account: str = "U12345678") -> Path:
    p = tmp_path / "activity-statement.csv"
    p.write_text(with_trades_totals(_AS_HAPPY_PATH.format(account=account)), encoding="utf-8")
    return p


def test_activity_statement_run_surfaces_input_format(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Stdout must announce the detected input format so users have
    an audit trail of which parser ran."""
    as_csv = write_activity_statement(tmp_path)
    cfg = write_user_toml(tmp_path)
    out = tmp_path / "review.csv"
    rc = main(
        [
            "schedule-fa",
            "--input",
            str(as_csv),
            "--config",
            str(cfg),
            "--output",
            str(out),
            "--calendar-year",
            "2025",
        ]
    )
    assert rc == 3  # manual-fill warnings still apply
    assert out.exists()
    captured = capsys.readouterr()
    assert "Input format: activity-statement" in captured.out
    assert "Account: U***5678" in captured.out
    assert "U12345678" not in captured.out


def test_masked_account_without_flag_exits_4(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A masked account ID without --allow-masked-account is a
    domain error: stderr explains the fix, exit code 4. It prints
    neither account number, masked or not (#1047)."""
    as_csv = write_activity_statement(tmp_path, account="U**********")
    cfg = write_user_toml(tmp_path)
    out = tmp_path / "review.csv"
    rc = main(
        [
            "schedule-fa",
            "--input",
            str(as_csv),
            "--config",
            str(cfg),
            "--output",
            str(out),
            "--calendar-year",
            "2025",
        ]
    )
    assert rc == 4
    captured = capsys.readouterr()
    assert captured.err == cli_account_refusal(MASKED_ACCOUNT_REFUSAL)
    for account in ("U**********", "U12345678", "U***5678"):
        assert account.lower() not in captured.err.lower(), account


def test_masked_account_with_flag_runs_with_warning(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """--allow-masked-account lets a masked-statement run complete;
    the CLI prints a loud warning that the safety net was skipped."""
    as_csv = write_activity_statement(tmp_path, account="U**********")
    # Use a deliberately-different user.toml account number to prove
    # the cross-check was actually skipped — if it ran, this would
    # raise ConfigError.
    cfg = write_user_toml(tmp_path, account_number="U99999999")
    out = tmp_path / "review.csv"
    rc = main(
        [
            "schedule-fa",
            "--input",
            str(as_csv),
            "--config",
            str(cfg),
            "--output",
            str(out),
            "--calendar-year",
            "2025",
            "--allow-masked-account",
        ]
    )
    assert rc == 3  # success path
    assert out.exists()
    captured = capsys.readouterr()
    assert "Account: U**********" in captured.out
    assert "cross-check was SKIPPED" in captured.out
    assert "responsible for confirming" in captured.out


def test_mask_with_a_line_break_is_cross_checked_despite_the_flag(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """#854: ``"U***\\n"`` is not IBKR's mask, so --allow-masked-account
    doesn't skip the cross-check, and the comparison refuses it without
    printing the account cell (#1047)."""
    as_csv = write_activity_statement(tmp_path, account='"U***\n"')
    cfg = write_user_toml(tmp_path)
    out = tmp_path / "review.csv"
    rc = main(
        [
            "schedule-fa",
            "--input",
            str(as_csv),
            "--config",
            str(cfg),
            "--output",
            str(out),
            "--calendar-year",
            "2025",
            "--allow-masked-account",
        ]
    )
    assert rc == 4
    assert not out.exists()
    err = capsys.readouterr().err
    assert err == cli_account_refusal(ACCOUNT_MISMATCH_REFUSAL)
    assert "U***" not in err


def test_allow_masked_flag_documented_in_subcommand_help(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """`ibkr-fa schedule-fa --help` mentions the new flag so users
    discover it without reading the source."""
    with pytest.raises(SystemExit):
        main(["schedule-fa", "--help"])
    captured = capsys.readouterr()
    assert "--allow-masked-account" in captured.out
    assert "Show full account number" in captured.out


# ============================================================================
# AY cross-AY-verification warning CLI plumbing (PR #132 / tracker #126)
# ============================================================================


def test_cross_ay_warn_cy_2026_proceeds_with_warning_in_stderr_or_downstream_error(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """CY 2026 no longer raises ConfigError at the AY guard — per
    Codex's review of PR #132, Rule 206(1) + Rule 207(3)(b) of the
    Income-tax Rules, 2026 preserve the SBI TTBR rate source for
    resident FX conversion (carry-forward fallback and FA-specific date
    selection rest on CBDT FAQ / practice). The run proceeds; with
    the bundled TTBR archive not extending to Dec 31 2026, it
    ultimately fails downstream with MissingFxRateError (not the
    AY guard). The refusal names the FA lookup step and the archive's
    latest prior rate date, but not the date it needed (#1047)."""
    open_positions_cy_2026 = _OPEN_POSITIONS_SECTION.replace(
        '"USD","1","STK","COMMON","XQAW","SAMPLE ISSUER AW INC","99990E103","US99990E1038","NASDAQ","US","20251231","10","41.25","412.5","38.4","384","Long","LOT","20250312","20250312"',  # noqa: E501
        '"USD","1","STK","COMMON","XQAW","SAMPLE ISSUER AW INC","99990E103","US99990E1038","NASDAQ","US","20261231","10","41.25","412.5","38.4","384","Long","LOT","20260312","20260312"',  # noqa: E501
    )
    assert open_positions_cy_2026 != _OPEN_POSITIONS_SECTION  # the row was rewritten
    flex = write_flex_csv(
        tmp_path,
        _ACCOUNT_INFO_SECTION,
        open_positions_cy_2026,
        _NO_TRADES_SECTION,
        calendar_year=2026,
    )
    cfg = write_user_toml(tmp_path)
    rc = main(
        [
            "schedule-fa",
            "--input",
            str(flex),
            "--config",
            str(cfg),
            "--output",
            str(tmp_path / "review.csv"),
            "--calendar-year",
            "2026",
        ]
    )
    assert rc == 4
    captured = capsys.readouterr()
    # The error is the downstream MissingFxRateError, NOT the
    # cross-AY guard (which no longer exists as a blocker).
    latest = max(day for day in load_ttbr("USD") if day < date(2026, 12, 31))
    assert captured.err == (
        "error: MissingFxRateError: no USD rate available [Schedule FA "
        "exact-date FX lookup]; latest available prior rate is "
        f"{latest.isoformat()}, beyond the carry-forward limit of 14 days. The "
        "bundled TTBR archive may need updating; see "
        "https://github.com/jay-tau/ibkr-fa for the latest snapshot.\n"
    )
    assert "2026-12-31" not in captured.err


def test_suppress_cross_ay_warning_flag_in_help(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The new flag must appear in `schedule-fa --help` so users
    discover it without reading source code."""
    with pytest.raises(SystemExit):
        main(["schedule-fa", "--help"])
    captured = capsys.readouterr()
    assert "--suppress-cross-ay-warning" in captured.out
    assert "AY 2027-28" in captured.out


def test_render_run_report_includes_cross_ay_warning() -> None:
    """Direct unit test of _render_run_report's cross-AY warning
    branch (fx_policy_appropriate_for_ay=False). Bundled TTBR
    archive doesn't extend to Dec 31 2026 so we can't get a full
    pipeline run with this state, but the render logic itself is
    testable in isolation with a synthetic RunReport."""
    report = RunReport(
        rows_written=1,
        output_path=Path("/tmp/x.csv"),
        manual_fill_columns=(),
        account_id="U12345678",
        account_id_cross_checked=True,
        input_format=IbkrCsvFormat.FLEX_QUERY,
        fx_policy_appropriate_for_ay=False,  # The branch under test
        short_lots_excluded=0,
        orphan_dividends=(),
        portal_labels_assessment_year_start=2026,
        run_outputs=RunOutputs((), ()),
    )
    rendered = _render_run_report(report)
    # Warning text reflects Codex's gazette finding.
    # Rule 206(1) is the resident FX rule; Rule 207(3)(b) is the
    # TTBR definition (corrected from initial Rule 207 citation per
    # Tier-1 Opus tax-law review). Carry-forward fallback and FA-specific
    # date selection rest on CBDT FAQ / practice — not on Rule 206
    # text — so the warning calls out the unverified piece is
    # date-selection + carry-forward, not the rate source itself.
    assert "Rule 206(1)" in rendered
    assert "Rule 207(3)(b)" in rendered
    assert "not yet been verified" in rendered
    assert "CBDT FAQ" in rendered
    assert "--suppress-cross-ay-warning" in rendered
    assert "issues/126" in rendered
    # Lock the 4-paragraph structure so the rendered warning can't
    # regress back to one long unwrapped line (Tier-1 round-3 fix;
    # see GPT-5.5 round-4 finding on substring-only coverage).
    # Paragraphs: (1) AY 2027-28 framework intro,
    # (2) rate-source preserved, (3) unverified components,
    # (4) how to suppress. Each warning paragraph starts with
    # "⚠️" or 3-space indentation; we assert all four are present
    # as distinct, separately appended lines.
    lines = rendered.splitlines()
    # Limit the line-count assertion to the cross-AY warning paragraphs
    # specifically — #63 added a BMA disclosure advisory that lives in
    # the same output and would otherwise drag this count up. Find the
    # cross-AY block by its leading line and the next 3 lines (the
    # 4-paragraph structure is contiguous in cli.py).
    cross_ay_start = next(
        i for i, ln in enumerate(lines) if ln.startswith("⚠️  This run targets AY 2027-28")
    )
    warning_lines = lines[cross_ay_start : cross_ay_start + 4]
    assert len(warning_lines) == 4, warning_lines
    assert warning_lines[0].startswith("⚠️  This run targets AY 2027-28")
    assert warning_lines[1].startswith("   The SBI TTBR rate source is preserved")
    assert warning_lines[2].startswith("   However, the Schedule-FA-specific date-selection")
    assert warning_lines[3].startswith("   See https://github.com/jay-tau/ibkr-fa")


def test_render_run_report_omits_cross_ay_warning_when_appropriate() -> None:
    """Symmetric contract to test_render_run_report_includes_cross_ay_warning:
    when fx_policy_appropriate_for_ay=True, the cross-AY warning
    text MUST be absent. This guards the suppress-flag's contract
    end-to-end. Live CY 2026 path can't exercise this (downstream
    MissingFxRateError), so we use a synthetic RunReport."""
    report = RunReport(
        rows_written=1,
        output_path=Path("/tmp/x.csv"),
        manual_fill_columns=(),
        account_id="U12345678",
        account_id_cross_checked=True,
        input_format=IbkrCsvFormat.FLEX_QUERY,
        fx_policy_appropriate_for_ay=True,
        short_lots_excluded=0,
        orphan_dividends=(),
        portal_labels_assessment_year_start=2026,
        run_outputs=RunOutputs((), ()),
    )
    rendered = _render_run_report(report)
    assert "Rule 206" not in rendered
    assert "Rule 207" not in rendered
    assert "AY 2027-28" not in rendered
    assert "suppress-cross-ay-warning" not in rendered
    assert "not yet been verified" not in rendered


def test_render_run_report_warns_when_a2_table_not_generated() -> None:
    """When [account.custodian] is unconfigured (a2_rows is empty), the FA
    report must NOT silently emit only Table A3 — it must warn that the
    required account-level Table A2 disclosure was skipped and how to
    enable it. Guards the "never be wrong" completeness contract."""
    report = RunReport(
        rows_written=3,
        output_path=Path("/tmp/x.csv"),
        manual_fill_columns=(),
        account_id="U12345678",
        account_id_cross_checked=True,
        input_format=IbkrCsvFormat.ACTIVITY_STATEMENT,
        fx_policy_appropriate_for_ay=True,
        short_lots_excluded=0,
        orphan_dividends=(),
        portal_labels_assessment_year_start=2026,
        # a2_rows defaults to empty — the custodian-unconfigured case.,
        run_outputs=RunOutputs((), ()),
    )
    rendered = _render_run_report(report)
    assert "Table A2" in rendered
    assert "NOT generated" in rendered
    assert "[account.custodian]" in rendered
    assert "incomplete" in rendered
    assert "ibkr-fa init" in rendered


def test_render_run_report_omits_a2_skip_advisory_when_a2_generated() -> None:
    """Symmetric contract: when Table A2 WAS generated (a2_rows set), the
    skip advisory must be absent and the normal A2 write line present."""
    a2_row = FaA2Row(
        country_code="2",
        country_name="UNITED STATES OF AMERICA",
        institution_name="Interactive Brokers LLC",
        institution_address="One Pickwick Plaza, Greenwich, CT",
        institution_zip="06830",
        account_number="U12345678",
        status=FaA2Status.BENEFICIAL_OWNER,
        account_opening_date=date(2024, 1, 1),
        peak_balance_inr=None,
        closing_balance_inr=Decimal("11069.08041662"),  # 123.718346 x 89.47
        nature_of_amount=FaA2NatureOfAmount.NO_AMOUNT,
        gross_amount_inr=Decimal("0"),
        base_currency="USD",
        audit_closing_balance_base_ccy=Decimal("123.718346"),
        audit_ttbr_dec31=Decimal("89.47"),
        audit_account_number_from_config=True,
        audit_nav_cross_checked=True,
        audit_gross_components={},
    )
    report = RunReport(
        rows_written=3,
        output_path=Path("/tmp/x.csv"),
        manual_fill_columns=(),
        account_id="U12345678",
        account_id_cross_checked=True,
        input_format=IbkrCsvFormat.ACTIVITY_STATEMENT,
        fx_policy_appropriate_for_ay=True,
        short_lots_excluded=0,
        orphan_dividends=(),
        a2_rows=(a2_row,),
        a2_output_path=Path("/tmp/x-a2.csv"),
        a2_manual_fill_columns=("Peak Balance During the Period",),
        portal_labels_assessment_year_start=2026,
        run_outputs=RunOutputs((), ()),
    )
    rendered = _render_run_report(report)
    assert "Wrote Schedule FA Table A2" in rendered
    assert "NOT generated" not in rendered
    assert "run `ibkr-fa init`" not in rendered


def _a2_row_with_cash(cash: Decimal | None) -> FaA2Row:
    return FaA2Row(
        country_code="2",
        country_name="UNITED STATES OF AMERICA",
        institution_name="Interactive Brokers LLC",
        institution_address="One Pickwick Plaza, Greenwich, CT",
        institution_zip="06830",
        account_number="U12345678",
        status=FaA2Status.BENEFICIAL_OWNER,
        account_opening_date=date(2024, 1, 1),
        peak_balance_inr=None,
        closing_balance_inr=Decimal("11069.08041662"),  # 123.718346 x 89.47
        nature_of_amount=FaA2NatureOfAmount.NO_AMOUNT,
        gross_amount_inr=Decimal("0"),
        base_currency="USD",
        audit_closing_balance_base_ccy=Decimal("123.718346"),
        audit_ttbr_dec31=Decimal("89.47"),
        audit_account_number_from_config=True,
        audit_nav_cross_checked=True,
        audit_gross_components={},
        audit_period_end_cash_base_ccy=cash,
    )


def _a2_report(a2_row: FaA2Row) -> RunReport:
    return RunReport(
        rows_written=3,
        output_path=Path("/tmp/x.csv"),
        manual_fill_columns=(),
        account_id="U12345678",
        account_id_cross_checked=True,
        input_format=IbkrCsvFormat.ACTIVITY_STATEMENT,
        fx_policy_appropriate_for_ay=True,
        short_lots_excluded=0,
        orphan_dividends=(),
        a2_rows=(a2_row,),
        a2_output_path=Path("/tmp/x-a2.csv"),
        a2_manual_fill_columns=("Peak Balance During the Period",),
        portal_labels_assessment_year_start=2026,
        run_outputs=RunOutputs((), ()),
    )


def test_missing_a2_table_forces_manual_fill_exit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(
        "ibkr_fa.cli.run_fa_a3",
        lambda **kwargs: RunReport(
            rows_written=1,
            output_path=Path(str(kwargs["output_path"])),
            manual_fill_columns=(),
            account_id="U12345678",
            account_id_cross_checked=True,
            input_format=IbkrCsvFormat.ACTIVITY_STATEMENT,
            fx_policy_appropriate_for_ay=True,
            short_lots_excluded=0,
            portal_labels_assessment_year_start=2026,
            run_outputs=RunOutputs((), ()),
        ),
    )

    rc = main(
        [
            "schedule-fa",
            "--input",
            str(tmp_path / "statement.csv"),
            "--config",
            str(tmp_path / "user.toml"),
            "--output",
            str(tmp_path / "schedule-fa.csv"),
            "--calendar-year",
            "2025",
        ]
    )

    captured = capsys.readouterr()
    assert rc == 3
    assert "Table A2" in captured.out
    assert "NOT generated" in captured.out


def test_render_run_report_a2_peak_shows_cash_reference_when_available() -> None:
    """The A2 peak manual-fill guidance cites Rule 114F and surfaces the
    period-end cash/non-cash split as a labelled reference (issue: 'warn
    for the cash amount')."""
    rendered = _render_run_report(_a2_report(_a2_row_with_cash(Decimal("42.518346"))))
    assert "Rule 114F" in rendered
    # USD 123.72 = 42.52 cash + 81.20 non-cash (derived from rounded operands:
    # 123.718346 -> 123.72, 42.518346 -> 42.52, 123.72 - 42.52 = 81.20).
    assert "PERIOD-END (Dec 31) account was USD 123.72" in rendered
    assert "42.52 cash + 81.20 non-cash assets" in rendered
    assert "do NOT simply add them" in rendered


def test_render_run_report_a2_cash_reference_identity_always_adds_up() -> None:
    """The printed identity total = cash + non-cash must hold on screen even
    when independent 2dp rounding of the raw figures would not (the non-cash
    remainder is derived from the rounded operands). Guards a rounding defect:
    e.g. total 2.31452 / cash 0.635108 naively prints 2.31 = 0.64 + 1.68."""
    import re  # noqa: PLC0415

    # closing_balance_inr is unrelated to the base-ccy reference; only the
    # audit base-ccy total + cash drive the printed split.
    row = _a2_row_with_cash(Decimal("0.635108"))
    row = dataclasses.replace(row, audit_closing_balance_base_ccy=Decimal("2.31452"))
    rendered = _render_run_report(_a2_report(row))
    m = re.search(r"USD (\S+) = (\S+) cash \+ (\S+) non-cash", rendered)
    assert m is not None, rendered
    total_s, cash_s, noncash_s = m.groups()
    assert Decimal(cash_s) + Decimal(noncash_s) == Decimal(total_s)


def test_render_run_report_a2_peak_omits_cash_reference_when_absent() -> None:
    """An all-securities account (no Cash row) → no cash component parsed →
    the reference line is omitted, but the Rule 114F peak guidance remains."""
    rendered = _render_run_report(_a2_report(_a2_row_with_cash(None)))
    assert "Rule 114F" in rendered
    assert "PERIOD-END (Dec 31) account" not in rendered


def test_render_run_report_includes_short_lots_excluded_message() -> None:
    """Issue #109 follow-up (docs-reviewer P2): when the pipeline
    excluded N > 0 short positions from FA-A3 output, the CLI
    must surface that fact. Otherwise the user holding shorts
    wouldn't know their shorts were silently dropped and might
    file an incomplete return believing all positions were
    accounted for."""
    report = RunReport(
        rows_written=1,
        output_path=Path("/tmp/x.csv"),
        manual_fill_columns=(),
        account_id="U12345678",
        account_id_cross_checked=True,
        input_format=IbkrCsvFormat.FLEX_QUERY,
        fx_policy_appropriate_for_ay=True,
        short_lots_excluded=2,
        orphan_dividends=(),
        portal_labels_assessment_year_start=2026,
        run_outputs=RunOutputs((), ()),
    )
    rendered = _render_run_report(report)
    assert "Excluded 2 short lot(s)" in rendered
    assert "liabilities" in rendered
    assert "Foreign Equity and Debt Interest" in rendered
    assert "issues/109" in rendered


def test_render_run_report_includes_never_held_orphan_dividend_message() -> None:
    """#142: when the pipeline detected dividend payments for symbols
    that never appeared in the Trades section (typical: spinoff payments
    on never-acquired tickers), the CLI emits the 'never_held' advisory
    pointing at Schedule OS (orphan cash dividends are Income from Other
    Sources). Since #1310 Schedule FA refuses a cash in lieu dated in the
    calendar year, so the advisory no longer names it as a cause.
    """

    report = RunReport(
        rows_written=1,
        output_path=Path("/tmp/x.csv"),
        manual_fill_columns=(),
        account_id="U12345678",
        account_id_cross_checked=True,
        input_format=IbkrCsvFormat.ACTIVITY_STATEMENT,
        fx_policy_appropriate_for_ay=True,
        short_lots_excluded=0,
        orphan_dividends=(
            OrphanDividendInfo(
                symbol=Ticker("XQDM"),
                classification="never_held",
                total_amount_inr=Decimal("1234.56"),
                latest_payment_date=date(2025, 6, 15),
                last_sold_date=None,
            ),
            OrphanDividendInfo(
                symbol=Ticker("SPINOFF"),
                classification="never_held",
                total_amount_inr=Decimal("4321.45"),
                latest_payment_date=date(2025, 9, 30),
                last_sold_date=None,
            ),
        ),
        portal_labels_assessment_year_start=2026,
        run_outputs=RunOutputs((), ()),
    )
    rendered = _render_run_report(report)
    # ₹1235 and ₹4321: 1234.56 and 4321.45 rounded HALF_UP to whole rupees.
    assert (
        "\nℹ️  Dropped dividends for 2 symbol(s) never held as a regular lot"  # noqa: RUF001
        " during the period: XQDM (₹1235 paid 2025-06-15), SPINOFF (₹4321 paid"
        " 2025-09-30). Typical cause is spinoff payments on tickers never"
        " acquired as a regular lot. Orphan cash dividends on a never-held"
        " ticker are typically Schedule OS. See"
        " https://github.com/jay-tau/ibkr-fa/issues/137.\n"
    ) in rendered
    for removed in ("Cash-In-Lieu", "Schedule CG", "deemed sale"):
        assert removed not in rendered


def test_render_run_report_includes_previously_held_orphan_dividend_message() -> None:
    """#142: when the pipeline detected a dividend on a symbol that
    WAS held during the period but sold before pay-date (a synthetic
    pattern: bought 09/09, ex-div 09/23, sold 10/07, paid
    10/21), the CLI emits the distinct 'previously_held' advisory
    that explicitly mentions BOTH the Schedule OS routing (for the
    dividend) AND the Schedule CG routing (for the sale), with the
    last-sold date so the user knows which sale anchors the
    Schedule CG row.
    """

    report = RunReport(
        rows_written=1,
        output_path=Path("/tmp/x.csv"),
        manual_fill_columns=(),
        account_id="U12345678",
        account_id_cross_checked=True,
        input_format=IbkrCsvFormat.ACTIVITY_STATEMENT,
        fx_policy_appropriate_for_ay=True,
        short_lots_excluded=0,
        orphan_dividends=(
            OrphanDividendInfo(
                symbol=Ticker("XQAS"),
                classification="previously_held",
                total_amount_inr=Decimal("555.55"),
                latest_payment_date=date(2025, 10, 21),
                last_sold_date=date(2025, 10, 7),
            ),
        ),
        portal_labels_assessment_year_start=2026,
        run_outputs=RunOutputs((), ()),
    )
    rendered = _render_run_report(report)
    assert "Dropped dividends for 1 symbol(s) not held at Dec 31" in rendered
    assert "previously owned and sold" in rendered
    assert "XQAS" in rendered
    assert "₹556" in rendered  # 555.55 rounded HALF_UP to whole rupees
    assert "paid 2025-10-21" in rendered
    assert "last sold 2025-10-07" in rendered
    assert "Schedule OS" in rendered
    assert "Schedule CG" in rendered
    assert "issues/142" in rendered


def test_render_run_report_orphan_dividends_mixes_both_classifications() -> None:
    """#142: when both classifications coexist (rare but supported),
    the renderer emits BOTH advisory blocks — one per classification —
    so the user gets the correct routing for each kind of orphan."""

    report = RunReport(
        rows_written=1,
        output_path=Path("/tmp/x.csv"),
        manual_fill_columns=(),
        account_id="U12345678",
        account_id_cross_checked=True,
        input_format=IbkrCsvFormat.ACTIVITY_STATEMENT,
        fx_policy_appropriate_for_ay=True,
        short_lots_excluded=0,
        orphan_dividends=(
            OrphanDividendInfo(
                symbol=Ticker("XQDM"),
                classification="never_held",
                total_amount_inr=Decimal("100"),
                latest_payment_date=date(2025, 3, 1),
                last_sold_date=None,
            ),
            OrphanDividendInfo(
                symbol=Ticker("XQAS"),
                classification="previously_held",
                total_amount_inr=Decimal("200"),
                latest_payment_date=date(2025, 10, 21),
                last_sold_date=date(2025, 10, 7),
            ),
        ),
        portal_labels_assessment_year_start=2026,
        run_outputs=RunOutputs((), ()),
    )
    rendered = _render_run_report(report)
    assert "1 symbol(s) not held at Dec 31 but previously owned" in rendered
    assert "1 symbol(s) never held" in rendered
    assert "XQAS" in rendered
    assert "XQDM" in rendered


def test_render_run_report_omits_orphan_dividend_message_when_empty() -> None:
    """Symmetric: when no orphan symbols, the message MUST be
    absent. Empty buy-and-hold accounts and accounts where every
    dividend matched a held lot should not see this line."""
    report = RunReport(
        rows_written=1,
        output_path=Path("/tmp/x.csv"),
        manual_fill_columns=(),
        account_id="U12345678",
        account_id_cross_checked=True,
        input_format=IbkrCsvFormat.ACTIVITY_STATEMENT,
        fx_policy_appropriate_for_ay=True,
        short_lots_excluded=0,
        orphan_dividends=(),
        portal_labels_assessment_year_start=2026,
        run_outputs=RunOutputs((), ()),
    )
    rendered = _render_run_report(report)
    assert "Dropped dividends" not in rendered
    assert "Cash-In-Lieu" not in rendered


def test_render_run_report_omits_short_lots_message_when_zero() -> None:
    """Symmetric: when no shorts were excluded, the message MUST
    be absent — otherwise every buy-and-hold user would see a
    confusing 'Excluded 0 short lot(s)' line."""
    report = RunReport(
        rows_written=1,
        output_path=Path("/tmp/x.csv"),
        manual_fill_columns=(),
        account_id="U12345678",
        account_id_cross_checked=True,
        input_format=IbkrCsvFormat.FLEX_QUERY,
        fx_policy_appropriate_for_ay=True,
        short_lots_excluded=0,
        orphan_dividends=(),
        portal_labels_assessment_year_start=2026,
        run_outputs=RunOutputs((), ()),
    )
    rendered = _render_run_report(report)
    assert "short lot" not in rendered
    assert "Excluded" not in rendered


# ============================================================================
# Issue #145 — US-source-dividend withholding advisory (3 mutually exclusive
# branches keyed off (us_person, has-US-WHT) with the US-source-dividend
# count gating all of them).
# ============================================================================


def test_render_run_report_w8ben_missing_warning_when_non_us_person_zero_wht() -> None:
    """Non-US-person account with US-source dividends but zero
    US-source withholding tax: warn that the W-8BEN at IBKR is
    probably missing/expired, because IBKR should be withholding
    ~25% under the India-US treaty."""
    report = RunReport(
        rows_written=1,
        output_path=Path("/tmp/x.csv"),
        manual_fill_columns=(),
        account_id="U12345678",
        account_id_cross_checked=True,
        input_format=IbkrCsvFormat.ACTIVITY_STATEMENT,
        fx_policy_appropriate_for_ay=True,
        short_lots_excluded=0,
        orphan_dividends=(),
        withholding_tax_entries=0,
        us_person=False,
        us_source_dividend_count=3,
        us_source_withholding_tax_net_paid=Decimal("0"),
        portal_labels_assessment_year_start=2026,
        run_outputs=RunOutputs((), ()),
    )
    rendered = _render_run_report(report)
    assert "W-8BEN" in rendered
    assert "3 US-source dividend(s)" in rendered
    assert "issues/145" in rendered
    # Other two branches must NOT fire.
    assert "expected pattern for a US person" not in rendered
    assert "backup withholding" not in rendered


def test_render_run_report_us_person_ftc_via_1040_when_zero_wht() -> None:
    """US-person account (W-9 on file) with US-source dividends and
    zero US-source withholding: expected pattern — informational
    note that FTC goes via US Form 1040, not Indian Form 67."""
    report = RunReport(
        rows_written=1,
        output_path=Path("/tmp/x.csv"),
        manual_fill_columns=(),
        account_id="U12345678",
        account_id_cross_checked=True,
        input_format=IbkrCsvFormat.ACTIVITY_STATEMENT,
        fx_policy_appropriate_for_ay=True,
        short_lots_excluded=0,
        orphan_dividends=(),
        withholding_tax_entries=0,
        us_person=True,
        us_source_dividend_count=5,
        us_source_withholding_tax_net_paid=Decimal("0"),
        portal_labels_assessment_year_start=2026,
        run_outputs=RunOutputs((), ()),
    )
    rendered = _render_run_report(report)
    assert "US Form 1040" in rendered
    assert "5 US-source dividend(s)" in rendered
    assert "W-9 on file" in rendered
    assert "issues/145" in rendered
    # Other two branches must NOT fire.
    assert "W-8BEN" not in rendered
    assert "backup withholding" not in rendered


def test_render_run_report_us_person_with_unexpected_us_withholding_warning() -> None:
    """US-person account but US-source WHT is present: unusual,
    warn that this could be backup withholding (TIN mismatch) or
    a misconfigured us_person flag."""
    report = RunReport(
        rows_written=1,
        output_path=Path("/tmp/x.csv"),
        manual_fill_columns=(),
        account_id="U12345678",
        account_id_cross_checked=True,
        input_format=IbkrCsvFormat.ACTIVITY_STATEMENT,
        fx_policy_appropriate_for_ay=True,
        short_lots_excluded=0,
        orphan_dividends=(),
        withholding_tax_entries=2,
        us_person=True,
        us_source_dividend_count=4,
        us_source_withholding_tax_net_paid=Decimal("123.45"),
        portal_labels_assessment_year_start=2026,
        run_outputs=RunOutputs((), ()),
    )
    rendered = _render_run_report(report)
    assert "backup withholding" in rendered
    assert "4 US-source dividend(s)" in rendered
    assert "$123.45 net US-source withholding tax" in rendered
    assert "1099-DIV" in rendered
    assert "issues/145" in rendered
    # Other two branches must NOT fire.
    assert "W-8BEN" not in rendered
    assert "US Form 1040" not in rendered


def test_render_run_report_no_advisory_when_zero_us_source_dividends() -> None:
    """No US-source dividends at all (e.g., a portfolio
    with only non-US-incorporated holdings, or any Flex Query
    input): no advisory branch fires regardless of us_person."""
    for us_person, us_wht_count in [(False, 0), (True, 0), (True, 1)]:
        report = RunReport(
            rows_written=1,
            output_path=Path("/tmp/x.csv"),
            manual_fill_columns=(),
            account_id="U12345678",
            account_id_cross_checked=True,
            input_format=IbkrCsvFormat.ACTIVITY_STATEMENT,
            fx_policy_appropriate_for_ay=True,
            short_lots_excluded=0,
            orphan_dividends=(),
            withholding_tax_entries=us_wht_count,
            us_person=us_person,
            us_source_dividend_count=0,
            us_source_withholding_tax_net_paid=Decimal(us_wht_count),
            portal_labels_assessment_year_start=2026,
            run_outputs=RunOutputs((), ()),
        )
        rendered = _render_run_report(report)
        assert "W-8BEN" not in rendered, f"us_person={us_person}, us_wht={us_wht_count}"
        assert "US Form 1040" not in rendered
        assert "backup withholding" not in rendered


def test_render_run_report_non_us_person_with_correct_us_withholding_no_advisory() -> None:
    """Non-US-person account with US-source dividends AND US-source
    withholding tax: the system is working as intended (W-8BEN on
    file, IBKR is withholding India treaty rate). No advisory."""
    report = RunReport(
        rows_written=1,
        output_path=Path("/tmp/x.csv"),
        manual_fill_columns=(),
        account_id="U12345678",
        account_id_cross_checked=True,
        input_format=IbkrCsvFormat.ACTIVITY_STATEMENT,
        fx_policy_appropriate_for_ay=True,
        short_lots_excluded=0,
        orphan_dividends=(),
        withholding_tax_entries=4,
        us_person=False,
        us_source_dividend_count=4,
        us_source_withholding_tax_net_paid=Decimal("18.50"),
        portal_labels_assessment_year_start=2026,
        run_outputs=RunOutputs((), ()),
    )
    rendered = _render_run_report(report)
    assert "W-8BEN" not in rendered
    assert "US Form 1040" not in rendered
    assert "backup withholding" not in rendered


def test_render_run_report_surfaces_interest_credits_count_when_present() -> None:
    """#139 follow-up: when the Activity Statement Interest section
    produced realized USD credit-interest rows, the schedule-fa CLI
    surfaces the count and says where they go: Table A2's gross
    amount credited, not Table A3 (#905)."""
    report = RunReport(
        rows_written=1,
        output_path=Path("/tmp/x.csv"),
        manual_fill_columns=(),
        account_id="U12345678",
        account_id_cross_checked=True,
        input_format=IbkrCsvFormat.ACTIVITY_STATEMENT,
        fx_policy_appropriate_for_ay=True,
        short_lots_excluded=0,
        orphan_dividends=(),
        withholding_tax_entries=0,
        interest_credits_entries=3,
        us_person=False,
        us_source_dividend_count=0,
        us_source_withholding_tax_net_paid=Decimal(0),
        portal_labels_assessment_year_start=2026,
        run_outputs=RunOutputs((), ()),
    )
    rendered = _render_run_report(report)
    assert "Parsed 3 realized" in rendered
    assert "credit-interest" in rendered
    # It no longer claims schedule-fa ignores interest: A2 uses it (#186).
    assert "does not emit" not in rendered
    assert "Table A2 adds the calendar year's credits" in rendered
    assert "Table A3 doesn't use them" in rendered
    assert "Debit interest (margin interest you paid) isn't income" in rendered
    # No unproven rows, so no unproven warning.
    assert "unproven" not in rendered


def test_render_run_report_names_each_unproven_interest_row() -> None:
    """#905: an unproven Interest row leaves A2's Interest for manual
    fill; the report names each row's file line and why, and says
    which commands refuse the statement. It never prints a row's
    Description, amount or date, which are input (#1047)."""
    rows = (
        InterestNonCredit(
            group=InterestRowGroup.UNPROVEN_NON_CREDIT,
            amount=Decimal("-4.00"),
            currency="USD",
            row_date=date(2025, 3, 31),
            description="USD Credit Interest for Mar-2025",
            file_line=42,
        ),
        InterestNonCredit(
            group=InterestRowGroup.UNPROVEN_ACCRUAL,
            amount=Decimal("1234567.89"),
            currency="USD",
            row_date=date(2025, 3, 1),
            description="Synthetic Holder Two U1234567 accrual adjustment",
            file_line=43,
        ),
    )
    report = RunReport(
        rows_written=1,
        output_path=Path("/tmp/x.csv"),
        manual_fill_columns=(),
        account_id="U12345678",
        account_id_cross_checked=True,
        input_format=IbkrCsvFormat.ACTIVITY_STATEMENT,
        fx_policy_appropriate_for_ay=True,
        short_lots_excluded=0,
        interest_unproven_rows=rows,
        portal_labels_assessment_year_start=2026,
        run_outputs=RunOutputs((), ()),
    )
    rendered = _render_run_report(report)
    assert "Table A2 leaves its Interest amount for manual fill" in rendered
    assert "schedule-os, schedule-fsi, schedule-tr and form-67 refuse" in rendered
    lines = rendered.splitlines()
    assert "     - line 42 (a negative or zero amount that isn't debit interest)" in lines
    assert "     - line 43 (its description mentions an accrual)" in lines
    for row in rows:
        for value in (row.description, str(row.amount), row.row_date.isoformat()):
            assert value.lower() not in rendered.lower(), value
    assert "Synthetic Holder Two".lower() not in rendered.lower()
    assert "U1234567".lower() not in rendered.lower()


def test_render_run_report_no_interest_advisory_when_zero_credits() -> None:
    """No interest section in the statement (or all rows were debit
    / accrual / footer-skipped): no advisory fires. Keeps the
    output free of noise when the section simply doesn't apply."""
    report = RunReport(
        rows_written=1,
        output_path=Path("/tmp/x.csv"),
        manual_fill_columns=(),
        account_id="U12345678",
        account_id_cross_checked=True,
        input_format=IbkrCsvFormat.ACTIVITY_STATEMENT,
        fx_policy_appropriate_for_ay=True,
        short_lots_excluded=0,
        orphan_dividends=(),
        withholding_tax_entries=0,
        interest_credits_entries=0,
        us_person=False,
        us_source_dividend_count=0,
        us_source_withholding_tax_net_paid=Decimal(0),
        portal_labels_assessment_year_start=2026,
        run_outputs=RunOutputs((), ()),
    )
    rendered = _render_run_report(report)
    assert "credit-interest" not in rendered
    assert "Schedule OS interest-income" not in rendered


def test_suppress_cross_ay_warning_flag_plumbs_to_pipeline(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The --suppress-cross-ay-warning CLI flag must actually reach
    run_fa_a3 as ``suppress_cross_ay_warning=True``. Without this
    test, a future regression in argparse wiring (e.g., a renamed
    dest or a missing kwarg in _run_schedule_fa) could silently
    drop the flag and the existing tests — which monkeypatch
    RunReport directly or assert on rendered-text branches —
    wouldn't catch it. We monkeypatch run_fa_a3 to capture the
    kwargs it received."""
    captured: dict[str, object] = {}

    def fake_run_fa_a3(**kwargs: object) -> RunReport:
        captured.update(kwargs)
        return RunReport(
            rows_written=0,
            output_path=Path(str(kwargs["output_path"])),
            manual_fill_columns=(),
            account_id="U12345678",
            account_id_cross_checked=True,
            input_format=IbkrCsvFormat.FLEX_QUERY,
            fx_policy_appropriate_for_ay=True,
            short_lots_excluded=0,
            orphan_dividends=(),
            a2_rows=(_a2_row_with_cash(Decimal("0")),),
            portal_labels_assessment_year_start=2026,
            run_outputs=RunOutputs((), ()),
        )

    monkeypatch.setattr("ibkr_fa.cli.run_fa_a3", fake_run_fa_a3)

    flex = write_flex_csv(tmp_path, _ACCOUNT_INFO_SECTION, _OPEN_POSITIONS_SECTION)
    cfg = write_user_toml(tmp_path)
    rc = main(
        [
            "schedule-fa",
            "--input",
            str(flex),
            "--config",
            str(cfg),
            "--output",
            str(tmp_path / "review.csv"),
            "--calendar-year",
            "2026",
            "--suppress-cross-ay-warning",
        ]
    )

    assert rc == 0
    assert captured["suppress_cross_ay_warning"] is True
    assert captured["calendar_year"] == 2026


def test_suppress_cross_ay_warning_flag_default_false(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Symmetric contract: when --suppress-cross-ay-warning is NOT
    passed, run_fa_a3 must receive ``suppress_cross_ay_warning=False``.
    Guards against a regression where the flag's default flips."""
    captured: dict[str, object] = {}

    def fake_run_fa_a3(**kwargs: object) -> RunReport:
        captured.update(kwargs)
        return RunReport(
            rows_written=0,
            output_path=Path(str(kwargs["output_path"])),
            manual_fill_columns=(),
            account_id="U12345678",
            account_id_cross_checked=True,
            input_format=IbkrCsvFormat.FLEX_QUERY,
            fx_policy_appropriate_for_ay=True,
            short_lots_excluded=0,
            orphan_dividends=(),
            a2_rows=(_a2_row_with_cash(Decimal("0")),),
            portal_labels_assessment_year_start=2026,
            run_outputs=RunOutputs((), ()),
        )

    monkeypatch.setattr("ibkr_fa.cli.run_fa_a3", fake_run_fa_a3)

    flex = write_flex_csv(tmp_path, _ACCOUNT_INFO_SECTION, _OPEN_POSITIONS_SECTION)
    cfg = write_user_toml(tmp_path)
    rc = main(
        [
            "schedule-fa",
            "--input",
            str(flex),
            "--config",
            str(cfg),
            "--output",
            str(tmp_path / "review.csv"),
            "--calendar-year",
            "2025",
        ]
    )

    assert rc == 0
    assert captured["suppress_cross_ay_warning"] is False


# ============================================================================
# #141 --accept-anonymised-statement flag plumbing
# ============================================================================


def test_accept_anonymised_statement_flag_in_schedule_fa_help(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """`ibkr-fa schedule-fa --help` documents the new flag."""
    with pytest.raises(SystemExit):
        main(["schedule-fa", "--help"])
    captured = capsys.readouterr()
    assert "--accept-anonymised-statement" in captured.out


def test_accept_anonymised_statement_flag_in_schedule_os_help(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """`ibkr-fa schedule-os --help` documents the new flag."""
    with pytest.raises(SystemExit):
        main(["schedule-os", "--help"])
    captured = capsys.readouterr()
    assert "--accept-anonymised-statement" in captured.out


def test_accept_anonymised_statement_flag_in_schedule_cg_help(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """`ibkr-fa schedule-cg --help` documents the new flag."""
    with pytest.raises(SystemExit):
        main(["schedule-cg", "--help"])
    captured = capsys.readouterr()
    assert "--accept-anonymised-statement" in captured.out


def test_accept_anonymised_statement_flag_in_schedule_cg_ltcg_help(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """`ibkr-fa schedule-cg-ltcg --help` documents the new flag."""
    with pytest.raises(SystemExit):
        main(["schedule-cg-ltcg", "--help"])
    captured = capsys.readouterr()
    assert "--accept-anonymised-statement" in captured.out


def test_run_report_render_includes_identity_fallback_warning() -> None:
    """#141: when identity_supplied_from_fallback is non-empty, the
    rendered FA report includes a loud user-attested-identity warning.
    """
    report = RunReport(
        rows_written=1,
        output_path=Path("/tmp/review.csv"),
        manual_fill_columns=("Peak value",),
        account_id="U12345678",
        account_id_cross_checked=True,
        input_format=IbkrCsvFormat.ACTIVITY_STATEMENT,
        fx_policy_appropriate_for_ay=True,
        short_lots_excluded=0,
        identity_supplied_from_fallback=frozenset({"Name", "Account"}),
        portal_labels_assessment_year_start=2026,
        run_outputs=RunOutputs((), ()),
    )
    out = _render_run_report(report)
    assert "Identity field(s)" in out
    assert "'Account'" in out
    assert "'Name'" in out
    assert "user-attested" in out
    assert "issues/141" in out


def test_run_report_render_omits_identity_fallback_warning_when_empty() -> None:
    """#141: the warning block is suppressed entirely when no
    fallback was used (the common case — no behavior change for
    users not passing --accept-anonymised-statement)."""
    report = RunReport(
        rows_written=1,
        output_path=Path("/tmp/review.csv"),
        manual_fill_columns=("Peak value",),
        account_id="U12345678",
        account_id_cross_checked=True,
        input_format=IbkrCsvFormat.ACTIVITY_STATEMENT,
        fx_policy_appropriate_for_ay=True,
        short_lots_excluded=0,
        portal_labels_assessment_year_start=2026,
        run_outputs=RunOutputs((), ()),
    )
    out = _render_run_report(report)
    assert "Identity field(s)" not in out
    assert "user-attested" not in out


def test_run_report_rejects_unexpected_identity_fallback_field() -> None:
    """#141: the RunReport invariant refuses any field name outside
    {'Name', 'Account'} in identity_supplied_from_fallback — drift
    detector for any future dispatcher bug that leaks an unexpected
    field through the parser/pipeline boundary.
    """
    with pytest.raises(InvariantError):
        RunReport(
            rows_written=1,
            output_path=Path("/tmp/review.csv"),
            manual_fill_columns=("Peak value",),
            account_id="U12345678",
            account_id_cross_checked=True,
            input_format=IbkrCsvFormat.ACTIVITY_STATEMENT,
            fx_policy_appropriate_for_ay=True,
            short_lots_excluded=0,
            identity_supplied_from_fallback=frozenset({"Account Type"}),
            portal_labels_assessment_year_start=2026,
            run_outputs=RunOutputs((), ()),
        )


def test_identity_fallback_warning_account_only_says_trivially_passed() -> None:
    """#141 / T1-correctness PR #166: when Account specifically came
    from fallback, the warning text asserts the cross-check passed
    trivially against a user-attested value."""
    report = RunReport(
        rows_written=1,
        output_path=Path("/tmp/review.csv"),
        manual_fill_columns=("Peak value",),
        account_id="U12345678",
        account_id_cross_checked=True,
        input_format=IbkrCsvFormat.ACTIVITY_STATEMENT,
        fx_policy_appropriate_for_ay=True,
        short_lots_excluded=0,
        identity_supplied_from_fallback=frozenset({"Account"}),
        portal_labels_assessment_year_start=2026,
        run_outputs=RunOutputs((), ()),
    )
    out = _render_run_report(report)
    assert "passed trivially" in out
    assert "Account row WAS present" not in out


def test_identity_fallback_warning_name_only_says_cross_check_statement_verified() -> None:
    """#141 / T1-correctness PR #166: when only Name came from
    fallback (Account row was present), the warning text clarifies
    the cross-check remained statement-verified — only the holder
    name was user-attested. Independent Name-only / Account-only
    fallback paths are parser-supported even though IBKR's current
    privacy exporter hides both fields together."""
    report = RunReport(
        rows_written=1,
        output_path=Path("/tmp/review.csv"),
        manual_fill_columns=("Peak value",),
        account_id="U12345678",
        account_id_cross_checked=True,
        input_format=IbkrCsvFormat.ACTIVITY_STATEMENT,
        fx_policy_appropriate_for_ay=True,
        short_lots_excluded=0,
        identity_supplied_from_fallback=frozenset({"Name"}),
        portal_labels_assessment_year_start=2026,
        run_outputs=RunOutputs((), ()),
    )
    out = _render_run_report(report)
    assert "Account row WAS present" in out
    assert "passed trivially" not in out


def test_accept_anonymised_statement_flag_plumbs_to_pipeline(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """T4 PR #166 P3-1/2 symmetric to test_suppress_cross_ay_warning_flag_plumbs_to_pipeline:
    the --accept-anonymised-statement CLI flag must actually reach
    run_fa_a3 as accept_anonymised_statement=True. Guards against
    a future regression in argparse wiring (renamed dest, missing
    kwarg in _run_schedule_fa).
    """
    captured: dict[str, object] = {}

    def fake_run_fa_a3(**kwargs: object) -> RunReport:
        captured.update(kwargs)
        return RunReport(
            rows_written=0,
            output_path=Path(str(kwargs["output_path"])),
            manual_fill_columns=(),
            account_id="U12345678",
            account_id_cross_checked=True,
            input_format=IbkrCsvFormat.FLEX_QUERY,
            fx_policy_appropriate_for_ay=True,
            short_lots_excluded=0,
            orphan_dividends=(),
            a2_rows=(_a2_row_with_cash(Decimal("0")),),
            portal_labels_assessment_year_start=2026,
            run_outputs=RunOutputs((), ()),
        )

    monkeypatch.setattr("ibkr_fa.cli.run_fa_a3", fake_run_fa_a3)

    flex = write_flex_csv(tmp_path, _ACCOUNT_INFO_SECTION, _OPEN_POSITIONS_SECTION)
    cfg = write_user_toml(tmp_path)
    rc = main(
        [
            "schedule-fa",
            "--input",
            str(flex),
            "--config",
            str(cfg),
            "--output",
            str(tmp_path / "review.csv"),
            "--calendar-year",
            "2025",
            "--accept-anonymised-statement",
        ]
    )

    assert rc == 0
    assert captured["accept_anonymised_statement"] is True


def test_accept_anonymised_statement_flag_default_false(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """T4 PR #166 P3-2 symmetric to test_suppress_cross_ay_warning_flag_default_false:
    when --accept-anonymised-statement is NOT passed, run_fa_a3 must
    receive accept_anonymised_statement=False. Guards against a
    regression where the flag's default flips."""
    captured: dict[str, object] = {}

    def fake_run_fa_a3(**kwargs: object) -> RunReport:
        captured.update(kwargs)
        return RunReport(
            rows_written=0,
            output_path=Path(str(kwargs["output_path"])),
            manual_fill_columns=(),
            account_id="U12345678",
            account_id_cross_checked=True,
            input_format=IbkrCsvFormat.FLEX_QUERY,
            fx_policy_appropriate_for_ay=True,
            short_lots_excluded=0,
            orphan_dividends=(),
            a2_rows=(_a2_row_with_cash(Decimal("0")),),
            portal_labels_assessment_year_start=2026,
            run_outputs=RunOutputs((), ()),
        )

    monkeypatch.setattr("ibkr_fa.cli.run_fa_a3", fake_run_fa_a3)

    flex = write_flex_csv(tmp_path, _ACCOUNT_INFO_SECTION, _OPEN_POSITIONS_SECTION)
    cfg = write_user_toml(tmp_path)
    rc = main(
        [
            "schedule-fa",
            "--input",
            str(flex),
            "--config",
            str(cfg),
            "--output",
            str(tmp_path / "review.csv"),
            "--calendar-year",
            "2025",
        ]
    )

    assert rc == 0
    assert captured["accept_anonymised_statement"] is False
