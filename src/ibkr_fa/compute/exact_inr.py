"""Exact INR conversions and sums (#1136, #1166).

Every Schedule OS income line (1a(i) dividends, 1b(ix) interest, 1e other
income) is converted with :func:`exact_inr_conversion`. Every filed Schedule
OS income total, the FSI source aggregations, the FSI relief other-income map
and the FSI merge are summed with :func:`exact_inr_sum` or
:func:`exact_signed_inr_sum`. These three work at :data:`EXACT_SUM_DIGITS`
digits or more, so they are exact up to that cap, and refuse rather than
round past it (#1312).

The income and foreign-tax conversions shared across brokers are exact the
same way (#1312): Schedule FA's December 31 conversion of each symbol's
dividend total (Table A3, and Table A2's dividend row) and of Table A2's
interest total, each withholding row's INR conversion that Schedule FSI,
Schedule TR and Form 67 read, and Form 67's treaty-rate test and treaty cap
(:func:`checked_product`, :func:`checked_subtraction`). Form 67's unsnapped rates file
the exact rate's hundredths when the context keeps a third decimal place,
withholding reversals pair only with an original
equal to every digit, and Fidelity CMA's eligible US tax is negated with
``copy_negate()``. The whole-rupee splits of exact Schedule OS and FSI amounts
compare exact remainders.

The Schedule CG portal totals, the FSI capital-gains nets (#1166) and the
Schedule FA securities, dividend, interest
and proceeds totals (#1168) add with :func:`checked_sum` and
:func:`checked_addition` instead. Splitting a sale across lots by quantity can produce per-lot
amounts with 28 to 60 significant digits, so adding them can need a digit
more than the context holds. These additions widen the
precision to at least :data:`EXACT_SUM_DIGITS` and never round. Each Schedule
CG line's INR proceeds and non-overridden cost (:func:`checked_product`) and
gain (:func:`checked_subtraction`) are computed the same way (#1184).
Statutory INR costs are preserved as supplied. The line models check proceeds
and gain with :func:`exact_product` and :func:`exact_difference`.
Schedule FA's December 31 close and its conversions are exact-or-refuse the
same way (#1250): each per-share close times its quantity, the cross-check
and FMV-anchor differences and tolerances, the Table A3 closing, initial and
gross-proceeds values, and the Table A2 closing balance and NetBenefits'
closing check (:func:`checked_product`, :func:`checked_subtraction`).
The Schedule FA daily peak's products are :func:`checked_product` too (#1260):
each A3 candidate's price times its quantity, the high basis's close and high
values, the floor's closing and peak values, and every peak's INR conversion.
Filed CG consideration and cost and FSI income are HALF_UP of their exact
totals; CG files rounded consideration minus rounded cost. Refusals name only
fixed labels and positions, never an input value.

A share split from a source amount by quantity, such as a lot's cost for the
part sold, is :func:`exact_share`: an exact :class:`~fractions.Fraction`,
since no Decimal holds a third (#1241). Robinhood's lot and sale shares stay
exact rationals until the filed rounding, and so do the quantity parts of a
same-day pro-rata sale (#1263), which later shares split by. Fidelity CMA's
(#1244) and IBKR's (#1273) FIFO cost and proceeds shares are exact the same
way, and their FIFO quantities and gross proceeds are exact Decimals
(:func:`checked_subtraction`, :func:`checked_product`), so IBKR's FIFO
quantities and shares are exact up to the filed rounding. The CG lines carry
them, the CG portal totals and the FSI capital-gains nets
add them (:func:`checked_exact_sum`). Filed CG consideration and cost and FSI income
are their exact HALF_UP (:func:`exact_whole_rupees`); CG balances subtract
the rounded components. A Decimal kept for review, such as a
line amount or the audit TOTAL row, is one division of its rational
(:func:`exact_decimal`). Operands past :data:`EXACT_SUM_DIGITS` refuse
(:func:`exact_fraction`). Schedule FA A3 converts Robinhood's carried cost
rationals, split closing shares and credit shares the same way (#1243):
these initial values, closing balances and gross credits file the exact
HALF_UP of the rational times the TTBR (:func:`checked_exact_product`). Costs
without a separate rational keep the existing Decimal initial-value product
(#1250). A held StockPlan vest's cost is its exact FMV times shares deposited
(:func:`checked_product`, #1314).

Parsers may not import this module, so their own cross-checks use
:mod:`ibkr_fa.parser._exact`: the IBKR Activity Statement totals (#908), and
the Robinhood activity CSV's dividend and ADR-fee check and the brokerage
statement's total, running-balance and row market-value checks (#1295). The
Robinhood normalizer's cash identities, statement position sums, quantity
bands and replay use it too (#1308), and so do the IBKR Activity Statement
Trades net proceeds and Open Positions reconciliation and the Flex closed-lot
proceeds and ``PositionValue`` check (#1311). The StockPlan Holdings Market
Value check (#1290) and the Activity Statement's Change in NAV income check
(#1279) use it as well. The Trades quantities and ``|Basis|`` keep every digit
with ``copy_abs()``.

Input numbers within each reader's scope are limited when read, through
:func:`ibkr_fa.number_checks.number_limit_breach`: Robinhood, NetBenefits and
Fidelity CMA statement numbers, ``user.toml`` decimal fields and the CLI USD
flag (#1275), and IBKR, StockPlan, TTBR and price numbers (#1276). A limited
input cell is nowhere near
:data:`EXACT_SUM_DIGITS`. The cap stays as the
backstop for derived values, which in-limit cells can still grow past any
read-time limit: products of a quantity, a price and a rate, sums over many
rows, exact shares of earlier shares, and models built directly in code.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from decimal import (
    MAX_EMAX,
    MIN_EMIN,
    ROUND_DOWN,
    Decimal,
    DecimalException,
    Inexact,
    Rounded,
    localcontext,
)
from fractions import Fraction
from typing import Final

from ibkr_fa.invariants import invariant_error, require

_ZERO: Final[Decimal] = Decimal("0")

# The minimum precision :func:`checked_addition` adds at (#1166). Ordinary
# statement-derived operands use up to 60 significant digits (Robinhood's
# allocation precision). This leaves over 900 digits for magnitude differences
# and accumulation, far beyond ordinary rupee or dollar totals. The cap
# rejects extreme input while allowing those splits. An addition's cost grows
# with its operands' and result's digits, not with this setting.
EXACT_SUM_DIGITS: Final[int] = 1000

# The fixed ends of the exact-arithmetic refusals; each site puts its own
# label in front.
INEXACT_PRODUCT_REFUSAL: Final[str] = ": product must be exact within Decimal precision"
_INEXACT_SUM_REFUSAL: Final[str] = ": sum must be exact within Decimal precision"
_INEXACT_INR_SUM_REFUSAL: Final[str] = " INR sum must be exact within Decimal precision"


def _exact(
    operation: Callable[[], Decimal], *, lost: type[DecimalException]
) -> tuple[Decimal, bool]:
    """Run ``operation`` in a copy of the active context with cleared flags.

    That copy is widened to at least :data:`EXACT_SUM_DIGITS` digits.
    Returns the result and whether it is finite, raised no decimal exception and
    did not signal ``lost``. The caller refuses outside any ``except`` block,
    so its error chains no arithmetic exception.
    """
    result = _ZERO
    with localcontext() as context:
        context.prec = max(context.prec, EXACT_SUM_DIGITS)
        context.clear_flags()
        try:
            result = operation()
            exact = result.is_finite() and not context.flags[lost]
        except DecimalException:
            exact = False
    return result, exact


def exact_inr_sum(amounts: Iterable[Decimal], *, label: str) -> Decimal:
    """Sum non-negative INR amounts, refusing a negative amount or a dropped digit.

    The exact-sum rule for the Schedule OS dividend and interest totals and
    the FSI dividend and interest aggregations. The sum runs as
    :func:`exact_signed_inr_sum` does, at :data:`EXACT_SUM_DIGITS` digits or
    the active context's precision if that is more (#1312). ``Rounded`` is
    signalled whenever an addition drops a digit, even a zero. If none is
    dropped, the total fits that precision at the smallest exponent, and so
    does every partial sum of non-negative amounts in any order. The total is
    then exact and the same in every order, so every writer and the run report
    agree on it.
    """
    materialised = tuple(amounts)
    require(
        all(amount.is_finite() for amount in materialised),
        what=f"{label}{_INEXACT_INR_SUM_REFUSAL}",
    )
    require(
        all(amount >= 0 for amount in materialised),
        what=f"{label} INR amounts must not be negative",
    )
    return exact_signed_inr_sum(materialised, label=label)


def exact_signed_inr_sum(amounts: Iterable[Decimal], *, label: str) -> Decimal:
    """Sum INR amounts of either sign, refusing a sum that drops any digit.

    The exact-sum rule for the signed totals: each Schedule OS 1e nature, the
    FSI substitute-dividend and other-income aggregations, the FSI relief
    other-income map and the FSI merge. The additions run at
    :data:`EXACT_SUM_DIGITS` digits, or the active context's precision if
    that is more, as :func:`checked_addition`'s do (#1312), so the sum is
    exact up to that cap. It refuses a nonfinite amount or total, a decimal
    arithmetic failure, or any addition, in the given order, that would still
    signal ``Rounded``; the total is then exact. Unlike :func:`exact_inr_sum`
    it applies no sign rule, and another order of mixed-sign amounts might
    have passed.
    """
    total, exact = _exact(lambda: sum(amounts, _ZERO), lost=Rounded)
    require(exact, what=f"{label}{_INEXACT_INR_SUM_REFUSAL}")
    return total


def checked_addition(
    augend: Decimal, addend: Decimal, *, at: str, source_row_index: int | None = None
) -> Decimal:
    """Return the exact ``augend + addend``, of any sign and in any currency.

    The addition runs at :data:`EXACT_SUM_DIGITS` digits, or the active
    context's precision if that is more, so a sum that needs a digit more
    than the context holds keeps it rather than rounding (#1166). It refuses
    a nonfinite operand or result, a decimal arithmetic failure such as an
    overflow past the context's exponent limit, and a sum that would still
    round at that precision. The refusal is
    ``f"{at}: sum must be exact within Decimal precision"``, so ``at`` names
    the total and where it failed; it must be a fixed label, never input text.
    The refusal names ``source_row_index`` when the addend's input has one.
    """
    total, exact = _exact(lambda: augend + addend, lost=Rounded)
    row = {} if source_row_index is None else {"source_row_index": source_row_index}
    require(exact, what=f"{at}{_INEXACT_SUM_REFUSAL}", **row)
    return total


def checked_subtraction(minuend: Decimal, subtrahend: Decimal, *, at: str) -> Decimal:
    """Return the exact ``minuend - subtrahend`` as a :func:`checked_addition`.

    The subtrahend's sign is changed with ``copy_negate()``, which never
    rounds; unary minus would round an operand longer than the context's
    precision before the addition saw it.
    """
    return checked_addition(minuend, subtrahend.copy_negate(), at=at)


def exact_product(multiplicand: Decimal, multiplier: Decimal) -> Decimal | None:
    """Return the exact ``multiplicand * multiplier``, or ``None`` if it can't be had.

    The product runs at the precision :func:`checked_addition` uses and keeps
    every digit (#1184). It is ``None`` for a nonfinite operand or result, a
    decimal arithmetic failure such as an overflow past the context's
    exponent limit, or a product that would still round at that precision.
    """
    product, exact = _exact(lambda: multiplicand * multiplier, lost=Rounded)
    return product if exact else None


def exact_difference(minuend: Decimal, subtrahend: Decimal) -> Decimal | None:
    """Return the exact ``minuend - subtrahend``, or ``None`` if it can't be had.

    The non-refusing form of :func:`checked_subtraction`, under the same
    precision and failures as :func:`exact_product`.
    """
    difference, exact = _exact(lambda: minuend + subtrahend.copy_negate(), lost=Rounded)
    return difference if exact else None


def checked_product(multiplicand: Decimal, multiplier: Decimal, *, at: str) -> Decimal:
    """Return :func:`exact_product`, refusing where it is ``None``.

    The refusal is ``f"{at}: product must be exact within Decimal
    precision"``; ``at`` must be a fixed label, never input text.
    """
    product = exact_product(multiplicand, multiplier)
    if product is None:
        raise invariant_error(what=f"{at}{INEXACT_PRODUCT_REFUSAL}")
    return product


def checked_sum(terms: Iterable[tuple[int, Decimal]], *, at: Callable[[int], str]) -> Decimal:
    """Add ``(position, amount)`` terms from zero in order, each a :func:`checked_addition`.

    A refusal is located by ``at(position)`` for the term whose addition
    failed, so the caller's positions, not this function, say which input
    row is named.
    """
    total = _ZERO
    for position, amount in terms:
        total = checked_addition(total, amount, at=at(position))
    return total


def exact_fraction(value: Decimal) -> Fraction | None:
    """``value`` as an exact :class:`~fractions.Fraction`, or ``None`` if it isn't
    finite or has more than :data:`EXACT_SUM_DIGITS` digits or places either side
    of the units digit.

    The bound keeps the integers small: ``Fraction(Decimal("1E-999999999"))``
    would need a billion-digit denominator.
    """
    _, digits, exponent = value.as_tuple()
    # A nonfinite value's exponent is a letter.
    if (
        not isinstance(exponent, int)
        or len(digits) > EXACT_SUM_DIGITS
        or exponent < -EXACT_SUM_DIGITS
        or value.adjusted() > EXACT_SUM_DIGITS
    ):
        return None
    return Fraction(value)


def exact_share(
    amount: Decimal | Fraction, part: Decimal | Fraction, whole: Decimal | Fraction
) -> Fraction | None:
    """Return ``amount * part / whole`` as an exact :class:`~fractions.Fraction` (#1241).

    The share of ``amount`` that ``part`` of ``whole`` carries, such as a lot's
    cost for the quantity sold. ``None`` if ``whole`` is zero or a
    :class:`~decimal.Decimal` operand fails :func:`exact_fraction`. A
    ``Fraction`` operand is an earlier share, such as a lot's exact quantity
    after a pro-rata sale (#1263), already within those bounds.
    """
    exact_amount, exact_part, exact_whole = (
        value if isinstance(value, Fraction) else exact_fraction(value)
        for value in (amount, part, whole)
    )
    if exact_amount is None or exact_part is None or not exact_whole:
        return None
    return exact_amount * exact_part / exact_whole


def exact_decimal(value: Fraction) -> Decimal:
    """``value`` as one division of its numerator by its denominator (#1241).

    The division runs in a copy of the caller's context, rounding toward zero
    with no rounding signal trapped, at the caller's precision or, where that
    can't keep a tenth, at the precision that does. Its exponent limits are
    the caller's, or the widest, so it can't overflow or underflow. So it is
    ``value`` exactly whenever that fits, and its HALF_UP whole rupees
    (:func:`~ibkr_fa.models.money.to_whole_rupees`) are always
    :func:`exact_whole_rupees` of ``value``: a tenth is kept, so a value on
    or past a half stays there.
    """
    numerator, denominator = Decimal(value.numerator), Decimal(value.denominator)
    with localcontext() as context:
        context.rounding = ROUND_DOWN
        context.traps[Inexact] = context.traps[Rounded] = False
        context.Emax, context.Emin = max(context.Emax, MAX_EMAX), min(context.Emin, MIN_EMIN)
        units_digits = Decimal(abs(value.numerator) // value.denominator).adjusted() + 1
        context.prec = max(context.prec, units_digits + 1)
        return numerator / denominator


def review_amount(amount: Decimal, exact: Fraction | None) -> tuple[Decimal, Fraction | None]:
    """An amount and its exact rational (#1241, #1243).

    ``amount`` where ``exact`` is ``None``, else :func:`exact_decimal` of
    ``exact`` at the caller's precision, with ``exact`` kept only where that
    Decimal can't hold it.
    """
    if exact is None:
        return amount, None
    decimal = exact_decimal(exact)
    return decimal, None if Fraction(decimal) == exact else exact


def checked_exact_product(amount: Fraction, multiplier: Decimal, *, at: str) -> Decimal:
    """:func:`exact_decimal` of the exact ``amount * multiplier`` (#1243).

    Refuses as :func:`checked_product` does where ``multiplier`` has no
    :func:`exact_fraction`; ``at`` must be a fixed label, never input text.
    """
    exact = exact_fraction(multiplier)
    if exact is None:
        raise invariant_error(what=f"{at}{INEXACT_PRODUCT_REFUSAL}")
    return exact_decimal(amount * exact)


def exact_whole_rupees(value: Fraction) -> Decimal:
    """``value`` rounded HALF_UP to whole rupees, as :func:`~ibkr_fa.models.money.to_whole_rupees`
    rounds a Decimal: half away from zero, and never ``-0`` (#1177, #1241).

    For ``value = n/d >= 0`` it is ``(2n + d) // (2d)``; a negative value is
    the negated rounding of its magnitude.
    """
    whole = (2 * abs(value.numerator) + value.denominator) // (2 * value.denominator)
    return Decimal(-whole if value < 0 else whole)


def is_exact_decimal(value: Decimal, exact: Fraction) -> bool:
    """Whether ``value`` is :func:`exact_decimal` of ``exact`` at some precision.

    Repeat the division at ``value``'s coefficient precision. A generated
    cut can extend beyond the source operand's decimal-place limit, so that
    limit must not be applied again here.
    """
    if not value.is_finite():
        return False
    with localcontext() as context:
        context.prec = len(value.as_tuple().digits)
        return exact_decimal(exact) == value


def checked_exact_sum(
    terms: Iterable[tuple[int, Decimal, Fraction | None]],
    *,
    at: Callable[[int], str],
    exact: bool,
) -> tuple[Decimal, Fraction | None]:
    """Add ``(position, amount, rational)`` terms: their :func:`checked_sum`, and,
    when ``exact``, the exact sum (#1241).

    A term's rational is its exact value where ``amount`` rounds it, else
    ``None``. Each refusal is :func:`checked_sum`'s, so a term's amount still
    adds exactly as it always has. The exact sum adds each term's rational, or
    the :func:`exact_fraction` of its amount; one that has none refuses at
    ``at(position)``, as an inexact sum does. Without ``exact``, no term may
    carry a rational.
    """
    materialised = tuple(terms)
    total = checked_sum(((position, amount) for position, amount, _ in materialised), at=at)
    if not exact:
        require(
            all(rational is None for *_, rational in materialised),
            what="checked_exact_sum: a term's exact rational would be dropped",
        )
        return total, None
    exact_total = Fraction()
    for position, amount, rational in materialised:
        value = exact_fraction(amount) if rational is None else rational
        if value is None:
            raise invariant_error(what=f"{at(position)}{_INEXACT_SUM_REFUSAL}")
        exact_total += value
    return total, exact_total


def exact_inr_conversion(
    amount: Decimal, rate: Decimal, *, label: str, source_row_index: int | None
) -> Decimal:
    """Return ``amount * rate``, refusing a product that loses a non-zero digit.

    The product runs at :data:`EXACT_SUM_DIGITS` digits, or the active
    context's precision if that is more, as :func:`exact_product`'s does
    (#1312), so it keeps every digit up to that cap. It refuses a nonfinite
    operand or product, a decimal arithmetic failure, or a product that would
    still lose a non-zero digit. A product that only drops trailing zeros
    signals ``Rounded`` without ``Inexact`` and keeps its exact value, so it
    passes. The refusal names ``source_row_index`` when the input has one.
    """
    product, exact = _exact(lambda: amount * rate, lost=Inexact)
    row = {} if source_row_index is None else {"source_row_index": source_row_index}
    require(
        exact,
        what=(
            f"{label} INR amount (column Amount \N{MULTIPLICATION SIGN} FX rate) "
            "must be exact within Decimal precision"
        ),
        **row,
    )
    return product
