"""Fail-fast invariant helper.

This module provides :func:`require`, the single entry point for
runtime invariant checks throughout ``ibkr_fa``. Use it for
conditions that should NEVER hold — bugs in this codebase, or
input shapes that violate an assumption we didn't catch at the
boundary.

For **expected** input-validation failures use the documented
exception types instead (:class:`~ibkr_fa.exceptions.ParseError` for
malformed CSV rows, :class:`~ibkr_fa.exceptions.ConfigError` for
``user.toml`` problems, etc.). Those carry section / row / column
context aimed at end users; :func:`require` is aimed at developers
catching internal bugs.

Design choices:

* **Always-on.** Unlike Python ``assert``, ``require()`` cannot be
  disabled with ``python -O``. The premise of this codebase is
  "correctness is paramount"; load-bearing invariants must never be
  silently skipped.
* **Eagerly-evaluated context.** Call sites pay the cost of building
  the context dict whether the invariant fires or not. The trade-off
  is readability — every ``require()`` site reads as a single
  expression. Reconsider lazy evaluation if profiling ever shows
  hot-path cost.
* **Raises :class:`~ibkr_fa.exceptions.InvariantError`.** That class
  is a subclass of :class:`~ibkr_fa.exceptions.IbkrFaError`, which
  the CLI already maps to exit-code 4 with structured rendering. No
  CLI change is needed when new ``require()`` sites land.
* **Values stay off the error.** The context values can be input,
  such as a symbol or an amount. The error keeps only their keys, in
  call order, plus the value of a position key (such as ``line``) that
  was given as an ``int``; its message prints the same.

Usage::

    from ibkr_fa.invariants import require


    def process(value: Decimal) -> Decimal:
        require(value > 0, what="value must be positive", value=value)
        ...

If ``value <= 0``, the call raises::

    InvariantError: value must be positive (context keys: value)

For ``value=Decimal("-3.5")``, the error's ``context`` is
``(("value", None),)``: the key, never the value.
"""

from __future__ import annotations

from typing import Final

from ibkr_fa.exceptions import InvariantError

#: The context keys whose ``int`` value the error keeps and prints: positions
#: the code counted, never cell values.
_POSITION_KEYS: Final = frozenset(
    {"line", "file_line", "header_line", "row_index", "source_row_index", "page"}
)


def require(cond: bool, *, what: str, **context: object) -> None:
    """Assert a runtime invariant; raise :class:`InvariantError` on violation.

    Args:
        cond: The invariant to check. If ``True``, this function
            returns immediately. If ``False``, raises
            :class:`InvariantError`.
        what: Short, human-readable description of the invariant
            (e.g., ``"OpenLot.quantity must match Side"``). Appears
            verbatim in the rendered error message.
        **context: The operands at the point of the check. The error
            keeps their keys, in call order, and no value but an
            ``int`` given for a position key such as ``line``.

    Raises:
        InvariantError: If ``cond`` is falsy, as built by
            :func:`invariant_error`.
    """
    if cond:
        return
    raise invariant_error(what=what, **context)


def invariant_error(*, what: str, **context: object) -> InvariantError:
    """Build the :class:`InvariantError` that :func:`require` raises.

    For a caller that raises it itself, such as one chaining the
    exception it caught. The error's :attr:`InvariantError.what` is
    ``what`` and its :attr:`InvariantError.context` pairs each key, in
    call order, with its value if the key is a position key (``line``,
    ``file_line``, ``header_line``, ``row_index``, ``source_row_index``
    or ``page``) and the value an ``int`` (not a ``bool``), else with
    ``None``. No other value reaches the error.
    """
    return InvariantError(
        what=what,
        context=tuple(
            (k, v if k in _POSITION_KEYS and type(v) is int else None) for k, v in context.items()
        ),
    )
