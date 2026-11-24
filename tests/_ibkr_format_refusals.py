"""The IBKR format detector's refusal that several test modules check.

- ``no_sentinel_refusal``: the ``(section, row_index, column, message)`` that
  ``detect_ibkr_csv_format`` refuses with when the first non-blank record of
  the input at ``path``, starting on zero-based line ``row``, matches no known
  IBKR export format.
"""

from __future__ import annotations

from pathlib import Path


def no_sentinel_refusal(row: int, path: Path) -> tuple[str, int, str | None, str]:
    return (
        "<file>",
        row,
        None,
        f"input CSV {path}'s first non-blank record doesn't match a known IBKR export format. "
        "Expected one of:\n  * Activity Statement: first two cells ('Statement', 'Header'); "
        "or\n  * Flex Query: first cell in ['BOF', 'ClientAccountID', 'CurrencyPrimary'].",
    )
