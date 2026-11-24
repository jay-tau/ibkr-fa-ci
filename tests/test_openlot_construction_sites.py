"""Ratchet the modules that build an ``OpenLot`` directly (#627).

New sources hand the engine a :class:`~ibkr_fa.models.holding_lot.HoldingLot`
and :func:`ibkr_fa.compute.holding_valuation.value_holding_lot` values it.
Moving a site onto ``HoldingLot`` shrinks this allowlist; it must not grow.
"""

from __future__ import annotations

import re
from pathlib import Path

import ibkr_fa

_PACKAGE_ROOT = Path(ibkr_fa.__file__).parent
# ``\b`` keeps ``RobinhoodOptionOpenLot(`` out of the match.
_OPEN_LOT_CALL = re.compile(r"\bOpenLot\(")
_ALLOWED = {
    "compute/fidelity_cma_capital_gains.py",
    "compute/holding_valuation.py",
    "compute/lot_matching.py",
    "compute/robinhood_fa.py",
    "parser/open_positions.py",
}


def test_open_lot_is_built_only_in_the_allowed_modules() -> None:
    builders = {
        path.relative_to(_PACKAGE_ROOT).as_posix()
        for path in _PACKAGE_ROOT.rglob("*.py")
        if _OPEN_LOT_CALL.search(path.read_text(encoding="utf-8"))
    }
    assert builders == _ALLOWED
