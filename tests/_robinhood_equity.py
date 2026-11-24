"""Shared synthetic Robinhood account that holds configured equities."""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from types import SimpleNamespace
from typing import cast

from ibkr_fa.config import RobinhoodConfig, RobinhoodSecurityConfig
from ibkr_fa.normalize.robinhood.accounts import RobinhoodAccountsReconciliation
from ibkr_fa.normalize.robinhood.inventory import RobinhoodSecurityQuantityResult
from tests._robinhood_security import robinhood_security
from tests._robinhood_statements import robinhood_account_config

SECURITY = robinhood_security()


def held_equity(
    *,
    opening_date: date,
    movement_date: date,
    securities: tuple[RobinhoodSecurityConfig, ...] = (SECURITY,),
) -> tuple[RobinhoodAccountsReconciliation, RobinhoodConfig, RobinhoodSecurityQuantityResult]:
    """One account that receives one share of each of ``securities`` on
    ``movement_date``."""
    account = SimpleNamespace(
        activity=SimpleNamespace(
            primary_bundle=SimpleNamespace(
                account_number="111111111",
                statements=(),
            )
        )
    )
    configured = robinhood_account_config(
        "111111111",
        opening_date=opening_date,
        address="500 Colonial Center Parkway",
        zip_code="32746",
        source="test",
    )
    quantities = SimpleNamespace(
        blockers=(),
        movements=tuple(
            SimpleNamespace(
                account_index=0,
                cusip=security.cusip,
                activity_date=movement_date,
                signed_quantity=Decimal("1"),
            )
            for security in securities
        ),
        residuals=(),
        anchors=(),
    )
    return (
        cast("RobinhoodAccountsReconciliation", SimpleNamespace(accounts=(account,))),
        RobinhoodConfig(accounts=(configured,), securities=securities),
        cast("RobinhoodSecurityQuantityResult", quantities),
    )
