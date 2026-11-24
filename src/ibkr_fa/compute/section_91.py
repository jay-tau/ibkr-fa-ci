"""The exact Form 67 section 91 credit and its refusals (#1177).

The credit is the exact rational ``R = min(foreign_tax_inr, total_tax *
income_inr / total_income)``. The minimum is chosen by comparing
``total_tax * income_inr`` with ``foreign_tax_inr * total_income`` as exact
:class:`~fractions.Fraction` products. When foreign tax wins or ties, the
credit is the original ``foreign_tax_inr``, never round-tripped through a rate
(PR #233). Otherwise ``R``'s numerator and denominator are converted to
:class:`~decimal.Decimal` exactly and divided once, rounding down at the
caller's precision, in a copy of the caller's context. The quotient may
repeat; it is never more than ``R``, so it stays below foreign tax.

Before it is returned, the credit must round HALF_UP, through the existing
whole-rupee conversion, to the exact ``(2n + d) // (2d)`` of ``R = n/d``. It
must also stay within ``[0, foreign_tax_inr]`` and be positive exactly when
``R`` is. Form 67 cols 12 and 13 and the Schedule FSI relief then hold this
one Decimal and round it where they always have.

The former formula, ``min(foreign_tax, total_tax / total_income *
income_inr)`` at the caller's precision, still runs first as a validation-only
preflight, so no invocation that refused before passes. Its credit is checked
here against the existing credit checks, and :class:`Section91Check` carries
it to the readiness and Schedule FSI portal checks, which apply it where they
already ran. It is never filed or returned as a credit.

Every refusal is an :class:`~ibkr_fa.exceptions.InvariantError` with one of
the fixed messages below, no stored context and no cause or context chain. It
names only the country's 1-based position among the countries the
foreign-tax netting returns, counted before any is skipped.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_DOWN, Decimal, DecimalException, localcontext
from fractions import Fraction
from typing import Final

from ibkr_fa.compute.exact_inr import exact_fraction, exact_whole_rupees
from ibkr_fa.exceptions import InvariantError
from ibkr_fa.invariants import invariant_error
from ibkr_fa.models.money import require_filed_amount, to_whole_rupees

__all__ = [
    "EXACT_OTHER",
    "EXACT_PORTAL",
    "EXACT_ZERO",
    "PREFLIGHT_OTHER",
    "PREFLIGHT_PORTAL",
    "PREFLIGHT_ZERO",
    "PROOF",
    "Section91Check",
    "exact_section_91_credit",
    "section_91_error",
]

_ZERO: Final[Decimal] = Decimal("0")

_CREDIT_FIELD: Final[str] = "field section_91_credit_inr"
PROOF: Final[str] = (
    "fields section_91_credit_inr and total_credit_inr: exact filed credit cannot be "
    "guaranteed within Decimal precision"
)
PREFLIGHT_ZERO: Final[str] = (
    f"{_CREDIT_FIELD}: the credit as filed today rounds to zero, and a filed credit "
    "must be positive"
)
PREFLIGHT_PORTAL: Final[str] = (
    f"{_CREDIT_FIELD}: the relief as filed today isn't the lower of the filed foreign "
    "tax and the filed Indian tax"
)
PREFLIGHT_OTHER: Final[str] = (
    f"{_CREDIT_FIELD}: the credit as filed today fails an existing Form 67 credit check"
)
EXACT_ZERO: Final[str] = (
    f"{_CREDIT_FIELD}: the exact credit rounds to zero, and a filed credit must be positive"
)
EXACT_PORTAL: Final[str] = (
    f"{_CREDIT_FIELD}: the exact relief isn't the lower of the filed foreign tax and the "
    "filed Indian tax"
)
EXACT_OTHER: Final[str] = (
    f"{_CREDIT_FIELD}: the exact credit fails an existing Form 67 credit check"
)


def section_91_error(position: int, detail: str) -> InvariantError:
    """The refusal for country input row ``position``; ``detail`` is a constant above."""
    return invariant_error(what=f"Form 67 section 91, country input row {position}, {detail}")


@dataclass(frozen=True, slots=True)
class Section91Check:
    """What the readiness and Schedule FSI portal checks need for one credit.

    ``position`` is the country's 1-based input row. ``legacy_credit_inr`` is
    the former formula's credit, kept only so those checks still refuse
    whatever they refused before; it is never filed.
    """

    position: int
    legacy_credit_inr: Decimal


def _passes_credit_checks(credit: Decimal, foreign_tax_inr: Decimal) -> bool:
    """Whether ``credit`` passes the existing Form 67 credit checks.

    Those are the filed-amount bound, ``0 <= credit <= foreign_tax_inr``
    (which Schedule FSI's relief shares) and the whole-rupee rounding, at the
    caller's precision and traps in a copy of its context.
    """
    with localcontext() as context:
        context.clear_flags()
        try:
            require_filed_amount(credit, what="Form67Entry.section_91_credit_inr")
            passes = credit.is_finite() and _ZERO <= credit <= foreign_tax_inr
            if passes:
                to_whole_rupees(credit)
        except (InvariantError, DecimalException):
            passes = False
    return passes


def _legacy_credit(
    total_tax: Decimal, total_income: Decimal, income_inr: Decimal, foreign_tax_inr: Decimal
) -> Decimal | None:
    """The former formula's credit, or ``None`` if it or a credit check fails."""
    with localcontext() as context:
        context.clear_flags()
        try:
            indian_average_rate = total_tax / total_income
            credit = min(foreign_tax_inr, indian_average_rate * income_inr)
        except DecimalException:
            return None
    return credit if _passes_credit_checks(credit, foreign_tax_inr) else None


def _proves(credit: Decimal, exact: Fraction, foreign_tax_inr: Decimal) -> bool:
    """Whether ``credit`` files as the exact credit and keeps its raw ceiling.

    The filed value is the existing HALF_UP whole-rupee conversion, at the
    caller's precision and traps in a copy of its context; for ``exact = n/d
    >= 0`` it must equal ``(2n + d) // (2d)``.
    """
    if not credit.is_finite() or exact < 0:
        return False
    with localcontext() as context:
        context.clear_flags()
        try:
            filed = to_whole_rupees(credit)
        except DecimalException:
            return False
    return (
        filed == exact_whole_rupees(exact)
        and _ZERO <= credit <= foreign_tax_inr
        and (credit > _ZERO) == (exact > 0)
    )


def _exact_credit(
    total_tax: Decimal, total_income: Decimal, income_inr: Decimal, foreign_tax_inr: Decimal
) -> Decimal | None:
    """The proven credit, or ``None`` if conversion, division or the proof fails."""
    operands = tuple(
        exact_fraction(value) for value in (total_tax, total_income, income_inr, foreign_tax_inr)
    )
    tax, total, income, foreign_tax = operands
    if tax is None or total is None or income is None or foreign_tax is None or total <= 0:
        return None
    if foreign_tax * total <= tax * income:
        exact = foreign_tax
        credit = foreign_tax_inr
    else:
        exact = tax * income / total
        with localcontext() as context:
            context.rounding = ROUND_DOWN
            context.clear_flags()
            try:
                credit = Decimal(exact.numerator) / Decimal(exact.denominator)
            except DecimalException:
                return None
    return credit if _proves(credit, exact, foreign_tax_inr) else None


def exact_section_91_credit(
    *,
    total_tax: Decimal,
    total_income: Decimal,
    income_inr: Decimal,
    foreign_tax_inr: Decimal,
    position: int,
) -> tuple[Decimal, Section91Check]:
    """Return the proven section 91 credit and its :class:`Section91Check`.

    Refuses, naming ``position``: with :data:`PREFLIGHT_OTHER` when the former
    formula raises a decimal exception or its credit fails an existing credit
    check; with :data:`PROOF` when conversion, division or the proof fails; and
    with :data:`EXACT_OTHER` when the proven credit fails an existing credit
    check (the filed-amount bound).
    """
    legacy = _legacy_credit(total_tax, total_income, income_inr, foreign_tax_inr)
    if legacy is None:
        raise section_91_error(position, PREFLIGHT_OTHER)
    credit = _exact_credit(total_tax, total_income, income_inr, foreign_tax_inr)
    if credit is None:
        raise section_91_error(position, PROOF)
    if not _passes_credit_checks(credit, foreign_tax_inr):
        raise section_91_error(position, EXACT_OTHER)
    return credit, Section91Check(position=position, legacy_credit_inr=legacy)
