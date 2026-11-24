"""Load the vendored India DTAA dividend reference CSV into typed records.

Companion to the on-disk
``src/ibkr_fa/reference/dtaa_dividend_rates.csv`` (see the
``.source`` file there for provenance, schema, and the tri-state
lookup contract). Powers the Form 67 relief computation in
:mod:`ibkr_fa.compute.form67`: the loader's tri-state output
(``DTAA`` / ``NO_DTAA`` / unknown) gates whether a Form 67 entry
goes down the §90 pathway (cols 9 and 10 from this entry; col 11
also needs col 7 and a proven source rate), the §91 pathway (col
12, which also needs col 7 and ``[form67.section_91]``), or stays
UNKNOWN with cols 9-13 left ``None`` for manual fill.

The CSV is keyed by ISO 3166-1 alpha-2 (the same key as
:mod:`ibkr_fa.reference.country_codes`) because that's what the
upstream WHT / OS streams carry by the time they reach the
Form 67 compute layer.

The loader parses the CSV strictly, checks the exact header, and
checks every cell as a whole value against its column's grammar. Any
breach raises ``InvariantError``. The "vendored no_dtaa" rows
must have all DTAA-specific fields empty, and "vendored dtaa" rows
must have all of them present — silent shape drift would corrupt
the tri-state contract that the relief computation depends on.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum
from functools import cache
from importlib import resources
from typing import Final

from ibkr_fa.invariants import require
from ibkr_fa.reference._strict_csv import read_rows, require_plain_cells

_REFERENCE_PACKAGE: Final[str] = "ibkr_fa.reference"
_DTAA_CSV: Final[str] = "dtaa_dividend_rates.csv"
_EXPECTED_HEADER: Final[tuple[str, ...]] = (
    "iso_alpha2",
    "country_name",
    "applicability",
    "withholding_article",
    "relief_article",
    "portfolio_dividend_rate_percent",
    "applicability_notes",
    "source_citation",
)

# ISO 3166-1 alpha-2 codes are exactly 2 uppercase ASCII letters
# (matches the convention from country_codes.py). Both patterns are
# used with ``fullmatch``.
_ISO_ALPHA2_PATTERN: Final[re.Pattern[str]] = re.compile(r"[A-Z]{2}")
# ASCII digits with an optional fraction: no sign, space, ``_``,
# exponent, ``NaN`` or non-ASCII digit, all of which ``Decimal`` takes.
_RATE_PATTERN: Final[re.Pattern[str]] = re.compile(r"[0-9]+(\.[0-9]+)?")

# Decimal rate sanity bound: any treaty cap above 100% would be a
# vendoring error (the highest commonly observed dividend cap in
# India's DTAA network is around 25%; even hypothetical caps don't
# exceed 100% of the gross amount).
_MAX_DTAA_RATE_PERCENT: Final[Decimal] = Decimal("100")


class DtaaApplicability(StrEnum):
    """Tri-state DTAA applicability for an Indian-resident taxpayer.

    The third state — UNKNOWN — is NOT modelled here as an enum
    value; it is represented by the loader returning ``None`` for
    an ISO code that is not in the vendored CSV. This split keeps
    the typed entry shape narrow: every materialised
    :class:`DtaaDividendEntry` is one of the two well-known
    pathways, while unvendored countries propagate ``None`` through
    the call chain and force the relief computation to leave
    Form 67 cols 9-13 empty.

    Values:
        DTAA: Country has a comprehensive DTAA with India under
            §90 or §90A. The Form 67 §90 / §90A pathway applies;
            cols 9 (article) + 10 (rate) populate from this entry.
        NO_DTAA: Country is in the vendored CSV but has NO
            comprehensive DTAA with India. The §91 unilateral
            relief pathway is the only available route (subject
            to the doubly-taxed-income precondition of §91 being
            met — see entry notes). Form 67 cols 9 + 10 stay
            empty per the model's mutex on §91 credit ↔ DTAA
            fields.
    """

    DTAA = "dtaa"
    NO_DTAA = "no_dtaa"


@dataclass(frozen=True, slots=True)
class DtaaDividendEntry:
    """One row of the vendored India DTAA dividend reference CSV.

    For DTAA-applicability rows: ``withholding_article``,
    ``relief_article``, and ``portfolio_dividend_rate_percent``
    are ALL populated. For no_dtaa-applicability rows: all three
    are ``None``. The loader enforces this shape at construction.

    Attributes:
        iso_alpha2: ISO 3166-1 alpha-2 country code (e.g., ``"US"``).
        country_name: Title-case display name (matches
            :class:`~ibkr_fa.reference.country_codes.CountryCodeEntry.country_name`
            convention).
        applicability: Tri-state pathway selector. See
            :class:`DtaaApplicability`.
        withholding_article: The DTAA article that caps source-state
            withholding on dividends (e.g., ``"Article 10(2)(b)"``
            of the India-US DTAA). ``None`` for ``NO_DTAA`` rows.
        relief_article: The DTAA article granting India the right
            to allow the foreign tax as a credit (e.g.,
            ``"Article 25(2)(a)"`` of the India-US DTAA). ``None``
            for ``NO_DTAA`` rows. Schedule FSI renders this relief
            article; Form 67 col 9 renders ``withholding_article``
            beside the treaty-rate column.
        portfolio_dividend_rate_percent: Treaty cap (percent) for
            the typical IBKR-investor case — portfolio Indian
            individuals not exceeding the "substantial holding"
            threshold of the relevant article. For India-US this
            is 25 (Article 10(2)(b), the non-substantial cap),
            NOT 15 (the substantial-corporate-holder cap of
            Article 10(2)(a)). Stored as :class:`Decimal` for
            audit-grade precision. ``None`` for ``NO_DTAA`` rows.
        applicability_notes: Plain-text caveats: dual-rate sub-
            clauses, REIT / RIC carve-outs, TIEA-only relationships,
            administrative-rate divergences, etc. Surfaced into
            downstream review CSV output for auditor reconciliation.
        source_citation: Per-row provenance: treaty notification
            number, IRS / CBDT treaty-text URL, or (for ``NO_DTAA``)
            the CBDT DTAA-list URL with note that the country is
            absent.
    """

    iso_alpha2: str
    country_name: str
    applicability: DtaaApplicability
    withholding_article: str | None
    relief_article: str | None
    portfolio_dividend_rate_percent: Decimal | None
    applicability_notes: str
    source_citation: str


@cache
def load_dtaa_dividend_rates() -> dict[str, DtaaDividendEntry]:
    """Load all rows of ``dtaa_dividend_rates.csv`` keyed by ``iso_alpha2``.

    Cached: the file is small (a few dozen rows in v1, capped at
    ~95 if eventually expanded to India's full DTAA network) and
    never changes at runtime, so :func:`functools.cache` lets every
    call site share a single materialised dict.

    Returns:
        A ``dict[str, DtaaDividendEntry]`` mapping each vendored
        ISO alpha-2 code to its row.

    Raises:
        InvariantError: If the file isn't strict CSV (naming the
            line), if the header has drifted from the expected
            8-column tuple (someone changed the schema without
            updating this loader), or if any row breaks its shape:
            8 printable cells with no leading or trailing
            whitespace, an ISO code, an applicability enum member,
            DTAA fields populated iff the row is ``dtaa``, a rate of
            plain digits within bounds, non-empty name / notes /
            citation. An empty line is skipped; a row of empty cells
            refuses.
    """
    rows = read_rows(
        resources.files(_REFERENCE_PACKAGE).joinpath(_DTAA_CSV).read_text(encoding="utf-8"),
        csv_name=_DTAA_CSV,
        header=_EXPECTED_HEADER,
    )
    applicability_values = [m.value for m in DtaaApplicability]
    out: dict[str, DtaaDividendEntry] = {}
    for row in rows:
        if not row:
            continue
        require_plain_cells(row, _EXPECTED_HEADER, csv_name=_DTAA_CSV)
        (
            iso,
            country_name,
            applicability_raw,
            withholding_article,
            relief_article,
            rate_raw,
            notes,
            citation,
        ) = row
        require(
            _ISO_ALPHA2_PATTERN.fullmatch(iso) is not None,
            what=(
                f"{_DTAA_CSV}: iso_alpha2 must be exactly 2 "
                "uppercase ASCII letters (ISO 3166-1 alpha-2)"
            ),
            iso=iso,
        )
        require(
            country_name != "",
            what=f"{_DTAA_CSV}: country_name must be non-empty",
            iso=iso,
        )
        require(
            applicability_raw in applicability_values,
            what=f"{_DTAA_CSV}: applicability must be one of {applicability_values!r}",
            iso=iso,
            applicability=applicability_raw,
        )
        applicability = DtaaApplicability(applicability_raw)
        require(
            notes != "",
            what=(
                f"{_DTAA_CSV}: applicability_notes must be non-empty "
                "(every row should document its provenance / caveats)"
            ),
            iso=iso,
        )
        require(
            citation != "",
            what=(
                f"{_DTAA_CSV}: source_citation must be non-empty "
                "(every row must cite its primary source)"
            ),
            iso=iso,
        )
        if applicability is DtaaApplicability.DTAA:
            require(
                withholding_article != "",
                what=(
                    f"{_DTAA_CSV}: withholding_article must be "
                    "populated for a DTAA row (col 9 of Form 67 "
                    "expects an article reference for §90 / §90A "
                    "relief)"
                ),
                iso=iso,
            )
            require(
                relief_article != "",
                what=(
                    f"{_DTAA_CSV}: relief_article must be "
                    "populated for a DTAA row (Schedule FSI asks "
                    "for the article under which relief is claimed)"
                ),
                iso=iso,
            )
            require(
                _RATE_PATTERN.fullmatch(rate_raw) is not None,
                what=(
                    f"{_DTAA_CSV}: portfolio_dividend_rate_percent "
                    "must be plain ASCII digits with an optional "
                    "fraction for a DTAA row (col 10 of Form 67 "
                    "expects the treaty cap rate for §90 / §90A "
                    "relief)"
                ),
                iso=iso,
                rate_raw=rate_raw,
            )
            rate = Decimal(rate_raw)
            require(
                rate <= _MAX_DTAA_RATE_PERCENT,
                what=(
                    f"{_DTAA_CSV}: portfolio_dividend_rate_percent "
                    f"must be <= {_MAX_DTAA_RATE_PERCENT} (any DTAA "
                    "treaty cap above 100% would be a vendoring "
                    "error)"
                ),
                iso=iso,
                rate=rate,
            )
            parsed_withholding_article: str | None = withholding_article
            parsed_relief_article: str | None = relief_article
            parsed_rate: Decimal | None = rate
        else:
            require(
                withholding_article == "",
                what=(
                    f"{_DTAA_CSV}: withholding_article must be EMPTY "
                    "for a no_dtaa row (the §91 unilateral relief "
                    "pathway has no DTAA article to cite; the model "
                    "invariant on Form67Entry rejects §91 credits "
                    "paired with DTAA fields)"
                ),
                iso=iso,
                withholding_article=withholding_article,
            )
            require(
                relief_article == "",
                what=(
                    f"{_DTAA_CSV}: relief_article must be EMPTY "
                    "for a no_dtaa row (same rationale as "
                    "withholding_article)"
                ),
                iso=iso,
                relief_article=relief_article,
            )
            require(
                rate_raw == "",
                what=(
                    f"{_DTAA_CSV}: portfolio_dividend_rate_percent "
                    "must be EMPTY for a no_dtaa row (no DTAA cap "
                    "applies to §91 unilateral relief)"
                ),
                iso=iso,
                rate_raw=rate_raw,
            )
            parsed_withholding_article = None
            parsed_relief_article = None
            parsed_rate = None
        require(
            iso not in out,
            what=f"{_DTAA_CSV}: duplicate iso_alpha2 key",
            iso=iso,
        )
        out[iso] = DtaaDividendEntry(
            iso_alpha2=iso,
            country_name=country_name,
            applicability=applicability,
            withholding_article=parsed_withholding_article,
            relief_article=parsed_relief_article,
            portfolio_dividend_rate_percent=parsed_rate,
            applicability_notes=notes,
            source_citation=citation,
        )
    return out


def lookup_dtaa_dividend(iso_alpha2: str) -> DtaaDividendEntry | None:
    """Return the :class:`DtaaDividendEntry` for ``iso_alpha2``, or
    ``None`` if the country is not in the vendored CSV.

    The tri-state contract this function exposes is the central
    design point of the Form 67 relief computation:

    * Returning a :class:`DtaaDividendEntry` whose
      :attr:`~DtaaDividendEntry.applicability` is
      :attr:`DtaaApplicability.DTAA` selects the §90 / §90A
      pathway (cols 9-10 populate from this entry; col 11 also
      needs col 7 and a proven source rate, see
      :mod:`ibkr_fa.compute.form67`).
    * Returning a :class:`DtaaDividendEntry` whose
      :attr:`~DtaaDividendEntry.applicability` is
      :attr:`DtaaApplicability.NO_DTAA` selects the §91
      unilateral pathway (col 12 may populate when the §91
      precondition is met; cols 9, 10 stay empty per the model
      mutex).
    * Returning ``None`` indicates the country is UNKNOWN
      (unvendored). The compute layer MUST NOT default to either
      §90 or §91 in this case — silently choosing §91 would
      mis-attribute relief for any DTAA country we have not yet
      vendored. The contract is to leave Form 67 cols 9-13
      ``None`` and surface the unvendored country via an
      advisory.

    Args:
        iso_alpha2: ISO 3166-1 alpha-2 country code (e.g., ``"US"``).

    Returns:
        The matching :class:`DtaaDividendEntry`, or ``None`` if the
        ISO code is not vendored.
    """
    table = load_dtaa_dividend_rates()
    return table.get(iso_alpha2)


__all__ = [
    "DtaaApplicability",
    "DtaaDividendEntry",
    "load_dtaa_dividend_rates",
    "lookup_dtaa_dividend",
]
