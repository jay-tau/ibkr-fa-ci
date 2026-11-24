"""``ibkr_fa.path_checks``: existence checks that keep filesystem errors (#1238).

On Python 3.14 ``Path.exists()``, ``is_dir()`` and ``is_file()`` return
``False`` for any ``OSError``; these checks raise ``EACCES`` on every version,
and answer like pathlib's for a missing entry, a file on the way, a symlink
loop and an invalid path.
"""

from __future__ import annotations

import errno
import os
from collections.abc import Callable
from pathlib import Path

import pytest

from ibkr_fa.path_checks import exists, is_dir, is_file, may_exist

_PERMISSIONS_ENFORCED = pytest.mark.skipif(
    os.name != "posix" or os.geteuid() == 0,
    reason="the platform does not enforce file permissions",
)
_CHECKS = pytest.mark.parametrize("check", [exists, is_dir, is_file])


def _layout(tmp_path: Path) -> dict[str, Path]:
    folder = tmp_path / "folder"
    folder.mkdir()
    file = tmp_path / "file.txt"
    file.write_text("x", encoding="utf-8")
    (tmp_path / "file-link").symlink_to(file)
    (tmp_path / "folder-link").symlink_to(folder)
    (tmp_path / "dangling").symlink_to(tmp_path / "gone")
    (tmp_path / "loop").symlink_to("loop")
    return {
        "folder": folder,
        "file": file,
        "file-link": tmp_path / "file-link",
        "folder-link": tmp_path / "folder-link",
        "missing": tmp_path / "missing",
        "dangling": tmp_path / "dangling",
        "loop": tmp_path / "loop",
        "under-a-file": file / "child",
        "under-a-missing-folder": tmp_path / "missing" / "child",
        "invalid": tmp_path / "bad\0path",
    }


@pytest.mark.parametrize(
    ("entry", "expected"),
    [
        ("folder", (True, True, False)),
        ("file", (True, False, True)),
        ("file-link", (True, False, True)),
        ("folder-link", (True, True, False)),
        ("missing", (False, False, False)),
        ("dangling", (False, False, False)),
        ("loop", (False, False, False)),
        ("under-a-file", (False, False, False)),
        ("under-a-missing-folder", (False, False, False)),
        ("invalid", (False, False, False)),
    ],
)
def test_checks_answer_like_pathlib_for_present_and_absent_entries(
    tmp_path: Path, entry: str, expected: tuple[bool, bool, bool]
) -> None:
    path = _layout(tmp_path)[entry]

    assert (exists(path), is_dir(path), is_file(path)) == expected
    assert (path.exists(), path.is_dir(), path.is_file()) == expected
    assert may_exist(path) is expected[0]


@_PERMISSIONS_ENFORCED
@_CHECKS
def test_an_entry_in_a_folder_that_cannot_be_searched_raises(
    tmp_path: Path, request: pytest.FixtureRequest, check: Callable[[Path], bool]
) -> None:
    blocked = tmp_path / "blocked"
    blocked.mkdir()
    path = blocked / "entry"
    path.mkdir()
    blocked.chmod(0)
    request.addfinalizer(lambda: blocked.chmod(0o700))

    with pytest.raises(PermissionError) as raised:
        check(path)

    assert type(raised.value) is PermissionError
    assert str(raised.value) == f"[Errno {errno.EACCES}] Permission denied: {str(path)!r}"


@_PERMISSIONS_ENFORCED
def test_may_exist_counts_an_entry_that_cannot_be_inspected_as_present(
    tmp_path: Path, request: pytest.FixtureRequest
) -> None:
    blocked = tmp_path / "blocked"
    blocked.mkdir()
    blocked.chmod(0)
    request.addfinalizer(lambda: blocked.chmod(0o700))

    assert may_exist(blocked / "absent-or-not") is True
