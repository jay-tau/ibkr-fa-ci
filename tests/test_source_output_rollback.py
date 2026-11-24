"""StockPlan, NetBenefits and Fidelity CMA commands remove their own output
when they fail after writing it (#745).

Each command runs for real on a test bundle and then fails between its first
write and its report. As for the IBKR schedules
(``tests/test_standalone_output_cleanup.py``), the failure removes every output
the run wrote, and a removal that fails is named in the error.
"""

from __future__ import annotations

import errno
import os
import re
from collections.abc import Callable
from pathlib import Path
from typing import NoReturn

import pytest

from ibkr_fa.cli import main
from ibkr_fa.exceptions import IbkrFaError, InvariantError
from ibkr_fa.output._cleanup import FileIdentity, OutputRemovalError
from ibkr_fa.output._writer_contract import WriterContractError
from ibkr_fa.output.base import Subcommand
from ibkr_fa.output.cbdt_json_fragment import (
    CbdtJsonIncompleteError,
    CbdtScheduleFaFragmentInput,
    CbdtScheduleFaJsonWriter,
)
from ibkr_fa.pipeline_netbenefits_fa import NetBenefitsFaError
from tests._crash_reports import assert_replaced_unchained, internal_error_line
from tests._fidelity_cma_bundle import cma_inputs  # noqa: F401 - fixture
from tests._oserror_markers import FILENAME_MARKER as _HOLDER
from tests._oserror_markers import STRERROR_MARKER as _STRERROR
from tests._output_removal_failures import CLEANUP_LAYERS as _LAYERS
from tests._output_removal_failures import fail_unlink as _fail_unlink
from tests._output_removal_failures import refuse_after_write as _refuse_after_write
from tests._source_commands import SOURCE_COMMANDS as _COMMANDS
from tests._source_commands import SourceRun as _Run
from tests._source_commands import prepare_source_command as _prepare
from tests._writer_stubs import StubWriter, patch_writers

# Every (command, written output) pair.
_WRITTEN = [(name, filename) for name, cmd in _COMMANDS.items() for filename in cmd.written]


def _left(run: _Run) -> list[str]:
    return sorted(path.name for path in run.out.iterdir())


@pytest.mark.parametrize("layer", _LAYERS)
@pytest.mark.parametrize("name", _COMMANDS)
def test_refusal_after_the_write_leaves_no_output(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    request: pytest.FixtureRequest,
    capsys: pytest.CaptureFixture[str],
    name: str,
    layer: str,
) -> None:
    run, written = _prepare(name, tmp_path, monkeypatch, request)
    command = _COMMANDS[name]
    error, refusals = _refuse_after_write(monkeypatch, command.module, command.report, written)

    if layer == "cli":
        assert main(run.argv) == 4
        assert error.what in capsys.readouterr().err
    else:
        with pytest.raises(InvariantError) as exc:
            run.entry()
        assert exc.value is error

    assert refusals == [list(command.written)]
    assert _left(run) == []


@pytest.mark.parametrize("layer", _LAYERS)
@pytest.mark.parametrize(("name", "filename"), _WRITTEN)
def test_refusal_after_the_write_whose_cleanup_fails_names_the_output(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    request: pytest.FixtureRequest,
    capsys: pytest.CaptureFixture[str],
    name: str,
    filename: str,
    layer: str,
) -> None:
    run, written = _prepare(name, tmp_path, monkeypatch, request)
    command = _COMMANDS[name]
    error, refusals = _refuse_after_write(monkeypatch, command.module, command.report, written)
    failing = run.out / filename
    # Only the cleanup after the refusal cannot remove the output.
    _fail_unlink(monkeypatch, failing, refusals)

    if layer == "cli":
        assert main(run.argv) == 4
        message = capsys.readouterr().err
    else:
        # Catch the base's frozen error too, so pytest cannot mask it with TypeError.
        with pytest.raises(IbkrFaError) as exc:
            run.entry()
        assert isinstance(exc.value, OutputRemovalError)
        # Keeps none of the refusal it replaced, nor the unlink failure (#1211).
        assert_replaced_unchained(exc.value, markers=(error.what, _HOLDER, _STRERROR))
        message = str(exc.value)

    assert "may still be there" in message
    assert str(failing) in message
    # The refusal ran once, after every write: its cleanup is the one that failed.
    assert refusals == [list(command.written)]
    assert _left(run) == [filename]


@pytest.mark.parametrize("name", ["stockplan-income", "netbenefits-income", "fidelity-cma-income"])
def test_unexpected_failure_after_the_write_leaves_no_output(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    request: pytest.FixtureRequest,
    capsys: pytest.CaptureFixture[str],
    name: str,
) -> None:
    """A bug still fails the run with exit 1, naming its class and source line
    but not its message (#1049), only once the run's output is gone."""
    run, _written = _prepare(name, tmp_path, monkeypatch, request)

    def crash(*_args: object, **_kwargs: object) -> NoReturn:
        raise RuntimeError("report bug")

    command = _COMMANDS[name]
    monkeypatch.setattr(f"{command.module}.{command.report}", crash)

    assert main(run.argv) == 1
    assert re.search(rf"{internal_error_line(RuntimeError)}\Z", capsys.readouterr().err)
    assert _left(run) == []


@pytest.mark.parametrize("name", _COMMANDS)
def test_failed_write_is_a_refusal_that_leaves_no_output(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    request: pytest.FixtureRequest,
    capsys: pytest.CaptureFixture[str],
    name: str,
) -> None:
    """A filesystem error publishing the last output, after the others were
    written, exits 4 without a traceback."""
    run, written = _prepare(name, tmp_path, monkeypatch, request)
    publish = os.replace

    def replace(source: str | Path, destination: str | Path, **kwargs: object) -> None:
        if Path(destination) == written[-1]:
            raise OSError(errno.ENOSPC, "No space left on device", str(destination))
        publish(source, destination, **kwargs)  # ty: ignore[invalid-argument-type]

    monkeypatch.setattr(os, "replace", replace)

    assert main(run.argv) == 4
    err = capsys.readouterr().err
    if name == "netbenefits-fa-json":
        # The fragment's refusal names the OSError by its class and errno name,
        # never its text or file name (#1048).
        assert err == (
            f"error: the Schedule FA JSON fragment {written[-1]} failed after it was staged or "
            "written: OSError: ENOSPC\n"
        )
    elif name == "netbenefits-fa-quicko":
        # The Quicko write's refusal names the workbook by the path the run was
        # asked to write and the OSError by its errno name, never its text or
        # its file name in the OSError's own form (#1042).
        assert err == (
            f"error: could not write the requested Quicko workbook {written[-1]} (ENOSPC)\n"
        )
        assert "No space left on device" not in err
        assert f"'{written[-1]}'" not in err
    else:
        # The other commands let the OSError reach the CLI, which names it by
        # its errno name, never its text or file name (#1049).
        assert "a file operation failed (ENOSPC)\n" in err
        assert "No space left on device" not in err
        assert str(written[-1]) not in err
    assert _left(run) == []


def _ltcg_writer_fails(monkeypatch: pytest.MonkeyPatch, failure: BaseException) -> None:
    """Make the LTCG writer raise ``failure`` once the STCG CSV is written."""

    class _FailingWriter(StubWriter):
        def write(self, _result: object, path: Path) -> None:
            assert (path.parent / "schedule-cg.csv").exists()
            raise failure

    patch_writers(monkeypatch, Subcommand.CG_LTCG, _FailingWriter)


def test_netbenefits_cg_ltcg_writer_refusal_removes_the_stcg_csv(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    request: pytest.FixtureRequest,
    capsys: pytest.CaptureFixture[str],
) -> None:
    run, _written = _prepare("netbenefits-cg", tmp_path, monkeypatch, request)
    refusal = WriterContractError(
        violation="formula_trigger",
        row_index=1,
        cell_index=1,
        trigger="=",
    )
    _ltcg_writer_fails(monkeypatch, refusal)

    assert main(run.argv) == 4
    assert str(refusal) in capsys.readouterr().err
    assert _left(run) == []


def test_fidelity_cma_cg_interrupted_write_removes_both_csvs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, request: pytest.FixtureRequest
) -> None:
    run, _written = _prepare("fidelity-cma-cg", tmp_path, monkeypatch, request)
    _ltcg_writer_fails(monkeypatch, KeyboardInterrupt())

    with pytest.raises(KeyboardInterrupt):
        run.entry()
    assert _left(run) == []


def _fragment_fails_after_publishing(
    monkeypatch: pytest.MonkeyPatch, failure: Exception
) -> list[bool]:
    """Publish the real fragment, then raise ``failure``. Returns whether the
    fragment existed when it was raised."""
    published: list[bool] = []

    real_write = CbdtScheduleFaJsonWriter.write

    def write(
        self: CbdtScheduleFaJsonWriter,
        fragment: CbdtScheduleFaFragmentInput,
        path: Path,
        *,
        on_staged: Callable[[FileIdentity], None],
    ) -> NoReturn:
        real_write(self, fragment, path, on_staged=on_staged)
        published.append(path.exists())
        raise failure

    monkeypatch.setattr(CbdtScheduleFaJsonWriter, "write", write)
    return published


@pytest.mark.parametrize("layer", _LAYERS)
def test_netbenefits_fa_json_incomplete_after_publishing_is_removed_and_refused(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    request: pytest.FixtureRequest,
    capsys: pytest.CaptureFixture[str],
    layer: str,
) -> None:
    """Only a missing filed value, refused before publishing, is manual fill
    (exit 3). The same error once the fragment is published is unexpected."""
    run, written = _prepare("netbenefits-fa-json", tmp_path, monkeypatch, request)
    failure = CbdtJsonIncompleteError(["raised after publishing"])
    published = _fragment_fails_after_publishing(monkeypatch, failure)
    # The refusal names the failure by its class only (#1048).
    refusal = (
        f"the Schedule FA JSON fragment {written[-1]} failed after it was staged or written: "
        "CbdtJsonIncompleteError"
    )

    if layer == "cli":
        assert main(run.argv) == 4
        assert capsys.readouterr().err == f"error: {refusal}\n"
    else:
        with pytest.raises(NetBenefitsFaError) as exc:
            run.entry()
        assert exc.value.__cause__ is failure
        assert str(exc.value) == refusal

    assert published == [True]
    assert _left(run) == []


def test_netbenefits_fa_json_failure_after_publishing_is_removed_and_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, request: pytest.FixtureRequest
) -> None:
    run, _written = _prepare("netbenefits-fa-json", tmp_path, monkeypatch, request)
    bug = RuntimeError("after publishing")
    published = _fragment_fails_after_publishing(monkeypatch, bug)

    with pytest.raises(NetBenefitsFaError) as exc:
        run.entry()
    assert exc.value.__cause__ is bug
    assert published == [True]
    assert _left(run) == []
