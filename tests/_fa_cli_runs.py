"""Capture a Schedule FA CLI run, and the user.toml step the Robinhood
``[[fa.entities]]`` CLI tests share (#1715)."""

from __future__ import annotations

from collections.abc import Callable
from functools import partial
from pathlib import Path
from types import SimpleNamespace
from typing import NamedTuple

import pytest

from ibkr_fa.compute.robinhood_identity import merge_robinhood_entity_overrides
from ibkr_fa.config import FaEntityOverride, load_user_config
from tests._robinhood_cli import robinhood_main as _robinhood_main


class CliRun(NamedTuple):
    """A run's exit, its masked streams and its output folder."""

    rc: int
    out: str
    err: str
    out_dir: Path


def capture(run_dir: Path, capsys: pytest.CaptureFixture[str], run: Callable[[], int]) -> CliRun:
    """Call ``run``; return its exit, its streams with ``run_dir`` masked as
    ``<run>``, and its output folder."""
    capsys.readouterr()
    rc = run()
    captured = capsys.readouterr()
    return CliRun(
        rc,
        captured.out.replace(str(run_dir), "<run>"),
        captured.err.replace(str(run_dir), "<run>"),
        run_dir / "out",
    )


def robinhood_run(
    run_dir: Path, capsys: pytest.CaptureFixture[str], command: str, *extra: str
) -> CliRun:
    """``command`` in ``run_dir`` with ``--no-fetch-prices``, captured."""
    return capture(
        run_dir, capsys, partial(_robinhood_main, command, run_dir, "--no-fetch-prices", *extra)
    )


def merge_entity_block(
    bundle: SimpleNamespace,
    run_dir: Path,
    block: str,
    automatic: tuple[FaEntityOverride, ...],
) -> None:
    """Append ``block`` to ``run_dir``'s user.toml, load it as ``bundle``'s
    config and merge its ``[[fa.entities]]`` over the ``automatic`` evidence
    as ``bundle``'s entity overrides."""
    config_path = run_dir / "user.toml"
    with config_path.open("a", encoding="utf-8") as handle:
        handle.write(block)
    bundle.config = load_user_config(config_path)
    bundle.entity_overrides = merge_robinhood_entity_overrides(automatic, bundle.config.fa.entities)
