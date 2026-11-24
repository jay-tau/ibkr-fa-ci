"""Tests for the shared atomic output helpers in :mod:`ibkr_fa.output._atomic` (#605)."""

from __future__ import annotations

import errno
import os
import stat
import tempfile
from collections.abc import Callable
from pathlib import Path
from typing import Any
from unittest.mock import Mock, patch

import pytest

from ibkr_fa.output._atomic import atomic_output_path, atomic_text_output, discard_staging_file
from ibkr_fa.output._cleanup import OutputRemovalError, OutputWriteError
from ibkr_fa.output._writer_contract import WriterContractError
from tests._crash_reports import assert_replaced_unchained

_Write = Callable[..., None]


def _write_text(path: Path, content: str, *, fail: BaseException | None = None) -> None:
    with atomic_text_output(path, encoding="utf-8", newline="") as handle:
        handle.write(content)
        if fail is not None:
            raise fail


def _write_by_path(path: Path, content: str, *, fail: BaseException | None = None) -> None:
    with atomic_output_path(path) as tmp_path:
        tmp_path.write_text(content, encoding="utf-8")
        if fail is not None:
            raise fail


_WRITERS = pytest.mark.parametrize("write", [_write_text, _write_by_path], ids=["text", "path"])


def temp_glob(path: Path) -> list[Path]:
    return sorted(path.parent.glob(f".{path.name}.*.tmp"))


@_WRITERS
def test_clean_exit_replaces_destination_and_leaves_no_temp_file(
    tmp_path: Path, write: _Write
) -> None:
    out = tmp_path / "out.csv"
    out.write_text("sentinel\n", encoding="utf-8")

    write(out, "new\n")

    assert out.read_text(encoding="utf-8") == "new\n"
    assert temp_glob(out) == []


@_WRITERS
@pytest.mark.parametrize(
    "error",
    [
        ValueError("boom"),
        KeyboardInterrupt(),
        # Frozen dataclass exceptions reject attribute assignment, which a
        # contextlib.contextmanager generator does on re-raise.
        WriterContractError(violation="nul_byte", row_index=1, cell_index=0, trigger=""),
    ],
    ids=["value-error", "keyboard-interrupt", "frozen-exception"],
)
def test_body_exception_keeps_destination_and_removes_temp_file(
    tmp_path: Path, write: _Write, error: BaseException
) -> None:
    out = tmp_path / "out.csv"
    out.write_text("sentinel\n", encoding="utf-8")

    with pytest.raises(type(error)) as raised:
        write(out, "partial", fail=error)

    assert raised.value is error
    assert out.read_text(encoding="utf-8") == "sentinel\n"
    assert temp_glob(out) == []


@_WRITERS
def test_temp_file_is_staged_beside_destination(
    tmp_path: Path, write: _Write, monkeypatch: pytest.MonkeyPatch
) -> None:
    out = tmp_path / "out.csv"
    staged: list[Path] = []
    real_replace = os.replace

    def spy_replace(src: str, dst: Path) -> None:
        staged.append(Path(src))
        real_replace(src, dst)

    monkeypatch.setattr(os, "replace", spy_replace)

    write(out, "new\n")

    assert len(staged) == 1
    assert staged[0].parent == tmp_path
    assert staged[0].match(".out.csv.*.tmp")


@_WRITERS
def test_mkstemp_failure_propagates_unchanged(
    tmp_path: Path, write: _Write, monkeypatch: pytest.MonkeyPatch
) -> None:
    out = tmp_path / "out.csv"

    def raise_permission(*_args: object, **_kwargs: object) -> tuple[int, str]:
        raise PermissionError("denied")

    monkeypatch.setattr(tempfile, "mkstemp", raise_permission)

    with pytest.raises(PermissionError, match="denied"):
        write(out, "new\n")

    assert list(tmp_path.iterdir()) == []


@_WRITERS
def test_replace_failure_keeps_destination_and_removes_temp_file(
    tmp_path: Path, write: _Write, monkeypatch: pytest.MonkeyPatch
) -> None:
    out = tmp_path / "out.csv"
    out.write_text("sentinel\n", encoding="utf-8")

    def raise_replace(*_args: object) -> None:
        raise OSError("EXDEV")

    monkeypatch.setattr(os, "replace", raise_replace)

    with pytest.raises(OSError, match="EXDEV"):
        write(out, "new\n")

    assert out.read_text(encoding="utf-8") == "sentinel\n"
    assert temp_glob(out) == []


# Markers an OSError can carry, which no error may print (#1048): code can
# raise an OSError with any text and any file name.
_STRERROR = "Synthetic Strerror Marker"
_HOLDER = "Synthetic Holder Two"


def _marker_error() -> PermissionError:
    """An EACCES failure whose text and file name are markers."""
    return PermissionError(errno.EACCES, _STRERROR, f"{_HOLDER}/x")


def _assert_omits_markers(message: str) -> None:
    """Unsupplied markers guard future echoes; their absence alone does not
    demonstrate a change from base."""
    for marker in (_STRERROR, _HOLDER):
        assert marker.lower() not in message.lower()


def _assert_names_staging_file(out: Path, error: OutputRemovalError) -> None:
    """``error`` names ``out``'s staging file by its pattern beside ``out`` and
    the unlink's errno name: not by its own random, absolute ``mkstemp`` name,
    nor by the unlink error's text or file name (#1048)."""
    (staged,) = temp_glob(out)
    message = str(error)
    assert message == (
        "the write failed, and its output may still be there: could not remove the "
        f"staging file {out.parent}/.{out.name}.*.tmp (EACCES)"
    )
    assert staged.name not in message
    _assert_omits_markers(message)


@_WRITERS
def test_unlink_failure_after_body_error_refuses_naming_the_staging_file(
    tmp_path: Path, write: _Write
) -> None:
    out = tmp_path / "out.csv"
    error = ValueError("boom")

    with (
        patch("pathlib.Path.unlink", side_effect=_marker_error()) as m_unlink,
        pytest.raises(OutputRemovalError) as raised,
    ):
        write(out, "partial", fail=error)

    assert_replaced_unchained(raised.value, ValueError, markers=("boom", _STRERROR, _HOLDER))
    m_unlink.assert_called_once_with(missing_ok=True)
    _assert_names_staging_file(out, raised.value)


def _fail_replace(monkeypatch: pytest.MonkeyPatch) -> BaseException:
    error = OSError("EXDEV")

    def raise_replace(*_args: object) -> None:
        raise error

    monkeypatch.setattr(os, "replace", raise_replace)
    return error


def _fail_fdopen(monkeypatch: pytest.MonkeyPatch) -> BaseException:
    error = OSError("EMFILE")

    def raise_fdopen(*_args: object, **_kwargs: object) -> object:
        raise error

    monkeypatch.setattr(os, "fdopen", raise_fdopen)
    return error


def _fail_text_close(monkeypatch: pytest.MonkeyPatch) -> BaseException:
    error = OSError("disk full")
    real_fdopen = os.fdopen

    def fdopen_with_failing_close(*args: Any, **kwargs: Any) -> Mock:
        handle = real_fdopen(*args, **kwargs)
        wrapped = Mock(wraps=handle)

        def fail_close() -> None:
            handle.close()
            raise error

        wrapped.close.side_effect = fail_close
        return wrapped

    monkeypatch.setattr(os, "fdopen", fdopen_with_failing_close)
    return error


def _fail_raw_close(monkeypatch: pytest.MonkeyPatch) -> BaseException:
    error = OSError("EIO")
    real_close = os.close

    def close_then_fail(fd: int) -> None:
        real_close(fd)
        raise error

    monkeypatch.setattr(os, "close", close_then_fail)
    return error


@pytest.mark.parametrize(
    ("write", "fail_step"),
    [
        (_write_text, _fail_replace),
        (_write_by_path, _fail_replace),
        (_write_text, _fail_fdopen),
        (_write_text, _fail_text_close),
        (_write_by_path, _fail_raw_close),
    ],
    ids=["text-replace", "path-replace", "text-fdopen", "text-close", "path-os-close"],
)
def test_unlink_failure_after_a_failed_step_refuses_keeping_none_of_that_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    write: _Write,
    fail_step: Callable[[pytest.MonkeyPatch], BaseException],
) -> None:
    out = tmp_path / "out.csv"
    out.write_text("sentinel\n", encoding="utf-8")
    first_failure = fail_step(monkeypatch)

    with (
        patch("pathlib.Path.unlink", side_effect=_marker_error()) as m_unlink,
        pytest.raises(OutputRemovalError) as raised,
    ):
        write(out, "new\n")

    assert_replaced_unchained(raised.value, markers=(str(first_failure), _STRERROR, _HOLDER))
    m_unlink.assert_called_once_with(missing_ok=True)
    _assert_names_staging_file(out, raised.value)
    assert out.read_text(encoding="utf-8") == "sentinel\n"


@pytest.mark.parametrize(
    ("body_error", "crash", "markers"),
    [
        (ValueError("boom"), ValueError, ("boom",)),
        (KeyboardInterrupt(), KeyboardInterrupt, ()),
        (
            WriterContractError(violation="nul_byte", row_index=1, cell_index=0, trigger=""),
            None,
            ("contains a NUL byte",),
        ),
    ],
    ids=["value-error", "keyboard-interrupt", "frozen-exception"],
)
def test_unlink_failure_after_body_and_close_errors_keeps_none_of_the_body_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    body_error: BaseException,
    crash: type[BaseException] | None,
    markers: tuple[str, ...],
) -> None:
    out = tmp_path / "out.csv"
    _fail_text_close(monkeypatch)

    with (
        patch("pathlib.Path.unlink", side_effect=_marker_error()) as m_unlink,
        pytest.raises(OutputRemovalError) as raised,
    ):
        _write_text(out, "partial", fail=body_error)

    assert_replaced_unchained(raised.value, crash, markers=(*markers, _STRERROR, _HOLDER))
    m_unlink.assert_called_once_with(missing_ok=True)
    _assert_names_staging_file(out, raised.value)


@pytest.mark.parametrize(
    ("unlink_error", "errno_text"),
    [
        (_marker_error(), "EACCES"),
        (OSError(_STRERROR), "unknown error"),
        (OSError(99999, _STRERROR), "unknown error"),
    ],
    ids=["eacces", "no-errno", "unmapped-errno"],
)
def test_discard_staging_file_names_the_pattern_and_the_errno_only(
    tmp_path: Path, unlink_error: OSError, errno_text: str
) -> None:
    """#1048: the staging file's own name is ``mkstemp``'s, and the OSError's
    text and file name can be anything, so the error prints the pattern beside
    the output and the errno name, or "unknown error" without a known errno."""
    out = tmp_path / "out.csv"
    staged = tmp_path / ".out.csv.r4nd0m.tmp"
    staged.write_text("partial", encoding="utf-8")
    failure = ValueError("boom")

    with (
        patch("pathlib.Path.unlink", side_effect=unlink_error),
        pytest.raises(OutputRemovalError) as raised,
    ):
        discard_staging_file(staged, out, failure)

    message = str(raised.value)
    assert message == (
        "the write failed, and its output may still be there: could not remove the "
        f"staging file {tmp_path}/.out.csv.*.tmp ({errno_text})"
    )
    assert "r4nd0m" not in message
    _assert_omits_markers(message)
    markers = ("boom", _STRERROR, _HOLDER) if unlink_error.filename else ("boom", _STRERROR)
    assert_replaced_unchained(raised.value, ValueError, markers=markers)
    assert staged.read_text(encoding="utf-8") == "partial"


@_WRITERS
@pytest.mark.parametrize(
    ("out", "pattern"),
    [(Path("out.csv"), ".out.csv.*.tmp"), (Path("reports/out.csv"), "reports/.out.csv.*.tmp")],
    ids=["working-directory", "subfolder"],
)
def test_unlink_failure_names_a_relative_output_as_given(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, write: _Write, out: Path, pattern: str
) -> None:
    """#1048: ``mkstemp`` returns an absolute name, which would print the
    working directory; the error prints the pattern beside the path as given."""
    monkeypatch.chdir(tmp_path)
    out.parent.mkdir(exist_ok=True)
    error = ValueError("boom")

    with (
        patch("pathlib.Path.unlink", side_effect=_marker_error()),
        pytest.raises(OutputRemovalError) as raised,
    ):
        write(out, "partial", fail=error)

    message = str(raised.value)
    assert message == (
        "the write failed, and its output may still be there: could not remove the "
        f"staging file {pattern} (EACCES)"
    )
    (staged,) = temp_glob(out)
    assert staged.name not in message
    assert str(tmp_path) not in message
    _assert_omits_markers(message)
    assert_replaced_unchained(raised.value, ValueError, markers=("boom", _STRERROR, _HOLDER))
    assert not out.exists()


@pytest.mark.parametrize(
    ("out", "pattern"),
    [(Path("out.csv"), ".out.csv.*.tmp"), (Path("reports/out.csv"), "reports/.out.csv.*.tmp")],
    ids=["working-directory", "subfolder"],
)
def test_staged_identity_failure_names_the_staging_file_pattern(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, out: Path, pattern: str
) -> None:
    """#1048: a staging file that cannot be inspected is named by its pattern
    beside the path as given, with the errno name only."""
    monkeypatch.chdir(tmp_path)
    out.parent.mkdir(exist_ok=True)
    error = _marker_error()
    real_fstat = os.fstat
    staged: list[Path] = []
    output = atomic_text_output(out, encoding="utf-8", newline="")

    with pytest.raises(OutputWriteError) as raised, output as handle:
        handle.write("partial")
        staged.extend(temp_glob(out))
        handle_fd = handle.fileno()

        def fstat(fd: int) -> os.stat_result:
            if fd == handle_fd:
                raise error
            return real_fstat(fd)

        monkeypatch.setattr(os, "fstat", fstat)
        output.staged_identity()

    message = str(raised.value)
    assert message == (
        f"could not write {out}: could not inspect its staging file {pattern} (EACCES)"
    )
    assert len(staged) == 1
    assert staged[0].name not in message
    assert str(tmp_path) not in message
    _assert_omits_markers(message)
    assert raised.value.__cause__ is error
    # The body's failure discards the staging file, as any other does.
    assert temp_glob(out) == []
    assert not out.exists()


@_WRITERS
def test_mkstemp_fd_is_closed(
    tmp_path: Path, write: _Write, monkeypatch: pytest.MonkeyPatch
) -> None:
    out = tmp_path / "out.csv"
    opened: list[int] = []
    real_mkstemp = tempfile.mkstemp

    def spy_mkstemp(**kwargs: Any) -> tuple[int, str]:
        fd, name = real_mkstemp(**kwargs)
        opened.append(fd)
        return fd, name

    monkeypatch.setattr(tempfile, "mkstemp", spy_mkstemp)

    write(out, "new\n")

    assert len(opened) == 1
    with pytest.raises(OSError, match="Bad file descriptor"):
        os.fstat(opened[0])


def test_text_fdopen_failure_closes_raw_fd_and_removes_temp_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    out = tmp_path / "out.csv"
    opened: list[int] = []
    closed: list[int] = []
    real_mkstemp = tempfile.mkstemp
    real_close = os.close

    def spy_mkstemp(**kwargs: Any) -> tuple[int, str]:
        fd, name = real_mkstemp(**kwargs)
        opened.append(fd)
        return fd, name

    def spy_close(fd: int) -> None:
        closed.append(fd)
        real_close(fd)

    def raise_fdopen(*_args: object, **_kwargs: object) -> object:
        raise OSError("EMFILE")

    monkeypatch.setattr(tempfile, "mkstemp", spy_mkstemp)
    monkeypatch.setattr(os, "close", spy_close)
    monkeypatch.setattr(os, "fdopen", raise_fdopen)

    with pytest.raises(OSError, match="EMFILE"):
        _write_text(out, "new\n")

    assert closed == opened
    assert len(opened) == 1
    assert list(tmp_path.iterdir()) == []


def test_text_output_applies_encoding_and_newline(tmp_path: Path) -> None:
    out = tmp_path / "out.csv"

    with atomic_text_output(out, encoding="utf-8-sig", newline="\r\n") as handle:
        handle.write("a\nb\n")

    assert out.read_bytes() == b"\xef\xbb\xbfa\r\nb\r\n"


def test_fdopen_error_after_closing_fd_preserves_error_and_removes_temp_file(
    tmp_path: Path,
) -> None:
    out = tmp_path / "out.csv"
    out.write_text("sentinel\n", encoding="utf-8")

    with (
        pytest.raises(LookupError, match="unknown encoding"),
        atomic_text_output(out, encoding="invalid-codec-605", newline=""),
    ):
        pytest.fail("fdopen should fail before entering the body")

    assert out.read_text(encoding="utf-8") == "sentinel\n"
    assert temp_glob(out) == []


@pytest.mark.parametrize(
    "body_error",
    [
        None,
        KeyboardInterrupt(),
        WriterContractError(violation="nul_byte", row_index=1, cell_index=0, trigger=""),
    ],
    ids=["clean-body", "keyboard-interrupt", "frozen-exception"],
)
def test_close_failure_preserves_body_error_and_removes_temp_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, body_error: BaseException | None
) -> None:
    out = tmp_path / "out.csv"
    out.write_text("sentinel\n", encoding="utf-8")
    close_error = _fail_text_close(monkeypatch)
    expected_error = body_error if body_error is not None else close_error

    with pytest.raises(type(expected_error)) as raised:
        _write_text(out, "partial", fail=body_error)

    assert raised.value is expected_error
    assert out.read_text(encoding="utf-8") == "sentinel\n"
    assert temp_glob(out) == []


@pytest.mark.skipif(os.name != "posix", reason="POSIX mode bits and symlinks")
@_WRITERS
def test_output_is_owner_only_and_replaces_symlink(tmp_path: Path, write: _Write) -> None:
    target = tmp_path / "target.csv"
    target.write_text("sentinel\n", encoding="utf-8")
    out = tmp_path / "out.csv"
    out.symlink_to(target)
    previous_umask = os.umask(0o022)
    try:
        write(out, "new\n")
    finally:
        os.umask(previous_umask)

    assert not out.is_symlink()
    assert stat.S_IMODE(out.stat().st_mode) == 0o600
    assert out.read_text(encoding="utf-8") == "new\n"
    assert target.read_text(encoding="utf-8") == "sentinel\n"
