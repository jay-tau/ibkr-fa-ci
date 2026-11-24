"""Shared builders for synthetic ``[[robinhood.securities]]`` entries, and the
line a refusal prints when one is missing."""

from __future__ import annotations

from pathlib import Path

from ibkr_fa.compute.robinhood_security_identities import RobinhoodSecurityIdentities
from ibkr_fa.config import RobinhoodConfig, RobinhoodSecurityConfig
from ibkr_fa.models import InstrumentIdentity
from ibkr_fa.types import CountryCode, Isin, Ticker

# A valid synthetic US ISIN for CUSIP 99971T103 (check digit 4).
SYNTHETIC_ISIN = Isin("US99971T1034")


def robinhood_security(
    *,
    symbol: str = "TEST",
    cusip: str = "99971T103",
    isin: str = SYNTHETIC_ISIN,
    listing_exchange: str = "NASDAQ",
    sub_category: str = "COMMON",
    asset_class: str = "STK",
    issuer_country_code: str = "US",
    is_depositary_receipt: bool = False,
    source: str = "test",
) -> RobinhoodSecurityConfig:
    """One configured Robinhood security; override any field by keyword."""
    return RobinhoodSecurityConfig(
        instrument=InstrumentIdentity(
            symbol=Ticker(symbol),
            isin=Isin(isin),
            listing_exchange=listing_exchange,
            issuer_country_code=CountryCode(issuer_country_code),
        ),
        cusip=cusip,
        sub_category=sub_category,
        asset_class=asset_class,
        is_depositary_receipt=is_depositary_receipt,
        source=source,
    )


def configured_securities(config: RobinhoodConfig) -> RobinhoodSecurityIdentities:
    """The identity table of a run that generated no identity."""
    return RobinhoodSecurityIdentities(config.securities)


# The name of the private identity guidance file a refusing run writes in its output folder.
IDENTITY_GUIDANCE_FILE = "robinhood-identity-guidance.toml"

# The private identity guidance file's two-line header and the blank line
# before its first block (#1044, #1727).
IDENTITY_GUIDANCE_HEADER = (
    "# Private: holds statement CUSIPs and symbols. Review the source evidence,\n"
    "# replace every placeholder, and add these blocks to user.toml.\n\n"
)


def identity_guidance_line(path: Path) -> str:
    """The line naming the private file ``path`` that holds the suggested
    ``user.toml`` blocks for unconfigured securities (#1044). Every test
    builds the line from here (#1580)."""
    return (
        "the suggested user.toml blocks for the unconfigured securities are in the private "
        f"file {path}: review the source evidence, replace every placeholder, and add them "
        "to user.toml"
    )
