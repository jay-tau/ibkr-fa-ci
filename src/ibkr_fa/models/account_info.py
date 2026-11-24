"""Account Information row from an IBKR Flex Query CSV.

The Account Information section emits exactly one row identifying the
IBKR account this statement covers. v1 uses it to validate that the
statement's account number matches the account number declared in
``user.toml`` — a defence against accidentally running the tool on
someone else's statement.

Field shape follows IBKR's documented Flex Reference column names.
The exact set of columns IBKR emits varies by Flex Query configuration;
the parser will tolerate extra columns and require only the ones
modelled here.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Final

from ibkr_fa.invariants import require
from ibkr_fa.types import AccountId

# ISO 4217 currency codes are exactly 3 uppercase ASCII letters.
_CURRENCY_PATTERN: Final[re.Pattern[str]] = re.compile(r"^[A-Z]{3}$")


@dataclass(frozen=True, slots=True)
class AccountInfo:
    """Identifies the IBKR account a statement belongs to.

    Construction-time invariants (see :meth:`__post_init__`) reject
    blatantly-empty identity fields and malformed currency codes so
    the cross-check against ``user.toml`` has something meaningful to
    compare.

    Attributes:
        account_id: IBKR account number (e.g., ``"U1234567"``). This is
            the value :func:`ibkr_fa.config.load_user_config` checks
            against to refuse to process a statement that doesn't match
            ``user.toml``'s ``[account].number``.
        account_holder_name: Name of the legal account holder, as IBKR
            stores it. We do **not** validate this against
            ``user.toml`` (the user's name in IBKR's records may differ
            in casing/initials from how they wrote it in ``user.toml``)
            but it is preserved for FA Table A2 future use and for
            inclusion in the audit columns of the output CSV.
        base_currency: Account's base currency (e.g., ``"USD"``).
            v1 hard-fails on non-USD lots regardless of this; preserved
            for future multi-currency support.
    """

    account_id: AccountId
    account_holder_name: str
    base_currency: str

    def __post_init__(self) -> None:
        """Fail-fast structural invariants. See class docstring."""
        require(
            self.account_id != "",
            what="AccountInfo.account_id must be non-empty",
        )
        require(
            self.account_holder_name != "",
            what="AccountInfo.account_holder_name must be non-empty",
        )
        # No value in the context: a shifted cell can hold the holder's name or account.
        require(
            _CURRENCY_PATTERN.fullmatch(self.base_currency) is not None,
            what="AccountInfo.base_currency must be 3 uppercase ASCII letters (ISO 4217)",
        )
