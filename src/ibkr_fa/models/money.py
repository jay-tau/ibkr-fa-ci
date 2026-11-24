"""Whole-rupee rounding for portal amounts (#615) and the CBDT bound on them (#788, #830)."""

from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal
from typing import Final

from ibkr_fa.invariants import require

_ONE_RUPEE: Final[Decimal] = Decimal("1")
_ZERO: Final[Decimal] = Decimal("0")
# The CBDT ITR-2 and ITR-3 schemas cap a filed amount at 14 digits. The bound
# applies to negative amounts too, although ITR-3 sets no minimum for the FA A2
# peak and closing balance (the owner's decision on #788).
CBDT_AMOUNT_BOUND: Final[int] = 99_999_999_999_999
# An amount rounds HALF_UP to a whole rupee within the bound exactly when its
# magnitude is below this. Comparing with it, rather than rounding, holds for
# any finite Decimal, whatever the decimal context's precision. It is spelled
# from the bound's digits rather than computed, so no context rounds it.
_HALF_UP_LIMIT: Final[Decimal] = Decimal(f"{CBDT_AMOUNT_BOUND}.5")


def to_whole_rupees(value: Decimal) -> Decimal:
    """Round ``value`` HALF_UP to whole rupees, never returning ``-0``."""
    rounded = value.quantize(_ONE_RUPEE, rounding=ROUND_HALF_UP)
    return rounded if rounded else _ZERO


def optional_whole_rupees(value: Decimal | None) -> Decimal | None:
    """:func:`to_whole_rupees` of ``value``; an absent amount (``None``) stays ``None``."""
    return None if value is None else to_whole_rupees(value)


def require_whole_rupees(value: Decimal, *, what: str, **context: object) -> None:
    """Require ``value`` to be exactly what :func:`to_whole_rupees` gives (#900).

    Writers render such a value as it is, so a fraction, an exponent form
    (``500.00``, ``5E+2``) or ``-0`` is refused. Call it after
    :func:`require_filed_amount`, which bounds the rounding.
    """
    require(
        value.compare_total(to_whole_rupees(value)) == 0,
        what=f"{what} must be a whole-rupee amount (no fraction, exponent or -0)",
        value=value,
        **context,
    )


def within_filed_bound(value: Decimal) -> bool:
    """Whether a finite ``value`` rounds HALF_UP to a whole rupee within the CBDT bound."""
    return value.copy_abs() < _HALF_UP_LIMIT


def require_filed_amount(value: Decimal, *, what: str, **context: object) -> None:
    """Require a finite ``value`` to round HALF_UP to a whole rupee within the CBDT bound.

    ``what`` names the filed amount, such as a model field or a total.
    """
    require(
        within_filed_bound(value),
        what=(
            f"{what} must round to a whole-rupee amount within "
            f"±{CBDT_AMOUNT_BOUND:,} (the CBDT 14-digit bound)"
        ),
        value=value,
        **context,
    )
