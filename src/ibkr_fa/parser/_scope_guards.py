"""Tax-scope refusals shared by the IBKR parsers (#641).

The IBKR parsers refuse a currency other than USD, an Indian-issued
security, an issuer country missing from the CBDT country-code table, a
Flex issuer country that differs from the ISIN prefix, and a Flex lot
acquired before the bundled SBI TTBR archive starts. No engine check runs on
every IBKR lot, for every command, with an error that names the row,
so these refusals stay in the parsers. Each check below names the
engine gap that keeps it here.

Per-parser usage pattern, mirroring :mod:`ibkr_fa.parser._isin`:

.. code-block:: python

   require_foreign_mapped_country(
       issuer_country,
       section=_SECTION,
       row_index=row_index,
       column="IssuerCountryCode",
   )

No refusal here carries a cell value, in its message or ``raw_value``: a
column-shifted row can put the holder's name or account number in any
cell. The section, column and row locate the cell (#1027, #1038).
"""

from __future__ import annotations

import datetime as _dt
from typing import Final

from ibkr_fa.exceptions import ParseError
from ibkr_fa.reference.country_codes import load_country_codes

# Earliest date the bundled SBI TTBR USD archive covers. Parsers don't
# import ``ibkr_fa.fx``; tests/test_parser_scope_guards.py ties this to
# the archive instead.
_EARLIEST_TTBR_DATE: Final[_dt.date] = _dt.date(2020, 1, 6)

_MULTI_CURRENCY_TRACKER_URL: Final[str] = "https://github.com/jay-tau/ibkr-fa/issues/52"


def require_usd(currency: str, *, section: str, row_index: int, column: str) -> None:
    """Refuse a currency cell that isn't exactly ``USD``, without echoing it (#1027).

    The message and ``raw_value`` hold no cell text: a column-shifted row can
    put the holder's name or account number in this cell, and a short name
    such as ``ANN`` looks exactly like a currency code. The section, column
    and row locate the cell.

    Raises:
        ParseError: If ``currency`` isn't ``"USD"``.
    """
    # Engine gap: FA A3 values a lot as USD without checking, and the engine's checks name no row.
    if currency != "USD":
        raise ParseError(
            section=section,
            row_index=row_index,
            column=column,
            message=(
                f"the currency isn't USD; v1 supports USD only. Multi-currency "
                f"support is tracked at {_MULTI_CURRENCY_TRACKER_URL}."
            ),
            raw_value="",
        )


def require_foreign_mapped_country(
    iso2: str,
    *,
    section: str,
    row_index: int,
    column: str,
) -> None:
    """Refuse an Indian issuer country, or one the CBDT table lacks.

    Args:
        iso2: The normalised ISO 3166-1 alpha-2 issuer country.
        section: Section name for the :class:`ParseError`.
        row_index: Source row index for the :class:`ParseError`.
        column: Source column for the :class:`ParseError`.

    Raises:
        ParseError: If ``iso2`` is ``"IN"``, or has no row in the
            bundled ITR country-code table. The error chains nothing
            (#1226).
    """
    # Engine gap: CG only sets IN lots aside, and FA A3's India refusal (#1325) names no row.
    if iso2 == "IN":
        raise ParseError(
            section=section,
            row_index=row_index,
            column=column,
            message=(
                "the issuer country is 'IN' (India). ibkr-fa v1 "
                "handles foreign-issued securities only: Schedule FA "
                "reports FOREIGN assets for an Indian tax resident, and "
                "Indian-issued securities do not belong on it regardless "
                "of which broker holds them. If you reached this via a "
                "non-IBKR-US export, please file an issue at "
                "https://github.com/jay-tau/ibkr-fa/issues."
            ),
            raw_value="",
        )
    # Engine gap: only FA A3 and FSI look a lot's country up, and that error names no ISIN or row.
    # A membership check, not lookup_country: there is no caught error to chain.
    if iso2 not in load_country_codes():
        raise ParseError(
            section=section,
            row_index=row_index,
            column=column,
            message=(
                "the issuer country isn't in the bundled ITR country-code table. "
                "Add a row to src/ibkr_fa/reference/itr_country_codes.csv with the "
                "CBDT code from the ITR-2 utility's country dropdown."
            ),
            raw_value="",
        )


def require_isin_prefix_country(iso2: str, *, isin: str, section: str, row_index: int) -> None:
    """Refuse a Flex issuer country that differs from the ISIN prefix (#794).

    Activity Statement lots take their country from the ISIN prefix.
    Flex lots take ``IssuerCountryCode``, which IBKR does not document,
    so no recorded rule says which of the two Schedule FA Table A3
    reports when they differ.

    Args:
        iso2: The normalised ``IssuerCountryCode``.
        isin: The validated, uppercased ISIN.
        section: Section name for the :class:`ParseError`.
        row_index: Source row index for the :class:`ParseError`.

    Raises:
        ParseError: If ``iso2`` differs from the first two characters
            of ``isin``, naming the ``IssuerCountryCode`` column.
    """
    # Engine gap: a lot keeps one country, so FA A3 can't see the disagreement.
    if iso2 != isin[:2]:
        raise ParseError(
            section=section,
            row_index=row_index,
            column="IssuerCountryCode",
            message=(
                "the issuer country differs from the ISIN's country prefix. Which "
                "of the two Schedule FA Table A3 should report is unresolved, so "
                "ibkr-fa refuses the lot; see https://github.com/jay-tau/ibkr-fa/issues/794."
            ),
            raw_value="",
        )


def require_ttbr_covered_acquisition(open_date: _dt.date, *, section: str, row_index: int) -> None:
    """Refuse a Flex lot opened before the bundled TTBR archive starts.

    Schedule FA's initial value is converted at the TTBR rate on the
    ``OpenDateTime`` date, so the archive must cover it.

    Raises:
        ParseError: If ``open_date`` is before the archive start, naming
            the ``OpenDateTime`` column.
    """
    # Engine gap: only FA A3 needs this rate, and its MissingFxRateError names no lot.
    if open_date < _EARLIEST_TTBR_DATE:
        raise ParseError(
            section=section,
            row_index=row_index,
            column="OpenDateTime",
            message=(
                f"lot acquired before {_EARLIEST_TTBR_DATE.isoformat()} — "
                f"the bundled SBI TTBR archive does not cover this date. "
                f"Pre-2020 acquisitions are unsupported in v1; see "
                f"https://github.com/jay-tau/ibkr-fa/issues/52 for "
                f"FX-history extension."
            ),
            raw_value="",
        )
