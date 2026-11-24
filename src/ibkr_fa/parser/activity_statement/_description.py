"""Shared ``Description``-column parser for Activity Statement sections.

The IBKR Activity Statement's ``Dividends`` and ``Withholding Tax``
sections both embed the issuing instrument's ticker and ISIN in the
``Description`` column using the same structural prefix:

  ``"<SYMBOL>(<ISIN>) <free-form-suffix>"``

Examples (the format was checked against a private FY 25-26 statement;
``XMPL`` and ``XQCY-A`` are made-up securities)::

  XMPL(US99999A1025) Cash Dividend USD 0.25 per Share (Ordinary Dividend)
  XMPL(US99999A1025) Cash Dividend USD 0.25 per Share - US Tax
  XQCY-A(US99972D1072) Cash Dividend USD 0.50 per Share (Ordinary Dividend)

The two sections share an emit shape but carry independent semantics
(gross distribution vs withholding tax) — they parse to different
models (:class:`~ibkr_fa.models.Dividend` and
:class:`~ibkr_fa.models.WithholdingTax`). To avoid drift between two
copies of the same regex (PR #137 widened the symbol charset in
the Dividends parser when the hyphenated-ticker case surfaced;
lifting the regex here prevents future sister-parser
drift as new Activity Statement sections — e.g. ``Interest`` per
#139 — adopt the same shape), the regex and the
``Description``-extraction helper live here and are imported by
both parsers.

Per #138 (which introduced the Withholding Tax parser), this module
must be importable from any Activity Statement section parser
without creating a circular import — keep dependencies minimal:
just :mod:`ibkr_fa.exceptions` for :class:`ParseError` and
:mod:`ibkr_fa.types` for the :class:`Ticker`/:class:`Isin`
``NewType`` aliases.
"""

from __future__ import annotations

import re
from typing import Final

from ibkr_fa.exceptions import ParseError
from ibkr_fa.types import Isin, Ticker

# Match only the structural SYMBOL(ISIN) prefix + whitespace. Don't
# require any specific distribution-type keyword — the Dividends
# section emits Cash Dividend, Special Dividend, Choice Dividend,
# Payment In Lieu Of Dividend, Return Of Capital, Long-Term Capital
# Gain, etc., and the Withholding Tax section emits descriptions
# that mirror the originating dividend with a ``- US Tax`` (or
# similar treaty-rate) suffix. Schedule FA Table A3 column 10
# ("Total gross amount paid/credited") and the FTC chain
# (Schedule FSI/TR + Form 67) both accept all of these uniformly.
#
# Symbol charset includes hyphens and dots for share-class tickers
# like the made-up ``XQCY-A`` and ``XQDA-B`` (also seen as
# ``XQDA.B``). Uppercase-leading remains required to preserve the
# parser invariant locked by the Dividends section's
# ``test_description_lowercase_symbol_rejected`` (lowercase would
# silently mismatch trade-side uppercase tickers and falsely
# orphan-detect at FA-A3 assembly time). ISIN remains strict ISO
# 6166 (2-letter country + 9 alphanumerics + 1 check digit).
_DESCRIPTION_PREFIX: Final[str] = r"^([A-Z][A-Z0-9.\-]*)\s*\(([A-Z]{2}[A-Z0-9]{9}[0-9])\)\s+"

_DESCRIPTION_PATTERN: Final[re.Pattern[str]] = re.compile(_DESCRIPTION_PREFIX + r"\S")

_DESCRIPTION_PREFIX_PATTERN: Final[re.Pattern[str]] = re.compile(_DESCRIPTION_PREFIX)
"""Same structural prefix as :data:`_DESCRIPTION_PATTERN` but without
the trailing ``\\S`` lookalike that consumes the first character of
the type-slice. Used by
:func:`parse_symbol_isin_and_type_slice_from_description` to capture
the type-slice cleanly via ``raw[match.end():].strip()`` — the
:data:`_DESCRIPTION_PATTERN` variant requires a ``\\S`` end-of-match
to enforce non-empty payload, but that bleeds one char into the
type-slice which the consumer would have to strip.

Both patterns are compiled from :data:`_DESCRIPTION_PREFIX`, so this
one matches every input :data:`_DESCRIPTION_PATTERN` matches.
"""


def parse_symbol_isin_from_description(
    raw: str,
    *,
    section: str,
    row_index: int,
) -> tuple[Ticker, Isin]:
    """Extract ``(symbol, isin)`` from a Dividends/Withholding-Tax
    ``Description`` cell.

    Args:
        raw: The verbatim ``Description`` column value from one
            data row.
        section: The section name to embed in any :class:`ParseError`
            (e.g., ``"Dividends"`` or ``"Withholding Tax"``).
            Threaded explicitly rather than inferred so the helper
            can stay agnostic of caller sections — adding a third
            consumer later (e.g., the Interest section #139) won't
            need to touch this module.
        row_index: The 0-based row index within the section, used
            for the :class:`ParseError`'s ``row_index`` attribute.

    Returns:
        ``(symbol, isin)`` — both as their type-aliased
        :class:`Ticker` and :class:`Isin` ``NewType`` wrappers.

    Raises:
        ParseError: If ``raw`` is empty/whitespace-only, or if it
            doesn't begin with the expected
            ``SYMBOL(ISIN) <something>`` shape (e.g., a free-text
            row IBKR shouldn't have emitted in this section). The
            error carries no cell value, in its message or
            ``raw_value``: a shifted cell can hold the holder's name.
    """
    if not raw.strip():
        raise ParseError(
            section=section,
            row_index=row_index,
            column="Description",
            message="must be non-empty",
            raw_value="",
        )
    match = _DESCRIPTION_PATTERN.match(raw)
    if not match:
        raise ParseError(
            section=section,
            row_index=row_index,
            column="Description",
            message=(
                "description doesn't match the expected "
                "'SYMBOL(ISIN) ...' shape. The parser accepts any "
                "free-form suffix (e.g., 'Cash Dividend USD 0.25 "
                "per Share (Ordinary Dividend)', '... - US Tax') "
                "but the structural prefix is required. If you "
                "believe this is a valid IBKR description shape, "
                "please file an issue at "
                "https://github.com/jay-tau/ibkr-fa/issues with "
                "the redacted description text."
            ),
            raw_value="",
        )
    symbol_str, isin_str = match.groups()
    return Ticker(symbol_str), Isin(isin_str)


def parse_symbol_isin_and_type_slice_from_description(
    raw: str,
    *,
    section: str,
    row_index: int,
) -> tuple[Ticker, Isin, str]:
    """Sister to :func:`parse_symbol_isin_from_description` that ALSO
    returns the ``type_slice`` (everything after the structural prefix).

    It runs :func:`parse_symbol_isin_from_description` first, so the two
    functions accept the exact same inputs and raise the exact same
    :class:`ParseError`s; the only difference is the return shape.
    This helper exists so the Dividends parser can populate
    :attr:`~ibkr_fa.models.Dividend.type_slice` and classify
    :attr:`~ibkr_fa.models.Dividend.kind` via
    :func:`~ibkr_fa.parser.activity_statement.dividends.classify_distribution` at parse time
    (issue #153), without forcing the Withholding-Tax parser
    (which only needs ``(symbol, isin)``) to also adopt the
    type-slice extraction.

    Args:
        raw: The verbatim ``Description`` column value from one
            data row.
        section: The section name to embed in any :class:`ParseError`.
        row_index: The 0-based row index within the section.

    Returns:
        ``(symbol, isin, type_slice)`` — ``type_slice`` is the
        substring of ``raw`` after the structural ``SYMBOL(ISIN)\\s+``
        prefix, with leading/trailing whitespace stripped. Will be
        non-empty whenever the function returns (the prefix-shape
        regex requires at least one non-whitespace char after the
        trailing whitespace via :data:`_DESCRIPTION_PATTERN`).

    Raises:
        ParseError: Same conditions as
            :func:`parse_symbol_isin_from_description`.
    """
    symbol, isin = parse_symbol_isin_from_description(raw, section=section, row_index=row_index)
    # _DESCRIPTION_PREFIX_PATTERN is _DESCRIPTION_PATTERN without the
    # trailing \S that would bleed one char into the type_slice (both
    # are compiled from _DESCRIPTION_PREFIX). Match it here so the
    # prefix end position is clean — slicing at the end of a
    # _DESCRIPTION_PATTERN match would lose the first non-whitespace
    # character.
    prefix_match = _DESCRIPTION_PREFIX_PATTERN.match(raw)
    # Defensive: _DESCRIPTION_PREFIX_PATTERN accepts a strict
    # superset of _DESCRIPTION_PATTERN inputs (no \S lookahead), so
    # this branch is unreachable when _DESCRIPTION_PATTERN matched.
    assert prefix_match is not None, (
        f"_DESCRIPTION_PREFIX_PATTERN must match whenever _DESCRIPTION_PATTERN matches; raw={raw!r}"
    )
    type_slice = raw[prefix_match.end() :].strip()
    return symbol, isin, type_slice
