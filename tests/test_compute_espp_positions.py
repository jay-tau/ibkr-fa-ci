"""Tests for compute/espp_positions.py — the Dec-31 NetBenefits ESPP positions.

Hand-built lots and confirmations (no PDFs, no pipeline). The numbers mirror
the pipeline fixtures: a Dec-31-2025 purchase of 3.482 shares at 192.12
(contribution 668.96, FMV 743.30, 10% discount), priced by a 213.47 override.

Refusals are pinned exactly. They name lots by their 1-based open- or
closed-lots data row and confirmations by purchase-date rank, and the input
values the old messages printed are checked absent, case-insensitively (#1042).
"""

from __future__ import annotations

import re
from dataclasses import replace
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

import pytest

from ibkr_fa.compute.dec31_close import CloseSource, ClosingPriceError, ResolvedClose
from ibkr_fa.compute.espp_positions import (
    EsppDec31Lot,
    EsppDec31Positions,
    NetBenefitsFaError,
    confirmation_label,
    espp_discount_mismatch,
    lots_data_rows,
    reconstruct_espp_dec31_lots,
    resolve_espp_dec31_positions,
)
from ibkr_fa.config import NetBenefitsConfig, NetBenefitsPriceOverride, load_user_config
from ibkr_fa.exceptions import InvariantError
from ibkr_fa.parser.netbenefits.confirmation_pdf import EspPurchase
from ibkr_fa.parser.netbenefits.lots_csv import EspClosedLot, EspLot
from ibkr_fa.prices.series import PriceSeries
from ibkr_fa.types import PeakBasis, Ticker
from tests._exact_refusals import INEXACT_SUM
from tests._netbenefits import esp_open_lot as _open
from tests._netbenefits import esp_purchase as _purchase
from tests._netbenefits_fa import BASE_CONFIG as _CONFIG
from tests._netbenefits_fa import (
    NB_NO_CLOSE_FETCHED_HIGH_REFUSAL,
    NB_NO_CLOSE_NOT_FETCHED_HIGH_REFUSAL,
    NB_NO_CLOSE_REFUSAL,
    NB_OVERRIDE_ANCHOR_ROUTE,
)

_DEC31 = date(2025, 12, 31)
_OCTOBER = date(2025, 10, 15)


@pytest.fixture
def cfg(tmp_path: Path) -> NetBenefitsConfig:
    path = tmp_path / "user.toml"
    path.write_text(_CONFIG, encoding="utf-8")
    netbenefits = load_user_config(path).netbenefits
    assert netbenefits is not None
    return netbenefits


def _october_purchase() -> EspPurchase:
    """A confirmation dated before Dec-31, so the Dec-31 one ranks second."""
    return _purchase(
        purchase_date=_OCTOBER, offering_period_start=_OCTOBER, offering_period_end=_OCTOBER
    )


def _closed(**overrides: object) -> EspClosedLot:
    defaults: dict[str, object] = {
        "acquire_date": _DEC31,
        "sold_or_transferred_date": date(2026, 4, 20),
        "quantity": Decimal("1.0000"),
        "proceeds_usd": Decimal("500.00"),
        "contribution_usd": Decimal("192.12"),
        "fidelity_term": "Short",
    }
    defaults.update(overrides)
    return EspClosedLot(**defaults)  # type: ignore[arg-type]


def _later_open() -> EspLot:
    """An open lot acquired after CY2025, so the CY filter drops it."""
    return _open(acquire_date=date(2026, 3, 31))


def _pre_cy_closed() -> EspClosedLot:
    """A closed lot bought and sold before CY2025, so it is never held in it."""
    return _closed(acquire_date=date(2024, 1, 15), sold_or_transferred_date=date(2024, 6, 3))


def _by_date(purchases: list[EspPurchase]) -> dict[date, EspPurchase]:
    return {purchase.purchase_date: purchase for purchase in purchases}


def _reconstruct(
    open_lots: list[EspLot],
    closed_lots: list[EspClosedLot] | None = None,
    purchases: list[EspPurchase] | None = None,
) -> list[EsppDec31Lot]:
    confirmations = [_purchase()] if purchases is None else purchases
    return reconstruct_espp_dec31_lots(
        open_lots, closed_lots or [], _by_date(confirmations), cy_end=_DEC31
    )


def _resolve(
    cfg: NetBenefitsConfig,
    lots: list[EsppDec31Lot] | None = None,
    purchases: list[EspPurchase] | None = None,
    series: PriceSeries | None = None,
    *,
    peak_basis: PeakBasis = "close",
    prices_fetched: bool = True,
) -> EsppDec31Positions:
    confirmations = [_purchase()] if purchases is None else purchases
    return resolve_espp_dec31_positions(
        [EsppDec31Lot(_open(), open_row=1)] if lots is None else lots,
        _by_date(confirmations),
        cfg=cfg,
        calendar_year=2025,
        series=series,
        peak_basis=peak_basis,
        prices_fetched=prices_fetched,
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


def _intra_cy_refusal(row: int) -> str:
    return (
        f"closed-lots data row {row} overlaps CY2025 but is not a Dec-31-held gap-window lot "
        "(acquired on Dec-31 and sold after it); NetBenefits intra-CY closed-lot Schedule FA "
        "A3 disclosure is not yet supported (see #I13) — refusing rather than emit an A3 that "
        "omits a disclosable holding"
    )


def _split_refusal(row: int, confirmation: int) -> str:
    return (
        f"closed-lots data row {row}: the gap-window lot's per-share cost differs from the "
        f"purchase price on confirmation {confirmation} (by purchase date) by more than the "
        "rounding tolerance; a split/transfer or foreign row between Dec-31 and the sale "
        "can't be reconciled — refusing"
    )


# --- lot and confirmation naming (#1042) --------------------------------------


@pytest.mark.parametrize(
    ("rows", "named"),
    [
        pytest.param((3,), "closed-lots data row 3", id="one-row"),
        pytest.param((2, 5), "closed-lots data rows 2 and 5", id="two-rows"),
        pytest.param((1, 2, 4), "closed-lots data rows 1, 2 and 4", id="three-rows"),
    ],
)
def test_lots_data_rows_names_one_or_more_rows(rows: tuple[int, ...], named: str) -> None:
    assert lots_data_rows("closed-lots", rows) == named
    assert lots_data_rows("open-lots", rows) == named.replace("closed-lots", "open-lots")


def test_confirmation_label_ranks_by_purchase_date_not_mapping_order() -> None:
    first, second, third = date(2025, 3, 31), _OCTOBER, _DEC31
    by_date = {third: "dec", first: "mar", second: "oct"}
    assert [confirmation_label(day, by_date) for day in (first, second, third)] == [
        "confirmation 1 (by purchase date)",
        "confirmation 2 (by purchase date)",
        "confirmation 3 (by purchase date)",
    ]


@pytest.mark.parametrize(
    ("open_row", "closed_rows", "subject"),
    [
        pytest.param(2, (), "open-lots data row 2", id="open-lot"),
        pytest.param(
            2,
            (1,),
            "the position folded from open-lots data row 2 and closed-lots data row 1",
            id="open-lot-and-one-closed-lot",
        ),
        pytest.param(
            2,
            (1, 3),
            "the position folded from open-lots data row 2 and closed-lots data rows 1 and 3",
            id="open-lot-and-two-closed-lots",
        ),
        pytest.param(
            None,
            (4,),
            "the position folded from closed-lots data row 4",
            id="one-closed-lot",
        ),
        pytest.param(
            None,
            (1, 2, 4),
            "the position folded from closed-lots data rows 1, 2 and 4",
            id="three-closed-lots",
        ),
    ],
)
def test_dec31_lot_subject_names_its_lots_data_rows(
    open_row: int | None, closed_rows: tuple[int, ...], subject: str
) -> None:
    assert EsppDec31Lot(_open(), open_row=open_row, closed_rows=closed_rows).subject == subject


def test_dec31_lot_with_no_data_row_is_an_invariant_error() -> None:
    with pytest.raises(InvariantError) as caught:
        EsppDec31Lot(_open(), open_row=None)
    assert str(caught.value) == (
        "InvariantError: an ESPP Dec-31 lot needs an open-lots or a closed-lots data row"
    )


# --- reconstruct_espp_dec31_lots ---------------------------------------------


def test_open_lot_only_is_kept_and_later_lots_dropped() -> None:
    # The kept lot keeps its open-lots data row, 2, after the dropped later lot (#1042).
    assert _reconstruct([_later_open(), _open()]) == [EsppDec31Lot(_open(), open_row=2)]


def test_gap_window_partial_sale_folds_into_the_open_lot() -> None:
    surviving = _open(quantity=Decimal("2.4820"), contribution_usd=Decimal("476.84"))
    lots = _reconstruct([surviving], [_closed()])
    assert lots == [
        EsppDec31Lot(
            _open(quantity=Decimal("3.4820"), contribution_usd=Decimal("668.96")),
            open_row=1,
            closed_rows=(1,),
        )
    ]


def test_gap_window_full_sale_reconstructs_without_an_open_lot() -> None:
    sold = _closed(quantity=Decimal("3.4820"), contribution_usd=Decimal("668.96"))
    lots = _reconstruct([], [sold])
    assert lots == [EsppDec31Lot(_open(), open_row=None, closed_rows=(1,))]


def test_folded_position_keeps_its_lot_rows_in_file_order() -> None:
    """The fold records the open-lots row and every gap-window closed-lots row
    it used, numbered in file order past the rows the CY filter skips (#1042)."""
    surviving = _open(quantity=Decimal("1.4820"), contribution_usd=Decimal("284.72"))
    lots = _reconstruct([_later_open(), surviving], [_closed(), _pre_cy_closed(), _closed()])
    assert lots == [EsppDec31Lot(_open(), open_row=2, closed_rows=(1, 3))]


def test_closed_lot_outside_the_gap_window_refuses() -> None:
    intra_cy = _closed(acquire_date=date(2025, 6, 30), sold_or_transferred_date=date(2025, 9, 30))
    with pytest.raises(NetBenefitsFaError) as caught:
        _reconstruct([_open()], [intra_cy])
    # #1042: neither lot date nor the CY end date is printed.
    _assert_refusal(
        caught.value, _intra_cy_refusal(1), absent=("2025-06-30", "2025-09-30", "2025-12-31")
    )


def test_closed_lot_outside_the_gap_window_names_its_data_row() -> None:
    intra_cy = _closed(acquire_date=date(2025, 6, 30), sold_or_transferred_date=date(2025, 9, 30))
    with pytest.raises(NetBenefitsFaError) as caught:
        _reconstruct([_open()], [_pre_cy_closed(), _closed(), intra_cy])
    _assert_refusal(caught.value, _intra_cy_refusal(3))


def test_duplicate_open_lot_dates_refuse() -> None:
    with pytest.raises(NetBenefitsFaError) as caught:
        _reconstruct([_later_open(), _open(), _later_open(), _open()])
    _assert_refusal(
        caught.value,
        "open-lots data rows 2 and 4 share an acquisition date; the bundle is ambiguous",
    )


def test_gap_window_split_mismatch_refuses() -> None:
    # 192.12 / 2.0 = 96.06 per share: the shape a 2:1 split takes.
    split = _closed(quantity=Decimal("2.0000"))
    with pytest.raises(NetBenefitsFaError) as caught:
        _reconstruct([_open(quantity=Decimal("2.4820"))], [split])
    # #1042: the per-share cost, the price, their gap (96.06), the tolerance
    # (0.106060) and the lot date used to be printed.
    _assert_refusal(
        caught.value,
        _split_refusal(row=1, confirmation=1),
        absent=("96.06", "192.12", "0.10606", "2025-12-31"),
    )


def test_gap_window_split_mismatch_names_its_data_row_and_confirmation() -> None:
    split = _closed(quantity=Decimal("2.0000"))
    with pytest.raises(NetBenefitsFaError) as caught:
        _reconstruct([], [_closed(), _closed(), split], [_purchase(), _october_purchase()])
    _assert_refusal(caught.value, _split_refusal(row=3, confirmation=2))


def test_no_lot_on_or_before_dec31_refuses() -> None:
    with pytest.raises(NetBenefitsFaError) as caught:
        _reconstruct([_later_open()])
    # #1042: the fixed "Dec-31" replaces the date; CY2025 is the caller's calendar year.
    _assert_refusal(
        caught.value,
        "no ESPP lot acquired on or before Dec-31; nothing to report for CY2025",
        absent=("2025-12-31",),
    )


# --- resolve_espp_dec31_positions --------------------------------------------


def test_dec31_lot_resolves_to_a_valued_position(cfg: NetBenefitsConfig) -> None:
    resolved = _resolve(cfg)

    (position,) = resolved.positions
    assert position.purchase == _purchase()
    assert position.close.source is CloseSource.OVERRIDE
    assert position.lot.quantity == Decimal("3.4820")
    assert position.lot.mark_price == Decimal("213.47")
    assert position.lot.cost_basis_money == Decimal("743.30")
    assert position.lot.open_date_time == _DEC31
    assert position.lot.report_date == _DEC31
    assert position.lot.description == "EXAMPLE CORP"
    assert resolved.securities_usd == Decimal("213.47") * Decimal("3.4820")
    assert resolved.security_name == "EXAMPLE CORP"


def test_reconstructed_folded_position_resolves(cfg: NetBenefitsConfig) -> None:
    surviving = _open(quantity=Decimal("2.4820"), contribution_usd=Decimal("476.84"))
    resolved = _resolve(cfg, lots=_reconstruct([surviving], [_closed()]))

    (position,) = resolved.positions
    assert position.lot.quantity == Decimal("3.4820")
    assert position.lot.cost_basis_money == Decimal("743.30")


def test_missing_confirmation_refuses(cfg: NetBenefitsConfig) -> None:
    with pytest.raises(NetBenefitsFaError) as caught:
        _resolve(cfg, purchases=[])
    # #1042: the lot date used to be printed.
    _assert_refusal(
        caught.value,
        "open-lots data row 1: no purchase confirmation covers this position; the FMV cost "
        "basis cannot be sourced",
        absent=("2025-12-31",),
    )


def test_later_missing_confirmation_refuses(cfg: NetBenefitsConfig) -> None:
    lots = [
        EsppDec31Lot(_open(), open_row=1),
        EsppDec31Lot(_open(acquire_date=_OCTOBER), open_row=2),
    ]
    with pytest.raises(NetBenefitsFaError) as caught:
        _resolve(cfg, lots=lots)
    # #1042: names the second open-lots row, not its date.
    _assert_refusal(
        caught.value,
        "open-lots data row 2: no purchase confirmation covers this position; the FMV cost "
        "basis cannot be sourced",
        absent=("2025-10-15",),
    )


def test_gap_only_position_without_a_confirmation_names_its_lot_rows(
    cfg: NetBenefitsConfig,
) -> None:
    """A Dec-31 purchase sold the next CY, with no confirmation at all, names
    the closed-lots rows it was folded from and the missing-confirmation rule (#1042)."""
    lots = _reconstruct([], [_closed(), _pre_cy_closed(), _closed()], purchases=[])
    assert [(held.open_row, held.closed_rows) for held in lots] == [(None, (1, 3))]
    with pytest.raises(NetBenefitsFaError) as caught:
        _resolve(cfg, lots=lots, purchases=[])
    _assert_refusal(
        caught.value,
        "the position folded from closed-lots data rows 1 and 3: no purchase confirmation "
        "covers this position; the FMV cost basis cannot be sourced",
        absent=("2025-12-31",),
    )


@pytest.mark.parametrize(
    ("field", "value", "config", "problem", "absent", "absent_tokens"),
    [
        pytest.param(
            "participant_no",
            "I12345678",
            {"participant_no": "I00000001"},
            "the PARTICIPANT NO. differs from netbenefits.participant_no in user.toml",
            ("I12345678", "I00000001"),
            (),
            id="participant_no",
        ),
        pytest.param(
            "symbol",
            "ANN",
            {},
            "the SYMBOL differs from netbenefits.security.symbol in user.toml",
            ("XESP",),
            ("ANN",),
            id="symbol",
        ),
        pytest.param(
            "cusip",
            "99993A108",
            {},
            "the CUSIP differs from netbenefits.security.cusip in user.toml",
            ("99993A108", "99999E104"),
            (),
            id="cusip",
        ),
    ],
)
def test_confirmation_for_another_security_refuses(
    cfg: NetBenefitsConfig,
    field: str,
    value: str,
    config: dict[str, str],
    problem: str,
    absent: tuple[str, ...],
    absent_tokens: tuple[str, ...],
) -> None:
    # #1042: names the differing field, never the confirmation's or the configured value.
    with pytest.raises(NetBenefitsFaError) as caught:
        _resolve(replace(cfg, **config), purchases=[_purchase(**{field: value})])
    _assert_refusal(
        caught.value,
        f"confirmation 1 (by purchase date): {problem}",
        absent=absent,
        absent_tokens=absent_tokens,
    )


@pytest.mark.parametrize(
    ("field", "message", "absent"),
    [
        pytest.param(
            "quantity",
            "open-lots data row 1: the Quantity differs from the shares on confirmation 1 "
            "(by purchase date)",
            ("1234567.89", "3.482"),
            id="quantity",
        ),
        pytest.param(
            "contribution_usd",
            "open-lots data row 1: the Cost basis differs from the Accumulated Contributions "
            "on confirmation 1 (by purchase date)",
            ("1234567.89", "668.96"),
            id="contribution",
        ),
        pytest.param(
            "cost_per_share_paid",
            "open-lots data row 1: the Cost basis/share differs from the purchase price on "
            "confirmation 1 (by purchase date)",
            ("1234567.89", "192.12"),
            id="cost-per-share",
        ),
    ],
)
def test_lot_disagreeing_with_its_confirmation_refuses(
    cfg: NetBenefitsConfig, field: str, message: str, absent: tuple[str, ...]
) -> None:
    # #1042: neither the lot's figure nor the confirmation's is printed.
    lots = [EsppDec31Lot(_open(**{field: Decimal("1234567.89")}), open_row=1)]
    with pytest.raises(NetBenefitsFaError) as caught:
        _resolve(cfg, lots=lots)
    _assert_refusal(caught.value, message, absent=absent)


@pytest.mark.parametrize(
    "purchases",
    [
        pytest.param([_purchase(), _october_purchase()], id="later-date-first"),
        pytest.param([_october_purchase(), _purchase()], id="earlier-date-first"),
    ],
)
def test_second_open_lots_row_and_second_confirmation_are_named(
    cfg: NetBenefitsConfig, purchases: list[EspPurchase]
) -> None:
    """Lots are numbered by open-lots data row and confirmations by purchase
    date, whatever order the confirmation mapping lists them in (#1042)."""
    lots = _reconstruct([_later_open(), _open(quantity=Decimal("1234567.89"))], purchases=purchases)
    with pytest.raises(NetBenefitsFaError) as caught:
        _resolve(cfg, lots=lots, purchases=purchases)
    _assert_refusal(
        caught.value,
        "open-lots data row 2: the Quantity differs from the shares on confirmation 2 "
        "(by purchase date)",
        absent=("1234567.89", "3.482"),
    )


def test_folded_position_quantity_mismatch_names_its_confirmation_and_lot_rows(
    cfg: NetBenefitsConfig,
) -> None:
    """A Dec-31 purchase folded from an open lot and two gap-window sales that
    don't add up to the confirmation names the confirmation and every
    contributing lot row, never the share counts (#1042)."""
    surviving = _open(quantity=Decimal("1.0000"), contribution_usd=Decimal("192.12"))
    purchases = [_purchase(), _october_purchase()]
    lots = _reconstruct(
        [_later_open(), surviving], [_pre_cy_closed(), _closed(), _closed()], purchases
    )
    assert [(held.open_row, held.closed_rows) for held in lots] == [(2, (2, 3))]
    with pytest.raises(NetBenefitsFaError) as caught:
        _resolve(cfg, lots=lots, purchases=purchases)
    _assert_refusal(
        caught.value,
        "the position folded from open-lots data row 2 and closed-lots data rows 2 and 3: "
        "the Quantity differs from the shares on confirmation 2 (by purchase date)",
        absent=("3.0000", "3.482"),
    )


def test_discount_mismatch_refuses(cfg: NetBenefitsConfig) -> None:
    mismatch = espp_discount_mismatch(_purchase(), Decimal("25"))
    assert mismatch is not None
    with pytest.raises(NetBenefitsFaError) as caught:
        _resolve(replace(cfg, espp_discount_pct=Decimal("25")))
    # #1042: the price, the FMV per share (213.469...), the discounted price,
    # their gap, the tolerance and the discount % used to be printed.
    _assert_refusal(
        caught.value,
        "confirmation 1 (by purchase date): the purchase price is not the FMV per share less "
        "the netbenefits.espp_discount_pct discount, within the rounding tolerance",
        absent=(
            "192.12",
            "213.469",
            "25%",
            str(mismatch.fmv_per_share_usd),
            str(mismatch.expected_price_usd),
            str(mismatch.deviation_usd),
            str(mismatch.tolerance_usd),
        ),
    )


def test_lot_not_bought_on_dec31_refuses(cfg: NetBenefitsConfig) -> None:
    # The October confirmation ranks first even though the mapping lists Dec-31 first.
    with pytest.raises(NetBenefitsFaError) as caught:
        _resolve(
            cfg,
            lots=[EsppDec31Lot(_open(acquire_date=_OCTOBER), open_row=1)],
            purchases=[_purchase(), _october_purchase()],
        )
    # #1042: the purchase date used to be printed; CY2025 is the caller's calendar year.
    _assert_refusal(
        caught.value,
        "open-lots data row 1: confirmation 1 (by purchase date) is not a Dec-31 purchase, so "
        "the lot is held across part of CY2025; its in-CY dividend can't be proven here — "
        "wire dividends via #I10",
        absent=("2025-10-15",),
    )


@pytest.mark.parametrize(
    ("extra_dates", "entries"),
    [
        pytest.param(
            (date(2025, 12, 30),),
            "netbenefits.price_overrides[0] and netbenefits.price_overrides[1]",
            id="two-in-window",
        ),
        pytest.param(
            (date(2025, 12, 1), date(2025, 12, 30), date(2025, 12, 28)),
            "netbenefits.price_overrides[0], netbenefits.price_overrides[2] and "
            "netbenefits.price_overrides[3]",
            id="three-of-four-in-window",
        ),
    ],
)
def test_two_overrides_in_the_carry_back_window_refuse(
    cfg: NetBenefitsConfig, extra_dates: tuple[date, ...], entries: str
) -> None:
    extras = tuple(
        NetBenefitsPriceOverride(
            price_date=price_date,
            price_per_share_usd=Decimal("213.47"),
            source="dup",
            reason="dup",
        )
        for price_date in extra_dates
    )
    with pytest.raises(NetBenefitsFaError) as caught:
        _resolve(replace(cfg, price_overrides=(*cfg.price_overrides, *extras)))
    # #1042: names the config entries by index, not the valuation date.
    _assert_refusal(
        caught.value,
        f"{entries} are each dated within 3 days on/before the valuation date; the override "
        "is ambiguous",
        absent=("2025-12-31",),
    )


def test_close_outside_the_fmv_anchor_tolerance_refuses(cfg: NetBenefitsConfig) -> None:
    (override,) = cfg.price_overrides
    typo = replace(override, price_per_share_usd=Decimal("500.00"))
    with pytest.raises(ClosingPriceError) as caught:
        _resolve(replace(cfg, price_overrides=(typo,)))
    # #1042: the close (500.00 x 3.4820 = 1741.00), the FMV at purchase, their
    # gap (997.70) and the tolerance (0.02741) used to be printed.
    _assert_refusal(
        caught.value,
        "open-lots data row 1: the Dec-31 close (override) differs from the Market Value at "
        "Purchase on confirmation 1 (by purchase date) by more than the tolerance; "
        f"{NB_OVERRIDE_ANCHOR_ROUTE}",
        absent=("1741", "743.3", "997.7", "0.02741"),
    )


def test_fmv_anchor_keeps_a_long_difference_within_the_exact_tolerance(
    cfg: NetBenefitsConfig,
) -> None:
    """The exact difference is just below 0.027410000000000000000000050000005.
    Main rounded the close up, crossed its rounded tolerance and refused."""
    shares = Decimal("3.482000000000000000000010000001")
    (override,) = cfg.price_overrides
    edge = replace(
        override,
        price_per_share_usd=Decimal("213.47714244686961516369841047626710487116642544535655"),
    )
    resolved = _resolve(
        replace(cfg, price_overrides=(edge,)),
        lots=[EsppDec31Lot(_open(quantity=shares), open_row=1)],
        purchases=[_purchase(shares=shares)],
    )
    (position,) = resolved.positions
    assert (
        position.close.total_usd.compare_total(
            Decimal(
                "743.32741000000000000000000005000000499999999999999996818152497876912561999094535655"
            )
        )
        == 0
    )


def test_close_just_past_the_fmv_anchor_tolerance_refuses(cfg: NetBenefitsConfig) -> None:
    """The exact close is 0.0274100000000000000000000000000000011280 above
    the FMV at purchase: just past the 0.02741 tolerance (#1250). Main rounded
    the close to 28 digits, 743.32741, on the tolerance, and passed it.
    Rounding only the subtraction or its absolute value would also pass it."""
    (override,) = cfg.price_overrides
    edge = replace(
        override, price_per_share_usd=Decimal("213.477142446869615163699023549684089604")
    )
    with pytest.raises(ClosingPriceError) as caught:
        _resolve(replace(cfg, price_overrides=(edge,)))
    _assert_refusal(
        caught.value,
        "open-lots data row 1: the Dec-31 close (override) differs from the Market Value at "
        "Purchase on confirmation 1 (by purchase date) by more than the tolerance; "
        f"{NB_OVERRIDE_ANCHOR_ROUTE}",
    )


def _resolve_at_total(
    cfg: NetBenefitsConfig,
    monkeypatch: pytest.MonkeyPatch,
    total: Decimal,
    *,
    shares: Decimal = Decimal("3.4820"),
    fmv_total_usd: Decimal = Decimal("743.30"),
) -> EsppDec31Positions:
    """Resolve one open lot of ``shares`` against a confirmation with that FMV
    total, with the close resolver replaced by a statement-style close of
    ``total``: with no per-share price, the total need not be shares x a price."""

    def resolve_close(
        lot: EspLot,
        cfg: NetBenefitsConfig,
        valuation_date: date,
        series: PriceSeries | None,
        **_kwargs: object,
    ) -> ResolvedClose:
        return ResolvedClose(
            total_usd=total, per_share_usd=None, source=CloseSource.STATEMENT, provenance="test"
        )

    monkeypatch.setattr("ibkr_fa.compute.espp_positions._resolve_close", resolve_close)
    return _resolve(
        cfg,
        lots=[EsppDec31Lot(_open(quantity=shares), open_row=1)],
        purchases=[_purchase(shares=shares, fmv_total_usd=fmv_total_usd)],
    )


@pytest.mark.parametrize(
    ("total", "shares", "fmv_total_usd", "at"),
    [
        # 2E+1000 x 0.005 is 1E+998, but adding 0.01 needs 1001 digits. The
        # FMV is 213.47 a share, so the plan-discount check passes.
        pytest.param(
            Decimal("4.2694E+1002"),
            Decimal("2E+1000"),
            Decimal("4.2694E+1002"),
            "NetBenefits FA, close anchor tolerance",
            id="tolerance",
        ),
        # The close is 0.01 + 1E-1002 off the FMV: within the tolerance, but
        # the difference needs 1001 digits.
        pytest.param(
            Decimal("743.31" + "0" * 999 + "1"),
            Decimal("3.4820"),
            Decimal("743.30"),
            "NetBenefits FA, close anchor difference",
            id="difference",
        ),
    ],
)
def test_fmv_anchor_refuses_past_the_cap(
    cfg: NetBenefitsConfig,
    monkeypatch: pytest.MonkeyPatch,
    total: Decimal,
    shares: Decimal,
    fmv_total_usd: Decimal,
    at: str,
) -> None:
    """The anchor's tolerance and difference are exact or refuse past the
    1000-digit cap, with a fixed label, no context and no chained error
    (#1250). Main rounded both to 28 digits, passed the anchor and refused
    only later, at the securities total of the long close (#1168)."""
    with pytest.raises(InvariantError) as caught:
        _resolve_at_total(cfg, monkeypatch, total, shares=shares, fmv_total_usd=fmv_total_usd)
    assert str(caught.value) == f"InvariantError: {at}: {INEXACT_SUM}"
    assert caught.value.context == ()
    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None


@pytest.mark.parametrize(
    ("peak_basis", "prices_fetched", "refusal"),
    [
        pytest.param("close", True, NB_NO_CLOSE_REFUSAL, id="close-fetched"),
        pytest.param("close", False, NB_NO_CLOSE_REFUSAL, id="close-not-fetched"),
        pytest.param("high", True, NB_NO_CLOSE_FETCHED_HIGH_REFUSAL, id="high-fetched"),
        pytest.param("high", False, NB_NO_CLOSE_NOT_FETCHED_HIGH_REFUSAL, id="high-not-fetched"),
    ],
)
def test_no_usable_close_names_the_netbenefits_override(
    cfg: NetBenefitsConfig, peak_basis: PeakBasis, prices_fetched: bool, refusal: str
) -> None:
    # #1116: no override and no Yahoo series; the remedy names the table this
    # module reads, for the valuation date. #1565: an override gives only the
    # close, so under peak_basis = "high" the route also names the A3 peak's
    # step: a fetch when prices weren't fetched, else entering it by hand.
    with pytest.raises(ClosingPriceError) as caught:
        _resolve(
            replace(cfg, price_overrides=()),
            peak_basis=peak_basis,
            prices_fetched=prices_fetched,
        )
    _assert_refusal(caught.value, refusal)


def test_split_refusal_carries_no_override_remedy(cfg: NetBenefitsConfig) -> None:
    # #1116: the remedy belongs to the no-source refusal only.
    split = PriceSeries(
        ticker=Ticker("XESP"),
        currency="USD",
        closes={_DEC31: Decimal("213.47")},
        has_split_in_range=True,
        source="test",
        fetched_at=datetime(2026, 1, 1, tzinfo=UTC),
    )
    with pytest.raises(ClosingPriceError) as caught:
        _resolve(cfg, series=split)
    _assert_refusal(
        caught.value,
        "split in range for 2025-12-31: Yahoo and any per-share override (x the current, "
        "rebased quantity) are unsafe and there is no broker statement total to fall back on; "
        "the price cache holds this security's closes on the current-split basis, so move its "
        "cache file, and any .lock beside it, out of the price cache directory and rerun with "
        "price fetching on",
    )
