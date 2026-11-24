"""#1209: Robinhood refusals chain no exception they caught.

Each refusal is raised after its handler has ended, so the caught decode,
TOML or calendar error is neither its cause nor its context: not the file's
bytes, not a duplicated TOML key or (Python 3.14) the whole document, and not
(3.14) the year of an invalid date. Each refusal test checks, in order: the
unchanged refusal, no input marker anywhere in the exception graph, and no
cause or context at all, which is what catches a chained calendar error on
3.12 and 3.13, whose message names no year there.

Markers stay in module constants and builders, off the line that calls the
parser, since a rendered traceback prints that line. A calendar marker is a
year from 9000 up, which no traceback line number can be.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from ibkr_fa.exceptions import ConfigError, ParseError
from ibkr_fa.normalize.robinhood.identity_exceptions import load_cusip_transitions
from ibkr_fa.parser.robinhood import parse_robinhood_activity, read_robinhood_activity
from ibkr_fa.parser.robinhood_1042s import parse_robinhood_1042s_packet
from ibkr_fa.parser.robinhood_statement import RobinhoodStatementSection, parse_robinhood_statement
from tests._crash_reports import assert_unchained_refusal
from tests._robinhood_1042s import SPACED_DOB as _SPACED_DOB
from tests._robinhood_1042s import replace_on_both_copies as _both_copies
from tests._robinhood_cusip_transitions import CUSIP_TRANSITION_ACCOUNTS as _ACCOUNTS
from tests._robinhood_cusip_transitions import cusip_transition as _transition
from tests._robinhood_refusals import MALFORMED_ACTIVITY_CONTRACT_REFUSAL as _ACTIVITY_MALFORMED
from tests._robinhood_statement_pages import ACTIVITY_OPTION_CONTRACT as _ACTIVITY_CONTRACT
from tests._robinhood_statement_pages import brokerage_statement as _statement
from tests._robinhood_statement_pages import option_activity_statement as _option_activity
from tests._robinhood_statement_pages import statement_activity_page as _activity_page
from tests._robinhood_statements import ACTIVITY_ROWS as _ROWS
from tests._robinhood_statements import activity_rows_csv as _csv

_ACTIVITY = "Robinhood account activity"
_PACKET = "Robinhood Form 1042-S packet"
_STATEMENT = "Robinhood brokerage statement"
_TRANSITIONS = "robinhood.cusip_transitions_file"
_CALENDAR = "date is not a valid calendar date"


# --- Activity CSV: Activity/Settle Date and description dates ----------------

_ACTIVITY_YEAR = "9101"
_DESCRIPTION_YEAR = "9102"
_FIRST_MIXED_YEAR = "9110"
_SECOND_MIXED_YEAR = "9111"


def _deposit_csv(activity_date: str, settle_date: str = _ROWS[1][2]) -> str:
    """One ACH Deposit record with these Activity Date and Settle Date cells."""
    _activity, process, _settle, *rest = _ROWS[1]
    return _csv(((activity_date, process, settle_date, *rest),))


def _dividend_csv(record_date: str) -> str:
    """One cash-dividend record whose Description prints ``record_date`` as R/D."""
    row = list(_ROWS[4])
    assert row[4].count("R/D 2025-01-01") == 1
    row[4] = row[4].replace("R/D 2025-01-01", f"R/D {record_date}")
    return _csv((tuple(row),))


_INVALID_ACTIVITY_DATE = _deposit_csv(f"2/30/{_ACTIVITY_YEAR}")
_LEAP_DAY_ACTIVITY_DATE = _deposit_csv("2/29/2024")
_INVALID_ACTIVITY_AND_SETTLE_DATES = _deposit_csv(
    f"2/30/{_FIRST_MIXED_YEAR}", settle_date=f"2/31/{_SECOND_MIXED_YEAR}"
)
_INVALID_RECORD_DATE = _dividend_csv(f"{_DESCRIPTION_YEAR}-02-30")
_LEAP_DAY_RECORD_DATE = _dividend_csv("2024-02-29")


def test_activity_date_refusal_chains_nothing() -> None:
    with pytest.raises(ParseError) as caught:
        parse_robinhood_activity(_INVALID_ACTIVITY_DATE)

    assert_unchained_refusal(
        caught.value,
        ParseError(
            section=_ACTIVITY,
            row_index=1,
            column="Activity Date",
            message=_CALENDAR,
            raw_value="",
        ),
        "ParseError in section 'Robinhood account activity' at row 1, "
        "column 'Activity Date': date is not a valid calendar date",
        markers=(_ACTIVITY_YEAR,),
    )


def test_leap_day_activity_date_still_parses() -> None:
    (event,) = parse_robinhood_activity(_LEAP_DAY_ACTIVITY_DATE)

    assert event.activity_date == date(2024, 2, 29)


def test_invalid_activity_and_settle_dates_still_refuse_the_activity_date_first() -> None:
    with pytest.raises(ParseError) as caught:
        parse_robinhood_activity(_INVALID_ACTIVITY_AND_SETTLE_DATES)

    assert_unchained_refusal(
        caught.value,
        ParseError(
            section=_ACTIVITY,
            row_index=1,
            column="Activity Date",
            message=_CALENDAR,
            raw_value="",
        ),
        "ParseError in section 'Robinhood account activity' at row 1, "
        "column 'Activity Date': date is not a valid calendar date",
        markers=(_FIRST_MIXED_YEAR,),
    )


def test_description_date_refusal_chains_nothing() -> None:
    with pytest.raises(ParseError) as caught:
        parse_robinhood_activity(_INVALID_RECORD_DATE)

    assert_unchained_refusal(
        caught.value,
        ParseError(
            section=_ACTIVITY,
            row_index=1,
            column="Description",
            message="description contains an invalid calendar date",
            raw_value="",
        ),
        "ParseError in section 'Robinhood account activity' at row 1, "
        "column 'Description': description contains an invalid calendar date",
        markers=(_DESCRIPTION_YEAR,),
    )


def test_leap_day_description_date_still_parses() -> None:
    (event,) = parse_robinhood_activity(_LEAP_DAY_RECORD_DATE)

    assert event.record_date == date(2024, 2, 29)


# --- Activity CSV file: bytes that aren't UTF-8 ------------------------------

_CSV_BYTES_MARKER = "QZRHCSVBYTES"


def _activity_bytes_with_invalid_utf8() -> bytes:
    """The one-purchase CSV whose issuer name is the marker and a byte that isn't UTF-8."""
    text = _csv((_ROWS[7],)).encode("utf-8")
    assert text.count(b"Ordinary Purchase Example") == 1
    return text.replace(b"Ordinary Purchase Example", _CSV_BYTES_MARKER.encode("ascii") + b" \xff")


_INVALID_UTF8_ACTIVITY = _activity_bytes_with_invalid_utf8()


def test_activity_file_that_is_not_utf8_refusal_chains_nothing(tmp_path: Path) -> None:
    path = tmp_path / "activity.csv"
    path.write_bytes(_INVALID_UTF8_ACTIVITY)

    with pytest.raises(ParseError) as caught:
        read_robinhood_activity(path)

    assert_unchained_refusal(
        caught.value,
        ParseError(
            section=_ACTIVITY,
            row_index=0,
            column=None,
            message="could not read Robinhood activity CSV as UTF-8",
            raw_value="",
        ),
        "ParseError in section 'Robinhood account activity' at row 0: "
        "could not read Robinhood activity CSV as UTF-8",
        markers=(_CSV_BYTES_MARKER,),
    )


# --- Form 1042-S: recipient date of birth -------------------------------------

_BIRTH_YEAR = "9103"
_INVALID_BIRTH_DATE_PACKET = _both_copies(_SPACED_DOB, " ".join(f"{_BIRTH_YEAR}0230"))
_LEAP_DAY_BIRTH_DATE_PACKET = _both_copies(_SPACED_DOB, " ".join("20240229"))


def test_birth_date_refusal_chains_nothing() -> None:
    with pytest.raises(ParseError) as caught:
        parse_robinhood_1042s_packet(_INVALID_BIRTH_DATE_PACKET)

    assert_unchained_refusal(
        caught.value,
        ParseError(
            section=_PACKET,
            row_index=1,
            column="Box 13l",
            message="Form 1042-S recipient date of birth is invalid",
            raw_value="",
        ),
        "ParseError in section 'Robinhood Form 1042-S packet' at row 1, "
        "column 'Box 13l': Form 1042-S recipient date of birth is invalid",
        markers=(_BIRTH_YEAR,),
    )


def test_leap_day_birth_date_still_parses() -> None:
    packet = parse_robinhood_1042s_packet(_LEAP_DAY_BIRTH_DATE_PACKET)

    assert [copy.recipient.date_of_birth for copy in packet.copies] == [date(2024, 2, 29)] * 2


# --- Brokerage statement: a row's date and an option contract's expiration ----

_ROW_YEAR = "9104"
_EXPIRATION_YEAR = "9105"


def _statement_with_purchase_dated(printed: str) -> str:
    """The statement with its Account Activity purchase row dated ``printed``,
    as wide as the date it replaces so every column keeps its place."""
    page = _activity_page()
    assert page.count("1/11/2025") == 1
    assert len(printed) == len("1/11/2025")
    return _statement().replace(page, page.replace("1/11/2025", printed))


def _option_trade_expiring(printed: str) -> str:
    """The statement plus an Account Activity BTO row whose contract expires ``printed``."""
    assert _ACTIVITY_CONTRACT.count("11/22/2033") == 1
    return _option_activity(_ACTIVITY_CONTRACT.replace("11/22/2033", printed), code="BTO")


_INVALID_ROW_DATE = _statement_with_purchase_dated(f"2/30/{_ROW_YEAR}")
_LEAP_DAY_ROW_DATE = _statement_with_purchase_dated("2/29/2024")
_INVALID_EXPIRATION = _option_trade_expiring(f"2/30/{_EXPIRATION_YEAR}")
_LEAP_DAY_EXPIRATION = _option_trade_expiring("2/29/2024")


def test_statement_row_date_refusal_chains_nothing() -> None:
    with pytest.raises(ParseError) as caught:
        parse_robinhood_statement(_INVALID_ROW_DATE)

    assert_unchained_refusal(
        caught.value,
        ParseError(
            section=_STATEMENT,
            row_index=3,
            column="Date",
            message=_CALENDAR,
            raw_value="",
        ),
        "ParseError in section 'Robinhood brokerage statement' at row 3, "
        "column 'Date': date is not a valid calendar date",
        markers=(_ROW_YEAR,),
    )


def test_leap_day_statement_row_date_still_parses() -> None:
    statement = parse_robinhood_statement(_LEAP_DAY_ROW_DATE)

    (purchase,) = (
        row
        for row in statement.activities
        if row.location.section is RobinhoodStatementSection.ACCOUNT_ACTIVITY
        and row.transaction_code == "Buy"
    )
    assert purchase.transaction_date == date(2024, 2, 29)


def test_option_contract_expiration_refusal_chains_no_inner_refusal() -> None:
    """The contract's wrapper refuses outside its ``except ParseError``, so
    neither the inner calendar refusal nor its ``ValueError`` is chained."""
    with pytest.raises(ParseError) as caught:
        parse_robinhood_statement(_INVALID_EXPIRATION)

    assert_unchained_refusal(
        caught.value,
        ParseError(
            section=_STATEMENT,
            row_index=3,
            column="Description",
            message=_ACTIVITY_MALFORMED,
            raw_value="",
        ),
        "ParseError in section 'Robinhood brokerage statement' at row 3, "
        "column 'Description': activity option contract is malformed",
        markers=(_EXPIRATION_YEAR,),
    )


def test_leap_day_option_contract_expiration_still_parses() -> None:
    statement = parse_robinhood_statement(_LEAP_DAY_EXPIRATION)

    (trade,) = (row for row in statement.activities if row.transaction_code == "BTO")
    assert (trade.location.section, trade.option_expiration) == (
        RobinhoodStatementSection.ACCOUNT_ACTIVITY,
        date(2024, 2, 29),
    )


# --- CUSIP transitions file: bytes that aren't UTF-8, invalid TOML ------------

_TOML_BYTES_MARKER = "QZTOMLBYTES"
_DUPLICATE_TABLE_MARKER = "QZDUPTABLE"
_DOCUMENT_ONLY_MARKER = "QZDOCONLY"
# The bad byte follows "# ", the marker and a blank: offset 14.
_INVALID_UTF8_TRANSITIONS = (
    f"# {_TOML_BYTES_MARKER} ".encode("ascii") + b"\xff\n" + _transition().encode("utf-8")
)
# tomllib's message quotes the duplicated table on every runtime; only
# Python 3.14's error keeps the whole document, comment line included, as ``doc``.
_DUPLICATE_TABLE_TRANSITIONS = (
    f"# {_DOCUMENT_ONLY_MARKER}\n[{_DUPLICATE_TABLE_MARKER}]\n[{_DUPLICATE_TABLE_MARKER}]\n"
)


def test_transitions_file_that_is_not_utf8_refusal_chains_nothing(tmp_path: Path) -> None:
    path = tmp_path / "cusip-transitions.toml"
    path.write_bytes(_INVALID_UTF8_TRANSITIONS)

    with pytest.raises(ConfigError) as caught:
        load_cusip_transitions(path, account_numbers=_ACCOUNTS)

    assert_unchained_refusal(
        caught.value,
        ConfigError(
            field=_TRANSITIONS,
            message=(
                "must be UTF-8 text, but the byte at offset 14 (counting from 0) is not valid UTF-8"
            ),
        ),
        "ConfigError at field 'robinhood.cusip_transitions_file': must be UTF-8 text, "
        "but the byte at offset 14 (counting from 0) is not valid UTF-8",
        markers=(_TOML_BYTES_MARKER,),
    )


def test_transitions_file_with_invalid_toml_refusal_chains_nothing(tmp_path: Path) -> None:
    """The comment marker was only in Python 3.14's ``TOMLDecodeError.doc`` on
    base; every runtime's message quotes the duplicated table's name."""
    path = tmp_path / "cusip-transitions.toml"
    path.write_text(_DUPLICATE_TABLE_TRANSITIONS, encoding="utf-8")

    with pytest.raises(ConfigError) as caught:
        load_cusip_transitions(path, account_numbers=_ACCOUNTS)

    assert_unchained_refusal(
        caught.value,
        ConfigError(field=_TRANSITIONS, message="invalid TOML at line 3, column 12"),
        "ConfigError at field 'robinhood.cusip_transitions_file': "
        "invalid TOML at line 3, column 12",
        markers=(_DUPLICATE_TABLE_MARKER, _DOCUMENT_ONLY_MARKER),
    )
