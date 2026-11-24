"""Load the vendored GLEIF ISO 20275 entity-legal-form labels.

The English label of an active code becomes a Robinhood holding's A3
"Nature of entity", attributed to GLEIF as a verbatim copy, so the
vendored CSV's content is pinned by its SHA-256 (#946). The loader
hashes the file's bytes before it parses anything and refuses any
other content. A re-vendor updates ``_CSV_SHA256`` and
``_PINNED_ROW_COUNT`` together with ``csv_sha256`` and ``rows`` in
``gleif_elf_codes.csv.source``; a test keeps them in step.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from functools import cache
from importlib import resources
from typing import Final

from ibkr_fa.invariants import require
from ibkr_fa.reference._strict_csv import read_rows, require_plain_cells

_REFERENCE_PACKAGE: Final[str] = "ibkr_fa.reference"
_CSV_NAME: Final[str] = "gleif_elf_codes.csv"
_HEADER: Final[tuple[str, ...]] = (
    "code",
    "country_iso2",
    "subdivision_code",
    "status",
    "english_name",
)
_CODE_PATTERN: Final[re.Pattern[str]] = re.compile(r"\A[A-Z0-9]{4}\Z")
_COUNTRY_PATTERN: Final[re.Pattern[str]] = re.compile(r"\A[A-Z]{2}\Z")
_SUBDIVISION_PATTERN: Final[re.Pattern[str]] = re.compile(r"\A[A-Z]{2}-[A-Z0-9]{1,3}\Z")
# The only statuses in the vendored file; a re-vendor with another refuses.
_STATUSES: Final[frozenset[str]] = frozenset({"ACTV", "INAC"})
_CSV_SHA256: Final[str] = "5360731c38c6b3038f2a296f2a514cb11a1c1add03a3b9c44a0607174a193f62"
_PINNED_ROW_COUNT: Final[int] = 3599


@dataclass(frozen=True, slots=True)
class GleifElfEntry:
    """One ISO 20275 legal-form code and its unique English label, if any."""

    code: str
    country_iso2: str
    subdivision_code: str
    status: str
    english_name: str


@cache
def load_gleif_elf_codes() -> dict[str, GleifElfEntry]:
    """Load all rows of ``gleif_elf_codes.csv`` keyed by ``code``.

    Raises:
        InvariantError: If the file's SHA-256 isn't the pinned one
            (checked before parsing, so the message holds no row
            text), if the file isn't strict CSV (naming the
            line), if the header isn't the expected one, or if any row
            breaks its shape: 5 printable cells with no leading or
            trailing whitespace (an empty line refuses too), a code, an
            empty or ISO country, an empty or ISO 3166-2 subdivision,
            ``ACTV`` or ``INAC``, a unique code; or if the row count
            isn't the pinned one. The English label may be empty.
    """
    content = resources.files(_REFERENCE_PACKAGE).joinpath(_CSV_NAME).read_bytes()
    require(
        hashlib.sha256(content).hexdigest() == _CSV_SHA256,
        what=f"{_CSV_NAME} content doesn't match the pinned SHA-256",
    )
    result: dict[str, GleifElfEntry] = {}
    for row in read_rows(content.decode("utf-8"), csv_name=_CSV_NAME, header=_HEADER):
        require_plain_cells(row, _HEADER, csv_name=_CSV_NAME)
        code, country, subdivision, status, english_name = row
        require(
            _CODE_PATTERN.fullmatch(code) is not None,
            what=f"{_CSV_NAME} code must be four uppercase ASCII alphanumerics",
            code=code,
        )
        require(
            not country or _COUNTRY_PATTERN.fullmatch(country) is not None,
            what=f"{_CSV_NAME} country_iso2 must be empty or ISO alpha-2",
            code=code,
        )
        require(
            not subdivision or _SUBDIVISION_PATTERN.fullmatch(subdivision) is not None,
            what=f"{_CSV_NAME} subdivision_code must be empty or ISO 3166-2 (e.g. US-OR)",
            code=code,
        )
        require(
            status in _STATUSES,
            what=f"{_CSV_NAME} status must be ACTV or INAC",
            code=code,
            status=status,
        )
        require(code not in result, what=f"{_CSV_NAME} code must be unique", code=code)
        result[code] = GleifElfEntry(code, country, subdivision, status, english_name)
    require(
        len(result) == _PINNED_ROW_COUNT,
        what=f"{_CSV_NAME} must contain the pinned GLEIF row count",
    )
    return result


def lookup_gleif_elf(code: str) -> GleifElfEntry | None:
    """Return one legal-form entry or ``None`` for reserved/unknown codes."""

    return None if code in {"8888", "9999"} else load_gleif_elf_codes().get(code)
