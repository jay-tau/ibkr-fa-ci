"""Tests for :mod:`ibkr_fa.parser.activity_statement.account_info`."""

from __future__ import annotations

import csv

import pytest

from ibkr_fa.exceptions import ParseError
from ibkr_fa.parser.activity_statement import (
    KnownActivitySection,
    parse_activity_statement_account_info,
    split_activity_statement_sections,
)
from ibkr_fa.types import AccountId
from tests._crash_reports import render_without_frames
from tests._private_checks import private_test
from tests._private_data import private_dataset


def make_field_row(name: str, value: str) -> dict[str, str]:
    """Build one Activity-Statement-shaped Account Information row."""
    return {"Field Name": name, "Field Value": value}


def make_full_rows(**overrides: str) -> list[dict[str, str]]:
    """Construct the full set of Account Information rows IBKR
    typically emits, in order. Override any field's value via
    kwargs; pass the empty string to test empty-value rejection.
    """
    base: dict[str, str] = {
        "Name": "TEST USER",
        "Account": "U12345678",
        "Account Type": "Individual",
        "Customer Type": "Individual",
        "Account Capabilities": "Cash",
        "Base Currency": "USD",
    }
    base.update(overrides)
    return [make_field_row(name, value) for name, value in base.items()]


# ---- happy path ------------------------------------------------------------


def test_parses_a_realistic_row_set() -> None:
    info, fallback_used = parse_activity_statement_account_info(make_full_rows())
    assert info.account_id == AccountId("U12345678")
    assert info.account_holder_name == "TEST USER"
    assert info.base_currency == "USD"
    assert fallback_used == frozenset()


def test_extra_optional_fields_tolerated() -> None:
    """IBKR Activity Statements emit more fields than we map (e.g.,
    Customer Type, Account Capabilities, Trading Permissions...).
    The parser must tolerate them silently — they're just extra
    dict entries we don't read."""
    rows = make_full_rows()
    rows.append(make_field_row("Trading Permissions", "Stocks;Options"))
    rows.append(make_field_row("Future Field We Haven't Seen", "..."))
    info, _ = parse_activity_statement_account_info(rows)
    # Required fields still populated correctly.
    assert info.account_id == AccountId("U12345678")


def test_field_value_with_commas_preserved() -> None:
    """The holder Name may contain commas (e.g., 'LAST, FIRST').
    The Field Value is a single CSV cell so commas inside it
    must round-trip intact."""
    rows = make_full_rows(Name="PAYER, TAX")
    info, _ = parse_activity_statement_account_info(rows)
    assert info.account_holder_name == "PAYER, TAX"


# ---- IBKR's masked account number (real-world case) ------------------------


def test_masked_account_id_passes_through() -> None:
    """IBKR masks the account number by default in Activity
    Statements (the user can re-export with full account number
    enabled). The parser does NOT reject masked values — that's
    the pipeline's job (a downstream PR adds an
    --allow-masked-account flag). For now we faithfully pass the
    masked value through so the cross-check can decide."""
    info, _ = parse_activity_statement_account_info(make_full_rows(Account="U**********"))
    assert info.account_id == AccountId("U**********")


# ---- required-field enforcement -------------------------------------------


def test_empty_input_raises() -> None:
    with pytest.raises(ParseError) as exc:
        parse_activity_statement_account_info([])
    assert exc.value.section == "Account Information"
    assert "no data rows" in exc.value.message


@pytest.mark.parametrize(
    "missing_field",
    ["Name", "Account", "Account Type", "Base Currency"],
)
def test_missing_required_field_raises(missing_field: str) -> None:
    """Each required Activity-Statement field, when absent from the
    transposed dict, must produce a ParseError naming THAT field
    so the user knows exactly what to look for in IBKR's exporter."""
    rows = [r for r in make_full_rows() if r["Field Name"] != missing_field]
    with pytest.raises(ParseError) as exc:
        parse_activity_statement_account_info(rows)
    assert exc.value.column == missing_field
    assert missing_field in exc.value.message
    assert "Re-export the statement" in exc.value.message


@pytest.mark.parametrize(
    "empty_field",
    ["Name", "Account", "Account Type", "Base Currency"],
)
def test_empty_required_value_raises(empty_field: str) -> None:
    """A field present in the rows but with an empty value is just
    as broken as an absent field — reject loudly."""
    with pytest.raises(ParseError) as exc:
        parse_activity_statement_account_info(make_full_rows(**{empty_field: ""}))
    assert exc.value.column == empty_field
    assert "empty value" in exc.value.message


def test_whitespace_only_value_treated_as_empty() -> None:
    """A whitespace-only Account number is not a real account
    number — reject."""
    with pytest.raises(ParseError) as exc:
        parse_activity_statement_account_info(make_full_rows(Account="   "))
    assert exc.value.column == "Account"


# ---- base currency shape (#1021) --------------------------------------------


@pytest.mark.parametrize("currency", ["USD", "EUR"])
def test_valid_base_currency_parses(currency: str) -> None:
    info, _ = parse_activity_statement_account_info(make_full_rows(**{"Base Currency": currency}))
    assert info.base_currency == currency


@pytest.mark.parametrize("currency", ["Synthetic Holder Two", "usd", "US", "USDX", "USD\n", "ÜSD"])
def test_malformed_base_currency_refuses_without_echoing_it(currency: str) -> None:
    """#1021: a shifted cell can put the holder's name or account number in
    Base Currency, so the refusal names the field but echoes no value."""
    with pytest.raises(ParseError) as exc:
        parse_activity_statement_account_info(make_full_rows(**{"Base Currency": currency}))
    assert exc.value.section == "Account Information"
    assert exc.value.column == "Base Currency"
    assert exc.value.raw_value == ""
    # strip(): repr would print "USD\n" as an escaped 'USD\\n'.
    assert currency.strip() not in str(exc.value)


# ---- #141 anonymised-statement fallback -----------------------------------


def test_fallback_used_when_name_absent_and_fallback_supplied() -> None:
    """#141: an Activity Statement export that hides the Name row
    (privacy option in IBKR's exporter) is accepted when the caller
    supplies a fallback name. The parser substitutes the fallback
    and records ``"Name"`` in the fallback-used set so the pipeline
    can surface a loud warning via RunReport."""
    rows = [r for r in make_full_rows() if r["Field Name"] != "Name"]
    info, fallback_used = parse_activity_statement_account_info(
        rows, fallback_account_holder_name="Sample Holder"
    )
    assert info.account_holder_name == "Sample Holder"
    assert info.account_id == AccountId("U12345678")
    assert fallback_used == frozenset({"Name"})


def test_fallback_used_when_account_absent_and_fallback_supplied() -> None:
    """#141: same shape but the Account row is hidden — fallback
    account ID substituted, fallback-used set records ``"Account"``."""
    rows = [r for r in make_full_rows() if r["Field Name"] != "Account"]
    info, fallback_used = parse_activity_statement_account_info(
        rows, fallback_account_id="U99999999"
    )
    assert info.account_id == AccountId("U99999999")
    assert info.account_holder_name == "TEST USER"
    assert fallback_used == frozenset({"Account"})


def test_fallback_used_when_both_name_and_account_absent() -> None:
    """#141: real-world shape — both Name and Account
    hidden simultaneously. Both fallback values consumed, both
    field names in the fallback-used set."""
    rows = [r for r in make_full_rows() if r["Field Name"] not in {"Name", "Account"}]
    info, fallback_used = parse_activity_statement_account_info(
        rows,
        fallback_account_holder_name="Sample Holder",
        fallback_account_id="U12345678",
    )
    assert info.account_holder_name == "Sample Holder"
    assert info.account_id == AccountId("U12345678")
    assert fallback_used == frozenset({"Name", "Account"})


def test_fallback_ignored_when_statement_provides_field() -> None:
    """#141: fallback is an absence-only safety net — when the
    statement provides Name (even a different value), the
    statement-provided value wins and fallback-used is empty."""
    rows = make_full_rows(Name="STATEMENT NAME")
    info, fallback_used = parse_activity_statement_account_info(
        rows, fallback_account_holder_name="FALLBACK NAME"
    )
    assert info.account_holder_name == "STATEMENT NAME"
    assert fallback_used == frozenset()


def test_missing_name_without_fallback_still_raises() -> None:
    """#141: hard-fail is preserved when no fallback supplied — the
    feature is opt-in via the CLI flag."""
    rows = [r for r in make_full_rows() if r["Field Name"] != "Name"]
    with pytest.raises(ParseError) as exc:
        parse_activity_statement_account_info(rows)
    assert exc.value.column == "Name"
    assert "--accept-anonymised-statement" in exc.value.message


def test_missing_account_without_fallback_still_raises() -> None:
    """#141: symmetric to the Name case."""
    rows = [r for r in make_full_rows() if r["Field Name"] != "Account"]
    with pytest.raises(ParseError) as exc:
        parse_activity_statement_account_info(rows)
    assert exc.value.column == "Account"
    assert "--accept-anonymised-statement" in exc.value.message


def test_fallback_does_not_cover_account_type() -> None:
    """#141: only Name + Account are user-hideable in IBKR's
    exporter. Account Type / Base Currency are always emitted by
    IBKR, so they are NOT in the fallback contract — a missing
    Account Type with all fallbacks supplied still raises."""
    rows = [r for r in make_full_rows() if r["Field Name"] != "Account Type"]
    with pytest.raises(ParseError) as exc:
        parse_activity_statement_account_info(
            rows,
            fallback_account_holder_name="Sample Holder",
            fallback_account_id="U12345678",
        )
    assert exc.value.column == "Account Type"


def test_fallback_supplied_but_unused_when_all_fields_present() -> None:
    """#141: supplying fallback on a complete statement is a no-op
    (fallback-used empty), so the dispatcher can always pass
    fallback through under the flag without changing behaviour
    on non-anonymised exports."""
    info, fallback_used = parse_activity_statement_account_info(
        make_full_rows(),
        fallback_account_holder_name="UNUSED FALLBACK",
        fallback_account_id="U99999999",
    )
    assert info.account_holder_name == "TEST USER"
    assert info.account_id == AccountId("U12345678")
    assert fallback_used == frozenset()


# ---- malformed input ------------------------------------------------------


def test_row_missing_field_name_column_raises() -> None:
    """If a row lacks the 'Field Name' column entirely (shouldn't
    happen because the splitter guarantees the column tuple, but
    defensive), raise with both the section and the missing-column
    hint."""
    with pytest.raises(ParseError) as exc:
        parse_activity_statement_account_info([{"Field Value": "x"}])
    assert exc.value.section == "Account Information"
    assert exc.value.column == "Field Name"


def test_row_missing_field_value_column_raises() -> None:
    with pytest.raises(ParseError) as exc:
        parse_activity_statement_account_info([{"Field Name": "Name"}])
    assert exc.value.column == "Field Value"


def test_first_missing_field_wins() -> None:
    """When multiple required fields are absent, the FIRST one in
    the IBKR order (Name, Account, Account Type, Base Currency) is
    reported — matches the user-facing "fix one thing at a time"
    semantic."""
    rows = [
        # Same as make_full_rows() but with Account and Base Currency
        # deliberately removed.
        make_field_row("Name", "TEST USER"),
        make_field_row("Account Type", "Individual"),
    ]
    with pytest.raises(ParseError) as exc:
        parse_activity_statement_account_info(rows)
    # Account comes before Base Currency in the required list.
    assert exc.value.column == "Account"


# ---- duplicate-field rejection --------------------------------------------


def test_duplicate_field_name_raises() -> None:
    """Codex P2 on PR #103: a malformed export (or a multi-account
    Activity Statement IBKR can emit) may include the same
    Field Name twice. Silently letting the second row win would
    yield an AccountInfo that doesn't represent all of the input —
    raise instead so the user investigates."""
    rows = [
        *make_full_rows(),
        # Inject a SECOND Account row with a different value to
        # simulate either malformed input or a multi-account export.
        make_field_row("Account", "U99999999"),
    ]
    with pytest.raises(ParseError) as exc:
        parse_activity_statement_account_info(rows)
    assert exc.value.column == "Field Name"
    assert "repeats an earlier row's Field Name" in exc.value.message
    assert "multiple accounts" in exc.value.message
    # The error names the row index of the duplicate, which is the
    # second occurrence — useful for the user to find in their file.
    assert exc.value.row_index == len(rows) - 1


@pytest.mark.parametrize(
    ("field", "first", "second"),
    [
        pytest.param("Name", "Synthetic Holder One", "Synthetic Holder Two", id="name"),
        pytest.param("Customer Type", "Individual", "Joint", id="optional-field"),
        pytest.param("Account", "U12345678", "U99999999", id="account"),
        pytest.param("Account", "U12345678", "Lee", id="short-account"),
    ],
)
def test_duplicate_field_refuses_without_echoing_either_value(
    field: str, first: str, second: str
) -> None:
    """#1017: :meth:`ParseError.__str__` prints ``raw_value`` verbatim, and
    Account Information values can identify the holder, so a duplicate
    field refuses with no value at all, masked or not. The check is
    structural, so fields this parser doesn't model (Customer Type)
    refuse too. Masking isn't enough for Account: :func:`display_account_id`
    returns a short value such as ``Lee`` unchanged. Since #1040 the error
    names the ``Field Name`` column, not the field."""
    rows = [*make_full_rows(**{field: first}), make_field_row(field, second)]
    with pytest.raises(ParseError) as exc:
        parse_activity_statement_account_info(rows)
    assert exc.value.column == "Field Name"
    assert exc.value.raw_value == ""
    rendered = str(exc.value)
    assert first not in rendered
    assert second not in rendered
    assert repr(field) not in rendered


# ---- end-to-end against a private statement -------------------------------


@private_test
def test_works_against_private_fy2025_26_statement() -> None:
    """End-to-end: load the private statement through the splitter and
    hand its Account Information block to this parser. Each parsed field
    equals the statement's own row for it, read with ``csv`` straight from
    the file."""
    [sample] = private_dataset("ibkr/activity-statement-fy2025-26.csv")
    with sample.open(newline="", encoding="utf-8-sig") as source:
        fields = {
            row[2]: row[3]
            for row in csv.reader(source)
            if row[:2] == ["Account Information", "Data"]
        }

    sections = split_activity_statement_sections(sample)
    [ai_block] = sections[KnownActivitySection.ACCOUNT_INFORMATION]
    info, fallback_used = parse_activity_statement_account_info(ai_block.rows)
    assert fallback_used == frozenset()
    assert info.account_holder_name == fields["Name"]
    assert info.account_id == AccountId(fields["Account"])
    assert info.base_currency == fields["Base Currency"]


# ---- #1190: Account Type is checked, not kept -------------------------------

_CLEAN_NAME = "SYNTHCLEANNAME"
_CLEAN_ACCOUNT = "SYNTHCLEANACCOUNT"
_CLEAN_DESC = "SYNTHCLEANDESC"
_CLEAN_MARKERS = (_CLEAN_ACCOUNT, _CLEAN_NAME, _CLEAN_DESC)
_MISSING = (
    "ParseError in section 'Account Information' at row 0, column '{0}': Activity "
    "Statement Account Information section is missing required field(s) {1}. Re-export "
    "the statement with all account-identity fields enabled, or pass "
    "--accept-anonymised-statement (with [account].holder_name + [account].number set in "
    "user.toml) to accept hidden Name / Account fields."
)
_EMPTY = (
    "ParseError in section 'Account Information' at row 0, column '{0}': Activity "
    "Statement Account Information field '{0}' has an empty value."
)


@pytest.mark.parametrize(
    ("overrides", "dropped", "fallback", "column", "message"),
    [
        pytest.param(
            {},
            ("Account Type",),
            False,
            "Account Type",
            _MISSING.format("Account Type", "['Account Type']"),
            id="missing",
        ),
        pytest.param(
            {"Account Type": ""},
            (),
            False,
            "Account Type",
            _EMPTY.format("Account Type"),
            id="empty",
        ),
        pytest.param(
            {"Account Type": " \t"},
            (),
            False,
            "Account Type",
            _EMPTY.format("Account Type"),
            id="blank",
        ),
        pytest.param(
            {},
            ("Name", "Account", "Account Type"),
            True,
            "Account Type",
            _MISSING.format("Account Type", "['Account Type']"),
            id="fallback-does-not-supply-it",
        ),
        pytest.param(
            {"Account Type": ""},
            ("Name", "Account"),
            True,
            "Account Type",
            _EMPTY.format("Account Type"),
            id="fallback-does-not-fill-a-blank",
        ),
        pytest.param(
            {},
            ("Name", "Account", "Account Type"),
            False,
            "Name",
            _MISSING.format("Name", "['Name', 'Account', 'Account Type']"),
            id="hidden-identity-without-fallback-first",
        ),
        pytest.param(
            {"Name": ""},
            ("Account Type",),
            False,
            "Account Type",
            _MISSING.format("Account Type", "['Account Type']"),
            id="missing-before-blank-name",
        ),
        pytest.param(
            {"Account Type": "", "Base Currency": _CLEAN_DESC},
            (),
            False,
            "Account Type",
            _EMPTY.format("Account Type"),
            id="blank-before-currency-shape",
        ),
    ],
)
def test_account_type_refusals_keep_their_message_frame_and_order(
    overrides: dict[str, str],
    dropped: tuple[str, ...],
    fallback: bool,
    column: str,
    message: str,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The parser no longer keeps Account Type but still requires it: every
    missing field refuses before any blank one, Name, Account, Account Type
    and Base Currency in that order, before the Base Currency shape. The
    anonymised-statement fallback supplies only a hidden Name and Account.
    Each refusal is the complete message in the row-0 frame, with an empty
    ``raw_value``, no chain and no account, holder or other field text."""
    marked = {
        "Name": _CLEAN_NAME,
        "Account": _CLEAN_ACCOUNT,
        "Customer Type": _CLEAN_DESC,
    }
    rows = [
        row for row in make_full_rows(**(marked | overrides)) if row["Field Name"] not in dropped
    ]
    with pytest.raises(ParseError) as raised:
        parse_activity_statement_account_info(
            rows,
            fallback_account_holder_name=_CLEAN_NAME if fallback else None,
            fallback_account_id=_CLEAN_ACCOUNT if fallback else None,
        )

    error = raised.value
    assert type(error) is ParseError
    assert str(error) == message
    assert (error.section, error.row_index, error.column, error.raw_value) == (
        "Account Information",
        0,
        column,
        "",
    )
    assert (error.__cause__, error.__context__, error.__suppress_context__) == (None, None, False)
    rendered = render_without_frames(error) + repr(error)
    assert capsys.readouterr() == ("", "")
    input_values = [value for row in rows for value in row.values()]
    if fallback:
        input_values.extend((_CLEAN_NAME, _CLEAN_ACCOUNT))
    for marker in (marker for marker in _CLEAN_MARKERS if marker in input_values):
        assert marker not in rendered, marker
