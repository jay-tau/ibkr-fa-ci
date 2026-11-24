"""Exception-graph privacy at Robinhood orchestration and output rollback (#1211).

Five relays replace one failure with another: the F&O warning wrapper, the
all-mode "leg output cleanup failed" refusal, the shared cleanup relay behind
``remove_outputs_on_failure`` and ``ClaimedOutputCleanup``, and staging-file
cleanup. Each replacement keeps its class, fields and exact message, and none
of the failure it replaced. It has no context, and its only cause, when that
failure or the active handler was or chained a crash, is a ``ReplacedCrashError``
naming the crash's class and line. Content-bearing failures use distinct
synthetic markers (a JSON document, decoder bytes, exception text), checked
over the whole graph, suppressed links included. The relay runs alone, under a caller's ``except``
and inside another context manager's exit.
"""

from __future__ import annotations

import errno
import fnmatch
import json
import re
from collections.abc import Callable
from contextlib import AbstractContextManager
from pathlib import Path
from types import SimpleNamespace
from typing import Any, NoReturn

import pytest

import ibkr_fa.pipeline_robinhood as pipeline
import ibkr_fa.pipeline_robinhood_core as core
import ibkr_fa.pipeline_robinhood_outputs as outputs
from ibkr_fa.cli import _raise_for_traceback
from ibkr_fa.exceptions import ConfigError
from ibkr_fa.output._atomic import (
    atomic_output_path,
    atomic_text_output,
    discard_staging_file,
    staging_file_pattern,
)
from ibkr_fa.output._cleanup import (
    ClaimedOutputCleanup,
    OutputRemovalError,
    remove_outputs_on_failure,
)
from ibkr_fa.pipeline_robinhood import RobinhoodPipelineError
from ibkr_fa.pipeline_robinhood_models import RobinhoodAllRemovalError, RobinhoodLegStatus
from tests._crash_reports import assert_no_markers, assert_replaced_unchained, crash_pattern
from tests._robinhood_cli import FINANCIAL_YEAR as _FY
from tests._robinhood_cli import outputs_left as _left
from tests._robinhood_cli import robinhood_main as _main
from tests._robinhood_income_stubs import (  # noqa: F401 - autouse fixture
    silenced_default_income_warnings as _silence_default_income_warnings,
)
from tests._robinhood_pipeline_stubs import complete_fa as _complete_fa
from tests._robinhood_pipeline_stubs import stand_in_bundle as _bundle
from tests._robinhood_pipeline_stubs import stub_successful_all as _stub_successful_all
from tests._robinhood_pipeline_stubs import stub_writing_all as _stub_writing_all

# One marker per relay, plus one for the exception a caller is handling.
_FNO_JSON = "SYNTH-FNO-JSON-DOCUMENT"
_ALL_DECODER = "SYNTH-ALL-DECODER-BYTES"
_CG_WRITE = "SYNTH-CG-WRITE-BYTES"
_CLEANUP_DECODER = "SYNTH-CLEANUP-DECODER-BYTES"
_CLEANUP_OSERROR = "SYNTH-CLEANUP-OSERROR-TEXT"
_INNER_CRASH = "SYNTH-INNER-REMOVAL-CAUSE"
_ATOMIC_DECODER = "SYNTH-ATOMIC-DECODER-BYTES"
_ATOMIC_WRITE = "SYNTH-ATOMIC-WRITE-ERROR"
_UNLINK_CRASH = "SYNTH-UNLINK-CRASH"
_OUTER = "SYNTH-OUTER-HANDLER"
# The failed removal's own text and file name.
_UNLINK_TEXT = "SYNTH-UNLINK-STRERROR"
_UNLINK_NAME = "SYNTH-UNLINK-FILENAME"

_FNO = re.escape(core.ROBINHOOD_FNO_FEMA_WARNING)
_REMOVAL = "the run failed, and its output may still be there: could not remove {} (EACCES)"
_INNER_REMOVAL = (
    "the write failed, and its output may still be there: could not remove "
    "the staging file inner/.x.csv.*.tmp (EACCES)"
)


def _json_error() -> json.JSONDecodeError:
    """A decoder error whose ``doc`` holds the whole document, as ``json.loads`` raises."""
    document = f'{{"note": "{_FNO_JSON}", }}'
    return json.JSONDecodeError("Expecting property name", document, len(document) - 1)


def _decode_error(marker: str) -> UnicodeDecodeError:
    """A decoder error whose ``object`` holds the bytes it read."""
    data = marker.encode() + b"\xff"
    return UnicodeDecodeError("utf-8", data, len(data) - 1, len(data), "invalid start byte")


def _unlink_fails(monkeypatch: pytest.MonkeyPatch, failing: Callable[[Path], bool]) -> None:
    """``Path.unlink`` raises a marked ``EACCES`` for each path ``failing`` selects."""
    unlink = Path.unlink

    def fake(path: Path, missing_ok: bool = False) -> None:
        if failing(path):
            raise OSError(errno.EACCES, _UNLINK_TEXT, _UNLINK_NAME)
        unlink(path, missing_ok=missing_ok)

    monkeypatch.setattr(Path, "unlink", fake)


# --- Where the relay runs ------------------------------------------------------


class _RunsOnExit:
    """Calls ``run`` in ``__exit__``, while its block's exception is being handled."""

    def __init__(self, run: Callable[[], object]) -> None:
        self._run = run

    def __enter__(self) -> None:
        return None

    def __exit__(self, *_exc_info: object) -> None:
        self._run()


def _alone(run: Callable[[], object]) -> None:
    run()


def _raise(error: BaseException) -> NoReturn:
    raise error


def _under_handler(run: Callable[[], object]) -> None:
    """``run()`` in a caller's ``except`` block for a marked error."""
    try:
        _raise(ValueError(_OUTER))
    except ValueError:
        run()


def _in_exit(run: Callable[[], object]) -> None:
    """``run()`` in another context manager's exit, after its block raised a marked error."""
    with _RunsOnExit(run):
        raise ValueError(_OUTER)


_WHERE = [
    pytest.param(_alone, id="alone"),
    pytest.param(_under_handler, id="under-handler"),
    pytest.param(_in_exit, id="in-exit"),
]


def _in_input(where: Callable[..., None], *markers: str) -> tuple[str, ...]:
    """``markers``, plus the handled error's when ``where`` runs under one:
    the markers that run's input holds."""
    return markers if where is _alone else (*markers, _OUTER)


# --- F&O warning wrapper ------------------------------------------------------


def _fail_inside_fno(monkeypatch: pytest.MonkeyPatch, target: str, failure: Exception) -> None:
    """Stub a Robinhood run with option trades whose ``target`` step, inside
    the F&O wrapper, raises ``failure``."""
    _stub_successful_all(monkeypatch, _complete_fa())
    monkeypatch.setattr(pipeline, "_load", lambda **_kwargs: _bundle(has_options=True))
    options = SimpleNamespace(trades=(object(),), blockers=())
    monkeypatch.setattr(pipeline, "_compute_options", lambda *_args: options)
    monkeypatch.setattr(pipeline, "_reconstruct_options", lambda *_args: options)

    def fail(*_args: object, **_kwargs: object) -> NoReturn:
        raise failure

    monkeypatch.setattr(pipeline, target, fail)


def _run(runner: Callable[..., object], tmp_path: Path, **extra: object) -> None:
    runner(
        input_dir=tmp_path / "bundle",
        config_path=tmp_path / "user.toml",
        output_dir=tmp_path / "out",
        financial_year=_FY,
        itr_form="ITR-2",
        **extra,
    )


def _assert_fno_relay(error: BaseException, markers: tuple[str, ...]) -> None:
    assert type(error) is RobinhoodPipelineError
    pattern = rf"{_FNO} internal error \({crash_pattern(json.JSONDecodeError)}\)"
    assert re.fullmatch(pattern, str(error)), str(error)
    assert_replaced_unchained(error, json.JSONDecodeError, markers=markers)


@pytest.mark.parametrize(
    ("runner", "target", "extra"),
    [
        pytest.param(pipeline.run_robinhood_cg, "_compute_cg", {}, id="robinhood-cg"),
        pytest.param(
            pipeline.run_robinhood_options, "_validate_owned_events", {}, id="robinhood-options"
        ),
        pytest.param(
            pipeline.run_robinhood_income,
            "_compute_cg",
            {"fetch_prices": False},
            id="robinhood-income",
        ),
        pytest.param(
            pipeline.run_robinhood_fa, "_fa_prices", {"fetch_prices": False}, id="robinhood-fa"
        ),
    ],
)
def test_fno_wrapper_keeps_no_json_document_from_a_standalone_run(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    runner: Callable[..., object],
    target: str,
    extra: dict[str, object],
) -> None:
    _fail_inside_fno(monkeypatch, target, _json_error())

    with pytest.raises(RobinhoodPipelineError) as raised:
        _run(runner, tmp_path, **extra)

    _assert_fno_relay(raised.value, (_FNO_JSON,))
    assert _left(tmp_path) == []


def _option_compute(monkeypatch: pytest.MonkeyPatch, step: str) -> Callable[[], object]:
    """The core option ``step`` on a bundle with options, its compute raising a
    JSON decoder error."""
    bundle: Any = _bundle(has_options=True)
    bundle.robinhood = None
    bundle.config.fx = None

    def fail(*_args: object, **_kwargs: object) -> NoReturn:
        raise _json_error()

    if step == "reconstruct":
        monkeypatch.setattr(core, "reconstruct_robinhood_options", fail)
        return lambda: core.reconstruct_options(bundle, _FY, "ITR-3")
    fx = SimpleNamespace(capital_gains=None, other_sources=None)
    monkeypatch.setattr(core, "TaxFxContext", SimpleNamespace(for_fy=lambda *_args: fx))
    monkeypatch.setattr(core, "compute_robinhood_options", fail)
    return lambda: core.compute_options(bundle, _FY, "ITR-3")


@pytest.mark.parametrize("where", _WHERE)
@pytest.mark.parametrize("step", ["reconstruct", "compute"])
def test_fno_wrapper_keeps_no_json_document_from_the_option_steps(
    monkeypatch: pytest.MonkeyPatch, step: str, where: Callable[..., None]
) -> None:
    run = _option_compute(monkeypatch, step)

    with pytest.raises(RobinhoodPipelineError) as raised:
        where(run)

    _assert_fno_relay(raised.value, _in_input(where, _FNO_JSON))


def test_fno_wrapped_crash_reraises_content_free_with_the_traceback_flag(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The stand-in for the crash still makes ``IBKR_FA_TRACEBACK=1`` re-raise."""
    _fail_inside_fno(monkeypatch, "_compute_cg", _json_error())
    monkeypatch.setenv("IBKR_FA_TRACEBACK", "1")

    with pytest.raises(RobinhoodPipelineError) as raised:
        _main("robinhood-cg", tmp_path)

    _assert_fno_relay(raised.value, (_FNO_JSON,))
    assert _left(tmp_path) == []


def test_fno_wrapped_crash_prints_a_fixed_line_without_the_traceback_flag(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _fail_inside_fno(monkeypatch, "_compute_cg", _json_error())

    assert _main("robinhood-cg", tmp_path) == 4

    captured = capsys.readouterr()
    assert captured.out == ""
    line = rf"error: {_FNO} internal error \({crash_pattern(json.JSONDecodeError)}\)\n"
    assert re.fullmatch(line, captured.err), captured.err
    assert_no_markers(captured.err, _FNO_JSON)
    assert _left(tmp_path) == []


def test_fno_wrapped_refusal_prints_even_with_the_traceback_flag(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A wrapped domain error has no stand-in, so the flag doesn't re-raise it."""
    refusal = ConfigError(field="robinhood", message="refused")
    _fail_inside_fno(monkeypatch, "_compute_cg", refusal)
    monkeypatch.setenv("IBKR_FA_TRACEBACK", "1")

    assert _main("robinhood-cg", tmp_path) == 4

    assert capsys.readouterr() == ("", f"error: {core.ROBINHOOD_FNO_FEMA_WARNING} {refusal}\n")


# --- All-mode stored cleanup error --------------------------------------------


@pytest.mark.parametrize("where", _WHERE)
@pytest.mark.parametrize(
    ("failing", "filenames"),
    [
        ("equity-cg", outputs.CG_FILING_FILENAMES),
        ("schedule-os", outputs.OS_FILENAMES),
    ],
)
def test_all_mode_refusal_keeps_no_stored_cleanup_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failing: str,
    filenames: tuple[str, ...],
    where: Callable[..., None],
) -> None:
    """A leg's writer fails, then its cleanup fails with decoder bytes; the run
    still removes every output and refuses, keeping none of that error."""
    out = tmp_path / "out"
    written = _stub_writing_all(monkeypatch, out, failing=failing)
    clear_leg = pipeline._clear_leg

    def fail_cleanup(output_dir: Path, names: tuple[str, ...]) -> None:
        if names == filenames:
            raise _decode_error(_ALL_DECODER)
        clear_leg(output_dir, names)

    monkeypatch.setattr(pipeline, "_clear_leg", fail_cleanup)
    monkeypatch.setattr(outputs, "clear_leg", fail_cleanup)

    def run() -> None:
        pipeline.run_robinhood_all(
            input_dir=tmp_path / "bundle",
            config_path=tmp_path / "user.toml",
            output_dir=out,
            financial_year=_FY,
            itr_form="ITR-2",
            fetch_prices=False,
            debug=True,
        )

    with pytest.raises(RobinhoodAllRemovalError) as raised:
        where(run)

    error = raised.value
    assert type(error) is RobinhoodAllRemovalError
    relayed = rf"internal error \({crash_pattern(UnicodeDecodeError)}\)"
    pattern = rf"leg output cleanup failed: {relayed}; run outputs were removed"
    assert re.fullmatch(pattern, str(error)), str(error)
    markers = _in_input(where, _ALL_DECODER, f"{failing} disk failure")
    assert_replaced_unchained(error, UnicodeDecodeError, markers=markers)
    assert error.report.status is RobinhoodLegStatus.ERROR
    reasons = " ".join(reason for leg in error.report.legs for reason in leg.reasons)
    assert re.search(rf"cleanup failed: {relayed}", reasons)
    assert_no_markers(reasons, _ALL_DECODER)
    assert not any(path.exists() for path in written)


def test_all_mode_cg_line_names_only_the_removal_its_writer_raised_instead(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The CG write fails, the writer can't remove its debug output, then the
    leg's own cleanup succeeds. The writer's removal error keeps none of the
    write failure, so the CG line names only that removal; before #1211 it
    also named the write failure, read from the chain (owner's call)."""
    out = tmp_path / "out"
    _stub_writing_all(monkeypatch, out)
    debug = out / "schedule-cg-debug.csv"

    def write_cg(*_args: object, **_kwargs: object) -> NoReturn:
        paths = (out / "schedule-cg.xlsx", debug)
        with remove_outputs_on_failure(paths):
            for path in paths:
                path.write_text("equity-cg", encoding="utf-8")
            raise _decode_error(_CG_WRITE)

    removals: list[Path] = []

    def first_debug_removal(path: Path) -> bool:
        """Only the writer's own removal of the debug output fails."""
        if path != debug or not debug.exists():
            return False
        removals.append(path)
        return len(removals) == 1

    monkeypatch.setattr(pipeline, "_write_cg", write_cg)
    _unlink_fails(monkeypatch, first_debug_removal)

    with pytest.raises(RobinhoodAllRemovalError) as raised:
        pipeline.run_robinhood_all(
            input_dir=tmp_path / "bundle",
            config_path=tmp_path / "user.toml",
            output_dir=out,
            financial_year=_FY,
            itr_form="ITR-2",
            fetch_prices=False,
            debug=True,
        )

    error = raised.value
    removal = _REMOVAL.format(debug)
    assert str(error) == f"leg output cleanup failed: {removal}; run outputs were removed"
    assert_replaced_unchained(error, UnicodeDecodeError, markers=(_CG_WRITE,))
    [cg_leg] = [leg for leg in error.report.legs if leg.name == "equity-cg"]
    assert cg_leg.status is RobinhoodLegStatus.ERROR
    assert cg_leg.reasons == (f"cleanup failed: {removal}",)
    assert not debug.exists()


# --- Shared cleanup relay -----------------------------------------------------


def _raise_decoder() -> NoReturn:
    raise _decode_error(_CLEANUP_DECODER)


def _raise_inner_removal() -> NoReturn:
    """An inner cleanup's removal error whose cause holds input."""
    try:
        _raise(KeyError(_INNER_CRASH))
    except KeyError as exc:
        raise OutputRemovalError(_INNER_REMOVAL) from exc


def _raise_os_error() -> NoReturn:
    raise OSError(errno.EIO, _CLEANUP_OSERROR, _CLEANUP_OSERROR)


def _raise_interrupt() -> NoReturn:
    raise KeyboardInterrupt


# Each block failure, its markers, the crash its stand-in names when the relay
# runs alone, and what the removal error's message adds. An OSError is a domain
# error, so only the marked ValueError a caller is handling gives it a stand-in.
_BLOCK_FAILURES = [
    pytest.param(_raise_decoder, (_CLEANUP_DECODER,), UnicodeDecodeError, "", id="decoder"),
    pytest.param(
        _raise_inner_removal, (_INNER_CRASH,), KeyError, f"; {_INNER_REMOVAL}", id="removal-error"
    ),
    pytest.param(_raise_os_error, (_CLEANUP_OSERROR,), None, "", id="os-error"),
    pytest.param(_raise_interrupt, (), KeyboardInterrupt, "", id="interrupt"),
]


def _claimed(path: Path) -> ClaimedOutputCleanup:
    cleanup = ClaimedOutputCleanup(path)
    info = path.lstat()
    cleanup.claim((info.st_dev, info.st_ino))
    return cleanup


@pytest.mark.parametrize("where", _WHERE)
@pytest.mark.parametrize(("block", "block_markers", "crash", "added"), _BLOCK_FAILURES)
@pytest.mark.parametrize("claim", [False, True], ids=["targets", "claim"])
def test_cleanup_relay_keeps_none_of_the_block_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    claim: bool,
    block: Callable[[], NoReturn],
    block_markers: tuple[str, ...],
    crash: type[BaseException] | None,
    added: str,
    where: Callable[..., None],
) -> None:
    stuck, other = tmp_path / "stuck.csv", tmp_path / "other.csv"
    for path in (stuck, other):
        path.write_text("output", encoding="utf-8")
    cleanup: AbstractContextManager[object] = (
        _claimed(stuck) if claim else remove_outputs_on_failure((stuck, other))
    )
    _unlink_fails(monkeypatch, lambda path: path == stuck)

    def run() -> None:
        with cleanup:
            block()

    with pytest.raises(OutputRemovalError) as raised:
        where(run)

    error = raised.value
    assert type(error) is OutputRemovalError
    assert str(error) == _REMOVAL.format(stuck) + added
    if crash is None and where is not _alone:
        crash = ValueError
    markers = _in_input(where, *block_markers, _UNLINK_TEXT, _UNLINK_NAME)
    assert_replaced_unchained(error, crash, markers=markers)
    assert stuck.exists()
    # Every removal is attempted; a claim covers its own path only.
    assert other.exists() is claim


@pytest.mark.parametrize("where", _WHERE)
def test_nested_cleanup_relays_name_both_paths_in_order(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, where: Callable[..., None]
) -> None:
    """The claim's relay is the outer cleanup's block failure: the outer error
    names its own path, then the claim's, and keeps neither failure."""
    outer, other, claimed = tmp_path / "a.csv", tmp_path / "b.csv", tmp_path / "c.json"
    for path in (outer, other, claimed):
        path.write_text("output", encoding="utf-8")
    claim = _claimed(claimed)
    _unlink_fails(monkeypatch, lambda path: path in (outer, claimed))

    def run() -> None:
        with remove_outputs_on_failure((outer, other)), claim:
            _raise_decoder()

    with pytest.raises(OutputRemovalError) as raised:
        where(run)

    error = raised.value
    assert str(error) == f"{_REMOVAL.format(outer)}; {_REMOVAL.format(claimed)}"
    markers = _in_input(where, _CLEANUP_DECODER, _UNLINK_TEXT, _UNLINK_NAME)
    assert_replaced_unchained(error, UnicodeDecodeError, markers=markers)
    assert not other.exists()


# --- Staging-file cleanup ---------------------------------------------------


@pytest.mark.parametrize("suppressed", [False, True])
def test_staging_cleanup_preserves_a_crash_only_in_the_unlink_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, suppressed: bool
) -> None:
    path = tmp_path / "out.csv"

    def unlink(_path: Path, missing_ok: bool = False) -> NoReturn:
        try:
            _raise(ValueError(_UNLINK_CRASH))
        except ValueError as crash:
            raise OSError(errno.EACCES, _UNLINK_TEXT, _UNLINK_NAME) from (
                None if suppressed else crash
            )

    monkeypatch.setattr(Path, "unlink", unlink)
    with (
        pytest.raises(OutputRemovalError) as raised,
        atomic_text_output(path, encoding="utf-8", newline="\n"),
    ):
        raise OSError(errno.EIO, _ATOMIC_WRITE)

    error = raised.value
    assert type(error) is OutputRemovalError
    assert str(error) == (
        "the write failed, and its output may still be there: could not remove "
        f"the staging file {staging_file_pattern(path)} (EACCES)"
    )
    assert_replaced_unchained(
        error, ValueError, markers=(_ATOMIC_WRITE, _UNLINK_CRASH, _UNLINK_TEXT, _UNLINK_NAME)
    )
    monkeypatch.setenv("IBKR_FA_TRACEBACK", "1")
    with pytest.raises(OutputRemovalError) as reraised:
        _raise_for_traceback(error)
    assert reraised.value is error
    assert not path.exists()


@pytest.mark.parametrize("where", _WHERE[1:])
def test_staging_cleanup_preserves_a_crash_only_in_the_caller(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, where: Callable[..., None]
) -> None:
    path = tmp_path / "out.csv"
    staged = tmp_path / ".out.csv.staged.tmp"
    failure = OSError(errno.EIO, _ATOMIC_WRITE)
    _unlink_fails(monkeypatch, lambda candidate: candidate == staged)

    with pytest.raises(OutputRemovalError) as raised:
        where(lambda: discard_staging_file(staged, path, failure))

    error = raised.value
    assert type(error) is OutputRemovalError
    assert str(error) == (
        "the write failed, and its output may still be there: could not remove "
        f"the staging file {staging_file_pattern(path)} (EACCES)"
    )
    assert_replaced_unchained(
        error, ValueError, markers=(_ATOMIC_WRITE, _OUTER, _UNLINK_TEXT, _UNLINK_NAME)
    )
    monkeypatch.setenv("IBKR_FA_TRACEBACK", "1")
    with pytest.raises(OutputRemovalError) as reraised:
        _raise_for_traceback(error)
    assert reraised.value is error
    assert failure.__context__ is None
    assert not path.exists()


@pytest.mark.parametrize("where", _WHERE)
@pytest.mark.parametrize(
    "helper",
    [
        pytest.param(
            lambda path: atomic_text_output(path, encoding="utf-8", newline="\n"), id="text"
        ),
        pytest.param(atomic_output_path, id="path"),
    ],
)
def test_staging_cleanup_relay_keeps_no_decoder_bytes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    helper: Callable[[Path], AbstractContextManager[object]],
    where: Callable[..., None],
) -> None:
    path = tmp_path / "out.csv"
    path.write_text("earlier output", encoding="utf-8")
    pattern = staging_file_pattern(path)
    _unlink_fails(monkeypatch, lambda staged: fnmatch.fnmatch(staged.name, pattern.name))

    def run() -> None:
        with helper(path):
            raise _decode_error(_ATOMIC_DECODER)

    with pytest.raises(OutputRemovalError) as raised:
        where(run)

    error = raised.value
    assert type(error) is OutputRemovalError
    assert str(error) == (
        "the write failed, and its output may still be there: could not remove "
        f"the staging file {pattern} (EACCES)"
    )
    markers = _in_input(where, _ATOMIC_DECODER, _UNLINK_TEXT, _UNLINK_NAME)
    assert_replaced_unchained(error, UnicodeDecodeError, markers=markers)
    # The output is untouched, and the staging file it couldn't remove is named.
    assert path.read_text(encoding="utf-8") == "earlier output"
    assert len(list(tmp_path.glob(pattern.name))) == 1
