"""Tests for :mod:`ibkr_fa.models.account_identity` (#618)."""

from __future__ import annotations

from dataclasses import replace
from datetime import date

import pytest

import ibkr_fa.config
import ibkr_fa.models
from ibkr_fa.exceptions import InvariantError
from ibkr_fa.models.account_identity import AccountIdentity, InstitutionIdentity, TaxpayerStatus

_INSTITUTION = InstitutionIdentity(
    name="Interactive Brokers LLC",
    address="One Pickwick Plaza, Greenwich, CT",
    zip_code="06830",
    country_iso2="US",
)
_IDENTITY = AccountIdentity(
    institution=_INSTITUTION,
    account_ref="U12345678",
    opening_date=date(2024, 1, 1),
    status=TaxpayerStatus.BENEFICIAL_OWNER,
    base_currency="USD",
)


def test_taxpayer_status_is_re_exported() -> None:
    assert ibkr_fa.config.TaxpayerStatus is TaxpayerStatus
    assert ibkr_fa.models.TaxpayerStatus is TaxpayerStatus


def test_empty_institution_name_refused() -> None:
    with pytest.raises(InvariantError, match=r"InstitutionIdentity\.name"):
        replace(_INSTITUTION, name="")


@pytest.mark.parametrize(
    ("account_ref", "match"),
    [("", "non-empty"), ("U**********", "never masked"), ("U***", "never masked")],
)
def test_empty_or_masked_account_ref_refused(account_ref: str, match: str) -> None:
    with pytest.raises(InvariantError, match=match):
        replace(_IDENTITY, account_ref=account_ref)


@pytest.mark.parametrize("account_ref", ["U1234567\n", "U***\n"])
def test_account_ref_with_a_control_character_refused(account_ref: str) -> None:
    """#854: debugging context keeps the keys of the character and its position, never the value."""
    with pytest.raises(InvariantError) as exc:
        replace(_IDENTITY, account_ref=account_ref)
    assert exc.value.what == "AccountIdentity.account_ref must not contain a control character"
    assert exc.value.context == (("character", None), ("position", None))


@pytest.mark.parametrize(
    "currency", ["", "usd", "US", "USDX", "USD\n", "ÜSD", "Synthetic Holder Two"]
)
def test_malformed_base_currency_refused(currency: str) -> None:
    """#1021: the error carries no value, since a shifted cell can hold the
    holder's name or account number."""
    with pytest.raises(InvariantError) as exc:
        replace(_IDENTITY, base_currency=currency)
    assert exc.value.context == ()
    assert str(exc.value) == (
        "InvariantError: AccountIdentity.base_currency must be three uppercase ASCII letters"
    )
