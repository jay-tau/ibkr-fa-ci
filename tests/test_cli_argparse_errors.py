"""argparse errors never print a typed value (#1071).

Each error prints the usage line, then ``<prog>: error: `` and either a
reviewed text that names options and rules alone or fixed text naming the
option when available, and exits 2. A message that isn't on the reviewed list
falls back to the fixed text, whichever exception class carried it.
"""

from __future__ import annotations

import argparse
from collections.abc import Callable
from typing import Any, Final

import pytest

from ibkr_fa import cli
from ibkr_fa.cli import main
from tests._cli_subcommands import subcommands as _commands

_MARKERS: Final = ("Synthetic Holder Two", "U1234567", "Synthetic Strerror Marker")
_REQUIRED: Final = (
    *("--input", "in.csv", "--config", "user.toml"),
    *("--output", "out.csv", "--calendar-year", "2025"),
)
_ROOT_FALLBACK: Final = "ibkr-fa: error: unrecognized or ambiguous arguments; run 'ibkr-fa --help'"


def _schedule_fa(*extra: str) -> list[str]:
    return ["schedule-fa", *_REQUIRED, *extra]


def _invalid(option: str, prog: str = "ibkr-fa schedule-fa") -> str:
    return f"{prog}: error: argument {option}: invalid value; run '{prog} --help'"


def _refusal(capsys: pytest.CaptureFixture[str], run: Callable[[], object]) -> str:
    """Run ``run``: it exits 2 after the usage line, printing no marker. Returns its last line."""
    with pytest.raises(SystemExit) as raised:
        run()
    assert raised.value.code == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err.startswith("usage: ibkr-fa")
    for marker in _MARKERS:
        assert marker.lower() not in captured.err.lower(), marker
    return captured.err.splitlines()[-1]


@pytest.mark.parametrize(
    ("argv", "expected"),
    [
        pytest.param(
            _schedule_fa("--format", "Synthetic Holder Two"), _invalid("--format"), id="choice"
        ),
        pytest.param(
            _schedule_fa("--calendar-year", "U1234567"), _invalid("--calendar-year"), id="int"
        ),
        pytest.param(
            _schedule_fa("--no-fetch-prices=U1234567"),
            _invalid("--no-fetch-prices"),
            id="explicit-argument",
        ),
        pytest.param(
            _schedule_fa("--a=U1234567"),
            "ibkr-fa schedule-fa: error: unrecognized or ambiguous arguments; "
            "run 'ibkr-fa schedule-fa --help'",
            id="ambiguous-option",
        ),
        pytest.param(_schedule_fa("Synthetic Holder Two"), _ROOT_FALLBACK, id="stray-token"),
        pytest.param(["U1234567"], _invalid("COMMAND", prog="ibkr-fa"), id="command"),
    ],
)
def test_usage_error_names_the_option_but_not_the_value(
    capsys: pytest.CaptureFixture[str], argv: list[str], expected: str
) -> None:
    assert _refusal(capsys, lambda: main(argv)) == expected


class _RaisingAction(argparse.Action):
    def __call__(self, parser, namespace, values, option_string=None):
        raise argparse.ArgumentError(self, "Synthetic Holder Two U1234567")


def _raising_type(spec: str) -> str:
    raise argparse.ArgumentTypeError("Synthetic Strerror Marker")


@pytest.mark.parametrize(
    "probe", [{"action": _RaisingAction}, {"type": _raising_type}], ids=["action", "type"]
)
@pytest.mark.parametrize("on_root", [True, False], ids=["root", "sub-parser"])
def test_unreviewed_message_prints_the_fixed_fallback(
    capsys: pytest.CaptureFixture[str], probe: dict[str, Any], on_root: bool
) -> None:
    parser = cli._build_parser()
    if on_root:
        parser.add_argument("--probe", **probe)
        argv, expected = ["--probe", "x", *_schedule_fa()], _invalid("--probe", prog="ibkr-fa")
    else:
        _commands(parser)["schedule-fa"].add_argument("--probe", **probe)
        argv, expected = _schedule_fa("--probe", "x"), _invalid("--probe")

    assert _refusal(capsys, lambda: parser.parse_args(argv)) == expected


def test_missing_required_argument_still_names_it(capsys: pytest.CaptureFixture[str]) -> None:
    argv = ["schedule-fa", *_REQUIRED[2:]]

    assert _refusal(capsys, lambda: main(argv)) == (
        "ibkr-fa schedule-fa: error: the following arguments are required: --input"
    )


def test_missing_option_value_still_names_the_count(capsys: pytest.CaptureFixture[str]) -> None:
    assert _refusal(capsys, lambda: main(_schedule_fa("--format"))) == (
        "ibkr-fa schedule-fa: error: argument --format: expected one argument"
    )


def _template_parser() -> cli._NoEchoArgumentParser:
    parser = cli._NoEchoArgumentParser(prog="ibkr-fa probe")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--left", action="store_true")
    group.add_argument("--right", action="store_true")
    parser.add_argument("--pair", nargs=2)
    parser.add_argument("--one", nargs=1)
    return parser


@pytest.mark.parametrize(
    ("argv", "expected"),
    [
        pytest.param([], "one of the arguments --left --right is required", id="group"),
        pytest.param(
            ["--left", "--right"], "argument --right: not allowed with argument --left", id="with"
        ),
        pytest.param(
            ["--left", "--pair", "Synthetic Holder Two"],
            "argument --pair: expected 2 arguments",
            id="count",
        ),
        pytest.param(["--left", "--one"], "argument --one: expected 1 argument", id="one"),
    ],
)
def test_reviewed_argparse_templates_print_as_they_stand(
    capsys: pytest.CaptureFixture[str], argv: list[str], expected: str
) -> None:
    with pytest.raises(SystemExit) as raised:
        _template_parser().parse_args(argv)

    assert raised.value.code == 2
    err = capsys.readouterr().err
    assert err.splitlines()[-1] == f"ibkr-fa probe: error: {expected}"
    assert "synthetic holder two" not in err.lower()


def _raise_error(named: bool, message: str) -> type[argparse.Action]:
    class _Action(argparse.Action):
        def __call__(self, parser, namespace, values, option_string=None):
            raise argparse.ArgumentError(self if named else None, message)

    return _Action


@pytest.mark.parametrize(
    ("named", "message"),
    [
        (False, "the following arguments are required: U1234567"),
        (False, "the following arguments are required: --left, U1234567"),
        (False, "one of the arguments --left U1234567 is required"),
        (True, "not allowed with argument U1234567"),
        (True, "expected 1234567 arguments"),
        (True, "U1234567"),
    ],
)
def test_reviewed_template_with_a_typed_token_falls_back(
    capsys: pytest.CaptureFixture[str], named: bool, message: str
) -> None:
    parser = _template_parser()
    parser.add_argument("--probe", action=_raise_error(named, message))

    with pytest.raises(SystemExit) as raised:
        parser.parse_args(["--left", "--probe", "x"])

    assert raised.value.code == 2
    err = capsys.readouterr().err
    fallback = "argument --probe: invalid value" if named else "unrecognized or ambiguous arguments"
    assert err.splitlines()[-1] == f"ibkr-fa probe: error: {fallback}; run 'ibkr-fa probe --help'"
    assert "1234567" not in err


def test_error_prints_fixed_text_for_an_unreviewed_message(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert _refusal(capsys, lambda: cli._build_parser().error("Synthetic Holder Two")) == (
        _ROOT_FALLBACK
    )


@pytest.mark.parametrize(
    ("message", "expected"),
    [
        pytest.param(
            "the following arguments are required: --input",
            "ibkr-fa schedule-fa: error: the following arguments are required: --input",
            id="required",
        ),
        pytest.param(
            "ambiguous option: --a=U1234567 could match --as-of, --allow-masked-account",
            "ibkr-fa schedule-fa: error: unrecognized or ambiguous arguments; "
            "run 'ibkr-fa schedule-fa --help'",
            id="ambiguous",
        ),
    ],
)
def test_error_prints_a_reviewed_message_as_older_pythons_send_it(
    capsys: pytest.CaptureFixture[str], message: str, expected: str
) -> None:
    # Older Python 3.12 releases call error() directly for these, not ArgumentError.
    sub_parser = _commands(cli._build_parser())["schedule-fa"]

    assert _refusal(capsys, lambda: sub_parser.error(message)) == expected


def test_every_parser_is_the_no_echo_parser() -> None:
    parser = cli._build_parser()

    for each in (parser, *_commands(parser).values()):
        assert type(each) is cli._NoEchoArgumentParser
        assert each.exit_on_error is False
