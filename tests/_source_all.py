"""The ``netbenefits-all`` and ``stockplan-all`` runs through ``cli.main`` that the
CLI tests share: each source's stubbed legs, the files they write, the real
handlers and the summary footer; and the income and CG output names the source
command tests share.

The real handlers are taken from ``ibkr_fa.cli`` when this module is imported,
before any test stubs them."""

from __future__ import annotations

import argparse
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

import ibkr_fa.cli
from ibkr_fa.cli import _EXIT_MANUAL_FILL_REQUIRED, _EXIT_OK, _SOURCE_NOT_REQUIRED
from ibkr_fa.compute.form67 import Form67Readiness
from ibkr_fa.parser.netbenefits.bundle import NetBenefitsBundle
from ibkr_fa.parser.netbenefits.lots_csv import EspClosedLot
from ibkr_fa.parser.netbenefits.statement_pdf import StatementData
from tests._netbenefits_income import CG_COVERING, patch_statements
from tests._stockplan_income import write_dividend_report as _write_dividend_report

_real_netbenefits_form67_readiness = ibkr_fa.cli.netbenefits_form67_readiness
real_netbenefits_fa = ibkr_fa.cli._run_netbenefits_fa
_real_netbenefits_income = ibkr_fa.cli._run_netbenefits_income
_real_netbenefits_cg = ibkr_fa.cli._run_netbenefits_cg
real_stockplan_form67_readiness = ibkr_fa.cli.stockplan_form67_readiness
real_stockplan_income = ibkr_fa.cli._run_stockplan_income
real_stockplan_fa = ibkr_fa.cli._run_stockplan_fa
real_stockplan_cg_status = ibkr_fa.cli._run_stockplan_cg_status

# The last line of the ``netbenefits-all`` and ``stockplan-all`` summaries.
SUMMARY_FOOTER = (
    "  'ERROR' = that schedule was NOT produced; 'manual-fill' = user work remains; "
    "'not required' = the source proved zero activity. Do NOT file until every "
    "manual-fill or ERROR line is resolved."
)
# An income run's four outputs and its four debug outputs, the same for every
# source (StockPlan, NetBenefits and Fidelity CMA), then all eight in order.
INCOME_OUTPUTS = ("schedule-os.csv", "schedule-fsi.csv", "schedule-tr.csv", "form-67.csv")
INCOME_DEBUG_OUTPUTS = (
    "schedule-os-debug.csv",
    "schedule-fsi-debug.csv",
    "schedule-tr-debug.csv",
    "form-67-debug.csv",
)
INCOME_OUTPUTS_WITH_DEBUG = (*INCOME_OUTPUTS, *INCOME_DEBUG_OUTPUTS)
# A CG run's two outputs.
CG_OUTPUTS = ("schedule-cg.csv", "schedule-cg-ltcg.csv")


# --- netbenefits-all -----------------------------------------------------------

NETBENEFITS_ALL_OK = {"fa": _EXIT_OK, "income": _EXIT_OK, "cg": _EXIT_OK}
NETBENEFITS_ALL_OUTPUTS = sorted(
    ("schedule-fa-a3.csv", "schedule-fa-a2.csv", *INCOME_OUTPUTS, *CG_OUTPUTS)
)


def write_netbenefits_promised(key: str, args: argparse.Namespace) -> None:
    """Write every file the real handler promises on success or manual fill."""
    if key == "fa":
        names = (
            "schedule-fa-a3.csv",
            "schedule-fa-a2.csv",
            *(("schedule-fa-quicko.xlsx",) if args.quicko_workbook else ()),
        )
        out = args.output.parent
    else:
        names = (
            CG_OUTPUTS
            if key == "cg"
            else (*INCOME_OUTPUTS, *(INCOME_DEBUG_OUTPUTS if args.debug else ()))
        )
        out = args.output_dir
    for name in names:
        (out / name).write_text("placeholder", encoding="utf-8")


def patch_netbenefits_handlers(
    monkeypatch: pytest.MonkeyPatch, codes: dict[str, int]
) -> dict[str, argparse.Namespace]:
    """Stub the three netbenefits handlers; record the Namespace each receives."""
    calls: dict[str, argparse.Namespace] = {}

    def make(key: str):
        def handler(args: argparse.Namespace) -> int:
            calls[key] = args
            if codes[key] in (_EXIT_OK, _EXIT_MANUAL_FILL_REQUIRED):
                write_netbenefits_promised(key, args)
            return codes[key]

        return handler

    monkeypatch.setattr("ibkr_fa.cli._run_netbenefits_fa", make("fa"))
    monkeypatch.setattr("ibkr_fa.cli._run_netbenefits_income", make("income"))
    monkeypatch.setattr("ibkr_fa.cli._run_netbenefits_cg", make("cg"))
    # The legs are fakes, so the #601 Form 67 readiness preflight is too.
    monkeypatch.setattr(
        "ibkr_fa.cli.netbenefits_form67_readiness", lambda **_kwargs: Form67Readiness.READY
    )
    return calls


def run_netbenefits_all(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    codes: dict[str, int],
    *,
    extra: tuple[str, ...] = (),
    patch: bool = True,
) -> tuple[int, dict[str, argparse.Namespace], Path]:
    calls = patch_netbenefits_handlers(monkeypatch, codes) if patch else {}
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    out = tmp_path / "out"  # intentionally absent — orchestrator must mkdir it
    rc = ibkr_fa.cli.main(
        [
            "netbenefits-all",
            "--input",
            str(bundle),
            "--config",
            str(tmp_path / "user.toml"),
            "--output-dir",
            str(out),
            "--financial-year",
            "2026-27",
            "--itr-form",
            "ITR-2",
            *extra,
        ]
    )
    return rc, calls, out


def run_netbenefits_all_real_income(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    config: str,
    stub_income: bool,
    stub_cg: bool = True,
    stub_fa: bool = True,
    closed_lots: tuple[EspClosedLot, ...] = (),
    extra: tuple[str, ...] = (),
    statements: dict[str, StatementData] = CG_COVERING,
) -> tuple[int, dict[str, argparse.Namespace], Path]:
    """Run netbenefits-all, with ``extra`` arguments, over synthetic
    ``statements`` and ``closed_lots``; FA when ``stub_fa``, income when
    ``stub_income`` and CG when ``stub_cg`` are stubbed and recorded."""
    calls = patch_netbenefits_handlers(monkeypatch, NETBENEFITS_ALL_OK)
    monkeypatch.setattr(
        "ibkr_fa.cli.netbenefits_form67_readiness", _real_netbenefits_form67_readiness
    )
    if not stub_fa:
        monkeypatch.setattr("ibkr_fa.cli._run_netbenefits_fa", real_netbenefits_fa)
    if not stub_income:
        monkeypatch.setattr("ibkr_fa.cli._run_netbenefits_income", _real_netbenefits_income)
    if not stub_cg:
        monkeypatch.setattr("ibkr_fa.cli._run_netbenefits_cg", _real_netbenefits_cg)
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    for name in statements:
        (bundle / name).write_bytes(b"%PDF-1.4 placeholder")
    patch_statements(monkeypatch, statements)
    for pipeline in ("pipeline_netbenefits_income", "pipeline_netbenefits_cg"):
        monkeypatch.setattr(
            f"ibkr_fa.{pipeline}.load_bundle",
            lambda _input, **_kwargs: NetBenefitsBundle(
                open_lots=(), closed_lots=closed_lots, confirmations_by_date={}
            ),
        )
    cfg = tmp_path / "user.toml"
    cfg.write_text(config, encoding="utf-8")
    out = tmp_path / "out"
    rc = ibkr_fa.cli.main(
        [
            "netbenefits-all",
            "--input",
            str(bundle),
            "--config",
            str(cfg),
            "--output-dir",
            str(out),
            "--financial-year",
            "2025-26",
            "--itr-form",
            "ITR-2",
            *extra,
        ]
    )
    return rc, calls, out


def write_stale_netbenefits_outputs(tmp_path: Path) -> None:
    out = tmp_path / "out"
    out.mkdir()
    for name in (
        *NETBENEFITS_ALL_OUTPUTS,
        *INCOME_DEBUG_OUTPUTS,
        "schedule-fa-quicko.xlsx",
        "schedule-fa.csv",
        "schedule-fa-a3-a2.csv",
    ):
        (out / name).write_text("stale", encoding="utf-8")


# --- stockplan-all -------------------------------------------------------------

STOCKPLAN_ALL_OK = {"income": _EXIT_OK, "fa": _EXIT_OK, "cg": _SOURCE_NOT_REQUIRED}
STOCKPLAN_ALL_OUTPUTS = sorted((*INCOME_OUTPUTS, "schedule-fa.csv"))


def write_stockplan_promised(key: str, args: argparse.Namespace) -> None:
    """Write every file the real handler promises on success or manual fill."""
    if key == "fa":
        args.output.write_text("placeholder", encoding="utf-8")
    elif key == "income":
        for name in INCOME_OUTPUTS:
            (args.output_dir / name).write_text("placeholder", encoding="utf-8")


def patch_stockplan_handlers(
    monkeypatch: pytest.MonkeyPatch, codes: dict[str, int]
) -> dict[str, argparse.Namespace]:
    """Stub the two stockplan handlers; record the Namespace each receives."""
    calls: dict[str, argparse.Namespace] = {}

    def make(key: str):
        def handler(args: argparse.Namespace) -> int:
            calls[key] = args
            if codes[key] in (_EXIT_OK, _EXIT_MANUAL_FILL_REQUIRED):
                write_stockplan_promised(key, args)
            return codes[key]

        return handler

    monkeypatch.setattr("ibkr_fa.cli._run_stockplan_income", make("income"))
    monkeypatch.setattr("ibkr_fa.cli._run_stockplan_fa", make("fa"))
    monkeypatch.setattr("ibkr_fa.cli._run_stockplan_cg_status", make("cg"))
    # The legs are fakes, so the #601 Form 67 readiness preflight is too.
    monkeypatch.setattr(
        "ibkr_fa.cli.stockplan_form67_readiness", lambda **_kwargs: Form67Readiness.READY
    )
    return calls


def run_stockplan_all(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    codes: dict[str, int],
    *,
    extra: tuple[str, ...] = (),
    patch: bool = True,
) -> tuple[int, dict[str, argparse.Namespace], Path]:
    calls = patch_stockplan_handlers(monkeypatch, codes) if patch else {}
    input_dir = tmp_path / "in"
    input_dir.mkdir()
    out = tmp_path / "out"  # intentionally absent — orchestrator must mkdir it
    rc = ibkr_fa.cli.main(
        [
            "stockplan-all",
            "--input",
            str(input_dir),
            "--config",
            str(tmp_path / "user.toml"),
            "--output-dir",
            str(out),
            "--financial-year",
            "2025-26",
            "--itr-form",
            "ITR-2",
            *extra,
        ]
    )
    return rc, calls, out


def run_stockplan_all_real_income(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    config: str,
    stub_income: bool,
    **report: Any,
) -> tuple[int, dict[str, argparse.Namespace], Path]:
    """Run stockplan-all with a synthetic dividend report (``report`` overrides its
    defaults); FA and CG (and income when ``stub_income``) are stubbed and
    recorded."""
    calls = patch_stockplan_handlers(
        monkeypatch, {"income": _EXIT_OK, "fa": _EXIT_OK, "cg": _EXIT_OK}
    )
    monkeypatch.setattr("ibkr_fa.cli.stockplan_form67_readiness", real_stockplan_form67_readiness)
    if not stub_income:
        monkeypatch.setattr("ibkr_fa.cli._run_stockplan_income", real_stockplan_income)
    input_dir = tmp_path / "in"
    _write_dividend_report(input_dir, **report)
    cfg = tmp_path / "user.toml"
    cfg.write_text(config, encoding="utf-8")
    out = tmp_path / "out"
    rc = ibkr_fa.cli.main(
        [
            "stockplan-all",
            "--input",
            str(input_dir),
            "--config",
            str(cfg),
            "--output-dir",
            str(out),
            "--financial-year",
            "2025-26",
            "--itr-form",
            "ITR-2",
        ]
    )
    return rc, calls, out


# --- one harness per source ----------------------------------------------------


@dataclass(frozen=True)
class SourceAll:
    """One ``<source>-all`` command's success codes, leg stubs and CLI run, for
    tests that run the same check on both sources."""

    all_ok: dict[str, int]
    patch_handlers: Callable[[pytest.MonkeyPatch, dict[str, int]], dict[str, argparse.Namespace]]
    run: Callable[..., tuple[int, dict[str, argparse.Namespace], Path]]


NETBENEFITS_ALL = SourceAll(NETBENEFITS_ALL_OK, patch_netbenefits_handlers, run_netbenefits_all)
STOCKPLAN_ALL = SourceAll(STOCKPLAN_ALL_OK, patch_stockplan_handlers, run_stockplan_all)
