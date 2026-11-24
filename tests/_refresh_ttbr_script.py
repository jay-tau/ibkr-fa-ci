"""The ``scripts/refresh_ttbr.py`` module, loaded once from its file path, and
the upstream TTBR CSV builder that the refresh script tests and the TTBR and
price limit tests share.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

# The script lives in scripts/ which isn't a package, so import it
# from its file path. Cache the imported module on the test module so
# pytest-xdist workers reuse it.
_SCRIPT_PATH = Path(__file__).resolve().parent.parent / "scripts" / "refresh_ttbr.py"
_spec = importlib.util.spec_from_file_location("refresh_ttbr", _SCRIPT_PATH)
assert _spec is not None and _spec.loader is not None
refresh_ttbr = importlib.util.module_from_spec(_spec)
sys.modules["refresh_ttbr"] = refresh_ttbr
_spec.loader.exec_module(refresh_ttbr)


TTBR_UPSTREAM_HEADER = (
    "DATE,PDF FILE,TT BUY,TT SELL,BILL BUY,BILL SELL,"
    "FOREX TRAVEL CARD BUY,FOREX TRAVEL CARD SELL,CN BUY,CN SELL"
)


def make_upstream(rows: list[str]) -> str:
    return TTBR_UPSTREAM_HEADER + "\n" + "\n".join(rows) + "\n"
