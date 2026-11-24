"""A NetBenefits or Robinhood Schedule FA JSON run that fails removes only the
fragment it staged, and says what happened (#801).

The writer passes ``on_staged`` its staging file's identity before publishing
it, so the run tells a failure before staging (a missing value stays manual
fill; a file that has appeared at the destination is not the run's, so it is
refused and kept) from one after it (publication is uncertain: the run refuses,
and removes the destination only while it is still the claimed file). IBKR's
emitter does the same (#774, ``tests/test_standalone_output_cleanup.py``).
"""

from __future__ import annotations

import errno
import os
from collections.abc import Callable, Sized
from functools import partial
from pathlib import Path
from typing import Any, NoReturn

import pytest

import ibkr_fa.pipeline_netbenefits_fa as netbenefits_fa
import ibkr_fa.pipeline_robinhood as pipeline
import ibkr_fa.pipeline_robinhood_outputs as outputs
from ibkr_fa.cli import main
from ibkr_fa.exceptions import IbkrFaError, InvariantError
from ibkr_fa.output._cleanup import FileIdentity, OutputRemovalError
from ibkr_fa.output.cbdt_json_fragment import (
    CbdtJsonIncompleteError,
    CbdtScheduleFaJsonWriter,
)
from ibkr_fa.pipeline_netbenefits_fa import NetBenefitsFaError
from ibkr_fa.pipeline_robinhood import RobinhoodPipelineError
from tests._crash_reports import assert_replaced_unchained
from tests._fragment_sources import OTHER_FRAGMENT_BYTES as _OTHER
from tests._fragment_sources import FragmentSource as _Source
from tests._fragment_sources import place_other_file as _place_other_file
from tests._fragment_sources import robinhood_all_source as _robinhood_all
from tests._fragment_sources import robinhood_fa_source as _robinhood_fa
from tests._robinhood_cli import LEG_LINE
from tests._robinhood_fa_json import write_fa_json
from tests._robinhood_income_stubs import (  # noqa: F401 - autouse fixture
    silenced_default_income_warnings as _silence_default_income_warnings,
)
from tests._source_commands import prepare_source_command

# Markers a failure can carry, which no refusal may print (#1048): an exception's
# message, and an OSError's text and file name, can be anything.
_HOLDER = "Synthetic Holder Two"
_STRERROR = "Synthetic Strerror Marker"
# The real functions, before any test patches them.
_REPLACE = os.replace
_LSTAT = os.lstat


def _marker_error() -> PermissionError:
    """An EACCES failure whose text and file name are markers."""
    return PermissionError(errno.EACCES, _STRERROR, f"{_HOLDER}/x")


def _assert_omits_markers(text: str) -> None:
    """Unsupplied markers guard future echoes; their absence alone does not
    demonstrate a change from base."""
    for marker in (_HOLDER, _STRERROR):
        assert marker.lower() not in text.lower()


def _appear_then_refuse(
    monkeypatch: pytest.MonkeyPatch, failure: Exception, then: Callable[[], None] = lambda: None
) -> None:
    """Make the writer put another file at its destination, run ``then`` and
    raise ``failure``, all before it stages anything."""

    def write(
        _self: CbdtScheduleFaJsonWriter,
        _value: object,
        path: Path,
        *,
        on_staged: Callable[[FileIdentity], None] | None = None,
    ) -> NoReturn:
        _place_other_file(path)
        then()
        raise failure

    monkeypatch.setattr(CbdtScheduleFaJsonWriter, "write", write)


def _fail_replace(
    monkeypatch: pytest.MonkeyPatch,
    fragment: Path,
    failure: BaseException,
    *,
    publish: bool = True,
    then: Callable[[], None] = lambda: None,
) -> None:
    """Make the atomic writer's ``os.replace`` of ``fragment`` raise ``failure``:
    after the real replace and ``then`` when ``publish``, otherwise without
    replacing. Every other replace is real."""

    def replace(src: str, dst: Path) -> None:
        if Path(dst) != fragment:
            _REPLACE(src, dst)
            return
        if publish:
            _REPLACE(src, dst)
            then()
        raise failure

    monkeypatch.setattr("ibkr_fa.output._atomic.os.replace", replace)


def _fail_lstat(monkeypatch: pytest.MonkeyPatch, path: Path, armed: Sized) -> list[Path]:
    """Make ``os.lstat`` of ``path`` raise ``PermissionError`` once ``armed`` is
    non-empty. Returns one entry per refused inspection."""
    refused: list[Path] = []

    def lstat(target: Path, *args: Any, **kwargs: Any) -> os.stat_result:
        if armed and Path(target) == path:
            refused.append(path)
            raise _marker_error()
        return _LSTAT(target, *args, **kwargs)

    monkeypatch.setattr(os, "lstat", lstat)
    return refused


def _netbenefits(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, request: pytest.FixtureRequest
) -> _Source:
    run, _ = prepare_source_command("netbenefits-fa-json", tmp_path, monkeypatch, request)
    return _Source(
        run.out / "schedule-fa.json", NetBenefitsFaError, run.entry, partial(main, run.argv)
    )


def _write_fa(
    tmp_path: Path, _monkeypatch: pytest.MonkeyPatch, _request: pytest.FixtureRequest
) -> _Source:
    """Robinhood's ``write_fa``, inside the claim its runs arm around it."""
    out = tmp_path / "out"
    out.mkdir()
    return _Source(
        out / outputs.JSON_FILENAME,
        RobinhoodPipelineError,
        partial(write_fa_json, out),
    )


_SOURCES = {
    "netbenefits-fa": _netbenefits,
    "robinhood-fa": _robinhood_fa,
    "write_fa": _write_fa,
    "robinhood-all": _robinhood_all,
}
_STANDALONE = ["netbenefits-fa", "robinhood-fa"]
# Every way to run a source that refuses with its typed error (exit 4 at the CLI).
_REFUSING = [
    *((name, layer) for name in _STANDALONE for layer in ("cli", "entry")),
    ("write_fa", "entry"),
]


def _refused(
    source: _Source, layer: str, capsys: pytest.CaptureFixture[str]
) -> tuple[str, BaseException | None]:
    """Run ``source`` through ``layer``, which refuses with exit 4 at the CLI and
    the typed error at the entry point. Returns the message and that error."""
    if layer == "cli":
        assert source.cli is not None
        assert source.cli() == 4
        return capsys.readouterr().err, None
    # Catch the base's frozen errors too, so pytest cannot mask them with TypeError.
    with pytest.raises(IbkrFaError) as raised:
        source.entry()
    assert isinstance(raised.value, source.refusal)
    return str(raised.value), raised.value


def _assert_refused_with(message: str, layer: str, expected: str) -> None:
    """``message`` from :func:`_refused` is exactly ``expected``: the CLI's one
    stderr line, or the error's text."""
    assert message == (f"error: {expected}\n" if layer == "cli" else expected)
    _assert_omits_markers(message)


def _all_statuses(stdout: str) -> dict[str, str]:
    return {name: status for status, name in LEG_LINE.findall(stdout)}


# --- Before staging --------------------------------------------------------


@pytest.mark.parametrize(("name", "layer"), _REFUSING)
def test_a_file_appearing_before_staging_is_refused_and_kept(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    request: pytest.FixtureRequest,
    capsys: pytest.CaptureFixture[str],
    name: str,
    layer: str,
) -> None:
    source = _SOURCES[name](tmp_path, monkeypatch, request)
    failure = CbdtJsonIncompleteError(["a value missing before staging"])
    _appear_then_refuse(monkeypatch, failure)

    message, error = _refused(source, layer, capsys)

    assert f"{source.fragment} appeared" in message
    assert "this run did not write it" in message
    if error is not None:
        assert error.__cause__ is failure
    assert source.fragment.read_bytes() == _OTHER


def test_all_a_file_appearing_before_staging_fails_the_leg_and_is_kept(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    request: pytest.FixtureRequest,
    capsys: pytest.CaptureFixture[str],
) -> None:
    source = _robinhood_all(tmp_path, monkeypatch, request)
    _appear_then_refuse(monkeypatch, CbdtJsonIncompleteError(["a value missing before staging"]))

    assert source.cli is not None
    assert source.cli() == 4

    stdout = capsys.readouterr().out
    assert _all_statuses(stdout)["schedule-fa"] == "error"
    assert f"{source.fragment} appeared" in stdout
    assert "this run did not write it" in stdout
    assert source.left() == ["schedule-fa.json"]
    assert source.fragment.read_bytes() == _OTHER


@pytest.mark.parametrize(
    ("bug", "kind"),
    [(RuntimeError(_HOLDER), "RuntimeError"), (_marker_error(), "PermissionError: EACCES")],
    ids=["runtime-error", "os-error"],
)
@pytest.mark.parametrize(("name", "layer"), _REFUSING)
def test_an_unexpected_error_before_staging_is_refused(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    request: pytest.FixtureRequest,
    capsys: pytest.CaptureFixture[str],
    name: str,
    layer: str,
    bug: Exception,
    kind: str,
) -> None:
    """#1048: the refusal names the failure's class (and an OSError's errno
    name), never its message, text or file name."""
    source = _SOURCES[name](tmp_path, monkeypatch, request)

    def refuse(_self: CbdtScheduleFaJsonWriter, *_args: object, **_kwargs: object) -> NoReturn:
        raise bug

    monkeypatch.setattr(CbdtScheduleFaJsonWriter, "write", refuse)

    message, error = _refused(source, layer, capsys)

    _assert_refused_with(
        message, layer, f"the Schedule FA JSON fragment {source.fragment} was not written: {kind}"
    )
    if error is not None:
        assert error.__cause__ is bug
    assert source.left() == []


@pytest.mark.parametrize("name", [*_STANDALONE, "write_fa"])
def test_a_destination_that_cannot_be_inspected_before_staging_is_left(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    request: pytest.FixtureRequest,
    name: str,
) -> None:
    source = _SOURCES[name](tmp_path, monkeypatch, request)
    failure = CbdtJsonIncompleteError(["a value missing before staging"])
    armed: list[bool] = []
    refused = _fail_lstat(monkeypatch, source.fragment, armed)
    _appear_then_refuse(monkeypatch, failure, then=lambda: armed.append(True))

    with pytest.raises(IbkrFaError) as raised:
        source.entry()

    assert isinstance(raised.value, OutputRemovalError)
    # #1048: the lstat failure by its errno name only, and the run's failure by
    # its class.
    assert str(raised.value) == (
        f"the Schedule FA JSON fragment was not written, and could not inspect {source.fragment}"
        " (EACCES); it was left untouched (CbdtJsonIncompleteError)"
    )
    _assert_omits_markers(str(raised.value))
    assert raised.value.__cause__ is failure
    assert refused == [source.fragment]
    monkeypatch.undo()
    assert source.fragment.read_bytes() == _OTHER


@pytest.mark.parametrize("layer", ["cli", "entry"])
def test_netbenefits_refuses_a_file_appearing_before_its_write(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    request: pytest.FixtureRequest,
    capsys: pytest.CaptureFixture[str],
    layer: str,
) -> None:
    """The preclear runs before everything the run reads; a file that has
    appeared since would be replaced by a successful write."""
    source = _netbenefits(tmp_path, monkeypatch, request)
    prove = netbenefits_fa.prove_and_build_netbenefits_a2

    def appear_then_prove(*args: Any, **kwargs: Any) -> object:
        _place_other_file(source.fragment)
        return prove(*args, **kwargs)

    monkeypatch.setattr(netbenefits_fa, "prove_and_build_netbenefits_a2", appear_then_prove)

    message, _ = _refused(source, layer, capsys)

    assert f"{source.fragment} already exists and this run did not write it" in message
    assert source.fragment.read_bytes() == _OTHER


# --- After staging ---------------------------------------------------------

# A failure raised after staging, and how the refusal names it (#1048): by its
# class, and an OSError by its errno name too; never its message or text.
_AFTER_STAGING = pytest.mark.parametrize(
    ("failure", "kind"),
    [
        (CbdtJsonIncompleteError(["raised after staging"]), "CbdtJsonIncompleteError"),
        (RuntimeError(_HOLDER), "RuntimeError"),
        (_marker_error(), "PermissionError: EACCES"),
    ],
    ids=["incomplete", "unexpected", "os-error"],
)


@_AFTER_STAGING
@pytest.mark.parametrize(("name", "layer"), _REFUSING)
def test_a_failure_after_publishing_removes_the_fragment_and_refuses(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    request: pytest.FixtureRequest,
    capsys: pytest.CaptureFixture[str],
    name: str,
    layer: str,
    failure: Exception,
    kind: str,
) -> None:
    source = _SOURCES[name](tmp_path, monkeypatch, request)
    published: list[bool] = []
    _fail_replace(monkeypatch, source.fragment, failure, then=lambda: published.append(True))

    message, error = _refused(source, layer, capsys)

    _assert_refused_with(
        message,
        layer,
        f"the Schedule FA JSON fragment {source.fragment} failed after it was staged or "
        f"written: {kind}",
    )
    if error is not None:
        assert error.__cause__ is failure
    assert published == [True]
    assert source.left() == []


@_AFTER_STAGING
def test_all_a_failure_after_publishing_fails_the_leg_and_rolls_back(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    request: pytest.FixtureRequest,
    capsys: pytest.CaptureFixture[str],
    failure: Exception,
    kind: str,
) -> None:
    source = _robinhood_all(tmp_path, monkeypatch, request)
    published: list[bool] = []
    _fail_replace(monkeypatch, source.fragment, failure, then=lambda: published.append(True))

    assert source.cli is not None
    assert source.cli() == 4

    stdout = capsys.readouterr().out
    assert _all_statuses(stdout)["schedule-fa"] == "error"
    assert (
        "automatic Schedule FA JSON failed: the Schedule FA JSON "
        f"fragment {source.fragment} failed after it was staged or written: {kind}\n"
    ) in stdout
    _assert_omits_markers(stdout)
    assert published == [True]
    # The rollback removed every leg's output, and the fragment.
    assert source.left() == []


@pytest.mark.parametrize(("name", "layer"), _REFUSING)
def test_a_fragment_staged_but_not_published_refuses(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    request: pytest.FixtureRequest,
    capsys: pytest.CaptureFixture[str],
    name: str,
    layer: str,
) -> None:
    source = _SOURCES[name](tmp_path, monkeypatch, request)
    failure = OSError(errno.EIO, _STRERROR, f"{_HOLDER}/x")
    _fail_replace(monkeypatch, source.fragment, failure, publish=False)

    message, error = _refused(source, layer, capsys)

    _assert_refused_with(
        message,
        layer,
        f"the Schedule FA JSON fragment {source.fragment} failed after it was staged or "
        "written: OSError: EIO",
    )
    if error is not None:
        assert error.__cause__ is failure
    assert source.left() == []


@pytest.mark.parametrize("change", ["replaced", "removed"])
@pytest.mark.parametrize(("name", "layer"), _REFUSING)
def test_a_fragment_replaced_or_removed_after_publishing_is_refused_not_manual(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    request: pytest.FixtureRequest,
    capsys: pytest.CaptureFixture[str],
    name: str,
    layer: str,
    change: str,
) -> None:
    """Whatever is at the destination by then, the run staged its file: a
    missing value raised now is not manual fill, and a replacement is kept."""
    source = _SOURCES[name](tmp_path, monkeypatch, request)
    failure = CbdtJsonIncompleteError(["raised after the fragment changed"])
    then = (
        partial(_place_other_file, source.fragment)
        if change == "replaced"
        else source.fragment.unlink
    )
    _fail_replace(monkeypatch, source.fragment, failure, then=then)

    message, error = _refused(source, layer, capsys)

    assert f"{source.fragment} failed after it was staged or written" in message
    if error is not None:
        assert error.__cause__ is failure
    if change == "replaced":
        assert source.left() == ["schedule-fa.json"]
        assert source.fragment.read_bytes() == _OTHER
    else:
        assert source.left() == []


@pytest.mark.parametrize("name", _SOURCES)
def test_an_interrupt_after_publishing_removes_the_fragment(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    request: pytest.FixtureRequest,
    name: str,
) -> None:
    source = _SOURCES[name](tmp_path, monkeypatch, request)
    interrupt = KeyboardInterrupt("interrupted after publishing the fragment")
    published: list[bool] = []
    _fail_replace(
        monkeypatch,
        source.fragment,
        interrupt,
        then=lambda: published.append(source.fragment.exists()),
    )

    with pytest.raises(KeyboardInterrupt) as raised:
        source.entry()

    assert raised.value is interrupt
    assert published == [True]
    assert source.left() == []


@pytest.mark.parametrize("name", [*_STANDALONE, "write_fa"])
def test_a_cleanup_that_cannot_inspect_the_fragment_says_so_once(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    request: pytest.FixtureRequest,
    name: str,
) -> None:
    source = _SOURCES[name](tmp_path, monkeypatch, request)
    failure = CbdtJsonIncompleteError(["raised after staging"])
    armed: list[bool] = []
    refused = _fail_lstat(monkeypatch, source.fragment, armed)
    _fail_replace(monkeypatch, source.fragment, failure, then=lambda: armed.append(True))

    with pytest.raises(IbkrFaError) as raised:
        source.entry()

    error = raised.value
    assert isinstance(error, OutputRemovalError)
    # Once: the cleanup's own inspection failure, by its errno name only (#1048).
    assert str(error) == (
        "the run failed, and its output may still be there: could not inspect "
        f"{source.fragment} (EACCES)"
    )
    _assert_omits_markers(str(error))
    # The cleanup replaced the run's refusal of the original error, and keeps
    # none of either, nor the lstat failure (#1211).
    assert_replaced_unchained(
        error,
        markers=(
            "failed after it was staged or written",
            "raised after staging",
            _HOLDER,
            _STRERROR,
        ),
    )
    assert refused == [source.fragment]
    monkeypatch.undo()
    assert source.left() == ["schedule-fa.json"]


# --- A failure after the fragment is written -------------------------------


@pytest.mark.parametrize("replaced", [False, True], ids=["own", "replaced"])
def test_standalone_failure_after_the_write_removes_only_the_runs_fragment(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    request: pytest.FixtureRequest,
    replaced: bool,
) -> None:
    """``write_fa`` returns; the leg report then refuses, in the run's window."""
    source = _robinhood_fa(tmp_path, monkeypatch, request)
    error = InvariantError(what="report refused after the write", context=())
    published: list[bool] = []

    def refuse(*_args: object, **_kwargs: object) -> NoReturn:
        published.append(source.fragment.exists())
        if replaced:
            _place_other_file(source.fragment)
        raise error

    monkeypatch.setattr(pipeline, "RobinhoodLegReport", refuse)

    with pytest.raises(InvariantError) as raised:
        source.entry()

    assert raised.value is error
    assert published == [True]
    if replaced:
        assert source.left() == ["schedule-fa.json"]
        assert source.fragment.read_bytes() == _OTHER
    else:
        assert source.left() == []


@pytest.mark.parametrize("hook", ["_write_cg", "_write_options"], ids=["before", "replacing"])
def test_all_rollback_keeps_a_file_that_is_not_the_runs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    request: pytest.FixtureRequest,
    capsys: pytest.CaptureFixture[str],
    hook: str,
) -> None:
    """Another file appears at the fragment's path after the preclear, before the
    run writes the fragment or by replacing it; the options leg then fails."""
    source = _robinhood_all(tmp_path, monkeypatch, request, failing="options")
    write = getattr(pipeline, hook)

    def place_then_write(*args: object, **kwargs: object) -> object:
        _place_other_file(source.fragment)
        return write(*args, **kwargs)

    monkeypatch.setattr(pipeline, hook, place_then_write)

    assert source.cli is not None
    assert source.cli() == 4

    assert _all_statuses(capsys.readouterr().out)["options"] == "error"
    assert source.left() == ["schedule-fa.json"]
    assert source.fragment.read_bytes() == _OTHER


def test_all_staging_file_left_by_a_failed_fragment_is_a_cleanup_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, request: pytest.FixtureRequest
) -> None:
    """The replace fails and the staging file can't be removed: the run says a
    file may remain, as it did before the fragment was refused as staged."""
    source = _robinhood_all(tmp_path, monkeypatch, request)
    _fail_replace(
        monkeypatch, source.fragment, OSError(errno.EIO, _STRERROR, f"{_HOLDER}/x"), publish=False
    )
    stuck: list[Path] = []
    unlink = Path.unlink

    def cannot_remove_staging(path: Path, missing_ok: bool = False) -> None:
        if path.name.startswith(f".{source.fragment.name}."):
            stuck.append(path)
            raise _marker_error()
        unlink(path, missing_ok=missing_ok)

    monkeypatch.setattr(Path, "unlink", cannot_remove_staging)

    with pytest.raises(pipeline.RobinhoodAllRemovalError) as raised:
        source.entry()

    assert len(stuck) == 1
    # #1048: the staging file by its pattern beside the fragment, not by its own
    # random name, and the unlink failure by its errno name only.
    message = str(raised.value)
    assert message == (
        "leg output cleanup failed: the Schedule FA JSON fragment "
        f"{source.fragment} failed after it was staged or written: OutputRemovalError: the "
        "write failed, and its output may still be there: could not remove the staging file "
        f"{source.fragment.parent}/.schedule-fa.json.*.tmp (EACCES); run outputs were removed"
    )
    assert stuck[0].name not in message
    _assert_omits_markers(message)
    leg = next(leg for leg in raised.value.report.legs if leg.name == "schedule-fa")
    assert leg.status is pipeline.RobinhoodLegStatus.ERROR
