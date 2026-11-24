"""The IBKR account cross-check's fixed refusals (#1047), written once.

Neither text prints an account number, masked or not. Tests pin these literals
rather than reading them from :mod:`ibkr_fa`, so a changed wording fails them.
"""

from __future__ import annotations

from typing import Final

ACCOUNT_MISMATCH_REFUSAL: Final = (
    "the account number in --input doesn't match account.number in user.toml. "
    "Refusing to process — this defence prevents accidentally filing someone "
    "else's holdings on your return."
)
MASKED_ACCOUNT_REFUSAL: Final = (
    "the statement in --input masks its account number, so it can't be checked "
    "against account.number in user.toml. This is the default behaviour for IBKR "
    "Activity Statement exports. Either re-export with 'Show full account number' "
    "enabled, or pass --allow-masked-account to skip the safety net (you are then "
    "responsible for ensuring the statement belongs to the same account as user.toml)."
)


def cli_account_refusal(text: str) -> str:
    """The whole stderr of a command that refuses with ``text``."""
    return f"error: ConfigError at field 'account.number': {text}\n"
