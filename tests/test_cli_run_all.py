"""Tests for the ``run-all`` one-shot orchestrator subcommand.

The per-schedule steps are monkeypatched with fakes (returning a chosen
exit code and a stand-in run report, and optionally writing their output
file), so these tests exercise the orchestration — input routing,
CY-from-FY derivation, target-file pre-deletion, status classification,
the status-first summary, and the worst-of exit-code aggregation —
without running real pipelines.
"""

from __future__ import annotations

import argparse
import errno
import os
import re
import sys
from pathlib import Path
from types import MappingProxyType, SimpleNamespace
from typing import Any, cast
from unittest.mock import Mock

import pytest

from ibkr_fa import cli, output
from ibkr_fa.cli import main
from ibkr_fa.compute.form67 import Form67ComputeResult
from ibkr_fa.compute.schedule_fsi import ScheduleFsiComputeResult
from ibkr_fa.exceptions import ConfigError
from ibkr_fa.pipeline import FinancialYear
from ibkr_fa.pipeline_fsi_compute import _compute_schedule_fsi_for_pipeline
from tests._config_toml import NON_UTF8_TOML
from tests._crash_reports import crash_pattern
from tests._ftc_warnings import MISSING_INDIAN_TAX_WARNING
from tests._oserror_markers import assert_no_oserror_markers as _assert_no_markers
from tests._oserror_markers import marked_oserror as _marked_oserror
from tests._run_all_fakes import RUN_ALL_FY_STEPS as _FY_STEPS
from tests._run_all_fakes import RUN_ALL_SCHEDULE_NAMES as _SCHEDULE_NAMES
from tests._run_all_fakes import RUN_ALL_STEPS as _ALL_STEPS
from tests._run_all_fakes import fake_run_all_argv as _args
from tests._run_all_fakes import fake_step as _fake_step
from tests._run_all_fakes import fake_step_report as _report
from tests._run_all_fakes import patch_all_steps as _patch_all
from tests._run_all_fakes import write_promised_outputs as _write_outputs


def _crash(args: argparse.Namespace, *, include_global_advisories: bool) -> tuple[int, None]:
    raise RuntimeError("kaboom")


def _record_steps(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Patch every step with a clean :func:`_fake_step` that also records the
    schedule it ran."""
    ran: list[str] = []
    step = _fake_step(0)

    def record(args: argparse.Namespace, *, include_global_advisories: bool) -> tuple[int, Mock]:
        ran.append(Path(args.output).stem)
        return step(args, include_global_advisories=include_global_advisories)

    for name in _ALL_STEPS:
        monkeypatch.setattr(cli, name, record)
    return ran


_FA_TARGETS = ("schedule-fa.csv", "schedule-fa.xlsx", "schedule-fa-a2.csv", "schedule-fa-a2.xlsx")


def _write_stale_fa_targets(out: Path) -> None:
    """An earlier run's Schedule FA output, in both formats."""
    out.mkdir(parents=True, exist_ok=True)
    for name in _FA_TARGETS:
        (out / name).write_text("STALE SCHEDULE FA", encoding="utf-8")


@pytest.fixture(autouse=True)
def _ready_form67_preflight(monkeypatch: pytest.MonkeyPatch) -> None:
    """The steps here are fakes, so the #601 preflight's FSI build sees a READY
    (empty) Form 67 result and an empty FSI result instead of parsing the
    placeholder input."""
    monkeypatch.setattr(
        cli,
        "_compute_schedule_fsi_for_pipeline",
        lambda **_kwargs: SimpleNamespace(
            form67_result=Form67ComputeResult(entries=()),
            fsi_result=ScheduleFsiComputeResult(rows=[]),
        ),
    )


def _summary_line(status: str, name: str, location: str = "(no file)") -> str:
    """One schedule's status line in the run-all summary."""
    return f"  {cli._RUN_ALL_STATUS_LABEL[status]}  {name:16}  {location}"


@pytest.fixture
def paths(tmp_path: Path) -> tuple[Path, Path, Path, Path]:
    fy = tmp_path / "fy.csv"
    fy.write_text("fy", encoding="utf-8")
    cy = tmp_path / "cy.csv"
    cy.write_text("cy", encoding="utf-8")
    cfg = tmp_path / "user.toml"
    cfg.write_text("cfg", encoding="utf-8")
    out = tmp_path / "out"
    return fy, cy, cfg, out


# ---- happy path -----------------------------------------------------------


def test_run_all_all_clean_exits_zero_and_writes_every_schedule(
    monkeypatch: pytest.MonkeyPatch,
    paths: tuple[Path, Path, Path, Path],
    capsys: pytest.CaptureFixture[str],
) -> None:
    _patch_all(monkeypatch, 0)
    rc = main(_args(paths))
    assert rc == 0
    _, _, _, out = paths
    produced = sorted(p.name for p in out.glob("*.csv"))
    assert produced == [
        "form-67.csv",
        "schedule-cg-ltcg.csv",
        "schedule-cg.csv",
        "schedule-fa-a2.csv",
        "schedule-fa.csv",
        "schedule-fsi.csv",
        "schedule-os.csv",
        "schedule-tr.csv",
    ]
    err = capsys.readouterr().err
    assert "STATUS: OK — every requested schedule was generated." in err


def test_run_all_derives_calendar_year_and_routes_inputs(
    monkeypatch: pytest.MonkeyPatch,
    paths: tuple[Path, Path, Path, Path],
) -> None:
    """FA gets the CY input + calendar_year = FY start year; the FY
    schedules get the FY input + the FinancialYear."""
    seen: dict[str, argparse.Namespace] = {}

    def _recorder(name: str):
        def step(args: argparse.Namespace, *, include_global_advisories: bool) -> tuple[int, Mock]:
            seen[name] = args
            _write_outputs(args)
            return 0, _report(Path(args.output).stem)

        return step

    for name in _ALL_STEPS:
        monkeypatch.setattr(cli, name, _recorder(name))

    fy, cy, _, _ = paths
    rc = main(_args(paths))
    assert rc == 0
    # FA: CY input, derived calendar_year 2025.
    assert seen["_schedule_fa_step"].input == cy
    assert seen["_schedule_fa_step"].calendar_year == 2025
    # An FY schedule: FY input, FinancialYear with start_year 2025.
    assert seen["_schedule_os_step"].input == fy
    assert seen["_schedule_os_step"].financial_year.start_year == 2025
    assert seen["_schedule_os_step"].format_id == "review-csv"
    assert seen["_form67_step"].format_id == "portal-csv"
    assert seen["_form67_step"].debug_form67 is False


def test_run_all_formats_follow_the_declaration(
    monkeypatch: pytest.MonkeyPatch,
    paths: tuple[Path, Path, Path, Path],
) -> None:
    """run-all writes each schedule in the format the declaration's run-all
    entry gives it: without the Form 67 substitute, review-csv stays (#1206)."""
    seen: dict[str, argparse.Namespace] = {}

    def form67(args: argparse.Namespace, *, include_global_advisories: bool) -> tuple[int, Mock]:
        seen["form67"] = args
        _write_outputs(args)
        return 0, _report("form-67")

    run_all = output._COMMAND_FORMATS["run-all"]
    unsubstituted = run_all._replace(substitutes=MappingProxyType({}))
    monkeypatch.setitem(cast(Any, output._COMMAND_FORMATS), "run-all", unsubstituted)
    _patch_all(monkeypatch)
    monkeypatch.setattr(cli, "_form67_step", form67)
    assert main(_args(paths)) == 0
    assert seen["form67"].format_id == "review-csv"
    assert seen["form67"].output.name == "form-67.csv"


def test_run_all_forwards_debug_form67(
    monkeypatch: pytest.MonkeyPatch,
    paths: tuple[Path, Path, Path, Path],
) -> None:
    seen: dict[str, argparse.Namespace] = {}

    def form67(args: argparse.Namespace, *, include_global_advisories: bool) -> tuple[int, Mock]:
        seen["form67"] = args
        _write_outputs(args)
        return 0, _report("form-67")

    _patch_all(monkeypatch)
    monkeypatch.setattr(cli, "_form67_step", form67)
    assert main(_args(paths, "--debug-form67")) == 0
    assert seen["form67"].format_id == "portal-csv"
    assert seen["form67"].debug_form67 is True


# ---- FA optional / skip ---------------------------------------------------


def test_run_all_skips_fa_without_cy_input(
    monkeypatch: pytest.MonkeyPatch,
    paths: tuple[Path, Path, Path, Path],
    capsys: pytest.CaptureFixture[str],
) -> None:
    _patch_all(monkeypatch, 0)
    rc = main(_args(paths, with_cy=False))
    assert rc == 0
    _, _, _, out = paths
    assert not (out / "schedule-fa.csv").exists()
    assert len(list(out.glob("*.csv"))) == 6
    err = capsys.readouterr().err
    assert "SKIPPED" in err
    assert "Schedule FA was NOT generated" in err


@pytest.mark.parametrize("stale", ["none", "files", "dangling-symlink", "price-cache-symlink"])
def test_run_all_without_cy_input_removes_stale_fa_outputs(
    monkeypatch: pytest.MonkeyPatch,
    paths: tuple[Path, Path, Path, Path],
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    stale: str,
) -> None:
    """Skipping Schedule FA still clears its targets (#738), so an earlier
    run's Schedule FA can't sit beside this run's files, and the summary says
    so. The price cache is not a target: a --price-cache-dir symlinked at one
    loses only the symlink."""
    _patch_all(monkeypatch)
    _, _, _, out = paths
    if stale == "files":
        _write_stale_fa_targets(out)
    cache = tmp_path / "cache" if stale == "price-cache-symlink" else out / ".ibkr-fa-price-cache"
    cached_price = cache / "price.csv"
    cached_price.parent.mkdir(parents=True)
    cached_price.write_bytes(b"cached price")
    out.mkdir(exist_ok=True)
    extra: tuple[str, ...] = ()
    if stale == "dangling-symlink":
        (out / "schedule-fa.csv").symlink_to(tmp_path / "missing.csv")
    elif stale == "price-cache-symlink":
        (out / "schedule-fa.csv").symlink_to(cache, target_is_directory=True)
        extra = ("--price-cache-dir", str(out / "schedule-fa.csv"))
    assert main(_args(paths, *extra, with_cy=False)) == 0
    assert not any(os.path.lexists(out / name) for name in _FA_TARGETS)
    assert cached_price.read_bytes() == b"cached price"
    lines = capsys.readouterr().err.splitlines()
    assert (
        "STATUS: OK — the six financial-year schedules were generated "
        "(Schedule FA skipped — see note)." in lines
    )
    detail = "not generated: --cy-input not provided (Schedule FA needs a calendar-year statement)"
    if stale != "none":
        detail += "; an earlier Schedule FA output was removed"
    assert lines[lines.index(_summary_line("skipped", "schedule-fa")) + 1].strip() == f"-> {detail}"


@pytest.mark.parametrize(
    ("failing", "expected"),
    [(_fake_step(4, write=False), 4), (_crash, 1)],
    ids=["domain-error", "crash"],
)
def test_run_all_without_cy_input_failure_removes_stale_fa_outputs(
    monkeypatch: pytest.MonkeyPatch,
    paths: tuple[Path, Path, Path, Path],
    capsys: pytest.CaptureFixture[str],
    failing: object,
    expected: int,
) -> None:
    _patch_all(monkeypatch)
    monkeypatch.setattr(cli, "_schedule_cg_step", failing)
    _, _, _, out = paths
    _write_stale_fa_targets(out)
    assert main(_args(paths, with_cy=False)) == expected
    assert list(out.iterdir()) == []
    err = capsys.readouterr().err
    assert "Every output of this run was removed." in err
    assert _summary_line("skipped", "schedule-fa") in err.splitlines()


def test_run_all_without_cy_input_unclearable_fa_target_fails_the_run(
    monkeypatch: pytest.MonkeyPatch,
    paths: tuple[Path, Path, Path, Path],
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A skipped Schedule FA's target that can't be cleared fails its leg, as a
    failed clearing does for every other schedule, so the run removes every
    other output."""
    _patch_all(monkeypatch)
    _, _, _, out = paths
    _write_stale_fa_targets(out)
    blocked = out / "schedule-fa.csv"
    blocked.unlink()
    blocked.mkdir()
    (blocked / "kept.txt").write_text("kept", encoding="utf-8")
    assert main(_args(paths, with_cy=False)) == 4
    assert [path.name for path in out.iterdir()] == [blocked.name]
    assert (blocked / "kept.txt").read_text(encoding="utf-8") == "kept"
    err = capsys.readouterr().err
    assert f"could not clear the stale output target {blocked} (" in err
    assert "STATUS: FAILED" in err
    lines = err.splitlines()
    for name in _SCHEDULE_NAMES:
        assert _summary_line("error" if name == "schedule-fa" else "ok", name) in lines


# ---- exit-code aggregation ------------------------------------------------


def test_run_all_manual_fill_aggregates_to_three_and_keeps_every_output(
    monkeypatch: pytest.MonkeyPatch,
    paths: tuple[Path, Path, Path, Path],
    capsys: pytest.CaptureFixture[str],
) -> None:
    for name in _FY_STEPS:
        monkeypatch.setattr(cli, name, _fake_step(0))
    monkeypatch.setattr(cli, "_schedule_fa_step", _fake_step(3))
    rc = main(_args(paths))
    assert rc == 3
    _, _, _, out = paths
    assert len(list(out.glob("*.csv"))) == 8
    err = capsys.readouterr().err
    assert "STATUS: ACTION NEEDED" in err
    assert _summary_line("manual_fill", "schedule-fa", str(out / "schedule-fa.csv")) in err


def test_run_all_fa_without_a2_by_design_keeps_every_output(
    monkeypatch: pytest.MonkeyPatch,
    paths: tuple[Path, Path, Path, Path],
) -> None:
    """With [account.custodian] unset, Schedule FA writes only Table A3, reports
    no A2 rows and exits 3. A missing A2 is then no failure."""

    def fa_without_a2(
        args: argparse.Namespace, *, include_global_advisories: bool
    ) -> tuple[int, Mock]:
        Path(args.output).write_text("a3", encoding="utf-8")
        return 3, _report("schedule-fa", a2_rows=())

    for name in _FY_STEPS:
        monkeypatch.setattr(cli, name, _fake_step(0))
    monkeypatch.setattr(cli, "_schedule_fa_step", fa_without_a2)
    assert main(_args(paths)) == 3
    _, _, _, out = paths
    assert (out / "schedule-fa.csv").exists()
    assert not (out / "schedule-fa-a2.csv").exists()
    assert len(list(out.glob("*.csv"))) == 7


@pytest.mark.parametrize("failing", ["schedule-os", "schedule-fa"])
def test_run_all_domain_error_aggregates_to_four_and_removes_every_output(
    monkeypatch: pytest.MonkeyPatch,
    paths: tuple[Path, Path, Path, Path],
    capsys: pytest.CaptureFixture[str],
    failing: str,
) -> None:
    """One refused schedule fails the run: every schedule still runs and
    reports, but the run leaves no output to file. The schedule-fa case is
    the #640 refusal of a closed lot whose holding date a wash sale shifted."""
    _patch_all(monkeypatch, 0)
    step = dict(zip(_SCHEDULE_NAMES, _ALL_STEPS, strict=True))[failing]
    monkeypatch.setattr(cli, step, _fake_step(4, write=False))
    _, _, cfg, out = paths
    cache = cfg.parent / "cache"
    cache.mkdir()
    cached_price = cache / "price.csv"
    cached_price.write_bytes(b"cached price")
    rc = main(_args(paths, "--price-cache-dir", str(cache)))
    assert rc == 4
    assert list(out.iterdir()) == []
    assert cached_price.read_bytes() == b"cached price"
    err = capsys.readouterr().err
    assert "STATUS: FAILED" in err
    assert "Every output of this run was removed." in err
    lines = err.splitlines()
    for name in _SCHEDULE_NAMES:
        assert _summary_line("error" if name == failing else "ok", name) in lines
    assert err.count("-> output removed because the run failed") == 6


def test_run_all_unexpected_crash_aggregates_to_one_and_removes_every_output(
    monkeypatch: pytest.MonkeyPatch,
    paths: tuple[Path, Path, Path, Path],
    capsys: pytest.CaptureFixture[str],
) -> None:
    _patch_all(monkeypatch, 0)
    monkeypatch.setattr(cli, "_schedule_fsi_step", _crash)
    rc = main(_args(paths))
    assert rc == 1  # internal error outranks domain/manual-fill
    _, _, _, out = paths
    assert list(out.iterdir()) == []
    err = capsys.readouterr().err
    # The class and source line, never the message (#1049).
    assert re.search(
        rf"-> internal error: crashed unexpectedly \({crash_pattern(RuntimeError)}\); "
        "no output written\n",
        err,
    )
    assert "kaboom" not in err
    assert "STATUS: FAILED" in err
    lines = err.splitlines()
    for name in _SCHEDULE_NAMES:
        assert _summary_line("error" if name == "schedule-fsi" else "ok", name) in lines


@pytest.mark.parametrize("code", [0, 3])
def test_run_all_success_without_its_output_fails_the_run(
    monkeypatch: pytest.MonkeyPatch,
    paths: tuple[Path, Path, Path, Path],
    capsys: pytest.CaptureFixture[str],
    code: int,
) -> None:
    _patch_all(monkeypatch, 0)
    monkeypatch.setattr(cli, "_schedule_cg_step", _fake_step(code, write=False))
    assert main(_args(paths)) == 4
    _, _, _, out = paths
    assert list(out.iterdir()) == []
    err = capsys.readouterr().err
    assert _summary_line("error", "schedule-cg") in err.splitlines()
    assert f"exit {code} without its promised output" in err
    assert str(out / "schedule-cg.csv") in err


@pytest.mark.parametrize(
    ("step", "missing", "extra"),
    [
        ("_schedule_fa_step", "schedule-fa-a2.csv", ()),
        ("_form67_step", "form-67-debug.csv", ("--debug-form67",)),
    ],
)
def test_run_all_leg_missing_one_of_its_promised_outputs_fails_the_run(
    monkeypatch: pytest.MonkeyPatch,
    paths: tuple[Path, Path, Path, Path],
    capsys: pytest.CaptureFixture[str],
    step: str,
    missing: str,
    extra: tuple[str, ...],
) -> None:
    """Schedule FA promises Table A2 unless it reports A2 skipped by design;
    Form 67 promises its debug CSV under --debug-form67."""

    def main_output_only(
        args: argparse.Namespace, *, include_global_advisories: bool
    ) -> tuple[int, Mock]:
        Path(args.output).write_text("placeholder", encoding="utf-8")
        return 0, _report(Path(args.output).stem)

    _patch_all(monkeypatch, 0)
    monkeypatch.setattr(cli, step, main_output_only)
    assert main(_args(paths, *extra)) == 4
    _, _, _, out = paths
    assert list(out.iterdir()) == []
    assert str(out / missing) in capsys.readouterr().err


@pytest.mark.parametrize("problem", ["not-regular", "cannot-stat"])
def test_run_all_refuses_uncheckable_or_nonregular_promised_output(
    monkeypatch: pytest.MonkeyPatch,
    paths: tuple[Path, Path, Path, Path],
    capsys: pytest.CaptureFixture[str],
    problem: str,
) -> None:
    _patch_all(monkeypatch)
    _, _, _, out = paths
    a2 = out / "schedule-fa-a2.csv"
    original_stat = Path.stat

    def cannot_stat(path: Path, *, follow_symlinks: bool = True):
        if path == a2:
            raise PermissionError(13, "Permission denied", str(path))
        return original_stat(path, follow_symlinks=follow_symlinks)

    def fa(args: argparse.Namespace, *, include_global_advisories: bool) -> tuple[int, Mock]:
        _write_outputs(args)
        if problem == "not-regular":
            a2.unlink()
            a2.mkdir()
        else:
            monkeypatch.setattr(Path, "stat", cannot_stat)
        return 0, _report("schedule-fa")

    monkeypatch.setattr(cli, "_schedule_fa_step", fa)
    assert main(_args(paths)) == 4
    assert list(out.iterdir()) == ([a2] if problem == "not-regular" else [])
    err = capsys.readouterr().err
    expected = (
        f"{a2}: not a regular file"
        if problem == "not-regular"
        else f"could not check {a2} (EACCES)"
    )
    assert f"without its promised output: {expected}" in err
    assert "Permission denied" not in err
    for name in _SCHEDULE_NAMES:
        assert _summary_line("error" if name == "schedule-fa" else "ok", name) in err.splitlines()


@pytest.mark.parametrize(
    ("failing", "expected"),
    [(_fake_step(4, write=False), 4), (_crash, 1)],
    ids=["domain-error", "crash"],
)
def test_run_all_failed_removal_names_the_file_and_removes_the_rest(
    monkeypatch: pytest.MonkeyPatch,
    paths: tuple[Path, Path, Path, Path],
    capsys: pytest.CaptureFixture[str],
    failing: object,
    expected: int,
) -> None:
    """The fault hits only the removal after the last schedule, not the
    clearing before a schedule runs."""
    _patch_all(monkeypatch, 0)
    monkeypatch.setattr(cli, "_schedule_os_step", failing)
    finished: list[bool] = []
    fa = _fake_step(0)

    def last_leg(
        args: argparse.Namespace, *, include_global_advisories: bool
    ) -> tuple[int, Mock | None]:
        finished.append(True)
        return fa(args, include_global_advisories=include_global_advisories)

    monkeypatch.setattr(cli, "_schedule_fa_step", last_leg)
    _, _, _, out = paths
    stuck = out / "schedule-cg.csv"
    unlink = Path.unlink

    def unlink_fails_after_the_run(path: Path, missing_ok: bool = False) -> None:
        if finished and path == stuck:
            raise _marked_oserror()
        unlink(path, missing_ok=missing_ok)

    monkeypatch.setattr(Path, "unlink", unlink_fails_after_the_run)
    assert main(_args(paths)) == expected
    assert [path.name for path in out.iterdir()] == [stuck.name]
    err = capsys.readouterr().err
    assert (
        f"error: the run failed, and its output may still be there: could not remove {stuck} "
        "(EACCES)" in err.splitlines()
    )
    _assert_no_markers(err)
    assert "could not all be removed" in err
    assert _summary_line("ok", "schedule-cg") in err.splitlines()
    assert "-> output removed because the run failed" not in err


def test_run_all_internal_error_outranks_manual_fill(
    monkeypatch: pytest.MonkeyPatch,
    paths: tuple[Path, Path, Path, Path],
) -> None:
    def _boom(args: argparse.Namespace, *, include_global_advisories: bool) -> tuple[int, None]:
        raise ValueError("x")

    monkeypatch.setattr(cli, "_schedule_os_step", _boom)
    for name in (
        "_schedule_cg_step",
        "_schedule_cg_ltcg_step",
        "_schedule_fsi_step",
        "_schedule_tr_step",
        "_form67_step",
    ):
        monkeypatch.setattr(cli, name, _fake_step(0))
    monkeypatch.setattr(cli, "_schedule_fa_step", _fake_step(3))  # manual-fill present too
    rc = main(_args(paths))
    assert rc == 1


# ---- stale-file pre-deletion ----------------------------------------------


def test_run_all_deletes_stale_output_on_failure(
    monkeypatch: pytest.MonkeyPatch,
    paths: tuple[Path, Path, Path, Path],
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A stale file from a prior run must not survive a failing schedule
    (it would otherwise look like fresh, file-ready output)."""
    _, _, _, out = paths
    out.mkdir(parents=True)
    stale = out / "schedule-os.csv"
    stale.write_text("STALE DATA FROM A PRIOR RUN", encoding="utf-8")
    monkeypatch.setattr(cli, "_schedule_os_step", _fake_step(4, write=False))
    for name in (
        "_schedule_cg_step",
        "_schedule_cg_ltcg_step",
        "_schedule_fsi_step",
        "_schedule_tr_step",
        "_form67_step",
        "_schedule_fa_step",
    ):
        monkeypatch.setattr(cli, name, _fake_step(0))
    rc = main(_args(paths))
    assert rc == 4
    assert not stale.exists()  # stale file removed, not left behind
    err = capsys.readouterr().err
    assert "ignore any existing schedule-os.csv" in err


def test_run_all_clears_stale_form67_debug_file(
    monkeypatch: pytest.MonkeyPatch,
    paths: tuple[Path, Path, Path, Path],
) -> None:
    _, _, _, out = paths
    out.mkdir(parents=True)
    debug = out / "form-67-debug.csv"
    debug.write_text("STALE", encoding="utf-8")
    _patch_all(monkeypatch)
    assert main(_args(paths)) == 0
    assert not debug.exists()


def test_run_all_clears_stale_fa_a2_when_fa_refuses(
    monkeypatch: pytest.MonkeyPatch,
    paths: tuple[Path, Path, Path, Path],
) -> None:
    """Schedule FA writes Table A2 to a ``-a2`` sibling; a refused FA leg must
    not leave the prior run's A2 (a stale account disclosure) behind."""
    _, _, _, out = paths
    out.mkdir(parents=True)
    stale_a2 = out / "schedule-fa-a2.csv"
    stale_a2.write_text("STALE A2", encoding="utf-8")
    _patch_all(monkeypatch)
    monkeypatch.setattr(cli, "_schedule_fa_step", _fake_step(4, write=False))
    assert main(_args(paths)) == 4
    assert not stale_a2.exists()


def test_run_all_clears_off_extension_fa_a2(
    monkeypatch: pytest.MonkeyPatch,
    paths: tuple[Path, Path, Path, Path],
) -> None:
    _, _, _, out = paths
    out.mkdir(parents=True)
    stale_a2 = out / "schedule-fa-a2.csv"
    stale_a2.write_text("STALE A2 FROM A PRIOR CSV RUN", encoding="utf-8")
    _patch_all(monkeypatch)
    assert main(_args(paths, "--format", "xlsx")) == 0
    assert not stale_a2.exists()


@pytest.mark.parametrize(
    ("flag", "target_name", "with_cy"),
    [
        ("--fy-input", "schedule-os.csv", True),
        ("--fy-input", "form-67-debug.csv", True),
        ("--cy-input", "schedule-fa-a2.csv", True),
        ("--config", "schedule-tr.xlsx", True),
        # Schedule FA's targets are cleared even when it is skipped (#738).
        ("--fy-input", "schedule-fa.csv", False),
        ("--config", "schedule-fa-a2.xlsx", False),
    ],
)
def test_run_all_refuses_input_that_is_an_output_target(
    monkeypatch: pytest.MonkeyPatch,
    paths: tuple[Path, Path, Path, Path],
    capsys: pytest.CaptureFixture[str],
    flag: str,
    target_name: str,
    with_cy: bool,
) -> None:
    """run-all deletes its targets before each schedule; an input that IS a
    target would be destroyed before it is read. Refuse up front, delete
    nothing, run nothing."""
    ran = _record_steps(monkeypatch)
    fy, cy, cfg, out = paths
    out.mkdir(parents=True)
    clashing = out / target_name
    clashing.write_text("THE USER'S INPUT", encoding="utf-8")
    bystander = out / "schedule-cg.csv"
    bystander.write_text("PRIOR OUTPUT", encoding="utf-8")
    argv = _args((fy, cy, cfg, out), with_cy=with_cy)
    # Reach the target through a relative ``..`` path, not the literal one.
    argv[argv.index(flag) + 1] = str(out / ".." / out.name / target_name)
    rc = main(argv)
    assert rc == 4
    assert clashing.read_text(encoding="utf-8") == "THE USER'S INPUT"
    assert bystander.read_text(encoding="utf-8") == "PRIOR OUTPUT"
    assert ran == []
    err = capsys.readouterr().err
    assert flag in err
    assert target_name in err


@pytest.mark.parametrize(
    ("target_name", "with_cy"), [("schedule-os.csv", True), ("schedule-fa.xlsx", False)]
)
def test_run_all_refuses_input_symlinked_to_an_output_target(
    monkeypatch: pytest.MonkeyPatch,
    paths: tuple[Path, Path, Path, Path],
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    target_name: str,
    with_cy: bool,
) -> None:
    """An input reached through a symlink is protected too: deleting the
    target would delete the file the symlink points at."""
    ran = _record_steps(monkeypatch)
    _, cy, cfg, out = paths
    out.mkdir(parents=True)
    real = out / target_name
    real.write_text("THE USER'S STATEMENT", encoding="utf-8")
    link = tmp_path / "statement.csv"
    link.symlink_to(real)
    assert main(_args((link, cy, cfg, out), with_cy=with_cy)) == 4
    assert ran == []
    assert real.read_text(encoding="utf-8") == "THE USER'S STATEMENT"
    assert [path.name for path in out.iterdir()] == [target_name]
    err = capsys.readouterr().err
    assert f"--fy-input {link}" in err
    assert str(real) in err


@pytest.mark.parametrize(
    ("target_name", "with_cy"), [("schedule-os.csv", True), ("schedule-fa.csv", False)]
)
@pytest.mark.parametrize("operation", ["target-stat", "input-stat", "samefile"])
def test_run_all_refuses_when_collision_check_cannot_inspect_files(
    monkeypatch: pytest.MonkeyPatch,
    paths: tuple[Path, Path, Path, Path],
    capsys: pytest.CaptureFixture[str],
    operation: str,
    target_name: str,
    with_cy: bool,
) -> None:
    ran = _record_steps(monkeypatch)
    fy, _, _, out = paths
    out.mkdir(parents=True)
    target = out / target_name
    target.write_bytes(b"PRIOR OUTPUT")
    method = "samefile" if operation == "samefile" else "stat"
    original = getattr(Path, method)
    inaccessible = fy if operation == "input-stat" else target

    def cannot_inspect(path: Path, *args: object):
        if path == inaccessible:
            raise _marked_oserror()
        return original(path, *args)

    def exists(path: Path) -> bool:
        # Python 3.14 suppresses all stat errors in exists().
        try:
            path.stat()
        except OSError:
            return False
        return True

    monkeypatch.setattr(Path, method, cannot_inspect)
    monkeypatch.setattr(Path, "exists", exists)
    assert main(_args(paths, with_cy=with_cy)) == 4
    assert ran == []
    assert fy.read_bytes() == b"fy"
    assert target.read_bytes() == b"PRIOR OUTPUT"
    err = capsys.readouterr().err
    assert err == (
        f"error: could not check output target {target} against --fy-input {fy} (EACCES)\n"
    )
    _assert_no_markers(err)


@pytest.mark.parametrize(
    "reach", ["direct", "dot-dot", "aliased-dir", "hard-link", "chain", "directory"]
)
@pytest.mark.parametrize(
    ("flag", "target_name"),
    [
        ("--config", "schedule-os.csv"),
        ("--config", "schedule-fa.csv"),
        ("--fy-input", "schedule-fa-a2.xlsx"),
    ],
)
def test_run_all_refuses_input_reached_through_a_dangling_target(
    monkeypatch: pytest.MonkeyPatch,
    paths: tuple[Path, Path, Path, Path],
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    flag: str,
    target_name: str,
    reach: str,
) -> None:
    """Every target is cleared before anything runs, Schedule FA's even when
    it is skipped (#738), so an input reached through a dangling symlink at
    one would lose that symlink. ``samefile`` can't follow a missing referent,
    so the target's own entry is compared with every entry on the input's
    path."""
    ran = _record_steps(monkeypatch)
    preflight = Mock(wraps=cli._compute_schedule_fsi_for_pipeline)
    monkeypatch.setattr(cli, "_compute_schedule_fsi_for_pipeline", preflight)
    _, _, _, out = paths
    out.mkdir()
    entry = out / target_name
    entry.symlink_to(tmp_path / "missing")
    bystander = out / "schedule-cg.csv"
    bystander.write_text("PRIOR OUTPUT", encoding="utf-8")
    cached_price = out / ".ibkr-fa-price-cache" / "price.csv"
    cached_price.parent.mkdir()
    cached_price.write_bytes(b"cached price")
    given = entry
    if reach == "dot-dot":
        given = out / ".." / out.name / target_name
    elif reach == "aliased-dir":
        alias = tmp_path / "alias"
        alias.symlink_to(out, target_is_directory=True)
        given = alias / target_name
    elif reach == "hard-link":
        given = tmp_path / "hard-link"
        os.link(entry, given, follow_symlinks=False)
    elif reach == "chain":
        given = tmp_path / "input-link"
        given.symlink_to(entry)
    elif reach == "directory":
        given = entry / "user.toml"
    argv = _args(paths, with_cy=False)
    argv[argv.index(flag) + 1] = str(given)
    assert main(argv) == 4
    assert ran == []
    preflight.assert_not_called()
    assert entry.readlink() == tmp_path / "missing"
    assert bystander.read_text(encoding="utf-8") == "PRIOR OUTPUT"
    assert cached_price.read_bytes() == b"cached price"
    err = capsys.readouterr().err
    assert f"{flag} {given} is or goes through {entry}" in err


def test_run_all_without_cy_input_refuses_uninspectable_fa_target(
    monkeypatch: pytest.MonkeyPatch,
    paths: tuple[Path, Path, Path, Path],
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Only a missing Schedule FA target counts as absent: one whose lstat
    fails otherwise refuses before anything runs or is removed."""
    ran = _record_steps(monkeypatch)
    preflight = Mock(wraps=cli._compute_schedule_fsi_for_pipeline)
    monkeypatch.setattr(cli, "_compute_schedule_fsi_for_pipeline", preflight)
    _, _, _, out = paths
    out.mkdir()
    target = out / "schedule-fa.csv"
    target.write_bytes(b"PRIOR OUTPUT")
    lstat = os.lstat

    def cannot_inspect(path: Path | str, *, dir_fd: int | None = None) -> os.stat_result:
        if Path(path) == target:
            raise _marked_oserror()
        return lstat(path, dir_fd=dir_fd)

    monkeypatch.setattr(os, "lstat", cannot_inspect)
    assert main(_args(paths, with_cy=False)) == 4
    assert ran == []
    preflight.assert_not_called()
    assert target.read_bytes() == b"PRIOR OUTPUT"
    err = capsys.readouterr().err
    assert err == f"error: could not inspect output target {target} (EACCES)\n"
    _assert_no_markers(err)


def test_run_all_without_cy_input_refuses_input_in_a_symlink_loop(
    monkeypatch: pytest.MonkeyPatch,
    paths: tuple[Path, Path, Path, Path],
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """An input whose path can't be walked refuses before a skipped Schedule
    FA's dangling target, which ``samefile`` skips, is cleared: a symlink loop
    in its directory."""
    ran = _record_steps(monkeypatch)
    preflight = Mock(wraps=cli._compute_schedule_fsi_for_pipeline)
    monkeypatch.setattr(cli, "_compute_schedule_fsi_for_pipeline", preflight)
    _, _, _, out = paths
    out.mkdir()
    target = out / "schedule-fa.csv"
    target.symlink_to(tmp_path / "missing")
    loop = tmp_path / "loop"
    loop.symlink_to(loop)
    config = loop / "user.toml"
    argv = _args(paths, with_cy=False)
    argv[argv.index("--config") + 1] = str(config)
    assert main(argv) == 4
    assert ran == []
    preflight.assert_not_called()
    assert target.readlink() == tmp_path / "missing"
    err = capsys.readouterr().err
    # The errno name, not its strerror (#1048).
    assert err == f"error: could not inspect --config {config} (ELOOP)\n"
    assert os.strerror(errno.ELOOP).lower() not in err.lower()


@pytest.mark.parametrize(
    ("schedule", "target_name"),
    [
        ("schedule-os", "schedule-os.csv"),
        ("form-67", "form-67-debug.csv"),
        ("schedule-fa", "schedule-fa-a2.csv"),
        ("schedule-fa", "schedule-fa-a2.xlsx"),
    ],
)
def test_run_all_cleanup_failure_skips_handler_and_reports_failed_summary(
    monkeypatch: pytest.MonkeyPatch,
    paths: tuple[Path, Path, Path, Path],
    capsys: pytest.CaptureFixture[str],
    schedule: str,
    target_name: str,
) -> None:
    ran: list[str] = []

    def step(args: argparse.Namespace, *, include_global_advisories: bool) -> tuple[int, Mock]:
        ran.append(Path(args.output).stem)
        _write_outputs(args, "current")
        return 0, _report(Path(args.output).stem)

    for name in _ALL_STEPS:
        monkeypatch.setattr(cli, name, step)
    _, _, _, out = paths
    target = out / target_name
    target.mkdir(parents=True)
    assert main(_args(paths)) == 4
    assert schedule not in ran
    assert len(ran) == 6
    assert target.is_dir()
    assert [path.name for path in out.iterdir()] == [target_name]
    err = capsys.readouterr().err
    assert "RUN-ALL SUMMARY" in err
    assert "STATUS: FAILED" in err
    assert f"{schedule:16}  (no file)" in err
    assert "could not clear" in err
    assert target_name in err


# ---- preflight + options --------------------------------------------------


@pytest.mark.parametrize("with_cy", [True, False])
@pytest.mark.parametrize(
    ("error", "expected"),
    [(ConfigError(field="form67", message="readiness unavailable"), 4), (RuntimeError("boom"), 1)],
    ids=["domain-error", "crash"],
)
def test_run_all_readiness_failure_stops_before_any_step_and_clears_every_target(
    monkeypatch: pytest.MonkeyPatch,
    paths: tuple[Path, Path, Path, Path],
    capsys: pytest.CaptureFixture[str],
    error: Exception,
    expected: int,
    with_cy: bool,
) -> None:
    _patch_all(monkeypatch)
    preflight = Mock(side_effect=error)
    invoke = Mock(wraps=cli._invoke_run_all_handler)
    monkeypatch.setattr(cli, "_compute_schedule_fsi_for_pipeline", preflight)
    monkeypatch.setattr(cli, "_invoke_run_all_handler", invoke)
    _, _, _, out = paths
    out.mkdir()
    for name in (*_SCHEDULE_NAMES, "schedule-fa-a2"):
        for extension in ("csv", "xlsx"):
            (out / f"{name}.{extension}").write_text("stale", encoding="utf-8")
    (out / "form-67-debug.csv").write_text("stale", encoding="utf-8")

    assert main(_args(paths, "--format", "xlsx", "--debug-form67", with_cy=with_cy)) == expected
    preflight.assert_called_once()
    invoke.assert_not_called()
    assert list(out.iterdir()) == []
    captured = capsys.readouterr()
    if expected == 1:
        # The class and source line, never the message (#1049).
        assert re.fullmatch(
            "error: run-all: the Form 67 readiness check crashed unexpectedly "
            rf"\({crash_pattern(RuntimeError)}\)\n",
            captured.err,
        )
    else:
        assert str(error) in captured.err
    assert captured.out == ""


def test_run_all_non_utf8_config_refuses_in_the_preflight_and_clears_every_target(
    monkeypatch: pytest.MonkeyPatch,
    paths: tuple[Path, Path, Path, Path],
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The real preflight loads the config first, so a user.toml that isn't
    UTF-8 is a refusal there, not a readiness crash (#934)."""
    _patch_all(monkeypatch)
    monkeypatch.setattr(
        cli, "_compute_schedule_fsi_for_pipeline", _compute_schedule_fsi_for_pipeline
    )
    invoke = Mock(wraps=cli._invoke_run_all_handler)
    monkeypatch.setattr(cli, "_invoke_run_all_handler", invoke)
    _, _, cfg, out = paths
    cfg.write_bytes(NON_UTF8_TOML)
    out.mkdir()
    for name in (*_SCHEDULE_NAMES, "schedule-fa-a2"):
        (out / f"{name}.csv").write_text("stale", encoding="utf-8")

    assert main(_args(paths)) == 4
    invoke.assert_not_called()
    assert list(out.iterdir()) == []
    captured = capsys.readouterr()
    assert "error: run-all: ConfigError at field '<file>': must be UTF-8 text" in captured.err
    assert "crashed unexpectedly" not in captured.err
    assert captured.out == ""


def test_run_all_creates_missing_output_dir(
    monkeypatch: pytest.MonkeyPatch,
    paths: tuple[Path, Path, Path, Path],
) -> None:
    _patch_all(monkeypatch, 0)
    _, _, _, out = paths
    nested = out / "deep" / "nested"
    fy, cy, cfg, _ = paths
    rc = main(
        [
            "run-all",
            "--fy-input",
            str(fy),
            "--cy-input",
            str(cy),
            "--financial-year",
            "2025-26",
            "--itr-form",
            "ITR-2",
            "--config",
            str(cfg),
            "--output-dir",
            str(nested),
        ]
    )
    assert rc == 0
    assert nested.is_dir()
    assert (nested / "schedule-os.csv").exists()


def test_run_all_xlsx_format_uses_xlsx_extension(
    monkeypatch: pytest.MonkeyPatch,
    paths: tuple[Path, Path, Path, Path],
) -> None:
    _patch_all(monkeypatch, 0)
    rc = main(_args(paths, "--format", "xlsx"))
    assert rc == 0
    _, _, _, out = paths
    assert (out / "schedule-os.xlsx").exists()
    assert (out / "schedule-fa.xlsx").exists()
    assert not list(out.glob("*.csv"))


def test_run_all_default_price_cache_under_output_dir(
    monkeypatch: pytest.MonkeyPatch,
    paths: tuple[Path, Path, Path, Path],
) -> None:
    captured: dict[str, Path | None] = {}

    def _fa(args: argparse.Namespace, *, include_global_advisories: bool) -> tuple[int, Mock]:
        captured["price_cache_dir"] = args.price_cache_dir
        _write_outputs(args)
        return 0, _report("schedule-fa")

    for name in _FY_STEPS:
        monkeypatch.setattr(cli, name, _fake_step(0))
    monkeypatch.setattr(cli, "_schedule_fa_step", _fa)
    _, _, _, out = paths
    rc = main(_args(paths))
    assert rc == 0
    assert captured["price_cache_dir"] == out / ".ibkr-fa-price-cache"


# ---- helper units ---------------------------------------------------------


def test_run_all_clears_other_format_variant(
    monkeypatch: pytest.MonkeyPatch,
    paths: tuple[Path, Path, Path, Path],
) -> None:
    """A prior run's off-extension file must not survive a run with a
    different --format (else the dir shows two 'current' files for one
    schedule). Pre-running with review-csv then xlsx must leave only the
    xlsx variant."""
    _patch_all(monkeypatch, 0)
    _, _, _, out = paths
    out.mkdir(parents=True)
    stale_csv = out / "schedule-os.csv"
    stale_csv.write_text("STALE CSV FROM A PRIOR review-csv RUN", encoding="utf-8")
    rc = main(_args(paths, "--format", "xlsx"))
    assert rc == 0
    assert (out / "schedule-os.xlsx").exists()
    assert not stale_csv.exists()  # off-extension stale file cleared


def test_aggregate_exit_code_precedence() -> None:
    agg = cli._aggregate_run_all_exit_code
    assert agg([0, 0, 0]) == 0
    assert agg([0, 3, 0]) == 3
    assert agg([0, 4, 3]) == 4
    assert agg([0, 1, 4, 3]) == 1  # internal error wins
    assert agg([]) == 0


def test_build_sub_args_sets_full_attribute_superset() -> None:
    """The builder must set every attribute any handler reads, so run-all
    cannot AttributeError if a subcommand grows a new flag."""
    ns = cli._build_run_all_sub_args(
        input_path=Path("in.csv"),
        output_path=Path("out.csv"),
        config_path=Path("cfg.toml"),
        format_id="review-csv",
        itr_form="ITR-2",
        financial_year=FinancialYear(start_year=2025),
        calendar_year=2025,
        quiet=False,
        allow_masked_account=True,
        accept_anonymised_statement=False,
        suppress_cross_ay_warning=False,
        no_fetch_prices=False,
        price_cache_dir=None,
    )
    for attr in (
        "input",
        "config",
        "output",
        "format_id",
        "itr_form",
        "financial_year",
        "calendar_year",
        "quiet",
        "allow_masked_account",
        "accept_anonymised_statement",
        "suppress_cross_ay_warning",
        "no_fetch_prices",
        "price_cache_dir",
    ):
        assert hasattr(ns, attr), f"missing {attr}"


# ---- advisory dedup (one emission per run-all invocation) ------------------
#
# run-all reuses each subcommand's step, so the two ~500-word compliance
# walls (BMA + ITR-form) and the masked-account cross-check warning would
# otherwise print once per schedule (up to 7x). Under run-all each step
# leaves those global advisories out, and run-all prints each one the run
# reports call for ONCE before the summary. Single-subcommand runs are
# untouched (no capture), preserving the #178/#179 non-quietable contract.


def _advisory_step(code: int = 0, *, stream_name: str = "stderr", masked: bool = True):
    """Fake step printing a schedule-unique note, as its renderer does with
    ``include_global_advisories=False``, and reporting a masked account.

    Mirrors the real steps: the 6 FY schedules print their warnings block on
    stderr; schedule-fa prints its report on stdout. ``stream_name`` selects
    which to simulate.
    """

    def step(args: argparse.Namespace, *, include_global_advisories: bool) -> tuple[int, Mock]:
        # Resolve the stream at CALL time (like the real steps' bare
        # ``print``/``print(..., file=sys.stderr)``) so run-all's
        # redirect_stdout/redirect_stderr capture actually intercepts it.
        stream = sys.stderr if stream_name == "stderr" else sys.stdout
        _write_outputs(args)
        stream.write(f"note for {Path(args.output).stem}\n")
        return code, _report(Path(args.output).stem, account_id_cross_checked=not masked)

    return step


def _reports(fields_by_name: dict[str, dict[str, object]]) -> list[Mock]:
    """One stand-in report per schedule, with ``fields_by_name[name]`` set."""
    return [_report(name, **fields_by_name.get(name, {})) for name in _SCHEDULE_NAMES]


@pytest.mark.parametrize(
    ("fields_by_name", "expected"),
    [
        ({}, (cli.BMA_DISCLOSURE_ADVISORY, cli.ITR_FORM_SELECTION_ADVISORY)),
        (
            {"schedule-cg": {"account_id_cross_checked": False}},
            (
                cli._MASKED_ACCOUNT_SKIP_WARNING,
                cli.BMA_DISCLOSURE_ADVISORY,
                cli.ITR_FORM_SELECTION_ADVISORY,
            ),
        ),
        (
            {"schedule-tr": {"has_foreign_tax_without_taxpayer_id": True}},
            (
                cli._FOREIGN_TIN_MISSING_ADVISORY,
                cli.BMA_DISCLOSURE_ADVISORY,
                cli.ITR_FORM_SELECTION_ADVISORY,
            ),
        ),
        (
            {"form-67": {"has_unresolved_indian_tax_payable": True}},
            (
                MISSING_INDIAN_TAX_WARNING,
                cli.BMA_DISCLOSURE_ADVISORY,
                cli.ITR_FORM_SELECTION_ADVISORY,
            ),
        ),
        (
            # Only FSI / TR carry the TIN flag, and only FSI / TR / Form 67
            # the tax-payable flag: the same field elsewhere is ignored.
            {
                "form-67": {"has_foreign_tax_without_taxpayer_id": True},
                "schedule-os": {"has_unresolved_indian_tax_payable": True},
            },
            (cli.BMA_DISCLOSURE_ADVISORY, cli.ITR_FORM_SELECTION_ADVISORY),
        ),
        (
            {
                "schedule-fa": {"account_id_cross_checked": False},
                "schedule-fsi": {
                    "has_foreign_tax_without_taxpayer_id": True,
                    "has_unresolved_indian_tax_payable": True,
                },
            },
            (
                cli._MASKED_ACCOUNT_SKIP_WARNING,
                cli._FOREIGN_TIN_MISSING_ADVISORY,
                MISSING_INDIAN_TAX_WARNING,
                cli.BMA_DISCLOSURE_ADVISORY,
                cli.ITR_FORM_SELECTION_ADVISORY,
            ),
        ),
    ],
)
def test_run_all_global_advisories_come_from_report_fields(
    fields_by_name: dict[str, dict[str, object]], expected: tuple[str, ...]
) -> None:
    assert cli._run_all_global_advisories(_reports(fields_by_name)) == list(expected)


def test_run_all_global_advisories_need_a_report() -> None:
    assert cli._run_all_global_advisories([]) == []


def test_run_all_dedupes_global_advisories_to_once(
    monkeypatch: pytest.MonkeyPatch,
    paths: tuple[Path, Path, Path, Path],
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Each global advisory appears exactly once across a whole run-all, and
    every schedule-specific note is preserved. Covers BOTH capture paths:
    the FY schedules emit on stderr, schedule-fa on stdout."""
    for name in _FY_STEPS:
        monkeypatch.setattr(cli, name, _advisory_step(0, stream_name="stderr"))
    monkeypatch.setattr(cli, "_schedule_fa_step", _advisory_step(0, stream_name="stdout"))

    rc = main(_args(paths, "--allow-masked-account"))
    assert rc == 0

    captured = capsys.readouterr()
    combined = captured.out + captured.err
    assert combined.count(cli.BMA_DISCLOSURE_ADVISORY) == 1
    assert combined.count(cli.ITR_FORM_SELECTION_ADVISORY) == 1
    assert combined.count(cli._MASKED_ACCOUNT_SKIP_WARNING) == 1
    # All 7 schedule-specific notes survive (not deduped).
    for stem in _SCHEDULE_NAMES:
        assert f"note for {stem}" in combined


def test_run_all_omits_cross_check_warning_when_no_schedule_emits_it(
    monkeypatch: pytest.MonkeyPatch,
    paths: tuple[Path, Path, Path, Path],
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The conditional cross-check warning is printed only if a report calls
    for it; the always-on BMA/ITR walls still appear once."""
    for name in _ALL_STEPS:
        monkeypatch.setattr(cli, name, _advisory_step(0, masked=False))

    rc = main(_args(paths))
    assert rc == 0

    combined = "".join(capsys.readouterr())
    assert combined.count(cli._MASKED_ACCOUNT_SKIP_WARNING) == 0
    assert combined.count(cli.BMA_DISCLOSURE_ADVISORY) == 1
    assert combined.count(cli.ITR_FORM_SELECTION_ADVISORY) == 1


def test_run_all_replays_partial_output_when_handler_raises(
    monkeypatch: pytest.MonkeyPatch,
    paths: tuple[Path, Path, Path, Path],
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A step that prints then crashes still has its output replayed (don't
    lose diagnostics), and is classified as an error so run-all's exit code
    reflects it."""

    def _raiser(args: argparse.Namespace, *, include_global_advisories: bool) -> tuple[int, None]:
        sys.stderr.write("partial diagnostic before crash\n")
        raise RuntimeError("boom")

    monkeypatch.setattr(cli, "_schedule_os_step", _raiser)
    for name in _ALL_STEPS:
        if name != "_schedule_os_step":
            monkeypatch.setattr(cli, name, _advisory_step(0))

    rc = main(_args(paths, "--allow-masked-account"))
    assert rc != 0  # worst-of reflects the crashed schedule

    combined = "".join(capsys.readouterr())
    assert "partial diagnostic before crash" in combined  # replayed, not lost
    assert "internal error" in combined  # surfaced in the summary
    # Only run-all's own BMA prints, once.
    assert combined.count(cli.BMA_DISCLOSURE_ADVISORY) == 1


@pytest.mark.parametrize("code", [0, 3])
def test_run_all_success_without_a_report_is_an_internal_error(
    monkeypatch: pytest.MonkeyPatch,
    paths: tuple[Path, Path, Path, Path],
    capsys: pytest.CaptureFixture[str],
    code: int,
) -> None:
    """Without a report run-all cannot tell which advisories or checklist
    items apply, so it fails the run instead of leaving them out."""

    def no_report(args: argparse.Namespace, *, include_global_advisories: bool) -> tuple[int, None]:
        _write_outputs(args)
        return code, None

    _patch_all(monkeypatch, 0)
    monkeypatch.setattr(cli, "_schedule_tr_step", no_report)
    assert main(_args(paths)) == 1
    _, _, _, out = paths
    assert list(out.iterdir()) == []
    err = capsys.readouterr().err
    assert _summary_line("error", "schedule-tr") in err.splitlines()
    assert f"internal error: exit {code} without a run report" in err


# ---- "Before you file" pre-filing action-item checklist -------------------


@pytest.mark.parametrize(
    ("fields_by_name", "expected"),
    [
        ({}, set()),
        ({"schedule-fa": {"a2_rows": ()}}, {cli._PreFilingAction.FA_A2_SKIPPED}),
        (
            {"schedule-fsi": {"has_foreign_tax_without_taxpayer_id": True}},
            {cli._PreFilingAction.FOREIGN_TIN_MISSING},
        ),
        (
            {"schedule-tr": {"has_foreign_tax_without_taxpayer_id": True}},
            {cli._PreFilingAction.FOREIGN_TIN_MISSING},
        ),
        # Only FSI / TR carry the TIN flag, and only Schedule FA has A2 rows.
        (
            {"form-67": {"has_foreign_tax_without_taxpayer_id": True, "a2_rows": ()}},
            set(),
        ),
        (
            {
                "schedule-fa": {"a2_rows": ()},
                "schedule-fsi": {"has_foreign_tax_without_taxpayer_id": True},
            },
            set(cli._PreFilingAction),
        ),
    ],
)
def test_pre_filing_actions_come_from_report_fields(
    fields_by_name: dict[str, dict[str, object]], expected: set[cli._PreFilingAction]
) -> None:
    assert cli._pre_filing_actions(_reports(fields_by_name)) == expected


def _summary(
    outcomes: list[cli._RunAllOutcome], actions: set[cli._PreFilingAction] | None = None
) -> str:
    return cli._render_run_all_summary(
        outcomes,
        financial_year=FinancialYear(start_year=2025),
        calendar_year=2025,
        fa_price_cache_dir=None,
        pre_filing_actions=actions,
    )


def test_summary_renders_checklist_and_action_needed_status_when_actions_fired() -> None:
    """When action-items fired (but no manual-fill / error), the status must
    NOT say a bare OK, and the checklist must list the fired remedies in
    _PRE_FILING_ACTION_ITEMS priority order."""
    ok_outcomes = [cli._RunAllOutcome("schedule-os", "ok", Path("os.csv"), "")]
    remedies = [remedy for _action, remedy in cli._PRE_FILING_ACTION_ITEMS]
    rendered = _summary(ok_outcomes, set(cli._PreFilingAction))

    assert "STATUS: ACTION NEEDED" in rendered
    assert "STATUS: OK" not in rendered
    assert f"Before you file ({len(remedies)} item(s) to address):" in rendered
    # Order matches _PRE_FILING_ACTION_ITEMS (A2, TIN).
    positions = [rendered.index(r) for r in remedies]
    assert positions == sorted(positions)


def test_summary_omits_checklist_and_stays_ok_when_no_actions() -> None:
    ok_outcomes = [cli._RunAllOutcome("schedule-os", "ok", Path("os.csv"), "")]
    rendered = _summary(ok_outcomes, set())
    assert "STATUS: OK — every requested schedule was generated." in rendered
    assert "Before you file" not in rendered


def test_summary_manual_fill_only_status_does_not_reference_checklist() -> None:
    """When only manual-fill columns are outstanding (no config action-items
    fired — e.g. a fully-configured run whose sole remaining step is the
    Table A2 peak balance), the status must name the manual-fill columns and
    NOT reference a 'Before you file' checklist that did not render."""
    outcomes = [cli._RunAllOutcome("schedule-fa", "manual_fill", Path("fa.csv"), "")]
    rendered = _summary(outcomes, set())
    assert "STATUS: ACTION NEEDED" in rendered
    assert "manual-fill column(s) flagged below" in rendered
    assert "'Before you file' checklist" not in rendered
    assert "Before you file (" not in rendered


def test_summary_manual_fill_and_actions_status_references_both() -> None:
    outcomes = [cli._RunAllOutcome("schedule-fa", "manual_fill", Path("fa.csv"), "")]
    rendered = _summary(outcomes, set(cli._PreFilingAction))
    assert "STATUS: ACTION NEEDED" in rendered
    assert "manual-fill column(s) flagged below and the 'Before you file' checklist" in rendered
    assert "Before you file (" in rendered


def test_summary_failed_status_takes_precedence_over_actions() -> None:
    """A hard error must keep STATUS: FAILED even if action-items fired —
    the checklist is moot when output isn't safe to file at all."""
    outcomes = [cli._RunAllOutcome("schedule-os", "error", None, "boom")]
    rendered = _summary(outcomes, set(cli._PreFilingAction))
    assert "STATUS: FAILED" in rendered
    assert "STATUS: ACTION NEEDED" not in rendered
    # The checklist is moot when the output isn't safe to file at all — it
    # must NOT appear beneath the "do NOT file" banner.
    assert "Before you file" not in rendered


def test_run_all_surfaces_pre_filing_checklist_end_to_end(
    monkeypatch: pytest.MonkeyPatch,
    paths: tuple[Path, Path, Path, Path],
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Integration: a Schedule FA report without A2 rows drives the
    consolidated checklist + ACTION NEEDED status, while exit stays 0 (the
    advisory is an informational completeness nudge, not a manual-fill)."""

    def _fa_with_a2_skip(
        args: argparse.Namespace, *, include_global_advisories: bool
    ) -> tuple[int, Mock]:
        Path(args.output).write_text("placeholder", encoding="utf-8")
        sys.stdout.write(f"\n{cli._FA_A2_SKIPPED_ADVISORY}\n")
        return 0, _report("schedule-fa", a2_rows=())

    for name in _FY_STEPS:
        monkeypatch.setattr(cli, name, _fake_step(0))
    monkeypatch.setattr(cli, "_schedule_fa_step", _fa_with_a2_skip)

    rc = main(_args(paths))

    assert rc == 0  # informational nudge does not change the exit code
    err = capsys.readouterr().err
    assert "STATUS: ACTION NEEDED" in err
    assert "Before you file (1 item(s) to address):" in err
    assert "[account.custodian]" in err
