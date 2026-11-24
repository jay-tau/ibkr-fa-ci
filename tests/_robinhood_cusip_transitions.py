"""Synthetic Robinhood CUSIP transitions file input, shared by the CUSIP
transitions and Robinhood refusal-chain tests (#1823).

:data:`CUSIP_TRANSITION_ACCOUNTS` holds the configured account numbers, and
:func:`cusip_transition` builds one ``[[transitions]]`` table of the file.
"""

from __future__ import annotations

CUSIP_TRANSITION_ACCOUNTS = frozenset({"111111111", "222222222"})


def cusip_transition(**replacements: str) -> str:
    """One ``[[transitions]]`` table, with ``replacements`` replacing or adding keys."""
    values = {
        "account_number": '"222222222"',
        "symbol": '"XQAO"',
        "old_cusip": '"99992G106"',
        "new_cusip": '"99992G205"',
        "new_cusip_from": "2026-07-01",
    }
    values.update(replacements)
    return "[[transitions]]\n" + "".join(f"{key} = {value}\n" for key, value in values.items())
