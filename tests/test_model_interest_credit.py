"""Tests for :class:`ibkr_fa.models.interest_credit.InterestCredit` and
:class:`~ibkr_fa.models.interest_credit.InterestNonCredit`."""

from __future__ import annotations

import re
from datetime import date
from decimal import Decimal

import pytest

from ibkr_fa.exceptions import InvariantError
from ibkr_fa.models import InterestCredit, InterestNonCredit, InterestRowGroup


def _make(**overrides: object) -> InterestCredit:
    defaults: dict[str, object] = {
        "credit_date": date(2025, 4, 30),
        "amount_gross": Decimal("1.23"),
        "currency": "USD",
        "description": "USD Credit Interest for Apr-2025",
    }
    defaults.update(overrides)
    return InterestCredit(**defaults)  # ty: ignore[invalid-argument-type]


def test_happy_path_constructs() -> None:
    """Happy path: a well-formed InterestCredit round-trips every
    field unchanged, with source_row_index defaulting to None for
    callers that construct outside the parser path (test fixtures)."""
    credit = _make()
    assert credit.credit_date == date(2025, 4, 30)
    assert credit.amount_gross == Decimal("1.23")
    assert credit.currency == "USD"
    assert credit.description == "USD Credit Interest for Apr-2025"
    assert credit.source_row_index is None


def test_source_row_index_round_trip() -> None:
    """A non-None source_row_index is preserved verbatim — the
    parser path populates it so audit-trail surfaces (future error
    messages, review CSVs) can point back to the source row."""
    credit = _make(source_row_index=7)
    assert credit.source_row_index == 7


def test_dataclass_is_frozen() -> None:
    """Mirror the immutability pattern of Dividend / WithholdingTax."""
    credit = _make()
    with pytest.raises(AttributeError):
        credit.amount_gross = Decimal("99")  # ty: ignore[invalid-assignment]


def test_zero_amount_rejected() -> None:
    """Zero credit isn't real interest — would corrupt downstream
    Schedule OS interest income / FSI totals if silently allowed."""
    with pytest.raises(InvariantError) as exc:
        _make(amount_gross=Decimal("0"))
    assert "positive" in exc.value.what.lower()


def test_negative_amount_rejected() -> None:
    """IBKR emits credit interest as positive amounts. Debit
    interest (margin interest the user PAID) is out of v1 scope."""
    with pytest.raises(InvariantError) as exc:
        _make(amount_gross=Decimal("-1.23"))
    assert "positive" in exc.value.what.lower()


@pytest.mark.parametrize(
    "bad_amount",
    [
        Decimal("NaN"),
        Decimal("Infinity"),
        Decimal("-Infinity"),
    ],
)
def test_non_finite_amount_rejected(bad_amount: Decimal) -> None:
    """NaN/Infinity would propagate into per-credit INR conversion
    and corrupt totals — rejected at construction."""
    with pytest.raises(InvariantError):
        _make(amount_gross=bad_amount)


def test_malformed_currency_rejected_lowercase() -> None:
    """ISO 4217 codes are uppercase by convention; lowercase rejects
    so a future case-folding bug in the parser can't silently slip
    through to downstream FX/INR conversion."""
    with pytest.raises(InvariantError):
        _make(currency="usd")


def test_malformed_currency_rejected_wrong_length() -> None:
    """ISO 4217 codes are exactly 3 characters; rejecting other
    lengths catches a misparsed Currency column before it corrupts
    downstream FX lookups."""
    with pytest.raises(InvariantError):
        _make(currency="US")


def test_malformed_currency_rejected_non_alpha() -> None:
    """Currency must be alphabetic; a digit in the field signals
    a misparsed CSV (e.g., reading the Amount column as Currency)."""
    with pytest.raises(InvariantError):
        _make(currency="US1")


def test_malformed_currency_rejected_non_ascii_capital() -> None:
    """#854: ``isupper`` and ``isalpha`` accept ``"Ü"``; ISO 4217 is ASCII."""
    with pytest.raises(InvariantError) as exc:
        _make(currency="ÜSD")
    assert exc.value.what == "InterestCredit.currency must be a 3-letter uppercase ISO 4217 code"
    assert exc.value.context == (("currency", None),)


def test_empty_description_rejected() -> None:
    """Description is the audit-trail link to the originating
    Interest row in the IBKR Activity Statement."""
    with pytest.raises(InvariantError):
        _make(description="")


def test_negative_source_row_index_rejected() -> None:
    """Defensive: source_row_index must be a valid 0-based index
    when present (matches Dividend.source_row_index invariant)."""
    with pytest.raises(InvariantError):
        _make(source_row_index=-1)


def test_source_row_index_zero_accepted() -> None:
    """0-based indexing: row 0 is the first data row, valid."""
    credit = _make(source_row_index=0)
    assert credit.source_row_index == 0


def _non_credit(**overrides: object) -> InterestNonCredit:
    defaults: dict[str, object] = {
        "group": InterestRowGroup.UNPROVEN_NON_CREDIT,
        "amount": Decimal("-4.00"),
        "currency": "USD",
        "row_date": date(2025, 3, 31),
        "description": "USD Credit Interest for Mar-2025",
        "file_line": 42,
    }
    defaults.update(overrides)
    return InterestNonCredit(**defaults)  # ty: ignore[invalid-argument-type]


def test_only_debit_interest_is_proven() -> None:
    """Debit interest is the one non-credit group nothing refuses (#905)."""
    assert [group for group in InterestRowGroup if not group.unproven] == [
        InterestRowGroup.DEBIT_INTEREST
    ]


@pytest.mark.parametrize(
    ("group", "amount"),
    [
        (InterestRowGroup.DEBIT_INTEREST, "-0.01"),
        (InterestRowGroup.UNPROVEN_POSITIVE, "0.01"),
        (InterestRowGroup.UNPROVEN_NON_CREDIT, "0"),
        (InterestRowGroup.UNPROVEN_NON_CREDIT, "-4.00"),
        (InterestRowGroup.UNPROVEN_ACCRUAL, "1.23"),
        (InterestRowGroup.UNPROVEN_ACCRUAL, "0"),
        (InterestRowGroup.UNPROVEN_ACCRUAL, "-1.23"),
    ],
)
def test_non_credit_accepts_its_groups_signs(group: InterestRowGroup, amount: str) -> None:
    """Each group admits exactly the signs the parser puts in it."""
    assert _non_credit(group=group, amount=Decimal(amount)).amount == Decimal(amount)


@pytest.mark.parametrize(
    ("group", "amount"),
    [
        (InterestRowGroup.DEBIT_INTEREST, "0"),
        (InterestRowGroup.DEBIT_INTEREST, "1.23"),
        (InterestRowGroup.UNPROVEN_POSITIVE, "0"),
        (InterestRowGroup.UNPROVEN_POSITIVE, "-0.01"),
        (InterestRowGroup.UNPROVEN_NON_CREDIT, "0.01"),
    ],
)
def test_non_credit_refuses_a_sign_its_group_cannot_have(
    group: InterestRowGroup, amount: str
) -> None:
    """A positive non-accrual row is a credit or unproven-positive, and zero
    isn't debit interest, so these combinations mean a caller
    misclassified."""
    with pytest.raises(InvariantError, match="sign"):
        _non_credit(group=group, amount=Decimal(amount))


@pytest.mark.parametrize("bad_amount", [Decimal("NaN"), Decimal("-Infinity")])
def test_non_credit_refuses_a_non_finite_amount(bad_amount: Decimal) -> None:
    with pytest.raises(InvariantError, match="finite"):
        _non_credit(amount=bad_amount)


def test_non_credit_refuses_an_empty_description() -> None:
    with pytest.raises(InvariantError, match="description"):
        _non_credit(description="")


@pytest.mark.parametrize("bad_line", [0, -1])
def test_non_credit_refuses_a_line_before_the_first(bad_line: int) -> None:
    with pytest.raises(InvariantError, match="file_line"):
        _non_credit(file_line=bad_line)


@pytest.mark.parametrize("currency", ["USD", "EUR", "ANN"])
@pytest.mark.parametrize(
    ("group", "description", "amount", "rendered"),
    [
        pytest.param(
            InterestRowGroup.DEBIT_INTEREST,
            "Synthetic Holder Two U1234567 Debit Interest",
            "-1234567.89",
            "line 42 (debit interest)",
            id="debit-interest",
        ),
        pytest.param(
            InterestRowGroup.UNPROVEN_ACCRUAL,
            "Synthetic Holder Two U1234567 accrual adjustment",
            "1234567.89",
            "line 42 (its description mentions an accrual)",
            id="accrual",
        ),
        pytest.param(
            InterestRowGroup.UNPROVEN_POSITIVE,
            "Synthetic Holder Two U1234567 Credit Interest",
            "1234567.89",
            "line 42 (a positive amount without IBKR's credit-interest wording)",
            id="positive",
        ),
        pytest.param(
            InterestRowGroup.UNPROVEN_NON_CREDIT,
            "Synthetic Holder Two U1234567 correction",
            "-1234567.89",
            "line 42 (a negative or zero amount that isn't debit interest)",
            id="non-credit",
        ),
    ],
)
def test_non_credit_describes_only_its_line_and_reason(
    group: InterestRowGroup, description: str, amount: str, rendered: str, currency: str
) -> None:
    """The refusals and the schedule-fa report name rows by their file line
    and group only. The Description, amount, date and currency are input: a
    shifted row could put the holder's name or account there, and a short
    name such as 'ANN' fits the currency's shape (#1027, #1047)."""
    described = _non_credit(
        group=group,
        description=description,
        amount=Decimal(amount),
        currency=currency,
        row_date=date(2025, 3, 1),
    ).describe()

    assert described == rendered
    for value in ("Synthetic Holder Two", "U1234567", "1234567.89", "2025-03-01"):
        assert value.lower() not in described.lower(), value
    # A whole token: fixed text such as "cannot" can hold the letters of ANN.
    assert re.search(rf"(?i)\b{currency}\b", described) is None
