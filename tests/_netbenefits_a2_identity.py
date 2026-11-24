"""The NetBenefits account identity the A2 row builder takes, shared by the
NetBenefits A2 row tests and the ``InvariantError`` no-echo tests (#1853)."""

from __future__ import annotations

from datetime import date

from ibkr_fa.models.account_identity import AccountIdentity, InstitutionIdentity, TaxpayerStatus


def netbenefits_a2_identity(
    status: TaxpayerStatus = TaxpayerStatus.BENEFICIAL_OWNER,
) -> AccountIdentity:
    return AccountIdentity(
        institution=InstitutionIdentity(
            name="Fidelity Stock Plan Services LLC",
            address="245 Summer Street, Boston, MA",
            zip_code="02210",
            country_iso2="US",
        ),
        account_ref="I99999999",
        opening_date=date(2025, 12, 31),
        status=status,
        base_currency="USD",
    )
