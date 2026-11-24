"""Atomic output helpers shared by the file writers.

Each helper stages the output in a ``tempfile.mkstemp`` file named
``.{path.name}.*.tmp`` beside ``path`` and commits it with ``os.replace``
only after the ``with`` body exits cleanly. On any exception, including
:class:`KeyboardInterrupt`, the staging file is unlinked once and the
original exception propagates. A reader never sees a partial file, and a
failed write leaves ``path`` untouched.

If that unlink fails, the helper raises
:class:`~ibkr_fa.output._cleanup.OutputRemovalError` naming the staging
file, which may still hold this run's output, in place of the first failure:
the body's exception when there is one, otherwise the failing ``os.fdopen``,
close, ``os.close`` or ``os.replace``. It keeps none of that failure
(:func:`~ibkr_fa.exceptions.raise_replacing`, #1211). Wrapped
``OutputRemovalError`` and ``OutputWriteError`` messages name the staging file
by its pattern beside ``path`` (:func:`staging_file_pattern`), never by its
random, absolute ``mkstemp`` name, and an ``OSError`` by its errno name only
(#1048).

The staging file is created with mode ``0o600``, and ``os.replace``
replaces a symlink at ``path`` rather than writing through it. Inside the
``with`` body, :meth:`_AtomicTextOutput.staged_identity` gives the staging
file's identity, which ``path`` has once it is published.

``tempfile.mkstemp``, ``os.fdopen``, ``os.fstat`` and ``os.replace`` are
looked up on their modules at call time, so tests can patch them there.

The helpers are classes, not :func:`contextlib.contextmanager` generators:
a generator context manager re-raising an exception assigns its
``__traceback__``, which fails with :class:`TypeError` on this package's
frozen dataclass exceptions such as
:class:`~ibkr_fa.output._writer_contract.WriterContractError`. For the same
reason they never assign to an exception they re-raise.
"""

from __future__ import annotations

import contextlib
import os
import tempfile
from contextlib import AbstractContextManager
from pathlib import Path
from types import TracebackType
from typing import TextIO

from ibkr_fa.exceptions import errno_name, raise_replacing
from ibkr_fa.output._cleanup import FileIdentity, OutputRemovalError, OutputWriteError


def staging_file_pattern(path: Path) -> Path:
    """The pattern of ``path``'s staging files, ``.{name}.*.tmp`` beside it: the
    path as given plus fixed text, never the ``mkstemp`` name itself."""
    return path.parent / f".{path.name}.*.tmp"


def discard_staging_file(staged: Path, path: Path, failure: BaseException) -> None:
    """Remove ``staged``, the staging file of ``path``, after ``failure``.

    Raises:
        OutputRemovalError: In place of ``failure``, keeping none of it
            (:func:`~ibkr_fa.exceptions.raise_replacing`), if ``staged`` cannot
            be removed.
    """
    try:
        staged.unlink(missing_ok=True)
    except OSError as exc:
        raise_replacing(
            OutputRemovalError(
                "the write failed, and its output may still be there: could not remove "
                f"the staging file {staging_file_pattern(path)} ({errno_name(exc)})"
            ),
            failure,
        )


class _Staged:
    def __init__(self, path: Path) -> None:
        self._path = path
        self._tmp_path = ""

    def _stage(self) -> int:
        fd, self._tmp_path = tempfile.mkstemp(
            dir=self._path.parent, prefix=f".{self._path.name}.", suffix=".tmp"
        )
        return fd

    def _discard(self, failure: BaseException) -> None:
        discard_staging_file(Path(self._tmp_path), self._path, failure)

    def _finish(self, failure: BaseException | None) -> None:
        """Commit the staged file, or discard it if the body or the commit failed."""
        if failure is not None:
            self._discard(failure)
            return
        try:
            os.replace(self._tmp_path, self._path)  # noqa: PTH105 - required atomic-write contract
        except BaseException as exc:
            self._discard(exc)
            raise


class _AtomicTextOutput(_Staged):
    def __init__(self, path: Path, *, encoding: str, newline: str) -> None:
        super().__init__(path)
        self._encoding = encoding
        self._newline = newline

    def __enter__(self) -> TextIO:
        fd = self._stage()
        try:
            self._handle = os.fdopen(fd, "w", encoding=self._encoding, newline=self._newline)
        except BaseException as exc:
            # fdopen may already have closed the descriptor before failing.
            with contextlib.suppress(OSError):
                os.close(fd)
            self._discard(exc)
            raise
        return self._handle

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        try:
            self._handle.close()
        except BaseException as close_exc:
            if exc is None:
                self._discard(close_exc)
                raise
        self._finish(exc)

    def staged_identity(self) -> FileIdentity:
        """The staging file's ``(st_dev, st_ino)``, from the open handle.

        Raises:
            OutputWriteError: From the ``OSError``, naming the staging file, if
                it cannot be inspected.
        """
        try:
            info = os.fstat(self._handle.fileno())
        except OSError as exc:
            raise OutputWriteError(
                f"could not write {self._path}: could not inspect its staging file "
                f"{staging_file_pattern(self._path)} ({errno_name(exc)})"
            ) from exc
        return info.st_dev, info.st_ino


class _AtomicOutputPath(_Staged):
    def __enter__(self) -> Path:
        fd = self._stage()
        try:
            os.close(fd)
        except BaseException as exc:
            self._discard(exc)
            raise
        return Path(self._tmp_path)

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self._finish(exc)


def atomic_text_output(path: Path, *, encoding: str, newline: str) -> _AtomicTextOutput:
    """Yield a text handle whose contents replace ``path`` atomically on clean exit."""
    return _AtomicTextOutput(path, encoding=encoding, newline=newline)


def atomic_output_path(path: Path) -> AbstractContextManager[Path]:
    """Yield a staging path whose file replaces ``path`` atomically on clean exit.

    For writers that open the file by name, such as openpyxl and ``ZipFile``.
    """
    return _AtomicOutputPath(path)
