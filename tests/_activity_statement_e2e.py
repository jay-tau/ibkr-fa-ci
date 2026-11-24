"""The Activity Statement end-to-end config and refusal text that several test
modules share.

- ``ACTIVITY_STATEMENT_E2E_USER_TOML``: the ``user.toml`` text the Activity
  Statement end-to-end runs use. It is the shared ``ACCOUNT_AND_FX_USER_TOML``
  from ``tests._ibkr_user_toml``.
- ``OUTSIDE_PERIOD``: the refusal text for a row dated outside the statement's
  own Period (#1143).
"""

from __future__ import annotations

from tests._ibkr_user_toml import ACCOUNT_AND_FX_USER_TOML

ACTIVITY_STATEMENT_E2E_USER_TOML = ACCOUNT_AND_FX_USER_TOML

# ---- rows dated outside the statement's own Period (#1143) ------------------

OUTSIDE_PERIOD = (
    "this row is dated outside the statement's Period; a statement whose rows "
    "contradict its own period can't prove its totals. Re-export the Activity "
    "Statement for one period."
)
