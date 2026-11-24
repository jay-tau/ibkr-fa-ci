"""A stand-in for the lots' read of Robinhood position anchors (#1308).

The normalizer's replay keeps every digit, so a holding past the lots'
60-digit ledger, such as one left by a 1E-61-share sale, now leaves its
anchor's exact drift for the lots to block. Compute tests that reach a check
past that point read each anchor as main's 28-digit replay gave it.
"""

from __future__ import annotations

from dataclasses import replace
from types import ModuleType
from typing import Any

import pytest


def stand_in_rounded_anchor_ledgers(monkeypatch: pytest.MonkeyPatch, module: ModuleType) -> None:
    """Make ``module``'s lots read each anchor's ledger quantity as its
    statement quantity."""
    reconcile = module.reconcile_robinhood_security_lots

    def rounded(quantities: Any) -> Any:
        anchors = tuple(
            replace(anchor, ledger_quantity=anchor.statement_quantity)
            for anchor in quantities.anchors
        )
        return reconcile(replace(quantities, anchors=anchors))

    monkeypatch.setattr(module, "reconcile_robinhood_security_lots", rounded)
