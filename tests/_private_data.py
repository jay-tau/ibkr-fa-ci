"""Locate the private data that real-statement tests and scripts read (#838).

Private files live outside git under one root: the absolute path in
``IBKR_FA_PRIVATE_DATA`` when that variable is set, otherwise the
git-ignored ``data/`` at the repository root. Never commit anything from
it. Each test names its files relative to the root:

    ibkr/activity-statement-fy2025-26.csv  IBKR Activity Statement, FY 2025-26
    ibkr/activity-statement-cy2025.csv     IBKR Activity Statement, CY 2025
    ibkr/flex-query.csv                    IBKR Flex Query: Open Positions lots, simple Trades
    ibkr/flex-query-closed-lots.csv        IBKR Flex Query with Closed Lots enabled
    rh/statements/                         Robinhood monthly statement PDFs (Crypto in crypto/)
    rh/<a folder named ...tax...>/         Robinhood Form 1042-S tax packet PDFs
    rh/report/individual.csv               Robinhood activity report, individual account
    rh/report/managed-individual.csv       Robinhood activity report, managed account
    rh/report/ranges.toml                  report date ranges and account opening dates
    user.toml                              config for scripts/preflight_massive_option_coverage.py

A test skips only when its whole dataset is absent, and fails when part
of it is present or can't be checked. A root that is set but unusable
fails the whole test session (``tests/conftest.py``).
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Final

import pytest

PRIVATE_DATA_ENV: Final[str] = "IBKR_FA_PRIVATE_DATA"
_DEFAULT_ROOT: Final[Path] = Path(__file__).resolve().parents[1] / "data"


class PrivateDataRootError(Exception):
    """The selected private-data root can't be used."""


def private_data_root() -> Path | None:
    """Return the selected root, or ``None`` when the variable is unset
    and the default ``data/`` doesn't exist."""
    value = os.environ.get(PRIVATE_DATA_ENV)
    if value is None:
        root = _DEFAULT_ROOT
        source = "the default data/ at the repository root"
    else:
        source = PRIVATE_DATA_ENV
        if not value.strip():
            raise PrivateDataRootError(
                f"{source} is set but blank; unset it or give an absolute path"
            )
        root = Path(value)
        if not root.is_absolute():
            raise PrivateDataRootError(f"{source} must be an absolute path: {value}")
    try:
        root.lstat()
        is_directory = root.is_dir()
    except FileNotFoundError as exc:
        if value is None:
            return None
        raise PrivateDataRootError(f"{source} names a path that doesn't exist: {value}") from exc
    except OSError as exc:
        raise PrivateDataRootError(f"{source} can't be read: {root}") from exc
    if not is_directory:
        raise PrivateDataRootError(f"{source} is not a directory: {root}")
    if not os.access(root, os.R_OK | os.X_OK):
        raise PrivateDataRootError(f"{source} can't be read: {root}")
    return root


def private_dataset(*relative: str) -> tuple[Path, ...]:
    """Return the dataset's paths under the root, in argument order.

    Skips when there is no root or none of the parts exists; fails when
    only some do. A broken symlink counts as present, so it fails when
    read rather than skipping."""
    root = private_data_root()
    names = ", ".join(relative)
    if root is None:
        pytest.skip(f"private dataset {names} is unavailable")
    paths = tuple(root / part for part in relative)
    missing = [part for part, path in zip(relative, paths, strict=True) if not _present(path, part)]
    if len(missing) == len(relative):
        pytest.skip(f"private dataset {names} is unavailable")
    if missing:
        pytest.fail(
            f"private dataset {names} is incomplete; missing {', '.join(missing)}",
            pytrace=False,
        )
    return paths


def _present(path: Path, part: str) -> bool:
    """Like ``os.path.lexists``, but only a missing entry in a readable
    directory counts as absent. Fails on any other lookup error, or when
    the nearest existing ancestor is a file or a broken symlink."""
    probe = path
    while True:
        try:
            probe.lstat()
            break
        except FileNotFoundError:
            probe = probe.parent
        except OSError:
            pytest.fail(f"private dataset part {part} can't be checked", pytrace=False)
    if probe == path:
        return True
    if not probe.is_dir():
        pytest.fail(f"private dataset part {part} is under a non-directory", pytrace=False)
    if not os.access(probe, os.R_OK | os.X_OK):
        pytest.fail(f"private dataset part {part} can't be checked", pytrace=False)
    return False


def private_pdfs(directory: Path) -> list[Path]:
    """Every ``*.pdf`` under ``directory``, sorted, without following
    symlinked folders (as ``rglob`` does). Fails, rather than leaving part
    of the tree out, when a folder can't be read."""

    def unreadable(_exc: OSError) -> None:
        pytest.fail("a private dataset folder can't be read", pytrace=False)

    return sorted(
        Path(folder) / name
        for folder, _subfolders, names in os.walk(directory, onerror=unreadable)
        for name in names
        if name.endswith(".pdf")
    )
