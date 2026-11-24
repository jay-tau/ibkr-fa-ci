"""Exact Schedule FA securities and gross-credit totals (#1168).

Owner decision (2026-11-10, made on #1166 and applied to #1168): these sums
never round. The NetBenefits securities total, the per-symbol dividend
totals and Table A2's gross interest, proceeds and dividends add in a copy
of the active context widened to at least ``EXACT_SUM_DIGITS``. They refuse
only a nonfinite amount, a decimal arithmetic failure or an addition that
would still round at that precision.

Dividend and interest totals are converted at the Dec-31 rate after adding;
since #1312 that product is exact too, so those outputs keep every digit. The
A2 proceeds and cross-symbol dividend totals add amounts already converted
to INR; the securities total adds resolved USD closes. These sums keep every
digit. All values are synthetic.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import replace
from datetime import date
from decimal import Context, Decimal, Inexact, Rounded, getcontext, localcontext
from pathlib import Path

import pytest

from ibkr_fa.compute.dec31_close import CloseSource, ClosingPriceError, ResolvedClose
from ibkr_fa.compute.dividends import aggregate_dividends_by_symbol_inr
from ibkr_fa.compute.espp_positions import (
    EsppDec31Lot,
    EsppDec31Positions,
    NetBenefitsFaError,
    resolve_espp_dec31_positions,
)
from ibkr_fa.compute.exact_inr import checked_addition
from ibkr_fa.compute.fa_a2 import FaA2GrossEvidence, build_gross_evidence, gross_interest_inr
from ibkr_fa.compute.fa_a3_dividends import resolve_fa_a3_dividends
from ibkr_fa.config import NetBenefitsConfig, load_user_config
from ibkr_fa.exceptions import InvariantError
from ibkr_fa.fx.policy import FxPolicy
from ibkr_fa.models import ClosedLot, Dividend, DividendEvidence, InterestCredit
from ibkr_fa.models.fa_a2_row import FaA2NatureOfAmount
from ibkr_fa.models.money import to_whole_rupees
from ibkr_fa.types import Isin, Ticker
from tests._fa_exact_sums import (
    DESCRIPTION,
    HUGE,
    ISIN,
    LONG,
    MARKED_DATE,
    MARKERS,
    MINUTE,
    NUMERIC,
    SIX,
    SOURCE,
    SYMBOL,
    TINY,
    assert_refusal,
    message,
)
from tests._fa_lot_inputs import fa_closed_lot as _closed_lot
from tests._fx_stubs import DateAwareRatePolicy as _DateAwareRatePolicy
from tests._fx_stubs import FixedRatePolicy as _FixedRatePolicy
from tests._netbenefits import esp_open_lot as _open
from tests._netbenefits import esp_purchase as _purchase
from tests._netbenefits_fa import BASE_CONFIG as ESPP_CONFIG
from tests._netbenefits_fa import NB_BY_HAND

_FX_ONE = _FixedRatePolicy(Decimal("1"))
_TTBR = _FixedRatePolicy(Decimal("85.47"))
_IN_YEAR = date(2025, 6, 2)
_LAST_YEAR = date(2024, 12, 31)
_SYMBOLS = ("XQFA", "XQFB", "XQFC", "XQFD", "XQFE", "XQFG")
# ``SIX`` added exactly keeps the trailing zero: 29 digits. Times FX 1 exactly
# (#1312), it keeps it; main's 28-digit product dropped it.
_EXACT_SIX = Decimal("1.5000000000000000000000000000")
_SYNTHETIC_AT = "Synthetic total, input row 2, column amount"
_INTEREST_AT = "Schedule FA A2 gross interest, input row {}, column amount_gross"
_DIVIDENDS_AT = "Schedule FA per-symbol dividends, input row {}, column amount_gross"
_PROCEEDS_AT = "Schedule FA A2 gross proceeds, input lot {}, field gross_proceeds_inr"
_A2_DIVIDENDS_AT = "Schedule FA A2 gross dividends, contribution {}, field dividend_total_inr"
_SECURITIES_AT = "NetBenefits FA securities, input lot {}, field total_usd"
_ESPP_MARKERS = tuple(marker for marker in MARKERS if marker != ISIN)


def _refused(
    raised: pytest.ExceptionInfo[InvariantError],
    capsys: pytest.CaptureFixture[str],
    at: str,
    *,
    source_row_index: int | None = None,
    markers: tuple[str, ...] = MARKERS,
) -> None:
    """The full located message, no context but the source row, no chained
    error and no marker in the exception chain or the captured output."""
    error = raised.value
    assert_refusal(error, message(at, source_row_index=source_row_index), markers=markers)
    row = () if source_row_index is None else (("source_row_index", source_row_index),)
    assert error.context == row
    captured = capsys.readouterr()
    for marker in markers:
        assert marker not in captured.out + captured.err, marker


def _ordered(amounts: Sequence[str], *, reverse: bool) -> tuple[str, ...]:
    return tuple(reversed(amounts)) if reverse else tuple(amounts)


# ---- inputs ------------------------------------------------------------------


def _payment(
    amount: str,
    *,
    symbol: str = "XQFA",
    payment_date: date = _IN_YEAR,
    source_row_index: int | None = None,
) -> Dividend:
    return Dividend(
        symbol=Ticker(symbol),
        isin=Isin("US99990E1038"),
        payment_date=payment_date,
        amount_gross=Decimal(amount),
        currency="USD",
        description=f"{symbol}(US99990E1038) Cash Dividend USD 0.50 per Share",
        source_row_index=source_row_index,
    )


def _marked_payment(*, source_row_index: int | None = None) -> Dividend:
    """An in-year payment carrying every marker; it adds exactly."""
    return Dividend(
        symbol=Ticker(SYMBOL),
        isin=Isin(ISIN),
        payment_date=MARKED_DATE,
        amount_gross=Decimal(NUMERIC),
        currency="USD",
        description=f"{SYMBOL}({ISIN}) {DESCRIPTION} {SOURCE}",
        source_row_index=source_row_index,
    )


def _credit(
    amount: str,
    *,
    credit_date: date = _IN_YEAR,
    currency: str = "USD",
    source_row_index: int | None = None,
) -> InterestCredit:
    return InterestCredit(
        credit_date=credit_date,
        amount_gross=Decimal(amount),
        currency=currency,
        description=f"{currency} Credit Interest",
        source_row_index=source_row_index,
    )


def _marked_credit(*, currency: str = "USD", source_row_index: int | None = None) -> InterestCredit:
    """An in-year credit carrying every marker the model holds; it adds exactly."""
    return InterestCredit(
        credit_date=MARKED_DATE,
        amount_gross=Decimal(NUMERIC),
        currency=currency,
        description=f"{SYMBOL} {ISIN} {DESCRIPTION} {SOURCE}",
        source_row_index=source_row_index,
    )


def _lot(gross_proceeds: str | None, *, year: int = 2025) -> ClosedLot:
    return _closed_lot(year, None if gross_proceeds is None else Decimal(gross_proceeds))


def _marked_lot() -> ClosedLot:
    """An in-year lot carrying every marker; its proceeds add exactly."""
    return replace(
        _lot(NUMERIC),
        symbol=Ticker(SYMBOL),
        isin=Isin(ISIN),
        description=f"{DESCRIPTION} {SOURCE}",
        open_date_time=MARKED_DATE,
        close_date_time=MARKED_DATE,
    )


def _evidence(
    dividends: Iterable[Dividend] = (),
    interest: Iterable[InterestCredit] = (),
    lots: Iterable[ClosedLot] = (),
    *,
    fx_policy: FxPolicy = _FX_ONE,
) -> FaA2GrossEvidence:
    return build_gross_evidence(
        list(dividends),
        list(interest),
        list(lots),
        interest_non_credits=(),
        calendar_year=2025,
        fx_policy=fx_policy,
        dividend_activity_present=False,
    )


# ---- shared helper -----------------------------------------------------------


@pytest.mark.parametrize("source_row_index", [None, 0, 7])
def test_checked_addition_appends_only_the_given_source_row(
    source_row_index: int | None, capsys: pytest.CaptureFixture[str]
) -> None:
    """The cap refusal ends with exactly `` (context keys: source_row_index=<r>)``
    when given a row, 0 included, and with nothing otherwise (#1168)."""
    with pytest.raises(InvariantError) as raised:
        checked_addition(
            Decimal(HUGE), Decimal(MINUTE), at=_SYNTHETIC_AT, source_row_index=source_row_index
        )
    _refused(raised, capsys, _SYNTHETIC_AT, source_row_index=source_row_index, markers=())


def test_checked_addition_leaves_a_hostile_context_unchanged() -> None:
    """A 9-digit context trapping ``Inexact`` and ``Rounded`` neither rounds
    the exact sum nor turns the refusal into a decimal trap, and keeps its
    precision, traps and flags (#1168)."""
    with localcontext(Context(prec=9, traps=[Inexact, Rounded])):
        total = checked_addition(Decimal(LONG), Decimal(TINY), at=_SYNTHETIC_AT, source_row_index=3)
        with pytest.raises(InvariantError) as raised:
            checked_addition(Decimal(HUGE), Decimal(MINUTE), at=_SYNTHETIC_AT, source_row_index=3)
        context = getcontext()
        assert context.prec == 9
        assert context.traps[Inexact]
        assert context.traps[Rounded]
        assert not any(context.flags.values())
    assert total.compare_total(Decimal("1.4999999999999999999999999984")) == 0
    assert_refusal(raised.value, message(_SYNTHETIC_AT, source_row_index=3), markers=())


# ---- gross_interest_inr ------------------------------------------------------


@pytest.mark.parametrize("reverse", [False, True], ids=["six", "reversed"])
def test_gross_interest_adds_six_credits_exactly(reverse: bool) -> None:
    """Six credits total exactly 1.50, so ₹2 at FX 1; main kept ``LONG`` and
    filed ₹1 before #1168. In both orders the FX product keeps the sum's 29th
    digit too, which main's 28-digit product dropped (#1312)."""
    interest = [_credit(amount) for amount in _ordered(SIX, reverse=reverse)]
    total = gross_interest_inr(interest, calendar_year=2025, fx_policy=_FX_ONE)
    assert total == Decimal("1.5")
    assert total.compare_total(_EXACT_SIX) == 0
    assert to_whole_rupees(total) == 2


@pytest.mark.parametrize("source_row_index", [None, 0, 7])
def test_gross_interest_past_the_cap_refuses_at_its_row(
    source_row_index: int | None, capsys: pytest.CaptureFixture[str]
) -> None:
    """Row 4 fails. The out-of-year credit keeps its place in the count (else
    row 3) but isn't added (else row 3). The suffix is the failing credit's
    own source row, not another credit's (#1168)."""
    interest = [
        _credit(MINUTE, credit_date=_LAST_YEAR, source_row_index=20),
        _marked_credit(source_row_index=21),
        _credit(HUGE, source_row_index=22),
        _credit(MINUTE, source_row_index=source_row_index),
    ]
    with pytest.raises(InvariantError) as raised:
        gross_interest_inr(interest, calendar_year=2025, fx_policy=_FX_ONE)
    _refused(raised, capsys, _INTEREST_AT.format(4), source_row_index=source_row_index)


def test_gross_interest_checks_the_currency_before_adding() -> None:
    """Preservation: another currency still refuses first, even when an
    earlier pair of credits would fail the cap (#1168)."""
    interest = [_credit(HUGE), _credit(MINUTE), _credit("10", currency="EUR")]
    with pytest.raises(InvariantError) as raised:
        gross_interest_inr(interest, calendar_year=2025, fx_policy=_FX_ONE)
    assert str(raised.value) == (
        "InvariantError: gross_interest_inr: every credit must be in the FX policy currency "
        "(context keys: currency)"
    )


@pytest.mark.parametrize(
    "interest",
    [(), (_credit(HUGE, credit_date=_LAST_YEAR), _credit(MINUTE, credit_date=date(2026, 1, 1)))],
    ids=["empty", "other-years"],
)
def test_gross_interest_without_an_in_year_credit_is_zero_without_a_rate(
    interest: tuple[InterestCredit, ...],
) -> None:
    """Preservation: no in-year credit is 0 with no FX lookup; other years'
    credits are never added, so they can't reach the cap (#1168)."""
    fx = _DateAwareRatePolicy({})
    total = gross_interest_inr(list(interest), calendar_year=2025, fx_policy=fx)
    assert total.compare_total(Decimal(0)) == 0
    assert fx.requested_dates == []


def test_gross_interest_keeps_ordinary_cents() -> None:
    """Preservation: cent credits at a TTBR keep their value and exponent (#1168)."""
    interest = [_credit("12.34"), _credit("0.56"), _credit("7.15")]
    total = gross_interest_inr(interest, calendar_year=2025, fx_policy=_TTBR)
    assert total.compare_total(Decimal("1713.6735")) == 0  # 20.05 x 85.47


# ---- aggregate_dividends_by_symbol_inr ---------------------------------------


@pytest.mark.parametrize("reverse", [False, True], ids=["six", "reversed"])
def test_per_symbol_dividends_add_six_payments_exactly(reverse: bool) -> None:
    """One symbol's six payments total exactly 1.50, so ₹2 at FX 1; main kept
    ``LONG`` and filed ₹1 before #1168. In both orders the FX product keeps the
    sum's 29th digit, as for interest (#1312)."""
    payments = [_payment(amount) for amount in _ordered(SIX, reverse=reverse)]
    totals = aggregate_dividends_by_symbol_inr(payments, calendar_year=2025, fx_policy=_FX_ONE)
    assert totals == {Ticker("XQFA"): Decimal("1.5")}
    assert totals[Ticker("XQFA")].compare_total(_EXACT_SIX) == 0
    assert to_whole_rupees(totals[Ticker("XQFA")]) == 2


@pytest.mark.parametrize("source_row_index", [None, 0, 7])
def test_per_symbol_dividends_past_the_cap_refuse_at_their_row(
    source_row_index: int | None, capsys: pytest.CaptureFixture[str]
) -> None:
    """XQFB fails at row 5. Rows count before year filtering (else row 4);
    the out-of-year XQFB payment isn't added (else row 3); and totals are per
    symbol, so ``SYMBOL``'s ``MINUTE`` at row 4 still fits (#1168)."""
    payments = [
        _payment(MINUTE, symbol="XQFB", payment_date=_LAST_YEAR, source_row_index=30),
        _marked_payment(source_row_index=31),
        _payment(HUGE, symbol="XQFB", source_row_index=32),
        replace(_marked_payment(source_row_index=33), amount_gross=Decimal(MINUTE)),
        _payment(MINUTE, symbol="XQFB", source_row_index=source_row_index),
    ]
    with pytest.raises(InvariantError) as raised:
        aggregate_dividends_by_symbol_inr(payments, calendar_year=2025, fx_policy=_FX_ONE)
    _refused(raised, capsys, _DIVIDENDS_AT.format(5), source_row_index=source_row_index)


def test_per_symbol_dividends_add_only_in_year_payments() -> None:
    """Preservation: other years' payments are left out before adding, so a
    pair that would fail the cap never reaches it (#1168)."""
    payments = [
        _payment(HUGE, payment_date=_LAST_YEAR),
        _payment("5"),
        _payment(MINUTE, payment_date=date(2026, 1, 1)),
    ]
    totals = aggregate_dividends_by_symbol_inr(payments, calendar_year=2025, fx_policy=_FX_ONE)
    assert totals == {Ticker("XQFA"): Decimal("5")}


def test_per_symbol_dividends_without_payments_are_empty_without_a_rate() -> None:
    """Preservation: no payment gives ``{}`` with no FX lookup (#1168)."""
    fx = _DateAwareRatePolicy({})
    assert aggregate_dividends_by_symbol_inr([], calendar_year=2025, fx_policy=fx) == {}
    assert fx.requested_dates == []


def test_per_symbol_dividends_keep_ordinary_cents() -> None:
    """Preservation: cent payments at a TTBR keep their value and exponent (#1168)."""
    payments = [_payment("12.34"), _payment("7.15", symbol="XQFB"), _payment("0.56")]
    totals = aggregate_dividends_by_symbol_inr(payments, calendar_year=2025, fx_policy=_TTBR)
    assert list(totals) == [Ticker("XQFA"), Ticker("XQFB")]
    assert totals[Ticker("XQFA")].compare_total(Decimal("1102.5630")) == 0  # 12.90 x 85.47
    assert totals[Ticker("XQFB")].compare_total(Decimal("611.1105")) == 0  # 7.15 x 85.47


# ---- build_gross_evidence ----------------------------------------------------


@pytest.mark.parametrize("reverse", [False, True], ids=["six", "reversed"])
def test_a2_gross_proceeds_add_six_lots_exactly(reverse: bool) -> None:
    """Proceeds convert per lot before adding, so the total keeps every digit:
    exactly 1.50 in both orders, ₹2. Main kept ``LONG`` (forward) or dropped
    the 29th digit (reversed) (#1168)."""
    evidence = _evidence(lots=[_lot(amount) for amount in _ordered(SIX, reverse=reverse)])
    proceeds = evidence.known_components[FaA2NatureOfAmount.PROCEEDS]
    assert list(evidence.known_components) == [FaA2NatureOfAmount.PROCEEDS]
    assert proceeds.compare_total(_EXACT_SIX) == 0
    assert to_whole_rupees(proceeds) == 2
    assert evidence.unresolved_natures == ()


def test_a2_gross_proceeds_past_the_cap_refuse_at_their_lot(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Lot 5 fails, after exact dividend and interest totals. Lots count
    before year filtering (else lot 4); the out-of-year lot isn't added (else
    lot 4); a lot without gross proceeds doesn't stop the sum; and the failure
    raises instead of leaving PROCEEDS unresolved (#1168)."""
    lots = [_lot(MINUTE, year=2024), _marked_lot(), _lot(None), _lot(HUGE), _lot(MINUTE)]
    with pytest.raises(InvariantError) as raised:
        _evidence([_payment("5")], [_credit("10")], lots)
    _refused(raised, capsys, _PROCEEDS_AT.format(5))


def test_a2_gross_proceeds_keep_the_exact_partial_total_beside_a_missing_one() -> None:
    """A lot without gross proceeds still leaves PROCEEDS unresolved, and the
    known partial total is the exact 1.50 (#1168)."""
    evidence = _evidence(lots=[*(_lot(amount) for amount in SIX), _lot(None)])
    assert evidence.known_components[FaA2NatureOfAmount.PROCEEDS].compare_total(_EXACT_SIX) == 0
    assert evidence.unresolved_natures == (FaA2NatureOfAmount.PROCEEDS,)


@pytest.mark.parametrize("reverse", [False, True], ids=["six", "reversed"])
def test_a2_gross_dividends_add_six_symbols_exactly(reverse: bool) -> None:
    """One exact payment per symbol, so every per-symbol total passes and the
    cross-symbol total is exactly 1.50 in both orders, ₹2 (#1168)."""
    amounts = _ordered(SIX, reverse=reverse)
    payments = [
        _payment(amount, symbol=symbol) for symbol, amount in zip(_SYMBOLS, amounts, strict=True)
    ]
    evidence = _evidence(payments)
    dividends = evidence.known_components[FaA2NatureOfAmount.DIVIDEND]
    assert list(evidence.known_components) == [FaA2NatureOfAmount.DIVIDEND]
    assert dividends.compare_total(_EXACT_SIX) == 0
    assert to_whole_rupees(dividends) == 2
    assert evidence.unresolved_natures == ()


def test_a2_gross_dividends_past_the_cap_refuse_at_their_contribution(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Each symbol's total is exact; XQFB's ``MINUTE``, the third symbol
    total (input row 5), fails the cross-symbol sum. The out-of-year payment
    makes no contribution, and no source row is named although every payment
    has one (#1168)."""
    payments = [
        _payment(MINUTE, symbol="XQFC", payment_date=_LAST_YEAR, source_row_index=40),
        _payment(HUGE, source_row_index=41),
        _marked_payment(source_row_index=42),
        _payment("5", source_row_index=43),
        _payment(MINUTE, symbol="XQFB", source_row_index=44),
    ]
    with pytest.raises(InvariantError) as raised:
        _evidence(payments)
    _refused(raised, capsys, _A2_DIVIDENDS_AT.format(3))


def test_a2_gross_interest_keeps_the_exact_partial_total_beside_another_currency() -> None:
    """A credit in another currency still leaves INTEREST unresolved, and the
    known partial total is the exact six-credit total, ₹2 at FX 1 (#1168),
    with the 29th digit main's 28-digit FX product dropped (#1312)."""
    evidence = _evidence(interest=[_credit("10", currency="EUR"), *(_credit(a) for a in SIX)])
    interest = evidence.known_components[FaA2NatureOfAmount.INTEREST]
    assert interest.compare_total(_EXACT_SIX) == 0
    assert to_whole_rupees(interest) == 2
    assert evidence.unresolved_natures == (FaA2NatureOfAmount.INTEREST,)


@pytest.mark.parametrize("source_row_index", [None, 7])
def test_a2_gross_interest_refusal_counts_every_credit(
    source_row_index: int | None, capsys: pytest.CaptureFixture[str]
) -> None:
    """After an exact dividend total, the fifth credit fails. Only the USD
    credits reach the sum, but the EUR credit before them still counts, so it
    is row 5; the failure raises instead of leaving INTEREST unresolved
    (#1168)."""
    interest = [
        _marked_credit(currency="EUR", source_row_index=50),
        _credit(MINUTE, credit_date=_LAST_YEAR, source_row_index=51),
        _marked_credit(source_row_index=52),
        _credit(HUGE, source_row_index=53),
        _credit(MINUTE, source_row_index=source_row_index),
    ]
    with pytest.raises(InvariantError) as raised:
        _evidence([_payment("5")], interest)
    _refused(raised, capsys, _INTEREST_AT.format(5), source_row_index=source_row_index)


# ---- resolve_fa_a3_dividends -------------------------------------------------


def test_a3_dividends_carry_the_exact_six_payment_total() -> None:
    """A3's dividend column gets the exact six-payment total, ₹2 at FX 1;
    main filed ₹1 (#1168)."""
    totals, zeroed = resolve_fa_a3_dividends(
        [_payment(amount) for amount in SIX],
        calendar_year=2025,
        fx_policy=_FX_ONE,
        dividend_evidence=DividendEvidence.PRESENT,
    )
    assert totals is not None
    assert totals == {Ticker("XQFA"): Decimal("1.5")}
    assert to_whole_rupees(totals[Ticker("XQFA")]) == 2
    assert zeroed is False


def test_a3_dividends_keep_ordinary_values() -> None:
    """Preservation: accepted cent payments at a TTBR keep their values and
    exponents (#1168)."""
    payments = [_payment("5"), _payment("1.25", symbol="XQFB"), _payment("2")]
    totals, zeroed = resolve_fa_a3_dividends(
        payments, calendar_year=2025, fx_policy=_TTBR, dividend_evidence=DividendEvidence.PRESENT
    )
    assert totals is not None
    assert totals[Ticker("XQFA")].compare_total(Decimal("598.29")) == 0  # 7 x 85.47
    assert totals[Ticker("XQFB")].compare_total(Decimal("106.8375")) == 0  # 1.25 x 85.47
    assert zeroed is False


@pytest.mark.parametrize("evidence", list(DividendEvidence))
def test_a3_dividends_propagate_a_cap_refusal(
    evidence: DividendEvidence, capsys: pytest.CaptureFixture[str]
) -> None:
    """Whatever the dividend evidence, a cap failure raises; it never becomes
    ``None`` (manual fill) or a proven zero (#1168)."""
    payments = [_marked_payment(), _payment(HUGE), _payment(MINUTE, source_row_index=11)]
    with pytest.raises(InvariantError) as raised:
        resolve_fa_a3_dividends(
            payments, calendar_year=2025, fx_policy=_FX_ONE, dividend_evidence=evidence
        )
    _refused(raised, capsys, _DIVIDENDS_AT.format(3), source_row_index=11)


# ---- resolve_espp_dec31_positions --------------------------------------------


@pytest.fixture
def netbenefits(tmp_path: Path) -> NetBenefitsConfig:
    """The ESPP compute tests' synthetic config: a 10% discount and a
    2025-12-31 override."""
    path = tmp_path / "user.toml"
    path.write_text(ESPP_CONFIG, encoding="utf-8")
    config = load_user_config(path).netbenefits
    assert config is not None
    return config


def _resolve_one_share_lots(
    cfg: NetBenefitsConfig, open_rows: Sequence[int], **purchase: object
) -> EsppDec31Positions:
    """One-share Dec-31 lots at the confirmation ``purchase`` describes, all
    reconciled to that one confirmation."""
    confirmation = _purchase(shares=Decimal("1"), **purchase)
    lot = _open(
        quantity=Decimal("1"),
        contribution_usd=confirmation.contribution_usd,
        cost_per_share_paid=confirmation.purchase_price_per_share_usd,
    )
    return resolve_espp_dec31_positions(
        [EsppDec31Lot(lot, open_row=row) for row in open_rows],
        {confirmation.purchase_date: confirmation},
        cfg=cfg,
        calendar_year=2025,
        series=None,
        peak_basis="close",
        prices_fetched=False,
    )


def test_espp_securities_add_two_lots_exactly(netbenefits: NetBenefitsConfig) -> None:
    """Two one-share lots priced by a 28-digit override add to the exact
    19.999999999999999999999999998; main rounded it to 20 (#1168)."""
    (override,) = netbenefits.price_overrides
    close = replace(override, price_per_share_usd=Decimal("9.999999999999999999999999999"))
    resolved = _resolve_one_share_lots(
        replace(netbenefits, price_overrides=(close,)),
        [1, 2],
        purchase_price_per_share_usd=Decimal("9.00"),
        fmv_total_usd=Decimal("10.00"),
        contribution_usd=Decimal("9.00"),
        perquisite_usd=Decimal("1.00"),
    )
    assert len(resolved.positions) == 2
    expected = Decimal("19.999999999999999999999999998")
    assert resolved.securities_usd.compare_total(expected) == 0


def _marked_resolution(
    cfg: NetBenefitsConfig, monkeypatch: pytest.MonkeyPatch, closes: Iterable[Decimal]
) -> EsppDec31Positions:
    """Resolve lots at open-lots rows 1 and 4 against a marked confirmation
    with an FMV of 1.23, each closing at the next of ``closes``."""
    totals = iter(closes)

    def resolve_close(lot, cfg, valuation_date, series, **_kwargs) -> ResolvedClose:
        return ResolvedClose(
            total_usd=next(totals),
            per_share_usd=None,
            source=CloseSource.STATEMENT,
            provenance=SOURCE,
        )

    monkeypatch.setattr("ibkr_fa.compute.espp_positions._resolve_close", resolve_close)
    instrument = replace(cfg.security.instrument, symbol=Ticker(SYMBOL))
    return _resolve_one_share_lots(
        replace(cfg, security=replace(cfg.security, instrument=instrument)),
        [1, 4],
        symbol=SYMBOL,
        security_description=DESCRIPTION,
        offering_period_start=MARKED_DATE,
        purchase_price_per_share_usd=Decimal("1.107"),
        fmv_total_usd=Decimal("1.23"),
        contribution_usd=Decimal("1.107"),
        perquisite_usd=Decimal("0.123"),
    )


def _long_close(extra: str = "0") -> Decimal:
    """1.23 + ``extra`` + 1E-1000: over 1000 digits added to ``NUMERIC``."""
    with localcontext(Context(prec=1200)):
        return Decimal("1.23") + Decimal(extra) + Decimal("1E-1000")


def test_espp_securities_past_the_cap_refuse_at_their_lot(
    netbenefits: NetBenefitsConfig,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Every lot reconciles to the one Dec-31 confirmation, so real lots share
    a quantity and a close and their 28-digit totals are equal: their sum
    needs at most one more digit and never reaches the cap. So the close
    resolver is replaced with closes that pass the FMV anchor, ``NUMERIC``
    and 1.23 + 1E-1000, whose sum needs 1001 digits. A statement-style close
    (no per-share price) keeps the long total off the valuation's per-share
    check. Lot 2 is open-lots data row 4: lots count by input position (#1168)."""
    with pytest.raises(InvariantError) as raised:
        _marked_resolution(netbenefits, monkeypatch, [Decimal(NUMERIC), _long_close()])
    _refused(raised, capsys, _SECURITIES_AT.format(2), markers=_ESPP_MARKERS)


@pytest.mark.parametrize(
    ("discount", "error", "expected"),
    [
        pytest.param(
            "10",
            ClosingPriceError,
            "open-lots data row 4: the Dec-31 close (statement) differs from the Market Value "
            "at Purchase on confirmation 1 (by purchase date) by more than the tolerance; "
            f"{NB_BY_HAND}",
            id="anchor",
        ),
        pytest.param(
            "25",
            NetBenefitsFaError,
            "confirmation 1 (by purchase date): the purchase price is not the FMV per share "
            "less the netbenefits.espp_discount_pct discount, within the rounding tolerance",
            id="discount",
        ),
    ],
)
def test_espp_anchor_and_discount_refusals_still_fire_first(
    netbenefits: NetBenefitsConfig,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    discount: str,
    error: type[Exception],
    expected: str,
) -> None:
    """Preservation: lot 2's close is fifty cents off the FMV, and adding it
    to lot 1 would exceed the cap; the anchor refuses it before the addition.
    Since #1250 the anchor difference is exact, so it must fit the cap
    itself: 0.5 + 1E-1000 needs 1000 digits, while the sum with ``NUMERIC``
    needs 1001. A broken plan discount refuses lot 1 before its close is
    resolved (#1168)."""
    cfg = replace(netbenefits, espp_discount_pct=Decimal(discount))
    with pytest.raises(error) as raised:
        _marked_resolution(cfg, monkeypatch, [Decimal(NUMERIC), _long_close("0.5")])
    assert str(raised.value) == expected
    captured = capsys.readouterr()
    for marker in _ESPP_MARKERS:
        assert marker not in str(raised.value) + captured.out + captured.err, marker
