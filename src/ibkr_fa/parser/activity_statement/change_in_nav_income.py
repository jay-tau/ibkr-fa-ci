"""Check an Activity Statement's income sections against its ``Change in NAV`` (#1279).

IBKR leaves an empty section out of the export, so the income commands
and Schedule FA (#1324) read a missing ``Dividends``, ``Withholding Tax``
or ``Interest`` section as no income. A custom statement can also leave out a section that had
rows. The statement's ``Change in NAV`` section prints the period's
totals, so when it is printed, each of these fields must agree with its
section:

* ``Dividends`` with the ``Dividends`` section;
* ``Withholding Tax`` with the ``Withholding Tax`` section;
* ``Interest`` with the ``Interest`` section, every row counted
  (credits, debit interest and unproven rows), as its printed totals are;
* ``Payment In Lieu Of Dividends``, when printed, with the ``Dividends``
  section's payment-in-lieu rows; the ``Dividends`` field then covers the
  other rows.

A field matches its label ignoring case and surrounding spaces, and a
field that isn't printed counts as zero (IBKR leaves zero lines out). A
nonzero field whose section is missing refuses, and so does a field that
differs from its rows' exact sum by more than 0.01. ``Change in NAV`` is
in the statement's base currency, so a section with a row in another
currency can't be checked and refuses too. A statement without ``Change
in NAV`` isn't checked.

Every comparison refusal is a :class:`ChangeInNavIncomeError` with fixed
text naming the field and the detail section; none prints an input amount,
date or account. Malformed blocks raise the shared reader's ``ParseError``.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from decimal import Decimal
from typing import Final

from ibkr_fa.exceptions import IbkrFaError
from ibkr_fa.models import (
    DistributionKind,
    Dividend,
    InterestCredit,
    InterestNonCredit,
    WithholdingTax,
)
from ibkr_fa.parser._exact import exactly
from ibkr_fa.parser.activity_statement._totals import _RE_EXPORT
from ibkr_fa.parser.activity_statement.multi_section import KnownActivitySection, Section
from ibkr_fa.parser.activity_statement.net_asset_value import parse_change_in_nav_fields

_DIVIDENDS: Final[str] = "Dividends"
_PAYMENT_IN_LIEU: Final[str] = "Payment In Lieu Of Dividends"
_WITHHOLDING_TAX: Final[str] = "Withholding Tax"
_INTEREST: Final[str] = "Interest"

# Change in NAV and the detail sections print to the cent; #906's NAV
# cross-check allows the same one-cent slack.
_TOLERANCE: Final[Decimal] = Decimal("0.01")


class ChangeInNavIncomeError(IbkrFaError):
    """An income section that the statement's ``Change in NAV`` contradicts (#1279)."""


def _field(
    fields: Mapping[str, Decimal], label: str, section: KnownActivitySection
) -> Decimal | None:
    """The field printed under ``label``, ignoring case; ``None`` when it isn't printed."""
    values = [value for name, value in fields.items() if name.casefold() == label.casefold()]
    if len(values) > 1:
        raise ChangeInNavIncomeError(
            f"the Activity Statement's Change in NAV prints more than one {label!r} "
            f"line (ignoring case), so the amount to check the {section.value!r} "
            f"section against is ambiguous. {_RE_EXPORT}"
        )
    return values[0] if values else None


def _check(
    *,
    label: str,
    field: Decimal | None,
    section: KnownActivitySection,
    present: bool,
    rows: str,
    amounts: Sequence[tuple[str, Decimal]],
    base_currency: str,
) -> None:
    """Refuse unless ``field`` agrees with ``amounts``, the ``(currency, amount)`` of ``rows``.

    ``rows`` names them in the refusal, such as "the 'Dividends' section".
    """
    if not present:
        if field is not None and field != 0:
            raise ChangeInNavIncomeError(
                f"the Activity Statement's Change in NAV shows a nonzero {label!r} "
                f"amount, but the statement has no {section.value!r} section. Re-export "
                f"the Activity Statement with the {section.value!r} section included; "
                f"the run refuses rather than treat the section as empty."
            )
        return
    if any(currency != base_currency for currency, _ in amounts):
        raise ChangeInNavIncomeError(
            f"the Activity Statement's {section.value!r} section has a row in a "
            f"currency other than the statement's base currency, so it can't be "
            f"checked against the Change in NAV {label!r} amount, which is in the "
            f"base currency. The run refuses rather than skip the check."
        )

    def precision_refusal(_: bool) -> ChangeInNavIncomeError:
        return ChangeInNavIncomeError(
            f"the Activity Statement's Change in NAV {label!r} amount can't be "
            f"compared exactly with the total of {rows}: the amounts are too large "
            f"or too small. {_RE_EXPORT}"
        )

    def _less() -> Decimal:
        difference = field if field is not None else Decimal(0)
        for _, amount in amounts:
            difference -= amount
        return difference

    mismatch: bool | None = None
    try:
        difference = exactly(_less, refusal=precision_refusal)
        mismatch = difference.copy_abs() > _TOLERANCE
    except MemoryError:
        pass  # context setup and magnitude allocation are outside exactly's handler
    if mismatch is None:
        raise precision_refusal(True)
    if mismatch:
        raise ChangeInNavIncomeError(
            f"the Activity Statement's Change in NAV {label!r} amount (zero when "
            f"the line isn't printed) differs by more than 0.01 from the total of "
            f"{rows}. A row may be missing or changed. {_RE_EXPORT}"
        )


def check_income_against_change_in_nav(
    sections: Mapping[KnownActivitySection, list[Section]],
    *,
    base_currency: str,
    dividends: Sequence[Dividend],
    withholding_tax: Sequence[WithholdingTax],
    interest_credits: Sequence[InterestCredit],
    interest_non_credits: Sequence[InterestNonCredit],
) -> None:
    """Refuse income sections that the statement's ``Change in NAV`` contradicts.

    ``sections`` is the split statement and the records are what its income
    sections parsed to. Does nothing when the statement has no ``Change in
    NAV`` section. The rules are in the module docstring.

    Raises:
        ParseError: When a ``Change in NAV`` block or row can't be read
            (see :func:`parse_change_in_nav_fields`).
        ChangeInNavIncomeError: When a field is printed more than once
            (ignoring case), or a field and its section disagree.
    """
    change_in_nav = sections.get(KnownActivitySection.CHANGE_IN_NAV)
    if not change_in_nav:
        return
    fields = parse_change_in_nav_fields(change_in_nav)
    dividends_field = _field(fields, _DIVIDENDS, KnownActivitySection.DIVIDENDS)
    payment_in_lieu_field = _field(fields, _PAYMENT_IN_LIEU, KnownActivitySection.DIVIDENDS)
    withholding_field = _field(fields, _WITHHOLDING_TAX, KnownActivitySection.WITHHOLDING_TAX)
    interest_field = _field(fields, _INTEREST, KnownActivitySection.INTEREST)

    dividends_present = KnownActivitySection.DIVIDENDS in sections
    section_rows = f"the {_DIVIDENDS!r} section"
    if payment_in_lieu_field is None:
        dividend_rows = dividends
        dividend_scope = section_rows
    else:
        in_lieu_kind = DistributionKind.PAYMENT_IN_LIEU_OF_DIVIDEND
        in_lieu = [row for row in dividends if row.kind is in_lieu_kind]
        dividend_rows = [row for row in dividends if row.kind is not in_lieu_kind]
        dividend_scope = f"{section_rows}'s rows other than payments in lieu"
        _check(
            label=_PAYMENT_IN_LIEU,
            field=payment_in_lieu_field,
            section=KnownActivitySection.DIVIDENDS,
            present=dividends_present,
            rows=f"{section_rows}'s payment-in-lieu rows",
            amounts=[(row.currency, row.amount_gross) for row in in_lieu],
            base_currency=base_currency,
        )
    _check(
        label=_DIVIDENDS,
        field=dividends_field,
        section=KnownActivitySection.DIVIDENDS,
        present=dividends_present,
        rows=dividend_scope,
        amounts=[(row.currency, row.amount_gross) for row in dividend_rows],
        base_currency=base_currency,
    )
    _check(
        label=_WITHHOLDING_TAX,
        field=withholding_field,
        section=KnownActivitySection.WITHHOLDING_TAX,
        present=KnownActivitySection.WITHHOLDING_TAX in sections,
        rows=f"the {_WITHHOLDING_TAX!r} section",
        amounts=[(row.currency, row.amount) for row in withholding_tax],
        base_currency=base_currency,
    )
    _check(
        label=_INTEREST,
        field=interest_field,
        section=KnownActivitySection.INTEREST,
        present=KnownActivitySection.INTEREST in sections,
        rows=f"the {_INTEREST!r} section",
        amounts=[
            *((row.currency, row.amount_gross) for row in interest_credits),
            *((row.currency, row.amount) for row in interest_non_credits),
        ],
        base_currency=base_currency,
    )
