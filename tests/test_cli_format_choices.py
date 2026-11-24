"""Every subcommand's ``--format`` choices and default, written out literally (#644).

The choices and defaults come from the output declaration's command formats
(#1206). Re-keying the registry or the declaration must not add, drop or
reorder one.
"""

from __future__ import annotations

import argparse
from typing import Any, cast

import pytest

from ibkr_fa import output
from ibkr_fa.cli import _build_parser
from ibkr_fa.output import CommandFormats, Subcommand

_FORMAT_ARGUMENTS: dict[str, tuple[list[str], str]] = {
    "robinhood-cg": (["review-csv", "xlsx"], "review-csv"),
    "robinhood-fa": (["cbdt-json-fragment", "filing-csv", "review-csv", "xlsx"], "review-csv"),
    "robinhood-all": (["review-csv", "xlsx"], "review-csv"),
    "schedule-fa": (["filing-csv", "review-csv", "xlsx", "cbdt-json-fragment"], "review-csv"),
    "netbenefits-fa": (["filing-csv", "review-csv", "cbdt-json-fragment"], "filing-csv"),
    "fidelity-cma-fa": (["filing-csv", "review-csv", "xlsx"], "review-csv"),
    "schedule-os": (["filing-csv", "filing-xlsx", "review-csv", "xlsx"], "review-csv"),
    "schedule-cg": (["review-csv", "xlsx"], "review-csv"),
    "schedule-cg-ltcg": (["review-csv", "xlsx"], "review-csv"),
    "schedule-fsi": (["filing-csv", "review-csv", "xlsx"], "review-csv"),
    "schedule-tr": (["filing-csv", "review-csv", "xlsx"], "review-csv"),
    "form-67": (["portal-csv", "review-csv", "xlsx"], "portal-csv"),
    "run-all": (["review-csv", "xlsx"], "review-csv"),
}


def _format_arguments() -> dict[str, tuple[list[str], str]]:
    subparsers = next(
        action
        for action in _build_parser()._actions
        if isinstance(action, argparse._SubParsersAction)
    )
    found: dict[str, tuple[list[str], str]] = {}
    for command, parser in subparsers.choices.items():
        for action in parser._actions:
            if "--format" in action.option_strings:
                assert action.choices is not None
                found[command] = (list(action.choices), action.default)
    return found


def test_format_choices_and_defaults_are_unchanged() -> None:
    assert _format_arguments() == _FORMAT_ARGUMENTS


def test_every_format_argument_comes_from_the_declaration() -> None:
    """Each ``--format`` lists its command's declared choices and default, and a
    declared command without choices has no ``--format`` (#1206)."""
    declared = output._COMMAND_FORMATS
    assert _format_arguments() == {
        command: (list(formats.choices), formats.default)
        for command, formats in declared.items()
        if formats.choices
    }
    assert {command for command, formats in declared.items() if not formats.choices} == {
        "netbenefits-all"
    }


def test_format_arguments_follow_a_changed_declaration(monkeypatch: pytest.MonkeyPatch) -> None:
    changed = CommandFormats((Subcommand.TR,), ("xlsx", "review-csv"), "xlsx")
    monkeypatch.setitem(cast(Any, output._COMMAND_FORMATS), "schedule-tr", changed)
    assert _format_arguments()["schedule-tr"] == (["xlsx", "review-csv"], "xlsx")
