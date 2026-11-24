"""Load the vendored CBDT country-code CSV into typed records.

Companion to the on-disk ``src/ibkr_fa/reference/itr_country_codes.csv``
(see the ``.source`` file there for provenance).

The CSV is keyed by ISO 3166-1 alpha-2 because that's what IBKR emits
in its Flex Query ``IssuerCountryCode`` column. Each row carries:

* ``iso_alpha2`` — the lookup key.
* ``cbdt_code`` — the numeric code as shown in the portal Schedule FA
  Table A3 country dropdown (e.g., ``"2"`` for the United States).
* ``country_name`` — title-case name suitable for the audit columns
  of the review CSV.
* ``country_name_portal`` — verbatim UPPERCASE name from the portal
  dropdown. The portal-facing country cell is built as
  ``f"{cbdt_code}-{country_name_portal}"``.

A missing ISO alpha-2 lookup raises :class:`CountryCodeNotFoundError`,
not :data:`None` — silently dropping a country from FA-A3 would file
a wrong return.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from functools import cache
from importlib import resources
from typing import Final

from ibkr_fa.exceptions import IbkrFaError
from ibkr_fa.invariants import require
from ibkr_fa.reference._strict_csv import read_rows, require_plain_cells

_REFERENCE_PACKAGE: Final[str] = "ibkr_fa.reference"
_COUNTRY_CSV: Final[str] = "itr_country_codes.csv"
_EXPECTED_HEADER: Final[tuple[str, ...]] = (
    "iso_alpha2",
    "cbdt_code",
    "country_name",
    "country_name_portal",
)

# ISO 3166-1 alpha-2 codes are exactly 2 uppercase ASCII letters.
# Both patterns are used with ``fullmatch``.
_ISO_ALPHA2_PATTERN: Final[re.Pattern[str]] = re.compile(r"[A-Z]{2}")

# CBDT ITR-2 country codes are ASCII digit strings with no leading
# zero (e.g., ``"2"`` for the United States, ``"1441"`` for Bermuda).
_CBDT_CODE_PATTERN: Final[re.Pattern[str]] = re.compile(r"[1-9][0-9]*")


@dataclass(frozen=True, slots=True)
class CountryCodeEntry:
    """One row of the vendored CBDT country-code CSV.

    Attributes:
        iso_alpha2: ISO 3166-1 alpha-2 country code (e.g., ``"US"``).
        cbdt_code: Portal-side numeric code as a string. Stored as
            ``str`` (not ``int``) because the portal treats it as a
            display token, not arithmetic.
        country_name: Title-case display name.
        country_name_portal: Verbatim UPPERCASE name as the portal
            dropdown spells it (e.g.,
            ``"UNITED STATES OF AMERICA"``).
    """

    iso_alpha2: str
    cbdt_code: str
    country_name: str
    country_name_portal: str

    @property
    def portal_display(self) -> str:
        """Combined ``"{cbdt_code}-{country_name_portal}"`` string —
        the exact dropdown value the portal accepts."""
        return f"{self.cbdt_code}-{self.country_name_portal}"


class CountryCodeNotFoundError(IbkrFaError):
    """Raised when an ISO alpha-2 country code is not in the vendored
    table. The error points the user at the CSV they need to extend.
    It neither prints nor keeps the looked-up code: the code can come
    from a statement cell, such as an ISIN prefix (#1047, #1368).
    """

    def __init__(self) -> None:
        super().__init__(
            "no CBDT country code mapped for a looked-up ISO alpha-2 code; "
            f"add a row to src/ibkr_fa/reference/{_COUNTRY_CSV} after "
            f"verifying the code against the official ITR-2 utility "
            f"(see the .source file there for the upstream URL)."
        )


@cache
def load_country_codes() -> dict[str, CountryCodeEntry]:
    """Load all rows of ``itr_country_codes.csv`` keyed by ``iso_alpha2``.

    Cached: the file is small (249 rows for AY 2025-26, one per
    CBDT country) and never changes at runtime, so
    :func:`functools.cache` lets every call site share a single
    materialised dict.

    Returns:
        A ``dict[str, CountryCodeEntry]`` mapping each ISO alpha-2
        code in the CSV to its row.

    Raises:
        InvariantError: If the file isn't strict CSV (naming the
            line), if the header has drifted from the expected column
            tuple (someone changed the schema without updating this
            loader), or if any row breaks its shape: 4 printable cells
            with no leading or trailing whitespace, an ISO code, a
            CBDT code, non-empty names, a unique ISO key. An empty
            line is skipped; a row of empty cells refuses.
    """
    rows = read_rows(
        resources.files(_REFERENCE_PACKAGE).joinpath(_COUNTRY_CSV).read_text(encoding="utf-8"),
        csv_name=_COUNTRY_CSV,
        header=_EXPECTED_HEADER,
    )
    out: dict[str, CountryCodeEntry] = {}
    for row in rows:
        if not row:
            continue
        require_plain_cells(row, _EXPECTED_HEADER, csv_name=_COUNTRY_CSV)
        iso, code, name, portal = row
        # Load-time invariants (PR #125): every row must satisfy the
        # documented shape. Catches accidental edits to the CSV
        # (e.g., a stray lowercase letter, a non-numeric CBDT code,
        # an empty display name) BEFORE the bad value reaches the
        # FA-A3 output. Cached forever via functools.cache.
        require(
            _ISO_ALPHA2_PATTERN.fullmatch(iso) is not None,
            what=(
                f"{_COUNTRY_CSV}: iso_alpha2 must be exactly 2 uppercase "
                "ASCII letters (ISO 3166-1 alpha-2)"
            ),
            iso=iso,
        )
        require(
            _CBDT_CODE_PATTERN.fullmatch(code) is not None,
            what=(
                f"{_COUNTRY_CSV}: cbdt_code must be ASCII digits with no "
                "leading zero (CBDT ITR-2 format)"
            ),
            iso=iso,
            cbdt_code=code,
        )
        require(
            name != "",
            what=f"{_COUNTRY_CSV}: country_name must be non-empty",
            iso=iso,
        )
        require(
            portal != "",
            what=f"{_COUNTRY_CSV}: country_name_portal must be non-empty",
            iso=iso,
        )
        require(
            iso not in out,
            what=f"{_COUNTRY_CSV}: duplicate iso_alpha2 key",
            iso=iso,
        )
        out[iso] = CountryCodeEntry(
            iso_alpha2=iso,
            cbdt_code=code,
            country_name=name,
            country_name_portal=portal,
        )
    return out


def lookup_country(iso_alpha2: str) -> CountryCodeEntry:
    """Return the :class:`CountryCodeEntry` for ``iso_alpha2``.

    Args:
        iso_alpha2: ISO 3166-1 alpha-2 country code (e.g., ``"US"``).

    Returns:
        The matching :class:`CountryCodeEntry`.

    Raises:
        CountryCodeNotFoundError: If the code is not in the CSV.
    """
    entry = load_country_codes().get(iso_alpha2)
    if entry is None:
        # No KeyError: even suppressed, its args would stay on the new
        # error's __context__ and hold the code (#1047, #1188).
        raise CountryCodeNotFoundError()
    return entry
