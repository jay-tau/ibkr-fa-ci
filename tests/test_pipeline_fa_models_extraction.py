"""Tests pinning the :mod:`ibkr_fa.pipeline_fa_models` extraction (issue #155).

Part of the #155 split: ``RunReport`` + ``OrphanDividendInfo`` moved
from :mod:`ibkr_fa.pipeline_fa` into their own models module.

Same contract as the other extraction tests:
* Direct imports work (new caller path).
* Legacy ``from ibkr_fa.pipeline_fa import RunReport,
  OrphanDividendInfo`` continues to work via the cosmetic re-import
  in pipeline_fa.py.
* Both import paths resolve to the SAME object.
* Module docstring references issue #155 for git-blame anchor.
"""

from __future__ import annotations

import subprocess
import sys

import ibkr_fa.pipeline_fa
import ibkr_fa.pipeline_fa_models


def test_direct_import_exposes_run_report_class() -> None:
    """The new module exposes :class:`RunReport` directly."""
    assert isinstance(ibkr_fa.pipeline_fa_models.RunReport, type)


def test_direct_import_exposes_orphan_dividend_info_class() -> None:
    """The new module exposes :class:`OrphanDividendInfo` directly."""
    assert isinstance(ibkr_fa.pipeline_fa_models.OrphanDividendInfo, type)


def test_legacy_pipeline_fa_import_path_still_works() -> None:
    """Backward-compat: ``from ibkr_fa.pipeline_fa import RunReport,
    OrphanDividendInfo`` continues to work via the cosmetic re-import
    in pipeline_fa.py. Eager (not lazy) so test files that
    ``isinstance(..., pipeline_fa.RunReport)``-check continue to
    pass against ``pipeline_fa_models.RunReport``-constructed
    instances."""
    assert hasattr(ibkr_fa.pipeline_fa, "RunReport")
    assert hasattr(ibkr_fa.pipeline_fa, "OrphanDividendInfo")


def test_legacy_and_direct_imports_are_the_same_object() -> None:
    """Identity check: both import paths MUST resolve to the same
    Python object. A duplicate-definition bug would break
    ``isinstance`` checks in caller code."""
    assert ibkr_fa.pipeline_fa.RunReport is ibkr_fa.pipeline_fa_models.RunReport
    assert ibkr_fa.pipeline_fa.OrphanDividendInfo is ibkr_fa.pipeline_fa_models.OrphanDividendInfo


def test_extraction_module_docstring_references_issue_155() -> None:
    """The extraction module's docstring must reference issue #155
    for git-blame anchor."""
    docstring = ibkr_fa.pipeline_fa_models.__doc__ or ""
    assert "#155" in docstring


def test_direct_import_works_without_pipeline_fa_preloaded() -> None:
    """Regression-lock: ``import ibkr_fa.pipeline_fa_models`` BEFORE
    ``import ibkr_fa.pipeline_fa`` must NOT trigger a circular
    ImportError. The new models module only imports from
    ``ibkr_fa.pipeline._require_identity_fallback_subset`` (no
    pipeline_fa back-import), so the leaf-level property is
    preserved."""
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import ibkr_fa.pipeline_fa_models as m; "
            "assert isinstance(m.RunReport, type); "
            "assert isinstance(m.OrphanDividendInfo, type); "
            "print('OK')",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, (
        f"direct import of pipeline_fa_models failed:\n"
        f"stdout: {result.stdout!r}\n"
        f"stderr: {result.stderr!r}"
    )
    assert result.stdout.strip() == "OK"
