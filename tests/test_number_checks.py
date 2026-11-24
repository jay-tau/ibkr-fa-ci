"""Read-time digit limits (#1275): :func:`number_limit_breach` at each kind's edges."""

from __future__ import annotations

import ast
from decimal import Context, Decimal, Inexact, InvalidOperation, Rounded, localcontext
from pathlib import Path

import pytest

from ibkr_fa import number_checks
from ibkr_fa.number_checks import NumberKind, number_limit_breach
from tests import _number_limits

# (kind, its literal refusal, whole digits, decimal places)
_KINDS = (
    (NumberKind.QUANTITY, _number_limits.QUANTITY, 12, 20),
    (NumberKind.MONEY, _number_limits.MONEY, 15, 20),
    (NumberKind.PRICE, _number_limits.PRICE, 9, 20),
    (NumberKind.RATE, _number_limits.RATE, 3, 6),
    (NumberKind.FX_RATE, _number_limits.FX_RATE, 3, 4),
)
_IDS = [kind.name for kind, *_ in _KINDS]


def _edge_values(whole: int, places: int) -> tuple[list[str], list[str]]:
    """Texts that fit each kind, and texts one digit past a limit."""
    fitting = [
        "9" * whole,
        "-" + "9" * whole,
        "0." + "0" * (places - 1) + "1",
        "-0." + "0" * (places - 1) + "1",
        "9" * whole + "." + "9" * places,
        "-" + "9" * whole + "." + "9" * places,
        f"1E+{whole - 1}",
        f"-1E+{whole - 1}",
        f"0E+{whole - 1}",
        f"0E-{places}",
        f"1E-{places}",
        "0",
        "-0",
    ]
    past = [
        "1" + "0" * whole,
        "-1" + "0" * whole,
        "0." + "0" * places + "1",
        "-0." + "0" * places + "1",
        "1" + "0" * whole + "." + "0" * places,
        "9" * whole + "." + "9" * (places + 1),
        f"1E+{whole}",
        f"-1E+{whole}",
        f"0E+{whole}",
        f"0E-{places + 1}",
        f"1E-{places + 1}",
        f"-1E-{places + 1}",
    ]
    return fitting, past


@pytest.mark.parametrize(("kind", "refusal", "whole", "places"), _KINDS, ids=_IDS)
def test_exactly_the_limits_pass_and_one_more_digit_refuses(
    kind: NumberKind, refusal: str, whole: int, places: int
) -> None:
    """Exactly ``whole`` digits before the point and ``places`` after it pass;
    one more of either refuses with the kind's one fixed text. Signs, ``1E+N``,
    ``0E+N`` and ``0E-N`` are measured on the parsed value as it stands."""
    fitting, past = _edge_values(whole, places)
    for text in fitting:
        assert number_limit_breach(Decimal(text), kind) is None, text
    for text in past:
        assert number_limit_breach(Decimal(text), kind) == refusal, text


def test_each_kind_has_its_issued_limits() -> None:
    """The limit table as issued: quantity 12/20, money 15/20, unit price 9/20,
    rate 3/6, FX rate 3/4, each with its fixed refusal."""
    for kind, refusal, whole, places in _KINDS:
        assert (kind.whole_digits, kind.decimal_places, kind.refusal) == (whole, places, refusal)
    assert len(NumberKind) == len(_KINDS)


@pytest.mark.parametrize("text", ["NaN", "-NaN", "sNaN", "Infinity", "-Infinity"])
@pytest.mark.parametrize(("kind", "refusal", "whole", "places"), _KINDS, ids=_IDS)
def test_a_non_finite_value_never_fits(
    kind: NumberKind, refusal: str, whole: int, places: int, text: str
) -> None:
    assert number_limit_breach(Decimal(text), kind) == refusal


@pytest.mark.parametrize(("kind", "refusal", "whole", "places"), _KINDS, ids=_IDS)
def test_the_check_ignores_the_decimal_context(
    kind: NumberKind, refusal: str, whole: int, places: int
) -> None:
    """Under a one-digit context that traps rounding, the check neither raises
    nor changes its answer: it reads the value's digits and does no arithmetic."""
    fitting, past = _edge_values(whole, places)
    hostile = Context(prec=1, Emax=1, Emin=-1, traps=[Inexact, Rounded, InvalidOperation])
    with localcontext(hostile):
        for text in fitting:
            assert number_limit_breach(Decimal(text), kind) is None, text
        for text in past:
            assert number_limit_breach(Decimal(text), kind) == refusal, text


def test_the_refusal_names_no_value() -> None:
    """The text is the kind's alone: two different breaching values with a
    digit-run marker each get the same text, which holds neither marker."""
    for kind, refusal, whole, _places in _KINDS:
        first = Decimal("73915" + "0" * whole)
        second = Decimal("0." + "0" * 30 + "84026")
        assert number_limit_breach(first, kind) == number_limit_breach(second, kind) == refusal
        assert "73915" not in refusal
        assert "84026" not in refusal


def test_the_module_imports_nothing_from_ibkr_fa() -> None:
    """Every layer can call it, as with ``text_checks``."""
    tree = ast.parse(Path(number_checks.__file__).read_text(encoding="utf-8"))
    nodes = list(ast.walk(tree))
    imported = {
        alias.name for node in nodes if isinstance(node, ast.Import) for alias in node.names
    } | {node.module or "" for node in nodes if isinstance(node, ast.ImportFrom)}
    assert imported == {"__future__", "decimal", "enum"}
