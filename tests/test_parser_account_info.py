"""Tests for :mod:`ibkr_fa.parser.account_info`."""

from __future__ import annotations

import pytest

from ibkr_fa.exceptions import ParseError
from ibkr_fa.parser import parse_account_info
from ibkr_fa.types import AccountId
from tests._crash_reports import render_without_frames


def make_row(**overrides: str) -> dict[str, str]:
    base = {
        "ClientAccountID": "U1234567",
        "AccountAlias": "",
        "Model": "",
        "CurrencyPrimary": "USD",
        "Name": "Test Holder",
        "AccountType": "Individual",
        "CustomerType": "INDIVIDUAL",
        "AccountCapabilities": "Margin",
        "TradingPermissions": "Stocks;Options",
        "DateOpened": "20200101",
        "DateFunded": "20200110",
        "DateClosed": "",
        "MasterName": "Test Master",
        "IBEntity": "IBLLC-US",
        "PrimaryEmail": "",
    }
    base.update(overrides)
    return base


def test_parses_valid_row() -> None:
    info = parse_account_info([make_row()])
    assert info.account_id == AccountId("U1234567")
    assert info.account_holder_name == "Test Holder"
    assert info.base_currency == "USD"


def test_tolerates_extra_columns() -> None:
    row = make_row(SomeFutureFlexColumn="ignore-me")
    info = parse_account_info([row])
    assert info.account_id == AccountId("U1234567")


def test_tolerates_minimal_row() -> None:
    row = {
        "ClientAccountID": "U7654321",
        "Name": "Minimal Holder",
        "AccountType": "Joint",
        "CurrencyPrimary": "USD",
        "IBEntity": "IBKR-IE",
    }
    info = parse_account_info([row])
    assert info.account_id == AccountId("U7654321")


def test_allows_empty_ibentity() -> None:
    # The IBEntity COLUMN must be present (so users get a clear error if
    # their Flex Query disables it), but the VALUE may be blank.
    info = parse_account_info([make_row(IBEntity="")])
    assert info.account_id == AccountId("U1234567")


def test_rejects_empty_input() -> None:
    with pytest.raises(ParseError) as exc:
        parse_account_info([])
    assert exc.value.section == "Account Information"
    assert exc.value.row_index == 0
    assert "no data rows" in exc.value.message


def test_rejects_multiple_rows() -> None:
    with pytest.raises(ParseError) as exc:
        parse_account_info([make_row(), make_row(ClientAccountID="U2222222")])
    assert exc.value.section == "Account Information"
    assert exc.value.row_index == 1
    assert "2 data rows" in exc.value.message
    assert "multi-account" in exc.value.message


def test_rejects_missing_client_account_id_column() -> None:
    row = make_row()
    del row["ClientAccountID"]
    with pytest.raises(ParseError) as exc:
        parse_account_info([row])
    assert exc.value.column == "ClientAccountID"
    assert "missing" in exc.value.message
    assert "Re-run your IBKR" in exc.value.message


def test_rejects_missing_name_column() -> None:
    row = make_row()
    del row["Name"]
    with pytest.raises(ParseError) as exc:
        parse_account_info([row])
    assert exc.value.column == "Name"


def test_rejects_missing_account_type_column() -> None:
    row = make_row()
    del row["AccountType"]
    with pytest.raises(ParseError) as exc:
        parse_account_info([row])
    assert exc.value.column == "AccountType"


def test_rejects_missing_currency_primary_column() -> None:
    row = make_row()
    del row["CurrencyPrimary"]
    with pytest.raises(ParseError) as exc:
        parse_account_info([row])
    assert exc.value.column == "CurrencyPrimary"


def test_rejects_missing_ib_entity_column() -> None:
    row = make_row()
    del row["IBEntity"]
    with pytest.raises(ParseError) as exc:
        parse_account_info([row])
    assert exc.value.column == "IBEntity"


def test_rejects_empty_client_account_id_value() -> None:
    with pytest.raises(ParseError) as exc:
        parse_account_info([make_row(ClientAccountID="")])
    assert exc.value.column == "ClientAccountID"
    assert "empty" in exc.value.message


def test_rejects_whitespace_only_client_account_id_value() -> None:
    with pytest.raises(ParseError) as exc:
        parse_account_info([make_row(ClientAccountID="   ")])
    assert exc.value.column == "ClientAccountID"


def test_rejects_empty_name_value() -> None:
    with pytest.raises(ParseError) as exc:
        parse_account_info([make_row(Name="")])
    assert exc.value.column == "Name"


def test_rejects_empty_account_type_value() -> None:
    with pytest.raises(ParseError) as exc:
        parse_account_info([make_row(AccountType="")])
    assert exc.value.column == "AccountType"


def test_rejects_empty_currency_primary_value() -> None:
    with pytest.raises(ParseError) as exc:
        parse_account_info([make_row(CurrencyPrimary="")])
    assert exc.value.column == "CurrencyPrimary"


@pytest.mark.parametrize("currency", ["USD", "EUR"])
def test_parses_valid_currency_primary(currency: str) -> None:
    assert parse_account_info([make_row(CurrencyPrimary=currency)]).base_currency == currency


@pytest.mark.parametrize("currency", ["Synthetic Holder Two", "usd", "US", "USDX", "USD\n", "ÜSD"])
def test_rejects_malformed_currency_primary_without_echoing_it(currency: str) -> None:
    """#1021: a shifted cell can put the holder's name or account number in
    CurrencyPrimary, so the refusal names the column but echoes no value."""
    with pytest.raises(ParseError) as exc:
        parse_account_info([make_row(CurrencyPrimary=currency)])
    assert exc.value.section == "Account Information"
    assert exc.value.column == "CurrencyPrimary"
    assert exc.value.raw_value == ""
    # strip(): repr would print "USD\n" as an escaped 'USD\\n'.
    assert currency.strip() not in str(exc.value)


def test_first_failing_column_wins_in_required_order() -> None:
    # When multiple required columns are missing, the first one in the
    # checked order (ClientAccountID, Name, AccountType, CurrencyPrimary,
    # IBEntity) is reported, matching the parser's fail-fast contract.
    row = make_row()
    del row["Name"]
    del row["AccountType"]
    with pytest.raises(ParseError) as exc:
        parse_account_info([row])
    assert exc.value.column == "Name"


# ---- #1190: AccountType / IBEntity are checked, not kept --------------------

_CLEAN_MARKERS = ("SYNTHCLEANACCOUNT", "SYNTHCLEANNAME", "SYNTHCLEANDESC")
_MISSING = (
    "ParseError in section 'Account Information' at row 0, column '{0}': required "
    "Account Information column '{0}' is missing from the section header. Re-run your "
    "IBKR Flex Query with this field enabled."
)
_EMPTY = (
    "ParseError in section 'Account Information' at row 0, column '{0}': required "
    "Account Information column '{0}' is empty in the data row."
)
_CURRENCY = (
    "ParseError in section 'Account Information' at row 0, column 'CurrencyPrimary': "
    "Account Information column 'CurrencyPrimary' must be 3 uppercase ASCII letters "
    "(ISO 4217)."
)


@pytest.mark.parametrize(
    ("overrides", "dropped", "column", "message"),
    [
        pytest.param({}, ("AccountType",), "AccountType", _MISSING, id="account-type-missing"),
        pytest.param({"AccountType": ""}, (), "AccountType", _EMPTY, id="account-type-empty"),
        pytest.param({"AccountType": " \t"}, (), "AccountType", _EMPTY, id="account-type-blank"),
        pytest.param({}, ("IBEntity",), "IBEntity", _MISSING, id="ib-entity-missing"),
        pytest.param({"Name": ""}, ("AccountType",), "Name", _EMPTY, id="name-before-account-type"),
        pytest.param(
            {"CurrencyPrimary": "SYNTHCLEANDESC"},
            ("AccountType",),
            "AccountType",
            _MISSING,
            id="account-type-before-currency",
        ),
        pytest.param(
            {"AccountType": ""},
            ("IBEntity",),
            "AccountType",
            _EMPTY,
            id="account-type-before-ib-entity",
        ),
        pytest.param(
            {"CurrencyPrimary": "SYNTHCLEANDESC"},
            ("IBEntity",),
            "CurrencyPrimary",
            _CURRENCY,
            id="currency-before-ib-entity",
        ),
    ],
)
def test_metadata_refusals_keep_their_message_frame_and_order(
    overrides: dict[str, str],
    dropped: tuple[str, ...],
    column: str,
    message: str,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The parser no longer keeps AccountType or IBEntity, but still checks
    both at the same points: ClientAccountID, Name, AccountType and
    CurrencyPrimary in that order, its shape, then IBEntity. Each refusal is
    the complete message in the row-0 frame, with an empty ``raw_value``, no
    chain and no account, holder or other cell text."""
    marked = {
        "ClientAccountID": "SYNTHCLEANACCOUNT",
        "Name": "SYNTHCLEANNAME",
        "MasterName": "SYNTHCLEANDESC",
    }
    row = make_row(**(marked | overrides))
    for name in dropped:
        del row[name]
    with pytest.raises(ParseError) as raised:
        parse_account_info([row])

    error = raised.value
    assert type(error) is ParseError
    assert str(error) == message.format(column)
    assert (error.section, error.row_index, error.column, error.raw_value) == (
        "Account Information",
        0,
        column,
        "",
    )
    assert (error.__cause__, error.__context__, error.__suppress_context__) == (None, None, False)
    rendered = render_without_frames(error) + repr(error)
    assert capsys.readouterr() == ("", "")
    for marker in (marker for marker in _CLEAN_MARKERS if marker in row.values()):
        assert marker not in rendered, marker
