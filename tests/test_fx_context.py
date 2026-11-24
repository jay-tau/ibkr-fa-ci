"""Tests for :mod:`ibkr_fa.fx.context` (#610)."""

from __future__ import annotations

import ast
from datetime import date
from pathlib import Path

import pytest

import ibkr_fa
from ibkr_fa.config import FxConfig
from ibkr_fa.fx import context, policy_citations
from ibkr_fa.fx.context import TaxFxContext
from ibkr_fa.fx.policy_rule115 import Rule115OtherSourcesPolicy, Rule115Policy
from ibkr_fa.fx.policy_rule115_capital_gains import Rule115CapitalGainsPolicy
from ibkr_fa.fx.policy_rule128 import Rule128Policy
from ibkr_fa.models.tax_year import FinancialYear

_HEADS = {
    "dividend": (Rule115Policy, "POLICY_KIND_RULE_115_2E"),
    "other_sources": (Rule115OtherSourcesPolicy, "POLICY_KIND_RULE_115_EXPLANATION_2C"),
    "capital_gains": (Rule115CapitalGainsPolicy, "POLICY_KIND_RULE_115_2F"),
    "wht": (Rule128Policy, "POLICY_KIND_RULE_128_5II"),
}
_POLICY_CLASS_NAMES = frozenset(cls.__name__ for cls, _attr in _HEADS.values())


@pytest.mark.parametrize("head", _HEADS)
def test_each_head_builds_its_policy_for_the_fy(head: str) -> None:
    policy_cls, citation_attr = _HEADS[head]
    ctx = TaxFxContext.for_fy(FinancialYear(2026), FxConfig(max_carry_forward_days=7))

    policy = getattr(ctx, head)

    assert type(policy) is policy_cls
    assert policy.max_carry_forward_days == 7
    assert policy.policy_kind_str() == getattr(policy_citations.for_ay(2027), citation_attr)
    assert getattr(ctx, head) is policy


def test_other_sources_uses_the_fy_end() -> None:
    policy = TaxFxContext.for_fy(FinancialYear(2025), FxConfig()).other_sources
    assert policy.rate_for_audited(date(2025, 6, 1)).specified_date == date(2026, 3, 31)


@pytest.mark.parametrize("head", _HEADS)
def test_policies_are_built_only_when_read(monkeypatch: pytest.MonkeyPatch, head: str) -> None:
    built: list[str] = []
    for name in _POLICY_CLASS_NAMES:

        def record(*_args: object, _name: str = name, **_kwargs: object) -> object:
            built.append(_name)
            return object()

        monkeypatch.setattr(context, name, record)

    ctx = TaxFxContext.for_fy(FinancialYear(2025), FxConfig())
    assert built == []
    getattr(ctx, head)
    getattr(ctx, head)
    assert built == [_HEADS[head][0].__name__]


@pytest.mark.parametrize(
    ("start_year", "suppress", "expected"),
    [
        pytest.param(1999, False, True, id="ay-2000-saved-by-section-536"),
        pytest.param(2023, False, True, id="ay-2024"),
        pytest.param(2025, False, True, id="ay-2026-last-verified"),
        pytest.param(2025, True, True, id="ay-2026-suppress-is-a-no-op"),
        pytest.param(2026, False, False, id="ay-2027-first-unverified"),
        pytest.param(2029, False, False, id="ay-2030"),
        pytest.param(2026, True, True, id="ay-2027-suppressed"),
    ],
)
def test_fx_policy_appropriate_for_ay(start_year: int, suppress: bool, expected: bool) -> None:
    ctx = TaxFxContext.for_fy(
        FinancialYear(start_year), FxConfig(), suppress_cross_ay_warning=suppress
    )
    assert ctx.fx_policy_appropriate_for_ay is expected


def _policy_constructions(source: str) -> list[str]:
    """Return ``"<line>: <call>"`` for each call of an FY-keyed policy class in ``source``."""
    names = set(_POLICY_CLASS_NAMES)
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.ImportFrom):
            names.update(
                alias.asname
                for alias in node.names
                if alias.asname and alias.name in _POLICY_CLASS_NAMES
            )
    found = []
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        name = func.id if isinstance(func, ast.Name) else getattr(func, "attr", None)
        if name in names:
            found.append(f"{node.lineno}: {ast.unparse(node)}")
    return found


def test_fy_keyed_policies_are_built_only_in_fx() -> None:
    package_root = Path(ibkr_fa.__file__).parent
    offenders = [
        f"{path.relative_to(package_root)}:{hit}"
        for path in sorted(package_root.rglob("*.py"))
        if path.parent.relative_to(package_root).parts[:1] != ("fx",)
        for hit in _policy_constructions(path.read_text(encoding="utf-8"))
    ]
    assert offenders == []


@pytest.mark.parametrize(
    ("source", "hits"),
    [
        ("Rule115Policy(max_carry_forward_days=14)", 1),
        ("fx.Rule128Policy()", 1),
        ("from ibkr_fa.fx import Rule115CapitalGainsPolicy as P\nP()", 1),
        ("def f():\n    return Rule115OtherSourcesPolicy(previous_year_end=d)", 1),
        ("from ibkr_fa.fx import Rule115OtherSourcesPolicy, Rule115Policy", 0),
        ("FaExactDatePolicy(max_carry_forward_days=14)", 0),
    ],
)
def test_construction_scan(source: str, hits: int) -> None:
    assert len(_policy_constructions(source)) == hits
