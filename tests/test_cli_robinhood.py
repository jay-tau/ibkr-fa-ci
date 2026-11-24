"""CLI checks for the Robinhood command family."""

from __future__ import annotations

import os
from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

import ibkr_fa.pipeline_robinhood as pipeline
import ibkr_fa.pipeline_robinhood_core as core
import ibkr_fa.pipeline_robinhood_option_prices as option_prices
from ibkr_fa import cli
from ibkr_fa.compute.robinhood_options import RobinhoodOptionTreatment
from ibkr_fa.config import PricesConfig
from ibkr_fa.exceptions import MissingFxRateError
from ibkr_fa.output._cleanup import RunOutputs
from ibkr_fa.output.income_bundle import INCOME_REVIEW_FILENAMES
from ibkr_fa.pipeline import FinancialYear
from ibkr_fa.pipeline_robinhood import (
    ROBINHOOD_FNO_FEMA_WARNING,
    RobinhoodAllReport,
    RobinhoodLegReport,
    RobinhoodLegStatus,
    RobinhoodPipelineError,
)
from ibkr_fa.pipeline_robinhood_outputs import CG_DEBUG_FILENAMES
from ibkr_fa.prices.series import PriceSeries
from ibkr_fa.types import Ticker
from tests._dec31_close import MARKER_SOURCE_DATE
from tests._ftc_warnings import BLANK_RELIEF_TIN_WARNING
from tests._massive_option_replies import MR_MARKERS, mr_bars, mr_overview
from tests._no_echo import assert_no_markers
from tests._robinhood_cg import cg_result
from tests._robinhood_cli import LEG_LINE as _LEG_LINE
from tests._robinhood_cli import robinhood_argv as _common
from tests._robinhood_cli import robinhood_main
from tests._robinhood_cli import stale_fa_outputs as _stale_fa_outputs
from tests._robinhood_fa_blocker_lines import BY_HAND, CLOSING_CAUSE, FA_UNRESOLVED, REFERENCES
from tests._robinhood_fa_inputs import FA_CUSIP as _CUSIP
from tests._robinhood_fa_inputs import FA_ISIN as _ISIN
from tests._robinhood_fa_inputs import FA_SYMBOL as _SYMBOL
from tests._robinhood_fa_inputs import fa_price_series as _prices
from tests._robinhood_fa_runs import DEC31_SESSION_REFUSALS as _SESSION_REFUSALS
from tests._robinhood_fa_runs import a3_peak_and_closing_cells as _a3_cells
from tests._robinhood_fa_runs import (
    compute_fa_one_dollar_three_shares as _compute_one_dollar_three_shares,
)
from tests._robinhood_fa_runs import compute_fa_open as _compute_open
from tests._robinhood_option_audit import held_call_options, serve_massive
from tests._robinhood_pipeline_stubs import OTHER_ACCOUNT as _OTHER_ACCOUNT
from tests._robinhood_pipeline_stubs import TIN_MARKER as _TIN_MARKER
from tests._robinhood_pipeline_stubs import complete_fa as _complete_fa
from tests._robinhood_pipeline_stubs import json_fa as _json_fa
from tests._robinhood_pipeline_stubs import stand_in_bundle, stand_in_options, stub_cg_run
from tests._robinhood_pipeline_stubs import stub_relief_run as _stub_relief_run
from tests._robinhood_pipeline_stubs import stub_successful_all as _stub_successful_all
from tests._robinhood_pipeline_stubs import stub_writing_all as _stub_writing_all
from tests._robinhood_refusals import (
    FNO_FEMA_WARNING,
    MASSIVE_CLOSE_REFUSAL,
    MASSIVE_CORRECTION_REFUSAL,
    MASSIVE_COUNT_REFUSAL,
    MASSIVE_JSON_REFUSAL,
    MASSIVE_MULTIPLIER_REFUSAL,
    MASSIVE_ROW_REFUSAL,
    MASSIVE_STATUS_REFUSAL,
    MASSIVE_STRIKE_REFUSAL,
    MASSIVE_TIMESTAMP_RANGE_REFUSAL,
    MASSIVE_TIMESTAMP_REFUSAL,
    MASSIVE_VOLUME_REFUSAL,
    PRIVATE_OUTPUT_DIR_REFUSAL,
)


def test_parser_registers_all_robinhood_commands(tmp_path: Path) -> None:
    parser = cli._build_parser()
    for command in (
        "robinhood-cg",
        "robinhood-income",
        "robinhood-fa",
        "robinhood-options",
        "robinhood-all",
    ):
        args = parser.parse_args(_common(command, tmp_path))
        assert args.command == command


@pytest.mark.parametrize(
    ("command", "extra_args", "expected_specific"),
    [
        (
            "robinhood-cg",
            (
                "--no-fetch-prices",
                "--price-cache-dir",
                "<cache>",
                "--debug",
                "--format",
                "xlsx",
            ),
            {
                "format_id": "xlsx",
                "debug": True,
                "fetch_prices": False,
                "price_cache_dir": "<cache>",
            },
        ),
        (
            "robinhood-income",
            (
                "--no-fetch-prices",
                "--price-cache-dir",
                "<cache>",
                "--debug",
                "--suppress-cross-ay-warning",
            ),
            {
                "suppress_cross_ay_warning": True,
                "debug": True,
                "fetch_prices": False,
                "price_cache_dir": "<cache>",
            },
        ),
        (
            "robinhood-fa",
            ("--no-fetch-prices", "--price-cache-dir", "<cache>", "--format", "xlsx"),
            {"format_id": "xlsx", "fetch_prices": False, "price_cache_dir": "<cache>"},
        ),
        ("robinhood-options", (), {}),
        (
            "robinhood-all",
            (
                "--no-fetch-prices",
                "--price-cache-dir",
                "<cache>",
                "--debug",
                "--suppress-cross-ay-warning",
                "--format",
                "xlsx",
            ),
            {
                "format_id": "xlsx",
                "fetch_prices": False,
                "price_cache_dir": "<cache>",
                "suppress_cross_ay_warning": True,
                "debug": True,
            },
        ),
        (
            "robinhood-cg",
            (),
            {
                "format_id": "review-csv",
                "debug": False,
                "fetch_prices": True,
                "price_cache_dir": None,
            },
        ),
        (
            "robinhood-income",
            (),
            {
                "suppress_cross_ay_warning": False,
                "debug": False,
                "fetch_prices": True,
                "price_cache_dir": None,
            },
        ),
        (
            "robinhood-fa",
            (),
            {"format_id": "review-csv", "fetch_prices": True, "price_cache_dir": None},
        ),
        (
            "robinhood-all",
            (),
            {
                "format_id": "review-csv",
                "fetch_prices": True,
                "price_cache_dir": None,
                "suppress_cross_ay_warning": False,
                "debug": False,
            },
        ),
    ],
)
def test_robinhood_cli_propagates_applicable_flags(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    command: str,
    extra_args: tuple[str, ...],
    expected_specific: dict[str, object],
) -> None:
    cache = tmp_path / "cache"
    captured: dict[str, object] = {}

    def fake_runner(**kwargs: object) -> RobinhoodLegReport | RobinhoodAllReport:
        captured.update(kwargs)
        if command == "robinhood-all":
            return RobinhoodAllReport(
                output_dir=tmp_path / "out",
                financial_year=FinancialYear(2025),
                calendar_year=2025,
                legs=(),
                run_outputs=RunOutputs((), ()),
            )
        return RobinhoodLegReport(command, RobinhoodLegStatus.OK, run_outputs=RunOutputs((), ()))

    monkeypatch.setattr(cli, f"run_{command.replace('-', '_')}", fake_runner)
    rendered_args = [str(cache) if value == "<cache>" else value for value in extra_args]

    assert cli.main([*_common(command, tmp_path), *rendered_args]) == 0
    expected: dict[str, object] = {
        "input_dir": tmp_path / "bundle",
        "config_path": tmp_path / "user.toml",
        "output_dir": tmp_path / "out",
        "financial_year": FinancialYear(2025),
        "itr_form": "ITR-2",
    }
    expected.update(
        {key: cache if value == "<cache>" else value for key, value in expected_specific.items()}
    )
    assert captured == expected


def test_manual_leg_returns_exit_3(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    report = RobinhoodLegReport(
        "options",
        RobinhoodLegStatus.MANUAL,
        (tmp_path / "robinhood-options.csv",),
        ("manual option schedules are required",),
        (ROBINHOOD_FNO_FEMA_WARNING,),
        run_outputs=RunOutputs((), ()),
    )
    monkeypatch.setattr(cli, "run_robinhood_options", lambda **_kwargs: report)
    assert cli.main(_common("robinhood-options", tmp_path)) == 3
    output = capsys.readouterr().out
    assert "manual" in output
    assert "manual option schedules" in output
    assert "FEMA/LRS F&O warning" in output


def test_domain_failure_returns_exit_4(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(
        cli,
        "run_robinhood_cg",
        lambda **_kwargs: (_ for _ in ()).throw(RobinhoodPipelineError("unresolved evidence")),
    )
    assert cli.main(_common("robinhood-cg", tmp_path)) == 4
    assert "unresolved evidence" in capsys.readouterr().err


def test_typed_error_inside_fno_wrapper_returns_exit_4(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    error = MissingFxRateError(
        currency="USD",
        last_available=None,
        max_carry_forward_days=0,
    )
    monkeypatch.setattr(pipeline, "_load", lambda **_kwargs: object())
    monkeypatch.setattr(pipeline, "_compute_options", lambda *_args: SimpleNamespace(trades=()))
    monkeypatch.setattr(pipeline, "_compute_cg", lambda *_args: (_ for _ in ()).throw(error))
    assert cli.main(_common("robinhood-cg", tmp_path)) == 4
    assert capsys.readouterr().err == f"error: {error}\n"


@pytest.mark.parametrize(
    ("worst_status", "expected_exit"),
    [
        (RobinhoodLegStatus.MANUAL, 3),
        (RobinhoodLegStatus.ERROR, 4),
    ],
)
def test_all_summary_and_worst_status(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    worst_status: RobinhoodLegStatus,
    expected_exit: int,
) -> None:
    report = RobinhoodAllReport(
        output_dir=tmp_path,
        financial_year=FinancialYear(2025),
        calendar_year=2025,
        legs=(
            RobinhoodLegReport("equity-cg", RobinhoodLegStatus.OK, run_outputs=RunOutputs((), ())),
            RobinhoodLegReport(
                "fa-a1",
                worst_status,
                reasons=("Program Bank requires manual A1",),
                run_outputs=RunOutputs((), ()),
            ),
        ),
        run_outputs=RunOutputs((), ()),
    )
    monkeypatch.setattr(cli, "run_robinhood_all", lambda **_kwargs: report)
    assert cli.main(_common("robinhood-all", tmp_path)) == expected_exit
    output = capsys.readouterr().out
    assert "ROBINHOOD SUMMARY" in output
    assert "fa-a1" in output
    assert f"STATUS: {worst_status.value}" in output


@pytest.mark.parametrize(("tin_country", "expected_exit"), [("GB", 3), ("US", 0)])
@pytest.mark.parametrize("command", ["robinhood-income", "robinhood-all"])
def test_relief_row_without_a_tin_exits_3(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    command: str,
    tin_country: str,
    expected_exit: int,
) -> None:
    """#1084 through the real pipeline status: the US relief row needs its own
    TIN, and the run echoes neither the TIN nor an account number."""
    _stub_relief_run(tmp_path, monkeypatch, tin_country)

    assert cli.main([*_common(command, tmp_path), "--no-fetch-prices"]) == expected_exit

    captured = capsys.readouterr()
    assert captured.out.count(BLANK_RELIEF_TIN_WARNING) == int(expected_exit == 3)
    fsi = (tmp_path / "out" / "schedule-fsi.csv").read_text(encoding="utf-8")
    assert (_TIN_MARKER in fsi) is (tin_country == "US")
    assert_no_markers(captured.out + captured.err, _TIN_MARKER, _OTHER_ACCOUNT)


# #1550: the a3-closing line for _compute_open's one open lot.
_CLOSING = f"{CLOSING_CAUSE} {REFERENCES}Robinhood security 1 (lots 0:2). {BY_HAND}."


@_SESSION_REFUSALS
@pytest.mark.parametrize(
    ("format_id", "written"),
    [
        ("review-csv", ["schedule-fa-a2.csv", "schedule-fa-a3.csv"]),
        ("cbdt-json-fragment", ["schedule-fa.json"]),
    ],
)
def test_dec31_session_refusal_blocks_robinhood_fa_and_removes_its_outputs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    format_id: str,
    written: list[str],
    session_refusal: Callable[[pytest.MonkeyPatch], dict[Ticker, PriceSeries]],
) -> None:
    """#1165 through the real Robinhood A3 compute, every other leg stubbed to
    succeed. With valid Dec-31 evidence the run files the open lot; once the
    resolver refuses the series for its required session, that is the run's
    only blocker. The run exits 4 with the fixed blocker message and that
    blocker's a3-closing line (#1550), prints no success report, and no FA
    filing output of the earlier run remains."""
    _stub_successful_all(monkeypatch, _json_fa())
    valid = _prices()
    monkeypatch.setattr(pipeline, "_compute_fa", lambda *_args, **_kwargs: _compute_open(valid))
    argv = [*_common("robinhood-fa", tmp_path), "--format", format_id]
    out = tmp_path / "out"
    assert cli.main(argv) == 0
    assert sorted(path.name for path in out.iterdir()) == written
    capsys.readouterr()
    blocked = session_refusal(monkeypatch)
    monkeypatch.setattr(pipeline, "_compute_fa", lambda *_args, **_kwargs: _compute_open(blocked))

    assert cli.main(argv) == 4

    captured = capsys.readouterr()
    assert captured.err == f"error: {FA_UNRESOLVED}\n{_CLOSING}\n"
    assert captured.out == ""
    assert_no_markers(captured.err, _SYMBOL, _CUSIP, _ISIN, MARKER_SOURCE_DATE)
    assert list(out.iterdir()) == []


def test_robinhood_fa_files_a_lone_lots_exact_statement_total(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """#1242 through the CLI, with the real Robinhood A3 compute and every other
    leg stubbed: 3 shares worth $1.00 at ₹82.50/$ file Peak 83 and Closing
    balance 83 (main filed 82 and 82)."""
    _stub_successful_all(monkeypatch, _json_fa())
    result = _compute_one_dollar_three_shares()
    monkeypatch.setattr(pipeline, "_compute_fa", lambda *_args, **_kwargs: result)

    assert cli.main([*_common("robinhood-fa", tmp_path), "--format", "filing-csv"]) == 0

    assert capsys.readouterr().err == ""
    assert _a3_cells(tmp_path / "out" / "schedule-fa-a3.csv") == ["83", "83"]


# --- #1169: malformed Massive evidence through the real reader ---------------


def _stub_real_option_prices(monkeypatch: pytest.MonkeyPatch, *, has_trades: bool = True) -> None:
    """After a stubbed run's setup, let the FA price step run for real: the
    option price audit, the Massive reader and the FA gate, for one held XQMR
    contract whose key file sits beside ``user.toml``."""
    options = stand_in_options(
        has_trades=True, treatment=RobinhoodOptionTreatment.SPECULATIVE_BUSINESS
    )
    priced, _contract = held_call_options("XQMR")
    options.trades = priced.trades if has_trades else ()
    options.lot_slices = priced.lot_slices
    bundle = stand_in_bundle(has_options=True)
    bundle.config = SimpleNamespace(prices=PricesConfig(massive_api_key_file="env"))
    monkeypatch.setattr(pipeline, "_load", lambda **_kwargs: bundle)
    monkeypatch.setattr(pipeline, "_reconstruct_options", lambda *_args: options)
    monkeypatch.setattr(pipeline, "_compute_options", lambda *_args: options)
    monkeypatch.setattr(pipeline, "_fa_prices", core.fa_prices)
    monkeypatch.setattr(
        core, "resolved_robinhood_quantities", lambda _bundle: SimpleNamespace(blockers=())
    )


def _price_args(command: str, tmp_path: Path, path: str, *extra: str) -> list[str]:
    no_fetch = ("--no-fetch-prices",) if path == "cache" else ()
    cache = ("--price-cache-dir", str(tmp_path / "prices"))
    return [*_common(command, tmp_path), *cache, *no_fetch, *extra]


def _run_fa_refusal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, path: str, overview: bytes, bars: bytes
) -> None:
    """Run ``robinhood-fa`` on these responses: it exits 4, leaves no FA
    output, and a cached artifact stays byte-identical."""
    _stub_successful_all(monkeypatch, _complete_fa())
    _stub_real_option_prices(monkeypatch)
    stale = _stale_fa_outputs(tmp_path / "out")
    artifact = serve_massive(monkeypatch, tmp_path, path, overview, bars)
    before = None if artifact is None else artifact.read_bytes()

    argv = _price_args("robinhood-fa", tmp_path, path, "--format", "cbdt-json-fragment")
    assert cli.main(argv) == 4

    assert not any(item.exists() for item in stale)
    if artifact is None:
        assert not (tmp_path / "prices").exists()
    else:
        assert artifact.read_bytes() == before


@pytest.mark.parametrize("path", ["fetch", "cache"])
def test_robinhood_fa_refuses_a_fractional_massive_count_with_its_located_text(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    path: str,
) -> None:
    _run_fa_refusal(tmp_path, monkeypatch, path, mr_overview(), mr_bars(n="1.5"))

    captured = capsys.readouterr()
    assert captured.err == f"error: {FNO_FEMA_WARNING} {MASSIVE_COUNT_REFUSAL}\n"
    assert_no_markers(captured.out + captured.err, *MR_MARKERS)


_OTHER_FAILURES = [
    pytest.param(mr_overview(), mr_bars()[:-1], MASSIVE_JSON_REFUSAL, id="malformed-json"),
    pytest.param(mr_overview(), mr_bars(c='"1_2"'), MASSIVE_CLOSE_REFUSAL, id="close"),
    pytest.param(mr_overview(), mr_bars(v="Infinity"), MASSIVE_VOLUME_REFUSAL, id="volume"),
    pytest.param(mr_overview(), mr_bars(t="true"), MASSIVE_TIMESTAMP_REFUSAL, id="timestamp"),
    pytest.param(
        mr_overview(), mr_bars(t=str(10**20)), MASSIVE_TIMESTAMP_RANGE_REFUSAL, id="timestamp-range"
    ),
    pytest.param(mr_overview(), mr_bars(row_two="[]"), MASSIVE_ROW_REFUSAL, id="row"),
    pytest.param(mr_overview(status='"ERROR"'), mr_bars(), MASSIVE_STATUS_REFUSAL, id="status"),
    pytest.param(
        mr_overview(strike_price='"2_50"'), mr_bars(), MASSIVE_STRIKE_REFUSAL, id="strike"
    ),
    pytest.param(
        mr_overview(shares_per_contract="true"),
        mr_bars(),
        MASSIVE_MULTIPLIER_REFUSAL,
        id="multiplier",
    ),
    pytest.param(
        mr_overview(correction="false"), mr_bars(), MASSIVE_CORRECTION_REFUSAL, id="correction"
    ),
]


@pytest.mark.parametrize("path", ["fetch", "cache"])
@pytest.mark.parametrize(("overview", "bars", "message"), _OTHER_FAILURES)
def test_robinhood_fa_refuses_other_malformed_massive_fields_with_their_text(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    path: str,
    overview: bytes,
    bars: bytes,
    message: str,
) -> None:
    _run_fa_refusal(tmp_path, monkeypatch, path, overview, bars)

    captured = capsys.readouterr()
    assert captured.err == f"error: {ROBINHOOD_FNO_FEMA_WARNING} {message}\n"
    assert_no_markers(captured.out + captured.err, *MR_MARKERS)


def test_robinhood_fa_without_option_trades_never_calls_the_option_reader(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Without trades the price audit returns before it loads any close."""
    _stub_successful_all(monkeypatch, _complete_fa())
    _stub_real_option_prices(monkeypatch, has_trades=False)
    audits: list[object] = []
    reads: list[object] = []
    real_audit = option_prices.collect_robinhood_option_price_audit

    def audit(**kwargs: Any) -> object:
        audits.append(kwargs)
        return real_audit(**kwargs)

    monkeypatch.setattr(option_prices, "collect_robinhood_option_price_audit", audit)
    monkeypatch.setattr(
        option_prices, "load_or_fetch_massive_option_closes", lambda **kwargs: reads.append(kwargs)
    )
    monkeypatch.setattr(
        core,
        "price_series",
        lambda *_args, **_kwargs: ({}, SimpleNamespace(errors=(), warnings=())),
    )
    monkeypatch.setattr(pipeline, "_write_fa", lambda *_args, **_kwargs: ((), False, ()))

    cli.main(_price_args("robinhood-fa", tmp_path, "fetch"))

    assert len(audits) == 1
    assert reads == []


@pytest.mark.parametrize("path", ["fetch", "cache"])
def test_robinhood_all_fa_leg_reports_the_located_text_and_removes_every_output(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    path: str,
) -> None:
    """The same refusal in robinhood-all keeps its all-or-nothing contract."""
    out = tmp_path / "out"
    written = _stub_writing_all(monkeypatch, out)
    monkeypatch.setattr(pipeline, "_annual_form_warnings", lambda _result: ())
    monkeypatch.setattr(pipeline, "_income_warnings", lambda _result: ())
    monkeypatch.setattr(pipeline, "_compute_os", lambda *_args: SimpleNamespace())
    _stub_real_option_prices(monkeypatch)
    serve_massive(monkeypatch, tmp_path, path, mr_overview(), mr_bars(n="1.5"))

    assert cli.main(_price_args("robinhood-all", tmp_path, path)) == 4

    captured = capsys.readouterr()
    statuses = {name: status for status, name in _LEG_LINE.findall(captured.out)}
    assert statuses["schedule-fa"] == "error"
    assert f"-> {MASSIVE_COUNT_REFUSAL}\n" in captured.out
    assert written
    assert not any(item.exists() for item in written)
    assert_no_markers(captured.out + captured.err, *MR_MARKERS)


# ---- #1435: --help says what --debug, --itr-form and --output-dir do -------


def _help(command: str, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch) -> str:
    """``command``'s --help with its whitespace collapsed. COLUMNS is wide
    enough that argparse never wraps a line, so no file name breaks at a hyphen."""
    monkeypatch.setenv("COLUMNS", "10000")
    with pytest.raises(SystemExit):
        cli.main([command, "--help"])
    return " ".join(capsys.readouterr().out.split())


_SHARED_DEBUG_HELP = (
    "Also emit non-filing audit CSVs for Schedule OS, FSI, TR, and Form 67. "
    "Use the files without '-debug' for filing."
)
_SHARED_ITR_FORM_HELP = (
    "Target return form. Required because this command emits form-specific Schedule "
    "CG/TR labels. Use ITR-2 when you have no business/profession income; use ITR-3 "
    "when you do."
)


@pytest.mark.parametrize("format_id", ["review-csv", "xlsx"])
def test_robinhood_cg_debug_help_names_the_files_it_writes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    format_id: str,
) -> None:
    """robinhood-cg --debug writes the filing workbook and two CG sidecars in
    --format's extension, and no Schedule OS, FSI, TR or Form 67 file."""
    stub_cg_run(monkeypatch, cg_result)
    assert robinhood_main("robinhood-cg", tmp_path, "--format", format_id, "--debug") == 0
    extension = "xlsx" if format_id == "xlsx" else "csv"
    written = {
        path.name.replace(f"-debug.{extension}", "-debug.<ext>")
        for path in (tmp_path / "out").iterdir()
    }
    assert written == {
        "schedule-cg.xlsx",
        "schedule-cg-debug.<ext>",
        "schedule-cg-ltcg-debug.<ext>",
    }
    capsys.readouterr()

    help_text = _help("robinhood-cg", capsys, monkeypatch)

    for name in written:
        assert name in help_text
    assert "Schedule OS, FSI, TR, and Form 67" not in help_text


def test_robinhood_all_debug_help_names_every_debug_file(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    help_text = _help("robinhood-all", capsys, monkeypatch)

    for name in {f"{Path(name).stem}.<ext>" for name in CG_DEBUG_FILENAMES}:
        assert name in help_text
    for name in INCOME_REVIEW_FILENAMES:
        assert name in help_text
    assert "schedule-cg.xlsx" in help_text


def test_robinhood_fa_itr_form_help_names_its_uses(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    help_text = _help("robinhood-fa", capsys, monkeypatch)

    assert "Schedule CG/TR labels" not in help_text
    assert (
        "--itr-form {ITR-2,ITR-3} Target return form. Required because it selects the form of "
        "the CBDT JSON fragment (--format cbdt-json-fragment), because filing-csv checks each "
        "Schedule FA country against that form's dropdown, and because reviewed business "
        "treatment of Robinhood options needs ITR-3. Use ITR-2 when you have no "
        "business/profession income; use ITR-3 when you do."
    ) in help_text


@pytest.mark.parametrize(
    ("command", "shared_help"),
    [
        *(
            (command, _SHARED_DEBUG_HELP)
            for command in (
                "robinhood-income",
                "netbenefits-income",
                "netbenefits-all",
                "fidelity-cma-income",
                "stockplan-income",
                "stockplan-all",
            )
        ),
        *(
            (command, _SHARED_ITR_FORM_HELP)
            for command in (
                "robinhood-cg",
                "robinhood-income",
                "robinhood-options",
                "robinhood-all",
            )
        ),
    ],
)
def test_other_commands_keep_the_shared_help(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    command: str,
    shared_help: str,
) -> None:
    assert shared_help in _help(command, capsys, monkeypatch)


@pytest.mark.skipif(os.name != "posix", reason="POSIX mode bits only")
@pytest.mark.parametrize(
    "command",
    ["robinhood-cg", "robinhood-income", "robinhood-fa", "robinhood-options", "robinhood-all"],
)
def test_robinhood_output_dir_help_names_the_private_rule(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    command: str,
) -> None:
    """A folder made first under umask 022 refuses; --help says so up front."""
    out = tmp_path / "out"
    out.mkdir()
    out.chmod(0o755)

    assert cli.main(_common(command, tmp_path)) == 4

    assert capsys.readouterr().err == f"error: {PRIVATE_OUTPUT_DIR_REFUSAL}: {out}\n"
    assert (
        "--output-dir DIR Dedicated Robinhood output directory; do not share it with another "
        "source. If it doesn't exist, the command creates it, readable only by you; an "
        "existing one must be yours and private (chmod 700), or the run refuses."
    ) in _help(command, capsys, monkeypatch)
