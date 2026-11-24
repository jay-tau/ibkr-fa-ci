"""Tests for compute/espp_capital_gains.py — Schedule CG for sold ESPP lots.

The closed lots and confirmations are hand-built synthetic rows.

Refusals are pinned exactly. They name lots by their 1-based closed-lots data
row and confirmations by purchase-date rank, and the input values the old
messages printed are checked absent, case-insensitively (#1042).
"""

from __future__ import annotations

import re
from dataclasses import replace
from datetime import date, timedelta
from decimal import Decimal

import pytest

from ibkr_fa.compute.espp_capital_gains import (
    EsppCapitalGainsError,
    _to_closed_lot,
    compute_espp_capital_gains,
)
from ibkr_fa.compute.espp_positions import espp_discount_mismatch
from ibkr_fa.compute.schedule_cg import ScheduleCgDroppedLotError
from ibkr_fa.config import NetBenefitsConfig
from ibkr_fa.exceptions import InvariantError
from ibkr_fa.parser.netbenefits.confirmation_pdf import EspPurchase
from tests._compute_builders import espp_closed_lot as _closed
from tests._compute_builders import espp_config as _cfg
from tests._compute_builders import espp_purchase as _purchase
from tests._fx_stubs import AuditedFxStub


def _cfg_participant(participant_no: str) -> NetBenefitsConfig:
    return replace(_cfg(), participant_no=participant_no)


def _june_2023_purchase() -> EspPurchase:
    """A confirmation dated before the default one, so that one ranks second."""
    return _purchase(
        purchase_date=date(2023, 6, 1),
        offering_period_start=date(2023, 3, 1),
        offering_period_end=date(2023, 6, 1),
    )


def _assert_refusal(
    error: BaseException,
    message: str,
    *,
    absent: tuple[str, ...] = (),
    absent_tokens: tuple[str, ...] = (),
) -> None:
    """``error`` reads exactly ``message`` and prints none of ``absent``, nor
    any of ``absent_tokens`` as a whole word, case-insensitively (#1042)."""
    rendered = str(error)
    assert rendered == message
    for value in absent:
        assert value.casefold() not in rendered.casefold(), value
    for token in absent_tokens:
        assert re.search(rf"(?i)\b{re.escape(token)}\b", rendered) is None, token


_FY_26_27 = (date(2026, 4, 1), date(2027, 3, 31))


def _run(closed, purchases, fy=_FY_26_27, fx=Decimal("85"), cfg=None):
    start, end = fy
    return compute_espp_capital_gains(
        closed,
        purchases,
        cfg=_cfg() if cfg is None else cfg,
        financial_year_start=start,
        financial_year_end=end,
        fx_policy=AuditedFxStub(fx),
    )


def test_gain_uses_fmv_basis_not_contribution() -> None:
    result = _run([_closed()], [_purchase()])
    (line,) = result.stcg.stcg_lines
    # §49(2AA): USD FMV stays audited, but the statutory basis is the
    # historical gross INR FMV taken into account under section 17(2)(vi).
    assert line.cost_basis_usd == Decimal("300")
    assert line.proceeds_usd == Decimal("400")
    assert line.cost_basis_inr == Decimal("24000")
    assert line.proceeds_inr == Decimal("400") * 85
    assert line.realized_gain_inr == Decimal("10000")
    assert "Employer perquisite worksheet" in line.cost_basis_inr_source


def test_closed_lot_has_no_security_class_and_keeps_config_exchange() -> None:
    cfg = _cfg()
    cfg = replace(
        cfg,
        security=replace(
            cfg.security, instrument=replace(cfg.security.instrument, listing_exchange="NYSE")
        ),
    )
    lot = _to_closed_lot(
        _closed(), _purchase(), cfg, row=1, label="confirmation 1 (by purchase date)"
    )
    assert lot.sub_category is None
    assert lot.listing_exchange == "NYSE"


def test_partial_sale_prorates_fmv_per_share() -> None:
    # Sell 1 of the 2 purchased shares: basis = fmv/share (150) x 1.
    result = _run(
        [
            _closed(
                quantity=Decimal("1"), proceeds_usd=Decimal("200"), contribution_usd=Decimal("135")
            )
        ],
        [_purchase()],
    )
    (line,) = result.stcg.stcg_lines
    assert line.cost_basis_usd == Decimal("150")
    assert line.cost_basis_inr == Decimal("12000")
    assert not result.ltcg.ltcg_lines


def test_missing_historical_inr_fmv_refuses() -> None:
    cfg = _cfg()
    cfg_without_fmv = replace(cfg, cg_fmv_values=())
    with pytest.raises(EsppCapitalGainsError) as caught:
        _run([_closed()], [_purchase()], cfg=cfg_without_fmv)
    # #1042: names the confirmation, not its purchase date.
    _assert_refusal(
        caught.value,
        "no [[netbenefits.cg_fmv_values]] entry for the purchase date of confirmation 1 (by "
        "purchase date); section 49(2AA) cost must use the historical gross INR FMV taken into "
        "account under section 17(2)(vi), and ibkr-fa will not substitute a proxy FX conversion",
        absent=("2026-01-15",),
    )


def test_24_month_boundary_is_stcg_ignoring_fidelity_long() -> None:
    # Acquired 2026-01-15, sold exactly +24 months = 2028-01-15 -> STCG, even
    # though Fidelity marks it "Long". FY 2027-28 contains the sale.
    result = _run(
        [
            _closed(
                sold_or_transferred_date=date(2028, 1, 15),
                fidelity_term="Long",
            )
        ],
        [_purchase()],
        fy=(date(2027, 4, 1), date(2028, 3, 31)),
    )
    assert len(result.stcg.stcg_lines) == 1
    assert not result.ltcg.ltcg_lines


def test_one_day_past_24_months_is_ltcg_ignoring_fidelity_short() -> None:
    # +24 months + 1 day -> LTCG, even though Fidelity marks it "Short".
    result = _run(
        [
            _closed(
                sold_or_transferred_date=date(2028, 1, 16),
                fidelity_term="Short",
            )
        ],
        [_purchase()],
        fy=(date(2027, 4, 1), date(2028, 3, 31)),
    )
    assert len(result.ltcg.ltcg_lines) == 1
    assert not result.stcg.stcg_lines


_NO_CONFIRMATION = (
    "no purchase confirmation covers this lot — FMV-at-purchase is the §49(2AA) cost of "
    "acquisition and cannot be derived from the closed-lots CSV"
)


def test_missing_fmv_evidence_refuses() -> None:
    # Closed lot acquired on a date with no matching purchase confirmation.
    with pytest.raises(EsppCapitalGainsError) as caught:
        _run([_closed(acquire_date=date(2025, 7, 1))], [_purchase()])
    # #1042: names the closed-lots row, not its acquisition date.
    _assert_refusal(
        caught.value, f"closed-lots data row 1: {_NO_CONFIRMATION}", absent=("2025-07-01",)
    )


def test_missing_fmv_evidence_names_the_third_closed_lots_row() -> None:
    # Row 1 is an unmatched out-of-FY disposal (skipped) and row 2 matches.
    out_of_fy = _closed(acquire_date=date(2030, 1, 1), sold_or_transferred_date=date(2030, 6, 1))
    unmatched = _closed(acquire_date=date(2025, 7, 1))
    with pytest.raises(EsppCapitalGainsError) as caught:
        _run([out_of_fy, _closed(), unmatched], [_purchase()])
    _assert_refusal(
        caught.value, f"closed-lots data row 3: {_NO_CONFIRMATION}", absent=("2025-07-01",)
    )


def test_ambiguous_purchase_dates_refuse() -> None:
    with pytest.raises(EsppCapitalGainsError) as caught:
        _run([_closed()], [_purchase(), _purchase(shares=Decimal("3"))])
    # #1042: the shared purchase date used to be printed.
    _assert_refusal(
        caught.value,
        "two purchase confirmations share a purchase date; the FMV cost basis is ambiguous",
        absent=("2026-01-15",),
    )


def test_oversell_across_two_rows_refuses() -> None:
    # Two disposals of 1.5 each from a 2-share purchase = 3 > 2.
    lot = _closed(
        quantity=Decimal("1.5"), proceeds_usd=Decimal("300"), contribution_usd=Decimal("202.5")
    )
    with pytest.raises(EsppCapitalGainsError) as caught:
        _run([lot, lot], [_purchase()])
    # #1042: names the rows and the confirmation, not the total disposed (3.0),
    # the shares purchased or the acquisition date.
    _assert_refusal(
        caught.value,
        "the disposals in closed-lots data rows 1 and 2 exceed the shares confirmation 1 (by "
        "purchase date) purchased",
        absent=("3.0", "2026-01-15"),
    )


def test_oversell_names_only_that_purchases_rows_and_its_confirmation() -> None:
    """Row 2 disposes of another purchase; the later purchase is listed first
    but ranks second by purchase date (#1042)."""
    lot = _closed(
        quantity=Decimal("1.5"), proceeds_usd=Decimal("300"), contribution_usd=Decimal("202.5")
    )
    other = _closed(acquire_date=date(2023, 6, 1), sold_or_transferred_date=date(2024, 6, 1))
    with pytest.raises(EsppCapitalGainsError) as caught:
        _run([lot, other, lot], [_purchase(), _june_2023_purchase()])
    _assert_refusal(
        caught.value,
        "the disposals in closed-lots data rows 1 and 3 exceed the shares confirmation 2 (by "
        "purchase date) purchased",
        absent=("3.0", "2026-01-15"),
    )


# The parser refuses negative proceeds; compute still refuses a direct caller's.
@pytest.mark.parametrize(
    ("proceeds", "absent_proceeds"),
    [
        pytest.param("0", (), id="0"),
        pytest.param("-1", (), id="-1"),
        pytest.param("-1234567.89", ("1234567.89",), id="-1234567.89"),
    ],
)
def test_non_positive_proceeds_refuses(proceeds: str, absent_proceeds: tuple[str, ...]) -> None:
    # "Date sold or transferred" doesn't say which, so neither is assumed.
    with pytest.raises(EsppCapitalGainsError) as excinfo:
        _run([_closed(proceeds_usd=Decimal(proceeds))], [_purchase()])
    # #1042: names the row, not its dates or proceeds.
    _assert_refusal(
        excinfo.value,
        "closed-lots data row 1: the Proceeds are not positive; the closed-lots export doesn't "
        "show whether it was a sale or a transfer, so its Schedule CG treatment is unknown",
        absent=("2026-01-15", "2026-06-15", *absent_proceeds),
    )


def test_participant_mismatch_refuses() -> None:
    with pytest.raises(EsppCapitalGainsError) as caught:
        _run(
            [_closed()],
            [_purchase(participant_no="I12345678")],
            cfg=_cfg_participant("I00000001"),
        )
    # #1042: neither participant number is printed.
    _assert_refusal(
        caught.value,
        "confirmation 1 (by purchase date): the PARTICIPANT NO. differs from "
        "netbenefits.participant_no in user.toml",
        absent=("I12345678", "I00000001"),
    )


def test_participant_mismatch_names_the_second_confirmation() -> None:
    # The later purchase is listed first but ranks second by purchase date.
    with pytest.raises(EsppCapitalGainsError) as caught:
        _run(
            [_closed()],
            [_purchase(participant_no="I12345678"), _june_2023_purchase()],
            cfg=_cfg_participant("I00000001"),
        )
    _assert_refusal(
        caught.value,
        "confirmation 2 (by purchase date): the PARTICIPANT NO. differs from "
        "netbenefits.participant_no in user.toml",
        absent=("I12345678", "I00000001"),
    )


def test_symbol_mismatch_refuses() -> None:
    with pytest.raises(EsppCapitalGainsError) as caught:
        _run([_closed()], [_purchase(symbol="ANN")])
    # #1042: neither symbol is printed; "ANN" is checked as a whole token.
    _assert_refusal(
        caught.value,
        "confirmation 1 (by purchase date): the SYMBOL differs from "
        "netbenefits.security.symbol in user.toml",
        absent=("XESP",),
        absent_tokens=("ANN",),
    )


def test_cusip_mismatch_refuses() -> None:
    with pytest.raises(EsppCapitalGainsError) as caught:
        _run([_closed()], [_purchase(cusip="99993A108")])
    # #1042: neither CUSIP is printed.
    _assert_refusal(
        caught.value,
        "confirmation 1 (by purchase date): the CUSIP differs from netbenefits.security.cusip "
        "in user.toml",
        absent=("99993A108", "99999E104"),
    )


def test_contribution_mismatch_refuses() -> None:
    # Closed-lot cost basis can't be reconciled with the confirmation price.
    with pytest.raises(EsppCapitalGainsError) as caught:
        _run([_closed(contribution_usd=Decimal("1234567.89"))], [_purchase()])
    # #1042: neither the cost basis, the price nor the acquisition date is printed.
    _assert_refusal(
        caught.value,
        "closed-lots data row 1: the Cost basis doesn't reconcile with the Quantity at the "
        "purchase price on confirmation 1 (by purchase date)",
        absent=("1234567.89", "135", "2026-01-15"),
    )


def test_fmv_discount_mismatch_refuses() -> None:
    # FMV doesn't match price / (1 - discount): the §49(2AA) basis is unreliable.
    # price 135 ties to contribution, but fmv/share 1234567.89 x 0.9 != 135.
    purchase = _purchase(fmv_total_usd=Decimal("2469135.78"))
    mismatch = espp_discount_mismatch(purchase, Decimal("10"))
    assert mismatch is not None
    with pytest.raises(EsppCapitalGainsError) as caught:
        _run([_closed()], [purchase])
    # #1042: the price, the FMV per share, the discounted price, the discount %
    # and the acquisition date used to be printed.
    _assert_refusal(
        caught.value,
        "confirmation 1 (by purchase date): the purchase price is not the FMV per share less "
        "the netbenefits.espp_discount_pct discount, within the rounding tolerance, so the FMV "
        "cost basis of closed-lots data row 1 is unreliable",
        absent=(
            "1234567.89",
            str(mismatch.expected_price_usd),
            "135",
            "10%",
            "2026-01-15",
        ),
    )


@pytest.mark.parametrize("proceeds", ["200", "0"])
def test_out_of_fy_sale_is_skipped_without_demanding_fmv(proceeds: str) -> None:
    # A disposal outside the FY must not be computed, refused or demand its FMV.
    out_of_fy = _closed(
        acquire_date=date(2030, 1, 1),
        sold_or_transferred_date=date(2030, 6, 1),  # not in FY 2026-27
        quantity=Decimal("1"),
        proceeds_usd=Decimal(proceeds),
        contribution_usd=Decimal("100"),
    )
    result = _run([out_of_fy], [_purchase()])  # no purchase for 2030-01-01
    assert not result.stcg.stcg_lines
    assert not result.ltcg.ltcg_lines


def test_pre_cutover_ltcg_refuses_rather_than_silently_dropping() -> None:
    # Long-term lot disposed before 2024-07-23 -> old indexation regime; the
    # engine emits no line for it, so we must refuse, not silently omit it.
    purchase = _purchase(
        purchase_date=date(2020, 1, 1),
        offering_period_start=date(2019, 10, 1),
        offering_period_end=date(2020, 1, 1),
    )
    lot = _closed(
        acquire_date=date(2020, 1, 1),
        sold_or_transferred_date=date(2024, 6, 1),  # in FY 2024-25, < 2024-07-23
    )
    with pytest.raises(ScheduleCgDroppedLotError, match="2024-07-23"):
        _run([lot], [purchase], fy=(date(2024, 4, 1), date(2025, 3, 31)))


# ---- filed totals and the CBDT 14-digit bound (#830) ------------------------


@pytest.mark.parametrize(
    ("purchase_date", "schedule"), [(date(2026, 1, 15), "STCG"), (date(2023, 6, 1), "LTCG")]
)
def test_section_49_2aa_costs_past_the_cbdt_bound_refuse(
    purchase_date: date, schedule: str
) -> None:
    """Each half-lot's section 49(2AA) cost of ₹60,000,000,000,000 is in range;
    the filed cost of acquisition, their sum, is not."""
    cfg = _cfg()
    cfg = replace(
        cfg,
        cg_fmv_values=tuple(
            replace(value, section17_gross_fmv_total_inr=Decimal("120000000000000"))
            for value in cfg.cg_fmv_values
        ),
    )
    purchase = _purchase(
        purchase_date=purchase_date,
        offering_period_start=purchase_date - timedelta(days=90),
        offering_period_end=purchase_date,
    )
    half = _closed(
        acquire_date=purchase_date,
        quantity=Decimal("1"),
        proceeds_usd=Decimal("200"),
        contribution_usd=Decimal("135"),
    )

    with pytest.raises(InvariantError) as excinfo:
        _run([half, half], [purchase], cfg=cfg)

    assert excinfo.value.what.startswith(f"Schedule CG {schedule} cost of acquisition must round")


def test_generic_cost_past_the_cbdt_bound_is_never_filed() -> None:
    """The USD FMV at the sale rate would be ₹255,000,000,000,000, but only the
    configured section 49(2AA) cost is filed."""
    purchase = _purchase(
        purchase_price_per_share_usd=Decimal("1350000000000"),
        fmv_total_usd=Decimal("3000000000000"),
        contribution_usd=Decimal("2700000000000"),
        perquisite_usd=Decimal("300000000000"),
    )

    result = _run([_closed(contribution_usd=Decimal("2700000000000"))], [purchase])

    (line,) = result.stcg.stcg_lines
    assert line.cost_basis_usd * 85 == Decimal("255000000000000")
    assert line.cost_basis_inr == Decimal("24000")
