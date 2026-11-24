"""Statement-bundle reconciliation checks for Robinhood."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from ibkr_fa.normalize.robinhood import (
    RobinhoodReconciliationError,
    validate_robinhood_statement_bundles,
)
from ibkr_fa.parser.robinhood_statement import (
    RobinhoodStatement,
    RobinhoodStatementFamily,
)
from tests._private_checks import private_test
from tests._robinhood_private_bundles import check_private_statement_series
from tests._robinhood_statements import robinhood_account_summary, robinhood_statement

_JAN_START = date(2025, 1, 1)
_JAN_END = date(2025, 1, 31)
_FEB_START = date(2025, 2, 1)
_FEB_END = date(2025, 2, 28)
_MAR_START = date(2025, 3, 1)
_MAR_END = date(2025, 3, 31)


def _statement(
    *,
    account: str = "111111111",
    family: RobinhoodStatementFamily = RobinhoodStatementFamily.INDIVIDUAL,
    start: date = _JAN_START,
    end: date = _JAN_END,
    opening: tuple[
        Decimal | None,
        Decimal | None,
        Decimal | None,
        Decimal | None,
    ] = (None, None, None, None),
    closing: tuple[Decimal, Decimal, Decimal, Decimal] = (
        Decimal("10"),
        Decimal("20"),
        Decimal("30"),
        Decimal("60"),
    ),
    registration: tuple[str, ...] = ("TAX PAYER", "TEST ADDRESS"),
    currency: str = "USD",
) -> RobinhoodStatement:
    return robinhood_statement(
        family=family,
        period_start=start,
        period_end=end,
        account_number=account,
        registration_lines=registration,
        base_currency=currency,
        page_count=1,
        account_summary=robinhood_account_summary(
            opening=opening,
            closing=closing,
            closing_percents=(Decimal("10"), Decimal("80"), Decimal("10")),
        ),
    )


def _valid_series(
    *,
    account: str = "111111111",
    first_family: RobinhoodStatementFamily = RobinhoodStatementFamily.UNSPECIFIED,
    explicit_family: RobinhoodStatementFamily = RobinhoodStatementFamily.INDIVIDUAL,
) -> tuple[RobinhoodStatement, RobinhoodStatement]:
    first = _statement(account=account, family=first_family)
    second = _statement(
        account=account,
        family=explicit_family,
        start=_FEB_START,
        end=_FEB_END,
        opening=(
            Decimal("10"),
            Decimal("20"),
            Decimal("30"),
            Decimal("60"),
        ),
        closing=(
            Decimal("11"),
            Decimal("21"),
            Decimal("31"),
            Decimal("63"),
        ),
    )
    return first, second


def test_groups_accounts_sorts_periods_and_resolves_early_family() -> None:
    first, second = _valid_series()
    other_first, other_second = _valid_series(
        account="222222222",
        first_family=RobinhoodStatementFamily.MANAGED_INDIVIDUAL,
        explicit_family=RobinhoodStatementFamily.MANAGED_INDIVIDUAL,
    )

    bundles = validate_robinhood_statement_bundles((other_second, second, other_first, first))

    assert [bundle.account_number for bundle in bundles] == [
        "111111111",
        "222222222",
    ]
    assert bundles[0].family is RobinhoodStatementFamily.INDIVIDUAL
    assert bundles[0].opening_components_unavailable
    assert bundles[0].coverage_start == _JAN_START
    assert bundles[0].coverage_end == _FEB_END
    assert bundles[0].statements == (first, second)
    assert bundles[1].family is RobinhoodStatementFamily.MANAGED_INDIVIDUAL


def test_numeric_first_opening_is_preserved_not_required_to_be_na() -> None:
    first = _statement(
        opening=(
            Decimal("1"),
            Decimal("2"),
            Decimal("3"),
            Decimal("6"),
        )
    )
    bundle = validate_robinhood_statement_bundles((first,))[0]
    assert not bundle.opening_components_unavailable


@pytest.mark.parametrize(
    "statements",
    [
        (),
        (_statement(family=RobinhoodStatementFamily.UNSPECIFIED),),
        (
            *_valid_series(),
            _statement(
                family=RobinhoodStatementFamily.MANAGED_INDIVIDUAL,
                start=_MAR_START,
                end=_MAR_END,
                opening=(
                    Decimal("11"),
                    Decimal("21"),
                    Decimal("31"),
                    Decimal("63"),
                ),
            ),
        ),
        (
            _statement(family=RobinhoodStatementFamily.INDIVIDUAL),
            _statement(
                family=RobinhoodStatementFamily.UNSPECIFIED,
                start=_FEB_START,
                end=_FEB_END,
                opening=(
                    Decimal("10"),
                    Decimal("20"),
                    Decimal("30"),
                    Decimal("60"),
                ),
            ),
        ),
        (
            _statement(),
            _statement(
                start=_JAN_START,
                end=_JAN_END,
                opening=(
                    Decimal("10"),
                    Decimal("20"),
                    Decimal("30"),
                    Decimal("60"),
                ),
            ),
        ),
        (
            _statement(),
            _statement(
                start=date(2025, 1, 15),
                end=_FEB_END,
                opening=(
                    Decimal("10"),
                    Decimal("20"),
                    Decimal("30"),
                    Decimal("60"),
                ),
            ),
        ),
        (
            _statement(),
            _statement(
                start=date(2025, 2, 2),
                end=_FEB_END,
                opening=(
                    Decimal("10"),
                    Decimal("20"),
                    Decimal("30"),
                    Decimal("60"),
                ),
            ),
        ),
        (
            _statement(),
            _statement(
                start=_FEB_START,
                end=_FEB_END,
                opening=(None, None, None, None),
            ),
        ),
        (
            _statement(
                opening=(None, Decimal("0"), None, None),
            ),
        ),
        (_statement(currency="EUR"),),
        (
            _statement(),
            _statement(
                start=_FEB_START,
                end=_FEB_END,
                opening=(
                    Decimal("10"),
                    Decimal("20"),
                    Decimal("30"),
                    Decimal("60"),
                ),
                registration=("OTHER TAXPAYER",),
            ),
        ),
    ],
    ids=[
        "empty",
        "all-unspecified",
        "conflicting-family",
        "unspecified-after-explicit",
        "duplicate-period",
        "overlap",
        "gap",
        "non-earliest-na",
        "partial-na",
        "non-usd",
        "registration-change",
    ],
)
def test_invalid_bundle_contracts_refuse(
    statements: tuple[RobinhoodStatement, ...],
) -> None:
    with pytest.raises(RobinhoodReconciliationError):
        validate_robinhood_statement_bundles(statements)


@pytest.mark.parametrize("component", range(4))
def test_each_adjacent_component_must_match(component: int) -> None:
    first, second = _valid_series()
    source_opening = (
        second.account_summary.opening_brokerage_cash_usd,
        second.account_summary.opening_deposit_sweep_usd,
        second.account_summary.opening_total_securities_usd,
        second.account_summary.opening_portfolio_value_usd,
    )
    if any(value is None for value in source_opening):
        pytest.fail("synthetic opening values are unexpectedly unavailable")
    opening = [
        source_opening[0] or Decimal(),
        source_opening[1] or Decimal(),
        source_opening[2] or Decimal(),
        source_opening[3] or Decimal(),
    ]
    opening[component] += Decimal("0.01")
    bad = _statement(
        start=_FEB_START,
        end=_FEB_END,
        opening=(opening[0], opening[1], opening[2], opening[3]),
        closing=(
            Decimal("11"),
            Decimal("21"),
            Decimal("31"),
            Decimal("63"),
        ),
    )
    with pytest.raises(RobinhoodReconciliationError, match="components disagree"):
        validate_robinhood_statement_bundles((first, bad))


def test_bundle_and_errors_do_not_render_private_source_values() -> None:
    first, second = _valid_series(account="SECRET-ACCOUNT")
    bundle = validate_robinhood_statement_bundles((first, second))[0]
    assert repr(bundle) == object.__repr__(bundle)
    assert "SECRET-ACCOUNT" not in repr(bundle)
    assert "TAX PAYER" not in repr(bundle)

    bad = _statement(account="SECRET-ACCOUNT", currency="EUR")
    with pytest.raises(RobinhoodReconciliationError) as caught:
        validate_robinhood_statement_bundles((bad,))
    assert "SECRET-ACCOUNT" not in str(caught.value)
    assert "TAX PAYER" not in str(caught.value)


@private_test
def test_private_statement_series_validate_without_leaking_source_facts() -> None:
    """The brokerage statements in the statement folders form validated
    bundles that match the statements they were built from. Skips when no
    PDF there is a brokerage statement."""
    check_private_statement_series()
