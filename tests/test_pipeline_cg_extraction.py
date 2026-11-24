"""Tests pinning the :mod:`ibkr_fa.pipeline_cg` extraction (issue #155).

Sister to :mod:`tests.test_pipeline_cg_ltcg_extraction` — verifies
the second per-subcommand extraction (Schedule CG STCG). Same
contract: direct import path and docstring anchor. Also pins the T5-codex-PR-#192 P2 circular-import
fix (subprocess test verifying ``import ibkr_fa.pipeline_cg`` works
when pipeline.py hasn't been loaded yet).
"""

from __future__ import annotations

import subprocess
import sys

import ibkr_fa.pipeline_cg


def test_direct_import_exposes_run_function() -> None:
    """The new per-schedule module exposes the entry-point function
    directly so new callers can use the per-schedule import path."""
    assert callable(ibkr_fa.pipeline_cg.run_schedule_cg)


def test_direct_import_exposes_run_report_class() -> None:
    """The new per-schedule module exposes the report dataclass
    directly so new callers can use the per-schedule import path."""
    assert isinstance(ibkr_fa.pipeline_cg.ScheduleCgRunReport, type)


def test_extraction_module_docstring_references_issue_155() -> None:
    """The extraction module's docstring must reference issue #155
    for git-blame anchor."""
    docstring = ibkr_fa.pipeline_cg.__doc__ or ""
    assert "#155" in docstring


def test_direct_import_works_without_pipeline_preloaded() -> None:
    """Regression-lock for T5 codex P2 on PR #192: a caller doing
    ``import ibkr_fa.pipeline_cg`` BEFORE ``import ibkr_fa.pipeline``
    must NOT trigger a circular ImportError.

    Pre-fix, the eager re-export at the bottom of pipeline.py
    (``from ibkr_fa.pipeline_cg import …``) raised
    ``ImportError: cannot import name 'ScheduleCgRunReport' from
    partially initialized module`` because pipeline.py was loading
    pipeline_cg, which was loading pipeline, which then tried to
    re-import names from pipeline_cg before pipeline_cg's body had
    finished executing.

    Fixed by replacing the eager re-export with a PEP 562
    ``__getattr__`` lazy resolver, which #669 removed along with the
    re-export. This test runs in a SUBPROCESS so it gets a fresh
    ``sys.modules`` (other tests in this process have already imported
    pipeline, which would mask the bug)."""
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import ibkr_fa.pipeline_cg; "
            "assert callable(ibkr_fa.pipeline_cg.run_schedule_cg); "
            "assert isinstance(ibkr_fa.pipeline_cg.ScheduleCgRunReport, type); "
            "print('OK')",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, (
        f"direct import of pipeline_cg failed:\n"
        f"stdout: {result.stdout!r}\n"
        f"stderr: {result.stderr!r}"
    )
    assert result.stdout.strip() == "OK"
