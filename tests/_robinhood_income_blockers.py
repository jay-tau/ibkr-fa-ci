"""Each Robinhood income blocker cause's synthetic case and line (#1487),
shared by the Robinhood income blocker and refusal tests (#1798).

:data:`INCOME_BLOCKER_CASES` builds one case per
``RobinhoodIncomeBlockerCause`` with :func:`income_case`, and
:data:`INCOME_BLOCKER_LINES` is each cause's blocker line, word for word.
:func:`compute_income_case` runs the real income compute on a case, and
:func:`run_income_command` runs a command on it behind the existing pipeline
stubs. :data:`FILLED_PLACEHOLDERS`, :func:`loaded_remedy_entry` and
:func:`income_blocker_remedy` follow a cause's printed remedy, and
:func:`unconfigured_security_entry` follows the unconfigured-security one.
Synthetic inputs only.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace
from datetime import date
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

import ibkr_fa.pipeline_robinhood as pipeline
import ibkr_fa.pipeline_robinhood_core as core
from ibkr_fa.compute.robinhood_income import (
    RobinhoodIncomeBlockerCause,
    RobinhoodIncomeResult,
    compute_robinhood_income,
)
from ibkr_fa.config import RobinhoodConfig
from ibkr_fa.parser.robinhood import RobinhoodEventKind
from ibkr_fa.parser.robinhood_1042s import Robinhood1042SPacket
from ibkr_fa.types import Isin
from tests._robinhood_cli import robinhood_main as _main
from tests._robinhood_income_inputs import (
    CGD_LABEL,
    INCOME_ACCOUNT,
    INCOME_FY_END,
    INCOME_FY_START,
    INCOME_ISIN,
    INCOME_OTHER_ACCOUNT,
    INCOME_PAYMENT_DATE,
    form_1042s_amount,
    income_base_events,
    income_config,
    income_event,
    income_packet,
    income_paired,
    packet_with_copies,
    stock_lending_events,
)
from tests._robinhood_pipeline_stubs import stub_income_bundle as _stub_income_bundle
from tests._robinhood_remedies import loaded_entry, security_entry
from tests._robinhood_security import configured_securities

_Cause = RobinhoodIncomeBlockerCause
_K = RobinhoodEventKind
_event = income_event
_packet = income_packet
_DAY = INCOME_PAYMENT_DATE
INCOME_BUY = _event(_K.EQUITY_BUY, 1, date(2025, 1, 2), "-1")
_NRA = _event(_K.NRA_WITHHOLDING, 3, _DAY, "-25.00", source_payment_date=date(2025, 6, 20))
STATEMENT_MARKER = "4321.09"
STATEMENT_MARKER_DELTA = (Decimal(STATEMENT_MARKER), Decimal(STATEMENT_MARKER))
OS_BY_HAND = "; complete Schedule OS, Schedule FSI, Schedule TR and Form 67 by hand"
_FTC = "; complete Schedule FSI, Schedule TR and Form 67 by hand"
OR_OS_BY_HAND = ", or complete Schedule OS, Schedule FSI, Schedule TR and Form 67 by hand"
OR_FTC_BY_HAND = ", or complete Schedule FSI, Schedule TR and Form 67 by hand"

type IncomeCase = tuple[RobinhoodConfig, Any, tuple[Robinhood1042SPacket, ...]]


def income_case(
    events: tuple[Any, ...] | None = None,
    packets: tuple[Robinhood1042SPacket, ...] = (_packet(),),
    *,
    config: RobinhoodConfig | None = None,
    **paired: Any,
) -> IncomeCase:
    """``events`` (default: a June 2025 dividend and its NRA withholding) in
    the first account, the second holding only an ACH deposit."""
    return (
        config or income_config(),
        income_paired(income_base_events() if events is None else events, **paired),
        packets,
    )


def compute_income_case(case: IncomeCase) -> RobinhoodIncomeResult:
    config, paired, packets = case
    return compute_robinhood_income(
        paired,
        packets,
        config=config,
        securities=configured_securities(config),
        financial_year_start=INCOME_FY_START,
        financial_year_end=INCOME_FY_END,
    )


def _with_copies(packet: Robinhood1042SPacket, **recipient: Any) -> Robinhood1042SPacket:
    """``packet`` with each copy's recipient changed by ``recipient``."""
    return replace(
        packet,
        copies=tuple(
            replace(copy, recipient=replace(copy.recipient, **recipient)) for copy in packet.copies
        ),
    )


def _summary_gross(packet: Robinhood1042SPacket, gross: str) -> Robinhood1042SPacket:
    """``packet`` whose summary page reports ``gross`` beside unchanged copies."""
    (row,) = packet.summary.rows
    row = replace(row, gross_income=form_1042s_amount(gross))
    return replace(packet, summary=replace(packet.summary, rows=(row,)))


def _statement(label: str) -> IncomeCase:
    """The first account's 2025 statement prints ``label`` income no row supports."""
    return income_case(
        statement_deltas={(INCOME_ACCOUNT, date(2025, 1, 1), label): STATEMENT_MARKER_DELTA}
    )


_OTHER_2026_FORM = _packet(
    account_reference=INCOME_OTHER_ACCOUNT,
    tax_year=2026,
    gross="10.00",
    tax="3.00",
    rate="30.00",
)
_DIVIDEND = _event(_K.CASH_DIVIDEND, 2, _DAY, "100.00", source_payment_date=date(2025, 6, 20))

# Each cause's synthetic case. The FTC-only ones raise no blocker that stops
# Schedule OS, so robinhood-income reaches its FTC reasons.
INCOME_BLOCKER_CASES: dict[RobinhoodIncomeBlockerCause, Callable[[], IncomeCase]] = {
    _Cause.ANNUAL_FORM_MISSING: lambda: income_case(packets=()),
    _Cause.ANNUAL_FORM_SET_ASIDE: lambda: income_case(
        packets=(_with_copies(_packet(), country_code="US"),)
    ),
    _Cause.ANNUAL_FORM_DUPLICATED: lambda: income_case(packets=(_packet(), _packet())),
    _Cause.ANNUAL_FORM_ACCOUNT_UNBOUND: lambda: income_case(
        packets=(_packet(), _packet(account_reference="SYNTHETIC-UNKNOWN"))
    ),
    _Cause.ANNUAL_FORM_PARTIES: lambda: income_case(
        packets=(_with_copies(_packet(), country_code="US"),)
    ),
    _Cause.ANNUAL_FORMS_DISAGREE: lambda: income_case(
        packets=(
            _packet(),
            packet_with_copies(agent=replace(_packet().copies[0].agent, phone="999-999-9998")),
        )
    ),
    _Cause.ANNUAL_FORM_SUMMARY: lambda: income_case(packets=(_summary_gross(_packet(), "99.00"),)),
    _Cause.ANNUAL_FORM_SOURCE: lambda: income_case(packets=(_packet(gross="99.00"),)),
    # The loader refuses this first (see the PR): only the harness reaches it.
    _Cause.EVIDENCE_SHORT_OF_FINANCIAL_YEAR: lambda: income_case(report_end=date(2026, 2, 28)),
    _Cause.EVIDENCE_SHORT_OF_FORM_YEAR: lambda: income_case(packets=(_packet(), _OTHER_2026_FORM)),
    _Cause.UNCONFIGURED_SECURITY: lambda: income_case(
        config=replace(income_config(), securities=())
    ),
    _Cause.UNMATCHED_SYMBOL: lambda: income_case(
        (
            *income_base_events(),
            replace(_event(_K.CASH_DIVIDEND, 4, _DAY, "10.00"), instrument="ZQXL"),
        )
    ),
    _Cause.ISSUER_COUNTRY: lambda: income_case(config=income_config(country="CA")),
    _Cause.US_WITHHOLDING_ON_NON_US_DIVIDEND: lambda: income_case(
        config=income_config(country="CA", isin=Isin("CA99991A1003"))
    ),
    _Cause.LENDING_FEE_SOURCE: lambda: income_case(stock_lending_events(), ()),
    _Cause.WITHHOLDING_WITHOUT_DIVIDEND: lambda: income_case((INCOME_BUY, _NRA), ()),
    _Cause.WITHHOLDING_SEVERAL_DIVIDENDS: lambda: income_case(
        (INCOME_BUY, _DIVIDEND, _NRA, replace(_DIVIDEND, source_record_index=4)), ()
    ),
    _Cause.DIVIDEND_SEVERAL_WITHHOLDINGS: lambda: income_case(
        (*income_base_events(), replace(_NRA, source_record_index=4)), ()
    ),
    _Cause.STATEMENT_SPANS_YEARS: lambda: income_case(
        extra_period=(date(2024, 12, 1), date(2025, 1, 31))
    ),
    _Cause.STATEMENT_DIVIDENDS: lambda: _statement("Dividends"),
    _Cause.STATEMENT_INTEREST: lambda: _statement("Interest Earned"),
    _Cause.STATEMENT_STOCK_LENDING: lambda: _statement("Stock Lending"),
    _Cause.SUBSTITUTE_PAYMENT: lambda: income_case(
        packets=(_packet(income_code="34", tax="0.00", rate="0.00"),)
    ),
    _Cause.UNSUPPORTED_FORM_CONTENT: lambda: income_case(packets=(_packet(income_code="02"),)),
    _Cause.CAPITAL_GAINS_DISTRIBUTIONS: lambda: _statement(CGD_LABEL),
}

_INDIVIDUAL = "(Individual account, 2025)"
_STATEMENT_LINE = (
    "statement-reconciliation (Individual account, 2025): a statement's Income summary "
    "{line} line (this period or year to date) disagrees with the activity report's {income} "
    f"for that period, so ibkr-fa can't tell which is right{OS_BY_HAND}"
)
# Each cause's line, word for word.
INCOME_BLOCKER_LINES: dict[RobinhoodIncomeBlockerCause, str] = {
    _Cause.ANNUAL_FORM_MISSING: (
        f"annual-form-coverage {_INDIVIDUAL}: no Form 1042-S was supplied for the listed "
        "account and year, and its income that year needs one. Put the Form 1042-S Robinhood "
        f"issued for it in the input's tax-forms folder and re-run{OR_FTC_BY_HAND}"
    ),
    _Cause.ANNUAL_FORM_SET_ASIDE: (
        f"annual-form-coverage {_INDIVIDUAL}: the Form 1042-S supplied for the listed account "
        "and year failed its annual-form-identity check, so no form covers that year's "
        f"income{_FTC}"
    ),
    _Cause.ANNUAL_FORM_DUPLICATED: (
        f"annual-form-coverage {_INDIVIDUAL}: more than one Form 1042-S was supplied for the "
        f"listed account and year, and ibkr-fa can't tell which one applies{_FTC}"
    ),
    _Cause.ANNUAL_FORM_ACCOUNT_UNBOUND: (
        "annual-form-identity (2025): a supplied Form 1042-S names neither configured Robinhood "
        "account, or names different accounts on its summary and form pages, so ibkr-fa can't "
        f"tell whose form it is{_FTC}"
    ),
    _Cause.ANNUAL_FORM_PARTIES: (
        f"annual-form-identity {_INDIVIDUAL}: a Form 1042-S doesn't name an India-resident "
        "recipient and a US withholding agent, or the account's statements name no registered "
        f"holder, so the form can't be matched to this filer{_FTC}"
    ),
    _Cause.ANNUAL_FORMS_DISAGREE: (
        f"annual-form-identity {_INDIVIDUAL}: the supplied Forms 1042-S have different "
        "recipient or withholding-agent details, so ibkr-fa can't reconcile their "
        f"identities{_FTC}"
    ),
    _Cause.ANNUAL_FORM_SUMMARY: (
        f"annual-form-reconciliation {_INDIVIDUAL}: a Form 1042-S's summary page disagrees "
        f"with its form pages, so ibkr-fa can't tell which figures apply{_FTC}"
    ),
    _Cause.ANNUAL_FORM_SOURCE: (
        f"annual-form-reconciliation {_INDIVIDUAL}: a Form 1042-S doesn't reconcile to the "
        "dividends, interest and Stock Lending income in the activity report for its year, so "
        f"ibkr-fa can't tell which is right{_FTC}"
    ),
    _Cause.EVIDENCE_SHORT_OF_FINANCIAL_YEAR: (
        "evidence-coverage (Individual and Managed Individual accounts): the account's activity "
        "report or statements don't cover the whole financial year. Supply an activity report "
        "and monthly statements from the account's opening date to the end of the financial "
        "year, with that report's [[robinhood.reports]] start_date and end_date set to match, "
        f"and re-run{OR_OS_BY_HAND}"
    ),
    _Cause.EVIDENCE_SHORT_OF_FORM_YEAR: (
        "evidence-coverage (Managed Individual account, 2026): the account's activity report or "
        "statements stop before 31 December of the listed year, whose Form 1042-S ibkr-fa must "
        "reconcile. Supply an activity report and monthly statements through 31 December of "
        "that year, with that report's [[robinhood.reports]] end_date set to match, and "
        f"re-run{OR_FTC_BY_HAND}"
    ),
    _Cause.UNCONFIGURED_SECURITY: (
        f"identity {_INDIVIDUAL}: a dividend or Stock Lending payment is in a security that has "
        "no [[robinhood.securities]] entry and wasn't identified automatically. Add a "
        "[[robinhood.securities]] entry for it with symbol, cusip, isin, listing_exchange, "
        "sub_category, asset_class, issuer_country_code, is_depositary_receipt and source, and "
        f"re-run{OR_OS_BY_HAND}"
    ),
    _Cause.UNMATCHED_SYMBOL: (
        f"identity {_INDIVIDUAL}: a dividend or Stock Lending payment's symbol matches no "
        "security the activity report or statements identify, so ibkr-fa can't tell which "
        f"security paid it{OS_BY_HAND}"
    ),
    _Cause.ISSUER_COUNTRY: (
        f"jurisdiction {_INDIVIDUAL}: a dividend's security has an issuer country that differs "
        f"from its ISIN's country, so the dividend's source country is unproven{_FTC}"
    ),
    _Cause.US_WITHHOLDING_ON_NON_US_DIVIDEND: (
        f"jurisdiction {_INDIVIDUAL}: US NRA tax was withheld from a dividend whose issuer "
        f"isn't in the US, which ibkr-fa can't explain{_FTC}"
    ),
    _Cause.LENDING_FEE_SOURCE: (
        f"jurisdiction {_INDIVIDUAL}: Stock Lending income in the financial year has no "
        "reviewed source country. Add one [[robinhood.stock_lending_fee_fsi_sources]] entry for "
        'the account with account_number = "<account number>", financial_year = <FY start '
        "year, e.g. 2025 for 2025-26>, country_iso2 = \"<two-letter code of the fee's source "
        'country, not IN>", source = "<what you reviewed>", reason = "<why that is the source '
        f'country>" and re-run{OR_FTC_BY_HAND}'
    ),
    _Cause.WITHHOLDING_WITHOUT_DIVIDEND: (
        f"payment-pairing {_INDIVIDUAL}: a tax-withholding row in the activity report matches "
        f"no dividend, so ibkr-fa can't tell what it was withheld from{OS_BY_HAND}"
    ),
    _Cause.WITHHOLDING_SEVERAL_DIVIDENDS: (
        f"payment-pairing {_INDIVIDUAL}: a tax-withholding row in the activity report matches "
        f"more than one dividend, so ibkr-fa can't tell which it was withheld from{OS_BY_HAND}"
    ),
    _Cause.DIVIDEND_SEVERAL_WITHHOLDINGS: (
        f"payment-pairing {_INDIVIDUAL}: more than one tax-withholding row in the activity "
        f"report matches the same dividend, so ibkr-fa can't tell which applies{OS_BY_HAND}"
    ),
    _Cause.STATEMENT_SPANS_YEARS: (
        "statement-reconciliation (Individual and Managed Individual accounts, 2025): a "
        "statement's period crosses 31 December, so its Income summary can't be checked "
        f"against one calendar year{OS_BY_HAND}"
    ),
    _Cause.STATEMENT_DIVIDENDS: _STATEMENT_LINE.format(line="Dividends", income="dividends"),
    _Cause.STATEMENT_INTEREST: _STATEMENT_LINE.format(line="Interest Earned", income="interest"),
    _Cause.STATEMENT_STOCK_LENDING: _STATEMENT_LINE.format(
        line="Stock Lending", income="Stock Lending income"
    ),
    _Cause.SUBSTITUTE_PAYMENT: (
        f"stock-lending-classification {_INDIVIDUAL}: a Form 1042-S reports a substitute "
        "payment (income code 33, 34, 35, 53 or 54), so ibkr-fa can't tell substitute dividends "
        f"from loan fees in the Stock Lending income{OS_BY_HAND}"
    ),
    _Cause.UNSUPPORTED_FORM_CONTENT: (
        f"unsupported-form-semantics {_INDIVIDUAL}: a Form 1042-S has an income code, tax "
        f"rate, amount or form condition ibkr-fa doesn't support{_FTC}"
    ),
    _Cause.CAPITAL_GAINS_DISTRIBUTIONS: (
        f"unsupported-income {_INDIVIDUAL}: a statement's Income summary shows Capital Gains "
        f"Distributions, which ibkr-fa doesn't compute{OS_BY_HAND}"
    ),
}


def run_income_command(
    command: str,
    case: IncomeCase,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> tuple[int, str, str, dict[str, Any]]:
    """Run ``command`` with real income compute; return its exit code, its
    stdout, its stderr and the keywords it loaded the bundle with."""
    bundle = _stub_income_bundle(monkeypatch, tmp_path, *case)
    loaded: dict[str, Any] = {}

    def load(**kwargs: Any) -> SimpleNamespace:
        loaded.update(kwargs)
        return bundle

    monkeypatch.setattr(pipeline, "_load", load)
    code = _main(command, tmp_path)
    captured = capsys.readouterr()
    # Nothing is written, or every file written is removed, as before.
    out_dir = tmp_path / "out"
    assert not out_dir.exists() or not any(out_dir.iterdir())
    return code, captured.out, captured.err, loaded


# Followed remedies: the values that fill a remedy's placeholders, the
# loaded entry and each cause's remedy.
REMEDY_ACCOUNT = "999999999"
FILLED_PLACEHOLDERS = {
    "<account number>": REMEDY_ACCOUNT,
    "<FY start year, e.g. 2025 for 2025-26>": "2025",
    "<two-letter code of the fee's source country, not IN>": "US",
    "<what you reviewed>": "synthetic reviewed agreement",
    "<why that is the source country>": "the synthetic loan-fee payer is a US entity",
}


def loaded_remedy_entry(tmp_path: Path, table: str, entry: str) -> Any:
    """The one ``[[robinhood.<table>]]`` entry the real loader reads from
    ``entry``, beside a configured account ``REMEDY_ACCOUNT``."""
    return loaded_entry(tmp_path, REMEDY_ACCOUNT, table, entry)


def income_blocker_remedy(cause: RobinhoodIncomeBlockerCause) -> str:
    remedy = core._INCOME_BLOCKER_TEXT[cause][1]
    assert remedy is not None
    return remedy


def unconfigured_security_entry(tmp_path: Path, symbol: str, cusip: str) -> Any:
    """The entry the identity remedy names, with exactly its keys, read by
    the real config loader."""
    text, _keys = security_entry(
        income_blocker_remedy(_Cause.UNCONFIGURED_SECURITY), symbol, cusip, INCOME_ISIN
    )
    folder = tmp_path / "security-remedy"
    folder.mkdir()
    return loaded_remedy_entry(folder, "securities", text)
