"""``schedule-fa`` still stops on Account Information metadata refusals (#1190).

Neither IBKR parser keeps the Flex Query ``AccountType`` / ``IBEntity`` or the
Activity Statement ``Account Type`` any more, but both still require them. The
run exits 4 with the complete message, prints no account, holder or other cell
text, and leaves no output, removing an earlier run's. Every input is synthetic.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tests._crash_reports import assert_no_markers
from tests._ibkr_fa_sums_inputs import IBKR_FA_SUMS_CONFIG as _CONFIG
from tests._ibkr_fa_sums_inputs import ibkr_fa_sums_statement as _statement
from tests._ibkr_fa_sums_inputs import run_schedule_fa_cy2025 as _schedule_fa
from tests._ibkr_flex_cli_inputs import CLI_OPEN_POSITIONS_SECTION as _OPEN_POSITIONS_SECTION

_MARKERS = ("SYNTHCLEANACCOUNT", "SYNTHCLEANNAME", "SYNTHCLEANDESC")
_FLEX_FIELDS = {
    "ClientAccountID": "SYNTHCLEANACCOUNT",
    "CurrencyPrimary": "USD",
    "Name": "SYNTHCLEANNAME",
    "AccountType": "Individual",
    "IBEntity": "IBLLC-US",
    "MasterName": "SYNTHCLEANDESC",
}
_FLEX_MISSING = (
    "error: ParseError in section 'Account Information' at row 0, column '{0}': required "
    "Account Information column '{0}' is missing from the section header. Re-run your "
    "IBKR Flex Query with this field enabled.\n"
)
_FLEX_EMPTY = (
    "error: ParseError in section 'Account Information' at row 0, column 'AccountType': "
    "required Account Information column 'AccountType' is empty in the data row.\n"
)
_STATEMENT_MISSING = (
    "error: ParseError in section 'Account Information' at row 0, column 'Account Type': "
    "Activity Statement Account Information section is missing required field(s) "
    "['Account Type']. Re-export the statement with all account-identity fields enabled, "
    "or pass --accept-anonymised-statement (with [account].holder_name + [account].number "
    "set in user.toml) to accept hidden Name / Account fields.\n"
)
_STATEMENT_EMPTY = (
    "error: ParseError in section 'Account Information' at row 0, column 'Account Type': "
    "Activity Statement Account Information field 'Account Type' has an empty value.\n"
)


def _flex(fault: str) -> str:
    fields = dict(_FLEX_FIELDS)
    if fault == "account-type-empty":
        fields["AccountType"] = ""
    else:
        del fields["AccountType" if fault == "account-type-missing" else "IBEntity"]
    header = ",".join(f'"{name}"' for name in fields)
    row = ",".join(f'"{value}"' for value in fields.values())
    return f"{header}\n{row}\n\n{_OPEN_POSITIONS_SECTION}"


def _activity_statement(fault: str) -> str:
    statement = (
        _statement()
        .replace("Data,Name,Sample Holder\n", "Data,Name,SYNTHCLEANNAME\n")
        .replace("Data,Account,U12345678\n", "Data,Account,SYNTHCLEANACCOUNT\n")
        .replace("Data,Customer Type,Individual\n", "Data,Customer Type,SYNTHCLEANDESC\n")
    )
    row = "Account Information,Data,Account Type,Individual\n"
    assert statement.count(row) == 1
    return statement.replace(row, "" if fault == "missing" else row.replace("Individual", ""))


@pytest.mark.parametrize("stale", [False, True], ids=["fresh", "stale"])
@pytest.mark.parametrize(
    ("text", "expected"),
    [
        pytest.param(
            _flex("account-type-missing"),
            _FLEX_MISSING.format("AccountType"),
            id="flex-account-type-missing",
        ),
        pytest.param(_flex("account-type-empty"), _FLEX_EMPTY, id="flex-account-type-empty"),
        pytest.param(
            _flex("ib-entity-missing"),
            _FLEX_MISSING.format("IBEntity"),
            id="flex-ib-entity-missing",
        ),
        pytest.param(
            _activity_statement("missing"), _STATEMENT_MISSING, id="statement-account-type-missing"
        ),
        pytest.param(
            _activity_statement("empty"), _STATEMENT_EMPTY, id="statement-account-type-empty"
        ),
    ],
)
def test_schedule_fa_refuses_with_the_complete_message_and_no_output(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    text: str,
    expected: str,
    stale: bool,
) -> None:
    statement = tmp_path / "statement.csv"
    statement.write_text(text, encoding="utf-8")
    config = tmp_path / "user.toml"
    config.write_text(_CONFIG, encoding="utf-8")
    out = tmp_path / "schedule-fa.csv"
    a2 = tmp_path / "schedule-fa-a2.csv"
    if stale:
        out.write_text("stale", encoding="utf-8")
        a2.write_text("stale", encoding="utf-8")

    assert _schedule_fa(statement, config, out) == 4

    captured = capsys.readouterr()
    assert captured.err == expected
    assert captured.out == ""
    for marker in _MARKERS:
        assert marker in text, marker
    assert_no_markers(captured.out + captured.err, *_MARKERS)
    assert not out.exists()
    assert not a2.exists()
