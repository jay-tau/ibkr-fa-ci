"""Focused checks for the Robinhood disposal-to-Schedule-CG adapter."""

from __future__ import annotations

import copy
from collections import defaultdict
from collections.abc import Mapping
from dataclasses import replace
from datetime import date
from decimal import Decimal, localcontext
from fractions import Fraction
from typing import Any, Literal, cast

import pytest

import ibkr_fa.compute.robinhood_capital_gains as robinhood_capital_gains_mod
from ibkr_fa.compute.exact_inr import exact_decimal, exact_whole_rupees
from ibkr_fa.compute.robinhood_capital_gains import (
    _ARITHMETIC_CONTEXT,
    RobinhoodCapitalGainLot,
    RobinhoodCapitalGainsBlockerKind,
    RobinhoodCapitalGainsError,
    RobinhoodCapitalGainsResult,
    _allocate_group,
    _source_cash_within_display_price_band,
    _source_groups,
    compute_robinhood_capital_gains,
    robinhood_sale_price_checks,
)
from ibkr_fa.compute.robinhood_lots import (
    RobinhoodLotBlocker,
    RobinhoodLotBlockerKind,
)
from ibkr_fa.compute.robinhood_options import RobinhoodOptionCapitalGainBucket
from ibkr_fa.compute.schedule_cg_portal_amounts import schedule_cg_portal_amounts
from ibkr_fa.compute.schedule_cg_table_f import ScheduleCgTableFCase, ScheduleCgTableFRow
from ibkr_fa.compute.schedule_fsi import compute_schedule_fsi_from_capital_gains
from ibkr_fa.config import (
    RobinhoodConfig,
    RobinhoodSecurityConfig,
    RobinhoodSecurityReceiptResolution,
    RobinhoodSecurityReceiptTreatment,
)
from ibkr_fa.exceptions import InvariantError
from ibkr_fa.models.money import to_whole_rupees
from ibkr_fa.normalize.robinhood import (
    RobinhoodAccountsReconciliation,
    RobinhoodSecurityEventReference,
    resolve_robinhood_security_identities,
    resolve_robinhood_security_quantities,
)
from ibkr_fa.parser.robinhood import RobinhoodEventKind
from ibkr_fa.types import Isin, Ticker, isin_check_digit
from tests._fx_stubs import AuditedFxStub
from tests._no_echo import CUSIP, HOLDER, assert_no_markers
from tests._robinhood_account_inputs import pair_with_security as _pair_with_security
from tests._robinhood_account_inputs import security_config as _reconciliation_security
from tests._robinhood_anchors import stand_in_rounded_anchor_ledgers
from tests._robinhood_cg import (
    CONSIDERATION_BASIS,
    CONSIDERATION_POSITION,
    PROVEN_GROSS_REFUSAL,
    TRANSFER_EXPENDITURE_BASIS,
    cg_result,
    option_lines,
    sale,
)
from tests._robinhood_cg_inputs import CG_CUSIP as _CUSIP
from tests._robinhood_cg_inputs import CG_FY_END as _FY_END
from tests._robinhood_cg_inputs import CG_FY_START as _FY_START
from tests._robinhood_cg_inputs import CG_RATE as _RATE
from tests._robinhood_cg_inputs import CG_SALE_DATE as _SALE_DATE
from tests._robinhood_cg_inputs import LONG_TERM_BUY as _LONG_TERM_BUY
from tests._robinhood_cg_inputs import SALE_AT_100 as _SALE_AT_100
from tests._robinhood_cg_inputs import SALE_AT_110 as _SALE_AT_110
from tests._robinhood_cg_inputs import SALE_ORDER_DAY as _SALE_DAY
from tests._robinhood_cg_inputs import SALE_ORDER_REASON as _SALE_ORDER_REASON
from tests._robinhood_cg_inputs import SHORT_TERM_BUY as _SHORT_TERM_BUY
from tests._robinhood_cg_inputs import ActivityStandIn as _Activity
from tests._robinhood_cg_inputs import BundleStandIn as _Bundle
from tests._robinhood_cg_inputs import PairedStandIn as _Paired
from tests._robinhood_cg_inputs import canonical_cg_case as _canonical_case
from tests._robinhood_cg_inputs import cg_base_events as _base_events
from tests._robinhood_cg_inputs import cg_base_slices as _base_slices
from tests._robinhood_cg_inputs import cg_config as _config
from tests._robinhood_cg_inputs import cg_from_lots as _compute
from tests._robinhood_cg_inputs import cg_lot_result as _lot_result
from tests._robinhood_cg_inputs import cg_paired as _paired
from tests._robinhood_cg_inputs import cg_reference as _reference
from tests._robinhood_cg_inputs import cg_security as _security
from tests._robinhood_cg_inputs import cg_slice as _slice
from tests._robinhood_cg_inputs import cg_trade as _trade
from tests._robinhood_cg_inputs import sale_order_case as _sale_order_case
from tests._robinhood_cg_inputs import traded_cg_result as _traded_result
from tests._robinhood_refusals import EVENT_REFERENCES
from tests._robinhood_security import configured_securities


def test_allocates_exact_recorded_cash_and_matches_tax_tools_netgain() -> None:
    result = _compute(_base_events(), _lot_result(_base_slices()))

    assert not result.blockers
    assert [item.closed_lot.proceeds_money for item in result.attributed_lots] == [
        Decimal("50.00"),
        Decimal("100.00"),
    ]
    assert sum(
        (item.closed_lot.cost_basis_money for item in result.attributed_lots), Decimal()
    ) == Decimal("50.00")
    assert (
        sum((line.realized_gain_inr for line in result.stcg.stcg_lines), Decimal())
        == (Decimal("150.00") - Decimal("50.00")) * _RATE
    )
    assert len(result.stcg.stcg_lines) == 2
    assert not result.ltcg.ltcg_lines
    # Each lot's gross is its share of the recorded Amount (#1383).
    assert [item.closed_lot.gross_proceeds_money for item in result.attributed_lots] == [
        Decimal("50.00"),
        Decimal("100.00"),
    ]
    assert all(check.source_cash_within_display_price_band for check in result.sale_price_checks)
    assert result.attributed_lots[0].source_slice.movement_references == (
        _reference(3),
        _reference(4),
    )
    assert _CUSIP not in repr(result)
    assert "150.00" not in repr(result)


def test_filing_result_reads_robinhood_scope_and_evidence() -> None:
    """#642: the shared filing result, with Robinhood's position and basis. The
    recorded Amount files as consideration with 0 transfer expenditure, so the
    STCG section and its Table F bands are filled (#1383)."""
    result = _compute(_base_events(), _lot_result(_base_slices()))

    filing = result.filing(())

    assert filing is not None
    # $150.00 of Amount less $50.00 of cost, at ₹85, sold in band (i).
    section = filing.stcg_section
    assert (
        section.consideration_inr,
        section.transfer_expenditure_inr,
        section.cost_of_acquisition_inr,
        section.result_inr,
    ) == (Decimal("12750"), Decimal("0"), Decimal("4250"), Decimal("8500"))
    # The positive STCG blocks nothing (#1360).
    assert filing.blockers == ()
    assert filing.stcg_table_f == ScheduleCgTableFRow(
        ScheduleCgTableFCase.COMPUTED, tuple(Decimal(band) for band in ("8500", "0", "0", "0", "0"))
    )
    assert not result.blockers
    assert filing.listing_venues == ("NASDAQ",)
    assert filing.basis.consideration == CONSIDERATION_BASIS
    assert filing.basis.transfer_expenditure == TRANSFER_EXPENDITURE_BASIS
    assert filing.basis.consideration_position == CONSIDERATION_POSITION


def test_blocked_result_has_no_filing_result() -> None:
    result = _compute(
        _base_events(),
        _lot_result(_base_slices()),
        report_end=date(2025, 12, 31),
    )

    assert result.blockers
    assert result.filing(()) is None


_STCG = RobinhoodOptionCapitalGainBucket.STCG_OTHER_ASSETS
_LTCG = RobinhoodOptionCapitalGainBucket.LTCG_OTHER_ASSETS


@pytest.mark.parametrize(
    ("buckets", "uncovered"),
    [
        ((), (False, False)),
        ((_STCG,), (True, False)),
        ((_LTCG,), (False, True)),
        ((_LTCG, _STCG, _STCG), (True, True)),
    ],
    ids=["none", "stcg", "ltcg", "both"],
)
def test_filing_marks_each_section_holding_an_option_capital_gain(
    buckets: tuple[RobinhoodOptionCapitalGainBucket, ...], uncovered: tuple[bool, bool]
) -> None:
    """#1322: a short-term option line falls in STCG, a long-term one in LTCG."""
    filing = cg_result(sale("12", "10")).filing(option_lines(*buckets))

    assert filing is not None
    assert (filing.stcg_uncovered_disposals, filing.ltcg_uncovered_disposals) == uncovered


def test_price_residual_does_not_replace_recorded_sale_amount() -> None:
    result = _compute(
        _base_events(first_sale_amount="49.98"),
        _lot_result(_base_slices()),
    )

    assert not result.blockers
    assert not result.sale_price_checks[0].source_cash_within_display_price_band
    assert sum(
        (item.closed_lot.proceeds_money for item in result.attributed_lots), Decimal()
    ) == Decimal("149.98")
    # #1383: each lot's gross is its exact share of the recorded Amount, beside
    # the share's rounded Decimal, as its proceeds are.
    lots = [item.closed_lot for item in result.attributed_lots]
    assert [lot.gross_proceeds_money_exact for lot in lots] == [
        Fraction("149.98") / 3,
        Fraction("149.98") * 2 / 3,
    ]
    assert [(lot.gross_proceeds_money, lot.gross_proceeds_money_exact) for lot in lots] == [
        (lot.proceeds_money, lot.proceeds_money_exact) for lot in lots
    ]


def test_multilot_proceeds_are_exact_shares_of_source_cash() -> None:
    """Each slice's proceeds is its exact share of the sale's cash, cut toward zero
    at 60 digits, and the exact shares add back to the cash exactly."""
    source_cash = Decimal("178726.97")
    quantities = tuple(map(Decimal, ("0.05629062", "0.06461048", "0.15847385")))
    source_quantity = sum(quantities, Decimal())
    sale = _trade(
        RobinhoodEventKind.EQUITY_SELL,
        4,
        _SALE_DATE,
        str(source_quantity),
        "1.00",
        str(source_cash),
    )
    _events, groups, checks = _source_groups(_paired((sale,)))
    group = groups[0]
    assert len(checks) == 1
    slices = tuple(
        _slice(
            ordinal,
            (4,),
            quantity=str(quantity),
            cost="1.00",
            original_date=date(2025, 1, ordinal),
        )
        for ordinal, quantity in enumerate(quantities, start=1)
    )
    shares = [
        Fraction(source_cash) * Fraction(quantity) / Fraction(source_quantity)
        for quantity in quantities
    ]

    with localcontext(_ARITHMETIC_CONTEXT):
        lots = _allocate_group(group, slices, _security())
        cut_shares = [exact_decimal(share) for share in shares]

    # #1241: the exact shares, not a 60-digit residual, now add back to the cash.
    assert [item.closed_lot.proceeds_money for item in lots] == cut_shares
    exponent = cut_shares[0].as_tuple().exponent
    assert isinstance(exponent, int) and exponent < -2
    assert [item.closed_lot.proceeds_money_exact for item in lots] == shares
    assert sum(shares, Fraction()) == source_cash


def test_routes_exact_24_month_boundary_and_one_day_after() -> None:
    events = (
        _trade(
            RobinhoodEventKind.EQUITY_BUY,
            1,
            date(2023, 6, 15),
            "1.0000",
            "10.00",
            "-10.00",
        ),
        _trade(
            RobinhoodEventKind.EQUITY_BUY,
            2,
            date(2023, 6, 14),
            "1.0000",
            "10.00",
            "-10.00",
        ),
        _trade(
            RobinhoodEventKind.EQUITY_SELL,
            3,
            _SALE_DATE,
            "2.0000",
            "50.00",
            "100.00",
        ),
    )
    slices = (
        _slice(
            1,
            (3,),
            quantity="1.0000",
            cost="10.00",
            original_date=date(2023, 6, 15),
        ),
        _slice(
            2,
            (3,),
            quantity="1.0000",
            cost="10.00",
            original_date=date(2023, 6, 14),
        ),
    )

    result = _compute(events, _lot_result(slices))

    assert len(result.stcg.stcg_lines) == 1
    assert len(result.ltcg.ltcg_lines) == 1
    assert result.stcg.stcg_lines[0].open_date == date(2023, 6, 15)
    assert result.ltcg.ltcg_lines[0].open_date == date(2023, 6, 14)


def test_internal_transfer_retains_original_holding_date() -> None:
    events = (
        _trade(
            RobinhoodEventKind.EQUITY_BUY,
            1,
            date(2023, 1, 1),
            "1.0000",
            "10.00",
            "-10.00",
        ),
        _trade(
            RobinhoodEventKind.EQUITY_SELL,
            2,
            _SALE_DATE,
            "1.0000",
            "50.00",
            "50.00",
        ),
    )
    item = _slice(
        1,
        (2,),
        quantity="1.0000",
        cost="10.00",
        original_date=date(2023, 1, 1),
        entry_date=date(2025, 1, 1),
    )

    result = _compute(events, _lot_result((item,)))

    assert not result.blockers
    assert not result.stcg.stcg_lines
    assert len(result.ltcg.ltcg_lines) == 1
    assert result.ltcg.ltcg_lines[0].open_date == date(2023, 1, 1)


@pytest.mark.parametrize(
    ("sale_date", "security", "expected"),
    [
        (
            date(2024, 7, 22),
            _security(),
            RobinhoodCapitalGainsBlockerKind.PRE_CUTOVER_HOLDING_RULE,
        ),
        (
            _SALE_DATE,
            _security(asset_class="OPT"),
            RobinhoodCapitalGainsBlockerKind.UNSUPPORTED_SECURITY_CLASS,
        ),
    ],
)
def test_unsupported_holding_rule_inputs_are_typed_blockers(
    sale_date: date,
    security: RobinhoodSecurityConfig,
    expected: RobinhoodCapitalGainsBlockerKind,
) -> None:
    events = (
        _trade(
            RobinhoodEventKind.EQUITY_BUY,
            1,
            date(2024, 1, 1),
            "1.0000",
            "10.00",
            "-10.00",
        ),
        _trade(
            RobinhoodEventKind.EQUITY_SELL,
            2,
            sale_date,
            "1.0000",
            "50.00",
            "50.00",
        ),
    )
    item = _slice(
        1,
        (2,),
        quantity="1.0000",
        cost="10.00",
        original_date=date(2024, 1, 1),
    )
    fy_start = date(2024, 4, 1) if sale_date.year == 2024 else _FY_START
    fy_end = date(fy_start.year + 1, 3, 31)

    result = _compute(
        events,
        _lot_result((item,)),
        config=_config(securities=(security,)),
        financial_year_start=fy_start,
        financial_year_end=fy_end,
    )

    assert not result.attributed_lots
    assert result.blockers[0].kind is expected


def test_upstream_lot_blocker_is_applied_to_the_sale_group() -> None:
    blocker = RobinhoodLotBlocker(
        account_index=0,
        cusip=_CUSIP,
        kind=RobinhoodLotBlockerKind.QUANTITY_SHORTFALL,
        event_references=(_reference(3), _reference(4)),
    )

    result = _compute(_base_events(), _lot_result((), blockers=(blocker,)))

    assert not result.attributed_lots
    assert result.blockers[0].kind is RobinhoodCapitalGainsBlockerKind.LOT_ATTRIBUTION
    assert result.blockers[0].source_lot_blockers == (blocker,)
    assert _CUSIP not in repr(result.blockers[0])


def test_missing_config_identity_blocks_even_resolved_lot_economics() -> None:
    paired, config = _canonical_case()
    config = replace(config, securities=())

    result = compute_robinhood_capital_gains(
        paired,
        config=config,
        securities=configured_securities(config),
        financial_year_start=date(2024, 4, 1),
        financial_year_end=date(2025, 3, 31),
        fx_policy=AuditedFxStub(_RATE),
    )

    assert not result.attributed_lots
    assert result.blockers[0].kind is RobinhoodCapitalGainsBlockerKind.IDENTITY
    assert result.blockers[0].source_identity_blockers


def test_incomplete_account_report_blocks_all_output() -> None:
    result = _compute(
        _base_events(),
        _lot_result(_base_slices()),
        report_end=date(2025, 12, 31),
    )

    assert not result.attributed_lots
    assert not result.stcg.stcg_lines
    assert {blocker.kind for blocker in result.blockers} == {
        RobinhoodCapitalGainsBlockerKind.EVIDENCE_COVERAGE
    }
    assert result.blockers[0].account_index is None
    assert result.blockers[0].disposal_references == (_reference(3), _reference(4))


def test_mid_fy_opening_clips_required_report_start() -> None:
    opening_date = date(2025, 10, 29)
    first, second = _config().accounts
    config = replace(
        _config(),
        accounts=(replace(second, opening_date=opening_date), first),
    )

    result = _compute(
        _base_events(),
        _lot_result(_base_slices()),
        config=config,
        report_start=_FY_START,
        report_end=_FY_END,
        second_report_start=opening_date,
    )
    assert not result.blockers

    result = _compute(
        _base_events(),
        _lot_result(_base_slices()),
        config=config,
        report_start=_FY_START,
        report_end=_FY_END,
        second_report_start=date(2025, 10, 30),
    )
    assert result.blockers[0].kind is RobinhoodCapitalGainsBlockerKind.EVIDENCE_COVERAGE
    assert result.blockers[0].account_index == 1


def test_out_of_fy_sale_is_tracked_without_blocking_current_fy() -> None:
    old_sale = _trade(
        RobinhoodEventKind.EQUITY_SELL,
        5,
        date(2024, 2, 1),
        "1.0000",
        "30.00",
        "30.00",
    )

    result = _compute((*_base_events(), old_sale), _lot_result(_base_slices()))

    assert not result.blockers
    assert len(result.stcg.stcg_lines) == 2
    assert result.excluded_out_of_fy_disposal_references == ((_reference(5),),)
    assert len(result.sale_price_checks) == 3


def test_sale_price_checks_cover_every_sale_in_any_year() -> None:
    """#1085: Schedule FA's warning reads these checks, which need no FX, lots or FY."""
    late_sale = _trade(
        RobinhoodEventKind.EQUITY_SELL,
        5,
        date(2027, 1, 10),
        "1.0000",
        "50.00",
        "60.00",
    )

    checks = robinhood_sale_price_checks(_paired((*_base_events(), late_sale)))

    assert [
        (check.event_reference, check.source_cash_within_display_price_band) for check in checks
    ] == [(_reference(3), True), (_reference(4), True), (_reference(5), False)]


def test_sale_price_check_keeps_its_precision_in_any_caller_context() -> None:
    """At precision 28 the band's upper edge rounds up to $50.005, which takes in
    $50.01 less its half cent; at 60 the edge stays below it."""
    sale = _trade(
        RobinhoodEventKind.EQUITY_SELL,
        3,
        _SALE_DATE,
        "0." + "9" * 30,
        "50.00",
        "50.01",
    )

    with localcontext(prec=28):
        assert _source_cash_within_display_price_band(sale)
        (check,) = robinhood_sale_price_checks(_paired((sale,)))

    assert not check.source_cash_within_display_price_band


_MARKED_ACCOUNT = "999900001"
_MARKED_ISSUER = "SAMPLE ISSUER AQ INC"
_MARKED_SYMBOL = "XQAQ"
_MARKED_CUSIP = "99992A109"


@pytest.mark.parametrize(
    ("name", "value"),
    [
        *(
            (name, value)
            for name in ("quantity", "price_usd", "amount_usd")
            for value in (None, Decimal("NaN"), Decimal("Infinity"), Decimal(0), Decimal(-1))
        ),
        ("cusip", None),
    ],
)
def test_sale_price_checks_refuse_an_invalid_sale_without_echo(
    name: str, value: Decimal | None
) -> None:
    """Built past the event model's own checks, which refuse most of these."""
    sale = replace(
        _trade(RobinhoodEventKind.EQUITY_SELL, 3, _SALE_DATE, "1.0000", "50.00", "50.00"),
        instrument=_MARKED_SYMBOL,
        description=f"{_MARKED_ISSUER} {_MARKED_CUSIP}",
        issuer_name=_MARKED_ISSUER,
        cusip=_MARKED_CUSIP,
    )
    unchecked = copy.copy(sale)
    object.__setattr__(unchecked, name, value)
    paired = cast("_Paired", _paired((unchecked,)))
    marked = replace(paired.accounts[0], activity=_Activity(_Bundle(_MARKED_ACCOUNT)))

    with pytest.raises(RobinhoodCapitalGainsError) as caught:
        robinhood_sale_price_checks(
            cast("RobinhoodAccountsReconciliation", _Paired((marked, paired.accounts[1])))
        )

    message = str(caught.value)
    assert_no_markers(message, _MARKED_ACCOUNT, _MARKED_ISSUER, _MARKED_SYMBOL, _MARKED_CUSIP)
    assert message == "resolved sale event lacks valid quantity, price, cash, or identity"


def test_quantity_drift_refuses_without_private_values() -> None:
    bad_slices = (
        _base_slices()[0],
        replace(_base_slices()[1], quantity=Decimal("1.9999")),
    )

    with pytest.raises(RobinhoodCapitalGainsError) as caught:
        _compute(_base_events(), _lot_result(bad_slices))

    rendered = str(caught.value)
    assert "source quantity" in rendered
    assert _CUSIP not in rendered
    assert "149" not in rendered


def test_public_api_recomputes_canonical_lots_from_paired_evidence() -> None:
    paired, config = _canonical_case()

    result = compute_robinhood_capital_gains(
        paired,
        config=config,
        securities=configured_securities(config),
        financial_year_start=date(2024, 4, 1),
        financial_year_end=date(2025, 3, 31),
        fx_policy=AuditedFxStub(_RATE),
    )

    assert not result.blockers
    assert len(result.attributed_lots) == 1
    assert result.attributed_lots[0].closed_lot.cost_basis_money == Decimal("10.00")
    assert result.attributed_lots[0].closed_lot.proceeds_money == Decimal("20.00")
    # #1383: the recorded Amount is the lot's gross; one lot needs no exact share.
    assert result.attributed_lots[0].closed_lot.gross_proceeds_money == Decimal("20.00")
    assert result.attributed_lots[0].closed_lot.gross_proceeds_money_exact is None


@pytest.mark.parametrize(
    ("start", "end"),
    [
        pytest.param(date(2024, 4, 1), date(2025, 2, 14), id="end-before-the-2025-02-15-sale"),
        pytest.param(date(2024, 4, 1), date(2026, 3, 31), id="two-financial-years"),
        pytest.param(date(2024, 4, 2), date(2025, 3, 31), id="start-after-april-1"),
    ],
)
def test_capital_gains_refuse_a_period_other_than_one_financial_year(
    start: date, end: date
) -> None:
    """#1417: the canonical case's share, bought 2025-01-10 and sold 2025-02-15,
    computes for FY 2024-25 (the test above). A period that ends before the sale,
    spans two years or starts after April 1 refuses."""
    paired, config = _canonical_case()
    with pytest.raises(
        RobinhoodCapitalGainsError,
        match=r"\Acapital gains require one complete April-to-March financial year\Z",
    ):
        compute_robinhood_capital_gains(
            paired,
            config=config,
            securities=configured_securities(config),
            financial_year_start=start,
            financial_year_end=end,
            fx_policy=AuditedFxStub(_RATE),
        )


def test_public_api_receipt_fallback_matches_explicit_resolved_quantities() -> None:
    # Synthetic reward: 0.052876 displays as 0.0529; 0.38245 + 0.052876 = 0.435326.
    paired, accounts = _pair_with_security(
        include_receipt=True,
        position_quantity_override=Decimal("0.435326"),
        managed_quantity=Decimal("0.38245"),
        managed_cost_usd=Decimal("45.89"),
        receipt_quantity=Decimal("0.0529"),
    )
    security = _reconciliation_security()
    identities = resolve_robinhood_security_identities(paired, (security,))
    receipt = next(
        event
        for account in paired.accounts
        for event in account.evidence.events
        if event.kind is RobinhoodEventKind.SECURITY_RECEIPT
    )
    account_number = paired.accounts[1].activity.primary_bundle.account_number
    resolution = RobinhoodSecurityReceiptResolution(
        account_number=account_number,
        source_record_index=receipt.source_record_index,
        cusip=security.cusip,
        treatment=RobinhoodSecurityReceiptTreatment.PROMOTIONAL_REWARD,
        exact_quantity=Decimal("0.052876"),
        reward_fair_market_value_usd=Decimal("6.35"),
        income_reported_financial_year=2024,
        income_reported=True,
        source="reward confirmation and filed return",
        reason="promotional reward",
    )
    config = RobinhoodConfig(
        accounts=accounts,
        securities=(security,),
        security_receipt_resolutions=(resolution,),
    )
    explicit = resolve_robinhood_security_quantities(paired, identities, (resolution,))

    expected = compute_robinhood_capital_gains(
        paired,
        config=config,
        securities=configured_securities(config),
        financial_year_start=date(2024, 4, 1),
        financial_year_end=date(2025, 3, 31),
        fx_policy=AuditedFxStub(_RATE),
        quantities=explicit,
    )
    actual = compute_robinhood_capital_gains(
        paired,
        config=config,
        securities=configured_securities(config),
        financial_year_start=date(2024, 4, 1),
        financial_year_end=date(2025, 3, 31),
        fx_policy=AuditedFxStub(_RATE),
    )

    assert actual == expected


def test_one_incomplete_account_globally_blocks_complete_account_sales() -> None:
    paired, config = _canonical_case()
    incomplete = replace(
        paired,
        accounts=(
            replace(
                paired.accounts[0],
                evidence=replace(
                    paired.accounts[0].evidence,
                    end_date=date(2025, 2, 28),
                ),
            ),
            paired.accounts[1],
        ),
    )

    result = compute_robinhood_capital_gains(
        incomplete,
        config=config,
        securities=configured_securities(config),
        financial_year_start=date(2024, 4, 1),
        financial_year_end=date(2025, 3, 31),
        fx_policy=AuditedFxStub(_RATE),
    )

    assert not result.attributed_lots
    assert result.blockers[0].kind is RobinhoodCapitalGainsBlockerKind.EVIDENCE_COVERAGE
    assert result.blockers[0].account_index == 0
    assert result.blockers[0].disposal_references == (RobinhoodSecurityEventReference(1, 4),)


def _gross_differs(*, sale_date: date, account_index: int, sale_ordinal: int, lot_ordinal: int):
    item = sale(
        "12",
        "10",
        long_term=True,
        sale_date=sale_date,
        account_index=account_index,
        cusip=CUSIP,
        sale_ordinal=sale_ordinal,
        lot_ordinal=lot_ordinal,
    )
    return replace(
        item,
        closed_lot=replace(
            item.closed_lot,
            symbol=Ticker(HOLDER),
            description="Synthetic Sale",
            isin=Isin(f"US{CUSIP}{isin_check_digit(f'US{CUSIP}')}"),
            gross_proceeds_money=Decimal("10.02"),
        ),
    )


def test_proven_gross_refusal_names_sales_by_movement_reference_only() -> None:
    """#752: in sale-date order, one entry per sale, and never a symbol, CUSIP
    or date (#1044). Each lot's gross is its Amount (#1383), so parsed input
    can't reach this; each lot here replaces its gross."""
    later = _gross_differs(
        sale_date=date(2025, 4, 2), account_index=0, sale_ordinal=3, lot_ordinal=1
    )
    earlier = tuple(
        _gross_differs(sale_date=date(2025, 3, 1), account_index=1, sale_ordinal=7, lot_ordinal=lot)
        for lot in (1, 2)
    )

    with pytest.raises(RobinhoodCapitalGainsError) as raised:
        cg_result(later, *earlier)

    message = str(raised.value)
    assert message == f"{PROVEN_GROSS_REFUSAL} {EVENT_REFERENCES}1:7; 0:3"
    assert_no_markers(message, "2025-03-01", "2025-04-02", HOLDER, CUSIP)


def test_a_gross_without_its_exact_amount_share_is_refused_as_differing() -> None:
    """#1383: #752's refusal compares the exact shares too, so a split sale's
    lot whose gross keeps only the rounded Decimal of its exact Amount share
    is refused."""
    result = _compute(_base_events(first_sale_amount="49.98"), _lot_result(_base_slices()))
    first, second = result.attributed_lots
    assert first.closed_lot.proceeds_money_exact is not None
    rounded = replace(
        first,
        closed_lot=replace(
            first.closed_lot,
            gross_proceeds_money=first.closed_lot.proceeds_money,
            gross_proceeds_money_exact=None,
        ),
    )

    with pytest.raises(RobinhoodCapitalGainsError) as raised:
        replace(result, attributed_lots=(rounded, second))

    assert str(raised.value) == f"{PROVEN_GROSS_REFUSAL} {EVENT_REFERENCES}0:3, 0:4"


# A sale Amount whose digit run is a no-echo marker (#1383).
_MARKED_AMOUNT = "739144615.00"
_MARKED_DIGITS = "739144615"
_NO_GROSS_REFUSAL = (
    "InvariantError: a Robinhood capital-gain lot has no gross proceeds; each lot "
    "files its recorded Amount as its gross"
)


def _without_gross(item: RobinhoodCapitalGainLot) -> RobinhoodCapitalGainLot:
    return replace(
        item,
        closed_lot=replace(
            item.closed_lot, gross_proceeds_money=None, gross_proceeds_money_exact=None
        ),
    )


def test_a_lot_without_gross_proceeds_is_refused_naming_no_amount() -> None:
    """#1383: both lot builders set each lot's gross to its recorded Amount, so
    a lot without one lost it and must not be filed. Parsed input can't reach
    this; the refusal is fixed text."""
    buy_date = date(2025, 1, 15)
    events = (
        _trade(RobinhoodEventKind.EQUITY_BUY, 1, buy_date, "1.0000", "10.00", "-10.00"),
        _trade(
            RobinhoodEventKind.EQUITY_SELL, 2, _SALE_DATE, "1.0000", _MARKED_AMOUNT, _MARKED_AMOUNT
        ),
    )
    slices = (_slice(1, (2,), quantity="1.0000", cost="10.00", original_date=buy_date),)
    result = _compute(events, _lot_result(slices))
    (item,) = result.attributed_lots
    assert item.closed_lot.proceeds_money == Decimal(_MARKED_AMOUNT)

    with pytest.raises(InvariantError) as raised:
        replace(result, attributed_lots=(_without_gross(item),))

    assert str(raised.value) == _NO_GROSS_REFUSAL
    assert raised.value.context == ()
    assert raised.value.__cause__ is None
    assert raised.value.__context__ is None
    for text in (str(raised.value), repr(raised.value), repr(raised.value.context)):
        assert_no_markers(text, _MARKED_DIGITS)


def test_a_lot_without_gross_proceeds_is_refused_before_a_differing_gross() -> None:
    """#1383: the missing-gross refusal runs before #752's differ refusal."""
    differing = _gross_differs(
        sale_date=date(2025, 4, 2), account_index=0, sale_ordinal=3, lot_ordinal=1
    )
    missing = _without_gross(sale("12", "10", lot_ordinal=5, sale_ordinal=6))

    with pytest.raises(InvariantError) as raised:
        cg_result(differing, missing)

    assert str(raised.value) == _NO_GROSS_REFUSAL


# ---- Exact lot and sale shares (#1241) ----------------------------------------

# Buy 3 shares for $10.00, then sell 1 for $3.00 (FY 2024-25) and 2 for $10.00.
_THIRDS_BUY = (date(2025, 1, 10), "3.0000", "3.33", "-10.00")
_THIRDS_FIRST_SALE = (date(2025, 2, 15), "1.0000", "3.00", "3.00")
# Buy 1 share long-term and 2 short-term, then sell all 3 for $10.00.
_PROCEEDS_TRADES = (
    (date(2022, 12, 1), "1.0000", "4.00", "-4.00"),
    (date(2025, 1, 10), "2.0000", "3.00", "-6.00"),
    (date(2025, 2, 15), "3.0000", "3.33", "10.00"),
)


def _traded_gains(
    *trades: tuple[date, str, str, str], **kwargs: Any
) -> RobinhoodCapitalGainsResult:
    """A blocker-free :func:`_traded_result`."""
    result = _traded_result(*trades, **kwargs)
    assert not result.blockers
    return result


@pytest.mark.parametrize(
    ("trades", "rate", "report_start", "schedule", "filed", "exact_usd"),
    [
        pytest.param(
            (_THIRDS_BUY, _THIRDS_FIRST_SALE, (date(2025, 5, 15), "2.0000", "5.00", "10.00")),
            Decimal("84.15"),
            date(2024, 4, 1),
            "STCG",
            # Cost 10/3 x 84.15 = 280.50 (main: 280.4999...97195 -> 280, balance -28).
            (252, 281, -29),
            [(Fraction(10, 3), Fraction(3))],
            id="cost-two-fys",
        ),
        pytest.param(
            (_THIRDS_BUY, _THIRDS_FIRST_SALE, (date(2025, 3, 20), "2.0000", "5.00", "10.00")),
            {date(2025, 2, 15): Decimal("84.25"), date(2025, 3, 20): Decimal("84.10")},
            date(2024, 4, 1),
            "STCG",
            # Cost 10/3 x 84.25 + 20/3 x 84.10 = 841.50 (main: 841).
            (1094, 842, 252),
            [(Fraction(10, 3), Fraction(3)), (Fraction(20, 3), Fraction(10))],
            id="cost-two-rates",
        ),
        pytest.param(
            _PROCEEDS_TRADES,
            Decimal("84.15"),
            date(2022, 4, 1),
            "LTCG",
            # Proceeds 10/3 x 84.15 = 280.50 (main: the 60-digit share -> 280).
            (281, 337, -56),
            [(Fraction(4), Fraction(10, 3))],
            id="proceeds-ltcg",
        ),
        pytest.param(
            _PROCEEDS_TRADES,
            Decimal("84.15"),
            date(2022, 4, 1),
            "STCG",
            # Proceeds 20/3 x 84.15 = 561 (main: 561 too, from the residual).
            (561, 505, 56),
            [(Fraction(6), Fraction(20, 3))],
            id="proceeds-stcg",
        ),
    ],
)
def test_split_shares_file_the_exact_half_up(
    trades: tuple[tuple[date, str, str, str], ...],
    rate: Decimal | Mapping[date, Decimal],
    report_start: date,
    schedule: Literal["STCG", "LTCG"],
    filed: tuple[int, int, int],
    exact_usd: list[tuple[Fraction, Fraction]],
) -> None:
    """#1241's CG repros in FY 2024-25: a total whose exact value sits on 50 paise
    files the exact HALF_UP, not the rounding of a 20- or 60-digit share."""
    result = _traded_gains(
        *trades,
        financial_year_start=date(2024, 4, 1),
        rate=rate,
        report_start=report_start,
        report_end=date(2025, 6, 30),
    )

    lines = result.stcg.stcg_lines if schedule == "STCG" else result.ltcg.ltcg_lines
    amounts = schedule_cg_portal_amounts(lines, schedule=schedule)
    assert (
        amounts.consideration_inr,
        amounts.cost_of_acquisition_inr,
        amounts.balance_inr,
    ) == filed
    assert [
        None
        if line.exact_amounts is None
        else (
            line.exact_amounts.cost_basis_usd,
            line.exact_amounts.proceeds_usd,
        )
        for line in lines
    ] == exact_usd


def test_split_cost_files_fsi_income_equal_to_the_cg_balance() -> None:
    """#1241's FSI repro: FY 2025-26 sells the last 2 of the 3 shares, whose exact
    cost $20/3 is ₹561, so CG files a ₹281 balance and FSI must file ₹281
    of capital-gains income too (main: 280.4999...97195 -> ₹280)."""
    result = _traded_gains(
        _THIRDS_BUY,
        _THIRDS_FIRST_SALE,
        (date(2025, 5, 15), "2.0000", "5.00", "10.00"),
        financial_year_start=date(2025, 4, 1),
    )

    amounts = schedule_cg_portal_amounts(result.stcg.stcg_lines, schedule="STCG")
    (row,) = compute_schedule_fsi_from_capital_gains(result.stcg, result.ltcg).rows
    assert (
        amounts.consideration_inr,
        amounts.cost_of_acquisition_inr,
        amounts.balance_inr,
    ) == (842, 561, 281)
    assert to_whole_rupees(row.income_inr) == amounts.balance_inr
    assert row.income_inr == Decimal("280.5")


def test_shares_that_divide_exactly_file_their_decimal_values() -> None:
    """Control (#1241): 1 of 2 shares bought for $10.00 costs exactly $5.00, so no
    lot or line carries a rational and the filed values are the Decimal ones.
    The other share sells in FY 2025-26, so no position stays open."""
    result = _traded_gains(
        (date(2025, 1, 10), "2.0000", "5.00", "-10.00"),
        (date(2025, 2, 15), "1.0000", "6.00", "6.00"),
        (date(2025, 5, 15), "1.0000", "6.00", "6.00"),
        financial_year_start=date(2024, 4, 1),
        report_end=date(2025, 6, 30),
    )

    amounts = schedule_cg_portal_amounts(result.stcg.stcg_lines, schedule="STCG")
    assert (
        amounts.consideration_inr,
        amounts.cost_of_acquisition_inr,
        amounts.balance_inr,
        amounts.gain_inr_full_precision,
    ) == (505, 421, 84, Decimal("84.15"))
    assert [
        (lot.cost_basis_money, lot.proceeds_money)
        for lot in (item.closed_lot for item in result.attributed_lots)
    ] == [(Decimal("5.00"), Decimal("6.00"))]
    assert [
        (lot.cost_basis_money_exact, lot.proceeds_money_exact)
        for lot in (item.closed_lot for item in result.attributed_lots)
    ] == [(None, None)]
    assert [line.exact_amounts for line in result.stcg.stcg_lines] == [None]


# ---- Exact pro-rata quantities (#1263) ----------------------------------------


def _exact_or_decimal(value: Decimal, exact: Fraction | None) -> Fraction:
    """The exact amount: ``exact``, else ``value``, which is exact where ``exact`` is ``None``."""
    return Fraction(value) if exact is None else exact


@pytest.mark.parametrize(
    ("bought", "report_start", "schedule"),
    [
        pytest.param(date(2025, 1, 10), date(2024, 4, 1), "STCG", id="stcg"),
        pytest.param(date(2022, 12, 1), date(2022, 4, 1), "LTCG", id="ltcg"),
    ],
)
def test_later_sale_of_a_pro_rata_remainder_files_exact_lot_shares(
    bought: date, report_start: date, schedule: Literal["STCG", "LTCG"]
) -> None:
    """#1263's CG control: three same-day 1-share lots bought for $10.00 each; 1
    share sells for $3.00, split pro rata, then the other 2 for $10.00, all at
    ₹84.15. The schedule files the exact totals' HALF_UP: consideration
    13 x 84.15 = 1093.95 -> ₹1094 and cost 30 x 84.15 = 2524.50 -> ₹2525, as
    on main. Each lot's later-sale line now has exactly $10/3 of the proceeds
    beside its $20/3 cost, and each lot's quantities across both sales add back
    to exactly 1 share. Main split the proceeds by the rounded
    0.66666666666666666667/...66 quantities, so its per-line proceeds aren't
    exact thirds (red there), while its filed totals match (a control)."""
    buy = (bought, "1.0000", "10.00", "-10.00")
    later_date = date(2025, 3, 20)
    result = _traded_gains(
        buy,
        buy,
        buy,
        (date(2025, 2, 15), "1.0000", "3.00", "3.00"),
        (later_date, "2.0000", "5.00", "10.00"),
        financial_year_start=date(2024, 4, 1),
        report_start=report_start,
    )

    lines = result.stcg.stcg_lines if schedule == "STCG" else result.ltcg.ltcg_lines
    amounts = schedule_cg_portal_amounts(lines, schedule=schedule)
    assert (
        amounts.consideration_inr,
        amounts.cost_of_acquisition_inr,
        amounts.balance_inr,
    ) == (1094, 2525, -1431)
    rate = Fraction("84.15")
    lots = [item.closed_lot for item in result.attributed_lots]
    exact_proceeds = sum(
        (_exact_or_decimal(lot.proceeds_money, lot.proceeds_money_exact) for lot in lots),
        Fraction(),
    )
    exact_cost = sum(
        (_exact_or_decimal(lot.cost_basis_money, lot.cost_basis_money_exact) for lot in lots),
        Fraction(),
    )
    assert (exact_proceeds, exact_cost) == (13, 30)
    assert amounts.consideration_inr == exact_whole_rupees(exact_proceeds * rate)
    assert amounts.cost_of_acquisition_inr == exact_whole_rupees(exact_cost * rate)
    later = sorted(
        (item for item in result.attributed_lots if item.closed_lot.close_date_time == later_date),
        key=lambda item: item.source_slice.lot_id,
    )
    assert [
        (
            _exact_or_decimal(lot.proceeds_money, lot.proceeds_money_exact),
            _exact_or_decimal(lot.cost_basis_money, lot.cost_basis_money_exact),
        )
        for lot in (item.closed_lot for item in later)
    ] == [(Fraction(10, 3), Fraction(20, 3))] * 3
    assert [item.source_slice.exact_quantity for item in later] == [Fraction(2, 3)] * 3
    sold: defaultdict[RobinhoodSecurityEventReference, Fraction] = defaultdict(Fraction)
    for item in result.attributed_lots:
        sold[item.source_slice.lot_id] += _exact_or_decimal(
            item.source_slice.quantity, item.source_slice.exact_quantity
        )
    assert sorted(sold.values()) == [1, 1, 1]


def test_later_pro_rata_sale_files_the_exact_cost_the_decimal_ledger_dropped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#1263: three same-day 1-share lots bought for $10.00 each; 1 share sells in
    February, 1E-61 shares in March (FY 2024-25), then 1 share for $10.00 in May
    (FY 2025-26). The 60-digit Decimal ledger drops the 1E-61 from each
    remainder, so main split the May sale's cost over the rounded group
    quantity and filed a cost just below $10.00: ₹841 against ₹842 of
    consideration at ₹84.15. Split by the exact quantities, the three lots give
    up exactly 1/3 share and $10/3 each, so the cost is exactly $10.00, which
    is ₹841.50 and files ₹842 with a balance of 0. The lots read the anchor
    as main's 28-digit replay gave it: the exact 1 - 1E-61 shares left need
    more digits than their ledger has (#1308)."""
    stand_in_rounded_anchor_ledgers(monkeypatch, robinhood_capital_gains_mod)
    buy = (date(2025, 1, 10), "1.0000", "10.00", "-10.00")
    result = _traded_gains(
        buy,
        buy,
        buy,
        (date(2025, 2, 15), "1.0000", "10.00", "10.00"),
        (date(2025, 3, 15), "1E-61", "10.00", "1E-60"),
        (date(2025, 5, 15), "1.0000", "10.00", "10.00"),
        financial_year_start=date(2025, 4, 1),
        report_start=date(2024, 4, 1),
        held=Decimal(1),
    )

    amounts = schedule_cg_portal_amounts(result.stcg.stcg_lines, schedule="STCG")
    assert (
        amounts.consideration_inr,
        amounts.cost_of_acquisition_inr,
        amounts.balance_inr,
    ) == (842, 842, 0)
    lots = [item.closed_lot for item in result.attributed_lots]
    assert sum(
        (_exact_or_decimal(lot.cost_basis_money, lot.cost_basis_money_exact) for lot in lots),
        Fraction(),
    ) == Fraction(10)
    assert [item.source_slice.exact_quantity for item in result.attributed_lots] == [
        Fraction(1, 3)
    ] * 3


# ---- #1540: same-day sales whose order decides the short/long-term split ----


def test_same_day_sales_at_different_cash_per_share_over_both_terms_block_cg() -> None:
    """#1540: on d081de8 each lot got $105 and STCG ₹7,225 / LTCG -₹1,275 were
    filed, though the sale order the CSV doesn't give decides the split."""
    result = _sale_order_case()

    assert result.attributed_lots == ()
    assert not result.stcg.stcg_lines
    assert not result.ltcg.ltcg_lines
    [blocker] = result.blockers
    assert blocker.kind is RobinhoodCapitalGainsBlockerKind.ORDERING_AMBIGUITY
    assert blocker.account_index == 1
    assert blocker.disposal_references == (
        RobinhoodSecurityEventReference(1, 5),
        RobinhoodSecurityEventReference(1, 6),
    )
    assert blocker.lot_ids == (
        RobinhoodSecurityEventReference(1, 3),
        RobinhoodSecurityEventReference(1, 4),
    )
    assert robinhood_capital_gains_mod.ordering_ambiguity_reason(result.blockers) == (
        _SALE_ORDER_REASON
    )


@pytest.mark.parametrize(
    ("trades", "stcg", "ltcg"),
    [
        pytest.param(
            (
                _LONG_TERM_BUY,
                _SHORT_TERM_BUY,
                (_SALE_DAY, "1.0000", "105.00", "105.00"),
                (_SALE_DAY, "1.0000", "105.00", "105.00"),
            ),
            (8925, 1700, 7225),
            (8925, 10200, -1275),
            id="equal-cash-per-share",
        ),
        pytest.param(
            (
                (date(2024, 1, 3), "1.0000", "120.00", "-120.00"),
                _SHORT_TERM_BUY,
                _SALE_AT_100,
                _SALE_AT_110,
            ),
            (17850, 11900, 5950),
            None,
            id="both-short-term",
        ),
        pytest.param(
            (
                _LONG_TERM_BUY,
                (date(2023, 2, 1), "1.0000", "20.00", "-20.00"),
                _SALE_AT_100,
                _SALE_AT_110,
            ),
            None,
            (17850, 11900, 5950),
            id="both-long-term",
        ),
        pytest.param(
            (_LONG_TERM_BUY, _SHORT_TERM_BUY, (_SALE_DAY, "2.0000", "105.00", "210.00")),
            (8925, 1700, 7225),
            (8925, 10200, -1275),
            id="one-sale-over-both-terms",
        ),
    ],
)
def test_sale_groups_whose_order_cannot_move_a_term_file_as_before(
    trades: tuple[tuple[date, str, str, str], ...],
    stcg: tuple[int, int, int] | None,
    ltcg: tuple[int, int, int] | None,
) -> None:
    """#1540 controls: equal cash per share, lots all on one side of 24
    months, or one sale give every sale order the same split, so each files
    its consideration, cost and balance as on d081de8."""
    result = _sale_order_case(*trades)

    assert result.blockers == ()
    for lines, schedule, filed in (
        (result.stcg.stcg_lines, "STCG", stcg),
        (result.ltcg.ltcg_lines, "LTCG", ltcg),
    ):
        if filed is None:
            assert not lines
            continue
        amounts = schedule_cg_portal_amounts(lines, schedule=schedule)
        assert (
            amounts.consideration_inr,
            amounts.cost_of_acquisition_inr,
            amounts.balance_inr,
        ) == filed
