"""The IBKR standalone command table and the runners built on it, which the
standalone output cleanup tests and other CLI tests share (#1886).

- ``FY_STATEMENT`` and ``CY_STATEMENT``: the financial-year and calendar-year
  Activity Statement fixtures; ``STANDALONE_ACCOUNT``: the account number the
  ``user.toml`` template gets by default;
- ``FY_ARGS`` and ``ITR_ARGS``: the ``--financial-year`` and ``--itr-form``
  arguments;
- ``STANDALONE_COMMANDS``: each standalone schedule's pipeline module, entry
  point, statement, periods, CLI arguments, entry keywords, output stems and run
  report; ``ALL_STANDALONE_COMMANDS``: those and ``form-67``;
- ``write_standalone_inputs``: writes a command's statement and ``user.toml``;
  ``standalone_targets``: a command's output paths; ``run_standalone_cli`` and
  ``run_standalone_entry``: run a command through ``main`` or its entry point;
- ``fa_json_inputs`` and ``run_fa_json``: the inputs and the run of JSON
  ``schedule-fa`` with offline prices.
"""

from __future__ import annotations

import functools
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import pytest

from ibkr_fa.cli import main
from ibkr_fa.pipeline import FinancialYear
from ibkr_fa.pipeline_cg import run_schedule_cg
from ibkr_fa.pipeline_cg_ltcg import run_schedule_cg_ltcg
from ibkr_fa.pipeline_fa import run_fa_a3
from ibkr_fa.pipeline_form67 import run_form67
from ibkr_fa.pipeline_fsi import run_schedule_fsi
from ibkr_fa.pipeline_os import run_schedule_os
from ibkr_fa.pipeline_tr import run_schedule_tr
from tests._form67_overrides import FORM67_READY_OVERRIDE
from tests._ibkr_schedule_fa_inputs import SCHEDULE_FA_CONFIG, flat_close_fetcher
from tests._ibkr_user_toml import CUSTODIAN_TOML

_FIXTURES = Path(__file__).parent / "fixtures"
FY_STATEMENT = _FIXTURES / "e2e_activity_statement_schedule_tr_with_wht_fy2025_26.csv"
CY_STATEMENT = _FIXTURES / "e2e_activity_statement_with_nav_2025.csv"
STANDALONE_ACCOUNT = "U12345678"
_CONFIG = (
    """\
[account]
number = "{number}"
holder_name = "Sample Holder"
opening_date = 2020-01-01
status = "BeneficialOwner"
us_person = true
"""
    + CUSTODIAN_TOML
    + """
[fx]
max_carry_forward_days = 4
"""
)


@dataclass(frozen=True)
class _Command:
    module: str  # the pipeline module whose writer a test replaces
    run: Callable[..., object]
    statement: Path
    period: tuple[str, str]  # the statement's covering Period, and a short one
    cli_args: tuple[str, ...]
    run_kwargs: dict[str, object]
    targets: tuple[str, ...]  # file stems; the first is --output
    report: str  # the run report class, which ``module`` builds after the write


_FY_PERIOD = ('"April 1, 2025 - March 31, 2026"', '"May 1, 2025 - March 31, 2026"')
_CY_PERIOD = ('"January 1, 2025 - December 31, 2025"', '"February 1, 2025 - December 31, 2025"')
FY_ARGS = ("--financial-year", "2025-26")
ITR_ARGS = ("--itr-form", "ITR-2")
_FY_KWARGS: dict[str, object] = {"financial_year": FinancialYear(2025)}

STANDALONE_COMMANDS = {
    "schedule-os": _Command(
        "ibkr_fa.pipeline_os",
        run_schedule_os,
        FY_STATEMENT,
        _FY_PERIOD,
        FY_ARGS,
        _FY_KWARGS,
        ("schedule-os",),
        "ScheduleOsRunReport",
    ),
    "schedule-cg": _Command(
        "ibkr_fa.pipeline_cg",
        run_schedule_cg,
        FY_STATEMENT,
        _FY_PERIOD,
        (*FY_ARGS, *ITR_ARGS),
        _FY_KWARGS,
        ("schedule-cg",),
        "ScheduleCgRunReport",
    ),
    "schedule-cg-ltcg": _Command(
        "ibkr_fa.pipeline_cg_ltcg",
        run_schedule_cg_ltcg,
        FY_STATEMENT,
        _FY_PERIOD,
        (*FY_ARGS, *ITR_ARGS),
        _FY_KWARGS,
        ("schedule-cg-ltcg",),
        "ScheduleCgLtcgRunReport",
    ),
    "schedule-fsi": _Command(
        "ibkr_fa.pipeline_fsi",
        run_schedule_fsi,
        FY_STATEMENT,
        _FY_PERIOD,
        FY_ARGS,
        _FY_KWARGS,
        ("schedule-fsi",),
        "ScheduleFsiRunReport",
    ),
    "schedule-tr": _Command(
        "ibkr_fa.pipeline_tr",
        run_schedule_tr,
        FY_STATEMENT,
        _FY_PERIOD,
        (*FY_ARGS, *ITR_ARGS),
        _FY_KWARGS,
        ("schedule-tr",),
        "ScheduleTrRunReport",
    ),
    "schedule-fa": _Command(
        "ibkr_fa.pipeline_fa",
        run_fa_a3,
        CY_STATEMENT,
        _CY_PERIOD,
        ("--calendar-year", "2025", *ITR_ARGS, "--no-fetch-prices"),
        {"calendar_year": 2025},
        ("schedule-fa", "schedule-fa-a2"),
        "RunReport",
    ),
}
# form-67 joins only the tests of a target that cannot be removed (#736), of
# a refusal after the write, and of trade-time (#915), total (#908) or
# classification (#913, #1012) refusals.
ALL_STANDALONE_COMMANDS = {
    **STANDALONE_COMMANDS,
    "form-67": _Command(
        "ibkr_fa.pipeline_form67",
        run_form67,
        FY_STATEMENT,
        _FY_PERIOD,
        FY_ARGS,
        _FY_KWARGS,
        ("form-67",),
        "Form67RunReport",
    ),
}


def write_standalone_inputs(
    tmp_path: Path,
    name: str,
    *,
    number: str = STANDALONE_ACCOUNT,
    statement_text: str | None = None,
) -> tuple[Path, Path]:
    statement = tmp_path / "statement.csv"
    if statement_text is None:
        statement_text = ALL_STANDALONE_COMMANDS[name].statement.read_text(encoding="utf-8")
    statement.write_text(statement_text, encoding="utf-8")
    config = tmp_path / "user.toml"
    config_text = _CONFIG.format(number=number)
    if name == "form-67":
        # #601: an exact Indian tax payable makes the row READY, so form-67
        # writes its official CSV rather than withholding it.
        config_text += FORM67_READY_OVERRIDE
    config.write_text(config_text, encoding="utf-8")
    return statement, config


def standalone_targets(out: Path, name: str, suffix: str = ".csv") -> list[Path]:
    out.mkdir(exist_ok=True)
    return [out / f"{stem}{suffix}" for stem in ALL_STANDALONE_COMMANDS[name].targets]


def run_standalone_cli(name: str, statement: Path, config: Path, output: Path, *extra: str) -> int:
    return main(
        [
            name,
            "--input",
            str(statement),
            "--config",
            str(config),
            "--output",
            str(output),
            *ALL_STANDALONE_COMMANDS[name].cli_args,
            *extra,
        ]
    )


def run_standalone_entry(
    name: str, statement: Path, config: Path, output: Path, **kwargs: object
) -> object:
    command = ALL_STANDALONE_COMMANDS[name]
    return command.run(
        input_path=statement,
        config_path=config,
        output_path=output,
        **command.run_kwargs,
        **kwargs,
    )


_JSON_PRICES: dict[str, object] = {
    "price_fetcher": flat_close_fetcher(),
    "price_now": datetime(2026, 6, 1, tzinfo=UTC),
}


def fa_json_inputs(
    tmp_path: Path, config_text: str = SCHEDULE_FA_CONFIG
) -> tuple[Path, Path, Path]:
    statement, config = write_standalone_inputs(tmp_path, "schedule-fa")
    config.write_text(config_text, encoding="utf-8")
    return statement, config, standalone_targets(tmp_path / "out", "schedule-fa", ".json")[0]


def run_fa_json(
    layer: str, monkeypatch: pytest.MonkeyPatch, statement: Path, config: Path, fragment: Path
) -> object:
    """JSON ``schedule-fa`` with offline prices: a fragment needs every peak
    value, so the CLI run cannot pass ``--no-fetch-prices``."""
    if layer == "entry":
        return run_standalone_entry(
            "schedule-fa",
            statement,
            config,
            fragment,
            format_id="cbdt-json-fragment",
            itr_form="ITR-2",
            fetch_prices=True,
            **_JSON_PRICES,
        )
    monkeypatch.setattr("ibkr_fa.cli.run_fa_a3", functools.partial(run_fa_a3, **_JSON_PRICES))
    return main(
        [
            "schedule-fa",
            "--input",
            str(statement),
            "--config",
            str(config),
            "--output",
            str(fragment),
            "--calendar-year",
            "2025",
            *ITR_ARGS,
            "--format",
            "cbdt-json-fragment",
        ]
    )
