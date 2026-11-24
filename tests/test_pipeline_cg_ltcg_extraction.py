"""Tests pinning the :mod:`ibkr_fa.pipeline_cg_ltcg` extraction (issue #155).

Verifies:

* The module exposes ``ScheduleCgLtcgRunReport`` and
  ``run_schedule_cg_ltcg`` directly.
* The module's docstring documents the issue #155 extraction
  rationale.

These tests don't exercise the actual LTCG compute path — that's
covered by the pre-extraction tests in
``tests/test_pipeline_schedule_cg_ltcg.py`` and the e2e suites. The
no-behaviour-change invariant is pinned by those tests continuing
to pass against the refactored module.
"""

from __future__ import annotations

import ibkr_fa.pipeline_cg_ltcg


def test_direct_import_exposes_run_function() -> None:
    """The new per-schedule module exposes the entry-point function
    directly so new callers can use the per-schedule import path."""
    assert callable(ibkr_fa.pipeline_cg_ltcg.run_schedule_cg_ltcg)


def test_direct_import_exposes_run_report_class() -> None:
    """The new per-schedule module exposes the report dataclass
    directly so new callers can use the per-schedule import path."""
    assert isinstance(ibkr_fa.pipeline_cg_ltcg.ScheduleCgLtcgRunReport, type)


def test_extraction_module_docstring_references_issue_155() -> None:
    """The extraction module's docstring must reference issue #155 so
    a future reader doing ``git blame`` finds the rationale for the
    split. Locks the documentation so a future refactor doesn't
    accidentally drop the issue link."""
    docstring = ibkr_fa.pipeline_cg_ltcg.__doc__ or ""
    assert "#155" in docstring


def test_direct_import_works_without_pipeline_preloaded() -> None:
    """Regression-lock for the T5-codex-PR-#192-P2 circular-import
    fix (which also retroactively repairs the same bug that landed
    silently in this LTCG extraction's PR #191): ``import
    ibkr_fa.pipeline_cg_ltcg`` BEFORE ``import ibkr_fa.pipeline``
    must NOT trigger a circular ImportError. See
    ``tests/test_pipeline_cg_extraction.py`` sister test for the
    full failure-mode explanation."""
    import subprocess  # noqa: PLC0415 — test-local
    import sys  # noqa: PLC0415 — test-local

    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import ibkr_fa.pipeline_cg_ltcg; "
            "assert callable(ibkr_fa.pipeline_cg_ltcg.run_schedule_cg_ltcg); "
            "assert isinstance(ibkr_fa.pipeline_cg_ltcg.ScheduleCgLtcgRunReport, type); "
            "print('OK')",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, (
        f"direct import of pipeline_cg_ltcg failed:\n"
        f"stdout: {result.stdout!r}\n"
        f"stderr: {result.stderr!r}"
    )
    assert result.stdout.strip() == "OK"
