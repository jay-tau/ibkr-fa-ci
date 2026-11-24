"""Unit tests for compute_schedule_cg_stcg_lines."""

from __future__ import annotations

from dataclasses import replace
from datetime import date
from decimal import Decimal

import pytest

from ibkr_fa.compute.lot_matching import BrokerBasisError
from ibkr_fa.compute.schedule_cg import (
    _RULE_115_COST_BASIS_SOURCE,
    ScheduleCgDroppedLotError,
    ScheduleCgStcgComputeResult,
    compute_schedule_cg_stcg_lines,
)
from ibkr_fa.exceptions import InvariantError
from ibkr_fa.models import BasisProvenance, ClosedLot, StatutoryInrCostBasis
from ibkr_fa.models.schedule_cg_row import STCG_HOLDING_THRESHOLD_MONTHS, _add_calendar_months
from ibkr_fa.types import CountryCode, Isin, Ticker
from tests._compute_builders import stcg_closed_lot as make_closed_lot
from tests._fx_stubs import AuditedFxStub
from tests._lot_matching_inputs import BROKER_BASIS_MESSAGE

_FY_START = date(2025, 4, 1)
_FY_END = date(2026, 3, 31)


def test_empty_input_returns_empty_result() -> None:
    result = compute_schedule_cg_stcg_lines(
        [],
        financial_year_start=_FY_START,
        financial_year_end=_FY_END,
        fx_policy=AuditedFxStub(),
    )
    assert result.stcg_lines == []
    assert result.excluded_ltcg_deferred == []
    assert result.excluded_indian_source == []
    assert result.excluded_out_of_fy == []


def test_in_fy_short_term_disposal_produces_one_stcg_line() -> None:
    """T5 Codex P1 fix: Rule 115(1), Explanation (2)(f), single conversion at
    transfer date — both cost_basis_inr and proceeds_inr use the SAME
    close-date FX rate, not separate open/close rates."""
    lot = make_closed_lot()
    fx = AuditedFxStub(
        {
            date(2025, 6, 15): Decimal("83"),  # open date, NOT used for CG
            date(2025, 12, 10): Decimal("84"),  # transfer date — the only FX used
        }
    )
    result = compute_schedule_cg_stcg_lines(
        [lot],
        financial_year_start=_FY_START,
        financial_year_end=_FY_END,
        fx_policy=fx,
    )
    assert len(result.stcg_lines) == 1
    line = result.stcg_lines[0]
    assert line.symbol == Ticker("XQAQ")
    assert line.cost_basis_usd == Decimal("1500")
    # Single conversion at transfer-date FX (84), not open-date FX.
    assert line.cost_basis_inr == Decimal("1500") * Decimal("84")
    assert line.proceeds_inr == Decimal("1800") * Decimal("84")
    assert line.realized_gain_inr == line.proceeds_inr - line.cost_basis_inr
    # Equivalently: (proceeds_usd - cost_basis_usd) * close FX.
    assert line.realized_gain_inr == (Decimal("1800") - Decimal("1500")) * Decimal("84")
    assert line.fx_specified_date == date(2025, 11, 30)
    assert line.fx_rate_inr_per_usd == Decimal("84")
    assert result.excluded_ltcg_deferred == []
    assert result.excluded_indian_source == []
    assert result.excluded_out_of_fy == []


def test_january_close_uses_december_31_specified_date() -> None:
    lot = make_closed_lot(
        open_date_time=date(2025, 10, 1),
        close_date_time=date(2026, 1, 15),
    )
    result = compute_schedule_cg_stcg_lines(
        [lot],
        financial_year_start=_FY_START,
        financial_year_end=_FY_END,
        fx_policy=AuditedFxStub(),
    )
    line = result.stcg_lines[0]
    # The specified date is the preceding-month-end of the CLOSE date
    # (the transfer event), per Rule 115(1), Explanation (2)(f). No open-event date.
    assert line.fx_specified_date == date(2025, 12, 31)


def test_out_of_fy_close_routes_to_excluded_bucket() -> None:
    before = make_closed_lot(
        open_date_time=date(2024, 6, 15),
        close_date_time=date(2025, 3, 15),
    )
    after = make_closed_lot(
        open_date_time=date(2025, 11, 1),
        close_date_time=date(2026, 4, 15),
    )
    result = compute_schedule_cg_stcg_lines(
        [before, after],
        financial_year_start=_FY_START,
        financial_year_end=_FY_END,
        fx_policy=AuditedFxStub(),
    )
    assert result.stcg_lines == []
    assert result.excluded_out_of_fy == [before, after]


def test_indian_source_isin_routes_to_excluded_bucket() -> None:
    lot = make_closed_lot(
        symbol=Ticker("XQCD"),
        isin=Isin("IN99970W1077"),
        issuer_country_code=CountryCode("IN"),
        description="SAMPLE ISSUER CD LTD",
    )
    result = compute_schedule_cg_stcg_lines(
        [lot],
        financial_year_start=_FY_START,
        financial_year_end=_FY_END,
        fx_policy=AuditedFxStub(),
    )
    assert result.stcg_lines == []
    assert result.excluded_indian_source == [lot]


def test_long_term_disposal_routes_to_ltcg_deferred_bucket() -> None:
    """Holding period > 24 months → LTCG (see schedule-cg-ltcg)."""
    lot = make_closed_lot(
        open_date_time=date(2022, 1, 1),
        close_date_time=date(2025, 12, 1),
    )
    result = compute_schedule_cg_stcg_lines(
        [lot],
        financial_year_start=_FY_START,
        financial_year_end=_FY_END,
        fx_policy=AuditedFxStub(),
    )
    assert result.stcg_lines == []
    assert len(result.excluded_ltcg_deferred) == 1
    deferred = result.excluded_ltcg_deferred[0]
    assert deferred.closed_lot is lot
    # Holding period in days exceeds 24 calendar months for this fixture.
    cutoff = _add_calendar_months(lot.open_date_time, STCG_HOLDING_THRESHOLD_MONTHS)
    assert lot.close_date_time > cutoff


def test_exact_calendar_month_threshold_is_stcg_not_ltcg() -> None:
    """T3 review: exactly 24 calendar months is STCG (boundary inclusive).

    Acquired 2024-04-01, disposed 2026-04-01 → 24 calendar months
    exactly. The OLD day-count threshold (720 days) would have
    misclassified this as LTCG (729 days > 720); the calendar-month
    check correctly routes it to STCG.
    """
    open_d = date(2024, 4, 1)
    close_d = date(2026, 4, 1)
    lot = make_closed_lot(open_date_time=open_d, close_date_time=close_d)
    fy_start = date(2026, 4, 1)
    fy_end = date(2027, 3, 31)
    result = compute_schedule_cg_stcg_lines(
        [lot],
        financial_year_start=fy_start,
        financial_year_end=fy_end,
        fx_policy=AuditedFxStub(),
    )
    assert len(result.stcg_lines) == 1
    assert result.excluded_ltcg_deferred == []


def test_one_day_past_24_calendar_months_is_ltcg() -> None:
    """T3 review: one day past 24 calendar months is LTCG."""
    open_d = date(2024, 4, 1)
    close_d = date(2026, 4, 2)
    lot = make_closed_lot(open_date_time=open_d, close_date_time=close_d)
    fy_start = date(2026, 4, 1)
    fy_end = date(2027, 3, 31)
    result = compute_schedule_cg_stcg_lines(
        [lot],
        financial_year_start=fy_start,
        financial_year_end=fy_end,
        fx_policy=AuditedFxStub(),
    )
    assert result.stcg_lines == []
    assert len(result.excluded_ltcg_deferred) == 1


def test_calendar_month_threshold_handles_month_end_clamping() -> None:
    """Acquired Jan 31 + 24 months → Jan 31 of year+2. A close one day
    later is LTCG."""
    open_d = date(2024, 1, 31)
    close_d = date(2026, 1, 31)
    lot = make_closed_lot(open_date_time=open_d, close_date_time=close_d)
    fy_start = date(2025, 4, 1)
    fy_end = date(2026, 3, 31)
    result = compute_schedule_cg_stcg_lines(
        [lot],
        financial_year_start=fy_start,
        financial_year_end=fy_end,
        fx_policy=AuditedFxStub(),
    )
    # close on exactly the 24-month-cutoff date = STCG
    assert len(result.stcg_lines) == 1


def test_non_usd_lots_fail_fast_with_invariant_error() -> None:
    """Defense-in-depth — parser already gates this; a bypass must fail loud."""
    lot = make_closed_lot()
    # Bypass frozen constraint via object.__setattr__ to simulate a
    # parser bug that produced a non-USD ClosedLot.
    object.__setattr__(lot, "currency_primary", "EUR")
    with pytest.raises(InvariantError):
        compute_schedule_cg_stcg_lines(
            [lot],
            financial_year_start=_FY_START,
            financial_year_end=_FY_END,
            fx_policy=AuditedFxStub(),
        )


def test_generator_input_materialised_once() -> None:
    """A generator must not be exhausted by the non-USD precondition pass."""
    lots = [make_closed_lot(), make_closed_lot(symbol=Ticker("XQCA"), isin=Isin("US99972B1017"))]

    def gen():
        yield from lots

    result = compute_schedule_cg_stcg_lines(
        gen(),
        financial_year_start=_FY_START,
        financial_year_end=_FY_END,
        fx_policy=AuditedFxStub(),
    )
    assert len(result.stcg_lines) == 2


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
    result = compute_schedule_cg_stcg_lines(
        lots,
        financial_year_start=_FY_START,
        financial_year_end=_FY_END,
        fx_policy=AuditedFxStub(),
    )
    syms = [str(line.symbol) for line in result.stcg_lines]
    # The earliest close leads although its symbol sorts after XQAB; the
    # same-day pair then follows symbol order, not input order.
    assert syms == ["XQAQ", "XQAB", "XQCA"]


def test_fx_policy_invoked_only_at_transfer_date() -> None:
    """T5 Codex P1 fix: Rule 115(1), Explanation (2)(f), single conversion at the transfer
    (close) date only. The open-date FX is NOT looked up — both basis
    and proceeds use the close-date TTBR."""
    lot = make_closed_lot(
        open_date_time=date(2025, 5, 1),
        close_date_time=date(2025, 11, 1),
    )
    # If the buggy double-conversion were still in effect, an
    # acquisition-date rate would be looked up. Set distinct rates
    # so a regression would change the asserted INR values.
    fx = AuditedFxStub(
        {
            date(2025, 5, 1): Decimal("80.00"),  # would-be open-event; NOT used
            date(2025, 11, 1): Decimal("90.00"),  # transfer event — the only FX
        }
    )
    result = compute_schedule_cg_stcg_lines(
        [lot],
        financial_year_start=_FY_START,
        financial_year_end=_FY_END,
        fx_policy=fx,
    )
    line = result.stcg_lines[0]
    assert line.fx_rate_inr_per_usd == Decimal("90.00")
    # 1500 USD basis * 90 = 135000 INR; 1800 USD proceeds * 90 = 162000 INR.
    assert line.cost_basis_inr == Decimal("135000.00")
    assert line.proceeds_inr == Decimal("162000.00")
    # Realized gain = (1800 - 1500) * 90 = 27000 INR.
    assert line.realized_gain_inr == Decimal("27000.00")


def test_loss_case_preserves_negative_realized_gain_inr() -> None:
    lot = make_closed_lot(
        cost_basis_money=Decimal("2000"),
        proceeds_money=Decimal("1500"),
    )
    fx = AuditedFxStub(Decimal("85"))
    result = compute_schedule_cg_stcg_lines(
        [lot],
        financial_year_start=_FY_START,
        financial_year_end=_FY_END,
        fx_policy=fx,
    )
    line = result.stcg_lines[0]
    assert line.realized_gain_inr < 0


def test_compute_result_is_frozen_dataclass() -> None:
    result = ScheduleCgStcgComputeResult(
        stcg_lines=[],
        excluded_ltcg_deferred=[],
        excluded_indian_source=[],
        excluded_out_of_fy=[],
    )
    with pytest.raises(AttributeError):
        result.stcg_lines = []  # ty: ignore[invalid-assignment]


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
        compute_schedule_cg_stcg_lines(
            [lot],
            financial_year_start=_FY_START,
            financial_year_end=_FY_END,
            fx_policy=AuditedFxStub(),
        )
    assert str(exc.value) == BROKER_BASIS_MESSAGE
    # The old text printed the holding-period date; none of the lot's
    # dates, symbol or ISIN are printed now (#1047).
    for value in ("2023-04-12", "2023-05-18", "2023-07-06", "XQAQ", "US99992A1097"):
        assert value.lower() not in str(exc.value).lower()


# ---- statutory INR cost override (#633) -------------------------------------


def _compute_at_85(lots: list[ClosedLot], *, strict: bool = False) -> ScheduleCgStcgComputeResult:
    return compute_schedule_cg_stcg_lines(
        lots,
        financial_year_start=_FY_START,
        financial_year_end=_FY_END,
        fx_policy=AuditedFxStub(Decimal("85")),
        strict=strict,
    )


def test_lot_without_override_keeps_the_rule_115_cost() -> None:
    (line,) = _compute_at_85([make_closed_lot()]).stcg_lines
    assert line.cost_basis_inr == Decimal("1500") * Decimal("85")
    assert line.cost_basis_inr_source == _RULE_115_COST_BASIS_SOURCE
    assert line.realized_gain_inr == (Decimal("1800") - Decimal("1500")) * Decimal("85")


@pytest.mark.parametrize("amount_inr", [Decimal("120000"), Decimal("160000")], ids=["gain", "loss"])
def test_override_replaces_only_the_inr_cost(amount_inr: Decimal) -> None:
    override = StatutoryInrCostBasis(amount_inr=amount_inr, source="employer worksheet")
    statutory = make_closed_lot(
        basis_provenance=BasisProvenance.S49_2AA, cost_basis_inr_override=override
    )
    long_term = replace(statutory, open_date_time=date(2023, 6, 15))
    (base,) = _compute_at_85([make_closed_lot()]).stcg_lines

    result = _compute_at_85([statutory, long_term])

    (line,) = result.stcg_lines
    assert line.cost_basis_inr == amount_inr
    assert line.cost_basis_inr_source == "employer worksheet"
    # Proceeds 1800 USD x 85 = 153000 INR: a gain over 120000, a loss under 160000.
    assert line.realized_gain_inr == Decimal("153000") - amount_inr
    assert (
        replace(
            line,
            cost_basis_inr=base.cost_basis_inr,
            cost_basis_inr_source=base.cost_basis_inr_source,
            realized_gain_inr=base.realized_gain_inr,
        )
        == base
    )
    assert [row.closed_lot for row in result.excluded_ltcg_deferred] == [long_term]


# ---- strict mode (#633) -----------------------------------------------------

_INDIAN_SOURCE = {
    "symbol": Ticker("XQCD"),
    "isin": Isin("IN99970W1077"),
    "issuer_country_code": CountryCode("IN"),
}
_OUT_OF_FY = {"open_date_time": date(2024, 6, 15), "close_date_time": date(2025, 3, 15)}


@pytest.mark.parametrize(
    ("bucket", "name", "fields"),
    [
        ("excluded_indian_source", "Indian-source", _INDIAN_SOURCE),
        ("excluded_out_of_fy", "out-of-FY", _OUT_OF_FY),
    ],
)
def test_strict_refuses_a_lot_neither_schedule_emits(
    bucket: str, name: str, fields: dict[str, object]
) -> None:
    lots = [make_closed_lot(), make_closed_lot(**fields)]

    result = _compute_at_85(lots, strict=False)
    assert len(result.stcg_lines) == 1
    assert len(getattr(result, bucket)) == 1

    with pytest.raises(ScheduleCgDroppedLotError) as exc:
        _compute_at_85(lots, strict=True)
    assert str(exc.value) == (
        "Schedule CG STCG: closed lots would get no line in either schedule "
        f"({name}: 1); refusing rather than silently omitting them"
    )


def test_strict_message_counts_each_bucket_without_lot_details() -> None:
    lots = [make_closed_lot(**_INDIAN_SOURCE), *[make_closed_lot(**_OUT_OF_FY)] * 2]

    with pytest.raises(ScheduleCgDroppedLotError) as exc:
        _compute_at_85(lots, strict=True)

    assert str(exc.value) == (
        "Schedule CG STCG: closed lots would get no line in either schedule "
        "(Indian-source: 1, out-of-FY: 2); refusing rather than silently omitting them"
    )


def test_strict_keeps_a_long_term_lot_for_the_ltcg_schedule() -> None:
    lot = make_closed_lot(open_date_time=date(2022, 1, 1), close_date_time=date(2025, 12, 1))

    result = _compute_at_85([lot], strict=True)

    assert [row.closed_lot for row in result.excluded_ltcg_deferred] == [lot]
