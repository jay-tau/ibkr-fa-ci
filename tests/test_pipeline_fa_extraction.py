"""Tests pinning the :mod:`ibkr_fa.pipeline_fa` extraction (issue #155).

Sister to the CG / CG-LTCG / OS extraction tests — verifies the
fourth and final per-subcommand extraction (Schedule FA Table A3).
With this extraction, all four subcommand pipelines (FA, OS, CG,
CG-LTCG) live in their own ``pipeline_<schedule>.py`` modules; the
remaining shared core (dispatchers, AY checks, FinancialYear,
identity helpers) stays in ``ibkr_fa.pipeline`` pending the
final split that closes #155.

Same contract as the sister extraction tests:
* Direct imports work (new caller path).
* Module docstring references issue #155 for git-blame anchor.
* Subprocess regression for circular-import (fresh sys.modules).
"""

from __future__ import annotations

import subprocess
import sys

import ibkr_fa.pipeline_fa


def test_direct_import_exposes_run_function() -> None:
    """The new per-schedule module exposes ``run_fa_a3`` directly."""
    assert callable(ibkr_fa.pipeline_fa.run_fa_a3)


def test_direct_import_exposes_run_report_class() -> None:
    """The new per-schedule module exposes ``RunReport`` directly."""
    assert isinstance(ibkr_fa.pipeline_fa.RunReport, type)


def test_direct_import_exposes_orphan_dividend_info_class() -> None:
    """OrphanDividendInfo moved to pipeline_fa.py with the FA extraction
    (it's only used by RunReport + the FA orphan-dividend classifier)."""
    assert isinstance(ibkr_fa.pipeline_fa.OrphanDividendInfo, type)


def test_direct_import_exposes_manual_fill_portal_columns() -> None:
    """The static tuple exposes every A3 field that may need manual fill."""
    assert isinstance(ibkr_fa.pipeline_fa.MANUAL_FILL_PORTAL_COLUMNS, tuple)
    assert len(ibkr_fa.pipeline_fa.MANUAL_FILL_PORTAL_COLUMNS) == 7


def test_extraction_module_docstring_references_issue_155() -> None:
    """The extraction module's docstring must reference issue #155
    for git-blame anchor."""
    docstring = ibkr_fa.pipeline_fa.__doc__ or ""
    assert "#155" in docstring


def test_direct_import_works_without_pipeline_preloaded() -> None:
    """Regression-lock for the T5-codex-PR-#192-P2 circular-import
    fix applied across all per-schedule extractions: ``import
    ibkr_fa.pipeline_fa`` BEFORE ``import ibkr_fa.pipeline`` must NOT
    trigger a circular ImportError. See the sister test in
    ``tests/test_pipeline_cg_extraction.py`` for the full
    failure-mode explanation."""
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import ibkr_fa.pipeline_fa; "
            "assert callable(ibkr_fa.pipeline_fa.run_fa_a3); "
            "assert isinstance(ibkr_fa.pipeline_fa.RunReport, type); "
            "assert isinstance(ibkr_fa.pipeline_fa.OrphanDividendInfo, type); "
            "assert isinstance(ibkr_fa.pipeline_fa.MANUAL_FILL_PORTAL_COLUMNS, tuple); "
            "print('OK')",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, (
        f"direct import of pipeline_fa failed:\n"
        f"stdout: {result.stdout!r}\n"
        f"stderr: {result.stderr!r}"
    )
    assert result.stdout.strip() == "OK"


def test_wildcard_import_preserves_pre_fa_extraction_surface() -> None:
    """Regression-lock for T5 codex P2 on PR #194 (mirrors the
    OS-extraction's wildcard-compat regression test in
    ``tests/test_pipeline_os_extraction.py``): pre-FA-extraction
    pipeline.py imported FA helpers (``aggregate_dividends_by_symbol_inr``,
    ``build_fa_a3_rows``, ``overlapping_lots_for_cy``,
    ``FaExactDatePolicy``) for use by run_fa_a3.
    After moving run_fa_a3 to pipeline_fa.py, those helpers would
    silently drop from the wildcard surface unless explicitly
    re-imported for compat. #790 retired the per-subcommand writer
    lookups this surface also carried."""
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "from ibkr_fa.pipeline import *\n"
            "for n in ('aggregate_dividends_by_symbol_inr', "
            "'build_fa_a3_rows', 'overlapping_lots_for_cy', "
            "'FaExactDatePolicy', "
            "'IbkrCsvFormat', 'detect_ibkr_csv_format', "
            "'load_user_config', 'Side', 'Ticker', 'Literal', "
            "'functools', 'Rule115CapitalGainsPolicy', "
            "'compute_schedule_cg_stcg_lines', "
            "'ScheduleCgStcgComputeResult', "
            "'ScheduleCgLtcgComputeResult', "
            "'compute_schedule_cg_ltcg_lines'):\n"
            "    assert n in dir(), f'pre-FA-extraction wildcard surface lost: {n}'\n"
            "print('OK')",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, (
        f"FA-wildcard-compat test failed:\nstdout: {result.stdout!r}\nstderr: {result.stderr!r}"
    )
    assert result.stdout.strip() == "OK"
