"""Tests for the vendored CBDT ITR-2 JSON Schema + validation harness (#39).

PR1: the schema is vendored + pinned and the harness validates Schedule FA
A2/A3 items against it. No emitter yet — these guard the foundation so later
PRs can trust the validation gate.
"""

from __future__ import annotations

from typing import Any

import pytest

from ibkr_fa.output.cbdt_json_schema import (
    A2_DEFINITION,
    A3_DEFINITION,
    CBDT_SCHEMA_ASSESSMENT_YEAR_START,
    CBDT_SCHEMA_VERSION,
    CbdtItrForm,
    CbdtSchemaError,
    require_validator,
    schema_sha256,
    schema_version_for,
    validate_item,
)

# Pinned sha256 of the vendored schema (must match the .source sidecar). A
# change here means the schema file was re-vendored — every ScheduleFA
# element name / enum / maxLength / required-field must be re-verified.
_EXPECTED_SHA256 = "3a5d08a9dd6666c3b0c9e830844323aaa585a3daf7996377955a430c3c6fc044"
_EXPECTED_ITR3_SHA256 = "efb5cbdb2752d11d78ec1ff67469acfa2f15d2a4065dda3a37b4a4fb743bad2d"

# A synthetic value an error must never print (#1048).
_MARKER = "Synthetic Holder Two"
# 140 characters, over NameOfEntity's maxLength of 125.
_LONG_NAME = _MARKER * 7
_A2_ITEM_0 = (
    "ITR-2 Schedule FA Table A2 (DtlsForeignCustodialAcc) item at index 0 failed CBDT "
    "schema validation: "
)
_A3_ITEM = (
    "{itr_form} Schedule FA Table A3 (DtlsForeignEquityDebtInterest) item at index "
    "{row_index} failed CBDT schema validation: "
)


def _assert_not_echoed(message: str, *values: str) -> None:
    """None of ``values`` appears in ``message``, compared case-insensitively."""
    for value in values:
        assert value.lower() not in message.lower(), value


def _valid_a2() -> dict[str, Any]:
    return {
        "CountryName": "UNITED STATES OF AMERICA",
        "CountryCodeExcludingIndia": "2",
        "FinancialInstName": "Interactive Brokers LLC",
        "FinancialInstAddress": "One Pickwick Plaza, Greenwich, CT",
        "ZipCode": "06830",
        "AccountNumber": "U12345678",
        "Status": "BENEFICIAL_OWNER",
        "AccOpenDate": "2024-01-01",
        "PeakBalanceDuringPeriod": 12345,
        "ClosingBalance": 6127,
        "GrossAmtPaidCredited": 0,
        "NatureOfAmount": "N",
    }


def _valid_a3() -> dict[str, Any]:
    return {
        "CountryName": "UNITED STATES OF AMERICA",
        "CountryCodeExcludingIndia": "2",
        "NameOfEntity": "Example Holdings Inc.",
        "AddressOfEntity": "1 Example Plaza, Anytown, NY",
        "ZipCode": "99999",
        "NatureOfEntity": "Equity",
        "InterestAcquiringDate": "2024-02-01",
        "InitialValOfInvstmnt": 100000,
        "PeakBalanceDuringPeriod": 150000,
        "ClosingBalance": 120000,
        "TotGrossAmtPaidCredited": 500,
        "TotGrossProceeds": 0,
    }


# ---- vendoring / pinning ---------------------------------------------------


def test_schema_sha256_is_pinned() -> None:
    """Drift guard: the vendored schema must be byte-identical to what the
    harness + .source provenance pin (validation correctness depends on it)."""
    assert schema_sha256() == _EXPECTED_SHA256
    assert schema_sha256("ITR-3") == _EXPECTED_ITR3_SHA256


def test_schema_version_and_ay_constants() -> None:
    assert CBDT_SCHEMA_VERSION == "ITR-2 AY2026-27 Main V1.2 (2026-08-13)"
    assert CBDT_SCHEMA_ASSESSMENT_YEAR_START == 2026
    assert schema_version_for("ITR-3") == "ITR-3 AY2026-27 Main V1.1 Schedule FA"


# ---- golden valid items ----------------------------------------------------


@pytest.mark.parametrize("itr_form", ["ITR-2", "ITR-3"])
def test_valid_a2_item_passes(itr_form: CbdtItrForm) -> None:
    validate_item(_valid_a2(), A2_DEFINITION, itr_form, row_index=0)


@pytest.mark.parametrize("itr_form", ["ITR-2", "ITR-3"])
def test_valid_a3_item_passes(itr_form: CbdtItrForm) -> None:
    validate_item(_valid_a3(), A3_DEFINITION, itr_form, row_index=0)


def test_itr3_uses_its_own_official_numeric_constraints() -> None:
    item = {
        **_valid_a2(),
        "PeakBalanceDuringPeriod": -100_000_000_000_000,
        "ClosingBalance": -100_000_000_000_000,
    }
    with pytest.raises(CbdtSchemaError):
        validate_item(item, A2_DEFINITION, "ITR-2", row_index=0)
    validate_item(item, A2_DEFINITION, "ITR-3", row_index=0)


def test_negative_margin_balances_are_schema_valid() -> None:
    """The schema allows negative peak/closing (margin accounts) — ibkr-fa
    must not clamp them. Guards against a future 'floor at 0' regression."""
    validate_item(
        {**_valid_a2(), "PeakBalanceDuringPeriod": -500, "ClosingBalance": -500},
        A2_DEFINITION,
        row_index=0,
    )


# ---- refusal paths (the validation gate) -----------------------------------


@pytest.mark.parametrize(
    ("mutation", "field", "keyword", "value"),
    [
        # DD/MM/YYYY, not ISO
        ({"AccOpenDate": "01/01/2024"}, "AccOpenDate", "pattern", "01/01/2024"),
        ({"Status": "OWNER_TYPO"}, "Status", "enum", "OWNER_TYPO"),  # not in enum
        # not in I/D/S/O/N; one letter, too short to check for
        ({"NatureOfAmount": "X"}, "NatureOfAmount", "enum", None),
        ({"GrossAmtPaidCredited": -1}, "GrossAmtPaidCredited", "minimum", None),  # < 0
        ({"ZipCode": ""}, "ZipCode", "minLength", None),  # non-empty required
        ({"PeakBalanceDuringPeriod": 1.5}, "PeakBalanceDuringPeriod", "type", None),  # not int
        ({"AccountNumber": "U" * 40}, "AccountNumber", "maxLength", "U" * 40),  # > maxLength 34
    ],
    ids=["date", "status", "nature", "negative", "empty", "float", "too-long"],
)
def test_invalid_a2_item_rejected(
    mutation: dict[str, Any], field: str, keyword: str, value: str | None
) -> None:
    """The refusal names the field and the schema keyword, not the value (#1048)."""
    item = {**_valid_a2(), **mutation}
    with pytest.raises(CbdtSchemaError) as exc:
        validate_item(item, A2_DEFINITION, row_index=0)
    message = str(exc.value)
    assert message == _A2_ITEM_0 + f"field {field!r} breaks the schema's {keyword!r} rule"
    assert (exc.value.field, exc.value.keyword) == (field, keyword)
    if value is not None:
        _assert_not_echoed(message, value)


def test_missing_required_field_rejected() -> None:
    item = _valid_a2()
    del item["PeakBalanceDuringPeriod"]
    with pytest.raises(CbdtSchemaError) as exc:
        validate_item(item, A2_DEFINITION, row_index=0)
    assert str(exc.value) == (
        _A2_ITEM_0 + "field 'PeakBalanceDuringPeriod' breaks the schema's 'required' rule"
    )


def test_unknown_extra_field_rejected() -> None:
    """additionalProperties:false — a stray key must be rejected (catches a
    mapping typo before it becomes filed data). The key is not printed (#1048)."""
    with pytest.raises(CbdtSchemaError) as exc:
        validate_item({**_valid_a2(), "TotallyMadeUp": 1}, A2_DEFINITION, row_index=0)
    assert str(exc.value) == _A2_ITEM_0 + "the item breaks the schema's 'additionalProperties' rule"
    assert exc.value.field is None
    _assert_not_echoed(str(exc.value), "TotallyMadeUp")


# ---- what a refusal names (#1048) ------------------------------------------


@pytest.mark.parametrize("itr_form", ["ITR-2", "ITR-3"])
def test_malformed_second_a3_item_names_its_row_field_and_keyword(itr_form: CbdtItrForm) -> None:
    """Validated in table order, as the fragment loops do, a malformed second
    A3 item is named by its 0-based index, its field and the schema keyword,
    never by its value."""
    items = [_valid_a3(), {**_valid_a3(), "NameOfEntity": _LONG_NAME}]
    with pytest.raises(CbdtSchemaError) as exc:
        for row_index, item in enumerate(items):
            validate_item(item, A3_DEFINITION, itr_form, row_index=row_index)
    error = exc.value
    assert str(error) == (
        _A3_ITEM.format(itr_form=itr_form, row_index=1)
        + "field 'NameOfEntity' breaks the schema's 'maxLength' rule"
    )
    assert (error.itr_form, error.definition, error.row_index, error.field, error.keyword) == (
        itr_form,
        A3_DEFINITION,
        1,
        "NameOfEntity",
        "maxLength",
    )
    _assert_not_echoed(str(error), _MARKER)


def test_each_missing_required_field_is_named() -> None:
    """Two items each missing a different required field name the right one;
    the name comes from the schema's ``required`` list."""
    without_zip = _valid_a3()
    del without_zip["ZipCode"]
    without_nature = _valid_a3()
    del without_nature["NatureOfEntity"]
    for row_index, (item, field) in enumerate(
        ((without_zip, "ZipCode"), (without_nature, "NatureOfEntity"))
    ):
        with pytest.raises(CbdtSchemaError) as exc:
            validate_item(item, A3_DEFINITION, row_index=row_index)
        assert str(exc.value) == (
            _A3_ITEM.format(itr_form="ITR-2", row_index=row_index)
            + f"field {field!r} breaks the schema's 'required' rule"
        )
        assert (exc.value.row_index, exc.value.field, exc.value.keyword) == (
            row_index,
            field,
            "required",
        )


def test_item_missing_two_fields_names_the_first_in_schema_order() -> None:
    """The schema lists CountryName first; alphabetical order would pick
    AddressOfEntity, so this pins the schema's ``required`` order."""
    item = _valid_a3()
    del item["AddressOfEntity"], item["CountryName"]
    with pytest.raises(CbdtSchemaError) as exc:
        validate_item(item, A3_DEFINITION, row_index=0)
    assert str(exc.value) == (
        _A3_ITEM.format(itr_form="ITR-2", row_index=0)
        + "field 'CountryName' breaks the schema's 'required' rule"
    )


@pytest.mark.parametrize(
    ("definition", "item"),
    [(A2_DEFINITION, _valid_a2()), (A3_DEFINITION, _valid_a3())],
    ids=["A2", "A3"],
)
@pytest.mark.parametrize("itr_form", ["ITR-2", "ITR-3"])
def test_extra_key_is_not_printed(
    itr_form: CbdtItrForm, definition: str, item: dict[str, Any]
) -> None:
    with pytest.raises(CbdtSchemaError) as exc:
        validate_item({**item, _MARKER: 1}, definition, itr_form, row_index=0)
    table = "A2" if definition == A2_DEFINITION else "A3"
    assert str(exc.value) == (
        f"{itr_form} Schedule FA Table {table} ({definition}) item at index 0 failed CBDT "
        "schema validation: the item breaks the schema's 'additionalProperties' rule"
    )
    assert (exc.value.field, exc.value.keyword) == (None, "additionalProperties")
    _assert_not_echoed(str(exc.value), _MARKER)


@pytest.mark.parametrize(
    ("table", "mutation", "field", "keyword", "value"),
    [
        ("A3", {"ClosingBalance": "1234567"}, "ClosingBalance", "type", "1234567"),
        ("A2", {"ClosingBalance": "1234567"}, "ClosingBalance", "type", "1234567"),
        ("A2", {"Status": _MARKER}, "Status", "enum", _MARKER),
    ],
    ids=["a3-amount-as-text", "a2-amount-as-text", "a2-status"],
)
def test_wrong_type_or_enum_value_is_not_printed(
    table: str, mutation: dict[str, Any], field: str, keyword: str, value: str
) -> None:
    definition, item = (
        (A2_DEFINITION, _valid_a2()) if table == "A2" else (A3_DEFINITION, _valid_a3())
    )
    with pytest.raises(CbdtSchemaError) as exc:
        validate_item({**item, **mutation}, definition, row_index=0)
    assert str(exc.value) == (
        f"ITR-2 Schedule FA Table {table} ({definition}) item at index 0 failed CBDT "
        f"schema validation: field {field!r} breaks the schema's {keyword!r} rule"
    )
    assert exc.value.rule_text() == f"field {field!r} breaks the schema's {keyword!r} rule"
    _assert_not_echoed(str(exc.value), value)


def test_first_error_selection_is_unchanged() -> None:
    """An item with a root-level error (a missing field) and a field error (an
    over-long name) reports the root one, as the old sort on the error's path
    did: an empty path sorts first."""
    item = {**_valid_a3(), "NameOfEntity": _LONG_NAME}
    del item["ZipCode"]
    validator = require_validator(A3_DEFINITION)
    errors = sorted(validator.iter_errors(item), key=lambda e: list(e.absolute_path))
    # The item really breaks both rules, the root one sorting first.
    assert [(list(e.absolute_path), e.validator) for e in errors] == [
        ([], "required"),
        (["NameOfEntity"], "maxLength"),
    ]
    with pytest.raises(CbdtSchemaError) as exc:
        validate_item(item, A3_DEFINITION, row_index=0)
    assert str(exc.value) == (
        _A3_ITEM.format(itr_form="ITR-2", row_index=0)
        + "field 'ZipCode' breaks the schema's 'required' rule"
    )
    # Base's required-field error already omitted the name; this guards future excerpts.
    _assert_not_echoed(str(exc.value), _MARKER)
