"""Input checks the Robinhood income, capital-gains and options adapters share (#1612).

Each adapter refuses with its own error type and text when one fails.
"""

from __future__ import annotations

from datetime import date
from typing import Final

from ibkr_fa.config import RobinhoodConfig
from ibkr_fa.normalize.robinhood.accounts import RobinhoodAccountsReconciliation

_ACCOUNT_COUNT: Final = 2


def is_one_financial_year(start: date, end: date) -> bool:
    """Whether ``start`` to ``end`` is one complete April-to-March financial year."""
    expected_end = date(start.year + 1, 3, 31)
    return (start.month, start.day) == (4, 1) and end == expected_end


def holds_two_accounts(paired: RobinhoodAccountsReconciliation, config: RobinhoodConfig) -> bool:
    """Whether the reconciled bundle and the config each hold two accounts."""
    return len(paired.accounts) == _ACCOUNT_COUNT and len(config.accounts) == _ACCOUNT_COUNT


def same_distinct_account_numbers(
    paired: RobinhoodAccountsReconciliation, config: RobinhoodConfig
) -> bool:
    """Whether the reconciled and configured account numbers are the same two
    distinct numbers, once :func:`holds_two_accounts` holds."""
    paired_numbers = {account.activity.primary_bundle.account_number for account in paired.accounts}
    configured_numbers = {account.number for account in config.accounts}
    return len(paired_numbers) == _ACCOUNT_COUNT and paired_numbers == configured_numbers
