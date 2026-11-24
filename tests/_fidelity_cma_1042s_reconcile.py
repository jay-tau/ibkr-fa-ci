"""The Fidelity CMA Form 1042-S reconciliation inputs that several test modules
share: :func:`_packet`, a three-record 2025 packet (income codes 01 and 06) built
from :func:`_record` and :func:`_dollars`; :func:`_statements`, twelve 2025 monthly
statements plus the following January; :func:`_year_end`, the matching year-end
income summary; and ``JANUARY_2025_REFUSAL``, the tax year 2025 late-start refusal
text, written out here rather than read from ``src``.

Every value is synthetic.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import date
from decimal import Decimal

from ibkr_fa.models.form_1042s import (
    Form1042SAmount,
    Form1042SAmountKind,
    Form1042SCopy,
    Form1042SCopyType,
)
from ibkr_fa.parser.fidelity_cma import (
    CmaIncomeCredit,
    CmaIncomeKind,
    CmaStatement,
    CmaWithholding,
    CmaYearEndIncomeSummary,
)
from ibkr_fa.parser.fidelity_cma_1042s import (
    Form1042SPacket,
    Form1042SRecord,
)
from tests._fidelity_cma import monthly_statement as _base_statement
from tests._fidelity_cma import year_end_summary
from tests._fidelity_cma_1042s import packet


def _dollars(value: int) -> Form1042SAmount:
    return Form1042SAmount(Form1042SAmountKind.AMOUNT, Decimal(value))


def _record(
    key: str,
    code: str,
    exemption: str,
    rate: str,
    gross: int,
    federal_tax: int,
) -> Form1042SRecord:
    return Form1042SRecord(
        record_key=key,
        form=Form1042SCopy(
            copy_type=Form1042SCopyType.B,
            unique_form_identifier=None,
            tax_year=2025,
            amended=False,
            amendment_number=None,
            income_code=code,
            gross_income=_dollars(gross),
            chapter_indicator=None,
            chapter3_exemption_code=exemption,
            chapter3_tax_rate_percent=Decimal(rate),
            chapter4_exemption_code="15",
            chapter4_tax_rate_percent=Decimal("0.00"),
            withholding_allowance=_dollars(0),
            net_income=_dollars(0),
            federal_tax_withheld=_dollars(federal_tax),
            federal_tax_not_deposited=None,
            subsequent_year_withholding=None,
            qualified_intermediary=None,
            tax_withheld_other_agents=_dollars(0),
            overwithheld_tax_repaid=_dollars(0),
            total_withholding_credit=_dollars(federal_tax),
            tax_paid_by_agent=_dollars(0),
            pro_rata_reporting=None,
        ),
    )


def _packet() -> Form1042SPacket:
    return packet(
        _record("01:0.00", "01", "02", "0.00", 2, 0),
        _record("06:0.00", "06", "02", "0.00", 2, 0),
        _record("06:25.00", "06", "00", "25.00", 8, 2),
    )


def _statements() -> tuple[CmaStatement, ...]:
    statements = [_base_statement(2025, month) for month in range(1, 13)]
    following_january = _base_statement(2026, 1)

    interest = tuple(
        CmaIncomeCredit(
            date(2025, 1, day),
            CmaIncomeKind.CREDIT_INTEREST,
            Decimal("0.10"),
            None,
        )
        for day in range(1, 15)
    )
    statements[0] = replace(statements[0], income_credits=interest)

    no_wht = tuple(
        CmaIncomeCredit(
            date(2025, 2, day),
            CmaIncomeKind.SPAXX_DIVIDEND,
            Decimal("1.24"),
            "99971W106",
        )
        for day in (1, 2)
    )
    statements[1] = replace(statements[1], income_credits=no_wht)

    with_wht = tuple(
        CmaIncomeCredit(
            date(2025, 3, day),
            CmaIncomeKind.SPAXX_DIVIDEND,
            Decimal("1.00"),
            "99971W106",
        )
        for day in range(1, 9)
    )
    taxes = tuple(CmaWithholding(date(2025, 3, day), Decimal("-0.25")) for day in range(1, 9))
    statements[2] = replace(
        statements[2],
        income_credits=with_wht,
        withholdings=taxes,
    )

    following_january = replace(
        following_january,
        income_credits=(
            CmaIncomeCredit(
                date(2025, 12, 31),
                CmaIncomeKind.CREDIT_INTEREST,
                Decimal("0.10"),
                None,
            ),
        ),
    )
    return (*statements, following_january)


def _year_end() -> CmaYearEndIncomeSummary:
    return year_end_summary(dividends_usd="10.48", interest_usd="1.50", withholding_usd="-2.00")


# The refusal of tax year 2025 statements that start after 1 January with a
# carried balance, before any caller's clauses (#1515).
JANUARY_2025_REFUSAL = (
    "Fidelity CMA Form 1042-S reconciliation refused: statement coverage for Form "
    "1042-S tax year 2025 must start by 1 January 2025, or at the account's first "
    "statement if it opened later with no carried balance; the statements start after "
    "1 January 2025 with a carried balance, so add every monthly statement from 1 January "
    "2025 (or from the account's first statement) up to the earliest one given"
)
