"""Focused checks for the two-account Robinhood Schedule FA adapter."""

from __future__ import annotations

import csv
import inspect
from collections import defaultdict
from collections.abc import Callable
from dataclasses import replace
from datetime import date, timedelta
from decimal import ROUND_HALF_UP, Context, Decimal, localcontext
from fractions import Fraction
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast, override

import pytest

import ibkr_fa.compute.robinhood_fa as robinhood_fa_mod
import tests._robinhood_fa_inputs as fa_inputs
from ibkr_fa.compute.dec31_close import ClosingPriceError, ResolvedClose
from ibkr_fa.compute.robinhood_a2_peak import RobinhoodA2Peak, RobinhoodA2PeakError
from ibkr_fa.compute.robinhood_capital_gains import compute_robinhood_capital_gains
from ibkr_fa.compute.robinhood_fa import (
    RobinhoodFaBlockerKind,
    RobinhoodFaManualKind,
    RobinhoodFaManualWork,
    _a2_gross_evidence,
    _dedupe_manual,
    _group_disposal_slices,
    _option_manual_work,
    _peak_floor,
    compute_robinhood_fa,
)
from ibkr_fa.compute.robinhood_income import (
    RobinhoodDividendPayment,
    RobinhoodIncomeBlocker,
    RobinhoodIncomeBlockerKind,
    RobinhoodIncomeEventReference,
    RobinhoodInterestPayment,
    RobinhoodStockLendingPayment,
    RobinhoodStockLendingSubtype,
)
from ibkr_fa.compute.robinhood_lots import (
    RobinhoodLotHoldingInterval,
    RobinhoodLotSlice,
    RobinhoodLotSliceKind,
    RobinhoodSecurityLotResult,
)
from ibkr_fa.compute.robinhood_options import (
    RobinhoodOptionBlocker,
    RobinhoodOptionBlockerKind,
    RobinhoodOptionContract,
    RobinhoodOptionFaDisposition,
)
from ibkr_fa.config import (
    RobinhoodAccountConfig,
    RobinhoodConfig,
    RobinhoodOptionTreatment,
    RobinhoodSecurityReceiptResolution,
    RobinhoodSecurityReceiptTreatment,
)
from ibkr_fa.exceptions import IbkrFaError, InvariantError
from ibkr_fa.fx.policy import FxPolicy
from ibkr_fa.invariants import invariant_error
from ibkr_fa.models import ClosedLot, OpenLot
from ibkr_fa.models.fa_a2_row import FaA2NatureOfAmount
from ibkr_fa.models.interest_credit import InterestCredit
from ibkr_fa.models.other_source_income import OtherSourceIncome
from ibkr_fa.normalize.robinhood import (
    RobinhoodAccountsReconciliation,
    RobinhoodActivityReportEvidence,
    RobinhoodPositionSnapshotBasis,
    RobinhoodQuantityMovement,
    RobinhoodQuantityMovementKind,
    RobinhoodReconciliationError,
    RobinhoodSecurityEventReference,
    reconcile_robinhood_accounts,
    resolve_robinhood_security_identities,
    resolve_robinhood_security_quantities,
    validate_robinhood_statement_bundles,
)
from ibkr_fa.output._cleanup import ClaimedOutputCleanup
from ibkr_fa.parser.robinhood import RobinhoodEvent, RobinhoodEventKind
from ibkr_fa.parser.robinhood_statement import (
    RobinhoodStatement,
    RobinhoodStatementFamily,
    RobinhoodStatementSection,
    RobinhoodStockLoanActivity,
    RobinhoodStockLoanedSecurity,
    RobinhoodStockLoanKind,
    SourceLocation,
)
from ibkr_fa.pipeline import FinancialYear
from ibkr_fa.pipeline_robinhood_outputs import JSON_FILENAME, write_fa
from ibkr_fa.prices.series import CONTEMPORANEOUS_BASIS, PriceSeries
from ibkr_fa.reference import us_market_calendar
from ibkr_fa.reference.us_market_calendar import previous_us_market_session
from ibkr_fa.types import CountryCode, Isin, Ticker
from tests._crash_reports import assert_prints_no_marker, assert_unchained_refusal
from tests._dec31_close import (
    MARKER_PRICE,
    MARKER_SOURCE_DATE,
    MARKET_SESSION_REFUSAL,
    assert_markers_absent,
)
from tests._exact_refusals import INEXACT_PRODUCT, TOO_MANY_DIGITS
from tests._fa_a2_peak import without_peak
from tests._fa_exact_sums import HUGE, MINUTE, assert_refusal, message
from tests._fx_stubs import AuditedFxStub
from tests._private_checks import private_test
from tests._robinhood_a3_exact import (
    DESCRIPTION,
    DIGIT_RUN,
    MARKERS,
    SYMBOL,
    TINY_QUANTITY,
    share_refusal,
)
from tests._robinhood_anchors import stand_in_rounded_anchor_ledgers
from tests._robinhood_fa_inputs import FA_CUSIP as _CUSIP
from tests._robinhood_fa_inputs import FA_END as _END
from tests._robinhood_fa_inputs import FA_ISIN as _ISIN
from tests._robinhood_fa_inputs import FA_OPENING as _OPENING
from tests._robinhood_fa_inputs import FA_SYMBOL as _SYMBOL
from tests._robinhood_fa_inputs import FlatFx as _FlatFx
from tests._robinhood_fa_inputs import fa_account as _account
from tests._robinhood_fa_inputs import fa_anchor_only_options as _anchor_only_options
from tests._robinhood_fa_inputs import fa_buy as _buy
from tests._robinhood_fa_inputs import fa_cash as _cash
from tests._robinhood_fa_inputs import fa_config as _config
from tests._robinhood_fa_inputs import fa_dividend as _dividend
from tests._robinhood_fa_inputs import fa_entity as _entity
from tests._robinhood_fa_inputs import fa_income as _income
from tests._robinhood_fa_inputs import fa_options as _options
from tests._robinhood_fa_inputs import fa_paired as _paired
from tests._robinhood_fa_inputs import fa_paired_events as _paired_events
from tests._robinhood_fa_inputs import fa_price_series as _prices
from tests._robinhood_fa_inputs import fa_sale as _sale
from tests._robinhood_fa_inputs import fa_same_day_sale as _same_day_sale
from tests._robinhood_fa_inputs import fa_security as _security
from tests._robinhood_fa_inputs import fa_statement as _statement
from tests._robinhood_fa_inputs import fa_summary as _summary
from tests._robinhood_fa_runs import DEC30 as _DEC30
from tests._robinhood_fa_runs import DEC31_SESSION_REFUSALS as _SESSION_REFUSALS
from tests._robinhood_fa_runs import Fx8250 as _Fx8250
from tests._robinhood_fa_runs import a3_peak_and_closing_cells as _a3_cells
from tests._robinhood_fa_runs import (
    compute_fa_one_dollar_three_shares as _compute_one_dollar_three_shares,
)
from tests._robinhood_fa_runs import compute_fa_open as _compute_open
from tests._robinhood_fa_runs import compute_fa_statement_total as _compute_statement_total
from tests._robinhood_lots import lot_acquisition as _lot_acquisition
from tests._robinhood_lots import lot_movement as _lot_movement
from tests._robinhood_lots import lot_quantity_result as _lot_result
from tests._robinhood_lots import lot_transfer as _lot_transfer
from tests._robinhood_pending import MARKERS as PENDING_MARKERS
from tests._robinhood_pending import (
    TRADED,
    dec31_trade,
    forge_settlement_anchors,
    pending_row,
)
from tests._robinhood_private import (
    load_private_accounts_reconciliation,
)
from tests._robinhood_refusals import (
    EQUITY_COVERAGE_REFUSAL,
    INDIA_REFUSAL,
    NEGATIVE_POSITION_REFUSAL,
)
from tests._robinhood_security import configured_securities, robinhood_security
from tests._robinhood_statements import robinhood_account_config


class _ClosingDateFx(FxPolicy):
    currency = "USD"

    @override
    def rate_for(self, event_date: date) -> Decimal:
        return Decimal("90") if event_date == _END else Decimal("80")


def _security_transfer(
    ordinal: int,
    *,
    source: bool,
) -> RobinhoodEvent:
    return RobinhoodEvent(
        kind=RobinhoodEventKind.IN_KIND_SECURITY_TRANSFER,
        source_record_index=ordinal,
        activity_date=date(2025, 6, 1),
        process_date=date(2025, 6, 1),
        settle_date=date(2025, 6, 2),
        instrument=_SYMBOL,
        description=f"Synthetic transfer\nCUSIP: {_CUSIP}",
        trans_code="ITRF",
        quantity=Decimal("2"),
        quantity_suffix="S" if source else None,
        price_usd=None,
        amount_usd=None,
        issuer_name="Synthetic Corporation",
        cusip=_CUSIP,
    )


def _paired_transfer():
    outgoing = _security_transfer(3, source=True)
    incoming = _security_transfer(2, source=False)
    first_events = (_buy(), outgoing)
    second_events = (incoming,)
    first = _statement(
        "111111111",
        RobinhoodStatementFamily.INDIVIDUAL,
        first_events,
        cash=Decimal("-20"),
        position_quantity=Decimal(),
    )
    second = _statement(
        "222222222",
        RobinhoodStatementFamily.MANAGED_INDIVIDUAL,
        second_events,
        cash=Decimal(),
        position_quantity=Decimal("2"),
    )
    first = replace(
        first,
        activities=(
            first.activities[0],
            replace(
                first.activities[1],
                settle_date=None,
                source_account_reference="111111111",
                destination_account_reference="222222222",
            ),
        ),
    )
    second = replace(
        second,
        activities=(
            replace(
                second.activities[0],
                settle_date=None,
                source_account_reference="111111111",
                destination_account_reference="222222222",
            ),
        ),
    )
    bundles = validate_robinhood_statement_bundles((first, second))
    reports = tuple(
        RobinhoodActivityReportEvidence(
            events=events,
            start_date=_OPENING,
            end_date=_END,
        )
        for events in (first_events, second_events)
    )
    accounts = (_account("111111111"), _account("222222222"))
    return reconcile_robinhood_accounts(bundles, reports, accounts), accounts


def _paired_with_2026_sale(
    buy: RobinhoodEvent,
    sold: date,
    first_statements: Callable[[RobinhoodEvent], tuple[RobinhoodStatement, RobinhoodStatement]],
):
    """Account 111111111 buys ``buy`` and sells both shares for USD 30 on
    ``sold``; ``first_statements`` builds its two statements from that sale.
    Account 222222222 holds its USD 5 deposit through 31 March 2026."""
    sale = replace(
        _sale(),
        source_record_index=3,
        activity_date=sold,
        process_date=sold,
        settle_date=sold,
        quantity=Decimal("2"),
        amount_usd=Decimal("30"),
    )
    deposit = _cash()
    other_first = _statement(
        "222222222",
        RobinhoodStatementFamily.MANAGED_INDIVIDUAL,
        (deposit,),
        cash=Decimal("5"),
        position_quantity=Decimal(),
    )
    other_second = _statement(
        "222222222",
        RobinhoodStatementFamily.MANAGED_INDIVIDUAL,
        (),
        cash=Decimal("5"),
        position_quantity=Decimal(),
        start=date(2026, 1, 1),
        end=date(2026, 3, 31),
        opening_cash=Decimal("5"),
        opening_securities=Decimal(),
    )
    bundles = validate_robinhood_statement_bundles(
        (*first_statements(sale), other_first, other_second)
    )
    reports = (
        RobinhoodActivityReportEvidence(
            events=(buy, sale),
            start_date=_OPENING,
            end_date=date(2026, 3, 31),
        ),
        RobinhoodActivityReportEvidence(
            events=(deposit,),
            start_date=_OPENING,
            end_date=date(2026, 3, 31),
        ),
    )
    accounts = (_account("111111111"), _account("222222222"))
    return reconcile_robinhood_accounts(bundles, reports, accounts), accounts


def _paired_future_sale():
    buy = _buy()
    return _paired_with_2026_sale(
        buy,
        date(2026, 1, 10),
        lambda sale: (
            _statement(
                "111111111",
                RobinhoodStatementFamily.INDIVIDUAL,
                (buy,),
                cash=Decimal("-20"),
                position_quantity=Decimal("2"),
            ),
            _statement(
                "111111111",
                RobinhoodStatementFamily.INDIVIDUAL,
                (sale,),
                cash=Decimal("10"),
                position_quantity=Decimal(),
                start=date(2026, 1, 1),
                end=date(2026, 3, 31),
                opening_cash=Decimal("-20"),
                opening_securities=Decimal("30"),
            ),
        ),
    )


def _paired_next_year_round_trip():
    buy = replace(
        _buy(),
        activity_date=date(2026, 1, 10),
        process_date=date(2026, 1, 10),
        settle_date=date(2026, 1, 10),
    )
    return _paired_with_2026_sale(
        buy,
        date(2026, 2, 10),
        lambda sale: (
            _statement(
                "111111111",
                RobinhoodStatementFamily.INDIVIDUAL,
                (),
                cash=Decimal(),
                position_quantity=Decimal(),
            ),
            _statement(
                "111111111",
                RobinhoodStatementFamily.INDIVIDUAL,
                (buy, sale),
                cash=Decimal("10"),
                position_quantity=Decimal(),
                start=date(2026, 1, 1),
                end=date(2026, 3, 31),
                opening_cash=Decimal(),
                opening_securities=Decimal(),
            ),
        ),
    )


@pytest.mark.parametrize(
    ("sub_category", "depositary", "name", "identity_task"),
    [
        pytest.param("ETF", False, None, True, id="etf-unnamed"),
        pytest.param("ETF", False, "Reviewed Trust - Reviewed ETF", False, id="etf-named"),
        pytest.param("ETF", True, None, False, id="depositary-receipt"),
        pytest.param("COMMON", False, None, False, id="common-stock"),
    ],
)
def test_only_an_unnamed_etf_needs_its_legal_name_filled(
    sub_category: str,
    depositary: bool,
    name: str | None,
    identity_task: bool,
) -> None:
    """#793: an ETF's broker description does not complete its legal identity."""
    paired, accounts = _paired(sold=False, dividend=False)
    security = replace(_security(), sub_category=sub_category, is_depositary_receipt=depositary)
    config = replace(_config(accounts), securities=(security,))

    result = compute_robinhood_fa(
        paired,
        config=config,
        securities=configured_securities(config),
        income=_income(dividend=False),
        options=_options(),
        calendar_year=2025,
        fx_policy=_FlatFx(),
        prices_by_ticker=_prices(),
        entity_overrides={_ISIN: replace(_entity()[_ISIN], name=name)},
    )

    assert {item.kind for item in result.manual_work} == (
        {RobinhoodFaManualKind.A3_IDENTITY} if identity_task else set()
    )
    assert result.cbdt_json_eligible is not identity_task


@pytest.mark.parametrize(
    ("issuer", "name", "identity_task"),
    [
        pytest.param("   ", None, True, id="spaces"),
        pytest.param("\u00a0", None, True, id="no-break-space"),
        pytest.param("   ", "Synthetic Corporation", False, id="configured-name"),
        pytest.param("Synthetic Corporation", None, False, id="issuer-name"),
    ],
)
def test_a_whitespace_only_name_needs_its_legal_name_filled(
    issuer: str, name: str | None, *, identity_task: bool
) -> None:
    """#1369: a whitespace-only issuer name with no configured name leaves the
    A3 Name of entity blank, so the security gets one ``a3-identity`` task
    over both of its lots, and the run writes no CBDT JSON. A configured name
    fills the cell."""
    first = replace(_buy(quantity="1"), issuer_name=issuer)
    second = replace(first, source_record_index=3)
    paired, accounts = _paired_events((first, second), position_quantity=Decimal("2"))
    config = _config(accounts)

    result = compute_robinhood_fa(
        paired,
        config=config,
        securities=configured_securities(config),
        income=_income(dividend=False),
        options=_options(),
        calendar_year=2025,
        fx_policy=_FlatFx(),
        prices_by_ticker=_prices(),
        entity_overrides={_ISIN: replace(_entity()[_ISIN], name=name)},
    )

    assert [row.name_of_entity for row in result.a3_rows] == [name or issuer] * 2
    tasks = [item for item in result.manual_work if item.kind is RobinhoodFaManualKind.A3_IDENTITY]
    if identity_task:
        assert len(tasks) == 1
        assert tasks[0].security_config_indices == (0,)
        assert tasks[0].lot_ids == tuple(sorted(review.lot_id for review in result.a3))
    else:
        assert tasks == []
    assert result.cbdt_json_eligible is not identity_task


def test_identity_manual_work_is_grouped_per_security_with_all_lots() -> None:
    first = RobinhoodSecurityEventReference(0, 2)
    second = RobinhoodSecurityEventReference(1, 7)
    peak = RobinhoodFaManualWork(
        kind=RobinhoodFaManualKind.A3_PEAK,
        security_config_indices=(3,),
        lot_ids=(first,),
    )

    result = _dedupe_manual(
        (
            RobinhoodFaManualWork(
                kind=RobinhoodFaManualKind.A3_IDENTITY,
                security_config_indices=(3,),
                lot_ids=(second,),
            ),
            peak,
            RobinhoodFaManualWork(
                kind=RobinhoodFaManualKind.A3_IDENTITY,
                security_config_indices=(3,),
                lot_ids=(first,),
            ),
        )
    )

    assert peak in result
    identity = next(item for item in result if item.kind is RobinhoodFaManualKind.A3_IDENTITY)
    assert identity.security_config_indices == (3,)
    assert identity.lot_ids == (first, second)


def _loan_location(section: RobinhoodStatementSection, row: int) -> SourceLocation:
    return SourceLocation(page=3, section=section, row=row)


def _loan_snapshot(shares: str) -> RobinhoodStockLoanedSecurity:
    quantity = Decimal(shares)
    return RobinhoodStockLoanedSecurity(
        location=_loan_location(RobinhoodStatementSection.STOCK_LOANED_SECURITIES, 1),
        description="Synthetic loaned security",
        symbol=_SYMBOL,
        shares_lent=quantity,
        price_usd=Decimal("10"),
        market_value_usd=quantity * Decimal("10"),
    )


def _loan_activity(
    day: date,
    kind: RobinhoodStockLoanKind,
    shares: str,
    *,
    row: int = 1,
) -> RobinhoodStockLoanActivity:
    return RobinhoodStockLoanActivity(
        location=_loan_location(RobinhoodStatementSection.STOCK_LOAN_ACTIVITY, row),
        description="Synthetic loan activity",
        symbol=_SYMBOL,
        transaction_date=day,
        kind=kind,
        shares=Decimal(shares),
    )


def _delayed_fee_fixture(
    *,
    payment_date: date = date(2025, 7, 7),
    source_account_index: int = 0,
    lend_after_close: bool = False,
    extra_fragment: bool = False,
):
    paired, accounts = _paired(sold=True, dividend=False)
    first = paired.accounts[0]
    sale = next(
        event for event in first.evidence.events if event.kind is RobinhoodEventKind.EQUITY_SELL
    )
    settled_sale = replace(sale, settle_date=date(2025, 6, 11))
    evidence = replace(
        first.evidence,
        events=tuple(
            settled_sale if event.source_record_index == sale.source_record_index else event
            for event in first.evidence.events
        ),
    )
    previous = replace(
        _statement(
            "111111111",
            RobinhoodStatementFamily.INDIVIDUAL,
            (),
            cash=Decimal(),
            position_quantity=Decimal("2"),
            start=date(2025, 5, 1),
            end=date(2025, 5, 31),
        ),
        stock_loaned_securities=(() if lend_after_close else (_loan_snapshot("2"),)),
    )
    current = replace(
        _statement(
            "111111111",
            RobinhoodStatementFamily.INDIVIDUAL,
            (),
            cash=Decimal(),
            position_quantity=Decimal(),
            start=date(2025, 6, 1),
            end=date(2025, 6, 30),
        ),
        stock_loaned_securities=((_loan_snapshot("2"),) if lend_after_close else ()),
        stock_loan_activity=(
            (
                _loan_activity(date(2025, 6, 11), RobinhoodStockLoanKind.LEND, "2")
                if lend_after_close
                else _loan_activity(date(2025, 6, 11), RobinhoodStockLoanKind.RETURN, "2")
            ),
        ),
    )
    source_bundle = replace(
        first.source_bundle,
        coverage_start=previous.period_start,
        coverage_end=current.period_end,
        statements=(previous, current),
    )
    paired = replace(
        paired,
        accounts=(
            replace(first, evidence=evidence, source_bundle=source_bundle),
            paired.accounts[1],
        ),
    )
    lot_id = RobinhoodSecurityEventReference(0, 2)
    sale_id = RobinhoodSecurityEventReference(0, 4)
    fragment = robinhood_fa_mod._Fragment(
        key=robinhood_fa_mod._FragmentKey(lot_id, (sale_id,)),
        security_config_index=0,
        quantity=Decimal("2"),
        cost_usd=Decimal("20"),
        open_date=date(2025, 1, 10),
        close_date=date(2025, 6, 10),
        account_indices=(0,),
        holdings=(robinhood_fa_mod._Holding(0, date(2025, 1, 10), Decimal("2")),),
        net_proceeds_usd=Decimal("14.99"),
    )
    fragments = [fragment]
    intervals = [
        RobinhoodLotHoldingInterval(
            lot_id=lot_id,
            account_index=0,
            cusip=_CUSIP,
            from_date=date(2025, 1, 10),
            to_date=date(2025, 6, 10),
            quantity=Decimal("2"),
        )
    ]
    if extra_fragment:
        other_id = RobinhoodSecurityEventReference(0, 9)
        fragments.append(
            robinhood_fa_mod._Fragment(
                key=robinhood_fa_mod._FragmentKey(other_id),
                security_config_index=0,
                quantity=Decimal("1"),
                cost_usd=Decimal("10"),
                open_date=date(2025, 5, 1),
                close_date=None,
                account_indices=(0,),
                holdings=(robinhood_fa_mod._Holding(0, date(2025, 5, 1), Decimal("1")),),
            )
        )
        intervals.append(
            RobinhoodLotHoldingInterval(
                lot_id=other_id,
                account_index=0,
                cusip=_CUSIP,
                from_date=date(2025, 5, 1),
                to_date=None,
                quantity=Decimal("1"),
            )
        )
    lots = RobinhoodSecurityLotResult(
        slices=(),
        open_fragments=(),
        holding_intervals=tuple(intervals),
        blockers=(),
        identity_blockers=(),
    )
    credit = robinhood_fa_mod._Credit(
        reference=RobinhoodIncomeEventReference(source_account_index, 99),
        security_config_index=0,
        holding_date=payment_date,
        credit_date=payment_date,
        amount_usd=Decimal("0.10"),
        source_quantity=None,
        distribution_rate_usd=None,
        symbol=_SYMBOL,
        monthly_loan_fee=True,
    )
    return paired, _config(accounts).securities, tuple(fragments), lots, credit


def test_delayed_loan_fee_uses_exact_statement_loan_activity() -> None:
    paired, securities, fragments, lots, credit = _delayed_fee_fixture()

    resolved = robinhood_fa_mod._delayed_loan_fee_fragment(
        fragments,
        (),
        lots,
        paired,
        securities,
        [credit],
        2025,
    )

    assert resolved == fragments[0]


def test_delayed_loan_fee_allocation_uses_calendar_year_closing_fx(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    paired, securities, fragments, lots, credit = _delayed_fee_fixture()
    monkeypatch.setattr(robinhood_fa_mod, "_source_credits", lambda *_args: (credit,))

    usd, inr, references, accrual_references, blockers = robinhood_fa_mod._allocate_credits(
        fragments,
        (),
        cast("Any", SimpleNamespace(schedule_os_complete=True)),
        paired,
        securities,
        lots,
        2025,
        _ClosingDateFx(),
    )

    assert not blockers
    assert usd[fragments[0].key] == credit.amount_usd
    assert inr[fragments[0].key] == credit.amount_usd * Decimal("90")
    assert references[fragments[0].key] == (credit.reference,)
    assert accrual_references[fragments[0].key] == (credit.reference,)


def test_substitute_payment_never_uses_delayed_loan_fee_fallback() -> None:
    paired, securities, fragments, lots, credit = _delayed_fee_fixture()

    assert (
        robinhood_fa_mod._delayed_loan_fee_fragment(
            fragments,
            (),
            lots,
            paired,
            securities,
            [replace(credit, monthly_loan_fee=False)],
            2025,
        )
        is None
    )


def test_settlement_carry_still_requires_paying_account_ownership() -> None:
    paired, securities, fragments, lots, credit = _delayed_fee_fixture()
    wrong_fragment = replace(fragments[0], account_indices=(1,))
    wrong_lots = replace(
        lots,
        holding_intervals=tuple(replace(item, account_index=1) for item in lots.holding_intervals),
    )

    assert (
        robinhood_fa_mod._delayed_loan_fee_fragment(
            (wrong_fragment,),
            (),
            wrong_lots,
            paired,
            securities,
            [credit],
            2025,
        )
        is None
    )


def test_delayed_loan_fee_refuses_incomplete_same_security_fragment_universe() -> None:
    paired, securities, fragments, lots, credit = _delayed_fee_fixture()
    blocker = robinhood_fa_mod.RobinhoodFaBlocker(
        kind=RobinhoodFaBlockerKind.A3_DISPOSAL,
        security_config_indices=(0,),
        lot_ids=(fragments[0].key.lot_id,),
    )

    assert (
        robinhood_fa_mod._delayed_loan_fee_fragment(
            fragments,
            (blocker,),
            lots,
            paired,
            securities,
            [credit],
            2025,
        )
        is None
    )


@pytest.mark.parametrize(
    ("payment_date", "source_account_index", "lend_after_close", "extra_fragment"),
    [
        (date(2025, 7, 9), 0, False, False),
        (date(2025, 7, 7), 1, False, False),
        (date(2025, 7, 7), 0, True, False),
        (date(2025, 7, 7), 0, False, True),
    ],
    ids=["after-fifth-session", "wrong-account", "new-lending-after-close", "multiple-fragments"],
)
def test_delayed_loan_fee_refuses_ambiguous_evidence(
    payment_date: date,
    source_account_index: int,
    lend_after_close: bool,
    extra_fragment: bool,
) -> None:
    paired, securities, fragments, lots, credit = _delayed_fee_fixture(
        payment_date=payment_date,
        source_account_index=source_account_index,
        lend_after_close=lend_after_close,
        extra_fragment=extra_fragment,
    )

    assert (
        robinhood_fa_mod._delayed_loan_fee_fragment(
            fragments,
            (),
            lots,
            paired,
            securities,
            [credit],
            2025,
        )
        is None
    )


def test_payment_date_multi_fragment_loan_fee_still_prorates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    paired, accounts = _paired(sold=False, dividend=False)
    first_id = RobinhoodSecurityEventReference(0, 2)
    second_id = RobinhoodSecurityEventReference(0, 9)
    fragments = (
        robinhood_fa_mod._Fragment(
            key=robinhood_fa_mod._FragmentKey(first_id),
            security_config_index=0,
            quantity=Decimal("1"),
            cost_usd=Decimal("10"),
            open_date=date(2025, 1, 10),
            close_date=None,
            account_indices=(0,),
            holdings=(robinhood_fa_mod._Holding(0, date(2025, 1, 10), Decimal("1")),),
        ),
        robinhood_fa_mod._Fragment(
            key=robinhood_fa_mod._FragmentKey(second_id),
            security_config_index=0,
            quantity=Decimal("1"),
            cost_usd=Decimal("10"),
            open_date=date(2025, 1, 10),
            close_date=None,
            account_indices=(0,),
            holdings=(robinhood_fa_mod._Holding(0, date(2025, 1, 10), Decimal("1")),),
        ),
    )
    credit = robinhood_fa_mod._Credit(
        reference=RobinhoodIncomeEventReference(0, 99),
        security_config_index=0,
        holding_date=date(2025, 3, 10),
        credit_date=date(2025, 3, 10),
        amount_usd=Decimal("1"),
        source_quantity=None,
        distribution_rate_usd=None,
        symbol=_SYMBOL,
        monthly_loan_fee=True,
    )
    monkeypatch.setattr(robinhood_fa_mod, "_source_credits", lambda *_args: (credit,))

    usd, _inr, references, accrual_references, blockers = robinhood_fa_mod._allocate_credits(
        fragments,
        (),
        cast("Any", SimpleNamespace(schedule_os_complete=True)),
        paired,
        _config(accounts).securities,
        RobinhoodSecurityLotResult((), (), (), (), ()),
        2025,
        _FlatFx(),
    )

    assert not blockers
    assert sum((value or Decimal() for value in usd.values()), Decimal()) == Decimal("1")
    assert all(value == Decimal("0.5") for value in usd.values())
    assert all(references[item.key] == (credit.reference,) for item in fragments)
    assert not any(accrual_references.values())


def test_a2_rows_split_credit_natures_without_losing_sale_fragments() -> None:
    paired, accounts = _paired(sold=True, dividend=True)
    result = compute_robinhood_fa(
        paired,
        config=_config(accounts),
        securities=configured_securities(_config(accounts)),
        income=_income(dividend=True),
        options=_options(),
        calendar_year=2025,
        fx_policy=_FlatFx(),
        prices_by_ticker=_prices(),
        entity_overrides=_entity(),
    )

    assert len(result.a2_rows) == 3
    assert {row.account_number for row in result.a2_rows} == {
        "111111111",
        "222222222",
    }
    assert {
        (row.nature_of_amount, row.gross_amount_inr)
        for row in result.a2_rows
        if row.account_number == "111111111"
    } == {
        (FaA2NatureOfAmount.DIVIDEND, Decimal("800")),
        (FaA2NatureOfAmount.PROCEEDS, Decimal("1199.20")),
    }
    first = next(item for item in result.a2 if item.row.account_number == "111111111")
    assert first.row.closing_balance_inr == Decimal("1599.20")
    assert first.row.peak_balance_inr == Decimal("1600")
    assert first.unresolved_gross_natures == ()
    assert first.known_gross_components_inr == {
        FaA2NatureOfAmount.DIVIDEND.value: Decimal("800"),
        FaA2NatureOfAmount.PROCEEDS.value: Decimal("1199.20"),
    }

    assert len(result.a3) == 2
    assert sum(item.row.initial_value_inr for item in result.a3) == Decimal("1600")
    assert sorted(item.gross_credit_usd for item in result.a3) == [
        Decimal("5"),
        Decimal("5"),
    ]
    closed = next(item for item in result.a3 if item.row.audit_disposal_date is not None)
    opened = next(item for item in result.a3 if item.row.audit_disposal_date is None)
    assert closed.row.gross_proceeds_from_sale_inr == Decimal("1199.20")
    assert opened.row.closing_value_inr == Decimal("1200")
    assert result.cbdt_json_eligible is True


def test_a2_rows_equal_the_rows_built_with_replace() -> None:
    # #619 characterization: each row equals the build_fa_a2_row result that
    # replace() then gave the computed peak, its USD/TTBR and the statement floor.
    paired, accounts = _paired(sold=True, dividend=True)
    result = compute_robinhood_fa(
        paired,
        config=_config(accounts),
        securities=configured_securities(_config(accounts)),
        income=_income(dividend=True),
        options=_options(),
        calendar_year=2025,
        fx_policy=_FlatFx(),
        prices_by_ticker=_prices(),
        entity_overrides=_entity(),
    )
    peaks = {
        "111111111": (Decimal("1600"), date(2025, 3, 15), Decimal("20"), Decimal("19.99")),
        "222222222": (Decimal("400"), date(2025, 1, 11), Decimal("5"), Decimal("5")),
    }

    assert len(result.a2_rows) == 3
    for row in result.a2_rows:
        peak_inr, peak_date, peak_usd, floor_usd = peaks[row.account_number]
        assert row == replace(
            without_peak(row),
            peak_balance_inr=peak_inr,
            audit_peak_balance_lower_inr=peak_inr,
            audit_peak_balance_upper_inr=peak_inr,
            audit_peak_balance_lower_date=peak_date,
            audit_peak_balance_upper_date=peak_date,
            audit_peak_balance_bound="lower",
            audit_peak_balance_base_ccy=peak_usd,
            audit_peak_ttbr=Decimal("80"),
            audit_statement_peak_floor_base_ccy=floor_usd,
            audit_statement_peak_floor_date=date(2025, 12, 31),
        )


def test_all_fa_gross_credits_use_dec31_rate() -> None:
    paired, accounts = _paired(sold=True, dividend=True)
    result = compute_robinhood_fa(
        paired,
        config=_config(accounts),
        securities=configured_securities(_config(accounts)),
        income=_income(dividend=True),
        options=_options(),
        calendar_year=2025,
        fx_policy=_ClosingDateFx(),
        prices_by_ticker=_prices(),
        entity_overrides=_entity(),
    )

    first = next(item for item in result.a2 if item.row.account_number == "111111111")
    assert first.known_gross_components_inr == {
        FaA2NatureOfAmount.DIVIDEND.value: Decimal("900"),
        FaA2NatureOfAmount.PROCEEDS.value: Decimal("1349.10"),
    }
    assert sum(
        item.row.gross_amount_paid_credited_inr or Decimal() for item in result.a3
    ) == Decimal("900")
    closed = next(item for item in result.a3 if item.row.audit_disposal_date is not None)
    assert closed.row.gross_proceeds_from_sale_inr == Decimal("1349.10")
    assert closed.row.audit_ttbr_dec31 == Decimal("90")


def test_in_cy_dividend_without_holding_date_refuses_a3_attribution() -> None:
    paired, accounts = _paired(sold=True, dividend=True)
    income = _income(dividend=True)
    [payment] = income.dividend_payments
    income = replace(
        income,
        dividend_payments=(
            replace(payment, dividend=replace(payment.dividend, holding_date=None)),
        ),
    )

    with pytest.raises(robinhood_fa_mod.RobinhoodFaError, match=r"precedes US T\+1 settlement"):
        compute_robinhood_fa(
            paired,
            config=_config(accounts),
            securities=configured_securities(_config(accounts)),
            income=income,
            options=_options(),
            calendar_year=2025,
            fx_policy=_FlatFx(),
            prices_by_ticker=_prices(),
            entity_overrides=_entity(),
        )


_DEC31 = date(2025, 12, 31)
_JAN2 = date(2026, 1, 2)


def _compute_dividend_dated(activity: date, paid: date) -> robinhood_fa_mod.RobinhoodFaResult:
    """CY 2025 Schedule FA at ₹80/$ for `_income`'s $10 dividend on the 2
    shares the first account holds from January 10, recorded December 19,
    2025 (holding date December 18), with Activity Date ``activity`` and
    the description's ``P/D`` ``paid``. Schedule FA reads both dates from the
    income payment, never from the paired event."""
    paired, accounts = _paired(sold=False, dividend=True)
    income = _income(dividend=True)
    [payment] = income.dividend_payments
    dated = replace(
        payment,
        source_record_date=date(2025, 12, 19),
        source_payment_date=paid,
        dividend=replace(payment.dividend, payment_date=activity, holding_date=date(2025, 12, 18)),
    )
    return compute_robinhood_fa(
        paired,
        config=_config(accounts),
        securities=configured_securities(_config(accounts)),
        income=replace(income, dividend_payments=(dated,)),
        options=_options(),
        calendar_year=2025,
        fx_policy=_FlatFx(),
        prices_by_ticker=_prices(),
        entity_overrides=_entity(),
    )


@pytest.mark.parametrize(
    ("activity", "paid", "a2", "a3"),
    [
        pytest.param(
            _DEC31, _JAN2, (FaA2NatureOfAmount.DIVIDEND, Decimal(800)), Decimal(800), id="pd-later"
        ),
        pytest.param(
            _JAN2, _DEC31, (FaA2NatureOfAmount.NO_AMOUNT, Decimal(0)), Decimal(0), id="pd-earlier"
        ),
        pytest.param(
            _DEC31,
            _DEC31,
            (FaA2NatureOfAmount.DIVIDEND, Decimal(800)),
            Decimal(800),
            id="both-2025",
        ),
        pytest.param(
            _JAN2, _JAN2, (FaA2NatureOfAmount.NO_AMOUNT, Decimal(0)), Decimal(0), id="both-2026"
        ),
    ],
)
def test_dividend_files_in_the_year_of_its_activity_date(
    activity: date, paid: date, a2: tuple[FaA2NatureOfAmount, Decimal], a3: Decimal
) -> None:
    """#1320: Schedule FA puts a dividend in its Activity Date's calendar year
    (ADR 0005), as Schedule OS does, whatever year the description's ``P/D``
    names. Main chose by the ``P/D``: posted December 31 with ``P/D``
    January 2 it filed A2 ``N``/0 and A3 0, and posted January 2 with ``P/D``
    December 31 it filed ₹800 in both. Same-year dates file as on main."""
    result = _compute_dividend_dated(activity, paid)

    assert result.blockers == ()
    assert [
        (row.nature_of_amount, row.gross_amount_inr)
        for row in result.a2_rows
        if row.account_number == "111111111"
    ] == [a2]
    assert [item.row.gross_amount_paid_credited_inr for item in result.a3] == [a3]


def test_robinhood_sale_consideration_contract() -> None:
    """#1383 (#602's option (a)): the recorded Amount is the sale's full value
    of consideration, so A3 gross proceeds and A2 ``PROCEEDS`` both file
    $9,999.70 at the December 31 TTBR, with no manual task. Main left both
    blank with an ``a3-gross-proceeds`` and an ``a2-gross-credits`` task."""
    sale = replace(
        _sale(),
        quantity=Decimal("100"),
        price_usd=Decimal("100.00"),
        amount_usd=Decimal("9999.70"),
    )
    paired, accounts = _paired_events(
        (_buy("101"), sale),
        position_quantity=Decimal("1"),
    )
    result = compute_robinhood_fa(
        paired,
        config=_config(accounts),
        securities=configured_securities(_config(accounts)),
        income=_income(dividend=False),
        options=_options(),
        calendar_year=2025,
        fx_policy=_FlatFx(),
        prices_by_ticker=_prices(),
        entity_overrides=_entity(),
    )

    closed = next(item for item in result.a3 if item.row.audit_disposal_date is not None)
    assert closed.row.gross_proceeds_from_sale_inr == Decimal("799976.00")
    assert closed.row.audit_ttbr_dec31 == Decimal("80")
    first = next(item for item in result.a2 if item.row.account_number == "111111111")
    assert first.unresolved_gross_natures == ()
    assert first.known_gross_components_inr == {
        FaA2NatureOfAmount.PROCEEDS.value: Decimal("799976.00"),
    }
    assert {
        (row.nature_of_amount, row.gross_amount_inr)
        for row in result.a2_rows
        if row.account_number == "111111111"
    } == {(FaA2NatureOfAmount.PROCEEDS, Decimal("799976.00"))}
    assert result.manual_work == ()
    assert result.cbdt_json_eligible is True


def test_no_sale_complete_case_is_cbdt_eligible() -> None:
    paired, accounts = _paired(sold=False, dividend=False)
    result = compute_robinhood_fa(
        paired,
        config=_config(accounts),
        securities=configured_securities(_config(accounts)),
        income=_income(dividend=False),
        options=_options(),
        calendar_year=2025,
        fx_policy=_FlatFx(),
        prices_by_ticker=_prices(),
        entity_overrides=_entity(),
    )
    assert result.blockers == ()
    assert result.manual_work == ()
    assert result.cbdt_json_eligible is True
    # Each account's statement number matched its configured one.
    assert {item.account_index for item in result.a2} == {0, 1}
    assert all(item.account_id_cross_checked for item in result.a2)


def test_a2_builder_takes_resolved_accounts_not_config() -> None:
    """A2 reads the resolved accounts only, so the config isn't passed (#1137)."""
    assert "config" not in inspect.signature(robinhood_fa_mod._build_a2).parameters


def test_a2_selects_earliest_usd_peak_before_ttbr_conversion() -> None:
    paired, accounts = _paired(sold=False, dividend=False)

    class _UsdFirstFx(FxPolicy):
        currency = "USD"

        @override
        def rate_for(self, event_date: date) -> Decimal:
            if event_date == date(2025, 1, 10):
                return Decimal("80")
            return Decimal("90") if event_date == _END else Decimal("1000")

    result = compute_robinhood_fa(
        paired,
        config=_config(accounts),
        securities=configured_securities(_config(accounts)),
        income=_income(dividend=False),
        options=_options(),
        calendar_year=2025,
        fx_policy=_UsdFirstFx(),
        prices_by_ticker=_prices(),
        entity_overrides=_entity(),
    )
    first = next(item for item in result.a2 if item.row.account_number == "111111111")
    assert first.row.peak_balance_inr == Decimal("800")
    assert first.row.audit_peak_balance_lower_date == date(2025, 1, 10)
    assert first.peak_floor_inr == Decimal("900")
    assert first.peak_floor_date == _END
    assert first.row.closing_balance_inr == Decimal("900")


# #1165: a missing Dec-31 bar refuses here, before the Dec-31 resolver could
# make it an A3_CLOSING blocker.
@pytest.mark.parametrize("missing", [date(2025, 1, 13), _END], ids=["mid-year", "dec31"])
def test_a2_missing_held_equity_session_refuses(missing: date) -> None:
    paired, accounts = _paired(sold=False, dividend=False)
    prices = _prices()
    series = prices[Ticker(_SYMBOL)]
    prices[Ticker(_SYMBOL)] = replace(
        series,
        closes={day: value for day, value in series.closes.items() if day != missing},
    )
    with pytest.raises(RobinhoodA2PeakError) as raised:
        compute_robinhood_fa(
            paired,
            config=_config(accounts),
            securities=configured_securities(_config(accounts)),
            income=_income(dividend=False),
            options=_options(),
            calendar_year=2025,
            fx_policy=_FlatFx(),
            prices_by_ticker=prices,
            entity_overrides=_entity(),
        )
    assert str(raised.value) == EQUITY_COVERAGE_REFUSAL


def test_a2_in_year_split_with_open_position_refuses() -> None:
    paired, accounts = _paired(sold=False, dividend=False)
    prices = _prices()
    series = prices[Ticker(_SYMBOL)]
    prices[Ticker(_SYMBOL)] = replace(
        series,
        has_split_in_range=True,
        split_ratios={date(2025, 1, 15): Decimal("2")},
        price_basis=CONTEMPORANEOUS_BASIS,
    )
    with pytest.raises(RobinhoodA2PeakError, match="in-year split"):
        compute_robinhood_fa(
            paired,
            config=_config(accounts),
            securities=configured_securities(_config(accounts)),
            income=_income(dividend=False),
            options=_options(),
            calendar_year=2025,
            fx_policy=_FlatFx(),
            prices_by_ticker=prices,
            entity_overrides=_entity(),
        )


# ---- #1165: the Dec-31 cross-check reads the required US market session ----

# Shared fixtures keep their identifiers and issuer name; privacy checks use their supplied values.
# The one open lot of ``_paired(sold=False, dividend=False)``: the buy at
# account 0, event 2, of configured security 0.
_OPEN_LOT_CLOSING_BLOCKER = robinhood_fa_mod.RobinhoodFaBlocker(
    kind=RobinhoodFaBlockerKind.A3_CLOSING,
    security_config_indices=(0,),
    lot_ids=(RobinhoodSecurityEventReference(0, 2),),
)


# ---- Issuer country India (#1325) ---------------------------------------------


def compute_with_issuer_country(
    country: str, *, sold: bool = False
) -> robinhood_fa_mod.RobinhoodFaResult:
    """:func:`_compute_open` (with ``sold``, a partial sale too) for a security
    configured with issuer country ``country``."""
    paired, accounts = _paired(sold=sold, dividend=False)
    security = _security()
    security = replace(
        security, instrument=replace(security.instrument, issuer_country_code=CountryCode(country))
    )
    config = replace(_config(accounts), securities=(security,))
    return compute_robinhood_fa(
        paired,
        config=config,
        securities=configured_securities(config),
        income=_income(dividend=False),
        options=_options(),
        calendar_year=2025,
        fx_policy=_FlatFx(),
        prices_by_ticker=_prices(),
        entity_overrides=_entity(),
    )


@pytest.mark.parametrize("sold", [False, True], ids=["open", "partly-sold"])
@pytest.mark.parametrize(("country", "code"), [("US", "2"), ("NL", "31")])
def test_a3_rows_keep_a_foreign_issuer_country(country: str, code: str, sold: bool) -> None:
    """Controls for the India refusal: the same lots file under any other country."""
    result = compute_with_issuer_country(country, sold=sold)

    assert [row.country_code for row in result.a3_rows] == [code] * (2 if sold else 1)
    assert sorted(row.audit_disposal_date is not None for row in result.a3_rows) == (
        [False, True] if sold else [False]
    )


@pytest.mark.parametrize("sold", [False, True], ids=["open", "partly-sold"])
def test_issuer_country_india_refuses_the_a3_row_without_echo(sold: bool) -> None:
    """An Indian issuer (say, an ADR configured with the underlying company's
    country) can't go in Table A3, whose dropdown leaves India out. Main filed
    code 91; now the row refuses with a fixed message naming none of the
    security's symbol, CUSIP, ISIN, name or address."""
    with pytest.raises(InvariantError) as exc:
        compute_with_issuer_country("IN", sold=sold)

    expected = InvariantError(what=INDIA_REFUSAL.removeprefix("InvariantError: "), context=())
    assert_unchained_refusal(
        exc.value,
        expected,
        INDIA_REFUSAL,
        markers=(_SYMBOL, _CUSIP, _ISIN, "Synthetic Corporation", "One Synthetic Way"),
    )


@_SESSION_REFUSALS
def test_dec31_session_refusal_is_the_existing_a3_closing_blocker(
    monkeypatch: pytest.MonkeyPatch,
    session_refusal: Callable[[pytest.MonkeyPatch], dict[Ticker, PriceSeries]],
) -> None:
    """#1165: the resolver's session refusal goes through the existing catch,
    so compute returns the same A3_CLOSING blocker a statement mismatch gets."""
    mismatch = _compute_open(_prices("20"))
    prices = session_refusal(monkeypatch)
    refusals: list[ClosingPriceError] = []
    resolve = robinhood_fa_mod.resolve_dec31_close

    def recorded(**kwargs: Any) -> ResolvedClose:
        try:
            return resolve(**kwargs)
        except ClosingPriceError as exc:
            refusals.append(exc)
            raise

    monkeypatch.setattr(robinhood_fa_mod, "resolve_dec31_close", recorded)

    result = _compute_open(prices)

    assert [str(exc) for exc in refusals] == [MARKET_SESSION_REFUSAL]
    assert_markers_absent(refusals[0], MARKER_SOURCE_DATE, _SYMBOL, _CUSIP, _ISIN)
    assert result.blockers == mismatch.blockers == (_OPEN_LOT_CLOSING_BLOCKER,)
    assert result.a3 == mismatch.a3 == ()
    assert result.cbdt_json_eligible is False


@pytest.mark.parametrize("dec31_closed", [False, True], ids=["dec31-session", "dec31-closure"])
def test_dec31_cross_check_reads_the_required_session_close(
    monkeypatch: pytest.MonkeyPatch, dec31_closed: bool
) -> None:
    """#1165 control: an earlier session's different close is never
    cross-checked, and the filed closing value stays the allocated statement
    total ($30 at 80), not the session's market value ($30.008)."""
    if dec31_closed:
        monkeypatch.setattr(us_market_calendar, "_CLOSURES", us_market_calendar._CLOSURES | {_END})
    session = us_market_calendar.us_market_session_on_or_before(_END)
    assert session == (_DEC30 if dec31_closed else _END)
    prices = _prices()
    series = prices[Ticker(_SYMBOL)]
    prices[Ticker(_SYMBOL)] = replace(
        series,
        closes={
            **series.closes,
            previous_us_market_session(session): Decimal(MARKER_PRICE),
            session: Decimal("15.004"),
        },
    )

    result = _compute_open(prices)

    assert result.blockers == ()
    assert [item.row.closing_value_inr for item in result.a3] == [Decimal("2400")]


# ---- #1242: a lone lot's statement total files its exact closing value ------


def test_lone_lot_files_its_exact_statement_total(tmp_path: Path) -> None:
    """#1242: $1.00 x ₹82.50 = ₹82.50 files ₹83 closing and ₹83 peak. Main
    multiplied 3 x ($1.00 / 3) back to ₹82.4999… and filed ₹82 and ₹82."""
    result = _compute_one_dollar_three_shares()

    assert result.blockers == ()
    [item] = result.a3
    assert item.row.closing_value_inr == item.row.peak_value_inr == Decimal("82.5")
    assert item.row.filed.closing_value_inr == item.row.filed.peak_value_inr == Decimal("83")
    written, complete, _missing = write_fa(
        result,
        output_dir=tmp_path,
        format_id="filing-csv",
        financial_year=FinancialYear(2025),
        itr_form="ITR-3",
        json_available=True,
        json_cleanup=ClaimedOutputCleanup(tmp_path / JSON_FILENAME),
    )
    assert complete is True
    assert tmp_path / "schedule-fa-a3.csv" in written
    assert _a3_cells(tmp_path / "schedule-fa-a3.csv") == ["83", "83"]


def test_lone_lot_close_mismatch_keeps_its_blocker_and_message(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#1242 refusing control: the same lot against a $0.50 Yahoo close ($1.50
    for 3 shares) still refuses with the existing complete message, naming no
    security or amount, and leaves the existing A3_CLOSING blocker."""
    refusals: list[ClosingPriceError] = []
    resolve = robinhood_fa_mod.resolve_dec31_close

    def recorded(**kwargs: Any) -> ResolvedClose:
        try:
            return resolve(**kwargs)
        except ClosingPriceError as exc:
            refusals.append(exc)
            raise

    monkeypatch.setattr(robinhood_fa_mod, "resolve_dec31_close", recorded)

    result = _compute_statement_total((_buy("3"),), total="1.00", price="0.33", yahoo="0.50")

    assert [str(exc) for exc in refusals] == [
        "Dec-31 close mismatch: the statement and yahoo_fallback totals differ by more than "
        "the tolerance; refusing rather than file an unverified closing value"
    ]
    assert_markers_absent(refusals[0], _SYMBOL, _CUSIP, _ISIN, "1.00", "0.33", "0.50")
    assert result.blockers == (_OPEN_LOT_CLOSING_BLOCKER,)
    assert result.a3 == ()


# ---- #1243: exact A3 shares of lot cost, statement total and credits --------


def _later(event: RobinhoodEvent, day: date = date(2025, 1, 20)) -> RobinhoodEvent:
    """``event`` as source row 5 on ``day``, a later lot than ``_buy()``'s row 2."""
    return replace(
        event, source_record_index=5, activity_date=day, process_date=day, settle_date=day
    )


def _compute_partly_sold() -> robinhood_fa_mod.RobinhoodFaResult:
    """The #1243 repro at a flat ₹84.15/$: 3 shares bought for $10.00 in
    January, 1 sold for $5.00 in June; the Dec-31 statement shows the other 2
    worth $30."""
    buy = replace(_buy("3"), price_usd=Decimal("3.33"), amount_usd=Decimal("-10.00"))
    sale = replace(_sale(), price_usd=Decimal("5.00"), amount_usd=Decimal("5.00"))
    paired, accounts = _paired_events((buy, sale), position_quantity=Decimal("2"))
    return compute_robinhood_fa(
        paired,
        config=_config(accounts),
        securities=configured_securities(_config(accounts)),
        income=_income(dividend=False),
        options=_options(),
        calendar_year=2025,
        fx_policy=AuditedFxStub(Decimal("84.15")),
        prices_by_ticker=_prices(),
        entity_overrides=_entity(),
    )


def _a3_column(path: Path, name: str) -> list[str]:
    """Every A3 row's ``name`` cell, in file order."""
    with path.open(encoding="utf-8", newline="") as handle:
        return [row[name] for row in csv.DictReader(handle)]


def test_partly_sold_lot_files_its_exact_initial_value_shares(tmp_path: Path) -> None:
    """#1243 repro: the sold share's cost is exactly $10.00/3, so at ₹84.15 its
    initial value is ₹280.50 and files ₹281 (main's 20-digit share made
    ₹280.4999… and filed ₹280). The 2 held shares still file ₹561."""
    result = _compute_partly_sold()

    assert result.blockers == ()
    assert [
        (item.row.audit_disposal_date, item.row.initial_value_inr, item.row.filed.initial_value_inr)
        for item in result.a3
    ] == [
        (None, Decimal("561"), Decimal("561")),
        (date(2025, 6, 10), Decimal("280.5"), Decimal("281")),
    ]
    written, _complete, _missing = write_fa(
        result,
        output_dir=tmp_path,
        format_id="filing-csv",
        financial_year=FinancialYear(2025),
        itr_form="ITR-3",
        json_available=True,
        json_cleanup=ClaimedOutputCleanup(tmp_path / JSON_FILENAME),
    )
    assert tmp_path / "schedule-fa-a3.csv" in written
    assert _a3_column(tmp_path / "schedule-fa-a3.csv", "Initial value of the investment") == [
        "561",
        "281",
    ]


@pytest.mark.parametrize(
    "drift",
    [
        pytest.param(lambda part: replace(part, quantity=part.quantity + 1), id="quantity"),
        pytest.param(lambda part: replace(part, exact_cost_usd=None), id="exact-cost"),
    ],
)
def test_lot_fragments_must_conserve_quantity_and_exact_cost(
    monkeypatch: pytest.MonkeyPatch, drift: Callable[[Any], Any]
) -> None:
    """#1243: A3 files each fragment's exact cost, so the lot-conservation
    check adds those too. The repro's open part dropping its exact $20/3 cost
    refuses with the existing complete message, as a share too many always did."""
    reconcile = robinhood_fa_mod.reconcile_robinhood_security_lots

    def drifted(quantities: Any) -> RobinhoodSecurityLotResult:
        lots = reconcile(quantities)
        (part,) = lots.open_fragments
        return replace(lots, open_fragments=(drift(part),))

    monkeypatch.setattr(robinhood_fa_mod, "reconcile_robinhood_security_lots", drifted)

    with pytest.raises(robinhood_fa_mod.RobinhoodFaError) as raised:
        _compute_partly_sold()

    assert str(raised.value) == "economic lot fragments do not conserve source quantity and cost"


def _recorded_lots[LotT: (OpenLot, ClosedLot)](
    monkeypatch: pytest.MonkeyPatch, kind: type[LotT]
) -> list[LotT]:
    """The A3 lots of type ``kind`` compute builds, recorded as it builds them."""
    recorded: list[LotT] = []
    temporary_lots = robinhood_fa_mod._temporary_lots

    def recording(*args: Any) -> tuple[dict[Any, OpenLot | ClosedLot], tuple[Any, ...]]:
        lots, blockers = temporary_lots(*args)
        recorded.extend(lot for lot in lots.values() if isinstance(lot, kind))
        return lots, blockers

    monkeypatch.setattr(robinhood_fa_mod, "_temporary_lots", recording)
    return recorded


def _recorded_open_lots(monkeypatch: pytest.MonkeyPatch) -> list[OpenLot]:
    """The open A3 lots compute builds, in row order, recorded as it builds them."""
    return _recorded_lots(monkeypatch, OpenLot)


def test_split_statement_total_files_exact_shares_that_add_back(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#1243: $3.40 over a 1-share and a 2-share lot splits into exactly $3.40/3
    and $6.80/3, which add back to $3.40. At ₹82.50 the 1-share lot's closing,
    and the peak it floors, is ₹93.50 and files ₹94 (main's 60-digit share made
    ₹93.4999… and filed ₹93); the 2-share lot files ₹187."""
    open_lots = _recorded_open_lots(monkeypatch)

    result = _compute_statement_total(
        (_buy("1"), _later(_buy("2"))), total="3.40", price="1.13", yahoo="1.1333"
    )

    assert result.blockers == ()
    assert [
        (
            item.row.closing_value_inr,
            item.row.filed.closing_value_inr,
            item.row.filed.peak_value_inr,
        )
        for item in result.a3
    ] == [
        (Decimal("93.5"), Decimal("94"), Decimal("94")),
        (Decimal("187"), Decimal("187"), Decimal("187")),
    ]
    shares = [cast("Fraction", lot.closing_total_usd_exact) for lot in open_lots]
    assert shares == [Fraction("3.40") / 3, Fraction("6.80") / 3]
    assert sum(shares, Fraction()) == Fraction("3.40")


def test_split_sale_files_exact_a3_gross_proceeds_shares(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#1383: a $100.00 sale of 3 shares from a 1-share and a 2-share lot
    splits into exactly $100/3 and $200/3. At a flat ₹90 the two A3 rows'
    gross proceeds are exactly ₹3,000 and ₹6,000; the 60-digit Decimal shares
    would give ₹2,999.99… and ₹6,000.00…03. Main left both blank."""
    closed_lots = _recorded_lots(monkeypatch, ClosedLot)
    sale = replace(
        _sale(),
        source_record_index=6,
        quantity=Decimal("3"),
        price_usd=Decimal("33.34"),
        amount_usd=Decimal("100.00"),
    )
    paired, accounts = _paired_events(
        (_buy("1"), _later(_buy("2")), sale), position_quantity=Decimal()
    )

    result = compute_robinhood_fa(
        paired,
        config=_config(accounts),
        securities=configured_securities(_config(accounts)),
        income=_income(dividend=False),
        options=_options(),
        calendar_year=2025,
        fx_policy=AuditedFxStub(Decimal("90")),
        prices_by_ticker=_prices(),
        entity_overrides=_entity(),
    )

    assert result.blockers == ()
    assert [item.row.gross_proceeds_from_sale_inr for item in result.a3] == [
        Decimal("3000"),
        Decimal("6000"),
    ]
    assert [lot.gross_proceeds_money_exact for lot in closed_lots] == [
        Fraction(100, 3),
        Fraction(200, 3),
    ]


@pytest.mark.parametrize(
    ("total", "price", "yahoo", "filed"),
    [
        pytest.param("1.00", "0.33", "0.3333", [Decimal("28"), Decimal("55")], id="one-dollar"),
        pytest.param("30", "10", "10", [Decimal("825"), Decimal("1650")], id="thirty-dollars"),
    ],
)
def test_split_statement_total_files_unchanged_values_where_main_was_exact(
    total: str, price: str, yahoo: str, filed: list[Decimal]
) -> None:
    """#1243 control: $1.00 over a 1-share and a 2-share lot files ₹28 and ₹55
    at ₹82.50, as on main, whose 60-digit share happened to round back to the
    exact ₹27.50; $30 divides exactly and files ₹825 and ₹1,650, as on main."""
    result = _compute_statement_total(
        (_buy("1"), _later(_buy("2"))), total=total, price=price, yahoo=yahoo
    )

    assert result.blockers == ()
    assert [item.row.filed.closing_value_inr for item in result.a3] == filed


@pytest.mark.parametrize(
    ("total", "price", "yahoo", "filed"),
    [("1.00", "0.33", "0.3333", ["27", "55"]), ("30", "10", "10", ["821", "1642"])],
    ids=["nonterminating", "decimal-exact"],
)
def test_split_statement_total_keeps_accepting_a_rate_at_the_digit_cap(
    monkeypatch: pytest.MonkeyPatch, total: str, price: str, yahoo: str, filed: list[str]
) -> None:
    """#1243 control: main accepts this 996-digit rate. A split share's review
    cut times that rate can need more product digits; its rational still converts.
    The rate is 996 digits, not 1,000, so the A2 closing balance's exact product
    with the statement NAV (#1250) still fits the 1,000-digit cap.
    """
    rate = Decimal("82." + "1" * 994)
    monkeypatch.setattr(_Fx8250, "rate_for", lambda self, day: rate)
    result = _compute_statement_total(
        (_buy("1"), _later(_buy("2"))), total=total, price=price, yahoo=yahoo
    )
    assert result.blockers == ()
    assert [item.row.filed.closing_value_inr for item in result.a3] == list(map(Decimal, filed))


def test_lone_statement_total_keeps_its_product_digit_refusal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#1242 control: a lone total's over-precision product still refuses. Since
    #1250 the A2 closing balance (the statement NAV x the same rate, built
    first) is exact too, so its product refuses first. Only the Dec-31 rate is
    long: since #1260 a peak's INR value is exact too, and these peaks fall on
    earlier days."""
    rate = Decimal("82." + "1" * 998)
    monkeypatch.setattr(
        _Fx8250,
        "rate_for",
        lambda self, day: rate if day == date(2025, 12, 31) else Decimal("82.50"),
    )
    with pytest.raises(InvariantError) as raised:
        _compute_statement_total((_buy("1"),), total="10", price="10", yahoo="10")
    assert str(raised.value) == (
        f"InvariantError: Schedule FA A2 closing balance, INR: {INEXACT_PRODUCT}"
    )
    assert raised.value.context == ()
    assert raised.value.__cause__ is None
    assert raised.value.__context__ is None


def _compute_split_dividend(amount: str, rate: str) -> robinhood_fa_mod.RobinhoodFaResult:
    """Schedule FA at ₹82.50/$ for a 1-share and a 2-share lot, both held on the
    holding date of an ``amount`` dividend paid at ``rate`` a share."""
    dividend = replace(
        _dividend(),
        amount_usd=Decimal(amount),
        description_shares=Decimal("3"),
        description_rate_usd=Decimal(rate),
    )
    paired, accounts = _paired_events(
        (_buy("1"), dividend, _later(_buy("2"))),
        position_quantity=Decimal("3"),
        dividends=Decimal(amount),
    )
    income = _income(dividend=True)
    [payment] = income.dividend_payments
    paid = replace(payment, dividend=replace(payment.dividend, amount_gross=Decimal(amount)))
    return compute_robinhood_fa(
        paired,
        config=_config(accounts),
        securities=configured_securities(_config(accounts)),
        income=replace(income, dividend_payments=(paid,)),
        options=_options(),
        calendar_year=2025,
        fx_policy=_Fx8250(),
        prices_by_ticker=_prices(),
        entity_overrides=_entity(),
    )


@pytest.mark.parametrize(
    ("amount", "rate", "credited", "filed"),
    [
        pytest.param("3.40", "1.1333", ("93.5", "187"), ("94", "187"), id="half-rupee-share"),
        pytest.param("1.00", "0.3333", ("27.5", "55"), ("28", "55"), id="one-dollar"),
        pytest.param("3.00", "1", ("82.5", "165"), ("83", "165"), id="exact-thirds"),
    ],
)
def test_split_dividend_files_each_holder_its_exact_credit(
    amount: str, rate: str, credited: tuple[str, str], filed: tuple[str, str]
) -> None:
    """#1243: a dividend held by a 1-share and a 2-share lot splits into
    exactly a third and two thirds, which add back to it at ₹82.50 and each
    file their exact HALF_UP. $3.40 files ₹94 and ₹187 (main's 60-digit share
    made ₹93.4999… and filed ₹93); $1.00 and $3.00 file as on main."""
    result = _compute_split_dividend(amount, rate)

    assert result.blockers == ()
    assert [
        (item.row.gross_amount_paid_credited_inr, item.row.filed.gross_amount_paid_credited_inr)
        for item in result.a3
    ] == [(Decimal(value), Decimal(rupees)) for value, rupees in zip(credited, filed, strict=True)]


_STAND_IN_PEAK_USD = Decimal(1000000)


def _stand_in_a2_peaks(monkeypatch: pytest.MonkeyPatch) -> None:
    """Replace each account's A2 peak with a fixed $1,000,000 on Dec 31.

    Since #1267 these cases holding a lot of ``TINY_QUANTITY`` shares beside
    cash or another lot refuse first at A2: their exact equity ledger or day
    value needs more digits than the cap. The stand-in lets these runs reach
    the A3 shares they test."""

    def peaks(
        paired: RobinhoodAccountsReconciliation,
        *,
        calendar_year: int,
        fx_policy: FxPolicy,
        **_: object,
    ) -> tuple[RobinhoodA2Peak, ...]:
        day = date(calendar_year, 12, 31)
        ttbr = fx_policy.rate_for(day)
        return tuple(
            RobinhoodA2Peak(index, day, _STAND_IN_PEAK_USD, ttbr, _STAND_IN_PEAK_USD * ttbr)
            for index in range(len(paired.accounts))
        )

    monkeypatch.setattr(robinhood_fa_mod, "compute_robinhood_a2_peaks", peaks)


def _past_cap_events(share: str) -> tuple[RobinhoodEvent, ...]:
    """A lot of ``TINY_QUANTITY`` shares beside a 2-share lot bought for an
    Amount holding ``DIGIT_RUN``, both described ``DESCRIPTION``. For
    ``closing`` the tiny lot is the earlier row, so it takes a non-last share
    of the $30 statement total; for ``credit`` it is the later row, and both
    lots hold ``_dividend()``."""
    tiny = replace(_buy("1"), quantity=Decimal(TINY_QUANTITY), amount_usd=Decimal("-0.01"))
    pair = replace(_buy("2"), price_usd=Decimal("368126"), amount_usd=Decimal("-736252.00"))
    tiny, pair = (
        replace(buy, description=DESCRIPTION, issuer_name=DESCRIPTION) for buy in (tiny, pair)
    )
    if share == "closing":
        return (tiny, _later(pair))
    return (pair, _dividend(), _later(tiny, date(2025, 2, 20)))


@pytest.mark.parametrize("share", ["closing", "credit"])
def test_a3_share_past_the_digit_cap_refuses_without_echo(
    monkeypatch: pytest.MonkeyPatch, share: str
) -> None:
    """#1243: a lot of 1E-1001 shares has no exact share of the statement total
    or of a credit, so compute refuses with the fixed digit-cap text naming only
    the security's index, where main filed A3 rows. The refusal's text, repr,
    args and chain hold none of the lots' description, symbol or Amount digits.
    The A2 peak is a stand-in, since A2 now refuses this run first (#1267), and
    so is the lots' anchor: the exact 2 + 1E-1001 shares the normalizer holds
    against the statement's 2 now block the lots first (#1308)."""
    # Helpers that use the symbol read ``FA_SYMBOL`` when called, so the run trades ``SYMBOL``.
    monkeypatch.setattr(fa_inputs, "FA_SYMBOL", SYMBOL)
    _stand_in_a2_peaks(monkeypatch)
    stand_in_rounded_anchor_ledgers(monkeypatch, robinhood_fa_mod)
    paired, accounts = _paired_events(
        _past_cap_events(share),
        position_quantity=Decimal("2"),
        dividends=Decimal("10") if share == "credit" else Decimal(),
    )
    buys = [
        event
        for event in paired.accounts[0].evidence.events
        if event.kind is RobinhoodEventKind.EQUITY_BUY
    ]
    # Each marker is in the run's input.
    assert {(event.description, event.issuer_name, event.instrument) for event in buys} == {
        (DESCRIPTION, DESCRIPTION, SYMBOL)
    }
    assert any(DIGIT_RUN in str(event.amount_usd) for event in buys)

    with pytest.raises(InvariantError) as raised:
        compute_robinhood_fa(
            paired,
            config=_config(accounts),
            securities=configured_securities(_config(accounts)),
            income=_income(dividend=share == "credit"),
            options=_options(),
            calendar_year=2025,
            fx_policy=_FlatFx(),
            prices_by_ticker=_prices(),
            entity_overrides=_entity(),
        )

    text = share_refusal(share)
    expected = invariant_error(what=text.removeprefix("InvariantError: "))
    assert_unchained_refusal(raised.value, expected, text, markers=MARKERS)


def test_lone_credit_holder_past_the_digit_cap_takes_the_whole_credit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#1243 control: a lone holder takes the whole credit without reading its
    quantity, as a lone fragment takes a whole statement total. A lot of 1E-1001
    shares that alone holds the $10 dividend, sold in June so there is no Dec-31
    split, files ₹841.50 as ₹842 at ₹84.15, as on main, with no digit-cap refusal
    in A3. The A2 peak is a stand-in, since A2 now refuses this run first (#1267)."""
    _stand_in_a2_peaks(monkeypatch)
    tiny = Decimal(TINY_QUANTITY)
    buy = replace(_buy("1"), quantity=tiny, amount_usd=Decimal("-0.01"))
    dividend = replace(_dividend(), description_shares=tiny)
    sale = replace(_sale(), quantity=tiny, amount_usd=Decimal("0.01"))
    paired, accounts = _paired_events(
        (buy, dividend, sale), position_quantity=Decimal(), dividends=Decimal("10")
    )

    result = compute_robinhood_fa(
        paired,
        config=_config(accounts),
        securities=configured_securities(_config(accounts)),
        income=_income(dividend=True),
        options=_options(),
        calendar_year=2025,
        fx_policy=AuditedFxStub(Decimal("84.15")),
        prices_by_ticker=_prices(),
        entity_overrides=_entity(),
    )

    assert result.blockers == ()
    assert [
        (item.row.gross_amount_paid_credited_inr, item.row.filed.gross_amount_paid_credited_inr)
        for item in result.a3
    ] == [(Decimal("841.5"), Decimal("842"))]


# ---- #1263: exact lot quantities after a same-day pro-rata sale -------------

# The 60-digit Decimal quantities, cut toward zero, of lots holding exactly 2/3
# and 1/3 of a share.
_TWO_THIRDS_CUT = Decimal("0." + "6" * 60)
_THIRD_CUT = Decimal("0." + "3" * 60)


def _priced(base: str, price: str, first: date, last: date = _END) -> dict[Ticker, PriceSeries]:
    """``_prices(base)``, closing at ``price`` every session from ``first`` to ``last``."""
    (series,) = _prices(base).values()
    closes = {
        day: Decimal(price) if first <= day <= last else close
        for day, close in series.closes.items()
    }
    return {series.ticker: replace(series, closes=closes)}


@pytest.mark.parametrize("pipeline", ["cg", "fa"])
def test_pro_rata_quantity_keeps_the_existing_disposal_digit_cap(pipeline: str) -> None:
    """The rational sidecar cannot bypass main's cap on a rounded disposal quantity."""
    events = tuple(
        replace(
            event,
            quantity=Decimal("1E-990"),
            price_usd=Decimal("1E+991")
            if event.kind is RobinhoodEventKind.EQUITY_BUY
            else Decimal("1.70E+990"),
        )
        for event in _same_day_sale(day=date(2025, 2, 15))
    )

    with pytest.raises(InvariantError) as raised:
        if pipeline == "fa":
            _compute_statement_total(events, total="3.40", price="1.70E+990", yahoo="1.70E+990")
        else:
            paired, accounts = _paired_events(events, position_quantity=Decimal("2E-990"))
            config = _config(accounts)
            compute_robinhood_capital_gains(
                paired,
                config=config,
                securities=configured_securities(config),
                financial_year_start=date(2024, 4, 1),
                financial_year_end=date(2025, 3, 31),
                fx_policy=AuditedFxStub(Decimal("84.15")),
            )

    text = (
        "InvariantError: Robinhood lot allocation, activity row 4, column Amount: "
        f"the share {TOO_MANY_DIGITS}"
    )
    expected = invariant_error(what=text.removeprefix("InvariantError: "))
    assert_unchained_refusal(raised.value, expected, text, markers=("1E-990", "1E+991"))


def test_non_pro_rata_quantity_keeps_decimal_conservation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An ordinary FIFO lot keeps main's 60-digit quantity remainder, without a rational.
    The A2 peak is a stand-in: the helper's statement cash rounds the $1E-60 sale
    away, so A2's exact daily cash now refuses this run first (#1267). So is the
    lots' anchor: the exact 1 - 1E-61 shares the normalizer holds against the
    statement's 1 now block the lots first (#1308)."""
    _stand_in_a2_peaks(monkeypatch)
    stand_in_rounded_anchor_ledgers(monkeypatch, robinhood_fa_mod)
    open_lots = _recorded_open_lots(monkeypatch)
    sale = replace(
        _sale(),
        quantity=Decimal("1E-61"),
        amount_usd=Decimal("1E-60"),
        price_usd=Decimal("10"),
    )

    result = _compute_statement_total((_buy("1"), sale), total="10", price="10", yahoo="10")

    assert result.blockers == ()
    assert [
        (item.row.filed.closing_value_inr, item.row.filed.peak_value_inr) for item in result.a3
    ] == [(Decimal("825"), Decimal("825")), (Decimal("0"), Decimal("0"))]
    assert [(lot.quantity, getattr(lot, "quantity_exact", None)) for lot in open_lots] == [
        (Decimal("1"), None)
    ]


def test_pro_rata_thirds_file_their_exact_closing_and_peak(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """#1263 repro: three same-day 1-share lots each give up a third of the 1
    share sold, and the Dec-31 statement shows the other 2 at $1.70, $3.40.
    Each lot keeps exactly 2/3 of a share and $3.40/3, so at ₹82.50 it closes
    and peaks at ₹93.50 and files ₹94. Main kept 0.66666666666666666666 of a
    share in the last lot, whose closing and peak filed ₹93."""
    open_lots = _recorded_open_lots(monkeypatch)

    result = _compute_statement_total(_same_day_sale(), total="3.40", price="1.70", yahoo="1.70")

    assert result.blockers == ()
    written, _complete, _missing = write_fa(
        result,
        output_dir=tmp_path,
        format_id="filing-csv",
        financial_year=FinancialYear(2025),
        itr_form="ITR-3",
        json_available=True,
        json_cleanup=ClaimedOutputCleanup(tmp_path / JSON_FILENAME),
    )
    path = tmp_path / "schedule-fa-a3.csv"
    assert path in written
    # Each lot's held 2/3, then its sold third.
    assert _a3_column(path, "Closing balance") == ["94", "0"] * 3
    assert _a3_column(path, "Peak value of investment during the Period") == ["94", "47"] * 3
    assert [
        (item.row.filed.closing_value_inr, item.row.filed.peak_value_inr) for item in result.a3
    ] == [(Decimal("94"), Decimal("94")), (Decimal("0"), Decimal("47"))] * 3
    assert [(item.row.closing_value_inr, item.row.peak_value_inr) for item in result.a3] == [
        (Decimal("93.5"), Decimal("93.5")),
        (Decimal("0"), Decimal("46.75")),
    ] * 3
    shares = [cast("Fraction", lot.closing_total_usd_exact) for lot in open_lots]
    assert shares == [Fraction("3.40") / 3] * 3
    assert sum(shares, Fraction()) == Fraction("3.40")
    assert [(lot.quantity, lot.quantity_exact) for lot in open_lots] == [
        (_TWO_THIRDS_CUT, Fraction(2, 3))
    ] * 3


def test_pro_rata_thirds_split_a_dividend_into_exact_thirds() -> None:
    """#1263: the same three lots, sold down in February, hold a $3.40
    dividend in March. Each holder's share is exactly $3.40/3, ₹93.50 at
    ₹82.50, and files ₹94; main split it by the rounded quantities and filed
    ₹93 for the last lot. The sold thirds don't hold it."""
    dividend = replace(
        _later(_dividend(), date(2025, 3, 15)),
        amount_usd=Decimal("3.40"),
        description_shares=Decimal("2"),
        description_rate_usd=Decimal("1.70"),
    )
    paired, accounts = _paired_events(
        (*_same_day_sale(day=date(2025, 2, 10)), dividend),
        position_quantity=Decimal("2"),
        dividends=Decimal("3.40"),
    )
    income = _income(dividend=True)
    [payment] = income.dividend_payments
    paid = replace(
        payment,
        event_reference=RobinhoodIncomeEventReference(0, dividend.source_record_index),
        dividend=replace(
            payment.dividend,
            amount_gross=Decimal("3.40"),
            source_row_index=dividend.source_record_index,
        ),
    )

    result = compute_robinhood_fa(
        paired,
        config=_config(accounts),
        securities=configured_securities(_config(accounts)),
        income=replace(income, dividend_payments=(paid,)),
        options=_options(),
        calendar_year=2025,
        fx_policy=_Fx8250(),
        prices_by_ticker=_prices(),
        entity_overrides=_entity(),
    )

    assert result.blockers == ()
    assert [item.row.filed.gross_amount_paid_credited_inr for item in result.a3] == [
        Decimal("94"),
        Decimal("0"),
    ] * 3
    assert [item.row.gross_amount_paid_credited_inr for item in result.a3] == [
        Decimal("93.5"),
        Decimal("0"),
    ] * 3


def test_pro_rata_thirds_value_a_later_peak_at_the_exact_quantity() -> None:
    """#1263: the same lots peak at a $1.70 close on 15 July, after the sale,
    and the year ends at $1.20, $2.40 for the 2 shares. Each lot's peak is
    exactly 2/3 x $1.70 x ₹82.50 = ₹93.50 and files ₹94; main valued it at
    the last lot's 0.66666666666666666666 of a share and filed ₹93. Each lot
    closes at ₹66."""
    july = date(2025, 7, 15)

    result = _compute_statement_total(
        _same_day_sale(), total="2.40", price="1.20", yahoo=_priced("1.20", "1.70", july, july)
    )

    assert result.blockers == ()
    assert [
        (item.row.filed.peak_value_inr, item.row.filed.closing_value_inr) for item in result.a3
    ] == [(Decimal("94"), Decimal("66")), (Decimal("33"), Decimal("0"))] * 3
    assert [item.row.peak_value_inr for item in result.a3] == [Decimal("93.5"), Decimal("33")] * 3


def test_pro_rata_thirds_keep_an_earlier_peak_at_its_own_rate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#1263: at a flat $1.70 close the lots peak on their first day, 10
    January, at ₹97.50 a dollar, against ₹82.50 on Dec 31. The exact $3.40/3
    closing over the exact 2/3 share is the $1.70 peak price, so the peak keeps
    its own day: 2/3 x $1.70 x ₹97.50 = ₹110.50 files ₹111, not the ₹94
    closing. Over the rounded quantity the closing would exceed the peak price
    and floor it to the closing. Main filed ₹110 and ₹93 for the last lot."""
    monkeypatch.setattr(
        _Fx8250,
        "rate_for",
        lambda self, day: Decimal("82.50") if day == _END else Decimal("97.50"),
    )
    peak = Fraction(2, 3) * Fraction("1.70") * Fraction("97.50")
    assert peak == Fraction("110.5")

    result = _compute_statement_total(_same_day_sale(), total="3.40", price="1.70", yahoo="1.70")

    assert result.blockers == ()
    assert [
        (item.row.filed.peak_value_inr, item.row.filed.closing_value_inr) for item in result.a3
    ] == [(Decimal("111"), Decimal("94")), (Decimal("55"), Decimal("0"))] * 3
    assert [item.row.peak_value_inr for item in result.a3] == [
        Decimal("110.5"),
        Decimal("55.25"),
    ] * 3


def test_pro_rata_sold_thirds_keep_their_exact_quantity(monkeypatch: pytest.MonkeyPatch) -> None:
    """#1263: each third sold in June is a closed A3 lot of exactly 1/3 of a
    share. Its peak, at the $1.40 close before the sale, is exactly 1/3 x
    $1.40 x ₹82.50 = ₹38.50 and files ₹39; main filed ₹38 for the two lots
    that gave up 0.33333333333333333333 of a share. The held lots peak at
    $1.70 from the sale on and file ₹94."""
    closed_lots = _recorded_lots(monkeypatch, ClosedLot)

    result = _compute_statement_total(
        _same_day_sale(),
        total="3.40",
        price="1.70",
        yahoo=_priced("1.40", "1.70", date(2025, 6, 10)),
    )

    assert result.blockers == ()
    assert [item.row.filed.peak_value_inr for item in result.a3] == [
        Decimal("94"),
        Decimal("39"),
    ] * 3
    assert [item.row.peak_value_inr for item in result.a3] == [
        Decimal("93.5"),
        Decimal("38.5"),
    ] * 3
    assert [(lot.quantity, lot.quantity_exact) for lot in closed_lots] == [
        (_THIRD_CUT, Fraction(1, 3))
    ] * 3


def test_pro_rata_halves_file_as_before_without_an_exact_quantity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#1263 control: two same-day 1-share lots each give up half of the 1 share
    sold, which a Decimal holds, so no lot has an exact quantity. The Dec-31
    statement shows the other share at $2.00: each held half closes and peaks
    at ₹82.50 and files ₹83, and each sold half peaks at ₹82.50 too, as on
    main."""
    open_lots = _recorded_open_lots(monkeypatch)
    closed_lots = _recorded_lots(monkeypatch, ClosedLot)

    result = _compute_statement_total(_same_day_sale(2), total="2.00", price="2.00", yahoo="2.00")

    assert result.blockers == ()
    assert [
        (
            item.row.filed.initial_value_inr,
            item.row.filed.peak_value_inr,
            item.row.filed.closing_value_inr,
        )
        for item in result.a3
    ] == [
        (Decimal("413"), Decimal("83"), Decimal("83")),
        (Decimal("413"), Decimal("83"), Decimal("0")),
    ] * 2
    assert [item.row.peak_value_inr for item in result.a3] == [Decimal("82.5")] * 4
    assert [
        (lot.quantity, getattr(lot, "quantity_exact", None)) for lot in (*open_lots, *closed_lots)
    ] == [(Decimal("0.5"), None)] * 4


@pytest.mark.parametrize(
    "exact_quantity",
    [
        pytest.param(None, id="dropped"),
        pytest.param(Fraction(2, 3) + Fraction(1, 10**61), id="drifted"),
    ],
)
def test_pro_rata_fragments_must_conserve_their_exact_quantity(
    monkeypatch: pytest.MonkeyPatch, exact_quantity: Fraction | None
) -> None:
    """#1263: A3 splits by the exact quantities, so the lot-conservation check
    adds those too. The repro's first open 2/3 dropping its exact quantity, or
    drifting from it, refuses with the existing complete message, though its
    Decimal quantity and cost still add back."""
    reconcile = robinhood_fa_mod.reconcile_robinhood_security_lots

    def drifted(quantities: Any) -> RobinhoodSecurityLotResult:
        lots = reconcile(quantities)
        first, *rest = lots.open_fragments
        assert first.exact_quantity == Fraction(2, 3)
        return replace(lots, open_fragments=(replace(first, exact_quantity=exact_quantity), *rest))

    monkeypatch.setattr(robinhood_fa_mod, "reconcile_robinhood_security_lots", drifted)

    with pytest.raises(robinhood_fa_mod.RobinhoodFaError) as raised:
        _compute_statement_total(_same_day_sale(), total="3.40", price="1.70", yahoo="1.70")

    assert str(raised.value) == "economic lot fragments do not conserve source quantity and cost"
    assert raised.value.__cause__ is None
    assert raised.value.__context__ is None


# ---- #1250: the Dec-31 cross-check compares the exact Yahoo total -----------

# For one share: a 60-digit statement total just under $1000, and a 61-digit
# Yahoo close exactly the 0.015 tolerance above it.
_EDGE_STATEMENT_TOTAL = "999.99" + "0" * 54 + "7"
_EDGE_YAHOO_CLOSE = "1000.005" + "0" * 53 + "7"


def test_cross_check_at_the_tolerance_compares_the_exact_yahoo_total() -> None:
    """#1250's newly accepted input, through Robinhood: the exact Yahoo total
    is the tolerance above the statement total, so the lot has no blocker and
    files the exact statement total x ₹82.50. Robinhood computes at 60 digits:
    main rounded the Yahoo total up by 3E-57, the difference exceeded the
    tolerance and the lot got an A3_CLOSING blocker. The wider context only
    builds the fixture's statement sums exactly; compute sets its own."""
    with localcontext(Context(prec=200)):
        result = _compute_statement_total(
            (_buy("1"),), total=_EDGE_STATEMENT_TOTAL, price="999.99", yahoo=_EDGE_YAHOO_CLOSE
        )

    assert result.blockers == ()
    [item] = result.a3
    assert (
        item.row.closing_value_inr.compare_total(
            Decimal("82499.17500000000000000000000000000000000000000000000000000057750")
        )
        == 0
    )
    assert item.row.filed.closing_value_inr == Decimal("82499")


# ---- #1183: FA's Dec-31 position sum never sees a negative equity row --------


def _dec31_rows(*quantities: str) -> robinhood_fa_mod.RobinhoodFaResult:
    """``_compute_open(_prices())`` with account 0's Dec-31 statement printing
    these Securities Held rows of its security at $15 a share in place of the
    one 2-share position. FA reconciles the quantities itself."""
    paired, accounts = _paired(sold=False, dividend=False)
    account = paired.accounts[0]
    statement = account.activity.primary_bundle.statements[0]
    (position,) = statement.positions
    rows = tuple(
        replace(
            position,
            location=replace(position.location, row=row),
            quantity=Decimal(quantity),
            market_value_usd=Decimal(quantity) * position.price_usd,
        )
        for row, quantity in enumerate(quantities, start=1)
    )
    bundle = replace(
        account.activity.primary_bundle, statements=(replace(statement, positions=rows),)
    )
    activity = replace(account.activity, primary_bundle=bundle)
    forged = replace(
        paired,
        accounts=(
            replace(
                account,
                activity=activity,
                cash=replace(account.cash, activity_reconciliation=activity),
            ),
            paired.accounts[1],
        ),
    )
    return compute_robinhood_fa(
        forged,
        config=_config(accounts),
        securities=configured_securities(_config(accounts)),
        income=_income(dividend=False),
        options=_options(),
        calendar_year=2025,
        fx_policy=_FlatFx(),
        prices_by_ticker=_prices(),
        entity_overrides=_entity(),
    )


def test_dec31_positive_rows_keep_their_filed_value() -> None:
    result = _dec31_rows("1", "1")

    assert result.blockers == ()
    assert [item.row.closing_value_inr for item in result.a3] == [Decimal("2400")]


def test_dec31_negative_row_refuses_before_fa_sums_the_positions() -> None:
    """The +3 and -1 rows net to the 2 held shares and their $30, so before
    #1183 FA filed the positive control's row. FA sums the Dec-31 positions
    only after reconciling the quantities, whose sign check now refuses."""
    with pytest.raises(RobinhoodReconciliationError) as raised:
        _dec31_rows("3", "-1")

    assert str(raised.value) == NEGATIVE_POSITION_REFUSAL.format(
        account_index=0, section="Securities Held in Account", page=1, row=2, column="Qty"
    )


def test_internal_transfer_remains_one_economic_a3_holding() -> None:
    paired, accounts = _paired_transfer()
    result = compute_robinhood_fa(
        paired,
        config=_config(accounts),
        securities=configured_securities(_config(accounts)),
        income=_income(dividend=False),
        options=_options(),
        calendar_year=2025,
        fx_policy=_FlatFx(),
        prices_by_ticker=_prices(),
        entity_overrides=_entity(),
    )
    assert result.blockers == ()
    assert len(result.a3) == 1
    assert result.a3[0].account_indices == (0, 1)
    assert result.a3[0].row.initial_value_inr == Decimal("1600")
    assert result.a3[0].row.closing_value_inr == Decimal("2400")


def test_disposal_after_dec31_remains_open_for_schedule_fa_year() -> None:
    paired, accounts = _paired_future_sale()
    result = compute_robinhood_fa(
        paired,
        config=_config(accounts),
        securities=configured_securities(_config(accounts)),
        income=_income(dividend=False),
        options=_options(),
        calendar_year=2025,
        fx_policy=_FlatFx(),
        prices_by_ticker=_prices(),
        entity_overrides=_entity(),
    )
    assert result.blockers == ()
    assert len(result.a3) == 1
    assert result.a3[0].row.audit_disposal_date is None
    assert result.a3[0].row.closing_value_inr == Decimal("2400")


def test_next_year_round_trip_does_not_block_prior_schedule_fa_year() -> None:
    paired, accounts = _paired_next_year_round_trip()
    result = compute_robinhood_fa(
        paired,
        config=_config(accounts),
        securities=configured_securities(_config(accounts)),
        income=_income(dividend=False),
        options=_options(),
        calendar_year=2025,
        fx_policy=_FlatFx(),
        prices_by_ticker=_prices(),
        entity_overrides=_entity(),
    )
    assert result.blockers == ()
    assert result.a3 == ()


def test_anchor_only_option_requires_manual_table_review() -> None:
    manual, blockers = _option_manual_work(_anchor_only_options(classified=True), 2025)
    assert RobinhoodFaManualKind.OPTION_TABLE_D in {item.kind for item in manual}
    assert blockers == ()


def test_business_option_a2_disposition_needs_no_fa_manual_work() -> None:
    options = _anchor_only_options(classified=True)
    options = replace(
        options,
        classifications=(
            replace(
                options.classifications[0],
                treatment=RobinhoodOptionTreatment.SPECULATIVE_BUSINESS,
                schedule_fa_disposition=RobinhoodOptionFaDisposition.A2_ONLY,
            ),
        ),
    )

    manual, blockers = _option_manual_work(options, 2025)

    assert manual == ()
    assert blockers == ()


def test_option_sell_cash_files_as_a2_proceeds() -> None:
    """#1383: an option sell-to-close's cash files as the account's A2
    ``PROCEEDS`` at the December 31 TTBR, as a stock sale's Amount does. Main
    left ``PROCEEDS`` unresolved and kept the cash only for review."""
    paired, _accounts = _paired(sold=False, dividend=False)
    contract = RobinhoodOptionContract(
        account_index=0,
        symbol=_SYMBOL,
        expiration=date(2025, 12, 18),
        right="Call",
        strike_usd=Decimal("100"),
    )
    options = replace(
        _options(),
        trades=(
            SimpleNamespace(
                contract=contract,
                activity_date=date(2025, 6, 1),
                kind=RobinhoodEventKind.OPTION_SELL_TO_CLOSE,
                exact_cash_usd=Decimal("3"),
            ),
        ),
    )

    gross = _a2_gross_evidence(
        paired,
        _income(dividend=False),
        options,
        account_index=0,
        calendar_year=2025,
        fx_policy=_FlatFx(),
    )

    assert gross.known_components == {FaA2NatureOfAmount.PROCEEDS: Decimal("240")}
    assert gross.unresolved_natures == ()


def _compute_sale_and_option_sell(
    monkeypatch: pytest.MonkeyPatch, *, blocked: bool, stand_in_peaks: bool = True
) -> robinhood_fa_mod.RobinhoodFaResult:
    """Schedule FA at ₹80/$ (₹90 on December 31) for account 111111111's June
    sale of 1 share for $14.99 beside a $3.00 option sell-to-close, classified
    as business income filed in A2 only. ``blocked`` adds an option blocker.
    The A2 peak is a stand-in unless ``stand_in_peaks`` is false: the real one
    would need the option's prices, and it refuses any option blocker first
    (#802)."""
    if stand_in_peaks:
        _stand_in_a2_peaks(monkeypatch)
    paired, accounts = _paired(sold=True, dividend=False)
    classified = _anchor_only_options(classified=True).classifications[0]
    options = replace(
        _options(),
        trades=(
            SimpleNamespace(
                contract=RobinhoodOptionContract(
                    account_index=0,
                    symbol=_SYMBOL,
                    expiration=date(2025, 12, 18),
                    right="Call",
                    strike_usd=Decimal("100"),
                ),
                activity_date=date(2025, 6, 1),
                kind=RobinhoodEventKind.OPTION_SELL_TO_CLOSE,
                exact_cash_usd=Decimal("3.00"),
            ),
        ),
        classifications=(
            replace(
                classified,
                treatment=RobinhoodOptionTreatment.SPECULATIVE_BUSINESS,
                schedule_fa_disposition=RobinhoodOptionFaDisposition.A2_ONLY,
            ),
        ),
        blockers=(
            (
                RobinhoodOptionBlocker(
                    kind=RobinhoodOptionBlockerKind.OPEN_BUSINESS_POSITION, account_index=0
                ),
            )
            if blocked
            else ()
        ),
    )
    return compute_robinhood_fa(
        paired,
        config=_config(accounts),
        securities=configured_securities(_config(accounts)),
        income=_income(dividend=False),
        options=options,
        calendar_year=2025,
        fx_policy=_ClosingDateFx(),
        prices_by_ticker=_prices(),
        entity_overrides=_entity(),
    )


def _first_account_a2(
    result: robinhood_fa_mod.RobinhoodFaResult,
) -> list[tuple[FaA2NatureOfAmount | None, Decimal | None]]:
    """Account 111111111's A2 rows as (nature, gross amount) pairs."""
    return [
        (row.nature_of_amount, row.gross_amount_inr)
        for row in result.a2_rows
        if row.account_number == "111111111"
    ]


def test_sale_and_option_sell_cash_file_one_a2_proceeds_row(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#1383: a stock sale and an option sell-to-close in the calendar year
    file one A2 ``PROCEEDS`` row, the sum of both cash amounts at the
    December 31 TTBR: ($14.99 + $3.00) x ₹90 = ₹1,619.10, with no
    ``a2-gross-credits`` task. Main filed a blank placeholder row instead."""
    result = _compute_sale_and_option_sell(monkeypatch, blocked=False)

    assert result.blockers == ()
    assert _first_account_a2(result) == [(FaA2NatureOfAmount.PROCEEDS, Decimal("1619.10"))]
    assert result.manual_work == ()


_OPTION_BLOCKER_AT_A2_GROSS = (
    "a Robinhood option blocker reached A2 gross evidence; "
    "compute_robinhood_a2_peaks must refuse first"
)


def test_option_blocker_reaching_a2_gross_evidence_is_an_invariant_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#1411: the A2 peak refuses any option blocker before A2 is built, so
    one that reaches A2 gross evidence, here past the stand-in peaks, is a
    bug. It refuses with fixed text that keeps the context key only, where
    main left the account's A2 ``PROCEEDS`` unresolved for manual entry."""
    with pytest.raises(InvariantError) as raised:
        _compute_sale_and_option_sell(monkeypatch, blocked=True)

    assert_unchained_refusal(
        raised.value,
        InvariantError(what=_OPTION_BLOCKER_AT_A2_GROSS, context=(("blockers", None),)),
        f"InvariantError: {_OPTION_BLOCKER_AT_A2_GROSS} (context keys: blockers)",
        markers=(),
    )


def test_option_blocker_refuses_at_the_a2_peak_before_a2_gross_evidence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#1411 control: with the real A2 peak, the same blocked run refuses
    there first, so no real run reaches the invariant above."""
    with pytest.raises(RobinhoodA2PeakError) as raised:
        _compute_sale_and_option_sell(monkeypatch, blocked=True, stand_in_peaks=False)

    assert str(raised.value).startswith("Robinhood option blockers prevent the A2 peak: ")


def test_stock_lending_income_is_an_a2_credit_at_the_dec31_rate() -> None:
    """The A2 other-income credit's own Dec-31 lookup, when it is the account's
    first credit: one of Schedule FA's FX calls robinhood-fa still makes (#1145)."""
    paired, _accounts = _paired(sold=False, dividend=False)
    loan_fee = SimpleNamespace(payment_date=date(2025, 6, 2), amount_gross=Decimal("3"))
    income = replace(
        _income(dividend=False),
        stock_lending_payments=(
            SimpleNamespace(
                event_reference=RobinhoodIncomeEventReference(0, 9),
                loan_fee_income=loan_fee,
                substitute_dividend_income=None,
            ),
        ),
    )

    gross = _a2_gross_evidence(
        paired,
        income,
        _options(),
        account_index=0,
        calendar_year=2025,
        fx_policy=_ClosingDateFx(),
    )

    assert gross.known_components == {FaA2NatureOfAmount.OTHER_INCOME: Decimal("270")}


def _interest_payment(
    account: int, row: int, amount: str, *, credited: date, program_bank: bool | None
) -> RobinhoodInterestPayment:
    """``account``'s interest credit of ``amount`` at activity row ``row``."""
    return RobinhoodInterestPayment(
        event_reference=RobinhoodIncomeEventReference(account, row),
        program_bank=program_bank,
        in_financial_year=True,
        interest_credit=InterestCredit(
            credit_date=credited,
            amount_gross=Decimal(amount),
            currency="USD",
            description="Synthetic interest",
            source_row_index=row,
        ),
    )


def _paired_with_interest_earned(
    usd: str,
) -> tuple[RobinhoodAccountsReconciliation, tuple[RobinhoodAccountConfig, RobinhoodAccountConfig]]:
    """Account 111111111's 2025 statement with ``usd`` Interest Earned, this
    period and year to date, so that much 2025 interest reconciles."""
    paired, accounts = _paired(sold=False, dividend=False)
    first = paired.accounts[0]
    [statement] = first.activity.primary_bundle.statements
    statement = replace(
        statement,
        income=tuple(
            replace(row, this_period_usd=Decimal(usd), year_to_date_usd=Decimal(usd))
            if row.label == "Interest Earned"
            else row
            for row in statement.income
        ),
    )
    bundle = replace(first.activity.primary_bundle, statements=(statement,))
    return replace(
        paired,
        accounts=(
            replace(first, activity=replace(first.activity, primary_bundle=bundle)),
            paired.accounts[1],
        ),
    ), accounts


def test_a2_dividends_and_resolved_interest_file_their_dec31_totals() -> None:
    """#1534: account 111111111's 2025 dividends on two securities and its
    brokerage and Program Bank interest each file one A2 total at the December
    31 TTBR, ($10.25 + $0.37 + $12.34) x ₹90 = ₹2,066.40 and ($1.23 + $0.45) x
    ₹90 = ₹151.20, before a ₹270 lending fee. Another account's and other
    years' records stay out. The shared A2 builder now adds them; 8737c74's own
    loops gave the same totals, in the same order and with the same digits."""
    other_security = _dividend_payment(0, 5, "12.34", paid=date(2025, 9, 15))
    other_security = replace(
        other_security,
        dividend=replace(other_security.dividend, symbol=Ticker("XQRB"), isin=Isin("US99995B1044")),
    )
    income = replace(
        _income(dividend=False),
        dividend_payments=(
            _dividend_payment(0, 1, "7", holding_date=date(2024, 12, 9), paid=date(2024, 12, 16)),
            _dividend_payment(0, 3, "10.25"),
            _dividend_payment(0, 4, "0.37", holding_date=date(2025, 6, 9), paid=date(2025, 6, 16)),
            other_security,
            _dividend_payment(1, 6, "5"),
        ),
        interest_payments=(
            _interest_payment(0, 7, "1.23", credited=date(2025, 1, 31), program_bank=False),
            _interest_payment(0, 8, "0.45", credited=date(2025, 11, 28), program_bank=True),
            _interest_payment(0, 9, "3", credited=date(2026, 1, 30), program_bank=True),
            _interest_payment(1, 10, "2", credited=date(2025, 5, 30), program_bank=True),
        ),
        stock_lending_payments=(
            SimpleNamespace(
                event_reference=RobinhoodIncomeEventReference(0, 11),
                loan_fee_income=SimpleNamespace(
                    payment_date=date(2025, 6, 2), amount_gross=Decimal("3")
                ),
                substitute_dividend_income=None,
            ),
        ),
    )

    paired, _accounts = _paired_with_interest_earned("1.68")
    gross = _a2_gross_evidence(
        paired,
        income,
        _options(),
        account_index=0,
        calendar_year=2025,
        fx_policy=_ClosingDateFx(),
    )

    assert [(nature, str(amount)) for nature, amount in gross.known_components.items()] == [
        (FaA2NatureOfAmount.DIVIDEND, "2066.40"),
        (FaA2NatureOfAmount.INTEREST, "151.20"),
        (FaA2NatureOfAmount.OTHER_INCOME, "270"),
    ]
    assert gross.unresolved_natures == ()


@pytest.mark.parametrize("nature", ["dividend", "interest"])
def test_a2_dividend_or_interest_total_past_the_cap_refuses(nature: str) -> None:
    """#1534: a Robinhood A2 dividend or interest total that can't be added
    exactly refuses through the shared checked addition, naming the second
    in-year payment and its activity row. 8737c74 added each payment's INR
    value in the 28-digit context, which silently dropped ``MINUTE``."""
    if nature == "dividend":
        income = replace(
            _income(dividend=False),
            dividend_payments=(
                _dividend_payment(0, 3, HUGE),
                _dividend_payment(
                    0, 4, MINUTE, holding_date=date(2025, 6, 9), paid=date(2025, 6, 16)
                ),
            ),
        )
        at = "Schedule FA per-symbol dividends, input row 2, column amount_gross"
    else:
        income = replace(
            _income(dividend=False),
            interest_payments=(
                _interest_payment(0, 3, HUGE, credited=date(2025, 1, 31), program_bank=False),
                _interest_payment(0, 4, MINUTE, credited=date(2025, 2, 28), program_bank=False),
            ),
        )
        at = "Schedule FA A2 gross interest, input row 2, column amount_gross"

    with pytest.raises(InvariantError) as raised:
        _a2_gross_evidence(
            _paired(sold=False, dividend=False)[0],
            income,
            _options(),
            account_index=0,
            calendar_year=2025,
            fx_policy=_ClosingDateFx(),
        )

    assert_refusal(raised.value, message(at, source_row_index=4), markers=())


def test_anchor_only_option_without_classification_blocks_completeness() -> None:
    _manual, blockers = _option_manual_work(_anchor_only_options(classified=False), 2025)
    assert RobinhoodFaBlockerKind.OPTION_CLASSIFICATION in {item.kind for item in blockers}


@pytest.mark.parametrize(
    ("opening_cash", "expected"),
    [
        ("100", (Decimal("8000"), date(2025, 1, 1))),
        ("1", (Decimal("900"), _END)),
    ],
    ids=["opening", "period-end"],
)
def test_peak_floor_includes_calendar_year_opening_balance(
    opening_cash: str, expected: tuple[Decimal, date]
) -> None:
    paired, _accounts = _paired(sold=False, dividend=False)
    account = paired.accounts[0]
    statement = account.activity.primary_bundle.statements[0]
    summary = replace(
        statement.account_summary,
        opening_brokerage_cash_usd=Decimal(opening_cash),
        opening_deposit_sweep_usd=Decimal(),
        opening_total_securities_usd=Decimal(),
        opening_portfolio_value_usd=Decimal(opening_cash),
    )
    statement = replace(statement, account_summary=summary)
    bundle = replace(account.activity.primary_bundle, statements=(statement,))
    account = replace(
        account,
        activity=replace(account.activity, primary_bundle=bundle),
    )
    assert _peak_floor(account, 2025, _ClosingDateFx()) == expected


def _quantity_disposal(
    ordinal: int,
    *,
    account_index: int = 0,
    settle_date: date = date(2025, 6, 11),
    quantity: str = "-1",
) -> RobinhoodQuantityMovement:
    reference = RobinhoodSecurityEventReference(account_index, ordinal)
    return RobinhoodQuantityMovement(
        account_index=account_index,
        cusip=_CUSIP,
        activity_date=date(2025, 6, 10),
        settle_date=settle_date,
        signed_quantity=Decimal(quantity),
        kind=RobinhoodQuantityMovementKind.DISPOSAL,
        event_references=(reference,),
    )


def test_grouped_disposal_resolves_complete_singleton_movements() -> None:
    first = _quantity_disposal(4)
    second = _quantity_disposal(5, settle_date=date(2025, 6, 12), quantity="-2")
    references = (*first.event_references, *second.event_references)

    result = robinhood_fa_mod._disposal_movement_for_references(
        references,
        {
            first.event_references: first,
            second.event_references: second,
        },
    )

    assert result is not None
    assert result.event_references == references
    assert result.signed_quantity == Decimal("-3")
    assert result.settle_date == date(2025, 6, 12)


def test_grouped_disposal_matches_fa_and_cg_proceeds_by_source_group() -> None:
    second_buy = replace(
        _buy("1"),
        source_record_index=3,
        activity_date=date(2025, 2, 10),
        process_date=date(2025, 2, 10),
        settle_date=date(2025, 2, 10),
        price_usd=Decimal("12"),
        amount_usd=Decimal("-12"),
    )
    first_sale = replace(
        _sale(),
        activity_date=date(2025, 3, 10),
        process_date=date(2025, 3, 10),
        settle_date=date(2025, 3, 11),
    )
    second_sale = replace(
        _sale(),
        source_record_index=5,
        activity_date=date(2025, 3, 10),
        process_date=date(2025, 3, 10),
        settle_date=date(2025, 3, 12),
        amount_usd=Decimal("15.98"),
    )
    paired, accounts = _paired_events(
        (_buy("1"), second_buy, first_sale, second_sale),
        position_quantity=Decimal(),
    )
    config = _config(accounts)

    fa = compute_robinhood_fa(
        paired,
        config=config,
        securities=configured_securities(config),
        income=_income(dividend=False),
        options=_options(),
        calendar_year=2025,
        fx_policy=_FlatFx(),
        prices_by_ticker=_prices(),
        entity_overrides=_entity(),
    )
    cg = compute_robinhood_capital_gains(
        paired,
        config=config,
        securities=configured_securities(config),
        financial_year_start=date(2024, 4, 1),
        financial_year_end=date(2025, 3, 31),
        fx_policy=AuditedFxStub(Decimal("80")),
    )

    assert not fa.blockers
    assert not cg.blockers
    fa_proceeds = {
        (item.lot_id, item.close_references): item.row.gross_proceeds_from_sale_inr
        for item in fa.a3
    }
    cg_proceeds: defaultdict[
        tuple[
            RobinhoodSecurityEventReference,
            tuple[RobinhoodSecurityEventReference, ...],
        ],
        Decimal,
    ] = defaultdict(Decimal)
    for item in cg.attributed_lots:
        cg_proceeds[
            item.source_slice.lot_id,
            item.source_slice.movement_references,
        ] += item.closed_lot.proceeds_money * Decimal("80")
    assert fa_proceeds == dict(cg_proceeds)
    assert sum((value or Decimal() for value in fa_proceeds.values()), Decimal()) == Decimal(
        "2477.60"
    )
    assert (
        len(
            {
                sale.amount_usd / sale.quantity
                for sale in (first_sale, second_sale)
                if sale.amount_usd is not None and sale.quantity is not None
            }
        )
        == 2
    )


def test_fa_keeps_the_quantity_split_of_a_sale_order_cg_refuses() -> None:
    """#1540: the issue's example, one share bought on 2023-01-03 for $120 and
    one on 2025-01-02 for $20, sold on 2025-06-13 for $100 and $110. CG now
    refuses it (the sale order decides its short/long-term split), and
    Schedule FA still files each lot's $105 quantity share: ₹8,400 at the
    flat ₹80, as on d081de8. Evidence runs from 2023 to the FY 2025-26 end,
    with a statement break at December 31 for A2's closing."""
    start, december, end = date(2023, 1, 1), date(2025, 12, 31), date(2026, 3, 31)

    def trade(base: RobinhoodEvent, ordinal: int, day: date, amount: str) -> RobinhoodEvent:
        return replace(
            base,
            source_record_index=ordinal,
            activity_date=day,
            process_date=day,
            settle_date=day,
            quantity=Decimal("1"),
            price_usd=abs(Decimal(amount)),
            amount_usd=Decimal(amount),
        )

    first = (
        trade(_buy(), 2, date(2023, 1, 3), "-120"),
        trade(_buy(), 3, date(2025, 1, 2), "-20"),
        trade(_sale(), 4, date(2025, 6, 13), "100"),
        trade(_sale(), 5, date(2025, 6, 13), "110"),
    )
    cash = Decimal("70")
    statements = tuple(
        statement
        for number, family, events, closing in (
            ("111111111", RobinhoodStatementFamily.INDIVIDUAL, first, cash),
            ("222222222", RobinhoodStatementFamily.MANAGED_INDIVIDUAL, (_cash(),), Decimal("5")),
        )
        for statement in (
            _statement(
                number, family, events, cash=closing, position_quantity=Decimal(), start=start
            ),
            _statement(
                number,
                family,
                (),
                cash=closing,
                position_quantity=Decimal(),
                start=december + timedelta(days=1),
                end=end,
                opening_cash=closing,
                opening_securities=Decimal(),
            ),
        )
    )
    accounts = (
        replace(_account("111111111"), opening_date=start),
        replace(_account("222222222"), opening_date=start),
    )
    paired = reconcile_robinhood_accounts(
        validate_robinhood_statement_bundles(statements),
        tuple(
            RobinhoodActivityReportEvidence(events=events, start_date=start, end_date=end)
            for events in (first, (_cash(),))
        ),
        accounts,
    )
    config = _config(accounts)

    fa = compute_robinhood_fa(
        paired,
        config=config,
        securities=configured_securities(config),
        income=_income(dividend=False),
        options=_options(),
        calendar_year=2025,
        fx_policy=_FlatFx(),
        prices_by_ticker=_prices(),
        entity_overrides=_entity(),
    )
    cg = compute_robinhood_capital_gains(
        paired,
        config=config,
        securities=configured_securities(config),
        financial_year_start=date(2025, 4, 1),
        financial_year_end=end,
        fx_policy=AuditedFxStub(Decimal("85")),
    )

    assert fa.blockers == ()
    assert fa.manual_work == ()
    assert [
        (item.lot_id.event_ordinal, item.row.gross_proceeds_from_sale_inr) for item in fa.a3
    ] == [(2, Decimal("8400")), (3, Decimal("8400"))]
    assert [blocker.kind.value for blocker in cg.blockers] == ["ordering-ambiguity"]


def test_exact_grouped_disposal_wins_without_reconstruction() -> None:
    first = _quantity_disposal(4)
    second = _quantity_disposal(5)
    references = (*first.event_references, *second.event_references)
    grouped = replace(
        first,
        signed_quantity=Decimal("-2"),
        event_references=references,
    )

    assert (
        robinhood_fa_mod._disposal_movement_for_references(
            references,
            {references: grouped},
        )
        is grouped
    )


def test_grouped_disposal_refuses_missing_or_mixed_singletons() -> None:
    first = _quantity_disposal(4)
    other_account = _quantity_disposal(5, account_index=1)
    references = (*first.event_references, *other_account.event_references)

    assert (
        robinhood_fa_mod._disposal_movement_for_references(
            references,
            {first.event_references: first},
        )
        is None
    )
    with pytest.raises(robinhood_fa_mod.RobinhoodFaError, match="reuses"):
        robinhood_fa_mod._disposal_movement_for_references(
            (*first.event_references, *first.event_references),
            {first.event_references: first},
        )
    with pytest.raises(RobinhoodReconciliationError, match="contradict"):
        robinhood_fa_mod._disposal_movement_for_references(
            references,
            {
                first.event_references: first,
                other_account.event_references: other_account,
            },
        )
    second = _quantity_disposal(5)
    with pytest.raises(robinhood_fa_mod.RobinhoodFaError, match="omits"):
        robinhood_fa_mod._disposal_movement_for_references(
            first.event_references,
            {
                first.event_references: first,
                second.event_references: second,
            },
        )


def test_same_lot_same_sale_fragments_are_aggregated_once() -> None:
    lot_id = RobinhoodSecurityEventReference(0, 2)
    movement = (RobinhoodSecurityEventReference(0, 4),)
    slices = (
        RobinhoodLotSlice(
            lot_id=lot_id,
            account_index=0,
            cusip=_CUSIP,
            movement_references=movement,
            quantity=Decimal("3"),
            cost_usd=Decimal("30"),
            original_date=date(2025, 1, 10),
            entry_date=date(2025, 1, 10),
            kind=RobinhoodLotSliceKind.DISPOSAL,
        ),
        RobinhoodLotSlice(
            lot_id=lot_id,
            account_index=0,
            cusip=_CUSIP,
            movement_references=movement,
            quantity=Decimal("1"),
            cost_usd=Decimal("10"),
            original_date=date(2025, 1, 10),
            entry_date=date(2025, 8, 1),
            kind=RobinhoodLotSliceKind.DISPOSAL,
        ),
    )
    grouped = _group_disposal_slices(slices)
    assert len(grouped) == 1
    assert grouped[0].quantity == Decimal("4")
    assert grouped[0].cost_usd == Decimal("40")


def test_grouped_disposal_keeps_the_exact_sum_of_decimal_exact_costs() -> None:
    """#1243: whole slices from two transfers each have exact Decimal costs,
    but their sum needs a rational even though neither part needed one.
    """
    slices = tuple(
        RobinhoodLotSlice(
            lot_id=RobinhoodSecurityEventReference(0, 2),
            account_index=1,
            cusip=_CUSIP,
            movement_references=(RobinhoodSecurityEventReference(1, 7),),
            quantity=cost,
            cost_usd=cost,
            original_date=date(2025, 1, 10),
            entry_date=day,
            kind=RobinhoodLotSliceKind.DISPOSAL,
        )
        for cost, day in (
            (Decimal(1), date(2025, 2, 10)),
            (Decimal("2E-60"), date(2025, 3, 10)),
        )
    )
    with localcontext(Context(prec=60)):
        (grouped,) = _group_disposal_slices(slices)
    assert grouped.cost_usd == Decimal(1)
    assert grouped.exact_cost_usd == Fraction(1) + Fraction("2E-60")
    # Decimal-only quantities keep main's sum even when it rounds.
    assert grouped.quantity == Decimal(1)
    assert getattr(grouped, "exact_quantity", None) is None


def test_automatic_peak_keeps_fractional_inr() -> None:
    paired, accounts = _paired(sold=False, dividend=False)

    class _FractionalFx(FxPolicy):
        currency = "USD"

        @override
        def rate_for(self, event_date: date) -> Decimal:
            return Decimal("85.4123")

    config = _config(accounts)
    result = compute_robinhood_fa(
        paired,
        config=config,
        securities=configured_securities(config),
        income=_income(dividend=False),
        options=_options(),
        calendar_year=2025,
        fx_policy=_FractionalFx(),
        prices_by_ticker=_prices(),
        entity_overrides=_entity(),
    )
    first = next(item for item in result.a2 if item.row.account_number == "111111111")
    assert first.row.peak_balance_inr == Decimal("854.1230")
    assert first.row.audit_peak_override_source is None


def test_blocked_income_never_files_false_no_amount_or_zero_a3_credit() -> None:
    paired, accounts = _paired(sold=False, dividend=False)
    result = compute_robinhood_fa(
        paired,
        config=_config(accounts),
        securities=configured_securities(_config(accounts)),
        income=_income(dividend=False, blocked=True),
        options=_options(),
        calendar_year=2025,
        fx_policy=_FlatFx(),
        prices_by_ticker=_prices(),
        entity_overrides=_entity(),
    )

    first = next(item for item in result.a2 if item.row.account_number == "111111111")
    assert first.row.nature_of_amount is None
    assert first.row.gross_amount_inr is None
    assert result.a3[0].row.gross_amount_paid_credited_inr is None


@pytest.mark.parametrize(
    "kind",
    [RobinhoodIncomeBlockerKind.JURISDICTION, RobinhoodIncomeBlockerKind.INTEREST_PAYER_COUNTRY],
)
def test_ftc_only_blocker_does_not_suppress_fa_credits(kind: RobinhoodIncomeBlockerKind) -> None:
    paired, accounts = _paired(sold=False, dividend=True)
    income = _income(dividend=True)
    blocked = replace(
        income,
        blockers=(
            RobinhoodIncomeBlocker(
                kind=kind,
                account_indices=(0,),
                event_references=(RobinhoodIncomeEventReference(0, 3),),
                tax_years=(2025,),
            ),
        ),
    )
    actual = compute_robinhood_fa(
        paired,
        config=_config(accounts),
        securities=configured_securities(_config(accounts)),
        income=blocked,
        options=_options(),
        calendar_year=2025,
        fx_policy=_FlatFx(),
        prices_by_ticker=_prices(),
        entity_overrides=_entity(),
    )
    expected = compute_robinhood_fa(
        paired,
        config=_config(accounts),
        securities=configured_securities(_config(accounts)),
        income=income,
        options=_options(),
        calendar_year=2025,
        fx_policy=_FlatFx(),
        prices_by_ticker=_prices(),
        entity_overrides=_entity(),
    )

    assert actual == expected


def test_program_bank_presence_is_disclosed_in_a2_without_a1_manual_work() -> None:
    paired, accounts = _paired(sold=False, dividend=False)
    first = paired.accounts[0]
    forged_cash = replace(
        first.cash,
        periods=(replace(first.cash.periods[0], program_bank_present=True),),
    )
    paired = replace(
        paired,
        accounts=(replace(first, cash=forged_cash), paired.accounts[1]),
    )
    result = compute_robinhood_fa(
        paired,
        config=_config(accounts),
        securities=configured_securities(_config(accounts)),
        income=_income(dividend=False),
        options=_options(),
        calendar_year=2025,
        fx_policy=_FlatFx(),
        prices_by_ticker=_prices(),
        entity_overrides=_entity(),
    )

    assert result.program_bank
    assert not result.manual_work
    assert result.cbdt_json_eligible


def test_program_bank_interest_is_an_a2_credit() -> None:
    paired, accounts = _paired_with_interest_earned("1")
    income = replace(
        _income(dividend=False),
        interest_payments=(
            RobinhoodInterestPayment(
                event_reference=RobinhoodIncomeEventReference(0, 99),
                program_bank=True,
                in_financial_year=True,
                interest_credit=InterestCredit(
                    credit_date=date(2025, 12, 1),
                    amount_gross=Decimal("1"),
                    currency="USD",
                    description="Program Bank interest",
                ),
            ),
        ),
    )

    result = compute_robinhood_fa(
        paired,
        config=_config(accounts),
        securities=configured_securities(_config(accounts)),
        income=income,
        options=_options(),
        calendar_year=2025,
        fx_policy=_ClosingDateFx(),
        prices_by_ticker=_prices(),
        entity_overrides=_entity(),
    )

    assert result.a2[0].known_gross_components_inr == {
        FaA2NatureOfAmount.INTEREST.value: Decimal("90")
    }
    assert result.a2[0].row.nature_of_amount is FaA2NatureOfAmount.INTEREST


def _security_receipt() -> RobinhoodEvent:
    return RobinhoodEvent(
        kind=RobinhoodEventKind.SECURITY_RECEIPT,
        source_record_index=3,
        activity_date=date(2025, 2, 10),
        process_date=date(2025, 2, 10),
        settle_date=date(2025, 2, 10),
        instrument=_SYMBOL,
        description=f"Synthetic Corporation\nCUSIP: {_CUSIP}",
        trans_code="REC",
        quantity=Decimal("1"),
        quantity_suffix=None,
        price_usd=None,
        amount_usd=None,
        issuer_name="Synthetic Corporation",
        cusip=_CUSIP,
    )


def test_unresolved_security_receipt_prompts_for_its_resolution() -> None:
    # Two bought shares plus one received share are held at the period end.
    paired, accounts = _paired_events(
        (_buy(), _security_receipt()),
        position_quantity=Decimal("3"),
    )

    with pytest.raises(
        RobinhoodReconciliationError,
        match=r"\[\[robinhood\.security_receipt_resolutions\]\]",
    ) as caught:
        compute_robinhood_fa(
            paired,
            config=_config(accounts),
            securities=configured_securities(_config(accounts)),
            income=_income(dividend=False),
            options=_options(),
            calendar_year=2025,
            fx_policy=_FlatFx(),
        )
    assert "source_record_index = 3" in str(caught.value)


@pytest.mark.parametrize(
    ("short", "padded"),
    [
        pytest.param(Decimal("1"), Decimal("1.0000"), id="1-for-1.0000"),
        pytest.param(Decimal("0.05"), Decimal("0.0500"), id="0.05-for-0.0500"),
    ],
)
def test_receipt_exact_quantity_without_trailing_zeros_files_the_same_values(
    tmp_path: Path, short: Decimal, padded: Decimal
) -> None:
    """Before #1186 an exact REC quantity spelled without its display's
    trailing zeros refused. It now files the same A2 and A3 values, byte for
    byte, as the padded spelling of the same number."""
    receipt = replace(_security_receipt(), quantity=padded)
    paired, accounts = _paired_events((_buy(), receipt), position_quantity=2 + padded)
    filed = []
    for spelling, exact in (("short", short), ("padded", padded)):
        resolution = RobinhoodSecurityReceiptResolution(
            account_number="111111111",
            source_record_index=receipt.source_record_index,
            cusip=_CUSIP,
            treatment=RobinhoodSecurityReceiptTreatment.PROMOTIONAL_REWARD,
            exact_quantity=exact,
            reward_fair_market_value_usd=Decimal("5"),
            income_reported_financial_year=2024,
            income_reported=True,
            source="synthetic reward confirmation",
            reason="synthetic promotional reward",
        )
        config = replace(_config(accounts), security_receipt_resolutions=(resolution,))
        result = compute_robinhood_fa(
            paired,
            config=config,
            securities=configured_securities(config),
            income=_income(dividend=False),
            options=_options(),
            calendar_year=2025,
            fx_policy=_FlatFx(),
            prices_by_ticker=_prices(),
            entity_overrides=_entity(),
        )
        assert (result.blockers, result.manual_work) == ((), ())
        files: dict[str, bytes] = {}
        for format_id in ("filing-csv", "cbdt-json-fragment"):
            output_dir = tmp_path / spelling / format_id
            output_dir.mkdir(parents=True)
            written, complete, _missing = write_fa(
                result,
                output_dir=output_dir,
                format_id=format_id,
                financial_year=FinancialYear(2025),
                itr_form="ITR-3",
                json_available=True,
                json_cleanup=ClaimedOutputCleanup(output_dir / JSON_FILENAME),
            )
            assert complete is True
            files.update({path.name: path.read_bytes() for path in written})
        filed.append(files)

    assert set(filed[0]) == {"schedule-fa-a2.csv", "schedule-fa-a3.csv", JSON_FILENAME}
    assert filed[0] == filed[1]


@private_test
def test_private_population_succeeds_or_refuses_with_a_typed_error() -> None:
    paired = load_private_accounts_reconciliation()
    accounts = tuple(
        robinhood_account_config(
            account.activity.primary_bundle.account_number,
            opening_date=account.evidence.start_date,
            address="private statement",
            zip_code="",
            source="private statement",
        )
        for account in paired.accounts
    )
    config = RobinhoodConfig(accounts=accounts)
    try:
        compute_robinhood_fa(
            paired,
            config=config,
            securities=configured_securities(config),
            income=_income(dividend=False, blocked=True),
            options=_options(),
            calendar_year=2025,
            fx_policy=_FlatFx(),
        )
    except IbkrFaError:
        untyped = False
    except Exception:
        untyped = True
    else:
        untyped = False
    # Failing after the except block keeps pytest from printing the source
    # exception.
    if untyped:
        pytest.fail("private Schedule FA run failed with an untyped error", pytrace=False)


# ---- #1246: a Dec-31 pending trade files on its proved trade-date quantity ----

_SETTLEMENT_DATE = RobinhoodPositionSnapshotBasis.SETTLEMENT_DATE
_JANUARY_LOT = RobinhoodSecurityEventReference(0, 2)
_DEC31_LOT = RobinhoodSecurityEventReference(0, 5)
# A sale of 1 share for $15.
_SELL_ONE = replace(_sale(), amount_usd=Decimal("15"))
_CLOSE_MISMATCH = (
    "Dec-31 close mismatch: the statement and yahoo_fallback totals differ by more than "
    "the tolerance; refusing rather than file an unverified closing value"
)


def _paired_pending(
    settled: tuple[RobinhoodEvent, ...],
    trade: RobinhoodEvent,
    *,
    shown: str,
    row: bool = True,
):
    """``_paired_events`` for ``settled`` and ``trade``, where ``trade`` is
    activity report row 5, traded 2025-12-31 and settling 2026-01-02. The
    December statement lists only ``settled`` and their cash, shows ``shown``
    shares worth $15 each and, with ``row``, the trade's Pending Trades row."""
    pending = dec31_trade(trade, 5)
    second = (_cash(),)
    first = _statement(
        "111111111",
        RobinhoodStatementFamily.INDIVIDUAL,
        settled,
        cash=sum((event.amount_usd or Decimal() for event in settled), Decimal()),
        position_quantity=Decimal(shown),
    )
    if row:
        first = replace(first, activities=(*first.activities, pending_row(pending)))
    statements = (
        first,
        _statement(
            "222222222",
            RobinhoodStatementFamily.MANAGED_INDIVIDUAL,
            second,
            cash=Decimal("5"),
            position_quantity=Decimal(),
        ),
    )
    reports = tuple(
        RobinhoodActivityReportEvidence(events=events, start_date=_OPENING, end_date=_END)
        for events in ((*settled, pending), second)
    )
    accounts = (_account("111111111"), _account("222222222"))
    bundles = validate_robinhood_statement_bundles(statements)
    return reconcile_robinhood_accounts(bundles, reports, accounts), accounts


def _repro(*, row: bool = True):
    """The #1246 repro: 2 shares bought for $20 on 2025-01-10, and 1 for $10
    traded 2025-12-31 and settling 2026-01-02. The statement shows 2 shares
    worth $30."""
    return _paired_pending((_buy(),), _buy("1"), shown="2", row=row)


def _fa(
    paired: RobinhoodAccountsReconciliation,
    accounts: tuple[RobinhoodAccountConfig, RobinhoodAccountConfig],
    *,
    yahoo: str = "15",
) -> robinhood_fa_mod.RobinhoodFaResult:
    """Schedule FA at a flat ₹80/$ against a Yahoo close of ``yahoo``."""
    return compute_robinhood_fa(
        paired,
        config=_config(accounts),
        securities=configured_securities(_config(accounts)),
        income=_income(dividend=False),
        options=_options(),
        calendar_year=2025,
        fx_policy=_FlatFx(),
        prices_by_ticker=_prices(yahoo),
        entity_overrides=_entity(),
    )


def _dec31_anchor(
    paired: RobinhoodAccountsReconciliation,
) -> tuple[RobinhoodPositionSnapshotBasis, Decimal, Decimal, Decimal]:
    """The one Dec-31 anchor's basis and its statement, trade-date and
    settlement-date quantities, as compute resolves them."""
    identities = resolve_robinhood_security_identities(paired, (_security(),))
    quantities = resolve_robinhood_security_quantities(paired, identities, ())
    (anchor,) = (anchor for anchor in quantities.anchors if anchor.period_end == _END)
    return (
        anchor.snapshot_basis,
        anchor.statement_quantity,
        anchor.trade_date_quantity,
        anchor.settlement_date_quantity,
    )


def _closing_blocker(
    *lots: RobinhoodSecurityEventReference,
) -> robinhood_fa_mod.RobinhoodFaBlocker:
    """The security's A3_CLOSING blocker, naming ``lots``."""
    return robinhood_fa_mod.RobinhoodFaBlocker(
        kind=RobinhoodFaBlockerKind.A3_CLOSING, security_config_indices=(0,), lot_ids=lots
    )


def _assert_pending_markers_in(paired: RobinhoodAccountsReconciliation) -> None:
    """The run's input holds both pending markers, on its one Pending Trades row."""
    statement = paired.accounts[0].activity.primary_bundle.statements[0]
    assert [
        (row.description, row.symbol)
        for row in statement.activities
        if row.location.section is RobinhoodStatementSection.PENDING_TRADES
    ] == [PENDING_MARKERS]


def test_dec31_pending_buy_files_its_trade_date_quantity(tmp_path: Path) -> None:
    """#1246 repro: the statement shows 2 shares worth $30 and its Pending
    Trades row proves 3 on the trade date. A3 values the 3 trade-dated shares
    at $30 x 3/2 = $45: the January lot's 2 are $30 (₹2,400 at 80) and the
    Dec-31 lot's 1 is $15 (₹1,200), which is also its peak. Main compared the
    3 shares with the statement's 2 and blocked A3."""
    paired, accounts = _repro()
    assert _dec31_anchor(paired) == (_SETTLEMENT_DATE, Decimal(2), Decimal(3), Decimal(2))

    result = _fa(paired, accounts)

    assert result.blockers == ()
    rows = result.a3_rows
    assert [row.date_of_acquiring for row in rows] == [date(2025, 1, 10), TRADED]
    for values in (rows, [row.filed for row in rows]):
        assert [
            (item.initial_value_inr, item.peak_value_inr, item.closing_value_inr) for item in values
        ] == [
            (Decimal(1600), Decimal(2400), Decimal(2400)),
            (Decimal(800), Decimal(1200), Decimal(1200)),
        ]
    written, complete, _missing = write_fa(
        result,
        output_dir=tmp_path,
        format_id="filing-csv",
        financial_year=FinancialYear(2025),
        itr_form="ITR-3",
        json_available=True,
        json_cleanup=ClaimedOutputCleanup(tmp_path / JSON_FILENAME),
    )
    path = tmp_path / "schedule-fa-a3.csv"
    assert complete is True
    assert path in written
    assert [
        _a3_column(path, name)
        for name in ("Peak value of investment during the Period", "Closing balance")
    ] == [["2400", "1200"], ["2400", "1200"]]


@pytest.mark.parametrize(
    ("settled", "trade", "shown", "basis", "settled_shown", "shares", "closing"),
    [
        pytest.param(
            (_buy(),), _buy("1"), "2", _SETTLEMENT_DATE, "3", (30, 15), (2400, 1200), id="buy"
        ),
        pytest.param(
            (_buy("3"),), _SELL_ONE, "3", _SETTLEMENT_DATE, "2", (30,), (2400, 0), id="sale"
        ),
        pytest.param(
            (_buy(),),
            _buy("1"),
            "3",
            RobinhoodPositionSnapshotBasis.TRADE_DATE,
            "3",
            (30, 15),
            (2400, 1200),
            id="trade-date-basis",
        ),
    ],
)
def test_dec31_pending_trade_files_as_the_same_trade_settled(
    monkeypatch: pytest.MonkeyPatch,
    settled: tuple[RobinhoodEvent, ...],
    trade: RobinhoodEvent,
    shown: str,
    basis: RobinhoodPositionSnapshotBasis,
    settled_shown: str,
    shares: tuple[int, ...],
    closing: tuple[int, ...],
) -> None:
    """#1246: a Dec-31 trade settling in January files exactly the A3 rows of
    the same trade settled on Dec 31, whose statement shows the trade-date
    quantity (a trade-and-settlement anchor), closed fragment included. The
    pending buy splits $30 x 3/2 = $45 into exact $30 and $15 shares. The
    pending sale of 1 of 3 files the 2 open shares at the statement's
    $45 x 2/3 = $30, an exact share even for a lone lot, ₹2,400 at 80. Control:
    a statement that already shows the pending buy's share, 3 worth $45, is a
    trade-date anchor and files as on main."""
    open_lots = _recorded_open_lots(monkeypatch)
    paired, accounts = _paired_pending(settled, trade, shown=shown)
    assert _dec31_anchor(paired)[0] is basis

    result = _fa(paired, accounts)
    exact = [lot.closing_total_usd_exact for lot in open_lots]
    settled_paired, settled_accounts = _paired_events(
        (*settled, _later(trade, _END)), position_quantity=Decimal(settled_shown)
    )
    assert _dec31_anchor(settled_paired)[0] is RobinhoodPositionSnapshotBasis.TRADE_AND_SETTLEMENT
    settled_result = _fa(settled_paired, settled_accounts)

    assert result.blockers == settled_result.blockers == ()
    assert result.a3_rows == settled_result.a3_rows
    assert [row.filed.closing_value_inr for row in result.a3_rows] == list(map(Decimal, closing))
    assert exact == [Fraction(share) for share in shares]


def test_dec31_pending_buy_keeps_the_a2_rows() -> None:
    """#1246 control, as on main: A2 is unchanged. The first account closes at
    the statement's $30 less the $20 its January buy took, $10 (₹800), as the
    pending buy settles in January; its peak is Dec 31's 3 trade-dated shares
    at $15 less the $30 both buys cost, $15 (₹1,200). The second holds its $5
    deposit (₹400)."""
    result = _fa(*_repro())

    assert [
        (
            row.account_number,
            row.peak_balance_inr,
            row.closing_balance_inr,
            row.nature_of_amount,
            row.gross_amount_inr,
        )
        for row in result.a2_rows
    ] == [
        ("111111111", Decimal(1200), Decimal(800), FaA2NatureOfAmount.NO_AMOUNT, Decimal()),
        ("222222222", Decimal(400), Decimal(400), FaA2NatureOfAmount.NO_AMOUNT, Decimal()),
    ]


def test_dec31_trade_without_its_pending_row_still_refuses() -> None:
    """#1246 refusing control, as on main: without its Pending Trades row the
    straddling trade has no settlement evidence, so reconciliation refuses with
    its complete existing message. With no pending row, neither pending marker
    is in the input; the refusal and its chain hold none of the trade's symbol
    or CUSIP."""
    trade = _buy("1")
    assert (trade.instrument, trade.cusip) == (_SYMBOL, _CUSIP)

    with pytest.raises(RobinhoodReconciliationError) as raised:
        _paired_pending((_buy(),), trade, shown="2", row=False)

    assert str(raised.value) == (
        "Robinhood reconciliation refused: no activity report assignment reconciles "
        "(report 0/bundle 0: a straddling trade lacks pending-settlement evidence; "
        "report 0/bundle 1: issued activity signature sets disagree; "
        "report 1/bundle 0: issued activity signature sets disagree)"
    )
    assert_prints_no_marker(raised.value, markers=(_SYMBOL, _CUSIP))


@pytest.mark.parametrize(
    ("settled", "shown", "changes", "blockers"),
    [
        pytest.param((), "0", {}, (_closing_blocker(_DEC31_LOT),), id="all-bought-dec31"),
        pytest.param(
            (_buy(),),
            "2",
            {"trade_date_quantity": Decimal(4)},
            (_closing_blocker(_JANUARY_LOT, _DEC31_LOT),),
            id="beyond-tolerance",
        ),
        pytest.param(
            (_buy(),),
            "2",
            {"statement_quantity": Decimal(1)},
            (_closing_blocker(), _closing_blocker(_JANUARY_LOT, _DEC31_LOT)),
            id="fa-vs-anchor",
        ),
        pytest.param(
            (_buy(),),
            "2",
            {"trade_date_quantity": Decimal(0), "tolerance": Decimal(5)},
            (_closing_blocker(_JANUARY_LOT, _DEC31_LOT),),
            id="zero-trade-date",
        ),
    ],
)
def test_dec31_pending_trade_keeps_its_a3_closing_blockers(
    monkeypatch: pytest.MonkeyPatch,
    settled: tuple[RobinhoodEvent, ...],
    shown: str,
    changes: dict[str, Decimal],
    blockers: tuple[robinhood_fa_mod.RobinhoodFaBlocker, ...],
) -> None:
    """#1246 refusing controls; A3 files nothing for the security.
    all-bought-dec31: the statement shows none of the 1 pending share
    (statement quantity 0), so A3 keeps today's path and blocks the lot, as on
    main. beyond-tolerance: an anchor proving 4 trade-date shares where the
    lots hold 3 is beyond its 0.5-share tolerance and blocks both lots.
    fa-vs-anchor: an anchor whose statement quantity, 1, is not FA's own
    Dec-31 position of 2 blocks the security with no lot ids; that account
    then keeps the statement's 2 shares, which the lots' 3 exceed, blocking
    both lots. zero-trade-date: an anchor proving no trade-date shares, with a
    5-share tolerance the lots' 3 fit inside, leaves a $0 total that no lot can
    take a share of, so both lots are blocked rather than divided by zero."""
    if changes:
        forge_settlement_anchors(monkeypatch, **changes)

    result = _fa(*_paired_pending(settled, _buy("1"), shown=shown))

    assert result.blockers == blockers
    assert result.a3 == ()


def test_dec31_pending_buy_cross_checks_each_lot_at_its_trade_dated_quantity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#1246 refusing control: against a $20 Yahoo close, the Dec-31 close
    cross-check runs per lot at its trade-dated quantity, 2 and 1 shares (the
    trade-date quantity 3), against its $30 and $15 share. $40 and $20 are
    outside the tolerance, so each refuses with the existing complete message,
    holding neither pending marker, and blocks its lot. Main never reached the
    cross-check: it blocked both lots for 3 shares against the statement's 2."""
    paired, accounts = _repro()
    _assert_pending_markers_in(paired)
    calls: list[tuple[Decimal, Decimal]] = []
    refusals: list[ClosingPriceError] = []
    resolve = robinhood_fa_mod.resolve_dec31_close

    def recorded(**kwargs: Any) -> ResolvedClose:
        calls.append((kwargs["quantity"], kwargs["statement_total_usd"]))
        try:
            return resolve(**kwargs)
        except ClosingPriceError as exc:
            refusals.append(exc)
            raise

    monkeypatch.setattr(robinhood_fa_mod, "resolve_dec31_close", recorded)

    result = _fa(paired, accounts, yahoo="20")

    assert calls == [(Decimal(2), Decimal(30)), (Decimal(1), Decimal(15))]
    assert [str(exc) for exc in refusals] == [_CLOSE_MISMATCH, _CLOSE_MISMATCH]
    for exc in refusals:
        assert_prints_no_marker(exc, markers=PENDING_MARKERS)
    assert result.blockers == (_closing_blocker(_JANUARY_LOT), _closing_blocker(_DEC31_LOT))
    assert result.a3 == ()


@pytest.mark.parametrize(
    "quantity",
    [
        pytest.param("3." + "0" * 1000 + "1", id="fractional-digits"),
        pytest.param("1E999999", id="product-overflow"),
        pytest.param("1E1000000", id="quantity-overflow"),
    ],
)
def test_dec31_pending_share_past_the_digit_cap_refuses_without_echo(
    monkeypatch: pytest.MonkeyPatch,
    quantity: str,
) -> None:
    """#1246: a trade-date quantity past the digit cap refuses before any
    Decimal scaling or sum can overflow, with the fixed #1243 digit-cap text
    naming only the security's index. Its text, repr, args and chain hold
    neither pending marker. Main never read this quantity and blocked A3."""
    forge_settlement_anchors(monkeypatch, trade_date_quantity=Decimal(quantity))
    paired, accounts = _repro()
    _assert_pending_markers_in(paired)

    with pytest.raises(InvariantError) as raised:
        _fa(paired, accounts)

    text = share_refusal("closing")
    expected = invariant_error(what=text.removeprefix("InvariantError: "))
    assert_unchained_refusal(raised.value, expected, text, markers=PENDING_MARKERS)


@pytest.mark.parametrize("whole_pending_first", [True, False])
def test_dec31_scaled_account_cannot_price_another_accounts_whole_pending_holding(
    whole_pending_first: bool,
) -> None:
    """#1246: pooled tolerance and a large lot's close tolerance must not let
    an account with no statement shares borrow another account's scaled price.
    Both settlement-date anchors come from real reconciliation, in either
    account order. Main blocks the lots; A3 now blocks the security itself."""
    cases = (((), "0", _buy("1")), ((_buy(),), "2", _buy("4000")))
    if not whole_pending_first:
        cases = cases[::-1]
    accounts = (_account("111111111"), _account("222222222"))
    statements = []
    reports = []
    for account, family, (settled, shown, trade) in zip(
        accounts,
        (RobinhoodStatementFamily.INDIVIDUAL, RobinhoodStatementFamily.MANAGED_INDIVIDUAL),
        cases,
        strict=True,
    ):
        pending = dec31_trade(trade, 5)
        statement = _statement(
            account.number,
            family,
            settled,
            cash=sum((event.amount_usd or Decimal() for event in settled), Decimal()),
            position_quantity=Decimal(shown),
        )
        statements.append(
            replace(statement, activities=(*statement.activities, pending_row(pending)))
        )
        reports.append(
            RobinhoodActivityReportEvidence(
                events=(*settled, pending), start_date=_OPENING, end_date=_END
            )
        )
    paired = reconcile_robinhood_accounts(
        validate_robinhood_statement_bundles(tuple(statements)), tuple(reports), accounts
    )
    identities = resolve_robinhood_security_identities(paired, (_security(),))
    quantities = resolve_robinhood_security_quantities(paired, identities, ())
    assert quantities.blockers == ()
    assert {anchor.snapshot_basis for anchor in quantities.anchors} == {_SETTLEMENT_DATE}

    result = _fa(paired, accounts)

    assert _closing_blocker() in result.blockers


def test_dec31_pending_total_keeps_the_source_dollar_before_its_close_check() -> None:
    """#1246: pre-resolved quantities are supported compute input. A wide
    caller proves them without losing the source total's last dollar; compute
    must keep it too. At $5E59 a share, the true total differs from Yahoo by
    $1, so both scaled lots must fail the existing close check. Rounding the
    total first hid that discrepancy and filed a wrong whole rupee."""
    with localcontext(Context(prec=200)):
        original, accounts = _repro()
        total = Decimal("1" + "0" * 59 + "1")
        yahoo = Decimal("5E59")
        # Keep INR inside the portal bound and expose the lost dollar at HALF_UP.
        rate = Decimal("5E-61") - Decimal("1E-121")
        statements = [
            account.activity.primary_bundle.statements[0] for account in original.accounts
        ]
        statement = statements[0]
        statements[0] = replace(
            statement,
            positions=(replace(statement.positions[0], market_value_usd=total, price_usd=yahoo),),
            account_summary=replace(
                statement.account_summary,
                closing_total_securities_usd=total,
                closing_portfolio_value_usd=(
                    total + statement.account_summary.closing_brokerage_cash_usd
                ),
            ),
        )
        paired = reconcile_robinhood_accounts(
            validate_robinhood_statement_bundles(tuple(statements)),
            tuple(account.evidence for account in original.accounts),
            accounts,
        )
        config = _config(accounts)
        identities = resolve_robinhood_security_identities(paired, config.securities)
        quantities = resolve_robinhood_security_quantities(paired, identities, ())
    assert quantities.blockers == ()

    result = compute_robinhood_fa(
        paired,
        config=config,
        securities=configured_securities(config),
        income=_income(dividend=False),
        options=_options(),
        calendar_year=2025,
        fx_policy=AuditedFxStub(rate),
        prices_by_ticker=_prices(str(yahoo)),
        entity_overrides=_entity(),
        quantities=quantities,
    )

    assert result.blockers == (_closing_blocker(_JANUARY_LOT), _closing_blocker(_DEC31_LOT))
    assert result.a3 == ()


def test_dec31_unscaled_totals_keep_mains_position_addition_order() -> None:
    """#1246 control: wholly unscaled totals retain main's account/row order.
    At 60 digits, $7E60 followed by six $1 rows sums to $7E60 on main.
    Grouping the second account first would instead add $6 and make $7E60+10,
    which wrongly fails both lots' close checks. Pre-resolved quantities are
    genuine compute input; the tiny audited FX keeps INR inside the portal bound."""
    with localcontext(Context(prec=200)):
        accounts = (_account("111111111"), _account("222222222"))
        events = ((_buy("1"),), (_buy("6"),))
        statements = []
        for index, (account, trades, family) in enumerate(
            zip(
                accounts,
                events,
                (
                    RobinhoodStatementFamily.INDIVIDUAL,
                    RobinhoodStatementFamily.MANAGED_INDIVIDUAL,
                ),
                strict=True,
            )
        ):
            statement = _statement(
                account.number,
                family,
                trades,
                cash=Decimal(-10 if index == 0 else -60),
                position_quantity=Decimal(1 if index == 0 else 6),
            )
            source = statement.positions[0]
            positions = (
                (replace(source, price_usd=Decimal("7E60"), market_value_usd=Decimal("7E60")),)
                if index == 0
                else tuple(
                    replace(
                        source,
                        location=replace(source.location, row=row),
                        quantity=Decimal(1),
                        price_usd=Decimal(1),
                        market_value_usd=Decimal(1),
                    )
                    for row in range(1, 7)
                )
            )
            statements.append(
                replace(
                    statement,
                    positions=positions,
                    account_summary=_summary(
                        statement.account_summary.closing_brokerage_cash_usd,
                        Decimal("7E60") if index == 0 else Decimal(6),
                    ),
                )
            )
        paired = reconcile_robinhood_accounts(
            validate_robinhood_statement_bundles(tuple(statements)),
            tuple(
                RobinhoodActivityReportEvidence(events=items, start_date=_OPENING, end_date=_END)
                for items in events
            ),
            accounts,
        )
        config = _config(accounts)
        quantities = resolve_robinhood_security_quantities(
            paired, resolve_robinhood_security_identities(paired, config.securities), ()
        )
    assert quantities.blockers == ()
    assert {anchor.snapshot_basis for anchor in quantities.anchors} == {
        RobinhoodPositionSnapshotBasis.TRADE_AND_SETTLEMENT
    }

    result = compute_robinhood_fa(
        paired,
        config=config,
        securities=configured_securities(config),
        income=_income(dividend=False),
        options=_options(),
        calendar_year=2025,
        fx_policy=AuditedFxStub(Decimal("1E-55")),
        prices_by_ticker=_prices("1E60"),
        entity_overrides=_entity(),
        quantities=quantities,
    )

    assert result.blockers == ()
    assert [row.filed.closing_value_inr for row in result.a3_rows] == [
        Decimal(100000),
        Decimal(600000),
    ]


@pytest.mark.parametrize(("cents", "refuses"), [("0.30", True), ("0.32", False)])
def test_dec31_pending_sale_cross_checks_its_exact_share(
    monkeypatch: pytest.MonkeyPatch, cents: str, refuses: bool
) -> None:
    """#1246: a lone scaled share of $1E60+1/3 must compare exactly, even
    where its review Decimal retains only $1E60+0.3. Yahoo +0.30 is outside
    the $0.015 tolerance; +0.32 is inside. Real pre-resolved quantities and
    whole-dollar costs/proceeds keep A2 valid; no anchor or lot is forged."""
    with localcontext(Context(prec=200)):
        total = Decimal("3E60") + 1
        yahoo = Decimal("1E60") + Decimal(cents)
        buy = replace(_buy("3"), price_usd=Decimal("1E60"), amount_usd=Decimal("-3E60"))
        sale = replace(
            _sale(), quantity=Decimal(2), price_usd=Decimal("1E60"), amount_usd=Decimal("2E60")
        )
        original, accounts = _paired_pending((buy,), sale, shown="3")
        statements = [
            account.activity.primary_bundle.statements[0] for account in original.accounts
        ]
        statement = statements[0]
        statements[0] = replace(
            statement,
            positions=(replace(statement.positions[0], market_value_usd=total, price_usd=yahoo),),
            account_summary=replace(
                statement.account_summary,
                closing_total_securities_usd=total,
                closing_portfolio_value_usd=(
                    total + statement.account_summary.closing_brokerage_cash_usd
                ),
            ),
        )
        paired = reconcile_robinhood_accounts(
            validate_robinhood_statement_bundles(tuple(statements)),
            tuple(account.evidence for account in original.accounts),
            accounts,
        )
        config = _config(accounts)
        quantities = resolve_robinhood_security_quantities(
            paired, resolve_robinhood_security_identities(paired, config.securities), ()
        )
    assert quantities.blockers == ()
    _assert_pending_markers_in(paired)
    calls: list[Fraction] = []
    errors: list[ClosingPriceError] = []
    resolve = robinhood_fa_mod.resolve_dec31_close

    def recorded(**kwargs: Any) -> ResolvedClose:
        calls.append(kwargs["statement_total_usd"])
        try:
            return resolve(**kwargs)
        except ClosingPriceError as exc:
            errors.append(exc)
            raise

    monkeypatch.setattr(robinhood_fa_mod, "resolve_dec31_close", recorded)

    result = compute_robinhood_fa(
        paired,
        config=config,
        securities=configured_securities(config),
        income=_income(dividend=False),
        options=_options(),
        calendar_year=2025,
        fx_policy=AuditedFxStub(Decimal("1E-60")),
        prices_by_ticker=_prices(str(yahoo)),
        entity_overrides=_entity(),
        quantities=quantities,
    )

    assert calls == [Fraction(total) / 3]
    assert all(isinstance(value, Fraction) for value in calls)
    assert [str(error) for error in errors] == ([_CLOSE_MISMATCH] if refuses else [])
    for error in errors:
        assert_prints_no_marker(error, markers=PENDING_MARKERS)
    assert result.blockers == ((_closing_blocker(_JANUARY_LOT),) if refuses else ())
    if not refuses:
        assert [row.filed.closing_value_inr for row in result.a3_rows] == [Decimal(1), Decimal(0)]


@pytest.mark.parametrize("total", [Fraction(), Fraction(-1)])
def test_exact_pending_statement_total_must_be_positive(total: Fraction) -> None:
    with pytest.raises(ClosingPriceError) as raised:
        robinhood_fa_mod.resolve_dec31_close(
            valuation_date=_END, quantity=Decimal(1), statement_total_usd=total, series=None
        )

    assert str(raised.value) == "statement_total_usd must be a positive, finite amount"
    assert raised.value.__cause__ is None
    assert raised.value.__context__ is None


def test_dec31_unscaled_quantities_keep_mains_position_addition_order() -> None:
    """#1246 control: main adds each statement quantity in account/row order.
    Six 201-share rows after 7*10^60 whole shares add 1200 at 60 digits;
    the two open lots add 1210, outside the two anchors' summed tolerance of 1.
    Main blocks both lots. Grouping by account would admit this unscaled input."""
    with localcontext(Context(prec=200)):
        accounts = (_account("111111111"), _account("222222222"))
        amounts = (Decimal("7" + "0" * 60), Decimal(1206))
        events = tuple(
            (replace(_buy(str(amount)), price_usd=Decimal(1), amount_usd=-amount),)
            for amount in amounts
        )
        statements = []
        for index, (account, amount, trades, family) in enumerate(
            zip(
                accounts,
                amounts,
                events,
                (
                    RobinhoodStatementFamily.INDIVIDUAL,
                    RobinhoodStatementFamily.MANAGED_INDIVIDUAL,
                ),
                strict=True,
            )
        ):
            statement = _statement(
                account.number, family, trades, cash=-amount, position_quantity=amount
            )
            source = statement.positions[0]
            positions = (
                (replace(source, price_usd=Decimal(1), market_value_usd=amount),)
                if index == 0
                else tuple(
                    replace(
                        source,
                        location=replace(source.location, row=row),
                        quantity=Decimal(201),
                        price_usd=Decimal(1),
                        market_value_usd=Decimal(201),
                    )
                    for row in range(1, 7)
                )
            )
            statements.append(
                replace(statement, positions=positions, account_summary=_summary(-amount, amount))
            )
        paired = reconcile_robinhood_accounts(
            validate_robinhood_statement_bundles(tuple(statements)),
            tuple(
                RobinhoodActivityReportEvidence(events=items, start_date=_OPENING, end_date=_END)
                for items in events
            ),
            accounts,
        )
        config = _config(accounts)
        quantities = resolve_robinhood_security_quantities(
            paired, resolve_robinhood_security_identities(paired, config.securities), ()
        )
    assert quantities.blockers == ()
    assert robinhood_fa_mod.reconcile_robinhood_security_lots(quantities).blockers == ()
    assert {anchor.snapshot_basis for anchor in quantities.anchors} == {
        RobinhoodPositionSnapshotBasis.TRADE_AND_SETTLEMENT
    }

    result = compute_robinhood_fa(
        paired,
        config=config,
        securities=configured_securities(config),
        income=_income(dividend=False),
        options=_options(),
        calendar_year=2025,
        fx_policy=AuditedFxStub(Decimal("1E-55")),
        prices_by_ticker=_prices("1"),
        entity_overrides=_entity(),
        quantities=quantities,
    )

    assert result.blockers == (
        _closing_blocker(_JANUARY_LOT, RobinhoodSecurityEventReference(1, 2)),
    )
    assert result.a3 == ()


# ---- each credit goes to the paying account's lots (#1318) ----

_FIRST_ACCOUNT = "111111111"
_SECOND_ACCOUNT = "222222222"
_OTHER_SYMBOL = "XQAB"
_OTHER_CUSIP = "99971T202"
_OTHER_ISIN = Isin("US99971T2024")
_OTHER_NAME = "Sample Issuer AB Inc"
_FEE_DAY = date(2025, 6, 2)
_LATER_DIVIDEND_DAYS = {
    "record_date": date(2025, 7, 10),
    "payment_date": date(2025, 7, 15),
    "activity_date": date(2025, 7, 15),
    "process_date": date(2025, 7, 15),
    "settle_date": date(2025, 7, 15),
}


def _two_accounts(
    first: tuple[RobinhoodEvent, ...],
    second: tuple[RobinhoodEvent, ...],
    *,
    held: tuple[str, str] = ("2", "2"),
    credited: tuple[dict[str, str], dict[str, str]] = ({}, {}),
    symbols: tuple[str, str] = (_SYMBOL, _SYMBOL),
) -> tuple[RobinhoodAccountsReconciliation, tuple[RobinhoodAccountConfig, RobinhoodAccountConfig]]:
    """Both accounts' CY 2025 evidence. Account ``i`` makes the events in
    ``(first, second)[i]``, holds ``held[i]`` shares of ``symbols[i]`` on
    Dec 31, and its statement's income rows show ``credited[i]`` (label to
    amount). Every internal transfer goes from the first account to the
    second."""
    statements: list[RobinhoodStatement] = []
    for number, family, events, quantity, income_rows, symbol in zip(
        (_FIRST_ACCOUNT, _SECOND_ACCOUNT),
        (RobinhoodStatementFamily.INDIVIDUAL, RobinhoodStatementFamily.MANAGED_INDIVIDUAL),
        (first, second),
        held,
        credited,
        symbols,
        strict=True,
    ):
        statement = _statement(
            number,
            family,
            events,
            cash=sum((event.amount_usd or Decimal() for event in events), Decimal()),
            position_quantity=Decimal(quantity),
        )
        statements.append(
            replace(
                statement,
                income=tuple(
                    replace(
                        row,
                        this_period_usd=Decimal(income_rows[row.label]),
                        year_to_date_usd=Decimal(income_rows[row.label]),
                    )
                    if row.label in income_rows
                    else row
                    for row in statement.income
                ),
                activities=tuple(
                    replace(
                        row,
                        settle_date=None,
                        source_account_reference=_FIRST_ACCOUNT,
                        destination_account_reference=_SECOND_ACCOUNT,
                    )
                    if row.transaction_code == "ITRF"
                    else row
                    for row in statement.activities
                ),
                positions=tuple(
                    replace(position, symbol_or_cusip=symbol) for position in statement.positions
                ),
            )
        )
    accounts = (_account(_FIRST_ACCOUNT), _account(_SECOND_ACCOUNT))
    paired = reconcile_robinhood_accounts(
        validate_robinhood_statement_bundles(tuple(statements)),
        tuple(
            RobinhoodActivityReportEvidence(events=events, start_date=_OPENING, end_date=_END)
            for events in (first, second)
        ),
        accounts,
    )
    return paired, accounts


def _paid_dividend(row: int, shares: str, amount: str, rate: str, **days: date) -> RobinhoodEvent:
    """``_dividend()`` as activity row ``row``: ``amount`` for ``shares`` at ``rate``."""
    return replace(
        _dividend(),
        source_record_index=row,
        amount_usd=Decimal(amount),
        description_shares=Decimal(shares),
        description_rate_usd=Decimal(rate),
        **days,
    )


def _dividend_payment(
    account: int,
    row: int,
    amount: str,
    *,
    holding_date: date = date(2025, 3, 9),
    paid: date = date(2025, 3, 15),
    security_index: int = 0,
) -> RobinhoodDividendPayment:
    """The income engine's payment for ``account``'s dividend at ``row`` on the
    ``security_index``th configured security."""
    [payment] = _income(dividend=True).dividend_payments
    return replace(
        payment,
        event_reference=RobinhoodIncomeEventReference(account, row),
        security_config_index=security_index,
        source_payment_date=paid,
        dividend=replace(
            payment.dividend,
            amount_gross=Decimal(amount),
            payment_date=paid,
            holding_date=holding_date,
            source_row_index=row,
        ),
    )


def _loan_fee(row: int, amount: str, day: date = _FEE_DAY) -> RobinhoodEvent:
    """A Stock Lending payment of ``amount`` on ``day`` at activity row ``row``."""
    return RobinhoodEvent(
        kind=RobinhoodEventKind.STOCK_LENDING_INCOME,
        source_record_index=row,
        activity_date=day,
        process_date=day,
        settle_date=day,
        instrument=_SYMBOL,
        description="Stock Lending",
        trans_code="SLIP",
        quantity=None,
        quantity_suffix=None,
        price_usd=None,
        amount_usd=Decimal(amount),
    )


def _loan_fee_payment(
    account: int, row: int, amount: str, day: date = _FEE_DAY
) -> RobinhoodStockLendingPayment:
    """The income engine's monthly loan fee for ``account``'s ``_loan_fee`` at ``row``."""
    return RobinhoodStockLendingPayment(
        event_reference=RobinhoodIncomeEventReference(account, row),
        security_config_index=0,
        issuer_country_iso2=CountryCode("US"),
        source_country_iso2=CountryCode("US"),
        withholding_agent_country_iso2=None,
        in_financial_year=True,
        subtype=RobinhoodStockLendingSubtype.SECURITIES_LENDING_FEE,
        annual_bucket_index=None,
        loan_fee_income=OtherSourceIncome(
            nature="Securities lending fee - Robinhood Stock Lending",
            payment_date=day,
            amount_gross=Decimal(amount),
            currency="USD",
            description="Stock Lending",
            source_row_index=row,
            source_country_iso2=CountryCode("US"),
        ),
        substitute_dividend_income=None,
    )


def _compute_two(
    paired: RobinhoodAccountsReconciliation,
    accounts: tuple[RobinhoodAccountConfig, RobinhoodAccountConfig],
    *,
    dividends: tuple[RobinhoodDividendPayment, ...] = (),
    loan_fees: tuple[RobinhoodStockLendingPayment, ...] = (),
    fx_policy: FxPolicy | None = None,
    config: RobinhoodConfig | None = None,
) -> robinhood_fa_mod.RobinhoodFaResult:
    """CY 2025 Schedule FA for ``paired``, at ₹80/$ unless ``fx_policy`` says
    otherwise, with ``_SYMBOL`` (and ``_OTHER_SYMBOL``) closing at $15."""
    config = config or _config(accounts)
    other_prices = {
        Ticker(_OTHER_SYMBOL): replace(series, ticker=Ticker(_OTHER_SYMBOL))
        for series in _prices().values()
    }
    return compute_robinhood_fa(
        paired,
        config=config,
        securities=configured_securities(config),
        income=replace(
            _income(dividend=False), dividend_payments=dividends, stock_lending_payments=loan_fees
        ),
        options=_options(),
        calendar_year=2025,
        fx_policy=fx_policy or _FlatFx(),
        prices_by_ticker={**_prices(), **other_prices},
        entity_overrides={
            **_entity(),
            _OTHER_ISIN: replace(_entity()[_ISIN], isin=_OTHER_ISIN, name=_OTHER_NAME),
        },
    )


def _a3_credits(
    result: robinhood_fa_mod.RobinhoodFaResult,
) -> list[tuple[tuple[int, ...], Decimal | None, Decimal | None]]:
    """Each A3 row's accounts, exact gross credit and filed gross credit."""
    return [
        (
            item.account_indices,
            item.row.gross_amount_paid_credited_inr,
            item.row.filed.gross_amount_paid_credited_inr,
        )
        for item in result.a3
    ]


def _a2_credits(result: robinhood_fa_mod.RobinhoodFaResult) -> dict[int, Decimal]:
    """Each account's exact A2 gross credits."""
    return {
        item.account_index: sum(item.known_gross_components_inr.values(), Decimal())
        for item in result.a2
    }


def _assert_each_accounts_a3_credits_add_up_to_a2(
    result: robinhood_fa_mod.RobinhoodFaResult,
) -> None:
    """Every A3 row here is one account's lot, so its credits sum to that account's A2 credits."""
    a3: dict[int, Decimal] = defaultdict(Decimal)
    for item in result.a3:
        (account,) = item.account_indices
        a3[account] += item.row.gross_amount_paid_credited_inr or Decimal()
    assert dict(a3) == _a2_credits(result)


def test_loan_fee_files_only_on_the_paying_accounts_lot() -> None:
    """#1318 example 1: each account holds 2 shares from January 10, and only the
    first receives a $3 Stock Lending fee on June 2. Its lot files ₹240 and
    the second account's lot 0, as A2 files ₹240 and No Amount; main split the
    fee ₹120 to each account's lot."""
    paired, accounts = _two_accounts(
        (_buy(), _loan_fee(3, "3")), (_buy(),), credited=({"Stock Lending": "3"}, {})
    )

    result = _compute_two(paired, accounts, loan_fees=(_loan_fee_payment(0, 3, "3"),))

    assert result.blockers == ()
    assert _a3_credits(result) == [
        ((0,), Decimal(240), Decimal(240)),
        ((1,), Decimal(0), Decimal(0)),
    ]
    assert _a2_credits(result) == {0: Decimal(240), 1: Decimal(0)}
    _assert_each_accounts_a3_credits_add_up_to_a2(result)


def test_loan_fee_after_its_account_sold_never_moves_to_the_other_accounts_lot() -> None:
    """#1318 example 2: the first account sells its 2 shares on June 10 and
    receives a $3 fee on July 7, while the second holds 2 from January 10. No Loan
    Activity proves the sold lot earned it, so the fee blocks A3 credit
    attribution and both lots' credits stay blank; main filed ₹240 on the
    second account's open lot and 0 on the sold one."""
    sale = replace(_sale(), quantity=Decimal(2), amount_usd=Decimal("29.98"))
    paired, accounts = _two_accounts(
        (_buy(), sale, _loan_fee(5, "3", date(2025, 7, 7))),
        (_buy(),),
        held=("0", "2"),
        credited=({"Stock Lending": "3"}, {}),
    )

    result = _compute_two(
        paired, accounts, loan_fees=(_loan_fee_payment(0, 5, "3", date(2025, 7, 7)),)
    )

    assert [blocker.kind for blocker in result.blockers] == [
        RobinhoodFaBlockerKind.A3_CREDIT_ATTRIBUTION
    ]
    assert _a3_credits(result) == [((0,), None, None), ((1,), None, None)]


def test_each_account_files_its_own_dividend_on_its_own_lot() -> None:
    """#1318 example 3: at ₹82.50, the first account's 1 share receives $0.33
    and the second's 2 shares $0.67 of one $0.3333 dividend. Each lot files its
    own account's credit, exactly $0.33 and $0.67 at ₹82.50, as ₹27 and ₹55,
    adding up to A2's ₹27 and ₹55; main pooled $1.00 and filed ₹28 (₹27.50) and
    ₹55."""
    paired, accounts = _two_accounts(
        (_buy("1"), _paid_dividend(3, "1", "0.33", "0.3333")),
        (_buy("2"), _paid_dividend(3, "2", "0.67", "0.3333")),
        held=("1", "2"),
        credited=({"Dividends": "0.33"}, {"Dividends": "0.67"}),
    )

    result = _compute_two(
        paired,
        accounts,
        dividends=(_dividend_payment(0, 3, "0.33"), _dividend_payment(1, 3, "0.67")),
        fx_policy=_Fx8250(),
    )

    assert result.blockers == ()
    assert _a3_credits(result) == [
        ((0,), Decimal("0.33") * Decimal("82.50"), Decimal(27)),
        ((1,), Decimal("0.67") * Decimal("82.50"), Decimal(55)),
    ]
    assert [item.row.filed.gross_amount_inr for item in result.a2] == [Decimal(27), Decimal(55)]
    _assert_each_accounts_a3_credits_add_up_to_a2(result)


def test_accounts_holding_different_securities_file_as_before() -> None:
    """#1318 control: the first account holds 2 shares of one security and the
    second 1 share of another, each paid its own dividend. Each lot files its
    own account's credit, ₹800 and ₹320, as on main."""
    other = {
        "instrument": _OTHER_SYMBOL,
        "cusip": _OTHER_CUSIP,
        "description": f"{_OTHER_NAME}\nCUSIP: {_OTHER_CUSIP}",
        "issuer_name": _OTHER_NAME,
    }
    paired, accounts = _two_accounts(
        (_buy(), _paid_dividend(3, "2", "10", "5")),
        (
            replace(_buy("1"), **other),
            replace(_paid_dividend(3, "1", "4", "4"), instrument=_OTHER_SYMBOL),
        ),
        held=("2", "1"),
        credited=({"Dividends": "10"}, {"Dividends": "4"}),
        symbols=(_SYMBOL, _OTHER_SYMBOL),
    )
    config = replace(
        _config(accounts),
        securities=(
            _security(),
            robinhood_security(
                symbol=_OTHER_SYMBOL,
                cusip=_OTHER_CUSIP,
                isin=_OTHER_ISIN,
                source="synthetic issuer evidence",
            ),
        ),
    )

    result = _compute_two(
        paired,
        accounts,
        dividends=(
            _dividend_payment(0, 3, "10"),
            _dividend_payment(1, 3, "4", security_index=1),
        ),
        config=config,
    )

    assert result.blockers == ()
    assert _a3_credits(result) == [
        ((0,), Decimal(800), Decimal(800)),
        ((1,), Decimal(320), Decimal(320)),
    ]
    _assert_each_accounts_a3_credits_add_up_to_a2(result)


def test_delayed_loan_fee_is_reached_while_the_other_account_still_holds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#1318: the first account's lot closed before its fee posted, while the
    second account holds another lot of the security. The first account holds
    nothing on the fee date, so its Loan Activity fallback finds its closed
    lot; main gave the fee to the second account's lot."""
    paired, securities, fragments, lots, credit = _delayed_fee_fixture()
    other_id = RobinhoodSecurityEventReference(1, 2)
    other = robinhood_fa_mod._Fragment(
        key=robinhood_fa_mod._FragmentKey(other_id),
        security_config_index=0,
        quantity=Decimal(2),
        cost_usd=Decimal(20),
        open_date=date(2025, 1, 10),
        close_date=None,
        account_indices=(1,),
        holdings=(robinhood_fa_mod._Holding(1, date(2025, 1, 10), Decimal(2)),),
    )
    lots = replace(
        lots,
        holding_intervals=(
            *lots.holding_intervals,
            RobinhoodLotHoldingInterval(
                lot_id=other_id,
                account_index=1,
                cusip=_CUSIP,
                from_date=date(2025, 1, 10),
                to_date=None,
                quantity=Decimal(2),
            ),
        ),
    )
    monkeypatch.setattr(robinhood_fa_mod, "_source_credits", lambda *_args: (credit,))

    usd, _inr, references, accrual_references, blockers = robinhood_fa_mod._allocate_credits(
        (*fragments, other),
        (),
        cast("Any", SimpleNamespace(schedule_os_complete=True)),
        paired,
        securities,
        lots,
        2025,
        _FlatFx(),
    )

    assert blockers == ()
    assert usd == {fragments[0].key: credit.amount_usd, other.key: Decimal(0)}
    assert references == {fragments[0].key: (credit.reference,)}
    assert accrual_references == {fragments[0].key: (credit.reference,)}


@pytest.mark.parametrize(
    ("days", "paying_account", "credited"),
    [
        pytest.param(_LATER_DIVIDEND_DAYS, 1, Decimal(800), id="transfer-before-record-receiving"),
        pytest.param(_LATER_DIVIDEND_DAYS, 0, None, id="transfer-before-record-sending"),
        pytest.param({}, 0, Decimal(800), id="transfer-after-record-sending"),
        pytest.param({}, 1, None, id="transfer-after-record-receiving"),
    ],
)
def test_transferred_lot_takes_a_dividend_in_the_account_that_held_it(
    days: dict[str, date], paying_account: int, credited: Decimal | None
) -> None:
    """#1318: the first account's 2-share lot moves to the second on June 1.
    A $10 dividend recorded after the transfer (held July 9) belongs to the
    receiving account, one recorded before it (held March 9) to the sending
    account, and either files ₹800 on the lot. Paid to the other account, it
    finds no lot there and blocks A3 credit attribution; main filed it on the
    lot either way."""
    dividend = _paid_dividend(4, "2", "10", "5", **days)
    paired, accounts = _two_accounts(
        (_buy(), _security_transfer(3, source=True), *((dividend,) * (paying_account == 0))),
        (_security_transfer(2, source=False), *((dividend,) * (paying_account == 1))),
        held=("0", "2"),
        credited=(({"Dividends": "10"}, {}) if paying_account == 0 else ({}, {"Dividends": "10"})),
    )
    payment = _dividend_payment(
        paying_account,
        4,
        "10",
        holding_date=days.get("record_date", date(2025, 3, 10)) - timedelta(days=1),
        paid=days.get("payment_date", date(2025, 3, 15)),
    )

    result = _compute_two(paired, accounts, dividends=(payment,))

    assert result.blockers == (
        ()
        if credited is not None
        else (
            robinhood_fa_mod.RobinhoodFaBlocker(
                kind=RobinhoodFaBlockerKind.A3_CREDIT_ATTRIBUTION,
                security_config_indices=(0,),
            ),
        )
    )
    assert _a3_credits(result) == [((0, 1), credited, credited)]


@pytest.mark.parametrize(
    ("days", "shares", "credited", "expected"),
    [
        pytest.param(
            _LATER_DIVIDEND_DAYS,
            ("1", "4"),
            ("0.33", "1.33"),
            (Decimal(53), Decimal("79.8")),
            id="after-transfer",
        ),
        pytest.param(
            {}, ("2", "3"), ("0.67", "1.00"), (Decimal("53.6"), Decimal(80)), id="before-transfer"
        ),
    ],
)
def test_partly_transferred_lot_takes_each_accounts_dividend_on_its_part(
    days: dict[str, date],
    shares: tuple[str, str],
    credited: tuple[str, str],
    expected: tuple[Decimal, Decimal],
) -> None:
    """#1318: the first account's 2-share lot sends 1 share to the second
    account on June 1; the second also holds its own 3-share lot. At ₹80, a
    $0.3333 dividend recorded after the transfer pays the first account $0.33
    for 1 share and the second $1.33 for 4: the transferred lot files $0.33 plus
    a quarter of $1.33 (₹53) and the second account's lot three quarters
    (₹79.80). Recorded before the transfer, each account's dividend stays on its
    own lot: $0.67 for 2 shares (₹53.60, filed 54) and $1.00 for 3 (₹80). Main
    pooled both accounts' dividend and split it by shares: ₹53.12 and ₹79.68
    after, ₹53.44 (filed 53) and ₹80.16 before."""
    transfer = replace(_security_transfer(3, source=True), quantity=Decimal(1))
    incoming = replace(_security_transfer(3, source=False), quantity=Decimal(1))
    paired, accounts = _two_accounts(
        (_buy(), transfer, _paid_dividend(4, shares[0], credited[0], "0.3333", **days)),
        (_buy("3"), incoming, _paid_dividend(4, shares[1], credited[1], "0.3333", **days)),
        held=("1", "4"),
        credited=({"Dividends": credited[0]}, {"Dividends": credited[1]}),
    )
    holding_date = days.get("record_date", date(2025, 3, 10)) - timedelta(days=1)
    paid = days.get("payment_date", date(2025, 3, 15))

    result = _compute_two(
        paired,
        accounts,
        dividends=tuple(
            _dividend_payment(account, 4, amount, holding_date=holding_date, paid=paid)
            for account, amount in enumerate(credited)
        ),
    )

    assert result.blockers == ()
    assert _a3_credits(result) == [
        ((0, 1), expected[0], expected[0].quantize(Decimal(1), ROUND_HALF_UP)),
        ((1,), expected[1], expected[1].quantize(Decimal(1), ROUND_HALF_UP)),
    ]
    assert sum(expected) == sum(_a2_credits(result).values())


_BOUNCED_LOT = RobinhoodSecurityEventReference(0, 2)


def _bounced_intervals() -> tuple[RobinhoodLotHoldingInterval, ...]:
    """A 3-share lot bought into the first account on January 10 sends 1 share
    to the second on June 1, takes it back on July 1, then moves whole to the
    second account on August 1."""

    def interval(account: int, start: date, end: date | None, shares: str):
        return RobinhoodLotHoldingInterval(
            lot_id=_BOUNCED_LOT,
            account_index=account,
            cusip=_CUSIP,
            from_date=start,
            to_date=end,
            quantity=Decimal(shares),
        )

    return (
        interval(0, date(2025, 1, 10), date(2025, 6, 1), "1"),
        interval(1, date(2025, 6, 1), date(2025, 7, 1), "1"),
        interval(0, date(2025, 1, 10), date(2025, 8, 1), "2"),
        interval(0, date(2025, 7, 1), date(2025, 8, 1), "1"),
        interval(1, date(2025, 8, 1), None, "2"),
        interval(1, date(2025, 8, 1), None, "1"),
    )


@pytest.mark.parametrize(
    ("day", "accounts"),
    [
        pytest.param(date(2025, 5, 9), frozenset({0}), id="before-the-first-transfer"),
        pytest.param(date(2025, 6, 9), frozenset({0, 1}), id="while-a-part-is-away"),
        pytest.param(date(2025, 7, 9), frozenset({0}), id="after-it-came-back"),
        pytest.param(date(2025, 8, 9), frozenset({1}), id="after-the-whole-move"),
    ],
)
def test_a_part_moved_by_one_transfer_from_parts_with_different_histories_is_traced_to_both(
    day: date, accounts: frozenset[int]
) -> None:
    """#1318: the August transfer takes the lot's 2 shares that never left the
    first account and the 1 that spent June there in the second, and each part
    it delivers can be either. While the 1 share was away, a delivered part's
    account is unknown, so a credit dated then blocks attribution."""
    holding = robinhood_fa_mod._Holding(1, date(2025, 8, 1), Decimal(2))

    assert robinhood_fa_mod._accounts_on(_bounced_intervals(), holding, day) == accounts


def test_a_part_whose_transfer_has_no_sending_interval_refuses() -> None:
    """#1318: a part that entered its account after the holding date with no
    interval ending that day in the other account contradicts the lot history."""
    holding = robinhood_fa_mod._Holding(1, date(2025, 6, 1), Decimal(2))

    with pytest.raises(robinhood_fa_mod.RobinhoodFaError) as raised:
        robinhood_fa_mod._accounts_on((), holding, date(2025, 3, 9))

    assert str(raised.value) == (
        "lot holding intervals do not trace a transferred part to its account"
    )


def test_a_credit_dated_while_a_part_is_away_blocks_attribution(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#1318: a credit dated while the bounced lot's delivered parts could be
    in either account blocks A3 credit attribution and blanks the security's
    credits."""
    paired, accounts = _paired(sold=False, dividend=False)
    fragment = robinhood_fa_mod._Fragment(
        key=robinhood_fa_mod._FragmentKey(_BOUNCED_LOT),
        security_config_index=0,
        quantity=Decimal(3),
        cost_usd=Decimal(30),
        open_date=date(2025, 1, 10),
        close_date=None,
        account_indices=(0, 1),
        holdings=(
            robinhood_fa_mod._Holding(1, date(2025, 8, 1), Decimal(2)),
            robinhood_fa_mod._Holding(1, date(2025, 8, 1), Decimal(1)),
        ),
    )
    credit = robinhood_fa_mod._Credit(
        reference=RobinhoodIncomeEventReference(0, 99),
        security_config_index=0,
        holding_date=date(2025, 6, 9),
        credit_date=date(2025, 6, 9),
        amount_usd=Decimal(1),
        source_quantity=None,
        distribution_rate_usd=None,
        symbol=_SYMBOL,
        monthly_loan_fee=True,
    )
    monkeypatch.setattr(robinhood_fa_mod, "_source_credits", lambda *_args: (credit,))

    usd, inr, _references, _accrual, blockers = robinhood_fa_mod._allocate_credits(
        (fragment,),
        (),
        cast("Any", SimpleNamespace(schedule_os_complete=True)),
        paired,
        _config(accounts).securities,
        RobinhoodSecurityLotResult((), (), _bounced_intervals(), (), ()),
        2025,
        _FlatFx(),
    )

    assert blockers == (
        robinhood_fa_mod.RobinhoodFaBlocker(
            kind=RobinhoodFaBlockerKind.A3_CREDIT_ATTRIBUTION,
            security_config_indices=(0,),
            lot_ids=(_BOUNCED_LOT,),
        ),
    )
    assert usd == {fragment.key: None}
    assert inr == {fragment.key: None}


def test_transfer_history_visits_each_entry_only_once() -> None:
    """Repeated whole transfers of a split lot must not multiply its backtrace."""
    specifications = [
        (0, date(2025, 6, 1), Decimal(1)),
        (1, date(2025, 7, 1), Decimal(1)),
        *((index % 2, date(2025, 8, 1) + timedelta(days=index), Decimal(3)) for index in range(8)),
    ]
    movements = []
    transfers = []
    for index, (account, day, quantity) in enumerate(specifications):
        pair, transfer = _lot_transfer(
            account,
            day,
            quantity,
            transfer_index=index,
            source_ordinal=10 + index * 2,
            destination_ordinal=11 + index * 2,
        )
        movements.extend(pair)
        transfers.append(transfer)
    quantities = _lot_result(
        (_lot_acquisition(0, 2, date(2025, 1, 10), Decimal(3), Decimal(30)),),
        tuple(movements),
        {0: Decimal(3), 1: Decimal()},
        transfers=tuple(transfers),
    )
    lots = robinhood_fa_mod.reconcile_robinhood_security_lots(quantities)
    assert lots.blockers == ()
    assert len(lots.open_fragments) == 2

    class CountedIntervals(tuple):
        scans = 0

        def __iter__(self):
            self.scans += 1
            return super().__iter__()

    intervals = CountedIntervals(lots.holding_intervals)
    part = lots.open_fragments[0]
    holding = robinhood_fa_mod._Holding(
        part.account_index, part.entry_date, part.quantity, part.exact_quantity
    )

    assert robinhood_fa_mod._accounts_on(intervals, holding, date(2025, 5, 9)) == frozenset({0})
    assert intervals.scans <= len(intervals)


def test_partly_transferred_thirds_split_credits_by_their_exact_quantities() -> None:
    """Each of three lots has 2/3 in the sender and 1/3 in the receiver.
    The receiver's $1.20 dividend gives each transferred part exactly $0.20,
    or ₹16.50 (filed 17). Its own 1-share lot takes $0.60, ₹49.50 (filed 50).
    """
    buys = tuple(
        replace(_buy("1"), source_record_index=row, price_usd=Decimal(12), amount_usd=Decimal(-12))
        for row in (2, 3, 4)
    )
    outgoing = replace(_security_transfer(5, source=True), quantity=Decimal(1))
    incoming = replace(_security_transfer(3, source=False), quantity=Decimal(1))
    paired, accounts = _two_accounts(
        (*buys, outgoing),
        (
            replace(_later(_buy("1")), source_record_index=2),
            incoming,
            _paid_dividend(4, "2", "1.20", "0.60", **_LATER_DIVIDEND_DAYS),
        ),
        held=("2", "2"),
        credited=({}, {"Dividends": "1.20"}),
    )
    result = _compute_two(
        paired,
        accounts,
        dividends=(
            _dividend_payment(1, 4, "1.20", holding_date=date(2025, 7, 9), paid=date(2025, 7, 15)),
        ),
        fx_policy=_Fx8250(),
    )

    assert result.blockers == ()
    assert _a3_credits(result) == [((0, 1), Decimal("16.50"), Decimal(17))] * 3 + [
        ((1,), Decimal("49.50"), Decimal(50))
    ]


@pytest.mark.parametrize(
    ("future", "disposed"),
    [(True, False), (True, True), (False, True)],
    ids=("held-after-dec31-transfer", "disposed-after-dec31-transfers", "disposed-mixed-entries"),
)
def test_fragment_parts_keep_their_accounts_before_later_transfers(
    future: bool,
    disposed: bool,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Future sales remain open for CY 2025; a sale of a returned part keeps
    both entry dates, so its earlier credit can find both accounts.
    """
    transfer_day = date(2026, 1, 2) if future else date(2025, 6, 1)
    return_day = date(2026, 1, 3) if future else date(2025, 8, 1)
    sale_day = date(2026, 1, 10) if future else date(2025, 9, 1)
    if future:
        paired, accounts = _paired_future_sale()
    else:
        sale = replace(
            _sale(),
            source_record_index=3,
            quantity=Decimal(2),
            amount_usd=Decimal(30),
            activity_date=sale_day,
            process_date=sale_day,
            settle_date=sale_day,
        )
        paired, accounts = _paired_events((_buy(), sale), position_quantity=Decimal())
    moves, transfer = _lot_transfer(
        0,
        transfer_day,
        Decimal(1) if disposed else Decimal(2),
        source_ordinal=10,
        destination_ordinal=11,
    )
    movements = list(moves)
    transfers = [transfer]
    if disposed:
        returned, transfer_back = _lot_transfer(
            1,
            return_day,
            Decimal(1),
            transfer_index=1,
            source_ordinal=12,
            destination_ordinal=13,
        )
        movements.extend(returned)
        transfers.append(transfer_back)
        movements.append(
            _lot_movement(0, 3, sale_day, Decimal(-2), RobinhoodQuantityMovementKind.DISPOSAL)
        )
    quantities = _lot_result(
        (_lot_acquisition(0, 2, date(2025, 1, 10), Decimal(2), Decimal(20)),),
        tuple(movements),
        {0: Decimal(), 1: Decimal() if disposed else Decimal(2)},
        transfers=tuple(transfers),
    )
    lots = robinhood_fa_mod.reconcile_robinhood_security_lots(quantities)
    identities = resolve_robinhood_security_identities(paired, _config(accounts).securities)

    fragments, blockers = robinhood_fa_mod._fragments(paired, identities, quantities, lots, 2025)

    assert lots.blockers == blockers == ()
    (fragment,) = fragments
    assert fragment.close_date == (None if future else sale_day)
    assert len(fragment.holdings) == (2 if disposed else 1)
    assert [
        robinhood_fa_mod._held_in_account(
            fragment, lots.holding_intervals, account, date(2025, 7, 9)
        )
        for account in (0, 1)
    ] == (
        [(Decimal(2), Fraction(2)), (Decimal(), Fraction())]
        if future
        else [(Decimal(1), Fraction(1)), (Decimal(1), Fraction(1))]
    )
    for account in (0, 1):
        quantity = Decimal(2) if future and account == 0 else Decimal(1)
        credit = robinhood_fa_mod._Credit(
            reference=RobinhoodIncomeEventReference(account, 99),
            security_config_index=0,
            holding_date=date(2025, 7, 9),
            credit_date=date(2025, 7, 15),
            amount_usd=quantity,
            source_quantity=quantity,
            distribution_rate_usd=Decimal(1),
            symbol=_SYMBOL,
            monthly_loan_fee=False,
        )
        monkeypatch.setattr(
            robinhood_fa_mod, "_source_credits", lambda *_args, credit=credit: (credit,)
        )
        usd, inr, _references, _accrual, credit_blockers = robinhood_fa_mod._allocate_credits(
            fragments,
            (),
            _income(dividend=False),
            paired,
            _config(accounts).securities,
            lots,
            2025,
            _FlatFx(),
        )
        blocked = future and account == 1
        assert bool(credit_blockers) == blocked
        assert usd == {fragment.key: None if blocked else quantity}
        assert inr == {fragment.key: None if blocked else quantity * Decimal(80)}


def test_accounts_paid_on_different_days_keep_their_own_dividends() -> None:
    """Different payment dates do not compare one account's dividend shares
    with both accounts' holdings, which blocked attribution on main.
    """
    later = {
        "payment_date": date(2025, 3, 16),
        "activity_date": date(2025, 3, 16),
        "process_date": date(2025, 3, 16),
        "settle_date": date(2025, 3, 16),
    }
    paired, accounts = _two_accounts(
        (_buy("2"), _paid_dividend(3, "2", "2", "1")),
        (_buy("3"), _paid_dividend(3, "3", "3", "1", **later)),
        held=("2", "3"),
        credited=({"Dividends": "2"}, {"Dividends": "3"}),
    )

    result = _compute_two(
        paired,
        accounts,
        dividends=(
            _dividend_payment(0, 3, "2"),
            _dividend_payment(1, 3, "3", paid=date(2025, 3, 16)),
        ),
    )

    assert result.blockers == ()
    assert _a3_credits(result) == [
        ((0,), Decimal(160), Decimal(160)),
        ((1,), Decimal(240), Decimal(240)),
    ]


def test_each_accounts_share_check_keeps_its_own_display_tolerance() -> None:
    """#1318: the first account holds 2 shares and the second 3, and the same
    $1-a-share dividend, paid the same day, describes 1 share in the first and
    2 in the second. Each account's check allows its own half-share display
    tolerances, as a lone account's does, so each 1-share gap passes and each
    lot files its own account's dividend, ₹80 and ₹160. Main pooled the 3
    described shares against the 5 held, past the pooled 1.5-share tolerance,
    and blocked both; accepted, as main refused only because it pooled the
    accounts."""
    paired, accounts = _two_accounts(
        (_buy("2"), _paid_dividend(3, "1", "1", "1")),
        (_buy("3"), _paid_dividend(3, "2", "2", "1")),
        held=("2", "3"),
        credited=({"Dividends": "1"}, {"Dividends": "2"}),
    )

    result = _compute_two(
        paired, accounts, dividends=(_dividend_payment(0, 3, "1"), _dividend_payment(1, 3, "2"))
    )

    assert result.blockers == ()
    assert _a3_credits(result) == [
        ((0,), Decimal(80), Decimal(80)),
        ((1,), Decimal(160), Decimal(160)),
    ]


def test_each_accounts_delayed_loan_fee_finds_its_own_closed_lot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#1318: both accounts sold their lots before same-day monthly loan fees
    posted, and each account's own Loan Activity proves its own closed lot.
    Each fee is its account's only credit that day, so the fallback finds each
    account's lot, $0.10 and ₹8 each. Main pooled the two fees, and the
    fallback, which takes exactly one credit, blocked both; accepted, as main
    refused only because it pooled the accounts."""
    paired, securities, fragments, lots, first_credit = _delayed_fee_fixture()
    first = paired.accounts[0]
    managed = RobinhoodStatementFamily.MANAGED_INDIVIDUAL
    second = replace(
        first,
        report_index=1,
        source_bundle=replace(
            first.source_bundle,
            family=managed,
            statements=tuple(
                replace(statement, account_number=_SECOND_ACCOUNT, family=managed)
                for statement in first.source_bundle.statements
            ),
        ),
    )
    second_lot = RobinhoodSecurityEventReference(1, 2)
    second_fragment = replace(
        fragments[0],
        key=robinhood_fa_mod._FragmentKey(second_lot, (RobinhoodSecurityEventReference(1, 4),)),
        account_indices=(1,),
        holdings=tuple(replace(item, account_index=1) for item in fragments[0].holdings),
    )
    lots = replace(
        lots,
        holding_intervals=(
            *lots.holding_intervals,
            *(replace(item, lot_id=second_lot, account_index=1) for item in lots.holding_intervals),
        ),
    )
    second_credit = replace(first_credit, reference=RobinhoodIncomeEventReference(1, 99))
    monkeypatch.setattr(
        robinhood_fa_mod, "_source_credits", lambda *_args: (first_credit, second_credit)
    )

    usd, inr, _references, accrual_references, blockers = robinhood_fa_mod._allocate_credits(
        (fragments[0], second_fragment),
        (),
        cast("Any", SimpleNamespace(schedule_os_complete=True)),
        replace(paired, accounts=(first, second)),
        securities,
        lots,
        2025,
        _FlatFx(),
    )

    assert blockers == ()
    assert usd == {fragments[0].key: Decimal("0.1"), second_fragment.key: Decimal("0.1")}
    assert inr == {fragments[0].key: Decimal(8), second_fragment.key: Decimal(8)}
    assert accrual_references == {
        fragments[0].key: (first_credit.reference,),
        second_fragment.key: (second_credit.reference,),
    }


@pytest.mark.parametrize("paid", [_DEC31, _JAN2], ids=["pd-2025", "pd-2026"])
def test_one_dividend_posted_in_two_years_files_each_payment_in_its_own_year(paid: date) -> None:
    """#1320 with #1318: one $5-a-share dividend with ``P/D`` ``paid`` pays
    the first account $10 on its 2 shares, posted December 31, 2025, and the
    second $15 on its 3 shares, posted January 2, 2026. CY 2025 holds only
    the first account's payment, and with per-account grouping its share
    check reads only that account's holdings, so nothing blocks. The first
    lot files ₹800 and the second 0, as A2 files ₹800 and No Amount. Main
    chose by the ``P/D``: both payments (₹800 and ₹1,200) for a 2025 ``P/D``,
    neither for a 2026 one."""
    paired, accounts = _two_accounts(
        (_buy(), _paid_dividend(3, "2", "10", "5")),
        (_buy("3"), _paid_dividend(3, "3", "15", "5")),
        held=("2", "3"),
        credited=({"Dividends": "10"}, {"Dividends": "15"}),
    )
    payments = tuple(
        replace(
            payment,
            source_record_date=date(2025, 12, 19),
            source_payment_date=paid,
            dividend=replace(payment.dividend, payment_date=posted),
        )
        for payment, posted in zip(
            (
                _dividend_payment(0, 3, "10", holding_date=date(2025, 12, 18)),
                _dividend_payment(1, 3, "15", holding_date=date(2025, 12, 18)),
            ),
            (_DEC31, _JAN2),
            strict=True,
        )
    )

    result = _compute_two(paired, accounts, dividends=payments)

    assert result.blockers == ()
    assert _a3_credits(result) == [
        ((0,), Decimal(800), Decimal(800)),
        ((1,), Decimal(0), Decimal(0)),
    ]
    assert [
        (row.account_number, row.nature_of_amount, row.gross_amount_inr) for row in result.a2_rows
    ] == [
        (_FIRST_ACCOUNT, FaA2NatureOfAmount.DIVIDEND, Decimal(800)),
        (_SECOND_ACCOUNT, FaA2NatureOfAmount.NO_AMOUNT, Decimal(0)),
    ]
    _assert_each_accounts_a3_credits_add_up_to_a2(result)


def test_one_accounts_dividend_rows_posted_in_two_years_block_a3_attribution() -> None:
    """#1320 new refusal: the first account holds 4 shares and receives one
    $1-a-share dividend (``P/D`` December 31, 2025) in two rows of 2 shares
    each, the first posted December 31, 2025 and the second January 2, 2026.
    CY 2025 holds only the first row, whose 2 shares are past the display
    tolerance of the 4 held, so A3 credit attribution blocks while A2 files
    the first row's ₹160. Main put both rows in CY 2025 by their ``P/D`` and
    filed ₹320 on the lot."""
    paired, accounts = _two_accounts(
        (_buy("4"), _paid_dividend(3, "2", "2", "1"), _paid_dividend(4, "2", "2", "1")),
        (_buy("1"),),
        held=("4", "1"),
        credited=({"Dividends": "4"}, {}),
    )
    payments = tuple(
        replace(
            payment,
            source_record_date=date(2025, 12, 19),
            dividend=replace(payment.dividend, payment_date=posted),
        )
        for payment, posted in (
            (_dividend_payment(0, 3, "2", holding_date=date(2025, 12, 18), paid=_DEC31), _DEC31),
            (_dividend_payment(0, 4, "2", holding_date=date(2025, 12, 18), paid=_DEC31), _JAN2),
        )
    )

    result = _compute_two(paired, accounts, dividends=payments)

    assert [(item.kind, item.security_config_indices) for item in result.blockers] == [
        (robinhood_fa_mod.RobinhoodFaBlockerKind.A3_CREDIT_ATTRIBUTION, (0,))
    ]
    assert _a3_credits(result) == [((0,), None, None), ((1,), None, None)]
    assert _a2_credits(result) == {0: Decimal(160), 1: Decimal(0)}
