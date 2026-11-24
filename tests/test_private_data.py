"""Tests for the private-data root helper (#838)."""

from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

from tests import _private_data
from tests._private_data import (
    PRIVATE_DATA_ENV,
    PrivateDataRootError,
    private_data_root,
    private_dataset,
    private_pdfs,
)

_REPO_ROOT = Path(__file__).resolve().parents[1]
_UNREADABLE = pytest.param(
    "unreadable",
    marks=pytest.mark.skipif(
        os.name != "posix" or os.geteuid() == 0,
        reason="needs a POSIX user that mode 000 locks out",
    ),
)


@pytest.fixture
def default_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point the default root at a tmp path (not created) and unset the variable."""
    default = tmp_path / "default"
    monkeypatch.setattr(_private_data, "_DEFAULT_ROOT", default)
    monkeypatch.delenv(PRIVATE_DATA_ENV, raising=False)
    return default


@pytest.fixture
def private_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Select an empty tmp root through the variable, from an unrelated cwd."""
    root = tmp_path / "private"
    root.mkdir()
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.setenv(PRIVATE_DATA_ENV, str(root))
    monkeypatch.chdir(elsewhere)
    return root


def _not_a_readable_directory(kind: str, tmp_path: Path, request: pytest.FixtureRequest) -> Path:
    path = tmp_path / kind
    if kind == "file":
        path.write_text("", encoding="utf-8")
    elif kind == "broken-symlink":
        path.symlink_to(tmp_path / "absent")
    elif kind == "unreadable":
        path.mkdir()
        path.chmod(0)
        request.addfinalizer(lambda: path.chmod(0o700))
    return path


# ---- root selection ---------------------------------------------------------


def test_unset_variable_without_default_selects_no_root(default_root: Path) -> None:
    assert private_data_root() is None


def test_unset_variable_selects_the_default(default_root: Path) -> None:
    default_root.mkdir()
    assert private_data_root() == default_root


def test_variable_selects_its_directory_whatever_the_cwd(
    default_root: Path, private_root: Path
) -> None:
    default_root.mkdir()
    assert private_data_root() == private_root


@pytest.mark.parametrize("value", ["", "   ", "relative/private-data"])
def test_variable_refuses_a_blank_or_relative_value(
    default_root: Path, monkeypatch: pytest.MonkeyPatch, value: str
) -> None:
    default_root.mkdir()
    monkeypatch.setenv(PRIVATE_DATA_ENV, value)
    with pytest.raises(PrivateDataRootError, match=PRIVATE_DATA_ENV):
        private_data_root()


@pytest.mark.parametrize("kind", ["missing", "file", "broken-symlink", _UNREADABLE])
def test_variable_refuses_a_path_that_is_not_a_readable_directory(
    default_root: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    request: pytest.FixtureRequest,
    kind: str,
) -> None:
    default_root.mkdir()
    monkeypatch.setenv(PRIVATE_DATA_ENV, str(_not_a_readable_directory(kind, tmp_path, request)))
    with pytest.raises(PrivateDataRootError, match=PRIVATE_DATA_ENV):
        private_data_root()


@pytest.mark.parametrize("kind", ["file", "broken-symlink", _UNREADABLE])
def test_default_refuses_a_path_that_is_not_a_readable_directory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    request: pytest.FixtureRequest,
    kind: str,
) -> None:
    monkeypatch.setattr(
        _private_data, "_DEFAULT_ROOT", _not_a_readable_directory(kind, tmp_path, request)
    )
    monkeypatch.delenv(PRIVATE_DATA_ENV, raising=False)
    with pytest.raises(PrivateDataRootError, match="default"):
        private_data_root()


@pytest.mark.skipif(
    os.name != "posix" or os.geteuid() == 0, reason="needs a POSIX user that mode 000 locks out"
)
@pytest.mark.parametrize("use_default", [False, True])
def test_root_lookup_errors_report_an_unreadable_root(
    default_root: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    request: pytest.FixtureRequest,
    use_default: bool,
) -> None:
    root = _not_a_readable_directory("unreadable", tmp_path, request) / "private"
    if use_default:
        monkeypatch.setattr(_private_data, "_DEFAULT_ROOT", root)
    else:
        monkeypatch.setenv(PRIVATE_DATA_ENV, str(root))
    with pytest.raises(PrivateDataRootError, match="can't be read"):
        private_data_root()


@pytest.mark.parametrize("value", ["", "relative/private-data"])
def test_bad_variable_fails_the_session_before_any_test(value: str) -> None:
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "-q",
            "-p",
            "no:cacheprovider",
            "--no-cov",
            "tests/test_private_data.py::test_unset_variable_without_default_selects_no_root",
        ],
        cwd=_REPO_ROOT,
        env={
            **{name: v for name, v in os.environ.items() if name != "PYTEST_ADDOPTS"},
            PRIVATE_DATA_ENV: value,
        },
        capture_output=True,
        text=True,
        check=False,
        timeout=120,
    )
    assert result.returncode == pytest.ExitCode.USAGE_ERROR
    assert PRIVATE_DATA_ENV in result.stderr
    assert "passed" not in result.stdout


# ---- datasets ---------------------------------------------------------------


def test_dataset_skips_without_a_root(default_root: Path) -> None:
    with pytest.raises(pytest.skip.Exception, match="unavailable"):
        private_dataset("a.csv")


def test_dataset_skips_when_every_part_is_absent(private_root: Path) -> None:
    with pytest.raises(pytest.skip.Exception, match="unavailable"):
        private_dataset("a.csv", "b/c.csv")


def test_dataset_fails_when_only_some_parts_are_present(private_root: Path) -> None:
    (private_root / "a.csv").write_text("", encoding="utf-8")
    with pytest.raises(
        (pytest.fail.Exception, pytest.skip.Exception),
        match=re.escape("a.csv, b/c.csv is incomplete; missing b/c.csv"),
    ) as caught:
        private_dataset("a.csv", "b/c.csv")
    assert caught.type is pytest.fail.Exception


def test_dataset_resolves_every_part_under_the_root(private_root: Path) -> None:
    (private_root / "a.csv").write_text("", encoding="utf-8")
    (private_root / "b").mkdir()
    (private_root / "b" / "c.csv").write_text("", encoding="utf-8")
    assert private_dataset("a.csv", "b/c.csv") == (
        private_root / "a.csv",
        private_root / "b" / "c.csv",
    )


def test_dataset_counts_a_broken_symlink_as_present(private_root: Path) -> None:
    (private_root / "a.csv").symlink_to(private_root / "absent.csv")
    with pytest.raises(
        (pytest.fail.Exception, pytest.skip.Exception),
        match=re.escape("a.csv, b.csv is incomplete; missing b.csv"),
    ) as caught:
        private_dataset("a.csv", "b.csv")
    assert caught.type is pytest.fail.Exception


def test_dataset_fails_when_a_folder_on_the_path_is_not_a_directory(private_root: Path) -> None:
    (private_root / "a").write_text("", encoding="utf-8")
    (private_root / "b").symlink_to(private_root / "absent")
    for part in ("a/x.csv", "b/x.csv"):
        with pytest.raises(
            (pytest.fail.Exception, pytest.skip.Exception),
            match=re.escape(f"private dataset part {part} "),
        ) as caught:
            private_dataset(part)
        assert caught.type is pytest.fail.Exception


@pytest.mark.skipif(
    os.name != "posix" or os.geteuid() == 0, reason="needs a POSIX user that mode 000 locks out"
)
@pytest.mark.parametrize("mode", [0, 0o100])
def test_dataset_fails_when_a_folder_on_the_path_cannot_be_read(
    private_root: Path, request: pytest.FixtureRequest, mode: int
) -> None:
    locked = _not_a_readable_directory("unreadable", private_root, request)
    locked.chmod(mode)
    with pytest.raises(
        (pytest.fail.Exception, pytest.skip.Exception),
        match=re.escape("part unreadable/x.csv can't be checked"),
    ) as caught:
        private_dataset(f"{locked.name}/x.csv")
    assert caught.type is pytest.fail.Exception


# ---- private PDF trees ------------------------------------------------------


def test_private_pdfs_lists_every_pdf_sorted_without_following_folder_links(
    private_root: Path,
) -> None:
    (private_root / "b").mkdir()
    (private_root / "b" / "2.pdf").write_text("", encoding="utf-8")
    (private_root / "a.pdf").write_text("", encoding="utf-8")
    (private_root / "a.txt").write_text("", encoding="utf-8")
    (private_root / "linked").symlink_to(private_root / "b")
    assert private_pdfs(private_root) == [private_root / "a.pdf", private_root / "b" / "2.pdf"]


@pytest.mark.skipif(
    os.name != "posix" or os.geteuid() == 0, reason="needs a POSIX user that mode 000 locks out"
)
def test_private_pdfs_fails_when_a_folder_cannot_be_read(
    private_root: Path, request: pytest.FixtureRequest
) -> None:
    (private_root / "a.pdf").write_text("", encoding="utf-8")
    _not_a_readable_directory("unreadable", private_root, request)
    with pytest.raises(pytest.fail.Exception, match="can't be read"):
        private_pdfs(private_root)
