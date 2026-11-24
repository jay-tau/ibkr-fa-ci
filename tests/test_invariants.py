"""Tests for :mod:`ibkr_fa.invariants` and :class:`InvariantError`."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from ibkr_fa.exceptions import IbkrFaError, InvariantError
from ibkr_fa.invariants import invariant_error, require
from tests._crash_reports import assert_prints_no_marker

# ---- require(): true / false branch behaviour ---------------------------


def test_require_true_returns_none() -> None:
    """The happy path: a true invariant returns ``None`` and does
    not raise. This is the common case at every call site."""
    assert require(True, what="anything") is None
    assert require(1 + 1 == 2, what="self-evident truth") is None


def test_require_false_raises_invariant_error() -> None:
    """The unhappy path: a false invariant raises
    :class:`InvariantError`. No silent pass-through."""
    with pytest.raises(InvariantError):
        require(False, what="this never holds")


def test_require_falsy_non_bool_also_raises() -> None:
    """``cond`` is a generic ``bool``-coercible parameter; an empty
    list / zero / None should also trigger the raise. Verifies the
    truthiness-test semantics match Python's ``if`` convention."""
    for falsy in (False, 0, "", [], {}, None):
        with pytest.raises(InvariantError):
            require(bool(falsy), what="must be truthy")


# ---- Structured context -------------------------------------------------


def test_require_passes_what_into_invariant_error() -> None:
    """``what`` round-trips into :attr:`InvariantError.what` verbatim
    so downstream renderers can show it without parsing strings."""
    with pytest.raises(InvariantError) as exc:
        require(False, what="quantity must match Side")
    assert exc.value.what == "quantity must match Side"


def test_require_no_context_yields_empty_tuple() -> None:
    """When no kwargs are passed, the context tuple is empty —
    not ``None``, not an empty dict (would break the frozen
    dataclass's hashability)."""
    with pytest.raises(InvariantError) as exc:
        require(False, what="bare")
    assert exc.value.context == ()


def test_require_kwargs_become_context_keys_in_call_order() -> None:
    """Context kwargs become an immutable tuple of ``(key, None)`` pairs
    in call order (#1252). Order preservation matters for readable
    error messages (insertion order matches the way the developer
    listed operands at the call site)."""
    with pytest.raises(InvariantError) as exc:
        require(False, what="y gt x", y=2, x=1)
    assert exc.value.context == (("y", None), ("x", None))


class _Marker:
    """A value whose text is a marker, and whose ``str`` and ``repr`` the
    error must never need."""

    def __str__(self) -> str:
        return "Synthetic Context Marker"

    __repr__ = __str__


@pytest.mark.parametrize(
    "value",
    [
        "Synthetic Holder Two",
        Decimal("1384.215067"),
        date(2031, 7, 19),
        1384215067,
        _Marker(),
    ],
    ids=["str", "Decimal", "date", "int", "custom"],
)
def test_require_keeps_no_context_value(value: object) -> None:
    """No context value reaches the error (#1252): not ``context``, any
    other field, args, ``str`` or ``repr``. An ``int`` under a key that
    isn't a position key is a value too."""
    with pytest.raises(InvariantError) as exc:
        require(False, what="synthetic check", value=value)
    assert_prints_no_marker(exc.value, markers=(str(value),))
    assert exc.value.context == (("value", None),)
    assert exc.value.args == ()


def test_require_never_formats_a_context_value() -> None:
    """Building the error doesn't call ``str`` or ``repr`` on a value, so
    one whose text can't be built still refuses with its key."""

    class Unprintable:
        def __str__(self) -> str:
            raise AssertionError("str called")

        __repr__ = __str__

    with pytest.raises(InvariantError) as exc:
        require(False, what="synthetic check", value=Unprintable())
    assert str(exc.value) == "InvariantError: synthetic check (context keys: value)"


# ---- InvariantError rendering ------------------------------------------


def test_invariant_error_str_with_no_context() -> None:
    """The bare form: just the ``what`` after the prefix."""
    err = InvariantError(what="trivial", context=())
    assert str(err) == "InvariantError: trivial"


def test_invariant_error_str_with_context() -> None:
    """Rendered form: ``what`` plus the context keys, never their values."""
    err = InvariantError(what="x must lt y", context=(("x", None), ("y", None)))
    assert str(err) == "InvariantError: x must lt y (context keys: x, y)"


def test_invariant_error_str_via_require_call_site() -> None:
    """End-to-end render: build via :func:`require`, then ``str()``."""
    with pytest.raises(InvariantError) as exc:
        require(False, what="positive quantity", quantity=Decimal("-5"))
    assert str(exc.value) == "InvariantError: positive quantity (context keys: quantity)"
    assert exc.value.context == (("quantity", None),)


# Synthetic input values; the call names them by constant, so the rendered
# traceback's source lines can't hold them.
_VALUES: dict[str, object] = {
    "a": "Synthetic Holder Two",
    "b": "U1234567",
    "c": "ANN",
    "d": Decimal("1234567.89"),
    "e": date(2031, 7, 19),
}


def test_invariant_error_keeps_no_context_value() -> None:
    """Context values can be input (#1046, #1252): the message names only
    the keys, in call order, and the error keeps none of the values."""
    with pytest.raises(InvariantError) as exc:
        require(False, what="synthetic check", **_VALUES)
    assert str(exc.value) == "InvariantError: synthetic check (context keys: a, b, c, d, e)"
    assert_prints_no_marker(exc.value, markers=tuple(str(value) for value in _VALUES.values()))
    assert exc.value.context == (("a", None), ("b", None), ("c", None), ("d", None), ("e", None))


@pytest.mark.parametrize(
    "key", ["line", "file_line", "header_line", "row_index", "source_row_index", "page"]
)
def test_invariant_error_str_prints_an_int_position(key: str) -> None:
    """A position the code counted stays findable: an ``int`` under a
    position key stays on the error and renders as ``key=value``, among
    the other keys."""
    with pytest.raises(InvariantError) as exc:
        require(False, what="bad row", isin="Synthetic Holder Two", **{key: 12})
    assert str(exc.value) == f"InvariantError: bad row (context keys: isin, {key}=12)"
    assert exc.value.context == (("isin", None), (key, 12))


@pytest.mark.parametrize("key", ["line", "row_index"])
@pytest.mark.parametrize("value", ["1234567", Decimal("1234567"), True], ids=repr)
def test_invariant_error_keeps_only_the_key_of_a_position_that_is_not_an_int(
    key: str, value: object
) -> None:
    """A string, ``Decimal`` or ``bool`` (an ``int`` subclass) under a
    position key could be a cell value, so the error keeps the key alone."""
    with pytest.raises(InvariantError) as exc:
        require(False, what="bad row", **{key: value})
    assert str(exc.value) == f"InvariantError: bad row (context keys: {key})"
    assert_prints_no_marker(exc.value, markers=("1234567",) if value is not True else ("True",))
    assert exc.value.context == ((key, None),)


def test_invariant_error_keeps_only_the_key_of_an_int_under_another_key() -> None:
    count = int("1234567")
    with pytest.raises(InvariantError) as exc:
        require(False, what="bad count", count=count)
    assert str(exc.value) == "InvariantError: bad count (context keys: count)"
    assert_prints_no_marker(exc.value, markers=(str(count),))
    assert exc.value.context == (("count", None),)


def test_invariant_error_str_via_builder() -> None:
    """An error built by ``invariant_error`` renders a kept position as ``key=value``
    and any other key alone."""
    err = invariant_error(what="bad row", isin="Synthetic Holder Two", line=12)
    assert str(err) == "InvariantError: bad row (context keys: isin, line=12)"


# ---- Exception-hierarchy integration -----------------------------------


def test_invariant_error_is_ibkr_fa_error() -> None:
    """The CLI catches :class:`IbkrFaError` and maps to exit-code 4.
    :class:`InvariantError` MUST be a subclass so the same catch
    path covers it; otherwise an invariant failure would surface
    as a bare exception trace + exit 1."""
    with pytest.raises(IbkrFaError):
        require(False, what="any")


def test_invariant_error_is_frozen() -> None:
    """The error carries no in-place mutation surface; once raised,
    its fields cannot be tampered with. Same convention as other
    error types in :mod:`ibkr_fa.exceptions`."""
    err = InvariantError(what="x", context=())
    with pytest.raises((AttributeError, TypeError)):
        err.what = "modified"  # ty: ignore[invalid-assignment]
