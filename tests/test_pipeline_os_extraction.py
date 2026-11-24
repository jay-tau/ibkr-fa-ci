"""Tests pinning the :mod:`ibkr_fa.pipeline_os` extraction (issue #155).

Sister to :mod:`tests.test_pipeline_cg_extraction` and
:mod:`tests.test_pipeline_cg_ltcg_extraction` — verifies the third
per-subcommand extraction (Schedule OS). Same contract: direct
import path, docstring anchor, plus a subprocess regression test
pinning the circular-import fix landed in PR #192 round 2.
"""

from __future__ import annotations

import subprocess
import sys

import ibkr_fa.pipeline_os


def test_direct_import_exposes_run_function() -> None:
    """The new per-schedule module exposes the entry-point function
    directly so new callers can use the per-schedule import path."""
    assert callable(ibkr_fa.pipeline_os.run_schedule_os)


def test_direct_import_exposes_run_report_class() -> None:
    """The new per-schedule module exposes the report dataclass
    directly so new callers can use the per-schedule import path."""
    assert isinstance(ibkr_fa.pipeline_os.ScheduleOsRunReport, type)


def test_extraction_module_docstring_references_issue_155() -> None:
    """The extraction module's docstring must reference issue #155
    for git-blame anchor."""
    docstring = ibkr_fa.pipeline_os.__doc__ or ""
    assert "#155" in docstring


def test_direct_import_works_without_pipeline_preloaded() -> None:
    """Regression-lock for the T5-codex-PR-#192-P2 circular-import
    fix: ``import ibkr_fa.pipeline_os`` BEFORE ``import
    ibkr_fa.pipeline`` must NOT trigger a circular ImportError. See
    the sister test in ``tests/test_pipeline_cg_extraction.py`` for
    the full failure-mode explanation."""
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import ibkr_fa.pipeline_os; "
            "assert callable(ibkr_fa.pipeline_os.run_schedule_os); "
            "assert isinstance(ibkr_fa.pipeline_os.ScheduleOsRunReport, type); "
            "print('OK')",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, (
        f"direct import of pipeline_os failed:\n"
        f"stdout: {result.stdout!r}\n"
        f"stderr: {result.stderr!r}"
    )
    assert result.stdout.strip() == "OK"


def test_wildcard_import_preserves_pre_extraction_surface() -> None:
    """Regression-lock for T5 codex P2 on PR #193 round 2:
    pre-extraction the pipeline.py module imported several library
    symbols (``Rule115Policy``, ``Rule115OtherSourcesPolicy``,
    ``ScheduleOsIndianAdrRoutingMode``,
    ``compute_schedule_os_dividend_lines``,
    ``require_financial_year_contained_in_period``,
    ``ConfigError``, ``ParseError``, etc.) that were incidentally
    surfaced via ``from ibkr_fa.pipeline import *``. After the OS
    extraction the pipeline.py body no longer needs these, so they
    would silently drop from the wildcard surface unless explicitly
    re-imported for compat. This test pins the compat re-imports
    so any notebook / script that previously relied on the wildcard
    form to obtain them continues to work. #790 retired the
    per-subcommand writer lookups this surface also carried, and #1284
    ``require_financial_year_period``, which Schedule OS no longer uses."""
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "from ibkr_fa.pipeline import *\n"
            "for n in ('Rule115Policy', 'Rule115OtherSourcesPolicy', "
            "'ScheduleOsIndianAdrRoutingMode', "
            "'compute_schedule_os_dividend_lines', "
            "'compute_schedule_os_interest_lines', "
            "'require_financial_year_contained_in_period', "
            "'ConfigError', 'ParseError', 'timedelta', 'ROUND_HALF_UP'):\n"
            "    assert n in dir(), f'pre-extraction wildcard surface lost: {n}'\n"
            "print('OK')",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, (
        f"wildcard import compat test failed:\nstdout: {result.stdout!r}\nstderr: {result.stderr!r}"
    )
    assert result.stdout.strip() == "OK"
