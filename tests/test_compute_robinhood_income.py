"""Focused checks for the reconciled Robinhood income adapter."""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterable, Mapping
from dataclasses import replace
from datetime import date
from decimal import Decimal
from typing import Any

import pytest

from ibkr_fa.compute.income_engine import compute_income_schedules
from ibkr_fa.compute.robinhood_fa import _program_bank_evidence
from ibkr_fa.compute.robinhood_income import (
    RobinhoodAnnualFormDiscrepancyDirection,
    RobinhoodFeeTreatment,
    RobinhoodIncomeBlocker,
    RobinhoodIncomeBlockerCause,
    RobinhoodIncomeBlockerKind,
    RobinhoodIncomeError,
    RobinhoodIncomeEventReference,
    RobinhoodIncomeResult,
    RobinhoodInterestPayment,
    RobinhoodStockLendingSubtype,
    _annual_form_years,
    _packet_buckets,
    _partition_interest_payments,
    _reconcile_bucket_events,
    _StockLendingSource,
    compute_robinhood_income,
)
from ibkr_fa.compute.schedule_tr import compute_schedule_tr_from_fsi
from ibkr_fa.config import (
    FxConfig,
    RobinhoodConfig,
    RobinhoodInterestKind,
    RobinhoodInterestPayerCountry,
)
from ibkr_fa.exceptions import InvariantError
from ibkr_fa.fx.context import TaxFxContext
from ibkr_fa.models import InterestCredit, SubstituteDividendIncome
from ibkr_fa.models.form_1042s import Form1042SAmount, Form1042SAmountKind
from ibkr_fa.parser.robinhood import RobinhoodEvent, RobinhoodEventKind
from ibkr_fa.parser.robinhood_1042s import Robinhood1042SPacket
from ibkr_fa.pipeline import FinancialYear
from ibkr_fa.types import CountryCode, Isin, Ticker
from tests._no_echo import assert_no_markers
from tests._private_checks import private_test
from tests._robinhood_income_inputs import ALL_US_PAYERS as _ALL_US_PAYERS
from tests._robinhood_income_inputs import BOTH_INTEREST_KINDS as _BOTH
from tests._robinhood_income_inputs import BROKERAGE_INTEREST as _BROKERAGE
from tests._robinhood_income_inputs import CGD_LABEL as _CGD
from tests._robinhood_income_inputs import INCOME_ACCOUNT as _ACCOUNT
from tests._robinhood_income_inputs import INCOME_FY_END as _FY_END
from tests._robinhood_income_inputs import INCOME_FY_START as _FY_START
from tests._robinhood_income_inputs import INCOME_ISIN as _ISIN
from tests._robinhood_income_inputs import INCOME_OTHER_ACCOUNT as _OTHER_ACCOUNT
from tests._robinhood_income_inputs import INCOME_PAYMENT_DATE as _PAYMENT_DATE
from tests._robinhood_income_inputs import INTEREST_MIXES as _INTEREST_MIXES
from tests._robinhood_income_inputs import NON_US_ISIN as _NON_US_ISIN
from tests._robinhood_income_inputs import NON_US_MARKER_AMOUNT as _NON_US_MARKER_AMOUNT
from tests._robinhood_income_inputs import NON_US_MARKERS as _NON_US_MARKERS
from tests._robinhood_income_inputs import PROGRAM_BANK_INTEREST as _PROGRAM_BANK
from tests._robinhood_income_inputs import ZERO_ACCOUNT as _ZERO_ACCOUNT
from tests._robinhood_income_inputs import ZERO_FILED as _ZERO_FILED
from tests._robinhood_income_inputs import ZERO_MARKERS as _ZERO_MARKERS
from tests._robinhood_income_inputs import ZERO_REFUSED as _ZERO_REFUSED
from tests._robinhood_income_inputs import SweepRow as _Sweep
from tests._robinhood_income_inputs import combined_packet as _combined_packet
from tests._robinhood_income_inputs import form_1042s_amount as _amount
from tests._robinhood_income_inputs import income_base_events as _base_events
from tests._robinhood_income_inputs import income_config as _config
from tests._robinhood_income_inputs import income_event as _event
from tests._robinhood_income_inputs import income_packet as _packet
from tests._robinhood_income_inputs import income_paired as _paired
from tests._robinhood_income_inputs import income_payer_country as _payer_country
from tests._robinhood_income_inputs import non_us_income_config as _non_us_config
from tests._robinhood_income_inputs import non_us_income_events as _non_us_events
from tests._robinhood_income_inputs import packet_with_copies as _packet_with_copies
from tests._robinhood_income_inputs import payer_case as _payer_case
from tests._robinhood_income_inputs import shared_user_config as _shared_config
from tests._robinhood_income_inputs import stock_lending_events as _stock_lending_events
from tests._robinhood_income_inputs import sweep_interest as _sweep_interest
from tests._robinhood_income_inputs import zero_form as _zero_form
from tests._robinhood_income_inputs import zero_rows as _zero_rows
from tests._robinhood_private import (
    load_private_accounts_reconciliation,
)
from tests._robinhood_security import configured_securities


def _compute(
    events: tuple[RobinhoodEvent, ...],
    packets: tuple[Robinhood1042SPacket, ...],
    *,
    program_bank: bool = False,
    report_end: date = _FY_END,
    country: str = "US",
    isin: Isin = _ISIN,
    other_opening_date: date = date(2020, 1, 1),
    other_report_start: date = date(2020, 1, 1),
    registration_lines: tuple[str, ...] = ("TAX PAYER",),
    statement_deltas: Mapping[tuple[str, date, str], tuple[Decimal, Decimal]] | None = None,
    extra_period: tuple[date, date] | None = None,
    sweep_rows: tuple[_Sweep, ...] = (),
    sweep_banks: tuple[object, ...] = (),
    reviewed_fee_source: bool = False,
    reviewed_fee_account: str = _ACCOUNT,
    interest_payer_countries: tuple[RobinhoodInterestPayerCountry, ...] = _ALL_US_PAYERS,
    other_rows: tuple[RobinhoodEvent, ...] = (),
    other_account: str = _OTHER_ACCOUNT,
):
    config = _config(
        country=country,
        isin=isin,
        other_opening_date=other_opening_date,
        reviewed_fee_source=reviewed_fee_source,
        reviewed_fee_account=reviewed_fee_account,
        interest_payer_countries=interest_payer_countries,
        other_account=other_account,
    )
    return compute_robinhood_income(
        _paired(
            events,
            program_bank=program_bank,
            report_end=report_end,
            other_report_start=other_report_start,
            registration_lines=registration_lines,
            statement_deltas=statement_deltas,
            extra_period=extra_period,
            sweep_rows=sweep_rows,
            sweep_banks=sweep_banks,
            other_rows=other_rows,
            other_account=other_account,
        ),
        packets,
        config=config,
        securities=configured_securities(config),
        financial_year_start=_FY_START,
        financial_year_end=_FY_END,
    )


def test_reconciles_gross_dividend_withholding_and_shared_schedules(
    tmp_path,
) -> None:
    result = _compute(_base_events(), (_packet(),))

    assert result.filing_complete
    assert len(result.dividends) == len(result.withholding_taxes) == 1
    assert result.dividends[0].amount_gross == Decimal("100.00")
    assert result.withholding_taxes[0].amount == Decimal("-25.00")
    assert result.dividend_payments[0].source_payment_date == date(2025, 6, 20)
    assert result.dividends[0].payment_date == _PAYMENT_DATE
    assert result.annual_buckets[0].payment_references == (
        result.dividend_payments[0].event_reference,
    )
    assert result.annual_buckets[0].gross_income_usd == Decimal("100")
    assert result.annual_buckets[0].source_gross_income_usd == Decimal("100")
    assert result.annual_buckets[0].withholding_usd == Decimal("25")
    assert result.annual_buckets[0].source_withholding_usd == Decimal("25")
    assert not result.zero_round_unreported_interest_references
    assert "100.00" not in repr(result)
    assert _ACCOUNT not in repr(result)

    assembled = compute_income_schedules(
        _shared_config(tmp_path),
        FinancialYear(2025),
        fx_ctx=TaxFxContext.for_fy(FinancialYear(2025), FxConfig()),
        dividends=list(result.dividends),
        withholding_taxes=list(result.withholding_taxes),
        closed_lots=[],
        interest_credits=list(result.interest_credits),
        interest_payer_country_iso2=None,
    )
    assert len(assembled.os_result.lines) == 1
    assert len(assembled.fsi_result.rows) == 1
    assert len(assembled.form67_result.entries) == 1
    assert len(compute_schedule_tr_from_fsi(assembled.fsi_result).rows) == 1


def test_shared_engine_withholding_carries_paired_dividend_account_index() -> None:
    """#890: Form 67 keys each payment on (source account, ISIN, payment
    date), so the withholding row takes its paired dividend's account."""
    result = _compute(_base_events(), (_packet(),))

    (payment,) = result.withholding_payments
    (dividend,) = result.dividends
    (withholding,) = result.withholding_taxes
    assert payment.dividend_reference.account_index == 0
    assert dividend.source_account_index == 0
    assert withholding.source_account_index == 0
    assert (withholding.isin, withholding.payment_date) == (dividend.isin, dividend.payment_date)


@pytest.mark.parametrize(
    ("amounts", "form_gross"),
    [
        (("2.60", "2.63"), "5.00"),
        (("0.30", "0.30"), "1.00"),
    ],
)
def test_annual_form_rounds_once_after_summing_exact_dividends(
    amounts: tuple[str, str],
    form_gross: str,
) -> None:
    events = (
        _event(RobinhoodEventKind.EQUITY_BUY, 1, date(2025, 1, 2), "-1"),
        _event(RobinhoodEventKind.CASH_DIVIDEND, 2, _PAYMENT_DATE, amounts[0]),
        _event(RobinhoodEventKind.CASH_DIVIDEND, 3, _PAYMENT_DATE, amounts[1]),
    )

    result = _compute(
        events,
        (_packet(gross=form_gross, tax="0.00", rate="0.00"),),
    )

    assert result.filing_complete
    (bucket,) = result.annual_buckets
    assert bucket.gross_income_usd == Decimal(form_gross)
    assert bucket.source_gross_income_usd == sum(map(Decimal, amounts), Decimal())


def test_bounded_zero_tax_interest_form_discrepancy_uses_exact_source_interest(
    tmp_path,
) -> None:
    interest = _event(RobinhoodEventKind.CASH_INTEREST, 4, _PAYMENT_DATE, "0.49")
    packet = _combined_packet(
        _packet(),
        _packet(income_code="01", gross="1.00", tax="0.00", rate="0.00"),
    )

    result = _compute((*_base_events(), interest), (packet,))

    assert result.filing_complete
    assert [bucket.income_code for bucket in result.annual_buckets] == ["06"]
    (discrepancy,) = result.annual_form_discrepancies
    assert discrepancy.direction is (RobinhoodAnnualFormDiscrepancyDirection.FORM_ABOVE_SOURCE)
    assert discrepancy.event_references == (
        RobinhoodIncomeEventReference(0, interest.source_record_index),
    )
    assert not result.zero_round_unreported_interest_references
    assert [item.amount_gross for item in result.interest_credits] == [Decimal("0.49")]
    annual_refs = {
        reference for bucket in result.annual_buckets for reference in bucket.payment_references
    }
    assert annual_refs.isdisjoint(discrepancy.event_references)

    assembled = compute_income_schedules(
        _shared_config(tmp_path),
        FinancialYear(2025),
        fx_ctx=TaxFxContext.for_fy(FinancialYear(2025), FxConfig()),
        dividends=list(result.dividends),
        withholding_taxes=list(result.withholding_taxes),
        closed_lots=[],
        interest_credits=list(result.interest_credits),
        interest_payer_country_iso2="US",
    )
    [fsi] = assembled.fsi_result.rows
    assert fsi.event_count == 2
    assert fsi.income_inr > assembled.os_result.lines[0].amount_gross_inr


def test_bounded_interest_discrepancy_records_form_below_source() -> None:
    interest = _event(RobinhoodEventKind.CASH_INTEREST, 4, _PAYMENT_DATE, "1.51")
    packet = _combined_packet(
        _packet(),
        _packet(income_code="01", gross="1.00", tax="0.00", rate="0.00"),
    )

    result = _compute((*_base_events(), interest), (packet,))

    assert result.filing_complete
    assert result.annual_form_discrepancies[0].direction is (
        RobinhoodAnnualFormDiscrepancyDirection.FORM_BELOW_SOURCE
    )


@pytest.mark.parametrize(
    "packet",
    [
        _combined_packet(
            _packet(),
            _packet(income_code="01", gross="2.00", tax="0.00", rate="0.00"),
        ),
        _combined_packet(
            _packet(gross="99.00"),
            _packet(income_code="01", gross="1.00", tax="0.00", rate="0.00"),
        ),
        _combined_packet(
            _packet(),
            _packet(income_code="01", gross="1.00", tax="0.00", rate="0.00"),
            _packet(income_code="01", gross="1.00", tax="0.00", rate="0.00"),
        ),
        _packet(income_code="01", gross="1.00", tax="0.00", rate="0.00"),
    ],
    ids=["delta-too-large", "dividend-mismatch", "duplicate-code01", "code01-only"],
)
def test_interest_discrepancy_fallback_stays_narrow(
    packet: Robinhood1042SPacket,
) -> None:
    interest = _event(RobinhoodEventKind.CASH_INTEREST, 4, _PAYMENT_DATE, "0.49")

    result = _compute((*_base_events(), interest), (packet,))

    assert not result.annual_form_discrepancies


def test_interest_discrepancy_fallback_requires_known_program_bank_partition() -> None:
    interest = _event(RobinhoodEventKind.CASH_INTEREST, 4, _PAYMENT_DATE, "0.49")
    packet = _combined_packet(
        _packet(),
        _packet(income_code="01", gross="1.00", tax="0.00", rate="0.00"),
    )

    result = _compute((*_base_events(), interest), (packet,), program_bank=True)

    assert any(
        blocker.kind is RobinhoodIncomeBlockerKind.PROGRAM_BANK_PARTITION
        for blocker in result.blockers
    )
    assert not result.annual_form_discrepancies


def test_missing_zero_round_interest_is_explicitly_reconciled() -> None:
    interest = _event(
        RobinhoodEventKind.CASH_INTEREST,
        4,
        _PAYMENT_DATE,
        "0.49",
    )
    result = _compute(
        (*_base_events(with_tax=False), interest),
        (_packet(gross="100.00", tax="0.00", rate="0.00"),),
    )

    assert result.filing_complete
    assert result.zero_round_unreported_interest_references == (
        RobinhoodIncomeEventReference(0, interest.source_record_index),
    )
    assert result.interest_payments[0].interest_credit.amount_gross == Decimal("0.49")


@pytest.mark.parametrize("amounts", [("0.50",), ("0.20", "0.20", "0.20")])
def test_missing_interest_bucket_rejects_annual_total_rounding_to_one(
    amounts: tuple[str, ...],
) -> None:
    events = (
        *_base_events(with_tax=False),
        *(
            _event(
                RobinhoodEventKind.CASH_INTEREST,
                4 + index,
                _PAYMENT_DATE,
                amount,
            )
            for index, amount in enumerate(amounts)
        ),
    )

    result = _compute(
        events,
        (_packet(gross="100.00", tax="0.00", rate="0.00"),),
    )

    assert any(
        blocker.kind is RobinhoodIncomeBlockerKind.ANNUAL_FORM_RECONCILIATION
        for blocker in result.blockers
    )
    assert not result.zero_round_unreported_interest_references


def test_zero_round_exemption_does_not_generalize_to_dividends() -> None:
    events = (
        _event(RobinhoodEventKind.EQUITY_BUY, 1, date(2025, 1, 2), "-1"),
        _event(RobinhoodEventKind.CASH_INTEREST, 2, _PAYMENT_DATE, "1.00"),
        _event(RobinhoodEventKind.CASH_DIVIDEND, 3, _PAYMENT_DATE, "0.11"),
    )

    result = _compute(
        events,
        (_packet(income_code="01", gross="1.00", tax="0.00", rate="0.00"),),
    )

    assert any(
        blocker.kind is RobinhoodIncomeBlockerKind.ANNUAL_FORM_RECONCILIATION
        for blocker in result.blockers
    )


def test_annual_form_whole_dollar_withholding_is_compared_after_sum() -> None:
    """Two USD 0.49 withholdings sum to 0.98, which rounds to the form's USD 1;
    rounding each payment first would give 0 + 0."""
    events = (
        _event(RobinhoodEventKind.EQUITY_BUY, 1, date(2025, 1, 2), "-1"),
        _event(RobinhoodEventKind.CASH_DIVIDEND, 2, _PAYMENT_DATE, "1.96"),
        _event(RobinhoodEventKind.NRA_WITHHOLDING, 3, _PAYMENT_DATE, "-0.49"),
        _event(RobinhoodEventKind.CASH_DIVIDEND, 4, date(2025, 9, 15), "1.96"),
        _event(RobinhoodEventKind.NRA_WITHHOLDING, 5, date(2025, 9, 15), "-0.49"),
    )

    result = _compute(
        events,
        (_packet(gross="4.00", tax="1.00", rate="25.00"),),
    )

    assert result.blockers == ()
    assert result.filing_complete
    (bucket,) = result.annual_buckets
    assert (bucket.gross_income_usd, bucket.withholding_usd) == (4, 1)
    assert bucket.source_gross_income_usd == Decimal("3.92")
    assert bucket.source_withholding_usd == Decimal("0.98")


def test_annual_form_whole_dollar_withholding_short_of_the_form_refuses() -> None:
    """One USD 1.49 withholding rounds to USD 1, not the form's USD 2."""
    events = (
        _event(RobinhoodEventKind.EQUITY_BUY, 1, date(2025, 1, 2), "-1"),
        _event(RobinhoodEventKind.CASH_DIVIDEND, 2, _PAYMENT_DATE, "5.96"),
        _event(RobinhoodEventKind.NRA_WITHHOLDING, 3, _PAYMENT_DATE, "-1.49"),
    )

    result = _compute(
        events,
        (_packet(gross="6.00", tax="2.00", rate="25.00"),),
    )

    assert any(
        blocker.kind is RobinhoodIncomeBlockerKind.ANNUAL_FORM_RECONCILIATION
        for blocker in result.blockers
    )


def test_positive_rate_bucket_accepts_dividend_whose_cent_withholding_is_zero() -> None:
    events = (
        _event(RobinhoodEventKind.EQUITY_BUY, 1, date(2025, 1, 2), "-1"),
        _event(RobinhoodEventKind.CASH_DIVIDEND, 2, _PAYMENT_DATE, "4.00"),
        _event(RobinhoodEventKind.NRA_WITHHOLDING, 3, _PAYMENT_DATE, "-1.00"),
        _event(RobinhoodEventKind.CASH_DIVIDEND, 4, date(2025, 6, 16), "0.01"),
        _event(RobinhoodEventKind.CASH_DIVIDEND, 5, date(2025, 6, 17), "0.01"),
        _event(RobinhoodEventKind.CASH_DIVIDEND, 6, date(2025, 6, 18), "0.01"),
    )

    result = _compute(
        events,
        (_packet(gross="4.00", tax="1.00", rate="25.00"),),
    )

    assert result.filing_complete
    (bucket,) = result.annual_buckets
    assert bucket.source_gross_income_usd == Decimal("4.03")
    assert bucket.source_withholding_usd == Decimal("1.00")


# ---- #1321: Form 1042-S is reconciled against US dividends only ----

_US_REFERENCE = RobinhoodIncomeEventReference(0, 2)
_US_TAX_REFERENCE = RobinhoodIncomeEventReference(0, 3)
_NON_US_REFERENCE = RobinhoodIncomeEventReference(0, 11)


def _non_us_compute(
    events: tuple[RobinhoodEvent, ...],
    packets: tuple[Robinhood1042SPacket, ...] = (_packet(),),
    *,
    config: RobinhoodConfig | None = None,
) -> RobinhoodIncomeResult:
    config = config or _non_us_config()
    return compute_robinhood_income(
        _paired(events),
        packets,
        config=config,
        securities=configured_securities(config),
        financial_year_start=_FY_START,
        financial_year_end=_FY_END,
    )


def _countries(rows: Iterable[Any]) -> list[str]:
    return sorted(str(row.iso_alpha2) for row in rows)


def test_form_1042s_reconciles_us_dividends_beside_a_proven_non_us_dividend(
    tmp_path,
) -> None:
    """Fails on aa7ed98: a US dividend of $100.00 with $25.00 NRA tax, which
    the 2025 code-06 form covers, plus an XQCA dividend of $100.00 on
    2025-06-16 with $15.00 DTAX. The form never counts the non-US dividend, yet
    main required it matched and blocked FSI, TR and Form 67."""
    result = _non_us_compute((*_base_events(), *_non_us_events()))

    assert result.filing_complete
    assert result.blockers == ()
    (bucket,) = result.annual_buckets
    assert bucket.payment_references == (_US_REFERENCE,)
    assert bucket.withholding_references == (_US_TAX_REFERENCE,)
    assert sorted((str(item.isin), item.amount_gross) for item in result.dividends) == [
        (str(_NON_US_ISIN), Decimal("100.00")),
        (str(_ISIN), Decimal("100.00")),
    ]
    assert sorted((str(item.isin), item.amount) for item in result.withholding_taxes) == [
        (str(_NON_US_ISIN), Decimal("-15.00")),
        (str(_ISIN), Decimal("-25.00")),
    ]

    assembled = compute_income_schedules(
        _shared_config(tmp_path),
        FinancialYear(2025),
        fx_ctx=TaxFxContext.for_fy(FinancialYear(2025), FxConfig()),
        dividends=list(result.dividends),
        withholding_taxes=list(result.withholding_taxes),
        closed_lots=[],
        interest_credits=list(result.interest_credits),
        interest_payer_country_iso2=None,
    )
    assert _countries(assembled.fsi_result.rows) == ["CA", "US"]
    assert _countries(compute_schedule_tr_from_fsi(assembled.fsi_result).rows) == ["CA", "US"]
    assert _countries(assembled.form67_result.entries) == ["CA", "US"]


def test_interest_discrepancy_fallback_reconciles_beside_a_non_us_dividend() -> None:
    """Fails on aa7ed98: the zero-tax code-01 bucket prints $1 for $0.49 of
    interest, so the packet reconciles only through the bounded fallback, which
    main also blocked on the non-US dividend."""
    interest = _event(RobinhoodEventKind.CASH_INTEREST, 4, _PAYMENT_DATE, "0.49")
    packet = _combined_packet(
        _packet(),
        _packet(income_code="01", gross="1.00", tax="0.00", rate="0.00"),
    )

    control = _non_us_compute((*_base_events(), interest), (packet,))
    result = _non_us_compute((*_base_events(), interest, *_non_us_events()), (packet,))

    assert control.filing_complete
    assert result.filing_complete
    assert result.blockers == ()
    assert result.annual_buckets == control.annual_buckets
    assert result.annual_buckets[0].payment_references == (_US_REFERENCE,)
    (discrepancy,) = result.annual_form_discrepancies
    assert result.annual_form_discrepancies == control.annual_form_discrepancies
    assert discrepancy.direction is RobinhoodAnnualFormDiscrepancyDirection.FORM_ABOVE_SOURCE
    assert _NON_US_REFERENCE in {item.event_reference for item in result.dividend_payments}


@pytest.mark.parametrize(
    ("interest", "packet"),
    [
        (
            "1.00",
            _combined_packet(
                _packet(),
                _packet(income_code="01", gross="1.00", tax="0.00", rate="0.00"),
            ),
        ),
        ("0.49", _packet()),
    ],
    ids=["code-01-bucket", "zero-round-interest"],
)
def test_reconciled_interest_beside_a_non_us_dividend(
    interest: str, packet: Robinhood1042SPacket
) -> None:
    """Fails on aa7ed98: interest that a code-01 bucket covers, or that sums to
    a whole-dollar zero with no code-01 bucket, reconciles beside the non-US
    dividend as it does without it."""
    events = (
        *_base_events(),
        _event(RobinhoodEventKind.CASH_INTEREST, 4, _PAYMENT_DATE, interest),
    )

    control = _non_us_compute(events, (packet,))
    result = _non_us_compute((*events, *_non_us_events()), (packet,))

    assert control.filing_complete
    assert result.filing_complete
    assert result.blockers == ()
    assert result.annual_buckets == control.annual_buckets
    assert result.zero_round_unreported_interest_references == (
        control.zero_round_unreported_interest_references
    )
    assert not result.annual_form_discrepancies


def _blocker_kinds(result: RobinhoodIncomeResult) -> list[RobinhoodIncomeBlockerKind]:
    return [blocker.kind for blocker in result.blockers]


def test_non_us_dividend_controls_keep_their_results() -> None:
    """The cases #1321 doesn't sanction keep their aa7ed98 results."""
    us_only = _non_us_compute(_base_events())
    fy_end_year = _non_us_compute((*_base_events(), *_non_us_events(day=date(2026, 1, 16))))
    form_counts_both = _non_us_compute(
        (*_base_events(), *_non_us_events()),
        (_packet(gross="200.00", tax="25.00", rate="25.00"),),
    )
    nra_tax = _non_us_compute(
        (
            *_base_events(),
            *_non_us_events(tax_kind=RobinhoodEventKind.NRA_WITHHOLDING),
        )
    )
    issuer_mismatch = _non_us_compute(
        (*_base_events(), *_non_us_events()),
        config=_non_us_config(issuer_country="GB"),
    )
    extra = _non_us_compute((*_base_events(), *_non_us_events()), (_packet(), _packet()))
    amended = _non_us_compute((*_base_events(), *_non_us_events()), (_packet(amended=True),))

    assert us_only.filing_complete
    assert us_only.annual_buckets[0].payment_references == (_US_REFERENCE,)
    assert fy_end_year.filing_complete
    (pending,) = fy_end_year.pending_annual_form_evidence
    assert pending.income_references == (_NON_US_REFERENCE,)
    assert form_counts_both.blockers == (
        RobinhoodIncomeBlocker(
            kind=RobinhoodIncomeBlockerKind.ANNUAL_FORM_RECONCILIATION,
            account_indices=(0,),
            event_references=(_US_REFERENCE, _NON_US_REFERENCE),
            tax_years=(2025,),
            causes=(RobinhoodIncomeBlockerCause.ANNUAL_FORM_SOURCE,),
        ),
    )
    assert _blocker_kinds(nra_tax) == [RobinhoodIncomeBlockerKind.JURISDICTION]
    assert _blocker_kinds(issuer_mismatch) == [RobinhoodIncomeBlockerKind.JURISDICTION]
    assert _blocker_kinds(extra) == [RobinhoodIncomeBlockerKind.ANNUAL_FORM_COVERAGE]
    assert _blocker_kinds(amended) == [RobinhoodIncomeBlockerKind.UNSUPPORTED_FORM_SEMANTICS]
    for refused in (form_counts_both, nra_tax, issuer_mismatch, extra, amended):
        assert not refused.filing_complete
        assert not refused.dividends
        assert not refused.withholding_taxes


@pytest.mark.parametrize(
    ("gross", "filed"), [("100.00", True), ("6283.00", False)], ids=["files", "refuses"]
)
def test_non_us_dividend_amount_reaches_no_blocker_or_repr(gross: str, filed: bool) -> None:
    """The non-US dividend's amount is a marker: neither the filed run nor a
    form whose Box 2 also counts it echoes it."""
    result = _non_us_compute(
        (*_base_events(), *_non_us_events(amount=_NON_US_MARKER_AMOUNT, tax="-927.49")),
        (_packet(gross=gross, tax="25.00", rate="25.00"),),
    )

    assert result.filing_complete is filed
    assert bool(result.blockers) is not filed
    assert_no_markers(
        "\n".join((repr(result), *map(repr, result.blockers), *map(str, result.blockers))),
        *_NON_US_MARKERS,
    )


# ---- #1247: a whole-dollar Form 1042-S $0 that exact dividends prove ----


def _zero_compute(
    dividend: str,
    withholding: str | None,
    *copies: tuple[str, str, str],
    rows: tuple[RobinhoodEvent, ...] | None = None,
    packet: Robinhood1042SPacket | None = None,
):
    return _compute(
        (),
        (_zero_form(*copies) if packet is None else packet,),
        other_account=_ZERO_ACCOUNT,
        other_rows=_zero_rows(dividend, withholding) if rows is None else rows,
    )


@pytest.mark.parametrize(("dividend", "withholding", "form"), _ZERO_FILED.values(), ids=_ZERO_FILED)
def test_whole_dollar_zero_form_box_files_the_exact_events(
    tmp_path,
    dividend: str,
    withholding: str | None,
    form: tuple[str, str, str],
) -> None:
    """A code-06 Box 2 or Box 7a of $0 files when its rate matches the exact
    dividend and withholding, and their sums round once per box (#1247)."""
    result = _zero_compute(dividend, withholding, form)

    assert result.filing_complete
    assert result.blockers == ()
    (bucket,) = result.annual_buckets
    assert (bucket.gross_income_usd, bucket.withholding_usd) == (Decimal(form[0]), Decimal(form[1]))
    assert bucket.source_gross_income_usd == Decimal(dividend)
    assert bucket.source_withholding_usd == Decimal(withholding or "0")
    assert [item.amount_gross for item in result.dividends] == [Decimal(dividend)]
    assert [item.amount for item in result.withholding_taxes] == (
        [] if withholding is None else [-Decimal(withholding)]
    )

    assembled = compute_income_schedules(
        _shared_config(tmp_path),
        FinancialYear(2025),
        fx_ctx=TaxFxContext.for_fy(FinancialYear(2025), FxConfig()),
        dividends=list(result.dividends),
        withholding_taxes=list(result.withholding_taxes),
        closed_lots=[],
        interest_credits=list(result.interest_credits),
        interest_payer_country_iso2=None,
    )
    assert [line.amount_gross_usd for line in assembled.os_result.lines] == [Decimal(dividend)]
    assert len(assembled.fsi_result.rows) == 1
    taxed = int(withholding is not None)
    assert len(assembled.form67_result.entries) == taxed
    assert len(compute_schedule_tr_from_fsi(assembled.fsi_result).rows) == taxed


@pytest.mark.parametrize(
    ("dividend", "withholding", "form"), _ZERO_REFUSED.values(), ids=_ZERO_REFUSED
)
def test_whole_dollar_zero_relaxation_keeps_other_refusals(
    dividend: str,
    withholding: str | None,
    form: tuple[str, str, str],
) -> None:
    """A positive-rate $0 Box 7a still needs each withholding to be the rate
    times its dividend; a zero rate still refuses printed or source tax, and a
    $0 Box 2 printed tax (#1247)."""
    result = _zero_compute(dividend, withholding, form)

    expected = (
        RobinhoodIncomeBlockerKind.UNSUPPORTED_FORM_SEMANTICS
        if form[1] != "0.00"
        else RobinhoodIncomeBlockerKind.ANNUAL_FORM_RECONCILIATION
    )
    assert not result.filing_complete
    assert [blocker.kind for blocker in result.blockers] == [expected]
    assert not result.dividends
    assert not result.withholding_taxes
    assert_no_markers(repr(result) + str(result), *_ZERO_MARKERS)


def test_zero_gross_copy_needs_a_dividend_its_rate_proves() -> None:
    """An empty $0 copy beside the one that covers the dividend refuses (#1247)."""
    result = _zero_compute(
        "100.00",
        "25.00",
        ("100.00", "25.00", "25.00"),
        ("0.00", "0.00", "15.00"),
    )

    assert not result.filing_complete
    assert [blocker.kind for blocker in result.blockers] == [
        RobinhoodIncomeBlockerKind.UNSUPPORTED_FORM_SEMANTICS
    ]
    assert not result.annual_buckets
    assert_no_markers(repr(result) + str(result), *_ZERO_MARKERS)


@pytest.mark.parametrize(
    ("income_code", "kind"),
    [
        ("01", RobinhoodEventKind.CASH_INTEREST),
        ("34", RobinhoodEventKind.STOCK_LENDING_INCOME),
    ],
)
def test_zero_gross_stays_unsupported_outside_code_06(
    income_code: str,
    kind: RobinhoodEventKind,
) -> None:
    """Codes 01 and 34 keep refusing a printed $0 Box 2, even over $0.40 (#1247)."""
    result = _zero_compute(
        "0.40",
        None,
        rows=(
            _event(RobinhoodEventKind.EQUITY_BUY, 2, date(2025, 1, 2), "-1"),
            _event(kind, 3, _PAYMENT_DATE, "0.40"),
        ),
        packet=_packet(
            income_code=income_code,
            gross="0.00",
            tax="0.00",
            rate="0.00",
            account_reference=_ZERO_ACCOUNT,
        ),
    )

    assert not result.filing_complete
    assert RobinhoodIncomeBlockerKind.UNSUPPORTED_FORM_SEMANTICS in {
        blocker.kind for blocker in result.blockers
    }


def test_non_whole_form_amount_is_unsupported_semantics() -> None:
    result = _compute(
        _base_events(with_tax=False),
        (_packet(gross="100.23", tax="0.00", rate="0.00"),),
    )

    assert any(
        blocker.kind is RobinhoodIncomeBlockerKind.UNSUPPORTED_FORM_SEMANTICS
        for blocker in result.blockers
    )


_BLANK = Form1042SAmount(Form1042SAmountKind.BLANK, None)


def test_clean_form_copy_becomes_one_bucket() -> None:
    buckets, blocker = _packet_buckets(0, 0, _packet(), (), ())

    assert blocker is None
    assert len(buckets) == 1


@pytest.mark.parametrize(
    "changes",
    [
        {"gross_income": _BLANK},
        {"gross_income": _amount("100.50")},
        {"federal_tax_withheld": _BLANK},
        {"federal_tax_withheld": _amount("25.50"), "total_withholding_credit": _amount("25.50")},
        {"total_withholding_credit": _BLANK},
        {"total_withholding_credit": _amount("26.00")},
        {"withholding_allowance": _amount("1.00")},
        {"tax_withheld_other_agents": _amount("1.00")},
        {"overwithheld_tax_repaid": _amount("1.00")},
        {"tax_paid_by_agent": _amount("1.00")},
        {"chapter4_tax_rate_percent": Decimal("1.00")},
        {"amended": True},
        {"amendment_number": "1"},
        {"chapter_indicator": "4"},
        {"federal_tax_not_deposited": True},
        {"subsequent_year_withholding": True},
        {"qualified_intermediary": True},
        {"pro_rata_reporting": True},
    ],
)
def test_form_copy_gate_blocks_each_unsupported_box(changes: dict[str, object]) -> None:
    buckets, blocker = _packet_buckets(0, 0, _packet_with_copies(**changes), (), ())

    assert buckets == ()
    assert blocker is not None
    assert blocker.kind is RobinhoodIncomeBlockerKind.UNSUPPORTED_FORM_SEMANTICS


@pytest.mark.parametrize(
    ("amount", "form_gross"),
    [("5.000", "5.00"), ("100000000000000.00", "100000000000000.00")],
)
def test_annual_form_refuses_over_precise_or_unbounded_source_amount(
    amount: str,
    form_gross: str,
) -> None:
    events = (
        _event(RobinhoodEventKind.EQUITY_BUY, 1, date(2025, 1, 2), "-1"),
        _event(RobinhoodEventKind.CASH_DIVIDEND, 2, _PAYMENT_DATE, amount),
    )

    with pytest.raises(RobinhoodIncomeError) as caught:
        _compute(events, (_packet(gross=form_gross, tax="0.00", rate="0.00"),))
    assert str(caught.value) == "annual gross income is not a non-negative whole-cent USD amount"


def test_annual_form_validates_source_before_withholding_arithmetic() -> None:
    events = (
        _event(RobinhoodEventKind.EQUITY_BUY, 1, date(2025, 1, 2), "-1"),
        _event(RobinhoodEventKind.CASH_DIVIDEND, 2, _PAYMENT_DATE, "1E+60"),
        _event(RobinhoodEventKind.NRA_WITHHOLDING, 3, _PAYMENT_DATE, "-2.5E+59"),
    )

    with pytest.raises(RobinhoodIncomeError) as caught:
        _compute(events, (_packet(gross="1E+60", tax="2.5E+59", rate="25.00"),))
    assert str(caught.value) == "annual gross income is not a non-negative whole-cent USD amount"


_UNIDENTIFIED_STOCK_LENDING = (
    "InvariantError: Robinhood activity report, stock-lending row without a security identity"
    " (column Instrument) reached annual-form reconciliation"
    " (context keys: source_row_index=6)"
)


def _stock_lending_source(
    *,
    account_index: int = 0,
    day: date = _PAYMENT_DATE,
    security_config_index: int | None = None,
) -> _StockLendingSource:
    event = replace(
        _event(RobinhoodEventKind.STOCK_LENDING_INCOME, 6, day, "6401.92"),
        instrument="ZQXL",
        description="SYNTHDESC-3308",
    )
    return _StockLendingSource(
        reference=RobinhoodIncomeEventReference(account_index, 6),
        event=event,
        security_config_index=security_config_index,
        issuer_country_iso2=None,
        in_financial_year=True,
    )


def _reconcile_interest_bucket(*stock_lending: _StockLendingSource):
    """Reconcile account 0's 2025 interest form (one $1 bucket) with a $1 credit."""
    buckets, blocker = _packet_buckets(
        0, 0, _packet(income_code="01", gross="1.00", tax="0.00", rate="0.00"), (), ()
    )
    assert blocker is None
    interest = RobinhoodInterestPayment(
        event_reference=RobinhoodIncomeEventReference(0, 4),
        program_bank=None,
        in_financial_year=True,
        interest_credit=InterestCredit(
            credit_date=_PAYMENT_DATE,
            amount_gross=Decimal("1.00"),
            currency="USD",
            description="synthetic interest",
            source_row_index=4,
        ),
    )
    return _reconcile_bucket_events(buckets, (), (interest,), stock_lending, ())


def test_unidentified_stock_lending_at_annual_form_reconciliation_is_an_invariant() -> None:
    """The IDENTITY blocker skips this account-year before reconciliation, so
    reaching it is a bug, not a silent incomplete bucket (#1137)."""
    with pytest.raises(InvariantError) as raised:
        _reconcile_interest_bucket(_stock_lending_source())

    assert str(raised.value) == _UNIDENTIFIED_STOCK_LENDING
    assert_no_markers(str(raised.value), "ZQXL", "SYNTHDESC-3308", "6401.92", "640192")


@pytest.mark.parametrize(
    "stock_lending",
    [
        (_stock_lending_source(account_index=1),),
        (_stock_lending_source(day=date(2024, 6, 15)),),
        (_stock_lending_source(security_config_index=0),),
        (),
    ],
    ids=["other-account", "other-year", "identified", "none"],
)
def test_stock_lending_identity_invariant_sees_only_this_account_year(
    stock_lending: tuple[_StockLendingSource, ...],
) -> None:
    annual, zero_round, complete = _reconcile_interest_bucket(*stock_lending)

    assert complete
    assert zero_round == ()
    assert [bucket.payment_references for bucket in annual] == [
        (RobinhoodIncomeEventReference(0, 4),)
    ]


def test_unidentified_stock_lending_blocks_its_year_before_annual_form_reconciliation() -> None:
    events = (
        *_base_events(),
        replace(
            _event(RobinhoodEventKind.STOCK_LENDING_INCOME, 4, _PAYMENT_DATE, "10.00"),
            instrument="ZQXL",
        ),
    )

    result = _compute(events, (_packet(),))

    assert not result.filing_complete
    assert RobinhoodIncomeBlockerKind.IDENTITY in {blocker.kind for blocker in result.blockers}
    assert result.annual_buckets == ()


def test_mid_fy_opening_clips_required_report_start() -> None:
    opening_date = date(2025, 10, 29)
    complete = _compute(
        _base_events(),
        (_packet(),),
        other_opening_date=opening_date,
        other_report_start=opening_date,
    )
    late = _compute(
        _base_events(),
        (_packet(),),
        other_opening_date=opening_date,
        other_report_start=date(2025, 10, 30),
    )

    assert not any(
        blocker.kind is RobinhoodIncomeBlockerKind.EVIDENCE_COVERAGE
        for blocker in complete.blockers
    )
    assert any(
        blocker.kind is RobinhoodIncomeBlockerKind.EVIDENCE_COVERAGE
        and blocker.account_indices == (1,)
        for blocker in late.blockers
    )


def test_code34_blocks_generic_stock_lending_fee_classification() -> None:
    events = (
        *_base_events(),
        _event(
            RobinhoodEventKind.STOCK_LENDING_INCOME,
            4,
            date(2025, 3, 15),
            "5.00",
        ),
        _event(
            RobinhoodEventKind.STOCK_LENDING_INCOME,
            5,
            _PAYMENT_DATE,
            "10.00",
        ),
    )
    packet = _combined_packet(
        _packet(),
        _packet(income_code="34", gross="15.00", tax="0.00", rate="0.00"),
    )

    result = _compute(events, (packet,))

    assert not result.filing_complete
    assert any(
        blocker.kind is RobinhoodIncomeBlockerKind.STOCK_LENDING_CLASSIFICATION
        for blocker in result.blockers
    )
    assert not result.schedule_os_complete
    assert [item.subtype for item in result.stock_lending_payments] == [
        RobinhoodStockLendingSubtype.SECURITIES_LENDING_FEE,
        RobinhoodStockLendingSubtype.SECURITIES_LENDING_FEE,
    ]
    assert not result.substitute_dividend_incomes
    assert not result.other_source_incomes
    assert not result.unreported_lending_fee_references


def test_code34_rounding_does_not_reclassify_generic_stock_lending_fee() -> None:
    result = _compute(
        (
            _event(RobinhoodEventKind.EQUITY_BUY, 1, date(2025, 1, 2), "-1"),
            _event(
                RobinhoodEventKind.STOCK_LENDING_INCOME,
                2,
                _PAYMENT_DATE,
                "9.60",
            ),
        ),
        (_packet(income_code="34", gross="10.00", tax="0.00", rate="0.00"),),
    )

    assert any(
        blocker.kind is RobinhoodIncomeBlockerKind.STOCK_LENDING_CLASSIFICATION
        for blocker in result.blockers
    )
    assert result.stock_lending_payments[0].subtype is (
        RobinhoodStockLendingSubtype.SECURITIES_LENDING_FEE
    )


def test_reviewed_fee_country_never_bypasses_substitute_payment_blocker() -> None:
    result = _compute(
        _stock_lending_events(),
        (_packet(income_code="34", gross="10.00", tax="0.00", rate="0.00"),),
        reviewed_fee_source=True,
    )

    assert any(
        blocker.kind is RobinhoodIncomeBlockerKind.STOCK_LENDING_CLASSIFICATION
        for blocker in result.blockers
    )
    assert not result.schedule_os_complete
    assert not result.filing_other_source_incomes


@pytest.mark.parametrize(
    "packets",
    [
        (
            _packet(income_code="34", gross="10.00", tax="0.00", rate="0.00"),
            _packet(income_code="34", gross="10.00", tax="0.00", rate="0.00"),
        ),
        (
            _packet(
                account_reference="UNBOUND",
                income_code="34",
                gross="10.00",
                tax="0.00",
                rate="0.00",
            ),
        ),
    ],
)
def test_substitute_form_blocks_os_before_annual_form_binding(
    packets: tuple[Robinhood1042SPacket, ...],
) -> None:
    result = _compute(_stock_lending_events(), packets)

    assert any(
        blocker.kind is RobinhoodIncomeBlockerKind.STOCK_LENDING_CLASSIFICATION
        for blocker in result.blockers
    )
    assert not result.schedule_os_complete
    assert not result.other_source_incomes


def test_non_us_stock_lending_reconciles_form_before_source_country_decision() -> None:
    result = _compute(
        _stock_lending_events(),
        (),
        country="BM",
        isin=Isin("BM99970C1011"),
    )

    assert {blocker.kind for blocker in result.blockers} == {
        RobinhoodIncomeBlockerKind.JURISDICTION
    }
    assert not any(
        blocker.kind is RobinhoodIncomeBlockerKind.ANNUAL_FORM_RECONCILIATION
        for blocker in result.blockers
    )
    (payment,) = result.stock_lending_payments
    assert payment.issuer_country_iso2 == "BM"
    assert payment.source_country_iso2 is None
    assert payment.withholding_agent_country_iso2 is None
    assert payment.subtype is RobinhoodStockLendingSubtype.SECURITIES_LENDING_FEE
    assert payment.annual_bucket_index is None
    assert payment.loan_fee_income is not None
    assert payment.substitute_dividend_income is None
    assert not result.substitute_dividend_incomes


def test_reviewed_us_fee_source_is_independent_of_issuer_and_flows_to_fsi(
    tmp_path,
) -> None:
    blocked = _compute(
        _stock_lending_events(),
        (),
        country="BM",
        isin=Isin("BM99970C1011"),
    )
    result = _compute(
        _stock_lending_events(),
        (),
        country="BM",
        isin=Isin("BM99970C1011"),
        reviewed_fee_source=True,
    )

    assert not any(
        blocker.kind is RobinhoodIncomeBlockerKind.JURISDICTION for blocker in result.blockers
    )
    assert result.filing_complete
    (payment,) = result.stock_lending_payments
    assert payment.issuer_country_iso2 == "BM"
    assert payment.source_country_iso2 == "US"
    assert payment.loan_fee_income is not None
    assert payment.loan_fee_income.source_country_iso2 == "US"
    assert result.other_source_incomes == result.filing_other_source_incomes
    assert blocked.schedule_os_complete

    assembled = compute_income_schedules(
        _shared_config(tmp_path),
        FinancialYear(2025),
        fx_ctx=TaxFxContext.for_fy(FinancialYear(2025), FxConfig()),
        dividends=[],
        withholding_taxes=[],
        closed_lots=[],
        interest_credits=[],
        interest_payer_country_iso2=None,
        other_source_incomes=result.filing_other_source_incomes,
    )

    [fsi] = assembled.fsi_result.rows
    assert fsi.iso_alpha2 == "US"
    assert fsi.head_of_income.value == "Other Sources"
    assert fsi.income_inr == assembled.os_other_income_lines[0].amount_gross_inr
    assert fsi.foreign_tax_inr == fsi.tax_relief_inr == 0
    assert fsi.event_count == 1
    assert not compute_schedule_tr_from_fsi(assembled.fsi_result).rows
    assert not assembled.form67_result.entries


def test_fee_source_review_for_other_account_does_not_clear_jurisdiction() -> None:
    result = _compute(
        _stock_lending_events(),
        (),
        reviewed_fee_source=True,
        reviewed_fee_account=_OTHER_ACCOUNT,
    )

    assert any(
        blocker.kind is RobinhoodIncomeBlockerKind.JURISDICTION for blocker in result.blockers
    )
    assert not result.filing_complete
    assert result.stock_lending_payments[0].source_country_iso2 is None


def test_substitute_dividend_separates_issuer_and_source_country(tmp_path) -> None:
    income = SubstituteDividendIncome(
        symbol=Ticker("TEST"),
        isin=Isin("BM99970C1019"),
        issuer_country_iso2=CountryCode("BM"),
        source_country_iso2=CountryCode("US"),
        payment_date=_PAYMENT_DATE,
        amount_gross=Decimal("10"),
        currency="USD",
        description="synthetic substitute dividend",
    )

    assembled = compute_income_schedules(
        _shared_config(tmp_path),
        FinancialYear(2025),
        fx_ctx=TaxFxContext.for_fy(FinancialYear(2025), FxConfig()),
        dividends=[],
        withholding_taxes=[],
        closed_lots=[],
        interest_credits=[],
        interest_payer_country_iso2=None,
        substitute_dividend_incomes=(income,),
    )

    [line] = assembled.os_substitute_dividend_lines
    assert line.issuer_country_iso2 == "BM"
    assert line.source_country_iso2 == "US"
    assert assembled.os_result.interest_total_inr_rounded == 0
    assert assembled.os_result.other_income_lines[0].nature == (
        "Substitute payment in lieu of dividend"
    )
    [fsi] = assembled.fsi_result.rows
    assert fsi.iso_alpha2 == "US"


def test_fees_are_explicit_reconciliation_only_and_never_net_income() -> None:
    events = (
        *_stock_lending_events(),
        _event(
            RobinhoodEventKind.ADR_FEE,
            3,
            _PAYMENT_DATE,
            "-1.00",
            shares="10",
            rate="0.1",
        ),
        _event(
            RobinhoodEventKind.MANAGEMENT_FEE,
            4,
            _PAYMENT_DATE,
            "-2.00",
        ),
    )
    result = _compute(
        events,
        (),
    )

    assert not result.filing_complete
    assert result.schedule_os_complete
    assert any(
        blocker.kind is RobinhoodIncomeBlockerKind.JURISDICTION for blocker in result.blockers
    )
    assert [item.source_kind for item in result.fee_payments] == [
        RobinhoodEventKind.ADR_FEE,
        RobinhoodEventKind.MANAGEMENT_FEE,
    ]
    assert all(
        item.treatment is RobinhoodFeeTreatment.RECONCILIATION_ONLY_NO_DEDUCTION
        for item in result.fee_payments
    )
    assert result.fee_payments[0].security_config_index == 0
    assert result.fee_payments[1].security_config_index is None
    assert result.other_source_incomes[0].amount_gross == Decimal("10.00")


def test_calendar_year_only_stock_lending_is_classified_for_a3() -> None:
    events = (
        _event(RobinhoodEventKind.EQUITY_BUY, 1, date(2025, 1, 2), "-1"),
        _event(
            RobinhoodEventKind.STOCK_LENDING_INCOME,
            2,
            date(2025, 3, 15),
            "10.00",
        ),
    )
    result = _compute(
        events,
        (),
    )

    assert result.filing_complete
    assert result.schedule_os_complete
    assert not result.substitute_dividend_incomes
    (payment,) = result.stock_lending_payments
    assert payment.subtype is RobinhoodStockLendingSubtype.SECURITIES_LENDING_FEE
    assert payment.security_config_index is not None
    assert payment.loan_fee_income is not None
    assert payment.loan_fee_income.amount_gross == Decimal("10.00")
    assert result.unreported_lending_fee_references


def test_calendar_year_only_dividend_requires_annual_evidence_for_a3() -> None:
    events = (
        _event(RobinhoodEventKind.EQUITY_BUY, 1, date(2025, 1, 2), "-1"),
        _event(RobinhoodEventKind.CASH_DIVIDEND, 2, date(2025, 3, 15), "40.00"),
    )

    blocked = _compute(events, ())
    complete = _compute(
        events,
        (_packet(gross="40.00", tax="0.00", rate="0.00"),),
    )

    assert any(
        blocker.kind is RobinhoodIncomeBlockerKind.ANNUAL_FORM_COVERAGE
        and blocker.tax_years == (2025,)
        for blocker in blocked.blockers
    )
    assert blocked.schedule_os_complete
    assert [item.dividend.amount_gross for item in blocked.dividend_payments] == [Decimal("40.00")]
    assert complete.filing_complete
    assert not complete.dividends
    assert [item.dividend.amount_gross for item in complete.dividend_payments] == [Decimal("40.00")]


@pytest.mark.parametrize(
    ("record_date", "holding_date"),
    [
        (_PAYMENT_DATE, date(2025, 6, 14)),
        (date(2024, 5, 29), date(2024, 5, 28)),
        (date(2024, 5, 28), None),
    ],
)
def test_dividend_holding_date_is_day_before_record_date_once_t1_began(
    record_date: date,
    holding_date: date | None,
) -> None:
    result = _compute(
        (
            _event(RobinhoodEventKind.EQUITY_BUY, 1, date(2024, 1, 2), "-1"),
            _event(RobinhoodEventKind.CASH_DIVIDEND, 2, record_date, "40.00"),
        ),
        (),
    )

    [payment] = result.dividend_payments
    assert payment.source_record_date == record_date
    assert payment.dividend.holding_date == holding_date


def test_zero_withholding_requires_annual_zero_rate_evidence() -> None:
    events = _base_events(with_tax=False)
    complete = _compute(events, (_packet(tax="0.00", rate="0.00"),))
    blocked = _compute(events, ())

    assert complete.filing_complete
    assert len(complete.dividends) == 1
    assert not complete.withholding_taxes
    assert not blocked.dividends
    assert blocked.blockers[0].kind is (RobinhoodIncomeBlockerKind.ANNUAL_FORM_COVERAGE)


def test_program_bank_interest_blocks_unproved_partition() -> None:
    events = (
        _event(
            RobinhoodEventKind.CASH_INTEREST,
            1,
            _PAYMENT_DATE,
            "1.00",
        ),
    )
    result = _compute(
        events,
        (_packet(income_code="01", gross="1.00", tax="0.00", rate="0.00"),),
        program_bank=True,
    )

    assert not result.filing_complete
    assert result.interest_payments[0].program_bank is None
    assert not result.interest_credits
    assert any(
        blocker.kind is RobinhoodIncomeBlockerKind.PROGRAM_BANK_PARTITION
        for blocker in result.blockers
    )


def test_program_bank_interest_pairs_account_wide_by_exact_date_and_amount() -> None:
    bank = _event(
        RobinhoodEventKind.CASH_INTEREST,
        1,
        date(2025, 11, 28),
        "0.04",
    )
    brokerage = _event(
        RobinhoodEventKind.CASH_INTEREST,
        2,
        date(2025, 12, 11),
        "0.19",
    )
    result = _compute(
        (bank, brokerage),
        (),
        program_bank=True,
        sweep_rows=(_sweep_interest(bank.activity_date, "0.04"),),
    )

    by_reference = {
        item.event_reference.event_ordinal: item.program_bank for item in result.interest_payments
    }
    assert by_reference == {1: True, 2: False}
    assert not any(
        blocker.kind is RobinhoodIncomeBlockerKind.PROGRAM_BANK_PARTITION
        for blocker in result.blockers
    )


def test_bank_and_brokerage_interest_reach_a2() -> None:
    day = date(2025, 11, 28)
    bank = _event(RobinhoodEventKind.CASH_INTEREST, 1, day, "0.40")
    brokerage = _event(
        RobinhoodEventKind.CASH_INTEREST,
        2,
        date(2025, 12, 11),
        "0.60",
    )
    paired = _paired(
        (bank, brokerage),
        program_bank=True,
        sweep_rows=(_sweep_interest(day, "0.40"),),
    )
    result = compute_robinhood_income(
        paired,
        (_packet(income_code="01", gross="1.00", tax="0.00", rate="0.00"),),
        config=_config(),
        securities=configured_securities(_config()),
        financial_year_start=_FY_START,
        financial_year_end=_FY_END,
    )

    assert result.filing_complete
    assert [item.amount_gross for item in result.interest_credits] == [
        Decimal("0.40"),
        Decimal("0.60"),
    ]
    bank_evidence = next(
        item for item in _program_bank_evidence(paired, result, 2025) if item.account_index == 0
    )
    assert bank_evidence.interest_references == (
        RobinhoodIncomeEventReference(0, bank.source_record_index),
    )


# ---- interest payer country (#698) ----------------------------------------


@pytest.mark.parametrize("mix", _INTEREST_MIXES)
def test_us_payer_entry_for_every_earned_kind_files_the_interest(mix: str) -> None:
    """The other account earns no interest, so it needs no entry."""
    kinds = _INTEREST_MIXES[mix][2]
    result = _payer_case(mix, (_payer_country(_ACCOUNT, interest_kinds=kinds),))

    assert result.blockers == ()
    assert sum(item.amount_gross for item in result.interest_credits) == Decimal("1.00")
    assert {_kind(item) for item in result.interest_payments} == kinds


def test_no_interest_needs_no_payer_entry() -> None:
    result = _compute(_base_events(), (_packet(),), interest_payer_countries=())

    assert result.filing_complete
    assert result.interest_payments == ()


def _kind(item: RobinhoodInterestPayment) -> RobinhoodInterestKind:
    return (
        RobinhoodInterestKind.PROGRAM_BANK if item.program_bank else RobinhoodInterestKind.BROKERAGE
    )


# name -> (account 0's interest mix, entries, kinds left uncovered)
_UNCOVERED = {
    "missing": ("both", (), _BOTH),
    "other-account-only": ("both", (_payer_country(_OTHER_ACCOUNT),), _BOTH),
    "other-fy-only": ("both", (_payer_country(_ACCOUNT, financial_year=2024),), _BOTH),
    "brokerage-only-entry": (
        "both",
        (_payer_country(_ACCOUNT, interest_kinds=_BROKERAGE),),
        _PROGRAM_BANK,
    ),
    "program-bank-only-entry": (
        "both",
        (_payer_country(_ACCOUNT, interest_kinds=_PROGRAM_BANK),),
        _BROKERAGE,
    ),
    "brokerage-entry-for-bank-interest": (
        "program-bank",
        (_payer_country(_ACCOUNT, interest_kinds=_BROKERAGE),),
        _PROGRAM_BANK,
    ),
    "program-bank-entry-for-brokerage-interest": (
        "brokerage",
        (_payer_country(_ACCOUNT, interest_kinds=_PROGRAM_BANK),),
        _BROKERAGE,
    ),
    "non-us": ("both", (_payer_country(_ACCOUNT, country_iso2="IE"),), _BOTH),
    "india": ("both", (_payer_country(_ACCOUNT, country_iso2="IN"),), _BOTH),
    "float-year": ("both", (replace(_payer_country(), financial_year=2025.0),), _BOTH),
    "none-kinds": ("both", (replace(_payer_country(), interest_kinds=None),), _BOTH),
    "bool-kinds": ("both", (replace(_payer_country(), interest_kinds=False),), _BOTH),
    "string-kinds": ("both", (replace(_payer_country(), interest_kinds="brokerage"),), _BOTH),
    "list-kinds": ("both", (replace(_payer_country(), interest_kinds=list(_BOTH)),), _BOTH),
    "unknown-kind": (
        "both",
        (replace(_payer_country(), interest_kinds=_BOTH | {"unknown"}),),
        _BOTH,
    ),
    "none-kind": (
        "both",
        (replace(_payer_country(), interest_kinds=_BOTH | {None}),),
        _BOTH,
    ),
    "duplicate": ("both", (_payer_country(_ACCOUNT), _payer_country(_ACCOUNT)), _BOTH),
    "conflicting": (
        "both",
        (_payer_country(_ACCOUNT), _payer_country(_ACCOUNT, country_iso2="IE")),
        _BOTH,
    ),
    "blank-source": ("both", (_payer_country(_ACCOUNT, source=" "),), _BOTH),
    "blank-reason": ("both", (_payer_country(_ACCOUNT, reason=""),), _BOTH),
    "non-string-source": ("both", (replace(_payer_country(), source=False),), _BOTH),
    "non-string-reason": ("both", (replace(_payer_country(), reason=False),), _BOTH),
}


@pytest.mark.parametrize("case", _UNCOVERED)
def test_uncovered_interest_blocks_ftc_but_keeps_schedule_os(case: str) -> None:
    mix, entries, uncovered = _UNCOVERED[case]
    covered = _payer_case(mix, _ALL_US_PAYERS)
    result = _payer_case(mix, entries)

    (blocker,) = result.blockers
    assert blocker.kind is RobinhoodIncomeBlockerKind.INTEREST_PAYER_COUNTRY
    assert blocker.account_indices == (0,)
    assert blocker.tax_years == (2025,)
    assert blocker.event_references == tuple(
        item.event_reference for item in covered.interest_payments if _kind(item) in uncovered
    )
    # FSI, TR and Form 67 get no interest; Schedule OS and FA keep all of it.
    assert not result.filing_complete
    assert result.interest_credits == ()
    assert result.schedule_os_complete
    assert result.schedule_os_interest_credits == covered.interest_credits
    assert result.interest_payments == covered.interest_payments
    assert result.annual_buckets == covered.annual_buckets


def test_one_uncovered_interest_bearing_account_blocks_ftc() -> None:
    """Account 1 earns interest too; account 0's entry never covers it."""
    other_interest = _event(RobinhoodEventKind.CASH_INTEREST, 2, date(2026, 1, 9), "0.25")
    only_first = _payer_case("both", (_payer_country(_ACCOUNT),), other_rows=(other_interest,))
    both = _payer_case("both", _ALL_US_PAYERS, other_rows=(other_interest,))

    (blocker,) = only_first.blockers
    assert blocker.kind is RobinhoodIncomeBlockerKind.INTEREST_PAYER_COUNTRY
    assert blocker.account_indices == (1,)
    assert blocker.event_references == (
        RobinhoodIncomeEventReference(1, other_interest.source_record_index),
    )
    assert blocker.tax_years == (2026,)
    assert only_first.interest_credits == ()
    assert only_first.schedule_os_interest_credits == both.interest_credits
    assert both.blockers == ()
    assert len(both.interest_credits) == 3


def test_out_of_fy_interest_needs_no_payer_entry() -> None:
    """Only in-FY interest reaches the shared engine."""
    out_of_fy = _event(RobinhoodEventKind.CASH_INTEREST, 4, date(2025, 3, 14), "0.25")
    result = _compute(
        (*_base_events(), out_of_fy),
        (_packet(),),
        interest_payer_countries=(),
    )

    assert result.blockers == ()
    assert [item.in_financial_year for item in result.interest_payments] == [False]


def test_equal_duplicate_interest_multisets_are_all_bank_interest() -> None:
    day = date(2025, 11, 28)
    result = _compute(
        (
            _event(RobinhoodEventKind.CASH_INTEREST, 1, day, "0.04"),
            _event(RobinhoodEventKind.CASH_INTEREST, 2, day, "0.04"),
        ),
        (),
        program_bank=True,
        sweep_rows=(
            _sweep_interest(day, "0.04"),
            _sweep_interest(day, "0.04"),
        ),
    )

    assert all(item.program_bank is True for item in result.interest_payments)
    assert [(item.event_reference, item.program_bank) for item in result.interest_payments] == [
        (RobinhoodIncomeEventReference(0, 1), True),
        (RobinhoodIncomeEventReference(0, 2), True),
    ]
    assert not any(
        blocker.kind is RobinhoodIncomeBlockerKind.PROGRAM_BANK_PARTITION
        for blocker in result.blockers
    )


@pytest.mark.parametrize(
    "sweep_rows",
    [
        (_sweep_interest(date(2025, 11, 28), "0.04"),),
        (_sweep_interest(date(2025, 11, 28), "0.04", debit=True),),
        (
            _sweep_interest(
                date(2025, 11, 28),
                "0.04",
                description="INTEREST PAYMENT - EXAMPLE BANK",
            ),
        ),
        (
            _sweep_interest(date(2025, 11, 28), "0.04"),
            _sweep_interest(date(2025, 12, 1), "0.01"),
        ),
    ],
)
def test_ambiguous_or_unsupported_sweep_interest_stays_unresolved(
    sweep_rows: tuple[_Sweep, ...],
) -> None:
    day = date(2025, 11, 28)
    result = _compute(
        (
            _event(RobinhoodEventKind.CASH_INTEREST, 1, day, "0.04"),
            _event(RobinhoodEventKind.CASH_INTEREST, 2, day, "0.04"),
        ),
        (),
        program_bank=True,
        sweep_rows=sweep_rows,
    )

    assert all(item.program_bank is None for item in result.interest_payments)
    assert [(item.event_reference, item.program_bank) for item in result.interest_payments] == [
        (RobinhoodIncomeEventReference(0, 1), None),
        (RobinhoodIncomeEventReference(0, 2), None),
    ]
    assert any(
        blocker.kind is RobinhoodIncomeBlockerKind.PROGRAM_BANK_PARTITION
        for blocker in result.blockers
    )


def test_no_sweep_enrollment_proves_brokerage_interest() -> None:
    result = _compute(
        (
            _event(
                RobinhoodEventKind.CASH_INTEREST,
                1,
                _PAYMENT_DATE,
                "1.00",
            ),
        ),
        (),
    )

    assert result.interest_payments[0].program_bank is False
    assert not any(
        blocker.kind is RobinhoodIncomeBlockerKind.PROGRAM_BANK_PARTITION
        for blocker in result.blockers
    )


def test_program_banks_with_empty_sweep_ledger_remain_unresolved() -> None:
    result = _compute(
        (
            _event(
                RobinhoodEventKind.CASH_INTEREST,
                1,
                _PAYMENT_DATE,
                "1.00",
            ),
        ),
        (),
        program_bank=True,
        sweep_banks=(object(),),
    )

    assert result.interest_payments[0].program_bank is None
    assert any(
        blocker.kind is RobinhoodIncomeBlockerKind.PROGRAM_BANK_PARTITION
        for blocker in result.blockers
    )
    assert any(
        blocker.kind is RobinhoodIncomeBlockerKind.INTEREST_PAYER_COUNTRY
        for blocker in result.blockers
    )


@private_test
def test_private_interest_partition_agrees_with_each_statement_sweep_ledger() -> None:
    """Each account partitions against its own sweep ledger or blocks as a whole.

    In an account that resolves, its Program Bank payments and the interest
    credits in its statements' sweep ledger are the same multiset of (date,
    amount), and every other payment is brokerage interest. A blocked account
    leaves each of its payments unresolved.
    """
    paired = load_private_accounts_reconciliation()
    interests = tuple(
        RobinhoodInterestPayment(
            event_reference=RobinhoodIncomeEventReference(
                account_index,
                event.source_record_index,
            ),
            program_bank=None,
            in_financial_year=False,
            interest_credit=InterestCredit(
                credit_date=event.activity_date,
                amount_gross=event.amount_usd,
                currency="USD",
                description=event.description,
                source_row_index=event.source_record_index,
            ),
        )
        for account_index, account in enumerate(paired.accounts)
        for event in account.evidence.events
        if event.kind is RobinhoodEventKind.CASH_INTEREST and event.amount_usd is not None
    )

    partitioned, blockers = _partition_interest_payments(paired, interests)

    assert [replace(item, program_bank=None) for item in partitioned] == list(interests)
    blocker_by_accounts = {blocker.account_indices: blocker for blocker in blockers}
    assert len(blocker_by_accounts) == len(blockers)
    for account_index, account in enumerate(paired.accounts):
        payments = [
            item for item in partitioned if item.event_reference.account_index == account_index
        ]
        blocker = blocker_by_accounts.pop((account_index,), None)
        if blocker is not None:
            assert blocker.kind is RobinhoodIncomeBlockerKind.PROGRAM_BANK_PARTITION
            assert set(blocker.event_references) == {item.event_reference for item in payments}
            assert all(item.program_bank is None for item in payments)
            continue
        assert all(item.program_bank is not None for item in payments)
        if not payments:
            continue
        ledger = Counter(
            (row.transaction_date, row.credit_usd)
            for statement in account.activity.primary_bundle.statements
            for row in statement.sweep_activity
            if " ".join(row.description.upper().split()) == "INTEREST PAYMENT"
        )
        bank = Counter(
            (item.interest_credit.credit_date, item.interest_credit.amount_gross)
            for item in payments
            if item.program_bank
        )
        assert bank == ledger
    assert not blocker_by_accounts


@pytest.mark.parametrize(
    ("packets", "expected"),
    [
        (
            (_packet(), _packet()),
            RobinhoodIncomeBlockerKind.ANNUAL_FORM_COVERAGE,
        ),
        (
            (_packet(amended=True),),
            RobinhoodIncomeBlockerKind.UNSUPPORTED_FORM_SEMANTICS,
        ),
        (
            (_packet(account_reference="UNBOUND"),),
            RobinhoodIncomeBlockerKind.ANNUAL_FORM_IDENTITY,
        ),
        (
            (_packet(income_code="34"),),
            RobinhoodIncomeBlockerKind.UNSUPPORTED_FORM_SEMANTICS,
        ),
    ],
)
def test_annual_form_failures_are_typed_and_emit_nothing(
    packets: tuple[Robinhood1042SPacket, ...],
    expected: RobinhoodIncomeBlockerKind,
) -> None:
    result = _compute(_base_events(), packets)

    assert not result.dividends
    assert not result.withholding_taxes
    assert any(blocker.kind is expected for blocker in result.blockers)


def test_annual_form_binds_by_account_reference_not_statement_contact_text() -> None:
    result = _compute(
        _base_events(),
        (_packet(),),
        registration_lines=(
            "500 Colonial Center Parkway, Suite 100, Lake Mary, FL 32746",
            "help@robinhood.com",
        ),
    )

    assert result.filing_complete
    assert not any(
        blocker.kind is RobinhoodIncomeBlockerKind.ANNUAL_FORM_IDENTITY
        for blocker in result.blockers
    )


def test_annual_form_mixed_summary_and_copy_references_block() -> None:
    packet = _packet()
    packet = replace(
        packet,
        copies=(
            replace(
                packet.copies[0],
                recipient=replace(
                    packet.copies[0].recipient,
                    account_reference=_OTHER_ACCOUNT,
                ),
            ),
            *packet.copies[1:],
        ),
    )

    result = _compute(_base_events(), (packet,))

    assert any(
        blocker.kind is RobinhoodIncomeBlockerKind.ANNUAL_FORM_IDENTITY
        for blocker in result.blockers
    )
    assert not result.dividends


@pytest.mark.parametrize(("field", "value"), [("recipient", "US"), ("agent", "BM")])
def test_annual_form_country_roles_remain_strict(field: str, value: str) -> None:
    packet = _packet()
    copies = tuple(
        (
            replace(copy, recipient=replace(copy.recipient, country_code=value))
            if field == "recipient"
            else replace(copy, agent=replace(copy.agent, country_code=value))
        )
        for copy in packet.copies
    )

    result = _compute(_base_events(), (replace(packet, copies=copies),))

    assert any(
        blocker.kind is RobinhoodIncomeBlockerKind.ANNUAL_FORM_IDENTITY
        for blocker in result.blockers
    )


def test_annual_form_recipient_identity_must_agree_across_forms() -> None:
    first = _packet(gross="50.00", tax="12.50")
    second = _packet(gross="50.00", tax="12.50")
    second = replace(
        second,
        copies=tuple(
            replace(copy, recipient=replace(copy.recipient, name="OTHER RECIPIENT"))
            for copy in second.copies
        ),
    )

    result = _compute(_base_events(), (_combined_packet(first, second),))

    assert any(
        blocker.kind is RobinhoodIncomeBlockerKind.ANNUAL_FORM_IDENTITY
        for blocker in result.blockers
    )


def test_annual_form_agent_identity_must_agree_across_forms() -> None:
    first = _packet(gross="50.00", tax="12.50")
    second = _packet(gross="50.00", tax="12.50")
    second = replace(
        second,
        copies=tuple(
            replace(copy, agent=replace(copy.agent, name="OTHER AGENT")) for copy in second.copies
        ),
    )

    result = _compute(_base_events(), (_combined_packet(first, second),))

    assert any(
        blocker.kind is RobinhoodIncomeBlockerKind.ANNUAL_FORM_IDENTITY
        for blocker in result.blockers
    )


def test_ambiguous_dtax_and_country_divergence_block() -> None:
    first = _event(
        RobinhoodEventKind.CASH_DIVIDEND,
        2,
        _PAYMENT_DATE,
        "50.00",
        shares="5",
    )
    second = _event(
        RobinhoodEventKind.CASH_DIVIDEND,
        3,
        _PAYMENT_DATE,
        "50.00",
        shares="5",
        source_payment_date=date(2025, 6, 20),
    )
    dtax = _event(
        RobinhoodEventKind.FOREIGN_TAX_WITHHOLDING,
        4,
        _PAYMENT_DATE,
        "-25.00",
    )
    events = (
        _event(RobinhoodEventKind.EQUITY_BUY, 1, date(2025, 1, 2), "-1"),
        first,
        second,
        dtax,
    )

    ambiguous = _compute(events, (_packet(),))
    divergent = _compute(_base_events(), (_packet(),), country="CA")

    assert any(
        item.kind is RobinhoodIncomeBlockerKind.PAYMENT_PAIRING for item in ambiguous.blockers
    )
    assert any(item.kind is RobinhoodIncomeBlockerKind.JURISDICTION for item in divergent.blockers)
    assert not ambiguous.dividends
    assert not divergent.dividends


def test_fy_end_year_without_form_uses_complete_source_evidence() -> None:
    events = (
        *_base_events(),
        _event(
            RobinhoodEventKind.CASH_DIVIDEND,
            4,
            date(2026, 1, 15),
            "20.00",
            shares="2",
        ),
        _event(
            RobinhoodEventKind.STOCK_LENDING_INCOME,
            5,
            date(2026, 1, 16),
            "10.00",
        ),
    )
    result = _compute(
        events,
        (_packet(),),
        reviewed_fee_source=True,
    )

    assert result.filing_complete
    assert not any(blocker.tax_years == (2026,) for blocker in result.blockers)
    (pending,) = result.pending_annual_form_evidence
    assert pending.account_index == 0
    assert pending.tax_year == 2026
    assert pending.income_references == (RobinhoodIncomeEventReference(0, 4),)
    assert pending.unpaired_us_dividend_references == pending.income_references
    assert not pending.withholding_references
    assert RobinhoodIncomeEventReference(0, 5) in result.unreported_lending_fee_references
    assert len(result.dividends) == 2


def test_fy_end_year_pending_form_preserves_exact_withholding_reference(
    tmp_path,
) -> None:
    events = (
        *_base_events(),
        _event(RobinhoodEventKind.CASH_DIVIDEND, 4, date(2026, 1, 15), "20.00"),
        _event(RobinhoodEventKind.NRA_WITHHOLDING, 5, date(2026, 1, 15), "-5.00"),
    )

    result = _compute(events, (_packet(),))

    assert result.filing_complete
    (pending,) = result.pending_annual_form_evidence
    assert pending.income_references == (RobinhoodIncomeEventReference(0, 4),)
    assert pending.withholding_references == (RobinhoodIncomeEventReference(0, 5),)
    assert not pending.unpaired_us_dividend_references
    assert any(
        item.event_reference == RobinhoodIncomeEventReference(0, 5)
        for item in result.withholding_payments
    )
    assembled = compute_income_schedules(
        _shared_config(tmp_path),
        FinancialYear(2025),
        fx_ctx=TaxFxContext.for_fy(FinancialYear(2025), FxConfig()),
        dividends=list(result.dividends),
        withholding_taxes=list(result.withholding_taxes),
        closed_lots=[],
        interest_credits=[],
        interest_payer_country_iso2=None,
    )
    [fsi] = assembled.fsi_result.rows
    [form67] = assembled.form67_result.entries
    [tr] = compute_schedule_tr_from_fsi(assembled.fsi_result).rows
    assert fsi.event_count == form67.event_count == 2
    assert tr.foreign_tax_inr_total > 0


def test_supplied_fy_end_year_form_reenables_full_year_coverage() -> None:
    events = (
        *_base_events(),
        _event(RobinhoodEventKind.CASH_DIVIDEND, 4, date(2026, 1, 15), "20.00"),
    )
    current_form = _packet(
        tax_year=2026,
        gross="20.00",
        tax="0.00",
        rate="0.00",
    )

    incomplete = _compute(events, (_packet(), current_form))
    complete = _compute(
        events,
        (_packet(), current_form),
        report_end=date(2026, 12, 31),
    )

    assert any(
        blocker.kind is RobinhoodIncomeBlockerKind.EVIDENCE_COVERAGE
        and blocker.tax_years == (2026,)
        for blocker in incomplete.blockers
    )
    assert not any(bucket.tax_year == 2026 for bucket in incomplete.annual_buckets)
    assert not incomplete.pending_annual_form_evidence
    assert complete.filing_complete
    assert any(
        bucket.tax_year == 2026 and bucket.income_code == "06" for bucket in complete.annual_buckets
    )


def test_duplicate_fy_end_year_forms_remain_blocking() -> None:
    events = (
        *_base_events(),
        _event(RobinhoodEventKind.CASH_DIVIDEND, 4, date(2026, 1, 15), "20.00"),
    )
    current_form = _packet(
        tax_year=2026,
        gross="20.00",
        tax="0.00",
        rate="0.00",
    )

    result = _compute(
        events,
        (_packet(), current_form, current_form),
        report_end=date(2026, 12, 31),
    )

    assert any(
        blocker.kind is RobinhoodIncomeBlockerKind.ANNUAL_FORM_COVERAGE
        and blocker.tax_years == (2026,)
        for blocker in result.blockers
    )


def test_fy_end_year_substitute_form_still_blocks_fee_classification() -> None:
    events = (
        *_base_events(),
        _event(RobinhoodEventKind.STOCK_LENDING_INCOME, 4, date(2026, 1, 15), "10.00"),
    )
    current_form = _packet(
        tax_year=2026,
        income_code="34",
        gross="10.00",
        tax="0.00",
        rate="0.00",
    )

    result = _compute(events, (_packet(), current_form), reviewed_fee_source=True)

    assert any(
        blocker.kind is RobinhoodIncomeBlockerKind.STOCK_LENDING_CLASSIFICATION
        and blocker.tax_years == (2026,)
        for blocker in result.blockers
    )
    assert not result.schedule_os_complete


def test_annual_form_year_boundary_is_derived_from_filing_source_period() -> None:
    source_years = {(0, 2025), (0, 2026)}

    assert _annual_form_years(source_years, set(), date(2025, 12, 31)) == {(0, 2025)}
    assert _annual_form_years(source_years, {(0, 2026)}, date(2025, 12, 31)) == {
        (0, 2025),
        (0, 2026),
    }


def test_out_of_scope_packet_never_promotes_stock_lending_year() -> None:
    events = (
        *_base_events(),
        _event(RobinhoodEventKind.STOCK_LENDING_INCOME, 4, date(2024, 6, 15), "1.00"),
    )
    old_packet = _packet(
        tax_year=2024,
        income_code="34",
        gross="1.00",
        tax="0.00",
        rate="0.00",
    )

    result = _compute(events, (_packet(), old_packet))

    assert result.filing_complete
    assert not any(blocker.tax_years == (2024,) for blocker in result.blockers)
    assert result.excluded_packet_indices == (1,)


@pytest.mark.parametrize(
    ("packet", "expected"),
    [
        (
            _packet(income_code="34", gross="9.00", tax="0.00", rate="0.00"),
            RobinhoodIncomeBlockerKind.STOCK_LENDING_CLASSIFICATION,
        ),
        (
            _packet(income_code="35", gross="10.00", tax="0.00", rate="0.00"),
            RobinhoodIncomeBlockerKind.UNSUPPORTED_FORM_SEMANTICS,
        ),
        (
            _packet(income_code="34", gross="10.00", tax="2.50", rate="25.00"),
            RobinhoodIncomeBlockerKind.UNSUPPORTED_FORM_SEMANTICS,
        ),
        (
            _combined_packet(
                _packet(income_code="34", gross="5.00", tax="0.00", rate="0.00"),
                _packet(income_code="34", gross="5.00", tax="0.00", rate="0.00"),
            ),
            RobinhoodIncomeBlockerKind.STOCK_LENDING_CLASSIFICATION,
        ),
    ],
)
def test_stock_lending_annual_ambiguity_blocks_every_filing_input(
    packet: Robinhood1042SPacket,
    expected: RobinhoodIncomeBlockerKind,
) -> None:
    result = _compute(_stock_lending_events(), (packet,))

    assert any(blocker.kind is expected for blocker in result.blockers)
    assert not result.substitute_dividend_incomes
    assert not result.schedule_os_complete
    assert not result.other_source_incomes


def test_stock_lending_statement_or_source_country_mismatch_blocks() -> None:
    packet = _packet(income_code="34", gross="10.00", tax="0.00", rate="0.00")
    statement_mismatch = _compute(
        _stock_lending_events(),
        (packet,),
        statement_deltas={
            (_ACCOUNT, date(2025, 1, 1), "Stock Lending"): (Decimal("1.00"), Decimal("1.00"))
        },
    )
    source_mismatch = _compute(
        _stock_lending_events(),
        (packet,),
        country="CA",
    )
    isin_mismatch = _compute(
        _stock_lending_events(),
        (packet,),
        isin=Isin("CA99991A1003"),
    )

    assert any(
        blocker.kind is RobinhoodIncomeBlockerKind.STATEMENT_RECONCILIATION
        for blocker in statement_mismatch.blockers
    )
    assert any(
        blocker.kind is RobinhoodIncomeBlockerKind.JURISDICTION
        for blocker in source_mismatch.blockers
    )
    assert any(
        blocker.kind is RobinhoodIncomeBlockerKind.JURISDICTION
        for blocker in isin_mismatch.blockers
    )
    assert not statement_mismatch.substitute_dividend_incomes
    assert not source_mismatch.substitute_dividend_incomes
    assert not isin_mismatch.substitute_dividend_incomes
    assert isin_mismatch.stock_lending_payments[0].subtype is (
        RobinhoodStockLendingSubtype.SECURITIES_LENDING_FEE
    )
    assert isin_mismatch.stock_lending_payments[0].annual_bucket_index is None
    assert isin_mismatch.stock_lending_payments[0].loan_fee_income is not None
    assert isin_mismatch.stock_lending_payments[0].substitute_dividend_income is None


# #1081: every account's statement Income summary is checked for both calendar
# years the FY touches, not only account-years with recognized income.
_TEN = (Decimal("10.00"), Decimal("10.00"))
_POST_FY_PERIOD = (date(2026, 4, 1), date(2026, 6, 30))


def _only_blocker(
    kind: RobinhoodIncomeBlockerKind,
    account_index: int,
    year: int,
    cause: RobinhoodIncomeBlockerCause,
) -> tuple[RobinhoodIncomeBlocker, ...]:
    return (
        RobinhoodIncomeBlocker(
            kind=kind, account_indices=(account_index,), tax_years=(year,), causes=(cause,)
        ),
    )


@pytest.mark.parametrize(
    ("label", "kind", "cause"),
    [
        (
            _CGD,
            RobinhoodIncomeBlockerKind.UNSUPPORTED_INCOME,
            RobinhoodIncomeBlockerCause.CAPITAL_GAINS_DISTRIBUTIONS,
        ),
        (
            "Dividends",
            RobinhoodIncomeBlockerKind.STATEMENT_RECONCILIATION,
            RobinhoodIncomeBlockerCause.STATEMENT_DIVIDENDS,
        ),
        (
            "Interest Earned",
            RobinhoodIncomeBlockerKind.STATEMENT_RECONCILIATION,
            RobinhoodIncomeBlockerCause.STATEMENT_INTEREST,
        ),
        (
            "Stock Lending",
            RobinhoodIncomeBlockerKind.STATEMENT_RECONCILIATION,
            RobinhoodIncomeBlockerCause.STATEMENT_STOCK_LENDING,
        ),
    ],
)
def test_income_free_account_statement_income_blocks(
    label: str, kind: RobinhoodIncomeBlockerKind, cause: RobinhoodIncomeBlockerCause
) -> None:
    """The second account has only an ACH row, yet its 2025 statement prints
    income: no activity row supports it."""
    result = _compute(
        _base_events(),
        (_packet(),),
        statement_deltas={(_OTHER_ACCOUNT, date(2025, 1, 1), label): _TEN},
    )

    assert result.blockers == _only_blocker(kind, 1, 2025, cause)
    assert not result.filing_complete
    assert not result.dividends


def test_buy_only_account_fy_end_year_distribution_blocks() -> None:
    result = _compute(
        (_event(RobinhoodEventKind.EQUITY_BUY, 1, date(2025, 1, 2), "-1"),),
        (),
        statement_deltas={(_ACCOUNT, date(2026, 1, 1), _CGD): _TEN},
    )

    assert result.blockers == _only_blocker(
        RobinhoodIncomeBlockerKind.UNSUPPORTED_INCOME,
        0,
        2026,
        RobinhoodIncomeBlockerCause.CAPITAL_GAINS_DISTRIBUTIONS,
    )


def test_fy_end_year_statement_dividends_without_rows_block() -> None:
    """The account's only dividend is in June 2025; its January to March 2026
    statement prints Dividends anyway."""
    result = _compute(
        _base_events(),
        (_packet(),),
        statement_deltas={(_ACCOUNT, date(2026, 1, 1), "Dividends"): _TEN},
    )

    assert result.blockers == _only_blocker(
        RobinhoodIncomeBlockerKind.STATEMENT_RECONCILIATION,
        0,
        2026,
        RobinhoodIncomeBlockerCause.STATEMENT_DIVIDENDS,
    )


def test_year_to_date_capital_gains_distribution_blocks() -> None:
    result = _compute(
        _base_events(),
        (_packet(),),
        statement_deltas={(_ACCOUNT, date(2025, 1, 1), _CGD): (Decimal(), Decimal("10.00"))},
    )

    assert result.blockers == _only_blocker(
        RobinhoodIncomeBlockerKind.UNSUPPORTED_INCOME,
        0,
        2025,
        RobinhoodIncomeBlockerCause.CAPITAL_GAINS_DISTRIBUTIONS,
    )


def test_income_free_account_reads_statements_only_through_fy_end() -> None:
    """ADR 0006 §8: a statement after the FY doesn't change its requirements."""
    result = _compute(
        _base_events(),
        (_packet(),),
        extra_period=_POST_FY_PERIOD,
        statement_deltas={(_OTHER_ACCOUNT, _POST_FY_PERIOD[0], _CGD): _TEN},
    )

    assert result.filing_complete
    assert result.blockers == ()


def test_income_free_account_with_zero_income_lines_completes() -> None:
    result = _compute(_base_events(), (_packet(),), extra_period=_POST_FY_PERIOD)

    assert result.filing_complete
    assert result.blockers == ()


def test_income_year_still_reads_its_post_fy_statement() -> None:
    """Kept: an account-year with recognized income reads its whole calendar
    year, so a distribution after the FY still blocks FY 2025-26."""
    events = (
        *_base_events(),
        _event(RobinhoodEventKind.CASH_DIVIDEND, 4, date(2026, 2, 10), "20.00", shares="2"),
    )
    clean = _compute(events, (_packet(),), extra_period=_POST_FY_PERIOD)
    result = _compute(
        events,
        (_packet(),),
        extra_period=_POST_FY_PERIOD,
        statement_deltas={(_ACCOUNT, _POST_FY_PERIOD[0], _CGD): _TEN},
    )

    assert clean.blockers == ()
    assert result.blockers == _only_blocker(
        RobinhoodIncomeBlockerKind.UNSUPPORTED_INCOME,
        0,
        2026,
        RobinhoodIncomeBlockerCause.CAPITAL_GAINS_DISTRIBUTIONS,
    )


def test_stock_lending_year_still_reads_its_statements() -> None:
    """Kept: 2024 Stock Lending pulls in the 2024 statement, outside the FY
    and the FA calendar year."""
    period = (date(2024, 1, 1), date(2024, 12, 31))
    events = (
        *_base_events(),
        _event(RobinhoodEventKind.STOCK_LENDING_INCOME, 4, date(2024, 6, 15), "1.00"),
    )
    clean = _compute(events, (_packet(),), extra_period=period)
    result = _compute(
        events,
        (_packet(),),
        extra_period=period,
        statement_deltas={(_ACCOUNT, period[0], _CGD): _TEN},
    )

    assert clean.blockers == ()
    assert result.blockers == _only_blocker(
        RobinhoodIncomeBlockerKind.UNSUPPORTED_INCOME,
        0,
        2024,
        RobinhoodIncomeBlockerCause.CAPITAL_GAINS_DISTRIBUTIONS,
    )


# #1087: a bound Form 1042-S for either FY calendar year is reconciled whether or
# not that account-year has recognized income.
_FULL_2026 = date(2026, 12, 31)


def _blocker_keys(
    result: RobinhoodIncomeResult,
) -> list[tuple[RobinhoodIncomeBlockerKind, tuple[int, ...], tuple[int, ...]]]:
    return [(item.kind, item.account_indices, item.tax_years) for item in result.blockers]


@pytest.mark.parametrize(
    ("packet", "kind", "cause"),
    [
        (
            _packet(account_reference=_OTHER_ACCOUNT, gross="10.00", tax="3.00", rate="30.00"),
            RobinhoodIncomeBlockerKind.ANNUAL_FORM_RECONCILIATION,
            RobinhoodIncomeBlockerCause.ANNUAL_FORM_SOURCE,
        ),
        (
            _packet(
                account_reference=_OTHER_ACCOUNT,
                income_code="01",
                gross="7.00",
                tax="0.00",
                rate="0.00",
            ),
            RobinhoodIncomeBlockerKind.ANNUAL_FORM_RECONCILIATION,
            RobinhoodIncomeBlockerCause.ANNUAL_FORM_SOURCE,
        ),
        (
            _packet(account_reference=_OTHER_ACCOUNT, gross="0.00", tax="0.00", rate="0.00"),
            RobinhoodIncomeBlockerKind.UNSUPPORTED_FORM_SEMANTICS,
            RobinhoodIncomeBlockerCause.UNSUPPORTED_FORM_CONTENT,
        ),
    ],
    ids=["code06", "code01", "all-zero"],
)
def test_income_free_account_year_form_is_reconciled(
    packet: Robinhood1042SPacket,
    kind: RobinhoodIncomeBlockerKind,
    cause: RobinhoodIncomeBlockerCause,
) -> None:
    """The second account has only an ACH row, so it has no 2025 income, yet
    its supplied 2025 form is reconciled rather than set aside."""
    result = _compute(_base_events(), (_packet(), packet))

    assert result.blockers == _only_blocker(kind, 1, 2025, cause)
    assert result.excluded_packet_indices == ()
    assert result.schedule_os_complete


def test_income_free_account_fy_end_year_form_needs_full_year_evidence() -> None:
    form = _packet(
        account_reference=_OTHER_ACCOUNT,
        tax_year=2026,
        gross="10.00",
        tax="3.00",
        rate="30.00",
    )

    through_march = _compute(_base_events(), (_packet(), form))
    full_year = _compute(_base_events(), (_packet(), form), report_end=_FULL_2026)

    assert through_march.blockers == _only_blocker(
        RobinhoodIncomeBlockerKind.EVIDENCE_COVERAGE,
        1,
        2026,
        RobinhoodIncomeBlockerCause.EVIDENCE_SHORT_OF_FORM_YEAR,
    )
    assert full_year.blockers == _only_blocker(
        RobinhoodIncomeBlockerKind.ANNUAL_FORM_RECONCILIATION,
        1,
        2026,
        RobinhoodIncomeBlockerCause.ANNUAL_FORM_SOURCE,
    )
    assert through_march.excluded_packet_indices == full_year.excluded_packet_indices == ()


@pytest.mark.parametrize(
    ("gross", "expected"),
    [
        ("10.00", []),
        (
            "25.00",
            [(RobinhoodIncomeBlockerKind.ANNUAL_FORM_RECONCILIATION, (0,), (2026,))],
        ),
    ],
    ids=["matching", "contradicting"],
)
def test_fy_end_year_form_reconciles_income_after_the_fy(
    gross: str,
    expected: list[tuple[RobinhoodIncomeBlockerKind, tuple[int, ...], tuple[int, ...]]],
) -> None:
    """The account's only 2026 income is a June 2026 dividend, after the FY."""
    events = (
        *_base_events(),
        _event(RobinhoodEventKind.CASH_DIVIDEND, 4, date(2026, 6, 15), "10.00", shares="1"),
    )
    form = _packet(tax_year=2026, gross=gross, tax="0.00", rate="0.00")

    result = _compute(events, (_packet(), form), report_end=_FULL_2026)

    assert _blocker_keys(result) == expected
    assert result.excluded_packet_indices == ()
    assert any(bucket.tax_year == 2026 for bucket in result.annual_buckets) is (not expected)


@pytest.mark.parametrize(
    ("gross", "expected"),
    [
        ("7.00", []),
        (
            "40.00",
            [(RobinhoodIncomeBlockerKind.ANNUAL_FORM_RECONCILIATION, (1,), (2025,))],
        ),
    ],
    ids=["matching", "contradicting"],
)
def test_fy_start_year_form_reconciles_income_before_the_fy(
    gross: str,
    expected: list[tuple[RobinhoodIncomeBlockerKind, tuple[int, ...], tuple[int, ...]]],
) -> None:
    """The second account's only income is February 2025 interest, before the FY."""
    interest = _event(RobinhoodEventKind.CASH_INTEREST, 2, date(2025, 2, 14), "7.00")
    form = _packet(
        account_reference=_OTHER_ACCOUNT,
        income_code="01",
        gross=gross,
        tax="0.00",
        rate="0.00",
    )

    result = _compute(_base_events(), (_packet(), form), other_rows=(interest,))

    assert _blocker_keys(result) == expected
    assert result.excluded_packet_indices == ()
    assert any(bucket.account_index == 1 for bucket in result.annual_buckets) is (not expected)


def test_income_after_the_fy_keeps_the_bounded_interest_discrepancy() -> None:
    """ADR 0006 §8's zero-tax code-01 exception applies to a newly reconciled
    account-year as to any other."""
    events = (
        *_base_events(),
        _event(RobinhoodEventKind.CASH_DIVIDEND, 4, date(2026, 6, 15), "10.00", shares="1"),
        _event(RobinhoodEventKind.CASH_INTEREST, 5, date(2026, 6, 16), "0.49"),
    )
    form = _combined_packet(
        _packet(tax_year=2026, gross="10.00", tax="0.00", rate="0.00"),
        _packet(tax_year=2026, income_code="01", gross="1.00", tax="0.00", rate="0.00"),
    )

    result = _compute(events, (_packet(), form), report_end=_FULL_2026)

    assert result.filing_complete
    (discrepancy,) = result.annual_form_discrepancies
    assert (discrepancy.account_index, discrepancy.tax_year) == (0, 2026)
    assert discrepancy.event_references == (RobinhoodIncomeEventReference(0, 5),)


def _pre_fy_interest_form(**changes: object) -> Robinhood1042SPacket:
    """A code 01 form for the second account's February 2025 interest of $7."""
    packet = _packet(
        account_reference=_OTHER_ACCOUNT,
        income_code="01",
        gross="7.00",
        tax="0.00",
        rate="0.00",
    )
    return replace(
        packet,
        copies=tuple(
            replace(copy, recipient=replace(copy.recipient, **changes)) for copy in packet.copies
        ),
    )


@pytest.mark.parametrize(
    ("packets", "kind"),
    [
        (
            (_pre_fy_interest_form(name="OTHER RECIPIENT"),),
            RobinhoodIncomeBlockerKind.ANNUAL_FORM_IDENTITY,
        ),
        (
            (_pre_fy_interest_form(), _pre_fy_interest_form()),
            RobinhoodIncomeBlockerKind.ANNUAL_FORM_COVERAGE,
        ),
    ],
    ids=["other-recipient", "duplicate"],
)
def test_form_for_income_before_the_fy_keeps_identity_and_coverage_checks(
    packets: tuple[Robinhood1042SPacket, ...], kind: RobinhoodIncomeBlockerKind
) -> None:
    interest = _event(RobinhoodEventKind.CASH_INTEREST, 2, date(2025, 2, 14), "7.00")

    result = _compute(_base_events(), (_packet(), *packets), other_rows=(interest,))

    assert [item.kind for item in result.blockers] == [kind]
    assert result.excluded_packet_indices == ()


def test_supplied_year_reads_every_statement_of_its_calendar_year() -> None:
    """Account 0's only 2026 income is June 2026 interest, after the FY. Its
    2026 form makes 2026 a supplied year, so the April to December statement is
    read too. Without the form, #1081's FY-end cutoff skips that statement."""
    late_2026 = (date(2026, 4, 1), _FULL_2026)
    events = (
        *_base_events(),
        _event(RobinhoodEventKind.CASH_INTEREST, 4, date(2026, 6, 15), "7.00"),
    )
    form = _packet(tax_year=2026, income_code="01", gross="7.00", tax="0.00", rate="0.00")
    distribution = {(_ACCOUNT, late_2026[0], _CGD): _TEN}

    clean = _compute(events, (_packet(), form), extra_period=late_2026)
    refused = _compute(
        events, (_packet(), form), extra_period=late_2026, statement_deltas=distribution
    )
    no_form = _compute(events, (_packet(),), extra_period=late_2026, statement_deltas=distribution)

    assert clean.filing_complete
    assert clean.blockers == ()
    assert refused.blockers == _only_blocker(
        RobinhoodIncomeBlockerKind.UNSUPPORTED_INCOME,
        0,
        2026,
        RobinhoodIncomeBlockerCause.CAPITAL_GAINS_DISTRIBUTIONS,
    )
    assert not refused.filing_complete
    assert not refused.schedule_os_complete
    assert no_form.filing_complete
    assert no_form.blockers == ()
