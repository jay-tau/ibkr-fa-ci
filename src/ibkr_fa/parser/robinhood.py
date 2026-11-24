"""Parse Robinhood account-activity CSVs into lossless typed events.

This module intentionally stops at the source-event boundary from ADR 0006.
It preserves every accepted activity record for later reconciliation and
refuses any unrecognised code, description, layout, sign, or suffix.
"""

from __future__ import annotations

import contextlib
import csv
import io
import operator
import re
from dataclasses import dataclass
from datetime import date
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from enum import StrEnum
from pathlib import Path
from typing import Final, NoReturn

from ibkr_fa.exceptions import ParseError
from ibkr_fa.number_checks import NumberKind, number_limit_breach
from ibkr_fa.parser._exact import exactly
from ibkr_fa.parser._robinhood_money import ROBINHOOD_WHOLE_DOLLARS as _WHOLE_MONEY

_SECTION: Final[str] = "Robinhood account activity"
_HEADER: Final[tuple[str, ...]] = (
    "Activity Date",
    "Process Date",
    "Settle Date",
    "Instrument",
    "Description",
    "Trans Code",
    "Quantity",
    "Price",
    "Amount",
)
_SEPARATOR: Final[tuple[str, ...]] = ("",)
_FOOTER: Final[tuple[str, ...]] = (
    "",
    "",
    "",
    "",
    "",
    "",
    "",
    "",
    "",
    (
        "The data provided is for informational purposes only. Please consult a "
        "professional tax service or personal tax advisor if you need instructions "
        "on how to calculate cost basis or questions regarding your specific tax "
        "situation. Reminder: This data does not include Robinhood Crypto or "
        "Robinhood Spending activity."
    ),
)
_CENT: Final[Decimal] = Decimal("0.01")
_DESCRIPTION_PRECISION: Final[str] = "description arithmetic exceeds the supported precision"
_DESCRIPTION_COLUMN_INDEX: Final[int] = 4

_ACTIVITY_DATE_RE: Final = re.compile(
    r"(?P<month>[1-9]|1[0-2])/(?P<day>[1-9]|[12][0-9]|3[01])/(?P<year>[0-9]{4})"
)
_ISO_DATE_RE: Final = re.compile(
    r"(?P<year>[0-9]{4})-(?P<month>0[1-9]|1[0-2])-(?P<day>0[1-9]|[12][0-9]|3[01])"
)
_NUMBER_TEXT: Final[str] = r"(?:0|[1-9][0-9]*)(?:\.[0-9]+)?"
_POSITIVE_MONEY_RE: Final = re.compile(rf"\$(?P<whole>{_WHOLE_MONEY})\.(?P<cents>[0-9]{{2}})")
_NEGATIVE_MONEY_RE: Final = re.compile(rf"\(\$(?P<whole>{_WHOLE_MONEY})\.(?P<cents>[0-9]{{2}})\)")
_QUANTITY_RE: Final = re.compile(rf"(?P<number>{_NUMBER_TEXT})(?P<suffix>S)?")
_SECURITY_DESCRIPTION_RE: Final = re.compile(
    r"(?P<issuer>[^\r\n]+)\r?\nCUSIP: (?P<cusip>[A-Z0-9]{9})"
    r"(?:\r?\n(?P<annotation>Dividend Reinvestment|Managed Trade|Primary Issue))?"
)
_PER_SHARE_TERMS: Final[str] = (
    r"R/D (?P<record_date>[0-9]{4}-[0-9]{2}-[0-9]{2}) "
    r"P/D (?P<payment_date>[0-9]{4}-[0-9]{2}-[0-9]{2}) - "
    rf"(?P<shares>{_NUMBER_TEXT}) shares at (?P<rate>{_NUMBER_TEXT})"
)
_DISTRIBUTION_DESCRIPTION_RE: Final = re.compile(
    rf"Cash Div: {_PER_SHARE_TERMS}(?P<nra_suffix> new dividend IN nra tax withhold)?"
)
_ADR_FEE_DESCRIPTION_RE: Final = re.compile(rf"ADR Fee: {_PER_SHARE_TERMS}")
_DTAX_DESCRIPTION_RE: Final = re.compile(
    rf"Foreign Tax Witholding at (?P<amount>\${_WHOLE_MONEY}\.[0-9]{{2}})"
)
_DCF_DESCRIPTION_RE: Final = re.compile(
    r"External debit card transfer - account ending in (?P<reference>[0-9]+)"
)
_RTP_DESCRIPTION_RE: Final = re.compile(
    r"Instant bank transfer - account ending in (?P<reference>[0-9]+)"
)
_OPTION_DESCRIPTION_RE: Final = re.compile(
    rf"(?P<symbol>[A-Z][A-Z.-]*) "
    rf"(?P<expiration>[0-9]{{1,2}}/[0-9]{{1,2}}/[0-9]{{4}}) "
    rf"(?P<right>Call|Put) (?P<strike>\${_WHOLE_MONEY}\.[0-9]{{2}})"
)


class RobinhoodEventKind(StrEnum):
    """Every source event accepted by ADR 0006."""

    ACH_CANCELLATION = "ach-cancellation"
    ACH_DEPOSIT = "ach-deposit"
    ACH_WITHDRAWAL = "ach-withdrawal"
    ADR_FEE = "adr-fee"
    CASH_DIVIDEND = "cash-dividend"
    CASH_INTEREST = "cash-interest"
    DIVIDEND_REINVESTMENT_BUY = "dividend-reinvestment-buy"
    EQUITY_BUY = "equity-buy"
    EQUITY_SELL = "equity-sell"
    EXTERNAL_DEBIT_CARD_TRANSFER = "external-debit-card-transfer"
    FOREIGN_TAX_WITHHOLDING = "foreign-tax-withholding"
    IN_KIND_SECURITY_TRANSFER = "in-kind-security-transfer"
    INSTANT_BANK_TRANSFER = "instant-bank-transfer"
    INTER_ACCOUNT_CASH_IN = "inter-account-cash-in"
    INTER_ACCOUNT_CASH_OUT = "inter-account-cash-out"
    MANAGEMENT_FEE = "management-fee"
    MANAGED_EQUITY_BUY = "managed-equity-buy"
    MANAGED_EQUITY_SELL = "managed-equity-sell"
    NRA_WITHHOLDING = "nra-withholding"
    OPTION_BUY_TO_OPEN = "option-buy-to-open"
    OPTION_SELL_TO_CLOSE = "option-sell-to-close"
    PRIMARY_ISSUE_BUY = "primary-issue-buy"
    SECURITY_RECEIPT = "security-receipt"
    STOCK_LENDING_INCOME = "stock-lending-income"


ROBINHOOD_EQUITY_SALE_KINDS: Final = frozenset(
    {
        RobinhoodEventKind.EQUITY_SELL,
        RobinhoodEventKind.MANAGED_EQUITY_SELL,
    }
)
ROBINHOOD_OPTION_TRADE_KINDS: Final = frozenset(
    {
        RobinhoodEventKind.OPTION_BUY_TO_OPEN,
        RobinhoodEventKind.OPTION_SELL_TO_CLOSE,
    }
)
ROBINHOOD_INCOME_KINDS: Final = frozenset(
    {
        RobinhoodEventKind.CASH_DIVIDEND,
        RobinhoodEventKind.CASH_INTEREST,
        RobinhoodEventKind.NRA_WITHHOLDING,
        RobinhoodEventKind.FOREIGN_TAX_WITHHOLDING,
        RobinhoodEventKind.STOCK_LENDING_INCOME,
    }
)
ROBINHOOD_FEE_KINDS: Final = frozenset(
    {
        RobinhoodEventKind.ADR_FEE,
        RobinhoodEventKind.MANAGEMENT_FEE,
    }
)
ROBINHOOD_INCOME_AND_FEE_KINDS: Final = ROBINHOOD_INCOME_KINDS | ROBINHOOD_FEE_KINDS

_OPTIONAL_FIELDS: Final[tuple[str, ...]] = (
    "instrument",
    "quantity",
    "quantity_suffix",
    "price_usd",
    "amount_usd",
    "issuer_name",
    "cusip",
    "record_date",
    "payment_date",
    "description_shares",
    "description_rate_usd",
    "description_amount_usd",
    "account_reference",
    "option_symbol",
    "option_expiration",
    "option_right",
    "option_strike_usd",
)

_TRADE_FIELDS: Final = frozenset(
    {"instrument", "quantity", "price_usd", "amount_usd", "issuer_name", "cusip"}
)
_OPTION_FIELDS: Final = frozenset(
    {
        "instrument",
        "quantity",
        "price_usd",
        "amount_usd",
        "option_symbol",
        "option_expiration",
        "option_right",
        "option_strike_usd",
    }
)
_DISTRIBUTION_FIELDS: Final = frozenset(
    {
        "instrument",
        "amount_usd",
        "record_date",
        "payment_date",
        "description_shares",
        "description_rate_usd",
    }
)

_PRESENT_FIELDS: Final[dict[RobinhoodEventKind, frozenset[str]]] = {
    RobinhoodEventKind.ACH_CANCELLATION: frozenset({"amount_usd"}),
    RobinhoodEventKind.ACH_DEPOSIT: frozenset({"amount_usd"}),
    RobinhoodEventKind.ACH_WITHDRAWAL: frozenset({"amount_usd"}),
    RobinhoodEventKind.ADR_FEE: _DISTRIBUTION_FIELDS,
    RobinhoodEventKind.CASH_DIVIDEND: _DISTRIBUTION_FIELDS,
    RobinhoodEventKind.CASH_INTEREST: frozenset({"amount_usd"}),
    RobinhoodEventKind.DIVIDEND_REINVESTMENT_BUY: _TRADE_FIELDS,
    RobinhoodEventKind.EQUITY_BUY: _TRADE_FIELDS,
    RobinhoodEventKind.EQUITY_SELL: _TRADE_FIELDS,
    RobinhoodEventKind.EXTERNAL_DEBIT_CARD_TRANSFER: frozenset({"amount_usd", "account_reference"}),
    RobinhoodEventKind.FOREIGN_TAX_WITHHOLDING: frozenset(
        {"instrument", "amount_usd", "description_amount_usd"}
    ),
    RobinhoodEventKind.IN_KIND_SECURITY_TRANSFER: frozenset(
        {"instrument", "quantity", "quantity_suffix", "issuer_name", "cusip"}
    ),
    RobinhoodEventKind.INSTANT_BANK_TRANSFER: frozenset({"amount_usd", "account_reference"}),
    RobinhoodEventKind.INTER_ACCOUNT_CASH_IN: frozenset({"amount_usd"}),
    RobinhoodEventKind.INTER_ACCOUNT_CASH_OUT: frozenset({"amount_usd"}),
    RobinhoodEventKind.MANAGEMENT_FEE: frozenset({"amount_usd"}),
    RobinhoodEventKind.MANAGED_EQUITY_BUY: _TRADE_FIELDS,
    RobinhoodEventKind.MANAGED_EQUITY_SELL: _TRADE_FIELDS,
    RobinhoodEventKind.NRA_WITHHOLDING: _DISTRIBUTION_FIELDS,
    RobinhoodEventKind.OPTION_BUY_TO_OPEN: _OPTION_FIELDS,
    RobinhoodEventKind.OPTION_SELL_TO_CLOSE: _OPTION_FIELDS,
    RobinhoodEventKind.PRIMARY_ISSUE_BUY: _TRADE_FIELDS,
    RobinhoodEventKind.SECURITY_RECEIPT: frozenset(
        {"instrument", "quantity", "issuer_name", "cusip"}
    ),
    RobinhoodEventKind.STOCK_LENDING_INCOME: frozenset({"instrument", "amount_usd"}),
}

_TRANS_CODE: Final[dict[RobinhoodEventKind, str]] = {
    RobinhoodEventKind.ACH_CANCELLATION: "ACH",
    RobinhoodEventKind.ACH_DEPOSIT: "ACH",
    RobinhoodEventKind.ACH_WITHDRAWAL: "ACH",
    RobinhoodEventKind.ADR_FEE: "AFEE",
    RobinhoodEventKind.CASH_DIVIDEND: "CDIV",
    RobinhoodEventKind.CASH_INTEREST: "INT",
    RobinhoodEventKind.DIVIDEND_REINVESTMENT_BUY: "Buy",
    RobinhoodEventKind.EQUITY_BUY: "Buy",
    RobinhoodEventKind.EQUITY_SELL: "Sell",
    RobinhoodEventKind.EXTERNAL_DEBIT_CARD_TRANSFER: "DCF",
    RobinhoodEventKind.FOREIGN_TAX_WITHHOLDING: "DTAX",
    RobinhoodEventKind.IN_KIND_SECURITY_TRANSFER: "ITRF",
    RobinhoodEventKind.INSTANT_BANK_TRANSFER: "RTP",
    RobinhoodEventKind.INTER_ACCOUNT_CASH_IN: "ITRF",
    RobinhoodEventKind.INTER_ACCOUNT_CASH_OUT: "ITRF",
    RobinhoodEventKind.MANAGEMENT_FEE: "ADVFEE",
    RobinhoodEventKind.MANAGED_EQUITY_BUY: "Buy",
    RobinhoodEventKind.MANAGED_EQUITY_SELL: "Sell",
    RobinhoodEventKind.NRA_WITHHOLDING: "NRAT",
    RobinhoodEventKind.OPTION_BUY_TO_OPEN: "BTO",
    RobinhoodEventKind.OPTION_SELL_TO_CLOSE: "STC",
    RobinhoodEventKind.PRIMARY_ISSUE_BUY: "Buy",
    RobinhoodEventKind.SECURITY_RECEIPT: "REC",
    RobinhoodEventKind.STOCK_LENDING_INCOME: "SLIP",
}

_POSITIVE_AMOUNT_KINDS: Final = frozenset(
    {
        RobinhoodEventKind.ACH_DEPOSIT,
        RobinhoodEventKind.CASH_DIVIDEND,
        RobinhoodEventKind.CASH_INTEREST,
        RobinhoodEventKind.EQUITY_SELL,
        RobinhoodEventKind.EXTERNAL_DEBIT_CARD_TRANSFER,
        RobinhoodEventKind.INSTANT_BANK_TRANSFER,
        RobinhoodEventKind.INTER_ACCOUNT_CASH_IN,
        RobinhoodEventKind.MANAGED_EQUITY_SELL,
        RobinhoodEventKind.OPTION_SELL_TO_CLOSE,
        RobinhoodEventKind.STOCK_LENDING_INCOME,
    }
)
_NEGATIVE_AMOUNT_KINDS: Final = frozenset(
    {
        RobinhoodEventKind.ACH_CANCELLATION,
        RobinhoodEventKind.ACH_WITHDRAWAL,
        RobinhoodEventKind.ADR_FEE,
        RobinhoodEventKind.DIVIDEND_REINVESTMENT_BUY,
        RobinhoodEventKind.EQUITY_BUY,
        RobinhoodEventKind.FOREIGN_TAX_WITHHOLDING,
        RobinhoodEventKind.INTER_ACCOUNT_CASH_OUT,
        RobinhoodEventKind.MANAGEMENT_FEE,
        RobinhoodEventKind.MANAGED_EQUITY_BUY,
        RobinhoodEventKind.NRA_WITHHOLDING,
        RobinhoodEventKind.OPTION_BUY_TO_OPEN,
        RobinhoodEventKind.PRIMARY_ISSUE_BUY,
    }
)


@dataclass(frozen=True, slots=True, repr=False)
class RobinhoodEvent:
    """One lossless, typed account-activity record.

    ``repr`` is deliberately disabled because descriptions and account
    references may contain private data.
    """

    kind: RobinhoodEventKind
    source_record_index: int
    activity_date: date
    process_date: date
    settle_date: date
    instrument: str | None
    description: str
    trans_code: str
    quantity: Decimal | None
    quantity_suffix: str | None
    price_usd: Decimal | None
    amount_usd: Decimal | None
    issuer_name: str | None = None
    cusip: str | None = None
    record_date: date | None = None
    payment_date: date | None = None
    description_shares: Decimal | None = None
    description_rate_usd: Decimal | None = None
    description_amount_usd: Decimal | None = None
    account_reference: str | None = None
    option_symbol: str | None = None
    option_expiration: date | None = None
    option_right: str | None = None
    option_strike_usd: Decimal | None = None

    def __post_init__(self) -> None:
        present = frozenset(name for name in _OPTIONAL_FIELDS if getattr(self, name) is not None)
        expected = _PRESENT_FIELDS[self.kind]
        valid_fields = present == expected
        if (
            self.kind is RobinhoodEventKind.IN_KIND_SECURITY_TRANSFER
            and self.quantity_suffix is None
        ):
            valid_fields = present == expected - {"quantity_suffix"}
        if not valid_fields:
            raise ValueError("Robinhood event fields do not match the event kind")
        if self.source_record_index < 1:
            raise ValueError("Robinhood source record index must follow the header")
        if self.trans_code != _TRANS_CODE[self.kind]:
            raise ValueError("Robinhood transaction code does not match the event kind")
        if self.settle_date < self.activity_date:
            raise ValueError("Robinhood settlement date precedes the activity date")
        if self.quantity is not None and self.quantity <= 0:
            raise ValueError("Robinhood event quantity must be positive")
        if self.price_usd is not None and self.price_usd <= 0:
            raise ValueError("Robinhood event price must be positive")
        if self.kind in _POSITIVE_AMOUNT_KINDS and (
            self.amount_usd is None or self.amount_usd <= 0
        ):
            raise ValueError("Robinhood event amount must be positive")
        if self.kind in _NEGATIVE_AMOUNT_KINDS and (
            self.amount_usd is None or self.amount_usd >= 0
        ):
            raise ValueError("Robinhood event amount must be negative")
        if self.quantity_suffix is not None and self.quantity_suffix != "S":
            raise ValueError("Robinhood event quantity suffix is unsupported")
        if self.option_symbol is not None and self.option_symbol != self.instrument:
            raise ValueError("Robinhood option symbol does not match the instrument")


def _fail(*, row_index: int, column: str | None, message: str) -> NoReturn:
    raise ParseError(
        section=_SECTION,
        row_index=row_index,
        column=column,
        message=message,
        raw_value="",
    )


def _parse_date(raw: str, *, row_index: int, column: str) -> date:
    match = _ACTIVITY_DATE_RE.fullmatch(raw)
    if match is None:
        _fail(row_index=row_index, column=column, message="date must use unpadded M/D/YYYY")
    with contextlib.suppress(ValueError):
        return date(
            int(match.group("year")),
            int(match.group("month")),
            int(match.group("day")),
        )
    # Refused outside the handler, so no chained error names the date's parts.
    _fail(row_index=row_index, column=column, message="date is not a valid calendar date")


def _parse_description_date(raw: str, *, row_index: int) -> date:
    match = _ISO_DATE_RE.fullmatch(raw)
    if match is None:
        _fail(
            row_index=row_index,
            column="Description",
            message="description date must use YYYY-MM-DD",
        )
    with contextlib.suppress(ValueError):
        return date(
            int(match.group("year")),
            int(match.group("month")),
            int(match.group("day")),
        )
    # Refused outside the handler, so no chained error names the date's parts.
    _fail(
        row_index=row_index,
        column="Description",
        message="description contains an invalid calendar date",
    )


def _within_limits(value: Decimal, kind: NumberKind, *, row_index: int, column: str) -> Decimal:
    breach = number_limit_breach(value, kind)
    if breach is not None:
        _fail(row_index=row_index, column=column, message=breach)
    return value


def _money(
    raw: str, *, row_index: int, column: str, kind: NumberKind = NumberKind.MONEY
) -> Decimal | None:
    if raw == "":
        return None
    positive = _POSITIVE_MONEY_RE.fullmatch(raw)
    negative = _NEGATIVE_MONEY_RE.fullmatch(raw)
    match = positive or negative
    if match is None:
        _fail(
            row_index=row_index,
            column=column,
            message="money must use canonical USD syntax",
        )
    value = Decimal(f"{match.group('whole').replace(',', '')}.{match.group('cents')}")
    signed = value.copy_negate() if negative is not None else value
    return _within_limits(signed, kind, row_index=row_index, column=column)


def _unsigned_description_money(raw: str, *, row_index: int, kind: NumberKind) -> Decimal:
    value = _money(raw, row_index=row_index, column="Description", kind=kind)
    if value is None or value <= 0:
        _fail(
            row_index=row_index,
            column="Description",
            message="description amount must be positive USD",
        )
    return value


def _quantity(raw: str, *, row_index: int) -> tuple[Decimal | None, str | None]:
    if raw == "":
        return None, None
    match = _QUANTITY_RE.fullmatch(raw)
    if match is None:
        _fail(
            row_index=row_index,
            column="Quantity",
            message="quantity has an unsupported format or suffix",
        )
    quantity = Decimal(match.group("number"))
    return (
        _within_limits(quantity, NumberKind.QUANTITY, row_index=row_index, column="Quantity"),
        match.group("suffix"),
    )


def _description_number(raw: str, *, row_index: int, kind: NumberKind) -> Decimal:
    if re.fullmatch(_NUMBER_TEXT, raw) is None:
        _fail(
            row_index=row_index,
            column="Description",
            message="description contains a malformed number",
        )
    return _within_limits(Decimal(raw), kind, row_index=row_index, column="Description")


def _security_description(raw: str, *, row_index: int) -> tuple[str, str, str | None]:
    match = _SECURITY_DESCRIPTION_RE.fullmatch(raw)
    if match is None:
        _fail(
            row_index=row_index,
            column="Description",
            message="security description has an unsupported grammar",
        )
    return match.group("issuer"), match.group("cusip"), match.group("annotation")


def _per_share_terms(
    match: re.Match[str], *, row_index: int
) -> tuple[date, date, Decimal, Decimal]:
    """Convert the dividend and ADR-fee readers' shared ``R/D … shares at …`` fields."""
    return (
        _parse_description_date(match.group("record_date"), row_index=row_index),
        _parse_description_date(match.group("payment_date"), row_index=row_index),
        _description_number(match.group("shares"), row_index=row_index, kind=NumberKind.QUANTITY),
        _description_number(match.group("rate"), row_index=row_index, kind=NumberKind.PRICE),
    )


def _distribution_description(
    raw: str, *, row_index: int
) -> tuple[date, date, Decimal, Decimal, bool]:
    match = _DISTRIBUTION_DESCRIPTION_RE.fullmatch(raw)
    if match is None:
        _fail(
            row_index=row_index,
            column="Description",
            message="distribution description has an unsupported grammar",
        )
    return (*_per_share_terms(match, row_index=row_index), match.group("nra_suffix") is not None)


def _adr_fee_description(raw: str, *, row_index: int) -> tuple[date, date, Decimal, Decimal]:
    match = _ADR_FEE_DESCRIPTION_RE.fullmatch(raw)
    if match is None:
        _fail(
            row_index=row_index,
            column="Description",
            message="ADR fee description has an unsupported grammar",
        )
    return _per_share_terms(match, row_index=row_index)


def _parse_event(  # noqa: PLR0912, PLR0915
    row: list[str],
    *,
    row_index: int,
) -> RobinhoodEvent:
    # ponytail: one exhaustive dispatch keeps the fail-closed grammar visible.
    for column_index, cell in enumerate(row):
        if column_index != _DESCRIPTION_COLUMN_INDEX and ("\n" in cell or "\r" in cell):
            _fail(
                row_index=row_index,
                column=_HEADER[column_index],
                message="only Description may contain embedded newlines",
            )

    activity_date = _parse_date(row[0], row_index=row_index, column="Activity Date")
    process_date = _parse_date(row[1], row_index=row_index, column="Process Date")
    settle_date = _parse_date(row[2], row_index=row_index, column="Settle Date")
    instrument = row[3] or None
    description = row[4]
    trans_code = row[5]
    quantity, quantity_suffix = _quantity(row[6], row_index=row_index)
    price_usd = _money(row[7], row_index=row_index, column="Price", kind=NumberKind.PRICE)
    amount_usd = _money(row[8], row_index=row_index, column="Amount")

    issuer_name: str | None = None
    cusip: str | None = None
    record_date: date | None = None
    payment_date: date | None = None
    description_shares: Decimal | None = None
    description_rate_usd: Decimal | None = None
    description_amount_usd: Decimal | None = None
    account_reference: str | None = None
    option_symbol: str | None = None
    option_expiration: date | None = None
    option_right: str | None = None
    option_strike_usd: Decimal | None = None

    match trans_code:
        case "ACH":
            kinds = {
                "ACH CANCEL": RobinhoodEventKind.ACH_CANCELLATION,
                "ACH Deposit": RobinhoodEventKind.ACH_DEPOSIT,
                "ACH Withdrawal": RobinhoodEventKind.ACH_WITHDRAWAL,
            }
            kind = kinds.get(description)
            if kind is None:
                _fail(
                    row_index=row_index,
                    column="Description",
                    message="ACH description has an unsupported grammar",
                )
        case "AFEE":
            kind = RobinhoodEventKind.ADR_FEE
            record_date, payment_date, description_shares, description_rate_usd = (
                _adr_fee_description(description, row_index=row_index)
            )
        case "ADVFEE":
            if description != "Management Fee":
                _fail(
                    row_index=row_index,
                    column="Description",
                    message="management-fee description has an unsupported grammar",
                )
            kind = RobinhoodEventKind.MANAGEMENT_FEE
        case "BTO" | "STC":
            option = _OPTION_DESCRIPTION_RE.fullmatch(description)
            if option is None:
                _fail(
                    row_index=row_index,
                    column="Description",
                    message="option description has an unsupported grammar",
                )
            kind = (
                RobinhoodEventKind.OPTION_BUY_TO_OPEN
                if trans_code == "BTO"
                else RobinhoodEventKind.OPTION_SELL_TO_CLOSE
            )
            option_symbol = option.group("symbol")
            option_expiration = _parse_date(
                option.group("expiration"),
                row_index=row_index,
                column="Description",
            )
            option_right = option.group("right")
            option_strike_usd = _unsigned_description_money(
                option.group("strike"),
                row_index=row_index,
                kind=NumberKind.PRICE,
            )
        case "Buy":
            issuer_name, cusip, annotation = _security_description(
                description,
                row_index=row_index,
            )
            kinds = {
                None: RobinhoodEventKind.EQUITY_BUY,
                "Dividend Reinvestment": RobinhoodEventKind.DIVIDEND_REINVESTMENT_BUY,
                "Managed Trade": RobinhoodEventKind.MANAGED_EQUITY_BUY,
                "Primary Issue": RobinhoodEventKind.PRIMARY_ISSUE_BUY,
            }
            kind = kinds.get(annotation)
            if kind is None:
                _fail(
                    row_index=row_index,
                    column="Description",
                    message="purchase description has an unsupported annotation",
                )
        case "CDIV" | "NRAT":
            (
                record_date,
                payment_date,
                description_shares,
                description_rate_usd,
                is_nra,
            ) = _distribution_description(description, row_index=row_index)
            if (trans_code == "NRAT") != is_nra:
                _fail(
                    row_index=row_index,
                    column="Description",
                    message="distribution description does not match its transaction code",
                )
            kind = (
                RobinhoodEventKind.NRA_WITHHOLDING if is_nra else RobinhoodEventKind.CASH_DIVIDEND
            )
        case "DCF":
            match = _DCF_DESCRIPTION_RE.fullmatch(description)
            if match is None:
                _fail(
                    row_index=row_index,
                    column="Description",
                    message="debit-card transfer description has an unsupported grammar",
                )
            kind = RobinhoodEventKind.EXTERNAL_DEBIT_CARD_TRANSFER
            account_reference = match.group("reference")
        case "DTAX":
            match = _DTAX_DESCRIPTION_RE.fullmatch(description)
            if match is None:
                _fail(
                    row_index=row_index,
                    column="Description",
                    message="foreign-tax description has an unsupported grammar",
                )
            kind = RobinhoodEventKind.FOREIGN_TAX_WITHHOLDING
            description_amount_usd = _unsigned_description_money(
                match.group("amount"),
                row_index=row_index,
                kind=NumberKind.MONEY,
            )
        case "INT":
            if description != "Interest Payment":
                _fail(
                    row_index=row_index,
                    column="Description",
                    message="interest description has an unsupported grammar",
                )
            kind = RobinhoodEventKind.CASH_INTEREST
        case "ITRF":
            if description == "Transfer from Brokerage to Brokerage":
                if amount_usd is None or amount_usd == 0:
                    _fail(
                        row_index=row_index,
                        column="Amount",
                        message="cash transfer must carry a signed amount",
                    )
                kind = (
                    RobinhoodEventKind.INTER_ACCOUNT_CASH_IN
                    if amount_usd > 0
                    else RobinhoodEventKind.INTER_ACCOUNT_CASH_OUT
                )
            else:
                issuer_name, cusip, annotation = _security_description(
                    description,
                    row_index=row_index,
                )
                if annotation is not None:
                    _fail(
                        row_index=row_index,
                        column="Description",
                        message="security transfer description has an unsupported annotation",
                    )
                kind = RobinhoodEventKind.IN_KIND_SECURITY_TRANSFER
        case "REC":
            issuer_name, cusip, annotation = _security_description(
                description,
                row_index=row_index,
            )
            if annotation is not None:
                _fail(
                    row_index=row_index,
                    column="Description",
                    message="security receipt description has an unsupported annotation",
                )
            kind = RobinhoodEventKind.SECURITY_RECEIPT
        case "RTP":
            match = _RTP_DESCRIPTION_RE.fullmatch(description)
            if match is None:
                _fail(
                    row_index=row_index,
                    column="Description",
                    message="instant-transfer description has an unsupported grammar",
                )
            kind = RobinhoodEventKind.INSTANT_BANK_TRANSFER
            account_reference = match.group("reference")
        case "Sell":
            issuer_name, cusip, annotation = _security_description(
                description,
                row_index=row_index,
            )
            if annotation is None:
                kind = RobinhoodEventKind.EQUITY_SELL
            elif annotation == "Managed Trade":
                kind = RobinhoodEventKind.MANAGED_EQUITY_SELL
            else:
                _fail(
                    row_index=row_index,
                    column="Description",
                    message="sale description has an unsupported annotation",
                )
        case "SLIP":
            if description != "Stock Lending":
                _fail(
                    row_index=row_index,
                    column="Description",
                    message="Stock Lending description has an unsupported grammar",
                )
            kind = RobinhoodEventKind.STOCK_LENDING_INCOME
        case _:
            _fail(
                row_index=row_index,
                column="Trans Code",
                message="transaction code is not supported",
            )

    if (
        kind in {RobinhoodEventKind.CASH_DIVIDEND, RobinhoodEventKind.ADR_FEE}
        and amount_usd is not None
        and description_shares is not None
        and description_rate_usd is not None
    ):
        # The exact product: rounded to the context first, it could round
        # to cents differently (#1295).
        product = exactly(
            operator.mul,
            description_shares,
            description_rate_usd,
            refusal=lambda _: ParseError(
                section=_SECTION,
                row_index=row_index,
                column="Description",
                message=_DESCRIPTION_PRECISION,
                raw_value="",
            ),
        )
        try:
            expected = product.quantize(_CENT, rounding=ROUND_HALF_UP)
        except InvalidOperation:
            expected = None
        if expected is None:
            _fail(row_index=row_index, column="Description", message=_DESCRIPTION_PRECISION)
        if expected != abs(amount_usd):
            _fail(
                row_index=row_index,
                column="Amount",
                message="description arithmetic does not match the amount",
            )
    if (
        kind is RobinhoodEventKind.FOREIGN_TAX_WITHHOLDING
        and amount_usd is not None
        and description_amount_usd != abs(amount_usd)
    ):
        _fail(
            row_index=row_index,
            column="Amount",
            message="foreign-tax description amount does not match the amount",
        )

    try:
        return RobinhoodEvent(
            kind=kind,
            source_record_index=row_index,
            activity_date=activity_date,
            process_date=process_date,
            settle_date=settle_date,
            instrument=instrument,
            description=description,
            trans_code=trans_code,
            quantity=quantity,
            quantity_suffix=quantity_suffix,
            price_usd=price_usd,
            amount_usd=amount_usd,
            issuer_name=issuer_name,
            cusip=cusip,
            record_date=record_date,
            payment_date=payment_date,
            description_shares=description_shares,
            description_rate_usd=description_rate_usd,
            description_amount_usd=description_amount_usd,
            account_reference=account_reference,
            option_symbol=option_symbol,
            option_expiration=option_expiration,
            option_right=option_right,
            option_strike_usd=option_strike_usd,
        )
    except ValueError:
        _fail(
            row_index=row_index,
            column=None,
            message="row fields do not match the accepted event variant",
        )


def parse_robinhood_activity(text: str) -> tuple[RobinhoodEvent, ...]:
    """Parse one complete Robinhood account-activity CSV."""

    reader = csv.reader(
        io.StringIO(text.removeprefix("\ufeff"), newline=""),
        strict=True,
    )
    try:
        header = next(reader)
    except StopIteration:
        _fail(row_index=0, column=None, message="file is empty")
    except csv.Error:
        _fail(row_index=0, column=None, message="CSV header is malformed")
    if tuple(header) != _HEADER:
        _fail(row_index=0, column=None, message="CSV header is not supported")

    events: list[RobinhoodEvent] = []
    separator_seen = False
    footer_seen = False
    record_index = 0
    try:
        for record_index, row in enumerate(reader, start=1):
            cells = tuple(row)
            if footer_seen:
                _fail(
                    row_index=record_index,
                    column=None,
                    message="unexpected record after the footer",
                )
            if not separator_seen:
                if cells == _SEPARATOR:
                    separator_seen = True
                    continue
                if len(row) != len(_HEADER):
                    _fail(
                        row_index=record_index,
                        column=None,
                        message="activity record has an unsupported column count",
                    )
                events.append(_parse_event(row, row_index=record_index))
                continue
            if cells != _FOOTER:
                _fail(
                    row_index=record_index,
                    column=None,
                    message="footer record is missing or malformed",
                )
            footer_seen = True
    except csv.Error:
        _fail(
            row_index=record_index + 1,
            column=None,
            message="CSV record is malformed",
        )

    if not separator_seen:
        _fail(row_index=record_index + 1, column=None, message="separator record is missing")
    if not footer_seen:
        _fail(row_index=record_index + 1, column=None, message="footer record is missing")
    return tuple(events)


def read_robinhood_activity(path: Path) -> tuple[RobinhoodEvent, ...]:
    """Read and parse a Robinhood activity CSV without exposing its path."""

    try:
        with path.open(encoding="utf-8-sig", newline="") as source:
            return parse_robinhood_activity(source.read())
    except ParseError:
        raise
    except (OSError, UnicodeError):
        pass  # refused below, outside the handler: a decode error holds the file's bytes
    raise ParseError(
        section=_SECTION,
        row_index=0,
        column=None,
        message="could not read Robinhood activity CSV as UTF-8",
        raw_value="",
    )
