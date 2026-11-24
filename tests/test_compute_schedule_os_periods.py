"""Tests for the Schedule OS section 10 dividend period split (#614)."""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import date
from decimal import ROUND_HALF_UP, Decimal

import pytest

from ibkr_fa.compute.exact_inr import exact_inr_sum
from ibkr_fa.compute.schedule_os import (
    ScheduleOsAccountSplit,
    ScheduleOsComputeResult,
    ScheduleOsDividendLine,
    ScheduleOsDomesticDividendLine,
    ScheduleOsSecuritySplit,
)
from ibkr_fa.compute.schedule_os_other_income import ScheduleOsOtherIncomeLine
from ibkr_fa.compute.schedule_os_periods import (
    ScheduleOsPeriodAmount,
    accrual_period_index,
    allocate_period_rupees,
    split_dividend_periods,
    split_whole_rupees,
)
from ibkr_fa.exceptions import InvariantError
from ibkr_fa.models import DistributionKind
from ibkr_fa.types import Isin, Ticker
from tests._exact_refusals import INEXACT_SUM


@dataclass(frozen=True)
class _Line:
    payment_date: date
    amount_gross_inr: Decimal


_DIVIDEND_SUM_REFUSAL = f"InvariantError: Schedule OS dividend INR {INEXACT_SUM}"
# Four periods without a dividend, as ``_shown`` renders them.
_EMPTY_PERIODS = [("0", "0")] * 4


def _whole(lines: list[_Line]) -> list[Decimal]:
    return [period.whole_rupees for period in split_dividend_periods(lines)]


def _shown(value: Decimal | tuple[ScheduleOsPeriodAmount, ...]) -> object:
    """``value`` as strings, so every exponent shows."""
    if isinstance(value, Decimal):
        return str(value)
    return [(str(period.exact_inr), str(period.whole_rupees)) for period in value]


def _dividend(paid: date, inr: str) -> ScheduleOsDividendLine:
    return ScheduleOsDividendLine(
        symbol=Ticker("XQCA"),
        isin=Isin("US99972B1017"),
        issuer_country_iso2="US",
        payment_date=paid,
        fx_specified_date=paid,
        fx_rate_publication_date=paid,
        fx_rate_inr_per_usd=Decimal("80"),
        amount_gross_usd=Decimal(inr) / Decimal("80"),
        amount_gross_inr=Decimal(inr),
        description="XQCA(US99972B1017) Cash Dividend USD 0.83 per Share",
        kind=DistributionKind.CASH_DIVIDEND,
    )


def _held(
    symbol: str,
    isin: str,
    country: str,
    inr: str,
    *,
    account: int | None = None,
    paid: date = date(2025, 5, 1),
) -> ScheduleOsDividendLine:
    return replace(
        _dividend(paid, inr),
        symbol=Ticker(symbol),
        isin=Isin(isin),
        issuer_country_iso2=country,
        description=f"{symbol}({isin}) Cash Dividend USD 0.83 per Share",
        source_account_index=account,
    )


def _domestic(
    symbol: str, isin: str, inr: str, *, account: int | None = None
) -> ScheduleOsDomesticDividendLine:
    paid = date(2025, 5, 1)
    return ScheduleOsDomesticDividendLine(
        symbol=Ticker(symbol),
        isin=Isin(isin),
        issuer_country_iso2="IN",
        payment_date=paid,
        fx_specified_date=paid,
        fx_rate_publication_date=paid,
        fx_rate_inr_per_usd=Decimal("80"),
        amount_gross_usd=Decimal(inr) / Decimal("80"),
        amount_gross_inr=Decimal(inr),
        description=f"{symbol}({isin}) Cash Dividend USD 0.10 per Share",
        kind=DistributionKind.CASH_DIVIDEND,
        source_account_index=account,
    )


def _os_result(
    lines: list[ScheduleOsDividendLine],
    domestic_lines: list[ScheduleOsDomesticDividendLine] | None = None,
) -> ScheduleOsComputeResult:
    return ScheduleOsComputeResult(lines=lines, domestic_lines=domestic_lines or [])


def _other(nature: str, inr: str) -> ScheduleOsOtherIncomeLine:
    return ScheduleOsOtherIncomeLine(
        nature=nature,
        payment_date=date(2025, 6, 1),
        fx_specified_date=date(2026, 3, 31),
        fx_rate_publication_date=date(2026, 3, 31),
        fx_rate_inr_per_usd=Decimal("80"),
        amount_gross_usd=Decimal(inr) / Decimal("80"),
        amount_gross_inr=Decimal(inr),
        description=nature,
        source_row_index=None,
    )


@pytest.mark.parametrize(
    ("payment_date", "expected"),
    [
        (date(2025, 4, 1), 0),
        (date(2025, 6, 15), 0),
        (date(2025, 6, 16), 1),
        (date(2025, 9, 15), 1),
        (date(2025, 9, 16), 2),
        (date(2025, 12, 15), 2),
        (date(2025, 12, 16), 3),
        (date(2026, 3, 15), 3),
        (date(2026, 3, 16), 4),
        (date(2026, 3, 31), 4),
    ],
)
def test_dividend_period_index_boundaries(payment_date: date, expected: int) -> None:
    assert accrual_period_index(payment_date) == expected


def test_empty_input_gives_five_zero_periods_and_zero_total() -> None:
    zero = ScheduleOsPeriodAmount(exact_inr=Decimal("0"), whole_rupees=Decimal("0"))
    assert split_dividend_periods([]) == (zero,) * 5
    result = ScheduleOsComputeResult(lines=[], domestic_lines=[])
    assert result.ordinary_dividend_total_inr_rounded == Decimal("0")


def test_remainder_tie_goes_to_lower_period() -> None:
    # Periods 2 and 1 both end in .5; the one leftover rupee goes to period 1.
    lines = [
        _Line(date(2025, 10, 1), Decimal("1.5")),
        _Line(date(2025, 7, 1), Decimal("1.5")),
    ]
    assert _whole(lines) == [0, 2, 1, 0, 0]


@pytest.mark.parametrize(
    "amounts",
    [
        # Exactly .5 in total: HALF_UP rounds the 1a(i) total up to 1.
        ("0.25", "0.25"),
        ("0.4", "0.4", "0.4"),
        ("10.49", "20.49", "30.49", "40.49", "50.49"),
        ("132.5817", "271.6942", "98.5163", "229.6815", "18.4528"),
    ],
)
def test_whole_rupees_sum_to_half_up_total(amounts: tuple[str, ...]) -> None:
    paid = (
        date(2025, 5, 1),
        date(2025, 8, 1),
        date(2025, 11, 1),
        date(2026, 2, 1),
        date(2026, 3, 20),
    )
    lines = [_Line(paid[index], Decimal(amount)) for index, amount in enumerate(amounts)]
    total = sum((Decimal(amount) for amount in amounts), Decimal("0"))
    assert sum(_whole(lines)) == total.quantize(Decimal("1"), rounding=ROUND_HALF_UP)


def test_exactly_half_rupee_total_rounds_up() -> None:
    lines = [_Line(date(2025, 5, 1), Decimal("0.25")), _Line(date(2025, 8, 1), Decimal("0.25"))]
    assert _whole(lines) == [1, 0, 0, 0, 0]


def test_long_sum_keeps_every_digit() -> None:
    """#1312: 1 + 4E-28 needs 29 digits; the sum keeps them all. Main refused it."""
    lines = [_Line(date(2025, 4, 3), Decimal("1")), _Line(date(2025, 4, 1), Decimal("4E-28"))]
    assert _shown(split_dividend_periods(lines)) == [
        ("1.0000000000000000000000000004", "1"),
        *_EMPTY_PERIODS,
    ]
    exact = exact_inr_sum([Decimal("1"), Decimal("4E-28")], label="Schedule OS dividend")
    assert exact.compare_total(Decimal("1.0000000000000000000000000004")) == 0


def test_sum_past_the_digit_cap_refuses() -> None:
    """#1312: 1 + 1E-1000 needs 1,001 digits, past ``EXACT_SUM_DIGITS``, and still refuses."""
    lines = [_Line(date(2025, 4, 3), Decimal("1")), _Line(date(2025, 4, 1), Decimal("1E-1000"))]
    with pytest.raises(InvariantError) as raised:
        split_dividend_periods(lines)
    assert str(raised.value) == _DIVIDEND_SUM_REFUSAL
    assert raised.value.__cause__ is None
    assert raised.value.__context__ is None
    with pytest.raises(InvariantError) as raised:
        exact_inr_sum([Decimal("1"), Decimal("1E-1000")], label="Schedule OS dividend")
    assert str(raised.value) == _DIVIDEND_SUM_REFUSAL
    assert raised.value.__cause__ is None
    assert raised.value.__context__ is None


def test_exact_inr_sum_refuses_negative_amounts() -> None:
    with pytest.raises(InvariantError, match="must not be negative"):
        exact_inr_sum([Decimal("1"), Decimal("-0.5")], label="Schedule OS dividend")


@pytest.mark.parametrize(
    ("property_name", "expected"),
    [
        pytest.param(
            "dividend_periods",
            [("1.5000000000000000000000000000", "2"), *_EMPTY_PERIODS],
            id="dividend_periods",
        ),
        pytest.param(
            "ordinary_dividend_total_inr_full",
            "1.5000000000000000000000000000",
            id="ordinary_dividend_total_inr_full",
        ),
        pytest.param(
            "ordinary_dividend_total_inr_rounded", "2", id="ordinary_dividend_total_inr_rounded"
        ),
    ],
)
def test_result_keeps_every_digit_of_the_sum(property_name: str, expected: object) -> None:
    """#1312: the sum is exactly 1.50 with its 29th digit, filed as ₹2. Main refused
    it: at 28 digits only a zero digit is dropped in input order, but the old
    review writers summed the large line first, which rounds away every 4E-28
    and files ₹1."""
    lines = [
        *[_dividend(date(2025, 4, 2), "4E-28") for _ in range(5)],
        _dividend(date(2025, 4, 1), "1.499999999999999999999999998"),
    ]
    result = ScheduleOsComputeResult(lines=lines, domestic_lines=[])
    assert _shown(getattr(result, property_name)) == expected


@pytest.mark.parametrize(
    "property_name",
    ("dividend_periods", "ordinary_dividend_total_inr_full", "ordinary_dividend_total_inr_rounded"),
)
def test_result_refuses_a_sum_past_the_digit_cap(property_name: str) -> None:
    """#1312: a 1a(i) sum that needs 1,001 digits still refuses."""
    lines = [_dividend(date(2025, 4, 2), "1E-1000"), _dividend(date(2025, 4, 1), "1")]
    result = ScheduleOsComputeResult(lines=lines, domestic_lines=[])
    with pytest.raises(InvariantError) as raised:
        getattr(result, property_name)
    assert str(raised.value) == _DIVIDEND_SUM_REFUSAL
    assert raised.value.__cause__ is None
    assert raised.value.__context__ is None


def test_allocate_period_rupees_matches_every_period_target() -> None:
    groups = [
        [_Line(date(2025, 5, 1), Decimal("100.6")), _Line(date(2025, 8, 1), Decimal("0.5"))],
        [_Line(date(2025, 5, 2), Decimal("40.9")), _Line(date(2025, 8, 2), Decimal("0.5"))],
        [_Line(date(2026, 3, 20), Decimal("12.4"))],
    ]
    targets = _whole([line for group in groups for line in group])
    allocated = allocate_period_rupees(groups, period_targets=targets)
    assert allocated == ((101, 1, 0, 0, 0), (41, 0, 0, 0, 0), (0, 0, 0, 0, 12))
    for period_index, target in enumerate(targets):
        assert sum(group[period_index] for group in allocated) == target


@pytest.mark.parametrize(
    "period_targets",
    [
        # Three rupees above the floors (100 + 40), but only two groups.
        [Decimal("143"), Decimal("0"), Decimal("0"), Decimal("0"), Decimal("0")],
        # Below the floors already given out.
        [Decimal("139"), Decimal("0"), Decimal("0"), Decimal("0"), Decimal("0")],
    ],
)
def test_allocate_period_rupees_refuses_a_target_that_cannot_fit(
    period_targets: list[Decimal],
) -> None:
    groups = [
        [_Line(date(2025, 5, 1), Decimal("100.6"))],
        [_Line(date(2025, 5, 2), Decimal("40.9"))],
    ]
    with pytest.raises(InvariantError, match="must fit the emitted dividend groups"):
        allocate_period_rupees(groups, period_targets=period_targets)


@pytest.mark.parametrize(
    "period_targets",
    [
        [Decimal("0.5"), Decimal("0"), Decimal("0"), Decimal("0"), Decimal("0")],
        [Decimal("NaN"), Decimal("0"), Decimal("0"), Decimal("0"), Decimal("0")],
        [Decimal("0"), Decimal("0"), Decimal("0"), Decimal("0")],
    ],
)
def test_allocate_period_rupees_refuses_targets_that_are_not_five_whole_rupees(
    period_targets: list[Decimal],
) -> None:
    with pytest.raises(InvariantError, match="five whole-rupee period targets"):
        allocate_period_rupees([], period_targets=period_targets)


def test_result_values_follow_dataclasses_replace() -> None:
    result = ScheduleOsComputeResult(
        lines=[_dividend(date(2025, 5, 1), "100.6")],
        domestic_lines=[],
        other_income_lines=(_other("Securities lending fee", "8.8"),),
    )
    assert result.ordinary_dividend_total_inr_full == Decimal("100.6")
    assert result.ordinary_dividend_total_inr_rounded == Decimal("101")
    assert result.other_income_totals == (("Securities lending fee", Decimal("8.8"), Decimal("9")),)

    replaced = replace(
        result,
        lines=[_dividend(date(2025, 5, 1), "100.6"), _dividend(date(2025, 8, 1), "50.2")],
        other_income_lines=(
            _other("Substitute payment in lieu of dividend", "23.5"),
            _other("Securities lending fee", "8.8"),
        ),
    )
    assert replaced.ordinary_dividend_total_inr_full == Decimal("150.8")
    assert replaced.ordinary_dividend_total_inr_rounded == Decimal("151")
    assert [period.whole_rupees for period in replaced.dividend_periods] == [101, 50, 0, 0, 0]
    assert replaced.other_income_totals == (
        ("Securities lending fee", Decimal("8.8"), Decimal("9")),
        ("Substitute payment in lieu of dividend", Decimal("23.5"), Decimal("24")),
    )


def _assert_splits_reconcile(
    result: ScheduleOsComputeResult,
    splits: tuple[ScheduleOsSecuritySplit, ...] | tuple[ScheduleOsAccountSplit, ...],
) -> None:
    for period_index, period in enumerate(result.dividend_periods):
        assert sum(split.period_whole_rupees[period_index] for split in splits) == (
            period.whole_rupees
        )
    assert sum(split.total_whole_rupees for split in splits) == (
        result.ordinary_dividend_total_inr_rounded
    )


def test_security_splits_sort_by_country_symbol_isin_with_one_domestic_split_last() -> None:
    result = _os_result(
        [
            _held("XQCA", "US99972B1017", "US", "100", paid=date(2026, 3, 20)),
            _held("ZZZ", "US99992A1097", "US", "0.5"),
            _held("ZZZ", "US99972B1017", "US", "0.5"),
            _held("XQCA", "US99972B1017", "US", "20", paid=date(2025, 8, 1)),
            _held("XQCA", "US99972B1017", "IE", "5"),
            _held("XQAE", "IE99970S1064", "IE", "10.6"),
        ],
        [_domestic("XQCD", "IN99970W1077", "3.3"), _domestic("XQCG", "IN99970X1050", "4")],
    )

    # Period 1 is 0.5 + 0.5 + 10.6 + 5 + 3.3 + 4 = 23.9, so 24 rupees: the floors
    # give 22, then the largest remainders (XQAE's .6, then the earlier ZZZ of
    # the tied .5s) take the other two; the domestic .3 gets none.
    assert result.dividend_splits_by_security == (
        ScheduleOsSecuritySplit("IE", Ticker("XQAE"), Isin("IE99970S1064"), (11, 0, 0, 0, 0)),
        ScheduleOsSecuritySplit("IE", Ticker("XQCA"), Isin("US99972B1017"), (5, 0, 0, 0, 0)),
        ScheduleOsSecuritySplit("US", Ticker("XQCA"), Isin("US99972B1017"), (0, 20, 0, 0, 100)),
        ScheduleOsSecuritySplit("US", Ticker("ZZZ"), Isin("US99972B1017"), (1, 0, 0, 0, 0)),
        ScheduleOsSecuritySplit("US", Ticker("ZZZ"), Isin("US99992A1097"), (0, 0, 0, 0, 0)),
        ScheduleOsSecuritySplit("IN", None, None, (7, 0, 0, 0, 0)),
    )
    assert [split.total_whole_rupees for split in result.dividend_splits_by_security] == [
        11,
        5,
        120,
        1,
        0,
        7,
    ]
    _assert_splits_reconcile(result, result.dividend_splits_by_security)


def test_splits_of_a_result_without_dividends_are_empty() -> None:
    result = _os_result([])
    assert result.dividend_splits_by_security == ()
    assert result.dividend_splits_by_account == ()


def test_one_security_in_two_accounts_splits_differently_by_account() -> None:
    result = _os_result(
        [
            _held("XQCA", "US99972B1017", "US", "0.49", account=1),
            _held("XQCA", "US99972B1017", "US", "0.49", account=0),
        ]
    )

    assert result.dividend_splits_by_security == (
        ScheduleOsSecuritySplit("US", Ticker("XQCA"), Isin("US99972B1017"), (1, 0, 0, 0, 0)),
    )
    assert result.dividend_splits_by_account == (
        ScheduleOsAccountSplit(0, "US", (1, 0, 0, 0, 0)),
        ScheduleOsAccountSplit(1, "US", (0, 0, 0, 0, 0)),
    )
    assert [split.total_whole_rupees for split in result.dividend_splits_by_account] == [1, 0]


def test_account_splits_group_by_account_then_country_domestic_included() -> None:
    result = _os_result(
        [
            _held("XQCA", "US99972B1017", "US", "40.4", account=1),
            _held("XQAE", "IE99970S1064", "IE", "10.6", account=1),
            _held("XQAQ", "US99992A1097", "US", "5.5", account=0),
            _held("XQCA", "US99972B1017", "US", "2.2", account=0, paid=date(2025, 8, 1)),
        ],
        [_domestic("XQCD", "IN99970W1077", "3.3", account=1)],
    )

    assert result.dividend_splits_by_account == (
        ScheduleOsAccountSplit(0, "US", (6, 2, 0, 0, 0)),
        ScheduleOsAccountSplit(1, "IE", (11, 0, 0, 0, 0)),
        ScheduleOsAccountSplit(1, "IN", (3, 0, 0, 0, 0)),
        ScheduleOsAccountSplit(1, "US", (40, 0, 0, 0, 0)),
    )
    _assert_splits_reconcile(result, result.dividend_splits_by_account)


def test_account_splits_are_empty_without_account_metadata() -> None:
    result = _os_result(
        [_held("XQCA", "US99972B1017", "US", "1.5")],
        [_domestic("XQCD", "IN99970W1077", "3.3")],
    )
    assert result.dividend_splits_by_account == ()


@pytest.mark.parametrize("untagged", ["foreign", "domestic"])
def test_account_splits_refuse_partial_account_metadata(untagged: str) -> None:
    result = _os_result(
        [_held("XQCA", "US99972B1017", "US", "1.5", account=None if untagged == "foreign" else 0)],
        [_domestic("XQCD", "IN99970W1077", "3.3", account=None if untagged == "domestic" else 0)],
    )
    with pytest.raises(InvariantError, match="account metadata on every dividend line"):
        _ = result.dividend_splits_by_account


def test_splits_follow_dataclasses_replace() -> None:
    result = _os_result([_held("XQCA", "US99972B1017", "US", "0.49", account=0)])
    assert result.dividend_splits_by_security[0].period_whole_rupees == (0, 0, 0, 0, 0)
    assert result.dividend_splits_by_account == (ScheduleOsAccountSplit(0, "US", (0, 0, 0, 0, 0)),)

    replaced = replace(
        result,
        lines=[*result.lines, _held("XQCA", "US99972B1017", "US", "0.49", account=1)],
    )
    assert replaced.dividend_splits_by_security == (
        ScheduleOsSecuritySplit("US", Ticker("XQCA"), Isin("US99972B1017"), (1, 0, 0, 0, 0)),
    )
    assert replaced.dividend_splits_by_account == (
        ScheduleOsAccountSplit(0, "US", (1, 0, 0, 0, 0)),
        ScheduleOsAccountSplit(1, "US", (0, 0, 0, 0, 0)),
    )


# ---- the shared whole-rupee split (#1319) ------------------------------------


@pytest.mark.parametrize(
    ("amounts", "target", "shares"),
    [
        # Floors 0 + 0 + 2; the remaining rupees go to the largest remainders.
        (["0.6", "0.7", "2.2"], "3", ["0", "1", "2"]),
        (["0.6", "0.7", "2.2"], "4", ["1", "1", "2"]),
        # A tie goes to the earlier amount.
        (["0.5", "0.5"], "1", ["1", "0"]),
        # Nothing to allocate: every share is its floor.
        (["1.4", "2.4"], "3", ["1", "2"]),
        # One amount takes the whole target.
        (["23.5105"], "24", ["24"]),
    ],
)
def test_split_whole_rupees_floors_then_gives_the_largest_remainders(
    amounts: list[str], target: str, shares: list[str]
) -> None:
    split = split_whole_rupees([Decimal(a) for a in amounts], target=Decimal(target), what="x")

    assert split == tuple(Decimal(share) for share in shares)
    assert sum(split, Decimal(0)) == Decimal(target)


@pytest.mark.parametrize("target", ["0", "4"], ids=["below-the-floors", "three-above-them"])
def test_split_whole_rupees_refuses_a_target_its_amounts_cannot_take(target: str) -> None:
    with pytest.raises(InvariantError) as exc:
        split_whole_rupees([Decimal("0.5"), Decimal("1.5")], target=Decimal(target), what="SPLIT")

    assert exc.value.what == "SPLIT"
    assert exc.value.context == (("rupees_to_allocate", None),)


@pytest.mark.parametrize(
    "amounts",
    [
        pytest.param(["1.5", "2.50000000000000000000000000001"], id="29th-digit"),
        pytest.param(["1.5", "2.5" + "0" * 997 + "1"], id="999th-digit"),
    ],
)
def test_split_whole_rupees_gives_the_rupee_to_the_exactly_larger_remainder(
    amounts: list[str],
) -> None:
    """#1312: the second remainder is larger only past its 28th digit. Exact
    remainders give it the one rupee; main's 28-digit remainders tied and gave
    it to the earlier amount."""
    split = split_whole_rupees([Decimal(a) for a in amounts], target=Decimal("4"), what="x")

    assert split == (Decimal("1"), Decimal("3"))


def test_result_period_takes_the_rupee_for_the_exactly_larger_remainder() -> None:
    """#1312: the exact 1a(i) total ₹1.00000000000000000000000000001 files ₹1, which
    goes to the second period, whose remainder is larger only past its 28th digit.
    Main refused the 29-digit sum; 28-digit remainders would have tied and given
    the rupee to the first period."""
    result = _os_result(
        [
            _dividend(date(2025, 5, 1), "0.5"),
            _dividend(date(2025, 8, 1), "0.50000000000000000000000000001"),
        ]
    )

    assert [period.whole_rupees for period in result.dividend_periods] == [0, 1, 0, 0, 0]
    assert result.ordinary_dividend_total_inr_rounded == Decimal("1")
