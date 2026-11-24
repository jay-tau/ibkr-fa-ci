"""A run attempts to remove its outputs if report rendering, printing or
flushing fails (#832, #1227, #1236).

Every command renders all it will print, its stdout report and any stderr
safety or advisory block, before printing any of it, inside a cleanup scope
over the outputs its run published. A renderer that raises attempts cleanup first:
a ``decimal`` failure (a value too large or too precise to round) then exits 4
with nothing printed from the report, and any other failure propagates. The
command prints in a fresh scope over the same manifest, after its own refusal
handling, so a failed console write attempts cleanup and then exits as before (#1227);
if a removal fails too, the command refuses with that removal error instead. It
flushes the stdout report there too, so a block-buffered stdout whose reader is
gone fails in that scope, not at exit (#1236). A
CBDT JSON fragment, or ``merge``'s merged return, is removed only while it is
still the file the run published.
"""

from __future__ import annotations

import contextlib
import dataclasses
import decimal
import errno
import io
import os
import re
import subprocess
import sys
import tempfile
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass
from functools import partial
from pathlib import Path
from types import SimpleNamespace
from typing import Any, NoReturn
from unittest.mock import Mock

import pytest

import ibkr_fa.pipeline_robinhood as robinhood_pipeline
import ibkr_fa.pipeline_robinhood_outputs as robinhood_outputs
from ibkr_fa import cli
from ibkr_fa.cli import main
from ibkr_fa.compliance import BMA_DISCLOSURE_ADVISORY
from ibkr_fa.exceptions import InvariantError
from ibkr_fa.output._cleanup import OutputRemovalError, RunOutputs
from tests._cbdt_merge_inputs import write_fragment_file, write_itr_file
from tests._crash_reports import assert_replaced_unchained, crash_pattern, internal_error_line
from tests._fidelity_cma_bundle import cma_inputs  # noqa: F401 - fixture
from tests._fragment_sources import (
    OTHER_FRAGMENT_BYTES,
    FragmentSource,
    place_other_file,
    robinhood_all_source,
    robinhood_fa_source,
)
from tests._ibkr_run_all import FY_2025_26_INPUT, run_all_fy, spy_on
from tests._ibkr_standalone_commands import (
    fa_json_inputs,
    run_fa_json,
    run_standalone_cli,
    standalone_targets,
    write_standalone_inputs,
)
from tests._oserror_markers import FILENAME_MARKER, STRERROR_MARKER
from tests._output_removal_failures import marker_permission_error
from tests._robinhood_cli import robinhood_main, robinhood_main_all
from tests._robinhood_identity_guidance import assert_guidance_file as _assert_guidance_file
from tests._robinhood_identity_guidance import stub_identity_refusals as _stub_identity_refusals
from tests._robinhood_income_stubs import (  # noqa: F401 - autouse fixture
    silenced_default_income_warnings as _silence_default_income_warnings,
)
from tests._robinhood_pipeline_stubs import stub_writing_all
from tests._robinhood_security import IDENTITY_GUIDANCE_FILE as _GUIDANCE
from tests._run_all_fakes import RUN_ALL_FY_SCHEDULE_NAMES, RUN_ALL_SCHEDULE_NAMES
from tests._schedule_os_e2e import SCHEDULE_OS_DIVIDENDS_STATEMENT, SCHEDULE_OS_USER_TOML
from tests._source_all import INCOME_OUTPUTS, NETBENEFITS_ALL, STOCKPLAN_ALL, SourceAll
from tests._source_commands import prepare_source_command

_REFUSAL = "a value in the input is too large or too precise to compute (InvalidOperation)"
_PRICE_CACHE = ".ibkr-fa-price-cache"


@dataclass(frozen=True)
class _Run:
    """A command that writes its outputs into ``out`` (and nothing else there)."""

    cli: Callable[[], object]
    out: Path


type _Prepare = Callable[[Path, pytest.MonkeyPatch, pytest.FixtureRequest], _Run]


def _schedule(name: str, *extra: str) -> _Prepare:
    def prepare(
        tmp_path: Path, _monkeypatch: pytest.MonkeyPatch, _request: pytest.FixtureRequest
    ) -> _Run:
        statement, config = write_standalone_inputs(tmp_path, name)
        output = standalone_targets(tmp_path / "out", name)[0]
        return _Run(
            partial(run_standalone_cli, name, statement, config, output, *extra), output.parent
        )

    return prepare


def _source(name: str) -> _Prepare:
    def prepare(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch, request: pytest.FixtureRequest
    ) -> _Run:
        run, _written = prepare_source_command(name, tmp_path, monkeypatch, request)
        return _Run(partial(main, run.argv), run.out)

    return prepare


def _robinhood(command: str) -> _Prepare:
    def prepare(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch, _request: pytest.FixtureRequest
    ) -> _Run:
        out = tmp_path / "out"
        stub_writing_all(monkeypatch, out)
        if command == "robinhood-all":
            return _Run(partial(robinhood_main_all, tmp_path), out)
        return _Run(partial(robinhood_main, command, tmp_path), out)

    return prepare


def _merge(
    tmp_path: Path, _monkeypatch: pytest.MonkeyPatch, _request: pytest.FixtureRequest
) -> _Run:
    out = tmp_path / "out"
    out.mkdir()
    argv = [
        "merge",
        *("--fragment", str(write_fragment_file(tmp_path))),
        *("--itr", str(write_itr_file(tmp_path))),
        *("--output", str(out / "merged.json")),
    ]
    return _Run(partial(main, argv), out)


def _schedule_fa_json(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, _request: pytest.FixtureRequest
) -> _Run:
    statement, config, fragment = fa_json_inputs(tmp_path)
    return _Run(
        partial(run_fa_json, "cli", monkeypatch, statement, config, fragment),
        fragment.parent,
    )


def _schedule_renderers(stem: str) -> list[tuple[str, tuple[str, ...]]]:
    """A schedule's safety block and report, verbose and ``--quiet``."""
    return [
        (f"_render_{stem}_safety_warnings", ()),
        (f"_render_{stem}_report", ()),
        (f"_render_{stem}_safety_warnings", ("--quiet",)),
        (f"_render_{stem}_report_quiet", ("--quiet",)),
    ]


# (command, how to run it, the renderer that fails, extra CLI flags)
_RENDERERS: list[tuple[str, Callable[..., _Prepare], str, tuple[str, ...]]] = [
    *(
        (name, _schedule, renderer, flags)
        for name, stem in (
            ("schedule-os", "schedule_os"),
            ("schedule-cg", "schedule_cg"),
            ("schedule-cg-ltcg", "schedule_cg_ltcg"),
            ("schedule-fsi", "schedule_fsi"),
            ("schedule-tr", "schedule_tr"),
            ("form-67", "form67"),
        )
        for renderer, flags in _schedule_renderers(stem)
    ),
    ("schedule-fa", _schedule, "_render_run_report", ()),
    ("schedule-fa-json", lambda _name: _schedule_fa_json, "_render_cbdt_json_report", ()),
    ("stockplan-income", _source, "_render_income_warnings", ()),
    ("stockplan-income", _source, "_render_stockplan_income_report", ()),
    ("stockplan-fa", _source, "_render_stockplan_fa_report", ()),
    ("netbenefits-income", _source, "_render_income_warnings", ()),
    ("netbenefits-income", _source, "_render_netbenefits_income_report", ()),
    ("netbenefits-cg", _source, "_render_netbenefits_cg_warnings", ()),
    ("netbenefits-cg", _source, "_render_netbenefits_cg_report", ()),
    ("netbenefits-fa", _source, "_render_netbenefits_fa_report", ()),
    ("netbenefits-fa-quicko", _source, "_render_netbenefits_fa_report", ()),
    ("netbenefits-fa-json", _source, "_render_netbenefits_fa_report", ()),
    ("fidelity-cma-income", _source, "_render_fidelity_cma_income_warnings", ()),
    ("fidelity-cma-income", _source, "_render_fidelity_cma_income_report", ()),
    ("fidelity-cma-cg", _source, "_render_fidelity_cma_cg_warnings", ()),
    ("fidelity-cma-cg", _source, "_render_fidelity_cma_cg_report", ()),
    ("fidelity-cma-fa", _source, "_render_fidelity_cma_fa_warnings", ()),
    ("fidelity-cma-fa", _source, "_render_fidelity_cma_fa_report", ()),
    *(
        (command, _robinhood, "_render_robinhood_leg", ())
        for command in ("robinhood-cg", "robinhood-income", "robinhood-fa", "robinhood-options")
    ),
    ("robinhood-all", _robinhood, "_render_robinhood_all", ()),
    ("merge", lambda _name: _merge, "_render_merge_report", ()),
]


def _command(name: str) -> str:
    """The CLI command a ``_RENDERERS`` entry runs."""
    for suffix in ("-json", "-quicko"):
        name = name.removesuffix(suffix)
    return name


def _refusal_tail(command: str) -> str:
    """The stderr a refused ``command`` ends with: StockPlan points at its contract."""
    pointer = f"{cli._STOCKPLAN_CONTRACT_POINTER}\n" if command.startswith("stockplan-") else ""
    return f"error: {command}: {_REFUSAL}\n{pointer}"


def _left(out: Path) -> list[str]:
    """Everything the run left in ``out``, except a price cache it may keep there."""
    return sorted(path.name for path in out.iterdir() if path.name != _PRICE_CACHE)


def _fail_with(
    monkeypatch: pytest.MonkeyPatch, name: str, error: BaseException, out: Path
) -> list[list[str]]:
    """Make ``cli.<name>`` raise ``error``. Returns, for each call, the outputs
    that were in ``out`` when it was called."""
    calls: list[list[str]] = []

    def fail(*_args: object, **_kwargs: object) -> NoReturn:
        calls.append(_left(out))
        raise error

    monkeypatch.setattr(cli, name, fail)
    return calls


@pytest.mark.parametrize(
    ("name", "how", "renderer", "flags"),
    _RENDERERS,
    ids=[f"{name}-{renderer}{'-quiet' * bool(flags)}" for name, _, renderer, flags in _RENDERERS],
)
def test_report_that_cannot_be_rounded_removes_the_outputs_and_exits_4(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    request: pytest.FixtureRequest,
    capsys: pytest.CaptureFixture[str],
    name: str,
    how: Callable[[str], _Prepare],
    renderer: str,
    flags: tuple[str, ...],
) -> None:
    run = how(name)(tmp_path, monkeypatch, request)
    run = _Run(partial(run.cli, *flags), run.out) if flags else run
    calls = _fail_with(monkeypatch, renderer, decimal.InvalidOperation(), run.out)

    assert run.cli() == 4

    [written] = calls
    assert written, "the run wrote its outputs before rendering"
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == _refusal_tail(_command(name))
    assert _left(run.out) == []


@pytest.mark.parametrize(
    ("name", "how", "renderer", "flags"),
    _RENDERERS,
    ids=[f"{name}-{renderer}{'-quiet' * bool(flags)}" for name, _, renderer, flags in _RENDERERS],
)
def test_report_that_crashes_removes_the_outputs_and_exits_1(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    request: pytest.FixtureRequest,
    capsys: pytest.CaptureFixture[str],
    name: str,
    how: Callable[[str], _Prepare],
    renderer: str,
    flags: tuple[str, ...],
) -> None:
    """The crash prints its class and source line, never its message (#1049)."""
    run = how(name)(tmp_path, monkeypatch, request)
    run = _Run(partial(run.cli, *flags), run.out) if flags else run
    calls = _fail_with(monkeypatch, renderer, RuntimeError("renderer bug"), run.out)

    assert run.cli() == 1

    [written] = calls
    assert written, "the run wrote its outputs before rendering"
    captured = capsys.readouterr()
    assert captured.out == ""
    assert re.fullmatch(internal_error_line(RuntimeError), captured.err)
    assert _left(run.out) == []


@pytest.mark.parametrize(
    ("name", "how", "renderer", "flags"),
    _RENDERERS,
    ids=[f"{name}-{renderer}{'-quiet' * bool(flags)}" for name, _, renderer, flags in _RENDERERS],
)
def test_report_that_crashes_propagates_after_cleanup_with_the_traceback_flag(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    request: pytest.FixtureRequest,
    capsys: pytest.CaptureFixture[str],
    name: str,
    how: Callable[[str], _Prepare],
    renderer: str,
    flags: tuple[str, ...],
) -> None:
    run = how(name)(tmp_path, monkeypatch, request)
    run = _Run(partial(run.cli, *flags), run.out) if flags else run
    bug = RuntimeError("renderer bug")
    calls = _fail_with(monkeypatch, renderer, bug, run.out)
    monkeypatch.setenv("IBKR_FA_TRACEBACK", "1")

    with pytest.raises(RuntimeError) as raised:
        run.cli()

    assert raised.value is bug
    [written] = calls
    assert written, "the run wrote its outputs before rendering"
    assert capsys.readouterr().out == ""
    assert _left(run.out) == []


@pytest.mark.parametrize(
    ("name", "how", "renderer"),
    [
        ("schedule-os", _schedule, "_render_schedule_os_report"),
        ("netbenefits-fa-json", _source, "_render_netbenefits_fa_report"),
        ("robinhood-all", _robinhood, "_render_robinhood_all"),
    ],
)
def test_report_that_refuses_with_a_frozen_domain_error_keeps_it(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    request: pytest.FixtureRequest,
    capsys: pytest.CaptureFixture[str],
    name: str,
    how: Callable[[str], _Prepare],
    renderer: str,
) -> None:
    """The cleanup scope re-raises this package's frozen exceptions unchanged."""
    run = how(name)(tmp_path, monkeypatch, request)
    error = InvariantError(what="report invariant", context=())
    calls = _fail_with(monkeypatch, renderer, error, run.out)

    assert run.cli() == 4

    [written] = calls
    assert written, "the run wrote its outputs before rendering"
    assert capsys.readouterr().err == f"error: {error}\n"
    assert _left(run.out) == []


# --- JSON ownership ------------------------------------------------------------


def _netbenefits_fa_json(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, request: pytest.FixtureRequest
) -> tuple[Callable[[], object], Path]:
    run = _source("netbenefits-fa-json")(tmp_path, monkeypatch, request)
    return run.cli, run.out / "schedule-fa.json"


def _fragment_source(
    make: Callable[..., FragmentSource],
) -> Callable[..., tuple[Callable[[], object], Path]]:
    def prepare(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch, request: pytest.FixtureRequest
    ) -> tuple[Callable[[], object], Path]:
        source = make(tmp_path, monkeypatch, request)
        assert source.cli is not None
        return source.cli, source.fragment

    return prepare


def _schedule_fa_fragment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, request: pytest.FixtureRequest
) -> tuple[Callable[[], object], Path]:
    run = _schedule_fa_json(tmp_path, monkeypatch, request)
    return run.cli, run.out / "schedule-fa.json"


def _merged_return(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, request: pytest.FixtureRequest
) -> tuple[Callable[[], object], Path]:
    run = _merge(tmp_path, monkeypatch, request)
    return run.cli, run.out / "merged.json"


# (command, how to run it, the renderer that fails)
_JSON = {
    "schedule-fa": (_schedule_fa_fragment, "_render_cbdt_json_report"),
    "netbenefits-fa": (_netbenefits_fa_json, "_render_netbenefits_fa_report"),
    "robinhood-fa": (_fragment_source(robinhood_fa_source), "_render_robinhood_leg"),
    "robinhood-all": (_fragment_source(robinhood_all_source), "_render_robinhood_all"),
    "merge": (_merged_return, "_render_merge_report"),
}


@pytest.mark.parametrize("replaced", [False, True], ids=["published", "replaced"])
@pytest.mark.parametrize("name", _JSON)
def test_render_failure_removes_the_json_only_while_it_is_the_published_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    request: pytest.FixtureRequest,
    capsys: pytest.CaptureFixture[str],
    name: str,
    replaced: bool,
) -> None:
    prepare, renderer = _JSON[name]
    run, path = prepare(tmp_path, monkeypatch, request)
    published: list[bytes] = []

    def fail(*_args: object, **_kwargs: object) -> NoReturn:
        published.append(path.read_bytes())
        if replaced:
            # Someone else's file at the same path, after publication.
            place_other_file(path)
        raise decimal.InvalidOperation

    monkeypatch.setattr(cli, renderer, fail)

    assert run() == 4

    assert published, "the run published its JSON before rendering"
    assert capsys.readouterr().err.endswith(f"error: {name}: {_REFUSAL}\n")
    if replaced:
        assert path.read_bytes() == OTHER_FRAGMENT_BYTES
    else:
        assert not path.exists()


# --- Orchestrators --------------------------------------------------------------


@pytest.mark.parametrize("renderer", ["_run_all_global_advisories", "_render_run_all_summary"])
def test_run_all_final_block_that_cannot_render_removes_every_output(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    renderer: str,
) -> None:
    calls = _fail_with(monkeypatch, renderer, decimal.InvalidOperation(), tmp_path / "out")

    assert run_all_fy(tmp_path, FY_2025_26_INPUT) == 4

    [written] = calls
    assert written, "the schedules wrote their outputs before the final block"
    err = capsys.readouterr().err
    assert err.endswith(f"error: run-all: {_REFUSAL}\n")
    assert BMA_DISCLOSURE_ADVISORY not in err
    assert "run-all summary" not in err
    assert _left(tmp_path / "out") == []


# The ids keep the module names pytest gave these cases before #1777.
@pytest.mark.parametrize(
    ("harness", "orchestrator"),
    [
        pytest.param(
            NETBENEFITS_ALL, "netbenefits-all", id="tests.test_cli_netbenefits_all-netbenefits-all"
        ),
        pytest.param(
            STOCKPLAN_ALL, "stockplan-all", id="tests.test_cli_stockplan_all-stockplan-all"
        ),
    ],
)
def test_source_all_summary_that_cannot_render_removes_every_output(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    harness: SourceAll,
    orchestrator: str,
) -> None:
    out = tmp_path / "out"
    calls = _fail_with(monkeypatch, "_render_source_all_summary", decimal.InvalidOperation(), out)

    rc, _calls, _out = harness.run(tmp_path, monkeypatch, harness.all_ok)

    assert rc == 4
    [written] = calls
    assert written, "the legs wrote their outputs before the summary"
    err = capsys.readouterr().err
    assert err.endswith(_refusal_tail(orchestrator))
    assert "summary (FY" not in err
    assert _left(out) == []


def test_robinhood_all_report_after_a_failed_removal_that_cannot_render(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A failed run whose leg cleanup failed still names that error, then
    refuses the report it could not render."""
    out = tmp_path / "out"
    written = stub_writing_all(monkeypatch, out, failing="options")
    clear_leg = robinhood_pipeline._clear_leg

    def fail_options_cleanup(output_dir: Path, names: tuple[str, ...]) -> None:
        if names == robinhood_outputs.OPTIONS_FILENAMES:
            raise PermissionError("cannot clear output")
        clear_leg(output_dir, names)

    monkeypatch.setattr(robinhood_pipeline, "_clear_leg", fail_options_cleanup)
    monkeypatch.setattr(robinhood_outputs, "clear_leg", fail_options_cleanup)
    calls = _fail_with(monkeypatch, "_render_robinhood_all", decimal.InvalidOperation(), out)

    assert robinhood_main_all(tmp_path) == 4

    assert len(calls) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    removal, refusal = captured.err.splitlines()
    # The OSError is named by fixed text, never its message (#1044).
    assert removal == (
        f"error: leg output cleanup failed: could not remove outputs in {out} "
        "(unknown error); run outputs were removed"
    )
    assert refusal == f"error: robinhood-all: {_REFUSAL}"
    assert not any(path.exists() for path in written)


# --- A console write that fails mid-run (#925) ----------------------------------


def _broken_pipe(command: str) -> str:
    """The refusal a ``command`` whose console broke prints while stderr works (#1049)."""
    return f"error: {command}: a file operation failed (EPIPE)\n"


_FY_SCHEDULES = list(RUN_ALL_FY_SCHEDULE_NAMES)
_FIRST_REPORT = "the first leg's report"


class _Console(io.StringIO):
    """A console stream that breaks as a pipe does once its reader exits.

    From the first write containing ``break_at`` ("" is the first write, and
    ``None`` waits until ``broken`` is set), every write and flush raises
    ``error()``, ``BrokenPipeError`` by default. The first failed write calls
    ``on_break`` once.
    With ``once``, only that first write fails: the stream works again, or
    only that text could not be encoded. Each write error raised is kept in
    ``failures``. Its descriptor is a real file's, so the run can point it at
    ``os.devnull``.
    """

    def __init__(
        self,
        fd: int,
        break_at: str | None,
        *,
        error: Callable[[], Exception] = lambda: BrokenPipeError(errno.EPIPE, "Broken pipe"),
        once: bool = False,
        on_break: Callable[[], object] = lambda: None,
    ) -> None:
        super().__init__()
        self._fd = fd
        self._break_at = break_at
        self._error = error
        self._once = once
        self._on_break = on_break
        self.broken = False
        self.failures: list[Exception] = []

    def write(self, text: str) -> int:
        if self._break_at is not None and self._break_at in text:
            self.broken = True
        if self.broken:
            if not self.failures:
                self._on_break()
            if self._once:
                self.broken = False
                self._break_at = None
            self.failures.append(self._error())
            raise self.failures[-1]
        return super().write(text)

    def flush(self) -> None:
        if self.broken:
            raise self._error()

    def fileno(self) -> int:
        return self._fd

    @property
    def settled(self) -> bool:
        """Whether the run pointed this stream's descriptor at ``os.devnull``."""
        return os.path.samestat(os.fstat(self._fd), Path(os.devnull).stat())


@pytest.fixture
def console() -> Iterator[Callable[..., _Console]]:
    """Make :class:`_Console` streams, each on its own temporary file."""
    with contextlib.ExitStack() as files:

        def make(break_at: str | None, **kwargs: Any) -> _Console:
            fd = files.enter_context(tempfile.TemporaryFile()).fileno()
            return _Console(fd, break_at, **kwargs)

        yield make


def _seed(out: Path, targets: Iterable[Path]) -> None:
    """Leave an earlier run's file at every target."""
    out.mkdir()
    for target in targets:
        target.write_text("an earlier run", encoding="utf-8")


def _seed_run_all(out: Path) -> None:
    _seed(
        out,
        (target for name in RUN_ALL_SCHEDULE_NAMES for target in cli._run_all_targets(out, name)),
    )


def _run_all(tmp_path: Path) -> int:
    return run_all_fy(tmp_path, FY_2025_26_INPUT)


def _ran(invoke: Mock) -> list[str]:
    """The run-all schedules that ran, in order."""
    return [call.args[0] for call in invoke.call_args_list]


@pytest.mark.parametrize(
    ("break_at", "ran"),
    [("  run-all: schedule-cg\n", _FY_SCHEDULES[:1]), ("Schedule CG STCG", _FY_SCHEDULES[:2])],
    ids=["header", "replayed-report"],
)
def test_run_all_stdout_failure_stops_the_run_and_removes_every_output(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    console: Callable[[str | None], _Console],
    break_at: str,
    ran: list[str],
) -> None:
    _seed_run_all(tmp_path / "out")
    invoke = spy_on(monkeypatch, cli, "_invoke_run_all_handler")
    stdout = console(break_at)

    with contextlib.redirect_stdout(stdout):
        assert _run_all(tmp_path) == 4

    assert _ran(invoke) == ran
    assert _left(tmp_path / "out") == []
    assert capsys.readouterr().err.endswith(_broken_pipe("run-all"))
    assert stdout.settled


def test_run_all_stdout_flush_failure_after_a_failed_schedule_removes_every_output(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    console: Callable[[str | None], _Console],
) -> None:
    """The reader exits after the last report: the flush before the final block
    fails before the run's own failed-run cleanup."""
    out = tmp_path / "out"
    _seed_run_all(out)
    _fail_with(monkeypatch, "_schedule_tr_step", RuntimeError("a bug"), out)
    stdout = console(None)
    skip_schedule_fa = cli._skip_schedule_fa

    def reader_exits(output_dir: Path) -> object:
        stdout.broken = True
        return skip_schedule_fa(output_dir)

    monkeypatch.setattr(cli, "_skip_schedule_fa", reader_exits)
    remove = spy_on(monkeypatch, cli, "_remove_failed_run_all_outputs")

    with contextlib.redirect_stdout(stdout):
        assert _run_all(tmp_path) == 4

    assert remove.call_count == 0
    assert _left(out) == []
    assert capsys.readouterr().err.endswith(_broken_pipe("run-all"))
    assert stdout.settled


@pytest.mark.parametrize(
    ("break_at", "ran"),
    [("", _FY_SCHEDULES[:1]), ("RUN-ALL SUMMARY", _FY_SCHEDULES)],
    ids=["every-write", "from-the-summary"],
)
def test_run_all_stderr_failure_exits_4_and_removes_every_output(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    console: Callable[[str | None], _Console],
    break_at: str,
    ran: list[str],
) -> None:
    _seed_run_all(tmp_path / "out")
    invoke = spy_on(monkeypatch, cli, "_invoke_run_all_handler")
    stderr = console(break_at)

    with contextlib.redirect_stderr(stderr):
        assert _run_all(tmp_path) == 4

    assert _ran(invoke) == ran
    assert _left(tmp_path / "out") == []
    assert stderr.settled


def test_run_all_interrupted_between_schedules_removes_every_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Ctrl-C between schedules leaves no mix of this run's and an earlier
    run's files."""
    out = tmp_path / "out"
    _seed_run_all(out)
    _fail_with(monkeypatch, "_schedule_cg_step", KeyboardInterrupt(), out)

    with pytest.raises(KeyboardInterrupt):
        _run_all(tmp_path)

    assert _left(out) == []


_SOURCE_ALL = [
    pytest.param(
        NETBENEFITS_ALL,
        "netbenefits-all",
        ("_run_netbenefits_fa", "fa"),
        "netbenefits-income",
        cli._NETBENEFITS_ALL_TARGETS,
        id="netbenefits-all",
    ),
    pytest.param(
        STOCKPLAN_ALL,
        "stockplan-all",
        ("_run_stockplan_income", "income"),
        "stockplan-fa",
        cli._STOCKPLAN_ALL_TARGETS,
        id="stockplan-all",
    ),
]


def _seed_source_all(out: Path, targets: dict[str, tuple[str, ...]]) -> None:
    _seed(out, (out / name for names in targets.values() for name in names))


@pytest.mark.parametrize("at", ["second-header", "first-report"])
@pytest.mark.parametrize(("harness", "orchestrator", "first", "second", "targets"), _SOURCE_ALL)
def test_source_all_stdout_failure_stops_the_run_and_removes_every_output(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    console: Callable[[str | None], _Console],
    harness: SourceAll,
    orchestrator: str,
    first: tuple[str, str],
    second: str,
    targets: dict[str, tuple[str, ...]],
    at: str,
) -> None:
    """A failed print in a leg handler is a console failure, not a crash: exit 4."""
    out = tmp_path / "out"
    _seed_source_all(out, targets)
    calls = harness.patch_handlers(monkeypatch, harness.all_ok)
    handler_name, first_key = first
    handler = getattr(cli, handler_name)

    def reporting(args: object) -> int:
        code = handler(args)
        sys.stdout.write(f"{_FIRST_REPORT}\n")
        return code

    monkeypatch.setattr(cli, handler_name, reporting)
    stdout = console(f"  {orchestrator}: {second}\n" if at == "second-header" else _FIRST_REPORT)

    with contextlib.redirect_stdout(stdout):
        rc, _calls, _out = harness.run(tmp_path, monkeypatch, harness.all_ok, patch=False)

    assert rc == 4
    assert list(calls) == [first_key]
    assert _left(out) == []
    pointer = f"{cli._STOCKPLAN_CONTRACT_POINTER}\n" if orchestrator == "stockplan-all" else ""
    assert capsys.readouterr().err == f"{_broken_pipe(orchestrator)}{pointer}"
    assert stdout.settled


def test_stockplan_all_with_both_streams_broken_exits_4_and_removes_every_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, console: Callable[[str | None], _Console]
) -> None:
    out = tmp_path / "out"
    _seed_source_all(out, cli._STOCKPLAN_ALL_TARGETS)
    calls = STOCKPLAN_ALL.patch_handlers(monkeypatch, STOCKPLAN_ALL.all_ok)
    stdout, stderr = console("  stockplan-all: stockplan-fa\n"), console("")

    with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
        rc, _calls, _out = STOCKPLAN_ALL.run(
            tmp_path, monkeypatch, STOCKPLAN_ALL.all_ok, patch=False
        )

    assert rc == 4
    assert list(calls) == ["income"]
    assert _left(out) == []
    assert stdout.settled
    assert stderr.settled


# --- A report that fails to print (#1227) ---------------------------------------

# Markers the injected console and removal failures carry, which no error may print.
_PRINT_STRERROR = "Synthetic Print Strerror"
_PRINT_FILE = "Synthetic Print File"
_ENCODE_TEXT = "Synthetic Encode Text"
_ENCODE_REASON = "Synthetic Encode Reason"


def _pipe_error() -> BrokenPipeError:
    return BrokenPipeError(errno.EPIPE, _PRINT_STRERROR, _PRINT_FILE)


def _encode_error() -> UnicodeEncodeError:
    """What a strict ASCII stdout raises for text it cannot encode. Python
    writes stderr with ``backslashreplace``, so stderr never raises it."""
    return UnicodeEncodeError("ascii", _ENCODE_TEXT, 0, 1, _ENCODE_REASON)


def _removal_refusal(path: Path) -> str:
    """The refusal of a run that failed and could not remove ``path``."""
    return (
        "error: the run failed, and its output may still be there: "
        f"could not remove {path} (EACCES)\n"
    )


def _stream(renderer: str) -> str:
    """The stream that prints what ``renderer`` renders."""
    return "stderr" if "warnings" in renderer else "stdout"


def _owned(report: Any) -> Path:
    """The first output the run of ``report`` owns, in removal order, that exists."""
    outputs = report.run_outputs
    paths = (*outputs.paths, *(claim._path for claim in outputs.claims))
    return next(path for path in paths if path.exists())


def _break_after(
    monkeypatch: pytest.MonkeyPatch,
    renderer: str,
    stream: _Console,
    on_render: Callable[[Any], object],
) -> None:
    """Break ``stream`` once ``cli.<renderer>`` has rendered, so the command's
    next write to it fails. First calls ``on_render`` with the report rendered,
    then drops what ``stream`` holds."""
    render = getattr(cli, renderer)

    def rendered(report: Any, *args: Any, **kwargs: Any) -> object:
        text = render(report, *args, **kwargs)
        on_render(report)
        stream.seek(0)
        stream.truncate()
        stream.broken = True
        return text

    monkeypatch.setattr(cli, renderer, rendered)


def _fail_removal(monkeypatch: pytest.MonkeyPatch, failing: list[Path], *, once: bool) -> None:
    """Make removing an existing file in ``failing`` raise the marker
    ``PermissionError``; with ``once``, only the first time for each."""
    unlink = Path.unlink

    def fail(path: Path, missing_ok: bool = False) -> None:
        if path in failing and path.exists():
            if once:
                failing.remove(path)
            raise marker_permission_error()
        unlink(path, missing_ok=missing_ok)

    monkeypatch.setattr(Path, "unlink", fail)


def _redirect(stream_name: str, stream: _Console) -> contextlib.AbstractContextManager[object]:
    if stream_name == "stderr":
        return contextlib.redirect_stderr(stream)
    return contextlib.redirect_stdout(stream)


def _id(name: str, renderer: str, flags: tuple[str, ...]) -> str:
    return f"{name}-{renderer}{'-quiet' * bool(flags)}"


def _unverified_fx(how: Callable[[str], _Prepare]) -> Callable[[str], _Prepare]:
    """``how``, with netbenefits-cg warning that the FX policy is unverified.
    Its fixture's year proves the policy and has in-year rows, so the run would
    print no stderr block."""

    def make(name: str) -> _Prepare:
        def prepare(
            tmp_path: Path, monkeypatch: pytest.MonkeyPatch, request: pytest.FixtureRequest
        ) -> _Run:
            render = cli._render_netbenefits_cg_warnings
            monkeypatch.setattr(
                cli,
                "_render_netbenefits_cg_warnings",
                lambda report: render(
                    dataclasses.replace(report, fx_policy_appropriate_for_ay=False)
                ),
            )
            return how(name)(tmp_path, monkeypatch, request)

        return prepare

    return make


# Each block a command prints: a pipe whose reader is gone, then on stderr one
# that works again for the refusal, and on stdout text it cannot encode.
_PRINTS = [
    pytest.param(
        name,
        _unverified_fx(how) if name == "netbenefits-cg" else how,
        renderer,
        flags,
        failure,
        id=f"{_id(name, renderer, flags)}-{failure}",
    )
    for name, how, renderer, flags in _RENDERERS
    for failure in ("pipe", "pipe-once" if _stream(renderer) == "stderr" else "encoding")
]
# Each command's stdout report.
_REPORTS = [
    pytest.param(name, how, renderer, id=_id(name, renderer, flags))
    for name, how, renderer, flags in _RENDERERS
    if _stream(renderer) == "stdout" and not flags
]


@pytest.mark.parametrize(("name", "how", "renderer", "flags", "failure"), _PRINTS)
def test_report_that_cannot_be_printed_removes_the_outputs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    request: pytest.FixtureRequest,
    capsys: pytest.CaptureFixture[str],
    console: Callable[..., _Console],
    name: str,
    how: Callable[[str], _Prepare],
    renderer: str,
    flags: tuple[str, ...],
    failure: str,
) -> None:
    """The run then exits as it did before: a broken pipe exits 4, refused
    while stderr works, and an encoding failure exits 1 as a crash (#1049)."""
    run = how(name)(tmp_path, monkeypatch, request)
    run = _Run(partial(run.cli, *flags), run.out) if flags else run
    written: list[list[str]] = []
    blocks: list[str] = []
    printing = Mock(wraps=cli._print_report)
    monkeypatch.setattr(cli, "_print_report", printing)
    stream = console(
        None,
        error=_encode_error if failure == "encoding" else _pipe_error,
        once=failure != "pipe",
        on_break=lambda: blocks.append(capsys.readouterr().err),
    )

    def at_render(_report: object) -> None:
        written.append(_left(run.out))
        capsys.readouterr()

    _break_after(monkeypatch, renderer, stream, at_render)

    with _redirect(_stream(renderer), stream):
        code = run.cli()

    [outputs] = written
    assert outputs, "the run wrote its outputs before printing"
    assert _left(run.out) == []
    captured = capsys.readouterr()
    if _stream(renderer) == "stderr":
        assert code == 4
        assert captured.out == ""
        assert stream.getvalue() == ("" if failure == "pipe" else _broken_pipe("ibkr-fa"))
    else:
        # The stderr block printed in full, then the stdout report failed.
        [block] = blocks
        assert block == "".join(f"{line}\n" for line in printing.call_args.args[-2])
        assert stream.getvalue() == ""
        if failure == "pipe":
            assert code == 4
            assert captured.err == _broken_pipe("ibkr-fa")
        else:
            assert code == 1
            assert re.fullmatch(internal_error_line(UnicodeEncodeError), captured.err)
    if failure == "pipe":
        assert stream.settled


@pytest.mark.parametrize("failure", ["pipe", "pipe-once"])
def test_stockplan_fa_warning_that_cannot_be_printed_removes_the_outputs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    request: pytest.FixtureRequest,
    capsys: pytest.CaptureFixture[str],
    console: Callable[..., _Console],
    failure: str,
) -> None:
    """StockPlan FA builds its stderr warnings inline, before its stdout report."""
    run = _source("stockplan-fa")(tmp_path, monkeypatch, request)
    written: list[list[str]] = []
    stream = console(None, error=_pipe_error, once=failure == "pipe-once")

    def at_render(report: Any) -> None:
        assert report.warnings
        written.append(_left(run.out))
        capsys.readouterr()

    _break_after(monkeypatch, "_render_stockplan_fa_report", stream, at_render)

    with contextlib.redirect_stderr(stream):
        assert run.cli() == 4

    [outputs] = written
    assert outputs, "the run wrote its outputs before printing"
    assert _left(run.out) == []
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == ""
    assert stream.getvalue() == ("" if failure == "pipe" else _broken_pipe("ibkr-fa"))
    if failure == "pipe":
        assert stream.settled


@pytest.mark.parametrize("failure", ["pipe", "encoding"])
@pytest.mark.parametrize(("name", "how", "renderer"), _REPORTS)
def test_report_that_cannot_be_printed_whose_cleanup_fails_refuses_with_the_removal_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    request: pytest.FixtureRequest,
    capsys: pytest.CaptureFixture[str],
    console: Callable[..., _Console],
    name: str,
    how: Callable[[str], _Prepare],
    renderer: str,
    failure: str,
) -> None:
    """Every removal is attempted; the refusal names the output left (exit 4)."""
    run = how(name)(tmp_path, monkeypatch, request)
    stream = console(
        None,
        error=_encode_error if failure == "encoding" else _pipe_error,
        once=failure == "encoding",
        on_break=capsys.readouterr,
    )
    failing: list[Path] = []

    def at_render(report: Any) -> None:
        failing.append(_owned(report))
        capsys.readouterr()

    _break_after(monkeypatch, renderer, stream, at_render)
    _fail_removal(monkeypatch, failing, once=False)

    with contextlib.redirect_stdout(stream):
        assert run.cli() == 4

    [left] = failing
    assert capsys.readouterr().err == _removal_refusal(left)
    assert stream.getvalue() == ""
    assert _left(run.out) == [left.name]


@pytest.mark.parametrize("removal", ["removed", "unremovable"])
@pytest.mark.parametrize("failure", ["pipe", "encoding"])
@pytest.mark.parametrize(("name", "how", "renderer"), _REPORTS)
def test_report_that_cannot_be_printed_propagates_after_cleanup_with_the_traceback_flag(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    request: pytest.FixtureRequest,
    console: Callable[..., _Console],
    name: str,
    how: Callable[[str], _Prepare],
    renderer: str,
    failure: str,
    removal: str,
) -> None:
    """The failed write propagates; a removal error that replaced it keeps
    none of it (#1211)."""
    run = how(name)(tmp_path, monkeypatch, request)
    stream = console(
        None,
        error=_encode_error if failure == "encoding" else _pipe_error,
        once=failure == "encoding",
    )
    failing: list[Path] = []

    def at_render(report: Any) -> None:
        if removal == "unremovable":
            failing.append(_owned(report))

    _break_after(monkeypatch, renderer, stream, at_render)
    _fail_removal(monkeypatch, failing, once=False)
    monkeypatch.setenv("IBKR_FA_TRACEBACK", "1")

    with (
        contextlib.redirect_stdout(stream),
        pytest.raises((OSError, UnicodeEncodeError, OutputRemovalError)) as raised,
    ):
        run.cli()

    if removal == "removed":
        assert raised.value is stream.failures[0]
        assert _left(run.out) == []
    else:
        [left] = failing
        assert isinstance(raised.value, OutputRemovalError)
        assert f"error: {raised.value}\n" == _removal_refusal(left)
        crash = UnicodeEncodeError if failure == "encoding" else None
        markers = (
            (_ENCODE_TEXT, _ENCODE_REASON)
            if failure == "encoding"
            else (_PRINT_STRERROR, _PRINT_FILE)
        )
        assert_replaced_unchained(
            raised.value, crash, markers=(*markers, FILENAME_MARKER, STRERROR_MARKER)
        )
        assert _left(run.out) == [left.name]


@pytest.mark.parametrize("replaced", [False, True], ids=["published", "replaced"])
@pytest.mark.parametrize("name", _JSON)
def test_print_failure_removes_the_json_only_while_it_is_the_published_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    request: pytest.FixtureRequest,
    capsys: pytest.CaptureFixture[str],
    console: Callable[..., _Console],
    name: str,
    replaced: bool,
) -> None:
    prepare, renderer = _JSON[name]
    run, path = prepare(tmp_path, monkeypatch, request)
    published: list[bytes] = []
    stream = console(None, error=_pipe_error)

    def at_render(_report: object) -> None:
        published.append(path.read_bytes())
        if replaced:
            # Someone else's file at the same path, after publication.
            place_other_file(path)
        capsys.readouterr()

    _break_after(monkeypatch, renderer, stream, at_render)

    with contextlib.redirect_stdout(stream):
        assert run() == 4

    assert published, "the run published its JSON before printing"
    assert capsys.readouterr().err == _broken_pipe("ibkr-fa")
    if replaced:
        assert path.read_bytes() == OTHER_FRAGMENT_BYTES
    else:
        assert not path.exists()


_LEFT_BY_ROLLBACK = "the run failed, and its output may still be there"


@pytest.mark.parametrize("case", ["report", "report-unremovable", "error"])
def test_robinhood_all_print_failure_after_a_failed_rollback_retries_the_removals(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    console: Callable[..., _Console],
    case: str,
) -> None:
    """A failed run that could not remove an output prints its report, then
    that removal error. A print that fails attempts the removals again."""
    out = tmp_path / "out"
    stub_writing_all(monkeypatch, out, failing="options")
    left = out / "schedule-os.xlsx"
    _fail_removal(monkeypatch, [left], once=case != "report-unremovable")
    on_stderr = case == "error"
    stream = console(_LEFT_BY_ROLLBACK if on_stderr else None, error=_pipe_error)
    if not on_stderr:
        _break_after(monkeypatch, "_render_robinhood_all", stream, lambda _: capsys.readouterr())

    with _redirect("stderr" if on_stderr else "stdout", stream):
        assert robinhood_main_all(tmp_path) == 4

    captured = capsys.readouterr()
    if case == "report-unremovable":
        assert captured.err == _removal_refusal(left)
        assert _left(out) == [left.name]
    else:
        assert _left(out) == []
        if on_stderr:
            assert captured.out.startswith("ROBINHOOD SUMMARY")
            assert stream.getvalue() == ""
        else:
            assert captured.err == _broken_pipe("ibkr-fa")
    assert stream.settled


def test_robinhood_all_print_failure_keeps_the_identity_guidance(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    console: Callable[..., _Console],
) -> None:
    """A refusing leg's identity guidance is not a run output: a failed print
    removes every output but keeps it."""
    out = tmp_path / "out"
    written = stub_writing_all(monkeypatch, out)
    _stub_identity_refusals(monkeypatch)
    stream = console(None, error=_pipe_error)
    _break_after(monkeypatch, "_render_robinhood_all", stream, lambda _: capsys.readouterr())

    with contextlib.redirect_stdout(stream):
        assert robinhood_main_all(tmp_path) == 4

    assert capsys.readouterr().err == _broken_pipe("ibkr-fa")
    assert written
    assert _left(out) == [_GUIDANCE]
    _assert_guidance_file(out / _GUIDANCE)


_LEG_REPORT = "Synthetic Leg Report"
# (harness, orchestrator, first leg's handler and key, its pipeline function,
# the renderers it calls and what they return, its outputs)
_FIRST_LEGS = [
    pytest.param(
        NETBENEFITS_ALL,
        "netbenefits-all",
        ("_run_netbenefits_fa", "fa", "netbenefits-fa"),
        "run_netbenefits_fa",
        {"_render_netbenefits_fa_report": _LEG_REPORT},
        ("schedule-fa-a3.csv", "schedule-fa-a2.csv"),
        id="netbenefits-all",
    ),
    pytest.param(
        STOCKPLAN_ALL,
        "stockplan-all",
        ("_run_stockplan_income", "income", "stockplan-income"),
        "run_stockplan_income",
        {"_render_income_warnings": [], "_render_stockplan_income_report": _LEG_REPORT},
        INCOME_OUTPUTS,
        id="stockplan-all",
    ),
]


@pytest.mark.parametrize("removal", ["removed", "unremovable-once"])
@pytest.mark.parametrize("failure", ["pipe", "encoding"])
@pytest.mark.parametrize(
    ("harness", "orchestrator", "first", "pipeline", "renderers", "outputs"), _FIRST_LEGS
)
def test_source_all_leg_report_that_cannot_be_printed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    console: Callable[..., _Console],
    harness: SourceAll,
    orchestrator: str,
    first: tuple[str, str, str],
    pipeline: str,
    renderers: dict[str, object],
    outputs: tuple[str, ...],
    failure: str,
    removal: str,
) -> None:
    """A leg prints its report in its own run's cleanup scope. When a removal
    fails there too, its refusal stops the run before any later leg; the
    orchestrator's cleanup then attempts every removal again. Otherwise the
    failed write is handled as before: a broken pipe stops the run (#925), and
    an encoding failure is a crashed leg."""
    out = tmp_path / "out"
    handler, key, leg = first
    real = getattr(cli, handler)
    calls = harness.patch_handlers(monkeypatch, harness.all_ok)
    monkeypatch.setattr(cli, handler, real)

    def run_leg(**kwargs: Any) -> SimpleNamespace:
        folder = kwargs["output_dir"] if "output_dir" in kwargs else kwargs["output_path"].parent
        paths = tuple(folder / name for name in outputs)
        for path in paths:
            path.write_text("leg output", encoding="utf-8")
        return SimpleNamespace(run_outputs=RunOutputs(paths, ()))

    monkeypatch.setattr(cli, pipeline, run_leg)
    for renderer, rendered in renderers.items():
        monkeypatch.setattr(cli, renderer, lambda *_args, _rendered=rendered, **_kwargs: _rendered)
    unremovable = out / outputs[0]
    _fail_removal(monkeypatch, [unremovable] if removal != "removed" else [], once=True)
    stream = console(
        _LEG_REPORT,
        error=_encode_error if failure == "encoding" else _pipe_error,
        once=failure == "encoding",
    )

    with contextlib.redirect_stdout(stream):
        rc, _calls, _out = harness.run(tmp_path, monkeypatch, harness.all_ok, patch=False)

    err = capsys.readouterr().err
    pointer = f"{cli._STOCKPLAN_CONTRACT_POINTER}\n" if orchestrator == "stockplan-all" else ""
    assert _left(out) == []
    if removal != "removed":
        assert rc == 4
        assert list(calls) == []
        assert err == f"{_removal_refusal(unremovable)}{pointer}"
    elif failure == "pipe":
        assert rc == 4
        assert list(calls) == []
        assert err == f"{_broken_pipe(orchestrator)}{pointer}"
    else:
        assert rc == 1
        assert sorted(calls) == sorted(set(harness.all_ok) - {key})
        if orchestrator == "stockplan-all":
            successful = ["stockplan-fa"]
            results = [
                (leg, 1),
                ("stockplan-fa", 0),
                ("Schedule CG", cli._SOURCE_NOT_REQUIRED),
                ("Schedule FA Table A2 (complete daily cash/NAV evidence unavailable)", 3),
            ]
        else:
            successful = ["netbenefits-income", "netbenefits-cg"]
            results = [(leg, 1), *((name, 0) for name in successful)]
        summary = cli._render_source_all_summary(
            results,
            financial_year=cli.FinancialYear(2025 if orchestrator == "stockplan-all" else 2026),
            orchestrator=orchestrator,
            notes=dict.fromkeys(successful, "output removed because the run failed"),
        )
        crashed = re.escape(f"error: {orchestrator}: {leg} crashed unexpectedly (")
        assert re.fullmatch(
            rf"{crashed}{crash_pattern(UnicodeEncodeError)}\)\n{re.escape(summary)}\n", err
        )


# --- A report whose flush fails (#1236) -----------------------------------------

# Markers the injected flush failures carry, which no error may print.
_FLUSH_STRERROR = "Synthetic Flush Strerror"
_FLUSH_FILE = "Synthetic Flush File"
# Larger than any report, so only a flush reaches the pipe.
_BUFFER_SIZE = 1 << 20


def _on_devnull(fd: int) -> bool:
    """Whether the run pointed ``fd`` at ``os.devnull``."""
    return os.path.samestat(os.fstat(fd), Path(os.devnull).stat())


def _flush_pipe_error() -> BrokenPipeError:
    return BrokenPipeError(errno.EPIPE, _FLUSH_STRERROR, _FLUSH_FILE)


class _Pipe(io.RawIOBase):
    """The write end of a pipe whose reader is gone, under a block-buffered
    stdout as Python opens one over a pipe or a file.

    Once ``broken``, every write raises ``error()`` until the run points the
    descriptor at ``os.devnull``; it then writes there. Before that it writes
    to the descriptor, a real file's. The first failed write calls
    ``on_break`` once. Each failed write's bytes are kept in ``refused``, and
    its error in ``failures``.
    """

    def __init__(
        self,
        fd: int,
        *,
        broken: bool,
        error: Callable[[], OSError],
        on_break: Callable[[], object],
    ) -> None:
        super().__init__()
        self._fd = fd
        self.broken = broken
        self._error = error
        self._on_break = on_break
        self.refused: list[bytes] = []
        self.failures: list[OSError] = []

    def writable(self) -> bool:
        return True

    def fileno(self) -> int:
        return self._fd

    def write(self, data: Any) -> int:
        if self.broken and not _on_devnull(self._fd):
            if not self.failures:
                self._on_break()
            self.refused.append(bytes(data))
            self.failures.append(self._error())
            raise self.failures[-1]
        return os.write(self._fd, data)


@pytest.fixture
def pipe() -> Iterator[Callable[..., tuple[io.TextIOWrapper, _Pipe]]]:
    """Make block-buffered consoles over a :class:`_Pipe`, each on its own
    temporary file. At teardown, each discards what it still buffers."""
    with contextlib.ExitStack() as files:

        def make(
            *,
            broken: bool = True,
            error: Callable[[], OSError] = _flush_pipe_error,
            on_break: Callable[[], object] = lambda: None,
        ) -> tuple[io.TextIOWrapper, _Pipe]:
            fd = files.enter_context(tempfile.TemporaryFile()).fileno()
            raw = _Pipe(fd, broken=broken, error=error, on_break=on_break)
            stream = io.TextIOWrapper(
                io.BufferedWriter(raw, buffer_size=_BUFFER_SIZE), encoding="utf-8"
            )

            def discard() -> None:
                raw.broken = False
                stream.close()

            files.callback(discard)
            return stream, raw

        yield make


def _on_render(
    monkeypatch: pytest.MonkeyPatch, renderer: str, on_render: Callable[[Any], object]
) -> None:
    """Call ``on_render`` with each report ``cli.<renderer>`` has rendered."""
    render = getattr(cli, renderer)

    def rendered(report: Any, *args: Any, **kwargs: Any) -> object:
        text = render(report, *args, **kwargs)
        on_render(report)
        return text

    monkeypatch.setattr(cli, renderer, rendered)


# Each command's stdout report, verbose and ``--quiet``.
_STDOUT_REPORTS = [
    pytest.param(name, how, renderer, flags, id=_id(name, renderer, flags))
    for name, how, renderer, flags in _RENDERERS
    if _stream(renderer) == "stdout"
]


def _with_flags(run: _Run, flags: tuple[str, ...]) -> _Run:
    return _Run(partial(run.cli, *flags), run.out) if flags else run


# What stderr ends with after each failed flush: the refusal, or nothing once
# stderr broke too.
_FLUSH_FAILURES = {
    "pipe": (_flush_pipe_error, _broken_pipe("ibkr-fa")),
    "pipe-and-stderr": (_flush_pipe_error, ""),
    "no-space": (
        lambda: OSError(errno.ENOSPC, _FLUSH_STRERROR, _FLUSH_FILE),
        "error: ibkr-fa: a file operation failed (ENOSPC)\n",
    ),
}


@pytest.mark.parametrize("failure", _FLUSH_FAILURES)
@pytest.mark.parametrize(("name", "how", "renderer", "flags"), _STDOUT_REPORTS)
def test_report_whose_flush_fails_removes_the_outputs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    request: pytest.FixtureRequest,
    console: Callable[..., _Console],
    pipe: Callable[..., tuple[io.TextIOWrapper, _Pipe]],
    name: str,
    how: Callable[[str], _Prepare],
    renderer: str,
    flags: tuple[str, ...],
    failure: str,
) -> None:
    """The report lands in stdout's buffer, and only its flush fails. Python
    would otherwise first flush it at exit, outside every cleanup scope."""
    run = _with_flags(how(name)(tmp_path, monkeypatch, request), flags)
    printing = Mock(wraps=cli._print_report)
    monkeypatch.setattr(cli, "_print_report", printing)
    stderr = console(None, error=_pipe_error)
    at_break: list[tuple[list[str], str]] = []

    def on_break() -> None:
        at_break.append((_left(run.out), stderr.getvalue()))
        stderr.broken = failure == "pipe-and-stderr"

    error, refusal = _FLUSH_FAILURES[failure]
    stdout, raw = pipe(error=error, on_break=on_break)

    def at_render(_report: object) -> None:
        stderr.seek(0)
        stderr.truncate()

    _on_render(monkeypatch, renderer, at_render)

    with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
        code = run.cli()

    assert code == 4
    [(outputs, block)] = at_break
    assert outputs, "the run wrote its outputs before the flush"
    assert _left(run.out) == []
    # The stderr block printed in full, then the report's flush failed.
    *_, stderr_lines, report = printing.call_args.args
    assert block == "".join(f"{line}\n" for line in stderr_lines)
    assert raw.refused[0] == f"{report}\n".encode()
    assert stderr.getvalue() == f"{block}{refusal}"
    assert _on_devnull(stdout.fileno())
    if failure == "pipe-and-stderr":
        assert stderr.settled


@pytest.mark.parametrize(("name", "how", "renderer", "flags"), _STDOUT_REPORTS)
def test_report_whose_flush_fails_and_whose_cleanup_fails_refuses_with_the_removal_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    request: pytest.FixtureRequest,
    capsys: pytest.CaptureFixture[str],
    pipe: Callable[..., tuple[io.TextIOWrapper, _Pipe]],
    name: str,
    how: Callable[[str], _Prepare],
    renderer: str,
    flags: tuple[str, ...],
) -> None:
    """Every removal is attempted; the refusal names the output left (exit 4)."""
    run = _with_flags(how(name)(tmp_path, monkeypatch, request), flags)
    failing: list[Path] = []
    _on_render(monkeypatch, renderer, lambda report: failing.append(_owned(report)))
    _fail_removal(monkeypatch, failing, once=False)
    stdout, raw = pipe(on_break=capsys.readouterr)

    with contextlib.redirect_stdout(stdout):
        assert run.cli() == 4

    [left] = failing
    assert raw.failures
    assert capsys.readouterr().err == _removal_refusal(left)
    assert _left(run.out) == [left.name]


@pytest.mark.parametrize("removal", ["removed", "unremovable"])
@pytest.mark.parametrize(("name", "how", "renderer", "flags"), _STDOUT_REPORTS)
def test_report_whose_flush_fails_propagates_after_cleanup_with_the_traceback_flag(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    request: pytest.FixtureRequest,
    pipe: Callable[..., tuple[io.TextIOWrapper, _Pipe]],
    name: str,
    how: Callable[[str], _Prepare],
    renderer: str,
    flags: tuple[str, ...],
    removal: str,
) -> None:
    """The failed flush propagates; a removal error that replaced it keeps
    none of it (#1211)."""
    run = _with_flags(how(name)(tmp_path, monkeypatch, request), flags)
    failing: list[Path] = []
    if removal == "unremovable":
        _on_render(monkeypatch, renderer, lambda report: failing.append(_owned(report)))
    _fail_removal(monkeypatch, failing, once=False)
    stdout, raw = pipe()
    monkeypatch.setenv("IBKR_FA_TRACEBACK", "1")

    with (
        contextlib.redirect_stdout(stdout),
        pytest.raises((OSError, OutputRemovalError)) as raised,
    ):
        run.cli()

    if removal == "removed":
        assert raised.value is raw.failures[0]
        assert _left(run.out) == []
    else:
        [left] = failing
        assert isinstance(raised.value, OutputRemovalError)
        assert f"error: {raised.value}\n" == _removal_refusal(left)
        assert_replaced_unchained(
            raised.value,
            markers=(_FLUSH_STRERROR, _FLUSH_FILE, FILENAME_MARKER, STRERROR_MARKER),
        )
        assert _left(run.out) == [left.name]


@pytest.mark.parametrize("removal", ["removed", "unremovable-once"])
@pytest.mark.parametrize(
    ("harness", "orchestrator", "first", "pipeline", "renderers", "outputs"), _FIRST_LEGS
)
def test_source_all_leg_report_whose_flush_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    pipe: Callable[..., tuple[io.TextIOWrapper, _Pipe]],
    harness: SourceAll,
    orchestrator: str,
    first: tuple[str, str, str],
    pipeline: str,
    renderers: dict[str, object],
    outputs: tuple[str, ...],
    removal: str,
) -> None:
    """The reader exits while the first leg runs. The leg's own flush fails,
    with only its report pending, in its run's cleanup scope, so no later
    leg runs. When a removal fails there too, its refusal stops the run; the
    orchestrator's cleanup then attempts every removal again."""
    out = tmp_path / "out"
    handler, _key, _leg = first
    real = getattr(cli, handler)
    calls = harness.patch_handlers(monkeypatch, harness.all_ok)
    monkeypatch.setattr(cli, handler, real)
    stdout, raw = pipe(broken=False)

    def run_leg(**kwargs: Any) -> SimpleNamespace:
        folder = kwargs["output_dir"] if "output_dir" in kwargs else kwargs["output_path"].parent
        paths = tuple(folder / name for name in outputs)
        for path in paths:
            path.write_text("leg output", encoding="utf-8")
        raw.broken = True
        return SimpleNamespace(run_outputs=RunOutputs(paths, ()))

    monkeypatch.setattr(cli, pipeline, run_leg)
    for renderer, rendered in renderers.items():
        monkeypatch.setattr(cli, renderer, lambda *_args, _rendered=rendered, **_kwargs: _rendered)
    unremovable = out / outputs[0]
    _fail_removal(monkeypatch, [unremovable] if removal != "removed" else [], once=True)

    with contextlib.redirect_stdout(stdout):
        rc, _calls, _out = harness.run(tmp_path, monkeypatch, harness.all_ok, patch=False)

    assert rc == 4
    assert list(calls) == []
    assert raw.refused[0] == f"{_LEG_REPORT}\n".encode()
    assert _left(out) == []
    pointer = f"{cli._STOCKPLAN_CONTRACT_POINTER}\n" if orchestrator == "stockplan-all" else ""
    refusal = _removal_refusal(unremovable) if removal != "removed" else _broken_pipe(orchestrator)
    assert capsys.readouterr().err == f"{refusal}{pointer}"


@pytest.mark.parametrize("format_id", ["filing-csv", "filing-xlsx"])
def test_schedule_os_process_whose_report_flush_fails_exits_4_and_removes_its_output(
    tmp_path: Path, format_id: str
) -> None:
    """``schedule-os ... | head`` after the reader has exited: the report
    waits in stdout's buffer, and its flush fails in the run's cleanup scope.
    Python's flush at exit would print ``Exception ignored``, exit 120 and
    leave the output."""
    config = tmp_path / "user.toml"
    config.write_text(SCHEDULE_OS_USER_TOML, encoding="utf-8")
    out = tmp_path / "out"
    out.mkdir()
    suffix = ".xlsx" if format_id == "filing-xlsx" else ".csv"
    argv = [
        "schedule-os",
        *("--input", str(SCHEDULE_OS_DIVIDENDS_STATEMENT)),
        *("--config", str(config)),
        *("--output", str(out / f"schedule-os{suffix}")),
        *("--financial-year", "2025-26"),
        *("--format", format_id),
    ]
    read_end, write_end = os.pipe()
    os.close(read_end)
    try:
        result = subprocess.run(
            [sys.executable, "-m", "ibkr_fa.cli", *argv],
            stdout=write_end,
            stderr=subprocess.PIPE,
            cwd=tmp_path,
            check=False,
            timeout=120,
        )
    finally:
        os.close(write_end)

    assert result.returncode == 4, result.stderr
    assert result.stderr.endswith(b"\nerror: ibkr-fa: a file operation failed (EPIPE)\n")
    assert b"Exception ignored" not in result.stderr
    assert list(out.iterdir()) == []
