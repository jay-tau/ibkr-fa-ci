"""The synthetic two-account Robinhood income inputs: the accounts, the
configured security and config, the activity events, the stand-in reconciled
accounts with their statements, the Form 1042-S packets, and the non-US
dividend, sweep interest, interest payer and whole-dollar zero-form cases,
shared by the Robinhood income compute, pipeline and CLI tests (#1778).

The income builders read ``INCOME_SYMBOL`` and ``INCOME_CUSIP``, never the
equal ``FA_*`` constants of ``tests._robinhood_fa_inputs``, so a test that
patches ``FA_SYMBOL`` there doesn't reach them."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, replace
from datetime import date
from decimal import Decimal
from typing import cast

from ibkr_fa.compute.robinhood_income import compute_robinhood_income
from ibkr_fa.config import (
    RobinhoodAccountConfig,
    RobinhoodConfig,
    RobinhoodInterestKind,
    RobinhoodInterestPayerCountry,
    RobinhoodSecurityConfig,
    RobinhoodStockLendingFeeFsiSource,
    load_user_config,
)
from ibkr_fa.models.form_1042s import Form1042SAmount, Form1042SAmountKind
from ibkr_fa.normalize.robinhood.accounts import (
    RobinhoodAccountsReconciliation,
    RobinhoodActivityReportEvidence,
)
from ibkr_fa.parser.robinhood import RobinhoodEvent, RobinhoodEventKind
from ibkr_fa.parser.robinhood_1042s import Robinhood1042SPacket, parse_robinhood_1042s_packet
from ibkr_fa.parser.robinhood_statement import RobinhoodStatementLayout
from ibkr_fa.types import Isin
from tests._robinhood_1042s import robinhood_1042s_text as _packet_text
from tests._robinhood_security import configured_securities, robinhood_security
from tests._robinhood_statements import robinhood_account_config

INCOME_ACCOUNT = "ACCOUNT-TEST"
INCOME_OTHER_ACCOUNT = "OTHER-ACCOUNT"
INCOME_CUSIP = "99971T103"
INCOME_ISIN = Isin("US99971T1034")
INCOME_SYMBOL = "ABCD"
INCOME_FY_START = date(2025, 4, 1)
INCOME_FY_END = date(2026, 3, 31)
INCOME_PAYMENT_DATE = date(2025, 6, 15)
CGD_LABEL = "Capital Gains Distributions"


@dataclass(frozen=True)
class _Income:
    label: str
    this_period_usd: Decimal
    year_to_date_usd: Decimal


@dataclass(frozen=True)
class _Statement:
    period_start: date
    period_end: date
    income: tuple[_Income, ...]
    activities: tuple[object, ...] = ()
    layout: RobinhoodStatementLayout = RobinhoodStatementLayout.LEGACY_NET_ACCOUNT
    sweep_activity: tuple[object, ...] = ()
    sweep_banks: tuple[object, ...] = ()


@dataclass(frozen=True)
class SweepRow:
    description: str
    transaction_date: date
    debit_usd: Decimal | None
    credit_usd: Decimal | None


@dataclass(frozen=True)
class _Bundle:
    account_number: str
    coverage_start: date
    coverage_end: date
    registration_lines: tuple[str, ...]
    statements: tuple[_Statement, ...]


@dataclass(frozen=True)
class _Activity:
    primary_bundle: _Bundle


@dataclass(frozen=True)
class _CashPeriod:
    program_bank_present: bool


@dataclass(frozen=True)
class _Cash:
    periods: tuple[_CashPeriod, ...]


@dataclass(frozen=True)
class _Account:
    evidence: RobinhoodActivityReportEvidence
    activity: _Activity
    cash: _Cash


@dataclass(frozen=True)
class _Paired:
    accounts: tuple[_Account, _Account]


def _account(number: str, *, opening_date: date = date(2020, 1, 1)) -> RobinhoodAccountConfig:
    return robinhood_account_config(
        number, opening_date=opening_date, address="synthetic", zip_code="", source="synthetic"
    )


def _security(*, country: str = "US", isin: Isin = INCOME_ISIN) -> RobinhoodSecurityConfig:
    return robinhood_security(
        symbol=INCOME_SYMBOL,
        cusip=INCOME_CUSIP,
        isin=isin,
        issuer_country_code=country,
        source="synthetic",
    )


def income_payer_country(
    account_number: str = INCOME_ACCOUNT,
    *,
    financial_year: int = 2025,
    country_iso2: str = "US",
    interest_kinds: frozenset[RobinhoodInterestKind] = frozenset(RobinhoodInterestKind),
    source: str = "synthetic reviewed interest terms",
    reason: str = "every payer of this interest is a US entity",
) -> RobinhoodInterestPayerCountry:
    return RobinhoodInterestPayerCountry(
        account_number=account_number,
        financial_year=financial_year,
        country_iso2=country_iso2,
        interest_kinds=interest_kinds,
        source=source,
        reason=reason,
    )


# Both accounts declare US payers for every interest kind, so tests that are
# not about #698 see the same filing-complete results as before it.
ALL_US_PAYERS = (income_payer_country(INCOME_ACCOUNT), income_payer_country(INCOME_OTHER_ACCOUNT))


def income_config(
    *,
    country: str = "US",
    isin: Isin = INCOME_ISIN,
    other_opening_date: date = date(2020, 1, 1),
    reviewed_fee_source: bool = False,
    reviewed_fee_account: str = INCOME_ACCOUNT,
    interest_payer_countries: tuple[RobinhoodInterestPayerCountry, ...] = ALL_US_PAYERS,
    other_account: str = INCOME_OTHER_ACCOUNT,
) -> RobinhoodConfig:
    return RobinhoodConfig(
        accounts=(
            _account(INCOME_ACCOUNT),
            _account(other_account, opening_date=other_opening_date),
        ),
        securities=(_security(country=country, isin=isin),),
        stock_lending_fee_fsi_sources=(
            (
                RobinhoodStockLendingFeeFsiSource(
                    account_number=reviewed_fee_account,
                    financial_year=2025,
                    country_iso2="US",
                    source="synthetic reviewed Robinhood agreement",
                    reason="RHS is the principal loan-fee obligor",
                ),
            )
            if reviewed_fee_source
            else ()
        ),
        interest_payer_countries=interest_payer_countries,
    )


def income_event(
    kind: RobinhoodEventKind,
    ordinal: int,
    day: date,
    amount: str,
    *,
    shares: str = "10",
    rate: str = "10",
    source_payment_date: date | None = None,
) -> RobinhoodEvent:
    instrument = (
        None
        if kind
        in {
            RobinhoodEventKind.CASH_INTEREST,
            RobinhoodEventKind.ACH_DEPOSIT,
            RobinhoodEventKind.MANAGEMENT_FEE,
        }
        else INCOME_SYMBOL
    )

    def build(
        *,
        quantity: Decimal | None = None,
        price_usd: Decimal | None = None,
        issuer_name: str | None = None,
        cusip: str | None = None,
        record_date: date | None = None,
        payment_date: date | None = None,
        description_shares: Decimal | None = None,
        description_rate_usd: Decimal | None = None,
        description_amount_usd: Decimal | None = None,
    ) -> RobinhoodEvent:
        return RobinhoodEvent(
            kind=kind,
            source_record_index=ordinal,
            activity_date=day,
            process_date=day,
            settle_date=day,
            instrument=instrument,
            description=f"synthetic {kind.value}",
            trans_code={
                RobinhoodEventKind.ACH_DEPOSIT: "ACH",
                RobinhoodEventKind.ADR_FEE: "AFEE",
                RobinhoodEventKind.CASH_DIVIDEND: "CDIV",
                RobinhoodEventKind.CASH_INTEREST: "INT",
                RobinhoodEventKind.EQUITY_BUY: "Buy",
                RobinhoodEventKind.FOREIGN_TAX_WITHHOLDING: "DTAX",
                RobinhoodEventKind.NRA_WITHHOLDING: "NRAT",
                RobinhoodEventKind.MANAGEMENT_FEE: "ADVFEE",
                RobinhoodEventKind.STOCK_LENDING_INCOME: "SLIP",
            }[kind],
            quantity=quantity,
            quantity_suffix=None,
            price_usd=price_usd,
            amount_usd=Decimal(amount),
            issuer_name=issuer_name,
            cusip=cusip,
            record_date=record_date,
            payment_date=payment_date,
            description_shares=description_shares,
            description_rate_usd=description_rate_usd,
            description_amount_usd=description_amount_usd,
        )

    if kind is RobinhoodEventKind.EQUITY_BUY:
        return build(
            quantity=Decimal("1"),
            price_usd=Decimal("1"),
            issuer_name="Synthetic Corporation",
            cusip=INCOME_CUSIP,
        )
    if kind in {
        RobinhoodEventKind.ADR_FEE,
        RobinhoodEventKind.CASH_DIVIDEND,
        RobinhoodEventKind.NRA_WITHHOLDING,
    }:
        return build(
            record_date=day,
            payment_date=source_payment_date or day,
            description_shares=Decimal(shares),
            description_rate_usd=Decimal(rate),
        )
    if kind is RobinhoodEventKind.FOREIGN_TAX_WITHHOLDING:
        return build(
            description_amount_usd=Decimal(amount).copy_abs(),
        )
    return build()


def income_base_events(*, with_tax: bool = True) -> tuple[RobinhoodEvent, ...]:
    rows = [
        income_event(RobinhoodEventKind.EQUITY_BUY, 1, date(2025, 1, 2), "-1"),
        income_event(
            RobinhoodEventKind.CASH_DIVIDEND,
            2,
            INCOME_PAYMENT_DATE,
            "100.00",
            source_payment_date=date(2025, 6, 20),
        ),
    ]
    if with_tax:
        rows.append(
            income_event(
                RobinhoodEventKind.NRA_WITHHOLDING,
                3,
                INCOME_PAYMENT_DATE,
                "-25.00",
                source_payment_date=date(2025, 6, 20),
            )
        )
    return tuple(rows)


def stock_lending_events() -> tuple[RobinhoodEvent, ...]:
    return (
        income_event(RobinhoodEventKind.EQUITY_BUY, 1, date(2025, 1, 2), "-1"),
        income_event(RobinhoodEventKind.STOCK_LENDING_INCOME, 2, INCOME_PAYMENT_DATE, "10.00"),
    )


def _income_rows(
    events: tuple[RobinhoodEvent, ...],
    start: date,
    end: date,
) -> tuple[_Income, ...]:
    """A statement's Income lines: This Period, then Year to Date from 1 January."""

    def total(kind: RobinhoodEventKind, first: date) -> Decimal:
        return sum(
            (
                event.amount_usd
                for event in events
                if event.kind is kind
                and first <= event.activity_date <= end
                and event.amount_usd is not None
            ),
            Decimal(),
        )

    year_start = date(end.year, 1, 1)
    return tuple(
        _Income(label, Decimal(), Decimal())
        if kind is None
        else _Income(label, total(kind, start), total(kind, year_start))
        for label, kind in (
            ("Dividends", RobinhoodEventKind.CASH_DIVIDEND),
            (CGD_LABEL, None),
            ("Interest Earned", RobinhoodEventKind.CASH_INTEREST),
            ("Stock Lending", RobinhoodEventKind.STOCK_LENDING_INCOME),
        )
    )


def income_paired(
    events: tuple[RobinhoodEvent, ...],
    *,
    program_bank: bool = False,
    report_end: date = INCOME_FY_END,
    other_report_start: date = date(2020, 1, 1),
    registration_lines: tuple[str, ...] = ("TAX PAYER",),
    statement_deltas: Mapping[tuple[str, date, str], tuple[Decimal, Decimal]] | None = None,
    extra_period: tuple[date, date] | None = None,
    sweep_rows: tuple[SweepRow, ...] = (),
    sweep_banks: tuple[object, ...] = (),
    other_rows: tuple[RobinhoodEvent, ...] = (),
    other_account: str = INCOME_OTHER_ACCOUNT,
) -> RobinhoodAccountsReconciliation:
    """Two reconciled accounts with statements for 2025 and January 2026 to
    ``report_end``, plus ``extra_period`` when given.

    ``statement_deltas`` adds (This Period, Year to Date) amounts to the Income
    line of the statement keyed by account number, period start and label.
    """
    deltas = statement_deltas or {}
    other_events = (
        income_event(
            RobinhoodEventKind.ACH_DEPOSIT,
            1,
            max(date(2025, 4, 1), other_report_start),
            "1.00",
        ),
        *other_rows,
    )

    def account(
        number: str,
        rows: tuple[RobinhoodEvent, ...],
        report_start: date = date(2020, 1, 1),
    ) -> _Account:
        periods = sorted(
            (
                (date(2025, 1, 1), date(2025, 12, 31)),
                (date(2026, 1, 1), report_end),
                *(() if extra_period is None else (extra_period,)),
            )
        )
        coverage_end = max(report_end, periods[-1][1])
        statements = []
        for start, end in periods:
            first = start == date(2025, 1, 1)
            income = tuple(
                replace(
                    item,
                    this_period_usd=item.this_period_usd + delta[0],
                    year_to_date_usd=item.year_to_date_usd + delta[1],
                )
                if (delta := deltas.get((number, start, item.label)))
                else item
                for item in _income_rows(rows, start, end)
            )
            statements.append(
                _Statement(
                    start,
                    end,
                    income,
                    layout=(
                        RobinhoodStatementLayout.DEPOSIT_SWEEP
                        if number == INCOME_ACCOUNT and first and (sweep_rows or sweep_banks)
                        else RobinhoodStatementLayout.LEGACY_NET_ACCOUNT
                    ),
                    sweep_activity=(sweep_rows if number == INCOME_ACCOUNT and first else ()),
                    sweep_banks=(sweep_banks if number == INCOME_ACCOUNT and first else ()),
                )
            )
        bundle = _Bundle(
            number,
            report_start,
            coverage_end,
            registration_lines,
            tuple(statements),
        )
        return _Account(
            evidence=RobinhoodActivityReportEvidence(rows, report_start, coverage_end),
            activity=_Activity(bundle),
            cash=_Cash(
                tuple(
                    _CashPeriod(program_bank and statement.period_start == date(2025, 1, 1))
                    for statement in statements
                )
            ),
        )

    return cast(
        "RobinhoodAccountsReconciliation",
        _Paired(
            (
                account(INCOME_ACCOUNT, events),
                account(other_account, other_events, other_report_start),
            )
        ),
    )


def form_1042s_amount(value: str) -> Form1042SAmount:
    return Form1042SAmount(Form1042SAmountKind.AMOUNT, Decimal(value))


def income_packet(
    *,
    income_code: str = "06",
    gross: str = "100.00",
    tax: str = "25.00",
    rate: str = "25.00",
    account_reference: str = INCOME_ACCOUNT,
    tax_year: int = 2025,
    amended: bool = False,
) -> Robinhood1042SPacket:
    packet = parse_robinhood_1042s_packet(_packet_text())
    copies = tuple(
        replace(
            copy,
            tax_year=tax_year,
            amended=amended,
            amendment_number="1" if amended else None,
            income_code=income_code,
            chapter3_tax_rate_percent=Decimal(rate),
            gross_income=form_1042s_amount(gross),
            federal_tax_withheld=form_1042s_amount(tax),
            total_withholding_credit=form_1042s_amount(tax),
            recipient=replace(copy.recipient, account_reference=account_reference),
        )
        for copy in packet.copies
    )
    summary_row = replace(
        packet.summary.rows[0],
        income_code=income_code,
        chapter3_tax_rate_percent=Decimal(rate),
        gross_income=form_1042s_amount(gross),
        federal_tax_withheld=form_1042s_amount(tax),
        total_withholding_credit=form_1042s_amount(tax),
    )
    return replace(
        packet,
        tax_year=tax_year,
        copies=copies,
        summary=replace(
            packet.summary,
            account_reference=account_reference,
            rows=(summary_row,),
        ),
    )


def combined_packet(
    *packets: Robinhood1042SPacket,
) -> Robinhood1042SPacket:
    base = packets[0]
    copies = tuple(
        replace(
            copy,
            page_ordinal=copy.page_ordinal + packet_index * 10,
            unique_form_identifier=f"{copy.unique_form_identifier}-{packet_index}",
        )
        for packet_index, packet in enumerate(packets)
        for copy in packet.copies
    )
    return replace(
        base,
        copies=copies,
        summary=replace(
            base.summary,
            rows=tuple(row for packet in packets for row in packet.summary.rows),
        ),
    )


def shared_user_config(tmp_path):
    config_path = tmp_path / "user.toml"
    config_path.write_text(
        """\
[account]
number = "U1234567"
holder_name = "Tax Payer"
opening_date = 2020-01-01
status = "BeneficialOwner"

[account.foreign_tins]
US = "123456789"

""",
        encoding="utf-8",
    )
    return load_user_config(config_path)


# #1321: Form 1042-S is reconciled against US dividends only.
_NON_US_SYMBOL = "XQCA"
_NON_US_CUSIP = "99991A100"
NON_US_ISIN = Isin("CA99991A1003")
_NON_US_DAY = date(2025, 6, 16)
# The digit run in the non-US dividend's amount is a privacy marker: it must
# reach no blocker, refusal, stdout, stderr or repr.
NON_US_MARKER_AMOUNT = "6183.27"
NON_US_MARKERS = (NON_US_MARKER_AMOUNT, "6,183.27")


def non_us_income_config(
    *, issuer_country: str = "CA", other_account: str = INCOME_OTHER_ACCOUNT
) -> RobinhoodConfig:
    """:func:`income_config` plus the Canadian security XQCA."""
    config = income_config(other_account=other_account)
    return replace(
        config,
        securities=(
            *config.securities,
            robinhood_security(
                symbol=_NON_US_SYMBOL,
                cusip=_NON_US_CUSIP,
                isin=NON_US_ISIN,
                issuer_country_code=issuer_country,
                source="synthetic",
            ),
        ),
    )


def non_us_income_events(
    *,
    day: date = _NON_US_DAY,
    amount: str = "100.00",
    tax: str = "-15.00",
    tax_kind: RobinhoodEventKind = RobinhoodEventKind.FOREIGN_TAX_WITHHOLDING,
) -> tuple[RobinhoodEvent, ...]:
    """A buy of XQCA, its dividend on ``day`` and the tax withheld from it."""
    return tuple(
        replace(event, instrument=_NON_US_SYMBOL, cusip=event.cusip and _NON_US_CUSIP)
        for event in (
            income_event(RobinhoodEventKind.EQUITY_BUY, 10, date(2025, 1, 3), "-1"),
            income_event(RobinhoodEventKind.CASH_DIVIDEND, 11, day, amount),
            income_event(tax_kind, 12, day, tax),
        )
    )


# #1247: a whole-dollar Form 1042-S $0 that exact dividends prove.
_ZERO_DESCRIPTION = "SYNTHZERODESC"
ZERO_ACCOUNT = "SYNTHZEROACCT"
ZERO_MARKERS = (_ZERO_DESCRIPTION, ZERO_ACCOUNT)

# Each row: (dividend, NRA withholding, (Box 2, Box 7a, chapter 3 rate)).
ZERO_FILED = {
    "box-7a-zero": ("1.00", "0.25", ("1.00", "0.00", "25.00")),
    "box-7a-zero-two-dollars": ("1.96", "0.49", ("2.00", "0.00", "25.00")),
    "box-2-zero": ("0.40", "0.10", ("0.00", "0.00", "25.00")),
    "box-2-zero-zero-rate": ("0.40", None, ("0.00", "0.00", "0.00")),
}
ZERO_REFUSED = {
    "no-withholding-under-a-positive-rate": ("1.00", None, ("1.00", "0.00", "25.00")),
    "wrong-withholding": ("1.00", "0.15", ("1.00", "0.00", "25.00")),
    "zero-rate-under-withholding": ("1.00", "0.25", ("1.00", "0.00", "0.00")),
    "zero-rate-with-printed-tax": ("1.00", None, ("1.00", "1.00", "0.00")),
    "zero-gross-with-printed-tax": ("0.40", "0.80", ("0.00", "1.00", "200.00")),
}


def zero_rows(dividend: str, withholding: str | None) -> tuple[RobinhoodEvent, ...]:
    """Rows 2 on of the other account (row 1 is its ACH deposit): a buy, one
    dividend and its NRA withholding when given, each described SYNTHZERODESC."""
    rows = [
        income_event(RobinhoodEventKind.EQUITY_BUY, 2, date(2025, 1, 2), "-1"),
        income_event(RobinhoodEventKind.CASH_DIVIDEND, 3, INCOME_PAYMENT_DATE, dividend),
    ]
    if withholding is not None:
        rows.append(
            income_event(
                RobinhoodEventKind.NRA_WITHHOLDING, 4, INCOME_PAYMENT_DATE, f"-{withholding}"
            )
        )
    return tuple(replace(row, description=_ZERO_DESCRIPTION) for row in rows)


def zero_form(*copies: tuple[str, str, str]) -> Robinhood1042SPacket:
    """Account SYNTHZEROACCT's 2025 form, one code-06 copy per (Box 2, Box 7a, rate)."""
    packets = [
        income_packet(gross=gross, tax=tax, rate=rate, account_reference=ZERO_ACCOUNT)
        for gross, tax, rate in copies
    ]
    return packets[0] if len(packets) == 1 else combined_packet(*packets)


_SUMMARY_FIELDS = {
    "gross_income": "gross_income",
    "federal_tax_withheld": "federal_tax_withheld",
    "tax_withheld_other_agents": "tax_withheld_other_agents",
    "overwithheld_tax_repaid": "amount_repaid_to_recipient",
    "total_withholding_credit": "total_withholding_credit",
    "tax_paid_by_agent": "tax_paid_by_agent",
    "chapter4_tax_rate_percent": "chapter4_tax_rate_percent",
}


def packet_with_copies(**changes: object) -> Robinhood1042SPacket:
    packet = income_packet()
    (row,) = packet.summary.rows
    summary_changes = {
        _SUMMARY_FIELDS[name]: value for name, value in changes.items() if name in _SUMMARY_FIELDS
    }
    return replace(
        packet,
        copies=tuple(replace(copy, **changes) for copy in packet.copies),
        summary=replace(packet.summary, rows=(replace(row, **summary_changes),)),
    )


def sweep_interest(
    day: date,
    amount: str,
    *,
    description: str = "INTEREST PAYMENT",
    debit: bool = False,
) -> SweepRow:
    value = Decimal(amount)
    return SweepRow(
        description,
        day,
        value if debit else None,
        None if debit else value,
    )


# Interest payer country (#698).
_BANK_DAY = date(2025, 11, 28)
_BROKERAGE_DAY = date(2025, 12, 11)
BROKERAGE_INTEREST = frozenset({RobinhoodInterestKind.BROKERAGE})
PROGRAM_BANK_INTEREST = frozenset({RobinhoodInterestKind.PROGRAM_BANK})
BOTH_INTEREST_KINDS = frozenset(RobinhoodInterestKind)
# name -> (Program Bank amount, brokerage amount, kinds the account earned)
INTEREST_MIXES = {
    "brokerage": (None, "1.00", BROKERAGE_INTEREST),
    "program-bank": ("1.00", None, PROGRAM_BANK_INTEREST),
    "both": ("0.40", "0.60", BOTH_INTEREST_KINDS),
}


def payer_case(
    mix: str,
    interest_payer_countries: tuple[RobinhoodInterestPayerCountry, ...],
    *,
    other_rows: tuple[RobinhoodEvent, ...] = (),
    other_account: str = INCOME_OTHER_ACCOUNT,
):
    """Account 0 earns the ``mix`` interest in CY 2025, with its 1042-S."""
    bank, brokerage, _kinds = INTEREST_MIXES[mix]
    events = tuple(
        income_event(RobinhoodEventKind.CASH_INTEREST, ordinal, day, amount)
        for ordinal, day, amount in ((1, _BANK_DAY, bank), (2, _BROKERAGE_DAY, brokerage))
        if amount is not None
    )
    config = income_config(
        interest_payer_countries=interest_payer_countries, other_account=other_account
    )
    return compute_robinhood_income(
        income_paired(
            events,
            program_bank=bank is not None,
            sweep_rows=() if bank is None else (sweep_interest(_BANK_DAY, bank),),
            other_rows=other_rows,
            other_account=other_account,
        ),
        (income_packet(income_code="01", gross="1.00", tax="0.00", rate="0.00"),),
        config=config,
        securities=configured_securities(config),
        financial_year_start=INCOME_FY_START,
        financial_year_end=INCOME_FY_END,
    )
