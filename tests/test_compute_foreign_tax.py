"""Tests for :mod:`ibkr_fa.compute.foreign_tax`."""

from __future__ import annotations

from dataclasses import replace
from datetime import date
from decimal import Decimal

import pytest

from ibkr_fa.compute.foreign_tax import (
    ForeignTaxNetting,
    PaidWithholding,
    StatedTaxCountryMismatchError,
    WithholdingPaymentKey,
    net_foreign_tax_by_country,
    require_no_orphan_wht,
)
from ibkr_fa.compute.schedule_os import ScheduleOsDividendLine
from ibkr_fa.compute.wht_reversal_pairing import (
    UnprovenWhtReversalError,
    WhtCrossFyReversalReport,
)
from ibkr_fa.config import ScheduleOsIndianAdrRoutingMode
from ibkr_fa.exceptions import IbkrFaError, InvariantError
from ibkr_fa.models.distribution_kind import DistributionKind
from ibkr_fa.models.withholding_tax import WithholdingTax
from ibkr_fa.reference.country_codes import CountryCodeNotFoundError, lookup_country
from ibkr_fa.types import Isin, Ticker
from tests._foreign_tax_refusals import (
    NO_ORIGINAL,
    TWO_FY_CANDIDATES,
    assert_no_wht_echo,
    orphan_wht_message,
    stated_country_message,
    unproven_reversal_message,
)

_FY_START = date(2025, 4, 1)
_FY_END = date(2026, 3, 31)
_XQAQ = Isin("US99992A1097")
_XQCA = Isin("US99972B1017")
_XQCR = Isin("GB99970J1065")
_INFY_ADR = Isin("US4567881085")


class _MonthlyFxPolicy:
    """USD stub whose rate is 80 + the event month; records each lookup."""

    currency = "USD"

    def __init__(self) -> None:
        self.calls: list[date] = []

    def rate_for(self, event_date: date) -> Decimal:
        self.calls.append(event_date)
        return Decimal(80 + event_date.month)


def _wht(isin: Isin, payment_date: date, amount: str, *, currency: str = "USD") -> WithholdingTax:
    return WithholdingTax(
        symbol=Ticker("TEST"),
        isin=isin,
        payment_date=payment_date,
        amount=Decimal(amount),
        currency=currency,
        description=f"TEST({isin}) Cash Dividend - Tax",
    )


def _stated(row: WithholdingTax, country: str, line: int) -> WithholdingTax:
    """``row`` as the IBKR parser builds it from a ``" - <country> Tax"`` suffix."""
    return replace(
        row,
        description=f"{row.description[: -len(' - Tax')]} - {country} Tax",
        tax_country_iso2=country,
        file_line=line,
    )


def _totals(netting: ForeignTaxNetting) -> tuple[object, ...]:
    """Everything ``netting`` files or reports, without the rows themselves."""
    return (
        netting.countries,
        netting.signed_inr_by_isin,
        netting.paid_by_payment,
        netting.cross_fy_reversal_report,
    )


def _dividend(isin: Isin, payment_date: date) -> ScheduleOsDividendLine:
    return ScheduleOsDividendLine(
        symbol=Ticker("TEST"),
        isin=isin,
        issuer_country_iso2=isin[:2],
        payment_date=payment_date,
        fx_specified_date=payment_date,
        fx_rate_publication_date=payment_date,
        fx_rate_inr_per_usd=Decimal("85"),
        amount_gross_usd=Decimal("10"),
        amount_gross_inr=Decimal("850"),
        description=f"TEST({isin}) Cash Dividend (Ordinary Dividend)",
        kind=DistributionKind.CASH_DIVIDEND,
    )


def _net(
    rows: list[WithholdingTax],
    *,
    fx_policy: _MonthlyFxPolicy | None = None,
    adr_routing_mode: ScheduleOsIndianAdrRoutingMode = ScheduleOsIndianAdrRoutingMode.DEPOSITARY,
) -> ForeignTaxNetting:
    return net_foreign_tax_by_country(
        rows,
        fx_policy=_MonthlyFxPolicy() if fx_policy is None else fx_policy,
        financial_year_start=_FY_START,
        financial_year_end=_FY_END,
        adr_routing_mode=adr_routing_mode,
    )


def test_adr_skip_runs_only_in_underlying_issuer_mode_and_before_pairing() -> None:
    rows = [
        _wht(_XQAQ, date(2025, 6, 10), "-1.00"),
        _wht(_INFY_ADR, date(2025, 6, 10), "-3.00"),
        _wht(_INFY_ADR, date(2025, 7, 10), "+0.50"),  # orphan reversal
    ]

    netting = _net(rows, adr_routing_mode=ScheduleOsIndianAdrRoutingMode.UNDERLYING_ISSUER)

    assert netting.included_rows == (rows[0],)
    assert netting.countries["US"].event_count == 1
    # A skipped row never reaches pairing, so it is neither counted nor refused.
    assert netting.cross_fy_reversal_report == WhtCrossFyReversalReport()
    # Depositary mode keeps the ADR row, so its orphan reversal refuses.
    with pytest.raises(UnprovenWhtReversalError) as excinfo:
        _net(rows)
    assert str(excinfo.value) == unproven_reversal_message(NO_ORIGINAL)
    assert_no_wht_echo(str(excinfo.value), rows)


def test_current_fy_reversal_of_prior_fy_original_is_excluded_and_reported() -> None:
    """Sub-case (a2): the XQAQ reversal must not net against XQCA's tax."""
    rows = [
        _wht(_XQAQ, date(2025, 3, 10), "-5.00"),  # prior FY
        _wht(_XQAQ, date(2025, 6, 10), "+5.00"),
        _wht(_XQCA, date(2025, 6, 12), "-1.00"),
    ]

    netting = _net(rows)

    assert netting.included_rows == (rows[2],)
    assert netting.countries["US"].signed_inr == Decimal("-86.00")
    assert netting.cross_fy_reversal_report == WhtCrossFyReversalReport(
        cross_fy_excluded_count=1,
        cross_fy_excluded_amount_foreign={"USD": Decimal("5.00")},
    )


@pytest.mark.parametrize(
    ("rows", "unproven"),
    [
        pytest.param(
            [
                _wht(_XQAQ, date(2025, 3, 15), "-5.00"),  # prior FY
                _wht(_XQAQ, date(2025, 6, 15), "-5.00"),
                _wht(_XQAQ, date(2025, 8, 1), "+5.00"),
            ],
            (TWO_FY_CANDIDATES,),
            id="candidates-in-prior-and-current-fy",
        ),
        pytest.param(
            [
                _wht(_XQAQ, date(2024, 3, 15), "-5.00"),  # FY 2023-24
                _wht(_XQAQ, date(2025, 2, 15), "-5.00"),  # FY 2024-25
                _wht(_XQAQ, date(2025, 8, 1), "+5.00"),
            ],
            (TWO_FY_CANDIDATES,),
            id="candidates-in-two-prior-fys",
        ),
        pytest.param(
            [
                _wht(_XQAQ, date(2025, 3, 10), "-5.00"),  # prior FY
                _wht(_XQAQ, date(2025, 6, 10), "-5.00"),
                _wht(_XQAQ, date(2025, 7, 1), "+5.00"),
                _wht(_XQAQ, date(2025, 7, 1), "+5.00"),  # one candidate left
            ],
            (TWO_FY_CANDIDATES,),
            id="first-of-two-value-identical-reversals",
        ),
        pytest.param(
            [_wht(_XQAQ, date(2025, 6, 15), "-3.00"), _wht(_XQAQ, date(2025, 8, 1), "+5.00")],
            (NO_ORIGINAL,),
            id="refund-larger-than-paid",
        ),
        pytest.param(
            [_wht(_XQAQ, date(2025, 6, 10), "-1.00"), _wht(_XQAQ, date(2025, 7, 10), "+0.30")],
            (NO_ORIGINAL,),
            id="partial-refund",
        ),
        pytest.param(
            [_wht(_XQAQ, date(2025, 6, 10), "-1.00"), _wht(_XQCA, date(2025, 7, 10), "+0.30")],
            (NO_ORIGINAL,),
            id="absent-original",
        ),
        pytest.param(
            [
                _wht(_XQAQ, date(2025, 3, 15), "-5.00"),
                _wht(_XQAQ, date(2025, 6, 15), "-5.00"),
                _wht(_XQAQ, date(2025, 8, 1), "+5.00"),
                _wht(_XQCA, date(2025, 8, 2), "+0.30"),
            ],
            (TWO_FY_CANDIDATES, NO_ORIGINAL),
            id="every-unproven-reversal-is-listed",
        ),
    ],
)
def test_current_fy_reversal_whose_original_year_is_unproven_refuses(
    rows: list[WithholdingTax], unproven: tuple[str, ...]
) -> None:
    """#893: netting would guess which year's tax the refund cancels. The
    refusal names each unproven reversal, in row order, by why its year is
    unproven, never by its ISIN, amount, currency or dates (#1047)."""
    with pytest.raises(UnprovenWhtReversalError) as excinfo:
        _net(rows)
    assert str(excinfo.value) == unproven_reversal_message(*unproven)
    assert_no_wht_echo(str(excinfo.value), rows)


@pytest.mark.parametrize(
    "rows",
    [
        pytest.param(
            [_wht(_XQAQ, date(2025, 6, 10), "-1.00"), _wht(_XQCA, date(2025, 3, 10), "+0.30")],
            id="prior-fy-orphan",
        ),
        pytest.param(
            [_wht(_XQAQ, date(2025, 6, 10), "-1.00"), _wht(_XQAQ, date(2026, 4, 17), "+0.30")],
            id="next-fy-partial-refund",
        ),
        pytest.param(
            [
                _wht(_XQAQ, date(2024, 3, 15), "-5.00"),  # FY 2023-24
                _wht(_XQAQ, date(2025, 2, 15), "-5.00"),  # FY 2024-25
                _wht(_XQAQ, date(2025, 3, 1), "+5.00"),  # FY 2024-25
                _wht(_XQAQ, date(2025, 6, 10), "-1.00"),
            ],
            id="prior-fy-ambiguity-across-fys",
        ),
    ],
)
def test_out_of_fy_unproven_reversals_do_not_refuse(rows: list[WithholdingTax]) -> None:
    netting = _net(rows)

    assert netting.included_rows == (_wht(_XQAQ, date(2025, 6, 10), "-1.00"),)
    assert netting.cross_fy_reversal_report == WhtCrossFyReversalReport()


def test_same_fy_ambiguity_nets_and_is_counted() -> None:
    """Either candidate is in this FY, so the FY total doesn't depend on it."""
    rows = [
        _wht(_XQAQ, date(2025, 5, 10), "-5.00"),
        _wht(_XQAQ, date(2025, 6, 10), "-5.00"),
        _wht(_XQAQ, date(2025, 7, 10), "+5.00"),
    ]

    netting = _net(rows)

    assert netting.included_rows == tuple(rows)
    assert netting.countries["US"].signed_native == Decimal("-5.00")
    assert netting.cross_fy_reversal_report == WhtCrossFyReversalReport(ambiguous_same_fy_count=1)


@pytest.mark.parametrize("account", [None, 0])
def test_reversal_nets_against_its_own_accounts_original(account: int | None) -> None:
    rows = [
        replace(_wht(_XQAQ, date(2025, 6, 10), "-5.00"), source_account_index=account),
        replace(_wht(_XQAQ, date(2025, 8, 1), "+5.00"), source_account_index=account),
    ]

    netting = _net(rows)

    assert netting.included_rows == tuple(rows)
    assert netting.countries["US"].signed_native == 0


def test_reversal_does_not_pair_with_another_accounts_withholding() -> None:
    """#893: account 0's refund can't cancel account 1's tax."""
    rows = [
        replace(_wht(_XQAQ, date(2025, 6, 10), "-5.00"), source_account_index=1),
        replace(_wht(_XQAQ, date(2025, 8, 1), "+5.00"), source_account_index=0),
    ]

    with pytest.raises(UnprovenWhtReversalError) as excinfo:
        _net(rows)
    # The source account's index is a position, not input, so it may print.
    assert str(excinfo.value) == unproven_reversal_message(
        "a reversal in source account 0 with no original"
    )
    assert_no_wht_echo(str(excinfo.value), rows)


def test_another_accounts_withholding_is_not_a_candidate() -> None:
    """Account 1's prior-FY tax doesn't make account 0's refund ambiguous."""
    rows = [
        replace(_wht(_XQAQ, date(2025, 3, 15), "-5.00"), source_account_index=1),
        replace(_wht(_XQAQ, date(2025, 6, 15), "-5.00"), source_account_index=0),
        replace(_wht(_XQAQ, date(2025, 8, 1), "+5.00"), source_account_index=0),
    ]

    netting = _net(rows)

    assert netting.included_rows == tuple(rows[1:])
    assert netting.cross_fy_reversal_report == WhtCrossFyReversalReport()


def test_out_of_fy_rows_stay_out_of_totals_fx_lookups_and_report() -> None:
    """The next-FY reversal still pairs with the in-FY original (LIFO), so
    it is not an orphan; out-of-FY reversals are never reported."""
    rows = [
        _wht(_XQAQ, date(2025, 3, 31), "-2.00"),
        _wht(_XQAQ, date(2025, 4, 1), "-1.00"),
        _wht(_XQAQ, date(2026, 4, 1), "+1.00"),
    ]
    fx_policy = _MonthlyFxPolicy()

    netting = _net(rows, fx_policy=fx_policy)

    assert netting.included_rows == (rows[1],)
    assert fx_policy.calls == [date(2025, 4, 1)]
    assert netting.countries["US"].signed_inr == Decimal("-84.00")
    assert netting.cross_fy_reversal_report == WhtCrossFyReversalReport()


def test_refund_in_later_fx_month_nets_native_to_zero_but_not_inr() -> None:
    netting = _net(
        [
            _wht(_XQAQ, date(2025, 11, 10), "-10.00"),
            _wht(_XQAQ, date(2025, 12, 10), "+10.00"),
        ]
    )

    us = netting.countries["US"]
    assert us.signed_native == 0
    assert us.signed_inr == Decimal("-910.00") + Decimal("920.00")
    assert netting.cross_fy_reversal_report == WhtCrossFyReversalReport()


def test_row_currency_must_match_policy_currency() -> None:
    with pytest.raises(InvariantError, match="multi-currency"):
        _net([_wht(_XQCR, date(2025, 6, 10), "-1.00", currency="EUR")])


def test_unknown_isin_country_prefix_raises() -> None:
    with pytest.raises(CountryCodeNotFoundError):
        _net([_wht(Isin("ZZ99990A3095"), date(2025, 6, 10), "-1.00")])


def test_per_country_and_per_isin_nets_and_event_counts() -> None:
    netting = _net(
        [
            _wht(_XQAQ, date(2025, 6, 10), "-1.00"),
            _wht(_XQCR, date(2025, 6, 11), "-2.00"),
            _wht(_XQCA, date(2025, 6, 12), "-0.30"),
            _wht(_XQCA, date(2025, 7, 10), "+0.30"),
            _wht(_XQAQ, date(2025, 8, 10), "-0.50"),
        ]
    )

    assert list(netting.countries) == ["US", "GB"]
    us = netting.countries["US"]
    gb = netting.countries["GB"]
    assert (us.event_count, gb.event_count) == (4, 1)
    assert us.signed_native == Decimal("-1.50")
    assert us.signed_inr == (
        Decimal("-86.00") + Decimal("-25.80") + Decimal("26.10") + Decimal("-44.00")
    )
    assert gb.signed_inr == Decimal("-172.00")
    assert (gb.cbdt_country_code, gb.country_name_portal) == (
        lookup_country("GB").cbdt_code,
        lookup_country("GB").country_name_portal,
    )
    assert netting.signed_inr_by_isin == {
        "US99992A1097": Decimal("-130.00"),
        "GB99970J1065": Decimal("-172.00"),
        "US99972B1017": Decimal("0.30"),
    }


def test_orphan_guard_raises_for_paid_wht_without_its_dividend() -> None:
    """The refusal counts the orphan events; it doesn't print their ISIN or
    payment date, which are input (#1047)."""
    orphan = _wht(_XQCA, date(2025, 6, 12), "-0.50")
    netting = _net([_wht(_XQAQ, date(2025, 6, 10), "-1.00"), orphan])

    with pytest.raises(IbkrFaError) as excinfo:
        require_no_orphan_wht(
            netting,
            [_dividend(_XQAQ, date(2025, 6, 10))],
            financial_year_start=_FY_START,
            financial_year_end=_FY_END,
        )
    assert str(excinfo.value) == orphan_wht_message(1)
    assert_no_wht_echo(str(excinfo.value), [orphan])


def test_orphan_guard_matches_the_source_account() -> None:
    """#890: paid WHT in account 1 doesn't match account 0's dividend on the
    same ISIN and date. The refusal counts the event without its ISIN, date
    or source account (#1047)."""
    paid = replace(_wht(_XQAQ, date(2025, 6, 10), "-1.00"), source_account_index=1)
    dividend = replace(_dividend(_XQAQ, date(2025, 6, 10)), source_account_index=0)

    with pytest.raises(IbkrFaError) as excinfo:
        require_no_orphan_wht(
            _net([paid]), [dividend], financial_year_start=_FY_START, financial_year_end=_FY_END
        )
    assert str(excinfo.value) == orphan_wht_message(1)
    assert_no_wht_echo(str(excinfo.value), [paid])
    assert "source account 1" not in str(excinfo.value)
    require_no_orphan_wht(
        _net([paid]),
        [replace(dividend, source_account_index=1)],
        financial_year_start=_FY_START,
        financial_year_end=_FY_END,
    )


def test_wht_refusals_print_no_synthetic_isin_amount_or_date() -> None:
    """Both WHT refusals are fixed text: a synthetic ISIN, a private amount,
    the Currency cell and the event dates never appear (#1047)."""
    isin = Isin("US99971T1035")
    paid = _wht(isin, date(2025, 6, 10), "-2000000.00")
    refund = _wht(isin, date(2025, 7, 10), "+1234567.89")  # partial: no original

    with pytest.raises(UnprovenWhtReversalError) as reversal:
        _net([paid, refund])
    assert str(reversal.value) == unproven_reversal_message(NO_ORIGINAL)
    assert_no_wht_echo(str(reversal.value), [paid, refund])

    with pytest.raises(IbkrFaError) as orphan:
        require_no_orphan_wht(
            _net([paid]), [], financial_year_start=_FY_START, financial_year_end=_FY_END
        )
    assert str(orphan.value) == orphan_wht_message(1)
    assert_no_wht_echo(str(orphan.value), [paid])


def test_paid_rows_are_summed_per_payment() -> None:
    """#890: paid rows sum per (source account, ISIN, payment date); refunds
    are left out."""
    netting = _net(
        [
            _wht(_XQAQ, date(2025, 6, 10), "-1.00"),
            _wht(_XQAQ, date(2025, 6, 10), "-0.50"),
            replace(_wht(_XQAQ, date(2025, 6, 10), "-2.00"), source_account_index=0),
            _wht(_XQAQ, date(2025, 7, 10), "+0.50"),
        ]
    )

    assert netting.paid_by_payment == {
        WithholdingPaymentKey(None, _XQAQ, date(2025, 6, 10)): PaidWithholding(
            Decimal("1.50"), Decimal("129.00"), 2
        ),
        WithholdingPaymentKey(0, _XQAQ, date(2025, 6, 10)): PaidWithholding(
            Decimal("2.00"), Decimal("172.00"), 1
        ),
    }


@pytest.mark.parametrize(
    "rows",
    [
        pytest.param(
            [
                _wht(_XQAQ, date(2025, 6, 10), "-1.00"),
                _wht(_XQAQ, date(2025, 6, 10), "-0.30"),
                _wht(_XQAQ, date(2025, 7, 15), "+0.30"),
            ],
            id="later-refund-row-of-net-paid-isin",
        ),
        pytest.param(
            [
                _wht(_XQAQ, date(2025, 6, 10), "-1.00"),
                _wht(_XQCA, date(2025, 6, 12), "-0.50"),
                _wht(_XQCA, date(2025, 7, 10), "+0.50"),  # a later FX month
            ],
            id="paid-row-of-net-refund-isin",
        ),
        pytest.param(
            [
                _wht(_XQAQ, date(2025, 6, 10), "-1.00"),
                _wht(_XQAQ, date(2025, 12, 10), "+1.00"),  # a later FX month
                _wht(_XQCA, date(2025, 6, 12), "-0.05"),
            ],
            id="paid-isin-in-net-refund-country",
        ),
    ],
)
def test_orphan_guard_skips_wht_that_cannot_add_credit(rows: list[WithholdingTax]) -> None:
    """Only the XQAQ dividend of 2025-06-10 exists, but no other row can
    raise the credit FSI keeps."""
    require_no_orphan_wht(
        _net(rows),
        [_dividend(_XQAQ, date(2025, 6, 10))],
        financial_year_start=_FY_START,
        financial_year_end=_FY_END,
    )


# ---- #1282: a stated tax country must match the ISIN's -----------------------


@pytest.mark.parametrize("active_handler", [False, True])
def test_stated_country_other_than_the_isins_refuses_before_any_total(active_handler: bool) -> None:
    """Every such row is named by its line, in input order, and the refusal
    comes before any FX lookup and before the reversal-year check."""
    rows = [
        _stated(_wht(_XQAQ, date(2025, 6, 10), "-1.37"), "NL", 41),
        _stated(_wht(_XQCA, date(2025, 6, 10), "-1.37"), "US", 42),
        _stated(_wht(_XQCR, date(2025, 7, 10), "-1.37"), "IE", 43),
        _wht(_XQAQ, date(2025, 8, 10), "+0.50"),  # orphan reversal
        _stated(_wht(_XQCA, date(2025, 9, 10), "+1.37"), "CA", 7),
    ]
    fx_policy = _MonthlyFxPolicy()

    with pytest.raises(StatedTaxCountryMismatchError) as excinfo:
        try:
            if active_handler:
                raise ValueError(rows)  # noqa: TRY301 - exercise an active input-bearing handler
        except ValueError:
            _net(rows, fx_policy=fx_policy)
        else:
            _net(rows, fx_policy=fx_policy)

    error = excinfo.value
    assert str(error) == stated_country_message(41, 43, 7)
    assert isinstance(error, IbkrFaError)
    assert error.args == (stated_country_message(41, 43, 7),)
    assert vars(error) == {}
    assert error.__cause__ is None
    assert error.__context__ is None
    assert fx_policy.calls == []
    for text in (str(error), repr(error)):
        assert_no_wht_echo(text, rows)
        for row in rows:
            assert row.symbol not in text
            if row.tax_country_iso2 is not None:
                assert row.tax_country_iso2 in row.description
                assert row.tax_country_iso2 not in text


@pytest.mark.parametrize(
    "payment_date",
    [date(2025, 3, 31), date(2026, 4, 1)],
    ids=["before-fy", "after-fy"],
)
def test_out_of_fy_row_with_another_stated_country_refuses(payment_date: date) -> None:
    """Every row that reaches netting is checked, in or out of the FY."""
    rows = [
        _wht(_XQAQ, date(2025, 6, 10), "-1.37"),
        _stated(_wht(_XQAQ, payment_date, "-1.37"), "NL", 9),
    ]
    with pytest.raises(StatedTaxCountryMismatchError) as excinfo:
        _net(rows)
    assert str(excinfo.value) == stated_country_message(9)


def test_stated_country_matching_the_isins_or_absent_nets_as_before() -> None:
    plain = [
        _wht(_XQAQ, date(2025, 6, 10), "-1.37"),
        _wht(_XQAQ, date(2025, 6, 20), "+1.37"),
        _wht(_XQCR, date(2025, 7, 10), "-2.00"),
        _wht(_XQCA, date(2025, 8, 10), "-2.74"),
    ]
    stated = [
        _stated(plain[0], "US", 31),
        _stated(plain[1], "US", 32),
        _stated(plain[2], "GB", 33),
        plain[3],
    ]

    assert _totals(_net(stated)) == _totals(_net(plain))


def test_indian_adr_stating_india_refuses_only_under_depositary_routing() -> None:
    """#1282: under ``underlying-issuer`` the mapped ADR row is skipped before
    the check, so the run keeps today's result; under ``depositary`` its
    Indian tax would be credited as US tax, so it refuses."""
    us_row = _wht(_XQAQ, date(2025, 6, 10), "-1.00")
    adr_row = _wht(_INFY_ADR, date(2025, 6, 10), "-3.00")
    stated_adr = _stated(adr_row, "IN", 17)
    underlying = ScheduleOsIndianAdrRoutingMode.UNDERLYING_ISSUER

    assert _totals(_net([us_row, stated_adr], adr_routing_mode=underlying)) == _totals(
        _net([us_row, adr_row], adr_routing_mode=underlying)
    )
    with pytest.raises(StatedTaxCountryMismatchError) as excinfo:
        _net([us_row, stated_adr])
    assert str(excinfo.value) == stated_country_message(17)
