"""#1546: ``robinhood-cg``'s refusal names the sales, lots, cause and route
for each Robinhood equity CG blocker an input can reach.

On 1f29b0b a ``lot-attribution``, ``pre-cutover-holding-rule`` or
``unsupported-security-class`` blocker printed only "Robinhood equity capital
gains contain unresolved blockers". Each case here runs the real account
reconciliation and CG compute over synthetic statements and activity reports,
behind the existing pipeline stubs, as #1540's tests do.

Two kinds keep only that line, because no input reaches them through the CLI:

- ``evidence-coverage``: the loader's ``validate_robinhood_report_coverage``
  refuses unless every account's activity report starts by its configured
  opening date and ends on or after the FY's end, which is all the CG coverage check
  asks.
- the lot engine's ``inherited-basis``: its one source is a REC receipt
  with no ``[[robinhood.security_receipt_resolutions]]`` entry, and
  ``resolve_robinhood_security_quantities``, which the CG compute calls
  first, refuses such a receipt (#1522).
"""

from __future__ import annotations

import itertools
import re
from collections.abc import Callable
from dataclasses import replace
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

import ibkr_fa.compute.robinhood_capital_gains as cg_mod
from ibkr_fa.compute.robinhood_capital_gains import (
    RobinhoodCapitalGainsBlocker,
    RobinhoodCapitalGainsBlockerKind,
    RobinhoodCapitalGainsResult,
    compute_robinhood_capital_gains,
)
from ibkr_fa.compute.robinhood_lots import RobinhoodLotBlocker, RobinhoodLotBlockerKind
from ibkr_fa.config import RobinhoodConfig
from ibkr_fa.normalize.robinhood import (
    RobinhoodActivityReportEvidence,
    RobinhoodSecurityEventReference,
    reconcile_robinhood_accounts,
)
from ibkr_fa.parser.robinhood import RobinhoodEvent, RobinhoodEventKind
from ibkr_fa.parser.robinhood_statement import RobinhoodStatementFamily
from ibkr_fa.types import isin_check_digit
from tests._fx_stubs import AuditedFxStub
from tests._robinhood_account_inputs import (
    ACCOUNT_OPENING,
    account_config,
    account_statement,
    security_config,
    statement_bundle,
    statement_position,
    statement_row,
)
from tests._robinhood_cg import CG_UNRESOLVED as _CG
from tests._robinhood_cg import (
    FILING_BY_HAND,
    LOT_SHORTFALL_CAUSE,
    PRE_CUTOVER_CAUSE,
    UNSUPPORTED_CLASS_CAUSE,
)
from tests._robinhood_cg import SAME_DAY_CAUSE as _SAME_DAY_CAUSE
from tests._robinhood_cg import SAME_DAY_ROUTE as _SAME_DAY_ROUTE
from tests._robinhood_cg_inputs import traded_cg_result as _traded_result
from tests._robinhood_cli import robinhood_main as _main
from tests._robinhood_income_stubs import (  # noqa: F401 - autouse fixture
    silenced_default_income_warnings as _silence_default_income_warnings,
)
from tests._robinhood_pipeline_stubs import assert_cg_refused as _assert_cg_refused
from tests._robinhood_pipeline_stubs import stub_cg_run as _stub_cg_run
from tests._robinhood_refusals import ANCHOR_DRIFT_CAUSE
from tests._robinhood_refusals import EVENT_REFERENCES as _REFERENCES
from tests._robinhood_security import configured_securities, robinhood_security

_FY_2025 = date(2025, 4, 1)
_FY_2024 = date(2024, 4, 1)


def _traded(*trades: tuple[date, str, str, str], **kwargs: Any) -> RobinhoodCapitalGainsResult:
    """#1540's managed-account trades, at the ₹85 rate, from 1 January of the
    FY's first year. Trades take managed activity rows 3 onward."""
    start = kwargs.pop("financial_year_start", _FY_2025)
    return _traded_result(
        *trades,
        financial_year_start=start,
        rate=Decimal("85"),
        report_start=date(start.year, 1, 1),
        **kwargs,
    )


def _transferred(
    destination_quantity: str, *trades: tuple[date, str, str, str, date]
) -> RobinhoodCapitalGainsResult:
    """The real CG compute for FY 2024-25 over one $10 buy of 2.0000 shares in
    the individual account (row 0:1) on 10 January 2025, moved in kind to the
    managed account on 12 January (rows 0:2 and 1:1), where the managed
    transfer row shows ``destination_quantity``. ``trades`` are managed sales
    ``(date, quantity, price, Amount, settle date)`` from row 1:2."""
    numbers = ("111111111", "222222222")
    families = (
        RobinhoodStatementFamily.INDIVIDUAL,
        RobinhoodStatementFamily.MANAGED_INDIVIDUAL,
    )
    buy_day, transfer_day, end = date(2025, 1, 10), date(2025, 1, 12), date(2025, 3, 31)
    events: tuple[list[RobinhoodEvent], list[RobinhoodEvent]] = ([], [])
    rows: tuple[list[Any], list[Any]] = ([], [])

    def add(account: int, event: RobinhoodEvent, **row: Any) -> None:
        ordinal = len(events[account]) + 1
        events[account].append(replace(event, source_record_index=ordinal))
        rows[account].append(
            statement_row(
                row=ordinal,
                code=event.trans_code,
                transaction_date=event.activity_date,
                symbol="ABCD",
                cusip="99971T103",
                quantity=event.quantity,
                **row,
            )
        )

    def event(
        kind: RobinhoodEventKind,
        day: date,
        quantity: str,
        code: str,
        *,
        settle: date | None = None,
        suffix: str | None = None,
        price: str | None = None,
        amount: str | None = None,
    ) -> RobinhoodEvent:
        return RobinhoodEvent(
            kind=kind,
            source_record_index=1,
            activity_date=day,
            process_date=day,
            settle_date=settle or day,
            instrument="ABCD",
            description="Synthetic\nCUSIP: 99971T103",
            trans_code=code,
            quantity=Decimal(quantity),
            quantity_suffix=suffix,
            price_usd=None if price is None else Decimal(price),
            amount_usd=None if amount is None else Decimal(amount),
            issuer_name="Synthetic",
            cusip="99971T103",
        )

    add(
        0,
        event(RobinhoodEventKind.EQUITY_BUY, buy_day, "2.0000", "Buy", price="10", amount="-20"),
        price=Decimal(10),
        debit=Decimal(20),
    )
    for account, quantity in enumerate(("2.0000", destination_quantity)):
        transfer = event(
            RobinhoodEventKind.IN_KIND_SECURITY_TRANSFER,
            transfer_day,
            quantity,
            "ITRF",
            settle=date(2025, 1, 13),
            suffix="S" if account == 0 else None,
        )
        add(account, transfer)
        rows[account][-1] = replace(
            rows[account][-1],
            source_account_reference=numbers[0],
            destination_account_reference=numbers[1],
        )
    for day, quantity, price, amount, settle in trades:
        sale = event(
            RobinhoodEventKind.MANAGED_EQUITY_SELL,
            day,
            quantity,
            "Sell",
            settle=settle,
            price=price,
            amount=amount,
        )
        add(1, sale, price=Decimal(price), credit=Decimal(amount))
    held = Decimal(destination_quantity) - sum(
        (Decimal(quantity) for _day, quantity, *_rest in trades), Decimal()
    )
    cash = sum((Decimal(amount) for *_head, amount, _settle in trades), Decimal())
    bundles = tuple(
        statement_bundle(
            account_statement(
                account=numbers[account],
                family=families[account],
                activities=tuple(rows[account]),
                end=end,
                positions=(
                    (statement_position(row=1, symbol="ABCD", quantity=held),)
                    if account == 1 and held
                    else ()
                ),
                closing=(
                    (cash, Decimal(), held * 10, cash + held * 10)
                    if account == 1
                    else (Decimal(), Decimal(), Decimal(), Decimal())
                ),
            )
        )
        for account in range(2)
    )
    reports = tuple(
        RobinhoodActivityReportEvidence(
            events=tuple(events[account]), start_date=ACCOUNT_OPENING, end_date=end
        )
        for account in range(2)
    )
    accounts = tuple(account_config(number) for number in numbers)
    config = RobinhoodConfig(accounts=accounts, securities=(security_config(),))
    return compute_robinhood_capital_gains(
        reconcile_robinhood_accounts(bundles, reports, accounts),
        config=config,
        securities=configured_securities(config),
        financial_year_start=_FY_2024,
        financial_year_end=end,
        fx_policy=AuditedFxStub(Decimal("85")),
    )


# The issue's example: two same-day buys at $10 and $11 (rows 1:3 and 1:4),
# then a sale of half a share (row 1:5).
_BUY_AT_10 = (date(2025, 5, 1), "1.0000", "10.00", "-10.00")
_BUY_AT_11 = (date(2025, 5, 1), "1.0000", "11.00", "-11.00")
_HALF_SALE = (date(2025, 6, 13), "0.5000", "12.00", "6.00")
_SAME_DAY_LINE = f"{_SAME_DAY_CAUSE}{_REFERENCES}sales 1:5 (lots 1:3, 1:4){_SAME_DAY_ROUTE}"
_INHERITED_QUANTITY_LINE = (
    "The activity report doesn't prove how many shares of a security "
    "an account held: a monthly statement shows a position that doesn't match it or "
    "can't be tied to one security, the statement's trade-date or settlement-date quantity "
    "basis can't be proved, or a sale or transfer takes more shares than the report "
    f"shows held. {_REFERENCES}sales 1:4. {FILING_BY_HAND}."
)
_IN_ISIN = "IN9999999" + "99" + isin_check_digit("IN999999999")

_CASES: dict[str, tuple[Callable[[], RobinhoodCapitalGainsResult], str]] = {
    "same-day-lots": (
        lambda: _traded(_BUY_AT_10, _BUY_AT_11, _HALF_SALE, held=Decimal("1.5")),
        _SAME_DAY_LINE,
    ),
    # The managed sale (row 1:2) runs on the day the shares arrive.
    "same-day-transfer": (
        lambda: _transferred(
            "2.0000", (date(2025, 1, 12), "1.0000", "10.00", "10.00", date(2025, 1, 13))
        ),
        f"{_SAME_DAY_CAUSE}{_REFERENCES}sales 1:2{_SAME_DAY_ROUTE}",
    ),
    # The statement shows 0.99999 of the 1.0000 shares the trades leave.
    "anchor-drift": (
        lambda: _traded(
            (date(2025, 5, 1), "2.0000", "10.00", "-20.00"),
            (date(2025, 6, 13), "1.0000", "12.00", "12.00"),
            held=Decimal("0.99999"),
        ),
        f"{ANCHOR_DRIFT_CAUSE.format(moved='sales')} {_REFERENCES}sales 1:4. {FILING_BY_HAND}.",
    ),
    # The statement shows 2 of the 1.0000 shares the trades leave.
    "inherited-quantity-position": (
        lambda: _traded(
            (date(2025, 5, 1), "2.0000", "10.00", "-20.00"),
            (date(2025, 6, 13), "1.0000", "12.00", "12.00"),
            held=Decimal(2),
        ),
        _INHERITED_QUANTITY_LINE,
    ),
    # The sale takes 2 of the 1 share bought.
    "inherited-quantity-oversold": (
        lambda: _traded(
            (date(2025, 5, 1), "1.0000", "10.00", "-10.00"),
            (date(2025, 6, 13), "2.0000", "12.00", "24.00"),
        ),
        _INHERITED_QUANTITY_LINE,
    ),
    # The transfer row shows 2.00004 arriving of the 2.0000 sent; the second
    # sale (row 1:3) takes only those 0.00004, which no lot holds.
    "quantity-shortfall": (
        lambda: _transferred(
            "2.00004",
            (date(2025, 1, 20), "2.0000", "10.00", "20.00", date(2025, 1, 20)),
            (date(2025, 1, 25), "0.00004", "250.00", "0.01", date(2025, 1, 25)),
        ),
        LOT_SHORTFALL_CAUSE.format(references="sales 1:2; sales 1:3"),
    ),
    # FY 2024-25: a sale (row 1:4) the day before the 23 July 2024 cutover.
    "pre-cutover": (
        lambda: _traded(
            (date(2024, 5, 1), "1.0000", "10.00", "-10.00"),
            (date(2024, 7, 22), "1.0000", "12.00", "12.00"),
            financial_year_start=_FY_2024,
        ),
        PRE_CUTOVER_CAUSE.format(sales="1:4 (lots 1:3)"),
    ),
}
_SECURITIES: dict[str, tuple[dict[str, Any], str]] = {
    "bond": ({"asset_class": "BOND"}, 'asset_class isn\'t "STK"'),
    "indian-issuer-receipt": (
        {"issuer_country_code": "IN", "is_depositary_receipt": True},
        'issuer_country_code is "IN"',
    ),
    "indian-isin": ({"isin": _IN_ISIN}, 'ISIN starts with "IN"'),
}
for _name, (_fields, _conditions) in _SECURITIES.items():
    _CASES[f"unsupported-{_name}"] = (
        lambda fields=_fields: _traded(
            (date(2025, 5, 1), "1.0000", "10.00", "-10.00"),
            (date(2025, 6, 13), "1.0000", "12.00", "12.00"),
            security=robinhood_security(
                symbol="ABCD", cusip="99971T103", source="synthetic evidence", **fields
            ),
        ),
        UNSUPPORTED_CLASS_CAUSE.format(sales="1:4 (lots 1:3)", conditions=_conditions),
    )


@pytest.mark.parametrize(("make_cg", "line"), _CASES.values(), ids=_CASES.keys())
def test_each_reachable_blocker_names_its_sales_cause_and_route(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    make_cg: Callable[[], RobinhoodCapitalGainsResult],
    line: str,
) -> None:
    """robinhood-cg exits 4 as on 1f29b0b, writes nothing, and prints the
    blocker's line after the fixed one; robinhood-all's equity-cg leg fails
    with the same text. On 1f29b0b both printed the fixed line alone."""
    _assert_cg_refused(tmp_path, monkeypatch, capsys, make_cg, f"{_CG}\n{line}")


def test_following_the_same_day_refusal_files_the_sale_by_hand(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Follow the issue's same-day refusal word for word. schedule-cg.xlsx
    isn't written. Its references are the sale row and the two same-day buy
    rows; matching the lots first in, first out, in either order a trade
    confirmation can give, calculates manual entries for the half share at
    ₹510 consideration against a cost of ₹425 (the $10 lot first) or
    ₹467.50 (the $11 lot)."""
    _stub_cg_run(monkeypatch, _CASES["same-day-lots"][0])
    assert _main("robinhood-cg", tmp_path) == 4
    refusal = capsys.readouterr().err

    assert "schedule-cg.xlsx isn't written while this stands" in refusal
    assert not (tmp_path / "out" / "schedule-cg.xlsx").exists()
    [(sales, lots)] = re.findall(r"sales ([\d:, ]+) \(lots ([\d:, ]+)\)", refusal)
    rows = dict(zip(("1:3", "1:4", "1:5"), (_BUY_AT_10, _BUY_AT_11, _HALF_SALE), strict=True))
    [(_sold, quantity, _price, amount)] = [rows[item] for item in sales.split(", ")]
    lot_rows = [rows[item] for item in lots.split(", ")]
    assert {row[0] for row in lot_rows} == {date(2025, 5, 1)}
    filed = set()
    for executed in itertools.permutations(lot_rows):
        # First in, first out: the earlier-executed lot covers the whole sale.
        _bought, lot_quantity, _lot_price, cost = executed[0]
        assert Decimal(quantity) <= Decimal(lot_quantity)
        share = Decimal(quantity) / Decimal(lot_quantity)
        filed.add((Decimal(amount) * 85, -Decimal(cost) * share * 85))
    assert filed == {(Decimal(510), Decimal(425)), (Decimal(510), Decimal("467.5"))}


def test_lines_follow_kind_order_and_name_each_causes_own_lots() -> None:
    """One line per kind present, in kind order, and for lot-attribution one
    per lot-engine cause; a group with two causes is named under each, with
    that cause's lots only. Identity and evidence-coverage add no line."""
    ref = RobinhoodSecurityEventReference

    def lot_blocker(kind: RobinhoodLotBlockerKind, *lots: int) -> RobinhoodLotBlocker:
        return RobinhoodLotBlocker(
            account_index=0, cusip=None, kind=kind, lot_ids=tuple(ref(0, lot) for lot in lots)
        )

    def blocker(kind: RobinhoodCapitalGainsBlockerKind, sale: int, **fields: Any) -> Any:
        return RobinhoodCapitalGainsBlocker(
            kind=kind, account_index=0, disposal_references=(ref(0, sale),), **fields
        )

    kinds = RobinhoodCapitalGainsBlockerKind
    reasons = cg_mod.blocker_reasons(
        (
            blocker(
                kinds.UNSUPPORTED_SECURITY_CLASS,
                9,
                lot_ids=(ref(0, 8),),
                unsupported_conditions=tuple(cg_mod.RobinhoodUnsupportedSecurityCondition),
            ),
            blocker(kinds.PRE_CUTOVER_HOLDING_RULE, 7, lot_ids=(ref(0, 6),)),
            blocker(
                kinds.LOT_ATTRIBUTION,
                5,
                lot_ids=(ref(0, 1), ref(0, 2)),
                source_lot_blockers=(
                    lot_blocker(RobinhoodLotBlockerKind.SAME_DAY_AMBIGUITY, 2),
                    lot_blocker(RobinhoodLotBlockerKind.QUANTITY_SHORTFALL),
                ),
            ),
            blocker(
                kinds.LOT_ATTRIBUTION,
                4,
                source_lot_blockers=(lot_blocker(RobinhoodLotBlockerKind.QUANTITY_SHORTFALL),),
            ),
            blocker(kinds.IDENTITY, 3),
            blocker(kinds.EVIDENCE_COVERAGE, 3),
        )
    )

    assert [reason.split(_REFERENCES)[1].split(". ")[0] for reason in reasons] == [
        "sales 0:5; sales 0:4",
        "sales 0:5 (lots 0:2)",
        "sales 0:7 (lots 0:6)",
        'sales 0:9 (lots 0:8): asset_class isn\'t "STK", issuer_country_code is "IN", '
        'ISIN starts with "IN"',
    ]
    assert reasons[0].startswith("The lots from the activity report")
    assert reasons[1].startswith(_SAME_DAY_CAUSE)
