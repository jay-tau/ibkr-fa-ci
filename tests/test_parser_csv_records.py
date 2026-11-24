"""Every CSV reader under ``src/ibkr_fa/parser/`` reads strictly (#1098).

The default ``csv`` dialect repairs bad quoting instead of refusing it, so a
cut-short or edited broker file could parse as complete.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

_PARSER = Path(__file__).resolve().parents[1] / "src" / "ibkr_fa" / "parser"
_READERS = frozenset({"reader", "DictReader"})


def _unstrict_readers(source: str) -> list[int]:
    """Lines of ``csv.reader`` / ``csv.DictReader`` calls in ``source`` that
    don't pass ``strict=True``, with direct or aliased imports."""
    tree = ast.parse(source)
    modules = {"csv"}
    readers: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules |= {alias.asname or alias.name for alias in node.names if alias.name == "csv"}
        elif isinstance(node, ast.ImportFrom) and node.module == "csv":
            readers |= {
                alias.asname or alias.name for alias in node.names if alias.name in _READERS
            }
    lines = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        is_reader = (
            isinstance(func, ast.Attribute)
            and func.attr in _READERS
            and isinstance(func.value, ast.Name)
            and func.value.id in modules
        ) or (isinstance(func, ast.Name) and func.id in readers)
        strict = any(
            keyword.arg == "strict"
            and isinstance(keyword.value, ast.Constant)
            and keyword.value.value is True
            for keyword in node.keywords
        )
        if is_reader and not strict:
            lines.append(node.lineno)
    return lines


def test_every_parser_csv_reader_is_strict() -> None:
    paths = sorted(_PARSER.rglob("*.py"))
    assert paths, "no parser modules found"
    unstrict = [
        f"{path.relative_to(_PARSER)}:{line}"
        for path in paths
        for line in _unstrict_readers(path.read_text(encoding="utf-8"))
    ]
    assert unstrict == []


@pytest.mark.parametrize(
    "source",
    [
        "import csv\ncsv.reader(f)\n",
        "import csv\ncsv.reader(f, strict=False)\n",
        "import csv\ncsv.DictReader(f, **options)\n",
        "import csv as c\nc.reader(f)\n",
        "from csv import reader\nreader(f)\n",
        "from csv import DictReader as R\nR(f)\n",
    ],
)
def test_the_check_finds_a_reader_that_isnt_strict(source: str) -> None:
    assert _unstrict_readers(source) == [2]


def test_the_check_accepts_a_strict_reader() -> None:
    assert _unstrict_readers("import csv\ncsv.reader(f, strict=True)\n") == []
