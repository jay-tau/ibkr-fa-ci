"""Source-neutral identity of one foreign custodial account (#618).

Each source maps its own config and statement facts into an
:class:`AccountIdentity`. The Schedule FA Table A2 builder reads only this,
so it needs no source's config types.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date
from enum import StrEnum
from typing import Final

from ibkr_fa.invariants import require
from ibkr_fa.models._identifier_checks import require_no_control_character
from ibkr_fa.pii import is_masked_account_id

# ISO 4217 currency codes are exactly 3 uppercase ASCII letters.
_CURRENCY_PATTERN: Final[re.Pattern[str]] = re.compile(r"[A-Z]{3}")


class TaxpayerStatus(StrEnum):
    """How the user owns / interacts with a foreign account.

    Values match the strings the e-filing portal accepts in Schedule
    FA's "Status" cell. We map them 1:1 so the CLI can emit them
    without translation.
    """

    BENEFICIAL_OWNER = "BeneficialOwner"
    JOINT = "Joint"
    SIGNING_AUTHORITY = "SigningAuthority"


# A filer who can only sign on an account doesn't own its holdings, and ibkr-fa
# doesn't support such accounts (#887). Config loading refuses the status with
# this message; compute refuses it again for an identity built in code.
SIGNING_AUTHORITY_UNSUPPORTED: Final[str] = (
    "signing-authority accounts aren't supported; the supported statuses are "
    "BeneficialOwner and Joint"
)


@dataclass(frozen=True, slots=True)
class InstitutionIdentity:
    """The custodial institution that holds a foreign account.

    Attributes:
        name: Institution legal name (e.g. ``"Interactive Brokers LLC"``).
        address: Institution address (free text).
        zip_code: Institution ZIP / postal code; may be empty.
        country_iso2: ISO 3166-1 alpha-2 code of the institution's country.
    """

    name: str
    address: str
    zip_code: str
    country_iso2: str

    def __post_init__(self) -> None:
        require(self.name != "", what="InstitutionIdentity.name must be non-empty")


@dataclass(frozen=True, slots=True)
class AccountIdentity:
    """One foreign custodial account, as filed.

    Attributes:
        institution: The custodial institution.
        account_ref: The real account number; never a masked literal.
        opening_date: Account opening date, as filed.
        status: How the taxpayer owns the account.
        base_currency: ISO 4217 code of the account's base currency.
        statement_masked_account_ref: ``True`` when the statement masked its
            account number, so :attr:`account_ref` came from config alone.
    """

    institution: InstitutionIdentity
    account_ref: str
    opening_date: date
    status: TaxpayerStatus
    base_currency: str
    statement_masked_account_ref: bool = False

    def __post_init__(self) -> None:
        require(self.account_ref != "", what="AccountIdentity.account_ref must be non-empty")
        require_no_control_character("AccountIdentity", "account_ref", self.account_ref)
        require(
            not is_masked_account_id(self.account_ref),
            what="AccountIdentity.account_ref must be the real account number, never masked",
        )
        # No value in the context: a shifted cell can hold the holder's name or account.
        require(
            _CURRENCY_PATTERN.fullmatch(self.base_currency) is not None,
            what="AccountIdentity.base_currency must be three uppercase ASCII letters",
        )
