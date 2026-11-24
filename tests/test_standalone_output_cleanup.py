"""Standalone schedules leave no output behind when they refuse (#678, #693).

``schedule-os``, ``schedule-cg``, ``schedule-cg-ltcg``, ``schedule-fsi``,
``schedule-tr`` and non-JSON ``schedule-fa`` remove every target (``--output``,
plus ``schedule-fa``'s ``-a2`` sibling) before they read config or statement,
and again when a write fails, so a run that exits 4 leaves no earlier or partial
file that could be filed. A target that cannot be removed refuses the run, as
it does for ``form-67`` (#736).

A refusal after the write, while the run builds its report, removes the run's
own output too (#693), as do ``form-67`` (with its debug file) and JSON
``schedule-fa``. JSON ``schedule-fa`` removes its fragment on any failure once
it is published, interrupts included, but never a file it did not publish (#774).
"""

from __future__ import annotations

import errno
import json
import os
import re
import zipfile
from collections.abc import Callable, Sized
from dataclasses import replace
from pathlib import Path
from typing import NoReturn
from unittest.mock import create_autospec

import pytest

from ibkr_fa.cli import main
from ibkr_fa.exceptions import ConfigError, IbkrFaError, InvariantError, ParseError
from ibkr_fa.output import OutputBinding, RenderContext, Subcommand, output_binding
from ibkr_fa.output._cleanup import FileIdentity, OutputCollisionError, OutputRemovalError
from ibkr_fa.output.cbdt_json_fragment import (
    CbdtJsonIncompleteError,
    CbdtScheduleFaFragmentInput,
    CbdtScheduleFaJsonWriter,
    is_ibkr_fa_cbdt_fragment,
)
from ibkr_fa.pipeline_fa_models import RunReport
from tests._cbdt_fragment_inputs import fail_staging_fstat
from tests._crash_reports import (
    assert_no_markers,
    assert_prints_no_marker,
    assert_replaced_unchained,
)
from tests._fa_a3_name_runs import A3_ASCII_ERROR
from tests._ibkr_schedule_fa_inputs import SCHEDULE_FA_CONFIG_NO_PEAK as _CONFIG_NO_PEAK
from tests._ibkr_standalone_commands import ALL_STANDALONE_COMMANDS as _ALL_COMMANDS
from tests._ibkr_standalone_commands import CY_STATEMENT as _CY_STATEMENT
from tests._ibkr_standalone_commands import FY_ARGS as _FY_ARGS
from tests._ibkr_standalone_commands import FY_STATEMENT as _FY_STATEMENT
from tests._ibkr_standalone_commands import ITR_ARGS as _ITR
from tests._ibkr_standalone_commands import STANDALONE_ACCOUNT as _ACCOUNT
from tests._ibkr_standalone_commands import STANDALONE_COMMANDS as _COMMANDS
from tests._ibkr_standalone_commands import fa_json_inputs as _json_inputs
from tests._ibkr_standalone_commands import run_fa_json as _run_json
from tests._ibkr_standalone_commands import run_standalone_cli as _cli
from tests._ibkr_standalone_commands import run_standalone_entry as _entry
from tests._ibkr_standalone_commands import standalone_targets as _targets
from tests._ibkr_standalone_commands import write_standalone_inputs as _write_inputs
from tests._oserror_markers import FILENAME_MARKER as _HOLDER
from tests._oserror_markers import STRERROR_MARKER as _STRERROR
from tests._output_removal_failures import CLEANUP_LAYERS as _LAYERS
from tests._output_removal_failures import STALE_BYTES as _STALE
from tests._output_removal_failures import existing_names as _existing
from tests._output_removal_failures import fail_unlink as _fail_unlink
from tests._output_removal_failures import make_unremovable as _make_unremovable
from tests._output_removal_failures import marker_permission_error as _marker_error
from tests._output_removal_failures import refuse_after_write as _refuse_after_write
from tests._text_edits import replace_once
from tests._writer_stubs import writer_type


def _assert_omits_markers(text: str) -> None:
    for marker in (_HOLDER, _STRERROR):
        assert marker.lower() not in text.lower()


# Every (command, index of a target) pair.
_TARGETS = [(name, index) for name, cmd in _COMMANDS.items() for index in range(len(cmd.targets))]
_ALL_TARGETS = [
    (name, index) for name, cmd in _ALL_COMMANDS.items() for index in range(len(cmd.targets))
]


def _stale_targets(out: Path, name: str, suffix: str = ".csv") -> list[Path]:
    targets = _targets(out, name, suffix)
    for target in targets:
        target.write_bytes(_STALE)
    return targets


_IRELAND = ("BrokerName,Interactive Brokers LLC", "BrokerName,Interactive Brokers Ireland Limited")
# #915: the first trade's Date/Time with a timezone suffix. Every command but
# schedule-os parses Trades.
_AWARE_TRADE = {
    _FY_STATEMENT: ('"2025-06-15, 10:00:00"', '"2025-06-15, 10:00:00+00:00"'),
    _CY_STATEMENT: ('"2025-01-15, 10:30:00"', '"2025-01-15, 10:30:00+00:00"'),
}
# #908: an Interest block whose 5.00 row was lost, so its rows add up to
# 10.00 but its Total says 15.00. Every command parses Interest.
_LOST_ROW = (
    "Interest,Header,Currency,Date,Description,Amount\n"
    "Interest,Data,USD,2025-06-30,USD Credit Interest for Jun-2025,10.00\n"
    "Interest,Data,Total,,,15.00\n"
)
_RU_SUMMARY = "Realized & Unrealized Performance Summary"
# Text appended to the statement, which every command splits (#908, #913).
_APPENDED = {
    "total": _LOST_ROW,
    # A section on no list.
    "section": "Transfers,Header,Symbol,Quantity\nTransfers,Data,ABC,10\n",
    # A row type a parsed section doesn't have.
    "row-type": (
        "Interest,Header,Currency,Date,Description,Amount\n"
        "Interest,Adjustment,USD,2025-06-30,USD Credit Interest for Jun-2025,-3.00\n"
    ),
    # A column a parsed section neither reads nor ignores.
    "column": "Dividends,Header,Currency,Date,Description,Amount,Adjustment\n",
    "cost-adjustment": (
        f"{_RU_SUMMARY},Header,Asset Category,Symbol,Cost Adj.,Realized Total\n"
        f"{_RU_SUMMARY},Data,Stocks,ABC,-5.00,1.00\n"
    ),
    # A SubTotal or Total row in a section that defines none (#1012).
    "aggregation-row": (
        "Net Asset Value,Header,Asset Class,Prior Total,Current Long,Current Short,"
        "Current Total,Change\n"
        "Net Asset Value,Total,Total,0,150.00,0,150.00,150.00\n"
    ),
}
_REFUSALS = [
    *((name, "account") for name in _COMMANDS),
    *((name, "period") for name in _COMMANDS),
    ("schedule-fsi", "broker"),
    ("schedule-tr", "broker"),
    *((name, "trade-time") for name in _ALL_COMMANDS if name != "schedule-os"),
    *((name, refusal) for name in _ALL_COMMANDS for refusal in _APPENDED),
]
_REFUSAL_ERRORS: dict[str, tuple[type[Exception], str]] = {
    "account": (ConfigError, "'account.number'"),
    "period": (ParseError, "section 'Statement' at row 0, column 'Period'"),
    "broker": (ParseError, "section 'Statement' at row 0, column 'BrokerName'"),
    "trade-time": (ParseError, "section 'Trades' at row 0, column 'Date/Time'"),
    "total": (ParseError, "doesn't equal the sum of the rows on line"),
    "section": (ParseError, "belongs to a section this tool neither parses nor lists"),
    "row-type": (ParseError, "has a row type other than 'Header', 'Data', 'SubTotal' or"),
    "column": (
        ParseError,
        "has a column name 5 (the first name after 'Header' is 1) that this block shape",
    ),
    "cost-adjustment": (ParseError, "has a 'Cost Adj.' cell that isn't blank or a zero decimal"),
    "aggregation-row": (
        ParseError,
        "is a 'Total' row, but the 'Net Asset Value' section defines no 'SubTotal'",
    ),
}


@pytest.mark.parametrize("layer", _LAYERS)
@pytest.mark.parametrize(("name", "refusal"), _REFUSALS)
def test_refusal_leaves_no_previous_output(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    name: str,
    refusal: str,
    layer: str,
) -> None:
    command = _ALL_COMMANDS[name]
    text = command.statement.read_text(encoding="utf-8")
    if refusal == "period":
        text = replace_once(text, *command.period)
    elif refusal == "broker":
        text = replace_once(text, *_IRELAND)
    elif refusal == "trade-time":
        text = replace_once(text, *_AWARE_TRADE[command.statement])
    elif refusal in _APPENDED:
        text += _APPENDED[refusal]
    number = "U99999999" if refusal == "account" else _ACCOUNT
    statement, config = _write_inputs(tmp_path, name, number=number, statement_text=text)
    targets = _stale_targets(tmp_path / "out", name)
    error, message = _REFUSAL_ERRORS[refusal]

    if layer == "cli":
        assert _cli(name, statement, config, targets[0]) == 4
        assert message in capsys.readouterr().err
    else:
        with pytest.raises(error, match=re.escape(message)):
            _entry(name, statement, config, targets[0])

    assert _existing(targets) == []


@pytest.mark.parametrize("layer", _LAYERS)
@pytest.mark.parametrize("name", _ALL_COMMANDS)
def test_total_typed_account_row_refuses_under_the_opt_in(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], name: str, layer: str
) -> None:
    """#1012: the statement's Account row, typed Total and holding another
    account's number, used to be dropped; under the anonymised-statement
    opt-in the configured number then stood in for it, so another account's
    statement filed under this one. Every command now refuses, without
    printing the number, and leaves no previous output."""
    text = replace_once(
        _ALL_COMMANDS[name].statement.read_text(encoding="utf-8"),
        f"Account Information,Data,Account,{_ACCOUNT},",
        "Account Information,Total,Account,U99999999,",
    )
    statement, config = _write_inputs(tmp_path, name, statement_text=text)
    targets = _stale_targets(tmp_path / "out", name)
    message = "is a 'Total' row, but the 'Account Information' section defines no 'SubTotal'"

    if layer == "cli":
        assert _cli(name, statement, config, targets[0], "--accept-anonymised-statement") == 4
        err = capsys.readouterr().err
    else:
        with pytest.raises(ParseError) as exc:
            _entry(name, statement, config, targets[0], accept_anonymised_statement=True)
        err = str(exc.value)
    assert message in err
    assert "U99999999" not in err

    assert _existing(targets) == []


@pytest.mark.parametrize("format_id", ["filing-csv", "review-csv", "xlsx"])
@pytest.mark.parametrize("name", ["schedule-fsi", "schedule-tr"])
@pytest.mark.parametrize("layer", _LAYERS)
def test_relief_refusal_after_the_compute_leaves_no_previous_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, layer: str, name: str, format_id: str
) -> None:
    """#900: every Schedule FSI and TR format checks the compute flag."""

    def refuse(*_args: object, **_kwargs: object) -> None:
        raise InvariantError(what="filing relief does not reconcile", context=())

    module = "pipeline_fsi" if name == "schedule-fsi" else "pipeline_tr"
    monkeypatch.setattr(f"ibkr_fa.{module}.require_fsi_write_ready", refuse)
    writer = writer_type(Subcommand.FSI if name == "schedule-fsi" else Subcommand.TR, format_id)
    write = create_autospec(writer.write, side_effect=writer.write)
    monkeypatch.setattr(writer, "write", write)
    statement, config = _write_inputs(tmp_path, name)
    suffix = ".xlsx" if format_id == "xlsx" else ".csv"
    targets = _stale_targets(tmp_path / "out", name, suffix)

    if layer == "cli":
        assert _cli(name, statement, config, targets[0], "--format", format_id) == 4
    else:
        with pytest.raises(InvariantError, match="filing relief does not reconcile"):
            _entry(name, statement, config, targets[0], format_id=format_id)

    write.assert_not_called()
    assert _existing(targets) == []


class _WriteFailedError(OSError):
    pass


def _install_partial_writer(monkeypatch: pytest.MonkeyPatch, name: str) -> list[list[str]]:
    """Replace the command's (A3) writer with one that writes a partial file and
    raises. ``schedule-fa``'s A2 writer, in ``pipeline_fa_a2``, still runs first.
    Returns the target folder's listing as each write began."""
    writes: list[list[str]] = []

    class PartialWriter:
        @classmethod
        def from_context(cls, _ctx: RenderContext) -> PartialWriter:
            return cls()

        def write(self, _result: object, path: Path) -> None:
            writes.append(sorted(entry.name for entry in path.parent.iterdir()))
            path.write_bytes(b"partial")
            raise _WriteFailedError(28, "No space left on device")

    monkeypatch.setattr(
        f"{_COMMANDS[name].module}.output_binding",
        lambda subcommand, format_id: replace(
            output_binding(subcommand, format_id), target=PartialWriter
        ),
    )
    return writes


def _format(suffix: str) -> tuple[tuple[str, ...], dict[str, object]]:
    if suffix == ".xlsx":
        return ("--format", "xlsx"), {"format_id": "xlsx"}
    return (), {}


@pytest.mark.parametrize("layer", _LAYERS)
@pytest.mark.parametrize("suffix", [".csv", ".xlsx"])
@pytest.mark.parametrize("name", _COMMANDS)
def test_failed_write_leaves_no_output(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    name: str,
    suffix: str,
    layer: str,
) -> None:
    """The entry point raises the filesystem error; the CLI refuses with its
    errno name (exit 4, #832, #1049)."""
    statement, config = _write_inputs(tmp_path, name)
    targets = _stale_targets(tmp_path / "out", name, suffix)
    writes = _install_partial_writer(monkeypatch, name)
    cli_format, entry_format = _format(suffix)

    if layer == "cli":
        assert _cli(name, statement, config, targets[0], *cli_format) == 4
        # Fixed text and the errno name, never the OS text (#1049).
        assert capsys.readouterr().err == f"error: {name}: a file operation failed (ENOSPC)\n"
    else:
        with pytest.raises(_WriteFailedError):
            _entry(name, statement, config, targets[0], **entry_format)

    # The stale files were gone before the write; schedule-fa had written A2.
    assert writes == [[target.name for target in targets[1:]]]
    assert _existing(targets) == []


_LENGTH_MARKER = "SYNTHLENNAME"
# A configured entity name for the statement's first holding (#1189).
_ENTITY_NAME = (
    '\n[[fa.entities]]\nisin = "US99995B1044"\naddress = "1 Sample Street"\nname = "{}"\n'
)


@pytest.mark.parametrize(
    ("edit", "message", "marker"),
    [
        pytest.param(
            lambda config: replace_once(
                config, "Interactive Brokers LLC", _LENGTH_MARKER.ljust(126, "X")
            ),
            "InvariantError: FaA2Row.institution_name must be at most 125 characters",
            _LENGTH_MARKER,
            id="a2-row-length",
        ),
        pytest.param(
            lambda config: config + _ENTITY_NAME.format(_LENGTH_MARKER.ljust(125, "É")),
            A3_ASCII_ERROR,
            _LENGTH_MARKER,
            id="a3-writer-ascii",
        ),
    ],
)
@pytest.mark.parametrize("layer", _LAYERS)
def test_typed_refusal_keeps_its_domain_error(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    layer: str,
    edit: Callable[[str], str],
    message: str,
    marker: str,
) -> None:
    """A row's own refusal and a writer's both exit 4 with their fixed text,
    never the value, and leave no output (#691, #1189)."""
    statement, config = _write_inputs(tmp_path, "schedule-fa")
    config.write_text(edit(config.read_text(encoding="utf-8")), encoding="utf-8")
    targets = _stale_targets(tmp_path / "out", "schedule-fa")

    if layer == "cli":
        assert _cli("schedule-fa", statement, config, targets[0], "--format", "filing-csv") == 4
        captured = capsys.readouterr()
        assert captured.err == f"error: {message}\n"
        assert_no_markers(captured.out, marker)
    else:
        with pytest.raises(InvariantError) as exc:
            _entry(
                "schedule-fa",
                statement,
                config,
                targets[0],
                format_id="filing-csv",
                itr_form="ITR-2",
            )
        assert type(exc.value) is InvariantError
        assert str(exc.value) == message
        assert_prints_no_marker(exc.value, markers=(marker,))

    assert _existing(targets) == []


@pytest.mark.parametrize("layer", _LAYERS)
@pytest.mark.parametrize("how", ["directory", "permission"])
@pytest.mark.parametrize(("name", "index"), _ALL_TARGETS)
def test_previous_output_that_cannot_be_removed_refuses_before_reading(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    name: str,
    index: int,
    how: str,
    layer: str,
) -> None:
    """A missing statement and a malformed config prove nothing was read: either
    would refuse with its own error had the run got that far."""
    statement = tmp_path / "missing-statement.csv"
    config = tmp_path / "user.toml"
    config.write_bytes(b"not = [valid toml")
    targets = _stale_targets(tmp_path / "out", name)
    failing = targets[index]
    _make_unremovable(monkeypatch, failing, how)

    if layer == "cli":
        assert _cli(name, statement, config, targets[0]) == 4
        err = capsys.readouterr().err
        assert "could not remove the previous output" in err
        assert str(failing) in err
    else:
        with pytest.raises(OutputRemovalError, match="could not remove the previous output") as exc:
            _entry(name, statement, config, targets[0])
        err = str(exc.value)
        assert str(failing) in err
    if how == "permission":
        _assert_omits_markers(err)

    assert _existing(targets) == [failing.name]
    assert config.read_bytes() == b"not = [valid toml"


@pytest.mark.parametrize("layer", _LAYERS)
@pytest.mark.parametrize(("name", "index"), _TARGETS)
def test_failed_write_whose_cleanup_fails_says_the_output_may_remain(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    name: str,
    index: int,
    layer: str,
) -> None:
    statement, config = _write_inputs(tmp_path, name)
    targets = _stale_targets(tmp_path / "out", name)
    writes = _install_partial_writer(monkeypatch, name)
    # Only the cleanup after the failed write cannot remove the target.
    _fail_unlink(monkeypatch, targets[index], writes)

    if layer == "cli":
        assert _cli(name, statement, config, targets[0]) == 4
        err = capsys.readouterr().err
        assert "may still be there" in err
        assert str(targets[index]) in err
    else:
        with pytest.raises(OutputRemovalError, match="may still be there") as exc:
            _entry(name, statement, config, targets[0])
        err = str(exc.value)
        assert str(targets[index]) in err
        # Keeps none of the write failure it replaced, nor the unlink failure (#1211).
        assert_replaced_unchained(
            exc.value, markers=("No space left on device", _HOLDER, _STRERROR)
        )
    _assert_omits_markers(err)

    # The failing write ran once: its cleanup is the one that failed.
    assert writes == [[target.name for target in targets[1:]]]
    assert _existing(targets) == [targets[index].name]


def test_schedule_fa_staging_file_and_a2_that_cannot_be_removed_are_both_named(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A2 is committed, then committing A3 fails, and neither A3's staging file
    nor A2 can be removed: the one error printed names both (#740)."""
    statement, config = _write_inputs(tmp_path, "schedule-fa")
    a3, a2 = _targets(tmp_path / "out", "schedule-fa")
    staged: list[Path] = []
    real_replace = os.replace
    real_unlink = Path.unlink

    def replace(src: str, dst: Path) -> None:
        if dst == a3:
            staged.append(Path(src))
            raise _WriteFailedError(28, "No space left on device")
        real_replace(src, dst)

    def unlink(path: Path, missing_ok: bool = False) -> None:
        if staged and path in (staged[0], a2):
            raise _marker_error()
        real_unlink(path, missing_ok=missing_ok)

    monkeypatch.setattr(os, "replace", replace)
    monkeypatch.setattr(Path, "unlink", unlink)

    assert _cli("schedule-fa", statement, config, a3) == 4
    err = capsys.readouterr().err
    # The staging file by its pattern beside A3, not its own random name, and
    # each unlink failure by its errno name only (#1048).
    assert err == (
        "error: the run failed, and its output may still be there: could not remove "
        f"{a2} (EACCES); the write failed, and its output may still be there: could not "
        f"remove the staging file {a3.parent}/.schedule-fa.csv.*.tmp (EACCES)\n"
    )
    assert staged[0].name not in err
    _assert_omits_markers(err)
    assert _existing([a3, a2, *staged]) == [a2.name, staged[0].name]


@pytest.mark.parametrize("layer", _LAYERS)
@pytest.mark.parametrize("alias", ["exact", "symlink", "hard-link"])
@pytest.mark.parametrize("flag", ["--input", "--config"])
@pytest.mark.parametrize(("name", "index"), _TARGETS)
def test_target_that_is_an_input_is_refused_untouched(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    name: str,
    index: int,
    flag: str,
    alias: str,
    layer: str,
) -> None:
    statement, config = _write_inputs(tmp_path, name)
    targets = _targets(tmp_path / "out", name)
    target = targets[index]
    supplied = {"--input": statement, "--config": config}
    target.write_bytes(supplied[flag].read_bytes())
    if alias == "exact":
        supplied[flag] = target
    else:
        supplied[flag] = tmp_path / f"alias-{supplied[flag].name}"
        if alias == "symlink":
            supplied[flag].symlink_to(target)
        else:
            supplied[flag].hardlink_to(target)
    before = {path: path.read_bytes() for path in supplied.values()}

    if layer == "cli":
        assert _cli(name, supplied["--input"], supplied["--config"], targets[0]) == 4
        err = capsys.readouterr().err
        assert flag in err
        assert target.name in err
    else:
        with pytest.raises(OutputCollisionError, match=re.escape(flag)):
            _entry(name, supplied["--input"], supplied["--config"], targets[0])

    assert {path: path.read_bytes() for path in supplied.values()} == before


def test_schedule_fa_json_entry_point_leaves_its_target_to_the_cli(tmp_path: Path) -> None:
    """``run_fa_a3`` does not remove a JSON target: the CLI's provenance-checked
    preclear owns it, so a file ibkr-fa did not write survives a refusal."""
    statement, config = _write_inputs(tmp_path, "schedule-fa", number="U99999999")
    fragment = tmp_path / "schedule-fa.json"
    fragment.write_text(json.dumps({"hand": "merged"}), encoding="utf-8")
    a2 = tmp_path / "schedule-fa-a2.json"
    a2.write_bytes(_STALE)

    with pytest.raises(ConfigError, match=re.escape("'account.number'")):
        _entry(
            "schedule-fa",
            statement,
            config,
            fragment,
            format_id="cbdt-json-fragment",
            itr_form="ITR-2",
        )

    assert json.loads(fragment.read_text(encoding="utf-8")) == {"hand": "merged"}
    assert a2.read_bytes() == _STALE


def _output_parts(path: Path) -> dict[str, bytes] | bytes:
    """A CSV's bytes, or an XLSX's parts minus ``docProps/core.xml``, whose
    created/modified timestamps differ between runs."""
    if path.suffix != ".xlsx":
        return path.read_bytes()
    with zipfile.ZipFile(path) as workbook:
        return {
            part: workbook.read(part) for part in workbook.namelist() if part != "docProps/core.xml"
        }


@pytest.mark.parametrize("suffix", [".csv", ".xlsx"])
@pytest.mark.parametrize("name", _COMMANDS)
def test_stale_outputs_do_not_change_a_successful_run(
    tmp_path: Path, name: str, suffix: str
) -> None:
    statement, config = _write_inputs(tmp_path, name)
    cli_format, _entry_format = _format(suffix)
    clean = _targets(tmp_path / "clean", name, suffix)
    stale = _stale_targets(tmp_path / "stale", name, suffix)

    clean_rc = _cli(name, statement, config, clean[0], *cli_format)
    stale_rc = _cli(name, statement, config, stale[0], *cli_format)

    assert clean_rc in (0, 3)
    assert stale_rc == clean_rc
    assert _existing(clean) == [path.name for path in clean]
    assert [_output_parts(path) for path in stale] == [_output_parts(path) for path in clean]


# Every class a run builds after its write: each command's run report, and the
# summary rows that schedule-tr and form-67 build before it.
_AFTER_WRITE = [
    *((name, command.report) for name, command in _ALL_COMMANDS.items()),
    ("schedule-tr", "TrPerCountryRow"),
    ("form-67", "Form67PerEntryRow"),
]


@pytest.mark.parametrize("layer", _LAYERS)
@pytest.mark.parametrize("suffix", [".csv", ".xlsx"])
@pytest.mark.parametrize(("name", "refusing"), _AFTER_WRITE)
def test_refusal_after_the_write_leaves_no_output(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    name: str,
    refusing: str,
    suffix: str,
    layer: str,
) -> None:
    statement, config = _write_inputs(tmp_path, name)
    targets = _targets(tmp_path / "out", name, suffix)
    module = _ALL_COMMANDS[name].module
    error, refusals = _refuse_after_write(monkeypatch, module, refusing, targets)
    cli_format, entry_format = _format(suffix)

    if layer == "cli":
        assert _cli(name, statement, config, targets[0], *cli_format) == 4
        assert error.what in capsys.readouterr().err
    else:
        with pytest.raises(InvariantError) as exc:
            _entry(name, statement, config, targets[0], **entry_format)
        assert exc.value is error

    assert refusals == [[target.name for target in targets]]
    assert _existing(targets) == []


@pytest.mark.parametrize("layer", _LAYERS)
@pytest.mark.parametrize("suffix", [".csv", ".xlsx"])
@pytest.mark.parametrize(("name", "index"), _ALL_TARGETS)
def test_refusal_after_the_write_whose_cleanup_fails_says_the_output_may_remain(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    name: str,
    index: int,
    suffix: str,
    layer: str,
) -> None:
    statement, config = _write_inputs(tmp_path, name)
    targets = _targets(tmp_path / "out", name, suffix)
    command = _ALL_COMMANDS[name]
    error, refusals = _refuse_after_write(monkeypatch, command.module, command.report, targets)
    cli_format, entry_format = _format(suffix)
    # Only the cleanup after the refusal cannot remove the target.
    _fail_unlink(monkeypatch, targets[index], refusals)

    if layer == "cli":
        assert _cli(name, statement, config, targets[0], *cli_format) == 4
        err = capsys.readouterr().err
        assert "may still be there" in err
        assert str(targets[index]) in err
    else:
        # Catch the base's frozen error too, so pytest cannot mask it with TypeError.
        with pytest.raises(IbkrFaError) as exc:
            _entry(name, statement, config, targets[0], **entry_format)
        assert isinstance(exc.value, OutputRemovalError)
        assert "may still be there" in str(exc.value)
        assert str(targets[index]) in str(exc.value)
        # Keeps none of the refusal it replaced, nor the unlink failure (#1211).
        assert_replaced_unchained(exc.value, markers=(error.what, _HOLDER, _STRERROR))

    # The refusal ran once, after every write: its cleanup is the one that failed.
    assert refusals == [[target.name for target in targets]]
    assert _existing(targets) == [targets[index].name]


@pytest.mark.parametrize("layer", _LAYERS)
def test_schedule_fa_json_refusal_after_the_write_removes_the_fragment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], layer: str
) -> None:
    statement, config, fragment = _json_inputs(tmp_path)
    error, refusals = _refuse_after_write(
        monkeypatch, "ibkr_fa.pipeline_fa_cbdt_json", "RunReport", [fragment]
    )

    if layer == "cli":
        assert _run_json(layer, monkeypatch, statement, config, fragment) == 4
        assert error.what in capsys.readouterr().err
    else:
        with pytest.raises(InvariantError) as exc:
            _run_json(layer, monkeypatch, statement, config, fragment)
        assert exc.value is error

    assert refusals == [[fragment.name]]
    assert not fragment.exists()


@pytest.mark.parametrize("layer", _LAYERS)
def test_schedule_fa_json_refusal_whose_cleanup_fails_says_the_fragment_may_remain(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], layer: str
) -> None:
    statement, config, fragment = _json_inputs(tmp_path)
    error, refusals = _refuse_after_write(
        monkeypatch, "ibkr_fa.pipeline_fa_cbdt_json", "RunReport", [fragment]
    )
    _fail_unlink(monkeypatch, fragment, refusals)

    if layer == "cli":
        assert _run_json(layer, monkeypatch, statement, config, fragment) == 4
        err = capsys.readouterr().err
        assert "may still be there" in err
        assert str(fragment) in err
    else:
        with pytest.raises(IbkrFaError) as exc:
            _run_json(layer, monkeypatch, statement, config, fragment)
        assert isinstance(exc.value, OutputRemovalError)
        err = str(exc.value)
        assert "may still be there" in err
        assert str(fragment) in err
        # Keeps none of the refusal it replaced, nor the unlink failure (#1211).
        assert_replaced_unchained(exc.value, markers=(error.what, _HOLDER, _STRERROR))
    _assert_omits_markers(err)

    # The refusal ran once, after the write: its cleanup is the one that failed.
    assert refusals == [[fragment.name]]
    assert fragment.exists()


def test_schedule_fa_json_writer_refusal_leaves_a_file_it_did_not_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The writer refuses an incomplete fragment before it writes, so the file
    at the target is not this run's and stays."""
    statement, config, fragment = _json_inputs(tmp_path, _CONFIG_NO_PEAK)
    fragment.write_text(json.dumps({"hand": "merged"}), encoding="utf-8")

    with pytest.raises(CbdtJsonIncompleteError, match="PeakBalanceDuringPeriod"):
        _run_json("entry", monkeypatch, statement, config, fragment)

    assert json.loads(fragment.read_text(encoding="utf-8")) == {"hand": "merged"}


def test_schedule_fa_json_writer_refusal_leaves_a_dangling_symlink(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    statement, config, fragment = _json_inputs(tmp_path, _CONFIG_NO_PEAK)
    fragment.symlink_to("missing.json")

    with pytest.raises(CbdtJsonIncompleteError, match="PeakBalanceDuringPeriod"):
        _run_json("entry", monkeypatch, statement, config, fragment)

    assert fragment.readlink() == Path("missing.json")


def _interrupt_after_publishing(
    monkeypatch: pytest.MonkeyPatch, fragment: Path, then: Callable[[], None] = lambda: None
) -> tuple[KeyboardInterrupt, list[list[str]]]:
    """Once the real ``os.replace`` publishes ``fragment``, run ``then`` and
    raise ``KeyboardInterrupt``. Returns the interrupt, and the targets that
    existed at it."""
    interrupt = KeyboardInterrupt("interrupted after publishing the fragment")
    seen: list[list[str]] = []
    replace = os.replace

    def publish_then_interrupt(src: str, dst: Path) -> None:
        replace(src, dst)
        if Path(dst) == fragment:
            then()
            seen.append(_existing([fragment]))
            raise interrupt

    monkeypatch.setattr("ibkr_fa.output._atomic.os.replace", publish_then_interrupt)
    return interrupt, seen


def test_schedule_fa_json_interrupt_after_publishing_removes_the_fragment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    statement, config, fragment = _json_inputs(tmp_path)
    interrupt, seen = _interrupt_after_publishing(monkeypatch, fragment)

    with pytest.raises(KeyboardInterrupt) as exc:
        _run_json("entry", monkeypatch, statement, config, fragment)

    assert exc.value is interrupt
    assert seen == [[fragment.name]]
    assert list(fragment.parent.iterdir()) == []


def test_schedule_fa_json_interrupt_after_publishing_leaves_a_file_that_replaced_the_fragment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    statement, config, fragment = _json_inputs(tmp_path)
    replace = os.replace
    other = fragment.with_name("other.json")
    other_bytes = json.dumps({"hand": "merged"}).encode()

    def replace_the_fragment() -> None:
        staged = os.lstat(fragment)
        other.write_bytes(other_bytes)
        replace(other, fragment)
        now = os.lstat(fragment)
        assert (now.st_dev, now.st_ino) != (staged.st_dev, staged.st_ino)

    interrupt, seen = _interrupt_after_publishing(monkeypatch, fragment, replace_the_fragment)

    with pytest.raises(KeyboardInterrupt) as exc:
        _run_json("entry", monkeypatch, statement, config, fragment)

    assert exc.value is interrupt
    assert seen == [[fragment.name]]
    assert fragment.read_bytes() == other_bytes


def test_schedule_fa_json_interrupt_after_the_writer_returns_removes_the_fragment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    statement, config, fragment = _json_inputs(tmp_path)
    interrupt = KeyboardInterrupt("interrupted in the post-write check")
    seen: list[list[str]] = []

    def interrupted(*_args: object, **_kwargs: object) -> NoReturn:
        seen.append(_existing([fragment]))
        raise interrupt

    monkeypatch.setattr("ibkr_fa.pipeline_fa_cbdt_json.require", interrupted)

    with pytest.raises(KeyboardInterrupt) as exc:
        _run_json("entry", monkeypatch, statement, config, fragment)

    assert exc.value is interrupt
    assert seen == [[fragment.name]]
    assert not fragment.exists()


def _fail_lstat(monkeypatch: pytest.MonkeyPatch, failing: Path, armed: Sized) -> None:
    """Make ``os.lstat`` of ``failing`` raise the marker ``PermissionError`` once
    ``armed`` is non-empty."""
    lstat = os.lstat

    def cannot_inspect(path: Path) -> os.stat_result:
        if armed and Path(path) == failing:
            raise _marker_error()
        return lstat(path)

    monkeypatch.setattr("ibkr_fa.output._cleanup.os.lstat", cannot_inspect)


@pytest.mark.parametrize("layer", _LAYERS)
def test_schedule_fa_json_cleanup_that_cannot_inspect_the_fragment_says_it_may_remain(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], layer: str
) -> None:
    statement, config, fragment = _json_inputs(tmp_path)
    error, refusals = _refuse_after_write(
        monkeypatch, "ibkr_fa.pipeline_fa_cbdt_json", "RunReport", [fragment]
    )
    _fail_lstat(monkeypatch, fragment, refusals)
    # The lstat failure by its errno name only (#1048).
    message = (
        f"the run failed, and its output may still be there: could not inspect {fragment} (EACCES)"
    )

    if layer == "cli":
        assert _run_json(layer, monkeypatch, statement, config, fragment) == 4
        printed = capsys.readouterr().err
        assert printed == f"error: {message}\n"
    else:
        with pytest.raises(IbkrFaError) as exc:
            _run_json(layer, monkeypatch, statement, config, fragment)
        assert isinstance(exc.value, OutputRemovalError)
        printed = str(exc.value)
        assert printed == message
        # Keeps none of the refusal it replaced, nor the lstat failure (#1211).
        assert_replaced_unchained(exc.value, markers=(error.what, _HOLDER, _STRERROR))
    _assert_omits_markers(printed)

    assert refusals == [[fragment.name]]
    assert fragment.exists()


def test_schedule_fa_json_staging_file_that_cannot_be_inspected_exits_4(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    statement, config, fragment = _json_inputs(tmp_path)
    failure, staging = fail_staging_fstat(monkeypatch, fragment)

    assert _run_json("cli", monkeypatch, statement, config, fragment) == 4

    err = capsys.readouterr().err
    assert len(staging) == 1
    assert failure.errno == errno.EIO
    # The staging file by its pattern beside the fragment, not its own random
    # name, and the fstat failure by its errno name only (#1048).
    assert err == (
        f"error: could not write {fragment}: could not inspect its staging file "
        f"{fragment.parent}/.schedule-fa.json.*.tmp (EIO)\n"
    )
    assert staging[0].name not in err
    for leaked in (failure.strerror, failure.filename):
        assert str(leaked).lower() not in err.lower()
    assert list(fragment.parent.iterdir()) == []


@pytest.mark.parametrize("layer", _LAYERS)
def test_schedule_fa_json_staging_file_and_fragment_cleanup_failures_are_both_named(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], layer: str
) -> None:
    """``on_staged`` claims the staging file and then refuses; neither the
    staging file's removal nor the fragment's inspection succeeds."""
    statement, config, fragment = _json_inputs(tmp_path)
    error = InvariantError(what="on_staged refused", context=())
    staging: list[Path] = []
    write = CbdtScheduleFaJsonWriter.write

    def claim_then_refuse(
        self: CbdtScheduleFaJsonWriter,
        value: CbdtScheduleFaFragmentInput,
        path: Path,
        *,
        on_staged: Callable[[FileIdentity], None],
    ) -> None:
        def refuse(identity: FileIdentity) -> NoReturn:
            on_staged(identity)
            staging.extend(path.parent.glob(f".{path.name}.*.tmp"))
            raise error

        write(self, value, path, on_staged=refuse)

    monkeypatch.setattr(CbdtScheduleFaJsonWriter, "write", claim_then_refuse)
    unlink = Path.unlink

    def cannot_remove(path: Path, missing_ok: bool = False) -> None:
        if path in staging:
            raise _marker_error()
        unlink(path, missing_ok=missing_ok)

    monkeypatch.setattr(Path, "unlink", cannot_remove)
    _fail_lstat(monkeypatch, fragment, staging)

    # The fragment's inspection and the staging file's removal, each by its errno
    # name only, and the staging file by its pattern, not its own name (#1048).
    staging_message = (
        "the write failed, and its output may still be there: could not remove the staging "
        f"file {fragment.parent}/.schedule-fa.json.*.tmp (EACCES)"
    )
    message = (
        f"the run failed, and its output may still be there: could not inspect {fragment} "
        f"(EACCES); {staging_message}"
    )

    if layer == "cli":
        assert _run_json(layer, monkeypatch, statement, config, fragment) == 4
        printed = capsys.readouterr().err
        assert printed == f"error: {message}\n"
    else:
        with pytest.raises(IbkrFaError) as exc:
            _run_json(layer, monkeypatch, statement, config, fragment)
        assert isinstance(exc.value, OutputRemovalError)
        printed = str(exc.value)
        assert printed == message
        # It replaced the staging file's removal error, named only in its message:
        # it keeps none of that error, the refusal or the OS failures (#1211).
        assert_replaced_unchained(exc.value, markers=(error.what, _HOLDER, _STRERROR))
    assert staging[0].name not in printed
    _assert_omits_markers(printed)

    assert len(staging) == 1
    assert not fragment.exists()


def test_schedule_fa_json_direct_call_replaces_a_file_it_did_not_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    statement, config, fragment = _json_inputs(tmp_path)
    fragment.write_text(json.dumps({"hand": "merged"}), encoding="utf-8")

    report = _run_json("entry", monkeypatch, statement, config, fragment)

    assert isinstance(report, RunReport)
    assert report.output_path == fragment
    assert is_ibkr_fa_cbdt_fragment(fragment)


@pytest.mark.parametrize("ours", [True, False])
def test_schedule_fa_json_cli_preclear_is_unchanged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], ours: bool
) -> None:
    """The CLI still replaces its own earlier fragment and refuses a file
    ibkr-fa did not write."""
    statement, config, fragment = _json_inputs(tmp_path)
    prior = json.dumps({"_ibkrFa": {"format": "cbdt-json-fragment"}} if ours else {"hand": 1})
    fragment.write_text(prior, encoding="utf-8")

    rc = _run_json("cli", monkeypatch, statement, config, fragment)

    if ours:
        assert rc == 0
        assert json.loads(fragment.read_text(encoding="utf-8"))["ScheduleFA"]
    else:
        assert rc == 4
        assert "not generated by ibkr-fa" in capsys.readouterr().err
        assert fragment.read_text(encoding="utf-8") == prior


def _form67_debug_targets(out: Path) -> list[Path]:
    """``form-67``'s output and the debug file ``run-all --debug-form67`` adds."""
    out.mkdir()
    return [out / "form-67.csv", out / "form-67-debug.csv"]


def _run_form67_with_debug(
    layer: str, statement: Path, config: Path, targets: list[Path]
) -> object:
    if layer == "entry":
        return _entry("form-67", statement, config, targets[0], debug_output_path=targets[1])
    return main(
        [
            "run-all",
            "--fy-input",
            str(statement),
            "--config",
            str(config),
            "--output-dir",
            str(targets[0].parent),
            *_FY_ARGS,
            *_ITR,
            "--debug-form67",
        ]
    )


def _fail_form67_write(
    monkeypatch: pytest.MonkeyPatch, target: Path, error: BaseException
) -> list[Path]:
    """Replace form-67's writers with one that writes a partial file and raises
    ``error`` at ``target``. Returns the failed writes."""
    failed: list[Path] = []

    class PartialWriter:
        @classmethod
        def from_context(cls, _ctx: RenderContext) -> PartialWriter:
            return cls()

        def write(self, _result: object, path: Path) -> None:
            path.write_bytes(b"partial")
            if path == target:
                failed.append(path)
                raise error

    monkeypatch.setattr(
        OutputBinding, "writer", lambda _binding, ctx: PartialWriter.from_context(ctx)
    )
    return failed


@pytest.mark.parametrize("how", ["directory", "permission"])
@pytest.mark.parametrize("index", [0, 1])
def test_form67_debug_output_that_cannot_be_removed_refuses_before_reading(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, index: int, how: str
) -> None:
    """``run_form67`` still removes the other target, and reads nothing."""
    statement = tmp_path / "missing-statement.csv"
    config = tmp_path / "user.toml"
    config.write_bytes(b"not = [valid toml")
    targets = _form67_debug_targets(tmp_path / "out")
    for target in targets:
        target.write_bytes(_STALE)
    failing = targets[index]
    _make_unremovable(monkeypatch, failing, how)

    with pytest.raises(OutputRemovalError, match="could not remove the previous output") as exc:
        _run_form67_with_debug("entry", statement, config, targets)

    assert [target for target in targets if str(target) in str(exc.value)] == [failing]
    assert _existing(targets) == [failing.name]
    assert config.read_bytes() == b"not = [valid toml"


@pytest.mark.parametrize("layer", _LAYERS)
def test_form67_debug_refusal_after_the_write_leaves_neither_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], layer: str
) -> None:
    statement, config = _write_inputs(tmp_path, "form-67")
    targets = _form67_debug_targets(tmp_path / "out")
    error, refusals = _refuse_after_write(
        monkeypatch, "ibkr_fa.pipeline_form67", "Form67RunReport", targets
    )

    if layer == "cli":
        assert _run_form67_with_debug(layer, statement, config, targets) == 4
        assert error.what in capsys.readouterr().err
    else:
        with pytest.raises(InvariantError) as exc:
            _run_form67_with_debug(layer, statement, config, targets)
        assert exc.value is error

    assert refusals == [[target.name for target in targets]]
    assert _existing(targets) == []


@pytest.mark.parametrize("layer", _LAYERS)
@pytest.mark.parametrize("index", [0, 1])
@pytest.mark.parametrize("failure", ["write", "report"])
def test_form67_debug_cleanup_that_fails_says_the_output_may_remain(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    failure: str,
    index: int,
    layer: str,
) -> None:
    statement, config = _write_inputs(tmp_path, "form-67")
    targets = _form67_debug_targets(tmp_path / "out")
    if failure == "write":
        error = _WriteFailedError(28, "No space left on device")
        armed: Sized = _fail_form67_write(monkeypatch, targets[1], error)
    else:
        error, armed = _refuse_after_write(
            monkeypatch, "ibkr_fa.pipeline_form67", "Form67RunReport", targets
        )
    _fail_unlink(monkeypatch, targets[index], armed)

    if layer == "cli":
        assert _run_form67_with_debug(layer, statement, config, targets) == 4
        err = capsys.readouterr().err
        assert "may still be there" in err
        assert str(targets[index]) in err
    else:
        with pytest.raises(IbkrFaError) as exc:
            _run_form67_with_debug(layer, statement, config, targets)
        assert isinstance(exc.value, OutputRemovalError)
        assert "may still be there" in str(exc.value)
        assert str(targets[index]) in str(exc.value)
        # Keeps none of the write failure or refusal it replaced, nor the unlink
        # failure (#1211).
        assert_replaced_unchained(
            exc.value,
            markers=(
                "No space left on device" if failure == "write" else "refused after the write",
                _HOLDER,
                _STRERROR,
            ),
        )

    # The failure ran once: its cleanup is the one that failed.
    assert len(armed) == 1
    assert _existing(targets) == [targets[index].name]


@pytest.mark.parametrize("index", [0, 1])
@pytest.mark.parametrize("error_type", [KeyboardInterrupt, BaseException])
def test_form67_interrupted_write_leaves_neither_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, index: int, error_type: type[BaseException]
) -> None:
    statement, config = _write_inputs(tmp_path, "form-67")
    targets = _form67_debug_targets(tmp_path / "out")
    error = error_type("Form 67 write interrupted")
    failed = _fail_form67_write(monkeypatch, targets[index], error)

    with pytest.raises(BaseException) as exc:
        _run_form67_with_debug("entry", statement, config, targets)

    assert exc.value is error
    assert failed == [targets[index]]
    assert _existing(targets) == []
