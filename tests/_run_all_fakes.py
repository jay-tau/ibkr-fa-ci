"""The ``run-all`` step doubles and argument list that the ``run-all`` and CLI
crash report tests share. Tests patch the doubles over the step functions on
:mod:`ibkr_fa.cli` by name, so no real pipeline runs.

- ``RUN_ALL_FY_STEPS``: the names of the ``ibkr_fa.cli`` step functions that
  read ``--fy-input``; ``RUN_ALL_STEPS``: those and ``_schedule_fa_step``.
- ``RUN_ALL_FY_SCHEDULE_NAMES``: the summary name of each ``RUN_ALL_FY_STEPS``
  schedule, in order; ``RUN_ALL_SCHEDULE_NAMES``: those and ``schedule-fa``.
- ``write_promised_outputs``: writes every file a step's real handler promises.
- ``fake_step_report``: a ``Mock`` stand-in for a schedule's run report.
- ``fake_step``: a step double that optionally writes its promised outputs and
  returns a chosen exit code and, unless that code is an error, a stand-in
  report.
- ``patch_all_steps``: patches every ``run-all`` step on ``ibkr_fa.cli`` with a
  ``fake_step``.
- ``fake_run_all_argv``: the ``run-all`` argument list for a financial-year
  statement, a calendar-year statement, a ``user.toml`` and an output folder.
"""

from __future__ import annotations

import argparse
from pathlib import Path
from unittest.mock import Mock

import pytest

from ibkr_fa import cli
from ibkr_fa.pipeline_cg import ScheduleCgRunReport
from ibkr_fa.pipeline_cg_ltcg import ScheduleCgLtcgRunReport
from ibkr_fa.pipeline_fa import RunReport
from ibkr_fa.pipeline_fa_a2 import fa_a2_path
from ibkr_fa.pipeline_form67 import Form67RunReport
from ibkr_fa.pipeline_fsi import ScheduleFsiRunReport
from ibkr_fa.pipeline_os import ScheduleOsRunReport
from ibkr_fa.pipeline_tr import ScheduleTrRunReport

RUN_ALL_FY_STEPS = (
    "_schedule_os_step",
    "_schedule_cg_step",
    "_schedule_cg_ltcg_step",
    "_schedule_fsi_step",
    "_schedule_tr_step",
    "_form67_step",
)
RUN_ALL_STEPS = (*RUN_ALL_FY_STEPS, "_schedule_fa_step")
# The summary name of each RUN_ALL_FY_STEPS schedule, in order.
RUN_ALL_FY_SCHEDULE_NAMES = (
    "schedule-os",
    "schedule-cg",
    "schedule-cg-ltcg",
    "schedule-fsi",
    "schedule-tr",
    "form-67",
)
RUN_ALL_SCHEDULE_NAMES = (*RUN_ALL_FY_SCHEDULE_NAMES, "schedule-fa")
_REPORT_TYPES = dict(
    zip(
        RUN_ALL_SCHEDULE_NAMES,
        (
            ScheduleOsRunReport,
            ScheduleCgRunReport,
            ScheduleCgLtcgRunReport,
            ScheduleFsiRunReport,
            ScheduleTrRunReport,
            Form67RunReport,
            RunReport,
        ),
        strict=True,
    )
)


def write_promised_outputs(args: argparse.Namespace, text: str = "placeholder") -> None:
    """Write every file the real handler promises: Schedule FA also writes
    Table A2, and Form 67 its debug CSV under ``--debug-form67``."""
    output = Path(args.output)
    output.write_text(text, encoding="utf-8")
    if output.stem == "schedule-fa":
        fa_a2_path(output).write_text(text, encoding="utf-8")
    if output.stem == "form-67" and args.debug_form67:
        output.with_name("form-67-debug.csv").write_text(text, encoding="utf-8")


def fake_step_report(name: str, **fields: object) -> Mock:
    """A stand-in for schedule ``name``'s run report. It passes ``isinstance``
    for the real report type, and no global advisory or checklist item fires
    unless ``fields`` sets one. Schedule FA has Table A2 rows."""
    clean: dict[str, object] = {
        "account_id_cross_checked": True,
        "has_foreign_tax_without_taxpayer_id": False,
        "has_unresolved_indian_tax_payable": False,
        "override_advice": (),
        "a2_rows": ("A2 row",),
        "official_output_withheld": False,
    }
    report = Mock(spec=_REPORT_TYPES[name])
    report.configure_mock(**(clean | fields))
    return report


def fake_step(code: int, *, write: bool = True, **fields: object):
    """A step double: optionally writes its promised outputs, returns code
    and, unless code is an error, a stand-in report with ``fields``."""

    def step(
        args: argparse.Namespace, *, include_global_advisories: bool
    ) -> tuple[int, Mock | None]:
        assert not include_global_advisories
        if write:
            write_promised_outputs(args)
        return code, fake_step_report(Path(args.output).stem, **fields) if code in (0, 3) else None

    return step


def patch_all_steps(monkeypatch: pytest.MonkeyPatch, code: int = 0, *, write: bool = True) -> None:
    for name in RUN_ALL_STEPS:
        monkeypatch.setattr(cli, name, fake_step(code, write=write))


def fake_run_all_argv(
    paths: tuple[Path, Path, Path, Path], *extra: str, with_cy: bool = True
) -> list[str]:
    fy, cy, cfg, out = paths
    base = [
        "run-all",
        "--fy-input",
        str(fy),
        "--financial-year",
        "2025-26",
        "--itr-form",
        "ITR-2",
        "--config",
        str(cfg),
        "--output-dir",
        str(out),
    ]
    if with_cy:
        base += ["--cy-input", str(cy)]
    return [*base, *extra]
