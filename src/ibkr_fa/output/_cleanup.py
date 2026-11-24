"""Output cleanup helpers.

Wrapped output refusals name a path the caller passed (a typed path, a fixed name joined to
one, or a name found by listing a typed folder) and an ``OSError`` by its
errno name only (:func:`~ibkr_fa.exceptions.errno_name`), never its text or
file names (#1048).
"""

from __future__ import annotations

import contextlib
import errno
import fnmatch
import os
import stat
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path
from types import TracebackType
from typing import NoReturn, Self

from ibkr_fa.exceptions import IbkrFaError, errno_name, raise_replacing

# A file's ``(st_dev, st_ino)``.
type FileIdentity = tuple[int, int]


class OutputCollisionError(IbkrFaError):
    """A file a run may delete or replace is one of that run's own inputs."""


class OutputRemovalError(IbkrFaError):
    """A run could not remove an output: an earlier run's, or its own after a failure."""


class OutputWriteError(IbkrFaError):
    """A writer could not finish staging an output; nothing was published."""


def refuse_input_collisions(targets: Iterable[Path], inputs: Iterable[tuple[str, Path]]) -> None:
    """Refuse, before any cleanup, if a target the run may delete is one of its
    inputs or a directory entry on an input's path.

    ``inputs`` pairs each input file with the flag or config key that supplied
    it. ``samefile`` (not a path-string compare) so a relative or ``..`` path, a
    symlink or a hard link cannot slip past. ``samefile`` needs the referent,
    so :func:`_refuse_entry_collisions` also compares the entries themselves.
    Only a missing entry is exempt: a missing target or input cannot clash, and
    a missing input keeps its normal later error. Any other error while
    checking refuses rather than risk deleting an input.
    """
    named_targets = tuple(targets)
    named_inputs = tuple(inputs)
    for target in named_targets:
        for label, path in named_inputs:
            try:
                same_file = target.samefile(path)
            except FileNotFoundError:
                continue
            except OSError as exc:
                raise OutputCollisionError(
                    f"could not check output target {target} against {label} {path} "
                    f"({errno_name(exc)})"
                ) from exc
            if same_file:
                raise OutputCollisionError(
                    f"{label} {path} is the same file as {target}, which this run deletes "
                    "or replaces. Move the input or choose a different output path."
                )
    _refuse_entry_collisions(named_targets, named_inputs)


def _refuse_entry_collisions(
    targets: tuple[Path, ...], inputs: tuple[tuple[str, Path], ...]
) -> None:
    """Refuse if a target's own entry is one an input's path goes through
    (see :func:`_path_entries`), so a dangling symlink counts as an input entry,
    and so does a symlink an input is reached through. No input path is walked
    while every target is missing.
    """
    present = []
    for target in targets:
        try:
            info = os.lstat(target)
        except FileNotFoundError:
            continue
        except OSError as exc:
            raise OutputCollisionError(
                f"could not inspect output target {target} ({errno_name(exc)})"
            ) from exc
        present.append((target, (info.st_dev, info.st_ino)))
    if not present:
        return
    for label, path in inputs:
        failure = None
        try:
            entries = _path_entries(path)
        except OSError as exc:
            failure = errno_name(exc)
        if failure is not None:
            # Raised after the handler, chaining nothing: the walk's error can name
            # the entry it stopped at, which absolute() or a symlink's target built,
            # not the user (#1623).
            raise OutputCollisionError(f"could not inspect {label} {path} ({failure})")
        for target, entry in present:
            if entry in entries:
                raise OutputCollisionError(
                    f"{label} {path} is or goes through {target}, which this run deletes "
                    "or replaces. Move the input or choose a different output path."
                )


# Linux's MAXSYMLINKS: the most symlinks one path lookup follows.
_MAX_SYMLINK_HOPS = 40


def _path_entries(path: Path) -> set[tuple[int, int]]:
    """The ``lstat`` identity of every existing entry ``path`` goes through.

    Resolves ``path`` one component at a time, as ``realpath`` does, following
    each symlink with ``readlink`` and applying ``..`` only once the symlinks
    before it are followed, and stops at the first missing component: so a
    dangling symlink, or one whose referent is reached only through a missing
    directory, is still recorded. Raises ``OSError`` on anything else: an
    existing non-directory with more path after it (``file/../x``, which
    ``realpath`` would accept), or more symlinks than Linux follows.
    """
    entries: set[tuple[int, int]] = set()
    # Always a directory reached without symlinks, so ``..`` is its parent.
    directory = Path("/")
    pending = list(reversed(path.absolute().parts))
    hops = 0
    while pending:
        name = pending.pop()
        if name == "..":
            directory = directory.parent
            continue
        entry = directory / name  # a raw link's "" or "." names the directory itself
        try:
            info = os.lstat(entry)
        except FileNotFoundError:
            break
        entries.add((info.st_dev, info.st_ino))
        if stat.S_ISLNK(info.st_mode):
            hops += 1
            if hops > _MAX_SYMLINK_HOPS:
                raise OSError(errno.ELOOP, os.strerror(errno.ELOOP))
            link = os.readlink(entry)  # noqa: PTH115 - pathlib drops trailing slash/dot requirements
            if link.startswith("/"):
                directory = Path("/")
            pending.extend(reversed(link.split("/")))
        elif stat.S_ISDIR(info.st_mode):
            directory = entry
        elif pending:
            raise NotADirectoryError(errno.ENOTDIR, os.strerror(errno.ENOTDIR))
    return entries


def scan_inputs(folder: Path, pattern: str, *, recursive: bool = False) -> list[Path]:
    """``folder.glob(pattern)`` (``rglob`` when ``recursive``) for the collision
    check, except that a directory that cannot be listed refuses instead of
    reading as empty: a file in it could not be checked. A missing ``folder`` or
    a regular file has no files (its run reports that later; the caller checks
    the supplied path itself). Like ``rglob``, this does not follow directory
    symlinks.
    """

    def refuse(directory: Path, exc: OSError) -> None:
        if not isinstance(exc, FileNotFoundError):
            raise OutputCollisionError(
                f"could not list {directory} to check it against the output targets "
                f"({errno_name(exc)})"
            ) from exc

    try:
        if not stat.S_ISDIR(folder.stat().st_mode):
            return []
    except OSError as exc:
        refuse(folder, exc)
        return []

    found: list[Path] = []
    pending = [folder]
    while pending:
        directory = pending.pop()
        try:
            with os.scandir(directory) as entries:
                for entry in entries:
                    try:
                        is_dir = entry.is_dir(follow_symlinks=False)
                    except FileNotFoundError:
                        continue
                    path = Path(entry.path)
                    if is_dir:
                        if recursive:
                            pending.append(path)
                    elif fnmatch.fnmatch(entry.name, pattern):
                        found.append(path)
        except OSError as exc:
            refuse(directory, exc)
    return sorted(found)


def remove_output_paths(paths: Iterable[Path]) -> None:
    """Attempt every removal, then propagate any filesystem error."""
    with contextlib.ExitStack() as cleanup:
        for path in paths:
            cleanup.callback(path.unlink, missing_ok=True)


def _unremovable(targets: Iterable[Path]) -> str:
    """Attempt every removal; describe each one that failed ("" if none did)."""
    failures = []
    for target in targets:
        try:
            target.unlink(missing_ok=True)
        except OSError as exc:
            failures.append(f"{target} ({errno_name(exc)})")
    return "; ".join(failures)


def remove_previous_outputs(targets: Sequence[Path], inputs: Iterable[tuple[str, Path]]) -> None:
    """Before a run reads anything, remove every output an earlier run left, so
    a run that refuses leaves none behind to be filed by mistake.

    Refuses first if a target is one of ``inputs`` (see
    :func:`refuse_input_collisions`); then attempts every removal before
    refusing, naming each target that could not be removed.
    """
    refuse_input_collisions(targets, inputs)
    if failures := _unremovable(targets):
        raise OutputRemovalError(f"could not remove the previous output {failures}")


def remove_failed_run_outputs(
    targets: Iterable[Path], claims: Iterable[ClaimedOutputCleanup] = ()
) -> None:
    """Remove every target of a run that failed, and each claim's path while it
    is still the file the claim recorded, so the run leaves no output to file.
    Attempts every removal, then raises :class:`OutputRemovalError` naming each
    path that could not be inspected or removed.
    """
    unremovable = _unremovable(targets)
    failures = [f"could not remove {unremovable}"] if unremovable else []
    failures.extend(failure for claim in claims if (failure := claim._remove()))
    if failures:
        raise OutputRemovalError(
            f"the run failed, and its output may still be there: {'; '.join(failures)}"
        )


def _cleanup_failed(error: OutputRemovalError, exc: BaseException) -> NoReturn:
    """Raise ``error`` in place of ``exc``, the block's exception, keeping none
    of it (:func:`~ibkr_fa.exceptions.raise_replacing`). When that is itself an
    :class:`OutputRemovalError`, such as a writer's staging file it could not
    remove, the new message includes its message, so the one error the CLI
    prints names every path left. Both are fixed text, allowed paths and errno
    names; no other exception's text is included.
    """
    if isinstance(exc, OutputRemovalError):
        error = OutputRemovalError(f"{error}; {exc}")
    raise_replacing(error, exc)


def remove_outputs_on_failure(
    targets: Sequence[Path], claims: Sequence[ClaimedOutputCleanup] = ()
) -> contextlib.ExitStack:
    """Remove every target, and each claim's path while it is still the claimed
    file, if the block raises, then re-raise, so a run that fails during or
    after its write leaves no output. If a removal fails too, raise
    :class:`OutputRemovalError` in place of the block's exception instead (see
    :func:`_cleanup_failed`).
    """

    def cleanup(
        _exc_type: type[BaseException] | None,
        exc: BaseException | None,
        _traceback: TracebackType | None,
    ) -> None:
        if exc is not None:
            try:
                remove_failed_run_outputs(targets, claims)
            except OutputRemovalError as removal_error:
                _cleanup_failed(removal_error, exc)

    # ExitStack propagates frozen domain errors without assigning their traceback.
    stack = contextlib.ExitStack()
    stack.push(cleanup)
    return stack


class ClaimedOutputCleanup:
    """If the block raises, remove ``path`` only while it is still the file
    :meth:`claim` recorded, then re-raise. For a writer whose ``path`` may hold
    a file the run did not write until the moment it publishes its own. A run
    that fails without raising passes it to :func:`remove_failed_run_outputs`.

    Unclaimed, or with ``path`` missing or a different file, the cleanup leaves
    it alone. If ``path`` cannot be inspected or removed, it raises
    :class:`OutputRemovalError` in place of the block's exception instead (see
    :func:`_cleanup_failed`). This tells the run's file apart from an earlier
    one; it does not lock ``path`` against another process writing it.

    A class, not a :func:`contextlib.contextmanager`, which cannot re-raise
    this package's frozen exceptions.
    """

    def __init__(self, path: Path) -> None:
        self._path = path
        self._identity: FileIdentity | None = None

    @property
    def claimed(self) -> bool:
        """Whether the run has staged its file: from then on it may have published it."""
        return self._identity is not None

    def claim(self, identity: FileIdentity) -> None:
        """Record the identity of the file the run is about to publish at ``path``."""
        self._identity = identity

    def _remove(self) -> str:
        """Remove ``path`` if it is still the claimed file. Describes what could
        not be inspected or removed ("" if nothing)."""
        if self._identity is None:
            return ""
        try:
            info = os.lstat(self._path)
        except FileNotFoundError:
            return ""
        except OSError as exc:
            return f"could not inspect {self._path} ({errno_name(exc)})"
        if (info.st_dev, info.st_ino) != self._identity:
            return ""
        unremovable = _unremovable((self._path,))
        return f"could not remove {unremovable}" if unremovable else ""

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        _exc_type: type[BaseException] | None,
        exc: BaseException | None,
        _traceback: TracebackType | None,
    ) -> None:
        if exc is not None:
            try:
                remove_failed_run_outputs((), (self,))
            except OutputRemovalError as removal_error:
                _cleanup_failed(removal_error, exc)


@dataclass(frozen=True, slots=True)
class RunOutputs:
    """Every output a finished run may have published, so that its caller can
    remove them if it fails after the run returned: each of ``paths``, and each
    claim's path while it is still the file the claim recorded.
    """

    paths: tuple[Path, ...]
    claims: tuple[ClaimedOutputCleanup, ...]

    def removed_on_failure(self) -> contextlib.ExitStack:
        """See :func:`remove_outputs_on_failure`."""
        return remove_outputs_on_failure(self.paths, self.claims)
