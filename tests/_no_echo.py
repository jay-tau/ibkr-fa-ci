"""Shared synthetic privacy markers for the Robinhood no-echo checks (#1044).

Each marker stands for a value a statement, activity CSV or exception could
carry. A refusal, guidance line or leg reason must print none of them.
"""

from __future__ import annotations

from tests._crash_reports import assert_no_markers as _assert_absent

HOLDER = "Synthetic Holder Two"
CUSIP = "999600001"
ACCOUNT = "U1234567"
STRERROR = "Synthetic Strerror Marker"
FILENAME = "Synthetic Filename Marker"
# ``ANN`` sits inside many words, so it is checked as a whole token.
TICKER = "ANN"
MARKERS = (HOLDER, CUSIP, ACCOUNT, STRERROR, FILENAME, TICKER)


def assert_no_markers(text: str, *markers: str) -> None:
    """``text`` holds none of ``markers`` (default :data:`MARKERS`), compared
    case-insensitively; :data:`TICKER` only as a whole token."""
    _assert_absent(text, *(markers or MARKERS))
