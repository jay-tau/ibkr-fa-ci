"""Parser for the IBKR Flex Query Account Information section.

Account Information is a singleton section: IBKR emits exactly one
data row per account containing identity metadata. v1 reads it for two
reasons:

1. **Account-number validation** — the upstream pipeline cross-checks
   the parsed :attr:`AccountInfo.account_id` against ``user.toml``'s
   ``[account].number`` to refuse to process a statement that doesn't
   belong to the configured account.
2. **Identity metadata** — the parsed record keeps the holder name
   with the account number and base currency.

The section's column composition varies by Flex Query config (the user
can enable/disable individual fields in IBKR's configurator), so this
parser **tolerates extra columns** and only requires five:
``ClientAccountID``, ``Name``, ``AccountType``, ``CurrencyPrimary`` and
``IBEntity``. ``AccountType`` and ``IBEntity`` are checked but not kept.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Final

from ibkr_fa.exceptions import ParseError
from ibkr_fa.models.account_info import _CURRENCY_PATTERN, AccountInfo
from ibkr_fa.types import AccountId

_SECTION: Final[str] = "Account Information"

# Flex columns that must have non-blank values. ``IBEntity`` is also
# required to be present (``_require``, so users get a clear error if
# their Flex config disables it) but its value may be blank.
_NON_EMPTY_COLUMNS: Final[frozenset[str]] = frozenset(
    {"ClientAccountID", "Name", "AccountType", "CurrencyPrimary"}
)


def _require(row: dict[str, str], column: str, *, row_index: int) -> str:
    if column not in row:
        raise ParseError(
            section=_SECTION,
            row_index=row_index,
            column=column,
            message=(
                f"required Account Information column {column!r} is "
                f"missing from the section header. Re-run your IBKR "
                f"Flex Query with this field enabled."
            ),
            raw_value="",
        )
    value = row[column]
    if column in _NON_EMPTY_COLUMNS and not value.strip():
        raise ParseError(
            section=_SECTION,
            row_index=row_index,
            column=column,
            message=(f"required Account Information column {column!r} is empty in the data row."),
            raw_value="",
        )
    return value


def parse_account_info(rows: Iterable[dict[str, str]]) -> AccountInfo:
    """Parse the Account Information section into a single :class:`AccountInfo`.

    Args:
        rows: One dict per data row (output of
            :func:`ibkr_fa.parser.multi_section.split_sections`). For
            Account Information IBKR always emits exactly one row.

    Returns:
        :class:`AccountInfo` populated from the single row.

    Raises:
        ParseError: If the section is empty, contains more than one
            row, is missing a required column, has an empty value
            in a required-non-empty column, or has a non-blank ``CurrencyPrimary``
            that isn't 3 uppercase ASCII letters. No refusal carries a cell
            value, in its message or ``raw_value``, because a shifted cell
            can hold the holder's name or account number. The first
            offending condition fires; later conditions are not reported.
    """
    materialised = list(rows)
    if not materialised:
        raise ParseError(
            section=_SECTION,
            row_index=0,
            column=None,
            message=(
                "Account Information section has no data rows; expected "
                "exactly one row identifying the account."
            ),
            raw_value="",
        )
    if len(materialised) > 1:
        raise ParseError(
            section=_SECTION,
            row_index=1,
            column=None,
            message=(
                f"Account Information section has {len(materialised)} "
                f"data rows; v1 expects exactly one (multi-account "
                f"Flex Query exports are not supported)."
            ),
            raw_value="",
        )

    row = materialised[0]
    client_account_id = _require(row, "ClientAccountID", row_index=0)
    name = _require(row, "Name", row_index=0)
    _require(row, "AccountType", row_index=0)
    currency_primary = _require(row, "CurrencyPrimary", row_index=0)
    # Echo no value: a shifted cell can put the holder's name or account here.
    if _CURRENCY_PATTERN.fullmatch(currency_primary) is None:
        raise ParseError(
            section=_SECTION,
            row_index=0,
            column="CurrencyPrimary",
            message=(
                "Account Information column 'CurrencyPrimary' must be 3 uppercase "
                "ASCII letters (ISO 4217)."
            ),
            raw_value="",
        )
    _require(row, "IBEntity", row_index=0)

    return AccountInfo(
        account_id=AccountId(client_account_id),
        account_holder_name=name,
        base_currency=currency_primary,
    )
