"""One keyword-argument factory per run report type, holding every required
field with the pipeline tests' values (#1760). The command tests pass their
own counts, totals and paths as overrides."""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

from ibkr_fa.config import ScheduleOsIndianAdrRoutingMode
from ibkr_fa.output._cleanup import RunOutputs
from ibkr_fa.pipeline import IbkrCsvFormat


def cg_report_kwargs(**overrides: Any) -> dict[str, Any]:
    """:class:`ScheduleCgRunReport` keyword arguments, then ``overrides``."""
    base: dict[str, Any] = {
        "run_outputs": RunOutputs((), ()),
        "stcg_rows_written": 0,
        "excluded_ltcg_deferred_count": 0,
        "excluded_indian_source_count": 0,
        "excluded_out_of_fy_count": 0,
        "total_realized_gain_inr_rounded": Decimal("0"),
        "output_path": Path("out.csv"),
        "account_id": "U1234567",
        "account_id_cross_checked": True,
        "input_format": IbkrCsvFormat.ACTIVITY_STATEMENT,
        "financial_year_start": date(2025, 4, 1),
        "financial_year_end": date(2026, 3, 31),
        "fx_policy_appropriate_for_ay": True,
        "portal_labels_assessment_year_start": 2026,
        "table_f_loss_across_bands": (),
    }
    base.update(overrides)
    return base


def ltcg_report_kwargs(**overrides: Any) -> dict[str, Any]:
    """:class:`ScheduleCgLtcgRunReport` keyword arguments, then ``overrides``."""
    base: dict[str, Any] = {
        "run_outputs": RunOutputs((), ()),
        "ltcg_rows_written": 0,
        "excluded_stcg_count": 0,
        "excluded_pre_2024_07_23_count": 0,
        "excluded_indian_source_count": 0,
        "excluded_out_of_fy_count": 0,
        "total_realized_gain_inr_rounded": Decimal("0"),
        "output_path": Path("out.csv"),
        "account_id": "U1234567",
        "account_id_cross_checked": True,
        "input_format": IbkrCsvFormat.ACTIVITY_STATEMENT,
        "financial_year_start": date(2025, 4, 1),
        "financial_year_end": date(2026, 3, 31),
        "fx_policy_appropriate_for_ay": True,
        "portal_labels_assessment_year_start": 2026,
        "table_f_loss_across_bands": (),
    }
    base.update(overrides)
    return base


def os_report_kwargs(**overrides: Any) -> dict[str, Any]:
    """:class:`ScheduleOsRunReport` keyword arguments, then ``overrides``."""
    base: dict[str, Any] = {
        "run_outputs": RunOutputs((), ()),
        "ordinary_dividend_rows_written": 0,
        "indian_source_dividend_rows_written": 0,
        "ordinary_dividend_total_inr_rounded": Decimal("0"),
        "adr_reroutes_count": 0,
        "adr_routing_mode": ScheduleOsIndianAdrRoutingMode.DEPOSITARY,
        "output_path": Path("out.csv"),
        "account_id": "U1234567",
        "account_id_cross_checked": True,
        "input_format": IbkrCsvFormat.ACTIVITY_STATEMENT,
        "financial_year_start": date(2025, 4, 1),
        "financial_year_end": date(2026, 3, 31),
        "fx_policy_appropriate_for_ay": True,
        "portal_labels_assessment_year_start": 2026,
    }
    base.update(overrides)
    return base
