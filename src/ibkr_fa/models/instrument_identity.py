"""Source-neutral identity of one listed security (#628).

Each security config builds an :class:`InstrumentIdentity` from its own keys,
so lot builders and compute read one type instead of each source's flat
fields. Source-specific facts (a Robinhood CUSIP and scope fields, a
NetBenefits CUSIP, entity details) stay on the source's config.
"""

from __future__ import annotations

from dataclasses import dataclass

from ibkr_fa.invariants import require
from ibkr_fa.models._identifier_checks import require_isin_shape, require_issuer_country_code
from ibkr_fa.types import CountryCode, Isin, Ticker, isin_check_digit

_MODEL = "InstrumentIdentity"


@dataclass(frozen=True, slots=True, repr=False)
class InstrumentIdentity:
    """One listed security, as filed.

    The repr is redacted, as on ``RobinhoodSecurityConfig``: a symbol and ISIN
    disclose a holding.

    Attributes:
        symbol: Exchange-traded symbol.
        isin: ISO 6166 ISIN with a valid check digit.
        listing_exchange: Exchange the security trades on (e.g. ``"NASDAQ"``).
        issuer_country_code: ISO 3166-1 alpha-2 country of the issuer. ``"IN"``
            is allowed; the sources that refuse it do so in their config parser.
    """

    symbol: Ticker
    isin: Isin
    listing_exchange: str
    issuer_country_code: CountryCode

    def __post_init__(self) -> None:
        require(self.symbol != "", what=f"{_MODEL}.symbol must be non-empty")
        require_isin_shape(_MODEL, self.isin)
        require(
            isin_check_digit(self.isin[:11]) == self.isin[11],
            what=f"{_MODEL}.isin check digit is invalid",
            isin=self.isin,
        )
        require(self.listing_exchange != "", what=f"{_MODEL}.listing_exchange must be non-empty")
        require_issuer_country_code(_MODEL, self.issuer_country_code)
