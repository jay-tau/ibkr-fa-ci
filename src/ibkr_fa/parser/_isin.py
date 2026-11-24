"""Shared ISO 6166 ISIN shape validation for parsers.

Issue #150: prior to this helper, three parser sites constructed
``Isin`` directly from raw IBKR cell values (after a non-empty
check), trusting the downstream ``OpenLot.__post_init__`` /
``ClosedLot.__post_init__`` regex check (or the issuer-country
prefix lookup) to catch malformed IDs. Both failure paths DO
trigger on bad input, but at the wrong granularity:

* ``OpenLot.__post_init__`` raises :class:`InvariantError`, losing
  the source-row context (no row index, no column name) — the kind
  of bug PR #146 Tier-5 caught for the ISIN casing case.
* The country-prefix lookup (``lookup_country()`` in the trades
  parser) only fires when the bad prefix happens to not match a
  known country. A 12-char ISIN with a valid prefix but malformed
  body would slip past the country check and only fail at the
  invariant.

This module centralises the ISO 6166 shape check so EVERY parser
construction site catches shape violations consistently AND with
row-scoped :class:`ParseError` context (section / row_index /
column), without the cell's value (#1038).

ISO 6166 ISIN shape (per the standard):

* Total length: 12 characters.
* Characters 1-2: ISO 3166-1 alpha-2 country prefix
  (e.g., ``"US"``, ``"CA"``, ``"IN"``). Uppercase A-Z.
* Characters 3-11: 9 alphanumerics — typically the National
  Securities Identifying Number (NSIN). Uppercase A-Z and digits.
* Character 12: single check digit (0-9) computed via the
  modified-Luhn algorithm.

The :data:`_ISIN_PATTERN` regex enforces the structural shape only;
the Luhn check digit is **NOT** validated (out of #150 scope).
Validating Luhn would require a constant-time integer-arithmetic
helper and is a separate hardening pass — IBKR-emitted ISINs are
not observed to fail Luhn, so deferring the check is the right
priority trade-off.

Per-parser usage pattern:

.. code-block:: python

   isin_validated = validate_isin_or_raise(
       row["ISIN"], section=_SECTION, row_index=row_index, column="ISIN"
   )
   ...
   isin = Isin(isin_validated)

The helper also normalises the returned string to uppercase before
the regex check, so a lowercase ``"us9999011011"`` is accepted and
emitted as ``"US9999011011"`` — consistent with PR #146 Tier-5 P2's
case-normalisation pattern. This pre-empts the silent mismatch
bug where a lowercased ISIN would fail to join against other
parsers' uppercased ISINs at downstream aggregation time.
"""

from __future__ import annotations

from ibkr_fa.exceptions import ParseError

# ISO 6166: 12 chars total. 2-letter country code + 9 alphanumerics
# + 1 single check digit (0-9). Uppercase-only after normalisation.
# The models check the same shared pattern
# (:mod:`ibkr_fa.models._identifier_checks`). The parser-side helper
# here is the FIRST-line defence (caught at parse time with row
# context); the model-layer checks remain as second-line invariants
# for any caller that constructs a model directly. The companion
# parser ``parse_symbol_isin_from_description`` in
# :mod:`ibkr_fa.parser.activity_statement._description` enforces the
# same shape inside its description-cell regex (no separate helper
# call needed there).
from ibkr_fa.types import ISIN_PATTERN as _ISIN_PATTERN


def validate_isin_or_raise(
    raw: str,
    *,
    section: str,
    row_index: int,
    column: str,
) -> str:
    """Return the uppercased ISIN string, or raise :class:`ParseError`.

    Validates the ISO 6166 structural shape (12 chars: 2 uppercase
    letters + 9 alphanumerics + 1 digit). The Luhn check digit is
    NOT validated — see module docstring for rationale.

    Args:
        raw: The raw cell value from the IBKR CSV. Stripped of
            surrounding whitespace before validation. Case-
            normalised to uppercase before regex matching, so a
            lowercase ``"us9999011011"`` is accepted and returned
            as ``"US9999011011"``.
        section: Section name to embed in the :class:`ParseError`
            (e.g., ``"Financial Instrument Information"``,
            ``"Open Positions"``, ``"Trades"``).
        row_index: Source row index for the :class:`ParseError`.
        column: Source column name to embed in the
            :class:`ParseError` (e.g., ``"Security ID"``,
            ``"ISIN"``).

    Returns:
        The uppercased, validated ISIN string. Callers wrap this
        with ``Isin(...)`` (the :class:`~ibkr_fa.types.Isin`
        ``NewType`` has no runtime constructor logic, so the
        wrapping is purely a typing-time concern).

    Raises:
        ParseError: If ``raw`` is empty / whitespace-only, or if
            its uppercased form doesn't match the ISO 6166 shape.
            The error names the section, column and row, and carries no
            cell value, in its message or ``raw_value``: a shifted cell
            can hold the holder's name or account number.
    """
    stripped = raw.strip()
    if not stripped:
        raise ParseError(
            section=section,
            row_index=row_index,
            column=column,
            message=(
                f"ISIN column {column!r} is empty; expected a 12-character "
                f"ISO 6166 ISIN (2 uppercase letters + 9 alphanumerics + "
                f"1 digit, e.g., 'US9999011011')."
            ),
            raw_value="",
        )
    candidate = stripped.upper()
    if not _ISIN_PATTERN.match(candidate):
        raise ParseError(
            section=section,
            row_index=row_index,
            column=column,
            message=(
                "the ISIN doesn't match ISO 6166 shape (12 chars: "
                "2 uppercase letters + 9 alphanumerics + 1 digit). "
                "IBKR may have changed the schema, or the cell is "
                "corrupted. See https://github.com/jay-tau/ibkr-fa/"
                "issues/150 for the validation contract."
            ),
            raw_value="",
        )
    return candidate
