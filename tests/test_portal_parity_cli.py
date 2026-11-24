"""Pin the listed portal command cases and the Fidelity CMA CG companion:
exit code, stdout, stderr and every output file, byte for byte (#1199).

Each case runs ``main`` on synthetic inputs the source's own tests already
build, offline, under the fixed workbook clock, and compares the run with the
baseline captured at the commit ``tests/fixtures/portal_parity_baseline.json``
names; #1289 re-captured the ``schedule-fa/filing-csv`` and ``run-all`` records,
#1359 the ``schedule-tr/filing-csv`` record, #1363 the ``schedule-tr/filing-csv``,
``run-all`` and four ``form-67`` records, #1360 the ``robinhood-cg/debug``
and ``robinhood-all`` records, #1361 the ``run-all``, ``fidelity-cma-cg``,
``netbenefits-all`` and ``robinhood-cg/debug`` records, #1379 the
``robinhood-cg/debug`` record, #1383 the ``robinhood-cg/debug`` and
``robinhood-all`` records, and #1427 and again #1446 the
``schedule-fsi/filing-csv``, ``schedule-tr/filing-csv``, ``form-67/blank``,
``form-67/fractional-rates/blank``, ``netbenefits-income`` and
``netbenefits-all`` records, #1447 the ``form-67/fractional-rates/ready``
record, and #1453 and again #1471 the ``fidelity-cma-income`` and
``fidelity-cma-income/debug`` records. #1385 added the
``fidelity-cma-fa/filing-csv`` and ``fidelity-cma-fa/filing-csv/attested``
records, captured on its own branch because earlier commits don't offer the
format.
The tmp path is written as ``<tmp>`` in both streams. Robinhood has no
synthetic bundle, so its cases stub the loading and compute stages the way
``tests/test_pipeline_robinhood.py`` does and run the real writers.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace

import pytest

from ibkr_fa import pipeline_robinhood as robinhood
from ibkr_fa.cli import main
from tests._fidelity_cma_bundle import cma_inputs  # noqa: F401 - fixture
from tests._fidelity_cma_fa_run import UPPER_BOUND_CONFIG as _UPPER_BOUND
from tests._ibkr_run_all import run_all_from_blocks as _run_all
from tests._ibkr_standalone_commands import ALL_STANDALONE_COMMANDS as _ALL_COMMANDS
from tests._ibkr_standalone_commands import write_standalone_inputs as _write_inputs
from tests._portal_parity import (
    assert_run_matches,
    baseline_cases,
    expected,
    fix_xlsx_clock,
    run_record,
)
from tests._robinhood_cg import cg_result, sale
from tests._robinhood_cli import robinhood_main
from tests._robinhood_pipeline_stubs import stub_cg_run as _stub_cg_run
from tests._robinhood_pipeline_stubs import stub_relief_run as _stub_relief_run
from tests._source_commands import SOURCE_COMMANDS, SOURCE_FY_ARGS, netbenefits_command_inputs

_GROUP = "portal-commands"
# Runs ``main`` in a fresh tmp path and returns its exit code and output folder.
_Case = Callable[[Path, pytest.MonkeyPatch, pytest.FixtureRequest], tuple[int, Path]]


def _ibkr(
    command: str,
    *,
    inputs_of: str | None = None,
    extra: tuple[str, ...] = (),
    statement_text: Callable[[], str] | None = None,
    config_extra: str = "",
) -> _Case:
    """``command`` on the IBKR statement and ``user.toml`` that
    ``tests/_ibkr_standalone_commands.py`` gives ``inputs_of`` (by default
    ``command``), or on ``statement_text`` and that config followed by
    ``config_extra``, writing ``--output`` under ``out/``."""

    def run(
        tmp_path: Path, _mp: pytest.MonkeyPatch, _rq: pytest.FixtureRequest
    ) -> tuple[int, Path]:
        text = None if statement_text is None else statement_text()
        statement, config = _write_inputs(tmp_path, inputs_of or command, statement_text=text)
        config.write_text(config.read_text(encoding="utf-8") + config_extra, encoding="utf-8")
        out = tmp_path / "out"
        out.mkdir()
        suffix = ".xlsx" if "filing-xlsx" in extra else ".csv"
        argv = [
            command,
            *("--input", str(statement), "--config", str(config)),
            *("--output", str(out / f"{_ALL_COMMANDS[command].targets[0]}{suffix}")),
            *_ALL_COMMANDS[command].cli_args,
            *extra,
        ]
        return main(argv), out

    return run


def _source(name: str, *, debug: bool = True) -> _Case:
    """A StockPlan, NetBenefits or Fidelity CMA command as
    ``tests/_source_commands.py`` prepares it."""

    def run(tmp_path: Path, mp: pytest.MonkeyPatch, rq: pytest.FixtureRequest) -> tuple[int, Path]:
        prepared = SOURCE_COMMANDS[name].prepare(tmp_path, mp, rq)
        argv = prepared.argv if debug else [arg for arg in prepared.argv if arg != "--debug"]
        return main(argv), prepared.out

    return run


def _fidelity_cma_fa_attested(
    tmp_path: Path, mp: pytest.MonkeyPatch, rq: pytest.FixtureRequest
) -> tuple[int, Path]:
    """``fidelity-cma-fa --format filing-csv`` with an attested upper A2 peak bound."""
    prepared = SOURCE_COMMANDS["fidelity-cma-fa-filing"].prepare(tmp_path, mp, rq)
    _statements, _tax_forms, config = rq.getfixturevalue("cma_inputs")
    config.write_text(config.read_text(encoding="utf-8") + _UPPER_BOUND, encoding="utf-8")
    return main(prepared.argv), prepared.out


def _netbenefits_income_with(config_extra: str) -> _Case:
    """``netbenefits-income`` as ``tests/_source_commands.py`` prepares it,
    without ``--debug``, with ``config_extra`` after its ``user.toml``."""

    def run(tmp_path: Path, mp: pytest.MonkeyPatch, _rq: pytest.FixtureRequest) -> tuple[int, Path]:
        bundle, config, out = netbenefits_command_inputs(tmp_path, mp)
        config.write_text(config.read_text(encoding="utf-8") + config_extra, encoding="utf-8")
        argv = [
            "netbenefits-income",
            *("--input", str(bundle), "--config", str(config), "--output-dir", str(out)),
            *SOURCE_FY_ARGS,
        ]
        return main(argv), out

    return run


def _netbenefits_all(
    tmp_path: Path, mp: pytest.MonkeyPatch, _rq: pytest.FixtureRequest
) -> tuple[int, Path]:
    bundle, config, out = netbenefits_command_inputs(tmp_path, mp)
    argv = [
        "netbenefits-all",
        *("--input", str(bundle), "--config", str(config), "--output-dir", str(out)),
        *("--financial-year", "2025-26", "--itr-form", "ITR-2", "--no-fetch-prices"),
    ]
    return main(argv), out


def _run_all_case(
    tmp_path: Path, _mp: pytest.MonkeyPatch, _rq: pytest.FixtureRequest
) -> tuple[int, Path]:
    return _run_all(tmp_path), tmp_path / "out"


def _robinhood_cg(
    tmp_path: Path, mp: pytest.MonkeyPatch, _rq: pytest.FixtureRequest
) -> tuple[int, Path]:
    _stub_cg_run(
        mp,
        lambda: cg_result(
            sale("12", "10"),
            sale("12", "10", long_term=True, lot_ordinal=3, sale_ordinal=4),
        ),
    )
    return robinhood_main("robinhood-cg", tmp_path, "--debug"), tmp_path / "out"


def _robinhood_all(
    tmp_path: Path, mp: pytest.MonkeyPatch, _rq: pytest.FixtureRequest
) -> tuple[int, Path]:
    _stub_cg_run(mp, lambda: cg_result(sale("12", "10")))
    # As tests/test_pipeline_robinhood.py's autouse fixture: no OS leg output.
    mp.setattr(robinhood, "_annual_form_warnings", lambda _result: ())
    mp.setattr(robinhood, "_income_warnings", lambda _result: ())
    mp.setattr(robinhood, "_compute_os", lambda *_args: SimpleNamespace())
    mp.setattr(robinhood, "_write_os", lambda *_args, **_kwargs: ())
    return robinhood_main("robinhood-all", tmp_path, "--no-fetch-prices"), tmp_path / "out"


def _robinhood_income(
    tmp_path: Path, mp: pytest.MonkeyPatch, _rq: pytest.FixtureRequest
) -> tuple[int, Path]:
    _stub_relief_run(tmp_path, mp, "US")
    return robinhood_main("robinhood-income", tmp_path), tmp_path / "out"


def _form67_override(country: str, amount: str) -> str:
    return (
        f'\n[[form67.tax_payable_overrides]]\ncountry = "{country}"\nsource = "DIVIDEND"\n'
        f'tax_payable_normal_inr = "{amount}"\n'
    )


# An exact Indian tax payable of 0.4 makes the US row's credit file as 0: REFUSED.
_REFUSED_OVERRIDE = "\n[form67]" + _form67_override("US", "0.4")


def _two_fractional_rates() -> str:
    """The Form 67 statement with a second, Canadian, dividend: each of the two
    rows has 0.40 tax on 2.60, a fractional 15.38%."""
    instrument = (
        "Financial Instrument Information,Data,Stocks,XQBA,SAMPLE ISSUER BA INC,11111,"
        "US99995C1027,US99995C1027,NASDAQ,1,COMMON,,,,,,\n"
    )
    dividend = (
        "Dividends,Data,USD,12-09-2025,XQBA(US99995C1027) Cash Dividend USD 0.26 per Share "
        "(Ordinary Dividend),2.60,,,,,,,,,,,\n"
    )
    tax = (
        "Withholding Tax,Data,USD,12-09-2025,XQBA(US99995C1027) Cash Dividend USD 0.26 per "
        "Share - US Tax,-0.39,,,,,,,,,,,\n"
    )
    text = _ALL_COMMANDS["form-67"].statement.read_text(encoding="utf-8")
    for old, new in (
        (instrument, instrument + instrument.replace("XQBA", "XQBB").replace("US9", "CA9")),
        (dividend, dividend + dividend.replace("XQBA", "XQBB").replace("US9", "CA9")),
        ("Dividends,Data,Total,,,2.60,", "Dividends,Data,Total,,,5.20,"),
        (
            tax,
            (
                tax
                + tax.replace("XQBA", "XQBB").replace("US9", "CA9").replace("- US Tax", "- CA Tax")
            ).replace("-0.39", "-0.40"),
        ),
        ("Withholding Tax,Data,Total,,,-0.39,", "Withholding Tax,Data,Total,,,-0.80,"),
    ):
        assert text.count(old) == 1
        text = text.replace(old, new)
    return text.replace("CA99995C1027", "CA99995C1023")  # its ISIN check digit


# A configured withholding agent and an exact Indian tax make the US dividend's
# Form 67 entry ready with a section 90 credit, so Schedule TR appends Quicko's
# foreign tax credit block (#1204).
_ACCOUNT = "[netbenefits.account]\n"
_AGENT = 'withholding_agent_name = "Example Withholding Agent LLC"\n'


def _netbenefits_income_quicko(
    tmp_path: Path, mp: pytest.MonkeyPatch, _rq: pytest.FixtureRequest
) -> tuple[int, Path]:
    bundle, config, out = netbenefits_command_inputs(tmp_path, mp)
    text = config.read_text(encoding="utf-8")
    assert text.count(_ACCOUNT) == 1
    ready = "\n[form67]" + _form67_override("US", "1000")
    config.write_text(text.replace(_ACCOUNT, _ACCOUNT + _AGENT) + ready, encoding="utf-8")
    argv = [
        "netbenefits-income",
        *("--input", str(bundle), "--config", str(config), "--output-dir", str(out)),
        *SOURCE_FY_ARGS,
    ]
    return main(argv), out


_CASES: dict[str, _Case] = {
    "schedule-fa/filing-csv": _ibkr("schedule-fa", extra=("--format", "filing-csv")),
    "schedule-os/filing-csv": _ibkr("schedule-os", extra=("--format", "filing-csv")),
    "schedule-os/filing-xlsx": _ibkr("schedule-os", extra=("--format", "filing-xlsx")),
    "schedule-fsi/filing-csv": _ibkr("schedule-fsi", extra=("--format", "filing-csv")),
    "schedule-tr/filing-csv": _ibkr("schedule-tr", extra=("--format", "filing-csv")),
    "form-67/portal-csv": _ibkr("form-67"),
    # Without schedule-os's [form67] override, the Form 67 row is BLANK.
    "form-67/blank": _ibkr("form-67", inputs_of="schedule-os"),
    # #1203: REFUSED, and one fractional-rate warning per row, then the
    # missing-Indian-tax warning once, before (BLANK) and after (READY) overrides.
    "form-67/refused": _ibkr("form-67", inputs_of="schedule-os", config_extra=_REFUSED_OVERRIDE),
    "form-67/fractional-rates/blank": _ibkr(
        "form-67", inputs_of="schedule-os", statement_text=_two_fractional_rates
    ),
    "form-67/fractional-rates/ready": _ibkr(
        "form-67", statement_text=_two_fractional_rates, config_extra=_form67_override("CA", "500")
    ),
    "run-all": _run_all_case,
    "netbenefits-fa": _source("netbenefits-fa"),
    "netbenefits-income": _source("netbenefits-income", debug=False),
    "netbenefits-income/refused": _netbenefits_income_with(_REFUSED_OVERRIDE),
    "netbenefits-income/quicko-block": _netbenefits_income_quicko,
    "netbenefits-all": _netbenefits_all,
    "stockplan-income": _source("stockplan-income", debug=False),
    "stockplan-income/debug": _source("stockplan-income"),
    "fidelity-cma-income": _source("fidelity-cma-income", debug=False),
    "fidelity-cma-income/debug": _source("fidelity-cma-income"),
    "fidelity-cma-cg": _source("fidelity-cma-cg"),
    "fidelity-cma-fa/filing-csv": _source("fidelity-cma-fa-filing"),
    "fidelity-cma-fa/filing-csv/attested": _fidelity_cma_fa_attested,
    "robinhood-cg/debug": _robinhood_cg,
    "robinhood-income": _robinhood_income,
    "robinhood-all": _robinhood_all,
}


@pytest.mark.parametrize("case", _CASES)
def test_command_output_matches_the_baseline(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    request: pytest.FixtureRequest,
    capsys: pytest.CaptureFixture[str],
    case: str,
) -> None:
    fix_xlsx_clock(monkeypatch)
    exit_code, out = _CASES[case](tmp_path, monkeypatch, request)

    captured = capsys.readouterr()
    run = run_record(exit_code, captured.out, captured.err, out, tmp_path)
    assert_run_matches(run, expected(_GROUP, case))


def test_every_command_case_has_a_baseline_record() -> None:
    assert set(_CASES) == baseline_cases(_GROUP)
