"""Tests for :mod:`ibkr_fa.reference.indian_adr_mapping` (issue #183).

Anti-data-rot guards for the hardcoded INDIAN_ADR_MAPPING table.
The most important test here is :func:`test_every_mapped_isin_passes_luhn_check`:
the ISO 6166 check digit guards the shipped table against a mistyped
entry, which would silently miss real IBKR holdings under the opt-in
``"underlying-issuer"`` routing mode.
"""

from __future__ import annotations

import pytest

from ibkr_fa.reference.indian_adr_mapping import (
    INDIAN_ADR_MAPPING,
    IndianAdrEntry,
    _isin_check_digit_valid,
    lookup_indian_adr,
)
from ibkr_fa.types import Isin


def test_every_mapped_isin_passes_luhn_check() -> None:
    """Every entry in :data:`INDIAN_ADR_MAPPING` must pass the ISO 6166
    modified-Luhn check digit. A Luhn-invalid ISIN in the table is
    almost certainly a typo or a source-of-record error. Failing this
    test means the opt-in ``"underlying-issuer"`` routing will silently
    miss the real-world IBKR-emitted ISIN (which IS Luhn-valid),
    defeating the entire opt-in.
    """
    failures = [str(isin) for isin in INDIAN_ADR_MAPPING if not _isin_check_digit_valid(str(isin))]
    assert failures == [], (
        f"Luhn-invalid ISINs in INDIAN_ADR_MAPPING: {failures}. "
        f"Real IBKR-emitted ISINs always pass Luhn; an invalid entry "
        f"here is a typo that will cause silent-miss on real input."
    )


def test_every_mapped_isin_is_12_characters() -> None:
    """ISO 6166 ISINs are exactly 12 characters. Defence-in-depth."""
    for isin in INDIAN_ADR_MAPPING:
        assert len(str(isin)) == 12, f"{isin!r} is not 12 characters"


def test_every_mapped_isin_starts_with_non_in_prefix() -> None:
    """The mapping table is explicitly for ADRs / GDRs — instruments
    whose ISIN-derived depositary country is NOT India. Direct
    IN-prefix-ISIN holdings already route to 1a(i) via the
    ``isin[:2] == "IN"`` branch in
    :func:`~ibkr_fa.compute.schedule_os.compute_schedule_os_dividend_lines`;
    they don't need (and must not have) a mapping entry.
    """
    for isin in INDIAN_ADR_MAPPING:
        assert not str(isin).startswith("IN"), (
            f"{isin!r}: IN-prefix ISINs are direct Indian-share holdings "
            f"that already route to 1a(i) without the mapping table; "
            f"remove this entry"
        )


def test_every_entry_has_non_empty_underlying_issuer_name() -> None:
    """The audit trail needs a non-empty human-readable name."""
    for isin, entry in INDIAN_ADR_MAPPING.items():
        assert entry.underlying_issuer_name, f"{isin!r}: underlying_issuer_name is empty / falsy"


def test_depositary_country_matches_isin_prefix() -> None:
    """``depositary_country_iso2`` must match the ISIN's first two
    characters (depositary-country derivation rule from
    :mod:`docs/portal/adr-handling.md`).
    """
    for isin, entry in INDIAN_ADR_MAPPING.items():
        assert entry.depositary_country_iso2 == str(isin)[:2], (
            f"{isin!r}: depositary_country_iso2 "
            f"{entry.depositary_country_iso2!r} does not match the ISIN's "
            f"first two chars {str(isin)[:2]!r}"
        )


def test_lookup_indian_adr_returns_entry_for_mapped_isin() -> None:
    """Spot-check the lookup helper against a known mapping (INFY).
    The exhaustive walk-the-table test is
    :func:`test_every_mapped_isin_passes_luhn_check` above.
    """
    entry = lookup_indian_adr(Isin("US4567881085"))
    assert isinstance(entry, IndianAdrEntry)
    assert entry.underlying_issuer_name == "Infosys Ltd"
    assert entry.depositary_country_iso2 == "US"


def test_lookup_indian_adr_returns_none_for_unmapped_isin() -> None:
    """Foreign companies not in the mapping (e.g., XQAQ) return
    ``None`` — the mapping is exhaustive, no heuristic matching."""
    assert lookup_indian_adr(Isin("US99992A1097")) is None  # XQAQ


# ---- _isin_check_digit_valid unit tests ---------------------------------


@pytest.mark.parametrize(
    "valid_isin",
    [
        # A synthetic US ISIN.
        "US99992A1097",
        # Synthetic, private-use issuers; check digits from a script that
        # imports no ibkr_fa code (#1070).
        "GB99970K1039",
        "JP99970Z1002",
        "DE99970H1001",
        "US99972A1034",
        "US99971Z1093",
        "US99971Y1029",
        "US99972G1004",
        "US99971V1089",
        "JE99970Y1031",
        "KY99971A1042",
    ],
)
def test_isin_check_digit_valid_accepts_valid_isins(valid_isin: str) -> None:
    assert _isin_check_digit_valid(valid_isin)


@pytest.mark.parametrize(
    "invalid_isin",
    [
        # Wrong check digits on synthetic MH and KY ISINs (the right ones are 1 and 5).
        "MH99971D1082",
        "KY99971B1021",
        # Wrong length.
        "US99992A109",  # 11 chars
        "US99992A10977",  # 13 chars
        "",  # empty
        # Wrong character set.
        "us99992a1097",  # lowercase
        "US99992A109!",  # non-alphanumeric
        "US99992A10\u00b27",  # superscript two: int(c, 36) raises on it
        "US99992A1\u066097",  # Arabic-Indic zero: int(c, 36) reads it as 0
        "US\u00c99992A1094",  # uppercase E with acute: ISINs are ASCII only
        # Wrong check digit (the synthetic US ISIN above ends in 7; tamper to 8).
        "US99992A1098",
    ],
)
def test_isin_check_digit_valid_rejects_invalid_isins(invalid_isin: str) -> None:
    assert not _isin_check_digit_valid(invalid_isin)
