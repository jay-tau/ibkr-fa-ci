"""Guard: the maintainer's real name stays out of every tracked file but the
two authorship lines (#1030).

The name is public (``pyproject.toml``, the README copyright line, git
history), so this guard is about hygiene, not secrecy: a real person in a
fixture blurs synthetic and real data. Test and fixture account holders use
the synthetic name ``Sample Holder`` instead. The guard reads the name from
``[project].authors`` rather than spelling it.
"""

from __future__ import annotations

import re
import subprocess
import tomllib
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[1]
_PYPROJECT = "pyproject.toml"
_README = "README.md"


def _maintainer_name() -> str:
    project = tomllib.loads((_REPO_ROOT / _PYPROJECT).read_text(encoding="utf-8"))["project"]
    [author] = project["authors"]
    name = author["name"]
    assert isinstance(name, str)
    assert len(name.split()) >= 2
    return name


def _name_pattern(name: str) -> re.Pattern[str]:
    # Case-insensitive; punctuation or whitespace may separate the parts, so
    # "NAME,SURNAME", "name_surname" and a line-wrapped name all match.
    return re.compile(r"[\W_]*".join(re.escape(part) for part in name.split()), re.IGNORECASE)


def _is_authorship_line(path: str, line: str, name: str) -> bool:
    if path == _PYPROJECT:
        return re.fullmatch(rf'\s*\{{\s*name\s*=\s*"{re.escape(name)}"\s*,.*', line) is not None
    if path == _README:
        return (
            re.fullmatch(rf"Copyright \(C\) \d{{4}}(-\d{{4}})? {re.escape(name)}", line) is not None
        )
    return False


def _name_hits(path: str, text: str, name: str) -> list[str]:
    """Each ``path:line`` holding the name, except one authorship line per file."""
    lines = text.splitlines()
    hits: list[str] = []
    allowed_used = False
    for match in _name_pattern(name).finditer(text):
        start = text.count("\n", 0, match.start())
        end = text.count("\n", 0, match.end())
        if (
            not allowed_used
            and start == end
            and start < len(lines)
            and _is_authorship_line(path, lines[start], name)
        ):
            allowed_used = True
            continue
        hits.append(f"{path}:{start + 1}")
    return hits


def test_maintainer_name_appears_only_in_the_authorship_lines() -> None:
    name = _maintainer_name()
    listed = subprocess.run(
        ["git", "ls-files", "-z"],
        cwd=_REPO_ROOT,
        capture_output=True,
        check=True,
    ).stdout.decode()
    paths = [path for path in listed.split("\0") if path]
    assert {_PYPROJECT, _README, "tests/conftest.py"} <= set(paths)
    assert any(path.startswith("tests/fixtures/") for path in paths)
    hits = [
        hit
        for path in paths
        for hit in _name_hits(
            path,
            (_REPO_ROOT / path).read_bytes().decode("utf-8", errors="replace"),
            name,
        )
    ]
    assert hits == []


@pytest.mark.parametrize(
    ("path", "template"),
    [
        ("tests/test_x.py", 'holder_name = "{name}"\n'),
        ("tests/test_x.py", 'holder_name = "{upper}"\n'),
        ("tests/fixtures/e2e_x.csv", "Account Information,Data,Name,{name},,\n"),
        ("tests/fixtures/e2e_x.csv", "Account Information,Data,Name,{first}\n  {last}\n"),
        ("docs/x.md", "Written by {first}_{last}.\n"),
        ("README.md", "Maintained by {name}.\n"),
        ("README.md", "Copyright (C) 2026 {name} and others\n"),
        ("pyproject.toml", 'description = "by {name}"\n'),
        ("pyproject.toml", 'maintainers = [{{ name = "{name}" }}]\n'),
    ],
)
def test_guard_refuses_the_name_outside_the_authorship_lines(path: str, template: str) -> None:
    name = _maintainer_name()
    first, last = name.split()[0], name.split()[-1]
    text = template.format(name=name, upper=name.upper(), first=first, last=last)
    assert _name_hits(path, text, name) == [f"{path}:1"]


def test_guard_refuses_a_second_authorship_line_in_the_same_file() -> None:
    name = _maintainer_name()
    pyproject = (_REPO_ROOT / _PYPROJECT).read_text(encoding="utf-8")
    assert _name_hits(_PYPROJECT, pyproject, name) == []
    extra = pyproject + f'\n[tool.x]\nauthors = [\n    {{ name = "{name}", email = "x" }}\n]\n'
    assert len(_name_hits(_PYPROJECT, extra, name)) == 1
    readme = f"Copyright (C) 2026 {name}\n"
    assert _name_hits(_README, readme, name) == []
    assert _name_hits(_README, readme + readme, name) == [f"{_README}:2"]
