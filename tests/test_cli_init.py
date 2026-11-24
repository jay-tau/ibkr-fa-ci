"""CLI tests for the ``ibkr-fa init`` subcommand."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from ibkr_fa.cli import main
from ibkr_fa.config_template import USER_TOML_TEMPLATE

_PERMISSIONS_ENFORCED = pytest.mark.skipif(
    os.name != "posix" or os.geteuid() == 0,
    reason="the platform does not enforce file permissions",
)


def test_main_init_happy_path(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    target = tmp_path / "user.toml"

    exit_code = main(["init", str(target)])

    assert exit_code == 0
    assert target.read_text(encoding="utf-8") == USER_TOML_TEMPLATE
    out = capsys.readouterr().out
    assert "Wrote starter config" in out
    assert "[account] table (required)" in out


def test_main_init_defaults_to_user_toml_in_cwd(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)

    exit_code = main(["init"])

    assert exit_code == 0
    assert (tmp_path / "user.toml").read_text(encoding="utf-8") == USER_TOML_TEMPLATE


def test_main_init_refuses_existing_without_force(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    target = tmp_path / "user.toml"
    target.write_text("PRECIOUS EDITS", encoding="utf-8")

    exit_code = main(["init", str(target)])

    assert exit_code == 4
    err = capsys.readouterr().err
    assert "already exists" in err
    assert "--force" in err
    # In-progress config untouched.
    assert target.read_text(encoding="utf-8") == "PRECIOUS EDITS"


def test_main_init_force_overwrites(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    target = tmp_path / "user.toml"
    target.write_text("stale", encoding="utf-8")

    exit_code = main(["init", str(target), "--force"])

    assert exit_code == 0
    assert target.read_text(encoding="utf-8") == USER_TOML_TEMPLATE


def test_main_init_missing_parent_returns_domain_error(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    target = tmp_path / "missing" / "user.toml"

    exit_code = main(["init", str(target)])

    assert exit_code == 4
    err = capsys.readouterr().err
    assert err.startswith("error:")
    assert "parent directory does not exist" in err
    assert not target.exists()


@_PERMISSIONS_ENFORCED
def test_main_init_parent_in_an_unsearchable_folder_reports_eacces(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], request: pytest.FixtureRequest
) -> None:
    """#1238: the parent-folder check raises EACCES instead of calling the folder missing."""
    blocked = tmp_path / "blocked"
    parent = blocked / "config"
    parent.mkdir(parents=True)
    blocked.chmod(0)
    request.addfinalizer(lambda: blocked.chmod(0o700))
    target = parent / "user.toml"

    exit_code = main(["init", str(target)])

    assert exit_code == 4
    captured = capsys.readouterr()
    assert captured.err == f"error: could not write {target} (EACCES)\n"
    assert captured.out == ""
    blocked.chmod(0o700)
    assert list(parent.iterdir()) == []
