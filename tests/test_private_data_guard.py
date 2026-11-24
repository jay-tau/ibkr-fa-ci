"""Guard: no test or script but ``tests/_private_data.py`` builds the
private-data root itself (#838)."""

from __future__ import annotations

import ast
import re
import subprocess
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[1]
_HELPER = "tests/_private_data.py"
_WHOLE_PATH = re.compile(r"[^\s\"'()]+")
_URL = re.compile(r"\S+://\S+")
_SHELL_DATA = re.compile(r"\bdata\b")


def _is_path_call_or_join(parent: ast.AST) -> bool:
    if isinstance(parent, ast.BinOp):
        return isinstance(parent.op, ast.Div)
    if not isinstance(parent, ast.Call):
        return False
    func = parent.func
    name = func.id if isinstance(func, ast.Name) else getattr(func, "attr", "")
    return name.endswith("Path") or name in {"joinpath", "join"}


def _builds_data_path(value: str, parent: ast.AST) -> bool:
    if value in {"data", "./data"}:
        return _is_path_call_or_join(parent)
    return (
        "://" not in value
        and _WHOLE_PATH.fullmatch(value) is not None
        and "data" in value.split("/")
    )


def _root_constructions(name: str, source: str) -> list[int]:
    """Return the lines of ``source`` that build a path under ``data/``.

    Python is read as code: a string that is a whole path with a ``data``
    component (``data/x``, ``../data/x``, or the ``/data/x`` part of an
    f-string), or a bare ``"data"`` joined with ``/`` or passed to a
    ``*Path``, ``joinpath`` or ``join`` call. Other scripts are read as
    text: any non-comment line naming ``data`` outside a URL."""
    if not name.endswith(".py"):
        return [
            number
            for number, line in enumerate(source.splitlines(), start=1)
            if not line.lstrip().startswith("#") and _SHELL_DATA.search(_URL.sub("", line))
        ]
    return sorted(
        {
            child.lineno
            for parent in ast.walk(ast.parse(source))
            for child in ast.iter_child_nodes(parent)
            if isinstance(child, ast.Constant)
            and isinstance(child.value, str)
            and _builds_data_path(child.value, parent)
        }
    )


@pytest.mark.parametrize(
    ("name", "snippet"),
    [
        ("tests/test_x.py", 'sample = Path(__file__).parent.parent / "data" / "x.csv"\n'),
        ("tests/test_x.py", 'ROOT = Path("data/x")\n'),
        ("tests/test_x.py", 'ROOT = Path("data") / "rh"\n'),
        ("tests/test_x.py", 'ROOT = PurePosixPath("data")\n'),
        ("tests/test_x.py", 'sample = Path("../data/x.csv")\n'),
        ("tests/test_x.py", 'sample = Path("/checkout/data/x.csv")\n'),
        ("tests/test_x.py", 'sample = Path(f"{repo}/data/x.csv")\n'),
        ("tests/test_x.py", 'sample = Path(f"data/{name}")\n'),
        ("tests/test_x.py", 'sample = REPO.joinpath("data", "x.csv")\n'),
        ("scripts/x.py", 'ROOT = os.path.join(REPO, "data")\n'),
        ("scripts/x.py", 'CONFIG = REPO / "./data" / "user.toml"\n'),
        ("scripts/x.sh", "cat data/x.csv\n"),
        ("scripts/x.sh", 'ROOT="$REPO/data"\n'),
        ("scripts/x.sh", "cd data\n"),
    ],
)
def test_guard_flags_a_root_built_directly(name: str, snippet: str) -> None:
    assert _root_constructions(name, snippet) == [1]


@pytest.mark.parametrize(
    ("name", "snippet"),
    [
        ("tests/test_x.py", '[sample] = private_dataset("ibkr/x.csv")\n'),
        ("tests/test_x.py", 'sample = root / "ibkr" / "x.csv"\n'),
        ("tests/test_x.py", 'value = payload["data"]\n'),
        ("tests/test_x.py", 'payload = {"data": 1}\n'),
        ("tests/test_x.py", 'payload.pop("data")\n'),
        ("tests/test_x.py", 'URL = "https://www.sec.gov/Archives/edgar/data/9991001/f6.htm"\n'),
        ("tests/test_x.py", "# reads data/x.csv\n"),
        ("tests/test_x.py", '"""Reads ``data/x.csv``."""\n'),
        ("scripts/x.sh", "# usage: run against data/x.csv\n"),
        ("scripts/x.sh", "curl https://www.sec.gov/Archives/edgar/data/9991001/f6.htm\n"),
        ("scripts/x.sh", 'echo "metadata"\n'),
    ],
)
def test_guard_allows_helper_joins_and_unrelated_data_strings(name: str, snippet: str) -> None:
    assert _root_constructions(name, snippet) == []


def test_no_test_or_script_builds_the_private_data_root_itself() -> None:
    listed = subprocess.run(
        ["git", "ls-files", "-z", "--", "tests", "scripts"],
        cwd=_REPO_ROOT,
        capture_output=True,
        check=True,
    ).stdout.decode()
    names = [
        name
        for name in listed.split("\0")
        if name
        and name != _HELPER
        and not name.startswith("tests/fixtures/")
        and (name.startswith("scripts/") or name.endswith(".py"))
    ]
    assert "tests/conftest.py" in names
    findings = [
        f"{name}:{line}"
        for name in names
        for line in _root_constructions(name, (_REPO_ROOT / name).read_text(encoding="utf-8"))
    ]
    assert findings == []
