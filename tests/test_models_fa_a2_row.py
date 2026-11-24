"""Tests for :class:`ibkr_fa.models.fa_a2_row.FaA2Row` (Schedule FA Table A2)."""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal, Inexact, localcontext
from typing import Any

import pytest

from ibkr_fa.exceptions import InvariantError
from ibkr_fa.models.account_identity import TaxpayerStatus
from ibkr_fa.models.fa_a2_row import (
    FA_A2_STATUS_BY_TAXPAYER_STATUS,
    FaA2ClosingBlankReason,
    FaA2NatureOfAmount,
    FaA2Row,
    FaA2Status,
)
from ibkr_fa.models.net_asset_value import NavRefusalReason
from tests._crash_reports import assert_no_markers


def _row(**overrides: object) -> FaA2Row:
    kwargs: dict[str, Any] = {
        "country_code": "2",
        "country_name": "UNITED STATES OF AMERICA",
        "institution_name": "Interactive Brokers LLC",
        "institution_address": "One Pickwick Plaza, Greenwich, CT",
        "institution_zip": "06830",
        "account_number": "U12345678",
        "status": FaA2Status.BENEFICIAL_OWNER,
        "account_opening_date": date(2024, 1, 1),
        "peak_balance_inr": None,
        "closing_balance_inr": Decimal("11069.08041662"),  # 123.718346 x 89.47
        "nature_of_amount": FaA2NatureOfAmount.NO_AMOUNT,
        "gross_amount_inr": Decimal("0"),
        "base_currency": "USD",
        "audit_closing_balance_base_ccy": Decimal("123.718346"),
        "audit_ttbr_dec31": Decimal("89.47"),
        "audit_account_number_from_config": True,
        "audit_nav_cross_checked": True,
        "audit_gross_components": {},
        "audit_period_end_cash_base_ccy": None,
    }
    kwargs.update(overrides)
    return FaA2Row(**kwargs)


def test_valid_row() -> None:
    row = _row()
    assert row.country_code == "2"
    assert row.peak_balance_inr is None
    assert row.status is FaA2Status.BENEFICIAL_OWNER


def test_beneficiary_token_preserves_official_typo() -> None:
    assert FaA2Status.BENEFICIARY.value == "BENIFICIARY"


def test_india_country_code_rejected() -> None:
    with pytest.raises(InvariantError):
        _row(country_code="91")


def test_non_numeric_country_code_rejected() -> None:
    with pytest.raises(InvariantError):
        _row(country_code="US")


def test_country_code_with_trailing_line_break_rejected() -> None:
    """``$`` also matches just before a final line break (#835)."""
    with pytest.raises(InvariantError) as exc:
        _row(country_code="2\n")
    assert exc.value.what == "FaA2Row.country_code must be a numeric CBDT ITR-2 code"
    assert exc.value.context == (("country_code", None),)


@pytest.mark.parametrize("account_number", ["U**********", "U***"])
def test_masked_account_number_rejected(account_number: str) -> None:
    with pytest.raises(InvariantError) as exc:
        _row(account_number=account_number)
    assert exc.value.what == (
        "FaA2Row.account_number must be the real account number, never a "
        "masked literal (Table A2's account number is a filed value)"
    )


@pytest.mark.parametrize("account_number", ["U1234567\n", "U***\n", "Ü1234567\n"])
def test_account_number_with_a_control_character_rejected(account_number: str) -> None:
    """#854: debugging context keeps the keys of the character and its position, never the value."""
    with pytest.raises(InvariantError) as exc:
        _row(account_number=account_number)
    assert exc.value.what == "FaA2Row.account_number must not contain a control character"
    assert exc.value.context == (("character", None), ("position", None))


def test_empty_institution_name_rejected() -> None:
    with pytest.raises(InvariantError):
        _row(institution_name="")


@pytest.mark.parametrize(
    "field", ["country_name", "institution_name", "institution_address", "base_currency"]
)
def test_required_text_must_be_non_empty(field: str) -> None:
    with pytest.raises(InvariantError) as exc:
        _row(**{field: ""})
    assert type(exc.value) is InvariantError
    assert str(exc.value) == f"InvariantError: FaA2Row.{field} must be non-empty"
    assert exc.value.context == ()


def test_peak_requires_audited_explicit_bound() -> None:
    row = _row(
        peak_balance_inr=Decimal("100"),
        audit_peak_balance_lower_inr=Decimal("100"),
        audit_peak_balance_upper_inr=Decimal("120"),
        audit_peak_balance_lower_date=date(2024, 5, 1),
        audit_peak_balance_upper_date=date(2024, 6, 1),
        audit_peak_balance_bound="lower",
    )
    assert row.peak_balance_inr == Decimal("100")
    with pytest.raises(InvariantError):
        _row(peak_balance_inr=Decimal("100"))
    with pytest.raises(InvariantError):
        _row(
            peak_balance_inr=Decimal("100"),
            audit_peak_balance_lower_inr=Decimal("90"),
            audit_peak_balance_upper_inr=Decimal("120"),
            audit_peak_balance_lower_date=date(2024, 5, 1),
            audit_peak_balance_upper_date=date(2024, 6, 1),
            audit_peak_balance_bound="lower",
        )


def test_native_peak_bounds_need_not_remain_ordered_after_conversion() -> None:
    row = _row(
        peak_balance_inr=Decimal("100"),
        audit_peak_balance_lower_inr=Decimal("120"),
        audit_peak_balance_upper_inr=Decimal("100"),
        audit_peak_balance_lower_date=date(2024, 5, 1),
        audit_peak_balance_upper_date=date(2024, 6, 1),
        audit_peak_balance_bound="upper",
    )
    assert row.peak_balance_inr == Decimal("100")


def test_peak_accepts_explicit_override_provenance() -> None:
    row = _row(
        peak_balance_inr=Decimal("100"),
        audit_peak_override_source="reviewed statement workbook",
        audit_peak_override_reason="daily peak supplied by reviewer",
    )
    assert row.peak_balance_inr == Decimal("100")
    with pytest.raises(InvariantError):
        _row(
            peak_balance_inr=Decimal("100"),
            audit_peak_override_source="reviewed statement workbook",
        )
    with pytest.raises(InvariantError):
        _row(
            peak_balance_inr=Decimal("100"),
            audit_peak_override_source=" ",
            audit_peak_override_reason="daily peak supplied by reviewer",
        )
    with pytest.raises(InvariantError):
        _row(
            peak_balance_inr=Decimal("100"),
            audit_peak_balance_lower_inr=Decimal("100"),
            audit_peak_balance_upper_inr=Decimal("120"),
            audit_peak_balance_lower_date=date(2024, 5, 1),
            audit_peak_balance_upper_date=date(2024, 6, 1),
            audit_peak_balance_bound="lower",
            audit_peak_override_source="reviewed statement workbook",
            audit_peak_override_reason="daily peak supplied by reviewer",
        )


def test_negative_closing_balance_allowed() -> None:
    """A margined account can carry a negative NAV — closing is not clamped."""
    row = _row(closing_balance_inr=Decimal("-500.00"))
    assert row.closing_balance_inr == Decimal("-500.00")


@pytest.mark.parametrize("reason", list(FaA2ClosingBlankReason))
def test_closing_balance_may_be_none_manual_fill(reason: FaA2ClosingBlankReason) -> None:
    row = _row(
        closing_balance_inr=None,
        audit_closing_balance_base_ccy=None,
        audit_ttbr_dec31=None,
        closing_blank_reason=reason,
    )
    assert row.closing_balance_inr is None
    assert row.closing_blank_reason is reason


def test_blank_closing_balance_requires_its_reason() -> None:
    """#1013: the reason is set exactly when the closing balance is blank."""
    match = "closing_blank_reason must be set exactly when closing_balance_inr is blank"
    with pytest.raises(InvariantError, match=match):
        _row(closing_balance_inr=None, audit_closing_balance_base_ccy=None, audit_ttbr_dec31=None)
    with pytest.raises(InvariantError, match=match):
        _row(closing_blank_reason=FaA2ClosingBlankReason.NON_USD_BASE)


@pytest.mark.parametrize("reason", ["non-usd-base", "statement NAV unclear"])
def test_closing_blank_reason_must_be_an_enum_member(reason: str) -> None:
    with pytest.raises(
        InvariantError, match="must be None or a FaA2ClosingBlankReason enum member"
    ):
        _row(
            closing_balance_inr=None,
            audit_closing_balance_base_ccy=None,
            audit_ttbr_dec31=None,
            closing_blank_reason=reason,
        )


def test_every_nav_refusal_reason_is_a_closing_blank_reason() -> None:
    """#1013: the builder copies a NAV refusal to the row by value."""
    assert {reason.value for reason in FaA2ClosingBlankReason} == {
        reason.value for reason in NavRefusalReason
    } | {FaA2ClosingBlankReason.NON_USD_BASE.value}


@pytest.mark.parametrize(
    "nature",
    [
        FaA2NatureOfAmount.DIVIDEND,
        FaA2NatureOfAmount.INTEREST,
        FaA2NatureOfAmount.PROCEEDS,
        FaA2NatureOfAmount.OTHER_INCOME,
    ],
)
def test_dec31_ttbr_may_audit_gross_credit_when_closing_is_manual_fill(
    nature: FaA2NatureOfAmount,
) -> None:
    row = _row(
        closing_balance_inr=None,
        audit_closing_balance_base_ccy=None,
        nature_of_amount=nature,
        gross_amount_inr=Decimal("89.47"),
        audit_gross_components={nature.value: Decimal("89.47")},
        closing_blank_reason=FaA2ClosingBlankReason.NO_ASSET_CLASS_BLOCK,
    )
    assert row.audit_ttbr_dec31 == Decimal("89.47")


def test_closing_audit_coupling_enforced() -> None:
    """A filed closing must carry its base-currency provenance and Dec-31 TTBR."""
    with pytest.raises(InvariantError, match="audit_closing_balance_base_ccy"):
        _row(
            closing_balance_inr=None,
            audit_gross_components={},
            closing_blank_reason=FaA2ClosingBlankReason.NO_ASSET_CLASS_BLOCK,
        )
    with pytest.raises(InvariantError):
        _row(audit_closing_balance_base_ccy=None, audit_ttbr_dec31=None)


def test_non_positive_audit_ttbr_rejected() -> None:
    with pytest.raises(InvariantError):
        _row(audit_ttbr_dec31=Decimal("0"))


def test_nature_and_gross_must_be_coupled() -> None:
    with pytest.raises(InvariantError):
        _row(nature_of_amount=None, gross_amount_inr=Decimal("0"))
    with pytest.raises(InvariantError):
        _row(nature_of_amount=FaA2NatureOfAmount.DIVIDEND, gross_amount_inr=None)


def test_both_nature_and_gross_none_is_manual_fill() -> None:
    row = _row(nature_of_amount=None, gross_amount_inr=None)
    assert row.nature_of_amount is None
    assert row.gross_amount_inr is None


def test_negative_gross_amount_rejected() -> None:
    with pytest.raises(InvariantError):
        _row(nature_of_amount=FaA2NatureOfAmount.DIVIDEND, gross_amount_inr=Decimal("-1"))


# ---- A no-credit row carries exactly zero (#1188) ----------------------------

# Distinct synthetic markers; no refusal below may print or chain them.
_MARKED = {
    "institution_name": "SYNTHROWNAME",
    "institution_address": "SYNTHROWADDRESS",
    "account_number": "SYNTHROWACCOUNT",
    "peak_balance_inr": Decimal("1"),
    "audit_peak_override_source": "SYNTHROWSOURCE",
    "audit_peak_override_reason": "synthetic attested peak",
}
_MARKERS = ("SYNTHROWNAME", "SYNTHROWADDRESS", "SYNTHROWACCOUNT", "SYNTHROWSOURCE")


@pytest.mark.parametrize("amount", ["100", "0.01"])
def test_no_amount_row_with_an_amount_rejected(
    amount: str, capfd: pytest.CaptureFixture[str]
) -> None:
    """A no-credit row must refuse a nonzero amount before rounding."""
    with pytest.raises(InvariantError) as exc:
        _row(
            nature_of_amount=FaA2NatureOfAmount.NO_AMOUNT,
            gross_amount_inr=Decimal(amount),
            **_MARKED,
        )
    assert type(exc.value) is InvariantError
    assert str(exc.value) == (
        "InvariantError: FaA2Row.gross_amount_inr must be zero when nature_of_amount is NO_AMOUNT"
    )
    assert exc.value.context == ()
    assert exc.value.__cause__ is None
    assert exc.value.__context__ is None
    captured = capfd.readouterr()
    for text in (str(exc.value), repr(exc.value), captured.out, captured.err):
        assert_no_markers(text, *_MARKERS)


@pytest.mark.parametrize("zero", ["0", "-0", "0.00", "-0.00", "0E+3", "-0E-7"])
def test_no_amount_row_accepts_every_zero_spelling(zero: str) -> None:
    row = _row(nature_of_amount=FaA2NatureOfAmount.NO_AMOUNT, gross_amount_inr=Decimal(zero))
    assert row.gross_amount_inr is not None
    assert row.gross_amount_inr.compare_total(Decimal(zero)) == 0
    assert row.filed.gross_amount_inr is not None
    assert row.filed.gross_amount_inr.compare_total(Decimal("0")) == 0


@pytest.mark.parametrize(
    "nature",
    [
        FaA2NatureOfAmount.INTEREST,
        FaA2NatureOfAmount.DIVIDEND,
        FaA2NatureOfAmount.PROCEEDS,
        FaA2NatureOfAmount.OTHER_INCOME,
    ],
)
@pytest.mark.parametrize("amount", ["0", "100"])
def test_other_natures_accept_zero_and_non_zero_amounts(
    nature: FaA2NatureOfAmount, amount: str
) -> None:
    row = _row(nature_of_amount=nature, gross_amount_inr=Decimal(amount))
    assert (row.nature_of_amount, row.gross_amount_inr) == (nature, Decimal(amount))


@pytest.mark.parametrize(
    ("amount", "what"),
    [
        (None, "FaA2Row.nature_of_amount and gross_amount_inr must both be set or both be None"),
        (Decimal("-1"), "FaA2Row.gross_amount_inr must be non-negative when set"),
        (Decimal("-0.01"), "FaA2Row.gross_amount_inr must be non-negative when set"),
        (Decimal("NaN"), "FaA2Row.gross_amount_inr must be a finite Decimal or None"),
        (Decimal("-Infinity"), "FaA2Row.gross_amount_inr must be a finite Decimal or None"),
        (0, "FaA2Row.gross_amount_inr must be a finite Decimal or None"),
        ("0", "FaA2Row.gross_amount_inr must be a finite Decimal or None"),
    ],
    ids=["blank", "negative", "negative-cent", "nan", "infinite", "int-zero", "str-zero"],
)
def test_no_amount_row_keeps_its_earlier_refusals(amount: object, what: str) -> None:
    with pytest.raises(InvariantError) as exc:
        _row(nature_of_amount=FaA2NatureOfAmount.NO_AMOUNT, gross_amount_inr=amount)
    assert exc.value.what == what


# ---- Raw text within the CBDT schemas' maximum lengths (#1189) ---------------

# Each limited field, its limit, a distinct synthetic marker and its refusal.
_TEXT_LIMITS = [
    pytest.param(
        "country_name",
        55,
        "SYNTHLENCOUNTRY",
        "InvariantError: FaA2Row.country_name must be at most 55 characters",
        id="country_name",
    ),
    pytest.param(
        "institution_name",
        125,
        "SYNTHLENNAME",
        "InvariantError: FaA2Row.institution_name must be at most 125 characters",
        id="institution_name",
    ),
    pytest.param(
        "institution_address",
        200,
        "SYNTHLENADDRESS",
        "InvariantError: FaA2Row.institution_address must be at most 200 characters",
        id="institution_address",
    ),
    pytest.param(
        "institution_zip",
        8,
        "SYNTHZIP",
        "InvariantError: FaA2Row.institution_zip must be at most 8 characters",
        id="institution_zip",
    ),
    pytest.param(
        "account_number",
        34,
        "SYNTHLENACCOUNT",
        "InvariantError: FaA2Row.account_number must be at most 34 characters",
        id="account_number",
    ),
]


@pytest.mark.parametrize(("field", "limit", "marker", "message"), _TEXT_LIMITS)
def test_text_at_its_maximum_length_accepted(
    field: str, limit: int, marker: str, message: str
) -> None:
    value = marker.ljust(limit, "X")
    assert getattr(_row(**{field: value}), field) == value


@pytest.mark.parametrize(("field", "limit", "marker", "message"), _TEXT_LIMITS)
def test_text_over_its_maximum_length_refused(
    field: str, limit: int, marker: str, message: str, capfd: pytest.CaptureFixture[str]
) -> None:
    with pytest.raises(InvariantError) as exc:
        _row(**{field: marker.ljust(limit + 1, "X")})
    assert type(exc.value) is InvariantError
    assert str(exc.value) == message
    assert exc.value.context == ()
    assert exc.value.__cause__ is None
    assert exc.value.__context__ is None
    captured = capfd.readouterr()
    for text in (str(exc.value), repr(exc.value), captured.out, captured.err):
        assert_no_markers(text, marker)


def test_zip_plus_four_refused() -> None:
    """A US ZIP+4 with its dash is 10 characters."""
    with pytest.raises(InvariantError) as exc:
        _row(institution_zip="12345-6789")
    assert str(exc.value) == "InvariantError: FaA2Row.institution_zip must be at most 8 characters"


def test_blank_zip_accepted() -> None:
    assert _row(institution_zip="").institution_zip == ""


# ---- Text the upload file would turn blank (#1329) --------------------------

# Each text field the bulk-upload file renders, with its #1189 length limit.
_UPLOAD_TEXT = [
    pytest.param("country_name", 55, id="country_name"),
    pytest.param("institution_name", 125, id="institution_name"),
    pytest.param("institution_address", 200, id="institution_address"),
    pytest.param("institution_zip", 8, id="institution_zip"),
    pytest.param("account_number", 34, id="account_number"),
]
# No control character: account_number refuses one first.
_COMMA_ONLY = [",", ", ,", " ,", ",  ,"]


@pytest.mark.parametrize("value", _COMMA_ONLY)
@pytest.mark.parametrize(("field", "limit"), _UPLOAD_TEXT)
def test_comma_only_text_refused(
    field: str, limit: int, value: str, capfd: pytest.CaptureFixture[str]
) -> None:
    """The upload file would write a blank cell for it, so the row refuses it,
    naming only the field."""
    marked = {name: v for name, v in _MARKED.items() if name != field}
    markers = tuple(v for v in marked.values() if isinstance(v, str))
    with pytest.raises(InvariantError) as exc:
        _row(**{field: value, **marked})
    assert type(exc.value) is InvariantError
    assert str(exc.value) == (
        f"InvariantError: FaA2Row.{field} must contain a character other than a comma or a space"
    )
    assert exc.value.context == ()
    assert exc.value.args == ()
    assert exc.value.__cause__ is None
    assert exc.value.__context__ is None
    captured = capfd.readouterr()
    for text in (str(exc.value), repr(exc.value), captured.out, captured.err):
        assert_no_markers(text, *markers)


@pytest.mark.parametrize("value", ["a,", ", a ,", "QZCOMMA,"])
@pytest.mark.parametrize(("field", "limit"), _UPLOAD_TEXT)
def test_text_with_another_character_accepted(field: str, limit: int, value: str) -> None:
    assert getattr(_row(**{field: value}), field) == value


@pytest.mark.parametrize(("field", "limit"), _UPLOAD_TEXT)
def test_whitespace_only_text_keeps_its_handling(field: str, limit: int) -> None:
    """The check leaves an empty or whitespace-only value to the row's older rules."""
    assert getattr(_row(**{field: "  "}), field) == "  "


@pytest.mark.parametrize(("field", "limit"), _UPLOAD_TEXT)
def test_comma_only_text_over_its_limit_keeps_the_length_refusal(field: str, limit: int) -> None:
    """#1189's length refusal fires first, with its own message."""
    with pytest.raises(InvariantError) as exc:
        _row(**{field: "," * (limit + 1)})
    assert type(exc.value) is InvariantError
    assert exc.value.what == f"FaA2Row.{field} must be at most {limit} characters"


def test_comma_only_text_keeps_every_earlier_refusal() -> None:
    """The check runs last: a row that already refused keeps its message."""
    with pytest.raises(InvariantError) as exc:
        _row(institution_address=",", gross_amount_inr=Decimal("1"))
    assert exc.value.what == (
        "FaA2Row.gross_amount_inr must be zero when nature_of_amount is NO_AMOUNT"
    )


def test_status_map_has_no_signing_authority_and_is_read_only() -> None:
    assert dict(FA_A2_STATUS_BY_TAXPAYER_STATUS) == {
        TaxpayerStatus.BENEFICIAL_OWNER: FaA2Status.BENEFICIAL_OWNER,
        TaxpayerStatus.JOINT: FaA2Status.OWNER,
    }
    with pytest.raises(TypeError):
        FA_A2_STATUS_BY_TAXPAYER_STATUS[TaxpayerStatus.SIGNING_AUTHORITY] = (  # ty: ignore[invalid-assignment]
            FaA2Status.OWNER
        )


# ---- field types and the filed-amount bound (#788) --------------------------


def _filing(field: str, amount: Decimal) -> dict[str, object]:
    """Overrides filing ``amount`` in ``field`` on an otherwise valid row."""
    if field == "peak_balance_inr":
        return {
            "peak_balance_inr": amount,
            "audit_peak_override_source": "reviewed statement workbook",
            "audit_peak_override_reason": "daily peak supplied by reviewer",
        }
    if field == "gross_amount_inr":
        return {"nature_of_amount": FaA2NatureOfAmount.DIVIDEND, "gross_amount_inr": amount}
    return {field: amount}


# Every filed amount, and the sign it may take: peak and closing may be negative.
_FILED_AMOUNTS = [
    ("peak_balance_inr", ""),
    ("peak_balance_inr", "-"),
    ("closing_balance_inr", ""),
    ("closing_balance_inr", "-"),
    ("gross_amount_inr", ""),
]


@pytest.mark.parametrize(("field", "sign"), _FILED_AMOUNTS)
@pytest.mark.parametrize("value", ["99999999999999", "99999999999999.4"])
def test_filed_amount_rounding_within_the_cbdt_bound_accepted(
    field: str, sign: str, value: str
) -> None:
    amount = Decimal(sign + value)
    assert getattr(_row(**_filing(field, amount)), field) == amount


@pytest.mark.parametrize(("field", "sign"), _FILED_AMOUNTS)
@pytest.mark.parametrize("value", ["99999999999999.5", "100000000000000", "1E100"])
def test_filed_amount_rounding_beyond_the_cbdt_bound_rejected(
    field: str, sign: str, value: str
) -> None:
    with pytest.raises(InvariantError) as exc:
        _row(**_filing(field, Decimal(sign + value)))
    assert exc.value.what.startswith(f"FaA2Row.{field} must round to a whole-rupee amount")


def test_filed_amount_bound_is_independent_of_decimal_context() -> None:
    with localcontext() as context:
        context.prec = 14
        context.traps[Inexact] = True
        amount = Decimal("-99999999999999.4")
        assert _row(closing_balance_inr=amount).closing_balance_inr == amount
        with pytest.raises(InvariantError, match=r"FaA2Row\.closing_balance_inr must round"):
            _row(closing_balance_inr=Decimal("-99999999999999.5"))


@pytest.mark.parametrize(("field", "sign"), _FILED_AMOUNTS)
@pytest.mark.parametrize(
    ("value", "filed"),
    [("64217.50", "64218"), ("64217.49", "64217"), ("0.00", "0"), ("5E+2", "500")],
)
def test_filed_value_is_the_field_rounded_half_up_to_whole_rupees(
    field: str, sign: str, value: str, filed: str
) -> None:
    """``filed`` rounds HALF_UP to whole rupees (a tie away from zero) and
    keeps the field's full precision on the row."""
    amount = Decimal(sign + value)
    row = _row(**_filing(field, amount))
    assert getattr(row, field).compare_total(amount) == 0
    expected = Decimal(filed) if filed == "0" else Decimal(sign + filed)
    assert getattr(row.filed, field).compare_total(expected) == 0


@pytest.mark.parametrize(
    ("field", "value", "filed"),
    [
        ("peak_balance_inr", "-500.5", "-501"),
        ("closing_balance_inr", "-500.5", "-501"),
        ("peak_balance_inr", "-0.4", "0"),
        ("closing_balance_inr", "-0.4", "0"),
        ("peak_balance_inr", "-0.00", "0"),
        ("closing_balance_inr", "-0.00", "0"),
        ("gross_amount_inr", "-0.00", "0"),
    ],
)
def test_filed_value_of_a_negative_amount_is_never_minus_zero(
    field: str, value: str, filed: str
) -> None:
    """A negative peak or closing balance rounds away from zero on a tie, and
    an amount that rounds to zero files as ``0``, never ``-0``."""
    row = _row(**_filing(field, Decimal(value)))
    assert getattr(row.filed, field).compare_total(Decimal(filed)) == 0


def test_filed_values_of_blank_fields_are_none() -> None:
    row = _row(
        closing_balance_inr=None,
        closing_blank_reason=FaA2ClosingBlankReason.NO_ENDING_VALUE,
        audit_closing_balance_base_ccy=None,
        audit_ttbr_dec31=None,
        nature_of_amount=None,
        gross_amount_inr=None,
    )
    assert row.filed.peak_balance_inr is None
    assert row.filed.closing_balance_inr is None
    assert row.filed.gross_amount_inr is None


def test_filed_values_are_frozen() -> None:
    filed = _row().filed
    with pytest.raises(AttributeError):
        filed.closing_balance_inr = Decimal("1")  # ty: ignore[invalid-assignment]


def test_audit_amount_beyond_the_cbdt_bound_accepted() -> None:
    row = _row(audit_closing_balance_base_ccy=Decimal("1E100"), audit_ttbr_dec31=Decimal("1E100"))
    assert row.audit_closing_balance_base_ccy == Decimal("1E100")


@pytest.mark.parametrize(
    ("overrides", "field"),
    [
        ({"account_number": 12345678}, "account_number"),
        ({"institution_zip": 6830}, "institution_zip"),
        ({"closing_balance_inr": 11069}, "closing_balance_inr"),
        ({"closing_balance_inr": Decimal("NaN")}, "closing_balance_inr"),
        ({"closing_balance_inr": Decimal("sNaN")}, "closing_balance_inr"),
        ({"closing_balance_inr": Decimal("Infinity")}, "closing_balance_inr"),
        ({"audit_ttbr_dec31": 89}, "audit_ttbr_dec31"),
        ({"audit_ttbr_dec31": Decimal("NaN")}, "audit_ttbr_dec31"),
        ({"account_opening_date": "2024-01-01"}, "account_opening_date"),
        ({"account_opening_date": datetime(2024, 1, 1)}, "account_opening_date"),
        ({"account_opening_date": datetime(2024, 1, 1, 12, 34, 56)}, "account_opening_date"),
        ({"account_opening_date": datetime(2024, 1, 1, tzinfo=UTC)}, "account_opening_date"),
        ({"status": "BENEFICIAL_OWNER"}, "status"),
        ({"nature_of_amount": "No Amount paid/credited"}, "nature_of_amount"),
        ({"audit_gross_components": None}, "audit_gross_components"),
        ({"audit_gross_components": {"Dividend": 5}}, "audit_gross_components"),
        ({"audit_account_number_from_config": "false"}, "audit_account_number_from_config"),
        ({"audit_nav_cross_checked": "false"}, "audit_nav_cross_checked"),
    ],
    ids=[
        "int-account-number",
        "int-zip",
        "int-filed-amount",
        "nan-filed-amount",
        "snan-filed-amount",
        "infinite-filed-amount",
        "int-audit-amount",
        "nan-audit-amount",
        "str-date",
        "midnight-datetime",
        "datetime",
        "aware-datetime",
        "str-status",
        "str-nature",
        "no-gross-components",
        "int-gross-component",
        "str-from-config-flag",
        "str-nav-cross-checked-flag",
    ],
)
def test_wrong_field_type_rejected_naming_the_field(
    overrides: dict[str, object], field: str
) -> None:
    with pytest.raises(InvariantError) as exc:
        _row(**overrides)
    assert exc.value.what.startswith(f"FaA2Row.{field} must be")
