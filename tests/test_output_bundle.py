"""Tests for :mod:`ibkr_fa.output.bundle` (#644)."""

from __future__ import annotations

from pathlib import Path
from typing import ClassVar, Self

import pytest

from ibkr_fa import output
from ibkr_fa.output.base import (
    OutputCatalogError,
    RenderContext,
    Subcommand,
    UnknownOutputFormatError,
)
from ibkr_fa.output.bundle import OutputSpec, spec_for, write_output_bundle
from tests._writer_stubs import patch_writers

_CTX = RenderContext(assessment_year_start=2026, itr_form="ITR-3", source_label="Test")


class _Recording:
    """Writes its payload and records the context it was built from."""

    contexts: ClassVar[list[RenderContext]] = []

    @classmethod
    def from_context(cls, ctx: RenderContext) -> Self:
        cls.contexts.append(ctx)
        return cls()

    def write(self, value: object, path: Path) -> None:
        path.write_text(str(value), encoding="utf-8")


class _Refusing(_Recording):
    def write(self, value: object, path: Path) -> None:
        path.write_text("partial", encoding="utf-8")
        raise OSError("disk full")


@pytest.fixture(autouse=True)
def _reset_contexts() -> None:
    _Recording.contexts.clear()


def test_writes_every_spec_in_order_with_the_context(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    patch_writers(monkeypatch, Subcommand.OS, _Recording)
    patch_writers(monkeypatch, Subcommand.TR, _Recording)
    specs = [
        spec_for(Subcommand.TR, "filing-csv", "tr", tmp_path / "tr.csv"),
        spec_for(Subcommand.OS, "review-csv", "os", tmp_path / "os.csv"),
    ]

    written = write_output_bundle(specs, _CTX)

    assert written == (tmp_path / "tr.csv", tmp_path / "os.csv")
    assert [path.read_text(encoding="utf-8") for path in written] == ["tr", "os"]
    assert _Recording.contexts == [_CTX, _CTX]


def test_spec_for_keeps_the_slot_and_refuses_an_unknown_format() -> None:
    assert spec_for(Subcommand.OS, "filing-xlsx", "payload", Path("os.xlsx")) == OutputSpec(
        Subcommand.OS, "filing-xlsx", "payload", Path("os.xlsx")
    )
    with pytest.raises(UnknownOutputFormatError, match="'portal-csv'"):
        spec_for(Subcommand.OS, "portal-csv", "payload", Path("os.csv"))


@pytest.mark.usefixtures("isolated_output_registry")
def test_writing_looks_the_binding_up_again(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A catalog corrupted after ``spec_for`` refuses before any write (#1200)."""
    patch_writers(monkeypatch, Subcommand.OS, _Recording)
    specs = [spec_for(Subcommand.OS, "review-csv", "os", tmp_path / "os.csv")]
    del output._CATALOG[(Subcommand.OS, "xlsx")]

    with pytest.raises(OutputCatalogError, match="no catalog entry"):
        write_output_bundle(specs, _CTX)

    assert list(tmp_path.iterdir()) == []
    assert _Recording.contexts == []


def test_empty_spec_list_writes_nothing(tmp_path: Path) -> None:
    assert write_output_bundle([], _CTX) == ()
    assert list(tmp_path.iterdir()) == []


def test_a_failing_spec_stops_the_rest_and_leaves_cleanup_to_the_caller(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The bundle removes nothing: the caller's ``remove_outputs_on_failure``
    window, which also covers its report, does."""
    patch_writers(monkeypatch, Subcommand.OS, _Recording)
    patch_writers(monkeypatch, Subcommand.FSI, _Refusing)
    patch_writers(monkeypatch, Subcommand.TR, _Recording)
    specs = [
        spec_for(Subcommand.OS, "filing-csv", "os", tmp_path / "os.csv"),
        spec_for(Subcommand.FSI, "filing-csv", "fsi", tmp_path / "fsi.csv"),
        spec_for(Subcommand.TR, "filing-csv", "tr", tmp_path / "tr.csv"),
    ]

    with pytest.raises(OSError, match="disk full"):
        write_output_bundle(specs, _CTX)

    assert sorted(path.name for path in tmp_path.iterdir()) == ["fsi.csv", "os.csv"]
    assert len(_Recording.contexts) == 2
