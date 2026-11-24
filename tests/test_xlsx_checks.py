"""The shared sheet scan turns only format failures into ``ValueError`` (#1071).

An ``OSError`` or a crash in the scan propagates as the same object, so a
caller can name the errno or report the crash instead of refusing the sheet as
malformed. ``RefuseOnWarning`` records every warning, lets only openpyxl's fixed
"no default style" text through, and refuses on any other (#1103). Every
workbook here is synthetic.
"""

from __future__ import annotations

import errno
import lzma
import warnings
import zlib
from collections.abc import Iterator
from pathlib import Path
from typing import NoReturn
from xml.etree.ElementTree import ParseError as XMLParseError
from zipfile import BadZipFile

import openpyxl
import pytest
from openpyxl.formula.tokenizer import TokenizerError
from openpyxl.formula.translate import TranslatorError
from openpyxl.utils.exceptions import InvalidFileException
from openpyxl.worksheet._read_only import ReadOnlyWorksheet
from openpyxl.worksheet._reader import WorkSheetParser

from ibkr_fa.xlsx_checks import ALLOWED_WARNING, RefuseOnWarning, require_cells_in_order

_STRERROR = "Synthetic Strerror Marker"
_FILENAME = "Synthetic Holder Two/x"


@pytest.fixture
def worksheet(tmp_path: Path) -> Iterator[ReadOnlyWorksheet]:
    """A valid workbook's sheet, opened read-only as callers open it."""
    path = tmp_path / "book.xlsx"
    workbook = openpyxl.Workbook()
    workbook.create_sheet("Data").append(["Synthetic Holder Two"])
    workbook.save(path)
    readonly = openpyxl.load_workbook(path, read_only=True)
    try:
        sheet = readonly["Data"]
        assert isinstance(sheet, ReadOnlyWorksheet)
        sheet.reset_dimensions()
        yield sheet
    finally:
        readonly.close()


def _fail_scan(monkeypatch: pytest.MonkeyPatch, failure: BaseException) -> None:
    """Make the scan's sheet parser raise ``failure``; the real check runs."""

    def parse(_parser: WorkSheetParser) -> NoReturn:
        raise failure

    monkeypatch.setattr(WorkSheetParser, "parse", parse)


def test_valid_sheet_passes(worksheet: ReadOnlyWorksheet) -> None:
    require_cells_in_order(worksheet)


def test_os_error_propagates_as_the_same_object(
    worksheet: ReadOnlyWorksheet, monkeypatch: pytest.MonkeyPatch
) -> None:
    failure = OSError(errno.EIO, _STRERROR, _FILENAME)
    _fail_scan(monkeypatch, failure)

    with pytest.raises(OSError) as caught:
        require_cells_in_order(worksheet)

    assert caught.value is failure


@pytest.mark.parametrize("kind", [RuntimeError, AssertionError, MemoryError, RecursionError])
def test_crash_propagates_as_the_same_object(
    worksheet: ReadOnlyWorksheet, monkeypatch: pytest.MonkeyPatch, kind: type[Exception]
) -> None:
    failure = kind(_STRERROR)
    _fail_scan(monkeypatch, failure)

    with pytest.raises(kind) as caught:
        require_cells_in_order(worksheet)

    assert caught.value is failure


@pytest.mark.parametrize(
    "kind",
    [
        ValueError,
        IndexError,
        KeyError,
        TypeError,
        AttributeError,
        OverflowError,
        XMLParseError,
        BadZipFile,
        InvalidFileException,
        TokenizerError,
        TranslatorError,
        zlib.error,
        lzma.LZMAError,
    ],
)
def test_format_failure_raises_value_error(
    worksheet: ReadOnlyWorksheet, monkeypatch: pytest.MonkeyPatch, kind: type[Exception]
) -> None:
    failure = kind(_STRERROR)
    _fail_scan(monkeypatch, failure)

    with pytest.raises(ValueError) as caught:
        require_cells_in_order(worksheet)

    # A ValueError propagates as it is; any other format failure is its cause.
    assert failure in (caught.value, caught.value.__cause__)


class _RefusalError(Exception):
    """A stand-in for a reader's typed refusal."""


class _UserWarningSubclass(UserWarning):
    pass


def _read(refusal: Exception, *raised: tuple[type[Warning], str], then: Exception | None = None):
    """Raise each of ``raised`` inside the check, then ``then`` when given."""
    with RefuseOnWarning(refusal):
        for category, text in raised:
            warnings.warn(text, category, stacklevel=1)
        if then is not None:
            raise then


@pytest.mark.parametrize(
    "raised",
    [
        pytest.param((), id="none"),
        pytest.param(((UserWarning, ALLOWED_WARNING),), id="allowed"),
        pytest.param(((UserWarning, ALLOWED_WARNING),) * 2, id="allowed-twice"),
    ],
)
def test_no_warning_or_only_the_allowed_one_passes_and_none_escapes(
    raised: tuple[tuple[type[Warning], str], ...],
) -> None:
    with warnings.catch_warnings(record=True) as escaped:
        warnings.simplefilter("always")
        _read(_RefusalError(), *raised)

    assert escaped == []


@pytest.mark.parametrize(
    "raised",
    [
        pytest.param(((UserWarning, _STRERROR),), id="other-text"),
        pytest.param(((UserWarning, f"{ALLOWED_WARNING}."),), id="longer-text"),
        pytest.param(((DeprecationWarning, ALLOWED_WARNING),), id="other-category"),
        pytest.param(((_UserWarningSubclass, ALLOWED_WARNING),), id="subclass"),
        pytest.param(
            ((UserWarning, ALLOWED_WARNING), (UserWarning, _STRERROR)), id="allowed-then-other"
        ),
    ],
)
def test_any_other_warning_raises_the_refusal_and_none_escapes(
    raised: tuple[tuple[type[Warning], str], ...],
) -> None:
    refusal = _RefusalError()
    with warnings.catch_warnings(record=True) as escaped:
        warnings.simplefilter("always")
        with pytest.raises(_RefusalError) as caught:
            _read(refusal, *raised)
        # The check stops recording before it refuses: a later warning reaches the caller.
        warnings.warn(_FILENAME, stacklevel=1)

    assert caught.value is refusal
    assert caught.value.__cause__ is None
    assert caught.value.__suppress_context__
    assert [str(warning.message) for warning in escaped] == [_FILENAME]


def test_the_refusal_replaces_an_error_of_its_type_raised_after_a_warning() -> None:
    refusal = _RefusalError()

    with pytest.raises(_RefusalError) as caught:
        _read(refusal, (UserWarning, _STRERROR), then=_RefusalError(_FILENAME))

    assert caught.value is refusal
    assert caught.value.__suppress_context__


@pytest.mark.parametrize(
    "raised",
    [
        pytest.param((), id="no-warning"),
        pytest.param(((UserWarning, ALLOWED_WARNING),), id="allowed-warning"),
    ],
)
def test_an_error_without_another_warning_propagates_unchanged(
    raised: tuple[tuple[type[Warning], str], ...],
) -> None:
    failure = _RefusalError(_FILENAME)

    with pytest.raises(_RefusalError) as caught:
        _read(_RefusalError(), *raised, then=failure)

    assert caught.value is failure


@pytest.mark.parametrize("kind", [RuntimeError, OSError, MemoryError])
def test_an_error_of_another_type_after_a_warning_propagates_unchanged(
    kind: type[Exception],
) -> None:
    failure = kind(_STRERROR)

    with pytest.raises(kind) as caught:
        _read(_RefusalError(), (UserWarning, _STRERROR), then=failure)

    assert caught.value is failure
