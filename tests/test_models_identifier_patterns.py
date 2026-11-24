"""Model identifier checks match the whole value (#835).

``$`` also matches just before a final line break, so ``.match()``
with a ``$``-anchored pattern accepted ``"US99992A1097\\n"`` as an
ISIN. Checks under ``models/`` use ``fullmatch`` instead.
"""

from __future__ import annotations

import ast
from pathlib import Path

import ibkr_fa

_MODELS_ROOT = Path(ibkr_fa.__file__).parent / "models"


def test_no_model_applies_a_pattern_with_match() -> None:
    offenders = [
        f"{path.relative_to(_MODELS_ROOT).as_posix()}:{node.lineno}"
        for path in sorted(_MODELS_ROOT.rglob("*.py"))
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8")))
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "match"
    ]
    assert offenders == []
