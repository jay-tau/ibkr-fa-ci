"""Unit tests for compute_schedule_cg_ltcg_lines."""

from __future__ import annotations

from dataclasses import replace
from datetime import date
from decimal import Decimal

import pytest

from ibkr_fa.compute.lot_matching import BrokerBasisError
from ibkr_fa.compute.schedule_cg import _RULE_115_COST_BASIS_SOURCE, ScheduleCgDroppedLotError
from ibkr_fa.compute.schedule_cg_ltcg import (
    DeferredStcgRow,
    PreCutoverLtcgRow,
    ScheduleCgLtcgComputeResult,
    compute_schedule_cg_ltcg_lines,
)
from ibkr_fa.exceptions import InvariantError
from ibkr_fa.models import BasisProvenance, ClosedLot, StatutoryInrCostBasis
from ibkr_fa.models.schedule_cg_ltcg_row import NEW_REGIME_CUTOVER_DATE
from ibkr_fa.types import CountryCode, Isin, Ticker
from tests._compute_builders import ltcg_closed_lot as make_closed_lot
from tests._fx_stubs import AuditedFxStub
from tests._lot_matching_inputs import BROKER_BASIS_MESSAGE

_FY_2025_26_START = date(2025, 4, 1)
_FY_2025_26_END = date(2026, 3, 31)


def test_empty_input_returns_empty_result() -> None:
    result = compute_schedule_cg_ltcg_lines(
        [],
        financial_year_start=_FY_2025_26_START,
        financial_year_end=_FY_2025_26_END,
        fx_policy=AuditedFxStub(),
    )
    assert result.ltcg_lines == []
    assert result.excluded_stcg == []
    assert result.excluded_pre_2024_07_23 == []
    assert result.excluded_indian_source == []
    assert result.excluded_out_of_fy == []


def test_in_fy_long_term_disposal_produces_one_ltcg_line() -> None:
    lot = make_closed_lot()
    fx = AuditedFxStub({date(2025, 12, 10): Decimal("84")})
    result = compute_schedule_cg_ltcg_lines(
        [lot],
        financial_year_start=_FY_2025_26_START,
        financial_year_end=_FY_2025_26_END,
        fx_policy=fx,
    )
    assert len(result.ltcg_lines) == 1
    line = result.ltcg_lines[0]
    assert line.symbol == Ticker("XQAQ")
    assert line.fx_rate_inr_per_usd == Decimal("84")
    # Single conversion at transfer date: both basis and proceeds at 84.
    assert line.cost_basis_inr == Decimal("1500") * Decimal("84")
    assert line.proceeds_inr == Decimal("2200") * Decimal("84")
    assert line.realized_gain_inr == (Decimal("2200") - Decimal("1500")) * Decimal("84")
    assert line.fx_specified_date == date(2025, 11, 30)


def test_short_term_lot_routed_to_excluded_stcg() -> None:
    lot = make_closed_lot(
        open_date_time=date(2025, 1, 15),
        close_date_time=date(2025, 6, 20),
    )
    result = compute_schedule_cg_ltcg_lines(
        [lot],
        financial_year_start=_FY_2025_26_START,
        financial_year_end=_FY_2025_26_END,
        fx_policy=AuditedFxStub(),
    )
    assert result.ltcg_lines == []
    assert len(result.excluded_stcg) == 1
    assert result.excluded_stcg[0].closed_lot is lot


def test_pre_cutover_long_term_lot_routed_to_excluded_pre_2024_07_23() -> None:
    """A long-term lot disposed BEFORE 2024-07-23 is routed to the
    pre-cutover bucket (old regime needs indexation; out of v1)."""
    lot = make_closed_lot(
        open_date_time=date(2020, 1, 1),
        close_date_time=date(2024, 7, 22),
    )
    result = compute_schedule_cg_ltcg_lines(
        [lot],
        financial_year_start=date(2024, 4, 1),
        financial_year_end=date(2025, 3, 31),
        fx_policy=AuditedFxStub(),
    )
    assert result.ltcg_lines == []
    assert len(result.excluded_pre_2024_07_23) == 1
    deferred = result.excluded_pre_2024_07_23[0]
    assert deferred.closed_lot is lot
    assert deferred.holding_period_days > 720


def test_disposal_on_cutover_date_routed_to_ltcg_lines() -> None:
    """Boundary: 2024-07-23 itself is in scope (first day of new regime)."""
    lot = make_closed_lot(
        open_date_time=date(2020, 1, 1),
        close_date_time=NEW_REGIME_CUTOVER_DATE,
    )
    result = compute_schedule_cg_ltcg_lines(
        [lot],
        financial_year_start=date(2024, 4, 1),
        financial_year_end=date(2025, 3, 31),
        fx_policy=AuditedFxStub(),
    )
    assert len(result.ltcg_lines) == 1
    assert result.excluded_pre_2024_07_23 == []


def test_indian_source_lot_routed_to_defense_in_depth_bucket() -> None:
    lot = make_closed_lot(
        isin=Isin("IN99970W1077"),
        issuer_country_code=CountryCode("IN"),
        description="SAMPLE ISSUER CD LTD",
    )
    result = compute_schedule_cg_ltcg_lines(
        [lot],
        financial_year_start=_FY_2025_26_START,
        financial_year_end=_FY_2025_26_END,
        fx_policy=AuditedFxStub(),
    )
    assert result.ltcg_lines == []
    assert result.excluded_indian_source == [lot]


def test_out_of_fy_lot_routed_to_excluded_bucket() -> None:
    before = make_closed_lot(
        open_date_time=date(2022, 1, 1),
        close_date_time=date(2025, 3, 15),  # pre-FY-start by 17 days
    )
    after = make_closed_lot(
        open_date_time=date(2023, 1, 1),
        close_date_time=date(2026, 4, 15),  # post-FY-end by 15 days
    )
    result = compute_schedule_cg_ltcg_lines(
        [before, after],
        financial_year_start=_FY_2025_26_START,
        financial_year_end=_FY_2025_26_END,
        fx_policy=AuditedFxStub(),
    )
    assert result.ltcg_lines == []
    assert result.excluded_out_of_fy == [before, after]


def test_non_usd_lots_fail_fast_with_invariant_error() -> None:
    lot = make_closed_lot()
    object.__setattr__(lot, "currency_primary", "EUR")
    with pytest.raises(InvariantError):
        compute_schedule_cg_ltcg_lines(
            [lot],
            financial_year_start=_FY_2025_26_START,
            financial_year_end=_FY_2025_26_END,
            fx_policy=AuditedFxStub(),
        )


def test_generator_input_materialised_once() -> None:
    lots = [
        make_closed_lot(),
        make_closed_lot(symbol=Ticker("XQCA"), isin=Isin("US99972B1017")),
    ]

    def gen():
        yield from lots

    result = compute_schedule_cg_ltcg_lines(
        gen(),
        financial_year_start=_FY_2025_26_START,
        financial_year_end=_FY_2025_26_END,
        fx_policy=AuditedFxStub(),
    )
    assert len(result.ltcg_lines) == 2


def test_output_is_sorted_by_close_date_then_symbol() -> None:
    lots = [
        make_closed_lot(
            symbol=Ticker("XQCA"),
            isin=Isin("US99972B1017"),
            description="XQCA (test)",
            close_date_time=date(2025, 12, 5),
        ),
        make_closed_lot(
            symbol=Ticker("XQAB"),
            isin=Isin("US99990A1016"),
            description="SAMPLE ISSUER AB INC",
            close_date_time=date(2025, 12, 5),
        ),
        make_closed_lot(close_date_time=date(2025, 10, 15)),  # the default XQAQ lot
    ]
    result = compute_schedule_cg_ltcg_lines(
        lots,
        financial_year_start=_FY_2025_26_START,
        financial_year_end=_FY_2025_26_END,
        fx_policy=AuditedFxStub(),
    )
    syms = [str(line.symbol) for line in result.ltcg_lines]
    # The earliest close leads although its symbol sorts after XQAB; the
    # same-day pair then follows symbol order, not input order.
    assert syms == ["XQAQ", "XQAB", "XQCA"]


def test_loss_case_preserves_negative_realized_gain_inr() -> None:
    """LTCG losses are valid (Section 74 carry-forward for long-term)."""
    lot = make_closed_lot(
        cost_basis_money=Decimal("2500"),
        proceeds_money=Decimal("1500"),
    )
    result = compute_schedule_cg_ltcg_lines(
        [lot],
        financial_year_start=_FY_2025_26_START,
        financial_year_end=_FY_2025_26_END,
        fx_policy=AuditedFxStub(),
    )
    assert len(result.ltcg_lines) == 1
    assert result.ltcg_lines[0].realized_gain_inr < 0


def test_negative_proceeds_lot_produces_ltcg_line() -> None:
    """Net negative proceeds (commissions > gross) should produce a
    line without aborting at the model invariant — mirrors the STCG
    T5 r5 fix."""
    lot = make_closed_lot(
        cost_basis_money=Decimal("100"),
        proceeds_money=Decimal("-50"),
    )
    result = compute_schedule_cg_ltcg_lines(
        [lot],
        financial_year_start=_FY_2025_26_START,
        financial_year_end=_FY_2025_26_END,
        fx_policy=AuditedFxStub(),
    )
    assert len(result.ltcg_lines) == 1
    line = result.ltcg_lines[0]
    assert line.proceeds_usd < 0
    assert line.proceeds_inr < 0


def test_compute_result_is_frozen_dataclass() -> None:
    result = ScheduleCgLtcgComputeResult(
        ltcg_lines=[],
        excluded_stcg=[],
        excluded_pre_2024_07_23=[],
        excluded_indian_source=[],
        excluded_out_of_fy=[],
    )
    with pytest.raises(AttributeError):
        result.ltcg_lines = []  # ty: ignore[invalid-assignment]


def test_deferred_stcg_row_carries_holding_period() -> None:
    row = DeferredStcgRow(closed_lot=make_closed_lot(), holding_period_days=150)
    assert row.holding_period_days == 150


def test_pre_cutover_row_carries_holding_period() -> None:
    row = PreCutoverLtcgRow(closed_lot=make_closed_lot(), holding_period_days=1500)
    assert row.holding_period_days == 1500


def test_shifted_broker_matched_lot_refused_before_fy_partition() -> None:
    """#599 defence in depth (#640): a broker-matched lot with a moved
    holding-period date is refused even when it falls outside the FY."""
    lot = make_closed_lot(
        open_date_time=date(2023, 5, 18),
        close_date_time=date(2023, 7, 6),
        basis_provenance=BasisProvenance.BROKER_MATCHED,
        holding_period_date_time=date(2023, 4, 12),
    )
    with pytest.raises(BrokerBasisError) as exc:
        compute_schedule_cg_ltcg_lines(
            [lot],
            financial_year_start=_FY_2025_26_START,
            financial_year_end=_FY_2025_26_END,
            fx_policy=AuditedFxStub(),
        )
    assert str(exc.value) == BROKER_BASIS_MESSAGE
    # The old text printed the holding-period date; none of the lot's
    # dates, symbol or ISIN are printed now (#1047).
    for value in ("2023-04-12", "2023-05-18", "2023-07-06", "XQAQ", "US99992A1097"):
        assert value.lower() not in str(exc.value).lower()


# ---- statutory INR cost override (#633) -------------------------------------


def _compute_at_85(lots: list[ClosedLot]) -> ScheduleCgLtcgComputeResult:
    return compute_schedule_cg_ltcg_lines(
        lots,
        financial_year_start=_FY_2025_26_START,
        financial_year_end=_FY_2025_26_END,
        fx_policy=AuditedFxStub(Decimal("85")),
    )


def test_lot_without_override_keeps_the_rule_115_cost() -> None:
    (line,) = _compute_at_85([make_closed_lot()]).ltcg_lines
    assert line.cost_basis_inr == Decimal("1500") * Decimal("85")
    assert line.cost_basis_inr_source == _RULE_115_COST_BASIS_SOURCE
    assert line.realized_gain_inr == (Decimal("2200") - Decimal("1500")) * Decimal("85")


@pytest.mark.parametrize("amount_inr", [Decimal("150000"), Decimal("200000")], ids=["gain", "loss"])
def test_override_replaces_only_the_inr_cost(amount_inr: Decimal) -> None:
    override = StatutoryInrCostBasis(amount_inr=amount_inr, source="employer worksheet")
    statutory = make_closed_lot(
        basis_provenance=BasisProvenance.S49_2AA, cost_basis_inr_override=override
    )
    short_term = replace(statutory, open_date_time=date(2025, 6, 15))
    (base,) = _compute_at_85([make_closed_lot()]).ltcg_lines

    result = _compute_at_85([statutory, short_term])

    (line,) = result.ltcg_lines
    assert line.cost_basis_inr == amount_inr
    assert line.cost_basis_inr_source == "employer worksheet"
    # Proceeds 2200 USD x 85 = 187000 INR: a gain over 150000, a loss under 200000.
    assert line.realized_gain_inr == Decimal("187000") - amount_inr
    assert (
        replace(
            line,
            cost_basis_inr=base.cost_basis_inr,
            cost_basis_inr_source=base.cost_basis_inr_source,
            realized_gain_inr=base.realized_gain_inr,
        )
        == base
    )
    assert [row.closed_lot for row in result.excluded_stcg] == [short_term]


# ---- strict mode (#633) -----------------------------------------------------


def _compute_fy_2024_25(lots: list[ClosedLot], *, strict: bool) -> ScheduleCgLtcgComputeResult:
    return compute_schedule_cg_ltcg_lines(
        lots,
        financial_year_start=date(2024, 4, 1),
        financial_year_end=date(2025, 3, 31),
        fx_policy=AuditedFxStub(),
        strict=strict,
    )


_IN_FY_LONG_TERM = {"open_date_time": date(2022, 1, 1), "close_date_time": date(2024, 12, 10)}
_PRE_CUTOVER = {"open_date_time": date(2020, 1, 1), "close_date_time": date(2024, 7, 22)}
_INDIAN_SOURCE = {
    **_IN_FY_LONG_TERM,
    "isin": Isin("IN99970W1077"),
    "issuer_country_code": CountryCode("IN"),
}
_OUT_OF_FY = {"open_date_time": date(2022, 1, 1), "close_date_time": date(2025, 6, 1)}


@pytest.mark.parametrize(
    ("bucket", "name", "fields"),
    [
        ("excluded_pre_2024_07_23", "pre-2024-07-23 long-term", _PRE_CUTOVER),
        ("excluded_indian_source", "Indian-source", _INDIAN_SOURCE),
        ("excluded_out_of_fy", "out-of-FY", _OUT_OF_FY),
    ],
)
def test_strict_refuses_a_lot_neither_schedule_emits(
    bucket: str, name: str, fields: dict[str, object]
) -> None:
    lots = [make_closed_lot(**_IN_FY_LONG_TERM), make_closed_lot(**fields)]

    result = _compute_fy_2024_25(lots, strict=False)
    assert len(result.ltcg_lines) == 1
    assert len(getattr(result, bucket)) == 1

    with pytest.raises(ScheduleCgDroppedLotError) as exc:
        _compute_fy_2024_25(lots, strict=True)
    assert str(exc.value) == (
        "Schedule CG LTCG: closed lots would get no line in either schedule "
        f"({name}: 1); refusing rather than silently omitting them"
    )


def test_strict_message_counts_each_bucket_without_lot_details() -> None:
    lots = [
        make_closed_lot(**_PRE_CUTOVER),
        make_closed_lot(**_INDIAN_SOURCE),
        *[make_closed_lot(**_OUT_OF_FY)] * 2,
    ]

    with pytest.raises(ScheduleCgDroppedLotError) as exc:
        _compute_fy_2024_25(lots, strict=True)

    assert str(exc.value) == (
        "Schedule CG LTCG: closed lots would get no line in either schedule "
        "(pre-2024-07-23 long-term: 1, Indian-source: 1, out-of-FY: 2); refusing rather "
        "than silently omitting them"
    )


def test_strict_keeps_a_short_term_lot_for_the_stcg_schedule() -> None:
    lot = make_closed_lot(open_date_time=date(2024, 6, 1), close_date_time=date(2024, 12, 10))

    result = _compute_fy_2024_25([lot], strict=True)

    assert [row.closed_lot for row in result.excluded_stcg] == [lot]
