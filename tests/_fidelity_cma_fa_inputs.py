"""Synthetic Fidelity CMA statements, privacy markers and refusal texts for the
Schedule FA peak-bound tests that several test modules share.

``mixed_day_statement()`` adapts ``tests._fidelity_cma``'s
``annual_statement()``, and every other statement here starts from it.
"""

from __future__ import annotations

import re
from dataclasses import replace
from datetime import date
from decimal import Decimal

from ibkr_fa.parser.fidelity_cma import (
    CmaCashActivity,
    CmaCashActivityKind,
    CmaCoreTransaction,
    CmaCoreTransactionKind,
    CmaIncomeCredit,
    CmaIncomeKind,
    CmaStatement,
)
from tests._fidelity_cma import annual_statement


def mixed_day_statement(*, ending_fcb: str = "2") -> CmaStatement:
    """``annual_statement()`` with ``ending_fcb`` USD free credit balance, and
    its 1 June deposit and withdrawal described as a credit and a debit."""
    base = annual_statement()
    opening, deposit, withdrawal = base.cash_activity
    return replace(
        base,
        free_credit_balance_usd=Decimal(ending_fcb),
        cash_activity=(
            opening,
            replace(deposit, description="mixed-day credit"),
            replace(withdrawal, description="mixed-day debit"),
        ),
    )


# Values a refusal must never print (#1135).
CMA_MARKER_HOLDER = "Synthetic Holder Two"
CMA_MARKER_ACCOUNT = "X99-887766"
CMA_MARKER_NAV = "1.0473"


def marker_statement(year: int = 2025) -> CmaStatement:
    """``mixed_day_statement()`` in ``year`` for ``CMA_MARKER_HOLDER``'s account
    ``CMA_MARKER_ACCOUNT``, with its 10 units bought and held at a NAV of
    ``CMA_MARKER_NAV``."""
    base = mixed_day_statement()
    (buy,) = base.core_transactions
    opening, *rest = base.cash_activity
    cost = Decimal(10) * Decimal(CMA_MARKER_NAV)
    return replace(
        base,
        period_start=date(year, 1, 1),
        period_end=date(year, 12, 31),
        account_id=CMA_MARKER_ACCOUNT,
        account_holder=CMA_MARKER_HOLDER,
        additions_usd=cost + 5,
        ending_account_value_usd=cost + 2,
        account_value_usd=cost + 2,
        position=replace(
            base.position, price_per_unit_usd=Decimal(CMA_MARKER_NAV), ending_market_value_usd=cost
        ),
        core_transactions=(
            replace(
                buy,
                date=date(year, 1, 1),
                price_per_unit_usd=Decimal(CMA_MARKER_NAV),
                amount_usd=cost,
                balance_usd=cost,
            ),
        ),
        cash_activity=tuple(
            replace(activity, date=activity.date.replace(year=year))
            for activity in (replace(opening, amount_usd=cost), *rest)
        ),
    )


def assert_no_marker(text: str, *, year: int | None = None) -> None:
    """``text`` has no marker value, and no number but Table A2's and ``year``."""
    for value in (CMA_MARKER_HOLDER, CMA_MARKER_ACCOUNT, CMA_MARKER_NAV):
        assert value not in text
    assert set(re.findall(r"\d+", text)) <= {"2", str(year)}


def late_interest_statements() -> tuple[CmaStatement, CmaStatement]:
    """January-June, then July-December crediting interest dated June 30."""
    base = mixed_day_statement()
    first = replace(
        base,
        period_end=date(2025, 6, 30),
        additions_usd=Decimal("10"),
        subtractions_usd=Decimal(0),
        ending_account_value_usd=Decimal("10"),
        account_value_usd=Decimal("10"),
        free_credit_balance_usd=None,
        cash_activity=(base.cash_activity[0],),
    )
    second = replace(
        base,
        period_start=date(2025, 7, 1),
        beginning_account_value_usd=Decimal("10"),
        additions_usd=Decimal("1"),
        subtractions_usd=Decimal("-1"),
        ending_account_value_usd=Decimal("10"),
        account_value_usd=Decimal("10"),
        free_credit_balance_usd=None,
        position=replace(
            base.position,
            beginning_market_value_usd=Decimal("10"),
        ),
        core_transactions=(),
        income_credits=(
            CmaIncomeCredit(
                date(2025, 6, 30),
                CmaIncomeKind.CREDIT_INTEREST,
                Decimal("1"),
                None,
            ),
        ),
        cash_activity=(
            CmaCashActivity(
                date(2025, 7, 15),
                CmaCashActivityKind.WITHDRAWAL,
                "following-statement debit",
                Decimal("-1"),
            ),
        ),
    )
    return first, second


def accrued_interest_statements() -> tuple[CmaStatement, CmaStatement]:
    """June's account value includes $1 accrued interest. The following
    statement credits it as cash; month-end cash and quantity, and the final
    statement's value, still reconcile."""
    first, second = late_interest_statements()
    return (
        replace(first, accrued_interest_usd=Decimal("1"), account_value_usd=Decimal("11")),
        replace(second, beginning_account_value_usd=Decimal("11"), additions_usd=Decimal(0)),
    )


ACCRUED_INTEREST_REFUSAL = (
    "non-zero accrued interest is unsupported for CMA peak bounds: statement 1 (by period "
    "start, year-end reports excluded) shows it, so enter the Fidelity CMA Schedule FA by hand."
)


def inverting_statement(*, debit_first: bool) -> CmaStatement:
    """#888's reproduction: native-first bounds whose INR order inverts.

    One sweep-fund unit plus free credit balance. 2025-03-10 has a 5,000 deposit
    and a 4,000 withdrawal, in either listed order; a 3,900 deposit on
    2025-12-10 brings the account to 5,900.
    """
    deposit = CmaCashActivity(
        date(2025, 3, 10), CmaCashActivityKind.DEPOSIT, "same-day credit", Decimal(5000)
    )
    withdrawal = CmaCashActivity(
        date(2025, 3, 10), CmaCashActivityKind.WITHDRAWAL, "same-day debit", Decimal(-4000)
    )
    base = mixed_day_statement()
    return replace(
        base,
        additions_usd=Decimal(9900),
        subtractions_usd=Decimal(-4000),
        ending_account_value_usd=Decimal(5900),
        account_value_usd=Decimal(5900),
        free_credit_balance_usd=Decimal(5899),
        position=replace(base.position, quantity=Decimal(1), ending_market_value_usd=Decimal(1)),
        core_transactions=(
            CmaCoreTransaction(
                date(2025, 1, 2),
                CmaCoreTransactionKind.BUY,
                Decimal(1),
                Decimal(1),
                Decimal(1),
                Decimal(1),
            ),
        ),
        cash_activity=(
            CmaCashActivity(
                date(2025, 1, 2), CmaCashActivityKind.DEPOSIT, "opening deposit", Decimal(1000)
            ),
            *((withdrawal, deposit) if debit_first else (deposit, withdrawal)),
            CmaCashActivity(
                date(2025, 12, 10), CmaCashActivityKind.DEPOSIT, "late deposit", Decimal(3900)
            ),
        ),
    )


NO_SESSION_CLOSE = (
    "Fidelity CMA Schedule FA Table A2: the SPAXX daily NAV series has no Close for a "
    "US market session in the priced window. Move SPAXX.csv, and any SPAXX.csv.lock beside "
    "it, out of the nasdaq-mutual-funds folder of the price cache directory "
    "(--price-cache-dir; by default the .ibkr-fa-price-cache folder in the --input "
    "directory), then re-run: a fresh fetch can return closes a series cached earlier "
    "lacks. If the fresh series still lacks a held or traded day's close, enter the "
    "Fidelity CMA Schedule FA by hand."
)
NON_SESSION_CLOSE = (
    "Fidelity CMA Schedule FA Table A2: the SPAXX daily NAV series has a Close dated on a "
    "day that is not a US market session in the priced window. Move SPAXX.csv, and any "
    "SPAXX.csv.lock beside it, out of the nasdaq-mutual-funds folder of the price cache "
    "directory (--price-cache-dir; by default the .ibkr-fa-price-cache folder in the --input "
    "directory), then re-run: a fresh fetch can return the series without that Close. If "
    "the fresh series still has it, enter the Fidelity CMA Schedule FA by hand."
)
