"""The Flex year-end statement's user.toml and the Schedule FA A3 missing-field
helpers that the end-to-end Flex test, the blank Address of entity tests and the
comma-only text tests share.

- ``FLEX_YEAR_END_CONFIG``: the user.toml the end-to-end Flex test runs
  ``tests._fa_a3_name_runs.IBKR_FLEX_STATEMENT`` with: ``[account]`` and
  ``[fx]`` only. It is the shared ``ACCOUNT_AND_FX_USER_TOML`` from
  ``tests._ibkr_user_toml``.
- ``ADDRESS_MISSING_HEADING`` and ``ZIP_MISSING_HEADING``: the first line of the
  manual-fill block that lists the holdings without an Address of entity or a
  ZIP Code.
- ``listed_holdings``: the holdings listed under the block a heading starts.
"""

from __future__ import annotations

from tests._ibkr_user_toml import ACCOUNT_AND_FX_USER_TOML

FLEX_YEAR_END_CONFIG = ACCOUNT_AND_FX_USER_TOML

ADDRESS_MISSING_HEADING = "   Address of entity is missing for these holdings;"
ZIP_MISSING_HEADING = "   ZIP Code is missing for these holdings;"


def listed_holdings(stdout: str, heading: str) -> list[str]:
    """The holdings listed under the manual-fill block starting ``heading``."""
    lines = stdout.splitlines()
    start = next(index for index, line in enumerate(lines) if line.startswith(heading))
    names = []
    for line in lines[start + 1 :]:
        if not line.startswith("     - "):
            break
        names.append(line.removeprefix("     - "))
    return names
