"""The output declaration's command formats and selections (#1206)."""

from __future__ import annotations

import importlib
from collections.abc import Callable
from pathlib import Path
from types import MappingProxyType
from typing import Any, cast

import pytest

from ibkr_fa import output
from ibkr_fa.compute.form67 import Form67Readiness
from ibkr_fa.exceptions import InvariantError
from ibkr_fa.output import (
    _CATALOG,
    CG_FILING_WORKBOOK,
    INCOME_FILING,
    INCOME_REVIEW,
    OS_FILING_WORKBOOK,
    CommandFormats,
    OutputCatalogError,
    Subcommand,
    available_formats,
    command_formats,
    income_bundle,
    output_binding,
)
from ibkr_fa.output.bundle import spec_for
from ibkr_fa.output.netbenefits_quicko_fa_xlsx import QuickoFaXlsxWriter
from tests._source_all import INCOME_OUTPUTS_WITH_DEBUG

pytestmark = pytest.mark.usefixtures("isolated_output_registry")

_FA = (Subcommand.FA, Subcommand.FA_A2)


def test_selections_are_unchanged() -> None:
    assert INCOME_FILING == (
        (Subcommand.OS, "filing-csv"),
        (Subcommand.FSI, "filing-csv"),
        (Subcommand.TR, "filing-csv"),
        (Subcommand.FORM67, "portal-csv"),
    )
    assert INCOME_REVIEW == (
        (Subcommand.OS, "review-csv"),
        (Subcommand.FSI, "review-csv"),
        (Subcommand.TR, "review-csv"),
        (Subcommand.FORM67, "review-csv"),
    )
    assert CG_FILING_WORKBOOK == (Subcommand.CG_FILING, "filing-xlsx")
    assert OS_FILING_WORKBOOK == (Subcommand.OS, "filing-xlsx")


def test_all_command_formats_are_unchanged() -> None:
    """netbenefits-all files Schedule FA as the upload CSV, and run-all's
    default files Form 67 as the portal upload; no other command substitutes."""
    assert command_formats("netbenefits-all") == CommandFormats(_FA, (), "filing-csv")
    substituting = {
        command: dict(formats.substitutes)
        for command, formats in output._COMMAND_FORMATS.items()
        if formats.substitutes
    }
    assert substituting == {"run-all": {(Subcommand.FORM67, "review-csv"): "portal-csv"}}


def test_income_cleanup_names_do_not_follow_the_selections(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A selection orders what a run writes; the income bundle's filenames,
    which every caller clears and removes, stay its own (#1206)."""
    assert income_bundle.INCOME_FILENAMES == INCOME_OUTPUTS_WITH_DEBUG
    try:
        monkeypatch.setattr(output, "INCOME_FILING", INCOME_FILING[:3])
        monkeypatch.setattr(output, "INCOME_REVIEW", INCOME_REVIEW[:3])
        reloaded = importlib.reload(income_bundle)
        assert reloaded.INCOME_FILENAMES == INCOME_OUTPUTS_WITH_DEBUG
        assert (
            tuple(name for name in INCOME_OUTPUTS_WITH_DEBUG if not name.startswith("schedule-os"))
            == reloaded.FTC_FILENAMES
        )
        assert reloaded.income_output_names(
            form67_readiness=Form67Readiness.READY, debug=True
        ) == tuple(name for name in INCOME_OUTPUTS_WITH_DEBUG if not name.startswith("form-67"))
    finally:
        monkeypatch.undo()
        importlib.reload(income_bundle)
    assert income_bundle.INCOME_FILENAMES == INCOME_OUTPUTS_WITH_DEBUG


def _set_formats(monkeypatch: pytest.MonkeyPatch, command: str, formats: CommandFormats) -> None:
    monkeypatch.setitem(cast(Any, output._COMMAND_FORMATS), command, formats)


def _run_all_substitutes(
    substitutes: dict[tuple[Subcommand, str], str],
) -> Callable[[pytest.MonkeyPatch], None]:
    run_all = output._COMMAND_FORMATS["run-all"]
    replaced = run_all._replace(substitutes=MappingProxyType(substitutes))
    return lambda monkeypatch: _set_formats(monkeypatch, "run-all", replaced)


def _drop_selected_slot(monkeypatch: pytest.MonkeyPatch) -> None:
    del _CATALOG[CG_FILING_WORKBOOK]


_CORRUPTIONS: dict[str, tuple[Callable[[pytest.MonkeyPatch], None], str]] = {
    "default-not-a-choice": (
        lambda mp: _set_formats(
            mp,
            "form-67",
            CommandFormats((Subcommand.FORM67,), ("review-csv", "xlsx"), "portal-csv"),
        ),
        "'form-67'",
    ),
    "choice-not-a-slot": (
        lambda mp: _set_formats(
            mp,
            "schedule-cg",
            CommandFormats((Subcommand.CG,), ("filing-csv", "review-csv"), "review-csv"),
        ),
        "'schedule-cg'",
    ),
    "choice-missing-for-one-schedule": (
        lambda mp: _set_formats(
            mp,
            "schedule-os",
            CommandFormats((Subcommand.OS, Subcommand.FSI), ("filing-xlsx",), "filing-xlsx"),
        ),
        "'schedule-os'",
    ),
    "default-without-choices-not-a-slot": (
        lambda mp: _set_formats(mp, "netbenefits-all", CommandFormats(_FA, (), "filing-xlsx")),
        "'netbenefits-all'",
    ),
    "other-key-only-format": (
        lambda mp: _set_formats(
            mp,
            "robinhood-fa",
            CommandFormats(_FA, (QuickoFaXlsxWriter.format_id, "review-csv"), "review-csv"),
        ),
        "'robinhood-fa'",
    ),
    "substitute-for-missing-choice": (
        _run_all_substitutes({(Subcommand.FORM67, "portal-csv"): "review-csv"}),
        "'run-all'",
    ),
    "substitute-not-a-slot": (
        _run_all_substitutes({(Subcommand.FORM67, "review-csv"): "filing-csv"}),
        "'run-all'",
    ),
    "substitute-foreign-schedule": (
        _run_all_substitutes({(Subcommand.CG_FILING, "review-csv"): "filing-xlsx"}),
        "'run-all'",
    ),
    "selection-not-a-slot": (
        lambda mp: mp.setattr(output, "_SELECTED", (*output._SELECTED, (Subcommand.CG, "xlsv"))),
        "a selection lists a slot the catalog lacks",
    ),
    "catalog-drops-a-selected-slot": (
        _drop_selected_slot,
        "a selection lists a slot the catalog lacks",
    ),
}

_ACCESSORS: dict[str, Callable[[], object]] = {
    "command-formats": lambda: command_formats("schedule-tr"),
    "output-binding": lambda: output_binding(Subcommand.TR, "xlsx"),
    "available-formats": lambda: available_formats(Subcommand.TR),
    "spec-for": lambda: spec_for(Subcommand.TR, "xlsx", None, Path("tr.xlsx")),
}


@pytest.mark.parametrize("corruption", _CORRUPTIONS)
@pytest.mark.parametrize("accessor", _ACCESSORS)
@pytest.mark.parametrize("looked_up_before", [False, True], ids=["first-access", "after-lookup"])
def test_every_lookup_refuses_a_declaration_the_catalog_does_not_back(
    monkeypatch: pytest.MonkeyPatch, corruption: str, accessor: str, *, looked_up_before: bool
) -> None:
    """Every lookup checks the command formats and selections against the
    catalog, including after an earlier successful lookup (#1206)."""
    if looked_up_before:
        assert _ACCESSORS[accessor]() is not None
    corrupt, message = _CORRUPTIONS[corruption]
    corrupt(monkeypatch)
    with pytest.raises(OutputCatalogError, match=message):
        _ACCESSORS[accessor]()


def test_the_cbdt_json_fragment_is_the_only_unslotted_choice() -> None:
    fa_commands = ("schedule-fa", "netbenefits-fa", "robinhood-fa")
    for command, formats in output._COMMAND_FORMATS.items():
        unslotted = {
            choice
            for choice in formats.choices
            if any((schedule, choice) not in _CATALOG for schedule in formats.schedules)
        }
        assert unslotted == ({"cbdt-json-fragment"} if command in fa_commands else set())


@pytest.mark.parametrize(
    "command",
    ["schedule-fa-a2", "schedule-cg-filing", "stockplan-all", None, ["run-all"]],
    ids=["slot-only-schedule", "registry-only-schedule", "undeclared", "none", "unhashable"],
)
def test_command_formats_refuses_an_undeclared_command(command: object) -> None:
    with pytest.raises(InvariantError, match="command_formats requires a declared command"):
        command_formats(cast(Any, command))
