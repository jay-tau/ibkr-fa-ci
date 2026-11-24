"""Tests for AccountInfo domain model."""

from __future__ import annotations

import pytest

from ibkr_fa.exceptions import InvariantError
from ibkr_fa.models import AccountInfo
from ibkr_fa.types import AccountId


def make_account_info(**overrides: object) -> AccountInfo:
    """Factory with sensible defaults; overrides via kwargs."""
    defaults: dict[str, object] = {
        "account_id": AccountId("U1234567"),
        "account_holder_name": "Sample Holder",
        "base_currency": "USD",
    }
    defaults.update(overrides)
    return AccountInfo(**defaults)  # type: ignore[arg-type]


def test_account_info_constructs_with_typical_us_individual_account() -> None:
    info = make_account_info()
    assert info.account_id == "U1234567"
    assert info.account_holder_name == "Sample Holder"
    assert info.base_currency == "USD"


def test_account_info_is_frozen() -> None:
    info = make_account_info()
    with pytest.raises(AttributeError):
        info.account_id = AccountId("U7654321")  # ty: ignore[invalid-assignment]


def test_account_info_uses_slots() -> None:
    info = make_account_info()
    assert not hasattr(info, "__dict__")


def test_account_info_supports_non_usd_base_currency() -> None:
    info = make_account_info(base_currency="EUR")
    assert info.base_currency == "EUR"


# ============================================================================
# Construction-time invariants (PR #122, issue #116)
# ============================================================================


def test_invariant_empty_account_id_rejected() -> None:
    with pytest.raises(InvariantError) as exc:
        make_account_info(account_id=AccountId(""))
    assert "account_id must be non-empty" in exc.value.what


def test_invariant_empty_account_holder_name_rejected() -> None:
    with pytest.raises(InvariantError) as exc:
        make_account_info(account_holder_name="")
    assert "account_holder_name must be non-empty" in exc.value.what


# ``$`` also matches just before a final line break (#835).
@pytest.mark.parametrize(
    "bad_currency", ["", "us", "USDT", "us1", "USD\n", "ÜSD", "Synthetic Holder Two"]
)
def test_invariant_malformed_base_currency_rejected(bad_currency: str) -> None:
    """#1021: the error carries no value, since a shifted cell can hold the
    holder's name or account number."""
    with pytest.raises(InvariantError) as exc:
        make_account_info(base_currency=bad_currency)
    assert exc.value.context == ()
    assert str(exc.value) == (
        "InvariantError: AccountInfo.base_currency must be 3 uppercase ASCII letters (ISO 4217)"
    )
