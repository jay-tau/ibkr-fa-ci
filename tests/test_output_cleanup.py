"""Output cleanup tests."""

import errno
import os
import re
from collections.abc import Callable
from pathlib import Path

import pytest

from ibkr_fa.output._atomic import atomic_output_path
from ibkr_fa.output._cleanup import (
    ClaimedOutputCleanup,
    FileIdentity,
    OutputCollisionError,
    OutputRemovalError,
    refuse_input_collisions,
    remove_failed_run_outputs,
    remove_output_paths,
    remove_outputs_on_failure,
    remove_previous_outputs,
    scan_inputs,
)
from tests._crash_reports import assert_replaced_unchained
from tests._oserror_markers import FILENAME_MARKER as _FILENAME_MARKER
from tests._oserror_markers import STRERROR_MARKER as _STRERROR_MARKER
from tests._oserror_markers import assert_no_oserror_markers as _assert_no_markers
from tests._oserror_markers import marked_oserror as _marked_oserror


def _fail_for(
    monkeypatch: pytest.MonkeyPatch, owner: object, name: str, path: Path, error: OSError
) -> None:
    """Make ``owner.name`` raise ``error`` when called on ``path``, and only then."""
    original = getattr(owner, name)

    def failing(first: str | os.PathLike[str], *args: object, **kwargs: object) -> object:
        if os.fspath(first) == os.fspath(path):
            raise error
        return original(first, *args, **kwargs)

    monkeypatch.setattr(owner, name, failing)


def _whole(message: str) -> str:
    """A ``pytest.raises`` pattern that matches exactly ``message``."""
    return rf"\A{re.escape(message)}\Z"


def test_remove_output_paths_attempts_every_path_before_raising(tmp_path: Path) -> None:
    invalid = tmp_path / "invalid.csv"
    invalid.mkdir()
    stale = tmp_path / "stale.csv"
    stale.write_text("stale", encoding="utf-8")

    with pytest.raises(IsADirectoryError):
        remove_output_paths((stale, invalid))

    assert invalid.is_dir()
    assert not stale.exists()


def _guard(target: Path, inputs: dict[str, Path]) -> None:
    refuse_input_collisions((target,), tuple(inputs.items()))


@pytest.mark.parametrize("alias", ["exact", "dotdot", "symlink", "hard-link"])
def test_refuse_input_collisions_refuses_every_alias_of_an_input(
    tmp_path: Path, alias: str
) -> None:
    target = tmp_path / "out" / "schedule-os.csv"
    target.parent.mkdir()
    target.write_bytes(b"the user's statement")
    supplied = {
        "exact": target,
        "dotdot": tmp_path / "out" / ".." / "out" / target.name,
        "symlink": tmp_path / "statement-link.csv",
        "hard-link": tmp_path / "statement-hard.csv",
    }[alias]
    if alias == "symlink":
        supplied.symlink_to(target)
    elif alias == "hard-link":
        supplied.hardlink_to(target)

    with pytest.raises(OutputCollisionError, match=r"--input .* is the same file as"):
        _guard(target, {"--config": tmp_path / "user.toml", "--input": supplied})

    assert target.read_bytes() == b"the user's statement"


def test_refuse_input_collisions_ignores_missing_targets_and_inputs(tmp_path: Path) -> None:
    present = tmp_path / "schedule-os.csv"
    present.write_bytes(b"stale")
    other = tmp_path / "statement.csv"
    other.write_bytes(b"statement")

    _guard(tmp_path / "missing.csv", {"--input": other})
    _guard(present, {"--input": tmp_path / "missing-statement.csv", "--config": other})
    # Only FileNotFoundError is exempt; a non-directory component refuses.
    beneath = other / "statement.csv"
    with pytest.raises(
        OutputCollisionError,
        match=_whole(
            f"could not check output target {present} against --input {beneath} (ENOTDIR)"
        ),
    ):
        _guard(present, {"--input": beneath})
    beneath = other / "schedule-os.csv"
    with pytest.raises(
        OutputCollisionError,
        match=_whole(
            f"could not check output target {beneath} against --input {present} (ENOTDIR)"
        ),
    ):
        _guard(beneath, {"--input": present})


def _dangling_target(tmp_path: Path) -> Path:
    """An output target that is a symlink to a missing referent, as on an
    unmounted volume."""
    target = tmp_path / "out" / "schedule-os.csv"
    target.parent.mkdir()
    target.symlink_to(tmp_path / "unmounted" / "config")
    return target


@pytest.mark.parametrize(
    "reach",
    ["exact", "dotdot", "chain", "directory", "chain-to-directory", "dotdot-after-directory"],
)
def test_refuse_input_collisions_refuses_an_input_reached_through_a_dangling_target(
    tmp_path: Path, reach: str
) -> None:
    """``samefile`` can't compare a missing referent, so the target's own entry
    is compared with every entry on the input's path; the walk follows the
    target before applying ``..``."""
    target = _dangling_target(tmp_path)
    link = tmp_path / "config-link"
    supplied = {
        "exact": target,
        "dotdot": tmp_path / "out" / ".." / "out" / target.name,
        "chain": link,
        "directory": target / "config.toml",
        "chain-to-directory": link,
        "dotdot-after-directory": target / ".." / "config.toml",
    }[reach]
    if reach == "chain":
        link.symlink_to(target)
    elif reach == "chain-to-directory":
        link.symlink_to(target / "config.toml")

    with pytest.raises(OutputCollisionError, match=r"--config .* is or goes through"):
        remove_previous_outputs((target,), (("--config", supplied),))

    assert target.readlink() == tmp_path / "unmounted" / "config"


@pytest.mark.parametrize(
    ("make_input", "error"),
    [
        ("file-dotdot", "ENOTDIR"),
        ("link-file-dot", "ENOTDIR"),
        ("link-file-slash", "ENOTDIR"),
        ("cycle", "ELOOP"),
        ("past-the-hop-limit", "ELOOP"),
    ],
)
def test_refuse_input_collisions_refuses_an_input_path_it_cannot_walk(
    tmp_path: Path, make_input: str, error: str
) -> None:
    """``realpath`` would normalize ``file/..``, and pathlib drops a link
    target's trailing slash or dot. Those and a 41-symlink lookup refuse before
    cleanup, even beside an unrelated dangling target ``samefile`` skips. The
    refusal names the input as given and the errno name, not its strerror
    (#1048)."""
    target = _dangling_target(tmp_path)
    config = tmp_path / "config.toml"
    config.write_bytes(b"the user's config")
    if make_input == "file-dotdot":
        supplied = config / ".." / config.name
    elif make_input in ("link-file-dot", "link-file-slash"):
        supplied = tmp_path / "config-link"
        supplied.symlink_to(config.name + ("/." if make_input == "link-file-dot" else "/"))
    elif make_input == "cycle":
        (tmp_path / "loop").symlink_to(tmp_path / "loop")
        supplied = tmp_path / "loop" / "config.toml"
    else:
        supplied = _symlink_chain(tmp_path, config, hops=41)

    with pytest.raises(OutputCollisionError) as raised:
        remove_previous_outputs((target,), (("--config", supplied),))

    message = str(raised.value)
    assert message == f"could not inspect --config {supplied} ({error})"
    assert os.strerror(getattr(errno, error)).lower() not in message.lower()
    # Nor anywhere in its exception graph: it chains no error (#1623).
    assert_replaced_unchained(raised.value, markers=(os.strerror(getattr(errno, error)),))
    assert os.path.lexists(target)
    assert config.read_bytes() == b"the user's config"


def _symlink_chain(tmp_path: Path, end: Path, *, hops: int) -> Path:
    """The first of ``hops`` symlinks, each pointing at the next; the last
    points at ``end``."""
    links = [tmp_path / f"link-{index}" for index in range(hops)]
    for link, referent in zip(links, [*links[1:], end], strict=True):
        link.symlink_to(referent)
    return links[0]


def test_refuse_input_collisions_follows_a_chain_at_the_hop_limit(tmp_path: Path) -> None:
    """Linux follows 40 symlinks in one lookup, so the walk does too: a chain
    whose 40th symlink is the target still finds it."""
    target = _dangling_target(tmp_path)
    links = _symlink_chain(tmp_path, target, hops=39)

    with pytest.raises(OutputCollisionError, match=r"--config .* is or goes through"):
        remove_previous_outputs((target,), (("--config", links),))

    assert os.path.lexists(target)


@pytest.mark.parametrize("operation", ["target-lstat", "input-lstat", "readlink"])
def test_refuse_input_collisions_refuses_when_entries_cannot_be_inspected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, operation: str
) -> None:
    target = _dangling_target(tmp_path)
    statement = tmp_path / "statement.csv"
    statement.write_bytes(b"statement")
    supplied = tmp_path / "statement-link.csv"
    supplied.symlink_to(statement)
    function = "readlink" if operation == "readlink" else "lstat"
    original = getattr(os, function)
    inaccessible = target if operation == "target-lstat" else supplied

    def cannot_inspect(path: str | Path, *args: object, **kwargs: object) -> object:
        if os.fspath(path) == os.fspath(inaccessible):
            raise PermissionError(13, "Permission denied", os.fspath(path))
        return original(path, *args, **kwargs)

    monkeypatch.setattr(os, function, cannot_inspect)
    with pytest.raises(OutputCollisionError) as raised:
        remove_previous_outputs((target,), (("--input", supplied),))
    monkeypatch.undo()

    named = f"output target {target}" if operation == "target-lstat" else f"--input {supplied}"
    assert str(raised.value) == f"could not inspect {named} (EACCES)"
    assert "permission denied" not in str(raised.value).lower()
    if operation == "target-lstat":
        # Kept: that error names only the typed target.
        assert type(raised.value.__cause__) is PermissionError
    else:
        # The walk's error can name an entry the user never typed (#1623).
        assert_replaced_unchained(raised.value, markers=("Permission denied",))
    assert os.path.lexists(target)
    assert statement.read_bytes() == b"statement"


def test_refuse_input_collisions_refuses_a_target_symlinked_to_an_input(tmp_path: Path) -> None:
    """Control, unchanged: ``samefile`` follows a live target symlink to the
    input it points at."""
    statement = tmp_path / "statement.csv"
    statement.write_bytes(b"statement")
    target = tmp_path / "schedule-os.csv"
    target.symlink_to(statement)

    with pytest.raises(OutputCollisionError, match=r"--input .* is the same file as"):
        _guard(target, {"--input": statement})


def test_remove_previous_outputs_removes_a_dangling_target_no_input_goes_through(
    tmp_path: Path,
) -> None:
    """Control, unchanged: a dangling symlink at a target is an earlier
    output, not an input, unless an input's path goes through it."""
    target = _dangling_target(tmp_path)
    config = tmp_path / "config.toml"
    config.write_bytes(b"the user's config")
    (tmp_path / "config-link").symlink_to(config)

    remove_previous_outputs(
        (target,), (("--config", tmp_path / "config-link"), ("--input", tmp_path / "missing.csv"))
    )

    assert not os.path.lexists(target)
    assert config.read_bytes() == b"the user's config"


def test_scan_inputs_matches_glob_and_skips_missing_or_non_directory_folders(
    tmp_path: Path,
) -> None:
    bundle = tmp_path / "bundle"
    (bundle / "nested").mkdir(parents=True)
    for name in ("a.pdf", "b.csv", "nested/c.pdf", ".hidden.pdf"):
        (bundle / name).write_bytes(b"x")
    (bundle / "linked").symlink_to(bundle / "nested")

    assert scan_inputs(bundle, "*.pdf") == sorted(bundle.glob("*.pdf"))
    assert scan_inputs(bundle, "*.pdf", recursive=True) == sorted(bundle.rglob("*.pdf"))
    assert scan_inputs(tmp_path / "missing", "*.pdf") == []
    assert scan_inputs(bundle / "a.pdf", "*.pdf") == []
    beneath_a_file = bundle / "a.pdf" / "nested"
    with pytest.raises(
        OutputCollisionError,
        match=_whole(
            f"could not list {beneath_a_file} to check it against the output targets (ENOTDIR)"
        ),
    ):
        scan_inputs(beneath_a_file, "*.pdf")


@pytest.mark.parametrize("operation", ["target-stat", "input-stat", "samefile"])
def test_refuse_input_collisions_refuses_when_files_cannot_be_inspected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, operation: str
) -> None:
    target = tmp_path / "schedule-os.csv"
    target.write_bytes(b"stale")
    supplied = tmp_path / "statement.csv"
    supplied.write_bytes(b"statement")
    method = "samefile" if operation == "samefile" else "stat"
    original = getattr(Path, method)
    inaccessible = supplied if operation == "input-stat" else target

    def cannot_inspect(path: Path, *args: object, **kwargs: object) -> object:
        if path == inaccessible:
            raise PermissionError("cannot inspect file")
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, method, cannot_inspect)
    with pytest.raises(OutputCollisionError) as raised:
        _guard(target, {"--input": supplied})
    monkeypatch.undo()
    # An OSError without an errno prints "unknown error", not its text (#1048).
    assert str(raised.value) == (
        f"could not check output target {target} against --input {supplied} (unknown error)"
    )
    assert "cannot inspect file" not in str(raised.value).lower()
    assert target.read_bytes() == b"stale"
    assert supplied.read_bytes() == b"statement"


def test_failed_cleanup_after_a_removal_error_names_both_paths(tmp_path: Path) -> None:
    target = tmp_path / "out.csv"
    target.mkdir()
    staging_error = OutputRemovalError(f"could not remove the staging file {tmp_path / '.x.tmp'}")

    with pytest.raises(OutputRemovalError) as raised, remove_outputs_on_failure((target,)):
        raise staging_error

    assert str(raised.value) == (
        f"the run failed, and its output may still be there: could not remove {target} (EISDIR); "
        f"{staging_error}"
    )
    assert "is a directory" not in str(raised.value).lower()
    assert_replaced_unchained(raised.value)


def test_failed_cleanup_after_a_staging_removal_error_prints_only_paths_and_errno_names(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A writer's own ``OutputRemovalError`` (a staging file it could not
    remove) and the cleanup's error that wraps it each name their path and an
    errno name; neither prints a cause's ``OSError`` text or file name, nor the
    staging file's temp name (#1048)."""
    target = tmp_path / "schedule-fa.xlsx"
    target.write_bytes(b"an earlier run's output")
    unlink = Path.unlink

    def cannot_remove(path: Path, missing_ok: bool = False) -> None:
        if path.parent == tmp_path:
            raise _marked_oserror()
        unlink(path, missing_ok=missing_ok)

    monkeypatch.setattr(Path, "unlink", cannot_remove)
    with (
        pytest.raises(OutputRemovalError) as raised,
        remove_outputs_on_failure((target,)),
        atomic_output_path(target),
    ):
        raise RuntimeError("the writer failed")
    monkeypatch.undo()

    message = str(raised.value)
    staging = (
        "the write failed, and its output may still be there: could not remove the staging "
        f"file {tmp_path / '.schedule-fa.xlsx.*.tmp'} (EACCES)"
    )
    assert message == (
        f"the run failed, and its output may still be there: could not remove {target} "
        f"(EACCES); {staging}"
    )
    _assert_no_markers(message)
    [staged] = tmp_path.glob(".schedule-fa.xlsx.*.tmp")
    assert staged.name not in message
    assert_replaced_unchained(
        raised.value,
        RuntimeError,
        markers=("the writer failed", _STRERROR_MARKER, _FILENAME_MARKER),
    )
    assert target.read_bytes() == b"an earlier run's output"


_PUBLISHED = b"this run's output"


def _published(tmp_path: Path) -> tuple[Path, FileIdentity]:
    """A file this run published, and its identity."""
    target = tmp_path / "schedule-fa.json"
    target.write_bytes(_PUBLISHED)
    info = os.lstat(target)
    return target, (info.st_dev, info.st_ino)


@pytest.mark.parametrize("error_type", [RuntimeError, KeyboardInterrupt])
def test_claimed_output_cleanup_removes_the_claimed_file(
    tmp_path: Path, error_type: type[BaseException]
) -> None:
    target, identity = _published(tmp_path)
    error = error_type("refused after the write")

    with pytest.raises(error_type) as raised, ClaimedOutputCleanup(target) as cleanup:
        cleanup.claim(identity)
        raise error

    assert raised.value is error
    assert not os.path.lexists(target)


@pytest.mark.parametrize(
    ("case", "left"),
    [("unclaimed", _PUBLISHED), ("replaced", b"another file"), ("removed", None)],
    ids=["unclaimed", "replaced", "removed"],
)
def test_claimed_output_cleanup_leaves_a_file_it_did_not_publish(
    tmp_path: Path, case: str, left: bytes | None
) -> None:
    """Unclaimed, the target may predate the run; claimed, it may have been
    replaced or removed since."""
    target, identity = _published(tmp_path)
    if case == "replaced":
        other = tmp_path / "other.json"
        other.write_bytes(b"another file")
        other.replace(target)
    elif case == "removed":
        target.unlink()
    error = RuntimeError("refused after the write")

    with pytest.raises(RuntimeError) as raised, ClaimedOutputCleanup(target) as cleanup:
        if case != "unclaimed":
            cleanup.claim(identity)
        raise error

    assert raised.value is error
    assert (target.read_bytes() if target.exists() else None) == left


@pytest.mark.parametrize("step", ["inspect", "remove"])
@pytest.mark.parametrize("staging_failed", [False, True])
def test_claimed_output_cleanup_that_fails_says_the_output_may_remain(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, step: str, staging_failed: bool
) -> None:
    target, identity = _published(tmp_path)
    error = (
        OutputRemovalError(f"could not remove the staging file {tmp_path / '.x.tmp'}")
        if staging_failed
        else RuntimeError("refused after the write")
    )
    lstat, unlink = os.lstat, Path.unlink

    def cannot_inspect(path: Path) -> os.stat_result:
        if Path(path) == target:
            raise PermissionError(13, "Permission denied", str(path))
        return lstat(path)

    def cannot_remove(path: Path, missing_ok: bool = False) -> None:
        if path == target:
            raise PermissionError(13, "Permission denied", str(path))
        unlink(path, missing_ok=missing_ok)

    if step == "inspect":
        monkeypatch.setattr("ibkr_fa.output._cleanup.os.lstat", cannot_inspect)
    else:
        monkeypatch.setattr(Path, "unlink", cannot_remove)

    with pytest.raises(OutputRemovalError) as raised, ClaimedOutputCleanup(target) as cleanup:
        cleanup.claim(identity)
        raise error

    message = str(raised.value)
    assert message == (
        f"the run failed, and its output may still be there: could not {step} {target} (EACCES)"
        + (f"; {error}" if staging_failed else "")
    )
    assert "permission denied" not in message.lower()
    assert (str(error) in message) is staging_failed
    if staging_failed:
        assert_replaced_unchained(raised.value)
    else:
        assert_replaced_unchained(raised.value, RuntimeError, markers=(str(error),))
    monkeypatch.undo()
    assert target.read_bytes() == _PUBLISHED


def test_claimed_output_cleanup_reports_its_claim(tmp_path: Path) -> None:
    target, identity = _published(tmp_path)
    cleanup = ClaimedOutputCleanup(target)

    assert not cleanup.claimed
    cleanup.claim(identity)
    assert cleanup.claimed


@pytest.mark.parametrize(
    ("case", "left"),
    [("claimed", None), ("unclaimed", _PUBLISHED), ("replaced", b"another file")],
)
def test_remove_failed_run_outputs_removes_a_claimed_path_only_while_it_is_the_claimed_file(
    tmp_path: Path, case: str, left: bytes | None
) -> None:
    """A run that fails without raising, such as ``robinhood-all`` with an ERROR leg."""
    target, identity = _published(tmp_path)
    other = tmp_path / "schedule-fa.csv"
    other.write_bytes(b"this run's other output")
    cleanup = ClaimedOutputCleanup(target)
    if case != "unclaimed":
        cleanup.claim(identity)
    if case == "replaced":
        replacement = tmp_path / "other.json"
        replacement.write_bytes(b"another file")
        replacement.replace(target)

    remove_failed_run_outputs((other,), (cleanup,))

    assert not other.exists()
    assert (target.read_bytes() if target.exists() else None) == left


def test_remove_failed_run_outputs_names_every_path_it_could_not_inspect_or_remove(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target, identity = _published(tmp_path)
    stuck = tmp_path / "schedule-fa.csv"
    stuck.mkdir()
    cleanup = ClaimedOutputCleanup(target)
    cleanup.claim(identity)
    lstat = os.lstat

    def cannot_inspect(path: Path) -> os.stat_result:
        if Path(path) == target:
            raise PermissionError(13, "Permission denied", str(path))
        return lstat(path)

    monkeypatch.setattr("ibkr_fa.output._cleanup.os.lstat", cannot_inspect)

    with pytest.raises(OutputRemovalError) as raised:
        remove_failed_run_outputs((stuck,), (cleanup,))

    message = str(raised.value)
    assert message == (
        f"the run failed, and its output may still be there: could not remove {stuck} (EISDIR); "
        f"could not inspect {target} (EACCES)"
    )
    assert "permission denied" not in message.lower()
    assert "is a directory" not in message.lower()
    monkeypatch.undo()
    assert target.read_bytes() == _PUBLISHED


# ---- #1048: wrapped OSError refusals print fixed text, an allowed path and errno


# Not in ``errno.errorcode``, so it has no name to print.
_UNMAPPED_ERRNO = 99999

# kind → the OSError, and the name the refusal prints for it.
_OSERRORS: dict[str, tuple[Callable[[], OSError], str]] = {
    "eacces": (_marked_oserror, "EACCES"),
    "no-errno": (lambda: OSError(_STRERROR_MARKER), "unknown error"),
    "unmapped-errno": (
        lambda: OSError(_UNMAPPED_ERRNO, _STRERROR_MARKER, f"{_FILENAME_MARKER}/x"),
        "unknown error",
    ),
}

# A site's call, the error it raises, and that error's text before " ({errno name})".
type _SiteCall = tuple[Callable[[], object], type[Exception], str]


def _samefile_site(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, error: OSError) -> _SiteCall:
    target = tmp_path / "schedule-os.csv"
    target.write_bytes(b"stale")
    supplied = tmp_path / "statement.csv"
    supplied.write_bytes(b"statement")
    _fail_for(monkeypatch, Path, "samefile", target, error)
    return (
        lambda: _guard(target, {"--input": supplied}),
        OutputCollisionError,
        f"could not check output target {target} against --input {supplied}",
    )


def _target_lstat_site(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, error: OSError
) -> _SiteCall:
    target = _dangling_target(tmp_path)
    supplied = tmp_path / "statement.csv"
    supplied.write_bytes(b"statement")
    _fail_for(monkeypatch, os, "lstat", target, error)
    return (
        lambda: remove_previous_outputs((target,), (("--input", supplied),)),
        OutputCollisionError,
        f"could not inspect output target {target}",
    )


def _input_walk_site(
    function: str,
) -> Callable[[Path, pytest.MonkeyPatch, OSError], _SiteCall]:
    """``os.{function}`` fails on the input's own entry while its path is walked."""

    def site(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, error: OSError) -> _SiteCall:
        target = _dangling_target(tmp_path)
        statement = tmp_path / "statement.csv"
        statement.write_bytes(b"statement")
        supplied = tmp_path / "statement-link.csv"
        supplied.symlink_to(statement)
        _fail_for(monkeypatch, os, function, supplied, error)
        return (
            lambda: remove_previous_outputs((target,), (("--input", supplied),)),
            OutputCollisionError,
            f"could not inspect --input {supplied}",
        )

    return site


def _scan_site(
    owner: object, name: str
) -> Callable[[Path, pytest.MonkeyPatch, OSError], _SiteCall]:
    """``owner.name`` fails on the typed folder ``scan_inputs`` lists."""

    def site(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, error: OSError) -> _SiteCall:
        folder = tmp_path / "bundle"
        folder.mkdir()
        (folder / "statement.pdf").write_bytes(b"statement")
        _fail_for(monkeypatch, owner, name, folder, error)
        return (
            lambda: scan_inputs(folder, "*.pdf"),
            OutputCollisionError,
            f"could not list {folder} to check it against the output targets",
        )

    return site


def _previous_output_site(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, error: OSError
) -> _SiteCall:
    target = tmp_path / "schedule-os.csv"
    target.write_bytes(b"stale")
    _fail_for(monkeypatch, Path, "unlink", target, error)
    return (
        lambda: remove_previous_outputs((target,), ()),
        OutputRemovalError,
        f"could not remove the previous output {target}",
    )


def _failed_run_output_site(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, error: OSError
) -> _SiteCall:
    target = tmp_path / "schedule-os.csv"
    target.write_bytes(_PUBLISHED)
    _fail_for(monkeypatch, Path, "unlink", target, error)
    return (
        lambda: remove_failed_run_outputs((target,)),
        OutputRemovalError,
        f"the run failed, and its output may still be there: could not remove {target}",
    )


def _claim_lstat_site(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, error: OSError) -> _SiteCall:
    target, identity = _published(tmp_path)
    cleanup = ClaimedOutputCleanup(target)
    cleanup.claim(identity)
    _fail_for(monkeypatch, os, "lstat", target, error)
    return (
        lambda: remove_failed_run_outputs((), (cleanup,)),
        OutputRemovalError,
        f"the run failed, and its output may still be there: could not inspect {target}",
    )


# The wrapped OSError refusal sites in ``_cleanup.py``, by the call that fails.
_OSERROR_SITES: dict[str, Callable[[Path, pytest.MonkeyPatch, OSError], _SiteCall]] = {
    "samefile": _samefile_site,
    "target-lstat": _target_lstat_site,
    "input-walk-lstat": _input_walk_site("lstat"),
    "input-walk-readlink": _input_walk_site("readlink"),
    "scan-folder-stat": _scan_site(Path, "stat"),
    "scan-scandir": _scan_site(os, "scandir"),
    "previous-output-unlink": _previous_output_site,
    "failed-run-unlink": _failed_run_output_site,
    "claimed-output-lstat": _claim_lstat_site,
}


@pytest.mark.parametrize("error_kind", list(_OSERRORS))
@pytest.mark.parametrize("site", list(_OSERROR_SITES))
def test_an_oserror_prints_only_fixed_text_the_path_and_its_errno_name(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, site: str, error_kind: str
) -> None:
    """Each wrapped ``OSError`` refusal prints fixed text, the path its caller passed and
    the errno name, or "unknown error" for no errno or an unmapped one; never
    the error's strerror or file name (#1048)."""
    assert _UNMAPPED_ERRNO not in errno.errorcode
    make_error, name = _OSERRORS[error_kind]
    call, error_type, prefix = _OSERROR_SITES[site](tmp_path, monkeypatch, make_error())

    with pytest.raises(error_type) as raised:
        call()
    monkeypatch.undo()

    message = str(raised.value)
    assert message == f"{prefix} ({name})"
    _assert_no_markers(message)


def test_scan_inputs_names_an_unlistable_subfolder_joined_to_the_typed_folder(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A subfolder found by listing the typed folder is named as ``Path`` joins
    it to that folder, not by the ``OSError``'s file name, and a relative
    typed folder stays relative (#1048)."""
    (tmp_path / "bundle" / "nested").mkdir(parents=True)
    (tmp_path / "bundle" / "statement.pdf").write_bytes(b"statement")
    monkeypatch.chdir(tmp_path)
    folder = Path("bundle")
    error = _marked_oserror()
    _fail_for(monkeypatch, os, "scandir", folder / "nested", error)

    with pytest.raises(OutputCollisionError) as raised:
        scan_inputs(folder, "*.pdf", recursive=True)

    message = str(raised.value)
    assert message == (
        f"could not list {folder / 'nested'} to check it against the output targets (EACCES)"
    )
    _assert_no_markers(message)
    # Base's relative filename already omitted cwd; exact text and markers detect the change.
    assert str(tmp_path).lower() not in message.lower()
    assert raised.value.__cause__ is error


@pytest.mark.parametrize(
    ("reach", "name"), [("file", "ENOTDIR"), ("loop", "ELOOP"), ("unsearchable", "EACCES")]
)
def test_refuse_input_collisions_never_names_a_folder_a_symlinked_input_goes_through(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, reach: str, name: str
) -> None:
    """A typed relative input whose symlink leads into a folder named
    ``Synthetic Holder Two`` refuses naming only the input as typed: neither
    that folder (a symlink's target) nor the working directory ``absolute()``
    adds (#1048), and it chains no error, so neither is anywhere in its
    exception graph (#1623). ``file``: the link leads to a regular file with
    more path after it, which before #1048 printed the walked entry. ``loop``:
    the link loops back through the folder; before #1048 that printed the
    strerror but already omitted the folder and working directory. Those checks
    guard future echoes; its exact errno message detects the change.
    ``unsearchable``: ``lstat`` on the file in the folder fails with
    ``EACCES``, whose file name is that walked entry; before #1623 the refusal
    chained it."""
    target = _dangling_target(tmp_path)
    holder = tmp_path / _FILENAME_MARKER
    holder.mkdir()
    statement = holder / "statement.csv"
    statement.write_bytes(b"the user's statement")
    if reach == "loop":
        (holder / "loop").symlink_to("../link")
        (tmp_path / "link").symlink_to(f"{_FILENAME_MARKER}/loop")
    else:
        (tmp_path / "link").symlink_to(f"{_FILENAME_MARKER}/{statement.name}")
    if reach == "unsearchable":
        # As a mode-000 folder would, which root could still search.
        denied = PermissionError(errno.EACCES, os.strerror(errno.EACCES), os.fspath(statement))
        _fail_for(monkeypatch, os, "lstat", statement, denied)
    monkeypatch.chdir(tmp_path)
    supplied = Path("link") / "x"

    with pytest.raises(OutputCollisionError) as raised:
        remove_previous_outputs((target,), (("--input", supplied),))

    message = str(raised.value)
    assert message == f"could not inspect --input {supplied} ({name})"
    assert _FILENAME_MARKER.lower() not in message.lower()
    assert str(tmp_path).lower() not in message.lower()
    assert_replaced_unchained(raised.value, markers=(_FILENAME_MARKER, str(tmp_path)))
    assert os.path.lexists(target)
    assert statement.read_bytes() == b"the user's statement"
