"""Parse the Activity Statement Account Information section.

Activity Statement's Account Information has a very different
shape from Flex Query's: it's **row-per-field** (each data row is a
``{"Field Name": ..., "Field Value": ...}`` pair) rather than one
data row with N columns. So this parser first transposes the rows
into a ``{field_name: field_value}`` dict, then maps to the same
:class:`AccountInfo` dataclass the Flex Query parser produces.

Field-name mapping (Activity Statement → :class:`AccountInfo`):

==================== ==========================
Activity Statement   ``AccountInfo`` field
==================== ==========================
``Name``             ``account_holder_name``
``Account``          ``account_id``
``Base Currency``    ``base_currency``
==================== ==========================

``Account Type`` is required and must be non-blank, but it isn't kept.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Final

from ibkr_fa.exceptions import ParseError
from ibkr_fa.models.account_info import _CURRENCY_PATTERN, AccountInfo
from ibkr_fa.types import AccountId

_SECTION: Final[str] = "Account Information"

# IBKR's Activity Statement Account Information field name for the
# account number. Matched case-sensitively against ``Field Name``.
# Account Information errors echo no cell: its values and a shifted Field Name can identify the
# holder.
_ACCOUNT_FIELD_NAME: Final[str] = "Account"

# Required field names AS IBKR SPELLS THEM in the Activity Statement
# (deliberately not a frozenset so the rendered error message keeps
# the IBKR ordering and is easy to find in a real file).
_REQUIRED_FIELDS: Final[tuple[str, ...]] = (
    "Name",
    _ACCOUNT_FIELD_NAME,
    "Account Type",
    "Base Currency",
)


def _transpose_rows(
    rows: Iterable[dict[str, str]],
) -> dict[str, str]:
    """Collapse row-per-field input into a single ``{name: value}`` dict.

    Raises:
        ParseError: If any row is missing the ``Field Name`` or
            ``Field Value`` column (the section's column tuple
            should be ``("Field Name", "Field Value")`` by
            construction, but defensive parsing here turns a
            future schema change into a clean error rather than a
            ``KeyError``); or if a ``Field Name`` appears more
            than once across rows. The duplicate-field check
            defends against malformed exports and multi-account
            statements where identity rows repeat — silent
            last-row-wins would yield an :class:`AccountInfo`
            that doesn't represent the whole input. That error
            names the row and the ``Field Name`` column but echoes
            no cell from either row, field name included, because
            Account Information values (the holder's name, the
            account number) can identify the holder, and a shifted
            row can put one in ``Field Name``.
    """
    out: dict[str, str] = {}
    for row_index, row in enumerate(rows):
        try:
            name = row["Field Name"]
            value = row["Field Value"]
        except KeyError as exc:
            raise ParseError(
                section=_SECTION,
                row_index=row_index,
                column=str(exc.args[0]),
                message=(
                    f"Activity Statement Account Information row is "
                    f"missing the {exc.args[0]!r} column; expected the "
                    f"section's columns to be ('Field Name', "
                    f"'Field Value'). IBKR may have changed the format."
                ),
                raw_value="",
            ) from exc
        if name in out:
            # Echo no cell, the field name included: a shifted row can put
            # the holder's name or account number in any cell.
            raise ParseError(
                section=_SECTION,
                row_index=row_index,
                column="Field Name",
                message=(
                    "Activity Statement Account Information row repeats an "
                    "earlier row's Field Name; refusing to silently overwrite "
                    "the earlier value. This usually means the export contains "
                    "multiple accounts (out of v1 scope) or is malformed."
                ),
                raw_value="",
            )
        out[name] = value
    return out


def parse_activity_statement_account_info(
    rows: Iterable[dict[str, str]],
    *,
    fallback_account_holder_name: str | None = None,
    fallback_account_id: str | None = None,
) -> tuple[AccountInfo, frozenset[str]]:
    """Parse the Activity Statement Account Information section into :class:`AccountInfo`.

    Args:
        rows: One dict per ``Data`` row from the
            :class:`KnownActivitySection.ACCOUNT_INFORMATION`
            block returned by
            :func:`split_activity_statement_sections`. Each row's
            keys are ``("Field Name", "Field Value")``.
        fallback_account_holder_name: Optional fallback value to
            use when the ``Name`` row is absent from the statement.
            When supplied, the parser does NOT raise on missing
            ``Name`` — it substitutes this value and records
            ``"Name"`` in the returned fallback-used set. IBKR's
            Activity Statement export configurator allows hiding
            the Name + Account fields for privacy (seen in a real
            private statement); pass the user's
            ``[account].holder_name`` from ``user.toml`` to accept
            such anonymised exports under the
            ``--accept-anonymised-statement`` CLI flag. See #141.
            Ignored when ``Name`` IS present in the statement (the
            statement-provided value always wins; the fallback is
            an absence-only safety net).
        fallback_account_id: Optional fallback value to use when
            the ``Account`` row is absent from the statement. Same
            semantics as ``fallback_account_holder_name`` —
            substitutes on absence, recorded in the returned
            fallback-used set as ``"Account"``, ignored when the
            ``Account`` row IS present (no interaction with the
            ``--allow-masked-account`` flag, which handles the
            orthogonal case of an Account row whose value is a
            ``"U**********"`` mask string).

    Returns:
        Tuple of:

        * :class:`AccountInfo` populated from the transposed rows
          (with absent fields backfilled from fallback when
          provided).
        * :class:`frozenset[str]` of IBKR field names supplied
          from fallback. Empty when no fallback was used, either
          because no required field was absent or because no
          fallback was provided. The pipeline forwards this set
          to :class:`RunReport` so the CLI can surface a loud
          "identity supplied from user.toml, not from statement"
          warning. Possible members: ``"Name"``, ``"Account"``.

    Raises:
        ParseError: If a required field is missing AND no fallback
            is available for that specific field (or if a row
            lacks the ``Field Name`` / ``Field Value`` columns,
            or if Account Type / Base Currency is missing —
            fallback only covers Name + Account per #141), or if
            a non-blank Base Currency isn't 3 uppercase ASCII letters (that
            error carries no value, because a shifted cell can hold
            the holder's name or account number). The first
            offending condition fires.
    """
    materialised = list(rows)
    if not materialised:
        raise ParseError(
            section=_SECTION,
            row_index=0,
            column=None,
            message=(
                "Activity Statement Account Information section has no "
                "data rows; expected one row per identity field "
                "(Name, Account, Account Type, Base Currency, ...)."
            ),
            raw_value="",
        )

    fields = _transpose_rows(materialised)

    # #141: backfill Name + Account from fallback when absent. The
    # statement-provided value always wins when present; fallback is
    # an absence-only safety net for anonymised exports (IBKR allows
    # hiding these two fields at export time). Account Type + Base
    # Currency are always emitted by IBKR (no hide-at-export option)
    # so they are NOT in the fallback contract.
    fallback_used: set[str] = set()
    if "Name" not in fields and fallback_account_holder_name is not None:
        fields["Name"] = fallback_account_holder_name
        fallback_used.add("Name")
    if _ACCOUNT_FIELD_NAME not in fields and fallback_account_id is not None:
        fields[_ACCOUNT_FIELD_NAME] = fallback_account_id
        fallback_used.add(_ACCOUNT_FIELD_NAME)

    missing = [name for name in _REQUIRED_FIELDS if name not in fields]
    if missing:
        raise ParseError(
            section=_SECTION,
            row_index=0,
            column=missing[0],
            message=(
                f"Activity Statement Account Information section is "
                f"missing required field(s) {missing!r}. Re-export the "
                f"statement with all account-identity fields enabled, "
                f"or pass --accept-anonymised-statement (with "
                f"[account].holder_name + [account].number set in "
                f"user.toml) to accept hidden Name / Account fields."
            ),
            raw_value="",
        )

    for name in _REQUIRED_FIELDS:
        if not fields[name].strip():
            raise ParseError(
                section=_SECTION,
                row_index=0,
                column=name,
                message=(
                    f"Activity Statement Account Information field {name!r} has an empty value."
                ),
                raw_value="",
            )

    # Echo no value: a shifted cell can put the holder's name or account here.
    if _CURRENCY_PATTERN.fullmatch(fields["Base Currency"]) is None:
        raise ParseError(
            section=_SECTION,
            row_index=0,
            column="Base Currency",
            message=(
                "Activity Statement Account Information field 'Base Currency' "
                "must be 3 uppercase ASCII letters (ISO 4217)."
            ),
            raw_value="",
        )

    return (
        AccountInfo(
            account_id=AccountId(fields["Account"]),
            account_holder_name=fields["Name"],
            base_currency=fields["Base Currency"],
        ),
        frozenset(fallback_used),
    )
