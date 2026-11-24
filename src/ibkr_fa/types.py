"""Type aliases, the ISIN and CUSIP shapes, identifier check digits and the
standard option multiplier for ibkr-fa.

Uses ``typing.NewType`` for identifiers that are conceptually distinct
even though they share an underlying ``str`` representation. This
prevents bugs like passing an account ID where an ISIN is expected.
"""

from __future__ import annotations

import re
from decimal import Decimal
from typing import Final, Literal, NewType

Isin = NewType("Isin", str)
"""ISO 6166 International Securities Identification Number (12 chars).

Validated by :func:`ibkr_fa.parser.open_positions.parse_open_positions`.
Empty/missing ISINs (rare; some IBKR rows lack an ISIN) are surfaced
as ``ParseError`` rather than smuggled in as the empty string.
"""


Ticker = NewType("Ticker", str)
"""Exchange-traded symbol (e.g., ``XQAA``, ``XMPL``).

Not globally unique — the same ticker on different exchanges may refer
to different securities. Use :data:`Isin` for cross-exchange identity.
"""


AccountId = NewType("AccountId", str)
"""IBKR account number, as it appears in the Flex Query and on the IBKR
account-management portal.

Carried through into :class:`ibkr_fa.models.account_info.AccountInfo`
and validated against ``user.toml`` to avoid mixing statements between
different accounts.
"""


CountryCode = NewType("CountryCode", str)
"""ISO 3166-1 alpha-2 country code (uppercase, e.g., ``"US"``, ``"NL"``).

In FA output rows this is mapped to the CBDT ITR-2 country code via
``reference/itr_country_codes.csv``.
"""


PeakBasis = Literal["close", "high"]
"""How Schedule FA Table A3 "Peak value during the period" is valued.

``"close"`` (default) uses each day's closing price; ``"high"`` uses the
daily intraday high. See :mod:`ibkr_fa.compute.fa_a3_peak_value`.
"""


PeakCurrency = Literal["native", "inr"]
"""Currency used to select a Schedule FA peak.

``"native"`` (default) selects the foreign-currency peak before converting
that date's value to INR. ``"inr"`` selects the largest daily INR-converted
value.
"""


ISIN_PATTERN: Final[re.Pattern[str]] = re.compile(r"^[A-Z]{2}[A-Z0-9]{9}[0-9]$")
"""The ISO 6166 ISIN shape: 2 upper-case letters, 9 upper-case letters or
digits, then 1 digit, all ASCII.

Shape only; the check digit is :func:`isin_check_digit`. Keep the ``^`` and
``$`` anchors: some callers use ``match``, not ``fullmatch``.
"""


CUSIP_PATTERN: Final[re.Pattern[str]] = re.compile(r"\A[A-Z0-9]{9}\Z")
"""The CUSIP shape: 9 upper-case letters or digits, all ASCII (8 identifier
characters, then the check digit).

Shape only; :func:`cusip_check_digit_matches` checks the digit. Anchored to
the whole string, so ``match`` and ``fullmatch`` agree.
"""


STANDARD_OPTION_MULTIPLIER: Final[Decimal] = Decimal("100")
"""The shares one standard U.S. option contract covers.

Keep the exponent: ``str()`` of a contract's multiplier is written into the
Massive artifact metadata.
"""


def isin_check_digit(body: str) -> str:
    """The ISO 6166 check digit for an 11-char ISIN body (country + CUSIP).

    Letters expand to two digits (A=10 .. Z=35), then the Luhn mod-10 algorithm
    over the resulting digit string yields the check digit.
    """
    digits = "".join(str(int(c, 36)) for c in body)
    total = 0
    for i, ch in enumerate(reversed(digits)):
        d = int(ch)
        if i % 2 == 0:
            doubled = d * 2
            d = doubled // 10 + doubled % 10
        total += d
    return str((10 - total % 10) % 10)


def cusip_check_digit(body: str) -> str:
    """The check digit for an 8-character CUSIP body (the first 8 of the 9).

    Letters expand to A=10 .. Z=35; every second character (1-indexed from the
    left, i.e. the even positions) is doubled, each contribution is reduced to
    its digit sum, then the modular complement is the check digit.
    """
    total = 0
    for i, ch in enumerate(body):
        v = int(ch, 36)
        if i % 2 == 1:
            v *= 2
        total += v // 10 + v % 10
    return str((10 - total % 10) % 10)


def cusip_check_digit_matches(cusip: str) -> bool:
    """Whether a 9-character CUSIP's last character is :func:`cusip_check_digit`
    of its first 8. The caller makes sure ``cusip`` has 9 characters."""
    return cusip_check_digit(cusip[:8]) == cusip[8]
