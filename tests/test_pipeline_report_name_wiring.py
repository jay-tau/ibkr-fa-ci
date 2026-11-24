"""Regression tests for per-pipeline ``report_name`` wiring through the
shared :func:`ibkr_fa.pipeline_fsi_compute._compute_schedule_fsi_for_pipeline`
seam.

Prior to the issue #259 fix, the seam hard-coded
``report_name="Schedule FSI reporting"`` in the period-validator call —
so when Schedule TR or Form 67 callers received a too-narrow Activity
Statement period, the ``ParseError`` message said "Schedule FSI
reporting requires..." even though the user had invoked
``schedule-tr`` or ``form-67`` from the CLI. These tests pin each
caller's correct schedule name in the error so a future refactor of
the seam can't silently regress.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from ibkr_fa.exceptions import ParseError
from ibkr_fa.pipeline import FinancialYear
from ibkr_fa.pipeline_form67 import run_form67
from ibkr_fa.pipeline_fsi import run_schedule_fsi
from ibkr_fa.pipeline_tr import run_schedule_tr
from tests._ibkr_user_toml import ACCOUNT_AND_FX_USER_TOML as _USER_TOML

# The bundled e2e fixture covers calendar year 2025 (2025-01-01 to
# 2025-12-31), which does NOT fully contain FY 2025-26 (2025-04-01 to
# 2026-03-31). All 3 pipelines reject this with a period-validator
# error; the regression is the schedule-name string in that error.
_TOO_NARROW_FIXTURE = Path(__file__).parent / "fixtures" / "e2e_activity_statement_2025.csv"
_FY_2025_26 = FinancialYear(start_year=2025)


@pytest.fixture
def setup(tmp_path: Path) -> tuple[Path, Path]:
    cfg = tmp_path / "user.toml"
    cfg.write_text(_USER_TOML, encoding="utf-8")
    out = tmp_path / "out.csv"
    return cfg, out


def test_schedule_fsi_period_error_self_identifies_as_schedule_fsi(
    setup: tuple[Path, Path],
) -> None:
    """The Schedule FSI pipeline's period-narrowness error message
    must say "Schedule FSI" — the legacy default ``report_name``.
    """
    cfg, out = setup
    with pytest.raises(ParseError) as exc:
        run_schedule_fsi(
            input_path=_TOO_NARROW_FIXTURE,
            config_path=cfg,
            financial_year=_FY_2025_26,
            output_path=out,
        )
    assert "Schedule FSI reporting" in exc.value.message
    assert "Schedule TR" not in exc.value.message
    assert "Form 67" not in exc.value.message


def test_schedule_tr_period_error_self_identifies_as_schedule_tr(
    setup: tuple[Path, Path],
) -> None:
    """The Schedule TR pipeline's period-narrowness error message
    must say "Schedule TR" — NOT "Schedule FSI" (the issue #259
    regression). Schedule TR users invoking ``schedule-tr`` from
    the CLI shouldn't see the upstream FSI seam's name in their
    error.
    """
    cfg, out = setup
    with pytest.raises(ParseError) as exc:
        run_schedule_tr(
            input_path=_TOO_NARROW_FIXTURE,
            config_path=cfg,
            financial_year=_FY_2025_26,
            output_path=out,
        )
    assert "Schedule TR foreign-tax-relief reporting" in exc.value.message
    assert "Schedule FSI reporting" not in exc.value.message
    assert "Form 67" not in exc.value.message


def test_form67_period_error_self_identifies_as_form_67(
    setup: tuple[Path, Path],
) -> None:
    """The Form 67 pipeline's period-narrowness error message
    must say "Form 67" — NOT "Schedule FSI" (the issue #259
    regression).
    """
    cfg, out = setup
    with pytest.raises(ParseError) as exc:
        run_form67(
            input_path=_TOO_NARROW_FIXTURE,
            config_path=cfg,
            financial_year=_FY_2025_26,
            output_path=out,
        )
    assert "Form 67 foreign-tax-credit reporting" in exc.value.message
    assert "Schedule FSI reporting" not in exc.value.message
    assert "Schedule TR" not in exc.value.message
