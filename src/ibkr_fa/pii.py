"""PII-redaction helpers for user-visible surfaces.

IBKR account identifiers are masked for non-error report output.
Errors must omit account IDs entirely, masked or not, and must not
echo any other input values.

This module is intentionally a low-level dependency so every layer of
the stack — parser, compute, pipeline, output, CLI — can import the
masking helpers without creating cycles back into
:mod:`ibkr_fa.pipeline`.

The report masking contract for :func:`display_account_id`:

* Already-masked IDs (matching ``_MASKED_ACCOUNT_PATTERN``, e.g.
  ``"U**********"`` — IBKR's default Activity Statement export shape
  when "Show full account number" is disabled) are returned unchanged
  so display does not double-mask the placeholder.
* A value shaped like an account number (up to two ASCII letters,
  then at least five ASCII digits) is rendered as
  ``f"{prefix}***{last_four}"`` — the leading prefix letter(s) stay
  (``U``, ``F``, ``I``, ``M``, ``DU``, ``DF``, …), the trailing four
  chars stay (so the account owner can disambiguate during
  debugging), and the middle is collapsed to a fixed three-asterisk
  block (not a 1-for-1 substitution — the asterisk count does NOT
  leak the original length).
* Everything else renders as ``<not an account number>``, so a
  malformed account cell can't put part of a name into a report
  (#1047). Errors must never print an account ID, masked or not.

Raw account IDs remain in structured internal fields
(``AccountInfo.account_id``, ``RunReport.account_id``,
``ScheduleOsRunReport.account_id``) for cross-checking against
``user.toml`` and for programmatic consumers; only the display path
crosses this safety boundary.
"""

from __future__ import annotations

import re
from typing import Final

# IBKR's Activity Statement masks the account number to ``U`` followed
# by one or more asterisks unless the user enabled "Show full account
# number" at export time. The pattern is unambiguous because real
# IBKR account IDs are alphanumeric with no asterisks.
_MASKED_ACCOUNT_PATTERN: Final[re.Pattern[str]] = re.compile(r"^U\*+$")

# Suffix length retained verbatim in masked output. The masking
# format is ``f"{prefix}***{account_id[-_DISPLAY_ACCOUNT_ID_SUFFIX_LEN:]}"``
# (e.g. ``U***5678``); the trailing 4 chars are kept so the account
# owner can disambiguate which account a given report line refers
# to without leaking the full identifier.
_DISPLAY_ACCOUNT_ID_SUFFIX_LEN: Final[int] = 4

# The account-number shape :func:`display_account_id` masks: up to two
# ASCII letters, then at least five ASCII digits. Any other value renders
# as ``<not an account number>``.
_DISPLAYABLE_ACCOUNT_ID_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"(?P<prefix>[A-Za-z]{0,2})[0-9]{5,}", re.ASCII
)

#: What :func:`display_account_id` shows for a value not shaped like an
#: account number.
_NOT_AN_ACCOUNT_NUMBER: Final[str] = "<not an account number>"


def is_masked_account_id(account_id: str) -> bool:
    """Return ``True`` iff *account_id* matches IBKR's masked shape.

    IBKR masks the account number to ``U`` plus a run of asterisks
    on Activity Statement exports unless the user enabled "Show
    full account number" at export time. A real IBKR account ID is
    alphanumeric with no asterisks, so the pattern is unambiguous. The
    whole value must match: ``$`` alone also matches before a final
    line break (#854).
    """
    return _MASKED_ACCOUNT_PATTERN.fullmatch(account_id) is not None


def display_account_id(account_id: str) -> str:
    """Return a display-safe rendering of an account ID for report output.

    Keeps the leading prefix letter(s) and the last 4 characters;
    masks the middle with three asterisks (e.g. ``U***5678``,
    ``DU***4567``). Only a value shaped like an account number (up to
    two ASCII letters, then at least five ASCII digits) is masked;
    anything else renders as ``<not an account number>``, so a
    malformed account cell can't put part of a name into a report
    (#1047). This is the rendering for non-error report output (CLI
    report headers and run-report fields) so that screenshots,
    pasted-into-issues snippets, and shared session recordings do not
    leak the full account identifier — while keeping enough prefix +
    suffix for the account owner to disambiguate. Errors must never print
    an account ID, masked or not.

    Raw account IDs remain in structured internal fields
    (``AccountInfo.account_id``, ``RunReport.account_id``,
    ``ScheduleOsRunReport.account_id``) for cross-checking against
    ``user.toml`` and for programmatic consumers; only the display
    path crosses this safety boundary.

    Already-masked IDs (matching ``_MASKED_ACCOUNT_PATTERN``, e.g.
    ``"U**********"``) are returned unchanged so display does not
    double-mask the placeholder.

    Examples:
        >>> display_account_id("U12345678")
        'U***5678'
        >>> display_account_id("U1234567")
        'U***4567'
        >>> display_account_id("F1234567")
        'F***4567'
        >>> display_account_id("DU1234567")
        'DU***4567'
        >>> display_account_id("DF12345")
        'DF***2345'
        >>> display_account_id("123456789")
        '***6789'
        >>> display_account_id("U**********")
        'U**********'
        >>> display_account_id("ANN")
        '<not an account number>'
        >>> display_account_id("U12")
        '<not an account number>'
    """
    if is_masked_account_id(account_id):
        return account_id
    shape = _DISPLAYABLE_ACCOUNT_ID_PATTERN.fullmatch(account_id)
    if shape is None:
        return _NOT_AN_ACCOUNT_NUMBER
    prefix = shape.group("prefix")
    return f"{prefix}***{account_id[-_DISPLAY_ACCOUNT_ID_SUFFIX_LEN:]}"
