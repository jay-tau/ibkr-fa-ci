"""Smoke test: package imports cleanly. Replace with real tests as features land."""

import ibkr_fa


def test_package_imports() -> None:
    assert ibkr_fa.__doc__ is not None
