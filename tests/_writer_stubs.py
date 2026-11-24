"""Swap registered writers for test doubles (#644).

Pipelines build writers through the registry (``from_context``), so a test
double replaces a registry slot rather than a pipeline module's getter.
"""

from __future__ import annotations

from dataclasses import replace
from typing import Any, Self

import pytest

from ibkr_fa import output
from ibkr_fa.output.base import RenderContext, Subcommand
from ibkr_fa.output.filing_csv import PortalCsv, PortalCsvWriter
from ibkr_fa.output.filing_xlsx import FilingXlsx, FilingXlsxWriter


class StubWriter:
    """A writer double that ignores the run's context."""

    @classmethod
    def from_context(cls, _ctx: RenderContext) -> Self:
        return cls()


def patch_writers(monkeypatch: pytest.MonkeyPatch, subcommand: Subcommand, writer: type) -> None:
    """Make every ``subcommand`` format resolve to ``writer`` for this test.

    Each of the subcommand's slots is bound to a subclass of ``writer`` that
    declares the slot's ``format_id``, registered under the slot's key, as the
    catalog check requires (#790, #1200)."""
    for (slot, format_id), binding in output._CATALOG.items():
        if slot is subcommand:
            double = type(writer.__name__, (writer,), {"format_id": format_id})
            monkeypatch.setitem(output._REGISTRY, binding.key, double)
            monkeypatch.setitem(output._CATALOG, (slot, format_id), replace(binding, target=double))


def writer_class(subcommand: Subcommand, format_id: str) -> type[Any]:
    """The writer class ``subcommand``'s ``format_id`` slot targets; a slot the
    portal CSV renderer serves has none (#1201)."""
    target = output.output_binding(subcommand, format_id).target
    assert isinstance(target, type), f"{subcommand.value} {format_id} is not a writer class slot"
    return target


def writer_type(subcommand: Subcommand, format_id: str) -> type[Any]:
    """The type of the writer ``subcommand``'s ``format_id`` slot builds: its
    writer class, or a shared portal renderer's writer (#1201, #1202, #1204)."""
    target = output.output_binding(subcommand, format_id).target
    if isinstance(target, PortalCsv):
        return PortalCsvWriter
    if isinstance(target, FilingXlsx):
        return FilingXlsxWriter
    return target
