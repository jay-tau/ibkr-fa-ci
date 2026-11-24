"""The broker income commands' smallest synthetic runs, shared by the Form 67 suites.

``tests/test_cli_section_91_broker_income.py`` (#1177) and
``tests/test_form67_override_advice_commands.py`` (#1469) launch
``robinhood-income``, ``robinhood-all``, ``netbenefits-income`` and
``fidelity-cma-income`` on these setups. Each launcher takes ``config``, which
turns the command's base user.toml text into the run's, writes to
``tmp_path / "out"`` and returns the CLI exit code. :func:`inject_income`
gives a run the suite's own payments and FX rate; those two suites and
``tests/_section_91_scenarios.py``'s ``_arrange`` read it.
"""

from __future__ import annotations

import functools
from collections.abc import Callable, Sequence
from decimal import Decimal
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

import ibkr_fa.pipeline_robinhood as robinhood
from ibkr_fa import cli
from ibkr_fa.config import load_user_config
from ibkr_fa.models.dividend import Dividend
from ibkr_fa.models.withholding_tax import WithholdingTax
from tests._fidelity_cma_income_run import CMA_FINALIZED_2025_TAX_CONFIG as _FINALIZED_TAX_CONFIG
from tests._fidelity_cma_income_run import CMA_INCOME_CONFIG as _CMA_CONFIG
from tests._fidelity_cma_income_run import patch_cma_income_sources as _patch_cma_sources
from tests._fidelity_cma_income_run import run_cma_income_cli as _run_cma
from tests._fx_stubs import AuditedFxStub
from tests._netbenefits_income import COVERING as _COVERING
from tests._netbenefits_income import INCOME_CONFIG as _NETBENEFITS_CONFIG
from tests._netbenefits_income import patch_statements as _patch_netbenefits_statements
from tests._robinhood_cli import robinhood_argv as _robinhood_args
from tests._robinhood_pipeline_stubs import stub_relief_run as _stub_relief_run


def inject_income(
    monkeypatch: pytest.MonkeyPatch,
    engine: ModuleType,
    *,
    dividends: Sequence[Dividend],
    withholding_taxes: Sequence[WithholdingTax],
    rate: Decimal,
) -> None:
    """Replace the dividends and withholding where ``engine`` calls the income
    engine, and price every dividend, withholding and capital gain at ``rate``."""
    real = engine.compute_income_schedules

    def injected(*args: Any, **kwargs: Any) -> Any:
        kwargs["dividends"] = list(dividends)
        kwargs["withholding_taxes"] = list(withholding_taxes)
        return real(*args, **kwargs)

    monkeypatch.setattr(engine, "compute_income_schedules", injected)
    for policy in ("Rule115Policy", "Rule128Policy", "Rule115CapitalGainsPolicy"):
        monkeypatch.setattr(f"ibkr_fa.fx.context.{policy}", functools.partial(AuditedFxStub, rate))


def output_dir(tmp_path: Path) -> Path:
    """The run's empty output directory, private as Robinhood requires."""
    out = tmp_path / "out"
    out.mkdir()
    out.chmod(0o700)
    return out


def run_robinhood(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    config: Callable[[str], str],
    *,
    command: str,
    country: str,
) -> int:
    # #1084's relief run, its foreign TIN filed under ``country``: real income
    # compute, everything else stubbed. Its stubbed loader's config is replaced
    # by the one loaded from user.toml.
    _stub_relief_run(tmp_path, monkeypatch, country)
    path = tmp_path / "user.toml"
    path.write_text(config(path.read_text(encoding="utf-8")), encoding="utf-8")
    stubbed_load: Any = robinhood._load
    stubbed_load().config = load_user_config(path)
    return cli.main([*_robinhood_args(command, tmp_path), "--no-fetch-prices"])


def run_netbenefits_income(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, config: Callable[[str], str]
) -> int:
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    for name in _COVERING:
        (bundle / name).write_bytes(b"%PDF-1.4 placeholder")
    _patch_netbenefits_statements(monkeypatch, _COVERING)
    path = tmp_path / "user.toml"
    path.write_text(config(_NETBENEFITS_CONFIG), encoding="utf-8")
    return cli.main(
        [
            "netbenefits-income",
            "--itr-form",
            "ITR-2",
            "--input",
            str(bundle),
            "--config",
            str(path),
            "--output-dir",
            str(tmp_path / "out"),
            "--financial-year",
            "2025-26",
        ]
    )


def run_fidelity_cma_income(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, config: Callable[[str], str]
) -> int:
    # A finalized US tax year, so the FTC isn't blocked.
    _patch_cma_sources(monkeypatch)
    return _run_cma(tmp_path, config(_CMA_CONFIG + _FINALIZED_TAX_CONFIG))
