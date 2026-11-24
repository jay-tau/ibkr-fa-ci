"""Tests for the NetBenefits income normalizer
(:mod:`ibkr_fa.normalize.netbenefits.income`): in-FY event collection, dedup
across overlapping statements, withholding matching, and every refusal.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import date
from decimal import Decimal

import pytest

from ibkr_fa.models import (
    Dividend,
    NetBenefitsSourceRecord,
    WithholdingTax,
)
from ibkr_fa.models.distribution_kind import DistributionKind
from ibkr_fa.normalize.netbenefits.income import normalize_income
from ibkr_fa.parser.netbenefits.bundle import NetBenefitsBundleError
from ibkr_fa.parser.netbenefits.statement_pdf import (
    StatementData,
    StatementDividend,
    StatementWithholding,
)
from ibkr_fa.types import Isin, Ticker
from tests._crash_reports import assert_no_markers
from tests._netbenefits_income import CORE_FUND_CONFIG_REFUSAL as _CORE_FUND_CONFIG_REFUSAL
from tests._netbenefits_income import COVER_FROM_CORRECTED_DATE as _COVER_FROM_CORRECTED_DATE
from tests._netbenefits_income import DIVIDENDS_TABLE as _DIVIDENDS_TABLE
from tests._netbenefits_income import FUND_DIV as _FUND_DIV
from tests._netbenefits_income import FUND_WHT as _FUND_WHT
from tests._netbenefits_income import MARCH_DIV as _DIV
from tests._netbenefits_income import MARCH_WHT as _WHT
from tests._netbenefits_income import NO_DIVIDEND_ON_ITS_DATE as _NO_DIVIDEND_ON_ITS_DATE
from tests._netbenefits_income import TAXES_TABLE as _TAXES_TABLE
from tests._netbenefits_income import event_at as _event_at
from tests._netbenefits_income import income_statement as _statement
from tests._netbenefits_income import missing_event_refusal as _missing_event_refusal
from tests._netbenefits_income import nth_statement as _nth_statement

# #1042: synthetic values a statement row can carry. The refusals used to
# print them; none may reach a refusal now (compared case-insensitively, and
# ``ANN`` as a whole token).
_HOLDER = "Synthetic Holder Two"
_ACCOUNT = "U1234567"
_OTHER_CUSIP = "99993A108"
_DAY = date(2025, 11, 14)  # in the FY and after the account opening
_DAY_TEXT = ("2025-11-14", "11/14/2025")
_OPENING_TEXT = "2025-11-01"  # account_opening_date in _normalize
_AMOUNT, _OTHER_AMOUNT = Decimal("1234567.89"), Decimal("7654321.98")
_AMOUNT_TEXT = ("1234567.89", "1,234,567.89", "7654321.98", "7,654,321.98")


def _normalize(
    statements: list[StatementData], *, security_cusip: str = "99999E104"
) -> NetBenefitsSourceRecord:
    return normalize_income(
        statements,
        security_cusip=security_cusip,
        security_symbol=Ticker("XESP"),
        security_isin=Isin("US99999E1047"),
        account_opening_date=date(2025, 11, 1),
        fy_start=date(2025, 4, 1),
        fy_end=date(2026, 3, 31),
    )


def _records(
    statements: list[StatementData],
) -> tuple[tuple[Dividend, ...], tuple[WithholdingTax, ...]]:
    record = _normalize(statements)
    return record.dividends, record.withholding_tax


def _listing(start: date, end: date, kind: str, rows: tuple[object, ...]) -> StatementData:
    """A statement over ``start``..``end`` whose ``kind`` table lists ``rows``."""
    return replace(_statement(start, end), **{f"{kind}s": rows})


def test_normalize_income_builds_the_record() -> None:
    # The stock's and the core fund's events keep their own identities, across
    # two statements.
    stmts = [
        _statement(date(2025, 11, 1), date(2026, 1, 31)),
        _statement(
            date(2026, 2, 1),
            date(2026, 3, 31),
            dividends=(_DIV, _FUND_DIV),
            withholdings=(_WHT, _FUND_WHT),
        ),
    ]

    record = _normalize(stmts)

    assert record == NetBenefitsSourceRecord(
        dividends=(
            Dividend(
                symbol=Ticker("XESP"),
                isin=Isin("US99999E1047"),
                payment_date=date(2026, 3, 5),
                amount_gross=Decimal("1.46"),
                currency="USD",
                description="XESP(US99999E1047) Cash Dividend",
                kind=DistributionKind.CASH_DIVIDEND,
            ),
            Dividend(
                symbol=Ticker("FYIXX"),
                isin=Isin("US31617H8218"),
                payment_date=date(2026, 3, 31),
                amount_gross=Decimal("1.12"),
                currency="USD",
                description="FYIXX(US31617H8218) Cash Dividend",
                kind=DistributionKind.CASH_DIVIDEND,
            ),
        ),
        withholding_tax=(
            WithholdingTax(
                symbol=Ticker("XESP"),
                isin=Isin("US99999E1047"),
                payment_date=date(2026, 3, 5),
                amount=Decimal("-0.36"),
                currency="USD",
                description="XESP(US99999E1047) Withholding Tax",
            ),
            WithholdingTax(
                symbol=Ticker("FYIXX"),
                isin=Isin("US31617H8218"),
                payment_date=date(2026, 3, 31),
                amount=Decimal("-0.28"),
                currency="USD",
                description="FYIXX(US31617H8218) Withholding Tax",
            ),
        ),
    )


def test_same_date_withholdings_take_their_own_dividends_identity() -> None:
    """The stock and the core fund pay on one day. Each withholding takes the
    identity of the dividend with its own security name, not the other one paid
    that day. The tables list them in opposite orders, so pairing by position
    or by the first dividend that day mislabels one."""
    fund_dividend = replace(_FUND_DIV, date=_DIV.date)
    fund_withholding = replace(_FUND_WHT, date=_WHT.date)
    stmt = _statement(
        date(2026, 2, 1),
        date(2026, 3, 31),
        dividends=(_DIV, fund_dividend),
        withholdings=(fund_withholding, _WHT),
    )

    record = _normalize([stmt])

    assert record == NetBenefitsSourceRecord(
        dividends=(
            Dividend(
                symbol=Ticker("XESP"),
                isin=Isin("US99999E1047"),
                payment_date=date(2026, 3, 5),
                amount_gross=Decimal("1.46"),
                currency="USD",
                description="XESP(US99999E1047) Cash Dividend",
                kind=DistributionKind.CASH_DIVIDEND,
            ),
            Dividend(
                symbol=Ticker("FYIXX"),
                isin=Isin("US31617H8218"),
                payment_date=date(2026, 3, 5),
                amount_gross=Decimal("1.12"),
                currency="USD",
                description="FYIXX(US31617H8218) Cash Dividend",
                kind=DistributionKind.CASH_DIVIDEND,
            ),
        ),
        withholding_tax=(
            WithholdingTax(
                symbol=Ticker("FYIXX"),
                isin=Isin("US31617H8218"),
                payment_date=date(2026, 3, 5),
                amount=Decimal("-0.28"),
                currency="USD",
                description="FYIXX(US31617H8218) Withholding Tax",
            ),
            WithholdingTax(
                symbol=Ticker("XESP"),
                isin=Isin("US99999E1047"),
                payment_date=date(2026, 3, 5),
                amount=Decimal("-0.36"),
                currency="USD",
                description="XESP(US99999E1047) Withholding Tax",
            ),
        ),
    )


def test_normalize_income_refuses_the_core_fund_as_the_security() -> None:
    with pytest.raises(NetBenefitsBundleError) as caught:
        _normalize([], security_cusip="31617H821")
    assert str(caught.value) == _CORE_FUND_CONFIG_REFUSAL
    assert_no_markers(str(caught.value), "31617H821")


def test_normalize_income_refuses_a_third_security_dividend() -> None:
    other = StatementDividend(_DAY, "SAMPLE ISSUER BN INC", _OTHER_CUSIP, Decimal("1.00"))
    stmt = _statement(date(2025, 4, 1), date(2026, 3, 31), dividends=(other, _DIV))
    with pytest.raises(NetBenefitsBundleError) as caught:
        _normalize([stmt])
    assert str(caught.value) == (
        "the in-FY dividend at statement 1 (by period start, year-end reports excluded), row 1 "
        "of its Dividends, Interest & Other Income table is for a CUSIP that is neither "
        "netbenefits.security.cusip nor the core money-market fund"
    )
    # Neither the row's CUSIP nor the configured or core-fund one is printed.
    assert_no_markers(str(caught.value), _OTHER_CUSIP, "99999E104", "31617H821", *_DAY_TEXT)


def test_normalize_income_names_the_row_of_a_later_third_security_dividend() -> None:
    other = StatementDividend(_DAY, "SAMPLE ISSUER BN INC", _OTHER_CUSIP, Decimal("1.00"))
    stmts = [
        _statement(date(2025, 4, 1), date(2025, 10, 31)),
        _statement(date(2025, 11, 1), date(2026, 3, 31), dividends=(_FUND_DIV, _DIV, other)),
    ]
    with pytest.raises(NetBenefitsBundleError) as caught:
        _normalize(stmts)
    assert str(caught.value) == (
        f"the in-FY dividend at {_event_at(2, 3, _DIVIDENDS_TABLE)} is for a CUSIP that is "
        "neither netbenefits.security.cusip nor the core money-market fund"
    )
    assert_no_markers(str(caught.value), _OTHER_CUSIP, *_DAY_TEXT)


def test_normalize_income_ignores_an_out_of_fy_third_security_dividend() -> None:
    # The FY filter runs before any per-event check, so an out-of-FY dividend is
    # dropped without being refused.
    other = StatementDividend(
        date(2026, 4, 1), "SAMPLE ISSUER BN INC", "99993A108", Decimal("1.00")
    )
    stmt = _statement(date(2026, 2, 1), date(2026, 4, 30), dividends=(_DIV, other))
    dividends, withholdings = _records([stmt])
    assert [d.payment_date for d in dividends] == [date(2026, 3, 5)]
    assert withholdings == ()


def test_normalize_income_refuses_an_ambiguous_withholding() -> None:
    # Both securities print the same name on the same date, so a withholding for
    # that name matches two dividends.
    stock = replace(_DIV, date=_DAY, security_name=_ACCOUNT)
    same_name_fund = replace(_FUND_DIV, date=_DAY, security_name=_ACCOUNT)
    stmt = _statement(
        date(2025, 4, 1),
        date(2026, 3, 31),
        dividends=(stock, same_name_fund),
        withholdings=(replace(_WHT, date=_DAY, security_name=_ACCOUNT),),
    )
    with pytest.raises(NetBenefitsBundleError) as caught:
        _normalize([stmt])
    assert str(caught.value) == (
        "the withholding at statement 1 (by period start, year-end reports excluded), row 1 "
        "of its Taxes Withheld table matches 2 in-FY dividend(s); expected exactly one "
        "(ambiguous or orphan)"
    )
    assert_no_markers(str(caught.value), _ACCOUNT, *_DAY_TEXT)


def test_normalize_income_handles_multiple_dividends() -> None:
    divs = (
        StatementDividend(date(2025, 12, 4), "EXAMPLE CORP", "99999E104", Decimal("1.50")),
        StatementDividend(date(2026, 3, 5), "EXAMPLE CORP", "99999E104", Decimal("1.46")),
    )
    whts = (
        StatementWithholding(date(2025, 12, 4), "EXAMPLE CORP", Decimal("-0.38")),
        StatementWithholding(date(2026, 3, 5), "EXAMPLE CORP", Decimal("-0.37")),
    )
    dividends, withholdings = _records(
        [_statement(date(2025, 4, 1), date(2026, 3, 31), dividends=divs, withholdings=whts)],
    )
    assert len(dividends) == 2
    assert len(withholdings) == 2
    assert {d.amount_gross for d in dividends} == {Decimal("1.50"), Decimal("1.46")}


def test_normalize_income_refuses_orphan_withholding() -> None:
    divs = (StatementDividend(date(2026, 3, 5), "EXAMPLE CORP", "99999E104", Decimal("1.46")),)
    whts = (StatementWithholding(_DAY, _HOLDER, Decimal("-0.37")),)
    with pytest.raises(NetBenefitsBundleError) as caught:
        _records(
            [_statement(date(2025, 4, 1), date(2026, 3, 31), dividends=divs, withholdings=whts)],
        )
    assert str(caught.value) == (
        f"the withholding at {_event_at(1, 1, _TAXES_TABLE)} matches 0 in-FY dividend(s); "
        "expected exactly one (ambiguous or orphan)"
    )
    assert_no_markers(str(caught.value), _HOLDER, *_DAY_TEXT)


# #1042: the refusing withholding is the second row of the second statement
# (the two periods don't overlap).
_EARLIER = _statement(date(2025, 4, 1), date(2025, 7, 31))
_ORPHAN = StatementWithholding(date(2026, 2, 17), _HOLDER, Decimal("-0.37"))
_BEFORE_OPENING = StatementWithholding(date(2025, 8, 19), "EXAMPLE CORP", Decimal("-0.37"))


@pytest.mark.parametrize(
    ("second_row", "refusal", "values"),
    [
        (
            _ORPHAN,
            "matches 0 in-FY dividend(s); expected exactly one (ambiguous or orphan)",
            (_HOLDER, "2026-02-17", "02/17/2026"),
        ),
        (
            _BEFORE_OPENING,
            "predates netbenefits.account.account_opening_date; fix "
            f"netbenefits.account.account_opening_date, {_COVER_FROM_CORRECTED_DATE}"
            f"{_NO_DIVIDEND_ON_ITS_DATE}",
            ("2025-08-19", "08/19/2025", _OPENING_TEXT),
        ),
    ],
    ids=["orphan", "before-opening"],
)
def test_normalize_income_names_the_second_withholding_of_a_later_statement(
    second_row: StatementWithholding, refusal: str, values: tuple[str, ...]
) -> None:
    later = _statement(
        date(2025, 8, 1), date(2026, 3, 31), dividends=(_DIV,), withholdings=(_WHT, second_row)
    )
    with pytest.raises(NetBenefitsBundleError) as caught:
        _records([_EARLIER, later])
    assert str(caught.value) == (
        "the withholding at statement 2 (by period start, year-end reports excluded), row 2 "
        f"of its Taxes Withheld table {refusal}"
    )
    assert_no_markers(str(caught.value), *values)


def test_normalize_income_dedups_across_overlapping_statements() -> None:
    stmt = _statement(date(2025, 4, 1), date(2026, 3, 31), dividends=(_DIV,), withholdings=(_WHT,))
    dividends, withholdings = _records([stmt, stmt])
    assert len(dividends) == 1
    assert len(withholdings) == 1


# #943: overlapping statements must list the same events for the days they share.
_FEB_MAR = _statement(date(2026, 2, 1), date(2026, 3, 31), dividends=(_DIV,), withholdings=(_WHT,))
_MARCH_NO_INCOME = _statement(date(2026, 3, 1), date(2026, 3, 31))


# Before #1042 these refusals printed the periods, the event's day and its
# CUSIP or security name; now none may appear.
_OVERLAP_VALUES = ("99999E104", "EXAMPLE CORP", "2026-03-05", "2026-02-01", "2026-03-01")


@pytest.mark.parametrize(
    ("statements", "lister", "other"),
    [([_FEB_MAR, _MARCH_NO_INCOME], 1, 2), ([_MARCH_NO_INCOME, _FEB_MAR], 2, 1)],
    ids=["lister-first", "lister-second"],
)
def test_normalize_income_refuses_an_overlap_that_leaves_out_an_event(
    statements: list[StatementData], lister: int, other: int
) -> None:
    # Each statement can match its own totals, but they can't both be right.
    # The same event is named in either order (#1042: by the place each
    # statement has in the list).
    with pytest.raises(NetBenefitsBundleError) as caught:
        _records(statements)
    assert str(caught.value) == _missing_event_refusal("dividend", lister, 1, other)
    assert_no_markers(str(caught.value), *_OVERLAP_VALUES)


def test_normalize_income_refuses_an_overlap_that_leaves_out_only_the_withholding() -> None:
    march = _statement(date(2026, 3, 1), date(2026, 3, 31), dividends=(_DIV,))
    with pytest.raises(NetBenefitsBundleError) as caught:
        _records([_FEB_MAR, march])
    assert str(caught.value) == _missing_event_refusal("withholding", 1, 1, 2)
    assert_no_markers(str(caught.value), *_OVERLAP_VALUES)


def test_normalize_income_refuses_a_one_day_overlap_that_leaves_out_its_dividend() -> None:
    # The statements share only 5 March, and only the later one lists its dividend.
    stmts = [
        _statement(date(2026, 2, 1), date(2026, 3, 5)),
        _statement(date(2026, 3, 5), date(2026, 3, 31), dividends=(_DIV,)),
    ]
    with pytest.raises(NetBenefitsBundleError) as caught:
        _records(stmts)
    assert str(caught.value) == _missing_event_refusal("dividend", 2, 1, 1)
    assert_no_markers(str(caught.value), *_OVERLAP_VALUES, "2026-03-31")


# #1042: two overlapping statements in period-start order. Both list a shared
# event at row 1; the lister also lists the left-out event at row 2.
_SHARED_DAY = date(2025, 11, 12)
_LEFT_OUT = {
    "dividend": (
        replace(_DIV, date=_SHARED_DAY),
        StatementDividend(_DAY, "SAMPLE ISSUER BN INC", _OTHER_CUSIP, Decimal("1.00")),
    ),
    "withholding": (
        replace(_WHT, date=_SHARED_DAY),
        StatementWithholding(_DAY, _HOLDER, Decimal("-0.37")),
    ),
}


@pytest.mark.parametrize("kind", ["dividend", "withholding"], ids=["dividend", "withholding"])
@pytest.mark.parametrize(
    ("lister", "other"), [(1, 2), (2, 1)], ids=["lister-first", "lister-second"]
)
def test_normalize_income_names_the_listing_row_of_an_event_the_overlap_leaves_out(
    kind: str, lister: int, other: int
) -> None:
    shared, left_out = _LEFT_OUT[kind]
    rows = {lister: (shared, left_out), other: (shared,)}
    stmts = [
        _listing(date(2025, 11, 1), date(2025, 11, 30), kind, rows[1]),
        _listing(date(2025, 11, 10), date(2025, 12, 31), kind, rows[2]),
    ]
    with pytest.raises(NetBenefitsBundleError) as caught:
        _records(stmts)
    assert str(caught.value) == _missing_event_refusal(kind, lister, 2, other)
    assert_no_markers(
        str(caught.value),
        _OTHER_CUSIP,
        _HOLDER,
        *_DAY_TEXT,
        "2025-11-01",
        "2025-11-10",
        "2025-11-30",
        "2025-12-31",
    )


@pytest.mark.parametrize("swap", [False, True], ids=["period-order", "reversed"])
def test_normalize_income_names_the_same_first_difference_between_two_securities(
    swap: bool,
) -> None:
    """#1042: each overlapping statement lists a 5 March dividend the other
    leaves out, one from the configured stock and one from the core fund. The
    refusal names the same event as before #1042."""
    shared = replace(_FUND_DIV, date=date(2026, 3, 2))
    stmts = [
        _statement(date(2026, 2, 1), date(2026, 3, 31), dividends=(shared, _DIV)),
        _statement(
            date(2026, 3, 1),
            date(2026, 3, 31),
            dividends=(shared, replace(_FUND_DIV, date=_DIV.date)),
        ),
    ]
    # Before #1042 the refusal named the listing statement's period, the event's
    # CUSIP and date, and the overlapping statement's period. Both differences fall
    # on the same day and kind, and the core fund's identity string, its CUSIP,
    # sorts before the stock's, CUSIP '99999E104'. So the March statement's fund
    # dividend (row 2) was named, not the February-March statement's stock
    # dividend (also row 2), and the new message names that same event.
    march, feb_mar = (1, 2) if swap else (2, 1)
    with pytest.raises(NetBenefitsBundleError) as caught:
        _records(stmts[::-1] if swap else stmts)
    assert str(caught.value) == _missing_event_refusal("dividend", march, 2, feb_mar)
    assert_no_markers(str(caught.value), _FUND_DIV.cusip, "99999E104", "2026-03-05")


def test_normalize_income_dedups_overlapping_statements_that_list_the_same_events() -> None:
    march = _statement(date(2026, 3, 1), date(2026, 3, 31), dividends=(_DIV,), withholdings=(_WHT,))
    dividends, withholdings = _records([_FEB_MAR, march])
    assert [d.amount_gross for d in dividends] == [Decimal("1.46")]
    assert [w.amount for w in withholdings] == [Decimal("-0.36")]


def test_normalize_income_keeps_an_event_before_the_overlap() -> None:
    # The December event is before the overlap, so only the earlier statement can list it.
    december = StatementDividend(date(2025, 12, 4), "EXAMPLE CORP", "99999E104", Decimal("1.50"))
    stmts = [
        _statement(date(2025, 11, 1), date(2026, 3, 31), dividends=(december,)),
        _statement(date(2026, 2, 1), date(2026, 3, 31)),
    ]
    dividends, _ = _records(stmts)
    assert [d.payment_date for d in dividends] == [date(2025, 12, 4)]


@pytest.mark.parametrize(
    "day", [date(2025, 3, 20), date(2026, 4, 10)], ids=["before-fy", "after-fy"]
)
def test_normalize_income_compares_overlaps_only_inside_the_fy(day: date) -> None:
    outside = StatementDividend(day, "EXAMPLE CORP", "99999E104", Decimal("1.80"))
    stmts = [
        _statement(date(2025, 3, 1), date(2026, 4, 30), dividends=(outside, _DIV)),
        _statement(date(2025, 3, 1), date(2026, 4, 30), dividends=(_DIV,)),
    ]
    dividends, _ = _records(stmts)
    assert [d.payment_date for d in dividends] == [date(2026, 3, 5)]


def test_normalize_income_refuses_duplicate_within_one_statement() -> None:
    # Two identical dividend rows in ONE statement are ambiguous → refuse (don't
    # silently collapse and under-report).
    dup = StatementDividend(_DAY, "ANN", "99999E104", Decimal("1.46"))
    stmt = _statement(date(2025, 4, 1), date(2026, 3, 31), dividends=(dup, _DIV, dup))
    with pytest.raises(NetBenefitsBundleError) as caught:
        _records([stmt])
    assert str(caught.value) == (
        "statement 1 (by period start, year-end reports excluded) lists the same dividend at "
        "rows 1 and 3 of its Dividends, Interest & Other Income table — ambiguous, refusing"
    )
    assert_no_markers(str(caught.value), "ANN", *_DAY_TEXT)


def test_normalize_income_refuses_duplicate_withholding_within_one_statement() -> None:
    dup = StatementWithholding(_DAY, _HOLDER, Decimal("-0.37"))
    stmt = _statement(
        date(2025, 4, 1), date(2026, 3, 31), dividends=(_DIV,), withholdings=(_WHT, dup, dup)
    )
    with pytest.raises(NetBenefitsBundleError) as caught:
        _records([stmt])
    assert str(caught.value) == (
        f"{_nth_statement(1)} lists the same withholding at rows 2 and 3 of its "
        f"{_TAXES_TABLE} table — ambiguous, refusing"
    )
    assert_no_markers(str(caught.value), _HOLDER, *_DAY_TEXT)


# #1042: cross-statement conflicts. The first statement lists an event before
# the overlap at row 1, so the conflicting event is its row 2 and the second
# statement's row 1.
_BEFORE_OVERLAP = date(2025, 11, 5)


def _conflicting(first: tuple[object, ...], second: tuple[object, ...], kind: str) -> str:
    """The refusal from two overlapping statements that list ``first`` and
    ``second`` as the same ``kind`` event."""
    stmts = [
        _listing(date(2025, 11, 1), date(2026, 3, 31), kind, first),
        _listing(date(2025, 11, 10), date(2026, 3, 31), kind, second),
    ]
    with pytest.raises(NetBenefitsBundleError) as caught:
        _records(stmts)
    return str(caught.value)


def test_normalize_income_refuses_conflicting_dividend_amount_across_statements() -> None:
    # Same (date, CUSIP) dividend reported with different gross across two
    # overlapping statements → conflict, refuse (don't file both).
    before = replace(_DIV, date=_BEFORE_OVERLAP)
    div_a = StatementDividend(_DAY, "EXAMPLE CORP", "99999E104", _AMOUNT)
    div_b = StatementDividend(_DAY, "EXAMPLE CORP", "99999E104", _OTHER_AMOUNT)
    message = _conflicting((before, div_a), (div_b,), "dividend")
    assert message == (
        "the dividend at statement 1 (by period start, year-end reports excluded), row 2 of its "
        "Dividends, Interest & Other Income table and the same dividend at statement 2 (by "
        "period start, year-end reports excluded), row 1 of its Dividends, Interest & Other "
        "Income table report different amounts; refusing rather than file a guessed figure"
    )
    assert_no_markers(message, *_AMOUNT_TEXT, "EXAMPLE CORP", *_DAY_TEXT)


def test_normalize_income_refuses_name_drift_within_one_statement() -> None:
    # Two rows for the SAME (date, CUSIP) but a drifted printed name in ONE
    # statement → same (date, CUSIP) identity → refuse rather than double-count.
    div_a = StatementDividend(_DAY, _HOLDER, "99999E104", Decimal("1.46"))
    div_b = StatementDividend(_DAY, _ACCOUNT, "99999E104", Decimal("0.50"))
    stmt = _statement(date(2025, 4, 1), date(2026, 3, 31), dividends=(div_a, div_b))
    with pytest.raises(NetBenefitsBundleError) as caught:
        _records([stmt])
    assert str(caught.value) == (
        f"{_nth_statement(1)} lists the same dividend at rows 1 and 2 of its "
        f"{_DIVIDENDS_TABLE} table — ambiguous, refusing"
    )
    assert_no_markers(str(caught.value), _HOLDER, _ACCOUNT, *_DAY_TEXT)


def test_normalize_income_refuses_name_drift_across_statements() -> None:
    # Same (date, CUSIP) reported across two overlapping statements with a drifted
    # printed name → refuse (don't guess which name/figure to file).
    before = replace(_DIV, date=_BEFORE_OVERLAP)
    div_a = StatementDividend(_DAY, _HOLDER, "99999E104", Decimal("1.46"))
    div_b = StatementDividend(_DAY, _ACCOUNT, "99999E104", Decimal("1.46"))
    message = _conflicting((before, div_a), (div_b,), "dividend")
    assert message == (
        f"the dividend at {_event_at(1, 2, _DIVIDENDS_TABLE)} and the same dividend at "
        f"{_event_at(2, 1, _DIVIDENDS_TABLE)} report different security names; refusing "
        "rather than guess which is correct"
    )
    assert_no_markers(message, _HOLDER, _ACCOUNT, *_DAY_TEXT)


def test_normalize_income_refuses_dividend_before_account_opening() -> None:
    # In-FY (>= 2025-04-01) but BEFORE the account opened → contradiction: income
    # can't precede the account, and coverage before opening isn't required.
    div = StatementDividend(date(2025, 8, 19), "EXAMPLE CORP", "99999E104", Decimal("1.46"))
    stmt = _statement(date(2025, 4, 1), date(2026, 3, 31), dividends=(div,))
    with pytest.raises(NetBenefitsBundleError) as caught:
        _records([stmt])
    assert str(caught.value) == (
        "the dividend at statement 1 (by period start, year-end reports excluded), row 1 of its "
        "Dividends, Interest & Other Income table predates "
        "netbenefits.account.account_opening_date; income can't precede the account, and "
        "coverage before it isn't required — fix netbenefits.account.account_opening_date, "
        f"{_COVER_FROM_CORRECTED_DATE}"
    )
    # Neither the dividend's date nor the configured opening date is printed.
    assert_no_markers(str(caught.value), "2025-08-19", "08/19/2025", _OPENING_TEXT)


def test_normalize_income_refuses_withholding_before_account_opening() -> None:
    wht = StatementWithholding(date(2025, 8, 19), "EXAMPLE CORP", Decimal("-0.37"))
    stmt = _statement(date(2025, 4, 1), date(2026, 3, 31), dividends=(_DIV,), withholdings=(wht,))
    with pytest.raises(NetBenefitsBundleError) as caught:
        _records([stmt])
    assert str(caught.value) == (
        f"the withholding at {_event_at(1, 1, _TAXES_TABLE)} predates "
        "netbenefits.account.account_opening_date; fix netbenefits.account.account_opening_date, "
        f"{_COVER_FROM_CORRECTED_DATE}{_NO_DIVIDEND_ON_ITS_DATE}"
    )
    assert_no_markers(str(caught.value), "2025-08-19", "08/19/2025", _OPENING_TEXT)


def test_normalize_income_refuses_conflicting_withholding_amount_across_statements() -> None:
    before = replace(_WHT, date=_BEFORE_OVERLAP)
    wht_a = StatementWithholding(_DAY, "EXAMPLE CORP", -_AMOUNT)
    wht_b = StatementWithholding(_DAY, "EXAMPLE CORP", -_OTHER_AMOUNT)
    message = _conflicting((before, wht_a), (wht_b,), "withholding")
    assert message == (
        f"the withholding at {_event_at(1, 2, _TAXES_TABLE)} and the same withholding at "
        f"{_event_at(2, 1, _TAXES_TABLE)} report different amounts; refusing rather than "
        "file a guessed figure"
    )
    assert_no_markers(message, *_AMOUNT_TEXT, "EXAMPLE CORP", *_DAY_TEXT)


def test_normalize_income_skips_out_of_fy_events() -> None:
    divs = (
        StatementDividend(date(2024, 12, 4), "EXAMPLE CORP", "99999E104", Decimal("9.99")),
        _DIV,
    )
    whts = (StatementWithholding(date(2024, 12, 4), "EXAMPLE CORP", Decimal("-2.00")), _WHT)
    dividends, withholdings = _records(
        [_statement(date(2025, 4, 1), date(2026, 3, 31), dividends=divs, withholdings=whts)],
    )
    assert [d.amount_gross for d in dividends] == [Decimal("1.46")]
    assert [w.amount for w in withholdings] == [Decimal("-0.36")]
