"""Output-removal failure helpers shared by the output cleanup and rollback
tests (#1875).

- ``CLEANUP_LAYERS``: the two layers a test runs a command through, the CLI and
  the entry function;
- ``STALE_BYTES``: the bytes a test writes into an earlier output;
- ``marker_permission_error``: an ``EACCES`` ``PermissionError`` whose text and
  file name are the ``OSError`` privacy markers (#1048);
- ``existing_names``: the names of the paths that exist;
- ``fail_unlink``: makes removing one path raise that error once ``armed`` is
  non-empty;
- ``make_unremovable``: replaces a target with a directory, or makes removing it
  fail;
- ``refuse_after_write``: makes a class in a module raise ``InvariantError``, and
  records which targets existed at each refusal.
"""

from __future__ import annotations

import errno
from collections.abc import Sized
from pathlib import Path
from typing import NoReturn

import pytest

from ibkr_fa.exceptions import InvariantError
from tests._oserror_markers import FILENAME_MARKER, STRERROR_MARKER

CLEANUP_LAYERS = ["cli", "entry"]
STALE_BYTES = b"STALE"


def marker_permission_error() -> PermissionError:
    """An EACCES failure whose text and file name are markers."""
    return PermissionError(errno.EACCES, STRERROR_MARKER, f"{FILENAME_MARKER}/x")


def existing_names(paths: list[Path]) -> list[str]:
    return [path.name for path in paths if path.exists()]


def fail_unlink(monkeypatch: pytest.MonkeyPatch, failing: Path, armed: Sized) -> None:
    """Make removing ``failing`` raise the marker ``PermissionError`` once ``armed``
    is non-empty."""
    original = Path.unlink

    def unlink(path: Path, missing_ok: bool = False) -> None:
        if armed and path == failing:
            raise marker_permission_error()
        original(path, missing_ok=missing_ok)

    monkeypatch.setattr(Path, "unlink", unlink)


def make_unremovable(monkeypatch: pytest.MonkeyPatch, target: Path, how: str) -> None:
    """Replace ``target`` with a directory, or make removing it raise ``PermissionError``."""
    if how == "directory":
        target.unlink()
        target.mkdir()
    else:
        fail_unlink(monkeypatch, target, [True])


def refuse_after_write(
    monkeypatch: pytest.MonkeyPatch, module: str, refusing: str, targets: list[Path]
) -> tuple[InvariantError, list[list[str]]]:
    """Make the class ``refusing`` in ``module`` raise ``InvariantError``. Returns
    the error, and the targets that existed at each refusal, which shows the
    write ran first."""
    error = InvariantError(what=f"{refusing} refused after the write", context=())
    refusals: list[list[str]] = []

    def refuse(*_args: object, **_kwargs: object) -> NoReturn:
        refusals.append(existing_names(targets))
        raise error

    monkeypatch.setattr(f"{module}.{refusing}", refuse)
    return error, refusals
