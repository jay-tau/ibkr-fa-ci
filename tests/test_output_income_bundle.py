"""Tests for :mod:`ibkr_fa.output.income_bundle` (#646)."""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from typing import Any, ClassVar, Self, cast

import pytest

from ibkr_fa.compute.form67 import Form67Readiness
from ibkr_fa.compute.schedule_fsi import ScheduleFsiComputeResult
from ibkr_fa.compute.schedule_tr import ScheduleTrComputeResult
from ibkr_fa.exceptions import InvariantError
from ibkr_fa.models.schedule_fsi_row import HeadOfIncome, ScheduleFsiRow
from ibkr_fa.output.base import RenderContext, Subcommand
from ibkr_fa.output.income_bundle import income_output_names, write_income_outputs
from tests._writer_stubs import patch_writers

_CTX = RenderContext(assessment_year_start=2026, itr_form="ITR-3", source_label="Test source")
_OS = object()
_FSI = ScheduleFsiComputeResult(rows=[])
_TR = ScheduleTrComputeResult(rows=[])


class _Recording:
    """Writes ``<format_id>`` to the path and records each call."""

    format_id: ClassVar[str]
    calls: ClassVar[list[tuple[str, str, object, RenderContext]]] = []

    def __init__(self, ctx: RenderContext) -> None:
        self._ctx = ctx

    @classmethod
    def from_context(cls, ctx: RenderContext) -> Self:
        return cls(ctx)

    def write(self, payload: object, path: Path) -> None:
        path.write_text(self.format_id, encoding="utf-8")
        _Recording.calls.append((path.name, self.format_id, payload, self._ctx))


@pytest.fixture(autouse=True)
def _recording_writers(monkeypatch: pytest.MonkeyPatch) -> None:
    _Recording.calls.clear()
    for subcommand in (Subcommand.OS, Subcommand.FSI, Subcommand.TR, Subcommand.FORM67):
        patch_writers(monkeypatch, subcommand, _Recording)


def _form67(readiness: Form67Readiness) -> Any:
    # No entries, so no section 91 credit for the portal check to read (#1177).
    return cast(Any, SimpleNamespace(readiness=readiness, entries=(), _section_91_checks={}))


def _write(
    out: Path, *, readiness: Form67Readiness, debug: bool, schedule_os: bool = True
) -> tuple[Path, ...]:
    return write_income_outputs(
        schedule_os=cast(Any, _OS) if schedule_os else None,
        fsi=cast(Any, _FSI),
        tr=cast(Any, _TR),
        form67=_form67(readiness),
        output_dir=out,
        context=_CTX,
        debug=debug,
    )


_OS_FILES = ("schedule-os.csv", "schedule-os-debug.csv")
_READY = Form67Readiness.READY
_BLANK = Form67Readiness.BLANK
_SELECTIONS = [
    (_READY, False, ("schedule-os.csv", "schedule-fsi.csv", "schedule-tr.csv", "form-67.csv")),
    (
        _READY,
        True,
        (
            "schedule-os.csv",
            "schedule-fsi.csv",
            "schedule-tr.csv",
            "form-67.csv",
            "schedule-os-debug.csv",
            "schedule-fsi-debug.csv",
            "schedule-tr-debug.csv",
            "form-67-debug.csv",
        ),
    ),
    (
        _BLANK,
        False,
        ("schedule-os.csv", "schedule-fsi.csv", "schedule-tr.csv", "form-67-debug.csv"),
    ),
    (
        _BLANK,
        True,
        (
            "schedule-os.csv",
            "schedule-fsi.csv",
            "schedule-tr.csv",
            "schedule-os-debug.csv",
            "schedule-fsi-debug.csv",
            "schedule-tr-debug.csv",
            "form-67-debug.csv",
        ),
    ),
]


@pytest.mark.parametrize("schedule_os", [True, False], ids=["with-os", "without-os"])
@pytest.mark.parametrize(
    ("readiness", "debug", "expected"),
    _SELECTIONS,
    ids=["ready", "ready-debug", "blank", "blank-debug"],
)
def test_writes_the_selected_files_in_order(
    tmp_path: Path,
    readiness: Form67Readiness,
    debug: bool,
    expected: tuple[str, ...],
    schedule_os: bool,
) -> None:
    if not schedule_os:
        expected = tuple(name for name in expected if name not in _OS_FILES)

    written = _write(tmp_path, readiness=readiness, debug=debug, schedule_os=schedule_os)

    assert written == tuple(tmp_path / name for name in expected)
    assert sorted(path.name for path in tmp_path.iterdir()) == sorted(expected)
    assert [name for name, *_ in _Recording.calls] == list(expected)


@pytest.mark.parametrize("readiness", [_READY, _BLANK], ids=["ready", "blank"])
@pytest.mark.parametrize("debug", [False, True], ids=["plain", "debug"])
def test_a_withheld_form67_writes_what_a_blank_one_writes(
    tmp_path: Path, readiness: Form67Readiness, debug: bool
) -> None:
    """Fidelity CMA withholds form-67.csv while its FTC is blocked (#1453)."""
    [expected] = [names for r, d, names in _SELECTIONS if r is _BLANK and d is debug]

    written = write_income_outputs(
        schedule_os=cast(Any, _OS),
        fsi=cast(Any, _FSI),
        tr=cast(Any, _TR),
        form67=_form67(readiness),
        output_dir=tmp_path,
        context=_CTX,
        debug=debug,
        withhold_form67=True,
    )

    assert written == tuple(tmp_path / name for name in expected)
    assert sorted(path.name for path in tmp_path.iterdir()) == sorted(expected)


@pytest.mark.parametrize(
    ("readiness", "debug", "expected"),
    _SELECTIONS,
    ids=["ready", "ready-debug", "blank", "blank-debug"],
)
def test_output_names_match_a_run_with_schedule_os(
    readiness: Form67Readiness, debug: bool, expected: tuple[str, ...]
) -> None:
    assert income_output_names(form67_readiness=readiness, debug=debug) == expected


def test_each_file_gets_its_writer_payload_and_the_run_context(tmp_path: Path) -> None:
    form67 = _form67(_READY)

    write_income_outputs(
        schedule_os=cast(Any, _OS),
        fsi=cast(Any, _FSI),
        tr=cast(Any, _TR),
        form67=form67,
        output_dir=tmp_path,
        context=_CTX,
        debug=True,
    )

    assert [call[:3] for call in _Recording.calls] == [
        ("schedule-os.csv", "filing-csv", _OS),
        ("schedule-fsi.csv", "filing-csv", _FSI),
        ("schedule-tr.csv", "filing-csv", _TR),
        ("form-67.csv", "portal-csv", form67),
        ("schedule-os-debug.csv", "review-csv", _OS),
        ("schedule-fsi-debug.csv", "review-csv", _FSI),
        ("schedule-tr-debug.csv", "review-csv", _TR),
        ("form-67-debug.csv", "review-csv", form67),
    ]
    assert {call[3].source_label for call in _Recording.calls} == {"Test source"}


def test_refused_form67_is_an_invariant_error_before_any_write(tmp_path: Path) -> None:
    with pytest.raises(InvariantError, match="REFUSED Form 67"):
        _write(tmp_path, readiness=Form67Readiness.REFUSED, debug=True)
    with pytest.raises(InvariantError, match="REFUSED Form 67"):
        income_output_names(form67_readiness=Form67Readiness.REFUSED, debug=False)

    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("error", [OSError("disk full"), KeyboardInterrupt()], ids=type)
@pytest.mark.parametrize("position", range(8))
def test_a_failing_write_propagates_and_stops_the_later_writes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, position: int, error: BaseException
) -> None:
    """The helper leaves cleanup to the caller's window, which also covers the
    report built after the writes."""
    expected = _SELECTIONS[1][2]
    original = _Recording.write

    def write(self: _Recording, payload: object, path: Path) -> None:
        if path.name == expected[position]:
            raise error
        original(self, payload, path)

    monkeypatch.setattr(_Recording, "write", write)

    with pytest.raises(type(error)) as raised:
        _write(tmp_path, readiness=_READY, debug=True)

    assert raised.value is error
    assert sorted(path.name for path in tmp_path.iterdir()) == sorted(expected[:position])


def _overwritten_relief_row() -> ScheduleFsiRow:
    """Relief ₹250 where the portal would fill min(₹300, ₹400) = ₹300 (#900)."""
    return ScheduleFsiRow(
        cbdt_country_code="2",
        country_name_portal="UNITED STATES OF AMERICA",
        iso_alpha2="US",
        head_of_income=HeadOfIncome.OTHER_SOURCES,
        income_inr=Decimal("1000"),
        foreign_tax_inr=Decimal("300"),
        event_count=1,
        tax_payable_inr=Decimal("400"),
        tax_relief_inr=Decimal("250"),
        dtaa_article="Article 25(2)(a)",
    )


@pytest.mark.parametrize("carrier", ["fsi", "tr"])
def test_relief_the_portal_overwrites_refuses_before_any_write(
    tmp_path: Path, carrier: str
) -> None:
    row = _overwritten_relief_row()
    fsi = ScheduleFsiComputeResult(rows=[row]) if carrier == "fsi" else _FSI
    tr = (
        ScheduleTrComputeResult(rows=[], fsi_rows_with_overwritten_relief=(row,))
        if carrier == "tr"
        else _TR
    )

    with pytest.raises(InvariantError, match="read-only relief"):
        write_income_outputs(
            schedule_os=cast(Any, _OS),
            fsi=fsi,
            tr=tr,
            form67=_form67(_READY),
            output_dir=tmp_path,
            context=_CTX,
            debug=True,
        )

    assert _Recording.calls == []
    assert list(tmp_path.iterdir()) == []
