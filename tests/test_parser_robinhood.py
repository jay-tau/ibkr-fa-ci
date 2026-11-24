"""Synthetic contract checks for the Robinhood activity parser."""

from __future__ import annotations

import csv
from datetime import date
from decimal import Context, Decimal, localcontext
from pathlib import Path

import pytest

from ibkr_fa.compute.robinhood_options import _contract_from_event
from ibkr_fa.exceptions import ParseError
from ibkr_fa.parser.robinhood import (
    RobinhoodEventKind,
    parse_robinhood_activity,
    read_robinhood_activity,
)
from tests._private_checks import private_test
from tests._private_data import private_dataset
from tests._repr_text import repr_without_addresses
from tests._robinhood_statements import ACTIVITY_FOOTER as _FOOTER
from tests._robinhood_statements import ACTIVITY_HEADER as _HEADER
from tests._robinhood_statements import ACTIVITY_ROWS as _ROWS
from tests._robinhood_statements import activity_rows_csv as _csv

_EXPECTED_KINDS = (
    RobinhoodEventKind.ACH_CANCELLATION,
    RobinhoodEventKind.ACH_DEPOSIT,
    RobinhoodEventKind.ACH_WITHDRAWAL,
    RobinhoodEventKind.ADR_FEE,
    RobinhoodEventKind.CASH_DIVIDEND,
    RobinhoodEventKind.CASH_INTEREST,
    RobinhoodEventKind.DIVIDEND_REINVESTMENT_BUY,
    RobinhoodEventKind.EQUITY_BUY,
    RobinhoodEventKind.EQUITY_SELL,
    RobinhoodEventKind.EXTERNAL_DEBIT_CARD_TRANSFER,
    RobinhoodEventKind.FOREIGN_TAX_WITHHOLDING,
    RobinhoodEventKind.IN_KIND_SECURITY_TRANSFER,
    RobinhoodEventKind.INSTANT_BANK_TRANSFER,
    RobinhoodEventKind.INTER_ACCOUNT_CASH_IN,
    RobinhoodEventKind.INTER_ACCOUNT_CASH_OUT,
    RobinhoodEventKind.NRA_WITHHOLDING,
    RobinhoodEventKind.OPTION_BUY_TO_OPEN,
    RobinhoodEventKind.OPTION_SELL_TO_CLOSE,
    RobinhoodEventKind.PRIMARY_ISSUE_BUY,
    RobinhoodEventKind.SECURITY_RECEIPT,
    RobinhoodEventKind.STOCK_LENDING_INCOME,
    RobinhoodEventKind.MANAGED_EQUITY_BUY,
    RobinhoodEventKind.MANAGED_EQUITY_SELL,
    RobinhoodEventKind.MANAGEMENT_FEE,
    RobinhoodEventKind.IN_KIND_SECURITY_TRANSFER,
)


def _replace(row_index: int, column_index: int, value: str) -> str:
    rows = [list(row) for row in _ROWS]
    rows[row_index][column_index] = value
    return _csv(tuple(tuple(row) for row in rows))


def _replace_many(row_index: int, replacements: dict[int, str]) -> str:
    rows = [list(row) for row in _ROWS]
    for column_index, value in replacements.items():
        rows[row_index][column_index] = value
    return _csv(tuple(tuple(row) for row in rows))


def test_parse_every_event_kind_and_preserve_source_facts() -> None:
    events = parse_robinhood_activity(_csv())

    assert tuple(event.kind for event in events) == _EXPECTED_KINDS
    assert {event.kind for event in events} == set(RobinhoodEventKind)
    assert events[6].description == _ROWS[6][4]
    assert events[6].cusip == "99971T103"
    assert events[6].issuer_name == "Dividend Reinvestment Example"
    assert events[11].quantity == Decimal("2")
    assert events[11].quantity_suffix == "S"
    assert events[15].description_shares == Decimal("2")
    assert events[15].description_rate_usd == Decimal("1.25")
    assert events[16].option_symbol == "OPT"
    assert events[16].option_expiration == date(2025, 1, 17)
    assert events[16].option_right == "Call"
    assert events[16].option_strike_usd == Decimal("10.00")
    assert events[9].account_reference == "0001"
    assert events[21].kind is RobinhoodEventKind.MANAGED_EQUITY_BUY
    assert events[22].kind is RobinhoodEventKind.MANAGED_EQUITY_SELL
    assert events[23].kind is RobinhoodEventKind.MANAGEMENT_FEE
    assert events[24].kind is RobinhoodEventKind.IN_KIND_SECURITY_TRANSFER
    assert events[24].quantity_suffix is None
    assert events[0].source_record_index == 1
    assert events[-1].source_record_index == len(_ROWS)


def test_put_identity_is_preserved_for_typed_option_refusal() -> None:
    events = parse_robinhood_activity(_replace(16, 4, "OPT 1/17/2025 Put $10.00"))
    put = events[16]

    assert put.kind is RobinhoodEventKind.OPTION_BUY_TO_OPEN
    assert put.option_symbol == "OPT"
    assert put.option_expiration == date(2025, 1, 17)
    assert put.option_right == "Put"
    assert put.option_strike_usd == Decimal("10.00")
    assert put.quantity == Decimal("1")
    assert put.price_usd == Decimal("2.00")
    assert put.amount_usd == Decimal("-200.25")
    assert _contract_from_event(0, put).right == "Put"


@pytest.mark.parametrize(
    ("printed", "precision"),
    [
        ("999,999,999,999,999.99", 16),
        ("1,234,567.89", 6),
        ("3.00", 28),
        ("3.00", 6),
    ],
    ids=["widest-prec16", "cents-prec6", "control-prec28", "control-prec6"],
)
def test_parenthesised_amount_keeps_every_digit(printed: str, precision: int) -> None:
    """#1185: a parenthesised Amount is negated without rounding at any
    context precision, so it keeps the digits a statement debit prints.
    The widest Amount the reader takes (#1275: 15 whole digits and cents)
    has 17 digits, so a 16-digit context shows it at that width."""
    with localcontext(Context(prec=precision)):
        events = parse_robinhood_activity(_replace(2, 8, f"(${printed})"))

    assert events[2].kind is RobinhoodEventKind.ACH_WITHDRAWAL
    assert events[2].amount_usd is not None
    assert events[2].amount_usd.as_tuple() == Decimal("-" + printed.replace(",", "")).as_tuple()


def test_preserves_exact_duplicate_occurrences_and_input_order() -> None:
    duplicate = _ROWS[7]
    events = parse_robinhood_activity(_csv((duplicate, duplicate)))

    assert len(events) == 2
    assert events[0].kind is RobinhoodEventKind.EQUITY_BUY
    assert events[1].kind is RobinhoodEventKind.EQUITY_BUY
    assert events[0].source_record_index == 1
    assert events[1].source_record_index == 2
    assert events[0] == events[0]
    assert events[0] != events[1]


@pytest.mark.parametrize(
    "bad",
    [
        "",
        '"unterminated',
        _csv(header=(*_HEADER[:-1], "Cash")),
        _csv(separator=None),
        _csv(rows=(_ROWS[0],), separator=None, footer=None),
        _csv(footer=None),
        _csv(footer=_FOOTER[:-1]),
        _csv(footer=(*_FOOTER[:-1], "Changed disclaimer")),
        _csv(trailing=(("",),)),
        _csv(rows=(_ROWS[0][:-1],)),
        _csv(rows=(_ROWS[0], ("",), _ROWS[1])),
        '"Activity Date","Process Date","Settle Date","Instrument","Description",'
        '"Trans Code","Quantity","Price","Amount"\n"unterminated',
    ],
    ids=[
        "empty-file",
        "malformed-header",
        "wrong-header",
        "missing-separator",
        "separator-missing-at-eof",
        "missing-footer",
        "nine-cell-footer",
        "changed-footer",
        "record-after-footer",
        "column-shift",
        "extra-separator",
        "malformed-csv",
    ],
)
def test_rejects_structural_drift(bad: str) -> None:
    with pytest.raises(ParseError):
        parse_robinhood_activity(bad)


@pytest.mark.parametrize(
    "bad",
    [
        _replace(1, 0, "01/2/2025"),
        _replace(1, 0, "2/30/2025"),
        _replace(1, 2, "1/1/2025"),
        _replace(1, 8, "$10"),
        _replace(1, 8, "$1000.00"),
        _replace(7, 6, "1X"),
        _replace(7, 6, "1S"),
        _replace(1, 8, "($10.00)"),
        _replace(5, 3, "UNEXPECTED"),
        _replace(5, 4, "Unknown interest"),
        _replace(5, 5, "NEW"),
        _replace(7, 3, "BUY\nSHIFT"),
        _replace(4, 8, "$2.49"),
        _replace(3, 8, "($0.03)"),
        _replace(10, 8, "($0.49)"),
        _replace(15, 8, "$0.63"),
        _replace(4, 8, "($2.50)"),
        _replace(16, 4, "OPT 1/17/2025 Call $0.00"),
        _replace(16, 3, "OTHER"),
        _replace(19, 6, "3S"),
        _replace(7, 7, "$0.00"),
        _replace(7, 6, "0"),
        _replace(7, 4, "Issuer\nCUSIP: INVALID"),
        _replace(7, 4, "Issuer\nCUSIP: 99961C102\nUnexpected"),
        _replace(11, 4, f"{_ROWS[11][4]}\nManaged Trade"),
        _replace(19, 4, f"{_ROWS[19][4]}\nManaged Trade"),
        _replace(8, 4, f"{_ROWS[8][4]}\nDividend Reinvestment"),
        _replace(8, 4, f"{_ROWS[8][4]}\nPrimary Issue"),
        _replace(21, 4, f"{_ROWS[21][4]}\nDividend Reinvestment"),
        _replace(21, 8, "$18.35"),
        _replace(22, 8, "($22.02)"),
        _replace_many(21, {3: "", 6: "", 7: ""}),
        _replace(23, 8, "$0.25"),
        _replace(23, 3, "FEE"),
        _replace(23, 6, "1"),
        _replace(23, 7, "$0.25"),
        _replace(23, 4, "Advisory Fee"),
        _replace(23, 4, "Management fee"),
        _replace(24, 6, "2X"),
    ],
    ids=[
        "padded-date",
        "invalid-date",
        "settle-before-activity",
        "bad-money",
        "ungrouped-money",
        "bad-quantity",
        "misplaced-suffix",
        "wrong-sign",
        "wrong-layout",
        "unknown-description",
        "unknown-code",
        "newline-outside-description",
        "dividend-arithmetic",
        "fee-arithmetic",
        "dtax-arithmetic",
        "positive-nrat",
        "negative-cdiv",
        "zero-option-strike",
        "option-symbol-mismatch",
        "receipt-suffix",
        "zero-price",
        "zero-quantity",
        "bad-cusip",
        "unknown-annotation",
        "managed-annotation-on-transfer",
        "managed-annotation-on-receipt",
        "dividend-reinvestment-sale",
        "primary-issue-sale",
        "multiple-buy-annotations",
        "positive-managed-buy",
        "negative-managed-sell",
        "managed-buy-amount-only-layout",
        "positive-management-fee",
        "management-fee-with-instrument",
        "management-fee-with-quantity",
        "management-fee-with-price",
        "wrong-management-fee-description",
        "wrong-management-fee-case",
        "unsupported-transfer-suffix",
    ],
)
def test_rejects_unknown_or_malformed_event_variants(bad: str) -> None:
    with pytest.raises(ParseError):
        parse_robinhood_activity(bad)


def test_bom_is_optional_and_file_reader_uses_utf8_sig(tmp_path: Path) -> None:
    text = _csv()
    assert parse_robinhood_activity(text) == parse_robinhood_activity("\ufeff" + text)

    sample = tmp_path / "activity.csv"
    sample.write_text("\ufeff" + text, encoding="utf-8")
    assert read_robinhood_activity(sample) == parse_robinhood_activity(text)


def test_crlf_records_and_multiline_descriptions_are_accepted() -> None:
    events = parse_robinhood_activity(_csv().replace("\n", "\r\n"))

    assert tuple(event.kind for event in events) == _EXPECTED_KINDS
    assert "\r\n" in events[6].description


def test_errors_and_event_repr_do_not_expose_source_content(tmp_path: Path) -> None:
    sentinels = (
        "SECRET-DATE",
        "SECRET-INSTRUMENT",
        "SECRET-DESCRIPTION",
        "SECRET-VALUE",
        "SECRET-PATH",
    )
    rows = [list(row) for row in _ROWS]
    rows[0][0] = sentinels[0]
    rows[0][3] = sentinels[1]
    rows[0][4] = sentinels[2]
    rows[0][8] = sentinels[3]

    with pytest.raises(ParseError) as caught:
        parse_robinhood_activity(_csv(tuple(tuple(row) for row in rows)))
    rendered = str(caught.value)
    assert caught.value.raw_value == ""
    assert all(secret not in rendered for secret in sentinels)

    missing = tmp_path / sentinels[4]
    with pytest.raises(ParseError) as read_error:
        read_robinhood_activity(missing)
    assert read_error.value.__cause__ is None
    assert all(secret not in str(read_error.value) for secret in sentinels)

    event = parse_robinhood_activity(_csv((_ROWS[9],)))[0]
    rendered_event = repr_without_addresses(event)
    assert event.account_reference is not None
    assert event.description not in rendered_event
    assert event.account_reference not in rendered_event


def _private_money(raw: str) -> Decimal | None:
    if raw == "":
        return None
    negative = raw.startswith("(")
    digits = raw.removeprefix("(").removesuffix(")").removeprefix("$").replace(",", "")
    value = Decimal(digits)
    return -value if negative else value


def _private_quantity(raw: str) -> tuple[Decimal | None, str | None]:
    if raw == "":
        return None, None
    suffix = "S" if raw.endswith("S") else None
    return Decimal(raw.removesuffix("S")), suffix


def _private_date(raw: str) -> date:
    month, day, year = (int(part) for part in raw.split("/"))
    return date(year, month, day)


def _private_round_trip(path: Path) -> None:
    events = read_robinhood_activity(path)
    with path.open(newline="", encoding="utf-8-sig") as source:
        records = list(csv.reader(source, strict=True))
    separator_index = next(i for i, row in enumerate(records) if tuple(row) == ("",))
    activity_records = records[1:separator_index]

    if len(events) != len(activity_records):
        pytest.fail("private report record coverage mismatch", pytrace=False)

    for record_index, (event, row) in enumerate(
        zip(events, activity_records, strict=True),
        start=1,
    ):
        quantity, suffix = _private_quantity(row[6])
        equivalent = (
            event.source_record_index == record_index
            and event.activity_date == _private_date(row[0])
            and event.process_date == _private_date(row[1])
            and event.settle_date == _private_date(row[2])
            and event.instrument == (row[3] or None)
            and event.description == row[4]
            and event.trans_code == row[5]
            and event.quantity == quantity
            and event.quantity_suffix == suffix
            and event.price_usd == _private_money(row[7])
            and event.amount_usd == _private_money(row[8])
        )
        if not equivalent:
            pytest.fail("private report semantic audit failed", pytrace=False)


@private_test
def test_private_reports_are_consumed_once_and_round_trip_semantically() -> None:
    _report, individual_report, managed_report = private_dataset(
        "rh/report", "rh/report/individual.csv", "rh/report/managed-individual.csv"
    )
    _private_round_trip(individual_report)
    _private_round_trip(managed_report)
