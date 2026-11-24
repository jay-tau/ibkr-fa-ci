"""Tests for the ISIN / CUSIP check-digit helpers in :mod:`ibkr_fa.types` (#603)."""

from __future__ import annotations

import pytest

from ibkr_fa.types import cusip_check_digit, isin_check_digit


@pytest.mark.parametrize(
    "isin",
    [
        "US99993A1088",  # synthetic: private-use issuer, check digit worked by hand
        # Synthetic, private-use issuers; check digits from a script that
        # imports no ibkr_fa code (#1070).
        "GB99970K1039",
        "JP99970Z1002",
        "DE99964DAB34",  # letters in the issue number too
        "US99964V1070",  # check digit 0
    ],
)
def test_isin_check_digit_matches_known_isins(isin: str) -> None:
    assert isin_check_digit(isin[:11]) == isin[11]


@pytest.mark.parametrize(
    "cusip",
    [
        "99993A108",  # synthetic: private-use issuer, check digit worked by hand
        # Synthetic, private-use issuers; check digits from a script that
        # imports no ibkr_fa code (#1070).
        "99972B101",
        "99972L109",
        "99972P100",
        "99971Z109",
    ],
)
def test_cusip_check_digit_matches_known_cusips(cusip: str) -> None:
    assert cusip_check_digit(cusip[:8]) == cusip[8]
