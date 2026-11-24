"""The StockPlan, NetBenefits and Fidelity CMA command table, which the source
output rollback tests and other CLI tests share (#1913).

- ``SOURCE_FY_ARGS``: the ``--financial-year`` and ``--itr-form`` arguments the
  income and CG commands take; ``_FY``: that financial year as their entry
  points take it;
- ``_INCOME_FORM67_BLANK``: the outputs an income run whose Form 67 tax payable
  is blank writes, in order (an income run's and a CG run's outputs are
  ``tests._source_all``'s ``INCOME_OUTPUTS_WITH_DEBUG`` and ``CG_OUTPUTS``);
- ``SourceRun``: a command's argv, its entry point and its output folder;
  ``_Command``: a command's preparer, pipeline module, last-built report class
  and written outputs; ``_out``: makes a run's output folder;
- ``netbenefits_command_inputs``: writes the NetBenefits test bundle and its
  ``user.toml`` (``config_text`` replaces the config if given), stubs
  ``pdftotext`` and makes the output folder;
- the per-command preparers build each command's run on its test bundle; the
  ``stockplan-fa`` and ``fidelity-cma-fa`` ones also patch the entry point
  ``ibkr_fa.cli`` calls with an offline price fetcher and a fixed clock;
- ``SOURCE_COMMANDS``: every command by name, with ``_NETBENEFITS_FA`` the
  module its three NetBenefits FA entries share; ``prepare_source_command``:
  prepares a command and returns its run and the paths it writes, in order.

The Fidelity CMA preparers read the ``cma_inputs`` fixture through the
requesting test's ``request``, so a test module that prepares one imports that
fixture itself.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from functools import partial
from pathlib import Path

import pytest

from ibkr_fa import cli
from ibkr_fa.output.cbdt_json_fragment import CBDT_JSON_FRAGMENT_FORMAT_ID
from ibkr_fa.output.netbenefits_quicko_fa_xlsx import QUICKO_TEMPLATE_FILENAME
from ibkr_fa.pipeline import FinancialYear
from ibkr_fa.pipeline_fidelity_cma_cg import run_fidelity_cma_cg
from ibkr_fa.pipeline_fidelity_cma_fa import run_fidelity_cma_fa
from ibkr_fa.pipeline_fidelity_cma_income import run_fidelity_cma_income
from ibkr_fa.pipeline_netbenefits_cg import run_netbenefits_cg
from ibkr_fa.pipeline_netbenefits_fa import run_netbenefits_fa
from ibkr_fa.pipeline_netbenefits_income import run_netbenefits_income
from ibkr_fa.pipeline_stockplan_fa import run_stockplan_fa
from ibkr_fa.pipeline_stockplan_income import run_stockplan_income
from tests._fidelity_cma_bundle import spaxx_nav
from tests._form67_overrides import FORM67_READY_OVERRIDE
from tests._netbenefits_e2e import NB_E2E_TOML_WITH_PEAK, nb_e2e_bundle
from tests._netbenefits_fa import write_quicko_template
from tests._source_all import CG_OUTPUTS, INCOME_OUTPUTS_WITH_DEBUG
from tests._stockplan_fa_exports import setup_stockplan_exports, stub_fetcher
from tests._stockplan_income import STOCKPLAN_INCOME_CONFIG, setup_income_run

_FY = FinancialYear(2025)
SOURCE_FY_ARGS = ("--financial-year", "2025-26", "--itr-form", "ITR-2")
# The official Form 67 CSV is withheld when its Indian tax payable is blank (#601).
_INCOME_FORM67_BLANK = tuple(name for name in INCOME_OUTPUTS_WITH_DEBUG if name != "form-67.csv")


@dataclass(frozen=True)
class SourceRun:
    """A command, ready to run on its test bundle through either layer."""

    argv: list[str]
    entry: Callable[[], object]
    out: Path  # holds the run's outputs and nothing else


@dataclass(frozen=True)
class _Command:
    prepare: Callable[[Path, pytest.MonkeyPatch, pytest.FixtureRequest], SourceRun]
    module: str  # the pipeline module that builds the report
    report: str  # the class it builds last, after every write
    written: tuple[str, ...]  # the outputs a successful run writes, in order


def _out(tmp_path: Path) -> Path:
    out = tmp_path / "out"
    out.mkdir()
    return out


def _stockplan_income(
    tmp_path: Path, _monkeypatch: pytest.MonkeyPatch, _request: pytest.FixtureRequest
) -> SourceRun:
    config_text = STOCKPLAN_INCOME_CONFIG + FORM67_READY_OVERRIDE
    input_dir, config, out = setup_income_run(tmp_path, config=config_text)
    return SourceRun(
        [
            "stockplan-income",
            *("--input", str(input_dir), "--config", str(config), "--output-dir", str(out)),
            *SOURCE_FY_ARGS,
            "--debug",
        ],
        partial(
            run_stockplan_income,
            input_dir=input_dir,
            config_path=config,
            output_dir=out,
            financial_year=_FY,
            debug=True,
        ),
        out,
    )


def _stockplan_fa(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, _request: pytest.FixtureRequest
) -> SourceRun:
    input_dir, config, _ = setup_stockplan_exports(tmp_path)
    out = _out(tmp_path)
    run = partial(
        run_stockplan_fa,
        price_fetcher=stub_fetcher,
        price_now=datetime(2026, 7, 6, tzinfo=UTC),
    )
    monkeypatch.setattr(cli, "run_stockplan_fa", run)
    output = out / "schedule-fa.csv"
    return SourceRun(
        [
            "stockplan-fa",
            *("--input", str(input_dir), "--config", str(config), "--output", str(output)),
            *("--calendar-year", "2025"),
        ],
        partial(
            run,
            input_dir=input_dir,
            config_path=config,
            output_path=output,
            calendar_year=2025,
            fetch_prices=True,
        ),
        out,
    )


def netbenefits_command_inputs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, config_text: str | None = None
) -> tuple[Path, Path, Path]:
    bundle, config = nb_e2e_bundle(tmp_path, monkeypatch, withholding_usd="0.36")
    if config_text is not None:
        config.write_text(config_text, encoding="utf-8")
    return bundle, config, _out(tmp_path)


def _netbenefits_income(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, _request: pytest.FixtureRequest
) -> SourceRun:
    bundle, config, out = netbenefits_command_inputs(tmp_path, monkeypatch)
    return SourceRun(
        [
            "netbenefits-income",
            *("--input", str(bundle), "--config", str(config), "--output-dir", str(out)),
            *SOURCE_FY_ARGS,
            "--debug",
        ],
        partial(
            run_netbenefits_income,
            input_dir=bundle,
            config_path=config,
            output_dir=out,
            financial_year=_FY,
            debug=True,
        ),
        out,
    )


def _netbenefits_cg(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, _request: pytest.FixtureRequest
) -> SourceRun:
    bundle, config, out = netbenefits_command_inputs(tmp_path, monkeypatch)
    return SourceRun(
        [
            "netbenefits-cg",
            *("--input", str(bundle), "--config", str(config), "--output-dir", str(out)),
            *SOURCE_FY_ARGS,
        ],
        partial(
            run_netbenefits_cg,
            input_dir=bundle,
            config_path=config,
            output_dir=out,
            financial_year=_FY,
        ),
        out,
    )


def _netbenefits_fa(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    output_name: str = "schedule-fa-a3.csv",
    config_text: str | None = None,
    cli_args: tuple[str, ...] = (),
    **entry_kwargs: object,
) -> SourceRun:
    bundle, config, out = netbenefits_command_inputs(tmp_path, monkeypatch, config_text)
    if entry_kwargs.get("quicko_workbook"):
        write_quicko_template(bundle / QUICKO_TEMPLATE_FILENAME)
    output = out / output_name
    return SourceRun(
        [
            "netbenefits-fa",
            *("--input", str(bundle), "--config", str(config), "--output", str(output)),
            *("--calendar-year", "2025", "--no-fetch-prices", *cli_args),
        ],
        partial(
            run_netbenefits_fa,
            input_dir=bundle,
            config_path=config,
            output_path=output,
            calendar_year=2025,
            fetch_prices=False,
            **entry_kwargs,
        ),
        out,
    )


def _netbenefits_fa_csv(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, _request: pytest.FixtureRequest
) -> SourceRun:
    return _netbenefits_fa(tmp_path, monkeypatch)


def _netbenefits_fa_quicko(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, _request: pytest.FixtureRequest
) -> SourceRun:
    return _netbenefits_fa(
        tmp_path, monkeypatch, cli_args=("--quicko-workbook",), quicko_workbook=True
    )


def _netbenefits_fa_json(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, _request: pytest.FixtureRequest
) -> SourceRun:
    return _netbenefits_fa(
        tmp_path,
        monkeypatch,
        output_name="schedule-fa.json",
        config_text=NB_E2E_TOML_WITH_PEAK,
        cli_args=("--format", CBDT_JSON_FRAGMENT_FORMAT_ID, "--itr-form", "ITR-2"),
        format_id=CBDT_JSON_FRAGMENT_FORMAT_ID,
        itr_form="ITR-2",
    )


def _fidelity_cma_income(
    tmp_path: Path, _monkeypatch: pytest.MonkeyPatch, request: pytest.FixtureRequest
) -> SourceRun:
    statements, tax_forms, config = request.getfixturevalue("cma_inputs")
    out = _out(tmp_path)
    return SourceRun(
        [
            "fidelity-cma-income",
            *("--input", str(statements), "--tax-forms", str(tax_forms)),
            *("--config", str(config), "--output-dir", str(out)),
            *SOURCE_FY_ARGS,
            "--debug",
        ],
        partial(
            run_fidelity_cma_income,
            input_dir=statements,
            tax_form_dir=tax_forms,
            config_path=config,
            output_dir=out,
            financial_year=_FY,
            debug=True,
        ),
        out,
    )


def _fidelity_cma_cg(
    tmp_path: Path, _monkeypatch: pytest.MonkeyPatch, request: pytest.FixtureRequest
) -> SourceRun:
    statements, _tax_forms, config = request.getfixturevalue("cma_inputs")
    out = _out(tmp_path)
    return SourceRun(
        [
            "fidelity-cma-cg",
            *("--input", str(statements), "--config", str(config), "--output-dir", str(out)),
            *SOURCE_FY_ARGS,
        ],
        partial(
            run_fidelity_cma_cg,
            input_dir=statements,
            config_path=config,
            output_dir=out,
            financial_year=_FY,
        ),
        out,
    )


def _fidelity_cma_fa(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    request: pytest.FixtureRequest,
    *,
    format_id: str = "review-csv",
) -> SourceRun:
    statements, _tax_forms, config = request.getfixturevalue("cma_inputs")
    out = _out(tmp_path)
    run = partial(
        run_fidelity_cma_fa,
        price_fetcher=spaxx_nav,
        price_now=datetime(2026, 6, 1, tzinfo=UTC),
    )
    monkeypatch.setattr(cli, "run_fidelity_cma_fa", run)
    output = out / "schedule-fa.csv"
    price_cache = tmp_path / "price-cache"
    return SourceRun(
        [
            "fidelity-cma-fa",
            *("--input", str(statements), "--config", str(config), "--output", str(output)),
            *("--calendar-year", "2025", "--format", format_id),
            *("--price-cache-dir", str(price_cache)),
        ],
        partial(
            run,
            input_dir=statements,
            config_path=config,
            output_path=output,
            calendar_year=2025,
            format_id=format_id,
            price_cache_dir=price_cache,
        ),
        out,
    )


_NETBENEFITS_FA = "ibkr_fa.pipeline_netbenefits_fa"
SOURCE_COMMANDS = {
    "stockplan-income": _Command(
        _stockplan_income,
        "ibkr_fa.pipeline_stockplan_income",
        "StockplanIncomeRunReport",
        INCOME_OUTPUTS_WITH_DEBUG,
    ),
    "stockplan-fa": _Command(
        _stockplan_fa, "ibkr_fa.pipeline_stockplan_fa", "StockplanFaRunReport", ("schedule-fa.csv",)
    ),
    "netbenefits-income": _Command(
        _netbenefits_income,
        "ibkr_fa.pipeline_netbenefits_income",
        "NetBenefitsIncomeRunReport",
        _INCOME_FORM67_BLANK,
    ),
    "netbenefits-cg": _Command(
        _netbenefits_cg, "ibkr_fa.pipeline_netbenefits_cg", "NetBenefitsCgRunReport", CG_OUTPUTS
    ),
    "netbenefits-fa": _Command(
        _netbenefits_fa_csv,
        _NETBENEFITS_FA,
        "NetBenefitsFaRunReport",
        ("schedule-fa-a2.csv", "schedule-fa-a3.csv"),
    ),
    "netbenefits-fa-quicko": _Command(
        _netbenefits_fa_quicko,
        _NETBENEFITS_FA,
        "NetBenefitsFaRunReport",
        ("schedule-fa-a2.csv", "schedule-fa-a3.csv", "schedule-fa-quicko.xlsx"),
    ),
    "netbenefits-fa-json": _Command(
        _netbenefits_fa_json, _NETBENEFITS_FA, "NetBenefitsFaRunReport", ("schedule-fa.json",)
    ),
    "fidelity-cma-income": _Command(
        _fidelity_cma_income,
        "ibkr_fa.pipeline_fidelity_cma_income",
        "FidelityCmaIncomeRunReport",
        _INCOME_FORM67_BLANK,
    ),
    "fidelity-cma-cg": _Command(
        _fidelity_cma_cg, "ibkr_fa.pipeline_fidelity_cma_cg", "FidelityCmaCgRunReport", CG_OUTPUTS
    ),
    "fidelity-cma-fa": _Command(
        _fidelity_cma_fa,
        "ibkr_fa.pipeline_fidelity_cma_fa",
        "FidelityCmaFaRunReport",
        ("schedule-fa-a2.csv", "schedule-fa.csv"),
    ),
    "fidelity-cma-fa-filing": _Command(
        partial(_fidelity_cma_fa, format_id="filing-csv"),
        "ibkr_fa.pipeline_fidelity_cma_fa",
        "FidelityCmaFaRunReport",
        ("schedule-fa-a2.csv", "schedule-fa.csv"),
    ),
}


def prepare_source_command(
    name: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, request: pytest.FixtureRequest
) -> tuple[SourceRun, list[Path]]:
    run = SOURCE_COMMANDS[name].prepare(tmp_path, monkeypatch, request)
    return run, [run.out / filename for filename in SOURCE_COMMANDS[name].written]
