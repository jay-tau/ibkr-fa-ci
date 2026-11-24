"""The CLI never prints exception text it doesn't own (#1049).

A crash prints a fixed line with its class and innermost ``ibkr_fa`` source
line, never its message, cause or context; ``IBKR_FA_TRACEBACK=1`` re-raises it
after cleanup instead. An ``OSError`` prints fixed text, the path the call site
used and its errno name, never its text or file names. The custom financial-year
and USD-amount type refusals name the option and the rule, never the value typed.
"""

from __future__ import annotations

import argparse
import errno
import io
import os
import re
import subprocess
import sys
import textwrap
from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace
from typing import NoReturn

import pytest

from ibkr_fa import cli
from ibkr_fa import config as config_module
from ibkr_fa.cli import main
from ibkr_fa.compute.form67 import Form67ComputeResult
from ibkr_fa.compute.schedule_fsi import ScheduleFsiComputeResult
from ibkr_fa.exceptions import IbkrFaError, PathRefusalError
from ibkr_fa.output._cleanup import OutputRemovalError
from ibkr_fa.output.robinhood_options_csv import OPTIONS_FILENAMES
from ibkr_fa.pipeline_robinhood_bundle import refuse_robinhood_input_collisions
from tests._crash_reports import (
    CAUSE,
    CONTEXT,
    MARKERS,
    MESSAGE,
    assert_no_markers,
    assert_replaced_unchained,
    chained_crash,
    cli_site,
    raise_context,
)
from tests._robinhood_refusals import PRIVATE_OUTPUT_DIR_REFUSAL
from tests._run_all_fakes import (
    RUN_ALL_SCHEDULE_NAMES,
    fake_run_all_argv,
    fake_step_report,
    patch_all_steps,
)
from tests._source_all import NETBENEFITS_ALL, STOCKPLAN_ALL, SourceAll
from tests._source_commands import SOURCE_FY_ARGS, prepare_source_command

_TRACEBACK = "IBKR_FA_TRACEBACK"
# Markers an OSError carries in its text and both file names.
_STRERROR = "Synthetic Strerror Marker"
_FILENAMES = (f"{MESSAGE}/a", f"{CONTEXT}/b")


def _marked_oserror(code: int | None = errno.EACCES) -> OSError:
    """An ``OSError`` with markers in its strerror and both file names."""
    return OSError(code, _STRERROR, _FILENAMES[0], None, _FILENAMES[1])


def _assert_clean(text: str) -> None:
    """No crash marker, OS text or traceback in ``text``."""
    assert_no_markers(text, *MARKERS, _STRERROR)
    assert "Traceback" not in text


_MESSAGES = [MESSAGE, "ANN"]

# --- main() ------------------------------------------------------------------


def _init(tmp_path: Path) -> list[str]:
    return ["init", str(tmp_path / "user.toml")]


@pytest.mark.parametrize("message", _MESSAGES)
def test_main_prints_a_crash_as_its_class_and_source_line(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    message: str,
) -> None:
    monkeypatch.setattr(cli, "_run_init", chained_crash(RuntimeError, message))

    assert main(_init(tmp_path)) == 1

    captured = capsys.readouterr()
    site = cli_site(RuntimeError, "return _run_init(args)")
    assert captured.err == f"error: internal error ({site})\n"
    assert captured.out == ""
    _assert_clean(captured.err)


def test_main_names_the_innermost_package_frame(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A crash deeper in the package is named where it was raised, not where
    the CLI called in."""

    def crash_inside_the_package(_path: Path, *, force: bool) -> str:
        return cli.errno_name(None)  # ty: ignore[invalid-argument-type]

    monkeypatch.setattr(cli, "write_user_toml_template", crash_inside_the_package)

    assert main(_init(tmp_path)) == 1

    lines = Path(cli.__file__).with_name("exceptions.py").read_text(encoding="utf-8")
    [number] = [
        index
        for index, line in enumerate(lines.splitlines(), start=1)
        if line.strip() == "code = exc.errno"
    ]
    assert capsys.readouterr().err == (
        f"error: internal error (AttributeError at ibkr_fa/exceptions.py:{number})\n"
    )


def test_main_reraises_a_crash_with_the_traceback_flag(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(cli, "_run_init", chained_crash())
    monkeypatch.setenv(_TRACEBACK, "1")

    with pytest.raises(RuntimeError, match=MESSAGE) as raised:
        main(_init(tmp_path))

    assert isinstance(raised.value.__cause__, KeyError)
    assert capsys.readouterr().err == ""


@pytest.mark.parametrize("value", ["0", "", "true", "yes", " 1"])
def test_only_the_value_1_turns_the_traceback_on(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    value: str,
) -> None:
    monkeypatch.setattr(cli, "_run_init", chained_crash())
    monkeypatch.setenv(_TRACEBACK, value)

    assert main(_init(tmp_path)) == 1
    _assert_clean(capsys.readouterr().err)


def test_main_refuses_an_escaped_filesystem_error_with_its_errno_name(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """``init``'s success report fails to print: the error escapes the handler
    and is refused with exit 4, naming no file."""
    path = tmp_path / "user.toml"

    class _Stdout(io.StringIO):
        def write(self, _text: str) -> NoReturn:
            raise OSError(errno.EACCES, _STRERROR, f"{MESSAGE}/x")

    monkeypatch.setattr(sys, "stdout", _Stdout())

    assert main(["init", str(path)]) == 4

    err = capsys.readouterr().err
    assert err == "error: ibkr-fa: a file operation failed (EACCES)\n"
    assert path.exists()


@pytest.mark.parametrize(
    "error",
    [IbkrFaError("the run refused"), PathRefusalError("user.toml not found", Path("cfg.toml"))],
    ids=["domain", "path-refusal"],
)
def test_main_relays_an_escaped_domain_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    error: IbkrFaError,
) -> None:
    def escaped(_args: argparse.Namespace) -> NoReturn:
        raise error from KeyError(CAUSE)

    monkeypatch.setattr(cli, "_run_init", escaped)

    assert main(_init(tmp_path)) == 4
    assert capsys.readouterr().err == f"error: {error}\n"


# --- Orchestrator catch-alls -------------------------------------------------

_SOURCE_ALL = [
    pytest.param(
        NETBENEFITS_ALL,
        "netbenefits-all",
        "netbenefits_form67_readiness",
        ("_run_netbenefits_income", "netbenefits-income"),
        id="netbenefits-all",
    ),
    pytest.param(
        STOCKPLAN_ALL,
        "stockplan-all",
        "stockplan_form67_readiness",
        ("_run_stockplan_fa", "stockplan-fa"),
        id="stockplan-all",
    ),
]


@pytest.mark.parametrize("message", _MESSAGES)
@pytest.mark.parametrize(("harness", "orchestrator", "readiness", "leg"), _SOURCE_ALL)
def test_source_all_readiness_crash_prints_a_fixed_line(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    harness: SourceAll,
    orchestrator: str,
    readiness: str,
    leg: tuple[str, str],
    message: str,
) -> None:
    calls = harness.patch_handlers(monkeypatch, harness.all_ok)
    monkeypatch.setattr(cli, readiness, chained_crash(RuntimeError, message))

    rc, _calls, out = harness.run(tmp_path, monkeypatch, harness.all_ok, patch=False)

    assert rc == cli._EXIT_GENERIC
    assert calls == {}
    assert list(out.iterdir()) == []
    captured = capsys.readouterr()
    site = cli_site(RuntimeError, "promised_files = promised(form67_readiness())")
    assert captured.err == (
        f"error: {orchestrator}: the Form 67 readiness check crashed unexpectedly ({site})\n"
    )
    _assert_clean(captured.err + captured.out)


@pytest.mark.parametrize(("harness", "orchestrator", "readiness", "leg"), _SOURCE_ALL)
def test_source_all_readiness_crash_reraises_with_the_traceback_flag(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    harness: SourceAll,
    orchestrator: str,
    readiness: str,
    leg: tuple[str, str],
) -> None:
    calls = harness.patch_handlers(monkeypatch, harness.all_ok)
    monkeypatch.setattr(cli, readiness, chained_crash())
    monkeypatch.setenv(_TRACEBACK, "1")

    with pytest.raises(RuntimeError, match=MESSAGE):
        harness.run(tmp_path, monkeypatch, harness.all_ok, patch=False)

    assert calls == {}
    assert list((tmp_path / "out").iterdir()) == []


@pytest.mark.parametrize("message", _MESSAGES)
@pytest.mark.parametrize(("harness", "orchestrator", "readiness", "leg"), _SOURCE_ALL)
def test_source_all_leg_crash_prints_a_fixed_line(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    harness: SourceAll,
    orchestrator: str,
    readiness: str,
    leg: tuple[str, str],
    message: str,
) -> None:
    """The second leg crashes; the others still run, and every output goes."""
    calls = harness.patch_handlers(monkeypatch, harness.all_ok)
    handler, name = leg
    monkeypatch.setattr(cli, handler, chained_crash(RuntimeError, message))

    rc, _calls, out = harness.run(tmp_path, monkeypatch, harness.all_ok, patch=False)

    assert rc == cli._EXIT_GENERIC
    assert len(calls) == 2
    assert list(out.iterdir()) == []
    captured = capsys.readouterr()
    site = cli_site(RuntimeError, "code = handler(sub_args)")
    assert f"error: {orchestrator}: {name} crashed unexpectedly ({site})\n" in captured.err
    _assert_clean(captured.err + captured.out)


@pytest.mark.parametrize(("harness", "orchestrator", "readiness", "leg"), _SOURCE_ALL)
def test_source_all_leg_crash_reraises_after_cleanup_with_the_traceback_flag(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    harness: SourceAll,
    orchestrator: str,
    readiness: str,
    leg: tuple[str, str],
) -> None:
    """The first leg's outputs are removed by the run's cleanup scope before
    the crash propagates; no later leg runs."""
    calls = harness.patch_handlers(monkeypatch, harness.all_ok)
    monkeypatch.setattr(cli, leg[0], chained_crash())
    monkeypatch.setenv(_TRACEBACK, "1")

    with pytest.raises(RuntimeError, match=MESSAGE):
        harness.run(tmp_path, monkeypatch, harness.all_ok, patch=False)

    assert len(calls) == 1
    assert list((tmp_path / "out").iterdir()) == []


def _ready_preflight(monkeypatch: pytest.MonkeyPatch) -> None:
    """Stand in for run-all's Form 67 preflight, as ``test_cli_run_all`` does."""
    monkeypatch.setattr(
        cli,
        "_compute_schedule_fsi_for_pipeline",
        lambda **_kwargs: SimpleNamespace(
            form67_result=Form67ComputeResult(entries=()),
            fsi_result=ScheduleFsiComputeResult(rows=[]),
        ),
    )


def _run_all_paths(tmp_path: Path) -> tuple[Path, Path, Path, Path]:
    """The inputs ``fake_run_all_argv`` names (see ``test_cli_run_all.paths``)."""
    paths = tuple(tmp_path / name for name in ("fy.csv", "cy.csv", "user.toml", "out"))
    for path in paths[:3]:
        path.write_text(path.stem, encoding="utf-8")
    fy, cy, cfg, out = paths
    return fy, cy, cfg, out


def _seed_run_all(out: Path) -> None:
    out.mkdir()
    for name in RUN_ALL_SCHEDULE_NAMES:
        for target in cli._run_all_targets(out, name):
            target.write_text("an earlier run", encoding="utf-8")


@pytest.mark.parametrize("message", _MESSAGES)
def test_run_all_readiness_crash_prints_a_fixed_line(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    message: str,
) -> None:
    patch_all_steps(monkeypatch)
    _ready_preflight(monkeypatch)
    monkeypatch.setattr(
        cli, "_compute_schedule_fsi_for_pipeline", chained_crash(RuntimeError, message)
    )
    paths = _run_all_paths(tmp_path)
    _seed_run_all(paths[3])

    assert main(fake_run_all_argv(paths)) == cli._EXIT_GENERIC

    assert list(paths[3].iterdir()) == []
    captured = capsys.readouterr()
    site = cli_site(RuntimeError, "context = _compute_schedule_fsi_for_pipeline(")
    assert captured.err == (
        f"error: run-all: the Form 67 readiness check crashed unexpectedly ({site})\n"
    )
    _assert_clean(captured.err + captured.out)


def test_run_all_readiness_crash_reraises_after_cleanup_with_the_traceback_flag(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    patch_all_steps(monkeypatch)
    _ready_preflight(monkeypatch)
    monkeypatch.setattr(cli, "_compute_schedule_fsi_for_pipeline", chained_crash())
    monkeypatch.setenv(_TRACEBACK, "1")
    paths = _run_all_paths(tmp_path)
    _seed_run_all(paths[3])

    with pytest.raises(RuntimeError, match=MESSAGE):
        main(fake_run_all_argv(paths))

    assert list(paths[3].iterdir()) == []


def _crashing_step(message: str = MESSAGE) -> Callable[..., NoReturn]:
    """A run-all step that prints a diagnostic, then crashes."""
    crash = chained_crash(RuntimeError, message)

    def step(_args: argparse.Namespace, *, include_global_advisories: bool) -> NoReturn:
        sys.stderr.write("partial diagnostics\n")
        crash()

    return step


@pytest.mark.parametrize("message", _MESSAGES)
def test_run_all_schedule_crash_prints_a_fixed_line(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    message: str,
) -> None:
    patch_all_steps(monkeypatch)
    _ready_preflight(monkeypatch)
    monkeypatch.setattr(cli, "_schedule_fsi_step", _crashing_step(message))
    paths = _run_all_paths(tmp_path)

    assert main(fake_run_all_argv(paths)) == cli._EXIT_GENERIC

    assert list(paths[3].iterdir()) == []
    captured = capsys.readouterr()
    site = cli_site(RuntimeError, "code, report = step(sub_args, include_global_advisories=False)")
    detail = f"-> internal error: crashed unexpectedly ({site}); no output written\n"
    assert detail in captured.err
    assert "partial diagnostics" in captured.err
    _assert_clean(captured.err + captured.out)


def test_run_all_schedule_crash_reraises_after_replay_and_cleanup_with_the_traceback_flag(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    patch_all_steps(monkeypatch)
    _ready_preflight(monkeypatch)
    monkeypatch.setattr(cli, "_schedule_fsi_step", _crashing_step())
    monkeypatch.setenv(_TRACEBACK, "1")
    paths = _run_all_paths(tmp_path)

    with pytest.raises(RuntimeError, match=MESSAGE):
        main(fake_run_all_argv(paths))

    assert "partial diagnostics" in capsys.readouterr().err
    assert list(paths[3].iterdir()) == []


class _ReplayFails(io.StringIO):
    """stderr whose write of the replayed diagnostics fails, as a closed pipe's does."""

    def write(self, text: str) -> int:
        if "partial diagnostics" in text:
            raise OSError(errno.EPIPE, _STRERROR, _FILENAMES[0])
        return super().write(text)


@pytest.mark.parametrize("traceback", [False, True], ids=["fixed-line", "traceback"])
def test_run_all_replay_failure_after_a_schedule_crash(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, traceback: bool
) -> None:
    """The step crashes, then replaying its captured stderr fails. With the
    flag the failure re-raises with the crash as its context; without it the
    run refuses with exit 4, as before. Every output goes either way."""
    patch_all_steps(monkeypatch)
    _ready_preflight(monkeypatch)
    step = _crashing_step()

    def crash_with_cause(args: argparse.Namespace, *, include_global_advisories: bool) -> NoReturn:
        try:
            step(args, include_global_advisories=include_global_advisories)
        except RuntimeError:
            raise KeyError(CAUSE) from None

    monkeypatch.setattr(cli, "_schedule_cg_step", crash_with_cause)
    stderr = _ReplayFails()
    monkeypatch.setattr(sys, "stderr", stderr)
    paths = _run_all_paths(tmp_path)
    _seed_run_all(paths[3])

    if traceback:
        monkeypatch.setenv(_TRACEBACK, "1")
        with pytest.raises(OSError, match=_STRERROR) as raised:
            main(fake_run_all_argv(paths))
        assert isinstance(raised.value.__context__, KeyError)
    else:
        assert main(fake_run_all_argv(paths)) == 4
        assert stderr.getvalue() == "error: run-all: a file operation failed (EPIPE)\n"
    assert list(paths[3].iterdir()) == []


# --- A crash whose cleanup fails ---------------------------------------------


def _stockplan_income_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, request: pytest.FixtureRequest
) -> tuple[list[str], Path, Path]:
    """A real stockplan-income run whose report renderer raises
    ``KeyError('U1234567')`` and whose removal of ``schedule-os.csv`` then fails.
    Returns its argv, output folder and the output it can't remove."""
    run, _written = prepare_source_command("stockplan-income", tmp_path, monkeypatch, request)
    stuck = run.out / "schedule-os.csv"
    crashed: list[bool] = []

    def render(_report: object) -> NoReturn:
        crashed.append(True)
        raise KeyError(CAUSE)

    unlink = Path.unlink

    def unlink_fails_after_the_crash(path: Path, missing_ok: bool = False) -> None:
        if crashed and path == stuck:
            raise _marked_oserror()
        unlink(path, missing_ok=missing_ok)

    monkeypatch.setattr(cli, "_render_stockplan_income_report", render)
    monkeypatch.setattr(Path, "unlink", unlink_fails_after_the_crash)
    return run.argv, run.out, stuck


def _in_stockplan_all(monkeypatch: pytest.MonkeyPatch, argv: list[str]) -> list[str]:
    """``argv`` as a stockplan-all run whose income leg is real and whose other
    legs are stand-ins that succeed."""

    def fa(args: argparse.Namespace) -> int:
        args.output.write_text("placeholder", encoding="utf-8")
        return cli._EXIT_OK

    monkeypatch.setattr(cli, "_run_stockplan_fa", fa)
    monkeypatch.setattr(cli, "_run_stockplan_cg_status", lambda _args: cli._SOURCE_NOT_REQUIRED)
    return ["stockplan-all", *argv[1:]]


@pytest.mark.parametrize("orchestrated", [False, True], ids=["alone", "in-stockplan-all"])
def test_crash_whose_cleanup_fails_refuses_with_the_removal_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    request: pytest.FixtureRequest,
    capsys: pytest.CaptureFixture[str],
    orchestrated: bool,
) -> None:
    argv, _out, stuck = _stockplan_income_run(tmp_path, monkeypatch, request)
    if orchestrated:
        argv = _in_stockplan_all(monkeypatch, argv)

    assert main(argv) == 4

    captured = capsys.readouterr()
    assert (
        f"error: the run failed, and its output may still be there: could not remove {stuck} "
        "(EACCES)\n"
    ) in captured.err
    _assert_clean(captured.err + captured.out)


@pytest.mark.parametrize("orchestrated", [False, True], ids=["alone", "in-stockplan-all"])
def test_crash_whose_cleanup_fails_reraises_a_content_free_chain_with_the_traceback_flag(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    request: pytest.FixtureRequest,
    orchestrated: bool,
) -> None:
    """The removal error keeps none of the crash it replaced (#1211). Its only
    link, a stand-in naming the crash's class and line, still makes the flag
    re-raise it."""
    argv, _out, stuck = _stockplan_income_run(tmp_path, monkeypatch, request)
    if orchestrated:
        argv = _in_stockplan_all(monkeypatch, argv)
    monkeypatch.setenv(_TRACEBACK, "1")

    with pytest.raises(OutputRemovalError) as raised:
        main(argv)

    assert type(raised.value) is OutputRemovalError
    removal = (
        f"the run failed, and its output may still be there: could not remove {stuck} (EACCES)"
    )
    # stockplan-all's own cleanup fails on the same file and names it again.
    assert str(raised.value) == (f"{removal}; {removal}" if orchestrated else removal)
    assert_replaced_unchained(raised.value, KeyError, markers=(CAUSE, MESSAGE, CONTEXT, _STRERROR))
    site = cli_site(KeyError, "rendered = _render_stockplan_income_report(report)")
    assert str(raised.value.__cause__) == site


# --- Filesystem errors -------------------------------------------------------


@pytest.mark.parametrize(
    ("code", "name"),
    [(errno.EACCES, "EACCES"), (None, "unknown error"), (999_999, "unknown error")],
    ids=["mapped", "no-errno", "unmapped"],
)
def test_domain_error_names_a_filesystem_error_by_its_errno_name_only(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    code: int | None,
    name: str,
) -> None:
    def fail(**_kwargs: object) -> NoReturn:
        raise _marked_oserror(code)

    monkeypatch.setattr(cli, "merge_cbdt_fragment", fail)
    argv = ["merge", "--fragment", "fragment.json", "--itr", "itr.json"]

    assert main([*argv, "--output", str(tmp_path / "merged.json")]) == 4

    err = capsys.readouterr().err
    assert err == f"error: merge: a file operation failed ({name})\n"


def test_refusal_chained_only_from_domain_errors_prints_even_with_the_traceback_flag(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Only a chain that holds a crash re-raises under the flag."""

    def fail(**_kwargs: object) -> NoReturn:
        try:
            _raise(_marked_oserror())
        except OSError as exc:
            raise IbkrFaError("the run refused") from exc

    monkeypatch.setattr(cli, "merge_cbdt_fragment", fail)
    monkeypatch.setenv(_TRACEBACK, "1")
    argv = ["merge", "--fragment", "fragment.json", "--itr", "itr.json"]

    assert main([*argv, "--output", str(tmp_path / "merged.json")]) == 4
    assert capsys.readouterr().err == "error: the run refused\n"


def _raise(error: Exception) -> NoReturn:
    raise error


@pytest.mark.parametrize(
    ("code", "name"),
    [(errno.EACCES, "EACCES"), (None, "unknown error"), (999_999, "unknown error")],
    ids=["mapped", "no-errno", "unmapped"],
)
def test_preclear_names_only_the_typed_output_and_the_errno_name(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    code: int | None,
    name: str,
) -> None:
    path = tmp_path / "schedule-fa.json"
    path.write_text('{"_ibkrFa": {"format": "cbdt-json-fragment"}}', encoding="utf-8")
    unlink = Path.unlink

    def cannot_remove(target: Path, missing_ok: bool = False) -> None:
        if target == path:
            raise _marked_oserror(code)
        unlink(target, missing_ok=missing_ok)

    monkeypatch.setattr(Path, "unlink", cannot_remove)

    assert cli._preclear_cbdt_json_target(path) == 4
    err = capsys.readouterr().err
    assert err == f"error: could not remove the previous output {path} ({name})\n"


def test_init_names_the_typed_path_and_the_errno_name(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def fail(_path: Path, *, force: bool) -> NoReturn:
        raise _marked_oserror()

    monkeypatch.setattr(cli, "write_user_toml_template", fail)
    path = tmp_path / "user.toml"

    assert main(["init", str(path)]) == 4
    assert capsys.readouterr().err == f"error: could not write {path} (EACCES)\n"


def _mkdir_fails_for(monkeypatch: pytest.MonkeyPatch, folder: Path) -> None:
    mkdir = Path.mkdir

    def failing(path: Path, *args: object, **kwargs: object) -> None:
        if path == folder:
            raise _marked_oserror()
        mkdir(path, *args, **kwargs)  # ty: ignore[invalid-argument-type]

    monkeypatch.setattr(Path, "mkdir", failing)


@pytest.mark.parametrize(("harness", "orchestrator", "readiness", "leg"), _SOURCE_ALL)
def test_source_all_names_the_typed_output_dir_it_cannot_create(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    harness: SourceAll,
    orchestrator: str,
    readiness: str,
    leg: tuple[str, str],
) -> None:
    _mkdir_fails_for(monkeypatch, tmp_path / "out")

    rc, calls, _out = harness.run(tmp_path, monkeypatch, harness.all_ok)

    assert rc == 4
    assert calls == {}
    err = capsys.readouterr().err
    assert err.startswith(f"error: could not create --output-dir {tmp_path / 'out'} (EACCES)\n")
    _assert_clean(err)


def test_run_all_names_the_typed_output_dir_it_cannot_create(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    patch_all_steps(monkeypatch)
    _ready_preflight(monkeypatch)
    paths = _run_all_paths(tmp_path)
    _mkdir_fails_for(monkeypatch, paths[3])

    assert main(fake_run_all_argv(paths)) == 4
    assert capsys.readouterr().err == (
        f"error: could not create --output-dir {paths[3]} (EACCES)\n"
    )


def test_unproduced_output_names_the_output_and_the_errno_name(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    output = tmp_path / "schedule-os.csv"
    stat = Path.stat

    def failing(path: Path, *, follow_symlinks: bool = True) -> os.stat_result:
        if path == output:
            raise _marked_oserror()
        return stat(path, follow_symlinks=follow_symlinks)

    monkeypatch.setattr(Path, "stat", failing)

    assert cli._unproduced_outputs([output]) == f"could not check {output} (EACCES)"


@pytest.mark.parametrize("problem", ["not-regular", "cannot-stat"])
def test_withheld_form67_output_check_keeps_the_typed_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, problem: str
) -> None:
    monkeypatch.chdir(tmp_path)
    output = Path("out/form-67.csv")
    review = output.with_name("form-67-debug.csv")
    output.parent.mkdir()
    report = fake_step_report(
        "form-67", official_output_withheld=True, output_path=review.resolve()
    )
    stat = Path.stat

    def failing(path: Path, *, follow_symlinks: bool = True) -> os.stat_result:
        if path in (review, report.output_path):
            raise _marked_oserror()
        return stat(path, follow_symlinks=follow_symlinks)

    def step(
        args: argparse.Namespace, *, include_global_advisories: bool
    ) -> tuple[int, cli._RunAllReport | None]:
        if problem == "not-regular":
            review.mkdir()
        else:
            review.write_text("placeholder", encoding="utf-8")
            monkeypatch.setattr(Path, "stat", failing)
        return 3, report

    outcome, code = cli._invoke_run_all_handler(
        "form-67", step, argparse.Namespace(output=output), output, []
    )

    assert code == 4
    expected = (
        f"{review}: not a regular file"
        if problem == "not-regular"
        else f"could not check {review} (EACCES)"
    )
    assert outcome.detail == (
        f"exit 3 without its promised output: {expected}; no current output "
        "— ignore any existing files for this schedule"
    )


def test_stale_run_all_target_names_the_target_and_the_errno_name(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "schedule-os.xlsx"
    unlink = Path.unlink

    def failing(path: Path, missing_ok: bool = False) -> None:
        if path == target:
            raise _marked_oserror()
        unlink(path, missing_ok=missing_ok)

    monkeypatch.setattr(Path, "unlink", failing)

    with pytest.raises(OutputRemovalError) as raised:
        cli._clear_run_all_targets(tmp_path, "schedule-os")
    assert str(raised.value) == f"could not clear the stale output target {target} (EACCES)"


# --- Custom filesystem refusals ----------------------------------------------


def test_missing_config_prints_its_path_after_clearing_stale_output(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    out = tmp_path / "out"
    out.mkdir(mode=0o700)
    stale = out / OPTIONS_FILENAMES[0]
    stale.write_text("an earlier run", encoding="utf-8")
    config = tmp_path / "missing.toml"
    bundle = tmp_path / "bundle"
    for folder in ("activity", "statements", "tax-forms"):
        (bundle / folder).mkdir(parents=True)
    argv = ["robinhood-options", "--input", str(bundle), "--config", str(config)]

    assert main([*argv, "--output-dir", str(out), *SOURCE_FY_ARGS]) == 4

    assert capsys.readouterr().err == f"error: user.toml not found: {config}\n"
    assert not stale.exists()


def test_config_that_disappears_before_it_is_read_is_treated_as_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """``load_user_config`` checks the file, then opens it; the file can go in
    between, and the input-collision check names no config file then either."""
    config = tmp_path / "user.toml"
    exists = config_module.exists
    monkeypatch.setattr(config_module, "exists", lambda path: path == config or exists(path))

    assert (
        refuse_robinhood_input_collisions(
            (tmp_path / "out" / OPTIONS_FILENAMES[0],),
            input_dir=tmp_path / "bundle",
            config_path=config,
        )
        is None
    )
    out = tmp_path / "out"
    out.mkdir(mode=0o700)
    stale = out / OPTIONS_FILENAMES[0]
    stale.write_text("an earlier run", encoding="utf-8")
    bundle = tmp_path / "bundle"
    for folder in ("activity", "statements", "tax-forms"):
        (bundle / folder).mkdir(parents=True)
    argv = ["robinhood-options", "--input", str(bundle), "--config", str(config)]

    assert main([*argv, "--output-dir", str(out), *SOURCE_FY_ARGS]) == 4

    assert capsys.readouterr().err == "error: robinhood-options: a file operation failed (ENOENT)\n"
    assert not stale.exists()


def test_init_refuses_a_missing_parent_folder_with_its_path(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    path = tmp_path / "missing" / "user.toml"

    assert main(["init", str(path)]) == 4

    assert capsys.readouterr().err == (
        "error: parent directory does not exist (create it first, or choose an existing "
        f"directory): {path.parent}\n"
    )
    assert not path.exists()


@pytest.mark.skipif(os.name != "posix", reason="POSIX mode bits only")
def test_robinhood_refuses_a_shared_output_folder_with_its_path(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    out = tmp_path / "out"
    out.mkdir()
    out.chmod(0o755)
    argv = ["robinhood-options", "--input", str(tmp_path / "bundle")]

    assert main([*argv, "--config", "user.toml", "--output-dir", str(out), *SOURCE_FY_ARGS]) == 4

    assert capsys.readouterr().err == f"error: {PRIVATE_OUTPUT_DIR_REFUSAL}: {out}\n"


# --- Console failures while reporting ----------------------------------------


class _FailsOnce(io.StringIO):
    """A console stream whose first write fails, as a pipe's does when its
    reader is briefly gone."""

    def __init__(self) -> None:
        super().__init__()
        self.failed = False

    def write(self, text: str) -> int:
        if not self.failed:
            self.failed = True
            raise OSError(errno.EPIPE, "Synthetic Reporting Strerror", "Synthetic Reporting File")
        return super().write(text)


_REPORTERS = [
    pytest.param(_marked_oserror, cli._EXIT_DOMAIN_ERROR, id="domain-fallback"),
    pytest.param(lambda: RuntimeError(MESSAGE), cli._EXIT_GENERIC, id="catch-all"),
]


def _escaping(error: Callable[[], Exception]) -> Callable[[argparse.Namespace], NoReturn]:
    """A handler from which ``error`` escapes, chained from a cause and a context."""

    def handler(_args: argparse.Namespace) -> NoReturn:
        try:
            raise_context()
        except ValueError:
            raise error() from KeyError(CAUSE)

    return handler


@pytest.mark.parametrize(("error", "code"), _REPORTERS)
def test_reporter_whose_stderr_write_fails_keeps_its_exit_code(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    error: Callable[[], Exception],
    code: int,
) -> None:
    monkeypatch.setattr(cli, "_run_init", _escaping(error))
    stderr = _FailsOnce()
    monkeypatch.setattr(sys, "stderr", stderr)

    assert main(_init(tmp_path)) == code

    assert stderr.failed
    _assert_clean(stderr.getvalue() + capsys.readouterr().out)
    assert_no_markers(stderr.getvalue(), "Synthetic Reporting")


@pytest.mark.parametrize(("error", "code"), _REPORTERS)
def test_reporter_reraises_with_the_traceback_flag_even_with_a_broken_console(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    error: Callable[[], Exception],
    code: int,
) -> None:
    """The debug re-raise sits outside the console-failure handling."""
    monkeypatch.setattr(cli, "_run_init", _escaping(error))
    monkeypatch.setattr(sys, "stderr", _FailsOnce())
    monkeypatch.setenv(_TRACEBACK, "1")

    with pytest.raises(type(error())) as raised:
        main(_init(tmp_path))
    assert isinstance(raised.value.__cause__, KeyError)


_FAILED_RECOVERY = textwrap.dedent(
    """
    import errno, os, sys
    from ibkr_fa import cli

    class Broken:
        def write(self, text):
            raise OSError(errno.EPIPE, "Synthetic Pipe Strerror", "Synthetic Pipe File")

        def flush(self):
            self.write("")

        def fileno(self):
            return 2

    def no_descriptor(*_args, **_kwargs):
        raise OSError(errno.EMFILE, "Synthetic Open Strerror", "Synthetic Open File")

    def handler(_args):
        try:
            raise ValueError("Synthetic Context Marker")
        except ValueError:
            raise ERROR from KeyError("U1234567")

    cli._run_init = handler
    os.open = no_descriptor
    sys.stdout, sys.stderr = Broken(), Broken()
    raise SystemExit(cli.main(["init", "user.toml"]))
    """
)


@pytest.mark.parametrize(
    ("error", "code"),
    [
        pytest.param(
            'OSError(errno.EACCES, "Synthetic Strerror Marker", "Synthetic Holder Two/a")',
            cli._EXIT_DOMAIN_ERROR,
            id="domain-fallback",
        ),
        pytest.param('RuntimeError("Synthetic Holder Two")', cli._EXIT_GENERIC, id="catch-all"),
    ],
)
def test_reporter_with_a_broken_console_it_cannot_recover_exits_cleanly(
    tmp_path: Path, error: str, code: int
) -> None:
    """Every write and flush fails and ``os.devnull`` can't be opened: the
    process still exits with the reporter's code, not 120, with no traceback."""
    env = {key: value for key, value in os.environ.items() if key != _TRACEBACK}
    result = subprocess.run(
        [sys.executable, "-c", _FAILED_RECOVERY.replace("ERROR", error)],
        capture_output=True,
        cwd=tmp_path,
        env=env,
        check=False,
        timeout=60,
    )

    assert result.returncode == code, result.stderr
    assert result.stdout == b""
    assert result.stderr == b""
    assert not (tmp_path / "user.toml").exists()


# --- Option values -----------------------------------------------------------


def _schedule_os(*option: str) -> list[str]:
    return [
        "schedule-os",
        *("--input", "in.csv", "--output", "out.csv", "--config", "user.toml"),
        *option,
    ]


_FY_RULES = {
    "--financial-year must be in YYYY-YY or YYYY-YYYY form (e.g., '2025-26' or '2025-2026' "
    "for the April 2025 - March 2026 period).": [MESSAGE],
    "--financial-year's two-digit end year must be the last two digits of its start year + 1 "
    "(e.g., '2025-26').": ["5678-80"],
    "--financial-year's end year must equal its start year + 1 (e.g., '2025-2026'; an Indian "
    "FY spans exactly one year, April 1 - March 31).": ["5678-5680"],
    "--financial-year must start in a year from 1900 to 9998.": ["1234-35"],
}
_USD_RULES = {
    "must be a USD amount, such as 100.25": ["U1234567.5"],
    "must be a non-negative, finite USD amount": ["NaN1234567"],
    "must have at most 2 decimal places (whole cents)": ["1234567.123"],
}
_USD_OPTION = "--reconcile-1042s-fy-withholding-usd"


def _netbenefits_income(*option: str) -> list[str]:
    return [
        "netbenefits-income",
        *("--input", "in", "--config", "user.toml", "--output-dir", "out"),
        *SOURCE_FY_ARGS,
        *option,
    ]


@pytest.mark.parametrize(
    ("argv", "option", "rule", "value"),
    [
        *(
            pytest.param(
                _schedule_os("--financial-year", value, "--itr-form", "ITR-2"),
                "--financial-year",
                rule,
                value,
                id=f"fy-{value}",
            )
            for rule, values in _FY_RULES.items()
            for value in values
        ),
        *(
            pytest.param(
                _netbenefits_income(_USD_OPTION, value), _USD_OPTION, rule, value, id=f"usd-{value}"
            )
            for rule, values in _USD_RULES.items()
            for value in values
        ),
    ],
)
def test_option_refusal_names_the_option_and_rule_but_not_the_value(
    capsys: pytest.CaptureFixture[str], argv: list[str], option: str, rule: str, value: str
) -> None:
    with pytest.raises(SystemExit) as raised:
        main(argv)

    assert raised.value.code == 2
    err = capsys.readouterr().err
    assert err.splitlines()[-1] == f"ibkr-fa {argv[0]}: error: argument {option}: {rule}"
    for part in (value, *re.findall(r"\d{4,}", value)):
        assert part.lower() not in err.lower(), part


@pytest.mark.parametrize(("spec", "start"), [("2025-26", 2025), ("2025-2026", 2025)])
def test_valid_financial_years_still_parse(spec: str, start: int) -> None:
    assert cli._parse_financial_year(spec).start_year == start


def test_valid_usd_amount_still_parses() -> None:
    assert str(cli._parse_usd_2dp("1234.56")) == "1234.56"
