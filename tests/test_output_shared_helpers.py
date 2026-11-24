"""Guard the shared output helpers (#605).

Only :mod:`ibkr_fa.output._atomic` stages and commits output files, apart
from the deliberate no-clobber publish in :mod:`ibkr_fa.output.cbdt_json_merge`,
and only :mod:`ibkr_fa.output._writer_contract` runs the CSV row checks, so
every CSV writer goes through ``write_checked_rows``. Reads the code with
``ast``, so docstrings that name these functions do not trip it.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

import ibkr_fa
from ibkr_fa.output import Subcommand, output_binding
from ibkr_fa.output._writer_contract import WriterContractError
from ibkr_fa.output.base import RenderContext
from ibkr_fa.output.schedules import fa

_PACKAGE_ROOT = Path(ibkr_fa.__file__).parent
_OUTPUT_DIR = _PACKAGE_ROOT / "output"
_ATOMIC_OWNERS = frozenset({"_atomic.py", "cbdt_json_merge.py"})
_ATOMIC_CALLS = frozenset({"tempfile.mkstemp", "os.replace"})
_ROW_CHECKS = frozenset({"_assert_no_nul_in_row", "_assert_no_csv_formula_injection"})


def _resolved_calls(source: str) -> list[tuple[int, str]]:
    """Return ``(line, dotted name)`` for each call, with import aliases resolved."""
    tree = ast.parse(source)
    aliases: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            aliases.update({alias.asname: alias.name for alias in node.names if alias.asname})
        elif isinstance(node, ast.ImportFrom) and node.module:
            aliases.update(
                {alias.asname or alias.name: f"{node.module}.{alias.name}" for alias in node.names}
            )
    calls = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        parts = []
        func = node.func
        while isinstance(func, ast.Attribute):
            parts.append(func.attr)
            func = func.value
        if isinstance(func, ast.Name):
            parts.append(aliases.get(func.id, func.id))
            calls.append((node.lineno, ".".join(reversed(parts))))
    return calls


def _output_modules() -> list[Path]:
    paths = sorted(_OUTPUT_DIR.rglob("*.py"))
    assert paths, f"no Python modules found under {_OUTPUT_DIR}"
    return paths


def test_only_atomic_module_stages_output_files() -> None:
    offenders = [
        f"{path.relative_to(_PACKAGE_ROOT)}:{line}: {name}"
        for path in _output_modules()
        if path.relative_to(_OUTPUT_DIR).as_posix() not in _ATOMIC_OWNERS
        for line, name in _resolved_calls(path.read_text(encoding="utf-8"))
        if name in _ATOMIC_CALLS
    ]
    assert offenders == []


def test_only_writer_contract_runs_csv_row_checks() -> None:
    offenders = [
        f"{path.relative_to(_PACKAGE_ROOT)}:{line}: {name}"
        for path in sorted(_PACKAGE_ROOT.rglob("*.py"))
        if path.relative_to(_PACKAGE_ROOT).as_posix() != "output/_writer_contract.py"
        for line, name in _resolved_calls(path.read_text(encoding="utf-8"))
        if name.rsplit(".", 1)[-1] in _ROW_CHECKS
    ]
    assert offenders == []


def test_atomic_module_owns_the_calls() -> None:
    source = (_OUTPUT_DIR / "_atomic.py").read_text(encoding="utf-8")
    assert {name for _line, name in _resolved_calls(source)} >= _ATOMIC_CALLS


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        ("import tempfile\ntempfile.mkstemp()", "tempfile.mkstemp"),
        ("import tempfile as t\nt.mkstemp()", "tempfile.mkstemp"),
        ("from tempfile import mkstemp\nmkstemp()", "tempfile.mkstemp"),
        ("import os\nos.replace(a, b)", "os.replace"),
        ("from os import replace as r\nr(a, b)", "os.replace"),
        ("def f():\n    os.replace(a, b)", "os.replace"),
        (
            "from ibkr_fa.output._writer_contract import _assert_no_nul_in_row as n\nn(r)",
            "ibkr_fa.output._writer_contract._assert_no_nul_in_row",
        ),
        (
            "from ibkr_fa.output import _writer_contract\n"
            "_writer_contract._assert_no_csv_formula_injection(r)",
            "ibkr_fa.output._writer_contract._assert_no_csv_formula_injection",
        ),
    ],
)
def test_guard_resolves_called_name(source: str, expected: str) -> None:
    assert [name for _line, name in _resolved_calls(source)] == [expected]


@pytest.mark.parametrize(
    "source",
    [
        '"""Stage via tempfile.mkstemp, commit via os.replace."""',
        '"""Checked by _assert_no_nul_in_row(row)."""',
        "# os.replace(a, b)",
        "text.replace('a', 'b')",
        "Path(a).replace(b)",
    ],
)
def test_guard_ignores_non_calls_and_other_replace(source: str) -> None:
    names = {name for _line, name in _resolved_calls(source)}
    assert not names & _ATOMIC_CALLS
    assert not {name.rsplit(".", 1)[-1] for name in names} & _ROW_CHECKS


@pytest.mark.parametrize(
    ("subcommand", "headers"),
    [(Subcommand.FA_A2, "A2_UPLOAD_HEADERS"), (Subcommand.FA, "A3_UPLOAD_HEADERS")],
)
@pytest.mark.parametrize(
    ("bad_cell", "violation"), [("=x", "formula_trigger"), ("a\x00b", "nul_byte")]
)
def test_fa_filing_headers_are_checked(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    subcommand: Subcommand,
    headers: str,
    bad_cell: str,
    violation: str,
) -> None:
    header = list(getattr(fa, headers))
    header[0] = bad_cell
    monkeypatch.setattr(fa, headers, tuple(header))
    writer = output_binding(subcommand, "filing-csv").writer(RenderContext(2026))

    with pytest.raises(WriterContractError) as raised:
        writer.write([], tmp_path / "out.csv")

    assert raised.value.violation == violation
    assert raised.value.row_index == 0
    assert raised.value.cell_index == 0
    assert list(tmp_path.iterdir()) == []
