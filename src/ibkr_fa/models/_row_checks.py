"""Field checks shared by the row models (#788, #830).

Each row runs them first, so a field of the wrong type is refused, by name,
before a later check fails on it with a raw exception. Each takes the row and
the names of the fields to check.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from fractions import Fraction
from typing import TYPE_CHECKING

from ibkr_fa.invariants import require
from ibkr_fa.models.money import require_filed_amount, require_whole_rupees

if TYPE_CHECKING:
    from collections.abc import Callable


def _require_each(
    row: object, names: tuple[str, ...], kind: str, ok: Callable[[object], bool], *, optional: bool
) -> None:
    for name in names:
        value = getattr(row, name)
        require(
            ok(value) or (optional and value is None),
            what=f"{type(row).__name__}.{name} must be {kind}{' or None' if optional else ''}",
            type=type(value).__name__,
        )


def require_text(row: object, *names: str, optional: bool = False) -> None:
    """Require each named field to be a ``str``."""
    _require_each(row, names, "a str", lambda value: isinstance(value, str), optional=optional)


def require_max_lengths(row: object, **limits: int) -> None:
    """Require each named text field to be at most its limit in characters.

    The limit applies to the raw text, before any writer renders it (#1189).
    """
    for name, limit in limits.items():
        require(
            len(getattr(row, name)) <= limit,
            what=f"{type(row).__name__}.{name} must be at most {limit} characters",
        )


def upload_text(value: str) -> str:
    """``value`` as Schedule FA's bulk-upload file writes it, because the portal
    splits rows on raw commas (#1329): each comma becomes a space, whitespace at
    either end is dropped and each run inside collapses to one space."""
    return " ".join(value.replace(",", " ").split())


def require_upload_text(row: object, *names: str) -> None:
    """Require each named text field that holds a non-whitespace character to
    keep one in its :func:`upload_text` rendering, so that a value of only
    commas and spaces never files as a blank cell (#1329). An empty or
    whitespace-only value passes; the row checks those by its own rules."""
    for name in names:
        value = getattr(row, name)
        require(
            value.strip() == "" or upload_text(value) != "",
            what=(
                f"{type(row).__name__}.{name} must contain a character other than "
                "a comma or a space"
            ),
        )


def require_dates(row: object, *names: str, optional: bool = False) -> None:
    """Require each named field to be exactly a :class:`date`, never a ``datetime``."""
    _require_each(
        row, names, "a date, not a datetime", lambda value: type(value) is date, optional=optional
    )


def require_bools(row: object, *names: str) -> None:
    """Require each named field to be exactly a ``bool``."""
    _require_each(row, names, "a bool", lambda value: type(value) is bool, optional=False)


def require_amounts(row: object, *names: str, optional: bool = False) -> None:
    """Require each named field to be a finite :class:`Decimal`."""
    _require_each(
        row,
        names,
        "a finite Decimal",
        lambda value: isinstance(value, Decimal) and value.is_finite(),
        optional=optional,
    )


def require_filed_amounts(row: object, *names: str, optional: bool = False) -> None:
    """Require each named field to be a finite :class:`Decimal` that rounds
    HALF_UP to a whole rupee within the CBDT bound."""
    require_amounts(row, *names, optional=optional)
    for name in names:
        value = getattr(row, name)
        if value is not None:
            require_filed_amount(value, what=f"{type(row).__name__}.{name}")


def require_whole_rupee_amounts(row: object, *names: str, optional: bool = False) -> None:
    """Require each named field to be a filed amount already in whole rupees (#900)."""
    require_filed_amounts(row, *names, optional=optional)
    for name in names:
        value = getattr(row, name)
        if value is not None:
            require_whole_rupees(value, what=f"{type(row).__name__}.{name}")


def require_exact_amount_fields(row: object, class_name: str, *names: str) -> None:
    """Require each named field's ``<name>_exact`` field to be ``None``, or a
    :class:`Fraction` (not a subclass) that the field's Decimal rounds
    (:func:`~ibkr_fa.compute.exact_inr.is_exact_decimal`).

    The fields are checked in the order given, and the refusal names
    ``class_name``.
    """
    # Imported here: a module-level import would run ibkr_fa.compute's
    # package init, which imports these models back.
    from ibkr_fa.compute.exact_inr import is_exact_decimal  # noqa: PLC0415

    for name in names:
        amount = getattr(row, name)
        exact = getattr(row, f"{name}_exact")
        require(
            exact is None
            or (type(exact) is Fraction and amount is not None and is_exact_decimal(amount, exact)),
            what=f"{class_name}.{name}_exact must be the exact amount {name} rounds",
        )
