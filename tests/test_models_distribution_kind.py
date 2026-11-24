"""Tests for :mod:`ibkr_fa.models.distribution_kind`."""

from __future__ import annotations

from ibkr_fa.compute.schedule_os import NON_ORDINARY_DISTRIBUTION_KINDS
from ibkr_fa.models import ALLOWED_DISTRIBUTION_KINDS, DistributionKind

# ---- enum membership pinning ---------------------------------------------


def test_allowed_distribution_kinds_pinned() -> None:
    """The allow-list pins exactly the three ordinary-dividend kinds
    that flow into Schedule OS 1a. Adding a new kind here is a tax-
    semantics decision (the kind must qualify as ordinary income at
    the user's slab rate); the test is a guard against accidental
    expansion / contraction via a future refactor."""
    assert (
        frozenset(
            {
                DistributionKind.CASH_DIVIDEND,
                DistributionKind.SPECIAL_DIVIDEND,
                DistributionKind.CHOICE_DIVIDEND,
            }
        )
        == ALLOWED_DISTRIBUTION_KINDS
    )


def test_refused_kinds_are_not_allow_listed() -> None:
    """Schedule OS refuses an in-FY payment in lieu (#1281), return of
    capital, long-term capital gain or cash in lieu (#1310). None of those
    four kinds is allow-listed."""
    assert {
        DistributionKind.RETURN_OF_CAPITAL,
        DistributionKind.LONG_TERM_CAPITAL_GAIN,
        DistributionKind.CASH_IN_LIEU,
    } == NON_ORDINARY_DISTRIBUTION_KINDS
    for kind in (*NON_ORDINARY_DISTRIBUTION_KINDS, DistributionKind.PAYMENT_IN_LIEU_OF_DIVIDEND):
        assert kind not in ALLOWED_DISTRIBUTION_KINDS


def test_parse_time_markers_are_not_classified() -> None:
    """The two parse-time failure markers (UNRECOGNISED / MALFORMED)
    are NEVER in the allow-list — they're handled by the compute
    layer's UnrecognisedDistributionTypeError dispatch."""
    for marker in (DistributionKind.UNRECOGNISED, DistributionKind.MALFORMED):
        assert marker not in ALLOWED_DISTRIBUTION_KINDS


def test_distribution_kind_str_values_are_human_readable() -> None:
    """The enum values are user-visible (rendered in error messages
    + the suggested conventional-commit title in
    UnrecognisedDistributionTypeError.__str__). Pin the values so a
    typo surfaces at test time."""
    assert DistributionKind.CASH_DIVIDEND.value == "Cash Dividend"
    assert DistributionKind.SPECIAL_DIVIDEND.value == "Special Dividend"
    assert DistributionKind.CHOICE_DIVIDEND.value == "Choice Dividend"
    assert DistributionKind.RETURN_OF_CAPITAL.value == "Return Of Capital"
    assert DistributionKind.LONG_TERM_CAPITAL_GAIN.value == "Long-Term Capital Gain"
    assert DistributionKind.PAYMENT_IN_LIEU_OF_DIVIDEND.value == "Payment In Lieu Of Dividend"
    assert DistributionKind.CASH_IN_LIEU.value == "Cash in Lieu"
    assert DistributionKind.UNRECOGNISED.value == "<unrecognised>"
    assert DistributionKind.MALFORMED.value == "<malformed>"
