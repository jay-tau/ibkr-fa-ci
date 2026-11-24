"""Existence checks that keep filesystem errors.

On Python 3.14, ``Path.exists()``, ``Path.is_dir()`` and ``Path.is_file()``
return ``False`` for any ``OSError``, ``EACCES`` included; on 3.12 and 3.13
they raise it. So a path inside a folder that can't be searched reads as
missing on 3.14, and a check written for the raise takes its "absent" branch
instead (#1238). These checks raise such errors on every version. A missing
entry, a file on the way, a symlink loop or an invalid path reads as absent;
pathlib's checks return ``False`` for those on every version, so they keep
the refusals they get today.

This module imports nothing from :mod:`ibkr_fa`, so every layer can use it.
"""

from __future__ import annotations

import errno
import stat
from pathlib import Path


def _mode(path: Path) -> int | None:
    """``path``'s mode, following symlinks, or ``None`` when it reads as absent
    (see the module docstring). Raises any other ``OSError``."""
    try:
        return path.stat().st_mode
    except (FileNotFoundError, NotADirectoryError, ValueError):
        return None
    except OSError as exc:
        if exc.errno == errno.ELOOP:
            return None
        raise


def exists(path: Path) -> bool:
    """``Path.exists()``, except that it raises an ``OSError`` such as ``EACCES``."""
    return _mode(path) is not None


def is_dir(path: Path) -> bool:
    """``Path.is_dir()``, except that it raises an ``OSError`` such as ``EACCES``."""
    mode = _mode(path)
    return mode is not None and stat.S_ISDIR(mode)


def is_file(path: Path) -> bool:
    """``Path.is_file()``, except that it raises an ``OSError`` such as ``EACCES``."""
    mode = _mode(path)
    return mode is not None and stat.S_ISREG(mode)


def may_exist(path: Path) -> bool:
    """:func:`exists`, except that a path that can't be inspected counts as
    present: a cache lookup then reads it and refuses with its read error,
    instead of fetching over it or calling the cache cold."""
    try:
        return exists(path)
    except OSError:
        return True
