"""The Robinhood option text harness: synthetic option trades and statement
positions and a load of two reconciled accounts.

``tests/test_robinhood_option_texts.py``,
``tests/test_robinhood_option_refusal_stops.py`` and
``tests/test_robinhood_fa_first_refusals.py`` drive it. The load runs the real
account reconciliation and stubs only the bundle load and the FY TTBR table,
unlike the stand-in option runs in ``tests/_robinhood_option_runs.py``.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest

import ibkr_fa.pipeline_robinhood as pipeline
from ibkr_fa.compute.robinhood_options import (
    RobinhoodOptionBlockerKind,
    RobinhoodOptionTreatment,
)
from ibkr_fa.config import RobinhoodConfig
from ibkr_fa.normalize.robinhood import (
    RobinhoodActivityReportEvidence,
    reconcile_robinhood_accounts,
    resolve_robinhood_security_quantities,
    validate_robinhood_statement_bundles,
)
from ibkr_fa.parser.robinhood import RobinhoodEvent, RobinhoodEventKind
from ibkr_fa.parser.robinhood_statement import (
    RobinhoodStatement,
    RobinhoodStatementFamily,
    RobinhoodStatementPosition,
)
from tests._fx_stubs import rule115_rates as _rule115_rates
from tests._robinhood_a3_load import a3_bundle
from tests._robinhood_fa_inputs import fa_account as _account
from tests._robinhood_fa_inputs import fa_cash as _cash
from tests._robinhood_fa_inputs import fa_statement as _statement
from tests._robinhood_fa_inputs import fa_statement_row as _statement_row
from tests._robinhood_option_inputs import (
    option_classification,
    option_event,
    option_statement_position,
)
from tests._robinhood_refusals import (
    FNO_FEMA_WARNING,
    OPTION_BLOCKER_MESSAGES,
    OPTION_CLASSIFICATION_RECIPE,
)
from tests._robinhood_remedies import FOLLOWED_REMEDY_ACCOUNT as _ACCOUNT

BUY_TO_OPEN = RobinhoodEventKind.OPTION_BUY_TO_OPEN
SELL_TO_CLOSE = RobinhoodEventKind.OPTION_SELL_TO_CLOSE
_CG = RobinhoodOptionTreatment.CAPITAL_GAINS
_FIRST = "111111111"
_EXPIRY = date(2031, 2, 21)


def option_at_row(
    kind: RobinhoodEventKind,
    row: int,
    day: date,
    *,
    quantity: str = "1",
    amount: str | None = None,
    right: str = "Call",
    expiration: date = _EXPIRY,
) -> RobinhoodEvent:
    """A trade at activity report row ``row`` at $1.00 a share, for ``amount``
    or exactly its premium."""
    premium = Decimal(quantity) * 100
    return option_event(
        kind,
        row,
        day,
        day + timedelta(days=1),
        quantity=quantity,
        amount=amount or str(-premium if kind is BUY_TO_OPEN else premium),
        right=right,
        expiration=expiration,
    )


def held_call(quantity: str) -> RobinhoodStatementPosition:
    """The call statement position, at a market value of zero."""
    return replace(
        option_statement_position(1, quantity=quantity, price="0", expiration=_EXPIRY),
        market_value_usd=Decimal(0),
    )


def _statements(
    number: str,
    family: RobinhoodStatementFamily,
    events: tuple[RobinhoodEvent, ...],
    periods: tuple[tuple[date, date], ...],
    positions: dict[int, tuple[RobinhoodStatementPosition, ...]],
) -> tuple[RobinhoodStatement, ...]:
    """One cash-only statement per period of the events in it, each option
    row carrying its contract, with ``positions`` by statement."""
    statements = []
    cash: Decimal | None = None
    for index, (start, end) in enumerate(periods):
        inside = tuple(event for event in events if start <= event.activity_date <= end)
        opening = cash
        cash = (opening or Decimal()) + sum(
            (event.amount_usd or Decimal() for event in inside), Decimal()
        )
        statement = _statement(
            number,
            family,
            inside,
            cash=cash,
            position_quantity=Decimal(),
            start=start,
            end=end,
            opening_cash=opening,
            opening_securities=None if opening is None else Decimal(),
        )
        rows = tuple(
            replace(
                _statement_row(row, event),
                option_symbol=event.option_symbol,
                option_expiration=event.option_expiration,
                option_right=event.option_right,
                option_strike_usd=event.option_strike_usd,
            )
            for row, event in enumerate(inside, start=1)
        )
        statements.append(replace(statement, activities=rows, positions=positions.get(index, ())))
    return tuple(statements)


def load_option_accounts(
    monkeypatch: pytest.MonkeyPatch,
    run_dir: Path,
    first: tuple[RobinhoodEvent, ...] = (),
    second: tuple[RobinhoodEvent, ...] = (),
    *,
    positions: dict[int, tuple[RobinhoodStatementPosition, ...]] | None = None,
    classified: tuple[str, ...] = (_FIRST,),
    treatment: RobinhoodOptionTreatment = _CG,
    year: int = 2025,
) -> SimpleNamespace:
    """Make every command load two reconciled accounts, ``_FIRST`` (account
    1, Individual) and ``_ACCOUNT`` (account 2, Managed Individual), opened
    on 5 January ``year``. Each gets a deposit at activity report row 1, then
    its option trades; ``positions`` are account 1's statement positions in
    its statement for CY ``year`` (0) and Q1 of the next year (1). The
    ``classified`` accounts' trades in FY ``year`` take ``treatment``.
    Returns the bundle."""
    opening = date(year, 1, 5)
    periods = (
        (date(year, 1, 1), date(year, 12, 31)),
        (date(year + 1, 1, 1), date(year + 1, 3, 31)),
    )
    day = opening + timedelta(days=1)
    deposit = replace(
        _cash(), source_record_index=1, activity_date=day, process_date=day, settle_date=day
    )
    reports = ((deposit, *first), (deposit, *second))
    accounts = tuple(
        replace(_account(number), opening_date=opening) for number in (_FIRST, _ACCOUNT)
    )
    statements = (
        *_statements(
            _FIRST, RobinhoodStatementFamily.INDIVIDUAL, reports[0], periods, positions or {}
        ),
        *_statements(
            _ACCOUNT, RobinhoodStatementFamily.MANAGED_INDIVIDUAL, reports[1], periods, {}
        ),
    )
    paired = reconcile_robinhood_accounts(
        validate_robinhood_statement_bundles(statements),
        tuple(
            RobinhoodActivityReportEvidence(
                events=events, start_date=opening, end_date=periods[1][1]
            )
            for events in reports
        ),
        accounts,
    )
    config = RobinhoodConfig(
        accounts=accounts,
        option_classifications=tuple(
            replace(
                option_classification(treatment, account_number=number),
                financial_year=year,
            )
            for number in classified
        ),
    )
    bundle = a3_bundle(paired, config, run_dir)
    bundle.quantities = resolve_robinhood_security_quantities(paired, bundle.identities, ())
    bundle.identity_resolution = None
    monkeypatch.setattr(pipeline, "_load", lambda **_kwargs: bundle)
    _rule115_rates(monkeypatch)
    return bundle


# The open business position's blockers, as the run names them.
OPEN_BUSINESS_BLOCKERS = (
    "option blockers: "
    f"{OPTION_BLOCKER_MESSAGES[RobinhoodOptionBlockerKind.OPEN_BUSINESS_POSITION]} (1 blocker; "
    "first at account 1, option positions open at the financial year's end)"
)

# A call bought on 1 May 2025 and sold on 2 June 2025, and the
# missing-classification refusal line.
CALL_ROUND_TRIP = (
    option_at_row(BUY_TO_OPEN, 2, date(2025, 5, 1)),
    option_at_row(SELL_TO_CLOSE, 3, date(2025, 6, 2)),
)
CLASSIFICATION_RECIPE_ERROR = f"error: {FNO_FEMA_WARNING} {OPTION_CLASSIFICATION_RECIPE}\n"
