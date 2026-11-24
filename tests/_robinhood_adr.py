"""The synthetic held ADR the Robinhood entity-cache and remedy tests share
(#1794): its security config and observed input, the SEC Form F-6 page and
depositary GLEIF record an opener serves for it, the ADR lookup and its cached
F-6, and the whole refusal an unusable F-6 cache gives.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

from ibkr_fa.compute.robinhood_entity_overrides import (
    RobinhoodAdrEntityAudit,
    resolve_robinhood_adr_entity_overrides,
)
from ibkr_fa.config import FaEntityOverride, RobinhoodSecurityConfig
from ibkr_fa.models.reference_identity import GleifLegalAddressRecord, ObservedIdentityInput
from ibkr_fa.robinhood_adr_entity_sources import _artifact_path, load_robinhood_adr_entity_evidence
from ibkr_fa.robinhood_identity_sources import IdentitySourceOpener
from ibkr_fa.robinhood_legal_entity_sources import LegalAddressOpener
from ibkr_fa.types import Isin
from tests._entity_cache_remedies import MOVE_ASIDE, MOVE_ASIDE_SET_CONTACT, SET_IDENTITY_TOO
from tests._gleif_records import gleif_address, gleif_record, gleif_reply
from tests._no_echo import assert_no_markers
from tests._robinhood_security import robinhood_security

ADR_ISIN = Isin("US99992F1084")
ADR_DEPOSITARY_LEI = "E57ODZWZ7FF32TWEFA76"
F6_URL = "https://www.sec.gov/Archives/edgar/data/9991080/000999100920000061/xqax-f6.htm"


def adr_security(*, depositary: bool = True) -> RobinhoodSecurityConfig:
    return robinhood_security(
        symbol="XQAX",
        cusip="99992F108",
        isin=ADR_ISIN,
        listing_exchange="NYSE",
        is_depositary_receipt=depositary,
        source=f"ADS programme Form F-6: {F6_URL}",
    )


def adr_observed() -> ObservedIdentityInput:
    return ObservedIdentityInput("99992F108", "XQAX", "Sample Issuer AX")


def f6_document(
    *, issuer: str = "Sample Issuer AX Holding Limited", depositary: str = "CITIBANK, N.A."
) -> bytes:
    return f"""\
<html><body>
<p>FORM F-6</p>
<p>REGISTRATION STATEMENT FOR AMERICAN DEPOSITARY SHARES</p>
<b>{issuer}</b>
<p>(Exact name of issuer of deposited securities as specified in its charter)</p>
<b>{depositary}</b>
<p>(Exact name of depositary as specified in its charter)</p>
</body></html>
""".encode()


def _gleif_record(
    *, lei: str, name: str, country: str = "US", successor_name: str | None = None
) -> dict[str, object]:
    return gleif_record(
        lei=lei,
        legal_name=name,
        legal_address=gleif_address(
            ["5800 S Corporate Place"],
            city="Sioux Falls",
            region="US-SD",
            country=country,
            postal_code="57108",
        ),
        jurisdiction="US",
        category="GENERAL",
        legal_form_id="62VJ",
        successor_name=successor_name,
        last_update="2026-05-01T00:00:00Z",
    )


def depositary_gleif_reply(*, successor_name: str | None = None) -> bytes:
    return gleif_reply(
        _gleif_record(
            lei=ADR_DEPOSITARY_LEI,
            name="Citibank, National Association",
            successor_name=successor_name,
        ),
        publish_date="2026-08-25T00:00:00Z",
    )


class AdrOpener:
    def __init__(
        self,
        *,
        issuer: str = "Sample Issuer AX Holding Limited",
        gleif: bytes | None = None,
    ) -> None:
        self.issuer = issuer
        self.gleif = gleif
        self.calls: list[str] = []

    def __call__(self, url: str) -> bytes:
        # Read as this module's globals: test_robinhood_sec_contact_remedy.py
        # patches ADR_DEPOSITARY_LEI, f6_document and depositary_gleif_reply here.
        self.calls.append(url)
        if "sec.gov/Archives/" in url:
            return f6_document(issuer=self.issuer)
        if url.endswith(f"/{ADR_DEPOSITARY_LEI}"):
            return depositary_gleif_reply() if self.gleif is None else self.gleif
        raise AssertionError(f"unexpected URL: {url}")


def resolve_adr(
    security: RobinhoodSecurityConfig,
    tmp_path: Path,
    *,
    fetch: bool,
    sec_opener: IdentitySourceOpener,
    gleif_opener: LegalAddressOpener,
) -> tuple[
    tuple[FaEntityOverride, ...],
    tuple[GleifLegalAddressRecord, ...],
    RobinhoodAdrEntityAudit,
]:
    lookup = load_robinhood_adr_entity_evidence(
        (security,),
        (adr_observed(),),
        (),
        cutoff=date(2025, 12, 31),
        cache_dir=tmp_path,
        fetch=fetch,
        sec_user_agent="test@example.com",
        sec_opener=sec_opener,
        gleif_opener=gleif_opener,
    )
    return resolve_robinhood_adr_entity_overrides((security,), lookup)


def cached_f6(tmp_path: Path) -> Path:
    opener = AdrOpener()
    resolve_adr(adr_security(), tmp_path, fetch=True, sec_opener=opener, gleif_opener=opener)
    return _artifact_path(tmp_path, adr_security(), "Sample Issuer AX", date(2025, 12, 31))


def assert_f6_refusal(
    message: str,
    cache_dir: Path,
    path: Path,
    problem: str,
    *,
    fetch: bool,
    set_contact: bool = False,
) -> None:
    """The whole F-6 cache refusal, so it can't also carry a cached value.

    It names the cache folder, never the ISIN, which may be built from a
    statement CUSIP, nor the artifact's path, whose folder name is a
    fingerprint (#1071). ``set_contact``: the run had no usable SEC contact.
    """
    move_aside = (MOVE_ASIDE_SET_CONTACT if set_contact else MOVE_ASIDE)[fetch]
    assert message == (
        f"SEC Form F-6 cache for a held ADR is unusable ({problem}): "
        f"{cache_dir / 'robinhood-adr-entities'}. {move_aside}, or review each held "
        "ADR's legal entity yourself and set prefer_configured_address, prefer_configured_name "
        "and prefer_configured_nature in its [[fa.entities]] entry to skip the lookup. "
        f"{SET_IDENTITY_TOO}"
    )
    assert_no_markers(message, ADR_ISIN, str(path), path.parent.name)
