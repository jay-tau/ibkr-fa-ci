"""Shared helper for tests that pin a Schedule FA A2 row's peak provenance."""

from __future__ import annotations

from dataclasses import replace

from ibkr_fa.models.fa_a2_row import FaA2Row


def without_peak(row: FaA2Row) -> FaA2Row:
    """``row`` with the filed peak and every peak audit field cleared."""
    return replace(
        row,
        peak_balance_inr=None,
        audit_peak_balance_lower_inr=None,
        audit_peak_balance_upper_inr=None,
        audit_peak_balance_lower_date=None,
        audit_peak_balance_upper_date=None,
        audit_peak_balance_bound=None,
        audit_peak_balance_base_ccy=None,
        audit_peak_ttbr=None,
        audit_statement_peak_floor_base_ccy=None,
        audit_statement_peak_floor_date=None,
        audit_peak_override_source=None,
        audit_peak_override_reason=None,
    )
