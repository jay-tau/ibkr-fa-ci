"""The Robinhood commands' CLI arguments and runs through ``cli.main``, the
financial year they name, the files each command owns and a previous run's
stale FA outputs, shared by the Robinhood CLI and topic tests."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pytest

import ibkr_fa.pipeline_robinhood_outputs as outputs
from ibkr_fa import cli
from ibkr_fa.pipeline import FinancialYear


def robinhood_argv(
    command: str,
    run_dir: Path,
    *,
    financial_year: str = "2025-26",
    itr_form: str = "ITR-2",
    no_fetch_prices: bool = False,
) -> list[str]:
    """``command``'s arguments for the bundle and ``user.toml`` in ``run_dir``,
    writing to its ``out`` folder, for ``financial_year`` and ``itr_form``;
    ``--no-fetch-prices`` comes last when ``no_fetch_prices``."""
    return [
        command,
        "--input",
        str(run_dir / "bundle"),
        "--config",
        str(run_dir / "user.toml"),
        "--output-dir",
        str(run_dir / "out"),
        "--financial-year",
        financial_year,
        "--itr-form",
        itr_form,
        *(("--no-fetch-prices",) if no_fetch_prices else ()),
    ]


# The financial year robinhood_argv names by default.
FINANCIAL_YEAR = FinancialYear(2025)


def robinhood_main(command: str, tmp_path: Path, *extra: str) -> int:
    """Run ``command`` on ``tmp_path``'s bundle and ``user.toml``, then ``extra``."""
    return cli.main([*robinhood_argv(command, tmp_path), *extra])


def robinhood_main_all(tmp_path: Path) -> int:
    """Run ``robinhood-all`` with ``--no-fetch-prices --debug``."""
    return robinhood_main("robinhood-all", tmp_path, "--no-fetch-prices", "--debug")


def run_robinhood_command(
    command: str,
    run_dir: Path,
    capsys: pytest.CaptureFixture[str],
    *,
    year: int = 2025,
    itr_form: str = "ITR-2",
) -> tuple[int, str, str]:
    """Run ``command`` on ``run_dir``'s bundle and ``user.toml`` for FY
    ``year`` and ``itr_form``, and return its exit code and the stdout and
    stderr it printed; only ``robinhood-options`` takes no
    ``--no-fetch-prices``."""
    capsys.readouterr()
    code = cli.main(
        robinhood_argv(
            command,
            run_dir,
            financial_year=f"{year}-{(year + 1) % 100:02d}",
            itr_form=itr_form,
            no_fetch_prices=command != "robinhood-options",
        )
    )
    captured = capsys.readouterr()
    return code, captured.out, captured.err


# One leg's name and status in a ``robinhood-all`` run report.
LEG_LINE = re.compile(r"^  (\S+) +(\S+)$", re.MULTILINE)


def outputs_left(tmp_path: Path) -> list[str]:
    """The names of the files in ``tmp_path``'s ``out`` folder, sorted."""
    return sorted(path.name for path in (tmp_path / "out").iterdir())


def pipeline_run_kwargs(tmp_path: Path, itr_form: str = "ITR-2") -> dict[str, Any]:
    """A ``run_robinhood_*`` call's keywords for ``tmp_path``'s bundle and
    ``user.toml``, writing to ``tmp_path`` itself."""
    return {
        "input_dir": tmp_path / "bundle",
        "config_path": tmp_path / "user.toml",
        "output_dir": tmp_path,
        "financial_year": FINANCIAL_YEAR,
        "itr_form": itr_form,
    }


def pipeline_run_kwargs_to_out(tmp_path: Path) -> dict[str, Any]:
    """:func:`pipeline_run_kwargs`, with outputs in ``out`` beside ``user.toml``."""
    return {**pipeline_run_kwargs(tmp_path), "output_dir": tmp_path / "out"}


# The output files each Robinhood command owns.
OWNED_FILENAMES = {
    "robinhood-cg": outputs.CG_FILENAMES,
    "robinhood-options": outputs.OPTIONS_FILENAMES,
    "robinhood-income": (*outputs.OS_FILENAMES, *outputs.INCOME_FILENAMES),
    "robinhood-fa": outputs.FA_FILENAMES,
    "robinhood-all": outputs.ALL_FILENAMES,
}


def stale_fa_outputs(out: Path) -> list[Path]:
    """A previous run's FA outputs, its own JSON fragment among them."""
    out.mkdir(mode=0o700)
    stale = [out / name for name in outputs.FA_FILENAMES]
    for path in stale:
        path.write_text("previous run", encoding="utf-8")
    fragment = out / outputs.JSON_FILENAME
    fragment.write_text('{"_ibkrFa":{"format":"cbdt-json-fragment"}}', encoding="utf-8")
    return [*stale, fragment]
