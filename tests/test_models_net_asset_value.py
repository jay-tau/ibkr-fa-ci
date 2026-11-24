"""Tests for :class:`ibkr_fa.models.net_asset_value.NetAssetValueSummary`."""

from __future__ import annotations

from decimal import Decimal

import pytest

from ibkr_fa.exceptions import InvariantError
from ibkr_fa.models.net_asset_value import NavRefusalReason, NetAssetValueSummary


def test_confident_figure_is_valid() -> None:
    summary = NetAssetValueSummary(closing_total=Decimal("123.718346"), refusal_reason=None)
    assert summary.closing_total == Decimal("123.718346")
    assert summary.refusal_reason is None


@pytest.mark.parametrize("reason", list(NavRefusalReason))
def test_refusal_is_valid(reason: NavRefusalReason) -> None:
    summary = NetAssetValueSummary(closing_total=None, refusal_reason=reason)
    assert summary.closing_total is None
    assert summary.refusal_reason is reason


def test_both_set_violates_xor() -> None:
    with pytest.raises(InvariantError):
        NetAssetValueSummary(
            closing_total=Decimal("1"), refusal_reason=NavRefusalReason.NO_ASSET_CLASS_BLOCK
        )


def test_neither_set_violates_xor() -> None:
    with pytest.raises(InvariantError):
        NetAssetValueSummary(closing_total=None, refusal_reason=None)


def test_non_finite_closing_total_rejected() -> None:
    with pytest.raises(InvariantError):
        NetAssetValueSummary(closing_total=Decimal("NaN"), refusal_reason=None)


@pytest.mark.parametrize("reason", ["no-asset-class-block", "section absent", "   "])
def test_refusal_reason_must_be_an_enum_member(reason: str) -> None:
    """#1013: free text, even a member's value, is refused."""
    with pytest.raises(InvariantError, match="must be None or a NavRefusalReason enum member"):
        NetAssetValueSummary(
            closing_total=None,
            refusal_reason=reason,  # ty: ignore[invalid-argument-type]
        )


def test_negative_closing_total_allowed() -> None:
    """Margined accounts can carry a negative NAV; not sign-constrained."""
    summary = NetAssetValueSummary(closing_total=Decimal("-500.00"), refusal_reason=None)
    assert summary.closing_total == Decimal("-500.00")
