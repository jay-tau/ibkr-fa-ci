"""Write a run's registered outputs in order (#644).

A pipeline lists what it writes as :class:`OutputSpec` entries and calls
:func:`write_output_bundle` inside its own
:func:`~ibkr_fa.output._cleanup.remove_outputs_on_failure` window. The bundle
does no cleanup itself: the caller's window also covers the report it builds
after the writes, so it removes every target if anything raises.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

from ibkr_fa.output import output_binding
from ibkr_fa.output.base import RenderContext, Subcommand


@dataclass(frozen=True)
class OutputSpec:
    """One output: its catalog slot, the value it writes, and where."""

    subcommand: Subcommand
    format_id: str
    payload: object
    path: Path


def spec_for(subcommand: Subcommand, format_id: str, payload: object, path: Path) -> OutputSpec:
    """The spec for ``subcommand``'s ``format_id``, refused now if the catalog
    has no such slot (see :func:`~ibkr_fa.output.output_binding`)."""
    output_binding(subcommand, format_id)
    return OutputSpec(subcommand, format_id, payload, path)


def write_output_bundle(specs: Iterable[OutputSpec], ctx: RenderContext) -> tuple[Path, ...]:
    """Look up each spec's binding again, build its writer from ``ctx`` and
    write its payload, in order.

    Returns:
        The written paths, in spec order.

    Raises:
        UnknownOutputFormatError: If the catalog no longer lists a spec's slot.
        OutputCatalogError: See :func:`~ibkr_fa.output.output_binding`.
        Whatever a writer raises; the later specs are not written.
    """
    written: list[Path] = []
    for spec in specs:
        binding = output_binding(spec.subcommand, spec.format_id)
        binding.writer(ctx).write(spec.payload, spec.path)
        written.append(spec.path)
    return tuple(written)
