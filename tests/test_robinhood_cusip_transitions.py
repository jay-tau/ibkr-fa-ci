"""Private-sidecar validation for reviewed Robinhood CUSIP transitions."""

from __future__ import annotations

import errno
from pathlib import Path
from typing import NoReturn

import pytest

from ibkr_fa.exceptions import ConfigError
from ibkr_fa.normalize.robinhood.identity_exceptions import (
    _cusip_valid,
    load_configured_cusip_transitions,
    load_cusip_transitions,
)
from ibkr_fa.types import cusip_check_digit
from tests._no_echo import ACCOUNT, FILENAME, STRERROR, assert_no_markers
from tests._robinhood_cusip_transitions import CUSIP_TRANSITION_ACCOUNTS as _ACCOUNTS
from tests._robinhood_cusip_transitions import cusip_transition as _transition


def _write(tmp_path: Path, body: str) -> Path:
    path = tmp_path / "cusip-transitions.toml"
    path.write_text(body, encoding="utf-8")
    return path


def test_loads_exact_account_scoped_transition(tmp_path: Path) -> None:
    (transition,) = load_cusip_transitions(
        _write(tmp_path, _transition()),
        account_numbers=_ACCOUNTS,
    )
    assert transition.account_number == "222222222"
    assert transition.symbol == "XQAO"
    assert transition.old_cusip == "99992G106"
    assert transition.new_cusip == "99992G205"
    assert transition.new_cusip_from.isoformat() == "2026-07-01"
    assert "99992G106" not in repr(transition)


def test_absent_transition_file_configuration_loads_nothing() -> None:
    assert load_cusip_transitions(None, account_numbers=_ACCOUNTS) == ()


def test_configured_relative_path_resolves_beside_user_toml(tmp_path: Path) -> None:
    sidecar = tmp_path / "overrides/robinhood/cusip-transitions.toml"
    sidecar.parent.mkdir(parents=True)
    sidecar.write_text(_transition(), encoding="utf-8")

    transitions = load_configured_cusip_transitions(
        tmp_path / "user.toml",
        "overrides/robinhood/cusip-transitions.toml",
        account_numbers=_ACCOUNTS,
    )

    assert len(transitions) == 1


@pytest.mark.parametrize(
    "body",
    [
        _transition(account_number='"999999999"'),
        _transition(old_cusip='"99992G107"'),
        _transition(new_cusip='"99992G106"'),
        _transition(new_cusip_from='"2026-07-01"'),
        _transition() + _transition(),
        _transition() + "surprise = true\n",
    ],
)
def test_invalid_transition_sidecar_fails_closed(tmp_path: Path, body: str) -> None:
    with pytest.raises(ConfigError):
        load_cusip_transitions(
            _write(tmp_path, body),
            account_numbers=_ACCOUNTS,
        )


_UNMAPPED = max(errno.errorcode) + 1


@pytest.mark.parametrize(
    ("failure", "name"),
    [
        (OSError(errno.EACCES, STRERROR, FILENAME), "EACCES"),
        (OSError(STRERROR), "unknown error"),
        (OSError(_UNMAPPED, STRERROR, FILENAME), "unknown error"),
    ],
    ids=["eacces", "no-errno", "unmapped"],
)
def test_unreadable_transition_file_names_the_setting_as_written(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure: OSError,
    name: str,
) -> None:
    """A read failure prints the setting as the user wrote it and the errno
    name, never the error's text or file name (#1044)."""
    sidecar = tmp_path / "overrides" / "transitions.toml"
    sidecar.parent.mkdir()
    sidecar.write_text(_transition(), encoding="utf-8")

    def failing_open(path: Path, *_args: object, **_kwargs: object) -> NoReturn:
        assert path == sidecar
        raise failure

    monkeypatch.setattr(Path, "open", failing_open)

    with pytest.raises(ConfigError) as raised:
        load_configured_cusip_transitions(
            tmp_path / "user.toml", "overrides/transitions.toml", account_numbers=_ACCOUNTS
        )

    assert str(raised.value) == (
        "ConfigError at field 'robinhood.cusip_transitions_file': could not read "
        f"overrides/transitions.toml ({name})"
    )
    assert_no_markers(str(raised.value))
    assert raised.value.__cause__ is None
    assert raised.value.__context__ is None


def test_invalid_toml_names_only_its_position(tmp_path: Path) -> None:
    """tomllib's message quotes the duplicated key, which may be pasted data."""
    path = _write(tmp_path, f"[{ACCOUNT}]\n[{ACCOUNT}]\n")

    with pytest.raises(ConfigError) as raised:
        load_cusip_transitions(path, account_numbers=_ACCOUNTS)

    assert str(raised.value) == (
        "ConfigError at field 'robinhood.cusip_transitions_file': invalid TOML at line 2, column 10"
    )
    assert_no_markers(str(raised.value), ACCOUNT)
    assert raised.value.__cause__ is None
    assert raised.value.__context__ is None


def test_non_utf8_transition_file_names_only_the_byte_offset(tmp_path: Path) -> None:
    path = tmp_path / "cusip-transitions.toml"
    path.write_bytes(b"# \xff\n")

    with pytest.raises(ConfigError) as raised:
        load_cusip_transitions(path, account_numbers=_ACCOUNTS)

    assert str(raised.value) == (
        "ConfigError at field 'robinhood.cusip_transitions_file': must be UTF-8 text, but "
        "the byte at offset 2 (counting from 0) is not valid UTF-8"
    )


@pytest.mark.parametrize(
    "value",
    ["[" * 2000 + "0" + "]" * 2000, "1" * 5000],
    ids=["deep-array", "oversized-integer"],
)
def test_transition_parser_limits_fail_with_fixed_config_error(tmp_path: Path, value: str) -> None:
    path = _write(tmp_path, f"# {ACCOUNT}\ntransitions = {value}\n")

    with pytest.raises(ConfigError) as raised:
        load_cusip_transitions(path, account_numbers=_ACCOUNTS)

    assert str(raised.value) == (
        "ConfigError at field 'robinhood.cusip_transitions_file': "
        "TOML exceeds supported parsing limits"
    )
    assert raised.value.__cause__ is None
    assert raised.value.__context__ is None


# --- #1190: the transition check uses the shared CUSIP check digit ----------


@pytest.mark.parametrize(
    ("body", "digit"),
    [("99999A10", "2"), ("99999Z99", "0"), ("99999000", "5"), ("ZZZZZZZZ", "0")],
)
def test_transition_checksum_matches_independently_calculated_digits(body: str, digit: str) -> None:
    """#1190: the shared check digit, computed by hand for each synthetic
    body, is the only ninth character the transition check accepts."""
    value = body + cusip_check_digit(body)
    assert value[-1] == digit
    assert _cusip_valid(value)
    for altered in "0123456789":
        assert _cusip_valid(body + altered) is (altered == digit)


@pytest.mark.parametrize(
    "value",
    [
        None,
        123456789,
        True,
        b"99999A102",
        [],
        {},
        "",
        "99999A10",
        "99999A1020",
        "99999a102",
        "99999A10A",
        "99999A10 ",
        "99999A10\n",
        "\uff199999A102",
        "9999-A102",
    ],
)
def test_transition_checksum_keeps_the_type_and_shape_guard(value: object) -> None:
    """#1190: a non-string, or a string that isn't nine ASCII capitals or
    digits, fails the guard before any check digit is computed."""
    assert not _cusip_valid(value)
